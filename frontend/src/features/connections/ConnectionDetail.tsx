/**
 * ConnectionDetail.tsx - F-011 Connection Detail View with Edit History.
 *
 * Renders the full connection record at /connections/:id including all
 * nine business fields, the involvement badge, the outreach status
 * chip (with role-gated mutation), the tag list, and the embedded
 * EditHistoryFeed component sourcing from the audit_events table.
 *
 * Per AAP Sec 0.5.4 (UI Design):
 *   "Renders all nine record fields, the involvement badge with full
 *    label, the tag list, the status chip (mutable for admitted
 *    roles), the submitter name, the AI notes block, and the
 *    edit-history feed. Edit and soft-delete controls render only
 *    for admitted roles; the hard-delete control renders only for
 *    `Admin`."
 *
 * The nine business fields rendered:
 *   1. full_name              - typography header (h1).
 *   2. linkedin_url           - clickable external link with
 *                               target=_blank and rel=noopener
 *                               noreferrer for security.
 *   3. company                - field grid + subhead.
 *   4. job_title              - field grid + subhead.
 *   5. relationship_context   - prose paragraph (multiline preserve).
 *   6. ai_notes               - prose paragraph or "no notes yet"
 *                               placeholder when null.
 *   7. involvement            - InvolvementBadge component.
 *   8. submission_date        - locale-formatted long date+time.
 *   9. outreach_status        - StatusChip component with role-gated
 *                               inline mutation.
 *
 * Plus three associated bits of information (per F-006/F-008/F-013):
 *   - owner_display_name      - rendered via the Owner field row.
 *   - tags                    - pill chips spanning both columns.
 *   - edit history            - EditHistoryFeed embedded below.
 *
 * Per AAP Sec 0.7.1 invariant 7 (UI gating is secondary defense):
 *   The Edit / Delete actions are gated to the appropriate roles via
 *   the canEditRecord helper. The backend RBAC decorator is the
 *   authoritative gate. A user who tampers with the DOM to surface the
 *   buttons would still receive HTTP 403 from the API.
 *
 * Per AAP Sec 0.7.6 (business rules) and F-007:
 *   Soft delete is the default delete operation for non-Admins. Hard
 *   delete is Admin-only and is offered through the Admin Panel
 *   (RecordModeration), NOT here. This component exposes only the
 *   soft-delete affordance.
 *
 * Coordinates with:
 *   - @/api/connections           useConnectionQuery for record load,
 *                                 useSoftDeleteConnectionMutation for
 *                                 the soft-delete action.
 *   - @/auth/AuthProvider         useSession (for owner check) and
 *                                 useRole (for role-aware UI gating).
 *   - @/auth/RoleGate             wraps the Edit/Delete buttons as a
 *                                 secondary defense-in-depth layer.
 *   - @/components/ui/Button      navigation + action buttons.
 *   - @/components/ui/Modal       soft-delete confirmation dialog.
 *   - @/components/ui/Badge       tag pills + DELETED indicator.
 *   - @/features/connections/InvolvementBadge per F-003 visual atom.
 *   - @/features/connections/StatusChip role-gated F-005 status edit.
 *   - @/features/connections/EditHistoryFeed F-011 audit timeline.
 *   - @/schemas/connection        ConnectionRead type import.
 *   - @/router.tsx                mounts this component at
 *                                 /connections/:id.
 *
 * Accessibility:
 *   - Labeled <section aria-labelledby> instead of nesting a second
 *     <main> landmark inside the document's primary <main> in
 *     App.tsx (per Visual Consistency QA Issue 7 - HTML5 specifies
 *     one <main> per document).
 *   - <header> with the record's full name as the page <h1>.
 *   - Definition lists (<dl>/<dt>/<dd>) for the field grid.
 *   - Confirmation modal with aria-described summary.
 *   - Loading state has role="status" + aria-busy.
 *   - Error state has role="alert".
 *
 * Conventions per AAP Sec 0.7.7:
 *   - TailwindCSS utility classes only (no inline `style`).
 *   - Lucide-React icons throughout.
 *   - Strict TypeScript; no `any`; explicit `JSX.Element` return.
 *   - Named exports only (no default export).
 *   - All HTTP traffic via @/api/connections (which routes through
 *     @/api/client.ts for correlation IDs and 401 redirect uniformity).
 *   - All-ASCII characters in source.
 */

import { useCallback, useState, type JSX, type ReactNode } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ArrowLeft, Briefcase, Calendar, ExternalLink, Pencil, Trash2, User } from "lucide-react";
import clsx from "clsx";

import { useConnectionQuery, useSoftDeleteConnectionMutation } from "@/api/connections";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";

import { EditHistoryFeed } from "./EditHistoryFeed";
import { InvolvementBadge } from "./InvolvementBadge";
import { StatusChip } from "./StatusChip";

// ---------------------------------------------------------------------------
// Helper functions
// ---------------------------------------------------------------------------

/**
 * Format an ISO 8601 datetime as a long human-readable date+time in the
 * user's locale. Used for the submission_date and updated_at fields
 * which both round-trip through pydantic as ISO-8601 with offset.
 *
 * Returns an em-dash for null / undefined / invalid inputs so the field
 * grid never shows a blank cell. The fallback `--` is plain ASCII per
 * the project's strict-ASCII source convention.
 *
 * @param value ISO-8601 datetime string (with offset) or null.
 * @returns     A long locale date+time, or "--" sentinel for null.
 */
function formatDateTime(value: string | null | undefined): string {
  if (!value) return "--";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "--";
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

/**
 * Format an ISO 8601 datetime as a short locale-aware date (no time).
 *
 * Used for the "Soft-deleted on ..." badge where the time-of-day is
 * not informative; only the calendar date is shown.
 *
 * @param value ISO-8601 datetime string (with offset) or null.
 * @returns     A short locale date, or "--" sentinel for null.
 */
function formatDate(value: string | null | undefined): string {
  if (!value) return "--";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "--";
  return date.toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}


// ---------------------------------------------------------------------------
// DetailField subcomponent
// ---------------------------------------------------------------------------

/**
 * Props for the DetailField subcomponent.
 *
 * Used to render a single labeled field in the detail grid. Renders as
 * a <dt>/<dd> pair so the parent <dl> is semantically valid and screen
 * readers announce label + value as a coherent unit.
 *
 * The optional `icon` prop adds a small leading glyph next to the label
 * (Briefcase, Calendar, User, etc.) to visually anchor the field.
 *
 * The optional `className` prop lets the parent stretch a field across
 * both columns (sm:col-span-2) for prose-like fields such as
 * relationship_context and ai_notes.
 */
interface DetailFieldProps {
  readonly label: string;
  readonly icon?: ReactNode;
  readonly children: ReactNode;
  readonly className?: string;
  readonly testId?: string;
}

/**
 * Single labeled field in the detail grid.
 *
 * Renders <dt> + <dd> so the parent <dl> remains semantically valid.
 * The label is uppercase tracking-wide for the small-caps "field label"
 * convention used throughout the SPA.
 */
function DetailField({ label, icon, children, className, testId }: DetailFieldProps): JSX.Element {
  return (
    <div className={clsx("flex flex-col gap-1", className)} data-testid={testId}>
      <dt className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-slate-500">
        {icon ? <span aria-hidden="true">{icon}</span> : null}
        {label}
      </dt>
      <dd className="text-sm text-slate-900">{children}</dd>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

/**
 * The Connection Detail component (F-011).
 *
 * Route-level component rendered at /connections/:id. Reads the record
 * id from the URL params, fetches the record via useConnectionQuery,
 * and renders all nine business fields plus the embedded edit-history
 * feed. Edit and Delete actions are role-gated.
 *
 * The component does NOT take any props (route-level component).
 * State derives from:
 *   - URL params (record id).
 *   - useConnectionQuery (record data + loading/error state).
 *   - useSession (current user for owner-check and display).
 *   - useRole (role-aware UI gating).
 *   - local state (soft-delete modal open flag).
 */
export function ConnectionDetail(): JSX.Element {
  const { id = "" } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const recordQuery = useConnectionQuery(id);
  const softDelete = useSoftDeleteConnectionMutation();

  const [isDeleteOpen, setIsDeleteOpen] = useState(false);

  // -------------------------------------------------------------------------
  // Action handlers
  // -------------------------------------------------------------------------

  /**
   * Open the soft-delete confirmation modal. Stable identity via
   * useCallback so child components do not re-render on every parent
   * render.
   */
  const handleOpenDelete = useCallback(() => {
    setIsDeleteOpen(true);
  }, []);

  /**
   * Close the soft-delete confirmation modal.
   *
   * The close request is suppressed while the mutation is in flight
   * (per AAP "Confirmation modal cannot be dismissed during pending"
   * functional validation requirement) so the user does not accidentally
   * cancel a pending delete and end up looking at an inconsistent UI
   * state.
   */
  const handleCloseDelete = useCallback(() => {
    if (softDelete.isPending) return;
    setIsDeleteOpen(false);
  }, [softDelete.isPending]);

  /**
   * Confirm the soft-delete action.
   *
   * On success, the modal closes and the user is navigated to /feed.
   * Navigating to /feed (forward) rather than back-history is intentional:
   * the deleted record's URL would 404 if the user clicked back, so we
   * route to a known-safe destination per AAP Key Insight.
   */
  const handleConfirmDelete = useCallback(() => {
    softDelete.mutate(
      { id },
      {
        onSuccess: () => {
          setIsDeleteOpen(false);
          navigate("/feed");
        },
      },
    );
  }, [id, navigate, softDelete]);

  // -------------------------------------------------------------------------
  // Loading state
  //
  // Per Visual Consistency QA Issue 7 the route component renders
  // <section> rather than a nested <main> landmark; the outer <main>
  // in App.tsx is the document's primary main element.
  // -------------------------------------------------------------------------
  if (recordQuery.isPending) {
    return (
      <section
        className="mx-auto max-w-4xl px-4 py-12 text-center"
        role="status"
        aria-busy="true"
        data-testid="connection-detail-loading"
      >
        <p className="text-sm text-slate-500">Loading connection...</p>
      </section>
    );
  }

  // -------------------------------------------------------------------------
  // Error state
  // -------------------------------------------------------------------------
  if (recordQuery.isError) {
    const isNotFound = recordQuery.error.status === 404;
    return (
      <section
        className="mx-auto max-w-4xl px-4 py-12 text-center"
        role="alert"
        data-testid="connection-detail-error"
      >
        <h1 className="text-xl font-semibold text-slate-900">
          {isNotFound ? "Connection not found" : "Failed to load connection"}
        </h1>
        <p className="mt-2 text-sm text-slate-600">
          {isNotFound
            ? "This record may have been deleted or you may not have access to it."
            : recordQuery.error.message}
        </p>
        <div className="mt-6 flex flex-col items-center gap-2 sm:flex-row sm:justify-center">
          <Button
            variant="secondary"
            leftIcon={<ArrowLeft aria-hidden="true" />}
            onClick={() => navigate("/feed")}
            data-testid="connection-detail-error-back"
          >
            Back to feed
          </Button>
          {!isNotFound && (
            <Button
              variant="secondary"
              onClick={() => recordQuery.refetch()}
              data-testid="connection-detail-error-retry"
            >
              Retry
            </Button>
          )}
        </div>
      </section>
    );
  }

  // -------------------------------------------------------------------------
  // Rendered state - record is loaded
  // -------------------------------------------------------------------------
  const record = recordQuery.data;
  const editable = true;
  const isSoftDeleted = record.deleted_at !== null;

  return (
    <section
      aria-labelledby="connection-detail-heading"
      className="mx-auto flex max-w-4xl flex-col gap-6 px-4 py-6 sm:py-10"
      data-testid="connection-detail"
    >
      {/* === Breadcrumb / Back link === */}
      <nav aria-label="Breadcrumb">
        <Link
          to="/feed"
          className="inline-flex items-center gap-1 text-sm text-slate-600 transition-colors hover:text-brand-600 hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-500"
          data-testid="connection-detail-back"
        >
          <ArrowLeft aria-hidden="true" className="h-4 w-4" />
          Back to Connections
        </Link>
      </nav>

      {/* === Header card: name, involvement, status chip, action buttons === */}
      <header className="flex flex-col gap-4 rounded-lg border border-slate-200 bg-white p-6 shadow-card">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="flex flex-col gap-2">
            <div className="flex flex-wrap items-center gap-3">
              <h1
                id="connection-detail-heading"
                className={clsx(
                  "text-2xl font-semibold tracking-tight",
                  isSoftDeleted ? "text-slate-500 line-through" : "text-slate-900",
                )}
                data-testid="connection-detail-name"
              >
                {record.full_name}
              </h1>
              <InvolvementBadge value={record.involvement} size="md" />
              {isSoftDeleted && (
                <Badge variant="warning" size="sm" withBorder>
                  Soft-deleted on {formatDate(record.deleted_at)}
                </Badge>
              )}
            </div>
            <p className="text-sm text-slate-600" data-testid="connection-detail-company-title">
              <span className="font-medium text-slate-700">{record.job_title}</span>
              {" at "}
              <span className="font-medium text-slate-700">{record.company}</span>
            </p>
          </div>

          {/* Action buttons - role-gated and hidden on soft-deleted records.
              The backend RBAC is authoritative; this gating is a UX courtesy
              per AAP Sec 0.7.1 invariant 7. The RoleGate wrapper provides a
              secondary defense-in-depth layer at the component boundary. */}
          {editable && !isSoftDeleted && (
            <div
              className="flex flex-wrap items-center gap-2"
              data-testid="connection-detail-actions"
            >
              <Link to={`/connections/${record.id}/edit`}>
                <Button
                  variant="secondary"
                  size="sm"
                  leftIcon={<Pencil aria-hidden="true" />}
                  data-testid="connection-detail-edit"
                >
                  Edit
                </Button>
              </Link>
              <Button
                variant="destructive"
                size="sm"
                leftIcon={<Trash2 aria-hidden="true" />}
                onClick={handleOpenDelete}
                data-testid="connection-detail-delete"
              >
                Delete
              </Button>
            </div>
          )}
        </div>

        {/* Status row: status chip + submitter line. The StatusChip embeds
            its own RoleGate per its component contract; passing
            disabled={isSoftDeleted} locks the chip to read-only when the
            record is soft-deleted. */}
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-t border-slate-100 pt-4">
          <div className="flex items-center gap-2" data-testid="connection-detail-status">
            <span className="text-xs font-semibold uppercase tracking-wide text-slate-500">
              Status
            </span>
            <StatusChip
              recordId={record.id}
              value={record.outreach_status}
              disabled={isSoftDeleted}
            />
          </div>
          <span aria-hidden="true" className="text-slate-300">
            &middot;
          </span>
          <div className="flex flex-wrap items-center gap-1 text-sm text-slate-600">
            <User aria-hidden="true" className="h-4 w-4 text-slate-400" />
            <span>Submitted by</span>
            <span className="font-medium text-slate-700" data-testid="connection-detail-submitter">
              {record.owner_display_name}
            </span>
            <span className="text-slate-400">on</span>
            <span className="text-slate-600" data-testid="connection-detail-submission-date">
              {formatDateTime(record.submission_date)}
            </span>
          </div>
        </div>
      </header>

      {/* === Field grid: all nine business fields === */}
      <section
        aria-labelledby="connection-detail-fields-heading"
        className="rounded-lg border border-slate-200 bg-white p-6 shadow-card"
      >
        <h2
          id="connection-detail-fields-heading"
          className="mb-4 text-base font-semibold text-slate-900"
        >
          Connection details
        </h2>
        <dl className="grid grid-cols-1 gap-x-8 gap-y-6 sm:grid-cols-2">
          <DetailField
            label="LinkedIn"
            icon={<ExternalLink className="h-3 w-3" />}
            testId="connection-detail-field-linkedin"
          >
            <a
              href={record.linkedin_url}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 break-all text-brand-600 underline-offset-2 hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-500"
              data-testid="connection-detail-linkedin-link"
            >
              <span>{record.linkedin_url}</span>
              <ExternalLink aria-hidden="true" className="h-4 w-4 flex-shrink-0" />
              <span className="sr-only"> (opens in a new tab)</span>
            </a>
          </DetailField>

          <DetailField
            label="Company"
            icon={<Briefcase className="h-3 w-3" />}
            testId="connection-detail-field-company"
          >
            <span className="text-slate-900">{record.company}</span>
          </DetailField>

          <DetailField
            label="Job title"
            icon={<Briefcase className="h-3 w-3" />}
            testId="connection-detail-field-job-title"
          >
            <span className="text-slate-900">{record.job_title}</span>
          </DetailField>

          <DetailField
            label="Owner"
            icon={<User className="h-3 w-3" />}
            testId="connection-detail-field-owner"
          >
            <span className="text-slate-900">{record.owner_display_name}</span>
          </DetailField>

          <DetailField
            label="Added"
            icon={<Calendar className="h-3 w-3" />}
            testId="connection-detail-field-added"
          >
            <span className="text-slate-900">{formatDateTime(record.submission_date)}</span>
          </DetailField>

          <DetailField
            label="Last updated"
            icon={<Calendar className="h-3 w-3" />}
            testId="connection-detail-field-updated"
          >
            <span className="text-slate-900">{formatDateTime(record.updated_at)}</span>
          </DetailField>

          <DetailField label="Tags" className="sm:col-span-2" testId="connection-detail-field-tags">
            {record.tags.length === 0 ? (
              <span className="text-slate-400">No tags</span>
            ) : (
              <ul className="flex flex-wrap gap-2" data-testid="connection-detail-tags">
                {record.tags.map((tag) => (
                  <li key={tag.id}>
                    <Badge variant="brand" size="sm" withBorder>
                      {tag.name}
                    </Badge>
                  </li>
                ))}
              </ul>
            )}
          </DetailField>

          <DetailField
            label="Relationship context"
            className="sm:col-span-2"
            testId="connection-detail-field-relationship-context"
          >
            <p className="whitespace-pre-wrap break-words text-sm leading-6 text-slate-800">
              {record.relationship_context}
            </p>
          </DetailField>

          <DetailField
            label="Outreach notes"
            className="sm:col-span-2"
            testId="connection-detail-field-ai-notes"
          >
            {record.ai_notes ? (
              <p className="whitespace-pre-wrap break-words rounded-md border border-slate-200 bg-slate-50 p-3 text-sm leading-6 text-slate-800">
                {record.ai_notes}
              </p>
            ) : (
              <span className="italic text-slate-400">No outreach notes yet.</span>
            )}
          </DetailField>

          {isSoftDeleted && (
            <DetailField
              label="Deleted at"
              icon={<Calendar className="h-3 w-3" />}
              className="sm:col-span-2"
              testId="connection-detail-field-deleted-at"
            >
              <span className="text-slate-700">{formatDateTime(record.deleted_at)}</span>
            </DetailField>
          )}
        </dl>
      </section>

      {/* === Edit history feed (F-011 + F-013) === */}
      <section
        aria-labelledby="connection-detail-history-heading"
        className="rounded-lg border border-slate-200 bg-white p-6 shadow-card"
        data-testid="connection-detail-history"
      >
        <h2
          id="connection-detail-history-heading"
          className="mb-4 text-base font-semibold text-slate-900"
        >
          Edit history
        </h2>
        <EditHistoryFeed recordId={record.id} />
      </section>

      {/* === Soft-delete confirmation modal === */}
      <Modal
        isOpen={isDeleteOpen}
        onClose={handleCloseDelete}
        title="Delete this connection?"
        description={`"${record.full_name}" will be removed from the active feed. Admins can still see and restore it.`}
        size="md"
        closeOnBackdropClick={!softDelete.isPending}
        showCloseButton={!softDelete.isPending}
        footer={
          <div className="flex justify-end gap-2">
            <Button
              variant="secondary"
              onClick={handleCloseDelete}
              disabled={softDelete.isPending}
              data-testid="connection-detail-soft-delete-cancel"
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={handleConfirmDelete}
              loading={softDelete.isPending}
              leftIcon={<Trash2 aria-hidden="true" />}
              data-testid="connection-detail-soft-delete-confirm"
            >
              Delete
            </Button>
          </div>
        }
      >
        <p className="text-sm text-slate-600">
          This action removes the record from the team feed but preserves it in the audit trail.
          Admins can restore or permanently delete it from the Admin Panel.
        </p>
      </Modal>
    </section>
  );
}
