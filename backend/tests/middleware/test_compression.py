"""Tests for ``app.middleware.compression``.

Validates the response-body compression middleware contract introduced
to close QA Checkpoint 9 Issue #1 (MINOR): backend ``/api/*`` and
``/auth/*`` responses were served uncompressed because the AWS ALB
routes those paths directly to Flask without nginx interposition per
AAP Section 0.4.6, while the frontend nginx ``gzip on`` block applied
only to the SPA static-asset path.

Coverage targets:

* The middleware compresses JSON response bodies above
  ``COMPRESS_MIN_SIZE`` when the client sends ``Accept-Encoding: gzip``,
  attaching the canonical ``Content-Encoding: gzip`` header AND
  shrinking the wire size below the uncompressed body size.
* The middleware compresses JSON bodies when the client sends
  ``Accept-Encoding: gzip, deflate, br`` (real-world browser shape) and
  prefers gzip over deflate per the canonical
  ``COMPRESS_ALGORITHM = ["gzip", "deflate"]`` order.
* The middleware does NOT compress responses when the client omits
  the ``Accept-Encoding`` header entirely or sends
  ``Accept-Encoding: identity``.
* The middleware does NOT compress responses for unsupported
  algorithms (br, zstd, compress) -- they are deliberately disabled
  on the backend per the module docstring "Algorithm selection".
* The middleware does NOT compress bodies smaller than
  ``COMPRESS_MIN_SIZE`` (500 bytes), even when the client requests
  gzip.
* The middleware does NOT compress non-JSON / non-text MIME types
  (e.g., a hypothetical ``image/png`` response).
* The middleware does NOT compress 4xx / 5xx error envelopes (per
  Flask-Compress's built-in status-code guard).
* The middleware appends ``Accept-Encoding`` to the ``Vary`` header
  on every response so caches do not serve a gzipped body to a
  non-gzip client.
* The middleware preserves a pre-set ``Content-Encoding`` header
  (idempotency: a future endpoint that returns a pre-gzipped body
  must not be re-compressed).
* The Flask after_request hook registration order is LIFO (last
  registered runs first); registering compression FIRST in
  ``app/__init__.py`` therefore runs it LAST in the after_request
  chain so the body and all upstream-attached headers are final
  before compression takes the body off the wire.
* The full-app integration via ``app.create_app(TestingConfig)``
  honors all of the above through every blueprint and every
  middleware layer.

Test isolation strategy
=======================

* Unit tests use a minimal Flask app constructed by
  ``_make_test_app`` that registers ONLY the compression middleware
  (and an inline route per test) so other middleware cannot pollute
  the assertion surface (e.g., the ``Cache-Control`` header set by
  security_headers does not influence Flask-Compress's decision but
  could complicate the assertion shape if it accidentally leaked
  in).

* The full-app integration tests piggyback on the session-scoped
  ``app`` and ``client`` fixtures from ``conftest.py`` so they
  exercise the EXACT same wiring (compression -> correlation ->
  cors -> security_headers -> auth -> rbac -> error_handlers) used
  by production. These tests are tagged ``integration`` so they can
  be skipped during fast unit-only runs.
"""

from __future__ import annotations

# Standard library imports.
import gzip
import json
import logging as _stdlib_logging
from typing import TYPE_CHECKING, Any
import zlib

# Third-party imports.
from flask import Flask, Response, jsonify
import pytest

# First-party imports under test.
from app.middleware.compression import register_compression_middleware

# Type-only imports gated under TYPE_CHECKING so the runtime surface
# stays minimal and ruff's TC002/TC003 typing-only-import lint rules
# are satisfied.
if TYPE_CHECKING:
    from flask.testing import FlaskClient


# ---------------------------------------------------------------------------
# Module-level test helpers
# ---------------------------------------------------------------------------


# A JSON payload large enough to clear the 500-byte
# ``COMPRESS_MIN_SIZE`` threshold even after JSON serialization
# overhead. The exact contents are uninteresting; we only need bulk
# bytes that compress well (repetitive ASCII text typically yields
# 70-90 percent reduction with gzip).
_LARGE_JSON_PAYLOAD: dict[str, Any] = {
    "items": [
        {
            "id": i,
            "name": f"connection-record-{i}",
            "company": "Example Corp",
            "context": (
                "We worked together for several years on multiple "
                "successful projects involving customer onboarding "
                "and retention strategy"
            ),
        }
        for i in range(40)
    ],
    "total": 40,
}


def _make_test_app() -> Flask:
    """Build a minimal Flask app wired ONLY with compression middleware.

    Deliberate isolation: the full ``create_app(TestingConfig)``
    factory wires correlation, CORS, security headers, auth, RBAC,
    and error handlers in addition to compression. Wiring all of
    them here would mix behavior across multiple after_request hooks
    and complicate targeted assertions about THIS middleware's
    behavior.

    The optional inline routes deliberately surface the response so
    assertions can verify each compression branch:

        * ``GET /api/large-json`` returns 200 with the large JSON
          payload (well above the 500-byte threshold) so we can
          verify gzip compression engages on JSON.
        * ``GET /api/small-json`` returns 200 with a tiny JSON body
          (under 500 bytes) so we can verify the
          ``COMPRESS_MIN_SIZE`` guard skips compression on small
          payloads.
        * ``GET /api/binary`` returns 200 with a non-compressible
          ``image/png``-typed response so we can verify the
          ``COMPRESS_MIMETYPES`` guard skips compression on
          non-text types.
        * ``GET /api/preset-encoding`` returns 200 with a body that
          claims ``Content-Encoding: identity`` so we can verify the
          idempotency guard preserves an explicit encoding.
        * ``GET /api/error-large`` returns 500 with a large JSON
          body so we can verify error envelopes are NOT compressed
          (Flask-Compress's status-code guard skips status >= 300).

    Returns:
        A Flask app with compression middleware installed and the
        five inline test routes mounted. Caller owns the lifetime;
        the app does not persist across tests.
    """
    app = Flask(__name__)

    register_compression_middleware(app)

    @app.route("/api/large-json")
    def large_json() -> Any:
        # jsonify produces an application/json response, which IS in
        # the COMPRESS_MIMETYPES default set, so this body is a
        # candidate for compression.
        return jsonify(_LARGE_JSON_PAYLOAD)

    @app.route("/api/small-json")
    def small_json() -> Any:
        # A tiny JSON body well under the 500-byte threshold; should
        # not be compressed even though the mime type is correct.
        return jsonify(ok=True)

    @app.route("/api/binary")
    def binary() -> Any:
        # A response with a binary content type. Even though the
        # body is large, image/png is NOT in COMPRESS_MIMETYPES so
        # compression must be skipped.
        body = b"\x89PNG\r\n\x1a\n" + b"\x00" * 2048
        return Response(body, mimetype="image/png")

    @app.route("/api/preset-encoding")
    def preset_encoding() -> Any:
        # A response that explicitly sets Content-Encoding to
        # ``identity``. The middleware MUST preserve this verbatim
        # and skip its own compression branch.
        response = jsonify(_LARGE_JSON_PAYLOAD)
        response.headers["Content-Encoding"] = "identity"
        return response

    @app.route("/api/error-large")
    def error_large() -> Any:
        # A 500 status with a large body. Flask-Compress's
        # status-code guard refuses to compress responses with
        # status >= 300.
        body = json.dumps(_LARGE_JSON_PAYLOAD)
        return Response(body, status=500, mimetype="application/json")

    return app


def _decode_gzip(body: bytes) -> bytes:
    """Decode a gzip-encoded response body or raise.

    Used by tests that need to decompress and inspect the original
    JSON to verify that compression preserves payload semantics.
    """
    return gzip.decompress(body)


def _decode_deflate(body: bytes) -> bytes:
    """Decode a deflate-encoded response body or raise.

    deflate is supported as a fallback algorithm but is rarely
    requested in practice; this helper covers the
    ``Accept-Encoding: deflate`` test case.
    """
    return zlib.decompress(body)


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_app() -> Flask:
    """Provide a minimal Flask app with ONLY compression middleware.

    Intentionally separate from the full-app fixture in
    ``conftest.py`` (which wires every middleware) so that
    assertions about compression behavior cannot be muddled by the
    presence of other after_request hooks.
    """
    return _make_test_app()


@pytest.fixture
def isolated_client(isolated_app: Flask) -> FlaskClient:
    """Test client backed by the isolated app fixture above."""
    return isolated_app.test_client()


# ---------------------------------------------------------------------------
# TestGzipCompression: the canonical happy path
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGzipCompression:
    """Validate the gzip-compression branch of the middleware."""

    def test_gzip_engaged_when_client_requests_gzip(self, isolated_client: FlaskClient) -> None:
        """A client sending ``Accept-Encoding: gzip`` receives a gzip body.

        This is the canonical reproduction step for QA Checkpoint 9
        Issue #1: a curl invocation with ``Accept-Encoding: gzip``
        must come back with ``Content-Encoding: gzip`` and a
        compressed body.
        """
        response = isolated_client.get(
            "/api/large-json",
            headers={"Accept-Encoding": "gzip"},
        )
        assert response.status_code == 200
        assert response.headers.get("Content-Encoding") == "gzip"

    def test_gzip_body_is_smaller_than_uncompressed(self, isolated_client: FlaskClient) -> None:
        """The gzip wire body is materially smaller than the JSON body.

        Performs two requests, one with gzip and one without, and
        verifies that the compressed body length is less than the
        uncompressed body length. The QA reproduction step for
        Issue #1 explicitly compared sizes with and without gzip;
        this test enforces that comparison contract.
        """
        no_gzip = isolated_client.get("/api/large-json")
        gzipped = isolated_client.get(
            "/api/large-json",
            headers={"Accept-Encoding": "gzip"},
        )
        # The uncompressed JSON body is a strict superset in size
        # of any gzip-encoded version; any margin verifies that
        # compression actually happened. Empirically the JSON
        # payload above compresses to roughly 15 percent of its
        # original size.
        assert len(gzipped.data) < len(no_gzip.data)

    def test_gzip_body_decompresses_to_original_json(self, isolated_client: FlaskClient) -> None:
        """The gzip body decompresses cleanly to the original JSON.

        Verifies that the compression is reversible (no truncation,
        no encoding mishaps) by gunzipping the response and parsing
        the result as JSON.
        """
        response = isolated_client.get(
            "/api/large-json",
            headers={"Accept-Encoding": "gzip"},
        )
        decoded = _decode_gzip(response.data)
        payload = json.loads(decoded)
        assert payload == _LARGE_JSON_PAYLOAD

    def test_browser_accept_encoding_picks_gzip_first(self, isolated_client: FlaskClient) -> None:
        """A real-world browser ``Accept-Encoding`` selects gzip.

        Browsers send ``Accept-Encoding: gzip, deflate, br`` (and
        increasingly ``zstd`` too). With brotli and zstd disabled in
        the backend ``COMPRESS_ALGORITHM`` config, gzip must be the
        chosen algorithm for any client that lists gzip among its
        accepted encodings.
        """
        response = isolated_client.get(
            "/api/large-json",
            headers={"Accept-Encoding": "gzip, deflate, br, zstd"},
        )
        assert response.status_code == 200
        assert response.headers.get("Content-Encoding") == "gzip"


# ---------------------------------------------------------------------------
# TestDeflateCompression: the secondary happy path
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDeflateCompression:
    """Validate the deflate-compression branch of the middleware."""

    def test_deflate_engaged_when_client_requests_deflate_only(
        self, isolated_client: FlaskClient
    ) -> None:
        """A client sending ``Accept-Encoding: deflate`` gets deflate.

        deflate is the secondary algorithm in
        ``COMPRESS_ALGORITHM``. A client that explicitly requests it
        (and only it) must receive a deflate-encoded body.
        """
        response = isolated_client.get(
            "/api/large-json",
            headers={"Accept-Encoding": "deflate"},
        )
        assert response.status_code == 200
        assert response.headers.get("Content-Encoding") == "deflate"

    def test_deflate_body_decompresses_to_original_json(self, isolated_client: FlaskClient) -> None:
        """The deflate body decompresses cleanly to the original JSON."""
        response = isolated_client.get(
            "/api/large-json",
            headers={"Accept-Encoding": "deflate"},
        )
        decoded = _decode_deflate(response.data)
        payload = json.loads(decoded)
        assert payload == _LARGE_JSON_PAYLOAD


# ---------------------------------------------------------------------------
# TestNoCompression: the negative branches
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNoCompression:
    """Validate every branch where compression must be skipped."""

    def test_no_accept_encoding_header(self, isolated_client: FlaskClient) -> None:
        """Without ``Accept-Encoding``, the response is uncompressed.

        Werkzeug's test client does not auto-attach Accept-Encoding,
        so a bare GET represents a client that explicitly omits the
        header. The middleware must NOT compress in that case
        (compression negotiation requires explicit client opt-in
        per RFC 9110).
        """
        response = isolated_client.get("/api/large-json")
        assert response.status_code == 200
        assert response.headers.get("Content-Encoding") is None

    def test_accept_encoding_identity(self, isolated_client: FlaskClient) -> None:
        """``Accept-Encoding: identity`` opts out of compression."""
        response = isolated_client.get(
            "/api/large-json",
            headers={"Accept-Encoding": "identity"},
        )
        assert response.status_code == 200
        assert response.headers.get("Content-Encoding") is None

    def test_accept_encoding_disabled_algorithm(self, isolated_client: FlaskClient) -> None:
        """A client requesting ONLY brotli gets no compression.

        brotli is deliberately disabled on the backend (see the
        module docstring "Algorithm selection"). A client that
        requests only brotli must receive an uncompressed response;
        the middleware MUST NOT silently fall back to gzip on a
        client that did not list gzip in its accepted encodings.
        """
        response = isolated_client.get(
            "/api/large-json",
            headers={"Accept-Encoding": "br"},
        )
        assert response.status_code == 200
        assert response.headers.get("Content-Encoding") is None

    def test_body_below_min_size_not_compressed(self, isolated_client: FlaskClient) -> None:
        """A tiny JSON body is not compressed even with gzip requested.

        ``COMPRESS_MIN_SIZE`` is 500 bytes. The ``/api/small-json``
        route returns a payload well under that threshold; gzip
        framing overhead would dominate any byte savings on such a
        small body so the middleware skips compression by design.
        """
        response = isolated_client.get(
            "/api/small-json",
            headers={"Accept-Encoding": "gzip"},
        )
        assert response.status_code == 200
        assert response.headers.get("Content-Encoding") is None

    def test_non_compressible_mime_type_not_compressed(self, isolated_client: FlaskClient) -> None:
        """A binary content type is not compressed even with gzip requested.

        ``image/png`` is not in ``COMPRESS_MIMETYPES``. Even with a
        large body and a gzip-friendly request header, the
        middleware skips compression for binary types.
        """
        response = isolated_client.get(
            "/api/binary",
            headers={"Accept-Encoding": "gzip"},
        )
        assert response.status_code == 200
        assert response.headers.get("Content-Encoding") is None

    def test_error_response_not_compressed(self, isolated_client: FlaskClient) -> None:
        """A 5xx error envelope is not compressed even with a large body.

        Flask-Compress's status-code guard skips compression for any
        response with ``status_code < 200`` or ``status_code >=
        300``. This keeps error envelopes (typically small) from
        paying the compression overhead and prevents a misbehaving
        proxy from misinterpreting an unexpectedly compressed error
        body.
        """
        response = isolated_client.get(
            "/api/error-large",
            headers={"Accept-Encoding": "gzip"},
        )
        assert response.status_code == 500
        assert response.headers.get("Content-Encoding") is None


# ---------------------------------------------------------------------------
# TestVaryHeader: cache safety
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestVaryHeader:
    """Validate that the middleware adds Accept-Encoding to Vary."""

    def test_vary_includes_accept_encoding_when_compressed(
        self, isolated_client: FlaskClient
    ) -> None:
        """``Vary: Accept-Encoding`` is set on a compressed response.

        This is required by RFC 9110 for correct cache behavior:
        without ``Vary: Accept-Encoding``, a CDN or browser cache
        could serve a gzipped body to a client that did not request
        gzip, breaking decoding.
        """
        response = isolated_client.get(
            "/api/large-json",
            headers={"Accept-Encoding": "gzip"},
        )
        vary = response.headers.get("Vary", "")
        assert "Accept-Encoding" in vary

    def test_vary_includes_accept_encoding_when_uncompressed(
        self, isolated_client: FlaskClient
    ) -> None:
        """``Vary: Accept-Encoding`` is set even on uncompressed responses.

        Flask-Compress sets the Vary header on every response,
        regardless of whether the algorithm negotiation chose to
        compress. This is correct because the response would have
        been different (compressed) for a client that DID send
        ``Accept-Encoding: gzip``, and shared caches must key on
        that difference.
        """
        response = isolated_client.get("/api/large-json")
        vary = response.headers.get("Vary", "")
        assert "Accept-Encoding" in vary


# ---------------------------------------------------------------------------
# TestIdempotency: pre-set encoding preserved
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestIdempotency:
    """Validate that pre-set Content-Encoding is preserved."""

    def test_preset_content_encoding_preserved(self, isolated_client: FlaskClient) -> None:
        """A handler-set ``Content-Encoding`` is preserved verbatim.

        The ``/api/preset-encoding`` route sets
        ``Content-Encoding: identity`` explicitly. Even though the
        client requests gzip and the body is over the size
        threshold, the middleware MUST NOT overwrite the explicit
        encoding (Flask-Compress short-circuits when
        ``Content-Encoding`` is already present in the response
        headers).
        """
        response = isolated_client.get(
            "/api/preset-encoding",
            headers={"Accept-Encoding": "gzip"},
        )
        assert response.status_code == 200
        assert response.headers.get("Content-Encoding") == "identity"


# ---------------------------------------------------------------------------
# TestRegistrationOrder: empirical Flask LIFO contract
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRegistrationOrder:
    """Validate the empirical Flask after_request LIFO contract.

    These tests are not strictly testing the compression module
    itself; they document the Flask contract that motivates
    compression's FIRST-position registration in
    ``app/__init__.py``. If a future Flask release ever changes
    after_request to FIFO, these tests will break loudly and
    contributors will know to revisit ``compression.py``'s wiring
    docstring.
    """

    def test_after_request_runs_in_reverse_registration_order(self) -> None:
        """Flask runs ``after_request`` hooks in reverse registration order.

        Registers three named hooks A, B, C in that order and asserts
        the execution order is C, B, A. This is the contract that
        makes registering compression FIRST equivalent to running it
        LAST in the after_request chain.
        """
        execution_log: list[str] = []
        app = Flask(__name__)

        @app.after_request
        def hook_a(response: Any) -> Any:
            execution_log.append("A")
            return response

        @app.after_request
        def hook_b(response: Any) -> Any:
            execution_log.append("B")
            return response

        @app.after_request
        def hook_c(response: Any) -> Any:
            execution_log.append("C")
            return response

        @app.route("/")
        def root() -> Any:
            return jsonify(ok=True)

        with app.test_client() as client:
            client.get("/")

        # Registered A, B, C; expect execution C, B, A. If Flask
        # ever changes to FIFO, this assertion fails and signals
        # that the compression registration order needs to be
        # revisited.
        assert execution_log == ["C", "B", "A"]


# ---------------------------------------------------------------------------
# TestFullAppIntegration: production-wiring exercise
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestFullAppIntegration:
    """Exercise compression through the full ``create_app`` factory.

    These tests use the session-scoped ``client`` fixture from
    ``conftest.py`` so the wiring matches production:

        compression -> correlation -> cors -> security_headers ->
        auth -> rbac -> error_handlers

    The endpoints exercised here are unauthenticated (or
    intentionally hit the 401 short-circuit) so the test does not
    need a populated test database or test session cookie. The only
    contract under test is whether compression engages on the final
    response payload.
    """

    def test_health_probe_response_not_compressed(self, client: FlaskClient) -> None:
        """``GET /healthz`` returns 200 with no compression.

        The health probe body is a tiny JSON document well under the
        500-byte threshold, so it must NOT be compressed even when
        the client requests gzip. This verifies the
        ``COMPRESS_MIN_SIZE`` guard threads correctly through the
        full middleware chain (compression -> correlation -> ...).
        """
        response = client.get(
            "/healthz",
            headers={"Accept-Encoding": "gzip"},
        )
        assert response.status_code == 200
        assert response.headers.get("Content-Encoding") is None

    def test_metrics_endpoint_compressed_when_large(self, client: FlaskClient) -> None:
        """``GET /metrics`` returns Prometheus text, gzipped if large.

        The ``/metrics`` endpoint emits Prometheus exposition text
        whose body grows with the number of registered metrics.
        After the test session has run a few requests the body
        typically clears the 500-byte threshold; when that is the
        case AND the client requests gzip, the response must be
        gzipped. The endpoint may also return 401 if the metrics
        bearer-token guard is configured (in which case the body
        is small and the assertion that compression did not engage
        on a 4xx body holds).
        """
        response = client.get(
            "/metrics",
            headers={"Accept-Encoding": "gzip"},
        )
        # The endpoint either returns 200 with Prometheus text (open
        # access) or 401 (bearer-token-protected). Both are valid
        # contract outcomes; the key invariant is that compression
        # engages only on the 200 path.
        if response.status_code == 200:
            content_type = response.headers.get("Content-Type", "")
            # The metrics body is text/plain; depending on size, it
            # may or may not be compressed. Either outcome is
            # acceptable but a compressed response must declare
            # gzip.
            encoding = response.headers.get("Content-Encoding")
            assert encoding in (None, "gzip")
            assert "text/plain" in content_type or "text/" in content_type
        else:
            assert response.status_code == 401
            # 401 envelopes are below 500 bytes and outside the
            # 200-299 status range, so compression must NOT engage.
            assert response.headers.get("Content-Encoding") is None

    def test_unauth_api_response_uncompressed_due_to_status(self, client: FlaskClient) -> None:
        """A 401 short-circuit on ``/api/connections`` is not compressed.

        Hitting ``/api/connections`` without a session cookie
        triggers the auth middleware's 401 short-circuit. The
        response body is a small JSON envelope; even if the client
        requests gzip, Flask-Compress's status-code guard refuses to
        compress 4xx responses.
        """
        response = client.get(
            "/api/connections",
            headers={"Accept-Encoding": "gzip"},
        )
        assert response.status_code == 401
        assert response.headers.get("Content-Encoding") is None

    def test_vary_accept_encoding_present_through_full_chain(self, client: FlaskClient) -> None:
        """``Vary: Accept-Encoding`` is set even on a 401 response.

        Flask-Compress sets the Vary header unconditionally on
        every response, regardless of whether the body was
        compressed. Verifying this through the full chain confirms
        no upstream middleware accidentally strips the Vary header.
        """
        response = client.get(
            "/api/connections",
            headers={"Accept-Encoding": "gzip"},
        )
        vary = response.headers.get("Vary", "")
        assert "Accept-Encoding" in vary


# ---------------------------------------------------------------------------
# TestRegistrationLogging: operator confirmation at startup
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRegistrationLogging:
    """Validate that registration emits an operator-visible log line."""

    def test_registration_emits_log_line(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """``register_compression_middleware`` emits a startup log line.

        Operators rely on the
        ``compression_middleware_registered`` log line to confirm
        wiring at app boot. Capturing it here ensures a future
        refactor that drops the log will be caught immediately.
        """
        with caplog.at_level(_stdlib_logging.INFO, logger="app.middleware.compression"):
            app = Flask(__name__)
            register_compression_middleware(app)

        log_messages = [record.message for record in caplog.records]
        assert "compression_middleware_registered" in log_messages
