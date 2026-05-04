"""HTTP response security-headers middleware.

This module installs a Flask ``after_request`` hook that attaches a
canonical set of defensive HTTP response headers to every response
returned by the backend. The headers complement the AWS ALB TLS edge
and the AAP Section 0.7.4 security invariants by hardening the
browser-side processing of every backend response.

Why this middleware exists
==========================

Per AAP Section 0.4.6, the production deployment topology routes
``/api/*`` and ``/auth/*`` directly from the AWS Application Load
Balancer to the Flask backend running on ECS Fargate; nginx is NOT
interposed on those paths. The frontend nginx server (which sets the
same defensive headers via ``add_header`` directives in
``frontend/nginx.conf`` lines 100-104) handles ONLY the SPA static-
asset path served by the separate frontend container. Without this
middleware, production API responses would lack the defensive headers
entirely, and the QA security checkpoint flagged this as the only
remaining MEDIUM-severity gap (see ``docs/decision-log.md`` row
``DL-0044``).

Headers attached
================

The middleware attaches the following headers, all using
``setdefault``-style semantics so any header explicitly set by an
upstream handler is preserved verbatim:

* ``X-Content-Type-Options: nosniff`` (every response)
    Disables browser MIME sniffing on declared ``Content-Type``
    values. Defense-in-depth: even if a misconfigured intermediate
    proxy were to strip the ``Content-Type`` header from a JSON
    response, the browser would still refuse to render it as HTML.

* ``X-Frame-Options: DENY`` (every response)
    Forbids embedding any backend response inside an iframe. The
    application has no legitimate embed use case (the executive deck
    in ``blitzy-deck/index.html`` is served as a top-level page);
    rejecting all framing closes off the clickjacking attack surface.

* ``Referrer-Policy: strict-origin-when-cross-origin`` (every response)
    Sends the full origin on same-origin requests but downgrades to
    origin-only on cross-origin navigations. The query string and
    path never leak to third-party origins, which protects
    correlation IDs and any other URL-borne identifiers from
    cross-site analytics traps.

* ``Permissions-Policy: geolocation=(), microphone=(), camera=()``
    (every response) Explicitly disables browser capabilities the
    application does not use. Reduces the post-XSS attack surface in
    the unlikely event that an attacker gains script execution; the
    parenthesized empty allowlist ``()`` is the modern syntax that
    replaces the deprecated ``Feature-Policy`` header.

* ``Cache-Control: no-store, no-cache, must-revalidate, private``
    (only on paths starting with ``/api/`` or ``/auth/`` and only
    when the request method is not ``OPTIONS``) Backend API and auth
    responses carry session-scoped data: user identity at
    ``/api/me``, the user's records at ``/api/connections``, OAuth
    tokens at ``/auth/google/callback``. None of these may be cached
    by the browser, by a CDN, or by an intermediate proxy. The four
    directives are layered:

        - ``no-store`` forbids any cache from retaining the response
          body or headers (the strictest directive).
        - ``no-cache`` requires revalidation against the origin for
          every use of any cached copy.
        - ``must-revalidate`` makes the directive binding even when
          a cache is operating in stale-while-revalidate mode.
        - ``private`` prevents shared caches (CloudFront, ALB, any
          downstream CDN) from storing a copy.

    ``OPTIONS`` preflight responses are intentionally exempt so
    browsers honor the ``Access-Control-Max-Age`` directive set by
    the CORS middleware (see ``backend/app/middleware/cors.py``);
    caching preflights is a CORS-prescribed performance optimization.

    The path filter is intentionally narrow: ``/healthz``,
    ``/readyz``, and ``/metrics`` (used by container orchestration
    and Prometheus scrapers) are exempt because their pollers
    legitimately benefit from short-window caching, and they carry
    no user-scoped data.

* ``Strict-Transport-Security: max-age=31536000; includeSubDomains``
    (only when ``SESSION_COOKIE_SECURE`` is True, i.e., production)
    Forces conformant browsers to prefer HTTPS for one year for the
    deployment domain and all subdomains. The ``preload`` directive
    is intentionally OMITTED to avoid the irreversible HSTS-preload
    list submission. The header is gated on ``SESSION_COOKIE_SECURE``
    so it is automatically disabled in development and testing where
    the dev server runs over plain HTTP and HSTS would otherwise
    block subsequent localhost interactions.

Headers NOT set by this middleware
==================================

* ``Content-Security-Policy`` -- the backend API serves only JSON;
  CSP is HTML-rendering specific and would be inert on a JSON
  response. The frontend nginx is responsible for any CSP attached
  to the SPA shell. Adding an unused CSP here would risk an
  inconsistent policy between SPA and API and complicate future
  policy updates.

* ``Server`` -- Gunicorn already sets ``Server: gunicorn`` (without
  a version tag); no additional masking is required.

* ``Pragma: no-cache`` -- legacy HTTP/1.0 header fully superseded by
  the ``Cache-Control`` directives above. Modern proxies and
  browsers honor ``Cache-Control``; emitting both would add noise
  without changing behavior.

Wiring
======

Per AAP Section 0.5.2 Layer 0, the middleware registration order is::

    correlation -> cors -> security_headers -> auth -> rbac -> error_handlers

Security headers are registered AFTER CORS so that the
``after_request`` chain runs in the order:

    1. correlation_after_request   (echoes ``X-Correlation-Id``)
    2. cors_after_request          (sets ``Access-Control-*`` for non-preflight)
    3. security_headers_after_request (this module; defensive headers layered on top)

This ordering is significant for the OPTIONS preflight short-circuit:
``cors._before_request_cors`` returns a 204 response from the
``before_request`` hook, which bypasses every subsequent
``before_request`` (auth, etc.) but DOES still flow through every
``after_request``, so the preflight response receives the security
headers too. The CORS-specific ``Access-Control-*`` headers are set
by the CORS middleware before this hook runs, so the ordering
preserves correctness for the preflight, the actual CORS response,
the 401 short-circuit from auth, and the standard 2xx/4xx/5xx
response paths.

Idempotency
===========

Every header is attached with a presence check (``if name not in
response.headers``); if a route handler explicitly set its own value
for any of these headers, the existing value is preserved verbatim.
This matches the convention established by the correlation middleware
(see ``app/middleware/correlation.py::_after_request_correlation``)
and prevents this module from overwriting more-specific values that
a future endpoint might set deliberately (for example, an admin
endpoint that wants its own ``Cache-Control: max-age=3600`` for a
public-data list).
"""

from __future__ import annotations

# Standard library imports.
#
# ``logging`` is imported under the alias ``_stdlib_logging`` to
# match the convention established by ``app/middleware/cors.py`` and
# to avoid the namespace conflict with ``app.observability.logging``
# (the project's structlog configuration module). The alias makes
# call sites explicit: this is the stdlib logger, not structlog.
import logging as _stdlib_logging
from typing import TYPE_CHECKING, Final

# Third-party runtime imports.
#
# ``current_app`` is a Flask LocalProxy that resolves to the active
# application context. We need it to read ``SESSION_COOKIE_SECURE``
# from the application's config in a per-request hook. ``request`` is
# the LocalProxy for the active request; we read its ``path`` and
# ``method`` to drive the conditional ``Cache-Control`` attachment.
from flask import current_app, request

# Type-only imports gated under ``TYPE_CHECKING`` so the runtime
# surface stays minimal. ``Flask`` is the application instance type;
# ``Response`` is the after_request hook's parameter type.
if TYPE_CHECKING:
    from flask import Flask, Response

# Module-level stdlib logger used only at registration time. Per-
# request logging is intentionally absent because the headers are
# applied on every response and per-request logging would generate
# meaningless noise; correlation_id binding is preserved through the
# correlation middleware's contextvars.
_logger: Final[_stdlib_logging.Logger] = _stdlib_logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public re-exports
# ---------------------------------------------------------------------------
__all__ = [
    "register_security_headers_middleware",
]


# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

# URL path prefixes whose responses carry strict ``Cache-Control``.
# The trailing slash is intentional: the prefix matcher uses
# ``str.startswith`` so ``/api/`` matches ``/api/me`` but does NOT
# match a hypothetical sibling root path ``/apiv2/...``. Listing the
# prefixes as a tuple preserves stable iteration order for the
# registration log message and makes the contract grep-able.
#
# ``/metrics`` is intentionally OMITTED because the AWS Distro for
# OpenTelemetry collector legitimately scrapes it on a short interval
# and a forced no-store would defeat any local optimization the
# collector might apply. ``/healthz`` and ``/readyz`` are intentionally
# OMITTED so ALB and ECS health-check pollers can cache the
# lightweight 200/503 responses for the brief intervals between
# checks. None of those three paths carry session-scoped data, so the
# omission is safe.
_CACHE_CONTROL_PREFIXES: Final[tuple[str, ...]] = ("/api/", "/auth/")

# ``Cache-Control`` directive value attached to sensitive paths.
# Layered directives:
#   - ``no-store``: forbid any cache from retaining the response.
#   - ``no-cache``: require revalidation for any cached copy.
#   - ``must-revalidate``: enforce revalidation even when stale.
#   - ``private``: prevent shared caches from storing the response.
# The four together produce the strongest possible "do not cache
# this" directive understood by all current major browsers and
# intermediate proxies.
_CACHE_CONTROL_VALUE: Final[str] = "no-store, no-cache, must-revalidate, private"

# Static security headers attached to EVERY response regardless of
# path or method. Stored in a dict so iteration order is stable and
# the contract is grep-able.
_STATIC_HEADERS: Final[dict[str, str]] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
}

# ``Strict-Transport-Security`` value applied only when the app
# config's ``SESSION_COOKIE_SECURE`` flag is True (production over
# HTTPS). Mirror of the canonical AWS-recommended default
# (one-year max-age plus ``includeSubDomains``); the ``preload``
# directive is OMITTED to avoid the irreversible HSTS-preload list
# submission, which would lock the deployment domain to HTTPS-only
# globally and require manual de-listing if ever rolled back.
_HSTS_VALUE: Final[str] = "max-age=31536000; includeSubDomains"


# ---------------------------------------------------------------------------
# after_request hook
# ---------------------------------------------------------------------------


def _after_request_security_headers(response: Response) -> Response:
    """Attach defensive HTTP response headers on every response.

    Runs as a Flask ``after_request`` hook for every response,
    including:

        * Successful 2xx responses returned by route handlers.
        * Error responses (4xx / 5xx) produced by
          ``register_error_handlers`` in
          ``app/middleware/error_handlers.py``.
        * Short-circuited responses produced by upstream middleware
          (e.g., the auth 401 envelope, the CORS 204 preflight).
        * Static and observability endpoint responses
          (``/healthz``, ``/readyz``, ``/metrics``).

    Behavior:

        1. The four static defensive headers
           (``X-Content-Type-Options``, ``X-Frame-Options``,
           ``Referrer-Policy``, ``Permissions-Policy``) are attached
           unconditionally.
        2. ``Cache-Control: no-store, no-cache, must-revalidate,
           private`` is attached only when:
              * The request method is NOT ``OPTIONS`` (preserves
                browser-side ``Access-Control-Max-Age`` caching of
                preflights), AND
              * The request path begins with ``/api/`` or ``/auth/``
                (excludes ``/healthz``, ``/readyz``, ``/metrics``,
                and any other public path).
        3. ``Strict-Transport-Security: max-age=31536000;
           includeSubDomains`` is attached only when
           ``current_app.config["SESSION_COOKIE_SECURE"]`` is True
           (production deploys over HTTPS; suppressed in dev/test
           where HSTS would lock the browser to HTTPS for localhost).

    Idempotency: each header is attached only when not already
    present. A handler that explicitly sets its own
    ``Cache-Control`` for a public-data list endpoint (or a future
    handler that needs a different ``X-Frame-Options`` for a hosted
    embed surface) is preserved verbatim. This matches the
    convention used by ``app/middleware/correlation.py``.

    Args:
        response: The Flask Response object on its way to the client.
            The same object is mutated in place; the return value is
            the same object (Flask requires the hook to return a
            response).

    Returns:
        The same Response with the security headers added (or
        preserved if already present). Flask consumes the return
        value as the canonical post-hook response, so returning the
        same object is the documented contract.
    """
    # Static headers - apply on every response regardless of path or
    # method. The ``setdefault``-style guard preserves any value that
    # a route handler explicitly set (e.g., a future handler that
    # wants ``X-Frame-Options: SAMEORIGIN`` for a hosted preview).
    for name, value in _STATIC_HEADERS.items():
        if name not in response.headers:
            response.headers[name] = value

    # Path-conditional Cache-Control. The ``request.method != "OPTIONS"``
    # check skips CORS preflight responses so browsers honor the
    # ``Access-Control-Max-Age`` directive set by the CORS middleware.
    # The ``startswith`` filter narrows the directive to user-scoped
    # API and auth paths only, leaving ``/healthz``, ``/readyz``, and
    # ``/metrics`` untouched. The combined condition keeps the
    # decision tree shallow and matches ruff's SIM102 nested-if
    # consolidation guideline.
    path = request.path or ""
    if (
        request.method != "OPTIONS"
        and path.startswith(_CACHE_CONTROL_PREFIXES)
        and "Cache-Control" not in response.headers
    ):
        response.headers["Cache-Control"] = _CACHE_CONTROL_VALUE

    # HSTS - production only. Gated on SESSION_COOKIE_SECURE which is
    # True only in ProductionConfig (per backend/app/config.py line
    # 431) and False in DevelopmentConfig (line 332) and TestingConfig
    # (line 394). This avoids forcing browsers to upgrade localhost
    # interactions to HTTPS during development. The combined condition
    # keeps the decision tree shallow per ruff's SIM102 guideline.
    if (
        current_app.config.get("SESSION_COOKIE_SECURE")
        and "Strict-Transport-Security" not in response.headers
    ):
        response.headers["Strict-Transport-Security"] = _HSTS_VALUE

    return response


# ---------------------------------------------------------------------------
# Public registration function
# ---------------------------------------------------------------------------


def register_security_headers_middleware(app: Flask) -> None:
    """Register the security-headers ``after_request`` hook on the app.

    Registers exactly one Flask hook:

        * ``after_request`` -> ``_after_request_security_headers``

    Per AAP Section 0.5.2 Layer 0, this MUST be registered AFTER
    ``register_cors_middleware`` (so the security headers layer on
    top of any ``Access-Control-*`` headers attached to non-preflight
    responses) and BEFORE ``register_auth_middleware`` (so auth's
    401 short-circuit responses also receive the security headers).
    The canonical sequence is::

        correlation -> cors -> security_headers -> auth -> rbac -> error_handlers

    Idempotent: registering the same hook twice is harmless because
    Flask deduplicates by view-function identity, and the module-
    level helper ``_after_request_security_headers`` has stable
    identity across re-registrations. The factory in
    ``app/__init__.py`` calls this function exactly once per
    ``create_app`` invocation; the idempotency guarantee covers
    edge cases where a test fixture might re-register middleware on
    an existing app instance.

    Args:
        app: The Flask application instance produced by
            :func:`app.create_app`. Hooks are registered on the
            application-wide function list, so they fire for every
            blueprint and every request (including the health
            probes, the metrics scrape, and the CORS preflight).

    Returns:
        None. Side effects:

            * Mutates ``app.after_request_funcs`` to include the
              security-headers hook.
            * Emits a single
              ``security_headers_middleware_registered`` info log
              line via the stdlib logger so operators can confirm
              wiring at startup. The log line includes the names of
              the static headers, the path prefixes that receive
              ``Cache-Control``, and whether HSTS is enabled (a
              proxy for "is this a production app?").
    """
    app.after_request(_after_request_security_headers)
    # Resolve config eagerly here only for the registration log line;
    # per-request resolution happens in the hook above. Reading
    # ``app.config`` at registration time is safe because
    # ``app.config.from_object(config_class)`` ran earlier in
    # ``create_app`` (see ``app/__init__.py`` wiring order step 4).
    _logger.info(
        "security_headers_middleware_registered",
        extra={
            "static_headers": list(_STATIC_HEADERS.keys()),
            "cache_control_prefixes": list(_CACHE_CONTROL_PREFIXES),
            "hsts_enabled": bool(app.config.get("SESSION_COOKIE_SECURE")),
        },
    )
