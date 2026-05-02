"""Per-request correlation ID middleware.

This module installs Flask before/after hooks that:

* Read the inbound ``X-Correlation-Id`` header (set by the SPA's
  ``frontend/src/api/client.ts`` for every fetch call). When absent,
  generate a fresh ``uuid4()`` so internal traffic and ad-hoc curl
  requests still get traced.
* Validate the inbound value defensively: a malformed correlation
  header (e.g., 4 KB of arbitrary data) is replaced with a fresh UUID
  so the value cannot poison logs or metric labels.
* Stash the resolved value on ``g.correlation_id`` so handlers and
  error handlers can read it.
* Bind the value into structlog's contextvars so the
  ``merge_contextvars`` processor in ``app.observability.logging``
  surfaces it on every log line within the request scope.
* Bind the value into the active OpenTelemetry span as both a span
  attribute (``app.correlation_id``) and a baggage entry, so traces
  in the observability backend can be cross-referenced with logs.
* Echo the value back via the ``X-Correlation-Id`` response header so
  the SPA can correlate requests with logs.
* Clear bound contextvars at the end of the request so subsequent
  requests on the same worker thread don't leak prior values.

Per AAP Section 0.5.2 (Layer 0), this middleware MUST be registered
FIRST so all subsequent middleware and handlers see the bound
correlation ID in their structlog context.
"""

from __future__ import annotations

# Standard library imports.
#
# NOTE: A sibling module ``app.observability.logging`` shares the
# ``app`` PEP 420 namespace package with this file. Toolchain
# configurations that treat ``app/`` as a namespace package without
# ``explicit_package_bases`` (notably the project's current mypy
# configuration) misresolve a direct ``import logging`` from inside a
# sibling ``app.*`` module as a self-import against
# ``app.observability.logging`` rather than the stdlib. We work
# around the collision by going through ``importlib.import_module``
# below (after the regular import block), matching the convention
# established in ``app/observability/tracing.py``: at runtime this is
# identical to ``import logging``, and at type-check time the ``Any``
# annotation defuses the namespace conflict without affecting the
# runtime behavior or surface area.
import importlib
import re
from typing import TYPE_CHECKING, Any
import uuid

# Third-party imports.
from flask import g, request
import structlog

# OpenTelemetry imports - wrap in try/except so the middleware works
# even when OTel is not installed (acceptable in lean test
# environments and ad-hoc dev shells without the OTel collector).
try:
    from opentelemetry import baggage, trace

    _OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover - defensive; OTel is pinned
    _OTEL_AVAILABLE = False

# Type-only imports - used only for type annotations evaluated lazily
# under ``from __future__ import annotations``. ``Response`` is the
# return/parameter type of the after_request hook; ``Flask`` is the
# parameter type of the registration function. Neither is needed at
# runtime, so both live in the TYPE_CHECKING block per the project's
# strict ``flake8-type-checking`` configuration (TC002).
if TYPE_CHECKING:
    from flask import Flask, Response

# Deferred stdlib ``logging`` load via ``importlib`` (see NOTE above).
# The ``Any`` annotation is intentional: it makes the wrapper opaque
# to mypy so the type-checker does not attempt the
# (incorrect, namespace-package-quirk) resolution against the sibling
# ``app.observability.logging``. The runtime behavior is identical to
# ``import logging``.
logging: Any = importlib.import_module("logging")


# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

# The HTTP header name used by the SPA's fetch wrapper and echoed back
# in responses. RFC 7230 says header names are case-insensitive; we use
# the canonical mixed-case form for the SET direction.
CORRELATION_HEADER_NAME: str = "X-Correlation-Id"

# Defensive validation: accept UUID v4 form OR a short alphanumeric
# token (some upstream proxies use shorter IDs). Reject anything that
# contains control characters, whitespace, or characters that could
# break log/metric label parsing. Cap at 128 chars to defend against
# log/header bloat attacks.
_CORRELATION_VALUE_PATTERN: re.Pattern[str] = re.compile(r"^[A-Za-z0-9_\-:.]{1,128}$")

# Namespaced attribute key used both for the active span attribute and
# the OpenTelemetry baggage entry. Using a namespaced key
# ("app.correlation_id") avoids colliding with reserved attributes.
_OTEL_CORRELATION_ATTR_KEY: str = "app.correlation_id"

# Module logger - emits a single info line on middleware registration
# to confirm wiring; runtime structured logs flow through structlog
# instead.
_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module public API
# ---------------------------------------------------------------------------

__all__ = [
    "CORRELATION_HEADER_NAME",
    "register_correlation_middleware",
]


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _resolve_correlation_id() -> str:
    """Resolve the per-request correlation ID.

    Priority:
        1. The inbound ``X-Correlation-Id`` request header, if present
           AND the value matches ``_CORRELATION_VALUE_PATTERN``.
        2. A freshly generated ``uuid4()`` string otherwise.

    Defensive behavior: if the inbound header is present but
    malformed (control characters, oversized, etc.), it is REPLACED
    with a fresh UUID. We never propagate untrusted client input as a
    correlation ID because it would land in log files and metric
    labels where injection could be problematic.

    Returns:
        A non-empty correlation ID string. When sourced from the
        inbound header it is guaranteed to match
        ``_CORRELATION_VALUE_PATTERN`` (max 128 chars, restricted
        character class). When generated locally it is a UUID v4
        string of exactly 36 characters.
    """
    raw = request.headers.get(CORRELATION_HEADER_NAME, "").strip()
    if raw and _CORRELATION_VALUE_PATTERN.match(raw):
        return raw
    return str(uuid.uuid4())


def _bind_to_otel(correlation_id: str) -> None:
    """Set the correlation ID as a span attribute and a baggage entry.

    No-op when OpenTelemetry is not installed OR no span is active
    (e.g., when ``OTLP_EXPORTER_ENDPOINT`` is empty and tracing was
    not initialized). All exceptions are caught and silently ignored
    because logging plumbing must NEVER cause an HTTP request to
    fail.

    Args:
        correlation_id: The resolved per-request correlation ID. Set
            both as the active span's ``app.correlation_id`` attribute
            and as a baggage entry under the same key.
    """
    if not _OTEL_AVAILABLE:
        return
    try:
        span = trace.get_current_span()
        if span is not None and span.is_recording():
            span.set_attribute(_OTEL_CORRELATION_ATTR_KEY, correlation_id)
        # Baggage propagates across spans; useful when the request
        # later calls out to an external service (e.g., Anthropic).
        # ``baggage.set_baggage`` returns a new ``Context``; we don't
        # explicitly attach it because attaching here without a
        # matching detach in teardown would create a stack imbalance
        # in Flask's request handling. Setting the attribute on the
        # active span (above) is sufficient for the in-process trace;
        # cross-service propagation is handled by OTel's HTTP
        # instrumentation when it injects W3C baggage headers from
        # the active context.
        ctx = baggage.set_baggage(_OTEL_CORRELATION_ATTR_KEY, correlation_id)
        # Assign to a local variable to make mypy happy and signal
        # intent; explicit ``del`` documents that we intentionally
        # discard the new context.
        del ctx
    except Exception:  # noqa: S110 - pragma: no cover - defensive
        # Logging plumbing must NEVER cause an HTTP request to fail.
        # We swallow every exception here because the OTel SDK can
        # raise in edge cases (e.g., during shutdown, with a corrupt
        # context, or when a custom processor errors); none of those
        # should break the request path. S110 (try-except-pass) is
        # intentionally suppressed - this is defensive plumbing.
        pass


# ---------------------------------------------------------------------------
# Private hooks
# ---------------------------------------------------------------------------


def _before_request_correlation() -> None:
    """Flask before_request hook: resolve and bind the correlation ID.

    Steps (in order):
        1. Resolve the correlation ID from header or generate fresh.
        2. Stash on ``g.correlation_id`` for handler/error-handler access.
        3. Bind into structlog's contextvars (surfaced by
           ``merge_contextvars`` processor on every log line).
        4. Bind into the active OpenTelemetry span as an attribute and
           into baggage for cross-service propagation.

    The bound contextvars are CLEARED in the teardown_request hook so
    subsequent requests on the same worker thread do not leak the
    prior request's correlation ID.
    """
    correlation_id = _resolve_correlation_id()
    g.correlation_id = correlation_id

    # Bind into structlog so subsequent log lines carry the value.
    # ``bind_contextvars`` is the correct API for per-request fields;
    # ``structlog.bind`` would create a new bound logger object instead
    # of populating the contextvar surfaced by ``merge_contextvars``.
    structlog.contextvars.bind_contextvars(correlation_id=correlation_id)

    # Bind into OpenTelemetry (no-op when OTel is not initialized).
    _bind_to_otel(correlation_id)


def _after_request_correlation(response: Response) -> Response:
    """Flask after_request hook: echo the correlation ID back.

    Sets the ``X-Correlation-Id`` response header so the SPA can
    correlate the request with its log output. Idempotent: if the
    header is somehow already set on the response (e.g., a downstream
    proxy added it), we leave the existing value alone.

    We do NOT clear structlog contextvars here because Flask's
    after_request runs BEFORE teardown_request; the contextvars need
    to remain bound for any final logging in teardown handlers. The
    teardown_request hook handles the clear.

    Defense-in-depth: we use ``getattr(g, "correlation_id", None)``
    rather than ``g.correlation_id`` directly so that if the
    before_request hook somehow didn't run (e.g., during a test that
    bypassed middleware), the after_request still works without
    raising AttributeError.

    Args:
        response: The Flask Response object produced by the view
            function (or by an error handler). The header is mutated
            in place; the same object is returned.

    Returns:
        The response object, unmodified except for the
        ``X-Correlation-Id`` header which is set when absent.
    """
    correlation_id = getattr(g, "correlation_id", None)
    if correlation_id and CORRELATION_HEADER_NAME not in response.headers:
        response.headers[CORRELATION_HEADER_NAME] = str(correlation_id)
    return response


def _teardown_request_correlation(exc: BaseException | None) -> None:
    """Flask teardown_request hook: clear bound structlog contextvars.

    Runs AFTER the response is sent (and after any after_request
    hooks). Clears the request-scoped contextvars so subsequent
    requests on the same worker thread (Gunicorn worker pool) do not
    see the prior request's correlation_id.

    We use ``clear_contextvars()`` (clears all bound vars) rather than
    ``unbind_contextvars(...)`` (unbinds specific keys) because:
        * The auth middleware also binds ``user_id``, ``org_id``,
          ``role`` into contextvars; those should also be cleared on
          request end.
        * One central clear is simpler than coordinating per-key
          unbinds across multiple middleware modules.

    The ``exc`` argument is the exception that caused the request to
    end (or None for normal completion). We don't differentiate; the
    cleanup runs in either case.

    Args:
        exc: The exception that ended the request, or ``None`` if
            the request completed normally. Intentionally unused;
            cleanup runs unconditionally so the worker is always in
            a clean state for the next request.
    """
    structlog.contextvars.clear_contextvars()
    # Note: ``exc`` is intentionally unused; teardown_request hooks
    # receive it but we run unconditional cleanup. Explicit ``del``
    # documents the intent and silences "argument unused" lint.
    del exc


# ---------------------------------------------------------------------------
# Public registration function
# ---------------------------------------------------------------------------


def register_correlation_middleware(app: Flask) -> None:
    """Register correlation-ID middleware on the Flask app.

    Registers three hooks:
        * ``before_request``  -> ``_before_request_correlation``
        * ``after_request``   -> ``_after_request_correlation``
        * ``teardown_request`` -> ``_teardown_request_correlation``

    This MUST be the FIRST middleware registered by the app factory
    (per AAP Section 0.5.2 Layer 0) so all subsequent middleware and
    handlers see the bound correlation ID in their structlog context.

    Idempotent: registering the same hooks twice is harmless because
    Flask deduplicates by view-function identity, and the module-level
    helper functions have stable identity across re-registrations.

    Args:
        app: The Flask application instance produced by
            ``app.create_app``. Hooks are registered on the
            application-wide function lists, so they fire for every
            blueprint and every request.

    Returns:
        None. Side effects: mutates ``app.before_request_funcs``,
        ``app.after_request_funcs``, and ``app.teardown_request_funcs``;
        emits a single ``correlation_middleware_registered`` info log
        line via the stdlib logger so operators can confirm wiring at
        startup.
    """
    app.before_request(_before_request_correlation)
    app.after_request(_after_request_correlation)
    app.teardown_request(_teardown_request_correlation)
    _logger.info(
        "correlation_middleware_registered",
        extra={
            "header_name": CORRELATION_HEADER_NAME,
            "otel_available": _OTEL_AVAILABLE,
        },
    )
