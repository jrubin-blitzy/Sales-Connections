"""Tests for outbound-HTTP tracing instrumentation.

QA Checkpoint 10 Issue 4: the Flask + SQLAlchemy instrumentations
cover inbound HTTP and database spans but do NOT inject W3C
``traceparent``/``tracestate`` headers on outbound HTTP calls. The
Anthropic Claude SDK uses ``httpx`` and Authlib's Google OAuth flow
uses ``requests``; both instrumentation packages must be loadable and
the ``_instrument_outbound_http`` helper must be wired into
``init_tracing``.

These tests verify:

- The two instrumentation modules are importable in the project's
  pinned environment.
- The ``_HTTPX_INSTRUMENTATION_AVAILABLE`` and
  ``_REQUESTS_INSTRUMENTATION_AVAILABLE`` flags reflect the import
  outcome.
- The ``_instrument_outbound_http`` helper exists, is idempotent, and
  swallows exceptions raised on subsequent calls (so that
  pytest-flask's "construct multiple Flask apps per session" pattern
  doesn't blow up).
- The ``init_tracing`` public function references the helper (a small
  source-level grep) so a refactor that drops the call site is caught
  by CI.
"""

from __future__ import annotations

import inspect

from app.observability import tracing


class TestOutboundInstrumentationModules:
    """The instrumentation packages are loadable in the pinned env."""

    def test_httpx_instrumentation_available_flag(self) -> None:
        """The HTTPX availability flag is True in the pinned env."""
        # The module-level flag is computed at import time. Per the
        # AAP-pinned ``opentelemetry-instrumentation-httpx==0.50b0``
        # requirement, the flag MUST be True.
        assert tracing._HTTPX_INSTRUMENTATION_AVAILABLE is True

    def test_requests_instrumentation_available_flag(self) -> None:
        """The Requests availability flag is True in the pinned env."""
        assert tracing._REQUESTS_INSTRUMENTATION_AVAILABLE is True

    def test_httpx_instrumentor_is_imported(self) -> None:
        """The ``HTTPXClientInstrumentor`` symbol is bound in the module."""
        assert hasattr(tracing, "HTTPXClientInstrumentor")
        # The class exposes ``.instrument()`` per the OTel contract.
        assert callable(tracing.HTTPXClientInstrumentor)

    def test_requests_instrumentor_is_imported(self) -> None:
        """The ``RequestsInstrumentor`` symbol is bound in the module."""
        assert hasattr(tracing, "RequestsInstrumentor")
        assert callable(tracing.RequestsInstrumentor)


class TestInstrumentOutboundHttp:
    """Behavior of the ``_instrument_outbound_http`` helper."""

    def test_helper_exists(self) -> None:
        """The helper function exists and is callable."""
        assert callable(tracing._instrument_outbound_http)

    def test_helper_is_idempotent(self) -> None:
        """Calling twice in a row does not raise.

        ``HTTPXClientInstrumentor().instrument()`` and
        ``RequestsInstrumentor().instrument()`` raise on second invocation
        in some OTel versions; the helper's try/except ensures the
        application factory can be invoked repeatedly under pytest-flask
        without crashing the test session.
        """
        # First call may succeed or warn-and-continue depending on
        # whether init_tracing has already run; either way, the second
        # call must not raise.
        tracing._instrument_outbound_http()
        tracing._instrument_outbound_http()

    def test_init_tracing_calls_outbound_instrumentation(self) -> None:
        """``init_tracing`` source references ``_instrument_outbound_http``.

        A regression in which a future refactor drops the helper call
        from ``init_tracing`` would silently re-introduce QA Issue 4.
        Source-level inspection guards against that drift.
        """
        source = inspect.getsource(tracing.init_tracing)
        assert "_instrument_outbound_http" in source, (
            "init_tracing must invoke _instrument_outbound_http; Issue 4 regression"
        )
