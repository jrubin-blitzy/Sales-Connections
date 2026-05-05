/**
 * server.ts - MSW (Mock Service Worker) Node-environment server bootstrap.
 *
 * Why this file exists:
 *   The Sales-Connections SPA tests (vitest in jsdom) issue real `fetch`
 *   calls through the API client wrapper at frontend/src/api/client.ts.
 *   In a Node test environment those calls would either hit the real
 *   network (slow, flaky, dangerous) or fail outright because no backend
 *   is running. MSW intercepts every fetch at the network layer and
 *   serves canned responses defined in ./handlers.ts. This produces
 *   realistic test behavior - the SPA code remains unchanged; only the
 *   network is intercepted.
 *
 * Lifecycle (managed by frontend/tests/setup.ts):
 *   - beforeAll:   server.listen({ onUnhandledRequest: "error" })
 *                  Any fetch to a path with no matching handler causes
 *                  the test to fail immediately rather than silently
 *                  hitting the network.
 *   - afterEach:   server.resetHandlers()
 *                  Per-test handler overrides via server.use(...) are
 *                  cleared so they do not leak across tests.
 *   - afterAll:    server.close()
 *                  Tears down the request interceptor so the Node
 *                  process can exit cleanly.
 *
 * Per-test override pattern:
 *   import { server } from "../mocks/server";
 *   import { overrides } from "../mocks/handlers";
 *
 *   it("handles 500", async () => {
 *     server.use(overrides.connections.list500());
 *     // ... render and assert error state ...
 *   });
 *
 * MSW version pinning (frontend/package.json):
 *   msw@2.7.0  - v2 API (http, HttpResponse, setupServer from "msw/node").
 *
 * Why "msw/node" and not "msw/browser":
 *   vitest runs in jsdom, which is a Node environment with a DOM
 *   emulation layer (NOT a real browser). MSW's `setupServer` (Node)
 *   uses @mswjs/interceptors to patch the Node http/https/fetch
 *   globals. The browser variant relies on Service Workers, which are
 *   not available in jsdom. The Node entrypoint is the only correct
 *   choice for unit/integration tests in this project.
 *
 * Conventions per AAP Sec 0.7.7 and frontend/.prettierrc.json:
 *   - Strict TypeScript; no `any`.
 *   - Double quotes (project Prettier config: `"singleQuote": false`);
 *     trailing commas; 2-space indent; line length <= 100.
 *   - Named export only; no default export.
 *   - No emoji; no console.log.
 *   - No top-level side effects: this module MUST NOT call
 *     server.listen() at import time. Lifecycle orchestration belongs
 *     in frontend/tests/setup.ts.
 */

import { setupServer } from "msw/node";

import { handlers } from "./handlers";

/**
 * MSW Node server pre-configured with the default handler set from
 * `./handlers.ts`. Started by `frontend/tests/setup.ts` in `beforeAll`,
 * reset between tests in `afterEach`, and closed in `afterAll`.
 *
 * Tests may override individual handlers per-test via `server.use(...)`
 * and the named factory functions exported by `./handlers.ts` under the
 * `overrides` namespace. After each test, `server.resetHandlers()`
 * (invoked from `setup.ts`) restores the default handler set so
 * overrides do not leak across tests.
 *
 * The exported `server` is the canonical MSW v2 `SetupServerApi`
 * instance. Its public surface includes:
 *   - `listen(options)`       Begin intercepting requests.
 *   - `close()`               Stop intercepting and restore globals.
 *   - `resetHandlers(...)`    Reset to the initial handler set.
 *   - `use(...handlers)`      Prepend per-test handler overrides.
 *   - `events`                Life-cycle event emitter.
 *   - `boundary(callback)`    Scope handler overrides to a callback.
 */
export const server = setupServer(...handlers);
