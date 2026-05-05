/**
 * correlationId.test.ts - Unit tests for the per-page-load correlation
 * ID generator at frontend/src/lib/correlationId.ts.
 *
 * Module under test exports:
 *   - getCorrelationId(): string  (lazy-cached)
 *   - resetCorrelationId(): string  (forces a fresh UUID)
 *
 * Test isolation:
 *   The module-under-test holds a module-level `cachedCorrelationId`
 *   that persists across `it()` blocks within a single test file (ES
 *   modules are evaluated exactly once per worker). Each test in this
 *   file is therefore designed to be order-independent: assertions
 *   verify behavioural invariants (caching consistency, format,
 *   reset behaviour, no storage access, env-handled prefix) rather
 *   than specific cached values. Tests that need to observe a fresh
 *   ID call `resetCorrelationId()` inside the test body; tests that
 *   need to compare pre/post-reset IDs capture the "before" value
 *   from `getCorrelationId()` themselves.
 *
 *   Env-stubbing tests use `vi.stubEnv` + `vi.resetModules` + dynamic
 *   `await import(...)` to re-evaluate the module with a stubbed
 *   `import.meta.env.VITE_CORRELATION_ID_PREFIX`. Static-imported
 *   references are NOT affected by `vi.resetModules()` (they continue
 *   to point at the original module evaluation), so tests outside
 *   the env-handling describe still operate on the same cached state
 *   they would in production.
 *
 * Coverage targets:
 *   - Lazy caching across repeated getCorrelationId() calls.
 *   - Format compliance (prefix + UUID v4 per RFC 4122).
 *   - resetCorrelationId() returns a NEW ID and updates the cache.
 *   - Configurable prefix via VITE_CORRELATION_ID_PREFIX (empty falls
 *     back to "sc-fe-" because the source uses `||`, not `??`).
 *   - No persistence to localStorage, sessionStorage, or cookies.
 *
 * Conventions per AAP Sec 0.7.7 and frontend/.prettierrc.json:
 *   - Strict TypeScript; no `any`.
 *   - Double quotes; trailing commas; 2-space indent; line length <= 100.
 *   - No React, no MSW, no HTTP. Pure unit tests.
 *   - No emoji; no console.log.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { getCorrelationId, resetCorrelationId } from "@/lib/correlationId";

// ---------------------------------------------------------------------------
// Shared regex constants
// ---------------------------------------------------------------------------

/**
 * Expected default format: "<prefix><uuid v4>"
 *   prefix:  "sc-fe-" (default per VITE_CORRELATION_ID_PREFIX fallback)
 *   uuid v4: 8-4-4-4-12 hex with the third group starting with "4" and
 *            the fourth group starting with [89ab] per RFC 4122.
 *
 * Example match: "sc-fe-3f50bd51-2d0a-4b3a-9f4f-1e25c6fdc5cf"
 */
const DEFAULT_FORMAT_REGEX =
  /^sc-fe-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

/**
 * Same RFC 4122 v4 layout as DEFAULT_FORMAT_REGEX but parameterised on
 * a custom prefix used by the env-handling describe block.
 */
const CUSTOM_PREFIX_FORMAT_REGEX =
  /^test-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

// ---------------------------------------------------------------------------
// Top-level afterEach: restore spies and stubbed env entries after each test.
// vi.resetModules() is intentionally NOT called here at the top level because
// non-env tests use the static-imported functions; resetting the module graph
// between every test would be wasteful. The env-specific describe block
// handles its own module-cache resets locally.
// ---------------------------------------------------------------------------

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllEnvs();
});

// ---------------------------------------------------------------------------
// Test suite root
// ---------------------------------------------------------------------------

describe("correlationId", () => {
  // -------------------------------------------------------------------------
  // getCorrelationId() - lazy caching, format, no storage access
  // -------------------------------------------------------------------------

  describe("getCorrelationId()", () => {
    it("returns the same ID on repeated calls (lazy caching)", () => {
      const first = getCorrelationId();
      const second = getCorrelationId();
      const third = getCorrelationId();

      expect(first).toBe(second);
      expect(second).toBe(third);
    });

    it('returns an ID matching the documented "<prefix><uuid v4>" format', () => {
      const id = getCorrelationId();
      expect(id).toMatch(DEFAULT_FORMAT_REGEX);
    });

    it('returns an ID starting with the default "sc-fe-" prefix', () => {
      const id = getCorrelationId();
      expect(id.startsWith("sc-fe-")).toBe(true);
    });

    it("does not access localStorage or sessionStorage", () => {
      // Spy on Storage.prototype methods so the spy fires for both
      // window.localStorage and window.sessionStorage (both inherit
      // from Storage). Per AAP authoring conventions the source must
      // be storage-API-free (the correlation ID is intentionally
      // not persisted across reloads).
      const getItemSpy = vi.spyOn(Storage.prototype, "getItem");
      const setItemSpy = vi.spyOn(Storage.prototype, "setItem");
      const removeItemSpy = vi.spyOn(Storage.prototype, "removeItem");
      const clearSpy = vi.spyOn(Storage.prototype, "clear");

      // Exercise both public APIs across multiple lifecycle transitions
      // so the assertion below covers all reachable code paths.
      resetCorrelationId();
      getCorrelationId();
      getCorrelationId();
      resetCorrelationId();
      getCorrelationId();

      expect(getItemSpy).not.toHaveBeenCalled();
      expect(setItemSpy).not.toHaveBeenCalled();
      expect(removeItemSpy).not.toHaveBeenCalled();
      expect(clearSpy).not.toHaveBeenCalled();
    });
  });

  // -------------------------------------------------------------------------
  // resetCorrelationId() - returns new ID, updates cache, format, uniqueness
  // -------------------------------------------------------------------------

  describe("resetCorrelationId()", () => {
    it("returns a new ID different from the previously cached one", () => {
      const before = getCorrelationId();
      const reset = resetCorrelationId();

      expect(reset).not.toBe(before);
    });

    it("updates the cache so subsequent getCorrelationId() returns the new ID", () => {
      const before = getCorrelationId();
      const reset = resetCorrelationId();
      const afterReset = getCorrelationId();

      // The post-reset cached ID is the same one returned by reset...
      expect(afterReset).toBe(reset);
      // ...and is distinct from the pre-reset ID.
      expect(afterReset).not.toBe(before);
    });

    it('returns an ID matching the documented "<prefix><uuid v4>" format', () => {
      const id = resetCorrelationId();
      expect(id).toMatch(DEFAULT_FORMAT_REGEX);
    });

    it("produces unique IDs across many consecutive resets", () => {
      // crypto.randomUUID() collisions are statistically negligible at
      // this volume (10 calls produce 10 distinct values with probability
      // > 1 - 10^-37). This test confirms the source uses a real UUID
      // generator rather than a constant or low-entropy fallback.
      const ids = new Set<string>();
      for (let i = 0; i < 10; i += 1) {
        ids.add(resetCorrelationId());
      }
      expect(ids.size).toBe(10);
    });
  });

  // -------------------------------------------------------------------------
  // VITE_CORRELATION_ID_PREFIX environment variable handling
  // -------------------------------------------------------------------------

  describe("environment variable handling", () => {
    // Each env-stubbing test does vi.stubEnv + vi.resetModules + dynamic
    // import. The top-level afterEach calls vi.unstubAllEnvs() to restore
    // the env. We additionally vi.resetModules() inside each test so the
    // dynamic import sees the freshly-stubbed env value, and again in
    // this describe-scoped afterEach to ensure the module cache cannot
    // leak a stubbed-env-derived module instance into later tests.

    afterEach(() => {
      vi.resetModules();
    });

    it('falls back to "sc-fe-" when VITE_CORRELATION_ID_PREFIX is empty string', async () => {
      // The source uses `||` (not `??`), so an empty-string env value
      // is treated as falsy and falls back to the default prefix. This
      // test guards against a regression where someone changes `||` to
      // `??`, which would silently produce empty-prefixed IDs.
      vi.stubEnv("VITE_CORRELATION_ID_PREFIX", "");
      vi.resetModules();

      const mod = await import("@/lib/correlationId");
      const id = mod.getCorrelationId();

      expect(id.startsWith("sc-fe-")).toBe(true);
      expect(id).toMatch(DEFAULT_FORMAT_REGEX);
    });

    it("uses a custom prefix when VITE_CORRELATION_ID_PREFIX is set", async () => {
      vi.stubEnv("VITE_CORRELATION_ID_PREFIX", "test-");
      vi.resetModules();

      const mod = await import("@/lib/correlationId");
      const id = mod.getCorrelationId();

      expect(id.startsWith("test-")).toBe(true);
      expect(id).toMatch(CUSTOM_PREFIX_FORMAT_REGEX);
    });

    it("uses the default prefix when VITE_CORRELATION_ID_PREFIX is undefined", async () => {
      // vi.stubEnv cannot directly produce `undefined`; stubbing with
      // an empty string is the closest representation of "unset" and
      // exercises the same `||`-fallback branch the source takes for
      // a genuinely undefined env value.
      vi.stubEnv("VITE_CORRELATION_ID_PREFIX", "");
      vi.resetModules();

      const mod = await import("@/lib/correlationId");
      const id = mod.getCorrelationId();

      expect(id.startsWith("sc-fe-")).toBe(true);
    });
  });
});
