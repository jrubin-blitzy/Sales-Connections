/**
 * ProtectedRoute.tsx - Authentication route guard for the Sales-Connections SPA.
 *
 * Wraps every authenticated route in @/router.tsx and ensures:
 *   - Unauthenticated users are redirected to /login?next=<encoded-path>
 *     so post-login they land back where they tried to go.
 *   - During the initial session-hydration query (cold page load), a
 *     loading affordance is shown INSTEAD of the children, preventing
 *     a flash of authenticated UI before the redirect fires.
 *   - Authenticated users see the children unmodified.
 *
 * Per AAP Sec 0.7.1 invariant 7:
 *   "API-layer authorization is authoritative. The frontend's <RoleGate>
 *   is a UX courtesy; the backend RBAC decorator is the only authoritative
 *   gate."
 * The same principle applies here: this guard is a UX nicety. The backend's
 * @/api/client.ts wrapper is what actually rejects unauthenticated requests
 * (401 -> redirect to /login). This component prevents the SPA from
 * RENDERING authenticated views before the fetch wrapper gets a chance
 * to redirect.
 *
 * Behavior matrix:
 *
 *   isLoading | session  | Outcome
 *   --------- | -------- | ------------------------------------------------
 *   true      | (any)    | Render <SessionHydrationSpinner /> (avoid flash)
 *   false     | null     | Redirect to /login?next=<encoded-current-path>
 *   false     | non-null | Render children unchanged
 *
 * Usage (see frontend/src/router.tsx):
 *
 *   {
 *     path: "/feed",
 *     element: (
 *       <ProtectedRoute>
 *         <ConnectionFeed />
 *       </ProtectedRoute>
 *     ),
 *   }
 *
 * Coordination:
 *   - @/auth/AuthProvider exports `useSession()` returning
 *     `Session | null` and `useSessionLoading()` returning `boolean`.
 *   - @/api/client.ts handles 401 redirects for already-authenticated
 *     routes; this guard handles the cold-load case where the
 *     session-hydration query hasn't fired yet or has just resolved
 *     to "no session."
 *   - The LoginScreen component decodes the `next` parameter via
 *     useSearchParams() + decodeURIComponent() and navigates after
 *     successful login.
 */

import type { JSX, ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";

import { useSession, useSessionLoading } from "@/auth/AuthProvider";

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

/**
 * Props for the <ProtectedRoute> component.
 *
 * The interface is intentionally NOT exported - consumers in
 * `@/router.tsx` rely on TypeScript's structural inference from the
 * JSX attribute syntax (`<ProtectedRoute>...</ProtectedRoute>`) so
 * there is no public name to import. Keeping the interface module-private
 * also discourages downstream code from coupling to a shape that may
 * gain optional props (e.g., `loadingComponent`) in a future revision.
 */
interface ProtectedRouteProps {
  /**
   * The route content to render when the user has an authenticated session.
   * Typically a feature page like <ConnectionFeed /> or <ConnectionDetail />.
   *
   * Typed as ReactNode (not JSX.Element) so callers can pass any valid
   * React content - elements, fragments, strings, arrays - without
   * needing to wrap simple values in extra fragments.
   */
  children: ReactNode;
}

// ---------------------------------------------------------------------------
// SessionHydrationSpinner - cold-load loading affordance
// ---------------------------------------------------------------------------

/**
 * Loading indicator shown while the session-hydration query is in flight.
 *
 * Rendered for the brief window between the SPA's initial mount and the
 * resolution of GET /api/me. Without this affordance, the children would
 * render with a `null` session and the user would briefly see a flash of
 * "logged out" UI (or the redirect would fire halfway through a paint)
 * before the AuthProvider settles.
 *
 * Styled with TailwindCSS utilities only per AAP Sec 0.7.7:
 *   - Full-viewport flex centering via `min-h-screen flex items-center
 *     justify-center` keeps the spinner visually anchored regardless of
 *     the surrounding layout chrome (or absence thereof - the spinner
 *     renders as the entire page during this window, replacing children).
 *   - The spinning ring uses `border-4 border-slate-200 border-t-brand-600
 *     animate-spin` so the brand color marks the leading edge of the
 *     rotation; the slate-200 background ring provides contrast.
 *   - Accessibility: `role="status"`, `aria-live="polite"`, and
 *     `aria-busy="true"` on the wrapper announce the loading state to
 *     screen readers. The visible label "Verifying session" is hidden
 *     from sighted users with the `sr-only` Tailwind utility but
 *     surfaces to assistive technology so the state has a name.
 *   - `data-testid="protected-route-spinner"` enables tests to assert
 *     the loading state without coupling to visual class names that
 *     could change with theme tweaks.
 *
 * Extracted as a named function (not inlined) so future revisions can
 * swap it for a branded skeleton or an <AppLayout> shell with a
 * content placeholder by changing only this single declaration.
 */
function SessionHydrationSpinner(): JSX.Element {
  return (
    <div
      role="status"
      aria-live="polite"
      aria-busy="true"
      className="min-h-screen flex items-center justify-center bg-slate-50"
    >
      <div
        className="h-12 w-12 rounded-full border-4 border-slate-200 border-t-brand-600 animate-spin"
        data-testid="protected-route-spinner"
      />
      <span className="sr-only">Verifying session</span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// ProtectedRoute - the route guard
// ---------------------------------------------------------------------------

/**
 * Route guard requiring an authenticated session.
 *
 * Behavior matrix:
 *
 *   isLoading | session  | Outcome
 *   --------- | -------- | ------------------------------------------------
 *   true      | (any)    | Render <SessionHydrationSpinner /> (avoid flash)
 *   false     | null     | Redirect to /login?next=<encoded-current-path>
 *   false     | non-null | Render children unchanged
 *
 * The `?next=` query param preserves the originally-requested URL so the
 * LoginScreen can navigate back after successful authentication. The
 * pathname AND search are preserved (e.g., a filtered feed URL like
 * `/feed?involvement=Warm+Intro` round-trips through login intact).
 * `location.hash` is intentionally omitted - SPA fragment identifiers
 * are rarely meaningful for route restoration and complicate encoding.
 *
 * Implementation notes:
 *   - <Navigate replace> swaps the current history entry instead of
 *     pushing a new one; without `replace`, hitting Back from /login
 *     would return to the protected route, redirect again, and bounce
 *     back to /login - a confusing perceptual loop.
 *   - encodeURIComponent on the `next` value handles reserved chars
 *     (`?`, `#`, `&`, `=`, `+`) that would otherwise break the parent
 *     query string. The LoginScreen's decodeURIComponent reverses this.
 *   - The fragment `<>{children}</>` converts ReactNode (which could
 *     be a string or array) into a JSX.Element that satisfies the
 *     return type without introducing an extra DOM node.
 *   - Declarative <Navigate> (not imperative useEffect + useNavigate)
 *     avoids the one-render-cycle gap during which children would
 *     briefly mount with a null session - the React-Router 6.x idiom.
 *   - useLocation() (not window.location) is the route-aware location
 *     synchronized with the SPA's history; window.location can drift
 *     from React Router state during transitions.
 *   - No try/catch around useSession(): if the AuthProvider is missing
 *     from the tree (a misconfiguration), useSession() throws a clear
 *     error that we let propagate so the bug is loud during development.
 *
 * @example
 *   <Route
 *     path="/feed"
 *     element={
 *       <ProtectedRoute>
 *         <ConnectionFeed />
 *       </ProtectedRoute>
 *     }
 *   />
 */
export function ProtectedRoute({ children }: ProtectedRouteProps): JSX.Element {
  const session = useSession();
  const isLoading = useSessionLoading();
  const location = useLocation();

  // Branch 1: While the session-hydration query is in flight, show a
  // spinner instead of the children. This prevents the briefly-flashed
  // unauthenticated UI before the redirect to /login fires - which is
  // both a UX problem (jarring) and a perception problem (users may
  // see record counts/names before the redirect).
  if (isLoading) {
    return <SessionHydrationSpinner />;
  }

  // Branch 2: Hydration settled with no session -> redirect to /login.
  // Preserve the originally-requested URL via the `next` query parameter
  // so the LoginScreen can route back after successful authentication.
  // location.pathname + location.search captures the full app-relative
  // URL; the hash is intentionally omitted (see header notes).
  if (session === null) {
    const currentPath = `${location.pathname}${location.search}`;
    const next = encodeURIComponent(currentPath);
    return <Navigate to={`/login?next=${next}`} replace />;
  }

  // Branch 3: Authenticated session present; render children unchanged.
  // The fragment wrapper preserves React's children typing without
  // introducing extra DOM nodes - returning bare `children` would not
  // satisfy the JSX.Element return type because ReactNode is wider
  // than JSX.Element (it includes strings and arrays).
  return <>{children}</>;
}
