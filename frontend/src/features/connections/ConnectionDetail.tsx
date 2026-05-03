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
 * The nine record fields rendered:
 *   1. full_name              - typography header (h1 size).
 *   2. linkedin_url           - clickable external link (rel=noopener
 *                               noreferrer, target=_blank).
 *   3. company                - subhead next to job_title.
 *   4. job_title              - subhead next to company.
 *   5. relationship_context   - prose paragraph (multiline preserve).
 *   6. ai_notes               - prose paragraph or "no notes yet"
 *                               placeholder when null.
 *   7. involvement            - InvolvementBadge component (size=md).
 *   8. submission_date        - formatted date with locale.
 *   9. outreach_status        - StatusChip component (with role-gated
 *                               edit; falls back to read-only Badge
 *                               for Contributors).
 *
 * Plus three associated bits of information (per F-006/F-008/F-013):
 *   - owner_display_name      - "Submitted by" line (NEVER editable
 *                               per F-006 invariant).
 *   - tags                    - Pill chips below header.
 *   - edit history            - EditHistoryFeed embedded below.
 *
 * Per AAP Sec 0.7.1 invariant 7:
 *   The Edit / Delete / Hard-delete actions are gated to the
 *   appropriate roles via <RoleGate>. Per architectural invariant 7,
 *   this is a UX courtesy only; the backend's @requires_role decorator
 *   on the corresponding API endpoints is the authoritative gate. A
 *   user who tampers with the DOM to surface the buttons would still
 *   receive HTTP 403 from the API.
 *
 * Per AAP Sec 0.7.6 (Business Rules) and F-007:
 *   Soft delete is the default delete operation for non-Admins. Hard
 *   delete is Admin-only and is offered through the Admin Panel
 *   (RecordModeration), NOT here. This component exposes only the
 *   soft-delete affordance for the record owner / admin per F-007.
 *
 * Per AAP Sec 0.7.7:
 *   - TailwindCSS utility classes only (no inline `style`).
 *   - Lucide-React icons throughout (Pencil, Trash2, ExternalLink,
 *     ArrowLeft, AlertTriangle, ...).
 *   - Strict TypeScript; no `any`; explicit `JSX.Element` return.
 *   - Named exports only (no default export).
 *   - All HTTP traffic via @/api/connections (which routes through
 *     @/api/client.ts for correlation IDs and 401 redirect uniformity).
 *
 * Coordinates with:
 *   - @/api/connections                useConnectionQuery hook for
 *                                      fetching the record, plus
 *                                      useSoftDeleteConnectionMutation
 *                                      for the soft-delete action.
 *   - @/auth/AuthProvider              useSession (for owner check)
 *                                      and useRole (for hide of admin
 *                                      controls in nav).
 *   - @/auth/RoleGate                  wraps Edit / Delete buttons.
 *   - @/components/ui/Button           navigation + action buttons.
 *   - @/components/ui/Modal            soft-delete confirmation dialog.
 *   - @/components/ui/Badge            tag pills + DELETED indicator.
 *   - @/features/connections/InvolvementBadge per F-003 visual atom.
 *   - @/features/connections/StatusChip role-gated F-005 status edit.
 *   - @/features/connections/EditHistoryFeed F-011 audit timeline.
 *   - @/schemas/connection             ConnectionRead type.
 *   - @/router.tsx                     mounted at "/connections/:id".
 */

import { useCallback, useState, type JSX } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  AlertTriangle,
  ArrowLeft,
  ExternalLink,
  Pencil,
  Trash2,
  User as UserIcon,
} from "lucide-react";
import clsx from "clsx";

import { useConnectionQuery, useSoftDeleteConnectionMutation } from "@/api/connections";
import { useSession } from "@/auth/AuthProvider";
import { RoleGate } from "@/auth/RoleGate";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import type { ConnectionRead } from "@/schemas/connection";

import { EditHistoryFeed } from "./EditHistoryFeed";
import { InvolvementBadge } from "./InvolvementBadge";
import { StatusChip } from "./StatusChip";

// ---------------------------------------------------------------------------
// Helper functions
// ---------------------------------------------------------------------------

/**
 * Format an ISO datetime string as a long human-readable date+time.
 *
 * Used for `submission_date` and `deleted_at` which both round-trip
 * through pydantic as ISO-8601 with offset. The fallback to the raw
 * string preserves the user's ability to debug bad payloads (vs.
 * silently rendering "Invalid Date").
 *
 * @param value ISO-8601 datetime string (with offset) or null.
 * @returns     A long locale date+time, or em-dash for null.
 */
function formatDetailedDate(value: string | null): string {
  if (!value) return "\u2014";
  try {
    const d = new Date(value);
    return `${d.toLocaleDateString(undefined, {
      year: "numeric",
      month: "long",
      day: "numeric",
    })} at ${d.toLocaleTimeString(undefined, {
      hour: "numeric",
      minute: "2-digit",
    })}`;
  } catch {
    return value;
  }
}

// ---------------------------------------------------------------------------
// SoftDeleteModal - confirmation dialog
// ---------------------------------------------------------------------------

/**
 * Props for the SoftDeleteModal sub-component.
 *
 * Defined inline (not as a default-exported sibling) because the modal
 * is tightly coupled to ConnectionDetail's state machine: only one
 * record is ever the subject of the modal at a time, and the modal
 * is short-lived (mounted only while open=true). Exporting it would
 * imply reusability that is not the design intent.
 */
interface SoftDeleteModalProps {
  readonly record: ConnectionRead;
  readonly isOpen: boolean;
  readonly isPending: boolean;
  readonly onClose: () => void;
  readonly onConfirm: () => void;
}

/**
 * Soft-delete confirmation modal.
 *
 * Soft delete is reversible (an Admin can opt in to soft-deleted
 * records via the include_deleted query param), so the warning copy
 * is calibrated for a "soft warning" tone rather than the destructive
 * "permanently removes" warning used by RecordModeration's hard-delete
 * flow.
 *
 * The modal is locked while the mutation is pending (closeOnBackdropClick
 * forced false, Cancel button disabled, X close button hidden) to
 * prevent the user from dismissing mid-flight which would leave the
 * detail page in a confusing partial state.
 */
function SoftDeleteModal({
  record,
  isOpen,
  isPending,
  onClose,
  onConfirm,
}: SoftDeleteModalProps): JSX.Element {
  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title="Delete this connection?"
      description="The record will be hidden from the connection feed but can be restored by an Admin."
      size="md"
      closeOnBackdropClick={!isPending}
      showCloseButton={!isPending}
      footer={
        <div className="flex justify-end gap-2">
          <Button
            variant="secondary"
            onClick={onClose}
            disabled={isPending}
            data-testid="connection-detail-soft-delete-cancel"
          >
            Cancel
          </Button>
          <Button
            variant="destructive"
            onClick={onConfirm}
            loading={isPending}
            leftIcon={<Trash2 aria-hidden="true" />}
            data-testid="connection-detail-soft-delete-confirm"
          >
            Delete
          </Button>
        </div>
      }
    >
      <div className="flex flex-col gap-3 text-sm text-slate-700">
        <div
          className="flex items-start gap-2 rounded-md border border-amber-200 bg-amber-50 p-3 text-amber-900"
          role="note"
        >
          <AlertTriangle aria-hidden="true" className="mt-0.5 h-5 w-5 flex-shrink-0" />
          <p>
            Soft-deleting hides this record from feeds and lookups. The audit history is preserved
            and an Admin can restore it from the Admin Panel.
          </p>
        </div>
        <dl className="grid grid-cols-3 gap-x-3 gap-y-1 rounded-md bg-slate-50 p-3 text-sm">
          <dt className="text-slate-500">Name</dt>
          <dd className="col-span-2 font-medium text-slate-900">{record.full_name}</dd>
          <dt className="text-slate-500">Company</dt>
          <dd className="col-span-2 text-slate-700">{record.company}</dd>
          <dt className="text-slate-500">Submitter</dt>
          <dd className="col-span-2 text-slate-700">{record.owner_display_name}</dd>
        </dl>
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------------------
// Loading and error states
// ---------------------------------------------------------------------------

/**
 * Skeleton placeholder shown while the record query is pending.
 *
 * Mirrors the actual layout (header, two-column section, history) so
 * the user gets a sense of where content will appear; this is more
 * informative than a spinner and reduces perceived loading time.
 */
function ConnectionDetailSkeleton(): JSX.Element {
  return (
    <div
      className="flex flex-col gap-6 p-4 sm:p-6"
      role="status"
      aria-label="Loading connection details"
      data-testid="connection-detail-skeleton"
    >
      <div className="h-6 w-32 animate-pulse rounded bg-slate-200" />
      <div className="rounded-lg border border-slate-200 bg-white p-6 shadow-card">
        <div className="h-8 w-2/3 animate-pulse rounded bg-slate-200" />
        <div className="mt-3 h-4 w-1/3 animate-pulse rounded bg-slate-200" />
        <div className="mt-6 grid grid-cols-1 gap-3 md:grid-cols-2">
          <div className="h-4 w-full animate-pulse rounded bg-slate-200" />
          <div className="h-4 w-full animate-pulse rounded bg-slate-200" />
          <div className="h-4 w-3/4 animate-pulse rounded bg-slate-200" />
          <div className="h-4 w-1/2 animate-pulse rounded bg-slate-200" />
        </div>
      </div>
      <div className="h-32 animate-pulse rounded bg-slate-100" />
    </div>
  );
}

/**
 * Error state shown when the record query fails.
 *
 * Distinguishes 404 (record not found / cross-org) from other errors:
 *   - 404 → "Connection not found" with back-to-feed CTA.
 *   - Other → generic message with retry button.
 *
 * Per AAP Sec 0.7.1 invariant 3 (org-scoped multi-tenancy): the
 * backend returns 404 for cross-org access (information-disclosure
 * defense). This component does NOT distinguish "this record exists
 * but you cannot see it" from "this record does not exist" - the user
 * sees the same message in both cases.
 */
interface ConnectionDetailErrorProps {
  readonly status: number | undefined;
  readonly message: string;
  readonly onRetry: () => void;
}

function ConnectionDetailError({
  status,
  message,
  onRetry,
}: ConnectionDetailErrorProps): JSX.Element {
  const isNotFound = status === 404;
  return (
    <div
      role="alert"
      className="flex flex-col gap-3 p-4 sm:p-6"
      data-testid="connection-detail-error"
    >
      <div className="flex flex-col gap-2 rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-800">
        <p className="font-semibold">
          {isNotFound ? "Connection not found" : "Could not load connection"}
        </p>
        <p>
          {isNotFound
            ? "This connection does not exist or has been removed."
            : (message ?? "Unknown error")}
        </p>
        {!isNotFound && (
          <div>
            <Button
              variant="secondary"
              size="sm"
              onClick={onRetry}
              data-testid="connection-detail-error-retry"
            >
              Retry
            </Button>
          </div>
        )}
      </div>
      <div>
        <Link
          to="/feed"
          className="inline-flex items-center gap-1 text-sm text-brand-600 hover:underline"
        >
          <ArrowLeft aria-hidden="true" className="h-4 w-4" />
          Back to Connections
        </Link>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Field row helper
// ---------------------------------------------------------------------------

/**
 * Props for the FieldRow sub-component.
 *
 * A FieldRow is a labeled vertical pair: a small uppercase label above
 * the value. Used for non-prose fields (involvement, owner, dates,
 * URL). The `wide` flag stretches the value across two columns in the
 * grid (used for prose fields like relationship_context and ai_notes).
 */
interface FieldRowProps {
  readonly label: string;
  readonly children: React.ReactNode;
  readonly testId?: string;
  readonly wide?: boolean;
}

function FieldRow({ label, children, testId, wide }: FieldRowProps): JSX.Element {
  return (
    <div className={clsx("flex flex-col gap-1", wide && "md:col-span-2")} data-testid={testId}>
      <dt className="text-xs font-semibold uppercase tracking-wide text-slate-500">{label}</dt>
      <dd className="text-sm text-slate-900">{children}</dd>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Public ConnectionDetail component
// ---------------------------------------------------------------------------

/**
 * The Connection Detail component.
 *
 * Route-level component rendered at /connections/:id. Reads the record
 * id from the URL params, fetches the record via useConnectionQuery,
 * and renders all nine business fields plus the embedded edit-history
 * feed. Edit and Delete actions are role-gated.
 *
 * The component does NOT take any props (route-level component).
 * State derives from:
 *   - URL params (record id)
 *   - useConnectionQuery (record data + loading/error state)
 *   - useSession (current user for owner-check and display)
 *   - local state (soft-delete modal open flag)
 */
export function ConnectionDetail(): JSX.Element {
  const navigate = useNavigate();
  const params = useParams<{ id: string }>();
  const recordId = params.id ?? "";
  const session = useSession();

  const recordQuery = useConnectionQuery(recordId);
  const softDeleteMutation = useSoftDeleteConnectionMutation();

  const [isDeleteOpen, setIsDeleteOpen] = useState(false);

  // ---------------------------------------------------------------------
  // Action handlers
  // ---------------------------------------------------------------------
  const handleEdit = useCallback(() => {
    navigate(`/connections/${recordId}/edit`);
  }, [navigate, recordId]);

  const handleOpenDelete = useCallback(() => {
    setIsDeleteOpen(true);
  }, []);

  const handleCloseDelete = useCallback(() => {
    if (softDeleteMutation.isPending) return;
    setIsDeleteOpen(false);
  }, [softDeleteMutation.isPending]);

  const handleConfirmDelete = useCallback(() => {
    softDeleteMutation.mutate(
      { id: recordId },
      {
        onSuccess: () => {
          setIsDeleteOpen(false);
          // Navigate back to the feed so the user does not stare at
          // a now-deleted record. The feed will reflect the deletion
          // automatically because the mutation invalidated the lists
          // cache.
          navigate("/feed");
        },
      },
    );
  }, [navigate, recordId, softDeleteMutation]);

  // ---------------------------------------------------------------------
  // Loading state
  // ---------------------------------------------------------------------
  if (recordQuery.isPending) {
    return <ConnectionDetailSkeleton />;
  }

  // ---------------------------------------------------------------------
  // Error state
  // ---------------------------------------------------------------------
  if (recordQuery.isError || !recordQuery.data) {
    return (
      <ConnectionDetailError
        status={recordQuery.error?.status}
        message={recordQuery.error?.message ?? "Unknown error"}
        onRetry={() => recordQuery.refetch()}
      />
    );
  }

  const record = recordQuery.data;

  // Owner-check predicate. The backend authoritatively checks ownership
  // on edit / soft-delete (Contributor own-only; Admin any), but the UI
  // hides controls for non-owners as a UX courtesy. The `currentUserId`
  // comes from the session context and is null when the session has
  // not yet hydrated; in that brief window we conservatively treat the
  // user as a non-owner so the buttons stay hidden.
  const currentUserId = session?.user.id ?? null;
  const currentRole = session?.user.role ?? null;
  const isOwner = currentUserId !== null && record.owner_user_id === currentUserId;
  const canEditOrDelete = isOwner || currentRole === "Admin";
  const isSoftDeleted = record.deleted_at !== null;

  // ---------------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------------
  return (
    <main
      aria-labelledby="connection-detail-heading"
      className="flex flex-col gap-6 p-4 sm:p-6"
      data-testid="connection-detail"
    >
      {/* Top navigation: back link */}
      <div>
        <Link
          to="/feed"
          className="inline-flex items-center gap-1 text-sm text-slate-600 hover:text-brand-600 hover:underline"
          data-testid="connection-detail-back"
        >
          <ArrowLeft aria-hidden="true" className="h-4 w-4" />
          Back to Connections
        </Link>
      </div>

      {/* Header card with name, status chip, and actions */}
      <header className="flex flex-col gap-4 rounded-lg border border-slate-200 bg-white p-6 shadow-card">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="flex flex-col gap-1">
            <div className="flex flex-wrap items-center gap-2">
              <h1
                id="connection-detail-heading"
                className={clsx(
                  "text-2xl font-semibold",
                  isSoftDeleted ? "text-slate-500 line-through" : "text-slate-900",
                )}
                data-testid="connection-detail-name"
              >
                {record.full_name}
              </h1>
              {isSoftDeleted && (
                <Badge variant="neutral" size="sm" withBorder>
                  Deleted
                </Badge>
              )}
            </div>
            <p className="text-sm text-slate-600" data-testid="connection-detail-company-title">
              <span className="font-medium text-slate-700">{record.job_title}</span>
              {" at "}
              <span className="font-medium text-slate-700">{record.company}</span>
            </p>
          </div>

          {/* Action buttons - role-gated */}
          {canEditOrDelete && !isSoftDeleted && (
            <div
              className="flex flex-wrap items-center gap-2"
              data-testid="connection-detail-actions"
            >
              <RoleGate role={["Admin", "Contributor"]}>
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={handleEdit}
                  leftIcon={<Pencil aria-hidden="true" />}
                  disabled={!isOwner && currentRole !== "Admin"}
                  data-testid="connection-detail-edit"
                >
                  Edit
                </Button>
              </RoleGate>
              <RoleGate role={["Admin", "Contributor", "Viewer"]}>
                <Button
                  variant="destructive"
                  size="sm"
                  onClick={handleOpenDelete}
                  leftIcon={<Trash2 aria-hidden="true" />}
                  disabled={!isOwner && currentRole !== "Admin"}
                  data-testid="connection-detail-delete"
                >
                  Delete
                </Button>
              </RoleGate>
            </div>
          )}
        </div>

        {/* Status row: status chip + involvement badge + submission date */}
        <div className="flex flex-wrap items-center gap-3 border-t border-slate-100 pt-4">
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
          <div className="flex items-center gap-2" data-testid="connection-detail-involvement">
            <span className="text-xs font-semibold uppercase tracking-wide text-slate-500">
              Involvement
            </span>
            <InvolvementBadge value={record.involvement} size="md" />
          </div>
          <span aria-hidden="true" className="text-slate-300">
            &middot;
          </span>
          <div className="flex items-center gap-1 text-sm text-slate-600">
            <UserIcon aria-hidden="true" className="h-4 w-4 text-slate-400" />
            <span>Submitted by</span>
            <span className="font-medium text-slate-700" data-testid="connection-detail-submitter">
              {record.owner_display_name}
            </span>
            <span className="text-slate-400">on</span>
            <span className="text-slate-600" data-testid="connection-detail-submission-date">
              {formatDetailedDate(record.submission_date)}
            </span>
          </div>
        </div>
      </header>

      {/* Field grid: all nine business fields */}
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
        <dl className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <FieldRow label="LinkedIn" testId="connection-detail-field-linkedin">
            <a
              href={record.linkedin_url}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 text-brand-600 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-500"
              data-testid="connection-detail-linkedin-link"
            >
              <span className="break-all">{record.linkedin_url}</span>
              <ExternalLink aria-hidden="true" className="h-4 w-4 flex-shrink-0" />
              <span className="sr-only"> (opens in a new tab)</span>
            </a>
          </FieldRow>

          <FieldRow label="Company" testId="connection-detail-field-company">
            <span className="text-slate-900">{record.company}</span>
          </FieldRow>

          <FieldRow label="Job title" testId="connection-detail-field-job-title">
            <span className="text-slate-900">{record.job_title}</span>
          </FieldRow>

          <FieldRow label="Tags" testId="connection-detail-field-tags">
            {record.tags.length === 0 ? (
              <span className="text-slate-400">&mdash;</span>
            ) : (
              <div className="flex flex-wrap gap-1.5">
                {record.tags.map((tag) => (
                  <Badge
                    key={tag.id}
                    variant="neutral"
                    size="sm"
                    withBorder
                    data-testid={`connection-detail-tag-${tag.id}`}
                  >
                    {tag.name}
                  </Badge>
                ))}
              </div>
            )}
          </FieldRow>

          <FieldRow
            label="Relationship context"
            testId="connection-detail-field-relationship-context"
            wide
          >
            <p className="whitespace-pre-line text-slate-900">{record.relationship_context}</p>
          </FieldRow>

          <FieldRow label="AI notes" testId="connection-detail-field-ai-notes" wide>
            {record.ai_notes ? (
              <p className="whitespace-pre-line rounded-md border border-slate-100 bg-slate-50 p-3 text-slate-900">
                {record.ai_notes}
              </p>
            ) : (
              <p className="italic text-slate-400">
                No AI notes were generated for this connection.
              </p>
            )}
          </FieldRow>

          {isSoftDeleted && (
            <FieldRow label="Deleted at" testId="connection-detail-field-deleted-at" wide>
              <span className="text-slate-700">{formatDetailedDate(record.deleted_at)}</span>
            </FieldRow>
          )}
        </dl>
      </section>

      {/* Edit history feed (F-011 + F-013) */}
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

      {/* Soft-delete confirmation modal */}
      <SoftDeleteModal
        record={record}
        isOpen={isDeleteOpen}
        isPending={softDeleteMutation.isPending}
        onClose={handleCloseDelete}
        onConfirm={handleConfirmDelete}
      />
    </main>
  );
}
