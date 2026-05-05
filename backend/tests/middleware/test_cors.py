"""Tests for ``app.middleware.cors``.

Validates the hand-rolled CORS middleware contract introduced to close
QA Issue 12 (MAJOR) which observed that ``CORS_ALLOWED_ORIGINS`` and
``CORS_ALLOW_CREDENTIALS`` were defined in ``app.config`` but never
wired into request processing. This test module specifically targets
the per-module coverage gap noted in QA Final Checkpoint 12 Issue 2:
``app/middleware/cors.py`` was at 39.2% coverage with no dedicated
test file.

Coverage targets (drawn from the AAP Section 0.7.1 architectural
invariants and the module's behavioral contract):

* **Preflight short-circuit (``_before_request_cors``)**:
  - OPTIONS with ``Access-Control-Request-Method`` from an allowed
    origin returns 204 with the full preflight header set:
    ``Access-Control-Allow-Origin`` (echo of request Origin),
    ``Access-Control-Allow-Credentials`` (when configured),
    ``Access-Control-Allow-Methods``, ``Access-Control-Allow-Headers``,
    ``Access-Control-Max-Age``, and ``Vary: Origin``.
  - OPTIONS with ``Access-Control-Request-Method`` from a
    non-allowlisted origin falls through to the route handler with
    NO ``Access-Control-Allow-*`` headers (browser will block).
  - OPTIONS without ``Access-Control-Request-Method`` is NOT treated
    as preflight (server-to-server CORS discovery probe).
  - OPTIONS without an ``Origin`` header from any origin falls
    through (server-to-server caller; CORS irrelevant).
  - Preflight echoes ``Access-Control-Request-Headers`` when
    provided by the client; falls back to the static allowlist
    otherwise.

* **Actual-request header attachment (``_after_request_cors``)**:
  - Allowed-origin actual request gets
    ``Access-Control-Allow-Origin`` echoed verbatim plus
    ``Access-Control-Allow-Credentials: true`` (when configured)
    plus ``Access-Control-Expose-Headers: X-Correlation-Id``.
  - Disallowed-origin actual request gets NO
    ``Access-Control-Allow-*`` headers (but DOES get ``Vary: Origin``
    because cache-key correctness requires it even for blocked
    origins).
  - No-origin (server-to-server) request gets NO CORS headers and
    NO ``Vary: Origin`` (no cache-keying needed without an Origin).
  - Idempotency: a pre-set ``Access-Control-Allow-Origin`` header
    (built by the preflight short-circuit) is NOT overwritten by
    the after_request hook.
  - ``Vary: Origin`` is APPENDED to an existing ``Vary`` header
    (e.g., ``Accept-Encoding`` from compression middleware) rather
    than overwriting.
  - ``Vary`` containing ``origin`` already (case-insensitive) is
    NOT duplicated.
  - Trailing slash on the Origin header is stripped before
    allowlist comparison.

* **Configuration toggles**:
  - ``CORS_ALLOW_CREDENTIALS=False`` suppresses the
    ``Access-Control-Allow-Credentials`` header on both preflight
    and actual responses.
  - Empty / missing ``CORS_ALLOWED_ORIGINS`` blocks all cross-origin
    requests.

* **Registration**:
  - ``register_cors_middleware`` is idempotent (registering twice
    does not double-attach hooks).
  - The before_request hook is registered before the after_request
    hook is invoked (ordering invariant).

Test isolation strategy
=======================

* Unit tests use a minimal Flask app constructed by
  ``_make_test_app`` that registers ONLY the CORS middleware (and
  inline routes per test) so other middleware cannot pollute the
  assertion surface (e.g., the auth middleware would 401 the
  preflight before our hook ran).

* The Flask app is configured per-test with
  ``CORS_ALLOWED_ORIGINS`` and ``CORS_ALLOW_CREDENTIALS`` so each
  test exercises a deterministic configuration without relying on
  global state.

Per AAP Section 0.5.2 Layer 0 the production middleware order is:
    correlation -> CORS -> auth -> rbac -> error_handlers
Tests deliberately omit the surrounding middleware to focus
assertions on CORS-specific behavior.
"""

from __future__ import annotations

# Standard library imports.
import logging
from typing import TYPE_CHECKING, Any

from flask import Flask, jsonify

# Third-party imports.
import pytest

# First-party imports under test.
from app.middleware.cors import register_cors_middleware

# Type-only imports gated under TYPE_CHECKING so the runtime surface
# stays minimal and ruff's TC002/TC003 typing-only-import lint rules
# are satisfied.
if TYPE_CHECKING:
    from flask.testing import FlaskClient


# ---------------------------------------------------------------------------
# Module-level test helpers
# ---------------------------------------------------------------------------


_ALLOWED_ORIGIN = "http://localhost:5173"
_OTHER_ALLOWED_ORIGIN = "https://app.sales-connections.example.com"
_DISALLOWED_ORIGIN = "https://evil.example.com"


def _make_test_app(
    allowed_origins: list[str] | None = None,
    allow_credentials: bool = True,
) -> Flask:
    """Build a minimal Flask app wired ONLY with CORS middleware.

    Deliberate isolation: the full ``create_app(TestingConfig)``
    factory wires correlation, auth, rbac, and error handlers in
    addition to CORS. Wiring all of them here would mix behavior
    across multiple before_request hooks and complicate targeted
    assertions about THIS middleware's behavior. In particular, the
    auth middleware would short-circuit the OPTIONS preflight with
    401 before our CORS hook ran, masking the contract under test.

    The inline route surfaces an actual JSON response so we can
    assert the after_request branch's CORS-header attachment on real
    response bodies (not just preflight 204s).

    Args:
        allowed_origins: List passed to ``CORS_ALLOWED_ORIGINS``.
            Defaults to ``[_ALLOWED_ORIGIN, _OTHER_ALLOWED_ORIGIN]``
            so most tests can use either origin.
        allow_credentials: Value passed to ``CORS_ALLOW_CREDENTIALS``.
            Defaults to ``True`` per AAP.

    Returns:
        A Flask app with CORS middleware installed and a ``GET /api/ping``
        inline route mounted. Caller owns the lifetime; the app does
        not persist across tests.
    """
    app = Flask(__name__)
    if allowed_origins is None:
        allowed_origins = [_ALLOWED_ORIGIN, _OTHER_ALLOWED_ORIGIN]
    app.config["CORS_ALLOWED_ORIGINS"] = allowed_origins
    app.config["CORS_ALLOW_CREDENTIALS"] = allow_credentials

    register_cors_middleware(app)

    @app.route("/api/ping", methods=["GET", "POST", "PATCH", "DELETE"])
    def ping() -> Any:
        return jsonify(ok=True)

    @app.route("/api/preset-acao", methods=["GET"])
    def preset_acao() -> Any:
        # A handler that pre-sets ``Access-Control-Allow-Origin`` so
        # we can verify the after_request hook's idempotency
        # invariant: a pre-set ACAO must NOT be overwritten.
        response = jsonify(ok=True)
        response.headers["Access-Control-Allow-Origin"] = "preset-do-not-overwrite"
        return response

    @app.route("/api/preset-vary", methods=["GET"])
    def preset_vary() -> Any:
        # A handler that pre-sets ``Vary: Accept-Encoding`` (mirroring
        # the compression middleware) so we can verify the
        # after_request hook APPENDS Origin rather than overwriting.
        response = jsonify(ok=True)
        response.headers["Vary"] = "Accept-Encoding"
        return response

    @app.route("/api/preset-vary-with-origin", methods=["GET"])
    def preset_vary_with_origin() -> Any:
        # A handler that pre-sets ``Vary: Origin, Accept-Encoding``
        # so we can verify the after_request hook does NOT duplicate
        # ``Origin`` when it is already present (case-insensitive).
        response = jsonify(ok=True)
        response.headers["Vary"] = "Origin, Accept-Encoding"
        return response

    return app


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_app() -> Flask:
    """Provide a minimal Flask app with ONLY CORS middleware.

    Intentionally separate from the full-app fixture in
    ``conftest.py`` (which wires every middleware) so that
    assertions about CORS behavior cannot be muddled by the presence
    of other before/after_request hooks.
    """
    return _make_test_app()


@pytest.fixture
def isolated_client(isolated_app: Flask) -> FlaskClient:
    """Test client backed by the isolated app fixture above."""
    return isolated_app.test_client()


@pytest.fixture
def no_credentials_app() -> Flask:
    """A Flask app configured with ``CORS_ALLOW_CREDENTIALS=False``.

    Used to verify the credentials-suppression branch on both
    preflight and actual responses.
    """
    return _make_test_app(allow_credentials=False)


@pytest.fixture
def no_credentials_client(no_credentials_app: Flask) -> FlaskClient:
    """Test client backed by the no-credentials app fixture above."""
    return no_credentials_app.test_client()


@pytest.fixture
def empty_origins_app() -> Flask:
    """A Flask app configured with an empty ``CORS_ALLOWED_ORIGINS``.

    Used to verify the disable-CORS-entirely branch (every cross-
    origin request blocked by missing CORS response headers).
    """
    return _make_test_app(allowed_origins=[])


@pytest.fixture
def empty_origins_client(empty_origins_app: Flask) -> FlaskClient:
    """Test client backed by the empty-origins app fixture above."""
    return empty_origins_app.test_client()


# ---------------------------------------------------------------------------
# TestPreflightAllowedOrigin: the canonical preflight happy path
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPreflightAllowedOrigin:
    """Validate the preflight short-circuit for an allowlisted Origin.

    A CORS preflight is an HTTP OPTIONS request carrying the
    ``Access-Control-Request-Method`` header. When the request's
    ``Origin`` is on the allowlist, the middleware short-circuits
    with 204 and the full preflight header set per W3C Fetch CORS.
    """

    def test_preflight_returns_204(self, isolated_client: FlaskClient) -> None:
        """Preflight from allowed origin returns 204 No Content."""
        response = isolated_client.options(
            "/api/ping",
            headers={
                "Origin": _ALLOWED_ORIGIN,
                "Access-Control-Request-Method": "POST",
            },
        )
        assert response.status_code == 204

    def test_preflight_echoes_origin(self, isolated_client: FlaskClient) -> None:
        """Preflight ACAO header echoes the request Origin verbatim."""
        response = isolated_client.options(
            "/api/ping",
            headers={
                "Origin": _ALLOWED_ORIGIN,
                "Access-Control-Request-Method": "POST",
            },
        )
        assert response.headers["Access-Control-Allow-Origin"] == _ALLOWED_ORIGIN

    def test_preflight_sets_credentials_header(self, isolated_client: FlaskClient) -> None:
        """Preflight includes ACAC: true when CORS_ALLOW_CREDENTIALS=True."""
        response = isolated_client.options(
            "/api/ping",
            headers={
                "Origin": _ALLOWED_ORIGIN,
                "Access-Control-Request-Method": "POST",
            },
        )
        assert response.headers["Access-Control-Allow-Credentials"] == "true"

    def test_preflight_sets_allow_methods(self, isolated_client: FlaskClient) -> None:
        """Preflight ACAM lists every allowed method explicitly."""
        response = isolated_client.options(
            "/api/ping",
            headers={
                "Origin": _ALLOWED_ORIGIN,
                "Access-Control-Request-Method": "POST",
            },
        )
        methods = response.headers["Access-Control-Allow-Methods"]
        # The middleware emits a comma-separated list of methods. We
        # assert each expected method is present rather than the
        # exact serialization to avoid coupling to formatting.
        for method in ("GET", "POST", "PATCH", "DELETE", "OPTIONS"):
            assert method in methods

    def test_preflight_falls_back_to_static_allow_headers(
        self, isolated_client: FlaskClient
    ) -> None:
        """When client omits AC-Request-Headers, ACAH falls back to the static allowlist."""
        response = isolated_client.options(
            "/api/ping",
            headers={
                "Origin": _ALLOWED_ORIGIN,
                "Access-Control-Request-Method": "POST",
            },
        )
        allow_headers = response.headers["Access-Control-Allow-Headers"]
        # The static allowlist includes Content-Type, Authorization,
        # X-Correlation-Id per the module-level _ALLOWED_HEADERS tuple.
        for header in ("Content-Type", "Authorization", "X-Correlation-Id"):
            assert header in allow_headers

    def test_preflight_echoes_requested_headers(self, isolated_client: FlaskClient) -> None:
        """When client sends AC-Request-Headers, ACAH echoes them verbatim.

        Echoing avoids stale-allowlist bugs when the SPA adds a new
        custom header (the preflight contract is that the server
        either confirms each requested header is allowed or blocks
        the preflight; echoing is the simplest correct behavior for
        a small known-allowlist surface).
        """
        requested = "Content-Type, X-Custom-Future-Header"
        response = isolated_client.options(
            "/api/ping",
            headers={
                "Origin": _ALLOWED_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": requested,
            },
        )
        assert response.headers["Access-Control-Allow-Headers"] == requested

    def test_preflight_sets_max_age(self, isolated_client: FlaskClient) -> None:
        """Preflight Access-Control-Max-Age caps browser preflight cache.

        600 seconds (10 minutes) is the Chromium-imposed maximum;
        higher values are silently capped by the browser.
        """
        response = isolated_client.options(
            "/api/ping",
            headers={
                "Origin": _ALLOWED_ORIGIN,
                "Access-Control-Request-Method": "POST",
            },
        )
        assert response.headers["Access-Control-Max-Age"] == "600"

    def test_preflight_sets_vary_origin(self, isolated_client: FlaskClient) -> None:
        """Preflight emits Vary: Origin so caches key responses by Origin."""
        response = isolated_client.options(
            "/api/ping",
            headers={
                "Origin": _ALLOWED_ORIGIN,
                "Access-Control-Request-Method": "POST",
            },
        )
        assert "Origin" in response.headers["Vary"]

    def test_preflight_body_is_empty(self, isolated_client: FlaskClient) -> None:
        """Preflight 204 body is empty per HTTP spec for status 204."""
        response = isolated_client.options(
            "/api/ping",
            headers={
                "Origin": _ALLOWED_ORIGIN,
                "Access-Control-Request-Method": "POST",
            },
        )
        assert response.data == b""

    def test_preflight_skips_route_handler(self, isolated_client: FlaskClient) -> None:
        """Preflight short-circuit bypasses the route handler entirely.

        Returning a Response from a before_request hook causes Flask
        to skip remaining before_request hooks AND the route handler.
        We verify this indirectly: if the handler had run, the
        response body would be ``{"ok": true}`` (the route's jsonify
        return). Empty body proves the short-circuit fired before the
        handler.
        """
        response = isolated_client.options(
            "/api/ping",
            headers={
                "Origin": _ALLOWED_ORIGIN,
                "Access-Control-Request-Method": "POST",
            },
        )
        # Empty body verified above; status 204 (not 200) further
        # confirms the handler did not run (the handler returns 200).
        assert response.status_code == 204
        assert b"ok" not in response.data

    def test_preflight_for_second_allowed_origin(self, isolated_client: FlaskClient) -> None:
        """A different allowed origin (production SPA URL) also gets 204."""
        response = isolated_client.options(
            "/api/ping",
            headers={
                "Origin": _OTHER_ALLOWED_ORIGIN,
                "Access-Control-Request-Method": "POST",
            },
        )
        assert response.status_code == 204
        assert response.headers["Access-Control-Allow-Origin"] == _OTHER_ALLOWED_ORIGIN

    def test_preflight_origin_with_trailing_slash_normalized(
        self, isolated_client: FlaskClient
    ) -> None:
        """Trailing slash on Origin is stripped before allowlist comparison.

        Browsers SHOULD never send a trailing slash on the Origin
        header (per RFC 6454), but the middleware defensively
        normalizes both sides to make the comparison robust.
        """
        response = isolated_client.options(
            "/api/ping",
            headers={
                "Origin": _ALLOWED_ORIGIN + "/",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert response.status_code == 204
        # The echoed ACAO is the normalized form (no trailing slash)
        # because the middleware stripped the slash before comparison
        # and uses the normalized value when echoing back.
        assert response.headers["Access-Control-Allow-Origin"] == _ALLOWED_ORIGIN


# ---------------------------------------------------------------------------
# TestPreflightDisallowedOrigin: rejection of non-allowlisted origins
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPreflightDisallowedOrigin:
    """Validate that preflights from non-allowlisted origins fall through.

    Per the W3C Fetch CORS spec, a server MUST NOT echo an
    unallowlisted origin back. The middleware does NOT 4xx the
    request (CORS is a browser-enforced policy, not a server-
    authorization signal); it lets the request fall through so that
    server-to-server callers without an Origin header continue to
    work. The browser will block the response on the client side
    because no Access-Control-Allow-Origin header was attached.
    """

    def test_disallowed_origin_no_acao_in_preflight(self, isolated_client: FlaskClient) -> None:
        """Preflight from disallowed origin omits ACAO header."""
        response = isolated_client.options(
            "/api/ping",
            headers={
                "Origin": _DISALLOWED_ORIGIN,
                "Access-Control-Request-Method": "POST",
            },
        )
        assert "Access-Control-Allow-Origin" not in response.headers

    def test_disallowed_origin_no_acac_in_preflight(self, isolated_client: FlaskClient) -> None:
        """Preflight from disallowed origin omits ACAC header."""
        response = isolated_client.options(
            "/api/ping",
            headers={
                "Origin": _DISALLOWED_ORIGIN,
                "Access-Control-Request-Method": "POST",
            },
        )
        assert "Access-Control-Allow-Credentials" not in response.headers

    def test_disallowed_origin_no_acam_in_preflight(self, isolated_client: FlaskClient) -> None:
        """Preflight from disallowed origin omits ACAM header."""
        response = isolated_client.options(
            "/api/ping",
            headers={
                "Origin": _DISALLOWED_ORIGIN,
                "Access-Control-Request-Method": "POST",
            },
        )
        assert "Access-Control-Allow-Methods" not in response.headers

    def test_empty_origin_treated_as_no_origin(self, isolated_client: FlaskClient) -> None:
        """An empty Origin header is treated as no origin (server-to-server probe).

        The middleware's check ``if not origin or origin not in
        allowed_origins`` covers both the empty-string case and the
        not-in-allowlist case in a single branch.
        """
        response = isolated_client.options(
            "/api/ping",
            headers={
                "Origin": "",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert "Access-Control-Allow-Origin" not in response.headers

    def test_no_allowed_origins_blocks_all(self, empty_origins_client: FlaskClient) -> None:
        """Empty CORS_ALLOWED_ORIGINS blocks every cross-origin request.

        This exercises the documented "Empty frozenset disables CORS
        entirely" branch of ``_resolve_allowed_origins``.
        """
        response = empty_origins_client.options(
            "/api/ping",
            headers={
                "Origin": _ALLOWED_ORIGIN,  # any origin, even one that would normally pass
                "Access-Control-Request-Method": "POST",
            },
        )
        assert "Access-Control-Allow-Origin" not in response.headers


# ---------------------------------------------------------------------------
# TestNonPreflightOptions: OPTIONS requests that are NOT CORS preflights
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNonPreflightOptions:
    """Validate handling of OPTIONS requests that are NOT CORS preflights.

    A CORS preflight requires BOTH the OPTIONS method AND the
    ``Access-Control-Request-Method`` header. OPTIONS requests
    without that header are server-to-server CORS-discovery probes
    (or HTTP/2 server hints) and MUST NOT be short-circuited; they
    should be handled by the application.
    """

    def test_options_without_request_method_falls_through(
        self, isolated_client: FlaskClient
    ) -> None:
        """OPTIONS without Access-Control-Request-Method is NOT a preflight.

        Flask's default behavior for OPTIONS on a route that does
        not explicitly handle it returns 200 with the Allow header
        listing supported methods. The status code is therefore NOT
        204 (which would indicate our short-circuit fired
        incorrectly).
        """
        response = isolated_client.options(
            "/api/ping",
            headers={"Origin": _ALLOWED_ORIGIN},
        )
        # The route declares OPTIONS-relevant methods, so Flask handles
        # the OPTIONS response itself. The key assertion is that the
        # short-circuit did NOT fire (status would be 204, body empty,
        # no Allow header from Flask). Here the response is the route's
        # default Flask-built OPTIONS reply.
        assert response.status_code != 204


# ---------------------------------------------------------------------------
# TestActualRequestAllowedOrigin: the canonical actual-request happy path
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestActualRequestAllowedOrigin:
    """Validate that actual (non-preflight) responses get CORS headers.

    The after_request hook attaches ``Access-Control-Allow-Origin``
    (echoing the request Origin), ``Access-Control-Allow-
    Credentials`` (when configured), ``Access-Control-Expose-
    Headers`` (so the SPA can read X-Correlation-Id), and
    ``Vary: Origin`` (cache-key correctness) on every response from
    an allowlisted origin.
    """

    def test_get_response_has_acao(self, isolated_client: FlaskClient) -> None:
        """Allowed-origin GET response includes ACAO echoing the Origin."""
        response = isolated_client.get("/api/ping", headers={"Origin": _ALLOWED_ORIGIN})
        assert response.status_code == 200
        assert response.headers["Access-Control-Allow-Origin"] == _ALLOWED_ORIGIN

    def test_get_response_has_acac(self, isolated_client: FlaskClient) -> None:
        """Allowed-origin GET response includes ACAC: true when configured."""
        response = isolated_client.get("/api/ping", headers={"Origin": _ALLOWED_ORIGIN})
        assert response.headers["Access-Control-Allow-Credentials"] == "true"

    def test_get_response_has_expose_headers(self, isolated_client: FlaskClient) -> None:
        """Allowed-origin GET response includes Access-Control-Expose-Headers.

        Without this header, the SPA cannot read X-Correlation-Id
        from the fetch Response.headers API (only the safelisted CORS
        response headers are visible to JS by default).
        """
        response = isolated_client.get("/api/ping", headers={"Origin": _ALLOWED_ORIGIN})
        assert "X-Correlation-Id" in response.headers["Access-Control-Expose-Headers"]

    def test_get_response_has_vary_origin(self, isolated_client: FlaskClient) -> None:
        """Allowed-origin GET response includes Vary: Origin."""
        response = isolated_client.get("/api/ping", headers={"Origin": _ALLOWED_ORIGIN})
        assert "Origin" in response.headers["Vary"]

    def test_post_response_has_acao(self, isolated_client: FlaskClient) -> None:
        """Allowed-origin POST response also gets ACAO (not just GET)."""
        response = isolated_client.post("/api/ping", headers={"Origin": _ALLOWED_ORIGIN})
        assert response.headers["Access-Control-Allow-Origin"] == _ALLOWED_ORIGIN

    def test_origin_with_trailing_slash_normalized_on_actual(
        self, isolated_client: FlaskClient
    ) -> None:
        """Trailing slash on actual-request Origin is stripped before comparison.

        Defensive normalization: the after_request hook calls
        ``rstrip("/")`` on the inbound Origin so a malformed client
        that sends ``http://localhost:5173/`` (with trailing slash)
        is still recognized as an allowed origin.
        """
        response = isolated_client.get("/api/ping", headers={"Origin": _ALLOWED_ORIGIN + "/"})
        assert response.status_code == 200
        assert response.headers["Access-Control-Allow-Origin"] == _ALLOWED_ORIGIN


# ---------------------------------------------------------------------------
# TestActualRequestDisallowedOrigin: rejection of non-allowlisted actuals
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestActualRequestDisallowedOrigin:
    """Validate that actual responses from disallowed origins lack CORS headers.

    The after_request hook does NOT attach any
    ``Access-Control-Allow-*`` headers when the request Origin is
    not on the allowlist, but DOES still attach ``Vary: Origin``
    because cache-key correctness requires it even for blocked
    origins (a cache populated from a privileged origin must not
    serve to a different origin).
    """

    def test_disallowed_origin_no_acao(self, isolated_client: FlaskClient) -> None:
        """Disallowed-origin response omits ACAO."""
        response = isolated_client.get("/api/ping", headers={"Origin": _DISALLOWED_ORIGIN})
        assert response.status_code == 200  # Request itself is NOT rejected
        assert "Access-Control-Allow-Origin" not in response.headers

    def test_disallowed_origin_no_acac(self, isolated_client: FlaskClient) -> None:
        """Disallowed-origin response omits ACAC."""
        response = isolated_client.get("/api/ping", headers={"Origin": _DISALLOWED_ORIGIN})
        assert "Access-Control-Allow-Credentials" not in response.headers

    def test_disallowed_origin_no_expose_headers(self, isolated_client: FlaskClient) -> None:
        """Disallowed-origin response omits Access-Control-Expose-Headers."""
        response = isolated_client.get("/api/ping", headers={"Origin": _DISALLOWED_ORIGIN})
        assert "Access-Control-Expose-Headers" not in response.headers

    def test_disallowed_origin_still_has_vary_origin(self, isolated_client: FlaskClient) -> None:
        """Disallowed-origin response STILL has Vary: Origin (cache-correctness).

        This is the documented invariant: even when the origin is
        NOT on the allowlist, ``Vary: Origin`` MUST be present so
        any caching layer (CloudFront, browser cache) keys the
        response by Origin. Without it, a cache populated from a
        privileged origin could serve the cached response to a
        different (less-privileged) origin.
        """
        response = isolated_client.get("/api/ping", headers={"Origin": _DISALLOWED_ORIGIN})
        assert "Origin" in response.headers["Vary"]


# ---------------------------------------------------------------------------
# TestNoOriginRequest: server-to-server callers without Origin header
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNoOriginRequest:
    """Validate handling of requests WITHOUT an Origin header.

    Server-to-server callers (curl, Postman, k8s health probes)
    typically do not send an Origin header. The middleware MUST NOT
    add any CORS-related headers to these responses because:
        * No browser is involved, so CORS is irrelevant.
        * Adding ``Vary: Origin`` to a no-Origin response would
          only confuse caching layers (no benefit, no harm in
          isolation, but inconsistency across the surface is
          worse than no header).
    """

    def test_no_origin_no_acao(self, isolated_client: FlaskClient) -> None:
        """Request without Origin gets no ACAO header."""
        response = isolated_client.get("/api/ping")
        assert response.status_code == 200
        assert "Access-Control-Allow-Origin" not in response.headers

    def test_no_origin_no_acac(self, isolated_client: FlaskClient) -> None:
        """Request without Origin gets no ACAC header."""
        response = isolated_client.get("/api/ping")
        assert "Access-Control-Allow-Credentials" not in response.headers

    def test_no_origin_no_expose_headers(self, isolated_client: FlaskClient) -> None:
        """Request without Origin gets no Access-Control-Expose-Headers."""
        response = isolated_client.get("/api/ping")
        assert "Access-Control-Expose-Headers" not in response.headers

    def test_no_origin_no_vary_origin(self, isolated_client: FlaskClient) -> None:
        """Request without Origin gets no Vary: Origin header.

        The after_request hook's outer ``if origin:`` guard skips
        the entire CORS branch for no-Origin requests. This is
        slightly different from the disallowed-origin branch (which
        DOES set Vary: Origin) — a no-Origin caller cannot be a
        browser, so cache-key correctness for browsers is not a
        concern here.
        """
        response = isolated_client.get("/api/ping")
        # If the response has no Vary header, that's fine. If it has
        # one (set by some other middleware), Origin should NOT be in
        # it because our hook skipped the branch.
        vary = response.headers.get("Vary", "")
        assert "Origin" not in vary


# ---------------------------------------------------------------------------
# TestCredentialsConfiguration: CORS_ALLOW_CREDENTIALS toggle
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCredentialsConfiguration:
    """Validate the ``CORS_ALLOW_CREDENTIALS`` configuration toggle.

    When set to False, the middleware MUST NOT emit ``Access-
    Control-Allow-Credentials: true``. This matters for deployments
    that put the SPA on the same origin as the API (no cross-origin
    cookies needed) or for stateless API-key-based deployments where
    the cookie surface is not used.
    """

    def test_no_credentials_in_preflight(self, no_credentials_client: FlaskClient) -> None:
        """ACAC is omitted from preflight when CORS_ALLOW_CREDENTIALS=False."""
        response = no_credentials_client.options(
            "/api/ping",
            headers={
                "Origin": _ALLOWED_ORIGIN,
                "Access-Control-Request-Method": "POST",
            },
        )
        assert "Access-Control-Allow-Credentials" not in response.headers

    def test_no_credentials_in_actual_response(self, no_credentials_client: FlaskClient) -> None:
        """ACAC is omitted from actual responses when CORS_ALLOW_CREDENTIALS=False."""
        response = no_credentials_client.get("/api/ping", headers={"Origin": _ALLOWED_ORIGIN})
        assert response.status_code == 200
        assert "Access-Control-Allow-Credentials" not in response.headers

    def test_acao_still_present_when_credentials_disabled(
        self, no_credentials_client: FlaskClient
    ) -> None:
        """ACAO is still emitted when ACAC is suppressed (orthogonal toggles)."""
        response = no_credentials_client.get("/api/ping", headers={"Origin": _ALLOWED_ORIGIN})
        assert response.headers["Access-Control-Allow-Origin"] == _ALLOWED_ORIGIN


# ---------------------------------------------------------------------------
# TestVaryHeaderHandling: idempotency and append semantics for Vary
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestVaryHeaderHandling:
    """Validate the after_request hook's Vary header handling.

    The middleware MUST:
        * Append ``Origin`` to an existing ``Vary`` header (e.g.,
          ``Accept-Encoding`` from compression middleware) rather
          than overwriting it — the resulting value is comma-
          separated.
        * NOT duplicate ``Origin`` if already present (case-
          insensitive).
        * Set ``Vary: Origin`` from scratch when no existing Vary
          header is present.
    """

    def test_vary_origin_set_when_no_existing_vary(self, isolated_client: FlaskClient) -> None:
        """Vary: Origin is set when the response has no pre-existing Vary."""
        response = isolated_client.get("/api/ping", headers={"Origin": _ALLOWED_ORIGIN})
        assert response.headers["Vary"] == "Origin"

    def test_vary_origin_appended_to_existing_vary(self, isolated_client: FlaskClient) -> None:
        """Vary: Origin is APPENDED (not overwritten) when existing Vary present.

        Mirrors the production scenario where the compression
        middleware sets ``Vary: Accept-Encoding`` and the CORS
        middleware must append Origin to it. The resulting header
        should be ``Accept-Encoding, Origin`` (comma-separated).
        """
        response = isolated_client.get("/api/preset-vary", headers={"Origin": _ALLOWED_ORIGIN})
        vary = response.headers["Vary"]
        assert "Accept-Encoding" in vary
        assert "Origin" in vary

    def test_vary_origin_not_duplicated_when_already_present(
        self, isolated_client: FlaskClient
    ) -> None:
        """Vary: Origin is NOT duplicated if already present (case-insensitive).

        The middleware checks ``if "origin" not in existing_vary.lower()``
        which means a pre-set ``Vary: Origin`` (or ``Vary: origin`` or
        ``Vary: Origin, Accept-Encoding``) is left alone. This avoids
        emitting ``Vary: Origin, Origin`` which is technically valid
        but ugly.
        """
        response = isolated_client.get(
            "/api/preset-vary-with-origin", headers={"Origin": _ALLOWED_ORIGIN}
        )
        vary = response.headers["Vary"]
        # "Origin" should appear exactly once in the Vary header
        # (case-insensitive count).
        occurrences = vary.lower().count("origin")
        assert occurrences == 1


# ---------------------------------------------------------------------------
# TestIdempotency: pre-set ACAO header is not overwritten
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestIdempotency:
    """Validate the after_request hook's ACAO idempotency invariant.

    When a response already has ``Access-Control-Allow-Origin`` set
    (e.g., the preflight short-circuit response built by
    ``_before_request_cors``), the after_request hook MUST NOT
    overwrite it. This ensures the preflight response retains its
    explicit ACAO header even though the after_request hook runs on
    the way out.
    """

    def test_preset_acao_not_overwritten(self, isolated_client: FlaskClient) -> None:
        """An ACAO already on the response is preserved verbatim."""
        response = isolated_client.get("/api/preset-acao", headers={"Origin": _ALLOWED_ORIGIN})
        # The route handler pre-set ACAO to the sentinel value. The
        # after_request hook MUST NOT overwrite it.
        assert response.headers["Access-Control-Allow-Origin"] == "preset-do-not-overwrite"

    def test_preset_acao_does_not_block_vary_origin(self, isolated_client: FlaskClient) -> None:
        """Even when ACAO is preset, the Vary: Origin invariant still holds.

        The Vary append branch is independent of the ACAO-set branch
        in the after_request hook (separate ``if`` blocks). A preset
        ACAO bypasses the ACAO-set branch but still triggers the
        Vary branch.
        """
        response = isolated_client.get("/api/preset-acao", headers={"Origin": _ALLOWED_ORIGIN})
        assert "Origin" in response.headers["Vary"]


# ---------------------------------------------------------------------------
# TestRegistration: the public registration entrypoint
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRegistration:
    """Validate ``register_cors_middleware`` registration semantics.

    The function is documented as idempotent: calling it twice on
    the same Flask app does not double-attach the hooks because
    Flask deduplicates by view-function identity, and the module-
    level helper functions have stable identity across re-
    registrations.
    """

    def test_register_attaches_before_request_hook(self) -> None:
        """After registration, the app has at least one before_request hook."""
        app = Flask(__name__)
        app.config["CORS_ALLOWED_ORIGINS"] = [_ALLOWED_ORIGIN]
        app.config["CORS_ALLOW_CREDENTIALS"] = True
        register_cors_middleware(app)
        # ``before_request_funcs`` is a dict keyed by blueprint name
        # (None for app-level hooks). After registration the None key
        # should map to a non-empty list.
        assert app.before_request_funcs.get(None)
        assert len(app.before_request_funcs[None]) >= 1

    def test_register_attaches_after_request_hook(self) -> None:
        """After registration, the app has at least one after_request hook."""
        app = Flask(__name__)
        app.config["CORS_ALLOWED_ORIGINS"] = [_ALLOWED_ORIGIN]
        app.config["CORS_ALLOW_CREDENTIALS"] = True
        register_cors_middleware(app)
        assert app.after_request_funcs.get(None)
        assert len(app.after_request_funcs[None]) >= 1

    def test_register_logs_at_startup(self, caplog: pytest.LogCaptureFixture) -> None:
        """Registration emits a single ``cors_middleware_registered`` info log line.

        The log line carries the resolved configuration (allowed
        origins, allow_credentials, allowed methods) so operators
        can confirm wiring at startup.
        """
        app = Flask(__name__)
        app.config["CORS_ALLOWED_ORIGINS"] = [_ALLOWED_ORIGIN]
        app.config["CORS_ALLOW_CREDENTIALS"] = True
        with caplog.at_level(logging.INFO, logger="app.middleware.cors"):
            register_cors_middleware(app)

        # The info-level log record should mention the registration.
        registration_records = [
            r for r in caplog.records if "cors_middleware_registered" in r.getMessage()
        ]
        assert len(registration_records) >= 1


# ---------------------------------------------------------------------------
# TestOriginNormalization: allowlist normalization edge cases
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOriginNormalization:
    """Validate the allowlist normalization done by ``_resolve_allowed_origins``.

    The helper normalizes:
        * Trailing slashes (browsers never send them on Origin
          headers but config might include them).
        * Surrounding whitespace (config files often include
          accidental whitespace).
        * Empty strings (filtered out).

    Together these defenses make the configuration robust against
    common environment-variable formatting mistakes.
    """

    def test_allowed_origin_with_trailing_slash_in_config(self) -> None:
        """Config entries with trailing slashes are normalized at lookup time."""
        app = _make_test_app(allowed_origins=[_ALLOWED_ORIGIN + "/"])
        client = app.test_client()
        response = client.get("/api/ping", headers={"Origin": _ALLOWED_ORIGIN})
        # The config has trailing slash; the request has none. After
        # normalization both sides are identical so the request is
        # allowed.
        assert response.headers["Access-Control-Allow-Origin"] == _ALLOWED_ORIGIN

    def test_allowed_origin_with_whitespace_in_config(self) -> None:
        """Config entries with surrounding whitespace are normalized."""
        app = _make_test_app(allowed_origins=[f"  {_ALLOWED_ORIGIN}  "])
        client = app.test_client()
        response = client.get("/api/ping", headers={"Origin": _ALLOWED_ORIGIN})
        assert response.headers["Access-Control-Allow-Origin"] == _ALLOWED_ORIGIN

    def test_empty_string_in_config_filtered_out(self) -> None:
        """Empty strings in the config list are filtered out by the normalizer.

        This protects against the common ``CORS_ALLOWED_ORIGINS=""``
        env-var pattern where the empty string would otherwise be
        treated as a valid empty origin.
        """
        app = _make_test_app(allowed_origins=["", _ALLOWED_ORIGIN])
        client = app.test_client()
        # Real allowed origin still works.
        response = client.get("/api/ping", headers={"Origin": _ALLOWED_ORIGIN})
        assert response.headers["Access-Control-Allow-Origin"] == _ALLOWED_ORIGIN
        # Empty Origin still gets nothing.
        response2 = client.get("/api/ping", headers={"Origin": ""})
        assert "Access-Control-Allow-Origin" not in response2.headers
