/**
 * InvolvementBadge.tsx - F-003 Involvement Indicator Badge.
 *
 * Pure presentational component that wraps the shared Badge primitive
 * with the appropriate `involvement-*` variant for the given value.
 * The component is the visual atom for F-003 across all three primary
 * Connection screens (Feed row, Detail header, Form selector buttons).
 *
 * The three involvement values per AAP Sec 0.1.1 F-003:
 *   - Warm Intro      submitter will personally facilitate the intro.
 *   - Soft Reference  submitter is a usable reference but not a direct
 *                     intro path.
 *   - Target Only     submitter has no relationship; target is just a
 *                     company/role they would like to engage.
 *
 * Visual treatment per the design system tokens declared in
 * `frontend/tailwind.config.ts` under theme.extend.colors.involvement.*
 * and consumed by the Badge primitive in
 * `frontend/src/components/ui/Badge.tsx`:
 *   - involvement-warm-intro       (typically green tint)
 *   - involvement-soft-reference   (typically blue tint)
 *   - involvement-target-only      (typically slate tint)
 *
 * Stateless. No hooks, no side effects. Composable from any other
 * feature component without prop drilling or context.
 *
 * Conventions per AAP Sec 0.7.7:
 *   - Strict TypeScript; no `any`; explicit `JSX.Element` return.
 *   - Named exports only (no default export).
 *   - clsx for conditional className composition.
 *   - TailwindCSS utility classes only (no inline `style`).
 *   - Double quotes per project Prettier configuration
 *     (singleQuote: false, trailingComma: "all", printWidth: 100).
 *   - Path imports use the `@/` alias declared in `vite.config.ts`
 *     and mirrored in `tsconfig.json`.
 *   - Type-only imports use `import type` per
 *     `verbatimModuleSyntax: true` in `tsconfig.json`.
 *
 * Coordinates with:
 *   - frontend/src/components/ui/Badge.tsx - the wrapped primitive
 *     that owns the variant-to-Tailwind-class mapping.
 *   - frontend/src/schemas/connection.ts - source of the
 *     `InvolvementValue` literal-union type that mirrors the
 *     backend pydantic / PostgreSQL `involvement_type` enum.
 *   - frontend/src/features/connections/ConnectionFeed.tsx - per-row
 *     consumer; renders one InvolvementBadge in the involvement
 *     column.
 *   - frontend/src/features/connections/ConnectionDetail.tsx -
 *     header consumer; renders one InvolvementBadge next to the
 *     connection's full name.
 *   - frontend/src/features/connections/AddEditConnectionForm.tsx -
 *     form consumer; renders one InvolvementBadge inside each of the
 *     three single-select buttons that drive the involvement field.
 *   - frontend/tests/features/connections/InvolvementBadge.test.tsx -
 *     verifies all three variants are mapped, the value text is
 *     rendered, and the default size and withDot props are applied.
 */

import type { JSX } from "react";
import clsx from "clsx";

import { Badge } from "@/components/ui/Badge";
import type { InvolvementValue } from "@/schemas/connection";

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

/**
 * Public props for the InvolvementBadge component.
 *
 * `readonly` is applied to every prop so consumers cannot accidentally
 * mutate the props object inside event handlers; this convention is
 * shared with the wrapped Badge primitive and with Button.
 *
 * Members exposed (per the file schema in the AAP):
 *   - value      The involvement value to display.
 *   - size       Optional badge size; defaults to "sm".
 *   - withDot    Optional leading dot indicator; defaults to true.
 *   - className  Optional className for layout integration.
 */
export interface InvolvementBadgeProps {
  /**
   * The involvement value to display. Must be one of the three F-003
   * literal strings exported from `@/schemas/connection`. The string
   * itself is the user-facing label rendered inside the badge.
   */
  readonly value: InvolvementValue;

  /**
   * Badge size. Defaults to "sm" because the most common consumer
   * (the connection feed table) needs compact rows; larger contexts
   * such as the detail header pass "md" or "lg" explicitly.
   */
  readonly size?: "sm" | "md" | "lg";

  /**
   * Whether to render a leading filled-circle dot indicator. Defaults
   * to `true` so the visual treatment combines color + dot + text
   * label, satisfying the AAP Sec 0.7.4 accessibility requirement
   * that "color is never the sole indicator of meaning".
   */
  readonly withDot?: boolean;

  /**
   * Optional additional className merged into the wrapped Badge. Lets
   * consumers layer alignment, margin, or width utilities without
   * forking this primitive. Conflicting Tailwind classes are resolved
   * by source order (later classes win in the cascade).
   */
  readonly className?: string;
}

// ---------------------------------------------------------------------------
// Internal types and variant map
// ---------------------------------------------------------------------------

/**
 * The three Badge variant keys that visually represent the F-003
 * involvement values. These string-literal keys mirror the
 * `BadgeVariant` involvement-* members declared in
 * `frontend/src/components/ui/Badge.tsx`. Listing them here as a
 * dedicated narrowed type (rather than reusing the broader
 * `BadgeVariant`) lets the `INVOLVEMENT_TO_VARIANT` map below provide
 * compile-time exhaustiveness over `InvolvementValue`.
 *
 * If a fourth involvement value is ever introduced upstream, the
 * `Record<InvolvementValue, InvolvementBadgeVariant>` constraint will
 * fail to type-check until this union is updated in lockstep with the
 * Zod schema and the backend pydantic / PostgreSQL enum.
 */
type InvolvementBadgeVariant =
  | "involvement-warm-intro"
  | "involvement-soft-reference"
  | "involvement-target-only";

/**
 * Translation table from the Zod-derived `InvolvementValue` literal
 * union to the corresponding Badge variant. The TypeScript
 * `Record<InvolvementValue, InvolvementBadgeVariant>` constraint
 * enforces exhaustiveness across the three F-003 values:
 *   - "Warm Intro"      -> "involvement-warm-intro"
 *   - "Soft Reference"  -> "involvement-soft-reference"
 *   - "Target Only"     -> "involvement-target-only"
 *
 * Declared as a module-level `const` so the lookup is a single object
 * dereference at render time and so future contributors can audit the
 * full mapping at a glance. This is the single source of truth for
 * the value-to-variant translation; consumers should never duplicate
 * it.
 */
const INVOLVEMENT_TO_VARIANT: Record<InvolvementValue, InvolvementBadgeVariant> = {
  "Warm Intro": "involvement-warm-intro",
  "Soft Reference": "involvement-soft-reference",
  "Target Only": "involvement-target-only",
};

// ---------------------------------------------------------------------------
// InvolvementBadge component
// ---------------------------------------------------------------------------

/**
 * Render a colored pill badge for a single F-003 involvement value.
 *
 * The component is intentionally minimal: it maps the input value to
 * the corresponding Badge variant, then delegates all visual rendering
 * (background, foreground, border, dot) to the Badge primitive. There
 * is no business logic and no state.
 *
 * The wrapping `<span className="contents">` carries a stable
 * `data-testid` of the form `involvement-badge-<variant>` so unit
 * tests can locate the rendered badge without inspecting Tailwind
 * color classes. The `contents` Tailwind utility sets
 * `display: contents` on the wrapper, which removes the wrapper from
 * the layout flow entirely - children render exactly as if the
 * wrapper were not present, preserving the inline-flex layout of the
 * inner Badge across every consumer (table cells, flex rows, button
 * children).
 *
 * @example In ConnectionFeed.tsx (sm, with dot)
 *   <InvolvementBadge value={record.involvement} />
 *
 * @example In ConnectionDetail.tsx (md, header context)
 *   <InvolvementBadge value={record.involvement} size="md" />
 *
 * @example In AddEditConnectionForm.tsx (inside a select button)
 *   <InvolvementBadge value="Warm Intro" withDot={false} />
 */
export function InvolvementBadge({
  value,
  size = "sm",
  withDot = true,
  className,
}: InvolvementBadgeProps): JSX.Element {
  const variant = INVOLVEMENT_TO_VARIANT[value];

  return (
    <span className="contents" data-testid={`involvement-badge-${variant}`}>
      <Badge variant={variant} size={size} withDot={withDot} className={clsx(className)}>
        {value}
      </Badge>
    </span>
  );
}
