"""OpenTelemetry distributed tracing initialization for the Sales-Connections backend.

This module exposes a single public function, ``init_tracing(app, engine=None)``,
which is invoked from the Flask application factory in ``app.__init__``.

When ``OTLP_EXPORTER_ENDPOINT`` is configured, the function:

1. Builds a ``Resource`` describing the service (name, namespace, version,
   deployment environment).
2. Constructs an ``OTLPSpanExporter`` (HTTP/protobuf transport) pointed at
   the configured endpoint, with optional headers parsed from
   ``OTLP_EXPORTER_HEADERS``.
3. Wires that exporter into a ``BatchSpanProcessor`` for efficient delivery
   on a background thread (vs. the latency-blocking ``SimpleSpanProcessor``).
4. Installs the resulting ``TracerProvider`` as the global default.
5. Auto-instruments Flask via ``FlaskInstrumentor`` and (when an engine is
   supplied) SQLAlchemy via ``SQLAlchemyInstrumentor`` for end-to-end
   request-to-database span linkage.

When ``OTLP_EXPORTER_ENDPOINT`` is empty (the default in local dev/tests
without a collector), this module is a graceful no-op so worker startup
does not require any tracing infrastructure to be reachable.

Per AAP Section 0.7.5 ("Observability rule"), the application is not
considered complete until distributed tracing is functional alongside
structured logging and metrics.

Transport choice
----------------
The OTLP HTTP/protobuf exporter is preferred over the gRPC variant for
ECS Fargate deployments because:

- HTTP works through standard ALB/proxy infrastructure without special
  HTTP/2 or gRPC routing.
- It avoids long-lived persistent connections that complicate auto-scaling
  and connection pooling under bursty traffic.
- The default endpoint shape (``http://localhost:4318/v1/traces``) is
  identical for local Jaeger, OpenTelemetry Collector, and most managed
  vendor backends, simplifying the operator-facing configuration story.

Idempotency
-----------
``init_tracing`` is safe to call multiple times in the same Python process.
The first call installs the global ``TracerProvider``; subsequent calls
re-apply Flask/SQLAlchemy instrumentation against any newly-constructed
test apps and engines without raising. This is required because
``pytest-flask`` constructs and tears down multiple Flask apps per session.

Coordination
------------
This module is one of three single-responsibility observability modules:

- :mod:`app.observability.logging`  Structured JSON logging via structlog.
- :mod:`app.observability.metrics`  Prometheus metrics endpoint.
- :mod:`app.observability.tracing`  OpenTelemetry distributed tracing (this module).

Each module is independent and may be imported and configured in isolation.
The Flask application factory in ``app.__init__`` invokes them in order
during startup.
"""

from __future__ import annotations

# Standard library imports.
#
# NOTE: This module's sibling file is named ``logging.py`` (the structlog
# bootstrap module) and shares the ``app.observability`` namespace
# package with this file. Toolchain configurations that treat
# ``app/observability/`` as a PEP 420 namespace package without
# ``explicit_package_bases`` (notably the project's current mypy
# configuration) misresolve a direct ``import logging`` from inside
# this file as a self-import against the sibling rather than the stdlib.
# We work around the collision by going through ``importlib.import_module``,
# matching the convention already established in the sibling logging.py:
# at runtime this is identical to ``import logging as stdlib_logging``,
# and at type-check time the ``Any`` annotation defuses the namespace
# conflict without affecting the runtime behavior or surface area.
import importlib
from typing import TYPE_CHECKING, Any

# Import the stdlib ``logging`` module via ``importlib`` to bypass the
# same-name sibling-file shadowing issue described above. The ``Any``
# type annotation is intentional: it makes the wrapper opaque to mypy
# so that mypy does not attempt the (incorrect, namespace-package-quirk)
# resolution against the sibling ``logging.py``. The runtime behavior
# is identical to ``import logging as stdlib_logging``.
stdlib_logging: Any = importlib.import_module("logging")

# ---------------------------------------------------------------------------
# Third-party imports
# ---------------------------------------------------------------------------
# OpenTelemetry packages are pinned in backend/requirements.txt
# (==1.29.0 for SDK/API/exporters; ==0.50b0 for instrumentations). The
# defensive try/except guard ensures this module remains importable in
# stripped-down environments (e.g., minimal recovery shells, isolated
# unit tests) where the OTel packages are missing. In that pathological
# case, ``init_tracing`` becomes a logged no-op rather than crashing
# the application factory.
try:
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.flask import FlaskInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    _OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover - defensive; OTel is pinned in requirements.txt
    _OTEL_AVAILABLE = False


if TYPE_CHECKING:
    # Type-only imports avoid a runtime coupling to Flask and SQLAlchemy.
    # Runtime integration with these libraries is delegated to the
    # OpenTelemetry instrumentations (which already declare the
    # dependency) and to the application factory (which constructs the
    # Flask app and the SQLAlchemy Engine before passing them in).
    from flask import Flask
    from sqlalchemy import Engine


# ---------------------------------------------------------------------------
# Module-level constants and state
# ---------------------------------------------------------------------------

# Canonical service name used across all environments. Differentiation
# between dev/staging/prod is encoded in the ``deployment.environment``
# resource attribute, NOT in the service name itself. This convention
# allows operators to query a single service name in observability
# backends and filter by environment.
_DEFAULT_SERVICE_NAME: str = "sales-connections-api"

# Module-level stdlib logger. Records emitted here flow through the
# structlog stdlib bridge configured by ``app.observability.logging``,
# inheriting JSON formatting and the canonical context fields
# (correlation_id, user_id, org_id, trace_id, span_id) when bound.
# Uses ``stdlib_logging`` (an alias for the importlib-loaded stdlib
# ``logging`` module) to avoid the sibling-file name-collision noted at
# the top of this file.
_logger = stdlib_logging.getLogger(__name__)

# Idempotency guard. OpenTelemetry's global ``TracerProvider`` can only
# be installed once per process; subsequent calls to
# ``trace.set_tracer_provider`` are silently ignored by the SDK. We
# track our own initialization state so the second call to
# ``init_tracing`` skips the provider-replacement step (which would be a
# no-op anyway) but STILL re-applies Flask/SQLAlchemy instrumentation
# against the newly-constructed app and engine. This is essential for
# pytest-flask test sessions which create one Flask app per test.
_initialized: bool = False


__all__ = ["init_tracing"]


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _parse_otlp_headers(raw: str | None) -> dict[str, str]:
    """Parse comma-separated ``key=value`` pairs into a dict.

    Tolerant of whitespace, empty entries, and missing equals signs.
    Used to convert the ``OTLP_EXPORTER_HEADERS`` config value into the
    shape expected by ``OTLPSpanExporter(headers=...)``.

    The naive ``dict(item.split("=") for item in raw.split(","))`` form
    crashes on malformed entries; this explicit parser is robust to
    operator typos in ``.env`` values such as missing equals signs,
    leading/trailing whitespace, empty entries between commas, and
    keys that contain an equals sign in their value
    (``key=value=extra`` is parsed as ``{"key": "value=extra"}``
    via ``str.partition``).

    Examples:
        ``"x-honeycomb-team=abc"`` -> ``{"x-honeycomb-team": "abc"}``
        ``"a=1, b=2 , =skip, no_eq, d="`` -> ``{"a": "1", "b": "2", "d": ""}``
        ``""`` -> ``{}``
        ``None`` -> ``{}``

    Args:
        raw: Comma-separated ``key=value`` string from the
            ``OTLP_EXPORTER_HEADERS`` config value, or ``None`` if the
            config value is unset.

    Returns:
        A dict mapping header name to header value. Empty if the input
        is empty, None, or contains no well-formed entries.
    """
    if not raw:
        return {}
    headers: dict[str, str] = {}
    for raw_chunk in raw.split(","):
        chunk = raw_chunk.strip()
        if not chunk or "=" not in chunk:
            # Skip empty entries and entries with no equals sign.
            # Defensive against operator typos in .env files.
            continue
        key, _, value = chunk.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            # Skip entries with empty keys (e.g., "=value").
            continue
        headers[key] = value
    return headers


def _build_resource(app: Flask) -> Resource:
    """Construct the OpenTelemetry Resource describing this service.

    The returned ``Resource`` is attached to the ``TracerProvider`` and
    flows through to every span emitted by this process. Resource
    attributes use OpenTelemetry semantic conventions so they are
    recognized by every major observability backend (Honeycomb, Datadog,
    Grafana Tempo, AWS X-Ray, Jaeger).

    Attributes assembled:

    - ``service.name``           From ``OTEL_SERVICE_NAME`` config
                                 (default ``sales-connections-api``).
    - ``service.namespace``      Constant ``sales-connections``.
    - ``service.version``        From the ``app.__version__`` module
                                 attribute when importable; falls back
                                 to the literal ``"unknown"``.
    - ``deployment.environment`` From ``ENV_NAME`` config
                                 (e.g., ``development``, ``testing``,
                                 ``production``).

    Args:
        app: The Flask application whose ``config`` mapping is queried
            for the configurable resource attribute values.

    Returns:
        An OpenTelemetry ``Resource`` ready to be attached to a
        ``TracerProvider``.
    """
    service_name = app.config.get("OTEL_SERVICE_NAME") or _DEFAULT_SERVICE_NAME
    env_name = app.config.get("ENV_NAME", "unknown")

    service_version: str = "unknown"
    try:
        # Lazy import to avoid a circular import: ``app.__init__`` may
        # itself import this module before ``__version__`` is defined.
        # The PLC0415 suppression keeps ruff's import-not-at-top rule
        # happy, which is the correct local idiom for a deferred
        # module attribute lookup. ``__version__`` is now declared in
        # ``app/__init__.py`` (see AAP Section 0.5.2 Layer 0) so the
        # attribute is always present in normal operation; the defensive
        # ``except`` branch below covers stripped-down environments
        # (recovery shells, minimal CI images) where the application
        # package may have been monkey-patched to omit it.
        from app import __version__ as app_version  # noqa: PLC0415

        if isinstance(app_version, str) and app_version:
            service_version = app_version
    except (ImportError, AttributeError):  # pragma: no cover - defensive
        # ``app.__version__`` is optional in stripped-down environments;
        # absence is not an error and falls through to the "unknown"
        # default which is still a valid OpenTelemetry attribute value.
        service_version = "unknown"

    return Resource.create(
        {
            "service.name": service_name,
            "service.namespace": "sales-connections",
            "service.version": service_version,
            "deployment.environment": env_name,
        }
    )


def _build_exporter(app: Flask) -> OTLPSpanExporter | None:
    """Construct the OTLP HTTP/protobuf span exporter from app config.

    Reads:

    - ``OTLP_EXPORTER_ENDPOINT`` (str): full URL to the OTLP HTTP collector
      (e.g., ``http://localhost:4318/v1/traces`` for a local Jaeger or
      OpenTelemetry Collector).
    - ``OTLP_EXPORTER_HEADERS`` (str): comma-separated ``key=value`` pairs
      for vendor authentication (e.g., Honeycomb's
      ``x-honeycomb-team=<api-key>``).

    Returns ``None`` if ``OTLP_EXPORTER_ENDPOINT`` is empty or whitespace-
    only, which signals the caller to skip tracing initialization
    entirely. This is the default in local dev and unit tests without a
    collector.

    Args:
        app: The Flask application whose ``config`` mapping holds the
            exporter configuration.

    Returns:
        A configured ``OTLPSpanExporter`` instance, or ``None`` when
        tracing is disabled (empty endpoint).
    """
    endpoint = (app.config.get("OTLP_EXPORTER_ENDPOINT") or "").strip()
    if not endpoint:
        return None

    headers_raw = app.config.get("OTLP_EXPORTER_HEADERS") or ""
    headers = _parse_otlp_headers(headers_raw)

    # Pass ``headers=None`` (rather than an empty dict) when no headers
    # were configured. This matches the OTLPSpanExporter parameter
    # convention and avoids sending an empty header dict that some
    # collectors would otherwise log as a warning.
    return OTLPSpanExporter(endpoint=endpoint, headers=headers or None)


def _instrument_flask(app: Flask) -> None:
    """Apply Flask auto-instrumentation idempotently.

    ``FlaskInstrumentor().instrument_app(app)`` raises
    ``RuntimeError`` (or in some OTel versions a generic ``Exception``)
    when called twice against the same Flask app. The pytest-flask test
    harness constructs multiple Flask apps per session, and a defensive
    try/except is the cheapest way to keep that workflow green.

    The Flask instrumentation wraps the request lifecycle and emits a
    server-kind span per HTTP request, capturing route, method,
    status_code, and propagated trace context. Downstream calls
    (SQLAlchemy queries via the SQLAlchemy instrumentation, outbound
    HTTP via requests/httpx instrumentations) are linked into the same
    trace through OpenTelemetry's context propagation.

    Args:
        app: The Flask application to instrument. Must be a fully-
            constructed ``Flask`` instance (not a blueprint).
    """
    try:
        FlaskInstrumentor().instrument_app(app)
    except Exception as exc:  # pragma: no cover - defensive
        # We deliberately catch the broad ``Exception`` here because
        # different OTel versions raise different exception classes for
        # the "already instrumented" case (RuntimeError in some
        # versions, AttributeError in others). The instrumentation
        # failure is non-fatal; the application can still serve
        # requests without per-request spans, so we log and continue.
        _logger.warning(
            "flask_instrumentation_skipped",
            extra={"error": repr(exc)},
        )


def _instrument_sqlalchemy(engine: Engine | None) -> None:
    """Apply SQLAlchemy auto-instrumentation when an engine is supplied.

    No-op when ``engine is None`` (e.g., tests that exercise tracing
    without a database). The ``SQLAlchemyInstrumentor`` is a global
    singleton; calling ``.instrument(engine=...)`` multiple times with
    different engines in the same process is supported by the
    instrumentation, which deduplicates by engine identity internally.

    The SQLAlchemy instrumentation hooks the SQLAlchemy 2.x Engine
    event lifecycle to emit a client-kind span per database round-trip.
    Combined with the Flask instrumentation, this provides end-to-end
    request-to-database span linkage in the configured observability
    backend.

    Args:
        engine: The SQLAlchemy 2.x ``Engine`` to instrument, or ``None``
            to skip SQLAlchemy instrumentation entirely.
    """
    if engine is None:
        # Caller did not provide an engine; nothing to instrument. This
        # is the normal path for no-database test scenarios and for
        # the tracing-disabled case where the application factory
        # passes ``None`` instead of ``db.engine``.
        return
    try:
        SQLAlchemyInstrumentor().instrument(engine=engine)
    except Exception as exc:  # pragma: no cover - defensive
        # As with Flask instrumentation, different OTel versions raise
        # different exception classes for the "already instrumented"
        # case. We log and continue rather than crashing the
        # application factory.
        _logger.warning(
            "sqlalchemy_instrumentation_skipped",
            extra={"error": repr(exc)},
        )


# ---------------------------------------------------------------------------
# Public function: init_tracing
# ---------------------------------------------------------------------------


def init_tracing(app: Flask, engine: Engine | None = None) -> None:
    """Initialize OpenTelemetry distributed tracing for the Sales-Connections backend.

    This is the public entry point invoked from the Flask application
    factory in ``app.__init__`` as the final observability step. The
    expected call shape from the factory is::

        init_tracing(app, db.engine if app.config.get("OTLP_EXPORTER_ENDPOINT") else None)

    Behavior matrix:

    - ``OTel packages missing``     -> log ``tracing_skipped_otel_not_available`` and return.
    - ``OTLP_EXPORTER_ENDPOINT=""`` -> log ``tracing_disabled`` and return.
    - ``OTLP_EXPORTER_ENDPOINT`` set -> install global TracerProvider with
      OTLP HTTP exporter, attach BatchSpanProcessor, instrument Flask
      (always) and SQLAlchemy (when ``engine`` is provided), and log
      ``tracing_initialized``.

    Idempotency: safe to call multiple times in the same Python process.
    Subsequent calls preserve the original global ``TracerProvider`` and
    re-apply Flask/SQLAlchemy instrumentation against the new Flask app
    and engine. This is required because pytest-flask creates one Flask
    app per test, but OpenTelemetry's global provider can only be set
    once.

    Args:
        app: The fully-constructed Flask application. Must have its
            ``config`` populated with at minimum ``OTEL_SERVICE_NAME``,
            ``ENV_NAME``, and (to enable tracing) ``OTLP_EXPORTER_ENDPOINT``.
        engine: Optional SQLAlchemy 2.x ``Engine`` to instrument for
            per-query span emission. When ``None``, SQLAlchemy
            instrumentation is skipped entirely (Flask instrumentation
            still runs). The application factory passes ``None`` when
            tracing is disabled overall, matching this module's no-op
            short-circuit.

    Returns:
        None. Side effects: installs the global ``TracerProvider`` (on
        the first call only), wraps the Flask request lifecycle, and
        wraps the SQLAlchemy Engine event lifecycle.
    """
    global _initialized  # noqa: PLW0603 - explicit idempotency guard, intentional

    # Step 1: Defensive bail-out when the OpenTelemetry packages are
    # not importable. OTel is pinned in requirements.txt and should
    # always be available in production, but we tolerate its absence
    # for stripped-down environments (recovery shells, minimal CI
    # images) so the application can still serve requests without
    # tracing.
    if not _OTEL_AVAILABLE:
        _logger.info(
            "tracing_skipped_otel_not_available",
            extra={"reason": "OpenTelemetry packages not importable"},
        )
        return

    # Step 2: Build the exporter. If the configured endpoint is empty,
    # the helper returns None and we treat that as a deliberate "tracing
    # disabled" signal. This is the default in local dev and unit tests
    # without a collector. We log a single INFO line so operators can
    # confirm that tracing is intentionally disabled vs. silently
    # broken.
    exporter = _build_exporter(app)
    if exporter is None:
        _logger.info(
            "tracing_disabled",
            extra={
                "reason": "OTLP_EXPORTER_ENDPOINT empty",
                "service.name": app.config.get("OTEL_SERVICE_NAME", _DEFAULT_SERVICE_NAME),
            },
        )
        return

    # Step 3: Build the resource and tracer provider. The Resource
    # describes the service identity (service.name, service.namespace,
    # service.version, deployment.environment) attached to every span
    # emitted by this process. The TracerProvider is the concrete SDK
    # implementation that the global tracer accessor returns to
    # downstream callers via ``trace.get_tracer(__name__)``.
    resource = _build_resource(app)
    provider = TracerProvider(resource=resource)
    # BatchSpanProcessor batches spans on a background thread before
    # delivery via the OTLPSpanExporter. This minimizes impact on
    # request latency vs. the synchronous SimpleSpanProcessor, which
    # would POST each span individually and would push the AI handler
    # closer to the 5-second P95 latency budget defined in AAP §0.7.3.
    provider.add_span_processor(BatchSpanProcessor(exporter))

    # Step 4: Install the global tracer provider on the first call only.
    # OpenTelemetry's ``set_tracer_provider`` is intentionally non-
    # overriding once a non-default provider has been installed; we
    # respect that by tracking our own initialization state. The
    # idempotency-guarded path STILL re-applies instrumentation on
    # subsequent calls because pytest-flask constructs multiple Flask
    # apps per session.
    if not _initialized:
        trace.set_tracer_provider(provider)
        _initialized = True

    # Step 5: Apply auto-instrumentation. Flask instrumentation always
    # runs (against the new app); SQLAlchemy instrumentation runs only
    # when an engine is supplied (the application factory passes None
    # in test scenarios that do not exercise the database).
    _instrument_flask(app)
    _instrument_sqlalchemy(engine)

    # Step 6: Confirm successful initialization in the process logs so
    # operators can verify the configuration at startup. The single
    # INFO line includes the service name, the configured endpoint
    # (which is intentionally NOT redacted because the endpoint is not
    # a secret), and a flag indicating whether SQLAlchemy
    # instrumentation was applied.
    _logger.info(
        "tracing_initialized",
        extra={
            "service.name": app.config.get("OTEL_SERVICE_NAME", _DEFAULT_SERVICE_NAME),
            "endpoint": app.config.get("OTLP_EXPORTER_ENDPOINT"),
            "sqlalchemy_instrumented": engine is not None,
        },
    )
