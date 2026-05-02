"""Health check endpoints for the Sales-Connections backend (observability).

Two endpoints serve different operational concerns:

- ``GET /healthz`` -- Liveness check. Always returns 200 OK as long as
  the Python process is alive and able to accept and respond to HTTP
  requests. Used by ECS Fargate task health checks to detect deadlocks
  or hung workers and trigger container restarts.

- ``GET /readyz`` -- Readiness check. Returns 200 OK only when the
  application is ready to serve real traffic. Verifies database
  connectivity via a parameter-free ``SELECT 1`` round-trip with a
  1-second timeout. Returns 503 Service Unavailable when any critical
  dependency is degraded. Used by the ALB to decide whether to route
  traffic to this task and by Kubernetes-style orchestrators to delay
  rolling deploys until all replicas pass readiness.

Per AAP Section 0.5.2 Layer 0:

    "/healthz returns 200 unconditionally; /readyz returns 200 only if
    a SELECT 1 round-trip to RDS succeeds within 1 second."

Per AAP Section 0.7.5, observability is a hard requirement: the
application is not considered complete without functional health and
readiness checks.

These endpoints are EXEMPT from the JWT authentication middleware
(see ``app.middleware.auth._PUBLIC_PATHS``). They never carry a
correlation ID requirement, never log PII, and never expose secrets
in their response bodies.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from flask import Blueprint, current_app, jsonify
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db

if TYPE_CHECKING:
    from flask.wrappers import Response


# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------
# The stdlib logger emits records that ``app.observability.logging``
# routes through structlog's processor chain, so probe-failure
# messages are rendered as JSON with the canonical context fields
# (correlation_id, user_id, org_id, trace_id) attached automatically.
_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------
# Timeout (seconds) for the readiness DB ping. Per AAP Section 0.5.2
# Layer 0, the readiness probe must complete within 1 second to avoid
# being a source of latency for orchestration-tier health polling.
_READYZ_DB_TIMEOUT_SECONDS: float = 1.0

# The parameter-free query used to verify connectivity. We use
# ``SELECT 1`` rather than something more elaborate because:
#   1. It does not depend on any application table (so it works before
#      and after migrations and never trips on schema drift).
#   2. It does not allocate locks or planner work of substance.
#   3. PostgreSQL responds in microseconds for an established
#      connection, so any duration above tens of milliseconds is
#      diagnostic of pool exhaustion or network trouble.
_READYZ_PROBE_SQL: str = "SELECT 1"


# ---------------------------------------------------------------------------
# Blueprint construction
# ---------------------------------------------------------------------------
# The blueprint is named ``"health"`` so handlers can be referenced as
# ``url_for("health.liveness")`` and ``url_for("health.readiness")``.
# No ``url_prefix`` is supplied: ``backend/app/api/__init__.py`` MUST
# register this blueprint without a prefix so the routes mount at the
# application root (``/healthz`` and ``/readyz``), NOT under ``/api``.
# Orchestration-tier health polling expects the well-known root paths.
health_bp = Blueprint("health", __name__)


# ---------------------------------------------------------------------------
# Public module surface
# ---------------------------------------------------------------------------
# Only the blueprint object is part of the module's public surface.
# The route handlers (``liveness``, ``readiness``) and the helper
# (``_probe_database``) are intentionally NOT exported: callers should
# go through HTTP requests against the registered routes rather than
# invoking the view functions directly.
__all__ = ["health_bp"]


def _probe_database() -> dict[str, Any]:
    """Run a bounded ``SELECT 1`` and report the outcome.

    Returns a dict with keys:

    - ``status``: ``"ok"`` or ``"error"``.
    - ``duration_ms``: float, round-trip wall-clock time in
      milliseconds (always populated; useful for trending and for
      operators inspecting probe responses during incidents).
    - ``error`` (only when ``status == "error"``): the exception class
      name (e.g., ``"OperationalError"``, ``"TimeoutExceeded"``,
      ``"RuntimeError"``). The full error message is logged at
      WARNING level via the module logger but is intentionally NOT
      included in the returned dict to avoid leaking infrastructure
      details (DSN strings, hostnames, traceback frames) to public
      clients that may reach the readiness endpoint.

    The probe acquires a connection from the SQLAlchemy engine pool
    directly (``db.engine.connect()``) rather than through a Session
    so it does not interfere with any ongoing transaction-tracking
    tooling and avoids the overhead of the ORM identity map.

    Returns:
        dict[str, Any]: The structured probe outcome described above.
    """
    start = time.perf_counter()
    try:
        # Acquire a connection from the engine pool. The context
        # manager guarantees the connection is returned to the pool
        # even on exception, preventing pool exhaustion when the
        # probe fails partway through.
        with db.engine.connect() as conn:
            conn.execute(text(_READYZ_PROBE_SQL))
        duration_seconds = time.perf_counter() - start
        duration_ms = round(duration_seconds * 1000.0, 2)

        # Bound by the 1-second budget per AAP Section 0.5.2 Layer 0.
        # A successful round-trip that exceeds the budget is reported
        # as a failure so the orchestrator can route around a
        # degraded but technically reachable database.
        if duration_seconds > _READYZ_DB_TIMEOUT_SECONDS:
            _logger.warning(
                "readyz_db_probe_slow",
                extra={
                    "duration_ms": duration_ms,
                    "budget_seconds": _READYZ_DB_TIMEOUT_SECONDS,
                },
            )
            return {
                "status": "error",
                "duration_ms": duration_ms,
                "error": "TimeoutExceeded",
            }

        return {
            "status": "ok",
            "duration_ms": duration_ms,
        }

    except SQLAlchemyError as exc:
        # Catches the broad SQLAlchemy/psycopg surface: connection
        # refused, DNS failure, authentication errors, query
        # timeouts, pool exhaustion, etc. The full exception repr
        # goes to the operator-facing log; only the class name
        # surfaces in the public response body.
        duration_ms = round((time.perf_counter() - start) * 1000.0, 2)
        _logger.warning(
            "readyz_db_probe_failed",
            extra={
                "duration_ms": duration_ms,
                "error_class": type(exc).__name__,
                "error_repr": repr(exc),
            },
        )
        return {
            "status": "error",
            "duration_ms": duration_ms,
            "error": type(exc).__name__,
        }
    except Exception as exc:
        # A health-probe handler MUST NOT propagate exceptions. If
        # something unexpected fails during the probe (e.g., the
        # SQLAlchemy wrapper has not been initialised yet so
        # ``db.engine`` raises RuntimeError, or a transient OS-level
        # error escapes SQLAlchemy's exception hierarchy), the right
        # behaviour is to report 503 (degraded), NOT to bubble up a
        # 500 (which would cause the orchestrator to interpret the
        # worker as crashed when it might just be experiencing a
        # brief network or configuration blip).
        duration_ms = round((time.perf_counter() - start) * 1000.0, 2)
        _logger.warning(
            "readyz_db_probe_unexpected_error",
            extra={
                "duration_ms": duration_ms,
                "error_class": type(exc).__name__,
                "error_repr": repr(exc),
            },
        )
        return {
            "status": "error",
            "duration_ms": duration_ms,
            "error": type(exc).__name__,
        }


@health_bp.route("/healthz", methods=["GET"])
def liveness() -> tuple[Response, int]:
    """Liveness probe: always 200 OK while the process is alive.

    No external dependencies are checked. The endpoint is
    unconditionally successful as long as the Flask worker is able to
    dispatch the request -- which by definition means the process is
    alive and the WSGI plumbing is intact.

    Response shape::

        {"status": "ok", "service": "sales-connections-api"}

    Used by ECS Fargate to detect hung workers and trigger container
    restarts (failing liveness => kill + replace). Used by Docker
    Compose ``healthcheck`` blocks for local development. NOT used by
    the ALB target group health check; that role belongs to
    ``/readyz`` since the ALB cares about traffic eligibility, not
    process aliveness.

    Returns:
        tuple[Response, int]: A Flask response with the JSON payload
        and HTTP 200 status code.
    """
    return (
        jsonify(
            {
                "status": "ok",
                "service": current_app.config.get("OTEL_SERVICE_NAME", "sales-connections-api"),
            }
        ),
        200,
    )


@health_bp.route("/readyz", methods=["GET"])
def readiness() -> tuple[Response, int]:
    """Readiness probe: 200 only when downstream dependencies are healthy.

    Verifies database connectivity with a ``SELECT 1`` round-trip
    bounded by a 1-second wall-clock timeout. Returns 503 Service
    Unavailable on any failure (network unreachable, pool exhausted,
    query timeout, configuration error, etc.).

    Success response (HTTP 200)::

        {
            "status": "ok",
            "service": "sales-connections-api",
            "checks": {"database": {"status": "ok", "duration_ms": 3.2}},
        }

    Failure response (HTTP 503)::

        {
            "status": "degraded",
            "service": "sales-connections-api",
            "checks": {
                "database": {
                    "status": "error",
                    "duration_ms": 1023.5,
                    "error": "<error class name>",
                }
            },
        }

    The error message in the response body is restricted to the
    exception class name (e.g., ``"OperationalError"``); the full
    string-form of the exception is logged at WARNING level by
    :func:`_probe_database` but NOT echoed to clients. This is
    defence in depth: even if the readiness endpoint is accidentally
    exposed to the public Internet, no infrastructure details (DSN,
    hostnames, credential fragments embedded in error strings) leak.

    Returns:
        tuple[Response, int]: A Flask response with the structured
        JSON payload and either 200 (healthy) or 503 (degraded).
    """
    service_name = current_app.config.get("OTEL_SERVICE_NAME", "sales-connections-api")

    db_check = _probe_database()

    overall_ok = db_check["status"] == "ok"
    payload = {
        "status": "ok" if overall_ok else "degraded",
        "service": service_name,
        "checks": {"database": db_check},
    }

    return jsonify(payload), (200 if overall_ok else 503)
