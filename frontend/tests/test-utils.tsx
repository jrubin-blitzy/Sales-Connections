/**
 * test-utils.tsx - Custom render helpers for the Sales-Connections SPA tests.
 *
 * Centralized test infrastructure for the React 19.2.5 + TanStack Query 5.x
 * + react-router-dom 6.x SPA. Every component test in `frontend/tests/`
 * uses one of the two `render*` helpers exported here so that no test
 * mounts a routed/auth-aware component without the production provider
 * stack.
 *
 * Exports:
 *   - createTestQueryClient(overrides?)
 *       Fresh QueryClient with retry disabled, gcTime/staleTime set to 0,
 *       and refetchOnWindowFocus/Reconnect disabled to prevent cache
 *       leakage between tests.
 *
 *   - renderWithProviders(ui, options?)
 *       Mounts `ui` inside the production provider stack:
 *         QueryClientProvider > AuthProvider > MemoryRouter > ui
 *       The real `<AuthProvider>` calls `useSessionQuery()` which fires
 *       `GET /api/me`; tests must configure MSW handlers accordingly
 *       (the default handler in `tests/mocks/handlers.ts` returns 401).
 *       Use this helper for end-to-end-style tests that exercise the
 *       full session-hydration round-trip.
 *
 *   - renderWithMockedSession(ui, session, options?)
 *       Mounts `ui` inside the production provider stack with a
 *       synchronously-controlled session via `MockAuthProvider`:
 *         QueryClientProvider > MockAuthProvider > MemoryRouter > ui
 *       The supplied `session` (or `null` for unauthenticated) is read
 *       synchronously by `useSession()`, `useSessionLoading()`, and
 *       `useRole()` without an MSW round-trip. Use this helper for
 *       unit tests that need a fixed session to test conditional
 *       rendering (e.g., RoleGate, StatusChip's read/edit modes,
 *       admin surfaces).
 *
 *   - MockAuthProvider, MockAuthProviderProps
 *       Standalone version of the synchronous AuthContext provider
 *       used internally by `renderWithMockedSession`. Exported so
 *       tests can compose it manually with custom wrappers if the
 *       built-in helpers do not suit (rare).
 *
 *   - Re-exports of @testing-library/react primitives
 *       `render`, `screen`, `within`, `waitFor`, `fireEvent`, `cleanup`
 *       are re-exported from this file so consuming test files have a
 *       single import surface (`'../test-utils'` or `'../../test-utils'`)
 *       rather than importing from multiple sources. Keeps test
 *       boilerplate minimal across the suite.
 *
 *   - userEvent
 *       The default export of `@testing-library/user-event` is
 *       re-exported as the named `userEvent` so test files can call
 *       `userEvent.setup()` without a separate import.
 *
 * Why two renderers:
 *   - End-to-end tests for login/auth flows (e.g.,
 *     `tests/features/auth/LoginScreen.test.tsx`) need the real
 *     AuthProvider so they can observe the session-hydration
 *     round-trip via MSW and verify cookie/credential propagation.
 *   - Component tests for routed/feature components (e.g.,
 *     `tests/features/connections/ConnectionFeed.test.tsx`,
 *     `tests/features/admin/AdminPanel.test.tsx`) want a synchronous
 *     session override so they can assert behavior under specific
 *     role conditions (Admin / Contributor / Viewer / null) without
 *     MSW timing or flake risk.
 *
 * Provider stack ordering (matches production main.tsx + App.tsx):
 *
 *     QueryClientProvider
 *       > AuthProvider (or MockAuthProvider)
 *         > MemoryRouter
 *           > <ui>
 *
 * The MemoryRouter is initialized to `['/']` by default but each
 * call site may override via `options.initialEntries` to start the
 * router at a specific URL like `'/feed'`, `'/connections/:id'`, or
 * `'/admin/users'` for route-specific component testing.
 *
 * Per-test QueryClient invariant:
 *   Both helpers construct a fresh QueryClient by default
 *   (or accept a caller-supplied one for cache inspection). NEVER
 *   share a QueryClient across tests - the v5 cache survives between
 *   `render()` calls in the same worker, and a query resolved in
 *   test A would seed test B's cache, causing flakes.
 *
 * Conventions per `frontend/.prettierrc.json` and AAP Sec 0.7.7:
 *   - Strict TypeScript; no `any` (despite eslint allowing it in tests).
 *   - Double quotes; trailing commas; 2-space indent; line length <= 100.
 *   - No emoji; no console.log.
 *   - All exports are named (no default export).
 *
 * react-refresh/only-export-components is intentionally disabled for
 * this file: by design it co-exports React components
 * (`MockAuthProvider`), helper functions (`createTestQueryClient`,
 * `renderWithProviders`, `renderWithMockedSession`), TypeScript types
 * (the four `*Options`/`*Props`/`*Result` interfaces), and the
 * @testing-library re-export surface. Splitting these would defeat
 * the file's purpose as the suite's single import surface, and Fast
 * Refresh is not relevant inside `tests/` since vitest/jsdom does
 * not run the React Refresh runtime. The same disable convention is
 * used in `frontend/src/auth/AuthProvider.tsx` for analogous reasons.
 */
/* eslint-disable react-refresh/only-export-components */

import { type ReactElement, type ReactNode } from "react";
import { QueryClient, QueryClientProvider, type QueryClientConfig } from "@tanstack/react-query";
import { MemoryRouter, type MemoryRouterProps } from "react-router-dom";
import { render, type RenderOptions, type RenderResult } from "@testing-library/react";
import { vi } from "vitest";

import { AuthContext, AuthProvider, type AuthContextValue } from "@/auth/AuthProvider";
import type { Session } from "@/schemas/auth";

// ---------------------------------------------------------------------------
// createTestQueryClient
// ---------------------------------------------------------------------------

/**
 * Construct a fresh `QueryClient` configured for fast, deterministic
 * tests.
 *
 * Defaults applied:
 *   - `retry: false`             Tests resolve in a single attempt.
 *                                Without this, transient failures (e.g.,
 *                                an MSW handler that returns 500 for a
 *                                specific test) would block the test for
 *                                ~3 seconds per retry attempt under
 *                                TanStack Query's default exponential
 *                                backoff.
 *   - `gcTime: 0`                Garbage-collect cached queries
 *                                immediately after their last unmount.
 *                                Combined with the per-test client
 *                                pattern, this guarantees no cache
 *                                bleed between tests in the same file.
 *   - `staleTime: 0`             Every query is considered stale on
 *                                mount, forcing a fresh fetch and
 *                                making refetch behavior easy to assert.
 *   - `refetchOnWindowFocus: false`
 *                                jsdom emits `focus` events during
 *                                `userEvent.tab()` and similar - we do
 *                                not want those to trigger spurious
 *                                refetches that would race with
 *                                assertions.
 *   - `refetchOnReconnect: false`
 *                                jsdom does not provide a meaningful
 *                                `online` signal, so refetch-on-
 *                                reconnect would be effectively dead
 *                                code. Disabling it removes one source
 *                                of test-environment ambiguity.
 *   - `mutations.retry: false`   Same rationale as queries.retry; tests
 *                                exercise mutations once per assertion.
 *
 * Caller overrides (via the optional `overrides` argument) are applied
 * over the test defaults using shallow merge; nested fields can be
 * overridden by passing matching nested objects.
 *
 * @example
 *   // Plain test client with all defaults:
 *   const queryClient = createTestQueryClient();
 *
 * @example
 *   // Override `retry` for a specific test that intentionally exercises
 *   // retry behavior:
 *   const queryClient = createTestQueryClient({
 *     defaultOptions: { queries: { retry: 2 } },
 *   });
 *
 * @param overrides - Optional QueryClientConfig partial that is merged
 *                    on top of the test defaults. Useful for tests that
 *                    need non-default behavior for a single assertion.
 * @returns         - A new QueryClient instance suitable for one test.
 */
export function createTestQueryClient(overrides?: QueryClientConfig): QueryClient {
  return new QueryClient({
    ...overrides,
    defaultOptions: {
      ...overrides?.defaultOptions,
      queries: {
        retry: false,
        gcTime: 0,
        staleTime: 0,
        refetchOnWindowFocus: false,
        refetchOnReconnect: false,
        ...overrides?.defaultOptions?.queries,
      },
      mutations: {
        retry: false,
        ...overrides?.defaultOptions?.mutations,
      },
    },
  });
}

// ---------------------------------------------------------------------------
// renderWithProviders - real AuthProvider (session via MSW)
// ---------------------------------------------------------------------------

/**
 * Options accepted by `renderWithProviders`.
 *
 * Inherits all of @testing-library/react's `RenderOptions` EXCEPT
 * `wrapper` (which is reserved for the helper's own provider stack).
 * Adds three helper-specific fields:
 *
 *   - `initialEntries`     Start the in-memory router at a specific URL.
 *                          Default: `['/']`. Common values:
 *                          `['/feed']`, `['/connections/:id/edit']`,
 *                          `['/admin/users']`, `['/login']`.
 *
 *   - `queryClient`        Caller-supplied QueryClient. Default: a
 *                          fresh client from `createTestQueryClient()`.
 *                          Use this when the test needs to inspect or
 *                          mutate the cache directly (e.g., to verify
 *                          a hook invalidated a specific query key).
 *
 *   - `skipAuthProvider`   When true, the `<AuthProvider>` is omitted
 *                          from the wrapper. Reserved for tests that
 *                          mount their own custom Auth provider (e.g.,
 *                          unit tests for `<AuthProvider>` itself).
 *                          Default: `false`.
 */
export interface RenderWithProvidersOptions extends Omit<RenderOptions, "wrapper"> {
  /** Initial URL entries for the MemoryRouter. Default: `['/']`. */
  initialEntries?: MemoryRouterProps["initialEntries"];
  /** Provide a custom QueryClient (e.g., to inspect cache state). */
  queryClient?: QueryClient;
  /** When true, omit the AuthProvider (e.g., for tests that mount their own). */
  skipAuthProvider?: boolean;
}

/**
 * Result of `renderWithProviders` and `renderWithMockedSession`.
 *
 * Extends @testing-library/react's `RenderResult` with the
 * `queryClient` reference so tests can call
 * `result.queryClient.getQueryData(['key'])` or
 * `result.queryClient.invalidateQueries(...)` directly without having
 * to reach into the provider tree.
 */
export interface RenderWithProvidersResult extends RenderResult {
  /** The QueryClient that was supplied to the QueryClientProvider. */
  queryClient: QueryClient;
}

/**
 * Render a React element wrapped in the production provider stack:
 *
 *     QueryClientProvider > AuthProvider > MemoryRouter > <ui>
 *
 * The real `<AuthProvider>` will call `useSessionQuery()` which fires
 * `GET /api/me` via the MSW server configured in `tests/mocks/server.ts`.
 * Tests must configure MSW handlers (`tests/mocks/handlers.ts`) so that
 * the session query resolves in the desired state for the assertion.
 * The default handler returns 401 (unauthenticated).
 *
 * Use this helper for end-to-end-style tests where exercising the
 * actual session-hydration flow is part of the assertion (login screen,
 * post-logout redirect, OAuth callback). For unit tests that just need
 * a fixed session to verify conditional rendering, prefer
 * `renderWithMockedSession`.
 *
 * @example
 *   // Smoke render with the default provider stack:
 *   const { getByText } = renderWithProviders(<MyComponent />);
 *
 * @example
 *   // Start the router at a specific URL:
 *   renderWithProviders(<ConnectionDetail />, {
 *     initialEntries: ['/connections/123e4567-e89b-12d3-a456-426614174000'],
 *   });
 *
 * @example
 *   // Inject a caller-controlled QueryClient for cache inspection:
 *   const queryClient = createTestQueryClient();
 *   queryClient.setQueryData(['connections', 'feed'], cachedFeed);
 *   const { getByRole } = renderWithProviders(<ConnectionFeed />, {
 *     queryClient,
 *   });
 *
 * @param ui      - The React element under test.
 * @param options - Optional render options; see RenderWithProvidersOptions.
 * @returns       - The standard render result extended with `queryClient`.
 */
export function renderWithProviders(
  ui: ReactElement,
  options: RenderWithProvidersOptions = {},
): RenderWithProvidersResult {
  // Capture helper-specific options into local closure variables so
  // `Wrapper` (defined below) can reference them. Defaults are applied
  // here so the closure observes the same values regardless of how the
  // caller spelled the options bag.
  const queryClient = options.queryClient ?? createTestQueryClient();
  const initialEntries = options.initialEntries ?? ["/"];
  const skipAuthProvider = options.skipAuthProvider ?? false;

  /**
   * Inline wrapper component that React Testing Library invokes once
   * per render, mounting the provider stack around the test subject.
   *
   * The router lives INSIDE the AuthProvider so that route-aware
   * components (e.g., `<ProtectedRoute>` calling `useNavigate()`) can
   * read the session from context AND issue navigation programmatically
   * - matching the production provider order.
   */
  function Wrapper({ children }: { children: ReactNode }): ReactElement {
    const inner = <MemoryRouter initialEntries={initialEntries}>{children}</MemoryRouter>;
    return (
      <QueryClientProvider client={queryClient}>
        {skipAuthProvider ? inner : <AuthProvider>{inner}</AuthProvider>}
      </QueryClientProvider>
    );
  }

  // The remaining option keys are forwarded to React Testing Library
  // verbatim. We deliberately spread `options` (rather than
  // hand-picking) so future @testing-library/react options become
  // available without code changes here. The three helper-specific
  // keys (`queryClient`, `initialEntries`, `skipAuthProvider`) are
  // not consumed by RTL; if RTL adds collidingly-named options in a
  // future release, the explicit `wrapper: Wrapper` override below
  // prevents the most damaging conflict.
  const result = render(ui, { ...options, wrapper: Wrapper });
  return { ...result, queryClient };
}

// ---------------------------------------------------------------------------
// MockAuthProvider - synchronous AuthContext provider for unit tests
// ---------------------------------------------------------------------------

/**
 * Props for `<MockAuthProvider>`.
 *
 * The `session` and `isLoading` fields drive the hydrated context
 * value read by `useSession()` and `useSessionLoading()`. Because
 * `useRole()` derives `role` from `session.user.role`, supplying a
 * non-null `session` is sufficient to exercise role-gated UI.
 *
 * The `logout` field is accepted for API symmetry and to give consumers
 * a single, type-safe way to declare a logout spy. The production
 * `AuthContextValue` includes a `logout` function (the localStorage-based
 * auth helper in `@/auth/AuthProvider`), but `MockAuthProvider` wires its
 * own no-op to keep tests isolated from localStorage side effects.
 */
export interface MockAuthProviderProps {
  /**
   * The session to provide. `null` simulates the unauthenticated /
   * loading state; a `Session` object simulates an authenticated
   * user. Use the `makeSessionRead(...)` factory from
   * `tests/mocks/data.ts` for ergonomic construction with
   * predictable defaults.
   */
  session: Session | null;

  /**
   * The loading flag exposed by `useSessionLoading()`. Defaults to
   * `false` so consuming components see a fully-resolved auth state.
   * Pass `true` to exercise loading-state UI (spinners, skeleton
   * rows) under a deterministic synchronous render.
   */
  isLoading?: boolean;

  /**
   * The element subtree that consumes the mocked context. Required.
   */
  children: ReactNode;

  /**
   * Optional logout spy. Currently NOT wired into the AuthContext
   * value because production `AuthContextValue` has no `logout`
   * field; tests requiring a callable logout spy should
   * `vi.mock("@/api/auth")` to replace `useLogoutMutation`. This
   * prop is preserved on `MockAuthProviderProps` for API symmetry
   * with `renderWithMockedSession.options.logout` and to ease
   * future migration if `AuthContextValue` ever gains a logout
   * field.
   */
  logout?: () => void;
}

/**
 * Synchronous AuthContext provider for unit tests.
 *
 * Mounts a real `AuthContext.Provider` whose `value` is constructed
 * directly from the supplied props - bypassing the production
 * AuthProvider's `useSessionQuery()` round-trip. This makes
 * `useSession()`, `useSessionLoading()`, and `useRole()` resolve
 * synchronously to a caller-controlled value, eliminating MSW
 * timing variability for unit tests.
 *
 * The `logout` prop is accepted but currently unused in the context
 * value (see `MockAuthProviderProps.logout`). It is referenced via
 * `void` below so that strict-typed tooling does not flag the unused
 * prop and so that future schema evolution remains source-stable.
 *
 * @example
 *   const session = makeSessionRead({ user: { role: 'Admin' } });
 *   render(
 *     <MockAuthProvider session={session}>
 *       <RoleGate role="Admin">
 *         <button>Hard Delete</button>
 *       </RoleGate>
 *     </MockAuthProvider>,
 *   );
 *
 * @param props - See `MockAuthProviderProps`.
 * @returns     - The provider element wrapping `children`.
 */
export function MockAuthProvider(props: MockAuthProviderProps): ReactElement {
  const { session, isLoading = false, children, logout } = props;

  // The `logout` prop is documented as currently-unused but accepted
  // for API symmetry; reference it via `void` so TypeScript's
  // `noUnusedParameters` (when re-enabled) and ESLint's
  // `no-unused-vars` (active outside the test glob) do not flag it.
  // This is a no-op at runtime.
  void logout;

  // Construct the context value from the supplied props. The shape
  // mirrors production `AuthContextValue` exactly: { session,
  // isLoading, isError }. We surface `isError = false` because the
  // mock provider intentionally does not exercise the error branch -
  // tests that need to drive the error path should use
  // `renderWithProviders` and an MSW error handler instead.
  const value: AuthContextValue = {
    session,
    isLoading,
    isError: false,
    login: async () => {},
    logout: () => {},
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

// ---------------------------------------------------------------------------
// renderWithMockedSession - synchronous session override
// ---------------------------------------------------------------------------

/**
 * Options accepted by `renderWithMockedSession`.
 *
 * Extends `RenderWithProvidersOptions` with two synchronous-session-
 * specific fields:
 *
 *   - `isLoading`   Forwarded to `MockAuthProvider.isLoading` so tests
 *                   can drive the loading-state UI deterministically.
 *                   Default: `false`.
 *   - `logout`      Caller-supplied logout spy. Forwarded to
 *                   `MockAuthProvider.logout`. Currently unused at the
 *                   context layer (see `MockAuthProviderProps.logout`)
 *                   but defaulted via `vi.fn()` so the helper always
 *                   has a callable reference. Tests that need to
 *                   assert on logout invocations should still
 *                   `vi.mock("@/api/auth")` to replace
 *                   `useLogoutMutation` in the production code path.
 */
export interface RenderWithMockedSessionOptions extends RenderWithProvidersOptions {
  /** Forwarded to MockAuthProvider; controls `useSessionLoading()` return value. */
  isLoading?: boolean;
  /**
   * Caller-supplied logout spy. Defaults to `vi.fn()` if omitted so the
   * helper always has a callable reference. See
   * `MockAuthProviderProps.logout` for the full contract.
   */
  logout?: () => void;
}

/**
 * Render a React element wrapped in the production provider stack
 * with a synchronously-controlled session via `MockAuthProvider`:
 *
 *     QueryClientProvider > MockAuthProvider > MemoryRouter > <ui>
 *
 * Use this helper for unit tests that need a fixed session to verify
 * conditional rendering by role (RoleGate, StatusChip, admin
 * surfaces) without exercising the real `useSessionQuery()` /api/me
 * round-trip. The supplied `session` flows directly into
 * `AuthContext`, so `useSession()`, `useSessionLoading()`, and
 * `useRole()` resolve synchronously to the caller-controlled values.
 *
 * Pass `null` as the second argument to simulate the unauthenticated
 * state (useful for testing redirect logic in `<ProtectedRoute>`).
 * Pass `options.isLoading: true` to simulate the initial loading
 * state (useful for testing skeleton/spinner UI).
 *
 * Pass `options.skipAuthProvider: true` to omit the MockAuthProvider
 * entirely (e.g., for tests that mount their own custom Auth wrapper);
 * in that case the supplied `session` and `isLoading` are ignored.
 *
 * @example
 *   // Test admin-only UI under an Admin session:
 *   const session = makeSessionRead({ user: { role: 'Admin' } });
 *   const { getByRole } = renderWithMockedSession(<AdminPanel />, session);
 *
 * @example
 *   // Test redirect for an unauthenticated user:
 *   const { getByText } = renderWithMockedSession(<ProtectedRoute />, null, {
 *     initialEntries: ['/feed'],
 *   });
 *
 * @example
 *   // Test loading-state UI:
 *   renderWithMockedSession(<MyComponent />, null, { isLoading: true });
 *
 * @param ui      - The React element under test.
 * @param session - The session to expose via `useSession()`. `null` for
 *                  unauthenticated; `Session` object for authenticated.
 * @param options - Optional render options; see
 *                  `RenderWithMockedSessionOptions`.
 * @returns       - The standard render result extended with `queryClient`.
 */
export function renderWithMockedSession(
  ui: ReactElement,
  session: Session | null,
  options: RenderWithMockedSessionOptions = {},
): RenderWithProvidersResult {
  // Capture helper-specific options into local closure variables. The
  // logout spy defaults to `vi.fn()` so the prop is always callable
  // even if a test forgot to supply one - keeps assertions like
  // `expect(logout).toHaveBeenCalledTimes(0)` from blowing up under
  // `undefined.toHaveBeenCalledTimes`.
  const queryClient = options.queryClient ?? createTestQueryClient();
  const initialEntries = options.initialEntries ?? ["/"];
  const isLoading = options.isLoading ?? false;
  const logout = options.logout ?? vi.fn();
  const skipAuthProvider = options.skipAuthProvider ?? false;

  /**
   * Inline wrapper component that mounts the provider stack with the
   * mocked AuthContext. When `skipAuthProvider` is true the
   * MockAuthProvider is omitted entirely (matching the
   * `renderWithProviders` semantics) - useful for tests that
   * intentionally mount no auth at all.
   */
  function Wrapper({ children }: { children: ReactNode }): ReactElement {
    const inner = <MemoryRouter initialEntries={initialEntries}>{children}</MemoryRouter>;
    if (skipAuthProvider) {
      return <QueryClientProvider client={queryClient}>{inner}</QueryClientProvider>;
    }
    return (
      <QueryClientProvider client={queryClient}>
        <MockAuthProvider session={session} isLoading={isLoading} logout={logout}>
          {inner}
        </MockAuthProvider>
      </QueryClientProvider>
    );
  }

  // Forward remaining options to RTL verbatim (see comment on the
  // analogous `render(...)` call in `renderWithProviders`).
  const result = render(ui, { ...options, wrapper: Wrapper });
  return { ...result, queryClient };
}

// ---------------------------------------------------------------------------
// Re-exports for single-import convenience
// ---------------------------------------------------------------------------
//
// Test files import from this module via `'../test-utils'` (or a
// computed relative path) and get all of:
//
//     import {
//       renderWithProviders,
//       renderWithMockedSession,
//       screen,
//       waitFor,
//       userEvent,
//     } from "../test-utils";
//
// rather than mixing imports from `@testing-library/react`,
// `@testing-library/user-event`, and the local helper file. This keeps
// test boilerplate minimal and gives the team a single chokepoint for
// future cross-cutting changes (e.g., adding a global toast-mock
// helper).
// ---------------------------------------------------------------------------

export { render, screen, within, waitFor, fireEvent, cleanup } from "@testing-library/react";
export { default as userEvent } from "@testing-library/user-event";
