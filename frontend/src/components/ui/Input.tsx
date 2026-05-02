/**
 * Input.tsx - Labeled text-input primitive backed by the native <input> element.
 *
 * Why <input> over a custom div:
 *   - Native keyboard, IME, paste, undo, and redo behavior.
 *   - Native autofill, password-manager integration, and type-specific
 *     mobile keyboards (email, url, tel, password).
 *   - WAI-ARIA semantics handled by the user agent.
 *   - Zero focus-management complexity (no contentEditable hacks).
 *
 * Capabilities:
 *   - Label tied to the input via htmlFor + id (auto-generated when the
 *     consumer does not supply one) so clicking the label focuses the input.
 *   - Helper text below the input, announced via aria-describedby.
 *   - Error message that REPLACES the helper text when present, with red
 *     border, role="alert", and aria-invalid=true on the input.
 *   - Optional leading and trailing icon slots (composable with Lucide-React).
 *     The wrapper applies pointer-events-none so clicks pass through to the
 *     input itself, and pl-10 / pr-10 padding modifiers reserve space for
 *     the icons without overlap.
 *   - forwardRef so feature components can imperatively focus the input
 *     (e.g., autofocus on form mount, or focusing the next field after AI
 *     note generation completes per F-002).
 *   - All native <input> attributes (placeholder, autoComplete, value,
 *     onChange, type, etc.) pass through via spread.
 *
 * Used by:
 *   - frontend/src/features/connections/AddEditConnectionForm.tsx (F-001)
 *     for full_name, linkedin_url, company, and job_title fields.
 *   - frontend/src/features/auth/LoginScreen.tsx (F-012) for email and
 *     password fields.
 *   - frontend/src/features/connections/TagInput.tsx (F-008) wraps Input
 *     for token-style multi-tag entry.
 *   - frontend/src/features/admin/UserManagement.tsx (F-014) for any
 *     admin user-detail text fields.
 *
 * Accessibility:
 *   - Native <input> + <label htmlFor> association (WCAG 1.3.1 / 4.1.2).
 *   - aria-invalid="true" when an error is present (WCAG 4.1.3 Status
 *     Messages); error message carries role="alert" so screen readers
 *     announce it on appearance.
 *   - aria-describedby points to the helper id (default) or error id
 *     (when an error is present) so the supporting text is part of the
 *     accessible name computation.
 *   - The native HTML `required` attribute is forwarded so screen
 *     readers announce "required"; a visible red asterisk is also
 *     rendered next to the label as a sighted-user cue.
 *   - :focus-visible (not :focus) keyboard ring keeps mouse focus quiet
 *     while still providing a strong indicator for keyboard users.
 *
 * Conventions per AAP Sec 0.7.7:
 *   - TailwindCSS utility classes only (no inline styles).
 *   - Strict TypeScript; no `any`; explicit return type.
 *   - clsx for conditional class composition.
 *   - Semantic HTML: native <input> with associated <label>.
 *   - forwardRef so feature components can imperatively focus.
 *   - Double quotes per project Prettier configuration (singleQuote: false).
 */

import { forwardRef, useId, type InputHTMLAttributes, type JSX, type ReactNode } from "react";
import clsx from "clsx";

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

/**
 * Visual size variant of the input.
 *
 * - "sm": 32px tall (h-8) - compact, dense table or admin form.
 * - "md": 40px tall (h-10) - default; matches Button "md" rhythm so an
 *   inline pair (input + submit) aligns vertically.
 * - "lg": 48px tall (h-12) - emphasis surfaces (e.g., a single-field
 *   landing-style search bar).
 */
export type InputSize = "sm" | "md" | "lg";

/**
 * Public props for the Input component.
 *
 * The native HTML `size` attribute is deliberately Omit-ted from the
 * spread base because:
 *   1. The native attribute is a numeric column count that almost no one
 *      uses in modern apps (CSS controls width).
 *   2. Its `number` type would conflict with our `inputSize` visual
 *      variant prop's string union.
 * Consumers who genuinely need the native column-count behavior can
 * cast on the way in or use a custom `style`-equivalent override; this
 * is an explicit opt-out, not an accidental one.
 *
 * `readonly` is applied to every prop so consumers cannot accidentally
 * mutate the props object inside event handlers (defensive programming
 * convention shared with Button.tsx and Badge.tsx).
 */
export interface InputProps extends Omit<InputHTMLAttributes<HTMLInputElement>, "size"> {
  /**
   * Visible label text rendered above the input. When supplied, a
   * `<label htmlFor>` element is emitted and bound to the input id so
   * clicking the label focuses the input. Pass any ReactNode (string,
   * span with rich formatting, etc.).
   */
  readonly label?: ReactNode;

  /**
   * Helper text rendered below the input. Use for format hints (e.g.,
   * "Must start with https://www.linkedin.com/in/"). Replaced by
   * `errorMessage` when the latter is present so only one supporting
   * paragraph is ever visible at a time.
   */
  readonly helperText?: ReactNode;

  /**
   * Error message rendered below the input. When present, the input
   * gains a red border, aria-invalid="true", and the message paragraph
   * carries role="alert" so screen readers announce the change. Pass
   * `undefined` (the default) for the rest state.
   */
  readonly errorMessage?: ReactNode;

  /**
   * When true, the native HTML `required` attribute is forwarded to
   * the input (so screen readers announce "required" and the browser
   * blocks empty submissions in HTML-form contexts), AND a small red
   * asterisk indicator is rendered next to the label as a sighted-user
   * cue.
   */
  readonly required?: boolean;

  /** Visual size variant. Defaults to "md". */
  readonly inputSize?: InputSize;

  /**
   * Optional leading icon node, rendered inside the input on the left.
   * The wrapping span carries pointer-events-none so clicks pass
   * through to the input (otherwise the icon would steal focus from
   * the field). The container padding-left increases to pl-10 to
   * reserve space.
   */
  readonly leftIcon?: ReactNode;

  /**
   * Optional trailing icon node, rendered inside the input on the
   * right. Same pointer-events-none + reserved padding pattern as
   * leftIcon. Common consumers: a clear-text X button, a password
   * visibility toggle (eye), or a validation status (check / x-circle).
   */
  readonly rightIcon?: ReactNode;

  /**
   * Optional className merged onto the wrapping <div>. Use to control
   * outer layout (width, margins, flex behavior) without forking the
   * primitive.
   */
  readonly className?: string;

  /**
   * Optional className merged onto the <input> itself. Use sparingly
   * for one-off visual tweaks that do not warrant a new variant; most
   * consumers should use `className` (outer) instead.
   */
  readonly inputClassName?: string;
}

// ---------------------------------------------------------------------------
// Size class map
//
// A module-level constant (not inlined) so:
//   1. The JIT Tailwind compiler picks up every utility at build time.
//   2. Tests / future variants can iterate over the keys.
//   3. The class strings sit in one place for design review.
// ---------------------------------------------------------------------------

const SIZE_CLASSES: Record<InputSize, string> = {
  sm: "h-8 text-xs",
  md: "h-10 text-sm",
  lg: "h-12 text-base",
};

// ---------------------------------------------------------------------------
// Input component
// ---------------------------------------------------------------------------

/**
 * Tailwind-styled, accessible labeled text-input primitive.
 *
 * @example Basic
 *   <Input label="Email" type="email" placeholder="jane@example.com" />
 *
 * @example With helper text
 *   <Input
 *     label="LinkedIn URL"
 *     type="url"
 *     helperText="Must start with https://www.linkedin.com/in/"
 *   />
 *
 * @example With error
 *   <Input
 *     label="Email"
 *     type="email"
 *     errorMessage="Email is required."
 *     required
 *   />
 *
 * @example With leading icon (Lucide)
 *   <Input
 *     label="Search"
 *     leftIcon={<Search aria-hidden="true" className="h-4 w-4" />}
 *     placeholder="Filter connections..."
 *   />
 *
 * @example Imperative focus from parent
 *   const ref = useRef<HTMLInputElement>(null);
 *   useEffect(() => { ref.current?.focus(); }, []);
 *   return <Input ref={ref} label="Full name" name="full_name" />;
 */
export const Input = forwardRef<HTMLInputElement, InputProps>(function Input(
  {
    label,
    helperText,
    errorMessage,
    required,
    inputSize = "md",
    leftIcon,
    rightIcon,
    className,
    inputClassName,
    id: propId,
    name,
    type = "text",
    disabled,
    ...rest
  },
  ref,
): JSX.Element {
  // -------------------------------------------------------------------------
  // Identifier resolution
  //
  // The label needs a stable id to bind to via htmlFor; if the consumer
  // did not supply one we generate a unique value with React's useId
  // (SSR-safe and stable across hydration). Suffixed ids let us point
  // aria-describedby at the helper or error paragraph deterministically.
  // -------------------------------------------------------------------------
  const generatedId = useId();
  const inputId = propId ?? `input-${generatedId}`;
  const helperId = `${inputId}-helper`;
  const errorId = `${inputId}-error`;
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

  return (
    <div className={clsx("flex flex-col gap-1.5", className)}>
      {label !== undefined && label !== null && (
        <label htmlFor={inputId} className="text-sm font-medium leading-5 text-slate-700">
          {label}
          {required && (
            <span aria-hidden="true" className="ml-0.5 text-red-600">
              *
            </span>
          )}
        </label>
      )}

      <div className="relative">
        {leftIcon !== undefined && leftIcon !== null && (
          <span
            aria-hidden="true"
            className={clsx(
              "pointer-events-none absolute inset-y-0 left-3",
              "flex items-center text-slate-400",
            )}
            data-testid="input-left-icon"
          >
            {leftIcon}
          </span>
        )}

        <input
          ref={ref}
          id={inputId}
          name={name}
          type={type}
          disabled={disabled}
          required={required}
          aria-invalid={hasError ? true : undefined}
          aria-describedby={describedBy}
          className={clsx(
            // Base layout + typography
            "block w-full rounded-md border bg-white leading-5 text-slate-900",
            "transition-colors placeholder:text-slate-400",
            // Default padding (icon-aware overrides below).
            "px-3 py-2",
            // Reserve extra padding on the side that has an icon so the
            // text never collides with the absolutely positioned glyph.
            leftIcon !== undefined && leftIcon !== null && "pl-10",
            rightIcon !== undefined && rightIcon !== null && "pr-10",
            // Visual size (height + base font size).
            SIZE_CLASSES[inputSize],
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
            // Consumer override (last so it can win the cascade).
            inputClassName,
          )}
          data-testid="input"
          {...rest}
        />

        {rightIcon !== undefined && rightIcon !== null && (
          <span
            aria-hidden="true"
            className={clsx(
              "pointer-events-none absolute inset-y-0 right-3",
              "flex items-center text-slate-400",
            )}
            data-testid="input-right-icon"
          >
            {rightIcon}
          </span>
        )}
      </div>

      {hasError ? (
        <p
          id={errorId}
          role="alert"
          className="text-xs leading-4 text-red-600"
          data-testid="input-error"
        >
          {errorMessage}
        </p>
      ) : helperText !== undefined && helperText !== null ? (
        <p id={helperId} className="text-xs leading-4 text-slate-500" data-testid="input-helper">
          {helperText}
        </p>
      ) : null}
    </div>
  );
});

// React DevTools display name (forwardRef components need this set
// explicitly because the inner function name is otherwise shadowed by
// the forwardRef wrapper).
Input.displayName = "Input";
