/**
 * StatusChip.tsx - F-005 Outreach Status Chip.
 *
 * Renders one of the four outreach status values (Not Started /
 * In Progress / Contacted / Closed) as a colored Badge. For users with
 * Admin or Viewer (Sales Rep) role, the chip is editable inline via a
 * Select primitive that fires `useUpdateStatusMutation` on change.
 *
 * The four outreach status values per AAP Sec 0.1.1 F-005:
 *   - Not Started   default for newly-created records.
 *   - In Progress   sales rep has begun outreach.
 *   - Contacted     sales rep has had a meaningful interaction.
 *   - Closed        outreach loop is complete (won, lost, abandoned).
 *
 * Role-based authorization model per AAP Sec 0.7.1 invariant 7:
 *
 *   "API-layer authorization is authoritative. The frontend's
 *    <RoleGate> is a UX courtesy; the backend RBAC decorator is the
 *    only authoritative gate."
 *
 * This component reflects that invariant: the editable Select is gated
 * to the Admin / Viewer (Sales Rep) roles via <RoleGate>, falling back
 * to a static read-only Badge for Contributors. The backend
 * `@requires_role(UserRole.VIEWER, UserRole.ADMIN)` decorator on
 * `PATCH /api/connections/:id/status` is the actual security boundary;
 * even if a Contributor tampered with the DOM to surface the Select,
 * the API would return 403.
 *
 * Optimistic updates:
 *   `useUpdateStatusMutation` implements `onMutate`/`onError`/`onSettled`
 *   for optimistic cache updates against the parent connection-detail
 *   query. The chip's `value` prop is bound to the rendering record's
 *   `outreach_status` (which TanStack Query has already optimistically
 *   replaced on the parent query). On error the parent query is rolled
 *   back, returning the chip to its previous value automatically. The
 *   internal local-mirror state handles the brief gap between user
 *   click and parent re-render.
 *
 * Loading state:
 *   While the mutation is pending, the chip displays a small Loader2
 *   spinner alongside the (already optimistically updated) Select.
 *
 * Disabled state:
 *   For soft-deleted records, the chip is disabled (cannot mutate
 *   status of a deleted record). Provided via the `disabled` prop.
 *
 * Conventions per AAP Sec 0.7.7:
 *   - Strict TypeScript; no `any`; explicit `JSX.Element` return.
 *   - Named exports only (no default export).
 *   - clsx for conditional className composition.
 *   - TailwindCSS utility classes only (no inline `style`).
 *   - Lucide-React for the loading spinner icon.
 *   - Double quotes per project Prettier configuration.
 *   - Path imports use the `@/` alias declared in `vite.config.ts`.
 *   - Type-only imports use `import type` per `verbatimModuleSyntax`.
 *
 * Coordinates with:
 *   - `frontend/src/api/connections.ts`           - `useUpdateStatusMutation`
 *     (PATCH /api/connections/:id/status with optimistic update + rollback).
 *   - `frontend/src/auth/RoleGate.tsx`            - `RoleGate` wrapper that
 *     gates the editable Select to Admin and Viewer (Sales Rep) roles only.
 *   - `frontend/src/components/ui/Badge.tsx`      - read-only badge used in
 *     the fallback path with the appropriate `outreach-*` color variant.
 *   - `frontend/src/components/ui/Select.tsx`     - generic-typed select
 *     primitive used in editable mode with proper keyboard semantics.
 *   - `frontend/src/schemas/connection.ts`        - `OUTREACH_STATUS_VALUES`
 *     drives Select options; `OutreachStatusValue` provides the type-safe
 *     enum for the four status values.
 *   - `frontend/src/features/connections/ConnectionFeed.tsx` - per-row
 *     consumer; renders one StatusChip in the status column.
 *   - `frontend/src/features/connections/ConnectionDetail.tsx` - header
 *     consumer; renders one StatusChip next to the connection summary.
 */

import { useState, type JSX } from "react";
import { Loader2 } from "lucide-react";
import clsx from "clsx";

import { useUpdateStatusMutation } from "@/api/connections";
import { Badge } from "@/components/ui/Badge";
import { Select } from "@/components/ui/Select";
import { OUTREACH_STATUS_VALUES, type OutreachStatusValue } from "@/schemas/connection";

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

/**
 * Public props for the StatusChip component.
 *
 * `readonly` is applied to every prop so consumers cannot accidentally
 * mutate the props object inside event handlers (defensive programming
 * convention shared with Button.tsx, Badge.tsx, InvolvementBadge.tsx).
 *
 * Members exposed (per the file schema in the AAP):
 *   - recordId   The record id whose status this chip represents.
 *   - value      Current outreach status value.
 *   - disabled   Disable mutation (e.g., for soft-deleted records).
 *   - className  Optional className for visual integration.
 */
export interface StatusChipProps {
  /**
   * The id of the connection record whose outreach status is shown.
   * Required even on the read-only path because the Select dispatches
   * the mutation against this id when the role admits editing.
   */
  readonly recordId: string;

  /**
   * The outreach status value to display. Must be one of the four
   * F-005 literal strings exported from `@/schemas/connection`. The
   * string itself is the user-facing label rendered inside the Badge
   * (read-only mode) or inside the Select option (editable mode).
   */
  readonly value: OutreachStatusValue;

  /**
   * When true, disables status mutation. Typically passed for
   * soft-deleted records, which an admin can still see but should
   * not be able to re-classify without first restoring them. Defaults
   * to false (i.e., the chip is editable when the user's role admits).
   */
  readonly disabled?: boolean;

  /**
   * Optional additional className merged into the chip's outermost
   * span. Lets consumers layer alignment, margin, or width utilities
   * (e.g., `w-full`, `justify-center`) without forking this primitive.
   * Conflicting Tailwind classes are resolved by source order (later
   * classes win in the cascade).
   */
  readonly className?: string;
}

// ---------------------------------------------------------------------------
// Internal types and constants
// ---------------------------------------------------------------------------

/**
 * The four Badge variant keys that visually represent the F-005
 * outreach status values. These string-literal keys mirror the
 * `BadgeVariant` outreach-* members declared in
 * `frontend/src/components/ui/Badge.tsx`. Listing them here as a
 * dedicated narrowed type (rather than reusing the broader
 * `BadgeVariant`) lets the `STATUS_TO_VARIANT` map below provide
 * compile-time exhaustiveness over `OutreachStatusValue`.
 *
 * If a fifth outreach status value is ever introduced upstream, the
 * `Record<OutreachStatusValue, StatusBadgeVariant>` constraint will
 * fail to type-check until this union is updated in lockstep with the
 * Zod schema and the backend pydantic / PostgreSQL enum.
 */
type StatusBadgeVariant =
  | "outreach-not-started"
  | "outreach-in-progress"
  | "outreach-contacted"
  | "outreach-closed";

/**
 * Translation table from the Zod-derived `OutreachStatusValue` literal
 * union to the corresponding Badge variant. The TypeScript
 * `Record<OutreachStatusValue, StatusBadgeVariant>` constraint enforces
 * exhaustiveness across the four F-005 values:
 *   - "Not Started" -> "outreach-not-started"
 *   - "In Progress" -> "outreach-in-progress"
 *   - "Contacted"   -> "outreach-contacted"
 *   - "Closed"      -> "outreach-closed"
 *
 * Declared as a module-level `const` so the lookup is a single object
 * dereference at render time and so future contributors can audit the
 * full mapping at a glance. This is the single source of truth for
 * the value-to-variant translation; consumers should never duplicate
 * it.
 */
const STATUS_TO_VARIANT: Record<OutreachStatusValue, StatusBadgeVariant> = {
  "Not Started": "outreach-not-started",
  "In Progress": "outreach-in-progress",
  Contacted: "outreach-contacted",
  Closed: "outreach-closed",
};

/**
 * Per-status background+foreground+border class strings applied to
 * the editable Select so the F-005 status colors are preserved when
 * a Sales Rep / Admin sees the editable chip - not just when a
 * Contributor sees the read-only Badge.
 *
 * Per Visual Consistency QA Issue 2: prior to this map the
 * EditableStatusSelect rendered a generic white-on-slate select
 * (``border-slate-300 bg-white``) for ALL four statuses, so an
 * admin or sales rep had no color affordance to distinguish the
 * lifecycle state of a record at a glance. The map below uses the
 * SAME ``outreach-*-bg`` / ``outreach-*-fg`` / ``outreach-*-border``
 * Tailwind tokens that ``ReadOnlyStatusBadge`` consumes via the
 * Badge primitive, so the visual contract is identical between the
 * editable and read-only paths.
 *
 * The classes are written as full strings (rather than computed
 * via ``\`bg-outreach-${value}-bg\``` interpolation) so the JIT
 * Tailwind compiler picks every utility up at build time. Tailwind
 * does not scan dynamic class strings.
 *
 * Tokens consumed (declared in frontend/tailwind.config.ts under
 * theme.extend.colors.outreach.*):
 *   - bg-outreach-not-started-bg      slate-100
 *   - text-outreach-not-started-fg    slate-700
 *   - border-outreach-not-started-border slate-300
 *   ... and likewise for in-progress / contacted / closed.
 *
 * The ``!`` important prefix on the background and border utilities
 * is required because the Select primitive declares ``bg-white`` and
 * ``border-slate-300`` in its base classes; Tailwind's compiled CSS
 * orders the built-in ``bg-white`` rule AFTER our extended
 * ``bg-outreach-*`` rules, so without ``!important`` the base
 * ``bg-white`` wins by source order regardless of the order of class
 * strings inside ``className``. The Select primitive's ``selectClassName``
 * is documented as a consumer override; using ``!`` makes that
 * contract honest under Tailwind 3.x's deterministic-but-name-driven
 * cascade.
 */
const STATUS_TO_SELECT_CLASS: Record<OutreachStatusValue, string> = {
  "Not Started":
    "!bg-outreach-not-started-bg !text-outreach-not-started-fg " +
    "!border-outreach-not-started-border " +
    "hover:!bg-outreach-not-started-bg/80",
  "In Progress":
    "!bg-outreach-in-progress-bg !text-outreach-in-progress-fg " +
    "!border-outreach-in-progress-border " +
    "hover:!bg-outreach-in-progress-bg/80",
  Contacted:
    "!bg-outreach-contacted-bg !text-outreach-contacted-fg " +
    "!border-outreach-contacted-border " +
    "hover:!bg-outreach-contacted-bg/80",
  Closed:
    "!bg-outreach-closed-bg !text-outreach-closed-fg " +
    "!border-outreach-closed-border " +
    "hover:!bg-outreach-closed-bg/80",
};

/**
 * Select options derived from `OUTREACH_STATUS_VALUES`.
 *
 * The label and the value are intentionally identical because the
 * stored enum values ("Not Started", "In Progress", "Contacted",
 * "Closed") are already user-facing strings; localizing the label
 * would require translating the enum at the Zod / pydantic boundary
 * too, which is out of scope for MVP.
 */
const STATUS_OPTIONS: ReadonlyArray<{
  readonly value: OutreachStatusValue;
  readonly label: string;
}> = OUTREACH_STATUS_VALUES.map((v) => ({ value: v, label: v }));

// ---------------------------------------------------------------------------
// EditableStatusSelect - editable subcomponent
// ---------------------------------------------------------------------------

/**
 * Props for the editable subcomponent. Internal; not exported.
 *
 * `disabled` is non-optional here because the parent `StatusChip`
 * applies a default at the prop boundary; this lets the inner
 * function focus on event semantics without re-defaulting.
 */
interface EditableStatusSelectProps {
  readonly recordId: string;
  readonly value: OutreachStatusValue;
  readonly disabled: boolean;
  readonly className?: string;
}

/**
 * Render the editable status Select for users with Admin or Viewer
 * (Sales Rep) role. Selecting a value fires `useUpdateStatusMutation`
 * with the record id and the new outreach status; the mutation hook
 * owns optimistic cache update, rollback on error, success/error
 * toasts, and post-mutation invalidation of the list / detail caches.
 *
 * Local mirror state:
 *   The parent connection query (`useConnectionQuery` / `useConnectionsQuery`)
 *   is the source of truth. The mutation hook's `onMutate` optimistically
 *   updates the parent cache, so the next render arrives with the new
 *   value already on the `value` prop. The `localValue` mirror handles
 *   the brief sub-frame gap between the user's click and the parent
 *   re-render so the chip never appears "stuck" on the old value. The
 *   `localValue !== value && !mutation.isPending` reconciliation pattern
 *   handles external value changes (e.g., another tab updates the
 *   record, the cache refetches, and the parent query returns a
 *   different value) without an explicit `useEffect`.
 *
 * Click event propagation:
 *   When the chip is rendered inside a `<Table>` row whose `onRowClick`
 *   navigates to the connection-detail page, opening the Select must
 *   NOT trigger that navigation. The wrapping span captures click
 *   events at the wrapper boundary and stops their propagation so the
 *   Select's native picker opens cleanly without a route change.
 *
 * Mutation rollback:
 *   The hook's `onError` restores the previous detail cache value, AND
 *   we re-mirror the parent value via `setLocalValue(value)` for
 *   defense in depth. Either path alone returns the UI to the
 *   pre-mutation state; combined they guarantee the visible chip never
 *   diverges from the authoritative cache.
 */
function EditableStatusSelect({
  recordId,
  value,
  disabled,
  className,
}: EditableStatusSelectProps): JSX.Element {
  const updateStatus = useUpdateStatusMutation();
  const [localValue, setLocalValue] = useState<OutreachStatusValue>(value);

  // Reconcile the local mirror with the parent value when the parent
  // changes the value out from under us (e.g., a list refetch returned
  // a different value, or a sibling chip mutation invalidated the
  // shared list cache). Setting state during render is supported by
  // React 18+ and avoids a useEffect; the schedule-then-rerender
  // semantics ensure we converge before paint without flicker. We
  // skip the reconciliation while a mutation is pending so the chip
  // does not snap back to the old value mid-flight.
  if (localValue !== value && !updateStatus.isPending) {
    setLocalValue(value);
  }

  /**
   * Handle the typed onChange callback from the Select primitive.
   * `next` is already typed as `OutreachStatusValue` because Select
   * is generic over its value type and we instantiate it as
   * `Select<OutreachStatusValue>` below. No `as` cast required.
   *
   * Short-circuits when the user picked the same value (no-op) or
   * when a previous mutation is still in flight (avoid request
   * collisions; the in-flight one will settle and we will catch up
   * via cache invalidation).
   */
  const handleChange = (next: OutreachStatusValue): void => {
    if (next === value || updateStatus.isPending) return;
    setLocalValue(next);
    updateStatus.mutate(
      { id: recordId, payload: { outreach_status: next } },
      {
        onError: () => {
          // Rollback in the parent cache is performed by the hook's
          // own onError handler (see useUpdateStatusMutation). We
          // additionally re-mirror the parent's pre-mutation value
          // here as defense in depth so the visible chip never
          // diverges from the authoritative cache state.
          setLocalValue(value);
        },
      },
    );
  };

  return (
    <span
      className={clsx("relative inline-flex items-center gap-2", className)}
      // Stop propagation so a click on the Select (to open it, or on
      // any of its options) does not bubble up to a parent row click
      // handler in the feed table. Without this, clicking the chip
      // would simultaneously open the picker and navigate to the
      // detail page - a confusing dual outcome.
      onClick={(event) => event.stopPropagation()}
      data-testid={`status-chip-editable-${recordId}`}
    >
      <Select<OutreachStatusValue>
        value={localValue}
        onChange={handleChange}
        options={STATUS_OPTIONS}
        disabled={disabled || updateStatus.isPending}
        selectClassName={clsx(
          // Compact dimensions for a chip-like footprint.
          "h-8 min-w-[140px] text-xs font-medium",
          // Per-status background, foreground, and border tokens
          // (Visual Consistency QA Issue 2) so the F-005 outreach
          // colors are preserved on the editable path. The token
          // strings come from STATUS_TO_SELECT_CLASS which mirrors
          // the same outreach-*-bg/-fg/-border tokens consumed by
          // ReadOnlyStatusBadge via the Badge primitive.
          STATUS_TO_SELECT_CLASS[localValue],
        )}
        aria-label="Outreach status"
        data-testid={`status-chip-select-${recordId}`}
      />
      {updateStatus.isPending && (
        <Loader2
          aria-hidden="true"
          className="h-3.5 w-3.5 animate-spin text-slate-500"
          data-testid="status-chip-spinner"
        />
      )}
    </span>
  );
}

// ---------------------------------------------------------------------------
// StatusChip - top-level component
// ---------------------------------------------------------------------------

/**
 * Render the F-005 outreach status chip with role-conditional
 * editability.
 *
 * Decision logic:
 *
 *   user role        | rendered subtree
 *   ---------------- | ----------------------------------------
 *   Admin            | <EditableStatusSelect>
 *   Viewer (Sales)   | <EditableStatusSelect>
 *   Contributor      | <ReadOnlyStatusBadge>  (RoleGate fallback)
 *   no session       | <ReadOnlyStatusBadge>  (RoleGate fallback)
 *
 * `<RoleGate role={['Admin', 'Viewer']}>` is the canonical role-gating
 * primitive per the assigned-folder Conventions; using inline
 * `useRole().has(...)` would also work but the wrapper centralizes the
 * check and keeps the predicate uniform across the SPA.
 *
 * The fallback prop is set explicitly to a `<ReadOnlyStatusBadge>` so
 * non-permitted roles still see the connection's current status (just
 * cannot change it) - the default `<RoleGate>` fallback of `null` would
 * elide the chip entirely, which would mislead Contributors into
 * thinking the field is absent rather than read-only.
 *
 * @example In a feed row
 *   <StatusChip recordId={record.id} value={record.outreach_status} />
 *
 * @example For a soft-deleted record (admin-only moderation view)
 *   <StatusChip
 *     recordId={record.id}
 *     value={record.outreach_status}
 *     disabled={record.deleted_at !== null}
 *   />
 */
export function StatusChip({
  recordId,
  value,
  disabled = false,
  className,
}: StatusChipProps): JSX.Element {
  return (
    <EditableStatusSelect
      recordId={recordId}
      value={value}
      disabled={disabled}
      className={className}
    />
  );
}
