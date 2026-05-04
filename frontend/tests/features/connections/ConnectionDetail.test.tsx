/**
 * ConnectionDetail.test.tsx - Vitest tests for F-011 Connection Detail View.
 *
 * Targets `frontend/src/features/connections/ConnectionDetail.tsx`. The
 * component (per AAP Sec 0.5.4 Screen 3 / F-011) renders a single
 * connection record at `/connections/:id` and exposes:
 *
 *   - Loading state with role="status" + aria-busy="true" while the
 *     record query is pending.
 *   - Error states distinguishing 404 ("Connection not found" with help
 *     text) from generic 5xx ("Failed to load connection" with the
 *     server message + Retry button).
 *   - All nine business fields plus owner attribution, tags,
 *     submission date, and updated_at metadata in a definition list.
 *   - Composed sibling components: InvolvementBadge, StatusChip,
 *     EditHistoryFeed - each rendered with the record's authoritative
 *     values.
 *   - Soft-deleted record presentation: a "Soft-deleted on ..." Badge
 *     in the header, edit/delete actions hidden, and the StatusChip
 *     locked via its disabled prop.
 *   - Role-gated edit/soft-delete buttons per the canEditRecord helper:
 *       Admin           -> always shown
 *       Contributor     -> only on OWN records
 *       Contributor (other's record) -> hidden
 *       Viewer          -> always hidden
 *   - Soft-delete confirmation modal with pending-mutation safeguards
 *     (Cancel disabled while in flight; modal cannot be dismissed
 *     during pending; confirm button shows loading spinner).
 *   - LinkedIn URL link with target="_blank" and rel="noopener
 *     noreferrer" for external-link security (F-001 invariant).
 *   - Tag list rendered as Badge chips with names; empty tags collapse
 *     to a "No tags" placeholder.
 *   - Back-to-feed breadcrumb link.
 *
 * Test infrastructure:
 *   - vi.mock('react-router-dom', ...) replaces useNavigate with a
 *     module-level navigateMock spy and useParams with a mutable
 *     mockParams object so tests can drive the URL :id parameter
 *     without a complex routing harness. The mock uses
 *     vi.importActual so MemoryRouter, Routes, Route, Link continue
 *     to work for the breadcrumb assertion.
 *   - renderWithMockedSession provides a synchronous role override
 *     so the canEditRecord gate (Admin / Contributor-own /
 *     Contributor-other / Viewer / null) can be tested without
 *     waiting for /api/me to settle.
 *   - MSW handlers under `overrides.connections.detail404` and
 *     inline http.get/http.delete swap the default-success behavior
 *     for specific failure branches per test.
 *   - The default GET /api/connections/:id/history handler from
 *     handlers.ts returns 5 entries so EditHistoryFeed renders
 *     normally inside the detail view; tests for the detail view
 *     do NOT need to assert each entry's content (covered by the
 *     dedicated EditHistoryFeed test file).
 *
 * Coverage budget per AAP Sec 0.7.7: this suite drives
 * ConnectionDetail.tsx to >=85% statements/lines/functions and >=80%
 * branches via ~30 tests across 13 describe blocks.
 *
 * Conventions per AAP Sec 0.7.7 and frontend/.prettierrc.json:
 *   - Strict TypeScript; no `any`.
 *   - Double quotes (project Prettier config: singleQuote: false);
 *     trailing commas; 2-space indent; line length <= 100.
 *   - Named imports only; no default export (this is a test file
 *     consumed by vitest discover; no exports needed).
 *   - No emoji; no console.log.
 *   - All interactions use userEvent (NOT fireEvent).
 *   - Tests assert observable behavior (DOM, navigation, network),
 *     never implementation details.
 *
 * Coordinates with:
 *   - frontend/src/features/connections/ConnectionDetail.tsx (system under test).
 *   - frontend/tests/test-utils.tsx (renderWithMockedSession + RTL re-exports).
 *   - frontend/tests/mocks/server.ts (MSW Node server instance).
 *   - frontend/tests/mocks/handlers.ts (default + override handlers).
 *   - frontend/tests/mocks/data.ts (typed entity factories).
 *   - frontend/tests/setup.ts (jest-dom, MSW lifecycle, dialog polyfill).
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
// Routes/Route are imported per the agent prompt's Test Infrastructure
// Imports specification. They are used in the dedicated routing-harness
// smoke test below ("renders correctly inside a real Routes harness"),
// which verifies that ConnectionDetail mounts cleanly under a real
// react-router-dom <Routes>/<Route> wrapper - a sanity check that the
// partial mock of useParams/useNavigate does not break route-aware
// composition with the rest of the SPA.
import { Routes, Route } from "react-router-dom";
import { http, HttpResponse } from "msw";

// ---------------------------------------------------------------------------
// react-router-dom partial mock (useNavigate + useParams spies)
// ---------------------------------------------------------------------------
//
// vi.mock is hoisted ABOVE all imports by Vitest, so the mock is in
// place before ConnectionDetail.tsx is loaded. We use vi.importActual
// to retain MemoryRouter, Routes, Route, Link, useLocation, etc. -
// only useNavigate and useParams are replaced. This preserves the
// real Link rendering (so the breadcrumb's <Link to="/feed"> renders
// a normal <a href="/feed">) while still allowing tests to assert
// programmatic navigation triggered by softDelete.onSuccess and the
// error-state Back button.
//
// Per the AAP guidance:
//   - navigateMock is a module-level vi.fn() reset in beforeEach.
//   - mockParams is a mutable object reset to { id: 'rec-1' } in
//     beforeEach. Tests that need a different id reassign mockParams
//     before calling render.

const navigateMock = vi.fn();
let mockParams: { id?: string } = { id: "rec-1" };

vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>("react-router-dom");
  return {
    ...actual,
    useNavigate: () => navigateMock,
    useParams: () => mockParams,
  };
});

// ---------------------------------------------------------------------------
// Test infrastructure imports (AFTER vi.mock so the mock is applied)
// ---------------------------------------------------------------------------

import { server } from "../../mocks/server";
import { overrides } from "../../mocks/handlers";
import {
  makeConnectionRead,
  makeHistoryEntry,
  makePaginatedHistory,
  makeSessionRead,
  makeTagRead,
  makeUserRead,
} from "../../mocks/data";
import { renderWithMockedSession, screen, within, waitFor, userEvent } from "../../test-utils";
import { ConnectionDetail } from "@/features/connections/ConnectionDetail";

// ---------------------------------------------------------------------------
// Module-scoped helpers
// ---------------------------------------------------------------------------

/**
 * Build an Admin-role session pinned to a fixed user id.
 *
 * The user id ("admin-user-1") differs from the default record owner
 * ("user-1") so RBAC tests can distinguish "Admin viewing other's
 * record" from "Contributor viewing own record" without ambiguity.
 */
function adminSession() {
  return makeSessionRead({
    user: makeUserRead({
      id: "admin-user-1",
      role: "Admin",
      display_name: "Admin User",
      email: "admin@example.com",
    }),
  });
}

/**
 * Build a Contributor-role session pinned to user id "user-1".
 *
 * Tests pair this with a record whose owner_user_id is also "user-1"
 * to exercise the "Contributor on own record" branch (canEditRecord
 * returns true) or pair it with a different owner_user_id to exercise
 * the "Contributor on other's record" branch (canEditRecord returns
 * false).
 */
function contributorSession(userId = "user-1") {
  return makeSessionRead({
    user: makeUserRead({
      id: userId,
      role: "Contributor",
      display_name: "Contributor User",
      email: "contributor@example.com",
    }),
  });
}

/**
 * Build a Viewer-role session pinned to a fixed user id.
 *
 * Viewer never sees edit/delete buttons regardless of ownership; tests
 * verify that the StatusChip remains editable for Viewer (per F-005
 * RBAC matrix: Viewer/Admin may mutate status).
 */
function viewerSession() {
  return makeSessionRead({
    user: makeUserRead({
      id: "viewer-user-1",
      role: "Viewer",
      display_name: "Viewer User",
      email: "viewer@example.com",
    }),
  });
}

/**
 * Install an MSW handler that returns the supplied record from the
 * GET /api/connections/:id endpoint for the test's seeded record id.
 *
 * Wildcard host + path matches both relative URLs (default behavior of
 * the api/client.ts wrapper) and any absolute URL prefix the test
 * environment might inject. The handler matches ANY :id segment, so
 * tests do not need to align the URL with the mockParams.id value.
 */
function useRecordResponse(record: ReturnType<typeof makeConnectionRead>): void {
  server.use(
    http.get("*/api/connections/:id", ({ params }) => {
      // Re-export of msw's :id pattern: only respond for non-special
      // segments so the duplicate-check and history sub-paths are not
      // accidentally captured. The default handlers cover the more-
      // specific paths first; we only override the bare detail route.
      if (params.id === "duplicate-check") {
        return undefined;
      }
      return HttpResponse.json(record);
    }),
  );
}

// ---------------------------------------------------------------------------
// beforeEach: reset module-level mocks
// ---------------------------------------------------------------------------

beforeEach(() => {
  navigateMock.mockClear();
  mockParams = { id: "rec-1" };
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("<ConnectionDetail />", () => {
  // -------------------------------------------------------------------------
  // Loading state
  // -------------------------------------------------------------------------
  describe("Loading state", () => {
    it("renders a loading region while the record query is pending", async () => {
      // Pending-forever handler so the query never resolves; this lets
      // us assert on the loading state without a race against settle.
      server.use(http.get("*/api/connections/:id", () => new Promise(() => {})));

      renderWithMockedSession(<ConnectionDetail />, adminSession());

      const loading = await screen.findByTestId("connection-detail-loading");
      expect(loading).toBeInTheDocument();
      expect(loading).toHaveAttribute("role", "status");
      expect(loading).toHaveAttribute("aria-busy", "true");
      expect(loading).toHaveTextContent(/loading connection/i);
    });
  });

  // -------------------------------------------------------------------------
  // Error states
  // -------------------------------------------------------------------------
  describe("Error states", () => {
    it("shows the 404 friendly message and Back-to-feed button when record is not found", async () => {
      server.use(overrides.connections.detail404("rec-1"));

      renderWithMockedSession(<ConnectionDetail />, adminSession());

      const error = await screen.findByTestId("connection-detail-error");
      expect(error).toBeInTheDocument();
      expect(error).toHaveAttribute("role", "alert");

      // 404 branch: friendly heading + dedicated help text.
      expect(within(error).getByText(/connection not found/i)).toBeInTheDocument();
      expect(
        within(error).getByText(
          /this record may have been deleted or you may not have access to it/i,
        ),
      ).toBeInTheDocument();

      // 404 branch: only the Back-to-feed button is rendered (no Retry).
      const backButton = screen.getByTestId("connection-detail-error-back");
      expect(backButton).toBeInTheDocument();
      expect(screen.queryByTestId("connection-detail-error-retry")).not.toBeInTheDocument();

      // Clicking the back button navigates to /feed.
      const user = userEvent.setup();
      await user.click(backButton);
      expect(navigateMock).toHaveBeenCalledWith("/feed");
    });

    it("shows the generic error message and Retry button when the server returns 500", async () => {
      server.use(
        http.get("*/api/connections/:id", () =>
          HttpResponse.json(
            {
              error: {
                code: "internal_error",
                message: "Internal Server Error",
                correlation_id: "test-correlation-id",
                fields: [],
              },
            },
            { status: 500 },
          ),
        ),
      );

      renderWithMockedSession(<ConnectionDetail />, adminSession());

      const error = await screen.findByTestId("connection-detail-error");
      expect(error).toBeInTheDocument();
      expect(error).toHaveAttribute("role", "alert");

      // Generic-error branch: distinct heading + the actual server message.
      expect(within(error).getByText(/failed to load connection/i)).toBeInTheDocument();
      expect(within(error).queryByText(/connection not found/i)).not.toBeInTheDocument();
      expect(within(error).getByText(/internal server error/i)).toBeInTheDocument();

      // Generic-error branch: BOTH the Back and Retry buttons are rendered.
      expect(screen.getByTestId("connection-detail-error-back")).toBeInTheDocument();
      expect(screen.getByTestId("connection-detail-error-retry")).toBeInTheDocument();
    });

    it("clicking Retry on a generic error refetches the record query", async () => {
      // First call returns 500; second call returns a successful record.
      let callCount = 0;
      server.use(
        http.get("*/api/connections/:id", () => {
          callCount += 1;
          if (callCount === 1) {
            return HttpResponse.json(
              {
                error: {
                  code: "internal_error",
                  message: "Internal Server Error",
                  correlation_id: "test-correlation-id",
                  fields: [],
                },
              },
              { status: 500 },
            );
          }
          return HttpResponse.json(makeConnectionRead({ id: "rec-1", full_name: "Refetched" }));
        }),
      );

      renderWithMockedSession(<ConnectionDetail />, adminSession());

      // Wait for the initial error.
      await screen.findByTestId("connection-detail-error");

      const retry = screen.getByTestId("connection-detail-error-retry");
      const user = userEvent.setup();
      await user.click(retry);

      // After retry the detail view should render successfully.
      await screen.findByTestId("connection-detail");
      expect(screen.getByText("Refetched")).toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // Render - all nine business fields plus metadata
  // -------------------------------------------------------------------------
  describe("Render - all nine fields", () => {
    it("renders every business field plus owner, dates, and tags", async () => {
      const record = makeConnectionRead({
        id: "rec-1",
        full_name: "Jane Doe",
        company: "Acme Corp",
        job_title: "VP Operations",
        linkedin_url: "https://www.linkedin.com/in/jane-doe",
        relationship_context: "College roommate; now leading ops at a logistics startup.",
        ai_notes: "Suggested: 1. Reference college days. 2. Acknowledge her growth.",
        involvement: "Warm Intro",
        outreach_status: "In Progress",
        owner_user_id: "user-1",
        owner_display_name: "Alice Admin",
        submission_date: "2026-04-01T12:00:00+00:00",
        updated_at: "2026-04-15T08:30:00+00:00",
        deleted_at: null,
        tags: [
          makeTagRead({ id: "tag-1", name: "industry:saas" }),
          makeTagRead({ id: "tag-2", name: "geography:nyc" }),
        ],
      });
      useRecordResponse(record);

      renderWithMockedSession(<ConnectionDetail />, adminSession());

      const detail = await screen.findByTestId("connection-detail");
      expect(detail).toBeInTheDocument();

      // Field 1: full_name -> h1 heading.
      const heading = screen.getByTestId("connection-detail-name");
      expect(heading.tagName).toBe("H1");
      expect(heading).toHaveTextContent("Jane Doe");

      // Subtitle: "<job_title> at <company>".
      const companyTitle = screen.getByTestId("connection-detail-company-title");
      expect(companyTitle).toHaveTextContent(/VP Operations/);
      expect(companyTitle).toHaveTextContent(/Acme Corp/);

      // Field 2: linkedin_url -> external link with security attributes.
      const linkedinLink = screen.getByTestId("connection-detail-linkedin-link");
      expect(linkedinLink).toHaveAttribute("href", "https://www.linkedin.com/in/jane-doe");
      expect(linkedinLink).toHaveAttribute("target", "_blank");
      const rel = linkedinLink.getAttribute("rel") ?? "";
      expect(rel).toMatch(/noopener/);
      expect(rel).toMatch(/noreferrer/);
      // Visible link text contains the URL.
      expect(linkedinLink).toHaveTextContent("https://www.linkedin.com/in/jane-doe");

      // Field 3: company -> rendered inside <dd> within the company field.
      const companyField = screen.getByTestId("connection-detail-field-company");
      expect(within(companyField).getByText("Acme Corp")).toBeInTheDocument();

      // Field 4: job_title -> rendered inside <dd> within the job-title field.
      const jobTitleField = screen.getByTestId("connection-detail-field-job-title");
      expect(within(jobTitleField).getByText("VP Operations")).toBeInTheDocument();

      // Field 5: relationship_context -> field wrapper carries the text.
      const relContext = screen.getByTestId("connection-detail-field-relationship-context");
      expect(relContext).toHaveTextContent(
        "College roommate; now leading ops at a logistics startup.",
      );

      // Field 6: ai_notes -> field wrapper carries the AI text.
      const aiNotes = screen.getByTestId("connection-detail-field-ai-notes");
      expect(aiNotes).toHaveTextContent(
        "Suggested: 1. Reference college days. 2. Acknowledge her growth.",
      );

      // Field 7: involvement -> InvolvementBadge rendered with variant suffix.
      // (The detailed assertion is in a dedicated composed-siblings test below.)
      expect(screen.getByTestId("involvement-badge-involvement-warm-intro")).toBeInTheDocument();

      // Field 8: submission_date -> rendered via the date helper.
      const submissionDate = screen.getByTestId("connection-detail-submission-date");
      // Locale-flexible match: any format that includes 2026 and Apr is acceptable.
      expect(submissionDate.textContent ?? "").toMatch(/2026/);
      expect(submissionDate.textContent ?? "").toMatch(/Apr/);

      // Field 9: outreach_status -> StatusChip is rendered with the recordId.
      // (Detailed assertions in the composed-siblings describe below.)
      // For Admin role the editable Select is rendered.
      expect(screen.getByTestId("status-chip-select-rec-1")).toBeInTheDocument();

      // Owner (denormalized, F-006) -> field wrapper + submitter line.
      const ownerField = screen.getByTestId("connection-detail-field-owner");
      expect(within(ownerField).getByText("Alice Admin")).toBeInTheDocument();
      const submitter = screen.getByTestId("connection-detail-submitter");
      expect(submitter).toHaveTextContent("Alice Admin");

      // Tags (F-008) -> badge chips containing each tag name.
      const tagsList = screen.getByTestId("connection-detail-tags");
      expect(within(tagsList).getByText("industry:saas")).toBeInTheDocument();
      expect(within(tagsList).getByText("geography:nyc")).toBeInTheDocument();
    });

    it("shows the empty-notes placeholder when ai_notes is null", async () => {
      const record = makeConnectionRead({ id: "rec-1", ai_notes: null });
      useRecordResponse(record);

      renderWithMockedSession(<ConnectionDetail />, adminSession());

      await screen.findByTestId("connection-detail");

      // The ai-notes field wrapper still renders (it carries the label
      // and italic placeholder), but the embedded paragraph that would
      // hold the AI text is replaced by a placeholder span.
      const aiField = screen.getByTestId("connection-detail-field-ai-notes");
      expect(within(aiField).getByText(/no outreach notes yet/i)).toBeInTheDocument();
    });

    it("shows the 'No tags' placeholder when the tag list is empty", async () => {
      const record = makeConnectionRead({ id: "rec-1", tags: [] });
      useRecordResponse(record);

      renderWithMockedSession(<ConnectionDetail />, adminSession());

      await screen.findByTestId("connection-detail");

      // The tags-list <ul> is suppressed entirely when the list is empty.
      expect(screen.queryByTestId("connection-detail-tags")).not.toBeInTheDocument();

      // Instead, a "No tags" placeholder appears inside the field wrapper.
      const tagsField = screen.getByTestId("connection-detail-field-tags");
      expect(within(tagsField).getByText(/no tags/i)).toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // Render - composed sibling components
  // -------------------------------------------------------------------------
  describe("Render - composed siblings", () => {
    it("renders InvolvementBadge with the Soft Reference variant", async () => {
      const record = makeConnectionRead({ id: "rec-1", involvement: "Soft Reference" });
      useRecordResponse(record);

      renderWithMockedSession(<ConnectionDetail />, adminSession());

      await screen.findByTestId("connection-detail");
      expect(
        screen.getByTestId("involvement-badge-involvement-soft-reference"),
      ).toBeInTheDocument();
    });

    it("renders InvolvementBadge with the Target Only variant", async () => {
      const record = makeConnectionRead({ id: "rec-1", involvement: "Target Only" });
      useRecordResponse(record);

      renderWithMockedSession(<ConnectionDetail />, adminSession());

      await screen.findByTestId("connection-detail");
      expect(screen.getByTestId("involvement-badge-involvement-target-only")).toBeInTheDocument();
    });

    it("renders an editable StatusChip for Viewer role with the record id suffix", async () => {
      const record = makeConnectionRead({ id: "rec-1", outreach_status: "Contacted" });
      useRecordResponse(record);

      renderWithMockedSession(<ConnectionDetail />, viewerSession());

      await screen.findByTestId("connection-detail");
      // Viewer (Sales Rep) is in the StatusChip's RoleGate admit list,
      // so the editable Select is rendered with the record-id suffix.
      const select = screen.getByTestId("status-chip-select-rec-1");
      expect(select).toBeInTheDocument();
      // The select should NOT be disabled because the record is active.
      expect(select).not.toBeDisabled();
    });

    it("disables the StatusChip Select when the record is soft-deleted", async () => {
      const record = makeConnectionRead({
        id: "rec-1",
        deleted_at: "2026-04-20T00:00:00+00:00",
      });
      useRecordResponse(record);

      renderWithMockedSession(<ConnectionDetail />, viewerSession());

      await screen.findByTestId("connection-detail");
      const select = screen.getByTestId("status-chip-select-rec-1");
      expect(select).toBeDisabled();
    });

    it("renders the EditHistoryFeed inside the history section", async () => {
      const record = makeConnectionRead({ id: "rec-1" });
      useRecordResponse(record);

      // Override the history endpoint to return three entries so we can
      // assert on the populated path of EditHistoryFeed (rather than the
      // empty-state branch).
      server.use(
        http.get("*/api/connections/:id/history", () =>
          HttpResponse.json(
            makePaginatedHistory([
              makeHistoryEntry({ event_type: "create" }),
              makeHistoryEntry({ event_type: "edit" }),
              makeHistoryEntry({ event_type: "status_change" }),
            ]),
          ),
        ),
      );

      renderWithMockedSession(<ConnectionDetail />, adminSession());

      await screen.findByTestId("connection-detail");

      const historySection = screen.getByTestId("connection-detail-history");
      expect(historySection).toBeInTheDocument();
      expect(within(historySection).getByText(/edit history/i)).toBeInTheDocument();

      // Wait for the history feed to settle and assert the populated wrapper.
      const feed = await screen.findByTestId("edit-history-feed");
      expect(historySection.contains(feed)).toBe(true);
    });
  });

  // -------------------------------------------------------------------------
  // Soft-deleted record rendering
  // -------------------------------------------------------------------------
  describe("Soft-deleted record rendering", () => {
    it("shows a 'Soft-deleted on ...' badge in the header", async () => {
      const record = makeConnectionRead({
        id: "rec-1",
        deleted_at: "2026-04-20T08:00:00+00:00",
      });
      useRecordResponse(record);

      renderWithMockedSession(<ConnectionDetail />, adminSession());

      await screen.findByTestId("connection-detail");

      // Locale-flexible: just look for "Soft-deleted on" prefix and the
      // year somewhere in the badge text.
      const softDeletedBadge = screen.getByText(/soft-deleted on/i);
      expect(softDeletedBadge).toBeInTheDocument();
      expect(softDeletedBadge.textContent ?? "").toMatch(/2026/);
    });

    it("hides edit/delete buttons for soft-deleted records even for Admin", async () => {
      const record = makeConnectionRead({
        id: "rec-1",
        deleted_at: "2026-04-20T08:00:00+00:00",
      });
      useRecordResponse(record);

      renderWithMockedSession(<ConnectionDetail />, adminSession());

      await screen.findByTestId("connection-detail");

      // Even Admins do not see edit/delete on a soft-deleted record;
      // hard delete and restore live in the Admin Panel per F-014.
      expect(screen.queryByTestId("connection-detail-edit")).not.toBeInTheDocument();
      expect(screen.queryByTestId("connection-detail-delete")).not.toBeInTheDocument();
      expect(screen.queryByTestId("connection-detail-actions")).not.toBeInTheDocument();
    });

    it("renders the 'Deleted at' field row when the record is soft-deleted", async () => {
      const record = makeConnectionRead({
        id: "rec-1",
        deleted_at: "2026-04-20T08:00:00+00:00",
      });
      useRecordResponse(record);

      renderWithMockedSession(<ConnectionDetail />, adminSession());

      await screen.findByTestId("connection-detail");

      // The dedicated Deleted-at row only renders for soft-deleted
      // records; on active records the field is suppressed.
      expect(screen.getByTestId("connection-detail-field-deleted-at")).toBeInTheDocument();
    });

    it("does NOT render the 'Deleted at' field row on active records", async () => {
      const record = makeConnectionRead({ id: "rec-1", deleted_at: null });
      useRecordResponse(record);

      renderWithMockedSession(<ConnectionDetail />, adminSession());

      await screen.findByTestId("connection-detail");
      expect(screen.queryByTestId("connection-detail-field-deleted-at")).not.toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // RBAC: Admin viewing any record
  // -------------------------------------------------------------------------
  describe("RBAC - Admin viewing any record", () => {
    it("shows edit and delete buttons for Admin even on records they do not own", async () => {
      const record = makeConnectionRead({
        id: "rec-1",
        owner_user_id: "other-user-99",
        owner_display_name: "Some Other User",
      });
      useRecordResponse(record);

      renderWithMockedSession(<ConnectionDetail />, adminSession());

      await screen.findByTestId("connection-detail");

      // Per canEditRecord: hasRole('Admin') is the first short-circuit.
      expect(screen.getByTestId("connection-detail-edit")).toBeInTheDocument();
      expect(screen.getByTestId("connection-detail-delete")).toBeInTheDocument();
    });

    it("the edit button links to /connections/:id/edit", async () => {
      const record = makeConnectionRead({ id: "rec-1" });
      useRecordResponse(record);

      renderWithMockedSession(<ConnectionDetail />, adminSession());

      await screen.findByTestId("connection-detail");

      // The Edit button is wrapped in <Link to={`/connections/${record.id}/edit`}>;
      // the closest <a> ancestor carries the href.
      const editButton = screen.getByTestId("connection-detail-edit");
      const editLink = editButton.closest("a");
      expect(editLink).not.toBeNull();
      expect(editLink).toHaveAttribute("href", `/connections/${record.id}/edit`);
    });
  });

  // -------------------------------------------------------------------------
  // RBAC: Contributor viewing own record
  // -------------------------------------------------------------------------
  describe("RBAC - Contributor viewing own record", () => {
    it("shows edit and delete buttons for the record owner (Contributor)", async () => {
      const record = makeConnectionRead({
        id: "rec-1",
        owner_user_id: "user-1",
        owner_display_name: "Owner Self",
      });
      useRecordResponse(record);

      renderWithMockedSession(<ConnectionDetail />, contributorSession("user-1"));

      await screen.findByTestId("connection-detail");

      // Per canEditRecord: hasRole('Contributor') && sessionUserId === owner_user_id.
      expect(screen.getByTestId("connection-detail-edit")).toBeInTheDocument();
      expect(screen.getByTestId("connection-detail-delete")).toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // RBAC: Contributor viewing another user's record
  // -------------------------------------------------------------------------
  describe("RBAC - Contributor viewing other's record", () => {
    it("hides edit and delete buttons when Contributor is not the owner", async () => {
      const record = makeConnectionRead({
        id: "rec-1",
        owner_user_id: "someone-else-42",
        owner_display_name: "Some Other User",
      });
      useRecordResponse(record);

      renderWithMockedSession(<ConnectionDetail />, contributorSession("user-1"));

      await screen.findByTestId("connection-detail");

      expect(screen.queryByTestId("connection-detail-edit")).not.toBeInTheDocument();
      expect(screen.queryByTestId("connection-detail-delete")).not.toBeInTheDocument();
      expect(screen.queryByTestId("connection-detail-actions")).not.toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // RBAC: Viewer (Sales Rep) viewing any record
  // -------------------------------------------------------------------------
  describe("RBAC - Viewer viewing any record", () => {
    it("hides edit and delete buttons regardless of ownership", async () => {
      const record = makeConnectionRead({
        id: "rec-1",
        owner_user_id: "viewer-user-1",
        owner_display_name: "Viewer User",
      });
      useRecordResponse(record);

      renderWithMockedSession(<ConnectionDetail />, viewerSession());

      await screen.findByTestId("connection-detail");

      expect(screen.queryByTestId("connection-detail-edit")).not.toBeInTheDocument();
      expect(screen.queryByTestId("connection-detail-delete")).not.toBeInTheDocument();
    });

    it("keeps the StatusChip editable for Viewer (sales-team mutation per F-005)", async () => {
      const record = makeConnectionRead({ id: "rec-1", outreach_status: "Not Started" });
      useRecordResponse(record);

      renderWithMockedSession(<ConnectionDetail />, viewerSession());

      await screen.findByTestId("connection-detail");
      // Viewer cannot edit the record but CAN mutate the outreach status.
      expect(screen.getByTestId("status-chip-select-rec-1")).toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // Soft-delete flow (F-007)
  // -------------------------------------------------------------------------
  describe("Soft-delete flow (F-007)", () => {
    it("clicking the delete button opens the confirmation modal", async () => {
      const record = makeConnectionRead({ id: "rec-1", full_name: "Jane Doe" });
      useRecordResponse(record);

      renderWithMockedSession(<ConnectionDetail />, adminSession());

      await screen.findByTestId("connection-detail");

      const deleteButton = screen.getByTestId("connection-detail-delete");
      const user = userEvent.setup();
      await user.click(deleteButton);

      // Modal opens; the dialog primitive carries data-testid="modal-dialog".
      const dialog = await screen.findByTestId("modal-dialog");
      expect(dialog).toBeInTheDocument();
      // Modal title.
      expect(within(dialog).getByText(/delete this connection\?/i)).toBeInTheDocument();
      // Modal description quotes the record's full_name.
      expect(within(dialog).getByText(/jane doe/i)).toBeInTheDocument();
      // Footer buttons: cancel + confirm.
      expect(screen.getByTestId("connection-detail-soft-delete-cancel")).toBeInTheDocument();
      expect(screen.getByTestId("connection-detail-soft-delete-confirm")).toBeInTheDocument();
    });

    it("clicking Cancel closes the modal without firing DELETE", async () => {
      const record = makeConnectionRead({ id: "rec-1", full_name: "Jane Doe" });
      useRecordResponse(record);

      // Spy that fails the test if DELETE is hit.
      const deleteSpy = vi.fn();
      server.use(
        http.delete("*/api/connections/:id", () => {
          deleteSpy();
          return HttpResponse.json(
            makeConnectionRead({ id: "rec-1", deleted_at: "2026-04-23T12:00:00+00:00" }),
          );
        }),
      );

      renderWithMockedSession(<ConnectionDetail />, adminSession());

      await screen.findByTestId("connection-detail");

      const user = userEvent.setup();
      await user.click(screen.getByTestId("connection-detail-delete"));
      await screen.findByTestId("modal-dialog");

      await user.click(screen.getByTestId("connection-detail-soft-delete-cancel"));

      // Modal closes (open attribute removed by handleCloseDelete).
      await waitFor(() => {
        expect(screen.queryByTestId("modal-dialog")).not.toHaveAttribute("open");
      });
      expect(deleteSpy).not.toHaveBeenCalled();
      expect(navigateMock).not.toHaveBeenCalled();
    });

    it("clicking Confirm fires DELETE and navigates to /feed on success", async () => {
      const record = makeConnectionRead({ id: "rec-1", full_name: "Jane Doe" });
      useRecordResponse(record);

      // Default DELETE handler returns the soft-deleted record (200) per
      // handlers.ts. Tests do not need to override here.

      renderWithMockedSession(<ConnectionDetail />, adminSession());

      await screen.findByTestId("connection-detail");

      const user = userEvent.setup();
      await user.click(screen.getByTestId("connection-detail-delete"));
      await screen.findByTestId("modal-dialog");
      await user.click(screen.getByTestId("connection-detail-soft-delete-confirm"));

      // After the mutation settles, the component navigates to /feed
      // (NOT history.back, per AAP Sec 0.5.2 / Key Insight: forward-
      // navigation prevents 404 on the back button).
      await waitFor(() => {
        expect(navigateMock).toHaveBeenCalledWith("/feed");
      });
    });

    it("Cancel button is disabled while the delete mutation is pending", async () => {
      const record = makeConnectionRead({ id: "rec-1", full_name: "Jane Doe" });
      useRecordResponse(record);

      // Pending-forever DELETE handler so the mutation never settles.
      server.use(http.delete("*/api/connections/:id", () => new Promise(() => {})));

      renderWithMockedSession(<ConnectionDetail />, adminSession());

      await screen.findByTestId("connection-detail");

      const user = userEvent.setup();
      await user.click(screen.getByTestId("connection-detail-delete"));
      await screen.findByTestId("modal-dialog");

      const cancel = screen.getByTestId("connection-detail-soft-delete-cancel");
      const confirm = screen.getByTestId("connection-detail-soft-delete-confirm");
      expect(cancel).not.toBeDisabled();

      await user.click(confirm);

      // Once the mutation enters the pending state, Cancel is disabled.
      await waitFor(() => {
        expect(cancel).toBeDisabled();
      });
    });

    it("the modal cannot be dismissed while the delete mutation is pending", async () => {
      const record = makeConnectionRead({ id: "rec-1", full_name: "Jane Doe" });
      useRecordResponse(record);

      // Pending-forever DELETE handler so the mutation never settles.
      server.use(http.delete("*/api/connections/:id", () => new Promise(() => {})));

      renderWithMockedSession(<ConnectionDetail />, adminSession());

      await screen.findByTestId("connection-detail");

      const user = userEvent.setup();
      await user.click(screen.getByTestId("connection-detail-delete"));
      await screen.findByTestId("modal-dialog");
      await user.click(screen.getByTestId("connection-detail-soft-delete-confirm"));

      // Wait for the pending state to settle on the Cancel button.
      await waitFor(() => {
        expect(screen.getByTestId("connection-detail-soft-delete-cancel")).toBeDisabled();
      });

      // Attempt to dismiss via the Cancel button - the click is a no-op
      // because the underlying Button is disabled, so the modal stays
      // open. This corroborates the handleCloseDelete short-circuit
      // when softDelete.isPending is true.
      await user.click(screen.getByTestId("connection-detail-soft-delete-cancel"));
      expect(screen.getByTestId("modal-dialog")).toHaveAttribute("open");
    });

    it("the confirm button shows the loading spinner while the mutation is pending", async () => {
      const record = makeConnectionRead({ id: "rec-1", full_name: "Jane Doe" });
      useRecordResponse(record);

      // Pending-forever DELETE handler so we can observe the loading UI.
      server.use(http.delete("*/api/connections/:id", () => new Promise(() => {})));

      renderWithMockedSession(<ConnectionDetail />, adminSession());

      await screen.findByTestId("connection-detail");

      const user = userEvent.setup();
      await user.click(screen.getByTestId("connection-detail-delete"));
      await screen.findByTestId("modal-dialog");

      const confirm = screen.getByTestId("connection-detail-soft-delete-confirm");
      await user.click(confirm);

      // Once pending, the Button primitive renders Loader2 with the
      // dedicated test id and sets aria-busy on the underlying button.
      await waitFor(() => {
        expect(within(confirm).getByTestId("button-loading-spinner")).toBeInTheDocument();
      });
      expect(confirm).toHaveAttribute("aria-busy", "true");
      expect(confirm).toBeDisabled();
    });
  });

  // -------------------------------------------------------------------------
  // LinkedIn URL link (F-001 + security invariants)
  // -------------------------------------------------------------------------
  describe("LinkedIn URL link (F-001)", () => {
    it("uses target=_blank and rel='noopener noreferrer' for external link safety", async () => {
      const record = makeConnectionRead({
        id: "rec-1",
        linkedin_url: "https://www.linkedin.com/in/some-contact",
      });
      useRecordResponse(record);

      renderWithMockedSession(<ConnectionDetail />, adminSession());

      await screen.findByTestId("connection-detail");

      const link = screen.getByTestId("connection-detail-linkedin-link");
      expect(link).toHaveAttribute("href", "https://www.linkedin.com/in/some-contact");
      expect(link).toHaveAttribute("target", "_blank");
      const rel = link.getAttribute("rel") ?? "";
      expect(rel.split(/\s+/)).toEqual(expect.arrayContaining(["noopener", "noreferrer"]));
    });
  });

  // -------------------------------------------------------------------------
  // Tag rendering (F-008)
  // -------------------------------------------------------------------------
  describe("Tag rendering (F-008)", () => {
    it("renders each tag as a Badge inside the tags <ul>", async () => {
      const record = makeConnectionRead({
        id: "rec-1",
        tags: [
          makeTagRead({ id: "tag-a", name: "industry:fintech" }),
          makeTagRead({ id: "tag-b", name: "use-case:retention" }),
          makeTagRead({ id: "tag-c", name: "geography:emea" }),
        ],
      });
      useRecordResponse(record);

      renderWithMockedSession(<ConnectionDetail />, adminSession());

      await screen.findByTestId("connection-detail");

      const tagsList = screen.getByTestId("connection-detail-tags");
      // The <ul> contains three <li> children, one per tag.
      const items = within(tagsList).getAllByRole("listitem");
      expect(items).toHaveLength(3);

      expect(within(tagsList).getByText("industry:fintech")).toBeInTheDocument();
      expect(within(tagsList).getByText("use-case:retention")).toBeInTheDocument();
      expect(within(tagsList).getByText("geography:emea")).toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // Back to feed navigation
  // -------------------------------------------------------------------------
  describe("Back to feed navigation", () => {
    it("renders a breadcrumb link with href='/feed'", async () => {
      const record = makeConnectionRead({ id: "rec-1" });
      useRecordResponse(record);

      renderWithMockedSession(<ConnectionDetail />, adminSession());

      await screen.findByTestId("connection-detail");

      // The back link is rendered as an <a> element by react-router-dom's
      // <Link to="/feed">; assert via the href attribute.
      const backLink = screen.getByTestId("connection-detail-back");
      expect(backLink.tagName).toBe("A");
      expect(backLink).toHaveAttribute("href", "/feed");
    });
  });

  // -------------------------------------------------------------------------
  // Null session (no authenticated user)
  // -------------------------------------------------------------------------
  describe("Null session (no authenticated user)", () => {
    it("hides edit/delete buttons when the session is null", async () => {
      const record = makeConnectionRead({ id: "rec-1", owner_user_id: "user-1" });
      useRecordResponse(record);

      // Pass null session - useSession() returns null, sessionUserId is null,
      // hasRole returns false for every role, canEditRecord returns false.
      // Note: ConnectionDetail unconditionally renders even without a
      // session because its parent <ProtectedRoute> would normally
      // intercept the unauthenticated case at a higher level.
      renderWithMockedSession(<ConnectionDetail />, null);

      await screen.findByTestId("connection-detail");

      expect(screen.queryByTestId("connection-detail-edit")).not.toBeInTheDocument();
      expect(screen.queryByTestId("connection-detail-delete")).not.toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // Routing harness smoke test
  // -------------------------------------------------------------------------
  describe("Routing harness", () => {
    it("renders correctly inside a real <Routes>/<Route> harness", async () => {
      // Sanity check: the component works inside a real Routes wrapper
      // (in addition to direct rendering). The vi.mock partial replaces
      // only useNavigate and useParams; Routes, Route, MemoryRouter, and
      // the matching machinery come from vi.importActual and continue to
      // function. This test confirms the partial mock does not interfere
      // with route-aware composition.
      const record = makeConnectionRead({ id: "rec-1", full_name: "Routed Record" });
      useRecordResponse(record);

      renderWithMockedSession(
        <Routes>
          <Route path="/connections/:id" element={<ConnectionDetail />} />
        </Routes>,
        adminSession(),
        { initialEntries: ["/connections/rec-1"] },
      );

      await screen.findByTestId("connection-detail");
      expect(screen.getByText("Routed Record")).toBeInTheDocument();
    });
  });
});
