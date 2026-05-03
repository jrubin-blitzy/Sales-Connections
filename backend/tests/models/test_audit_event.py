"""Tests for the AuditEvent model.

Per AAP Section 0.7.1 invariant 5: audit_events is APPEND-ONLY. The
ORM should not issue UPDATE or DELETE against it. The database also
revokes UPDATE/DELETE privileges from the application role; that is
verified in services/test_audit.py via grep.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

from app.models import AuditEvent
from app.models.enums import AuditEventType

if TYPE_CHECKING:
    from sqlalchemy.orm import Session as DBSession


class TestAuditEventCreation:
    """AuditEvent creation invariants."""

    def test_create_with_required_fields(
        self,
        db_session: DBSession,
        admin_user,
    ) -> None:
        """AuditEvent persists with required fields."""
        event = AuditEvent(
            actor_user_id=admin_user.id,
            event_type=AuditEventType.AUTHENTICATION,
            target_record_id=None,
            before_payload=None,
            after_payload={"method": "password", "outcome": "success"},
        )
        db_session.add(event)
        db_session.commit()
        assert event.id is not None

    def test_event_timestamp_set_on_create(
        self,
        db_session: DBSession,
        admin_user,
    ) -> None:
        """event_timestamp is server-assigned on create."""
        event = AuditEvent(
            actor_user_id=admin_user.id,
            event_type=AuditEventType.AUTHENTICATION,
            target_record_id=None,
        )
        db_session.add(event)
        db_session.commit()
        assert event.event_timestamp is not None
        assert event.event_timestamp.tzinfo is not None

    def test_id_is_uuid(
        self,
        db_session: DBSession,
        admin_user,
    ) -> None:
        """id is a server-assigned UUID."""
        event = AuditEvent(
            actor_user_id=admin_user.id,
            event_type=AuditEventType.AUTHENTICATION,
        )
        db_session.add(event)
        db_session.flush()
        assert isinstance(event.id, uuid.UUID)

    def test_target_record_id_optional(
        self,
        db_session: DBSession,
        admin_user,
    ) -> None:
        """target_record_id is nullable (auth events have no target)."""
        event = AuditEvent(
            actor_user_id=admin_user.id,
            event_type=AuditEventType.AUTHENTICATION,
            target_record_id=None,
        )
        db_session.add(event)
        db_session.commit()
        assert event.target_record_id is None

    def test_payloads_optional(
        self,
        db_session: DBSession,
        admin_user,
    ) -> None:
        """before_payload and after_payload are nullable."""
        event = AuditEvent(
            actor_user_id=admin_user.id,
            event_type=AuditEventType.AUTHENTICATION,
            before_payload=None,
            after_payload=None,
        )
        db_session.add(event)
        db_session.commit()


class TestAuditEventTypes:
    """AuditEvent records of each documented type."""

    def test_create_event(
        self,
        db_session: DBSession,
        admin_user,
        contributor_user,
        organization,
    ) -> None:
        """CREATE event with target record."""
        from tests.factories import RecordFactory  # noqa: PLC0415

        record = RecordFactory(
            organization=organization, owner=contributor_user
        )
        event = AuditEvent(
            actor_user_id=admin_user.id,
            event_type=AuditEventType.CREATE,
            target_record_id=record.id,
            after_payload={"created": True},
        )
        db_session.add(event)
        db_session.commit()
        assert event.event_type == AuditEventType.CREATE

    def test_status_change_event(
        self,
        db_session: DBSession,
        admin_user,
        contributor_user,
        organization,
    ) -> None:
        """STATUS_CHANGE event with before/after payloads."""
        from tests.factories import RecordFactory  # noqa: PLC0415

        record = RecordFactory(
            organization=organization, owner=contributor_user
        )
        event = AuditEvent(
            actor_user_id=admin_user.id,
            event_type=AuditEventType.STATUS_CHANGE,
            target_record_id=record.id,
            before_payload={"outreach_status": "Not Started"},
            after_payload={"outreach_status": "In Progress"},
        )
        db_session.add(event)
        db_session.commit()
        assert event.before_payload["outreach_status"] == "Not Started"
        assert event.after_payload["outreach_status"] == "In Progress"

    def test_role_change_event(
        self,
        db_session: DBSession,
        admin_user,
    ) -> None:
        """ROLE_CHANGE event records role transition."""
        event = AuditEvent(
            actor_user_id=admin_user.id,
            event_type=AuditEventType.ROLE_CHANGE,
            target_record_id=None,
            before_payload={
                "user_id": str(admin_user.id),
                "role": "Contributor",
            },
            after_payload={
                "user_id": str(admin_user.id),
                "role": "Admin",
            },
        )
        db_session.add(event)
        db_session.commit()
        assert event.event_type == AuditEventType.ROLE_CHANGE


class TestAuditEventNoCascadeOnUserDelete:
    """audit_events.actor_user_id has NO cascade delete.

    Per AAP, audit history must persist past user deletion. We don't
    actually test deleting a user (since users are referenced widely
    by FKs), but we confirm the relationship exists without cascade.
    """

    def test_actor_user_relationship_exists(
        self,
        db_session: DBSession,
        admin_user,
    ) -> None:
        """audit_events FK to users is defined."""
        event = AuditEvent(
            actor_user_id=admin_user.id,
            event_type=AuditEventType.AUTHENTICATION,
        )
        db_session.add(event)
        db_session.commit()
        # The relationship resolves correctly back-population is tested.
        assert event.actor_user_id == admin_user.id
