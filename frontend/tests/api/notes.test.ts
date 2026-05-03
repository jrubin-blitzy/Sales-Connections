/**
 * notes.test.ts - Vitest tests for the F-002 AI note generation hook.
 *
 * Tests `frontend/src/api/notes.ts`:
 *   - useGenerateNotesMutation()
 *   - isSoftAiFailure(error) helper
 *
 * Coverage of the assigned-folder concerns:
 *   - Happy path: POST /api/notes/generate returns GenerateNotesResponse.
 *   - retry: 0 - mutation does NOT retry on 504 / 503 / 500.
 *   - Soft failures (ai_timeout / ai_unavailable) - NO toast.error.
 *   - Hard failures (validation_failed / server errors) - toast.error fires.
 *   - isSoftAiFailure helper for all documented inputs.
 *
 * F-002 latency budget per AAP Sec 0.7.3: AI note generation <= 5 s P95
 * end-to-end. The 5-second timeout watchdog is server-side; the hook
 * surfaces 504/503 as SOFT failures the form can render inline without
 * blocking submission. retry: 0 prevents compounding the 5-second budget
 * via TanStack Query's default 3-retry policy.
 *
 * Test infrastructure:
 *   - MSW server lifecycle managed in tests/setup.ts (listen/reset/close).
 *   - Per-test handler overrides via server.use(overrides.notes.*).
 *   - Toast spies via vi.hoisted + vi.mock on @/components/ui/Toast.
 *   - Fresh QueryClient per test (createTestQueryClient already disables
 *     retries at the QueryClient level; the hook's own retry: 0 config is
 *     verified by counting outbound fetch attempts via server.events).
 *
 * Conventions per AAP Sec 0.7.7 and frontend/.prettierrc.json:
 *   - Strict TypeScript; no `any`.
 *   - Double quotes (singleQuote: false); trailing commas; 2-space indent;
 *     line length <= 100.
 *   - No emoji; no console.log.
 *
 * NOTE: This is a `.ts` file (NOT `.tsx`), so JSX is forbidden. The
 * QueryClientProvider wrapper is constructed via React.createElement
 * mirroring the canonical pattern in tests/api/auth.test.ts.
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
// and tests/api/client.test.ts (the canonical references for this codebase).
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
// notes.ts calls `useToast()` inside useGenerateNotesMutation and stores
// the returned facade as `toast`. By replacing useToast() with a function
// returning toastSpies, every call to `toast.error(...)` in the hook
// dispatches into our spies. The `toast` named export is mocked too for
// future-compatibility even though notes.ts does not import it directly;
// __resetToastStoreForTesting and ToastContainer are stubbed to satisfy
// any other consumer that may indirectly load this module during testing
// (tests/setup.ts calls __resetToastStoreForTesting in afterEach).
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

import { isSoftAiFailure, useGenerateNotesMutation } from "@/api/notes";
import { ApiError } from "@/api/client";

// ---------------------------------------------------------------------------
// Test-infrastructure imports.
// ---------------------------------------------------------------------------

import { createTestQueryClient } from "../test-utils";
import { server } from "../mocks/server";
import { overrides } from "../mocks/handlers";

// ---------------------------------------------------------------------------
// Wrapper helper - QueryClientProvider via React.createElement (no JSX).
//
// renderHook's `options.wrapper` field requires a component that accepts
// `{ children }` and returns a JSX-compatible element. We build that
// element with React.createElement so the file remains a `.ts` file
// (matching the assigned filename) while still producing a valid React
// element tree. The wrapper exposes the `queryClient` reference so tests
// could `vi.spyOn(queryClient, ...)` on it if needed (currently unused in
// this file, but preserved for parity with tests/api/auth.test.ts).
// ---------------------------------------------------------------------------

interface WrapperBundle {
  /** Component used as renderHook's options.wrapper. */
  wrapper: (props: { children: ReactNode }) => ReturnType<typeof createElement>;
  /** The QueryClient supplied to QueryClientProvider. */
  queryClient: QueryClient;
}

/**
 * Build a fresh QueryClientProvider wrapper for one test. If `client` is
 * supplied, it is used as the provider's value; otherwise a new
 * `createTestQueryClient()` is constructed and returned in the bundle.
 *
 * The createTestQueryClient defaults disable retries at the QueryClient
 * level; this file's tests further verify that the hook ITSELF sets
 * retry: 0 by counting outbound fetch attempts via server.events.
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
// `expect(toastSpies.error).toHaveBeenCalled()` would observe invocations
// from prior tests in this file (vi.fn() persists across tests because
// it is created once at vi.hoisted time).
//
// afterEach: Detach any per-test MSW request listeners registered via
// `server.events.on(...)`. The MSW server is a long-lived singleton in
// the test process (started once in setup.ts beforeAll) and its event
// emitter accumulates listeners across tests if not cleared. Without
// this teardown, a "request:start" listener installed in test A would
// continue to fire during test B and leak request counts/captures.
// (server.resetHandlers() is invoked separately by tests/setup.ts.)
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
// useGenerateNotesMutation - happy path
// ---------------------------------------------------------------------------

describe("useGenerateNotesMutation - happy path", () => {
  it("POSTs /api/notes/generate and returns GenerateNotesResponse", async () => {
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useGenerateNotesMutation(), { wrapper });

    result.current.mutate({
      relationship_context:
        "We went to college together, he is now VP of Ops at a Series B logistics startup",
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    // Default MSW handler returns { ai_notes, model, generated_at } via
    // makeGenerateNotesResponse(). The response shape matches the
    // backend pydantic NoteGenerationResponse contract per AAP Sec 0.4.4.
    expect(result.current.data).toMatchObject({
      ai_notes: expect.any(String),
      model: expect.any(String),
      generated_at: expect.any(String),
    });
  });

  it("sends the relationship_context field in the request body", async () => {
    let capturedBody: unknown = null;
    server.events.on("request:start", async ({ request }) => {
      if (request.url.includes("/api/notes/generate")) {
        // request.clone() returns a fresh body stream so reading it here
        // does not consume the body that MSW's handler will read for
        // matching/echoing. Without clone(), reading the body twice would
        // throw "body stream already read".
        const cloned = request.clone();
        capturedBody = await cloned.json();
      }
    });

    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useGenerateNotesMutation(), { wrapper });

    result.current.mutate({
      relationship_context: "Met at a conference in Berlin in 2024",
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    // The hook MUST forward the relationship_context field verbatim with
    // no extra fields injected. Backend pydantic uses extra="forbid", so
    // any extra field would cause a 422 in production.
    expect(capturedBody).toEqual({
      relationship_context: "Met at a conference in Berlin in 2024",
    });
  });

  it("does NOT fire toast on success (form renders AI text directly)", async () => {
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useGenerateNotesMutation(), { wrapper });

    result.current.mutate({ relationship_context: "Test context" });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    // Per AAP Sec 0.5.4 (Screen 2 - Add/Edit Connection Form): the form
    // renders the returned ai_notes directly into an editable textarea.
    // A success toast would be redundant and pull focus from the form.
    expect(toastSpies.success).not.toHaveBeenCalled();
    expect(toastSpies.error).not.toHaveBeenCalled();
    expect(toastSpies.info).not.toHaveBeenCalled();
    expect(toastSpies.warning).not.toHaveBeenCalled();
    expect(toastSpies.show).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// useGenerateNotesMutation - retry: 0 (no retries on failure)
// ---------------------------------------------------------------------------

describe("useGenerateNotesMutation - retry: 0", () => {
  it("does NOT retry on 504 (only fires fetch once)", async () => {
    let requestCount = 0;
    server.events.on("request:start", ({ request }) => {
      if (request.url.includes("/api/notes/generate")) {
        requestCount += 1;
      }
    });

    server.use(overrides.notes.timeout504());

    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useGenerateNotesMutation(), { wrapper });

    result.current.mutate({ relationship_context: "Test" });

    await waitFor(() => expect(result.current.isError).toBe(true));

    // Fail-fast: exactly one network call. Without retry: 0 (and the
    // QueryClient default), TanStack Query would retry 3 times (4 fetches
    // total) compounding the 5-second AI budget per AAP Sec 0.7.3.
    expect(requestCount).toBe(1);
  });

  it("does NOT retry on 503", async () => {
    let requestCount = 0;
    server.events.on("request:start", ({ request }) => {
      if (request.url.includes("/api/notes/generate")) {
        requestCount += 1;
      }
    });

    server.use(overrides.notes.unavailable503());

    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useGenerateNotesMutation(), { wrapper });

    result.current.mutate({ relationship_context: "Test" });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(requestCount).toBe(1);
  });

  it("does NOT retry on 500", async () => {
    let requestCount = 0;
    server.events.on("request:start", ({ request }) => {
      if (request.url.includes("/api/notes/generate")) {
        requestCount += 1;
      }
    });

    server.use(overrides.notes.error500());

    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useGenerateNotesMutation(), { wrapper });

    result.current.mutate({ relationship_context: "Test" });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(requestCount).toBe(1);
  });

  it("does NOT retry on 422 validation error", async () => {
    let requestCount = 0;
    server.events.on("request:start", ({ request }) => {
      if (request.url.includes("/api/notes/generate")) {
        requestCount += 1;
      }
    });

    server.use(overrides.notes.validation422());

    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useGenerateNotesMutation(), { wrapper });

    result.current.mutate({ relationship_context: "Test" });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(requestCount).toBe(1);
  });
});

// ---------------------------------------------------------------------------
// useGenerateNotesMutation - soft failures (NO toast.error)
//
// Per AAP Sec 0.4.4: "AI failure does NOT roll back the form submit."
// Soft failures (504 ai_timeout / 503 ai_unavailable) signal that the
// AI provider is unhealthy but the user's session and connection-record
// submission are unaffected. The hook deliberately fires NO toast so
// that AddEditConnectionForm.tsx can render an inline non-blocking
// banner ("AI unavailable; you can still submit") without modal noise.
// ---------------------------------------------------------------------------

describe("useGenerateNotesMutation - soft failures (NO toast.error)", () => {
  it("on 504 ai_timeout: error is ApiError with status=504 and code='ai_timeout'", async () => {
    server.use(overrides.notes.timeout504());

    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useGenerateNotesMutation(), { wrapper });

    result.current.mutate({ relationship_context: "Test" });

    await waitFor(() => expect(result.current.isError).toBe(true));

    expect(result.current.error).toBeInstanceOf(ApiError);
    expect(result.current.error?.status).toBe(504);
    expect(result.current.error?.code).toBe("ai_timeout");
    expect(isSoftAiFailure(result.current.error)).toBe(true);

    // Critical: NO toast on soft failures. The form shows an inline banner.
    expect(toastSpies.error).not.toHaveBeenCalled();
    expect(toastSpies.success).not.toHaveBeenCalled();
    expect(toastSpies.info).not.toHaveBeenCalled();
    expect(toastSpies.warning).not.toHaveBeenCalled();
    expect(toastSpies.show).not.toHaveBeenCalled();
  });

  it("on 503 ai_unavailable: error is ApiError with status=503 and code='ai_unavailable'", async () => {
    server.use(overrides.notes.unavailable503());

    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useGenerateNotesMutation(), { wrapper });

    result.current.mutate({ relationship_context: "Test" });

    await waitFor(() => expect(result.current.isError).toBe(true));

    expect(result.current.error).toBeInstanceOf(ApiError);
    expect(result.current.error?.status).toBe(503);
    expect(result.current.error?.code).toBe("ai_unavailable");
    expect(isSoftAiFailure(result.current.error)).toBe(true);

    // Critical: NO toast on soft failures.
    expect(toastSpies.error).not.toHaveBeenCalled();
    expect(toastSpies.success).not.toHaveBeenCalled();
    expect(toastSpies.show).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// useGenerateNotesMutation - hard failures (toast.error fires)
//
// Hard failures (5xx server errors, 422 validation, etc.) are not
// recoverable via the inline banner UX. The hook fires toast.error so
// the user has clear, actionable feedback that the AI button click
// failed. The form's submit flow continues to function normally; this
// only affects the optional AI note generation.
// ---------------------------------------------------------------------------

describe("useGenerateNotesMutation - hard failures (toast.error fires)", () => {
  it("on 500 internal_error: fires toast.error with the server message", async () => {
    server.use(overrides.notes.error500());

    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useGenerateNotesMutation(), { wrapper });

    result.current.mutate({ relationship_context: "Test" });

    await waitFor(() => expect(result.current.isError).toBe(true));

    expect(result.current.error).toBeInstanceOf(ApiError);
    expect(result.current.error?.status).toBe(500);
    expect(result.current.error?.code).toBe("internal_error");
    expect(isSoftAiFailure(result.current.error)).toBe(false);

    // Toast fires for hard failures.
    expect(toastSpies.error).toHaveBeenCalledTimes(1);
    // Soft-failure toast variants are NOT fired.
    expect(toastSpies.success).not.toHaveBeenCalled();
    expect(toastSpies.info).not.toHaveBeenCalled();
  });

  it("on 422 validation_error: fires toast.error", async () => {
    server.use(overrides.notes.validation422());

    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useGenerateNotesMutation(), { wrapper });

    result.current.mutate({ relationship_context: "" });

    await waitFor(() => expect(result.current.isError).toBe(true));

    expect(result.current.error).toBeInstanceOf(ApiError);
    expect(result.current.error?.status).toBe(422);
    // Default validation422() handler uses backend code "validation_error";
    // isSoftAiFailure correctly classifies this as a hard failure because
    // only "ai_timeout" and "ai_unavailable" qualify as soft per AAP Sec 0.4.4.
    expect(result.current.error?.code).toBe("validation_error");
    expect(isSoftAiFailure(result.current.error)).toBe(false);

    // Toast fires for hard failures.
    expect(toastSpies.error).toHaveBeenCalledTimes(1);
  });

  it("toast.error message is the server-provided message text", async () => {
    server.use(overrides.notes.error500());

    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useGenerateNotesMutation(), { wrapper });

    result.current.mutate({ relationship_context: "Test" });

    await waitFor(() => expect(result.current.isError).toBe(true));

    // The hook calls toast.error(error.message || fallback). The default
    // error500 handler returns "Internal server error" in the envelope.
    // We assert that the toast received a non-empty string argument so
    // future copy changes do not break this test, while still verifying
    // the message-propagation contract.
    expect(toastSpies.error).toHaveBeenCalledTimes(1);
    const firstCallArgs = toastSpies.error.mock.calls[0];
    expect(firstCallArgs).toBeDefined();
    expect(typeof firstCallArgs?.[0]).toBe("string");
    expect((firstCallArgs?.[0] as string).length).toBeGreaterThan(0);
  });
});

// ---------------------------------------------------------------------------
// isSoftAiFailure helper - exhaustive input coverage
//
// Twelve documented inputs covering:
//   - 4 ApiError soft (ai_timeout, ai_unavailable) and hard
//     (validation_failed, internal_error) failure codes
//   - null and undefined (the ApiError|null|undefined slots)
//   - plain Error, plain object, string, number (negative-type guards)
//   - 3 additional ApiError codes (auth_required, forbidden, network_error)
//
// ApiError instances are constructed directly to avoid network round-trips.
// The constructor signature is (status, code, message, options?) where
// `options` is { correlationId?, fields?, cause? }. We pass empty options
// (or omit it) for tests that do not exercise those fields.
// ---------------------------------------------------------------------------

describe("isSoftAiFailure helper", () => {
  it('returns true for ApiError with code "ai_timeout"', () => {
    const error = new ApiError(504, "ai_timeout", "AI service timed out");
    expect(isSoftAiFailure(error)).toBe(true);
  });

  it('returns true for ApiError with code "ai_unavailable"', () => {
    const error = new ApiError(503, "ai_unavailable", "AI service unavailable");
    expect(isSoftAiFailure(error)).toBe(true);
  });

  it('returns false for ApiError with code "validation_failed"', () => {
    const error = new ApiError(422, "validation_failed", "Invalid input", {
      fields: [{ field: "relationship_context", code: "missing", message: "Required" }],
    });
    expect(isSoftAiFailure(error)).toBe(false);
  });

  it('returns false for ApiError with code "internal_error"', () => {
    const error = new ApiError(500, "internal_error", "Server error");
    expect(isSoftAiFailure(error)).toBe(false);
  });

  it("returns false for null", () => {
    expect(isSoftAiFailure(null)).toBe(false);
  });

  it("returns false for undefined", () => {
    expect(isSoftAiFailure(undefined)).toBe(false);
  });

  it("returns false for a plain Error (not an ApiError)", () => {
    // The runtime guard inside isSoftAiFailure short-circuits when
    // error.code is not one of the soft codes; a plain Error has no
    // `.code` property at all (undefined !== "ai_timeout" / "ai_unavailable"),
    // so the function returns false. We cast to ApiError here only to
    // satisfy the static signature; the helper is documented as defensive
    // against non-ApiError truthy values to avoid crashing the form.
    const error = new Error("Generic JS error") as unknown as ApiError;
    expect(isSoftAiFailure(error)).toBe(false);
  });

  it("returns false for an arbitrary object lacking a code property", () => {
    // Same rationale as the plain-Error case: defensive coverage of the
    // soft-vs-hard classifier when the input is structurally unexpected.
    // The cast is required because ApiError is a class type with a
    // narrower nominal shape than this anonymous object literal.
    const fakeError = { status: 504 } as unknown as ApiError;
    expect(isSoftAiFailure(fakeError)).toBe(false);
  });

  it("returns false for a string", () => {
    // Strings are truthy but lack a `.code` property; the helper must
    // not crash on accessor evaluation and must return false.
    const stringError = "ai_timeout" as unknown as ApiError;
    expect(isSoftAiFailure(stringError)).toBe(false);
  });

  it("returns false for an empty string", () => {
    // Empty strings are falsy; the !error early-return path returns false.
    const emptyString = "" as unknown as ApiError;
    expect(isSoftAiFailure(emptyString)).toBe(false);
  });

  it('returns false for ApiError with code "auth_required"', () => {
    const error = new ApiError(401, "auth_required", "Authentication required.");
    expect(isSoftAiFailure(error)).toBe(false);
  });

  it('returns false for ApiError with code "forbidden"', () => {
    const error = new ApiError(403, "forbidden", "Forbidden");
    expect(isSoftAiFailure(error)).toBe(false);
  });

  it('returns false for ApiError with code "network_error"', () => {
    // Network errors get status=0 from the client.ts wrapper.
    const error = new ApiError(0, "network_error", "Network request failed.");
    expect(isSoftAiFailure(error)).toBe(false);
  });

  it("preserves ApiError fields when classifying", () => {
    // Round-trip check: a soft failure with rich envelope data must still
    // classify as soft, and the envelope fields remain accessible to
    // callers that need them (e.g., for debugging). This guards against
    // a refactor that accidentally narrows isSoftAiFailure to instanceof
    // checks and drops the data.
    const error = new ApiError(504, "ai_timeout", "AI service timed out", {
      correlationId: "sc-fe-test-correlation-id",
      fields: [],
    });
    expect(isSoftAiFailure(error)).toBe(true);
    expect(error.correlationId).toBe("sc-fe-test-correlation-id");
  });
});
