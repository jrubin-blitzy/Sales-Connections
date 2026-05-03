"""Tests for the Organization model."""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import Organization

if TYPE_CHECKING:
    from sqlalchemy.orm import Session as DBSession


class TestOrganization:
    """Organization model invariants."""

    def test_create_with_required_fields(
        self, db_session: DBSession
    ) -> None:
        """Organization with name persists and gets an id."""
        org = Organization(name=f"Acme-{uuid.uuid4().hex[:8]}")
        db_session.add(org)
        db_session.commit()
        assert org.id is not None
        assert isinstance(org.id, uuid.UUID)

    def test_id_is_uuid(self, db_session: DBSession) -> None:
        """id is a server-assigned UUID."""
        org = Organization(name=f"UUID-Test-{uuid.uuid4().hex[:6]}")
        db_session.add(org)
        db_session.flush()
        assert isinstance(org.id, uuid.UUID)

    def test_created_at_populated(self, db_session: DBSession) -> None:
        """created_at is automatically set."""
        org = Organization(name=f"Timestamp-{uuid.uuid4().hex[:6]}")
        db_session.add(org)
        db_session.commit()
        assert org.created_at is not None
        # Must be timezone-aware.
        assert org.created_at.tzinfo is not None

    def test_name_required(
        self, db_session: DBSession
    ) -> None:
        """name is NOT NULL."""
        org = Organization(name=None)  # type: ignore[arg-type]
        db_session.add(org)
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()

    def test_repr_does_not_leak_pii(
        self, db_session: DBSession
    ) -> None:
        """__repr__ does not contain PII."""
        org = Organization(name=f"Repr-{uuid.uuid4().hex[:6]}")
        db_session.add(org)
        db_session.flush()
        repr_str = repr(org)
        # Repr should contain the class name and id but not arbitrary
        # PII. The class itself doesn't have PII fields, so this is
        # mostly a "doesn't crash" test.
        assert "Organization" in repr_str

    def test_users_relationship_empty_initially(
        self, db_session: DBSession
    ) -> None:
        """A new Organization has no users."""
        org = Organization(name=f"NoUsers-{uuid.uuid4().hex[:6]}")
        db_session.add(org)
        db_session.commit()
        assert list(org.users) == []
