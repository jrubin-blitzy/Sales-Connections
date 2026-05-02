/**
 * Vitest global setup — runs ONCE per worker before any test file is
 * loaded. Registers @testing-library/jest-dom matchers (e.g.,
 * `toBeInTheDocument()`, `toHaveTextContent()`) so component tests in
 * later layers can use them without per-file imports.
 *
 * Referenced by `frontend/vitest.config.ts` via the `setupFiles`
 * configuration:
 *
 *     test: {
 *       setupFiles: ["./tests/setup.ts"],
 *     }
 *
 * This file MUST exist for vitest to start; without it, any
 * `npm run test` invocation aborts with a "Failed to load url" error
 * BEFORE collecting any test files.
 *
 * Layer-0 scope per AAP Sec 0.6.1:
 *   `frontend/tests/setup.ts` — vitest setup file with
 *   @testing-library/jest-dom matchers.
 */
import "@testing-library/jest-dom/vitest";
