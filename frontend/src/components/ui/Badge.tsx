/**
 * Badge.tsx - Pill-shaped color-tinted label primitive.
 *
 * Used as the visual atom for:
 *   - F-003 Involvement indicator (Warm Intro / Soft Reference / Target Only)
 *     via frontend/src/features/connections/InvolvementBadge.tsx
 *   - F-005 Outreach status (Not Started / In Progress / Contacted / Closed)
 *     via frontend/src/features/connections/StatusChip.tsx
 *   - F-008 Tag chips on the Connection Detail and Feed views
 *     via frontend/src/features/connections/TagInput.tsx
 *
 * Variants (Tailwind classes verified against tailwind.config.ts):
 *
 *   GENERIC:
 *     neutral   slate-100 / slate-700
 *     brand     brand-100 / brand-800
 *     success   emerald-100 / emerald-800
 *     warning   amber-100 / amber-800
 *     danger    red-100 / red-800
 *     info      blue-100 / blue-800
 *
 *   INVOLVEMENT (F-003) - driven by tailwind.config.ts theme.extend.colors.involvement:
 *     involvement-warm-intro       involvement.warm-intro.{bg,fg,border}
 *     involvement-soft-reference   involvement.soft-reference.{bg,fg,border}
 *     involvement-target-only      involvement.target-only.{bg,fg,border}
 *
 *   OUTREACH (F-005) - driven by tailwind.config.ts theme.extend.colors.outreach:
 *     outreach-not-started         outreach.not-started.{bg,fg,border}
 *     outreach-in-progress         outreach.in-progress.{bg,fg,border}
 *     outreach-contacted           outreach.contacted.{bg,fg,border}
 *     outreach-closed              outreach.closed.{bg,fg,border}
 *
 * Sizes:
 *   sm   text-xs px-1.5 py-0.5  (compact for table rows)
 *   md   text-sm px-2 py-0.5    (default)
 *   lg   text-base px-2.5 py-1  (detail views)
 *
 * Optional dot indicator: when `withDot=true`, prepends a small filled
 * circle (the same fg color) before the label. Useful for "live" status
 * indicators (e.g., In Progress).
 *
 * Conventions per AAP Sec 0.7.7:
 *   - TailwindCSS utility classes only (no inline styles).
 *   - Strict TypeScript; no `any`.
 *   - clsx for conditional class composition.
 *   - Stateless presentational primitive; no hooks; no business logic.
 *   - Double quotes per project prettier configuration (singleQuote: false).
 *
 * Coordinates with:
 *   - frontend/tailwind.config.ts - provides involvement.* and outreach.*
 *     color tokens; CRITICAL drift point if tokens change.
 *   - frontend/src/features/connections/InvolvementBadge.tsx - wraps Badge
 *     with involvement-enum-to-variant mapping.
 *   - frontend/src/features/connections/StatusChip.tsx - wraps Badge with
 *     outreach-enum-to-variant mapping plus role-gated inline edit.
 *   - frontend/src/features/connections/TagInput.tsx - uses Badge for
 *     removable tag chips (composes the X button as part of children).
 *   - frontend/tests/components/ui/Badge.test.tsx - verifies all 13
 *     variants, 3 sizes, and the optional withDot / withBorder modifiers.
 */

import { type JSX, type ReactNode } from "react";
import clsx from "clsx";

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

/**
 * Visual variant. Generic palette options plus theme-token-aware
 * involvement (F-003) and outreach (F-005) variants.
 *
 * The involvement-* and outreach-* values map 1:1 to nested color tokens
 * declared in frontend/tailwind.config.ts under
 * theme.extend.colors.involvement.* and theme.extend.colors.outreach.*.
 * If those token names ever change, this union and the VARIANT_CLASSES
 * map below must be updated in lockstep.
 */
export type BadgeVariant =
  // Generic palette
  | "neutral"
  | "brand"
  | "success"
  | "warning"
  | "danger"
  | "info"
  // F-003 Involvement palette (matches tailwind.config.ts theme.extend)
  | "involvement-warm-intro"
  | "involvement-soft-reference"
  | "involvement-target-only"
  // F-005 Outreach status palette (matches tailwind.config.ts theme.extend)
  | "outreach-not-started"
  | "outreach-in-progress"
  | "outreach-contacted"
  | "outreach-closed";

/**
 * Visual size variant.
 *
 * - "sm": text-xs, compact - intended for dense tables (feed rows).
 * - "md": text-sm, default - matches body-text rhythm.
 * - "lg": text-base - detail views and emphasis surfaces.
 */
export type BadgeSize = "sm" | "md" | "lg";

/**
 * Public props for the Badge component.
 *
 * `readonly` is applied to every prop so consumers cannot accidentally
 * mutate the props object inside event handlers (defensive programming
 * convention shared with Button.tsx).
 */
export interface BadgeProps {
  /** Visual variant. Defaults to "neutral". */
  readonly variant?: BadgeVariant;

  /** Visual size. Defaults to "md". */
  readonly size?: BadgeSize;

  /**
   * When true, renders a small filled circle (matching the foreground
   * color of the chosen variant) before the label. Useful for "live"
   * status indicators such as the outreach In Progress chip. The dot is
   * marked aria-hidden so screen readers announce only the label.
   */
  readonly withDot?: boolean;

  /**
   * When true, renders a thin 1px border around the badge using the
   * border-* token of the chosen variant. Increases visual weight when
   * the badge appears on a colored background (e.g., a tinted row).
   */
  readonly withBorder?: boolean;

  /**
   * Optional additional className merged onto the outer span. Allows
   * consumers to layer alignment, margin, or width utilities without
   * forking the primitive. Conflicting Tailwind classes are resolved
   * by source order (later classes win in the cascade).
   */
  readonly className?: string;

  /**
   * Label content; usually a short text string but may include an icon
   * node, a removable-chip "X" button (TagInput pattern), or any other
   * inline node.
   */
  readonly children: ReactNode;
}

// ---------------------------------------------------------------------------
// Variant + size class maps
//
// These maps are module-level constants (not inlined) so:
//   1. The JIT Tailwind compiler picks up every utility at build time.
//   2. Tests / Storybook / future variants can iterate over the keys.
//   3. The class strings are visible in one place for design review.
//
// Each variant exposes three coordinated class strings:
//   container - background + foreground for the pill itself
//   dot       - background for the optional leading filled circle
//   border    - border color when withBorder=true
//
// Tailwind class names mirror the tailwind.config.ts token paths:
//   theme.extend.colors.involvement["warm-intro"].bg
//     => bg-involvement-warm-intro-bg
//   theme.extend.colors.outreach["in-progress"].fg
//     => text-outreach-in-progress-fg
// ---------------------------------------------------------------------------

const VARIANT_CLASSES: Record<BadgeVariant, { container: string; dot: string; border: string }> = {
  // -----------------------------------------------------------------------
  // Generic palette - draws from Tailwind's stock 100/500/700/800 ramps.
  // -----------------------------------------------------------------------
  neutral: {
    container: "bg-slate-100 text-slate-700",
    dot: "bg-slate-500",
    border: "border-slate-200",
  },
  brand: {
    container: "bg-brand-100 text-brand-800",
    dot: "bg-brand-500",
    border: "border-brand-200",
  },
  success: {
    container: "bg-emerald-100 text-emerald-800",
    dot: "bg-emerald-500",
    border: "border-emerald-200",
  },
  warning: {
    container: "bg-amber-100 text-amber-800",
    dot: "bg-amber-500",
    border: "border-amber-200",
  },
  danger: {
    container: "bg-red-100 text-red-800",
    dot: "bg-red-500",
    border: "border-red-200",
  },
  info: {
    container: "bg-blue-100 text-blue-800",
    dot: "bg-blue-500",
    border: "border-blue-200",
  },

  // -----------------------------------------------------------------------
  // F-003 Involvement palette
  // (tailwind.config.ts theme.extend.colors.involvement.*)
  // -----------------------------------------------------------------------
  "involvement-warm-intro": {
    container: "bg-involvement-warm-intro-bg text-involvement-warm-intro-fg",
    dot: "bg-involvement-warm-intro-fg",
    border: "border-involvement-warm-intro-border",
  },
  "involvement-soft-reference": {
    container: "bg-involvement-soft-reference-bg text-involvement-soft-reference-fg",
    dot: "bg-involvement-soft-reference-fg",
    border: "border-involvement-soft-reference-border",
  },
  "involvement-target-only": {
    container: "bg-involvement-target-only-bg text-involvement-target-only-fg",
    dot: "bg-involvement-target-only-fg",
    border: "border-involvement-target-only-border",
  },

  // -----------------------------------------------------------------------
  // F-005 Outreach status palette
  // (tailwind.config.ts theme.extend.colors.outreach.*)
  // -----------------------------------------------------------------------
  "outreach-not-started": {
    container: "bg-outreach-not-started-bg text-outreach-not-started-fg",
    dot: "bg-outreach-not-started-fg",
    border: "border-outreach-not-started-border",
  },
  "outreach-in-progress": {
    container: "bg-outreach-in-progress-bg text-outreach-in-progress-fg",
    dot: "bg-outreach-in-progress-fg",
    border: "border-outreach-in-progress-border",
  },
  "outreach-contacted": {
    container: "bg-outreach-contacted-bg text-outreach-contacted-fg",
    dot: "bg-outreach-contacted-fg",
    border: "border-outreach-contacted-border",
  },
  "outreach-closed": {
    container: "bg-outreach-closed-bg text-outreach-closed-fg",
    dot: "bg-outreach-closed-fg",
    border: "border-outreach-closed-border",
  },
};

/**
 * Per-size container padding/typography and dot dimensions.
 *
 * The dot dimensions intentionally scale with the badge size so the dot
 * remains visually proportional to the label across all three sizes.
 */
const SIZE_CLASSES: Record<BadgeSize, { container: string; dot: string }> = {
  sm: {
    container: "text-xs px-1.5 py-0.5",
    dot: "h-1.5 w-1.5",
  },
  md: {
    container: "text-sm px-2 py-0.5",
    dot: "h-2 w-2",
  },
  lg: {
    container: "text-base px-2.5 py-1",
    dot: "h-2.5 w-2.5",
  },
};

// ---------------------------------------------------------------------------
// Badge component
// ---------------------------------------------------------------------------

/**
 * Pill-shaped color-tinted label.
 *
 * Renders an inline `<span>` element (rather than a block-level wrapper)
 * so the badge flows naturally inside inline contexts like `<p>`,
 * `<button>`, `<th>`, and `<td>` without breaking layout. The element
 * is decorated with:
 *   - `inline-flex` so the optional dot and label are vertically centered.
 *   - `whitespace-nowrap` so multi-word labels (e.g., "In Progress") do
 *     not wrap inside the pill.
 *   - `align-middle` so the badge aligns with surrounding text inside
 *     `<th>`/`<td>` cells, headings, and inline paragraphs.
 *   - `rounded-full` to produce the pill shape regardless of size.
 *
 * The optional dot uses `shrink-0` so it stays its specified size when
 * the parent container is constrained; only the inner label span is
 * subject to truncation via `truncate`. Note that `truncate` only
 * activates when the badge is inside a fixed-width container - that is
 * the consumer's responsibility, not the primitive's.
 *
 * @example Generic
 *   <Badge variant="brand">3 new</Badge>
 *
 * @example Involvement (F-003)
 *   <Badge variant="involvement-warm-intro" withDot>Warm Intro</Badge>
 *
 * @example Outreach (F-005)
 *   <Badge variant="outreach-in-progress" withBorder>In Progress</Badge>
 *
 * @example Removable tag chip (F-008)
 *   <Badge variant="brand" size="sm">
 *     {tagName}
 *     <button aria-label={`Remove ${tagName}`} onClick={onRemove}>X</button>
 *   </Badge>
 */
export function Badge({
  variant = "neutral",
  size = "md",
  withDot = false,
  withBorder = false,
  className,
  children,
}: BadgeProps): JSX.Element {
  const variantClasses = VARIANT_CLASSES[variant];
  const sizeClasses = SIZE_CLASSES[size];

  return (
    <span
      className={clsx(
        // Base layout + typography for every badge.
        "inline-flex items-center gap-1.5 rounded-full font-medium leading-tight",
        "whitespace-nowrap align-middle",
        // Variant-derived colors.
        variantClasses.container,
        // Size-derived padding + font size.
        sizeClasses.container,
        // Optional border modifier.
        withBorder && ["border", variantClasses.border],
        // Consumer-supplied class overrides.
        className,
      )}
      data-testid={`badge-${variant}`}
    >
      {withDot && (
        <span
          aria-hidden="true"
          className={clsx(
            "inline-block rounded-full shrink-0",
            variantClasses.dot,
            sizeClasses.dot,
          )}
          data-testid="badge-dot"
        />
      )}
      <span className="truncate">{children}</span>
    </span>
  );
}
