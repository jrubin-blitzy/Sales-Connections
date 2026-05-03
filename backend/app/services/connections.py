"""Connection-record service layer (F-001 Connection Idea Form).

This module owns the business logic for creating Connection-Idea
records. Per AAP Section 0.5.3 ("Service functions own
transactions"), :func:`create_record` opens its own
``with session.begin():`` block and invokes
:func:`app.services.audit.emit_audit_event` inside that transaction so
the state change and the audit row commit (or roll back) atomically
per AAP Section 0.7.1 invariant 6 (atomic state-change + audit pair).

What this module enforces, beyond what pydantic guarantees on the
inbound payload:

* **Owner attribution permanence (F-006).** ``owner_user_id`` is
  derived from the authenticated session, NOT from the inbound
  payload. ``owner_display_name`` is denormalized from the User row
  for fast feed rendering (per AAP Section 0.7.6); it is captured
  AT submission time and remains stable even if the user later
  renames.
* **Multi-tenant scoping (F-009 architectural invariant 3).**
  ``org_id`` is derived from ``actor.org_id``. Tags supplied via
  ``payload.tag_ids`` are validated to live in the SAME org -- a
  cross-org tag id reference is rejected with HTTP 422.
* **LinkedIn URL normalization (F-010).** The URL is normalized via
  :func:`app.utils.url.normalize_linkedin_url` BEFORE the INSERT so
  the unique partial index
  ``(org_id, normalized_linkedin_url) WHERE deleted_at IS NULL``
  behaves predictably even when contributors paste different
  variants (with/without ``www``, trailing slash, query string).
* **Duplicate-detection race resolution (F-010).** The pre-check
  via :func:`find_duplicate` is best-effort; the database's unique
  partial index is the authoritative race-resolver. An
  ``IntegrityError`` raised by ``session.flush()`` is caught and
  surfaces as HTTP 409 with ``error.code = "duplicate_record"`` so
  the SPA can show an explicit duplicate-warning UX rather than a
  generic 500.
* **Audit-event emission (F-013).** A single
  :class:`app.models.enums.AuditEventType.CREATE` event is emitted
  inside the same transaction. The ``after_payload`` snapshots the
  business fields plus owner/tag identifiers for forensic
  reconstruction; the ``before_payload`` is ``NULL`` because no
  prior state exists for a CREATE event.

What this module does NOT do:

* It does NOT validate the LinkedIn URL format. That happens at the
  schema layer (``ConnectionCreate`` field validator) BEFORE this
  service is invoked.
* It does NOT perform RBAC. That happens at the API layer via the
  ``@requires_role(...)`` decorator on the handler.
* It does NOT log raw user-supplied free text (relationship_context,
  ai_notes, etc.) per AAP Section 0.7.4 security invariant.
* It does NOT mutate ``g.session``; sessions are read-only.

Performance:

The end-to-end create flow target is sub-2-second per AAP
Section 0.7.3 (form submit budget excluding AI). Within this budget
the database operations are:

* 1 SELECT to load the User row for ``owner_display_name``.
* 1 SELECT to validate tag ids against the org (skipped if the
  payload has zero tags).
* 1 SELECT to short-circuit on existing duplicate (best-effort).
* 1 INSERT into ``records``.
* N INSERTs into ``record_tags`` (N = len(payload.tag_ids)).
* 1 INSERT into ``audit_events``.

All within a single ``with session.begin():`` block. The whole
sequence completes in O(10ms) at MVP scale; the 2-second budget is
not at risk.

Public API:

:class:`DuplicateRecordError`
    AppError subclass mapped to HTTP 409 with stable error code
    ``duplicate_record``. Raised when the unique partial index on
    ``normalized_linkedin_url`` fires either via the pre-check or
    via the database's race-resolution.

:func:`create_record`
    The sole writer of new ``records`` rows. Invoked by the
    ``POST /api/connections`` handler with the validated
    :class:`app.schemas.connection.ConnectionCreate` payload and the
    authenticated :class:`app.middleware.auth.Session`. Returns the
    persisted :class:`app.models.record.Record` so the handler can
    serialize it into a :class:`app.schemas.connection.ConnectionRead`
    response.
"""

from __future__ import annotations

import http
import logging
import time
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.extensions import db
from app.middleware.error_handlers import (
    AppError,
    NotFoundError,
    ValidationFailedError,
)
from app.models.audit_event import AuditEvent
from app.models.enums import AuditEventType
from app.models.record import Record
from app.models.tag import RecordTag, Tag
from app.models.user import User
from app.services.audit import emit_audit_event
from app.services.duplicate_detection import find_duplicate
from app.utils.url import normalize_linkedin_url

if TYPE_CHECKING:
    from app.middleware.auth import Session
    from app.schemas.connection import ConnectionCreate


# ---------------------------------------------------------------------------
# Module logger
# ---------------------------------------------------------------------------
# The stdlib logger routes through structlog's processor chain
# (configured in ``app.observability.logging``). The
# ``add_stdlib_record_extras`` processor (introduced for QA Issue 11)
# promotes ``extra={...}`` kwargs from stdlib LogRecord into the
# emitted JSON body so the structural fields below are searchable in
# CloudWatch Insights without a custom log parser.
_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------
# ``create_record`` is the only entry point. ``DuplicateRecordError``
# is exported so handlers and tests can pin to the typed exception
# class rather than catching the broad ``AppError``.
__all__ = [
    "DuplicateRecordError",
    "create_record",
]


# ---------------------------------------------------------------------------
# Stable error codes
# ---------------------------------------------------------------------------
# Stable strings consumed by the SPA's typed error dispatch. Centralized
# at module scope so the constants can be referenced from tests
# without re-stringifying the literal at every call site.
_ERROR_CODE_DUPLICATE_RECORD: str = "duplicate_record"


# ---------------------------------------------------------------------------
# Exception classes
# ---------------------------------------------------------------------------


class DuplicateRecordError(AppError):
    """Raised when the unique partial index on ``normalized_linkedin_url`` fires.

    Two paths reach this exception:

    1. The best-effort pre-check via :func:`find_duplicate` returned a
       matching active record for the same ``(org_id,
       normalized_linkedin_url)`` pair. We raise BEFORE the INSERT
       to avoid a wasted round-trip.
    2. The pre-check returned ``None`` (or was skipped) but the
       database's unique partial index fired during INSERT, indicating
       a concurrent submit by another contributor. The
       ``IntegrityError`` from ``session.flush()`` is caught and
       converted to this exception so the handler emits the same
       409 envelope the SPA expects regardless of timing.

    Mapped to HTTP 409 with ``error.code = "duplicate_record"`` by the
    registered Flask error handler in
    :mod:`app.middleware.error_handlers` via the generic
    ``_handle_app_error`` handler (which reads ``status_code`` and
    ``error_code`` off the instance).

    Per AAP Section 0.7.6 (Business Rules), F-010 duplicate detection
    is a "warning, not a block" -- but that contract applies to the
    PRE-SUBMIT duplicate-check endpoint
    (``GET /api/connections/duplicate-check``) which always returns
    HTTP 200 with ``duplicate_found=true|false``. The CREATE flow,
    in contrast, MUST honour the unique constraint at the database
    level: two contributors cannot both win the race to log the same
    LinkedIn URL into the same org. The 409 surface lets the SPA
    re-fetch the now-existing record and surface a clear "Jane Doe
    just submitted this contact" message.
    """

    status_code: int = http.HTTPStatus.CONFLICT.value  # 409
    error_code: str = _ERROR_CODE_DUPLICATE_RECORD

    @property
    def default_message(self) -> str:
        """Return the default duplicate-record message."""
        return "A connection with this LinkedIn URL already exists in your organization."


# ---------------------------------------------------------------------------
# Public function: create_record
# ---------------------------------------------------------------------------


def create_record(payload: ConnectionCreate, actor: Session) -> Record:
    """Create a new Connection-Idea record (F-001).

    Workflow (single transaction):

    1. Lookup the actor's :class:`User` row to capture the
       ``owner_display_name`` snapshot.
    2. Normalize ``payload.linkedin_url`` via
       :func:`app.utils.url.normalize_linkedin_url`.
    3. Pre-check for an existing duplicate via
       :func:`find_duplicate`; raise :class:`DuplicateRecordError`
       BEFORE the INSERT if found.
    4. Resolve and validate ``payload.tag_ids`` against the actor's
       org. Cross-org or unknown tag ids surface as HTTP 422.
    5. Open ``with session.begin():`` and:
        a. ``session.add(record)`` -- the new ``records`` row.
        b. ``session.flush()`` -- forces the INSERT so we discover
           any constraint violations BEFORE adding RecordTag rows
           (and BEFORE :func:`emit_audit_event`, which references
           ``record.id``).
        c. For each tag id, ``session.add(RecordTag(...))`` to
           materialize the M:N edge.
        d. :func:`emit_audit_event` with
           :class:`AuditEventType.CREATE`. The audit row's
           ``after_payload`` snapshots the business identifiers; the
           free-text fields (relationship_context, ai_notes) are
           NOT included (PII risk per AAP Section 0.7.4).
    6. ``session.refresh(record, ['record_tags'])`` so the response
       can serialize the tag relationship without an extra
       round-trip.
    7. Return the persisted :class:`Record`.

    Atomicity guarantee: the ``with session.begin():`` block ensures
    the records INSERT, the record_tags INSERTs, AND the
    audit_events INSERT either all commit or all roll back. A
    failure at any step (e.g., a tag id that doesn't exist, an
    audit-emit error, the unique-index race firing during flush)
    cleanly rolls back the entire create.

    Multi-tenant scoping: every SQL predicate that touches user-
    visible data (tag lookup, duplicate lookup, owner lookup)
    injects ``org_id == actor.org_id`` so cross-org bleed is
    structurally prevented. The audit row is scoped via
    ``actor_user_id``, which inherently belongs to ``actor.org_id``
    via the ``users.org_id`` FK.

    Args:
        payload: Schema-validated :class:`ConnectionCreate` from the
            inbound JSON body. The schema has already enforced the
            nine-field shape, length caps, LinkedIn URL format,
            ``involvement`` enum membership, and rejection of any
            client-supplied owner / status overrides.
        actor: Authenticated :class:`Session` populated by
            :mod:`app.middleware.auth`. ``actor.user_id`` and
            ``actor.org_id`` are read for owner attribution and
            multi-tenant scoping; no other attribute is consumed.

    Returns:
        The persisted :class:`Record` with ``id``, ``submission_date``,
        ``created_at``, ``updated_at`` populated by PostgreSQL
        defaults, and the ``record_tags`` collection eagerly loaded
        so the API handler can serialize the response without N+1.

    Raises:
        DuplicateRecordError: A non-soft-deleted record with the same
            normalized LinkedIn URL already exists in the actor's
            org (HTTP 409, ``error.code = "duplicate_record"``).
        ValidationFailedError: One or more ``tag_ids`` reference tags
            that don't exist in the actor's org (HTTP 422,
            ``error.code = "validation_failed"``, with field-scoped
            details).
        NotFoundError: The actor's User row is missing (HTTP 404).
            This is unreachable in practice -- the auth middleware
            verifies ``users.token_version`` on every request, which
            implicitly requires the User row to exist -- but the
            explicit raise documents the invariant for future
            readers.
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
                    "loc": ["linkedin_url"],
                    "msg": "Could not normalize LinkedIn URL.",
                    "type": "value_error.url",
                }
            ],
        )

    # ----- Open the transaction --------------------------------------
    # The ``with db.session() as session:`` context manages the
    # connection lifetime; the inner ``with session.begin():`` opens
    # the explicit transaction within which the audit row and the
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
            raise NotFoundError(
                message="The authenticated user could not be found.",
            )
        owner_display_name = owner.display_name

        # ----- Step 2: Resolve and validate tags ---------------------
        # Skip the round-trip when the payload has zero tags.
        # Otherwise lookup ALL tag ids in a single SELECT and verify
        # each requested id was found AND belongs to the actor's org.
        # Cross-org or unknown tag ids surface as a structured 422.
        resolved_tags: list[Tag] = []
        if payload.tag_ids:
            # ``set(payload.tag_ids)`` deduplicates request payload
            # repetitions before issuing the SELECT; the database's
            # composite primary key on ``record_tags`` would also
            # reject duplicates downstream, but pre-deduplication
            # avoids a wasted IntegrityError catch.
            requested_tag_ids = list(set(payload.tag_ids))
            resolved_tags = list(
                session.execute(
                    select(Tag).where(
                        Tag.id.in_(requested_tag_ids),
                        Tag.org_id == org_id,
                    )
                )
                .scalars()
                .all()
            )
            resolved_ids = {tag.id for tag in resolved_tags}
            missing_ids = [tid for tid in requested_tag_ids if tid not in resolved_ids]
            if missing_ids:
                # Structured 422 with one field-error per missing id
                # so the SPA can highlight the offending chip(s) in
                # the tag-input control. Cross-org and unknown-id
                # cases are intentionally lumped into the same error
                # code to avoid leaking the existence of cross-org
                # tags (info disclosure defense).
                fields = [
                    {
                        "loc": ["tag_ids", str(tid)],
                        "msg": "Tag does not exist in your organization.",
                        "type": "value_error.unknown_tag",
                    }
                    for tid in missing_ids
                ]
                raise ValidationFailedError(
                    message="One or more tag ids are invalid.",
                    fields=fields,
                )

        # ----- Step 3: Pre-check for duplicate -----------------------
        # Best-effort lookup against the partial unique index. The
        # database is the authoritative race-resolver below; this
        # pre-check just avoids wasting a flush when the duplicate
        # is already visible.
        existing = find_duplicate(org_id, normalized_url, db_session=session)
        if existing is not None:
            raise DuplicateRecordError()

        # ----- Step 4: Construct and INSERT the record --------------
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

        # ----- Step 5: Materialize tag associations ------------------
        # Build the M:N rows now that the parent ``records`` row
        # exists with its server-assigned id. The composite primary
        # key on ``record_tags`` (record_id, tag_id) deduplicates
        # naturally; we deduplicated client-side above just to avoid
        # the IntegrityError on payload-side repetition.
        for tag in resolved_tags:
            session.add(RecordTag(record_id=record.id, tag_id=tag.id))

        # Flush again so the record_tags INSERTs land before the
        # audit row. Not strictly required (the entire transaction
        # commits atomically) but keeps the database state visible
        # to the audit emitter's session-scoped queries (none today,
        # but defensively).
        if resolved_tags:
            session.flush()

        # ----- Step 6: Emit audit event ------------------------------
        # ``after_payload`` snapshots the business identifiers so an
        # operator can reconstruct the record from CloudWatch JSON
        # log lines plus the audit table without consulting the
        # ``records`` table directly. Free-text PII fields
        # (relationship_context, ai_notes) are deliberately omitted
        # per AAP Section 0.7.4 ("the payload bodies are NEVER
        # logged because they may contain user-supplied free text
        # that constitutes PII").
        after_payload: dict[str, Any] = {
            "id": str(record.id),
            "org_id": str(record.org_id),
            "owner_user_id": str(record.owner_user_id),
            "owner_display_name": record.owner_display_name,
            "full_name": record.full_name,
            "linkedin_url": record.linkedin_url,
            "normalized_linkedin_url": record.normalized_linkedin_url,
            "company": record.company,
            "job_title": record.job_title,
            "involvement": record.involvement.value,
            "outreach_status": record.outreach_status.value,
            "tag_ids": [str(tag.id) for tag in resolved_tags],
        }
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
    _logger.info(
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
# Internal helper: serialize Record + tags into ConnectionRead-ready dict
# ---------------------------------------------------------------------------
# The handler imports ``ConnectionRead.model_validate`` directly and
# calls ``.model_dump(mode='json')`` on the result, so the service
# layer does not need a serialization helper. This comment is kept
# as a marker so that contributors who add additional service
# functions (PATCH, DELETE, GET) maintain the same convention:
# services return ORM rows; handlers serialize.
#
# NOTE: ``AuditEvent`` is imported at the top of this module to
# document the audit-emission dependency in the import graph; the
# actual ``AuditEvent`` constructor is invoked from
# ``app.services.audit.emit_audit_event``, NOT from this module
# directly. Linting rules permitting unused imports for
# documentation purposes are deliberate; the ``AuditEvent`` name is
# referenced by the type system through the
# :func:`emit_audit_event` return annotation.
_AUDIT_EVENT_TYPE_REF: type[AuditEvent] = AuditEvent
_UUID_TYPE_REF: type[UUID] = UUID
