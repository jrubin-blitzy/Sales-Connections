/**
 * EditHistoryFeed.tsx - F-011 + F-013 Edit History Feed.
 *
 * Renders the audit trail for a single connection record. Sourced from
 * GET /api/connections/:id/history via useConnectionHistoryQuery. Each
 * audit event is rendered as a timeline-style row with:
 *   - An event-type icon (color-coded per event type).
 *   - A human-readable summary line (actor + verb + target).
 *   - A relative timestamp (e.g., "3 minutes ago").
 *   - For status_change events, a before-after delta visualization.
 *
 * The eight event types from AAP Sec 0.4.7 audit_events.event_type:
 *   - create         - record was created
 *   - status_change  - outreach status mutated (shows before/after)
 *   - edit           - non-status field edited
 *   - soft_delete    - record soft-deleted
 *   - hard_delete    - record permanently removed (admin only)
 *   - role_change    - actor's role changed (rare on a record's feed but
 *                      possible if context links to a user role change)
 *   - authentication - login/logout (typically NOT scoped to a record;
 *                      defensive rendering only)
 *   - admin_op       - generic admin operation
 *
 * Per AAP Sec 0.7.4, the audit trail is append-only and database-level
 * grants prevent UPDATE/DELETE from the application role. The frontend
 * simply renders what is there; no edit/delete affordances.
 *
 * Pagination:
 *   The history hook returns PaginatedHistory { items, total, page,
 *   page_size }. The feed renders 25 items per page by default with
 *   Previous/Next pagination matching the feed pattern.
 *
 * Accessibility:
 *   - <ol> ordered list (audit trail has temporal order).
 *   - role="status" on loading; role="alert" on error.
 *   - Each event has an aria-label summarizing the event for screen
 *     readers.
 *
 * Conventions per AAP Sec 0.7.7 and frontend/.prettierrc.json:
 *   - Strict TypeScript; no `any`; explicit `JSX.Element` return type.
 *   - Named exports only (no default export).
 *   - clsx for conditional className composition.
 *   - TailwindCSS utility classes only (no inline `style`).
 *   - Lucide-React for per-event-type icons.
 *   - Double quotes per project Prettier configuration
 *     (singleQuote: false, trailingComma: "all", printWidth: 100).
 *   - Path imports use the `@/` alias declared in `vite.config.ts`
 *     and mirrored in `tsconfig.json`.
 *   - Type-only imports use the inline `type` modifier per
 *     `verbatimModuleSyntax: true` in `tsconfig.json`.
 *
 * Coordinates with:
 *   - frontend/src/api/connections.ts - useConnectionHistoryQuery hook
 *     fetching paginated audit history for the target record.
 *   - frontend/src/components/ui/Badge.tsx - Tailwind-styled badge
 *     primitive used for the per-event-type label and the before/after
 *     status pills inside status_change rows.
 *   - frontend/src/components/ui/Button.tsx - Tailwind-styled button
 *     primitive used by the Previous/Next pagination controls beneath
 *     the history timeline.
 *   - frontend/src/schemas/connection.ts - source of the
 *     ConnectionHistoryEntry type (per-row payload) and the
 *     AuditEventType literal-union (drives EVENT_TYPE_META and the
 *     exhaustive event-type branching in HistoryEventRow).
 *   - frontend/src/features/connections/ConnectionDetail.tsx - the
 *     consumer; embeds this feed in the history section beneath the
 *     primary record fields.
 */

import { useMemo, useState, type JSX, type ReactNode } from "react";
import { Clock, Crown, LogIn, Pencil, Plus, Shield, Trash2, type LucideIcon } from "lucide-react";
import clsx from "clsx";

import { useConnectionHistoryQuery } from "@/api/connections";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import type { AuditEventType, ConnectionHistoryEntry } from "@/schemas/connection";

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

/**
 * Public props for the EditHistoryFeed component.
 *
 * `readonly` is applied to every prop so consumers cannot accidentally
 * mutate the props object inside event handlers; this convention is
 * shared with the UI primitives (Button, Badge) and with sibling
 * feature components (InvolvementBadge, DuplicateWarning, TagInput).
 *
 * Members exposed (per the file schema in the AAP):
 *   - recordId   The record id whose audit history is to be displayed.
 *   - pageSize   Items per page; defaults to 25.
 */
export interface EditHistoryFeedProps {
  /** The record id whose audit history is to be displayed. */
  readonly recordId: string;

  /**
   * Items per page; defaults to 25 to match the connection feed
   * convention. Most records will have a handful of events, so
   * pagination is largely a defensive overflow handler.
   */
  readonly pageSize?: number;
}

// ---------------------------------------------------------------------------
// Event type metadata
// ---------------------------------------------------------------------------

/**
 * Tone palette names. Used to drive both the Badge variant for the
 * event label and the Tailwind utility classes on the avatar circle
 * surrounding the icon. Centralizing this to a small literal-union
 * keeps the visual palette consistent across rows.
 */
type EventTone = "success" | "brand" | "neutral" | "warning" | "danger" | "info";

/**
 * Visual metadata per audit event type. Defines the icon, color tone,
 * and human-readable label. Used by the row renderer.
 */
interface EventTypeMeta {
  readonly icon: LucideIcon;
  readonly label: string;
  readonly tone: EventTone;
}

/**
 * Translation table from the Zod-derived `AuditEventType` literal
 * union to the corresponding visual metadata. The TypeScript
 * `Record<AuditEventType, EventTypeMeta>` constraint enforces
 * exhaustiveness across the eight F-013 audit event types:
 *
 *   - create         - Plus icon, success tone
 *   - status_change  - Clock icon, info tone
 *   - edit           - Pencil icon, brand tone
 *   - soft_delete    - Trash2 icon, warning tone
 *   - hard_delete    - Trash2 icon, danger tone
 *   - role_change    - Crown icon, brand tone
 *   - authentication - LogIn icon, neutral tone
 *   - admin_op       - Shield icon, neutral tone
 *
 * Declared as a module-level `const` so the lookup is a single object
 * dereference at render time and so future contributors can audit the
 * full mapping at a glance. If a ninth audit event value is ever
 * introduced upstream, the Record constraint will fail to type-check
 * until this map is updated in lockstep with the Zod schema and the
 * backend pydantic / PostgreSQL enum.
 */
const EVENT_TYPE_META: Record<AuditEventType, EventTypeMeta> = {
  create: { icon: Plus, label: "Created", tone: "success" },
  status_change: { icon: Clock, label: "Status changed", tone: "info" },
  edit: { icon: Pencil, label: "Edited", tone: "brand" },
  soft_delete: { icon: Trash2, label: "Deleted", tone: "warning" },
  hard_delete: { icon: Trash2, label: "Permanently deleted", tone: "danger" },
  role_change: { icon: Crown, label: "Role changed", tone: "brand" },
  authentication: { icon: LogIn, label: "Authentication", tone: "neutral" },
  admin_op: { icon: Shield, label: "Admin operation", tone: "neutral" },
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Compute a coarse relative time string (e.g., "3 minutes ago", "2 days
 * ago"). Falls back to an absolute date string for events older than
 * 30 days so the user does not have to mentally compute "147 days ago".
 *
 * Locale-agnostic enough for MVP. A future enhancement could switch to
 * `Intl.RelativeTimeFormat` for full i18n.
 *
 * @param date  The Date to format; assumed to be a valid Date (callers
 *              must guard against NaN-time inputs upstream).
 * @returns     A short human-readable relative or absolute string.
 */
function relativeTime(date: Date): string {
  const now = Date.now();
  const diffMs = now - date.getTime();
  if (diffMs < 0) {
    // Future timestamps (clock drift) render as "just now" rather
    // than a misleading "in N seconds" - the audit trail should
    // never describe events that have not happened yet.
    return "just now";
  }
  const seconds = Math.floor(diffMs / 1000);
  if (seconds < 60) {
    return seconds <= 5 ? "just now" : `${seconds} seconds ago`;
  }
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) {
    return minutes === 1 ? "1 minute ago" : `${minutes} minutes ago`;
  }
  const hours = Math.floor(minutes / 60);
  if (hours < 24) {
    return hours === 1 ? "1 hour ago" : `${hours} hours ago`;
  }
  const days = Math.floor(hours / 24);
  if (days < 30) {
    return days === 1 ? "1 day ago" : `${days} days ago`;
  }
  // For events older than 30 days, render an absolute short date
  // (e.g., "Apr 23, 2026") so the user does not need to do mental
  // arithmetic. Locale defaults to the user agent.
  return date.toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

/**
 * Format an ISO 8601 datetime as both an absolute timestamp and a
 * relative ("X minutes ago") string. Returns both for tooltip + visible
 * display: the visible text shows the relative form; the `title`
 * attribute and `dateTime` attribute on the surrounding <time> element
 * preserve the absolute form for screen readers, browsers, and tools
 * (e.g., copy-paste into a calendar).
 *
 * Defensive against:
 *   - null / undefined input (handled by the caller via `?? iso`).
 *   - Invalid date strings - returns the original ISO string for both
 *     fields so the row still renders something rather than crashing.
 *   - Exceptions from toLocaleString in unusual environments - falls
 *     through with the original ISO string.
 */
function formatTimestamp(iso: string): { absolute: string; relative: string } {
  let absolute = iso;
  let relative = iso;
  try {
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) {
      return { absolute: iso, relative: iso };
    }
    absolute = date.toLocaleString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
      second: "2-digit",
    });
    relative = relativeTime(date);
  } catch {
    // Fall through with the original ISO strings.
  }
  return { absolute, relative };
}

/**
 * Read a string field from a JSONB payload defensively. Returns null
 * if the key is absent or the value is not a string.
 *
 * The audit `before_payload` and `after_payload` are JSONB columns
 * whose schema in `frontend/src/schemas/connection.ts` is
 * `z.record(z.string(), z.unknown()).nullable()` - the contents are
 * not type-narrowed at compile time, so the only sound way to read
 * a value is to check its runtime type.
 */
function readStringField(
  payload: Record<string, unknown> | null | undefined,
  key: string,
): string | null {
  if (!payload) {
    return null;
  }
  const value = payload[key];
  return typeof value === "string" ? value : null;
}

/**
 * Tone-to-Badge-variant mapping. The Badge primitive declares the
 * generic palette variants `neutral | brand | success | warning |
 * danger | info` directly, so this is effectively an identity for the
 * tones we use here. Wrapping it in a function makes the visual-token
 * mapping explicit and provides a single point to evolve if the tone
 * vocabulary ever diverges from the Badge variant set.
 */
function toneToBadgeVariant(tone: EventTone): EventTone {
  return tone;
}

/**
 * Tone-to-Tailwind-classes mapping for the avatar circle behind each
 * event icon. Returns a coordinated background + foreground class pair
 * so the icon reads against its tinted backdrop at every tone.
 */
function toneToIconClasses(tone: EventTone): string {
  switch (tone) {
    case "success":
      return "bg-emerald-100 text-emerald-700";
    case "brand":
      return "bg-brand-100 text-brand-700";
    case "neutral":
      return "bg-slate-100 text-slate-700";
    case "warning":
      return "bg-amber-100 text-amber-700";
    case "danger":
      return "bg-red-100 text-red-700";
    case "info":
      return "bg-sky-100 text-sky-700";
    default:
      return "bg-slate-100 text-slate-700";
  }
}

// ---------------------------------------------------------------------------
// HistoryEventRow - private subcomponent
// ---------------------------------------------------------------------------

/**
 * Props for the internal HistoryEventRow renderer.
 */
interface HistoryEventRowProps {
  readonly entry: ConnectionHistoryEntry;
}

/**
 * Render one audit event as a single timeline row. Different event
 * types get different summary lines, encoded as ReactNode fragments so
 * inline Badge primitives (for the before/after status pills) compose
 * naturally with the surrounding text.
 *
 * Summary copy per event type:
 *   - create         -> "{actor} created this connection."
 *   - status_change  -> "{actor} changed status from <X> to <Y>."
 *   - edit           -> "{actor} edited this connection."
 *   - soft_delete    -> "{actor} deleted this connection."
 *   - hard_delete    -> "{actor} permanently deleted this connection."
 *   - role_change    -> "{actor}'s role changed."
 *   - authentication -> "{actor} authenticated."
 *   - admin_op       -> "{actor} performed an admin operation."
 *
 * The icon avatar is `aria-hidden="true"` so screen readers do not
 * announce the decorative SVG. The `aria-label` on the surrounding
 * <li> conveys the event type and actor in plain language so screen
 * reader users get the same information as sighted users without the
 * visual scanning.
 *
 * The component is NOT exported because it is tightly coupled to this
 * feed's layout (avatar circle on the left, summary column on the
 * right). Reusing audit-event rendering elsewhere would re-create the
 * row component in that consumer.
 */
function HistoryEventRow({ entry }: HistoryEventRowProps): JSX.Element {
  // Defensive lookup with admin_op fallback in case a future backend
  // event type is rendered before the frontend EVENT_TYPE_META map is
  // updated (forward compatibility per the soft-coupling philosophy).
  const meta: EventTypeMeta = EVENT_TYPE_META[entry.event_type] ?? EVENT_TYPE_META.admin_op;
  const Icon = meta.icon;
  const { absolute, relative } = formatTimestamp(entry.event_timestamp);
  // Falls back to "Someone" when actor_display_name is null - the
  // server populates this from the joined users.display_name, but
  // for system-generated events the join may yield null (e.g., a
  // record created by a deleted user account).
  const actor: string = entry.actor_display_name ?? "Someone";

  // Build the summary line per event type. Each branch composes the
  // actor name (bold) with the verb phrase plus, where applicable,
  // inline Badge pills for the before/after status delta.
  let summary: ReactNode;
  if (entry.event_type === "status_change") {
    const before = readStringField(entry.before_payload, "outreach_status") ?? "—";
    const after = readStringField(entry.after_payload, "outreach_status") ?? "—";
    summary = (
      <>
        <span className="font-medium text-slate-900">{actor}</span> changed status from{" "}
        <Badge variant="neutral" size="sm">
          {before}
        </Badge>{" "}
        to{" "}
        <Badge variant="info" size="sm">
          {after}
        </Badge>
        .
      </>
    );
  } else if (entry.event_type === "create") {
    summary = (
      <>
        <span className="font-medium text-slate-900">{actor}</span> created this connection.
      </>
    );
  } else if (entry.event_type === "edit") {
    summary = (
      <>
        <span className="font-medium text-slate-900">{actor}</span> edited this connection.
      </>
    );
  } else if (entry.event_type === "soft_delete") {
    summary = (
      <>
        <span className="font-medium text-slate-900">{actor}</span> deleted this connection.
      </>
    );
  } else if (entry.event_type === "hard_delete") {
    summary = (
      <>
        <span className="font-medium text-slate-900">{actor}</span> permanently deleted this
        connection.
      </>
    );
  } else if (entry.event_type === "role_change") {
    summary = (
      <>
        <span className="font-medium text-slate-900">{actor}</span>
        {"'s role changed."}
      </>
    );
  } else if (entry.event_type === "authentication") {
    summary = (
      <>
        <span className="font-medium text-slate-900">{actor}</span> authenticated.
      </>
    );
  } else {
    // admin_op (and any future audit event type that drifts ahead of
    // this frontend before the EVENT_TYPE_META map is updated).
    summary = (
      <>
        <span className="font-medium text-slate-900">{actor}</span> performed an admin operation.
      </>
    );
  }

  return (
    <li
      className="flex gap-3"
      aria-label={`${meta.label} by ${actor}, ${relative}`}
      data-testid={`history-event-${entry.id}`}
    >
      <span
        aria-hidden="true"
        className={clsx(
          "flex h-8 w-8 shrink-0 items-center justify-center rounded-full",
          toneToIconClasses(meta.tone),
        )}
      >
        <Icon className="h-4 w-4" />
      </span>
      <div className="min-w-0 flex-1 pt-1">
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant={toneToBadgeVariant(meta.tone)} size="sm">
            {meta.label}
          </Badge>
          <p className="text-sm text-slate-700">{summary}</p>
        </div>
        <p className="mt-1 text-xs text-slate-500">
          <time dateTime={entry.event_timestamp} title={absolute}>
            {relative}
          </time>
        </p>
      </div>
    </li>
  );
}

// ---------------------------------------------------------------------------
// EditHistoryFeed - main component
// ---------------------------------------------------------------------------

/**
 * Render the edit-history feed for a single connection record (F-011
 * + F-013). Wraps useConnectionHistoryQuery with loading, error, and
 * empty states, then renders one `<HistoryEventRow>` per audit event
 * inside an ordered list. Includes Previous / Next pagination controls
 * beneath the timeline when more than one page of events exists.
 *
 * Pagination state is local to this component (via `useState`) rather
 * than encoded in the URL. Unlike the connection feed - which uses URL
 * state so a filtered view is shareable - the audit-history page state
 * is ephemeral: someone sharing a `/connections/:id` link does not
 * need to share which page of history they were viewing.
 *
 * @example In ConnectionDetail.tsx
 *   <section aria-label="Edit history">
 *     <h2 className="text-base font-semibold text-slate-800">History</h2>
 *     <EditHistoryFeed recordId={record.id} />
 *   </section>
 *
 * @example With custom page size
 *   <EditHistoryFeed recordId={record.id} pageSize={10} />
 */
export function EditHistoryFeed({
  recordId,
  pageSize: initialPageSize = 25,
}: EditHistoryFeedProps): JSX.Element {
  const [page, setPage] = useState<number>(1);
  const historyQuery = useConnectionHistoryQuery(recordId, page, initialPageSize);

  // Derive the total page count from the server-reported total and
  // page_size. Memoized on the inputs so React only recomputes when
  // the underlying values change. Math.max(1, ...) prevents a zero
  // result from degenerating the pagination UI when the server
  // returns total=0 (in which case the empty state below renders
  // and the pagination nav is suppressed by the totalPages > 1 gate).
  const totalPages = useMemo<number>(() => {
    const total = historyQuery.data?.total ?? 0;
    const size = historyQuery.data?.page_size ?? initialPageSize;
    if (size <= 0) {
      return 1;
    }
    return Math.max(1, Math.ceil(total / size));
  }, [historyQuery.data?.total, historyQuery.data?.page_size, initialPageSize]);

  // === Loading state =====================================================
  // role="status" + aria-live="polite" announces the loading state to
  // screen readers without interrupting whatever they are reading.
  if (historyQuery.isPending) {
    return (
      <div
        role="status"
        aria-busy="true"
        aria-live="polite"
        className="rounded-lg border border-slate-200 bg-white p-6 text-center text-sm text-slate-500"
        data-testid="history-loading"
      >
        Loading history...
      </div>
    );
  }

  // === Error state =======================================================
  // role="alert" forces an immediate screen reader announcement;
  // appropriate for a hard error that blocks rendering of the feed.
  if (historyQuery.isError) {
    return (
      <div
        role="alert"
        className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700"
        data-testid="history-error"
      >
        Failed to load history: {historyQuery.error.message}
      </div>
    );
  }

  const items = historyQuery.data.items;

  // === Empty state =======================================================
  // role="status" (not alert) because an empty audit trail is a normal
  // condition for a freshly created record - no need to interrupt the
  // user with an assertive announcement.
  if (items.length === 0) {
    return (
      <div
        role="status"
        className="rounded-lg border border-slate-200 bg-white p-6 text-center text-sm text-slate-500"
        data-testid="history-empty"
      >
        No history events yet.
      </div>
    );
  }

  // === Populated render ==================================================
  return (
    <div data-testid="edit-history-feed">
      <ol className="space-y-4 rounded-lg border border-slate-200 bg-white p-4 shadow-card sm:p-6">
        {items.map((entry) => (
          <HistoryEventRow key={entry.id} entry={entry} />
        ))}
      </ol>

      {totalPages > 1 && (
        <nav
          aria-label="History pagination"
          className="mt-3 flex items-center justify-between"
          data-testid="history-pagination"
        >
          <Button
            type="button"
            variant="secondary"
            size="sm"
            disabled={page <= 1}
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            data-testid="history-page-prev"
          >
            Previous
          </Button>
          <span className="text-xs text-slate-600" data-testid="history-page-indicator">
            Page {page} of {totalPages}
          </span>
          <Button
            type="button"
            variant="secondary"
            size="sm"
            disabled={page >= totalPages}
            onClick={() => setPage((p) => p + 1)}
            data-testid="history-page-next"
          >
            Next
          </Button>
        </nav>
      )}
    </div>
  );
}
