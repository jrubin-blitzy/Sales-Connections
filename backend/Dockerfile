# syntax=docker/dockerfile:1.7
# =============================================================================
# Sales-Connections Backend - Multi-stage Production Dockerfile
# =============================================================================
# Produces the container image that runs on AWS ECS Fargate (AAP 0.4.6) and
# inside the local Docker Compose stack (AAP 0.5.2). It packages the Flask
# 3.1.3 + Gunicorn 23.x application, optionally runs Alembic schema migrations
# at startup, and exposes port 8000.
#
# Stage 1 (builder): installs requirements.txt into a virtualenv at /opt/venv.
#                    Carries build tooling (build-essential, libpq-dev) that
#                    must NOT ship to production.
# Stage 2 (runtime): python:3.12-slim-bookworm with the prebuilt venv and the
#                    application source. Tini reaps zombies and forwards
#                    signals so Gunicorn shuts down cleanly during ECS scale-
#                    down. The non-root "appuser" (UID/GID 1000) owns /app.
#
# Migration semantics (AAP 0.5.2 - "preflight step"):
#   When RUN_MIGRATIONS=true the container FIRST runs `alembic upgrade head`
#   and THEN execs Gunicorn. Migrations are idempotent so this is safe to
#   leave on at every container start; production typically uses a dedicated
#   migration task and leaves RUN_MIGRATIONS=false on API tasks.
#
# Build commands:
#   docker build -f backend/Dockerfile -t sales-connections-backend:latest backend/
#   docker compose build backend                  # via local dev stack
#
# Run commands:
#   docker run --rm -p 5000:8000 --env-file backend/.env \
#       sales-connections-backend:latest
#   docker run --rm -e RUN_MIGRATIONS=true -e DATABASE_URL=... \
#       sales-connections-backend:latest
#
# Per AAP 0.7.7 ("Pinned versions everywhere") the base image tag pins both
# the Python minor (3.12) and the Debian codename (bookworm). The "slim"
# variant is used for both stages; the runtime stage is reduced to ~250 MB by
# excluding build tooling and apt caches.
# =============================================================================


# -----------------------------------------------------------------------------
# Stage 1: builder
# -----------------------------------------------------------------------------
# Compiles any source-only wheels (e.g. legacy bcrypt fall-throughs) and
# installs every pinned production dependency from requirements.txt into a
# self-contained virtualenv at /opt/venv. The venv is then copied verbatim
# into stage 2 - none of the build tooling installed here ships to runtime.
# -----------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS builder

# Predictable, deterministic Python and pip behavior:
#   PYTHONDONTWRITEBYTECODE - skip .pyc files in the venv (smaller image,
#                              avoids permission issues at runtime).
#   PYTHONUNBUFFERED         - flush stdout/stderr immediately so logs reach
#                              CloudWatch in real time.
#   PIP_NO_CACHE_DIR         - never write the pip wheel cache (smaller image).
#   PIP_DISABLE_PIP_VERSION_CHECK - silence the upgrade-prompt that pollutes
#                                    build logs.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build dependencies. The psycopg[binary] extra ships pre-compiled libpq
# wheels so libpq-dev is technically redundant for psycopg itself, but we
# include it as a safety net in case a contributor adds psycopg2 (source-
# only) or another C-extension that links against libpq. build-essential
# covers gcc + make + libc6-dev for any source-only wheel that lacks a
# manylinux build for cp312.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Create the virtualenv at a stable location that survives the multi-stage
# COPY. Putting it under /opt/venv (not /app) lets stage 2 own /app for the
# non-root user without permission churn on /opt/venv.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Upgrade pip / setuptools / wheel in the venv so newer build-system PEPs
# (PEP 517, PEP 660) are honoured by every wheel we install next.
RUN pip install --upgrade pip setuptools wheel

WORKDIR /build

# Layer-cache friendly: copy ONLY requirements.txt before the rest of the
# source so that a code-only change does not invalidate the dependency
# install layer.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt


# -----------------------------------------------------------------------------
# Stage 2: runtime
# -----------------------------------------------------------------------------
# Production-targeted image. No compilers, no -dev headers, no apt cache.
# Inherits the same Python and Debian pin as the builder so wheels compiled
# in stage 1 link cleanly here.
# -----------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

# OCI image labels for provenance and supply-chain governance. The CI/CD
# pipeline (.github/workflows/cd.yml) MAY override `org.opencontainers.image
# .source` and `.revision` via --label flags at build time to tag images
# with the exact git SHA that produced them.
LABEL org.opencontainers.image.title="sales-connections-backend" \
      org.opencontainers.image.description="Sales-Connections Flask 3.1.3 REST API (Python 3.12)" \
      org.opencontainers.image.source="https://github.com/blitzy-public/sales-connections" \
      org.opencontainers.image.licenses="Proprietary" \
      org.opencontainers.image.vendor="Sales-Connections" \
      org.opencontainers.image.base.name="docker.io/library/python:3.12-slim-bookworm"

# Runtime environment defaults. EVERY value here is a default that the ECS
# task definition (production) or docker-compose (local) should override as
# needed. Notable invariants:
#   PATH                        - prepend the venv so `gunicorn`, `alembic`,
#                                  and `python` all resolve to /opt/venv.
#   WEB_CONCURRENCY=4           - sane default for a 1-vCPU Fargate task.
#                                  AAP 0.5.2 says "production tunes to vCPU".
#   PORT=8000                   - matches docker-compose `5000:8000` mapping
#                                  (host 5000 -> container 8000) and the ALB
#                                  target group port 8000 in production.
#   RUN_MIGRATIONS=false        - migrations are opt-in. Production typically
#                                  runs them once via a dedicated ECS task.
#   GUNICORN_TIMEOUT=30         - covers the 5s AI budget (AAP 0.7.3) plus a
#                                  generous safety margin.
#   GUNICORN_GRACEFUL_TIMEOUT=30 - allows Gunicorn to drain in-flight requests
#                                  during ECS scale-down before SIGKILL.
#   GUNICORN_KEEP_ALIVE=5       - matches typical ALB idle timeout behaviour.
#   GUNICORN_LOG_LEVEL=info     - structured Gunicorn logs go to stdout.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/opt/venv/bin:$PATH" \
    WEB_CONCURRENCY=4 \
    PORT=8000 \
    RUN_MIGRATIONS=false \
    GUNICORN_LOG_LEVEL=info \
    GUNICORN_TIMEOUT=30 \
    GUNICORN_GRACEFUL_TIMEOUT=30 \
    GUNICORN_KEEP_ALIVE=5

# Minimal runtime libraries. tini becomes PID 1 (via ENTRYPOINT below) and
# forwards SIGTERM cleanly to Gunicorn during ECS task replacement; without
# it, Gunicorn would receive SIGKILL after ECS' stop timeout and in-flight
# requests would be terminated. libpq5 is the runtime shared library that
# psycopg[binary] dlopen()s; it is small and inexpensive even though the
# binary wheel ships its own copy.
#
# The non-root "appuser" account (UID/GID 1000) is created in the same RUN
# layer to keep the image flat. AAP 0.7.4 ("Security Invariants") forbids
# running production containers as root.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libpq5 \
        tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 1000 appuser \
    && useradd --system --create-home --uid 1000 --gid 1000 --shell /bin/bash appuser

WORKDIR /app

# Copy the prebuilt virtualenv from the builder stage. /opt/venv is owned by
# root (read-only for appuser) which is fine: Gunicorn never writes into it.
COPY --from=builder /opt/venv /opt/venv

# Copy ONLY the artefacts needed at runtime. Per AAP 0.5.2 we explicitly
# exclude the dev venv, test directories, build caches, and the rest of
# the development surface (further filtered by .dockerignore).
#
# `app/` is the Flask application package (created by feature-handler agents).
# `migrations/` is the Alembic migrations tree (env.py + versions/).
# `wsgi.py` is the Gunicorn entrypoint (`app = create_app()`).
# `alembic.ini` configures Alembic when present at the backend root. The
# trailing "*" makes this COPY tolerant of Alembic config that lives inline
# inside `migrations/env.py` instead.
COPY --chown=appuser:appuser app/ /app/app/
COPY --chown=appuser:appuser migrations/ /app/migrations/
COPY --chown=appuser:appuser wsgi.py /app/wsgi.py
COPY --chown=appuser:appuser alembic.ini* /app/

# Drop privileges. Every subsequent COMMAND, ENTRYPOINT, and CMD runs as
# UID 1000.
USER appuser

# Document the listening port. ECS task definitions and docker-compose
# `ports:` mappings still need to publish this explicitly; EXPOSE alone
# does not open ports.
EXPOSE 8000

# Container-level health check. The Flask /healthz endpoint (AAP 0.4.3)
# returns 200 unconditionally for liveness; /readyz also probes Postgres.
# We use the stdlib `urllib.request` instead of curl/wget to avoid adding
# either to the runtime image - python is already on PATH via the venv.
#
# In production (AWS ECS Fargate behind an ALB) the ALB target group's own
# health check is authoritative; this HEALTHCHECK exists for docker-compose
# and local debugging.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status == 200 else 1)" || exit 1

# Tini becomes PID 1 inside the container. It reaps zombie children and,
# critically, forwards SIGTERM to its child (the shell, then Gunicorn) so
# graceful shutdown actually happens. Without tini the shell would not
# propagate signals and Gunicorn would only stop after SIGKILL.
ENTRYPOINT ["/usr/bin/tini", "--"]

# Default command:
#   1. (Preflight) If RUN_MIGRATIONS=true, run `alembic upgrade head`.
#      Alembic is idempotent: if the schema is already at head, this is a
#      no-op that exits 0 in milliseconds.
#   2. Replace the shell with Gunicorn via `exec` so Gunicorn becomes tini's
#      direct child and receives signals correctly. Without `exec`, the
#      shell would be the signal target and Gunicorn would never see
#      SIGTERM.
#
# Gunicorn flags (AAP 0.5.2):
#   --workers ${WEB_CONCURRENCY}        process count (tune to vCPU)
#   --bind 0.0.0.0:${PORT}              listen on all interfaces in-container
#   --timeout ${GUNICORN_TIMEOUT}       worker request budget
#   --graceful-timeout ${...}           drain timeout on SIGTERM
#   --keep-alive ${GUNICORN_KEEP_ALIVE} idle keep-alive seconds
#   --access-logfile -                  access log to stdout (CloudWatch)
#   --error-logfile  -                  error log to stdout (CloudWatch)
#   --log-level ${GUNICORN_LOG_LEVEL}   filter level for Gunicorn's own logs
#
# CMD is shell-form (sh -c "...") to allow env-var expansion of
# WEB_CONCURRENCY etc. while still benefiting from `exec` to swap in
# Gunicorn as tini's direct child.
CMD ["sh", "-c", "if [ \"$RUN_MIGRATIONS\" = \"true\" ]; then echo 'Running Alembic migrations...'; alembic upgrade head; fi && exec gunicorn wsgi:app --workers ${WEB_CONCURRENCY} --bind 0.0.0.0:${PORT} --timeout ${GUNICORN_TIMEOUT} --graceful-timeout ${GUNICORN_GRACEFUL_TIMEOUT} --keep-alive ${GUNICORN_KEEP_ALIVE} --access-logfile - --error-logfile - --log-level ${GUNICORN_LOG_LEVEL}"]
