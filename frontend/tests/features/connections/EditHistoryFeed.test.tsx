/**
 * EditHistoryFeed.test.tsx - Vitest tests for F-011 + F-013 Edit History Feed.
 *
 * Targets `frontend/src/features/connections/EditHistoryFeed.tsx`. Drives the
 * component end-to-end via MSW (Mock Service Worker) so the real
 * `useConnectionHistoryQuery` -> `apiGet` -> fetch pipeline is exercised in
 * every test - the only mocked surface is the network layer. This matches the
 * project convention established by the sibling api/feature test suites and
 * keeps the test honest: a regression in the API client, the query hook, the
 * cache key factory, or the URL construction is observable here.
 *
 * Coverage targets (per AAP Sec 0.7.7 / vite.config.ts thresholds):
 *   85% statements / 80% branches / 85% functions / 85% lines for
 *   `EditHistoryFeed.tsx`. The branches that need explicit exercising are:
 *
 *     - The 8-arm if/else cascade in HistoryEventRow's summary builder,
 *       one per audit event type (create, status_change, edit, soft_delete,
 *       hard_delete, role_change, authentication, admin_op).
 *     - All six tones in `toneToIconClasses` (success, brand, neutral,
 *       warning, danger, info) - each event type maps to a unique tone, so
 *       the 8 event-type tests above cover this transitively.
 *     - `readStringField`'s null-payload guard and non-string-value guard,
 *       both of which fall through to the "—" em-dash fallback in the
 *       status_change summary.
 *     - All seven branches of `relativeTime`: `diffMs < 0`, `seconds <= 5`,
 *       `seconds < 60`, `minutes === 1`, `minutes < 60`, `hours === 1`,
 *       `hours < 24`, `days === 1`, `days < 30`, and the absolute-date
 *       fallback.
 *     - The pagination conditional `totalPages > 1` (rendered vs not),
 *       plus `disabled` states at page 1 (Previous) and last page (Next).
 *     - Custom `pageSize` prop forwarded to the backend `page_size` query
 *       parameter.
 *     - Empty state (items.length === 0).
 *     - Error state (network/500 response).
 *     - Loading state (query pending).
 *     - The `<time>` element's `dateTime` and `title` attribute population.
 *     - The `actor_display_name ?? "Someone"` fallback when the join yields
 *       null.
 *
 * Test infrastructure:
 *   - MSW v2 (`http`, `HttpResponse`) for per-test handler overrides.
 *   - `server` from `tests/mocks/server.ts` for `server.use(...)` to inject
 *     handlers; the global lifecycle (listen/reset/close) is owned by
 *     `tests/setup.ts`.
 *   - `overrides` from `tests/mocks/handlers.ts` for the canned 500/empty
 *     scenarios; this keeps test code declarative.
 *   - `makeHistoryEntry` and `makePaginatedHistory` from `tests/mocks/data.ts`
 *     for typed payload construction with deterministic UUIDs.
 *   - `renderWithProviders` from `tests/test-utils.tsx` for the production
 *     provider stack (QueryClientProvider > AuthProvider > MemoryRouter).
 *     The component does not consume session context, but renderWithProviders
 *     is the canonical helper and simplifies adding session-aware tests
 *     later if the component evolves.
 *   - `vi.spyOn(Date, "now")` for the relative-timestamp tests so the
 *     deterministic "now" anchor does not drift with wall-clock time.
 *     This is preferred over `vi.useFakeTimers()` because fake timers
 *     interact awkwardly with MSW's pending fetch promises.
 *
 * Conventions per AAP Sec 0.7.7:
 *   - Strict TypeScript; no `any`.
 *   - Double quotes; trailing commas; 2-space indent; line length <= 100.
 *   - Named exports only (this file has no exports - it is a test module).
 *   - No emoji; no console.log.
 *   - jest-dom matchers (`toBeInTheDocument`, `toHaveAttribute`,
 *     `toHaveTextContent`, `toBeDisabled`) are globally registered via
 *     `tests/setup.ts`.
 *   - All async assertions use `findBy*` or `await waitFor(...)` to settle
 *     the TanStack Query cache.
 *
 * Coordinates with:
 *   - frontend/src/features/connections/EditHistoryFeed.tsx (component
 *     under test).
 *   - frontend/tests/mocks/server.ts (MSW Node server instance).
 *   - frontend/tests/mocks/handlers.ts (default handlers + per-test
 *     overrides namespace).
 *   - frontend/tests/mocks/data.ts (typed factory helpers).
 *   - frontend/tests/test-utils.tsx (provider-wrapping render helper plus
 *     re-exports of @testing-library/react primitives and userEvent).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";

import { EditHistoryFeed } from "@/features/connections/EditHistoryFeed";

import { server } from "../../mocks/server";
import { overrides } from "../../mocks/handlers";
import { makeHistoryEntry, makePaginatedHistory } from "../../mocks/data";
import { renderWithProviders, screen, userEvent, waitFor, within } from "../../test-utils";

// ---------------------------------------------------------------------------
// Module-scoped fixtures
// ---------------------------------------------------------------------------

/**
 * Stable record id used by every test that does not need to discriminate
 * between two records. The string is a syntactically-valid UUID so it
 * passes the schema-level z.string().uuid() validations applied to the
 * recordId in the mocked payloads.
 */
const RECORD_ID = "11111111-1111-1111-1111-111111111111";

/**
 * Anchor "now" timestamp for the deterministic relative-time tests. ISO
 * 8601 with explicit "+00:00" UTC offset to satisfy the schema-level
 * z.string().datetime({ offset: true }) validation that backs the
 * ConnectionHistoryEntry.event_timestamp field.
 */
const ANCHOR_NOW_ISO = "2026-04-01T12:00:00+00:00";
const ANCHOR_NOW_MS = new Date(ANCHOR_NOW_ISO).getTime();

/**
 * Helper that subtracts `seconds` seconds from the anchor "now" and
 * returns the resulting ISO string with explicit "+00:00" offset. The
 * trailing "Z" emitted by `toISOString()` is rewritten to "+00:00" so
 * the value passes the offset-required datetime validation.
 */
function isoSecondsBeforeAnchor(seconds: number): string {
  const ms = ANCHOR_NOW_MS - seconds * 1000;
  return new Date(ms).toISOString().replace(/Z$/, "+00:00");
}

/**
 * Helper installing a per-test MSW handler that returns the supplied
 * paginated history envelope for any GET /api/connections/:id/history
 * request. The wildcard prefix on the handler path (an asterisk
 * followed by the literal /api/connections/:id/history) matches both
 * relative URL fetches (which the SPA's API client emits when
 * VITE_API_BASE_URL is "/api") and any absolute URL the test
 * environment may construct.
 */
function setupHistory(
  entries: Parameters<typeof makePaginatedHistory>[0],
  pagination: Parameters<typeof makePaginatedHistory>[1] = {},
): void {
  const envelope = makePaginatedHistory(entries, pagination);
  server.use(http.get("*/api/connections/:id/history", () => HttpResponse.json(envelope)));
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("<EditHistoryFeed />", () => {
  // -------------------------------------------------------------------------
  // Loading state
  // -------------------------------------------------------------------------
  describe("Loading state", () => {
    it("renders the loading affordance while the history query is pending", async () => {
      // Install a pending-forever handler so the query never settles.
      // The component must render the loading state until then.
      server.use(http.get("*/api/connections/:id/history", () => new Promise(() => {})));

      renderWithProviders(<EditHistoryFeed recordId={RECORD_ID} />);

      const status = await screen.findByTestId("history-loading");
      expect(status).toBeInTheDocument();
      expect(status).toHaveAttribute("role", "status");
      expect(status).toHaveAttribute("aria-busy", "true");
      expect(status).toHaveTextContent(/Loading history/i);
    });
  });

  // -------------------------------------------------------------------------
  // Error state
  // -------------------------------------------------------------------------
  describe("Error state", () => {
    it("renders the error affordance when the history query rejects", async () => {
      server.use(overrides.connections.history500(RECORD_ID));

      renderWithProviders(<EditHistoryFeed recordId={RECORD_ID} />);

      const alert = await screen.findByTestId("history-error");
      expect(alert).toBeInTheDocument();
      expect(alert).toHaveAttribute("role", "alert");
      expect(alert).toHaveTextContent(/Failed to load history/i);
    });

    it("includes the API error message in the error affordance", async () => {
      server.use(overrides.connections.history500(RECORD_ID));

      renderWithProviders(<EditHistoryFeed recordId={RECORD_ID} />);

      // The text is "Failed to load history: <message>" where <message>
      // is the parsed envelope error message ("Internal server error"
      // from the override). Asserting on the substring keeps the test
      // resilient to whitespace and exact phrasing of the prefix.
      const alert = await screen.findByTestId("history-error");
      expect(alert.textContent ?? "").toMatch(/Internal server error/i);
    });
  });

  // -------------------------------------------------------------------------
  // Empty state
  // -------------------------------------------------------------------------
  describe("Empty state", () => {
    it("renders the empty affordance when items.length === 0", async () => {
      server.use(overrides.connections.historyEmpty(RECORD_ID));

      renderWithProviders(<EditHistoryFeed recordId={RECORD_ID} />);

      const empty = await screen.findByTestId("history-empty");
      expect(empty).toBeInTheDocument();
      expect(empty).toHaveAttribute("role", "status");
      expect(empty).toHaveTextContent(/No history events yet/i);

      // The populated container, the loading affordance, and the error
      // affordance must all be absent for the empty branch to be the
      // single visible state.
      expect(screen.queryByTestId("edit-history-feed")).not.toBeInTheDocument();
      expect(screen.queryByTestId("history-loading")).not.toBeInTheDocument();
      expect(screen.queryByTestId("history-error")).not.toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // Single event-type renders (one per AuditEventType literal)
  // -------------------------------------------------------------------------
  describe("Render - single event types", () => {
    it("renders 'create' with the Created label and copy", async () => {
      const entry = makeHistoryEntry({
        id: "68697374-0000-4000-8000-000000000c01",
        event_type: "create",
        actor_display_name: "Alice Admin",
        event_timestamp: ANCHOR_NOW_ISO,
        before_payload: null,
        after_payload: null,
      });
      setupHistory([entry], { total: 1 });

      renderWithProviders(<EditHistoryFeed recordId={RECORD_ID} />);

      await screen.findByTestId("edit-history-feed");
      const row = screen.getByTestId(`history-event-${entry.id}`);
      expect(row).toBeInTheDocument();
      expect(within(row).getByText("Created")).toBeInTheDocument();
      expect(within(row).getByText("Alice Admin")).toBeInTheDocument();
      // Surrounding-text fragment from the source's create branch.
      expect(row.textContent ?? "").toMatch(/created this connection/i);
    });

    it("renders 'edit' with the Edited label and copy", async () => {
      const entry = makeHistoryEntry({
        id: "68697374-0000-4000-8000-000000000e01",
        event_type: "edit",
        actor_display_name: "Alice Admin",
        event_timestamp: ANCHOR_NOW_ISO,
      });
      setupHistory([entry], { total: 1 });

      renderWithProviders(<EditHistoryFeed recordId={RECORD_ID} />);

      await screen.findByTestId("edit-history-feed");
      const row = screen.getByTestId(`history-event-${entry.id}`);
      expect(within(row).getByText("Edited")).toBeInTheDocument();
      expect(within(row).getByText("Alice Admin")).toBeInTheDocument();
      expect(row.textContent ?? "").toMatch(/edited this connection/i);
    });

    it("renders 'soft_delete' with the Deleted label and copy", async () => {
      const entry = makeHistoryEntry({
        id: "68697374-0000-4000-8000-000000000d01",
        event_type: "soft_delete",
        actor_display_name: "Alice Admin",
        event_timestamp: ANCHOR_NOW_ISO,
      });
      setupHistory([entry], { total: 1 });

      renderWithProviders(<EditHistoryFeed recordId={RECORD_ID} />);

      await screen.findByTestId("edit-history-feed");
      const row = screen.getByTestId(`history-event-${entry.id}`);
      expect(within(row).getByText("Deleted")).toBeInTheDocument();
      // Use a regex anchored on the copy so a future "permanently deleted"
      // copy regression cannot pass this assertion.
      expect(row.textContent ?? "").toMatch(/Alice Admin\s+deleted this connection/i);
      expect(row.textContent ?? "").not.toMatch(/permanently deleted/i);
    });

    it("renders 'hard_delete' with the Permanently deleted label and copy", async () => {
      const entry = makeHistoryEntry({
        id: "68697374-0000-4000-8000-000000000d02",
        event_type: "hard_delete",
        actor_display_name: "Alice Admin",
        event_timestamp: ANCHOR_NOW_ISO,
      });
      setupHistory([entry], { total: 1 });

      renderWithProviders(<EditHistoryFeed recordId={RECORD_ID} />);

      await screen.findByTestId("edit-history-feed");
      const row = screen.getByTestId(`history-event-${entry.id}`);
      expect(within(row).getByText("Permanently deleted")).toBeInTheDocument();
      expect(row.textContent ?? "").toMatch(/permanently deleted this connection/i);
    });

    it("renders 'role_change' with the Role changed label and copy", async () => {
      const entry = makeHistoryEntry({
        id: "68697374-0000-4000-8000-000000000r01",
        event_type: "role_change",
        actor_display_name: "Alice",
        event_timestamp: ANCHOR_NOW_ISO,
      });
      setupHistory([entry], { total: 1 });

      renderWithProviders(<EditHistoryFeed recordId={RECORD_ID} />);

      await screen.findByTestId("edit-history-feed");
      const row = screen.getByTestId(`history-event-${entry.id}`);
      expect(within(row).getByText("Role changed")).toBeInTheDocument();
      expect(row.textContent ?? "").toMatch(/Alice's role changed\./i);
    });

    it("renders 'authentication' with the Authentication label and copy", async () => {
      const entry = makeHistoryEntry({
        id: "68697374-0000-4000-8000-000000000a01",
        event_type: "authentication",
        actor_display_name: "Alice",
        event_timestamp: ANCHOR_NOW_ISO,
      });
      setupHistory([entry], { total: 1 });

      renderWithProviders(<EditHistoryFeed recordId={RECORD_ID} />);

      await screen.findByTestId("edit-history-feed");
      const row = screen.getByTestId(`history-event-${entry.id}`);
      expect(within(row).getByText("Authentication")).toBeInTheDocument();
      expect(row.textContent ?? "").toMatch(/Alice\s+authenticated\./i);
    });

    it("renders 'admin_op' with the Admin operation label and copy", async () => {
      const entry = makeHistoryEntry({
        id: "68697374-0000-4000-8000-000000000a02",
        event_type: "admin_op",
        actor_display_name: "Alice Admin",
        event_timestamp: ANCHOR_NOW_ISO,
      });
      setupHistory([entry], { total: 1 });

      renderWithProviders(<EditHistoryFeed recordId={RECORD_ID} />);

      await screen.findByTestId("edit-history-feed");
      const row = screen.getByTestId(`history-event-${entry.id}`);
      expect(within(row).getByText("Admin operation")).toBeInTheDocument();
      expect(row.textContent ?? "").toMatch(/performed an admin operation/i);
    });

    it("falls back to 'Someone' when actor_display_name is null", async () => {
      const entry = makeHistoryEntry({
        id: "68697374-0000-4000-8000-000000000n01",
        event_type: "create",
        actor_display_name: null,
        event_timestamp: ANCHOR_NOW_ISO,
      });
      setupHistory([entry], { total: 1 });

      renderWithProviders(<EditHistoryFeed recordId={RECORD_ID} />);

      await screen.findByTestId("edit-history-feed");
      const row = screen.getByTestId(`history-event-${entry.id}`);
      // The source uses `actor_display_name ?? "Someone"` so a null
      // actor renders as the literal string "Someone".
      expect(within(row).getByText("Someone")).toBeInTheDocument();
      expect(row.textContent ?? "").toMatch(/Someone created this connection/i);
    });

    it("sets aria-label on each row to summarize the event for screen readers", async () => {
      const entry = makeHistoryEntry({
        id: "68697374-0000-4000-8000-000000000ar1",
        event_type: "edit",
        actor_display_name: "Alice Admin",
        event_timestamp: ANCHOR_NOW_ISO,
      });
      setupHistory([entry], { total: 1 });

      renderWithProviders(<EditHistoryFeed recordId={RECORD_ID} />);

      await screen.findByTestId("edit-history-feed");
      const row = screen.getByTestId(`history-event-${entry.id}`);
      // The aria-label encodes the EVENT_TYPE_META.label, the actor,
      // and a relative-time fragment (e.g., "Edited by Alice Admin,
      // just now"). Asserting on the static prefix keeps the test
      // independent of the exact relative-time string at this anchor.
      expect(row.getAttribute("aria-label") ?? "").toMatch(/^Edited by Alice Admin/);
    });
  });

  // -------------------------------------------------------------------------
  // Status change with before/after badges
  // -------------------------------------------------------------------------
  describe("Render - status_change with before/after", () => {
    it("renders before/after status badges for status_change events", async () => {
      const entry = makeHistoryEntry({
        id: "68697374-0000-4000-8000-000000000sc1",
        event_type: "status_change",
        actor_display_name: "Bob Sales",
        event_timestamp: ANCHOR_NOW_ISO,
        before_payload: { outreach_status: "Not Started" },
        after_payload: { outreach_status: "In Progress" },
      });
      setupHistory([entry], { total: 1 });

      renderWithProviders(<EditHistoryFeed recordId={RECORD_ID} />);

      await screen.findByTestId("edit-history-feed");
      const row = screen.getByTestId(`history-event-${entry.id}`);

      // The "Status changed" header label.
      expect(within(row).getByText("Status changed")).toBeInTheDocument();
      // The actor name, scoped to the row.
      expect(within(row).getByText("Bob Sales")).toBeInTheDocument();
      // The connecting copy.
      expect(row.textContent ?? "").toMatch(/changed status from/i);
      // The two value badges, both within the row's scope so we do
      // not collide with any other status occurrences elsewhere.
      expect(within(row).getByText("Not Started")).toBeInTheDocument();
      expect(within(row).getByText("In Progress")).toBeInTheDocument();
    });

    it("falls back to em-dash when before/after payloads are null", async () => {
      const entry = makeHistoryEntry({
        id: "68697374-0000-4000-8000-000000000sc2",
        event_type: "status_change",
        actor_display_name: "Bob Sales",
        event_timestamp: ANCHOR_NOW_ISO,
        before_payload: null,
        after_payload: null,
      });
      setupHistory([entry], { total: 1 });

      renderWithProviders(<EditHistoryFeed recordId={RECORD_ID} />);

      await screen.findByTestId("edit-history-feed");
      const row = screen.getByTestId(`history-event-${entry.id}`);

      // The source uses "—" (U+2014) as the fallback when readStringField
      // returns null. Two separate badges should both render the em-dash;
      // getAllByText returns both for the row scope.
      const dashes = within(row).getAllByText("\u2014");
      expect(dashes).toHaveLength(2);
    });

    it("falls back to em-dash when outreach_status is not a string", async () => {
      // before_payload has a numeric value at the outreach_status key;
      // after_payload has an object value. readStringField must reject
      // both as not-a-string and return null, so the surrounding "—"
      // fallback fires.
      const entry = makeHistoryEntry({
        id: "68697374-0000-4000-8000-000000000sc3",
        event_type: "status_change",
        actor_display_name: "Bob Sales",
        event_timestamp: ANCHOR_NOW_ISO,
        before_payload: { outreach_status: 123 },
        after_payload: { outreach_status: { nested: "wrong-shape" } },
      });
      setupHistory([entry], { total: 1 });

      renderWithProviders(<EditHistoryFeed recordId={RECORD_ID} />);

      await screen.findByTestId("edit-history-feed");
      const row = screen.getByTestId(`history-event-${entry.id}`);
      const dashes = within(row).getAllByText("\u2014");
      expect(dashes).toHaveLength(2);

      // Belt-and-suspenders: the malformed values must NEVER appear in
      // the rendered output - that is the entire point of the
      // type-narrowing fallback.
      expect(row.textContent ?? "").not.toMatch(/\b123\b/);
      expect(row.textContent ?? "").not.toMatch(/wrong-shape/);
    });

    it("falls back to em-dash when outreach_status key is absent from payload", async () => {
      // payload is a non-null dict but lacks the outreach_status key
      // entirely. readStringField hits the `payload[key] === undefined`
      // case and returns null, exercising the fallback once more.
      const entry = makeHistoryEntry({
        id: "68697374-0000-4000-8000-000000000sc4",
        event_type: "status_change",
        actor_display_name: "Bob Sales",
        event_timestamp: ANCHOR_NOW_ISO,
        before_payload: { other_field: "something" },
        after_payload: { other_field: "something_else" },
      });
      setupHistory([entry], { total: 1 });

      renderWithProviders(<EditHistoryFeed recordId={RECORD_ID} />);

      await screen.findByTestId("edit-history-feed");
      const row = screen.getByTestId(`history-event-${entry.id}`);
      const dashes = within(row).getAllByText("\u2014");
      expect(dashes).toHaveLength(2);
    });
  });

  // -------------------------------------------------------------------------
  // Relative timestamps
  // -------------------------------------------------------------------------
  //
  // These tests pin Date.now() to a deterministic anchor so the relative
  // time branches are exercised against a stable "now". Mocking Date.now
  // (rather than vi.useFakeTimers) avoids pulling MSW's internal
  // setTimeout-based fetch resolution into fake-timer mode.
  describe("Render - relative timestamps", () => {
    beforeEach(() => {
      vi.spyOn(Date, "now").mockReturnValue(ANCHOR_NOW_MS);
    });

    afterEach(() => {
      vi.restoreAllMocks();
    });

    it("renders 'just now' when the event happened 5 seconds ago (boundary)", async () => {
      const entry = makeHistoryEntry({
        id: "68697374-0000-4000-8000-000000000t01",
        event_type: "edit",
        event_timestamp: isoSecondsBeforeAnchor(5),
      });
      setupHistory([entry], { total: 1 });

      renderWithProviders(<EditHistoryFeed recordId={RECORD_ID} />);

      const row = await screen.findByTestId(`history-event-${entry.id}`);
      expect(within(row).getByText(/just now/i)).toBeInTheDocument();
    });

    it("renders '30 seconds ago' for a timestamp inside the seconds window", async () => {
      const entry = makeHistoryEntry({
        id: "68697374-0000-4000-8000-000000000t02",
        event_type: "edit",
        event_timestamp: isoSecondsBeforeAnchor(30),
      });
      setupHistory([entry], { total: 1 });

      renderWithProviders(<EditHistoryFeed recordId={RECORD_ID} />);

      const row = await screen.findByTestId(`history-event-${entry.id}`);
      expect(within(row).getByText(/30 seconds ago/i)).toBeInTheDocument();
    });

    it("renders '1 minute ago' (singular) for a 60-second-old timestamp", async () => {
      const entry = makeHistoryEntry({
        id: "68697374-0000-4000-8000-000000000t03",
        event_type: "edit",
        event_timestamp: isoSecondsBeforeAnchor(60),
      });
      setupHistory([entry], { total: 1 });

      renderWithProviders(<EditHistoryFeed recordId={RECORD_ID} />);

      const row = await screen.findByTestId(`history-event-${entry.id}`);
      // Anchor on the exact "1 minute ago" string so a plural-form
      // regression ("1 minutes ago") would fail this test.
      expect(within(row).getByText("1 minute ago")).toBeInTheDocument();
    });

    it("renders '30 minutes ago' (plural) inside the minutes window", async () => {
      const entry = makeHistoryEntry({
        id: "68697374-0000-4000-8000-000000000t04",
        event_type: "edit",
        event_timestamp: isoSecondsBeforeAnchor(30 * 60),
      });
      setupHistory([entry], { total: 1 });

      renderWithProviders(<EditHistoryFeed recordId={RECORD_ID} />);

      const row = await screen.findByTestId(`history-event-${entry.id}`);
      expect(within(row).getByText(/30 minutes ago/i)).toBeInTheDocument();
    });

    it("renders '1 hour ago' (singular) for a 60-minute-old timestamp", async () => {
      const entry = makeHistoryEntry({
        id: "68697374-0000-4000-8000-000000000t05",
        event_type: "edit",
        event_timestamp: isoSecondsBeforeAnchor(60 * 60),
      });
      setupHistory([entry], { total: 1 });

      renderWithProviders(<EditHistoryFeed recordId={RECORD_ID} />);

      const row = await screen.findByTestId(`history-event-${entry.id}`);
      expect(within(row).getByText("1 hour ago")).toBeInTheDocument();
    });

    it("renders '2 hours ago' (plural) inside the hours window", async () => {
      const entry = makeHistoryEntry({
        id: "68697374-0000-4000-8000-000000000t06",
        event_type: "edit",
        event_timestamp: isoSecondsBeforeAnchor(2 * 60 * 60),
      });
      setupHistory([entry], { total: 1 });

      renderWithProviders(<EditHistoryFeed recordId={RECORD_ID} />);

      const row = await screen.findByTestId(`history-event-${entry.id}`);
      expect(within(row).getByText(/2 hours ago/i)).toBeInTheDocument();
    });

    it("renders '1 day ago' (singular) for a 24-hour-old timestamp", async () => {
      const entry = makeHistoryEntry({
        id: "68697374-0000-4000-8000-000000000t07",
        event_type: "edit",
        event_timestamp: isoSecondsBeforeAnchor(24 * 60 * 60),
      });
      setupHistory([entry], { total: 1 });

      renderWithProviders(<EditHistoryFeed recordId={RECORD_ID} />);

      const row = await screen.findByTestId(`history-event-${entry.id}`);
      expect(within(row).getByText("1 day ago")).toBeInTheDocument();
    });

    it("renders 'N days ago' (plural) inside the days window", async () => {
      const entry = makeHistoryEntry({
        id: "68697374-0000-4000-8000-000000000t08",
        event_type: "edit",
        event_timestamp: isoSecondsBeforeAnchor(3 * 24 * 60 * 60),
      });
      setupHistory([entry], { total: 1 });

      renderWithProviders(<EditHistoryFeed recordId={RECORD_ID} />);

      const row = await screen.findByTestId(`history-event-${entry.id}`);
      expect(within(row).getByText(/3 days ago/i)).toBeInTheDocument();
    });

    it("falls through to an absolute date for events older than 30 days", async () => {
      const thirtyOneDaysSeconds = 31 * 24 * 60 * 60;
      const entry = makeHistoryEntry({
        id: "68697374-0000-4000-8000-000000000t09",
        event_type: "edit",
        event_timestamp: isoSecondsBeforeAnchor(thirtyOneDaysSeconds),
      });
      setupHistory([entry], { total: 1 });

      renderWithProviders(<EditHistoryFeed recordId={RECORD_ID} />);

      const row = await screen.findByTestId(`history-event-${entry.id}`);
      // The visible text is the locale-formatted absolute date. The
      // exact text depends on the test runner's locale, but the
      // year (2026) should always appear and the relative phrases
      // ("seconds ago", "minutes ago", "hours ago", "days ago",
      // "just now") must not.
      const time = row.querySelector("time");
      expect(time).not.toBeNull();
      expect(time?.textContent ?? "").toMatch(/2026/);
      expect(time?.textContent ?? "").not.toMatch(/just now/i);
      expect(time?.textContent ?? "").not.toMatch(/seconds ago|minutes ago|hours ago|days ago/i);
    });

    it("renders 'just now' when the event timestamp is in the future (clock drift)", async () => {
      // 1-hour-future timestamp simulates a clock-drift edge case. The
      // source returns "just now" for any negative diff to avoid
      // misleading "in N seconds" copy.
      const ms = ANCHOR_NOW_MS + 60 * 60 * 1000;
      const entry = makeHistoryEntry({
        id: "68697374-0000-4000-8000-000000000t10",
        event_type: "edit",
        event_timestamp: new Date(ms).toISOString().replace(/Z$/, "+00:00"),
      });
      setupHistory([entry], { total: 1 });

      renderWithProviders(<EditHistoryFeed recordId={RECORD_ID} />);

      const row = await screen.findByTestId(`history-event-${entry.id}`);
      expect(within(row).getByText(/just now/i)).toBeInTheDocument();
    });

    it("populates the <time> dateTime and title attributes from the entry timestamp", async () => {
      const ts = isoSecondsBeforeAnchor(30);
      const entry = makeHistoryEntry({
        id: "68697374-0000-4000-8000-000000000t11",
        event_type: "edit",
        event_timestamp: ts,
      });
      setupHistory([entry], { total: 1 });

      renderWithProviders(<EditHistoryFeed recordId={RECORD_ID} />);

      const row = await screen.findByTestId(`history-event-${entry.id}`);
      const time = row.querySelector("time");
      expect(time).not.toBeNull();
      // dateTime is the raw ISO 8601 input - the standard machine-
      // readable form preserved verbatim for screen readers and
      // calendar copy-paste.
      expect(time).toHaveAttribute("dateTime", ts);
      // title is the formatTimestamp absolute output - locale-formatted
      // and non-empty. We do not assert on the exact contents (locale-
      // dependent) but require it to be a non-empty string.
      const title = time?.getAttribute("title") ?? "";
      expect(title.length).toBeGreaterThan(0);
    });
  });

  // -------------------------------------------------------------------------
  // Pagination
  // -------------------------------------------------------------------------
  describe("Pagination", () => {
    it("does not render pagination when totalPages === 1", async () => {
      // 5 items / page_size 25 = 1 total page; pagination must be hidden.
      const entries = Array.from({ length: 5 }, (_, i) =>
        makeHistoryEntry({
          id: `68697374-0000-4000-8000-0000000000p${i}`,
          event_type: "edit",
          event_timestamp: ANCHOR_NOW_ISO,
        }),
      );
      setupHistory(entries, { total: 5, page: 1, page_size: 25 });

      renderWithProviders(<EditHistoryFeed recordId={RECORD_ID} />);

      await screen.findByTestId("edit-history-feed");
      expect(screen.queryByTestId("history-pagination")).not.toBeInTheDocument();
      expect(screen.queryByTestId("history-page-prev")).not.toBeInTheDocument();
      expect(screen.queryByTestId("history-page-next")).not.toBeInTheDocument();
    });

    it("renders pagination when totalPages > 1 with the correct indicator copy", async () => {
      // 60 items / page_size 25 = 3 total pages.
      const entries = Array.from({ length: 25 }, (_, i) =>
        makeHistoryEntry({
          id: `68697374-0000-4000-8000-0000000000q${i.toString(16)}`,
          event_type: "edit",
          event_timestamp: ANCHOR_NOW_ISO,
        }),
      );
      setupHistory(entries, { total: 60, page: 1, page_size: 25 });

      renderWithProviders(<EditHistoryFeed recordId={RECORD_ID} />);

      await screen.findByTestId("edit-history-feed");

      const nav = screen.getByTestId("history-pagination");
      expect(nav).toBeInTheDocument();
      expect(nav).toHaveAttribute("aria-label", "History pagination");

      const indicator = screen.getByTestId("history-page-indicator");
      expect(indicator).toHaveTextContent("Page 1 of 3");

      expect(screen.getByTestId("history-page-prev")).toBeInTheDocument();
      expect(screen.getByTestId("history-page-next")).toBeInTheDocument();
    });

    it("disables the Previous button at page 1", async () => {
      const entries = Array.from({ length: 25 }, (_, i) =>
        makeHistoryEntry({
          id: `68697374-0000-4000-8000-0000000000r${i.toString(16)}`,
          event_type: "edit",
          event_timestamp: ANCHOR_NOW_ISO,
        }),
      );
      setupHistory(entries, { total: 60, page: 1, page_size: 25 });

      renderWithProviders(<EditHistoryFeed recordId={RECORD_ID} />);

      await screen.findByTestId("edit-history-feed");
      const prev = screen.getByTestId("history-page-prev");
      expect(prev).toBeDisabled();
      // The Next button must be enabled - we are not at the last page.
      expect(screen.getByTestId("history-page-next")).not.toBeDisabled();
    });

    it("clicking Next requests page 2 and updates the indicator", async () => {
      const user = userEvent.setup();

      // Capture the request URL on each call so we can verify the page
      // query parameter advances. Map `page=N` from the URL to a fresh
      // 25-item payload echoed back as page N of 3.
      const capturedUrls: string[] = [];
      server.use(
        http.get("*/api/connections/:id/history", ({ request }) => {
          capturedUrls.push(request.url);
          const u = new URL(request.url);
          const page = Number(u.searchParams.get("page") ?? "1");
          const pageSize = Number(u.searchParams.get("page_size") ?? "25");
          const items = Array.from({ length: 25 }, (_, i) =>
            makeHistoryEntry({
              id: `68697374-0000-4000-8000-0000000${page}${i.toString(16).padStart(3, "0")}`,
              event_type: "edit",
              event_timestamp: ANCHOR_NOW_ISO,
            }),
          );
          return HttpResponse.json(
            makePaginatedHistory(items, { total: 60, page, page_size: pageSize }),
          );
        }),
      );

      renderWithProviders(<EditHistoryFeed recordId={RECORD_ID} />);

      await screen.findByTestId("edit-history-feed");
      expect(screen.getByTestId("history-page-indicator")).toHaveTextContent("Page 1 of 3");

      await user.click(screen.getByTestId("history-page-next"));

      // After the click, TanStack Query swaps to the new query key
      // (page=2) and re-fetches; the indicator must update to "Page 2
      // of 3" once the new data arrives.
      await waitFor(() => {
        expect(screen.getByTestId("history-page-indicator")).toHaveTextContent("Page 2 of 3");
      });

      // Verify at least one captured URL contains "page=2" - the
      // request fired with the advanced page argument.
      const sawPageTwo = capturedUrls.some((u) => u.includes("page=2"));
      expect(sawPageTwo).toBe(true);
    });

    it("disables the Next button at the last page", async () => {
      const user = userEvent.setup();

      // Advance the component all the way to page 3 of 3 by clicking
      // Next twice (1 -> 2 -> 3).
      server.use(
        http.get("*/api/connections/:id/history", ({ request }) => {
          const u = new URL(request.url);
          const page = Number(u.searchParams.get("page") ?? "1");
          const pageSize = Number(u.searchParams.get("page_size") ?? "25");
          const items = Array.from({ length: 25 }, (_, i) =>
            makeHistoryEntry({
              id: `68697374-0000-4000-8000-0000000${page}${i.toString(16).padStart(3, "0")}`,
              event_type: "edit",
              event_timestamp: ANCHOR_NOW_ISO,
            }),
          );
          return HttpResponse.json(
            makePaginatedHistory(items, { total: 60, page, page_size: pageSize }),
          );
        }),
      );

      renderWithProviders(<EditHistoryFeed recordId={RECORD_ID} />);

      await screen.findByTestId("edit-history-feed");
      await user.click(screen.getByTestId("history-page-next"));
      await waitFor(() => {
        expect(screen.getByTestId("history-page-indicator")).toHaveTextContent("Page 2 of 3");
      });
      await user.click(screen.getByTestId("history-page-next"));
      await waitFor(() => {
        expect(screen.getByTestId("history-page-indicator")).toHaveTextContent("Page 3 of 3");
      });

      // Page 3 of 3 is the last page; Next must be disabled, Previous
      // must be enabled.
      expect(screen.getByTestId("history-page-next")).toBeDisabled();
      expect(screen.getByTestId("history-page-prev")).not.toBeDisabled();
    });

    it("clicking Previous from page 2 returns to page 1", async () => {
      const user = userEvent.setup();

      server.use(
        http.get("*/api/connections/:id/history", ({ request }) => {
          const u = new URL(request.url);
          const page = Number(u.searchParams.get("page") ?? "1");
          const pageSize = Number(u.searchParams.get("page_size") ?? "25");
          const items = Array.from({ length: 25 }, (_, i) =>
            makeHistoryEntry({
              id: `68697374-0000-4000-8000-0000000${page}${i.toString(16).padStart(3, "0")}`,
              event_type: "edit",
              event_timestamp: ANCHOR_NOW_ISO,
            }),
          );
          return HttpResponse.json(
            makePaginatedHistory(items, { total: 60, page, page_size: pageSize }),
          );
        }),
      );

      renderWithProviders(<EditHistoryFeed recordId={RECORD_ID} />);

      await screen.findByTestId("edit-history-feed");
      await user.click(screen.getByTestId("history-page-next"));
      await waitFor(() => {
        expect(screen.getByTestId("history-page-indicator")).toHaveTextContent("Page 2 of 3");
      });

      // Now click Previous; the indicator should return to page 1.
      await user.click(screen.getByTestId("history-page-prev"));
      await waitFor(() => {
        expect(screen.getByTestId("history-page-indicator")).toHaveTextContent("Page 1 of 3");
      });
      expect(screen.getByTestId("history-page-prev")).toBeDisabled();
    });
  });

  // -------------------------------------------------------------------------
  // Custom pageSize prop
  // -------------------------------------------------------------------------
  describe("Custom pageSize prop", () => {
    it("forwards pageSize=10 to the backend as page_size=10", async () => {
      let capturedUrl = "";
      server.use(
        http.get("*/api/connections/:id/history", ({ request }) => {
          capturedUrl = request.url;
          // Server echoes back a response sized to the requested
          // page_size to make the response a faithful round-trip.
          const items = Array.from({ length: 10 }, (_, i) =>
            makeHistoryEntry({
              id: `68697374-0000-4000-8000-0000000000s${i.toString(16)}`,
              event_type: "edit",
              event_timestamp: ANCHOR_NOW_ISO,
            }),
          );
          return HttpResponse.json(
            makePaginatedHistory(items, { total: 50, page: 1, page_size: 10 }),
          );
        }),
      );

      renderWithProviders(<EditHistoryFeed recordId={RECORD_ID} pageSize={10} />);

      await screen.findByTestId("edit-history-feed");
      // The captured URL must encode page_size=10 (the prop) - this
      // is the assertion that proves the prop reaches the backend.
      expect(capturedUrl).toContain("page_size=10");
    });

    it("uses page_size=25 (default) when no pageSize prop is provided", async () => {
      let capturedUrl = "";
      server.use(
        http.get("*/api/connections/:id/history", ({ request }) => {
          capturedUrl = request.url;
          const items = [
            makeHistoryEntry({
              id: "68697374-0000-4000-8000-000000000def",
              event_type: "edit",
              event_timestamp: ANCHOR_NOW_ISO,
            }),
          ];
          return HttpResponse.json(
            makePaginatedHistory(items, { total: 1, page: 1, page_size: 25 }),
          );
        }),
      );

      renderWithProviders(<EditHistoryFeed recordId={RECORD_ID} />);

      await screen.findByTestId("edit-history-feed");
      expect(capturedUrl).toContain("page_size=25");
    });

    it("computes totalPages from the server-reported total and page_size (custom pageSize)", async () => {
      // 50 items / page_size 10 = 5 total pages. With a custom pageSize
      // prop forwarded to the backend, the totalPages calculation must
      // honour the SERVER-reported page_size in the response (not the
      // initialPageSize prop) so a server that clamps the page_size
      // still drives a correct UI.
      const entries = Array.from({ length: 10 }, (_, i) =>
        makeHistoryEntry({
          id: `68697374-0000-4000-8000-000000000ps${i.toString(16)}`,
          event_type: "edit",
          event_timestamp: ANCHOR_NOW_ISO,
        }),
      );
      setupHistory(entries, { total: 50, page: 1, page_size: 10 });

      renderWithProviders(<EditHistoryFeed recordId={RECORD_ID} pageSize={10} />);

      await screen.findByTestId("edit-history-feed");
      expect(screen.getByTestId("history-page-indicator")).toHaveTextContent("Page 1 of 5");
    });
  });

  // -------------------------------------------------------------------------
  // Defensive payload reads (additional coverage for readStringField)
  // -------------------------------------------------------------------------
  describe("Defensive payload reads", () => {
    it("renders correctly when before_payload is non-null but after_payload is null", async () => {
      // Mixed payloads: only `before` carries the field. The "before"
      // badge gets the value; "after" falls back to the em-dash.
      const entry = makeHistoryEntry({
        id: "68697374-0000-4000-8000-000000000df1",
        event_type: "status_change",
        actor_display_name: "Bob Sales",
        event_timestamp: ANCHOR_NOW_ISO,
        before_payload: { outreach_status: "Not Started" },
        after_payload: null,
      });
      setupHistory([entry], { total: 1 });

      renderWithProviders(<EditHistoryFeed recordId={RECORD_ID} />);

      await screen.findByTestId("edit-history-feed");
      const row = screen.getByTestId(`history-event-${entry.id}`);
      expect(within(row).getByText("Not Started")).toBeInTheDocument();
      // Exactly one em-dash for the missing `after` value.
      expect(within(row).getAllByText("\u2014")).toHaveLength(1);
    });

    it("renders correctly when after_payload is non-null but before_payload is null", async () => {
      const entry = makeHistoryEntry({
        id: "68697374-0000-4000-8000-000000000df2",
        event_type: "status_change",
        actor_display_name: "Bob Sales",
        event_timestamp: ANCHOR_NOW_ISO,
        before_payload: null,
        after_payload: { outreach_status: "In Progress" },
      });
      setupHistory([entry], { total: 1 });

      renderWithProviders(<EditHistoryFeed recordId={RECORD_ID} />);

      await screen.findByTestId("edit-history-feed");
      const row = screen.getByTestId(`history-event-${entry.id}`);
      expect(within(row).getByText("In Progress")).toBeInTheDocument();
      expect(within(row).getAllByText("\u2014")).toHaveLength(1);
    });

    it("renders multiple entries in the order returned by the server", async () => {
      // Verify the full list pipeline: every entry from the response
      // appears as its own <li> in declared order. Useful for catching
      // regressions where the source might accidentally key on
      // event_type and dedupe rows.
      const entries = [
        makeHistoryEntry({
          id: "68697374-0000-4000-8000-000000000ord1",
          event_type: "create",
          actor_display_name: "Alice",
          event_timestamp: ANCHOR_NOW_ISO,
        }),
        makeHistoryEntry({
          id: "68697374-0000-4000-8000-000000000ord2",
          event_type: "edit",
          actor_display_name: "Bob",
          event_timestamp: ANCHOR_NOW_ISO,
        }),
        makeHistoryEntry({
          id: "68697374-0000-4000-8000-000000000ord3",
          event_type: "status_change",
          actor_display_name: "Carol",
          event_timestamp: ANCHOR_NOW_ISO,
          before_payload: { outreach_status: "Not Started" },
          after_payload: { outreach_status: "In Progress" },
        }),
      ];
      setupHistory(entries, { total: 3 });

      renderWithProviders(<EditHistoryFeed recordId={RECORD_ID} />);

      await screen.findByTestId("edit-history-feed");
      // All three rows must be in the DOM, in declared order.
      const rows = screen.getAllByRole("listitem");
      expect(rows).toHaveLength(3);
      expect(rows[0]).toHaveAttribute("data-testid", "history-event-" + entries[0]!.id);
      expect(rows[1]).toHaveAttribute("data-testid", "history-event-" + entries[1]!.id);
      expect(rows[2]).toHaveAttribute("data-testid", "history-event-" + entries[2]!.id);
    });
  });
});
