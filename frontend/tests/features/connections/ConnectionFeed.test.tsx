/**
 * ConnectionFeed.test.tsx - Vitest tests for F-004 Connection Feed Dashboard.
 *
 * Targets `frontend/src/features/connections/ConnectionFeed.tsx`. The
 * component (per AAP Sec 0.5.4 Screen 1 / F-004) is the post-login
 * landing surface for Viewer and Contributor roles. It exposes:
 *
 *   - Six row elements per record:
 *       Name (with detail-page link), Company, Involvement badge,
 *       Submitter / owner_display_name, Outreach Status chip,
 *       Submission Date (plus a 7th Tags column with +N overflow).
 *
 *   - Six filter dimensions:
 *       full-name search (URL `q`), company text, submission-date
 *       range (`from` / `to`), involvement multi-select chips (3
 *       enum values), outreach-status multi-select chips (4 enum
 *       values), and tags multi-select pills.
 *
 *   - Five sort dimensions:
 *       submission_date (default, desc), full_name, company,
 *       owner_display_name, outreach_status.
 *
 *   - Page-based pagination:
 *       page sizes [10, 25, 50, 100] (default 25); URL params `page`
 *       and `page_size`. The backend list endpoint mirrors these.
 *
 *   - "Add Connection" CTA wrapped in `<Link to="/connections/new">`.
 *
 *   - URL-encoded filter/sort/pagination state via `useSearchParams`
 *     so the screen is shareable and back-button-friendly.
 *
 * What this file verifies (each test bullet maps to a `describe`/`it`
 * pair below):
 *
 *   - Initial render: heading, subtitle, Add CTA, filter bar, table
 *     columns, pagination region, and that the default backend
 *     request encodes `sort=submission_date`, `sort_dir=desc`,
 *     `page=1`, `page_size=25`.
 *   - Row content: name link to `/connections/:id`, company text,
 *     InvolvementBadge variant testid, owner display name, status
 *     chip, formatted date, and the F-008 tag chips (with +N
 *     overflow when more than three are present, and an em-dash
 *     placeholder when the tag list is empty).
 *   - Empty state: `connection-feed-empty` renders with `role="status"`
 *     and the pagination region is suppressed at total=0.
 *   - Error state: a 500 from the list endpoint surfaces
 *     `connection-feed-error` with `role="alert"` and a Retry button;
 *     the filter bar and Add CTA remain visible so the user can
 *     adjust filters or compose a record while the list is broken.
 *   - Loading state: while the list query is pending the Table
 *     primitive renders skeleton rows (no empty state, no error).
 *   - Filter wiring: typing in the search input adds `q=` to the URL
 *     and the backend receives `full_name_search=`; same pattern for
 *     company (URL `company=`, backend `company=`) and date range
 *     (URL `from=` / `to=`, backend `submission_date_from=` /
 *     `submission_date_to=`).
 *   - Multi-select chip toggles: clicking an involvement / status /
 *     tag chip flips its `aria-pressed`, the URL gains the value,
 *     and a second click removes it. Multiple values use repeated
 *     URL keys (`involvement=Warm+Intro&involvement=Soft+Reference`).
 *   - Clear-all: the clear button is visible only when at least one
 *     filter is active; clicking it removes every filter param while
 *     preserving the active sort and page-size.
 *   - Sort: a URL-seeded `sort=full_name&sort_dir=asc` round-trips to
 *     the backend; clicking the Table primitive's column-header sort
 *     button rewrites the URL.
 *   - Pagination: changing the page-size Select rewrites `page_size`
 *     and resets `page`; the Next button advances `page`; Previous
 *     is disabled at page 1; Next is disabled at the last page.
 *   - Add Connection CTA: the rendered button is wrapped in
 *     `<Link to="/connections/new">` (assert via `href`, not navigation).
 *   - Row navigation: clicking a non-link area of a row fires
 *     `navigate(`/connections/:id`)` via the Table primitive's
 *     `onRowClick` (asserted on the navigateMock spy); the name
 *     `<Link>` itself carries the same `/connections/:id` href.
 *   - Filter changes reset the page: starting at `?page=3`, typing
 *     in the search input drops `page` from the URL.
 *
 * Test infrastructure:
 *   - MSW (msw@2.7.0) intercepts every `fetch` made by the real
 *     `useConnectionsQuery` and `useTagsQuery` hooks. Per-test
 *     overrides via `server.use(...)` swap the default behavior for
 *     specific scenarios (empty list, 500, capture-request URL,
 *     never-resolving for loading state).
 *   - The MSW server is booted in `frontend/tests/setup.ts` with
 *     `onUnhandledRequest: "error"`, so the default handlers in
 *     `frontend/tests/mocks/handlers.ts` cover every URL the SPA
 *     fetches (they do, including GET /api/connections and /api/tags).
 *   - `renderWithMockedSession` wraps the feed in
 *     QueryClientProvider > MockAuthProvider > MemoryRouter so role
 *     gating (e.g., StatusChip's RoleGate) resolves synchronously
 *     to the supplied session; default tests use a Contributor
 *     session so the StatusChip falls back to the read-only badge.
 *   - A `LocationCapture` test-harness component reads the current
 *     `useLocation().search` and renders it into a stable
 *     `data-testid="location-search"` element so URL-state
 *     assertions are robust under MemoryRouter (where
 *     `window.location` does NOT update).
 *   - `vi.mock("react-router-dom", ...)` replaces `useNavigate` with
 *     a module-level `navigateMock` while preserving the real
 *     implementations of `useLocation`, `useSearchParams`,
 *     `MemoryRouter`, `Link`, etc. The mock is hoisted above the
 *     test imports so the component picks it up at module load.
 *
 * Conventions per AAP Sec 0.7.7 and frontend/.prettierrc.json:
 *   - Strict TypeScript; eslint allows `any` in tests but the project
 *     still avoids it (we keep types tight to surface drift early).
 *   - Double quotes; trailing commas; 2-space indent; line length <= 100.
 *   - Named imports only; no default export.
 *   - No emoji; no console.log.
 *   - jest-dom matchers (`toBeInTheDocument`, `toHaveAttribute`,
 *     `toHaveClass`, etc.) are globally registered in tests/setup.ts.
 *
 * Coordinates with:
 *   - frontend/src/features/connections/ConnectionFeed.tsx (system under test).
 *   - frontend/src/api/connections.ts (`useConnectionsQuery`,
 *     `useTagsQuery`, `ConnectionListParams`, query-key factories).
 *   - frontend/tests/test-utils.tsx (renderWithMockedSession + RTL re-exports).
 *   - frontend/tests/mocks/server.ts (MSW Node server instance).
 *   - frontend/tests/mocks/handlers.ts (default + override handlers).
 *   - frontend/tests/mocks/data.ts (typed entity factories).
 *   - frontend/tests/setup.ts (jest-dom, MSW lifecycle, toast/correlation reset).
 */

import { type JSX } from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { http, HttpResponse } from "msw";
import { useLocation } from "react-router-dom";

// ---------------------------------------------------------------------------
// react-router-dom partial mock (useNavigate spy only)
// ---------------------------------------------------------------------------
//
// Vitest hoists vi.mock above all imports, so this mock is in place
// before ConnectionFeed.tsx is loaded. We use vi.importActual to
// retain the real MemoryRouter, Routes, Route, Link, useSearchParams,
// useLocation, and useParams - only useNavigate is replaced. This
// preserves the entire URL-state pipeline (which the feed depends on
// via useSearchParams) while still letting tests assert programmatic
// navigation from the component's onRowClick handler.

const navigateMock = vi.fn();

vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>("react-router-dom");
  return {
    ...actual,
    useNavigate: () => navigateMock,
  };
});

// ---------------------------------------------------------------------------
// Test infrastructure imports (AFTER vi.mock so the mock is applied)
// ---------------------------------------------------------------------------

import { server } from "../../mocks/server";
import { overrides } from "../../mocks/handlers";
import {
  makeConnectionRead,
  makeConnectionList,
  makePaginatedConnections,
  makeSessionRead,
  makeTagRead,
  makeUserRead,
} from "../../mocks/data";
import {
  renderWithMockedSession,
  screen,
  within,
  waitFor,
  userEvent,
  fireEvent,
} from "../../test-utils";
import { ConnectionFeed } from "@/features/connections/ConnectionFeed";

// ---------------------------------------------------------------------------
// LocationCapture test harness
// ---------------------------------------------------------------------------

/**
 * Renders the current `useLocation().search` into a stable
 * `data-testid="location-search"` element so tests can assert on URL
 * state changes without poking at MemoryRouter internals.
 *
 * Why this helper exists:
 *   `MemoryRouter` does NOT update `window.location.search`; the
 *   in-memory router's history is the single source of truth.
 *   Asserting on `window.location` would silently always pass with
 *   an empty string. Reading `useLocation().search` from inside the
 *   provider tree is the canonical way to observe URL changes
 *   triggered by `setSearchParams(...)`.
 *
 * The component renders the empty string when no params are present,
 * and `?key=value...` once filters/sort/pagination are seeded. Tests
 * therefore assert via either `toHaveTextContent("...")` or
 * substring-match against `getByTestId("location-search").textContent`.
 */
function LocationCapture(): JSX.Element {
  const location = useLocation();
  return <div data-testid="location-search">{location.search}</div>;
}

// ---------------------------------------------------------------------------
// Module-scoped helpers
// ---------------------------------------------------------------------------

/**
 * Build a Contributor-role session for the default (read-only feed)
 * test path. Contributor sees every record but cannot mutate the
 * outreach status; the StatusChip therefore falls through to the
 * read-only Badge fallback (`status-chip-readonly-{recordId}`),
 * matching AAP Sec 0.7.1 invariant 7 ("API-layer authorization is
 * authoritative"; the role gate in the SPA is a UX courtesy).
 */
function contributorSession() {
  return makeSessionRead({
    user: makeUserRead({
      id: "session-user-1",
      role: "Contributor",
      display_name: "Contributor User",
      email: "contributor@example.com",
    }),
    authenticated: true,
  });
}

/**
 * Render the feed alongside `<LocationCapture />` so URL-state
 * assertions remain available across every test. The two components
 * share the same MemoryRouter (because they share the renderer's
 * provider stack), so `useLocation()` inside LocationCapture observes
 * exactly the same URL state that the feed reads via
 * `useSearchParams()`.
 *
 * Defaults to a Contributor session and `initialEntries: ['/feed']`;
 * tests pass overrides via the second argument.
 */
function renderFeed(initialEntries: string[] = ["/feed"]) {
  return renderWithMockedSession(
    <>
      <ConnectionFeed />
      <LocationCapture />
    </>,
    contributorSession(),
    { initialEntries },
  );
}

// ---------------------------------------------------------------------------
// Top-level test suite
// ---------------------------------------------------------------------------

describe("<ConnectionFeed />", () => {
  beforeEach(() => {
    // Clear navigateMock so cross-test state does not pollute later
    // assertions like `expect(navigateMock).toHaveBeenCalledWith(...)`.
    navigateMock.mockClear();
  });

  // -------------------------------------------------------------------------
  // Render - initial mount with default state
  // -------------------------------------------------------------------------
  describe("Render - initial mount with default state", () => {
    it("renders heading, subtitle, Add CTA, filter bar, table, and pagination", async () => {
      // Default handler returns 10 records; that's enough to render
      // the table body and keep pagination visible (total > 0).
      renderFeed();

      // Wait for the feed shell to appear (the heading is the cheapest
      // unique anchor; alternatively findByTestId('connection-feed')).
      const feed = await screen.findByTestId("connection-feed");
      expect(feed).toBeInTheDocument();

      // Heading and subtitle exactly as documented in the source.
      expect(screen.getByRole("heading", { level: 1, name: /connections/i })).toBeInTheDocument();
      expect(
        screen.getByText("Browse and act on every connection idea logged by the team."),
      ).toBeInTheDocument();

      // Add Connection CTA (button + wrapping link).
      expect(screen.getByTestId("connection-feed-add-connection")).toBeInTheDocument();
      expect(screen.getByTestId("connection-feed-add-connection-link")).toBeInTheDocument();

      // Filter bar inputs.
      expect(screen.getByTestId("connection-feed-filter-search")).toBeInTheDocument();
      expect(screen.getByTestId("connection-feed-filter-company")).toBeInTheDocument();
      expect(screen.getByTestId("connection-feed-filter-date-from")).toBeInTheDocument();
      expect(screen.getByTestId("connection-feed-filter-date-to")).toBeInTheDocument();

      // Three involvement chips (kebab-case suffix per source).
      expect(
        screen.getByTestId("connection-feed-filter-involvement-warm-intro"),
      ).toBeInTheDocument();
      expect(
        screen.getByTestId("connection-feed-filter-involvement-soft-reference"),
      ).toBeInTheDocument();
      expect(
        screen.getByTestId("connection-feed-filter-involvement-target-only"),
      ).toBeInTheDocument();

      // Four outreach status chips (kebab-case suffix per source).
      expect(screen.getByTestId("connection-feed-filter-status-not-started")).toBeInTheDocument();
      expect(screen.getByTestId("connection-feed-filter-status-in-progress")).toBeInTheDocument();
      expect(screen.getByTestId("connection-feed-filter-status-contacted")).toBeInTheDocument();
      expect(screen.getByTestId("connection-feed-filter-status-closed")).toBeInTheDocument();

      // Wait for the table data to settle so pagination becomes
      // visible (PaginationBar only renders when total > 0).
      await waitFor(() => {
        expect(screen.getByTestId("connection-feed-pagination")).toBeInTheDocument();
      });

      // Page navigation primitives.
      expect(screen.getByTestId("connection-feed-prev-page")).toBeInTheDocument();
      expect(screen.getByTestId("connection-feed-next-page")).toBeInTheDocument();
      expect(screen.getByTestId("connection-feed-page-size")).toBeInTheDocument();

      // At page 1 with total=10, page_size=25 -> Previous disabled,
      // Next disabled (only one page worth of data).
      expect(screen.getByTestId("connection-feed-prev-page")).toBeDisabled();
      expect(screen.getByTestId("connection-feed-next-page")).toBeDisabled();
    });

    it("issues the initial GET /api/connections with default sort and pagination", async () => {
      // Capture the exact query string the SPA sends on first mount so
      // we can verify the default param values per the source's
      // paramsToListParams() and buildConnectionListQuery() helpers.
      let capturedUrl = "";
      server.use(
        http.get("*/api/connections", ({ request }) => {
          capturedUrl = request.url;
          return HttpResponse.json(makePaginatedConnections(makeConnectionList(0), { total: 0 }));
        }),
      );

      renderFeed();

      // Await the request firing - the empty-state appears once the
      // query settles, which guarantees the handler ran.
      await waitFor(() => {
        expect(capturedUrl).toContain("/api/connections");
      });

      // Default sort key/direction must be on the wire so the backend
      // returns "newest first" (matches the user's mental model).
      expect(capturedUrl).toContain("sort=submission_date");
      expect(capturedUrl).toContain("sort_dir=desc");
      // Default pagination - the source always populates these into
      // the listParams object so the cache key stays stable.
      expect(capturedUrl).toContain("page=1");
      expect(capturedUrl).toContain("page_size=25");
    });
  });

  // -------------------------------------------------------------------------
  // Render - table columns and row content
  // -------------------------------------------------------------------------
  describe("Render - table columns and row content", () => {
    it("renders all 7 column elements for a record (Name, Company, Involvement, Submitter, Status, Date, Tags)", async () => {
      const record = makeConnectionRead({
        id: "rec-A",
        full_name: "Alice Smith",
        company: "Acme Corp",
        job_title: "VP Engineering",
        involvement: "Warm Intro",
        owner_display_name: "Bob Owner",
        outreach_status: "Not Started",
        submission_date: "2026-04-01T10:00:00+00:00",
        tags: [makeTagRead({ id: "tag-1", name: "industry:saas" })],
      });

      server.use(
        http.get("*/api/connections", () =>
          HttpResponse.json(makePaginatedConnections([record], { total: 1, limit: 25, offset: 0 })),
        ),
      );

      renderFeed();

      // Wait for the row to render via its name-link testid (the
      // column-cell testids only appear once the row mounts).
      const nameLink = await screen.findByTestId("connection-feed-row-name-rec-A");
      expect(nameLink).toHaveTextContent("Alice Smith");
      // The Link points at the canonical detail route per F-011.
      expect(nameLink).toHaveAttribute("href", "/connections/rec-A");

      // Job title sub-line under the name (renders alongside the link
      // inside the same column cell per the source).
      expect(screen.getByText("VP Engineering")).toBeInTheDocument();

      // Company column.
      const companyCell = screen.getByTestId("connection-feed-row-company-rec-A");
      expect(companyCell).toHaveTextContent("Acme Corp");

      // InvolvementBadge variant testid - the column wraps the badge
      // in a span carrying connection-feed-row-involvement-{id}.
      expect(screen.getByTestId("connection-feed-row-involvement-rec-A")).toBeInTheDocument();
      // The InvolvementBadge itself emits its own variant testid.
      expect(screen.getByTestId("involvement-badge-involvement-warm-intro")).toBeInTheDocument();

      // Submitter column shows the denormalized owner_display_name
      // (per F-006, this is captured at submit time and immutable).
      const submitterCell = screen.getByTestId("connection-feed-row-submitter-rec-A");
      expect(submitterCell).toHaveTextContent("Bob Owner");

      // Status chip column - Contributor session falls through to the
      // read-only Badge fallback (RoleGate fallback per StatusChip.tsx),
      // so we expect the readonly testid suffix.
      expect(screen.getByTestId("connection-feed-row-status-rec-A")).toBeInTheDocument();
      expect(screen.getByTestId("status-chip-readonly-rec-A")).toBeInTheDocument();

      // Submission-date column carries the formatted date.
      const dateCell = screen.getByTestId("connection-feed-row-date-rec-A");
      expect(dateCell).toBeInTheDocument();
      // The exact rendered string depends on the test runner's locale;
      // the cell must at minimum contain the year so the format helper
      // produced a real date (and not the em-dash placeholder).
      expect(dateCell.textContent).toMatch(/2026/);

      // Tags list with one tag - the tag name appears as a Badge.
      const tagsList = screen.getByTestId("connection-feed-row-tags-rec-A");
      expect(within(tagsList).getByText("industry:saas")).toBeInTheDocument();
    });

    it("shows +N overflow indicator when a record has more than 3 tags", async () => {
      const record = makeConnectionRead({
        id: "rec-overflow",
        tags: [
          makeTagRead({ id: "t-1", name: "industry:saas" }),
          makeTagRead({ id: "t-2", name: "use-case:demand-gen" }),
          makeTagRead({ id: "t-3", name: "geography:nyc" }),
          makeTagRead({ id: "t-4", name: "stage:series-b" }),
          makeTagRead({ id: "t-5", name: "vertical:fintech" }),
        ],
      });

      server.use(
        http.get("*/api/connections", () =>
          HttpResponse.json(makePaginatedConnections([record], { total: 1 })),
        ),
      );

      renderFeed();

      const tagsList = await screen.findByTestId("connection-feed-row-tags-rec-overflow");
      // First three tags render as inline Badges.
      expect(within(tagsList).getByText("industry:saas")).toBeInTheDocument();
      expect(within(tagsList).getByText("use-case:demand-gen")).toBeInTheDocument();
      expect(within(tagsList).getByText("geography:nyc")).toBeInTheDocument();
      // The overflow indicator shows "+2" for the two truncated tags.
      expect(within(tagsList).getByText("+2")).toBeInTheDocument();
      // The truncated tag names are NOT rendered as visible text -
      // only the first three plus the +N badge appear in the cell.
      expect(within(tagsList).queryByText("stage:series-b")).not.toBeInTheDocument();
      expect(within(tagsList).queryByText("vertical:fintech")).not.toBeInTheDocument();
    });

    it("shows an em-dash placeholder when a record has zero tags", async () => {
      const record = makeConnectionRead({ id: "rec-no-tags", tags: [] });
      server.use(
        http.get("*/api/connections", () =>
          HttpResponse.json(makePaginatedConnections([record], { total: 1 })),
        ),
      );

      renderFeed();

      // Wait for the row to render; the em-dash is rendered inside
      // the tags column cell as the explicit empty-state.
      await screen.findByTestId("connection-feed-row-name-rec-no-tags");

      // The em-dash (U+2014) is the documented zero-tag affordance per
      // the source; it must appear somewhere in the rendered DOM.
      // Using textContent and a regex is safer than getByText because
      // the same em-dash also serves as the empty-date placeholder for
      // records lacking a submission_date.
      expect(document.body.textContent).toContain("\u2014");
    });
  });

  // -------------------------------------------------------------------------
  // Render - empty state
  // -------------------------------------------------------------------------
  describe("Render - empty state", () => {
    it("renders the empty-state message with role='status' when zero records", async () => {
      server.use(overrides.connections.listEmpty());

      renderFeed();

      const empty = await screen.findByTestId("connection-feed-empty");
      expect(empty).toBeInTheDocument();
      expect(empty).toHaveAttribute("role", "status");
      // The message guides the user toward the next action.
      expect(empty.textContent).toMatch(/no connections match/i);
    });

    it("hides the pagination region when total is zero", async () => {
      server.use(overrides.connections.listEmpty());

      renderFeed();

      // Wait for the empty-state to confirm the query settled.
      await screen.findByTestId("connection-feed-empty");

      // PaginationBar only mounts when total > 0 (per the source's
      // `{total > 0 && <PaginationBar ... />}` guard).
      expect(screen.queryByTestId("connection-feed-pagination")).not.toBeInTheDocument();
      expect(screen.queryByTestId("connection-feed-prev-page")).not.toBeInTheDocument();
      expect(screen.queryByTestId("connection-feed-next-page")).not.toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // Render - error state
  // -------------------------------------------------------------------------
  describe("Render - error state", () => {
    it("renders the error banner with role='alert' when GET /api/connections returns 500", async () => {
      server.use(overrides.connections.list500());

      renderFeed();

      const errorBanner = await screen.findByTestId("connection-feed-error");
      expect(errorBanner).toBeInTheDocument();
      expect(errorBanner).toHaveAttribute("role", "alert");
      // The banner shows a "Could not load connections." headline so
      // the user understands the network failure is the cause.
      expect(errorBanner.textContent).toMatch(/could not load connections/i);
      // The Retry control is present so the user can recover.
      expect(screen.getByTestId("connection-feed-error-retry")).toBeInTheDocument();
    });

    it("keeps the filter bar and Add Connection CTA visible during the error", async () => {
      server.use(overrides.connections.list500());

      renderFeed();

      // Wait for the error to render so the test captures the
      // error-state snapshot deterministically.
      await screen.findByTestId("connection-feed-error");

      // Filter bar primitives still render so the user can adjust
      // filters and (implicitly) trigger a refetch.
      expect(screen.getByTestId("connection-feed-filter-search")).toBeInTheDocument();
      expect(screen.getByTestId("connection-feed-filter-company")).toBeInTheDocument();

      // Add Connection CTA still renders so users can compose a new
      // record while the list endpoint is broken.
      expect(screen.getByTestId("connection-feed-add-connection")).toBeInTheDocument();
    });

    it("re-fires the request when the Retry button is clicked", async () => {
      // Track how many times the handler is invoked. The first call
      // returns 500; the second returns a successful empty list so we
      // can detect that retry triggered a fresh fetch.
      let callCount = 0;
      server.use(
        http.get("*/api/connections", () => {
          callCount += 1;
          if (callCount === 1) {
            return HttpResponse.json(
              { error: { code: "internal_error", message: "Internal server error" } },
              { status: 500 },
            );
          }
          return HttpResponse.json(makePaginatedConnections([], { total: 0 }));
        }),
      );

      const user = userEvent.setup();
      renderFeed();

      const retry = await screen.findByTestId("connection-feed-error-retry");
      await user.click(retry);

      // After retry the empty-state appears (the second handler call
      // returned an empty list). Both paths through the handler
      // increment callCount, so we expect at least two invocations.
      await waitFor(() => {
        expect(screen.queryByTestId("connection-feed-empty")).toBeInTheDocument();
      });
      expect(callCount).toBeGreaterThanOrEqual(2);
    });
  });

  // -------------------------------------------------------------------------
  // Render - loading state
  // -------------------------------------------------------------------------
  describe("Render - loading state", () => {
    it("renders without empty/error state while the query is pending", async () => {
      // Install a never-resolving handler so the query stays pending
      // for the duration of the assertion. Returning a Promise that
      // never resolves (`new Promise(() => {})`) is the canonical
      // way to drive React Query's `isPending` to true under MSW v2.
      server.use(http.get("*/api/connections", () => new Promise<HttpResponse>(() => {})));

      renderFeed();

      // The feed shell still mounts immediately - the loading state
      // lives inside the Table primitive (which renders 5 skeleton
      // rows when isLoading=true) and does NOT replace the page-level
      // header / filter bar.
      await screen.findByTestId("connection-feed");

      // Neither the empty-state nor the error-state should be present
      // while the query is pending; the Table's skeleton row pathway
      // is the only visible body affordance.
      expect(screen.queryByTestId("connection-feed-empty")).not.toBeInTheDocument();
      expect(screen.queryByTestId("connection-feed-error")).not.toBeInTheDocument();

      // The Table primitive also suppresses the "table-empty-state"
      // testid while loading (only renders it when data.length===0
      // AND isLoading===false). This is a defense-in-depth check on
      // the loading-state branch in Table.tsx.
      expect(screen.queryByTestId("table-empty-state")).not.toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // Filter - search by name (q -> full_name_search)
  // -------------------------------------------------------------------------
  describe("Filter - search by name (q)", () => {
    it("typing in the search input adds q= to the URL and refetches with full_name_search=", async () => {
      // Capture every backend request URL so we can verify the
      // serialized query string after the filter change.
      const capturedUrls: string[] = [];
      server.use(
        http.get("*/api/connections", ({ request }) => {
          capturedUrls.push(request.url);
          return HttpResponse.json(makePaginatedConnections([], { total: 0 }));
        }),
      );

      const user = userEvent.setup();
      renderFeed();

      await screen.findByTestId("connection-feed");

      // The Input primitive forwards data-testid to the underlying
      // HTMLInputElement so user.type fires native input events.
      const searchInput = screen.getByTestId("connection-feed-filter-search");
      await user.type(searchInput, "Jane");

      // URL state observable via the LocationCapture harness. The
      // FilterBar uses URL key "q" (not "full_name_search") so the
      // shareable URL stays compact; the API hook translates "q" to
      // "full_name_search" before serializing the backend request.
      await waitFor(() => {
        expect(screen.getByTestId("location-search").textContent).toContain("q=Jane");
      });

      // Backend translation: the API hook converts URL "q" to the
      // backend's "full_name_search" param (per the source's
      // paramsToListParams + buildConnectionListQuery contract).
      await waitFor(() => {
        expect(capturedUrls.some((url) => url.includes("full_name_search=Jane"))).toBe(true);
      });
    });
  });

  // -------------------------------------------------------------------------
  // Filter - company text
  // -------------------------------------------------------------------------
  describe("Filter - company text", () => {
    it("typing in the company input adds company= to the URL and to the backend request", async () => {
      const capturedUrls: string[] = [];
      server.use(
        http.get("*/api/connections", ({ request }) => {
          capturedUrls.push(request.url);
          return HttpResponse.json(makePaginatedConnections([], { total: 0 }));
        }),
      );

      const user = userEvent.setup();
      renderFeed();

      await screen.findByTestId("connection-feed");
      const companyInput = screen.getByTestId("connection-feed-filter-company");
      await user.type(companyInput, "Acme");

      // URL state observable via LocationCapture - the URL key matches
      // the backend param exactly for the company filter (no rename).
      await waitFor(() => {
        expect(screen.getByTestId("location-search").textContent).toContain("company=Acme");
      });

      // Backend request URL ALSO contains company= unchanged.
      await waitFor(() => {
        expect(capturedUrls.some((url) => url.includes("company=Acme"))).toBe(true);
      });
    });
  });

  // -------------------------------------------------------------------------
  // Filter - date range
  // -------------------------------------------------------------------------
  describe("Filter - date range", () => {
    it("setting date-from adds from= to the URL and submission_date_from= to the backend", async () => {
      const capturedUrls: string[] = [];
      server.use(
        http.get("*/api/connections", ({ request }) => {
          capturedUrls.push(request.url);
          return HttpResponse.json(makePaginatedConnections([], { total: 0 }));
        }),
      );

      renderFeed();

      await screen.findByTestId("connection-feed");
      const dateFromInput = screen.getByTestId("connection-feed-filter-date-from");

      // Native type=date inputs do not accept user.type (they require
      // strict format validation that user-event simulates poorly).
      // fireEvent.change is the canonical workaround in the project's
      // existing test suite.
      fireEvent.change(dateFromInput, { target: { value: "2026-01-01" } });

      // URL state uses the short key "from".
      await waitFor(() => {
        expect(screen.getByTestId("location-search").textContent).toContain("from=2026-01-01");
      });

      // Backend request URL uses the long key "submission_date_from"
      // (the API hook translates "from" -> "submission_date_from").
      await waitFor(() => {
        expect(capturedUrls.some((url) => url.includes("submission_date_from=2026-01-01"))).toBe(
          true,
        );
      });
    });

    it("setting date-to adds to= to the URL and submission_date_to= to the backend", async () => {
      const capturedUrls: string[] = [];
      server.use(
        http.get("*/api/connections", ({ request }) => {
          capturedUrls.push(request.url);
          return HttpResponse.json(makePaginatedConnections([], { total: 0 }));
        }),
      );

      renderFeed();

      await screen.findByTestId("connection-feed");
      const dateToInput = screen.getByTestId("connection-feed-filter-date-to");
      fireEvent.change(dateToInput, { target: { value: "2026-12-31" } });

      await waitFor(() => {
        expect(screen.getByTestId("location-search").textContent).toContain("to=2026-12-31");
      });

      await waitFor(() => {
        expect(capturedUrls.some((url) => url.includes("submission_date_to=2026-12-31"))).toBe(
          true,
        );
      });
    });
  });

  // -------------------------------------------------------------------------
  // Filter - involvement multi-select
  // -------------------------------------------------------------------------
  describe("Filter - involvement multi-select", () => {
    it("clicking an involvement chip flips aria-pressed and adds it to the URL", async () => {
      const capturedUrls: string[] = [];
      server.use(
        http.get("*/api/connections", ({ request }) => {
          capturedUrls.push(request.url);
          return HttpResponse.json(makePaginatedConnections([], { total: 0 }));
        }),
      );

      const user = userEvent.setup();
      renderFeed();

      await screen.findByTestId("connection-feed");
      const warmIntroChip = screen.getByTestId("connection-feed-filter-involvement-warm-intro");

      // Initial state: not selected (aria-pressed="false").
      expect(warmIntroChip).toHaveAttribute("aria-pressed", "false");

      await user.click(warmIntroChip);

      // After click: aria-pressed flips to true.
      await waitFor(() => {
        expect(warmIntroChip).toHaveAttribute("aria-pressed", "true");
      });

      // URL gets the encoded enum value. URLSearchParams encodes the
      // space in "Warm Intro" as either "+" or "%20" depending on the
      // route version; both are valid - we accept either.
      await waitFor(() => {
        const search = screen.getByTestId("location-search").textContent ?? "";
        expect(
          search.includes("involvement=Warm+Intro") || search.includes("involvement=Warm%20Intro"),
        ).toBe(true);
      });

      // Backend request also encodes the involvement param (URLSearchParams
      // serializes the same way on both sides; we accept either form).
      await waitFor(() => {
        const matched = capturedUrls.some(
          (url) =>
            url.includes("involvement=Warm+Intro") || url.includes("involvement=Warm%20Intro"),
        );
        expect(matched).toBe(true);
      });
    });

    it("clicking an already-selected involvement chip removes it from the URL", async () => {
      const user = userEvent.setup();
      // Seed the URL with an active involvement filter so the chip
      // mounts in the selected state.
      renderFeed(["/feed?involvement=Warm+Intro"]);

      await screen.findByTestId("connection-feed");
      const warmIntroChip = screen.getByTestId("connection-feed-filter-involvement-warm-intro");

      // Initial state: selected.
      expect(warmIntroChip).toHaveAttribute("aria-pressed", "true");

      await user.click(warmIntroChip);

      // After click: deselected.
      await waitFor(() => {
        expect(warmIntroChip).toHaveAttribute("aria-pressed", "false");
      });

      // URL no longer carries the involvement param.
      await waitFor(() => {
        expect(screen.getByTestId("location-search").textContent).not.toContain("involvement=");
      });
    });

    it("supports selecting multiple involvement values simultaneously", async () => {
      const user = userEvent.setup();
      renderFeed();

      await screen.findByTestId("connection-feed");
      const warmIntroChip = screen.getByTestId("connection-feed-filter-involvement-warm-intro");
      const softReferenceChip = screen.getByTestId(
        "connection-feed-filter-involvement-soft-reference",
      );

      await user.click(warmIntroChip);
      await waitFor(() => {
        expect(warmIntroChip).toHaveAttribute("aria-pressed", "true");
      });

      await user.click(softReferenceChip);
      await waitFor(() => {
        expect(softReferenceChip).toHaveAttribute("aria-pressed", "true");
      });

      // Both chips remain pressed; URL contains two repeated keys
      // (URLSearchParams supports duplicate keys for "any-of" semantics).
      expect(warmIntroChip).toHaveAttribute("aria-pressed", "true");

      await waitFor(() => {
        const search = screen.getByTestId("location-search").textContent ?? "";
        // Count "involvement=" occurrences. "any-of" semantics rely
        // on multiple URL keys; this guard ensures both chips wrote
        // their values into the URL.
        const matches = search.match(/involvement=/g);
        expect(matches).not.toBeNull();
        expect(matches?.length).toBe(2);
      });
    });
  });

  // -------------------------------------------------------------------------
  // Filter - outreach status multi-select
  // -------------------------------------------------------------------------
  describe("Filter - outreach status multi-select", () => {
    it("clicking a status chip flips aria-pressed and adds outreach_status= to the URL", async () => {
      const user = userEvent.setup();
      renderFeed();

      await screen.findByTestId("connection-feed");
      const inProgressChip = screen.getByTestId("connection-feed-filter-status-in-progress");

      expect(inProgressChip).toHaveAttribute("aria-pressed", "false");

      await user.click(inProgressChip);

      await waitFor(() => {
        expect(inProgressChip).toHaveAttribute("aria-pressed", "true");
      });

      await waitFor(() => {
        const search = screen.getByTestId("location-search").textContent ?? "";
        expect(
          search.includes("outreach_status=In+Progress") ||
            search.includes("outreach_status=In%20Progress"),
        ).toBe(true);
      });
    });

    it("clicking the same status chip again toggles it off", async () => {
      const user = userEvent.setup();
      renderFeed(["/feed?outreach_status=Not+Started"]);

      await screen.findByTestId("connection-feed");
      const notStartedChip = screen.getByTestId("connection-feed-filter-status-not-started");
      expect(notStartedChip).toHaveAttribute("aria-pressed", "true");

      await user.click(notStartedChip);

      await waitFor(() => {
        expect(notStartedChip).toHaveAttribute("aria-pressed", "false");
      });
      await waitFor(() => {
        expect(screen.getByTestId("location-search").textContent).not.toContain("outreach_status=");
      });
    });
  });

  // -------------------------------------------------------------------------
  // Filter - tags multi-select
  // -------------------------------------------------------------------------
  describe("Filter - tags multi-select", () => {
    it("renders tag chips after the tags query loads and adds tag= when clicked", async () => {
      const capturedUrls: string[] = [];
      server.use(
        http.get("*/api/connections", ({ request }) => {
          capturedUrls.push(request.url);
          return HttpResponse.json(makePaginatedConnections([], { total: 0 }));
        }),
      );

      const user = userEvent.setup();
      renderFeed();

      await screen.findByTestId("connection-feed");

      // Tags arrive via a separate useTagsQuery call; wait for any
      // tag chip to render before interacting. The default handler
      // for /api/tags returns 3 tags ("industry:saas", "use-case:demand-gen",
      // "geography:nyc"); the chip testid encodes the tag id.
      const firstChip = await waitFor(
        () => {
          const chips = document.querySelectorAll('[data-testid^="connection-feed-filter-tag-"]');
          if (chips.length === 0) {
            throw new Error("No tag chips yet");
          }
          return chips[0] as HTMLElement;
        },
        { timeout: 3000 },
      );

      expect(firstChip).toHaveAttribute("aria-pressed", "false");
      const tagId = firstChip
        .getAttribute("data-testid")
        ?.replace("connection-feed-filter-tag-", "");
      expect(tagId).toBeTruthy();

      await user.click(firstChip);

      await waitFor(() => {
        expect(firstChip).toHaveAttribute("aria-pressed", "true");
      });

      // URL key for tags is the singular "tag"; backend key is "tag_ids".
      await waitFor(() => {
        expect(screen.getByTestId("location-search").textContent).toContain(`tag=${tagId}`);
      });
      await waitFor(() => {
        expect(capturedUrls.some((url) => url.includes(`tag_ids=${tagId}`))).toBe(true);
      });
    });
  });

  // -------------------------------------------------------------------------
  // Filter - clear all
  // -------------------------------------------------------------------------
  describe("Filter - clear all", () => {
    it("renders the Clear all button only when at least one filter is active", async () => {
      // Case A: no filters - clear-all is hidden.
      renderFeed(["/feed"]);
      await screen.findByTestId("connection-feed");
      expect(screen.queryByTestId("connection-feed-clear-filters")).not.toBeInTheDocument();
    });

    it("clicking Clear all removes every filter while preserving sort and page_size", async () => {
      const user = userEvent.setup();
      renderFeed([
        "/feed?company=Acme&q=Jane&from=2026-01-01&involvement=Warm+Intro&sort=full_name&sort_dir=asc&page_size=50",
      ]);

      // The clear-all button mounts when at least one filter is active.
      const clearBtn = await screen.findByTestId("connection-feed-clear-filters");
      expect(clearBtn).toBeInTheDocument();

      await user.click(clearBtn);

      await waitFor(() => {
        const search = screen.getByTestId("location-search").textContent ?? "";
        // Filter params removed.
        expect(search).not.toContain("company=");
        expect(search).not.toContain("q=");
        expect(search).not.toContain("from=");
        expect(search).not.toContain("involvement=");
        // Sort and page_size preserved.
        expect(search).toContain("sort=full_name");
        expect(search).toContain("sort_dir=asc");
        expect(search).toContain("page_size=50");
      });

      // The Clear all button itself disappears once filters are cleared.
      await waitFor(() => {
        expect(screen.queryByTestId("connection-feed-clear-filters")).not.toBeInTheDocument();
      });

      // Filter inputs are also visually reset to empty.
      expect(screen.getByTestId("connection-feed-filter-search")).toHaveValue("");
      expect(screen.getByTestId("connection-feed-filter-company")).toHaveValue("");
      expect(screen.getByTestId("connection-feed-filter-date-from")).toHaveValue("");
    });
  });

  // -------------------------------------------------------------------------
  // Sort - by columns
  // -------------------------------------------------------------------------
  describe("Sort - by columns", () => {
    it("seeds backend request with the URL's sort and sort_dir on initial mount", async () => {
      const capturedUrls: string[] = [];
      server.use(
        http.get("*/api/connections", ({ request }) => {
          capturedUrls.push(request.url);
          return HttpResponse.json(makePaginatedConnections([], { total: 0 }));
        }),
      );

      renderFeed(["/feed?sort=full_name&sort_dir=asc"]);

      await screen.findByTestId("connection-feed");
      // Default sort is overridden by the URL: backend receives
      // sort=full_name&sort_dir=asc (not the submission_date desc default).
      await waitFor(() => {
        expect(
          capturedUrls.some(
            (url) => url.includes("sort=full_name") && url.includes("sort_dir=asc"),
          ),
        ).toBe(true);
      });
    });

    it("clicking a column-header sort button rewrites the URL", async () => {
      const user = userEvent.setup();
      renderFeed();

      await screen.findByTestId("connection-feed");

      // The Table primitive renders a sort button per sortable column
      // with data-testid="table-header-sort-{column.key}". The full_name
      // column's button toggles the sort.
      const fullNameSortBtn = await screen.findByTestId("table-header-sort-full_name");
      await user.click(fullNameSortBtn);

      // After the click, the URL must contain sort=full_name (the exact
      // direction depends on the Table primitive's three-state sort
      // cycle: asc -> desc -> none -> asc; we only assert the column
      // was selected, not the cycle phase).
      await waitFor(() => {
        expect(screen.getByTestId("location-search").textContent).toContain("sort=full_name");
      });
    });
  });

  // -------------------------------------------------------------------------
  // Pagination
  // -------------------------------------------------------------------------
  describe("Pagination", () => {
    it("changing the page-size Select rewrites page_size and resets page", async () => {
      const user = userEvent.setup();
      // Seed the URL at page=3, page_size=25 so the reset is observable.
      // Provide enough records that pagination is meaningful (total=200).
      server.use(
        http.get("*/api/connections", () =>
          HttpResponse.json(
            makePaginatedConnections(makeConnectionList(25), { total: 200, limit: 25, offset: 50 }),
          ),
        ),
      );
      renderFeed(["/feed?page=3&page_size=25"]);

      await screen.findByTestId("connection-feed-pagination");

      const pageSizeSelect = screen.getByTestId("connection-feed-page-size");
      await user.selectOptions(pageSizeSelect, "50");

      await waitFor(() => {
        const search = screen.getByTestId("location-search").textContent ?? "";
        expect(search).toContain("page_size=50");
        // page=3 removed -> reset to default page 1.
        expect(search).not.toContain("page=3");
      });
    });

    it("clicking Next advances to page=2 when more pages are available", async () => {
      const user = userEvent.setup();
      // total=100 with page_size=25 yields 4 pages, so Next is enabled.
      server.use(
        http.get("*/api/connections", () =>
          HttpResponse.json(
            makePaginatedConnections(makeConnectionList(25), { total: 100, limit: 25, offset: 0 }),
          ),
        ),
      );
      renderFeed(["/feed?page=1&page_size=25"]);

      await screen.findByTestId("connection-feed-pagination");
      const next = screen.getByTestId("connection-feed-next-page");
      expect(next).toBeEnabled();

      await user.click(next);

      await waitFor(() => {
        expect(screen.getByTestId("location-search").textContent).toContain("page=2");
      });
    });

    it("disables the Previous button at page 1", async () => {
      // Default handler (10 records) plus seeded page=1; Previous
      // should be disabled because page <= 1.
      renderFeed(["/feed?page=1"]);
      await screen.findByTestId("connection-feed-pagination");
      expect(screen.getByTestId("connection-feed-prev-page")).toBeDisabled();
    });

    it("disables the Next button on the last page", async () => {
      // total=10 with page_size=25 -> totalPages=1, so Next is disabled.
      server.use(
        http.get("*/api/connections", () =>
          HttpResponse.json(
            makePaginatedConnections(makeConnectionList(10), { total: 10, limit: 25, offset: 0 }),
          ),
        ),
      );
      renderFeed(["/feed?page=1&page_size=25"]);

      await screen.findByTestId("connection-feed-pagination");
      expect(screen.getByTestId("connection-feed-next-page")).toBeDisabled();
    });
  });

  // -------------------------------------------------------------------------
  // Add Connection CTA
  // -------------------------------------------------------------------------
  describe("Add Connection CTA", () => {
    it("wraps the button in a Link to /connections/new", async () => {
      renderFeed();
      // The wrapping Link carries connection-feed-add-connection-link;
      // assert its href points to the create form route.
      const link = await screen.findByTestId("connection-feed-add-connection-link");
      expect(link).toHaveAttribute("href", "/connections/new");
      // The inner Button is also present.
      expect(within(link).getByTestId("connection-feed-add-connection")).toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // Row navigation
  // -------------------------------------------------------------------------
  describe("Row navigation", () => {
    it("clicking a non-link cell of a row fires navigate('/connections/:id')", async () => {
      const record = makeConnectionRead({ id: "rec-X", full_name: "Name X" });
      server.use(
        http.get("*/api/connections", () =>
          HttpResponse.json(makePaginatedConnections([record], { total: 1 })),
        ),
      );

      const user = userEvent.setup();
      renderFeed();

      // Wait for the row to render via the Table primitive's row testid.
      const tableRow = await screen.findByTestId("table-row-rec-X");

      // Click on a cell that is NOT the name link - the company cell
      // is a safe choice (no link, no inline interactive control).
      // The Table primitive's onRowClick fires navigate(`/connections/${row.id}`)
      // for any click that bubbles up through the row.
      const companyCell = within(tableRow).getByTestId("connection-feed-row-company-rec-X");
      await user.click(companyCell);

      // navigateMock is called with the canonical detail route.
      await waitFor(() => {
        expect(navigateMock).toHaveBeenCalledWith("/connections/rec-X");
      });
    });

    it("the row name Link carries href='/connections/:id'", async () => {
      const record = makeConnectionRead({ id: "rec-Y" });
      server.use(
        http.get("*/api/connections", () =>
          HttpResponse.json(makePaginatedConnections([record], { total: 1 })),
        ),
      );

      renderFeed();

      const link = await screen.findByTestId("connection-feed-row-name-rec-Y");
      // The inline name uses a real <Link> so the href round-trips
      // through react-router-dom (assert via attribute, NOT via
      // navigateMock - clicking the Link does not flow through
      // useNavigate).
      expect(link).toHaveAttribute("href", "/connections/rec-Y");
    });
  });

  // -------------------------------------------------------------------------
  // Filter changes reset page to 1
  // -------------------------------------------------------------------------
  describe("Filter changes reset page to 1", () => {
    it("typing in search after page=3 drops the page param from the URL", async () => {
      const user = userEvent.setup();
      // Provide enough data that page=3 is meaningful (total=100).
      server.use(
        http.get("*/api/connections", () =>
          HttpResponse.json(
            makePaginatedConnections(makeConnectionList(25), { total: 100, limit: 25, offset: 50 }),
          ),
        ),
      );
      renderFeed(["/feed?page=3&page_size=25"]);

      await screen.findByTestId("connection-feed");

      // Confirm the seeded page is reflected in the URL before mutation.
      expect(screen.getByTestId("location-search").textContent).toContain("page=3");

      const searchInput = screen.getByTestId("connection-feed-filter-search");
      await user.type(searchInput, "Q");

      // After the filter change, the page param is removed (reset to
      // default page 1) so deep-paginated views do not point past the
      // last page of a now-narrower result set.
      await waitFor(() => {
        const search = screen.getByTestId("location-search").textContent ?? "";
        expect(search).toContain("q=Q");
        expect(search).not.toContain("page=3");
      });
    });
  });
});
