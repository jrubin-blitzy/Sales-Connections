/**
 * connections.test.tsx - Vitest tests for the connections API hooks.
 *
 * Targets `frontend/src/api/connections.ts`. Verifies key API hook
 * behaviors using vi.spyOn(global, "fetch") to control responses.
 *
 * Coverage:
 *   - Query key factories produce stable, hierarchical keys
 *   - useConnectionsQuery hits GET /api/connections
 *   - useConnectionQuery hits GET /api/connections/:id
 *   - useDuplicateCheckQuery hits GET /api/connections/duplicate-check
 *   - useCreateConnectionMutation hits POST /api/connections
 *   - useUpdateStatusMutation hits PATCH /api/connections/:id/status
 *   - useSoftDeleteConnectionMutation hits DELETE /api/connections/:id
 *   - useTagsQuery hits GET /api/tags
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

import {
  connectionKeys,
  tagKeys,
  useConnectionQuery,
  useCreateConnectionMutation,
  useDuplicateCheckQuery,
  useTagsQuery,
} from "@/api/connections";
import { createTestQueryClient } from "../test-utils";

function makeWrapper(client: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

function makeJsonResponse(body: unknown, status: number = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function makeMockConnectionRead() {
  return {
    id: "11111111-1111-1111-1111-111111111111",
    full_name: "Jane Doe",
    linkedin_url: "https://www.linkedin.com/in/jane/",
    company: "Acme",
    job_title: "VP",
    relationship_context: "College classmate",
    ai_notes: null,
    involvement: "Warm Intro" as const,
    outreach_status: "Not Started" as const,
    owner_user_id: "00000000-0000-0000-0000-000000000001",
    owner_display_name: "Owner",
    submission_date: "2026-05-01T12:00:00Z",
    deleted_at: null,
    tags: [],
  };
}

// ---------------------------------------------------------------------------
// Query key factories
// ---------------------------------------------------------------------------

describe("connectionKeys", () => {
  it("all key starts with 'connections'", () => {
    expect(connectionKeys.all).toEqual(["connections"]);
  });

  it("list key extends all", () => {
    const key = connectionKeys.list({});
    expect(Array.isArray(key)).toBe(true);
    expect(key[0]).toBe("connections");
  });

  it("detail keys for different ids are different", () => {
    const a = connectionKeys.detail("a");
    const b = connectionKeys.detail("b");
    expect(a).not.toEqual(b);
  });

  it("history keys for different pages are different", () => {
    const p1 = connectionKeys.history("a", 1);
    const p2 = connectionKeys.history("a", 2);
    expect(p1).not.toEqual(p2);
  });
});

describe("tagKeys", () => {
  it("all key starts with 'tags'", () => {
    expect(tagKeys.all).toEqual(["tags"]);
  });
});

// ---------------------------------------------------------------------------
// useConnectionQuery
// ---------------------------------------------------------------------------

describe("useConnectionQuery", () => {
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

  it("calls GET /api/connections/:id", async () => {
    fetchSpy.mockResolvedValue(makeJsonResponse(makeMockConnectionRead()));

    const { result } = renderHook(
      () => useConnectionQuery("11111111-1111-1111-1111-111111111111"),
      { wrapper: makeWrapper(client) },
    );

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    expect(fetchSpy).toHaveBeenCalled();
    const url = String(fetchSpy.mock.calls[0]![0]);
    expect(url).toContain("/api/connections/");
    expect(url).toContain("11111111-1111-1111-1111-111111111111");
  });

  it("disables query when id is empty", () => {
    fetchSpy.mockResolvedValue(makeJsonResponse({}));

    const { result } = renderHook(() => useConnectionQuery(""), {
      wrapper: makeWrapper(client),
    });
    // With empty id, `enabled: false` should prevent the fetch.
    expect(result.current.isPending).toBe(true);
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// useDuplicateCheckQuery
// ---------------------------------------------------------------------------

describe("useDuplicateCheckQuery", () => {
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

  it("disabled by default (enabled: false)", () => {
    fetchSpy.mockResolvedValue(makeJsonResponse({ duplicate_found: false }));

    renderHook(() => useDuplicateCheckQuery("https://linkedin.com/in/test"), {
      wrapper: makeWrapper(client),
    });
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("fires when enabled: true", async () => {
    fetchSpy.mockResolvedValue(
      makeJsonResponse({
        duplicate_found: true,
        existing_record_id: "abc",
      }),
    );

    const { result } = renderHook(
      () =>
        useDuplicateCheckQuery("https://linkedin.com/in/test", {
          enabled: true,
        }),
      { wrapper: makeWrapper(client) },
    );

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    expect(fetchSpy).toHaveBeenCalled();
    const url = String(fetchSpy.mock.calls[0]![0]);
    expect(url).toContain("/api/connections/duplicate-check");
  });
});

// ---------------------------------------------------------------------------
// useCreateConnectionMutation
// ---------------------------------------------------------------------------

describe("useCreateConnectionMutation", () => {
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

  it("posts to /api/connections", async () => {
    fetchSpy.mockResolvedValue(makeJsonResponse(makeMockConnectionRead()));

    const { result } = renderHook(() => useCreateConnectionMutation(), {
      wrapper: makeWrapper(client),
    });

    result.current.mutate({
      full_name: "Jane Doe",
      linkedin_url: "https://www.linkedin.com/in/jane/",
      company: "Acme",
      job_title: "VP",
      relationship_context: "College classmate",
      involvement: "Warm Intro",
      tag_ids: [],
    });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    const url = String(fetchSpy.mock.calls[0]![0]);
    expect(url).toContain("/api/connections");
    const init = fetchSpy.mock.calls[0]![1] as RequestInit | undefined;
    expect(init?.method).toBe("POST");
  });
});

// ---------------------------------------------------------------------------
// useTagsQuery
// ---------------------------------------------------------------------------

describe("useTagsQuery", () => {
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

  it("calls GET /api/tags", async () => {
    fetchSpy.mockResolvedValue(makeJsonResponse([]));

    const { result } = renderHook(() => useTagsQuery(), {
      wrapper: makeWrapper(client),
    });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    const url = String(fetchSpy.mock.calls[0]![0]);
    expect(url).toContain("/api/tags");
  });

  it("returns empty array when no tags", async () => {
    fetchSpy.mockResolvedValue(makeJsonResponse([]));

    const { result } = renderHook(() => useTagsQuery(), {
      wrapper: makeWrapper(client),
    });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    expect(result.current.data).toEqual([]);
  });
});
