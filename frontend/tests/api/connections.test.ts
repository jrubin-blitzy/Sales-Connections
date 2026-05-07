/**
 * connections.test.ts - Vitest tests for the Connection + Tag API hooks.
 *
 * Tests `frontend/src/api/connections.ts`:
 *   Connection queries:
 *     - useConnectionsQuery(params)
 *     - useConnectionQuery(id)
 *     - useConnectionHistoryQuery(id, page, pageSize)
 *     - useDuplicateCheckQuery(url, options)
 *   Connection mutations:
 *     - useCreateConnectionMutation
 *     - useUpdateConnectionMutation
 *     - useUpdateStatusMutation (optimistic update with rollback)
 *     - useSoftDeleteConnectionMutation
 *   Tag hooks (co-located per AAP Sec 0.4.3):
 *     - useTagsQuery
 *     - useCreateTagMutation
 *   Cache key factories: connectionKeys, tagKeys.
 *
 * Test infrastructure:
 *   - MSW server lifecycle managed in tests/setup.ts.
 *   - Per-test handler overrides via server.use(overrides.connections.*).
 *   - Toast spies via vi.hoisted + vi.mock.
 *   - Fresh QueryClient per test (retry: false).
 *
 * Coverage of the assigned-folder concerns:
 *   - Cache key factory shapes (connectionKeys.all/lists/list/details/detail/
 *     history/duplicateCheck; tagKeys.all/lists).
 *   - Query key includes params (verified via the query cache).
 *   - useConnectionQuery and useConnectionHistoryQuery disabled when id
 *     is empty (no fetch fires).
 *   - useDuplicateCheckQuery disabled when enabled:false OR url is empty.
 *   - Optimistic update for status mutation: full cycle with rollback
 *     on error.
 *   - Cache invalidation after mutations (lists, detail).
 *   - Toast feedback (success/error) for every mutation.
 *   - Soft-delete returns 200 with ConnectionRead (NOT 204; deleted_at
 *     populated).
 *   - Multi-valued query params encode as repeating keys.
 *
 * NOTE: This is a `.ts` file (NOT `.tsx`), so JSX is forbidden. The
 * QueryClientProvider wrapper is constructed via React.createElement
 * mirroring the canonical pattern in tests/api/auth.test.ts and
 * tests/api/notes.test.ts.
 *
 * Conventions per AAP Sec 0.7.7 and frontend/.prettierrc.json:
 *   - Strict TypeScript; no `any` (relaxed in tests, but avoided).
 *   - Double quotes (singleQuote: false); trailing commas; 2-space
 *     indent; line length <= 100.
 *   - No emoji; no console.log.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement, type ReactNode } from "react";

// ---------------------------------------------------------------------------
// vi.hoisted spies (must be declared BEFORE any vi.mock factories).
//
// vi.mock factories cannot reference top-level `const` bindings because
// vi.mock is hoisted to the top of the file at compile time. vi.hoisted
// gives us a place to declare spies whose references are visible to the
// hoisted vi.mock factory bodies. Same pattern as tests/api/auth.test.ts
// and tests/api/notes.test.ts (the canonical references for this codebase).
// ---------------------------------------------------------------------------

const { toastSpies } = vi.hoisted(() => ({
  toastSpies: {
    success: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
    warning: vi.fn(),
    show: vi.fn().mockReturnValue("test-toast-id"),
    dismiss: vi.fn(),
  },
}));

// ---------------------------------------------------------------------------
// vi.mock - replace the Toast module with hoisted spies.
//
// connections.ts calls `useToast()` at the top of every mutation hook
// and stores the returned facade as `toast`. By replacing useToast() with
// a function returning toastSpies, every call to `toast.success(...)` /
// `toast.error(...)` in the hook dispatches into our spies. The `toast`
// named export is mocked too for future-compatibility even though
// connections.ts does not import it directly; __resetToastStoreForTesting
// and ToastContainer are stubbed to satisfy any other consumer that may
// indirectly load this module during testing (tests/setup.ts calls
// __resetToastStoreForTesting in afterEach).
// ---------------------------------------------------------------------------

vi.mock("@/components/ui/Toast", () => ({
  toast: toastSpies,
  useToast: () => toastSpies,
  __resetToastStoreForTesting: vi.fn(),
  ToastContainer: () => null,
}));

// ---------------------------------------------------------------------------
// Module under test (loaded AFTER vi.mock so the hoisted mock resolves).
// ---------------------------------------------------------------------------

import {
  connectionKeys,
  tagKeys,
  useConnectionHistoryQuery,
  useConnectionQuery,
  useConnectionsQuery,
  useCreateConnectionMutation,
  useCreateTagMutation,
  useDuplicateCheckQuery,
  useSoftDeleteConnectionMutation,
  useTagsQuery,
  useUpdateConnectionMutation,
  useUpdateStatusMutation,
  type ConnectionListParams,
} from "@/api/connections";
import type { ConnectionRead } from "@/schemas/connection";

// ---------------------------------------------------------------------------
// Test-infrastructure imports.
// ---------------------------------------------------------------------------

import { createTestQueryClient } from "../test-utils";
import { server } from "../mocks/server";
import { overrides } from "../mocks/handlers";
import {
  makeConnectionList,
  makeConnectionRead,
  makeDuplicateCheckResponse,
  makeHistoryEntry,
  makePaginatedConnections,
  makePaginatedHistory,
  makeTagRead,
} from "../mocks/data";

// ---------------------------------------------------------------------------
// Wrapper helper - QueryClientProvider via React.createElement (no JSX).
//
// renderHook's `options.wrapper` field requires a component that accepts
// `{ children }` and returns a JSX-compatible element. We build that
// element with React.createElement so the file remains a `.ts` file
// (per the assigned filename) while still producing a valid React
// element tree. The wrapper exposes the `queryClient` reference so tests
// can `vi.spyOn(queryClient, "invalidateQueries")` to verify the
// surgical invalidation behavior of mutation success-paths.
// ---------------------------------------------------------------------------

interface WrapperBundle {
  /** Component used as renderHook's options.wrapper. */
  wrapper: (props: { children: ReactNode }) => ReturnType<typeof createElement>;
  /** The QueryClient supplied to QueryClientProvider; spied on by tests. */
  queryClient: QueryClient;
}

/**
 * Build a fresh QueryClientProvider wrapper for one test. If `client` is
 * supplied, it is used as the provider's value; otherwise a new
 * `createTestQueryClient()` is constructed and returned in the bundle.
 *
 * The returned `wrapper` is a function component (not a class) so it
 * mounts cleanly inside renderHook's enabled-by-default React 19 root.
 * createTestQueryClient defaults disable retries/refetches at the
 * QueryClient level so each test resolves deterministically without
 * exponential-backoff delays or focus-driven refetches polluting the
 * MSW request log.
 *
 * @param client - Optional caller-supplied QueryClient.
 * @returns        Bundle of `{ wrapper, queryClient }`.
 */
function makeWrapper(client?: QueryClient): WrapperBundle {
  const queryClient = client ?? createTestQueryClient();
  function wrapper({ children }: { children: ReactNode }): ReturnType<typeof createElement> {
    return createElement(QueryClientProvider, { client: queryClient }, children);
  }
  return { wrapper, queryClient };
}

// ---------------------------------------------------------------------------
// Per-test cleanup hooks.
//
// beforeEach: Clear all spy mock state so call counts are isolated per
// test. Without this, an assertion like
// `expect(toastSpies.success).toHaveBeenCalled()` would observe
// invocations from prior tests within the same file (vi.fn() persists
// across tests because it is created once at vi.hoisted time).
//
// afterEach: Detach any per-test MSW request listeners registered via
// `server.events.on(...)`. The MSW server is a long-lived singleton in
// the test process (started once in setup.ts beforeAll) and its event
// emitter accumulates listeners across tests if not cleared. Without
// this teardown, a "request:start" listener installed in test A would
// continue to fire during test B and leak request counts/captures.
// ---------------------------------------------------------------------------

beforeEach(() => {
  toastSpies.success.mockClear();
  toastSpies.error.mockClear();
  toastSpies.info.mockClear();
  toastSpies.warning.mockClear();
  toastSpies.show.mockClear();
  toastSpies.dismiss.mockClear();
});

afterEach(() => {
  server.events.removeAllListeners();
});

// ---------------------------------------------------------------------------
// connectionKeys cache key factory
// ---------------------------------------------------------------------------

describe("connectionKeys cache key factory", () => {
  it("connectionKeys.all is ['connections']", () => {
    expect(connectionKeys.all).toEqual(["connections"]);
  });

  it("connectionKeys.lists() is ['connections', 'list']", () => {
    expect(connectionKeys.lists()).toEqual(["connections", "list"]);
  });

  it("connectionKeys.list(params) is ['connections', 'list', params]", () => {
    const params: ConnectionListParams = { page: 1, sort: "submission_date" };
    expect(connectionKeys.list(params)).toEqual(["connections", "list", params]);
  });

  it("connectionKeys.details() is ['connections', 'detail']", () => {
    expect(connectionKeys.details()).toEqual(["connections", "detail"]);
  });

  it("connectionKeys.detail(id) is ['connections', 'detail', id]", () => {
    const id = "636f6e6e-0000-4000-8000-000000000001";
    expect(connectionKeys.detail(id)).toEqual(["connections", "detail", id]);
  });

  it("connectionKeys.history(id, page) nests detail+'history'+page", () => {
    const id = "636f6e6e-0000-4000-8000-000000000002";
    expect(connectionKeys.history(id, 2)).toEqual(["connections", "detail", id, "history", 2]);
  });

  it("connectionKeys.duplicateCheck(url, excludeId) includes both args", () => {
    const url = "https://www.linkedin.com/in/test";
    const excludeId = "636f6e6e-0000-4000-8000-000000000003";
    expect(connectionKeys.duplicateCheck(url, excludeId)).toEqual([
      "connections",
      "duplicateCheck",
      url,
      excludeId,
    ]);
  });

  it("connectionKeys.duplicateCheck(url, undefined) replaces excludeId with null", () => {
    const url = "https://www.linkedin.com/in/test2";
    expect(connectionKeys.duplicateCheck(url, undefined)).toEqual([
      "connections",
      "duplicateCheck",
      url,
      null,
    ]);
  });

  it("connectionKeys.lists() differs from connectionKeys.details()", () => {
    // Sanity: invalidating lists() must NOT match detail() entries and
    // vice-versa; different second-segment values keep them in disjoint
    // namespaces in the TanStack Query partial-match rules.
    expect(connectionKeys.lists()).not.toEqual(connectionKeys.details());
  });

  it("connectionKeys.detail values for different ids are not equal", () => {
    const a = connectionKeys.detail("636f6e6e-0000-4000-8000-000000000004");
    const b = connectionKeys.detail("636f6e6e-0000-4000-8000-000000000005");
    expect(a).not.toEqual(b);
  });

  it("connectionKeys.history values for different pages are not equal", () => {
    const id = "636f6e6e-0000-4000-8000-000000000006";
    expect(connectionKeys.history(id, 1)).not.toEqual(connectionKeys.history(id, 2));
  });
});

// ---------------------------------------------------------------------------
// tagKeys cache key factory
// ---------------------------------------------------------------------------

describe("tagKeys cache key factory", () => {
  it("tagKeys.all is ['tags']", () => {
    expect(tagKeys.all).toEqual(["tags"]);
  });

  it("tagKeys.lists() is ['tags', 'list']", () => {
    expect(tagKeys.lists()).toEqual(["tags", "list"]);
  });
});

// ---------------------------------------------------------------------------
// useConnectionsQuery
// ---------------------------------------------------------------------------

describe("useConnectionsQuery", () => {
  it("fetches GET /api/connections and returns paginated envelope", async () => {
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useConnectionsQuery(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(result.current.data).toMatchObject({
      items: expect.any(Array),
      total: expect.any(Number),
      limit: expect.any(Number),
      offset: expect.any(Number),
    });
  });

  it("cache key includes the params object so the cache entry is keyed on filters", async () => {
    const params: ConnectionListParams = {
      involvement: ["Warm Intro"],
      page: 2,
    };
    const { wrapper, queryClient } = makeWrapper();
    const { result } = renderHook(() => useConnectionsQuery(params), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    // The cache key MUST include the params object verbatim. If the hook
    // ever loses params from the cache key, two different filter
    // combinations would share the same cache slot and clobber each
    // other's data - this assertion guards against that regression.
    const cached = queryClient.getQueryData(connectionKeys.list(params));
    expect(cached).toEqual(result.current.data);
  });

  it("encodes multi-valued involvement and tag_ids params as repeating keys", async () => {
    let capturedUrl = "";
    server.events.on("request:start", ({ request }) => {
      // Filter to the list endpoint - skip the duplicate-check and
      // history endpoints which share the /api/connections prefix.
      if (
        request.url.includes("/api/connections") &&
        !request.url.includes("duplicate-check") &&
        !request.url.includes("/history")
      ) {
        capturedUrl = request.url;
      }
    });

    const { wrapper } = makeWrapper();
    const { result } = renderHook(
      () =>
        useConnectionsQuery({
          involvement: ["Warm Intro", "Soft Reference"],
          tag_ids: ["tag1", "tag2"],
        }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    // URLSearchParams.append produces repeating keys (Flask's
    // request.args.getlist parses them into a Python list).
    expect(capturedUrl.match(/involvement=/g) ?? []).toHaveLength(2);
    expect(capturedUrl.match(/tag_ids=/g) ?? []).toHaveLength(2);
  });

  it("exposes 500 as ApiError with status 500", async () => {
    server.use(overrides.connections.list500());
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useConnectionsQuery(), { wrapper });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.status).toBe(500);
  });

  it("handles empty list response with total=0", async () => {
    server.use(overrides.connections.listEmpty());
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useConnectionsQuery(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.items).toEqual([]);
    expect(result.current.data?.total).toBe(0);
  });

  it("default handler returns 10 items matching makeConnectionList(10) shape", async () => {
    // Cross-check the default MSW handler shape against the data factory.
    // makeConnectionList is referenced here to guarantee the factory is
    // imported and used; if the schema ever drifts, the type system
    // catches it at compile time.
    const expectedLength = makeConnectionList(10).length;
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useConnectionsQuery(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.items).toHaveLength(expectedLength);
  });

  it("custom override returning makePaginatedConnections preserves the envelope", async () => {
    // Pin the response to a deterministic envelope built via the data
    // factory so we can assert exact field counts and total values. The
    // factory produces a real PaginatedConnections shape, which the
    // hook surfaces unchanged.
    const fixedItems = makeConnectionList(3);
    const envelope = makePaginatedConnections(fixedItems, { total: 42, limit: 25, offset: 0 });

    const { http, HttpResponse } = await import("msw");
    server.use(http.get("/api/connections", () => HttpResponse.json(envelope)));

    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useConnectionsQuery(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(result.current.data?.total).toBe(42);
    expect(result.current.data?.limit).toBe(25);
    expect(result.current.data?.items).toHaveLength(3);
  });
});

// ---------------------------------------------------------------------------
// useConnectionQuery
// ---------------------------------------------------------------------------

describe("useConnectionQuery", () => {
  it("fetches GET /api/connections/:id and returns ConnectionRead", async () => {
    const id = "636f6e6e-0000-4000-8000-000000000010";
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useConnectionQuery(id), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toMatchObject({ id });
  });

  it("is disabled when id is an empty string (no fetch fires)", async () => {
    let requestCount = 0;
    server.events.on("request:start", ({ request }) => {
      if (request.url.includes("/api/connections/")) {
        requestCount += 1;
      }
    });

    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useConnectionQuery(""), { wrapper });

    // Wait briefly to ensure no request fires. The query is disabled
    // via `enabled: id.length > 0` in the hook body.
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(requestCount).toBe(0);
    expect(result.current.isFetching).toBe(false);
    expect(result.current.data).toBeUndefined();
  });

  it("exposes 404 as ApiError with status 404", async () => {
    const id = "636f6e6e-0000-4000-8000-000000000011";
    server.use(overrides.connections.detail404(id));
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useConnectionQuery(id), { wrapper });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.status).toBe(404);
  });

  it("exposes 403 as ApiError with status 403", async () => {
    const id = "636f6e6e-0000-4000-8000-000000000012";
    server.use(overrides.connections.detail403(id));
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useConnectionQuery(id), { wrapper });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.status).toBe(403);
  });
});

// ---------------------------------------------------------------------------
// useConnectionHistoryQuery
// ---------------------------------------------------------------------------

describe("useConnectionHistoryQuery", () => {
  it("fetches GET /api/connections/:id/history with explicit page and page_size", async () => {
    let capturedUrl = "";
    server.events.on("request:start", ({ request }) => {
      if (request.url.includes("/history")) {
        capturedUrl = request.url;
      }
    });

    const id = "636f6e6e-0000-4000-8000-000000000020";
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useConnectionHistoryQuery(id, 2, 50), {
      wrapper,
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(capturedUrl).toContain("page=2");
    expect(capturedUrl).toContain("page_size=50");
  });

  it("returns the PaginatedHistory envelope shape with page/page_size (NOT limit/offset)", async () => {
    // The history endpoint returns page/page_size pagination, distinct
    // from the connections-list endpoint's limit/offset envelope. This
    // assertion guards against a regression where the hook accidentally
    // reuses PaginatedConnections.
    const id = "636f6e6e-0000-4000-8000-000000000021";
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useConnectionHistoryQuery(id), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(result.current.data).toMatchObject({
      items: expect.any(Array),
      total: expect.any(Number),
      page: expect.any(Number),
      page_size: expect.any(Number),
    });
    // Belt-and-suspenders: confirm the limit/offset keys are NOT present.
    expect(result.current.data).not.toHaveProperty("limit");
    expect(result.current.data).not.toHaveProperty("offset");
  });

  it("uses default page=1 and pageSize=25 when not specified", async () => {
    let capturedUrl = "";
    server.events.on("request:start", ({ request }) => {
      if (request.url.includes("/history")) {
        capturedUrl = request.url;
      }
    });

    const id = "636f6e6e-0000-4000-8000-000000000022";
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useConnectionHistoryQuery(id), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(capturedUrl).toContain("page=1");
    expect(capturedUrl).toContain("page_size=25");
  });

  it("is disabled when id is an empty string (no fetch fires)", async () => {
    let requestCount = 0;
    server.events.on("request:start", ({ request }) => {
      if (request.url.includes("/history")) {
        requestCount += 1;
      }
    });

    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useConnectionHistoryQuery(""), { wrapper });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(requestCount).toBe(0);
    expect(result.current.isFetching).toBe(false);
  });

  it("exposes 500 as ApiError with status 500", async () => {
    const id = "636f6e6e-0000-4000-8000-000000000023";
    server.use(overrides.connections.history500(id));
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useConnectionHistoryQuery(id), { wrapper });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.status).toBe(500);
  });

  it("handles empty history response", async () => {
    const id = "636f6e6e-0000-4000-8000-000000000024";
    server.use(overrides.connections.historyEmpty(id));
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useConnectionHistoryQuery(id), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.items).toEqual([]);
  });

  it("custom override returning makePaginatedHistory preserves the envelope", async () => {
    // Use the data factories to build a deterministic history payload.
    // makeHistoryEntry/makePaginatedHistory ensure the test exercises
    // the same shapes the rest of the suite uses for assertions on
    // EditHistoryFeed rendering.
    const id = "636f6e6e-0000-4000-8000-000000000025";
    const fixedItems = [makeHistoryEntry(), makeHistoryEntry()];
    const envelope = makePaginatedHistory(fixedItems, { total: 7, page: 1, page_size: 25 });

    const { http, HttpResponse } = await import("msw");
    server.use(http.get(`/api/connections/${id}/history`, () => HttpResponse.json(envelope)));

    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useConnectionHistoryQuery(id), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(result.current.data?.total).toBe(7);
    expect(result.current.data?.items).toHaveLength(2);
  });
});

// ---------------------------------------------------------------------------
// useDuplicateCheckQuery
// ---------------------------------------------------------------------------

describe("useDuplicateCheckQuery", () => {
  it("does NOT fire when enabled=false (regardless of url)", async () => {
    let requestCount = 0;
    server.events.on("request:start", ({ request }) => {
      if (request.url.includes("duplicate-check")) {
        requestCount += 1;
      }
    });

    const { wrapper } = makeWrapper();
    renderHook(() => useDuplicateCheckQuery("https://www.linkedin.com/in/x", { enabled: false }), {
      wrapper,
    });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(requestCount).toBe(0);
  });

  it("does NOT fire when enabled=true but url is empty", async () => {
    let requestCount = 0;
    server.events.on("request:start", ({ request }) => {
      if (request.url.includes("duplicate-check")) {
        requestCount += 1;
      }
    });

    const { wrapper } = makeWrapper();
    renderHook(() => useDuplicateCheckQuery("", { enabled: true }), { wrapper });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(requestCount).toBe(0);
  });

  it("fires when enabled=true and url is non-empty; returns not-duplicate by default", async () => {
    const { wrapper } = makeWrapper();
    const { result } = renderHook(
      () => useDuplicateCheckQuery("https://www.linkedin.com/in/test", { enabled: true }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.duplicate_found).toBe(false);
  });

  it("returns duplicate-found response when override is set", async () => {
    const existingId = "636f6e6e-0000-4000-8000-000000000099";
    server.use(overrides.duplicateCheck.duplicateFound(existingId, "Existing Owner"));
    const { wrapper } = makeWrapper();
    const { result } = renderHook(
      () => useDuplicateCheckQuery("https://www.linkedin.com/in/dup", { enabled: true }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.duplicate_found).toBe(true);
    expect(result.current.data?.existing_record_id).toBe(existingId);
    expect(result.current.data?.existing_owner_display_name).toBe("Existing Owner");
  });

  it("default response shape matches makeDuplicateCheckResponse() (no duplicate)", async () => {
    // Cross-check the default MSW handler against the factory's
    // not-duplicate defaults. makeDuplicateCheckResponse is the
    // canonical reference for "no duplicate found" payloads.
    const expected = makeDuplicateCheckResponse();
    expect(expected.duplicate_found).toBe(false);
    expect(expected.existing_record_id).toBeNull();

    const { wrapper } = makeWrapper();
    const { result } = renderHook(
      () => useDuplicateCheckQuery("https://www.linkedin.com/in/probe", { enabled: true }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.duplicate_found).toBe(false);
    expect(result.current.data?.existing_record_id).toBeNull();
  });

  it("cache key includes the linkedinUrl and excludeRecordId", () => {
    const url = "https://www.linkedin.com/in/test";
    const excludeId = "636f6e6e-0000-4000-8000-000000000050";
    expect(connectionKeys.duplicateCheck(url, excludeId)).toEqual([
      "connections",
      "duplicateCheck",
      url,
      excludeId,
    ]);
  });

  it("forwards exclude_record_id to the server when provided", async () => {
    let capturedUrl = "";
    server.events.on("request:start", ({ request }) => {
      if (request.url.includes("duplicate-check")) {
        capturedUrl = request.url;
      }
    });

    const excludeId = "636f6e6e-0000-4000-8000-000000000051";
    const { wrapper } = makeWrapper();
    const { result } = renderHook(
      () =>
        useDuplicateCheckQuery("https://www.linkedin.com/in/me", {
          enabled: true,
          excludeRecordId: excludeId,
        }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(capturedUrl).toContain("exclude_record_id=");
    expect(capturedUrl).toContain(excludeId);
  });

  it("exposes 500 as ApiError with status 500", async () => {
    server.use(overrides.duplicateCheck.error500());
    const { wrapper } = makeWrapper();
    const { result } = renderHook(
      () => useDuplicateCheckQuery("https://www.linkedin.com/in/err", { enabled: true }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.status).toBe(500);
  });
});

// ---------------------------------------------------------------------------
// useCreateConnectionMutation
// ---------------------------------------------------------------------------

describe("useCreateConnectionMutation", () => {
  it("POSTs /api/connections and returns the persisted ConnectionRead", async () => {
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useCreateConnectionMutation(), { wrapper });
    result.current.mutate({
      submitted_by: "Test User",
      full_name: "Jane Doe",
      linkedin_url: "https://www.linkedin.com/in/janedoe",
      company: "Acme",
      job_title: "CEO",
      relationship_context: "College friend",
      ai_notes: undefined,
      involvement: "Warm Intro",
      tag_ids: [],
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toMatchObject({
      full_name: "Jane Doe",
      involvement: "Warm Intro",
    });
  });

  it("on success: invalidates connectionKeys.lists()", async () => {
    const { wrapper, queryClient } = makeWrapper();
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    const { result } = renderHook(() => useCreateConnectionMutation(), { wrapper });
    result.current.mutate({
      submitted_by: "Test User",
      full_name: "Test",
      linkedin_url: "https://www.linkedin.com/in/test",
      company: "Test Co",
      job_title: "Test",
      relationship_context: "Test context",
      ai_notes: undefined,
      involvement: "Warm Intro",
      tag_ids: [],
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    // The hook should call invalidateQueries with the lists() key shape.
    // Use objectContaining so optional fields on InvalidateQueryFilters
    // (e.g., refetchType) do not break the assertion if the hook adds
    // them in a future iteration.
    expect(invalidateSpy).toHaveBeenCalledWith(
      expect.objectContaining({ queryKey: connectionKeys.lists() }),
    );
  });

  it("on success: fires toast.success", async () => {
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useCreateConnectionMutation(), { wrapper });
    result.current.mutate({
      submitted_by: "Test User",
      full_name: "Test",
      linkedin_url: "https://www.linkedin.com/in/x",
      company: "X Corp",
      job_title: "Engineer",
      relationship_context: "Met at conference",
      ai_notes: undefined,
      involvement: "Warm Intro",
      tag_ids: [],
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(toastSpies.success).toHaveBeenCalled();
  });

  it("on 422 validation error: surfaces ApiError with fields and fires toast.error", async () => {
    server.use(
      overrides.connections.create422([{ field: "linkedin_url", message: "Invalid URL format" }]),
    );
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useCreateConnectionMutation(), { wrapper });
    result.current.mutate({
      submitted_by: "Test User",
      full_name: "X",
      linkedin_url: "not-a-url",
      company: "X Corp",
      job_title: "Engineer",
      relationship_context: "Test context",
      ai_notes: undefined,
      involvement: "Warm Intro",
      tag_ids: [],
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.status).toBe(422);
    expect(result.current.error?.fields).toContainEqual(
      expect.objectContaining({ field: "linkedin_url" }),
    );
    expect(toastSpies.error).toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// useUpdateConnectionMutation
// ---------------------------------------------------------------------------

describe("useUpdateConnectionMutation", () => {
  it("PATCHes /api/connections/:id and returns updated ConnectionRead", async () => {
    const id = "636f6e6e-0000-4000-8000-000000000030";
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useUpdateConnectionMutation(), { wrapper });
    result.current.mutate({
      id,
      payload: { company: "New Company Name" },
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toMatchObject({ id, company: "New Company Name" });
  });

  it("on success: invalidates BOTH connectionKeys.lists() and connectionKeys.detail(id)", async () => {
    const id = "636f6e6e-0000-4000-8000-000000000031";
    const { wrapper, queryClient } = makeWrapper();
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    const { result } = renderHook(() => useUpdateConnectionMutation(), { wrapper });
    result.current.mutate({ id, payload: { company: "X Corp" } });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    // The hook should issue TWO separate invalidateQueries calls - one
    // for lists() (in case sort fields changed) and one for detail(id)
    // (so any open detail view refreshes). Verify both via two
    // independent toHaveBeenCalledWith assertions.
    expect(invalidateSpy).toHaveBeenCalledWith(
      expect.objectContaining({ queryKey: connectionKeys.lists() }),
    );
    expect(invalidateSpy).toHaveBeenCalledWith(
      expect.objectContaining({ queryKey: connectionKeys.detail(id) }),
    );
  });

  it("on success: fires toast.success", async () => {
    const id = "636f6e6e-0000-4000-8000-000000000032";
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useUpdateConnectionMutation(), { wrapper });
    result.current.mutate({ id, payload: { company: "Z Corp" } });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(toastSpies.success).toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// useUpdateStatusMutation (CRITICAL: optimistic update with rollback)
// ---------------------------------------------------------------------------

describe("useUpdateStatusMutation (optimistic update with rollback)", () => {
  it("PATCHes /api/connections/:id/status and returns updated ConnectionRead", async () => {
    const id = "636f6e6e-0000-4000-8000-000000000040";
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useUpdateStatusMutation(), { wrapper });
    result.current.mutate({
      id,
      payload: { outreach_status: "In Progress" },
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.outreach_status).toBe("In Progress");
  });

  it("on mutation start: applies optimistic update to detail cache before server responds", async () => {
    const id = "636f6e6e-0000-4000-8000-000000000041";

    // createTestQueryClient defaults to gcTime: 0 which can garbage-
    // collect a manually-seeded query (no observer) before onMutate
    // has a chance to read it via getQueryData. For this test we need
    // the seeded cache entry to survive long enough for the optimistic
    // path to be exercised, so we use a non-zero gcTime via the
    // createTestQueryClient overrides hatch.
    const client = createTestQueryClient({
      defaultOptions: { queries: { gcTime: 60_000 } },
    });
    const { wrapper, queryClient } = makeWrapper(client);

    // Pre-seed the detail cache with a known record so onMutate has
    // something to read from getQueryData. Without this seed,
    // onMutate's setQueryData branch is skipped (the optimistic
    // update is a no-op when there's no existing cache entry).
    const initialRecord: ConnectionRead = makeConnectionRead({
      id,
      outreach_status: "Not Started",
    });
    queryClient.setQueryData(connectionKeys.detail(id), initialRecord);

    const { result } = renderHook(() => useUpdateStatusMutation(), { wrapper });
    result.current.mutate({ id, payload: { outreach_status: "Contacted" } });

    // Wait for the optimistic value to land in the cache. This
    // happens synchronously inside onMutate via setQueryData, then
    // the mutationFn fires asynchronously. We use waitFor (rather
    // than reading the cache immediately) because onMutate is async
    // (it awaits cancelQueries).
    await waitFor(() => {
      const cached = queryClient.getQueryData<ConnectionRead>(connectionKeys.detail(id));
      expect(cached?.outreach_status).toBe("Contacted");
    });

    // Wait for the mutation to settle so subsequent tests are not
    // racing with an in-flight request.
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
  });

  it("on error: rolls back the detail cache to the pre-mutate snapshot", async () => {
    const id = "636f6e6e-0000-4000-8000-000000000042";
    server.use(overrides.connections.statusForbidden(id));

    // Same gcTime override as the optimistic-success case above so
    // the seeded query is not GC'd before onError's rollback path
    // runs; without this, onMutate's getQueryData would return
    // undefined and the rollback branch would no-op.
    const client = createTestQueryClient({
      defaultOptions: { queries: { gcTime: 60_000 } },
    });
    const { wrapper, queryClient } = makeWrapper(client);
    const initialRecord: ConnectionRead = makeConnectionRead({
      id,
      outreach_status: "Not Started",
    });
    queryClient.setQueryData(connectionKeys.detail(id), initialRecord);

    const { result } = renderHook(() => useUpdateStatusMutation(), { wrapper });
    result.current.mutate({ id, payload: { outreach_status: "In Progress" } });

    // Wait for the mutation to fail.
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.status).toBe(403);

    // After error AND onSettled invalidation, the cache must NOT be
    // showing the optimistic 'In Progress' value. The rollback path
    // setQueryData(detail(id), previousDetail) restores the snapshot,
    // so the cache should reflect the original 'Not Started' state
    // (or be empty after invalidation, but there's no observer here
    // so the data persists).
    await waitFor(() => {
      const cached = queryClient.getQueryData<ConnectionRead>(connectionKeys.detail(id));
      // Critical: the optimistic value was rolled back. The cache
      // either holds the original snapshot OR is undefined after
      // invalidation triggers a refetch attempt.
      expect(cached?.outreach_status === "In Progress").toBe(false);
    });
  });

  it("on error: fires toast.error", async () => {
    const id = "636f6e6e-0000-4000-8000-000000000043";
    server.use(overrides.connections.statusForbidden(id));
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useUpdateStatusMutation(), { wrapper });
    result.current.mutate({ id, payload: { outreach_status: "In Progress" } });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(toastSpies.error).toHaveBeenCalled();
  });

  it("on success: fires toast.success", async () => {
    const id = "636f6e6e-0000-4000-8000-000000000044";
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useUpdateStatusMutation(), { wrapper });
    result.current.mutate({ id, payload: { outreach_status: "Contacted" } });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(toastSpies.success).toHaveBeenCalled();
  });

  it("on settled: invalidates BOTH detail(id) and lists() caches", async () => {
    const id = "636f6e6e-0000-4000-8000-000000000045";
    const { wrapper, queryClient } = makeWrapper();
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    const { result } = renderHook(() => useUpdateStatusMutation(), { wrapper });
    result.current.mutate({ id, payload: { outreach_status: "Closed" } });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    // onSettled fires after success or failure; invalidates both
    // detail and lists so the cache converges to server state.
    expect(invalidateSpy).toHaveBeenCalledWith(
      expect.objectContaining({ queryKey: connectionKeys.detail(id) }),
    );
    expect(invalidateSpy).toHaveBeenCalledWith(
      expect.objectContaining({ queryKey: connectionKeys.lists() }),
    );
  });

  it("when no detail cache exists: skips optimistic write but still invalidates", async () => {
    // When the detail cache is empty (e.g., status mutation invoked
    // from a feed row without ever opening the detail page), the
    // optimistic write is gated behind `if (previousDetail)` so it
    // becomes a no-op. The mutation should still complete normally
    // and onSettled should still invalidate.
    const id = "636f6e6e-0000-4000-8000-000000000046";
    const { wrapper, queryClient } = makeWrapper();
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

    // Verify the cache is empty before the mutation runs.
    expect(queryClient.getQueryData(connectionKeys.detail(id))).toBeUndefined();

    const { result } = renderHook(() => useUpdateStatusMutation(), { wrapper });
    result.current.mutate({ id, payload: { outreach_status: "In Progress" } });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    // The onSettled invalidations always fire regardless of the
    // optimistic-write branch.
    expect(invalidateSpy).toHaveBeenCalledWith(
      expect.objectContaining({ queryKey: connectionKeys.detail(id) }),
    );
  });
});

// ---------------------------------------------------------------------------
// useSoftDeleteConnectionMutation
// ---------------------------------------------------------------------------

describe("useSoftDeleteConnectionMutation", () => {
  it("DELETEs /api/connections/:id; returns 200 with ConnectionRead (deleted_at set)", async () => {
    // Soft delete returns 200 with the soft-deleted record (deleted_at
    // populated to the timestamp of the delete). NOT 204 - that's the
    // hard-delete admin endpoint. The default MSW handler in
    // tests/mocks/handlers.ts sets deleted_at to a fixed timestamp.
    const id = "636f6e6e-0000-4000-8000-000000000060";
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useSoftDeleteConnectionMutation(), { wrapper });
    result.current.mutate({ id });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(result.current.data).toBeDefined();
    expect(result.current.data?.id).toBe(id);
    expect(result.current.data?.deleted_at).not.toBeNull();
  });

  it("on success: invalidates BOTH connectionKeys.lists() and connectionKeys.detail(id)", async () => {
    const id = "636f6e6e-0000-4000-8000-000000000061";
    const { wrapper, queryClient } = makeWrapper();
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    const { result } = renderHook(() => useSoftDeleteConnectionMutation(), { wrapper });
    result.current.mutate({ id });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(invalidateSpy).toHaveBeenCalledWith(
      expect.objectContaining({ queryKey: connectionKeys.lists() }),
    );
    expect(invalidateSpy).toHaveBeenCalledWith(
      expect.objectContaining({ queryKey: connectionKeys.detail(id) }),
    );
  });

  it("on success: fires toast.success", async () => {
    const id = "636f6e6e-0000-4000-8000-000000000062";
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useSoftDeleteConnectionMutation(), { wrapper });
    result.current.mutate({ id });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(toastSpies.success).toHaveBeenCalled();
  });

  it("on 403 forbidden: surfaces ApiError and fires toast.error", async () => {
    const id = "636f6e6e-0000-4000-8000-000000000063";
    server.use(overrides.connections.deleteForbidden(id));
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useSoftDeleteConnectionMutation(), { wrapper });
    result.current.mutate({ id });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.status).toBe(403);
    expect(toastSpies.error).toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// useTagsQuery
// ---------------------------------------------------------------------------

describe("useTagsQuery", () => {
  it("fetches GET /api/tags and returns a plain TagRead[] array (not paginated)", async () => {
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useTagsQuery(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    // The tag list endpoint returns a plain array, NOT a PaginatedConnections
    // envelope. Guard against a regression where the hook accidentally
    // expects paginated shape.
    expect(Array.isArray(result.current.data)).toBe(true);
    expect(result.current.data).not.toHaveProperty("items");
    expect(result.current.data?.length ?? 0).toBeGreaterThan(0);
    expect(result.current.data?.[0]).toMatchObject({
      id: expect.any(String),
      name: expect.any(String),
      created_at: expect.any(String),
    });
  });

  it("uses cache key tagKeys.lists() so the cache entry is keyed correctly", async () => {
    const { wrapper, queryClient } = makeWrapper();
    const { result } = renderHook(() => useTagsQuery(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const cached = queryClient.getQueryData(tagKeys.lists());
    expect(cached).toEqual(result.current.data);
  });

  it("handles empty tag list", async () => {
    server.use(overrides.tags.listEmpty());
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useTagsQuery(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual([]);
  });

  it("exposes 500 as ApiError with status 500", async () => {
    server.use(overrides.tags.list500());
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useTagsQuery(), { wrapper });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.status).toBe(500);
  });
});

// ---------------------------------------------------------------------------
// useCreateTagMutation
// ---------------------------------------------------------------------------

describe("useCreateTagMutation", () => {
  it("POSTs /api/tags and returns the new TagRead", async () => {
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useCreateTagMutation(), { wrapper });
    result.current.mutate({ name: "industry:saas" });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.name).toBe("industry:saas");
  });

  it("on success: invalidates tagKeys.lists()", async () => {
    const { wrapper, queryClient } = makeWrapper();
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    const { result } = renderHook(() => useCreateTagMutation(), { wrapper });
    result.current.mutate({ name: "use-case:lead-gen" });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(invalidateSpy).toHaveBeenCalledWith(
      expect.objectContaining({ queryKey: tagKeys.lists() }),
    );
  });

  it("on success: fires toast.success", async () => {
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useCreateTagMutation(), { wrapper });
    result.current.mutate({ name: "geography:emea" });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(toastSpies.success).toHaveBeenCalled();
  });

  it("handles idempotent 200 response (existing tag returned)", async () => {
    // The backend is idempotent on POST /api/tags: a unique-constraint
    // violation on (org_id, name) returns 200 with the existing tag
    // rather than raising an error. The hook treats this identically
    // to the 201 created path. makeTagRead is the canonical fixture
    // factory for the existing-tag payload.
    const existing = makeTagRead({ name: "pre-existing" });
    server.use(overrides.tags.createDuplicate(existing));
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useCreateTagMutation(), { wrapper });
    result.current.mutate({ name: "pre-existing" });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.id).toBe(existing.id);
    expect(result.current.data?.name).toBe("pre-existing");
  });
});
