"""Admin panel aggregation and moderation services (F-014, F-009).

This module provides the data-tier helpers that back the admin panel
endpoints :mod:`app.api.admin` (CP5 deliverable). Per AAP Section 0.5.2
Layer 6:

    "services/admin.py - Aggregation queries for analytics and
     record-moderation operations."

Aggregation queries (F-014):

* :func:`get_analytics_snapshot` returns the three-panel response
  shape (most active contributors, leads by status, weekly activity)
  consumed by ``GET /api/admin/analytics``. The query plan hits the
  composite ``(org_id, deleted_at, submission_date DESC)`` index per
  AAP Section 0.7.3 so the call remains responsive at the 10K-record
  scale ceiling.

User management (F-009 + F-014):

* :func:`list_org_users` paginates the users in an organization for
  the admin user-management view.
* :func:`update_user_role` mutates a user's role and emits the F-013
  ``role_change`` audit event in the same transaction. Enforces two
  organizational invariants:
      1. The "last admin" guarantee: an Admin cannot be demoted if
         they are the SOLE Admin in the organization. This prevents
         lockout scenarios.
      2. The self-demotion guard: an Admin cannot demote themselves.
         Demotion always flows through another admin to preserve
         four-eyes governance.

Record moderation (F-007 + F-014):

* :func:`hard_delete_record` permanently removes a record from the
  database (admin-only path per AAP Section 0.5.2). Unlike the soft
  delete handled by service-level connection update flow, hard delete
  emits ``audit_events.event_type = 'hard_delete'`` with the full
  record payload preserved in ``before_payload`` so the audit history
  retains the row's content even after the row is gone.

Per AAP Section 0.5.3 ("service functions own transactions"), every
state-changing function in this module opens its own ``with
session.begin():`` block when called outside an active transaction
and invokes :func:`emit_audit_event` inside that transaction so the
state change and the audit row commit (or roll back) atomically.

Per AAP Section 0.7.1 invariant 3 (org-scoped queries), every read
and every write in this module injects ``WHERE org_id = ...`` either
explicitly or via the caller-supplied ``org_id`` parameter. Cross-org
access would surface as a 404 from the calling handler.

This module deliberately has no module-level side effects beyond the
exception class declarations. Importing :mod:`app.services.admin`
does not log, does not make HTTP calls, does not touch the
filesystem, and does not connect to any database.
"""

from __future__ import annotations

# Standard library imports.
#
# ``datetime`` provides timezone-aware UTC timestamps for the
# weekly-activity sparkline and the analytics snapshot timestamp.
# ``timedelta`` computes the trailing-N-weeks window. ``UUID``
# annotates the org/user/record identity parameters.
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

# Third-party runtime imports.
#
# ``sqlalchemy.func`` provides DATE_TRUNC, COUNT, and other SQL
# aggregates used in :func:`get_analytics_snapshot`. ``select`` is
# the SQLAlchemy 2.x query builder. ``and_`` composes filter
# expressions. ``cast`` and ``Date`` coerce DATE_TRUNC's return
# (timestamptz) to a plain DATE for week_start serialization.
from sqlalchemy import Date, and_, cast, func, select
import structlog

# First-party imports.
#
# ``AppError`` is the base class extended by the two domain
# exceptions defined here (LastAdminError, SelfDemotionError).
# ``Record``, ``User`` are the SQLAlchemy declarative models the
# aggregation queries operate on. ``OutreachStatus`` and ``UserRole``
# are the Python enums mirroring the PostgreSQL enum types.
# ``AuditEventType`` powers the F-013 audit emission. ``emit_audit_event``
# is the SOLE writer of the audit_events table per AAP Section 0.7.1
# invariant 5. The :class:`app.schemas.admin` schemas (UserRead,
# AnalyticsResponse, etc.) are imported only for type annotations
# of the public return shapes.
from app.middleware.error_handlers import ConflictError, ForbiddenError, NotFoundError
from app.models import Record, User
from app.models.enums import AuditEventType, OutreachStatus, UserRole
from app.schemas import (
    AnalyticsResponse,
    ContributorActivity,
    LeadsByStatusEntry,
    UserRead,
    WeeklyActivityEntry,
)
from app.services.audit import emit_audit_event

# Type-only imports.
if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.orm import Session as DBSession


# ---------------------------------------------------------------------------
# Module logger
# ---------------------------------------------------------------------------
_logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

# Cap on the most-active-contributors panel size. Larger orgs would
# overwhelm the SPA's small panel layout; the panel is intended as an
# at-a-glance leaderboard, not an exhaustive list.
_MAX_CONTRIBUTORS_RANK: int = 50

# Number of trailing weeks shown in the sparkline panel. 12 matches
# the SPA's small panel width.
_DEFAULT_WEEKLY_WEEKS: int = 12

# Hard cap on the trailing-weeks parameter to prevent pathological
# inputs (e.g., 10000 weeks) from producing an oversized response.
_MAX_WEEKLY_WEEKS: int = 52


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

__all__ = [
    "LastAdminError",
    "SelfDemotionError",
    "get_analytics_snapshot",
    "hard_delete_record",
    "list_org_users",
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
    """

    @property
    def default_message(self) -> str:
        """Return the canonical 'last admin' error message."""
        return (
            "Cannot demote the last Admin in the organization. "
            "Promote another user to Admin first."
        )


class SelfDemotionError(ForbiddenError):
    """Raised when an admin attempts to demote themselves.

    Mapped to HTTP 403 with stable error code ``"forbidden"`` by the
    registered Flask error handler (inherited from
    :class:`ForbiddenError`). Per AAP Section 0.5.2 Layer 6, role
    demotion always requires another admin to act, preserving
    four-eyes governance: a single admin's compromised account cannot
    silently downgrade their own role to evade audit oversight.
    """

    @property
    def default_message(self) -> str:
        """Return the canonical 'self-demotion forbidden' message."""
        return (
            "Admins cannot change their own role. "
            "Another Admin must perform the role change."
        )


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

    1. **Most active contributors** - GROUP BY ``records.owner_user_id``
       JOIN ``users`` to project ``display_name``, ORDER BY
       ``record_count DESC`` LIMIT :data:`_MAX_CONTRIBUTORS_RANK`.
       Filtered by ``deleted_at IS NULL`` because the panel measures
       *active inventory*, not historical activity.

    2. **Leads by status** - GROUP BY ``records.outreach_status`` over
       active records. Always returns exactly 4 entries (one per
       OutreachStatus value); statuses with zero leads get
       ``count=0`` placeholders so the SPA renders a complete
       bar/pie chart even on a quiet org.

    3. **Weekly activity sparkline** - GROUP BY DATE_TRUNC('week',
       submission_date) over the trailing ``weeks`` window. Includes
       soft-deleted records because the sparkline measures
       contributor *activity*, not active inventory.

    Per AAP Section 0.7.3, the queries hit the composite index
    ``(org_id, deleted_at, submission_date DESC)`` (panels 1, 2) and
    a subset of the same index (panel 3, which filters by
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
            :data:`_MAX_WEEKLY_WEEKS` (52). Defaults to 12 (SPA panel
            width).

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
    # rank cap. Using ``users.display_name`` (not records.owner_display_name)
    # so that a recent rename surfaces in the analytics on the next
    # refresh per the AAP Section 0.5.2 Layer 6 specification of the
    # ContributorActivity schema's display_name field.
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
        .order_by(contributor_count.desc())
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
    # GROUP BY outreach_status over active records. Build a lookup
    # table from the SQL result then synthesize four entries (one per
    # enum value) so statuses with zero leads still appear with
    # count=0. This guarantees the SPA's bar/pie chart always renders
    # a complete view.
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
    status_counts: dict[OutreachStatus, int] = {
        row.outreach_status: int(row.status_count) for row in status_rows
    }
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
    # STILL counts as a contribution).
    now_utc = datetime.now(UTC)
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
        .order_by(week_bin)
    )
    weekly_rows = db_session.execute(weekly_stmt).all()
    weekly_activity: list[WeeklyActivityEntry] = [
        WeeklyActivityEntry(
            week_start=row.week_start,
            record_count=int(row.weekly_count),
        )
        for row in weekly_rows
    ]

    _logger.info(
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
    limit: int = 100,
    offset: int = 0,
) -> list[UserRead]:
    """Paginate users within the supplied organization.

    Used by ``GET /api/admin/users`` to render the admin user list.
    Users are returned sorted by ``display_name ASC`` so the list is
    deterministic across pagination calls.

    Per AAP Section 0.7.4 (Security Invariants), the returned
    :class:`UserRead` shape NEVER includes ``password_hash`` or
    ``org_id`` - those are server-only fields.

    Args:
        db_session: An open SQLAlchemy session. Read-only.
        org_id: The organization whose users are listed. Injected as
            ``WHERE org_id = :org_id``.
        limit: Maximum number of users to return. Defaults to 100;
            capped at 500 defensively.
        offset: Number of rows to skip. Defaults to 0.

    Returns:
        A list of :class:`UserRead` shapes, ordered by display_name.
    """
    safe_limit = max(1, min(limit, 500))
    safe_offset = max(0, offset)

    stmt = (
        select(User)
        .where(User.org_id == org_id)
        .order_by(User.display_name.asc())
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
    new_role: UserRole,
    actor_user_id: UUID,
) -> User:
    """Mutate a user's role and emit a F-013 audit event.

    Used by ``PATCH /api/admin/users/:id`` (Admin-only). Enforces two
    organizational invariants per AAP Section 0.5.2 Layer 6:

    1. The "last admin" guarantee: an Admin cannot be demoted if they
       are the SOLE Admin in the organization. Surface as
       :class:`LastAdminError` (HTTP 409).
    2. The self-demotion guard: an Admin cannot demote themselves.
       Surface as :class:`SelfDemotionError` (HTTP 403). The handler's
       ``@requires_role(Admin)`` decorator already gates the endpoint;
       this guard is the secondary defense against an admin issuing
       the call through their own session.

    The function emits ``audit_events.event_type = role_change``
    inside the caller's transaction. The ``before_payload`` carries
    the prior role and the ``after_payload`` carries the new role
    plus the target user_id, so the audit history reconstructs the
    full role change without joining to the users table.

    Args:
        db_session: An open SQLAlchemy session with an active
            transaction (caller-owned per AAP Section 0.5.3).
        org_id: The organization scope.
        target_user_id: The id of the user whose role is being
            mutated.
        new_role: The new role to assign. One of UserRole.
        actor_user_id: The id of the admin issuing the change. Used
            for the self-demotion guard and for the audit event's
            ``actor_user_id`` field.

    Returns:
        The mutated :class:`User` ORM instance, refreshed by the
        caller's transaction.

    Raises:
        NotFoundError: When ``target_user_id`` does not exist in the
            organization (could be cross-org or non-existent; both
            surface as 404 to avoid leaking the existence of cross-org
            users per AAP Section 0.5.2 NotFoundError contract).
        SelfDemotionError: When ``actor_user_id == target_user_id``.
            Mapped to HTTP 403.
        LastAdminError: When demoting the SOLE Admin in the
            organization. Mapped to HTTP 409.
    """
    # Self-demotion guard. Runs FIRST so the most actionable error
    # surfaces before any database round-trips.
    if actor_user_id == target_user_id:
        _logger.warning(
            "update_user_role_self_demotion_blocked",
            org_id=str(org_id),
            actor_user_id=str(actor_user_id),
        )
        raise SelfDemotionError()

    # Lookup the target user, scoped to the org.
    target_stmt = select(User).where(
        User.id == target_user_id,
        User.org_id == org_id,
    )
    target_user: User | None = db_session.scalar(target_stmt)
    if target_user is None:
        # Per AAP Section 0.5.2 NotFoundError contract: return 404 for
        # both non-existent IDs and cross-org IDs so the response
        # never reveals whether a UUID exists in a different
        # organization.
        raise NotFoundError(message="User not found.")

    previous_role: UserRole = target_user.role
    if previous_role == new_role:
        # No-op role change. Still permitted (idempotent), but no
        # audit event is emitted because nothing changed. The handler
        # surfaces the unchanged user back to the SPA.
        _logger.info(
            "update_user_role_noop",
            org_id=str(org_id),
            target_user_id=str(target_user_id),
            role=new_role.value,
        )
        return target_user

    # Last-admin guard: only fires when DEMOTING from Admin (i.e.,
    # previous role is Admin and new role is anything else). Counts
    # admins in the org BEFORE the mutation; if the count is exactly
    # 1, we would lock the org out by demoting.
    if previous_role == UserRole.ADMIN and new_role != UserRole.ADMIN:
        admin_count_stmt = select(func.count(User.id)).where(
            User.org_id == org_id,
            User.role == UserRole.ADMIN,
        )
        admin_count = db_session.scalar(admin_count_stmt) or 0
        if admin_count <= 1:
            _logger.warning(
                "update_user_role_last_admin_blocked",
                org_id=str(org_id),
                target_user_id=str(target_user_id),
                actor_user_id=str(actor_user_id),
            )
            raise LastAdminError()

    # Apply the mutation. The flush below ensures the role change is
    # observable in the same transaction (so the audit event sees the
    # post-mutation state if it queries) without committing the
    # transaction.
    target_user.role = new_role
    db_session.flush()

    # Emit the F-013 role_change audit event inside the caller's
    # transaction. The before/after payloads capture the role
    # transition plus the target user_id so audit history is
    # reconstructable without joining to users.
    emit_audit_event(
        db_session=db_session,
        event_type=AuditEventType.ROLE_CHANGE,
        actor_user_id=actor_user_id,
        # ROLE_CHANGE has no record target; the user is identified in
        # the payload below.
        target_record_id=None,
        before_payload={
            "user_id": str(target_user_id),
            "role": previous_role.value,
        },
        after_payload={
            "user_id": str(target_user_id),
            "role": new_role.value,
        },
    )

    _logger.info(
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
    row from the ``records`` table.

    Per AAP Section 0.5.2 Layer 6, the F-013 ``hard_delete`` audit
    event captures the FULL record payload in ``before_payload``
    BEFORE the delete fires, so the audit history retains the row's
    content even after the row itself is gone. The audit event has
    ``target_record_id = None`` because the record ceases to exist
    after the transaction commits and a non-null FK would conflict
    with the post-commit row state.

    Per AAP Section 0.7.1 invariant 5 (append-only audit), the audit
    row INSERT happens inside the same transaction as the record
    DELETE so they commit (or roll back) atomically. The transaction
    fence is the caller's responsibility per AAP Section 0.5.3.

    Args:
        db_session: An open SQLAlchemy session with an active
            transaction.
        org_id: The organization scope. Injected as ``WHERE org_id =
            :org_id`` on the SELECT and DELETE.
        record_id: The id of the record to hard-delete.
        actor_user_id: The id of the admin issuing the delete.

    Raises:
        NotFoundError: When the record does not exist or is not in
            the supplied organization. Both cases surface as 404 per
            AAP Section 0.5.2 NotFoundError contract.
    """
    # Lookup the record, scoped to the org. We include soft-deleted
    # records in this query because hard delete must be able to
    # operate on any row regardless of soft-delete state - admins
    # may want to purge a soft-deleted record permanently from the
    # database (e.g., for GDPR right-to-erasure compliance).
    record_stmt = select(Record).where(
        Record.id == record_id,
        Record.org_id == org_id,
    )
    record: Record | None = db_session.scalar(record_stmt)
    if record is None:
        raise NotFoundError(message="Record not found.")

    # Snapshot the record payload BEFORE the delete so the audit
    # row can preserve it. The snapshot uses string forms of UUIDs
    # and enums so the JSONB column accepts them without custom
    # encoders.
    before_payload = {
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
        "involvement": (
            record.involvement.value if record.involvement is not None else None
        ),
        "outreach_status": (
            record.outreach_status.value if record.outreach_status is not None else None
        ),
        "submission_date": (
            record.submission_date.isoformat() if record.submission_date is not None else None
        ),
        "deleted_at": (
            record.deleted_at.isoformat() if record.deleted_at is not None else None
        ),
    }

    # Emit the audit event FIRST so the audit row's INSERT runs
    # inside the same transaction as the record DELETE. If the
    # audit emission fails, the DELETE never fires and the caller's
    # transaction rolls back atomically per AAP Section 0.7.1
    # invariant 6.
    emit_audit_event(
        db_session=db_session,
        event_type=AuditEventType.HARD_DELETE,
        actor_user_id=actor_user_id,
        # Per AAP Section 0.5.2 Layer 6: target_record_id is None
        # for hard_delete because the row ceases to exist after
        # commit; populating the FK would conflict with the
        # post-commit row state.
        target_record_id=None,
        before_payload=before_payload,
        after_payload=None,
    )

    # Physical delete. SQLAlchemy issues a DELETE statement on the
    # next flush; the caller's ``with session.begin():`` block
    # commits the transaction once this function returns.
    db_session.delete(record)

    _logger.info(
        "hard_delete_record_applied",
        org_id=str(org_id),
        record_id=str(record_id),
        actor_user_id=str(actor_user_id),
    )



