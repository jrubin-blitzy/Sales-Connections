/**
 * LoginScreen.tsx - F-012 User Authentication surface.
 *
 * Mounted at /login (the only public route; NOT wrapped in <ProtectedRoute>).
 *
 * Two authentication paths per AAP Sec 0.5.4:
 *   1. Google OAuth (primary): full-page navigation to /auth/google/start.
 *      Flask handles the OAuth dance and 302-redirects to /feed on success.
 *      No SPA-side fetch is involved.
 *   2. Email/password (fallback): submits via useLoginMutation() which
 *      calls POST /auth/login. The server validates credentials, mints
 *      the session JWT, and sets it as an HttpOnly cookie. The mutation
 *      invalidates the session query (so AuthProvider re-fetches via
 *      GET /api/me) and the LoginScreen navigates to ?next= or /feed.
 *
 * Already-authenticated guard:
 *   If the user is already authenticated (session present), the screen
 *   redirects them to ?next= or /feed via useEffect on mount and on
 *   session change. This prevents authenticated users from re-logging-in.
 *
 * Validation:
 *   Client-side via Zod (LoginRequestSchema) for UX. The backend is
 *   authoritative per AAP Sec 0.7.1 invariant 8. Field-level errors
 *   are surfaced inline; form-level (e.g., 401, 5xx) errors are
 *   surfaced both via the api/auth.ts toast plumbing and via a
 *   visible <p role="alert"> below the form for visual emphasis.
 *
 * Accessibility:
 *   - <main> landmark with aria-labelledby tying to the page heading
 *   - Semantic <form> with aria-describedby for the form-level error
 *   - <label htmlFor> associations via the Input primitive
 *   - role="alert" on the form-level error container (Input handles
 *     this for field-level errors internally)
 *   - Focus management: email input auto-focuses on mount
 *
 * Open-redirect protection:
 *   The ?next= query param is decoded and validated to be a same-origin
 *   relative path before being used as a navigation target. Protocol-
 *   relative URLs (starts with //) and any /auth/* or /login* path are
 *   rejected, defending against phishing links of the form
 *   /login?next=https://evil.example.com that would redirect victims
 *   to attacker-controlled origins after legitimate authentication.
 *
 * Conventions per AAP Sec 0.7.7 and frontend/.prettierrc.json:
 *   - TailwindCSS utility classes only (no inline styles).
 *   - Strict TypeScript; no `any` types.
 *   - Named export; no default export.
 *   - Lucide-React icons (Mail, Lock, LogIn).
 *   - Inline SVG for the Google "G" brand mark (Lucide does not ship
 *     brand-specific marks; adding react-icons would violate the
 *     dependency manifest per AAP Sec 0.3.4).
 *   - Path imports use the @/ alias for src.
 *   - Type-only imports use the `type` modifier (verbatimModuleSyntax).
 *   - Double quotes per project Prettier configuration
 *     (singleQuote: false). Trailing commas; line length <= 100.
 *   - No emoji - strict ASCII.
 *   - No direct fetch() calls - login uses useLoginMutation; OAuth uses
 *     window.location.href for full-page navigation.
 *
 * Coordinates with:
 *   - @/api/auth                useLoginMutation (POST /auth/login)
 *   - @/auth/AuthProvider       useSession, useSessionLoading
 *   - @/schemas/auth            LoginRequestSchema, LoginRequest type
 *   - @/components/ui/Button    Tailwind-styled <button> primitive
 *   - @/components/ui/Input     Tailwind-styled labeled input primitive
 *   - @/components/ui/Toast     useToast for fallback network-error toasts
 *   - @/router.tsx              Mounts this component at /login
 *   - @/auth/ProtectedRoute     Sets the ?next= query param when
 *                               redirecting unauthenticated users here
 *   - backend/app/api/auth.py   Server handler for POST /auth/login,
 *                               GET /auth/google/start, /auth/google/callback
 */

import { useCallback, useEffect, useState, type FormEvent, type JSX } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Lock, LogIn, Mail } from "lucide-react";

import { useLoginMutation } from "@/api/auth";
import { useSession, useSessionLoading } from "@/auth/AuthProvider";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { useToast } from "@/components/ui/Toast";
import { LoginRequestSchema, type LoginRequest } from "@/schemas/auth";

// ---------------------------------------------------------------------------
// Module-level constants
// ---------------------------------------------------------------------------

/**
 * Default post-login destination when the ?next= query param is absent,
 * malformed, or rejected by the open-redirect guard.
 *
 * The /feed route is the canonical landing page for authenticated users
 * per AAP Sec 0.5.4 (it is the primary surface for both Contributor and
 * Viewer roles). The Admin role lands on /feed too and navigates from
 * there to /admin via the app shell - there is no role-conditional
 * default landing page for the MVP.
 */
const DEFAULT_LOGIN_DESTINATION = "/feed";

// ---------------------------------------------------------------------------
// Helper: parseNextDestination
// ---------------------------------------------------------------------------

/**
 * Decode the `?next=` query param and validate it as a same-origin path.
 *
 * The `?next=` parameter is set by <ProtectedRoute> via
 * `encodeURIComponent(location.pathname + location.search)` whenever an
 * unauthenticated user attempts to visit a protected route. After
 * successful authentication, this LoginScreen navigates to the decoded
 * value so users land back where they tried to go.
 *
 * Open-redirect protection (defense against phishing):
 *   Without validation, an attacker could craft a phishing link of the
 *   form `https://app.example.com/login?next=https%3A%2F%2Fevil.com`.
 *   A victim clicks, authenticates legitimately, and is then redirected
 *   to the attacker-controlled origin (which can mimic the real app's
 *   UI to harvest session cookies, OAuth tokens, etc.). We defend
 *   layered:
 *     1. Decode the URI component, falling safely to /feed on error.
 *     2. Require a single leading "/" - rejects absolute URLs like
 *        "https://evil.com/path" which start with "h" or any other
 *        non-slash character.
 *     3. Reject "//" (protocol-relative URLs that browsers resolve to
 *        the current page's protocol but the attacker's host).
 *     4. Reject any "/auth/*" path - would loop the user back through
 *        the OAuth flow on login complete, possibly creating a
 *        confusing UX or an exploitable redirect chain.
 *     5. Reject any "/login*" path - prevents the screen from
 *        bouncing the user right back to the login form (perception
 *        loop) and blocks an attacker from chaining /login?next=/login?
 *        next=... payloads.
 *
 * This function is pure (no side effects, no React hooks) so it is
 * trivially unit-testable; the LoginScreen tests can import and call it
 * directly without rendering the component.
 *
 * @param rawNext - The raw ?next= query param value, or null if absent.
 * @returns A safe same-origin path (always starting with "/"); falls
 *          back to DEFAULT_LOGIN_DESTINATION when the input is missing,
 *          malformed, or fails any of the safety checks above.
 */
function parseNextDestination(rawNext: string | null): string {
  if (rawNext === null || rawNext.length === 0) {
    return DEFAULT_LOGIN_DESTINATION;
  }
  let decoded: string;
  try {
    decoded = decodeURIComponent(rawNext);
  } catch {
    // decodeURIComponent throws URIError on malformed inputs (e.g. a
    // lone "%" or "%XX" with non-hex digits). Defensive fallback: treat
    // the param as if it were absent.
    return DEFAULT_LOGIN_DESTINATION;
  }
  // Single leading slash: rejects absolute URLs like "https://evil.com",
  // empty strings, and bare path components like "feed".
  if (!decoded.startsWith("/")) {
    return DEFAULT_LOGIN_DESTINATION;
  }
  // Protocol-relative URLs ("//evil.com/path") would otherwise bypass
  // the leading-slash check. Browsers resolve "//host/p" to the current
  // protocol (https/http) plus the attacker's host - clearly off-origin.
  if (decoded.startsWith("//")) {
    return DEFAULT_LOGIN_DESTINATION;
  }
  // /auth/* paths would loop the user back through the authentication
  // surface; harmless on its own but creates a confusing UX (the user
  // just signed in - why are they on a Google OAuth page again?).
  if (decoded.startsWith("/auth/")) {
    return DEFAULT_LOGIN_DESTINATION;
  }
  // /login* paths would bounce the user right back to the login form
  // they just submitted. Catch the bare path, the query-string variant,
  // and any sub-path variant.
  if (
    decoded === "/login" ||
    decoded.startsWith("/login?") ||
    decoded.startsWith("/login/") ||
    decoded.startsWith("/login#")
  ) {
    return DEFAULT_LOGIN_DESTINATION;
  }
  return decoded;
}

// ---------------------------------------------------------------------------
// LoginScreen component
// ---------------------------------------------------------------------------

/**
 * The public login surface for the Sales-Connections SPA.
 *
 * Behavior:
 *   - On mount: if a session already exists, immediately redirect to
 *     ?next= (validated) or /feed via useEffect. The brief "Loading..."
 *     placeholder shown while the session-hydration query is in flight
 *     prevents flashing the login form to authenticated users.
 *   - The email input auto-focuses on mount so keyboard-first users
 *     can start typing immediately.
 *   - Google sign-in: full-page browser navigation to /auth/google/start.
 *     The SPA cannot intercept this (and should not) because Flask
 *     handles the OAuth state cookie and the 302 redirect to Google.
 *     React Router's <Link> would intercept the click and prevent the
 *     full-page nav, breaking the flow - hence window.location.href
 *     from a button click.
 *   - Email/password: validates the form via Zod (LoginRequestSchema)
 *     for instant UX feedback, then mutates via useLoginMutation. On
 *     success, navigates to the resolved next destination. On error,
 *     surfaces a form-level error message (and api/auth.ts fires its
 *     own toast independently per AAP Sec 0.5.3).
 *
 * The component renders three distinct render outputs depending on
 * session state:
 *   - sessionLoading=true       : a minimal loading placeholder with
 *                                 aria-busy so screen readers announce
 *                                 the wait.
 *   - session.authenticated=true: an empty <main> placeholder with
 *                                 aria-hidden=true; the useEffect above
 *                                 has already issued the navigate().
 *   - otherwise (unauthenticated): the full login form with Google
 *                                 button, divider, and email/password
 *                                 inputs.
 *
 * @returns The login screen JSX.
 */
export function LoginScreen(): JSX.Element {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const session = useSession();
  const sessionLoading = useSessionLoading();
  const loginMutation = useLoginMutation();
  const toast = useToast();

  // -------------------------------------------------------------------------
  // Email input auto-focus via callback ref
  //
  // Why a callback ref instead of `useRef` + `useEffect(() => ..., [])`:
  //
  // The form is CONDITIONALLY rendered behind `if (sessionLoading) return
  // <Loading>` and `if (session?.authenticated) return <Hidden>` guards.
  // On a cold page load with no cached session, the FIRST render commits
  // the loading placeholder (the form is not in the DOM, useRef.current
  // is null, focus() is a no-op). Once /api/me settles to 401, sessionLoading
  // flips false and the form mounts on a subsequent render - but a
  // useEffect with `[]` deps does NOT re-fire on conditional re-renders
  // of the same component instance. The auto-focus would silently fail.
  //
  // A callback ref runs whenever the underlying element attaches /
  // detaches from the DOM, so it fires the moment the input mounts -
  // regardless of which conditional branch rendered it. Wrapping in
  // useCallback with empty deps keeps the function reference stable so
  // React does not consider the ref "changed" on every parent render.
  // -------------------------------------------------------------------------
  const emailInputRef = useCallback((node: HTMLInputElement | null): void => {
    if (node !== null) {
      // The element just mounted (or was replaced). Focus it for
      // keyboard-first UX. Browsers silently ignore focus() calls when
      // the document has no focus (e.g., the tab is in the background)
      // - that is fine here; the user will focus it themselves on
      // return to the tab.
      node.focus();
    }
  }, []);

  // -------------------------------------------------------------------------
  // Form state
  //
  // Plain useState (NOT React 19's useActionState) because the form has
  // three distinct error surfaces (per-field email error, per-field
  // password error, form-level error) and mapping a single useActionState
  // result onto three error slots is more complex and less readable than
  // the explicit-state approach. This decision is recorded in
  // docs/decision-log.md per the user's Explainability rule.
  // -------------------------------------------------------------------------
  const [email, setEmail] = useState<string>("");
  const [password, setPassword] = useState<string>("");
  const [emailError, setEmailError] = useState<string | undefined>(undefined);
  const [passwordError, setPasswordError] = useState<string | undefined>(undefined);
  const [formError, setFormError] = useState<string | undefined>(undefined);

  // -------------------------------------------------------------------------
  // Resolve the post-login destination from ?next= query param.
  //
  // Computed every render (cheap; pure function) so re-mounts after
  // navigation see fresh values. Memoization would be premature: the
  // search params are already React-stable across renders, and
  // parseNextDestination is a pure string operation.
  // -------------------------------------------------------------------------
  const rawNext = searchParams.get("next");
  const nextDestination = parseNextDestination(rawNext);

  // -------------------------------------------------------------------------
  // Already-authenticated guard
  //
  // If the user already has a valid session and visits /login directly
  // (or via a stale bookmark), redirect them to the intended destination.
  // We use useEffect (deferred) rather than a synchronous redirect at
  // render time because:
  //   1. The session query is async; on cold load the session is null
  //      until /api/me resolves. A synchronous redirect would never
  //      fire because the first render always sees null.
  //   2. useEffect runs AFTER paint, so the brief "Loading..." paint
  //      below covers the gap.
  //
  // navigate(..., { replace: true }) overwrites the /login history
  // entry so hitting Back from /feed does not return to /login.
  // -------------------------------------------------------------------------
  useEffect(() => {
    if (sessionLoading) {
      return;
    }
    if (session?.authenticated === true) {
      navigate(nextDestination, { replace: true });
    }
  }, [session, sessionLoading, nextDestination, navigate]);

  // -------------------------------------------------------------------------
  // Google OAuth handler
  //
  // The full-page navigation is intentional and architectural per AAP
  // Sec 0.4.5: Flask handles /auth/google/start by setting the OAuth
  // state cookie and 302-redirecting to Google's authorization endpoint.
  // The SPA cannot - and must not - intercept this because:
  //   1. Authlib (the backend OAuth client) needs to set the state
  //      cookie BEFORE the redirect to Google so the callback can
  //      validate the state. An XHR/fetch would not let Flask write
  //      cookies that the next browser request would honor.
  //   2. The Google authorization page itself is a top-level browser
  //      view; it cannot live inside an iframe (X-Frame-Options:
  //      DENY on Google's side).
  //
  // The OAuth flow does NOT carry the ?next= param - per AAP Sec 0.4.5
  // the Flask callback hard-codes /feed as the post-OAuth destination.
  // Users wanting to land on a specific page must use email/password.
  //
  // useCallback memoizes the handler so React does not consider the
  // Button's onClick prop changed on every render (preventing
  // unnecessary re-renders of the Button subtree).
  // -------------------------------------------------------------------------
  const handleGoogleSignIn = useCallback((): void => {
    window.location.href = "/auth/google/start";
  }, []);

  // -------------------------------------------------------------------------
  // Email/password submit handler
  //
  // Three phases:
  //   1. Reset prior errors (so the form starts each submit in a clean
  //      state and the user does not see stale messages).
  //   2. Validate via Zod safeParse. On failure, map the field-level
  //      errors onto the per-field error state slots and abort.
  //   3. Mutate via loginMutation. On success, navigate to ?next=. On
  //      error, branch on the HTTP status code:
  //        - 401: invalid creds. The mutation has already fired a
  //          generic toast.error per AAP Sec 0.7.4 (defense in depth -
  //          do not leak email-existence). We additionally render an
  //          inline form-level error message for visual emphasis.
  //        - 0 or 5xx: network/server failures. Render a friendly
  //          inline error AND fire a toast.error (the mutation's toast
  //          covers some of this but is generic; ours adds a
  //          connectivity-specific message).
  //        - 422: pydantic rejection (rare; client validation should
  //          catch most). Map per-field errors back onto field state
  //          slots; fall through to a form-level error if no field
  //          context is available.
  //        - other: unexpected; render the server's message.
  //
  // The mutation's onSuccess inside @/api/auth.ts handles:
  //   - resetCorrelationId() (per AAP Sec 0.7.5)
  //   - queryClient.invalidateQueries(['auth', 'session'])
  //   - toast.success("Signed in")
  // The LoginScreen does NOT duplicate any of those.
  // -------------------------------------------------------------------------
  const handleSubmit = useCallback(
    (event: FormEvent<HTMLFormElement>): void => {
      event.preventDefault();

      // Phase 1: Reset prior errors.
      setEmailError(undefined);
      setPasswordError(undefined);
      setFormError(undefined);

      // Phase 2: Client-side Zod validation (UX optimization; backend
      // re-validates per AAP Sec 0.7.1 invariant 8).
      const candidate: { email: string; password: string } = { email, password };
      const result = LoginRequestSchema.safeParse(candidate);
      if (!result.success) {
        const fieldErrors = result.error.flatten().fieldErrors;
        const emailErr = fieldErrors.email?.[0];
        const passwordErr = fieldErrors.password?.[0];
        if (emailErr !== undefined) {
          setEmailError(emailErr);
        }
        if (passwordErr !== undefined) {
          setPasswordError(passwordErr);
        }
        return;
      }

      // Phase 3: Submit via mutation; navigate / surface errors based
      // on response. Note: result.data is the strict-validated, typed
      // LoginRequest (no extra fields).
      const validated: LoginRequest = result.data;
      loginMutation.mutate(validated, {
        onSuccess: () => {
          // Mutation has already invalidated the session query and
          // fired toast.success("Signed in"). Navigate to ?next= or
          // /feed; replace: true so Back from /feed does not return
          // to /login (which would then immediately redirect again
          // via the already-authenticated guard - perception loop).
          navigate(nextDestination, { replace: true });
        },
        onError: (error) => {
          if (error.status === 401) {
            // Generic credential-failure message per defense-in-depth
            // (do not leak whether the email is registered).
            setFormError("Invalid email or password. Please try again.");
            return;
          }
          if (error.status === 0 || error.status >= 500) {
            // Network failure (status=0) or server outage (5xx). The
            // mutation's onError fires a toast.error with the server's
            // message; we override with a connectivity-specific
            // inline message and fire an additional toast for users
            // who may have missed the first one.
            setFormError("We could not reach the server. Please try again in a moment.");
            toast.error("Network error. Please check your connection.");
            return;
          }
          if (error.status === 422) {
            // Pydantic rejection. The error.fields array typically
            // carries per-field details; map them onto our local
            // state. Fall through to a form-level message if no
            // field-specific info is present (defensive).
            const emailFieldError = error.fieldError("email");
            const passwordFieldError = error.fieldError("password");
            if (emailFieldError !== undefined) {
              setEmailError(emailFieldError.message ?? "Invalid email address.");
            }
            if (passwordFieldError !== undefined) {
              setPasswordError(passwordFieldError.message ?? "Invalid password.");
            }
            if (emailFieldError === undefined && passwordFieldError === undefined) {
              // No field context; surface the server's top-level
              // message or a generic fallback.
              setFormError(error.message.length > 0 ? error.message : "Validation failed.");
            }
            return;
          }
          // Unexpected status (403, 404, etc. - all unlikely on /auth/login
          // but handled defensively). Show the server's message or a
          // safe fallback.
          setFormError(
            error.message.length > 0
              ? error.message
              : "An unexpected error occurred. Please try again.",
          );
        },
      });
    },
    [email, password, loginMutation, navigate, nextDestination, toast],
  );

  // -------------------------------------------------------------------------
  // Render: loading placeholder
  //
  // While the session-hydration query is in flight, render an empty
  // placeholder. This prevents an authenticated user (cold-load with
  // valid session cookie) from briefly seeing the login form before
  // the useEffect above triggers the redirect.
  //
  // aria-busy="true" announces the wait state to screen readers; the
  // visible "Loading..." text is the accessible name for the region.
  // -------------------------------------------------------------------------
  if (sessionLoading) {
    return (
      // Per Visual Consistency QA Issue 7 use <section> instead of
      // a nested <main>; the document's primary main is in App.tsx.
      <section
        className="min-h-screen flex items-center justify-center bg-slate-50 px-4 py-8"
        aria-busy="true"
        data-testid="login-screen-loading"
      >
        <div className="text-sm text-slate-500">Loading...</div>
      </section>
    );
  }

  // -------------------------------------------------------------------------
  // Render: post-redirect placeholder
  //
  // If we have settled session state and the user is authenticated, the
  // useEffect above has issued navigate(); render an empty placeholder
  // for the one-render-cycle gap before the route changes. aria-hidden
  // keeps it out of the accessibility tree (no visible content for
  // screen readers to announce).
  // -------------------------------------------------------------------------
  if (session?.authenticated === true) {
    return <section className="min-h-screen" aria-hidden="true" />;
  }

  // -------------------------------------------------------------------------
  // Render: the login form
  //
  // Layout:
  //   - <main> centered card on a slate-50 background.
  //   - Header with brand title and a one-line subtitle.
  //   - Google sign-in button (full-width).
  //   - "Or" divider.
  //   - Email/password form with inline error rendering.
  //   - Footer line with usage policy reminder.
  //
  // The card's max-w-md (28rem ~ 448px) keeps the form line length
  // readable on wide displays while shrinking to the viewport width on
  // mobile (px-4 padding on the <main> ensures gutter on narrow screens).
  // -------------------------------------------------------------------------
  const isSubmitting = loginMutation.isPending;

  return (
    // Per Visual Consistency QA Issue 7 the login route renders
    // <section aria-labelledby> rather than nesting a second <main>
    // landmark inside the document's primary <main> in App.tsx.
    <section
      className="min-h-screen flex items-center justify-center bg-slate-50 px-4 py-8"
      aria-labelledby="login-heading"
      data-testid="login-screen"
    >
      <div className="w-full max-w-md bg-white border border-slate-200 rounded-lg shadow-card p-6 sm:p-8">
        <div className="text-center mb-6">
          <h1 id="login-heading" className="text-2xl font-semibold text-slate-900">
            Sign in to Sales-Connections
          </h1>
          <p className="mt-2 text-sm text-slate-600">Turn your network into actionable leads.</p>
        </div>

        {/*
          Google OAuth button. Full-page navigation, NOT a SPA route
          transition. See handleGoogleSignIn comment for why.
        */}
        <Button
          type="button"
          variant="secondary"
          size="md"
          fullWidth
          onClick={handleGoogleSignIn}
          leftIcon={<GoogleIcon />}
          data-testid="login-google-button"
        >
          Sign in with Google
        </Button>

        {/* "Or" divider between OAuth and email/password options. */}
        <div
          className="my-6 flex items-center gap-3"
          role="separator"
          aria-orientation="horizontal"
        >
          <span className="h-px flex-1 bg-slate-200" />
          <span className="text-xs font-medium uppercase tracking-wide text-slate-500">Or</span>
          <span className="h-px flex-1 bg-slate-200" />
        </div>

        {/*
          Email / password form. noValidate prevents the browser's
          built-in HTML5 validation tooltip from fighting our Zod-driven
          messages (which are richer and more consistent across browsers).
          aria-describedby points at the form-level error paragraph
          (when present) so screen readers announce the failure context.
        */}
        <form
          onSubmit={handleSubmit}
          noValidate
          aria-describedby={formError !== undefined ? "login-form-error" : undefined}
          data-testid="login-form"
        >
          <div className="flex flex-col gap-4">
            <Input
              ref={emailInputRef}
              label="Email address"
              type="email"
              name="email"
              autoComplete="email"
              required
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              errorMessage={emailError}
              leftIcon={<Mail aria-hidden="true" />}
              placeholder="you@company.com"
              disabled={isSubmitting}
              data-testid="login-email"
            />
            <Input
              label="Password"
              type="password"
              name="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              errorMessage={passwordError}
              leftIcon={<Lock aria-hidden="true" />}
              placeholder="Enter your password"
              disabled={isSubmitting}
              data-testid="login-password"
            />

            {formError !== undefined && (
              <p
                id="login-form-error"
                role="alert"
                className="text-sm text-red-600"
                data-testid="login-form-error"
              >
                {formError}
              </p>
            )}

            <Button
              type="submit"
              variant="primary"
              size="md"
              fullWidth
              loading={isSubmitting}
              leftIcon={<LogIn aria-hidden="true" />}
              data-testid="login-submit"
            >
              Sign in
            </Button>
          </div>
        </form>

        <p className="mt-6 text-center text-xs text-slate-500">
          By signing in you agree to our internal usage policy.
        </p>
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// GoogleIcon - inline SVG component for the Google "G" brand mark
// ---------------------------------------------------------------------------

/**
 * Inline Google "G" brand mark as an SVG component.
 *
 * Why inline rather than Lucide-React: Lucide does not ship the Google
 * brand mark (Google's brand guidelines restrict third-party
 * redistribution; brand-specific marks live in libraries like
 * @lobehub/icons or react-icons, neither of which is in the project's
 * dependency manifest per AAP Sec 0.3.4). The inline SVG is < 1 KB and
 * sized via the className "h-4 w-4" so it matches the visual rhythm of
 * the Lucide icons used elsewhere on the screen.
 *
 * The SVG uses Google's published brand colors (#4285F4 blue, #34A853
 * green, #FBBC05 yellow, #EA4335 red) per the Google Identity
 * guidelines; aria-hidden="true" because the icon is decorative (the
 * accompanying button label "Sign in with Google" is the accessible
 * name for the action).
 *
 * Module-private (not exported) because the icon is only used by the
 * LoginScreen and has no broader application elsewhere in the SPA.
 *
 * @returns The SVG element representing Google's "G" mark.
 */
function GoogleIcon(): JSX.Element {
  return (
    <svg
      aria-hidden="true"
      width="16"
      height="16"
      viewBox="0 0 18 18"
      xmlns="http://www.w3.org/2000/svg"
      className="h-4 w-4"
    >
      <path
        fill="#4285F4"
        d="M17.64 9.2c0-.637-.057-1.251-.164-1.84H9v3.481h4.844c-.209 1.125-.843 2.078-1.796 2.717v2.258h2.908c1.702-1.567 2.684-3.874 2.684-6.615z"
      />
      <path
        fill="#34A853"
        d="M9 18c2.43 0 4.467-.806 5.956-2.18l-2.908-2.259c-.806.54-1.837.86-3.048.86-2.344 0-4.328-1.584-5.036-3.711H.957v2.332C2.438 15.983 5.482 18 9 18z"
      />
      <path
        fill="#FBBC05"
        d="M3.964 10.71A5.41 5.41 0 0 1 3.682 9c0-.593.102-1.17.282-1.71V4.958H.957A8.996 8.996 0 0 0 0 9c0 1.452.348 2.827.957 4.042l3.007-2.332z"
      />
      <path
        fill="#EA4335"
        d="M9 3.58c1.321 0 2.508.454 3.44 1.345l2.582-2.58C13.463.891 11.426 0 9 0 5.482 0 2.438 2.017.957 4.958L3.964 7.29C4.672 5.163 6.656 3.58 9 3.58z"
      />
    </svg>
  );
}
