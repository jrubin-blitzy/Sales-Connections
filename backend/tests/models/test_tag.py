"""Tests for the Tag and RecordTag models."""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import Record, RecordTag, Tag
from app.models.enums import InvolvementType

if TYPE_CHECKING:
    from sqlalchemy.orm import Session as DBSession


class TestTag:
    """Tag model invariants."""

    def test_create_with_required_fields(self, db_session: DBSession, organization) -> None:
        """Tag with org_id and name persists."""
        tag = Tag(org_id=organization.id, name=f"Industry-{uuid.uuid4().hex[:6]}")
        db_session.add(tag)
        db_session.commit()
        assert tag.id is not None

    def test_org_scoped_unique_name(self, db_session: DBSession, organization) -> None:
        """(org_id, name) is uniquely constrained."""
        common_name = f"Logistics-{uuid.uuid4().hex[:6]}"
        db_session.add(Tag(org_id=organization.id, name=common_name))
        db_session.commit()

        db_session.add(Tag(org_id=organization.id, name=common_name))
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()

    def test_same_name_different_orgs_allowed(self, db_session: DBSession, organization) -> None:
        """Same name in DIFFERENT orgs is allowed."""
        from tests.factories import OrganizationFactory  # noqa: PLC0415

        other_org = OrganizationFactory()
        common_name = f"SaaS-{uuid.uuid4().hex[:6]}"

        db_session.add(Tag(org_id=organization.id, name=common_name))
        db_session.add(Tag(org_id=other_org.id, name=common_name))
        db_session.commit()

        tags = db_session.query(Tag).filter_by(name=common_name).all()
        assert len(tags) == 2

    def test_id_is_uuid(self, db_session: DBSession, organization) -> None:
        """Tag id is UUID."""
        tag = Tag(org_id=organization.id, name=f"UUID-Tag-{uuid.uuid4().hex[:6]}")
        db_session.add(tag)
        db_session.flush()
        assert isinstance(tag.id, uuid.UUID)


class TestRecordTag:
    """RecordTag association model."""

    def test_associate_record_with_tag(
        self,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """A RecordTag row links a Record to a Tag."""
        record = Record(
            org_id=organization.id,
            owner_user_id=contributor_user.id,
            owner_display_name=contributor_user.display_name,
            full_name="Tagged Person",
            linkedin_url="https://www.linkedin.com/in/tagged/",
            normalized_linkedin_url="https://linkedin.com/in/tagged",
            company="Tag Co",
            job_title="Tagger",
            relationship_context="r",
            involvement=InvolvementType.WARM_INTRO,
        )
        tag = Tag(
            org_id=organization.id,
            name=f"Tag-Assoc-{uuid.uuid4().hex[:6]}",
        )
        db_session.add(record)
        db_session.add(tag)
        db_session.flush()

        association = RecordTag(record_id=record.id, tag_id=tag.id)
        db_session.add(association)
        db_session.commit()

        # Composite PK: lookup by both keys.
        looked_up = db_session.get(RecordTag, (record.id, tag.id))
        assert looked_up is not None

    def test_duplicate_association_rejected(
        self,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """Same record/tag pair cannot be associated twice."""
        record = Record(
            org_id=organization.id,
            owner_user_id=contributor_user.id,
            owner_display_name=contributor_user.display_name,
            full_name="Dup Assoc",
            linkedin_url="https://www.linkedin.com/in/dup-assoc/",
            normalized_linkedin_url="https://linkedin.com/in/dup-assoc",
            company="C",
            job_title="T",
            relationship_context="R",
            involvement=InvolvementType.WARM_INTRO,
        )
        tag = Tag(
            org_id=organization.id,
            name=f"Tag-Dup-{uuid.uuid4().hex[:6]}",
        )
        db_session.add_all([record, tag])
        db_session.flush()

        db_session.add(RecordTag(record_id=record.id, tag_id=tag.id))
        db_session.commit()

        db_session.add(RecordTag(record_id=record.id, tag_id=tag.id))
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()
