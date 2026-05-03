/**
 * setup.ts - Global Vitest setup for the Sales-Connections SPA.
 *
 * Loaded automatically by Vitest before each test file (configured in
 * frontend/vitest.config.ts via `test.setupFiles: ["./tests/setup.ts"]`).
 * This file is the SINGLE source of cross-test infrastructure for the
 * frontend test suite; per-test setup/teardown belongs in individual
 * `*.test.{ts,tsx}` files.
 *
 * Responsibilities (per AAP Sec 0.2.3 and assigned-folder requirements):
 *   1. Register @testing-library/jest-dom matchers on Vitest's `expect`
 *      so every test file can use `toBeInTheDocument`, `toHaveTextContent`,
 *      `toBeVisible`, `toHaveAttribute`, etc., without per-file imports.
 *   2. Boot the MSW (Mock Service Worker) Node server with the default
 *      handler set defined in tests/mocks/handlers.ts:
 *        - beforeAll: server.listen({ onUnhandledRequest: "error" })
 *        - afterEach: server.resetHandlers()
 *        - afterAll:  server.close()
 *      `onUnhandledRequest: "error"` makes the test fail immediately if a
 *      fetch reaches an unhandled URL, preventing silent network leaks.
 *   3. Auto-cleanup React Testing Library trees between tests via
 *      `cleanup()` in afterEach. Belt-and-suspenders alongside RTL's
 *      built-in cleanup auto-registration; harmless if it runs twice.
 *   4. Polyfill jsdom-missing browser APIs:
 *        - window.matchMedia (some Tailwind-aware code/libraries probe it)
 *        - IntersectionObserver (lazy-load and visibility libraries)
 *        - HTMLDialogElement.prototype.show / showModal / close
 *          (jsdom 25.x ships incomplete <dialog> support; Modal.tsx relies
 *           on the `open` attribute toggling correctly).
 *        - crypto.randomUUID (defensive; jsdom 25.x supports it natively
 *          but correlationId.ts depends on it being present).
 *   5. Reset module-level singletons between tests to prevent state
 *      bleed across tests:
 *        - Toast queue via __resetToastStoreForTesting()
 *        - Correlation ID cache via resetCorrelationId()
 *
 * Coordinates with:
 *   - frontend/vitest.config.ts (references this file via setupFiles).
 *   - frontend/src/components/ui/Toast.tsx (exposes the reset escape hatch).
 *   - frontend/src/lib/correlationId.ts (exposes resetCorrelationId).
 *   - frontend/tests/mocks/server.ts (exports the MSW Node server).
 *   - frontend/tests/mocks/handlers.ts (the default handler set served).
 *   - frontend/tsconfig.json (declares "vitest/globals" and
 *     "@testing-library/jest-dom" in compilerOptions.types so the matcher
 *     types extend Vitest's expect at type-check time).
 *
 * Conventions per AAP Sec 0.7.7 and frontend/.prettierrc.json:
 *   - Strict TypeScript; no `any`.
 *   - Double quotes (singleQuote: false); trailing commas; 2-space indent;
 *     line length <= 100.
 *   - No emoji; no console.log.
 *   - Top-level side effects ARE permitted in this file because it IS the
 *     global setup file. Each side effect is bracketed by an explanatory
 *     comment.
 */

// ---------------------------------------------------------------------------
// Imports
// ---------------------------------------------------------------------------

// Side-effect import: registers DOM-aware matchers (toBeInTheDocument, etc.)
// on Vitest's `expect`. The "/vitest" subpath is the modern entry-point that
// extends Vitest's expect (the older "/extend-expect" path targets Jest and
// produces TypeScript errors when imported here).
import "@testing-library/jest-dom/vitest";

import { afterAll, afterEach, beforeAll, vi } from "vitest";
import { cleanup } from "@testing-library/react";

// Synchronous Node built-in import. Used as a defensive crypto polyfill
// when globalThis.crypto.randomUUID is missing (older jsdom versions).
import { webcrypto } from "node:crypto";

import { __resetToastStoreForTesting } from "@/components/ui/Toast";
import { resetCorrelationId } from "@/lib/correlationId";
import { server } from "./mocks/server";

// ---------------------------------------------------------------------------
// crypto.randomUUID polyfill
// ---------------------------------------------------------------------------
// correlationId.ts calls crypto.randomUUID() unconditionally. jsdom 25.x
// supports it natively, but we install a defensive polyfill to guard
// against (a) future jsdom downgrades, (b) early-evaluation order quirks
// where modules are imported before jsdom finishes wiring globals, and
// (c) Node-environment-only test files that bypass jsdom entirely.
//
// Node's `node:crypto` ships a WebCrypto-conformant implementation under
// the `webcrypto` export which exposes randomUUID(), so it is a drop-in
// replacement for the browser global.
if (typeof globalThis.crypto?.randomUUID !== "function") {
  Object.defineProperty(globalThis, "crypto", {
    configurable: true,
    writable: true,
    value: webcrypto,
  });
}

// ---------------------------------------------------------------------------
// window.matchMedia polyfill
// ---------------------------------------------------------------------------
// jsdom 25.x does NOT implement matchMedia. Tailwind responsive utilities
// and a number of third-party libraries (e.g., theming, prefers-reduced-
// motion checks) call `window.matchMedia("(min-width: 640px)")`; without a
// stub the call throws "matchMedia is not a function" and aborts the test.
// We provide a no-op implementation that always returns matches=false so
// consumers see a deterministic "no match" response. Tests that need to
// assert specific match behavior can override the mock per-test using
// vi.spyOn(window, "matchMedia").
Object.defineProperty(window, "matchMedia", {
  configurable: true,
  writable: true,
  value: vi.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    // Legacy Safari API names (kept for completeness; some libraries still
    // probe these even though they are deprecated in favor of
    // addEventListener/removeEventListener).
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })),
});

// ---------------------------------------------------------------------------
// IntersectionObserver polyfill
// ---------------------------------------------------------------------------
// jsdom 25.x does NOT implement IntersectionObserver. Components or
// libraries that construct one (e.g., for lazy-load triggers, infinite
// scroll, viewport-aware animations) crash on `new IntersectionObserver(...)`
// without this stub. We provide a no-op implementation conforming to the
// IntersectionObserver interface so construction succeeds and observe/
// unobserve/disconnect/takeRecords are callable. Tests that need to
// simulate intersection events can replace this on a per-test basis.
class MockIntersectionObserver implements IntersectionObserver {
  readonly root: Element | Document | null = null;
  readonly rootMargin: string = "";
  readonly thresholds: ReadonlyArray<number> = [];

  // Constructor signature mirrors the platform: (callback, options?).
  // We accept and ignore both arguments. Args are underscore-prefixed so
  // strict lint rules (when re-enabled outside the tests/ folder) treat
  // them as deliberately unused.
  constructor(_callback: IntersectionObserverCallback, _options?: IntersectionObserverInit) {
    // No-op. Real implementation would store callback for later invocation.
  }

  observe(): void {
    // No-op. Real implementation would register a target for observation.
  }

  unobserve(): void {
    // No-op. Real implementation would deregister a target.
  }

  disconnect(): void {
    // No-op. Real implementation would deregister all targets.
  }

  takeRecords(): IntersectionObserverEntry[] {
    return [];
  }
}

Object.defineProperty(window, "IntersectionObserver", {
  configurable: true,
  writable: true,
  value: MockIntersectionObserver,
});

// Mirror onto globalThis because some libraries reference the global
// directly rather than via the window namespace.
Object.defineProperty(globalThis, "IntersectionObserver", {
  configurable: true,
  writable: true,
  value: MockIntersectionObserver,
});

// ---------------------------------------------------------------------------
// HTMLDialogElement <dialog> polyfill
// ---------------------------------------------------------------------------
// jsdom 25.x has incomplete native-<dialog> support: HTMLDialogElement
// exists, but .show()/.showModal()/.close() are stubs that do NOT toggle
// the `open` content attribute (and therefore do not flip the reflected
// `dialog.open` IDL property). The Modal primitive relies on the `open`
// attribute to drive its open/closed visual state and tests assert on it.
// We patch the prototype methods to set/remove the `open` attribute so
// the IDL property reflects correctly and tests can assert on the open
// state.
//
// Idempotency: a private sentinel property `__sc_patched` is defined on
// the prototype after first patch; subsequent imports of this setup file
// (which should not happen, but defensive against worker re-evaluation)
// skip re-patching.
//
// The `cancel` event for ESC-to-close is NOT polyfilled here. Tests that
// need to simulate ESC dispatch the event manually via fireEvent or
// dispatchEvent in the test body; this matches real-browser semantics.
if (typeof HTMLDialogElement !== "undefined") {
  // Cast through `Record<string, unknown>` so TypeScript permits property
  // existence checks for our private `__sc_patched` sentinel without
  // expanding the public HTMLDialogElement interface.
  const dialogProto = HTMLDialogElement.prototype as HTMLDialogElement & Record<string, unknown>;

  if (!("__sc_patched" in dialogProto)) {
    // Non-modal show: sets the `open` attribute. The `dialog.open` IDL
    // property automatically reflects this attribute, so consumers can
    // read `dialog.open === true` after .show().
    Object.defineProperty(dialogProto, "show", {
      configurable: true,
      writable: true,
      value(this: HTMLDialogElement): void {
        this.setAttribute("open", "");
      },
    });

    // Modal show: same observable side effect for our purposes (the
    // ::backdrop pseudo-element and focus trap are not modeled by jsdom
    // anyway). Setting the `open` attribute is sufficient to make
    // Modal.tsx's controlled-prop logic work in tests.
    Object.defineProperty(dialogProto, "showModal", {
      configurable: true,
      writable: true,
      value(this: HTMLDialogElement): void {
        this.setAttribute("open", "");
      },
    });

    // Close: removes the `open` attribute, dispatches the `close` event
    // (per the HTML spec), and stores the optional `returnValue`. The
    // event is non-bubbling and non-cancelable per the spec.
    Object.defineProperty(dialogProto, "close", {
      configurable: true,
      writable: true,
      value(this: HTMLDialogElement, returnValue?: string): void {
        this.removeAttribute("open");
        if (typeof returnValue === "string") {
          this.returnValue = returnValue;
        }
        const closeEvent = new Event("close", {
          bubbles: false,
          cancelable: false,
        });
        this.dispatchEvent(closeEvent);
      },
    });

    // Sentinel so re-imports of this file do not re-patch. Marked
    // non-configurable, non-writable so the sentinel itself cannot be
    // accidentally cleared by test code.
    Object.defineProperty(dialogProto, "__sc_patched", {
      configurable: false,
      writable: false,
      enumerable: false,
      value: true,
    });
  }
}

// ---------------------------------------------------------------------------
// MSW server lifecycle
// ---------------------------------------------------------------------------
// The MSW Node server (defined in ./mocks/server.ts) intercepts every
// fetch made by the SPA during tests and serves canned responses from
// ./mocks/handlers.ts. Lifecycle:
//   - beforeAll  : server.listen({ onUnhandledRequest: "error" })
//                  Any fetch to an unhandled URL fails the test
//                  immediately rather than silently falling through to
//                  the real network.
//   - afterEach  : server.resetHandlers()
//                  Per-test handler overrides via server.use(...) are
//                  cleared so they do not leak across tests.
//   - afterAll   : server.close()
//                  Tears down the request interceptor so the Node
//                  process can exit cleanly.
//
// Note: tests that bypass MSW by spying directly on globalThis.fetch
// (e.g., tests/api/client.test.ts) intercept at a higher layer and
// therefore do not hit the MSW handler chain at all; the
// `onUnhandledRequest` policy only applies to fetches that actually
// reach MSW's network layer.
beforeAll(() => {
  server.listen({ onUnhandledRequest: "error" });
});

afterEach(() => {
  server.resetHandlers();
});

afterAll(() => {
  server.close();
});

// ---------------------------------------------------------------------------
// React Testing Library cleanup
// ---------------------------------------------------------------------------
// Unmounts all rendered React component trees and removes their DOM
// nodes between tests so test A's mounted elements do not leak into
// test B's queries. RTL v16 auto-registers a cleanup hook when running
// under Vitest globals mode, so this explicit call is belt-and-
// suspenders correctness; it is idempotent if RTL has already cleaned.
afterEach(() => {
  cleanup();
});

// ---------------------------------------------------------------------------
// Module-level state reset
// ---------------------------------------------------------------------------
// Toast.tsx and correlationId.ts both maintain module-level singleton
// state (the toast queue and the cached correlation ID, respectively).
// ES modules are evaluated exactly once per worker, so this state
// persists across tests within the same worker. To guarantee per-test
// isolation we reset both stores in afterEach:
//
//   - __resetToastStoreForTesting(): clears the toast queue and resets
//     the toast id counter so a toast fired in test A is not visible
//     when test B mounts <ToastContainer />.
//
//   - resetCorrelationId(): replaces the cached correlation ID with a
//     fresh UUID v4 so two consecutive tests calling getCorrelationId()
//     observe DIFFERENT IDs (verifying the per-test isolation
//     guarantee).
afterEach(() => {
  __resetToastStoreForTesting();
  resetCorrelationId();
});
