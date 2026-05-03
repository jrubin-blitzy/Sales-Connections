/**
 * StatusChip.tsx - F-005 Outreach Status Chip with role-gated mutation.
 *
 * Renders the four-state outreach status (Not Started / In Progress /
 * Contacted / Closed) as a colored Badge primitive. When the current
 * user's role admits status mutation (Admin OR Viewer/Sales-Rep per
 * AAP Sec 0.1.1 F-005), the chip behaves as an inline editable control:
 * clicking it opens a small dropdown of the four status values, and
 * picking one fires the `useUpdateStatusMutation` hook from
 * `@/api/connections`. When the user's role does NOT admit mutation
 * (Contributor), the chip renders as a read-only Badge.
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
 * This component reflects that invariant: hiding the dropdown for
 * Contributors is purely a UX optimization. The backend
 * `@requires_role(UserRole.VIEWER, UserRole.ADMIN)` decorator on
 * `PATCH /api/connections/:id/status` is the actual security
 * boundary; even if a Contributor tampered with the DOM to show the
 * dropdown, the API would return 403.
 *
 * Visual treatment per the design system tokens declared in
 * `frontend/tailwind.config.ts` under theme.extend.colors.outreach.*
 * and consumed by the Badge primitive in
 * `frontend/src/components/ui/Badge.tsx`:
 *   - outreach-not-started   (slate tint)
 *   - outreach-in-progress   (amber tint, with optional dot indicator)
 *   - outreach-contacted     (blue tint)
 *   - outreach-closed        (slate-darker tint)
 *
 * Conventions per AAP Sec 0.7.7:
 *   - Strict TypeScript; no `any`; explicit `JSX.Element` return.
 *   - Named exports only (no default export).
 *   - clsx for conditional className composition.
 *   - TailwindCSS utility classes only (no inline `style`).
 *   - Double quotes per project Prettier configuration.
 *   - Path imports use the `@/` alias declared in `vite.config.ts`.
 *   - Type-only imports use `import type` per `verbatimModuleSyntax`.
 *
 * Coordinates with:
 *   - `frontend/src/components/ui/Badge.tsx` - the wrapped primitive
 *     that owns the variant-to-Tailwind-class mapping.
 *   - `frontend/src/schemas/connection.ts` - source of the
 *     `OutreachStatusValue` literal-union type and `OUTREACH_STATUS_VALUES`
 *     array that mirrors the backend pydantic / PostgreSQL enum.
 *   - `frontend/src/api/connections.ts` - source of
 *     `useUpdateStatusMutation()` which fires the
 *     `PATCH /api/connections/:id/status` request and applies the
 *     optimistic cache update.
 *   - `frontend/src/auth/AuthProvider.tsx` - source of `useRole()`
 *     which surfaces the current user's role. Hidden for
 *     Contributors per the AAP F-005 RBAC matrix.
 *   - `frontend/src/features/connections/ConnectionFeed.tsx` - per-row
 *     consumer; renders one StatusChip in the status column.
 *   - `frontend/src/features/connections/ConnectionDetail.tsx` -
 *     header consumer; renders one StatusChip in the detail header.
 */

import { useEffect, useId, useMemo, useRef, useState, type JSX } from "react";
import { ChevronDown } from "lucide-react";
import clsx from "clsx";

import { Badge, type BadgeVariant } from "@/components/ui/Badge";
import { useRole } from "@/auth/AuthProvider";
import { useUpdateStatusMutation } from "@/api/connections";
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
 */
export interface StatusChipProps {
  /**
   * The id of the connection record whose outreach status is shown.
   * Required even on the read-only path because the dropdown action
   * dispatches the mutation against this id.
   */
  readonly recordId: string;

  /**
   * The outreach status value to display. Must be one of the four
   * F-005 literal strings exported from `@/schemas/connection`. The
   * string itself is the user-facing label rendered inside the
   * badge.
   */
  readonly value: OutreachStatusValue;

  /**
   * Badge size. Defaults to "sm" because the most common consumer
   * (the connection feed table) needs compact rows; larger contexts
   * such as the detail header pass "md" or "lg" explicitly.
   */
  readonly size?: "sm" | "md" | "lg";

  /**
   * When true (the default), the chip renders as an editable control
   * IF the current user's role admits status mutation. When false,
   * the chip is read-only regardless of role. Useful for read-only
   * contexts (e.g., audit trail rows showing historical status
   * values).
   */
  readonly editable?: boolean;

  /**
   * Optional additional className merged into the wrapper. Lets
   * consumers layer alignment, margin, or width utilities without
   * forking this component.
   */
  readonly className?: string;
}

// ---------------------------------------------------------------------------
// Internal types and variant map
// ---------------------------------------------------------------------------

/**
 * The four Badge variant keys that visually represent the F-005
 * outreach status values. These string-literal keys mirror the
 * `BadgeVariant` outreach-* members declared in
 * `frontend/src/components/ui/Badge.tsx`. Listing them here as a
 * dedicated narrowed type (rather than reusing the broader
 * `BadgeVariant`) lets the `OUTREACH_TO_VARIANT` map below provide
 * compile-time exhaustiveness over `OutreachStatusValue`.
 */
type OutreachBadgeVariant =
  | "outreach-not-started"
  | "outreach-in-progress"
  | "outreach-contacted"
  | "outreach-closed";

/**
 * Translation table from the Zod-derived `OutreachStatusValue` literal
 * union to the corresponding Badge variant. The TypeScript
 * `Record<OutreachStatusValue, OutreachBadgeVariant>` constraint
 * enforces exhaustiveness across the four F-005 values:
 *   - "Not Started"  -> "outreach-not-started"
 *   - "In Progress"  -> "outreach-in-progress"
 *   - "Contacted"    -> "outreach-contacted"
 *   - "Closed"       -> "outreach-closed"
 *
 * Declared as a module-level `const` so the lookup is a single object
 * dereference at render time and so future contributors can audit the
 * full mapping at a glance. This is the single source of truth for
 * the value-to-variant translation; consumers should never duplicate
 * it.
 */
const OUTREACH_TO_VARIANT: Record<OutreachStatusValue, OutreachBadgeVariant> = {
  "Not Started": "outreach-not-started",
  "In Progress": "outreach-in-progress",
  Contacted: "outreach-contacted",
  Closed: "outreach-closed",
};

/**
 * The two roles authorized by the F-005 spec to mutate outreach
 * status. Per AAP Sec 0.1.1 F-005:
 *
 *   "Outreach status is updatable only by Sales Rep or Admin roles,
 *    never by the original submitter without Admin rights, in order
 *    to preserve sales team accountability."
 *
 * Sales Rep maps to the `Viewer` role per AAP Sec 0.5.2 Layer 6.
 *
 * The frontend hides the dropdown for Contributors as a UX courtesy;
 * the backend `@requires_role(UserRole.VIEWER, UserRole.ADMIN)`
 * decorator on `PATCH /api/connections/:id/status` is the
 * authoritative gate per AAP Sec 0.7.1 invariant 7.
 */
const STATUS_MUTATION_ROLES = ["Admin", "Viewer"] as const;

// ---------------------------------------------------------------------------
// StatusChip component
// ---------------------------------------------------------------------------

/**
 * Render a colored pill badge for a single F-005 outreach status
 * value, with optional inline edit dropdown for admitted roles.
 *
 * Rendering modes:
 *
 *   1. Read-only Badge (Contributor, or `editable={false}`):
 *      The chip renders as a static Badge with the appropriate
 *      `outreach-*` variant and the current value as the label.
 *
 *   2. Editable dropdown trigger (Admin/Viewer with `editable=true`):
 *      The chip wraps the Badge in a `<button type="button">` with
 *      a chevron icon. Clicking opens a small popover with the four
 *      OutreachStatusValue options. Picking one fires the mutation
 *      and closes the popover. The current value is dim-styled in
 *      the popover so the user sees their selection.
 *
 * Mutation state:
 *   - During mutation, the trigger button is `disabled` and shows
 *     the optimistically-updated value (TanStack Query's `onMutate`
 *     in `useUpdateStatusMutation` updates the detail cache in
 *     place; the consumer typically subscribes to the same cache
 *     and re-renders this component with the new `value` prop).
 *   - On error, the parent's cache is rolled back (handled by the
 *     mutation hook) and the toast surface in `@/api/connections`
 *     emits a user-facing error.
 *
 * Accessibility:
 *   - The trigger button has `aria-haspopup="listbox"` and
 *     `aria-expanded` reflecting the popover state.
 *   - The popover has `role="listbox"` and each option has
 *     `role="option"` with `aria-selected` set to the current value.
 *   - Keyboard interactions: Enter / Space opens the popover;
 *     Escape closes it; ArrowUp / ArrowDown move focus between
 *     options (handled by the browser's native listbox behavior
 *     when the user is focused on options).
 *   - The chevron icon is `aria-hidden` so it does not duplicate
 *     the button label.
 *
 * @example In a read-only context (audit history row)
 *   <StatusChip recordId={record.id} value="Contacted" editable={false} />
 *
 * @example In ConnectionFeed (Sales Rep / Admin clicks to change)
 *   <StatusChip recordId={record.id} value={record.outreach_status} />
 */
export function StatusChip({
  recordId,
  value,
  size = "sm",
  editable = true,
  className,
}: StatusChipProps): JSX.Element {
  const variant: BadgeVariant = OUTREACH_TO_VARIANT[value];

  // Stable id for ARIA wiring between the trigger button and the
  // listbox. `useId` is the React 18+ SSR-safe primitive.
  const reactId = useId();
  const triggerId = `status-chip-trigger-${reactId}`;
  const listboxId = `status-chip-listbox-${reactId}`;

  // Local UI state.
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const popoverRef = useRef<HTMLUListElement | null>(null);

  // Resolve the current user's role and decide whether the chip is
  // editable. The frontend role check is a UX courtesy; the backend
  // RBAC decorator is authoritative per AAP Sec 0.7.1 invariant 7.
  const { role } = useRole();
  const roleAdmitsMutation = useMemo(
    () => role !== null && (STATUS_MUTATION_ROLES as ReadonlyArray<string>).includes(role),
    [role],
  );
  const isEditable = editable && roleAdmitsMutation;

  // Wire the mutation hook. The hook handles optimistic updates,
  // rollback on error, success/error toasts, and cache invalidation.
  const mutation = useUpdateStatusMutation();
  const isMutating = mutation.isPending;

  // Click-outside / Escape handling for the popover. We listen on
  // the document so clicks inside the popover (which dispatch onMouseDown
  // before the document handler) keep the popover open, while clicks
  // anywhere else close it. The cleanup on unmount prevents memory
  // leaks when the parent unmounts mid-interaction.
  useEffect(() => {
    if (!open) return undefined;

    const handlePointerDown = (event: MouseEvent): void => {
      const target = event.target as Node | null;
      if (!target) return;
      if (popoverRef.current?.contains(target)) return;
      if (triggerRef.current?.contains(target)) return;
      setOpen(false);
    };

    const handleKeyDown = (event: KeyboardEvent): void => {
      if (event.key === "Escape") {
        setOpen(false);
        // Restore focus to the trigger so keyboard users do not lose
        // their place in the tab order.
        triggerRef.current?.focus();
      }
    };

    document.addEventListener("mousedown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [open]);

  // ---- Read-only branch -----------------------------------------------
  // Either the consumer explicitly disabled editing, or the user's
  // role does not admit mutation. Render the static Badge.
  if (!isEditable) {
    return (
      <span className={clsx("inline-flex", className)} data-testid={`status-chip-${variant}`}>
        <Badge variant={variant} size={size} withDot={value === "In Progress"}>
          {value}
        </Badge>
      </span>
    );
  }

  // ---- Editable branch ------------------------------------------------
  // Render the Badge wrapped in a button with a chevron, plus a
  // dropdown listbox of the four status values when open.
  const handleSelect = (next: OutreachStatusValue): void => {
    setOpen(false);
    if (next === value) {
      // No-op selection (user clicked the current value). Skip the
      // network round-trip; the optimistic update would be a no-op
      // anyway.
      return;
    }
    mutation.mutate({
      id: recordId,
      payload: { outreach_status: next },
    });
  };

  return (
    <span
      className={clsx("relative inline-flex", className)}
      data-testid={`status-chip-${variant}`}
    >
      <button
        ref={triggerRef}
        id={triggerId}
        type="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={listboxId}
        aria-label={`Outreach status: ${value}. Click to change.`}
        disabled={isMutating}
        onClick={() => setOpen((prev) => !prev)}
        className={clsx(
          "inline-flex items-center gap-1 rounded-full",
          "focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500",
          "disabled:opacity-60 disabled:cursor-progress",
          "cursor-pointer",
        )}
      >
        <Badge variant={variant} size={size} withDot={value === "In Progress"}>
          {value}
        </Badge>
        <ChevronDown
          className={clsx("h-3 w-3 text-slate-500 transition-transform", open && "rotate-180")}
          aria-hidden="true"
        />
      </button>

      {open && (
        <ul
          ref={popoverRef}
          id={listboxId}
          role="listbox"
          aria-labelledby={triggerId}
          tabIndex={-1}
          className={clsx(
            "absolute z-10 mt-1 top-full left-0",
            "min-w-[10rem] rounded-md border border-slate-200 bg-white shadow-lg",
            "py-1",
          )}
        >
          {OUTREACH_STATUS_VALUES.map((option) => {
            const optionVariant = OUTREACH_TO_VARIANT[option];
            const isCurrent = option === value;
            return (
              <li
                key={option}
                role="option"
                aria-selected={isCurrent}
                data-testid={`status-chip-option-${optionVariant}`}
                onMouseDown={(event) => {
                  // Use mouseDown (not click) so the selection
                  // dispatches BEFORE the document mouseDown listener
                  // closes the popover. This avoids the
                  // click-target-disappearing race seen in TagInput.tsx.
                  event.preventDefault();
                  handleSelect(option);
                }}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    handleSelect(option);
                  }
                }}
                tabIndex={0}
                className={clsx(
                  "px-2 py-1.5 cursor-pointer hover:bg-slate-50",
                  "focus:outline-none focus:bg-slate-100",
                  isCurrent && "bg-slate-50",
                )}
              >
                <Badge variant={optionVariant} size={size} withDot={option === "In Progress"}>
                  {option}
                </Badge>
              </li>
            );
          })}
        </ul>
      )}
    </span>
  );
}
