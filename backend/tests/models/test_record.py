"""Tests for the Record model.

Covers:
    - Required fields persist
    - Default outreach_status = NOT_STARTED
    - Soft-delete via deleted_at
    - Org-scoping via org_id FK
    - Owner attribution via owner_user_id FK
    - Unique partial index on (org_id, normalized_linkedin_url)
      WHERE deleted_at IS NULL
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import Record
from app.models.enums import InvolvementType, OutreachStatus

if TYPE_CHECKING:
    from sqlalchemy.orm import Session as DBSession


class TestRecordCreation:
    """Record creation invariants."""

    def test_create_with_required_fields(
        self,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """Record with all required fields persists."""
        record = Record(
            org_id=organization.id,
            owner_user_id=contributor_user.id,
            owner_display_name=contributor_user.display_name,
            full_name="Jane Doe",
            linkedin_url="https://www.linkedin.com/in/janedoe/",
            normalized_linkedin_url="https://linkedin.com/in/janedoe",
            company="Acme Corp",
            job_title="VP Engineering",
            relationship_context="College classmate",
            involvement=InvolvementType.WARM_INTRO,
        )
        db_session.add(record)
        db_session.commit()
        assert record.id is not None

    def test_default_outreach_status(
        self,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """outreach_status defaults to NOT_STARTED."""
        record = Record(
            org_id=organization.id,
            owner_user_id=contributor_user.id,
            owner_display_name=contributor_user.display_name,
            full_name="Default Status",
            linkedin_url="https://www.linkedin.com/in/default-status/",
            normalized_linkedin_url=(
                "https://linkedin.com/in/default-status"
            ),
            company="Test Co",
            job_title="Tester",
            relationship_context="Test",
            involvement=InvolvementType.TARGET_ONLY,
        )
        db_session.add(record)
        db_session.commit()
        assert record.outreach_status == OutreachStatus.NOT_STARTED

    def test_id_is_uuid(
        self,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """Record id is a server-assigned UUID."""
        record = Record(
            org_id=organization.id,
            owner_user_id=contributor_user.id,
            owner_display_name=contributor_user.display_name,
            full_name="UUID Test",
            linkedin_url="https://www.linkedin.com/in/uuid-test/",
            normalized_linkedin_url="https://linkedin.com/in/uuid-test",
            company="UUID Co",
            job_title="Tester",
            relationship_context="Test",
            involvement=InvolvementType.WARM_INTRO,
        )
        db_session.add(record)
        db_session.flush()
        assert isinstance(record.id, uuid.UUID)

    def test_submission_date_set_on_create(
        self,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """submission_date is set on creation."""
        record = Record(
            org_id=organization.id,
            owner_user_id=contributor_user.id,
            owner_display_name=contributor_user.display_name,
            full_name="Submission Test",
            linkedin_url="https://www.linkedin.com/in/sub-test/",
            normalized_linkedin_url="https://linkedin.com/in/sub-test",
            company="Sub Co",
            job_title="Test",
            relationship_context="Test",
            involvement=InvolvementType.WARM_INTRO,
        )
        db_session.add(record)
        db_session.commit()
        assert record.submission_date is not None
        assert record.submission_date.tzinfo is not None

    def test_deleted_at_null_by_default(
        self,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """deleted_at is NULL on create (record is active)."""
        record = Record(
            org_id=organization.id,
            owner_user_id=contributor_user.id,
            owner_display_name=contributor_user.display_name,
            full_name="Active",
            linkedin_url="https://www.linkedin.com/in/active/",
            normalized_linkedin_url="https://linkedin.com/in/active",
            company="A",
            job_title="A",
            relationship_context="A",
            involvement=InvolvementType.WARM_INTRO,
        )
        db_session.add(record)
        db_session.commit()
        assert record.deleted_at is None


class TestRecordSoftDelete:
    """Soft delete via deleted_at."""

    def test_set_deleted_at(
        self,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """Setting deleted_at marks the record as soft-deleted."""
        record = Record(
            org_id=organization.id,
            owner_user_id=contributor_user.id,
            owner_display_name=contributor_user.display_name,
            full_name="Soft Delete",
            linkedin_url="https://www.linkedin.com/in/soft-delete/",
            normalized_linkedin_url="https://linkedin.com/in/soft-delete",
            company="A",
            job_title="A",
            relationship_context="A",
            involvement=InvolvementType.WARM_INTRO,
        )
        db_session.add(record)
        db_session.commit()
        record.deleted_at = datetime.now(UTC)
        db_session.commit()
        assert record.deleted_at is not None


class TestRecordUniqueLinkedInUrl:
    """Unique partial index on (org_id, normalized_linkedin_url)
    WHERE deleted_at IS NULL.
    """

    def test_duplicate_url_in_same_org_rejected(
        self,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """Two active records with the same normalized URL in same
        org are rejected."""
        url = f"https://linkedin.com/in/dup-{uuid.uuid4().hex[:8]}"
        common_kwargs = {
            "org_id": organization.id,
            "owner_user_id": contributor_user.id,
            "owner_display_name": contributor_user.display_name,
            "linkedin_url": url + "/",
            "normalized_linkedin_url": url,
            "company": "C",
            "job_title": "T",
            "relationship_context": "R",
            "involvement": InvolvementType.WARM_INTRO,
        }
        db_session.add(Record(full_name="First", **common_kwargs))
        db_session.commit()
        db_session.add(Record(full_name="Second (dup)", **common_kwargs))
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()

    def test_soft_deleted_url_can_be_reused(
        self,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """A soft-deleted record's URL can be reused (partial index)."""
        url = f"https://linkedin.com/in/reuse-{uuid.uuid4().hex[:8]}"
        # Insert and soft-delete first record.
        first = Record(
            org_id=organization.id,
            owner_user_id=contributor_user.id,
            owner_display_name=contributor_user.display_name,
            full_name="First (deleted)",
            linkedin_url=url + "/",
            normalized_linkedin_url=url,
            company="C",
            job_title="T",
            relationship_context="R",
            involvement=InvolvementType.WARM_INTRO,
        )
        db_session.add(first)
        db_session.commit()
        first.deleted_at = datetime.now(UTC)
        db_session.commit()

        # Now insert second active record with same URL.
        second = Record(
            org_id=organization.id,
            owner_user_id=contributor_user.id,
            owner_display_name=contributor_user.display_name,
            full_name="Second (active)",
            linkedin_url=url + "/",
            normalized_linkedin_url=url,
            company="C",
            job_title="T",
            relationship_context="R",
            involvement=InvolvementType.WARM_INTRO,
        )
        db_session.add(second)
        # This MUST succeed because the unique index is partial
        # (WHERE deleted_at IS NULL).
        db_session.commit()
        assert second.id is not None

    def test_same_url_different_orgs_allowed(
        self,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """Same URL in DIFFERENT orgs is allowed."""
        from tests.factories import OrganizationFactory, UserFactory  # noqa: PLC0415

        other_org = OrganizationFactory()
        other_user = UserFactory(organization=other_org)

        url = f"https://linkedin.com/in/cross-org-{uuid.uuid4().hex[:8]}"
        # Org 1
        db_session.add(
            Record(
                org_id=organization.id,
                owner_user_id=contributor_user.id,
                owner_display_name=contributor_user.display_name,
                full_name="Org 1",
                linkedin_url=url + "/",
                normalized_linkedin_url=url,
                company="C",
                job_title="T",
                relationship_context="R",
                involvement=InvolvementType.WARM_INTRO,
            )
        )
        # Org 2
        db_session.add(
            Record(
                org_id=other_org.id,
                owner_user_id=other_user.id,
                owner_display_name=other_user.display_name,
                full_name="Org 2",
                linkedin_url=url + "/",
                normalized_linkedin_url=url,
                company="C",
                job_title="T",
                relationship_context="R",
                involvement=InvolvementType.WARM_INTRO,
            )
        )
        db_session.commit()


class TestRecordRelationships:
    """Record relationships."""

    def test_organization_back_reference(
        self,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """Record.organization back-references the parent."""
        record = Record(
            org_id=organization.id,
            owner_user_id=contributor_user.id,
            owner_display_name=contributor_user.display_name,
            full_name="Rel Test",
            linkedin_url="https://www.linkedin.com/in/rel-test/",
            normalized_linkedin_url="https://linkedin.com/in/rel-test",
            company="C",
            job_title="T",
            relationship_context="R",
            involvement=InvolvementType.WARM_INTRO,
        )
        db_session.add(record)
        db_session.commit()
        assert record.organization == organization

    def test_owner_back_reference(
        self,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """Record.owner back-references the User."""
        record = Record(
            org_id=organization.id,
            owner_user_id=contributor_user.id,
            owner_display_name=contributor_user.display_name,
            full_name="Owner Test",
            linkedin_url="https://www.linkedin.com/in/owner-test/",
            normalized_linkedin_url="https://linkedin.com/in/owner-test",
            company="C",
            job_title="T",
            relationship_context="R",
            involvement=InvolvementType.WARM_INTRO,
        )
        db_session.add(record)
        db_session.commit()
        assert record.owner == contributor_user
