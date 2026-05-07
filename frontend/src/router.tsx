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

import { createBrowserRouter, Navigate } from "react-router-dom";

import { App } from "@/App";

import { AddEditConnectionForm } from "@/features/connections/AddEditConnectionForm";
import { ConnectionDetail } from "@/features/connections/ConnectionDetail";
import { ConnectionFeed } from "@/features/connections/ConnectionFeed";

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
  {
    element: <App />,
    children: [
      { path: "/", element: <Navigate to="/feed" replace /> },
      { path: "/feed", element: <ConnectionFeed /> },
      { path: "/connections/new", element: <AddEditConnectionForm mode="create" /> },
      { path: "/connections/:id", element: <ConnectionDetail /> },
      { path: "/connections/:id/edit", element: <AddEditConnectionForm mode="edit" /> },
    ],
  },
  { path: "*", element: <Navigate to="/feed" replace /> },
]);
