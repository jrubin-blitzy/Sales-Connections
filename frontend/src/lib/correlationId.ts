/**
 * correlationId.ts - Per-page-load correlation ID generator.
 *
 * The correlation ID is attached as the X-Correlation-Id header on every
 * API request via @/api/client.ts. The backend's correlation middleware
 * (backend/app/middleware/correlation.py) reads the header and binds it
 * to structlog context and OpenTelemetry trace context, enabling
 * end-to-end log correlation across the frontend and backend per AAP
 * Sec 0.7.5 (Observability rule) and Sec 0.4.3 (Surface 1).
 *
 * Lifecycle:
 *   - First call to getCorrelationId() lazily generates an ID and caches
 *     it in a module-level variable.
 *   - All subsequent calls return the same cached ID for the rest of the
 *     page load. This means every API call within a single SPA session
 *     shares the same correlation ID, while a hard reload (or a deliberate
 *     resetCorrelationId() call) starts a fresh correlation context.
 *   - resetCorrelationId() is invoked at logical operation boundaries
 *     (e.g., login success, logout) to start a fresh correlation context.
 *
 * Storage choice:
 *   - Module-level `let` only. Not localStorage, not sessionStorage,
 *     not cookies. The correlation ID is intentionally NOT persisted
 *     across reloads because each page load is a logically distinct
 *     "session" from an observability perspective.
 *
 * Format:
 *   <prefix><uuid v4>
 *   e.g., "sc-fe-3f50bd51-2d0a-4b3a-9f4f-1e25c6fdc5cf"
 *
 *   The prefix comes from VITE_CORRELATION_ID_PREFIX (default "sc-fe-").
 *   The UUID v4 comes from the Web Crypto API's crypto.randomUUID().
 *
 * Browser support:
 *   crypto.randomUUID() is available in all modern browsers (Chrome 92+,
 *   Firefox 95+, Safari 15.4+). The browserslist configuration in
 *   frontend/package.json excludes IE 11, so a polyfill is not required.
 *
 * Security:
 *   The correlation ID is NOT a secret - it is a non-sensitive
 *   pseudo-random identifier. It must NOT be reused as a session token,
 *   nonce, or anti-CSRF token. Its sole purpose is observability.
 */

/**
 * Configurable prefix for every correlation ID. Sourced from
 * VITE_CORRELATION_ID_PREFIX (declared in frontend/.env.example and
 * typed in frontend/src/vite-env.d.ts). Defaults to "sc-fe-" when the
 * environment variable is unset or empty.
 *
 * Uses `||` (not `??`) so an explicitly-set empty string `""` falls back
 * to the default. A user who sets `VITE_CORRELATION_ID_PREFIX=` (with no
 * value) almost certainly wants the default behavior, not literally
 * empty-prefixed IDs.
 */
const CORRELATION_ID_PREFIX: string = import.meta.env.VITE_CORRELATION_ID_PREFIX || "sc-fe-";

/**
 * Module-level cache for the current correlation ID. `null` means "not
 * yet generated"; the first call to getCorrelationId() materializes it.
 *
 * Module-level mutable state is intentional and justified by the
 * observability requirement. Each ES module is evaluated exactly once
 * per page load, so this `let` is effectively a per-page singleton.
 */
let cachedCorrelationId: string | null = null;

/**
 * Constructs a fresh correlation ID by concatenating the configured
 * prefix with a Web Crypto UUID v4. Always returns a non-empty string.
 *
 * No try/catch around `crypto.randomUUID()`: if Web Crypto is unavailable
 * the SPA cannot meaningfully run anyway, and crashing loudly is better
 * than silent fallback to a weak random source such as Math.random().
 *
 * @internal
 */
function generateNewId(): string {
  // crypto.randomUUID() is supported in all modern browsers per the
  // project's browserslist; no polyfill is required.
  return `${CORRELATION_ID_PREFIX}${crypto.randomUUID()}`;
}

/**
 * Returns the current correlation ID for this page load.
 *
 * Behavior:
 *   - First call: lazily generates a new ID via crypto.randomUUID(),
 *     caches it, and returns it.
 *   - Subsequent calls: returns the cached value unchanged.
 *
 * The ID format is "<prefix><uuid v4>", e.g.,
 * "sc-fe-3f50bd51-2d0a-4b3a-9f4f-1e25c6fdc5cf".
 *
 * Consumers:
 *   - frontend/src/api/client.ts attaches this value to the
 *     X-Correlation-Id header on every fetch request.
 *
 * @returns The cached correlation ID for this page load.
 */
export function getCorrelationId(): string {
  if (cachedCorrelationId === null) {
    cachedCorrelationId = generateNewId();
  }
  return cachedCorrelationId;
}

/**
 * Forces generation of a new correlation ID, replacing the cached value.
 *
 * Use at logical operation boundaries where a fresh correlation context
 * is desired:
 *   - Login success (so the post-auth session has a new correlation
 *     scope distinct from the unauthenticated browsing).
 *   - Logout (so any subsequent activity correlates separately).
 *   - Long-running multi-step workflows where each step is logically
 *     distinct.
 *
 * @returns The newly-generated correlation ID.
 */
export function resetCorrelationId(): string {
  cachedCorrelationId = generateNewId();
  return cachedCorrelationId;
}
