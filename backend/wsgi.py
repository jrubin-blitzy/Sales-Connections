"""Gunicorn WSGI entrypoint for the Sales-Connections backend.

Per AAP Section 0.5.2 Layer 0, the production deployment binds
Gunicorn against this module's ``app`` symbol::

    gunicorn --workers $WEB_CONCURRENCY --bind 0.0.0.0:8000 \
             --access-logfile - wsgi:app

The module deliberately exposes only the ``app`` symbol -- it does
NOT expose the factory function or the config class. Callers that
want to construct a non-default app graph (e.g., for tests) should
import :func:`app.create_app` directly.

Per AAP Section 0.7.1 invariant 1 (stateless backend workers), this
module is the SOLE bridge between Gunicorn and the Flask app graph;
it performs no per-worker state mutation beyond the one-time
:func:`create_app` invocation that builds the singleton ``app``.
Each Gunicorn worker re-imports this module on fork, calling
:func:`create_app` afresh; the resulting Flask app is local to that
worker (no shared mutable state).

Configuration selection happens inside :func:`create_app` via the
``FLASK_ENV`` environment variable -- this module does NOT pass an
explicit config class so that the production container's
``FLASK_ENV=production`` (or the default ``ProductionConfig``
fallback) drives the secret-loading and fail-fast invariants.

The module is intentionally minimal: it MUST NOT contain business
logic, configuration mutation, or side effects beyond the factory
call. All initialization (extensions, blueprints, middleware,
observability) lives inside :func:`create_app`.
"""

from __future__ import annotations

from app import create_app

# ``app`` is the WSGI application object. Gunicorn imports this
# module and reads the symbol named ``app`` (per the
# ``gunicorn wsgi:app`` invocation), so the construction of the
# Flask app graph happens at import time. The Flask app is bound to
# the worker process; each Gunicorn worker re-imports and gets its
# own instance.
#
# Per AAP Section 0.5.2 Layer 0, the factory expects ``FLASK_ENV``
# to drive config selection. The container's
# ``ENV FLASK_ENV=production`` (set in ``backend/Dockerfile``) is
# what makes :class:`app.config.ProductionConfig` the resolved
# class; no explicit override is needed here.
app = create_app()

# Note: This module is intended to be imported by Gunicorn (or another WSGI
# server). It is NOT meant to be executed directly via ``python wsgi.py``.
# For local development without Gunicorn, use the Flask CLI instead:
#     flask --app app run --debug --host 0.0.0.0 --port 5000
#
# A ``if __name__ == "__main__":`` guard is intentionally omitted: this
# file is never the program entrypoint. Production goes through Gunicorn
# (``gunicorn wsgi:app``) and local development goes through the Flask CLI
# against the ``app`` package directly. Keeping this file free of an
# ``app.run()`` block prevents accidental invocation of Flask's built-in
# development server in production, which would lack pre-fork concurrency
# and the production-grade signal handling Gunicorn provides.

__all__ = ["app"]
