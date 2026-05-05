/**
 * AuthProvider.test.tsx - Vitest tests for the session and role context provider.
 *
 * Targets `frontend/src/auth/AuthProvider.tsx`. The provider exposes four
 * hooks plus the AuthProvider component itself. These tests verify:
 *
 *   - <AuthProvider> renders children unconditionally (gating is in
 *     consumers, not the provider).
 *   - useSession() returns null when sessionQuery is pending OR errored;
 *     returns SessionRead when sessionQuery is successful.
 *   - useSessionLoading() reflects sessionQuery.isPending exactly.
 *   - useRole() returns { role: UserRole | null, has: (target) => boolean }
 *     where role comes from session.user.role (NESTED shape) and has is
 *     exact-match.
 *   - useLogout() returns the result of useLogoutMutation() (a thin
 *     wrapper).
 *   - Hooks throw a clear error when used outside <AuthProvider> (sentinel
 *     undefined context detection).
 *   - Context value is memoized: identity-equal across re-renders that
 *     do not change the four observable query fields.
 *
 * Strategy:
 *   - Mock @/api/auth so useSessionQuery and useLogoutMutation return
 *     synchronous, deterministic values. Real TanStack Query is wrapped
 *     in a fresh QueryClient for each test to ensure cache isolation.
 *   - Render the REAL AuthProvider (not mocked). Use renderHook for
 *     hook-only tests.
 *   - Use vi.spyOn(console, "error").mockImplementation(() => {}) when
 *     expecting hooks to throw; React logs the error to console even
 *     when the test catches it.
 *
 * Conventions per AAP Sec 0.7.7 and frontend/.prettierrc.json:
 *   - Strict TypeScript; eslint allows `any` in tests but the project
 *     still avoids it - the only escape hatch is the documented
 *     `as unknown as` double-cast on the QueryResult mock shape.
 *   - Double quotes; trailing commas; 2-space indent; line length <= 100.
 *   - No emoji; no console.log.
 *   - Named imports only.
 */

import { type JSX, type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, renderHook, screen } from "@testing-library/react";

import {
  AuthProvider,
  useLogout,
  useRole,
  useSession,
  useSessionLoading,
  type UserRole,
} from "@/auth/AuthProvider";
import { useLogoutMutation, useSessionQuery } from "@/api/auth";
import type { SessionRead } from "@/schemas/auth";

import { makeSessionRead } from "../mocks/data";

// ---------------------------------------------------------------------------
// Module-scope vi.mock for @/api/auth
// ---------------------------------------------------------------------------
//
// Hoisted by vitest to the top of the module so the mock is in place
// before AuthProvider's static import of @/api/auth resolves. The
// `await vi.importActual(...)` preserves transitive exports
// (authKeys, useLoginMutation) so any code path that imports them
// continues to see the real implementation.

vi.mock("@/api/auth", async () => {
  const actual = await vi.importActual<typeof import("@/api/auth")>("@/api/auth");
  return {
    ...actual,
    useSessionQuery: vi.fn(),
    useLogoutMutation: vi.fn(),
  };
});

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Build a fresh QueryClient with retry disabled and gcTime/staleTime set
 * to 0 so the cache cannot leak between tests. Each test should construct
 * its own client to avoid stale data fanning across cases in the same
 * worker.
 */
function createTestQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        gcTime: 0,
        staleTime: 0,
        refetchOnWindowFocus: false,
        refetchOnReconnect: false,
      },
      mutations: {
        retry: false,
      },
    },
  });
}

/**
 * Wrap children in QueryClientProvider + AuthProvider for renderHook.
 * Returns a stable wrapper component bound to the supplied client so
 * the same client is used across renderHook calls in the same test.
 */
function withProviders(client: QueryClient): (props: { children: ReactNode }) => JSX.Element {
  return function Wrapper({ children }: { children: ReactNode }): JSX.Element {
    return (
      <QueryClientProvider client={client}>
        <AuthProvider>{children}</AuthProvider>
      </QueryClientProvider>
    );
  };
}

/**
 * Type alias for the session query mock's return type. Using
 * `ReturnType<typeof useSessionQuery>` keeps the helper aligned with the
 * exact contract published by `@/api/auth` even if the underlying
 * UseQueryResult variants drift in future @tanstack/react-query releases.
 */
type SessionQueryShape = ReturnType<typeof useSessionQuery>;

/**
 * Build a partial UseQueryResult shape mimicking the four observable
 * states. AuthProvider only reads four fields off this shape - isSuccess,
 * data, isPending, isError - so the helper only populates those plus a
 * minimal scaffold for completeness. The double-cast (as unknown as
 * SessionQueryShape) bypasses TanStack Query's strict discriminated-
 * union variance check; producing a complete object that satisfies every
 * variant is impractical and unnecessary for these tests.
 */
function makeQueryResult(
  state: "pending" | "success" | "error",
  data?: SessionRead,
): SessionQueryShape {
  const base = {
    data: undefined,
    error: null,
    isError: false,
    isPending: false,
    isSuccess: false,
    isFetching: false,
    isLoading: false,
    isLoadingError: false,
    isPaused: false,
    isPlaceholderData: false,
    isRefetchError: false,
    isRefetching: false,
    isStale: false,
    refetch: vi.fn(),
    status: "success" as const,
    fetchStatus: "idle" as const,
    dataUpdatedAt: 0,
    errorUpdatedAt: 0,
    failureCount: 0,
    failureReason: null,
    errorUpdateCount: 0,
    isFetched: false,
    isFetchedAfterMount: false,
    isInitialLoading: false,
    promise: Promise.resolve(undefined),
  };

  if (state === "pending") {
    return {
      ...base,
      isPending: true,
      isLoading: true,
      status: "pending",
      fetchStatus: "fetching",
    } as unknown as SessionQueryShape;
  }

  if (state === "success") {
    return {
      ...base,
      isSuccess: true,
      data,
      status: "success",
      isFetched: true,
      isFetchedAfterMount: true,
      promise: Promise.resolve(data),
    } as unknown as SessionQueryShape;
  }

  // state === "error"
  return {
    ...base,
    isError: true,
    error: new Error("Mock 401"),
    status: "error",
    isFetched: true,
    isFetchedAfterMount: true,
  } as unknown as SessionQueryShape;
}

/**
 * Configure the mocked useSessionQuery to return the supplied state.
 * Single-line wrapper kept as a helper so test bodies stay readable.
 */
function mockSessionQuery(state: "pending" | "success" | "error", data?: SessionRead): void {
  vi.mocked(useSessionQuery).mockReturnValue(makeQueryResult(state, data));
}

/**
 * Configure the mocked useLogoutMutation to return a basic mutation
 * result shape. Returns the inner mutate / mutateAsync handles so the
 * caller can assert that AuthProvider's useLogout passes them through
 * unchanged (identity-equal).
 */
function mockLogoutMutation(): {
  mutate: ReturnType<typeof vi.fn>;
  mutateAsync: ReturnType<typeof vi.fn>;
} {
  const mutate = vi.fn();
  const mutateAsync = vi.fn(async () => ({ status: "ok" }));
  const result = {
    mutate,
    mutateAsync,
    reset: vi.fn(),
    data: undefined,
    error: null,
    isError: false,
    isIdle: true,
    isPending: false,
    isPaused: false,
    isSuccess: false,
    failureCount: 0,
    failureReason: null,
    status: "idle" as const,
    submittedAt: 0,
    variables: undefined,
    context: undefined,
  };
  vi.mocked(useLogoutMutation).mockReturnValue(
    result as unknown as ReturnType<typeof useLogoutMutation>,
  );
  return { mutate, mutateAsync };
}

// ---------------------------------------------------------------------------
// Setup and teardown
// ---------------------------------------------------------------------------

beforeEach(() => {
  vi.mocked(useSessionQuery).mockReset();
  vi.mocked(useLogoutMutation).mockReset();
  // Default seed: no session, error state. Tests override per-case
  // with mockSessionQuery("pending"|"success", session) as needed.
  mockSessionQuery("error");
  mockLogoutMutation();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// <AuthProvider /> component
// ---------------------------------------------------------------------------

describe("<AuthProvider />", () => {
  it("renders children unconditionally (does not gate on session)", () => {
    mockSessionQuery("pending");
    const client = createTestQueryClient();
    render(
      <QueryClientProvider client={client}>
        <AuthProvider>
          <div data-testid="child">Child content</div>
        </AuthProvider>
      </QueryClientProvider>,
    );
    expect(screen.getByTestId("child")).toBeInTheDocument();
  });

  it("renders children when session is null and query errored", () => {
    mockSessionQuery("error");
    const client = createTestQueryClient();
    render(
      <QueryClientProvider client={client}>
        <AuthProvider>
          <div data-testid="child">Child content</div>
        </AuthProvider>
      </QueryClientProvider>,
    );
    expect(screen.getByTestId("child")).toBeInTheDocument();
  });

  it("renders children when session is hydrated (success state)", () => {
    mockSessionQuery("success", makeSessionRead());
    const client = createTestQueryClient();
    render(
      <QueryClientProvider client={client}>
        <AuthProvider>
          <div data-testid="child">Child content</div>
        </AuthProvider>
      </QueryClientProvider>,
    );
    expect(screen.getByTestId("child")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// useSession()
// ---------------------------------------------------------------------------

describe("useSession()", () => {
  it("returns null while session query is pending", () => {
    mockSessionQuery("pending");
    const { result } = renderHook(() => useSession(), {
      wrapper: withProviders(createTestQueryClient()),
    });
    expect(result.current).toBeNull();
  });

  it("returns null when session query errored (e.g., 401)", () => {
    mockSessionQuery("error");
    const { result } = renderHook(() => useSession(), {
      wrapper: withProviders(createTestQueryClient()),
    });
    expect(result.current).toBeNull();
  });

  it("returns the session object when query succeeds", () => {
    const session = makeSessionRead({ user: { role: "Admin" } });
    mockSessionQuery("success", session);
    const { result } = renderHook(() => useSession(), {
      wrapper: withProviders(createTestQueryClient()),
    });
    expect(result.current).not.toBeNull();
    expect(result.current).toEqual(session);
  });

  it("returns the NESTED session shape (session.user.role accessor works)", () => {
    const session = makeSessionRead({ user: { role: "Contributor" } });
    mockSessionQuery("success", session);
    const { result } = renderHook(() => useSession(), {
      wrapper: withProviders(createTestQueryClient()),
    });
    // Verify the nested access path matches the backend response shape.
    // useRole() reads session.user.role specifically; a flat session.role
    // accessor would silently break role gating across the SPA.
    expect(result.current?.user.role).toBe("Contributor");
    expect(result.current?.user.id).toBeTruthy();
    expect(result.current?.user.email).toBeTruthy();
    expect(result.current?.authenticated).toBe(true);
  });

  it("throws when used outside <AuthProvider>", () => {
    // Suppress the React error log emitted when an error escapes a render.
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});

    expect(() => {
      renderHook(() => useSession());
    }).toThrow(/must be used inside <AuthProvider>/);

    errorSpy.mockRestore();
  });
});

// ---------------------------------------------------------------------------
// useSessionLoading()
// ---------------------------------------------------------------------------

describe("useSessionLoading()", () => {
  it("returns true while session query is pending", () => {
    mockSessionQuery("pending");
    const { result } = renderHook(() => useSessionLoading(), {
      wrapper: withProviders(createTestQueryClient()),
    });
    expect(result.current).toBe(true);
  });

  it("returns false when session query has succeeded", () => {
    mockSessionQuery("success", makeSessionRead());
    const { result } = renderHook(() => useSessionLoading(), {
      wrapper: withProviders(createTestQueryClient()),
    });
    expect(result.current).toBe(false);
  });

  it("returns false when session query has errored", () => {
    mockSessionQuery("error");
    const { result } = renderHook(() => useSessionLoading(), {
      wrapper: withProviders(createTestQueryClient()),
    });
    expect(result.current).toBe(false);
  });

  it("throws when used outside <AuthProvider>", () => {
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    expect(() => {
      renderHook(() => useSessionLoading());
    }).toThrow(/must be used inside <AuthProvider>/);
    errorSpy.mockRestore();
  });
});

// ---------------------------------------------------------------------------
// useRole()
// ---------------------------------------------------------------------------

describe("useRole()", () => {
  it("returns role=null and has() that always returns false when no session", () => {
    mockSessionQuery("error");
    const { result } = renderHook(() => useRole(), {
      wrapper: withProviders(createTestQueryClient()),
    });
    expect(result.current.role).toBeNull();
    expect(result.current.has("Admin")).toBe(false);
    expect(result.current.has("Contributor")).toBe(false);
    expect(result.current.has("Viewer")).toBe(false);
  });

  it("returns role=null while session query is pending", () => {
    mockSessionQuery("pending");
    const { result } = renderHook(() => useRole(), {
      wrapper: withProviders(createTestQueryClient()),
    });
    expect(result.current.role).toBeNull();
    expect(result.current.has("Admin")).toBe(false);
  });

  it('returns role="Admin" when session.user.role is Admin', () => {
    mockSessionQuery("success", makeSessionRead({ user: { role: "Admin" } }));
    const { result } = renderHook(() => useRole(), {
      wrapper: withProviders(createTestQueryClient()),
    });
    expect(result.current.role).toBe("Admin");
  });

  it('returns role="Contributor" when session.user.role is Contributor', () => {
    mockSessionQuery("success", makeSessionRead({ user: { role: "Contributor" } }));
    const { result } = renderHook(() => useRole(), {
      wrapper: withProviders(createTestQueryClient()),
    });
    expect(result.current.role).toBe("Contributor");
  });

  it('returns role="Viewer" when session.user.role is Viewer', () => {
    mockSessionQuery("success", makeSessionRead({ user: { role: "Viewer" } }));
    const { result } = renderHook(() => useRole(), {
      wrapper: withProviders(createTestQueryClient()),
    });
    expect(result.current.role).toBe("Viewer");
  });

  it("has(target) is exact-match: Admin does NOT match Contributor", () => {
    mockSessionQuery("success", makeSessionRead({ user: { role: "Admin" } }));
    const { result } = renderHook(() => useRole(), {
      wrapper: withProviders(createTestQueryClient()),
    });
    expect(result.current.has("Admin")).toBe(true);
    expect(result.current.has("Contributor")).toBe(false);
    expect(result.current.has("Viewer")).toBe(false);
  });

  it("has(target) is exact-match for Contributor", () => {
    mockSessionQuery("success", makeSessionRead({ user: { role: "Contributor" } }));
    const { result } = renderHook(() => useRole(), {
      wrapper: withProviders(createTestQueryClient()),
    });
    expect(result.current.has("Admin")).toBe(false);
    expect(result.current.has("Contributor")).toBe(true);
    expect(result.current.has("Viewer")).toBe(false);
  });

  it("has(target) is exact-match for Viewer", () => {
    mockSessionQuery("success", makeSessionRead({ user: { role: "Viewer" } }));
    const { result } = renderHook(() => useRole(), {
      wrapper: withProviders(createTestQueryClient()),
    });
    expect(result.current.has("Admin")).toBe(false);
    expect(result.current.has("Contributor")).toBe(false);
    expect(result.current.has("Viewer")).toBe(true);
  });

  it("returns a memoized object: identity is stable across re-renders when role does not change", () => {
    mockSessionQuery("success", makeSessionRead({ user: { role: "Admin" } }));
    const { result, rerender } = renderHook(() => useRole(), {
      wrapper: withProviders(createTestQueryClient()),
    });
    const firstReturn = result.current;
    rerender();
    expect(result.current).toBe(firstReturn);
  });

  it("throws when used outside <AuthProvider>", () => {
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    expect(() => {
      renderHook(() => useRole());
    }).toThrow(/must be used inside <AuthProvider>/);
    errorSpy.mockRestore();
  });
});

// ---------------------------------------------------------------------------
// useLogout()
// ---------------------------------------------------------------------------

describe("useLogout()", () => {
  it("returns an object with mutateAsync and isPending", () => {
    mockSessionQuery("error");
    mockLogoutMutation();

    const { result } = renderHook(() => useLogout(), {
      wrapper: withProviders(createTestQueryClient()),
    });

    expect(typeof result.current.mutateAsync).toBe("function");
    expect(typeof result.current.isPending).toBe("boolean");
  });

  it("exposes the mutation status fields (isPending, isError, etc.)", () => {
    mockSessionQuery("error");
    mockLogoutMutation();

    const { result } = renderHook(() => useLogout(), {
      wrapper: withProviders(createTestQueryClient()),
    });

    // The returned object should have the standard UseMutationResult shape.
    expect(result.current).toHaveProperty("mutate");
    expect(result.current).toHaveProperty("mutateAsync");
    expect(result.current).toHaveProperty("reset");
    expect(result.current).toHaveProperty("isPending");
    expect(result.current).toHaveProperty("isError");
    expect(result.current).toHaveProperty("isSuccess");
    expect(result.current).toHaveProperty("isIdle");
  });

  it("mutateAsync() is callable and returns a Promise", async () => {
    mockSessionQuery("error");
    mockLogoutMutation();

    const { result } = renderHook(() => useLogout(), {
      wrapper: withProviders(createTestQueryClient()),
    });

    const ret = result.current.mutateAsync();
    expect(ret).toBeInstanceOf(Promise);
    await ret;
  });

  it("does NOT throw when used outside <AuthProvider> (it does not consume the AuthContext)", () => {
    // useLogout is a thin wrapper over useLogoutMutation; it does NOT call
    // useAuthContext. So it works without <AuthProvider>, only requiring a
    // QueryClientProvider. This test confirms that subtle architectural
    // detail and prevents accidental coupling regressions.
    mockLogoutMutation();
    const client = createTestQueryClient();
    const wrapper = ({ children }: { children: ReactNode }): JSX.Element => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    expect(() => {
      renderHook(() => useLogout(), { wrapper });
    }).not.toThrow();
  });
});

// ---------------------------------------------------------------------------
// hooks throw when AuthProvider is missing
// ---------------------------------------------------------------------------

describe("hooks throw when AuthProvider is missing", () => {
  it("useSession throws a clear error message including provider hint", () => {
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    expect(() => {
      renderHook(() => useSession());
    }).toThrow(
      /must be used inside <AuthProvider>.*frontend\/src\/main\.tsx wraps the app in <AuthProvider>/,
    );
    errorSpy.mockRestore();
  });

  it("useSessionLoading throws a clear error message", () => {
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    expect(() => {
      renderHook(() => useSessionLoading());
    }).toThrow(/must be used inside <AuthProvider>/);
    errorSpy.mockRestore();
  });

  it("useRole throws a clear error message", () => {
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    expect(() => {
      renderHook(() => useRole());
    }).toThrow(/must be used inside <AuthProvider>/);
    errorSpy.mockRestore();
  });
});

// ---------------------------------------------------------------------------
// UserRole type consistency
// ---------------------------------------------------------------------------

describe("UserRole type consistency", () => {
  it("UserRole accepts the three backend role names", () => {
    // Compile-time test: assigning the three valid values must compile.
    // This guards against regression where UserRole drifts from the
    // backend's UserRole enum ('Admin' | 'Contributor' | 'Viewer').
    const admin: UserRole = "Admin";
    const contributor: UserRole = "Contributor";
    const viewer: UserRole = "Viewer";
    // Runtime assertion just to use the values (avoid unused-var lint).
    expect([admin, contributor, viewer]).toEqual(["Admin", "Contributor", "Viewer"]);
  });
});
