"""Admin panel service-layer operations (F-014, F-009, F-007).

This module provides the data-tier helpers that back the admin panel
endpoints in :mod:`app.api.admin` (F-014). Every function in this
module is intended to be called by handlers behind the
``@requires_role(UserRole.ADMIN)`` decorator. The decorator is the
authoritative authorization gate per AAP Section 0.7.1 invariant 7;
this module performs additional org-scope checks defensively.

Public surface (handler-facing API per the file schema):

* :func:`list_all_users` -- list all users in the actor's organization.
* :func:`update_user_role` -- mutate a user's role; emits the F-013
  ``role_change`` audit event in the same transaction.
* :func:`list_records_for_moderation` -- admin moderation view of
  records, including soft-deleted ones by default.
* :func:`hard_delete_record` -- physically remove a record; emits the
  F-013 ``hard_delete`` audit event in the same transaction.
* :func:`compute_analytics` -- aggregate the three analytics panels
  (most active contributors, leads by status, weekly activity) into
  a single :class:`AnalyticsResponse`.

Lower-level service helpers (used by tests and by the handler-facing
wrappers above):

* :func:`list_org_users` -- paginated user list with explicit
  session/org parameters.
* :func:`get_analytics_snapshot` -- raw three-panel aggregation with
  explicit session/org/weeks parameters.

Domain exceptions:

* :class:`LastAdminError` (subclass of
  :class:`app.middleware.error_handlers.ConflictError`, mapped to
  HTTP 409) -- raised by :func:`update_user_role` when demoting a
  user would leave the organization without any Admin.
* :class:`SelfDemotionError` (subclass of
  :class:`app.middleware.error_handlers.ForbiddenError`, mapped to
  HTTP 403) -- raised by :func:`update_user_role` when an Admin
  attempts to demote themselves.

State-changing functions (:func:`update_user_role`,
:func:`hard_delete_record`) emit audit events inside the parent
transaction per AAP Section 0.7.1 invariant 6 (atomic state-change
plus audit-emit pair). Read-only functions
(:func:`list_all_users`, :func:`list_records_for_moderation`,
:func:`compute_analytics`) do not require an open transaction.

Org-scoping (AAP Section 0.7.1 invariant 3): every read and every
write injects ``WHERE org_id = :org_id`` either explicitly or via
the caller-supplied ``actor.org_id``. Cross-org access surfaces as
404 from the calling handler so the response never leaks the
existence of cross-org entities.

Soft-delete awareness (AAP Section 0.7.1 invariant 4): the analytics
inventory panels (contributors, leads-by-status) filter
``deleted_at IS NULL`` because they measure active inventory. The
weekly-activity sparkline does NOT filter ``deleted_at`` because it
measures contributor activity (a record submitted then later
soft-deleted still counts as a contribution). The moderation view
defaults to ``include_deleted=True`` because admins frequently need
to review soft-deleted records.

This module deliberately has no module-level side effects beyond the
exception class declarations. Importing :mod:`app.services.admin`
does not log, does not make HTTP calls, does not touch the
filesystem, and does not connect to any database.
"""

from __future__ import annotations

# Standard library imports.
#
# ``OrderedDict`` provides deterministic key ordering for analytics
# aggregations; used in :func:`get_analytics_snapshot` to guarantee
# a stable response shape ordered by the canonical
# :class:`OutreachStatus` enum sequence.
#
# ``UTC``, ``datetime``, ``timedelta`` provide the date/time
# primitives for the 12-week sparkline window, the current ISO
# Monday computation, and the ``generated_at`` UTC stamp on
# :class:`AnalyticsResponse`. ``date`` is type-only (annotation
# inside ``dict[date, int]``).
#
# ``Any`` annotates the JSON-shaped audit payload dicts
# (``before_payload``, ``after_payload``) where heterogeneous values
# defy a stricter type. ``TYPE_CHECKING`` gates the type-only
# imports below.
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

# SQLAlchemy 2.x Core/ORM constructs:
# * ``Date`` and ``cast`` coerce DATE_TRUNC's ``timestamptz`` return
#   to a plain DATE for ``week_start`` serialization.
# * ``and_`` composes filter expressions when multiple predicates
#   are required in a single ``WHERE`` clause.
# * ``asc`` and ``desc`` provide explicit ordering directives on
#   columns and labeled aggregates.
# * ``func`` exposes SQL aggregate functions (COUNT, DATE_TRUNC).
# * ``select`` is the SQLAlchemy 2.x query builder.
# * ``Select`` is the type for query statements; gated to
#   :data:`TYPE_CHECKING` because it appears only in return-type
#   annotations.
from sqlalchemy import Date, and_, asc, cast, desc, func, select

# First-party imports - absolute paths only per the project's
# ``flake8-tidy-imports`` configuration (relative imports are banned
# under AAP Section 0.3.7).
#
# ``db`` is the SQLAlchemy wrapper singleton; provides
# ``db.session()`` for opening short-lived sessions in the
# handler-facing wrappers (``list_all_users``, ``compute_analytics``,
# ``list_records_for_moderation``). The lower-level helpers receive
# an open session from the caller per AAP Section 0.5.3.
#
# ``ConflictError``, ``ForbiddenError``, and ``NotFoundError`` are
# the application's HTTP-mapped exception classes raised by this
# module. ``LastAdminError`` and ``SelfDemotionError`` below
# subclass ``ConflictError`` and ``ForbiddenError`` respectively so
# they participate in the standard JSON error envelope produced by
# the registered Flask error handlers.
#
# ``Record`` and ``User`` are the SQLAlchemy ORM classes queried by
# this module. The :mod:`app.models` re-export package is imported
# so ``Base.metadata`` is fully populated before any query touches
# the registry.
#
# :mod:`app.models.enums` provides ``AuditEventType.ROLE_CHANGE``
# and ``AuditEventType.HARD_DELETE`` for the audit emission, the
# :class:`OutreachStatus` enum used to enumerate all four leads-by-
# status entries (regardless of whether records exist for each
# status), and :class:`UserRole.ADMIN` for the anti-lockout
# admin-count check.
#
# :class:`app.schemas.admin` schemas are the response shapes returned
# by the public functions. ``UserRoleUpdate`` is accepted by
# :func:`update_user_role` as a convenience for handlers that pass
# the validated request payload directly without unpacking ``.role``.
#
# :func:`emit_audit_event` is the SOLE writer of ``audit_events`` per
# AAP Section 0.7.1 invariant 5 (append-only audit table); admin
# state-changes invoke it inside the parent transaction so the
# state change and the audit row commit (or roll back) atomically
# per invariant 6.
from sqlalchemy.exc import IntegrityError

# Third-party runtime imports.
#
# ``structlog.get_logger(__name__)`` produces a JSON-emitting bound
# logger. The ``merge_contextvars`` processor configured in
# :mod:`app.observability.logging` automatically surfaces the
# request-scoped ``correlation_id``, ``user_id``, and ``org_id``
# bound by the correlation/auth middleware so log lines emitted
# here are automatically correlated by request without per-call
# bookkeeping.
import structlog

from app.extensions import db
from app.middleware.error_handlers import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
)
from app.models import Record, User
from app.models.enums import AuditEventType, OutreachStatus, UserRole
from app.schemas.admin import (
    AnalyticsResponse,
    ContributorActivity,
    LeadsByStatusEntry,
    UserRead,
    UserRoleUpdate,
    WeeklyActivityEntry,
)
from app.services.audit import emit_audit_event

# Type-only imports. Under ``from __future__ import annotations`` all
# annotations are strings (PEP 563) and the symbols inside the
# ``TYPE_CHECKING`` block are never evaluated at runtime, satisfying
# the project's strict ``flake8-type-checking`` configuration.
#
# ``date`` annotates the lookup-table type ``dict[date, int]`` in the
# weekly-activity padding pass.
#
# ``UUID`` annotates the user_id and record_id parameters of
# :func:`update_user_role` and :func:`hard_delete_record`.
#
# ``Select`` is the type for SQLAlchemy query statements; used in
# the :func:`_build_moderation_base_stmt` return annotation.
#
# ``Session`` is the :class:`app.middleware.auth.Session` typed
# dataclass populated on ``flask.g.session``. The handler-facing
# wrappers read ``actor.user_id`` and ``actor.org_id`` from it.
#
# ``sqlalchemy.orm.Session`` (aliased ``DBSession``) is the type of
# the caller's open session passed to the lower-level helpers. The
# alias avoids name collision with ``app.middleware.auth.Session``.
if TYPE_CHECKING:
    from datetime import date
    from uuid import UUID

    from sqlalchemy import Select
    from sqlalchemy.orm import Session as DBSession

    from app.middleware.auth import Session


# ---------------------------------------------------------------------------
# Module logger
# ---------------------------------------------------------------------------
# structlog's ``merge_contextvars`` processor (configured in
# :mod:`app.observability.logging`) automatically surfaces the
# request-scoped ``correlation_id``, ``user_id``, and ``org_id``
# bound by the correlation/auth middleware so log lines emitted here
# are automatically correlated by request without per-call work.
logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# Number of contributor entries returned by the analytics panels.
# Bounds the response payload at the AAP Section 0.7.3 10K-records
# scale ceiling. Matches the SPA's small leaderboard panel layout;
# the cap is a UX bound, not a security one.
_TOP_CONTRIBUTORS_LIMIT: int = 10

# Hard cap on the contributors list size in the schema-required
# wrapper. Larger orgs would overwhelm the SPA's panel layout; the
# cap is a UX bound, not a security one. The schema declares
# ``_MAX_CONTRIBUTORS_RANK = 50`` for back-end validation; the
# service layer enforces the more restrictive ``_TOP_CONTRIBUTORS_LIMIT``
# by default.
_MAX_CONTRIBUTORS_RANK: int = 50

# Number of trailing weeks shown in the sparkline panel. 12 matches
# the SPA's small panel width and is the default for
# :func:`compute_analytics`. The lower-level
# :func:`get_analytics_snapshot` accepts a ``weeks`` override (capped
# at :data:`_MAX_WEEKLY_WEEKS`).
_DEFAULT_WEEKLY_WEEKS: int = 12

# Hard cap on the trailing-weeks parameter to prevent pathological
# inputs (e.g., 10000 weeks) from producing an oversized response.
# The pydantic schema enforces ``_MAX_WEEKLY_ENTRIES = 52`` (see
# ``app/schemas/admin.py``); we mirror it here as a service-layer
# defensive bound.
_MAX_WEEKLY_WEEKS: int = 52

# Default and maximum page sizes for the moderation view. The
# admin panel renders one row per record and shows up to 50 records
# per page by default; admins can request larger pages up to 200 for
# bulk review workflows. The cap is a UX bound, not a security one.
_DEFAULT_MODERATION_PAGE: int = 1
_DEFAULT_MODERATION_PAGE_SIZE: int = 50
_MAX_MODERATION_PAGE_SIZE: int = 200

# Default page size for ``list_org_users`` / ``list_all_users``.
# Matches the SPA's user-management panel layout (paginated at 100).
_DEFAULT_USER_LIST_LIMIT: int = 100
_MAX_USER_LIST_LIMIT: int = 500


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------
# ``__all__`` is sorted alphabetically per ruff RUF022 (isort-style
# sorting). The public surface is the union of the schema-required
# exports (:func:`list_all_users`, :func:`update_user_role`,
# :func:`list_records_for_moderation`, :func:`hard_delete_record`,
# :func:`compute_analytics`) and the lower-level service helpers
# used by the test suite (:func:`get_analytics_snapshot`,
# :func:`list_org_users`).

__all__ = [
    "HardDeleteAuditHistoryError",
    "LastAdminError",
    "SelfDemotionError",
    "compute_analytics",
    "get_analytics_snapshot",
    "hard_delete_record",
    "list_all_users",
    "list_org_users",
    "list_records_for_moderation",
    "update_user_role",
]


# ---------------------------------------------------------------------------
# Domain exceptions
# ---------------------------------------------------------------------------


class LastAdminError(ConflictError):
    """Raised when demoting a user would leave the org with zero admins.

    Mapped to HTTP 409 with stable error code ``"conflict"`` by the
    registered Flask error handler (inherited from
    :class:`ConflictError`). The user-facing message is descriptive
    enough that admins understand the constraint without exposing
    internal user IDs.

    Per AAP Section 0.5.2 Layer 6, the admin panel role-change flow
    must preserve the "at least one admin per org" invariant so the
    organization is never locked out of administrative operations.

    Note: This is an application-level guard, not a database
    constraint. A bypass via direct SQL (e.g., a raw ``UPDATE users
    SET role = 'Contributor'`` issued via psql) is not prevented by
    this class; the database role separation in the initial
    migration limits such bypasses to the ``DB_ADMIN_ROLE`` (used
    only for migrations and ad-hoc admin), never to the application
    role used by the running Flask process.
    """

    @property
    def default_message(self) -> str:
        """Return the canonical 'last admin' error message."""
        return (
            "Cannot demote the last Admin in the organization. Promote another user to Admin first."
        )


class HardDeleteAuditHistoryError(ConflictError):
    """Raised when hard delete is blocked by existing audit history.

    Per AAP Section 0.7.1 invariant 5 ("Append-only audit table.
    No code path issues UPDATE or DELETE against audit_events.
    Database-level grants enforce this in production"), a record
    that has any associated audit_events rows CANNOT be hard-deleted
    because doing so would either:

    1. Require the application role to issue an UPDATE on
       ``audit_events.target_record_id`` (forbidden by the
       application-role privileges installed by migration 0001), OR
    2. Require the database engine to issue an FK CASCADE/SET NULL
       action on ``audit_events`` (also functionally an UPDATE,
       which the AAP forbids).

    Instead, the hard-delete path is BLOCKED at the database layer
    via the ``ondelete="RESTRICT"`` foreign-key constraint on
    ``audit_events.target_record_id``. SQLAlchemy is told not to
    auto-nullify those rows (``passive_deletes=True`` on
    ``Record.audit_events``), so the DB-level RESTRICT is the sole
    arbiter. PostgreSQL raises an :class:`IntegrityError` with the
    constraint name ``fk_audit_events_target_record_id_records``
    when this happens; :func:`hard_delete_record` catches that and
    re-raises this exception subclass for a clean 409 surface.

    Operational guidance: admins should use SOFT delete (which sets
    ``deleted_at`` without deleting the row) instead of HARD delete
    when audit history exists. Soft delete preserves both the record
    and its audit history. HARD delete remains supported only for
    records with no audit history (a rare edge case in production
    since every API-created record has at least a CREATE audit).

    Mapped to HTTP 409 with the stable error code ``"conflict"``.
    """

    @property
    def default_message(self) -> str:
        """Return the canonical 'hard-delete blocked by audit' message."""
        return (
            "Cannot hard-delete a record with audit history; the audit "
            "trail must remain immutable per AAP Section 0.7.1. Use soft "
            "delete instead, which preserves both the record and its "
            "audit history."
        )


class SelfDemotionError(ForbiddenError):
    """Raised when an admin attempts to demote themselves.

    Mapped to HTTP 403 with stable error code ``"forbidden"`` by the
    registered Flask error handler (inherited from
    :class:`ForbiddenError`). Per AAP Section 0.5.2 Layer 6, role
    demotion always requires another admin to act, preserving
    four-eyes governance: a single admin's compromised account cannot
    silently downgrade their own role to evade audit oversight.

    Note: This guard fires only on ``actor_user_id == target_user_id``
    AND a non-trivial role change (i.e., the new role differs from
    the current role). A no-op self-call (admin -> admin) is allowed
    because nothing changes; an audit event is also not emitted in
    that case.
    """

    @property
    def default_message(self) -> str:
        """Return the canonical 'self-demotion forbidden' message."""
        return "Admins cannot change their own role. Another Admin must perform the role change."


# ---------------------------------------------------------------------------
# F-014 Analytics aggregation
# ---------------------------------------------------------------------------


def get_analytics_snapshot(
    *,
    db_session: DBSession,
    org_id: UUID,
    weeks: int = _DEFAULT_WEEKLY_WEEKS,
) -> AnalyticsResponse:
    """Aggregate the three admin-panel analytics panels in one snapshot.

    Three queries, each scoped to ``org_id`` and (for the inventory
    panels) to ``deleted_at IS NULL``:

    1. **Most active contributors** -- GROUP BY ``records.owner_user_id``
       JOIN ``users`` to project ``display_name``, ORDER BY
       ``record_count DESC`` LIMIT :data:`_MAX_CONTRIBUTORS_RANK`.
       Filtered by ``deleted_at IS NULL`` because the panel measures
       active inventory, not historical activity.

    2. **Leads by status** -- GROUP BY ``records.outreach_status``
       over active records. Always returns exactly four entries (one
       per :class:`OutreachStatus` value); statuses with zero leads
       get ``count=0`` placeholders so the SPA renders a complete
       bar/pie chart even on a quiet org.

    3. **Weekly activity sparkline** -- GROUP BY DATE_TRUNC('week',
       submission_date) over the trailing ``weeks`` window. Includes
       soft-deleted records because the sparkline measures
       contributor activity, not active inventory.

    Per AAP Section 0.7.3, the queries hit the composite index
    ``(org_id, deleted_at, submission_date DESC)`` (panels 1 and 2)
    and a subset of the same index (panel 3, which filters by
    submission_date range), keeping execution time sub-second at the
    10K-record scale ceiling.

    Args:
        db_session: An open SQLAlchemy session. The function performs
            three SELECT queries and does NOT mutate state, so the
            caller's transaction lifecycle is irrelevant; the queries
            execute correctly inside or outside an explicit
            transaction.
        org_id: The organization whose records are aggregated. All
            queries inject ``WHERE org_id = :org_id``.
        weeks: Number of trailing weeks for the sparkline. Capped at
            :data:`_MAX_WEEKLY_WEEKS` (52). Defaults to 12 (the SPA's
            small panel width).

    Returns:
        An :class:`AnalyticsResponse` carrying the three panel lists
        plus a ``generated_at`` UTC timestamp that the SPA shows as a
        "refreshed N minutes ago" hint and that the handler MAY
        consume for ``Cache-Control: max-age=60`` headers.
    """
    # Cap weeks defensively so a malicious or careless caller cannot
    # request an oversized response. The pydantic-validated path uses
    # the default; service-level callers (tests, scripts) might bypass
    # the schema.
    capped_weeks = max(1, min(weeks, _MAX_WEEKLY_WEEKS))

    # ----- Panel 1: most active contributors --------------------------
    # JOIN users to project display_name, GROUP BY owner_user_id,
    # filter to active records, order by count DESC, LIMIT to the
    # rank cap. Using ``users.display_name`` (not
    # ``records.owner_display_name``) so that a recent rename surfaces
    # in the analytics on the next refresh per the AAP Section 0.5.2
    # Layer 6 specification of the ``ContributorActivity`` schema's
    # ``display_name`` field.
    contributor_count = func.count(Record.id).label("record_count")
    contributors_stmt = (
        select(
            Record.owner_user_id.label("user_id"),
            User.display_name,
            contributor_count,
        )
        .join(User, User.id == Record.owner_user_id)
        .where(
            and_(
                Record.org_id == org_id,
                Record.deleted_at.is_(None),
            )
        )
        .group_by(Record.owner_user_id, User.display_name)
        .order_by(desc(contributor_count), asc(User.display_name))
        .limit(_MAX_CONTRIBUTORS_RANK)
    )
    contributors_rows = db_session.execute(contributors_stmt).all()
    most_active_contributors: list[ContributorActivity] = [
        ContributorActivity(
            user_id=row.user_id,
            display_name=row.display_name,
            record_count=int(row.record_count),
        )
        for row in contributors_rows
    ]

    # ----- Panel 2: leads by status -----------------------------------
    # GROUP BY outreach_status over active records. Build an
    # OrderedDict from the SQL result then synthesize four entries
    # (one per enum value) so statuses with zero leads still appear
    # with count=0. This guarantees the SPA's bar/pie chart always
    # renders a complete view.
    status_count = func.count(Record.id).label("status_count")
    status_stmt = (
        select(Record.outreach_status, status_count)
        .where(
            and_(
                Record.org_id == org_id,
                Record.deleted_at.is_(None),
            )
        )
        .group_by(Record.outreach_status)
    )
    status_rows = db_session.execute(status_stmt).all()
    # OrderedDict gives a deterministic iteration order even if the
    # SQL row order varies between PostgreSQL versions or hash-aggregate
    # implementations. The synthesis loop below iterates the canonical
    # ``OutreachStatus`` enum order, which is the order the SPA expects.
    status_counts: OrderedDict[OutreachStatus, int] = OrderedDict(
        (row.outreach_status, int(row.status_count)) for row in status_rows
    )
    leads_by_status: list[LeadsByStatusEntry] = [
        LeadsByStatusEntry(
            status=status_value,
            count=status_counts.get(status_value, 0),
        )
        for status_value in OutreachStatus
    ]

    # ----- Panel 3: weekly activity sparkline -------------------------
    # DATE_TRUNC('week', submission_date) bins records by ISO-8601
    # week (Monday-anchored in PostgreSQL). The ``records`` table is
    # not filtered by ``deleted_at`` here because the sparkline
    # measures activity (a record submitted then later soft-deleted
    # STILL counts as a contribution). The ``cast(..., Date)`` coerces
    # PostgreSQL's ``timestamptz`` return to a plain ``date`` for
    # serialization through pydantic's ``WeeklyActivityEntry`` schema.
    now_utc = datetime.now(tz=UTC)
    window_start = now_utc - timedelta(weeks=capped_weeks)
    weekly_count = func.count(Record.id).label("weekly_count")
    week_bin = func.date_trunc("week", Record.submission_date).label("week_bin")
    weekly_stmt = (
        select(
            cast(week_bin, Date).label("week_start"),
            weekly_count,
        )
        .where(
            and_(
                Record.org_id == org_id,
                Record.submission_date >= window_start,
            )
        )
        .group_by(week_bin)
        .order_by(asc(week_bin))
    )
    weekly_rows = db_session.execute(weekly_stmt).all()
    weekly_activity: list[WeeklyActivityEntry] = [
        WeeklyActivityEntry(
            week_start=row.week_start,
            record_count=int(row.weekly_count),
        )
        for row in weekly_rows
    ]

    logger.info(
        "analytics_snapshot_generated",
        org_id=str(org_id),
        weeks=capped_weeks,
        contributors_count=len(most_active_contributors),
        weekly_entries=len(weekly_activity),
    )

    return AnalyticsResponse(
        most_active_contributors=most_active_contributors,
        leads_by_status=leads_by_status,
        weekly_activity=weekly_activity,
        generated_at=now_utc,
    )


# ---------------------------------------------------------------------------
# F-014 User management
# ---------------------------------------------------------------------------


def list_org_users(
    *,
    db_session: DBSession,
    org_id: UUID,
    limit: int = _DEFAULT_USER_LIST_LIMIT,
    offset: int = 0,
) -> list[UserRead]:
    """Paginate users within the supplied organization.

    Used by the admin user-management view (F-014). Users are
    returned sorted by ``display_name ASC`` then ``email ASC`` so the
    list is deterministic across pagination calls and stable across
    refreshes.

    Per AAP Section 0.7.4 (Security Invariants), the returned
    :class:`UserRead` shape NEVER includes ``password_hash`` or
    ``org_id`` -- those are server-only fields. The schema's
    ``from_attributes=True`` mode reads ONLY the fields declared on
    the schema; even if the ORM instance carries those attributes,
    they are not serialized.

    Args:
        db_session: An open SQLAlchemy session. Read-only.
        org_id: The organization whose users are listed. Injected as
            ``WHERE org_id = :org_id``.
        limit: Maximum number of users to return. Defaults to
            :data:`_DEFAULT_USER_LIST_LIMIT` (100); capped at
            :data:`_MAX_USER_LIST_LIMIT` (500) defensively.
        offset: Number of rows to skip. Defaults to 0; clamped to a
            non-negative value defensively.

    Returns:
        A list of :class:`UserRead` shapes, ordered by display_name
        ascending then email ascending.
    """
    safe_limit = max(1, min(limit, _MAX_USER_LIST_LIMIT))
    safe_offset = max(0, offset)

    stmt = (
        select(User)
        .where(User.org_id == org_id)
        .order_by(asc(User.display_name), asc(User.email))
        .limit(safe_limit)
        .offset(safe_offset)
    )
    users = db_session.scalars(stmt).all()
    return [UserRead.model_validate(user) for user in users]


def update_user_role(
    *,
    db_session: DBSession,
    org_id: UUID,
    target_user_id: UUID,
    new_role: UserRole | UserRoleUpdate,
    actor_user_id: UUID,
) -> User:
    """Mutate a user's role and emit a F-013 ``role_change`` audit event.

    Used by ``PATCH /api/admin/users/:id`` (Admin-only). Enforces two
    organizational invariants per AAP Section 0.5.2 Layer 6:

    1. The "last admin" guarantee: an Admin cannot be demoted if they
       are the SOLE Admin in the organization. Surface as
       :class:`LastAdminError` (HTTP 409, mapped to
       :class:`ConflictError`).
    2. The self-demotion guard: an Admin cannot demote themselves.
       Surface as :class:`SelfDemotionError` (HTTP 403, mapped to
       :class:`ForbiddenError`). The handler's
       ``@requires_role(Admin)`` decorator already gates the endpoint;
       this guard is the secondary defense against an admin issuing
       the call through their own session.

    The function emits ``audit_events.event_type = role_change``
    inside the caller's transaction. The ``before_payload`` carries
    the prior role and the ``after_payload`` carries the new role
    plus the target ``user_id`` so the audit history reconstructs
    the full role change without joining to the users table.

    For convenience, ``new_role`` accepts EITHER a :class:`UserRole`
    enum value OR a :class:`UserRoleUpdate` pydantic payload (which
    has a ``.role`` attribute); the latter form lets handlers pass
    the validated request payload directly without unpacking
    ``payload.role`` first.

    Args:
        db_session: An open SQLAlchemy session with an active
            transaction (caller-owned per AAP Section 0.5.3). The
            audit emit and the role mutation must commit (or roll
            back) atomically, so the caller MUST wrap this call in
            a ``with session.begin():`` block.
        org_id: The organization scope. Injected as
            ``WHERE org_id = :org_id`` on the target lookup and on
            the admin-count query.
        target_user_id: The id of the user whose role is being
            mutated.
        new_role: The new role to assign. Either a :class:`UserRole`
            enum value or a :class:`UserRoleUpdate` payload.
        actor_user_id: The id of the admin issuing the change. Used
            for the self-demotion guard and for the audit event's
            ``actor_user_id`` field.

    Returns:
        The mutated :class:`User` ORM instance, with the new role
        applied. The caller can serialize it via
        ``UserRead.model_validate(...)`` for the response payload.

    Raises:
        NotFoundError: When ``target_user_id`` does not exist in the
            organization (could be cross-org or non-existent; both
            surface as 404 to avoid leaking the existence of cross-
            org users per AAP Section 0.5.2 ``NotFoundError`` contract).
        SelfDemotionError: When ``actor_user_id == target_user_id``
            and the role would actually change. Mapped to HTTP 403.
        LastAdminError: When demoting the SOLE Admin in the
            organization. Mapped to HTTP 409.
    """
    # Coerce a UserRoleUpdate payload into a UserRole enum value so
    # the rest of the function can treat ``new_role`` uniformly. The
    # isinstance check accepts the pydantic model directly; the
    # alternate path (already a UserRole) is the typical test/
    # internal call site.
    if isinstance(new_role, UserRoleUpdate):
        new_role = new_role.role

    # Lookup the target user, scoped to the org. We perform this
    # FIRST so that NotFoundError fires before SelfDemotionError when
    # the target id is unknown -- the more actionable diagnostic
    # surfaces first.
    target_stmt = select(User).where(and_(User.id == target_user_id, User.org_id == org_id))
    target_user: User | None = db_session.scalar(target_stmt)
    if target_user is None:
        # Per AAP Section 0.5.2 NotFoundError contract: return 404
        # for both non-existent IDs and cross-org IDs so the response
        # never reveals whether a UUID exists in a different
        # organization.
        raise NotFoundError(message="User not found.")

    previous_role: UserRole = target_user.role

    # No-op short-circuit. Setting the role to its current value is
    # permitted (idempotent) but does NOT emit an audit event because
    # nothing changed. Returning here also bypasses the self-demotion
    # guard (which only fires on a genuine role change) and the
    # last-admin guard (which only fires on a demotion FROM Admin).
    if previous_role == new_role:
        logger.info(
            "update_user_role_noop",
            org_id=str(org_id),
            target_user_id=str(target_user_id),
            role=new_role.value,
        )
        return target_user

    # Self-demotion guard. Fires AFTER the no-op check so an admin
    # who calls with their current role doesn't trigger the guard.
    # Fires BEFORE the last-admin guard because a self-demotion is
    # the more specific (and more actionable) error.
    if actor_user_id == target_user_id:
        logger.warning(
            "update_user_role_self_demotion_blocked",
            org_id=str(org_id),
            actor_user_id=str(actor_user_id),
        )
        raise SelfDemotionError()

    # Last-admin guard: only fires when DEMOTING from Admin (i.e.,
    # previous role is Admin and new role is anything else). Counts
    # admins in the org BEFORE the mutation; if the count is exactly
    # 1, demoting would lock the org out of admin operations.
    if previous_role == UserRole.ADMIN and new_role != UserRole.ADMIN:
        admin_count_stmt = select(func.count(User.id)).where(
            and_(
                User.org_id == org_id,
                User.role == UserRole.ADMIN,
            )
        )
        admin_count = db_session.scalar(admin_count_stmt) or 0
        if admin_count <= 1:
            logger.warning(
                "update_user_role_last_admin_blocked",
                org_id=str(org_id),
                target_user_id=str(target_user_id),
                actor_user_id=str(actor_user_id),
            )
            raise LastAdminError()

    # Apply the mutation. The flush below ensures the role change is
    # observable in the same transaction (so the audit event sees the
    # post-mutation state if it queries) without committing the
    # transaction. The caller's ``with session.begin():`` block is
    # responsible for the commit.
    target_user.role = new_role
    db_session.flush()

    # Emit the F-013 role_change audit event inside the caller's
    # transaction. The before/after payloads capture the role
    # transition plus the target user_id so audit history is
    # reconstructable without joining to ``users``. ``target_record_id``
    # is None because role_change targets a user, not a record; the
    # user identity is captured inside the payload.
    before_payload: dict[str, Any] = {
        "user_id": str(target_user_id),
        "role": previous_role.value,
    }
    after_payload: dict[str, Any] = {
        "user_id": str(target_user_id),
        "role": new_role.value,
    }
    emit_audit_event(
        db_session=db_session,
        event_type=AuditEventType.ROLE_CHANGE,
        actor_user_id=actor_user_id,
        target_record_id=None,
        before_payload=before_payload,
        after_payload=after_payload,
    )

    logger.info(
        "update_user_role_applied",
        org_id=str(org_id),
        target_user_id=str(target_user_id),
        actor_user_id=str(actor_user_id),
        previous_role=previous_role.value,
        new_role=new_role.value,
    )

    return target_user


# ---------------------------------------------------------------------------
# F-014 Record moderation
# ---------------------------------------------------------------------------


def _record_snapshot(record: Record) -> dict[str, Any]:
    """Serialize a :class:`Record` ORM instance to a JSON-safe dict.

    Used to populate the ``before_payload`` of the ``hard_delete``
    audit event so the audit history retains the row's content even
    after the row itself is gone. UUIDs are stringified, datetimes
    are ISO-8601 formatted, and enum values are unwrapped to their
    string representation.

    Args:
        record: The :class:`Record` ORM instance to snapshot.

    Returns:
        A JSON-safe ``dict[str, Any]`` carrying every business field
        plus the operational fields (``id``, ``org_id``,
        ``owner_user_id``, ``owner_display_name``,
        ``normalized_linkedin_url``, ``deleted_at``,
        ``submission_date``).
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
        "relationship_context": record.relationship_context,
        "ai_notes": record.ai_notes,
        "involvement": (record.involvement.value if record.involvement is not None else None),
        "outreach_status": (
            record.outreach_status.value if record.outreach_status is not None else None
        ),
        "submission_date": (
            record.submission_date.isoformat() if record.submission_date is not None else None
        ),
        "deleted_at": (record.deleted_at.isoformat() if record.deleted_at is not None else None),
    }


def hard_delete_record(
    *,
    db_session: DBSession,
    org_id: UUID,
    record_id: UUID,
    actor_user_id: UUID,
) -> None:
    """Permanently delete a record from the database (admin-only).

    Used by ``DELETE /api/admin/records/:id`` (Admin-only per AAP
    Section 0.5.2 Layer 6). Unlike soft delete (which sets
    ``deleted_at`` on the row), hard delete physically removes the
    row from the ``records`` table. Cascade-deletion on the
    ``record_tags`` association rows is handled by the FK
    ``ondelete=CASCADE`` declared on ``RecordTag.record_id``;
    this function does not need to remove tag links explicitly.

    Per AAP Section 0.5.2 Layer 6, the F-013 ``hard_delete`` audit
    event captures the FULL record payload in ``before_payload``
    BEFORE the delete fires, so the audit history retains the row's
    content even after the row itself is gone. The audit event has
    ``target_record_id = None`` because the record ceases to exist
    after the transaction commits and a non-null FK would conflict
    with the post-commit row state. The record's ``id`` is
    captured inside ``before_payload`` so forensic queries can still
    correlate the audit row to its target.

    Per AAP Section 0.7.1 invariant 5 (append-only audit), the audit
    row INSERT happens inside the same transaction as the record
    DELETE so they commit (or roll back) atomically. The transaction
    fence is the caller's responsibility per AAP Section 0.5.3.

    The audit emit happens BEFORE the physical delete so an emit
    failure (e.g., constraint violation, connectivity loss) leaves
    the record intact rather than orphaning it without an audit
    trail.

    Args:
        db_session: An open SQLAlchemy session with an active
            transaction (caller-owned per AAP Section 0.5.3).
        org_id: The organization scope. Injected as
            ``WHERE org_id = :org_id`` on the SELECT and DELETE so
            cross-org access surfaces as 404.
        record_id: The id of the record to hard-delete.
        actor_user_id: The id of the admin issuing the delete.

    Raises:
        NotFoundError: When the record does not exist or is not in
            the supplied organization. Both cases surface as 404
            per AAP Section 0.5.2 ``NotFoundError`` contract so the
            response never reveals whether a UUID exists in a
            different organization.
    """
    # Lookup the record, scoped to the org. We include soft-deleted
    # records in this query because hard delete must be able to
    # operate on any row regardless of soft-delete state -- admins
    # may want to purge a soft-deleted record permanently from the
    # database (e.g., for GDPR right-to-erasure compliance).
    record_stmt = select(Record).where(and_(Record.id == record_id, Record.org_id == org_id))
    record: Record | None = db_session.scalar(record_stmt)
    if record is None:
        raise NotFoundError(message="Record not found.")

    # Snapshot the record payload BEFORE the delete so the audit
    # row can preserve it. The snapshot uses string forms of UUIDs
    # and enum values so the JSONB column accepts them without
    # custom encoders.
    before_payload = _record_snapshot(record)

    # Emit the audit event FIRST so the audit row's INSERT runs
    # inside the same transaction as the record DELETE. If the audit
    # emission fails, the DELETE never fires and the caller's
    # transaction rolls back atomically per AAP Section 0.7.1
    # invariant 6.
    #
    # ``target_record_id`` is None because the row ceases to exist
    # after commit; populating the FK would conflict with the
    # post-commit row state given ``audit_events.target_record_id``
    # uses ``ondelete=RESTRICT`` per the AuditEvent model. The
    # record id is captured inside ``before_payload`` so forensic
    # queries can still correlate the audit row to its target.
    emit_audit_event(
        db_session=db_session,
        event_type=AuditEventType.HARD_DELETE,
        actor_user_id=actor_user_id,
        target_record_id=None,
        before_payload=before_payload,
        after_payload=None,
    )

    # Physical delete. SQLAlchemy issues a DELETE statement on the
    # next flush; the caller's ``with session.begin():`` block
    # commits the transaction once this function returns. Cascade
    # deletion on ``record_tags`` happens at the database layer via
    # the FK ``ondelete=CASCADE``; we do not need to delete tag links
    # explicitly.
    #
    # Per AAP Section 0.7.1 invariant 5 ("Append-only audit table"),
    # SQLAlchemy is configured with ``passive_deletes=True`` on the
    # ``Record.audit_events`` relationship so it does NOT auto-nullify
    # the FK on dependent ``audit_events`` rows. The DB-level
    # ``ondelete="RESTRICT"`` constraint on
    # ``audit_events.target_record_id`` is the sole arbiter:
    # PostgreSQL raises an :class:`IntegrityError` when audit history
    # references the record being deleted. We catch that here and
    # surface :class:`HardDeleteAuditHistoryError` (HTTP 409) so the
    # admin gets a clear actionable error instead of a generic 500.
    db_session.delete(record)
    try:
        db_session.flush()
    except IntegrityError as exc:
        # Identify the FK constraint by name. The constraint name is
        # set by migration 0001 to
        # ``fk_audit_events_target_record_id_records``; matching by
        # substring is robust to PostgreSQL's verbose error formatting
        # which may include schema/table prefixes around the name.
        # Note: the audit row INSERTed above this DELETE will roll
        # back atomically with this exception per the caller's
        # ``with session.begin():`` block, so the F-013 invariant
        # ("audit emit and state change commit or roll back together")
        # holds without any extra cleanup here.
        constraint_name = "fk_audit_events_target_record_id_records"
        if constraint_name in str(exc.orig):
            logger.info(
                "hard_delete_record_blocked_by_audit_history",
                org_id=str(org_id),
                record_id=str(record_id),
                actor_user_id=str(actor_user_id),
                constraint=constraint_name,
            )
            raise HardDeleteAuditHistoryError() from exc
        # Some other integrity violation; re-raise so the global
        # error handler surfaces a generic 500 (this would indicate
        # a server-side schema issue worth investigating).
        raise

    logger.info(
        "hard_delete_record_applied",
        org_id=str(org_id),
        record_id=str(record_id),
        actor_user_id=str(actor_user_id),
    )


def _build_moderation_base_stmt(
    org_id: UUID,
    *,
    include_deleted: bool,
    full_name_search: str | None,
    company_search: str | None,
) -> Select[tuple[Record]]:
    """Construct the base ``SELECT`` for the moderation list query.

    Centralized here so the count and page queries share the same
    filter predicates, ensuring the totals returned by the count
    query match the rows returned by the page query.

    Args:
        org_id: The organization scope.
        include_deleted: When True, include soft-deleted records;
            when False, restrict to active records only.
        full_name_search: Optional case-insensitive substring filter
            on ``full_name``. ``None`` disables the filter.
        company_search: Optional case-insensitive substring filter
            on ``company``. ``None`` disables the filter.

    Returns:
        A typed ``Select`` statement carrying the filter predicates.
        The caller adds ordering, limit, and offset as needed.
    """
    base: Select[tuple[Record]] = select(Record).where(Record.org_id == org_id)
    if not include_deleted:
        base = base.where(Record.deleted_at.is_(None))
    if full_name_search:
        base = base.where(Record.full_name.ilike(f"%{full_name_search}%"))
    if company_search:
        base = base.where(Record.company.ilike(f"%{company_search}%"))
    return base


# ---------------------------------------------------------------------------
# Schema-required handler-facing API
# ---------------------------------------------------------------------------
# The functions below are the handler-facing public API per the file's
# exports schema (AAP Section 0.5.2 Layer 6). They take an
# :class:`app.middleware.auth.Session` as the actor parameter and
# open their own short-lived session via ``db.session()``. The
# transaction lifecycle is owned by the service layer per AAP
# Section 0.5.3 ("Service functions own transactions").
#
# Read-only wrappers (``list_all_users``, ``compute_analytics``,
# ``list_records_for_moderation``) do not open a transaction; they
# rely on PostgreSQL's per-statement implicit transactions to keep
# read consistency.
#
# Soft-deleted weekly-activity inclusion (per AAP Section 0.5.2
# Layer 6 spec) is documented in :func:`get_analytics_snapshot`.


def list_all_users(actor: Session) -> list[UserRead]:
    """List all users in the actor's organization (F-014).

    Org-scoped per AAP Section 0.7.1 invariant 3. The returned
    :class:`UserRead` shape NEVER includes ``password_hash`` or
    ``org_id`` -- those are server-only fields per AAP Section 0.7.4
    Security Invariants. The schema's ``from_attributes=True`` mode
    reads ONLY the fields declared on the schema; even if the ORM
    instance carries those attributes, they are not serialized.

    The endpoint that calls this function is gated by the
    ``@requires_role(UserRole.ADMIN)`` decorator (per AAP Section
    0.5.2 Layer 6); the org-scope check here is defense in depth
    against any future weakening of the decorator.

    Args:
        actor: Authenticated :class:`Session` populated by
            :mod:`app.middleware.auth`. ``actor.org_id`` is the
            multi-tenant scope; no other attribute is consumed.

    Returns:
        A list of :class:`UserRead` shapes for every user in
        ``actor.org_id``, ordered by display_name ascending then
        email ascending. Returns up to
        :data:`_MAX_USER_LIST_LIMIT` (500) users; orgs larger than
        that should switch to the paginated
        :func:`list_org_users` API.
    """
    with db.session() as session:
        return list_org_users(
            db_session=session,
            org_id=actor.org_id,
            limit=_MAX_USER_LIST_LIMIT,
            offset=0,
        )


def compute_analytics(actor: Session) -> AnalyticsResponse:
    """Compute the three basic admin analytics panels (F-014).

    Wrapper around :func:`get_analytics_snapshot` that opens a
    short-lived database session, fetches the raw aggregation, and
    pads the weekly-activity panel so the response always contains
    exactly :data:`_DEFAULT_WEEKLY_WEEKS` (12) entries -- one per
    ISO-8601 calendar week in the trailing window, including weeks
    with zero activity. Padding gives the SPA's sparkline chart a
    continuous domain without per-render gap-filling.

    Panels:
      1. Most active contributors (top contributors by record count).
      2. Leads by outreach status (count grouped by status, all four
         enum values present).
      3. Weekly activity sparkline (record submissions per ISO week,
         last 12 weeks, every week present including zero-count
         weeks).

    Per AAP Section 0.7.1 invariant 3 (org-scoped queries), all
    panel queries filter on ``actor.org_id``. Per invariant 4
    (soft-delete-aware queries), the inventory panels (contributors,
    leads-by-status) filter ``deleted_at IS NULL``; the activity
    sparkline does NOT filter ``deleted_at`` because it measures
    contributor activity, not active inventory.

    Args:
        actor: Authenticated :class:`Session` populated by
            :mod:`app.middleware.auth`. ``actor.org_id`` is the
            multi-tenant scope; no other attribute is consumed.

    Returns:
        An :class:`AnalyticsResponse` with all three panels populated.
        The ``weekly_activity`` list contains exactly
        :data:`_DEFAULT_WEEKLY_WEEKS` (12) entries; the
        ``leads_by_status`` list contains exactly four entries (one
        per :class:`OutreachStatus` value); the
        ``most_active_contributors`` list contains 0 to
        :data:`_MAX_CONTRIBUTORS_RANK` (50) entries depending on the
        org's record population.
    """
    with db.session() as session:
        snapshot = get_analytics_snapshot(
            db_session=session,
            org_id=actor.org_id,
            weeks=_DEFAULT_WEEKLY_WEEKS,
        )

    # Pad the weekly_activity panel so the response contains exactly
    # ``_DEFAULT_WEEKLY_WEEKS`` entries. The raw aggregation only
    # returns weeks with at least one record; the SPA's sparkline
    # expects a continuous domain (one entry per week in the trailing
    # window), so we synthesize zero-count entries for empty weeks.
    #
    # Anchor on the Monday of the current ISO week (PostgreSQL's
    # ``DATE_TRUNC('week', ...)`` returns the Monday-anchored bin).
    today_utc = datetime.now(tz=UTC).date()
    monday_this_week = today_utc - timedelta(days=today_utc.weekday())
    earliest_monday = monday_this_week - timedelta(weeks=_DEFAULT_WEEKLY_WEEKS - 1)

    # Build a lookup table from the raw result. The raw entries
    # carry ``date`` objects (PostgreSQL's ``timestamptz`` was
    # ``cast``-ed to ``date`` in ``get_analytics_snapshot``).
    by_week: dict[date, int] = {
        entry.week_start: entry.record_count for entry in snapshot.weekly_activity
    }

    # Synthesize the dense series, oldest-to-newest.
    padded_weekly: list[WeeklyActivityEntry] = []
    cursor = earliest_monday
    while cursor <= monday_this_week:
        padded_weekly.append(
            WeeklyActivityEntry(
                week_start=cursor,
                record_count=by_week.get(cursor, 0),
            )
        )
        cursor = cursor + timedelta(weeks=1)

    return AnalyticsResponse(
        most_active_contributors=snapshot.most_active_contributors,
        leads_by_status=snapshot.leads_by_status,
        weekly_activity=padded_weekly,
        generated_at=snapshot.generated_at,
    )


def list_records_for_moderation(
    actor: Session,
    *,
    include_deleted: bool = True,
    page: int = _DEFAULT_MODERATION_PAGE,
    page_size: int = _DEFAULT_MODERATION_PAGE_SIZE,
    full_name_search: str | None = None,
    company_search: str | None = None,
) -> tuple[list[Record], int]:
    """Admin moderation view of records (F-014 records tab; F-007 admin path).

    Unlike the public feed (``app.services.connections.list_records``,
    F-004), this view defaults to ``include_deleted=True`` so admins
    can review and (in a future flow) restore soft-deleted records.
    Pagination defaults to the moderation panel's layout
    (:data:`_DEFAULT_MODERATION_PAGE_SIZE` = 50 records per page;
    capped at :data:`_MAX_MODERATION_PAGE_SIZE` = 200 for bulk review
    workflows).

    Org-scoped per AAP Section 0.7.1 invariant 3. The endpoint that
    calls this function is gated by the
    ``@requires_role(UserRole.ADMIN)`` decorator (per AAP Section
    0.5.2 Layer 6); the org-scope check here is defense in depth.

    Optional filters:

    * ``full_name_search`` -- case-insensitive substring match on
      ``records.full_name``. Powered by ``ILIKE`` so admins can
      search "smit" and find both "Smith" and "Smithson".
    * ``company_search`` -- case-insensitive substring match on
      ``records.company``. Same semantics as ``full_name_search``.

    Both filters are applied with leading and trailing ``%``
    wildcards. They are intentionally NOT injected into the public
    feed query (which uses exact-match filters per the F-004
    specification) because the moderation flow is admin-only and
    benefits from the more flexible substring search.

    Pagination is offset-based with the moderation defaults. The
    ``ORDER BY`` is ``submission_date DESC, id ASC`` so the result
    is deterministic across pagination calls (the ``id`` tiebreaker
    handles records with identical ``submission_date``).

    Args:
        actor: Authenticated :class:`Session` populated by
            :mod:`app.middleware.auth`. ``actor.org_id`` is the
            multi-tenant scope; no other attribute is consumed.
        include_deleted: When True (default), the result includes
            soft-deleted records. When False, the result is
            equivalent to the public feed query (active records
            only). Admins typically want True so they can see and
            moderate soft-deleted records.
        page: 1-based page number. Clamped to a minimum of 1.
        page_size: Maximum number of records per page. Clamped to
            the range [1, :data:`_MAX_MODERATION_PAGE_SIZE`].
        full_name_search: Optional case-insensitive substring filter
            on ``full_name``. ``None`` (default) or empty string
            disables the filter.
        company_search: Optional case-insensitive substring filter
            on ``company``. ``None`` (default) or empty string
            disables the filter.

    Returns:
        A tuple of ``(records, total_count)``:

        * ``records`` -- the page of :class:`Record` ORM instances
          (raw entities; the caller serializes via
          :class:`app.schemas.connection.ConnectionRead` if a rich
          representation is needed).
        * ``total_count`` -- the unpaginated total count of records
          matching the filter predicates. The SPA uses this to
          render pagination controls (page count, "showing X of Y").

        ``records`` is empty when ``page > total_count / page_size``
        (i.e., the requested page is past the end of the result set).
    """
    safe_page = max(1, page)
    safe_page_size = max(1, min(page_size, _MAX_MODERATION_PAGE_SIZE))

    # Build the base statement once so the count and page queries
    # share the same filter predicates. This guarantees the totals
    # returned by the count query match the rows returned by the
    # page query.
    base = _build_moderation_base_stmt(
        actor.org_id,
        include_deleted=include_deleted,
        full_name_search=full_name_search,
        company_search=company_search,
    )

    # Count query: wraps the base SELECT in a subquery so the COUNT
    # respects the filter predicates without re-encoding them. The
    # subquery is necessary because PostgreSQL's COUNT(*) over a
    # JOIN-free SELECT would otherwise count rows from the FROM
    # clause directly, ignoring the WHERE predicates.
    count_stmt = select(func.count()).select_from(base.subquery())

    # Page query: same predicates as the base, plus deterministic
    # ordering and offset/limit pagination. The ``id ASC`` tiebreaker
    # handles records with identical ``submission_date`` (which can
    # happen when bulk-imported records share a server timestamp).
    page_stmt = (
        base.order_by(desc(Record.submission_date), asc(Record.id))
        .limit(safe_page_size)
        .offset((safe_page - 1) * safe_page_size)
    )

    with db.session() as session:
        total = session.execute(count_stmt).scalar_one()
        rows = list(session.execute(page_stmt).scalars().all())
    return rows, total
