/**
 * Analytics.test.tsx - Vitest tests for the F-014 Admin Analytics view.
 *
 * Targets `frontend/src/features/admin/Analytics.tsx`, which:
 *   - Calls useAnalyticsQuery() (GET /api/admin/analytics, 5-minute staleTime)
 *   - Renders three sub-panels: MostActiveContributorsPanel,
 *     LeadsByStatusPanel, WeeklyActivitySparkline
 *   - Re-orders leads_by_status to canonical OUTREACH_STATUS_VALUES order
 *   - Defaults missing status entries to count=0
 *   - Renders SVG sparkline with role="img" and aria-label
 *   - Shows generated_at timestamp
 *   - Has loading/error/success states with retry on error
 *
 * Test scope (per the assigned folder requirements):
 *   1. Loading state: analytics-loading with 3 skeleton panels.
 *   2. Error state: analytics-error, analytics-error-message,
 *      analytics-retry-button; click retry triggers refetch.
 *   3. Success: 3 panels render (contributors, leads, activity).
 *   4. MostActiveContributorsPanel:
 *      - Renders rows with display_name and record_count
 *      - Empty state: analytics-contributors-empty
 *      - Counts use tabular-nums for stable digit alignment
 *   5. LeadsByStatusPanel:
 *      - List analytics-leads-list re-orders to canonical
 *        OUTREACH_STATUS_VALUES order even when backend response is shuffled.
 *      - Missing entries default to count=0.
 *      - Total analytics-leads-total computed correctly.
 *      - All-zero counts handled gracefully.
 *   6. WeeklyActivitySparkline:
 *      - Empty state: analytics-activity-empty
 *      - Rendered SVG with role="img" and aria-label
 *      - Summary analytics-activity-summary
 *      - All-zero counts edge case (dashed baseline)
 *      - Single-week edge case
 *   7. analytics-generated-at timestamp shown on success.
 *   8. Accessibility: aria-labelledby on root <main> and each sub-panel.
 *
 * Mocking strategy:
 *   - Use renderWithMockedSession with an Admin session so the route-level
 *     RoleGate is satisfied (defense-in-depth) without needing MSW for /api/me.
 *   - MSW handles GET /api/admin/analytics; per-test overrides via
 *     server.use(overrides.admin.analytics500()) for failure scenarios.
 *   - Factory functions from mocks/data.ts produce typed stub data.
 *
 * Coordinates with:
 *   - frontend/src/features/admin/Analytics.tsx     Component under test
 *   - frontend/src/api/admin.ts                     useAnalyticsQuery hook
 *   - frontend/src/schemas/admin.ts                 AnalyticsResponse types
 *   - frontend/src/schemas/connection.ts            OUTREACH_STATUS_VALUES
 *   - frontend/tests/test-utils.tsx                 renderWithMockedSession
 *   - frontend/tests/mocks/server.ts                MSW server instance
 *   - frontend/tests/mocks/handlers.ts              `overrides` factory
 *   - frontend/tests/mocks/data.ts                  Factory functions
 *   - frontend/tests/setup.ts                       Global MSW lifecycle
 *
 * Conventions per AAP Sec 0.7.7 and frontend/.prettierrc.json:
 *   - Strict TypeScript; no `any`.
 *   - Double quotes (singleQuote: false); trailing commas; 2-space indent;
 *     line length <= 100.
 *   - Named role values: 'Admin' / 'Contributor' / 'Viewer' (NEVER
 *     'Sales Rep'); SessionRead uses the nested {user: UserRead, ...}
 *     shape per the backend pydantic schema.
 *   - No emoji; no console.log.
 *   - All interactions use @testing-library/user-event (never fireEvent).
 */

import { describe, it, expect, beforeEach, vi } from "vitest";
import { http, HttpResponse, delay } from "msw";

import { Analytics } from "@/features/admin/Analytics";
import { OUTREACH_STATUS_VALUES } from "@/schemas/connection";

import { server } from "../../mocks/server";
import { overrides } from "../../mocks/handlers";
import {
  makeAnalyticsResponse,
  makeContributorActivity,
  makeLeadsByStatusEntry,
  makeSessionRead,
  makeUserRead,
  makeWeeklyActivityEntry,
} from "../../mocks/data";
import { renderWithMockedSession, screen, userEvent, waitFor, within } from "../../test-utils";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Build a SessionRead for an Admin user. Used by every renderWithMockedSession
 * call in this test file because Analytics is route-gated by
 * `<RoleGate role="Admin">` per the production router. Even though the
 * Analytics component itself does NOT call useSession()/useRole() (it only
 * calls useAnalyticsQuery), supplying an Admin session keeps test parity with
 * the other admin-tab tests and prevents any future defensive RoleGate
 * wrapping inside Analytics from blocking assertions.
 *
 * The MockAuthProvider's value reads ctx.session?.user.role - the nested
 * shape matches frontend/src/schemas/auth.ts (Session = { user: UserRead,
 * authenticated: boolean }). Returning the typed factory output keeps the
 * test file's session shape in lockstep with the backend SessionRead
 * pydantic schema.
 */
function buildAdminSession() {
  return makeSessionRead({
    user: makeUserRead({ role: "Admin", display_name: "Admin User" }),
    authenticated: true,
  });
}

/**
 * Token reference to silence ESLint for the explicitly-imported but
 * sometimes-unused `vi` and `beforeEach` symbols. The test file's external
 * imports surface is mandated by the file specification (Phase 2 imports);
 * keeping these tokens referenced here documents the intent without forcing
 * a per-call usage in every test. ESLint disables `no-unused-vars` inside
 * the tests/ glob (per frontend/eslint.config.js), so this `void` no-op is
 * defensive against future config changes.
 */
void vi;
void beforeEach;

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("<Analytics />", () => {
  // -------------------------------------------------------------------------
  // Loading state
  // -------------------------------------------------------------------------

  describe("loading state", () => {
    it("renders the skeleton panels while fetching analytics", async () => {
      // Slow the response by ~200ms so the test can observe the
      // synchronous isPending=true render before MSW resolves the
      // analytics handler. Without the delay the handler resolves
      // immediately on the next microtask and the loading state may
      // not be visible by the time the assertion runs.
      server.use(
        http.get("/api/admin/analytics", async () => {
          await delay(200);
          return HttpResponse.json(makeAnalyticsResponse());
        }),
      );

      renderWithMockedSession(<Analytics />, buildAdminSession());

      // Loading skeleton appears immediately (synchronous render before
      // the fetch resolves).
      const loadingRoot = screen.getByTestId("analytics-loading");
      expect(loadingRoot).toBeInTheDocument();

      // The skeleton renders three placeholder panels per the source
      // implementation (`[0, 1, 2].map(...)`). Each child is a
      // <div aria-hidden="true">. We query for those and assert the
      // count is exactly 3 - a regression here would mean the loading
      // state no longer matches the loaded layout.
      const skeletons = loadingRoot.querySelectorAll('[aria-hidden="true"]');
      expect(skeletons).toHaveLength(3);

      // No success panels yet - they are rendered only when query.isSuccess.
      expect(screen.queryByTestId("analytics-contributors-panel")).not.toBeInTheDocument();
      expect(screen.queryByTestId("analytics-leads-panel")).not.toBeInTheDocument();
      expect(screen.queryByTestId("analytics-activity-panel")).not.toBeInTheDocument();

      // Wait for the loading state to be replaced by success content.
      // The bounded timeout protects against a stuck pending state.
      await waitFor(
        () => {
          expect(screen.queryByTestId("analytics-loading")).not.toBeInTheDocument();
        },
        { timeout: 2000 },
      );

      // After the query resolves, the success panels are rendered.
      expect(screen.getByTestId("analytics-contributors-panel")).toBeInTheDocument();
    });

    it("renders the heading and aria-labelledby root regardless of state", () => {
      renderWithMockedSession(<Analytics />, buildAdminSession());

      // The <main> root element with aria-labelledby is always rendered,
      // independent of query state. This guarantees screen-reader users
      // can locate the analytics surface even during the pending state.
      expect(screen.getByTestId("admin-analytics")).toHaveAttribute(
        "aria-labelledby",
        "admin-analytics-heading",
      );

      // The h2 heading paired with the aria-labelledby pointer.
      const heading = screen.getByRole("heading", { level: 2, name: /analytics/i });
      expect(heading).toHaveAttribute("id", "admin-analytics-heading");
      expect(heading).toHaveTextContent(/^Analytics$/);
    });
  });

  // -------------------------------------------------------------------------
  // Error state + retry
  // -------------------------------------------------------------------------

  describe("error state", () => {
    it("renders the error UI on a 500 response with retry button", async () => {
      // Replace the default 200 handler with the pre-built 500-error
      // factory so the error envelope shape matches production.
      server.use(overrides.admin.analytics500());

      renderWithMockedSession(<Analytics />, buildAdminSession());

      // Wait for the error state. Use findByTestId so RTL retries the
      // query until the fetch fails and the error branch renders.
      const errorRoot = await screen.findByTestId("analytics-error");
      expect(errorRoot).toBeInTheDocument();
      expect(errorRoot).toHaveAttribute("role", "alert");

      // Error message is shown - the text content is the API error message
      // OR the user-friendly fallback. Either is acceptable; we just
      // verify it's a non-empty string so screen readers announce
      // something meaningful.
      const errorMessage = screen.getByTestId("analytics-error-message");
      expect(errorMessage).toBeInTheDocument();
      const errorText = errorMessage.textContent ?? "";
      expect(errorText.length).toBeGreaterThan(0);

      // Retry button is rendered, enabled, and inside the error region.
      const retryButton = screen.getByTestId("analytics-retry-button");
      expect(retryButton).toBeInTheDocument();
      expect(retryButton).toBeEnabled();
      expect(retryButton.tagName).toBe("BUTTON");
      expect(errorRoot).toContainElement(retryButton);

      // Success panels remain hidden during the error state.
      expect(screen.queryByTestId("analytics-contributors-panel")).not.toBeInTheDocument();
      expect(screen.queryByTestId("analytics-leads-panel")).not.toBeInTheDocument();
      expect(screen.queryByTestId("analytics-activity-panel")).not.toBeInTheDocument();
    });

    it("retry triggers a refetch that succeeds on the second attempt", async () => {
      // First attempt returns 500; second attempt returns success.
      // The closure-captured `attempt` counter doubles as a probe to
      // assert exactly two requests were made (one initial fail, one
      // post-click success).
      let attempt = 0;
      server.use(
        http.get("/api/admin/analytics", () => {
          attempt += 1;
          if (attempt === 1) {
            return HttpResponse.json(
              {
                error: {
                  code: "server_error",
                  message: "Database temporarily unavailable",
                  correlation_id: "test-correlation-id",
                  fields: [],
                },
              },
              { status: 500 },
            );
          }
          return HttpResponse.json(makeAnalyticsResponse());
        }),
      );

      renderWithMockedSession(<Analytics />, buildAdminSession());

      // First attempt fails - error UI renders, success panels do not.
      const retryButton = await screen.findByTestId("analytics-retry-button");
      expect(screen.queryByTestId("analytics-contributors-panel")).not.toBeInTheDocument();

      // Click retry - second attempt succeeds. Use userEvent (NEVER
      // fireEvent) per project conventions; userEvent.click is async
      // because it emulates the full pointerdown/up/click sequence.
      await userEvent.click(retryButton);

      // After the refetch resolves, the success panels appear and
      // the error UI is gone.
      expect(await screen.findByTestId("analytics-contributors-panel")).toBeInTheDocument();
      expect(screen.queryByTestId("analytics-error")).not.toBeInTheDocument();

      // Exactly two attempts were made (one fail + one success).
      expect(attempt).toBe(2);
    });
  });

  // -------------------------------------------------------------------------
  // Success state - three panels
  // -------------------------------------------------------------------------

  describe("success state - three panels", () => {
    it("renders all three panels with default analytics data", async () => {
      // Default MSW handler returns makeAnalyticsResponse() with 3
      // contributors, 4 leads_by_status entries (one per status), and
      // 4 weekly_activity rows.
      renderWithMockedSession(<Analytics />, buildAdminSession());

      // Wait for the contributors panel - signal that the query has
      // resolved and the AnalyticsPanels tree is mounted.
      expect(await screen.findByTestId("analytics-contributors-panel")).toBeInTheDocument();
      expect(screen.getByTestId("analytics-leads-panel")).toBeInTheDocument();
      expect(screen.getByTestId("analytics-activity-panel")).toBeInTheDocument();

      // Loading and error indicators are gone.
      expect(screen.queryByTestId("analytics-loading")).not.toBeInTheDocument();
      expect(screen.queryByTestId("analytics-error")).not.toBeInTheDocument();
    });

    it("renders the analytics-generated-at timestamp on success", async () => {
      // Override the default handler with a fixed generated_at so the
      // assertion is deterministic regardless of the factory default.
      server.use(
        http.get("/api/admin/analytics", () =>
          HttpResponse.json(makeAnalyticsResponse({ generated_at: "2026-04-23T10:30:00+00:00" })),
        ),
      );

      renderWithMockedSession(<Analytics />, buildAdminSession());

      // The timestamp is rendered as a <p data-testid="analytics-generated-at">
      // with leading "Last updated:" prefix and the locale-formatted date.
      const timestamp = await screen.findByTestId("analytics-generated-at");
      expect(timestamp).toBeInTheDocument();
      expect(timestamp.tagName).toBe("P");
      expect(timestamp.textContent).toMatch(/last updated/i);
    });
  });

  // -------------------------------------------------------------------------
  // MostActiveContributorsPanel
  // -------------------------------------------------------------------------

  describe("MostActiveContributorsPanel", () => {
    it("renders one row per contributor with display_name and record_count", async () => {
      const contributors = [
        makeContributorActivity({ display_name: "Alice Carter", record_count: 42 }),
        makeContributorActivity({ display_name: "Bob Singh", record_count: 15 }),
        makeContributorActivity({ display_name: "Carol Liu", record_count: 7 }),
      ];
      server.use(
        http.get("/api/admin/analytics", () =>
          HttpResponse.json(makeAnalyticsResponse({ most_active_contributors: contributors })),
        ),
      );

      renderWithMockedSession(<Analytics />, buildAdminSession());

      const panel = await screen.findByTestId("analytics-contributors-panel");

      // Display names render in the contributor column.
      expect(within(panel).getByText("Alice Carter")).toBeInTheDocument();
      expect(within(panel).getByText("Bob Singh")).toBeInTheDocument();
      expect(within(panel).getByText("Carol Liu")).toBeInTheDocument();

      // Counts render formatted via toLocaleString (numbers <1000 are
      // unchanged in en-US).
      expect(within(panel).getByText("42")).toBeInTheDocument();
      expect(within(panel).getByText("15")).toBeInTheDocument();
      expect(within(panel).getByText("7")).toBeInTheDocument();

      // Empty placeholder must NOT render alongside populated rows.
      expect(within(panel).queryByTestId("analytics-contributors-empty")).not.toBeInTheDocument();
    });

    it("renders the empty placeholder when no contributors are returned", async () => {
      server.use(
        http.get("/api/admin/analytics", () =>
          HttpResponse.json(makeAnalyticsResponse({ most_active_contributors: [] })),
        ),
      );

      renderWithMockedSession(<Analytics />, buildAdminSession());

      // The empty-state placeholder appears with the documented message.
      const empty = await screen.findByTestId("analytics-contributors-empty");
      expect(empty).toBeInTheDocument();
      expect(empty.textContent).toMatch(/no contributors/i);
      expect(empty.textContent).toMatch(/start adding connections/i);
    });

    it("record-count cells use tabular-nums for stable alignment", async () => {
      // Choose a count >= 1000 so toLocaleString produces a thousands
      // separator ("1,234"), making the rendered number unambiguous to
      // locate via getByText (no collision with rank "1" / "2" / "3").
      const contributors = [
        makeContributorActivity({ display_name: "Alice Carter", record_count: 1234 }),
      ];
      server.use(
        http.get("/api/admin/analytics", () =>
          HttpResponse.json(makeAnalyticsResponse({ most_active_contributors: contributors })),
        ),
      );

      renderWithMockedSession(<Analytics />, buildAdminSession());

      const panel = await screen.findByTestId("analytics-contributors-panel");

      // The thousands-separator format from toLocaleString in en-US.
      const countCell = within(panel).getByText("1,234");
      expect(countCell).toBeInTheDocument();

      // Tailwind utility for stable digit alignment - a regression here
      // would mean the counts column visibly jitters when values change.
      expect(countCell.className).toMatch(/tabular-nums/);
    });
  });

  // -------------------------------------------------------------------------
  // LeadsByStatusPanel
  // -------------------------------------------------------------------------

  describe("LeadsByStatusPanel", () => {
    it("renders the four canonical OutreachStatus rows", async () => {
      server.use(
        http.get("/api/admin/analytics", () =>
          HttpResponse.json(
            makeAnalyticsResponse({
              leads_by_status: [
                { status: "Not Started", count: 10 },
                { status: "In Progress", count: 5 },
                { status: "Contacted", count: 8 },
                { status: "Closed", count: 3 },
              ],
            }),
          ),
        ),
      );

      renderWithMockedSession(<Analytics />, buildAdminSession());

      const list = await screen.findByTestId("analytics-leads-list");
      expect(list).toBeInTheDocument();
      expect(list.tagName).toBe("UL");

      // All four rows present, each with the documented slug-based testid.
      expect(screen.getByTestId("analytics-leads-row-not-started")).toBeInTheDocument();
      expect(screen.getByTestId("analytics-leads-row-in-progress")).toBeInTheDocument();
      expect(screen.getByTestId("analytics-leads-row-contacted")).toBeInTheDocument();
      expect(screen.getByTestId("analytics-leads-row-closed")).toBeInTheDocument();
    });

    it("re-orders leads_by_status to canonical OUTREACH_STATUS_VALUES order", async () => {
      // Backend returns entries in REVERSE order; the SPA must re-order
      // client-side via OUTREACH_STATUS_VALUES so the visual pipeline
      // ('Not Started' -> 'Closed') is stable regardless of backend
      // response ordering.
      server.use(
        http.get("/api/admin/analytics", () =>
          HttpResponse.json(
            makeAnalyticsResponse({
              leads_by_status: [
                { status: "Closed", count: 3 },
                { status: "Contacted", count: 8 },
                { status: "In Progress", count: 5 },
                { status: "Not Started", count: 10 },
              ],
            }),
          ),
        ),
      );

      renderWithMockedSession(<Analytics />, buildAdminSession());

      const list = await screen.findByTestId("analytics-leads-list");

      // Each <li> in the list corresponds to one OutreachStatus row,
      // preserving the canonical pipeline order in document order.
      const rows = within(list).getAllByRole("listitem");
      expect(rows).toHaveLength(4);
      expect(rows[0]).toHaveAttribute("data-testid", "analytics-leads-row-not-started");
      expect(rows[1]).toHaveAttribute("data-testid", "analytics-leads-row-in-progress");
      expect(rows[2]).toHaveAttribute("data-testid", "analytics-leads-row-contacted");
      expect(rows[3]).toHaveAttribute("data-testid", "analytics-leads-row-closed");

      // Sanity: the canonical order constant remains the source of
      // truth. Locking the test's expected order to the constant means
      // any future re-order of OUTREACH_STATUS_VALUES will surface
      // here as a deliberate change rather than a silent regression.
      expect([...OUTREACH_STATUS_VALUES]).toEqual([
        "Not Started",
        "In Progress",
        "Contacted",
        "Closed",
      ]);
    });

    it("defaults missing leads_by_status entries to count=0", async () => {
      // Only return ONE status; the other three should default to count=0
      // so the panel always renders a complete four-row pipeline view.
      server.use(
        http.get("/api/admin/analytics", () =>
          HttpResponse.json(
            makeAnalyticsResponse({
              leads_by_status: [makeLeadsByStatusEntry({ status: "In Progress", count: 5 })],
            }),
          ),
        ),
      );

      renderWithMockedSession(<Analytics />, buildAdminSession());

      // List itself renders.
      await screen.findByTestId("analytics-leads-list");

      // The three missing rows still render with count "0".
      const notStartedCount = screen.getByTestId("analytics-leads-count-not-started");
      expect(notStartedCount).toHaveTextContent("0");
      const contactedCount = screen.getByTestId("analytics-leads-count-contacted");
      expect(contactedCount).toHaveTextContent("0");
      const closedCount = screen.getByTestId("analytics-leads-count-closed");
      expect(closedCount).toHaveTextContent("0");

      // The single returned row has count 5.
      const inProgressCount = screen.getByTestId("analytics-leads-count-in-progress");
      expect(inProgressCount).toHaveTextContent("5");
    });

    it("computes the analytics-leads-total as the sum of counts", async () => {
      server.use(
        http.get("/api/admin/analytics", () =>
          HttpResponse.json(
            makeAnalyticsResponse({
              leads_by_status: [
                { status: "Not Started", count: 10 },
                { status: "In Progress", count: 5 },
                { status: "Contacted", count: 8 },
                { status: "Closed", count: 3 },
              ],
            }),
          ),
        ),
      );

      renderWithMockedSession(<Analytics />, buildAdminSession());

      // 10 + 5 + 8 + 3 = 26.
      const total = await screen.findByTestId("analytics-leads-total");
      expect(total).toHaveTextContent("26");
    });

    it("handles all-zero counts gracefully", async () => {
      // When every status has count=0, the panel must still render
      // the four rows with count "0" and the total as "0" (no
      // division-by-zero in the percentage computation).
      server.use(
        http.get("/api/admin/analytics", () =>
          HttpResponse.json(
            makeAnalyticsResponse({
              leads_by_status: [
                { status: "Not Started", count: 0 },
                { status: "In Progress", count: 0 },
                { status: "Contacted", count: 0 },
                { status: "Closed", count: 0 },
              ],
            }),
          ),
        ),
      );

      renderWithMockedSession(<Analytics />, buildAdminSession());

      await screen.findByTestId("analytics-leads-list");

      // All four rows render as zero; total is zero.
      expect(screen.getByTestId("analytics-leads-count-not-started")).toHaveTextContent("0");
      expect(screen.getByTestId("analytics-leads-count-in-progress")).toHaveTextContent("0");
      expect(screen.getByTestId("analytics-leads-count-contacted")).toHaveTextContent("0");
      expect(screen.getByTestId("analytics-leads-count-closed")).toHaveTextContent("0");

      const total = screen.getByTestId("analytics-leads-total");
      expect(total).toHaveTextContent("0");
    });
  });

  // -------------------------------------------------------------------------
  // WeeklyActivitySparkline
  // -------------------------------------------------------------------------

  describe("WeeklyActivitySparkline", () => {
    it("renders the empty placeholder when weekly_activity is empty", async () => {
      server.use(
        http.get("/api/admin/analytics", () =>
          HttpResponse.json(makeAnalyticsResponse({ weekly_activity: [] })),
        ),
      );

      renderWithMockedSession(<Analytics />, buildAdminSession());

      // The empty-state placeholder appears.
      const empty = await screen.findByTestId("analytics-activity-empty");
      expect(empty).toBeInTheDocument();
      expect(empty.textContent).toMatch(/no activity yet/i);

      // Sparkline SVG and summary should NOT be present in empty state.
      expect(screen.queryByTestId("analytics-activity-sparkline")).not.toBeInTheDocument();
      expect(screen.queryByTestId("analytics-activity-summary")).not.toBeInTheDocument();
    });

    it("renders an SVG sparkline with role and aria-label when weeks have data", async () => {
      const weeks = [
        makeWeeklyActivityEntry({ week_start: "2026-04-01", record_count: 7 }),
        makeWeeklyActivityEntry({ week_start: "2026-04-08", record_count: 12 }),
        makeWeeklyActivityEntry({ week_start: "2026-04-15", record_count: 9 }),
        makeWeeklyActivityEntry({ week_start: "2026-04-22", record_count: 15 }),
      ];
      server.use(
        http.get("/api/admin/analytics", () =>
          HttpResponse.json(makeAnalyticsResponse({ weekly_activity: weeks })),
        ),
      );

      renderWithMockedSession(<Analytics />, buildAdminSession());

      const sparkline = await screen.findByTestId("analytics-activity-sparkline");
      expect(sparkline).toBeInTheDocument();
      expect(sparkline.tagName.toLowerCase()).toBe("svg");

      // role="img" + aria-label is the documented WAI-ARIA pattern for
      // a meaningful chart-like SVG.
      expect(sparkline).toHaveAttribute("role", "img");
      expect(sparkline).toHaveAttribute("aria-label");

      // The label encodes both the verb ("weekly record count") and
      // the duration ("4 weeks"). Using two regex assertions keeps the
      // test resilient to small wording changes that preserve intent.
      const ariaLabel = sparkline.getAttribute("aria-label") ?? "";
      expect(ariaLabel).toMatch(/weekly record count/i);
      expect(ariaLabel).toMatch(/4 weeks/i);

      // With 4 data points and at least one non-zero count, the
      // sparkline draws at least one <path> for the line (and a
      // second optional <path> for the area-fill). We assert
      // "at least one" rather than an exact count to allow internal
      // refactors that consolidate or split paths.
      const paths = sparkline.querySelectorAll("path");
      expect(paths.length).toBeGreaterThanOrEqual(1);

      // Empty-state placeholder must NOT render alongside the sparkline.
      expect(screen.queryByTestId("analytics-activity-empty")).not.toBeInTheDocument();
    });

    it("renders the summary block with total records and peak week", async () => {
      const weeks = [
        makeWeeklyActivityEntry({ week_start: "2026-04-01", record_count: 7 }),
        makeWeeklyActivityEntry({ week_start: "2026-04-08", record_count: 12 }),
        makeWeeklyActivityEntry({ week_start: "2026-04-15", record_count: 9 }),
      ];
      server.use(
        http.get("/api/admin/analytics", () =>
          HttpResponse.json(makeAnalyticsResponse({ weekly_activity: weeks })),
        ),
      );

      renderWithMockedSession(<Analytics />, buildAdminSession());

      const summary = await screen.findByTestId("analytics-activity-summary");
      expect(summary).toBeInTheDocument();

      // Total records (7 + 12 + 9 = 28). Rendered via toLocaleString so
      // values <1000 produce no thousands separator.
      expect(within(summary).getByText("28")).toBeInTheDocument();

      // Peak week: the entry with max record_count is week_start
      // "2026-04-08" with count 12. The peak-week span renders both
      // values together as "{week_start} ({maxCount})".
      expect(within(summary).getByText(/2026-04-08/)).toBeInTheDocument();
      expect(within(summary).getByText(/\(12\)/)).toBeInTheDocument();

      // Header text mentions the duration in plural form ("3 weeks").
      expect(within(summary).getByText(/3 weeks/i)).toBeInTheDocument();
    });

    it("renders a dashed baseline (no path with d) when all weekly counts are zero", async () => {
      const weeks = [
        makeWeeklyActivityEntry({ week_start: "2026-04-01", record_count: 0 }),
        makeWeeklyActivityEntry({ week_start: "2026-04-08", record_count: 0 }),
      ];
      server.use(
        http.get("/api/admin/analytics", () =>
          HttpResponse.json(makeAnalyticsResponse({ weekly_activity: weeks })),
        ),
      );

      renderWithMockedSession(<Analytics />, buildAdminSession());

      const sparkline = await screen.findByTestId("analytics-activity-sparkline");
      expect(sparkline).toBeInTheDocument();

      // No <path> with non-empty d (no line/area) when maxCount === 0.
      // We tolerate <path> elements with empty/missing d attributes
      // because the renderer guards them with `pathD !== ""` and
      // `areaD !== ""`, but a defensive query still catches stray
      // elements.
      const paths = sparkline.querySelectorAll("path");
      paths.forEach((p) => {
        const d = p.getAttribute("d");
        expect(d === null || d === "").toBeTruthy();
      });

      // At least one <line> exists (gridlines + dashed baseline).
      const lines = sparkline.querySelectorAll("line");
      expect(lines.length).toBeGreaterThanOrEqual(1);

      // A dashed line is rendered as the all-zero baseline. We locate
      // it by its stroke-dasharray attribute, which only the baseline
      // carries (the gridlines are solid).
      const dashedLine = Array.from(lines).find((l) => l.getAttribute("stroke-dasharray") !== null);
      expect(dashedLine).toBeDefined();
    });

    it("handles a single-week input by NOT throwing (line requires >=2 points)", async () => {
      // With only one entry the sparkline cannot draw a multi-point
      // line. The component must still render the panel, summary, and
      // SVG container without throwing - the dashed baseline serves
      // as the visual fallback.
      const weeks = [makeWeeklyActivityEntry({ week_start: "2026-04-22", record_count: 5 })];
      server.use(
        http.get("/api/admin/analytics", () =>
          HttpResponse.json(makeAnalyticsResponse({ weekly_activity: weeks })),
        ),
      );

      renderWithMockedSession(<Analytics />, buildAdminSession());

      // Panel, summary, and sparkline elements all render.
      expect(await screen.findByTestId("analytics-activity-panel")).toBeInTheDocument();
      expect(screen.getByTestId("analytics-activity-summary")).toBeInTheDocument();

      const sparkline = screen.getByTestId("analytics-activity-sparkline");
      expect(sparkline).toBeInTheDocument();

      // The aria-label uses singular "week" for totalWeeks === 1
      // (per the source: `${totalWeeks === 1 ? "week" : "weeks"}`).
      // Asserting the exact string locks the singular/plural branch
      // so a regression to always-plural would fail loudly.
      expect(sparkline.getAttribute("aria-label")).toBe("Weekly record count over 1 week");

      // No multi-point <path> elements for a single data point.
      const paths = sparkline.querySelectorAll("path");
      paths.forEach((p) => {
        const d = p.getAttribute("d");
        expect(d === null || d === "").toBeTruthy();
      });
    });
  });

  // -------------------------------------------------------------------------
  // Accessibility and semantics
  // -------------------------------------------------------------------------

  describe("accessibility and semantics", () => {
    it("uses semantic <section> root with aria-labelledby pointing to the heading", () => {
      // No await - the root <section> is rendered synchronously
      // regardless of query state.
      //
      // Per QA Visual Consistency Issue 7 fix, route content uses a
      // labeled <section> region rather than a nested <main> landmark
      // (HTML5 spec permits only one <main> per document; the document's
      // primary <main> lives in App.tsx).
      renderWithMockedSession(<Analytics />, buildAdminSession());

      const root = screen.getByTestId("admin-analytics");
      expect(root.tagName.toLowerCase()).toBe("section");
      expect(root).toHaveAttribute("aria-labelledby", "admin-analytics-heading");
    });

    it("each panel section uses aria-labelledby for its sub-heading", async () => {
      // Default handler returns a populated payload, so all three
      // sub-panels render. We assert each sub-panel's
      // aria-labelledby attribute points at its heading id.
      renderWithMockedSession(<Analytics />, buildAdminSession());

      const contributorsPanel = await screen.findByTestId("analytics-contributors-panel");
      expect(contributorsPanel).toHaveAttribute(
        "aria-labelledby",
        "analytics-contributors-heading",
      );

      const leadsPanel = screen.getByTestId("analytics-leads-panel");
      expect(leadsPanel).toHaveAttribute("aria-labelledby", "analytics-leads-heading");

      const activityPanel = screen.getByTestId("analytics-activity-panel");
      expect(activityPanel).toHaveAttribute("aria-labelledby", "analytics-activity-heading");

      // Each labelled-by id resolves to a matching <h3> heading inside
      // its panel - a regression here would mean assistive tech
      // announces the sub-panel as unlabelled.
      const contributorsHeading = within(contributorsPanel).getByRole("heading", { level: 3 });
      expect(contributorsHeading).toHaveAttribute("id", "analytics-contributors-heading");

      const leadsHeading = within(leadsPanel).getByRole("heading", { level: 3 });
      expect(leadsHeading).toHaveAttribute("id", "analytics-leads-heading");

      const activityHeading = within(activityPanel).getByRole("heading", { level: 3 });
      expect(activityHeading).toHaveAttribute("id", "analytics-activity-heading");
    });
  });
});
