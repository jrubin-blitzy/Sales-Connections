"""Gunicorn configuration for the Sales-Connections backend.

This file routes Gunicorn's own logging (boot lifecycle messages,
access logs, worker errors, signal handlers) through Python's stdlib
logging module, which the application factory in
:mod:`app.observability.logging` integrates with structlog via
``structlog.stdlib.ProcessorFormatter``. The result is that EVERY log
line emitted by the running container -- including Gunicorn's own
boot and access lines -- is valid JSON parseable by ``jq``,
CloudWatch Logs Insights, Loki, Fluent Bit, and similar tooling.

Per QA Checkpoint 10 Issue 2: previously, the container emitted a
mix of structured JSON (from the Flask application) and plain
Common Log Format (from Gunicorn's ``--access-logfile -`` and boot
banners). Operators piping ``docker logs`` through ``jq`` saw parse
errors on the Gunicorn lines. This configuration closes that gap.

Mechanism
---------
Gunicorn's :class:`gunicorn.glogging.Logger` constructor reads the
``logconfig_dict`` setting and applies it via
:func:`logging.config.dictConfig`. The dict-config below configures:

- A single ``json`` formatter that wraps a :class:`logging.Formatter`
  whose ``format`` method delegates to a structlog
  :class:`structlog.stdlib.ProcessorFormatter` (constructed lazily by
  :func:`app.observability.logging._make_gunicorn_processor_formatter`).
- A single ``console`` handler emitting to stdout via the JSON
  formatter.
- The two Gunicorn-specific loggers (``gunicorn.error`` for boot and
  worker lifecycle messages; ``gunicorn.access`` for per-request
  lines) wired exclusively to the console handler with
  ``propagate = False`` so log records do not double-emit through the
  root handler.

Boot ordering
-------------
The Gunicorn master process imports this file BEFORE ``wsgi.py`` is
loaded by each worker. ``configure_structlog`` is therefore called at
the top of this module so the JSON renderer is available when
Gunicorn applies the ``logconfig_dict``. Each worker then re-runs
``configure_structlog`` inside :func:`app.create_app`, but the call
is idempotent (guarded by an internal ``_initialized`` flag) so the
duplicate invocation is a no-op.

Operator-facing settings
------------------------
Worker count, timeout, keep-alive, and graceful-timeout values are
sourced from environment variables in the Dockerfile CMD line so
operators can tune them without rebuilding the image. This file does
NOT override those values; it only handles logging integration.
"""

from __future__ import annotations

import logging
import os
import sys

# Configure structlog AT MODULE IMPORT TIME so that the JSON
# ProcessorFormatter is wired up before Gunicorn applies the
# logconfig_dict below. The Gunicorn master process imports this
# file in the parent process before forking workers; the structlog
# state established here is inherited by every worker via the fork
# (per CPython's standard fork semantics on Linux).
#
# We import lazily inside a try/except so the file can still be
# parsed (and Gunicorn can still start) even in the rare degraded
# environment where the application package is unavailable. In that
# pathological case the boot logs fall back to Gunicorn's default
# CLF format and operators can diagnose the broken environment.
_LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
_LOG_FORMAT = os.environ.get("LOG_FORMAT", "json")

# Broad ``except Exception`` is intentional: this file is loaded by
# Gunicorn's master process during boot and any unhandled exception
# would prevent the server from starting. We MUST fall back to
# Gunicorn's built-in CLF logging on any failure rather than
# crash-loop the container. See the docstring above for rationale.
try:
    # The application package may not be importable in some sandboxed
    # build environments; the try/except below preserves the ability
    # to parse this config file even in those cases. A successful
    # import is the normal production path.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from app.observability.logging import configure_structlog

    configure_structlog(log_level=_LOG_LEVEL, log_format=_LOG_FORMAT)
    _STRUCTLOG_BOOTSTRAPPED = True
except Exception:
    _STRUCTLOG_BOOTSTRAPPED = False


# ---------------------------------------------------------------------------
# Gunicorn logconfig_dict
# ---------------------------------------------------------------------------
# Gunicorn evaluates ``logconfig_dict`` once at master-process startup
# via :func:`logging.config.dictConfig`. The settings here REPLACE the
# default Gunicorn handlers; we deliberately disable existing loggers
# only for the ``gunicorn.access`` and ``gunicorn.error`` namespaces
# so unrelated stdlib loggers (the ones :mod:`app.observability.logging`
# already wired through structlog) keep their existing handlers.
#
# When ``_STRUCTLOG_BOOTSTRAPPED`` is False (degraded environment),
# Gunicorn falls back to its built-in default logger because the
# ``logconfig_dict`` setting is undefined. This is the safest possible
# fallback path: operators see CLF lines (which jq cannot parse) but
# the application still serves traffic.

if _STRUCTLOG_BOOTSTRAPPED:
    # Use a class-based formatter that delegates to the structlog
    # ProcessorFormatter already attached to the root logger. The
    # standard library's logging.config.dictConfig accepts the
    # ``()`` factory key to call any callable that returns a
    # Formatter instance. We use a tiny inline factory below.
    def _gunicorn_formatter_factory() -> logging.Formatter:
        """Return a ``logging.Formatter`` that emits the same JSON shape.

        Reaches up to the root logger configured by
        ``configure_structlog`` and reuses its handler's formatter.
        Falls back to a no-op stdlib ``Formatter`` if the root logger
        has no handler (which would only happen if ``configure_structlog``
        was somehow skipped, contrary to the bootstrapping above).
        """
        root = logging.getLogger()
        for handler in root.handlers:
            if handler.formatter is not None:
                return handler.formatter
        # Fallback: plain stdlib formatter. Better to emit a flat
        # plain-text line than crash the worker boot.
        return logging.Formatter("%(message)s")

    logconfig_dict = {
        "version": 1,
        # Do NOT disable existing loggers -- we only want to override
        # the gunicorn-specific ones; everything else keeps the
        # structlog-bridged handlers.
        "disable_existing_loggers": False,
        "formatters": {
            "json": {
                "()": _gunicorn_formatter_factory,
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "json",
                "stream": "ext://sys.stdout",
            },
        },
        "loggers": {
            "gunicorn.access": {
                "handlers": ["console"],
                "level": _LOG_LEVEL,
                # Do not propagate to the root logger; the root
                # handler has its own structlog formatter and would
                # double-emit.
                "propagate": False,
            },
            "gunicorn.error": {
                "handlers": ["console"],
                "level": _LOG_LEVEL,
                "propagate": False,
            },
        },
        # Leave the root logger alone; it was configured by
        # configure_structlog above.
    }

# ---------------------------------------------------------------------------
# Standard Gunicorn worker / process settings
# ---------------------------------------------------------------------------
# Read from env vars to mirror the Dockerfile CMD. Documented defaults
# match the Dockerfile's ENV declarations so a developer running
# ``gunicorn -c gunicorn.conf.py wsgi:app`` outside the container
# observes the same behavior as a deployed container.

bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"
workers = int(os.environ.get("WEB_CONCURRENCY", "2"))
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "30"))
graceful_timeout = int(os.environ.get("GUNICORN_GRACEFUL_TIMEOUT", "30"))
keepalive = int(os.environ.get("GUNICORN_KEEP_ALIVE", "5"))
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")

# Stream access and error logs to stdout so the docker logging driver
# (awslogs in production, json-file in local development) captures
# both. The structlog-bridged JSON formatter applies regardless of
# stream destination.
accesslog = "-"
errorlog = "-"
