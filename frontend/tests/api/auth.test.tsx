/**
 * auth.test.tsx - Vitest tests for the F-012 auth API hooks.
 *
 * Targets `frontend/src/api/auth.ts`. Uses vi.spyOn(global, "fetch")
 * to intercept all HTTP calls (matching the pattern in client.test.ts).
 * Verifies:
 *   - authKeys factory produces stable query keys
 *   - useLoginMutation calls POST /auth/login
 *   - useLoginMutation surfaces 401 as ApiError
 *   - useLogoutMutation calls POST /auth/logout
 *   - useLogoutMutation clears query cache on success
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

import { authKeys, useLoginMutation, useLogoutMutation } from "@/api/auth";
import { createTestQueryClient } from "../test-utils";

// ---------------------------------------------------------------------------
// Test harness
// ---------------------------------------------------------------------------

function makeWrapper(client: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

function makeJsonResponse(
  body: unknown,
  status: number = 200,
  headers: Record<string, string> = {},
): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("authKeys", () => {
  it("session key is stable across calls", () => {
    expect(authKeys.session()).toEqual(authKeys.session());
  });

  it("session key starts with 'auth'", () => {
    const key = authKeys.session();
    expect(Array.isArray(key)).toBe(true);
    expect(key[0]).toBe("auth");
  });
});

describe("useLoginMutation", () => {
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

  it("posts to /auth/login and returns user on success", async () => {
    const mockUser = {
      id: "00000000-0000-0000-0000-000000000001",
      email: "test@example.com",
      display_name: "Test User",
      role: "Contributor" as const,
      created_at: "2026-01-01T00:00:00Z",
    };
    fetchSpy.mockResolvedValue(makeJsonResponse({ user: mockUser }));

    const { result } = renderHook(() => useLoginMutation(), {
      wrapper: makeWrapper(client),
    });

    result.current.mutate({
      email: "test@example.com",
      password: "password123",
    });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    expect(result.current.data?.user.email).toBe("test@example.com");

    // Verify fetch URL and method.
    expect(fetchSpy).toHaveBeenCalled();
    const callArgs = fetchSpy.mock.calls[0]!;
    const url = String(callArgs[0]);
    expect(url).toContain("/auth/login");
    const init = callArgs[1] as RequestInit | undefined;
    expect(init?.method).toBe("POST");
  });

  it("surfaces 401 as ApiError", async () => {
    fetchSpy.mockResolvedValue(
      makeJsonResponse(
        {
          error: {
            code: "unauthorized",
            message: "Authentication failed.",
          },
        },
        401,
      ),
    );

    const { result } = renderHook(() => useLoginMutation(), {
      wrapper: makeWrapper(client),
    });

    result.current.mutate({
      email: "wrong@example.com",
      password: "wrong",
    });

    await waitFor(() => {
      expect(result.current.isError).toBe(true);
    });
  });

  it("sends credentials: include for HttpOnly cookie", async () => {
    fetchSpy.mockResolvedValue(
      makeJsonResponse({
        user: {
          id: "00000000-0000-0000-0000-000000000001",
          email: "x@y.com",
          display_name: "x",
          role: "Contributor" as const,
          created_at: "2026-01-01T00:00:00Z",
        },
      }),
    );

    const { result } = renderHook(() => useLoginMutation(), {
      wrapper: makeWrapper(client),
    });
    result.current.mutate({ email: "x@y.com", password: "p" });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const init = fetchSpy.mock.calls[0]![1] as RequestInit | undefined;
    expect(init?.credentials).toBe("include");
  });
});

describe("useLogoutMutation", () => {
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

  it("posts to /auth/logout", async () => {
    fetchSpy.mockResolvedValue(makeJsonResponse({}));

    const { result } = renderHook(() => useLogoutMutation(), {
      wrapper: makeWrapper(client),
    });

    result.current.mutate();

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    const url = String(fetchSpy.mock.calls[0]![0]);
    expect(url).toContain("/auth/logout");
  });

  it("clears the query cache on success", async () => {
    fetchSpy.mockResolvedValue(makeJsonResponse({}));

    // Pre-populate cache.
    client.setQueryData(["auth", "session"], { stale: "data" });

    const { result } = renderHook(() => useLogoutMutation(), {
      wrapper: makeWrapper(client),
    });

    result.current.mutate();

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    // After logout, the cache should be cleared.
    const cached = client.getQueryData(["auth", "session"]);
    expect(cached).toBeUndefined();
  });
});
