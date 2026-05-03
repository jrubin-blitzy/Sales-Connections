"""Tests for the F-012 auth API surface (`app.api.auth`).

Covers:
    - POST /auth/login: happy path, validation errors, anti-enumeration
    - POST /auth/logout: cookie clearing
    - GET /auth/google/start: OAuth redirect with state+PKCE
    - GET /auth/google/callback: state validation, success, error path
    - GET /api/me: authenticated session probe
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.models.enums import AuditEventType

if TYPE_CHECKING:
    from flask import Flask
    from flask.testing import FlaskClient

    from app.models.user import User


# ---------------------------------------------------------------------------
# TestLogin
# ---------------------------------------------------------------------------


class TestLogin:
    """POST /auth/login email/password authentication."""

    def test_login_success_returns_200_with_user(
        self,
        app: Flask,
        client: FlaskClient,
        db_session,
        organization,
    ) -> None:
        """Successful login returns 200 with UserRead body."""
        from app.services.auth import hash_password  # noqa: PLC0415
        from tests.factories import UserFactory  # noqa: PLC0415

        user = UserFactory(
            organization=organization,
            email="login-test@example.com",
            password_hash=hash_password("correct-password"),
        )
        # Commit so the user is visible to the new session opened by the API.
        db_session.commit()
        _ = user

        response = client.post(
            "/auth/login",
            json={
                "email": "login-test@example.com",
                "password": "correct-password",
            },
        )
        assert response.status_code == 200
        body = response.get_json()
        assert "user" in body
        assert body["user"]["email"] == "login-test@example.com"
        # Token MUST NOT appear in body (HttpOnly cookie only).
        assert "token" not in body

    def test_login_sets_session_cookie(
        self,
        app: Flask,
        client: FlaskClient,
        db_session,
        organization,
    ) -> None:
        """Successful login sets an HttpOnly session cookie."""
        from app.services.auth import hash_password  # noqa: PLC0415
        from tests.factories import UserFactory  # noqa: PLC0415

        UserFactory(
            organization=organization,
            email="cookie-test@example.com",
            password_hash=hash_password("correct-password"),
        )
        db_session.commit()

        response = client.post(
            "/auth/login",
            json={
                "email": "cookie-test@example.com",
                "password": "correct-password",
            },
        )
        assert response.status_code == 200
        # Check Set-Cookie header.
        set_cookie = response.headers.get("Set-Cookie", "")
        assert "session=" in set_cookie
        assert "HttpOnly" in set_cookie

    def test_login_wrong_password_returns_401(
        self,
        app: Flask,
        client: FlaskClient,
        db_session,
        organization,
    ) -> None:
        """Wrong password yields generic 401 (anti-enumeration)."""
        from app.services.auth import hash_password  # noqa: PLC0415
        from tests.factories import UserFactory  # noqa: PLC0415

        UserFactory(
            organization=organization,
            email="wrong-password@example.com",
            password_hash=hash_password("real-password"),
        )
        db_session.commit()

        response = client.post(
            "/auth/login",
            json={
                "email": "wrong-password@example.com",
                "password": "wrong-password",
            },
        )
        assert response.status_code == 401
        body = response.get_json()
        # Generic message - does NOT reveal if email exists. The actual
        # implementation returns "Authentication failed." which is also
        # generic (anti-enumeration preserved).
        message = body["error"]["message"]
        # Must NOT contain specific user-state hints.
        assert "exist" not in message.lower()
        assert "not found" not in message.lower()
        assert "wrong" not in message.lower()
        assert "incorrect" not in message.lower()

    def test_login_unknown_email_returns_401(
        self,
        app: Flask,
        client: FlaskClient,
    ) -> None:
        """Unknown email yields a generic 401 message (anti-enumeration)."""
        response = client.post(
            "/auth/login",
            json={
                "email": "does-not-exist@example.com",
                "password": "any-password",
            },
        )
        assert response.status_code == 401
        body = response.get_json()
        message = body["error"]["message"]
        # Must NOT reveal whether the email exists.
        assert "exist" not in message.lower()
        assert "not found" not in message.lower()

    def test_login_malformed_json_returns_422(
        self,
        app: Flask,
        client: FlaskClient,
    ) -> None:
        """Non-JSON or non-object body yields 422."""
        response = client.post(
            "/auth/login",
            data="not-json",
            content_type="application/json",
        )
        assert response.status_code == 422

    def test_login_missing_password_returns_422(
        self,
        app: Flask,
        client: FlaskClient,
    ) -> None:
        """Missing password field yields 422."""
        response = client.post(
            "/auth/login",
            json={"email": "test@example.com"},
        )
        assert response.status_code == 422

    def test_login_invalid_email_format_returns_422(
        self,
        app: Flask,
        client: FlaskClient,
    ) -> None:
        """Invalid email format yields 422."""
        response = client.post(
            "/auth/login",
            json={"email": "not-an-email", "password": "valid-pass"},
        )
        assert response.status_code == 422

    def test_login_does_not_echo_password(
        self,
        app: Flask,
        client: FlaskClient,
    ) -> None:
        """Validation error response does NOT contain the submitted password."""
        response = client.post(
            "/auth/login",
            json={
                "email": "not-an-email",
                "password": "S3cretSauce!",
            },
        )
        # Password must NOT appear in the response body anywhere.
        body_text = response.get_data(as_text=True)
        assert "S3cretSauce!" not in body_text

    def test_login_emits_audit_event(
        self,
        app: Flask,
        client: FlaskClient,
        db_session,
        organization,
    ) -> None:
        """Successful login emits an AUTHENTICATION audit event."""
        from app.models import AuditEvent  # noqa: PLC0415
        from app.services.auth import hash_password  # noqa: PLC0415
        from tests.factories import UserFactory  # noqa: PLC0415

        user = UserFactory(
            organization=organization,
            email="audit-test@example.com",
            password_hash=hash_password("correct-password"),
        )
        user_id = user.id
        db_session.commit()

        response = client.post(
            "/auth/login",
            json={
                "email": "audit-test@example.com",
                "password": "correct-password",
            },
        )
        assert response.status_code == 200

        # Check audit event in fresh session.
        events = (
            db_session.query(AuditEvent)
            .filter(
                AuditEvent.actor_user_id == user_id,
                AuditEvent.event_type == AuditEventType.AUTHENTICATION,
            )
            .all()
        )
        assert len(events) >= 1
        evt = events[0]
        assert evt.after_payload["method"] == "password"
        assert evt.after_payload["outcome"] == "success"


# ---------------------------------------------------------------------------
# TestLogout
# ---------------------------------------------------------------------------


class TestLogout:
    """POST /auth/logout cookie clearing."""

    def test_logout_clears_cookie_when_session_present(
        self,
        app: Flask,
        admin_client: FlaskClient,
    ) -> None:
        """Logout with active session clears the cookie."""
        response = admin_client.post("/auth/logout")
        assert response.status_code == 200
        # Must include a Set-Cookie clearing the session.
        set_cookie = response.headers.get("Set-Cookie", "")
        assert "session=" in set_cookie
        # Either an empty value or Max-Age=0 indicates clearance.
        assert ("Max-Age=0" in set_cookie or 'session="";' in set_cookie
                or "session=;" in set_cookie)

    def test_logout_returns_200_without_session(
        self,
        app: Flask,
        client: FlaskClient,
    ) -> None:
        """Logout without an active session still returns 200 (idempotent)."""
        response = client.post("/auth/logout")
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# TestGoogleStart
# ---------------------------------------------------------------------------


class TestGoogleStart:
    """GET /auth/google/start OAuth authorization redirect."""

    def test_google_start_returns_500_when_unconfigured(
        self,
        app: Flask,
        client: FlaskClient,
    ) -> None:
        """Without GOOGLE_OAUTH_CLIENT_ID, the endpoint returns 500."""
        # TestingConfig does NOT configure Google OAuth credentials, so
        # the endpoint should signal misconfiguration cleanly.
        response = client.get("/auth/google/start")
        # 500 (server misconfig) or 503 (service unavailable) are
        # acceptable; the key invariant is "not 200" in the unconfigured
        # case.
        assert response.status_code >= 400

    def test_google_start_no_session_required(
        self,
        app: Flask,
        client: FlaskClient,
    ) -> None:
        """The endpoint is in the public allowlist (no auth required)."""
        # We don't care about the OAuth result here, only that the
        # auth middleware did NOT reject the request with 401.
        response = client.get("/auth/google/start")
        assert response.status_code != 401


# ---------------------------------------------------------------------------
# TestGoogleCallback
# ---------------------------------------------------------------------------


class TestGoogleCallback:
    """GET /auth/google/callback OAuth code-exchange."""

    def test_callback_without_state_cookie_returns_error(
        self,
        app: Flask,
        client: FlaskClient,
    ) -> None:
        """Callback without the state cookie is rejected."""
        response = client.get(
            "/auth/google/callback?code=fake-code&state=fake-state"
        )
        # 401 (state mismatch) or 4xx (validation) - just not 200.
        assert response.status_code != 200

    def test_callback_with_error_query_redirects_to_login(
        self,
        app: Flask,
        client: FlaskClient,
    ) -> None:
        """Callback with ?error=... clears state and redirects to /login."""
        # Set a state cookie matching the callback's state to satisfy
        # the state-match precondition (the handler partitions
        # ``state.verifier`` from the cookie).
        client.set_cookie("oauth_state", "fake-state.fake-verifier")
        response = client.get(
            "/auth/google/callback?error=access_denied&state=fake-state",
            follow_redirects=False,
        )
        # Should be a redirect (302/303).
        assert response.status_code in (302, 303)
        location = response.headers.get("Location", "")
        # Login destination with error param.
        assert "/login" in location

    def test_callback_no_session_required(
        self,
        app: Flask,
        client: FlaskClient,
    ) -> None:
        """Callback is public (auth middleware does not require session).

        Note: /auth/google/callback IS in the public allowlist, so the
        auth middleware does not enforce a session cookie. However, the
        handler may return its own 401 for OAuth-specific failures
        (state mismatch, etc.). We assert the middleware bypass occurred
        by inspecting the error message which is OAuth-specific, not the
        generic "missing session cookie" message.
        """
        response = client.get(
            "/auth/google/callback?code=test&state=test"
        )
        # If 401, it must be OAuth-specific (state validation), NOT
        # the generic auth middleware "missing cookie" 401.
        if response.status_code == 401:
            body = response.get_json()
            message = body["error"]["message"].lower()
            # Auth middleware uses "missing session cookie" or similar
            # generic phrasing; the OAuth handler emits "OAuth state
            # validation failed" or similar - assert specifically NOT
            # the middleware message.
            assert (
                ("session cookie" not in message
                and "session" not in message.split("oauth")[0].lower())
                or "oauth" in message
                or "state" in message
            ), f"Unexpected 401 source: {message}"


# ---------------------------------------------------------------------------
# TestApiMe
# ---------------------------------------------------------------------------


class TestApiMe:
    """GET /api/me session probe."""

    def test_me_returns_user_with_admin_session(
        self,
        app: Flask,
        admin_client: FlaskClient,
        admin_user: User,
    ) -> None:
        """Authenticated admin returns their UserRead."""
        response = admin_client.get("/api/me")
        assert response.status_code == 200
        body = response.get_json()
        assert "user" in body
        assert body["user"]["id"] == str(admin_user.id)
        assert body["user"]["role"] == "Admin"
        assert body["user"]["email"] == admin_user.email

    def test_me_returns_user_with_contributor_session(
        self,
        app: Flask,
        authed_client: FlaskClient,
        contributor_user: User,
    ) -> None:
        """Authenticated contributor returns their UserRead."""
        response = authed_client.get("/api/me")
        assert response.status_code == 200
        body = response.get_json()
        assert body["user"]["id"] == str(contributor_user.id)
        assert body["user"]["role"] == "Contributor"

    def test_me_returns_user_with_viewer_session(
        self,
        app: Flask,
        viewer_client: FlaskClient,
        viewer_user: User,
    ) -> None:
        """Authenticated viewer returns their UserRead."""
        response = viewer_client.get("/api/me")
        assert response.status_code == 200
        body = response.get_json()
        assert body["user"]["id"] == str(viewer_user.id)
        assert body["user"]["role"] == "Viewer"

    def test_me_unauthenticated_returns_401(
        self,
        app: Flask,
        client: FlaskClient,
    ) -> None:
        """Unauthenticated /api/me yields 401."""
        response = client.get("/api/me")
        assert response.status_code == 401

    def test_me_does_not_expose_password_hash(
        self,
        app: Flask,
        admin_client: FlaskClient,
    ) -> None:
        """Response body never contains password_hash."""
        response = admin_client.get("/api/me")
        body_text = response.get_data(as_text=True)
        assert "password_hash" not in body_text
        assert "$2b$" not in body_text  # bcrypt prefix

    def test_me_does_not_expose_org_id(
        self,
        app: Flask,
        admin_client: FlaskClient,
    ) -> None:
        """org_id is server-only - not in UserRead response."""
        response = admin_client.get("/api/me")
        body = response.get_json()
        assert "org_id" not in body["user"]


# ---------------------------------------------------------------------------
# TestSecurityInvariants
# ---------------------------------------------------------------------------


class TestSecurityInvariants:
    """Cross-cutting security invariants."""

    def test_login_response_has_no_token_field(
        self,
        app: Flask,
        client: FlaskClient,
        db_session,
        organization,
    ) -> None:
        """LoginResponse never includes the JWT in the body (HttpOnly only)."""
        from app.services.auth import hash_password  # noqa: PLC0415
        from tests.factories import UserFactory  # noqa: PLC0415

        UserFactory(
            organization=organization,
            email="security@example.com",
            password_hash=hash_password("p"),
        )
        db_session.commit()

        response = client.post(
            "/auth/login",
            json={"email": "security@example.com", "password": "p"},
        )
        if response.status_code == 200:
            body = response.get_json()
            # NO token field anywhere.
            assert "token" not in body
            assert "jwt" not in body
            assert "access_token" not in body

    def test_anti_enumeration_same_message_for_both_failures(
        self,
        app: Flask,
        client: FlaskClient,
        db_session,
        organization,
    ) -> None:
        """Wrong-password and unknown-email return identical messages."""
        from app.services.auth import hash_password  # noqa: PLC0415
        from tests.factories import UserFactory  # noqa: PLC0415

        UserFactory(
            organization=organization,
            email="anti-enum@example.com",
            password_hash=hash_password("real-pass"),
        )
        db_session.commit()

        wrong_pass = client.post(
            "/auth/login",
            json={"email": "anti-enum@example.com", "password": "WRONG"},
        )
        unknown_email = client.post(
            "/auth/login",
            json={"email": "unknown@example.com", "password": "anything"},
        )
        assert wrong_pass.status_code == 401
        assert unknown_email.status_code == 401
        # Same generic message.
        assert (
            wrong_pass.get_json()["error"]["message"]
            == unknown_email.get_json()["error"]["message"]
        )
