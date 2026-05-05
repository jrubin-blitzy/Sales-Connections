/**
 * queryClient.ts - Singleton TanStack Query 5.x client for the Sales-Connections SPA.
 *
 * Created once at module-load time and consumed by:
 *   - frontend/src/main.tsx - wraps the app in <QueryClientProvider client={queryClient}>
 *   - frontend/src/auth/AuthProvider.tsx - calls queryClient.clear() on logout
 *
 * Configuration rationale (per AAP Sec 0.2.3 and the frontend/src/lib folder spec):
 *
 * Queries:
 *   - staleTime: 60_000 ms (1 minute)
 *       Reduces unnecessary refetches during normal navigation. Connection
 *       records, tags, and admin lists rarely change second-to-second; one
 *       minute of cached data is a good UX/freshness compromise.
 *   - gcTime: 5 * 60_000 ms (5 minutes)
 *       Keeps cached responses around for 5 minutes after components unmount
 *       so navigation between pages feels instant.
 *   - refetchOnWindowFocus: false
 *       Disabled by default to avoid surprise network calls when users tab
 *       away and back. Specific queries (e.g., session check) may opt in
 *       via per-hook overrides.
 *   - refetchOnReconnect: true
 *       Left at the TanStack Query default. When the browser regains
 *       network connectivity after a disconnect, queries refetch - sensible
 *       because the Connection Feed and Status chip should reflect server
 *       state immediately on reconnect.
 *   - retry: custom predicate (shouldRetryQuery)
 *       Returns false for HTTP 4xx errors (no point retrying validation,
 *       auth, or not-found errors). Allows up to MAX_QUERY_RETRIES retries
 *       for 5xx errors and network failures, with TanStack Query's default
 *       exponential backoff between attempts.
 *   - throwOnError: false
 *       Hooks handle errors via their own onError callbacks routing to
 *       toast notifications. The React error boundary is reserved for
 *       genuine render-time crashes, not transient API failures.
 *
 * Mutations:
 *   - retry: 0
 *       Mutations never auto-retry. A failed POST/PATCH/DELETE may have
 *       partial side effects on the server, and silent retry could result
 *       in duplicate state changes or conflicting updates. The user
 *       explicitly retries via the UI.
 *   - throwOnError: false (inherits same rationale as queries)
 *
 * Singleton invariant:
 *   This module exports a SINGLE `queryClient` constant. Importing the file
 *   from multiple places (main.tsx, hooks, AuthProvider) returns the same
 *   instance because ES modules cache evaluation.
 *
 * TanStack Query 5.x note:
 *   v5 renamed the legacy v4 garbage-collection option to `gcTime` to
 *   clarify that it is not a max age but a "how long to keep around after
 *   the last component unmounts" threshold. This file uses the v5 name
 *   exclusively.
 */

import { QueryClient } from "@tanstack/react-query";

// ---------------------------------------------------------------------------
// Module-level configuration constants
// ---------------------------------------------------------------------------

/**
 * Maximum number of automatic retries for query failures (after the initial
 * attempt). With TanStack Query's default exponential backoff, attempt 1 is
 * immediate, attempt 2 waits ~1s, attempt 3 waits ~2s. Three total attempts
 * (initial + 2 retries) is a good balance between user-visible latency and
 * successful recovery from transient 5xx failures.
 */
const MAX_QUERY_RETRIES = 2;

/**
 * Stale-while-revalidate duration: how long after a successful fetch a
 * query is considered fresh enough to skip refetches on remount/focus.
 * 60 seconds matches the assigned-folder spec and the documented UX
 * trade-off in the AAP (Sec 0.2.3).
 */
const QUERY_STALE_TIME_MS = 60 * 1000;

/**
 * Garbage-collection time: how long unused query data stays in memory after
 * its last component unmounts. 5 minutes keeps navigation snappy while
 * bounding memory growth on long-lived sessions.
 */
const QUERY_GC_TIME_MS = 5 * 60 * 1000;

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

/**
 * Returns the numeric HTTP status code attached to the error, or null if
 * the error is not an HTTP-shaped object (e.g., a network failure with no
 * status). Duck-types on the structural `{ status: number }` shape rather
 * than depending on the api/client error class, which keeps this module
 * free of circular references against the consumer of the singleton.
 *
 * @param error - The error caught by TanStack Query (typed as `unknown`
 *                because v5's error parameters are unconstrained).
 * @returns     - The HTTP status integer when present, otherwise null.
 *
 * @internal
 */
function extractErrorStatus(error: unknown): number | null {
  if (
    error !== null &&
    typeof error === "object" &&
    "status" in error &&
    typeof (error as { status: unknown }).status === "number"
  ) {
    return (error as { status: number }).status;
  }
  return null;
}

/**
 * Custom retry predicate for queries.
 *
 * Returns false (do not retry) when the error is a deliberate client-side
 * 4xx response - retrying these wastes time, can mask bugs, and in the case
 * of 401 delays the /login redirect handled by frontend/src/api/client.ts:
 *   - 400 Bad Request (validation failure)
 *   - 401 Unauthorized (session expired; client.ts will redirect to /login)
 *   - 403 Forbidden (RBAC denial; user lacks role for this action)
 *   - 404 Not Found (record id was deleted or never existed)
 *   - 409 Conflict (e.g., duplicate detection collision)
 *   - 422 Unprocessable Entity (server-side validation failed)
 *
 * Returns true (retry) for up to MAX_QUERY_RETRIES attempts on:
 *   - 5xx server errors (transient backend failures)
 *   - Network errors (no `status` field on the error)
 *   - Timeouts
 *
 * @param failureCount - Number of times this query has already failed
 *                       (TanStack Query passes 1 on the first failure, 2 on
 *                       the second, etc.).
 * @param error        - The error that caused the failure.
 * @returns            - true to retry, false to stop.
 *
 * @internal
 */
function shouldRetryQuery(failureCount: number, error: unknown): boolean {
  // Stop retrying after the configured maximum has been reached.
  if (failureCount >= MAX_QUERY_RETRIES) {
    return false;
  }

  // Inspect the error for a `status` field. The api/client.ts wrapper
  // throws structured error instances that carry the HTTP status code;
  // here we duck-type the field rather than imposing a circular dependency.
  const status = extractErrorStatus(error);

  // Don't retry on any 4xx (deliberate client errors).
  if (status !== null && status >= 400 && status < 500) {
    return false;
  }

  // Otherwise, retry (5xx, network errors, timeouts).
  return true;
}

// ---------------------------------------------------------------------------
// Singleton QueryClient instance
// ---------------------------------------------------------------------------

/**
 * Singleton TanStack Query client. Instantiated once at module load.
 *
 * Imported by:
 *   - frontend/src/main.tsx - for <QueryClientProvider client={queryClient}>
 *   - frontend/src/auth/AuthProvider.tsx - calls queryClient.clear() on logout
 *
 * Most application code does NOT import this directly; instead, hooks
 * obtain the client from React context via useQueryClient() inside
 * components that are descendants of <QueryClientProvider>.
 */
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: QUERY_STALE_TIME_MS,
      gcTime: QUERY_GC_TIME_MS,
      refetchOnWindowFocus: false,
      refetchOnReconnect: true,
      retry: shouldRetryQuery,
      throwOnError: false,
    },
    mutations: {
      retry: 0,
      throwOnError: false,
    },
  },
});
