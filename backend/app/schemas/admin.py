"""Pydantic 2.x admin panel schemas (F-009, F-014).

Schemas defined here:

- ``UserRead``                 Outbound shape for a User row.
                               Used by GET /api/admin/users (admin),
                               by LoginResponse (auth.py), and by
                               SessionRead (auth.py).
                               NEVER includes password_hash.
- ``UserRoleUpdate``           Inbound payload for
                               PATCH /api/admin/users/:id (F-009 +
                               F-014). Validates the three-valued
                               UserRole enum.
- ``ContributorActivity``      One row of the "most active contributors"
                               analytics panel (F-014).
- ``LeadsByStatusEntry``       One row of the "leads by status"
                               analytics panel (F-014).
- ``WeeklyActivityEntry``      One row of the "weekly activity"
                               sparkline analytics panel (F-014).
- ``AnalyticsResponse``        Composite response shape for
                               GET /api/admin/analytics aggregating
                               the three panels above.

Per AAP Section 0.7.4 (Security Invariants), ``UserRead`` MUST NOT
expose ``password_hash``. The bcrypt hash is server-only.

Per AAP Section 0.5.2 (Layer 6), all admin endpoints are
``@requires_role('Admin')`` gated. Schema validation is the second
layer of defense; the RBAC decorator is the first.

Per AAP Section 0.5.3, schemas mirror Zod schemas in
``frontend/src/schemas/admin.ts`` field-for-field.

Note on imports: pydantic v2 evaluates field type hints at
class-definition time (via ``get_type_hints``) to wire up its
validators, so ``AwareDatetime`` and ``EmailStr`` MUST be present at
runtime even though they appear only in type annotations. The
``# noqa: TC002`` suppression on the pydantic import line documents
this constraint and prevents ruff's flake8-type-checking strict mode
from moving the import into a ``TYPE_CHECKING`` block (which would
break the schemas at first use).

The ``UUID`` and ``date`` imports are likewise evaluated at runtime
by pydantic's schema construction, so they are also kept out of any
``TYPE_CHECKING`` block.
"""

from __future__ import annotations

from datetime import date  # noqa: TC003  (used in pydantic type annotations at runtime)
from typing import Annotated
from uuid import UUID  # noqa: TC003  (used in pydantic type annotations at runtime)

from pydantic import (  # noqa: TC002  (pydantic resolves these at class-construction time)
    AwareDatetime,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
)

from app.models.enums import OutreachStatus, UserRole  # noqa: TC001

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# Field length caps. Match the underlying VARCHAR column lengths in
# ``app.models.user.User`` so that pydantic rejection happens BEFORE
# the database would otherwise raise a DataError.
_EMAIL_MAX_CHARS: int = 320  # RFC 5321 maximum
_DISPLAY_NAME_MAX_CHARS: int = 255

# Analytics panel size caps. The handler constructs these lists at
# query time; the cap here is a sanity bound, not a query parameter.
_MAX_CONTRIBUTORS_RANK: int = 50
_MAX_WEEKLY_ENTRIES: int = 52  # 1 year of weeks

# Per the assigned folder Conventions: every inbound schema rejects
# unexpected keys (defends against role-escalation tampering) and
# strips whitespace.
_STRICT_CONFIG: ConfigDict = ConfigDict(
    extra="forbid",
    str_strip_whitespace=True,
)

# Outbound config enables ORM-mode serialization. Pydantic's
# ``from_attributes=True`` mode reads ONLY the fields declared on the
# schema; extra ORM attributes (e.g. ``password_hash``, ``org_id``)
# are silently ignored. This is the cornerstone of the security
# invariant in ``UserRead``.
_OUTBOUND_CONFIG: ConfigDict = ConfigDict(
    from_attributes=True,
)


# ---------------------------------------------------------------------------
# Public exports
# ---------------------------------------------------------------------------

__all__ = [
    "AnalyticsResponse",
    "ContributorActivity",
    "LeadsByStatusEntry",
    "UserRead",
    "UserRoleUpdate",
    "WeeklyActivityEntry",
]


# ---------------------------------------------------------------------------
# Outbound: UserRead
# ---------------------------------------------------------------------------


class UserRead(BaseModel):
    """Outbound shape for a User record.

    Returned by:

    - ``GET /api/admin/users`` (list, Admin role only).
    - ``PATCH /api/admin/users/:id`` (after role mutation).
    - Embedded in ``LoginResponse.user`` and ``SessionRead.user``.
    - ``GET /api/me`` (current session user).

    CRITICAL SECURITY: This schema does NOT expose ``password_hash``.
    Per AAP Section 0.7.4: "Passwords stored as bcrypt salted hashes...
    never logged." The hash is also never transmitted to any client,
    even an admin client. Admins reset passwords via a future
    password-reset flow that mints a one-time token; they do not see
    hash material.

    ``org_id`` is also intentionally excluded -- it is a server-only
    multi-tenant scoping concept and exposing it would leak internal
    addressing detail to the client.

    Fields:
        id            UUID v4 primary key.
        email         RFC 5322 email; unique within the org.
        display_name  User-facing name (denormalized into
                      ``records.owner_display_name`` at submission
                      time per AAP Section 0.7.6).
        role          One of ``Admin``, ``Contributor``, ``Viewer``
                      per F-009.
        created_at    Server timestamp (immutable, timezone-aware UTC).

    Why ``from_attributes=True``:
        Pydantic's ORM-mode lets the handler call
        ``UserRead.model_validate(user_orm_instance)`` directly on a
        SQLAlchemy ``User`` row. Pydantic reads only the five fields
        declared above; even if the ORM instance carries
        ``password_hash`` and ``org_id`` attributes, those are NOT
        serialized in ``model_dump()`` output. This is defense in
        depth: the schema declaration itself prevents leakage.
    """

    model_config = _OUTBOUND_CONFIG

    id: UUID
    email: EmailStr
    display_name: Annotated[
        str,
        Field(
            min_length=1,
            max_length=_DISPLAY_NAME_MAX_CHARS,
            description=(
                "Display name shown in the SPA next to records owned by "
                "this user. Denormalized into "
                "``records.owner_display_name`` at submission time."
            ),
        ),
    ]
    role: UserRole
    created_at: AwareDatetime


# ---------------------------------------------------------------------------
# Inbound: UserRoleUpdate
# ---------------------------------------------------------------------------


class UserRoleUpdate(BaseModel):
    """Inbound payload for ``PATCH /api/admin/users/:id`` (F-009 + F-014).

    The endpoint is RBAC-gated to Admin role via
    ``@requires_role('Admin')`` from ``app.middleware.rbac``. The
    handler emits an audit event of type ``role_change`` capturing
    the prior and new role in ``before_payload`` / ``after_payload``.

    Per AAP Section 0.7.4 (Security Invariants), this schema's
    ``extra='forbid'`` rejects any other field (e.g., a malicious
    attempt to also change ``email`` or ``id``) so role mutation is
    strictly limited to the role field. The path parameter ``:id``
    identifies the target user; the body carries only the new role.

    Anti-tampering rationale:
        A compromised admin session could attempt to send
        ``{"role": "Admin", "email": "newvictim@example.com"}`` to
        mutate fields beyond role. Strict mode (extra='forbid')
        rejects that with HTTP 422 before any handler logic runs.
        Similarly ``{"role": "Admin", "id": "<other_user_uuid>"}``
        cannot retarget the operation because ``id`` is not declared
        on this schema and is therefore rejected.
    """

    model_config = _STRICT_CONFIG

    role: UserRole


# ---------------------------------------------------------------------------
# Outbound: ContributorActivity
# ---------------------------------------------------------------------------


class ContributorActivity(BaseModel):
    """One row of the "most active contributors" analytics panel.

    Returned as part of ``AnalyticsResponse.most_active_contributors``.
    The list is ordered by ``record_count DESC`` and capped at
    ``_MAX_CONTRIBUTORS_RANK`` rows by the service layer.

    Fields:
        user_id       UUID of the contributor.
        display_name  Display name at the time of aggregation. Note:
                      this is denormalized from
                      ``users.display_name`` (NOT from
                      ``records.owner_display_name``) so that a
                      recent rename surfaces in the analytics panel.
        record_count  Number of NON-soft-deleted records owned by
                      this user in the contributor's organization.
                      Filtered by ``deleted_at IS NULL``.
    """

    model_config = _OUTBOUND_CONFIG

    user_id: UUID
    display_name: Annotated[
        str,
        Field(
            min_length=1,
            max_length=_DISPLAY_NAME_MAX_CHARS,
            description=(
                "Contributor display name at aggregation time. Sourced from "
                "``users.display_name`` so renames are reflected on the next "
                "analytics refresh."
            ),
        ),
    ]
    record_count: Annotated[
        int,
        Field(
            ge=0,
            description=(
                "Count of active (non-soft-deleted) records owned by this "
                "contributor in their organization."
            ),
        ),
    ]


# ---------------------------------------------------------------------------
# Outbound: LeadsByStatusEntry
# ---------------------------------------------------------------------------


class LeadsByStatusEntry(BaseModel):
    """One row of the "leads by status" analytics panel.

    Returned as part of ``AnalyticsResponse.leads_by_status``. The
    list contains exactly four entries (one per OutreachStatus value)
    so the SPA can render a complete bar/pie chart even when a
    status has zero leads.

    Fields:
        status  One of the four OutreachStatus values
                (Not Started, In Progress, Contacted, Closed).
        count   Number of NON-soft-deleted records in the
                contributor's organization with this status.
                Filtered by ``deleted_at IS NULL``.

    Why use the enum (not string):
        The SPA renders status badges with status-specific colors. Using
        the enum ensures the SPA's TypeScript types match the server
        response and that an unrecognized status surfaces as a
        validation error rather than rendering an "unknown" badge.

    Why no enforced count of entries:
        The service layer is responsible for emitting all four statuses
        (with count=0 placeholders where applicable). This schema does
        NOT enforce the entry count because the response shape is a
        list-of-rows, not a fixed-length tuple.
    """

    model_config = _OUTBOUND_CONFIG

    status: OutreachStatus
    count: Annotated[
        int,
        Field(
            ge=0,
            description=(
                "Count of active (non-soft-deleted) records in the "
                "organization with this outreach status."
            ),
        ),
    ]


# ---------------------------------------------------------------------------
# Outbound: WeeklyActivityEntry
# ---------------------------------------------------------------------------


class WeeklyActivityEntry(BaseModel):
    """One row of the weekly activity sparkline panel.

    The sparkline shows record-creation activity over the trailing
    N weeks (12 weeks default per the SPA's Analytics view, capped at
    ``_MAX_WEEKLY_ENTRIES`` = 52 by the service layer).

    Fields:
        week_start    ISO date of the Monday that begins the week
                      (UTC). The handler bins records by
                      ``DATE_TRUNC('week', submission_date)`` in SQL.
        record_count  Number of records CREATED (regardless of
                      soft-delete status) during this week. We
                      include soft-deleted records here because the
                      sparkline measures contributor *activity*, not
                      active inventory.

    Why ``date`` (not ``datetime``):
        The bin is a calendar week; storing a time component would
        be misleading. Pydantic 2.x handles ``date`` natively and
        serializes to "YYYY-MM-DD".

    Why include soft-deleted records:
        Other panels (``ContributorActivity``, ``LeadsByStatusEntry``)
        measure active inventory and therefore filter to active
        records. The sparkline measures activity (a record submitted
        then later removed STILL counts as a contribution), so it does
        NOT filter on ``deleted_at``. This subtle difference is
        documented per-panel; tests verify each panel's filtering
        convention.
    """

    model_config = _OUTBOUND_CONFIG

    week_start: date
    record_count: Annotated[
        int,
        Field(
            ge=0,
            description=(
                "Number of records created during this calendar week, "
                "INCLUDING those later soft-deleted. The sparkline "
                "measures activity, not active inventory."
            ),
        ),
    ]


# ---------------------------------------------------------------------------
# Outbound: AnalyticsResponse
# ---------------------------------------------------------------------------


class AnalyticsResponse(BaseModel):
    """Composite response for ``GET /api/admin/analytics`` (F-014).

    Aggregates three panels in a single round-trip so the SPA's
    Analytics view renders without a waterfall of three sequential
    requests::

        {
            "most_active_contributors": [ContributorActivity, ...],
            "leads_by_status": [LeadsByStatusEntry, ...],
            "weekly_activity": [WeeklyActivityEntry, ...],
            "generated_at": "2026-05-01T12:00:00Z",
        }

    Per AAP Section 0.5.2 Layer 6, the handler is
    ``@requires_role('Admin')`` gated. The aggregation queries hit
    the same indexes as the F-004 feed (composite
    ``(org_id, deleted_at, submission_date DESC)``) so they remain
    responsive at the 10K-record scale ceiling per AAP Section 0.7.3.

    Fields:
        most_active_contributors  List of contributors ordered by
                                  ``record_count DESC``, capped at 50.
                                  Defaults to empty list for new orgs.
        leads_by_status           One entry per OutreachStatus value
                                  (4 entries total). Defaults to empty
                                  list for new orgs.
        weekly_activity           List of WeeklyActivityEntry rows for
                                  the trailing N weeks (default 12,
                                  max 52). Defaults to empty list for
                                  new orgs.
        generated_at              Timezone-aware UTC timestamp recorded
                                  when the aggregation snapshot was
                                  taken. Lets the SPA show "Refreshed
                                  N minutes ago" hints AND lets the
                                  handler add ``Cache-Control:
                                  max-age=60`` headers without
                                  ambiguity.

    Why three separate row schemas instead of one polymorphic shape:
        Each panel has a distinct field signature (UUID + display_name
        + count vs. status + count vs. date + count). Forcing one
        shape would require optional fields and runtime branching,
        which would be more error-prone than three small schemas.
    """

    model_config = _OUTBOUND_CONFIG

    most_active_contributors: list[ContributorActivity] = Field(default_factory=list)
    leads_by_status: list[LeadsByStatusEntry] = Field(default_factory=list)
    weekly_activity: list[WeeklyActivityEntry] = Field(default_factory=list)
    generated_at: AwareDatetime
