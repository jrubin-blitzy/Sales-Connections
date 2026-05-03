"""Tests for the F-002 notes generation API (`app.api.notes`).

Covers:
    - POST /api/notes/generate happy path with mocked AI
    - RBAC: Admin/Contributor admitted, Viewer rejected
    - Validation: malformed JSON, missing fields, empty context
    - AI failure modes: timeout (504), service error (502/503)
    - PII: relationship_context not echoed back in error responses
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from app.schemas import NoteGenerationResponse
from app.services.ai_orchestration import AIServiceUnavailableError

if TYPE_CHECKING:
    from flask import Flask
    from flask.testing import FlaskClient
    import pytest


def _make_response(notes: str = "Mocked outreach notes.") -> NoteGenerationResponse:
    """Build a valid NoteGenerationResponse for tests."""
    return NoteGenerationResponse(
        ai_notes=notes,
        model="claude-sonnet-4-5",
        generated_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# TestNotesGenerateHappyPath
# ---------------------------------------------------------------------------


class TestNotesGenerateHappyPath:
    """POST /api/notes/generate happy path."""

    def test_returns_200_with_notes(
        self,
        app: Flask,
        authed_client: FlaskClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Successful AI call returns 200 with ai_notes string."""

        def _fake_generate(payload, **_kwargs: object):
            return _make_response("Reach out via warm intro and mention shared alma mater.")

        monkeypatch.setattr(
            "app.api.notes.generate_outreach_notes", _fake_generate
        )

        response = authed_client.post(
            "/api/notes/generate",
            json={
                "relationship_context": "We went to college together.",
            },
        )
        assert response.status_code == 200
        body = response.get_json()
        assert "ai_notes" in body
        assert "warm intro" in body["ai_notes"]
        assert "model" in body
        assert "generated_at" in body

    def test_admin_can_generate_notes(
        self,
        app: Flask,
        admin_client: FlaskClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Admin role admits to /api/notes/generate."""

        def _fake_generate(payload, **_kwargs: object):
            return _make_response("Admin-generated notes.")

        monkeypatch.setattr(
            "app.api.notes.generate_outreach_notes", _fake_generate
        )

        response = admin_client.post(
            "/api/notes/generate",
            json={
                "relationship_context": "Met at conference.",
            },
        )
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# TestNotesGenerateRBAC
# ---------------------------------------------------------------------------


class TestNotesGenerateRBAC:
    """RBAC enforcement on /api/notes/generate."""

    def test_viewer_role_rejected_with_403(
        self,
        app: Flask,
        viewer_client: FlaskClient,
    ) -> None:
        """Viewer role rejected by the @requires_role(Contributor, Admin) decorator."""
        response = viewer_client.post(
            "/api/notes/generate",
            json={
                "relationship_context": "Test context.",
            },
        )
        assert response.status_code == 403
        body = response.get_json()
        assert body["error"]["code"] == "forbidden"

    def test_unauthenticated_rejected_with_401(
        self,
        app: Flask,
        client: FlaskClient,
    ) -> None:
        """Unauthenticated request rejected by the auth middleware."""
        response = client.post(
            "/api/notes/generate",
            json={
                "relationship_context": "Test context.",
            },
        )
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# TestNotesGenerateValidation
# ---------------------------------------------------------------------------


class TestNotesGenerateValidation:
    """Input validation on /api/notes/generate."""

    def test_malformed_json_returns_422(
        self,
        app: Flask,
        authed_client: FlaskClient,
    ) -> None:
        """Non-JSON body yields 422."""
        response = authed_client.post(
            "/api/notes/generate",
            data="not-json-at-all",
            content_type="application/json",
        )
        assert response.status_code == 422

    def test_missing_relationship_context_returns_422(
        self,
        app: Flask,
        authed_client: FlaskClient,
    ) -> None:
        """Missing relationship_context yields 422."""
        response = authed_client.post(
            "/api/notes/generate",
            json={},
        )
        assert response.status_code == 422

    def test_empty_relationship_context_returns_422(
        self,
        app: Flask,
        authed_client: FlaskClient,
    ) -> None:
        """Empty relationship_context yields 422 (sanitization rejects)."""
        response = authed_client.post(
            "/api/notes/generate",
            json={
                "relationship_context": "",
            },
        )
        # Empty fails Zod-level min length OR sanitization-level emptiness.
        assert response.status_code == 422

    def test_whitespace_only_context_rejected(
        self,
        app: Flask,
        authed_client: FlaskClient,
    ) -> None:
        """Whitespace-only context yields 422 (str_strip_whitespace=True)."""
        response = authed_client.post(
            "/api/notes/generate",
            json={
                "relationship_context": "   ",
            },
        )
        assert response.status_code == 422

    def test_extra_fields_rejected_strict_mode(
        self,
        app: Flask,
        authed_client: FlaskClient,
    ) -> None:
        """Extra fields rejected by strict pydantic config."""
        response = authed_client.post(
            "/api/notes/generate",
            json={
                "relationship_context": "Valid context.",
                "extra_field": "should be rejected",
            },
        )
        # Strict config rejects extra fields with 422.
        assert response.status_code == 422

    def test_validation_error_does_not_echo_context(
        self,
        app: Flask,
        authed_client: FlaskClient,
    ) -> None:
        """Validation error response does NOT echo the relationship_context."""
        secret_marker = "TOPSECRET_PII_MARKER_42_xyz_unique"
        response = authed_client.post(
            "/api/notes/generate",
            json={
                "relationship_context": secret_marker,
                "extra": "force a 422",  # makes it fail
            },
        )
        if response.status_code == 422:
            body_text = response.get_data(as_text=True)
            # PII marker MUST NOT appear in the error response.
            assert secret_marker not in body_text


# ---------------------------------------------------------------------------
# TestNotesGenerateAIFailures
# ---------------------------------------------------------------------------


class TestNotesGenerateAIFailures:
    """AI service failure modes (timeout, error)."""

    def test_ai_timeout_returns_504(
        self,
        app: Flask,
        authed_client: FlaskClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """AI timeout yields 504 with ai_timeout error code."""

        def _fake_timeout(payload, **_kwargs: object):
            raise AIServiceUnavailableError(
                code="ai_timeout",
                message="AI request timed out.",
                status_code=504,
            )

        monkeypatch.setattr(
            "app.api.notes.generate_outreach_notes", _fake_timeout
        )

        response = authed_client.post(
            "/api/notes/generate",
            json={
                "relationship_context": "Met at a conference last week.",
            },
        )
        assert response.status_code == 504
        body = response.get_json()
        assert body["error"]["code"] == "ai_timeout"

    def test_ai_provider_error_returns_502(
        self,
        app: Flask,
        authed_client: FlaskClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """AI provider error yields 502 with ai_unavailable code."""

        def _fake_error(payload, **_kwargs: object):
            raise AIServiceUnavailableError(
                code="ai_unavailable",
                message="Anthropic API error.",
                status_code=502,
            )

        monkeypatch.setattr(
            "app.api.notes.generate_outreach_notes", _fake_error
        )

        response = authed_client.post(
            "/api/notes/generate",
            json={
                "relationship_context": "Test context for provider error.",
            },
        )
        assert response.status_code == 502
        body = response.get_json()
        assert body["error"]["code"] == "ai_unavailable"

    def test_ai_not_configured_returns_503(
        self,
        app: Flask,
        authed_client: FlaskClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """AI service unconfigured yields 503."""

        def _fake_unconfigured(payload, **_kwargs: object):
            raise AIServiceUnavailableError(
                code="ai_not_configured",
                message="AI provider not configured.",
                status_code=503,
            )

        monkeypatch.setattr(
            "app.api.notes.generate_outreach_notes", _fake_unconfigured
        )

        response = authed_client.post(
            "/api/notes/generate",
            json={
                "relationship_context": "Context for unconfigured AI test.",
            },
        )
        assert response.status_code == 503
        body = response.get_json()
        assert body["error"]["code"] == "ai_not_configured"
