/**
 * Textarea.tsx - Tailwind-styled, accessible labeled multi-line text
 * input primitive.
 *
 * Mirrors the API of @/components/ui/Input.tsx but renders a native
 * <textarea> element instead of <input>. Used by the
 * AddEditConnectionForm AI/outreach notes field (Visual Consistency
 * QA Issue 9): per AAP F-002 the AI-generated outreach notes are
 * "multi-paragraph outreach talking points" that need a multi-line
 * editor; the previous single-line <Input> implementation made the
 * text cramped and hard to edit.
 *
 * Differences from Input:
 *   - Wraps a <textarea>, not an <input>. The native control supports
 *     vertical resize via the rendered ``resize-y`` utility.
 *   - No leftIcon / rightIcon affordances: a multi-line region with
 *     an absolutely-positioned glyph would clash with line wrapping
 *     and resize behavior. Form labels and helper text remain.
 *   - The visual height is controlled via ``rows`` (default 4) and
 *     ``resize`` ('vertical' is the default; consumers may pass
 *     'none' / 'both' / 'horizontal' for special cases).
 *
 * Variants:
 *   - resize: "vertical" (default) | "none" | "both" | "horizontal"
 *
 * Accessibility:
 *   - Bound <label htmlFor> for every labeled instance.
 *   - aria-invalid="true" + role="alert" on the error paragraph
 *     when errorMessage is supplied.
 *   - aria-describedby resolves to the error or helper paragraph
 *     id (errors take priority).
 *   - required attribute forwards to the native control.
 *
 * Conventions per AAP Sec 0.7.7:
 *   - TailwindCSS utility classes only (no inline styles).
 *   - clsx for conditional class composition.
 *   - Strict TypeScript; no `any`; explicit JSX.Element return.
 *   - Named export only (no default export).
 *   - All-ASCII characters in source.
 *
 * Coordinates with:
 *   - frontend/src/features/connections/AddEditConnectionForm.tsx
 *     (consumes Textarea for the ai_notes field).
 *   - frontend/src/components/ui/Input.tsx (sister primitive; same
 *     label / helper / error visual conventions).
 */

import { forwardRef, useId, type JSX, type ReactNode, type TextareaHTMLAttributes } from "react";
import clsx from "clsx";

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

/**
 * Native CSS resize values the textarea may expose. Defaults to
 * "vertical" because horizontal resize tends to break the surrounding
 * layout grid and "both" is rarely user-intended.
 */
export type TextareaResize = "vertical" | "none" | "both" | "horizontal";

/**
 * Public props for the Textarea component.
 *
 * `readonly` is applied to every prop so consumers cannot accidentally
 * mutate the props object inside event handlers (defensive programming
 * convention shared with Button.tsx, Input.tsx, Badge.tsx).
 */
export interface TextareaProps extends TextareaHTMLAttributes<HTMLTextAreaElement> {
  /**
   * Visible label text rendered above the textarea. When supplied, a
   * `<label htmlFor>` element is emitted and bound to the textarea id
   * so clicking the label focuses the control.
   */
  readonly label?: ReactNode;

  /**
   * Helper text rendered below the textarea. Use for format hints
   * (e.g., "Optional. Edit AI suggestions or write your own.").
   * Replaced by errorMessage when the latter is present so only one
   * supporting paragraph is visible at a time.
   */
  readonly helperText?: ReactNode;

  /**
   * Error message rendered below the textarea. When present, the
   * textarea gains a red border, aria-invalid="true", and the message
   * paragraph carries role="alert" so screen readers announce the
   * change.
   */
  readonly errorMessage?: ReactNode;

  /**
   * When true, the native HTML `required` attribute is forwarded to
   * the textarea (so screen readers announce "required" and the
   * browser blocks empty submissions in HTML-form contexts), AND a
   * small red asterisk indicator is rendered next to the label as a
   * sighted-user cue.
   */
  readonly required?: boolean;

  /**
   * CSS resize value. Defaults to "vertical" so the textarea grows
   * downward for long content; "none" disables resizing entirely.
   */
  readonly resize?: TextareaResize;

  /**
   * Optional className merged onto the wrapping <div>. Use to control
   * outer layout (width, margins, flex behavior) without forking the
   * primitive.
   */
  readonly className?: string;

  /**
   * Optional className merged onto the <textarea> itself. Use sparingly
   * for one-off visual tweaks; most consumers should use `className`
   * (outer) instead.
   */
  readonly textareaClassName?: string;
}

// ---------------------------------------------------------------------------
// Resize class map
// ---------------------------------------------------------------------------

const RESIZE_CLASSES: Record<TextareaResize, string> = {
  vertical: "resize-y",
  none: "resize-none",
  both: "resize",
  horizontal: "resize-x",
};

// ---------------------------------------------------------------------------
// Textarea component
// ---------------------------------------------------------------------------

/**
 * Tailwind-styled, accessible labeled multi-line input primitive.
 *
 * @example Basic
 *   <Textarea label="Notes" rows={4} />
 *
 * @example With helper text
 *   <Textarea
 *     label="AI / outreach notes"
 *     helperText="Optional. Edit AI suggestions or write your own."
 *     rows={5}
 *   />
 *
 * @example With error
 *   <Textarea
 *     label="Relationship context"
 *     errorMessage="Required field."
 *     required
 *   />
 *
 * @example Imperative focus from parent
 *   const ref = useRef<HTMLTextAreaElement>(null);
 *   useEffect(() => { ref.current?.focus(); }, []);
 *   return <Textarea ref={ref} label="Notes" name="notes" />;
 */
export const Textarea = forwardRef<HTMLTextAreaElement, TextareaProps>(function Textarea(
  {
    label,
    helperText,
    errorMessage,
    required,
    resize = "vertical",
    className,
    textareaClassName,
    id: propId,
    name,
    rows = 4,
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
  const textareaId = propId ?? `textarea-${generatedId}`;
  const helperId = `${textareaId}-helper`;
  const errorId = `${textareaId}-error`;
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
        <label htmlFor={textareaId} className="text-sm font-medium leading-5 text-slate-700">
          {label}
          {required && (
            <span aria-hidden="true" className="ml-0.5 text-red-600">
              *
            </span>
          )}
        </label>
      )}

      <textarea
        ref={ref}
        id={textareaId}
        name={name}
        rows={rows}
        disabled={disabled}
        required={required}
        aria-invalid={hasError ? true : undefined}
        aria-describedby={describedBy}
        className={clsx(
          // Base layout + typography
          "block w-full rounded-md border bg-white text-sm leading-5 text-slate-900",
          "transition-colors placeholder:text-slate-400",
          // Default padding (no icon affordance like Input).
          "px-3 py-2",
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
          // Resize behavior.
          RESIZE_CLASSES[resize],
          // Consumer override (last so it can win the cascade).
          textareaClassName,
        )}
        data-testid="textarea"
        {...rest}
      />

      {hasError ? (
        <p
          id={errorId}
          role="alert"
          className="text-xs leading-4 text-red-600"
          data-testid="textarea-error"
        >
          {errorMessage}
        </p>
      ) : helperText !== undefined && helperText !== null ? (
        <p id={helperId} className="text-xs leading-4 text-slate-500" data-testid="textarea-helper">
          {helperText}
        </p>
      ) : null}
    </div>
  );
});

// React DevTools display name (forwardRef components need this set
// explicitly because the inner function name is otherwise shadowed by
// the forwardRef wrapper).
Textarea.displayName = "Textarea";
