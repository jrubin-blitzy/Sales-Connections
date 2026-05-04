"""API tests for the Connections blueprint (F-001/F-004/F-005/F-007/F-010/F-011).

Covers all eight wired endpoints under ``/api/connections``:

- ``POST   /api/connections``                       F-001 create
- ``GET    /api/connections``                       F-004 paginated feed
- ``GET    /api/connections/duplicate-check``       F-010 non-blocking warning
- ``GET    /api/connections/<uuid>``                F-011 detail
- ``GET    /api/connections/<uuid>/history``        F-011 audit history
- ``PATCH  /api/connections/<uuid>``                F-007 edit
- ``PATCH  /api/connections/<uuid>/status``         F-005 status mutation
- ``DELETE /api/connections/<uuid>``                F-007 soft delete

Per AAP Section 0.7.4 security invariants, every test verifies one or more
of:

* The route exists and is reachable through the HTTP API (NOT just via
  direct service-layer invocation, per QA Issue #1 of Checkpoint 2).
* RBAC at the route layer rejects forbidden roles with HTTP 403 BEFORE
  the service layer runs.
* Owner attribution is server-derived; client-supplied ``owner_*``
  fields are rejected as 422.
* Org-scoping is enforced; cross-org reads return 404 (info-disclosure
  defense).
* Soft-delete-aware reads default to ``WHERE deleted_at IS NULL``;
  Admin can opt out with ``?include_deleted=true``.
* Audit events are emitted on every state change in the same
  transaction.
* The static ``/duplicate-check`` route is matched correctly even
  though a sibling dynamic ``<uuid>`` route exists (Phase 7 of the
  checkpoint instructions calls this out as "CRITICAL").

Coordination contract:

Fixtures inherited from :mod:`backend.tests.conftest`:

- ``client``, ``authed_client``, ``admin_client``, ``viewer_client``
- ``contributor_user``, ``admin_user``, ``viewer_user``
- ``organization`` (default org)
- ``db_session`` (TRUNCATEs all tables on teardown)

Factory data setup uses ``backend/tests/factories.py``:

- :class:`RecordFactory` for active records
- :class:`SoftDeletedRecordFactory` for soft-deleted records
- :class:`TagFactory` for tag attachments
- :class:`ContributorUserFactory`/:class:`AdminUserFactory`/:class:`ViewerUserFactory`
- :class:`OrganizationFactory` for cross-org isolation tests
"""

from __future__ import annotations

# Standard library imports.
#
# ``typing.Any`` is used as a permissive annotation on fixture
# parameters whose concrete types (Flask test client, SQLAlchemy
# Session, factory boy User) are imported under TYPE_CHECKING in
# the fixture module.
#
# ``uuid.uuid4`` produces fresh UUIDs for negative-path tests
# (e.g., POST a non-existent record id and expect 404 with
# org-scope masking).
from typing import Any
import uuid

# Third-party runtime imports.
import pytest
from sqlalchemy import func, select

# First-party imports. Absolute paths only per the project's
# ``flake8-tidy-imports`` configuration.
from app.extensions import db
from app.models import AuditEvent
from app.models.enums import AuditEventType, OutreachStatus
from tests.factories import (
    ContributorUserFactory,
    OrganizationFactory,
    RecordFactory,
    SoftDeletedRecordFactory,
    TagFactory,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _audit_event_count() -> int:
    """Return the total number of rows in ``audit_events``.

    The query opens its own session so callers do not need to flush
    the surrounding test session.
    """
    with db.session() as session:
        return int(session.execute(select(func.count()).select_from(AuditEvent)).scalar_one())


def _audit_events_for_record(record_id: Any) -> list[AuditEvent]:
    """Return all audit events targeting the given record id."""
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


def _make_create_payload(
    *, full_name: str = "Jane Doe", linkedin_slug: str | None = None
) -> dict[str, Any]:
    """Construct a minimal valid create payload."""
    slug = linkedin_slug or f"jane-doe-{uuid.uuid4().hex[:8]}"
    return {
        "full_name": full_name,
        "linkedin_url": f"https://www.linkedin.com/in/{slug}",
        "company": "Acme Corp",
        "job_title": "VP of Operations",
        "relationship_context": "We worked together at a previous company.",
        "involvement": "Warm Intro",
    }


# ---------------------------------------------------------------------------
# TestPostCreate -- POST /api/connections (regression for F-001)
# ---------------------------------------------------------------------------


class TestPostCreate:
    """``POST /api/connections`` creates a record (F-001).

    Regression tests that confirm the F-001 happy path still works
    after the L4/L5 routes were appended to the same blueprint
    (per the Checkpoint 2 fix for QA Issue #1).
    """

    def test_contributor_creates_record_returns_201(
        self,
        authed_client: Any,
        organization: Any,
    ) -> None:
        """Happy path: Contributor creates a record."""
        payload = _make_create_payload()
        response = authed_client.post("/api/connections", json=payload)
        assert response.status_code == 201
        body = response.get_json()
        assert body["full_name"] == "Jane Doe"
        assert body["company"] == "Acme Corp"
        assert body["outreach_status"] == OutreachStatus.NOT_STARTED.value
        assert "id" in body
        assert "owner_user_id" in body
        assert "submission_date" in body
        # CR-CKPT5-MINOR#1: 201 responses MUST include a Location
        # header pointing at the canonical detail URL of the new
        # resource (RFC 7231 Section 6.3.2). This is the single
        # source-of-truth assertion for that contract.
        assert response.headers.get("Location") == f"/api/connections/{body['id']}"

    def test_admin_creates_record_returns_201(
        self,
        admin_client: Any,
        organization: Any,
    ) -> None:
        """Admin role admitted to POST /api/connections."""
        payload = _make_create_payload()
        response = admin_client.post("/api/connections", json=payload)
        assert response.status_code == 201

    def test_viewer_forbidden(
        self,
        viewer_client: Any,
        organization: Any,
    ) -> None:
        """Viewer (Sales Rep) rejected with 403 from POST."""
        payload = _make_create_payload()
        response = viewer_client.post("/api/connections", json=payload)
        assert response.status_code == 403
        body = response.get_json()
        assert body["error"]["code"] == "forbidden"

    def test_anonymous_unauthorized(self, client: Any) -> None:
        """Unauthenticated POST rejected with 401."""
        response = client.post("/api/connections", json=_make_create_payload())
        assert response.status_code == 401

    def test_owner_user_id_payload_rejected_422(
        self,
        authed_client: Any,
        organization: Any,
    ) -> None:
        """Client-supplied owner_user_id rejected by extra='forbid'."""
        payload = _make_create_payload()
        payload["owner_user_id"] = str(uuid.uuid4())
        response = authed_client.post("/api/connections", json=payload)
        assert response.status_code == 422
        body = response.get_json()
        assert body["error"]["code"] == "validation_failed"


# ---------------------------------------------------------------------------
# TestGetList -- GET /api/connections (F-004)
# ---------------------------------------------------------------------------


class TestGetList:
    """``GET /api/connections`` returns a paginated, filterable feed (F-004)."""

    def test_admin_lists_records(
        self,
        admin_client: Any,
        admin_user: Any,
        organization: Any,
    ) -> None:
        """Admin sees all records in their org."""
        RecordFactory.create_batch(3, owner=admin_user)

        response = admin_client.get("/api/connections")

        assert response.status_code == 200
        body = response.get_json()
        assert "items" in body
        assert "total" in body
        assert "limit" in body
        assert "offset" in body
        assert body["total"] == 3
        assert len(body["items"]) == 3

    def test_contributor_lists_records(
        self,
        authed_client: Any,
        contributor_user: Any,
        organization: Any,
    ) -> None:
        """Contributor sees all records in their org (not just their own)."""
        # Records owned by another user in the same org
        other_user = ContributorUserFactory(organization=organization)
        RecordFactory.create_batch(2, owner=other_user)
        RecordFactory.create_batch(1, owner=contributor_user)

        response = authed_client.get("/api/connections")

        assert response.status_code == 200
        body = response.get_json()
        assert body["total"] == 3

    def test_viewer_lists_records(
        self,
        viewer_client: Any,
        viewer_user: Any,
        organization: Any,
    ) -> None:
        """Viewer (Sales Rep) sees all records in their org."""
        contributor = ContributorUserFactory(organization=organization)
        RecordFactory.create_batch(2, owner=contributor)

        response = viewer_client.get("/api/connections")

        assert response.status_code == 200
        body = response.get_json()
        assert body["total"] == 2

    def test_anonymous_unauthorized(self, client: Any) -> None:
        """Unauthenticated GET rejected with 401."""
        response = client.get("/api/connections")
        assert response.status_code == 401

    def test_default_sort_is_submission_date_desc(
        self,
        admin_client: Any,
        admin_user: Any,
        organization: Any,
    ) -> None:
        """Default sort is ``submission_date DESC`` per AAP §0.5.2 Layer 4."""
        from datetime import UTC, datetime, timedelta  # noqa: PLC0415

        now = datetime.now(UTC)
        # Three records with different submission_dates
        RecordFactory(
            owner=admin_user,
            full_name="Oldest",
            submission_date=now - timedelta(days=2),
        )
        RecordFactory(
            owner=admin_user,
            full_name="Newest",
            submission_date=now,
        )
        RecordFactory(
            owner=admin_user,
            full_name="Middle",
            submission_date=now - timedelta(days=1),
        )

        response = admin_client.get("/api/connections")

        assert response.status_code == 200
        body = response.get_json()
        assert [item["full_name"] for item in body["items"]] == [
            "Newest",
            "Middle",
            "Oldest",
        ]

    def test_soft_delete_excluded_by_default(
        self,
        authed_client: Any,
        contributor_user: Any,
        organization: Any,
    ) -> None:
        """Soft-deleted records are excluded by default (AAP §0.7.1 inv 4)."""
        RecordFactory(owner=contributor_user, full_name="Active")
        SoftDeletedRecordFactory(owner=contributor_user, full_name="Deleted")

        response = authed_client.get("/api/connections")

        assert response.status_code == 200
        body = response.get_json()
        assert body["total"] == 1
        assert body["items"][0]["full_name"] == "Active"

    def test_admin_include_deleted_opt_in(
        self,
        admin_client: Any,
        admin_user: Any,
        organization: Any,
    ) -> None:
        """Admin can opt-in to soft-deleted records via include_deleted=true."""
        RecordFactory(owner=admin_user, full_name="Active")
        SoftDeletedRecordFactory(owner=admin_user, full_name="Deleted")

        response = admin_client.get("/api/connections?include_deleted=true")

        assert response.status_code == 200
        body = response.get_json()
        assert body["total"] == 2

    def test_non_admin_include_deleted_silently_downgraded(
        self,
        authed_client: Any,
        contributor_user: Any,
        organization: Any,
    ) -> None:
        """Non-Admin requesting include_deleted=true is silently downgraded.

        Per the F-007 + F-009 architectural invariant, Contributors and
        Viewers cannot see soft-deleted records even if they explicitly
        request the flag. We DO NOT 403 (info-disclosure defense); we
        silently coerce to False.
        """
        RecordFactory(owner=contributor_user, full_name="Active")
        SoftDeletedRecordFactory(owner=contributor_user, full_name="Deleted")

        response = authed_client.get("/api/connections?include_deleted=true")

        assert response.status_code == 200
        body = response.get_json()
        # Soft-deleted record is NOT visible to the Contributor.
        assert body["total"] == 1
        assert body["items"][0]["full_name"] == "Active"

    def test_filter_by_company(
        self,
        admin_client: Any,
        admin_user: Any,
        organization: Any,
    ) -> None:
        """Filter by company performs case-insensitive substring match."""
        RecordFactory(owner=admin_user, company="Acme Corp")
        RecordFactory(owner=admin_user, company="Globex Industries")

        response = admin_client.get("/api/connections?company=acme")

        assert response.status_code == 200
        body = response.get_json()
        assert body["total"] == 1
        assert body["items"][0]["company"] == "Acme Corp"

    def test_filter_by_involvement(
        self,
        admin_client: Any,
        admin_user: Any,
        organization: Any,
    ) -> None:
        """Filter by involvement=Warm Intro returns only matching records."""
        from app.models.enums import InvolvementType  # noqa: PLC0415

        RecordFactory(owner=admin_user, involvement=InvolvementType.WARM_INTRO)
        RecordFactory(owner=admin_user, involvement=InvolvementType.WARM_INTRO)
        RecordFactory(owner=admin_user, involvement=InvolvementType.SOFT_REFERENCE)

        response = admin_client.get("/api/connections?involvement=Warm Intro")

        assert response.status_code == 200
        body = response.get_json()
        assert body["total"] == 2
        assert all(item["involvement"] == "Warm Intro" for item in body["items"])

    def test_filter_by_outreach_status(
        self,
        admin_client: Any,
        admin_user: Any,
        organization: Any,
    ) -> None:
        """Filter by outreach_status=Closed returns only matching records."""
        RecordFactory(owner=admin_user, outreach_status=OutreachStatus.CLOSED)
        RecordFactory(owner=admin_user, outreach_status=OutreachStatus.NOT_STARTED)

        response = admin_client.get("/api/connections?outreach_status=Closed")

        assert response.status_code == 200
        body = response.get_json()
        assert body["total"] == 1
        assert body["items"][0]["outreach_status"] == "Closed"

    def test_sort_by_full_name_asc(
        self,
        admin_client: Any,
        admin_user: Any,
        organization: Any,
    ) -> None:
        """Sort by full_name ascending."""
        RecordFactory(owner=admin_user, full_name="Charlie")
        RecordFactory(owner=admin_user, full_name="Alice")
        RecordFactory(owner=admin_user, full_name="Bob")

        response = admin_client.get("/api/connections?sort=full_name&sort_dir=asc")

        assert response.status_code == 200
        body = response.get_json()
        assert [item["full_name"] for item in body["items"]] == [
            "Alice",
            "Bob",
            "Charlie",
        ]

    def test_unknown_sort_key_returns_422(
        self,
        admin_client: Any,
        admin_user: Any,
        organization: Any,
    ) -> None:
        """Unknown sort key surfaces as HTTP 422."""
        RecordFactory(owner=admin_user)
        response = admin_client.get("/api/connections?sort=bogus_key")
        assert response.status_code == 422

    def test_pagination_limit_and_offset(
        self,
        admin_client: Any,
        admin_user: Any,
        organization: Any,
    ) -> None:
        """``limit`` and ``offset`` query params control page size/start."""
        RecordFactory.create_batch(5, owner=admin_user)

        # First page
        response = admin_client.get("/api/connections?limit=2&offset=0")
        assert response.status_code == 200
        body = response.get_json()
        assert body["total"] == 5
        assert len(body["items"]) == 2
        assert body["limit"] == 2
        assert body["offset"] == 0

        # Second page
        response2 = admin_client.get("/api/connections?limit=2&offset=2")
        body2 = response2.get_json()
        assert len(body2["items"]) == 2
        assert body2["offset"] == 2

    def test_invalid_limit_returns_422(
        self,
        admin_client: Any,
        admin_user: Any,
        organization: Any,
    ) -> None:
        """Non-integer limit returns 422."""
        response = admin_client.get("/api/connections?limit=abc")
        assert response.status_code == 422

    def test_org_scoped_isolation(
        self,
        authed_client: Any,
        contributor_user: Any,
        organization: Any,
    ) -> None:
        """Cross-org records are NOT visible (AAP §0.7.1 invariant 3)."""
        # Records in this org
        RecordFactory.create_batch(2, owner=contributor_user)
        # Records in a different org
        other_org = OrganizationFactory()
        other_user = ContributorUserFactory(organization=other_org)
        RecordFactory.create_batch(3, owner=other_user)

        response = authed_client.get("/api/connections")

        assert response.status_code == 200
        body = response.get_json()
        assert body["total"] == 2  # NOT 5


# ---------------------------------------------------------------------------
# TestGetDetail -- GET /api/connections/<uuid> (F-011)
# ---------------------------------------------------------------------------


class TestGetDetail:
    """``GET /api/connections/<uuid>`` returns a single record (F-011)."""

    def test_returns_record_for_admin(
        self,
        admin_client: Any,
        admin_user: Any,
    ) -> None:
        """Admin fetches detail for a record they own."""
        record = RecordFactory(owner=admin_user)
        response = admin_client.get(f"/api/connections/{record.id}")

        assert response.status_code == 200
        body = response.get_json()
        assert body["id"] == str(record.id)
        assert body["full_name"] == record.full_name

    def test_returns_record_for_contributor(
        self,
        authed_client: Any,
        contributor_user: Any,
    ) -> None:
        """Contributor fetches detail for a record they own."""
        record = RecordFactory(owner=contributor_user)
        response = authed_client.get(f"/api/connections/{record.id}")

        assert response.status_code == 200

    def test_returns_record_for_viewer(
        self,
        viewer_client: Any,
        viewer_user: Any,
        organization: Any,
    ) -> None:
        """Viewer can read any record in their org."""
        contributor = ContributorUserFactory(organization=organization)
        record = RecordFactory(owner=contributor)
        response = viewer_client.get(f"/api/connections/{record.id}")

        assert response.status_code == 200

    def test_unknown_uuid_returns_404(
        self,
        admin_client: Any,
    ) -> None:
        """Non-existent record id returns 404."""
        bogus = uuid.uuid4()
        response = admin_client.get(f"/api/connections/{bogus}")
        assert response.status_code == 404

    def test_cross_org_returns_404(
        self,
        authed_client: Any,
        contributor_user: Any,
    ) -> None:
        """Cross-org record id returns 404 (info-disclosure defense)."""
        # Record in DIFFERENT org
        other_org = OrganizationFactory()
        other_user = ContributorUserFactory(organization=other_org)
        record = RecordFactory(owner=other_user)

        response = authed_client.get(f"/api/connections/{record.id}")
        assert response.status_code == 404

    def test_soft_deleted_returns_404_for_non_admin(
        self,
        authed_client: Any,
        contributor_user: Any,
    ) -> None:
        """Soft-deleted record is invisible to Contributor by default."""
        record = SoftDeletedRecordFactory(owner=contributor_user)
        response = authed_client.get(f"/api/connections/{record.id}")
        assert response.status_code == 404

    def test_admin_can_view_soft_deleted_with_opt_in(
        self,
        admin_client: Any,
        admin_user: Any,
    ) -> None:
        """Admin can fetch a soft-deleted record via include_deleted=true."""
        record = SoftDeletedRecordFactory(owner=admin_user)
        response = admin_client.get(f"/api/connections/{record.id}?include_deleted=true")
        assert response.status_code == 200
        body = response.get_json()
        assert body["deleted_at"] is not None

    def test_anonymous_unauthorized(
        self,
        client: Any,
    ) -> None:
        """Unauthenticated request returns 401."""
        bogus = uuid.uuid4()
        response = client.get(f"/api/connections/{bogus}")
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# TestGetHistory -- GET /api/connections/<uuid>/history (F-011)
# ---------------------------------------------------------------------------


class TestGetHistory:
    """``GET /api/connections/<uuid>/history`` returns audit history (F-011)."""

    def test_returns_create_event(
        self,
        admin_client: Any,
        admin_user: Any,
        organization: Any,
    ) -> None:
        """A freshly-created record's history includes the CREATE event."""
        # POST to create so the audit event fires
        payload = _make_create_payload()
        create_response = admin_client.post("/api/connections", json=payload)
        record_id = create_response.get_json()["id"]

        response = admin_client.get(f"/api/connections/{record_id}/history")

        assert response.status_code == 200
        body = response.get_json()
        assert body["total"] >= 1
        # Most recent first; the CREATE event should be the (only) event
        events = body["items"]
        create_events = [e for e in events if e["event_type"] == "create"]
        assert len(create_events) == 1
        assert create_events[0]["actor_user_id"] == str(admin_user.id)

    def test_cross_org_returns_404(
        self,
        authed_client: Any,
        contributor_user: Any,
    ) -> None:
        """History endpoint enforces org-scope BEFORE the audit query."""
        other_org = OrganizationFactory()
        other_user = ContributorUserFactory(organization=other_org)
        record = RecordFactory(owner=other_user)

        response = authed_client.get(f"/api/connections/{record.id}/history")
        assert response.status_code == 404

    def test_unknown_record_returns_404(
        self,
        admin_client: Any,
    ) -> None:
        """Unknown record id returns 404."""
        bogus = uuid.uuid4()
        response = admin_client.get(f"/api/connections/{bogus}/history")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# TestPatchEdit -- PATCH /api/connections/<uuid> (F-007 edit)
# ---------------------------------------------------------------------------


class TestPatchEdit:
    """``PATCH /api/connections/<uuid>`` edits a record (F-007)."""

    def test_owner_contributor_can_edit_own_record(
        self,
        authed_client: Any,
        contributor_user: Any,
    ) -> None:
        """Contributor can edit a record they own."""
        record = RecordFactory(owner=contributor_user, company="Old Corp")

        response = authed_client.patch(
            f"/api/connections/{record.id}",
            json={"company": "New Corp"},
        )

        assert response.status_code == 200
        body = response.get_json()
        assert body["company"] == "New Corp"

    def test_admin_can_edit_any_record(
        self,
        admin_client: Any,
        admin_user: Any,
        organization: Any,
    ) -> None:
        """Admin can edit a record they do NOT own."""
        contributor = ContributorUserFactory(organization=organization)
        record = RecordFactory(owner=contributor, company="Old Corp")

        response = admin_client.patch(
            f"/api/connections/{record.id}",
            json={"company": "New Corp"},
        )

        assert response.status_code == 200

    def test_non_owner_contributor_forbidden(
        self,
        authed_client: Any,
        contributor_user: Any,
        organization: Any,
    ) -> None:
        """Contributor cannot edit a record they do NOT own (returns 403)."""
        other_user = ContributorUserFactory(organization=organization)
        record = RecordFactory(owner=other_user)

        response = authed_client.patch(
            f"/api/connections/{record.id}",
            json={"company": "Hijacked Corp"},
        )

        assert response.status_code == 403

    def test_viewer_cannot_edit(
        self,
        viewer_client: Any,
        viewer_user: Any,
        organization: Any,
    ) -> None:
        """Viewer is rejected at the route layer with 403."""
        contributor = ContributorUserFactory(organization=organization)
        record = RecordFactory(owner=contributor)

        response = viewer_client.patch(
            f"/api/connections/{record.id}",
            json={"company": "Hijacked Corp"},
        )
        assert response.status_code == 403

    def test_unknown_record_returns_404(
        self,
        authed_client: Any,
    ) -> None:
        """Unknown record id returns 404."""
        bogus = uuid.uuid4()
        response = authed_client.patch(
            f"/api/connections/{bogus}",
            json={"company": "Whatever"},
        )
        assert response.status_code == 404

    def test_cross_org_returns_404(
        self,
        admin_client: Any,
    ) -> None:
        """Cross-org record id returns 404 (info-disclosure defense)."""
        other_org = OrganizationFactory()
        other_user = ContributorUserFactory(organization=other_org)
        record = RecordFactory(owner=other_user)

        response = admin_client.patch(
            f"/api/connections/{record.id}",
            json={"company": "Whatever"},
        )
        assert response.status_code == 404

    def test_outreach_status_in_body_rejected(
        self,
        authed_client: Any,
        contributor_user: Any,
    ) -> None:
        """Edit endpoint cannot mutate outreach_status (F-005 separation)."""
        record = RecordFactory(owner=contributor_user)
        response = authed_client.patch(
            f"/api/connections/{record.id}",
            json={"outreach_status": "Closed"},
        )
        assert response.status_code == 422

    def test_owner_user_id_in_body_rejected(
        self,
        authed_client: Any,
        contributor_user: Any,
    ) -> None:
        """Client-supplied owner_user_id rejected (owner immutable)."""
        record = RecordFactory(owner=contributor_user)
        response = authed_client.patch(
            f"/api/connections/{record.id}",
            json={"owner_user_id": str(uuid.uuid4())},
        )
        assert response.status_code == 422

    def test_audit_event_emitted(
        self,
        authed_client: Any,
        contributor_user: Any,
    ) -> None:
        """A successful edit emits an EDIT audit event."""
        record = RecordFactory(owner=contributor_user, company="Old Corp")
        events_before = _audit_events_for_record(record.id)

        authed_client.patch(
            f"/api/connections/{record.id}",
            json={"company": "New Corp"},
        )

        events_after = _audit_events_for_record(record.id)
        edit_events = [e for e in events_after if e.event_type == AuditEventType.EDIT]
        # Exactly one new edit event
        assert (
            len(edit_events)
            == len([e for e in events_before if e.event_type == AuditEventType.EDIT]) + 1
        )


# ---------------------------------------------------------------------------
# TestPatchStatus -- PATCH /api/connections/<uuid>/status (F-005)
# ---------------------------------------------------------------------------


class TestPatchStatus:
    """``PATCH /api/connections/<uuid>/status`` mutates outreach status (F-005)."""

    def test_viewer_can_update_status(
        self,
        viewer_client: Any,
        viewer_user: Any,
        organization: Any,
    ) -> None:
        """Sales Rep (Viewer) can mutate status (the F-005 invariant)."""
        contributor = ContributorUserFactory(organization=organization)
        record = RecordFactory(owner=contributor)

        response = viewer_client.patch(
            f"/api/connections/{record.id}/status",
            json={"outreach_status": "In Progress"},
        )

        assert response.status_code == 200
        body = response.get_json()
        assert body["outreach_status"] == "In Progress"

    def test_admin_can_update_status(
        self,
        admin_client: Any,
        admin_user: Any,
        organization: Any,
    ) -> None:
        """Admin can mutate status."""
        contributor = ContributorUserFactory(organization=organization)
        record = RecordFactory(owner=contributor)

        response = admin_client.patch(
            f"/api/connections/{record.id}/status",
            json={"outreach_status": "Closed"},
        )

        assert response.status_code == 200

    def test_contributor_cannot_update_status_even_on_own_record(
        self,
        authed_client: Any,
        contributor_user: Any,
    ) -> None:
        """Contributor (the OWNER) cannot mutate status (F-005 invariant).

        This is the canonical F-005 RBAC rule: "Outreach status is
        updatable only by Sales Rep or Admin roles, never by the
        original submitter without Admin rights, in order to preserve
        sales team accountability." (AAP §0.1.2, DL-0019)
        """
        record = RecordFactory(owner=contributor_user)
        response = authed_client.patch(
            f"/api/connections/{record.id}/status",
            json={"outreach_status": "In Progress"},
        )
        assert response.status_code == 403
        body = response.get_json()
        assert body["error"]["code"] == "forbidden"

    def test_anonymous_unauthorized(
        self,
        client: Any,
    ) -> None:
        """Unauthenticated request returns 401."""
        bogus = uuid.uuid4()
        response = client.patch(
            f"/api/connections/{bogus}/status",
            json={"outreach_status": "In Progress"},
        )
        assert response.status_code == 401

    def test_unknown_record_returns_404(
        self,
        admin_client: Any,
    ) -> None:
        """Unknown record id returns 404."""
        bogus = uuid.uuid4()
        response = admin_client.patch(
            f"/api/connections/{bogus}/status",
            json={"outreach_status": "Closed"},
        )
        assert response.status_code == 404

    def test_invalid_status_value_returns_422(
        self,
        admin_client: Any,
        admin_user: Any,
    ) -> None:
        """Unknown enum value rejected with 422."""
        record = RecordFactory(owner=admin_user)
        response = admin_client.patch(
            f"/api/connections/{record.id}/status",
            json={"outreach_status": "Bogus"},
        )
        assert response.status_code == 422

    def test_no_op_idempotent_no_audit_event(
        self,
        admin_client: Any,
        admin_user: Any,
    ) -> None:
        """Setting status to its current value emits NO audit event."""
        record = RecordFactory(owner=admin_user, outreach_status=OutreachStatus.NOT_STARTED)
        events_before = _audit_event_count()

        response = admin_client.patch(
            f"/api/connections/{record.id}/status",
            json={"outreach_status": "Not Started"},
        )

        assert response.status_code == 200
        events_after = _audit_event_count()
        assert events_after == events_before

    def test_real_status_change_emits_audit_event(
        self,
        admin_client: Any,
        admin_user: Any,
    ) -> None:
        """A genuine status change emits exactly one status_change event."""
        record = RecordFactory(owner=admin_user, outreach_status=OutreachStatus.NOT_STARTED)
        events_before = _audit_events_for_record(record.id)

        admin_client.patch(
            f"/api/connections/{record.id}/status",
            json={"outreach_status": "In Progress"},
        )

        events_after = _audit_events_for_record(record.id)
        status_changes = [e for e in events_after if e.event_type == AuditEventType.STATUS_CHANGE]
        # One MORE status_change event than before
        assert (
            len(status_changes)
            == len([e for e in events_before if e.event_type == AuditEventType.STATUS_CHANGE]) + 1
        )
        # Verify before/after payload structure
        latest = status_changes[-1]
        assert latest.before_payload == {"outreach_status": "Not Started"}
        assert latest.after_payload == {"outreach_status": "In Progress"}

    def test_extra_field_in_body_rejected(
        self,
        admin_client: Any,
        admin_user: Any,
    ) -> None:
        """Smuggled extra fields rejected by extra='forbid'."""
        record = RecordFactory(owner=admin_user)
        response = admin_client.patch(
            f"/api/connections/{record.id}/status",
            json={"outreach_status": "Closed", "owner_user_id": str(uuid.uuid4())},
        )
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# TestDelete -- DELETE /api/connections/<uuid> (F-007 soft delete)
# ---------------------------------------------------------------------------


class TestDelete:
    """``DELETE /api/connections/<uuid>`` soft-deletes a record (F-007)."""

    def test_owner_contributor_can_soft_delete(
        self,
        authed_client: Any,
        contributor_user: Any,
    ) -> None:
        """Contributor can soft-delete a record they own."""
        record = RecordFactory(owner=contributor_user)

        response = authed_client.delete(f"/api/connections/{record.id}")

        assert response.status_code == 200
        body = response.get_json()
        assert body["deleted_at"] is not None

    def test_admin_can_soft_delete_any(
        self,
        admin_client: Any,
        admin_user: Any,
        organization: Any,
    ) -> None:
        """Admin can soft-delete any record in their org."""
        contributor = ContributorUserFactory(organization=organization)
        record = RecordFactory(owner=contributor)

        response = admin_client.delete(f"/api/connections/{record.id}")

        assert response.status_code == 200

    def test_non_owner_contributor_forbidden(
        self,
        authed_client: Any,
        contributor_user: Any,
        organization: Any,
    ) -> None:
        """Contributor cannot soft-delete another user's record."""
        other_user = ContributorUserFactory(organization=organization)
        record = RecordFactory(owner=other_user)

        response = authed_client.delete(f"/api/connections/{record.id}")
        assert response.status_code == 403

    def test_unknown_record_returns_404(
        self,
        authed_client: Any,
    ) -> None:
        """Unknown record id returns 404."""
        bogus = uuid.uuid4()
        response = authed_client.delete(f"/api/connections/{bogus}")
        assert response.status_code == 404

    def test_anonymous_unauthorized(
        self,
        client: Any,
    ) -> None:
        """Unauthenticated request returns 401."""
        bogus = uuid.uuid4()
        response = client.delete(f"/api/connections/{bogus}")
        assert response.status_code == 401

    def test_idempotent_second_delete_no_new_audit(
        self,
        authed_client: Any,
        contributor_user: Any,
    ) -> None:
        """A second soft-delete is a no-op (no new audit event)."""
        record = RecordFactory(owner=contributor_user)

        # First delete
        first = authed_client.delete(f"/api/connections/{record.id}")
        assert first.status_code == 200

        events_after_first = _audit_events_for_record(record.id)
        soft_deletes_first = [
            e for e in events_after_first if e.event_type == AuditEventType.SOFT_DELETE
        ]
        assert len(soft_deletes_first) == 1

        # Second delete (idempotent)
        second = authed_client.delete(f"/api/connections/{record.id}")
        assert second.status_code == 200

        events_after_second = _audit_events_for_record(record.id)
        soft_deletes_second = [
            e for e in events_after_second if e.event_type == AuditEventType.SOFT_DELETE
        ]
        # Still exactly ONE soft_delete audit event
        assert len(soft_deletes_second) == 1


# ---------------------------------------------------------------------------
# TestDuplicateCheck -- GET /api/connections/duplicate-check (F-010)
# ---------------------------------------------------------------------------


class TestDuplicateCheck:
    """``GET /api/connections/duplicate-check`` returns non-blocking warning (F-010).

    These tests also verify the CRITICAL route-ordering invariant from
    Phase 7 of the checkpoint instructions: the static
    ``/duplicate-check`` route MUST resolve correctly even though a
    sibling ``<uuid:record_id>`` dynamic route exists.
    """

    def test_clean_url_returns_200_with_duplicate_false(
        self,
        authed_client: Any,
        contributor_user: Any,
        organization: Any,
    ) -> None:
        """An unknown URL returns 200 with duplicate_found=False."""
        response = authed_client.get(
            "/api/connections/duplicate-check"
            "?linkedin_url=https://www.linkedin.com/in/never-seen-this-name"
        )

        assert response.status_code == 200
        body = response.get_json()
        assert body["duplicate_found"] is False
        assert body["existing_record_id"] is None
        assert "normalized_linkedin_url" in body

    def test_existing_url_returns_200_with_duplicate_true(
        self,
        authed_client: Any,
        contributor_user: Any,
    ) -> None:
        """An existing URL returns 200 with duplicate_found=True."""
        record = RecordFactory(
            owner=contributor_user,
            linkedin_url="https://www.linkedin.com/in/jane-doe-known",
            normalized_linkedin_url="https://linkedin.com/in/jane-doe-known",
        )

        response = authed_client.get(
            f"/api/connections/duplicate-check?linkedin_url={record.linkedin_url}"
        )

        assert response.status_code == 200
        body = response.get_json()
        assert body["duplicate_found"] is True
        assert body["existing_record_id"] == str(record.id)
        assert body["existing_owner_display_name"] == record.owner_display_name

    def test_static_route_resolves_before_dynamic(
        self,
        authed_client: Any,
        contributor_user: Any,
    ) -> None:
        """``/duplicate-check`` resolves to F-010 endpoint, NOT F-011 detail.

        CRITICAL test from Phase 7 of the checkpoint instructions.
        Without correct route registration order, Flask might attempt to
        match ``duplicate-check`` against the ``<uuid:record_id>``
        converter. The ``uuid`` converter rejects it, so the static
        route wins on URL-converter grounds alone, but registering the
        static route first is defense-in-depth.
        """
        # The endpoint requires a query param; without it we get 422,
        # NOT 404. If routing were wrong, we'd get 404 (the dynamic
        # route would not match "duplicate-check" as a UUID).
        response = authed_client.get("/api/connections/duplicate-check")
        assert response.status_code == 422  # Missing linkedin_url
        body = response.get_json()
        assert body["error"]["code"] == "validation_failed"

    def test_missing_linkedin_url_returns_422(
        self,
        authed_client: Any,
    ) -> None:
        """Missing linkedin_url query param returns 422."""
        response = authed_client.get("/api/connections/duplicate-check")
        assert response.status_code == 422

    def test_malformed_url_returns_422(
        self,
        authed_client: Any,
    ) -> None:
        """Malformed URL returns 422 with field-scoped error."""
        response = authed_client.get(
            "/api/connections/duplicate-check?linkedin_url=not-a-valid-url"
        )
        assert response.status_code == 422

    def test_org_scoped(
        self,
        authed_client: Any,
        contributor_user: Any,
    ) -> None:
        """Cross-org records do NOT match the duplicate-check query."""
        # Record in DIFFERENT org
        other_org = OrganizationFactory()
        other_user = ContributorUserFactory(organization=other_org)
        record = RecordFactory(
            owner=other_user,
            linkedin_url="https://www.linkedin.com/in/cross-org-name",
            normalized_linkedin_url="https://linkedin.com/in/cross-org-name",
        )

        response = authed_client.get(
            f"/api/connections/duplicate-check?linkedin_url={record.linkedin_url}"
        )

        assert response.status_code == 200
        body = response.get_json()
        # Cross-org record is NOT a duplicate from the actor's POV
        assert body["duplicate_found"] is False

    def test_soft_deleted_does_not_match(
        self,
        authed_client: Any,
        contributor_user: Any,
    ) -> None:
        """Soft-deleted records do NOT count as duplicates (URL freed)."""
        SoftDeletedRecordFactory(
            owner=contributor_user,
            linkedin_url="https://www.linkedin.com/in/once-known",
            normalized_linkedin_url="https://linkedin.com/in/once-known",
        )

        response = authed_client.get(
            "/api/connections/duplicate-check?linkedin_url=https://www.linkedin.com/in/once-known"
        )

        assert response.status_code == 200
        body = response.get_json()
        assert body["duplicate_found"] is False

    def test_exclude_record_id_self_excludes(
        self,
        authed_client: Any,
        contributor_user: Any,
    ) -> None:
        """``exclude_record_id`` excludes the matching record (edit flow)."""
        record = RecordFactory(
            owner=contributor_user,
            linkedin_url="https://www.linkedin.com/in/edit-flow",
            normalized_linkedin_url="https://linkedin.com/in/edit-flow",
        )

        # WITHOUT exclude_record_id: duplicate_found=True
        response_without = authed_client.get(
            f"/api/connections/duplicate-check?linkedin_url={record.linkedin_url}"
        )
        assert response_without.get_json()["duplicate_found"] is True

        # WITH exclude_record_id: duplicate_found=False
        response_with = authed_client.get(
            "/api/connections/duplicate-check"
            f"?linkedin_url={record.linkedin_url}&exclude_record_id={record.id}"
        )
        assert response_with.get_json()["duplicate_found"] is False

    def test_invalid_exclude_record_id_returns_422(
        self,
        authed_client: Any,
    ) -> None:
        """Invalid exclude_record_id UUID returns 422."""
        response = authed_client.get(
            "/api/connections/duplicate-check"
            "?linkedin_url=https://www.linkedin.com/in/x"
            "&exclude_record_id=not-a-uuid"
        )
        assert response.status_code == 422

    def test_anonymous_unauthorized(
        self,
        client: Any,
    ) -> None:
        """Unauthenticated request returns 401."""
        response = client.get(
            "/api/connections/duplicate-check?linkedin_url=https://www.linkedin.com/in/x"
        )
        assert response.status_code == 401

    # ----- CR-CKPT5-MINOR#2 RBAC scope: Contributor + Admin only -----
    #
    # Per the Checkpoint 5 RBAC scope (CR-CKPT5-MINOR#2) and AAP
    # Section 0.5.4 / Section 1.2: F-010 is a *pre-submit* warning
    # for the connection-creation flow. Sales Reps (Viewer role) do
    # not submit new records, so the duplicate-check feature belongs
    # only to Contributor and Admin. Viewers can still discover
    # existing records via the standard list/detail endpoints;
    # rejecting them from /duplicate-check is a scope decision.
    #
    # Each role gets its own test method so the underlying shared
    # ``client`` fixture's cookie state is unambiguous (only one
    # ``_mint_session_cookie`` call per test invocation). Combining
    # the three fixtures into one test would let the last-resolved
    # fixture's cookie clobber the earlier ones, masking RBAC bugs.

    def test_contributor_can_check(self, authed_client: Any) -> None:
        """Contributor (the canonical record-author role) is admitted."""
        url = "https://www.linkedin.com/in/duplicate-check-contributor"
        path = f"/api/connections/duplicate-check?linkedin_url={url}"
        response = authed_client.get(path)
        assert response.status_code == 200

    def test_admin_can_check(self, admin_client: Any) -> None:
        """Admin is admitted (Admins inherit Contributor capabilities)."""
        url = "https://www.linkedin.com/in/duplicate-check-admin"
        path = f"/api/connections/duplicate-check?linkedin_url={url}"
        response = admin_client.get(path)
        assert response.status_code == 200

    def test_viewer_forbidden(self, viewer_client: Any) -> None:
        """Viewer (Sales Rep) is rejected with 403 forbidden.

        Previously the decorator admitted Viewer (the checkpoint-5
        review identified this as a scope deviation in
        CR-CKPT5-MINOR#2); the corrected decorator restricts the
        endpoint to Contributor + Admin only.
        """
        url = "https://www.linkedin.com/in/duplicate-check-viewer"
        path = f"/api/connections/duplicate-check?linkedin_url={url}"
        response = viewer_client.get(path)
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# TestRouteOrdering (Phase 7 critical regression)
# ---------------------------------------------------------------------------


class TestRouteOrdering:
    """The static /duplicate-check route MUST resolve before dynamic /<uuid>.

    Per QA Issue #1 of Checkpoint 2: Phase 7 of the checkpoint
    instructions calls this out as "CRITICAL". The route registration
    order in :mod:`app.api.connections` places the static route first
    so this invariant holds.
    """

    def test_duplicate_check_path_does_not_match_uuid_route(
        self,
        authed_client: Any,
    ) -> None:
        """Hitting /duplicate-check returns 422 (the F-010 handler) NOT 404.

        If Flask had matched the dynamic ``<uuid:record_id>`` route
        first (and the converter coerced the literal text), the
        handler would have raised 404. Receiving 422 (missing
        linkedin_url) confirms the static route is matching.
        """
        response = authed_client.get("/api/connections/duplicate-check")
        assert response.status_code == 422

    def test_url_map_includes_all_eight_endpoints(
        self,
        app: Any,
    ) -> None:
        """Verify every checkpoint-2 endpoint is registered.

        This is the test that would have caught the original
        QA Issue #1 (only POST /api/connections was wired). Any
        future refactor that drops a route will fail this test.
        """
        rules = {
            (
                rule.rule,
                tuple(sorted(rule.methods - {"HEAD", "OPTIONS"})),
            )
            for rule in app.url_map.iter_rules()
        }
        expected_routes = {
            ("/api/connections", ("POST",)),
            ("/api/connections", ("GET",)),
            ("/api/connections/duplicate-check", ("GET",)),
            ("/api/connections/<uuid:record_id>", ("GET",)),
            ("/api/connections/<uuid:record_id>", ("PATCH",)),
            ("/api/connections/<uuid:record_id>", ("DELETE",)),
            ("/api/connections/<uuid:record_id>/history", ("GET",)),
            ("/api/connections/<uuid:record_id>/status", ("PATCH",)),
        }
        missing = expected_routes - rules
        assert not missing, f"Missing routes: {missing}"


# ---------------------------------------------------------------------------
# Tag attachment tests (cross-cuts F-001 and F-008)
# ---------------------------------------------------------------------------


class TestTagAttachment:
    """Tag attachment via the create / edit endpoints."""

    def test_create_with_tag_ids(
        self,
        authed_client: Any,
        contributor_user: Any,
        organization: Any,
    ) -> None:
        """POST with tag_ids attaches the tags."""
        tag_a = TagFactory(organization=organization, name="fintech")
        tag_b = TagFactory(organization=organization, name="enterprise")

        payload = _make_create_payload()
        payload["tag_ids"] = [str(tag_a.id), str(tag_b.id)]

        response = authed_client.post("/api/connections", json=payload)

        assert response.status_code == 201
        body = response.get_json()
        assert {tag["name"] for tag in body["tags"]} == {"fintech", "enterprise"}

    def test_cross_org_tag_id_rejected(
        self,
        authed_client: Any,
        contributor_user: Any,
    ) -> None:
        """Tag id from a different org rejected with 422."""
        other_org = OrganizationFactory()
        other_org_tag = TagFactory(organization=other_org, name="cross-org-tag")

        payload = _make_create_payload()
        payload["tag_ids"] = [str(other_org_tag.id)]

        response = authed_client.post("/api/connections", json=payload)
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# Audit invariant test (referenced by Issue #9)
# ---------------------------------------------------------------------------


class TestAuditInvariantOnAppRole:
    """Verify the audit-immutability invariant against the app role.

    Per QA Issue #9 of Checkpoint 2: when the application role is
    provisioned with default DML privileges (e.g., a setup script
    grants ``ALL ON ALL TABLES`` AFTER migration 0002 ran the
    REVOKE), the F-013 audit-immutability invariant is silently
    violated.

    These tests verify that the application role (named
    ``sales_connections_app``) has ONLY ``SELECT, INSERT`` on the
    ``audit_events`` table and lacks ``UPDATE``, ``DELETE``, and
    ``TRUNCATE``. The tests connect as the app role explicitly so
    they do NOT depend on which role the test session uses by
    default.

    If the app role does not exist (e.g., the test environment was
    not provisioned), the tests SKIP with a clear message rather
    than fail. This matches the existing pattern in
    :mod:`tests.services.test_audit`.
    """

    @pytest.fixture
    def app_role_engine(self, app: Any) -> Any:
        """Build a SQLAlchemy engine connecting AS the app role.

        The app-role credentials default to the values used by
        ``conftest._provision_app_role`` and can be overridden via
        the ``APP_DB_ROLE`` and ``APP_DB_PASSWORD`` environment
        variables (matching the conftest helper).

        Reuses the Flask app's DSN to pick up the host/port/db,
        then swaps the user/password for the app role.
        """
        import os  # noqa: PLC0415

        from sqlalchemy import create_engine  # noqa: PLC0415
        from sqlalchemy.engine.url import make_url  # noqa: PLC0415

        original_url = app.config["DATABASE_URL"]
        parsed = make_url(original_url)
        app_role = os.environ.get("APP_DB_ROLE", "sales_connections_app")
        app_password = os.environ.get("APP_DB_PASSWORD", "sales_connections_app_dev")
        new_url = parsed.set(username=app_role, password=app_password)
        engine = create_engine(new_url, pool_pre_ping=True)
        try:
            yield engine
        finally:
            engine.dispose()

    @pytest.mark.integration
    def test_app_role_cannot_update_audit_events(
        self,
        app_role_engine: Any,
        admin_user: Any,
    ) -> None:
        """As the app role, ``UPDATE audit_events`` raises permission denied.

        This test would have caught QA Issue #9 if it had been in
        place at Checkpoint 2: a CI environment that grants the app
        role default DML privileges AFTER running migration 0002
        leaves the F-013 invariant silently violated. Running this
        test against the live database fails fast.
        """
        from sqlalchemy import text  # noqa: PLC0415
        from sqlalchemy.exc import (  # noqa: PLC0415
            DataError,
            IntegrityError,
            OperationalError,
            ProgrammingError,
        )

        # Seed an audit event via the test session (which is
        # privileged) so the app role has a row to attempt to mutate.
        from app.models.enums import AuditEventType  # noqa: PLC0415
        from app.services.audit import emit_audit_event  # noqa: PLC0415

        with db.session() as session, session.begin():
            audit = emit_audit_event(
                db_session=session,
                event_type=AuditEventType.CREATE,
                actor_user_id=admin_user.id,
            )
            session.flush()
            audit_id = audit.id

        # Connect as the app role and attempt the UPDATE.
        try:
            with app_role_engine.connect() as conn:
                conn.execute(
                    text("UPDATE audit_events SET event_type = 'edit' WHERE id = :audit_id"),
                    {"audit_id": audit_id},
                )
                conn.commit()
        except (
            ProgrammingError,
            IntegrityError,
            DataError,
        ) as exc:
            error_text = str(exc).lower()
            assert (
                "permission" in error_text or "denied" in error_text or "audit_events" in error_text
            )
        except OperationalError as exc:
            # Most likely the role does not exist or the password
            # does not match. Skip with a clear message.
            pytest.skip(
                f"app role connection failed; cannot test app-role audit immutability: {exc}"
            )
        else:
            pytest.fail(
                "App role was permitted to UPDATE audit_events. "
                "F-013 audit-immutability invariant is VIOLATED. "
                "Re-run migration 0002 to re-apply REVOKE on the app role."
            )

    @pytest.mark.integration
    def test_app_role_cannot_delete_audit_events(
        self,
        app_role_engine: Any,
        admin_user: Any,
    ) -> None:
        """As the app role, ``DELETE FROM audit_events`` raises permission denied."""
        from sqlalchemy import text  # noqa: PLC0415
        from sqlalchemy.exc import (  # noqa: PLC0415
            DataError,
            IntegrityError,
            OperationalError,
            ProgrammingError,
        )

        from app.models.enums import AuditEventType  # noqa: PLC0415
        from app.services.audit import emit_audit_event  # noqa: PLC0415

        with db.session() as session, session.begin():
            audit = emit_audit_event(
                db_session=session,
                event_type=AuditEventType.CREATE,
                actor_user_id=admin_user.id,
            )
            session.flush()
            audit_id = audit.id

        try:
            with app_role_engine.connect() as conn:
                conn.execute(
                    text("DELETE FROM audit_events WHERE id = :audit_id"),
                    {"audit_id": audit_id},
                )
                conn.commit()
        except (
            ProgrammingError,
            IntegrityError,
            DataError,
        ) as exc:
            error_text = str(exc).lower()
            assert (
                "permission" in error_text or "denied" in error_text or "audit_events" in error_text
            )
        except OperationalError as exc:
            pytest.skip(
                f"app role connection failed; cannot test app-role audit immutability: {exc}"
            )
        else:
            pytest.fail(
                "App role was permitted to DELETE audit_events. "
                "F-013 audit-immutability invariant is VIOLATED. "
                "Re-run migration 0002 to re-apply REVOKE on the app role."
            )

    @pytest.mark.integration
    def test_app_role_can_insert_audit_events(
        self,
        app_role_engine: Any,
        admin_user: Any,
    ) -> None:
        """As the app role, ``INSERT INTO audit_events`` succeeds.

        Counterpart to the UPDATE/DELETE denial tests: the app role
        MUST retain INSERT so the audit emitter can do its job.
        """
        from datetime import UTC, datetime  # noqa: PLC0415

        from sqlalchemy import text  # noqa: PLC0415
        from sqlalchemy.exc import OperationalError  # noqa: PLC0415

        try:
            with app_role_engine.connect() as conn:
                conn.execute(
                    text(
                        "INSERT INTO audit_events "
                        "(id, actor_user_id, event_type, event_timestamp) "
                        "VALUES (gen_random_uuid(), :actor_id, 'create', :ts)"
                    ),
                    {
                        "actor_id": admin_user.id,
                        "ts": datetime.now(UTC),
                    },
                )
                conn.commit()
        except OperationalError as exc:
            pytest.skip(f"app role connection failed; cannot test INSERT permission: {exc}")
