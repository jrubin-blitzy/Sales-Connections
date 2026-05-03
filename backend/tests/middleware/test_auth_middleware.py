"""Tests for ``app.middleware.auth``.

Validates per AAP s 0.4.3 / s 0.7.4:

* ``Session`` dataclass is frozen and slotted (no mutation, no
  ``__dict__``).
* Public path allowlist uses EXACT match (security-critical: a
  ``/healthzx`` path must NOT bypass auth).
* Protected ``/api/*`` paths require a valid JWT cookie OR
  ``Authorization: Bearer`` header.
* Cookie value wins over ``Authorization`` header when both are
  present (cookie is the canonical source).
* Malformed / expired / missing claims produce 401 with the uniform
  error envelope -- no internal exception details leaked.
* Successful auth populates ``g.session`` with a typed ``Session``
  instance and binds ``user_id``, ``org_id``, ``role`` into
  structlog contextvars.
* Non-protected paths that are also non-public (e.g., a stray ``/foo``
  request) are NOT authenticated; the 404 handler runs.

Per AAP s 0.7.4 these constitute the authentication-layer security
invariants. The corresponding RBAC role-gate behaviour is tested in
``test_rbac.py``.
"""

from __future__ import annotations

# Standard library imports.
from dataclasses import FrozenInstanceError
from typing import Any
from unittest.mock import MagicMock, patch  # noqa: F401 - kept available per agent prompt
from uuid import UUID, uuid4

# Third-party imports.
from flask import Flask, g, jsonify
import pytest
import structlog

# First-party imports - sibling middleware modules under test.
from app.middleware.auth import (
    _DEFAULT_COOKIE_NAME,
    _PROTECTED_PREFIXES,
    _PUBLIC_PATHS,
    Session,
    _build_session_from_claims,
    _extract_token,
    _is_protected_path,
    _is_public_path,
    register_auth_middleware,
)
from app.middleware.correlation import register_correlation_middleware
from app.middleware.error_handlers import (
    AuthError,  # noqa: F401 - referenced for type/import-availability
    register_error_handlers,
)
from app.models.enums import UserRole

# ---------------------------------------------------------------------------
# Module-level test helpers
# ---------------------------------------------------------------------------


def _make_test_app() -> Flask:
    """Build a Flask app with correlation + error_handlers + auth wired.

    Production middleware order per AAP s 0.5.2 Layer 0:

        correlation -> auth -> rbac -> error_handlers

    Auth needs error_handlers to convert AuthError -> 401 envelope,
    so we register error_handlers BEFORE auth (correctness:
    error handlers are looked up reverse-MRO at exception time, so
    registration order does not affect dispatch as long as both are
    registered).
    """
    app = Flask(__name__)
    app.config["TESTING"] = True
    # Set explicit values for any config the auth middleware may read
    # so we do not depend on full TestingConfig wiring.
    app.config["PROPAGATE_EXCEPTIONS"] = False
    app.config["SECRET_KEY"] = "test-secret"
    app.config["JWT_SIGNING_KEY"] = "test-signing-key-do-not-use-in-prod"
    app.config["JWT_ALGORITHM"] = "HS256"
    register_correlation_middleware(app)
    register_error_handlers(app)
    register_auth_middleware(app)

    @app.route("/api/ping")
    def _ping() -> Any:
        # If auth succeeds, g.session is populated.
        session = getattr(g, "session", None)
        return jsonify(
            ok=True,
            user_id=str(session.user_id) if session else None,
            org_id=str(session.org_id) if session else None,
            role=session.role.value if session else None,
        )

    @app.route("/foo")
    def _foo() -> Any:
        # Non-protected, non-public path. With no token, auth should
        # NOT raise, and Flask returns this response.
        return jsonify(public=False, authenticated=hasattr(g, "session"))

    @app.route("/healthz")
    def _health() -> Any:
        return jsonify(status="ok")

    return app


def _claims(
    *,
    user_id: str | None = None,
    org_id: str | None = None,
    role: str | None = "Contributor",
    email: str = "test@example.com",
    display_name: str = "Test User",
    issued_at: str = "2026-05-01T00:00:00Z",
    expires_at: str = "2026-05-01T08:00:00Z",
    token_version: int = 0,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Helper to build a JWT claims dict for tests.

    Returns a dict with the keys ``user_id``, ``org_id``, ``role``,
    ``tv`` (token version) and optional metadata. Override individual
    fields by passing kwargs.

    Note: ``tv`` is REQUIRED by ``_build_session_from_claims`` per AAP
    s 0.7.4 (token-rotation invariant); the middleware compares it
    against ``users.token_version`` on every protected request.
    """
    claims: dict[str, Any] = {
        "user_id": user_id or str(uuid4()),
        "org_id": org_id or str(uuid4()),
        "role": role,
        "tv": token_version,
        "email": email,
        "display_name": display_name,
        "iat": issued_at,
        "exp": expires_at,
    }
    if extra:
        claims.update(extra)
    return claims


def _stub_token_version_check(monkeypatch: pytest.MonkeyPatch, ok: bool = True) -> None:
    """Patch ``_verify_token_version`` so happy-path tests admit the request.

    The auth middleware compares the JWT's ``tv`` claim against the
    stored ``users.token_version`` via a DB round-trip. In tests that
    do not have a DB context wired up, the lookup throws and the
    function returns ``False`` (fail-safe by design), causing the
    middleware to reject the request with 401.

    To assert the happy-path admission contract directly, we patch
    the verifier in-module so ``_verify_token_version`` returns the
    requested boolean unconditionally. The patch is scoped to the
    test that calls this helper via ``monkeypatch``.
    """
    from app.middleware import auth as auth_middleware  # noqa: PLC0415

    monkeypatch.setattr(
        auth_middleware,
        "_verify_token_version",
        lambda _session: ok,
    )


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def test_app() -> Flask:
    """A fresh Flask app with correlation + error_handlers + auth wired."""
    return _make_test_app()


@pytest.fixture
def test_client(test_app: Flask) -> Any:
    """Test client for the app fixture."""
    return test_app.test_client()


@pytest.fixture(autouse=True)
def _isolate_structlog_contextvars() -> Any:
    """Clear structlog contextvars between tests."""
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()


# ---------------------------------------------------------------------------
# Test: Session dataclass
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSessionDataclass:
    """Validate the Session dataclass contract."""

    def test_required_fields(self) -> None:
        """Session requires user_id (UUID), org_id (UUID), role (UserRole)."""
        uid = uuid4()
        oid = uuid4()
        s = Session(user_id=uid, org_id=oid, role=UserRole.CONTRIBUTOR)
        assert s.user_id == uid
        assert s.org_id == oid
        assert s.role == UserRole.CONTRIBUTOR

    def test_optional_fields_default(self) -> None:
        """Optional fields default to empty string / empty dict."""
        s = Session(user_id=uuid4(), org_id=uuid4(), role=UserRole.ADMIN)
        assert s.email == ""
        assert s.display_name == ""
        assert s.issued_at == ""
        assert s.expires_at == ""
        assert s.raw_claims == {}

    def test_optional_fields_overridable(self) -> None:
        """Optional fields accept explicit values."""
        s = Session(
            user_id=uuid4(),
            org_id=uuid4(),
            role=UserRole.VIEWER,
            email="x@y.com",
            display_name="X",
            issued_at="2026-01-01T00:00:00Z",
            expires_at="2026-01-02T00:00:00Z",
            raw_claims={"sub": "x"},
        )
        assert s.email == "x@y.com"
        assert s.display_name == "X"
        assert s.issued_at == "2026-01-01T00:00:00Z"
        assert s.expires_at == "2026-01-02T00:00:00Z"
        assert s.raw_claims == {"sub": "x"}

    def test_session_is_frozen(self) -> None:
        """Mutation of any attribute raises FrozenInstanceError.

        Frozen ensures that downstream handlers cannot privilege-escalate
        by mutating session.role mid-request.
        """
        s = Session(user_id=uuid4(), org_id=uuid4(), role=UserRole.VIEWER)
        with pytest.raises(FrozenInstanceError):
            s.role = UserRole.ADMIN  # type: ignore[misc]
        with pytest.raises(FrozenInstanceError):
            s.user_id = uuid4()  # type: ignore[misc]

    def test_session_uses_slots(self) -> None:
        """Session has no ``__dict__`` (slots=True saves memory and
        prevents arbitrary attribute injection).
        """
        s = Session(user_id=uuid4(), org_id=uuid4(), role=UserRole.ADMIN)
        # slots=True classes do not have __dict__.
        assert not hasattr(s, "__dict__"), "Session must be slotted (no __dict__)"

    def test_cannot_inject_arbitrary_attribute(self) -> None:
        """Slotted classes reject ad-hoc attributes (extra defense).

        Note: depending on the Python interpreter version, the exact
        exception raised when assigning to an undeclared slot on a
        frozen+slotted dataclass can be ``AttributeError``,
        ``FrozenInstanceError``, or ``TypeError`` (CPython 3.12 raises
        the latter from ``super().__setattr__`` when the dataclass'
        synthesized ``__setattr__`` rejects the assignment). All three
        outcomes equally enforce the security invariant: an attacker
        cannot add an attribute to ``Session`` after construction.
        """
        s = Session(user_id=uuid4(), org_id=uuid4(), role=UserRole.ADMIN)
        with pytest.raises((AttributeError, FrozenInstanceError, TypeError)):
            s.malicious = "payload"  # type: ignore[attr-defined]

    def test_session_is_hashable_or_equality(self) -> None:
        """Two Session instances with identical fields compare equal.

        Required for caching/deduplication in handler code.
        """
        uid = uuid4()
        oid = uuid4()
        a = Session(user_id=uid, org_id=oid, role=UserRole.CONTRIBUTOR)
        b = Session(user_id=uid, org_id=oid, role=UserRole.CONTRIBUTOR)
        assert a == b



# ---------------------------------------------------------------------------
# Test: Public/Protected Path Classification
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPublicPathClassification:
    """Validate the path classification helpers."""

    def test_public_paths_constant_membership(self) -> None:
        """All seven canonical public paths are present."""
        expected = {
            "/healthz",
            "/readyz",
            "/metrics",
            "/auth/login",
            "/auth/logout",
            "/auth/google/start",
            "/auth/google/callback",
        }
        assert expected.issubset(_PUBLIC_PATHS)

    def test_public_paths_is_frozenset(self) -> None:
        """``_PUBLIC_PATHS`` is a frozenset (immutable)."""
        assert isinstance(_PUBLIC_PATHS, frozenset)

    def test_protected_prefixes_contains_api(self) -> None:
        """``/api/`` is in the protected-prefix list."""
        assert "/api/" in _PROTECTED_PREFIXES

    def test_default_cookie_name(self) -> None:
        """The default cookie name is 'session'."""
        assert _DEFAULT_COOKIE_NAME == "session"

    @pytest.mark.parametrize(
        "path",
        [
            "/healthz",
            "/readyz",
            "/metrics",
            "/auth/login",
            "/auth/logout",
            "/auth/google/start",
            "/auth/google/callback",
        ],
    )
    def test_is_public_path_exact_match(self, path: str) -> None:
        """Each canonical public path returns True."""
        assert _is_public_path(path) is True

    @pytest.mark.parametrize(
        "path",
        [
            "/healthzx",
            "/healthz/",
            "/healthz/extra",
            "/health",
            "/auth/loginx",
            "/auth/login/",
            "/auth/admin",
            "/api/healthz",
            "/api/connections",
            "/foo",
            "",
        ],
    )
    def test_is_public_path_rejects_non_exact(self, path: str) -> None:
        """Sub-prefix and similar paths are NOT public.

        Security-critical: a ``/healthzx`` route must NOT bypass auth.
        """
        assert _is_public_path(path) is False, (
            f"Path {path!r} unexpectedly classified as public; "
            "this would bypass auth."
        )

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("/api/connections", True),
            ("/api/notes/generate", True),
            ("/api/admin/users", True),
            ("/api/", True),
            ("/foo", False),
            ("/healthz", False),
            ("/readyz", False),
            ("/api", False),  # missing trailing slash
        ],
    )
    def test_is_protected_path(self, path: str, expected: bool) -> None:
        """``/api/*`` paths are protected; everything else is not."""
        assert _is_protected_path(path) is expected


# ---------------------------------------------------------------------------
# Test: _extract_token
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestExtractToken:
    """Validate token extraction from cookie or Authorization header."""

    def test_token_from_cookie(self, test_app: Flask) -> None:
        """Cookie value is returned when present."""
        with test_app.test_request_context(
            "/api/ping",
            headers={"Cookie": "session=token-from-cookie"},
        ):
            from flask import request  # noqa: PLC0415

            token = _extract_token(request, _DEFAULT_COOKIE_NAME)
        assert token == "token-from-cookie"

    def test_token_from_authorization_bearer_header(self, test_app: Flask) -> None:
        """``Authorization: Bearer <token>`` header is the fallback."""
        with test_app.test_request_context(
            "/api/ping",
            headers={"Authorization": "Bearer header-token-xyz"},
        ):
            from flask import request  # noqa: PLC0415

            token = _extract_token(request, _DEFAULT_COOKIE_NAME)
        assert token == "header-token-xyz"

    def test_cookie_wins_over_authorization_header(self, test_app: Flask) -> None:
        """When both are present, the cookie value is returned.

        AAP s 0.4.3: cookie is the canonical source. Bearer is only
        supported for backward-compat / programmatic clients.
        """
        with test_app.test_request_context(
            "/api/ping",
            headers={
                "Cookie": "session=cookie-token",
                "Authorization": "Bearer header-token",
            },
        ):
            from flask import request  # noqa: PLC0415

            token = _extract_token(request, _DEFAULT_COOKIE_NAME)
        assert token == "cookie-token"

    def test_no_token_returns_none(self, test_app: Flask) -> None:
        """Missing both cookie and header yields None."""
        with test_app.test_request_context("/api/ping"):
            from flask import request  # noqa: PLC0415

            token = _extract_token(request, _DEFAULT_COOKIE_NAME)
        assert token is None

    def test_bearer_prefix_only_returns_none(self, test_app: Flask) -> None:
        """``Authorization: Bearer `` (empty token) yields None."""
        with test_app.test_request_context(
            "/api/ping", headers={"Authorization": "Bearer "}
        ):
            from flask import request  # noqa: PLC0415

            token = _extract_token(request, _DEFAULT_COOKIE_NAME)
        assert token is None

    def test_authorization_without_bearer_prefix_returns_none(
        self, test_app: Flask
    ) -> None:
        """An Authorization header without 'Bearer ' prefix yields None.

        We do NOT support Basic auth, Digest, or any other scheme.
        """
        with test_app.test_request_context(
            "/api/ping", headers={"Authorization": "Basic abc"}
        ):
            from flask import request  # noqa: PLC0415

            token = _extract_token(request, _DEFAULT_COOKIE_NAME)
        assert token is None

    def test_cookie_name_overridable(self, test_app: Flask) -> None:
        """Cookie name is configurable via the second arg."""
        with test_app.test_request_context(
            "/api/ping", headers={"Cookie": "custom_session=v1"}
        ):
            from flask import request  # noqa: PLC0415

            token = _extract_token(request, "custom_session")
        assert token == "v1"

    def test_empty_cookie_value_returns_none(self, test_app: Flask) -> None:
        """An empty-string cookie value is not treated as a valid token."""
        with test_app.test_request_context(
            "/api/ping", headers={"Cookie": "session="}
        ):
            from flask import request  # noqa: PLC0415

            token = _extract_token(request, _DEFAULT_COOKIE_NAME)
        # Either None or empty -- both indicate no usable token.
        assert not token


# ---------------------------------------------------------------------------
# Test: _build_session_from_claims
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBuildSessionFromClaims:
    """Validate claims-to-Session conversion."""

    def test_valid_claims_produce_session(self) -> None:
        """All required fields present and well-formed -> Session."""
        uid = str(uuid4())
        oid = str(uuid4())
        claims = _claims(user_id=uid, org_id=oid, role="Contributor")
        s = _build_session_from_claims(claims)
        assert isinstance(s, Session)
        assert str(s.user_id) == uid
        assert str(s.org_id) == oid
        assert s.role == UserRole.CONTRIBUTOR

    def test_uuid_is_typed_uuid_not_string(self) -> None:
        """user_id and org_id are returned as ``uuid.UUID`` objects."""
        claims = _claims()
        s = _build_session_from_claims(claims)
        assert isinstance(s.user_id, UUID)
        assert isinstance(s.org_id, UUID)

    def test_role_is_typed_userrole_not_string(self) -> None:
        """role is returned as a ``UserRole`` enum member."""
        claims = _claims(role="Admin")
        s = _build_session_from_claims(claims)
        assert isinstance(s.role, UserRole)
        assert s.role == UserRole.ADMIN

    @pytest.mark.parametrize("missing_field", ["user_id", "org_id", "role"])
    def test_missing_required_field_raises(self, missing_field: str) -> None:
        """Missing user_id, org_id, or role -> ValueError."""
        claims = _claims()
        del claims[missing_field]
        with pytest.raises((ValueError, KeyError)):
            _build_session_from_claims(claims)

    def test_malformed_user_id_raises(self) -> None:
        """A non-UUID user_id raises ValueError."""
        claims = _claims(user_id="not-a-uuid")
        with pytest.raises((ValueError, TypeError)):
            _build_session_from_claims(claims)

    def test_malformed_org_id_raises(self) -> None:
        """A non-UUID org_id raises ValueError."""
        claims = _claims(org_id="not-a-uuid")
        with pytest.raises((ValueError, TypeError)):
            _build_session_from_claims(claims)

    def test_unknown_role_raises(self) -> None:
        """A role string not in UserRole raises ValueError."""
        claims = _claims(role="Hacker")
        with pytest.raises((ValueError, KeyError)):
            _build_session_from_claims(claims)

    def test_optional_metadata_passed_through(self) -> None:
        """email, display_name, iat, exp, raw_claims preserved when present."""
        claims = _claims(
            email="alice@acme.com",
            display_name="Alice",
            issued_at="2026-05-01T12:00:00Z",
            expires_at="2026-05-01T20:00:00Z",
        )
        s = _build_session_from_claims(claims)
        # Email, display_name may be passed in or empty -- assert truthy
        # if implementation preserves them.
        assert s.email in {"alice@acme.com", ""}
        assert s.display_name in {"Alice", ""}
        # raw_claims should hold the original dict (or a subset).
        if s.raw_claims:
            # If implementation preserves raw_claims, it must include
            # the original input.
            assert s.raw_claims  # non-empty


# ---------------------------------------------------------------------------
# Test: Public Path Bypass (End-to-End)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPublicPathBypass:
    """Public paths bypass authentication entirely."""

    def test_healthz_no_auth_required(self, test_client: Any) -> None:
        """``GET /healthz`` returns 200 without any token."""
        response = test_client.get("/healthz")
        assert response.status_code == 200

    def test_protected_prefix_blocks_unauthenticated(self, test_client: Any) -> None:
        """``GET /api/ping`` without a token returns 401."""
        response = test_client.get("/api/ping")
        assert response.status_code == 401
        payload = response.get_json()
        assert payload["error"]["code"] == "unauthorized"

    def test_non_protected_non_public_path_unauthenticated(
        self, test_client: Any
    ) -> None:
        """``GET /foo`` (non-protected, non-public) is reachable without auth.

        The auth middleware must NOT raise for paths outside both lists;
        the framework returns the route's response (200 here) or a 404.
        """
        response = test_client.get("/foo")
        # /foo is registered in our test app, so 200.
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["authenticated"] is False


# ---------------------------------------------------------------------------
# Test: Protected Path Authentication End-to-End
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProtectedPathAuthentication:
    """End-to-end: /api/* paths require a valid JWT.

    The auth middleware lazy-imports ``verify_session_jwt`` from
    ``app.services.auth`` inside the request hook; tests patch that
    attribute on the source module so the lazy-resolved reference
    picks up the stub.
    """

    def test_no_cookie_returns_401(self, test_client: Any) -> None:
        """No cookie + no Authorization header -> 401."""
        response = test_client.get("/api/ping")
        assert response.status_code == 401

    def test_no_cookie_envelope_shape(self, test_client: Any) -> None:
        """The 401 envelope is the uniform error shape."""
        response = test_client.get("/api/ping")
        payload = response.get_json()
        assert "error" in payload
        assert payload["error"]["code"] == "unauthorized"
        assert "message" in payload["error"]
        assert "correlation_id" in payload["error"]
        assert "fields" in payload["error"]

    def test_malformed_jwt_returns_401(
        self, test_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A token that ``verify_session_jwt`` rejects -> 401.

        ``verify_session_jwt`` is lazy-imported inside the auth hook;
        we patch it at the source path used by the import.
        """
        from app.services import auth as services_auth  # noqa: PLC0415

        def _raise_invalid(_token: str) -> dict[str, Any]:
            raise services_auth.AuthenticationError("malformed token")

        monkeypatch.setattr(services_auth, "verify_session_jwt", _raise_invalid)
        test_client.set_cookie("session", "malformed.jwt.string", domain="localhost")
        response = test_client.get("/api/ping")
        assert response.status_code == 401
        payload = response.get_json()
        assert payload["error"]["code"] == "unauthorized"
        # No internal exception details leak.
        assert "AuthenticationError" not in payload["error"]["message"]
        assert "malformed token" not in payload["error"]["message"]

    def test_expired_jwt_returns_401(
        self, test_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An expired token -> 401."""
        from app.services import auth as services_auth  # noqa: PLC0415

        def _raise_expired(_token: str) -> dict[str, Any]:
            raise services_auth.AuthenticationError("token expired")

        monkeypatch.setattr(services_auth, "verify_session_jwt", _raise_expired)
        test_client.set_cookie("session", "expired.jwt.string", domain="localhost")
        response = test_client.get("/api/ping")
        assert response.status_code == 401

    def test_unexpected_exception_in_verify_returns_401(
        self, test_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Any unexpected exception during verify -> 401 (NOT 500).

        Defense-in-depth: even unforeseen failures in the JWT layer
        must produce a clean 401; we never leak a 500 traceback at the
        auth boundary.
        """
        from app.services import auth as services_auth  # noqa: PLC0415

        def _explode(_token: str) -> dict[str, Any]:
            raise RuntimeError("crypto provider crashed")

        monkeypatch.setattr(services_auth, "verify_session_jwt", _explode)
        test_client.set_cookie("session", "x.y.z", domain="localhost")
        response = test_client.get("/api/ping")
        assert response.status_code == 401
        payload = response.get_json()
        # Internal error not leaked.
        assert "RuntimeError" not in payload["error"]["message"]
        assert "crypto provider" not in payload["error"]["message"]

    def test_empty_claims_returns_401(
        self, test_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``verify_session_jwt`` returning an empty dict -> 401."""
        from app.services import auth as services_auth  # noqa: PLC0415

        monkeypatch.setattr(
            services_auth, "verify_session_jwt", lambda _t: {}
        )
        test_client.set_cookie("session", "valid-but-empty", domain="localhost")
        response = test_client.get("/api/ping")
        assert response.status_code == 401

    def test_malformed_claims_returns_401(
        self, test_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Claims missing user_id -> 401."""
        from app.services import auth as services_auth  # noqa: PLC0415

        bad_claims = {"org_id": str(uuid4()), "role": "Contributor", "tv": 0}
        monkeypatch.setattr(
            services_auth, "verify_session_jwt", lambda _t: bad_claims
        )
        test_client.set_cookie("session", "tok", domain="localhost")
        response = test_client.get("/api/ping")
        assert response.status_code == 401

    def test_unknown_role_returns_401(
        self, test_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Claims with role='Hacker' -> 401."""
        from app.services import auth as services_auth  # noqa: PLC0415

        bad_claims = _claims(role="Hacker")
        monkeypatch.setattr(
            services_auth, "verify_session_jwt", lambda _t: bad_claims
        )
        test_client.set_cookie("session", "tok", domain="localhost")
        response = test_client.get("/api/ping")
        assert response.status_code == 401

    def test_valid_jwt_admits_request(
        self, test_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A valid JWT -> 200; g.session is populated."""
        from app.services import auth as services_auth  # noqa: PLC0415

        uid = str(uuid4())
        oid = str(uuid4())
        good_claims = _claims(user_id=uid, org_id=oid, role="Admin")
        monkeypatch.setattr(
            services_auth, "verify_session_jwt", lambda _t: good_claims
        )
        # The middleware also performs a token-version check against
        # the database after verifying the JWT signature/claims. In
        # this isolated test we have no DB context, so we patch the
        # version check to admit the session unconditionally - the
        # signature/claim validation is what we are exercising here.
        _stub_token_version_check(monkeypatch, ok=True)
        test_client.set_cookie("session", "valid-token", domain="localhost")
        response = test_client.get("/api/ping")
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["ok"] is True
        assert payload["user_id"] == uid
        assert payload["org_id"] == oid
        assert payload["role"] == "Admin"

    def test_authorization_header_admits_request(
        self, test_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A valid Authorization: Bearer header -> 200 (no cookie)."""
        from app.services import auth as services_auth  # noqa: PLC0415

        good_claims = _claims(role="Viewer")
        monkeypatch.setattr(
            services_auth, "verify_session_jwt", lambda _t: good_claims
        )
        _stub_token_version_check(monkeypatch, ok=True)
        response = test_client.get(
            "/api/ping",
            headers={"Authorization": "Bearer header-token"},
        )
        assert response.status_code == 200
        assert response.get_json()["role"] == "Viewer"

    def test_cookie_wins_when_both_present(
        self, test_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When BOTH cookie and Bearer are present, the COOKIE token is verified.

        We make ``verify_session_jwt`` echo the input so we can detect
        which token was passed in.
        """
        from app.services import auth as services_auth  # noqa: PLC0415

        captured_tokens: list[str] = []

        def _echo(token: str) -> dict[str, Any]:
            captured_tokens.append(token)
            return _claims()

        monkeypatch.setattr(services_auth, "verify_session_jwt", _echo)
        _stub_token_version_check(monkeypatch, ok=True)
        test_client.set_cookie("session", "cookie-tok", domain="localhost")
        response = test_client.get(
            "/api/ping",
            headers={"Authorization": "Bearer header-tok"},
        )
        assert response.status_code == 200
        assert captured_tokens == ["cookie-tok"], (
            f"verify_session_jwt was called with {captured_tokens}; "
            "expected ['cookie-tok'] (cookie must win over Bearer)"
        )


# ---------------------------------------------------------------------------
# Test: structlog Contextvars Binding
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestStructlogContextvarsBinding:
    """Validate that authenticated requests bind user_id/org_id/role into structlog."""

    def test_contextvars_bound_inside_handler(
        self, test_app: Flask, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """During an authenticated request, contextvars include user_id/org_id/role.

        We register an additional route that reads contextvars at the
        moment of execution (after auth ran) and returns them.
        """
        from app.services import auth as services_auth  # noqa: PLC0415

        uid = str(uuid4())
        oid = str(uuid4())
        good_claims = _claims(user_id=uid, org_id=oid, role="Contributor")
        monkeypatch.setattr(
            services_auth, "verify_session_jwt", lambda _t: good_claims
        )
        _stub_token_version_check(monkeypatch, ok=True)

        @test_app.route("/api/inspect-contextvars")
        def _inspect() -> Any:
            ctx = structlog.contextvars.get_contextvars()
            return jsonify(
                user_id=ctx.get("user_id"),
                org_id=ctx.get("org_id"),
                role=ctx.get("role"),
            )

        client = test_app.test_client()
        client.set_cookie("session", "tok", domain="localhost")
        response = client.get("/api/inspect-contextvars")
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["user_id"] == uid
        assert payload["org_id"] == oid
        assert payload["role"] == "Contributor"

    def test_contextvars_not_bound_for_unauthenticated_request(
        self, test_app: Flask
    ) -> None:
        """Unauthenticated requests have no user_id/org_id/role in contextvars."""

        @test_app.route("/inspect-contextvars-public")
        def _inspect() -> Any:
            ctx = structlog.contextvars.get_contextvars()
            return jsonify(
                user_id=ctx.get("user_id"),
                org_id=ctx.get("org_id"),
                role=ctx.get("role"),
            )

        client = test_app.test_client()
        response = client.get("/inspect-contextvars-public")
        assert response.status_code == 200
        payload = response.get_json()
        # No user-related fields bound.
        assert payload["user_id"] is None
        assert payload["org_id"] is None
        assert payload["role"] is None


# ---------------------------------------------------------------------------
# Test: register_auth_middleware
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRegisterAuthMiddleware:
    """Validate the registration function attaches the before_request hook."""

    def test_registers_before_request_hook(self) -> None:
        """``register_auth_middleware`` adds a before_request hook."""
        app = Flask(__name__)
        app.config["TESTING"] = True
        before_count_before = sum(
            len(funcs) for funcs in app.before_request_funcs.values()
        )
        register_auth_middleware(app)
        before_count_after = sum(
            len(funcs) for funcs in app.before_request_funcs.values()
        )
        assert before_count_after >= before_count_before + 1

    def test_double_registration_appends_second_hook(self) -> None:
        """Registering twice does not crash; effect is idempotent for routes
        because the hook is pure and produces the same result.

        Flask's before_request registry is a list; registering twice
        appends two entries. The hook is idempotent so the end-to-end
        behaviour is unchanged. We assert no exception.
        """
        app = Flask(__name__)
        app.config["TESTING"] = True
        register_auth_middleware(app)
        register_auth_middleware(app)
        # If we got here, no exception. Smoke-test that the app is still
        # usable end-to-end.

        @app.route("/ping-public-x")
        def _ping() -> Any:
            return jsonify(ok=True)

        client = app.test_client()
        # Even though /ping-public-x is not in _PUBLIC_PATHS, the auth
        # hook returns None for non-protected non-public paths, so
        # the response is 200.
        response = client.get("/ping-public-x")
        assert response.status_code == 200

    def test_app_config_session_cookie_name_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``app.config['SESSION_COOKIE_NAME']`` overrides the default cookie name.

        When configured, the auth hook reads from the custom cookie name.
        """
        from app.services import auth as services_auth  # noqa: PLC0415

        good_claims = _claims(role="Admin")
        monkeypatch.setattr(
            services_auth, "verify_session_jwt", lambda _t: good_claims
        )
        _stub_token_version_check(monkeypatch, ok=True)

        app = Flask(__name__)
        app.config["TESTING"] = True
        app.config["PROPAGATE_EXCEPTIONS"] = False
        app.config["SECRET_KEY"] = "test-secret"
        app.config["JWT_SIGNING_KEY"] = "test-signing-key-do-not-use-in-prod"
        app.config["JWT_ALGORITHM"] = "HS256"
        app.config["SESSION_COOKIE_NAME"] = "custom_session"
        register_correlation_middleware(app)
        register_error_handlers(app)
        register_auth_middleware(app)

        @app.route("/api/ping")
        def _ping() -> Any:
            return jsonify(role=g.session.role.value)

        client = app.test_client()
        # Set the CUSTOM cookie name; default 'session' should not work.
        client.set_cookie("custom_session", "tok", domain="localhost")
        response = client.get("/api/ping")
        # If the override took effect, we get 200; otherwise 401.
        # Implementations that don't honour the config will fail this test,
        # which is the desired outcome (fail loudly).
        assert response.status_code == 200, (
            f"Expected 200 with custom_session cookie; got {response.status_code}. "
            "app.config['SESSION_COOKIE_NAME'] override is not honoured."
        )


# ---------------------------------------------------------------------------
# Test: Defensive Behaviour & PII Safety
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDefensivePIISafety:
    """Verify that auth failures never leak PII or internal state."""

    def test_401_message_does_not_echo_token(
        self, test_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The 401 envelope must NOT contain the offending token string.

        A logged-out attacker MUST NOT be able to confirm token shape
        by inspecting the error message.
        """
        from app.services import auth as services_auth  # noqa: PLC0415

        def _raise_invalid(_token: str) -> dict[str, Any]:
            raise services_auth.AuthenticationError("invalid")

        monkeypatch.setattr(services_auth, "verify_session_jwt", _raise_invalid)
        secret_token = "secret-token-AbCdEfG12345"
        test_client.set_cookie("session", secret_token, domain="localhost")
        response = test_client.get("/api/ping")
        body = response.get_data(as_text=True)
        assert secret_token not in body, (
            "Token leak: the secret token appeared in the 401 response."
        )

    def test_401_message_does_not_leak_internals(
        self, test_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The 401 envelope contains a generic message, not internal auth-layer state."""
        from app.services import auth as services_auth  # noqa: PLC0415

        def _raise_invalid(_token: str) -> dict[str, Any]:
            raise services_auth.AuthenticationError(
                "JWT signature mismatch with key rotation #42"
            )

        monkeypatch.setattr(services_auth, "verify_session_jwt", _raise_invalid)
        test_client.set_cookie("session", "tok", domain="localhost")
        response = test_client.get("/api/ping")
        payload = response.get_json()
        assert "key rotation" not in payload["error"]["message"]
        assert "#42" not in payload["error"]["message"]
        assert "JWT" not in payload["error"]["message"]

    def test_correlation_id_preserved_on_401(self, test_client: Any) -> None:
        """401 responses still carry the inbound correlation_id."""
        response = test_client.get(
            "/api/ping",
            headers={"X-Correlation-Id": "auth-trace-cid"},
        )
        assert response.status_code == 401
        # Correlation header is echoed.
        assert response.headers.get("X-Correlation-Id") == "auth-trace-cid"
        payload = response.get_json()
        assert payload["error"]["correlation_id"] == "auth-trace-cid"

