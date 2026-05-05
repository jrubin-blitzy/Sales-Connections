/**
 * Select.test.tsx - Vitest tests for the Select UI primitive.
 *
 * Targets `frontend/src/components/ui/Select.tsx` - the generic-typed
 * single-select dropdown primitive backed by the native <select>
 * element. Used by the F-001 Add/Edit Connection form (involvement
 * single-select), the F-014 admin user-management role dropdown, and
 * the F-005 inline outreach-status mutation control.
 *
 * Coverage goals (>= 90% line per `Select.tsx`):
 *   - Renders all options.
 *   - Label association via htmlFor / id; explicit id prop honored;
 *     auto-generated id when prop absent.
 *   - onChange fires with the typed value (string default, generic
 *     enum union when explicitly parameterized).
 *   - Generic typing: <Select<"Admin" | "Contributor" | "Viewer"> ...>
 *     compiles and the onChange callback is typed to that union (the
 *     compile-time check is the actual generic-preservation proof).
 *   - Placeholder option rendered as the FIRST option with value="".
 *     Disabled when required=true; enabled when required=false.
 *   - Required: red asterisk visible next to label, aria-required="true"
 *     on the select, placeholder option flips to disabled.
 *   - errorMessage: aria-invalid="true", aria-describedby points to
 *     the error <p> id, error <p> carries role="alert".
 *   - helperText: aria-describedby points to the helper <p> id, no
 *     role="alert" on the helper paragraph; error wins over helper
 *     when both props are passed.
 *   - forwardRef: ref.current is an HTMLSelectElement and imperative
 *     focus moves document.activeElement.
 *   - appearance-none class on the select (so the native arrow is
 *     hidden in favor of the custom Lucide ChevronDown overlay).
 *   - ChevronDown overlay renders as an inline SVG inside the wrapping
 *     <span data-testid="select-chevron">.
 *   - Disabled select; disabled options.
 *
 * Conventions per AAP Sec 0.7.7 and frontend/.prettierrc.json:
 *   - Strict TypeScript; no `any`.
 *   - Double quotes; trailing commas; 2-space indent; line length <= 100.
 *   - Uses Vitest globals via test.globals: true (vitest.config.ts), but
 *     also imports describe/it/expect/vi explicitly for IDE type-awareness.
 *   - Uses @testing-library/react render and screen directly (Select is
 *     a pure presentational primitive; no provider stack needed).
 *   - Uses @testing-library/user-event for user interaction (NOT
 *     fireEvent) - more realistic event sequences.
 *   - Uses createRef from "react" for the forwardRef tests (useRef is a
 *     hook and only valid inside components).
 *   - No emoji; no console.log.
 *
 * Coordinates with:
 *   - frontend/src/components/ui/Select.tsx (system under test)
 *   - frontend/tests/setup.ts (registers @testing-library/jest-dom
 *     matchers globally so toBeInTheDocument, toHaveAttribute,
 *     toBeDisabled, toHaveFocus, toBeInstanceOf are available without
 *     per-file imports)
 *   - frontend/tsconfig.json (provides the @/ path alias)
 *   - frontend/vitest.config.ts (declares test.globals: true)
 */

import { createRef } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { Select, type SelectOption } from "@/components/ui/Select";

// ---------------------------------------------------------------------------
// Module-scoped test fixtures
// ---------------------------------------------------------------------------

/**
 * Typed-enum option list used by the generic-typing test. The union
 * literal "Admin" | "Contributor" | "Viewer" mirrors the project's
 * UserRole enum from F-009 RBAC. SelectOption<TValue> is parameterized
 * with this union so any stray string would fail to type-check.
 */
const ROLE_OPTIONS: ReadonlyArray<SelectOption<"Admin" | "Contributor" | "Viewer">> = [
  { label: "Administrator", value: "Admin" },
  { label: "Contributor", value: "Contributor" },
  { label: "Viewer (Sales Rep)", value: "Viewer" },
];

/**
 * Default-string-value option list used everywhere the generic-typing
 * pathway is irrelevant. SelectOption defaults TValue to plain string,
 * so omitting the type parameter produces an array of
 * SelectOption<string>.
 */
const SIMPLE_OPTIONS: ReadonlyArray<SelectOption> = [
  { label: "Apple", value: "apple" },
  { label: "Banana", value: "banana" },
  { label: "Cherry", value: "cherry" },
];

// ---------------------------------------------------------------------------
// Test suite
// ---------------------------------------------------------------------------

describe("<Select />", () => {
  // -------------------------------------------------------------------------
  // 1. Rendering options
  //
  // The source spec maps over `props.options` to emit one <option> per
  // entry, preserving order. getAllByRole("option") returns every
  // <option> in the document (native HTML elements have implicit
  // role="option"); the count and per-option text content are the
  // canonical assertions.
  // -------------------------------------------------------------------------
  describe("rendering options", () => {
    it("renders all options", () => {
      render(<Select options={SIMPLE_OPTIONS} onChange={vi.fn()} />);

      const options = screen.getAllByRole("option");
      // Three real options, no placeholder added because the
      // `placeholder` prop was not supplied.
      expect(options).toHaveLength(3);
      expect(options[0]).toHaveTextContent("Apple");
      expect(options[1]).toHaveTextContent("Banana");
      expect(options[2]).toHaveTextContent("Cherry");
    });

    it("renders no options when options array is empty", () => {
      render(<Select options={[]} onChange={vi.fn()} />);

      // No placeholder, no options -> queryAllByRole returns [].
      expect(screen.queryAllByRole("option")).toHaveLength(0);
    });
  });

  // -------------------------------------------------------------------------
  // 2. Label association
  //
  // The source emits <label htmlFor={selectId}> bound to <select id>.
  // getByLabelText walks that association, so a passing query proves
  // the binding is correct end-to-end.
  // -------------------------------------------------------------------------
  describe("label association", () => {
    it("renders <label> with htmlFor matching the select id", () => {
      render(<Select label="Role" options={SIMPLE_OPTIONS} onChange={vi.fn()} />);

      const select = screen.getByLabelText("Role");
      expect(select).toBeInTheDocument();
      // getByLabelText must return the <select> itself (the source spec
      // associates the label to the <select>, not to the wrapping div).
      expect(select.tagName).toBe("SELECT");
    });

    it("auto-generates a unique id when no id prop is provided", () => {
      render(
        <>
          <Select label="First" options={SIMPLE_OPTIONS} onChange={vi.fn()} />
          <Select label="Second" options={SIMPLE_OPTIONS} onChange={vi.fn()} />
        </>,
      );

      const firstSelect = screen.getByLabelText("First");
      const secondSelect = screen.getByLabelText("Second");

      // Both selects must have non-empty ids (for the label htmlFor
      // binding to work) and the ids must differ between instances.
      expect(firstSelect.id).not.toBe("");
      expect(secondSelect.id).not.toBe("");
      expect(firstSelect.id).not.toBe(secondSelect.id);
    });

    it("uses the provided id prop verbatim", () => {
      render(<Select id="my-select-id" label="Role" options={SIMPLE_OPTIONS} onChange={vi.fn()} />);

      const select = screen.getByLabelText("Role");
      expect(select.id).toBe("my-select-id");

      // The <label> element must point its htmlFor at the same id so
      // the assistive-tech association still works.
      const labelEl = screen.getByText("Role");
      expect(labelEl).toHaveAttribute("for", "my-select-id");
    });
  });

  // -------------------------------------------------------------------------
  // 3. onChange callback
  //
  // The source's handleChange casts event.target.value to TValue and
  // invokes onChange(typedValue). userEvent.selectOptions fires the
  // synthetic change event the way a real user would (mousedown +
  // click on the option, or keyboard navigation).
  // -------------------------------------------------------------------------
  describe("onChange callback", () => {
    it("fires onChange with the selected value (string type)", async () => {
      const user = userEvent.setup();
      const handleChange = vi.fn();
      render(<Select options={SIMPLE_OPTIONS} onChange={handleChange} value="apple" />);

      const select = screen.getByTestId("select");
      await user.selectOptions(select, "banana");

      expect(handleChange).toHaveBeenCalledTimes(1);
      expect(handleChange).toHaveBeenCalledWith("banana");
    });

    it("fires onChange when a different option is selected", async () => {
      const user = userEvent.setup();
      const handleChange = vi.fn();
      render(<Select options={SIMPLE_OPTIONS} onChange={handleChange} value="apple" />);

      const select = screen.getByTestId("select");
      await user.selectOptions(select, "cherry");

      expect(handleChange).toHaveBeenCalledWith("cherry");
    });
  });

  // -------------------------------------------------------------------------
  // 4. Generic typing
  //
  // The Select component is generic over TValue extends string. The
  // forwardRef wrapper preserves the generic via an inner function +
  // type cast. The strongest verification is at compile time: if the
  // generic is lost, the typed mock signature below would not be
  // assignable to the onChange prop and `tsc --noEmit` would fail.
  //
  // The runtime assertion is a secondary check that the value flows
  // through unchanged.
  // -------------------------------------------------------------------------
  describe("generic typing", () => {
    it("preserves the generic type in the onChange callback", async () => {
      type Role = "Admin" | "Contributor" | "Viewer";

      // Typed mock. A callsite that does not match the declared
      // (value: Role) => void signature would fail tsc. That is the
      // actual compile-time generic-preservation check.
      const handleChange = vi.fn<(value: Role) => void>();

      const user = userEvent.setup();
      render(
        <Select<Role> label="Role" options={ROLE_OPTIONS} onChange={handleChange} value="Admin" />,
      );

      const select = screen.getByTestId("select");
      await user.selectOptions(select, "Contributor");

      // Runtime confirmation that the typed value flows through.
      expect(handleChange).toHaveBeenCalledWith("Contributor");
    });
  });

  // -------------------------------------------------------------------------
  // 5. Placeholder option
  //
  // The source emits <option value="" disabled={required}>{placeholder}</option>
  // as the FIRST option whenever the placeholder prop is set. The
  // empty-string value is the project's "no selection" sentinel.
  // -------------------------------------------------------------------------
  describe("placeholder option", () => {
    it("renders the placeholder as the first option with empty value", () => {
      render(
        <Select placeholder="Choose a fruit..." options={SIMPLE_OPTIONS} onChange={vi.fn()} />,
      );

      const allOptions = screen.getAllByRole("option");
      // Placeholder + 3 real options = 4 total.
      expect(allOptions).toHaveLength(4);

      const placeholderOption = screen.getByRole("option", { name: "Choose a fruit..." });
      expect(placeholderOption).toHaveAttribute("value", "");
      // First option in the rendered <select>.
      expect(allOptions[0]).toBe(placeholderOption);
    });

    it("does NOT render placeholder option when placeholder prop is omitted", () => {
      render(<Select options={SIMPLE_OPTIONS} onChange={vi.fn()} />);

      // Without a placeholder, the option count equals the array length.
      expect(screen.getAllByRole("option")).toHaveLength(SIMPLE_OPTIONS.length);
      // No <option> with empty-string value exists.
      const allOptions = screen.getAllByRole("option");
      for (const option of allOptions) {
        expect(option.getAttribute("value")).not.toBe("");
      }
    });

    it("placeholder option is disabled when required=true", () => {
      render(
        <Select placeholder="Choose..." required options={SIMPLE_OPTIONS} onChange={vi.fn()} />,
      );

      const placeholderOption = screen.getByRole("option", { name: "Choose..." });
      expect(placeholderOption).toBeDisabled();
    });

    it("placeholder option is NOT disabled when required is omitted", () => {
      render(<Select placeholder="Choose..." options={SIMPLE_OPTIONS} onChange={vi.fn()} />);

      const placeholderOption = screen.getByRole("option", { name: "Choose..." });
      expect(placeholderOption).not.toBeDisabled();
    });
  });

  // -------------------------------------------------------------------------
  // 6. Required prop
  //
  // The source renders BOTH a sighted-user red asterisk (with
  // aria-hidden="true") AND aria-required="true" on the <select>.
  // The source uses `aria-required={required ? true : undefined}` so
  // when required is omitted the attribute is not present at all.
  // -------------------------------------------------------------------------
  describe("required prop", () => {
    it("renders the asterisk indicator next to the label when required=true", () => {
      render(<Select label="Role" required options={SIMPLE_OPTIONS} onChange={vi.fn()} />);

      const asterisk = screen.getByText("*");
      expect(asterisk).toBeInTheDocument();
      // Decorative-only: the assistive-tech announcement comes from
      // aria-required on the <select>.
      expect(asterisk).toHaveAttribute("aria-hidden", "true");
    });

    it('sets aria-required="true" on the select when required=true', () => {
      render(<Select label="Role" required options={SIMPLE_OPTIONS} onChange={vi.fn()} />);

      const select = screen.getByTestId("select");
      expect(select).toHaveAttribute("aria-required", "true");
    });

    it("does NOT set aria-required when required is omitted", () => {
      render(<Select label="Role" options={SIMPLE_OPTIONS} onChange={vi.fn()} />);

      const select = screen.getByTestId("select");
      // The source emits `aria-required={required ? true : undefined}`
      // so the attribute is omitted entirely in the rest state.
      expect(select).not.toHaveAttribute("aria-required", "true");
    });

    it("does NOT render asterisk when required is omitted", () => {
      render(<Select label="Role" options={SIMPLE_OPTIONS} onChange={vi.fn()} />);

      expect(screen.queryByText("*")).toBeNull();
    });
  });

  // -------------------------------------------------------------------------
  // 7. errorMessage prop
  //
  // The source flips three things when errorMessage is non-empty:
  //   1. aria-invalid="true" on the <select>
  //   2. aria-describedby points to the error <p>'s id
  //   3. The error <p> renders with role="alert" so screen readers
  //      announce the change immediately (WCAG 4.1.3 Status Messages).
  // -------------------------------------------------------------------------
  describe("errorMessage prop", () => {
    it('sets aria-invalid="true" on the select when errorMessage is present', () => {
      render(
        <Select
          label="Role"
          errorMessage="Role is required"
          options={SIMPLE_OPTIONS}
          onChange={vi.fn()}
        />,
      );

      const select = screen.getByTestId("select");
      expect(select).toHaveAttribute("aria-invalid", "true");
    });

    it('renders the error element with data-testid="select-error" and role="alert"', () => {
      render(
        <Select
          label="Role"
          errorMessage="Role is required"
          options={SIMPLE_OPTIONS}
          onChange={vi.fn()}
        />,
      );

      const errorEl = screen.getByTestId("select-error");
      expect(errorEl).toHaveAttribute("role", "alert");
      expect(errorEl).toHaveTextContent("Role is required");
    });

    it("aria-describedby points to the error element id", () => {
      render(
        <Select
          label="Role"
          errorMessage="Role is required"
          options={SIMPLE_OPTIONS}
          onChange={vi.fn()}
        />,
      );

      const select = screen.getByTestId("select");
      const errorEl = screen.getByTestId("select-error");

      const describedBy = select.getAttribute("aria-describedby");
      expect(describedBy).not.toBeNull();
      expect(describedBy).toBe(errorEl.id);
    });

    it("does NOT set aria-invalid when no errorMessage", () => {
      render(<Select label="Role" options={SIMPLE_OPTIONS} onChange={vi.fn()} />);

      const select = screen.getByTestId("select");
      // The source emits `aria-invalid={hasError ? true : undefined}`
      // so the attribute is omitted entirely in the rest state.
      expect(select).not.toHaveAttribute("aria-invalid", "true");
    });
  });

  // -------------------------------------------------------------------------
  // 8. helperText prop
  //
  // The helper text renders below the select in the rest state (no
  // error). aria-describedby points to its id so the supporting text
  // is part of the accessible name computation. role="alert" is NOT
  // applied because the helper is a passive hint, not a reactive
  // announcement.
  // -------------------------------------------------------------------------
  describe("helperText prop", () => {
    it('renders the helper element with data-testid="select-helper"', () => {
      render(
        <Select
          label="Role"
          helperText="Pick a role"
          options={SIMPLE_OPTIONS}
          onChange={vi.fn()}
        />,
      );

      const helperEl = screen.getByTestId("select-helper");
      expect(helperEl).toBeInTheDocument();
      expect(helperEl).toHaveTextContent("Pick a role");
    });

    it("aria-describedby points to helper element id", () => {
      render(
        <Select
          label="Role"
          helperText="Pick a role"
          options={SIMPLE_OPTIONS}
          onChange={vi.fn()}
        />,
      );

      const select = screen.getByTestId("select");
      const helperEl = screen.getByTestId("select-helper");

      const describedBy = select.getAttribute("aria-describedby");
      expect(describedBy).not.toBeNull();
      expect(describedBy).toBe(helperEl.id);
    });

    it("does NOT render helper when both error and helper are passed (error wins)", () => {
      render(
        <Select
          label="Role"
          errorMessage="Bad"
          helperText="Hint"
          options={SIMPLE_OPTIONS}
          onChange={vi.fn()}
        />,
      );

      // Error wins: helper is not rendered, error is.
      expect(screen.getByTestId("select-error")).toBeInTheDocument();
      expect(screen.queryByTestId("select-helper")).toBeNull();
    });
  });

  // -------------------------------------------------------------------------
  // 9. forwardRef - imperative focus
  //
  // The source wraps an inner generic function with React.forwardRef
  // so feature components (e.g., AddEditConnectionForm autofocus on
  // mount, focus role select after user-management modal opens) can
  // call ref.current.focus() imperatively. Verify both that the ref
  // is attached to the <select> element (not the wrapping <div>) and
  // that imperative focus actually moves document.activeElement.
  // -------------------------------------------------------------------------
  describe("forwardRef", () => {
    it("forwards ref to the underlying <select> element", () => {
      const ref = createRef<HTMLSelectElement>();
      render(<Select ref={ref} options={SIMPLE_OPTIONS} onChange={vi.fn()} />);

      expect(ref.current).not.toBeNull();
      expect(ref.current).toBeInstanceOf(HTMLSelectElement);
    });

    it("imperative focus via ref works", () => {
      const ref = createRef<HTMLSelectElement>();
      render(<Select ref={ref} options={SIMPLE_OPTIONS} onChange={vi.fn()} />);

      ref.current?.focus();

      expect(document.activeElement).toBe(ref.current);
    });
  });

  // -------------------------------------------------------------------------
  // 10. Appearance and chevron overlay
  //
  // The source adds the Tailwind `appearance-none` utility to the
  // <select> so the native browser arrow is suppressed, and renders
  // a custom Lucide ChevronDown icon (an inline SVG) inside a
  // `<span data-testid="select-chevron">` overlay positioned to the
  // right. The overlay is `pointer-events-none` so clicks on it fall
  // through to the <select>.
  // -------------------------------------------------------------------------
  describe("appearance and chevron", () => {
    it("applies appearance-none class to suppress the native arrow", () => {
      render(<Select options={SIMPLE_OPTIONS} onChange={vi.fn()} />);

      const select = screen.getByTestId("select");
      expect(select.className).toContain("appearance-none");
    });

    it("renders a ChevronDown icon overlay inside the chevron span", () => {
      const { container } = render(<Select options={SIMPLE_OPTIONS} onChange={vi.fn()} />);

      // The chevron span carries data-testid="select-chevron".
      const chevronSpan = screen.getByTestId("select-chevron");
      expect(chevronSpan).toBeInTheDocument();

      // The Lucide ChevronDown renders as an inline <svg>.
      const svg = container.querySelector("svg");
      expect(svg).toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // 11. Disabled state
  //
  // The source forwards the native `disabled` prop directly onto the
  // <select> (extracted from the props object before the {...rest}
  // spread). Per-option `disabled` flags pass through via the option
  // map: each <option disabled={option.disabled}>.
  // -------------------------------------------------------------------------
  describe("disabled state", () => {
    it("disables the select when disabled=true", () => {
      render(<Select disabled options={SIMPLE_OPTIONS} onChange={vi.fn()} />);

      expect(screen.getByTestId("select")).toBeDisabled();
    });

    it("select is enabled by default", () => {
      render(<Select options={SIMPLE_OPTIONS} onChange={vi.fn()} />);

      expect(screen.getByTestId("select")).not.toBeDisabled();
    });

    it("renders disabled options when option.disabled=true", () => {
      const OPTIONS_WITH_DISABLED: ReadonlyArray<SelectOption> = [
        { label: "Active", value: "a" },
        { label: "Unavailable", value: "u", disabled: true },
        { label: "Also Active", value: "b" },
      ];

      render(<Select options={OPTIONS_WITH_DISABLED} onChange={vi.fn()} />);

      const disabledOption = screen.getByRole("option", { name: "Unavailable" });
      expect(disabledOption).toBeDisabled();

      // Sibling options remain enabled.
      const activeOption = screen.getByRole("option", { name: "Active" });
      expect(activeOption).not.toBeDisabled();
    });
  });
});
