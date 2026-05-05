/**
 * RecordModeration.tsx - F-014 + F-007 Admin Records moderation view.
 *
 * Mounted at /admin/records as a child of the AdminPanel layout. Provides:
 *   - Listing of ALL records in the org including soft-deleted (toggle).
 *   - Visual differentiation of soft-deleted records (DELETED badge,
 *     muted row styling, deleted_at timestamp column).
 *   - Admin-only hard-delete action that emits a 'hard_delete' audit event
 *     server-side (per AAP Sec 0.5.2 Layer 6 and Sec 0.7.1 invariant 5).
 *   - Confirmation Modal before any hard-delete to satisfy the destructive-
 *     action UX rule from the assigned folder requirements.
 *
 * Per AAP Sec 0.7.1:
 *   - The backend /api/admin/records endpoints are gated by @requires_role
 *     (Admin); the SPA's RoleGate is secondary defense.
 *   - Hard delete is a permanent operation and ALWAYS emits an audit row
 *     atomically inside the parent transaction.
 *
 * Per AAP Sec 0.7.7:
 *   - TailwindCSS utility classes only
 *   - Lucide-React icons
 *   - Strict TypeScript; no any
 *   - No direct fetch (uses TanStack Query hooks from @/api/admin)
 *   - All mutations confirm via Modal first
 *   - Toast on success; mutation hook attaches its own error toast
 *
 * State machine (ASCII):
 *
 *     [list] --click Hard delete--> [confirm modal open]
 *     [confirm modal open] --Cancel/Esc/backdrop--> [list]
 *     [confirm modal open] --Confirm--> [mutation pending]
 *     [mutation pending] --success--> [toast + list refetched]
 *     [mutation pending] --error--> [hook's error toast; modal stays open]
 *
 * Coordinates with:
 *   - @/api/admin                useAdminRecordsQuery, useHardDeleteRecordMutation
 *   - @/schemas/connection       ConnectionRead (row type with deleted_at)
 *   - @/components/ui/Badge      OutreachStatus pill + DELETED pill
 *   - @/components/ui/Button     Per-row + modal footer + retry actions
 *   - @/components/ui/Modal      Confirmation dialog before hard-delete
 *   - @/components/ui/Table      Generic typed Table primitive
 *   - @/components/ui/Toast      useToast() success feedback
 *   - @/router.tsx               Mounted at /admin/records (Admin-only).
 */

import { useMemo, useState, type JSX } from "react";
import { AlertTriangle, FileWarning, RefreshCw, ShieldAlert, Trash2 } from "lucide-react";
import clsx from "clsx";

import { useAdminRecordsQuery, useHardDeleteRecordMutation } from "@/api/admin";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { Table, type TableColumn } from "@/components/ui/Table";
import { useToast } from "@/components/ui/Toast";
import type { ConnectionRead } from "@/schemas/connection";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Format an ISO date string as a short locale-aware date for table cells.
 *
 * Returns the en-dash placeholder "-" (U+2014 EM DASH) when the input is
 * null or empty so the column never renders empty whitespace; an empty
 * cell would be visually ambiguous with a missing column.
 *
 * Wrapped in try/catch because `new Date(value)` does not throw for an
 * unparseable string but rather produces an Invalid Date whose
 * .toLocaleDateString() returns "Invalid Date" - we degrade gracefully
 * to the raw input string in that case so debugging an upstream payload
 * problem is easier than seeing "Invalid Date" everywhere.
 */
function formatDateCell(value: string | null): string {
  if (!value) return "\u2014";
  try {
    return new Date(value).toLocaleDateString();
  } catch {
    return value;
  }
}

/**
 * Map OutreachStatus to Badge variant. Mirrors the convention used by
 * Analytics.tsx and StatusChip.tsx for consistency across the SPA.
 *
 * The four cases below match `OUTREACH_STATUS_VALUES` from
 * `@/schemas/connection` exactly. The function's return type is the
 * literal-string union of the four `outreach-*` Badge variants, so
 * callers consuming the return value get full type safety - a typo here
 * is a TypeScript error rather than a runtime missing-style issue.
 */
function statusToBadgeVariant(
  status: ConnectionRead["outreach_status"],
): "outreach-not-started" | "outreach-in-progress" | "outreach-contacted" | "outreach-closed" {
  switch (status) {
    case "Not Started":
      return "outreach-not-started";
    case "In Progress":
      return "outreach-in-progress";
    case "Contacted":
      return "outreach-contacted";
    case "Closed":
      return "outreach-closed";
  }
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/**
 * Records page size for the admin moderation view. Hardcoded server-side
 * sort by `submission_date DESC` (newest first) matches the AAP F-004
 * default order; admin moderation almost always wants the newest records
 * first. A dedicated constant (rather than a hardcoded literal in the
 * useAdminRecordsQuery call) makes the value easy to find, easy to tune,
 * and self-documenting at the call sites.
 */
const PAGE_SIZE = 50;

// ---------------------------------------------------------------------------
// Hard-delete confirmation modal sub-component
// ---------------------------------------------------------------------------

/**
 * Props for the HardDeleteModal sub-component.
 *
 * `record` is nullable so the parent can keep the modal mounted in the
 * React tree for instant re-opens (the Modal primitive uses a controlled
 * <dialog> that toggles via .showModal() / .close() rather than mount /
 * unmount). When `record` is null, the body simply omits the metadata
 * dl block.
 */
interface HardDeleteModalProps {
  readonly record: ConnectionRead | null;
  readonly isOpen: boolean;
  readonly isPending: boolean;
  readonly onClose: () => void;
  readonly onConfirm: () => void;
}

/**
 * Confirmation modal for hard-delete. Uses the SPA's Modal primitive with
 * a destructive footer button that triggers the actual mutation.
 *
 * The mutation hook itself attaches an error toast on failure; this
 * component is responsible only for capturing the user's confirmation
 * intent. The parent component (RecordModeration) attaches the
 * record-name-specific success toast on `onSuccess`.
 *
 * Keyboard / dismiss semantics:
 *   - Cancel button (or Escape, or backdrop click) closes the modal IF
 *     the mutation is not currently pending. The Modal primitive's
 *     onClose is wired to a no-op when isPending=true so the user
 *     cannot close the dialog mid-flight (which would be confusing if
 *     the operation later fails and the modal had already disappeared).
 */
function HardDeleteModal({
  record,
  isOpen,
  isPending,
  onClose,
  onConfirm,
}: HardDeleteModalProps): JSX.Element {
  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title="Hard delete record?"
      description="This action permanently removes the record. It cannot be undone."
      size="md"
      footer={
        <div className="flex justify-end gap-2">
          <Button
            variant="secondary"
            onClick={onClose}
            disabled={isPending}
            data-testid="hard-delete-cancel"
          >
            Cancel
          </Button>
          <Button
            variant="destructive"
            onClick={onConfirm}
            loading={isPending}
            leftIcon={<Trash2 aria-hidden="true" />}
            data-testid="hard-delete-confirm"
          >
            Hard delete
          </Button>
        </div>
      }
    >
      <div className="flex flex-col gap-3 text-sm text-slate-700">
        <div className="flex items-start gap-2 rounded-md border border-amber-200 bg-amber-50 p-3 text-amber-900">
          <AlertTriangle aria-hidden="true" className="mt-0.5 h-5 w-5 flex-shrink-0" />
          <p>
            Hard deletion permanently removes the record from the database. The audit history for
            this record is preserved (audit_events is append-only) but the record itself cannot be
            recovered.
          </p>
        </div>
        {record && (
          <dl className="grid grid-cols-3 gap-x-3 gap-y-1 rounded-md bg-slate-50 p-3 text-sm">
            <dt className="text-slate-500">Name</dt>
            <dd className="col-span-2 font-medium text-slate-900">{record.full_name}</dd>
            <dt className="text-slate-500">Company</dt>
            <dd className="col-span-2 text-slate-700">{record.company}</dd>
            <dt className="text-slate-500">Owner</dt>
            <dd className="col-span-2 text-slate-700">{record.owner_display_name}</dd>
            {record.deleted_at && (
              <>
                <dt className="text-slate-500">Soft-deleted</dt>
                <dd className="col-span-2 text-slate-700">{formatDateCell(record.deleted_at)}</dd>
              </>
            )}
          </dl>
        )}
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------------------
// Error state sub-component
// ---------------------------------------------------------------------------

/**
 * Props for the ErrorState sub-component.
 *
 * `errorMessage` is a string (not the full ApiError) so the component
 * does not have to worry about exposing internal error properties; the
 * parent extracts a user-facing message from the ApiError before
 * rendering.
 */
interface ErrorStateProps {
  readonly errorMessage: string;
  readonly onRetry: () => void;
}

/**
 * Renders an inline error card for the records moderation surface.
 *
 * Per AAP Sec 0.7.7:
 *   - role="alert" makes the message announce to assistive tech.
 *   - The retry button is a real <Button> (not a styled <a>) because
 *     it triggers a JS-only refetch, not navigation.
 *   - The ShieldAlert icon is decorative (aria-hidden); the heading is
 *     the accessible name.
 */
function ErrorState({ errorMessage, onRetry }: ErrorStateProps): JSX.Element {
  return (
    <div
      className="flex flex-col items-center gap-3 rounded-lg border border-red-200 bg-red-50 p-8 text-center"
      role="alert"
      data-testid="record-moderation-error"
    >
      <ShieldAlert aria-hidden="true" className="h-8 w-8 text-red-600" />
      <h3 className="text-base font-semibold text-red-900">Could not load records</h3>
      <p className="text-sm text-red-700" data-testid="record-moderation-error-message">
        {errorMessage || "Something went wrong loading the records."}
      </p>
      <Button
        variant="secondary"
        size="sm"
        onClick={onRetry}
        leftIcon={<RefreshCw aria-hidden="true" />}
        data-testid="record-moderation-retry"
      >
        Try again
      </Button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// RecordModeration component
// ---------------------------------------------------------------------------

/**
 * Admin Record Moderation view. Top-level component mounted at
 * /admin/records.
 *
 * State (all useState):
 *   - includeDeleted: whether to include soft-deleted records in the
 *     listing. Default false (active records only); user toggles via the
 *     header checkbox to true.
 *   - page: 1-indexed page number for pagination. Reset to 1 whenever
 *     `includeDeleted` toggles so the user does not end up on a now-
 *     out-of-range page after the result set shrinks/grows.
 *   - confirmingRecord: the record currently in the hard-delete
 *     confirmation modal. `null` means the modal is closed; a non-null
 *     value drives the modal's `isOpen={confirmingRecord !== null}`
 *     prop.
 *
 * Server data:
 *   - useAdminRecordsQuery({ include_deleted, page, page_size, sort,
 *     sort_dir }) - paginated records list. include_deleted is REQUIRED
 *     per the @/api/admin contract.
 *   - useHardDeleteRecordMutation() - hard-delete mutation. The hook
 *     auto-invalidates adminKeys.recordsAll(), connectionKeys.all, and
 *     adminKeys.analytics() on success; we do NOT manually refetch.
 *
 * Mutation success path:
 *   - Optimistic update is intentionally NOT used; hard-delete is
 *     destructive and waiting for the server confirmation before
 *     dimming the row is the right UX (per @/api/admin documented
 *     decision).
 *   - On success: dispatch a record-name-specific success toast
 *     ('Permanently deleted "X".') and close the confirmation modal.
 *   - On error: handled by the hook's default error toast; we do NOT
 *     override that here.
 *
 * @returns The RecordModeration view as a JSX.Element.
 */
export function RecordModeration(): JSX.Element {
  // ---------------------------------------------------------------------
  // Local state
  // ---------------------------------------------------------------------

  const [includeDeleted, setIncludeDeleted] = useState<boolean>(false);
  const [page, setPage] = useState<number>(1);
  const [confirmingRecord, setConfirmingRecord] = useState<ConnectionRead | null>(null);

  const toast = useToast();

  // ---------------------------------------------------------------------
  // Server data + mutations
  //
  // include_deleted is REQUIRED per the useAdminRecordsQuery contract;
  // page, page_size, sort, sort_dir are passed explicitly so the cache
  // key encodes the exact slice and reads are deterministic.
  // ---------------------------------------------------------------------

  const recordsQuery = useAdminRecordsQuery({
    include_deleted: includeDeleted,
    page,
    page_size: PAGE_SIZE,
    sort: "submission_date",
    sort_dir: "desc",
  });

  const hardDeleteMutation = useHardDeleteRecordMutation();

  // ---------------------------------------------------------------------
  // Event handlers
  // ---------------------------------------------------------------------

  /**
   * Toggle whether soft-deleted records are included AND reset the page
   * to 1. Reset is necessary because toggling can shrink (only-active)
   * or grow (include-deleted) the result set; without reset the user
   * could land on a now-empty page.
   */
  const handleToggleIncludeDeleted = (next: boolean): void => {
    setIncludeDeleted(next);
    setPage(1);
  };

  /**
   * Confirm-handler for the modal. Captures `recordId` and `recordName`
   * BEFORE invoking mutate so the closure used by onSuccess does not
   * race with a possibly-cleared `confirmingRecord` state.
   *
   * Defensive `!confirmingRecord` guard handles the (logically
   * impossible) case where the modal somehow rendered with a null
   * record; this guard makes the function safe to call regardless of
   * caller behavior.
   */
  const handleConfirmHardDelete = (): void => {
    if (!confirmingRecord) return;
    const recordId = confirmingRecord.id;
    const recordName = confirmingRecord.full_name;
    hardDeleteMutation.mutate(
      { recordId },
      {
        onSuccess: () => {
          toast.success(`Permanently deleted "${recordName}".`);
          setConfirmingRecord(null);
        },
        // onError is intentionally NOT overridden; the hook's default
        // error handler attaches its own error toast per the
        // @/api/admin contract. Overriding here would either suppress
        // that toast (a regression) or duplicate it (UX noise).
      },
    );
  };

  // ---------------------------------------------------------------------
  // Derived values
  // ---------------------------------------------------------------------

  /**
   * Total pages derived from `total` (across all filter slices) and the
   * fixed PAGE_SIZE. `Math.max(1, ...)` guarantees that the displayed
   * page indicator never reads "Page 1 of 0" (which is technically
   * possible when `total === 0` due to integer division).
   */
  const totalPages = useMemo(() => {
    if (!recordsQuery.data) return 1;
    return Math.max(1, Math.ceil(recordsQuery.data.total / PAGE_SIZE));
  }, [recordsQuery.data]);

  /**
   * Column definitions for the records table. Memoized with an empty
   * dependency array because the column array does not depend on any
   * state or prop; React re-uses the same reference on every render
   * which keeps the Table primitive's referential equality checks
   * happy.
   *
   * `setConfirmingRecord` is stable across renders (useState setters
   * have a referentially stable identity), so the closure inside the
   * "actions" column.render does not need to be in the deps array per
   * the React rules-of-hooks rule about stable setters.
   */
  const columns: ReadonlyArray<TableColumn<ConnectionRead>> = useMemo(
    () => [
      {
        key: "full_name",
        header: "Name",
        render: (row) => (
          <span
            className={clsx(
              "text-sm font-medium",
              row.deleted_at ? "text-slate-500 line-through" : "text-slate-900",
            )}
            data-testid={`record-row-name-${row.id}`}
          >
            {row.full_name}
          </span>
        ),
      },
      {
        key: "company",
        header: "Company",
        render: (row) => (
          <span className={clsx("text-sm", row.deleted_at ? "text-slate-400" : "text-slate-700")}>
            {row.company}
          </span>
        ),
        hideBelow: "md",
      },
      {
        key: "owner_display_name",
        header: "Owner",
        render: (row) => (
          <span className={clsx("text-sm", row.deleted_at ? "text-slate-400" : "text-slate-700")}>
            {row.owner_display_name}
          </span>
        ),
        hideBelow: "lg",
      },
      {
        key: "outreach_status",
        header: "Status",
        render: (row) => (
          <Badge variant={statusToBadgeVariant(row.outreach_status)} withDot size="sm">
            {row.outreach_status}
          </Badge>
        ),
      },
      {
        key: "submission_date",
        header: "Submitted",
        render: (row) => (
          <span className="text-sm text-slate-700 tabular-nums">
            {formatDateCell(row.submission_date)}
          </span>
        ),
        hideBelow: "md",
      },
      {
        key: "deleted_at",
        header: "Deleted",
        render: (row) =>
          row.deleted_at ? (
            <Badge variant="danger" size="sm" withDot>
              DELETED {formatDateCell(row.deleted_at)}
            </Badge>
          ) : (
            <span className="text-xs text-slate-400">{"\u2014"}</span>
          ),
        hideBelow: "lg",
      },
      {
        key: "actions",
        header: "Actions",
        align: "right",
        render: (row) => (
          <Button
            variant="destructive"
            size="sm"
            onClick={() => setConfirmingRecord(row)}
            leftIcon={<Trash2 aria-hidden="true" />}
            data-testid={`record-row-hard-delete-${row.id}`}
            aria-label={`Hard delete ${row.full_name}`}
          >
            Hard delete
          </Button>
        ),
        widthClassName: "w-40",
      },
    ],
    [],
  );

  // ---------------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------------

  return (
    // Per Visual Consistency QA Issue 7 the route component renders
    // <section> rather than nesting a second <main> landmark inside
    // the document's primary <main> in App.tsx.
    <section
      aria-labelledby="admin-record-moderation-heading"
      className="flex flex-col gap-4"
      data-testid="admin-record-moderation"
    >
      <header className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-center gap-2">
          <FileWarning aria-hidden="true" className="h-6 w-6 text-slate-700" />
          <h2 id="admin-record-moderation-heading" className="text-xl font-semibold text-slate-900">
            Record moderation
          </h2>
        </div>
        <label
          className="inline-flex cursor-pointer select-none items-center gap-2 text-sm text-slate-700"
          data-testid="record-moderation-include-deleted-toggle"
        >
          <input
            type="checkbox"
            checked={includeDeleted}
            onChange={(e) => handleToggleIncludeDeleted(e.target.checked)}
            className={clsx(
              "h-4 w-4 rounded border-slate-300 text-brand-600",
              "focus:ring-2 focus:ring-brand-500 focus:ring-offset-1",
            )}
            data-testid="record-moderation-include-deleted-checkbox"
          />
          <span>Show soft-deleted records</span>
        </label>
      </header>

      {recordsQuery.isError && (
        <ErrorState
          errorMessage={recordsQuery.error?.message ?? ""}
          onRetry={() => {
            void recordsQuery.refetch();
          }}
        />
      )}

      {!recordsQuery.isError && (
        <Table<ConnectionRead>
          columns={columns}
          data={recordsQuery.data?.items ?? []}
          rowKey={(row) => row.id}
          isLoading={recordsQuery.isPending}
          caption="All records in the organization, with optional inclusion of soft-deleted entries."
          emptyState={
            <div className="py-8 text-center text-sm text-slate-500">
              {includeDeleted
                ? "No records found, even when including soft-deleted entries."
                : 'No active records yet. Toggle "Show soft-deleted records" to include deleted ones.'}
            </div>
          }
        />
      )}

      {recordsQuery.data && recordsQuery.data.total > PAGE_SIZE && (
        <nav
          aria-label="Records pagination"
          className="flex items-center justify-between gap-2 border-t border-slate-200 pt-3"
          data-testid="record-moderation-pagination"
        >
          <p className="text-sm text-slate-600 tabular-nums">
            Page {page} of {totalPages} &middot; {recordsQuery.data.total.toLocaleString()} total
          </p>
          <div className="flex gap-2">
            <Button
              variant="secondary"
              size="sm"
              onClick={() => setPage((p) => Math.max(1, p - 1))}
              disabled={page <= 1 || recordsQuery.isFetching}
              data-testid="record-moderation-prev-page"
            >
              Previous
            </Button>
            <Button
              variant="secondary"
              size="sm"
              onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
              disabled={page >= totalPages || recordsQuery.isFetching}
              data-testid="record-moderation-next-page"
            >
              Next
            </Button>
          </div>
        </nav>
      )}

      <HardDeleteModal
        record={confirmingRecord}
        isOpen={confirmingRecord !== null}
        isPending={hardDeleteMutation.isPending}
        onClose={() => {
          if (!hardDeleteMutation.isPending) {
            setConfirmingRecord(null);
          }
        }}
        onConfirm={handleConfirmHardDelete}
      />
    </section>
  );
}
