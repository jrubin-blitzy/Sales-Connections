"""Audit-event emitter (F-013).

This module is the SOLE writer of the ``audit_events`` table. All
state-changing services call :func:`emit_audit_event` inside their
parent transaction so the audit row and the state change commit (or
roll back) together. AAP section 0.7.1 invariant 6 (atomic
state-change + audit pair) is enforced here.

The function only ever issues an INSERT (no UPDATE, no DELETE) per
AAP section 0.7.1 invariant 5 (append-only audit table). The
PostgreSQL-level ``GRANT INSERT`` / ``REVOKE UPDATE, DELETE`` clauses
in the initial migration prevent the application role from issuing
UPDATE/DELETE against ``audit_events`` even if a future code path
attempted it. This module's "no UPDATE/DELETE" stance is the
application-side mirror of that database-level invariant - one of the
three defense-in-depth layers documented in
:mod:`app.models.audit_event`.

Performance budget: <= 100 ms per emission per AAP section 0.7.3,
easily achieved by a single INSERT against an indexed table. The
:data:`app.observability.metrics.audit_emit_duration_seconds`
Prometheus histogram is observed on both success and failure paths
so SREs can detect emit slowness or error spikes through the
CloudWatch dashboards.

Public API
----------

:class:`AuditEmissionError`
    AppError subclass raised on caller misuse - no active
    transaction, malformed argument types, or INSERT failure.
    Mapped to HTTP 500 with stable error code
    ``audit_emission_error`` by the registered Flask error handler
    in :mod:`app.middleware.error_handlers`.

:func:`emit_audit_event`
    The sole writer of ``audit_events``. Invoked by service modules
    inside their open transaction. Returns the persisted
    :class:`app.models.AuditEvent` so the caller can introspect the
    server-assigned ``id`` and ``event_timestamp`` if needed (the
    typical caller does not - the audit row is written for posterity
    rather than for in-process consumption).

Calling convention
------------------

The canonical usage from a service-layer caller (per AAP section
0.5.3 "service functions own transactions") is::

    with db.session() as session, session.begin():
        session.add(record)
        emit_audit_event(
            db_session=session,
            event_type=AuditEventType.CREATE,
            actor_user_id=actor.user_id,
            target_record_id=record.id,
            after_payload={"full_name": record.full_name, ...},
        )

The outer ``session.begin()`` is the caller's responsibility; the
emitter REJECTS calls made outside an active transaction by raising
:class:`AuditEmissionError`. If the INSERT of the audit row raises
(e.g., a constraint violation), the exception propagates out of the
emitter so the caller's ``with session.begin():`` block rolls back
the parent state change atomically.

Logging strategy
----------------

Audit emission is itself an operationally significant event. The
emitter logs at info level on success and at error level on failure
so that even without database access, an operator can reconstruct
audit history from CloudWatch JSON log lines. Each log line carries
structural identifiers (``event_type``, ``actor_user_id``,
``target_record_id``, ``audit_event_id``, ``elapsed_seconds``) plus
``error_class`` on the failure path. Per AAP section 0.7.4 security
invariant, the payload bodies (``before_payload``, ``after_payload``)
are NEVER logged because they may contain user-supplied free text
(e.g., the relationship_context field) that constitutes PII.
"""

from __future__ import annotations

# Standard library imports - alphabetized within sections per ruff
# isort with ``force-sort-within-sections=true``.
#
# ``time.perf_counter`` is the monotonic high-resolution clock used
# to measure elapsed seconds for the
# ``audit_emit_duration_seconds`` Prometheus histogram observation
# on both success and failure paths. Monotonicity matters: wall-clock
# adjustments (NTP slews, daylight savings transitions) would corrupt
# elapsed-time measurements taken with ``time.time()``.
#
# ``typing.TYPE_CHECKING`` gates the type-only import of
# ``sqlalchemy.orm.Session`` so the project's strict
# ``flake8-type-checking`` configuration is satisfied. ``typing.Any``
# annotates the JSONB payload columns ``before_payload`` and
# ``after_payload`` as ``dict[str, Any] | None`` because the JSONB
# column accepts arbitrary JSON-safe shapes that vary by event_type;
# a precise structural type cannot be enforced at this layer.
#
# ``uuid.UUID`` annotates the actor_user_id (required) and
# target_record_id (optional) kwargs of :func:`emit_audit_event`.
# Also referenced by :func:`_validate_arguments` in ``isinstance``
# checks that fail-fast on caller misuse before the SQLAlchemy
# session is touched. ``UUID`` is consumed at runtime (by
# ``isinstance``) so it CANNOT live inside the ``TYPE_CHECKING``
# block - it must be imported unconditionally.
import time
from typing import TYPE_CHECKING, Any
from uuid import UUID

# Third-party runtime imports.
#
# ``structlog.get_logger(__name__)`` produces a JSON-emitting bound
# logger. The ``merge_contextvars`` processor configured in
# :mod:`app.observability.logging` automatically surfaces the
# request-scoped ``correlation_id``, ``user_id``, and ``org_id``
# bound by the correlation/auth middleware, so log lines emitted
# here are automatically correlated by request without any per-call
# bookkeeping.
import structlog

# First-party imports - absolute paths only per the project's
# ``flake8-tidy-imports`` configuration (relative imports are banned
# under AAP section 0.3.7).
#
# ``AppError`` is the base class extended by
# :class:`AuditEmissionError`; the subclass relationship makes the
# emitter's misuse errors integrate with the project's uniform JSON
# error envelope handler chain (mapped to HTTP 500 with code
# ``audit_emission_error``) without requiring a duplicate exception
# hierarchy in services.
#
# ``AuditEvent`` is the SQLAlchemy declarative model for the
# append-only ``audit_events`` table; the emitter instantiates it
# with the caller-supplied attributes, adds it to the caller's open
# session, and flushes inside the parent transaction.
#
# ``AuditEventType`` is the Python enum mirroring the PostgreSQL
# ``audit_event_type`` enum with the eight tracked event types. The
# emitter uses it for argument validation, as the typed signature for
# the ``event_type`` kwarg, and for the metrics histogram label and
# structured log field ``event_type.value``.
#
# ``audit_emit_duration_seconds`` is the Prometheus Histogram
# instrument with the ``event_type`` label and audit-specific buckets
# (1ms..1s, with budget edge at 100ms). The emitter calls
# ``audit_emit_duration_seconds.labels(event_type=...).observe(...)``
# on BOTH success and failure paths.
from app.middleware.error_handlers import AppError
from app.models import AuditEvent
from app.models.enums import AuditEventType
from app.observability.metrics import audit_emit_duration_seconds

# Type-only imports. Under ``from __future__ import annotations`` all
# annotations are strings (PEP 563) and the symbols inside the
# ``TYPE_CHECKING`` block are never evaluated at runtime, satisfying
# the project's strict ``flake8-type-checking`` configuration.
#
# ``sqlalchemy.orm.Session`` (imported as ``DBSession`` alias) is the
# typed parameter for the caller's open session passed to
# :func:`emit_audit_event`. The function relies on three of its
# methods at runtime, but those calls do not need the type to be
# importable at runtime (Python is duck-typed; the parameter object
# itself carries the methods regardless of the annotation):
#   * ``session.in_transaction()`` - the transaction-active guard
#     that enforces AAP section 0.7.1 invariant 6.
#   * ``session.add(audit)`` - enrolls the AuditEvent in the caller's
#     transaction.
#   * ``session.flush()`` - synchronously issues the INSERT and
#     populates the audit row's database-assigned ``id`` and
#     ``event_timestamp`` columns inside the parent transaction so
#     any constraint violation surfaces immediately for atomic
#     rollback.
if TYPE_CHECKING:
    from sqlalchemy.orm import Session as DBSession


# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------
# structlog's ``merge_contextvars`` processor (configured in
# :mod:`app.observability.logging`) automatically surfaces the
# request-scoped ``correlation_id``, ``user_id``, and ``org_id`` bound
# by the correlation/auth middleware so log lines emitted here are
# automatically correlated by request without any per-call work.
logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------
# Stable error code consumed by the SPA's typed ``ApiError`` dispatch
# and matched against by the registered Flask error handler chain.
# Adding a new code is a non-breaking change; renaming this constant is
# breaking and requires SPA coordination.
_ERROR_CODE_AUDIT_EMISSION: str = "audit_emission_error"

# HTTP status code returned for any caller misuse of the emitter.
# Audit-emission errors are always programmer errors (the calling
# service got the contract wrong, or the database itself is
# unreachable); they are NOT user-recoverable and therefore surface
# as 500 Internal Server Error per AAP section 0.5.2 Layer 0
# error-handler conventions.
_AUDIT_EMISSION_HTTP_STATUS: int = 500


# ---------------------------------------------------------------------------
# Module public API
# ---------------------------------------------------------------------------
# ``__all__`` is sorted alphabetically per ruff RUF022 (isort-style
# sorting). Private helpers (``_assert_in_transaction``,
# ``_validate_arguments``) are intentionally omitted from the public
# surface; they are implementation details of :func:`emit_audit_event`
# and not contracted to callers.
__all__ = [
    "AuditEmissionError",
    "emit_audit_event",
]


# ---------------------------------------------------------------------------
# Domain exception
# ---------------------------------------------------------------------------


class AuditEmissionError(AppError):
    """Raised when the audit emitter is called incorrectly.

    This exception class signals PROGRAMMER ERROR, not a user-recoverable
    condition. Three fault categories surface as :class:`AuditEmissionError`:

    1. The caller invoked :func:`emit_audit_event` outside an active
       database transaction. AAP section 0.7.1 invariant 6 (atomic
       state-change + audit pair) requires every emission to occur
       inside the caller's open transaction so the audit row commits
       (or rolls back) together with the state change.
    2. The caller supplied an argument of an invalid type
       (e.g., ``event_type`` not an :class:`AuditEventType` enum
       member, ``actor_user_id`` not a :class:`uuid.UUID`,
       ``before_payload``/``after_payload`` not a ``dict``).
    3. The :func:`sqlalchemy.orm.Session.flush` call raised an
       exception unrelated to a normal constraint violation (e.g.,
       the database is unreachable). The original exception is
       re-raised by :func:`emit_audit_event` after logging and
       observing the failure histogram; this subclass is reserved
       for programmer-error categories 1 and 2.

    Mapped to HTTP 500 with stable error code
    ``audit_emission_error`` by the registered Flask error handler
    chain (the generic ``_handle_app_error`` reads ``status_code``
    and ``error_code`` from the exception instance). The
    user-facing ``message`` is deliberately generic in the JSON
    envelope so internals are not leaked; the structured log line
    captures the full context for engineers.

    Inherited public attributes (set by :class:`AppError.__init__`):

    Attributes:
        message: User-facing message included in the JSON envelope.
            Defaults to ``"Audit emission failed."`` via
            :attr:`default_message` when the caller does not supply
            one.
        fields: Empty list - audit-emission errors do not carry
            field-level error detail (this is server-side
            programmer error, not request payload validation).
        status_code: HTTP status code (500). Set as a class attribute
            so the Flask error handler reads it off any
            :class:`AuditEmissionError` instance.
        error_code: Stable error code (``"audit_emission_error"``).
            Set as a class attribute for the same reason.
    """

    # Class-level overrides of the :class:`AppError` defaults. The
    # registered error handler in :mod:`app.middleware.error_handlers`
    # reads ``status_code`` and ``error_code`` directly off the
    # instance; assigning them as class attributes (rather than
    # per-instance overrides) keeps the constructor minimal and
    # mirrors the pattern used by :class:`AuthError`,
    # :class:`ForbiddenError`, etc.
    status_code: int = _AUDIT_EMISSION_HTTP_STATUS
    error_code: str = _ERROR_CODE_AUDIT_EMISSION

    @property
    def default_message(self) -> str:
        """Return the default user-facing message for this exception class.

        Used by :class:`AppError.__init__` when the caller does not
        supply a ``message`` argument. Kept generic so internals are
        not leaked to the client; the structured log line captures
        the diagnostic detail.
        """
        return "Audit emission failed."


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _assert_in_transaction(db_session: DBSession) -> None:
    """Raise :class:`AuditEmissionError` unless the session has an active transaction.

    Enforces AAP section 0.7.1 invariant 6 - every audit emission must
    occur inside the caller's open transaction so the state change and
    the audit row commit atomically.

    SQLAlchemy 2.0 considers a session "in_transaction" if either an
    explicit ``session.begin()`` is active or autobegin has fired due
    to a pending change. The convention in this codebase is
    ``with session.begin():`` - explicit always - and that pattern is
    what callers typically use; this guard catches the misuse case
    where the caller forgot to open a transaction at all.

    Args:
        db_session: The caller's SQLAlchemy session to inspect.

    Raises:
        AuditEmissionError: ``db_session.in_transaction()`` returned
            False. The structured log emits an
            ``audit_emit_outside_transaction`` event before the raise
            so SREs can investigate misconfigured callers without
            requiring the database trace.
    """
    if not db_session.in_transaction():
        # The structured log captures the misuse so SREs can trace
        # which calling service forgot to open a transaction. The
        # ``correlation_id`` / ``user_id`` / ``org_id`` fields are
        # surfaced automatically by the structlog ``merge_contextvars``
        # processor configured in
        # :mod:`app.observability.logging`.
        logger.error("audit_emit_outside_transaction")
        raise AuditEmissionError(
            message=(
                "emit_audit_event must be invoked inside an active database "
                "transaction (use 'with session.begin():' in the caller)."
            )
        )


def _validate_arguments(
    *,
    event_type: AuditEventType,
    actor_user_id: UUID,
    target_record_id: UUID | None,
    before_payload: dict[str, Any] | None,
    after_payload: dict[str, Any] | None,
) -> None:
    """Validate the inputs to :func:`emit_audit_event`.

    Catches programmer mistakes early, BEFORE the SQLAlchemy session
    is touched, so the failure surfaces with a clear diagnostic
    message rather than as a downstream type or constraint error.
    Note that callers in this codebase are trusted (other service
    modules), so this is a "fast-fail on misuse" check, not a
    security boundary.

    Args:
        event_type: Must be an :class:`AuditEventType` enum member.
        actor_user_id: Must be a :class:`uuid.UUID`.
        target_record_id: Must be a :class:`uuid.UUID` or ``None``.
            ``None`` is the correct value for non-record events
            (``role_change``, ``authentication``) and for
            ``hard_delete`` (where the record ceases to exist after
            the transaction commits).
        before_payload: Must be a ``dict`` or ``None``.
        after_payload: Must be a ``dict`` or ``None``.

    Raises:
        AuditEmissionError: Any argument is of an invalid type. The
            ``message`` describes which argument failed validation
            so the engineer reading logs can correct the call site
            quickly.
    """
    # Reject non-AuditEventType event_type values via isinstance check.
    # Coercion from a bare string would silently accept a typo
    # ("creat" instead of "create") and produce an audit row with the
    # wrong event_type, which would corrupt the audit history. We
    # require the typed enum member at the call site.
    if not isinstance(event_type, AuditEventType):
        raise AuditEmissionError(message="event_type must be an AuditEventType enum member.")

    # actor_user_id is REQUIRED on every audit row. Even
    # ``authentication`` events have a known actor (the user logging
    # in or out). The non-null FK constraint at the database layer
    # would otherwise surface as a constraint error, but rejecting
    # here gives a faster, more diagnostic-friendly failure mode.
    if not isinstance(actor_user_id, UUID):
        raise AuditEmissionError(message="actor_user_id must be a UUID.")

    # target_record_id is optional. Passing a non-UUID and non-None
    # value (e.g., a bare string id) is a programmer mistake; the FK
    # constraint would catch it at flush time, but rejecting here is
    # cheaper and more diagnostic.
    if target_record_id is not None and not isinstance(target_record_id, UUID):
        raise AuditEmissionError(message="target_record_id must be a UUID or None.")

    # before_payload and after_payload are JSONB columns; SQLAlchemy
    # accepts any JSON-serializable Python value, but the
    # AAP-documented contract is "dict or None". Tightening the
    # accepted shape to ``dict`` (rejecting bare lists and primitives)
    # matches the documented usage in service code and makes the
    # log/forensic queries deterministic.
    if before_payload is not None and not isinstance(before_payload, dict):
        raise AuditEmissionError(message="before_payload must be a dict or None.")

    if after_payload is not None and not isinstance(after_payload, dict):
        raise AuditEmissionError(message="after_payload must be a dict or None.")


# ---------------------------------------------------------------------------
# Public function: emit_audit_event
# ---------------------------------------------------------------------------


def emit_audit_event(
    *,
    db_session: DBSession,
    event_type: AuditEventType,
    actor_user_id: UUID,
    target_record_id: UUID | None = None,
    before_payload: dict[str, Any] | None = None,
    after_payload: dict[str, Any] | None = None,
) -> AuditEvent:
    """Emit an audit event inside the caller's open transaction.

    Per AAP section 0.7.1 invariant 6, this function MUST be called
    inside a ``with db_session.begin():`` block opened by the caller.
    The audit row is added to that transaction so it commits or rolls
    back together with the state change.

    The audit table is append-only at the database privilege layer
    (per AAP section 0.7.4); this function only ever issues an INSERT
    - no UPDATE, no DELETE.

    Performance budget: <= 100 ms per emission per AAP section 0.7.3,
    enforced by the
    :data:`app.observability.metrics.audit_emit_duration_seconds`
    Prometheus histogram observation.

    Logging strategy: emits an ``audit_emit_success`` info log on the
    happy path and an ``audit_emit_failed`` error log on the flush
    failure path. Each log line carries structural identifiers
    (``event_type``, ``actor_user_id``, ``target_record_id``,
    ``audit_event_id``, ``elapsed_seconds``) but NEVER the payload
    contents - those may contain user-supplied PII per AAP section
    0.7.4 security invariant.

    Args:
        db_session: The caller's open SQLAlchemy session with an
            active transaction.
        event_type: One of the eight :class:`AuditEventType` enum
            values (``CREATE``, ``STATUS_CHANGE``, ``EDIT``,
            ``SOFT_DELETE``, ``HARD_DELETE``, ``ROLE_CHANGE``,
            ``AUTHENTICATION``, ``ADMIN_OP``).
        actor_user_id: The user id whose action produced this event.
            ALWAYS sourced from ``actor.user_id`` (i.e., the JWT
            session) per AAP section 0.7.4 owner-attribution
            invariant - never from client-supplied input.
        target_record_id: The record id this event affects, or
            ``None`` for non-record events. ``None`` is correct for
            ``ROLE_CHANGE`` (targets a user, captured in the
            payload), ``AUTHENTICATION`` (the audit row is keyed to
            the user not to a record), and ``HARD_DELETE`` (the
            record ceases to exist after the transaction commits, so
            populating the FK would conflict with the post-commit
            row state). Defaults to ``None``.
        before_payload: Optional JSON-safe ``dict`` describing the
            entity's state BEFORE the change. ``None`` is correct
            for ``CREATE`` events (no prior state) and for events
            where the prior state is not meaningful (e.g.,
            ``AUTHENTICATION``). Defaults to ``None``.
        after_payload: Optional JSON-safe ``dict`` describing the
            entity's state AFTER the change. ``None`` is correct for
            ``HARD_DELETE`` (the entity no longer exists). Defaults
            to ``None``.

    Returns:
        The persisted :class:`AuditEvent` entity (post-flush so
        ``id`` and ``event_timestamp`` are populated). The typical
        caller does not consume the return value; the audit row is
        written for posterity rather than for in-process consumption.

    Raises:
        AuditEmissionError: Caller misuse - no active transaction or
            malformed argument types. Mapped to HTTP 500 with code
            ``audit_emission_error`` by the registered Flask error
            handler.
        Exception: Any exception raised by
            :meth:`sqlalchemy.orm.Session.flush` (e.g.,
            :class:`sqlalchemy.exc.IntegrityError` from a forced FK
            violation, :class:`sqlalchemy.exc.OperationalError` from
            a database connectivity loss) is re-raised after logging
            and observing the failure histogram. The caller's
            ``with session.begin():`` block then rolls back the
            parent state change atomically per AAP section 0.7.1
            invariant 6.
    """
    # Argument validation first - cheap, fail-fast, no side effects.
    # Validating BEFORE the transaction guard means a malformed call
    # outside a transaction surfaces the argument issue (which is the
    # actionable problem) rather than the missing-transaction issue
    # (which is a downstream symptom).
    _validate_arguments(
        event_type=event_type,
        actor_user_id=actor_user_id,
        target_record_id=target_record_id,
        before_payload=before_payload,
        after_payload=after_payload,
    )

    # Transaction guard - enforces AAP section 0.7.1 invariant 6.
    # Must run AFTER argument validation so the more actionable
    # error category (argument types) is reported first when both
    # categories of misuse occur simultaneously.
    _assert_in_transaction(db_session)

    # Start the perf-counter clock AFTER the cheap validation passes.
    # The histogram measures the cost of the actual INSERT-and-flush
    # path, not the pre-flight argument validation, so SREs reading
    # the dashboard see a number that reflects database health rather
    # than caller misuse rate.
    started = time.perf_counter()

    # Construct the AuditEvent ORM entity. The model declares the
    # following defaults that we do NOT override here:
    #   * ``id``               - UUID v4 generated client-side via
    #     ``default=uuid.uuid4`` in the model's mapped_column.
    #   * ``event_timestamp``  - server-side ``NOW()`` set by the
    #     PostgreSQL ``server_default=func.now()`` clause; populated
    #     into the Python instance by the flush below via SQLAlchemy's
    #     post-INSERT refresh.
    audit = AuditEvent(
        actor_user_id=actor_user_id,
        target_record_id=target_record_id,
        event_type=event_type,
        before_payload=before_payload,
        after_payload=after_payload,
    )

    # Enroll the AuditEvent in the caller's open transaction. This is
    # NOT a synchronous database write - SQLAlchemy adds the entity
    # to the session's pending state; the actual INSERT fires on the
    # next flush.
    db_session.add(audit)

    # ``flush()`` within the caller's transaction so that:
    # (1) ``audit.id`` and ``audit.event_timestamp`` are populated
    #     for the return value (SQLAlchemy issues a RETURNING clause
    #     and refreshes the instance attributes from the row state),
    #     and
    # (2) any database-level constraint violation surfaces here,
    #     INSIDE the caller's transaction, allowing the caller's
    #     ``with session.begin():`` block to roll back the parent
    #     state change atomically.
    #
    # We catch ``Exception`` (not ``BaseException``) so that
    # ``KeyboardInterrupt`` and ``SystemExit`` propagate without
    # observation - those are administrative signals, not flush
    # errors. Bare ``except`` would swallow them and is also banned
    # by ruff E722.
    try:
        db_session.flush()
    except Exception as exc:
        # Log the failure with full structural context, observe the
        # failure histogram, and re-raise so the caller's transaction
        # rolls back. The audit emitter MUST never silently swallow
        # errors - that would violate AAP section 0.7.1 invariant 6
        # (atomic state-change + audit pair).
        elapsed = time.perf_counter() - started
        logger.error(
            "audit_emit_failed",
            event_type=event_type.value,
            actor_user_id=str(actor_user_id),
            target_record_id=(str(target_record_id) if target_record_id is not None else None),
            elapsed_seconds=elapsed,
            error_class=type(exc).__name__,
        )
        # Histogram observation on the FAILURE path. SREs can detect
        # emit slowness or error spikes by correlating the
        # ``audit_emit_failed`` log volume with the histogram label
        # series in CloudWatch.
        audit_emit_duration_seconds.labels(event_type=event_type.value).observe(elapsed)
        # Re-raise the ORIGINAL exception so the caller's
        # ``with session.begin():`` block sees the same diagnostic
        # information SQLAlchemy raised. We do NOT wrap the
        # exception in ``AuditEmissionError`` because that would
        # obscure the actual failure mode (e.g., constraint
        # violations and connectivity errors are valuable diagnostic
        # signals for the caller's exception handler).
        raise

    # Happy path: observe the success histogram and emit an info-level
    # structured log line so the audit-emission rate is visible in
    # both the metrics and logs surfaces.
    elapsed = time.perf_counter() - started
    audit_emit_duration_seconds.labels(event_type=event_type.value).observe(elapsed)
    logger.info(
        "audit_emit_success",
        event_type=event_type.value,
        actor_user_id=str(actor_user_id),
        target_record_id=(str(target_record_id) if target_record_id is not None else None),
        # ``audit.id`` is populated by the flush above; safe to
        # serialize unconditionally.
        audit_event_id=str(audit.id),
        elapsed_seconds=elapsed,
    )

    # Return the persisted entity for the caller's reference. Most
    # callers ignore the return value; the F-011 detail-history
    # endpoint queries ``audit_events`` directly via SQLAlchemy
    # rather than caching this return value across the request
    # boundary.
    return audit
