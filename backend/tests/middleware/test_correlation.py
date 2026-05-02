"""Tests for ``app.middleware.correlation``.

Validates the per-request correlation-ID middleware contract:

* Inbound ``X-Correlation-Id`` headers matching the validation regex
  are preserved verbatim.
* Inbound malformed headers are REPLACED with a fresh UUID4 (defensive
  validation against log-injection attacks).
* Missing inbound headers cause a fresh UUID4 to be generated.
* The resolved value is stashed on ``g.correlation_id``, bound into
  structlog contextvars, and (when OTel is active) set as a span
  attribute ``app.correlation_id`` plus a baggage entry.
* The ``X-Correlation-Id`` response header echoes the value back to the
  SPA - idempotent: an existing header value is left alone.
* The ``teardown_request`` hook calls
  ``structlog.contextvars.clear_contextvars`` to prevent leakage across
  requests on the same worker thread.

Per AAP Section 0.5.2 Layer 0, this middleware is registered FIRST so
all subsequent middleware and handlers inherit the bound correlation
ID in their structlog context.

Test isolation strategy:

* Each test gets a fresh ``Flask(__name__)`` instance via the
  ``test_app`` / ``test_client`` fixtures, deliberately bypassing the
  full ``create_app(TestingConfig)`` factory in ``conftest.py`` so
  other middleware (auth, rbac, error_handlers) cannot pollute
  correlation-only assertions.
* An autouse ``_isolate_structlog_contextvars`` fixture double-clears
  ``structlog.contextvars`` before AND after every test to prevent
  state bleed across tests on the same Python thread.
* OpenTelemetry interactions are mocked via
  ``unittest.mock.patch`` against the dotted module paths
  ``app.middleware.correlation.trace.get_current_span`` and
  ``app.middleware.correlation._OTEL_AVAILABLE`` so the suite runs
  fully offline without requiring an OTel collector.
"""

from __future__ import annotations

import re
import time
from typing import Any
from unittest.mock import MagicMock, patch
import uuid

from flask import Flask, g, jsonify
import pytest
import structlog

from app.middleware.correlation import (
    CORRELATION_HEADER_NAME,
    register_correlation_middleware,
)

# ---------------------------------------------------------------------------
# Module-level test helpers
# ---------------------------------------------------------------------------


def _make_test_app(register_routes: bool = True) -> Flask:
    """Build a minimal Flask app wired ONLY with correlation middleware.

    Pure isolation: we deliberately avoid the full ``create_app()``
    factory so other middleware (auth, rbac, error_handlers) cannot
    pollute these tests. This helper returns a fresh app instance per
    test caller.

    The optional inline routes deliberately surface internal
    middleware state via the response body so assertions can verify
    binding without relying on private hooks:

    - ``GET /ping`` returns the value bound on ``g.correlation_id``
      AND the value present in ``structlog.contextvars`` at handler
      execution time. This proves the before_request hook stashed the
      value on g AND bound it into structlog contextvars before the
      handler ran.
    - ``GET /header-set`` pre-sets the ``X-Correlation-Id`` response
      header to a sentinel value so we can verify the after_request
      hook is idempotent (does NOT overwrite a pre-set header).
    - ``GET /raises`` raises a RuntimeError to exercise the
      error-response path.

    Args:
        register_routes: When True (default), register the three
            inline test routes described above. Set to False for
            registration-only tests that don't need request-cycle
            coverage.

    Returns:
        A fully-wired Flask app instance with correlation middleware
        installed. Caller owns the lifetime; the app does not persist
        across tests.
    """
    app = Flask(__name__)
    register_correlation_middleware(app)

    if register_routes:

        @app.route("/ping")
        def ping() -> Any:
            return jsonify(
                correlation_id=getattr(g, "correlation_id", None),
                contextvars_correlation_id=structlog.contextvars.get_contextvars().get(
                    "correlation_id"
                ),
            )

        @app.route("/header-set")
        def header_set() -> Any:
            response = jsonify(ok=True)
            # Pre-set a custom X-Correlation-Id header on the response
            # so we can verify the after_request hook does NOT
            # overwrite it (idempotency invariant).
            response.headers[CORRELATION_HEADER_NAME] = "preset-value-do-not-overwrite"
            return response

        @app.route("/raises")
        def raises() -> Any:
            raise RuntimeError("simulated failure")

    return app


# Compile the UUID v4 pattern at module-import time so the
# ``_is_uuid4`` helper is fast even when called inside a tight loop
# (``test_inbound_with_special_chars_outside_regex_replaced`` iterates
# six times). The pattern matches the canonical RFC 4122 UUID v4
# string form: 8-4-4-4-12 hex digits with the version nibble fixed at
# ``4`` and the variant nibble in ``[89ab]``.
_UUID4_PATTERN: re.Pattern[str] = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def _is_uuid4(value: str) -> bool:
    """Return True iff *value* is a valid UUID v4 string.

    Uses a two-stage check: a fast regex pre-filter against the canonical
    UUID v4 form, followed by a parse via ``uuid.UUID(value)`` to confirm
    the parsed object's ``.version`` attribute equals 4. Both checks are
    needed because the regex alone does not validate the full RFC 4122
    structure (e.g., variant bits) and ``uuid.UUID`` alone accepts forms
    with surrounding braces or URN prefixes that would not appear in a
    correctly-generated correlation ID.

    Args:
        value: The candidate string to validate. May be any string;
            non-string inputs are coerced via the ``.lower()`` call
            below and would raise an ``AttributeError`` caught by the
            wrapper.

    Returns:
        True if *value* is exactly a UUID v4 in canonical form;
        False otherwise.
    """
    if not _UUID4_PATTERN.match(value.lower()):
        return False
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        return False
    return parsed.version == 4


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_structlog_contextvars() -> Any:
    """Clear structlog contextvars BEFORE and AFTER every test.

    structlog's ``contextvars`` use Python's ``contextvars`` module
    which is thread-local in WSGI workers. Pytest runs all tests in
    one thread; without explicit clearing, contextvars set by one test
    bleed into the next. The autouse fixture clears them before AND
    after each test as a defensive double-clear so:

    * No prior test's state can leak into the test under inspection.
    * No state from the test under inspection can leak into a later
      test (even if the middleware's teardown_request hook didn't run
      because the test, e.g., never made a request).

    Yields:
        None. The fixture exists purely for its side effects.
    """
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()


@pytest.fixture
def test_app() -> Flask:
    """Provide a fresh Flask app with correlation middleware registered."""
    return _make_test_app()


@pytest.fixture
def test_client(test_app: Flask) -> Any:
    """Provide a test client for the fresh app fixture."""
    return test_app.test_client()


# ---------------------------------------------------------------------------
# TestCorrelationConstant - sanity checks on the public constant
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCorrelationConstant:
    """Validate the public ``CORRELATION_HEADER_NAME`` constant.

    The constant is the canonical mixed-case form per RFC 7230
    (header names are case-insensitive on the wire, but a stable
    canonical form keeps log lines and assertions consistent).
    """

    def test_header_name_value(self) -> None:
        """The exported constant matches the canonical mixed-case form."""
        assert CORRELATION_HEADER_NAME == "X-Correlation-Id"

    def test_header_name_is_string(self) -> None:
        """Constant is a plain ``str`` (not bytes, not a custom subclass)."""
        assert isinstance(CORRELATION_HEADER_NAME, str)


# ---------------------------------------------------------------------------
# TestResolveCorrelationId - inbound header validation and UUID generation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestResolveCorrelationId:
    """Validate inbound header validation and UUID generation.

    These tests exercise ``_resolve_correlation_id`` indirectly via
    the ``/ping`` route, which echoes ``g.correlation_id`` back in the
    response body. Direct unit testing of the private helper would
    require Flask's ``test_request_context`` and is redundant with
    the integration coverage here.

    The validation regex ``^[A-Za-z0-9_\\-:.]{1,128}$`` is the
    security boundary: anything that fails it is REPLACED with a
    fresh UUID v4 to prevent untrusted client input from landing in
    log files or metric labels where injection could be problematic.
    """

    def test_valid_uuid_inbound_preserved(self, test_client: Any) -> None:
        """An inbound header matching the regex is preserved verbatim."""
        valid_id = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
        response = test_client.get("/ping", headers={CORRELATION_HEADER_NAME: valid_id})
        assert response.status_code == 200
        assert response.get_json()["correlation_id"] == valid_id

    def test_short_alphanumeric_inbound_preserved(self, test_client: Any) -> None:
        """Short alphanumeric tokens (e.g., upstream proxy IDs) are accepted."""
        valid_id = "abc123_def-456"
        response = test_client.get("/ping", headers={CORRELATION_HEADER_NAME: valid_id})
        assert response.get_json()["correlation_id"] == valid_id

    def test_dotted_inbound_preserved(self, test_client: Any) -> None:
        """Values containing dots/colons (allowed by the regex) are preserved."""
        valid_id = "trace-id:1.2.3"
        response = test_client.get("/ping", headers={CORRELATION_HEADER_NAME: valid_id})
        assert response.get_json()["correlation_id"] == valid_id

    def test_max_length_inbound_preserved(self, test_client: Any) -> None:
        """A 128-character valid value is preserved (boundary case)."""
        valid_id = "a" * 128
        response = test_client.get("/ping", headers={CORRELATION_HEADER_NAME: valid_id})
        assert response.get_json()["correlation_id"] == valid_id

    def test_oversized_inbound_replaced_with_uuid4(self, test_client: Any) -> None:
        """A value > 128 chars is REPLACED with a fresh UUID4."""
        oversized = "a" * 129
        response = test_client.get("/ping", headers={CORRELATION_HEADER_NAME: oversized})
        result = response.get_json()["correlation_id"]
        assert result != oversized
        assert _is_uuid4(result)

    def test_control_character_inbound_replaced(self, test_client: Any) -> None:
        """Inbound value with control chars is REPLACED with a fresh UUID4.

        Defends against log-injection attacks where an attacker tries
        to smuggle ANSI escape codes or embedded NUL/control bytes
        into the correlation ID and thence into log files. We use a
        NUL char which Werkzeug accepts in headers (it is not a
        newline) but our regex rejects.
        """
        control_chars_value = "evil\x00value"
        response = test_client.get("/ping", headers={CORRELATION_HEADER_NAME: control_chars_value})
        result = response.get_json()["correlation_id"]
        assert "\x00" not in result
        assert _is_uuid4(result)

    def test_whitespace_inbound_replaced(self, test_client: Any) -> None:
        """Whitespace-only inbound value is REPLACED with a fresh UUID4.

        The middleware ``.strip()``s the inbound value before regex
        matching; whitespace-only values become empty after strip and
        fall through to UUID generation.
        """
        response = test_client.get("/ping", headers={CORRELATION_HEADER_NAME: "   "})
        result = response.get_json()["correlation_id"]
        assert _is_uuid4(result)

    def test_empty_inbound_replaced(self, test_client: Any) -> None:
        """Empty inbound value is REPLACED with a fresh UUID4.

        Werkzeug may strip empty headers entirely; we still assert the
        end-to-end behaviour: a fresh UUID is generated.
        """
        response = test_client.get("/ping", headers={CORRELATION_HEADER_NAME: ""})
        result = response.get_json()["correlation_id"]
        assert _is_uuid4(result)

    def test_no_inbound_header_generates_uuid4(self, test_client: Any) -> None:
        """A request without the header gets a fresh UUID4."""
        response = test_client.get("/ping")
        result = response.get_json()["correlation_id"]
        assert _is_uuid4(result)

    def test_inbound_with_special_chars_outside_regex_replaced(self, test_client: Any) -> None:
        """Spaces, slashes, equals, plus, etc. trigger regex replacement.

        The validation regex ``^[A-Za-z0-9_\\-:.]{1,128}$`` admits only
        alphanumerics, underscore, hyphen, colon, and period. Every
        character outside that class is rejected and the value is
        replaced with a fresh UUID v4. We loop through a handful of
        representative bad values to catch any regex regression.
        """
        for bad_value in (
            "has spaces",
            "has/slash",
            "has=equals",
            "has+plus",
            "has@at",
            "has!bang",
        ):
            response = test_client.get("/ping", headers={CORRELATION_HEADER_NAME: bad_value})
            result = response.get_json()["correlation_id"]
            assert result != bad_value
            assert _is_uuid4(result)

    def test_two_requests_without_header_get_different_uuids(self, test_client: Any) -> None:
        """Each request without the header gets its OWN UUID.

        UUIDs are generated per request via ``uuid.uuid4()`` (not
        per-process or per-worker), so two consecutive requests
        without an inbound header should produce two distinct values.
        """
        first = test_client.get("/ping").get_json()["correlation_id"]
        second = test_client.get("/ping").get_json()["correlation_id"]
        assert first != second


# ---------------------------------------------------------------------------
# TestGCorrelationIdBinding - g.correlation_id stash for handlers
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGCorrelationIdBinding:
    """Validate the resolved value is stashed on ``g.correlation_id``.

    Handlers and error handlers read ``g.correlation_id`` to include
    the value in user-facing error envelopes (per
    ``app.middleware.error_handlers``). This class exercises the
    invariant by reading ``g.correlation_id`` from inside the
    ``/ping`` handler and asserting the value is present.
    """

    def test_g_populated_for_handler_access(self, test_client: Any) -> None:
        """Handler observes the correlation ID via ``g.correlation_id``."""
        valid_id = "trace-1234"
        response = test_client.get("/ping", headers={CORRELATION_HEADER_NAME: valid_id})
        # The /ping route returns g.correlation_id in its body.
        assert response.get_json()["correlation_id"] == valid_id

    def test_g_populated_when_header_absent(self, test_client: Any) -> None:
        """Handler observes a generated UUID when no header sent."""
        response = test_client.get("/ping")
        result = response.get_json()["correlation_id"]
        assert result is not None
        assert _is_uuid4(result)


# ---------------------------------------------------------------------------
# TestStructlogContextvarsBinding - structlog.contextvars binding
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestStructlogContextvarsBinding:
    """Validate structlog contextvars are bound during the request.

    The middleware calls
    ``structlog.contextvars.bind_contextvars(correlation_id=...)``
    in the before_request hook so the ``merge_contextvars`` processor
    in ``app.observability.logging`` automatically surfaces the
    correlation ID on every log line emitted within the request scope.
    The teardown_request hook then calls
    ``structlog.contextvars.clear_contextvars()`` to prevent the
    correlation ID (and any other request-bound keys, e.g.,
    ``user_id``, ``org_id``, ``role`` from the auth middleware) from
    leaking into a subsequent request handled by the same Gunicorn
    worker thread.
    """

    def test_contextvar_bound_during_request(self, test_client: Any) -> None:
        """``correlation_id`` is in structlog contextvars during the handler.

        The /ping route returns the contextvars snapshot it captured
        at handler-execution time (i.e., AFTER the before_request
        hook ran, BEFORE the after_request hook runs).
        """
        valid_id = "test-cv-binding"
        response = test_client.get("/ping", headers={CORRELATION_HEADER_NAME: valid_id})
        assert response.get_json()["contextvars_correlation_id"] == valid_id

    def test_contextvar_cleared_after_request(self, test_client: Any) -> None:
        """Contextvars are cleared by the ``teardown_request`` hook.

        After the request completes, the calling thread's contextvars
        no longer carry the correlation_id (or any other request-bound
        keys). We assert this BEFORE the autouse fixture's after-yield
        cleanup runs so the assertion specifically tests the
        middleware's teardown_request hook (not the fixture).
        """
        # Make a request to establish a correlation ID; the
        # teardown_request hook should clear contextvars before this
        # call returns.
        test_client.get("/ping", headers={CORRELATION_HEADER_NAME: "leak-check-id"})
        # If the teardown hook works, contextvars are already cleared
        # by the time we read them here. The autouse fixture's
        # after-yield cleanup runs AFTER this assertion, so any
        # leftover state would still be present at this point and
        # cause the assertion to fail.
        leftover = structlog.contextvars.get_contextvars()
        assert "correlation_id" not in leftover

    def test_contextvars_isolated_between_requests(self, test_client: Any) -> None:
        """A second request does not see the first request's contextvars.

        The teardown_request hook runs at the end of each request,
        so contextvars from the first request must be cleared before
        the before_request hook of the second request runs. The
        second request's snapshot should therefore reflect ONLY the
        second request's correlation ID.
        """
        # First request binds "first-id" then clears via teardown.
        test_client.get("/ping", headers={CORRELATION_HEADER_NAME: "first-id"})
        # Second request without the header generates a fresh UUID.
        second_response = test_client.get("/ping")
        second_id = second_response.get_json()["correlation_id"]
        assert second_id != "first-id"
        assert _is_uuid4(second_id)
        # The second request's contextvars snapshot should be
        # second_id (no leakage from the first request).
        second_cv_value = second_response.get_json()["contextvars_correlation_id"]
        assert second_cv_value == second_id


# ---------------------------------------------------------------------------
# TestResponseHeaderEcho - X-Correlation-Id response header
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestResponseHeaderEcho:
    """Validate the after_request hook echoes the correlation ID.

    The SPA's fetch wrapper (``frontend/src/api/client.ts``) reads the
    ``X-Correlation-Id`` response header to allow the user to copy a
    correlation ID for support tickets. The after_request hook must
    set the header on every successful response, BUT must be
    idempotent (leave a pre-set header alone) to avoid double-setting
    when an upstream proxy or test fixture has already stamped one.
    """

    def test_response_carries_inbound_header(self, test_client: Any) -> None:
        """The response echoes the inbound header value back to the SPA."""
        valid_id = "echo-test"
        response = test_client.get("/ping", headers={CORRELATION_HEADER_NAME: valid_id})
        assert response.headers.get(CORRELATION_HEADER_NAME) == valid_id

    def test_response_carries_generated_header_when_no_inbound(self, test_client: Any) -> None:
        """When no inbound header, the response carries the GENERATED value."""
        response = test_client.get("/ping")
        echoed = response.headers.get(CORRELATION_HEADER_NAME)
        assert echoed is not None
        assert _is_uuid4(echoed)
        # The echoed value matches g.correlation_id from the body.
        assert response.get_json()["correlation_id"] == echoed

    def test_response_header_is_idempotent(self, test_client: Any) -> None:
        """If the handler pre-set ``X-Correlation-Id``, after_request leaves it.

        The after_request hook checks
        ``CORRELATION_HEADER_NAME not in response.headers`` before
        setting the header. The /header-set route pre-sets a sentinel
        value to verify the hook does NOT overwrite a pre-set
        response header.
        """
        response = test_client.get("/header-set")
        assert response.headers.get(CORRELATION_HEADER_NAME) == "preset-value-do-not-overwrite"

    def test_response_header_set_on_error_response(self, test_client: Any) -> None:
        """Behaviour on unhandled-exception responses is bounded.

        Without ``app.middleware.error_handlers`` registered, an
        unhandled exception falls through to Werkzeug's default 500
        page. Flask's default behavior is to call after_request even
        for the 500 response, but we don't assert on the header
        because response-header echo on error responses is the
        responsibility of ``app.middleware.error_handlers`` (which
        builds its own envelope with the X-Correlation-Id header per
        its dedicated agent prompt).

        The test asserts only the status code here so we know the
        request completed (no infinite loop, no hung test client) and
        the middleware did not crash the dispatch loop.
        """
        response = test_client.get("/raises")
        assert response.status_code == 500


# ---------------------------------------------------------------------------
# TestOpenTelemetryBinding - OTel span attribute and graceful degradation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOpenTelemetryBinding:
    """Validate OpenTelemetry span attribute and baggage binding.

    The middleware calls ``trace.get_current_span()`` and, when a
    recording span is active, sets the ``app.correlation_id``
    attribute on the span. This lets observability backends
    cross-reference traces with logs (which carry the same
    correlation ID via structlog's ``merge_contextvars`` processor).

    The middleware MUST also degrade gracefully:

    * When OpenTelemetry is not installed (``_OTEL_AVAILABLE = False``),
      the binding is skipped silently.
    * When ``get_current_span`` returns None, no attribute is set and
      no exception is raised.
    * When the active span is not recording, no attribute is set
      (avoids polluting the trace with attributes for spans that
      won't be exported anyway).
    * When any OTel call raises (e.g., during shutdown, with a
      corrupt context), the exception is swallowed so the request
      path is not affected.

    All tests in this class run fully OFFLINE: we patch the OTel
    primitives via ``unittest.mock.patch`` against the dotted module
    paths defined inside ``app.middleware.correlation`` so the suite
    does not require an OTel collector or an active span context.
    """

    def test_otel_attribute_set_when_active(self, test_app: Flask, test_client: Any) -> None:
        """A recording span gets the ``app.correlation_id`` attribute.

        Patches ``app.middleware.correlation.trace.get_current_span``
        to return a mock span whose ``is_recording()`` returns True,
        then asserts ``set_attribute`` was called with the namespaced
        key ``app.correlation_id`` and the resolved correlation ID.
        """
        # ``test_app`` is requested as a fixture parameter to ensure
        # the same Flask instance backs ``test_client`` (pytest
        # fixture composition guarantees this; we declare it
        # explicitly to make the intent visible).
        del test_app  # Acknowledged but not directly used; presence is the point.
        mock_span = MagicMock()
        mock_span.is_recording.return_value = True

        with patch(
            "app.middleware.correlation.trace.get_current_span",
            return_value=mock_span,
        ):
            valid_id = "otel-test-id"
            test_client.get("/ping", headers={CORRELATION_HEADER_NAME: valid_id})

        # ``assert_any_call`` rather than ``assert_called_once_with``
        # because OTel instrumentation libraries may set additional
        # attributes on the same span; we only assert OUR attribute
        # is present.
        mock_span.set_attribute.assert_any_call("app.correlation_id", valid_id)

    def test_otel_attribute_not_set_when_span_not_recording(self, test_client: Any) -> None:
        """When the span is NOT recording, ``set_attribute`` is NOT called.

        Avoids polluting traces with attributes for spans that won't
        be exported anyway. The middleware checks
        ``span.is_recording()`` before calling ``set_attribute``.
        """
        mock_span = MagicMock()
        mock_span.is_recording.return_value = False

        with patch(
            "app.middleware.correlation.trace.get_current_span",
            return_value=mock_span,
        ):
            test_client.get("/ping", headers={CORRELATION_HEADER_NAME: "no-record"})

        mock_span.set_attribute.assert_not_called()

    def test_otel_attribute_not_set_when_span_is_none(self, test_client: Any) -> None:
        """When ``get_current_span`` returns None, no exception is raised.

        The middleware checks ``span is not None`` before invoking
        ``span.is_recording()`` so a None return from
        ``get_current_span`` is handled gracefully.
        """
        with patch(
            "app.middleware.correlation.trace.get_current_span",
            return_value=None,
        ):
            response = test_client.get("/ping", headers={CORRELATION_HEADER_NAME: "no-span"})
        # Request completes successfully even with no span.
        assert response.status_code == 200

    def test_otel_failure_does_not_break_request(self, test_client: Any) -> None:
        """Logging plumbing must NEVER cause an HTTP request to fail.

        Patches ``trace.get_current_span`` to RAISE; the middleware
        must catch and continue. The catch block is intentional
        (annotated with ``noqa: S110``) because OTel SDK exceptions
        during shutdown or with a corrupt context should NEVER
        propagate to the request path.
        """
        with patch(
            "app.middleware.correlation.trace.get_current_span",
            side_effect=RuntimeError("OTel SDK is broken"),
        ):
            response = test_client.get("/ping", headers={CORRELATION_HEADER_NAME: "otel-broken"})
        # The request still completes successfully and the
        # correlation ID is still bound on g and in the response.
        assert response.status_code == 200
        assert response.get_json()["correlation_id"] == "otel-broken"

    def test_otel_unavailable_skipped_gracefully(self, test_client: Any) -> None:
        """When ``_OTEL_AVAILABLE`` is False, OTel binding is skipped.

        Patches the module-level flag directly to simulate a
        deployment where OTel is not installed (e.g., a slimmed-down
        test container). The middleware's early-return guard inside
        ``_bind_to_otel`` must short-circuit before any OTel API is
        touched so no ImportError or AttributeError can propagate.
        """
        with patch("app.middleware.correlation._OTEL_AVAILABLE", False):
            response = test_client.get(
                "/ping",
                headers={CORRELATION_HEADER_NAME: "no-otel"},
            )
        assert response.status_code == 200
        assert response.get_json()["correlation_id"] == "no-otel"


# ---------------------------------------------------------------------------
# TestRegisterCorrelationMiddleware - hook registration semantics
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRegisterCorrelationMiddleware:
    """Validate the registration function wires up Flask hooks correctly.

    Flask exposes registered hooks via three dicts on the app
    object:

    * ``app.before_request_funcs`` (dict of {endpoint or None: [funcs]})
    * ``app.after_request_funcs`` (same shape)
    * ``app.teardown_request_funcs`` (same shape)

    The global hooks (those registered via the bare ``app.before_request``
    decorator with no blueprint) live under the dict key ``None``.
    These tests inspect those dicts directly to confirm registration
    occurred, then exercise an end-to-end request to confirm the
    hooks fire correctly.
    """

    def test_registers_three_hooks(self) -> None:
        """All three Flask hook types are registered under the global key.

        Verifies registration occurred on a fresh Flask app: before,
        after, and teardown function lists each gain at least one
        entry under the global ``None`` key after
        ``register_correlation_middleware`` runs.
        """
        app = Flask(__name__)
        # Before registration: empty (the dicts may not even contain
        # the None key yet, depending on Flask's internals).
        assert not app.before_request_funcs
        assert not app.after_request_funcs
        assert not app.teardown_request_funcs

        register_correlation_middleware(app)

        # After registration: at least one entry under the global key None.
        # ``len(...) >= 1`` rather than ``== 1`` so the test stays
        # green if a future change registers more than one hook of a
        # given kind.
        assert None in app.before_request_funcs
        assert len(app.before_request_funcs[None]) >= 1
        assert None in app.after_request_funcs
        assert len(app.after_request_funcs[None]) >= 1
        assert None in app.teardown_request_funcs
        assert len(app.teardown_request_funcs[None]) >= 1

    def test_double_registration_idempotent(self) -> None:
        """Calling ``register_correlation_middleware`` twice still works.

        Flask does NOT deduplicate hooks by view-function identity;
        calling the registration function twice will append the
        helper functions to the hook lists twice. The middleware
        helpers are designed to be safe under double invocation:

        * ``_before_request_correlation`` resolves the correlation ID
          twice but produces the same value both times (read from the
          same header / generated UUID is stable within a request).
        * ``_after_request_correlation`` is idempotent by design (it
          checks ``CORRELATION_HEADER_NAME not in response.headers``
          before setting the header).
        * ``_teardown_request_correlation`` calls
          ``clear_contextvars`` which is itself idempotent.

        We test the end-to-end behaviour: a request still works and
        produces a single correlation ID and a single response header.
        """
        app = Flask(__name__)
        register_correlation_middleware(app)
        register_correlation_middleware(app)

        @app.route("/ping")
        def ping() -> Any:
            return jsonify(correlation_id=getattr(g, "correlation_id", None))

        client = app.test_client()
        response = client.get("/ping", headers={CORRELATION_HEADER_NAME: "double-register"})
        assert response.status_code == 200
        # Even if Flask called the before_request hook twice, the
        # result is the same: the value resolves to "double-register"
        # both times (the second call sees the same inbound header).
        assert response.get_json()["correlation_id"] == "double-register"
        # Header is echoed once because the second after_request call
        # sees the header is already present and skips.
        assert response.headers.get(CORRELATION_HEADER_NAME) == "double-register"


# ---------------------------------------------------------------------------
# TestPerformance - sub-millisecond overhead smoke test
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPerformance:
    """Smoke-test the middleware's per-request overhead.

    Per AAP Section 0.7.3, the correlation middleware should add
    sub-millisecond overhead per request. This smoke test catches
    catastrophic regressions (e.g., an accidental DB call inside
    ``_resolve_correlation_id``) without being flaky on slow CI
    machines.
    """

    def test_middleware_overhead_sub_millisecond(self, test_client: Any) -> None:
        """100 sequential requests complete in well under 1 second.

        This is a soft upper bound: per AAP, the middleware should
        add < 1 ms per request. 100 requests should complete in
        << 1 second on any reasonable test runner. We use a generous
        5-second ceiling to avoid flakiness on slow CI machines while
        still flagging catastrophic regressions like an accidentally-
        introduced DB round-trip or a synchronous network call.
        """
        start = time.perf_counter()
        for _ in range(100):
            test_client.get("/ping")
        elapsed = time.perf_counter() - start
        # Generous ceiling: 5 seconds for 100 requests = 50 ms per
        # request. The actual middleware portion is microseconds; the
        # rest is Flask test-client overhead (route dispatch,
        # Werkzeug environ construction, JSON serialization).
        assert elapsed < 5.0, (
            f"100 requests took {elapsed:.2f}s; correlation middleware overhead may have regressed."
        )
