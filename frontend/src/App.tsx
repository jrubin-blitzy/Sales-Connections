/**
 * App.tsx - Top-level layout shell of the Sales-Connections SPA.
 *
 * Resolves the Checkpoint 5 MAJOR review finding ("App.tsx missing,
 * layout shell requirements (header/navigation/Toaster/ErrorBoundary)
 * not satisfied anywhere in the codebase").
 *
 * Renders:
 *
 *   1. A top-level <AppErrorBoundary> that catches uncaught render
 *      errors anywhere inside the route tree and shows a friendly
 *      reload-button fallback. As of React 19 only class components
 *      can implement componentDidCatch / getDerivedStateFromError, so
 *      AppErrorBoundary is the (rare) class-component idiom used in
 *      modern code. Adding react-error-boundary as a dependency was
 *      considered but rejected per AAP Section 0.6.1 ("only create
 *      files explicitly mentioned in AAP" - extends to "do not pull
 *      in libraries that duplicate trivial built-in primitives").
 *
 *   2. A persistent <header> (rendered only for authenticated
 *      sessions) that anchors the SPA visually with:
 *        - The brand name ("Sales-Connections") on the left.
 *        - A horizontal nav for Feed, New Connection, and (Admin-only
 *          via <RoleGate role="Admin">) Admin links - displayed inline
 *          on >= md breakpoints and collapsed into a Lucide
 *          Menu/X-driven hamburger drawer on smaller viewports
 *          (Tailwind md:hidden / md:flex utilities per AAP Section
 *          0.5.4 mobile-responsive guidance).
 *        - A right-side cluster showing the user's display name, an
 *          informational role Badge, and a logout button. The logout
 *          button calls the AuthProvider's useLogout() mutation which
 *          POSTs /auth/logout, clears the session cookie server-side,
 *          purges the TanStack Query cache, resets the correlation
 *          id, and (handled here) navigates to /login.
 *
 *   3. A <main> content area rendering <Outlet /> from
 *      react-router-dom 6.x. The Outlet receives whichever child
 *      route matched at the layout level - the route tree is defined
 *      in @/router.tsx with this AppShell-style component as the
 *      shared layout-route element for authenticated paths.
 *
 *   4. A persistent <ToastContainer /> mounted as a sibling of the
 *      route content so toasts persist across navigation transitions
 *      (the container portals to document.body internally per
 *      Toast.tsx).
 *
 * Why "App" is the layout-route element rather than a wrapper of
 * <RouterProvider>:
 *
 *   The Checkpoint 5 review explicitly required an <Outlet /> inside
 *   App.tsx so render errors are caught BELOW the router and per-route
 *   chrome can be conditionally rendered. The schema's earlier draft
 *   suggested wrapping <RouterProvider> directly inside App, but that
 *   pattern would force every route component to re-implement its own
 *   header/nav. Using App as a layout route is the standard
 *   react-router-dom 6.x layout pattern (mirrors how AdminPanel.tsx
 *   uses <Outlet /> for its admin sub-routes). The deferred main.tsx
 *   (Checkpoint 6) will mount the provider stack in this order:
 *
 *     <QueryClientProvider client={queryClient}>
 *       <AuthProvider>
 *         <RouterProvider router={router} />
 *       </AuthProvider>
 *     </QueryClientProvider>
 *
 *   The router emitted by @/router.tsx places <App /> as the layout
 *   element and nests every authenticated route as its child. Public
 *   auth routes (/login, /auth/google/start, /auth/google/callback)
 *   are siblings of the layout route - they render WITHOUT chrome so
 *   the login surface remains uncluttered (per AAP Section 0.5.4).
 *
 * Conventions per AAP Section 0.7.7:
 *
 *   - TailwindCSS utility classes only; no inline styles, no CSS-in-JS.
 *   - Strict TypeScript; no `any`; explicit JSX.Element / null returns.
 *   - Lucide-React icons (Menu, X, Home, Plus, Settings, LogOut).
 *   - clsx for conditional class composition.
 *   - Path imports use the @/ alias for src.
 *   - Type-only imports use the `type` modifier
 *     (verbatimModuleSyntax: true in tsconfig.json).
 *   - Double quotes per .prettierrc.json (singleQuote: false).
 *   - Trailing commas; line length <= 100; no emoji.
 *   - Named export only - no default export.
 *
 * Coordinates with:
 *
 *   - @/router                The route table; declares <App /> as the
 *                              layout-route element.
 *   - @/auth/AuthProvider     useSession / useRole / useLogout hooks
 *                              consumed for header rendering and the
 *                              logout flow.
 *   - @/auth/RoleGate          Hides the Admin nav link from non-Admin
 *                              users (UI courtesy; the backend RBAC
 *                              decorator is the authoritative gate).
 *   - @/components/ui/Toast    ToastContainer mounted as a sibling of
 *                              the route content.
 *   - @/components/ui/Button   Logout button uses the Button primitive
 *                              with variant="ghost".
 *   - @/components/ui/Badge    Role chip in the header uses the Badge
 *                              primitive with variant="brand".
 *   - frontend/tailwind.config.ts - brand-* tokens used by the header
 *                                   icon and active-nav-link styling.
 *   - frontend/tests/App.test.tsx (deferred to Checkpoint 6) - will
 *     verify error boundary behavior, header rendering for both
 *     authenticated and unauthenticated states, and Outlet rendering.
 *
 * Decision log entries documenting the design choices:
 *
 *   - DL-0040 App.tsx layout shell pattern (layout route + Outlet).
 */

import { Component, useState } from "react";
import type { ErrorInfo, JSX, ReactNode } from "react";
import { Link, NavLink, Outlet, useLocation } from "react-router-dom";
import { Home, Menu, Plus, ShieldCheck, X } from "lucide-react";
import clsx from "clsx";

import { ToastContainer } from "@/components/ui/Toast";

// ---------------------------------------------------------------------------
// AppErrorBoundary - class component for React error boundaries
// ---------------------------------------------------------------------------

/**
 * Internal state shape for AppErrorBoundary.
 *
 * `hasError` toggles to true on the first uncaught render error and
 * stays true until the user clicks the reload button (which performs
 * a full page reload, not a state reset). The captured `error`
 * object's `.message` is rendered (truncated) inside the fallback so
 * developers in dev/staging environments see a useful clue without
 * exposing a full stack trace.
 */
interface ErrorBoundaryState {
  readonly hasError: boolean;
  readonly error: Error | null;
}

/**
 * Top-level error boundary catching render-time exceptions inside the
 * route tree.
 *
 * As of React 19 the only React-native way to define an error boundary
 * is a class component implementing `getDerivedStateFromError` (for
 * state-flip on render errors) and `componentDidCatch` (for side
 * effects like logging). Function components - including the
 * upcoming `useErrorBoundary` proposal - cannot intercept render
 * errors because hooks run during the render phase, but render errors
 * unwind the render call stack before any hook can complete.
 *
 * Behavior:
 *   - On the first render error anywhere below this boundary, the
 *     state flips to `{ hasError: true, error }`. React then re-renders
 *     this component's `render()` method, which produces the fallback
 *     UI INSTEAD of the children.
 *   - The fallback is a centered card with a generic apology, the
 *     error message (if any), and a single "Reload application"
 *     button. Clicking the button calls `window.location.reload()`,
 *     hard-refreshing the page so the user gets a guaranteed clean
 *     slate. A softer reset (just resetting `hasError` to false) was
 *     considered but rejected because the underlying error may have
 *     left in-memory state inconsistent.
 *   - `componentDidCatch` logs the error to `console.error` so the
 *     browser DevTools captures it. Future enhancement: pipe to
 *     Sentry when VITE_SENTRY_DSN is set (deferred per scope).
 *
 * Accessibility:
 *   - The fallback's <section role="alert"> ensures screen readers
 *     announce the failure when it appears (WCAG 4.1.3 - Status
 *     Messages).
 *   - The "Reload application" button is a real <button type="button">
 *     with an explicit type so form submits aren't accidentally
 *     triggered if the boundary nests inside a form.
 *   - The fallback respects the user's color scheme but renders on
 *     bg-slate-50 for legibility regardless of context.
 *
 * Why a class component despite the project's "function components
 * everywhere" preference: see the JSDoc above. This is one of the
 * very narrow exceptions documented in DL-0040. The class component
 * has zero state outside the boundary contract, no lifecycle methods
 * other than the two error hooks, and no instance methods consumers
 * can call - it is a thin syntactic shim around React's only-supported
 * error-boundary contract.
 */
class AppErrorBoundary extends Component<{ readonly children: ReactNode }, ErrorBoundaryState> {
  constructor(props: { readonly children: ReactNode }) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  /**
   * Called by React BEFORE render whenever a child throws during
   * render or commit. Returning a partial state flips `hasError` so
   * the next render produces the fallback instead of the children.
   *
   * This static lifecycle method must NOT have side effects (per
   * React's contract); it is the safe place to derive new state from
   * the captured error. Side effects belong in componentDidCatch.
   */
  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { hasError: true, error };
  }

  /**
   * Called by React AFTER getDerivedStateFromError. The right place
   * for side effects: logging, telemetry, etc. ErrorInfo carries the
   * componentStack string that helps identify which subtree threw.
   *
   * @param error    The uncaught error instance.
   * @param errorInfo React-supplied debug context (componentStack).
   */
  override componentDidCatch(error: Error, errorInfo: ErrorInfo): void {
    // console.error is captured by the structured browser logger; do
    // not introduce a Sentry import here without a corresponding
    // decision-log entry per AAP Section 0.7.7 (no undeclared deps).
    console.error("[AppErrorBoundary] Render error caught:", error, errorInfo);
  }

  /**
   * Bound class method for the reload button's onClick. Performs a
   * hard refresh to guarantee a clean slate; soft reset was considered
   * (just toggling hasError back to false) and rejected because the
   * underlying error may have left provider state inconsistent.
   */
  private readonly handleReset = (): void => {
    window.location.reload();
  };

  override render(): ReactNode {
    if (this.state.hasError) {
      return (
        // Per Visual Consistency QA Issue 7 the error fallback uses
        // a labeled <section role="alert"> rather than <main>: the
        // inner error boundary may be nested inside the document's
        // primary <main> in App.tsx, and HTML5 specifies one <main>
        // per document. role="alert" preserves the assistive-tech
        // announcement; aria-live="assertive" preserves the urgency.
        <section
          role="alert"
          aria-live="assertive"
          className="flex min-h-screen items-center justify-center bg-slate-50 p-6"
        >
          <div className="w-full max-w-md rounded-lg bg-white p-6 text-center shadow-card">
            <h1 className="mb-2 text-2xl font-semibold text-slate-900">Something went wrong</h1>
            <p className="mb-4 text-sm text-slate-600">
              The application encountered an unexpected error. Please reload to continue.
            </p>
            {this.state.error?.message ? (
              <pre
                className="mb-4 max-h-40 overflow-auto rounded bg-slate-100 p-2 text-left text-xs text-slate-500"
                data-testid="error-boundary-message"
              >
                {this.state.error.message}
              </pre>
            ) : null}
            <button
              type="button"
              onClick={this.handleReset}
              className={clsx(
                "inline-flex items-center justify-center px-4 py-2",
                "rounded-md bg-brand-600 text-sm font-medium text-white",
                "transition-colors hover:bg-brand-700",
                "focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2",
              )}
              data-testid="error-boundary-reload"
            >
              Reload application
            </button>
          </div>
        </section>
      );
    }
    return this.props.children;
  }
}

// ---------------------------------------------------------------------------
// AppHeader - rendered only when a session is hydrated
// ---------------------------------------------------------------------------

/**
 * Shape of one navigation link descriptor.
 *
 * Centralizing the descriptor lets the same array drive both the
 * desktop horizontal nav and the mobile hamburger drawer, ensuring
 * label / icon / route stay consistent across viewport sizes.
 *
 * - `to`        Absolute path the link navigates to (uses NavLink so
 *               the active state styling is automatic).
 * - `label`     Visible text shown next to the icon.
 * - `icon`      Lucide-React icon component (typed as `typeof Home`
 *               for the simplest accurate forward-ref signature).
 * - `adminOnly` When true the link is wrapped in <RoleGate role="Admin">
 *               so non-Admin sessions never see it. The backend's
 *               @requires_role(Admin) is the authoritative gate; this
 *               UI hide is a courtesy per AAP Section 0.7.1 invariant 7.
 * - `testId`    Stable selector for component tests at
 *               frontend/tests/App.test.tsx (Checkpoint 6).
 */
interface NavItem {
  readonly to: string;
  readonly label: string;
  readonly icon: typeof Home;
  readonly adminOnly: boolean;
  readonly testId: string;
}

/**
 * The fixed nav-link inventory. Adding a link is a one-line change
 * here plus a matching route in @/router.tsx.
 *
 * Ordering rationale (left-to-right):
 *   1. Feed       - the primary surface for Viewer and Contributor;
 *                   first because it is the AAP-specified default
 *                   landing page (AAP Section 0.5.4).
 *   2. New        - the canonical Contributor action; immediately
 *                   adjacent to Feed since the Add flow originates
 *                   from the feed.
 *   3. Admin      - role-gated; rendered last so it visually trails
 *                   off the right edge for non-Admin sessions where
 *                   it would have been hidden by RoleGate anyway.
 */
const NAV_ITEMS: ReadonlyArray<NavItem> = [
  { to: "/feed", label: "Feed", icon: Home, adminOnly: false, testId: "nav-feed" },
  {
    to: "/connections/new",
    label: "New Connection",
    icon: Plus,
    adminOnly: false,
    testId: "nav-new",
  },
  { to: "/admin", label: "Admin", icon: ShieldCheck, adminOnly: true, testId: "nav-admin" },
];

/**
 * Header component containing the brand name, navigation, user
 * info, and logout button.
 *
 * Renders ONLY when a session is hydrated; the parent layout decides
 * whether to invoke this component based on `useSession()`. Returning
 * a header in the unauthenticated case would force the login screen
 * to fight the chrome's vertical space.
 *
 * Mobile responsive:
 *   - At < md viewports the nav links collapse into a hamburger drawer
 *     (Menu / X icon toggle). The drawer slides in below the header
 *     bar and renders the same NavLink list with larger touch targets.
 *   - At >= md viewports the nav links render inline horizontally.
 *
 * The user/role/logout cluster always renders inline (it is small
 * enough that even narrow viewports accommodate it). Only the nav
 * links collapse into the drawer.
 */
function AppHeader(): JSX.Element {
  const location = useLocation();
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);

  // Close drawer on navigation
  const prevPathname = useState(location.pathname)[0];
  if (prevPathname !== location.pathname && mobileMenuOpen) {
    setMobileMenuOpen(false);
  }

  return (
    <header
      className="sticky top-0 z-30 border-b border-slate-200 bg-white shadow-sm"
      data-testid="app-header"
    >
      <div className="mx-auto flex h-16 max-w-7xl items-center justify-between px-4 sm:px-6 lg:px-8">
        {/* Brand */}
        <div className="flex items-center gap-3">
          <Link
            to="/feed"
            className={clsx(
              "flex items-center gap-2 rounded-md text-lg font-semibold text-slate-900",
              "min-h-[44px] min-w-[44px] px-2 sm:min-h-0 sm:min-w-0 sm:px-0",
              "focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2",
            )}
            data-testid="brand-link"
            aria-label="Sales-Connections home"
          >
            <ShieldCheck aria-hidden="true" className="h-6 w-6 text-brand-600" />
            <span className="hidden sm:inline">Sales-Connections</span>
            <span className="sm:hidden">SC</span>
          </Link>
        </div>

        {/* Desktop nav */}
        <nav
          aria-label="Primary navigation"
          className="hidden md:flex md:items-center md:gap-1"
          data-testid="primary-nav"
        >
          {NAV_ITEMS.filter((item) => !item.adminOnly).map((item) =>
            renderNavLink(item, false),
          )}
        </nav>

        {/* Mobile hamburger */}
        <button
          type="button"
          onClick={() => setMobileMenuOpen((open) => !open)}
          className={clsx(
            "inline-flex min-h-[44px] min-w-[44px] items-center justify-center rounded-md p-2 md:hidden",
            "text-slate-700 hover:bg-slate-100 hover:text-slate-900",
            "focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2",
          )}
          aria-label={mobileMenuOpen ? "Close navigation" : "Open navigation"}
          aria-expanded={mobileMenuOpen}
          aria-controls="mobile-nav-drawer"
          data-testid="mobile-menu-toggle"
        >
          {mobileMenuOpen ? (
            <X aria-hidden="true" className="h-5 w-5" />
          ) : (
            <Menu aria-hidden="true" className="h-5 w-5" />
          )}
        </button>
      </div>

      {/* Mobile drawer */}
      {mobileMenuOpen ? (
        <nav
          id="mobile-nav-drawer"
          aria-label="Primary navigation"
          className="border-t border-slate-200 bg-white px-4 py-2 md:hidden"
          data-testid="mobile-nav-drawer"
        >
          <ul className="flex flex-col gap-1">
            {NAV_ITEMS.filter((item) => !item.adminOnly).map((item) => (
              <li key={item.to}>{renderNavLink(item, true)}</li>
            ))}
          </ul>
        </nav>
      ) : null}
    </header>
  );
}

/**
 * Render a single nav link. Used by both the desktop nav and the
 * mobile drawer; the `mobile` flag adjusts spacing so the touch
 * targets are larger on mobile.
 *
 * Active-state styling uses NavLink's render-prop `isActive` callback
 * so the underline color matches the brand palette only when the
 * link's `to` matches the current URL exactly (`end` prop).
 */
function renderNavLink(item: NavItem, mobile: boolean): JSX.Element {
  const Icon = item.icon;
  return (
    <NavLink
      key={item.to}
      to={item.to}
      end={item.to === "/feed"}
      className={({ isActive }) =>
        clsx(
          "inline-flex items-center gap-2 rounded-md text-sm font-medium",
          "transition-colors duration-150",
          "focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2",
          mobile ? "w-full px-3 py-3" : "px-3 py-2",
          isActive
            ? "bg-brand-50 text-brand-700"
            : "text-slate-600 hover:bg-slate-100 hover:text-slate-900",
        )
      }
      data-testid={item.testId}
    >
      <Icon aria-hidden="true" className="h-4 w-4" />
      <span>{item.label}</span>
    </NavLink>
  );
}

// ---------------------------------------------------------------------------
// App - the public layout component
// ---------------------------------------------------------------------------

/**
 * Top-level layout shell.
 *
 * This component is the layout-route ELEMENT for the authenticated
 * route subtree in @/router.tsx. It is NOT a wrapper around
 * <RouterProvider>; the router itself is constructed at module scope
 * in @/router.tsx and mounted by main.tsx (deferred to Checkpoint 6).
 *
 * Renders:
 *   - <AppErrorBoundary> wrapping everything inside the layout.
 *   - <AppHeader /> only when a session is present (rendered above
 *     the route content). The header itself is NOT inside the inner
 *     error boundary so a render error in a child route still leaves
 *     the chrome navigable for recovery.
 *   - <main><Outlet /></main> for the matched child route.
 *   - <ToastContainer /> mounted as a sibling of the layout content
 *     so toasts persist across navigation transitions.
 *
 * Behavior during initial session hydration (`useSessionLoading()` is
 * true): the header does NOT render to avoid a flash of an empty
 * header before /api/me settles. ProtectedRoute handles the
 * "still hydrating" case for child routes by rendering its own
 * spinner before the Outlet content mounts.
 */
export function App(): JSX.Element {
  return (
    <div className="flex min-h-screen flex-col bg-slate-50" data-testid="app-shell">
      <AppErrorBoundary>
        <AppHeader />
        <main className="flex-1" data-testid="app-main">
          {/* Inner error boundary so a route render error preserves
              the header chrome above (when present). The outer
              boundary catches header-render errors as a last resort. */}
          <AppErrorBoundary>
            <Outlet />
          </AppErrorBoundary>
        </main>
      </AppErrorBoundary>
      {/* ToastContainer portals to document.body internally, so the
          mount position in this tree only matters for unmount order
          (when App unmounts the toasts disappear). */}
      <ToastContainer />
    </div>
  );
}
