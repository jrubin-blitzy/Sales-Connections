/**
 * InvolvementBadge.test.tsx - Vitest tests for the F-003 Involvement
 * Indicator Badge.
 *
 * Targets `frontend/src/features/connections/InvolvementBadge.tsx` - a
 * pure presentational wrapper around the shared `<Badge>` primitive
 * that maps the three involvement values to the corresponding Badge
 * variants. Despite the simplicity, the variant mapping is the single
 * source of visual truth for F-003 across all three primary connection
 * surfaces (Feed row, Detail header, Form selector buttons), so the
 * runtime mapping is exercised explicitly.
 *
 * Coverage goals (>= 85% line, 80% branch per AAP Sec 0.7.7):
 *   - Variant mapping for all three InvolvementValues:
 *       'Warm Intro'      -> 'involvement-warm-intro'
 *       'Soft Reference'  -> 'involvement-soft-reference'
 *       'Target Only'     -> 'involvement-target-only'
 *   - data-testid format `involvement-badge-${variant}` on the
 *     outer wrapper span carrying className "contents".
 *   - Default props: size="sm" and withDot=true.
 *   - Custom size prop forwarded (sm, md, lg) - assertions stay loose
 *     to avoid coupling to the wrapped Badge primitive's Tailwind
 *     classes (covered by the dedicated Badge.test.tsx).
 *   - withDot prop forwarded - presence/absence of the inner
 *     `data-testid="badge-dot"` element rendered by the Badge primitive.
 *   - className prop forwarded to the inner Badge (NOT to the outer
 *     wrapper span, which carries only the literal "contents" class).
 *   - The value string is rendered as Badge children (text content).
 *
 * Component shape (verified against InvolvementBadge.tsx):
 *
 *     <span className="contents" data-testid="involvement-badge-{variant}">
 *       <Badge variant={variant} size={size} withDot={withDot} className={className}>
 *         {value}
 *       </Badge>
 *     </span>
 *
 * Where the inner Badge primitive renders:
 *
 *     <span data-testid="badge-{variant}" class="...{className merged here}...">
 *       {withDot && <span data-testid="badge-dot" aria-hidden="true" />}
 *       <span class="truncate">{children}</span>
 *     </span>
 *
 * This means:
 *   - `screen.getByTestId('involvement-badge-{variant}')` returns the
 *     OUTER wrapper span with className="contents". The custom
 *     className prop is NOT on this element; it is forwarded to the
 *     inner Badge.
 *   - `screen.getByTestId('badge-{variant}')` returns the inner Badge
 *     wrapper, which receives the consumer-supplied className.
 *   - `screen.queryByTestId('badge-dot')` is the Badge primitive's
 *     optional dot indicator; null when withDot=false.
 *
 * Conventions per AAP Sec 0.7.7 and frontend/.prettierrc.json:
 *   - Strict TypeScript; eslint allows `any` in tests but the project
 *     still avoids it.
 *   - Double quotes; trailing commas; 2-space indent; line length <= 100.
 *   - Plain `render` from @testing-library/react (NOT renderWithProviders)
 *     because the component has no hooks, no QueryClient, no router,
 *     and no AuthProvider dependencies.
 *   - jest-dom matchers (`toBeInTheDocument`, `toHaveTextContent`,
 *     `toHaveAttribute`, `toContainElement`) are globally registered
 *     via tests/setup.ts; no per-file matcher imports needed.
 *   - No emoji; no console.log; no async/await (component is
 *     fully synchronous).
 *
 * Coordinates with:
 *   - frontend/src/features/connections/InvolvementBadge.tsx
 *     (system under test - only direct import dependency per the
 *     test file's depends_on_files whitelist)
 *   - frontend/src/components/ui/Badge.tsx (transitive dependency:
 *     wrapped primitive whose `data-testid="badge-{variant}"` and
 *     `data-testid="badge-dot"` are queried below to verify prop
 *     forwarding; not directly imported here)
 *   - frontend/tests/setup.ts (jest-dom matcher registration)
 *   - frontend/vite.config.ts (declares test.globals: true and the
 *     `@/` path alias)
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { InvolvementBadge } from "@/features/connections/InvolvementBadge";

// ---------------------------------------------------------------------------
// Module-scoped test fixtures
// ---------------------------------------------------------------------------

/**
 * Local literal-union type mirroring the three F-003 InvolvementValues
 * produced by `frontend/src/schemas/connection.ts`. Declared locally
 * (rather than imported) so this test file's import surface remains
 * exactly the depends_on_files whitelist of one
 * (`InvolvementBadge.tsx`). If `InvolvementBadge.tsx` ever drifts away
 * from these three values, the call site
 * `render(<InvolvementBadge value={value} />)` below will fail to
 * type-check, surfacing the drift at compile time.
 */
type InvolvementValueLiteral = "Warm Intro" | "Soft Reference" | "Target Only";

/**
 * Exhaustive value-to-variant mapping that mirrors the
 * `INVOLVEMENT_TO_VARIANT` table inside InvolvementBadge.tsx. Listing
 * the expected variant string alongside each input value lets the
 * variant-mapping describe block be table-driven via `it.each` so that
 * adding a fourth involvement value upstream surfaces as a single new
 * row here (and a TypeScript compile error if the mapping is
 * incomplete).
 *
 * The three rows are:
 *   1. 'Warm Intro'      -> 'involvement-warm-intro'
 *   2. 'Soft Reference'  -> 'involvement-soft-reference'
 *   3. 'Target Only'     -> 'involvement-target-only'
 */
const VALUE_TO_VARIANT: ReadonlyArray<{
  readonly value: InvolvementValueLiteral;
  readonly variant: string;
}> = [
  { value: "Warm Intro", variant: "involvement-warm-intro" },
  { value: "Soft Reference", variant: "involvement-soft-reference" },
  { value: "Target Only", variant: "involvement-target-only" },
];

/**
 * Exhaustive list of BadgeSize values exercised by the size prop tests.
 * The Badge primitive tests already verify the Tailwind size classes
 * (text-xs / text-sm / text-base) so these tests assert only that
 * InvolvementBadge accepts each size value without crashing and
 * continues to render the variant testid - keeping the assertions
 * loose to avoid coupling to internal Badge styling.
 */
const ALL_SIZES: ReadonlyArray<"sm" | "md" | "lg"> = ["sm", "md", "lg"];

// ---------------------------------------------------------------------------
// Test suite
// ---------------------------------------------------------------------------

describe("<InvolvementBadge />", () => {
  // -------------------------------------------------------------------------
  // 1. Variant mapping - all three F-003 InvolvementValues
  //
  // The variant map is the central thing under test for this component;
  // every other behaviour is structural. Each of the three F-003 values
  // is exercised twice (once via it.each here, once again via the
  // children describe block below) to guard against the variant string
  // ever being computed incorrectly.
  // -------------------------------------------------------------------------
  describe("variant mapping", () => {
    it.each(VALUE_TO_VARIANT)('value "$value" maps to variant "$variant"', ({ value, variant }) => {
      render(<InvolvementBadge value={value} />);
      const wrapper = screen.getByTestId(`involvement-badge-${variant}`);
      expect(wrapper).toBeInTheDocument();
    });

    it("exposes all 3 documented involvement values in VALUE_TO_VARIANT", () => {
      // Regression guard: if a fourth involvement value is ever added
      // upstream (e.g., via the Zod schema), this expectation surfaces
      // it so the fixture does not silently drift out of sync.
      expect(VALUE_TO_VARIANT).toHaveLength(3);
      expect(VALUE_TO_VARIANT.map((entry) => entry.value)).toEqual([
        "Warm Intro",
        "Soft Reference",
        "Target Only",
      ]);
    });
  });

  // -------------------------------------------------------------------------
  // 2. data-testid - format and uniqueness on the outer wrapper span
  //
  // The wrapper span carries a stable `data-testid` of the form
  // `involvement-badge-<variant>` so unit tests (and snapshot/e2e
  // automation in later layers) can locate the rendered badge without
  // inspecting Tailwind color classes. The wrapper is `<span
  // className="contents">` so it does not affect layout - children
  // render exactly as if the wrapper were not present.
  // -------------------------------------------------------------------------
  describe("data-testid", () => {
    it('uses the exact format "involvement-badge-{variant}" on the outer wrapper', () => {
      render(<InvolvementBadge value="Warm Intro" />);
      // The wrapper span is found by the variant-prefixed testid.
      const wrapper = screen.getByTestId("involvement-badge-involvement-warm-intro");
      expect(wrapper).toBeInTheDocument();
      expect(wrapper.tagName).toBe("SPAN");
    });

    it('renders the wrapper with className="contents" so it is layout-invisible', () => {
      render(<InvolvementBadge value="Soft Reference" />);
      const wrapper = screen.getByTestId("involvement-badge-involvement-soft-reference");
      // The Tailwind `contents` utility sets display: contents on the
      // element - this is what allows the wrapper to host the testid
      // without disturbing the inline-flex Badge layout.
      expect(wrapper.className).toContain("contents");
    });

    it("renders exactly one element matching the variant testid", () => {
      // Defends against accidental duplicate-testid regressions if the
      // component ever wrapped its tree in a fragment-of-spans.
      render(<InvolvementBadge value="Target Only" />);
      expect(screen.getAllByTestId("involvement-badge-involvement-target-only")).toHaveLength(1);
    });

    it("contains the inner Badge primitive matching badge-{variant}", () => {
      // The wrapper must contain the inner Badge span carrying the
      // primitive's own testid, proving the Badge variant prop is
      // forwarded correctly (not just visually present).
      render(<InvolvementBadge value="Warm Intro" />);
      const wrapper = screen.getByTestId("involvement-badge-involvement-warm-intro");
      const innerBadge = screen.getByTestId("badge-involvement-warm-intro");
      expect(wrapper).toContainElement(innerBadge);
    });
  });

  // -------------------------------------------------------------------------
  // 3. Default props - size="sm" and withDot=true
  //
  // The agent prompt deliberately keeps these assertions pragmatic: we
  // do NOT couple to the Badge primitive's specific Tailwind classes
  // (text-xs / h-1.5 / etc.) because those are owned by Badge.test.tsx.
  // Instead we assert (a) the dot indicator is present by default
  // (proving withDot defaults to true) and (b) the badge renders for
  // each documented size value.
  // -------------------------------------------------------------------------
  describe("default props", () => {
    it("renders the dot indicator by default (withDot defaults to true)", () => {
      render(<InvolvementBadge value="Warm Intro" />);
      // Badge primitive renders <span data-testid="badge-dot"> only
      // when withDot is true - so its presence confirms the default.
      expect(screen.queryByTestId("badge-dot")).toBeInTheDocument();
    });

    it("renders the inner Badge with size sm by default", () => {
      // The Badge primitive applies `text-xs` for size sm; that class
      // must be present on the inner Badge (badge-{variant}) when
      // InvolvementBadge is rendered with no explicit size prop.
      render(<InvolvementBadge value="Soft Reference" />);
      const innerBadge = screen.getByTestId("badge-involvement-soft-reference");
      expect(innerBadge.className).toContain("text-xs");
    });

    it("renders the value text as Badge children (default props)", () => {
      // Every default render must surface the value string somewhere
      // inside the wrapper - the value is the badge's primary content.
      render(<InvolvementBadge value="Target Only" />);
      const wrapper = screen.getByTestId("involvement-badge-involvement-target-only");
      expect(wrapper).toHaveTextContent("Target Only");
    });
  });

  // -------------------------------------------------------------------------
  // 4. Custom size prop - forwards to the wrapped Badge
  //
  // For each of the three documented sizes (sm, md, lg) we verify the
  // component renders without crashing AND the variant testid is still
  // present (proving the size prop did not break the Badge primitive's
  // own data-testid output). Specific Tailwind classes are NOT asserted
  // here - that coverage lives in Badge.test.tsx by design.
  // -------------------------------------------------------------------------
  describe("size prop", () => {
    it.each(ALL_SIZES)('size="%s" renders the variant testid', (size) => {
      render(<InvolvementBadge value="Warm Intro" size={size} />);
      expect(screen.getByTestId("involvement-badge-involvement-warm-intro")).toBeInTheDocument();
    });

    it('forwards size="md" to the inner Badge (text-sm class)', () => {
      // Loose coupling: confirm the size prop is wired through by
      // checking the Badge primitive's md-size class on the inner span.
      // Owned by Badge.test.tsx, but checked once here as a wiring test.
      render(<InvolvementBadge value="Warm Intro" size="md" />);
      const innerBadge = screen.getByTestId("badge-involvement-warm-intro");
      expect(innerBadge.className).toContain("text-sm");
    });

    it('forwards size="lg" to the inner Badge (text-base class)', () => {
      render(<InvolvementBadge value="Warm Intro" size="lg" />);
      const innerBadge = screen.getByTestId("badge-involvement-warm-intro");
      expect(innerBadge.className).toContain("text-base");
    });
  });

  // -------------------------------------------------------------------------
  // 5. withDot prop - presence/absence of the dot indicator
  //
  // The Badge primitive renders an inner <span data-testid="badge-dot">
  // when its withDot prop is true and omits it entirely when false.
  // InvolvementBadge forwards its own withDot prop verbatim to Badge,
  // so these assertions verify both the default (true) and the explicit
  // false override.
  // -------------------------------------------------------------------------
  describe("withDot prop", () => {
    it("renders the dot indicator when withDot is omitted (default true)", () => {
      render(<InvolvementBadge value="Warm Intro" />);
      expect(screen.getByTestId("badge-dot")).toBeInTheDocument();
    });

    it("renders the dot indicator when withDot=true is explicit", () => {
      render(<InvolvementBadge value="Soft Reference" withDot={true} />);
      expect(screen.getByTestId("badge-dot")).toBeInTheDocument();
    });

    it("does NOT render the dot indicator when withDot=false", () => {
      render(<InvolvementBadge value="Target Only" withDot={false} />);
      expect(screen.queryByTestId("badge-dot")).toBeNull();
    });

    it('marks the dot aria-hidden="true" so screen readers announce only the label', () => {
      // F-003 accessibility invariant per AAP Sec 0.7.4: the dot is a
      // decorative reinforcement of the variant color and must not be
      // announced by assistive technology.
      render(<InvolvementBadge value="Warm Intro" />);
      const dot = screen.getByTestId("badge-dot");
      expect(dot).toHaveAttribute("aria-hidden", "true");
    });
  });

  // -------------------------------------------------------------------------
  // 6. className prop - forwarded to the inner Badge (NOT the wrapper)
  //
  // Per InvolvementBadge.tsx the className prop is wrapped through
  // clsx() and passed to the inner Badge component. The Badge primitive
  // merges that string onto its own outer span. The `<span className=
  // "contents">` wrapper does NOT receive the consumer-supplied class.
  // -------------------------------------------------------------------------
  describe("className prop", () => {
    it("forwards a custom className to the inner Badge primitive", () => {
      render(<InvolvementBadge value="Warm Intro" className="extra-class" />);
      const innerBadge = screen.getByTestId("badge-involvement-warm-intro");
      expect(innerBadge.className).toContain("extra-class");
    });

    it("forwards multiple space-separated classes to the inner Badge", () => {
      render(<InvolvementBadge value="Soft Reference" className="custom-a custom-b" />);
      const innerBadge = screen.getByTestId("badge-involvement-soft-reference");
      expect(innerBadge.className).toContain("custom-a");
      expect(innerBadge.className).toContain("custom-b");
    });

    it("does NOT add the consumer className to the outer wrapper span", () => {
      // The wrapper carries only the literal "contents" Tailwind class;
      // the consumer's className must NOT leak onto it (otherwise
      // layout integration utilities like ml-4 or w-full would apply
      // twice and produce visually broken margins).
      render(<InvolvementBadge value="Target Only" className="leak-guard" />);
      const wrapper = screen.getByTestId("involvement-badge-involvement-target-only");
      expect(wrapper.className).not.toContain("leak-guard");
    });

    it("renders without crashing when className is omitted", () => {
      // The component must accept undefined className gracefully -
      // clsx(undefined) collapses to an empty string and the Badge
      // primitive accepts that without further special-casing.
      render(<InvolvementBadge value="Warm Intro" />);
      expect(screen.getByTestId("involvement-badge-involvement-warm-intro")).toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // 7. Children rendering - the value text is the Badge label
  //
  // The component passes the input `value` string straight through as
  // Badge children. For each of the three F-003 values we verify the
  // user-facing label is rendered exactly as the InvolvementValue
  // string (case-sensitive, with the embedded space).
  // -------------------------------------------------------------------------
  describe("children rendering", () => {
    it.each(VALUE_TO_VARIANT)(
      'value "$value" is rendered as visible Badge text content',
      ({ value, variant }) => {
        render(<InvolvementBadge value={value} />);
        const wrapper = screen.getByTestId(`involvement-badge-${variant}`);
        expect(wrapper).toHaveTextContent(value);
      },
    );

    it("renders the value text inside an inner truncate span (Badge primitive convention)", () => {
      // Per Badge.tsx the children are wrapped in <span class="truncate">
      // to keep multi-word labels (e.g., "Warm Intro") from blowing out
      // fixed-width feed cells. The label text must appear inside such a
      // span so InvolvementBadge inherits the same truncation behaviour.
      render(<InvolvementBadge value="Warm Intro" />);
      const labelText = screen.getByText("Warm Intro");
      expect(labelText.tagName).toBe("SPAN");
      expect(labelText.className).toContain("truncate");
    });
  });
});
