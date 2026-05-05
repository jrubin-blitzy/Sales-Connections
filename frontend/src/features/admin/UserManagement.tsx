/**
 * UserManagement.tsx - F-014 + F-009 Admin user/role management view.
 *
 * Mounted at /admin/users as a child of the AdminPanel layout. Provides:
 *   - Listing of all org users (id, display_name, email, current role,
 *     joined date) via the useAdminUsersQuery() TanStack Query hook
 *     (GET /api/admin/users; returns a plain UserRead[] array - no
 *     pagination per the api/admin.ts contract).
 *   - Role-edit dropdown per row (Admin / Contributor / Viewer) gated by
 *     <RoleGate role="Admin"> as UI defense-in-depth (the backend
 *     @requires_role(Admin) decorator on /api/admin/users/:id is the
 *     authoritative gate per AAP Sec 0.7.1 invariant 7).
 *   - Confirmation Modal before any role change (especially Admin
 *     demotions, which surface an amber warning panel previewing the
 *     server-side last-Admin protection).
 *   - "(you)" marker plus self-demotion helper text on the current
 *     user's row to discourage attempts the server will reject with
 *     HTTP 403 ("You cannot demote yourself").
 *   - Special-case error handling delegated to useUpdateUserRoleMutation:
 *       - 409 "Cannot demote the last remaining Admin"
 *       - 403 "You cannot demote yourself"
 *     The hook's onError attaches the toast; this component does not
 *     duplicate that logic. Only the success toast (with the user's
 *     display name) is fired here.
 *
 * Note on scope: The api/admin.ts contract documents that the role
 * mutation emits an `audit_events.event_type = 'role_change'` row
 * server-side per AAP Sec 0.7.1 invariant 6 (atomic state-change +
 * audit emit). The SPA does not re-render the audit log in this view;
 * admins can find role-change audit rows via the Connection Detail
 * edit-history feed if needed (F-011 / F-013).
 *
 * Per AAP Sec 0.7.7 Coding and Quality Standards:
 *   - TailwindCSS utility classes only; no inline styles.
 *   - Lucide-React icons throughout.
 *   - Strict TypeScript; no `any`; explicit JSX.Element return types.
 *   - No direct fetch (uses TanStack Query hooks from @/api/admin).
 *   - Mutations confirm via a Modal first (two-stage confirmation flow).
 *   - Toast on success.
 *   - Path imports use the `@/` alias for src.
 *   - Double quotes per project Prettier configuration
 *     (singleQuote: false).
 *
 * Coordinates with:
 *   - @/api/admin             useAdminUsersQuery, useUpdateUserRoleMutation.
 *   - @/auth/AuthProvider     useSession (nested session.user.id),
 *                              UserRole string-union type.
 *   - @/auth/RoleGate         UI defense-in-depth around the role-edit
 *                              Select (route-level RoleGate also wraps
 *                              entry to this component).
 *   - @/components/ui/Badge   Current-role pill in the Name column and
 *                              the side-by-side role badges in the
 *                              confirmation Modal.
 *   - @/components/ui/Button  Modal Cancel/Confirm + ErrorState retry.
 *   - @/components/ui/Modal   Role-change confirmation dialog.
 *   - @/components/ui/Select  Generic Select<UserRole> bound to
 *                              ROLE_SELECT_OPTIONS.
 *   - @/components/ui/Table   Generic Table<UserRead> with loading
 *                              skeleton, empty-state slot, and
 *                              per-column rendering.
 *   - @/components/ui/Toast   useToast() for the success feedback toast.
 *   - @/schemas/admin         UserRead row type, USER_ROLE_VALUES tuple.
 */

import { useCallback, useMemo, useState, type JSX, type ReactNode } from "react";
import { AlertTriangle, RefreshCw, ShieldAlert, UserCog, Users } from "lucide-react";
import clsx from "clsx";

import { useAdminUsersQuery, useUpdateUserRoleMutation } from "@/api/admin";
import { useSession, type UserRole } from "@/auth/AuthProvider";
import { RoleGate } from "@/auth/RoleGate";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { Select, type SelectOption } from "@/components/ui/Select";
import { Table, type TableColumn } from "@/components/ui/Table";
import { useToast } from "@/components/ui/Toast";
import { USER_ROLE_VALUES, type UserRead } from "@/schemas/admin";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Format an ISO 8601 date string for the "Joined" column.
 *
 * Returns "-" for null/empty input and falls back to the original string
 * if the date cannot be parsed. Locale formatting follows the user's
 * browser locale via `toLocaleDateString()`; the column is rendered with
 * `tabular-nums` so column widths align across rows.
 *
 * Defensive against malformed timestamps: `Number.isNaN(date.getTime())`
 * detects invalid dates that the Date constructor produces silently
 * (e.g., the string "not a date" yields a Date whose getTime() is NaN).
 */
function formatJoinedDate(value: string | null | undefined): string {
  if (value === null || value === undefined || value === "") {
    return "-";
  }
  try {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
      return value;
    }
    return date.toLocaleDateString();
  } catch {
    return value;
  }
}

/**
 * Map a UserRole to a Badge variant for the "Current role" column.
 *
 * The mapping reflects authority gradient at a glance:
 *   - Admin       -> brand   (most powerful; highest visual weight)
 *   - Contributor -> neutral (everyday creator role)
 *   - Viewer      -> info    (read-mostly; sales-rep status mutations)
 *
 * Exhaustive switch over the UserRole union; TypeScript flags any
 * future addition to USER_ROLE_VALUES at compile time.
 */
function roleBadgeVariant(role: UserRole): "brand" | "neutral" | "info" {
  switch (role) {
    case "Admin":
      return "brand";
    case "Contributor":
      return "neutral";
    case "Viewer":
      return "info";
  }
}

/**
 * Memoized SelectOption list for the role dropdown.
 *
 * Built once at module load (the option set does not change at
 * runtime) so each row's Select gets a stable reference - avoids
 * spurious option-list re-creations on every render of the table.
 */
const ROLE_SELECT_OPTIONS: ReadonlyArray<SelectOption<UserRole>> = USER_ROLE_VALUES.map((role) => ({
  value: role,
  label: role,
}));

// ---------------------------------------------------------------------------
// Confirmation Modal
// ---------------------------------------------------------------------------

/**
 * Pending role-change request awaiting confirmation. Captured by the
 * row's Select onChange; the mutation only fires after the user
 * confirms in the modal. `null` represents the idle state.
 */
interface PendingRoleChange {
  /** The user whose role is being changed. */
  user: UserRead;
  /** The new role the admin selected from the dropdown. */
  newRole: UserRole;
}

/**
 * Props for the internal RoleChangeModal sub-component.
 *
 * The modal is fully controlled: `pending` is null in the idle state
 * and an object during the confirmation flow. `isMutating` reflects
 * the in-flight status of the role mutation; while true the modal
 * cannot be dismissed (Cancel/Close are disabled, backdrop click and
 * Escape are inert) so the user does not accidentally abandon the
 * dialog mid-flight.
 */
interface RoleChangeModalProps {
  pending: PendingRoleChange | null;
  isMutating: boolean;
  onClose: () => void;
  onConfirm: () => void;
}

/**
 * Confirmation modal shown before a role change is committed.
 *
 * Two-stage confirmation flow:
 *   1. Selecting a new role in the per-row Select opens this modal
 *      with the chosen change captured in `pending`.
 *   2. The user clicks Confirm (or Cancel) to commit (or abort).
 *
 * Demotion-specific UX:
 *   - When the current role is Admin and the new role is not, a
 *     prominent amber warning panel previews the backend's
 *     last-Admin protection (HTTP 409). The Confirm button switches
 *     to the destructive variant and reads "Demote user".
 *   - For non-demoting changes the Confirm button is the primary
 *     variant and reads "Update role".
 *
 * Locked while pending:
 *   While `isMutating` is true the Cancel button is disabled,
 *   `closeOnBackdropClick` is false, and `showCloseButton` is false
 *   so the user cannot dismiss the dialog mid-flight. The Confirm
 *   button shows the spinner via `loading={isMutating}`.
 */
function RoleChangeModal({
  pending,
  isMutating,
  onClose,
  onConfirm,
}: RoleChangeModalProps): JSX.Element {
  // Demotion = Admin -> non-Admin. We compute it once here and reuse
  // it for the warning panel, the Confirm button variant, and the
  // Confirm button label.
  const isDemotion =
    pending !== null && pending.user.role === "Admin" && pending.newRole !== "Admin";

  return (
    <Modal
      isOpen={pending !== null}
      onClose={onClose}
      title="Change user role?"
      description="Updating a user's role takes effect immediately and is recorded in the audit log."
      size="md"
      closeOnBackdropClick={!isMutating}
      showCloseButton={!isMutating}
      footer={
        <div className="flex justify-end gap-2">
          <Button
            variant="secondary"
            onClick={onClose}
            disabled={isMutating}
            data-testid="role-change-cancel"
          >
            Cancel
          </Button>
          <Button
            variant={isDemotion ? "destructive" : "primary"}
            onClick={onConfirm}
            loading={isMutating}
            leftIcon={<UserCog aria-hidden="true" />}
            data-testid="role-change-confirm"
          >
            {isDemotion ? "Demote user" : "Update role"}
          </Button>
        </div>
      }
    >
      <div className="flex flex-col gap-3 text-sm text-slate-700">
        {isDemotion && (
          <div
            className="flex items-start gap-2 rounded-md border border-amber-200 bg-amber-50 p-3 text-amber-900"
            data-testid="role-change-demotion-warning"
          >
            <AlertTriangle aria-hidden="true" className="mt-0.5 h-5 w-5 flex-shrink-0" />
            <p>
              Demoting an Admin removes their administrative privileges. The system prevents
              demoting the last remaining Admin (you will see an error if this user is the only
              Admin in the organization).
            </p>
          </div>
        )}
        {pending && (
          <dl className="grid grid-cols-3 gap-x-3 gap-y-1 rounded-md bg-slate-50 p-3 text-sm">
            <dt className="text-slate-500">User</dt>
            <dd className="col-span-2 font-medium text-slate-900">{pending.user.display_name}</dd>
            <dt className="text-slate-500">Email</dt>
            <dd className="col-span-2 text-slate-700">{pending.user.email}</dd>
            <dt className="text-slate-500">Current role</dt>
            <dd className="col-span-2">
              <Badge variant={roleBadgeVariant(pending.user.role)}>{pending.user.role}</Badge>
            </dd>
            <dt className="text-slate-500">New role</dt>
            <dd className="col-span-2">
              <Badge variant={roleBadgeVariant(pending.newRole)}>{pending.newRole}</Badge>
            </dd>
          </dl>
        )}
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------------------
// Error state
// ---------------------------------------------------------------------------

/**
 * Props for the internal ErrorState sub-component.
 *
 * `errorMessage` is a string (not the full ApiError) so the component
 * stays decoupled from the api/client.ts ApiError type; the parent
 * extracts a user-facing message from the ApiError before passing it
 * here.
 */
interface ErrorStateProps {
  errorMessage: string;
  onRetry: () => void;
}

/**
 * Renders an inline error block when the users query fails.
 *
 * Surfaces a ShieldAlert icon, a heading, the API error message (or a
 * generic fallback), and a Retry button that re-runs the query. The
 * outer container uses `role="alert"` so screen readers announce the
 * failure as soon as the block appears.
 *
 * Why an inline error rather than a toast:
 *   - The query failure leaves the table empty; the user needs a
 *     visible recovery affordance, not a transient toast that may
 *     auto-dismiss before they can act on it.
 *   - The retry button gives the admin a one-click recovery path.
 */
function ErrorState({ errorMessage, onRetry }: ErrorStateProps): JSX.Element {
  return (
    <div
      className="flex flex-col items-center gap-3 rounded-lg border border-red-200 bg-red-50 p-8 text-center"
      role="alert"
      data-testid="user-management-error"
    >
      <ShieldAlert aria-hidden="true" className="h-8 w-8 text-red-600" />
      <h3 className="text-base font-semibold text-red-900">Could not load users</h3>
      <p className="text-sm text-red-700" data-testid="user-management-error-message">
        {errorMessage || "Something went wrong loading the user list."}
      </p>
      <Button
        variant="secondary"
        size="sm"
        onClick={onRetry}
        leftIcon={<RefreshCw aria-hidden="true" />}
        data-testid="user-management-retry"
      >
        Try again
      </Button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

/**
 * Empty-state slot rendered inside the Table when the users list is
 * empty. Hoisted to a module-level constant so the Table reference
 * does not churn on every UserManagement render.
 *
 * The wrapping <div> applies the same vertical rhythm as the loading
 * skeleton so layout stays stable when transitioning between states.
 */
const TABLE_EMPTY_STATE: ReactNode = (
  <div className="py-8 text-center text-sm text-slate-500">No users found in the organization.</div>
);

/**
 * Admin User Management view.
 *
 * Mounted at `/admin/users` per the @/router.tsx route table. The
 * route is wrapped in <ProtectedRoute> and <RoleGate role="Admin">
 * upstream so by the time this component renders, the session is
 * authenticated and confirmed to have the Admin role.
 *
 * State:
 *   - pendingChange: the role change awaiting confirmation. `null`
 *     represents the idle state. The mutation is fired only after the
 *     user confirms via the modal.
 *
 * Hook composition:
 *   - useSession()                  Self-identification ("(you)" marker
 *                                    plus helper text on the current
 *                                    user's row).
 *   - useToast()                    Success toast on role change.
 *   - useAdminUsersQuery()          GET /api/admin/users.
 *   - useUpdateUserRoleMutation()   PATCH /api/admin/users/:id.
 *
 * Why no optimistic updates:
 *   The role mutation has multiple server-side guards (anti-lockout
 *   409, self-demotion 403) that cannot be replicated client-side. An
 *   optimistic flip and roll-back on guard violation would be jarring;
 *   waiting for the server response is the correct UX.
 */
export function UserManagement(): JSX.Element {
  const [pendingChange, setPendingChange] = useState<PendingRoleChange | null>(null);

  const session = useSession();
  const toast = useToast();

  const usersQuery = useAdminUsersQuery();
  const roleMutation = useUpdateUserRoleMutation();

  /**
   * Handle a role-select change. Capture the pending change and open
   * the confirmation Modal; the mutation does NOT fire until the user
   * confirms.
   *
   * Same-role select is a no-op: re-selecting the existing role does
   * not open the modal so the user is not prompted to confirm a
   * change that is not really a change.
   */
  const handleRoleSelect = useCallback((user: UserRead, newRole: UserRole): void => {
    if (newRole === user.role) {
      return;
    }
    setPendingChange({ user, newRole });
  }, []);

  /**
   * Cancel the pending role change. No-op while the mutation is
   * in-flight (the modal cannot be dismissed mid-flight).
   */
  const handleCancelChange = useCallback((): void => {
    if (!roleMutation.isPending) {
      setPendingChange(null);
    }
  }, [roleMutation.isPending]);

  /**
   * Confirm the pending role change by firing the mutation.
   *
   * On success: shows a per-user success toast with the display name
   *   and new role, then closes the modal. The mutation hook also
   *   invalidates the users cache so the table refreshes with the
   *   new role.
   *
   * On error: closes the modal so the error toast attached by the
   *   mutation hook (special 409/403 mapping per the api/admin.ts
   *   contract) is unobstructed.
   */
  const handleConfirmChange = useCallback((): void => {
    if (!pendingChange) {
      return;
    }
    const targetUser = pendingChange.user;
    const targetRole = pendingChange.newRole;
    roleMutation.mutate(
      {
        userId: targetUser.id,
        payload: { role: targetRole },
      },
      {
        onSuccess: () => {
          toast.success(`Updated ${targetUser.display_name} to ${targetRole}.`);
          setPendingChange(null);
        },
        // The hook's onError attaches the special 409/403 toast; this
        // component does not duplicate that logic. We only close the
        // modal so the user can see the toast.
        onError: () => {
          setPendingChange(null);
        },
      },
    );
  }, [pendingChange, roleMutation, toast]);

  // -------------------------------------------------------------------------
  // Column definitions
  //
  // Memoized so the Table's prop identity is stable between renders
  // (cheap re-renders avoid spurious work in Table.tsx). Dependencies
  // are intentionally minimal:
  //   - handleRoleSelect       memoized callback
  //   - pendingChange?.user.id row identity to evaluate the
  //                            per-row disabled state
  //   - roleMutation.isPending pending flag for the disabled state
  //   - session?.user.id       self-identification for the "(you)"
  //                            marker and the helper text
  // -------------------------------------------------------------------------
  const columns: ReadonlyArray<TableColumn<UserRead>> = useMemo(
    () => [
      {
        key: "display_name",
        header: "Name",
        render: (row) => {
          const isSelf = session?.user.id === row.id;
          return (
            <div className="flex flex-col">
              <span
                className={clsx(
                  "text-sm font-medium",
                  isSelf ? "text-brand-700" : "text-slate-900",
                )}
              >
                {row.display_name}
                {isSelf && (
                  <span
                    className="ml-2 text-xs font-normal text-slate-500"
                    data-testid={`user-row-self-marker-${row.id}`}
                  >
                    (you)
                  </span>
                )}
              </span>
              <span className="text-xs text-slate-500">{row.email}</span>
            </div>
          );
        },
      },
      {
        key: "role",
        header: "Current role",
        render: (row) => (
          // Wrap the Badge in a span so we can attach a per-row
          // data-testid for tests; the Badge primitive does not
          // accept arbitrary DOM attributes.
          <span data-testid={`user-row-role-badge-${row.id}`}>
            <Badge variant={roleBadgeVariant(row.role)} withDot>
              {row.role}
            </Badge>
          </span>
        ),
      },
      {
        key: "created_at",
        header: "Joined",
        render: (row) => (
          <span className="text-sm text-slate-700 tabular-nums">
            {formatJoinedDate(row.created_at)}
          </span>
        ),
        hideBelow: "md",
      },
      {
        key: "role_edit",
        header: "Update role",
        align: "right",
        render: (row) => {
          const isSelf = session?.user.id === row.id;
          return (
            <RoleGate role="Admin">
              <Select<UserRole>
                value={row.role}
                onChange={(newRole) => handleRoleSelect(row, newRole)}
                options={ROLE_SELECT_OPTIONS}
                aria-label={`Update role for ${row.display_name}`}
                data-testid={`user-row-role-select-${row.id}`}
                disabled={roleMutation.isPending && pendingChange?.user.id === row.id}
                helperText={isSelf ? "Self-demotion is rejected by the server." : undefined}
              />
            </RoleGate>
          );
        },
        widthClassName: "w-56",
      },
    ],
    [handleRoleSelect, pendingChange?.user.id, roleMutation.isPending, session?.user.id],
  );

  // -------------------------------------------------------------------------
  // Render
  // -------------------------------------------------------------------------
  return (
    // Per Visual Consistency QA Issue 7 the route component renders
    // <section> rather than nesting a second <main> landmark inside
    // the document's primary <main> in App.tsx.
    <section
      aria-labelledby="admin-user-management-heading"
      className="flex flex-col gap-4"
      data-testid="admin-user-management"
    >
      <header className="flex items-center gap-2">
        <Users aria-hidden="true" className="h-6 w-6 text-slate-700" />
        <h2 id="admin-user-management-heading" className="text-xl font-semibold text-slate-900">
          User management
        </h2>
      </header>

      {usersQuery.isError ? (
        <ErrorState
          errorMessage={usersQuery.error?.message ?? ""}
          onRetry={() => {
            void usersQuery.refetch();
          }}
        />
      ) : (
        <Table<UserRead>
          columns={columns}
          data={usersQuery.data ?? []}
          rowKey={(row) => row.id}
          isLoading={usersQuery.isPending}
          caption="Organization users with role assignment controls."
          emptyState={TABLE_EMPTY_STATE}
        />
      )}

      <RoleChangeModal
        pending={pendingChange}
        isMutating={roleMutation.isPending}
        onClose={handleCancelChange}
        onConfirm={handleConfirmChange}
      />
    </section>
  );
}
