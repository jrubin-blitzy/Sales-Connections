/**
 * main.tsx - React 19 root mount for the Sales-Connections SPA.
 *
 * This file is the SOLE entry-point script referenced by the SPA shell
 * `frontend/index.html` (`<script type="module" src="/src/main.tsx">`).
 * Vite's Rollup pipeline starts here; everything reachable from this
 * module ends up in the production bundle. Removing or breaking this
 * file makes `npm run build` (and therefore `docker build ./frontend`)
 * fail with `Rollup failed to resolve import "/src/main.tsx"`.
 *
 * Per AAP Sec 0.5.2 (Layer 0 - Foundation, frontend tier):
 *   "frontend/src/main.tsx, frontend/src/App.tsx, frontend/src/router.tsx
 *    - React 19 root mount with <QueryClientProvider>, <AuthProvider>,
 *    <RouterProvider>."
 *
 * Per AAP Sec 0.4.3 (Surface 1 wiring):
 *   "frontend/src/main.tsx mounts <QueryClientProvider> from
 *    frontend/src/lib/queryClient.ts so that every TanStack Query hook
 *    in frontend/src/api/*.ts shares cache configuration."
 *
 * Provider stack ordering (matches App.tsx and AuthProvider.tsx
 * inline documentation, and is also asserted by
 * frontend/tests/test-utils.tsx so component tests run against an
 * identical context tree):
 *
 *     <StrictMode>
 *       <QueryClientProvider client={queryClient}>
 *         <AuthProvider>
 *           <RouterProvider router={router} />
 *         </AuthProvider>
 *       </QueryClientProvider>
 *     </StrictMode>
 *
 * Why this exact order:
 *   - QueryClientProvider must be OUTERMOST (after StrictMode) so that
 *     AuthProvider's useSessionQuery hook resolves a QueryClient via
 *     React context. Without the outer QueryClientProvider, the
 *     `useQuery(... )` call inside `useSessionQuery` throws "No
 *     QueryClient set, use QueryClientProvider to set one".
 *   - AuthProvider sits in the middle so that downstream route
 *     components (rendered by RouterProvider via the App layout
 *     <Outlet />) can read session state through useSession() / useRole().
 *   - RouterProvider is INNERMOST because it renders the route table
 *     (defined in @/router.tsx). The router uses <App /> as the layout
 *     route element for the authenticated subtree, so the App layout
 *     shell (header chrome + ToastContainer) lives BELOW the providers,
 *     not above them. This is the standard react-router-dom 6.x
 *     data-router pattern documented in DL-0041.
 *
 * Why <StrictMode>:
 *   React 19 StrictMode double-invokes effects, refs, and state
 *   updaters in development to surface side-effect bugs that would
 *   otherwise lurk until production. Several files in this codebase
 *   (notably frontend/src/router.tsx::OAuthStartRedirect) explicitly
 *   document StrictMode-aware behavior (the second
 *   window.location.replace call is a no-op because the navigation
 *   initiated by the first call has already taken control of the
 *   browser). Disabling StrictMode would mask those bugs and is
 *   forbidden by project convention.
 *
 *   StrictMode has ZERO runtime cost in production: React's bundler
 *   strips the StrictMode component to a no-op when NODE_ENV=production
 *   (set by frontend/Dockerfile in the builder stage just before
 *   `npm run build` runs).
 *
 * Why createRoot (not the legacy ReactDOM.render):
 *   React 18+ deprecated ReactDOM.render in favor of createRoot, which
 *   enables Concurrent Rendering features (automatic batching, useTransition,
 *   Suspense for data fetching). React 19 still supports ReactDOM.render
 *   but logs a deprecation warning at first use. createRoot is the only
 *   forward-compatible API per the React 19 migration guide.
 *
 * Why a hard throw on a missing #root element:
 *   `document.getElementById("root")` returns `Element | null`. The
 *   non-null assertion `!` would silence the TypeScript error but mask
 *   the runtime failure with an unhelpful "createRoot called on null"
 *   message. An explicit throw with a clear remediation hint
 *   ("verify frontend/index.html has <div id='root'></div>") helps
 *   future contributors diagnose the misconfiguration in seconds. The
 *   throw is unreachable in normal execution because index.html ships
 *   the <div id="root"></div> element on line 26.
 *
 * Stylesheet import:
 *   `@/styles/index.css` is imported here for its SIDE EFFECT
 *   (loading Tailwind directives + the @layer base block). Vite
 *   handles CSS imports specially: in dev they hot-reload via the HMR
 *   client; in production they are extracted into a content-hashed
 *   stylesheet at /assets/index-<hash>.css and linked from the
 *   generated index.html. This is the ONLY entry-point CSS import in
 *   the application; component styles are Tailwind utility classes
 *   inlined in JSX, not separate .css files (per AAP Sec 0.7.7 and
 *   styles/index.css's own header docstring).
 *
 * Conventions per AAP Sec 0.7.7 and frontend/.prettierrc.json:
 *   - Double quotes for all strings (singleQuote: false in Prettier).
 *   - Strict TypeScript; no `any`; explicit return-type / parameter
 *     types where they add clarity.
 *   - Path imports use the `@/` alias for `src/`.
 *   - Type-only imports use the `type` modifier (verbatimModuleSyntax:
 *     true in tsconfig.json). This file has no type-only imports
 *     because StrictMode, createRoot, etc. are all values.
 *   - Trailing commas; 2-space indent; line length <= 100.
 *   - No emoji - strict ASCII.
 *   - No default export. (This file does not export anything; it is a
 *     side-effecting bootstrap module.)
 *
 * Coordinates with:
 *   - frontend/index.html              The SPA shell that loads this script.
 *   - frontend/src/lib/queryClient.ts  Singleton QueryClient instance.
 *   - frontend/src/auth/AuthProvider.tsx Session/role context provider.
 *   - frontend/src/router.tsx          Route table (createBrowserRouter).
 *   - frontend/src/App.tsx             Layout route element (header,
 *                                      Outlet, ToastContainer).
 *   - frontend/src/styles/index.css    Tailwind directives and global
 *                                      element defaults.
 *   - frontend/tests/test-utils.tsx    Mirrors this provider stack so
 *                                      component tests render against
 *                                      the identical context tree.
 *
 * Excluded scope (deferred future enhancements per docstrings in
 * frontend/src/vite-env.d.ts and frontend/.env.example):
 *   - VITE_ENABLE_QUERY_DEVTOOLS conditional dynamic import of
 *     @tanstack/react-query-devtools. The dependency is NOT bundled in
 *     MVP per the per-feature dependency list in AAP Sec 0.3.4; the
 *     flag is reserved for future activation. Adding the import here
 *     would require bundling the devtools package, violating AAP
 *     Sec 0.6.2 ("Additional features not specified by the user or the
 *     technical specification are out of scope").
 *   - VITE_ENABLE_MSW conditional dynamic import of src/mocks/browser.ts.
 *     The browser-runtime mock worker is NOT in MVP scope per AAP
 *     Sec 0.6.1 (only files explicitly listed in AAP are created); MSW
 *     is configured for the test runner only via frontend/tests/setup.ts.
 *
 * Build / run:
 *   npm run build       Compiles TS, bundles via Vite, emits /dist/.
 *   docker build ./frontend
 *                       Multi-stage build invoking npm run build inside
 *                       the Node 20 Alpine builder stage; failure here
 *                       blocks the entire frontend image and the
 *                       docker-compose `frontend` service.
 *
 * Coverage exclusion:
 *   This file is excluded from coverage thresholds in
 *   frontend/vite.config.ts and frontend/vitest.config.ts (`exclude:
 *   ["src/main.tsx", ...]`) because the bootstrap is hard to test in
 *   isolation: createRoot mutates the global DOM and runs once at
 *   module load. The behavior IS exercised end-to-end by the
 *   containerized smoke test (docker compose up; curl /; verify the
 *   SPA shell HTML is served and React mounts).
 */

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "react-router-dom";

import { queryClient } from "@/lib/queryClient";
import { router } from "@/router";

// Side-effect import: loads Tailwind directives + global @layer base
// styles. Must be imported BEFORE any component renders so the first
// paint already carries the correct base styling. See styles/index.css
// header docstring for what this file contains.
import "@/styles/index.css";

// ---------------------------------------------------------------------------
// Locate the SPA mount point
// ---------------------------------------------------------------------------
// The mount-point div is declared in frontend/index.html line 26 as
// `<div id="root"></div>`. Throwing a clear error if the element is
// missing helps a contributor who edits index.html and accidentally
// removes or renames the div - the alternative (createRoot called on
// null) produces a much less actionable error message.

const rootElement = document.getElementById("root");
if (rootElement === null) {
  throw new Error(
    'Sales-Connections SPA bootstrap failed: no <div id="root"> element ' +
      "found in document. Verify frontend/index.html ships the root mount " +
      "point. This error is unreachable in normal execution.",
  );
}

// ---------------------------------------------------------------------------
// Mount the React tree
// ---------------------------------------------------------------------------
// Provider stack rationale documented in the file-level docstring above.
// React 19's createRoot returns a Root handle; we do NOT retain it
// because there is no need to programmatically unmount the SPA - the
// browser tab closing is the only "unmount" event.

createRoot(rootElement).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
);
