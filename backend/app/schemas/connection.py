"""Pydantic 2.x schemas for the Connection Record domain.

Schemas defined here:

- ``ConnectionCreate``                 Inbound payload for POST /api/connections (F-001).
- ``ConnectionUpdate``                 Inbound payload for PATCH /api/connections/:id (F-007).
- ``ConnectionStatusUpdate``           Inbound for PATCH /api/connections/:id/status (F-005).
- ``ConnectionRead``                   Outbound shape for GET /api/connections[/:id] (F-004, F-011).
- ``ConnectionDuplicateCheckResponse`` Response for GET /api/connections/duplicate-check (F-010).
- ``ConnectionHistoryEntry``           Per-row shape for GET /api/connections/:id/history (F-011).
- ``PaginatedConnections``             Pagination envelope for the feed list endpoint.
- ``TagRead``, ``TagCreate``           Tag CRUD schemas (F-008).

Per AAP Section 0.7.1 (Architectural Invariant 8), these schemas are
the AUTHORITATIVE server-side validation gates. Any payload that
reaches a Flask handler is re-validated by them regardless of any
client-side Zod validation.

Per AAP Section 0.7.4 (Security Invariants):

* Owner identity is always derived from ``g.session.user_id``;
  ``ConnectionCreate`` REJECTS any client-supplied ``owner_user_id``
  or ``owner_display_name`` value via ``extra='forbid'``.
* LinkedIn URLs are validated by ``app.utils.url.is_valid_linkedin_url``
  so malformed URLs surface as HTTP 422 with field-scoped errors.

Per AAP Section 0.5.3, schemas mirror Zod schemas in
``frontend/src/schemas/connection.ts`` field-for-field; drift between
the two layers is a defect.

Note on imports: pydantic v2 evaluates field type hints at
class-definition time (via ``get_type_hints``) to wire up its
validators, so ``AwareDatetime``, ``UUID``, and the domain enum
classes MUST be present at runtime even though they appear only in
type annotations. The ``# noqa: TC001/TC002/TC003`` suppressions on
the import lines document this constraint and prevent ruff's
flake8-type-checking strict mode from moving the imports into a
``TYPE_CHECKING`` block (which would break the schemas at first
use).
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID  # noqa: TC003  (used in pydantic type annotations at runtime)

from pydantic import (  # noqa: TC002  (pydantic resolves these at class-construction time)
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
)

from app.models.enums import (  # noqa: TC001  (used in pydantic type annotations at runtime)
    AuditEventType,
    InvolvementType,
    OutreachStatus,
)
from app.utils.url import is_valid_linkedin_url

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# Field length caps. These match the underlying VARCHAR / TEXT column
# lengths in ``app.models.record.Record`` so that pydantic rejection
# happens BEFORE the database would otherwise raise a DataError.
_FULL_NAME_MAX_CHARS: int = 255
_LINKEDIN_URL_MAX_CHARS: int = 2048
_COMPANY_MAX_CHARS: int = 255
_JOB_TITLE_MAX_CHARS: int = 255
# Matches ``BaseConfig.AI_PROMPT_CONTEXT_MAX_CHARS`` AND
# ``_RELATIONSHIP_CONTEXT_MAX_CHARS`` in ``app.schemas.note_generation``.
# Drift between these three constants is a defect.
_RELATIONSHIP_CONTEXT_MAX_CHARS: int = 4000
# Matches ``_AI_NOTES_MAX_CHARS`` in ``app.schemas.note_generation``.
_AI_NOTES_MAX_CHARS: int = 8000
_OWNER_DISPLAY_NAME_MAX_CHARS: int = 255
# Matches ``Tag.name`` ``String(64)`` length in ``app.models.tag``.
_TAG_NAME_MAX_CHARS: int = 64

# Pagination defaults for ``GET /api/connections`` list endpoint.
# ``_DEFAULT_PAGE_SIZE`` is informational only (the handler resolves
# the user-requested ``limit`` query parameter and defaults to this
# value); ``_MAX_PAGE_SIZE`` is the hard upper bound enforced by
# ``PaginatedConnections.limit`` so a malicious client cannot request
# a 1,000,000-record page.
_DEFAULT_PAGE_SIZE: int = 50
_MAX_PAGE_SIZE: int = 200

# Per the assigned folder Conventions: every inbound schema rejects
# unexpected keys (defends against client-supplied owner_user_id /
# owner_display_name per AAP Section 0.7.4) and strips whitespace.
# ``str_strip_whitespace=True`` runs BEFORE length validation, so a
# whitespace-only payload becomes ``""`` post-strip and is rejected by
# ``min_length=1`` rather than passing through as a meaningless value.
_STRICT_CONFIG: ConfigDict = ConfigDict(
    extra="forbid",
    str_strip_whitespace=True,
)

# Outbound config enables ORM-mode serialization. Pydantic's
# ``from_attributes=True`` mode reads ONLY the fields declared on the
# schema; extra ORM attributes (e.g. internal ``org_id``,
# ``password_hash``) are silently ignored. This is defense in depth
# against accidental leakage of server-only fields.
_OUTBOUND_CONFIG: ConfigDict = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Public exports
# ---------------------------------------------------------------------------

__all__ = [
    "ConnectionCreate",
    "ConnectionDuplicateCheckResponse",
    "ConnectionHistoryEntry",
    "ConnectionRead",
    "ConnectionStatusUpdate",
    "ConnectionUpdate",
    "PaginatedConnections",
    "TagCreate",
    "TagRead",
]


# ---------------------------------------------------------------------------
# Tag schemas (F-008)
# ---------------------------------------------------------------------------


class TagRead(BaseModel):
    """Outbound shape for an organization-scoped tag (F-008).

    Returned by ``GET /api/tags`` (a list) and embedded inside
    ``ConnectionRead.tags`` for the feed/detail views.

    Fields:
        id          UUID v4 primary key.
        name        Tag display name (1-64 chars). Unique within the
                    organization (enforced at the database layer by
                    ``UniqueConstraint('org_id', 'name')``).
        created_at  Server-assigned timestamp at insertion time.

    ``org_id`` is intentionally excluded; it is a server-only
    multi-tenant scoping concept and exposing it would leak internal
    addressing detail to the client.
    """

    model_config = _OUTBOUND_CONFIG

    id: UUID
    name: Annotated[str, Field(min_length=1, max_length=_TAG_NAME_MAX_CHARS)]
    created_at: AwareDatetime


class TagCreate(BaseModel):
    """Inbound payload for ``POST /api/tags`` (F-008).

    Org scope is derived from ``g.session.org_id`` server-side; the
    client cannot set ``org_id``. The unique constraint on
    ``(org_id, name)`` is enforced at the database layer; a 409
    conflict response is returned by the handler if the tag name
    already exists for the organization.

    Per AAP Section 0.7.4 (Security Invariants), this schema's
    ``extra='forbid'`` rejects any other field (e.g., a malicious
    attempt to set ``org_id`` or ``id``) so creation is strictly
    limited to the ``name`` field.
    """

    model_config = _STRICT_CONFIG

    name: Annotated[
        str,
        Field(
            min_length=1,
            max_length=_TAG_NAME_MAX_CHARS,
            description=(
                "Tag display name (e.g., 'fintech', 'north-america'). "
                "Unique within the organization."
            ),
        ),
    ]


# ---------------------------------------------------------------------------
# Inbound: ConnectionCreate (F-001)
# ---------------------------------------------------------------------------


class ConnectionCreate(BaseModel):
    """Inbound payload for ``POST /api/connections`` (F-001).

    Validates the nine business fields the contributor enters on the
    Add Connection form. Per AAP Section 0.7.4 (Security Invariants),
    this schema REJECTS any client-supplied ``owner_user_id`` or
    ``owner_display_name`` value: owner identity is derived
    exclusively from ``g.session.user_id`` server-side. The
    ``extra='forbid'`` config makes any unexpected field surface as
    HTTP 422.

    Field validations:
        full_name              Required; 1-255 chars (whitespace stripped).
        linkedin_url           Required; 1-2048 chars; MUST be a syntactically
                               valid LinkedIn profile URL per
                               ``app.utils.url.is_valid_linkedin_url``.
        company                Required; 1-255 chars.
        job_title              Required; 1-255 chars.
        relationship_context   Required; 1-4000 chars.
                               (Matches ``AI_PROMPT_CONTEXT_MAX_CHARS``.)
        ai_notes               Optional; 0-8000 chars. May be NULL when
                               the AI call failed (non-blocking per
                               AAP F-002) or when the contributor
                               chose not to generate AI notes.
        involvement            Required; one of the three
                               ``InvolvementType`` enum values.
        tag_ids                Optional list of tag UUIDs. Tags must
                               already exist in the contributor's org
                               (creation happens via
                               ``POST /api/tags``).

    Anti-tampering rationale:
        A malicious client could attempt to send
        ``{"full_name": "...", "owner_user_id": "<admin_uuid>"}``
        to attribute the new record to someone else. Strict mode
        (``extra='forbid'``) rejects that with HTTP 422 before any
        handler logic runs. Similarly, the client cannot pre-set
        ``outreach_status='Closed'`` to bypass the sales-team
        workflow because ``outreach_status`` is not declared on this
        schema and is therefore rejected.
    """

    model_config = _STRICT_CONFIG

    full_name: Annotated[str, Field(min_length=1, max_length=_FULL_NAME_MAX_CHARS)]
    linkedin_url: Annotated[
        str,
        Field(min_length=1, max_length=_LINKEDIN_URL_MAX_CHARS),
    ]
    company: Annotated[str, Field(min_length=1, max_length=_COMPANY_MAX_CHARS)]
    job_title: Annotated[str, Field(min_length=1, max_length=_JOB_TITLE_MAX_CHARS)]
    relationship_context: Annotated[
        str,
        Field(min_length=1, max_length=_RELATIONSHIP_CONTEXT_MAX_CHARS),
    ]
    ai_notes: Annotated[str | None, Field(default=None, max_length=_AI_NOTES_MAX_CHARS)]
    involvement: InvolvementType
    tag_ids: Annotated[list[UUID], Field(default_factory=list)]

    @field_validator("linkedin_url")
    @classmethod
    def _validate_linkedin_url(cls, v: str) -> str:
        """Reject URLs that don't match the LinkedIn profile shape.

        Delegates to ``app.utils.url.is_valid_linkedin_url`` so the
        single source of truth for LinkedIn URL semantics lives in
        ``utils/url.py``. Malformed URLs surface as HTTP 422 with a
        field-scoped error message.
        """
        if not is_valid_linkedin_url(v):
            raise ValueError(
                "linkedin_url must be a valid LinkedIn profile URL "
                "(e.g., https://www.linkedin.com/in/<slug>)"
            )
        return v

    @field_validator("ai_notes", mode="before")
    @classmethod
    def _empty_ai_notes_to_none(cls, v: Any) -> Any:
        """Treat empty string as None for ai_notes.

        The SPA may submit ``ai_notes=""`` when the contributor cleared
        the AI textarea. Treating empty as None matches the database
        column's nullable semantics and avoids storing meaningless
        empty strings (which would create three valid states - NULL,
        "", non-empty - when only two are meaningful).
        """
        if isinstance(v, str) and v.strip() == "":
            return None
        return v


# ---------------------------------------------------------------------------
# Inbound: ConnectionUpdate (F-007)
# ---------------------------------------------------------------------------


class ConnectionUpdate(BaseModel):
    """Inbound payload for ``PATCH /api/connections/:id`` (F-007 edit).

    All fields are OPTIONAL - only the fields supplied by the client
    are updated. Validators (LinkedIn URL format, length caps) still
    apply when a field is present.

    Per AAP Section 0.7.4 (Security Invariants), this schema also
    rejects ``owner_user_id`` and ``owner_display_name`` via
    ``extra='forbid'`` because owner attribution is permanent (F-006).

    Per AAP Section 0.7.6 (Business Rules), ``outreach_status`` is
    NOT modifiable via this endpoint; status changes flow through
    ``PATCH /api/connections/:id/status`` (RBAC-gated to Sales Rep
    and Admin only). This schema does NOT include ``outreach_status``;
    a request body containing it is rejected by ``extra='forbid'``.

    PATCH semantics for ``tag_ids``:
        ``tag_ids: list[UUID] | None = None`` - a missing
        ``tag_ids`` key (``None`` after parsing) means "do not modify
        existing tags". An empty list (``[]``) is a meaningful value
        meaning "remove all tags from this record". The handler
        distinguishes the two cases when applying the update.
    """

    model_config = _STRICT_CONFIG

    full_name: Annotated[
        str | None,
        Field(default=None, min_length=1, max_length=_FULL_NAME_MAX_CHARS),
    ]
    linkedin_url: Annotated[
        str | None,
        Field(default=None, min_length=1, max_length=_LINKEDIN_URL_MAX_CHARS),
    ]
    company: Annotated[
        str | None,
        Field(default=None, min_length=1, max_length=_COMPANY_MAX_CHARS),
    ]
    job_title: Annotated[
        str | None,
        Field(default=None, min_length=1, max_length=_JOB_TITLE_MAX_CHARS),
    ]
    relationship_context: Annotated[
        str | None,
        Field(default=None, min_length=1, max_length=_RELATIONSHIP_CONTEXT_MAX_CHARS),
    ]
    ai_notes: Annotated[
        str | None,
        Field(default=None, max_length=_AI_NOTES_MAX_CHARS),
    ]
    involvement: InvolvementType | None = None
    tag_ids: list[UUID] | None = None

    @field_validator("linkedin_url")
    @classmethod
    def _validate_linkedin_url(cls, v: str | None) -> str | None:
        """Reject URLs that don't match the LinkedIn profile shape.

        Skipped when the field is absent (``None``); applied otherwise.
        """
        if v is None:
            return v
        if not is_valid_linkedin_url(v):
            raise ValueError(
                "linkedin_url must be a valid LinkedIn profile URL "
                "(e.g., https://www.linkedin.com/in/<slug>)"
            )
        return v


# ---------------------------------------------------------------------------
# Inbound: ConnectionStatusUpdate (F-005)
# ---------------------------------------------------------------------------


class ConnectionStatusUpdate(BaseModel):
    """Inbound payload for ``PATCH /api/connections/:id/status`` (F-005).

    The only field is ``outreach_status``. Per AAP Section 0.7.6:

    > "Outreach status is updatable only by Sales Rep or Admin roles,
    > never by the original submitter without Admin rights, in order
    > to preserve sales team accountability."

    RBAC enforcement happens at the handler level via the
    ``@requires_role('Viewer', 'Admin')`` decorator from
    ``app.middleware.rbac``; this schema only validates the payload
    shape.

    The handler emits an audit event of type ``status_change`` with
    ``before_payload`` and ``after_payload`` capturing the prior and
    new status (per AAP Section 0.5.2 Layer 4). The schema's
    ``extra='forbid'`` ensures a malicious client cannot smuggle
    additional fields (e.g., ``owner_user_id``, ``id``) alongside the
    status update.
    """

    model_config = _STRICT_CONFIG

    outreach_status: OutreachStatus


# ---------------------------------------------------------------------------
# Outbound: ConnectionRead (F-004, F-011)
# ---------------------------------------------------------------------------


class ConnectionRead(BaseModel):
    """Outbound shape for a Connection record.

    Returned by:
      - ``POST /api/connections`` (after creation)
      - ``GET /api/connections`` (list, embedded in
        ``PaginatedConnections.items``)
      - ``GET /api/connections/:id`` (detail)
      - ``PATCH /api/connections/:id`` (after edit)
      - ``PATCH /api/connections/:id/status`` (after status change)
      - ``DELETE /api/connections/:id`` (after soft delete)

    The ``deleted_at`` column on ``records`` is intentionally NOT
    surfaced in this outbound schema (per QA Issue 8). It is a server-
    internal soft-delete marker; default-scoped reads inject
    ``WHERE deleted_at IS NULL`` so a non-admin caller would see only
    ``null`` values anyway, and exposing the field could mislead
    integrators into believing it is part of the public contract. The
    soft-delete admin moderation view in :mod:`app.api.admin` uses a
    separate response shape (``ConnectionAdminRead``) which retains
    ``deleted_at`` because admins legitimately need to distinguish
    active from soft-deleted rows. The audit-trail snapshots produced
    by :func:`app.services.connections._record_to_audit_payload`
    continue to capture ``deleted_at`` because the audit invariant
    requires the full pre/post state of the row, NOT the public API
    contract.

    Field provenance:
        id                         Server-generated UUID v4.
        full_name, linkedin_url,
        company, job_title,
        relationship_context,
        ai_notes                   Set by contributor (F-001).
        involvement                Set by contributor (F-003).
        outreach_status            Default 'Not Started' (F-005);
                                   mutable only by Sales Rep / Admin.
        submission_date            Server-set on creation; immutable.
        owner_user_id              Derived from g.session at creation;
                                   immutable (F-006).
        owner_display_name         Denormalized snapshot of the
                                   submitter's display name AT
                                   submission time (per AAP Section
                                   0.7.6: "owner_display_name is
                                   denormalized for fast feed
                                   rendering"). Stays stable even if
                                   the user later renames.
        normalized_linkedin_url    Set by service layer via
                                   ``app.utils.url.normalize_linkedin_url``;
                                   used for the F-010 unique partial
                                   index. Surfaced in responses so the
                                   SPA can stably link to the same
                                   record across URL variations.
        tags                       Embedded ``TagRead`` objects (F-008).
                                   Empty list when no tags applied.
                                   Eager-loaded via SQLAlchemy
                                   ``selectinload`` to avoid N+1.
        created_at, updated_at     Server timestamps; surfaced for the
                                   SPA's "last edited" hint.
    """

    model_config = _OUTBOUND_CONFIG

    id: UUID
    full_name: str
    linkedin_url: str
    normalized_linkedin_url: str
    company: str
    job_title: str
    relationship_context: str
    ai_notes: str | None = None
    involvement: InvolvementType
    outreach_status: OutreachStatus
    submission_date: AwareDatetime
    owner_user_id: UUID
    owner_display_name: str
    tags: list[TagRead] = Field(default_factory=list)
    created_at: AwareDatetime
    updated_at: AwareDatetime
    # NOTE: ``deleted_at`` intentionally NOT exposed (QA Issue 8).
    # Use ``ConnectionAdminRead`` for the admin moderation surface
    # which legitimately needs the soft-delete timestamp.


# ---------------------------------------------------------------------------
# Outbound: ConnectionAdminRead (admin moderation only)
# ---------------------------------------------------------------------------


class ConnectionAdminRead(ConnectionRead):
    """Outbound shape for admin moderation views.

    Extends :class:`ConnectionRead` with the server-internal
    ``deleted_at`` soft-delete timestamp. Returned ONLY by the
    admin moderation endpoints
    (:func:`app.api.admin.list_records` and friends) where the
    operator legitimately needs to distinguish active rows from
    soft-deleted ones.

    Per QA Issue 8 the standard :class:`ConnectionRead` shape
    (used by ``GET /api/connections``, ``GET /api/connections/:id``,
    etc.) intentionally omits ``deleted_at`` because non-admin
    callers always receive ``null`` for the field (default queries
    inject ``WHERE deleted_at IS NULL``) and exposing the field
    confused integrators about whether it was a public-contract
    field. The admin surface gets its own schema so the
    information-disclosure boundary is explicit and reviewable.

    Field provenance:
        deleted_at  NULL for active records; populated ISO-8601
                    UTC timestamp for soft-deleted records. Set
                    by :func:`app.services.connections.soft_delete_record`
                    and never mutated thereafter (a hard delete
                    removes the row entirely; a re-create writes
                    a NEW row with a fresh ``id``).
        (all other fields inherited from :class:`ConnectionRead`)
    """

    model_config = _OUTBOUND_CONFIG

    deleted_at: AwareDatetime | None = None


# ---------------------------------------------------------------------------
# Outbound: PaginatedConnections
# ---------------------------------------------------------------------------


class PaginatedConnections(BaseModel):
    """Pagination envelope for ``GET /api/connections`` (F-004).

    Uniform pagination shape per the assigned folder Conventions::

        {
            "items": [ConnectionRead, ...],
            "total": int,  # total count matching the filters
            "limit": int,  # page size echoed back from the request
            "offset": int,  # offset echoed back from the request
        }

    The SPA uses ``total`` for "Showing 50 of 1234" labels and for
    computing whether to render a "Next page" button.

    Bounds:
        items   List of ``ConnectionRead`` (may be empty).
        total   ``ge=0`` so a negative count is rejected; the count
                is a non-negative integer per database semantics.
        limit   ``ge=1, le=200`` to defend against pathological page
                sizes; the handler caps the user-requested ``limit``
                to ``_MAX_PAGE_SIZE`` BEFORE constructing this
                envelope. ``ge=1`` rejects zero-size pages which
                would make pagination meaningless.
        offset  ``ge=0`` so a negative offset is rejected.
    """

    model_config = _OUTBOUND_CONFIG

    items: list[ConnectionRead] = Field(default_factory=list)
    total: Annotated[int, Field(ge=0)]
    limit: Annotated[int, Field(ge=1, le=_MAX_PAGE_SIZE)]
    offset: Annotated[int, Field(ge=0)]


# ---------------------------------------------------------------------------
# Outbound: PaginatedAdminConnections (admin moderation list)
# ---------------------------------------------------------------------------


class PaginatedAdminConnections(BaseModel):
    """Pagination envelope for ``GET /api/admin/records`` (F-014).

    Identical shape to :class:`PaginatedConnections` except that
    ``items`` is a list of :class:`ConnectionAdminRead` (which extends
    :class:`ConnectionRead` with the server-internal ``deleted_at``
    soft-delete timestamp). The admin moderation surface needs
    ``deleted_at`` so operators can distinguish active rows from
    soft-deleted rows in the same listing; non-admin endpoints use
    :class:`PaginatedConnections` and never expose ``deleted_at``
    (per QA Issue 8).

    Bounds are identical to :class:`PaginatedConnections`; see that
    class for documentation.
    """

    model_config = _OUTBOUND_CONFIG

    items: list[ConnectionAdminRead] = Field(default_factory=list)
    total: Annotated[int, Field(ge=0)]
    limit: Annotated[int, Field(ge=1, le=_MAX_PAGE_SIZE)]
    offset: Annotated[int, Field(ge=0)]


# ---------------------------------------------------------------------------
# Outbound: ConnectionDuplicateCheckResponse (F-010)
# ---------------------------------------------------------------------------


class ConnectionDuplicateCheckResponse(BaseModel):
    """Response for ``GET /api/connections/duplicate-check?linkedin_url=...`` (F-010).

    Returns whether the supplied URL (after server-side normalization)
    already exists in the contributor's organization. The response is
    INFORMATIONAL only: per AAP Section 0.7.6 (Business Rules):

    > "Duplicate detection is a warning, not a block. The user is
    > informed but may proceed."

    The handler runs the input through
    ``app.utils.url.normalize_linkedin_url`` to compute the canonical
    form, then queries the unique partial index
    ``(org_id, normalized_linkedin_url) WHERE deleted_at IS NULL``.
    The lookup runs in sub-second time at the 10K-record scale ceiling
    per AAP Section 0.7.3.

    Fields:
        duplicate_found             ``True`` iff a non-soft-deleted
                                    record with the same normalized
                                    URL exists in the contributor's
                                    org.
        existing_record_id          The UUID of the duplicate record,
                                    or ``None`` when no duplicate.
        existing_owner_display_name Denormalized owner of the duplicate
                                    (e.g., "Jane Doe") so the SPA can
                                    render "This contact was already
                                    submitted by Jane Doe on ...".
        existing_submission_date    The ``submission_date`` of the
                                    duplicate, ISO-8601 UTC. Surfaced
                                    for the same warning UI.
        normalized_linkedin_url     The result of running the input
                                    through ``normalize_linkedin_url``
                                    for transparency / debugging. Always
                                    populated (even when no duplicate
                                    exists) so the SPA can display the
                                    canonical form back to the user.
    """

    model_config = _OUTBOUND_CONFIG

    duplicate_found: bool
    existing_record_id: UUID | None = None
    existing_owner_display_name: str | None = None
    existing_submission_date: AwareDatetime | None = None
    normalized_linkedin_url: str


# ---------------------------------------------------------------------------
# Outbound: ConnectionHistoryEntry (F-011, F-013)
# ---------------------------------------------------------------------------


class ConnectionHistoryEntry(BaseModel):
    """Per-row shape for ``GET /api/connections/:id/history`` (F-011).

    Each entry corresponds to one row in the ``audit_events`` table
    that targets the requested record. The history endpoint returns
    a list of these entries sorted by ``event_timestamp DESC`` so the
    most recent events appear first.

    Fields:
        id                  UUID of the audit event row.
        event_type          One of the eight ``AuditEventType``
                            values relevant to a record:
                            ``create``, ``status_change``, ``edit``,
                            ``soft_delete``, ``hard_delete``,
                            ``admin_op``.
                            (``role_change`` and ``authentication``
                            never have a ``target_record_id``; they
                            are filtered out at the query level.
                            The schema does not enforce this filter -
                            it is defensive about what the database
                            COULD return, not prescriptive about
                            what the query DOES return.)
        event_timestamp     ISO-8601 UTC timestamp of the event.
        actor_user_id       UUID of the user who performed the action.
        actor_display_name  Joined from the users table for fast
                            rendering. May be NULL for system events
                            (none in MVP) or for users whose record
                            was scrubbed (none in MVP).
        before_payload      JSON snapshot of the relevant fields BEFORE
                            the event, or ``None`` for ``create``
                            events. Shape varies by ``event_type``:

                                - status_change:
                                  ``{"outreach_status": "..."}``
                                - edit:
                                  ``{"<field>": <prior value>, ...}``
                                - soft_delete / hard_delete: full record
                                - create: NULL
                                - admin_op: implementation-defined
        after_payload       JSON snapshot AFTER the event, or ``None``
                            for ``hard_delete``. Shape mirrors
                            ``before_payload``.

    Why ``dict[str, Any]`` payload typing:
        The JSONB column accepts arbitrary nested JSON; we don't
        enforce a schema on the audit payload because the shape
        varies per event type. The SPA's ``EditHistoryFeed.tsx``
        component does runtime branching on ``event_type`` to render
        the right UI (key-value diff for ``edit``; status-arrow
        widget for ``status_change``; full-record block for
        soft/hard deletes).
    """

    model_config = _OUTBOUND_CONFIG

    id: UUID
    event_type: AuditEventType
    event_timestamp: AwareDatetime
    actor_user_id: UUID
    actor_display_name: str | None = None
    before_payload: dict[str, Any] | None = None
    after_payload: dict[str, Any] | None = None
