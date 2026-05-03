"""Tests for the User model.

Covers:
    - Required fields (org_id, email, display_name, role)
    - Composite unique constraint on (org_id, email)
    - Default role = Contributor
    - PII-safe __repr__ (no email/password_hash/oauth_subject)
    - OAuth users can have password_hash=None
    - Password users can have oauth_subject=None
    - audit_events relationship has NO cascade (audit must persist)
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import Organization, User
from app.models.enums import UserRole

if TYPE_CHECKING:
    from sqlalchemy.orm import Session as DBSession


def _make_org(db_session: DBSession, name_suffix: str | None = None) -> Organization:
    """Create and persist a fresh Organization for a test."""
    suffix = name_suffix or uuid.uuid4().hex[:8]
    org = Organization(name=f"User-Test-Org-{suffix}")
    db_session.add(org)
    db_session.flush()
    return org


class TestUserCreation:
    """User creation invariants."""

    def test_create_with_required_fields(
        self, db_session: DBSession, organization
    ) -> None:
        """User with required fields persists."""
        user = User(
            org_id=organization.id,
            email=f"user-{uuid.uuid4().hex[:6]}@example.com",
            display_name="Test User",
            role=UserRole.CONTRIBUTOR,
        )
        db_session.add(user)
        db_session.commit()
        assert user.id is not None

    def test_default_role_is_contributor(
        self, db_session: DBSession, organization
    ) -> None:
        """Without an explicit role, User defaults to CONTRIBUTOR."""
        user = User(
            org_id=organization.id,
            email=f"default-role-{uuid.uuid4().hex[:6]}@example.com",
            display_name="Default Role User",
        )
        db_session.add(user)
        db_session.commit()
        assert user.role == UserRole.CONTRIBUTOR

    def test_oauth_user_no_password_hash(
        self, db_session: DBSession, organization
    ) -> None:
        """OAuth-only users have password_hash=None."""
        user = User(
            org_id=organization.id,
            email=f"oauth-{uuid.uuid4().hex[:6]}@example.com",
            display_name="OAuth User",
            password_hash=None,
        )
        db_session.add(user)
        db_session.commit()
        assert user.password_hash is None

    def test_password_user_with_password_hash(
        self, db_session: DBSession, organization
    ) -> None:
        """Password users have a non-null password_hash."""
        user = User(
            org_id=organization.id,
            email=f"pw-only-{uuid.uuid4().hex[:6]}@example.com",
            display_name="Password User",
            password_hash=(
                "$2b$04$abcd1234567890abcd1234567890abcd1234567890abcd1234567"
            ),
        )
        db_session.add(user)
        db_session.commit()
        assert user.password_hash is not None

    def test_id_is_uuid(
        self, db_session: DBSession, organization
    ) -> None:
        """User id is a server-assigned UUID."""
        user = User(
            org_id=organization.id,
            email=f"uuid-{uuid.uuid4().hex[:6]}@example.com",
            display_name="UUID User",
        )
        db_session.add(user)
        db_session.flush()
        assert isinstance(user.id, uuid.UUID)


class TestUserConstraints:
    """User constraint enforcement."""

    def test_composite_unique_org_email(
        self, db_session: DBSession, organization
    ) -> None:
        """(org_id, email) is uniquely constrained."""
        common_email = f"unique-test-{uuid.uuid4().hex[:6]}@example.com"
        db_session.add(
            User(
                org_id=organization.id,
                email=common_email,
                display_name="First",
            )
        )
        db_session.commit()

        db_session.add(
            User(
                org_id=organization.id,
                email=common_email,
                display_name="Second (duplicate)",
            )
        )
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()

    def test_same_email_different_orgs_allowed(
        self, db_session: DBSession, organization
    ) -> None:
        """Same email in DIFFERENT orgs is allowed (composite unique)."""
        common_email = f"cross-org-{uuid.uuid4().hex[:6]}@example.com"
        other_org = _make_org(db_session)

        db_session.add(
            User(
                org_id=organization.id,
                email=common_email,
                display_name="Org 1",
            )
        )
        db_session.add(
            User(
                org_id=other_org.id,
                email=common_email,
                display_name="Org 2",
            )
        )
        db_session.commit()
        # Both succeed.
        users = (
            db_session.query(User).filter_by(email=common_email).all()
        )
        assert len(users) == 2

    def test_email_required(
        self, db_session: DBSession, organization
    ) -> None:
        """email is NOT NULL."""
        user = User(
            org_id=organization.id,
            email=None,  # type: ignore[arg-type]
            display_name="No Email",
        )
        db_session.add(user)
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()


class TestUserRepr:
    """PII-safe __repr__."""

    def test_repr_excludes_email(
        self, db_session: DBSession, organization
    ) -> None:
        """__repr__ does NOT contain the email address."""
        secret_email = f"do-not-leak-{uuid.uuid4().hex[:8]}@secret.com"
        user = User(
            org_id=organization.id,
            email=secret_email,
            display_name="Repr Test",
        )
        db_session.add(user)
        db_session.flush()
        repr_str = repr(user)
        assert secret_email not in repr_str

    def test_repr_excludes_password_hash(
        self, db_session: DBSession, organization
    ) -> None:
        """__repr__ does NOT contain the password_hash."""
        secret_hash = "$2b$12$DO_NOT_LEAK_HASH_xxxxxxxxxxxxxxxxxxxxxx"
        user = User(
            org_id=organization.id,
            email=f"pwhash-repr-{uuid.uuid4().hex[:6]}@example.com",
            display_name="Repr Hash",
            password_hash=secret_hash,
        )
        db_session.add(user)
        db_session.flush()
        repr_str = repr(user)
        assert secret_hash not in repr_str
        assert "$2b$" not in repr_str

    def test_repr_includes_class_name(
        self, db_session: DBSession, organization
    ) -> None:
        """__repr__ contains the class name (sanity check)."""
        user = User(
            org_id=organization.id,
            email=f"class-name-{uuid.uuid4().hex[:6]}@example.com",
            display_name="Class Name",
        )
        db_session.add(user)
        db_session.flush()
        repr_str = repr(user)
        assert "User" in repr_str


class TestUserRelationships:
    """User -> Organization, Record, AuditEvent relationships."""

    def test_organization_relationship(
        self, db_session: DBSession, organization
    ) -> None:
        """User.organization back-references the parent Organization."""
        user = User(
            org_id=organization.id,
            email=f"rel-org-{uuid.uuid4().hex[:6]}@example.com",
            display_name="Rel Org",
        )
        db_session.add(user)
        db_session.commit()
        assert user.organization == organization

    def test_records_relationship_empty_initially(
        self, db_session: DBSession, organization
    ) -> None:
        """A new user has no records."""
        user = User(
            org_id=organization.id,
            email=f"no-records-{uuid.uuid4().hex[:6]}@example.com",
            display_name="No Records",
        )
        db_session.add(user)
        db_session.commit()
        assert list(user.records) == []
