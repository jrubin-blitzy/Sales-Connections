/**
 * Select.tsx - Single-select dropdown primitive backed by the native
 * <select> element.
 *
 * Why <select> over a custom dropdown:
 *   - Native keyboard navigation (arrow keys, type-to-search).
 *   - Native mobile picker UI (iOS wheel, Android dropdown).
 *   - Built-in accessibility (no aria-* gymnastics required).
 *   - Zero JS focus-trap complexity.
 *   - Smaller bundle (no @radix-ui/react-select or similar).
 *
 * Trade-off: Native <select> options cannot be styled across browsers
 * (the option list appearance is determined by the OS/browser). For MVP,
 * this is acceptable because:
 *   - The dropdown content is text-only (no icons, no rich rendering).
 *   - Mobile users get a native picker that is more accessible.
 *   - Keyboard users get type-ahead and arrow keys for free.
 *
 * Capabilities:
 *   - Label tied to the select via htmlFor + id (auto-generated when the
 *     consumer does not supply one) so clicking the label focuses the
 *     select.
 *   - Helper text below the select, announced via aria-describedby.
 *   - Error message that REPLACES the helper text when present, with red
 *     border, role="alert", and aria-invalid=true on the select.
 *   - Optional placeholder rendered as a leading <option value="">.
 *     When `required` is also set, the placeholder option is `disabled`
 *     so the user cannot re-select the empty sentinel after picking a
 *     real value.
 *   - forwardRef so feature components can imperatively focus the
 *     select (e.g., autofocus the involvement select after relationship
 *     context entry, or focus the role dropdown after opening a user
 *     edit modal).
 *   - Custom Lucide-React ChevronDown overlay (the native browser arrow
 *     is hidden via Tailwind's `appearance-none`; the overlay caret is
 *     `pointer-events-none` so clicks fall through to the <select>).
 *   - Generic over the value type so consumers can pass a typed enum
 *     tuple without `as` assertions:
 *
 *       <Select<"Warm Intro" | "Soft Reference" | "Target Only">
 *         options={INVOLVEMENT_OPTIONS}
 *         value={form.involvement}
 *         onChange={(v) => setInvolvement(v)}
 *       />
 *
 * Used by:
 *   - frontend/src/features/connections/AddEditConnectionForm.tsx
 *     (F-001 involvement single-select; F-005 outreach_status on edit).
 *   - frontend/src/features/admin/UserManagement.tsx
 *     (F-009 / F-014 role dropdown: Admin / Contributor / Viewer).
 *   - frontend/src/features/connections/StatusChip.tsx
 *     (F-005 inline status mutation when role permits).
 *
 * Accessibility:
 *   - Native <select> + <label htmlFor> association (WCAG 1.3.1 / 4.1.2).
 *   - aria-invalid="true" when an error is present (WCAG 4.1.3 Status
 *     Messages); error message carries role="alert" so screen readers
 *     announce it on appearance.
 *   - aria-describedby points to the helper id (default) or error id
 *     (when an error is present) so the supporting text is part of the
 *     accessible name computation.
 *   - aria-required="true" forwarded so screen readers announce
 *     "required"; a visible red asterisk is also rendered next to the
 *     label as a sighted-user cue.
 *   - :focus-visible (not :focus) keyboard ring keeps mouse focus quiet
 *     while still providing a strong indicator for keyboard users.
 *
 * Conventions per AAP Sec 0.7.7:
 *   - TailwindCSS utility classes only (no inline styles).
 *   - Strict TypeScript; no `any`; explicit return type.
 *   - Lucide-React for the chevron icon.
 *   - clsx for conditional class composition.
 *   - Semantic HTML: native <select> with associated <label>.
 *   - forwardRef so feature components can imperatively focus.
 *   - Double quotes per project Prettier configuration (singleQuote: false).
 */

import {
  forwardRef,
  useId,
  type ChangeEvent,
  type ForwardedRef,
  type JSX,
  type ReactElement,
  type ReactNode,
  type SelectHTMLAttributes,
} from "react";
import { ChevronDown } from "lucide-react";
import clsx from "clsx";

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

/**
 * A single option in the dropdown.
 *
 * Generic over the value type (defaults to plain string) so an enum-typed
 * select does not require `as` casts. For example, when the involvement
 * indicator's three values come from a const tuple, the option array can
 * be typed as `ReadonlyArray<SelectOption<InvolvementValue>>` and the
 * compiler verifies that no stray strings sneak in.
 *
 * `readonly` is applied to every prop so consumers cannot accidentally
 * mutate the option object inside event handlers (defensive programming
 * convention shared with Button.tsx, Input.tsx, and Badge.tsx).
 */
export interface SelectOption<TValue extends string = string> {
  /** Display text shown to users in the option list. */
  readonly label: string;

  /**
   * Backing value submitted on form change. The native <option value>
   * attribute requires a string, hence the `extends string` constraint
   * on the generic.
   */
  readonly value: TValue;

  /**
   * When true, the option is rendered but not selectable. Useful for
   * "header" rows or temporarily-unavailable values.
   */
  readonly disabled?: boolean;
}

/**
 * Public props for the Select component.
 *
 * The native HTML attributes `value`, `onChange`, `size`, and `children`
 * are deliberately Omit-ted from the spread base because:
 *   - `value` is replaced with a strongly-typed `TValue | ""` union
 *     where `""` represents the placeholder / "no selection" sentinel.
 *   - `onChange` is replaced with a callback that receives the typed
 *     value directly, sparing every caller a `event.target.value as ...`
 *     cast at the call site.
 *   - `size` is the native column-count attribute (rarely used, conflicts
 *     with our visual sizing approach via Tailwind utilities); CSS
 *     controls width in modern apps.
 *   - `children` is replaced with the typed `options` array so consumers
 *     do not hand-roll <option> elements (this also enforces the typed-
 *     value invariant).
 *
 * `readonly` is applied to every prop so consumers cannot accidentally
 * mutate the props object inside event handlers (defensive programming).
 */
export interface SelectProps<TValue extends string = string>
  extends Omit<
    SelectHTMLAttributes<HTMLSelectElement>,
    "value" | "onChange" | "size" | "children"
  > {
  /**
   * Visible label text rendered above the select. When supplied, a
   * `<label htmlFor>` element is emitted and bound to the select id so
   * clicking the label focuses the select. Pass any ReactNode (string,
   * span with rich formatting, etc.).
   */
  readonly label?: ReactNode;

  /**
   * Helper text rendered below the select. Use for short hints (e.g.,
   * "Pick how you can help"). Replaced by `errorMessage` when the latter
   * is present so only one supporting paragraph is ever visible at a
   * time.
   */
  readonly helperText?: ReactNode;

  /**
   * Error message rendered below the select. When present, the select
   * gains a red border, aria-invalid="true", and the message paragraph
   * carries role="alert" so screen readers announce the change. Pass
   * `undefined` (the default) for the rest state.
   */
  readonly errorMessage?: ReactNode;

  /**
   * When true, sets aria-required on the select so screen readers
   * announce "required", AND a small red asterisk indicator is rendered
   * next to the label as a sighted-user cue. When a placeholder is also
   * provided, the placeholder option becomes `disabled` so the user
   * cannot re-select the empty sentinel after picking a real value.
   */
  readonly required?: boolean;

  /**
   * The list of selectable options. Order is preserved as supplied; the
   * primitive does not sort or dedupe. Use `ReadonlyArray` so consumers
   * may pass `as const` literals without TypeScript complaining about
   * mutability.
   */
  readonly options: ReadonlyArray<SelectOption<TValue>>;

  /**
   * Currently-selected value. Pass an empty string `""` to indicate
   * "no selection" (typically paired with a `placeholder`). When
   * undefined, the select is treated as uncontrolled by React; consumers
   * who want strict control should always supply this prop.
   */
  readonly value?: TValue | "";

  /**
   * Called when the user picks a different option. Receives the new
   * value (typed as TValue, or "" if the placeholder was selected and
   * `required` is not set).
   */
  readonly onChange?: (value: TValue) => void;

  /**
   * Optional placeholder text rendered as the first <option> with
   * value="" (empty string sentinel). When undefined, no placeholder
   * option is rendered. When `required` is also true, the placeholder
   * option is rendered with `disabled` so the user cannot re-select the
   * empty sentinel after picking a real value.
   */
  readonly placeholder?: string;

  /**
   * Optional className merged onto the wrapping <div>. Use to control
   * outer layout (width, margins, flex behavior) without forking the
   * primitive.
   */
  readonly className?: string;

  /**
   * Optional className merged onto the <select> itself. Use sparingly
   * for one-off visual tweaks that do not warrant a new variant; most
   * consumers should use `className` (outer) instead.
   */
  readonly selectClassName?: string;
}

// ---------------------------------------------------------------------------
// SelectInner - implementation function
//
// We define the implementation as a named generic function and then wrap
// it with `forwardRef` below. This is the standard React + TypeScript
// idiom for a generic component that also accepts a forwarded ref:
// `forwardRef` itself does not preserve generics in its return type, so
// we cast the wrapper's type to a function that DOES preserve the
// generic. The cast is safe at runtime because `forwardRef(SelectInner)`
// produces a forward-ref-aware component; the cast only changes how
// TypeScript represents its public signature.
// ---------------------------------------------------------------------------

function SelectInner<TValue extends string>(
  props: SelectProps<TValue>,
  ref: ForwardedRef<HTMLSelectElement>,
): JSX.Element {
  const {
    label,
    helperText,
    errorMessage,
    required,
    options,
    value,
    onChange,
    placeholder,
    className,
    selectClassName,
    id: propId,
    name,
    disabled,
    ...rest
  } = props;

  // -------------------------------------------------------------------------
  // Identifier resolution
  //
  // The label needs a stable id to bind to via htmlFor; if the consumer
  // did not supply one we generate a unique value with React's useId
  // (SSR-safe and stable across hydration). Suffixed ids let us point
  // aria-describedby at the helper or error paragraph deterministically.
  // -------------------------------------------------------------------------
  const generatedId = useId();
  const selectId = propId ?? `select-${generatedId}`;
  const helperId = `${selectId}-helper`;
  const errorId = `${selectId}-error`;
  const hasError = Boolean(errorMessage);

  // -------------------------------------------------------------------------
  // aria-describedby resolution
  //
  // Errors take priority over helper text (the user must see the error
  // message first). Only emit the attribute when there is something to
  // describe; an empty aria-describedby would be invalid markup.
  // -------------------------------------------------------------------------
  const describedBy: string | undefined = hasError
    ? errorId
    : helperText !== undefined && helperText !== null
      ? helperId
      : undefined;

  // -------------------------------------------------------------------------
  // Change handler
  //
  // Native ChangeEvent<HTMLSelectElement> fires `event.target.value` as
  // a plain string. The cast to TValue is safe because every <option>
  // we render carries a value typed as TValue (or "" when the placeholder
  // is selected and `required` is not set), so the runtime universe of
  // possible values exactly matches the typed universe.
  // -------------------------------------------------------------------------
  const handleChange = (event: ChangeEvent<HTMLSelectElement>): void => {
    if (onChange !== undefined) {
      onChange(event.target.value as TValue);
    }
  };

  // -------------------------------------------------------------------------
  // Placeholder-muted text color
  //
  // When the value is "" (or undefined and there is a placeholder), the
  // select is showing the placeholder text rather than a real selection.
  // Rendering the field text in a muted color matches the visual
  // convention established by Input.tsx's placeholder styling.
  // -------------------------------------------------------------------------
  const isShowingPlaceholder = value === undefined || value === "";

  return (
    <div className={clsx("flex flex-col gap-1.5", className)}>
      {label !== undefined && label !== null && (
        <label htmlFor={selectId} className="text-sm font-medium leading-5 text-slate-700">
          {label}
          {required && (
            <span aria-hidden="true" className="ml-0.5 text-red-600">
              *
            </span>
          )}
        </label>
      )}

      <div className="relative">
        <select
          ref={ref}
          id={selectId}
          name={name}
          value={value ?? ""}
          onChange={handleChange}
          disabled={disabled}
          aria-required={required ? true : undefined}
          aria-invalid={hasError ? true : undefined}
          aria-describedby={describedBy}
          className={clsx(
            // Base layout + typography
            "block w-full appearance-none rounded-md border bg-white",
            "px-3 py-2 pr-10 text-sm leading-5 text-slate-900 transition-colors",
            // Focus ring: keyboard-only via :focus-visible.
            "focus-visible:outline-2 focus-visible:outline-offset-2",
            "focus-visible:outline-brand-500",
            // Default border + hover affordance (only when not in error).
            !hasError && "border-slate-300 hover:border-slate-400",
            // Error border (red) takes precedence over the default.
            hasError && "border-red-400 hover:border-red-500",
            // Disabled visuals: dimmed background, not-allowed cursor.
            "disabled:cursor-not-allowed disabled:bg-slate-100",
            "disabled:text-slate-500",
            // Placeholder option styling: empty-string value renders as muted.
            isShowingPlaceholder && "text-slate-500",
            // Consumer override (last so it can win the cascade).
            selectClassName,
          )}
          data-testid="select"
          {...rest}
        >
          {placeholder !== undefined && (
            <option value="" disabled={required}>
              {placeholder}
            </option>
          )}
          {options.map((option) => (
            <option key={option.value} value={option.value} disabled={option.disabled}>
              {option.label}
            </option>
          ))}
        </select>

        {/*
          Chevron-down indicator (visual only).
          The native browser arrow is hidden via `appearance-none` on the
          <select> above (paired with `pr-10` to reserve space). This
          custom chevron provides a brand-consistent caret across all
          browsers and operating systems. `pointer-events-none` ensures
          clicks on the chevron fall through to the <select> and open the
          native picker rather than being absorbed by the overlay span.
         */}
        <span
          aria-hidden="true"
          className={clsx(
            "pointer-events-none absolute inset-y-0 right-3",
            "flex items-center text-slate-500",
          )}
          data-testid="select-chevron"
        >
          <ChevronDown className="h-4 w-4" />
        </span>
      </div>

      {hasError ? (
        <p
          id={errorId}
          role="alert"
          className="text-xs leading-4 text-red-600"
          data-testid="select-error"
        >
          {errorMessage}
        </p>
      ) : helperText !== undefined && helperText !== null ? (
        <p id={helperId} className="text-xs leading-4 text-slate-500" data-testid="select-helper">
          {helperText}
        </p>
      ) : null}
    </div>
  );
}

// React DevTools display name (forwardRef components need this set
// explicitly because the inner function name is otherwise shadowed by
// the forwardRef wrapper). Setting it on `SelectInner` before the
// `forwardRef` cast wraps the component ensures DevTools shows "Select"
// rather than the default "ForwardRef" label.
SelectInner.displayName = "Select";

/**
 * Tailwind-styled, accessible single-select dropdown primitive.
 *
 * Generic over the option value type so enum-typed selects do not need
 * `as` casts at the call site. The `forwardRef` wrapper preserves the
 * generic through the type assertion below.
 *
 * @example Basic
 *   <Select
 *     label="Role"
 *     options={[
 *       { label: "Admin", value: "Admin" },
 *       { label: "Contributor", value: "Contributor" },
 *       { label: "Viewer", value: "Viewer" },
 *     ]}
 *     value={role}
 *     onChange={setRole}
 *   />
 *
 * @example With placeholder + required
 *   <Select
 *     label="Involvement"
 *     placeholder="Choose an involvement level"
 *     required
 *     options={INVOLVEMENT_OPTIONS}
 *     value={involvement}
 *     onChange={setInvolvement}
 *   />
 *
 * @example With error
 *   <Select
 *     label="Outreach status"
 *     options={OUTREACH_OPTIONS}
 *     value={status}
 *     onChange={setStatus}
 *     errorMessage="Status is required."
 *   />
 *
 * @example Generic typing (no `as` needed)
 *   <Select<"Warm Intro" | "Soft Reference" | "Target Only">
 *     options={INVOLVEMENT_OPTIONS}
 *     onChange={(v) => {
 *       // v is typed as "Warm Intro" | "Soft Reference" | "Target Only"
 *     }}
 *   />
 *
 * @example Imperative focus from parent
 *   const ref = useRef<HTMLSelectElement>(null);
 *   useEffect(() => { ref.current?.focus(); }, []);
 *   return <Select ref={ref} label="Role" options={ROLE_OPTIONS} />;
 */
export const Select = forwardRef(SelectInner) as <TValue extends string = string>(
  props: SelectProps<TValue> & { ref?: ForwardedRef<HTMLSelectElement> },
) => ReactElement;
