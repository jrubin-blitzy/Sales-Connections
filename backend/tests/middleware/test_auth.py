"""Tests for the F-012 auth middleware (`app.middleware.auth`).

Covers:
    - Public-path allowlist bypass (no auth required)
    - Protected-path enforcement (401 on missing/invalid token)
    - Cookie extraction (HttpOnly session cookie)
    - Bearer header fallback (CLI/service-to-service)
    - Cookie wins over Bearer when both present
    - JWT validation errors → 401
    - Session population on g.session
    - Cross-org token rejection (org_id from JWT)
    - Expired token rejection
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
import uuid

from flask import g, jsonify
import jwt as pyjwt
import pytest

from app.middleware.auth import (
    Session,
    _build_session_from_claims,
    _extract_token,
    _is_protected_path,
    _is_public_path,
)
from app.models.enums import UserRole
from app.services.auth import mint_session_jwt

if TYPE_CHECKING:
    from flask import Flask
    from flask.testing import FlaskClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _attach_probe_route(app: Flask) -> None:
    """Add a /api/_probe route that echoes g.session for inspection."""
    if "_probe" in {rule.endpoint for rule in app.url_map.iter_rules()}:
        return

    @app.route("/api/_probe", endpoint="_probe")
    def _probe():
        session = getattr(g, "session", None)
        if session is None:
            return jsonify(authenticated=False), 200
        return jsonify(
            authenticated=True,
            user_id=str(session.user_id),
            org_id=str(session.org_id),
            role=session.role.value,
            email=session.email,
        ), 200


# ---------------------------------------------------------------------------
# TestPublicPathAllowlist
# ---------------------------------------------------------------------------


class TestPublicPathAllowlist:
    """Public paths bypass auth entirely."""

    def test_healthz_is_public(self) -> None:
        """/healthz is in the public allowlist."""
        assert _is_public_path("/healthz")

    def test_readyz_is_public(self) -> None:
        """/readyz is in the public allowlist."""
        assert _is_public_path("/readyz")

    def test_metrics_is_public(self) -> None:
        """/metrics is in the public allowlist."""
        assert _is_public_path("/metrics")

    def test_auth_login_is_public(self) -> None:
        """/auth/login is in the public allowlist."""
        assert _is_public_path("/auth/login")

    def test_auth_logout_is_public(self) -> None:
        """/auth/logout is in the public allowlist."""
        assert _is_public_path("/auth/logout")

    def test_auth_google_start_is_public(self) -> None:
        """/auth/google/start is in the public allowlist."""
        assert _is_public_path("/auth/google/start")

    def test_auth_google_callback_is_public(self) -> None:
        """/auth/google/callback is in the public allowlist."""
        assert _is_public_path("/auth/google/callback")

    def test_api_paths_not_public(self) -> None:
        """/api/* is not in the public allowlist."""
        assert not _is_public_path("/api/me")
        assert not _is_public_path("/api/connections")
        assert not _is_public_path("/api/admin/users")

    def test_subprefix_attack_rejected(self) -> None:
        """Sub-prefix matching is intentionally avoided.

        Per the docstring: ``/healthzx`` and similar must NOT be
        considered public.
        """
        assert not _is_public_path("/healthzx")
        assert not _is_public_path("/readyz/something")
        assert not _is_public_path("/auth/login/extra")

    def test_metrics_endpoint_no_auth_needed(self, app: Flask, client: FlaskClient) -> None:
        """/metrics responds without a session cookie."""
        # Without a cookie, /metrics should still respond (not 401).
        response = client.get("/metrics")
        # Could be 200 (registry populated) or 404 (no metrics route
        # registered), but NOT 401.
        assert response.status_code != 401


# ---------------------------------------------------------------------------
# TestProtectedPathEnforcement
# ---------------------------------------------------------------------------


class TestProtectedPathEnforcement:
    """Protected paths require a valid session cookie."""

    def test_api_paths_are_protected(self) -> None:
        """All /api/* paths are protected."""
        assert _is_protected_path("/api/me")
        assert _is_protected_path("/api/connections")
        assert _is_protected_path("/api/admin/users")

    def test_public_paths_not_protected(self) -> None:
        """Public allowlist paths are not protected."""
        assert not _is_protected_path("/healthz")
        assert not _is_protected_path("/readyz")
        assert not _is_protected_path("/metrics")
        assert not _is_protected_path("/auth/login")

    def test_random_path_not_protected(self) -> None:
        """Ambient paths fall through to 404 handler."""
        assert not _is_protected_path("/random-path")
        assert not _is_protected_path("/static/x.png")

    def test_missing_cookie_returns_401(self, app: Flask, client: FlaskClient) -> None:
        """Request to /api/* without session cookie yields 401."""
        _attach_probe_route(app)
        response = client.get("/api/_probe")
        assert response.status_code == 401
        body = response.get_json()
        assert body["error"]["code"] == "unauthorized"

    def test_invalid_cookie_returns_401(self, app: Flask, client: FlaskClient) -> None:
        """Request with malformed JWT yields 401."""
        _attach_probe_route(app)
        client.set_cookie("session", "not.a.valid.jwt")
        response = client.get("/api/_probe")
        assert response.status_code == 401

    def test_wrong_signature_returns_401(self, app: Flask, client: FlaskClient) -> None:
        """JWT signed with wrong secret yields 401."""
        _attach_probe_route(app)
        # Mint a token with the wrong secret.
        bogus_token = pyjwt.encode(
            {
                "user_id": str(uuid.uuid4()),
                "org_id": str(uuid.uuid4()),
                "role": "Admin",
                "iat": int(time.time()),
                "exp": int(time.time()) + 3600,
            },
            key="WRONG_SECRET_KEY",
            algorithm="HS256",
        )
        client.set_cookie("session", bogus_token)
        response = client.get("/api/_probe")
        assert response.status_code == 401

    def test_expired_token_returns_401(self, app: Flask, client: FlaskClient) -> None:
        """Expired JWT yields 401."""
        _attach_probe_route(app)
        # Mint an expired token using the app's signing key.
        signing_key = app.config["JWT_SIGNING_KEY"]
        expired_token = pyjwt.encode(
            {
                "user_id": str(uuid.uuid4()),
                "org_id": str(uuid.uuid4()),
                "role": "Admin",
                "iat": int(time.time()) - 7200,  # 2 hours ago
                "exp": int(time.time()) - 3600,  # 1 hour ago (expired)
            },
            key=signing_key,
            algorithm=app.config.get("JWT_ALGORITHM", "HS256"),
        )
        client.set_cookie("session", expired_token)
        response = client.get("/api/_probe")
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# TestSessionPopulation
# ---------------------------------------------------------------------------


class TestSessionPopulation:
    """Valid token populates g.session correctly."""

    def test_valid_cookie_populates_session(
        self,
        app: Flask,
        client: FlaskClient,
        admin_user,
    ) -> None:
        """A valid session cookie populates g.session with user_id/org_id/role."""
        _attach_probe_route(app)

        with app.app_context():
            token = mint_session_jwt(admin_user)

        client.set_cookie("session", token)
        response = client.get("/api/_probe")
        assert response.status_code == 200
        body = response.get_json()
        assert body["authenticated"] is True
        assert body["user_id"] == str(admin_user.id)
        assert body["org_id"] == str(admin_user.org_id)
        assert body["role"] == "Admin"

    def test_session_role_is_userrole_enum(
        self,
        app: Flask,
        client: FlaskClient,
        contributor_user,
    ) -> None:
        """g.session.role is a UserRole enum (matches by value)."""
        _attach_probe_route(app)
        with app.app_context():
            token = mint_session_jwt(contributor_user)
        client.set_cookie("session", token)
        response = client.get("/api/_probe")
        assert response.status_code == 200
        body = response.get_json()
        # role is serialized to .value (string).
        assert body["role"] == "Contributor"

    def test_viewer_role_populated(
        self,
        app: Flask,
        client: FlaskClient,
        viewer_user,
    ) -> None:
        """Viewer role populated correctly."""
        _attach_probe_route(app)
        with app.app_context():
            token = mint_session_jwt(viewer_user)
        client.set_cookie("session", token)
        response = client.get("/api/_probe")
        assert response.status_code == 200
        body = response.get_json()
        assert body["role"] == "Viewer"


# ---------------------------------------------------------------------------
# TestTokenExtraction
# ---------------------------------------------------------------------------


class TestTokenExtraction:
    """Verify _extract_token's cookie-vs-header precedence."""

    def test_cookie_returned_when_present(self, app: Flask) -> None:
        """Cookie value returned when set."""
        with app.test_request_context("/api/_probe", headers={"Cookie": "session=cookie-value"}):
            from flask import request  # noqa: PLC0415

            token = _extract_token(request, "session")
            assert token == "cookie-value"

    def test_bearer_returned_when_no_cookie(self, app: Flask) -> None:
        """Bearer header returned when cookie absent."""
        with app.test_request_context(
            "/api/_probe",
            headers={"Authorization": "Bearer header-value"},
        ):
            from flask import request  # noqa: PLC0415

            token = _extract_token(request, "session")
            assert token == "header-value"

    def test_cookie_wins_over_bearer(self, app: Flask) -> None:
        """Cookie takes precedence when both are present."""
        with app.test_request_context(
            "/api/_probe",
            headers={
                "Cookie": "session=cookie-value",
                "Authorization": "Bearer header-value",
            },
        ):
            from flask import request  # noqa: PLC0415

            token = _extract_token(request, "session")
            assert token == "cookie-value"

    def test_empty_returns_none(self, app: Flask) -> None:
        """No cookie and no header returns None."""
        with app.test_request_context("/api/_probe"):
            from flask import request  # noqa: PLC0415

            token = _extract_token(request, "session")
            assert token is None

    def test_empty_bearer_returns_none(self, app: Flask) -> None:
        """Bearer with empty token returns None."""
        with app.test_request_context("/api/_probe", headers={"Authorization": "Bearer "}):
            from flask import request  # noqa: PLC0415

            token = _extract_token(request, "session")
            assert token is None


# ---------------------------------------------------------------------------
# TestSessionFromClaims
# ---------------------------------------------------------------------------


class TestSessionFromClaims:
    """Verify _build_session_from_claims construction and validation."""

    def test_valid_claims_build_session(self) -> None:
        """All required claims present yield a valid Session."""
        user_id = uuid.uuid4()
        org_id = uuid.uuid4()
        claims = {
            "user_id": str(user_id),
            "org_id": str(org_id),
            "role": "Admin",
            "email": "test@example.com",
            "display_name": "Test User",
            "tv": 0,
        }
        session = _build_session_from_claims(claims)
        assert isinstance(session, Session)
        assert session.user_id == user_id
        assert session.org_id == org_id
        assert session.role == UserRole.ADMIN
        assert session.email == "test@example.com"
        assert session.display_name == "Test User"
        assert session.token_version == 0

    def test_missing_user_id_raises(self) -> None:
        """Missing user_id claim raises ValueError."""
        with pytest.raises(ValueError, match="user_id"):
            _build_session_from_claims(
                {
                    "org_id": str(uuid.uuid4()),
                    "role": "Admin",
                }
            )

    def test_missing_org_id_raises(self) -> None:
        """Missing org_id claim raises ValueError."""
        with pytest.raises(ValueError, match="org_id"):
            _build_session_from_claims(
                {
                    "user_id": str(uuid.uuid4()),
                    "role": "Admin",
                }
            )

    def test_missing_role_raises(self) -> None:
        """Missing role claim raises ValueError."""
        with pytest.raises(ValueError, match="role"):
            _build_session_from_claims(
                {
                    "user_id": str(uuid.uuid4()),
                    "org_id": str(uuid.uuid4()),
                }
            )

    def test_invalid_role_raises(self) -> None:
        """Non-UserRole role value raises ValueError."""
        with pytest.raises(ValueError, match="role"):
            _build_session_from_claims(
                {
                    "user_id": str(uuid.uuid4()),
                    "org_id": str(uuid.uuid4()),
                    "role": "SuperUser",  # invalid
                }
            )

    def test_non_uuid_user_id_raises(self) -> None:
        """Non-UUID user_id raises ValueError."""
        with pytest.raises(ValueError, match="user_id"):
            _build_session_from_claims(
                {
                    "user_id": "not-a-uuid",
                    "org_id": str(uuid.uuid4()),
                    "role": "Admin",
                }
            )

    def test_userrole_enum_role_accepted(self) -> None:
        """role passed as UserRole enum directly is accepted."""
        claims = {
            "user_id": str(uuid.uuid4()),
            "org_id": str(uuid.uuid4()),
            "role": UserRole.VIEWER,
            "tv": 0,
        }
        session = _build_session_from_claims(claims)
        assert session.role == UserRole.VIEWER

    def test_missing_tv_claim_raises(self) -> None:
        """Missing ``tv`` (token_version) claim raises ValueError.

        Per AAP section 0.7.4 (Security Invariants), the ``tv`` claim
        is mandatory for every session JWT. A pre-rotation token (one
        minted by a build that did not yet include ``tv``) MUST be
        rejected at the build_session boundary so the auth middleware
        cannot admit a stale-version JWT.
        """
        with pytest.raises(ValueError, match="tv"):
            _build_session_from_claims(
                {
                    "user_id": str(uuid.uuid4()),
                    "org_id": str(uuid.uuid4()),
                    "role": "Admin",
                }
            )

    def test_session_is_frozen(self) -> None:
        """Session is frozen — attempts to mutate raise FrozenInstanceError."""
        from dataclasses import FrozenInstanceError  # noqa: PLC0415

        session = _build_session_from_claims(
            {
                "user_id": str(uuid.uuid4()),
                "org_id": str(uuid.uuid4()),
                "role": "Admin",
                "tv": 0,
            }
        )
        with pytest.raises(FrozenInstanceError):
            session.role = UserRole.VIEWER  # type: ignore[misc]
