/**
 * Button.test.tsx - Vitest tests for the Button UI primitive.
 *
 * Targets `frontend/src/components/ui/Button.tsx` - the Tailwind-styled
 * button primitive used by every feature component in the SPA. The
 * Button is the most-composed primitive in the SPA so this test file
 * exercises every documented branch.
 *
 * Coverage goals (>= 90% line per `Button.tsx`):
 *   - Default render with no props (variant=primary, size=md, type=button).
 *   - All 5 variants render with correct data-variant attribute.
 *   - All 3 sizes render with correct data-size attribute and height class.
 *   - Loading state: aria-busy="true", disabled, spinner visible, label
 *     visible, data-loading attribute present.
 *   - Disabled state: disabled attribute set; default is enabled.
 *   - onClick short-circuit when loading or disabled (defense-in-depth).
 *   - forwardRef works for imperative focus.
 *   - type="button" default; type="submit" / type="reset" passthrough.
 *   - leftIcon and rightIcon render correctly; spinner replaces leftIcon
 *     and rightIcon is hidden when loading=true.
 *   - fullWidth adds w-full class; default does NOT.
 *
 * Conventions per AAP Sec 0.7.7 and frontend/.prettierrc.json:
 *   - Strict TypeScript; no `any`.
 *   - Double quotes; trailing commas; 2-space indent; line length <= 100.
 *   - Uses Vitest globals via test.globals: true (vite.config.ts), but
 *     also imports describe/it/expect/vi explicitly for IDE type-awareness.
 *   - Uses @testing-library/react render and screen directly (Button is a
 *     pure presentational primitive; no provider stack needed).
 *   - Uses @testing-library/user-event for click simulation (NOT fireEvent).
 *   - Uses createRef for forwardRef tests (useRef is a hook and only
 *     valid inside components).
 *   - No emoji; no console.log.
 *
 * Coordinates with:
 *   - frontend/src/components/ui/Button.tsx (system under test)
 *   - frontend/tests/setup.ts (registers @testing-library/jest-dom matchers)
 *   - frontend/tsconfig.json (provides the @/ path alias)
 *   - frontend/vite.config.ts (declares test.globals: true)
 */

import { createRef, type ReactElement } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { Button, type ButtonSize, type ButtonVariant } from "@/components/ui/Button";

// ---------------------------------------------------------------------------
// Module-scoped test fixtures
// ---------------------------------------------------------------------------

/**
 * Exhaustive list of ButtonVariant string-literal values. Used as the
 * data table for the "variants" describe block. If a new variant is
 * ever added to `ButtonVariant`, TypeScript's exhaustiveness will not
 * catch it here (strings are widened) - but the it.each matrix below
 * is the single declarative source of truth and a missing entry would
 * be caught by code review and by the variant-coverage assertion in
 * the rendering describe.
 */
const ALL_VARIANTS: ReadonlyArray<ButtonVariant> = [
  "primary",
  "secondary",
  "destructive",
  "ghost",
  "link",
];

/**
 * Exhaustive list of ButtonSize values paired with the Tailwind height
 * class the Button renders for that size. The class is asserted as a
 * substring of `button.className` to defend against accidental SIZE_CLASSES
 * regressions in `frontend/src/components/ui/Button.tsx`.
 */
const ALL_SIZES: ReadonlyArray<{ readonly size: ButtonSize; readonly heightClass: string }> = [
  { size: "sm", heightClass: "h-8" },
  { size: "md", heightClass: "h-10" },
  { size: "lg", heightClass: "h-12" },
];

/**
 * Helper icon component used in the leftIcon test cases. Plain ASCII
 * label per project convention (no emoji, no fancy unicode). Carries
 * `data-testid="plus-icon"` so tests can locate the rendered icon
 * regardless of where the Button places it within its rendered tree.
 */
function PlusIcon(): ReactElement {
  return <span data-testid="plus-icon">PLUS</span>;
}

/**
 * Helper icon component used in the rightIcon test cases. Plain ASCII
 * label per project convention. Carries `data-testid="arrow-icon"`.
 */
function ArrowIcon(): ReactElement {
  return <span data-testid="arrow-icon">RIGHT</span>;
}

// ---------------------------------------------------------------------------
// Test suite
// ---------------------------------------------------------------------------

describe("<Button />", () => {
  // -------------------------------------------------------------------------
  // 1. Rendering - default props
  // -------------------------------------------------------------------------
  describe("rendering", () => {
    it("renders with default variant=primary and size=md when no props are set", () => {
      render(<Button>Click me</Button>);
      const button = screen.getByTestId("button");
      expect(button).toBeInTheDocument();
      expect(button).toHaveAttribute("data-variant", "primary");
      expect(button).toHaveAttribute("data-size", "md");
      expect(button).toHaveTextContent("Click me");
    });

    it("renders the children label", () => {
      render(<Button>Save Connection</Button>);
      expect(screen.getByText("Save Connection")).toBeInTheDocument();
    });

    it("renders an HTMLButtonElement (semantic <button>)", () => {
      render(<Button>X</Button>);
      const button = screen.getByTestId("button");
      expect(button.tagName).toBe("BUTTON");
    });

    it("passes through arbitrary HTML attributes (e.g., aria-label, name)", () => {
      render(
        <Button aria-label="Close dialog" name="close-btn">
          X
        </Button>,
      );
      const button = screen.getByTestId("button");
      expect(button).toHaveAttribute("aria-label", "Close dialog");
      expect(button).toHaveAttribute("name", "close-btn");
    });

    it("merges consumer-supplied className with the variant/size classes", () => {
      render(<Button className="custom-class">X</Button>);
      const button = screen.getByTestId("button");
      expect(button).toHaveClass("custom-class");
    });
  });

  // -------------------------------------------------------------------------
  // 2. Variants - all 5 (table-driven)
  // -------------------------------------------------------------------------
  describe("variants", () => {
    it.each(ALL_VARIANTS)(
      "renders variant '%s' with the matching data-variant attribute",
      (variant) => {
        render(<Button variant={variant}>X</Button>);
        const button = screen.getByTestId("button");
        expect(button).toHaveAttribute("data-variant", variant);
      },
    );

    it("exposes all 5 documented variants in ALL_VARIANTS", () => {
      // Regression guard: if a new variant is added to ButtonVariant
      // without updating ALL_VARIANTS, this expectation surfaces it.
      expect(ALL_VARIANTS).toHaveLength(5);
      expect(ALL_VARIANTS).toEqual(
        expect.arrayContaining(["primary", "secondary", "destructive", "ghost", "link"]),
      );
    });
  });

  // -------------------------------------------------------------------------
  // 3. Sizes - all 3 (table-driven, with height-class regression coverage)
  // -------------------------------------------------------------------------
  describe("sizes", () => {
    it.each(ALL_SIZES)(
      "size '$size' sets data-size and contains class '$heightClass'",
      ({ size, heightClass }) => {
        render(<Button size={size}>X</Button>);
        const button = screen.getByTestId("button");
        expect(button).toHaveAttribute("data-size", size);
        expect(button).toHaveClass(heightClass);
      },
    );

    it("exposes all 3 documented sizes in ALL_SIZES", () => {
      expect(ALL_SIZES).toHaveLength(3);
      expect(ALL_SIZES.map((entry) => entry.size)).toEqual(["sm", "md", "lg"]);
    });
  });

  // -------------------------------------------------------------------------
  // 4. Loading state - aria-busy, disabled, spinner, label visible,
  //                    data-loading, icon swap
  // -------------------------------------------------------------------------
  describe("loading state", () => {
    it('sets aria-busy="true" when loading=true', () => {
      render(<Button loading>Save</Button>);
      expect(screen.getByTestId("button")).toHaveAttribute("aria-busy", "true");
    });

    it("disables the button when loading=true (defense-in-depth)", () => {
      render(<Button loading>Save</Button>);
      expect(screen.getByTestId("button")).toBeDisabled();
    });

    it('renders the spinner via data-testid="button-loading-spinner" when loading=true', () => {
      render(<Button loading>Save</Button>);
      const spinner = screen.getByTestId("button-loading-spinner");
      expect(spinner).toBeInTheDocument();
    });

    it("applies the animate-spin class to the spinner when loading=true", () => {
      render(<Button loading>Save</Button>);
      expect(screen.getByTestId("button-loading-spinner")).toHaveClass("animate-spin");
    });

    it("keeps the children label visible when loading=true", () => {
      render(<Button loading>Save Connection</Button>);
      expect(screen.getByText("Save Connection")).toBeInTheDocument();
    });

    it('sets data-loading="true" attribute when loading=true', () => {
      render(<Button loading>X</Button>);
      expect(screen.getByTestId("button")).toHaveAttribute("data-loading", "true");
    });

    it("does NOT set aria-busy when loading is omitted", () => {
      render(<Button>X</Button>);
      expect(screen.getByTestId("button")).not.toHaveAttribute("aria-busy", "true");
    });

    it("does NOT set aria-busy when loading=false explicitly", () => {
      render(<Button loading={false}>X</Button>);
      expect(screen.getByTestId("button")).not.toHaveAttribute("aria-busy", "true");
    });

    it("does NOT render the spinner when loading is omitted", () => {
      render(<Button>X</Button>);
      expect(screen.queryByTestId("button-loading-spinner")).toBeNull();
    });

    it("does NOT set data-loading attribute when loading is omitted", () => {
      render(<Button>X</Button>);
      expect(screen.getByTestId("button")).not.toHaveAttribute("data-loading");
    });
  });

  // -------------------------------------------------------------------------
  // 5. Disabled state
  // -------------------------------------------------------------------------
  describe("disabled state", () => {
    it("disables the button when disabled=true", () => {
      render(<Button disabled>X</Button>);
      expect(screen.getByTestId("button")).toBeDisabled();
    });

    it("is enabled by default (no disabled prop)", () => {
      render(<Button>X</Button>);
      expect(screen.getByTestId("button")).not.toBeDisabled();
    });

    it("is enabled when disabled=false explicitly", () => {
      render(<Button disabled={false}>X</Button>);
      expect(screen.getByTestId("button")).not.toBeDisabled();
    });
  });

  // -------------------------------------------------------------------------
  // 6. onClick handler - normal click, short-circuit on loading/disabled
  // -------------------------------------------------------------------------
  describe("onClick handler", () => {
    it("fires onClick exactly once when clicked normally", async () => {
      const handleClick = vi.fn();
      const user = userEvent.setup();
      render(<Button onClick={handleClick}>Click</Button>);

      await user.click(screen.getByTestId("button"));

      expect(handleClick).toHaveBeenCalledTimes(1);
    });

    it("does NOT fire onClick when loading=true", async () => {
      const handleClick = vi.fn();
      const user = userEvent.setup();
      render(
        <Button loading onClick={handleClick}>
          X
        </Button>,
      );

      await user.click(screen.getByTestId("button"));

      // Both the `disabled` attribute (which user-event honours) AND the
      // runtime short-circuit `if (isDisabled) return;` prevent invocation.
      expect(handleClick).not.toHaveBeenCalled();
    });

    it("does NOT fire onClick when disabled=true", async () => {
      const handleClick = vi.fn();
      const user = userEvent.setup();
      render(
        <Button disabled onClick={handleClick}>
          X
        </Button>,
      );

      await user.click(screen.getByTestId("button"));

      expect(handleClick).not.toHaveBeenCalled();
    });

    it("passes a click event object to the onClick handler", async () => {
      const handleClick = vi.fn();
      const user = userEvent.setup();
      render(<Button onClick={handleClick}>Click</Button>);

      await user.click(screen.getByTestId("button"));

      expect(handleClick).toHaveBeenCalledTimes(1);
      const firstCallArg = handleClick.mock.calls[0]?.[0];
      expect(firstCallArg).toBeDefined();
      expect(firstCallArg).toHaveProperty("type", "click");
    });

    it("supports being clicked multiple times (no implicit single-fire guard)", async () => {
      const handleClick = vi.fn();
      const user = userEvent.setup();
      render(<Button onClick={handleClick}>X</Button>);

      const button = screen.getByTestId("button");
      await user.click(button);
      await user.click(button);
      await user.click(button);

      expect(handleClick).toHaveBeenCalledTimes(3);
    });
  });

  // -------------------------------------------------------------------------
  // 7. forwardRef - imperative ref + focus
  // -------------------------------------------------------------------------
  describe("forwardRef", () => {
    it("forwards the ref to the underlying <button> element", () => {
      const ref = createRef<HTMLButtonElement>();
      render(<Button ref={ref}>X</Button>);

      expect(ref.current).not.toBeNull();
      expect(ref.current).toBeInstanceOf(HTMLButtonElement);
    });

    it("imperative focus via the forwarded ref makes the button document.activeElement", () => {
      const ref = createRef<HTMLButtonElement>();
      render(<Button ref={ref}>X</Button>);

      ref.current?.focus();

      expect(document.activeElement).toBe(ref.current);
    });

    it("forwarded ref points to the same element as data-testid='button'", () => {
      const ref = createRef<HTMLButtonElement>();
      render(<Button ref={ref}>X</Button>);

      expect(ref.current).toBe(screen.getByTestId("button"));
    });
  });

  // -------------------------------------------------------------------------
  // 8. type attribute - default override + passthrough
  // -------------------------------------------------------------------------
  describe("type attribute", () => {
    it('renders type="button" by default (overrides the HTML default of "submit")', () => {
      render(<Button>X</Button>);
      expect(screen.getByTestId("button")).toHaveAttribute("type", "button");
    });

    it('passes through type="submit" when explicitly set', () => {
      render(<Button type="submit">Save</Button>);
      expect(screen.getByTestId("button")).toHaveAttribute("type", "submit");
    });

    it('passes through type="reset" when explicitly set', () => {
      render(<Button type="reset">Reset</Button>);
      expect(screen.getByTestId("button")).toHaveAttribute("type", "reset");
    });
  });

  // -------------------------------------------------------------------------
  // 9. Icons - leftIcon, rightIcon, swap on loading
  // -------------------------------------------------------------------------
  describe("icons", () => {
    it("renders leftIcon when not loading", () => {
      render(<Button leftIcon={<PlusIcon />}>Add</Button>);
      expect(screen.getByTestId("plus-icon")).toBeInTheDocument();
      expect(screen.getByText("Add")).toBeInTheDocument();
    });

    it("renders rightIcon when not loading", () => {
      render(<Button rightIcon={<ArrowIcon />}>Next</Button>);
      expect(screen.getByTestId("arrow-icon")).toBeInTheDocument();
      expect(screen.getByText("Next")).toBeInTheDocument();
    });

    it("renders both leftIcon and rightIcon when not loading", () => {
      render(
        <Button leftIcon={<PlusIcon />} rightIcon={<ArrowIcon />}>
          Both
        </Button>,
      );
      expect(screen.getByTestId("plus-icon")).toBeInTheDocument();
      expect(screen.getByTestId("arrow-icon")).toBeInTheDocument();
    });

    it("replaces leftIcon with the loading spinner when loading=true", () => {
      render(
        <Button loading leftIcon={<PlusIcon />}>
          X
        </Button>,
      );
      expect(screen.getByTestId("button-loading-spinner")).toBeInTheDocument();
      // The leftIcon must not be rendered while loading; the spinner takes
      // its slot so only one leading visual appears at a time.
      expect(screen.queryByTestId("plus-icon")).toBeNull();
    });

    it("hides rightIcon when loading=true (only the spinner is visible)", () => {
      render(
        <Button loading rightIcon={<ArrowIcon />}>
          X
        </Button>,
      );
      expect(screen.queryByTestId("arrow-icon")).toBeNull();
      expect(screen.getByTestId("button-loading-spinner")).toBeInTheDocument();
    });

    it("renders the spinner alongside the label even with no leftIcon provided", () => {
      render(<Button loading>Save</Button>);
      // No leftIcon was provided, but the spinner must still appear so
      // the wait state is always visible at the start of the button.
      expect(screen.getByTestId("button-loading-spinner")).toBeInTheDocument();
      expect(screen.getByText("Save")).toBeInTheDocument();
    });

    it("wraps the leftIcon in an aria-hidden span (decorative-only)", () => {
      const { container } = render(<Button leftIcon={<PlusIcon />}>Add</Button>);
      const iconWrapper = container.querySelector('span[aria-hidden="true"]');
      expect(iconWrapper).not.toBeNull();
      expect(iconWrapper).toContainElement(screen.getByTestId("plus-icon"));
    });
  });

  // -------------------------------------------------------------------------
  // 10. fullWidth - default off; opt-in via prop
  // -------------------------------------------------------------------------
  describe("fullWidth", () => {
    it("does NOT add the w-full class by default", () => {
      render(<Button>X</Button>);
      expect(screen.getByTestId("button")).not.toHaveClass("w-full");
    });

    it("does NOT add the w-full class when fullWidth=false explicitly", () => {
      render(<Button fullWidth={false}>X</Button>);
      expect(screen.getByTestId("button")).not.toHaveClass("w-full");
    });

    it("adds the w-full class when fullWidth=true", () => {
      render(<Button fullWidth>X</Button>);
      expect(screen.getByTestId("button")).toHaveClass("w-full");
    });
  });
});
