/**
 * queryClient.test.ts - Unit tests for the singleton TanStack Query 5.x
 * QueryClient at frontend/src/lib/queryClient.ts.
 *
 * Module under test exports:
 *   - queryClient: QueryClient  (singleton)
 *
 * Internal helpers (NOT exported):
 *   - extractErrorStatus(error): number | null
 *   - shouldRetryQuery(failureCount, error): boolean
 *
 * Test strategy:
 *   The singleton exposes its retry predicate via
 *   queryClient.getDefaultOptions().queries.retry, which is the same
 *   reference as the internal `shouldRetryQuery` function. We extract
 *   it once per test and exercise the predicate with synthetic error
 *   shapes covering 4xx, 5xx, network errors, and the MAX_QUERY_RETRIES
 *   ceiling.
 *
 *   `extractErrorStatus` is tested indirectly by feeding the retry
 *   predicate errors with and without `status` fields.
 *
 * Coverage targets:
 *   - Singleton invariant (same instance across imports).
 *   - Default options match the documented values:
 *       gcTime, staleTime, refetchOnWindowFocus, refetchOnReconnect,
 *       throwOnError, mutations.retry, mutations.throwOnError.
 *   - v5 API used (gcTime, NOT v4 cacheTime).
 *   - Retry predicate:
 *       - 4xx (400, 401, 403, 404, 409, 422, 499) -> false
 *       - 5xx (500, 502, 503, 504) -> true while failureCount < MAX_QUERY_RETRIES
 *       - failureCount >= MAX_QUERY_RETRIES -> false
 *       - Network errors (no status field) -> true while under MAX_QUERY_RETRIES
 *       - Various error shapes (Error, plain object, null, undefined, string) handled.
 *
 * Conventions per AAP Sec 0.7.7 and project .prettierrc.json:
 *   - Strict TypeScript; no `any`.
 *   - Double quotes; trailing commas; 2-space indent; line length <= 100.
 *   - No React, no MSW, no HTTP. Pure unit tests.
 *   - No emoji; no console.log.
 */

import { describe, expect, it } from "vitest";
import { QueryClient } from "@tanstack/react-query";

import { queryClient } from "@/lib/queryClient";

// ---------------------------------------------------------------------------
// Type narrowing helpers
// ---------------------------------------------------------------------------

/**
 * Type alias for the retry predicate signature exposed by TanStack
 * Query 5.x default options. Used by tests to safely cast the
 * `retry` field into a callable form after a typeof-narrowing check.
 *
 * The TanStack Query `RetryValue<TError>` union type is
 * `boolean | number | ShouldRetryFunction<TError>`; we assert the
 * function branch in `getRetryPredicate()` below.
 */
type RetryPredicate = (failureCount: number, error: unknown) => boolean;

/**
 * Extract the retry predicate from the singleton queryClient. Throws
 * if the predicate is not a function (which would indicate a regression
 * to a boolean/number value, defeating the project's custom 4xx-no-retry
 * policy).
 *
 * Centralising this narrowing in a single helper:
 *   - keeps each `it()` body short,
 *   - gives a clear error message on regression,
 *   - avoids repeating the typeof check in dozens of test cases.
 */
function getRetryPredicate(): RetryPredicate {
  const retry = queryClient.getDefaultOptions().queries?.retry;
  if (typeof retry !== "function") {
    throw new Error(
      `Expected queryClient default queries.retry to be a function, got ${typeof retry}`,
    );
  }
  return retry as RetryPredicate;
}

// ---------------------------------------------------------------------------
// Test suite: singleton invariant
// ---------------------------------------------------------------------------

describe("queryClient singleton", () => {
  it("exports a TanStack Query QueryClient instance", () => {
    expect(queryClient).toBeInstanceOf(QueryClient);
  });

  it("returns the same instance on repeated imports (ES module cache)", async () => {
    // Import the module a second time; ES module evaluation cache means
    // the same `queryClient` reference must be returned by every importer.
    const moduleA = await import("@/lib/queryClient");
    const moduleB = await import("@/lib/queryClient");

    expect(moduleA.queryClient).toBe(queryClient);
    expect(moduleB.queryClient).toBe(queryClient);
    expect(moduleA.queryClient).toBe(moduleB.queryClient);
  });

  it("exposes a getDefaultOptions() method", () => {
    expect(typeof queryClient.getDefaultOptions).toBe("function");
  });
});

// ---------------------------------------------------------------------------
// Test suite: default query options
// ---------------------------------------------------------------------------

describe("default query options", () => {
  it("has staleTime set to 60_000 ms (1 minute)", () => {
    expect(queryClient.getDefaultOptions().queries?.staleTime).toBe(60_000);
  });

  it("has gcTime set to 300_000 ms (5 minutes)", () => {
    expect(queryClient.getDefaultOptions().queries?.gcTime).toBe(300_000);
  });

  it("has refetchOnWindowFocus disabled", () => {
    expect(queryClient.getDefaultOptions().queries?.refetchOnWindowFocus).toBe(false);
  });

  it("has refetchOnReconnect enabled", () => {
    expect(queryClient.getDefaultOptions().queries?.refetchOnReconnect).toBe(true);
  });

  it("has throwOnError disabled (errors flow into hook callbacks)", () => {
    expect(queryClient.getDefaultOptions().queries?.throwOnError).toBe(false);
  });

  it("uses a function for the retry option (custom predicate)", () => {
    const retry = queryClient.getDefaultOptions().queries?.retry;
    expect(typeof retry).toBe("function");
  });

  it("does NOT define the v4 cacheTime field (TanStack Query 5.x renamed it to gcTime)", () => {
    // TanStack Query 5.x does not include `cacheTime` in DefaultOptions
    // typings; the field exists only on v4. This is a regression guard
    // against accidental v4 usage.
    //
    // Accessing the field via dynamic key avoids TS errors on the v5
    // type, which has no `cacheTime` property at all.
    const queries = queryClient.getDefaultOptions().queries as Record<string, unknown>;
    expect(queries["cacheTime"]).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// Test suite: default mutation options
// ---------------------------------------------------------------------------

describe("default mutation options", () => {
  it("has retry set to 0 (mutations never auto-retry)", () => {
    expect(queryClient.getDefaultOptions().mutations?.retry).toBe(0);
  });

  it("has throwOnError disabled (errors flow into hook callbacks)", () => {
    expect(queryClient.getDefaultOptions().mutations?.throwOnError).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Test suite: shouldRetryQuery predicate (exercised via queries.retry)
// ---------------------------------------------------------------------------

describe("shouldRetryQuery predicate (via queryClient.queries.retry)", () => {
  describe("4xx client errors do not retry", () => {
    it("returns false for status 400 (Bad Request)", () => {
      const retry = getRetryPredicate();
      expect(retry(0, { status: 400 })).toBe(false);
    });

    it("returns false for status 401 (Unauthorized)", () => {
      const retry = getRetryPredicate();
      expect(retry(0, { status: 401 })).toBe(false);
    });

    it("returns false for status 403 (Forbidden)", () => {
      const retry = getRetryPredicate();
      expect(retry(0, { status: 403 })).toBe(false);
    });

    it("returns false for status 404 (Not Found)", () => {
      const retry = getRetryPredicate();
      expect(retry(0, { status: 404 })).toBe(false);
    });

    it("returns false for status 409 (Conflict)", () => {
      const retry = getRetryPredicate();
      expect(retry(0, { status: 409 })).toBe(false);
    });

    it("returns false for status 422 (Unprocessable Entity)", () => {
      const retry = getRetryPredicate();
      expect(retry(0, { status: 422 })).toBe(false);
    });

    it("returns false at the upper 4xx boundary (status 499)", () => {
      const retry = getRetryPredicate();
      expect(retry(0, { status: 499 })).toBe(false);
    });
  });

  describe("5xx server errors retry up to MAX_QUERY_RETRIES", () => {
    it("returns true for status 500 on first failure (failureCount=0)", () => {
      const retry = getRetryPredicate();
      expect(retry(0, { status: 500 })).toBe(true);
    });

    it("returns true for status 500 on second failure (failureCount=1)", () => {
      const retry = getRetryPredicate();
      expect(retry(1, { status: 500 })).toBe(true);
    });

    it("returns false for status 500 once MAX_QUERY_RETRIES (2) is reached", () => {
      const retry = getRetryPredicate();
      expect(retry(2, { status: 500 })).toBe(false);
    });

    it("returns false for status 500 beyond MAX_QUERY_RETRIES (failureCount=3)", () => {
      const retry = getRetryPredicate();
      expect(retry(3, { status: 500 })).toBe(false);
    });

    it("returns true for status 502 (Bad Gateway)", () => {
      const retry = getRetryPredicate();
      expect(retry(0, { status: 502 })).toBe(true);
    });

    it("returns true for status 503 (Service Unavailable)", () => {
      const retry = getRetryPredicate();
      expect(retry(0, { status: 503 })).toBe(true);
    });

    it("returns true for status 504 (Gateway Timeout - AI watchdog)", () => {
      const retry = getRetryPredicate();
      expect(retry(0, { status: 504 })).toBe(true);
    });
  });

  describe("network and unknown errors retry (status field absent or non-numeric)", () => {
    it("returns true for a generic Error with no status field", () => {
      const retry = getRetryPredicate();
      expect(retry(0, new Error("network failure"))).toBe(true);
    });

    it("returns true for a TypeError (e.g., fetch network failure)", () => {
      const retry = getRetryPredicate();
      expect(retry(0, new TypeError("Failed to fetch"))).toBe(true);
    });

    it("returns true for a plain object with non-numeric status", () => {
      const retry = getRetryPredicate();
      // Duck-typing via extractErrorStatus: `status` must be `number`,
      // so a string value falls into the "no status" category and the
      // error is treated as transient.
      expect(retry(0, { status: "down" })).toBe(true);
    });

    it("returns true for a plain object without a status field", () => {
      const retry = getRetryPredicate();
      expect(retry(0, { message: "something went wrong" })).toBe(true);
    });

    it("returns true for null error", () => {
      const retry = getRetryPredicate();
      expect(retry(0, null)).toBe(true);
    });

    it("returns true for undefined error", () => {
      const retry = getRetryPredicate();
      expect(retry(0, undefined)).toBe(true);
    });

    it("returns true for a string error", () => {
      const retry = getRetryPredicate();
      // Some HTTP libraries throw raw strings; treat as transient.
      expect(retry(0, "network down")).toBe(true);
    });

    it("respects MAX_QUERY_RETRIES even for network errors (failureCount=2 -> false)", () => {
      const retry = getRetryPredicate();
      const networkError = new Error("connection lost");
      expect(retry(2, networkError)).toBe(false);
    });
  });

  describe("successful 2xx and informational responses (edge cases)", () => {
    // Note: The retry predicate is only invoked on FAILURES, so 2xx
    // responses normally never reach it. These tests document defensive
    // behaviour: only `400 <= status < 500` returns false; everything
    // else returns true (subject to MAX_QUERY_RETRIES). If an unusual
    // 2xx-shaped error somehow reaches the predicate, treating it as
    // transient is the safe default.

    it("returns true for status 200 (treated as transient since not 4xx)", () => {
      const retry = getRetryPredicate();
      expect(retry(0, { status: 200 })).toBe(true);
    });

    it("returns true for status 304 (Not Modified - 3xx, not 4xx)", () => {
      const retry = getRetryPredicate();
      expect(retry(0, { status: 304 })).toBe(true);
    });
  });
});
