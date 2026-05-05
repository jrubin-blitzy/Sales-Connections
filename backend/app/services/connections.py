"""Connection record business logic.

Implements F-001 (form-driven create), F-004 (feed/dashboard listing),
F-005 (outreach-status mutation), F-007 (edit + soft delete), and F-011
(detail view with edit history).

This module is the sole owner of the ``records`` and ``record_tags``
tables' state-changing logic. All callers go through the public
functions defined here. Each state-changing function:

1. Opens an explicit ``with session.begin():`` transaction.
2. Performs the database mutation.
3. Calls :func:`app.services.audit.emit_audit_event` inside the same
   transaction.
4. Returns a hydrated SQLAlchemy entity (or tuple thereof) to the
   caller.

Reads always default to ``WHERE org_id = actor.org_id AND deleted_at
IS NULL`` unless the caller explicitly opts out (admin-only paths via
``include_deleted=True``).

Public surface:

* :class:`ConnectionFilters` -- frozen dataclass carrying the seven
  optional filter parameters used by :func:`list_records` (F-004).
* :class:`DuplicateRecordError` -- raised when the unique partial
  index on ``normalized_linkedin_url`` fires either via the pre-check
  or via the database race-resolution path.
* :func:`create_record` -- F-001 form-driven create; emits
  ``AuditEventType.CREATE`` inside the parent transaction.
* :func:`get_record` -- F-011 single-record fetch; org-scoped and
  soft-delete-aware.
* :func:`list_records` -- F-004 feed query with seven filter
  dimensions, five sort dimensions, paginated.
* :func:`update_record` -- F-007 edit; RBAC-aware (Contributors may
  edit only their own records, Admins bypass); emits
  ``AuditEventType.EDIT``.
* :func:`update_status` -- F-005 outreach-status mutation; idempotent
  (no-op when current value matches); emits
  ``AuditEventType.STATUS_CHANGE`` only when the value actually
  changes.
* :func:`soft_delete_record` -- F-007 soft delete (sets
  ``deleted_at``); idempotent; emits ``AuditEventType.SOFT_DELETE``.
* :func:`get_record_history` -- F-011 audit-event feed for a record.

Per AAP Section 0.7.4 security invariants:

* Owner attribution is derived from ``actor.user_id`` server-side
  (never from the inbound payload).
* The session role is the authoritative gate on edit/delete
  ownership rules; this module enforces the rules even though the
  API layer's ``@requires_role`` decorator runs first.
* Free-text PII fields (relationship_context, ai_notes) are
  EXCLUDED from audit payloads per the AAP's PII guidance; the
  audit trail captures who-changed-what at the structural level
  (record id, business identifiers, status transitions) rather
  than the full prose body.

Per AAP Section 0.5.3 (uniform implementation rules):

* Service functions own transactions.
* Models hold no behavior beyond declarations.
* Schemas mirror Zod schemas in
  ``frontend/src/schemas/connection.ts`` field-for-field.
"""

# Imports rationale (consolidated to satisfy ruff's import-grouping
# and import-sorting checks).
#
# Standard library:
# * ``http`` provides the canonical HTTP status enum used by
#   :class:`DuplicateRecordError`'s ``status_code`` class
#   attribute.
# * ``logging`` is the stdlib logger used as the module-level
#   diagnostic emitter; the project's structlog processor chain
#   routes stdlib log records through structlog's JSON encoder.
# * ``time.perf_counter`` is the monotonic high-resolution clock
#   used to measure elapsed seconds for the structured log lines
#   emitted on the create/list paths.
# * ``collections.abc.Iterable`` / ``Sequence`` parameterize the
#   :func:`_resolve_org_tags` helper.
# * ``dataclasses.dataclass`` declares :class:`ConnectionFilters`
#   as a frozen, slot-backed value object.
# * ``datetime.UTC`` / ``datetime.datetime`` are used to set
#   ``updated_at`` / ``deleted_at`` deterministically (so
#   freezegun-based tests can pin the value).
# * ``typing.Any`` annotates the JSON-shaped audit-payload return
#   type and the type-elided ``Select[Any]`` annotations.
# * ``typing.TYPE_CHECKING`` gates the type-only imports of
#   pydantic schemas, the auth ``Session``, and the model enums
#   that are referenced only in dataclass annotations.
#
# Third-party runtime:
# * ``structlog.get_logger(__name__)`` produces a JSON-emitting
#   bound logger. The ``merge_contextvars`` processor surfaces
#   the request-scoped correlation/user/org bindings.
# * SQLAlchemy primitives ``select`` (build queries),
#   ``func.count()`` (pagination total), ``asc`` / ``desc``
#   (ordering directives), ``IntegrityError`` (translated to
#   :class:`DuplicateRecordError` on partial-unique-index fire),
#   ``joinedload`` (eager-load :attr:`AuditEvent.actor`), and
#   ``selectinload`` (eager-load
#   :attr:`Record.record_tags` -> :attr:`RecordTag.tag`).
#
# First-party:
# * ``db`` from :mod:`app.extensions` is the SQLAlchemy 2.x
#   extension singleton; public service functions wrap state
#   changes in ``with db.session() as session, session.begin():``
#   per AAP s 0.5.3 and s 0.7.1 invariant 6.
# * Error subclasses from :mod:`app.middleware.error_handlers`:
#   ``AppError`` is extended by :class:`DuplicateRecordError`;
#   ``ForbiddenError`` / ``NotFoundError`` /
#   ``ValidationFailedError`` are raised by service-layer
#   functions and mapped to JSON envelopes by the registered
#   Flask handlers.
# * SQLAlchemy declarative models from :mod:`app.models`.
# * ``AuditEventType`` and ``UserRole`` enums from
#   :mod:`app.models.enums`. (``InvolvementType`` and
#   ``OutreachStatus`` are TYPE_CHECKING-only because they appear
#   solely in dataclass annotations under PEP 563.)
# * ``emit_audit_event`` is the SOLE writer of ``audit_events``;
#   called inside the parent ``with session.begin():`` per AAP
#   s 0.7.1 invariant 6.
# * ``find_duplicate`` is reused from
#   :mod:`app.services.duplicate_detection` for the
#   in-transaction pre-check on :func:`create_record`.
# * ``normalize_linkedin_url`` (F-010) canonicalizes URLs before
#   the unique partial index lookup.
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import http
import logging
import time
from typing import TYPE_CHECKING, Any

from sqlalchemy import asc, desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload, selectinload
import structlog

from app.extensions import db
from app.middleware.error_handlers import (
    AppError,
    ForbiddenError,
    NotFoundError,
    ValidationFailedError,
)
from app.models import (
    AuditEvent,
    Record,
    RecordTag,
    Tag,
    User,
)
from app.models.enums import (
    AuditEventType,
    UserRole,
)
from app.services.audit import emit_audit_event
from app.services.duplicate_detection import find_duplicate
from app.utils.url import normalize_linkedin_url

# Type-only imports. Under ``from __future__ import annotations`` all
# annotations are PEP 563 strings (never evaluated at runtime), so
# placing these imports inside a ``TYPE_CHECKING`` guard satisfies
# ruff's strict ``flake8-type-checking`` configuration without
# breaking the type annotations on the public function signatures.
#
# * ``date`` types the ``submission_date_from`` /
#   ``submission_date_to`` filter parameters on
#   :class:`ConnectionFilters`. ``datetime.datetime`` is imported
#   above (runtime) because :func:`datetime.now` is invoked.
# * ``UUID`` is the type annotation on every public function's
#   ``record_id`` parameter; never instantiated by this module.
# * ``Select`` provides the typed annotation for helper functions
#   that compose SQL fragments (e.g., :func:`_apply_org_scope`);
#   never invoked at runtime.
# * ``Session as DBSession`` (SQLAlchemy ORM) is the typed
#   parameter for private helpers receiving the open session from
#   ``db.session()``; never instantiated here.
# * ``InvolvementType`` / ``OutreachStatus`` appear in the
#   :class:`ConnectionFilters` dataclass annotations only.
# * ``Session`` (auth dataclass) is the typed parameter ``actor``
#   on every public function; never instantiated by this module
#   (the auth middleware is the sole producer).
# * ``ConnectionCreate`` / ``ConnectionStatusUpdate`` /
#   ``ConnectionUpdate`` are the pydantic DTOs forming the input
#   contract between the API blueprint and this service module.
if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from datetime import date
    from uuid import UUID

    from sqlalchemy import Select
    from sqlalchemy.orm import Session as DBSession

    from app.middleware.auth import Session
    from app.models.enums import InvolvementType, OutreachStatus
    from app.schemas.connection import (
        ConnectionCreate,
        ConnectionStatusUpdate,
        ConnectionUpdate,
    )


# ---------------------------------------------------------------------------
# Module loggers
# ---------------------------------------------------------------------------
# Two loggers are instantiated to mirror the dual-logging pattern
# established in :mod:`app.middleware.error_handlers` and
# :mod:`app.api.connections`:
# * The structlog logger emits structured JSON on the service-layer
#   lifecycle events (create/edit/status/delete/history). The
#   ``merge_contextvars`` processor configured in
#   :mod:`app.observability.logging` automatically surfaces the
#   request-scoped ``correlation_id``, ``user_id``, and ``org_id``.
# * The stdlib logger is used by performance-instrumented paths
#   that propagate ``extra={...}`` kwargs (the project's
#   ``add_stdlib_record_extras`` processor promotes those into the
#   emitted JSON body); kept here as a parallel emitter so log
#   forensics can correlate service-layer activity with the
#   structured audit-event log lines emitted by the audit emitter.
logger = structlog.get_logger(__name__)
_stdlib_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# Stable error code consumed by the SPA's typed ApiError dispatch.
# Centralized at module scope so the constant can be referenced from
# tests without re-stringifying the literal at every call site.
# Adding a new code is a non-breaking change; renaming this constant
# is breaking and requires SPA coordination per AAP s 0.7.5
# Explainability rule.
_ERROR_CODE_DUPLICATE_RECORD: str = "duplicate_record"

# Allowed sort fields per AAP s 0.5.2 Layer 4: five sort dimensions.
# The dictionary is keyed by the stable string name accepted on the
# wire (e.g., ``?sort=full_name``) and maps to the SQLAlchemy column
# expression used inside ``ORDER BY``. Adding a new sort dimension
# is a non-breaking change; renaming an existing key is breaking and
# requires SPA coordination.
_SORT_COLUMNS: dict[str, Any] = {
    "submission_date": Record.submission_date,
    "full_name": Record.full_name,
    "company": Record.company,
    "owner_display_name": Record.owner_display_name,
    "outreach_status": Record.outreach_status,
}
_DEFAULT_SORT_KEY: str = "submission_date"
_DEFAULT_SORT_DIR: str = "desc"
_ALLOWED_SORT_DIRECTIONS: frozenset[str] = frozenset({"asc", "desc"})

# Pagination guardrails. The feed defaults to a 25-record page; the
# hard ceiling of 100 records per page protects the database from
# pathological page-size requests that would otherwise blow past
# the AAP s 0.7.3 feed-load budget at the 10K-record scale ceiling.
# ``_MAX_PAGE_SIZE`` is intentionally lower than the schema-level
# ``_MAX_PAGE_SIZE`` in :mod:`app.schemas.connection` (200) so that
# the service layer is the more restrictive gate; the service
# value is the authoritative ceiling because the schema-level
# value is informational.
_DEFAULT_PAGE_SIZE: int = 25
_MAX_PAGE_SIZE: int = 100


# ---------------------------------------------------------------------------
# Module public API
# ---------------------------------------------------------------------------
# ``__all__`` is sorted alphabetically (RUF022 isort-style sorting).
# The categories - public dataclass, exception class, and the seven
# service-layer functions - are documented in the module docstring
# rather than as inline section comments here.
__all__ = [
    "ConnectionFilters",
    "DuplicateRecordError",
    "create_record",
    "get_record",
    "get_record_history",
    "list_records",
    "soft_delete_record",
    "update_record",
    "update_status",
]


# ---------------------------------------------------------------------------
# Exception classes
# ---------------------------------------------------------------------------


class DuplicateRecordError(AppError):
    """Raised when the unique partial index on ``normalized_linkedin_url`` fires.

    Two paths reach this exception:

    1. The best-effort pre-check via :func:`find_duplicate` returned
       a matching active record for the same ``(org_id,
       normalized_linkedin_url)`` pair. Raised BEFORE the INSERT to
       avoid a wasted round-trip.
    2. The pre-check returned ``None`` (or was skipped) but the
       database's unique partial index fired during INSERT,
       indicating a concurrent submit by another contributor. The
       :class:`IntegrityError` from ``session.flush()`` is caught
       and converted to this exception so the handler emits the
       same 409 envelope the SPA expects regardless of timing.

    Mapped to HTTP 409 with ``error.code = "duplicate_record"`` by
    the registered Flask error handler in
    :mod:`app.middleware.error_handlers` via the generic
    ``_handle_app_error`` handler (which reads ``status_code`` and
    ``error_code`` off the instance).

    Per AAP Section 0.7.6 (Business Rules), F-010 duplicate
    detection is a "warning, not a block" -- but that contract
    applies to the PRE-SUBMIT duplicate-check endpoint
    (``GET /api/connections/duplicate-check``) which always returns
    HTTP 200 with ``duplicate_found=true|false``. The CREATE flow,
    in contrast, MUST honour the unique constraint at the database
    level: two contributors cannot both win the race to log the
    same LinkedIn URL into the same org. The 409 surface lets the
    SPA re-fetch the now-existing record and surface a clear "Jane
    Doe just submitted this contact" message.
    """

    status_code: int = http.HTTPStatus.CONFLICT.value  # 409
    error_code: str = _ERROR_CODE_DUPLICATE_RECORD

    @property
    def default_message(self) -> str:
        """Return the default duplicate-record message."""
        return "A connection with this LinkedIn URL already exists in your organization."


# ---------------------------------------------------------------------------
# Public dataclass: ConnectionFilters
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConnectionFilters:
    """Filter parameters for :func:`list_records` (F-004).

    All fields are optional. Multi-valued fields (``involvement``,
    ``outreach_status``, ``tag_ids``, ``owner_user_ids``) accept
    tuples for "any-of" semantics and default to the empty tuple
    (which the helper :func:`_apply_filters` interprets as "no
    filter on this dimension").

    The dataclass is ``frozen=True`` so handlers cannot accidentally
    mutate filter parameters mid-request, and ``slots=True`` so the
    per-instance memory footprint stays minimal at the page-size
    scale.

    Attributes:
        company: Case-insensitive substring match on
            :attr:`Record.company`. ``None`` (default) means "no
            filter".
        involvement: "Any-of" filter on
            :attr:`Record.involvement`. Empty tuple (default) means
            "no filter".
        outreach_status: "Any-of" filter on
            :attr:`Record.outreach_status`. Empty tuple (default)
            means "no filter".
        owner_user_ids: "Any-of" filter on
            :attr:`Record.owner_user_id`. Empty tuple (default)
            means "no filter".
        tag_ids: "Any-of" filter via
            :attr:`RecordTag.tag_id`. Empty tuple (default) means
            "no filter".
        submission_date_from: Lower bound (inclusive) on
            :attr:`Record.submission_date`. ``None`` (default)
            means "no lower bound".
        submission_date_to: Upper bound (inclusive) on
            :attr:`Record.submission_date`. ``None`` (default)
            means "no upper bound".
        full_name_search: Case-insensitive substring match on
            :attr:`Record.full_name`. ``None`` (default) means "no
            filter".
        include_deleted: When ``True``, the soft-delete filter
            ``WHERE deleted_at IS NULL`` is OMITTED. Admin-only
            opt-out (F-007 admin moderation). Default ``False``
            preserves the AAP s 0.7.1 invariant 4 default-on
            soft-delete-aware-reads behavior.
    """

    company: str | None = None
    involvement: tuple[InvolvementType, ...] = ()
    outreach_status: tuple[OutreachStatus, ...] = ()
    owner_user_ids: tuple[UUID, ...] = ()
    tag_ids: tuple[UUID, ...] = ()
    submission_date_from: date | None = None
    submission_date_to: date | None = None
    full_name_search: str | None = None
    include_deleted: bool = False


# ---------------------------------------------------------------------------
# Public function: create_record
# ---------------------------------------------------------------------------


def create_record(payload: ConnectionCreate, actor: Session) -> Record:
    """Create a new Connection-Idea record (F-001).

    Workflow (single transaction):

    1. Normalize ``payload.linkedin_url`` via
       :func:`app.utils.url.normalize_linkedin_url`. A normalizer
       returning the empty-string sentinel is treated as a
       validation failure (HTTP 422); the schema validator should
       have rejected it first, but the explicit raise documents the
       invariant for future readers.
    2. Open ``with db.session() as session, session.begin():`` and:

        a. Lookup the actor's :class:`User` row to capture the
           ``owner_display_name`` snapshot (F-006 owner-attribution
           denormalization).
        b. Resolve and validate ``payload.tag_ids`` against the
           actor's org. Cross-org or unknown tag ids surface as
           HTTP 422 with field-scoped error metadata.
        c. Pre-check for an existing duplicate via
           :func:`find_duplicate`; raise
           :class:`DuplicateRecordError` BEFORE the INSERT if found.
        d. Construct and ``session.add(record)``.
        e. ``session.flush()`` to force the INSERT and assign the
           server-side defaults (``id``, ``submission_date``,
           ``created_at``, ``updated_at``) before chaining the
           record_tags INSERTs and the audit-event emit.
        f. For each resolved tag, ``session.add(RecordTag(...))``.
        g. :func:`emit_audit_event` with
           :class:`AuditEventType.CREATE` to write the audit row
           inside the same transaction (atomic state-change + audit
           pair per AAP s 0.7.1 invariant 6).

    3. Re-open a fresh session to eager-load the
       ``record_tags`` -> ``tag`` chain so the API handler can
       serialize the response without an N+1 query.

    Atomicity guarantee: the records INSERT, the record_tags
    INSERTs, AND the audit_events INSERT all commit together via
    the parent ``with session.begin():`` block. A failure at any
    step (e.g., the unique-index race firing during flush, an
    audit-emit error) cleanly rolls back the entire create.

    Multi-tenant scoping: every SQL predicate that touches user-
    visible data (tag lookup, duplicate lookup, owner lookup)
    injects ``org_id == actor.org_id`` so cross-org bleed is
    structurally prevented. The audit row is scoped via
    ``actor_user_id``, which inherently belongs to ``actor.org_id``
    via the ``users.org_id`` FK.

    Owner attribution: ``owner_user_id`` is derived from
    ``actor`` (per AAP s 0.7.4 security invariant); any
    client-supplied ``owner_*`` value would have been rejected by
    the pydantic schema's ``extra='forbid'`` config before reaching
    this function.

    Args:
        payload: Schema-validated :class:`ConnectionCreate` from
            the inbound JSON body. The schema has already enforced
            the nine-field shape, length caps, LinkedIn URL format,
            ``involvement`` enum membership, and rejection of any
            client-supplied owner / status overrides.
        actor: Authenticated :class:`Session` populated by
            :mod:`app.middleware.auth`. ``actor.user_id`` and
            ``actor.org_id`` are read for owner attribution and
            multi-tenant scoping.

    Returns:
        The persisted :class:`Record` with ``id``,
        ``submission_date``, ``created_at``, ``updated_at``
        populated by PostgreSQL defaults, and the ``record_tags``
        collection eagerly loaded so the API handler can serialize
        the response without N+1.

    Raises:
        DuplicateRecordError: A non-soft-deleted record with the
            same normalized LinkedIn URL already exists in the
            actor's org (HTTP 409,
            ``error.code = "duplicate_record"``).
        ValidationFailedError: One or more ``tag_ids`` reference
            tags that don't exist in the actor's org, or the
            normalized LinkedIn URL is the empty-string sentinel
            (HTTP 422, ``error.code = "validation_failed"``, with
            field-scoped details).
        ForbiddenError: The actor's User row is missing or belongs
            to a different organization (HTTP 403). Theoretically
            unreachable under a valid JWT but kept as defensive
            check.
    """
    started = time.perf_counter()

    actor_user_id = actor.user_id
    org_id = actor.org_id

    normalized_url = normalize_linkedin_url(payload.linkedin_url)
    if not normalized_url:
        # Defensive branch: the schema's LinkedIn URL validator
        # already rejects malformed URLs, so a normalizer that
        # returns the empty-string sentinel is theoretically
        # unreachable. The explicit raise documents the invariant
        # for future readers and is fail-safe in case the validator
        # is ever weakened.
        raise ValidationFailedError(
            message="The provided LinkedIn URL could not be normalized.",
            fields=[
                {
                    "field": "linkedin_url",
                    "code": "value_error.url",
                    "message": "Could not normalize LinkedIn URL.",
                }
            ],
        )

    # ----- Open the transaction --------------------------------------
    # The ``with db.session() as session:`` context manages the
    # connection lifetime; the inner ``session.begin()`` opens the
    # explicit transaction within which the audit row and the
    # state change must commit atomically per AAP Section 0.7.1
    # invariant 6.
    with db.session() as session, session.begin():
        # ----- Step 1: Resolve owner ----------------------------------
        # Org-scoped lookup: the User row MUST be in the actor's org
        # (by definition the actor's own org via the auth middleware,
        # but enforce explicitly so a future change to the auth
        # middleware doesn't silently change the security invariant).
        owner: User | None = session.execute(
            select(User).where(User.id == actor_user_id, User.org_id == org_id)
        ).scalar_one_or_none()
        if owner is None:
            # Theoretically unreachable (the auth middleware verifies
            # the User row via the token-version check) but kept as
            # explicit defense.
            raise ForbiddenError(
                message="The authenticated user no longer exists in this organization.",
            )
        owner_display_name = owner.display_name

        # ----- Step 2: Resolve and validate tags ----------------------
        # Skip the round-trip when the payload has zero tags.
        # Otherwise lookup ALL tag ids in a single SELECT and verify
        # each requested id was found AND belongs to the actor's org.
        # Cross-org or unknown tag ids surface as a structured 422.
        resolved_tags: list[Tag] = []
        if payload.tag_ids:
            resolved_tags = list(_resolve_org_tags(session, org_id, payload.tag_ids))

        # ----- Step 3: Pre-check for duplicate ------------------------
        # Best-effort lookup against the partial unique index. The
        # database is the authoritative race-resolver below; this
        # pre-check just avoids wasting a flush when the duplicate
        # is already visible.
        existing = find_duplicate(org_id, normalized_url, db_session=session)
        if existing is not None:
            raise DuplicateRecordError()

        # ----- Step 4: Construct and INSERT the record ---------------
        # ``id`` defaults via Python's ``uuid.uuid4`` factory.
        # ``submission_date``, ``created_at``, ``updated_at`` default
        # via PostgreSQL ``NOW()``. ``outreach_status`` defaults to
        # ``'Not Started'`` via the column's server_default (per
        # F-005 architectural invariant).
        record = Record(
            org_id=org_id,
            owner_user_id=actor_user_id,
            owner_display_name=owner_display_name,
            full_name=payload.full_name,
            linkedin_url=payload.linkedin_url,
            normalized_linkedin_url=normalized_url,
            company=payload.company,
            job_title=payload.job_title,
            relationship_context=payload.relationship_context,
            ai_notes=payload.ai_notes,
            involvement=payload.involvement,
        )
        session.add(record)

        # ``flush()`` forces the INSERT so we (a) discover any
        # constraint violations BEFORE chaining record_tags and
        # audit_events INSERTs that reference ``record.id``, and (b)
        # let PostgreSQL assign the server-default ``submission_date``
        # / ``created_at`` / ``updated_at`` values that the response
        # shape needs to surface.
        try:
            session.flush()
        except IntegrityError as exc:
            # The unique partial index fired -- a concurrent request
            # created a record with the same normalized URL between
            # our pre-check and our INSERT. Convert to the typed 409
            # so the SPA renders the duplicate-warning UI; the
            # ``with session.begin():`` block above will roll the
            # transaction back when the exception propagates out.
            raise DuplicateRecordError() from exc

        # ----- Step 5: Materialize tag associations -------------------
        # Build the M:N rows now that the parent ``records`` row
        # exists with its server-assigned id. The composite primary
        # key on ``record_tags`` (record_id, tag_id) deduplicates
        # naturally; we deduplicated client-side via the helper to
        # avoid the IntegrityError on payload-side repetition.
        for tag in resolved_tags:
            session.add(RecordTag(record_id=record.id, tag_id=tag.id))

        # Flush again so the record_tags INSERTs land before the
        # audit row. Not strictly required (the entire transaction
        # commits atomically) but keeps the database state visible
        # to the audit emitter's session-scoped queries.
        if resolved_tags:
            session.flush()

        # ----- Step 6: Emit audit event ------------------------------
        # ``after_payload`` snapshots the structural identifiers via
        # :func:`_record_to_audit_payload`. Free-text PII fields
        # (relationship_context, ai_notes) are deliberately omitted
        # by the helper per AAP Section 0.7.4.
        after_payload = _record_to_audit_payload(record, tag_ids=[t.id for t in resolved_tags])
        emit_audit_event(
            db_session=session,
            event_type=AuditEventType.CREATE,
            actor_user_id=actor_user_id,
            target_record_id=record.id,
            before_payload=None,
            after_payload=after_payload,
        )

    # ----- Step 7: Refresh for response serialization ----------------
    # The ``with session.begin():`` block above committed and the
    # session was closed by the outer ``with db.session()``. We open
    # a fresh session purely to eager-load the ``record_tags`` ->
    # ``tag`` chain so the API handler can serialize the
    # ``ConnectionRead`` response without an N+1 query. Using a
    # fresh session keeps this read out of the audit-bound
    # transaction's lifecycle (the transaction has already
    # committed; this is a read-only round-trip).
    with db.session() as session:
        record_with_tags = session.execute(
            select(Record)
            .options(selectinload(Record.record_tags).selectinload(RecordTag.tag))
            .where(Record.id == record.id, Record.org_id == org_id)
        ).scalar_one()
        # Detach so the caller can access attributes after the
        # session is closed without triggering a DetachedInstanceError
        # on lazy-load of any column not already populated.
        session.expunge_all()

    elapsed = time.perf_counter() - started
    _stdlib_logger.info(
        "connection_record_created",
        extra={
            "record_id": str(record_with_tags.id),
            "org_id": str(org_id),
            "actor_user_id": str(actor_user_id),
            "tag_count": len(resolved_tags),
            "involvement": record_with_tags.involvement.value,
            "elapsed_seconds": round(elapsed, 4),
        },
    )

    return record_with_tags


# ---------------------------------------------------------------------------
# Public function: get_record
# ---------------------------------------------------------------------------


def get_record(record_id: UUID, actor: Session, *, include_deleted: bool = False) -> Record:
    """Fetch a single connection record (F-011).

    Org-scoped, soft-delete-aware. Raises :class:`NotFoundError` on
    miss to avoid leaking the existence of records in other
    organizations (cross-org enumeration defense).

    Args:
        record_id: UUID of the record to fetch.
        actor: Authenticated :class:`Session`. ``actor.org_id`` is
            injected into the SQL WHERE clause.
        include_deleted: When ``True``, soft-deleted records are
            returned. Admin-only opt-out (F-007 admin moderation).
            Default ``False`` preserves the AAP s 0.7.1 invariant 4
            default-on soft-delete-aware-reads behavior.

    Returns:
        The :class:`Record` with eagerly-loaded ``record_tags`` ->
        ``tag`` chain so the API handler can serialize the response
        without an N+1 query. The instance is expunged from the
        session before return so attribute access does not trigger
        DetachedInstanceError on the caller side.

    Raises:
        NotFoundError: No record with the given id exists in the
            actor's org (or the record is soft-deleted and
            ``include_deleted`` is ``False``).
    """
    with db.session() as session:
        record = _fetch_record_with_relations(
            session, record_id, actor, include_deleted=include_deleted
        )
        if record is None:
            raise NotFoundError(message="Connection record not found.")
        # Detach so attribute access after session close is safe.
        session.expunge_all()
    return record


# ---------------------------------------------------------------------------
# Public function: list_records
# ---------------------------------------------------------------------------


def list_records(
    filters: ConnectionFilters,
    actor: Session,
    *,
    sort_key: str = _DEFAULT_SORT_KEY,
    sort_dir: str = _DEFAULT_SORT_DIR,
    limit: int = _DEFAULT_PAGE_SIZE,
    offset: int = 0,
) -> tuple[list[Record], int]:
    """List connection records (F-004) with filter, sort, and pagination.

    Uses the composite index ``ix_records_org_deleted_submission``
    for the default sort (``submission_date DESC``); other sorts
    may scan more rows but remain within the AAP s 0.7.3 feed-load
    budget at the 10K-record scale ceiling thanks to the
    single-column indexes on ``involvement`` and
    ``outreach_status``.

    Stable sort tiebreaker: every ``ORDER BY`` clause appends
    ``Record.id ASC`` so paginated results are deterministic across
    requests even when the primary sort key has duplicate values.
    Without the tiebreaker, two records with identical
    ``submission_date`` could swap positions across page boundaries.

    Pagination guardrails: ``limit`` is silently clamped to
    ``[1, _MAX_PAGE_SIZE]`` and ``offset`` is clamped to ``>= 0``
    so a malicious client cannot request a 1,000,000-record page.

    Args:
        filters: :class:`ConnectionFilters` carrying the seven
            optional filter parameters. ``include_deleted=True``
            opts out of the default soft-delete filter
            (admin-only).
        actor: Authenticated :class:`Session`. ``actor.org_id`` is
            injected into the WHERE clause.
        sort_key: One of the five allowed sort dimensions (see
            :data:`_SORT_COLUMNS`). Unknown keys raise
            :class:`ValidationFailedError` (HTTP 422).
        sort_dir: ``"asc"`` or ``"desc"``. Other values silently
            fall back to the default direction (``"desc"``).
        limit: Maximum rows per page. Clamped to
            ``[1, _MAX_PAGE_SIZE]``.
        offset: Zero-indexed offset into the result set. Clamped
            to ``>= 0``.

    Returns:
        A 2-tuple of ``(records, total)``:

        * ``records``: List of :class:`Record` instances (may be
          empty) with eager-loaded ``record_tags`` -> ``tag`` chain.
        * ``total``: Total count of records matching the filters
          (used by the SPA for pagination labels).

    Raises:
        ValidationFailedError: Unknown ``sort_key`` (HTTP 422).
    """
    started = time.perf_counter()

    # Pagination guardrails: silently clamp to defensible ranges.
    safe_limit = max(1, min(int(limit), _MAX_PAGE_SIZE))
    safe_offset = max(0, int(offset))

    # Validate sort_key. Unknown keys are explicit programmer or
    # client errors that must surface as a 422 rather than silently
    # falling back to a default.
    if sort_key not in _SORT_COLUMNS:
        raise ValidationFailedError(
            message=f"Unknown sort key '{sort_key}'.",
            fields=[
                {
                    "field": "sort",
                    "code": "value_error.invalid_sort",
                    "message": (f"sort must be one of: {sorted(_SORT_COLUMNS.keys())}"),
                }
            ],
        )

    # Sort direction silently falls back to the default for unknown
    # values (less defensive than sort_key because direction is
    # commonly mis-cased, e.g., "DESC" vs "desc").
    if sort_dir not in _ALLOWED_SORT_DIRECTIONS:
        sort_dir = _DEFAULT_SORT_DIR

    sort_column = _SORT_COLUMNS[sort_key]
    direction = desc if sort_dir == "desc" else asc

    # Build the base query with org-scope and soft-delete-scope
    # applied uniformly via the private helpers. Filters are layered
    # on top via :func:`_apply_filters`.
    base = select(Record)
    base = _apply_org_scope(base, actor)
    base = _apply_soft_delete_scope(base, include_deleted=filters.include_deleted)
    base = _apply_filters(base, filters)

    # Count: total rows matching the filter (used for pagination
    # metadata). The subquery wrapper preserves the WHERE/JOIN
    # semantics from the base query while reducing the projection
    # to a single COUNT(*) so the database does not materialize
    # the per-row payload.
    count_stmt = select(func.count()).select_from(base.subquery())

    # Eager-load tags via selectinload (a separate IN-clause query
    # rather than a JOIN). At page-size scale this avoids the
    # Cartesian explosion that joinedload would cause when a record
    # has multiple tags.
    page_stmt = (
        base.options(selectinload(Record.record_tags).selectinload(RecordTag.tag))
        .order_by(direction(sort_column), Record.id.asc())
        .limit(safe_limit)
        .offset(safe_offset)
    )

    with db.session() as session:
        total = int(session.execute(count_stmt).scalar_one())
        rows = list(session.execute(page_stmt).scalars().all())
        # Detach so attribute access after session close is safe.
        session.expunge_all()

    elapsed = time.perf_counter() - started
    _stdlib_logger.info(
        "connection_records_listed",
        extra={
            "org_id": str(actor.org_id),
            "actor_user_id": str(actor.user_id),
            "filter_count": _count_active_filters(filters),
            "sort_key": sort_key,
            "sort_dir": sort_dir,
            "limit": safe_limit,
            "offset": safe_offset,
            "returned_count": len(rows),
            "total_count": total,
            "elapsed_seconds": round(elapsed, 4),
        },
    )

    return rows, total


# ---------------------------------------------------------------------------
# Public function: update_record
# ---------------------------------------------------------------------------


def update_record(
    record_id: UUID,
    payload: ConnectionUpdate,
    actor: Session,
) -> Record:
    """Edit an existing record (F-007).

    RBAC: ``Contributor`` may edit only their own records;
    ``Admin`` may edit any record in their org. The API-layer
    ``@requires_role`` decorator gates role membership; this
    function additionally checks ownership for Contributors.

    The ``outreach_status`` field is intentionally NOT mutable
    here; status changes flow through :func:`update_status`
    (F-005) which is RBAC-gated separately to Sales Rep and Admin
    only. The :class:`ConnectionUpdate` schema deliberately
    excludes ``outreach_status`` so a request body containing it
    is rejected by ``extra='forbid'`` at the API boundary.

    PATCH semantics for ``tag_ids``:

    * ``None`` (key absent from payload): tags are NOT modified.
    * ``[]`` (empty list): all existing tag associations are
      removed.
    * ``[uuid1, uuid2, ...]``: associations are replaced with the
      given set; cross-org or unknown tag ids surface as HTTP 422.

    Atomic guarantee: the records UPDATE, the record_tags
    INSERT/DELETE diffs, AND the audit_events INSERT are persisted
    in the same transaction. If any step fails, the entire edit
    rolls back.

    Args:
        record_id: UUID of the record to edit.
        payload: Schema-validated :class:`ConnectionUpdate` from
            the inbound JSON body.
        actor: Authenticated :class:`Session`. ``actor.role`` is
            checked against the Contributor-may-only-edit-own
            rule; ``actor.org_id`` is injected into the WHERE
            clause.

    Returns:
        The updated :class:`Record` with eagerly-loaded
        ``record_tags`` -> ``tag`` chain.

    Raises:
        NotFoundError: Record not found in the actor's org (HTTP
            404; cross-org enumeration defense).
        ForbiddenError: Contributor attempting to edit another
            user's record (HTTP 403).
        ValidationFailedError: Invalid LinkedIn URL or unknown
            tag id (HTTP 422).
        DuplicateRecordError: The new normalized LinkedIn URL
            collides with another active record in the same org
            (HTTP 409).
    """
    with db.session() as session, session.begin():
        record = _fetch_record_for_update(session, record_id, actor)
        if record is None:
            raise NotFoundError(message="Connection record not found.")

        # Ownership check for Contributors. Admins bypass; Viewers
        # are denied by the API-layer RBAC decorator and never
        # reach here, but if a future routing change permits them,
        # this branch denies them defensively.
        if actor.role != UserRole.ADMIN and record.owner_user_id != actor.user_id:
            raise ForbiddenError(
                message="Only the record owner or an Admin may edit this record.",
            )

        # Capture the before-state for the audit row. Eagerly load
        # the record_tags relationship via the same session so the
        # tag_ids snapshot is accurate.
        before_tag_ids = _load_record_tag_ids(session, record.id)
        before_payload = _record_to_audit_payload(record, tag_ids=before_tag_ids)

        update_data = payload.model_dump(exclude_unset=True)
        normalized: str | None = None
        if "linkedin_url" in update_data:
            new_url = update_data["linkedin_url"]
            if new_url is not None:
                normalized = normalize_linkedin_url(new_url)
                if not normalized:
                    raise ValidationFailedError(
                        message="The provided LinkedIn URL could not be normalized.",
                        fields=[
                            {
                                "field": "linkedin_url",
                                "code": "value_error.url",
                                "message": "Could not normalize LinkedIn URL.",
                            }
                        ],
                    )

        # Apply scalar updates. Skip ``tag_ids`` (handled below)
        # and skip the no-op normalized_url assignment when
        # linkedin_url is unset.
        for attr, value in update_data.items():
            if attr == "tag_ids":
                continue
            if value is None:
                # ``None`` in PATCH-semantics for an optional field
                # means "do not change" rather than "set to NULL".
                # ai_notes is the one nullable column where the
                # client may legitimately want to clear the value;
                # the schema's empty-string-to-None validator ALSO
                # produces None there, but in BOTH cases ``None``
                # at this layer means "no-op" because the schema
                # rejects null-clearing of required fields and the
                # ai_notes-clear flow goes through an explicit
                # empty-string post-strip path.
                continue
            setattr(record, attr, value)
        if "linkedin_url" in update_data and normalized is not None:
            record.normalized_linkedin_url = normalized
        record.updated_at = datetime.now(tz=UTC)

        # Update tags if provided. ``tag_ids`` may legitimately be
        # an empty list (meaning "remove all tags"); we therefore
        # check for KEY presence rather than truthiness.
        if "tag_ids" in update_data and update_data["tag_ids"] is not None:
            new_tag_ids = tuple(update_data["tag_ids"])
            _replace_record_tags(session, record, new_tag_ids, actor.org_id)

        try:
            session.flush()
        except IntegrityError as exc:
            # The unique partial index fired -- the new normalized
            # URL collides with another active record in the same
            # org. Convert to the typed 409 so the SPA renders the
            # duplicate-warning UI.
            raise DuplicateRecordError() from exc

        # Capture the after-state for the audit row.
        after_tag_ids = _load_record_tag_ids(session, record.id)
        after_payload = _record_to_audit_payload(record, tag_ids=after_tag_ids)
        emit_audit_event(
            db_session=session,
            event_type=AuditEventType.EDIT,
            actor_user_id=actor.user_id,
            target_record_id=record.id,
            before_payload=before_payload,
            after_payload=after_payload,
        )

    # Open a fresh session to load the updated record with
    # eager-loaded relationships for response serialization.
    with db.session() as session:
        record_with_relations = _fetch_record_with_relations(session, record_id, actor)
        if record_with_relations is None:
            # Theoretically unreachable because we just edited the
            # record successfully; defensive fallback raises 404
            # rather than returning None.
            raise NotFoundError(message="Connection record not found after update.")
        session.expunge_all()

    return record_with_relations


# ---------------------------------------------------------------------------
# Public function: update_status
# ---------------------------------------------------------------------------


def update_status(
    record_id: UUID,
    payload: ConnectionStatusUpdate,
    actor: Session,
) -> Record:
    """Mutate the outreach status of a connection record (F-005).

    RBAC: enforced by the API decorator
    ``@requires_role(UserRole.VIEWER, UserRole.ADMIN)``; this
    function does not re-check the role but does enforce
    org-scope.

    Idempotency: setting ``outreach_status`` to its current value
    is a no-op (no audit event emitted, no row updated). This
    prevents the audit log from being flooded with redundant
    entries when the SPA polls or retries.

    Atomic guarantee: the records UPDATE and the audit_events
    INSERT commit in the same transaction.

    Args:
        record_id: UUID of the record whose status is being
            mutated.
        payload: Schema-validated :class:`ConnectionStatusUpdate`
            carrying the new ``outreach_status`` value.
        actor: Authenticated :class:`Session`. ``actor.org_id``
            is injected into the WHERE clause.

    Returns:
        The updated :class:`Record` with eagerly-loaded
        ``record_tags`` -> ``tag`` chain.

    Raises:
        NotFoundError: Record not found in the actor's org (HTTP
            404; cross-org enumeration defense).
    """
    with db.session() as session, session.begin():
        record = _fetch_record_for_update(session, record_id, actor)
        if record is None:
            raise NotFoundError(message="Connection record not found.")

        new_status = payload.outreach_status
        # Idempotent no-op when the status is unchanged. We still
        # commit the (empty) transaction so the connection is
        # released cleanly; the absence of an audit event is the
        # signal that the call was a no-op.
        if record.outreach_status != new_status:
            before_payload: dict[str, Any] = {
                "outreach_status": record.outreach_status.value,
            }
            record.outreach_status = new_status
            record.updated_at = datetime.now(tz=UTC)
            session.flush()
            after_payload: dict[str, Any] = {
                "outreach_status": new_status.value,
            }
            emit_audit_event(
                db_session=session,
                event_type=AuditEventType.STATUS_CHANGE,
                actor_user_id=actor.user_id,
                target_record_id=record.id,
                before_payload=before_payload,
                after_payload=after_payload,
            )

    # Open a fresh session to load the (possibly updated) record
    # with eager-loaded relationships for response serialization.
    with db.session() as session:
        record_with_relations = _fetch_record_with_relations(session, record_id, actor)
        if record_with_relations is None:
            raise NotFoundError(message="Connection record not found after status update.")
        session.expunge_all()

    return record_with_relations


# ---------------------------------------------------------------------------
# Public function: soft_delete_record
# ---------------------------------------------------------------------------


def soft_delete_record(record_id: UUID, actor: Session) -> Record:
    """Soft-delete a connection record (F-007).

    Sets ``deleted_at = NOW()``. The composite partial unique
    index ``(org_id, normalized_linkedin_url) WHERE deleted_at IS
    NULL`` allows a future record with the same LinkedIn URL to be
    created (the soft-delete-then-recreate flow).

    RBAC: only the record's owner or an Admin may soft-delete.
    Contributors may soft-delete their own records; Viewers may
    not soft-delete (the API-layer RBAC decorator gates the route
    and this function further checks ownership).

    Idempotency: a second soft-delete on the same record is a
    no-op (no audit event emitted, ``deleted_at`` not advanced).
    The frontend may show a "Delete" button on a stale record
    list; receiving a second delete request without changing
    state and without emitting a duplicate audit event preserves
    data integrity and a clean audit log.

    Atomic guarantee: the records UPDATE and the audit_events
    INSERT commit in the same transaction.

    Args:
        record_id: UUID of the record to soft-delete.
        actor: Authenticated :class:`Session`. ``actor.role`` is
            compared against the owner-only rule; ``actor.org_id``
            is injected into the WHERE clause.

    Returns:
        The soft-deleted :class:`Record` (with ``deleted_at``
        populated) and eagerly-loaded ``record_tags`` -> ``tag``
        chain. Returned even on the idempotent no-op path so the
        caller can surface a uniform response shape.

    Raises:
        NotFoundError: Record not found in the actor's org (HTTP
            404; cross-org enumeration defense).
        ForbiddenError: Non-owner non-Admin attempting to delete
            (HTTP 403).
    """
    with db.session() as session, session.begin():
        # Include soft-deleted rows so the idempotent path can
        # observe the existing deleted_at and short-circuit.
        record = _fetch_record_for_update(session, record_id, actor, include_deleted=True)
        if record is None:
            raise NotFoundError(message="Connection record not found.")

        if record.deleted_at is None:
            # Active record: enforce ownership rules before
            # soft-deleting. Admins bypass; Contributors and
            # Viewers may only soft-delete records they own.
            if actor.role != UserRole.ADMIN and record.owner_user_id != actor.user_id:
                raise ForbiddenError(
                    message="Only the record owner or an Admin may delete this record.",
                )

            before_tag_ids = _load_record_tag_ids(session, record.id)
            before_payload = _record_to_audit_payload(record, tag_ids=before_tag_ids)
            now = datetime.now(tz=UTC)
            record.deleted_at = now
            record.updated_at = now
            session.flush()
            after_payload = _record_to_audit_payload(record, tag_ids=before_tag_ids)
            emit_audit_event(
                db_session=session,
                event_type=AuditEventType.SOFT_DELETE,
                actor_user_id=actor.user_id,
                target_record_id=record.id,
                before_payload=before_payload,
                after_payload=after_payload,
            )
        # else: already soft-deleted. Idempotent no-op; do not
        # emit another audit event and do not advance deleted_at.

    # Re-fetch with eager-loaded relations for response
    # serialization. The ``include_deleted=True`` opt-out is
    # required because the record is now soft-deleted.
    with db.session() as session:
        record_with_relations = _fetch_record_with_relations(
            session, record_id, actor, include_deleted=True
        )
        if record_with_relations is None:
            raise NotFoundError(message="Connection record not found after soft delete.")
        session.expunge_all()

    return record_with_relations


# ---------------------------------------------------------------------------
# Public function: get_record_history
# ---------------------------------------------------------------------------


def get_record_history(
    record_id: UUID,
    actor: Session,
    *,
    limit: int = _DEFAULT_PAGE_SIZE,
    offset: int = 0,
    include_deleted: bool = False,
) -> tuple[list[AuditEvent], int]:
    """Return paginated audit-event history for a record (F-011).

    Verifies the actor's organization owns the target record
    BEFORE returning audit data. Otherwise a malicious caller
    could enumerate audit events for records in other organizations
    by guessing UUIDs.

    Stable sort tiebreaker: ``ORDER BY event_timestamp DESC,
    AuditEvent.id ASC`` so paginated results are deterministic
    across requests even when multiple events fire in the same
    millisecond (e.g., a CREATE event and an immediately-following
    EDIT event).

    Pagination guardrails: ``limit`` is silently clamped to
    ``[1, _MAX_PAGE_SIZE]`` and ``offset`` is clamped to ``>= 0``.

    Args:
        record_id: UUID of the record whose history to fetch.
        actor: Authenticated :class:`Session`. ``actor.org_id``
            is checked against the record's ``org_id`` BEFORE
            the audit query runs.
        limit: Maximum events per page. Clamped.
        offset: Zero-indexed offset into the result set. Clamped.
        include_deleted: When ``True``, the org-scope check
            permits soft-deleted records (admin moderation
            view). Default ``False``.

    Returns:
        A 2-tuple of ``(events, total)``:

        * ``events``: List of :class:`AuditEvent` instances with
          eager-loaded ``actor`` relationship for fast UI
          rendering.
        * ``total``: Total count of audit events matching the
          record (for pagination labels).

    Raises:
        NotFoundError: Record not found in the actor's org (HTTP
            404; cross-org enumeration defense).
    """
    safe_limit = max(1, min(int(limit), _MAX_PAGE_SIZE))
    safe_offset = max(0, int(offset))

    with db.session() as session:
        # Org-scope check via the record itself. We use the
        # private helper so the org/soft-delete predicates stay
        # consistent with the other read paths in this module.
        org_check_stmt = select(Record.id).where(Record.id == record_id)
        org_check_stmt = _apply_org_scope(org_check_stmt, actor)
        org_check_stmt = _apply_soft_delete_scope(org_check_stmt, include_deleted=include_deleted)
        if session.execute(org_check_stmt).scalar_one_or_none() is None:
            raise NotFoundError(message="Connection record not found.")

        base = select(AuditEvent).where(AuditEvent.target_record_id == record_id)

        # Total count for pagination metadata. Subquery wrapper
        # preserves the WHERE semantics while reducing the
        # projection to COUNT(*).
        count_stmt = select(func.count()).select_from(base.subquery())

        page_stmt = (
            base.options(joinedload(AuditEvent.actor))
            .order_by(desc(AuditEvent.event_timestamp), AuditEvent.id.asc())
            .limit(safe_limit)
            .offset(safe_offset)
        )

        total = int(session.execute(count_stmt).scalar_one())
        rows = list(session.execute(page_stmt).scalars().all())
        # Detach so attribute access after session close is safe.
        session.expunge_all()

    return rows, total


# ---------------------------------------------------------------------------
# Private helpers - SQL composition
# ---------------------------------------------------------------------------


def _apply_org_scope(stmt: Select[Any], actor: Session) -> Select[Any]:
    """Inject ``WHERE org_id = actor.org_id`` per architectural invariant 3.

    Centralized so every read path uses the SAME predicate; a
    typo in any single call site would otherwise be a multi-tenant
    isolation bug.

    Args:
        stmt: The SELECT statement under construction. The leading
            FROM target MUST expose an ``org_id`` column matching
            :attr:`Record.org_id`.
        actor: Authenticated :class:`Session`.

    Returns:
        The statement with the additional WHERE predicate.
    """
    return stmt.where(Record.org_id == actor.org_id)


def _apply_soft_delete_scope(stmt: Select[Any], *, include_deleted: bool = False) -> Select[Any]:
    """Inject ``WHERE deleted_at IS NULL`` per architectural invariant 4.

    Centralized so every read path uses the SAME predicate; a
    forgotten filter would surface soft-deleted records in the
    feed, contradicting the F-007 invariant.

    Args:
        stmt: The SELECT statement under construction. The leading
            FROM target MUST expose a ``deleted_at`` column matching
            :attr:`Record.deleted_at`.
        include_deleted: When ``True``, the predicate is NOT
            injected. Used by admin moderation paths.

    Returns:
        The statement with (or without) the additional WHERE
        predicate.
    """
    if include_deleted:
        return stmt
    return stmt.where(Record.deleted_at.is_(None))


def _apply_filters(stmt: Select[Any], filters: ConnectionFilters) -> Select[Any]:
    """Apply the seven filter dimensions per AAP s 0.5.2 Layer 4.

    Each filter is conditional - an unset (None or empty) filter
    does NOT modify the statement. Tag filtering uses a subquery
    on ``record_tags`` for "any-of" semantics rather than an inner
    join, so the result set stays unique even when a record matches
    multiple requested tags.

    Args:
        stmt: The SELECT statement under construction.
        filters: :class:`ConnectionFilters` carrying the optional
            filter parameters.

    Returns:
        The statement with all active filter predicates layered
        on top.
    """
    if filters.company:
        stmt = stmt.where(Record.company.ilike(f"%{filters.company}%"))
    if filters.involvement:
        stmt = stmt.where(Record.involvement.in_(filters.involvement))
    if filters.outreach_status:
        stmt = stmt.where(Record.outreach_status.in_(filters.outreach_status))
    if filters.owner_user_ids:
        stmt = stmt.where(Record.owner_user_id.in_(filters.owner_user_ids))
    if filters.submission_date_from is not None:
        stmt = stmt.where(Record.submission_date >= filters.submission_date_from)
    if filters.submission_date_to is not None:
        stmt = stmt.where(Record.submission_date <= filters.submission_date_to)
    if filters.full_name_search:
        stmt = stmt.where(Record.full_name.ilike(f"%{filters.full_name_search}%"))
    if filters.tag_ids:
        # Tag filtering via subquery on record_tags; "any-of"
        # semantics. The subquery returns the distinct set of
        # record ids that match the requested tags; the outer
        # query then restricts Record.id to that set. This avoids
        # the row duplication that an INNER JOIN with tag IN (...)
        # would produce when a record carries multiple matching
        # tags.
        tag_subquery = (
            select(RecordTag.record_id).where(RecordTag.tag_id.in_(filters.tag_ids)).distinct()
        )
        stmt = stmt.where(Record.id.in_(tag_subquery))
    return stmt


def _count_active_filters(filters: ConnectionFilters) -> int:
    """Return the number of filter dimensions that are actively narrowing.

    Used only for the structured log line emitted by
    :func:`list_records` so SREs can correlate slow queries with
    filter complexity. The count is purely diagnostic; it does NOT
    affect query semantics.

    Args:
        filters: :class:`ConnectionFilters` instance.

    Returns:
        Integer count of "set" filter fields. ``include_deleted``
        is NOT counted because it is an opt-out flag, not a
        narrowing dimension.
    """
    count = 0
    if filters.company:
        count += 1
    if filters.involvement:
        count += 1
    if filters.outreach_status:
        count += 1
    if filters.owner_user_ids:
        count += 1
    if filters.tag_ids:
        count += 1
    if filters.submission_date_from is not None:
        count += 1
    if filters.submission_date_to is not None:
        count += 1
    if filters.full_name_search:
        count += 1
    return count


# ---------------------------------------------------------------------------
# Private helpers - record/tag access
# ---------------------------------------------------------------------------


def _fetch_record_with_relations(
    session: DBSession,
    record_id: UUID,
    actor: Session,
    *,
    include_deleted: bool = False,
) -> Record | None:
    """Fetch a record with eager-loaded tags, org-scoped.

    Args:
        session: The open SQLAlchemy session.
        record_id: UUID of the record to fetch.
        actor: Authenticated :class:`Session` for org-scope.
        include_deleted: When ``True``, soft-deleted records are
            returned.

    Returns:
        The :class:`Record` with eager-loaded ``record_tags`` ->
        ``tag`` chain, or ``None`` when no record matches the
        org-scoped lookup.
    """
    stmt = select(Record).where(Record.id == record_id)
    stmt = _apply_org_scope(stmt, actor)
    stmt = _apply_soft_delete_scope(stmt, include_deleted=include_deleted)
    stmt = stmt.options(
        selectinload(Record.record_tags).selectinload(RecordTag.tag),
    )
    return session.execute(stmt).scalar_one_or_none()


def _fetch_record_for_update(
    session: DBSession,
    record_id: UUID,
    actor: Session,
    *,
    include_deleted: bool = False,
) -> Record | None:
    """Fetch a record for in-place mutation. Same scoping but no eager loads.

    Used by mutation paths (update_record, update_status,
    soft_delete_record) where the response serialization happens
    in a separate session AFTER commit, so eager-loading at this
    fetch is wasted work.

    Args:
        session: The open SQLAlchemy session.
        record_id: UUID of the record to fetch.
        actor: Authenticated :class:`Session` for org-scope.
        include_deleted: When ``True``, soft-deleted records are
            returned.

    Returns:
        The :class:`Record` (without eager-loaded relationships),
        or ``None`` when no record matches.
    """
    stmt = select(Record).where(Record.id == record_id)
    stmt = _apply_org_scope(stmt, actor)
    stmt = _apply_soft_delete_scope(stmt, include_deleted=include_deleted)
    return session.execute(stmt).scalar_one_or_none()


def _resolve_org_tags(session: DBSession, org_id: UUID, tag_ids: Iterable[UUID]) -> Sequence[Tag]:
    """Fetch all Tag rows in ``tag_ids`` that belong to the given org.

    Defensive cross-org check: a Contributor in org A could
    technically attempt to attach a tag id from org B if the
    schema layer alone validated tag ids. This helper performs
    the JOIN-equivalent check inside a single SELECT so the
    cross-org tag injection vector is closed structurally.

    Tag id deduplication: callers may submit duplicate ids in
    the payload; ``set(tag_id_tuple)`` deduplicates BEFORE the
    SELECT so the database round-trip is minimal AND the
    resolution-count check below correctly compares against the
    distinct count.

    Args:
        session: The open SQLAlchemy session.
        org_id: The actor's organization id.
        tag_ids: Iterable of UUIDs requested by the caller.

    Returns:
        Sequence of :class:`Tag` instances, deduplicated and
        order-irrelevant. Empty sequence when ``tag_ids`` is
        empty.

    Raises:
        ValidationFailedError: One or more requested tag ids
            were not found in the actor's org (HTTP 422 with
            field-scoped detail per missing id).
    """
    tag_id_tuple = tuple(tag_ids)
    if not tag_id_tuple:
        return []
    distinct_ids = list(set(tag_id_tuple))
    tags = list(
        session.execute(select(Tag).where(Tag.id.in_(distinct_ids), Tag.org_id == org_id))
        .scalars()
        .all()
    )
    found_ids = {tag.id for tag in tags}
    missing_ids = [tid for tid in distinct_ids if tid not in found_ids]
    if missing_ids:
        # Cross-org and unknown-id cases are intentionally lumped
        # into the same error code so the response does NOT leak
        # the existence of cross-org tags (info-disclosure
        # defense).
        fields = [
            {
                "field": f"tag_ids.{tid}",
                "code": "value_error.unknown_tag",
                "message": "Tag does not exist in your organization.",
            }
            for tid in missing_ids
        ]
        raise ValidationFailedError(
            message="One or more tag ids are invalid.",
            fields=fields,
        )
    return tags


def _replace_record_tags(
    session: DBSession,
    record: Record,
    tag_ids: tuple[UUID, ...],
    org_id: UUID,
) -> None:
    """Replace the ``record_tags`` rows for ``record`` with the given set.

    Computes the diff between the record's current tag
    associations and the requested set, then issues only the
    needed INSERTs and DELETEs. Tags that are already associated
    are left untouched so the audit trail stays clean.

    Args:
        session: The open SQLAlchemy session.
        record: The :class:`Record` whose tags are being replaced.
        tag_ids: The new (final) tuple of tag ids to associate.
            Empty tuple removes all existing associations.
        org_id: The actor's organization id (forwarded to
            :func:`_resolve_org_tags` for cross-org defense).

    Raises:
        ValidationFailedError: One or more requested tag ids are
            not in the actor's org (raised by
            :func:`_resolve_org_tags`).
    """
    # Validate tags belong to org first (raises if invalid).
    new_tags = _resolve_org_tags(session, org_id, tag_ids)
    new_tag_ids: set[UUID] = {tag.id for tag in new_tags}

    existing_assoc = list(
        session.execute(select(RecordTag).where(RecordTag.record_id == record.id)).scalars().all()
    )
    existing_tag_ids: set[UUID] = {assoc.tag_id for assoc in existing_assoc}

    # Delete removed associations.
    for assoc in existing_assoc:
        if assoc.tag_id not in new_tag_ids:
            session.delete(assoc)

    # Add new associations.
    for tag_id in new_tag_ids:
        if tag_id not in existing_tag_ids:
            session.add(RecordTag(record_id=record.id, tag_id=tag_id))


def _load_record_tag_ids(session: DBSession, record_id: UUID) -> list[UUID]:
    """Return the list of tag ids currently associated with ``record_id``.

    Used by mutation paths to capture the tag-id snapshot for the
    audit-event before/after payloads. Issues a single SELECT
    against the indexed ``record_tags.record_id`` column.

    Args:
        session: The open SQLAlchemy session.
        record_id: UUID of the parent record.

    Returns:
        List of tag UUIDs (may be empty).
    """
    rows = (
        session.execute(select(RecordTag.tag_id).where(RecordTag.record_id == record_id))
        .scalars()
        .all()
    )
    return list(rows)


# ---------------------------------------------------------------------------
# Private helpers - audit payload construction
# ---------------------------------------------------------------------------


def _record_to_audit_payload(
    record: Record, *, tag_ids: list[UUID] | None = None
) -> dict[str, Any]:
    """Serialize a Record to a JSON-safe dict for audit_events payloads.

    Per AAP Section 0.7.4 (Security Invariants), the free-text
    fields ``relationship_context`` and ``ai_notes`` are
    deliberately EXCLUDED from the audit payload because they may
    contain user-supplied PII (e.g., personal anecdotes about the
    contributor's network). The structural identifiers (record id,
    org id, owner identity, business identifiers, status, dates,
    tag ids) ARE included so an operator can reconstruct WHO did
    WHAT to WHICH record at WHAT TIME from the audit trail
    without leaking PII.

    Output dict ordering is stable: this dict is JSON-serialized
    into ``audit_events.before_payload`` / ``after_payload``;
    stable field order makes diff inspection in the edit-history
    UI deterministic.

    Args:
        record: The :class:`Record` instance to serialize.
        tag_ids: Optional pre-computed list of tag UUIDs. When
            supplied, used directly; when ``None``, falls back to
            an empty list (callers that need accurate tag_ids
            MUST supply them via :func:`_load_record_tag_ids`).
            The fallback path exists so the helper does not
            trigger lazy-load round-trips inside the audit
            emit.

    Returns:
        A JSON-safe dict with all structural identifiers. Suitable
        for direct assignment to
        ``AuditEvent.before_payload`` / ``after_payload``.
    """
    return {
        "id": str(record.id),
        "org_id": str(record.org_id),
        "owner_user_id": str(record.owner_user_id),
        "owner_display_name": record.owner_display_name,
        "full_name": record.full_name,
        "linkedin_url": record.linkedin_url,
        "normalized_linkedin_url": record.normalized_linkedin_url,
        "company": record.company,
        "job_title": record.job_title,
        "involvement": (record.involvement.value if record.involvement is not None else None),
        "outreach_status": (
            record.outreach_status.value if record.outreach_status is not None else None
        ),
        "submission_date": (record.submission_date.isoformat() if record.submission_date else None),
        "deleted_at": record.deleted_at.isoformat() if record.deleted_at else None,
        "tag_ids": [str(tid) for tid in (tag_ids or [])],
    }
