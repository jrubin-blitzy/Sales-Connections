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
