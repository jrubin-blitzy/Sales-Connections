/// <reference types="vitest" />

// =============================================================================
// Sales-Connections Frontend - Vite Build Configuration
// =============================================================================
// Central source of truth for how the SPA is compiled, served in development,
// previewed after build, and exercised by Vitest. Wires the React 19 plugin,
// the `@/` path alias, the dev-server proxy that forwards `/api`, `/auth`,
// `/healthz`, `/readyz`, and `/metrics` to the local Flask backend, the
// production rollup output (ES2022 with vendor-split manual chunks), and
// the inline Vitest test runner configuration with a v8 coverage threshold.
//
// Runs as an ESM module under Node 20+, so `__dirname`/`__filename` are not
// available; path resolution uses `fileURLToPath(new URL('./...',
// import.meta.url))` which is the modern, ESM-safe replacement.
// =============================================================================

import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath, URL } from "node:url";

// -----------------------------------------------------------------------------
// Dev-server proxy target resolution
// -----------------------------------------------------------------------------
// AAP Sec 0.4.3 mandates that `/api/*` and `/auth/*` (plus the observability
// surfaces) are proxied to the local Flask backend at http://localhost:5000.
// The target may be overridden via environment variable so contributors using
// a dev container, a remote Flask instance, or a non-default port do not need
// to edit this file.
//
// We honour both `VITE_DEV_PROXY_TARGET` (canonical name per AAP) and the
// pre-existing `VITE_DEV_API_PROXY_TARGET` (documented in `.env.example`) so
// that existing developer setups continue to work without modification.
// -----------------------------------------------------------------------------
const DEV_PROXY_TARGET =
  process.env.VITE_DEV_PROXY_TARGET ??
  process.env.VITE_DEV_API_PROXY_TARGET ??
  "http://localhost:5000";

// Reused proxy entry shape - changeOrigin rewrites the Host header to match
// the upstream (required by Authlib's OAuth state validation), and
// secure: false permits proxying to backends with self-signed certificates
// during development. Production never proxies through Vite (nginx + ALB
// terminate TLS and handle routing).
const proxyEntry = {
  target: DEV_PROXY_TARGET,
  changeOrigin: true,
  secure: false,
} as const;

// -----------------------------------------------------------------------------
// Vite configuration
// -----------------------------------------------------------------------------
// https://vitejs.dev/config/
export default defineConfig({
  // ---------------------------------------------------------------------------
  // Plugins
  // ---------------------------------------------------------------------------
  // React plugin enables Fast Refresh (HMR), the automatic JSX transform, and
  // React-specific build optimizations such as preserving displayName.
  plugins: [react()],

  // ---------------------------------------------------------------------------
  // Path resolution
  // ---------------------------------------------------------------------------
  // Per AAP Sec 0.3.7 the alias `@/` maps to `frontend/src/` so feature code
  // can import via `@/features/connections/ConnectionFeed` instead of fragile
  // relative paths like `../../../features/connections/ConnectionFeed`. The
  // alias must remain in lock-step with the `paths` entry in tsconfig.app.json.
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },

  // ---------------------------------------------------------------------------
  // Environment variable exposure
  // ---------------------------------------------------------------------------
  // Only variables prefixed `VITE_` are inlined into client bundles via
  // `import.meta.env`. Anything not prefixed VITE_ stays server-side and is
  // never shipped to the browser. Explicit declaration (vs relying on the
  // default) makes the security boundary obvious at code-review time.
  envPrefix: ["VITE_"],

  // ---------------------------------------------------------------------------
  // Dev server (npm run dev)
  // ---------------------------------------------------------------------------
  // host: true binds to 0.0.0.0 so the dev server is reachable from inside
  // a Docker container or another machine on the LAN. strictPort: true makes
  // Vite fail loudly if 5173 is already taken instead of silently binding to
  // a different port - critical for deterministic CI and developer workflows.
  server: {
    host: true,
    port: 5173,
    strictPort: true,
    // Forward backend traffic so the SPA does not need CORS configuration
    // during development. All five proxy paths target the same Flask process.
    proxy: {
      "/api": proxyEntry,
      "/auth": proxyEntry,
      "/healthz": proxyEntry,
      "/readyz": proxyEntry,
      "/metrics": proxyEntry,
    },
  },

  // ---------------------------------------------------------------------------
  // Preview server (npm run preview, after vite build)
  // ---------------------------------------------------------------------------
  // Used for smoke-testing the production bundle locally before deployment.
  preview: {
    host: true,
    port: 4173,
    strictPort: true,
  },

  // ---------------------------------------------------------------------------
  // Production build (npm run build)
  // ---------------------------------------------------------------------------
  // AAP Sec 0.5.2 mandates `target: 'es2022'`. Modern evergreen browsers (per
  // the package.json browserslist) all support ES2022, so we avoid the cost
  // of transpiling features like top-level await, class fields, and Error
  // cause to older targets. esbuild minification is faster than Terser and
  // produces equivalent output for our codebase. Sourcemaps are emitted in
  // production to enable Sentry / browser DevTools debugging; access can be
  // restricted later via S3 + CloudFront origin restrictions if required.
  build: {
    target: "es2022",
    outDir: "dist",
    sourcemap: true,
    minify: "esbuild",
    cssMinify: true,
    emptyOutDir: true,
    // Warn if any chunk exceeds 1 MB - guards against accidental fat-chunk
    // regressions. Modern bundlers should keep most chunks well under 1 MB
    // when manualChunks are set correctly.
    chunkSizeWarningLimit: 1024,
    rollupOptions: {
      output: {
        // Stable, cache-friendly hashing scheme: assets/<name>-<hash>.<ext>.
        // Long-term browser caches benefit from content-addressed filenames.
        entryFileNames: "assets/[name]-[hash].js",
        chunkFileNames: "assets/[name]-[hash].js",
        assetFileNames: "assets/[name]-[hash].[ext]",
        // Vendor split improves cache hit rate: when only feature code
        // changes, the React/Router/Query bundles stay cache-fresh in the
        // browser, dramatically shrinking the bytes-over-the-wire on deploys.
        manualChunks: {
          react: ["react", "react-dom"],
          router: ["react-router-dom"],
          query: ["@tanstack/react-query"],
        },
      },
    },
  },

  // ---------------------------------------------------------------------------
  // Vitest configuration (npm run test)
  // ---------------------------------------------------------------------------
  // Inlining the test config here lets `vitest run` work without a separate
  // vitest.config.ts. Per AAP Sec 0.7.7, the coverage threshold is 85% for
  // statements/lines/functions; branches is relaxed to 80% so conditional
  // rendering (`role === 'Admin' ? <X /> : <Y />`) does not block PRs over
  // unreachable false-paths.
  //
  // The triple-slash `/// <reference types="vitest" />` directive at the top
  // of the file extends Vite's `UserConfig` to accept this `test` block;
  // without it TypeScript would emit:
  //   "Object literal may only specify known properties, and 'test' does
  //    not exist in type 'UserConfig'".
  test: {
    environment: "jsdom",
    setupFiles: ["./tests/setup.ts"],
    globals: true,
    css: true,
    include: ["tests/**/*.{test,spec}.{ts,tsx}", "src/**/*.{test,spec}.{ts,tsx}"],
    coverage: {
      provider: "v8",
      reporter: ["text", "html", "lcov", "json-summary"],
      reportsDirectory: "./coverage",
      include: ["src/**/*.{ts,tsx}"],
      exclude: [
        "src/main.tsx", // bootstrap, hard to test in isolation
        "src/types/**", // shimmed third-party types
        "src/**/*.d.ts",
        "**/index.ts", // pure re-exports
      ],
      thresholds: {
        statements: 85,
        branches: 80,
        functions: 85,
        lines: 85,
      },
    },
  },
});
