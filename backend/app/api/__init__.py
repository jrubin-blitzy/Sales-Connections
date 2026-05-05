"""API blueprint registration for the Sales-Connections backend.

This package contains the seven Flask blueprints that compose the
HTTP surface of the application. Six are listed in the AAP file
schema (admin, auth, connections, health, notes, tags); the seventh,
``me_bp`` (re-exported from :mod:`app.api.auth`), exists because the
authentication surface spans two URL prefixes (``/auth/*`` for the
login surface and ``/api/me`` for the SPA's session-introspection
probe) and a single Flask blueprint cannot be mounted at two
different prefixes simultaneously.

URL-prefix coordination
-----------------------

The wiring below is the single authoritative source for where each
blueprint mounts. Per AAP Section 0.4.3 endpoint catalog::

    +------------------+--------------------------+--------------+
    | Blueprint object | URL prefix               | Module       |
    +------------------+--------------------------+--------------+
    | health_bp        | (no prefix; absolute)    | health       |
    | auth_bp          | /auth                    | auth         |
    | me_bp            | /api                     | auth         |
    | notes_bp         | /api/notes               | notes        |
    | tags_bp          | /api/tags                | tags         |
    | connections_bp   | /api/connections         | connections  |
    | admin_bp         | /api/admin               | admin        |
    +------------------+--------------------------+--------------+

* ``health_bp`` registers WITHOUT a ``url_prefix`` so the routes
  ``/healthz`` and ``/readyz`` mount at the application root. AWS
  ALB target-group health checks and Kubernetes-style orchestrators
  expect probe endpoints at the application root, never under
  ``/api``.
* ``auth_bp`` mounts under ``/auth`` so its relative routes
  ``/login``, ``/logout``, ``/google/start`` and ``/google/callback``
  resolve to the documented absolute paths.
* ``me_bp`` mounts under ``/api`` so the SPA's ``AuthProvider`` can
  call ``GET /api/me`` consistently with the rest of the data API
  surface (the ``@/api/client`` wrapper expects all data routes to
  live under ``/api``).
* The remaining four blueprints (``notes_bp``, ``tags_bp``,
  ``connections_bp``, ``admin_bp``) declare their routes with
  prefix-relative paths and mount under their respective ``/api/*``
  prefixes.

Coordination with ``app.create_app``
------------------------------------

The application factory in :mod:`app.__init__` calls
:func:`register_blueprints` AFTER middleware registration so every
request reaches the middleware chain (correlation -> auth -> RBAC
decorators -> error_handlers) before being dispatched to a blueprint
route. Per AAP Section 0.5.2 Layer 0 the canonical sequence is::

    correlation -> auth -> rbac decorators -> error_handlers
                                              -> blueprints
                                              -> metrics endpoint

Public API
----------

This module exports:

* :func:`register_blueprints` -- the SOLE entry point invoked by the
  application factory at boot.
* The six canonical blueprint objects (``admin_bp``, ``auth_bp``,
  ``connections_bp``, ``health_bp``, ``notes_bp``, ``tags_bp``) for
  test scaffolding that wishes to mount an isolated subset of
  blueprints. ``me_bp`` is intentionally NOT re-exported because
  it is an implementation detail of the auth surface; tests that
  need it import it from :mod:`app.api.auth` directly.

Side effects
------------

Importing this module triggers ZERO database calls, ZERO HTTP calls,
and ZERO network activity. All blueprint imports are pure module
loads; route registration occurs only when
:func:`register_blueprints` is invoked.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Standard library imports
# ---------------------------------------------------------------------------
# ``logging`` is resolved via ``importlib`` (NOT ``import logging``)
# because the ``app`` namespace tree contains a sibling module
# ``app.observability.logging`` (a file literally named
# ``logging.py``). Toolchain configurations that treat ``app/`` as a
# PEP 420 namespace package without ``explicit_package_bases``
# (notably the project's current mypy configuration) misresolve a
# direct ``import logging`` from inside a sibling ``app.*`` module
# as a self-import against ``app.observability.logging`` rather than
# the stdlib. This matches the convention established in
# ``app/middleware/correlation.py``, ``app/middleware/auth.py``, and
# ``app/__init__.py``: at runtime the behavior is identical to
# ``import logging``, and at type-check time the ``Any`` annotation
# defuses the namespace conflict without affecting the runtime
# behavior or surface area.
import importlib
from typing import TYPE_CHECKING, Any

# ---------------------------------------------------------------------------
# First-party blueprint imports (alphabetical)
# ---------------------------------------------------------------------------
# Each module exports exactly one canonical blueprint object that
# ``register_blueprints`` mounts. The ``app.api.auth`` module
# additionally exports ``me_bp`` because its session-introspection
# route lives at ``/api/me`` (i.e., a different URL prefix than the
# rest of the auth surface) and Flask blueprints support exactly one
# ``url_prefix`` per registration. Both blueprints are imported here
# so the registration function can wire them in a single place.
#
# Importing the blueprint modules at module level is safe: none of
# these modules imports back from ``app.api.__init__`` (verified by
# inspection of every dependency module), so there is no risk of a
# circular import. The lazy import pattern used by some Flask
# projects exists to defer expensive third-party library loads,
# which is not a concern here: the blueprint modules' module-level
# work is limited to constructing one ``Blueprint(...)`` object and
# registering route decorators against it. Module-level imports also
# let test scaffolding access the blueprint objects directly (for
# isolated subset registration) without invoking the full
# ``register_blueprints`` side effect.
from app.api.admin import admin_bp
from app.api.auth import auth_bp, me_bp
from app.api.connections import connections_bp
from app.api.health import health_bp
from app.api.notes import notes_bp
from app.api.tags import tags_bp

# ---------------------------------------------------------------------------
# Type-checking-only imports
# ---------------------------------------------------------------------------
# Flask is imported under ``TYPE_CHECKING`` ONLY so this module
# retains zero runtime coupling to Flask at import time. The
# ``register_blueprints`` annotation ``app: Flask`` is preserved as a
# string by ``from __future__ import annotations`` above, so mypy
# resolves it correctly while the runtime never imports Flask through
# this gateway. (The ``app.__init__`` factory imports Flask anyway,
# so this is a pure dependency-graph hygiene measure rather than a
# functional concern.)
if TYPE_CHECKING:
    from flask import Flask


# Deferred stdlib ``logging`` load via ``importlib`` (see NOTE above
# in the standard library imports section). The ``Any`` annotation is
# intentional: it makes the wrapper opaque to mypy so the type-checker
# does not attempt the (incorrect, namespace-package-quirk) resolution
# against the sibling ``app.observability.logging``. The runtime
# behavior is identical to ``import logging``. Placed AFTER the
# regular ``from X import Y`` statements per the project convention
# so ruff's ``E402`` (module-level imports must precede non-import
# statements) does not flag the first-party imports as misplaced.
logging: Any = importlib.import_module("logging")


# ---------------------------------------------------------------------------
# Module logger
# ---------------------------------------------------------------------------
# Stdlib logger used to emit a single ``api_blueprints_registered``
# log line at registration time so operators can confirm wiring at
# startup. Runtime structured logs flow through structlog (configured
# in :mod:`app.observability.logging`) which renders the stdlib
# ``LogRecord.extra`` payload as JSON fields alongside the
# correlation context.
_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# URL prefixes (single source of truth)
# ---------------------------------------------------------------------------
# Centralized so contributors never duplicate prefix strings across
# modules. Per AAP Section 0.4.3 endpoint catalog, the ``/api``
# prefix is mandatory for all REST data surfaces; ``/auth`` and the
# health probes live at the application root.
_PREFIX_AUTH: str = "/auth"
_PREFIX_API: str = "/api"
_PREFIX_NOTES: str = "/api/notes"
_PREFIX_TAGS: str = "/api/tags"
_PREFIX_CONNECTIONS: str = "/api/connections"
_PREFIX_ADMIN: str = "/api/admin"


# ---------------------------------------------------------------------------
# Public module surface
# ---------------------------------------------------------------------------
# Re-exporting the six canonical blueprint objects (per the AAP
# schema for this file) is a convenience for tests that wish to
# register an isolated subset of blueprints (for example a test app
# that mounts only ``health_bp`` for a liveness-only smoke test).
# The primary export remains :func:`register_blueprints`; the
# blueprint objects are NOT used directly by the application
# factory, which always calls the registration function.
#
# ``me_bp`` is intentionally NOT in ``__all__``: it is an
# implementation detail of the auth surface (see module docstring).
# Tests needing it should import from :mod:`app.api.auth` directly.
__all__ = [
    "admin_bp",
    "auth_bp",
    "connections_bp",
    "health_bp",
    "notes_bp",
    "register_blueprints",
    "tags_bp",
]


# ---------------------------------------------------------------------------
# Public registration function
# ---------------------------------------------------------------------------


def register_blueprints(app: Flask) -> None:
    """Register every API blueprint on the supplied Flask app.

    Called by :func:`app.create_app` AFTER the middleware
    registration sequence is complete. Per AAP Section 0.5.2 Layer 0,
    the strict middleware order is::

        correlation -> auth -> rbac decorators -> error_handlers
                                                  -> blueprints

    NOT idempotent within a single Flask app instance: re-registering
    a blueprint with the same ``name`` raises an error from Flask
    (Flask 3.x raises ``ValueError`` with the message "A name
    collision occurred between blueprints"). Per AAP Section 0.7.1
    invariant 1 (Stateless backend workers), the Flask app is
    constructed once per worker process; this function is therefore
    called exactly once per app instance. Tests that need a clean
    app state should construct a fresh ``create_app(...)`` instance
    per test rather than reusing and re-registering on a shared
    instance.

    Per AAP Section 0.4.3, the auth surface lives at ``/auth/*`` and
    the data API surface lives at ``/api/*``. The ``GET /api/me``
    route is registered on a SECOND blueprint (``me_bp``) inside the
    same module as ``auth_bp`` (because it is the session-hydration
    probe and the handler is conceptually paired with the rest of
    the authentication code) but mounted at ``/api/me`` rather than
    ``/auth/me`` so the SPA's ``@/api/client`` wrapper can route it
    consistently with the rest of the data API.

    The health blueprint mounts at the application root (not
    ``/api``) because the AWS ALB target-group health-check does not
    prepend ``/api``. See :mod:`app.middleware.auth._PUBLIC_PATHS`
    for the list of paths the auth middleware exempts from
    authentication; that list MUST include every public path
    registered here (``/healthz``, ``/readyz``, ``/auth/login``,
    ``/auth/google/start``, ``/auth/google/callback``).

    Args:
        app: The Flask application instance produced by
            :func:`app.create_app`. Blueprints are registered on the
            application-wide registry, so they apply to every
            request matching the prefixed URL.

    Returns:
        None. Side effects: mutates ``app.blueprints`` and
        ``app.url_map``; emits a single
        ``api_blueprints_registered`` info log line so operators can
        confirm wiring at startup.
    """
    # ----- Health probes (no prefix; reachable at /healthz, /readyz)
    # The health blueprint declares its routes at the absolute paths
    # ``/healthz`` and ``/readyz`` so mounting WITHOUT a
    # ``url_prefix`` yields the unprefixed final URLs. ALB and
    # Kubernetes-style orchestrator health checks expect this.
    app.register_blueprint(health_bp)

    # ----- Authentication surface (mounted at /auth/*) ----------------
    # The auth blueprint declares relative routes for ``/login``,
    # ``/logout``, ``/google/start`` and ``/google/callback`` so
    # mounting under ``/auth`` produces ``/auth/login`` etc. The
    # ``GET /api/me`` route is registered on a separate ``me_bp``
    # blueprint (defined alongside ``auth_bp`` in the same module)
    # and mounted under ``/api`` so the SPA can reach it via the
    # standard data-API base URL.
    app.register_blueprint(auth_bp, url_prefix=_PREFIX_AUTH)
    app.register_blueprint(me_bp, url_prefix=_PREFIX_API)

    # ----- AI Note Generation (mounted at /api/notes/*) ---------------
    # The notes blueprint declares the route at the relative path
    # ``/generate`` so mounting under ``/api/notes`` produces
    # ``POST /api/notes/generate`` per AAP Section 0.4.3.
    app.register_blueprint(notes_bp, url_prefix=_PREFIX_NOTES)

    # ----- Tag CRUD (mounted at /api/tags) ----------------------------
    # The tags blueprint declares routes at the relative path ``""``
    # (empty) for both GET and POST so the final URLs are
    # ``GET /api/tags`` and ``POST /api/tags`` per AAP Section 0.4.3.
    app.register_blueprint(tags_bp, url_prefix=_PREFIX_TAGS)

    # ----- Connection records (mounted at /api/connections) ----------
    # The connections blueprint declares routes at the relative
    # paths ``""`` (POST/GET), ``/<uuid:record_id>`` (GET/PATCH/
    # DELETE), ``/<uuid:record_id>/history`` (GET),
    # ``/<uuid:record_id>/status`` (PATCH), and ``/duplicate-check``
    # (GET) per AAP Section 0.4.3 endpoint catalog. Mounting under
    # ``/api/connections`` produces the F-001/F-004/F-005/F-007/
    # F-010/F-011 endpoint surface.
    app.register_blueprint(connections_bp, url_prefix=_PREFIX_CONNECTIONS)

    # ----- Admin Panel (mounted at /api/admin) ------------------------
    # The admin blueprint declares routes at the relative paths
    # ``/users``, ``/users/<uuid:user_id>``, ``/records``,
    # ``/records/<uuid:record_id>`` and ``/analytics`` so mounting
    # under ``/api/admin`` produces the F-014 endpoint surface per
    # AAP Section 0.5.2 Layer 6. All five routes are gated by
    # ``@requires_role(UserRole.ADMIN)``; non-Admin callers receive
    # HTTP 403 envelopes from the RBAC error-handler chain.
    app.register_blueprint(admin_bp, url_prefix=_PREFIX_ADMIN)

    _logger.info(
        "api_blueprints_registered",
        extra={
            "blueprint_count": 7,
            "blueprints": [
                health_bp.name,
                auth_bp.name,
                me_bp.name,
                notes_bp.name,
                tags_bp.name,
                connections_bp.name,
                admin_bp.name,
            ],
            "prefixes": {
                auth_bp.name: _PREFIX_AUTH,
                me_bp.name: _PREFIX_API,
                notes_bp.name: _PREFIX_NOTES,
                tags_bp.name: _PREFIX_TAGS,
                connections_bp.name: _PREFIX_CONNECTIONS,
                admin_bp.name: _PREFIX_ADMIN,
            },
        },
    )
