"""API blueprint registration helpers (AAP Section 0.4.3 / 0.5.2 Layer 0).

This module owns the URL-prefix wiring for every Flask blueprint
declared under :mod:`app.api`. The application factory
:func:`app.create_app` calls :func:`register_blueprints` exactly once
during construction; tests construct minimal Flask apps and import the
blueprints directly when needed.

Wired blueprints (per AAP Sec 0.4.3 endpoint catalog):

* :mod:`app.api.health`  -> mounted at the application root so
  ``/healthz`` and ``/readyz`` are reachable without an ``/api`` prefix
  (these are AWS ALB target-group health checks; the ALB does not
  prepend ``/api``).
* :mod:`app.api.auth`    -> mounted under ``/auth`` for the OAuth and
  email/password login surface, AND a single ``GET /api/me`` route
  registered separately on the same blueprint via the ``/me`` rule
  with the ``/api`` prefix carrier (see ``register_me_route`` below).
* :mod:`app.api.notes`   -> mounted under ``/api/notes`` so the route
  declared at the relative path ``/generate`` is reachable at
  ``POST /api/notes/generate``.
* :mod:`app.api.tags`    -> mounted under ``/api/tags`` so the routes
  at ``/`` (POST + GET) are reachable at
  ``GET /api/tags`` and ``POST /api/tags``.
* :mod:`app.api.connections` -> mounted under ``/api/connections``;
  the ``POST /api/connections`` route delivers F-001 (Connection
  Idea Form) per AAP Section 0.5.2 Layer 3. Additional routes
  (``GET``, ``PATCH``, ``DELETE``, ``GET /:id/history``,
  ``GET /duplicate-check``) ship in Layers 4-6 by appending to the
  same blueprint.

Future blueprints (Checkpoint 4-5 deliverables):

* :mod:`app.api.admin`       -> mounted under ``/api/admin``.

Per AAP Section 0.5.2 (Layer 0), :func:`register_blueprints` is the
LAST step in :func:`app.create_app` after middleware registration so
handlers see the populated ``g.session`` and the registered error
handlers convert any raised :class:`app.middleware.error_handlers.AppError`
subclass to a JSON envelope.

Importing this module triggers ZERO database calls, ZERO HTTP calls,
and ZERO network activity. All blueprint imports are pure module
loads; route registration occurs only when
:func:`register_blueprints` is invoked.
"""

from __future__ import annotations

# Standard library imports.
#
# ``logging`` is loaded via ``importlib`` (NOT ``import logging``)
# because the ``app`` namespace tree contains a sibling module
# ``app.observability.logging`` (a file literally named ``logging.py``).
# Toolchain configurations that treat ``app/`` as a PEP 420 namespace
# package without ``explicit_package_bases`` (notably the project's
# current mypy configuration) misresolve a direct ``import logging``
# from inside a sibling ``app.*`` module as a self-import against
# ``app.observability.logging`` rather than the stdlib. This matches
# the convention established in ``app/middleware/correlation.py`` and
# ``app/middleware/auth.py``: at runtime the behavior is identical to
# ``import logging``, and at type-check time the ``Any`` annotation
# defuses the namespace conflict without affecting the runtime
# behavior or surface area.
import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from flask import Flask


# Deferred stdlib ``logging`` load via ``importlib`` (see NOTE above).
# The ``Any`` annotation is intentional: it makes the wrapper opaque
# to mypy so the type-checker does not attempt the (incorrect,
# namespace-package-quirk) resolution against the sibling
# ``app.observability.logging``. The runtime behavior is identical to
# ``import logging``.
logging: Any = importlib.import_module("logging")


# ---------------------------------------------------------------------------
# Module logger
# ---------------------------------------------------------------------------
# Stdlib logger used to emit a single ``api_blueprints_registered`` log
# line so operators can confirm wiring at startup. Runtime structured
# logs flow through structlog (configured in
# :mod:`app.observability.logging`).
_stdlib_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# URL prefixes (single source of truth)
# ---------------------------------------------------------------------------
# Centralized so contributors never duplicate prefix strings across
# modules. Per AAP Section 0.4.3 endpoint catalog, the ``/api`` prefix
# is mandatory for all REST surfaces; ``/auth`` and the health probes
# live at the application root.
_PREFIX_AUTH: str = "/auth"
_PREFIX_API: str = "/api"
_PREFIX_NOTES: str = "/api/notes"
_PREFIX_TAGS: str = "/api/tags"
_PREFIX_CONNECTIONS: str = "/api/connections"


__all__ = [
    "register_blueprints",
]


# ---------------------------------------------------------------------------
# Public registration function
# ---------------------------------------------------------------------------


def register_blueprints(app: Flask) -> None:
    """Register every API blueprint on the supplied Flask app.

    Called by :func:`app.create_app` AFTER the middleware registration
    sequence is complete. Per AAP Section 0.5.2 Layer 0, the strict
    middleware order is::

        correlation -> auth -> rbac decorators -> error_handlers
                                                  -> blueprints

    Idempotent: re-registering a blueprint with the same ``name`` raises
    :class:`AssertionError` from Flask. Tests that construct multiple
    apps via :func:`app.create_app` should construct a fresh
    :class:`flask.Flask` instance per test rather than registering on a
    shared instance.

    Per AAP Section 0.4.3, the auth surface lives at ``/auth/*`` and
    the data API surface lives at ``/api/*``. The ``GET /api/me``
    endpoint is registered on the auth blueprint (because it is the
    session-hydration probe) but mounted at ``/api/me`` rather than
    ``/auth/me`` so the SPA's @/api/client wrapper can route it
    consistently with the rest of the data API.

    The health blueprint mounts at the application root (not ``/api``)
    because the AWS ALB target-group health-check does not prepend
    ``/api``. See :mod:`app.middleware.auth._PUBLIC_PATHS` for the
    list of paths the auth middleware exempts from authentication.

    Args:
        app: The Flask application instance produced by
            :func:`app.create_app`. Blueprints are registered on the
            application-wide registry, so they fire for every request
            matching the prefixed URL.

    Returns:
        None. Side effects: mutates ``app.blueprints`` and
        ``app.url_map``; emits a single ``api_blueprints_registered``
        info log line via the stdlib logger so operators can confirm
        wiring at startup.
    """
    # Lazy imports keep the top of this module free of circular import
    # risk. The blueprint modules import their service-layer
    # dependencies (services.auth, services.duplicate_detection,
    # services.ai_orchestration, etc.) at module-load time; those
    # services in turn import models, schemas, and middleware. Loading
    # the blueprints lazily here ensures the import graph is fully
    # constructed before Flask's blueprint-registration assertions run.
    from app.api.auth import auth_bp, me_bp  # noqa: PLC0415
    from app.api.connections import connections_bp  # noqa: PLC0415
    from app.api.health import health_bp  # noqa: PLC0415
    from app.api.notes import notes_bp  # noqa: PLC0415
    from app.api.tags import tags_bp  # noqa: PLC0415

    # ----- Health probes (no prefix; reachable at /healthz, /readyz)
    # The health blueprint declares its routes at the relative paths
    # ``/healthz`` and ``/readyz`` so mounting WITHOUT a ``url_prefix``
    # yields the unprefixed final URLs. ALB health checks expect this.
    app.register_blueprint(health_bp)

    # ----- Authentication surface (mounted at /auth/*) ----------------
    # The auth blueprint declares routes for ``/login``, ``/logout``,
    # ``/google/start``, ``/google/callback`` (relative paths).
    # Mounting under ``/auth`` produces ``/auth/login`` etc. The
    # ``GET /api/me`` route is registered on a separate ``me_bp``
    # blueprint mounted under ``/api`` so the SPA can route it via
    # the @/api/client wrapper consistently with the rest of the data
    # API.
    app.register_blueprint(auth_bp, url_prefix=_PREFIX_AUTH)
    app.register_blueprint(me_bp, url_prefix=_PREFIX_API)

    # ----- AI Note Generation (mounted at /api/notes/*) ---------------
    # The notes blueprint declares the route at the relative path
    # ``/generate`` so mounting under ``/api/notes`` produces
    # ``POST /api/notes/generate``.
    app.register_blueprint(notes_bp, url_prefix=_PREFIX_NOTES)

    # ----- Tag CRUD (mounted at /api/tags) ----------------------------
    # The tags blueprint declares routes at ``/`` for both GET and
    # POST so the final URLs are ``GET /api/tags`` and
    # ``POST /api/tags``. (Flask normalizes trailing slashes by
    # default; the SPA hits ``/api/tags`` without a trailing slash.)
    app.register_blueprint(tags_bp, url_prefix=_PREFIX_TAGS)

    # ----- Connection records (mounted at /api/connections) ----------
    # The connections blueprint declares ``POST /`` (relative path)
    # so the final URL is ``POST /api/connections``. Per AAP Section
    # 0.5.2 Layer 3, this is the F-001 (Connection Idea Form)
    # surface; additional routes for F-004/F-005/F-007/F-010/F-011
    # are appended to the same blueprint as those layers ship.
    app.register_blueprint(connections_bp, url_prefix=_PREFIX_CONNECTIONS)

    _stdlib_logger.info(
        "api_blueprints_registered",
        extra={
            "blueprints": [
                "health",
                "auth",
                "me",
                "notes",
                "tags",
                "connections",
            ],
            "prefixes": {
                "auth": _PREFIX_AUTH,
                "me": _PREFIX_API,
                "notes": _PREFIX_NOTES,
                "tags": _PREFIX_TAGS,
                "connections": _PREFIX_CONNECTIONS,
            },
        },
    )
