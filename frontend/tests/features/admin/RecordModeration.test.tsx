/**
 * RecordModeration.test.tsx - Vitest tests for the F-014 + F-007 record
 * moderation view.
 *
 * Targets `frontend/src/features/admin/RecordModeration.tsx`, which:
 *   - Calls useAdminRecordsQuery({ include_deleted, page, page_size: 50,
 *     sort: 'submission_date', sort_dir: 'desc' })
 *   - Renders a Table<ConnectionRead> with 7 columns
 *   - Toggles include_deleted via a checkbox; toggling resets page to 1
 *   - Visually distinguishes soft-deleted rows (line-through, muted,
 *     DELETED badge)
 *   - Shows pagination only when total > 50; Previous/Next disabled at
 *     boundaries
 *   - Hard-delete button opens a confirmation Modal (destructive Button +
 *     amber warning)
 *   - On confirm: useHardDeleteRecordMutation fires, Modal closes,
 *     toast.success('Permanently deleted "..."')
 *   - Modal cannot close during pending mutation
 *
 * Test scope (per the assigned folder requirements):
 *   1. Render records list with the correct query parameters.
 *   2. Show-soft-deleted toggle and page-1 reset on toggle.
 *   3. Soft-deleted row visual distinction (line-through, DELETED badge).
 *   4. Pagination: hidden when total <= 50; visible and functional when
 *      total > 50.
 *   5. Hard-delete Modal flow: open on click, cancel without mutation,
 *      confirm with mutation.
 *   6. Modal cannot close while mutation is pending.
 *   7. Success toast on hard-delete success.
 *   8. Error state with retry.
 *   9. 403 (forbidden) and 500 (server error) on hard-delete (mutation
 *      hook handles its own toasts).
 *
 * Mocking strategy:
 *   - renderWithMockedSession with an Admin session.
 *   - MSW handles GET /api/admin/records and DELETE /api/admin/records/:id.
 *   - <ToastContainer /> mounted alongside <RecordModeration /> for toast
 *     assertions.
 *   - delay() helper from MSW for timing-sensitive tests
 *     (Modal-during-pending).
 *
 * Coordinates with:
 *   - frontend/src/features/admin/RecordModeration.tsx  Component under test
 *   - frontend/src/components/ui/Toast.tsx               ToastContainer
 *   - frontend/tests/mocks/server.ts                     MSW server instance
 *   - frontend/tests/mocks/handlers.ts                   `overrides` factory
 *   - frontend/tests/mocks/data.ts                       Factory functions
 *   - frontend/tests/test-utils.tsx                      renderWithMockedSession
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

import { describe, it, expect } from "vitest";
import { http, HttpResponse, delay } from "msw";

import { RecordModeration } from "@/features/admin/RecordModeration";
import { ToastContainer } from "@/components/ui/Toast";

import { server } from "../../mocks/server";
import { overrides } from "../../mocks/handlers";
import {
  makeConnectionList,
  makeConnectionRead,
  makePaginatedConnections,
  makeSessionRead,
  makeUserRead,
} from "../../mocks/data";
import { renderWithMockedSession, screen, userEvent, waitFor, within } from "../../test-utils";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Build a SessionRead with an Admin role for the AuthContext provided by
 * `renderWithMockedSession`. The component itself does not read the session
 * directly (RoleGate is at the route level), but providing an Admin session
 * keeps test parity with other admin-tab tests and prevents any future
 * defensive RoleGate wrapping inside RecordModeration from blocking
 * assertions.
 */
function buildAdminSession() {
  return makeSessionRead({
    user: makeUserRead({ role: "Admin", display_name: "Admin User" }),
    authenticated: true,
  });
}

/**
 * Render <RecordModeration /> alongside a <ToastContainer />.
 *
 * The container is rendered as a sibling so it subscribes to the
 * module-level Toast store and portals into document.body. This lets the
 * tests assert on toasts emitted by both:
 *   - The component itself (the success-toast: 'Permanently deleted "X".')
 *   - The useHardDeleteRecordMutation hook (its own success/error toasts)
 */
function renderRecordModeration() {
  return renderWithMockedSession(
    <>
      <RecordModeration />
      <ToastContainer />
    </>,
    buildAdminSession(),
  );
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("<RecordModeration />", () => {
  // -------------------------------------------------------------------------
  // Initial render
  // -------------------------------------------------------------------------

  describe("initial render", () => {
    it("queries with include_deleted=false on initial mount", async () => {
      // Capture the inbound query string so we can assert all five params
      // are wired exactly as the AAP requires.
      let receivedSearch: string | null = null;
      server.use(
        http.get("/api/admin/records", ({ request }) => {
          receivedSearch = new URL(request.url).search;
          return HttpResponse.json(makePaginatedConnections(makeConnectionList(5)));
        }),
      );

      renderRecordModeration();

      await waitFor(() => {
        expect(receivedSearch).not.toBeNull();
      });

      // Per AAP: include_deleted=false, page=1, page_size=50,
      // sort=submission_date, sort_dir=desc.
      expect(receivedSearch).toContain("include_deleted=false");
      expect(receivedSearch).toContain("page=1");
      expect(receivedSearch).toContain("page_size=50");
      expect(receivedSearch).toContain("sort=submission_date");
      expect(receivedSearch).toContain("sort_dir=desc");
    });

    it("renders the heading and aria-labelledby root", () => {
      renderRecordModeration();

      const root = screen.getByTestId("admin-record-moderation");
      expect(root).toHaveAttribute("aria-labelledby", "admin-record-moderation-heading");

      const heading = screen.getByRole("heading", { level: 2, name: /record moderation/i });
      expect(heading).toHaveAttribute("id", "admin-record-moderation-heading");
    });

    it("renders rows with name testid for each record", async () => {
      const records = [
        makeConnectionRead({ id: "rec-1", full_name: "Alice Smith" }),
        makeConnectionRead({ id: "rec-2", full_name: "Bob Jones" }),
      ];
      server.use(
        http.get("/api/admin/records", () =>
          HttpResponse.json(makePaginatedConnections(records, { total: 2 })),
        ),
      );

      renderRecordModeration();

      // findByTestId waits for the async query to resolve and the table
      // to render rows; getByTestId verifies the second row synchronously.
      expect(await screen.findByTestId("record-row-name-rec-1")).toHaveTextContent("Alice Smith");
      expect(screen.getByTestId("record-row-name-rec-2")).toHaveTextContent("Bob Jones");
    });
  });

  // -------------------------------------------------------------------------
  // Show-soft-deleted toggle
  // -------------------------------------------------------------------------

  describe("show-soft-deleted toggle", () => {
    it("renders the toggle label and unchecked checkbox initially", async () => {
      renderRecordModeration();

      const toggleLabel = await screen.findByTestId("record-moderation-include-deleted-toggle");
      expect(toggleLabel).toBeInTheDocument();
      expect(toggleLabel).toHaveTextContent(/show soft-deleted records/i);

      const checkbox = screen.getByTestId("record-moderation-include-deleted-checkbox");
      expect(checkbox).not.toBeChecked();
    });

    it("toggling include_deleted refetches with include_deleted=true", async () => {
      const calls: string[] = [];
      server.use(
        http.get("/api/admin/records", ({ request }) => {
          calls.push(new URL(request.url).search);
          return HttpResponse.json(makePaginatedConnections(makeConnectionList(3)));
        }),
      );

      renderRecordModeration();

      // Wait for the initial fetch to be captured.
      await waitFor(() => {
        expect(calls.length).toBeGreaterThanOrEqual(1);
      });
      expect(calls[0]).toContain("include_deleted=false");

      // Toggle the checkbox.
      const checkbox = screen.getByTestId("record-moderation-include-deleted-checkbox");
      await userEvent.click(checkbox);

      // Wait for the second fetch with include_deleted=true.
      await waitFor(() => {
        const matching = calls.find((s) => s.includes("include_deleted=true"));
        expect(matching).toBeDefined();
      });

      // The checkbox visually flips to checked after the toggle.
      expect(checkbox).toBeChecked();
    });

    it("toggling resets page to 1 (verified via query param)", async () => {
      const calls: string[] = [];
      server.use(
        http.get("/api/admin/records", ({ request }) => {
          calls.push(new URL(request.url).search);
          return HttpResponse.json(
            makePaginatedConnections(makeConnectionList(50), { total: 200 }),
          );
        }),
      );

      renderRecordModeration();

      // Wait for initial fetch (page=1).
      await waitFor(() => expect(calls.length).toBeGreaterThanOrEqual(1));

      // Navigate to page 2 via Next button (visible because total=200 > 50).
      const nextButton = await screen.findByTestId("record-moderation-next-page");
      await userEvent.click(nextButton);

      // Wait for the page-2 fetch.
      await waitFor(() => {
        const page2Call = calls.find((s) => s.includes("page=2"));
        expect(page2Call).toBeDefined();
      });

      // Toggle include_deleted; this should reset page to 1.
      const checkbox = screen.getByTestId("record-moderation-include-deleted-checkbox");
      await userEvent.click(checkbox);

      // Find a fetch call AFTER the toggle that has BOTH page=1 AND
      // include_deleted=true.
      await waitFor(() => {
        const resetCall = calls.find(
          (s) => s.includes("include_deleted=true") && s.includes("page=1"),
        );
        expect(resetCall).toBeDefined();
      });
    });
  });

  // -------------------------------------------------------------------------
  // Soft-deleted row visual distinction
  // -------------------------------------------------------------------------

  describe("soft-deleted row visual distinction", () => {
    it('renders line-through on the name and a "DELETED" badge for soft-deleted rows', async () => {
      const records = [
        makeConnectionRead({
          id: "rec-deleted",
          full_name: "Deleted Person",
          deleted_at: "2026-04-01T12:00:00+00:00",
        }),
        makeConnectionRead({
          id: "rec-active",
          full_name: "Active Person",
          deleted_at: null,
        }),
      ];
      server.use(
        http.get("/api/admin/records", () =>
          HttpResponse.json(makePaginatedConnections(records, { total: 2 })),
        ),
      );

      renderRecordModeration();

      // Soft-deleted name has the line-through utility class on the span.
      const deletedNameCell = await screen.findByTestId("record-row-name-rec-deleted");
      expect(deletedNameCell.className).toMatch(/line-through/);

      // Active name does NOT have line-through.
      const activeNameCell = screen.getByTestId("record-row-name-rec-active");
      expect(activeNameCell.className).not.toMatch(/line-through/);

      // The DELETED badge text should appear for the soft-deleted row.
      // The badge in the source contains "DELETED <date>" so a simple
      // /DELETED/ regex matches without coupling to the formatted date.
      expect(screen.getByText(/DELETED/)).toBeInTheDocument();
    });

    it("renders the empty-state copy that mentions toggling when no active records", async () => {
      server.use(
        http.get("/api/admin/records", () =>
          HttpResponse.json(makePaginatedConnections([], { total: 0 })),
        ),
      );

      renderRecordModeration();

      // The Table primitive renders the emptyState slot inline; the copy
      // includes an instruction to toggle the checkbox to see deleted rows.
      expect(await screen.findByText(/no active records yet/i)).toBeInTheDocument();
    });

    it("renders the alternate empty-state copy when toggle is on and no records", async () => {
      let includeDeletedSeen = false;
      server.use(
        http.get("/api/admin/records", ({ request }) => {
          const search = new URL(request.url).search;
          if (search.includes("include_deleted=true")) {
            includeDeletedSeen = true;
          }
          return HttpResponse.json(makePaginatedConnections([], { total: 0 }));
        }),
      );

      renderRecordModeration();

      // Toggle the checkbox to include_deleted=true.
      const checkbox = await screen.findByTestId("record-moderation-include-deleted-checkbox");
      await userEvent.click(checkbox);

      // Wait for the include_deleted=true call to be observed.
      await waitFor(() => expect(includeDeletedSeen).toBe(true));

      // With includeDeleted=true and 0 records, the alternate copy appears.
      expect(
        await screen.findByText(/no records found, even when including soft-deleted/i),
      ).toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // Pagination
  // -------------------------------------------------------------------------

  describe("pagination", () => {
    it("hides pagination when total <= PAGE_SIZE (50)", async () => {
      server.use(
        http.get("/api/admin/records", () =>
          HttpResponse.json(makePaginatedConnections(makeConnectionList(10), { total: 10 })),
        ),
      );

      renderRecordModeration();

      // Wait for any record row to render before asserting on the
      // absence of pagination. findAllByTestId resolves with a non-empty
      // array which guarantees the data has loaded into the table.
      const rows = await screen.findAllByTestId(/^record-row-name-/);
      expect(rows.length).toBeGreaterThan(0);

      // Pagination nav must NOT be rendered when total <= PAGE_SIZE (50).
      expect(screen.queryByTestId("record-moderation-pagination")).not.toBeInTheDocument();
    });

    it("renders pagination when total > PAGE_SIZE", async () => {
      server.use(
        http.get("/api/admin/records", () =>
          HttpResponse.json(makePaginatedConnections(makeConnectionList(50), { total: 120 })),
        ),
      );

      renderRecordModeration();

      const paginationNav = await screen.findByTestId("record-moderation-pagination");
      expect(paginationNav).toBeInTheDocument();
      expect(paginationNav).toHaveAttribute("aria-label", "Records pagination");

      // Page indicator text - within() scopes the queries inside the
      // pagination nav so unrelated text in the page does not interfere.
      expect(within(paginationNav).getByText(/page 1 of 3/i)).toBeInTheDocument();
      expect(within(paginationNav).getByText(/120 total/)).toBeInTheDocument();

      // Both buttons present.
      expect(screen.getByTestId("record-moderation-prev-page")).toBeInTheDocument();
      expect(screen.getByTestId("record-moderation-next-page")).toBeInTheDocument();
    });

    it("disables Previous on page 1 and enables Next", async () => {
      server.use(
        http.get("/api/admin/records", () =>
          HttpResponse.json(makePaginatedConnections(makeConnectionList(50), { total: 120 })),
        ),
      );

      renderRecordModeration();

      const prevButton = await screen.findByTestId("record-moderation-prev-page");
      const nextButton = screen.getByTestId("record-moderation-next-page");

      // page <= 1 means Previous is disabled; Next is enabled because
      // page < totalPages (1 < 3).
      expect(prevButton).toBeDisabled();
      expect(nextButton).toBeEnabled();
    });

    it("Next button advances to page 2", async () => {
      const calls: string[] = [];
      server.use(
        http.get("/api/admin/records", ({ request }) => {
          calls.push(new URL(request.url).search);
          return HttpResponse.json(
            makePaginatedConnections(makeConnectionList(50), { total: 120 }),
          );
        }),
      );

      renderRecordModeration();

      const nextButton = await screen.findByTestId("record-moderation-next-page");
      await userEvent.click(nextButton);

      await waitFor(() => {
        const page2Call = calls.find((s) => s.includes("page=2"));
        expect(page2Call).toBeDefined();
      });

      // Page indicator updates to reflect the new state.
      expect(await screen.findByText(/page 2 of 3/i)).toBeInTheDocument();
    });

    it("disables Next on the last page", async () => {
      let currentPage = 1;
      server.use(
        http.get("/api/admin/records", ({ request }) => {
          const url = new URL(request.url);
          currentPage = Number(url.searchParams.get("page") ?? "1");
          return HttpResponse.json(
            makePaginatedConnections(makeConnectionList(20), { total: 120 }),
          );
        }),
      );

      renderRecordModeration();

      // Click Next twice to reach page 3 (last page) for total=120, size=50.
      const nextButton = await screen.findByTestId("record-moderation-next-page");
      await userEvent.click(nextButton);
      await waitFor(() => expect(currentPage).toBe(2));

      await userEvent.click(screen.getByTestId("record-moderation-next-page"));
      await waitFor(() => expect(currentPage).toBe(3));

      // Page 3 of 3 - Next disabled, Prev enabled.
      expect(await screen.findByText(/page 3 of 3/i)).toBeInTheDocument();
      expect(screen.getByTestId("record-moderation-next-page")).toBeDisabled();
      expect(screen.getByTestId("record-moderation-prev-page")).toBeEnabled();
    });
  });

  // -------------------------------------------------------------------------
  // Hard-delete confirmation modal
  // -------------------------------------------------------------------------

  describe("hard-delete confirmation modal", () => {
    it("opens the modal on hard-delete button click", async () => {
      const records = [
        makeConnectionRead({
          id: "rec-1",
          full_name: "Test Person",
          company: "TestCo",
          owner_display_name: "Owner Person",
        }),
      ];
      server.use(
        http.get("/api/admin/records", () =>
          HttpResponse.json(makePaginatedConnections(records, { total: 1 })),
        ),
      );

      renderRecordModeration();

      // The Modal primitive ALWAYS renders the <dialog> + its children
      // in the DOM regardless of isOpen; the dialog's `open` attribute
      // toggles based on isOpen. Therefore the open/closed state is
      // observed via the `open` HTML attribute on the dialog, NOT via
      // queryByTestId for the inner Cancel/Confirm buttons (which are
      // always present).
      const dialog = await screen.findByTestId("modal-dialog");
      expect(dialog).not.toHaveAttribute("open");

      // Click the hard-delete row button.
      const hardDeleteButton = await screen.findByTestId("record-row-hard-delete-rec-1");
      await userEvent.click(hardDeleteButton);

      // Dialog now opens (open attribute set by .showModal() polyfill).
      await waitFor(() => {
        expect(dialog).toHaveAttribute("open");
      });

      // Cancel/Confirm buttons are interactable and rendered inside the
      // open dialog. Scope queries to within(dialog) so we only match
      // text inside the modal (avoids collisions with row text outside).
      expect(within(dialog).getByTestId("hard-delete-cancel")).toBeInTheDocument();
      expect(within(dialog).getByTestId("hard-delete-confirm")).toBeInTheDocument();

      // Modal title and description rendered by the Modal primitive.
      expect(within(dialog).getByText(/hard delete record\?/i)).toBeInTheDocument();
      expect(
        within(dialog).getByText(/this action permanently removes the record/i),
      ).toBeInTheDocument();

      // Amber warning panel content present.
      expect(within(dialog).getByText(/hard deletion permanently removes/i)).toBeInTheDocument();

      // Record details visible inside the dl. Scoped to the dialog so
      // these assertions do not match incidental text in the row.
      expect(within(dialog).getByText("Test Person")).toBeInTheDocument();
      expect(within(dialog).getByText("TestCo")).toBeInTheDocument();
      expect(within(dialog).getByText("Owner Person")).toBeInTheDocument();
    });

    it("Cancel closes the Modal WITHOUT firing the mutation", async () => {
      let deleteCalled = false;
      const records = [makeConnectionRead({ id: "rec-1", full_name: "Test Person" })];
      server.use(
        http.get("/api/admin/records", () =>
          HttpResponse.json(makePaginatedConnections(records, { total: 1 })),
        ),
        http.delete("/api/admin/records/:id", () => {
          deleteCalled = true;
          return new HttpResponse(null, { status: 204 });
        }),
      );

      renderRecordModeration();

      await userEvent.click(await screen.findByTestId("record-row-hard-delete-rec-1"));

      // Wait for the dialog to open. Cancel/Confirm buttons are always
      // in the DOM; the open state is read from the dialog's `open`
      // attribute.
      const dialog = await screen.findByTestId("modal-dialog");
      await waitFor(() => expect(dialog).toHaveAttribute("open"));

      // Click the Cancel button via the dialog-scoped query.
      const cancelButton = within(dialog).getByTestId("hard-delete-cancel");
      await userEvent.click(cancelButton);

      // Dialog closes (open attribute removed by .close() polyfill).
      await waitFor(() => {
        expect(dialog).not.toHaveAttribute("open");
      });

      // Critically: the DELETE endpoint was NEVER hit.
      expect(deleteCalled).toBe(false);
    });

    it("Confirm fires the mutation and closes the Modal on success", async () => {
      let deletedId: string | null = null;
      const records = [makeConnectionRead({ id: "rec-success", full_name: "Doomed Person" })];
      server.use(
        http.get("/api/admin/records", () =>
          HttpResponse.json(makePaginatedConnections(records, { total: 1 })),
        ),
        http.delete("/api/admin/records/:id", ({ params }) => {
          deletedId = params.id as string;
          return new HttpResponse(null, { status: 204 });
        }),
      );

      renderRecordModeration();

      await userEvent.click(await screen.findByTestId("record-row-hard-delete-rec-success"));

      const dialog = await screen.findByTestId("modal-dialog");
      await waitFor(() => expect(dialog).toHaveAttribute("open"));

      const confirmButton = within(dialog).getByTestId("hard-delete-confirm");
      await userEvent.click(confirmButton);

      // The DELETE endpoint was hit with the correct record id.
      await waitFor(() => {
        expect(deletedId).toBe("rec-success");
      });

      // Modal closes (open attribute removed) after mutation succeeds.
      await waitFor(() => {
        expect(dialog).not.toHaveAttribute("open");
      });
    });

    it("shows success toast with the record name on successful hard-delete", async () => {
      const records = [makeConnectionRead({ id: "rec-1", full_name: "Goodbye Friend" })];
      server.use(
        http.get("/api/admin/records", () =>
          HttpResponse.json(makePaginatedConnections(records, { total: 1 })),
        ),
        http.delete("/api/admin/records/:id", () => new HttpResponse(null, { status: 204 })),
      );

      renderRecordModeration();

      await userEvent.click(await screen.findByTestId("record-row-hard-delete-rec-1"));
      await userEvent.click(await screen.findByTestId("hard-delete-confirm"));

      // The component-level success toast has the format
      // 'Permanently deleted "X".' (with literal quotes around the name).
      // The hook's own success toast 'Connection permanently deleted'
      // does NOT match this regex, so the assertion targets the
      // record-name-specific message uniquely.
      expect(
        await screen.findByText(/Permanently deleted "Goodbye Friend"\./i),
      ).toBeInTheDocument();
    });

    it("keeps the Modal open during the in-flight mutation (cannot close while pending)", async () => {
      const records = [makeConnectionRead({ id: "rec-slow", full_name: "Slow Delete" })];
      server.use(
        http.get("/api/admin/records", () =>
          HttpResponse.json(makePaginatedConnections(records, { total: 1 })),
        ),
        http.delete("/api/admin/records/:id", async () => {
          // Use MSW's delay() so the test can observe the pending state
          // window while the mutation is in flight. 300ms is long enough
          // to assert the loading state without significantly slowing
          // the suite.
          await delay(300);
          return new HttpResponse(null, { status: 204 });
        }),
      );

      renderRecordModeration();

      await userEvent.click(await screen.findByTestId("record-row-hard-delete-rec-slow"));

      const dialog = await screen.findByTestId("modal-dialog");
      await waitFor(() => expect(dialog).toHaveAttribute("open"));

      const confirmButton = within(dialog).getByTestId("hard-delete-confirm");
      await userEvent.click(confirmButton);

      // Mutation now in-flight; Button.tsx renders the spinner with
      // data-testid="button-loading-spinner" inside the confirm button.
      await waitFor(() => {
        expect(within(dialog).getByTestId("button-loading-spinner")).toBeInTheDocument();
      });

      // The dialog stays open while the mutation is in-flight: the
      // open attribute is still present.
      expect(dialog).toHaveAttribute("open");

      // While pending, the Cancel button is disabled per the source
      // contract. Clicking a disabled button via @testing-library/user-event
      // is a no-op (the action is dropped silently), but we still assert
      // the disabled state directly because that is the contract.
      const cancelButton = within(dialog).getByTestId("hard-delete-cancel");
      expect(cancelButton).toBeDisabled();

      // Wait for the mutation to complete; Modal closes when the
      // component's onSuccess callback calls setConfirmingRecord(null).
      await waitFor(
        () => {
          expect(dialog).not.toHaveAttribute("open");
        },
        { timeout: 2000 },
      );
    });

    it("shows the soft-deleted timestamp in the Modal when the record is already soft-deleted", async () => {
      const records = [
        makeConnectionRead({
          id: "rec-soft",
          full_name: "Already Soft-Deleted",
          deleted_at: "2026-04-01T12:00:00+00:00",
        }),
      ];
      server.use(
        http.get("/api/admin/records", () =>
          HttpResponse.json(makePaginatedConnections(records, { total: 1 })),
        ),
      );

      renderRecordModeration();

      // Toggle the checkbox so the soft-deleted row is unambiguously
      // shown (the MSW handler returns the row regardless of the
      // include_deleted query param, but toggling exercises the
      // realistic admin-flow path: an admin would only see
      // soft-deleted rows after enabling the toggle).
      const checkbox = await screen.findByTestId("record-moderation-include-deleted-checkbox");
      await userEvent.click(checkbox);

      // Click the hard-delete button on the soft-deleted row.
      const hardDeleteButton = await screen.findByTestId("record-row-hard-delete-rec-soft");
      await userEvent.click(hardDeleteButton);

      // Wait for the dialog to open.
      const dialog = await screen.findByTestId("modal-dialog");
      await waitFor(() => expect(dialog).toHaveAttribute("open"));

      // Modal shows the "Soft-deleted" dt/dd pair (the dt term renders
      // ONLY when the record's deleted_at is non-null per the source
      // contract). The exact string match (default exact: true) avoids
      // collision with the record's full_name "Already Soft-Deleted"
      // which is also rendered inside the dialog as the Name dd value.
      expect(within(dialog).getByText("Soft-deleted")).toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // Error state
  // -------------------------------------------------------------------------

  describe("error state", () => {
    it("renders the error UI on a 500 response", async () => {
      server.use(
        http.get("/api/admin/records", () =>
          HttpResponse.json(
            {
              error: {
                code: "server_error",
                message: "Database temporarily unavailable",
                correlation_id: "test-correlation-id",
                fields: [],
              },
            },
            { status: 500 },
          ),
        ),
      );

      renderRecordModeration();

      const errorRoot = await screen.findByTestId("record-moderation-error");
      expect(errorRoot).toBeInTheDocument();
      expect(errorRoot).toHaveAttribute("role", "alert");

      // The error message paragraph and retry button render inside the
      // error card; both should be present and the button enabled.
      expect(screen.getByTestId("record-moderation-error-message")).toBeInTheDocument();
      expect(screen.getByTestId("record-moderation-retry")).toBeEnabled();
    });

    it("retry button refetches and recovers on second attempt", async () => {
      let attempt = 0;
      server.use(
        http.get("/api/admin/records", () => {
          attempt += 1;
          if (attempt === 1) {
            return HttpResponse.json(
              {
                error: {
                  code: "server_error",
                  message: "Temporary failure",
                  correlation_id: "test-correlation-id",
                  fields: [],
                },
              },
              { status: 500 },
            );
          }
          return HttpResponse.json(makePaginatedConnections(makeConnectionList(3), { total: 3 }));
        }),
      );

      renderRecordModeration();

      const retryButton = await screen.findByTestId("record-moderation-retry");
      // Pagination is hidden while the error state is showing because
      // recordsQuery.data is undefined.
      expect(screen.queryByTestId("record-moderation-pagination")).not.toBeInTheDocument();

      await userEvent.click(retryButton);

      // After retry the second attempt succeeds, the error vanishes,
      // and the table renders the records.
      await waitFor(() => {
        expect(screen.queryByTestId("record-moderation-error")).not.toBeInTheDocument();
      });

      // Both attempts were observed exactly once each.
      expect(attempt).toBe(2);
    });
  });

  // -------------------------------------------------------------------------
  // Hard-delete failure modes (403 forbidden, 500 server error)
  //
  // The useHardDeleteRecordMutation hook attaches its OWN error toast
  // inside its onError callback. The component does NOT duplicate this
  // toast - it only adds the success toast on onSuccess. These tests
  // therefore verify the hook's error toast surfaces on each failure
  // mode by checking the ToastContainer for a toast-error element.
  // -------------------------------------------------------------------------

  describe("hard-delete failure modes", () => {
    it("handles 403 forbidden from the hard-delete endpoint", async () => {
      const records = [makeConnectionRead({ id: "rec-forbidden", full_name: "Forbidden Target" })];
      server.use(
        http.get("/api/admin/records", () =>
          HttpResponse.json(makePaginatedConnections(records, { total: 1 })),
        ),
        overrides.admin.hardDeleteForbidden("rec-forbidden"),
      );

      renderRecordModeration();

      await userEvent.click(await screen.findByTestId("record-row-hard-delete-rec-forbidden"));
      await userEvent.click(await screen.findByTestId("hard-delete-confirm"));

      // The mutation hook fires its own error toast on 403; it has
      // data-testid="toast-error" per the Toast.tsx variant convention.
      const errorToast = await screen.findByTestId("toast-error", {}, { timeout: 2000 });
      expect(errorToast).toBeInTheDocument();
    });

    it("handles 500 server error from the hard-delete endpoint", async () => {
      const records = [makeConnectionRead({ id: "rec-500", full_name: "Crash Target" })];
      server.use(
        http.get("/api/admin/records", () =>
          HttpResponse.json(makePaginatedConnections(records, { total: 1 })),
        ),
        overrides.admin.hardDelete500("rec-500"),
      );

      renderRecordModeration();

      await userEvent.click(await screen.findByTestId("record-row-hard-delete-rec-500"));
      await userEvent.click(await screen.findByTestId("hard-delete-confirm"));

      // Same as the 403 case - hook's onError fires toast.error(...).
      const errorToast = await screen.findByTestId("toast-error", {}, { timeout: 2000 });
      expect(errorToast).toBeInTheDocument();
    });
  });
});
