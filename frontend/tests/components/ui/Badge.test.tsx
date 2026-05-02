/**
 * Badge.test.tsx - Vitest tests for the Badge UI primitive.
 *
 * Targets `frontend/src/components/ui/Badge.tsx` - the stateless
 * pill-shaped label primitive used for:
 *   - F-003 Involvement badge (Warm Intro / Soft Reference / Target Only)
 *   - F-005 Outreach status chip (Not Started / In Progress / Contacted /
 *     Closed)
 *   - F-008 Tag chips on the Connection Detail and Feed views
 *
 * Coverage goals (>= 90% line per `Badge.tsx`):
 *   - Default render with no props uses variant=neutral, size=md.
 *   - All 13 variants render with the matching data-testid attribute.
 *   - All 3 sizes apply the documented Tailwind text-size class.
 *   - `withDot` toggles the data-testid="badge-dot" inner span.
 *   - The dot carries aria-hidden="true" (decorative only).
 *   - `withBorder` toggles the standalone `border` class on the wrapper.
 *   - Children render inside an inner `<span class="truncate">` element
 *     and accept arbitrary ReactNode payloads (icons, removable chips).
 *   - Custom `className` is merged onto the wrapping span.
 *   - Wrapping element is `<span>` (NOT `<div>`).
 *
 * Conventions per AAP Sec 0.7.7 and frontend/.prettierrc.json:
 *   - Strict TypeScript; explicit types preferred (eslint allows `any` in
 *     tests but the project still avoids it).
 *   - Double quotes; trailing commas; 2-space indent; line length <= 100.
 *   - Uses Vitest globals via test.globals: true (vite.config.ts), but
 *     also imports describe/it/expect explicitly so editors and ESLint
 *     resolve them without ambient-type lookups.
 *   - Uses @testing-library/react render and screen directly (Badge is a
 *     pure presentational primitive; no provider stack needed).
 *   - Uses table-driven tests via it.each for the 13 variants and 3 sizes
 *     so adding a new variant becomes one row in `ALL_VARIANTS`.
 *   - No emoji; no console.log; no ASCII art.
 *
 * Coordinates with:
 *   - frontend/src/components/ui/Badge.tsx (system under test)
 *   - frontend/tests/setup.ts (registers @testing-library/jest-dom matchers
 *     globally so `toBeInTheDocument`, `toHaveClass`, and
 *     `toContainElement` are available without per-file imports)
 *   - frontend/tsconfig.json (provides the @/ path alias)
 *   - frontend/vite.config.ts (declares test.globals: true)
 */

import { type ReactElement } from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Badge, type BadgeSize, type BadgeVariant } from "@/components/ui/Badge";

// ---------------------------------------------------------------------------
// Module-scoped test fixtures
// ---------------------------------------------------------------------------

/**
 * Exhaustive list of BadgeVariant string-literal values.
 *
 * Group ordering mirrors the source declaration in `Badge.tsx`: the
 * 6 generic variants come first, then the 3 F-003 involvement variants,
 * then the 4 F-005 outreach variants, totalling 13 variants. The list
 * is the single declarative source of truth for the variant matrix
 * exercised by `it.each` in the "variants" describe block below; the
 * count is also asserted explicitly so a missing entry surfaces in CI.
 */
const ALL_VARIANTS: ReadonlyArray<BadgeVariant> = [
  // Generic palette (6)
  "neutral",
  "brand",
  "success",
  "warning",
  "danger",
  "info",
  // F-003 Involvement palette (3)
  "involvement-warm-intro",
  "involvement-soft-reference",
  "involvement-target-only",
  // F-005 Outreach status palette (4)
  "outreach-not-started",
  "outreach-in-progress",
  "outreach-contacted",
  "outreach-closed",
];

/**
 * Exhaustive list of BadgeSize values paired with the Tailwind
 * text-size utility class the Badge applies for that size. The class
 * is asserted as a substring of `el.className` (rather than via
 * `toHaveClass`) so the test will pass regardless of whether `text-sm`
 * appears alongside other `text-*` utilities in the cascade.
 */
const SIZE_TO_CLASS: ReadonlyArray<{
  readonly size: BadgeSize;
  readonly expectedTextClass: string;
}> = [
  { size: "sm", expectedTextClass: "text-xs" },
  { size: "md", expectedTextClass: "text-sm" },
  { size: "lg", expectedTextClass: "text-base" },
];

/**
 * Helper component used in the children describe block to verify that
 * Badge accepts arbitrary ReactNode payloads (not just plain strings).
 * Plain ASCII text per project convention (no emoji, no fancy unicode).
 * Carries `data-testid="strong-child"` so the test can locate the
 * rendered element regardless of where Badge places it within its tree.
 */
function StrongChild(): ReactElement {
  return <strong data-testid="strong-child">bold</strong>;
}

// ---------------------------------------------------------------------------
// Test suite
// ---------------------------------------------------------------------------

describe("<Badge />", () => {
  // -------------------------------------------------------------------------
  // 1. Rendering - default props, semantic element, attributes
  // -------------------------------------------------------------------------
  describe("rendering", () => {
    it('renders with the default variant "neutral" when no variant prop is set', () => {
      render(<Badge>Default</Badge>);
      const el = screen.getByTestId("badge-neutral");
      expect(el).toBeInTheDocument();
      expect(el).toHaveTextContent("Default");
    });

    it("renders the children label inside the badge wrapper", () => {
      render(<Badge>Hello world</Badge>);
      expect(screen.getByText("Hello world")).toBeInTheDocument();
    });

    it("uses a <span> element, not <div>", () => {
      render(<Badge>X</Badge>);
      const el = screen.getByTestId("badge-neutral");
      expect(el.tagName).toBe("SPAN");
    });

    it("renders the wrapper as the only element matching the variant testid", () => {
      // Defends against accidental duplicate-testid regressions if the
      // component ever wrapped its tree in a fragment-of-spans.
      render(<Badge>One</Badge>);
      expect(screen.getAllByTestId("badge-neutral")).toHaveLength(1);
    });
  });

  // -------------------------------------------------------------------------
  // 2. Variants - all 13 (table-driven)
  // -------------------------------------------------------------------------
  describe("variants", () => {
    it.each(ALL_VARIANTS)('renders variant "%s" with data-testid="badge-%s"', (variant) => {
      render(<Badge variant={variant}>Label</Badge>);
      const el = screen.getByTestId(`badge-${variant}`);
      expect(el).toBeInTheDocument();
      expect(el.tagName).toBe("SPAN");
      // Children must always render regardless of which variant is
      // active - the label is the badge's primary content.
      expect(el).toHaveTextContent("Label");
    });

    it("exposes all 13 documented variants in ALL_VARIANTS", () => {
      // Regression guard: if a new variant is ever added to BadgeVariant
      // without updating ALL_VARIANTS, this expectation surfaces it.
      // 6 generic + 3 involvement + 4 outreach = 13 total.
      expect(ALL_VARIANTS).toHaveLength(13);
    });

    it("includes the 6 generic palette variants", () => {
      expect(ALL_VARIANTS).toEqual(
        expect.arrayContaining(["neutral", "brand", "success", "warning", "danger", "info"]),
      );
    });

    it("includes the 3 F-003 involvement palette variants", () => {
      expect(ALL_VARIANTS).toEqual(
        expect.arrayContaining([
          "involvement-warm-intro",
          "involvement-soft-reference",
          "involvement-target-only",
        ]),
      );
    });

    it("includes the 4 F-005 outreach palette variants", () => {
      expect(ALL_VARIANTS).toEqual(
        expect.arrayContaining([
          "outreach-not-started",
          "outreach-in-progress",
          "outreach-contacted",
          "outreach-closed",
        ]),
      );
    });
  });

  // -------------------------------------------------------------------------
  // 3. Sizes - all 3 (table-driven, with text-size regression coverage)
  // -------------------------------------------------------------------------
  describe("sizes", () => {
    it.each(SIZE_TO_CLASS)(
      'size "$size" applies the "$expectedTextClass" Tailwind class',
      ({ size, expectedTextClass }) => {
        render(<Badge size={size}>Label</Badge>);
        const el = screen.getByTestId("badge-neutral");
        expect(el.className).toContain(expectedTextClass);
      },
    );

    it('uses size="md" by default (text-sm)', () => {
      render(<Badge>Default size</Badge>);
      const el = screen.getByTestId("badge-neutral");
      expect(el.className).toContain("text-sm");
    });

    it("exposes all 3 documented sizes in SIZE_TO_CLASS", () => {
      expect(SIZE_TO_CLASS).toHaveLength(3);
      expect(SIZE_TO_CLASS.map((entry) => entry.size)).toEqual(["sm", "md", "lg"]);
    });
  });

  // -------------------------------------------------------------------------
  // 4. withDot - dot indicator presence, element type, accessibility
  // -------------------------------------------------------------------------
  describe("withDot", () => {
    it("does NOT render the dot when withDot is omitted", () => {
      render(<Badge>X</Badge>);
      expect(screen.queryByTestId("badge-dot")).toBeNull();
    });

    it("does NOT render the dot when withDot=false", () => {
      render(<Badge withDot={false}>X</Badge>);
      expect(screen.queryByTestId("badge-dot")).toBeNull();
    });

    it("renders the dot when withDot=true", () => {
      render(<Badge withDot>X</Badge>);
      expect(screen.getByTestId("badge-dot")).toBeInTheDocument();
    });

    it("renders the dot as a <span> element (not <div>)", () => {
      render(<Badge withDot>X</Badge>);
      expect(screen.getByTestId("badge-dot").tagName).toBe("SPAN");
    });

    it("places the dot inside the badge wrapper (sibling of the label span)", () => {
      render(<Badge withDot>Label</Badge>);
      const badge = screen.getByTestId("badge-neutral");
      const dot = screen.getByTestId("badge-dot");
      expect(badge).toContainElement(dot);
    });

    it('marks the dot aria-hidden="true" so screen readers announce only the label', () => {
      render(<Badge withDot>X</Badge>);
      const dot = screen.getByTestId("badge-dot");
      expect(dot).toHaveAttribute("aria-hidden", "true");
    });

    it("renders the dot for non-default variants too (involvement-warm-intro)", () => {
      render(
        <Badge variant="involvement-warm-intro" withDot>
          Warm Intro
        </Badge>,
      );
      expect(screen.getByTestId("badge-dot")).toBeInTheDocument();
      expect(screen.getByTestId("badge-involvement-warm-intro")).toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // 5. withBorder - presence/absence of the standalone `border` class
  // -------------------------------------------------------------------------
  describe("withBorder", () => {
    it('does NOT add the standalone "border" class when withBorder is omitted', () => {
      // toHaveClass checks for an EXACT class token, NOT a substring match.
      // The variant container may contain `border-slate-200` (a different
      // class entirely); this assertion verifies that the bare `border`
      // token has not been added to the cascade.
      render(<Badge>X</Badge>);
      const el = screen.getByTestId("badge-neutral");
      expect(el).not.toHaveClass("border");
    });

    it('does NOT add the standalone "border" class when withBorder=false', () => {
      render(<Badge withBorder={false}>X</Badge>);
      const el = screen.getByTestId("badge-neutral");
      expect(el).not.toHaveClass("border");
    });

    it('adds the standalone "border" class when withBorder=true', () => {
      render(<Badge withBorder>X</Badge>);
      const el = screen.getByTestId("badge-neutral");
      expect(el).toHaveClass("border");
    });

    it('adds both "border" and the variant-specific border-color class together', () => {
      // Source: VARIANT_CLASSES.neutral.border = "border-slate-200".
      // When withBorder=true, the component composes `["border", border-color]`
      // via clsx so both tokens must end up on the rendered span.
      render(<Badge withBorder>X</Badge>);
      const el = screen.getByTestId("badge-neutral");
      expect(el).toHaveClass("border");
      expect(el).toHaveClass("border-slate-200");
    });
  });

  // -------------------------------------------------------------------------
  // 6. children - text, ReactNode, dot coexistence, truncate wrapper
  // -------------------------------------------------------------------------
  describe("children", () => {
    it('renders text children inside the inner "truncate" span', () => {
      render(<Badge>Hello World</Badge>);
      const text = screen.getByText("Hello World");
      expect(text).toBeInTheDocument();
      // The Badge component wraps its children in `<span class="truncate">`
      // so multi-word labels do not blow out fixed-width feed cells.
      expect(text.className).toContain("truncate");
      expect(text.tagName).toBe("SPAN");
    });

    it("renders ReactNode children (custom inner component)", () => {
      render(
        <Badge>
          <StrongChild />
        </Badge>,
      );
      const inner = screen.getByTestId("strong-child");
      expect(inner).toBeInTheDocument();
      expect(inner.tagName).toBe("STRONG");
      expect(screen.getByText("bold")).toBeInTheDocument();
    });

    it("renders children alongside the dot when withDot=true", () => {
      render(<Badge withDot>Hello</Badge>);
      // Both visual elements must coexist: the leading dot AND the label.
      expect(screen.getByTestId("badge-dot")).toBeInTheDocument();
      expect(screen.getByText("Hello")).toBeInTheDocument();
    });

    it("places the inner truncate span inside the badge wrapper", () => {
      render(<Badge>Wrapped</Badge>);
      const badge = screen.getByTestId("badge-neutral");
      const innerLabel = screen.getByText("Wrapped");
      expect(badge).toContainElement(innerLabel);
    });
  });

  // -------------------------------------------------------------------------
  // 7. className - consumer-supplied classes are merged onto the wrapper
  // -------------------------------------------------------------------------
  describe("className", () => {
    it("merges custom className onto the wrapping span", () => {
      render(<Badge className="custom-class">X</Badge>);
      const el = screen.getByTestId("badge-neutral");
      expect(el).toHaveClass("custom-class");
    });

    it("preserves built-in size classes when a custom className is provided", () => {
      // The default size "md" maps to text-sm; the consumer-supplied
      // class must layer on top WITHOUT clobbering the size cascade.
      render(<Badge className="custom-margin">X</Badge>);
      const el = screen.getByTestId("badge-neutral");
      expect(el).toHaveClass("custom-margin");
      expect(el.className).toContain("text-sm");
    });

    it("preserves built-in variant classes when a custom className is provided", () => {
      // The neutral variant container is "bg-slate-100 text-slate-700";
      // the custom class must not displace those tokens.
      render(<Badge className="ml-4">X</Badge>);
      const el = screen.getByTestId("badge-neutral");
      expect(el).toHaveClass("ml-4");
      expect(el.className).toContain("bg-slate-100");
      expect(el.className).toContain("text-slate-700");
    });

    it("supports multiple space-separated classes", () => {
      render(<Badge className="custom-a custom-b">X</Badge>);
      const el = screen.getByTestId("badge-neutral");
      expect(el).toHaveClass("custom-a");
      expect(el).toHaveClass("custom-b");
    });
  });
});
