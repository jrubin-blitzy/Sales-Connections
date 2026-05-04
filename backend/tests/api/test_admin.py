"""API tests for the admin blueprint (F-014).

Covers every endpoint under ``/api/admin/*`` plus the cross-cutting RBAC
matrix (Admin / Contributor / Viewer / Anonymous), org-scope isolation,
anti-lockout invariants, hard-delete cascade behavior, and audit-event
emission for every state-changing operation.

Endpoint catalog
----------------

- ``GET    /api/admin/users``                F-014 list users (Admin only)
- ``PATCH  /api/admin/users/<uuid>``         F-009 + F-014 role mutation
- ``GET    /api/admin/records``              F-014 record moderation
- ``DELETE /api/admin/records/<uuid>``       F-007 + F-014 hard delete
- ``GET    /api/admin/analytics``            F-014 three-panel snapshot

Key invariants verified by this module
--------------------------------------

- Every state-changing endpoint emits an audit event in the same
  transaction as the state change (AAP s 0.7.1 invariant 6).
- ``UserRead`` schema NEVER includes ``password_hash`` (AAP s 0.7.4).
- Cross-org access returns 404 (existence-leak avoidance) per AAP s 0.7.1
  invariant 3.
- The last admin in an organization cannot be demoted (anti-lockout).
- An admin cannot demote themselves (self-demotion guard).
- ``GET /api/admin/records`` defaults to ``include_deleted=True``.
- ``DELETE /api/admin/records/:id`` returns 204 No Content and emits a
  ``hard_delete`` audit event whose ``target_record_id`` is NULL (because
  the parent record is gone) and whose ``before_payload`` is the full
  record snapshot.
- ``GET /api/admin/analytics`` returns a stable shape: 4 entries in
  ``leads_by_status`` (one per OutreachStatus) and exactly 12 entries
  in ``weekly_activity`` even when most weeks are empty.
- Hard-delete cascades to ``record_tags`` association rows but NOT to
  parent ``tags`` rows.

Coordination contract
---------------------

Fixtures inherited from :mod:`tests.conftest`:

- ``client``, ``admin_client``, ``contributor_client``, ``viewer_client``
- ``contributor_user``, ``admin_user``, ``viewer_user``
- ``organization`` (default org)
- ``db_session`` (SAVEPOINT-isolated session, rolls back on test teardown)
- ``audit_assertion`` (helper that asserts an audit event was emitted)
- ``frozen_time`` (wraps freezegun.freeze_time for deterministic dates)

Factory data setup uses :mod:`tests.factories`:

- :class:`AdminUserFactory`, :class:`ContributorUserFactory`,
  :class:`ViewerUserFactory` for role-specific seed users
- :class:`RecordFactory` for active records
- :class:`SoftDeletedRecordFactory` for soft-deleted records
- :class:`TagFactory`, :class:`RecordTagFactory` for tag attachments
- :class:`OrganizationFactory` for cross-org isolation tests
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
import uuid

import pytest
from sqlalchemy import func, select

from app.extensions import db
from app.models import AuditEvent, Record, RecordTag, Tag, User
from app.models.enums import AuditEventType, OutreachStatus, UserRole
from tests.factories import (
    AdminUserFactory,
    ContributorUserFactory,
    OrganizationFactory,
    RecordFactory,
    RecordTagFactory,
    SoftDeletedRecordFactory,
    TagFactory,
    ViewerUserFactory,
)

# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _count_audit_events(event_type: AuditEventType) -> int:
    """Count audit events of a given type using a fresh session.

    Used by the no-op / anti-lockout / self-demotion / 404 negative-path
    tests that must verify ZERO new audit rows were emitted by a failed
    operation. Opens its own session so callers do not need to flush
    the surrounding test session; the SAVEPOINT-isolated factory means
    the new session sees the same transactional state.

    Args:
        event_type: The :class:`AuditEventType` to filter by.

    Returns:
        The count of matching ``audit_events`` rows in the test
        transaction.
    """
    with db.session() as session:
        return int(
            session.execute(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == event_type)
            ).scalar_one()
        )


def _fresh_admin_client_for(app: Any, base_client: Any, target_user: Any) -> Any:
    """Mint a session JWT for ``target_user`` and attach it to ``base_client``.

    Mirrors :func:`tests.conftest._make_authenticated_client` but lives
    in this module so the test bodies can construct authenticated
    clients for users not covered by the standard role fixtures (e.g.,
    a freshly-created admin in a different organization).

    The cookie name is read from the Flask app config to honor any
    custom ``SESSION_COOKIE_NAME`` override; the domain matches the
    Flask test client's default host (``localhost``) so the cookie
    is round-tripped on subsequent requests through the same client.

    Args:
        app: The Flask application supplying the JWT signing key and
            cookie attributes via ``app.config``.
        base_client: An unauthenticated :class:`flask.testing.FlaskClient`
            instance whose cookie jar receives the session JWT.
        target_user: The :class:`User` ORM instance whose claims drive
            the token. Typed as :data:`typing.Any` because factory-boy's
            ``SQLAlchemyModelFactory`` does not propagate its produced
            model class through static type inference; tests pass
            ``AdminUserFactory()``, ``ContributorUserFactory()``, etc.,
            which all return ``User`` rows at runtime even though the
            static return type is the factory class.

    Returns:
        The same ``base_client`` (returned for fluent chaining) with
        the session cookie attached. Subsequent calls overwrite the
        cookie -- the same client may therefore be re-authenticated
        as different users in sequential test phases.
    """
    # Local (function-scope) import keeps the module-level import
    # surface aligned with the file's depends_on_files allow-list while
    # letting tests that need a custom-authenticated client mint a JWT
    # via the production helper. Per AAP s 0.5.3, tests should exercise
    # the SAME JWT mint/verify path that production users carry; using
    # the helper here ensures parity with admin_client / contributor_client
    # / viewer_client constructed by the conftest fixtures.
    from app.services.auth import mint_session_jwt  # noqa: PLC0415

    jwt_token = mint_session_jwt(target_user)
    cookie_name = app.config.get("SESSION_COOKIE_NAME", "session")
    base_client.set_cookie(
        cookie_name,
        jwt_token,
        domain="localhost",
        path="/",
    )
    return base_client


# ---------------------------------------------------------------------------
# TestAdminListUsers -- GET /api/admin/users
# ---------------------------------------------------------------------------


@pytest.mark.rbac
class TestAdminListUsers:
    """``GET /api/admin/users`` lists all users in the actor's org (F-014).

    Verifies the F-014 happy path, the ``password_hash``-NEVER-in-response
    invariant per AAP s 0.7.4, org-scope isolation per AAP s 0.7.1 invariant
    3, deterministic alphabetical ordering for SPA pagination stability,
    and the RBAC matrix (Admin 200 / Contributor 403 / Viewer 403 /
    Anonymous 401).
    """

    def test_admin_lists_all_users_in_their_org(
        self,
        admin_client: Any,
        admin_user: User,
        contributor_user: User,
        viewer_user: User,
    ) -> None:
        """Admin sees every user in their org with the full UserRead shape."""
        response = admin_client.get("/api/admin/users")
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        assert isinstance(body, list)
        # All three seeded users are visible.
        ids = {entry["id"] for entry in body}
        assert str(admin_user.id) in ids
        assert str(contributor_user.id) in ids
        assert str(viewer_user.id) in ids
        # Each entry carries the documented UserRead fields.
        for entry in body:
            assert "id" in entry
            assert "email" in entry
            assert "display_name" in entry
            assert "role" in entry
            assert "created_at" in entry

    def test_password_hash_never_in_response(
        self,
        admin_client: Any,
    ) -> None:
        """``password_hash`` MUST NEVER appear in the response per AAP s 0.7.4."""
        response = admin_client.get("/api/admin/users")
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        assert isinstance(body, list)
        # No entry exposes the bcrypt hash. Defense-in-depth: even the
        # ORG_ID server-only field is also hidden by the schema.
        for entry in body:
            assert "password_hash" not in entry, (
                "UserRead must not expose password_hash to any client, including admins."
            )
            assert "org_id" not in entry, (
                "UserRead must not expose org_id (server-only multi-tenant scope)."
            )

    def test_users_org_scoped(
        self,
        admin_client: Any,
        admin_user: User,
        db_session: Any,
    ) -> None:
        """Cross-org users MUST NOT appear in another org's listing.

        Seeds a Contributor AND a Viewer in the foreign org so the
        assertion exercises both role variants of the cross-org scope
        check; only the calling admin's own org should be visible in
        the response.
        """
        # Create a second organization plus two cross-org users
        # (one Contributor, one Viewer/Sales-Rep) so the assertion
        # demonstrates org isolation across multiple roles.
        other_org = OrganizationFactory()
        cross_org_contributor = ContributorUserFactory(org=other_org)
        cross_org_viewer = ViewerUserFactory(org=other_org)
        db_session.commit()

        response = admin_client.get("/api/admin/users")
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        assert isinstance(body, list)
        ids = {entry["id"] for entry in body}
        assert str(cross_org_contributor.id) not in ids, (
            "Cross-org Contributor must NOT be in the response (AAP s 0.7.1 invariant 3)."
        )
        assert str(cross_org_viewer.id) not in ids, (
            "Cross-org Viewer must NOT be in the response (AAP s 0.7.1 invariant 3)."
        )
        # Sanity: the calling admin IS visible.
        assert str(admin_user.id) in ids

    def test_alphabetical_sort_order(
        self,
        admin_client: Any,
        organization: Any,
        db_session: Any,
    ) -> None:
        """Users are sorted alphabetically by ``display_name`` ascending."""
        # Seed three users with names that span the alphabet so the
        # sort order is visually verifiable in the response.
        ContributorUserFactory(org=organization, display_name="Zebra Engineer")
        ContributorUserFactory(org=organization, display_name="Alpha Tester")
        ContributorUserFactory(org=organization, display_name="Middle User")
        db_session.commit()

        response = admin_client.get("/api/admin/users")
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        names = [entry["display_name"] for entry in body]
        # Sort order must be ascending. We do not assert the exact
        # position of every name (other fixtures seed admin_user /
        # contributor_user / viewer_user with Faker-random names) -- we
        # only assert the three deterministic names appear in the
        # correct relative order.
        idx_alpha = names.index("Alpha Tester")
        idx_middle = names.index("Middle User")
        idx_zebra = names.index("Zebra Engineer")
        assert idx_alpha < idx_middle < idx_zebra, (
            f"Expected ascending order Alpha < Middle < Zebra, got names={names!r}"
        )

    def test_contributor_forbidden(self, contributor_client: Any) -> None:
        """A Contributor receives 403 with ``error.code = 'forbidden'``."""
        response = contributor_client.get("/api/admin/users")
        assert response.status_code == 403, response.get_json()
        assert response.get_json()["error"]["code"] == "forbidden"

    def test_viewer_forbidden(self, viewer_client: Any) -> None:
        """A Viewer receives 403 with ``error.code = 'forbidden'``."""
        response = viewer_client.get("/api/admin/users")
        assert response.status_code == 403, response.get_json()
        assert response.get_json()["error"]["code"] == "forbidden"

    def test_anonymous_unauthorized(self, client: Any) -> None:
        """An unauthenticated request receives 401."""
        response = client.get("/api/admin/users")
        assert response.status_code == 401, response.get_json()
        assert response.get_json()["error"]["code"] == "unauthorized"


# ---------------------------------------------------------------------------
# TestAdminUpdateUserRole -- PATCH /api/admin/users/<uuid:user_id>
# ---------------------------------------------------------------------------


@pytest.mark.rbac
@pytest.mark.audit
class TestAdminUpdateUserRole:
    """``PATCH /api/admin/users/<uuid>`` mutates a user's role (F-009 + F-014).

    Covers:
        - Promotion / demotion happy paths with audit emission.
        - Anti-lockout invariant (sole admin in org cannot be demoted by
          another stale-JWT actor -> HTTP 409, no audit emitted).
        - Self-demotion guard (admin cannot demote themselves -> HTTP 403,
          no audit emitted).
        - No-op idempotent path (no change, no audit).
        - 404 for cross-org and non-existent user IDs (no audit).
        - 422 for invalid roles, malformed bodies, and ``extra='forbid'``
          field rejection.
        - The ``password_hash``-NEVER-in-response invariant on the success
          payload.
        - The RBAC matrix (Contributor/Viewer 403, Anonymous 401).
    """

    def test_admin_can_promote_contributor_to_admin(
        self,
        admin_client: Any,
        admin_user: User,
        contributor_user: User,
        db_session: Any,
        audit_assertion: Any,
    ) -> None:
        """Admin promotes a Contributor to Admin; role change persists; audit emitted."""
        target_id = contributor_user.id
        response = admin_client.patch(
            f"/api/admin/users/{target_id}",
            json={"role": UserRole.ADMIN.value},
        )
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        assert body["id"] == str(target_id)
        assert body["role"] == UserRole.ADMIN.value
        assert "password_hash" not in body
        # Verify the role mutation persisted to the DB.
        db_session.expire_all()
        refreshed = db_session.execute(select(User).where(User.id == target_id)).scalar_one()
        assert refreshed.role == UserRole.ADMIN
        # Verify the audit event was emitted with before/after payloads.
        audit = audit_assertion(
            event_type=AuditEventType.ROLE_CHANGE,
            actor_user_id=admin_user.id,
        )
        assert audit.actor_user_id == admin_user.id
        assert audit.before_payload is not None
        assert audit.after_payload is not None
        assert audit.before_payload["role"] == UserRole.CONTRIBUTOR.value
        assert audit.after_payload["role"] == UserRole.ADMIN.value
        assert audit.before_payload["user_id"] == str(target_id)
        assert audit.after_payload["user_id"] == str(target_id)

    def test_admin_can_demote_other_admin(
        self,
        admin_client: Any,
        admin_user: User,
        organization: Any,
        db_session: Any,
        audit_assertion: Any,
    ) -> None:
        """Admin demotes a peer admin to Contributor; role change persists; audit emitted."""
        # Create a second admin in the same org so demotion does NOT
        # trigger the anti-lockout invariant (after demotion, admin_user
        # remains as the sole admin which is fine).
        peer_admin = AdminUserFactory(org=organization)
        db_session.commit()

        response = admin_client.patch(
            f"/api/admin/users/{peer_admin.id}",
            json={"role": UserRole.CONTRIBUTOR.value},
        )
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        assert body["role"] == UserRole.CONTRIBUTOR.value
        # Verify in DB.
        db_session.expire_all()
        refreshed = db_session.execute(select(User).where(User.id == peer_admin.id)).scalar_one()
        assert refreshed.role == UserRole.CONTRIBUTOR
        # Audit emitted.
        audit = audit_assertion(
            event_type=AuditEventType.ROLE_CHANGE,
            actor_user_id=admin_user.id,
        )
        assert audit.before_payload["role"] == UserRole.ADMIN.value
        assert audit.after_payload["role"] == UserRole.CONTRIBUTOR.value

    def test_anti_lockout_last_admin_cannot_be_demoted(
        self,
        app: Any,
        client: Any,
        admin_user: User,
        organization: Any,
        db_session: Any,
    ) -> None:
        """Demoting the SOLE admin in an org returns 409; no audit emitted.

        Setup: the default org has admin_user as the sole admin
        (other fixture admins demoted via direct DB mutation). A peer
        actor whose JWT still claims Admin role (stale token, role
        mutation does not bump token_version per AAP s 0.7.4) attempts
        to demote admin_user. The service-layer LastAdminError fires
        because admin_user is the only Admin row in the org BEFORE the
        attempted mutation.

        Why a stale JWT and not a self-demotion: the self-demotion
        guard fires BEFORE the last-admin guard in the service layer
        (per the documented order). Reaching LastAdminError via the
        API therefore requires actor != target with only one admin
        in the org; a stale JWT is the cleanest way to satisfy both
        constraints in a test.
        """
        # Step 1: create a peer admin and capture their identity BEFORE
        # demotion so the JWT we mint below carries the Admin role.
        peer_admin = AdminUserFactory(org=organization)
        db_session.commit()

        # Step 2: mint the JWT while peer_admin is still Admin so the
        # token's ``role`` claim is Admin. The auth middleware honors
        # the JWT's role claim; only token_version invalidation flips
        # an already-issued token, and role mutation does NOT bump
        # token_version.
        peer_client = _fresh_admin_client_for(app, client, peer_admin)

        # Step 3: demote peer_admin in the database WITHOUT going
        # through the API so the JWT stays valid. Now admin_user is
        # the SOLE admin row in the org, but peer_admin's JWT still
        # claims Admin role.
        peer_admin.role = UserRole.CONTRIBUTOR
        db_session.commit()

        # Capture audit-event count before the request to assert ZERO
        # new role_change rows are emitted by the rejected attempt.
        baseline_audit_count = _count_audit_events(AuditEventType.ROLE_CHANGE)

        # Step 4: peer_admin (stale JWT) attempts to demote admin_user.
        # Service flow:
        #   - target = admin_user, exists in org -> not 404
        #   - previous Admin -> Contributor -> not no-op
        #   - actor (peer_admin) != target (admin_user) -> not self-demotion
        #   - admin_count = 1 (only admin_user) -> LastAdminError -> 409
        response = peer_client.patch(
            f"/api/admin/users/{admin_user.id}",
            json={"role": UserRole.CONTRIBUTOR.value},
        )
        assert response.status_code == 409, response.get_json()
        assert response.get_json()["error"]["code"] == "conflict"

        # Verify admin_user's role is UNCHANGED.
        db_session.expire_all()
        refreshed = db_session.execute(select(User).where(User.id == admin_user.id)).scalar_one()
        assert refreshed.role == UserRole.ADMIN

        # Verify NO new audit event was emitted.
        assert _count_audit_events(AuditEventType.ROLE_CHANGE) == baseline_audit_count

    def test_self_demotion_blocked(
        self,
        admin_client: Any,
        admin_user: User,
        organization: Any,
        db_session: Any,
    ) -> None:
        """Admin cannot demote themselves; returns 403; no audit emitted."""
        # Create a peer admin so the org would still have an admin
        # after a hypothetical demotion -- this isolates the self-
        # demotion guard from the last-admin guard.
        AdminUserFactory(org=organization)
        db_session.commit()

        baseline_audit_count = _count_audit_events(AuditEventType.ROLE_CHANGE)

        response = admin_client.patch(
            f"/api/admin/users/{admin_user.id}",
            json={"role": UserRole.CONTRIBUTOR.value},
        )
        assert response.status_code == 403, response.get_json()
        error = response.get_json()["error"]
        assert error["code"] == "forbidden"
        # The user-facing message references the self-demotion semantics
        # so the SPA can present a clear error. Match case-insensitively
        # to be tolerant of the exact wording.
        assert "themselves" in error["message"].lower() or "own role" in error["message"].lower()

        # Verify admin_user's role is UNCHANGED.
        db_session.expire_all()
        refreshed = db_session.execute(select(User).where(User.id == admin_user.id)).scalar_one()
        assert refreshed.role == UserRole.ADMIN

        # Verify NO new audit event was emitted.
        assert _count_audit_events(AuditEventType.ROLE_CHANGE) == baseline_audit_count

    def test_no_op_role_update_does_not_emit_audit(
        self,
        admin_client: Any,
        contributor_user: User,
        db_session: Any,
    ) -> None:
        """PATCHing a user with their CURRENT role is a no-op; no audit emitted."""
        baseline_audit_count = _count_audit_events(AuditEventType.ROLE_CHANGE)

        # contributor_user is Contributor; PATCH with role=Contributor.
        response = admin_client.patch(
            f"/api/admin/users/{contributor_user.id}",
            json={"role": UserRole.CONTRIBUTOR.value},
        )
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        assert body["role"] == UserRole.CONTRIBUTOR.value

        # No audit event because the role did not change.
        assert _count_audit_events(AuditEventType.ROLE_CHANGE) == baseline_audit_count

    def test_user_not_in_org_returns_404(
        self,
        admin_client: Any,
        db_session: Any,
    ) -> None:
        """PATCH against a user in another org returns 404 (existence-leak avoidance)."""
        # Create a user in a different org.
        other_org = OrganizationFactory()
        cross_org_user = ContributorUserFactory(org=other_org)
        db_session.commit()

        baseline_audit_count = _count_audit_events(AuditEventType.ROLE_CHANGE)

        response = admin_client.patch(
            f"/api/admin/users/{cross_org_user.id}",
            json={"role": UserRole.ADMIN.value},
        )
        assert response.status_code == 404, response.get_json()
        assert response.get_json()["error"]["code"] == "not_found"

        # Verify the cross-org user's role is UNCHANGED (the operation
        # never reached the service layer past the org-scope check).
        db_session.expire_all()
        refreshed = db_session.execute(
            select(User).where(User.id == cross_org_user.id)
        ).scalar_one()
        assert refreshed.role == UserRole.CONTRIBUTOR

        # Verify NO new audit event was emitted.
        assert _count_audit_events(AuditEventType.ROLE_CHANGE) == baseline_audit_count

    def test_nonexistent_user_returns_404(
        self,
        admin_client: Any,
    ) -> None:
        """PATCH against a non-existent UUID returns 404; no audit emitted."""
        baseline_audit_count = _count_audit_events(AuditEventType.ROLE_CHANGE)

        response = admin_client.patch(
            f"/api/admin/users/{uuid.uuid4()}",
            json={"role": UserRole.ADMIN.value},
        )
        assert response.status_code == 404, response.get_json()
        assert response.get_json()["error"]["code"] == "not_found"

        assert _count_audit_events(AuditEventType.ROLE_CHANGE) == baseline_audit_count

    def test_invalid_role_returns_422(
        self,
        admin_client: Any,
        contributor_user: User,
    ) -> None:
        """An unknown role value is rejected with 422 ``validation_failed``."""
        response = admin_client.patch(
            f"/api/admin/users/{contributor_user.id}",
            json={"role": "RootUser"},
        )
        assert response.status_code == 422, response.get_json()
        assert response.get_json()["error"]["code"] == "validation_failed"

    def test_extra_field_rejected_by_extra_forbid(
        self,
        admin_client: Any,
        contributor_user: User,
    ) -> None:
        """``UserRoleUpdate`` has ``extra='forbid'`` so unexpected fields raise 422."""
        response = admin_client.patch(
            f"/api/admin/users/{contributor_user.id}",
            json={
                "role": UserRole.ADMIN.value,
                "email": "tampered@example.com",
            },
        )
        assert response.status_code == 422, response.get_json()
        body = response.get_json()
        assert body["error"]["code"] == "validation_failed"
        # Verify the response references the offending field. The
        # envelope's ``fields`` array is a list of dicts with ``loc``
        # entries; we look for any entry that references ``email``.
        fields = body["error"].get("fields", [])
        email_referenced = any("email" in (entry.get("loc") or []) for entry in fields)
        assert email_referenced, (
            f"Expected 422 fields to reference the unexpected 'email' key, got fields={fields!r}"
        )

    def test_password_hash_never_in_response(
        self,
        admin_client: Any,
        contributor_user: User,
    ) -> None:
        """The PATCH success response MUST NOT expose ``password_hash``."""
        response = admin_client.patch(
            f"/api/admin/users/{contributor_user.id}",
            json={"role": UserRole.VIEWER.value},
        )
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        assert "password_hash" not in body
        assert "org_id" not in body

    def test_contributor_forbidden(
        self,
        contributor_client: Any,
        contributor_user: User,
    ) -> None:
        """A Contributor receives 403 even with a valid PATCH body."""
        response = contributor_client.patch(
            f"/api/admin/users/{contributor_user.id}",
            json={"role": UserRole.ADMIN.value},
        )
        assert response.status_code == 403, response.get_json()
        assert response.get_json()["error"]["code"] == "forbidden"

    def test_viewer_forbidden(
        self,
        viewer_client: Any,
        contributor_user: User,
    ) -> None:
        """A Viewer receives 403 even with a valid PATCH body."""
        response = viewer_client.patch(
            f"/api/admin/users/{contributor_user.id}",
            json={"role": UserRole.ADMIN.value},
        )
        assert response.status_code == 403, response.get_json()
        assert response.get_json()["error"]["code"] == "forbidden"

    def test_anonymous_unauthorized(
        self,
        client: Any,
        contributor_user: User,
    ) -> None:
        """An unauthenticated request receives 401."""
        response = client.patch(
            f"/api/admin/users/{contributor_user.id}",
            json={"role": UserRole.ADMIN.value},
        )
        assert response.status_code == 401, response.get_json()
        assert response.get_json()["error"]["code"] == "unauthorized"


# ---------------------------------------------------------------------------
# TestAdminListRecords -- GET /api/admin/records
# ---------------------------------------------------------------------------


@pytest.mark.rbac
class TestAdminListRecords:
    """``GET /api/admin/records`` lists records for moderation (F-014).

    Verifies the F-014 happy path: admins see ALL records (including
    soft-deleted) by default per AAP s 0.5.2 Layer 6, can opt out via
    ``?include_deleted=false``, the response carries the
    :class:`PaginatedConnections` envelope (``items`` / ``total`` /
    ``limit`` / ``offset``), filters by full_name and company are
    server-side, pagination via ``limit``/``offset`` is honored, and
    the RBAC matrix.
    """

    def test_admin_lists_all_records_including_soft_deleted_by_default(
        self,
        admin_client: Any,
        organization: Any,
        contributor_user: User,
        db_session: Any,
    ) -> None:
        """``include_deleted`` defaults to True so the moderation tab sees ALL rows."""
        # Create 3 active and 2 soft-deleted records owned by the
        # contributor in the default org.
        RecordFactory.create_batch(3, owner=contributor_user)
        SoftDeletedRecordFactory.create_batch(2, owner=contributor_user)
        db_session.commit()

        response = admin_client.get("/api/admin/records")
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        # The admin endpoint mirrors the public-feed envelope shape.
        assert "items" in body
        assert "total" in body
        # All 5 records visible by default (3 active + 2 soft-deleted).
        assert body["total"] == 5
        assert len(body["items"]) == 5

    def test_admin_can_explicitly_filter_to_active_only(
        self,
        admin_client: Any,
        contributor_user: User,
        db_session: Any,
    ) -> None:
        """``?include_deleted=false`` filters out soft-deleted records."""
        RecordFactory.create_batch(3, owner=contributor_user)
        SoftDeletedRecordFactory.create_batch(2, owner=contributor_user)
        db_session.commit()

        response = admin_client.get("/api/admin/records?include_deleted=false")
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        # Only the 3 active records visible when filter is opted out.
        assert body["total"] == 3
        assert len(body["items"]) == 3
        # Verify none of the returned records are soft-deleted.
        for entry in body["items"]:
            assert entry["deleted_at"] is None

    def test_records_org_scoped(
        self,
        admin_client: Any,
        contributor_user: User,
        db_session: Any,
    ) -> None:
        """Cross-org records MUST NOT appear in the moderation list."""
        # Create records in the default org owned by contributor_user.
        own_records = RecordFactory.create_batch(2, owner=contributor_user)
        # Create records in ANOTHER org with a different owner.
        other_org = OrganizationFactory()
        other_owner = ContributorUserFactory(org=other_org)
        RecordFactory.create_batch(3, owner=other_owner)
        db_session.commit()

        response = admin_client.get("/api/admin/records")
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        # Only the 2 own-org records appear.
        assert body["total"] == 2
        ids = {entry["id"] for entry in body["items"]}
        for record in own_records:
            assert str(record.id) in ids

    def test_pagination(
        self,
        admin_client: Any,
        contributor_user: User,
        db_session: Any,
    ) -> None:
        """``limit``/``offset`` parameters drive pagination boundaries."""
        # Seed 30 records so we can exercise pagination math without
        # crossing the service-layer's max-limit cap (100).
        RecordFactory.create_batch(30, owner=contributor_user)
        db_session.commit()

        # Page 1: limit=10, offset=0 -> 10 items, total=30.
        response = admin_client.get("/api/admin/records?limit=10&offset=0")
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        assert body["total"] == 30
        assert body["limit"] == 10
        assert body["offset"] == 0
        assert len(body["items"]) == 10

        # Page 2: limit=10, offset=10 -> 10 items, total=30.
        response = admin_client.get("/api/admin/records?limit=10&offset=10")
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        assert body["total"] == 30
        assert body["offset"] == 10
        assert len(body["items"]) == 10

        # Page 3: limit=10, offset=20 -> 10 items.
        response = admin_client.get("/api/admin/records?limit=10&offset=20")
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        assert len(body["items"]) == 10

    def test_response_includes_owner_display_name_and_status(
        self,
        admin_client: Any,
        contributor_user: User,
        db_session: Any,
    ) -> None:
        """Each record entry carries the documented moderation fields."""
        RecordFactory.create(owner=contributor_user)
        db_session.commit()

        response = admin_client.get("/api/admin/records")
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        assert len(body["items"]) >= 1
        first = body["items"][0]
        for required_field in (
            "id",
            "full_name",
            "company",
            "owner_display_name",
            "outreach_status",
            "submission_date",
            "deleted_at",
        ):
            assert required_field in first, (
                f"Missing field {required_field!r} in moderation row {first!r}"
            )

    def test_filter_by_full_name_search(
        self,
        admin_client: Any,
        contributor_user: User,
        db_session: Any,
    ) -> None:
        """``?full_name_search=...`` filters by substring match on ``full_name``."""
        # Create two records with deterministic distinguishable names.
        RecordFactory.create(owner=contributor_user, full_name="Alice Wonder")
        RecordFactory.create(owner=contributor_user, full_name="Bob Builder")
        db_session.commit()

        response = admin_client.get("/api/admin/records?full_name_search=Alice")
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        names = [entry["full_name"] for entry in body["items"]]
        assert "Alice Wonder" in names
        assert "Bob Builder" not in names

    def test_filter_by_company_search(
        self,
        admin_client: Any,
        contributor_user: User,
        db_session: Any,
    ) -> None:
        """``?company=...`` filters by substring match on ``company``."""
        # Create two records with deterministic companies.
        RecordFactory.create(owner=contributor_user, company="Acme Corp")
        RecordFactory.create(owner=contributor_user, company="Beta Industries")
        db_session.commit()

        response = admin_client.get("/api/admin/records?company=Acme")
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        companies = [entry["company"] for entry in body["items"]]
        assert "Acme Corp" in companies
        assert "Beta Industries" not in companies

    def test_contributor_forbidden(self, contributor_client: Any) -> None:
        """A Contributor receives 403."""
        response = contributor_client.get("/api/admin/records")
        assert response.status_code == 403, response.get_json()
        assert response.get_json()["error"]["code"] == "forbidden"

    def test_viewer_forbidden(self, viewer_client: Any) -> None:
        """A Viewer receives 403."""
        response = viewer_client.get("/api/admin/records")
        assert response.status_code == 403, response.get_json()
        assert response.get_json()["error"]["code"] == "forbidden"

    def test_anonymous_unauthorized(self, client: Any) -> None:
        """An unauthenticated request receives 401."""
        response = client.get("/api/admin/records")
        assert response.status_code == 401, response.get_json()
        assert response.get_json()["error"]["code"] == "unauthorized"


# ---------------------------------------------------------------------------
# TestAdminHardDeleteRecord -- DELETE /api/admin/records/<uuid:record_id>
# ---------------------------------------------------------------------------


@pytest.mark.rbac
@pytest.mark.audit
class TestAdminHardDeleteRecord:
    """``DELETE /api/admin/records/<uuid>`` physically deletes a record (F-014).

    Verifies the F-007 + F-014 hard-delete contract:
        - HTTP 204 No Content on success (empty body).
        - The Record row is GONE from the database (verified via direct
          DB query returning ``scalar_one_or_none() is None``).
        - The ``hard_delete`` audit event is emitted with
          ``target_record_id IS NULL`` (the parent record is gone, so
          a non-null FK with ``ondelete=RESTRICT`` would conflict) and
          a ``before_payload`` dict carrying the full pre-delete record
          snapshot per AAP s 0.5.2 Layer 6.
        - Cascade deletion to ``record_tags`` association rows (the
          ``ondelete=CASCADE`` on RecordTag.record_id), but NOT to
          parent ``tags`` rows (which persist for reuse).
        - 404 for cross-org and non-existent UUIDs (no audit emitted).
        - 204 on a soft-deleted record (admin can still purge it).
        - The RBAC matrix.

    Note on the ``confirm`` query parameter: per the admin handler's
    defense-in-depth confirmation flag, every DELETE request MUST
    include ``?confirm=true``. Tests that exercise the happy path
    pass it; tests that verify the 404 path also pass it so the 404
    is returned by the service layer (not the confirmation guard).
    """

    def test_admin_hard_deletes_record(
        self,
        admin_client: Any,
        admin_user: User,
        contributor_user: User,
        db_session: Any,
        audit_assertion: Any,
    ) -> None:
        """Admin hard-deletes a record; row gone; full snapshot captured in audit."""
        record = RecordFactory.create(owner=contributor_user, ai_notes="seed AI notes")
        db_session.commit()
        record_id = record.id

        response = admin_client.delete(f"/api/admin/records/{record_id}?confirm=true")
        assert response.status_code == 204, response.data
        # 204 No Content -> response body must be empty.
        assert response.data == b""

        # Verify the Record row is gone.
        with db.session() as session:
            survived = session.execute(
                select(Record).where(Record.id == record_id)
            ).scalar_one_or_none()
            assert survived is None, (
                f"Record {record_id} should be physically deleted but is still in DB."
            )

        # Verify the audit event was emitted with target_record_id IS NULL
        # and a before_payload that contains the full record snapshot.
        # The audit_assertion helper takes a target_record_id keyword
        # but None is the canonical value for hard_delete events.
        audit = audit_assertion(
            event_type=AuditEventType.HARD_DELETE,
            actor_user_id=admin_user.id,
        )
        assert audit.target_record_id is None, (
            "hard_delete audit row MUST have target_record_id IS NULL "
            "(parent record has been deleted; FK with ondelete=RESTRICT "
            "would otherwise conflict)."
        )
        assert audit.actor_user_id == admin_user.id
        # The before_payload captures the full pre-delete record state.
        snapshot = audit.before_payload
        assert isinstance(snapshot, dict)
        assert snapshot["id"] == str(record_id)
        # Verify the documented snapshot fields are all present.
        for required in (
            "id",
            "full_name",
            "linkedin_url",
            "normalized_linkedin_url",
            "company",
            "job_title",
            "relationship_context",
            "ai_notes",
            "involvement",
            "outreach_status",
            "submission_date",
            "deleted_at",
            "org_id",
            "owner_user_id",
            "owner_display_name",
        ):
            assert required in snapshot, (
                f"hard_delete audit before_payload missing required field {required!r}"
            )
        # The captured ai_notes matches the pre-delete value.
        assert snapshot["ai_notes"] == "seed AI notes"

    def test_hard_delete_cascades_to_record_tags(
        self,
        admin_client: Any,
        contributor_user: User,
        organization: Any,
        db_session: Any,
    ) -> None:
        """Hard delete cascades to record_tags but not to parent tags."""
        record = RecordFactory.create(owner=contributor_user)
        # Create three Tag rows in the same org and three association rows.
        tags = [TagFactory(org=organization) for _ in range(3)]
        for tag in tags:
            RecordTagFactory(record=record, tag=tag)
        db_session.commit()
        record_id = record.id
        tag_ids = [tag.id for tag in tags]

        # Sanity: the 3 RecordTag rows exist before the delete.
        with db.session() as session:
            pre_count = session.execute(
                select(func.count()).select_from(RecordTag).where(RecordTag.record_id == record_id)
            ).scalar_one()
            assert pre_count == 3

        response = admin_client.delete(f"/api/admin/records/{record_id}?confirm=true")
        assert response.status_code == 204, response.data

        # Verify the RecordTag association rows are gone (CASCADE).
        with db.session() as session:
            post_assoc = session.execute(
                select(func.count()).select_from(RecordTag).where(RecordTag.record_id == record_id)
            ).scalar_one()
            assert post_assoc == 0, "RecordTag rows must cascade-delete with the parent Record."
            # But the Tag rows themselves PERSIST (they are not children
            # of Record; only the association rows cascade).
            for tag_id in tag_ids:
                tag_row = session.execute(select(Tag).where(Tag.id == tag_id)).scalar_one_or_none()
                assert tag_row is not None, (
                    f"Tag {tag_id} must persist after parent record hard-delete; "
                    "only the RecordTag association cascades."
                )

    def test_hard_delete_record_in_other_org_returns_404(
        self,
        admin_client: Any,
        db_session: Any,
    ) -> None:
        """Cross-org DELETE returns 404; record is NOT deleted; no audit emitted."""
        # Create a record in a different organization.
        other_org = OrganizationFactory()
        other_owner = ContributorUserFactory(org=other_org)
        cross_org_record = RecordFactory.create(owner=other_owner)
        db_session.commit()
        cross_org_record_id = cross_org_record.id

        baseline_audit_count = _count_audit_events(AuditEventType.HARD_DELETE)

        response = admin_client.delete(f"/api/admin/records/{cross_org_record_id}?confirm=true")
        assert response.status_code == 404, response.get_json()
        assert response.get_json()["error"]["code"] == "not_found"

        # Verify the cross-org record is STILL in the database.
        with db.session() as session:
            survived = session.execute(
                select(Record).where(Record.id == cross_org_record_id)
            ).scalar_one_or_none()
            assert survived is not None, (
                "Cross-org record must NOT be deleted; the response must be 404."
            )

        # Verify NO new hard_delete audit event was emitted.
        assert _count_audit_events(AuditEventType.HARD_DELETE) == baseline_audit_count

    def test_hard_delete_nonexistent_record_returns_404(
        self,
        admin_client: Any,
    ) -> None:
        """DELETE against a non-existent UUID returns 404; no audit emitted."""
        baseline_audit_count = _count_audit_events(AuditEventType.HARD_DELETE)

        response = admin_client.delete(f"/api/admin/records/{uuid.uuid4()}?confirm=true")
        assert response.status_code == 404, response.get_json()
        assert response.get_json()["error"]["code"] == "not_found"

        assert _count_audit_events(AuditEventType.HARD_DELETE) == baseline_audit_count

    def test_hard_delete_soft_deleted_record_succeeds(
        self,
        admin_client: Any,
        admin_user: User,
        contributor_user: User,
        db_session: Any,
        audit_assertion: Any,
    ) -> None:
        """Admin can hard-delete an already-soft-deleted record."""
        soft_deleted_record = SoftDeletedRecordFactory.create(owner=contributor_user)
        db_session.commit()
        record_id = soft_deleted_record.id

        response = admin_client.delete(f"/api/admin/records/{record_id}?confirm=true")
        assert response.status_code == 204, response.data

        # Verify the Record row is gone.
        with db.session() as session:
            survived = session.execute(
                select(Record).where(Record.id == record_id)
            ).scalar_one_or_none()
            assert survived is None

        # Verify the hard_delete audit event was emitted; the
        # before_payload's deleted_at should NOT be None for a
        # soft-deleted-then-hard-deleted record.
        audit = audit_assertion(
            event_type=AuditEventType.HARD_DELETE,
            actor_user_id=admin_user.id,
        )
        assert audit.target_record_id is None
        assert audit.before_payload["id"] == str(record_id)
        assert audit.before_payload["deleted_at"] is not None, (
            "before_payload should preserve the soft-delete timestamp from before the hard delete."
        )

    def test_contributor_forbidden(
        self,
        contributor_client: Any,
        contributor_user: User,
        db_session: Any,
    ) -> None:
        """A Contributor receives 403 even with a valid record id."""
        record = RecordFactory.create(owner=contributor_user)
        db_session.commit()

        response = contributor_client.delete(f"/api/admin/records/{record.id}?confirm=true")
        assert response.status_code == 403, response.get_json()
        assert response.get_json()["error"]["code"] == "forbidden"

        # Verify the record is STILL in the database.
        with db.session() as session:
            survived = session.execute(
                select(Record).where(Record.id == record.id)
            ).scalar_one_or_none()
            assert survived is not None

    def test_viewer_forbidden(
        self,
        viewer_client: Any,
        contributor_user: User,
        db_session: Any,
    ) -> None:
        """A Viewer receives 403 even with a valid record id."""
        record = RecordFactory.create(owner=contributor_user)
        db_session.commit()

        response = viewer_client.delete(f"/api/admin/records/{record.id}?confirm=true")
        assert response.status_code == 403, response.get_json()
        assert response.get_json()["error"]["code"] == "forbidden"

        # Verify the record is STILL in the database.
        with db.session() as session:
            survived = session.execute(
                select(Record).where(Record.id == record.id)
            ).scalar_one_or_none()
            assert survived is not None

    def test_anonymous_unauthorized(
        self,
        client: Any,
        contributor_user: User,
        db_session: Any,
    ) -> None:
        """An unauthenticated request receives 401."""
        record = RecordFactory.create(owner=contributor_user)
        db_session.commit()

        response = client.delete(f"/api/admin/records/{record.id}?confirm=true")
        assert response.status_code == 401, response.get_json()
        assert response.get_json()["error"]["code"] == "unauthorized"


# ---------------------------------------------------------------------------
# TestAdminAnalytics -- GET /api/admin/analytics
# ---------------------------------------------------------------------------


@pytest.mark.rbac
class TestAdminAnalytics:
    """``GET /api/admin/analytics`` returns the three-panel snapshot (F-014).

    Verifies:
        - The :class:`AnalyticsResponse` shape: ``most_active_contributors``,
          ``leads_by_status``, ``weekly_activity``, ``generated_at``.
        - ``leads_by_status`` always contains exactly 4 entries (one per
          ``OutreachStatus`` value), in canonical enum order, even for an
          empty org.
        - ``leads_by_status`` counts are correct when records exist.
        - ``most_active_contributors`` orders by ``record_count DESC``
          and excludes soft-deleted records from the count.
        - ``weekly_activity`` always contains exactly 12 entries, one per
          ISO week in the trailing 12-week window, even for empty weeks.
        - ``weekly_activity`` ISO-week buckets are correct against a
          deterministic frozen "today".
        - Org scope: cross-org records do NOT appear in any panel.
        - The RBAC matrix.
    """

    def test_admin_returns_analytics_response_shape(
        self,
        admin_client: Any,
    ) -> None:
        """The response body has the four documented top-level keys."""
        response = admin_client.get("/api/admin/analytics")
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        # The schema spec uses ``most_active_contributors`` (per
        # ``app.schemas.admin.AnalyticsResponse``) over the design-doc
        # alias ``top_contributors``; assert against the actual schema.
        assert "most_active_contributors" in body
        assert "leads_by_status" in body
        assert "weekly_activity" in body
        assert "generated_at" in body

    def test_leads_by_status_includes_all_four_statuses_even_when_zero(
        self,
        admin_client: Any,
    ) -> None:
        """All four OutreachStatus entries are present (zero counts included).

        Order matches the canonical :class:`OutreachStatus` enum sequence:
        Not Started -> In Progress -> Contacted -> Closed.
        """
        # No records seeded; each org-default fixture user has zero
        # records, so all four status counts should be 0.
        response = admin_client.get("/api/admin/analytics")
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        leads = body["leads_by_status"]
        assert len(leads) == 4, (
            f"leads_by_status MUST contain exactly 4 entries (one per "
            f"OutreachStatus); got {len(leads)} entries: {leads!r}"
        )
        # Order matches the OutreachStatus enum sequence.
        expected_statuses = [status.value for status in OutreachStatus]
        actual_statuses = [entry["status"] for entry in leads]
        assert actual_statuses == expected_statuses, (
            f"Expected leads_by_status order {expected_statuses!r}, got {actual_statuses!r}"
        )
        for entry in leads:
            assert entry["count"] == 0

    def test_leads_by_status_counts_correctly(
        self,
        admin_client: Any,
        contributor_user: User,
        db_session: Any,
    ) -> None:
        """Counts reflect the live records grouped by outreach_status."""
        # Seed 5 records: 2 NOT_STARTED, 1 IN_PROGRESS, 0 CONTACTED, 2 CLOSED.
        RecordFactory.create_batch(
            2, owner=contributor_user, outreach_status=OutreachStatus.NOT_STARTED
        )
        RecordFactory.create(owner=contributor_user, outreach_status=OutreachStatus.IN_PROGRESS)
        RecordFactory.create_batch(2, owner=contributor_user, outreach_status=OutreachStatus.CLOSED)
        db_session.commit()

        response = admin_client.get("/api/admin/analytics")
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        leads = body["leads_by_status"]
        # Build a lookup so the assertion is order-tolerant for safety.
        counts_by_status: dict[str, int] = {entry["status"]: entry["count"] for entry in leads}
        assert counts_by_status[OutreachStatus.NOT_STARTED.value] == 2
        assert counts_by_status[OutreachStatus.IN_PROGRESS.value] == 1
        assert counts_by_status[OutreachStatus.CONTACTED.value] == 0
        assert counts_by_status[OutreachStatus.CLOSED.value] == 2

    def test_top_contributors_orders_by_record_count_desc(
        self,
        admin_client: Any,
        organization: Any,
        db_session: Any,
    ) -> None:
        """``most_active_contributors`` is ordered by ``record_count`` desc."""
        # Three users with distinct record counts: U1=3, U2=5, U3=1.
        u1 = ContributorUserFactory(org=organization, display_name="UserOne")
        u2 = ContributorUserFactory(org=organization, display_name="UserTwo")
        u3 = ContributorUserFactory(org=organization, display_name="UserThree")
        RecordFactory.create_batch(3, owner=u1)
        RecordFactory.create_batch(5, owner=u2)
        RecordFactory.create_batch(1, owner=u3)
        db_session.commit()

        response = admin_client.get("/api/admin/analytics")
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        contributors = body["most_active_contributors"]
        # Find the entries for our three test users (the org may have
        # additional contributors from other fixtures).
        by_user_id: dict[str, dict[str, Any]] = {entry["user_id"]: entry for entry in contributors}
        assert str(u1.id) in by_user_id
        assert str(u2.id) in by_user_id
        assert str(u3.id) in by_user_id
        # U2 has the highest record_count.
        assert by_user_id[str(u2.id)]["record_count"] == 5
        assert by_user_id[str(u1.id)]["record_count"] == 3
        assert by_user_id[str(u3.id)]["record_count"] == 1
        # U2's position in the ordered list is BEFORE U1 and U3.
        positions = {entry["user_id"]: idx for idx, entry in enumerate(contributors)}
        assert positions[str(u2.id)] < positions[str(u1.id)]
        assert positions[str(u1.id)] < positions[str(u3.id)]

    def test_top_contributors_excludes_soft_deleted_records(
        self,
        admin_client: Any,
        organization: Any,
        db_session: Any,
    ) -> None:
        """Soft-deleted records are excluded from contributor counts."""
        contributor = ContributorUserFactory(org=organization, display_name="MixedRecordsUser")
        # 3 active records and 2 soft-deleted records owned by the
        # same contributor. The contributor's record_count in the
        # analytics panel should be 3 (active only).
        RecordFactory.create_batch(3, owner=contributor)
        SoftDeletedRecordFactory.create_batch(2, owner=contributor)
        db_session.commit()

        response = admin_client.get("/api/admin/analytics")
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        contributors = body["most_active_contributors"]
        match = next(
            (entry for entry in contributors if entry["user_id"] == str(contributor.id)),
            None,
        )
        assert match is not None, (
            f"Expected contributor {contributor.id} in most_active_contributors; "
            f"got {[entry['user_id'] for entry in contributors]!r}"
        )
        assert match["record_count"] == 3, (
            "Soft-deleted records MUST be excluded from the contributor count "
            "(panel measures active inventory, not historical activity)."
        )

    def test_weekly_activity_returns_12_weeks_including_zero_count(
        self,
        admin_client: Any,
    ) -> None:
        """``weekly_activity`` always contains exactly 12 entries.

        Each entry has a ``week_start`` (date string) and ``record_count``
        (int >= 0). Empty weeks are present with ``record_count = 0``.
        """
        response = admin_client.get("/api/admin/analytics")
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        weekly = body["weekly_activity"]
        assert len(weekly) == 12, (
            f"weekly_activity MUST contain exactly 12 entries; got {len(weekly)}"
        )
        for entry in weekly:
            assert "week_start" in entry
            assert "record_count" in entry
            assert isinstance(entry["record_count"], int)
            assert entry["record_count"] >= 0

    def test_weekly_activity_correct_iso_week_buckets(
        self,
        app: Any,
        client: Any,
        admin_user: User,
        contributor_user: User,
        db_session: Any,
        frozen_time: Any,
    ) -> None:
        """Records seeded inside a frozen time window land in the correct ISO bucket.

        Setup:
            - Freeze "today" at 2026-04-15 (a Wednesday); Monday of that
              ISO week is 2026-04-13.
            - Inside the frozen-time block, seed two records with
              ``submission_date`` set to 2026-04-15.
            - The analytics service computes the trailing 12 weeks
              relative to "today"; the most recent bucket's ``week_start``
              is 2026-04-13.
            - Verify the entry whose ``week_start`` is 2026-04-13 has
              ``record_count == 2``.

        The session JWT is re-minted INSIDE the frozen-time block via
        :func:`_fresh_admin_client_for` so the token's ``iat``/``exp``
        claims are computed against the frozen clock; otherwise the
        ``admin_client`` fixture token would appear "issued in the
        future" relative to the frozen "now" and verification would
        fail with a 401.
        """
        # The "today" anchor: Wednesday 2026-04-15.
        anchor = datetime(2026, 4, 15, 12, 0, 0, tzinfo=UTC)
        # Date in the previous ISO week (offset by ``timedelta(days=7)``)
        # used to seed an additional record in a different bucket so the
        # weekly_activity panel exercises multi-bucket aggregation logic.
        prior_week = anchor - timedelta(days=7)

        with frozen_time(anchor):
            # Re-mint the admin session JWT against the frozen clock so
            # ``iat``/``exp`` align with the frozen "now"; otherwise the
            # auth middleware would reject the token as expired or
            # not-yet-valid (see fixture comment in
            # :func:`_fresh_admin_client_for`).
            frozen_admin_client = _fresh_admin_client_for(app, client, admin_user)

            # Seed two records dated to the anchor day (Wed 2026-04-15).
            # The PostgreSQL ``DATE_TRUNC('week', ...)`` bins this into
            # the Monday-anchored bucket starting 2026-04-13.
            RecordFactory.create(
                owner=contributor_user,
                submission_date=anchor,
                created_at=anchor,
                updated_at=anchor,
            )
            RecordFactory.create(
                owner=contributor_user,
                submission_date=anchor,
                created_at=anchor,
                updated_at=anchor,
            )
            # Seed one record in the prior ISO week to verify the
            # bucket boundary; this lands in the 2026-04-06 bucket.
            RecordFactory.create(
                owner=contributor_user,
                submission_date=prior_week,
                created_at=prior_week,
                updated_at=prior_week,
            )
            db_session.commit()

            response = frozen_admin_client.get("/api/admin/analytics")
            assert response.status_code == 200, response.get_json()
            body = response.get_json()
            weekly = body["weekly_activity"]
            assert len(weekly) == 12

            # Find the entry whose week_start is 2026-04-13 (Monday of
            # the frozen-time anchor week). The serialized form is the
            # ISO date string "2026-04-13".
            target_week = "2026-04-13"
            match = next(
                (entry for entry in weekly if entry["week_start"] == target_week),
                None,
            )
            assert match is not None, (
                f"Expected weekly_activity entry with week_start={target_week!r}; "
                f"got week_starts={[entry['week_start'] for entry in weekly]!r}"
            )
            assert match["record_count"] == 2, (
                f"Expected 2 records in week 2026-04-13; got {match['record_count']}"
            )

            # The record dated to the prior week lives in the 2026-04-06
            # bucket so the aggregation correctly distinguishes ISO weeks.
            prior_match = next(
                (entry for entry in weekly if entry["week_start"] == "2026-04-06"),
                None,
            )
            assert prior_match is not None
            assert prior_match["record_count"] == 1

    def test_analytics_org_scoped(
        self,
        admin_client: Any,
        contributor_user: User,
        db_session: Any,
    ) -> None:
        """Cross-org records MUST NOT appear in any analytics panel."""
        # Seed 1 record in the default org.
        RecordFactory.create(owner=contributor_user, outreach_status=OutreachStatus.IN_PROGRESS)
        # Seed 5 records in OTHER org (none should be counted).
        other_org = OrganizationFactory()
        other_owner = ContributorUserFactory(org=other_org)
        RecordFactory.create_batch(5, owner=other_owner, outreach_status=OutreachStatus.IN_PROGRESS)
        db_session.commit()

        response = admin_client.get("/api/admin/analytics")
        assert response.status_code == 200, response.get_json()
        body = response.get_json()

        # leads_by_status: IN_PROGRESS should be 1, not 6.
        leads = {entry["status"]: entry["count"] for entry in body["leads_by_status"]}
        assert leads[OutreachStatus.IN_PROGRESS.value] == 1, (
            "leads_by_status must scope to actor's org (cross-org records excluded)."
        )

        # most_active_contributors: the cross-org owner MUST NOT appear.
        contributor_ids = {entry["user_id"] for entry in body["most_active_contributors"]}
        assert str(other_owner.id) not in contributor_ids, (
            "Cross-org users MUST NOT appear in most_active_contributors."
        )

    def test_contributor_forbidden(self, contributor_client: Any) -> None:
        """A Contributor receives 403."""
        response = contributor_client.get("/api/admin/analytics")
        assert response.status_code == 403, response.get_json()
        assert response.get_json()["error"]["code"] == "forbidden"

    def test_viewer_forbidden(self, viewer_client: Any) -> None:
        """A Viewer receives 403."""
        response = viewer_client.get("/api/admin/analytics")
        assert response.status_code == 403, response.get_json()
        assert response.get_json()["error"]["code"] == "forbidden"

    def test_anonymous_unauthorized(self, client: Any) -> None:
        """An unauthenticated request receives 401."""
        response = client.get("/api/admin/analytics")
        assert response.status_code == 401, response.get_json()
        assert response.get_json()["error"]["code"] == "unauthorized"


# ---------------------------------------------------------------------------
# TestAdminRBACMatrix -- exhaustive cross-cutting permission verification
# ---------------------------------------------------------------------------


# Hardcoded UUID literals for parametrized RBAC tests so the request
# is rejected by the RBAC decorator BEFORE any service-layer lookup
# fires. Using a non-existent UUID keeps the assertion focused on the
# RBAC gate's behavior rather than confounding it with 404 lookups.
_RBAC_PROBE_UUID: str = "00000000-0000-0000-0000-000000000099"


@pytest.mark.rbac
class TestAdminRBACMatrix:
    """Exhaustive RBAC permission matrix for ALL admin endpoints.

    Per AAP s 0.7.1 invariant 7: 'API-layer authorization is authoritative.
    The frontend's <RoleGate> is a UX courtesy; the backend RBAC decorator
    is the only authoritative gate.'

    This class is a pragmatic redundancy on top of the per-endpoint
    test classes' RBAC tests so a future PR that accidentally drops the
    ``@requires_role`` decorator on any one endpoint surfaces here even
    if the per-endpoint tests are skipped or refactored.

    Parametrization covers all 5 endpoints x 3 non-admin roles
    (Contributor 403, Viewer 403, Anonymous 401):

        - GET    /api/admin/users
        - GET    /api/admin/records
        - GET    /api/admin/analytics
        - PATCH  /api/admin/users/<uuid>
        - DELETE /api/admin/records/<uuid>

    For PATCH the body carries ``{"role": "Admin"}`` so the request is
    rejected by the RBAC decorator BEFORE any pydantic validation can
    run. For DELETE the URL includes ``?confirm=true`` so the request
    is rejected by RBAC before the confirmation guard.
    """

    # Tuple of (HTTP method, URL path) pairs covering all 5 admin
    # endpoints. The path UUIDs are non-existent so the request is
    # rejected by RBAC before any service-layer lookup; this keeps the
    # assertion focused on the RBAC gate.
    _ENDPOINTS: tuple[tuple[str, str], ...] = (
        ("GET", "/api/admin/users"),
        ("GET", "/api/admin/records"),
        ("GET", "/api/admin/analytics"),
        ("PATCH", f"/api/admin/users/{_RBAC_PROBE_UUID}"),
        ("DELETE", f"/api/admin/records/{_RBAC_PROBE_UUID}?confirm=true"),
    )

    @staticmethod
    def _send(test_client: Any, method: str, path: str) -> Any:
        """Issue a request via ``test_client`` matching the parametrized tuple.

        Centralises the per-method dispatch so the parametrized test
        bodies stay focused on the assertion. The PATCH branch passes
        a minimal valid ``UserRoleUpdate`` body so RBAC fires before
        schema validation; the GET / DELETE branches do not need
        bodies.
        """
        if method == "GET":
            return test_client.get(path)
        if method == "PATCH":
            return test_client.patch(path, json={"role": UserRole.ADMIN.value})
        if method == "DELETE":
            return test_client.delete(path)
        raise AssertionError(f"Unsupported method {method!r} in RBAC matrix.")

    @pytest.mark.parametrize(("method", "path"), _ENDPOINTS)
    def test_contributor_forbidden_on_all_endpoints(
        self,
        contributor_client: Any,
        method: str,
        path: str,
    ) -> None:
        """A Contributor receives 403 ``forbidden`` on every admin endpoint."""
        response = self._send(contributor_client, method, path)
        assert response.status_code == 403, (
            f"Expected 403 on {method} {path}; got {response.status_code} {response.get_json()!r}"
        )
        assert response.get_json()["error"]["code"] == "forbidden"

    @pytest.mark.parametrize(("method", "path"), _ENDPOINTS)
    def test_viewer_forbidden_on_all_endpoints(
        self,
        viewer_client: Any,
        method: str,
        path: str,
    ) -> None:
        """A Viewer receives 403 ``forbidden`` on every admin endpoint."""
        response = self._send(viewer_client, method, path)
        assert response.status_code == 403, (
            f"Expected 403 on {method} {path}; got {response.status_code} {response.get_json()!r}"
        )
        assert response.get_json()["error"]["code"] == "forbidden"

    @pytest.mark.parametrize(("method", "path"), _ENDPOINTS)
    def test_anonymous_unauthorized_on_all_endpoints(
        self,
        client: Any,
        method: str,
        path: str,
    ) -> None:
        """An unauthenticated request receives 401 ``unauthorized`` on every admin endpoint."""
        response = self._send(client, method, path)
        assert response.status_code == 401, (
            f"Expected 401 on {method} {path}; got {response.status_code} {response.get_json()!r}"
        )
        assert response.get_json()["error"]["code"] == "unauthorized"
