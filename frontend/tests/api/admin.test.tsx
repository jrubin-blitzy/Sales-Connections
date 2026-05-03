/**
 * admin.test.tsx - Vitest tests for the admin API hooks.
 *
 * Targets `frontend/src/api/admin.ts`. Verifies key API hook
 * behaviors using vi.spyOn(global, "fetch") to control responses.
 *
 * Coverage:
 *   - adminKeys factory produces stable, hierarchical keys
 *   - useAdminUsersQuery hits GET /api/admin/users
 *   - useAdminRecordsQuery hits GET /api/admin/records
 *   - useAnalyticsQuery hits GET /api/admin/analytics
 *   - useUpdateUserRoleMutation hits PATCH /api/admin/users/:id
 *   - useHardDeleteRecordMutation hits DELETE /api/admin/records/:id
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

import {
  adminKeys,
  useAdminUsersQuery,
  useAdminRecordsQuery,
  useAnalyticsQuery,
  useUpdateUserRoleMutation,
  useHardDeleteRecordMutation,
} from "@/api/admin";
import { createTestQueryClient } from "../test-utils";

function makeWrapper(client: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

function makeJsonResponse(
  body: unknown,
  status: number = 200,
): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function makeEmptyResponse(status: number = 204): Response {
  return new Response(null, {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

// ---------------------------------------------------------------------------
// adminKeys factory
// ---------------------------------------------------------------------------

describe("adminKeys", () => {
  it("all key starts with 'admin'", () => {
    expect(adminKeys.all).toEqual(["admin"]);
  });

  it("users key extends all", () => {
    const key = adminKeys.users();
    expect(key[0]).toBe("admin");
    expect(key).toContain("users");
  });

  it("analytics key extends all", () => {
    const key = adminKeys.analytics();
    expect(key[0]).toBe("admin");
    expect(key).toContain("analytics");
  });

  it("recordsAll key returns parent without filters", () => {
    const key = adminKeys.recordsAll();
    expect(key[0]).toBe("admin");
    expect(key).toContain("records");
  });

  it("records key includes filter params", () => {
    const k1 = adminKeys.records({ include_deleted: false });
    const k2 = adminKeys.records({ include_deleted: true });
    expect(k1).not.toEqual(k2);
  });
});

// ---------------------------------------------------------------------------
// useAdminUsersQuery
// ---------------------------------------------------------------------------

describe("useAdminUsersQuery", () => {
  let client: QueryClient;
  let fetchSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    client = createTestQueryClient();
    fetchSpy = vi.spyOn(global, "fetch") as ReturnType<typeof vi.spyOn>;
  });

  afterEach(() => {
    vi.restoreAllMocks();
    client.clear();
  });

  it("calls GET /api/admin/users", async () => {
    fetchSpy.mockResolvedValue(makeJsonResponse([]));

    const { result } = renderHook(() => useAdminUsersQuery(), {
      wrapper: makeWrapper(client),
    });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    const url = String(fetchSpy.mock.calls[0]![0]);
    expect(url).toContain("/api/admin/users");
    const init = fetchSpy.mock.calls[0]![1] as RequestInit | undefined;
    expect(init?.credentials).toBe("include");
  });

  it("returns array of users", async () => {
    const users = [
      {
        id: "u1",
        email: "a@example.com",
        display_name: "Admin",
        role: "Admin",
        created_at: "2026-05-01T12:00:00Z",
      },
    ];
    fetchSpy.mockResolvedValue(makeJsonResponse(users));

    const { result } = renderHook(() => useAdminUsersQuery(), {
      wrapper: makeWrapper(client),
    });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    expect(Array.isArray(result.current.data)).toBe(true);
    expect(result.current.data?.length).toBe(1);
  });
});

// ---------------------------------------------------------------------------
// useAdminRecordsQuery
// ---------------------------------------------------------------------------

describe("useAdminRecordsQuery", () => {
  let client: QueryClient;
  let fetchSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    client = createTestQueryClient();
    fetchSpy = vi.spyOn(global, "fetch") as ReturnType<typeof vi.spyOn>;
  });

  afterEach(() => {
    vi.restoreAllMocks();
    client.clear();
  });

  it("calls GET /api/admin/records with include_deleted param", async () => {
    fetchSpy.mockResolvedValue(
      makeJsonResponse({ records: [], total: 0, page: 1, page_size: 25 }),
    );

    const { result } = renderHook(
      () => useAdminRecordsQuery({ include_deleted: true }),
      { wrapper: makeWrapper(client) },
    );

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    const url = String(fetchSpy.mock.calls[0]![0]);
    expect(url).toContain("/api/admin/records");
    expect(url).toContain("include_deleted=true");
  });

  it("encodes include_deleted=false explicitly", async () => {
    fetchSpy.mockResolvedValue(
      makeJsonResponse({ records: [], total: 0, page: 1, page_size: 25 }),
    );

    renderHook(
      () => useAdminRecordsQuery({ include_deleted: false }),
      { wrapper: makeWrapper(client) },
    );

    await waitFor(() => {
      expect(fetchSpy).toHaveBeenCalled();
    });
    const url = String(fetchSpy.mock.calls[0]![0]);
    expect(url).toContain("include_deleted=false");
  });
});

// ---------------------------------------------------------------------------
// useAnalyticsQuery
// ---------------------------------------------------------------------------

describe("useAnalyticsQuery", () => {
  let client: QueryClient;
  let fetchSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    client = createTestQueryClient();
    fetchSpy = vi.spyOn(global, "fetch") as ReturnType<typeof vi.spyOn>;
  });

  afterEach(() => {
    vi.restoreAllMocks();
    client.clear();
  });

  it("calls GET /api/admin/analytics", async () => {
    fetchSpy.mockResolvedValue(
      makeJsonResponse({
        most_active_contributors: [],
        leads_by_status: [],
        weekly_activity: [],
      }),
    );

    const { result } = renderHook(() => useAnalyticsQuery(), {
      wrapper: makeWrapper(client),
    });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    const url = String(fetchSpy.mock.calls[0]![0]);
    expect(url).toContain("/api/admin/analytics");
  });
});

// ---------------------------------------------------------------------------
// useUpdateUserRoleMutation
// ---------------------------------------------------------------------------

describe("useUpdateUserRoleMutation", () => {
  let client: QueryClient;
  let fetchSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    client = createTestQueryClient();
    fetchSpy = vi.spyOn(global, "fetch") as ReturnType<typeof vi.spyOn>;
  });

  afterEach(() => {
    vi.restoreAllMocks();
    client.clear();
  });

  it("PATCHes /api/admin/users/:id with role payload", async () => {
    fetchSpy.mockResolvedValue(
      makeJsonResponse({
        id: "u1",
        email: "a@example.com",
        display_name: "Admin",
        role: "Admin",
        created_at: "2026-05-01T12:00:00Z",
      }),
    );

    const { result } = renderHook(() => useUpdateUserRoleMutation(), {
      wrapper: makeWrapper(client),
    });

    result.current.mutate({
      userId: "u1",
      payload: { role: "Admin" },
    });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    const url = String(fetchSpy.mock.calls[0]![0]);
    expect(url).toContain("/api/admin/users/");
    expect(url).toContain("u1");
    const init = fetchSpy.mock.calls[0]![1] as RequestInit | undefined;
    expect(init?.method).toBe("PATCH");
    expect(init?.credentials).toBe("include");
  });

  it("URL-encodes user ids with special chars", async () => {
    fetchSpy.mockResolvedValue(
      makeJsonResponse({
        id: "u/1",
        email: "a@example.com",
        display_name: "Admin",
        role: "Admin",
        created_at: "2026-05-01T12:00:00Z",
      }),
    );

    const { result } = renderHook(() => useUpdateUserRoleMutation(), {
      wrapper: makeWrapper(client),
    });

    result.current.mutate({
      userId: "u/1",
      payload: { role: "Admin" },
    });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    const url = String(fetchSpy.mock.calls[0]![0]);
    // encodeURIComponent should escape '/' as %2F
    expect(url).toContain("u%2F1");
  });
});

// ---------------------------------------------------------------------------
// useHardDeleteRecordMutation
// ---------------------------------------------------------------------------

describe("useHardDeleteRecordMutation", () => {
  let client: QueryClient;
  let fetchSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    client = createTestQueryClient();
    fetchSpy = vi.spyOn(global, "fetch") as ReturnType<typeof vi.spyOn>;
  });

  afterEach(() => {
    vi.restoreAllMocks();
    client.clear();
  });

  it("DELETEs /api/admin/records/:id", async () => {
    fetchSpy.mockResolvedValue(makeEmptyResponse(204));

    const { result } = renderHook(() => useHardDeleteRecordMutation(), {
      wrapper: makeWrapper(client),
    });

    result.current.mutate({ recordId: "r1" });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    const url = String(fetchSpy.mock.calls[0]![0]);
    expect(url).toContain("/api/admin/records/");
    expect(url).toContain("r1");
    const init = fetchSpy.mock.calls[0]![1] as RequestInit | undefined;
    expect(init?.method).toBe("DELETE");
    expect(init?.credentials).toBe("include");
  });
});
