/**
 * ProtectedRoute.test.tsx - Vitest tests for the F-012 authentication route guard.
 *
 * Targets `frontend/src/auth/ProtectedRoute.tsx`. The guard implements three
 * branches per session state:
 *
 *   isLoading=true         -> SessionHydrationSpinner with role="status"
 *   isLoading=false, null  -> Navigate to /login?next=<encoded path> replace
 *   isLoading=false, set   -> children rendered
 *
 * Strategy:
 *   - Mock '@/auth/AuthProvider' at module scope so useSession() and
 *     useSessionLoading() return synchronous, per-test values. We do NOT
 *     mount a real <AuthProvider> + <QueryClientProvider> because this
 *     test focuses on routing behavior given session state, not on the
 *     session-hydration round-trip itself (covered by AuthProvider.test.tsx).
 *   - Use <MemoryRouter> with `initialEntries` to simulate the originally-
 *     requested URL without touching window.location (jsdom's default).
 *   - Use a sibling <Route path="/login"> rendering a placeholder so that
 *     after <Navigate> fires we can assert the redirect happened, the
 *     pathname is "/login", and the `next` query parameter encodes the
 *     original URL correctly.
 *
 * Coverage targets (verified to bring ProtectedRoute.tsx to 100% line/branch
 * coverage):
 *   - Spinner branch: data-testid="protected-route-spinner", role="status",
 *     aria-live="polite", aria-busy="true", and the sr-only "Verifying
 *     session" label.
 *   - Redirect branch: `next=` URL-encoded current path, including
 *     pathname-only, pathname+search, pathnames containing reserved
 *     characters (?, &, =), and deep parameterized routes.
 *   - Authenticated branch: children render unchanged across every role
 *     (Admin / Contributor / Viewer) and across nested JSX trees.
 *   - State transitions: loading -> redirect, loading -> authenticated.
 *   - Loading-takes-precedence: even when session is non-null, isLoading
 *     true forces the spinner (guards against priority regression).
 *
 * Conventions per AAP Sec 0.7.7 and frontend/.prettierrc.json:
 *   - Strict TypeScript; no `any`.
 *   - Double quotes; trailing commas; 2-space indent; line length <= 100.
 *   - Named imports only; no default export.
 *   - No emoji; no console.log.
 *   - data-testid selectors preferred over class-name coupling.
 *   - Backend role names ("Admin", "Contributor", "Viewer") only.
 */

import { type JSX, type ReactNode } from "react";
import { MemoryRouter, Route, Routes, useLocation, useSearchParams } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import { ProtectedRoute } from "@/auth/ProtectedRoute";
import { useSession, useSessionLoading } from "@/auth/AuthProvider";
import type { SessionRead } from "@/schemas/auth";

import { makeSessionRead } from "../mocks/data";

// ---------------------------------------------------------------------------
// Module-scope vi.mock for @/auth/AuthProvider
// ---------------------------------------------------------------------------
//
// CRITICAL: vi.mock calls at module scope are HOISTED by Vitest above any
// imports, so the mock is in place when ProtectedRoute.tsx resolves its
// `import { useSession, useSessionLoading } from "@/auth/AuthProvider"` at
// module load time. Without hoisting, the real hooks would attempt to read
// the AuthContext (sentinel undefined) and throw "must be used inside
// <AuthProvider>" inside the test render.
//
// We provide stubs for ALL exports of @/auth/AuthProvider (not just the two
// hooks ProtectedRoute consumes) so any transitive import resolves cleanly.
// ProtectedRoute.tsx only directly uses useSession + useSessionLoading, but
// mocking the full export surface protects against future ProtectedRoute
// revisions that consume additional hooks.

vi.mock("@/auth/AuthProvider", () => ({
  AuthProvider: ({ children }: { children: ReactNode }) => children,
  useSession: vi.fn<() => SessionRead | null>(),
  useSessionLoading: vi.fn<() => boolean>(),
  useRole: vi.fn(() => ({ role: null, has: () => false })),
  useLogout: vi.fn(),
}));

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Configure useSession() and useSessionLoading() for the next render.
 *
 * Pass `null` for `session` to simulate the unauthenticated state; pass a
 * SessionRead (from makeSessionRead) for the authenticated state. Pass
 * `isLoading=true` to simulate the cold-load window during which
 * <ProtectedRoute> must render the spinner.
 *
 * The two hooks are configured in lock-step so each test sees a coherent
 * snapshot of the (session, isLoading) state machine.
 */
function mockSessionState(session: SessionRead | null, isLoading = false): void {
  vi.mocked(useSession).mockReturnValue(session);
  vi.mocked(useSessionLoading).mockReturnValue(isLoading);
}

/**
 * Test placeholder rendered at /login. After <Navigate to="/login?..."> fires
 * inside <ProtectedRoute>, this component mounts at /login and exposes the
 * post-redirect pathname plus the decoded `next` query parameter and the
 * raw search string for assertions.
 *
 * Three observable surfaces:
 *   - login-pathname  the new pathname (should be "/login")
 *   - login-next      the decoded `next` parameter (useSearchParams.get
 *                     auto-decodes via the URL primitive)
 *   - login-search    the raw, still-encoded search string (used to verify
 *                     special characters like ?, &, = are URL-encoded inside
 *                     the next= value so they do not break the parent query
 *                     string)
 */
function LoginPlaceholder(): JSX.Element {
  const location = useLocation();
  const [searchParams] = useSearchParams();
  return (
    <div data-testid="login-placeholder">
      <span data-testid="login-pathname">{location.pathname}</span>
      <span data-testid="login-next">{searchParams.get("next") ?? ""}</span>
      <span data-testid="login-search">{location.search}</span>
    </div>
  );
}

/**
 * Test placeholder for the protected route's children. Rendered inside
 * <ProtectedRoute> for authenticated tests so the suite can assert children
 * appear (and conversely that they do NOT appear during loading or redirect
 * branches).
 */
function FeedContent(): JSX.Element {
  return <div data-testid="feed-content">Feed loaded</div>;
}

/**
 * Render the test harness with a <MemoryRouter> starting at `initialPath`.
 * Mounts <ProtectedRoute><FeedContent /></ProtectedRoute> at `protectedPath`
 * AND a <LoginPlaceholder /> at /login so redirect tests can assert the
 * redirect actually fired and inspect the resulting URL.
 *
 * Default `initialEntries` is ["/feed"] so the redirect's `next` value is
 * meaningful for assertions. Tests overriding `protectedPath` (e.g., to
 * exercise a parameterized route like /connections/:id/edit) must keep the
 * `initialPath` consistent with the route definition.
 */
function renderProtected(initialPath = "/feed", protectedPath = "/feed"): void {
  render(
    <MemoryRouter initialEntries={[initialPath]}>
      <Routes>
        <Route
          path={protectedPath}
          element={
            <ProtectedRoute>
              <FeedContent />
            </ProtectedRoute>
          }
        />
        <Route path="/login" element={<LoginPlaceholder />} />
      </Routes>
    </MemoryRouter>,
  );
}

// ---------------------------------------------------------------------------
// Setup and teardown
// ---------------------------------------------------------------------------

beforeEach(() => {
  // Reset call history and any prior mockReturnValue so each test starts
  // with a fresh mock and must explicitly call mockSessionState(...) before
  // rendering. Tests that forget will see the hooks return `undefined`,
  // which surfaces as a clear React error - a loud failure mode is
  // preferable to silent default behavior masking the omission.
  vi.mocked(useSession).mockReset();
  vi.mocked(useSessionLoading).mockReset();
});

afterEach(() => {
  // Unmount any rendered trees and restore mocks for full isolation. Without
  // cleanup(), DOM nodes from test A leak into test B's screen queries
  // (jsdom is shared across tests in a worker). vi.restoreAllMocks() clears
  // any spies installed mid-test but preserves the module-scope vi.mock
  // factory above (which is a different mechanism).
  cleanup();
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// Test suite: Loading state
// ---------------------------------------------------------------------------

describe("<ProtectedRoute /> loading state", () => {
  it("renders the SessionHydrationSpinner when isLoading is true", () => {
    mockSessionState(null, true);
    renderProtected();

    const spinner = screen.getByTestId("protected-route-spinner");
    expect(spinner).toBeInTheDocument();
  });

  it("does NOT render children while loading", () => {
    mockSessionState(null, true);
    renderProtected();

    expect(screen.queryByTestId("feed-content")).toBeNull();
  });

  it("does NOT redirect to /login while loading (no flash of LoginPlaceholder)", () => {
    mockSessionState(null, true);
    renderProtected();

    // Critical: we should see the spinner, NOT the LoginPlaceholder.
    // Otherwise the user would briefly see the redirect even though we are
    // still hydrating the session - jarring UX and a perception leak (record
    // counts/names could flash before the redirect fires).
    expect(screen.queryByTestId("login-placeholder")).toBeNull();
    expect(screen.getByTestId("protected-route-spinner")).toBeInTheDocument();
  });

  it("announces the loading state to screen readers via role and aria attributes", () => {
    mockSessionState(null, true);
    renderProtected();

    // The wrapper div carries role="status", aria-live="polite", and
    // aria-busy="true" so assistive technology announces the loading state.
    const status = screen.getByRole("status");
    expect(status).toHaveAttribute("aria-live", "polite");
    expect(status).toHaveAttribute("aria-busy", "true");
  });

  it("includes the hidden text 'Verifying session' for screen readers", () => {
    mockSessionState(null, true);
    renderProtected();

    // The sr-only span text is queryable by getByText even though it is
    // visually hidden via Tailwind's sr-only utility.
    expect(screen.getByText("Verifying session")).toBeInTheDocument();
  });

  it("renders the spinner even when session is non-null but isLoading is true", () => {
    // Edge case: TanStack Query may briefly hold stale data while refetching.
    // Per the spec behavior matrix, isLoading=true takes precedence over the
    // session value. Although ProtectedRoute checks isLoading FIRST (so this
    // is naturally satisfied today), this test guards against a future
    // refactor that swaps the priority order.
    mockSessionState(makeSessionRead({ user: { role: "Admin" } }), true);
    renderProtected();

    expect(screen.getByTestId("protected-route-spinner")).toBeInTheDocument();
    expect(screen.queryByTestId("feed-content")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Test suite: Unauthenticated redirect
// ---------------------------------------------------------------------------

describe("<ProtectedRoute /> unauthenticated redirect", () => {
  it("redirects to /login when session is null and isLoading is false", () => {
    mockSessionState(null, false);
    renderProtected("/feed");

    // After <Navigate> fires, the LoginPlaceholder mounts at /login.
    expect(screen.getByTestId("login-placeholder")).toBeInTheDocument();
  });

  it("does NOT render the children when redirecting", () => {
    mockSessionState(null, false);
    renderProtected("/feed");

    expect(screen.queryByTestId("feed-content")).toBeNull();
  });

  it("does NOT render the spinner when redirecting", () => {
    mockSessionState(null, false);
    renderProtected("/feed");

    expect(screen.queryByTestId("protected-route-spinner")).toBeNull();
  });

  it("encodes the originally-requested pathname in the `next` query parameter", () => {
    mockSessionState(null, false);
    renderProtected("/feed");

    // useSearchParams.get auto-decodes via the URL primitive, so the
    // decoded value should equal the original pathname exactly.
    expect(screen.getByTestId("login-next")).toHaveTextContent("/feed");
  });

  it("preserves an existing query string in the `next` parameter", () => {
    mockSessionState(null, false);
    renderProtected("/feed?involvement=Warm+Intro&owner=jane");

    // The pathname + search round-trips through encodeURIComponent on the
    // way out and decodeURIComponent (via useSearchParams) on the way back
    // in, so the decoded next= value should equal the original URL.
    const nextEl = screen.getByTestId("login-next");
    expect(nextEl).toHaveTextContent("/feed?involvement=Warm+Intro&owner=jane");
  });

  it("encodes special characters (?, &, =) so they do not break the parent query string", () => {
    mockSessionState(null, false);
    // Use a parameterized protectedPath so that /connections/abc-123 matches
    // a route in the test harness; otherwise the URL fails to match any
    // route at all and ProtectedRoute never renders the redirect.
    renderProtected("/connections/abc-123?tab=history&page=2", "/connections/:id");

    // The raw search string contains the encoded next= value. Without
    // encodeURIComponent, the embedded ?, &, and = would parse as ADDITIONAL
    // top-level query parameters on /login, breaking the round-trip and
    // potentially exposing the original URL components as separate params
    // (e.g., a `tab=history` query param on /login itself).
    const search = screen.getByTestId("login-search").textContent ?? "";
    // The next= prefix is always present.
    expect(search).toContain("next=");
    // The reserved characters MUST appear in their percent-encoded form:
    //   %3F = ?  ;  %26 = &  ;  %3D = =
    expect(search).toContain("%3F");
    expect(search).toContain("%26");
    expect(search).toContain("%3D");
  });

  it("produces the correct `next` for a deep parameterized route", () => {
    mockSessionState(null, false);
    renderProtected("/connections/abc-123/edit", "/connections/:id/edit");

    // Even a deep parameterized route round-trips through the next=
    // parameter intact - location.pathname captures the resolved URL,
    // not the route pattern.
    expect(screen.getByTestId("login-next")).toHaveTextContent("/connections/abc-123/edit");
  });

  it("redirects to /login (not just any URL)", () => {
    mockSessionState(null, false);
    renderProtected("/feed");

    // The pathname after the redirect must be exactly "/login" - tests above
    // verify the next= parameter is correct, but this test pins the
    // destination so a future regression that redirects to (e.g.) /signin
    // would surface immediately.
    expect(screen.getByTestId("login-pathname")).toHaveTextContent("/login");
  });
});

// ---------------------------------------------------------------------------
// Test suite: Authenticated render
// ---------------------------------------------------------------------------

describe("<ProtectedRoute /> authenticated render", () => {
  it("renders children when session is non-null and isLoading is false", () => {
    mockSessionState(makeSessionRead({ user: { role: "Admin" } }), false);
    renderProtected();

    expect(screen.getByTestId("feed-content")).toBeInTheDocument();
    expect(screen.getByTestId("feed-content")).toHaveTextContent("Feed loaded");
  });

  it("does NOT render the spinner when authenticated", () => {
    mockSessionState(makeSessionRead({ user: { role: "Contributor" } }), false);
    renderProtected();

    expect(screen.queryByTestId("protected-route-spinner")).toBeNull();
  });

  it("does NOT redirect to /login when authenticated", () => {
    mockSessionState(makeSessionRead({ user: { role: "Viewer" } }), false);
    renderProtected();

    expect(screen.queryByTestId("login-placeholder")).toBeNull();
  });

  it("renders children for any non-null session regardless of role", () => {
    // ProtectedRoute does not gate on role - that is RoleGate's
    // responsibility (covered by RoleGate.test.tsx). Each of the three
    // production roles should render the children equally; if any role
    // is incorrectly rejected here, the gate is over-restrictive.
    const roles: ReadonlyArray<"Admin" | "Contributor" | "Viewer"> = [
      "Admin",
      "Contributor",
      "Viewer",
    ];
    roles.forEach((role) => {
      // Reset mocks between iterations so each role starts from a clean
      // slate; without reset the previous role's mockReturnValue would
      // still apply (vi.fn accumulates without reset).
      vi.mocked(useSession).mockReset();
      vi.mocked(useSessionLoading).mockReset();
      mockSessionState(makeSessionRead({ user: { role } }), false);
      const { unmount } = render(
        <MemoryRouter initialEntries={["/feed"]}>
          <Routes>
            <Route
              path="/feed"
              element={
                <ProtectedRoute>
                  <div data-testid={`feed-${role}`}>Loaded for {role}</div>
                </ProtectedRoute>
              }
            />
          </Routes>
        </MemoryRouter>,
      );
      expect(screen.getByTestId(`feed-${role}`)).toBeInTheDocument();
      // Unmount before the next iteration so the next render starts with an
      // empty DOM; otherwise repeated data-testid queries find stale nodes.
      unmount();
    });
  });

  it("renders children that are nested JSX trees", () => {
    mockSessionState(makeSessionRead({ user: { role: "Admin" } }), false);
    render(
      <MemoryRouter initialEntries={["/feed"]}>
        <Routes>
          <Route
            path="/feed"
            element={
              <ProtectedRoute>
                <main data-testid="main">
                  <h1>Feed</h1>
                  <button type="button" data-testid="action">
                    Action
                  </button>
                </main>
              </ProtectedRoute>
            }
          />
        </Routes>
      </MemoryRouter>,
    );
    // The fragment wrapper inside ProtectedRoute does NOT introduce extra
    // DOM nodes, so a deeply nested tree renders unchanged. Both the outer
    // <main> and the inner <button> appear in the rendered DOM.
    expect(screen.getByTestId("main")).toBeInTheDocument();
    expect(screen.getByTestId("action")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Test suite: State transitions
// ---------------------------------------------------------------------------

describe("<ProtectedRoute /> state transitions", () => {
  it("transitions from loading to redirect when session settles to null", () => {
    // Initial state: session-hydration query in flight.
    mockSessionState(null, true);
    const { rerender } = render(
      <MemoryRouter initialEntries={["/feed"]}>
        <Routes>
          <Route
            path="/feed"
            element={
              <ProtectedRoute>
                <FeedContent />
              </ProtectedRoute>
            }
          />
          <Route path="/login" element={<LoginPlaceholder />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(screen.getByTestId("protected-route-spinner")).toBeInTheDocument();
    expect(screen.queryByTestId("login-placeholder")).toBeNull();

    // Update mocks to "settled, no session" and rerender. This simulates
    // the moment GET /api/me resolves with 401 - the spinner should
    // unmount and the redirect should fire.
    mockSessionState(null, false);
    rerender(
      <MemoryRouter initialEntries={["/feed"]}>
        <Routes>
          <Route
            path="/feed"
            element={
              <ProtectedRoute>
                <FeedContent />
              </ProtectedRoute>
            }
          />
          <Route path="/login" element={<LoginPlaceholder />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(screen.getByTestId("login-placeholder")).toBeInTheDocument();
    expect(screen.queryByTestId("protected-route-spinner")).toBeNull();
  });

  it("transitions from loading to authenticated when session settles", () => {
    // Initial state: session-hydration query in flight.
    mockSessionState(null, true);
    const { rerender } = render(
      <MemoryRouter initialEntries={["/feed"]}>
        <Routes>
          <Route
            path="/feed"
            element={
              <ProtectedRoute>
                <FeedContent />
              </ProtectedRoute>
            }
          />
          <Route path="/login" element={<LoginPlaceholder />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(screen.getByTestId("protected-route-spinner")).toBeInTheDocument();

    // Update mocks to "settled with session" and rerender. This simulates
    // the moment GET /api/me resolves with 200 OK and a SessionRead - the
    // spinner should unmount and the children should appear.
    mockSessionState(makeSessionRead({ user: { role: "Admin" } }), false);
    rerender(
      <MemoryRouter initialEntries={["/feed"]}>
        <Routes>
          <Route
            path="/feed"
            element={
              <ProtectedRoute>
                <FeedContent />
              </ProtectedRoute>
            }
          />
          <Route path="/login" element={<LoginPlaceholder />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(screen.getByTestId("feed-content")).toBeInTheDocument();
    expect(screen.queryByTestId("protected-route-spinner")).toBeNull();
  });
});
