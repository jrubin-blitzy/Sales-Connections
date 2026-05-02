# syntax=docker/dockerfile:1.7
# =============================================================================
# Sales-Connections Backend - Multi-stage Production Dockerfile
# =============================================================================
# Stage 1 (builder): install Python dependencies into a virtualenv.
# Stage 2 (runtime): copy the venv and application source onto a slim image.
#
# Build:   docker build -t sales-connections-backend:latest backend/
# Run:     docker run --rm -p 8000:8000 --env-file backend/.env \
#              sales-connections-backend:latest
# Migrate: docker run --rm --env-file backend/.env \
#              -e RUN_MIGRATIONS=true sales-connections-backend:latest
# =============================================================================

# ---------- Stage 1: builder ----------
FROM python:3.12-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build deps for psycopg, cryptography, grpc native wheels (where binaries
# aren't published for slim).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        libssl-dev \
        libffi-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Create the venv and install Python deps.
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

COPY requirements.txt ./
RUN pip install --upgrade pip setuptools wheel \
    && pip install --no-cache-dir -r requirements.txt

# ---------- Stage 2: runtime ----------
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH=/opt/venv/bin:$PATH \
    GUNICORN_CMD_ARGS="--bind 0.0.0.0:8000 --access-logfile - --error-logfile - --log-level info" \
    WEB_CONCURRENCY=4

# Runtime libs only (no -dev).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
        tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -r app --gid 1001 \
    && useradd -r -g app --uid 1001 --home-dir /app --shell /bin/bash app

WORKDIR /app

# Copy the prebuilt venv from the builder.
COPY --from=builder /opt/venv /opt/venv

# Copy application source. The build context is `backend/`, so the layout below
# matches the repository layout.
COPY --chown=app:app . /app

# Switch to non-root user.
USER app

EXPOSE 8000

# Liveness probe (matches /healthz endpoint).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl --fail --silent --max-time 4 http://localhost:8000/healthz || exit 1

# Tini reaps zombie children & forwards signals so Gunicorn can shut down cleanly.
ENTRYPOINT ["/usr/bin/tini", "--"]

# The default command runs Gunicorn against the `wsgi:app` callable.
# Override RUN_MIGRATIONS=true to instead run Alembic migrations.
CMD ["bash", "-c", "if [ \"${RUN_MIGRATIONS:-false}\" = \"true\" ]; then exec alembic upgrade head; else exec gunicorn wsgi:app --workers ${WEB_CONCURRENCY:-4}; fi"]
