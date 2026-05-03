/**
 * admin.test.ts - Vitest tests for the F-014 Admin Panel API hooks.
 *
 * Tests `frontend/src/api/admin.ts`:
 *   - useAdminUsersQuery (returns plain array, not paginated envelope)
 *   - useAdminRecordsQuery (paginated, accepts include_deleted required flag)
 *   - useAnalyticsQuery (5-minute staleTime)
 *   - useUpdateUserRoleMutation (409 last-admin / 403 self-demotion / other)
 *   - useHardDeleteRecordMutation (204 No Content; cross-cache invalidation)
 *
 * Test infrastructure:
 *   - MSW server lifecycle managed in tests/setup.ts (beforeAll/afterEach/afterAll).
 *   - Per-test handler overrides via server.use(overrides.admin.*).
 *   - Toast spies via vi.hoisted + vi.mock (captures both `toast.error` and useToast().error).
 *   - Fresh QueryClient per test via createTestQueryClient() (retry: false).
 *
 * Coverage of the assigned-folder concerns:
 *   - Plain array vs paginated envelope distinction (admin users vs records).
 *   - 5-minute staleTime on analytics (verified by re-mounting hook within window).
 *   - Special 409/403 error code -> specific toast message mapping.
 *   - 204 No Content handling (apiDelete<void> returns undefined).
 *   - Cross-cache invalidation (3 caches: recordsAll, connectionKeys.all, analytics).
 *   - Cache key factory shape: adminKeys.users(), adminKeys.records(params),
 *     adminKeys.recordsAll(), adminKeys.analytics().
 *
 * NOTE: This is a `.ts` file (NOT `.tsx`), so JSX is forbidden. The
 * QueryClientProvider wrapper is constructed via React.createElement
 * mirroring the canonical pattern in tests/api/connections.test.ts and
 * tests/api/auth.test.ts.
 *
 * Conventions per AAP Sec 0.7.7 and frontend/.prettierrc.json:
 *   - Strict TypeScript; no `any` (relaxed in tests but avoided here).
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
// hoisted vi.mock factory bodies. Same pattern as tests/api/connections.test.ts
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
// admin.ts calls `useToast()` at the top of every mutation hook and
// stores the returned facade as `toast`. By replacing useToast() with
// a function returning toastSpies, every call to `toast.success(...)` /
// `toast.error(...)` in the hook dispatches into our spies. The `toast`
// named export is mocked too for safety even though admin.ts does not
// import it directly; __resetToastStoreForTesting and ToastContainer
// are stubbed to satisfy any other consumer that may indirectly load
// this module during testing (tests/setup.ts calls
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
  adminKeys,
  useAdminRecordsQuery,
  useAdminUsersQuery,
  useAnalyticsQuery,
  useHardDeleteRecordMutation,
  useUpdateUserRoleMutation,
  type AdminRecordsParams,
} from "@/api/admin";
import { connectionKeys } from "@/api/connections";

// ---------------------------------------------------------------------------
// Test-infrastructure imports.
// ---------------------------------------------------------------------------

import { createTestQueryClient } from "../test-utils";
import { server } from "../mocks/server";
import { overrides } from "../mocks/handlers";
import {
  makeAdminUserList,
  makeAnalyticsResponse,
  makeConnectionList,
  makePaginatedConnections,
  makeUserRead,
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
 * Build a fresh QueryClientProvider wrapper for one test. If `client`
 * is supplied, it is used as the provider's value; otherwise a new
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
// adminKeys cache key factory
// ---------------------------------------------------------------------------

describe("adminKeys cache key factory", () => {
  it("adminKeys.all is ['admin']", () => {
    expect(adminKeys.all).toEqual(["admin"]);
  });

  it("adminKeys.users() is ['admin', 'users']", () => {
    expect(adminKeys.users()).toEqual(["admin", "users"]);
  });

  it("adminKeys.records(params) is ['admin', 'records', params]", () => {
    const params: AdminRecordsParams = { include_deleted: true, page: 1 };
    expect(adminKeys.records(params)).toEqual(["admin", "records", params]);
  });

  it("adminKeys.recordsAll() is ['admin', 'records']", () => {
    expect(adminKeys.recordsAll()).toEqual(["admin", "records"]);
  });

  it("adminKeys.analytics() is ['admin', 'analytics']", () => {
    expect(adminKeys.analytics()).toEqual(["admin", "analytics"]);
  });

  it("adminKeys.records produces equal arrays for the same params", () => {
    const params: AdminRecordsParams = { include_deleted: false, page: 2, page_size: 25 };
    const a = adminKeys.records(params);
    const b = adminKeys.records(params);
    expect(a).toEqual(b);
  });

  it("adminKeys.users() differs from adminKeys.recordsAll()", () => {
    // Sanity: invalidating users() must NOT match records entries and
    // vice-versa; different second-segment values keep them in disjoint
    // namespaces in the TanStack Query partial-match rules.
    expect(adminKeys.users()).not.toEqual(adminKeys.recordsAll());
  });

  it("adminKeys.recordsAll() is the prefix of adminKeys.records(params)", () => {
    // The recordsAll() key is the parent under which every records()
    // entry nests; invalidating recordsAll() must match every records()
    // cache slice. The factory enforces this by deriving records() as
    // [...recordsAll(), params].
    const params: AdminRecordsParams = { include_deleted: true };
    const recordsKey = adminKeys.records(params);
    expect(recordsKey.slice(0, 2)).toEqual(adminKeys.recordsAll());
  });
});

// ---------------------------------------------------------------------------
// useAdminUsersQuery
// ---------------------------------------------------------------------------

describe("useAdminUsersQuery", () => {
  it("fetches GET /api/admin/users and returns a plain UserRead[] array", async () => {
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useAdminUsersQuery(), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    // The hook returns a plain array (NOT a paginated envelope).
    expect(Array.isArray(result.current.data)).toBe(true);
    expect(result.current.data?.length).toBeGreaterThan(0);
    // Each entry should look like a UserRead.
    expect(result.current.data?.[0]).toMatchObject({
      id: expect.any(String),
      email: expect.any(String),
      display_name: expect.any(String),
      role: expect.stringMatching(/^(Admin|Contributor|Viewer)$/),
      created_at: expect.any(String),
    });
  });

  it("uses cache key adminKeys.users()", async () => {
    const { wrapper, queryClient } = makeWrapper();
    const { result } = renderHook(() => useAdminUsersQuery(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    // The query data should be retrievable via the documented key.
    const cached = queryClient.getQueryData(adminKeys.users());
    expect(cached).toEqual(result.current.data);
  });

  it("default handler returns 5 entries matching makeAdminUserList shape", async () => {
    // Cross-check the default MSW handler shape against the data
    // factory. makeAdminUserList is referenced here to guarantee the
    // factory is imported and used; if the schema ever drifts, the
    // type system catches it at compile time.
    const expectedShape = makeAdminUserList(5);
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useAdminUsersQuery(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toHaveLength(expectedShape.length);
  });

  it("exposes 403 forbidden as an ApiError with status 403 and code 'forbidden'", async () => {
    server.use(overrides.admin.usersForbidden());
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useAdminUsersQuery(), { wrapper });

    await waitFor(() => expect(result.current.isError).toBe(true));

    const err = result.current.error;
    expect(err).toBeDefined();
    expect(err?.status).toBe(403);
    expect(err?.code).toBe("forbidden");
  });

  it("handles empty user list", async () => {
    server.use(overrides.admin.usersEmpty());
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useAdminUsersQuery(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// useAdminRecordsQuery
// ---------------------------------------------------------------------------

describe("useAdminRecordsQuery", () => {
  it("fetches GET /api/admin/records and returns PaginatedConnections", async () => {
    const params: AdminRecordsParams = { include_deleted: true };
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useAdminRecordsQuery(params), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    // PaginatedConnections envelope: items/total/limit/offset (NOT
    // page/page_size; those are reserved for the history endpoint).
    expect(result.current.data).toMatchObject({
      items: expect.any(Array),
      total: expect.any(Number),
      limit: expect.any(Number),
      offset: expect.any(Number),
    });
    expect(Array.isArray(result.current.data?.items)).toBe(true);
  });

  it("encodes filter, sort, and pagination params in the request URL", async () => {
    let capturedRequestUrl = "";
    server.events.on("request:start", ({ request }) => {
      if (request.url.includes("/api/admin/records")) {
        capturedRequestUrl = request.url;
      }
    });

    const params: AdminRecordsParams = {
      include_deleted: false,
      page: 2,
      page_size: 50,
      sort: "submission_date",
      sort_dir: "asc",
    };
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useAdminRecordsQuery(params), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(capturedRequestUrl).toContain("include_deleted=false");
    expect(capturedRequestUrl).toContain("page=2");
    expect(capturedRequestUrl).toContain("page_size=50");
    expect(capturedRequestUrl).toContain("sort=submission_date");
    expect(capturedRequestUrl).toContain("sort_dir=asc");
  });

  it("always serializes include_deleted=true on the wire when set true", async () => {
    let capturedRequestUrl = "";
    server.events.on("request:start", ({ request }) => {
      if (request.url.includes("/api/admin/records")) {
        capturedRequestUrl = request.url;
      }
    });

    const params: AdminRecordsParams = { include_deleted: true };
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useAdminRecordsQuery(params), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    // include_deleted is REQUIRED on AdminRecordsParams precisely so the
    // wire encoding is deterministic and cache keys distinct between
    // include_deleted=true and include_deleted=false.
    expect(capturedRequestUrl).toContain("include_deleted=true");
  });

  it("uses different cache keys for include_deleted=true vs include_deleted=false", () => {
    const trueParams: AdminRecordsParams = { include_deleted: true };
    const falseParams: AdminRecordsParams = { include_deleted: false };
    expect(adminKeys.records(trueParams)).not.toEqual(adminKeys.records(falseParams));
  });

  it("returns a custom records list when overridden via recordsCustom", async () => {
    const customItems = makeConnectionList(3);
    const customResponse = makePaginatedConnections(customItems, { total: 3 });
    server.use(overrides.admin.recordsCustom(customResponse));
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useAdminRecordsQuery({ include_deleted: true }), {
      wrapper,
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.items).toHaveLength(3);
    expect(result.current.data?.total).toBe(3);
  });

  it("encodes multi-valued involvement and tag_ids params as repeating keys", async () => {
    let capturedUrl = "";
    server.events.on("request:start", ({ request }) => {
      if (request.url.includes("/api/admin/records")) {
        capturedUrl = request.url;
      }
    });

    const params: AdminRecordsParams = {
      include_deleted: true,
      involvement: ["Warm Intro", "Soft Reference"],
      tag_ids: ["tag1", "tag2"],
    };
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useAdminRecordsQuery(params), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    // URLSearchParams.append produces repeating keys (Flask's
    // request.args.getlist parses them into a Python list).
    expect(capturedUrl.match(/involvement=/g) ?? []).toHaveLength(2);
    expect(capturedUrl.match(/tag_ids=/g) ?? []).toHaveLength(2);
  });
});

// ---------------------------------------------------------------------------
// useAnalyticsQuery
// ---------------------------------------------------------------------------

describe("useAnalyticsQuery", () => {
  it("fetches GET /api/admin/analytics and returns the AnalyticsResponse shape", async () => {
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useAnalyticsQuery(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(result.current.data).toMatchObject({
      most_active_contributors: expect.any(Array),
      leads_by_status: expect.any(Array),
      weekly_activity: expect.any(Array),
      generated_at: expect.any(String),
    });
  });

  it("includes all 4 OutreachStatus rows in leads_by_status", async () => {
    // makeAnalyticsResponse() builds exactly 4 leads_by_status entries
    // (one per OutreachStatus value: Not Started, In Progress,
    // Contacted, Closed). The hook surfaces the wire shape verbatim,
    // so the SPA can render a complete chart even when a status has
    // zero leads. The factory is referenced here to anchor the test
    // to the same fixture the default handler returns.
    const expectedShape = makeAnalyticsResponse();
    expect(expectedShape.leads_by_status).toHaveLength(4);

    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useAnalyticsQuery(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(result.current.data?.leads_by_status).toHaveLength(4);
  });

  it("uses cache key adminKeys.analytics()", async () => {
    const { wrapper, queryClient } = makeWrapper();
    const { result } = renderHook(() => useAnalyticsQuery(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const cached = queryClient.getQueryData(adminKeys.analytics());
    expect(cached).toEqual(result.current.data);
  });

  it("uses 5-minute staleTime: re-mounting within window does NOT re-fetch", async () => {
    let requestCount = 0;
    server.events.on("request:start", ({ request }) => {
      if (request.url.includes("/api/admin/analytics")) {
        requestCount += 1;
      }
    });

    // Use the SAME queryClient across both mount cycles so the cache
    // persists. createTestQueryClient defaults staleTime: 0 at the
    // QueryClient level, but the hook's per-query staleTime: 5 min
    // takes precedence.
    const { wrapper, queryClient } = makeWrapper();
    const { result, unmount } = renderHook(() => useAnalyticsQuery(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(requestCount).toBe(1);

    unmount();

    // Re-mount IMMEDIATELY using the SAME queryClient (so the cache
    // persists). With the 5-minute staleTime applied at the hook
    // level, the data is still fresh and no second fetch should fire.
    function sameClientWrapper({
      children,
    }: {
      children: ReactNode;
    }): ReturnType<typeof createElement> {
      return createElement(QueryClientProvider, { client: queryClient }, children);
    }
    const { result: result2 } = renderHook(() => useAnalyticsQuery(), {
      wrapper: sameClientWrapper,
    });
    // Should be immediately successful (from cache); no second fetch.
    await waitFor(() => expect(result2.current.isSuccess).toBe(true));
    expect(requestCount).toBe(1);
  });

  it("exposes 500 as ApiError with status 500", async () => {
    server.use(overrides.admin.analytics500());
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useAnalyticsQuery(), { wrapper });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.status).toBe(500);
  });
});

// ---------------------------------------------------------------------------
// useUpdateUserRoleMutation
// ---------------------------------------------------------------------------

describe("useUpdateUserRoleMutation", () => {
  it("PATCHes /api/admin/users/:id with { role } and returns the updated user", async () => {
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useUpdateUserRoleMutation(), { wrapper });

    const targetUserId = "00000001-0000-4000-8000-000000000042";
    result.current.mutate({ userId: targetUserId, payload: { role: "Admin" } });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toMatchObject({ id: targetUserId, role: "Admin" });
  });

  it("default handler honors the role from the request payload", async () => {
    // Cross-check that makeUserRead is invoked with the request's role
    // by the default handler. We ask for "Contributor"; the handler
    // returns a UserRead with role: Contributor (not the factory
    // default).
    const expectedDefaultRole = makeUserRead().role;
    // Sanity: factory default differs from requested role so the test
    // is meaningful.
    expect(expectedDefaultRole).not.toBe("Viewer");

    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useUpdateUserRoleMutation(), { wrapper });
    result.current.mutate({
      userId: "00000001-0000-4000-8000-000000000043",
      payload: { role: "Viewer" },
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.role).toBe("Viewer");
  });

  it("on success: invalidates adminKeys.users()", async () => {
    const { wrapper, queryClient } = makeWrapper();
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    const { result } = renderHook(() => useUpdateUserRoleMutation(), { wrapper });

    result.current.mutate({
      userId: "00000001-0000-4000-8000-000000000099",
      payload: { role: "Contributor" },
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    // The hook should call invalidateQueries with the users() key.
    // Use objectContaining so optional fields on InvalidateQueryFilters
    // (e.g., refetchType) do not break the assertion if the hook adds
    // them in a future iteration.
    expect(invalidateSpy).toHaveBeenCalledWith(
      expect.objectContaining({ queryKey: adminKeys.users() }),
    );
  });

  it("on success: fires toast.success", async () => {
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useUpdateUserRoleMutation(), { wrapper });
    result.current.mutate({
      userId: "00000001-0000-4000-8000-000000000100",
      payload: { role: "Admin" },
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(toastSpies.success).toHaveBeenCalled();
  });

  it("on 409 last-admin: fires toast.error with 'Cannot demote the last remaining Admin' message", async () => {
    const userId = "00000001-0000-4000-8000-000000000101";
    server.use(overrides.admin.roleUpdateLastAdmin(userId));
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useUpdateUserRoleMutation(), { wrapper });
    result.current.mutate({ userId, payload: { role: "Contributor" } });
    await waitFor(() => expect(result.current.isError).toBe(true));

    expect(result.current.error?.status).toBe(409);
    expect(toastSpies.error).toHaveBeenCalledWith(
      expect.stringContaining("Cannot demote the last remaining Admin"),
    );
  });

  it("on 403 self-demotion: fires toast.error with 'You cannot demote yourself' message", async () => {
    const userId = "00000001-0000-4000-8000-000000000102";
    server.use(overrides.admin.roleUpdateSelfDemotion(userId));
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useUpdateUserRoleMutation(), { wrapper });
    result.current.mutate({ userId, payload: { role: "Contributor" } });
    await waitFor(() => expect(result.current.isError).toBe(true));

    expect(result.current.error?.status).toBe(403);
    expect(toastSpies.error).toHaveBeenCalledWith(
      expect.stringContaining("You cannot demote yourself"),
    );
  });

  it("on other errors (500): fires generic toast.error and NOT the special-case messages", async () => {
    // For a non-special status code, we need an inline override since
    // overrides.admin.* does not include a generic 500 for the role
    // endpoint. Construct one via msw's http + HttpResponse primitives
    // so the test is self-contained and exercises the 500 branch in
    // the hook's onError handler.
    const userId = "00000001-0000-4000-8000-000000000103";
    const { http, HttpResponse } = await import("msw");
    server.use(
      http.patch(`/api/admin/users/${userId}`, () =>
        HttpResponse.json(
          {
            error: {
              code: "internal_error",
              message: "Database is on fire",
              correlation_id: "test-correlation-id",
              fields: [],
            },
          },
          { status: 500 },
        ),
      ),
    );

    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useUpdateUserRoleMutation(), { wrapper });
    result.current.mutate({ userId, payload: { role: "Admin" } });
    await waitFor(() => expect(result.current.isError).toBe(true));

    expect(result.current.error?.status).toBe(500);
    // Generic toast: should call error with EITHER the server message
    // OR a fallback "Failed to update role" string per the hook's
    // onError fallback branch.
    expect(toastSpies.error).toHaveBeenCalled();
    // It must NOT match the 409/403 specific patterns.
    const calledWith = (toastSpies.error.mock.calls[0]?.[0] ?? "") as string;
    expect(calledWith).not.toContain("Cannot demote the last remaining Admin");
    expect(calledWith).not.toContain("You cannot demote yourself");
  });

  it("does NOT invalidate caches when the mutation fails", async () => {
    const userId = "00000001-0000-4000-8000-000000000104";
    server.use(overrides.admin.roleUpdateLastAdmin(userId));
    const { wrapper, queryClient } = makeWrapper();
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    const { result } = renderHook(() => useUpdateUserRoleMutation(), { wrapper });

    result.current.mutate({ userId, payload: { role: "Contributor" } });
    await waitFor(() => expect(result.current.isError).toBe(true));

    // Cache invalidation lives in onSuccess; the failure path must NOT
    // call invalidateQueries because the underlying server state is
    // unchanged (the 409 was a guard rejection, not a partial commit).
    expect(invalidateSpy).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// useHardDeleteRecordMutation (CRITICAL: 204 handling + 3-cache invalidation)
// ---------------------------------------------------------------------------

describe("useHardDeleteRecordMutation", () => {
  it("DELETEs /api/admin/records/:id and resolves to undefined (204 No Content)", async () => {
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useHardDeleteRecordMutation(), { wrapper });

    const recordId = "636f6e6e-0000-4000-8000-000000000050";
    result.current.mutate({ recordId });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    // 204 No Content -> apiDelete<void> returns undefined (skips JSON
    // parse). The mutation's data field reflects the resolved value
    // verbatim, so it MUST be undefined, NOT null and NOT {}. Tests
    // must use toBeUndefined() rather than toBeNull() or toBe(null).
    expect(result.current.data).toBeUndefined();
  });

  it("on success: invalidates THREE caches (recordsAll, connectionKeys.all, analytics)", async () => {
    const { wrapper, queryClient } = makeWrapper();
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    const { result } = renderHook(() => useHardDeleteRecordMutation(), { wrapper });

    result.current.mutate({ recordId: "636f6e6e-0000-4000-8000-000000000051" });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    // 3 invalidations expected per AAP Sec 0.4.6 cross-cache invalidation:
    //   - adminKeys.recordsAll()  (admin moderation table)
    //   - connectionKeys.all       (public feed and detail caches)
    //   - adminKeys.analytics()    (counts/aggregates may have shifted)
    const queryKeys = invalidateSpy.mock.calls.map((call) => call[0]?.queryKey);
    expect(queryKeys).toEqual(
      expect.arrayContaining([adminKeys.recordsAll(), connectionKeys.all, adminKeys.analytics()]),
    );
    // Verify exactly 3 invalidations fired (no extras leaking).
    expect(invalidateSpy).toHaveBeenCalledTimes(3);
  });

  it("on success: invalidation queryKey for connectionKeys.all matches the cross-tier root", async () => {
    // Sanity: connectionKeys.all is the public feed/detail root; this
    // test isolates that single invalidation call so a regression that
    // accidentally narrows the key (e.g., to connectionKeys.lists())
    // would surface clearly.
    const { wrapper, queryClient } = makeWrapper();
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    const { result } = renderHook(() => useHardDeleteRecordMutation(), { wrapper });

    result.current.mutate({ recordId: "636f6e6e-0000-4000-8000-000000000055" });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(invalidateSpy).toHaveBeenCalledWith(
      expect.objectContaining({ queryKey: connectionKeys.all }),
    );
  });

  it("on success: fires toast.success", async () => {
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useHardDeleteRecordMutation(), { wrapper });
    result.current.mutate({ recordId: "636f6e6e-0000-4000-8000-000000000052" });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(toastSpies.success).toHaveBeenCalled();
  });

  it("on 403 forbidden: surfaces ApiError with status 403 and fires toast.error", async () => {
    const recordId = "636f6e6e-0000-4000-8000-000000000053";
    server.use(overrides.admin.hardDeleteForbidden(recordId));
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useHardDeleteRecordMutation(), { wrapper });
    result.current.mutate({ recordId });
    await waitFor(() => expect(result.current.isError).toBe(true));

    expect(result.current.error?.status).toBe(403);
    expect(toastSpies.error).toHaveBeenCalled();
  });

  it("on 500: surfaces ApiError with status 500 and fires toast.error", async () => {
    const recordId = "636f6e6e-0000-4000-8000-000000000054";
    server.use(overrides.admin.hardDelete500(recordId));
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useHardDeleteRecordMutation(), { wrapper });
    result.current.mutate({ recordId });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.status).toBe(500);
    expect(toastSpies.error).toHaveBeenCalled();
  });

  it("does NOT invalidate caches when the mutation fails", async () => {
    const recordId = "636f6e6e-0000-4000-8000-000000000056";
    server.use(overrides.admin.hardDeleteForbidden(recordId));
    const { wrapper, queryClient } = makeWrapper();
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    const { result } = renderHook(() => useHardDeleteRecordMutation(), { wrapper });

    result.current.mutate({ recordId });
    await waitFor(() => expect(result.current.isError).toBe(true));

    // Failure path must NOT invalidate caches because the server
    // state is unchanged (the 403 was a guard rejection, not a
    // partial commit).
    expect(invalidateSpy).not.toHaveBeenCalled();
  });
});
