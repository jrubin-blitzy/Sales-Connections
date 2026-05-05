/**
 * LoginScreen.test.tsx - Vitest tests for the F-012 login screen.
 *
 * Targets `frontend/src/features/auth/LoginScreen.tsx`, which:
 *   - Mounts at /login (the only public route)
 *   - Provides Google OAuth (full-page nav) and email/password (mutation) paths
 *   - Auto-redirects already-authenticated users via useEffect
 *   - Reads ?next= and uses parseNextDestination() for open-redirect protection
 *   - Auto-focuses the email input on mount
 *   - Validates form input via Zod (LoginRequestSchema)
 *
 * Test scope (per the assigned folder requirements):
 *   1. Renders core elements (form, inputs, buttons, no loading placeholder).
 *   2. Loading state renders the loading placeholder.
 *   3. Email auto-focus on mount.
 *   4. Google button click sets window.location.href.
 *   5. Email/password submit happy path -> navigate to /feed (or ?next=).
 *   6. 401 response -> form-level error.
 *   7. 422 response with field errors -> per-field errors.
 *   8. 5xx/network response -> form-level error + toast.error.
 *   9. parseNextDestination validation against open-redirect attempts.
 *  10. Already-authenticated guard redirects on mount.
 *  11. isPending state shows loading spinner and disables inputs.
 *  12. Form has noValidate attribute.
 *  13. Empty form submit shows Zod field errors.
 *  14. Invalid email format shows Zod error.
 *
 * Mocking strategy:
 *   - Module-scope vi.mock("@/auth/AuthProvider", ...) following the
 *     established pattern in tests/auth/RoleGate.test.tsx and
 *     tests/auth/ProtectedRoute.test.tsx. This gives synchronous control
 *     over useSession()/useSessionLoading() return values via
 *     vi.mocked(...).mockReturnValue(...) per test.
 *   - The real `useLoginMutation` is used so MSW intercepts POST /auth/login.
 *   - `<ToastContainer />` is mounted alongside the LoginScreen for tests
 *     that assert toast behavior (the network-error case).
 *   - `window.location.href` is replaced via Object.defineProperty with a
 *     writable mock for the Google OAuth navigation test, then restored
 *     in afterEach.
 *
 * Conventions per AAP Sec 0.7.7 and frontend/.prettierrc.json:
 *   - Strict TypeScript; no `any`.
 *   - Double quotes (singleQuote: false); trailing commas; 2-space indent;
 *     line length <= 100.
 *   - Named imports only; no default export.
 *   - No emoji; no console.log.
 *   - data-testid selectors preferred over fragile text matches.
 *   - userEvent.setup() pattern (NOT fireEvent) for all interactions.
 */

import { type JSX, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { waitFor } from "@testing-library/react";
import { delay, http, HttpResponse } from "msw";
import { Route, Routes, useLocation } from "react-router-dom";

import { LoginScreen } from "@/features/auth/LoginScreen";
import { useSession, useSessionLoading } from "@/auth/AuthProvider";
import { ToastContainer } from "@/components/ui/Toast";
import type { SessionRead } from "@/schemas/auth";

import { server } from "../../mocks/server";
import { overrides } from "../../mocks/handlers";
import { makeSessionRead, makeUserRead } from "../../mocks/data";
import { renderWithProviders, screen, userEvent } from "../../test-utils";

// ---------------------------------------------------------------------------
// Module-scope vi.mock for @/auth/AuthProvider
//
// CRITICAL: vi.mock calls at module scope are HOISTED by Vitest above any
// imports, so the mock is in place when LoginScreen.tsx resolves its
// `import { useSession, useSessionLoading } from "@/auth/AuthProvider"` at
// module load time. Without hoisting, the real hooks would attempt to read
// the AuthContext (sentinel undefined) and throw "must be used inside
// <AuthProvider>" inside the test render.
//
// We provide stubs for ALL exports of @/auth/AuthProvider (not just the two
// hooks LoginScreen consumes) so any transitive import of the module from
// LoginScreen.tsx or its imports resolves cleanly. The AuthProvider stub
// is a passthrough so renderWithProviders' real-AuthProvider wrapper still
// renders children unchanged.
//
// Tests configure useSession() and useSessionLoading() per case via
// vi.mocked(...).mockReturnValue(...). The default in beforeEach is
// the unauthenticated/ready state (session=null, isLoading=false) so the
// LoginScreen renders its full form by default.
// ---------------------------------------------------------------------------

vi.mock("@/auth/AuthProvider", () => ({
  AuthProvider: ({ children }: { children: ReactNode }) => children,
  useSession: vi.fn<() => SessionRead | null>(),
  useSessionLoading: vi.fn<() => boolean>(),
  useRole: vi.fn(() => ({ role: null, has: () => false })),
  useLogout: vi.fn(),
}));

// ---------------------------------------------------------------------------
// Helper: install a writable mock for window.location
//
// jsdom's default window.location parses href assignments as URL navigations
// and does not allow direct overwrite of href in a way that lets tests
// inspect what was assigned. The standard pattern is to replace
// window.location entirely with an object that traps href via a setter.
// Restore the original in afterEach to avoid leaking the mock across tests.
// ---------------------------------------------------------------------------

interface LocationMock {
  /** Spy that records every assignment to window.location.href. */
  hrefSetter: ReturnType<typeof vi.fn>;
  /** Restore the original window.location. Idempotent. */
  restore: () => void;
}

/**
 * Replace `window.location` with a mock that proxies most fields to the
 * original but traps assignments to `href`. Returns the spy plus a
 * restore function for afterEach cleanup.
 */
function installLocationMock(): LocationMock {
  const original = window.location;
  const hrefSetter = vi.fn<(value: string) => void>();
  // Build a Location-shaped object that proxies most fields to the
  // original but intercepts href via a setter. The cast is necessary
  // because we substitute getters/setters for several properties and not
  // every Location member is reasonably mockable in jsdom.
  const mock: Location = {
    ancestorOrigins: original.ancestorOrigins,
    hash: original.hash,
    host: original.host,
    hostname: original.hostname,
    origin: original.origin,
    pathname: original.pathname,
    port: original.port,
    protocol: original.protocol,
    search: original.search,
    assign: vi.fn(),
    reload: vi.fn(),
    replace: vi.fn(),
    toString: () => original.toString(),
    get href(): string {
      return original.href;
    },
    set href(value: string) {
      hrefSetter(value);
    },
  } as unknown as Location;

  Object.defineProperty(window, "location", {
    configurable: true,
    enumerable: true,
    writable: true,
    value: mock,
  });

  const restore = (): void => {
    Object.defineProperty(window, "location", {
      configurable: true,
      enumerable: true,
      writable: true,
      value: original,
    });
  };

  return { hrefSetter, restore };
}

// ---------------------------------------------------------------------------
// Helper: LocationCapture sibling-route component
//
// To capture react-router's navigate() destination without mocking
// useNavigate (which is fragile and couples the test to react-router
// internals), define a sibling route that renders the current location as
// a DOM-rendered text node. Tests assert via
// screen.getByTestId("location-display").toHaveTextContent("/feed").
// ---------------------------------------------------------------------------

function LocationCapture(): JSX.Element {
  const location = useLocation();
  return <div data-testid="location-display">{location.pathname + location.search}</div>;
}

// ---------------------------------------------------------------------------
// Helper: configure session hooks
//
// Wraps the boilerplate of vi.mocked(useSession).mockReturnValue(...)
// + vi.mocked(useSessionLoading).mockReturnValue(...) so each test can
// declare the session state in a single line.
// ---------------------------------------------------------------------------

function setSessionState(session: SessionRead | null, isLoading = false): void {
  vi.mocked(useSession).mockReturnValue(session);
  vi.mocked(useSessionLoading).mockReturnValue(isLoading);
}

// ---------------------------------------------------------------------------
// Top-level describe and shared lifecycle
// ---------------------------------------------------------------------------

describe("<LoginScreen />", () => {
  let locationMock: LocationMock | null = null;

  beforeEach(() => {
    // Default: unauthenticated, ready state. Tests that need a different
    // session state override these via setSessionState() at the start of
    // the test body.
    setSessionState(null, false);
    locationMock = null;
  });

  afterEach(() => {
    locationMock?.restore();
    locationMock = null;
    // Reset call history so per-test mock interactions do not leak.
    vi.mocked(useSession).mockReset();
    vi.mocked(useSessionLoading).mockReset();
  });

  // -------------------------------------------------------------------------
  // Rendering: core elements present in the unauthenticated/ready state.
  // -------------------------------------------------------------------------
  describe("rendering", () => {
    it("renders the login screen with form, inputs, and Google button", async () => {
      renderWithProviders(<LoginScreen />);

      // The unauthenticated <main> is present.
      expect(await screen.findByTestId("login-screen")).toBeInTheDocument();

      // The loading placeholder is NOT rendered.
      expect(screen.queryByTestId("login-screen-loading")).not.toBeInTheDocument();

      // Core form elements are present.
      expect(screen.getByTestId("login-form")).toBeInTheDocument();
      expect(screen.getByTestId("login-email")).toBeInTheDocument();
      expect(screen.getByTestId("login-password")).toBeInTheDocument();
      expect(screen.getByTestId("login-submit")).toBeInTheDocument();
      expect(screen.getByTestId("login-google-button")).toBeInTheDocument();

      // The submit button has type="submit" so the form's onSubmit
      // handler fires on Enter and on click.
      expect(screen.getByTestId("login-submit")).toHaveAttribute("type", "submit");

      // Email input attributes match the LoginScreen contract.
      const emailInput = screen.getByTestId("login-email");
      expect(emailInput).toHaveAttribute("type", "email");
      expect(emailInput).toHaveAttribute("autocomplete", "email");

      // Password input attributes match the LoginScreen contract.
      const passwordInput = screen.getByTestId("login-password");
      expect(passwordInput).toHaveAttribute("type", "password");
      expect(passwordInput).toHaveAttribute("autocomplete", "current-password");
    });

    it("the form element has noValidate set so the browser does not show its own tooltip", async () => {
      renderWithProviders(<LoginScreen />);
      const form = await screen.findByTestId("login-form");
      // noValidate is the JS property; the HTML attribute is "novalidate"
      // (lowercase). We assert via the JS property to avoid case-
      // sensitivity ambiguity in toHaveAttribute().
      expect((form as HTMLFormElement).noValidate).toBe(true);
    });

    it("renders the loading placeholder when session hydration is in flight", () => {
      // Override the hooks to simulate the in-flight session probe.
      setSessionState(null, true);
      renderWithProviders(<LoginScreen />);

      // The loading placeholder is rendered with aria-busy.
      const loading = screen.getByTestId("login-screen-loading");
      expect(loading).toBeInTheDocument();
      expect(loading).toHaveAttribute("aria-busy", "true");

      // The full login surface is NOT rendered while loading.
      expect(screen.queryByTestId("login-screen")).not.toBeInTheDocument();
      expect(screen.queryByTestId("login-form")).not.toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // Focus management: the email input auto-focuses on mount via callback ref.
  // -------------------------------------------------------------------------
  describe("focus management", () => {
    it("auto-focuses the email input on mount", async () => {
      renderWithProviders(<LoginScreen />);

      const emailInput = await screen.findByTestId("login-email");
      // The callback ref runs synchronously when the input mounts; wrap
      // in waitFor for defensive timing safety against future React
      // batching changes.
      await waitFor(() => {
        expect(emailInput).toHaveFocus();
      });
    });
  });

  // -------------------------------------------------------------------------
  // Google sign-in: the click handler sets window.location.href to
  // /auth/google/start (full-page navigation, NOT a SPA route transition).
  // -------------------------------------------------------------------------
  describe("Google sign-in", () => {
    it("navigates to /auth/google/start on Google button click", async () => {
      const user = userEvent.setup();
      locationMock = installLocationMock();
      renderWithProviders(<LoginScreen />);

      const googleButton = await screen.findByTestId("login-google-button");
      await user.click(googleButton);

      // The click handler should have set window.location.href exactly
      // once with the canonical OAuth start URL.
      expect(locationMock.hrefSetter).toHaveBeenCalledTimes(1);
      expect(locationMock.hrefSetter).toHaveBeenCalledWith("/auth/google/start");
    });

    it("the Google button is type='button' (not 'submit')", async () => {
      // Defense against a regression where the Google button accidentally
      // becomes a submit button and triggers form validation on click.
      renderWithProviders(<LoginScreen />);
      const googleButton = await screen.findByTestId("login-google-button");
      expect(googleButton).toHaveAttribute("type", "button");
    });
  });

  // -------------------------------------------------------------------------
  // Email/password submit: the happy path and three documented error
  // branches (401, 422, 5xx). Per the AAP, the LoginScreen distinguishes
  // form-level errors (login-form-error <p>) from field-level errors (the
  // Input primitive's input-error <p> rendered when errorMessage is set).
  // -------------------------------------------------------------------------
  describe("email/password submit", () => {
    it("submits valid credentials and navigates to /feed on success", async () => {
      const user = userEvent.setup();
      // Default POST /auth/login handler returns 200; no override needed.
      // Mount LoginScreen + a /feed sibling route so we can capture the
      // post-login navigation without mocking useNavigate directly.
      renderWithProviders(
        <Routes>
          <Route path="/login" element={<LoginScreen />} />
          <Route path="/feed" element={<LocationCapture />} />
        </Routes>,
        { initialEntries: ["/login"] },
      );

      await screen.findByTestId("login-screen");
      await user.type(screen.getByTestId("login-email"), "jane@example.com");
      await user.type(screen.getByTestId("login-password"), "correct-password");
      await user.click(screen.getByTestId("login-submit"));

      // Navigation should land on /feed (default destination when ?next=
      // is absent).
      await waitFor(() => {
        expect(screen.getByTestId("location-display")).toHaveTextContent("/feed");
      });

      // No form-level error should be visible after a successful submit.
      expect(screen.queryByTestId("login-form-error")).not.toBeInTheDocument();
    });

    it("shows form-level error on 401 invalid credentials", async () => {
      const user = userEvent.setup();
      server.use(overrides.auth.loginInvalidCredentials());
      renderWithProviders(<LoginScreen />);

      await screen.findByTestId("login-screen");
      await user.type(screen.getByTestId("login-email"), "jane@example.com");
      await user.type(screen.getByTestId("login-password"), "wrong-password");
      await user.click(screen.getByTestId("login-submit"));

      // Form-level error appears with the documented LoginScreen message.
      const formError = await screen.findByTestId("login-form-error");
      expect(formError).toBeInTheDocument();
      expect(formError).toHaveAttribute("role", "alert");
      expect(formError).toHaveTextContent(/invalid email or password/i);
    });

    it("maps 422 field errors to per-field errors via the Input primitive", async () => {
      const user = userEvent.setup();
      server.use(
        overrides.auth.loginValidationError([
          { field: "email", message: "Email format invalid (server)" },
          { field: "password", message: "Password too weak (server)" },
        ]),
      );
      renderWithProviders(<LoginScreen />);

      await screen.findByTestId("login-screen");
      // Provide values that pass client-side Zod so the mutation actually
      // fires and the server's 422 response can drive the assertion.
      await user.type(screen.getByTestId("login-email"), "jane@example.com");
      await user.type(screen.getByTestId("login-password"), "somepassword");
      await user.click(screen.getByTestId("login-submit"));

      // Field-level errors render via the Input primitive's errorMessage
      // prop, which produces <p data-testid="input-error" role="alert">
      // containing the message string.
      await waitFor(() => {
        expect(screen.getByText(/email format invalid \(server\)/i)).toBeInTheDocument();
      });
      expect(screen.getByText(/password too weak \(server\)/i)).toBeInTheDocument();

      // When both fields have server-supplied errors, the form-level
      // error fallback should NOT render (LoginScreen only sets formError
      // when neither field has context).
      expect(screen.queryByTestId("login-form-error")).not.toBeInTheDocument();
    });

    it("shows network-error form message and fires toast.error on 500", async () => {
      const user = userEvent.setup();
      server.use(overrides.auth.login500());
      // Mount ToastContainer alongside LoginScreen so toasts dispatched
      // via useToast() actually render to the DOM (the portal targets
      // document.body, which jsdom provides). Without ToastContainer, the
      // toast.error call would dispatch to the module-level store but
      // never produce DOM output to assert on.
      renderWithProviders(
        <>
          <LoginScreen />
          <ToastContainer />
        </>,
      );

      await screen.findByTestId("login-screen");
      await user.type(screen.getByTestId("login-email"), "jane@example.com");
      await user.type(screen.getByTestId("login-password"), "somepassword");
      await user.click(screen.getByTestId("login-submit"));

      // Form-level connectivity error appears.
      const formError = await screen.findByTestId("login-form-error");
      expect(formError).toHaveTextContent(/could not reach the server/i);

      // The LoginScreen-specific toast.error fires. Look up the toast by
      // its message text to disambiguate from the mutation's own
      // toast.error (the mutation falls through to "internal_error..." or
      // similar); the LoginScreen's message is unique.
      await waitFor(() => {
        expect(
          screen.getByText(/network error\. please check your connection/i),
        ).toBeInTheDocument();
      });
    });
  });

  // -------------------------------------------------------------------------
  // parseNextDestination open-redirect protection: each documented case is
  // exercised via successful-login + LocationCapture sibling-route capture.
  //
  // The cases mirror the LoginScreen's parseNextDestination branches:
  //   - absent next param            -> /feed
  //   - valid path                   -> decoded value
  //   - protocol-relative URL        -> /feed (rejected)
  //   - /auth/* path                 -> /feed (rejected)
  //   - /login self-loop             -> /feed (rejected)
  //   - /login? self-loop            -> /feed (rejected)
  //   - /login/ self-loop            -> /feed (rejected)
  //   - malformed URI encoding       -> /feed (decode throws)
  //   - no leading slash             -> /feed (rejected)
  // -------------------------------------------------------------------------
  describe("parseNextDestination open-redirect protection", () => {
    interface NextCase {
      readonly name: string;
      readonly rawNext: string;
      readonly expected: string;
    }

    const cases: ReadonlyArray<NextCase> = [
      { name: "absent next param", rawNext: "", expected: "/feed" },
      { name: "valid /feed path", rawNext: "%2Ffeed", expected: "/feed" },
      {
        name: "protocol-relative URL blocked",
        rawNext: "%2F%2Fevil.example.com",
        expected: "/feed",
      },
      {
        name: "auth path loop blocked",
        rawNext: "%2Fauth%2Fgoogle%2Fstart",
        expected: "/feed",
      },
      { name: "/login self-loop blocked", rawNext: "%2Flogin", expected: "/feed" },
      {
        name: "/login? self-loop blocked",
        rawNext: "%2Flogin%3Fnext%3D%252F",
        expected: "/feed",
      },
      {
        name: "/login/ self-loop blocked",
        rawNext: "%2Flogin%2Fextra",
        expected: "/feed",
      },
      // A bare "%" character is unconditionally invalid for
      // decodeURIComponent and triggers the URIError catch branch.
      { name: "malformed URI encoding", rawNext: "%", expected: "/feed" },
      { name: "no leading slash", rawNext: "feed", expected: "/feed" },
      {
        name: "valid /admin/users path",
        rawNext: "%2Fadmin%2Fusers",
        expected: "/admin/users",
      },
    ];

    it.each(cases)(
      "redirects to $expected when next=$rawNext after successful login ($name)",
      async ({ rawNext, expected }) => {
        const user = userEvent.setup();
        const initialPath = rawNext === "" ? "/login" : `/login?next=${rawNext}`;

        renderWithProviders(
          <Routes>
            <Route path="/login" element={<LoginScreen />} />
            <Route path="/feed" element={<LocationCapture />} />
            <Route path="/admin/users" element={<LocationCapture />} />
          </Routes>,
          { initialEntries: [initialPath] },
        );

        await screen.findByTestId("login-screen");
        await user.type(screen.getByTestId("login-email"), "jane@example.com");
        await user.type(screen.getByTestId("login-password"), "somepassword");
        await user.click(screen.getByTestId("login-submit"));

        await waitFor(() => {
          expect(screen.getByTestId("location-display")).toHaveTextContent(expected);
        });
      },
    );
  });

  // -------------------------------------------------------------------------
  // Already-authenticated guard: when session?.authenticated is true after
  // loading, useEffect calls navigate(nextDestination, { replace: true })
  // immediately on mount.
  // -------------------------------------------------------------------------
  describe("already-authenticated guard", () => {
    it("redirects to /feed when session is authenticated and no next param", async () => {
      // Configure an authenticated session synchronously via the mocked
      // hooks - no MSW round-trip needed.
      setSessionState(makeSessionRead({ user: { email: "jane@example.com" } }), false);

      renderWithProviders(
        <Routes>
          <Route path="/login" element={<LoginScreen />} />
          <Route path="/feed" element={<LocationCapture />} />
        </Routes>,
        { initialEntries: ["/login"] },
      );

      // The useEffect-driven redirect happens after the first paint;
      // wait for the destination route to mount.
      await waitFor(() => {
        expect(screen.getByTestId("location-display")).toHaveTextContent("/feed");
      });

      // The login form should NOT be visible after the redirect fires.
      expect(screen.queryByTestId("login-form")).not.toBeInTheDocument();
    });

    it("redirects to ?next= when authenticated and next param is set", async () => {
      setSessionState(makeSessionRead({ user: { email: "jane@example.com" } }), false);

      renderWithProviders(
        <Routes>
          <Route path="/login" element={<LoginScreen />} />
          <Route path="/admin/users" element={<LocationCapture />} />
        </Routes>,
        { initialEntries: ["/login?next=%2Fadmin%2Fusers"] },
      );

      await waitFor(() => {
        expect(screen.getByTestId("location-display")).toHaveTextContent("/admin/users");
      });
    });

    it("does not redirect while session is still loading", () => {
      // sessionLoading=true is the in-flight session probe state. Even
      // when session is null, the guard must NOT redirect (the user could
      // still be authenticated; we just do not know yet). The loading
      // placeholder takes precedence and the form is not rendered.
      setSessionState(null, true);

      renderWithProviders(
        <Routes>
          <Route path="/login" element={<LoginScreen />} />
          <Route path="/feed" element={<LocationCapture />} />
        </Routes>,
        { initialEntries: ["/login"] },
      );

      // Loading placeholder shown, form NOT shown, no redirect happened.
      expect(screen.getByTestId("login-screen-loading")).toBeInTheDocument();
      expect(screen.queryByTestId("login-form")).not.toBeInTheDocument();
      expect(screen.queryByTestId("location-display")).not.toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // isPending state: while the mutation is in flight, the Button primitive
  // shows a spinner via data-testid="button-loading-spinner" and the
  // inputs/buttons are disabled.
  //
  // Use MSW's delay() helper to slow the response so the test has a
  // deterministic window during which the loading state is observable.
  // -------------------------------------------------------------------------
  describe("isPending state", () => {
    it("shows loading spinner and disables inputs during in-flight mutation", async () => {
      const user = userEvent.setup();
      // Custom slow handler: 200ms delay before responding so we have a
      // reliable window in which to observe the in-flight UI state.
      server.use(
        http.post("/auth/login", async () => {
          await delay(200);
          return HttpResponse.json({ user: makeUserRead({ email: "jane@example.com" }) });
        }),
      );

      renderWithProviders(<LoginScreen />);

      await screen.findByTestId("login-screen");
      await user.type(screen.getByTestId("login-email"), "jane@example.com");
      await user.type(screen.getByTestId("login-password"), "somepassword");

      const submitButton = screen.getByTestId("login-submit");
      const emailInput = screen.getByTestId("login-email");
      const passwordInput = screen.getByTestId("login-password");

      // Click submit but do NOT await; the promise resolves AFTER all
      // microtasks, including the mutation's onSuccess. We need to assert
      // on the in-flight state in the 200ms window before the promise
      // resolves.
      const clickPromise = user.click(submitButton);

      // The Button primitive renders a Loader2 spinner with
      // data-testid="button-loading-spinner" while loading=true.
      await waitFor(() => {
        expect(screen.getByTestId("button-loading-spinner")).toBeInTheDocument();
      });

      // Inputs and the submit button are disabled while pending. The
      // Google button is intentionally NOT asserted on here - it is
      // independent of the email/password mutation (Google OAuth is a
      // full-page nav, not an XHR), so its enabled state is unaffected.
      expect((emailInput as HTMLInputElement).disabled).toBe(true);
      expect((passwordInput as HTMLInputElement).disabled).toBe(true);
      expect((submitButton as HTMLButtonElement).disabled).toBe(true);

      // Wait for the click to resolve and the mutation to settle so the
      // afterEach cleanup does not see in-flight async work.
      await clickPromise;

      // After the mutation resolves, the spinner should be gone.
      await waitFor(() => {
        expect(screen.queryByTestId("button-loading-spinner")).not.toBeInTheDocument();
      });
    });
  });

  // -------------------------------------------------------------------------
  // Zod client-side validation: empty fields, invalid email format. The
  // LoginScreen short-circuits before calling the mutation, so the inputs
  // surface their errors via the Input primitive's input-error <p>.
  // -------------------------------------------------------------------------
  describe("Zod client-side validation", () => {
    it("shows email and password required errors on empty submit", async () => {
      const user = userEvent.setup();
      renderWithProviders(<LoginScreen />);
      await screen.findByTestId("login-screen");

      // Click submit without typing anything. The form's onSubmit handler
      // runs Zod and short-circuits when validation fails, surfacing the
      // per-field messages.
      await user.click(screen.getByTestId("login-submit"));

      expect(await screen.findByText(/email is required/i)).toBeInTheDocument();
      expect(await screen.findByText(/password is required/i)).toBeInTheDocument();
    });

    it("shows email format error for invalid email", async () => {
      const user = userEvent.setup();
      renderWithProviders(<LoginScreen />);
      await screen.findByTestId("login-screen");

      await user.type(screen.getByTestId("login-email"), "not-an-email");
      await user.type(screen.getByTestId("login-password"), "somepassword");
      await user.click(screen.getByTestId("login-submit"));

      expect(await screen.findByText(/please enter a valid email address/i)).toBeInTheDocument();
    });
  });
});
