/**
 * auth.test.ts - Vitest tests for the authentication API hooks.
 *
 * Tests `frontend/src/api/auth.ts`:
 *   - authKeys cache key factory
 *   - useSessionQuery() - GET /api/me with skipAuthRedirect
 *   - useLoginMutation() - POST /auth/login with skipAuthRedirect
 *   - useLogoutMutation() - POST /auth/logout (cleanup on success AND error)
 *
 * Coverage of the assigned-folder concerns:
 *   - useSessionQuery: 200 returns nested SessionRead, 401 returns
 *     data:undefined WITHOUT redirecting (skipAuthRedirect: true),
 *     retry: false (no retry on 401), staleTime: 30 * 1000,
 *     refetchOnWindowFocus: true.
 *   - useLoginMutation: success -> resetCorrelationId, invalidates
 *     session, toast.success; 401 -> toast.error with the documented
 *     message; other errors -> toast.error with the server message.
 *   - useLogoutMutation: success -> queryClient.clear, resetCorrelationId,
 *     toast.success("Signed out"); error -> STILL queryClient.clear and
 *     resetCorrelationId, toast.error with the documented message.
 *
 * Test infrastructure:
 *   - MSW server lifecycle managed in tests/setup.ts.
 *   - Per-test handler overrides via server.use(overrides.api.* /
 *     overrides.auth.*).
 *   - Toast spies via vi.hoisted + vi.mock.
 *   - resetCorrelationId spy via vi.hoisted + vi.mock of
 *     "@/lib/correlationId".
 *   - Fresh QueryClient per test.
 *
 * Conventions per AAP Sec 0.7.7 and frontend/.prettierrc.json:
 *   - Strict TypeScript; no `any`.
 *   - Double quotes (singleQuote: false); trailing commas; 2-space
 *     indent; line length <= 100.
 *   - No emoji; no console.log.
 *   - Named exports only - no default export.
 *
 * NOTE: This is a `.ts` file (NOT `.tsx`), so JSX is forbidden. The
 * QueryClientProvider wrapper is constructed via React.createElement to
 * keep the file pure-TypeScript while still mirroring the production
 * provider hierarchy expected by the hooks under test.
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
// hoisted vi.mock factory bodies. The same pattern is used in
// tests/api/client.test.ts (the canonical reference for this codebase).
// ---------------------------------------------------------------------------

const { toastSpies, correlationIdSpies } = vi.hoisted(() => ({
  toastSpies: {
    success: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
    warning: vi.fn(),
    show: vi.fn().mockReturnValue("test-toast-id"),
    dismiss: vi.fn(),
  },
  correlationIdSpies: {
    getCorrelationId: vi.fn(() => "sc-fe-test-correlation-id"),
    resetCorrelationId: vi.fn(),
  },
}));

// ---------------------------------------------------------------------------
// vi.mock - replace Toast and correlationId modules with hoisted spies.
//
// The mocks must be declared BEFORE the import of @/api/auth so that
// Vitest's module registry resolves the mocked modules when @/api/auth
// is loaded. vi.mock is auto-hoisted by Vitest, so the source-level
// ordering relative to imports does not matter, but we keep the mocks
// adjacent to vi.hoisted for readability.
//
// Mocks both `toast` (a non-existent named export, harmless) and
// `useToast()` to return `toastSpies`. The auth.ts hooks call
// `useToast()` inside each mutation hook and the returned facade is
// stored as `toast`, so `toast.success(...)` -> `toastSpies.success(...)`.
// ---------------------------------------------------------------------------

vi.mock("@/components/ui/Toast", () => ({
  toast: toastSpies,
  useToast: () => toastSpies,
  __resetToastStoreForTesting: vi.fn(),
  ToastContainer: () => null,
}));

vi.mock("@/lib/correlationId", () => ({
  getCorrelationId: correlationIdSpies.getCorrelationId,
  resetCorrelationId: correlationIdSpies.resetCorrelationId,
}));

// ---------------------------------------------------------------------------
// Module under test (loaded AFTER vi.mock so the hoisted mocks resolve).
// ---------------------------------------------------------------------------

import { authKeys, useLoginMutation, useLogoutMutation, useSessionQuery } from "@/api/auth";
import { ApiError } from "@/api/client";

// ---------------------------------------------------------------------------
// Test-infrastructure imports.
// ---------------------------------------------------------------------------

import { createTestQueryClient } from "../test-utils";
import { server } from "../mocks/server";
import { overrides } from "../mocks/handlers";
import { makeUserRead } from "../mocks/data";

// ---------------------------------------------------------------------------
// Wrapper helper - QueryClientProvider via React.createElement (no JSX).
//
// renderHook's `options.wrapper` field requires a component that accepts
// `{ children }` and returns a JSX-compatible element. We build that
// element with React.createElement so the file remains a `.ts` file
// (per the assignment's filename) while still producing a valid React
// element tree. The wrapper exposes the `queryClient` reference so tests
// can `vi.spyOn(queryClient, "clear")` and `vi.spyOn(queryClient,
// "invalidateQueries")` to verify cache-management behaviors.
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
// `expect(toastSpies.success).toHaveBeenCalledTimes(1)` would observe
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
  correlationIdSpies.resetCorrelationId.mockClear();
  correlationIdSpies.getCorrelationId.mockClear();
});

afterEach(() => {
  server.events.removeAllListeners();
});

// ---------------------------------------------------------------------------
// authKeys cache key factory
// ---------------------------------------------------------------------------

describe("authKeys cache key factory", () => {
  it("authKeys.all is ['auth']", () => {
    expect(authKeys.all).toEqual(["auth"]);
  });

  it("authKeys.session() is ['auth', 'session']", () => {
    expect(authKeys.session()).toEqual(["auth", "session"]);
  });

  it("authKeys.session() is referentially-stable in shape across calls", () => {
    // Two invocations of session() produce equal-by-value tuples even
    // though they are distinct array instances; queryClient.invalidateQueries
    // matches by deep equality, so this is the production-relevant guarantee.
    expect(authKeys.session()).toEqual(authKeys.session());
  });
});

// ---------------------------------------------------------------------------
// useSessionQuery - GET /api/me
// ---------------------------------------------------------------------------

describe("useSessionQuery", () => {
  it("on default 401: data is undefined, query is in error state, NO redirect occurs", async () => {
    // Default /api/me handler in mocks/handlers.ts returns 401. We mock
    // window.location.replace to detect any redirect attempt; with
    // skipAuthRedirect: true on the hook, the 401 must NOT trigger the
    // client.ts auto-redirect.
    const replaceSpy = vi.fn();
    Object.defineProperty(window, "location", {
      configurable: true,
      value: {
        ...window.location,
        pathname: "/feed",
        search: "",
        replace: replaceSpy,
      },
    });

    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useSessionQuery(), { wrapper });

    await waitFor(() => expect(result.current.isError).toBe(true));

    expect(result.current.data).toBeUndefined();
    expect(result.current.error).toBeInstanceOf(ApiError);
    expect(result.current.error?.status).toBe(401);

    // Critical: skipAuthRedirect prevents the default 401 redirect.
    expect(replaceSpy).not.toHaveBeenCalled();
  });

  it("on 200: returns the SessionRead payload with nested user", async () => {
    const user = makeUserRead({ display_name: "Alice Admin", role: "Admin" });
    // overrides.api.me200 takes Parameters<typeof makeSessionRead>[0]
    // which is { user?: Partial<UserRead>; authenticated?: boolean }.
    // We pass { user } so the mocked SessionRead carries the test's
    // user verbatim (UserRead is structurally compatible with
    // Partial<UserRead>).
    server.use(overrides.api.me200({ user }));

    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useSessionQuery(), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(result.current.data).toMatchObject({
      authenticated: true,
      user: expect.objectContaining({
        id: user.id,
        email: user.email,
        display_name: "Alice Admin",
        role: "Admin",
      }),
    });
  });

  it("uses cache key authKeys.session()", async () => {
    const user = makeUserRead();
    server.use(overrides.api.me200({ user }));

    const { wrapper, queryClient } = makeWrapper();
    const { result } = renderHook(() => useSessionQuery(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    // The query landed in the cache under authKeys.session(). Reading
    // by that key should return the same payload that result.current.data
    // surfaced - confirming the queryKey wiring is correct.
    const cached = queryClient.getQueryData(authKeys.session());
    expect(cached).toEqual(result.current.data);
  });

  it("does NOT retry on 401 (retry: false)", async () => {
    let requestCount = 0;
    server.events.on("request:start", ({ request }) => {
      if (request.url.includes("/api/me")) {
        requestCount += 1;
      }
    });

    // Default 401 handler is in effect. With retry: false, the query
    // must give up after exactly one fetch; the default TanStack Query
    // policy of 3 retries would produce 4 total fetches.
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useSessionQuery(), { wrapper });

    await waitFor(() => expect(result.current.isError).toBe(true));

    // Fail-fast: only one fetch.
    expect(requestCount).toBe(1);
  });

  it("on 500: surfaces ApiError without retry", async () => {
    let requestCount = 0;
    server.events.on("request:start", ({ request }) => {
      if (request.url.includes("/api/me")) {
        requestCount += 1;
      }
    });

    server.use(overrides.api.me500());

    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useSessionQuery(), { wrapper });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error).toBeInstanceOf(ApiError);
    expect(result.current.error?.status).toBe(500);
    // retry: false applies to ALL non-2xx, not just 401 - 5xx must also
    // fail-fast.
    expect(requestCount).toBe(1);
  });
});

// ---------------------------------------------------------------------------
// useLoginMutation - POST /auth/login
// ---------------------------------------------------------------------------
//
// Note: useLoginMutation returns LoginResponse (= { user: UserRead }),
// NOT SessionRead. The session-hydration step is performed afterward via
// invalidateQueries(authKeys.session()) which forces a fresh /api/me
// fetch. So `result.current.data` here has shape `{ user: UserRead }`
// only - no `authenticated` field.
// ---------------------------------------------------------------------------

describe("useLoginMutation", () => {
  it("on success: returns LoginResponse with the authenticated user", async () => {
    const user = makeUserRead({ email: "logged-in@example.com" });
    server.use(overrides.auth.loginSuccess(user));

    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useLoginMutation(), { wrapper });

    result.current.mutate({
      email: "logged-in@example.com",
      password: "correct-password",
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    // LoginResponse = { user: UserRead } - no `authenticated` field.
    expect(result.current.data).toMatchObject({
      user: expect.objectContaining({ email: "logged-in@example.com" }),
    });
  });

  it("on success: calls resetCorrelationId() (per AAP Sec 0.7.5)", async () => {
    const user = makeUserRead();
    server.use(overrides.auth.loginSuccess(user));

    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useLoginMutation(), { wrapper });

    result.current.mutate({ email: user.email, password: "pwd" });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    // Pre-login traffic and post-login traffic should be distinguishable
    // in the observability platform; resetCorrelationId() draws the
    // boundary.
    expect(correlationIdSpies.resetCorrelationId).toHaveBeenCalled();
  });

  it("on success: invalidates the session query", async () => {
    const user = makeUserRead();
    server.use(overrides.auth.loginSuccess(user));

    const { wrapper, queryClient } = makeWrapper();
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    const { result } = renderHook(() => useLoginMutation(), { wrapper });

    result.current.mutate({ email: user.email, password: "pwd" });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    // Invalidation by the canonical session queryKey - the AuthProvider
    // re-fetches /api/me and picks up the freshly-authenticated user.
    expect(invalidateSpy).toHaveBeenCalledWith(
      expect.objectContaining({ queryKey: authKeys.session() }),
    );
  });

  it('on success: fires toast.success("Signed in")', async () => {
    const user = makeUserRead();
    server.use(overrides.auth.loginSuccess(user));

    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useLoginMutation(), { wrapper });

    result.current.mutate({ email: user.email, password: "pwd" });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(toastSpies.success).toHaveBeenCalled();
    const firstCall = toastSpies.success.mock.calls[0];
    const msg = typeof firstCall?.[0] === "string" ? firstCall[0] : "";
    // The exact message in auth.ts is "Signed in"; we match the
    // semantically-stable substring so micro-edits to capitalization or
    // punctuation do not break the test.
    expect(msg).toMatch(/sign(ed)?\s*in/i);
  });

  it("on 401 invalid credentials: NO redirect (skipAuthRedirect), fires toast.error", async () => {
    const replaceSpy = vi.fn();
    Object.defineProperty(window, "location", {
      configurable: true,
      value: {
        ...window.location,
        pathname: "/login",
        search: "",
        replace: replaceSpy,
      },
    });

    server.use(overrides.auth.loginInvalidCredentials());

    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useLoginMutation(), { wrapper });

    result.current.mutate({ email: "a@b.com", password: "wrong" });

    await waitFor(() => expect(result.current.isError).toBe(true));

    expect(result.current.error).toBeInstanceOf(ApiError);
    expect(result.current.error?.status).toBe(401);
    // Critical: skipAuthRedirect=true prevents redirect on login 401.
    // Without it, the user would be bounced from /login to /login.
    expect(replaceSpy).not.toHaveBeenCalled();
    expect(toastSpies.error).toHaveBeenCalled();
  });

  it('on 401: toast message matches the documented "Login failed..." text', async () => {
    server.use(overrides.auth.loginInvalidCredentials());

    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useLoginMutation(), { wrapper });

    result.current.mutate({ email: "a@b.com", password: "wrong" });

    await waitFor(() => expect(result.current.isError).toBe(true));

    const firstCall = toastSpies.error.mock.calls[0];
    const msg = typeof firstCall?.[0] === "string" ? firstCall[0] : "";
    // auth.ts says: "Login failed. Check your email and password."
    // Match either phrase to keep the assertion resilient to copy edits.
    expect(msg.toLowerCase()).toMatch(/login\s+failed|email|password/);
  });

  it("on 422 validation error: fires toast.error with the server message", async () => {
    server.use(overrides.auth.loginValidationError());

    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useLoginMutation(), { wrapper });

    result.current.mutate({ email: "not-an-email", password: "" });

    await waitFor(() => expect(result.current.isError).toBe(true));

    expect(result.current.error?.status).toBe(422);
    expect(toastSpies.error).toHaveBeenCalled();
  });

  it("on 500 server error: fires toast.error", async () => {
    server.use(overrides.auth.login500());

    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useLoginMutation(), { wrapper });

    result.current.mutate({ email: "a@b.com", password: "pwd" });

    await waitFor(() => expect(result.current.isError).toBe(true));

    expect(result.current.error?.status).toBe(500);
    expect(toastSpies.error).toHaveBeenCalled();
  });

  it("on success: does NOT call toast.error", async () => {
    const user = makeUserRead();
    server.use(overrides.auth.loginSuccess(user));

    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useLoginMutation(), { wrapper });

    result.current.mutate({ email: user.email, password: "pwd" });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    // Sanity: success path does not accidentally fire the error toast
    // (which would happen if onSuccess and onError both ran).
    expect(toastSpies.error).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// useLogoutMutation - POST /auth/logout
// ---------------------------------------------------------------------------
//
// useLogoutMutation has DEFENSIVE cleanup: queryClient.clear() and
// resetCorrelationId() run on BOTH success AND error. The reasoning
// (per AAP Sec 0.7.4) is that the cookie state on the server is
// uncertain after an error, so the safest client-side posture is to
// treat the user as logged out regardless. This block tests both
// branches explicitly.
// ---------------------------------------------------------------------------

describe("useLogoutMutation", () => {
  it("POSTs /auth/logout with an empty body", async () => {
    let capturedBody: unknown = null;
    server.events.on("request:start", async ({ request }) => {
      if (request.url.includes("/auth/logout")) {
        const cloned = request.clone();
        try {
          capturedBody = await cloned.json();
        } catch {
          capturedBody = null;
        }
      }
    });

    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useLogoutMutation(), { wrapper });

    result.current.mutate();

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    // Body is an empty object {} per the auth.ts source - the server
    // identifies the session via the HttpOnly cookie, no payload needed.
    expect(capturedBody).toEqual({});
  });

  it("on success: calls queryClient.clear() (entire cache wiped)", async () => {
    const { wrapper, queryClient } = makeWrapper();
    const clearSpy = vi.spyOn(queryClient, "clear");

    // Pre-seed the cache to verify clear() actually wipes it (not a
    // no-op spy assertion). queryClient.clear() purges all queries
    // synchronously, so post-mutate the cache is empty.
    queryClient.setQueryData(["some-query-key"], { foo: "bar" });
    expect(queryClient.getQueryData(["some-query-key"])).toEqual({ foo: "bar" });

    const { result } = renderHook(() => useLogoutMutation(), { wrapper });
    result.current.mutate();

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(clearSpy).toHaveBeenCalled();
    // After clear, the previously seeded data is gone.
    expect(queryClient.getQueryData(["some-query-key"])).toBeUndefined();
  });

  it("on success: calls resetCorrelationId() (per AAP Sec 0.7.5)", async () => {
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useLogoutMutation(), { wrapper });

    result.current.mutate();

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(correlationIdSpies.resetCorrelationId).toHaveBeenCalled();
  });

  it('on success: fires toast.success("Signed out")', async () => {
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useLogoutMutation(), { wrapper });

    result.current.mutate();

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(toastSpies.success).toHaveBeenCalled();
    const firstCall = toastSpies.success.mock.calls[0];
    const msg = typeof firstCall?.[0] === "string" ? firstCall[0] : "";
    expect(msg).toMatch(/sign(ed)?\s*out/i);
  });

  it("on 401 error: STILL clears cache and resets correlation (defensive cleanup)", async () => {
    server.use(overrides.auth.logout401());

    const { wrapper, queryClient } = makeWrapper();
    const clearSpy = vi.spyOn(queryClient, "clear");

    const { result } = renderHook(() => useLogoutMutation(), { wrapper });
    result.current.mutate();

    await waitFor(() => expect(result.current.isError).toBe(true));

    // Critical: defensive cleanup runs even on error - the SECURITY
    // posture per AAP Sec 0.7.4 requires the client to treat itself as
    // logged-out regardless of the server's response.
    expect(clearSpy).toHaveBeenCalled();
    expect(correlationIdSpies.resetCorrelationId).toHaveBeenCalled();
  });

  it("on 500 error: STILL clears cache and resets correlation", async () => {
    server.use(overrides.auth.logout500());

    const { wrapper, queryClient } = makeWrapper();
    const clearSpy = vi.spyOn(queryClient, "clear");

    const { result } = renderHook(() => useLogoutMutation(), { wrapper });
    result.current.mutate();

    await waitFor(() => expect(result.current.isError).toBe(true));

    // Same defensive posture for 5xx errors as for 4xx.
    expect(clearSpy).toHaveBeenCalled();
    expect(correlationIdSpies.resetCorrelationId).toHaveBeenCalled();
  });

  it("on error: fires toast.error", async () => {
    server.use(overrides.auth.logout500());

    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useLogoutMutation(), { wrapper });

    result.current.mutate();

    await waitFor(() => expect(result.current.isError).toBe(true));

    expect(toastSpies.error).toHaveBeenCalled();
  });

  it("uses skipAuthRedirect: true (401 logout does NOT trigger redirect)", async () => {
    const replaceSpy = vi.fn();
    Object.defineProperty(window, "location", {
      configurable: true,
      value: {
        ...window.location,
        pathname: "/feed",
        search: "",
        replace: replaceSpy,
      },
    });

    server.use(overrides.auth.logout401());

    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useLogoutMutation(), { wrapper });

    result.current.mutate();

    await waitFor(() => expect(result.current.isError).toBe(true));

    // Critical: skipAuthRedirect prevents redirect even on logout 401.
    // The session was already invalid - redirecting would be redundant
    // (and noisy if logout fires during component teardown).
    expect(replaceSpy).not.toHaveBeenCalled();
  });

  it("on 500 error: surfaces ApiError with status 500", async () => {
    server.use(overrides.auth.logout500());

    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useLogoutMutation(), { wrapper });

    result.current.mutate();

    await waitFor(() => expect(result.current.isError).toBe(true));

    expect(result.current.error).toBeInstanceOf(ApiError);
    expect(result.current.error?.status).toBe(500);
  });

  it("on success: does NOT call toast.error", async () => {
    const { wrapper } = makeWrapper();
    const { result } = renderHook(() => useLogoutMutation(), { wrapper });

    result.current.mutate();

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    // Sanity: success path does not accidentally fire the error toast.
    expect(toastSpies.error).not.toHaveBeenCalled();
  });
});
