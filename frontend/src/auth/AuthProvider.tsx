/**
 * AuthProvider.tsx - Session and role context for the Sales-Connections SPA.
 *
 * Mounted by frontend/src/main.tsx inside the QueryClientProvider:
 *
 *   <QueryClientProvider client={queryClient}>
 *     <AuthProvider>          <- THIS COMPONENT
 *       <App />               <- contains <RouterProvider>
 *     </AuthProvider>
 *   </QueryClientProvider>
 *
 * Responsibilities:
 *   1. On mount, fire GET /api/me via useSessionQuery() (TanStack Query
 *      cache key ['auth', 'session']) to hydrate the user's session.
 *   2. Expose the resulting SessionRead via React context, with three
 *      consumption hooks:
 *
 *        useSession()          -> SessionRead | null
 *        useSessionLoading()   -> boolean (true during initial hydration)
 *        useRole()             -> { role: UserRole | null, has(target) }
 *
 *   3. Re-export useLogout() (which is useLogoutMutation from @/api/auth)
 *      for ergonomic access from logout buttons.
 *
 * Per AAP Sec 0.7.1 invariant 7:
 *   "API-layer authorization is authoritative. The frontend's <RoleGate>
 *   is a UX courtesy; the backend RBAC decorator is the only authoritative
 *   gate."
 *
 * The role exposed by useRole() is the role the BACKEND assigned in the
 * JWT claims when the session was minted. Mutating client-side state to
 * forge a different role does NOT grant any actual access; the backend
 * re-checks the role on every request via the @requires_role decorator.
 *
 * 401 redirect handling:
 *   This component does NOT redirect on 401. The @/api/client wrapper
 *   handles 401 redirects globally (including for non-session API calls
 *   that 401 mid-session). This component simply observes the session
 *   query state:
 *     - isPending (initial hydration in flight)  -> session = null
 *     - error (401 or other failure)             -> session = null
 *     - success                                  -> session = SessionRead
 *
 * Cache management on logout:
 *   useLogoutMutation (in @/api/auth) calls queryClient.clear() on success
 *   per AAP Sec 0.7.4 ("Tokens rotated on logout"). This component does
 *   not need to clear cache itself; it merely re-reads the session query,
 *   which after clear() returns to the loading state and then settles
 *   into "not authenticated" once the next /api/me call returns 401.
 *
 * Session shape (NESTED per backend response, NOT flat):
 *   SessionRead = {
 *     user: {
 *       id: string;
 *       email: string;
 *       display_name: string;
 *       role: 'Admin' | 'Contributor' | 'Viewer';
 *       created_at: string;
 *     };
 *     authenticated: boolean;
 *   }
 *
 *   Consumers needing user fields access them via session.user.id,
 *   session.user.email, session.user.display_name, session.user.role.
 *   The role union ('Admin' | 'Contributor' | 'Viewer') matches the
 *   backend's UserRole enum exactly per AAP Sec 0.1.2.
 *
 * react-refresh/only-export-components is intentionally disabled for
 * this file: the AAP requires <AuthProvider /> (a React component) and
 * four consumer hooks (useSession, useSessionLoading, useRole,
 * useLogout) to be co-located in this single context module. Splitting
 * them into separate files would violate AAP Sec 0.6.1 ("only create
 * files explicitly mentioned in AAP") and would break the standard
 * React Context idiom where the Provider and consumer hooks live
 * together so the context constant stays module-private. HMR
 * fast-refresh works correctly in practice because the hooks are
 * pure projections of the context value.
 */
/* eslint-disable react-refresh/only-export-components */

import { createContext, useContext, useMemo, type JSX, type ReactNode } from "react";
import type { UseMutationResult } from "@tanstack/react-query";

import { useLogoutMutation, useSessionQuery } from "@/api/auth";
import type { ApiError } from "@/api/client";
import type { Session, SessionRead } from "@/schemas/auth";
import { USER_ROLE_VALUES } from "@/schemas/admin";

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

/**
 * Three-role authorization model per F-009 and AAP Sec 0.1.2.
 * Matches backend `app.models.enums.UserRole` exactly:
 *   - 'Admin'       full platform control
 *   - 'Contributor' creates/edits own records
 *   - 'Viewer'      sales rep; mutates outreach status
 *
 * Derived from USER_ROLE_VALUES (single source of truth) so adding a
 * fourth role becomes a one-line schema change in @/schemas/admin.
 */
export type UserRole = (typeof USER_ROLE_VALUES)[number];

/**
 * Return value of `useRole()`.
 *
 * Provides BOTH the raw role (for components that need to render
 * something role-specific, like an admin badge) AND a predicate
 * (for components that need a yes/no role check, like <RoleGate>).
 */
export interface UseRoleReturn {
  /**
   * The current user's role, or `null` if there is no session.
   *
   * Examples of role-aware rendering:
   *   const { role } = useRole();
   *   if (role === 'Admin') return <AdminBadge />;
   */
  role: UserRole | null;

  /**
   * Predicate: returns `true` if the current user's role exactly equals
   * `target`. Returns `false` for the no-session case.
   *
   * Used by <RoleGate> for declarative role-based rendering.
   *
   *   const { has } = useRole();
   *   if (has('Admin')) showHardDeleteButton();
   *
   * Note: `has` is exact-match, not hierarchical. 'Admin' does NOT
   * imply 'Contributor' or 'Viewer'. This aligns with the backend's
   * `@requires_role(...)` decorator which also requires exact match.
   */
  has: (target: UserRole) => boolean;
}

// ---------------------------------------------------------------------------
// Internal context value
// ---------------------------------------------------------------------------

/**
 * Internal context value. Application code consumes it via the typed
 * hooks (useSession, useSessionLoading, useRole); the type is exported
 * primarily so the test harness at `frontend/tests/test-utils.tsx`
 * (specifically `MockAuthProvider`) can construct a synchronous, fully
 * typed mock context value without having to round-trip through MSW.
 *
 * The `session` field is `null` in two distinct cases:
 *   - Initial mount before /api/me has resolved (also: `isLoading` is true)
 *   - After /api/me settled with 401 (also: `isLoading` is false)
 *
 * Consumers gate on `isLoading` first to distinguish these two states.
 */
export interface AuthContextValue {
  /** The hydrated session, or `null` if unauthenticated/loading. */
  session: SessionRead | null;
  /** True during the initial /api/me hydration query. */
  isLoading: boolean;
  /** True if /api/me settled in error (typically 401). */
  isError: boolean;
}

/**
 * The AuthContext. Sentinel `undefined` distinguishes "no provider in
 * tree" from "provider with no session" - the former is a misconfiguration,
 * the latter is the unauthenticated state.
 *
 * A `null` initial value would be ambiguous (no session vs. no provider).
 * `undefined` lets us throw a clear error in the hook helpers when
 * `useContext` returns the initial value (i.e., the provider is missing).
 *
 * Exported (alongside `AuthContextValue`) so the test harness at
 * `frontend/tests/test-utils.tsx` can re-provide the context with a
 * synchronously controlled value via `MockAuthProvider`. Production
 * code MUST continue to consume the context only through the typed
 * hooks (useSession, useSessionLoading, useRole) - direct
 * `useContext(AuthContext)` reads outside the test harness would
 * bypass the missing-provider safety net those hooks implement.
 */
export const AuthContext = createContext<AuthContextValue | undefined>(undefined);

// ---------------------------------------------------------------------------
// AuthProvider component
// ---------------------------------------------------------------------------

/**
 * Root provider that hydrates session state and exposes it via context.
 *
 * Renders its `children` unconditionally (NOT gated by session state)
 * - gating is the responsibility of <ProtectedRoute> and <RoleGate>.
 * This separation of concerns is intentional: a Provider is a context
 * publisher, not a route gate. Gating in the Provider would make the
 * entire app blank during the brief hydration window - bad UX.
 *
 * @example
 *   <QueryClientProvider client={queryClient}>
 *     <AuthProvider>
 *       <App />
 *     </AuthProvider>
 *   </QueryClientProvider>
 */
export function AuthProvider({ children }: { children: ReactNode }): JSX.Element {
  // Fire GET /api/me once on mount; subsequent navigations reuse the
  // cache (30 s stale time per @/api/auth.ts). The session query opts
  // out of the global 401-redirect via skipAuthRedirect:true (per
  // @/api/auth.ts) so a missing/expired session resolves to an
  // ApiError with status=401 rather than a redirect; we treat that as
  // "not authenticated" and let <ProtectedRoute> handle the redirect.
  const sessionQuery = useSessionQuery();

  // Compute the context value. useMemo prevents identity churn on
  // unrelated re-renders (e.g., when QueryClient internals tick a
  // metric counter), which would otherwise re-render every consumer.
  //
  // The dependency list captures the four observable fields of the
  // query: isSuccess, data, isPending, isError. We deliberately omit
  // error details (e.g., status code) - consumers don't need them.
  // The ApiError is captured by the api/client.ts wrapper for logging.
  //
  // TanStack Query 5.x notes:
  //   - `isPending` (NOT v4's `isLoading`) is the correct loading
  //     indicator: it is `true` when the query has neither data nor
  //     error (the initial state) and `false` after ANY resolution.
  //     Refetches do NOT toggle it back to `true` - cached data is
  //     shown while the refetch is in flight.
  //   - `isSuccess`-narrowed `data` access is required because v5
  //     types `data` as `TData | undefined` until the query succeeds.
  //     Using `query.isSuccess ? query.data : null` narrows it
  //     properly. Without the narrowing, TypeScript would complain
  //     about possibly-undefined data.
  const value = useMemo<AuthContextValue>(
    () => ({
      session: sessionQuery.isSuccess ? sessionQuery.data : null,
      isLoading: sessionQuery.isPending,
      isError: sessionQuery.isError,
    }),
    [sessionQuery.isSuccess, sessionQuery.data, sessionQuery.isPending, sessionQuery.isError],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

// ---------------------------------------------------------------------------
// Internal helper: read context or throw on missing provider
// ---------------------------------------------------------------------------

/**
 * Internal helper: read the AuthContext value or throw if the provider
 * is missing. Throwing is preferable to returning a fallback because:
 *   - A missing provider is a configuration bug, not a runtime state.
 *   - Silent fallback would mask the bug and produce confusing
 *     unauthenticated UX where authentication should be present.
 *
 * @internal
 */
function useAuthContext(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (ctx === undefined) {
    throw new Error(
      "useSession/useSessionLoading/useRole must be used inside <AuthProvider>. " +
        "Verify that frontend/src/main.tsx wraps the app in <AuthProvider>.",
    );
  }
  return ctx;
}

// ---------------------------------------------------------------------------
// Public hook: useSession
// ---------------------------------------------------------------------------

/**
 * Returns the current session, or `null` if unauthenticated/loading.
 *
 * The hook does NOT distinguish "loading" from "unauthenticated" -
 * both produce `null`. Use `useSessionLoading()` to disambiguate.
 *
 * @example
 *   const session = useSession();
 *   if (session) {
 *     return <span>Hello, {session.user.display_name}</span>;
 *   }
 *
 * @example
 *   // Pattern with loading distinction:
 *   const session = useSession();
 *   const isLoading = useSessionLoading();
 *   if (isLoading) return <Spinner />;
 *   if (!session) return <NotAuthenticated />;
 *   return <AuthenticatedView session={session} />;
 *
 * @returns The session object, or null.
 */
export function useSession(): Session | null {
  return useAuthContext().session;
}

// ---------------------------------------------------------------------------
// Public hook: useSessionLoading
// ---------------------------------------------------------------------------

/**
 * Returns `true` while the initial GET /api/me query is in flight.
 *
 * Consumed by <ProtectedRoute> to render a loading spinner instead of
 * the children, preventing a flash of authenticated UI before the
 * session settles.
 *
 * After the first resolution (success OR error), this hook returns
 * `false` for the rest of the page's life. Refetches do NOT toggle
 * this back to `true` - the cached data is shown while the refetch
 * is in flight.
 *
 * @returns `true` during initial hydration; `false` thereafter.
 */
export function useSessionLoading(): boolean {
  return useAuthContext().isLoading;
}

// ---------------------------------------------------------------------------
// Public hook: useRole
// ---------------------------------------------------------------------------

/**
 * Returns role-related accessors derived from the current session.
 *
 * SECURITY NOTICE (per AAP Sec 0.7.1 invariant 7):
 *   The role exposed here is for UI gating ONLY. The backend's
 *   @requires_role decorator is the authoritative authorization gate.
 *   A user who tampers with this hook's return value gains NO actual
 *   privileges - the API rejects forbidden requests with HTTP 403.
 *
 * @example
 *   const { has } = useRole();
 *   if (has('Admin')) showHardDeleteButton();
 *
 * @example
 *   const { role } = useRole();
 *   return <span>You are signed in as a {role ?? 'guest'}.</span>;
 *
 * @returns `{ role, has }` where `role` is the current user's role
 *          (or null if no session) and `has(target)` is a predicate
 *          that returns true when role === target.
 */
export function useRole(): UseRoleReturn {
  const ctx = useAuthContext();
  // Optional chaining + nullish coalescing handle the no-session case
  // cleanly; `role` is `null` when there is no session, otherwise the
  // exact UserRole string from the JWT claims.
  const role: UserRole | null = ctx.session?.user.role ?? null;

  // Memoize the return so consumers using { role, has } in dependency
  // arrays don't see new identity on every render. The `has` closure
  // captures `role` directly; recreating only when `role` changes.
  return useMemo<UseRoleReturn>(
    () => ({
      role,
      has: (target: UserRole) => role === target,
    }),
    [role],
  );
}

// ---------------------------------------------------------------------------
// Public hook: useLogout
// ---------------------------------------------------------------------------

/**
 * Returns a TanStack Query mutation hook for logout.
 *
 * Wraps useLogoutMutation from @/api/auth; the wrapper exists so that
 * logout-button consumers don't need to import from two places. The
 * underlying mutation handles:
 *   - POST /auth/logout (server clears session cookie)
 *   - queryClient.clear() (purge ALL caches per AAP Sec 0.7.4)
 *   - resetCorrelationId() (fresh correlation context)
 *   - Success/error toasts
 *
 * The component consuming this hook is responsible for the post-logout
 * navigation (typically navigate('/login') via react-router-dom).
 *
 * The wrapper layer also lets us inject pre/post-logout logic later
 * (e.g., navigate-on-success, analytics) without breaking the
 * consumer API.
 *
 * @example
 *   const logout = useLogout();
 *   const navigate = useNavigate();
 *
 *   async function handleLogout() {
 *     await logout.mutateAsync();
 *     navigate('/login');
 *   }
 *
 * @returns The TanStack Query mutation result. Call `.mutate()` or
 *          `.mutateAsync()` to trigger the logout flow.
 */
export function useLogout(): UseMutationResult<{ status: string }, ApiError, void> {
  return useLogoutMutation();
}
