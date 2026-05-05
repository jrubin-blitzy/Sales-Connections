/**
 * Input.test.tsx - Vitest tests for the Input UI primitive.
 *
 * Targets `frontend/src/components/ui/Input.tsx` - the labeled
 * text-input primitive used by F-001 form fields (full_name,
 * linkedin_url, company, job_title), F-012 LoginScreen (email and
 * password), F-008 TagInput (token-style multi-tag entry), and
 * F-014 admin user-detail text fields.
 *
 * Coverage goals (>= 90% line per `Input.tsx`):
 *   - Default render with label, input, no error, no helper.
 *   - Label-input association via htmlFor / id.
 *   - Auto-generated unique id via React useId when no id prop.
 *   - Custom id prop honored.
 *   - Required asterisk rendering and native required attribute.
 *   - errorMessage: aria-invalid="true", aria-describedby, role="alert".
 *   - helperText: aria-describedby, no role="alert".
 *   - Error wins over helper (helper not rendered when both passed).
 *   - leftIcon and rightIcon slots with aria-hidden wrappers.
 *   - forwardRef works for imperative focus.
 *   - Native attributes (type, placeholder, autoComplete, disabled)
 *     pass through via {...rest} spread.
 *   - Type-ahead via userEvent.type works (uncontrolled input).
 *
 * Conventions per AAP Sec 0.7.7 and frontend/.prettierrc.json:
 *   - Strict TypeScript; explicit types preferred (eslint allows `any`
 *     in tests but the project still avoids it).
 *   - Double quotes; trailing commas; 2-space indent; line length <= 100.
 *   - Uses Vitest globals via test.globals: true (vitest.config.ts), but
 *     also imports describe/it/expect explicitly so editors and ESLint
 *     resolve them without ambient-type lookups.
 *   - Uses @testing-library/react render and screen directly (Input is
 *     a pure presentational primitive; no provider stack needed).
 *   - Uses @testing-library/user-event (NOT fireEvent) for click and
 *     type simulation - more realistic event sequences.
 *   - Uses createRef for forwardRef tests (useRef is a hook and only
 *     valid inside components).
 *   - No emoji; no console.log; no ASCII art.
 *
 * Coordinates with:
 *   - frontend/src/components/ui/Input.tsx (system under test)
 *   - frontend/tests/setup.ts (registers @testing-library/jest-dom
 *     matchers globally so toBeInTheDocument, toHaveAttribute,
 *     toBeRequired, toBeDisabled, toHaveValue, toHaveFocus, and
 *     toBeInstanceOf are available without per-file imports)
 *   - frontend/tsconfig.json (provides the @/ path alias)
 *   - frontend/vitest.config.ts (declares test.globals: true)
 */

import { createRef, type ReactElement } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { Input } from "@/components/ui/Input";

// ---------------------------------------------------------------------------
// Module-scoped test fixtures
// ---------------------------------------------------------------------------

/**
 * Exhaustive list of HTML input `type` values exercised by the
 * native-attribute-passthrough test. Each maps to a different mobile
 * keyboard layout, so testing all five exercises the {...rest} spread
 * propagation in `frontend/src/components/ui/Input.tsx`.
 *
 * The tuple is `as const` so TypeScript narrows the element type to
 * the exact string literal union (rather than widening to `string`),
 * which keeps the it.each row strongly typed when the value is fed
 * back into the JSX `<Input type={type} />` prop position.
 */
const TYPES = ["text", "email", "password", "url", "tel"] as const;

/**
 * Helper icon component used in the `leftIcon` test cases. Plain
 * ASCII label per project convention (no emoji, no fancy unicode).
 * Carries `data-testid="mail-svg"` so tests can locate the rendered
 * inner placeholder regardless of where the Input nests it inside
 * the wrapping `data-testid="input-left-icon"` span.
 */
function MailIcon(): ReactElement {
  return <span data-testid="mail-svg">M</span>;
}

/**
 * Helper icon component used in the `rightIcon` test cases. Plain
 * ASCII label per project convention. Carries
 * `data-testid="eye-svg"` mirroring the password-visibility toggle
 * use case enumerated in the source-file JSDoc for `rightIcon`.
 */
function EyeIcon(): ReactElement {
  return <span data-testid="eye-svg">E</span>;
}

// ---------------------------------------------------------------------------
// Test suite
// ---------------------------------------------------------------------------

describe("<Input />", () => {
  // -------------------------------------------------------------------------
  // 1. Rendering and label association
  //
  // The Input source (Sec 5.1 high-level architecture, accessibility
  // invariants) emits a <label htmlFor={inputId}> bound to <input id>.
  // getByLabelText walks that association, so a passing query proves
  // the binding is correct end-to-end.
  // -------------------------------------------------------------------------
  describe("rendering and label association", () => {
    it("renders label and input together", () => {
      render(<Input label="Email" />);
      const input = screen.getByLabelText("Email");
      expect(input).toBeInTheDocument();
      // getByLabelText must return the input itself (the source spec
      // associates the label to the <input>, not to a wrapping div).
      expect(input.tagName).toBe("INPUT");
    });

    it("clicking the label focuses the input", async () => {
      const user = userEvent.setup();
      render(<Input label="Email" />);

      // getByText returns the <label> element (NOT the input).
      // getByLabelText returns the bound <input>.
      const labelEl = screen.getByText("Email");
      await user.click(labelEl);

      const inputEl = screen.getByLabelText("Email");
      expect(inputEl).toHaveFocus();
    });

    it("renders without label when label prop is omitted", () => {
      render(<Input placeholder="No label" />);
      // Locate the input via its placeholder since no label exists.
      expect(screen.getByPlaceholderText("No label")).toBeInTheDocument();
      // No <label> element is in the document.
      expect(screen.queryByText("No label")).toBeNull();
    });
  });

  // -------------------------------------------------------------------------
  // 2. id generation
  //
  // The source uses React.useId() to mint a per-instance id when the
  // consumer does not supply one (`input-${useId}`). When the consumer
  // DOES supply one, the prop value is honored verbatim and propagated
  // to the <label htmlFor=...> binding.
  // -------------------------------------------------------------------------
  describe("id generation", () => {
    it("generates a unique id when no id prop is provided", () => {
      render(
        <>
          <Input label="First" />
          <Input label="Second" />
        </>,
      );

      const firstInput = screen.getByLabelText("First");
      const secondInput = screen.getByLabelText("Second");

      // Both inputs must have non-empty ids (for the label htmlFor
      // binding to work) and the ids must differ between instances.
      expect(firstInput.id).not.toBe("");
      expect(secondInput.id).not.toBe("");
      expect(firstInput.id).not.toBe(secondInput.id);
    });

    it("uses the provided id prop when set", () => {
      render(<Input id="my-custom-id" label="Test" />);

      const input = screen.getByLabelText("Test");
      expect(input.id).toBe("my-custom-id");

      // The <label> element must point its htmlFor at the same id so
      // the assistive-tech association still works.
      const labelEl = screen.getByText("Test");
      expect(labelEl).toHaveAttribute("for", "my-custom-id");
    });
  });

  // -------------------------------------------------------------------------
  // 3. Required prop
  //
  // The source renders BOTH a sighted-user red asterisk AND the
  // native HTML `required` attribute. The asterisk carries
  // aria-hidden="true" because the screen-reader announcement comes
  // from the `required` attribute itself; double-announcing would be
  // a WCAG 1.3.1 / 4.1.2 nuisance.
  // -------------------------------------------------------------------------
  describe("required prop", () => {
    it("renders the asterisk indicator next to the label when required=true", () => {
      render(<Input label="Name" required />);
      const asterisk = screen.getByText("*");
      expect(asterisk).toBeInTheDocument();
      // Decorative-only: the assistive-tech announcement comes from
      // the native `required` attribute on the <input>.
      expect(asterisk).toHaveAttribute("aria-hidden", "true");
    });

    it("sets the native required attribute when required=true", () => {
      render(<Input label="Name" required />);
      // Use a regex to skip the asterisk that getByLabelText("Name")
      // would also fail on cleanly. The label text is "Name*".
      const input = screen.getByLabelText(/name/i);
      expect(input).toBeRequired();
    });

    it("does NOT render asterisk when required is omitted", () => {
      render(<Input label="Name" />);
      expect(screen.queryByText("*")).toBeNull();
    });

    it("input is not required when required is omitted", () => {
      render(<Input label="Name" />);
      expect(screen.getByLabelText("Name")).not.toBeRequired();
    });
  });

  // -------------------------------------------------------------------------
  // 4. errorMessage prop
  //
  // The source flips three things when errorMessage is non-empty:
  //   1. aria-invalid="true" on the <input>
  //   2. aria-describedby points to the error <p>'s id
  //   3. The error <p> renders with role="alert" so screen readers
  //      announce the change immediately (WCAG 4.1.3 Status Messages).
  // -------------------------------------------------------------------------
  describe("errorMessage prop", () => {
    it('sets aria-invalid="true" on the input when errorMessage is present', () => {
      render(<Input label="Email" errorMessage="Required field" />);
      const input = screen.getByTestId("input");
      expect(input).toHaveAttribute("aria-invalid", "true");
    });

    it('renders the error element with data-testid="input-error" and role="alert"', () => {
      render(<Input label="Email" errorMessage="Required field" />);
      const errorEl = screen.getByTestId("input-error");
      expect(errorEl).toHaveAttribute("role", "alert");
      expect(errorEl).toHaveTextContent("Required field");
    });

    it("sets aria-describedby on the input pointing to the error element id", () => {
      render(<Input label="Email" errorMessage="Required field" />);
      const input = screen.getByTestId("input");
      const errorEl = screen.getByTestId("input-error");

      const describedBy = input.getAttribute("aria-describedby");
      expect(describedBy).not.toBeNull();
      expect(describedBy).toBe(errorEl.id);
    });

    it("does NOT set aria-invalid when no errorMessage", () => {
      render(<Input label="Email" />);
      const input = screen.getByTestId("input");
      // The source emits `aria-invalid={hasError ? true : undefined}`
      // so the attribute is omitted entirely in the rest state. The
      // negated matcher accepts both "the attribute is absent" and
      // "the attribute exists with a different value".
      expect(input).not.toHaveAttribute("aria-invalid", "true");
    });
  });

  // -------------------------------------------------------------------------
  // 5. helperText prop
  //
  // The helper text renders below the input in the rest state (no
  // error). aria-describedby points to its id so the supporting text
  // is part of the accessible name computation. role="alert" is NOT
  // applied because the helper is not a reactive announcement, just
  // a passive hint.
  // -------------------------------------------------------------------------
  describe("helperText prop", () => {
    it('renders the helper element with data-testid="input-helper"', () => {
      render(<Input label="Email" helperText="We never share your email" />);
      const helperEl = screen.getByTestId("input-helper");
      expect(helperEl).toBeInTheDocument();
      expect(helperEl).toHaveTextContent("We never share your email");
    });

    it('does NOT set role="alert" on the helper element', () => {
      render(<Input label="Email" helperText="Hint" />);
      const helperEl = screen.getByTestId("input-helper");
      expect(helperEl).not.toHaveAttribute("role", "alert");
    });

    it("sets aria-describedby on the input pointing to the helper element id", () => {
      render(<Input label="Email" helperText="Hint" />);
      const input = screen.getByTestId("input");
      const helperEl = screen.getByTestId("input-helper");

      const describedBy = input.getAttribute("aria-describedby");
      expect(describedBy).not.toBeNull();
      expect(describedBy).toBe(helperEl.id);
    });

    it("does NOT render helper when helperText is omitted", () => {
      render(<Input label="Email" />);
      expect(screen.queryByTestId("input-helper")).toBeNull();
    });
  });

  // -------------------------------------------------------------------------
  // 6. error-vs-helper precedence
  //
  // The source shows ONLY the error when both props are passed:
  //   - The helper <p> is not rendered.
  //   - aria-describedby points to the error id (not the helper id).
  // This invariant matters because the user must see the actionable
  // error message, not a now-irrelevant format hint.
  // -------------------------------------------------------------------------
  describe("error-vs-helper precedence", () => {
    it("renders error and NOT helper when both are passed", () => {
      render(<Input label="Email" errorMessage="Bad email" helperText="Format hint" />);
      expect(screen.getByTestId("input-error")).toBeInTheDocument();
      expect(screen.queryByTestId("input-helper")).toBeNull();
    });

    it("aria-describedby points to error id (not helper id) when both are passed", () => {
      render(<Input label="Email" errorMessage="Bad email" helperText="Format hint" />);
      const input = screen.getByTestId("input");
      const errorEl = screen.getByTestId("input-error");

      expect(input.getAttribute("aria-describedby")).toBe(errorEl.id);
    });
  });

  // -------------------------------------------------------------------------
  // 7. Icons - leftIcon and rightIcon slots
  //
  // Both slots wrap the consumer-supplied node in a span carrying
  // aria-hidden="true" and data-testid="input-{left,right}-icon".
  // The wrapper also carries pointer-events-none so clicks pass
  // through to the input itself; that styling concern is verified at
  // the visual review level, not in this unit test.
  // -------------------------------------------------------------------------
  describe("icons", () => {
    it('renders leftIcon via data-testid="input-left-icon"', () => {
      render(<Input label="Email" leftIcon={<MailIcon />} />);
      // Outer wrapper from the source spec:
      const wrapper = screen.getByTestId("input-left-icon");
      expect(wrapper).toBeInTheDocument();
      // Inner consumer-supplied node propagated through the slot:
      expect(screen.getByTestId("mail-svg")).toBeInTheDocument();
    });

    it('renders rightIcon via data-testid="input-right-icon"', () => {
      render(<Input label="Pass" rightIcon={<EyeIcon />} />);
      const wrapper = screen.getByTestId("input-right-icon");
      expect(wrapper).toBeInTheDocument();
      expect(screen.getByTestId("eye-svg")).toBeInTheDocument();
    });

    it("does NOT render input-left-icon when leftIcon prop is omitted", () => {
      render(<Input label="X" />);
      expect(screen.queryByTestId("input-left-icon")).toBeNull();
    });

    it("does NOT render input-right-icon when rightIcon prop is omitted", () => {
      render(<Input label="X" />);
      expect(screen.queryByTestId("input-right-icon")).toBeNull();
    });
  });

  // -------------------------------------------------------------------------
  // 8. forwardRef - imperative focus
  //
  // The source wraps the inner function with React.forwardRef so
  // feature components (e.g., AddEditConnectionForm autofocus on
  // mount, focus next field after AI note generation) can call
  // ref.current.focus() imperatively. Verify both that the ref is
  // attached to an HTMLInputElement (not the wrapping <div>) and
  // that imperative focus actually moves document.activeElement.
  // -------------------------------------------------------------------------
  describe("forwardRef", () => {
    it("forwards ref to the underlying <input> element", () => {
      const ref = createRef<HTMLInputElement>();
      render(<Input ref={ref} label="X" />);

      expect(ref.current).not.toBeNull();
      expect(ref.current).toBeInstanceOf(HTMLInputElement);
    });

    it("imperative focus via ref works", () => {
      const ref = createRef<HTMLInputElement>();
      render(<Input ref={ref} label="X" />);

      ref.current?.focus();

      expect(document.activeElement).toBe(ref.current);
    });
  });

  // -------------------------------------------------------------------------
  // 9. Native attribute passthrough
  //
  // The source spreads {...rest} onto the underlying <input> after
  // explicitly extracting only the props it interprets. Verify that
  // common HTML attributes (type for keyboard layout selection,
  // placeholder, autoComplete for password-manager UX, disabled) all
  // make it through.
  // -------------------------------------------------------------------------
  describe("native attribute passthrough", () => {
    it.each(TYPES)("passes through type=%s", (type) => {
      render(<Input label="X" type={type} />);
      const input = screen.getByTestId("input");
      expect(input).toHaveAttribute("type", type);
    });

    it("passes through placeholder attribute", () => {
      render(<Input label="X" placeholder="jane@example.com" />);
      expect(screen.getByTestId("input")).toHaveAttribute("placeholder", "jane@example.com");
    });

    it("passes through autoComplete attribute", () => {
      // The React prop is camelCase autoComplete; the rendered HTML
      // attribute is lowercase autocomplete. jest-dom toHaveAttribute
      // matches against the lowercase rendered name.
      render(<Input label="X" autoComplete="email" />);
      expect(screen.getByTestId("input")).toHaveAttribute("autocomplete", "email");
    });

    it("disables the input when disabled=true", () => {
      render(<Input label="X" disabled />);
      expect(screen.getByTestId("input")).toBeDisabled();
    });

    it("input is enabled by default", () => {
      render(<Input label="X" />);
      expect(screen.getByTestId("input")).not.toBeDisabled();
    });
  });

  // -------------------------------------------------------------------------
  // 10. User interaction - typing
  //
  // userEvent.type drives realistic key-by-key input including the
  // browser's native value-update side effect. The "user can clear
  // and retype" case exercises the ground-state transition that is
  // actually used in the F-001 form (when the user re-edits a field
  // after a validation error).
  // -------------------------------------------------------------------------
  describe("user interaction", () => {
    it("user can type into the input (uncontrolled)", async () => {
      const user = userEvent.setup();
      render(<Input label="Name" />);

      const input = screen.getByLabelText("Name");
      await user.type(input, "hello world");

      expect(input).toHaveValue("hello world");
    });

    it("user can clear and retype", async () => {
      const user = userEvent.setup();
      render(<Input label="Name" />);

      const input = screen.getByLabelText("Name");
      await user.type(input, "first");
      await user.clear(input);
      await user.type(input, "second");

      expect(input).toHaveValue("second");
    });
  });
});
