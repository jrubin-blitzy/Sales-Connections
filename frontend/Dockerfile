# syntax=docker/dockerfile:1.7
# =============================================================================
# Sales-Connections Frontend - Multi-stage Production Container
# =============================================================================
# Builds the React 19.2.5 + Vite 5.4.x SPA into a static bundle in stage 1
# (Node 20 LTS Alpine), then serves the bundle from nginx 1.27 Alpine in
# stage 2. The resulting image is the canonical containerized delivery for
# the SPA per AAP Sec 0.5.4 (S3 + CloudFront is the documented alternative).
#
# Per AAP Sec 0.2.3 / 0.6.1:
#   "frontend/Dockerfile - Multi-stage build producing nginx static-asset
#    image (alternative to S3+CloudFront)"
#
# Per AAP Sec 0.7.7:
#   "Two-Docker-image release strategy. One backend image, one optional
#    frontend nginx image."  &  "Pinned versions everywhere."
#
# Build:    docker build -t sales-connections-frontend:dev frontend/
# Run:      docker run --rm -p 5173:80 sales-connections-frontend:dev
# Health:   curl -sf http://localhost:5173/healthz
# Compose:  docker compose up frontend  (build context ./frontend)
#
# Production override example:
#   docker build \
#     --build-arg VITE_API_BASE_URL=https://api.sales-connections.example.com \
#     -t sales-connections-frontend:prod frontend/
# =============================================================================

# =============================================================================
# Stage 1: builder - Node 20 LTS Alpine with Vite production build
# =============================================================================
# Pinned to Node 20 LTS Alpine per AAP Sec 0.3.1 ("Node.js 20 LTS"). Alpine
# is selected for minimal attack surface and fast pull times in ECS Fargate
# cold starts. The major-tag pin (`20-alpine`, not `20.20.2-alpine`) lets
# upstream security patches roll in on rebuild while keeping the LTS line.
FROM node:20-alpine AS builder

# CI=true silences npm progress output and disables interactive prompts
# (matching the GitHub Actions environment that the workflows run under).
# NPM_CONFIG_LOGLEVEL=warn keeps build logs concise without losing
# actionable warnings.
#
# NODE_ENV is intentionally NOT set to "production" here, because doing
# so before `npm ci` would cause npm to skip devDependencies - and the
# devDependencies block contains TypeScript, Vite, Tailwind, PostCSS,
# Autoprefixer, and the React build plugin. Without those, `tsc -b &&
# vite build` cannot succeed. NODE_ENV is set to production AFTER the
# install step, immediately before the build runs.
ENV NPM_CONFIG_LOGLEVEL=warn \
    CI=true

WORKDIR /app

# -----------------------------------------------------------------------------
# Layer 1: dependency manifest (cacheable across most rebuilds)
# -----------------------------------------------------------------------------
# Copy ONLY the lockfile and manifest first. Docker layer caching means
# `npm ci` re-runs only when package.json or package-lock.json changes,
# not on every source-code edit. This is the single largest contributor
# to fast incremental rebuilds in CI/CD.
COPY package.json package-lock.json ./

# Reproducible install from the lockfile only. `npm ci` (NOT `npm install`)
# refuses to run if package.json and package-lock.json are out of sync, so
# any dependency drift is caught immediately - this is mandatory per AAP
# Sec 0.7.7 ("Pinned versions everywhere").
#
# --include=dev forces installation of devDependencies regardless of
# NODE_ENV - we need TypeScript, Vite, Tailwind, etc. to build the
# bundle. Although NODE_ENV is unset above, --include=dev belt-and-
# suspenders the behavior so that future ENV changes do not silently
# break the install.
#
# --no-audit and --no-fund silence vendor noise that has no place in CI
# logs; --prefer-offline accelerates rebuilds when the npm cache is warm.
RUN npm ci --include=dev --no-audit --no-fund --prefer-offline

# -----------------------------------------------------------------------------
# Layer 2: TypeScript / Vite / Tailwind / PostCSS configuration
# -----------------------------------------------------------------------------
# `npm run build` runs `tsc -b && vite build`. The `tsc -b` step walks the
# project references in tsconfig.json:
#   tsconfig.json -> { tsconfig.app.json, tsconfig.node.json,
#                      tsconfig.test.json }
# All four files MUST be present, or `tsc -b` will fail with
# "File 'tsconfig.app.json' not found".
#
# Vite reads vite.config.ts (which transitively imports tailwind.config.ts
# and references postcss.config.cjs). index.html is the SPA entry point
# Vite mounts the bundle into.
COPY tsconfig.json tsconfig.app.json tsconfig.node.json tsconfig.test.json ./
COPY vite.config.ts tailwind.config.ts postcss.config.cjs index.html ./

# -----------------------------------------------------------------------------
# Layer 3: project sources
# -----------------------------------------------------------------------------
# src/ holds the React application code (components, hooks, schemas, API
# client). tests/ is included because tsconfig.test.json (referenced by
# tsconfig.json) has `"include": ["tests", "src"]` - omitting tests/
# would cause `tsc -b` to fail. The image-size cost is zero in the final
# stage because the runtime stage drops everything from the builder.
COPY src ./src
COPY tests ./tests

# -----------------------------------------------------------------------------
# Build-time environment variables
# -----------------------------------------------------------------------------
# Vite inlines `import.meta.env.VITE_*` references at build time, so the
# value of VITE_API_BASE_URL is BAKED into the bundle. To change it at
# runtime, the image MUST be rebuilt with a new --build-arg.
#
# Defaults to /api so that:
#   - In docker-compose: docker-compose.yml override sets this to the
#     compose-network backend URL (e.g., http://localhost:5000) so the
#     SPA's fetch() calls reach the published Flask container port.
#   - In production behind ALB: pass --build-arg with the public API
#     hostname, e.g. https://api.sales-connections.example.com.
#
# Per AAP Sec 0.7.4 ("No secrets baked in"), VITE_API_BASE_URL is the
# ONLY build-arg accepted here. ANTHROPIC_API_KEY, JWT_SIGNING_KEY,
# OAuth secrets, DB credentials are NEVER set in this Dockerfile - the
# frontend never sees them per the security model.
ARG VITE_API_BASE_URL=/api
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}

# Switch NODE_ENV to production NOW (after `npm ci`) so Vite emits
# minified, dev-tool-free bundles and React drops PropTypes / dev
# warnings. This is set as a separate ENV instruction rather than
# inlined with VITE_API_BASE_URL above so that the cache layer for
# the production-mode flip is isolated and readable in `docker history`.
ENV NODE_ENV=production

# Production build - emits /app/dist/ with content-hashed assets at
# /app/dist/assets/<name>-<hash>.<ext> (see vite.config.ts rollupOptions).
# The `tsc -b` step in the build script catches type errors before
# bundling so the image only ships if TypeScript compiles cleanly.
RUN npm run build

# =============================================================================
# Stage 2: runtime - nginx 1.27 Alpine serving static SPA assets
# =============================================================================
# Pinned to nginx 1.27.x per AAP Sec 0.3.1 ("nginx 1.27.x"). The Alpine
# variant is chosen for the same reasons as stage 1: minimal attack
# surface and fast ECS Fargate cold starts. Final image size (Alpine +
# nginx + static bundle) is well under 60 MB.
FROM nginx:1.27-alpine AS runtime

# OCI image labels enable image governance, ECR provenance tracking,
# GitHub container scanning attribution, and CloudWatch dashboard
# image-tag rendering. These follow the OCI Image Specification
# annotations (https://specs.opencontainers.org/image-spec/annotations/).
LABEL org.opencontainers.image.title="sales-connections-frontend" \
      org.opencontainers.image.description="Sales-Connections React 19.2.5 SPA served by nginx" \
      org.opencontainers.image.licenses="Proprietary" \
      org.opencontainers.image.vendor="Sales-Connections"

# -----------------------------------------------------------------------------
# Replace default nginx site
# -----------------------------------------------------------------------------
# The base image ships /etc/nginx/conf.d/default.conf which serves the
# nginx welcome page on port 80. Removing it before copying the project's
# config guarantees there is no overlap and that our server block is the
# only one nginx loads.
RUN rm -f /etc/nginx/conf.d/default.conf

# -----------------------------------------------------------------------------
# Project nginx configuration
# -----------------------------------------------------------------------------
# frontend/nginx.conf provides:
#   - History-mode SPA fallback (try_files $uri $uri/ /index.html) so
#     deep routes like /feed, /connections/:id, /admin/users return
#     index.html and let react-router-dom 6.x own the route table.
#   - Gzip compression for text-based assets above 1 KB.
#   - Cache-Control: public, max-age=31536000, immutable for content-
#     hashed Vite assets (per AAP Sec 0.7.3 cache strategy).
#   - Cache-Control: no-cache for index.html so deploys roll out fast.
#   - Security headers (X-Content-Type-Options, X-Frame-Options,
#     Referrer-Policy, Permissions-Policy) on every response.
#   - /healthz endpoint returning 200 "ok" without loading the SPA
#     bundle - the HEALTHCHECK directive below probes this endpoint.
COPY nginx.conf /etc/nginx/conf.d/default.conf

# -----------------------------------------------------------------------------
# Static SPA bundle from builder stage
# -----------------------------------------------------------------------------
# The multi-stage COPY drops Node.js, npm, node_modules/, source code,
# tests/, and all TypeScript/Vite tooling from the final image - only
# the compiled /app/dist/ tree (HTML, CSS, JS, assets) survives.
COPY --from=builder /app/dist /usr/share/nginx/html

# -----------------------------------------------------------------------------
# Drop privileges to non-root nginx user
# -----------------------------------------------------------------------------
# The official nginx:alpine image already includes a `nginx` user/group
# (UID/GID 101) but several runtime paths default to root:root ownership.
# Without the chown step, nginx running as `nginx` cannot create its PID
# file or write cache/log files, and the container exits with
# "[emerg] open() ... failed (13: Permission denied)".
#
# Granting `nginx:nginx` ownership of:
#   - /usr/share/nginx/html  (read - serves the SPA static assets)
#   - /var/cache/nginx       (write - fastcgi/proxy temp files)
#   - /var/log/nginx         (write - access/error logs symlinked
#                             to /dev/stdout and /dev/stderr by the
#                             base image, which the awslogs Docker
#                             driver picks up in production)
#   - /etc/nginx/conf.d      (read - our default.conf is here)
#   - /var/run/nginx.pid     (write - the master daemon PID file;
#                             touched first because it does not exist
#                             on the base image until nginx starts)
RUN chown -R nginx:nginx /usr/share/nginx/html /var/cache/nginx /var/log/nginx /etc/nginx/conf.d \
 && touch /var/run/nginx.pid \
 && chown nginx:nginx /var/run/nginx.pid

# Switch to the unprivileged user. Subsequent instructions, including
# the entrypoint, run as UID 101. This satisfies the principle of least
# privilege and permits deployment to ECS Fargate task definitions that
# enforce non-root constraints.
USER nginx

# -----------------------------------------------------------------------------
# Network and lifecycle directives
# -----------------------------------------------------------------------------
# Container port 80 is the documented contract:
#   - docker-compose.yml maps host 5173 -> container 80 to mirror the
#     Vite dev-server default port for developer mental-model continuity
#     (per AAP Sec 0.5.2).
#   - infra/terraform/modules/alb/main.tf target group port 80.
EXPOSE 80

# -----------------------------------------------------------------------------
# HEALTHCHECK
# -----------------------------------------------------------------------------
# Probes the /healthz endpoint defined in nginx.conf, which returns
# `200 "ok\n"` without loading the SPA bundle. /healthz is preferred
# over `/` for three reasons:
#   1. It is cheaper (5 bytes vs the full SPA shell HTML).
#   2. It is excluded from access logs (no log noise from the polling).
#   3. It is the conventional path the ALB target group health check
#      uses, so the local probe matches the production probe.
#
# Probe parameters:
#   --interval=30s     probe every 30s after the start period elapses
#   --timeout=5s       fail the probe if /healthz takes longer than 5s
#   --start-period=10s do not count failures during the first 10s
#                      while nginx initializes
#   --retries=3        require 3 consecutive failures before marking
#                      the container unhealthy
#
# wget is selected (not curl) because the nginx:alpine image ships
# with BusyBox wget pre-installed; adding curl would inflate the image.
# The --spider flag makes wget perform a HEAD-style probe without
# downloading the body.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD wget --quiet --spider --tries=1 --timeout=3 http://localhost:80/healthz || exit 1

# -----------------------------------------------------------------------------
# Entrypoint
# -----------------------------------------------------------------------------
# nginx must run in the foreground inside a container - the default
# `daemon on;` would fork the master process and exit, which Docker
# interprets as "container died" and triggers a restart loop. The
# `daemon off;` directive keeps nginx attached to PID 1 of the container
# so logs flow to the Docker logging driver and SIGTERM is delivered
# to nginx for graceful shutdown.
CMD ["nginx", "-g", "daemon off;"]
