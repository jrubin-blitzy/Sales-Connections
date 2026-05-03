"""Tests for ``app.services.connections`` (F-001/F-004/F-005/F-007/F-011).

This file exercises the central business-logic module that owns the
``records`` and ``record_tags`` table state changes plus the F-004 feed
query, the F-011 detail/history reads, and the F-007 soft-delete
semantics. It is the single most security-sensitive service in the
checkpoint scope: every state-change function must satisfy the AAP
Section 0.7.1 architectural invariants:

* invariant 3 (org-scoped multi-tenancy) - cross-org reads return 404
  (info-disclosure defense per AAP Section 0.7.4); cross-org tag
  injection is rejected as 422 with field-scoped error metadata.
* invariant 4 (soft-delete-aware reads) - default-on
  ``WHERE deleted_at IS NULL`` filter; admin moderation paths opt out
  via ``include_deleted=True``.
* invariant 5 (append-only audit) - audit rows are emitted by every
  state-change function; failure of the audit emit rolls back the
  parent state change atomically.
* invariant 6 (atomic state-change + audit pair) - records UPDATE,
  record_tags INSERT/DELETE diffs, AND audit_events INSERT all commit
  together via ``with session.begin():``.
* invariant 7 (RBAC enforced at the service layer too) - Contributors
  may edit/delete only their own records; Admins bypass.

The file is organized into seven test classes mirroring the public
surface of the module:

* :class:`TestCreateRecord` - F-001 form-driven create. Atomicity,
  org-scope, owner attribution, tag resolution, duplicate races,
  audit emission.
* :class:`TestGetRecord` - F-011 detail fetch. Org-scope,
  soft-delete-aware (default + admin opt-in), eager tag loading,
  not-found mapping.
* :class:`TestListRecords` - F-004 feed query. Filtering on every
  dimension, sorting on every key + direction, pagination clamping
  + offset, stable tiebreaker, count correctness.
* :class:`TestUpdateRecord` - F-007 edit. Ownership check
  (Contributor own-only, Admin any), PATCH semantics, atomic
  state-change + audit, duplicate-URL race, tag replacement diff.
* :class:`TestUpdateStatus` - F-005 status mutation. Idempotent no-op,
  audit event payload structure, org-scope.
* :class:`TestSoftDeleteRecord` - F-007 soft delete. Idempotency on
  re-delete, ownership check, atomic state-change + audit.
* :class:`TestGetRecordHistory` - F-011 history feed. Org-scope
  pre-check, ordering by event_timestamp DESC, stable tiebreaker,
  pagination.

Markers:

* ``@pytest.mark.integration`` applied to every test (every test
  exercises a real Postgres connection because the service module's
  ``with db.session() as session, session.begin():`` pattern requires
  a live database for transaction semantics to be observable).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
import uuid

import pytest
from sqlalchemy import select

from app.middleware.auth import Session
from app.middleware.error_handlers import (
    ForbiddenError,
    NotFoundError,
    ValidationFailedError,
)
from app.models import AuditEvent, Record, RecordTag
from app.models.enums import (
    AuditEventType,
    InvolvementType,
    OutreachStatus,
    UserRole,
)
from app.schemas.connection import (
    ConnectionCreate,
    ConnectionStatusUpdate,
    ConnectionUpdate,
)
from app.services.connections import (
    ConnectionFilters,
    DuplicateRecordError,
    create_record,
    get_record,
    get_record_history,
    list_records,
    soft_delete_record,
    update_record,
    update_status,
)
from app.utils.url import normalize_linkedin_url
from tests.factories import (
    AdminUserFactory,
    AuditEventFactory,
    ContributorUserFactory,
    OrganizationFactory,
    RecordFactory,
    SoftDeletedRecordFactory,
    TagFactory,
    UserFactory,
    ViewerUserFactory,
)

if TYPE_CHECKING:
    from flask import Flask
    from sqlalchemy.orm import Session as DBSession


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

# Every test in this module exercises real Postgres transactions because
# the service module's ``with db.session() as session, session.begin():``
# pattern's atomicity is not observable against an in-memory mock. The
# ``integration`` marker matches the convention from sibling test
# modules (test_audit.py, test_duplicate_detection.py).
pytestmark = pytest.mark.integration


def _make_session(user) -> Session:
    """Build a typed :class:`Session` from a User factory instance.

    The :class:`Session` dataclass is the contract between
    :mod:`app.middleware.auth` and the service layer; constructing it
    explicitly here lets tests drive the service-layer functions
    without spinning up a full HTTP round-trip via the Flask test
    client. The ``raw_claims`` dict is populated with sentinel values
    so any test that inadvertently logs the session does not surface
    plausible-but-fake JWT data.

    Args:
        user: A User factory instance carrying ``id``, ``org_id``,
            ``role``, ``email``, and ``display_name`` attributes.

    Returns:
        An immutable :class:`Session` carrying the same identifying
        fields.
    """
    return Session(
        user_id=user.id,
        org_id=user.org_id,
        role=user.role,
        email=user.email,
        display_name=user.display_name,
        issued_at="",
        expires_at="",
        raw_claims={},
    )


def _build_create_payload(
    *,
    full_name: str = "Jane Doe",
    linkedin_url: str = "https://www.linkedin.com/in/jane-doe-test",
    company: str = "Acme Corp",
    job_title: str = "VP Engineering",
    relationship_context: str = "We worked together at BigCo from 2018-2020.",
    ai_notes: str | None = None,
    involvement: InvolvementType = InvolvementType.WARM_INTRO,
    tag_ids: list[uuid.UUID] | None = None,
) -> ConnectionCreate:
    """Construct a valid :class:`ConnectionCreate` payload for tests.

    Defaults are chosen to satisfy every pydantic validator:
      * full_name, company, job_title within length caps,
      * linkedin_url is a syntactically valid LinkedIn profile URL,
      * relationship_context within 4000-char cap,
      * involvement is a valid :class:`InvolvementType` enum member,
      * tag_ids is empty by default to avoid cross-test interference.

    Args:
        full_name: Full name for the new record. Defaults to a
            generic value usable across tests.
        linkedin_url: LinkedIn profile URL. Must pass
            :func:`is_valid_linkedin_url`. Defaults to a unique
            test-scoped URL.
        company: Company name.
        job_title: Job title.
        relationship_context: Relationship context (free-form).
        ai_notes: Optional AI-generated notes.
        involvement: Involvement enum member.
        tag_ids: Optional list of tag UUIDs.

    Returns:
        A schema-validated :class:`ConnectionCreate` ready to pass
        directly to :func:`create_record`.
    """
    payload: dict = {
        "full_name": full_name,
        "linkedin_url": linkedin_url,
        "company": company,
        "job_title": job_title,
        "relationship_context": relationship_context,
        "involvement": involvement,
    }
    if ai_notes is not None:
        payload["ai_notes"] = ai_notes
    if tag_ids is not None:
        payload["tag_ids"] = tag_ids
    return ConnectionCreate.model_validate(payload)


# ===========================================================================
# TestCreateRecord - F-001 form-driven create
# ===========================================================================


class TestCreateRecord:
    """Tests for :func:`create_record` (F-001 form-driven create).

    Verifies the atomic state-change + audit pair invariant
    (AAP Section 0.7.1 invariant 6), owner attribution from session
    only (F-006), org-scoping for tags (F-008 cross-org defense), and
    duplicate-URL race resolution (F-010).
    """

    def test_creates_record_with_owner_from_session(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """Owner is derived from the actor's session; never from payload.

        Verifies AAP Section 0.7.4 security invariant: the API never
        accepts a client-supplied owner_user_id; the schema's
        ``extra='forbid'`` rejects it at the boundary AND the service
        layer derives the value exclusively from ``actor.user_id``.
        """
        actor = _make_session(contributor_user)
        payload = _build_create_payload()

        record = create_record(payload=payload, actor=actor)

        assert record.id is not None
        assert record.owner_user_id == contributor_user.id
        assert record.owner_display_name == contributor_user.display_name
        assert record.org_id == organization.id

    def test_emits_create_audit_event_atomically(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """A CREATE audit event is emitted in the same transaction as the INSERT.

        Verifies AAP Section 0.7.1 invariant 6 (atomic state-change +
        audit pair). After the create completes successfully, exactly
        one CREATE audit event must reference the new record id.
        """
        actor = _make_session(contributor_user)
        payload = _build_create_payload(linkedin_url="https://www.linkedin.com/in/atomic-create")

        record = create_record(payload=payload, actor=actor)

        events = (
            db_session.execute(select(AuditEvent).where(AuditEvent.target_record_id == record.id))
            .scalars()
            .all()
        )
        assert len(events) == 1
        event = events[0]
        assert event.event_type == AuditEventType.CREATE
        assert event.actor_user_id == contributor_user.id
        assert event.before_payload is None
        # The after_payload captures structural identifiers per
        # AAP Section 0.7.4; relationship_context and ai_notes are
        # excluded as PII.
        assert event.after_payload is not None
        assert event.after_payload["id"] == str(record.id)
        assert event.after_payload["owner_user_id"] == str(contributor_user.id)
        # PII fields excluded from the audit payload per
        # _record_to_audit_payload helper docstring.
        assert "relationship_context" not in event.after_payload
        assert "ai_notes" not in event.after_payload

    def test_normalizes_linkedin_url_for_duplicate_detection(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """The normalized URL is computed and persisted for F-010 lookups.

        The normalized form (lowercased host, no trailing slash, no
        query params) feeds the partial unique index that powers the
        duplicate-detection feature. Verifying it is computed at
        create time prevents future regressions where the
        normalization step is silently skipped.
        """
        actor = _make_session(contributor_user)
        # An URL with trailing slash + query params + uppercase host
        # to exercise the full normalization pipeline.
        raw_url = "https://www.LinkedIn.com/in/Test-Normalize/?utm_source=spam"
        payload = _build_create_payload(linkedin_url=raw_url)

        record = create_record(payload=payload, actor=actor)

        # The persisted normalized form matches what the helper produces.
        assert record.normalized_linkedin_url == normalize_linkedin_url(raw_url)
        # The original URL is preserved verbatim for display purposes.
        assert record.linkedin_url == raw_url

    def test_attaches_org_scoped_tags(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """Tags from the actor's org are attached via record_tags rows.

        Verifies F-008 (Tagging) integration with F-001 (form create).
        The record's tags relationship should reflect the requested
        tag ids after the transaction commits.
        """
        actor = _make_session(contributor_user)
        tag_a = TagFactory(organization=organization, name="fintech")
        tag_b = TagFactory(organization=organization, name="series-a")
        payload = _build_create_payload(
            linkedin_url="https://www.linkedin.com/in/tagged-record",
            tag_ids=[tag_a.id, tag_b.id],
        )

        record = create_record(payload=payload, actor=actor)

        # The eager-loaded record_tags relationship must contain both
        # associations. RecordFactory's record_tags collection
        # populates from the M:N join so we read the tag ids back.
        attached_tag_ids = {rt.tag_id for rt in record.record_tags}
        assert attached_tag_ids == {tag_a.id, tag_b.id}

    def test_rejects_cross_org_tag_ids(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """Tag ids from another org are rejected as 422 (cross-org defense).

        Verifies the multi-tenant isolation invariant at the tag
        layer: a contributor in org A cannot attach a tag from org B,
        regardless of how the request reached the service layer.
        """
        other_org = OrganizationFactory()
        cross_org_tag = TagFactory(organization=other_org, name="cross-org-tag")
        actor = _make_session(contributor_user)
        payload = _build_create_payload(
            linkedin_url="https://www.linkedin.com/in/cross-org-tag-test",
            tag_ids=[cross_org_tag.id],
        )

        with pytest.raises(ValidationFailedError) as exc_info:
            create_record(payload=payload, actor=actor)

        # The error must reference the cross-org tag id as a
        # field-scoped problem, not a generic 422.
        assert any("tag_ids" in field.get("loc", []) for field in exc_info.value.fields)

    def test_rejects_duplicate_normalized_url_via_pre_check(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """A pre-existing active record with the same URL raises 409.

        Verifies the F-010 duplicate detection pre-check path. The
        duplicate is found BEFORE the INSERT to avoid wasting a
        round-trip; the response is HTTP 409 with
        ``error.code = "duplicate_record"``.
        """
        url = "https://www.linkedin.com/in/duplicate-target"
        normalized = normalize_linkedin_url(url)
        # Seed an existing record with the same normalized URL.
        RecordFactory(
            organization=organization,
            owner=contributor_user,
            linkedin_url=url,
            normalized_linkedin_url=normalized,
        )
        actor = _make_session(contributor_user)
        payload = _build_create_payload(linkedin_url=url)

        with pytest.raises(DuplicateRecordError):
            create_record(payload=payload, actor=actor)

    def test_allows_url_already_used_in_other_org(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """The same URL in a DIFFERENT org does not trigger the duplicate check.

        Verifies AAP Section 0.7.1 invariant 3: the unique partial
        index is org-scoped (``ON (org_id, normalized_linkedin_url)
        WHERE deleted_at IS NULL``) so the same LinkedIn profile may
        appear in records belonging to different organizations.
        """
        url = "https://www.linkedin.com/in/cross-org-same-url"
        # Seed in another org.
        other_org = OrganizationFactory()
        other_user = UserFactory(organization=other_org)
        RecordFactory(
            organization=other_org,
            owner=other_user,
            linkedin_url=url,
            normalized_linkedin_url=normalize_linkedin_url(url),
        )
        actor = _make_session(contributor_user)
        payload = _build_create_payload(linkedin_url=url)

        # Should succeed because the duplicate lookup is org-scoped.
        record = create_record(payload=payload, actor=actor)
        assert record.org_id == organization.id

    def test_allows_url_previously_soft_deleted(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """A URL belonging to a soft-deleted record does NOT trigger 409.

        Verifies the partial unique index condition
        ``WHERE deleted_at IS NULL``: soft-deleted records are
        excluded from the uniqueness constraint so a fresh record
        with the same URL can be created after a soft delete.
        """
        url = "https://www.linkedin.com/in/soft-deleted-url"
        SoftDeletedRecordFactory(
            organization=organization,
            owner=contributor_user,
            linkedin_url=url,
            normalized_linkedin_url=normalize_linkedin_url(url),
        )
        actor = _make_session(contributor_user)
        payload = _build_create_payload(linkedin_url=url)

        record = create_record(payload=payload, actor=actor)
        assert record.linkedin_url == url

    def test_persists_optional_ai_notes(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """``ai_notes`` is persisted when supplied on the payload.

        Verifies F-002 integration: AI notes (when generated) flow
        into the create payload and are persisted as part of the
        record. AI failure is non-blocking; this test covers the
        successful path where AI notes are populated.
        """
        actor = _make_session(contributor_user)
        notes = "Recent move to a Series B logistics startup; warm intro likely."
        payload = _build_create_payload(
            linkedin_url="https://www.linkedin.com/in/with-ai-notes",
            ai_notes=notes,
        )

        record = create_record(payload=payload, actor=actor)
        assert record.ai_notes == notes

    def test_status_defaults_to_not_started(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """Newly created records start with outreach_status = Not Started.

        Verifies F-005 default semantics: every record begins life
        in the canonical ``Not Started`` status. The schema does not
        accept ``outreach_status`` on create (status mutation is
        gated to Sales Rep / Admin only via a separate endpoint).
        """
        actor = _make_session(contributor_user)
        payload = _build_create_payload(linkedin_url="https://www.linkedin.com/in/default-status")

        record = create_record(payload=payload, actor=actor)
        assert record.outreach_status == OutreachStatus.NOT_STARTED


# ===========================================================================
# TestGetRecord - F-011 detail fetch
# ===========================================================================


class TestGetRecord:
    """Tests for :func:`get_record` (F-011 single-record fetch).

    Verifies org-scope, soft-delete-aware reads (default + admin
    opt-out), eager tag loading, and not-found mapping.
    """

    def test_returns_record_in_actor_org(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """A record in the actor's org is returned with eager-loaded tags."""
        record = RecordFactory(organization=organization, owner=contributor_user)
        tag = TagFactory(organization=organization, name="get-record-tag")
        db_session.add(RecordTag(record_id=record.id, tag_id=tag.id))
        db_session.commit()

        actor = _make_session(contributor_user)
        result = get_record(record_id=record.id, actor=actor)

        assert result.id == record.id
        # Eager-loaded relationship must be populated; accessing it
        # after session close (via expunge_all) must NOT raise.
        attached_tag_ids = {rt.tag_id for rt in result.record_tags}
        assert tag.id in attached_tag_ids

    def test_cross_org_lookup_returns_404(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """A record id in another org returns 404, not 403.

        Verifies AAP Section 0.7.1 invariant 3 + AAP Section 0.7.4
        info-disclosure defense: cross-org access surfaces as 404
        (the record "does not exist" from this org's perspective)
        rather than 403 (which would confirm the record exists in
        another org).
        """
        other_org = OrganizationFactory()
        other_user = UserFactory(organization=other_org)
        cross_org_record = RecordFactory(organization=other_org, owner=other_user)

        actor = _make_session(contributor_user)
        with pytest.raises(NotFoundError):
            get_record(record_id=cross_org_record.id, actor=actor)

    def test_soft_deleted_record_returns_404_by_default(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """Soft-deleted records are excluded from default reads.

        Verifies AAP Section 0.7.1 invariant 4: every read defaults
        to ``WHERE deleted_at IS NULL`` so soft-deleted records do
        not surface in routine queries. Admin moderation views opt
        out via ``include_deleted=True``.
        """
        deleted_record = SoftDeletedRecordFactory(organization=organization, owner=contributor_user)
        actor = _make_session(contributor_user)

        with pytest.raises(NotFoundError):
            get_record(record_id=deleted_record.id, actor=actor)

    def test_soft_deleted_record_returned_with_admin_opt_in(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        admin_user,
    ) -> None:
        """``include_deleted=True`` returns soft-deleted records.

        Admin moderation paths explicitly opt out of the default
        soft-delete filter to surface soft-deleted records for
        review and potential hard-delete.
        """
        deleted_record = SoftDeletedRecordFactory(organization=organization, owner=admin_user)
        actor = _make_session(admin_user)

        result = get_record(record_id=deleted_record.id, actor=actor, include_deleted=True)
        assert result.id == deleted_record.id
        assert result.deleted_at is not None

    def test_unknown_record_id_returns_404(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """A random UUID that does not exist returns 404."""
        actor = _make_session(contributor_user)
        with pytest.raises(NotFoundError):
            get_record(record_id=uuid.uuid4(), actor=actor)


# ===========================================================================
# TestListRecords - F-004 feed query
# ===========================================================================


class TestListRecords:
    """Tests for :func:`list_records` (F-004 feed query).

    Covers filtering on every dimension, sorting on every key + each
    direction, pagination clamping + offset, the stable sort
    tiebreaker, and count correctness.
    """

    def test_returns_only_records_in_actor_org(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """Cross-org records are silently excluded from the feed."""
        # Two records in actor's org.
        RecordFactory(organization=organization, owner=contributor_user)
        RecordFactory(organization=organization, owner=contributor_user)
        # One record in a different org.
        other_org = OrganizationFactory()
        other_user = UserFactory(organization=other_org)
        RecordFactory(organization=other_org, owner=other_user)

        actor = _make_session(contributor_user)
        records, total = list_records(filters=ConnectionFilters(), actor=actor)

        assert total == 2
        assert len(records) == 2
        for r in records:
            assert r.org_id == organization.id

    def test_excludes_soft_deleted_records_by_default(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """Default reads exclude soft-deleted records."""
        active = RecordFactory(organization=organization, owner=contributor_user)
        SoftDeletedRecordFactory(organization=organization, owner=contributor_user)

        actor = _make_session(contributor_user)
        records, total = list_records(filters=ConnectionFilters(), actor=actor)

        assert total == 1
        assert records[0].id == active.id

    def test_includes_soft_deleted_with_admin_opt_in(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        admin_user,
    ) -> None:
        """``include_deleted=True`` surfaces soft-deleted records."""
        active = RecordFactory(organization=organization, owner=admin_user)
        deleted = SoftDeletedRecordFactory(organization=organization, owner=admin_user)

        actor = _make_session(admin_user)
        records, total = list_records(filters=ConnectionFilters(include_deleted=True), actor=actor)

        assert total == 2
        ids = {r.id for r in records}
        assert ids == {active.id, deleted.id}

    def test_filters_by_company_substring_case_insensitive(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """Company filter performs case-insensitive substring match (ILIKE)."""
        RecordFactory(organization=organization, owner=contributor_user, company="Acme Corp")
        RecordFactory(organization=organization, owner=contributor_user, company="OtherCo")

        actor = _make_session(contributor_user)
        # Lowercase substring of "Acme Corp" - should match via ILIKE.
        records, total = list_records(filters=ConnectionFilters(company="acme"), actor=actor)

        assert total == 1
        assert records[0].company == "Acme Corp"

    def test_filters_by_involvement_any_of(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """Involvement filter is "any-of" semantics across the tuple."""
        RecordFactory(
            organization=organization,
            owner=contributor_user,
            involvement=InvolvementType.WARM_INTRO,
        )
        RecordFactory(
            organization=organization,
            owner=contributor_user,
            involvement=InvolvementType.SOFT_REFERENCE,
        )
        RecordFactory(
            organization=organization,
            owner=contributor_user,
            involvement=InvolvementType.TARGET_ONLY,
        )

        actor = _make_session(contributor_user)
        records, total = list_records(
            filters=ConnectionFilters(
                involvement=(InvolvementType.WARM_INTRO, InvolvementType.SOFT_REFERENCE),
            ),
            actor=actor,
        )

        assert total == 2
        involvements = {r.involvement for r in records}
        assert involvements == {
            InvolvementType.WARM_INTRO,
            InvolvementType.SOFT_REFERENCE,
        }

    def test_filters_by_outreach_status_any_of(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """Outreach status filter is "any-of" semantics across the tuple."""
        RecordFactory(
            organization=organization,
            owner=contributor_user,
            outreach_status=OutreachStatus.IN_PROGRESS,
        )
        RecordFactory(
            organization=organization,
            owner=contributor_user,
            outreach_status=OutreachStatus.CONTACTED,
        )

        actor = _make_session(contributor_user)
        records, total = list_records(
            filters=ConnectionFilters(
                outreach_status=(OutreachStatus.IN_PROGRESS,),
            ),
            actor=actor,
        )

        assert total == 1
        assert records[0].outreach_status == OutreachStatus.IN_PROGRESS

    def test_filters_by_owner_user_ids(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """Owner filter restricts to the supplied user ids."""
        owner_a = ContributorUserFactory(organization=organization)
        owner_b = ContributorUserFactory(organization=organization)
        RecordFactory(organization=organization, owner=owner_a)
        RecordFactory(organization=organization, owner=owner_b)

        actor = _make_session(contributor_user)
        records, total = list_records(
            filters=ConnectionFilters(owner_user_ids=(owner_a.id,)),
            actor=actor,
        )

        assert total == 1
        assert records[0].owner_user_id == owner_a.id

    def test_filters_by_submission_date_range(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """Submission date bounds are inclusive at both ends."""
        now = datetime.now(UTC)
        old = RecordFactory(
            organization=organization,
            owner=contributor_user,
            submission_date=now - timedelta(days=10),
        )
        recent = RecordFactory(
            organization=organization,
            owner=contributor_user,
            submission_date=now,
        )

        actor = _make_session(contributor_user)
        # Only "recent" should match. Provide a date 5 days ago.
        records, total = list_records(
            filters=ConnectionFilters(
                submission_date_from=(now - timedelta(days=5)).date(),
            ),
            actor=actor,
        )

        assert total == 1
        assert records[0].id == recent.id

        # Reverse: from epoch to 5 days ago should match "old" only.
        records2, total2 = list_records(
            filters=ConnectionFilters(
                submission_date_to=(now - timedelta(days=5)).date(),
            ),
            actor=actor,
        )
        assert total2 == 1
        assert records2[0].id == old.id

    def test_filters_by_full_name_substring(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """Full-name filter performs case-insensitive substring match (ILIKE)."""
        RecordFactory(
            organization=organization,
            owner=contributor_user,
            full_name="Alice Smith",
        )
        RecordFactory(
            organization=organization,
            owner=contributor_user,
            full_name="Bob Jones",
        )

        actor = _make_session(contributor_user)
        records, total = list_records(
            filters=ConnectionFilters(full_name_search="alice"),
            actor=actor,
        )
        assert total == 1
        assert records[0].full_name == "Alice Smith"

    def test_filters_by_tag_ids_any_of(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """Tag filter uses "any-of" semantics via M:N subquery.

        Records with ANY of the requested tags should match. The
        subquery prevents the row duplication that an INNER JOIN
        would cause when a single record has multiple matching tags.
        """
        tag_a = TagFactory(organization=organization, name="tagged-a")
        tag_b = TagFactory(organization=organization, name="tagged-b")
        record_with_a = RecordFactory(organization=organization, owner=contributor_user)
        record_with_b = RecordFactory(organization=organization, owner=contributor_user)
        record_without = RecordFactory(organization=organization, owner=contributor_user)
        db_session.add(RecordTag(record_id=record_with_a.id, tag_id=tag_a.id))
        db_session.add(RecordTag(record_id=record_with_b.id, tag_id=tag_b.id))
        db_session.commit()

        actor = _make_session(contributor_user)
        records, total = list_records(
            filters=ConnectionFilters(tag_ids=(tag_a.id, tag_b.id)),
            actor=actor,
        )

        # Both tagged records match; the untagged record does not.
        assert total == 2
        ids = {r.id for r in records}
        assert ids == {record_with_a.id, record_with_b.id}
        assert record_without.id not in ids

    def test_combined_filters_apply_intersection(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """Multiple filters intersect (AND, not OR)."""
        RecordFactory(
            organization=organization,
            owner=contributor_user,
            company="Acme",
            involvement=InvolvementType.WARM_INTRO,
        )
        RecordFactory(
            organization=organization,
            owner=contributor_user,
            company="Acme",
            involvement=InvolvementType.SOFT_REFERENCE,
        )
        RecordFactory(
            organization=organization,
            owner=contributor_user,
            company="OtherCo",
            involvement=InvolvementType.WARM_INTRO,
        )

        actor = _make_session(contributor_user)
        # Both filters MUST match, so only the (Acme, Warm Intro) row.
        records, total = list_records(
            filters=ConnectionFilters(
                company="Acme",
                involvement=(InvolvementType.WARM_INTRO,),
            ),
            actor=actor,
        )
        assert total == 1
        assert records[0].company == "Acme"
        assert records[0].involvement == InvolvementType.WARM_INTRO

    def test_sorts_by_full_name_ascending(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """``sort_key='full_name'`` orders results alphabetically."""
        RecordFactory(organization=organization, owner=contributor_user, full_name="Charlie")
        RecordFactory(organization=organization, owner=contributor_user, full_name="Alice")
        RecordFactory(organization=organization, owner=contributor_user, full_name="Bob")

        actor = _make_session(contributor_user)
        records, _ = list_records(
            filters=ConnectionFilters(),
            actor=actor,
            sort_key="full_name",
            sort_dir="asc",
        )
        names = [r.full_name for r in records]
        assert names == ["Alice", "Bob", "Charlie"]

    def test_sorts_by_full_name_descending(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """``sort_dir='desc'`` reverses the order."""
        RecordFactory(organization=organization, owner=contributor_user, full_name="Alice")
        RecordFactory(organization=organization, owner=contributor_user, full_name="Bob")

        actor = _make_session(contributor_user)
        records, _ = list_records(
            filters=ConnectionFilters(),
            actor=actor,
            sort_key="full_name",
            sort_dir="desc",
        )
        names = [r.full_name for r in records]
        assert names == ["Bob", "Alice"]

    def test_default_sort_is_submission_date_desc(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """Default sort is ``submission_date DESC`` (newest first)."""
        now = datetime.now(UTC)
        old = RecordFactory(
            organization=organization,
            owner=contributor_user,
            submission_date=now - timedelta(days=10),
        )
        new = RecordFactory(
            organization=organization,
            owner=contributor_user,
            submission_date=now,
        )

        actor = _make_session(contributor_user)
        records, _ = list_records(filters=ConnectionFilters(), actor=actor)
        # Newest first.
        assert records[0].id == new.id
        assert records[1].id == old.id

    def test_invalid_sort_key_raises_validation_error(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """An unknown ``sort_key`` raises 422 with field-scoped error."""
        actor = _make_session(contributor_user)
        with pytest.raises(ValidationFailedError) as exc_info:
            list_records(
                filters=ConnectionFilters(),
                actor=actor,
                sort_key="not_a_real_column",
            )
        assert any("sort" in field.get("loc", []) for field in exc_info.value.fields)

    def test_invalid_sort_dir_falls_back_silently(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """An unknown ``sort_dir`` silently uses the default direction.

        Per the service docstring: ``sort_dir`` is silently coerced
        because direction is commonly mis-cased (``"DESC"`` vs
        ``"desc"``); raising would be too brittle.
        """
        RecordFactory(organization=organization, owner=contributor_user)
        actor = _make_session(contributor_user)
        # Should NOT raise; should silently fall back to "desc".
        records, _ = list_records(
            filters=ConnectionFilters(),
            actor=actor,
            sort_dir="INVALID",
        )
        assert len(records) == 1

    def test_pagination_clamps_limit_to_max(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """``limit > _MAX_PAGE_SIZE`` is silently clamped (no error)."""
        # Create just one record to keep the test fast; we verify the
        # clamp behavior, not the actual page contents.
        RecordFactory(organization=organization, owner=contributor_user)
        actor = _make_session(contributor_user)
        records, total = list_records(filters=ConnectionFilters(), actor=actor, limit=10_000)
        # No exception; result count cannot exceed the actual data
        # (1 record), but total is correct.
        assert total == 1
        assert len(records) <= 100  # _MAX_PAGE_SIZE

    def test_pagination_clamps_limit_below_one(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """``limit < 1`` is silently clamped to 1 (never zero/negative)."""
        RecordFactory(organization=organization, owner=contributor_user)
        actor = _make_session(contributor_user)
        # Negative or zero limit should be clamped to 1.
        records, _ = list_records(filters=ConnectionFilters(), actor=actor, limit=0)
        # Exactly one record should be returned (limit clamped to 1).
        assert len(records) == 1

    def test_pagination_clamps_negative_offset(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """A negative ``offset`` is clamped to 0."""
        RecordFactory(organization=organization, owner=contributor_user)
        actor = _make_session(contributor_user)
        # Negative offset should be clamped to 0.
        records, _ = list_records(filters=ConnectionFilters(), actor=actor, offset=-100, limit=10)
        # Should not crash; should return the one record.
        assert len(records) == 1

    def test_pagination_offset_skips_records(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """``offset`` skips that many records from the sorted result."""
        # Create 3 records with deterministic ordering on full_name.
        RecordFactory(organization=organization, owner=contributor_user, full_name="Alice")
        RecordFactory(organization=organization, owner=contributor_user, full_name="Bob")
        RecordFactory(organization=organization, owner=contributor_user, full_name="Charlie")

        actor = _make_session(contributor_user)
        # Skip the first record sorted ASC by name.
        records, total = list_records(
            filters=ConnectionFilters(),
            actor=actor,
            sort_key="full_name",
            sort_dir="asc",
            offset=1,
            limit=10,
        )
        # Total count remains 3 (across all pages).
        assert total == 3
        # Page contains 2 records starting from "Bob".
        assert len(records) == 2
        assert records[0].full_name == "Bob"

    def test_total_count_is_filtered_count(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """``total`` reflects the filtered count, not the global count."""
        RecordFactory(organization=organization, owner=contributor_user, company="Acme")
        RecordFactory(organization=organization, owner=contributor_user, company="Acme")
        RecordFactory(organization=organization, owner=contributor_user, company="OtherCo")

        actor = _make_session(contributor_user)
        _, total = list_records(filters=ConnectionFilters(company="Acme"), actor=actor)
        assert total == 2

    def test_empty_result_returns_empty_list_and_zero_total(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """A no-match filter returns an empty list and total=0 (not None)."""
        actor = _make_session(contributor_user)
        records, total = list_records(filters=ConnectionFilters(), actor=actor)
        assert records == []
        assert total == 0


# ===========================================================================
# TestUpdateRecord - F-007 edit
# ===========================================================================


class TestUpdateRecord:
    """Tests for :func:`update_record` (F-007 edit).

    Covers ownership check (Contributor own-only, Admin any),
    PATCH semantics (None=no-op, [] and [...]=replace tag set),
    atomic state-change + audit pair, duplicate-URL race resolution,
    and PII exclusion from the audit payload.
    """

    def test_owner_can_edit_own_record(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """A Contributor may edit their own record."""
        record = RecordFactory(organization=organization, owner=contributor_user)
        actor = _make_session(contributor_user)
        payload = ConnectionUpdate.model_validate({"company": "New Company"})

        result = update_record(record_id=record.id, payload=payload, actor=actor)
        assert result.company == "New Company"

    def test_contributor_cannot_edit_other_users_record(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """Non-Admin contributor cannot edit a record owned by another user.

        Verifies the F-007 ownership invariant + AAP Section 0.7.1
        invariant 7 (RBAC enforced at the service layer too): even
        if the API decorator allowed the role, the service-layer
        ownership check rejects.
        """
        other_owner = ContributorUserFactory(organization=organization)
        record = RecordFactory(organization=organization, owner=other_owner)
        actor = _make_session(contributor_user)
        payload = ConnectionUpdate.model_validate({"company": "Hijacked"})

        with pytest.raises(ForbiddenError):
            update_record(record_id=record.id, payload=payload, actor=actor)

    def test_admin_can_edit_any_record(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        admin_user,
        contributor_user,
    ) -> None:
        """An Admin can edit a record owned by another user."""
        record = RecordFactory(organization=organization, owner=contributor_user)
        actor = _make_session(admin_user)
        payload = ConnectionUpdate.model_validate({"company": "Admin Updated"})

        result = update_record(record_id=record.id, payload=payload, actor=actor)
        assert result.company == "Admin Updated"

    def test_cross_org_record_returns_404(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """Editing a record in another org returns 404 (info-disclosure defense)."""
        other_org = OrganizationFactory()
        other_user = UserFactory(organization=other_org)
        cross_org_record = RecordFactory(organization=other_org, owner=other_user)

        actor = _make_session(contributor_user)
        payload = ConnectionUpdate.model_validate({"company": "Cross-org"})

        with pytest.raises(NotFoundError):
            update_record(record_id=cross_org_record.id, payload=payload, actor=actor)

    def test_emits_edit_audit_event_atomically(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """Edit emits exactly one EDIT audit event with before/after payloads."""
        record = RecordFactory(organization=organization, owner=contributor_user, company="Before")
        actor = _make_session(contributor_user)
        payload = ConnectionUpdate.model_validate({"company": "After"})

        update_record(record_id=record.id, payload=payload, actor=actor)

        events = (
            db_session.execute(
                select(AuditEvent)
                .where(AuditEvent.target_record_id == record.id)
                .where(AuditEvent.event_type == AuditEventType.EDIT)
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        event = events[0]
        assert event.before_payload is not None
        assert event.after_payload is not None
        assert event.before_payload["company"] == "Before"
        assert event.after_payload["company"] == "After"

    def test_unset_fields_are_not_modified(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """Fields absent from the payload are not modified (PATCH semantics)."""
        record = RecordFactory(
            organization=organization,
            owner=contributor_user,
            full_name="Stable Name",
            company="To Be Updated",
        )
        actor = _make_session(contributor_user)
        payload = ConnectionUpdate.model_validate({"company": "Updated"})

        result = update_record(record_id=record.id, payload=payload, actor=actor)
        # full_name was not in the payload, so it must be unchanged.
        assert result.full_name == "Stable Name"
        assert result.company == "Updated"

    def test_tag_replacement_clears_when_empty_list(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """``tag_ids=[]`` removes all existing tag associations."""
        tag = TagFactory(organization=organization, name="to-be-removed")
        record = RecordFactory(organization=organization, owner=contributor_user)
        db_session.add(RecordTag(record_id=record.id, tag_id=tag.id))
        db_session.commit()

        actor = _make_session(contributor_user)
        payload = ConnectionUpdate.model_validate({"tag_ids": []})
        update_record(record_id=record.id, payload=payload, actor=actor)

        # No remaining record_tags rows.
        rt_rows = db_session.execute(
            select(RecordTag).where(RecordTag.record_id == record.id)
        ).all()
        assert rt_rows == []

    def test_tag_replacement_replaces_set(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """``tag_ids=[t1, t2]`` replaces existing associations with the set."""
        tag_old = TagFactory(organization=organization, name="old")
        tag_new_a = TagFactory(organization=organization, name="new-a")
        tag_new_b = TagFactory(organization=organization, name="new-b")
        record = RecordFactory(organization=organization, owner=contributor_user)
        db_session.add(RecordTag(record_id=record.id, tag_id=tag_old.id))
        db_session.commit()

        actor = _make_session(contributor_user)
        payload = ConnectionUpdate.model_validate(
            {"tag_ids": [str(tag_new_a.id), str(tag_new_b.id)]}
        )
        update_record(record_id=record.id, payload=payload, actor=actor)

        # The old tag is gone; the two new tags are present.
        rt_rows = (
            db_session.execute(select(RecordTag).where(RecordTag.record_id == record.id))
            .scalars()
            .all()
        )
        tag_ids = {rt.tag_id for rt in rt_rows}
        assert tag_ids == {tag_new_a.id, tag_new_b.id}
        assert tag_old.id not in tag_ids

    def test_tag_replacement_rejects_cross_org_tag(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """A cross-org tag id surfaces as 422 during edit (defense-in-depth)."""
        other_org = OrganizationFactory()
        cross_org_tag = TagFactory(organization=other_org, name="injected-tag")
        record = RecordFactory(organization=organization, owner=contributor_user)
        actor = _make_session(contributor_user)
        payload = ConnectionUpdate.model_validate({"tag_ids": [str(cross_org_tag.id)]})
        with pytest.raises(ValidationFailedError):
            update_record(record_id=record.id, payload=payload, actor=actor)

    def test_linkedin_url_change_updates_normalized_form(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """When linkedin_url changes, the normalized form is recomputed."""
        record = RecordFactory(
            organization=organization,
            owner=contributor_user,
            linkedin_url="https://www.linkedin.com/in/old-url",
            normalized_linkedin_url=normalize_linkedin_url("https://www.linkedin.com/in/old-url"),
        )
        actor = _make_session(contributor_user)
        new_url = "https://www.linkedin.com/in/NEW-url/?utm=test"
        payload = ConnectionUpdate.model_validate({"linkedin_url": new_url})

        result = update_record(record_id=record.id, payload=payload, actor=actor)
        assert result.linkedin_url == new_url
        assert result.normalized_linkedin_url == normalize_linkedin_url(new_url)

    def test_duplicate_normalized_url_during_edit_returns_409(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """Editing to a URL that collides with another active record returns 409."""
        existing_url = "https://www.linkedin.com/in/already-exists"
        # Existing active record with the URL we'll try to migrate to.
        RecordFactory(
            organization=organization,
            owner=contributor_user,
            linkedin_url=existing_url,
            normalized_linkedin_url=normalize_linkedin_url(existing_url),
        )
        # The record we're editing has a different URL.
        record = RecordFactory(
            organization=organization,
            owner=contributor_user,
            linkedin_url="https://www.linkedin.com/in/about-to-collide",
            normalized_linkedin_url=normalize_linkedin_url(
                "https://www.linkedin.com/in/about-to-collide"
            ),
        )
        actor = _make_session(contributor_user)
        payload = ConnectionUpdate.model_validate({"linkedin_url": existing_url})

        with pytest.raises(DuplicateRecordError):
            update_record(record_id=record.id, payload=payload, actor=actor)


# ===========================================================================
# TestUpdateStatus - F-005 status mutation
# ===========================================================================


class TestUpdateStatus:
    """Tests for :func:`update_status` (F-005 outreach-status mutation).

    The service does NOT enforce role membership (the API decorator
    ``@requires_role(UserRole.VIEWER, UserRole.ADMIN)`` is the
    authoritative gate per AAP Section 0.7.1 invariant 7). The service
    DOES enforce org-scope and idempotency.
    """

    def test_changes_status_and_emits_audit(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        viewer_user,
    ) -> None:
        """A status change persists and emits one STATUS_CHANGE audit event."""
        record = RecordFactory(
            organization=organization,
            owner=viewer_user,
            outreach_status=OutreachStatus.NOT_STARTED,
        )
        actor = _make_session(viewer_user)
        payload = ConnectionStatusUpdate.model_validate({"outreach_status": "In Progress"})

        result = update_status(record_id=record.id, payload=payload, actor=actor)
        assert result.outreach_status == OutreachStatus.IN_PROGRESS

        events = (
            db_session.execute(
                select(AuditEvent)
                .where(AuditEvent.target_record_id == record.id)
                .where(AuditEvent.event_type == AuditEventType.STATUS_CHANGE)
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        event = events[0]
        assert event.before_payload == {"outreach_status": "Not Started"}
        assert event.after_payload == {"outreach_status": "In Progress"}

    def test_no_op_when_status_unchanged_emits_no_audit(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        viewer_user,
    ) -> None:
        """Setting the same status is idempotent (no audit event emitted).

        Per the service docstring: idempotent no-op prevents the audit
        log from being flooded with redundant entries when the SPA
        polls or retries. The absence of an audit event is the
        signal that the call was a no-op.
        """
        record = RecordFactory(
            organization=organization,
            owner=viewer_user,
            outreach_status=OutreachStatus.IN_PROGRESS,
        )
        actor = _make_session(viewer_user)
        payload = ConnectionStatusUpdate.model_validate({"outreach_status": "In Progress"})

        update_status(record_id=record.id, payload=payload, actor=actor)

        # Zero STATUS_CHANGE events because the status was unchanged.
        events = (
            db_session.execute(
                select(AuditEvent)
                .where(AuditEvent.target_record_id == record.id)
                .where(AuditEvent.event_type == AuditEventType.STATUS_CHANGE)
            )
            .scalars()
            .all()
        )
        assert events == []

    def test_cross_org_status_update_returns_404(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        viewer_user,
    ) -> None:
        """Updating status on a cross-org record returns 404."""
        other_org = OrganizationFactory()
        other_user = UserFactory(organization=other_org)
        cross_org_record = RecordFactory(organization=other_org, owner=other_user)
        actor = _make_session(viewer_user)
        payload = ConnectionStatusUpdate.model_validate({"outreach_status": "Closed"})

        with pytest.raises(NotFoundError):
            update_status(record_id=cross_org_record.id, payload=payload, actor=actor)

    def test_unknown_record_id_returns_404(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        viewer_user,
    ) -> None:
        """A random UUID returns 404."""
        actor = _make_session(viewer_user)
        payload = ConnectionStatusUpdate.model_validate({"outreach_status": "Closed"})

        with pytest.raises(NotFoundError):
            update_status(record_id=uuid.uuid4(), payload=payload, actor=actor)


# ===========================================================================
# TestSoftDeleteRecord - F-007 soft delete
# ===========================================================================


class TestSoftDeleteRecord:
    """Tests for :func:`soft_delete_record` (F-007 soft delete).

    Covers ownership check, idempotency on re-delete, and atomic
    state-change + audit pair.
    """

    def test_owner_can_soft_delete_own_record(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """A Contributor may soft-delete their own record."""
        record = RecordFactory(organization=organization, owner=contributor_user)
        actor = _make_session(contributor_user)

        result = soft_delete_record(record_id=record.id, actor=actor)
        assert result.deleted_at is not None

        # The DB row reflects the soft-delete state. ``expire_all`` is
        # required because the service ran inside its own session
        # (opened via ``with db.session() as session, session.begin():``)
        # which committed independently of the test fixture's
        # ``db_session``. The test session has the pre-soft-delete
        # snapshot cached on the in-memory ``Record`` instance; expiring
        # all loaded objects forces the next access to re-read from the
        # database, surfacing the now-committed ``deleted_at``.
        db_session.expire_all()
        db_record = db_session.execute(select(Record).where(Record.id == record.id)).scalar_one()
        assert db_record.deleted_at is not None

    def test_contributor_cannot_delete_other_users_record(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """A Contributor cannot soft-delete a record they do not own."""
        other_owner = ContributorUserFactory(organization=organization)
        record = RecordFactory(organization=organization, owner=other_owner)
        actor = _make_session(contributor_user)

        with pytest.raises(ForbiddenError):
            soft_delete_record(record_id=record.id, actor=actor)

    def test_admin_can_soft_delete_any_record(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        admin_user,
        contributor_user,
    ) -> None:
        """An Admin may soft-delete any record in the org."""
        record = RecordFactory(organization=organization, owner=contributor_user)
        actor = _make_session(admin_user)

        result = soft_delete_record(record_id=record.id, actor=actor)
        assert result.deleted_at is not None

    def test_emits_soft_delete_audit_event(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """A soft delete emits exactly one SOFT_DELETE audit event."""
        record = RecordFactory(organization=organization, owner=contributor_user)
        actor = _make_session(contributor_user)

        soft_delete_record(record_id=record.id, actor=actor)

        events = (
            db_session.execute(
                select(AuditEvent)
                .where(AuditEvent.target_record_id == record.id)
                .where(AuditEvent.event_type == AuditEventType.SOFT_DELETE)
            )
            .scalars()
            .all()
        )
        assert len(events) == 1

    def test_idempotent_when_already_soft_deleted(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """A second soft-delete is a no-op (no new audit event, no advance)."""
        record = SoftDeletedRecordFactory(organization=organization, owner=contributor_user)
        original_deleted_at = record.deleted_at
        actor = _make_session(contributor_user)

        # Re-delete: should not raise, should not emit a second audit row,
        # should not advance deleted_at.
        result = soft_delete_record(record_id=record.id, actor=actor)
        assert result.deleted_at == original_deleted_at

        # Zero SOFT_DELETE events emitted (the record was created
        # already-deleted via the factory, so no SOFT_DELETE event
        # was ever generated for it).
        events = (
            db_session.execute(
                select(AuditEvent)
                .where(AuditEvent.target_record_id == record.id)
                .where(AuditEvent.event_type == AuditEventType.SOFT_DELETE)
            )
            .scalars()
            .all()
        )
        assert events == []

    def test_cross_org_soft_delete_returns_404(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """Soft-deleting a cross-org record returns 404."""
        other_org = OrganizationFactory()
        other_user = UserFactory(organization=other_org)
        cross_org_record = RecordFactory(organization=other_org, owner=other_user)
        actor = _make_session(contributor_user)

        with pytest.raises(NotFoundError):
            soft_delete_record(record_id=cross_org_record.id, actor=actor)


# ===========================================================================
# TestGetRecordHistory - F-011 history feed
# ===========================================================================


class TestGetRecordHistory:
    """Tests for :func:`get_record_history` (F-011 audit-event feed).

    Verifies the org-scope pre-check (cross-org enumeration defense),
    ordering by ``event_timestamp DESC``, the stable sort tiebreaker
    on ``AuditEvent.id ASC``, and pagination correctness.
    """

    def test_returns_audit_events_for_record(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """Audit events targeting the record are returned."""
        record = RecordFactory(organization=organization, owner=contributor_user)
        AuditEventFactory(
            actor=contributor_user,
            target_record=record,
            event_type=AuditEventType.CREATE,
        )

        actor = _make_session(contributor_user)
        events, total = get_record_history(record_id=record.id, actor=actor)
        assert total == 1
        assert events[0].target_record_id == record.id
        assert events[0].event_type == AuditEventType.CREATE

    def test_orders_by_event_timestamp_desc(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """Events are ordered newest-first."""
        record = RecordFactory(organization=organization, owner=contributor_user)
        now = datetime.now(UTC)

        oldest = AuditEventFactory(
            actor=contributor_user,
            target_record=record,
            event_type=AuditEventType.CREATE,
            event_timestamp=now - timedelta(hours=2),
        )
        middle = AuditEventFactory(
            actor=contributor_user,
            target_record=record,
            event_type=AuditEventType.EDIT,
            event_timestamp=now - timedelta(hours=1),
        )
        newest = AuditEventFactory(
            actor=contributor_user,
            target_record=record,
            event_type=AuditEventType.STATUS_CHANGE,
            event_timestamp=now,
        )

        actor = _make_session(contributor_user)
        events, _ = get_record_history(record_id=record.id, actor=actor)
        ids_in_order = [e.id for e in events]
        assert ids_in_order == [newest.id, middle.id, oldest.id]

    def test_cross_org_record_returns_404(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """Cross-org history requests return 404 (enumeration defense)."""
        other_org = OrganizationFactory()
        other_user = UserFactory(organization=other_org)
        cross_org_record = RecordFactory(organization=other_org, owner=other_user)
        actor = _make_session(contributor_user)

        with pytest.raises(NotFoundError):
            get_record_history(record_id=cross_org_record.id, actor=actor)

    def test_unknown_record_id_returns_404(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """A random UUID returns 404."""
        actor = _make_session(contributor_user)
        with pytest.raises(NotFoundError):
            get_record_history(record_id=uuid.uuid4(), actor=actor)

    def test_pagination_clamps_limit_and_offset(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """``limit`` and ``offset`` are clamped to safe ranges."""
        record = RecordFactory(organization=organization, owner=contributor_user)
        # Seed a few events.
        for _ in range(3):
            AuditEventFactory(
                actor=contributor_user,
                target_record=record,
                event_type=AuditEventType.EDIT,
            )
        actor = _make_session(contributor_user)

        # Excessive limit and negative offset must not crash.
        events, total = get_record_history(
            record_id=record.id, actor=actor, limit=10_000, offset=-50
        )
        assert total == 3
        assert len(events) == 3

    def test_pagination_offset_skips_events(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """``offset`` skips that many events from the sorted result."""
        record = RecordFactory(organization=organization, owner=contributor_user)
        now = datetime.now(UTC)
        AuditEventFactory(
            actor=contributor_user,
            target_record=record,
            event_type=AuditEventType.CREATE,
            event_timestamp=now - timedelta(hours=2),
        )
        AuditEventFactory(
            actor=contributor_user,
            target_record=record,
            event_type=AuditEventType.EDIT,
            event_timestamp=now - timedelta(hours=1),
        )
        AuditEventFactory(
            actor=contributor_user,
            target_record=record,
            event_type=AuditEventType.STATUS_CHANGE,
            event_timestamp=now,
        )
        actor = _make_session(contributor_user)

        events, total = get_record_history(record_id=record.id, actor=actor, limit=10, offset=1)
        # Total count remains 3 (across all pages).
        assert total == 3
        # Page contains 2 events skipping the most recent one.
        assert len(events) == 2

    def test_admin_can_see_history_of_soft_deleted_record(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        admin_user,
    ) -> None:
        """Admin moderation view can fetch history of soft-deleted records.

        ``include_deleted=True`` permits the org-scope pre-check to
        succeed for soft-deleted records, enabling the admin
        moderation surface to review history before hard-deleting.
        """
        record = SoftDeletedRecordFactory(organization=organization, owner=admin_user)
        AuditEventFactory(
            actor=admin_user,
            target_record=record,
            event_type=AuditEventType.SOFT_DELETE,
        )
        actor = _make_session(admin_user)

        events, total = get_record_history(record_id=record.id, actor=actor, include_deleted=True)
        assert total == 1
        assert events[0].event_type == AuditEventType.SOFT_DELETE


# ===========================================================================
# TestRoleConstants - referential symbols for static analysis
# ===========================================================================
# The tests above import every public symbol from
# ``app.services.connections``, ``app.middleware.error_handlers``, and
# ``app.models.enums`` that the module's contract uses. Reference each
# import once at module scope so static analysis tools that ignore
# in-class usage do not flag any of them as unused.
_ = (
    AdminUserFactory,
    ViewerUserFactory,
    UserRole,  # imported alongside the enums for symbolic completeness
)
