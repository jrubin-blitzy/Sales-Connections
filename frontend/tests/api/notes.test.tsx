/**
 * notes.test.tsx - Vitest tests for the F-002 notes API hooks.
 *
 * Targets `frontend/src/api/notes.ts`. Verifies:
 *   - useGenerateNotesMutation calls POST /api/notes/generate
 *   - isSoftAiFailure detects ai_timeout and ai_unavailable codes
 *   - retry: 0 (no automatic retry on AI errors)
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

import { useGenerateNotesMutation, isSoftAiFailure } from "@/api/notes";
import { createTestQueryClient } from "../test-utils";
import { ApiError } from "@/api/client";

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

describe("isSoftAiFailure", () => {
  it("returns true for ai_timeout error code", () => {
    const err = new ApiError(504, "ai_timeout", "timed out");
    expect(isSoftAiFailure(err)).toBe(true);
  });

  it("returns true for ai_unavailable error code", () => {
    const err = new ApiError(503, "ai_unavailable", "unavailable");
    expect(isSoftAiFailure(err)).toBe(true);
  });

  it("returns false for other error codes", () => {
    const err = new ApiError(422, "validation_failed", "fail");
    expect(isSoftAiFailure(err)).toBe(false);
  });

  it("returns false for null/undefined", () => {
    expect(isSoftAiFailure(null)).toBe(false);
    expect(isSoftAiFailure(undefined)).toBe(false);
  });
});

describe("useGenerateNotesMutation", () => {
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

  it("posts to /api/notes/generate and returns response", async () => {
    fetchSpy.mockResolvedValue(
      makeJsonResponse({
        ai_notes: "Generated notes.",
        model: "claude-sonnet-4-5",
        generated_at: "2026-05-01T12:00:00Z",
      }),
    );

    const { result } = renderHook(() => useGenerateNotesMutation(), {
      wrapper: makeWrapper(client),
    });

    result.current.mutate({
      relationship_context: "We met at a conference.",
    });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });
    expect(result.current.data?.ai_notes).toBe("Generated notes.");

    const url = String(fetchSpy.mock.calls[0]![0]);
    expect(url).toContain("/api/notes/generate");
    const init = fetchSpy.mock.calls[0]![1] as RequestInit | undefined;
    expect(init?.method).toBe("POST");
  });

  it("does NOT retry on ai_timeout error", async () => {
    fetchSpy.mockResolvedValue(
      makeJsonResponse(
        {
          error: {
            code: "ai_timeout",
            message: "AI request timed out.",
          },
        },
        504,
      ),
    );

    const { result } = renderHook(() => useGenerateNotesMutation(), {
      wrapper: makeWrapper(client),
    });

    result.current.mutate({ relationship_context: "test" });

    await waitFor(() => {
      expect(result.current.isError).toBe(true);
    });
    // retry: 0 means exactly ONE fetch call.
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });

  it("error is exposed as ApiError with ai_timeout code", async () => {
    fetchSpy.mockResolvedValue(
      makeJsonResponse(
        {
          error: {
            code: "ai_timeout",
            message: "AI request timed out.",
          },
        },
        504,
      ),
    );

    const { result } = renderHook(() => useGenerateNotesMutation(), {
      wrapper: makeWrapper(client),
    });

    result.current.mutate({ relationship_context: "test" });

    await waitFor(() => {
      expect(result.current.isError).toBe(true);
    });
    const err = result.current.error as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect(err.code).toBe("ai_timeout");
    expect(isSoftAiFailure(err)).toBe(true);
  });
});
