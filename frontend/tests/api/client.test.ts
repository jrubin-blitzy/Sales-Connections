/**
 * client.test.ts - Vitest tests for the fetch wrapper (AAP-MANDATED).
 *
 * Tests `frontend/src/api/client.ts`, the foundational HTTP wrapper used
 * by every API hook in the SPA.
 *
 * Coverage targets (per AAP Sec 0.2.3 and the assigned-folder
 * requirements):
 *   - apiGet, apiPost, apiPatch, apiPut, apiDelete  (happy paths)
 *   - ApiError class (status, code, correlationId, fields, fieldError)
 *   - 401 redirect via window.location.replace
 *   - skipAuthRedirect option suppresses the 401 redirect
 *   - X-Correlation-Id header propagation from getCorrelationId()
 *   - credentials: "include" for cookie-based session JWT
 *   - Content-Type: application/json on requests with bodies
 *   - 204 No Content returns undefined (DELETE)
 *   - Error envelope deserialization { error: { code, message,
 *       correlation_id, fields } }
 *   - Network error -> ApiError(0, "network_error")
 *   - AbortError passthrough (NOT wrapped in ApiError)
 *   - URL construction with VITE_API_BASE_URL
 *   - AbortSignal propagation
 *   - Custom headers merge with defaults
 *
 * Test approach:
 *   - Direct vi.spyOn(global, "fetch") for full control over Response
 *     objects (204 with null body, malformed JSON, network error
 *     rejections, AbortError).
 *   - Mock window.location.replace via Object.defineProperty with the
 *     `configurable: true` flag (jsdom's Location is normally read-only).
 *   - Mock @/lib/correlationId via vi.mock(factory) with hoisted spies
 *     so we can both assert call counts and override the returned ID
 *     mid-test (used to verify "new ID after reset" behaviour).
 *
 * Per AAP Sec 0.2.3, this is an AAP-MANDATED test file. Per AAP
 * Sec 0.7.7, all imports are pinned to versions documented in
 * frontend/package.json; vitest is 2.1.9.
 *
 * Conventions per frontend/.prettierrc.json:
 *   - Strict TypeScript; no `any`.
 *   - Double quotes; trailing commas; 2-space indent; line length <= 100.
 *   - No emoji; no console.log.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// ---------------------------------------------------------------------------
// Mock @/lib/correlationId BEFORE importing the module under test.
//
// vi.hoisted lifts the spy declarations above the import statements
// (vitest hoists vi.mock calls to the top of the file at compile time;
// vi.hoisted gives us a place to declare the spy references that the
// factory captures in the same lexical scope).
// ---------------------------------------------------------------------------

const { correlationIdSpies } = vi.hoisted(() => ({
  correlationIdSpies: {
    getCorrelationId: vi.fn<() => string>(),
    resetCorrelationId: vi.fn<() => string>(),
  },
}));

vi.mock("@/lib/correlationId", () => ({
  getCorrelationId: correlationIdSpies.getCorrelationId,
  resetCorrelationId: correlationIdSpies.resetCorrelationId,
}));

// Static imports of the module under test. These resolve AFTER the
// vi.mock above is hoisted, so the wrapper sees the mocked correlationId.
import {
  ApiError,
  apiDelete,
  apiGet,
  apiPatch,
  apiPost,
  apiPut,
  type ApiErrorField,
  type ApiRequestOptions,
} from "@/api/client";

// ---------------------------------------------------------------------------
// Module-scoped fixtures
// ---------------------------------------------------------------------------

/**
 * A correlation ID that matches the documented format (per AAP
 * Sec 0.4.3): "<prefix><uuid v4>" where the prefix defaults to
 * "sc-fe-". The UUID below is hand-crafted to satisfy the RFC 4122 v4
 * layout (third group starts with "4", fourth group with [89ab]).
 */
const FIXED_CORRELATION_ID = "sc-fe-12345678-1234-4567-89ab-123456789012";

/**
 * RFC 4122 v4 layout regex matching the documented correlation ID
 * format. Reused across the correlation-ID describe block to verify
 * both the test fixture and the propagated header conform.
 */
const CORRELATION_ID_FORMAT =
  /^sc-fe-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

// ---------------------------------------------------------------------------
// Per-test fixtures (re-initialised in beforeEach)
// ---------------------------------------------------------------------------

/**
 * Spy reference to the global fetch function. Installed by beforeEach,
 * cleared by afterEach via vi.restoreAllMocks(). Tests configure
 * per-call behaviour via fetchSpy.mockResolvedValueOnce(response) or
 * fetchSpy.mockRejectedValueOnce(error).
 *
 * Using vi.MockInstance<typeof fetch> avoids the verbose
 * vi.spyOn-generic form that TypeScript narrows aggressively against
 * the globalThis surface (where many globals are not Methods/Classes
 * in the strict sense and would fail the spyOn constraint).
 */
let fetchSpy: import("vitest").MockInstance<typeof fetch>;

/**
 * Captured original window.location reference. Restored by afterEach so
 * the patch performed in beforeEach does not leak into other test files
 * (jsdom shares a single window across the worker).
 */
let originalLocation: Location;

/**
 * Per-test spy for window.location.replace. Tests assert against
 * replaceSpy.mock.calls to verify 401-redirect URL construction.
 */
let replaceSpy: ReturnType<typeof vi.fn>;

// ---------------------------------------------------------------------------
// Test helpers
// ---------------------------------------------------------------------------

/**
 * Build a fetch Response carrying a JSON-stringified body, the given
 * status, and an optional set of additional headers. The Content-Type
 * header always includes "application/json" so the wrapper's
 * response.json() path is exercised.
 *
 * @param body    - JSON-serialisable body (will be stringified).
 * @param status  - HTTP status (default 200).
 * @param headers - Optional extra headers merged AFTER Content-Type.
 * @returns         A Response object suitable for fetchSpy.mockResolvedValueOnce.
 */
function makeJsonResponse(
  body: unknown,
  status = 200,
  headers: Record<string, string> = {},
): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

/**
 * Build a 204 No Content Response with a null body. The wrapper's
 * special 204 handling (returns undefined) is exercised via this
 * helper.
 *
 * @returns A Response object with status 204 and no body.
 */
function makeNoContentResponse(): Response {
  return new Response(null, { status: 204 });
}

/**
 * Inspect the first recorded fetch call and return a normalised
 * { url, init } pair. Returns null when no fetch call has been made
 * yet (defensive: tests that need a call first should resolve the
 * helper promise before invoking captureFetchCall).
 *
 * @returns The first fetch call's URL string and RequestInit, or null.
 */
function captureFetchCall(): { url: string; init: RequestInit } | null {
  if (fetchSpy.mock.calls.length === 0) {
    return null;
  }
  const args = fetchSpy.mock.calls[0];
  if (!args) {
    return null;
  }
  const target = args[0];
  return {
    url:
      typeof target === "string" ? target : target instanceof URL ? target.toString() : target.url,
    init: args[1] ?? {},
  };
}

/**
 * Read a header value from a RequestInit, accepting Headers,
 * Record<string, string>, and array-pair forms. Header lookup is
 * case-insensitive per HTTP semantics. Returns null if the header is
 * absent or the init has no headers field.
 *
 * @param init - The RequestInit object to inspect.
 * @param name - Header name (case-insensitive).
 * @returns      Header value, or null if absent.
 */
function getHeader(init: RequestInit, name: string): string | null {
  const headers = init.headers;
  if (!headers) {
    return null;
  }
  if (headers instanceof Headers) {
    return headers.get(name);
  }
  if (Array.isArray(headers)) {
    const found = headers.find(([k]) => k.toLowerCase() === name.toLowerCase());
    return found ? found[1] : null;
  }
  const record = headers as Record<string, string>;
  for (const key of Object.keys(record)) {
    if (key.toLowerCase() === name.toLowerCase()) {
      const value = record[key];
      return value !== undefined ? value : null;
    }
  }
  return null;
}

// ---------------------------------------------------------------------------
// Per-test setup / teardown
// ---------------------------------------------------------------------------

beforeEach(() => {
  // Predictable correlation ID for header tests; individual tests can
  // override via correlationIdSpies.getCorrelationId.mockReturnValueOnce(...)
  // or correlationIdSpies.getCorrelationId.mockReturnValue(...).
  correlationIdSpies.getCorrelationId.mockReturnValue(FIXED_CORRELATION_ID);
  correlationIdSpies.resetCorrelationId.mockClear();

  // Spy on global.fetch. Default behaviour: tests must register a mock
  // resolution per call via mockResolvedValueOnce or mockRejectedValueOnce.
  fetchSpy = vi.spyOn(globalThis, "fetch");

  // Replace window.location with a configurable copy so we can mock
  // the .replace method and override .pathname / .search per test.
  // jsdom's default Location is read-only on the window prototype; we
  // shadow it with a configurable property on the window instance.
  originalLocation = window.location;
  replaceSpy = vi.fn();
  Object.defineProperty(window, "location", {
    configurable: true,
    writable: true,
    value: {
      ...originalLocation,
      pathname: "/feed",
      search: "",
      replace: replaceSpy,
      assign: vi.fn(),
      reload: vi.fn(),
    } as unknown as Location,
  });
});

afterEach(() => {
  // Restore the original fetch and any other spies installed during
  // the test (e.g., correlationId spies are kept via the module mock,
  // but their per-call return values are cleared by mockReset within
  // tests that override them).
  vi.restoreAllMocks();

  // Restore the original window.location. Without this, the next test
  // file would inherit the patched location and fail when reading
  // window.location.href (which we never re-assigned).
  Object.defineProperty(window, "location", {
    configurable: true,
    writable: true,
    value: originalLocation,
  });
});

// ---------------------------------------------------------------------------
// ApiError class tests
//
// The ApiError constructor signature in the SUT is:
//   new ApiError(status, code, message, options?: { correlationId?, fields?, cause? })
// This is the canonical shape; tests below construct ApiError directly
// to verify the public surface independently of the request pipeline.
// ---------------------------------------------------------------------------

describe("ApiError class", () => {
  it("extends Error and is instanceof both Error and ApiError", () => {
    const error = new ApiError(422, "validation_failed", "Bad input");
    expect(error).toBeInstanceOf(Error);
    expect(error).toBeInstanceOf(ApiError);
  });

  it("exposes status, code, and message via the constructor positional args", () => {
    const error = new ApiError(422, "validation_failed", "Bad input");
    expect(error.status).toBe(422);
    expect(error.code).toBe("validation_failed");
    expect(error.message).toBe("Bad input");
  });

  it("exposes correlationId from the options bag", () => {
    const error = new ApiError(500, "internal_error", "Crashed", {
      correlationId: "corr-abc-123",
    });
    expect(error.correlationId).toBe("corr-abc-123");
  });

  it("correlationId is undefined when not supplied", () => {
    const error = new ApiError(500, "internal_error", "Crashed");
    expect(error.correlationId).toBeUndefined();
  });

  it("exposes fields from the options bag (preserves order)", () => {
    const fields: ApiErrorField[] = [
      { field: "email", code: "invalid_email", message: "Invalid email" },
      { field: "password", code: "too_short", message: "Too short" },
    ];
    const error = new ApiError(422, "validation_failed", "Bad", { fields });
    expect(error.fields).toEqual(fields);
    expect(error.fields).toHaveLength(2);
  });

  it("fields defaults to empty array when not supplied", () => {
    const error = new ApiError(500, "internal_error", "Crashed");
    expect(error.fields).toEqual([]);
  });

  it("error.name is 'ApiError'", () => {
    const error = new ApiError(500, "internal_error", "Crashed");
    expect(error.name).toBe("ApiError");
  });

  it("preserves the cause when supplied via options", () => {
    const underlying = new TypeError("underlying");
    const error = new ApiError(0, "network_error", "Failed", { cause: underlying });
    expect(error.cause).toBe(underlying);
  });

  describe("fieldError(field)", () => {
    it("returns the matching ApiErrorField when present", () => {
      const fields: ApiErrorField[] = [
        { field: "email", code: "invalid_email", message: "Invalid email" },
        { field: "password", code: "too_short", message: "Too short" },
      ];
      const error = new ApiError(422, "validation_failed", "Bad", { fields });
      const found = error.fieldError("email");
      expect(found).toBeDefined();
      expect(found?.field).toBe("email");
      expect(found?.code).toBe("invalid_email");
      expect(found?.message).toBe("Invalid email");
    });

    it("returns the entry that exactly matches a different field name", () => {
      const fields: ApiErrorField[] = [
        { field: "email", code: "invalid_email", message: "Invalid email" },
        { field: "password", code: "too_short", message: "Too short" },
      ];
      const error = new ApiError(422, "validation_failed", "Bad", { fields });
      const password = error.fieldError("password");
      expect(password?.code).toBe("too_short");
      expect(password?.message).toBe("Too short");
    });

    it("returns undefined for an unknown field", () => {
      const fields: ApiErrorField[] = [
        { field: "email", code: "invalid_email", message: "Invalid email" },
      ];
      const error = new ApiError(422, "validation_failed", "Bad", { fields });
      expect(error.fieldError("non-existent")).toBeUndefined();
    });

    it("returns undefined when fields is empty", () => {
      const error = new ApiError(500, "internal_error", "Crashed", { fields: [] });
      expect(error.fieldError("any")).toBeUndefined();
    });

    it("returns undefined when fields is unset", () => {
      const error = new ApiError(500, "internal_error", "Crashed");
      expect(error.fieldError("any")).toBeUndefined();
    });
  });
});

// ---------------------------------------------------------------------------
// Happy-path tests: each HTTP verb returns the parsed JSON body on 2xx.
// ---------------------------------------------------------------------------

describe("apiGet - happy path", () => {
  it("returns the parsed JSON body on 200", async () => {
    fetchSpy.mockResolvedValueOnce(makeJsonResponse({ ok: true, count: 42 }));
    const result = await apiGet<{ ok: boolean; count: number }>("/api/test");
    expect(result).toEqual({ ok: true, count: 42 });
  });

  it("uses the GET HTTP method", async () => {
    fetchSpy.mockResolvedValueOnce(makeJsonResponse({}));
    await apiGet("/api/test");
    const call = captureFetchCall();
    expect(call?.init.method).toBe("GET");
  });

  it("does NOT include a body or Content-Type header on GET", async () => {
    fetchSpy.mockResolvedValueOnce(makeJsonResponse({}));
    await apiGet("/api/test");
    const call = captureFetchCall();
    expect(call?.init.body).toBeUndefined();
    expect(getHeader(call!.init, "Content-Type")).toBeNull();
  });

  it("includes Accept: application/json header", async () => {
    fetchSpy.mockResolvedValueOnce(makeJsonResponse({}));
    await apiGet("/api/test");
    const call = captureFetchCall();
    expect(getHeader(call!.init, "Accept")).toBe("application/json");
  });

  it("returns an array body when the endpoint returns a JSON array", async () => {
    fetchSpy.mockResolvedValueOnce(makeJsonResponse([1, 2, 3]));
    const result = await apiGet<number[]>("/api/numbers");
    expect(result).toEqual([1, 2, 3]);
  });
});

describe("apiPost - happy path", () => {
  it("returns the parsed JSON body on 201 Created", async () => {
    fetchSpy.mockResolvedValueOnce(makeJsonResponse({ id: "new-1" }, 201));
    const result = await apiPost<{ id: string }, { name: string }>("/api/items", {
      name: "X",
    });
    expect(result).toEqual({ id: "new-1" });
  });

  it("returns the parsed JSON body on 200 OK", async () => {
    fetchSpy.mockResolvedValueOnce(makeJsonResponse({ ok: true }));
    const result = await apiPost<{ ok: boolean }, { x: number }>("/api/echo", { x: 1 });
    expect(result).toEqual({ ok: true });
  });

  it("uses the POST HTTP method", async () => {
    fetchSpy.mockResolvedValueOnce(makeJsonResponse({}, 201));
    await apiPost("/api/items", { name: "X" });
    const call = captureFetchCall();
    expect(call?.init.method).toBe("POST");
  });

  it("serializes the request body to JSON", async () => {
    fetchSpy.mockResolvedValueOnce(makeJsonResponse({}, 201));
    await apiPost("/api/items", { name: "X", count: 7 });
    const call = captureFetchCall();
    expect(call?.init.body).toBe(JSON.stringify({ name: "X", count: 7 }));
  });

  it("sets Content-Type: application/json when a body is present", async () => {
    fetchSpy.mockResolvedValueOnce(makeJsonResponse({}, 201));
    await apiPost("/api/items", { foo: "bar" });
    const call = captureFetchCall();
    expect(getHeader(call!.init, "Content-Type")).toBe("application/json");
  });

  it("serializes an empty-object body when payload is {}", async () => {
    fetchSpy.mockResolvedValueOnce(makeJsonResponse({}));
    await apiPost("/api/items", {});
    const call = captureFetchCall();
    expect(call?.init.body).toBe("{}");
    expect(getHeader(call!.init, "Content-Type")).toBe("application/json");
  });

  it("preserves array payloads in the body", async () => {
    fetchSpy.mockResolvedValueOnce(makeJsonResponse({}));
    await apiPost("/api/bulk", [1, 2, 3]);
    const call = captureFetchCall();
    expect(call?.init.body).toBe("[1,2,3]");
  });
});

describe("apiPatch - happy path", () => {
  it("returns the parsed JSON body on 200 OK", async () => {
    fetchSpy.mockResolvedValueOnce(makeJsonResponse({ id: "x", updated: true }));
    const result = await apiPatch<{ id: string; updated: boolean }, { name: string }>(
      "/api/items/x",
      { name: "updated" },
    );
    expect(result).toEqual({ id: "x", updated: true });
  });

  it("uses the PATCH HTTP method with a JSON body", async () => {
    fetchSpy.mockResolvedValueOnce(makeJsonResponse({}));
    await apiPatch("/api/items/x", { name: "Y" });
    const call = captureFetchCall();
    expect(call?.init.method).toBe("PATCH");
    expect(call?.init.body).toBe(JSON.stringify({ name: "Y" }));
  });

  it("sets Content-Type: application/json on PATCH with body", async () => {
    fetchSpy.mockResolvedValueOnce(makeJsonResponse({}));
    await apiPatch("/api/items/x", { y: 2 });
    const call = captureFetchCall();
    expect(getHeader(call!.init, "Content-Type")).toBe("application/json");
  });
});

describe("apiPut - happy path", () => {
  it("returns the parsed JSON body on 200 OK", async () => {
    fetchSpy.mockResolvedValueOnce(makeJsonResponse({ id: "x", replaced: true }));
    const result = await apiPut<{ id: string; replaced: boolean }, { name: string }>(
      "/api/items/x",
      { name: "Y" },
    );
    expect(result).toEqual({ id: "x", replaced: true });
  });

  it("uses the PUT HTTP method with a JSON body", async () => {
    fetchSpy.mockResolvedValueOnce(makeJsonResponse({}));
    await apiPut("/api/items/x", { name: "Y" });
    const call = captureFetchCall();
    expect(call?.init.method).toBe("PUT");
    expect(call?.init.body).toBe(JSON.stringify({ name: "Y" }));
  });

  it("sets Content-Type: application/json on PUT with body", async () => {
    fetchSpy.mockResolvedValueOnce(makeJsonResponse({}));
    await apiPut("/api/items/x", { z: 3 });
    const call = captureFetchCall();
    expect(getHeader(call!.init, "Content-Type")).toBe("application/json");
  });
});

describe("apiDelete - happy path", () => {
  it("uses the DELETE HTTP method", async () => {
    fetchSpy.mockResolvedValueOnce(makeJsonResponse({ ok: true }));
    await apiDelete("/api/items/x");
    const call = captureFetchCall();
    expect(call?.init.method).toBe("DELETE");
  });

  it("returns the parsed JSON body when response is 200 with body", async () => {
    fetchSpy.mockResolvedValueOnce(
      makeJsonResponse({ id: "x", deleted_at: "2026-04-01T00:00:00Z" }),
    );
    const result = await apiDelete<{ id: string; deleted_at: string }>("/api/items/x");
    expect(result).toEqual({ id: "x", deleted_at: "2026-04-01T00:00:00Z" });
  });

  it("returns undefined on 204 No Content (admin hard-delete contract)", async () => {
    fetchSpy.mockResolvedValueOnce(makeNoContentResponse());
    const result = await apiDelete<void>("/api/admin/records/x");
    expect(result).toBeUndefined();
  });

  it("does NOT include a body on DELETE", async () => {
    fetchSpy.mockResolvedValueOnce(makeNoContentResponse());
    await apiDelete("/api/items/x");
    const call = captureFetchCall();
    expect(call?.init.body).toBeUndefined();
  });

  it("does NOT include a Content-Type header on DELETE without body", async () => {
    fetchSpy.mockResolvedValueOnce(makeNoContentResponse());
    await apiDelete("/api/items/x");
    const call = captureFetchCall();
    expect(getHeader(call!.init, "Content-Type")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// credentials: "include" - cookie propagation
//
// AAP Sec 0.4.3 mandates HttpOnly-cookie session JWTs. credentials must
// be "include" on every request so the cookie travels.
// ---------------------------------------------------------------------------

describe("credentials: include (cookie propagation)", () => {
  it("sets credentials: include on GET", async () => {
    fetchSpy.mockResolvedValueOnce(makeJsonResponse({}));
    await apiGet("/api/test");
    const call = captureFetchCall();
    expect(call?.init.credentials).toBe("include");
  });

  it("sets credentials: include on POST", async () => {
    fetchSpy.mockResolvedValueOnce(makeJsonResponse({}, 201));
    await apiPost("/api/test", { x: 1 });
    const call = captureFetchCall();
    expect(call?.init.credentials).toBe("include");
  });

  it("sets credentials: include on PATCH", async () => {
    fetchSpy.mockResolvedValueOnce(makeJsonResponse({}));
    await apiPatch("/api/test/1", { x: 1 });
    const call = captureFetchCall();
    expect(call?.init.credentials).toBe("include");
  });

  it("sets credentials: include on PUT", async () => {
    fetchSpy.mockResolvedValueOnce(makeJsonResponse({}));
    await apiPut("/api/test/1", { x: 1 });
    const call = captureFetchCall();
    expect(call?.init.credentials).toBe("include");
  });

  it("sets credentials: include on DELETE", async () => {
    fetchSpy.mockResolvedValueOnce(makeNoContentResponse());
    await apiDelete("/api/test/1");
    const call = captureFetchCall();
    expect(call?.init.credentials).toBe("include");
  });
});

// ---------------------------------------------------------------------------
// X-Correlation-Id header propagation
//
// Per AAP Sec 0.4.3 (Surface 1) and Sec 0.7.5 (Observability rule),
// every outbound request must carry the per-page correlation ID so the
// backend's correlation middleware can bind it to structlog and OTLP
// trace context for end-to-end log correlation.
// ---------------------------------------------------------------------------

describe("X-Correlation-Id header propagation", () => {
  it("includes the X-Correlation-Id header sourced from getCorrelationId()", async () => {
    fetchSpy.mockResolvedValueOnce(makeJsonResponse({}));
    await apiGet("/api/test");
    const call = captureFetchCall();
    expect(getHeader(call!.init, "X-Correlation-Id")).toBe(FIXED_CORRELATION_ID);
  });

  it("invokes getCorrelationId() on every request", async () => {
    // mockImplementation rather than mockResolvedValue so each call gets
    // a FRESH Response instance; otherwise the body stream is consumed
    // by the first call and subsequent json() reads throw.
    fetchSpy.mockImplementation(() => Promise.resolve(makeJsonResponse({})));
    correlationIdSpies.getCorrelationId.mockClear();

    await apiGet("/api/a");
    await apiPost("/api/b", { x: 1 });
    await apiDelete("/api/c");

    expect(correlationIdSpies.getCorrelationId).toHaveBeenCalledTimes(3);
  });

  it("the correlation ID matches the documented prefix + UUID v4 regex", async () => {
    expect(FIXED_CORRELATION_ID).toMatch(CORRELATION_ID_FORMAT);

    fetchSpy.mockResolvedValueOnce(makeJsonResponse({}));
    await apiGet("/api/test");
    const call = captureFetchCall();
    const header = getHeader(call!.init, "X-Correlation-Id");
    expect(header).not.toBeNull();
    expect(header!).toMatch(CORRELATION_ID_FORMAT);
  });

  it("reuses the same correlation ID across multiple requests (cached)", async () => {
    // mockImplementation per call so each Response carries a fresh body
    // stream (response.json() consumes the body and a single Response
    // instance cannot be parsed twice).
    fetchSpy.mockImplementation(() => Promise.resolve(makeJsonResponse({})));

    await apiGet("/api/test1");
    await apiGet("/api/test2");
    await apiGet("/api/test3");

    const headers = fetchSpy.mock.calls.map((args) => {
      const init = args[1] ?? {};
      return getHeader(init, "X-Correlation-Id");
    });

    expect(headers).toHaveLength(3);
    expect(headers[0]).toBe(FIXED_CORRELATION_ID);
    expect(headers[1]).toBe(FIXED_CORRELATION_ID);
    expect(headers[2]).toBe(FIXED_CORRELATION_ID);
  });

  it("after resetCorrelationId() (simulated), the next request gets the NEW ID", async () => {
    // mockImplementation per call so each Response carries a fresh body
    // stream that can be consumed by response.json().
    fetchSpy.mockImplementation(() => Promise.resolve(makeJsonResponse({})));

    // First request uses FIXED_CORRELATION_ID per beforeEach default.
    await apiGet("/api/test1");
    const firstCall = fetchSpy.mock.calls[0];
    expect(getHeader(firstCall![1] ?? {}, "X-Correlation-Id")).toBe(FIXED_CORRELATION_ID);

    // Simulate the effect of resetCorrelationId(): the mocked
    // getCorrelationId now returns a different value. (The spy itself
    // has no internal state; we just override its return.)
    const NEW_ID = "sc-fe-87654321-4321-4321-bcde-210987654321";
    correlationIdSpies.getCorrelationId.mockReturnValue(NEW_ID);

    await apiGet("/api/test2");
    const secondCall = fetchSpy.mock.calls[1];
    expect(getHeader(secondCall![1] ?? {}, "X-Correlation-Id")).toBe(NEW_ID);
  });
});

// ---------------------------------------------------------------------------
// 401 redirect behaviour
//
// Per AAP Sec 0.4.3 and the source agent prompt: 401 responses redirect
// the browser to /login?next=<encoded-pathname+search> via
// window.location.replace. The wrapper STILL throws ApiError so the
// caller's promise chain breaks (TanStack Query onError still fires).
// ---------------------------------------------------------------------------

describe("401 redirect behaviour", () => {
  it("redirects to /login?next=<encoded-pathname> on 401", async () => {
    fetchSpy.mockResolvedValueOnce(
      makeJsonResponse(
        {
          error: {
            code: "auth_required",
            message: "Unauthorized",
            correlation_id: "corr-1",
            fields: [],
          },
        },
        401,
      ),
    );

    await expect(apiGet("/api/test")).rejects.toBeInstanceOf(ApiError);

    expect(replaceSpy).toHaveBeenCalledTimes(1);
    const redirectUrl = replaceSpy.mock.calls[0]![0] as string;
    expect(redirectUrl).toBe("/login?next=" + encodeURIComponent("/feed"));
  });

  it("encodes both pathname AND search in the next param", async () => {
    // Override window.location to include search params.
    Object.defineProperty(window, "location", {
      configurable: true,
      writable: true,
      value: {
        ...originalLocation,
        pathname: "/connections/abc-123",
        search: "?filter=warm&sort=-date",
        replace: replaceSpy,
        assign: vi.fn(),
        reload: vi.fn(),
      } as unknown as Location,
    });

    fetchSpy.mockResolvedValueOnce(
      makeJsonResponse({ error: { code: "auth_required", message: "X" } }, 401),
    );

    await expect(apiGet("/api/test")).rejects.toBeInstanceOf(ApiError);

    const redirectUrl = replaceSpy.mock.calls[0]![0] as string;
    expect(redirectUrl).toBe(
      "/login?next=" + encodeURIComponent("/connections/abc-123?filter=warm&sort=-date"),
    );
  });

  it("STILL throws ApiError(status: 401) so calling code can react", async () => {
    fetchSpy.mockResolvedValueOnce(
      makeJsonResponse({ error: { code: "auth_required", message: "No" } }, 401),
    );

    await expect(apiGet("/api/test")).rejects.toMatchObject({
      status: 401,
      code: "auth_required",
    });
  });

  it("the thrown error is an ApiError instance (instanceof works across realms)", async () => {
    fetchSpy.mockResolvedValueOnce(
      makeJsonResponse({ error: { code: "auth_required", message: "No" } }, 401),
    );

    let captured: unknown;
    try {
      await apiGet("/api/test");
    } catch (error) {
      captured = error;
    }
    expect(captured).toBeInstanceOf(ApiError);
    expect(captured).toBeInstanceOf(Error);
  });

  it("does NOT redirect when the user is already on /login (no infinite loop)", async () => {
    Object.defineProperty(window, "location", {
      configurable: true,
      writable: true,
      value: {
        ...originalLocation,
        pathname: "/login",
        search: "",
        replace: replaceSpy,
        assign: vi.fn(),
        reload: vi.fn(),
      } as unknown as Location,
    });

    fetchSpy.mockResolvedValueOnce(
      makeJsonResponse({ error: { code: "auth_required", message: "X" } }, 401),
    );

    await expect(apiGet("/auth/login")).rejects.toBeInstanceOf(ApiError);
    expect(replaceSpy).not.toHaveBeenCalled();
  });
});

describe("skipAuthRedirect: true suppresses the 401 redirect", () => {
  it("does NOT redirect when skipAuthRedirect is true", async () => {
    fetchSpy.mockResolvedValueOnce(
      makeJsonResponse({ error: { code: "auth_required", message: "No" } }, 401),
    );

    const opts: ApiRequestOptions = { skipAuthRedirect: true };
    await expect(apiGet("/api/me", opts)).rejects.toBeInstanceOf(ApiError);

    expect(replaceSpy).not.toHaveBeenCalled();
  });

  it("STILL throws ApiError when skipAuthRedirect is true", async () => {
    fetchSpy.mockResolvedValueOnce(
      makeJsonResponse({ error: { code: "auth_required", message: "No" } }, 401),
    );

    await expect(apiGet("/api/me", { skipAuthRedirect: true })).rejects.toMatchObject({
      status: 401,
      code: "auth_required",
    });
  });

  it("skipAuthRedirect on POST also suppresses the redirect", async () => {
    fetchSpy.mockResolvedValueOnce(
      makeJsonResponse({ error: { code: "auth_required", message: "No" } }, 401),
    );

    await expect(
      apiPost("/auth/login", { email: "x", password: "y" }, { skipAuthRedirect: true }),
    ).rejects.toBeInstanceOf(ApiError);

    expect(replaceSpy).not.toHaveBeenCalled();
  });

  it("default behaviour (no options object) still redirects on 401", async () => {
    fetchSpy.mockResolvedValueOnce(
      makeJsonResponse({ error: { code: "auth_required", message: "No" } }, 401),
    );

    await expect(apiGet("/api/test")).rejects.toBeInstanceOf(ApiError);
    expect(replaceSpy).toHaveBeenCalledTimes(1);
  });

  it("explicit skipAuthRedirect: false still triggers the redirect", async () => {
    fetchSpy.mockResolvedValueOnce(
      makeJsonResponse({ error: { code: "auth_required", message: "No" } }, 401),
    );

    await expect(apiGet("/api/test", { skipAuthRedirect: false })).rejects.toBeInstanceOf(ApiError);
    expect(replaceSpy).toHaveBeenCalledTimes(1);
  });
});

// ---------------------------------------------------------------------------
// Error envelope deserialization
//
// Backend uniform envelope shape per AAP Sec 0.4.3:
//   { error: { code, message, correlation_id, fields: ApiErrorField[] } }
// ---------------------------------------------------------------------------

describe("Error envelope deserialization", () => {
  it("parses { error: { code, message, correlation_id, fields } } into ApiError", async () => {
    fetchSpy.mockResolvedValueOnce(
      makeJsonResponse(
        {
          error: {
            code: "validation_failed",
            message: "Validation failed for 2 fields",
            correlation_id: "corr-server-99",
            fields: [
              { field: "email", code: "invalid_email", message: "Invalid email format" },
              { field: "password", code: "too_short", message: "Must be at least 8 characters" },
            ],
          },
        },
        422,
      ),
    );

    let captured: ApiError | undefined;
    try {
      await apiPost("/api/test", { email: "bad", password: "x" });
    } catch (error) {
      captured = error as ApiError;
    }

    expect(captured).toBeInstanceOf(ApiError);
    expect(captured!.status).toBe(422);
    expect(captured!.code).toBe("validation_failed");
    expect(captured!.message).toBe("Validation failed for 2 fields");
    expect(captured!.correlationId).toBe("corr-server-99");
    expect(captured!.fields).toHaveLength(2);
    expect(captured!.fieldError("email")?.message).toBe("Invalid email format");
    expect(captured!.fieldError("password")?.message).toBe("Must be at least 8 characters");
    expect(captured!.fieldError("non-existent")).toBeUndefined();
  });

  it("handles 422 with no fields array (defaults to empty)", async () => {
    fetchSpy.mockResolvedValueOnce(
      makeJsonResponse(
        {
          error: {
            code: "validation_failed",
            message: "Generic validation",
            correlation_id: "c1",
          },
        },
        422,
      ),
    );

    let captured: ApiError | undefined;
    try {
      await apiPost("/api/test", { x: 1 });
    } catch (error) {
      captured = error as ApiError;
    }

    expect(captured).toBeInstanceOf(ApiError);
    expect(captured!.fields).toEqual([]);
    expect(captured!.fieldError("any")).toBeUndefined();
  });

  it("falls back to a default code/message when the envelope is missing (404)", async () => {
    // Server returns 404 with no body and no JSON Content-Type.
    fetchSpy.mockResolvedValueOnce(new Response(null, { status: 404 }));

    let captured: ApiError | undefined;
    try {
      await apiGet("/api/missing");
    } catch (error) {
      captured = error as ApiError;
    }

    expect(captured).toBeInstanceOf(ApiError);
    expect(captured!.status).toBe(404);
    expect(captured!.code).toBe("not_found");
    expect(captured!.message.length).toBeGreaterThan(0);
  });

  it("falls back to a default code on 500 with a non-JSON body", async () => {
    fetchSpy.mockResolvedValueOnce(
      new Response("Internal server error", {
        status: 500,
        headers: { "Content-Type": "text/plain" },
      }),
    );

    let captured: ApiError | undefined;
    try {
      await apiGet("/api/test");
    } catch (error) {
      captured = error as ApiError;
    }

    expect(captured).toBeInstanceOf(ApiError);
    expect(captured!.status).toBe(500);
    expect(typeof captured!.code).toBe("string");
    expect(captured!.code.length).toBeGreaterThan(0);
  });

  it("falls back to a default code when the body is not the envelope shape", async () => {
    // Body parses as JSON but does not have an "error" key.
    fetchSpy.mockResolvedValueOnce(makeJsonResponse({ unrelated: "thing" }, 403));

    let captured: ApiError | undefined;
    try {
      await apiGet("/api/forbidden");
    } catch (error) {
      captured = error as ApiError;
    }

    expect(captured).toBeInstanceOf(ApiError);
    expect(captured!.status).toBe(403);
    expect(captured!.code).toBe("forbidden");
  });

  it("synthesises an error from the X-Correlation-Id response header when envelope is absent", async () => {
    fetchSpy.mockResolvedValueOnce(
      new Response(null, {
        status: 502,
        headers: { "X-Correlation-Id": "corr-from-response-header" },
      }),
    );

    let captured: ApiError | undefined;
    try {
      await apiGet("/api/test");
    } catch (error) {
      captured = error as ApiError;
    }

    expect(captured).toBeInstanceOf(ApiError);
    expect(captured!.correlationId).toBe("corr-from-response-header");
  });
});

// ---------------------------------------------------------------------------
// Status code handling
//
// Each non-2xx status produces an ApiError carrying the documented
// status and code.
// ---------------------------------------------------------------------------

describe("Status code handling", () => {
  it("404: throws ApiError with status 404 and code 'not_found'", async () => {
    fetchSpy.mockResolvedValueOnce(
      makeJsonResponse({ error: { code: "not_found", message: "Not found" } }, 404),
    );
    await expect(apiGet("/api/missing")).rejects.toMatchObject({
      status: 404,
      code: "not_found",
    });
  });

  it("403: throws ApiError with status 403 and code 'forbidden'", async () => {
    fetchSpy.mockResolvedValueOnce(
      makeJsonResponse({ error: { code: "forbidden", message: "Forbidden" } }, 403),
    );
    await expect(apiGet("/api/admin/users")).rejects.toMatchObject({
      status: 403,
      code: "forbidden",
    });
  });

  it("400: throws ApiError with status 400 and default code 'bad_request'", async () => {
    fetchSpy.mockResolvedValueOnce(new Response(null, { status: 400 }));
    await expect(apiGet("/api/bad")).rejects.toMatchObject({
      status: 400,
      code: "bad_request",
    });
  });

  it("409: throws ApiError with status 409 and default code 'conflict'", async () => {
    fetchSpy.mockResolvedValueOnce(new Response(null, { status: 409 }));
    await expect(apiPost("/api/items", { dup: true })).rejects.toMatchObject({
      status: 409,
      code: "conflict",
    });
  });

  it("422: throws ApiError with status 422 and surfaces the fields entries", async () => {
    fetchSpy.mockResolvedValueOnce(
      makeJsonResponse(
        {
          error: {
            code: "validation_failed",
            message: "Bad",
            fields: [{ field: "email", code: "invalid_email", message: "Invalid" }],
          },
        },
        422,
      ),
    );

    let captured: ApiError | undefined;
    try {
      await apiPost("/api/test", { email: "bad" });
    } catch (error) {
      captured = error as ApiError;
    }

    expect(captured!.status).toBe(422);
    expect(captured!.fieldError("email")?.message).toBe("Invalid");
  });

  it("500: throws ApiError with status 500", async () => {
    fetchSpy.mockResolvedValueOnce(
      makeJsonResponse({ error: { code: "internal_error", message: "Crashed" } }, 500),
    );
    await expect(apiGet("/api/test")).rejects.toMatchObject({ status: 500 });
  });

  it("502: throws ApiError with default code 'bad_gateway'", async () => {
    fetchSpy.mockResolvedValueOnce(new Response(null, { status: 502 }));
    await expect(apiGet("/api/test")).rejects.toMatchObject({
      status: 502,
      code: "bad_gateway",
    });
  });

  it("503: throws ApiError preserving an explicit envelope code", async () => {
    fetchSpy.mockResolvedValueOnce(
      makeJsonResponse({ error: { code: "ai_unavailable", message: "AI unavailable" } }, 503),
    );
    await expect(apiPost("/api/notes/generate", { context: "x" })).rejects.toMatchObject({
      status: 503,
      code: "ai_unavailable",
    });
  });

  it("504: throws ApiError preserving the envelope code 'ai_timeout'", async () => {
    fetchSpy.mockResolvedValueOnce(
      makeJsonResponse({ error: { code: "ai_timeout", message: "AI timed out" } }, 504),
    );
    await expect(apiPost("/api/notes/generate", { context: "x" })).rejects.toMatchObject({
      status: 504,
      code: "ai_timeout",
    });
  });

  it("504: falls back to 'gateway_timeout' when no envelope is present", async () => {
    fetchSpy.mockResolvedValueOnce(new Response(null, { status: 504 }));
    await expect(apiGet("/api/test")).rejects.toMatchObject({
      status: 504,
      code: "gateway_timeout",
    });
  });

  it("429: throws ApiError with default code 'rate_limited'", async () => {
    fetchSpy.mockResolvedValueOnce(new Response(null, { status: 429 }));
    await expect(apiGet("/api/test")).rejects.toMatchObject({
      status: 429,
      code: "rate_limited",
    });
  });
});

// ---------------------------------------------------------------------------
// Network errors
//
// fetch rejection (TypeError for browser network failures, generic Error
// for Node/jsdom DNS, etc.) is wrapped in ApiError(0, "network_error").
// AbortError is special-cased and re-thrown unwrapped (see below).
// ---------------------------------------------------------------------------

describe("Network errors", () => {
  it("throws ApiError(status: 0, code: 'network_error') when fetch rejects with TypeError", async () => {
    fetchSpy.mockRejectedValueOnce(new TypeError("Failed to fetch"));

    let captured: ApiError | undefined;
    try {
      await apiGet("/api/test");
    } catch (error) {
      captured = error as ApiError;
    }

    expect(captured).toBeInstanceOf(ApiError);
    expect(captured!.status).toBe(0);
    expect(captured!.code).toBe("network_error");
  });

  it("treats a generic Error rejection as a network error", async () => {
    fetchSpy.mockRejectedValueOnce(new Error("Connection refused"));

    let captured: ApiError | undefined;
    try {
      await apiGet("/api/test");
    } catch (error) {
      captured = error as ApiError;
    }

    expect(captured).toBeInstanceOf(ApiError);
    expect(captured!.status).toBe(0);
    expect(captured!.code).toBe("network_error");
  });

  it("preserves the original cause on the wrapped network ApiError", async () => {
    const underlying = new TypeError("Failed to fetch");
    fetchSpy.mockRejectedValueOnce(underlying);

    let captured: ApiError | undefined;
    try {
      await apiGet("/api/test");
    } catch (error) {
      captured = error as ApiError;
    }

    expect(captured!.cause).toBe(underlying);
  });

  it("does NOT trigger 401-redirect for network errors (status is 0, not 401)", async () => {
    fetchSpy.mockRejectedValueOnce(new TypeError("Failed to fetch"));

    await expect(apiGet("/api/test")).rejects.toBeInstanceOf(ApiError);
    expect(replaceSpy).not.toHaveBeenCalled();
  });

  it("network error message reflects the underlying error message when available", async () => {
    fetchSpy.mockRejectedValueOnce(new Error("DNS lookup failed"));

    let captured: ApiError | undefined;
    try {
      await apiGet("/api/test");
    } catch (error) {
      captured = error as ApiError;
    }

    expect(captured!.message).toBe("DNS lookup failed");
  });
});

// ---------------------------------------------------------------------------
// AbortError passthrough
//
// fetch rejecting with a DOMException whose name is "AbortError" is
// re-thrown verbatim so TanStack Query / useEffect cleanup can detect
// cancellation by checking error.name === "AbortError".
// ---------------------------------------------------------------------------

describe("AbortError passthrough", () => {
  it("re-throws AbortError unchanged (NOT wrapped in ApiError)", async () => {
    const abortError = new DOMException("The operation was aborted", "AbortError");
    fetchSpy.mockRejectedValueOnce(abortError);

    let captured: unknown;
    try {
      await apiGet("/api/test");
    } catch (error) {
      captured = error;
    }

    expect(captured).not.toBeInstanceOf(ApiError);
    expect(captured).toBeInstanceOf(DOMException);
    expect((captured as DOMException).name).toBe("AbortError");
  });

  it("preserves the AbortError reference identity (no defensive cloning)", async () => {
    const abortError = new DOMException("Aborted", "AbortError");
    fetchSpy.mockRejectedValueOnce(abortError);

    let captured: unknown;
    try {
      await apiGet("/api/test");
    } catch (error) {
      captured = error;
    }

    expect(captured).toBe(abortError);
  });

  it("AbortError does NOT trigger 401-redirect (no replace call)", async () => {
    const abortError = new DOMException("Aborted", "AbortError");
    fetchSpy.mockRejectedValueOnce(abortError);

    try {
      await apiGet("/api/test");
    } catch {
      // expected
    }

    expect(replaceSpy).not.toHaveBeenCalled();
  });

  it("passes the supplied AbortSignal through to fetch", async () => {
    fetchSpy.mockResolvedValueOnce(makeJsonResponse({}));
    const controller = new AbortController();

    await apiGet("/api/test", { signal: controller.signal });

    const call = captureFetchCall();
    expect(call?.init.signal).toBe(controller.signal);
  });

  it("AbortError on POST is also re-thrown unwrapped", async () => {
    const abortError = new DOMException("Aborted", "AbortError");
    fetchSpy.mockRejectedValueOnce(abortError);

    let captured: unknown;
    try {
      await apiPost("/api/test", { x: 1 });
    } catch (error) {
      captured = error;
    }

    expect(captured).not.toBeInstanceOf(ApiError);
    expect((captured as DOMException).name).toBe("AbortError");
  });
});

// ---------------------------------------------------------------------------
// URL construction with VITE_API_BASE_URL
//
// Per the source: when API_BASE_URL is empty/"/" the path is returned
// unchanged so the Vite dev proxy intercepts /api/* and /auth/*. When
// API_BASE_URL is non-empty, the wrapper joins base + path with single
// slash semantics.
//
// In the test environment, VITE_API_BASE_URL is empty by default, so
// the path is passed verbatim.
// ---------------------------------------------------------------------------

describe("URL construction with VITE_API_BASE_URL", () => {
  it("passes a leading-slash path through verbatim when base URL is empty", async () => {
    fetchSpy.mockResolvedValueOnce(makeJsonResponse({}));
    await apiGet("/api/connections");
    const call = captureFetchCall();
    expect(call?.url).toContain("/api/connections");
  });

  it("does not double-prefix the path", async () => {
    fetchSpy.mockResolvedValueOnce(makeJsonResponse({}));
    await apiGet("/api/connections");
    const call = captureFetchCall();
    expect(call?.url).not.toContain("//api/connections");
  });

  it("passes absolute https:// URLs through unchanged", async () => {
    fetchSpy.mockResolvedValueOnce(makeJsonResponse({}));
    await apiGet("https://example.com/api/x");
    const call = captureFetchCall();
    expect(call?.url).toBe("https://example.com/api/x");
  });

  it("passes absolute http:// URLs through unchanged", async () => {
    fetchSpy.mockResolvedValueOnce(makeJsonResponse({}));
    await apiGet("http://localhost:5000/api/y");
    const call = captureFetchCall();
    expect(call?.url).toBe("http://localhost:5000/api/y");
  });

  it("preserves query strings on the path", async () => {
    fetchSpy.mockResolvedValueOnce(makeJsonResponse({}));
    await apiGet("/api/connections?company=Acme&involvement=Warm%20Intro");
    const call = captureFetchCall();
    expect(call?.url).toContain("?company=Acme&involvement=Warm%20Intro");
  });
});

// ---------------------------------------------------------------------------
// Custom headers via options.headers
//
// User-supplied headers merge with the wrapper's defaults. Per the
// source, options.headers spreads INTO the headers object after the
// defaults (Accept, X-Correlation-Id), so user values take precedence.
// However, Content-Type is set AFTER the spread when a body is present,
// so it is the wrapper's enforced value for body-carrying methods.
// ---------------------------------------------------------------------------

describe("Custom headers via options.headers", () => {
  it("merges custom headers with the X-Correlation-Id default", async () => {
    fetchSpy.mockResolvedValueOnce(makeJsonResponse({}, 201));
    await apiPost("/api/test", { x: 1 }, { headers: { "X-Custom": "value" } });
    const call = captureFetchCall();
    expect(getHeader(call!.init, "X-Custom")).toBe("value");
    expect(getHeader(call!.init, "X-Correlation-Id")).toBe(FIXED_CORRELATION_ID);
    expect(getHeader(call!.init, "Accept")).toBe("application/json");
  });

  it("retains the wrapper's Content-Type on body-carrying requests", async () => {
    fetchSpy.mockResolvedValueOnce(makeJsonResponse({}, 201));
    await apiPost("/api/test", { x: 1 }, { headers: { "X-Other": "1" } });
    const call = captureFetchCall();
    expect(getHeader(call!.init, "Content-Type")).toBe("application/json");
  });

  it("user-supplied X-Correlation-Id overrides the default (header precedence)", async () => {
    fetchSpy.mockResolvedValueOnce(makeJsonResponse({}));
    await apiGet("/api/test", { headers: { "X-Correlation-Id": "user-supplied-id" } });
    const call = captureFetchCall();
    expect(getHeader(call!.init, "X-Correlation-Id")).toBe("user-supplied-id");
  });

  it("custom headers on GET do not introduce a Content-Type", async () => {
    fetchSpy.mockResolvedValueOnce(makeJsonResponse({}));
    await apiGet("/api/test", { headers: { "X-Custom": "abc" } });
    const call = captureFetchCall();
    expect(getHeader(call!.init, "Content-Type")).toBeNull();
    expect(getHeader(call!.init, "X-Custom")).toBe("abc");
  });

  it("multiple custom headers are all forwarded", async () => {
    fetchSpy.mockResolvedValueOnce(makeJsonResponse({}));
    await apiGet("/api/test", {
      headers: { "X-A": "1", "X-B": "2", "X-C": "3" },
    });
    const call = captureFetchCall();
    expect(getHeader(call!.init, "X-A")).toBe("1");
    expect(getHeader(call!.init, "X-B")).toBe("2");
    expect(getHeader(call!.init, "X-C")).toBe("3");
  });
});

// ---------------------------------------------------------------------------
// Invalid response body handling (malformed JSON on 2xx)
// ---------------------------------------------------------------------------

describe("Invalid response body handling", () => {
  it("throws ApiError with code 'invalid_response_body' when 2xx body is malformed JSON", async () => {
    fetchSpy.mockResolvedValueOnce(
      new Response("not-json-{{{", {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    let captured: ApiError | undefined;
    try {
      await apiGet("/api/test");
    } catch (error) {
      captured = error as ApiError;
    }

    expect(captured).toBeInstanceOf(ApiError);
    expect(captured!.status).toBe(200);
    expect(captured!.code).toBe("invalid_response_body");
  });
});
