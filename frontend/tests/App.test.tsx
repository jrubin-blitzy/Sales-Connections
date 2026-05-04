/**
 * App.test.tsx - Vitest tests for the top-level <App /> shell component.
 *
 * Targets `frontend/src/App.tsx`, the layout-route element used by
 * `@/router.tsx` that composes:
 *   - <AppErrorBoundary> outer (catches header/chrome render errors)
 *   - Conditional <AppHeader> (rendered only for hydrated sessions)
 *   - <main><AppErrorBoundary><Outlet /></AppErrorBoundary></main>
 *   - <ToastContainer /> (portal-mounted overlay)
 *
 * Coverage goals (per AAP Sec 0.5.4 "Connection Feed landing route after
 * auth" and Sec 0.5.2 "Layer 0 provider stack"):
 *
 *   1. Happy path: <App /> renders the app shell without crashing inside
 *      the production provider stack
 *      (QueryClientProvider > AuthProvider > RouterProvider).
 *   2. The <ToastContainer /> portal mounts in document.body so toasts
 *      can render above the route content from any nested component.
 *   3. The error-boundary fallback renders when a child route throws
 *      during render, exposing role="alert", the "Something went wrong"
 *      heading, the captured error message, and the reload button.
 *   4. Clicking the reload button invokes window.location.reload() exactly
 *      once - the boundary's hard-refresh recovery path per AAP Sec 0.7.1
 *      ("graceful failure surfacing for unrecoverable render errors").
 *
 * Why createMemoryRouter (not the production createBrowserRouter):
 *   <App /> renders <Outlet />, NOT <RouterProvider />. The router itself
 *   is the singleton declared in `@/router.tsx` and is mounted by
 *   `frontend/src/main.tsx`. To exercise <App /> in isolation, the test
 *   mounts a small in-test router that uses <App /> as the layout-route
 *   element (matching the production composition in `@/router.tsx`) and
 *   supplies a controllable child route element. createMemoryRouter is
 *   preferred over the production browser router because:
 *     - It isolates each test from window.history singleton state.
 *     - It lets the error-boundary tests register a child whose element
 *       throws during render so the inner <AppErrorBoundary> catches it.
 *     - It avoids tight coupling between the <App /> tests and the full
 *       production route table (which is independently tested elsewhere).
 *
 * Why @/auth/AuthProvider is mocked at module scope:
 *   <App /> calls useSession() and useSessionLoading() directly. With the
 *   real AuthProvider those hooks would trigger a useSessionQuery() round-
 *   trip via MSW - introducing async timing into a test that is otherwise
 *   synchronous. The schema requires `AuthProvider` be imported and used,
 *   so we mock the module at module scope: AuthProvider becomes a
 *   passthrough component (preserving the production wrapping order),
 *   while useSession() and useSessionLoading() become controllable spies
 *   driven via vi.mocked(...).mockReturnValue(...). useRole() and
 *   useLogout() are also stubbed because <AppHeader> consumes them when
 *   a session is present (the default in this suite is null session, so
 *   AppHeader does not render and those stubs are inert).
 *
 *   This mocking pattern follows the established convention in
 *   `tests/auth/ProtectedRoute.test.tsx` and
 *   `tests/features/auth/LoginScreen.test.tsx`.
 *
 * Conventions per AAP Sec 0.7.7 and frontend/.prettierrc.json:
 *   - Strict TypeScript; no `any` (the eslint override allows `any` in
 *     `tests/**` per `frontend/eslint.config.js` but we prefer explicit
 *     types where natural).
 *   - Double quotes (Prettier `singleQuote: false`); trailing commas;
 *     2-space indent; line length <= 100.
 *   - Named imports only; no default exports.
 *   - No emoji; no console.log (only vi.spyOn(console, "error") to
 *     suppress React's noisy boundary error logs).
 *   - data-testid selectors preferred over fragile text matches when the
 *     source uses them (App.tsx defines stable testids).
 *   - userEvent (NOT fireEvent) for clicks per AAP Sec 0.7.7.
 *
 * Coordinates with:
 *   - frontend/src/App.tsx          (system under test)
 *   - frontend/src/auth/AuthProvider.tsx
 *                                   (mocked at module scope)
 *   - frontend/src/components/ui/Toast.tsx
 *                                   (the ToastContainer mounted by App;
 *                                    portals to document.body)
 *   - frontend/tests/setup.ts       (registers jest-dom matchers, MSW
 *                                    server, polyfills, store resets)
 *   - frontend/tests/mocks/server.ts and handlers.ts
 *                                   (MSW infra, not exercised here since
 *                                    AuthProvider is mocked synchronously)
 */

import { type JSX, type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "@/App";
import { AuthProvider, useSession, useSessionLoading } from "@/auth/AuthProvider";
import type { SessionRead } from "@/schemas/auth";

// ---------------------------------------------------------------------------
// Module-scope vi.mock for @/auth/AuthProvider
// ---------------------------------------------------------------------------
//
// CRITICAL: vi.mock calls at module scope are HOISTED by Vitest above ALL
// imports, so the mock is in place when App.tsx resolves its
// `import { useLogout, useRole, useSession, useSessionLoading } from "@/auth/AuthProvider"`
// at module load time. Without hoisting, the real hooks would attempt to
// read AuthContext (sentinel undefined) and throw the
// "must be used inside <AuthProvider>" error.
//
// We provide stubs for ALL exports of @/auth/AuthProvider (not just the
// two hooks <App /> consumes directly) so any transitive consumer
// (notably <AppHeader> when session is non-null) resolves cleanly.
// AuthProvider becomes a pass-through component so the test's
// `<AuthProvider>...</AuthProvider>` wrapper preserves the production
// component tree without invoking the real useSessionQuery() round-trip.
//
// Tests configure useSession() / useSessionLoading() per case via
// vi.mocked(...).mockReturnValue(...). The default in beforeEach is the
// unauthenticated/ready state (session=null, isLoading=false) so
// <AppHeader> does NOT render and the simplest <App /> shell is exercised.

vi.mock("@/auth/AuthProvider", () => ({
  AuthProvider: ({ children }: { children: ReactNode }) => children,
  useSession: vi.fn<() => SessionRead | null>(),
  useSessionLoading: vi.fn<() => boolean>(),
  useRole: vi.fn(() => ({ role: null, has: () => false })),
  useLogout: vi.fn(() => ({
    mutateAsync: vi.fn(),
    isPending: false,
  })),
}));

// ---------------------------------------------------------------------------
// QueryClient helper
// ---------------------------------------------------------------------------

/**
 * Construct a fresh QueryClient configured for fast, deterministic tests.
 *
 * - retry: false              No exponential backoff on failed queries;
 *                             tests resolve in a single attempt.
 * - gcTime: 0, staleTime: 0   Garbage-collect immediately so cache cannot
 *                             leak across tests in the same worker.
 * - mutations.retry: false    Same rationale for mutations.
 *
 * Each test constructs its own client to guarantee per-test cache
 * isolation (matching the convention in `tests/test-utils.tsx`).
 */
function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0, staleTime: 0 },
      mutations: { retry: false },
    },
  });
}

// ---------------------------------------------------------------------------
// Test child components
// ---------------------------------------------------------------------------

/**
 * Happy-path child route element.
 *
 * Renders a single <div data-testid="hello-child"> so the happy-path
 * tests can assert that <App />'s <Outlet /> mounted the child route's
 * element (i.e., the layout shell propagated route content correctly).
 */
function HelloChild(): JSX.Element {
  return <div data-testid="hello-child">Hello from child</div>;
}

/**
 * Error-boundary child route element.
 *
 * Throws a deterministic Error during render. The throw bubbles up
 * through the inner <AppErrorBoundary> wrapping <Outlet /> in App.tsx,
 * which sets `hasError: true` via getDerivedStateFromError() and
 * renders the fallback UI in place of children. Tests then assert on
 * the fallback's role="alert" region, "Something went wrong" heading,
 * the captured error message, and the reload button.
 *
 * The error message string ("Boundary catch test") is asserted
 * verbatim by the error-boundary test below; do not change it without
 * updating the assertion.
 */
function ThrowingChild(): JSX.Element {
  throw new Error("Boundary catch test");
}

// ---------------------------------------------------------------------------
// renderApp helper
// ---------------------------------------------------------------------------

/**
 * Options accepted by renderApp().
 *
 * `childElement` is the JSX element rendered as the only child route of
 * <App />'s layout. The happy-path tests pass <HelloChild />; the
 * error-boundary tests pass <ThrowingChild />.
 */
interface RenderAppOptions {
  childElement: JSX.Element;
}

/**
 * Result of renderApp() - exposes the QueryClient instance so tests
 * that need cache inspection or invalidation can reach it. The current
 * suite does not exercise the cache directly, but the shape mirrors
 * tests/test-utils.tsx so future cache-aware assertions can be added
 * without changing the helper signature.
 */
interface RenderAppResult {
  client: QueryClient;
}

/**
 * Render <App /> inside the production provider stack with a
 * controllable single-child memory router.
 *
 * Structure:
 *
 *   <QueryClientProvider client={client}>
 *     <AuthProvider>           (mocked passthrough)
 *       <RouterProvider router={memoryRouter} />
 *     </AuthProvider>
 *   </QueryClientProvider>
 *
 * The memoryRouter declares <App /> as its layout-route element (the
 * top-level entry has no `path`, only `element` and `children`) and a
 * single child route at "/" whose element is `options.childElement`.
 * This mirrors the production composition in `@/router.tsx` while
 * keeping the test fully isolated from the production route table.
 */
function renderApp(options: RenderAppOptions): RenderAppResult {
  const client = makeQueryClient();
  const router = createMemoryRouter(
    [
      {
        element: <App />,
        children: [{ path: "/", element: options.childElement }],
      },
    ],
    { initialEntries: ["/"] },
  );

  render(
    <QueryClientProvider client={client}>
      <AuthProvider>
        <RouterProvider router={router} />
      </AuthProvider>
    </QueryClientProvider>,
  );

  return { client };
}

// ===========================================================================
// describe: <App /> happy path
// ===========================================================================

describe("<App />", () => {
  beforeEach(() => {
    // Default: unauthenticated, ready state. With session=null and
    // isLoading=false, App.tsx's `showHeader` derives to false so
    // <AppHeader /> does NOT render. Only the outer wrapper, the inner
    // <AppErrorBoundary>, the <main> region, the <Outlet />, and the
    // <ToastContainer /> portal are exercised.
    vi.mocked(useSession).mockReturnValue(null);
    vi.mocked(useSessionLoading).mockReturnValue(false);
  });

  afterEach(() => {
    cleanup();
    // Reset mock call history (but not the module-level vi.mock surface)
    // so per-test assertions on call counts are deterministic. The
    // module-scope mock factory above remains intact across resets.
    vi.mocked(useSession).mockReset();
    vi.mocked(useSessionLoading).mockReset();
    vi.restoreAllMocks();
  });

  it("renders the app shell without crashing", () => {
    renderApp({ childElement: <HelloChild /> });

    // App.tsx wraps everything in <div data-testid="app-shell">.
    expect(screen.getByTestId("app-shell")).toBeInTheDocument();
    // The <main> region is the parent of the inner error boundary +
    // <Outlet />. Its presence confirms the layout structure mounted.
    expect(screen.getByTestId("app-main")).toBeInTheDocument();
    // The single child route's element rendered inside the Outlet,
    // proving <App /> propagated child route content correctly.
    expect(screen.getByTestId("hello-child")).toBeInTheDocument();
    // <AppHeader> must NOT render with session=null (showHeader=false).
    // Asserting absence guards against a regression that would render
    // the header for unauthenticated users.
    expect(screen.queryByTestId("app-header")).not.toBeInTheDocument();
  });

  it("mounts the ToastContainer portal in document.body", () => {
    renderApp({ childElement: <HelloChild /> });

    // ToastContainer renders <div data-testid="toast-container"> via
    // createPortal(node, document.body). Use document.querySelector
    // (NOT screen.* queries) because RTL's screen scopes to the rendered
    // container by default and the portal is a child of <body>, not the
    // RTL-rendered subtree. We also assert the portal's parentElement
    // is document.body to verify the portal target.
    const portal = document.querySelector('[data-testid="toast-container"]');
    expect(portal).toBeInTheDocument();
    expect(portal?.parentElement).toBe(document.body);
  });
});

// ===========================================================================
// describe: <App /> error boundary
// ===========================================================================

describe("<App /> error boundary", () => {
  // Suppress React's noisy "The above error occurred in..." console.error
  // log for the entire error-boundary describe block. Without this, every
  // throw inside <ThrowingChild /> would dump the full component stack to
  // the test runner's stderr and clutter CI output. Restored in afterEach
  // via vi.restoreAllMocks().
  let errorSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    // Same default session state as the happy-path describe block: with
    // session=null, <AppHeader> does not render and the throw inside
    // <ThrowingChild /> is exclusively caught by the inner
    // <AppErrorBoundary> wrapping <Outlet />.
    vi.mocked(useSession).mockReturnValue(null);
    vi.mocked(useSessionLoading).mockReturnValue(false);

    // React 19 logs the captured error to console.error from inside
    // componentDidCatch (App.tsx's AppErrorBoundary.componentDidCatch
    // logs intentionally for browser DevTools); the test runner
    // promotes this to test-output noise. Suppress it for the duration
    // of each error-boundary test.
    errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
  });

  afterEach(() => {
    cleanup();
    errorSpy.mockRestore();
    vi.mocked(useSession).mockReset();
    vi.mocked(useSessionLoading).mockReset();
    vi.restoreAllMocks();
  });

  it("renders the fallback UI when a child route throws during render", () => {
    renderApp({ childElement: <ThrowingChild /> });

    // The fallback's <main role="alert" aria-live="assertive"> must
    // appear with role="alert" so screen readers announce the failure
    // (WCAG 4.1.3 - Status Messages). getByRole asserts both presence
    // and accessibility-tree visibility.
    expect(screen.getByRole("alert")).toBeInTheDocument();

    // The visible "Something went wrong" heading from the fallback UI.
    // /i flag guards against future case-style refactors of the heading.
    expect(screen.getByText(/something went wrong/i)).toBeInTheDocument();

    // The reload button is the only recovery affordance the fallback
    // exposes. Assert by accessible name (the visible button text)
    // rather than the testid for stronger semantic coupling.
    expect(screen.getByRole("button", { name: /reload application/i })).toBeInTheDocument();

    // The captured error message renders inside <pre data-testid=
    // "error-boundary-message">. We assert on text content to verify
    // the boundary actually captured ThrowingChild's error rather than
    // rendering an empty fallback.
    const messageEl = screen.getByTestId("error-boundary-message");
    expect(messageEl).toHaveTextContent("Boundary catch test");
  });

  it("invokes window.location.reload exactly once when reload button is clicked", async () => {
    // jsdom's window.location.reload is a no-op stub by default and is
    // not configurable as a writable property in older jsdom releases.
    // The portable pattern, established in tests/features/auth/
    // LoginScreen.test.tsx, is to replace the entire window.location
    // object via Object.defineProperty - the descriptor's
    // configurable: true + writable: true + value: { ... } combination
    // makes this work in every supported jsdom version.
    const originalLocation = window.location;
    const reloadMock = vi.fn();

    // Build a Location-shaped mock that proxies the original's read-only
    // fields and substitutes our spy for reload. Cast through `unknown`
    // because not every Location member (assign, replace, etc.) needs to
    // be modeled for this single-purpose test.
    const locationMock: Location = {
      ancestorOrigins: originalLocation.ancestorOrigins,
      hash: originalLocation.hash,
      host: originalLocation.host,
      hostname: originalLocation.hostname,
      origin: originalLocation.origin,
      pathname: originalLocation.pathname,
      port: originalLocation.port,
      protocol: originalLocation.protocol,
      search: originalLocation.search,
      href: originalLocation.href,
      assign: vi.fn(),
      replace: vi.fn(),
      reload: reloadMock,
      toString: () => originalLocation.toString(),
    } as unknown as Location;

    Object.defineProperty(window, "location", {
      configurable: true,
      enumerable: true,
      writable: true,
      value: locationMock,
    });

    try {
      renderApp({ childElement: <ThrowingChild /> });

      // Find the reload button via its accessible name (matches the
      // visible "Reload application" text).
      const reloadButton = screen.getByRole("button", {
        name: /reload application/i,
      });

      // userEvent.setup() returns an interaction object that fires a
      // properly-sequenced pointer/click event flow. AAP Sec 0.7.7
      // mandates userEvent (NOT fireEvent) for clicks so the test
      // exercises the same DOM event ordering as a real user.
      const user = userEvent.setup();
      await user.click(reloadButton);

      // The boundary's handleReset() bound class method calls
      // window.location.reload() exactly once. Asserting on call count
      // (rather than just presence) guards against accidental
      // double-invocation regressions (e.g., a future onClick that
      // also dispatches a synthetic onMouseDown).
      expect(reloadMock).toHaveBeenCalledTimes(1);
      // No arguments are passed to reload(); assert the call shape so
      // a future signature change is caught at test time.
      expect(reloadMock).toHaveBeenCalledWith();
    } finally {
      // Always restore window.location, even if the assertion above
      // throws. Other tests in the same worker (and the global
      // afterEach in tests/setup.ts) depend on the original location
      // being intact.
      Object.defineProperty(window, "location", {
        configurable: true,
        enumerable: true,
        writable: true,
        value: originalLocation,
      });
    }
  });
});
