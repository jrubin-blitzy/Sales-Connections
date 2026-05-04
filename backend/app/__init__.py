"""Flask application factory for the Sales-Connections backend.

This module exposes :func:`create_app`, the canonical entry point used
by:

* ``backend/wsgi.py`` to construct the production app for Gunicorn.
* Deployment health-check tooling that needs to instantiate the
  production app graph against environment-supplied secrets.
* Tests that exercise the full production wiring (the dedicated
  ``backend/tests/conftest.py::_build_test_app`` mirrors this exact
  ordering and is kept in lock-step with this factory).

Per AAP Section 0.5.2 Layer 0, the canonical wiring order is::

    1.  configure_structlog            (so subsequent logs are JSON)
    2.  Flask(__name__)                (construct the Flask app)
    3.  app.config.from_object(config) (load the config class)
    4.  config_class.init_app(app)     (Secrets Manager + fail-fast)
    5.  db.init_app(app)               (SQLAlchemy engine + session)
    6.  oauth.init_app(app)            (Authlib OAuth client table)
    7.  init_oauth_clients(app, oauth) (register Google client)
    8.  register_correlation_middleware (per-request correlation ID)
    9.  register_cors_middleware       (preflight short-circuit +
                                        Access-Control-* headers)
    10. register_security_headers_middleware
                                       (defensive HTTP response
                                        headers: nosniff,
                                        X-Frame-Options, Referrer-
                                        Policy, Permissions-Policy,
                                        Cache-Control on /api and
                                        /auth, HSTS in production;
                                        registered AFTER cors so
                                        every response -- preflight
                                        included -- carries the
                                        defensive headers)
    11. register_auth_middleware       (JWT verification +
                                        token_version freshness check)
    12. register_rbac_error_handlers   (ForbiddenError special-case;
                                        registered before
                                        register_error_handlers per
                                        the AAP-mandated middleware
                                        sequence correlation ->
                                        cors -> security_headers ->
                                        auth -> rbac ->
                                        error_handlers)
    13. register_error_handlers        (AppError -> JSON envelope;
                                        registered LAST so it sees
                                        exceptions raised by every
                                        upstream middleware)
    14. register_blueprints            (mount API surfaces AFTER all
                                        middleware so handlers see
                                        populated ``g.session`` and
                                        the registered error
                                        handlers convert raised
                                        AppErrors)
    15. init_metrics                   (Prometheus /metrics endpoint
                                        + per-request hooks)
    16. init_tracing                   (OpenTelemetry tracer +
                                        Flask/SQLAlchemy auto-
                                        instrumentation; gracefully
                                        no-ops when
                                        ``OTLP_EXPORTER_ENDPOINT``
                                        is empty)
    17. import app.models              (side-effect import that
                                        registers every declarative
                                        model class on
                                        ``Base.metadata`` so
                                        Alembic's autogenerate sees
                                        the full schema)

Per AAP Section 0.7.1 invariant 1 (stateless workers), the factory
performs ALL initialization that any worker needs at startup time
(no lazy first-request wiring); this guarantees that a worker can
be terminated and replaced without losing state.

Per AAP Section 0.7.5 (Observability rule), the factory wires
structured logging FIRST so every subsequent log line during
startup carries correlation IDs and the canonical context.
Tracing, metrics, and the ``/healthz`` / ``/readyz`` health probes
ride on this foundation.

Configuration selection:

The ``config_object`` parameter accepts either a dotted import string
(``"app.config.ProductionConfig"``) or a config class object (the
class itself, not an instance). Resolution order:

1. Explicit ``config_object`` argument (highest precedence).
2. ``FLASK_ENV`` environment variable
   (``development`` -> ``DevelopmentConfig``,
   ``testing`` -> ``TestingConfig``,
   anything else / unset -> ``ProductionConfig``).
3. Default to ``ProductionConfig`` (production-safe default; fail-fast
   on missing secrets).

This module deliberately has no module-level side effects beyond
importing :mod:`app.extensions` (which itself is side-effect-free
per its own contract). Importing ``app`` does NOT call
:func:`create_app` automatically; callers MUST invoke
:func:`create_app` explicitly.
"""

from __future__ import annotations

import importlib
import os
from typing import Any

from flask import Flask

# First-party imports. Ordered to mirror the runtime initialization
# order documented in the module docstring so contributors can read
# top-to-bottom without bouncing around.
from app.config import (
    BaseConfig,
    DevelopmentConfig,
    ProductionConfig,
    TestingConfig,
)
from app.extensions import db, init_oauth_clients, oauth
from app.middleware.auth import register_auth_middleware
from app.middleware.correlation import register_correlation_middleware
from app.middleware.cors import register_cors_middleware
from app.middleware.error_handlers import register_error_handlers
from app.middleware.rbac import register_rbac_error_handlers
from app.middleware.security_headers import register_security_headers_middleware
from app.observability.logging import configure_structlog
from app.observability.metrics import init_metrics
from app.observability.tracing import init_tracing

# ``importlib`` workaround per the convention established in
# ``app.api.__init__``: the ``app`` namespace contains a sibling
# ``app.observability.logging`` module, so a direct ``import logging``
# can misresolve under some mypy configurations. Importing via
# ``importlib`` plus an ``Any`` annotation defuses the namespace
# conflict at type-check time without changing runtime behavior.
# Placed AFTER the regular ``from X import Y`` statements per the
# project convention so ruff's ``E402`` (module-level imports must
# precede non-import statements) does not flag the first-party
# imports as misplaced.
logging: Any = importlib.import_module("logging")


# ---------------------------------------------------------------------------
# Module logger
# ---------------------------------------------------------------------------
# A stdlib logger so the factory can emit ``app_factory_started`` /
# ``app_factory_completed`` markers BEFORE structlog is configured
# (which itself happens INSIDE :func:`create_app`). The bare stdlib
# logger uses the project's default handler configuration; once
# :func:`configure_structlog` runs inside the factory, subsequent
# emits are routed through the structlog processor chain.
_stdlib_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module version
# ---------------------------------------------------------------------------
# Mirrors the ``version`` field in ``backend/pyproject.toml`` and is the
# authoritative source of truth for the application's semantic version
# at runtime. Read by :mod:`app.observability.tracing` to populate the
# ``service.version`` resource attribute on every emitted span (see
# ``app.observability.tracing._build_resource``). Bumping this value
# should be paired with a corresponding bump in ``pyproject.toml`` and
# a release commit; the two MUST stay in lock-step so a deployed
# container's reported version matches the artifact in the registry.
__version__: str = "0.1.0"


__all__ = ["__version__", "create_app"]


# ---------------------------------------------------------------------------
# Internal helper: resolve config class
# ---------------------------------------------------------------------------


def _resolve_config_class(config_object: str | type[BaseConfig] | None) -> type[BaseConfig]:
    """Resolve ``config_object`` into a concrete config class.

    Args:
        config_object: One of:
            * A class object (subclass of :class:`BaseConfig`) -- used
              directly.
            * A dotted import string
              (e.g., ``"app.config.ProductionConfig"``) -- imported
              via ``importlib.import_module`` and the attribute
              looked up.
            * ``None`` -- consult ``FLASK_ENV`` env var; fall back to
              :class:`ProductionConfig`.

    Returns:
        A class object suitable for ``app.config.from_object(...)``.

    Raises:
        ImportError: The dotted string could not be resolved.
        TypeError: The resolved object is not a subclass of
            :class:`BaseConfig`.
    """
    if config_object is None:
        env = os.environ.get("FLASK_ENV", "").strip().lower()
        if env == "development":
            return DevelopmentConfig
        if env == "testing":
            return TestingConfig
        # Default to production: fail-fast on missing secrets is
        # safer than silently running a dev config in a prod
        # container.
        return ProductionConfig

    if isinstance(config_object, str):
        # Dotted-string resolution: ``"app.config.ProductionConfig"``
        # -> import ``app.config`` and read ``ProductionConfig``.
        module_path, _, class_name = config_object.rpartition(".")
        if not module_path or not class_name:
            raise ImportError(
                f"Invalid config_object string {config_object!r}; "
                "expected dotted path like 'app.config.ProductionConfig'."
            )
        module = importlib.import_module(module_path)
        resolved = getattr(module, class_name, None)
        if resolved is None:
            raise ImportError(f"Module {module_path!r} has no attribute {class_name!r}.")
        config_object = resolved

    # At this point ``config_object`` is expected to be a class.
    if not isinstance(config_object, type) or not issubclass(config_object, BaseConfig):
        raise TypeError(
            f"config_object resolved to {config_object!r}; expected a subclass of BaseConfig."
        )
    return config_object


# ---------------------------------------------------------------------------
# Public function: create_app
# ---------------------------------------------------------------------------


def create_app(config_object: str | type[BaseConfig] | None = None) -> Flask:
    """Construct and return a fully-wired Flask application.

    Wiring order (mirrors the canonical sequence documented in the
    module docstring; tests in
    ``backend/tests/conftest.py::_build_test_app`` exercise the
    same wiring)::

        1.  Resolve config class (explicit arg / FLASK_ENV / default)
        2.  configure_structlog (log level + format from the config)
        3.  Flask(__name__)
        4.  app.config.from_object(config_class)
        5.  config_class.init_app(app)  -- Secrets Manager / fail-fast
        6.  db.init_app(app)
        7.  oauth.init_app(app)
        8.  init_oauth_clients(app, oauth)
        9.  register_correlation_middleware(app)
        10. register_cors_middleware(app)
        11. register_security_headers_middleware(app)
        12. register_auth_middleware(app)
        13. register_rbac_error_handlers(app)
        14. register_error_handlers(app)         -- LAST middleware
        15. register_blueprints(app)             -- mount API surfaces
        16. init_metrics(app)                    -- Prometheus /metrics
        17. init_tracing(app, db.engine)         -- OpenTelemetry tracer
        18. import app.models                    -- register on metadata
        19. _register_shell_context(app)         -- flask shell helpers

    Per AAP Section 0.5.2 Layer 0, the middleware-then-blueprints
    order is required so that:

    * The correlation ID is bound on contextvars BEFORE any handler
      logs.
    * The auth middleware populates ``g.session`` BEFORE any handler
      reads it.
    * The error handlers convert raised :class:`AppError` subclasses
      into the canonical JSON envelope BEFORE Flask's default 500
      page would otherwise leak.
    * The blueprints are registered LAST so they fire AFTER all
      middleware has been wired.

    Args:
        config_object: Optional config class or dotted-string
            override. ``None`` consults ``FLASK_ENV`` (default
            :class:`ProductionConfig`).

    Returns:
        A fully-wired :class:`flask.Flask` instance ready to be
        mounted under Gunicorn (via ``backend/wsgi.py``) or invoked
        directly via ``app.run()`` (development only -- production
        always goes through Gunicorn per AAP Section 0.5.2).
    """
    # Resolve the config class FIRST so we can read log_level /
    # log_format from it before structlog initializes. Any
    # ImportError or TypeError here propagates to the caller (a
    # misconfigured deploy fails fast at import time, before any
    # request is served).
    config_class = _resolve_config_class(config_object)

    # Configure structlog BEFORE Flask construction so that any
    # logger calls inside the Flask constructor (or any extension's
    # ``init_app``) are JSON-formatted and carry the project's
    # canonical context fields.
    log_level = getattr(config_class, "LOG_LEVEL", "INFO")
    log_format = getattr(config_class, "LOG_FORMAT", "json")
    configure_structlog(log_level=log_level, log_format=log_format)

    # Construct the Flask app. ``__name__`` is "app" (this package's
    # name) -- matches the Flask convention so static-file lookups
    # and template loading would resolve under ``app/static/`` and
    # ``app/templates/`` if those folders ever existed (they do not
    # for this API-only backend, but the convention is preserved).
    flask_app = Flask(__name__)

    # Load configuration. ``from_object`` reads class-level
    # attributes (UPPER_CASE only) into ``flask_app.config``.
    flask_app.config.from_object(config_class)

    # Run the config class's ``init_app`` hook. Production reads
    # secrets from AWS Secrets Manager and fails fast on missing /
    # placeholder values; development and testing are no-ops.
    config_class.init_app(flask_app)

    # ----- Bind extensions to the app --------------------------------
    # ``db.init_app(app)`` constructs the SQLAlchemy 2.x engine and
    # session factory using the DSN in ``app.config["DATABASE_URL"]``.
    # ``oauth.init_app(app)`` wires Authlib's OAuth registration table.
    # ``init_oauth_clients(app, oauth)`` registers the Google OAuth
    # client (or no-ops when ``GOOGLE_OAUTH_CLIENT_ID`` is empty,
    # which is the testing path).
    db.init_app(flask_app)
    oauth.init_app(flask_app)
    init_oauth_clients(flask_app, oauth)

    # ----- Register middleware in canonical order --------------------
    # Order is significant per AAP Section 0.5.2 Layer 0:
    #   1. correlation -- generate / extract X-Correlation-Id and
    #      bind on contextvars BEFORE any other middleware logs.
    #   2. cors -- short-circuit CORS preflight (OPTIONS) requests
    #      with 204 + Access-Control-Allow-* headers BEFORE the auth
    #      middleware runs. CORS preflight requests never carry the
    #      session cookie so they would otherwise be rejected with
    #      401 by the auth middleware (QA Issue 12). Also attaches
    #      Access-Control-Allow-Origin / -Credentials to non-preflight
    #      responses for allowlisted origins.
    #   3. security_headers -- attach defensive HTTP response headers
    #      (X-Content-Type-Options nosniff, X-Frame-Options DENY,
    #      Referrer-Policy, Permissions-Policy on every response;
    #      Cache-Control no-store on /api/* and /auth/*; HSTS in
    #      production). Registered AFTER cors so the after_request
    #      chain runs correlation -> cors -> security_headers, which
    #      means the OPTIONS preflight 204 short-circuit response and
    #      every CORS-augmented response carries the security headers
    #      too. Registered BEFORE auth so even auth's 401 short-
    #      circuit response carries the defensive headers, closing the
    #      QA Checkpoint 7 gap (Issue #1, MEDIUM) where production
    #      /api/* responses lacked these headers because the AWS ALB
    #      routes /api/* directly to Flask without nginx interposition
    #      per AAP Section 0.4.6.
    #   4. auth -- decode the session JWT, verify token_version, and
    #      populate ``g.session``. Public paths (``/healthz``,
    #      ``/readyz``, ``/metrics``, ``/auth/*``) are skipped.
    #   5. rbac_error_handlers -- special-case for ForbiddenError so
    #      RBAC denials carry the structured ``required_roles`` /
    #      ``actual_role`` log context. Registered BEFORE
    #      ``register_error_handlers`` so that the comprehensive
    #      handler suite registered there has the final word on the
    #      ``ForbiddenError`` -> 403 envelope (Flask de-duplicates
    #      handler registrations by exception-class identity, so the
    #      final registration wins; the canonical envelope shape is
    #      identical regardless of order, but matching the AAP-
    #      mandated sequence is important for documentation and
    #      future contributors).
    #   6. error_handlers -- convert raised AppError subclasses,
    #      pydantic ``ValidationError``, werkzeug ``HTTPException``,
    #      and any unhandled ``Exception`` into the canonical JSON
    #      envelope. Registered LAST in the middleware sequence so
    #      it sees exceptions raised by every upstream middleware
    #      (correlation, cors, security_headers, auth, rbac) and by
    #      every handler.
    register_correlation_middleware(flask_app)
    register_cors_middleware(flask_app)
    register_security_headers_middleware(flask_app)
    register_auth_middleware(flask_app)
    register_rbac_error_handlers(flask_app)
    register_error_handlers(flask_app)

    # ----- Register API blueprints -----------------------------------
    # Blueprints are imported and mounted via the
    # ``app.api.register_blueprints`` helper which centralizes the
    # URL-prefix wiring per AAP Section 0.4.3. Blueprint imports
    # happen INSIDE that helper to avoid circular-import risk.
    # Mounting blueprints AFTER all middleware is registered ensures
    # handlers see the populated ``g.session`` and that any AppError
    # they raise is converted to the canonical JSON envelope by the
    # handlers registered above.
    from app.api import register_blueprints  # noqa: PLC0415

    register_blueprints(flask_app)

    # ----- Initialize Prometheus metrics -----------------------------
    # ``init_metrics(app)`` registers per-request before/after hooks
    # that observe the ``http_requests_total`` counter and the
    # ``http_request_duration_seconds`` histogram, plus mounts the
    # ``/metrics`` endpoint that the AWS Distro for OpenTelemetry
    # collector scrapes. Idempotent: callable multiple times without
    # double-registration (per its own contract).
    init_metrics(flask_app)

    # ----- Initialize OpenTelemetry tracing --------------------------
    # ``init_tracing(app, engine)`` installs the global TracerProvider
    # with an OTLP HTTP exporter and wires Flask + SQLAlchemy auto-
    # instrumentation. Gracefully no-ops when ``OTLP_EXPORTER_ENDPOINT``
    # is empty, which is the local-dev and unit-test default. The
    # SQLAlchemy engine is passed only when tracing is enabled so that
    # tests which never exercise the database do not pay the
    # instrumentation cost. Per AAP Section 0.7.5 (Observability rule),
    # this completes the structured-logging + metrics + tracing
    # observability triad that the deliverable requires.
    otlp_endpoint = flask_app.config.get("OTLP_EXPORTER_ENDPOINT")
    init_tracing(flask_app, db.engine if otlp_endpoint else None)

    # ----- Ensure model classes register on db.metadata --------------
    # Side-effect import: importing ``app.models`` executes the
    # declarative model class definitions, which in turn register
    # their ``__tablename__`` and constraints on
    # ``app.extensions.Base.metadata``. Without this import, Alembic's
    # autogenerate would silently miss any model whose module had not
    # otherwise been imported, producing a migration that DROPS those
    # tables. Per AAP Section 0.5.2 Layer 0 this is the canonical
    # final wiring step before the factory returns. The ``PLC0415``
    # suppression is required because ruff flags non-top-level imports;
    # the deferred import is intentional here so the side effect runs
    # only after the full extension/middleware/blueprint wiring is
    # complete.
    import app.models  # noqa: PLC0415

    # ----- Register `flask shell` context provider -------------------
    # Exposes ``db`` and the ``app.models`` package to the
    # ``flask shell`` command for ad-hoc operator queries. This is a
    # convenience for production debugging and local development; it
    # has zero impact on request handling. Registered last so the
    # shell context provider sees the fully-wired application.
    _register_shell_context(flask_app)

    _stdlib_logger.info(
        "app_factory_completed",
        extra={
            "config_class": config_class.__name__,
            "env_name": getattr(config_class, "ENV_NAME", "unknown"),
            "debug": flask_app.debug,
            "testing": flask_app.testing,
            "version": __version__,
        },
    )

    return flask_app


# ---------------------------------------------------------------------------
# Internal helper: register flask shell context
# ---------------------------------------------------------------------------


def _register_shell_context(flask_app: Flask) -> None:
    """Expose ``db`` and ``app.models`` to ``flask shell`` for ad-hoc queries.

    The ``flask shell`` command opens a Python REPL with the Flask
    application context pushed; any objects registered via
    :meth:`flask.Flask.shell_context_processor` are pre-bound in the
    REPL namespace so operators can write::

        $ flask shell
        >>> models.User.__tablename__
        'users'
        >>> with db.session() as session:
        ...     count = session.execute(text("SELECT count(*) FROM users")).scalar()

    This is purely a developer-experience convenience and does not
    affect request handling or production observability.

    Args:
        flask_app: The Flask application instance produced by
            :func:`create_app`. Mutates ``flask_app.shell_context_processors``
            in place.

    Returns:
        None. Side effect is one new entry in
        ``flask_app.shell_context_processors``.
    """

    @flask_app.shell_context_processor
    def _shell_context() -> dict[str, object]:
        # Lazy import inside the closure so importing this module does
        # not eagerly load every model class. The closure runs only
        # when the operator types ``flask shell``, at which point the
        # full app graph is already constructed and the import is a
        # no-op (the modules are cached in ``sys.modules``).
        from app import models  # noqa: PLC0415

        return {"db": db, "models": models}
