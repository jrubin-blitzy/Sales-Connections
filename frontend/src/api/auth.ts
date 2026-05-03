/**
 * auth.ts - TanStack Query 5.x hooks for F-012 User Authentication.
 *
 * Hooks exposed:
 *   - useSessionQuery()    GET /api/me (session hydration)
 *   - useLoginMutation()   POST /auth/login (email + password fallback)
 *   - useLogoutMutation()  POST /auth/logout
 *
 * The Google OAuth flow (GET /auth/google/start, /callback) is a
 * server-side redirect chain handled entirely by Flask + Authlib; the
 * SPA navigates the browser directly via window.location, NOT through
 * a TanStack Query hook. So no hook is provided here for OAuth.
 *
 * Cache key conventions:
 *   - Session: ['auth', 'session']
 *   - The session query is the SOLE consumer of this key; mutations
 *     invalidate (login) or clear (logout) it.
 *
 * Coordination with @/auth/AuthProvider:
 *   - useSessionQuery() is the data source for the AuthProvider's
 *     session context. The provider calls this hook once at mount and
 *     re-renders descendants when the cache updates.
 *   - On login, useLoginMutation invalidates ['auth', 'session'] so the
 *     provider re-fetches and picks up the freshly-authenticated user.
 *   - On logout, useLogoutMutation calls queryClient.clear() to purge
 *     ALL caches (not just the session) per AAP Sec 0.7.4 "Tokens
 *     rotated on logout" - preventing stale data from a previous user
 *     session from leaking to the next user on the same browser.
 *
 * Coordination with @/lib/correlationId:
 *   - On login and logout, resetCorrelationId() is called so subsequent
 *     requests carry a fresh correlation ID, distinguishing the
 *     pre-auth and post-auth log streams cleanly per AAP Sec 0.7.5.
 *
 * Toast feedback:
 *   The mutation hooks fire toast.success / toast.error in their
 *   onSuccess / onError callbacks. The toast facade is obtained via
 *   the useToast() hook (Toast.tsx exposes the toast helpers via that
 *   hook rather than as a module-level singleton, so we call
 *   useToast() at the top of each mutation hook - allowed by the
 *   React rules of hooks because the call site is itself a hook).
 *
 * Per AAP Sec 0.7.4 Security Invariants:
 *   - The session JWT is delivered via HttpOnly cookie ONLY; never
 *     in any response body. None of these hooks parse or expose a
 *     session credential.
 *   - useSessionQuery uses skipAuthRedirect: true on its GET /api/me
 *     call so the api/client.ts wrapper does NOT redirect to /login on
 *     401. The 401 surfaces as ApiError(status=401) on query.error
 *     and the AuthProvider treats it as the unauthenticated state.
 *     Without this flag, every cold page load would trigger an
 *     immediate /login redirect even on routes that gracefully handle
 *     anonymous visitors (e.g., the /login route itself).
 *   - useLoginMutation and useLogoutMutation also pass
 *     skipAuthRedirect: true defensively. Login is unauthenticated by
 *     definition, so a 401 there means "wrong credentials" not
 *     "session expired"; redirecting would bounce the user away from
 *     the form. Logout is fine to attempt even when already logged
 *     out, so a 401 there is also harmless and should not redirect.
 *
 * Coordination touchpoints:
 *   - @/api/client            apiGet, apiPost, ApiError.
 *   - @/lib/correlationId     resetCorrelationId() at auth boundaries.
 *   - @/schemas/auth          Type-only imports for request/response
 *                              shapes (LoginRequest, LoginResponse,
 *                              SessionRead).
 *   - @/components/ui/Toast   useToast() returns the toast facade with
 *                              success(), error(), etc. methods.
 *   - @tanstack/react-query   useQuery, useMutation, useQueryClient,
 *                              UseQueryResult, UseMutationResult.
 *
 * Conventions per AAP Sec 0.7.7 and frontend/.prettierrc.json:
 *   - Strict TypeScript; explicit return types on every hook.
 *   - No `any` types.
 *   - Double quotes; trailing commas; line length <= 100.
 *   - `type` modifier on type-only imports (verbatimModuleSyntax).
 *   - No emoji - strict ASCII.
 *   - Named exports only - no default exports.
 *   - No direct fetch() calls - every HTTP via @/api/client.
 *   - No business logic - pure HTTP plumbing with cache-management
 *     side effects.
 */

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";

import { apiGet, apiPost, type ApiError } from "@/api/client";
import { useToast } from "@/components/ui/Toast";
import { resetCorrelationId } from "@/lib/correlationId";
import type { LoginRequest, LoginResponse, SessionRead } from "@/schemas/auth";

// ---------------------------------------------------------------------------
// Cache key factory
// ---------------------------------------------------------------------------

/**
 * Cache keys for authentication queries.
 *
 * Only one query lives under this namespace (the session). Defined as
 * a factory for parity with connectionKeys / tagKeys / adminKeys and
 * for consistency in the codebase.
 *
 * Members:
 *   - all       Root tuple ['auth']. Useful as a broad-stroke
 *               invalidation target if more auth queries are added in
 *               the future (e.g., an MFA-enrollment query).
 *   - session() Tuple ['auth', 'session'] keying the GET /api/me
 *               query. Used by useSessionQuery as queryKey and by
 *               useLoginMutation as the invalidation target on
 *               successful login.
 *
 * The factory pattern (functions, not literal arrays) makes the
 * compiler distinguish the literal types of each tuple - useful when
 * passing into queryClient.invalidateQueries({ queryKey: ... }) where
 * the key shape is type-checked.
 */
export const authKeys = {
  /** Root key shared by all authentication queries. */
  all: ["auth"] as const,
  /** Key for the session-hydration query (GET /api/me). */
  session: () => [...authKeys.all, "session"] as const,
};

// ---------------------------------------------------------------------------
// useSessionQuery - GET /api/me
// ---------------------------------------------------------------------------

/**
 * `useQuery` hook for session hydration (`GET /api/me`).
 *
 * Consumed by frontend/src/auth/AuthProvider.tsx as the single source
 * of session truth. The AuthProvider's context value is derived from
 * `query.data` (the SessionRead) and `query.isLoading` / `query.isError`
 * to render loading state and unauthenticated state respectively.
 *
 * Behavior:
 *   - On mount: GET /api/me is fired immediately.
 *   - 200 response: query.data = SessionRead (carries the authenticated
 *     user shape and an `authenticated: true` flag); the AuthProvider
 *     exposes useSession() returning a non-null Session.
 *   - 401 response: query.error is ApiError with status === 401; the
 *     AuthProvider treats this as "not logged in" and renders the
 *     <LoginScreen> via <ProtectedRoute> redirect. CRITICALLY, the
 *     fetch wrapper's 401-redirect-to-/login is BYPASSED here via
 *     skipAuthRedirect: true - otherwise the page would redirect to
 *     /login on every cold load, which is correct behavior but creates
 *     a visible flash. Letting the AuthProvider decide is cleaner and
 *     also lets the /login route itself handle anonymous visitors
 *     gracefully (the wrapper guards against /login -> /login loops
 *     but a per-query opt-out is more explicit).
 *
 * Per-hook overrides (relative to the QueryClient defaults declared in
 * frontend/src/lib/queryClient.ts):
 *   - retry: false. Overrides the default predicate (which would not
 *     retry 4xx anyway, but explicit here so unauthenticated state is
 *     reached fast - no waiting for retry attempts on 5xx outages).
 *     Session hydration is on the critical render path; failing
 *     quickly to "unauthenticated" is preferable to lingering in the
 *     loading state during a backend hiccup.
 *   - refetchOnWindowFocus: true. Overrides the default of false.
 *     When the user tabs back to the SPA, re-check the session; if it
 *     expired (or was logged out in another tab), this query catches
 *     it and the AuthProvider redirects. This is the ONE query in the
 *     app that benefits from focus-refetch per AAP Sec 0.5.4.
 *   - staleTime: 30 seconds. Avoids hammering /api/me on every
 *     navigation but still re-checks reasonably often. Shorter than
 *     the QueryClient default of 60 s because session staleness has
 *     security implications (a revoked role should propagate quickly).
 *
 * @returns The TanStack Query result. Consumers read `data` (SessionRead),
 *          `error` (ApiError; status === 401 means unauthenticated),
 *          `isLoading` (initial fetch in flight), and `isError`.
 */
export function useSessionQuery(): UseQueryResult<SessionRead, ApiError> {
  return useQuery<SessionRead, ApiError>({
    queryKey: authKeys.session(),
    queryFn: () => apiGet<SessionRead>("/api/me", { skipAuthRedirect: true }),
    retry: false,
    refetchOnWindowFocus: true,
    staleTime: 30 * 1000,
  });
}

// ---------------------------------------------------------------------------
// useLoginMutation - POST /auth/login
// ---------------------------------------------------------------------------

/**
 * `useMutation` hook for email/password login (`POST /auth/login`).
 *
 * Variables: `LoginRequest` (email + password).
 * Result data: `LoginResponse` (carries the authenticated user info;
 *              the JWT itself is in the HttpOnly cookie set by the
 *              response's Set-Cookie header, NOT in the response
 *              body - per AAP Sec 0.7.4 invariant 1).
 *
 * On success:
 *   1. Reset the correlation ID via resetCorrelationId() so post-login
 *      requests carry a fresh correlation context (per AAP Sec 0.7.5).
 *      Pre-login and post-login traffic should be logically distinct
 *      streams in the observability platform.
 *   2. Invalidate the session query so AuthProvider re-fetches /api/me
 *      and picks up the freshly-authenticated user. Setting the cache
 *      directly via setQueryData would also work (the LoginResponse
 *      already carries the user shape), but invalidation guarantees
 *      fresh data from the canonical endpoint and decouples this
 *      hook from the SessionRead shape (which has an `authenticated`
 *      flag that LoginResponse does not). Cleaner.
 *   3. Show success toast.
 *
 * On error:
 *   - 401 (invalid credentials): show generic toast "Login failed -
 *     check your email and password." Do NOT show the server's exact
 *     message because it might leak whether the email is registered.
 *     Backend already uses a generic message, but we override here for
 *     defense in depth - even if a future backend change accidentally
 *     leaks the email-existence signal, the SPA still shows a generic
 *     message.
 *   - Other statuses (422 malformed body, 5xx, network): show the
 *     server's specific message (or a generic fallback). Zod should
 *     have caught 422-shaped errors before the mutation fired, so
 *     reaching this branch typically means the SPA and backend
 *     schemas have drifted - showing the server message helps diagnose
 *     the drift.
 *
 * Note: This hook does NOT navigate to /feed. The component invoking
 * this mutation (LoginScreen.tsx) is responsible for the post-login
 * navigation, typically via react-router's useNavigate. Keeping
 * navigation out of the hook makes it reusable from future surfaces
 * (e.g., a re-authentication modal that should NOT redirect).
 *
 * @returns The TanStack Query mutation result. Consumers call
 *          `mutation.mutate(payload)` or `mutation.mutateAsync(payload)`
 *          and read `data`, `error`, `isPending` for UI feedback.
 */
export function useLoginMutation(): UseMutationResult<LoginResponse, ApiError, LoginRequest> {
  const queryClient = useQueryClient();
  // useToast is itself a React hook; calling it here (inside another
  // hook) is permitted by the React rules of hooks because the call
  // site is reached through the same component tree path on every
  // render. The returned facade is referentially stable across renders.
  const toast = useToast();

  return useMutation<LoginResponse, ApiError, LoginRequest>({
    mutationFn: (payload) =>
      apiPost<LoginResponse, LoginRequest>("/auth/login", payload, {
        skipAuthRedirect: true,
      }),
    onSuccess: () => {
      // Fresh correlation ID for the post-login session per AAP Sec 0.7.5.
      resetCorrelationId();
      // Trigger a re-fetch of /api/me so AuthProvider picks up the new
      // session. void-prefixed because invalidateQueries returns a
      // promise we deliberately do not await (the cache flips
      // immediately to a refetching state).
      void queryClient.invalidateQueries({ queryKey: authKeys.session() });
      toast.success("Signed in");
    },
    onError: (error) => {
      // Map known error codes to friendlier messages; fall through to
      // the server message for everything else. Per AAP Sec 0.7.4
      // (defense in depth), the 401 message is intentionally generic
      // to avoid leaking whether the email is registered.
      if (error.status === 401) {
        toast.error("Login failed. Check your email and password.");
        return;
      }
      // ApiError.message is always populated (the client.ts wrapper
      // falls back to a default-message-for-status string when the
      // backend envelope is malformed), so the `||` branch is defensive.
      toast.error(error.message || "Sign-in failed. Please try again.");
    },
  });
}

// ---------------------------------------------------------------------------
// useLogoutMutation - POST /auth/logout
// ---------------------------------------------------------------------------

/**
 * Result body shape for `POST /auth/logout`.
 *
 * The backend returns `{ "status": "ok" }` (string, not boolean) per
 * the auth blueprint contract. Modeled here as a local interface so
 * the mutation result type does not depend on importing yet another
 * schema for a single-field response.
 */
interface LogoutResponse {
  /** Always "ok" on a successful logout response. */
  readonly status: string;
}

/**
 * `useMutation` hook for logout (`POST /auth/logout`).
 *
 * Variables: void (no payload). The endpoint requires no body; the
 * server identifies the session via the HttpOnly cookie.
 * Result data: `{ status: "ok" }` per backend contract.
 *
 * On success:
 *   1. queryClient.clear() - purge ALL caches per AAP Sec 0.7.4
 *      "Tokens rotated on logout". This prevents data from a previous
 *      user (records, tags, admin views, analytics) from being visible
 *      on the same browser after logout. Without this, navigating
 *      back via the browser's back button could show cached records
 *      that the new user shouldn't see. clear() purges everything
 *      synchronously, removing that data from memory before any
 *      refetch fires - this is a SECURITY posture, not just a UX
 *      nicety.
 *   2. Reset the correlation ID so post-logout requests carry a fresh
 *      correlation context (per AAP Sec 0.7.5).
 *   3. Show success toast.
 *
 * On error:
 *   - The cookie may or may not be invalidated server-side. To be safe,
 *     still clear the cache and reset the correlation ID; the user can
 *     retry login. The client-side cache could still hold the prior
 *     user's data, so clearing on both success AND error means the
 *     user always lands in a "fully logged out" client state. If the
 *     server still considers them authenticated, the next request will
 *     either succeed (and refresh the cache from scratch) or 401-
 *     redirect to /login - either way, no stale data leaks.
 *   - Show a softer error message because the user just wanted to
 *     leave, and from their perspective the action succeeded
 *     (client-side cache was cleared).
 *
 * Note: This hook does NOT navigate to /login. The component invoking
 * this mutation (a logout button in the app shell) is responsible for
 * the post-logout navigation, typically via react-router's useNavigate.
 *
 * Note: The session cookie is cleared by the server's
 * `Set-Cookie: session=; Max-Age=0` header. The browser handles cookie
 * deletion automatically; this hook does NOT need to manipulate
 * document.cookie (and could not, since the cookie is HttpOnly).
 *
 * @returns The TanStack Query mutation result. Consumers call
 *          `mutation.mutate()` (no argument needed) or
 *          `mutation.mutateAsync()` and read `isPending` to show a
 *          spinner on the logout button.
 */
export function useLogoutMutation(): UseMutationResult<LogoutResponse, ApiError, void> {
  const queryClient = useQueryClient();
  // useToast is itself a React hook; calling it here (inside another
  // hook) is permitted by the React rules of hooks because the call
  // site is reached through the same component tree path on every
  // render. The returned facade is referentially stable across renders.
  const toast = useToast();

  return useMutation<LogoutResponse, ApiError, void>({
    mutationFn: () =>
      apiPost<LogoutResponse, Record<string, never>>(
        "/auth/logout",
        {},
        { skipAuthRedirect: true },
      ),
    onSuccess: () => {
      // Purge ALL caches: records, tags, admin data, analytics. Per
      // AAP Sec 0.7.4 this is part of the "tokens rotated on logout"
      // posture - no data from the prior session should be visible to
      // the next user on this browser.
      queryClient.clear();
      // Fresh correlation ID for the post-logout context.
      resetCorrelationId();
      toast.success("Signed out");
    },
    onError: () => {
      // Best-effort: clear caches and reset correlation even on
      // server-side error (cookie state on the server is uncertain;
      // safest to treat as logged out client-side). Per AAP Sec 0.7.4,
      // this is the SECURITY-correct posture even if it costs us a
      // toast.error against an arguably-successful client-side state.
      queryClient.clear();
      resetCorrelationId();
      toast.error("Sign-out had an issue, but your session is cleared.");
    },
  });
}
