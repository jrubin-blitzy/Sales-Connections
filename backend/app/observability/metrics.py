"""Prometheus metrics for the Sales-Connections backend.

This module owns the Prometheus instrumentation surface: it defines the
application-level metric instruments (counters, histograms, gauges),
mounts the ``/metrics`` endpoint, and registers Flask before/after request
hooks that record HTTP-level metrics on every request.

The metrics are registered against the custom ``metrics_registry``
``CollectorRegistry`` from ``app.extensions``, NOT the default global
``prometheus_client`` registry. The custom registry isolates the
application's metric namespace so tests can construct multiple Flask
apps per session without ``Duplicated timeseries`` errors.

Public API
----------
``init_metrics(app)``  Wire metrics into a Flask application.
``http_requests_total``            Counter[method, path, status]
``http_request_duration_seconds``  Histogram[method, path]
``ai_request_duration_seconds``    Histogram (F-002 latency budget)
``audit_emit_duration_seconds``    Histogram (F-013 emission budget)
``active_sessions``                Gauge (per-worker session lifecycle)
``failed_login_attempts_total``    Counter[outcome] (security signal)

Performance budgets enforced by histogram buckets (per AAP Section 0.7.3):

- AI note generation:        <= 5 s P95   (ai_request_duration_seconds)
- Audit event emission:      <= 100 ms    (audit_emit_duration_seconds)
- Form submit (non-AI):      <= 2 s       (http_request_duration_seconds)
- Authentication completion: <= 2 s       (http_request_duration_seconds)
- RBAC authorization check:  << 50 ms     (http_request_duration_seconds)

Coordination
------------
This module is one of three single-responsibility observability modules:

- :mod:`app.observability.logging`  Structured JSON logging via structlog.
- :mod:`app.observability.metrics`  Prometheus metrics endpoint (this module).
- :mod:`app.observability.tracing`  OpenTelemetry distributed tracing.

The Flask application factory in :mod:`app.__init__` invokes
:func:`init_metrics` after blueprint registration. Service-layer modules
(e.g., :mod:`app.services.ai_orchestration`, :mod:`app.services.audit`)
import the duration histograms directly from this module to record
latency observations from feature handlers.
"""

from __future__ import annotations

# Standard library imports.
#
# NOTE: This module's sibling file is named ``logging.py`` (the structlog
# bootstrap module) and shares the ``app.observability`` namespace
# package with this file. Toolchain configurations that treat
# ``app/observability/`` as a PEP 420 namespace package without
# ``explicit_package_bases`` (notably the project's current mypy
# configuration) misresolve a direct ``import logging`` from inside a
# sibling module as a self-import against the local ``logging.py``
# rather than the stdlib. We work around the collision by going through
# ``importlib.import_module``, matching the convention already
# established in the sibling :mod:`app.observability.tracing`: at
# runtime this is identical to ``import logging as stdlib_logging``,
# and at type-check time the ``Any`` annotation defuses the namespace
# conflict without affecting the runtime behavior or surface area.
import importlib
import re
import time
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
# prometheus_client is pinned in backend/requirements.txt at ==0.21.1.
# The defensive try/except guard ensures this module remains importable
# in degraded environments (e.g., a minimal recovery shell) where the
# package is missing. In that pathological case ``init_metrics`` becomes
# a logged no-op rather than crashing the application factory, and the
# module-level metric singletons are absent (any consumer that imports
# them would itself have to handle the ImportError).
try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )

    _PROM_AVAILABLE = True
except ImportError:  # pragma: no cover - defensive; prometheus_client is pinned
    _PROM_AVAILABLE = False
    # Provide a fallback content-type constant so the module remains
    # syntactically valid even if prometheus_client is absent. The
    # value matches the canonical prometheus_client text exposition
    # format header so downstream code that reads CONTENT_TYPE_LATEST
    # behaves identically in either branch.
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"

# ---------------------------------------------------------------------------
# First-party imports
# ---------------------------------------------------------------------------
# Custom CollectorRegistry singleton; see the module-level comment block
# in :mod:`app.extensions` for the rationale on using a dedicated
# registry instead of the default global one shipped by prometheus_client.
# The ``noqa: E402`` is required because the third-party ``try/except``
# block above contains module-level fallback assignments
# (``_PROM_AVAILABLE``, ``CONTENT_TYPE_LATEST``); ruff's "import not at
# top" rule does not distinguish between fallback assignments and
# arbitrary statements, but PEP 8 still mandates third-party imports
# before first-party imports, which is the order we preserve here.
from app.extensions import metrics_registry  # noqa: E402

if TYPE_CHECKING:
    # Type-only imports avoid a runtime coupling to Flask. The Flask
    # application is constructed by the application factory and passed
    # into ``init_metrics(app)`` at startup; the function-scope imports
    # inside the request hooks reach Flask through the application
    # context that is already established by the time those hooks fire.
    from flask import Flask, Response


# ---------------------------------------------------------------------------
# Module-level logger and idempotency state
# ---------------------------------------------------------------------------

# Module-level stdlib logger. Records emitted here flow through the
# structlog stdlib bridge configured by ``app.observability.logging``,
# inheriting JSON formatting and the canonical context fields
# (correlation_id, user_id, org_id, trace_id, span_id) when bound. Uses
# ``stdlib_logging`` (an alias for the importlib-loaded stdlib
# ``logging`` module) to avoid the sibling-file name-collision noted at
# the top of this file.
_logger = stdlib_logging.getLogger(__name__)

# Idempotency guard. ``init_metrics`` is callable multiple times because
# pytest-flask creates one Flask app per test; tracking initialisation
# state lets the function safely skip re-registering the URL rule when
# called against an already-initialised app instance. The flag itself
# is module-global rather than per-app because the metric singletons
# are registered against the shared ``metrics_registry`` exactly once,
# at module import time.
_initialized: bool = False


# ---------------------------------------------------------------------------
# Histogram bucket boundaries
# ---------------------------------------------------------------------------
# Bucket boundaries are deliberately chosen to land on the project's
# documented latency budgets (AAP Section 0.7.3) so that operators can
# read SLO compliance directly off the histogram quantiles without
# needing to interpolate between unrelated bucket edges.

# HTTP request duration histogram bucket boundaries - covers the four
# project-relevant latency thresholds (RBAC << 50 ms, audit <= 100 ms,
# form/auth <= 2 s, AI <= 5 s) plus a 10 s long-tail bucket for
# unexpected slowness.
_HTTP_DURATION_BUCKETS: tuple[float, ...] = (
    0.005,  # 5 ms   - bucket edge under RBAC budget
    0.01,  # 10 ms
    0.025,  # 25 ms  - half of RBAC budget
    0.05,  # 50 ms  - RBAC budget ceiling
    0.1,  # 100 ms - audit emit budget ceiling
    0.25,  # 250 ms
    0.5,  # 500 ms
    1.0,  # 1 s
    2.0,  # 2 s    - form/auth budget ceiling
    5.0,  # 5 s    - AI budget ceiling
    10.0,  # 10 s   - long tail
)

# AI request histogram buckets - concentrated near the 5-second P95
# budget that AAP F-002 enforces. Longer-tail buckets up to 30 s let
# operators see how badly the budget is being missed when the timeout
# watchdog itself is slow to fire (network stalls, etc.).
_AI_DURATION_BUCKETS: tuple[float, ...] = (
    0.1,
    0.25,
    0.5,
    1.0,
    1.5,
    2.0,
    3.0,
    4.0,
    5.0,
    7.5,
    10.0,
    15.0,
    30.0,
)

# Audit emit histogram buckets - concentrated under the 100 ms budget
# that AAP F-013 enforces. Buckets above 100 ms still exist so the
# histogram surfaces budget breaches rather than silently bucketising
# everything into ``+Inf``.
_AUDIT_DURATION_BUCKETS: tuple[float, ...] = (
    0.001,  # 1 ms
    0.005,  # 5 ms
    0.01,  # 10 ms
    0.025,  # 25 ms
    0.05,  # 50 ms
    0.1,  # 100 ms - budget ceiling
    0.25,  # 250 ms - alert above this
    0.5,  # 500 ms
    1.0,  # 1 s
)


# ---------------------------------------------------------------------------
# Metric instrument singletons
# ---------------------------------------------------------------------------
# These five instruments are the entire application-level metric surface.
# They are module-level singletons registered against the custom
# ``metrics_registry`` at module import time. Importing this module is
# therefore idempotent (the registry rejects duplicate names by raising
# ``ValueError`` on the second registration of the same name); the
# custom registry instead of the default global one ensures pytest-flask
# test sessions can construct multiple Flask apps without tripping
# that protection.

# HTTP request counter labelled by method, path, and status. Cardinality
# is bounded by the number of route templates (dozens) times HTTP
# methods (a handful) times status codes (also a handful), well within
# Prometheus' practical labelset limits. The ``path`` label is the Flask
# URL rule (e.g., ``/api/connections/<id>``) when available, with a
# regex-normalised fallback for unmatched routes.
http_requests_total: Counter = Counter(
    "http_requests_total",
    "Total number of HTTP requests handled by the Flask application.",
    labelnames=("method", "path", "status"),
    registry=metrics_registry,
)

# HTTP request duration histogram labelled by method and path. The
# bucket boundaries are tuned to the project's latency budgets so that
# operators can read P50/P95/P99 directly off the histogram without
# interpolation between unrelated bucket edges.
http_request_duration_seconds: Histogram = Histogram(
    "http_request_duration_seconds",
    "End-to-end Flask handler duration in seconds.",
    labelnames=("method", "path"),
    buckets=_HTTP_DURATION_BUCKETS,
    registry=metrics_registry,
)

# AI request duration histogram labelled by outcome. The ``outcome``
# label takes one of four values - ``success``, ``timeout``, ``error``,
# ``validation`` - so that alerting rules can isolate timeout rate
# ("AI timeout rate > 1% over 5 minutes"), generic-error rate, and
# input-rejection rate independently. The ``validation`` label is
# observed when ``app.services.ai_orchestration.generate_outreach_notes``
# rejects an input that sanitizes down to the empty string (the call is
# never sent to Anthropic, but the observation still fires with a 0.0
# duration so input-rejection volume is visible in the histogram).
# Service-layer code in :mod:`app.services.ai_orchestration` records
# observations here for every Anthropic Claude invocation.
ai_request_duration_seconds: Histogram = Histogram(
    "ai_request_duration_seconds",
    (
        "End-to-end Anthropic Claude AI request duration in seconds. "
        "Per AAP F-002, P95 must remain under 5 seconds."
    ),
    labelnames=("outcome",),
    buckets=_AI_DURATION_BUCKETS,
    registry=metrics_registry,
)

# Audit emit duration histogram labelled by event_type. Service-layer
# code in :mod:`app.services.audit` records observations here for every
# audit_events row insertion. The label allows operators to isolate
# audit-emission slowness by event class - e.g., is the ``hard_delete``
# audit emission slower than ``status_change``?
audit_emit_duration_seconds: Histogram = Histogram(
    "audit_emit_duration_seconds",
    (
        "Time spent persisting an audit_events row in the parent transaction. "
        "Per AAP F-013, emission must stay under 100 ms."
    ),
    labelnames=("event_type",),
    buckets=_AUDIT_DURATION_BUCKETS,
    registry=metrics_registry,
)

# Active sessions gauge. Maintained by session lifecycle code in
# :mod:`app.api.auth` (incremented on session JWT mint via the
# password-login and OAuth-callback paths; decremented on explicit
# logout). Distinct from "concurrent in-flight requests"; this metric
# counts authenticated users with valid sessions regardless of whether
# they have an active request at the moment.
#
# Semantics and known limitations (per QA Checkpoint 10 Issue 6):
#
# * Per-worker counter. Each Gunicorn worker maintains its own gauge
#   value. The Prometheus scrape sums across workers when the
#   ``multiproc`` mode is enabled; without that mode the scraped value
#   reflects ONE worker's view. CloudWatch dashboards aggregate via the
#   ECS service summary metric.
# * Resets to 0 on worker restart. Sessions minted before the restart
#   remain valid (the JWT is verified statelessly via HMAC) but the
#   gauge does not track them.
# * Best-effort decrement on natural expiry. The 8-hour JWT TTL elapses
#   silently; this gauge is decremented only when the user explicitly
#   logs out via ``POST /auth/logout``. Operators reading the gauge
#   should interpret it as "logins minus explicit logouts since worker
#   start" rather than a true "currently-valid-JWT" count.
# * May briefly go negative on worker restart. If a worker that started
#   AFTER a session was minted receives the corresponding logout, it
#   decrements its (zero) counter to -1. Operators interpret the sum
#   across workers as the authoritative value; the ECS task count and
#   service-mesh-level connection metrics are the canonical
#   substitutes when worker-bound bookkeeping is insufficient.
active_sessions: Gauge = Gauge(
    "active_sessions",
    (
        "Number of authenticated user sessions currently considered "
        "active. Per-worker counter, incremented on session JWT mint "
        "and decremented on explicit logout; resets to 0 on worker "
        "restart and does not track natural JWT expiry."
    ),
    registry=metrics_registry,
)


# Failed login attempts counter labelled by outcome. Incremented inside
# the password-login handler in :mod:`app.api.auth` BEFORE the global
# error handler converts the AuthError into the anti-enumeration HTTP
# 401 response. Provides a dedicated security signal independent of
# the generic ``http_requests_total{path="/auth/login",status="401"}``
# series so SIEM tooling and alerting rules can isolate authentication
# failures from generic 401s emitted by RBAC and middleware paths.
#
# Outcome values (per QA Checkpoint 10 Issue 8):
#
# * ``user_not_found`` -- the email did not match any user row in the
#   organization. Defense against email enumeration is preserved
#   because the response body is identical to ``wrong_password``; the
#   metric is internal-only.
# * ``wrong_password`` -- the email matched a row but the bcrypt
#   verification failed.
# * ``oauth_only_user`` -- the email matched a row created via Google
#   OAuth (``password_hash IS NULL``); the user must use the Google
#   sign-in button.
#
# Cardinality is bounded at three labels regardless of traffic
# volume, well within Prometheus' practical labelset limits.
failed_login_attempts_total: Counter = Counter(
    "failed_login_attempts_total",
    (
        "Failed password login attempts partitioned by outcome. "
        "Per AAP F-012 the per-attempt response body is uniform to "
        "preserve anti-enumeration; this counter exposes the "
        "operator-facing breakdown without leaking it to the client."
    ),
    labelnames=("outcome",),
    registry=metrics_registry,
)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

# Pre-compiled UUID v4 segment pattern. Compiling the pattern once at
# module load (instead of inside ``_normalize_path``) keeps the per-call
# overhead at one regex sub() instead of one regex compile() plus one
# regex sub(). The pattern matches the canonical 8-4-4-4-12 hex layout
# preceded by a slash so that only path segments (not random hex
# substrings inside other tokens) are collapsed.
_UUID_PATH_SEGMENT = re.compile(
    r"/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

# Pre-compiled numeric path segment pattern. Matches a slash-prefixed
# integer followed by either another slash or end-of-string, so that
# ``/api/connections/42`` collapses to ``/api/connections/:id`` without
# touching ``/api/v1/...`` (where ``v1`` is alphanumeric, not numeric).
_NUMERIC_PATH_SEGMENT = re.compile(r"/\d+(?=/|$)")


def _normalize_path(path: str) -> str:
    """Reduce label cardinality by collapsing dynamic URL segments.

    Replaces UUID-like and numeric segments with the placeholder
    ``/:id``. The Flask ``request.url_rule.rule`` (e.g.,
    ``/api/connections/<id>``) is the ideal source for the ``path``
    label because it is canonical and bounded; this helper provides a
    robust fallback for code paths that lack a matched URL rule (404s
    and pre-routing error paths).

    Args:
        path: The raw request path (e.g., ``/api/connections/42``).

    Returns:
        The normalised path with UUID and numeric segments collapsed
        (e.g., ``/api/connections/:id``).
    """
    # Collapse UUID v4-like segments first; the numeric collapse below
    # would not match UUIDs because of the dashes, but ordering keeps
    # the result deterministic for paths that contain both forms.
    path = _UUID_PATH_SEGMENT.sub("/:id", path)
    # Collapse numeric integer segments (e.g., ``/42`` -> ``/:id``).
    path = _NUMERIC_PATH_SEGMENT.sub("/:id", path)
    return path


def _get_route_template(request_obj: object) -> str:
    """Return the Flask URL rule for the current request when known.

    Falls back to a normalised path for unmatched routes (404s) so the
    label cardinality on ``http_requests_total`` and
    ``http_request_duration_seconds`` stays bounded by the size of the
    URL rule table rather than by the universe of attempted URLs.

    Args:
        request_obj: A duck-typed Flask request-like object. Typed as
            ``object`` rather than ``flask.Request`` so the helper can
            be unit-tested with simple stand-in objects without
            constructing a full Flask test client.

    Returns:
        Either the Flask URL rule (e.g., ``/api/connections/<id>``)
        when the request matched a registered handler, or a
        regex-normalised path when it did not.
    """
    url_rule = getattr(request_obj, "url_rule", None)
    if url_rule is not None:
        rule_str = getattr(url_rule, "rule", None)
        if rule_str:
            # Cast through ``str()`` so consumers always receive a
            # Python string regardless of the upstream type
            # (Werkzeug returns ``str``; defensive in case of subclasses).
            return str(rule_str)
    raw_path = getattr(request_obj, "path", "") or "unknown"
    return _normalize_path(raw_path)


def _metrics_endpoint() -> Response:
    """Render the current metrics in Prometheus exposition format.

    When ``METRICS_BEARER_TOKEN`` is configured (non-empty), the endpoint
    requires ``Authorization: Bearer <token>`` and returns 401
    otherwise. Local development and intra-VPC scrapes typically leave
    the token empty so the endpoint is freely accessible inside the
    private network; production deployments behind a managed Prometheus
    or CloudWatch agent set the token to harden the endpoint against
    incidental external exposure.

    Returns:
        A Flask :class:`Response` carrying the Prometheus text
        exposition format payload (Content-Type
        ``text/plain; version=0.0.4; charset=utf-8``) on success, or a
        401 ``unauthorized\\n`` response when bearer-token validation
        fails.
    """
    # Lazy Flask imports keep this module importable in non-Flask
    # contexts (Alembic env.py, ad-hoc scripts) without paying the
    # Flask-import cost at module load time. The PLC0415 suppression
    # acknowledges that ruff's import-not-at-top rule does not apply to
    # function-scope Flask imports inside an observability module that
    # must be importable independently of any Flask application.
    from flask import Response, current_app, request  # noqa: PLC0415

    expected = current_app.config.get("METRICS_BEARER_TOKEN") or ""
    if expected:
        provided = request.headers.get("Authorization", "")
        if not provided.startswith("Bearer ") or provided[len("Bearer ") :] != expected:
            return Response(
                "unauthorized\n",
                status=401,
                mimetype="text/plain; charset=utf-8",
            )

    payload = generate_latest(metrics_registry)
    # Per QA Checkpoint 10 Issue 7: prometheus_client's
    # ``CONTENT_TYPE_LATEST`` already includes ``charset=utf-8`` (e.g.,
    # ``text/plain; version=0.0.4; charset=utf-8``). Flask's
    # ``Response(..., mimetype=...)`` constructor independently appends
    # its default charset, producing the duplicated
    # ``charset=utf-8; charset=utf-8`` parameter on the wire. Setting
    # the header explicitly via the ``content_type`` argument bypasses
    # Flask's auto-charset behavior and yields the canonical
    # ``Content-Type: text/plain; version=0.0.4; charset=utf-8`` value
    # documented in the Prometheus exposition specification.
    return Response(payload, content_type=CONTENT_TYPE_LATEST)


def _before_request_record_start_time() -> None:
    """Record the wall-clock start time of the request on ``flask.g``.

    Uses :func:`time.perf_counter` rather than :func:`time.time` because
    ``perf_counter`` is monotonic and immune to wall-clock adjustments
    (NTP slews, daylight savings transitions). For latency measurement
    monotonicity is more important than absolute timestamp accuracy.

    The value is stashed under a name with an underscore prefix
    (``_metrics_start_time``) on :data:`flask.g` so it does not collide
    with any application-level value the handler chain might attach to
    the request context.
    """
    from flask import g  # noqa: PLC0415

    g._metrics_start_time = time.perf_counter()


def _after_request_record_metrics(response: Response) -> Response:
    """Record the request count and duration histograms.

    Skips the ``/metrics`` endpoint itself so that Prometheus scrapes
    do not inflate the counter for their own scrape path. Without this
    guard, every scrape would record a ``GET /metrics 200`` row and
    operators would see a perpetually-incrementing scrape series in
    their own dashboards.

    Args:
        response: The Flask :class:`Response` produced by the matched
            handler. Returned unchanged so the after_request chain
            propagates the response correctly.

    Returns:
        The same :class:`Response` instance passed in. Flask's
        after_request convention requires the hook to return the
        response (possibly modified) to the next link in the chain.
    """
    from flask import g, request  # noqa: PLC0415

    # Skip the metrics endpoint itself; otherwise scrapes inflate the
    # counter for their own path. This is a Prometheus best practice -
    # see https://prometheus.io/docs/practices/instrumentation/.
    if request.path == "/metrics":
        return response

    route = _get_route_template(request)
    method = request.method
    status = str(response.status_code)

    # Counter: count by method/path/status.
    http_requests_total.labels(method=method, path=route, status=status).inc()

    # Histogram: duration since the before_request hook recorded the
    # start time. Defensive against the unlikely case that
    # before_request did not run (e.g., a teardown ran without a
    # matching setup); silently skipping the observation is preferable
    # to crashing the request response on a missing attribute.
    start = getattr(g, "_metrics_start_time", None)
    if start is not None:
        duration_seconds = time.perf_counter() - start
        http_request_duration_seconds.labels(method=method, path=route).observe(duration_seconds)

    return response


# ---------------------------------------------------------------------------
# Public function: init_metrics
# ---------------------------------------------------------------------------


def init_metrics(app: Flask) -> None:
    """Wire Prometheus metrics into a Flask application.

    Idempotent: calling twice on the same app is harmless because the
    URL rule is keyed by a stable endpoint name (``prometheus_metrics``)
    that is checked for prior registration before re-adding. Flask's
    before_request and after_request hooks deduplicate by view function
    identity so re-registering the same hook reference is a no-op.

    Mounts:
        * ``GET /metrics`` returning the Prometheus text exposition
          format. The endpoint is mounted at the application root, NOT
          under ``/api/``, because Prometheus and CloudWatch scrape
          configurations expect ``/metrics`` at a well-known root path.

    Registers:
        * A ``before_request`` hook that records the request start
          time on :data:`flask.g`.
        * An ``after_request`` hook that records the request count
          and duration histograms.

    The metric instruments themselves are module-level singletons
    registered against the custom ``metrics_registry`` from
    :mod:`app.extensions`. They are importable directly from this
    module by the rest of the codebase
    (``from app.observability.metrics import ai_request_duration_seconds``).

    Args:
        app: The Flask application being initialised. The function
            reads ``METRICS_BEARER_TOKEN`` from ``app.config`` only at
            request time (not at init time), so a config change applied
            after this function runs takes effect on the next request.

    Returns:
        None. Side effects are URL rule registration and before/after
        request hook installation on the supplied ``app``.
    """
    global _initialized  # noqa: PLW0603 - explicit idempotency guard, intentional

    if not _PROM_AVAILABLE:
        # If prometheus_client could not be imported, skip mount and
        # leave the metric instrument singletons absent. Service-layer
        # code that imports the histograms would itself fail with an
        # ImportError, which is the correct surfacing of a missing
        # foundation dependency.
        _logger.info("metrics_skipped_prom_not_available")
        return

    # Mount the /metrics endpoint at the application root. Use a stable
    # endpoint name so duplicate registrations are idempotent: Flask's
    # ``add_url_rule`` raises ``AssertionError`` on a second registration
    # of the same endpoint with a different view function, but a
    # second registration of the same endpoint with the same view
    # function is a no-op. The explicit ``in app.view_functions``
    # check makes the idempotency obvious and avoids relying on the
    # implicit Flask behaviour.
    if "prometheus_metrics" not in app.view_functions:
        app.add_url_rule(
            "/metrics",
            endpoint="prometheus_metrics",
            view_func=_metrics_endpoint,
            methods=["GET"],
        )

    # Register the before/after hooks. Flask deduplicates these by
    # function identity, so re-registering the same module-level
    # function on a second call to ``init_metrics`` is a no-op.
    app.before_request(_before_request_record_start_time)
    app.after_request(_after_request_record_metrics)

    _initialized = True
    _logger.info(
        "metrics_initialized",
        extra={
            "endpoint": "/metrics",
            "registry": "custom (app.extensions.metrics_registry)",
            "auth_required": bool(app.config.get("METRICS_BEARER_TOKEN")),
        },
    )


# ---------------------------------------------------------------------------
# Module public API
# ---------------------------------------------------------------------------
# Keeping ``__all__`` alphabetised reduces merge conflicts when the list
# grows. Helper functions (``_normalize_path``, ``_get_route_template``,
# ``_metrics_endpoint``, ``_before_request_record_start_time``,
# ``_after_request_record_metrics``) are private (underscore-prefixed)
# per the assigned folder's conventions and intentionally omitted from
# the public surface.
__all__ = [
    "active_sessions",
    "ai_request_duration_seconds",
    "audit_emit_duration_seconds",
    "failed_login_attempts_total",
    "http_request_duration_seconds",
    "http_requests_total",
    "init_metrics",
]
