/// <reference types="vite/client" />

/**
 * vite-env.d.ts - Vite client type shim for the Sales-Connections SPA.
 *
 * This file has two purposes:
 *
 *   1. The triple-slash <reference types="vite/client" /> directive on the
 *      first line pulls in Vite's built-in TypeScript type definitions for
 *      `import.meta.env`, `import.meta.glob`, `import.meta.hot`, and the
 *      module declarations for asset imports (*.svg, *.png, *.json,
 *      *.module.css, etc.). Without this directive, accessing any of those
 *      Vite-provided APIs would produce TypeScript compile errors.
 *
 *   2. The ImportMetaEnv interface declared below augments (via TypeScript's
 *      declaration-merging rules) the base ImportMetaEnv interface from
 *      `vite/client`. The result is a single interface that exposes BOTH
 *      Vite's built-in fields (MODE, BASE_URL, DEV, PROD, SSR) AND the
 *      project-specific VITE_* environment variables this app consumes.
 *      Mirroring frontend/.env.example here means TypeScript surfaces the
 *      exact set of variables in IDE autocomplete and reports a compile
 *      error when code references an undocumented or misspelled variable.
 *
 * CONVENTIONS
 *
 *   - The triple-slash reference MUST be the very first line of the file.
 *     TypeScript only recognises triple-slash directives when they appear
 *     before any other code or comments. Moving it below this comment
 *     block would silently disable the Vite type pull-in.
 *
 *   - This file MUST remain a global declaration (no `import` or `export`
 *     statements). Adding `export {}` would convert it to an ES module,
 *     making the interface declarations module-local and breaking the
 *     declaration-merging augmentation of the global ImportMetaEnv.
 *
 *   - All fields are `readonly` because environment variables are immutable
 *     at runtime. Vite resolves them at build time and freezes them into
 *     the bundle; assigning to `import.meta.env.VITE_*` has no effect.
 *
 *   - Boolean-flag variables use the literal-string union "true" | "false"
 *     instead of `boolean`. Vite always serialises env values as strings,
 *     so typing as boolean would be a runtime lie. The literal union forces
 *     consumers to compare via `=== "true"`, which is the correct pattern.
 *     Comparing the string "false" to a JavaScript boolean truthiness check
 *     would be a bug because non-empty strings are always truthy.
 *
 *   - JSDoc comments next to each field surface in IDE hover-help, so new
 *     contributors can read the purpose, default value, and consumers
 *     without leaving their editor.
 *
 * ADDING A NEW VARIABLE
 *
 *   1. Document the variable's purpose, allowed values, and default in
 *      frontend/.env.example.
 *   2. Add a corresponding `readonly VITE_NEW_VAR: ...` declaration to
 *      the ImportMetaEnv interface below.
 *   3. Reference the variable in source code via
 *      `import.meta.env.VITE_NEW_VAR`.
 *   4. If the variable must be baked into production bundles at build
 *      time, add an `ARG` for it in frontend/Dockerfile and pass it
 *      through to `npm run build`.
 *
 * AAP REFERENCES
 *
 *   - Section 0.2.3 (frontend tier file inventory)
 *   - Section 0.4.3 (Surface 1: SPA <-> Backend REST integration; correlation
 *     ID propagation; auth route base)
 *   - Section 0.7.7 (Coding and quality standards: pinned versions, no `any`)
 */

/**
 * Augmentation of the ImportMetaEnv interface defined by `vite/client`.
 *
 * The base interface from `vite/client` already declares:
 *   - MODE: string         (e.g., "development" | "production" | "test")
 *   - BASE_URL: string     (Vite `base` config, default "/")
 *   - DEV: boolean         (true when running `vite` / `vite dev`)
 *   - PROD: boolean        (true when running `vite build`)
 *   - SSR: boolean         (always false for this SPA - no SSR configured)
 *   - an index signature `[key: string]` of unspecified type, kept open
 *     by Vite for late-added variables that have not yet been declared
 *
 * Below we add the project-specific VITE_* variables consumed by this SPA.
 * TypeScript's interface-declaration-merging combines both sets into a
 * single ImportMetaEnv at type-check time.
 */
interface ImportMetaEnv {
  /**
   * Base URL for the Flask backend REST API.
   *
   * - Default in development: "/api". The Vite dev server proxies "/api/*"
   *   to http://localhost:5000 (configured in frontend/vite.config.ts), so
   *   the SPA never has to deal with CORS during local development.
   * - Containerised local stack (docker-compose): may be overridden to an
   *   absolute URL such as "http://localhost:5000" via build arg, because
   *   nginx (not the Vite dev server) serves the bundle and the SPA must
   *   reach the backend container directly.
   * - Production: full HTTPS URL of the ALB-fronted backend, e.g.,
   *   "https://api.sales-connections.example.com".
   *
   * Consumed by frontend/src/api/client.ts to construct absolute request URLs.
   */
  readonly VITE_API_BASE_URL: string;

  /**
   * Base URL for OAuth and authentication endpoints (sibling of
   * VITE_API_BASE_URL).
   *
   * - Default: "/auth".
   * - The Flask backend mounts the OAuth surface under "/auth" rather than
   *   "/api/auth" (see backend/app/__init__.py blueprint registration and
   *   AAP Section 0.4.3), so the SPA needs a separate base URL for auth
   *   redirects (the "/auth/google/start" 302 redirect link in particular).
   *
   * Consumed by frontend/src/auth/AuthProvider.tsx and the LoginScreen
   * for constructing OAuth start URLs.
   */
  readonly VITE_AUTH_BASE_URL: string;

  /**
   * Feature flag: when "true", mounts the TanStack Query devtools panel
   * inside the running app for cache inspection during development.
   *
   * - Allowed values: "true" or "false". Default: "false".
   * - String-literal type because Vite passes env values as strings.
   *
   * Note: For MVP the @tanstack/react-query-devtools dependency is NOT
   * bundled (decision-log records the deferral). Toggling this flag is a
   * no-op until the dependency is added; the flag is declared here so
   * frontend/src/main.tsx can perform the conditional dynamic-import once
   * the package is introduced.
   */
  readonly VITE_ENABLE_QUERY_DEVTOOLS: "true" | "false";

  /**
   * Feature flag: when "true", activates the Mock Service Worker (MSW)
   * in the browser for offline frontend development against in-memory
   * fixtures instead of the real Flask backend.
   *
   * - Allowed values: "true" or "false". Default: "false".
   * - String-literal type because Vite passes env values as strings.
   *
   * Note: For MVP, MSW is configured for tests only via
   * frontend/tests/setup.ts. Browser-runtime activation is a no-op until
   * src/mocks/browser.ts is added (decision-log records the deferral);
   * the flag is declared here so frontend/src/main.tsx can perform the
   * conditional dynamic-import once the worker file is introduced.
   */
  readonly VITE_ENABLE_MSW: "true" | "false";

  /**
   * Prefix for per-page correlation IDs generated by
   * frontend/src/lib/correlationId.ts.
   *
   * - Default: "sc-fe-" (Sales-Connections frontend).
   * - The full correlation ID is `<prefix><uuid v4>` and is sent as the
   *   `X-Correlation-Id` request header on every API call by
   *   frontend/src/api/client.ts. The backend echoes the header back in
   *   responses and stamps it onto every structlog line so a single ID
   *   threads through both browser console and CloudWatch logs (see AAP
   *   Section 0.4.3).
   */
  readonly VITE_CORRELATION_ID_PREFIX: string;

  /**
   * Sentry DSN for client-side error reporting.
   *
   * - Default: "" (empty - Sentry disabled).
   * - When non-empty, the Sentry browser SDK initialises with this DSN
   *   and captures uncaught exceptions, unhandled promise rejections, and
   *   ErrorBoundary captures.
   *
   * Note: Reserved for future use; not active in MVP per AAP scope. The
   * variable is declared here so future enablement does not require a
   * type-shim change.
   */
  readonly VITE_SENTRY_DSN: string;
}

/**
 * Augmentation of the ImportMeta interface defined by `vite/client`.
 *
 * The `readonly env: ImportMetaEnv` declaration here re-asserts (does
 * not replace) the binding between `import.meta.env` and our augmented
 * ImportMetaEnv, ensuring TypeScript routes property accesses through
 * the merged interface even in projects that load multiple .d.ts files
 * declaring their own ImportMeta extensions.
 *
 * The base ImportMeta from `vite/client` also exposes `url`, `hot`, and
 * `glob`; those declarations remain in effect via interface merging.
 */
interface ImportMeta {
  readonly env: ImportMetaEnv;
}
