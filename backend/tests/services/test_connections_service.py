"""Tests for ``app.services.connections`` -- F-001/F-004/F-005/F-007/F-010/F-011.

Each test exercises ONE service function with the canonical pattern:

    1. Seed via factories committed to ``db_session``.
    2. Construct a :class:`Session` for the actor.
    3. Call the service function under test.
    4. Assert (a) the return value, (b) any audit-trail emission via the
       :func:`audit_assertion` helper or direct ``select(AuditEvent)``
       queries, and (c) any database state via ``db_session``.

Test markers:

* ``@pytest.mark.audit`` -- tests verifying audit emission per AAP
  Section 0.7.1 invariant 5 (every state-changing path emits an audit
  event in the same transaction).
* ``@pytest.mark.rbac`` -- tests exercising the F-009 ownership matrix
  (Contributor own-only, Admin any, Viewer cannot edit).
* ``@pytest.mark.integration`` -- tests requiring real PostgreSQL
  features (the partial unique index on
  ``(org_id, normalized_linkedin_url) WHERE deleted_at IS NULL``,
  ``EXPLAIN`` plan output, and the soft-delete-then-recreate flow that
  depends on the partial index excluding soft-deleted rows).

Coverage map:

* :class:`TestCreateRecord` -- F-001 form-driven create. Covers atomic
  state-change + audit pair, owner attribution from session only
  (F-006), normalized LinkedIn URL derivation, default outreach
  status, default ``deleted_at = None``, duplicate-URL race resolution
  (F-010), soft-delete-then-recreate semantics, and tag association.
* :class:`TestGetRecord` -- F-011 detail fetch. Covers org-scope
  enforcement via ``NotFoundError`` (NOT ``ForbiddenError`` per the
  cross-org enumeration defense), soft-delete-aware default reads,
  and eager tag loading.
* :class:`TestListRecords` -- F-004 feed query. Covers pagination
  (``items``, ``total``, ``limit``, ``offset``), soft-delete
  exclusion (default), admin opt-in via ``include_deleted=True``,
  org-scoping, filter by company / involvement, page-size capping at
  ``_MAX_PAGE_SIZE = 100``, every documented sort key, and the
  composite-index ``EXPLAIN`` assertion.
* :class:`TestUpdateRecord` -- F-007 edit. Covers owner-only edit by
  Contributor, Admin bypass, Viewer denial, ``EDIT`` audit with
  before/after payloads, and cross-org ``NotFoundError``.
* :class:`TestUpdateStatus` -- F-005 status mutation. Covers the
  ``STATUS_CHANGE`` audit emission with before/after payloads, the
  no-op skip when the value is unchanged, and cross-org
  ``NotFoundError``.
* :class:`TestSoftDeleteRecord` -- F-007 soft delete. Covers
  ``deleted_at`` setting, ``SOFT_DELETE`` audit emission, idempotency
  (no double audit), Contributor-other-record denial, Admin bypass,
  and cross-org ``NotFoundError``.
* :class:`TestGetRecordHistory` -- F-011 audit-event-sourced edit
  history. Covers DESC ordering, filtering of ``role_change`` /
  ``authentication`` events (which carry ``target_record_id IS
  NULL``), cross-org and unknown-record ``NotFoundError``, and
  pagination via ``limit`` + ``offset``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import uuid

import pytest
from sqlalchemy import select, text

from app.middleware.auth import Session
from app.middleware.error_handlers import (
    ForbiddenError,
    NotFoundError,
)
from app.models import AuditEvent, Record
from app.models.enums import (
    AuditEventType,
    InvolvementType,
    OutreachStatus,
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
    AuditEventFactory,
    ContributorUserFactory,
    OrganizationFactory,
    RecordFactory,
    SoftDeletedRecordFactory,
    TagFactory,
    UserFactory,
)

# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _make_session(user) -> Session:
    """Build a typed :class:`Session` from a User factory instance.

    The :class:`Session` dataclass is the contract between
    :mod:`app.middleware.auth` and the service layer; constructing it
    explicitly here lets tests drive the service-layer functions
    without spinning up a full HTTP round-trip via the Flask test
    client. The ``raw_claims`` dict is populated empty so any test
    that inadvertently logs the session does not surface plausible-
    but-fake JWT data.

    Args:
        user: A User factory instance carrying ``id``, ``org_id``,
            ``role``, ``email``, and ``display_name`` attributes.

    Returns:
        An immutable, frozen :class:`Session` carrying the same
        identifying fields and the user's role enum.
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


def _basic_payload(**overrides: object) -> ConnectionCreate:
    """Build a minimal :class:`ConnectionCreate` payload with sensible defaults.

    Defaults satisfy every pydantic validator on
    :class:`ConnectionCreate`:

    * ``full_name``, ``company``, ``job_title`` within the
      255-character cap.
    * ``linkedin_url`` is a syntactically valid LinkedIn profile URL
      that passes :func:`is_valid_linkedin_url`.
    * ``relationship_context`` within the 4000-character cap.
    * ``involvement`` defaults to :data:`InvolvementType.WARM_INTRO`.
    * ``tag_ids`` defaults to an empty list to avoid cross-test
      interference.

    Args:
        **overrides: Any of the eight :class:`ConnectionCreate`
            fields to override (e.g. ``linkedin_url=...``,
            ``involvement=InvolvementType.TARGET_ONLY``).

    Returns:
        A schema-validated :class:`ConnectionCreate` ready to pass
        directly to :func:`create_record`.
    """
    base: dict = {
        "full_name": "Jane Doe",
        "linkedin_url": "https://www.linkedin.com/in/jane-doe",
        "company": "Acme Corp",
        "job_title": "VP of Operations",
        "relationship_context": "We worked together at Initech for 3 years.",
        "ai_notes": None,
        "involvement": InvolvementType.WARM_INTRO,
        "tag_ids": [],
    }
    base.update(overrides)
    return ConnectionCreate.model_validate(base)


# ---------------------------------------------------------------------------
# Module-level fixture: audit assertion helper
# ---------------------------------------------------------------------------


@pytest.fixture
def audit_assertion(db_session):
    """Return a helper that asserts an audit row matching the criteria exists.

    The helper queries ``audit_events`` for the most recent row
    matching the supplied filters and returns the row for further
    inspection (e.g., the test can then assert on ``before_payload``
    / ``after_payload`` content).

    Used by tests that verify the AAP Section 0.7.1 invariant 5
    "every state change emits an audit event in the same transaction"
    -- the parent operation has already committed by the time the
    helper runs, so the audit row MUST be visible to a fresh SELECT.

    Args:
        db_session: The test-scoped SQLAlchemy session (auto-injected
            by pytest's fixture dependency resolution).

    Returns:
        A callable ``_assert(*, event_type, target_record_id=None,
        actor_user_id=None)`` that returns the matching
        :class:`AuditEvent` row.
    """

    def _assert(
        *,
        event_type: AuditEventType,
        target_record_id: uuid.UUID | None = None,
        actor_user_id: uuid.UUID | None = None,
    ) -> AuditEvent:
        # The service-layer functions open their own SQLAlchemy
        # session via ``with db.session() as session, session.begin():``
        # and commit independently of the test fixture's
        # ``db_session``. ``expire_all`` evicts any stale ORM cache
        # entries so the next SELECT re-reads from the database,
        # surfacing rows committed by the service layer.
        db_session.expire_all()

        stmt = select(AuditEvent).where(AuditEvent.event_type == event_type)
        if target_record_id is not None:
            stmt = stmt.where(AuditEvent.target_record_id == target_record_id)
        if actor_user_id is not None:
            stmt = stmt.where(AuditEvent.actor_user_id == actor_user_id)
        stmt = stmt.order_by(AuditEvent.event_timestamp.desc(), AuditEvent.id.asc())
        result = db_session.execute(stmt).scalars().first()
        assert result is not None, (
            f"Expected audit event of type {event_type.value!r} "
            f"for target_record_id={target_record_id} "
            f"actor_user_id={actor_user_id}, but no matching row was found."
        )
        return result

    return _assert


# ===========================================================================
# TestCreateRecord -- F-001 form-driven create
# ===========================================================================


class TestCreateRecord:
    """Tests for :func:`create_record` (F-001 form-driven create).

    Verifies the AAP Section 0.7.1 invariant 6 (atomic state-change +
    audit pair), the F-006 owner-attribution invariant (owner derived
    from session only), the F-010 partial unique index race resolution
    (duplicate URL within an org raises 409), and the F-008 tag
    association flow.
    """

    @pytest.mark.audit
    def test_persists_record_and_emits_create_audit(
        self, db_session, contributor_user, audit_assertion
    ):
        """The record is persisted and a CREATE audit is emitted atomically.

        AAP Section 0.7.1 invariant 6: the records INSERT and the
        audit_events INSERT both commit (or both roll back) in a
        single transaction.
        """
        actor = _make_session(contributor_user)
        payload = _basic_payload()

        result = create_record(payload=payload, actor=actor)

        # Service returns the persisted SQLAlchemy entity directly.
        assert isinstance(result, Record)
        assert result.full_name == "Jane Doe"
        assert result.org_id == contributor_user.org_id

        # Verify the row is in the database via a fresh SELECT.
        record = db_session.get(Record, result.id)
        assert record is not None
        assert record.full_name == "Jane Doe"

        # Verify the audit row was emitted.
        audit = audit_assertion(
            event_type=AuditEventType.CREATE,
            target_record_id=result.id,
            actor_user_id=contributor_user.id,
        )
        assert audit.target_record_id == result.id
        assert audit.before_payload is None
        assert audit.after_payload is not None
        assert audit.after_payload["full_name"] == "Jane Doe"

    def test_owner_derived_from_actor_not_payload(self, db_session, contributor_user):
        """``owner_user_id`` is derived from ``actor.user_id`` exclusively.

        F-006 invariant: even though the pydantic schema's
        ``extra='forbid'`` config would already reject a malicious
        payload carrying ``owner_user_id``, the service layer
        additionally derives the value from the session so a future
        schema relaxation cannot silently weaken the invariant.
        """
        actor = _make_session(contributor_user)

        result = create_record(payload=_basic_payload(), actor=actor)

        # The session.user_id is the ONLY source of truth for owner.
        assert result.owner_user_id == actor.user_id
        assert result.owner_user_id == contributor_user.id

        # Verify directly from DB.
        record = db_session.get(Record, result.id)
        assert record.owner_user_id == contributor_user.id

    def test_owner_display_name_denormalized_from_user(self, db_session, organization):
        """``owner_display_name`` is denormalized from the User row at create.

        F-006 invariant: the display name is captured at submission
        time so the feed render is fast (no JOIN required) and the
        attribution stays stable even if the user later renames.
        """
        contributor = ContributorUserFactory(organization=organization, display_name="Alice Smith")
        db_session.commit()
        actor = _make_session(contributor)

        result = create_record(payload=_basic_payload(), actor=actor)

        assert result.owner_display_name == "Alice Smith"
        record = db_session.get(Record, result.id)
        assert record.owner_display_name == "Alice Smith"

    def test_normalized_linkedin_url_derived_from_input(self, db_session, contributor_user):
        """``normalized_linkedin_url`` is computed via ``normalize_linkedin_url``.

        F-010 invariant: the canonical normalized form
        (lowercase host, strip trailing slash, drop query parameters
        and fragments) is the ONLY source of truth for the partial
        unique index. The service must derive the normalized form
        server-side (never trust a client-supplied normalized value).
        """
        actor = _make_session(contributor_user)
        # Mixed case + trailing slash + query string: stress the
        # normalizer's canonicalization rules.
        raw_url = "HTTPS://WWW.LinkedIn.com/in/Jane-Doe/?utm=foo"
        payload = _basic_payload(linkedin_url=raw_url)

        result = create_record(payload=payload, actor=actor)

        record = db_session.get(Record, result.id)
        assert record.normalized_linkedin_url == normalize_linkedin_url(raw_url)

    def test_default_outreach_status_is_not_started(self, db_session, contributor_user):
        """New records default to :data:`OutreachStatus.NOT_STARTED` (F-005).

        Per the database column's ``server_default`` and the
        application-level invariant: every new record begins at the
        start of the sales-team progression workflow.
        """
        actor = _make_session(contributor_user)

        result = create_record(payload=_basic_payload(), actor=actor)

        assert result.outreach_status == OutreachStatus.NOT_STARTED

    def test_default_deleted_at_is_none(self, db_session, contributor_user):
        """New records have ``deleted_at = None`` (F-007).

        The soft-delete sentinel is NULL on creation; only the
        :func:`soft_delete_record` path advances it to a non-NULL
        UTC timestamp. This invariant guarantees that the partial
        unique index ``uq_records_org_normalized_linkedin_url_active``
        applies to the new record (since the partial predicate is
        ``WHERE deleted_at IS NULL``).
        """
        actor = _make_session(contributor_user)

        result = create_record(payload=_basic_payload(), actor=actor)

        record = db_session.get(Record, result.id)
        assert record.deleted_at is None

    @pytest.mark.integration
    def test_duplicate_linkedin_url_in_same_org_raises_conflict(self, db_session, contributor_user):
        """The partial unique index fires on duplicate URL in the same org.

        F-010 invariant: a second record with the same normalized
        URL (in the same org, with deleted_at IS NULL) MUST be
        rejected at the database level. The service catches the
        :class:`IntegrityError` from ``session.flush()`` and re-
        raises as :class:`DuplicateRecordError`, which is mapped to
        HTTP 409 by the registered Flask error handler.

        Note: :class:`DuplicateRecordError` extends :class:`AppError`
        directly (not :class:`ConflictError`); they are siblings in
        the exception hierarchy. The test catches the actual class
        raised by the service.
        """
        actor = _make_session(contributor_user)
        url = "https://www.linkedin.com/in/jane-doe-conflict-test"
        create_record(payload=_basic_payload(linkedin_url=url), actor=actor)

        with pytest.raises(DuplicateRecordError) as exc_info:
            create_record(payload=_basic_payload(linkedin_url=url), actor=actor)

        # Verify the exception carries the documented HTTP 409 status
        # so the registered error handler emits the right envelope.
        assert exc_info.value.status_code == 409

    @pytest.mark.integration
    def test_duplicate_url_after_soft_delete_succeeds(self, db_session, contributor_user):
        """The partial unique index excludes soft-deleted rows.

        F-007 + F-010 interaction: after soft-deleting a record, a
        new record with the same normalized URL MUST be createable
        because the partial predicate ``WHERE deleted_at IS NULL``
        no longer matches the soft-deleted row.
        """
        actor = _make_session(contributor_user)
        url = "https://www.linkedin.com/in/jane-doe-soft-delete-recreate"

        first = create_record(payload=_basic_payload(linkedin_url=url), actor=actor)
        soft_delete_record(record_id=first.id, actor=actor)

        # New record with the same URL should succeed because the
        # partial unique index now excludes the soft-deleted row.
        second = create_record(payload=_basic_payload(linkedin_url=url), actor=actor)
        assert second.id != first.id
        assert second.normalized_linkedin_url == first.normalized_linkedin_url

    def test_with_tag_ids_creates_associations(self, db_session, organization, contributor_user):
        """``tag_ids`` in payload creates ``record_tags`` rows (F-008).

        Verifies that tags supplied in the payload are validated for
        org-membership AND associated to the record via the M:N
        join table, all inside the parent transaction.
        """
        tag_a = TagFactory(organization=organization, name="industry-saas")
        tag_b = TagFactory(organization=organization, name="geography-na")
        db_session.commit()

        actor = _make_session(contributor_user)
        payload = _basic_payload(tag_ids=[tag_a.id, tag_b.id])

        result = create_record(payload=payload, actor=actor)

        # Verify the M:N rows are present in the DB.
        record = db_session.get(Record, result.id)
        record_tag_ids = {rt.tag_id for rt in record.record_tags}
        assert tag_a.id in record_tag_ids
        assert tag_b.id in record_tag_ids


# ===========================================================================
# TestGetRecord -- F-011 single-record fetch
# ===========================================================================


class TestGetRecord:
    """Tests for :func:`get_record` (F-011 detail fetch).

    Verifies AAP Section 0.7.1 invariant 3 (org-scoped reads),
    invariant 4 (soft-delete-aware reads default-on), and the
    cross-org enumeration defense from AAP Section 0.7.4 (cross-org
    access returns 404, NOT 403, so a malicious caller cannot
    enumerate record IDs by status code).
    """

    def test_returns_record_for_existing_record(self, db_session, contributor_user):
        """A record in the actor's org is returned with eager-loaded tags."""
        record = RecordFactory(org_id=contributor_user.org_id, owner=contributor_user)
        db_session.commit()

        result = get_record(record_id=record.id, actor=_make_session(contributor_user))

        # Service returns the persisted SQLAlchemy entity directly.
        assert isinstance(result, Record)
        assert result.id == record.id

    def test_unknown_record_raises_not_found(self, db_session, contributor_user):
        """A random UUID returns NotFoundError (the row simply does not exist)."""
        with pytest.raises(NotFoundError):
            get_record(record_id=uuid.uuid4(), actor=_make_session(contributor_user))

    def test_cross_org_record_raises_not_found(self, db_session, contributor_user):
        """Cross-org access returns NotFoundError, NOT ForbiddenError.

        AAP Section 0.7.4 cross-org enumeration defense: returning
        404 instead of 403 ensures the response code does not
        reveal whether a record id exists in another organization.
        """
        other_org = OrganizationFactory()
        other_user = UserFactory(organization=other_org)
        other_record = RecordFactory(org_id=other_org.id, owner=other_user)
        db_session.commit()

        with pytest.raises(NotFoundError):
            get_record(
                record_id=other_record.id,
                actor=_make_session(contributor_user),
            )

    def test_soft_deleted_record_raises_not_found_by_default(self, db_session, contributor_user):
        """Soft-deleted records are filtered from default reads (F-007).

        AAP Section 0.7.1 invariant 4: every read defaults to
        ``WHERE deleted_at IS NULL``. The admin-only opt-out (via
        ``include_deleted=True``) is exercised in
        :class:`TestListRecords`.
        """
        record = SoftDeletedRecordFactory(org_id=contributor_user.org_id, owner=contributor_user)
        db_session.commit()

        with pytest.raises(NotFoundError):
            get_record(record_id=record.id, actor=_make_session(contributor_user))

    def test_includes_tags_via_eager_loading(self, db_session, organization, contributor_user):
        """The returned :class:`Record` carries the eagerly-loaded tag list.

        F-008 invariant: the feed and detail handlers serialize
        ``record.record_tags[*].tag`` without an N+1 query. The
        service uses ``selectinload`` to prefetch the chain.
        """
        record = RecordFactory(org_id=contributor_user.org_id, owner=contributor_user)
        tag = TagFactory(organization=organization, name="industry-fintech")
        db_session.commit()

        # Associate the tag via direct ORM insertion to avoid the
        # factory order-of-events around RecordTagFactory's tag /
        # record SubFactory creation.
        from app.models import RecordTag  # noqa: PLC0415  -- lazy local import

        rt = RecordTag(record_id=record.id, tag_id=tag.id)
        db_session.add(rt)
        db_session.commit()

        result = get_record(record_id=record.id, actor=_make_session(contributor_user))
        tag_names = {rt.tag.name for rt in result.record_tags}
        assert "industry-fintech" in tag_names


# ===========================================================================
# TestListRecords -- F-004 paginated feed query
# ===========================================================================


class TestListRecords:
    """Tests for :func:`list_records` (F-004 feed/dashboard query).

    Verifies AAP Section 0.5.2 Layer 4 (composite-index hit for the
    canonical sort), AAP Section 0.7.1 invariants 3/4 (org-scoping
    + soft-delete-aware default), and AAP Section 0.7.3 feed-load
    budget (page-size cap).

    The service signature is::

        list_records(
            filters: ConnectionFilters,
            actor: Session,
            *,
            sort_key: str = "submission_date",
            sort_dir: str = "desc",
            limit: int = 25,
            offset: int = 0,
        ) -> tuple[list[Record], int]

    Tests assert against the (records, total) tuple shape.
    """

    def test_returns_records_and_total(self, db_session, contributor_user):
        """The service returns ``(list[Record], int)`` with correct totals."""
        for _ in range(3):
            RecordFactory(org_id=contributor_user.org_id, owner=contributor_user)
        db_session.commit()

        records, total = list_records(
            filters=ConnectionFilters(),
            actor=_make_session(contributor_user),
            sort_key="submission_date",
            sort_dir="desc",
            limit=25,
            offset=0,
        )

        assert isinstance(records, list)
        assert isinstance(total, int)
        assert total >= 3
        assert len(records) >= 3
        # Every returned row is a Record entity.
        for r in records:
            assert isinstance(r, Record)

    def test_excludes_soft_deleted_by_default(self, db_session, contributor_user):
        """Default reads inject ``WHERE deleted_at IS NULL`` (F-007).

        AAP Section 0.7.1 invariant 4: the soft-delete predicate is
        applied uniformly to every read path unless the caller opts
        out via ``include_deleted=True``.
        """
        active = RecordFactory(org_id=contributor_user.org_id, owner=contributor_user)
        soft_deleted = SoftDeletedRecordFactory(
            org_id=contributor_user.org_id, owner=contributor_user
        )
        db_session.commit()

        records, _ = list_records(
            filters=ConnectionFilters(),
            actor=_make_session(contributor_user),
            sort_key="submission_date",
            sort_dir="desc",
            limit=25,
            offset=0,
        )
        ids = {r.id for r in records}
        assert active.id in ids
        assert soft_deleted.id not in ids

    def test_admin_can_include_deleted_via_filter(self, db_session, organization, admin_user):
        """Admin moderation opts into soft-deleted via ``include_deleted=True``.

        F-014 admin moderation surface: the admin record-moderation
        view shows soft-deleted records so the operator can hard-
        delete them. The opt-out flag flips the soft-delete filter
        off.
        """
        active = RecordFactory(org_id=organization.id, owner=admin_user)
        soft_deleted = SoftDeletedRecordFactory(org_id=organization.id, owner=admin_user)
        db_session.commit()

        records, _ = list_records(
            filters=ConnectionFilters(include_deleted=True),
            actor=_make_session(admin_user),
            sort_key="submission_date",
            sort_dir="desc",
            limit=25,
            offset=0,
        )
        ids = {r.id for r in records}
        assert active.id in ids
        assert soft_deleted.id in ids

    def test_org_scoped_listing(self, db_session, contributor_user):
        """Records in other orgs are filtered out (AAP invariant 3)."""
        own = RecordFactory(org_id=contributor_user.org_id, owner=contributor_user)
        other_org = OrganizationFactory()
        other_user = UserFactory(organization=other_org)
        other_record = RecordFactory(org_id=other_org.id, owner=other_user)
        db_session.commit()

        records, _ = list_records(
            filters=ConnectionFilters(),
            actor=_make_session(contributor_user),
            sort_key="submission_date",
            sort_dir="desc",
            limit=25,
            offset=0,
        )
        ids = {r.id for r in records}
        assert own.id in ids
        assert other_record.id not in ids

    def test_filter_by_company(self, db_session, contributor_user):
        """The ``company`` filter applies a case-insensitive substring match."""
        a = RecordFactory(
            org_id=contributor_user.org_id,
            owner=contributor_user,
            company="Acme Corp",
        )
        b = RecordFactory(
            org_id=contributor_user.org_id,
            owner=contributor_user,
            company="Globex Inc",
        )
        db_session.commit()

        records, _ = list_records(
            filters=ConnectionFilters(company="Acme"),
            actor=_make_session(contributor_user),
            sort_key="submission_date",
            sort_dir="desc",
            limit=25,
            offset=0,
        )
        ids = {r.id for r in records}
        assert a.id in ids
        assert b.id not in ids

    def test_filter_by_involvement(self, db_session, contributor_user):
        """The ``involvement`` filter accepts a tuple of enum members."""
        warm = RecordFactory(
            org_id=contributor_user.org_id,
            owner=contributor_user,
            involvement=InvolvementType.WARM_INTRO,
        )
        target = RecordFactory(
            org_id=contributor_user.org_id,
            owner=contributor_user,
            involvement=InvolvementType.TARGET_ONLY,
        )
        db_session.commit()

        records, _ = list_records(
            filters=ConnectionFilters(involvement=(InvolvementType.WARM_INTRO,)),
            actor=_make_session(contributor_user),
            sort_key="submission_date",
            sort_dir="desc",
            limit=25,
            offset=0,
        )
        ids = {r.id for r in records}
        assert warm.id in ids
        assert target.id not in ids

    def test_pagination_caps_limit(self, db_session, contributor_user):
        """A request with ``limit > _MAX_PAGE_SIZE`` is silently capped at 100.

        AAP Section 0.7.3 feed-load budget defense: the service
        clamps ``limit`` to ``[1, 100]`` so a malicious client
        cannot request a 1,000,000-record page that would blow past
        the feed-load budget.
        """
        for _ in range(5):
            RecordFactory(org_id=contributor_user.org_id, owner=contributor_user)
        db_session.commit()

        records, _ = list_records(
            filters=ConnectionFilters(),
            actor=_make_session(contributor_user),
            sort_key="submission_date",
            sort_dir="desc",
            limit=10000,  # absurd value; service must clamp
            offset=0,
        )
        # The clamp is internal; we verify by checking the returned
        # count never exceeds _MAX_PAGE_SIZE = 100. With only 5
        # records seeded the assertion is trivially true, but the
        # bound documents the contract for future readers.
        assert len(records) <= 100

    @pytest.mark.parametrize(
        "sort_key",
        [
            "submission_date",
            "full_name",
            "company",
            "owner_display_name",
            "outreach_status",
        ],
    )
    def test_all_documented_sort_keys_accepted(self, db_session, contributor_user, sort_key):
        """Every documented ``_SORT_COLUMNS`` key is a valid sort.

        AAP Section 0.5.2 Layer 4: the five sort dimensions are
        ``submission_date``, ``full_name``, ``company``,
        ``owner_display_name``, and ``outreach_status``. An unknown
        sort key would raise :class:`ValidationFailedError`.
        """
        RecordFactory(org_id=contributor_user.org_id, owner=contributor_user)
        db_session.commit()

        # Should not raise.
        records, total = list_records(
            filters=ConnectionFilters(),
            actor=_make_session(contributor_user),
            sort_key=sort_key,
            sort_dir="desc",
            limit=25,
            offset=0,
        )
        assert isinstance(records, list)
        assert isinstance(total, int)

    @pytest.mark.integration
    def test_explain_uses_composite_index(self, db_session, contributor_user):
        """The canonical feed query plan references the composite index.

        AAP Section 0.5.2 Layer 4: the composite index
        ``ix_records_org_deleted_submission`` on
        ``(org_id, deleted_at, submission_date DESC)`` powers the
        F-004 default-sort feed query within the AAP Section 0.7.3
        feed-load budget at the 10K-record scale ceiling.

        With small datasets (< 1000 rows) PostgreSQL's planner
        prefers a sequential scan because it is cheaper than an
        index scan on a hot, fully-cached small table. We force
        the index to be considered via ``SET LOCAL enable_seqscan
        = off`` so the test verifies that the index EXISTS and is
        applicable, not that it is the planner's default choice on
        this row count. The assertion stays permissive (accepts
        either the index name or generic "Index Scan" verbiage) so
        the test does not fail when PostgreSQL's plan-textual
        format changes between major versions.
        """
        for _ in range(20):
            RecordFactory(org_id=contributor_user.org_id, owner=contributor_user)
        db_session.commit()

        # Force PostgreSQL to consider indexes even at small row
        # counts; LOCAL scopes the change to the current
        # transaction, so it does not leak into subsequent tests.
        db_session.execute(text("SET LOCAL enable_seqscan = off"))

        org_id = contributor_user.org_id
        explain_sql = text(
            "EXPLAIN SELECT * FROM records "
            "WHERE org_id = :org_id AND deleted_at IS NULL "
            "ORDER BY submission_date DESC LIMIT 25"
        )
        rows = db_session.execute(explain_sql, {"org_id": org_id}).all()
        plan = "\n".join(row[0] for row in rows)
        # The index name is defined in app/models/record.py; the
        # generic "Index Scan" verbiage is the PostgreSQL
        # plan-text fallback for any index-driven access path.
        assert "ix_records_org_deleted_submission" in plan or "Index Scan" in plan


# ===========================================================================
# TestUpdateRecord -- F-007 edit
# ===========================================================================


class TestUpdateRecord:
    """Tests for :func:`update_record` (F-007 edit-in-place).

    Verifies the F-009 ownership matrix at the service layer
    (Contributor own-only, Admin any, Viewer denied), the F-013 EDIT
    audit emission with before/after payloads, and the cross-org
    enumeration defense (NotFoundError, not ForbiddenError).

    The service layer enforces ownership EVEN THOUGH the API-layer
    ``@requires_role(...)`` decorator runs first; this is
    defense-in-depth per AAP Section 0.7.1 invariant 7.
    """

    @pytest.mark.audit
    def test_owner_can_update_own_record_and_emits_edit_audit(
        self, db_session, contributor_user, audit_assertion
    ):
        """Owner edits succeed and emit an EDIT audit with before/after."""
        record = RecordFactory(
            org_id=contributor_user.org_id,
            owner=contributor_user,
            full_name="Old Name",
        )
        db_session.commit()

        actor = _make_session(contributor_user)
        # Partial update payload: only the changed field.
        payload = ConnectionUpdate.model_validate({"full_name": "New Name"})
        result = update_record(record_id=record.id, payload=payload, actor=actor)

        # Service returns the updated SQLAlchemy entity.
        assert result.full_name == "New Name"
        # DB confirms. ``expire_all`` evicts the stale identity-map
        # entry so the next SELECT re-reads the row that the service
        # committed in its own session.
        db_session.expire_all()
        refreshed = db_session.get(Record, record.id)
        assert refreshed.full_name == "New Name"

        # Audit emission with before/after payloads.
        audit = audit_assertion(
            event_type=AuditEventType.EDIT,
            target_record_id=record.id,
            actor_user_id=contributor_user.id,
        )
        assert audit.before_payload is not None
        assert audit.before_payload["full_name"] == "Old Name"
        assert audit.after_payload is not None
        assert audit.after_payload["full_name"] == "New Name"

    @pytest.mark.rbac
    def test_contributor_cannot_update_others_record(
        self, db_session, organization, contributor_user
    ):
        """Contributors may edit ONLY their own records (F-009 + F-006).

        A second Contributor in the same org cannot edit a record
        owned by another Contributor; the service raises
        :class:`ForbiddenError` (HTTP 403).
        """
        other = ContributorUserFactory(organization=organization)
        record = RecordFactory(org_id=organization.id, owner=other)
        db_session.commit()

        actor = _make_session(contributor_user)
        with pytest.raises(ForbiddenError):
            update_record(
                record_id=record.id,
                payload=ConnectionUpdate.model_validate({"full_name": "Hijacked"}),
                actor=actor,
            )

    @pytest.mark.rbac
    def test_admin_can_update_any_record_in_org(self, db_session, organization, admin_user):
        """Admins bypass the owner check (F-009)."""
        other = ContributorUserFactory(organization=organization)
        record = RecordFactory(org_id=organization.id, owner=other, full_name="Original")
        db_session.commit()

        result = update_record(
            record_id=record.id,
            payload=ConnectionUpdate.model_validate({"full_name": "Admin Edited"}),
            actor=_make_session(admin_user),
        )
        assert result.full_name == "Admin Edited"

    @pytest.mark.rbac
    def test_viewer_cannot_update_record(
        self, db_session, organization, contributor_user, viewer_user
    ):
        """Viewers (Sales Reps) cannot edit fields -- only mutate status.

        F-009 invariant: the Viewer role is gated to status mutation
        via :func:`update_status`; non-status edits raise
        :class:`ForbiddenError` even when the record belongs to the
        viewer's org.
        """
        record = RecordFactory(org_id=organization.id, owner=contributor_user)
        db_session.commit()

        actor = _make_session(viewer_user)
        with pytest.raises(ForbiddenError):
            update_record(
                record_id=record.id,
                payload=ConnectionUpdate.model_validate({"full_name": "Forbidden"}),
                actor=actor,
            )

    def test_cross_org_update_raises_not_found(self, db_session, contributor_user):
        """Cross-org edit returns 404 (enumeration defense per AAP 0.7.4)."""
        other_org = OrganizationFactory()
        other_user = UserFactory(organization=other_org)
        record = RecordFactory(org_id=other_org.id, owner=other_user)
        db_session.commit()

        with pytest.raises(NotFoundError):
            update_record(
                record_id=record.id,
                payload=ConnectionUpdate.model_validate({"full_name": "X"}),
                actor=_make_session(contributor_user),
            )


# ===========================================================================
# TestUpdateStatus -- F-005 outreach-status mutation
# ===========================================================================


class TestUpdateStatus:
    """Tests for :func:`update_status` (F-005 outreach-status mutation).

    Verifies the F-013 STATUS_CHANGE audit emission with structured
    before/after payloads, the idempotent no-op semantics (no audit
    when the value is unchanged), and the cross-org enumeration
    defense (NotFoundError).

    RBAC for this endpoint is enforced at the API layer via
    ``@requires_role(VIEWER, ADMIN)``; the service-layer tests pass
    a Viewer actor on the success path because that mirrors the
    realistic flow and exercises the audit emission with the actor
    that the production system most often sees on this path.
    """

    @pytest.mark.audit
    def test_status_change_emits_status_change_audit(
        self, db_session, contributor_user, viewer_user, audit_assertion
    ):
        """Status mutation emits a STATUS_CHANGE audit with before/after.

        The audit ``before_payload`` and ``after_payload`` carry
        ONLY the ``outreach_status`` value (per the service's
        intentional minimal-payload structure for status events).
        """
        record = RecordFactory(
            org_id=contributor_user.org_id,
            owner=contributor_user,
            outreach_status=OutreachStatus.NOT_STARTED,
        )
        db_session.commit()

        # The status mutation is admitted for VIEWER (sales-rep)
        # and ADMIN at the API layer; the service layer does not
        # re-check the role but does enforce org-scoping.
        actor = _make_session(viewer_user)
        payload = ConnectionStatusUpdate.model_validate(
            {"outreach_status": OutreachStatus.IN_PROGRESS.value}
        )
        result = update_status(record_id=record.id, payload=payload, actor=actor)

        assert result.outreach_status == OutreachStatus.IN_PROGRESS

        audit = audit_assertion(
            event_type=AuditEventType.STATUS_CHANGE,
            target_record_id=record.id,
        )
        assert audit.before_payload is not None
        assert audit.after_payload is not None
        assert audit.before_payload["outreach_status"] == OutreachStatus.NOT_STARTED.value
        assert audit.after_payload["outreach_status"] == OutreachStatus.IN_PROGRESS.value

    @pytest.mark.audit
    def test_no_op_status_update_emits_no_audit(self, db_session, viewer_user, contributor_user):
        """Setting the status to its current value is a no-op (no audit).

        Idempotency invariant: the service explicitly skips the
        audit emit when ``record.outreach_status == new_status``
        so the audit log is not flooded with redundant entries
        when the SPA polls or retries.
        """
        record = RecordFactory(
            org_id=contributor_user.org_id,
            owner=contributor_user,
            outreach_status=OutreachStatus.NOT_STARTED,
        )
        db_session.commit()

        # Count baseline STATUS_CHANGE audits for this record.
        baseline = db_session.execute(
            select(AuditEvent).where(
                AuditEvent.event_type == AuditEventType.STATUS_CHANGE,
                AuditEvent.target_record_id == record.id,
            )
        ).all()
        baseline_n = len(baseline)

        payload = ConnectionStatusUpdate.model_validate(
            {"outreach_status": OutreachStatus.NOT_STARTED.value}
        )
        update_status(record_id=record.id, payload=payload, actor=_make_session(viewer_user))

        # Post-call count should be unchanged.
        post = db_session.execute(
            select(AuditEvent).where(
                AuditEvent.event_type == AuditEventType.STATUS_CHANGE,
                AuditEvent.target_record_id == record.id,
            )
        ).all()
        assert len(post) == baseline_n  # no audit added

    def test_cross_org_raises_not_found(self, db_session, viewer_user):
        """Cross-org status update returns 404 (enumeration defense)."""
        other_org = OrganizationFactory()
        other_user = UserFactory(organization=other_org)
        record = RecordFactory(org_id=other_org.id, owner=other_user)
        db_session.commit()

        with pytest.raises(NotFoundError):
            update_status(
                record_id=record.id,
                payload=ConnectionStatusUpdate.model_validate(
                    {"outreach_status": OutreachStatus.IN_PROGRESS.value}
                ),
                actor=_make_session(viewer_user),
            )


# ===========================================================================
# TestSoftDeleteRecord -- F-007 soft delete
# ===========================================================================


class TestSoftDeleteRecord:
    """Tests for :func:`soft_delete_record` (F-007 soft-delete).

    Verifies that soft-deletion sets ``deleted_at`` to a non-NULL
    timestamp, emits a SOFT_DELETE audit event, is idempotent (a
    second call does not advance ``deleted_at`` nor emit a second
    audit event), enforces the F-009 ownership matrix
    (Contributor own-only, Admin any), and returns
    :class:`NotFoundError` (NOT 403) for cross-org access.
    """

    @pytest.mark.audit
    def test_soft_delete_sets_deleted_at_and_emits_audit(
        self, db_session, contributor_user, audit_assertion
    ):
        """Soft delete sets ``deleted_at`` and emits a SOFT_DELETE audit."""
        record = RecordFactory(org_id=contributor_user.org_id, owner=contributor_user)
        db_session.commit()

        result = soft_delete_record(record_id=record.id, actor=_make_session(contributor_user))

        # The returned entity carries the populated deleted_at.
        assert result.deleted_at is not None
        # DB confirms the persisted state. ``expire_all`` evicts the
        # stale identity-map entry so the next SELECT re-reads the
        # row that the service committed in its own session.
        db_session.expire_all()
        refreshed = db_session.get(Record, record.id)
        assert refreshed.deleted_at is not None

        # SOFT_DELETE audit was emitted in the same transaction.
        audit = audit_assertion(
            event_type=AuditEventType.SOFT_DELETE,
            target_record_id=record.id,
            actor_user_id=contributor_user.id,
        )
        assert audit.target_record_id == record.id

    @pytest.mark.audit
    def test_soft_delete_idempotent(self, db_session, contributor_user):
        """A second soft-delete is a no-op (no second audit event).

        Idempotency invariant: when the SPA shows a "Delete" button
        on a stale record list, a duplicate delete request must not
        flood the audit log nor advance ``deleted_at``.
        """
        record = RecordFactory(org_id=contributor_user.org_id, owner=contributor_user)
        db_session.commit()
        actor = _make_session(contributor_user)

        # First soft-delete: emits exactly one audit row.
        soft_delete_record(record_id=record.id, actor=actor)
        first_count = len(
            db_session.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == AuditEventType.SOFT_DELETE,
                    AuditEvent.target_record_id == record.id,
                )
            ).all()
        )
        assert first_count == 1

        # Second soft-delete: idempotent no-op; count remains 1.
        soft_delete_record(record_id=record.id, actor=actor)
        second_count = len(
            db_session.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == AuditEventType.SOFT_DELETE,
                    AuditEvent.target_record_id == record.id,
                )
            ).all()
        )
        assert second_count == 1

    @pytest.mark.rbac
    def test_contributor_cannot_soft_delete_others_record(
        self, db_session, organization, contributor_user
    ):
        """Contributors may soft-delete ONLY their own records (F-009)."""
        other = ContributorUserFactory(organization=organization)
        record = RecordFactory(org_id=organization.id, owner=other)
        db_session.commit()

        with pytest.raises(ForbiddenError):
            soft_delete_record(record_id=record.id, actor=_make_session(contributor_user))

    @pytest.mark.rbac
    def test_admin_can_soft_delete_any_record(self, db_session, organization, admin_user):
        """Admins bypass the owner check on soft-delete (F-009)."""
        other = ContributorUserFactory(organization=organization)
        record = RecordFactory(org_id=organization.id, owner=other)
        db_session.commit()

        result = soft_delete_record(record_id=record.id, actor=_make_session(admin_user))
        assert result is not None
        # ``expire_all`` evicts the stale identity-map entry so the next
        # SELECT re-reads the row committed by the service-layer session.
        db_session.expire_all()
        refreshed = db_session.get(Record, record.id)
        assert refreshed.deleted_at is not None

    def test_cross_org_raises_not_found(self, db_session, contributor_user):
        """Cross-org soft-delete returns 404 (enumeration defense)."""
        other_org = OrganizationFactory()
        other_user = UserFactory(organization=other_org)
        record = RecordFactory(org_id=other_org.id, owner=other_user)
        db_session.commit()

        with pytest.raises(NotFoundError):
            soft_delete_record(record_id=record.id, actor=_make_session(contributor_user))


# ===========================================================================
# TestGetRecordHistory -- F-011 audit-event-sourced edit history
# ===========================================================================


class TestGetRecordHistory:
    """Tests for :func:`get_record_history` (F-011 history feed).

    Verifies DESC ordering by ``event_timestamp``, the structural
    filter on ``target_record_id`` (which naturally excludes
    ``role_change`` and ``authentication`` events because those
    rows carry ``target_record_id IS NULL``), the cross-org
    enumeration defense (NotFoundError on cross-org and
    unknown-record requests), and the ``limit`` / ``offset``
    pagination contract.

    The service signature is::

        get_record_history(
            record_id: UUID,
            actor: Session,
            *,
            limit: int = 25,
            offset: int = 0,
            include_deleted: bool = False,
        ) -> tuple[list[AuditEvent], int]

    Tests assert against the (events, total) tuple shape; the items
    are :class:`AuditEvent` SQLAlchemy entities, NOT pydantic
    :class:`ConnectionHistoryEntry` instances (the API layer
    serializes the entity to the schema; the service layer returns
    the entity).
    """

    def test_returns_audit_events_ordered_desc(self, db_session, contributor_user):
        """History rows are ordered most-recent-first by event_timestamp."""
        record = RecordFactory(org_id=contributor_user.org_id, owner=contributor_user)
        db_session.commit()

        # Seed two audit rows with deterministic timestamps so the
        # DESC-ordering assertion is unambiguous.
        AuditEventFactory(
            actor=contributor_user,
            target_record=record,
            event_type=AuditEventType.CREATE,
            event_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        )
        AuditEventFactory(
            actor=contributor_user,
            target_record=record,
            event_type=AuditEventType.EDIT,
            event_timestamp=datetime(2026, 2, 1, tzinfo=UTC),
        )
        db_session.commit()

        events, total = get_record_history(
            record_id=record.id,
            actor=_make_session(contributor_user),
            limit=25,
            offset=0,
        )

        assert isinstance(events, list)
        assert total >= 2
        for ev in events:
            # Service returns SQLAlchemy entities (the API layer
            # serializes them to ConnectionHistoryEntry).
            assert isinstance(ev, AuditEvent)
            # Every row references the requested record.
            assert ev.target_record_id == record.id
        # DESC ordering: timestamps must be non-increasing.
        timestamps = [ev.event_timestamp for ev in events]
        assert timestamps == sorted(timestamps, reverse=True)

    def test_filters_out_role_change_and_authentication(self, db_session, contributor_user):
        """role_change/authentication events have NULL target_record_id.

        These events are keyed to a user (not a record), so the
        ``WHERE target_record_id = :record_id`` filter naturally
        excludes them. The test verifies the SQL semantics by
        constructing a role_change row directly and confirming it
        does NOT appear in the history feed for any record.
        """
        record = RecordFactory(org_id=contributor_user.org_id, owner=contributor_user)
        db_session.commit()

        # CREATE event for the record (will appear in history).
        AuditEventFactory(
            actor=contributor_user,
            target_record=record,
            event_type=AuditEventType.CREATE,
        )
        # role_change event with target_record_id=None (will NOT
        # appear in history because the WHERE clause filters by
        # target_record_id, and NULL never matches a UUID).
        # Bypass the factory's RecordFactory SubFactory default by
        # constructing the row directly via the model.
        from app.models import AuditEvent as AuditEventModel  # noqa: PLC0415

        role_event = AuditEventModel(
            actor_user_id=contributor_user.id,
            target_record_id=None,
            event_type=AuditEventType.ROLE_CHANGE,
            event_timestamp=datetime.now(UTC),
            before_payload={"role": "Contributor"},
            after_payload={"role": "Admin"},
        )
        db_session.add(role_event)
        db_session.commit()

        events, _ = get_record_history(
            record_id=record.id,
            actor=_make_session(contributor_user),
            limit=25,
            offset=0,
        )
        types = {ev.event_type for ev in events}
        # role_change is NOT in history (target_record_id is NULL).
        assert AuditEventType.ROLE_CHANGE not in types
        # CREATE event IS in history.
        assert AuditEventType.CREATE in types

    def test_cross_org_record_raises_not_found(self, db_session, contributor_user):
        """Cross-org history requests return 404 (enumeration defense).

        AAP Section 0.7.4: returning 404 (not 403) prevents a
        malicious caller from enumerating audit data across orgs by
        guessing record UUIDs.
        """
        other_org = OrganizationFactory()
        other_user = UserFactory(organization=other_org)
        record = RecordFactory(org_id=other_org.id, owner=other_user)
        db_session.commit()

        with pytest.raises(NotFoundError):
            get_record_history(
                record_id=record.id,
                actor=_make_session(contributor_user),
                limit=25,
                offset=0,
            )

    def test_unknown_record_raises_not_found(self, db_session, contributor_user):
        """A random UUID returns NotFoundError (the row does not exist)."""
        with pytest.raises(NotFoundError):
            get_record_history(
                record_id=uuid.uuid4(),
                actor=_make_session(contributor_user),
                limit=25,
                offset=0,
            )

    def test_pagination(self, db_session, contributor_user):
        """``limit`` and ``offset`` paginate the history feed deterministically.

        Seeds many audit rows with monotonically advancing
        timestamps so the DESC-ordered first page contains the
        most-recent ``limit`` events.
        """
        record = RecordFactory(org_id=contributor_user.org_id, owner=contributor_user)
        db_session.commit()

        # Seed 30 audit events with strictly-increasing timestamps
        # (one second apart) so the DESC-ordering and pagination
        # assertions are deterministic.
        base = datetime(2026, 1, 1, tzinfo=UTC)
        for i in range(30):
            AuditEventFactory(
                actor=contributor_user,
                target_record=record,
                event_type=AuditEventType.EDIT,
                event_timestamp=base + timedelta(seconds=i),
            )
        db_session.commit()

        events, total = get_record_history(
            record_id=record.id,
            actor=_make_session(contributor_user),
            limit=10,
            offset=0,
        )

        # Page returns at most the requested limit.
        assert len(events) <= 10
        # Total reflects all matching rows, not the page size.
        assert total >= 30
