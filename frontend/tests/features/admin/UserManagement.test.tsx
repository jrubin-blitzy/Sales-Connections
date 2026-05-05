/**
 * UserManagement.test.tsx - Vitest tests for the F-014 + F-009 user
 * management view.
 *
 * Targets `frontend/src/features/admin/UserManagement.tsx`, which:
 *   - Calls useAdminUsersQuery() (returns UserRead[] PLAIN ARRAY).
 *   - Calls useUpdateUserRoleMutation().
 *   - Renders a Table with 4 columns (Name+email, Role badge, Joined,
 *     Role-edit Select).
 *   - Uses useSession() to identify the current user and show the
 *     "(you)" marker (data-testid `user-row-self-marker-${id}`).
 *   - Wraps the role-edit Select in <RoleGate role="Admin"> as UI
 *     defense-in-depth.
 *   - Two-stage confirmation Modal flow: select different role -> Modal
 *     opens -> confirm -> mutation fires -> success toast + Modal
 *     closes.
 *   - Same-role select is a no-op (Modal does NOT open).
 *   - Demotion warning panel (role-change-demotion-warning testid)
 *     when current=Admin and new!=Admin; the Confirm button label
 *     switches to "Demote user" (vs. "Update role").
 *   - Special-case errors handled by the mutation hook:
 *       409 -> "Cannot demote the last remaining Admin."
 *       403 -> "You cannot demote yourself."
 *     The component closes the Modal on error so the hook's toast is
 *     visible to the user.
 *   - Modal cannot close during a pending mutation (Cancel disabled,
 *     Confirm shows the loading spinner).
 *   - Loading state via Table skeleton; error state via inline
 *     ErrorState block (`user-management-error` + retry button
 *     `user-management-retry`).
 *
 * Test scope (per the assigned folder requirements in the AAP):
 *   1. Initial render: heading, root container, list of users from
 *      useAdminUsersQuery; loading state via Table skeleton.
 *   2. Error state: 4xx/5xx responses surface the user-management-error
 *      block; retry recovers on second attempt.
 *   3. Self-marker `user-row-self-marker-${id}` shows on the row whose
 *      id matches `session.user.id` ONLY.
 *   4. Role badge `user-row-role-badge-${id}` per row with correct text.
 *   5. Same-role select is a no-op (Modal does NOT open).
 *   6. Different-role select opens the Modal with cancel/confirm.
 *   7. Demotion warning is shown when current=Admin and new!=Admin and
 *      hidden for promotions / no-op same-role selections.
 *   8. Confirming the change fires the mutation with the correct
 *      payload, closes the Modal on success, and surfaces the
 *      component's success toast.
 *   9. Special error mapping: 409 last-admin, 403 self-demote.
 *  10. RoleGate defense-in-depth: the role-edit Select is hidden for
 *      Contributor / Viewer sessions; visible for Admin.
 *  11. Modal cannot close during pending mutation (Cancel disabled,
 *      Confirm shows spinner).
 *
 * Mocking strategy:
 *   - renderWithMockedSession with a synchronous session
 *     (Admin / Contributor / Viewer) so RoleGate / useSession resolve
 *     without an MSW round-trip.
 *   - <ToastContainer /> mounted alongside <UserManagement /> so the
 *     module-level toast store renders into the DOM and toast text can
 *     be asserted.
 *   - MSW handlers cover GET /api/admin/users and PATCH
 *     /api/admin/users/:id; per-test overrides via server.use(...) and
 *     the `overrides.admin.*` factory functions.
 *   - delay() helper from MSW for in-flight Modal / loading-spinner
 *     assertions.
 *
 * Coordinates with:
 *   - frontend/src/features/admin/UserManagement.tsx  component under test
 *   - frontend/src/components/ui/Toast.tsx            ToastContainer
 *   - frontend/src/schemas/admin.ts                   USER_ROLE_VALUES
 *   - frontend/tests/mocks/server.ts                  MSW server
 *   - frontend/tests/mocks/handlers.ts                `overrides` factory
 *   - frontend/tests/mocks/data.ts                    factory functions
 *   - frontend/tests/test-utils.tsx                   render helpers
 *
 * Conventions per AAP Sec 0.7.7 and frontend/.prettierrc.json:
 *   - Strict TypeScript; no `any`.
 *   - Double quotes (`singleQuote: false`); trailing commas; 2-space
 *     indent; line length <= 100.
 *   - No emoji; no console.log.
 *   - All interactions use @testing-library/user-event (never
 *     fireEvent).
 *   - Helpers imported from ../../test-utils for a single import surface.
 *
 * Modal-open detection note:
 *   The Modal primitive in @/components/ui/Modal.tsx uses the native
 *   <dialog> element and ALWAYS renders its children regardless of
 *   `isOpen`. The open/closed state is reflected via the `open` HTML
 *   attribute on the <dialog> (toggled by the .showModal() / .close()
 *   methods polyfilled in tests/setup.ts). Therefore tests that need
 *   to assert "Modal closed" wait for `dialog.not.toHaveAttribute("open")`
 *   rather than asserting the cancel/confirm testids are absent (they
 *   are always present in the DOM tree).
 */

import { describe, it, expect } from "vitest";
import { http, HttpResponse, delay } from "msw";

import { UserManagement } from "@/features/admin/UserManagement";
import { ToastContainer } from "@/components/ui/Toast";
import { USER_ROLE_VALUES } from "@/schemas/admin";

import { server } from "../../mocks/server";
import { overrides } from "../../mocks/handlers";
import { makeAdminUserList, makeSessionRead, makeUserRead } from "../../mocks/data";
import { renderWithMockedSession, screen, userEvent, waitFor, within } from "../../test-utils";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Narrow the role names accepted by the helpers below to the three
 * values defined in `@/schemas/admin` USER_ROLE_VALUES. Re-declaring
 * the type at the test boundary catches typos (e.g., "Sales Rep")
 * at compile time rather than producing a silent UX failure.
 */
type SessionRole = (typeof USER_ROLE_VALUES)[number];

/**
 * Build a SessionRead with the given role and user id (and optionally
 * a custom display_name and email). Defaults match the conventions of
 * the AdminPanel / RecordModeration tests for predictable session
 * shape across the admin test suite.
 *
 * The returned shape is the nested `{ user: UserRead, authenticated:
 * true }` matching the production AuthContextValue.session type, so
 * `useSession()` and `useRole()` resolve synchronously inside
 * <UserManagement /> without an MSW round-trip.
 */
function buildSession(role: SessionRole = "Admin", id = "session-user-1") {
  return makeSessionRead({
    user: makeUserRead({
      id,
      role,
      display_name: "Session User",
      email: "session@example.com",
    }),
    authenticated: true,
  });
}

/**
 * Render <UserManagement /> wrapped in the standard test provider
 * stack with a synchronous session AND a <ToastContainer /> mounted
 * alongside.
 *
 * Why ToastContainer is mounted here (not just in App.tsx production):
 *   The toast store is module-level; useToast().success/error()
 *   dispatch into the same singleton regardless of where they are
 *   called. ToastContainer is the React component that subscribes to
 *   the store and renders the active toasts via createPortal into
 *   document.body. Mounting it as a sibling of <UserManagement />
 *   gives the tests a real, queryable DOM for the toasts emitted by:
 *     - The component itself: `Updated ${display_name} to ${role}.`
 *     - The mutation hook: `User role updated` (success), `Cannot
 *       demote the last remaining Admin.` (409), `You cannot demote
 *       yourself.` (403), or the fallback "Failed to update role".
 *
 * The session arguments default to (Admin, "session-user-1") so the
 * majority of tests can simply call `renderUserManagement()` without
 * arguments. Tests for RoleGate defense-in-depth or self-marker pass
 * specific roles / ids.
 */
function renderUserManagement(role: SessionRole = "Admin", id = "session-user-1") {
  return renderWithMockedSession(
    <>
      <UserManagement />
      <ToastContainer />
    </>,
    buildSession(role, id),
  );
}

// ---------------------------------------------------------------------------
// Top-level describe
// ---------------------------------------------------------------------------

describe("<UserManagement />", () => {
  // -------------------------------------------------------------------------
  // Initial render
  // -------------------------------------------------------------------------

  describe("initial render", () => {
    it("renders the heading and root container with correct ARIA wiring", async () => {
      renderUserManagement();

      // Root <main> element identified by data-testid; aria-labelledby
      // points at the heading id so screen readers announce the
      // section title.
      expect(screen.getByTestId("admin-user-management")).toHaveAttribute(
        "aria-labelledby",
        "admin-user-management-heading",
      );

      // The matching heading exists with the documented id; semantic
      // <h2> level satisfies the accessible-document-outline rule.
      const heading = screen.getByRole("heading", {
        level: 2,
        name: /user management/i,
      });
      expect(heading).toHaveAttribute("id", "admin-user-management-heading");
    });

    it("renders the list of users returned by useAdminUsersQuery", async () => {
      const users = [
        makeUserRead({
          id: "u-1",
          display_name: "Alice Carter",
          email: "alice@example.com",
          role: "Admin",
        }),
        makeUserRead({
          id: "u-2",
          display_name: "Bob Singh",
          email: "bob@example.com",
          role: "Contributor",
        }),
        makeUserRead({
          id: "u-3",
          display_name: "Carol Liu",
          email: "carol@example.com",
          role: "Viewer",
        }),
      ];
      // The hook expects a PLAIN ARRAY (not a paginated envelope); the
      // /api/admin/users handler in the contract returns UserRead[]
      // directly per AAP Sec 0.4.3.
      server.use(http.get("/api/admin/users", () => HttpResponse.json(users)));

      renderUserManagement();

      // Display names are rendered as the primary cell content.
      expect(await screen.findByText("Alice Carter")).toBeInTheDocument();
      expect(screen.getByText("Bob Singh")).toBeInTheDocument();
      expect(screen.getByText("Carol Liu")).toBeInTheDocument();

      // Emails render below the display name in the same cell.
      expect(screen.getByText("alice@example.com")).toBeInTheDocument();
      expect(screen.getByText("bob@example.com")).toBeInTheDocument();
      expect(screen.getByText("carol@example.com")).toBeInTheDocument();
    });

    it("shows the table while the query is pending and renders rows once data arrives", async () => {
      server.use(
        http.get("/api/admin/users", async () => {
          // Delay the response so the test observes the loading state
          // window before rows appear. 200ms is short enough to keep
          // the suite fast but long enough to assert on the skeleton.
          await delay(200);
          return HttpResponse.json(makeAdminUserList(3));
        }),
      );

      renderUserManagement();

      // The Table primitive renders the table-container immediately
      // (the skeleton lives inside it during loading); per Table.tsx
      // the data-testid="table-container" hook is always present.
      expect(screen.getByTestId("table-container")).toBeInTheDocument();

      // After the delay completes, role badges render for each row.
      // Use a regex testid match to avoid hard-coding factory IDs.
      await waitFor(() => {
        const badges = screen.queryAllByTestId(/^user-row-role-badge-/);
        expect(badges.length).toBeGreaterThan(0);
      });
    });

    it("renders the empty-state slot when the user list is empty", async () => {
      server.use(overrides.admin.usersEmpty());

      renderUserManagement();

      // The Table primitive's emptyState slot renders the configured
      // ReactNode when data.length === 0. The component supplies the
      // string "No users found in the organization."
      expect(await screen.findByText(/no users found in the organization/i)).toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // Error state + retry
  // -------------------------------------------------------------------------

  describe("error state", () => {
    it("renders the user-management-error block on a 403 forbidden response", async () => {
      server.use(overrides.admin.usersForbidden());

      renderUserManagement();

      // The ErrorState component is keyed by data-testid
      // user-management-error and uses role="alert" so screen readers
      // announce the failure as soon as it appears.
      const errorRoot = await screen.findByTestId("user-management-error");
      expect(errorRoot).toBeInTheDocument();
      expect(errorRoot).toHaveAttribute("role", "alert");

      // The error message paragraph and the retry button are children
      // of the error block; both must be present and the button
      // enabled so the user can recover.
      expect(screen.getByTestId("user-management-error-message")).toBeInTheDocument();
      expect(screen.getByTestId("user-management-retry")).toBeEnabled();
    });

    it("retry recovers when the second attempt succeeds", async () => {
      let attempt = 0;
      // Stateful handler so attempt 1 fails and attempt 2 succeeds.
      // This exercises the full Try-again recovery path including
      // TanStack Query's refetch() behavior.
      server.use(
        http.get("/api/admin/users", () => {
          attempt += 1;
          if (attempt === 1) {
            return HttpResponse.json(
              {
                error: {
                  code: "server_error",
                  message: "oops",
                  correlation_id: "test-correlation-id",
                  fields: [],
                },
              },
              { status: 500 },
            );
          }
          return HttpResponse.json([
            makeUserRead({
              id: "u-1",
              display_name: "Recovered User",
              role: "Contributor",
            }),
          ]);
        }),
      );

      renderUserManagement();

      // The error block surfaces first; click the retry button to
      // force a refetch.
      const retryButton = await screen.findByTestId("user-management-retry");
      await userEvent.click(retryButton);

      // After the second attempt resolves successfully, the list of
      // users replaces the error block.
      expect(await screen.findByText("Recovered User")).toBeInTheDocument();
      expect(screen.queryByTestId("user-management-error")).not.toBeInTheDocument();

      // Sanity check on attempt counter: exactly two handler invocations.
      expect(attempt).toBe(2);
    });

    it("renders a 500 server-error response as the error block", async () => {
      // Direct 500 (no recovery) verifies that ANY non-2xx status
      // surfaces the error UI consistently with 403, not just the
      // specific 403 forbidden override.
      server.use(
        http.get("/api/admin/users", () =>
          HttpResponse.json(
            {
              error: {
                code: "internal_error",
                message: "Internal server error",
                correlation_id: "test-correlation-id",
                fields: [],
              },
            },
            { status: 500 },
          ),
        ),
      );

      renderUserManagement();

      const errorRoot = await screen.findByTestId("user-management-error");
      expect(errorRoot).toBeInTheDocument();
      expect(errorRoot).toHaveAttribute("role", "alert");
    });
  });

  // -------------------------------------------------------------------------
  // Self-marker
  // -------------------------------------------------------------------------

  describe("self-marker", () => {
    it('shows the "(you)" marker only on the row matching session.user.id', async () => {
      const sessionUserId = "session-user-1";
      const users = [
        makeUserRead({
          id: sessionUserId,
          display_name: "Me",
          role: "Admin",
        }),
        makeUserRead({
          id: "other-1",
          display_name: "Someone Else",
          role: "Contributor",
        }),
      ];
      server.use(http.get("/api/admin/users", () => HttpResponse.json(users)));

      renderUserManagement("Admin", sessionUserId);

      // The self-marker testid embeds the row id; it must appear on
      // the session-user row.
      const selfMarker = await screen.findByTestId(`user-row-self-marker-${sessionUserId}`);
      expect(selfMarker).toBeInTheDocument();
      expect(selfMarker).toHaveTextContent(/\(you\)/i);

      // The other row must NOT have a self-marker; queryByTestId
      // returns null (rather than throwing) so the negative assertion
      // is precise.
      expect(screen.queryByTestId("user-row-self-marker-other-1")).not.toBeInTheDocument();
    });

    it("renders no self-marker when no row matches the session id", async () => {
      // Session id matches none of the returned users; the marker
      // should be absent from the entire table.
      const users = [
        makeUserRead({ id: "u-1", display_name: "Alice", role: "Admin" }),
        makeUserRead({ id: "u-2", display_name: "Bob", role: "Contributor" }),
      ];
      server.use(http.get("/api/admin/users", () => HttpResponse.json(users)));

      renderUserManagement("Admin", "ghost-id");

      // Wait for rows to render so the assertion is on resolved state.
      await screen.findByText("Alice");

      // No self-marker for either user, nor for the ghost session id.
      expect(screen.queryByTestId("user-row-self-marker-u-1")).not.toBeInTheDocument();
      expect(screen.queryByTestId("user-row-self-marker-u-2")).not.toBeInTheDocument();
      expect(screen.queryByTestId("user-row-self-marker-ghost-id")).not.toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // Role badge per row
  // -------------------------------------------------------------------------

  describe("role badge per row", () => {
    it("renders a role badge with the correct text for each user", async () => {
      const users = [
        makeUserRead({ id: "a", display_name: "A", role: "Admin" }),
        makeUserRead({ id: "c", display_name: "C", role: "Contributor" }),
        makeUserRead({ id: "v", display_name: "V", role: "Viewer" }),
      ];
      server.use(http.get("/api/admin/users", () => HttpResponse.json(users)));

      renderUserManagement();

      // The badge wrapper carries the testid; the inner Badge text
      // matches the role exactly. Use findByTestId for the first row
      // so the test waits for data to load, then getByTestId for the
      // remaining rows (already in the DOM by that point).
      const badgeA = await screen.findByTestId("user-row-role-badge-a");
      expect(badgeA).toHaveTextContent("Admin");

      const badgeC = screen.getByTestId("user-row-role-badge-c");
      expect(badgeC).toHaveTextContent("Contributor");

      const badgeV = screen.getByTestId("user-row-role-badge-v");
      expect(badgeV).toHaveTextContent("Viewer");
    });

    it("renders one role badge per user in a 5-user list cycling through roles", async () => {
      // makeAdminUserList(5) cycles roles through USER_ROLE_VALUES so
      // the resulting list contains a mix of Admin / Contributor /
      // Viewer rows. We assert exactly five badges are present, one
      // per row, after the data resolves.
      server.use(http.get("/api/admin/users", () => HttpResponse.json(makeAdminUserList(5))));

      renderUserManagement();

      await waitFor(() => {
        const badges = screen.queryAllByTestId(/^user-row-role-badge-/);
        expect(badges).toHaveLength(5);
      });
    });
  });

  // -------------------------------------------------------------------------
  // Same-role select no-op
  // -------------------------------------------------------------------------

  describe("same-role select no-op", () => {
    it("does NOT open the Modal when the same role is re-selected", async () => {
      const users = [
        makeUserRead({
          id: "u-1",
          display_name: "Alice",
          role: "Contributor",
        }),
      ];
      server.use(http.get("/api/admin/users", () => HttpResponse.json(users)));

      renderUserManagement();

      const select = await screen.findByTestId("user-row-role-select-u-1");
      expect(select).toBeInTheDocument();

      // Selecting the SAME value as the row.role triggers the change
      // event but the component's handleRoleSelect short-circuits
      // because `newRole === user.role`. The Modal stays closed.
      await userEvent.selectOptions(select, "Contributor");

      // The Modal primitive ALWAYS renders the dialog and its children
      // in the DOM; the open state is observed via the `open`
      // attribute. After a same-role selection the dialog must NOT
      // have the open attribute.
      const dialog = screen.getByTestId("modal-dialog");
      expect(dialog).not.toHaveAttribute("open");
    });

    it("does not fire the role-update mutation on same-role select", async () => {
      let patchCalled = false;
      const users = [
        makeUserRead({
          id: "u-1",
          display_name: "Alice",
          role: "Contributor",
        }),
      ];
      server.use(
        http.get("/api/admin/users", () => HttpResponse.json(users)),
        http.patch("/api/admin/users/:id", () => {
          patchCalled = true;
          return HttpResponse.json(makeUserRead({ id: "u-1", role: "Contributor" }));
        }),
      );

      renderUserManagement();

      const select = await screen.findByTestId("user-row-role-select-u-1");
      await userEvent.selectOptions(select, "Contributor");

      // Belt-and-suspenders: verify the PATCH endpoint was never hit.
      // The handler captures patchCalled at request time; if the
      // mutation fired, the boolean would be true.
      expect(patchCalled).toBe(false);
    });
  });

  // -------------------------------------------------------------------------
  // Different-role select opens Modal
  // -------------------------------------------------------------------------

  describe("different-role select opens Modal", () => {
    it("opens the confirmation Modal with cancel/confirm buttons and title/description", async () => {
      const users = [
        makeUserRead({
          id: "u-1",
          display_name: "Alice",
          role: "Contributor",
        }),
      ];
      server.use(http.get("/api/admin/users", () => HttpResponse.json(users)));

      renderUserManagement();

      const select = await screen.findByTestId("user-row-role-select-u-1");
      await userEvent.selectOptions(select, "Viewer");

      // Wait for the dialog's `open` attribute to be set by the
      // .showModal() polyfill (see tests/setup.ts). The dialog
      // element itself is always in the DOM; only the attribute
      // toggles.
      const dialog = await screen.findByTestId("modal-dialog");
      await waitFor(() => expect(dialog).toHaveAttribute("open"));

      // Cancel/Confirm buttons live inside the dialog. Scope queries
      // via within(dialog) so collisions with row text outside the
      // modal cannot occur.
      expect(within(dialog).getByTestId("role-change-cancel")).toBeInTheDocument();
      expect(within(dialog).getByTestId("role-change-confirm")).toBeInTheDocument();

      // The Modal primitive renders the title via aria-labelledby +
      // a visible <h2>; the description renders as a sibling <p>.
      expect(within(dialog).getByText(/change user role\?/i)).toBeInTheDocument();
      expect(within(dialog).getByText(/recorded in the audit log/i)).toBeInTheDocument();

      // Contributor -> Viewer is NOT a demotion of an Admin; the
      // demotion warning panel must be absent.
      expect(within(dialog).queryByTestId("role-change-demotion-warning")).not.toBeInTheDocument();
    });

    it("shows the current role and new role badges inside the Modal", async () => {
      const users = [
        makeUserRead({
          id: "u-1",
          display_name: "Alice",
          role: "Contributor",
        }),
      ];
      server.use(http.get("/api/admin/users", () => HttpResponse.json(users)));

      renderUserManagement();

      const select = await screen.findByTestId("user-row-role-select-u-1");
      await userEvent.selectOptions(select, "Viewer");

      const dialog = await screen.findByTestId("modal-dialog");
      await waitFor(() => expect(dialog).toHaveAttribute("open"));

      // The dl inside the Modal has dt/dd pairs for User, Email,
      // Current role, New role; the role labels are rendered via
      // the Badge primitive whose visible text is the role name.
      expect(within(dialog).getByText("Alice")).toBeInTheDocument();
      // Both the row badge AND the modal badge contain the text
      // "Contributor"; using within(dialog) scopes the assertion to
      // the modal subtree only.
      expect(within(dialog).getByText("Contributor")).toBeInTheDocument();
      expect(within(dialog).getByText("Viewer")).toBeInTheDocument();
    });

    it("Cancel closes the Modal without firing the mutation", async () => {
      let patchCalled = false;
      const users = [
        makeUserRead({
          id: "u-1",
          display_name: "Alice",
          role: "Contributor",
        }),
      ];
      server.use(
        http.get("/api/admin/users", () => HttpResponse.json(users)),
        http.patch("/api/admin/users/:id", () => {
          patchCalled = true;
          return HttpResponse.json(makeUserRead({ id: "u-1", role: "Viewer" }));
        }),
      );

      renderUserManagement();

      const select = await screen.findByTestId("user-row-role-select-u-1");
      await userEvent.selectOptions(select, "Viewer");

      const dialog = await screen.findByTestId("modal-dialog");
      await waitFor(() => expect(dialog).toHaveAttribute("open"));

      // Click the Cancel button via the dialog-scoped query.
      const cancelButton = within(dialog).getByTestId("role-change-cancel");
      await userEvent.click(cancelButton);

      // Dialog closes (open attribute removed by .close() polyfill).
      await waitFor(() => {
        expect(dialog).not.toHaveAttribute("open");
      });

      // Critically: the PATCH endpoint was NEVER hit.
      expect(patchCalled).toBe(false);
    });
  });

  // -------------------------------------------------------------------------
  // Demotion warning
  // -------------------------------------------------------------------------

  describe("demotion warning", () => {
    it("shows the demotion warning panel when demoting an Admin to Contributor", async () => {
      const users = [
        makeUserRead({
          id: "admin-1",
          display_name: "Boss",
          role: "Admin",
        }),
      ];
      server.use(http.get("/api/admin/users", () => HttpResponse.json(users)));

      renderUserManagement();

      const select = await screen.findByTestId("user-row-role-select-admin-1");
      await userEvent.selectOptions(select, "Contributor");

      const dialog = await screen.findByTestId("modal-dialog");
      await waitFor(() => expect(dialog).toHaveAttribute("open"));

      // The amber warning panel testid is present only when
      // `pending.user.role === 'Admin' && pending.newRole !== 'Admin'`.
      const warning = within(dialog).getByTestId("role-change-demotion-warning");
      expect(warning).toBeInTheDocument();
      expect(warning).toHaveTextContent(
        /demoting an admin removes their administrative privileges/i,
      );

      // Confirm button label switches to "Demote user" for demotions.
      const confirmButton = within(dialog).getByTestId("role-change-confirm");
      expect(confirmButton).toHaveTextContent(/demote user/i);
    });

    it("shows the demotion warning when demoting an Admin to Viewer", async () => {
      // Both Admin -> Contributor and Admin -> Viewer are demotions
      // (current=Admin, new!=Admin); the warning condition is
      // role-symmetric and applies equally.
      const users = [
        makeUserRead({
          id: "admin-2",
          display_name: "Other Boss",
          role: "Admin",
        }),
      ];
      server.use(http.get("/api/admin/users", () => HttpResponse.json(users)));

      renderUserManagement();

      const select = await screen.findByTestId("user-row-role-select-admin-2");
      await userEvent.selectOptions(select, "Viewer");

      const dialog = await screen.findByTestId("modal-dialog");
      await waitFor(() => expect(dialog).toHaveAttribute("open"));

      expect(within(dialog).getByTestId("role-change-demotion-warning")).toBeInTheDocument();
      expect(within(dialog).getByTestId("role-change-confirm")).toHaveTextContent(/demote user/i);
    });

    it("does NOT show the demotion warning for Contributor -> Admin (promotion)", async () => {
      const users = [
        makeUserRead({
          id: "u-1",
          display_name: "Alice",
          role: "Contributor",
        }),
      ];
      server.use(http.get("/api/admin/users", () => HttpResponse.json(users)));

      renderUserManagement();

      const select = await screen.findByTestId("user-row-role-select-u-1");
      await userEvent.selectOptions(select, "Admin");

      const dialog = await screen.findByTestId("modal-dialog");
      await waitFor(() => expect(dialog).toHaveAttribute("open"));

      // The warning is conditional on Admin -> non-Admin; promoting
      // someone to Admin has no warning.
      expect(within(dialog).queryByTestId("role-change-demotion-warning")).not.toBeInTheDocument();

      // Confirm button label is "Update role" for non-demotions.
      expect(within(dialog).getByTestId("role-change-confirm")).toHaveTextContent(/update role/i);
    });

    it("does NOT show the demotion warning for Viewer -> Contributor (lateral, non-Admin)", async () => {
      const users = [
        makeUserRead({
          id: "u-1",
          display_name: "Alice",
          role: "Viewer",
        }),
      ];
      server.use(http.get("/api/admin/users", () => HttpResponse.json(users)));

      renderUserManagement();

      const select = await screen.findByTestId("user-row-role-select-u-1");
      await userEvent.selectOptions(select, "Contributor");

      const dialog = await screen.findByTestId("modal-dialog");
      await waitFor(() => expect(dialog).toHaveAttribute("open"));

      // Neither Viewer nor Contributor matches the warning condition
      // (current=Admin && new!=Admin); the warning panel is absent.
      expect(within(dialog).queryByTestId("role-change-demotion-warning")).not.toBeInTheDocument();

      // Confirm button label remains "Update role" for non-demotions.
      expect(within(dialog).getByTestId("role-change-confirm")).toHaveTextContent(/update role/i);
    });

    it("does NOT show the demotion warning for Admin -> Admin (no-op short-circuit)", async () => {
      const users = [
        makeUserRead({
          id: "admin-1",
          display_name: "Boss",
          role: "Admin",
        }),
      ];
      server.use(http.get("/api/admin/users", () => HttpResponse.json(users)));

      renderUserManagement();

      const select = await screen.findByTestId("user-row-role-select-admin-1");
      await userEvent.selectOptions(select, "Admin");

      // Same-role short-circuit means the Modal never opens AT ALL,
      // so the warning panel is also absent. The dialog must NOT
      // have the open attribute.
      const dialog = screen.getByTestId("modal-dialog");
      expect(dialog).not.toHaveAttribute("open");
    });
  });

  // -------------------------------------------------------------------------
  // Confirming role change (success path)
  // -------------------------------------------------------------------------

  describe("confirming role change", () => {
    it("fires the mutation with the correct user id and role payload", async () => {
      let receivedPayload: { role?: string } | null = null;
      let receivedId: string | null = null;
      const users = [
        makeUserRead({
          id: "u-1",
          display_name: "Alice",
          role: "Contributor",
        }),
      ];
      server.use(
        http.get("/api/admin/users", () => HttpResponse.json(users)),
        // Capture the path param and the request body so we can
        // assert the mutation hook sends `{ userId: "u-1", payload:
        // { role: "Viewer" } }` exactly. The endpoint URL is
        // /api/admin/users/u-1; MSW path params extract the id.
        http.patch("/api/admin/users/:id", async ({ request, params }) => {
          receivedId = params.id as string;
          receivedPayload = (await request.json()) as { role?: string };
          return HttpResponse.json(
            makeUserRead({
              id: "u-1",
              display_name: "Alice",
              role: "Viewer",
            }),
          );
        }),
      );

      renderUserManagement();

      const select = await screen.findByTestId("user-row-role-select-u-1");
      await userEvent.selectOptions(select, "Viewer");

      const dialog = await screen.findByTestId("modal-dialog");
      await waitFor(() => expect(dialog).toHaveAttribute("open"));

      const confirmButton = within(dialog).getByTestId("role-change-confirm");
      await userEvent.click(confirmButton);

      // The PATCH endpoint was hit with the row's id and the new
      // role in the body.
      await waitFor(() => {
        expect(receivedId).toBe("u-1");
        expect(receivedPayload).toEqual({ role: "Viewer" });
      });
    });

    it("closes the Modal on a successful mutation", async () => {
      const users = [
        makeUserRead({
          id: "u-1",
          display_name: "Alice",
          role: "Contributor",
        }),
      ];
      server.use(
        http.get("/api/admin/users", () => HttpResponse.json(users)),
        http.patch("/api/admin/users/:id", () =>
          HttpResponse.json(
            makeUserRead({
              id: "u-1",
              display_name: "Alice",
              role: "Viewer",
            }),
          ),
        ),
      );

      renderUserManagement();

      const select = await screen.findByTestId("user-row-role-select-u-1");
      await userEvent.selectOptions(select, "Viewer");

      const dialog = await screen.findByTestId("modal-dialog");
      await waitFor(() => expect(dialog).toHaveAttribute("open"));

      const confirmButton = within(dialog).getByTestId("role-change-confirm");
      await userEvent.click(confirmButton);

      // After onSuccess fires, the component calls setPendingChange(null)
      // which flips isOpen to false; the Modal's useEffect then calls
      // dialog.close() which removes the open attribute.
      await waitFor(() => {
        expect(dialog).not.toHaveAttribute("open");
      });
    });

    it("shows the component's per-user success toast with display_name and new role", async () => {
      const users = [
        makeUserRead({
          id: "u-1",
          display_name: "Alice Carter",
          role: "Contributor",
        }),
      ];
      server.use(
        http.get("/api/admin/users", () => HttpResponse.json(users)),
        http.patch("/api/admin/users/:id", () =>
          HttpResponse.json(
            makeUserRead({
              id: "u-1",
              display_name: "Alice Carter",
              role: "Viewer",
            }),
          ),
        ),
      );

      renderUserManagement();

      const select = await screen.findByTestId("user-row-role-select-u-1");
      await userEvent.selectOptions(select, "Viewer");

      const dialog = await screen.findByTestId("modal-dialog");
      await waitFor(() => expect(dialog).toHaveAttribute("open"));

      await userEvent.click(within(dialog).getByTestId("role-change-confirm"));

      // The component-level success toast format is
      // `Updated ${display_name} to ${role}.` with the literal period
      // at the end. The hook also fires its own success toast
      // ("User role updated") but this assertion targets the
      // user-specific message which is unique enough to not collide.
      expect(await screen.findByText(/Updated Alice Carter to Viewer\./i)).toBeInTheDocument();
    });

    it("fires the demotion mutation when confirming an Admin -> Contributor change", async () => {
      let receivedPayload: { role?: string } | null = null;
      const users = [
        makeUserRead({
          id: "admin-1",
          display_name: "Boss",
          role: "Admin",
        }),
      ];
      server.use(
        http.get("/api/admin/users", () => HttpResponse.json(users)),
        http.patch("/api/admin/users/:id", async ({ request }) => {
          receivedPayload = (await request.json()) as { role?: string };
          return HttpResponse.json(
            makeUserRead({
              id: "admin-1",
              display_name: "Boss",
              role: "Contributor",
            }),
          );
        }),
      );

      renderUserManagement();

      const select = await screen.findByTestId("user-row-role-select-admin-1");
      await userEvent.selectOptions(select, "Contributor");

      const dialog = await screen.findByTestId("modal-dialog");
      await waitFor(() => expect(dialog).toHaveAttribute("open"));

      // Even with the demotion warning shown, the Demote user button
      // fires the mutation when clicked - the warning is informational
      // only.
      await userEvent.click(within(dialog).getByTestId("role-change-confirm"));

      await waitFor(() => {
        expect(receivedPayload).toEqual({ role: "Contributor" });
      });
    });
  });

  // -------------------------------------------------------------------------
  // Special error mapping (409 last admin, 403 self-demote)
  // -------------------------------------------------------------------------

  describe("special error mapping", () => {
    it("handles the 409 last-remaining-Admin error and closes the Modal", async () => {
      const users = [
        makeUserRead({
          id: "admin-only",
          display_name: "Lone Admin",
          role: "Admin",
        }),
      ];
      server.use(
        http.get("/api/admin/users", () => HttpResponse.json(users)),
        // The override returns 409 with code "last_admin_lockout".
        // useUpdateUserRoleMutation maps the 409 status to the
        // friendly toast "Cannot demote the last remaining Admin."
        // (with period) per the hook contract in api/admin.ts.
        overrides.admin.roleUpdateLastAdmin("admin-only"),
      );

      renderUserManagement();

      const select = await screen.findByTestId("user-row-role-select-admin-only");
      await userEvent.selectOptions(select, "Contributor");

      const dialog = await screen.findByTestId("modal-dialog");
      await waitFor(() => expect(dialog).toHaveAttribute("open"));

      await userEvent.click(within(dialog).getByTestId("role-change-confirm"));

      // The component's onError handler calls setPendingChange(null)
      // so the Modal closes even though the mutation failed; this
      // unobstructs the toast emitted by the hook.
      await waitFor(
        () => {
          expect(dialog).not.toHaveAttribute("open");
        },
        { timeout: 2000 },
      );

      // The hook's 409 branch fires toast.error("Cannot demote the
      // last remaining Admin.") - the toast appears in the
      // ToastContainer-portal subtree.
      expect(
        await screen.findByText(/Cannot demote the last remaining Admin\./i),
      ).toBeInTheDocument();
    });

    it("handles the 403 self-demotion error and closes the Modal", async () => {
      const sessionUserId = "self-id";
      const users = [
        makeUserRead({
          id: sessionUserId,
          display_name: "Self Admin",
          role: "Admin",
        }),
      ];
      server.use(
        http.get("/api/admin/users", () => HttpResponse.json(users)),
        // The override returns 403 with code "self_demotion".
        // useUpdateUserRoleMutation maps the 403 status to the
        // friendly toast "You cannot demote yourself." (with period).
        overrides.admin.roleUpdateSelfDemotion(sessionUserId),
      );

      // Render with the same id as the row's id so the (you) marker
      // appears - the test verifies the self-demotion path exactly
      // mirrors the production scenario.
      renderUserManagement("Admin", sessionUserId);

      const select = await screen.findByTestId(`user-row-role-select-${sessionUserId}`);
      await userEvent.selectOptions(select, "Contributor");

      const dialog = await screen.findByTestId("modal-dialog");
      await waitFor(() => expect(dialog).toHaveAttribute("open"));

      await userEvent.click(within(dialog).getByTestId("role-change-confirm"));

      // Modal closes on error per the component's onError handler.
      await waitFor(
        () => {
          expect(dialog).not.toHaveAttribute("open");
        },
        { timeout: 2000 },
      );

      // The hook's 403 branch fires the special self-demotion toast.
      expect(await screen.findByText(/You cannot demote yourself\./i)).toBeInTheDocument();
    });

    it("does NOT fire the component success toast on error", async () => {
      const users = [
        makeUserRead({
          id: "admin-only",
          display_name: "Lone Admin",
          role: "Admin",
        }),
      ];
      server.use(
        http.get("/api/admin/users", () => HttpResponse.json(users)),
        overrides.admin.roleUpdateLastAdmin("admin-only"),
      );

      renderUserManagement();

      const select = await screen.findByTestId("user-row-role-select-admin-only");
      await userEvent.selectOptions(select, "Contributor");

      const dialog = await screen.findByTestId("modal-dialog");
      await waitFor(() => expect(dialog).toHaveAttribute("open"));

      await userEvent.click(within(dialog).getByTestId("role-change-confirm"));

      // Wait for the error toast to appear (deterministic point).
      await screen.findByText(/Cannot demote the last remaining Admin\./i);

      // The component's per-user success message must NOT be present
      // because onSuccess never ran. This guards against a regression
      // where both branches fire toasts.
      expect(screen.queryByText(/Updated Lone Admin to Contributor\./i)).not.toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // RoleGate defense-in-depth
  // -------------------------------------------------------------------------

  describe("RoleGate defense-in-depth", () => {
    it("hides the role-edit Select when the session role is Contributor", async () => {
      const users = [
        makeUserRead({
          id: "u-1",
          display_name: "Alice",
          role: "Contributor",
        }),
      ];
      server.use(http.get("/api/admin/users", () => HttpResponse.json(users)));

      renderUserManagement("Contributor");

      // Wait for users to load so the assertion is on a fully-rendered
      // table.
      await screen.findByText("Alice");

      // The role badge is still visible (read-only display).
      expect(screen.getByTestId("user-row-role-badge-u-1")).toBeInTheDocument();

      // The role-edit Select is hidden by <RoleGate role="Admin">
      // since the session role is Contributor; the entire <Select>
      // subtree is replaced by RoleGate's null fallback.
      expect(screen.queryByTestId("user-row-role-select-u-1")).not.toBeInTheDocument();
    });

    it("hides the role-edit Select when the session role is Viewer", async () => {
      const users = [
        makeUserRead({
          id: "u-1",
          display_name: "Alice",
          role: "Contributor",
        }),
      ];
      server.use(http.get("/api/admin/users", () => HttpResponse.json(users)));

      renderUserManagement("Viewer");

      await screen.findByText("Alice");

      // Same behavior as Contributor: the Viewer role does not
      // satisfy <RoleGate role="Admin">.
      expect(screen.queryByTestId("user-row-role-select-u-1")).not.toBeInTheDocument();

      // The badge remains visible so Viewers still see the role
      // assignments without being able to mutate them.
      expect(screen.getByTestId("user-row-role-badge-u-1")).toBeInTheDocument();
    });

    it("renders the role-edit Select for Admin sessions", async () => {
      const users = [
        makeUserRead({
          id: "u-1",
          display_name: "Alice",
          role: "Contributor",
        }),
      ];
      server.use(http.get("/api/admin/users", () => HttpResponse.json(users)));

      renderUserManagement("Admin");

      // Admin satisfies <RoleGate role="Admin"> so the Select is
      // present and interactable.
      expect(await screen.findByTestId("user-row-role-select-u-1")).toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // Modal pending state (cannot close during pending)
  // -------------------------------------------------------------------------

  describe("modal pending state", () => {
    it("disables the Cancel button while the mutation is in flight", async () => {
      const users = [
        makeUserRead({
          id: "u-1",
          display_name: "Alice",
          role: "Contributor",
        }),
      ];
      server.use(
        http.get("/api/admin/users", () => HttpResponse.json(users)),
        http.patch("/api/admin/users/:id", async () => {
          // 300ms gives the test a deterministic in-flight window
          // during which the loading-state assertions can fire before
          // the mutation completes. Long enough to assert, short
          // enough not to slow the suite.
          await delay(300);
          return HttpResponse.json(
            makeUserRead({
              id: "u-1",
              display_name: "Alice",
              role: "Viewer",
            }),
          );
        }),
      );

      renderUserManagement();

      const select = await screen.findByTestId("user-row-role-select-u-1");
      await userEvent.selectOptions(select, "Viewer");

      const dialog = await screen.findByTestId("modal-dialog");
      await waitFor(() => expect(dialog).toHaveAttribute("open"));

      await userEvent.click(within(dialog).getByTestId("role-change-confirm"));

      // Mutation now in-flight; the Button primitive renders the
      // spinner (data-testid="button-loading-spinner") inside the
      // confirm button while loading is true.
      await waitFor(() => {
        expect(within(dialog).getByTestId("button-loading-spinner")).toBeInTheDocument();
      });

      // While pending, the dialog stays open (cannot be dismissed).
      expect(dialog).toHaveAttribute("open");

      // Cancel button is disabled per the source contract; clicking
      // a disabled button via @testing-library/user-event is a no-op
      // (the action is dropped silently).
      const cancelButton = within(dialog).getByTestId("role-change-cancel");
      expect(cancelButton).toBeDisabled();

      // Wait for the mutation to complete; Modal then closes via the
      // onSuccess setPendingChange(null) call.
      await waitFor(
        () => {
          expect(dialog).not.toHaveAttribute("open");
        },
        { timeout: 2000 },
      );
    });

    it("renders the loading spinner inside the Confirm button while the mutation is in flight", async () => {
      const users = [
        makeUserRead({
          id: "u-1",
          display_name: "Alice",
          role: "Contributor",
        }),
      ];
      server.use(
        http.get("/api/admin/users", () => HttpResponse.json(users)),
        http.patch("/api/admin/users/:id", async () => {
          await delay(200);
          return HttpResponse.json(makeUserRead({ id: "u-1", role: "Viewer" }));
        }),
      );

      renderUserManagement();

      const select = await screen.findByTestId("user-row-role-select-u-1");
      await userEvent.selectOptions(select, "Viewer");

      const dialog = await screen.findByTestId("modal-dialog");
      await waitFor(() => expect(dialog).toHaveAttribute("open"));

      await userEvent.click(within(dialog).getByTestId("role-change-confirm"));

      // The loading spinner is the universal Button.tsx visual
      // indicator (data-testid="button-loading-spinner") that the
      // Confirm button is in its loading state.
      await waitFor(() => {
        expect(within(dialog).getByTestId("button-loading-spinner")).toBeInTheDocument();
      });

      // The spinner is gone once the mutation resolves.
      await waitFor(
        () => {
          expect(dialog).not.toHaveAttribute("open");
        },
        { timeout: 2000 },
      );
    });

    it("does NOT close the Modal when the backdrop is clicked during pending", async () => {
      const users = [
        makeUserRead({
          id: "u-1",
          display_name: "Alice",
          role: "Contributor",
        }),
      ];
      server.use(
        http.get("/api/admin/users", () => HttpResponse.json(users)),
        http.patch("/api/admin/users/:id", async () => {
          await delay(300);
          return HttpResponse.json(makeUserRead({ id: "u-1", role: "Viewer" }));
        }),
      );

      renderUserManagement();

      const select = await screen.findByTestId("user-row-role-select-u-1");
      await userEvent.selectOptions(select, "Viewer");

      const dialog = await screen.findByTestId("modal-dialog");
      await waitFor(() => expect(dialog).toHaveAttribute("open"));

      await userEvent.click(within(dialog).getByTestId("role-change-confirm"));

      // Wait for the spinner so we know we are mid-flight.
      await waitFor(() => {
        expect(within(dialog).getByTestId("button-loading-spinner")).toBeInTheDocument();
      });

      // Clicking the dialog itself simulates a backdrop click in
      // jsdom; the Modal primitive treats event.target ===
      // dialogRef.current as a backdrop click. With
      // closeOnBackdropClick={!isMutating} the click is ignored
      // while pending.
      await userEvent.click(dialog);

      // Dialog still open mid-flight.
      expect(dialog).toHaveAttribute("open");

      // Wait for the mutation to complete so the test cleans up.
      await waitFor(
        () => {
          expect(dialog).not.toHaveAttribute("open");
        },
        { timeout: 2000 },
      );
    });
  });
});
