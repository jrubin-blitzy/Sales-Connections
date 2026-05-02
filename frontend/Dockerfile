# syntax=docker/dockerfile:1.7
# =============================================================================
# Sales-Connections Frontend - Multi-stage Production Dockerfile
# =============================================================================
# Stage 1 (builder): install npm deps and run `vite build`.
# Stage 2 (runtime): copy built `dist/` into an nginx static-asset image.
#
# Build:   docker build -t sales-connections-frontend:latest frontend/
# Run:     docker run --rm -p 8080:80 sales-connections-frontend:latest
#
# Note: §0.7.7 of the AAP allows S3+CloudFront as an alternative; this image
# is the local-dev / single-VM deployment target.
# =============================================================================

# ---------- Stage 1: builder ----------
FROM node:20.20.2-bookworm-slim AS builder

ENV CI=true \
    NPM_CONFIG_FUND=false \
    NPM_CONFIG_AUDIT=false

WORKDIR /build

# Use a deterministic install. Copy lock first for cache efficiency.
COPY package.json package-lock.json* ./
RUN npm ci --no-fund --no-audit --loglevel=error

# Copy the rest of the source and run the production build.
COPY . .

# `vite build` reads VITE_* env vars at build time. Provide via --build-arg.
ARG VITE_API_BASE_URL=""
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}

RUN npm run build

# ---------- Stage 2: runtime ----------
FROM nginx:1.27-alpine AS runtime

# Replace the default nginx config with the SPA-aware one.
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Copy built assets.
COPY --from=builder /build/dist /usr/share/nginx/html

# Drop privileges where possible. The default nginx user is "nginx".
EXPOSE 80

# Liveness probe.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD wget -qO- http://localhost/healthz || exit 1

CMD ["nginx", "-g", "daemon off;"]
