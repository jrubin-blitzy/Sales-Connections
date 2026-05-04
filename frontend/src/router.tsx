/**
 * router.tsx - Client-side route table for the Sales-Connections SPA.
 *
 * Uses react-router-dom 6.x's data-router API (createBrowserRouter). The
 * exported `router` constant is consumed by <RouterProvider router={router} />
 * in @/main.tsx (or @/App.tsx). This file is the single source of truth for
 * every URL the SPA recognizes.
 *
 * Route catalog (per AAP Sec 0.2.3):
 *
 *   /                         -> redirects to /feed (or /login if no session)
 *   /login                    -> LoginScreen (F-012)
 *   /feed                     -> ConnectionFeed (F-004)
 *   /connections/new          -> AddEditConnectionForm in create mode (F-001)
 *   /connections/:id          -> ConnectionDetail (F-011)
 *   /connections/:id/edit     -> AddEditConnectionForm in edit mode (F-001/F-007)
 *   /admin                    -> AdminPanel root (F-014); index redirects to /admin/users
 *   /admin/users              -> UserManagement (F-009/F-014)
 *   /admin/records            -> RecordModeration (F-007/F-014)
 *   /admin/analytics          -> Analytics (F-014)
 *   /auth/google/start        -> server-side OAuth start (full-page redirect to Flask)
 *   /auth/google/callback     -> server-side OAuth callback (Flask 302s to /feed)
 *   *                         -> catch-all; redirects unknown paths to /feed
 *
 * Defense-in-depth:
 *   - Layer 1 (UI): every route except /login and /auth/* is wrapped in
 *     <ProtectedRoute>, which redirects unauthenticated visitors to
 *     /login?next=<encoded-original-path>. Admin routes additionally wrap
 *     in <RoleGate role="Admin">, which renders a fallback <Navigate to="/feed">
 *     for non-Admin sessions.
 *   - Layer 2 (API): the backend's RBAC middleware (@requires_role decorator
 *     on every protected handler in backend/app/api/*.py) is the authoritative
 *     security boundary per AAP Sec 0.7.1 invariant 7. A determined attacker
 *     can bypass the UI guards (via DevTools, direct curl, etc.); the backend
 *     rejects unauthorized requests with HTTP 403 in every case.
 *
 *   The two layers are NOT substitutes; both must be present. Skipping
 *   Layer 1 leads to a confusing UX (403 toasts on click); skipping Layer 2
 *   is a security incident. This file implements Layer 1; the backend
 *   middleware implements Layer 2.
 *
 * Why createBrowserRouter (data router) and not the older <BrowserRouter>:
 *   The data-router API (react-router-dom 6.4+) is the modern idiom. It
 *   enables future use of `loader` and `action` per-route data fetching,
 *   which would let TanStack Query become a pure client-state cache. We do
 *   NOT use loaders/actions in MVP (TanStack Query handles all data
 *   fetching), but adopting the data-router API now positions the project
 *   to add them later without breaking changes.
 *
 * Why a catch-all to /feed instead of a styled NotFound page:
 *   For MVP simplicity. The AAP scope (Sec 0.2.3) does not list a NotFound
 *   component. A future enhancement (logged in docs/decision-log.md as a
 *   suggested next task per AAP Sec 0.7.5) is a friendlier dedicated 404.
 *
 * Conventions per AAP Sec 0.7.7 and frontend/.prettierrc.json:
 *   - Double quotes for all strings (singleQuote: false in Prettier config).
 *   - Strict TypeScript; no `any`; explicit return types on every helper
 *     component.
 *   - Named export; no default export per project convention.
 *   - Path imports use the @/ alias for src.
 *   - Type-only imports use the `type` modifier (verbatimModuleSyntax: true
 *     in tsconfig.json).
 *   - Trailing commas; line length <= 100.
 *   - No emoji - strict ASCII.
 *
 * Coordinates with:
 *   - @/auth/ProtectedRoute       Wrap authenticated routes; redirects to /login.
 *   - @/auth/RoleGate             Wrap admin routes; UI-level role check.
 *   - @/features/auth/LoginScreen The /login surface (Google OAuth + email/password).
 *   - @/features/connections/*    Feed, detail, and add/edit form surfaces.
 *   - @/features/admin/*          Admin panel layout and three tab views.
 *   - @/main.tsx                  Mounts <RouterProvider router={router} />.
 *
 * react-refresh/only-export-components is intentionally disabled for this
 * file: the AAP (Sec 0.2.3 frontend tier) mandates a single `router.tsx`
 * that exports a `router` constant and contains the route-element JSX. The
 * two private helper components (OAuthStartRedirect, OAuthCallbackFallback)
 * exist solely to satisfy specific route entries; splitting them into
 * separate files would violate AAP Sec 0.6.1 ("only create files explicitly
 * mentioned in AAP") and provide no architectural benefit. HMR fast-refresh
 * is not affected in practice because (a) the helpers are not exported and
 * thus not tracked by Fast Refresh, and (b) edits to the router constant
 * itself trigger a full reload regardless of this rule because the router
 * instance is created once at module-load time and held by RouterProvider.
 */
/* eslint-disable react-refresh/only-export-components */

import { useEffect, type JSX } from "react";
import { createBrowserRouter, Navigate } from "react-router-dom";

import { App } from "@/App";
import { ProtectedRoute } from "@/auth/ProtectedRoute";
import { RoleGate } from "@/auth/RoleGate";

import { AdminPanel } from "@/features/admin/AdminPanel";
import { Analytics } from "@/features/admin/Analytics";
import { RecordModeration } from "@/features/admin/RecordModeration";
import { UserManagement } from "@/features/admin/UserManagement";

import { LoginScreen } from "@/features/auth/LoginScreen";

import { AddEditConnectionForm } from "@/features/connections/AddEditConnectionForm";
import { ConnectionDetail } from "@/features/connections/ConnectionDetail";
import { ConnectionFeed } from "@/features/connections/ConnectionFeed";

// ---------------------------------------------------------------------------
// OAuth helper components
// ---------------------------------------------------------------------------

/**
 * Performs a full-page navigation to the backend's /auth/google/start endpoint.
 *
 * The /auth/google/start route is handled server-side by Flask (per AAP
 * Sec 0.4.5): Flask issues a 302 redirect to Google's authorization URL with
 * state and PKCE parameters. The Vite dev-server proxy (configured in
 * vite.config.ts) forwards /auth/* to the Flask backend, so in normal flow
 * the SPA never actually renders this route - the browser navigates directly
 * to Flask before any React rendering occurs.
 *
 * This component exists for completeness and as a fallback for the rare
 * cases where a user lands on /auth/google/start via a direct URL or a
 * stale bookmark and the proxy has been bypassed (e.g., in production
 * environments where the proxy lives in nginx/ALB rather than Vite).
 *
 * Implementation notes:
 *   - useEffect runs after mount, so the side effect is not performed during
 *     render (which would violate React's rules-of-render). The empty
 *     dependency array ensures the navigation fires exactly once per mount.
 *   - window.location.replace (not assign) is chosen so the back button does
 *     not return the user to this transient redirect page; instead, it skips
 *     past it to the page they were on before /auth/google/start.
 *   - The function returns null because there is nothing to render: the
 *     browser is about to navigate away. A spinner could be added, but the
 *     navigation typically completes within a few hundred milliseconds, far
 *     too brief for a useful loading affordance.
 *   - Strict Mode in development invokes effects twice. The second
 *     window.location.replace call is a no-op because the navigation
 *     initiated by the first call has already taken control of the browser.
 *
 * @returns null - nothing renders; the browser is about to navigate away.
 */
function OAuthStartRedirect(): null {
  useEffect(() => {
    // Use replace (not assign) so the back button does not return to this
    // transient redirect page. The browser will navigate to Flask, which
    // 302s to Google's authorization URL.
    window.location.replace("/auth/google/start");
  }, []);
  return null;
}

/**
 * Fallback element for the /auth/google/callback route.
 *
 * The /auth/google/callback route is handled server-side by Flask (per AAP
 * Sec 0.4.5): Flask validates the state cookie, exchanges the OAuth code for
 * an ID token, validates the ID token signature against Google's JWKS,
 * upserts the user, mints a session JWT, sets it as an HttpOnly cookie, and
 * 302-redirects to /feed. The SPA should never render this route in the
 * happy path because the Vite dev-server proxy (and nginx/ALB in production)
 * forwards /auth/* to Flask before React boots.
 *
 * This component exists for the edge case where the proxy is misconfigured
 * (e.g., a contributor running the SPA without the Flask backend, or a
 * production deploy where /auth/* is incorrectly routed to the SPA bundle).
 * In that case, redirecting to /feed lets the AuthProvider's session-hydration
 * query fire against /api/me. If a session cookie was set by the OAuth
 * callback, the user lands on the feed; otherwise, ProtectedRoute bounces
 * them to /login.
 *
 * @returns A <Navigate> element that redirects to /feed with `replace`
 *          history semantics so the back button does not return here.
 */
function OAuthCallbackFallback(): JSX.Element {
  return <Navigate to="/feed" replace />;
}

// ---------------------------------------------------------------------------
// Route table
// ---------------------------------------------------------------------------

/**
 * The complete client-side route table for the Sales-Connections SPA.
 *
 * Consumed by <RouterProvider router={router} /> in main.tsx. The export
 * shape (`const`, not `function`) is deliberate: createBrowserRouter must be
 * invoked once at module-load time so the router instance is shared across
 * the application; constructing it inside a component would re-create the
 * router on every render and break navigation.
 *
 * Route ordering rationale:
 *   1. Public auth routes first: /login and /auth/google/* are NOT wrapped
 *      in <ProtectedRoute> so unauthenticated users can reach them. Putting
 *      them first makes the public surface visually obvious in this file.
 *   2. Authenticated routes next: each is wrapped in <ProtectedRoute>,
 *      which redirects to /login?next=<encoded-path> when there is no
 *      session.
 *   3. Admin routes: wrapped in BOTH <ProtectedRoute> (must be authenticated)
 *      AND <RoleGate role="Admin"> (must be an Admin role). Non-Admin
 *      sessions are redirected to /feed by the RoleGate fallback.
 *   4. Catch-all last: any unmatched URL redirects to /feed; ProtectedRoute
 *      then handles the unauthenticated case by bouncing to /login.
 */
export const router = createBrowserRouter([
  // =========================================================================
  // Public authentication surface (NOT wrapped in <ProtectedRoute>; NOT
  // wrapped in the App layout shell either, because the login screen and
  // the OAuth bounce surfaces should render WITHOUT the SPA chrome).
  // =========================================================================

  {
    path: "/login",
    element: <LoginScreen />,
  },
  {
    path: "/auth/google/start",
    element: <OAuthStartRedirect />,
  },
  {
    path: "/auth/google/callback",
    element: <OAuthCallbackFallback />,
  },

  // =========================================================================
  // Authenticated surface (wrapped in the App layout shell + ProtectedRoute)
  //
  // The App layout shell renders: outer <AppErrorBoundary>, conditional
  // <AppHeader> (visible only when authenticated), <main><Outlet /></main>,
  // and the global <ToastContainer />. Per AAP Section 0.7.1 invariant 7,
  // the App layout's <RoleGate> on the Admin nav link is a UI courtesy;
  // the backend RBAC decorator on every /api/admin/* endpoint is the
  // authoritative authorization gate. The App layout itself does NOT gate
  // authentication - that responsibility stays with <ProtectedRoute> on
  // each child route below so a forgotten ProtectedRoute does NOT silently
  // expose a route to unauthenticated visitors.
  //
  // Resolves the Checkpoint 5 MAJOR review finding ("App.tsx missing,
  // layout shell requirements (header/navigation/Toaster/ErrorBoundary)
  // not satisfied anywhere in the codebase") by mounting <App /> as the
  // layout-route element. Resolves the Checkpoint 5 MINOR finding ("No
  // top-level <ErrorBoundary> wrapping any route") via the AppErrorBoundary
  // class component inside App.tsx wrapping <Outlet /> for per-route render
  // failures plus an outer boundary catching header/chrome failures.
  // =========================================================================

  {
    element: <App />,
    children: [
      // Index route: the bare "/" redirects to /feed. Wrapped in
      // <ProtectedRoute> so unauthenticated visitors are bounced to /login
      // first; the Navigate only fires for authenticated users, who then
      // land on /feed (the AAP-specified default landing surface for
      // Viewer and Contributor roles per AAP Sec 0.5.4).
      {
        path: "/",
        element: (
          <ProtectedRoute>
            <Navigate to="/feed" replace />
          </ProtectedRoute>
        ),
      },

      // F-004 Connection Feed / Dashboard: the primary surface for Sales
      // Reps (Viewer role) and Contributors. Filter and sort state is
      // encoded in URL search params (handled inside the ConnectionFeed
      // component via useSearchParams), NOT as separate routes - keeping
      // the route table minimal and the URL shareable.
      {
        path: "/feed",
        element: (
          <ProtectedRoute>
            <ConnectionFeed />
          </ProtectedRoute>
        ),
      },

      // F-001 Add Connection: mounts AddEditConnectionForm in create mode.
      // The component reads its `mode` prop to decide whether to load an
      // existing record (mode="edit") or start with empty state (mode="create").
      {
        path: "/connections/new",
        element: (
          <ProtectedRoute>
            <AddEditConnectionForm mode="create" />
          </ProtectedRoute>
        ),
      },

      // F-011 Connection Detail: shows all nine record fields plus the
      // edit-history feed (sourced from audit_events via
      // GET /api/connections/:id/history). The :id parameter is read inside
      // the component via useParams.
      {
        path: "/connections/:id",
        element: (
          <ProtectedRoute>
            <ConnectionDetail />
          </ProtectedRoute>
        ),
      },

      // F-001 / F-007 Edit Connection: mounts AddEditConnectionForm in edit
      // mode. The component reads :id via useParams and hydrates the form
      // by fetching GET /api/connections/:id. Edit authorization is
      // enforced by the backend (own record for Contributor, any record
      // for Admin); the UI does not gate this route because Contributors
      // should be able to navigate to their own records' edit form.
      {
        path: "/connections/:id/edit",
        element: (
          <ProtectedRoute>
            <AddEditConnectionForm mode="edit" />
          </ProtectedRoute>
        ),
      },

      // ======================================================================
      // Admin surface (Admin role required via <RoleGate>)
      //
      // F-014 Admin Panel: nested route tree. The /admin root mounts
      // AdminPanel (header banner + tab navigation + <Outlet />), and the
      // children render inside the outlet. Index route redirects /admin to
      // /admin/users so the user lands on a populated tab immediately
      // rather than seeing an empty outlet, matching the AAP Sec 0.5.4
      // description. The App layout's <Outlet /> hosts the AdminPanel,
      // which itself contains a nested <Outlet /> for users/records/analytics.
      //
      // RoleGate's `fallback` prop renders <Navigate to="/feed" replace />
      // for non-Admin sessions, surfacing the "you cannot view this surface"
      // outcome as a route redirect rather than a blank page. The backend's
      // @requires_role(Admin) decorator on every /api/admin/* endpoint is
      // the authoritative gate (per AAP Sec 0.7.1 invariant 7); this
      // RoleGate is a UI courtesy that prevents the broken admin UI from
      // rendering for users who would only see 403 errors anyway.
      // ======================================================================
      {
        path: "/admin",
        element: (
          <ProtectedRoute>
            <RoleGate role="Admin" fallback={<Navigate to="/feed" replace />}>
              <AdminPanel />
            </RoleGate>
          </ProtectedRoute>
        ),
        children: [
          // Default index: /admin -> /admin/users.
          { index: true, element: <Navigate to="/admin/users" replace /> },

          // F-009 / F-014 User management: list users, edit roles.
          { path: "users", element: <UserManagement /> },

          // F-007 / F-014 Record moderation: includes soft-deleted records
          // and the Admin-only hard-delete control.
          { path: "records", element: <RecordModeration /> },

          // F-014 Analytics: most active contributors, leads by status,
          // weekly activity sparkline.
          { path: "analytics", element: <Analytics /> },
        ],
      },
    ],
  },

  // =========================================================================
  // Catch-all (unknown URLs) - rendered OUTSIDE the layout shell so
  // unmatched paths are not subjected to the header chrome flicker
  // before the redirect fires.
  // =========================================================================

  // Any unmatched path redirects to /feed. ProtectedRoute on /feed then
  // handles the unauthenticated case by bouncing to /login. This is simpler
  // than a dedicated 404 component for MVP; a friendlier styled NotFound
  // page is logged as a suggested next task in docs/decision-log.md per
  // AAP Sec 0.7.5.
  {
    path: "*",
    element: <Navigate to="/feed" replace />,
  },
]);
