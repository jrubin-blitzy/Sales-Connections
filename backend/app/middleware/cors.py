"""Cross-Origin Resource Sharing (CORS) middleware.

This module installs Flask before/after hooks that implement the
narrow subset of the CORS specification (RFC 6454 + W3C Fetch CORS
algorithm) required by the Sales-Connections SPA. It satisfies QA
Issue 12 (MAJOR) which observed that the configured
``CORS_ALLOWED_ORIGINS`` and ``CORS_ALLOW_CREDENTIALS`` values were
defined in ``app.config`` but never wired into request processing.

Why a hand-rolled implementation rather than ``flask-cors``:
    * Avoids adding a third-party dependency for a small, well-
      understood surface (only origin allowlist + credentials +
      preflight handling are required).
    * Lets us run BEFORE the auth middleware so the OPTIONS
      preflight (which never carries the session cookie) does not
      get rejected with 401. ``flask-cors`` registers as a generic
      ``after_request`` hook and does not deterministically intercept
      OPTIONS preflight before our auth ``before_request`` runs.
    * Mirrors the explicit, auditable style used by the other
      middleware modules in ``backend/app/middleware/``.

Behavior:
    * **Preflight (OPTIONS with ``Access-Control-Request-Method``)**:
      ``before_request`` short-circuits with HTTP 204 and the full
      preflight response headers when the request's ``Origin`` is on
      the allowlist. Returning a response from a Flask
      ``before_request`` hook bypasses any subsequent
      ``before_request`` hooks (in particular the auth middleware),
      which is exactly what the CORS spec requires for preflight.
    * **Actual requests (any method)**: ``after_request`` echoes
      ``Access-Control-Allow-Origin`` (mirroring the request
      ``Origin`` if it is on the allowlist) plus
      ``Access-Control-Allow-Credentials`` when configured. The
      ``Vary: Origin`` header is appended so caches do not serve a
      response targeted at one origin to another origin.
    * **Disallowed origins**: no ``Access-Control-Allow-*`` headers
      are added; the browser will block the response. The request
      itself is NOT rejected with 4xx so server-to-server callers
      (curl, Postman) without an ``Origin`` header continue to work.

Per AAP Section 0.5.2 Layer 0 the middleware order is:
    correlation -> CORS -> auth -> rbac_error_handlers -> error_handlers

CORS is registered AFTER correlation (so log lines emitted from CORS
hooks carry the correlation_id) and BEFORE auth (so OPTIONS
preflight bypasses authentication entirely).
"""

from __future__ import annotations

# Standard library imports.
import logging as _stdlib_logging
from typing import TYPE_CHECKING, Final

from flask import current_app, make_response, request

# Third-party imports. The runtime imports from Flask and structlog
# are needed in the request hooks below; ``Flask`` and ``Response``
# are type-only and gated under ``TYPE_CHECKING`` so the module's
# import-time surface is minimal and ruff's TC002 (typing-only
# import) rule is satisfied.
import structlog

if TYPE_CHECKING:
    from flask import Flask, Response

# Module-level stdlib logger used only at registration time. Per-
# request logging uses structlog so the correlation_id binding is
# preserved.
_logger: Final[_stdlib_logging.Logger] = _stdlib_logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public re-exports
# ---------------------------------------------------------------------------
__all__ = [
    "register_cors_middleware",
]


# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

# HTTP methods the SPA is expected to issue against the API. We list
# them explicitly rather than using a wildcard ``*`` because the
# ``Access-Control-Allow-Methods`` header is only consulted by the
# browser on preflight; an explicit allowlist makes the intent clear
# and prevents accidental enablement of methods like ``TRACE``.
_ALLOWED_METHODS: Final[tuple[str, ...]] = (
    "GET",
    "POST",
    "PATCH",
    "DELETE",
    "OPTIONS",
)

# Request headers the SPA is expected to send. ``Content-Type`` is
# the canonical ``application/json`` POST/PATCH header. ``Authorization``
# is included for the email/password Bearer-token fallback. ``X-
# Correlation-Id`` matches the header the SPA's fetch wrapper sets
# (see ``frontend/src/api/client.ts``); without it the browser would
# strip the header from cross-origin requests.
_ALLOWED_HEADERS: Final[tuple[str, ...]] = (
    "Content-Type",
    "Authorization",
    "X-Correlation-Id",
)

# Response headers the SPA must be able to read. ``X-Correlation-Id``
# is exposed so the SPA can correlate logs and trace data with the
# response it received. Other observability headers (e.g.,
# ``X-Request-Id`` if added later) should be appended here.
_EXPOSED_HEADERS: Final[tuple[str, ...]] = ("X-Correlation-Id",)

# Cache duration (seconds) the browser may keep the preflight result
# without re-issuing the OPTIONS request. 600 s = 10 min is the
# Chromium-imposed maximum; setting it higher is silently capped.
_PREFLIGHT_MAX_AGE_SECONDS: Final[int] = 600


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_allowed_origins() -> frozenset[str]:
    """Return the configured ``CORS_ALLOWED_ORIGINS`` as a frozenset.

    Reads from ``current_app.config`` so the value can be overridden
    per-environment (development uses the Vite dev origin
    ``http://localhost:5173``; production uses the SPA's CloudFront
    distribution domain). Returns a frozenset for O(1) origin
    membership tests in the request hot path.

    Returns:
        frozenset of canonical origin strings (no trailing slash).
        Empty frozenset disables CORS entirely (every cross-origin
        request will be blocked by the browser).
    """
    raw = current_app.config.get("CORS_ALLOWED_ORIGINS") or []
    # Normalize: strip whitespace; drop empties; remove any trailing
    # slash so the comparison matches the browser-supplied ``Origin``
    # header verbatim (browsers never send a trailing slash on the
    # Origin header).
    return frozenset(o.strip().rstrip("/") for o in raw if o and o.strip())


def _resolve_allow_credentials() -> bool:
    """Return whether credentialed cross-origin requests are allowed.

    When True (the default per AAP), the ``Access-Control-Allow-
    Credentials: true`` response header is set so the SPA may include
    the ``HttpOnly`` session cookie on cross-origin fetches with
    ``credentials: 'include'``.
    """
    return bool(current_app.config.get("CORS_ALLOW_CREDENTIALS", True))


def _is_preflight() -> bool:
    """Return True iff the current request is a CORS preflight.

    A CORS preflight is an HTTP OPTIONS request that carries the
    ``Access-Control-Request-Method`` header. This is the only
    distinguishing signal documented by the W3C Fetch CORS spec; we
    DO NOT treat plain OPTIONS requests (without the
    ``Access-Control-Request-Method`` header) as preflights because
    those are server-to-server CORS-discovery probes that should be
    handled by the application, not short-circuited to 204.
    """
    return (
        request.method == "OPTIONS"
        and "Access-Control-Request-Method" in request.headers
    )


# ---------------------------------------------------------------------------
# Flask hook implementations
# ---------------------------------------------------------------------------


def _before_request_cors() -> Response | None:
    """before_request hook: short-circuit CORS preflight.

    Decision tree:
        1. Not a preflight (OPTIONS + AC-Request-Method absent) ->
           return None; downstream middleware (auth) runs as normal.
        2. Preflight from a non-allowlisted origin -> return None;
           the auth/handler chain runs and the response will lack
           any ``Access-Control-Allow-*`` headers, causing the
           browser to block the response (the correct CORS-spec
           outcome). NOTE: we explicitly do NOT 403 the request
           here; CORS is a browser-enforced policy, not a server-
           authorization signal.
        3. Preflight from an allowlisted origin -> build a 204 No
           Content response with the full preflight header set
           (``Access-Control-Allow-Origin``, ``-Credentials``,
           ``-Methods``, ``-Headers``, ``-Max-Age``, ``Vary``) and
           return it. Returning a Response from a before_request
           hook causes Flask to skip remaining before_request hooks
           AND skip the route handler entirely; only after_request
           hooks run on the way out, which is exactly what we want
           (the after_request hook will not double-attach headers
           because it short-circuits when ``Access-Control-Allow-
           Origin`` is already set).

    Returns:
        Response on preflight short-circuit; None for all other
        requests so the auth middleware and route handler proceed.
    """
    if not _is_preflight():
        return None

    origin = request.headers.get("Origin", "").rstrip("/")
    allowed_origins = _resolve_allowed_origins()
    logger = structlog.get_logger("app.middleware.cors")

    if not origin or origin not in allowed_origins:
        # Not an allowed origin. Per CORS spec we MUST NOT echo the
        # origin back. Letting the request fall through ensures that
        # OPTIONS requests without an Origin (CORS-discovery probes)
        # still reach the route handler if one is defined; in
        # practice Flask returns 405 for unhandled OPTIONS which is
        # acceptable.
        logger.info(
            "cors_preflight_origin_disallowed",
            origin=origin or None,
            path=request.path,
            allowed_count=len(allowed_origins),
        )
        return None

    # Build the 204 preflight response.
    response = make_response("", 204)
    response.headers["Access-Control-Allow-Origin"] = origin
    if _resolve_allow_credentials():
        response.headers["Access-Control-Allow-Credentials"] = "true"
    response.headers["Access-Control-Allow-Methods"] = ", ".join(_ALLOWED_METHODS)

    # Echo the requested headers if provided; otherwise fall back to
    # the static allowlist. Echoing avoids stale-allowlist bugs when
    # the SPA adds a new custom header.
    requested_headers = request.headers.get("Access-Control-Request-Headers")
    if requested_headers:
        response.headers["Access-Control-Allow-Headers"] = requested_headers
    else:
        response.headers["Access-Control-Allow-Headers"] = ", ".join(_ALLOWED_HEADERS)

    response.headers["Access-Control-Max-Age"] = str(_PREFLIGHT_MAX_AGE_SECONDS)
    response.headers["Vary"] = "Origin"

    logger.info(
        "cors_preflight_allowed",
        origin=origin,
        path=request.path,
        requested_method=request.headers.get("Access-Control-Request-Method"),
    )
    return response


def _after_request_cors(response: Response) -> Response:
    """after_request hook: attach CORS headers to non-preflight responses.

    Runs for every response (including those produced by the auth
    middleware's 401 short-circuit and the error_handlers' JSON
    envelopes). Behavior:

        * No ``Origin`` header on the request -> no CORS headers
          added (server-to-server caller; CORS is irrelevant).
        * Origin not on the allowlist -> no CORS headers added; the
          browser will block the response.
        * Origin on the allowlist -> set ``Access-Control-Allow-
          Origin`` (echo, not wildcard, because credentials are in
          play and the spec forbids ``*`` with credentials),
          ``Access-Control-Allow-Credentials`` if configured, and
          append ``Origin`` to the ``Vary`` header so caches do not
          conflate responses across origins.

    Idempotent: if ``Access-Control-Allow-Origin`` is already set
    (which happens for the preflight short-circuit response built in
    ``_before_request_cors``), we do NOT overwrite it but DO ensure
    ``Vary: Origin`` is present.

    Args:
        response: the Flask Response object on its way to the client.

    Returns:
        The same Response with CORS headers possibly added. The
        return is required because Flask passes the response by
        reference but consumes the return value as the canonical
        post-hook response.
    """
    # If the preflight branch already populated ACAO we are done.
    # The Vary: Origin header may not have been propagated by an
    # ``after_request`` chain entry that built the response from
    # scratch, so we still ensure it is present below.
    origin = request.headers.get("Origin", "").rstrip("/")
    if origin:
        allowed_origins = _resolve_allowed_origins()
        if (
            origin in allowed_origins
            and "Access-Control-Allow-Origin" not in response.headers
        ):
            response.headers["Access-Control-Allow-Origin"] = origin
            if _resolve_allow_credentials():
                response.headers["Access-Control-Allow-Credentials"] = "true"
            # Expose the correlation-id header so the SPA can read it
            # via the fetch Response.headers API (without ``Access-
            # Control-Expose-Headers`` only the safelisted CORS
            # response headers are visible to JS).
            response.headers["Access-Control-Expose-Headers"] = ", ".join(
                _EXPOSED_HEADERS
            )

        # Append (do not overwrite) ``Origin`` to ``Vary`` so any
        # caching layer (CloudFront, browser cache) keys the response
        # by Origin. This is required for correctness even when the
        # origin is NOT on the allowlist (a cache populated from a
        # privileged origin must not serve to a different origin).
        existing_vary = response.headers.get("Vary", "")
        if "origin" not in existing_vary.lower():
            response.headers["Vary"] = (
                f"{existing_vary}, Origin" if existing_vary else "Origin"
            )

    return response


# ---------------------------------------------------------------------------
# Public registration function
# ---------------------------------------------------------------------------


def register_cors_middleware(app: Flask) -> None:
    """Register CORS middleware on the Flask app.

    Registers two hooks:
        * ``before_request`` -> ``_before_request_cors`` (preflight
          short-circuit)
        * ``after_request``  -> ``_after_request_cors`` (header
          attachment for actual requests)

    This MUST be registered AFTER ``register_correlation_middleware``
    (so log lines emitted from these hooks carry the correlation_id)
    and BEFORE ``register_auth_middleware`` (so OPTIONS preflight
    bypasses authentication entirely).

    Idempotent: registering the same hooks twice is harmless because
    Flask deduplicates by view-function identity, and the module-
    level helper functions have stable identity across re-
    registrations.

    Args:
        app: The Flask application instance produced by
            ``app.create_app``.

    Returns:
        None. Side effects: mutates ``app.before_request_funcs`` and
        ``app.after_request_funcs``; emits a single
        ``cors_middleware_registered`` info log line via the stdlib
        logger so operators can confirm wiring at startup.
    """
    app.before_request(_before_request_cors)
    app.after_request(_after_request_cors)
    # Resolve config eagerly here only for the registration log line;
    # per-request resolution happens in the hooks above.
    raw_origins = app.config.get("CORS_ALLOWED_ORIGINS") or []
    _logger.info(
        "cors_middleware_registered",
        extra={
            "allowed_origins": list(raw_origins),
            "allow_credentials": bool(app.config.get("CORS_ALLOW_CREDENTIALS", True)),
            "allowed_methods": list(_ALLOWED_METHODS),
        },
    )
