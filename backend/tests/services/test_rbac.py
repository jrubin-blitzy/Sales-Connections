"""Service-level integration tests for the F-009 permission matrix.

This file complements ``tests/middleware/test_rbac.py`` (which covers
the ``requires_role`` decorator in isolation) by verifying that the
permission matrix documented in AAP Section 0.7.1 invariant 7 is
actually enforced by the registered API endpoints. Each parametrized
test case fires an HTTP request through the Flask test client with a
session cookie carrying a specific role, and asserts the response
matches the documented expectation.

Permission matrix per AAP Section 0.5.2 Layer 6 / rbac.py docstring:

    POST /api/notes/generate                CONTRIBUTOR or ADMIN
    GET  /api/tags                          any authenticated role
    POST /api/tags                          CONTRIBUTOR or ADMIN
    GET  /api/me                            any authenticated role

Future-checkpoint endpoints (CP4-CP5) are NOT covered here; their
decorators will be tested by the corresponding API test files when
the handlers are created.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from app.models.enums import UserRole

if TYPE_CHECKING:
    from flask import Flask
    from flask.testing import FlaskClient


# ---------------------------------------------------------------------------
# TestNotesGeneratePermissionMatrix
# ---------------------------------------------------------------------------


class TestNotesGeneratePermissionMatrix:
    """POST /api/notes/generate permission matrix (F-002)."""

    def test_admin_can_call_notes_generate(
        self,
        app: Flask,
        admin_client: FlaskClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Admin role admitted to /api/notes/generate."""
        # Patch the imported reference in api.notes to avoid real Anthropic calls.
        from app.schemas import NoteGenerationResponse  # noqa: PLC0415

        def _fake_generate(payload, **kwargs: object):
            return NoteGenerationResponse(notes="Mocked AI notes.")

        monkeypatch.setattr(
            "app.api.notes.generate_outreach_notes", _fake_generate
        )

        response = admin_client.post(
            "/api/notes/generate",
            json={
                "relationship_context": "We met at a conference last year.",
                "full_name": "Jane Doe",
            },
        )
        # The decorator must NOT reject Admin. Status may be 200 or
        # any non-403 value (server-side errors are unrelated).
        assert response.status_code != 403, (
            f"Admin must not be rejected; got {response.status_code}"
        )

    def test_contributor_can_call_notes_generate(
        self,
        app: Flask,
        authed_client: FlaskClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Contributor role admitted to /api/notes/generate."""
        from app.schemas import NoteGenerationResponse  # noqa: PLC0415

        def _fake_generate(payload, **kwargs: object):
            return NoteGenerationResponse(notes="Mocked AI notes.")

        monkeypatch.setattr(
            "app.api.notes.generate_outreach_notes", _fake_generate
        )

        response = authed_client.post(
            "/api/notes/generate",
            json={
                "relationship_context": "We are college classmates.",
                "full_name": "Jane Doe",
            },
        )
        assert response.status_code != 403

    def test_viewer_cannot_call_notes_generate(
        self,
        app: Flask,
        viewer_client: FlaskClient,
    ) -> None:
        """Viewer role REJECTED with 403 by RBAC decorator."""
        response = viewer_client.post(
            "/api/notes/generate",
            json={
                "relationship_context": "We met at a conference.",
                "full_name": "Jane Doe",
            },
        )
        assert response.status_code == 403
        body = response.get_json()
        assert body["error"]["code"] == "forbidden"

    def test_unauthenticated_cannot_call_notes_generate(
        self,
        app: Flask,
        client: FlaskClient,
    ) -> None:
        """No session cookie -> 401 from auth middleware."""
        response = client.post(
            "/api/notes/generate",
            json={
                "relationship_context": "Test context.",
                "full_name": "Jane",
            },
        )
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# TestTagsPermissionMatrix
# ---------------------------------------------------------------------------


class TestTagsPermissionMatrix:
    """GET /api/tags and POST /api/tags permission matrix (F-008)."""

    @pytest.mark.parametrize(
        "role_fixture_name",
        ["admin_client", "authed_client", "viewer_client"],
        ids=["admin", "contributor", "viewer"],
    )
    def test_get_tags_admits_all_authenticated_roles(
        self,
        request: pytest.FixtureRequest,
        app: Flask,
        role_fixture_name: str,
    ) -> None:
        """GET /api/tags admits all 3 authenticated roles."""
        client_for_role: FlaskClient = request.getfixturevalue(role_fixture_name)
        response = client_for_role.get("/api/tags")
        assert response.status_code != 403, (
            f"GET /api/tags must admit role from {role_fixture_name}; "
            f"got {response.status_code}"
        )

    def test_get_tags_rejects_unauthenticated(
        self, app: Flask, client: FlaskClient
    ) -> None:
        """GET /api/tags requires authentication (401 from middleware)."""
        response = client.get("/api/tags")
        assert response.status_code == 401

    def test_admin_can_post_tags(
        self, app: Flask, admin_client: FlaskClient
    ) -> None:
        """Admin admitted to POST /api/tags."""
        response = admin_client.post(
            "/api/tags",
            json={"name": "Industry: Logistics"},
        )
        assert response.status_code != 403

    def test_contributor_can_post_tags(
        self, app: Flask, authed_client: FlaskClient
    ) -> None:
        """Contributor admitted to POST /api/tags."""
        response = authed_client.post(
            "/api/tags",
            json={"name": "Geography: SF Bay Area"},
        )
        assert response.status_code != 403

    def test_viewer_cannot_post_tags(
        self, app: Flask, viewer_client: FlaskClient
    ) -> None:
        """Viewer REJECTED from POST /api/tags."""
        response = viewer_client.post(
            "/api/tags",
            json={"name": "Use Case: Sales"},
        )
        assert response.status_code == 403
        body = response.get_json()
        assert body["error"]["code"] == "forbidden"


# ---------------------------------------------------------------------------
# TestApiMePermissionMatrix
# ---------------------------------------------------------------------------


class TestApiMePermissionMatrix:
    """GET /api/me permission matrix (F-012 session probe)."""

    @pytest.mark.parametrize(
        "role_fixture_name",
        ["admin_client", "authed_client", "viewer_client"],
        ids=["admin", "contributor", "viewer"],
    )
    def test_all_authenticated_roles_can_get_me(
        self,
        request: pytest.FixtureRequest,
        app: Flask,
        role_fixture_name: str,
    ) -> None:
        """All authenticated roles can probe their own session."""
        client_for_role: FlaskClient = request.getfixturevalue(role_fixture_name)
        response = client_for_role.get("/api/me")
        # Must not be 403 (and ideally 200).
        assert response.status_code != 403

    def test_unauthenticated_cannot_get_me(
        self, app: Flask, client: FlaskClient
    ) -> None:
        """Unauthenticated /api/me yields 401 (auth middleware)."""
        response = client.get("/api/me")
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# TestRoleEnumIntegrity
# ---------------------------------------------------------------------------


class TestRoleEnumIntegrity:
    """Verify the UserRole enum matches the documented matrix.

    The permission matrix in the rbac.py docstring and AAP Section
    0.5.2 Layer 6 references three roles. If a fourth role is added,
    these tests will fail and force a documentation update.
    """

    def test_user_role_has_exactly_three_members(self) -> None:
        """UserRole has exactly 3 members per AAP."""
        assert len(list(UserRole)) == 3

    def test_user_role_values_match_documented_matrix(self) -> None:
        """UserRole values match Admin / Contributor / Viewer."""
        values = {role.value for role in UserRole}
        assert values == {"Admin", "Contributor", "Viewer"}

    def test_user_role_admin_constant(self) -> None:
        """UserRole.ADMIN.value == 'Admin'."""
        assert UserRole.ADMIN.value == "Admin"

    def test_user_role_contributor_constant(self) -> None:
        """UserRole.CONTRIBUTOR.value == 'Contributor'."""
        assert UserRole.CONTRIBUTOR.value == "Contributor"

    def test_user_role_viewer_constant(self) -> None:
        """UserRole.VIEWER.value == 'Viewer'."""
        assert UserRole.VIEWER.value == "Viewer"


# ---------------------------------------------------------------------------
# TestServiceLayerOrgScopeEnforcement
# ---------------------------------------------------------------------------


class TestServiceLayerOrgScopeEnforcement:
    """Service-level enforcement of AAP Section 0.7.1 invariant 3 (org-scoping).

    Even when the decorator admits the request, the service layer must
    inject ``WHERE org_id = session.org_id`` so a user in org A cannot
    observe records from org B by guessing UUIDs. This test validates
    the service layer (admin.py) treats cross-org IDs as 404.
    """

    def test_update_user_role_rejects_cross_org_target(
        self,
        app: Flask,
        db_session,
        organization,
        admin_user,
    ) -> None:
        """update_user_role with a cross-org target raises NotFoundError."""
        from app.middleware.error_handlers import NotFoundError  # noqa: PLC0415
        from app.services.admin import update_user_role  # noqa: PLC0415
        from tests.factories import OrganizationFactory, UserFactory  # noqa: PLC0415

        other_org = OrganizationFactory()
        other_user = UserFactory(organization=other_org)

        with db_session.begin(), pytest.raises(NotFoundError):
            update_user_role(
                db_session=db_session,
                org_id=organization.id,
                target_user_id=other_user.id,
                new_role=UserRole.VIEWER,
                actor_user_id=admin_user.id,
            )

    def test_hard_delete_record_rejects_cross_org_record(
        self,
        app: Flask,
        db_session,
        organization,
        admin_user,
    ) -> None:
        """hard_delete_record with a cross-org record raises NotFoundError."""
        from app.middleware.error_handlers import NotFoundError  # noqa: PLC0415
        from app.services.admin import hard_delete_record  # noqa: PLC0415
        from tests.factories import (  # noqa: PLC0415
            OrganizationFactory,
            RecordFactory,
            UserFactory,
        )

        other_org = OrganizationFactory()
        other_user = UserFactory(organization=other_org)
        other_record = RecordFactory(
            organization=other_org, owner=other_user
        )

        with db_session.begin(), pytest.raises(NotFoundError):
            hard_delete_record(
                db_session=db_session,
                org_id=organization.id,
                record_id=other_record.id,
                actor_user_id=admin_user.id,
            )

    def test_find_duplicate_only_returns_records_in_actor_org(
        self,
        app: Flask,
        db_session,
        organization,
        contributor_user,
    ) -> None:
        """find_duplicate restricts results to the supplied org_id."""
        from app.services.duplicate_detection import find_duplicate  # noqa: PLC0415
        from tests.factories import (  # noqa: PLC0415
            OrganizationFactory,
            RecordFactory,
            UserFactory,
        )

        # Create a record in another org with a specific URL.
        other_org = OrganizationFactory()
        other_user = UserFactory(organization=other_org)
        other_url = "https://www.linkedin.com/in/cross-org-target/"
        RecordFactory(
            organization=other_org,
            owner=other_user,
            linkedin_url=other_url,
            normalized_linkedin_url="https://linkedin.com/in/cross-org-target",
        )

        # Search in our org for the same normalized URL.
        result = find_duplicate(
            db_session=db_session,
            org_id=organization.id,
            normalized_url="https://linkedin.com/in/cross-org-target",
        )
        # Cross-org record must NOT match.
        assert result is None
