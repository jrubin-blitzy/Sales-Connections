"""Tests for the F-014 admin service (`app.services.admin`).

Covers:
    - get_analytics_snapshot: three-panel aggregation
    - list_org_users: pagination, ordering
    - update_user_role: last-admin guard, self-demotion guard,
      audit emission
    - hard_delete_record: payload preservation in audit, physical
      DELETE in same transaction
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

import pytest

from app.middleware.error_handlers import NotFoundError
from app.models import AuditEvent
from app.models.enums import AuditEventType, OutreachStatus, UserRole
from app.services.admin import (
    LastAdminError,
    SelfDemotionError,
    get_analytics_snapshot,
    hard_delete_record,
    list_org_users,
    update_user_role,
)

if TYPE_CHECKING:
    from flask import Flask
    from sqlalchemy.orm import Session as DBSession


# ---------------------------------------------------------------------------
# TestGetAnalyticsSnapshot
# ---------------------------------------------------------------------------


class TestGetAnalyticsSnapshot:
    """Verify get_analytics_snapshot produces the three-panel response."""

    def test_empty_org_returns_zeroed_panels(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
    ) -> None:
        """A fresh org returns empty panels with leads_by_status = 4 zeros."""
        snapshot = get_analytics_snapshot(
            db_session=db_session,
            org_id=organization.id,
        )
        assert snapshot.most_active_contributors == []
        # leads_by_status: always 4 entries (one per OutreachStatus).
        assert len(snapshot.leads_by_status) == 4
        for entry in snapshot.leads_by_status:
            assert entry.count == 0
        assert snapshot.weekly_activity == []
        assert snapshot.generated_at is not None

    def test_most_active_contributors_ordered_by_count(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
    ) -> None:
        """Contributors are ranked by record count DESC."""
        from tests.factories import RecordFactory, UserFactory  # noqa: PLC0415

        user_a = UserFactory(organization=organization, display_name="Alice")
        user_b = UserFactory(organization=organization, display_name="Bob")

        # Alice has 3 records, Bob has 1.
        for _ in range(3):
            RecordFactory(organization=organization, owner=user_a)
        RecordFactory(organization=organization, owner=user_b)

        snapshot = get_analytics_snapshot(db_session=db_session, org_id=organization.id)
        assert len(snapshot.most_active_contributors) == 2
        assert snapshot.most_active_contributors[0].display_name == "Alice"
        assert snapshot.most_active_contributors[0].record_count == 3
        assert snapshot.most_active_contributors[1].display_name == "Bob"

    def test_leads_by_status_includes_all_four_statuses(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """All 4 OutreachStatus values appear, even with zero records."""
        from tests.factories import RecordFactory  # noqa: PLC0415

        # Only one record with status=Contacted.
        RecordFactory(
            organization=organization,
            owner=contributor_user,
            outreach_status=OutreachStatus.CONTACTED,
        )

        snapshot = get_analytics_snapshot(db_session=db_session, org_id=organization.id)
        # Build status -> count map for assertion.
        status_map = {entry.status: entry.count for entry in snapshot.leads_by_status}
        assert status_map[OutreachStatus.NOT_STARTED] == 0
        assert status_map[OutreachStatus.IN_PROGRESS] == 0
        assert status_map[OutreachStatus.CONTACTED] == 1
        assert status_map[OutreachStatus.CLOSED] == 0

    def test_soft_deleted_records_excluded_from_inventory_panels(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """Soft-deleted records do NOT count in contributor or status panels."""
        from tests.factories import RecordFactory, SoftDeletedRecordFactory  # noqa: PLC0415

        # 1 active record + 1 soft-deleted record.
        RecordFactory(
            organization=organization,
            owner=contributor_user,
            outreach_status=OutreachStatus.NOT_STARTED,
        )
        SoftDeletedRecordFactory(
            organization=organization,
            owner=contributor_user,
            outreach_status=OutreachStatus.NOT_STARTED,
        )

        snapshot = get_analytics_snapshot(db_session=db_session, org_id=organization.id)

        # most_active_contributors counts only active records.
        assert snapshot.most_active_contributors[0].record_count == 1
        # leads_by_status only counts active records.
        not_started = next(
            e for e in snapshot.leads_by_status if e.status == OutreachStatus.NOT_STARTED
        )
        assert not_started.count == 1


# ---------------------------------------------------------------------------
# TestListOrgUsers
# ---------------------------------------------------------------------------


class TestListOrgUsers:
    """Verify list_org_users pagination and ordering."""

    def test_returns_users_ordered_by_display_name(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
    ) -> None:
        """Users are ordered by display_name ASC."""
        from tests.factories import UserFactory  # noqa: PLC0415

        UserFactory(organization=organization, display_name="Charlie")
        UserFactory(organization=organization, display_name="Alice")
        UserFactory(organization=organization, display_name="Bob")

        users = list_org_users(db_session=db_session, org_id=organization.id)
        names = [u.display_name for u in users]
        assert names == sorted(names)

    def test_excludes_other_orgs(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
    ) -> None:
        """Only users in the supplied org are returned."""
        from tests.factories import OrganizationFactory, UserFactory  # noqa: PLC0415

        UserFactory(organization=organization, display_name="In Org")

        other_org = OrganizationFactory()
        UserFactory(organization=other_org, display_name="Other Org")

        users = list_org_users(db_session=db_session, org_id=organization.id)
        names = {u.display_name for u in users}
        assert "In Org" in names
        assert "Other Org" not in names

    def test_pagination_with_limit_and_offset(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
    ) -> None:
        """limit and offset paginate correctly."""
        from tests.factories import UserFactory  # noqa: PLC0415

        for i in range(5):
            UserFactory(organization=organization, display_name=f"User{i:02d}")

        page1 = list_org_users(
            db_session=db_session,
            org_id=organization.id,
            limit=2,
            offset=0,
        )
        page2 = list_org_users(
            db_session=db_session,
            org_id=organization.id,
            limit=2,
            offset=2,
        )
        assert len(page1) == 2
        assert len(page2) == 2
        # No overlap.
        assert {u.id for u in page1}.isdisjoint({u.id for u in page2})


# ---------------------------------------------------------------------------
# TestUpdateUserRole
# ---------------------------------------------------------------------------


class TestUpdateUserRole:
    """Verify update_user_role's invariants and audit emission."""

    def test_promote_contributor_to_admin(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        admin_user,
        contributor_user,
    ) -> None:
        """Admin can promote a Contributor to Admin."""
        with db_session.begin():
            updated = update_user_role(
                db_session=db_session,
                org_id=organization.id,
                target_user_id=contributor_user.id,
                new_role=UserRole.ADMIN,
                actor_user_id=admin_user.id,
            )

        assert updated.id == contributor_user.id
        assert updated.role == UserRole.ADMIN

    def test_role_change_emits_audit_event(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        admin_user,
        contributor_user,
    ) -> None:
        """Role mutation emits a ROLE_CHANGE audit event."""
        with db_session.begin():
            update_user_role(
                db_session=db_session,
                org_id=organization.id,
                target_user_id=contributor_user.id,
                new_role=UserRole.VIEWER,
                actor_user_id=admin_user.id,
            )

        # Check audit event.
        events = (
            db_session.query(AuditEvent)
            .filter(AuditEvent.event_type == AuditEventType.ROLE_CHANGE)
            .all()
        )
        assert len(events) == 1
        evt = events[0]
        assert evt.actor_user_id == admin_user.id
        assert evt.before_payload["role"] == "Contributor"
        assert evt.after_payload["role"] == "Viewer"
        assert evt.before_payload["user_id"] == str(contributor_user.id)

    def test_self_demotion_blocked(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        admin_user,
    ) -> None:
        """Admin cannot demote themselves."""
        with db_session.begin(), pytest.raises(SelfDemotionError):
            update_user_role(
                db_session=db_session,
                org_id=organization.id,
                target_user_id=admin_user.id,
                new_role=UserRole.CONTRIBUTOR,
                actor_user_id=admin_user.id,
            )

    def test_last_admin_demotion_blocked(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        admin_user,
    ) -> None:
        """Cannot demote the sole Admin via another admin's action."""
        # We only have ONE admin (admin_user). Create a second admin
        # who issues the demotion call against admin_user.
        from tests.factories import AdminUserFactory  # noqa: PLC0415

        # admin_user is the sole admin so far. Create a second admin.
        # But wait: if there are 2 admins, demoting one is allowed.
        # To test the "last admin" guard, we need only 1 admin in the
        # org and another user (non-admin) attempting to demote them.
        # However, the function checks actor != target FIRST (self-
        # demotion guard), so we need a NON-self actor that is also
        # an admin... but if there's only one admin, that's a
        # contradiction. Instead, let's create 2 admins, then have
        # one demote the other - that should SUCCEED. And to test
        # the last-admin guard, we delete one admin and try to demote
        # the sole remaining one.
        second_admin = AdminUserFactory(organization=organization)

        # Now there are 2 admins (admin_user, second_admin). Demote
        # admin_user via second_admin. This should succeed.
        with db_session.begin():
            update_user_role(
                db_session=db_session,
                org_id=organization.id,
                target_user_id=admin_user.id,
                new_role=UserRole.CONTRIBUTOR,
                actor_user_id=second_admin.id,
            )

        # Now second_admin is the sole admin. Demoting them should
        # fire LastAdminError - but only another admin can issue the
        # call, and there are no other admins. We simulate the
        # scenario by passing a freshly-created admin AS the actor;
        # that admin would have to demote second_admin.
        another_admin = AdminUserFactory(organization=organization)
        with db_session.begin():
            update_user_role(
                db_session=db_session,
                org_id=organization.id,
                target_user_id=another_admin.id,
                new_role=UserRole.CONTRIBUTOR,
                actor_user_id=second_admin.id,
            )
        # Now second_admin is the SOLE admin. Try to demote them.
        # Actor must be different (not self-demotion). Since
        # another_admin is now Contributor, we use another fresh
        # admin to issue the call.
        third_admin = AdminUserFactory(organization=organization)
        # Demote third_admin to Contributor first so second_admin
        # is truly alone.
        with db_session.begin():
            update_user_role(
                db_session=db_session,
                org_id=organization.id,
                target_user_id=third_admin.id,
                new_role=UserRole.CONTRIBUTOR,
                actor_user_id=second_admin.id,
            )

        # Now second_admin is the sole admin. Create one more admin
        # to act as the demotion-issuer.
        fourth_admin = AdminUserFactory(organization=organization)
        # Two admins now: second_admin and fourth_admin.
        # If we demote second_admin via fourth_admin, fourth_admin
        # remains - that succeeds (not last admin scenario).
        # The "last admin" scenario only fires when demoting WOULD
        # leave zero admins. So we need: only 1 admin in the org.
        # Then any demotion call against that admin (issued by anyone
        # other than themselves) fires LastAdminError.
        # Demote fourth_admin first.
        with db_session.begin():
            update_user_role(
                db_session=db_session,
                org_id=organization.id,
                target_user_id=fourth_admin.id,
                new_role=UserRole.CONTRIBUTOR,
                actor_user_id=second_admin.id,
            )

        # Now ONLY second_admin is admin. Try to demote them via
        # any non-admin actor. The function would fail self-demotion
        # check first, but actor_user_id is now fourth_admin (a
        # Contributor). The function does NOT check the actor's
        # role - that's the API layer's job (@requires_role(Admin)).
        # So we expect LastAdminError to fire.
        with db_session.begin(), pytest.raises(LastAdminError):
            update_user_role(
                db_session=db_session,
                org_id=organization.id,
                target_user_id=second_admin.id,
                new_role=UserRole.CONTRIBUTOR,
                actor_user_id=fourth_admin.id,  # Not self
            )

    def test_no_op_role_change_returns_user_unchanged(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        admin_user,
        contributor_user,
    ) -> None:
        """Setting role to current value is a no-op (no audit event)."""
        # contributor_user is already a Contributor.
        with db_session.begin():
            updated = update_user_role(
                db_session=db_session,
                org_id=organization.id,
                target_user_id=contributor_user.id,
                new_role=UserRole.CONTRIBUTOR,
                actor_user_id=admin_user.id,
            )

        # No audit event was emitted for a no-op.
        events = (
            db_session.query(AuditEvent)
            .filter(AuditEvent.event_type == AuditEventType.ROLE_CHANGE)
            .all()
        )
        assert events == []
        assert updated.role == UserRole.CONTRIBUTOR

    def test_target_not_found_raises(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        admin_user,
    ) -> None:
        """Non-existent user yields NotFoundError."""
        with db_session.begin(), pytest.raises(NotFoundError):
            update_user_role(
                db_session=db_session,
                org_id=organization.id,
                target_user_id=uuid.uuid4(),
                new_role=UserRole.VIEWER,
                actor_user_id=admin_user.id,
            )

    def test_cross_org_target_returns_not_found(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        admin_user,
    ) -> None:
        """Target in another org is treated as not-found (avoids leaking)."""
        from tests.factories import OrganizationFactory, UserFactory  # noqa: PLC0415

        other_org = OrganizationFactory()
        other_user = UserFactory(organization=other_org)

        with db_session.begin(), pytest.raises(NotFoundError):
            update_user_role(
                db_session=db_session,
                org_id=organization.id,
                target_user_id=other_user.id,
                new_role=UserRole.VIEWER,
                actor_user_id=admin_user.id,
            )


# ---------------------------------------------------------------------------
# TestHardDeleteRecord
# ---------------------------------------------------------------------------


class TestHardDeleteRecord:
    """Verify hard_delete_record's audit-and-delete atomicity."""

    def test_deletes_record_and_emits_audit(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        admin_user,
        contributor_user,
    ) -> None:
        """Hard delete removes the row AND emits a HARD_DELETE audit event."""
        from app.models import Record  # noqa: PLC0415
        from tests.factories import RecordFactory  # noqa: PLC0415

        record = RecordFactory(organization=organization, owner=contributor_user)
        record_id = record.id

        with db_session.begin():
            hard_delete_record(
                db_session=db_session,
                org_id=organization.id,
                record_id=record_id,
                actor_user_id=admin_user.id,
            )

        # Record is physically gone.
        rows = db_session.query(Record).filter_by(id=record_id).all()
        assert rows == []

        # HARD_DELETE audit event exists.
        events = (
            db_session.query(AuditEvent)
            .filter(AuditEvent.event_type == AuditEventType.HARD_DELETE)
            .all()
        )
        assert len(events) == 1
        evt = events[0]
        assert evt.actor_user_id == admin_user.id
        assert evt.target_record_id is None  # FK is null after delete

    def test_audit_payload_preserves_record_data(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        admin_user,
        contributor_user,
    ) -> None:
        """before_payload captures the full record before deletion."""
        from tests.factories import RecordFactory  # noqa: PLC0415

        record = RecordFactory(
            organization=organization,
            owner=contributor_user,
            full_name="Before Delete",
        )
        record_id_str = str(record.id)
        full_name = record.full_name

        with db_session.begin():
            hard_delete_record(
                db_session=db_session,
                org_id=organization.id,
                record_id=record.id,
                actor_user_id=admin_user.id,
            )

        events = (
            db_session.query(AuditEvent)
            .filter(AuditEvent.event_type == AuditEventType.HARD_DELETE)
            .all()
        )
        evt = events[0]
        assert evt.before_payload["id"] == record_id_str
        assert evt.before_payload["full_name"] == full_name
        assert evt.after_payload is None

    def test_record_not_found_raises(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        admin_user,
    ) -> None:
        """Non-existent record yields NotFoundError."""
        with db_session.begin(), pytest.raises(NotFoundError):
            hard_delete_record(
                db_session=db_session,
                org_id=organization.id,
                record_id=uuid.uuid4(),
                actor_user_id=admin_user.id,
            )

    def test_cross_org_record_returns_not_found(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        admin_user,
    ) -> None:
        """Record in another org returns 404."""
        from tests.factories import (  # noqa: PLC0415
            OrganizationFactory,
            RecordFactory,
            UserFactory,
        )

        other_org = OrganizationFactory()
        other_user = UserFactory(organization=other_org)
        other_record = RecordFactory(organization=other_org, owner=other_user)

        with db_session.begin(), pytest.raises(NotFoundError):
            hard_delete_record(
                db_session=db_session,
                org_id=organization.id,
                record_id=other_record.id,
                actor_user_id=admin_user.id,
            )

    def test_can_hard_delete_soft_deleted_record(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        admin_user,
        contributor_user,
    ) -> None:
        """Hard delete works on soft-deleted records too (GDPR compliance)."""
        from app.models import Record  # noqa: PLC0415
        from tests.factories import SoftDeletedRecordFactory  # noqa: PLC0415

        record = SoftDeletedRecordFactory(organization=organization, owner=contributor_user)
        record_id = record.id

        with db_session.begin():
            hard_delete_record(
                db_session=db_session,
                org_id=organization.id,
                record_id=record_id,
                actor_user_id=admin_user.id,
            )

        rows = db_session.query(Record).filter_by(id=record_id).all()
        assert rows == []
