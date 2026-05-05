"""Tests for ``app.middleware.security_headers``.

Validates the per-response defensive HTTP-headers middleware contract
introduced to close QA Checkpoint 7 Issue #1 (MEDIUM): production
/api/* responses lacked baseline security headers because the AWS ALB
routes /api/* and /auth/* directly to Flask without nginx
interposition per AAP Section 0.4.6.

Coverage targets:

* The four static defensive headers (``X-Content-Type-Options``,
  ``X-Frame-Options``, ``Referrer-Policy``, ``Permissions-Policy``)
  are attached to EVERY response regardless of path or method,
  including:

    - 2xx successes from route handlers.
    - 4xx error responses produced by ``register_error_handlers``.
    - 5xx exception responses from the catch-all handler.
    - Public observability endpoints (``/healthz``, ``/readyz``).
    - The Prometheus metrics endpoint (``/metrics``).
    - CORS preflight (OPTIONS) short-circuit responses.

* ``Cache-Control: no-store, no-cache, must-revalidate, private``
  is attached to ``/api/*`` and ``/auth/*`` non-OPTIONS responses;
  it is NOT attached to ``/healthz``, ``/readyz``, ``/metrics``, or
  to OPTIONS preflight responses.

* ``Strict-Transport-Security`` is attached only when
  ``app.config['SESSION_COOKIE_SECURE']`` is True (production); it
  is NOT attached when False (development/testing).

* The middleware is idempotent: route handlers (or upstream hooks)
  that explicitly set any of these headers have their values
  preserved verbatim.

* The full-app integration via ``app.create_app(TestingConfig)``
  honors all of the above through every blueprint and through the
  error-handler middleware.

Test isolation strategy
=======================

* Unit tests use a minimal Flask app constructed by
  ``_make_test_app`` that registers ONLY the security-headers
  middleware (and an inline route per test) so other middleware
  cannot pollute the assertion surface.

* The full-app integration tests piggyback on the session-scoped
  ``app`` and ``client`` fixtures from ``conftest.py`` so they
  exercise the EXACT same wiring (correlation -> cors ->
  security_headers -> auth -> rbac -> error_handlers) used by
  production. These tests are tagged ``integration`` so they can be
  skipped during fast unit-only runs.
"""

from __future__ import annotations

# Standard library imports.
from typing import TYPE_CHECKING, Any

# Third-party imports.
from flask import Flask, jsonify
import pytest

# First-party imports under test.
from app.middleware.security_headers import (
    register_security_headers_middleware,
)

# Type-only imports gated under TYPE_CHECKING so the runtime surface
# stays minimal and ruff's TC002/TC003 typing-only-import lint rules
# are satisfied.
if TYPE_CHECKING:
    from collections.abc import Generator

    from flask.testing import FlaskClient


# ---------------------------------------------------------------------------
# Module-level test helpers
# ---------------------------------------------------------------------------


def _make_test_app(*, secure_cookies: bool = False) -> Flask:
    """Build a minimal Flask app wired ONLY with security-headers middleware.

    This is deliberate isolation: the full ``create_app(TestingConfig)``
    factory wires correlation, CORS, auth, RBAC, and error handlers in
    addition to security headers. Wiring all of them here would mix
    behavior across multiple after_request hooks and complicate
    targeted assertions about THIS middleware's behavior.

    The optional inline routes deliberately surface the response so
    assertions can verify each header path:

        * ``GET /api/echo`` returns 200 with a tiny JSON body so we
          can verify ``Cache-Control`` is attached to ``/api/*``.
        * ``GET /auth/echo`` returns 200 with a tiny JSON body so we
          can verify ``Cache-Control`` is attached to ``/auth/*``.
        * ``GET /healthz`` returns 200 to verify ``Cache-Control``
          is NOT attached to public health endpoints.
        * ``GET /metrics`` returns 200 to verify ``Cache-Control``
          is NOT attached to the metrics scrape endpoint.
        * ``GET /api/preset-cache`` pre-sets a ``Cache-Control``
          value to verify the middleware preserves an explicit value
          (idempotency invariant).
        * ``GET /api/preset-frame`` pre-sets ``X-Frame-Options`` to
          a non-default value to verify the same idempotency invariant
          applies to the static headers.
        * ``GET /api/raises`` raises a RuntimeError so we can verify
          the headers are attached even on the Flask default 500
          response (no error handlers wired in this minimal app).

    Args:
        secure_cookies: When True, configure the app with
            ``SESSION_COOKIE_SECURE = True`` so the HSTS branch is
            exercised. Default False matches the development /
            testing posture documented in
            ``backend/app/config.py``.

    Returns:
        A Flask app with security-headers middleware installed and
        the seven inline test routes mounted. Caller owns the
        lifetime; the app does not persist across tests.
    """
    app = Flask(__name__)
    # ``SESSION_COOKIE_SECURE`` drives the HSTS branch in the
    # middleware. ``False`` here mirrors DevelopmentConfig and
    # TestingConfig; ``True`` mirrors ProductionConfig.
    app.config["SESSION_COOKIE_SECURE"] = secure_cookies

    register_security_headers_middleware(app)

    @app.route("/api/echo")
    def api_echo() -> Any:
        return jsonify(ok=True)

    @app.route("/auth/echo")
    def auth_echo() -> Any:
        return jsonify(ok=True)

    @app.route("/healthz")
    def healthz() -> Any:
        return jsonify(ok=True)

    @app.route("/metrics")
    def metrics() -> Any:
        return jsonify(ok=True)

    @app.route("/api/preset-cache")
    def preset_cache() -> Any:
        # A future endpoint might want a public-cache directive
        # (e.g., a cached list of org-wide tags). The middleware MUST
        # preserve an explicit Cache-Control value rather than
        # overwrite it with no-store.
        response = jsonify(ok=True)
        response.headers["Cache-Control"] = "public, max-age=3600"
        return response

    @app.route("/api/preset-frame")
    def preset_frame() -> Any:
        # A hosted-embed surface (post-MVP) might need a different
        # X-Frame-Options. The middleware MUST preserve an explicit
        # value.
        response = jsonify(ok=True)
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        return response

    @app.route("/api/raises")
    def raises() -> Any:
        raise RuntimeError("simulated failure for security-headers test")

    @app.route("/api/cors-preflight", methods=["OPTIONS"])
    def cors_preflight() -> Any:
        # An OPTIONS handler returning 204 to simulate the CORS
        # preflight short-circuit. The middleware MUST attach the
        # static headers but MUST NOT attach Cache-Control (preflight
        # caching is a CORS-prescribed optimization).
        return ("", 204)

    return app


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def insecure_app() -> Flask:
    """Provide a minimal Flask app with security headers and HSTS off.

    Models DevelopmentConfig / TestingConfig posture: the dev server
    runs over plain HTTP and HSTS would otherwise lock the browser
    to HTTPS for localhost.
    """
    return _make_test_app(secure_cookies=False)


@pytest.fixture
def secure_app() -> Flask:
    """Provide a minimal Flask app with security headers and HSTS on.

    Models ProductionConfig posture: the deployment terminates TLS
    at the AWS ALB, the session cookie is ``Secure``, and HSTS is
    appropriate to set on every response.
    """
    return _make_test_app(secure_cookies=True)


@pytest.fixture
def insecure_client(insecure_app: Flask) -> FlaskClient:
    """Test client for the dev/test posture app fixture."""
    return insecure_app.test_client()


@pytest.fixture
def secure_client(secure_app: Flask) -> FlaskClient:
    """Test client for the production posture app fixture."""
    return secure_app.test_client()


# ---------------------------------------------------------------------------
# TestStaticHeaders: nosniff, X-Frame-Options, Referrer-Policy, Permissions-Policy
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestStaticHeaders:
    """Validate the four always-on static defensive headers."""

    def test_x_content_type_options_on_api(self, insecure_client: FlaskClient) -> None:
        """X-Content-Type-Options: nosniff is set on /api/* 2xx responses."""
        response = insecure_client.get("/api/echo")
        assert response.status_code == 200
        assert response.headers.get("X-Content-Type-Options") == "nosniff"

    def test_x_content_type_options_on_auth(self, insecure_client: FlaskClient) -> None:
        """X-Content-Type-Options: nosniff is set on /auth/* 2xx responses."""
        response = insecure_client.get("/auth/echo")
        assert response.status_code == 200
        assert response.headers.get("X-Content-Type-Options") == "nosniff"

    def test_x_content_type_options_on_healthz(self, insecure_client: FlaskClient) -> None:
        """X-Content-Type-Options: nosniff is set on /healthz responses."""
        response = insecure_client.get("/healthz")
        assert response.status_code == 200
        assert response.headers.get("X-Content-Type-Options") == "nosniff"

    def test_x_content_type_options_on_metrics(self, insecure_client: FlaskClient) -> None:
        """X-Content-Type-Options: nosniff is set on /metrics responses."""
        response = insecure_client.get("/metrics")
        assert response.status_code == 200
        assert response.headers.get("X-Content-Type-Options") == "nosniff"

    def test_x_frame_options_deny(self, insecure_client: FlaskClient) -> None:
        """X-Frame-Options: DENY is set on every response."""
        for path in ["/api/echo", "/auth/echo", "/healthz", "/metrics"]:
            response = insecure_client.get(path)
            assert response.headers.get("X-Frame-Options") == "DENY", (
                f"X-Frame-Options missing on {path}"
            )

    def test_referrer_policy(self, insecure_client: FlaskClient) -> None:
        """Referrer-Policy: strict-origin-when-cross-origin is set on every response."""
        for path in ["/api/echo", "/auth/echo", "/healthz", "/metrics"]:
            response = insecure_client.get(path)
            assert response.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin", (
                f"Referrer-Policy missing on {path}"
            )

    def test_permissions_policy(self, insecure_client: FlaskClient) -> None:
        """Permissions-Policy: geolocation=(), microphone=(), camera=() set on every response."""
        for path in ["/api/echo", "/auth/echo", "/healthz", "/metrics"]:
            response = insecure_client.get(path)
            assert (
                response.headers.get("Permissions-Policy")
                == "geolocation=(), microphone=(), camera=()"
            ), f"Permissions-Policy missing on {path}"

    def test_static_headers_on_500_response(
        self, insecure_app: Flask, insecure_client: FlaskClient
    ) -> None:
        """Static defensive headers are attached even when the route raises.

        Flask's default 500 response is still produced by the wsgi
        machinery and flows through after_request, so the headers
        must be present even on unhandled exceptions. This is the
        critical path for hardening: 500 responses are exactly when
        security headers matter most because they often expose the
        last bit of information a misconfigured server would leak.
        """
        # ``propagate_exceptions=False`` is the default for non-test
        # apps; setting it explicitly keeps Flask returning a 500
        # response rather than re-raising the RuntimeError.
        insecure_app.config["PROPAGATE_EXCEPTIONS"] = False
        response = insecure_client.get("/api/raises")
        assert response.status_code == 500
        assert response.headers.get("X-Content-Type-Options") == "nosniff"
        assert response.headers.get("X-Frame-Options") == "DENY"
        assert response.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"

    def test_static_headers_on_404_response(self, insecure_client: FlaskClient) -> None:
        """Static defensive headers are attached on 404 responses too.

        Flask's built-in 404 handler returns an HTML 404 page; the
        after_request hooks still fire so the security headers
        attach. This is essential because 404 probing is a common
        first step in attack chains.
        """
        response = insecure_client.get("/api/does-not-exist")
        assert response.status_code == 404
        assert response.headers.get("X-Content-Type-Options") == "nosniff"
        assert response.headers.get("X-Frame-Options") == "DENY"
        assert response.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"


# ---------------------------------------------------------------------------
# TestCacheControl: no-store on /api/* and /auth/*; not on observability paths
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCacheControl:
    """Validate the path-conditional Cache-Control attachment."""

    _EXPECTED_VALUE = "no-store, no-cache, must-revalidate, private"

    def test_cache_control_on_api_path(self, insecure_client: FlaskClient) -> None:
        """Cache-Control no-store applies to /api/* responses."""
        response = insecure_client.get("/api/echo")
        assert response.status_code == 200
        assert response.headers.get("Cache-Control") == self._EXPECTED_VALUE

    def test_cache_control_on_auth_path(self, insecure_client: FlaskClient) -> None:
        """Cache-Control no-store applies to /auth/* responses."""
        response = insecure_client.get("/auth/echo")
        assert response.status_code == 200
        assert response.headers.get("Cache-Control") == self._EXPECTED_VALUE

    def test_no_cache_control_on_healthz(self, insecure_client: FlaskClient) -> None:
        """Cache-Control no-store is NOT applied to /healthz.

        ALB and ECS health-check pollers benefit from a brief
        in-poller cache window of the lightweight 200/503 response;
        forcing no-store would defeat that without any security
        benefit (no user data is exposed by the probe).
        """
        response = insecure_client.get("/healthz")
        assert response.status_code == 200
        # The Flask default jsonify response does NOT set
        # Cache-Control on its own; the middleware-driven omission
        # is therefore observed as an absent header.
        assert "Cache-Control" not in response.headers

    def test_no_cache_control_on_metrics(self, insecure_client: FlaskClient) -> None:
        """Cache-Control no-store is NOT applied to /metrics.

        The AWS Distro for OpenTelemetry collector legitimately
        scrapes /metrics on a short interval; no user data is
        exposed by the metrics endpoint (only aggregate counters
        and histograms).
        """
        response = insecure_client.get("/metrics")
        assert response.status_code == 200
        assert "Cache-Control" not in response.headers

    def test_no_cache_control_on_options_preflight(self, insecure_client: FlaskClient) -> None:
        """Cache-Control is NOT attached to OPTIONS preflight responses.

        OPTIONS preflight responses must remain cacheable so browsers
        honor the Access-Control-Max-Age directive set by the CORS
        middleware. Forcing no-store here would cause every cross-
        origin request from the SPA to send a redundant preflight,
        degrading user-perceived latency.
        """
        response = insecure_client.open("/api/cors-preflight", method="OPTIONS")
        assert response.status_code == 204
        assert "Cache-Control" not in response.headers

    def test_cache_control_preserved_when_handler_sets_it(
        self, insecure_client: FlaskClient
    ) -> None:
        """A handler that explicitly sets Cache-Control has its value preserved.

        Idempotency invariant: the middleware uses setdefault-style
        semantics. A future handler that wants public caching for a
        non-sensitive list endpoint (e.g., the org-wide tag catalog
        in an admin tool) must be able to override the default.
        """
        response = insecure_client.get("/api/preset-cache")
        assert response.status_code == 200
        assert response.headers.get("Cache-Control") == "public, max-age=3600"


# ---------------------------------------------------------------------------
# TestHSTS: Strict-Transport-Security only when SESSION_COOKIE_SECURE=True
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHSTS:
    """Validate the production-only HSTS attachment."""

    _EXPECTED_VALUE = "max-age=31536000; includeSubDomains"

    def test_hsts_set_when_secure_cookies(self, secure_client: FlaskClient) -> None:
        """HSTS is set when SESSION_COOKIE_SECURE is True (production)."""
        response = secure_client.get("/api/echo")
        assert response.status_code == 200
        assert response.headers.get("Strict-Transport-Security") == self._EXPECTED_VALUE

    def test_hsts_set_on_auth_when_secure(self, secure_client: FlaskClient) -> None:
        """HSTS is set on /auth/* in production posture."""
        response = secure_client.get("/auth/echo")
        assert response.status_code == 200
        assert response.headers.get("Strict-Transport-Security") == self._EXPECTED_VALUE

    def test_hsts_set_on_healthz_when_secure(self, secure_client: FlaskClient) -> None:
        """HSTS is set on every response in production, including /healthz.

        AWS ALB health-check pollers are server-side and HSTS is a
        no-op for them, but consistent behavior across all paths
        keeps the contract simple. There is no benefit to omitting
        HSTS on health-check endpoints.
        """
        response = secure_client.get("/healthz")
        assert response.status_code == 200
        assert response.headers.get("Strict-Transport-Security") == self._EXPECTED_VALUE

    def test_hsts_not_set_when_insecure_cookies(self, insecure_client: FlaskClient) -> None:
        """HSTS is NOT set when SESSION_COOKIE_SECURE is False (dev/test).

        Setting HSTS on plain HTTP responses would lock conformant
        browsers to HTTPS for localhost interactions, breaking
        development workflows. The middleware correctly suppresses
        the header in non-secure-cookie environments.
        """
        response = insecure_client.get("/api/echo")
        assert response.status_code == 200
        assert "Strict-Transport-Security" not in response.headers


# ---------------------------------------------------------------------------
# TestIdempotency: handler-set values are preserved
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestIdempotency:
    """Validate that the middleware preserves explicitly-set handler headers."""

    def test_x_frame_options_handler_value_preserved(self, insecure_client: FlaskClient) -> None:
        """A handler that sets X-Frame-Options has the value preserved."""
        response = insecure_client.get("/api/preset-frame")
        assert response.status_code == 200
        assert response.headers.get("X-Frame-Options") == "SAMEORIGIN"

    def test_static_headers_only_attached_once(self, insecure_client: FlaskClient) -> None:
        """Each static defensive header appears exactly once on a response.

        Werkzeug's Headers container supports duplicate keys; the
        middleware must use setdefault-style semantics so a future
        change that adds a duplicate definition (e.g., a contributor
        copy-pasting the registration) does not produce two values.
        """
        response = insecure_client.get("/api/echo")
        # ``getlist`` returns every header value associated with the
        # given name. Each must appear exactly once.
        assert len(response.headers.getlist("X-Content-Type-Options")) == 1
        assert len(response.headers.getlist("X-Frame-Options")) == 1
        assert len(response.headers.getlist("Referrer-Policy")) == 1
        assert len(response.headers.getlist("Permissions-Policy")) == 1


# ---------------------------------------------------------------------------
# TestRegistrationFunction: the public entry point is well-behaved
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRegistrationFunction:
    """Validate the ``register_security_headers_middleware`` public API."""

    def test_registration_attaches_after_request_hook(self) -> None:
        """register_security_headers_middleware adds an after_request hook.

        Flask stores after_request hooks in
        ``app.after_request_funcs``: a dict keyed by blueprint name
        (None for app-level hooks) mapping to a list of callables.
        The middleware must add exactly one new entry to the
        app-level (None) bucket.
        """
        app = Flask(__name__)
        app.config["SESSION_COOKIE_SECURE"] = False
        # Snapshot the count of app-level after_request hooks before
        # registration. Flask may install internal ones depending on
        # extensions; we measure the delta rather than the absolute.
        before_count = len(app.after_request_funcs.get(None, []))
        register_security_headers_middleware(app)
        after_count = len(app.after_request_funcs.get(None, []))
        assert after_count == before_count + 1

    def test_registration_is_idempotent_safe(self) -> None:
        """Multiple registrations on the same app do not raise.

        Flask deduplicates handler registrations by view-function
        identity within a single registration call, but the
        ``after_request`` list does NOT deduplicate. Calling the
        registration function twice would attach the hook twice;
        the headers would still be set correctly because each pass
        through the after_request chain checks ``if name not in
        response.headers`` (idempotent setdefault semantics). This
        test confirms the registration function does not raise on
        re-call.
        """
        app = Flask(__name__)
        app.config["SESSION_COOKIE_SECURE"] = False
        register_security_headers_middleware(app)
        # No exception is raised on the second call.
        register_security_headers_middleware(app)


# ---------------------------------------------------------------------------
# TestFullAppIntegration: integration with create_app(TestingConfig)
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestFullAppIntegration:
    """Validate the middleware in the full Flask app via create_app.

    These tests use the session-scoped ``client`` fixture from
    ``conftest.py`` which is built via
    ``create_app(config_object='app.config.TestingConfig')``.
    They confirm that the production wiring (correlation -> cors ->
    security_headers -> auth -> rbac -> error_handlers) honors all
    of the unit-tested invariants on every reachable endpoint
    including the auth 401 short-circuit, the error-handler 404 /
    500 paths, and the public observability endpoints.
    """

    def test_healthz_carries_static_headers(self, client: FlaskClient) -> None:
        """The /healthz endpoint carries the four defensive headers."""
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.headers.get("X-Content-Type-Options") == "nosniff"
        assert response.headers.get("X-Frame-Options") == "DENY"
        assert response.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
        assert (
            response.headers.get("Permissions-Policy") == "geolocation=(), microphone=(), camera=()"
        )

    def test_metrics_carries_static_headers(self, client: FlaskClient) -> None:
        """The /metrics endpoint carries the four defensive headers."""
        response = client.get("/metrics")
        # /metrics is open in TestingConfig (no METRICS_BEARER_TOKEN),
        # so 200 is the expected outcome.
        assert response.status_code == 200
        assert response.headers.get("X-Content-Type-Options") == "nosniff"
        assert response.headers.get("X-Frame-Options") == "DENY"

    def test_unauthenticated_api_response_carries_headers(self, client: FlaskClient) -> None:
        """The auth-middleware 401 short-circuit response carries headers.

        This is the critical path for the QA Checkpoint 7 finding:
        previously the 401 response from an unauthenticated /api/*
        request lacked X-Content-Type-Options, X-Frame-Options, etc.
        After this change, every 401 must carry the full defensive
        header set AND the Cache-Control no-store directive.
        """
        response = client.get("/api/me")
        assert response.status_code == 401
        assert response.headers.get("X-Content-Type-Options") == "nosniff"
        assert response.headers.get("X-Frame-Options") == "DENY"
        assert response.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
        assert (
            response.headers.get("Permissions-Policy") == "geolocation=(), microphone=(), camera=()"
        )
        assert (
            response.headers.get("Cache-Control") == "no-store, no-cache, must-revalidate, private"
        )

    def test_unauthenticated_connections_response_carries_headers(
        self, client: FlaskClient
    ) -> None:
        """The /api/connections 401 short-circuit response carries headers."""
        response = client.get("/api/connections")
        assert response.status_code == 401
        assert response.headers.get("X-Content-Type-Options") == "nosniff"
        assert response.headers.get("X-Frame-Options") == "DENY"
        assert (
            response.headers.get("Cache-Control") == "no-store, no-cache, must-revalidate, private"
        )

    def test_auth_login_response_carries_headers(self, client: FlaskClient) -> None:
        """The /auth/login response (even on validation failure) carries headers."""
        # Posting an empty body produces a 422 validation failure,
        # which still flows through the after_request chain.
        response = client.post("/auth/login", json={})
        assert response.status_code in (400, 422)
        assert response.headers.get("X-Content-Type-Options") == "nosniff"
        assert response.headers.get("X-Frame-Options") == "DENY"
        assert (
            response.headers.get("Cache-Control") == "no-store, no-cache, must-revalidate, private"
        )

    def test_testing_app_does_not_set_hsts(self, app: Flask, client: FlaskClient) -> None:
        """TestingConfig posture (SESSION_COOKIE_SECURE=False) suppresses HSTS.

        Defensive setup: the session-scoped ``app`` fixture is shared
        across the entire test session, and another test in the suite
        (``tests/api/test_auth.py``) explicitly mutates
        ``SESSION_COOKIE_SECURE`` to True without restoring it. We
        therefore set the value explicitly and restore it here so
        this test is self-contained regardless of the order in which
        the surrounding suite ran.
        """
        # Save and restore the original value. Setting to False
        # mirrors TestingConfig (per ``backend/app/config.py:394``)
        # and is the canonical posture for this assertion.
        original = app.config.get("SESSION_COOKIE_SECURE")
        app.config["SESSION_COOKIE_SECURE"] = False
        try:
            response = client.get("/healthz")
            assert response.status_code == 200
            assert "Strict-Transport-Security" not in response.headers
        finally:
            app.config["SESSION_COOKIE_SECURE"] = original

    def test_healthz_does_not_set_cache_control(self, client: FlaskClient) -> None:
        """The /healthz endpoint does NOT receive Cache-Control no-store.

        Health probes carry no user data and benefit from short-window
        caching at the ALB poller. The middleware correctly excludes
        them from the Cache-Control directive.
        """
        response = client.get("/healthz")
        assert response.status_code == 200
        cache_control = response.headers.get("Cache-Control", "")
        # The handler may set its own short cache value; the
        # middleware-driven no-store value MUST NOT be present.
        assert "no-store" not in cache_control

    def test_404_response_carries_headers(self, client: FlaskClient) -> None:
        """A 404 response from the error-handler middleware carries headers.

        The error-handler middleware converts 404 NotFound into the
        canonical JSON envelope. The after_request chain still runs,
        so security headers must be attached.
        """
        response = client.get("/api/connections/not-a-real-uuid-here")
        # Either 401 (auth rejects malformed UUID before route) or
        # 404 (route accepts then handler converts NotFound) is
        # acceptable; both flow through after_request.
        assert response.status_code in (401, 404, 422)
        assert response.headers.get("X-Content-Type-Options") == "nosniff"
        assert response.headers.get("X-Frame-Options") == "DENY"


# ---------------------------------------------------------------------------
# TestSecureAppFullIntegration: HSTS on a production-like app
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestSecureAppFullIntegration:
    """Validate HSTS attachment on a fully-wired app with secure cookies.

    Constructs a fresh app via the same factory used for the
    session-scoped ``app`` fixture, but overrides
    ``SESSION_COOKIE_SECURE`` to True to model production behavior.
    This validates that the middleware's HSTS branch interacts
    correctly with the rest of the wiring.
    """

    @pytest.fixture
    def secure_full_app(self, app: Flask) -> Generator[Flask, None, None]:
        """Yield the session app with SESSION_COOKIE_SECURE flipped on."""
        # Save and restore so other tests in the session see the
        # original False value. Restoration is critical because the
        # session-scoped ``app`` fixture is shared across tests.
        original = app.config.get("SESSION_COOKIE_SECURE")
        app.config["SESSION_COOKIE_SECURE"] = True
        try:
            yield app
        finally:
            app.config["SESSION_COOKIE_SECURE"] = original

    def test_hsts_attached_to_healthz(self, secure_full_app: Flask) -> None:
        """HSTS appears on /healthz when SESSION_COOKIE_SECURE=True."""
        with secure_full_app.test_client() as cli:
            response = cli.get("/healthz")
            assert response.status_code == 200
            assert (
                response.headers.get("Strict-Transport-Security")
                == "max-age=31536000; includeSubDomains"
            )

    def test_hsts_attached_to_unauth_api(self, secure_full_app: Flask) -> None:
        """HSTS appears on the /api/* 401 short-circuit when secure."""
        with secure_full_app.test_client() as cli:
            response = cli.get("/api/me")
            assert response.status_code == 401
            assert (
                response.headers.get("Strict-Transport-Security")
                == "max-age=31536000; includeSubDomains"
            )
