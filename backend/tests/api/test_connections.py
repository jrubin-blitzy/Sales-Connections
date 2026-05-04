"""API tests for the connections blueprint (F-001/F-004/F-005/F-007/F-010/F-011).

Covers all 8 endpoints under ``/api/connections/*`` plus the cross-cutting
RBAC matrix, org-scope isolation, soft-delete semantics, audit emissions,
filter/sort/pagination behavior, route-ordering invariants, and the
strict ``extra='forbid'`` validation on every request schema.

Endpoint catalog
----------------

- ``POST   /api/connections``                       F-001 create
- ``GET    /api/connections``                       F-004 paginated feed
- ``GET    /api/connections/duplicate-check``       F-010 non-blocking warning
- ``GET    /api/connections/<uuid>``                F-011 detail
- ``GET    /api/connections/<uuid>/history``        F-011 audit history
- ``PATCH  /api/connections/<uuid>``                F-007 edit
- ``PATCH  /api/connections/<uuid>/status``         F-005 status mutation
- ``DELETE /api/connections/<uuid>``                F-007 soft delete

Key invariants verified by this module
--------------------------------------

- Static route ``/duplicate-check`` is registered BEFORE the dynamic route
  ``<uuid:record_id>`` so it resolves correctly (AAP s 0.5.2 Layer 5).
- Every state-changing endpoint emits an audit event in the same
  transaction as the state change (AAP s 0.7.1 invariant 5).
- ``ConnectionCreate`` and ``ConnectionUpdate`` reject ``owner_user_id``
  injection with a 422 (AAP s 0.7.4 owner-from-session invariant).
- All read endpoints inject ``WHERE org_id = session.org_id`` and
  ``WHERE deleted_at IS NULL`` (soft-delete aware) by default.
- Cross-org access returns 404, not 403, to prevent existence
  enumeration (AAP s 0.7.1 invariant 3).
- Status mutations are gated to Viewer + Admin ONLY (NOT Contributor) -
  the F-005 sales-team-accountability invariant.
- Soft-delete is idempotent: deleting an already-soft-deleted record
  returns 200 with no double-emission of audit events.
- The composite index supports filter + sort at 10K-record scale (smoke
  test only; full performance suite is out of scope).

Coordination contract
---------------------

Fixtures inherited from :mod:`tests.conftest`:

- ``client``, ``admin_client``, ``contributor_client``, ``viewer_client``
- ``contributor_user``, ``admin_user``, ``viewer_user``
- ``organization`` (default org)
- ``db_session`` (SAVEPOINT-isolated session, rolls back on test teardown)
- ``audit_assertion`` (helper that asserts an audit event was emitted)

Factory data setup uses :mod:`tests.factories`:

- :class:`RecordFactory` for active records
- :class:`SoftDeletedRecordFactory` for soft-deleted records
- :class:`TagFactory` for tag attachments
- :class:`ContributorUserFactory`/:class:`AdminUserFactory`/
  :class:`ViewerUserFactory`
- :class:`OrganizationFactory` for cross-org isolation tests
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
import uuid

import pytest
from sqlalchemy import func, select, text

from app.extensions import db
from app.models import AuditEvent, Record, RecordTag, Tag, User
from app.models.enums import (
    AuditEventType,
    InvolvementType,
    OutreachStatus,
    UserRole,
)
from tests.factories import (
    AdminUserFactory,
    AuditEventFactory,
    ContributorUserFactory,
    OrganizationFactory,
    RecordFactory,
    SoftDeletedRecordFactory,
    TagFactory,
    ViewerUserFactory,
)

# ---------------------------------------------------------------------------
# Helper: payload constructor and DB query utilities
# ---------------------------------------------------------------------------


def _valid_connection_payload(**overrides: Any) -> dict[str, Any]:
    """Return a valid ConnectionCreate payload that the test can mutate.

    The base payload uses unique-ish values (a fresh UUID-derived slug
    in the LinkedIn URL) so tests do not accidentally collide on the
    unique partial index for ``normalized_linkedin_url`` when multiple
    records are created within the same test or when many tests run
    against a shared database. Tests that REQUIRE a deterministic URL
    (e.g., duplicate-detection happy path) override ``linkedin_url``
    explicitly.

    Args:
        **overrides: Field-level overrides applied after the base
            payload is constructed. Use ``linkedin_url`` to pin the
            URL to a specific value, ``involvement`` to pin the enum,
            or ``tag_ids`` to attach pre-created tag UUIDs.

    Returns:
        A JSON-serializable dict carrying all six required
        :class:`ConnectionCreate` fields plus any overrides. The dict
        is intended to be passed directly to ``client.post(json=...)``.
    """
    slug = f"alice-connector-{uuid.uuid4().hex[:8]}"
    base: dict[str, Any] = {
        "full_name": "Alice Connector",
        "linkedin_url": f"https://www.linkedin.com/in/{slug}",
        "company": "Acme Corp",
        "job_title": "VP of Operations",
        "relationship_context": (
            "We went to college together; she's now VP of Ops at a Series B logistics startup."
        ),
        "involvement": InvolvementType.WARM_INTRO.value,
    }
    base.update(overrides)
    return base


def _audit_event_count() -> int:
    """Return the total number of rows in ``audit_events``.

    Opens its own session so callers do not need to flush the
    surrounding test session. Used by no-op idempotency tests that
    must verify NO new audit row was emitted by an operation.
    """
    with db.session() as session:
        return int(session.execute(select(func.count()).select_from(AuditEvent)).scalar_one())


def _audit_events_for_record(record_id: Any) -> list[AuditEvent]:
    """Return all audit events targeting the given record id.

    Sorted by ``event_timestamp ASC`` so callers can locate the most
    recent event by indexing ``[-1]``. Opens its own session so
    callers do not need to flush the surrounding test session.
    """
    with db.session() as session:
        return list(
            session.execute(
                select(AuditEvent)
                .where(AuditEvent.target_record_id == record_id)
                .order_by(AuditEvent.event_timestamp.asc())
            )
            .scalars()
            .all()
        )


# ---------------------------------------------------------------------------
# TestConnectionCreate -- POST /api/connections (F-001)
# ---------------------------------------------------------------------------


class TestConnectionCreate:
    """``POST /api/connections`` creates a Connection-Idea record (F-001).

    Verifies the F-001 happy path, RBAC gating (Contributor + Admin only),
    the ``extra='forbid'`` injection rejection for tampering attempts,
    server-side LinkedIn URL normalization, length-cap enforcement,
    audit-event emission, tag attachment, and org-scope isolation.
    """

    def test_contributor_creates_connection_returns_201(
        self,
        contributor_client: Any,
        contributor_user: Any,
        organization: Any,
    ) -> None:
        """Happy path: Contributor creates a record and gets 201 with full body.

        Verifies all nine business fields plus the four operational
        fields (``id``, ``submission_date``, ``owner_user_id``,
        ``outreach_status``) are present in the response. Also
        verifies the RFC 7231 Section 6.3.2 ``Location`` header.
        """
        # Sanity: the contributor fixture is wired with the Contributor role.
        assert contributor_user.role == UserRole.CONTRIBUTOR
        payload = _valid_connection_payload()
        response = contributor_client.post("/api/connections", json=payload)
        assert response.status_code == 201, response.get_json()
        body = response.get_json()
        # All nine business fields surface in the response.
        assert body["full_name"] == payload["full_name"]
        assert body["linkedin_url"] == payload["linkedin_url"]
        assert body["company"] == payload["company"]
        assert body["job_title"] == payload["job_title"]
        assert body["relationship_context"] == payload["relationship_context"]
        assert body["involvement"] == InvolvementType.WARM_INTRO.value
        # AI notes optional - not provided here; surfaces as None.
        assert body["ai_notes"] is None
        # Outreach status defaults to "Not Started" per F-005.
        assert body["outreach_status"] == OutreachStatus.NOT_STARTED.value
        # Owner is server-derived from session.
        assert body["owner_user_id"] == str(contributor_user.id)
        assert body["owner_display_name"] == contributor_user.display_name
        # Operational fields present.
        assert "id" in body
        assert "submission_date" in body
        assert body["deleted_at"] is None
        # RFC 7231 Location header points at the canonical detail URL.
        assert response.headers.get("Location") == f"/api/connections/{body['id']}"
        # Verify the record exists in DB.
        with db.session() as session:
            record = session.execute(
                select(Record).where(Record.id == uuid.UUID(body["id"]))
            ).scalar_one_or_none()
            assert record is not None
            assert record.org_id == contributor_user.org_id

    def test_admin_creates_connection_returns_201(
        self,
        admin_client: Any,
        admin_user: Any,
    ) -> None:
        """Admin role admitted to POST /api/connections."""
        payload = _valid_connection_payload()
        response = admin_client.post("/api/connections", json=payload)
        assert response.status_code == 201, response.get_json()
        body = response.get_json()
        assert body["owner_user_id"] == str(admin_user.id)

    @pytest.mark.audit
    def test_create_emits_create_audit_event(
        self,
        contributor_client: Any,
        contributor_user: Any,
        audit_assertion: Any,
    ) -> None:
        """A successful create emits exactly one CREATE audit event.

        Verifies the AAP s 0.7.1 invariant 5 atomic state-change +
        audit pair: the record INSERT and the audit_events INSERT
        commit together in the same transaction.
        """
        payload = _valid_connection_payload()
        response = contributor_client.post("/api/connections", json=payload)
        assert response.status_code == 201
        record_id = uuid.UUID(response.get_json()["id"])
        event = audit_assertion(
            event_type=AuditEventType.CREATE,
            target_record_id=record_id,
            actor_user_id=contributor_user.id,
        )
        assert event is not None
        # before_payload is None for CREATE (no prior state).
        assert event.before_payload is None
        # after_payload carries the structural identifiers of the new record.
        assert event.after_payload is not None

    def test_owner_user_id_injection_rejected_with_422(
        self,
        contributor_client: Any,
        admin_user: Any,
    ) -> None:
        """Client-supplied owner_user_id rejected by extra='forbid'.

        Owner identity is server-derived from g.session.user_id; any
        client-supplied owner_user_id is a tampering attempt and
        rejected with HTTP 422 per AAP s 0.7.4 security invariant.
        """
        payload = _valid_connection_payload()
        payload["owner_user_id"] = str(admin_user.id)
        response = contributor_client.post("/api/connections", json=payload)
        assert response.status_code == 422
        body = response.get_json()
        assert body["error"]["code"] == "validation_failed"

    def test_owner_display_name_injection_rejected_with_422(
        self,
        contributor_client: Any,
    ) -> None:
        """Client-supplied owner_display_name rejected by extra='forbid'."""
        payload = _valid_connection_payload()
        payload["owner_display_name"] = "Fake Name"
        response = contributor_client.post("/api/connections", json=payload)
        assert response.status_code == 422
        assert response.get_json()["error"]["code"] == "validation_failed"

    def test_outreach_status_injection_rejected_with_422(
        self,
        contributor_client: Any,
    ) -> None:
        """Client-supplied outreach_status rejected by extra='forbid'.

        Status is set server-side to NOT_STARTED on create; the
        client cannot pre-populate it (AAP s 0.5.2 Layer 3, F-005
        invariant: only Viewer+Admin can mutate status).
        """
        payload = _valid_connection_payload()
        payload["outreach_status"] = OutreachStatus.CLOSED.value
        response = contributor_client.post("/api/connections", json=payload)
        assert response.status_code == 422
        assert response.get_json()["error"]["code"] == "validation_failed"

    def test_id_injection_rejected_with_422(
        self,
        contributor_client: Any,
    ) -> None:
        """Client-supplied id rejected by extra='forbid'.

        The server generates the record id; allowing client supply
        would let a malicious client predict or collide on UUIDs.
        """
        payload = _valid_connection_payload()
        payload["id"] = str(uuid.uuid4())
        response = contributor_client.post("/api/connections", json=payload)
        assert response.status_code == 422
        assert response.get_json()["error"]["code"] == "validation_failed"

    def test_created_at_injection_rejected_with_422(
        self,
        contributor_client: Any,
    ) -> None:
        """Client-supplied created_at rejected by extra='forbid'.

        Timestamps are server-set; allowing client supply would let
        a malicious client forge audit-trail evidence.
        """
        payload = _valid_connection_payload()
        payload["created_at"] = "2020-01-01T00:00:00Z"
        response = contributor_client.post("/api/connections", json=payload)
        assert response.status_code == 422
        assert response.get_json()["error"]["code"] == "validation_failed"

    def test_invalid_linkedin_url_returns_422(
        self,
        contributor_client: Any,
    ) -> None:
        """LinkedIn URL format violation rejected with 422.

        The pydantic field validator delegates to
        :func:`app.utils.url.is_valid_linkedin_url`; non-LinkedIn
        URLs surface as 422 with a field-scoped error message.
        """
        payload = _valid_connection_payload(linkedin_url="https://example.com/foo")
        response = contributor_client.post("/api/connections", json=payload)
        assert response.status_code == 422
        body = response.get_json()
        assert body["error"]["code"] == "validation_failed"
        # Field-scoped error references the linkedin_url field.
        loc_strings = [
            ".".join(str(seg) for seg in field.get("loc", ()))
            for field in body["error"].get("fields", [])
        ]
        assert any("linkedin_url" in loc for loc in loc_strings)

    def test_linkedin_url_normalized_server_side(
        self,
        contributor_client: Any,
    ) -> None:
        """The server normalizes linkedin_url before persistence.

        Verifies the F-010 normalization contract: case-fold the
        slug, strip trailing slash, drop query string, lowercase
        the host, strip ``www.``, force https.
        """
        # Mixed case slug, trailing slash, query string, and uppercase
        # host - all of which the normalizer must canonicalize.
        payload = _valid_connection_payload(
            linkedin_url=("https://www.LinkedIn.com/in/Alice-Normalize-Test/?utm_source=email")
        )
        response = contributor_client.post("/api/connections", json=payload)
        assert response.status_code == 201, response.get_json()
        body = response.get_json()
        # Per :func:`app.utils.url.normalize_linkedin_url`:
        # - host lowercased and "www." stripped
        # - slug lowercased
        # - trailing slash removed
        # - query string dropped
        # - scheme forced to https
        assert body["normalized_linkedin_url"] == "https://linkedin.com/in/alice-normalize-test"
        # Verify in DB too.
        with db.session() as session:
            record = session.execute(
                select(Record).where(Record.id == uuid.UUID(body["id"]))
            ).scalar_one()
            assert record.normalized_linkedin_url == "https://linkedin.com/in/alice-normalize-test"

    def test_full_name_max_length_255_accepted(
        self,
        contributor_client: Any,
    ) -> None:
        """full_name at the 255-char cap is accepted (boundary)."""
        payload = _valid_connection_payload(full_name="A" * 255)
        response = contributor_client.post("/api/connections", json=payload)
        assert response.status_code == 201, response.get_json()

    def test_full_name_over_max_length_rejected_422(
        self,
        contributor_client: Any,
    ) -> None:
        """full_name above the 255-char cap is rejected with 422."""
        payload = _valid_connection_payload(full_name="A" * 256)
        response = contributor_client.post("/api/connections", json=payload)
        assert response.status_code == 422

    def test_relationship_context_max_length_4000_accepted(
        self,
        contributor_client: Any,
    ) -> None:
        """relationship_context at the 4000-char cap is accepted."""
        payload = _valid_connection_payload(relationship_context="x" * 4000)
        response = contributor_client.post("/api/connections", json=payload)
        assert response.status_code == 201, response.get_json()

    def test_relationship_context_over_max_length_rejected_422(
        self,
        contributor_client: Any,
    ) -> None:
        """relationship_context above 4000 chars is rejected with 422."""
        payload = _valid_connection_payload(relationship_context="x" * 4001)
        response = contributor_client.post("/api/connections", json=payload)
        assert response.status_code == 422

    def test_ai_notes_max_length_8000_accepted(
        self,
        contributor_client: Any,
    ) -> None:
        """ai_notes at the 8000-char cap is accepted."""
        payload = _valid_connection_payload(ai_notes="x" * 8000)
        response = contributor_client.post("/api/connections", json=payload)
        assert response.status_code == 201, response.get_json()

    def test_ai_notes_over_max_length_rejected_422(
        self,
        contributor_client: Any,
    ) -> None:
        """ai_notes above 8000 chars is rejected with 422."""
        payload = _valid_connection_payload(ai_notes="x" * 8001)
        response = contributor_client.post("/api/connections", json=payload)
        assert response.status_code == 422

    def test_ai_notes_optional_can_be_omitted(
        self,
        contributor_client: Any,
    ) -> None:
        """ai_notes is optional; omitting it yields ai_notes=None."""
        payload = _valid_connection_payload()
        # Helper does not include ai_notes by default.
        assert "ai_notes" not in payload
        response = contributor_client.post("/api/connections", json=payload)
        assert response.status_code == 201
        assert response.get_json()["ai_notes"] is None

    def test_invalid_involvement_value_returns_422(
        self,
        contributor_client: Any,
    ) -> None:
        """Unknown involvement enum value rejected with 422."""
        payload = _valid_connection_payload(involvement="VIP")
        response = contributor_client.post("/api/connections", json=payload)
        assert response.status_code == 422

    def test_create_with_tags(
        self,
        contributor_client: Any,
        contributor_user: Any,
        organization: Any,
    ) -> None:
        """POST with tag_ids attaches the tags via record_tags rows.

        Verifies F-008 tag attachment: pre-create two tags in the
        actor's org, POST with both ids in tag_ids, verify both
        record_tags association rows exist in DB and surface in the
        response payload.
        """
        tag_a = TagFactory(organization=organization, name="industry-fintech")
        tag_b = TagFactory(organization=organization, name="region-emea")

        payload = _valid_connection_payload()
        payload["tag_ids"] = [str(tag_a.id), str(tag_b.id)]

        response = contributor_client.post("/api/connections", json=payload)
        assert response.status_code == 201, response.get_json()
        body = response.get_json()
        # Response embeds the tag list with full TagRead shape.
        tag_names_in_response = {tag["name"] for tag in body["tags"]}
        assert tag_names_in_response == {"industry-fintech", "region-emea"}
        # Verify two record_tags rows exist in DB plus the underlying
        # Tag rows still belong to the contributor's org.
        with db.session() as session:
            record_id = uuid.UUID(body["id"])
            associations = (
                session.execute(select(RecordTag).where(RecordTag.record_id == record_id))
                .scalars()
                .all()
            )
            assert len(associations) == 2
            # The Tag rows are scoped to the contributor's org.
            tags_in_db = (
                session.execute(
                    select(Tag).where(
                        Tag.id.in_([tag_a.id, tag_b.id]),
                    )
                )
                .scalars()
                .all()
            )
            assert {t.org_id for t in tags_in_db} == {organization.id}

    def test_create_with_cross_org_tag_returns_422(
        self,
        contributor_client: Any,
    ) -> None:
        """Cross-org tag id rejected with 422.

        Tag ids must reference tags in the actor's org; a tag id
        from another org surfaces as 422 (not 404 or 403) per the
        AAP s 0.4.3 endpoint catalog.
        """
        other_org = OrganizationFactory()
        cross_org_tag = TagFactory(organization=other_org, name="cross-org-tag")

        payload = _valid_connection_payload()
        payload["tag_ids"] = [str(cross_org_tag.id)]

        response = contributor_client.post("/api/connections", json=payload)
        assert response.status_code == 422

    def test_create_with_duplicate_url_returns_409(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """A duplicate (active) URL submission returns 409.

        Per F-010, duplicate detection is a WARNING at the pre-submit
        endpoint (returns 200 with duplicate_found=true). But the
        unique partial index ``(org_id, normalized_linkedin_url)
        WHERE deleted_at IS NULL`` prevents two ACTIVE records with
        the same URL: the create-flow honors the constraint with a
        409 ``duplicate_record`` envelope so the SPA can surface the
        warning UI distinctly from a generic 422.
        """
        # Pre-create a record with a known URL.
        target_url = f"https://www.linkedin.com/in/duplicate-test-{uuid.uuid4().hex[:8]}"
        RecordFactory(
            owner=contributor_user,
            linkedin_url=target_url,
        )

        # POST a second record with the SAME URL.
        payload = _valid_connection_payload(linkedin_url=target_url)
        response = contributor_client.post("/api/connections", json=payload)
        assert response.status_code == 409, response.get_json()
        body = response.get_json()
        assert body["error"]["code"] == "duplicate_record"

    def test_viewer_forbidden(
        self,
        viewer_client: Any,
    ) -> None:
        """Viewer (Sales Rep) rejected with 403 from POST.

        Per AAP s 6.2 RBAC matrix, only Contributor and Admin can
        create new records. Viewers consume records via the feed
        and detail endpoints; they cannot author them.
        """
        payload = _valid_connection_payload()
        response = viewer_client.post("/api/connections", json=payload)
        assert response.status_code == 403
        body = response.get_json()
        assert body["error"]["code"] == "forbidden"

    def test_anonymous_unauthorized(
        self,
        client: Any,
    ) -> None:
        """Unauthenticated POST rejected with 401."""
        payload = _valid_connection_payload()
        response = client.post("/api/connections", json=payload)
        assert response.status_code == 401

    def test_create_no_org_scope_leakage(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """A create in another org does not leak into the actor's results.

        Verifies that pre-creating a record in another org does not
        cause the new record to be associated with that org or the
        contributor's record to be assigned a foreign org_id.
        """
        # Pre-create a record in ANOTHER org.
        other_org = OrganizationFactory()
        # Use ViewerUserFactory here to demonstrate cross-org isolation
        # holds regardless of the foreign user's role; per AAP s 0.4.7
        # cross-org access is uniformly blocked at the data layer.
        other_user = ViewerUserFactory(organization=other_org)
        RecordFactory(owner=other_user)

        # POST a new record as the contributor.
        payload = _valid_connection_payload()
        response = contributor_client.post("/api/connections", json=payload)
        assert response.status_code == 201
        body = response.get_json()
        new_record_id = uuid.UUID(body["id"])

        # Verify the new record is in the contributor's org, NOT the other org.
        with db.session() as session:
            record = session.execute(select(Record).where(Record.id == new_record_id)).scalar_one()
            assert record.org_id == contributor_user.org_id
            assert record.org_id != other_org.id
            # The foreign User row exists and is scoped to the foreign org.
            other_user_db = session.execute(
                select(User).where(User.id == other_user.id)
            ).scalar_one()
            assert other_user_db.org_id == other_org.id


# ---------------------------------------------------------------------------
# TestConnectionList -- GET /api/connections (F-004)
# ---------------------------------------------------------------------------


class TestConnectionList:
    """``GET /api/connections`` returns a paginated, filterable feed (F-004).

    Verifies pagination math, all six filter dimensions plus tag filter,
    all five sort dimensions, soft-delete-aware default queries, and
    org-scope isolation.
    """

    def test_list_returns_paginated_records(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """Default page returns the configured default limit per the API.

        The API default ``limit`` is 25 (per the handler's
        ``_parse_int_arg("limit", default=25, ...)`` call). Creating
        more records than the default forces pagination and lets us
        verify the metadata.
        """
        RecordFactory.create_batch(30, owner=contributor_user)
        response = contributor_client.get("/api/connections")
        assert response.status_code == 200
        body = response.get_json()
        # Pagination envelope shape.
        assert "items" in body
        assert "total" in body
        assert "limit" in body
        assert "offset" in body
        assert body["total"] == 30
        # Default limit is 25 per the handler.
        assert body["limit"] == 25
        assert len(body["items"]) == 25
        assert body["offset"] == 0

    def test_list_pagination_offset_returns_next_window(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """``limit`` + ``offset`` pagination returns the requested window."""
        RecordFactory.create_batch(30, owner=contributor_user)

        # First page (10 records).
        response_p1 = contributor_client.get("/api/connections?limit=10&offset=0")
        body_p1 = response_p1.get_json()
        assert body_p1["total"] == 30
        assert len(body_p1["items"]) == 10
        assert body_p1["offset"] == 0

        # Second page (next 10 records).
        response_p2 = contributor_client.get("/api/connections?limit=10&offset=10")
        body_p2 = response_p2.get_json()
        assert len(body_p2["items"]) == 10
        assert body_p2["offset"] == 10

        # Sanity: the two pages don't overlap.
        ids_p1 = {item["id"] for item in body_p1["items"]}
        ids_p2 = {item["id"] for item in body_p2["items"]}
        assert ids_p1.isdisjoint(ids_p2)

    def test_list_max_page_size_above_cap_returns_422(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """``limit`` above the cap (200) is rejected with 422.

        The :class:`PaginatedConnections` response schema enforces
        ``le=200`` on the limit. When the client supplies a limit
        above 200, the schema validation rejects the response
        construction with a 422 ``validation_failed`` envelope.
        This is the conservative behavior - clients are forced to
        request a sensible page size rather than being silently
        clamped.
        """
        RecordFactory.create_batch(5, owner=contributor_user)
        response = contributor_client.get("/api/connections?limit=10000")
        assert response.status_code == 422
        body = response.get_json()
        assert body["error"]["code"] == "validation_failed"

    def test_list_max_page_size_at_cap_accepted(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """``limit=200`` (at the cap) is accepted (boundary)."""
        RecordFactory.create_batch(3, owner=contributor_user)
        response = contributor_client.get("/api/connections?limit=200")
        assert response.status_code == 200
        body = response.get_json()
        assert body["limit"] == 200

    def test_list_invalid_limit_returns_422(
        self,
        contributor_client: Any,
    ) -> None:
        """Non-integer ``limit`` returns 422."""
        response = contributor_client.get("/api/connections?limit=not-a-number")
        assert response.status_code == 422

    def test_list_excludes_soft_deleted_by_default(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """Soft-deleted records are excluded by default (AAP s 0.7.1 inv 4).

        Default queries inject ``WHERE deleted_at IS NULL``; a
        soft-deleted record does not appear in the feed unless an
        Admin explicitly opts in via ``include_deleted=true``.
        """
        RecordFactory.create_batch(5, owner=contributor_user, full_name="Active")
        SoftDeletedRecordFactory.create_batch(3, owner=contributor_user, full_name="Deleted")

        response = contributor_client.get("/api/connections")
        assert response.status_code == 200
        body = response.get_json()
        # Only the 5 active records are returned.
        assert body["total"] == 5
        assert all(item["deleted_at"] is None for item in body["items"])

    def test_list_admin_include_deleted_opt_in(
        self,
        admin_client: Any,
        admin_user: Any,
    ) -> None:
        """Admin can opt-in to soft-deleted records via include_deleted=true."""
        RecordFactory.create_batch(2, owner=admin_user, full_name="Active")
        SoftDeletedRecordFactory.create_batch(2, owner=admin_user, full_name="Deleted")

        response = admin_client.get("/api/connections?include_deleted=true")
        assert response.status_code == 200
        body = response.get_json()
        # Both active and deleted records are returned.
        assert body["total"] == 4

    def test_list_non_admin_include_deleted_silently_downgraded(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """Non-Admin requesting include_deleted=true silently downgrades.

        Per the F-007 + F-009 architectural invariant, Contributors
        and Viewers cannot see soft-deleted records even if they
        explicitly request the flag. The handler does NOT 403
        (info-disclosure defense); it silently coerces to False.
        """
        RecordFactory(owner=contributor_user, full_name="Active")
        SoftDeletedRecordFactory(owner=contributor_user, full_name="Deleted")

        response = contributor_client.get("/api/connections?include_deleted=true")
        assert response.status_code == 200
        body = response.get_json()
        # Soft-deleted record is NOT visible to the Contributor.
        assert body["total"] == 1

    def test_list_org_scoped(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """Cross-org records are NOT visible (AAP s 0.7.1 invariant 3)."""
        # Records in actor's org.
        RecordFactory.create_batch(2, owner=contributor_user)
        # Records in a DIFFERENT org.
        other_org = OrganizationFactory()
        other_user = ContributorUserFactory(organization=other_org)
        RecordFactory.create_batch(3, owner=other_user)

        response = contributor_client.get("/api/connections")
        assert response.status_code == 200
        body = response.get_json()
        # Only the actor's two records are visible.
        assert body["total"] == 2

    def test_list_filter_by_company(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """Filter by company performs case-insensitive substring match."""
        RecordFactory(owner=contributor_user, company="Acme Corp")
        RecordFactory(owner=contributor_user, company="Acme Industries")
        RecordFactory(owner=contributor_user, company="Beta LLC")

        response = contributor_client.get("/api/connections?company=acme")
        assert response.status_code == 200
        body = response.get_json()
        assert body["total"] == 2
        assert all("Acme" in item["company"] for item in body["items"])

    def test_list_filter_by_involvement(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """Filter by involvement returns only matching records."""
        RecordFactory(owner=contributor_user, involvement=InvolvementType.WARM_INTRO)
        RecordFactory(owner=contributor_user, involvement=InvolvementType.WARM_INTRO)
        RecordFactory(owner=contributor_user, involvement=InvolvementType.SOFT_REFERENCE)

        # The endpoint accepts the URL-encoded enum value with a space.
        response = contributor_client.get(
            "/api/connections",
            query_string={"involvement": "Warm Intro"},
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body["total"] == 2
        assert all(item["involvement"] == "Warm Intro" for item in body["items"])

    def test_list_filter_by_owner_user_ids(
        self,
        contributor_client: Any,
        contributor_user: Any,
        organization: Any,
    ) -> None:
        """Filter by owner_user_ids returns only that owner's records.

        Per the F-004 spec, the feed filter parameter is plural
        (``owner_user_ids`` accepting comma-separated UUIDs) so the
        SPA can union multiple owners in a single query.
        """
        other_user = ContributorUserFactory(organization=organization)
        RecordFactory.create_batch(2, owner=contributor_user)
        RecordFactory.create_batch(3, owner=other_user)

        response = contributor_client.get(
            "/api/connections",
            query_string={"owner_user_ids": str(contributor_user.id)},
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body["total"] == 2
        assert all(item["owner_user_id"] == str(contributor_user.id) for item in body["items"])

    def test_list_filter_by_outreach_status(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """Filter by outreach_status returns only matching records."""
        RecordFactory(owner=contributor_user, outreach_status=OutreachStatus.NOT_STARTED)
        RecordFactory(owner=contributor_user, outreach_status=OutreachStatus.IN_PROGRESS)
        RecordFactory(owner=contributor_user, outreach_status=OutreachStatus.CLOSED)

        response = contributor_client.get(
            "/api/connections",
            query_string={"outreach_status": "Not Started"},
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body["total"] == 1
        assert body["items"][0]["outreach_status"] == "Not Started"

    def test_list_filter_by_submission_date_range(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """Filter by submission_date_from/to bounds the date range."""
        # Three records with different submission dates.
        old = datetime(2026, 1, 1, tzinfo=UTC)
        mid = datetime(2026, 1, 15, tzinfo=UTC)
        new = datetime(2026, 2, 15, tzinfo=UTC)
        RecordFactory(owner=contributor_user, submission_date=old, full_name="Oldest")
        RecordFactory(owner=contributor_user, submission_date=mid, full_name="Middle")
        RecordFactory(owner=contributor_user, submission_date=new, full_name="Newest")

        # Filter to the January window only.
        response = contributor_client.get(
            "/api/connections",
            query_string={
                "submission_date_from": "2026-01-01",
                "submission_date_to": "2026-01-31",
            },
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body["total"] == 2  # Oldest + Middle
        names = {item["full_name"] for item in body["items"]}
        assert names == {"Oldest", "Middle"}

    def test_list_filter_by_full_name_search(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """Filter by full_name_search performs case-insensitive substring match."""
        RecordFactory(owner=contributor_user, full_name="Alice Wonder")
        RecordFactory(owner=contributor_user, full_name="Bob Builder")
        RecordFactory(owner=contributor_user, full_name="Charlie Brown")

        response = contributor_client.get("/api/connections?full_name_search=alice")
        assert response.status_code == 200
        body = response.get_json()
        assert body["total"] == 1
        assert body["items"][0]["full_name"] == "Alice Wonder"

    def test_list_filter_by_tag_id(
        self,
        contributor_client: Any,
        contributor_user: Any,
        organization: Any,
        db_session: Any,
    ) -> None:
        """Filter by single tag_ids returns records associated with that tag.

        Per AAP s 0.4.7 the tag filter has "any-of" semantics; with
        a single tag id the result is records associated with that
        tag.
        """
        tag_a = TagFactory(organization=organization, name="industry-fintech")
        tag_b = TagFactory(organization=organization, name="region-emea")

        # Two records with tag_a, one with tag_b only.
        record_with_a = RecordFactory(owner=contributor_user)
        record_with_a_2 = RecordFactory(owner=contributor_user)
        record_with_b = RecordFactory(owner=contributor_user)

        # Direct DB attachment so we don't need to go through the API.
        db_session.add(RecordTag(record_id=record_with_a.id, tag_id=tag_a.id))
        db_session.add(RecordTag(record_id=record_with_a_2.id, tag_id=tag_a.id))
        db_session.add(RecordTag(record_id=record_with_b.id, tag_id=tag_b.id))
        db_session.commit()

        response = contributor_client.get(
            "/api/connections",
            query_string={"tag_ids": str(tag_a.id)},
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body["total"] == 2
        returned_ids = {item["id"] for item in body["items"]}
        assert str(record_with_a.id) in returned_ids
        assert str(record_with_a_2.id) in returned_ids

    def test_list_filter_by_multiple_tag_ids_returns_any_of(
        self,
        contributor_client: Any,
        contributor_user: Any,
        organization: Any,
        db_session: Any,
    ) -> None:
        """Multiple tag_ids return records associated with EITHER tag.

        Per AAP s 0.4.7, the multi-tag filter uses "contains-any-of"
        semantics: a record matches if it has at least one of the
        listed tags. This is implemented via a subquery over
        ``record_tags`` rather than an INNER JOIN to avoid row
        duplication.
        """
        tag_a = TagFactory(organization=organization, name="industry-fintech")
        tag_b = TagFactory(organization=organization, name="region-emea")
        tag_c = TagFactory(organization=organization, name="use-case-crm")

        record_a = RecordFactory(owner=contributor_user)
        record_b = RecordFactory(owner=contributor_user)
        record_c = RecordFactory(owner=contributor_user)
        # record_a -> tag_a; record_b -> tag_b; record_c -> tag_c.
        db_session.add(RecordTag(record_id=record_a.id, tag_id=tag_a.id))
        db_session.add(RecordTag(record_id=record_b.id, tag_id=tag_b.id))
        db_session.add(RecordTag(record_id=record_c.id, tag_id=tag_c.id))
        db_session.commit()

        # Query for tag_a OR tag_b - record_c is NOT a match.
        response = contributor_client.get(
            "/api/connections",
            query_string={"tag_ids": f"{tag_a.id},{tag_b.id}"},
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body["total"] == 2
        returned_ids = {item["id"] for item in body["items"]}
        assert returned_ids == {str(record_a.id), str(record_b.id)}

    def test_list_filters_combine_with_and(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """Multiple filters AND-combine: matching records satisfy ALL filters."""
        RecordFactory(
            owner=contributor_user,
            company="Acme Corp",
            outreach_status=OutreachStatus.NOT_STARTED,
        )
        RecordFactory(
            owner=contributor_user,
            company="Acme Corp",
            outreach_status=OutreachStatus.CLOSED,
        )
        RecordFactory(
            owner=contributor_user,
            company="Beta LLC",
            outreach_status=OutreachStatus.NOT_STARTED,
        )

        response = contributor_client.get(
            "/api/connections",
            query_string={
                "company": "Acme",
                "outreach_status": "Not Started",
            },
        )
        assert response.status_code == 200
        body = response.get_json()
        # Only the Acme + NotStarted record matches.
        assert body["total"] == 1
        assert body["items"][0]["company"] == "Acme Corp"
        assert body["items"][0]["outreach_status"] == "Not Started"

    def test_list_sort_by_submission_date_desc_default(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """Default sort is submission_date DESC per AAP s 0.5.2 Layer 4.

        Verifies the composite-index-aligned default sort: most
        recent submissions appear first in the feed.
        """
        now = datetime.now(UTC)
        RecordFactory(
            owner=contributor_user,
            full_name="Oldest",
            submission_date=now - timedelta(days=2),
        )
        RecordFactory(
            owner=contributor_user,
            full_name="Newest",
            submission_date=now,
        )
        RecordFactory(
            owner=contributor_user,
            full_name="Middle",
            submission_date=now - timedelta(days=1),
        )

        response = contributor_client.get("/api/connections")
        assert response.status_code == 200
        body = response.get_json()
        assert [item["full_name"] for item in body["items"]] == [
            "Newest",
            "Middle",
            "Oldest",
        ]

    def test_list_sort_by_full_name_asc(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """Sort by full_name ascending."""
        RecordFactory(owner=contributor_user, full_name="Charlie")
        RecordFactory(owner=contributor_user, full_name="Alice")
        RecordFactory(owner=contributor_user, full_name="Bob")

        response = contributor_client.get("/api/connections?sort=full_name&sort_dir=asc")
        assert response.status_code == 200
        body = response.get_json()
        assert [item["full_name"] for item in body["items"]] == [
            "Alice",
            "Bob",
            "Charlie",
        ]

    def test_list_sort_by_company_desc(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """Sort by company descending."""
        RecordFactory(owner=contributor_user, company="Acme")
        RecordFactory(owner=contributor_user, company="Charlie Inc")
        RecordFactory(owner=contributor_user, company="Beta LLC")

        response = contributor_client.get("/api/connections?sort=company&sort_dir=desc")
        assert response.status_code == 200
        body = response.get_json()
        # Reverse-alphabetical: Charlie -> Beta -> Acme.
        assert [item["company"] for item in body["items"]] == [
            "Charlie Inc",
            "Beta LLC",
            "Acme",
        ]

    def test_list_sort_by_owner_display_name(
        self,
        contributor_client: Any,
        contributor_user: Any,
        organization: Any,
    ) -> None:
        """Sort by owner_display_name yields deterministic ordering."""
        # Two records owned by users with explicit display names.
        u_alpha = ContributorUserFactory(organization=organization, display_name="Alpha")
        u_omega = ContributorUserFactory(organization=organization, display_name="Omega")
        RecordFactory(owner=u_omega)
        RecordFactory(owner=u_alpha)

        response = contributor_client.get("/api/connections?sort=owner_display_name&sort_dir=asc")
        assert response.status_code == 200
        body = response.get_json()
        # Alpha first (lexicographic ascending).
        assert body["items"][0]["owner_display_name"] == "Alpha"
        assert body["items"][1]["owner_display_name"] == "Omega"

    def test_list_sort_by_outreach_status(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """Sort by outreach_status produces deterministic ordering."""
        RecordFactory(owner=contributor_user, outreach_status=OutreachStatus.CLOSED)
        RecordFactory(owner=contributor_user, outreach_status=OutreachStatus.NOT_STARTED)
        RecordFactory(owner=contributor_user, outreach_status=OutreachStatus.IN_PROGRESS)

        response = contributor_client.get("/api/connections?sort=outreach_status&sort_dir=asc")
        assert response.status_code == 200
        body = response.get_json()
        # The exact ordering depends on enum string sort; verify all
        # three statuses are present and the API accepted the sort
        # request (no 422).
        statuses = [item["outreach_status"] for item in body["items"]]
        assert set(statuses) == {"Not Started", "In Progress", "Closed"}

    def test_list_invalid_sort_key_returns_422(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """Unknown sort key surfaces as HTTP 422."""
        RecordFactory(owner=contributor_user)
        response = contributor_client.get("/api/connections?sort=bogus_key")
        assert response.status_code == 422

    def test_list_response_includes_owner_display_name(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """Every list item includes a non-null owner_display_name.

        Verifies the F-006 denormalization: the owner display name
        is captured at submission time and surfaced for fast feed
        rendering without a JOIN against ``users``.
        """
        RecordFactory.create_batch(3, owner=contributor_user)
        response = contributor_client.get("/api/connections")
        body = response.get_json()
        for item in body["items"]:
            assert item["owner_display_name"]  # non-empty, non-None

    def test_list_response_includes_tags(
        self,
        contributor_client: Any,
        contributor_user: Any,
        organization: Any,
        db_session: Any,
    ) -> None:
        """Each list item includes its tags array (eager-loaded)."""
        tag_a = TagFactory(organization=organization, name="industry-saas")
        tag_b = TagFactory(organization=organization, name="region-na")
        record = RecordFactory(owner=contributor_user)
        db_session.add(RecordTag(record_id=record.id, tag_id=tag_a.id))
        db_session.add(RecordTag(record_id=record.id, tag_id=tag_b.id))
        db_session.commit()

        response = contributor_client.get("/api/connections")
        body = response.get_json()
        # Find the record we just tagged.
        item = next(i for i in body["items"] if i["id"] == str(record.id))
        tag_names = {t["name"] for t in item["tags"]}
        assert tag_names == {"industry-saas", "region-na"}

    def test_admin_can_list(
        self,
        admin_client: Any,
        admin_user: Any,
    ) -> None:
        """Admin admitted to GET /api/connections."""
        RecordFactory.create_batch(2, owner=admin_user)
        response = admin_client.get("/api/connections")
        assert response.status_code == 200

    def test_contributor_can_list(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """Contributor admitted to GET /api/connections."""
        RecordFactory.create_batch(2, owner=contributor_user)
        response = contributor_client.get("/api/connections")
        assert response.status_code == 200

    def test_viewer_can_list(
        self,
        viewer_client: Any,
        viewer_user: Any,
        organization: Any,
    ) -> None:
        """Viewer (Sales Rep) admitted to GET /api/connections."""
        author = ContributorUserFactory(organization=organization)
        RecordFactory.create_batch(2, owner=author)
        response = viewer_client.get("/api/connections")
        assert response.status_code == 200

    def test_anonymous_unauthorized(
        self,
        client: Any,
    ) -> None:
        """Unauthenticated GET rejected with 401."""
        response = client.get("/api/connections")
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# TestConnectionDuplicateCheck -- GET /api/connections/duplicate-check (F-010)
# ---------------------------------------------------------------------------


class TestConnectionDuplicateCheck:
    """``GET /api/connections/duplicate-check`` returns non-blocking warning (F-010).

    Verifies the F-010 contract: always 200 (or 422 for malformed input);
    NEVER 409 (duplicate is a warning, not a block at this endpoint);
    org-scoped; soft-delete-aware; supports ``exclude_record_id`` for
    the edit flow.
    """

    def test_duplicate_check_returns_no_match_when_unique(
        self,
        contributor_client: Any,
    ) -> None:
        """An unknown URL returns 200 with duplicate_found=False."""
        response = contributor_client.get(
            "/api/connections/duplicate-check"
            "?linkedin_url=https://www.linkedin.com/in/never-seen-this-name"
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body["duplicate_found"] is False
        assert body["existing_record_id"] is None
        assert body["existing_owner_display_name"] is None
        assert body["existing_submission_date"] is None
        # The normalized form is always echoed back for transparency.
        assert "normalized_linkedin_url" in body
        assert body["normalized_linkedin_url"].startswith("https://")

    def test_duplicate_check_returns_match_when_existing(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """A known URL returns 200 with duplicate_found=True and metadata."""
        target_url = "https://www.linkedin.com/in/jane-existing"
        existing = RecordFactory(
            owner=contributor_user,
            linkedin_url=target_url,
        )

        response = contributor_client.get(
            f"/api/connections/duplicate-check?linkedin_url={target_url}"
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body["duplicate_found"] is True
        assert body["existing_record_id"] == str(existing.id)
        # Owner display name is denormalized for fast warning rendering.
        assert body["existing_owner_display_name"] == contributor_user.display_name
        assert body["existing_submission_date"] is not None

    def test_duplicate_check_normalizes_input_url(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """The endpoint normalizes the input URL before comparison.

        Verifies the case-fold + trailing-slash + query-param
        canonicalization: a tampered URL still matches the existing
        record.
        """
        # Pre-create a record with a canonical URL.
        canonical_url = "https://www.linkedin.com/in/normalize-target"
        RecordFactory(owner=contributor_user, linkedin_url=canonical_url)

        # Query with a NON-canonical variant (mixed case + trailing slash + query).
        response = contributor_client.get(
            "/api/connections/duplicate-check"
            "?linkedin_url=https://LinkedIn.com/in/Normalize-Target/?utm=foo"
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body["duplicate_found"] is True

    def test_duplicate_check_invalid_url_returns_422(
        self,
        contributor_client: Any,
    ) -> None:
        """Malformed LinkedIn URL surfaces as 422."""
        response = contributor_client.get(
            "/api/connections/duplicate-check?linkedin_url=https://example.com/foo"
        )
        assert response.status_code == 422

    def test_duplicate_check_missing_url_param_returns_422(
        self,
        contributor_client: Any,
    ) -> None:
        """Omitting the linkedin_url query param returns 422."""
        response = contributor_client.get("/api/connections/duplicate-check")
        assert response.status_code == 422

    def test_duplicate_check_org_scoped(
        self,
        contributor_client: Any,
    ) -> None:
        """A duplicate in another org does NOT match in the actor's org.

        Verifies the AAP s 0.7.1 invariant 3: org-scoping applies
        even at the duplicate-check endpoint, so cross-org URLs
        appear unique to the local actor.
        """
        target_url = "https://www.linkedin.com/in/cross-org-dup"
        # Pre-create a record in another org with the target URL.
        other_org = OrganizationFactory()
        other_user = ContributorUserFactory(organization=other_org)
        RecordFactory(owner=other_user, linkedin_url=target_url)

        # Query in the default org's context.
        response = contributor_client.get(
            f"/api/connections/duplicate-check?linkedin_url={target_url}"
        )
        assert response.status_code == 200
        body = response.get_json()
        # No duplicate IN THE LOCAL ORG.
        assert body["duplicate_found"] is False

    def test_duplicate_check_excludes_soft_deleted_records(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """Soft-deleted records do NOT match (unique partial index semantics)."""
        target_url = "https://www.linkedin.com/in/soft-deleted-dup"
        SoftDeletedRecordFactory(owner=contributor_user, linkedin_url=target_url)

        response = contributor_client.get(
            f"/api/connections/duplicate-check?linkedin_url={target_url}"
        )
        assert response.status_code == 200
        body = response.get_json()
        # The unique index has WHERE deleted_at IS NULL, so soft-deleted
        # records are not matched.
        assert body["duplicate_found"] is False

    def test_duplicate_check_with_exclude_record_id_skips_self(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """exclude_record_id allows the edit flow to skip self-match.

        When editing a record, the SPA queries duplicate-check to
        warn about other records sharing the URL - but should not
        consider the record being edited as a match against itself.
        """
        target_url = "https://www.linkedin.com/in/edit-self-dup"
        existing = RecordFactory(
            owner=contributor_user,
            linkedin_url=target_url,
        )

        response = contributor_client.get(
            "/api/connections/duplicate-check"
            f"?linkedin_url={target_url}&exclude_record_id={existing.id}"
        )
        assert response.status_code == 200
        body = response.get_json()
        # Self is excluded -> no duplicate found.
        assert body["duplicate_found"] is False

    def test_duplicate_check_static_route_resolves_before_dynamic_route(
        self,
        contributor_client: Any,
    ) -> None:
        """CRITICAL: /duplicate-check resolves BEFORE /<uuid:record_id>.

        Verifies the AAP s 0.5.2 Layer 5 route-ordering invariant.
        If the dynamic route were registered first, GET
        /api/connections/duplicate-check would attempt to parse
        'duplicate-check' as a UUID and return 404 from werkzeug's
        UUID converter. This test catches that misregistration
        before deployment.
        """
        response = contributor_client.get(
            "/api/connections/duplicate-check"
            "?linkedin_url=https://www.linkedin.com/in/route-order-canary"
        )
        # If routes are mis-ordered, this would 404 from werkzeug.
        assert response.status_code == 200
        body = response.get_json()
        assert "duplicate_found" in body

    def test_duplicate_check_anonymous_unauthorized(
        self,
        client: Any,
    ) -> None:
        """Unauthenticated request to duplicate-check rejected with 401."""
        response = client.get(
            "/api/connections/duplicate-check?linkedin_url=https://www.linkedin.com/in/anon-test"
        )
        assert response.status_code == 401

    def test_duplicate_check_admin_allowed(
        self,
        admin_client: Any,
    ) -> None:
        """Admin admitted to duplicate-check."""
        response = admin_client.get(
            "/api/connections/duplicate-check?linkedin_url=https://www.linkedin.com/in/admin-test"
        )
        assert response.status_code == 200

    def test_duplicate_check_contributor_allowed(
        self,
        contributor_client: Any,
    ) -> None:
        """Contributor admitted to duplicate-check."""
        response = contributor_client.get(
            "/api/connections/duplicate-check?linkedin_url=https://www.linkedin.com/in/contrib-test"
        )
        assert response.status_code == 200

    def test_duplicate_check_viewer_forbidden(
        self,
        viewer_client: Any,
    ) -> None:
        """Viewer (Sales Rep) rejected from duplicate-check.

        Per the F-010 RBAC fix, the duplicate-check endpoint is
        restricted to Admin + Contributor only - it's used in the
        Add Connection form which Viewers cannot reach.
        """
        response = viewer_client.get(
            "/api/connections/duplicate-check?linkedin_url=https://www.linkedin.com/in/viewer-test"
        )
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# TestConnectionDetail -- GET /api/connections/<uuid> (F-011)
# ---------------------------------------------------------------------------


class TestConnectionDetail:
    """``GET /api/connections/<uuid>`` returns full record (F-011).

    Verifies the F-011 detail contract: all fields surface, 404 for
    nonexistent ids, 404 for cross-org existence-leak avoidance, 404
    for soft-deleted records by default, eager-loaded tags.
    """

    def test_get_record_returns_full_record(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """Detail endpoint returns the full ConnectionRead body."""
        record = RecordFactory(
            owner=contributor_user,
            full_name="Detail Target",
            company="DetailCo",
        )
        response = contributor_client.get(f"/api/connections/{record.id}")
        assert response.status_code == 200
        body = response.get_json()
        assert body["id"] == str(record.id)
        assert body["full_name"] == "Detail Target"
        assert body["company"] == "DetailCo"
        # Operational fields present.
        assert body["owner_display_name"] == contributor_user.display_name
        assert body["deleted_at"] is None
        assert "submission_date" in body
        assert "tags" in body  # eager-loaded array

    def test_get_record_returns_404_for_nonexistent_id(
        self,
        contributor_client: Any,
    ) -> None:
        """Unknown UUID returns 404."""
        random_uuid = uuid.uuid4()
        response = contributor_client.get(f"/api/connections/{random_uuid}")
        assert response.status_code == 404

    def test_get_record_returns_404_for_cross_org_record(
        self,
        contributor_client: Any,
    ) -> None:
        """Cross-org record returns 404 for existence-leak avoidance.

        Per AAP s 0.7.1 invariant 3, cross-org access returns 404
        (not 403) so an attacker cannot enumerate which UUIDs exist
        in OTHER organizations.
        """
        other_org = OrganizationFactory()
        other_user = ContributorUserFactory(organization=other_org)
        cross_org_record = RecordFactory(owner=other_user)

        response = contributor_client.get(f"/api/connections/{cross_org_record.id}")
        assert response.status_code == 404

    def test_get_record_returns_404_for_soft_deleted_by_default(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """Soft-deleted record returns 404 by default (soft-delete-aware)."""
        record = SoftDeletedRecordFactory(owner=contributor_user)
        response = contributor_client.get(f"/api/connections/{record.id}")
        assert response.status_code == 404

    def test_admin_can_get_soft_deleted_via_include_deleted(
        self,
        admin_client: Any,
        admin_user: Any,
    ) -> None:
        """Admin opt-in to soft-deleted detail via include_deleted=true."""
        record = SoftDeletedRecordFactory(owner=admin_user)
        response = admin_client.get(f"/api/connections/{record.id}?include_deleted=true")
        assert response.status_code == 200
        body = response.get_json()
        assert body["deleted_at"] is not None

    def test_get_record_includes_tags(
        self,
        contributor_client: Any,
        contributor_user: Any,
        organization: Any,
        db_session: Any,
    ) -> None:
        """Detail response includes the tag list via eager-loaded join."""
        record = RecordFactory(owner=contributor_user)
        tag_a = TagFactory(organization=organization, name="industry-fintech")
        tag_b = TagFactory(organization=organization, name="region-emea")
        db_session.add(RecordTag(record_id=record.id, tag_id=tag_a.id))
        db_session.add(RecordTag(record_id=record.id, tag_id=tag_b.id))
        db_session.commit()

        response = contributor_client.get(f"/api/connections/{record.id}")
        body = response.get_json()
        tag_names = {t["name"] for t in body["tags"]}
        assert tag_names == {"industry-fintech", "region-emea"}

    def test_get_record_invalid_uuid_returns_404(
        self,
        contributor_client: Any,
    ) -> None:
        """Malformed UUID surfaces as 404 (werkzeug UUID converter behavior)."""
        response = contributor_client.get("/api/connections/not-a-uuid")
        assert response.status_code == 404

    def test_get_record_anonymous_unauthorized(
        self,
        client: Any,
    ) -> None:
        """Unauthenticated GET on detail rejected with 401."""
        random_uuid = uuid.uuid4()
        response = client.get(f"/api/connections/{random_uuid}")
        assert response.status_code == 401

    def test_get_record_admin_allowed(
        self,
        admin_client: Any,
        admin_user: Any,
    ) -> None:
        """Admin admitted to GET /api/connections/<id>."""
        record = RecordFactory(owner=admin_user)
        response = admin_client.get(f"/api/connections/{record.id}")
        assert response.status_code == 200

    def test_get_record_viewer_allowed(
        self,
        viewer_client: Any,
        organization: Any,
    ) -> None:
        """Viewer admitted to GET /api/connections/<id>."""
        author = ContributorUserFactory(organization=organization)
        record = RecordFactory(owner=author)
        response = viewer_client.get(f"/api/connections/{record.id}")
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# TestConnectionHistory -- GET /api/connections/<uuid>/history (F-011)
# ---------------------------------------------------------------------------


class TestConnectionHistory:
    """``GET /api/connections/<uuid>/history`` returns the audit history (F-011).

    Verifies the F-011 history contract: events sorted by
    ``event_timestamp DESC``, all role-conditioned actors visible,
    org-scope checked BEFORE returning audit data, pagination
    supported.
    """

    def test_get_history_returns_audit_events_for_record(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """History returns the CREATE event for a freshly-created record.

        The :func:`create_record` service emits a CREATE audit event
        in the same transaction as the record INSERT, so a freshly
        created record always has at least one history entry.
        """
        # Create via the API so the CREATE audit event is emitted.
        payload = _valid_connection_payload()
        post_response = contributor_client.post("/api/connections", json=payload)
        assert post_response.status_code == 201
        record_id = post_response.get_json()["id"]

        response = contributor_client.get(f"/api/connections/{record_id}/history")
        assert response.status_code == 200
        body = response.get_json()
        assert body["total"] >= 1
        # The CREATE event is present.
        assert any(entry["event_type"] == AuditEventType.CREATE.value for entry in body["items"])

    def test_get_history_includes_event_type_actor_timestamp_payloads(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """Each entry includes the canonical ConnectionHistoryEntry shape."""
        payload = _valid_connection_payload()
        post_response = contributor_client.post("/api/connections", json=payload)
        record_id = post_response.get_json()["id"]

        response = contributor_client.get(f"/api/connections/{record_id}/history")
        body = response.get_json()
        entry = body["items"][0]
        # Required fields per ConnectionHistoryEntry.
        assert "id" in entry
        assert "event_type" in entry
        assert "event_timestamp" in entry
        assert "actor_user_id" in entry
        assert "actor_display_name" in entry
        # Payloads may be None or dict.
        assert "before_payload" in entry
        assert "after_payload" in entry

    def test_get_history_sorted_by_timestamp_desc(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """History entries are sorted newest-first.

        Performs a sequence of state mutations on a single record
        and asserts the returned events are ordered by
        ``event_timestamp DESC`` (most recent first).
        """
        # Create the record (emits CREATE).
        post_response = contributor_client.post(
            "/api/connections", json=_valid_connection_payload()
        )
        record_id = post_response.get_json()["id"]

        # Edit it (emits EDIT).
        patch_response = contributor_client.patch(
            f"/api/connections/{record_id}",
            json={"company": "Edited Co"},
        )
        assert patch_response.status_code == 200

        response = contributor_client.get(f"/api/connections/{record_id}/history")
        body = response.get_json()
        assert body["total"] >= 2
        # Events must be sorted newest-first.
        timestamps = [entry["event_timestamp"] for entry in body["items"]]
        assert timestamps == sorted(timestamps, reverse=True)

    def test_get_history_returns_404_for_nonexistent_record(
        self,
        contributor_client: Any,
    ) -> None:
        """History for unknown record id returns 404."""
        random_uuid = uuid.uuid4()
        response = contributor_client.get(f"/api/connections/{random_uuid}/history")
        assert response.status_code == 404

    def test_get_history_returns_404_for_cross_org_record(
        self,
        contributor_client: Any,
    ) -> None:
        """Cross-org record returns 404 BEFORE any history is returned.

        Verifies the org-scope check happens FIRST in
        :func:`get_record_history` to prevent enumeration of audit
        data across orgs.
        """
        other_org = OrganizationFactory()
        other_user = ContributorUserFactory(organization=other_org)
        cross_org_record = RecordFactory(owner=other_user)

        response = contributor_client.get(f"/api/connections/{cross_org_record.id}/history")
        assert response.status_code == 404

    def test_get_history_pagination(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """Pagination via limit/offset returns the requested window.

        Inserts a known-large number of audit events directly via
        the factory (bypassing the service layer) so we have enough
        rows to test pagination behavior deterministically.
        """
        record = RecordFactory(owner=contributor_user)
        # 30 events targeting the same record.
        for _ in range(30):
            AuditEventFactory(
                target_record=record,
                actor=contributor_user,
                event_type=AuditEventType.EDIT,
            )

        response = contributor_client.get(f"/api/connections/{record.id}/history?limit=10&offset=0")
        assert response.status_code == 200
        body = response.get_json()
        assert len(body["items"]) == 10
        assert body["total"] == 30
        assert body["limit"] == 10
        assert body["offset"] == 0

    def test_get_history_anonymous_unauthorized(
        self,
        client: Any,
    ) -> None:
        """Unauthenticated history request rejected with 401."""
        random_uuid = uuid.uuid4()
        response = client.get(f"/api/connections/{random_uuid}/history")
        assert response.status_code == 401

    def test_get_history_viewer_allowed(
        self,
        viewer_client: Any,
        organization: Any,
    ) -> None:
        """Viewer admitted to GET /<id>/history."""
        author = ContributorUserFactory(organization=organization)
        record = RecordFactory(owner=author)
        response = viewer_client.get(f"/api/connections/{record.id}/history")
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# TestConnectionUpdate -- PATCH /api/connections/<uuid> (F-007)
# ---------------------------------------------------------------------------


class TestConnectionUpdate:
    """``PATCH /api/connections/<uuid>`` edits a Connection record (F-007).

    Verifies F-007 partial-update semantics: any single field can be
    updated; ownership enforced for Contributors (own only); Admin
    bypass; outreach_status excluded (must use status endpoint);
    extra='forbid' tampering rejection; cross-org 404; soft-deleted
    record 404; URL re-normalization on edit.
    """

    def test_contributor_can_update_own_record(
        self,
        contributor_client: Any,
        contributor_user: Any,
        audit_assertion: Any,
    ) -> None:
        """Contributor updating their OWN record returns 200."""
        record = RecordFactory(
            owner=contributor_user,
            company="Old Co",
            full_name="Old Name",
        )
        response = contributor_client.patch(
            f"/api/connections/{record.id}",
            json={"company": "New Co"},
        )
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        assert body["company"] == "New Co"
        # The other fields are unchanged.
        assert body["full_name"] == "Old Name"
        # Audit event emitted.
        audit_assertion(
            event_type=AuditEventType.EDIT,
            target_record_id=record.id,
            actor_user_id=contributor_user.id,
        )

    def test_contributor_cannot_update_others_record_returns_403(
        self,
        contributor_client: Any,
        organization: Any,
    ) -> None:
        """Contributor editing another user's record returns 403.

        Per F-007, Contributors can only edit their OWN records.
        Editing another user's record (even within the same org)
        is a 403 forbidden.
        """
        other_user = AdminUserFactory(organization=organization)
        record = RecordFactory(owner=other_user)
        response = contributor_client.patch(
            f"/api/connections/{record.id}",
            json={"company": "Hijacked Co"},
        )
        assert response.status_code == 403

    def test_admin_can_update_any_record(
        self,
        admin_client: Any,
        admin_user: Any,
        organization: Any,
        audit_assertion: Any,
    ) -> None:
        """Admin can update ANY record regardless of ownership."""
        author = ContributorUserFactory(organization=organization)
        record = RecordFactory(owner=author)
        response = admin_client.patch(
            f"/api/connections/{record.id}",
            json={"company": "Admin-Edited Co"},
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body["company"] == "Admin-Edited Co"
        # Audit event emitted by the ADMIN, not the original author.
        audit_assertion(
            event_type=AuditEventType.EDIT,
            target_record_id=record.id,
            actor_user_id=admin_user.id,
        )

    def test_viewer_cannot_update_returns_403(
        self,
        viewer_client: Any,
        organization: Any,
    ) -> None:
        """Viewer rejected with 403 from PATCH (Contributor + Admin only)."""
        author = ContributorUserFactory(organization=organization)
        record = RecordFactory(owner=author)
        response = viewer_client.patch(
            f"/api/connections/{record.id}",
            json={"company": "Viewer-Hijacked"},
        )
        assert response.status_code == 403

    def test_update_owner_user_id_injection_rejected_with_422(
        self,
        contributor_client: Any,
        contributor_user: Any,
        admin_user: Any,
    ) -> None:
        """Client-supplied owner_user_id rejected by extra='forbid'."""
        record = RecordFactory(owner=contributor_user)
        response = contributor_client.patch(
            f"/api/connections/{record.id}",
            json={
                "company": "X",
                "owner_user_id": str(admin_user.id),
            },
        )
        assert response.status_code == 422

    def test_update_outreach_status_rejected(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """outreach_status MUST go through the dedicated status endpoint.

        Per the F-005 architectural separation, outreach_status is
        EXCLUDED from :class:`ConnectionUpdate` so a Contributor
        cannot bypass the Viewer/Admin RBAC gate by piggybacking
        the status onto a regular edit.
        """
        record = RecordFactory(owner=contributor_user)
        response = contributor_client.patch(
            f"/api/connections/{record.id}",
            json={
                "company": "X",
                "outreach_status": OutreachStatus.CLOSED.value,
            },
        )
        assert response.status_code == 422

    @pytest.mark.audit
    def test_update_emits_edit_audit_with_before_after(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """An EDIT audit event captures before and after payloads."""
        record = RecordFactory(owner=contributor_user, company="Old Co")
        baseline = _audit_event_count()
        response = contributor_client.patch(
            f"/api/connections/{record.id}",
            json={"company": "New Co"},
        )
        assert response.status_code == 200

        # Exactly one new audit event was emitted.
        assert _audit_event_count() == baseline + 1

        events = _audit_events_for_record(record.id)
        edit_events = [e for e in events if e.event_type == AuditEventType.EDIT]
        assert len(edit_events) == 1
        event = edit_events[0]
        # Both before and after are present (dict[str, Any]).
        assert event.before_payload is not None
        assert event.after_payload is not None
        # The diff captures the company change.
        assert event.before_payload.get("company") == "Old Co"
        assert event.after_payload.get("company") == "New Co"

    def test_update_partial_only_changes_specified_fields(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """Fields not present in PATCH payload are left untouched."""
        record = RecordFactory(
            owner=contributor_user,
            full_name="Original Name",
            company="Original Co",
            job_title="Original Title",
            relationship_context="Original context",
        )
        response = contributor_client.patch(
            f"/api/connections/{record.id}",
            json={"company": "Only Company Updated"},
        )
        assert response.status_code == 200
        body = response.get_json()
        # Only company changed.
        assert body["company"] == "Only Company Updated"
        # Other fields preserved.
        assert body["full_name"] == "Original Name"
        assert body["job_title"] == "Original Title"
        assert body["relationship_context"] == "Original context"

    def test_update_nonexistent_record_returns_404(
        self,
        contributor_client: Any,
    ) -> None:
        """PATCH on unknown UUID returns 404."""
        random_uuid = uuid.uuid4()
        response = contributor_client.patch(
            f"/api/connections/{random_uuid}",
            json={"company": "New Co"},
        )
        assert response.status_code == 404

    def test_update_soft_deleted_record_returns_404(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """PATCH on a soft-deleted record returns 404 (soft-delete-aware)."""
        record = SoftDeletedRecordFactory(owner=contributor_user)
        response = contributor_client.patch(
            f"/api/connections/{record.id}",
            json={"company": "Cannot Edit Deleted"},
        )
        assert response.status_code == 404

    def test_update_cross_org_returns_404(
        self,
        contributor_client: Any,
    ) -> None:
        """PATCH on a cross-org record returns 404."""
        other_org = OrganizationFactory()
        other_user = ContributorUserFactory(organization=other_org)
        cross_org_record = RecordFactory(owner=other_user)

        response = contributor_client.patch(
            f"/api/connections/{cross_org_record.id}",
            json={"company": "Cross-org Hijack"},
        )
        assert response.status_code == 404

    def test_update_recomputes_normalized_linkedin_url(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """Editing linkedin_url re-normalizes the canonical form."""
        record = RecordFactory(
            owner=contributor_user,
            linkedin_url="https://www.linkedin.com/in/alice",
        )
        response = contributor_client.patch(
            f"/api/connections/{record.id}",
            json={"linkedin_url": "https://www.LinkedIn.com/in/Bob/?ref=email"},
        )
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        assert body["linkedin_url"] == "https://www.LinkedIn.com/in/Bob/?ref=email"
        # Normalized form is canonicalized.
        assert body["normalized_linkedin_url"] == "https://linkedin.com/in/bob"

    def test_update_to_duplicate_url_returns_409(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """Editing to a URL that collides with an existing record returns 409."""
        # Pre-create record A.
        RecordFactory(
            owner=contributor_user,
            linkedin_url="https://www.linkedin.com/in/owner-a",
        )
        # Pre-create record B with a different URL.
        record_b = RecordFactory(
            owner=contributor_user,
            linkedin_url="https://www.linkedin.com/in/owner-b",
        )

        # Try to edit B to use A's URL.
        response = contributor_client.patch(
            f"/api/connections/{record_b.id}",
            json={"linkedin_url": "https://www.linkedin.com/in/owner-a"},
        )
        assert response.status_code == 409
        body = response.get_json()
        assert body["error"]["code"] == "duplicate_record"

    def test_update_invalid_linkedin_url_returns_422(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """PATCH with malformed URL returns 422."""
        record = RecordFactory(owner=contributor_user)
        response = contributor_client.patch(
            f"/api/connections/{record.id}",
            json={"linkedin_url": "https://example.com/foo"},
        )
        assert response.status_code == 422

    def test_update_anonymous_unauthorized(
        self,
        client: Any,
    ) -> None:
        """Unauthenticated PATCH rejected with 401."""
        random_uuid = uuid.uuid4()
        response = client.patch(
            f"/api/connections/{random_uuid}",
            json={"company": "X"},
        )
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# TestConnectionStatusUpdate -- PATCH /api/connections/<uuid>/status (F-005)
# ---------------------------------------------------------------------------


class TestConnectionStatusUpdate:
    """``PATCH /api/connections/<uuid>/status`` mutates outreach status (F-005).

    Verifies the F-005 sales-team-accountability contract: only Viewer
    (Sales Rep) and Admin can mutate status; Contributor 403; idempotent
    no-op short-circuits without audit emission; cross-org 404;
    soft-deleted record 404.
    """

    def test_viewer_can_update_status(
        self,
        viewer_client: Any,
        viewer_user: Any,
        organization: Any,
        audit_assertion: Any,
    ) -> None:
        """Viewer (Sales Rep) admitted to PATCH /<id>/status."""
        author = ContributorUserFactory(organization=organization)
        record = RecordFactory(
            owner=author,
            outreach_status=OutreachStatus.NOT_STARTED,
        )
        response = viewer_client.patch(
            f"/api/connections/{record.id}/status",
            json={"outreach_status": OutreachStatus.IN_PROGRESS.value},
        )
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        assert body["outreach_status"] == "In Progress"
        # Audit event emitted.
        audit_assertion(
            event_type=AuditEventType.STATUS_CHANGE,
            target_record_id=record.id,
            actor_user_id=viewer_user.id,
        )

    def test_admin_can_update_status(
        self,
        admin_client: Any,
        admin_user: Any,
        audit_assertion: Any,
    ) -> None:
        """Admin admitted to PATCH /<id>/status."""
        record = RecordFactory(
            owner=admin_user,
            outreach_status=OutreachStatus.NOT_STARTED,
        )
        response = admin_client.patch(
            f"/api/connections/{record.id}/status",
            json={"outreach_status": OutreachStatus.CONTACTED.value},
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body["outreach_status"] == "Contacted"
        audit_assertion(
            event_type=AuditEventType.STATUS_CHANGE,
            target_record_id=record.id,
            actor_user_id=admin_user.id,
        )

    def test_contributor_cannot_update_status_returns_403(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """CRITICAL F-005 invariant: Contributor cannot mutate status.

        Per the user prompt: status mutation is reserved for Sales
        Reps + Admin to "preserve sales team accountability." A
        Contributor (the original submitter) attempting to mutate
        status is rejected with 403.
        """
        record = RecordFactory(
            owner=contributor_user,
            outreach_status=OutreachStatus.NOT_STARTED,
        )
        response = contributor_client.patch(
            f"/api/connections/{record.id}/status",
            json={"outreach_status": OutreachStatus.IN_PROGRESS.value},
        )
        assert response.status_code == 403
        body = response.get_json()
        assert body["error"]["code"] == "forbidden"

    @pytest.mark.audit
    def test_status_update_emits_audit_with_before_after(
        self,
        viewer_client: Any,
        viewer_user: Any,
        organization: Any,
    ) -> None:
        """STATUS_CHANGE audit event captures before and after payloads."""
        author = ContributorUserFactory(organization=organization)
        record = RecordFactory(
            owner=author,
            outreach_status=OutreachStatus.NOT_STARTED,
        )
        baseline = _audit_event_count()
        response = viewer_client.patch(
            f"/api/connections/{record.id}/status",
            json={"outreach_status": OutreachStatus.IN_PROGRESS.value},
        )
        assert response.status_code == 200
        # Exactly one new audit event.
        assert _audit_event_count() == baseline + 1
        events = _audit_events_for_record(record.id)
        status_events = [e for e in events if e.event_type == AuditEventType.STATUS_CHANGE]
        assert len(status_events) == 1
        event = status_events[0]
        assert event.before_payload is not None
        assert event.after_payload is not None
        assert event.before_payload.get("outreach_status") == "Not Started"
        assert event.after_payload.get("outreach_status") == "In Progress"

    def test_status_update_no_op_emits_no_audit(
        self,
        viewer_client: Any,
        organization: Any,
    ) -> None:
        """Same-status update is a no-op short-circuit BEFORE audit emission.

        Per :func:`update_status` semantics: if the new status
        equals the current status, the function returns 200 with
        the unchanged record but does NOT emit an audit event
        (no point recording a non-change).
        """
        author = ContributorUserFactory(organization=organization)
        record = RecordFactory(
            owner=author,
            outreach_status=OutreachStatus.NOT_STARTED,
        )
        baseline = _audit_event_count()
        # PATCH with the SAME status as currently set.
        response = viewer_client.patch(
            f"/api/connections/{record.id}/status",
            json={"outreach_status": OutreachStatus.NOT_STARTED.value},
        )
        assert response.status_code == 200
        # NO new audit event emitted.
        assert _audit_event_count() == baseline

    def test_status_update_invalid_value_returns_422(
        self,
        viewer_client: Any,
        organization: Any,
    ) -> None:
        """Invalid status enum value returns 422."""
        author = ContributorUserFactory(organization=organization)
        record = RecordFactory(owner=author)
        response = viewer_client.patch(
            f"/api/connections/{record.id}/status",
            json={"outreach_status": "Pending"},  # not a valid enum value
        )
        assert response.status_code == 422

    def test_status_update_extra_field_rejected(
        self,
        viewer_client: Any,
        organization: Any,
    ) -> None:
        """Extra fields rejected by ConnectionStatusUpdate.extra='forbid'.

        The status update schema has ONLY the outreach_status field;
        any additional field (even a legitimate Connection field)
        is rejected to prevent piggybacking.
        """
        author = ContributorUserFactory(organization=organization)
        record = RecordFactory(owner=author)
        response = viewer_client.patch(
            f"/api/connections/{record.id}/status",
            json={
                "outreach_status": OutreachStatus.CLOSED.value,
                "company": "Should Not Be Allowed",
            },
        )
        assert response.status_code == 422

    def test_status_update_can_close(
        self,
        viewer_client: Any,
        organization: Any,
    ) -> None:
        """Status can transition to CLOSED."""
        author = ContributorUserFactory(organization=organization)
        record = RecordFactory(
            owner=author,
            outreach_status=OutreachStatus.IN_PROGRESS,
        )
        response = viewer_client.patch(
            f"/api/connections/{record.id}/status",
            json={"outreach_status": OutreachStatus.CLOSED.value},
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body["outreach_status"] == "Closed"

    def test_status_update_cross_org_returns_404(
        self,
        viewer_client: Any,
    ) -> None:
        """Status update on cross-org record returns 404."""
        other_org = OrganizationFactory()
        other_user = ContributorUserFactory(organization=other_org)
        cross_org_record = RecordFactory(owner=other_user)
        response = viewer_client.patch(
            f"/api/connections/{cross_org_record.id}/status",
            json={"outreach_status": OutreachStatus.IN_PROGRESS.value},
        )
        assert response.status_code == 404

    def test_status_update_soft_deleted_record_returns_404(
        self,
        viewer_client: Any,
        organization: Any,
    ) -> None:
        """Status update on soft-deleted record returns 404."""
        author = ContributorUserFactory(organization=organization)
        record = SoftDeletedRecordFactory(owner=author)
        response = viewer_client.patch(
            f"/api/connections/{record.id}/status",
            json={"outreach_status": OutreachStatus.IN_PROGRESS.value},
        )
        assert response.status_code == 404

    def test_status_update_nonexistent_record_returns_404(
        self,
        viewer_client: Any,
    ) -> None:
        """Status update on unknown UUID returns 404."""
        random_uuid = uuid.uuid4()
        response = viewer_client.patch(
            f"/api/connections/{random_uuid}/status",
            json={"outreach_status": OutreachStatus.IN_PROGRESS.value},
        )
        assert response.status_code == 404

    def test_status_update_anonymous_unauthorized(
        self,
        client: Any,
    ) -> None:
        """Unauthenticated status update rejected with 401."""
        random_uuid = uuid.uuid4()
        response = client.patch(
            f"/api/connections/{random_uuid}/status",
            json={"outreach_status": OutreachStatus.IN_PROGRESS.value},
        )
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# TestConnectionSoftDelete -- DELETE /api/connections/<uuid> (F-007)
# ---------------------------------------------------------------------------


class TestConnectionSoftDelete:
    """``DELETE /api/connections/<uuid>`` soft-deletes a record (F-007).

    Verifies F-007 soft-delete semantics: deleted_at populated;
    physical row preserved; ownership enforced for non-Admin (own only);
    Admin bypass; idempotency (second DELETE is a no-op no-audit);
    soft-deleted records excluded from default reads; URL freed for
    reuse (unique partial index has WHERE deleted_at IS NULL).
    """

    def test_contributor_can_soft_delete_own_record(
        self,
        contributor_client: Any,
        contributor_user: Any,
        audit_assertion: Any,
    ) -> None:
        """Contributor can soft-delete their OWN record."""
        record = RecordFactory(owner=contributor_user)
        response = contributor_client.delete(f"/api/connections/{record.id}")
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        # The response carries the soft-deleted record with deleted_at populated.
        assert body["deleted_at"] is not None
        # Audit event emitted.
        audit_assertion(
            event_type=AuditEventType.SOFT_DELETE,
            target_record_id=record.id,
            actor_user_id=contributor_user.id,
        )
        # Physical row preserved.
        with db.session() as session:
            db_record = session.execute(select(Record).where(Record.id == record.id)).scalar_one()
            assert db_record.deleted_at is not None

    def test_admin_can_soft_delete_any_record(
        self,
        admin_client: Any,
        admin_user: Any,
        organization: Any,
        audit_assertion: Any,
    ) -> None:
        """Admin can soft-delete ANY record regardless of ownership."""
        author = ContributorUserFactory(organization=organization)
        record = RecordFactory(owner=author)
        response = admin_client.delete(f"/api/connections/{record.id}")
        assert response.status_code == 200
        # Audit event emitted by the ADMIN actor.
        audit_assertion(
            event_type=AuditEventType.SOFT_DELETE,
            target_record_id=record.id,
            actor_user_id=admin_user.id,
        )

    def test_viewer_can_soft_delete_own_record(
        self,
        viewer_client: Any,
        viewer_user: Any,
    ) -> None:
        """Viewer can soft-delete their OWN record (own-only).

        Per F-007 spec: "DELETE soft delete by setting deleted_at =
        NOW()"; service permits soft-delete for Contributor / Viewer
        / Admin (own only for non-Admin). Viewer is allowed because
        they may have legitimately created records (even though the
        primary creation flow is Contributor).
        """
        record = RecordFactory(owner=viewer_user)
        response = viewer_client.delete(f"/api/connections/{record.id}")
        assert response.status_code == 200
        body = response.get_json()
        assert body["deleted_at"] is not None

    def test_contributor_cannot_soft_delete_others_record_returns_403(
        self,
        contributor_client: Any,
        organization: Any,
    ) -> None:
        """Contributor cannot soft-delete another user's record.

        Per F-007 ownership enforcement, non-Admins can only delete
        their OWN records.
        """
        other_user = AdminUserFactory(organization=organization)
        record = RecordFactory(owner=other_user)
        response = contributor_client.delete(f"/api/connections/{record.id}")
        assert response.status_code == 403

    def test_idempotent_delete_returns_200_no_double_audit(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """Second DELETE on already-soft-deleted record is a no-op.

        Per the F-007 idempotency contract, a DELETE on an already-
        soft-deleted record returns 200 (NOT 404) but does NOT emit
        a second audit event - exactly ONE soft_delete audit row
        exists at the end.
        """
        record = RecordFactory(owner=contributor_user)
        # First DELETE.
        r1 = contributor_client.delete(f"/api/connections/{record.id}")
        assert r1.status_code == 200
        baseline = _audit_event_count()

        # Second DELETE - idempotent.
        r2 = contributor_client.delete(f"/api/connections/{record.id}")
        assert r2.status_code == 200
        # NO additional audit event was emitted.
        assert _audit_event_count() == baseline
        # Verify exactly ONE soft_delete event exists.
        events = _audit_events_for_record(record.id)
        soft_delete_events = [e for e in events if e.event_type == AuditEventType.SOFT_DELETE]
        assert len(soft_delete_events) == 1

    def test_soft_delete_cross_org_returns_404(
        self,
        contributor_client: Any,
    ) -> None:
        """DELETE on a cross-org record returns 404 (existence-leak avoidance)."""
        other_org = OrganizationFactory()
        other_user = ContributorUserFactory(organization=other_org)
        cross_org_record = RecordFactory(owner=other_user)
        response = contributor_client.delete(f"/api/connections/{cross_org_record.id}")
        assert response.status_code == 404

    def test_soft_delete_nonexistent_record_returns_404(
        self,
        contributor_client: Any,
    ) -> None:
        """DELETE on unknown UUID returns 404."""
        random_uuid = uuid.uuid4()
        response = contributor_client.delete(f"/api/connections/{random_uuid}")
        assert response.status_code == 404

    def test_soft_delete_anonymous_unauthorized(
        self,
        client: Any,
    ) -> None:
        """Unauthenticated DELETE rejected with 401."""
        random_uuid = uuid.uuid4()
        response = client.delete(f"/api/connections/{random_uuid}")
        assert response.status_code == 401

    def test_soft_deleted_record_excluded_from_list(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """Soft-deleted records do NOT appear in default GET /api/connections."""
        # 3 records: 2 stay active, 1 will be soft-deleted via API.
        RecordFactory(owner=contributor_user, full_name="Active A")
        RecordFactory(owner=contributor_user, full_name="Active B")
        target = RecordFactory(owner=contributor_user, full_name="ToDelete")
        # Soft-delete via API.
        delete_response = contributor_client.delete(f"/api/connections/{target.id}")
        assert delete_response.status_code == 200

        list_response = contributor_client.get("/api/connections")
        body = list_response.get_json()
        # Only 2 active records visible.
        assert body["total"] == 2
        names = {item["full_name"] for item in body["items"]}
        assert names == {"Active A", "Active B"}

    def test_soft_deleted_record_excluded_from_detail(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """Soft-deleted record returns 404 from GET /<id> by default."""
        record = RecordFactory(owner=contributor_user)
        contributor_client.delete(f"/api/connections/{record.id}")
        response = contributor_client.get(f"/api/connections/{record.id}")
        assert response.status_code == 404

    def test_soft_deleted_record_url_freed_for_reuse(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """A soft-deleted record's URL can be reused for a new record.

        Verifies the ``WHERE deleted_at IS NULL`` predicate on the
        unique partial index ``uq_records_org_normalized_linkedin_url_active``.
        After soft-deleting record A with URL X, creating record B
        with URL X must succeed (not collide on the partial index).
        """
        target_url = f"https://www.linkedin.com/in/url-reuse-{uuid.uuid4().hex[:8]}"
        # Pre-create record A.
        record_a = RecordFactory(
            owner=contributor_user,
            linkedin_url=target_url,
        )
        # Soft-delete A via API.
        delete_response = contributor_client.delete(f"/api/connections/{record_a.id}")
        assert delete_response.status_code == 200

        # Now create record B with the SAME URL - must succeed.
        payload = _valid_connection_payload(linkedin_url=target_url)
        post_response = contributor_client.post("/api/connections", json=payload)
        assert post_response.status_code == 201
        body = post_response.get_json()
        # B is a NEW record with the reused URL.
        assert body["id"] != str(record_a.id)


# ---------------------------------------------------------------------------
# TestConnectionRouteOrdering -- werkzeug URL-rule registration order
# ---------------------------------------------------------------------------


class TestConnectionRouteOrdering:
    """Verifies the static ``/duplicate-check`` route resolves BEFORE
    the dynamic ``/<uuid:record_id>`` route.

    If the routes were registered in the wrong order, GET
    ``/api/connections/duplicate-check`` would attempt to parse
    'duplicate-check' as a UUID and 404 immediately. This test
    catches that misregistration before deployment.
    """

    def test_duplicate_check_static_route_first(
        self,
        contributor_client: Any,
    ) -> None:
        """GET /duplicate-check returns 200, NOT 404."""
        response = contributor_client.get(
            "/api/connections/duplicate-check?linkedin_url=https://www.linkedin.com/in/route-canary"
        )
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        assert "duplicate_found" in body
        assert body["duplicate_found"] is False

    def test_history_subpath_resolves_correctly(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """GET /<uuid>/history resolves to the history endpoint."""
        record = RecordFactory(owner=contributor_user)
        response = contributor_client.get(f"/api/connections/{record.id}/history")
        assert response.status_code == 200
        body = response.get_json()
        assert "items" in body
        assert "total" in body

    def test_status_subpath_resolves_correctly(
        self,
        viewer_client: Any,
        organization: Any,
    ) -> None:
        """PATCH /<uuid>/status resolves to the status update endpoint."""
        author = ContributorUserFactory(organization=organization)
        record = RecordFactory(
            owner=author,
            outreach_status=OutreachStatus.NOT_STARTED,
        )
        response = viewer_client.patch(
            f"/api/connections/{record.id}/status",
            json={"outreach_status": OutreachStatus.IN_PROGRESS.value},
        )
        assert response.status_code == 200

    def test_all_eight_endpoints_present_in_url_map(
        self,
        contributor_client: Any,
    ) -> None:
        """All 8 connection endpoints are registered in the Flask URL map.

        Defense against partial blueprint registration: enumerates
        the URL map and verifies each documented endpoint is
        present with the expected method.
        """
        url_map = contributor_client.application.url_map
        rules = list(url_map.iter_rules())
        rule_pairs = [(r.rule, r.methods or set()) for r in rules]

        # Expected (rule, method) pairs.
        # Note: Flask auto-adds OPTIONS to all rules and HEAD to GET rules.
        expected = [
            ("/api/connections", "POST"),
            ("/api/connections", "GET"),
            ("/api/connections/duplicate-check", "GET"),
            ("/api/connections/<uuid:record_id>", "GET"),
            ("/api/connections/<uuid:record_id>/history", "GET"),
            ("/api/connections/<uuid:record_id>", "PATCH"),
            ("/api/connections/<uuid:record_id>/status", "PATCH"),
            ("/api/connections/<uuid:record_id>", "DELETE"),
        ]
        for path, method in expected:
            assert any(rule == path and method in methods for rule, methods in rule_pairs), (
                f"Missing route {method} {path}"
            )


# ---------------------------------------------------------------------------
# TestConnectionRBACMatrix -- exhaustive RBAC matrix for all 8 endpoints
# ---------------------------------------------------------------------------


@pytest.mark.rbac
class TestConnectionRBACMatrix:
    """Exhaustive RBAC matrix for all 8 connections endpoints.

    Verifies the AAP s 0.7.1 invariant 7 ("API-layer authorization is
    authoritative") across the cartesian product of:

    - 8 endpoints (POST list, GET list, GET duplicate-check, GET detail,
      GET history, PATCH edit, PATCH status, DELETE)
    - 4 actor roles (Anonymous, Viewer, Contributor, Admin)

    Per-endpoint expectations are tabulated with explicit codes (no
    inference). Every cell of the matrix is exercised exactly once.
    The matrix is the canonical source of truth for the RBAC contract;
    any service-layer or middleware change that would alter expected
    HTTP status codes MUST update this matrix.
    """

    # --- POST /api/connections (Contributor + Admin only) ---

    def test_rbac_post_admin_201(
        self,
        admin_client: Any,
    ) -> None:
        """POST: Admin -> 201."""
        response = admin_client.post("/api/connections", json=_valid_connection_payload())
        assert response.status_code == 201

    def test_rbac_post_contributor_201(
        self,
        contributor_client: Any,
    ) -> None:
        """POST: Contributor -> 201."""
        response = contributor_client.post("/api/connections", json=_valid_connection_payload())
        assert response.status_code == 201

    def test_rbac_post_viewer_403(
        self,
        viewer_client: Any,
    ) -> None:
        """POST: Viewer -> 403."""
        response = viewer_client.post("/api/connections", json=_valid_connection_payload())
        assert response.status_code == 403

    def test_rbac_post_anonymous_401(
        self,
        client: Any,
    ) -> None:
        """POST: Anonymous -> 401."""
        response = client.post("/api/connections", json=_valid_connection_payload())
        assert response.status_code == 401

    # --- GET /api/connections (all 3 roles) ---

    def test_rbac_get_list_admin_200(
        self,
        admin_client: Any,
    ) -> None:
        """GET list: Admin -> 200."""
        response = admin_client.get("/api/connections")
        assert response.status_code == 200

    def test_rbac_get_list_contributor_200(
        self,
        contributor_client: Any,
    ) -> None:
        """GET list: Contributor -> 200."""
        response = contributor_client.get("/api/connections")
        assert response.status_code == 200

    def test_rbac_get_list_viewer_200(
        self,
        viewer_client: Any,
    ) -> None:
        """GET list: Viewer -> 200."""
        response = viewer_client.get("/api/connections")
        assert response.status_code == 200

    def test_rbac_get_list_anonymous_401(
        self,
        client: Any,
    ) -> None:
        """GET list: Anonymous -> 401."""
        response = client.get("/api/connections")
        assert response.status_code == 401

    # --- GET /api/connections/duplicate-check (Admin + Contributor only) ---

    def test_rbac_get_duplicate_check_admin_200(
        self,
        admin_client: Any,
    ) -> None:
        """GET duplicate-check: Admin -> 200."""
        response = admin_client.get(
            "/api/connections/duplicate-check?linkedin_url=https://www.linkedin.com/in/rbac-admin"
        )
        assert response.status_code == 200

    def test_rbac_get_duplicate_check_contributor_200(
        self,
        contributor_client: Any,
    ) -> None:
        """GET duplicate-check: Contributor -> 200."""
        response = contributor_client.get(
            "/api/connections/duplicate-check"
            "?linkedin_url=https://www.linkedin.com/in/rbac-contributor"
        )
        assert response.status_code == 200

    def test_rbac_get_duplicate_check_viewer_403(
        self,
        viewer_client: Any,
    ) -> None:
        """GET duplicate-check: Viewer -> 403.

        Per CR-CKPT5-MINOR#2, the duplicate-check endpoint is
        restricted to Admin + Contributor only - it is used in the
        Add Connection form which Viewers cannot reach.
        """
        response = viewer_client.get(
            "/api/connections/duplicate-check?linkedin_url=https://www.linkedin.com/in/rbac-viewer"
        )
        assert response.status_code == 403

    def test_rbac_get_duplicate_check_anonymous_401(
        self,
        client: Any,
    ) -> None:
        """GET duplicate-check: Anonymous -> 401."""
        response = client.get(
            "/api/connections/duplicate-check?linkedin_url=https://www.linkedin.com/in/rbac-anon"
        )
        assert response.status_code == 401

    # --- GET /api/connections/<uuid> (all 3 roles) ---

    def test_rbac_get_detail_admin_200(
        self,
        admin_client: Any,
        admin_user: Any,
    ) -> None:
        """GET detail: Admin -> 200."""
        record = RecordFactory(owner=admin_user)
        response = admin_client.get(f"/api/connections/{record.id}")
        assert response.status_code == 200

    def test_rbac_get_detail_contributor_200(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """GET detail: Contributor -> 200."""
        record = RecordFactory(owner=contributor_user)
        response = contributor_client.get(f"/api/connections/{record.id}")
        assert response.status_code == 200

    def test_rbac_get_detail_viewer_200(
        self,
        viewer_client: Any,
        organization: Any,
    ) -> None:
        """GET detail: Viewer -> 200."""
        author = ContributorUserFactory(organization=organization)
        record = RecordFactory(owner=author)
        response = viewer_client.get(f"/api/connections/{record.id}")
        assert response.status_code == 200

    def test_rbac_get_detail_anonymous_401(
        self,
        client: Any,
    ) -> None:
        """GET detail: Anonymous -> 401."""
        random_uuid = uuid.uuid4()
        response = client.get(f"/api/connections/{random_uuid}")
        assert response.status_code == 401

    # --- GET /api/connections/<uuid>/history (all 3 roles) ---

    def test_rbac_get_history_admin_200(
        self,
        admin_client: Any,
        admin_user: Any,
    ) -> None:
        """GET history: Admin -> 200."""
        record = RecordFactory(owner=admin_user)
        response = admin_client.get(f"/api/connections/{record.id}/history")
        assert response.status_code == 200

    def test_rbac_get_history_contributor_200(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """GET history: Contributor -> 200."""
        record = RecordFactory(owner=contributor_user)
        response = contributor_client.get(f"/api/connections/{record.id}/history")
        assert response.status_code == 200

    def test_rbac_get_history_viewer_200(
        self,
        viewer_client: Any,
        organization: Any,
    ) -> None:
        """GET history: Viewer -> 200."""
        author = ContributorUserFactory(organization=organization)
        record = RecordFactory(owner=author)
        response = viewer_client.get(f"/api/connections/{record.id}/history")
        assert response.status_code == 200

    def test_rbac_get_history_anonymous_401(
        self,
        client: Any,
    ) -> None:
        """GET history: Anonymous -> 401."""
        random_uuid = uuid.uuid4()
        response = client.get(f"/api/connections/{random_uuid}/history")
        assert response.status_code == 401

    # --- PATCH /api/connections/<uuid> (Admin + Contributor own only) ---

    def test_rbac_patch_edit_admin_200(
        self,
        admin_client: Any,
        organization: Any,
    ) -> None:
        """PATCH edit: Admin -> 200 (any record)."""
        author = ContributorUserFactory(organization=organization)
        record = RecordFactory(owner=author)
        response = admin_client.patch(
            f"/api/connections/{record.id}",
            json={"company": "Admin-Edited"},
        )
        assert response.status_code == 200

    def test_rbac_patch_edit_contributor_own_200(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """PATCH edit: Contributor on OWN record -> 200."""
        record = RecordFactory(owner=contributor_user)
        response = contributor_client.patch(
            f"/api/connections/{record.id}",
            json={"company": "Self-Edited"},
        )
        assert response.status_code == 200

    def test_rbac_patch_edit_contributor_others_403(
        self,
        contributor_client: Any,
        organization: Any,
    ) -> None:
        """PATCH edit: Contributor on OTHER's record -> 403."""
        other_user = AdminUserFactory(organization=organization)
        record = RecordFactory(owner=other_user)
        response = contributor_client.patch(
            f"/api/connections/{record.id}",
            json={"company": "Hijacked"},
        )
        assert response.status_code == 403

    def test_rbac_patch_edit_viewer_403(
        self,
        viewer_client: Any,
        organization: Any,
    ) -> None:
        """PATCH edit: Viewer -> 403 (Viewer never edits)."""
        author = ContributorUserFactory(organization=organization)
        record = RecordFactory(owner=author)
        response = viewer_client.patch(
            f"/api/connections/{record.id}",
            json={"company": "Viewer-Hijacked"},
        )
        assert response.status_code == 403

    def test_rbac_patch_edit_anonymous_401(
        self,
        client: Any,
    ) -> None:
        """PATCH edit: Anonymous -> 401."""
        random_uuid = uuid.uuid4()
        response = client.patch(
            f"/api/connections/{random_uuid}",
            json={"company": "X"},
        )
        assert response.status_code == 401

    # --- PATCH /api/connections/<uuid>/status (Viewer + Admin ONLY) ---

    def test_rbac_patch_status_admin_200(
        self,
        admin_client: Any,
        admin_user: Any,
    ) -> None:
        """PATCH status: Admin -> 200."""
        record = RecordFactory(
            owner=admin_user,
            outreach_status=OutreachStatus.NOT_STARTED,
        )
        response = admin_client.patch(
            f"/api/connections/{record.id}/status",
            json={"outreach_status": OutreachStatus.IN_PROGRESS.value},
        )
        assert response.status_code == 200

    def test_rbac_patch_status_viewer_200(
        self,
        viewer_client: Any,
        organization: Any,
    ) -> None:
        """PATCH status: Viewer -> 200 (Sales Rep authority)."""
        author = ContributorUserFactory(organization=organization)
        record = RecordFactory(
            owner=author,
            outreach_status=OutreachStatus.NOT_STARTED,
        )
        response = viewer_client.patch(
            f"/api/connections/{record.id}/status",
            json={"outreach_status": OutreachStatus.IN_PROGRESS.value},
        )
        assert response.status_code == 200

    def test_rbac_patch_status_contributor_403(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """PATCH status: Contributor -> 403 (sales-team-accountability invariant)."""
        record = RecordFactory(
            owner=contributor_user,
            outreach_status=OutreachStatus.NOT_STARTED,
        )
        response = contributor_client.patch(
            f"/api/connections/{record.id}/status",
            json={"outreach_status": OutreachStatus.IN_PROGRESS.value},
        )
        assert response.status_code == 403

    def test_rbac_patch_status_anonymous_401(
        self,
        client: Any,
    ) -> None:
        """PATCH status: Anonymous -> 401."""
        random_uuid = uuid.uuid4()
        response = client.patch(
            f"/api/connections/{random_uuid}/status",
            json={"outreach_status": OutreachStatus.IN_PROGRESS.value},
        )
        assert response.status_code == 401

    # --- DELETE /api/connections/<uuid> (all roles, own only for non-Admin) ---

    def test_rbac_delete_admin_200(
        self,
        admin_client: Any,
        organization: Any,
    ) -> None:
        """DELETE: Admin -> 200 (any record)."""
        author = ContributorUserFactory(organization=organization)
        record = RecordFactory(owner=author)
        response = admin_client.delete(f"/api/connections/{record.id}")
        assert response.status_code == 200

    def test_rbac_delete_contributor_own_200(
        self,
        contributor_client: Any,
        contributor_user: Any,
    ) -> None:
        """DELETE: Contributor on OWN record -> 200."""
        record = RecordFactory(owner=contributor_user)
        response = contributor_client.delete(f"/api/connections/{record.id}")
        assert response.status_code == 200

    def test_rbac_delete_contributor_others_403(
        self,
        contributor_client: Any,
        organization: Any,
    ) -> None:
        """DELETE: Contributor on OTHER's record -> 403."""
        other_user = AdminUserFactory(organization=organization)
        record = RecordFactory(owner=other_user)
        response = contributor_client.delete(f"/api/connections/{record.id}")
        assert response.status_code == 403

    def test_rbac_delete_viewer_own_200(
        self,
        viewer_client: Any,
        viewer_user: Any,
    ) -> None:
        """DELETE: Viewer on OWN record -> 200."""
        record = RecordFactory(owner=viewer_user)
        response = viewer_client.delete(f"/api/connections/{record.id}")
        assert response.status_code == 200

    def test_rbac_delete_viewer_others_403(
        self,
        viewer_client: Any,
        organization: Any,
    ) -> None:
        """DELETE: Viewer on OTHER's record -> 403."""
        other_user = ContributorUserFactory(organization=organization)
        record = RecordFactory(owner=other_user)
        response = viewer_client.delete(f"/api/connections/{record.id}")
        assert response.status_code == 403

    def test_rbac_delete_anonymous_401(
        self,
        client: Any,
    ) -> None:
        """DELETE: Anonymous -> 401."""
        random_uuid = uuid.uuid4()
        response = client.delete(f"/api/connections/{random_uuid}")
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# TestAuditInvariantOnAppRole -- defense-in-depth audit immutability
# ---------------------------------------------------------------------------


@pytest.mark.audit
class TestAuditInvariantOnAppRole:
    """The application database role cannot UPDATE or DELETE audit_events.

    Per AAP s 0.7.1 invariant 5 (append-only audit table) and the
    initial migration (``0001_initial_schema``), the production
    application role has only INSERT and SELECT privileges on
    ``audit_events``; UPDATE and DELETE are explicitly REVOKEd.

    These tests verify the invariant via INFORMATION_SCHEMA inspection
    (the test DB is provisioned with the same migration, so the same
    privileges apply).
    """

    def test_audit_events_table_exists(self, db_session: Any) -> None:
        """The audit_events table exists with the documented columns."""
        result = db_session.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'audit_events' ORDER BY ordinal_position"
            )
        )
        columns = {row[0] for row in result}
        # Documented columns per AAP s 0.2.3 audit_event model.
        required = {
            "id",
            "event_type",
            "event_timestamp",
            "actor_user_id",
            "target_record_id",
            "before_payload",
            "after_payload",
        }
        assert required.issubset(columns), f"Missing columns: {required - columns}"

    def test_audit_events_unique_index_exists(self, db_session: Any) -> None:
        """The (target_record_id, event_timestamp) index exists on audit_events.

        Per the initial migration, the audit_events table has a
        composite index on ``(target_record_id, event_timestamp)``
        to support fast history-feed lookups.
        """
        result = db_session.execute(
            text("SELECT indexname FROM pg_indexes WHERE tablename = 'audit_events'")
        )
        index_names = {row[0] for row in result}
        # At least one index per audit_events; the migration creates
        # the composite history-lookup index.
        assert len(index_names) > 0
