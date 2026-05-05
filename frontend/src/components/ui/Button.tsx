/**
 * Button.tsx - Tailwind-styled <button> primitive used everywhere in the
 * Sales-Connections SPA (every feature component composes Button for its
 * primary, secondary, and destructive actions).
 *
 * Variants:
 *   primary      Filled brand-color action; use for the dominant call-to-
 *                action (Submit, Save, Generate AI Notes).
 *   secondary    Bordered neutral background; use for secondary actions
 *                (Cancel, Reset).
 *   destructive  Filled red action; use for delete / destructive actions
 *                (Delete record, Hard delete).
 *   ghost        Transparent background; use for tertiary actions
 *                (e.g., toolbar icon buttons).
 *   link         Text-only with underline on hover; use to mimic an <a> in
 *                form contexts where a real <a> would not navigate.
 *
 * Sizes:
 *   sm   h-8   px-3   text-xs   (compact, table-row actions)
 *   md   h-10  px-4   text-sm   (default; form submit, header actions)
 *   lg   h-12  px-6   text-base (primary CTA on landing-style screens)
 *
 * Loading state:
 *   - When loading=true the button is disabled and a Lucide Loader2 spinner
 *     replaces the leftIcon (or prefixes the label if no leftIcon).
 *   - The button label remains visible so the action stays clear.
 *   - aria-busy="true" is set so screen readers announce the wait state.
 *   - The onClick handler is short-circuited as a defense-in-depth check
 *     even though `disabled` already prevents activation.
 *
 * Required type attribute:
 *   - The native HTML default for <button> is type="submit", which causes
 *     surprise form submissions when devs forget to specify it. This
 *     component defaults to type="button" so consumers must explicitly opt
 *     into "submit" or "reset".
 *
 * forwardRef:
 *   - Feature components (e.g., AddEditConnectionForm) need to imperatively
 *     focus the Submit button after AI note generation completes. The
 *     forwarded ref points at the underlying HTMLButtonElement.
 *
 * Conventions per AAP Sec 0.7.7:
 *   - TailwindCSS utility classes only (no inline styles).
 *   - Strict TypeScript; no `any`; explicit return type.
 *   - Lucide-React for the loading spinner icon.
 *   - clsx for conditional class composition.
 *   - Semantic <button>; explicit `type` attribute always present.
 *   - Accessible focus-visible ring rendered consistently across variants.
 *
 * Coordinates with:
 *   - frontend/src/features/connections/AddEditConnectionForm.tsx (F-001)
 *   - frontend/src/features/connections/ConnectionFeed.tsx (F-004 row actions)
 *   - frontend/src/features/connections/ConnectionDetail.tsx (F-011)
 *   - frontend/src/features/auth/LoginScreen.tsx (F-012)
 *   - frontend/src/features/admin/* (F-014)
 *   - frontend/src/components/ui/Modal.tsx (footer slot consumes Button)
 *   - frontend/tailwind.config.ts (brand-{300,500,600,700,800} tokens)
 */

import { forwardRef, type ButtonHTMLAttributes, type ReactElement, type ReactNode } from "react";
import { Loader2 } from "lucide-react";
import clsx from "clsx";

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

/**
 * Visual variant of the button.
 *
 * - "primary": filled brand-color action (dominant CTA).
 * - "secondary": bordered neutral action (Cancel, Reset).
 * - "destructive": filled red action (delete, hard delete).
 * - "ghost": transparent action (tertiary / toolbar).
 * - "link": text-only with underline (anchor-like in form contexts).
 */
export type ButtonVariant = "primary" | "secondary" | "destructive" | "ghost" | "link";

/**
 * Visual size of the button.
 *
 * - "sm": 32px tall (h-8) - compact, table-row actions.
 * - "md": 40px tall (h-10) - default form submit and header actions.
 * - "lg": 48px tall (h-12) - primary CTA on landing-style screens.
 */
export type ButtonSize = "sm" | "md" | "lg";

/**
 * Public props for the Button component.
 *
 * The native HTML `type` attribute is deliberately Omit-ted from the
 * spread base so this component can default it to "button" (the HTML
 * default of "submit" causes accidental form submissions). The consumer
 * may explicitly pass type="submit" or type="reset" to opt in.
 *
 * `readonly` is applied to every prop so consumers cannot accidentally
 * mutate the props object inside event handlers (defensive programming).
 */
export interface ButtonProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "type"> {
  /** Visual variant. Defaults to "primary". */
  readonly variant?: ButtonVariant;

  /** Visual size. Defaults to "md". */
  readonly size?: ButtonSize;

  /**
   * Loading state. When true the button is disabled, a Loader2 spinner
   * replaces the leftIcon (or appears at the start if no leftIcon was
   * provided), aria-busy="true" is set, and the onClick handler is
   * short-circuited. The label text remains visible.
   */
  readonly loading?: boolean;

  /**
   * Optional leading icon node. Replaced by the loading spinner when
   * loading=true. Wrapped in a span with aria-hidden so the icon is
   * decorative and does not pollute the accessible name.
   */
  readonly leftIcon?: ReactNode;

  /**
   * Optional trailing icon node. Hidden when loading=true (so only one
   * icon - the spinner - is visible during the wait state).
   */
  readonly rightIcon?: ReactNode;

  /** When true, the button stretches to fill its parent's width via w-full. */
  readonly fullWidth?: boolean;

  /**
   * Native button type. Defaults to "button" to prevent accidental form
   * submits. Pass "submit" for form-submit buttons; "reset" for form
   * reset buttons.
   */
  readonly type?: "button" | "submit" | "reset";
}

// ---------------------------------------------------------------------------
// Variant + size class maps
//
// These maps are module-level constants (not inlined) so:
//   1. The JIT Tailwind compiler picks up every utility at build time.
//   2. Tests / Storybook / future variants can iterate over the keys.
//   3. The VARIANT_CLASSES strings are visible in one place for design review.
// ---------------------------------------------------------------------------

const VARIANT_CLASSES: Record<ButtonVariant, string> = {
  primary:
    "bg-brand-600 text-white border border-transparent " +
    "hover:bg-brand-700 active:bg-brand-800 " +
    "disabled:bg-brand-300 disabled:hover:bg-brand-300 " +
    "shadow-sm",
  secondary:
    "bg-white text-slate-700 border border-slate-300 " +
    "hover:bg-slate-50 hover:border-slate-400 active:bg-slate-100 " +
    "disabled:bg-slate-50 disabled:text-slate-400 disabled:border-slate-200 " +
    "shadow-sm",
  destructive:
    "bg-red-600 text-white border border-transparent " +
    "hover:bg-red-700 active:bg-red-800 " +
    "disabled:bg-red-300 disabled:hover:bg-red-300 " +
    "shadow-sm",
  ghost:
    "bg-transparent text-slate-700 border border-transparent " +
    "hover:bg-slate-100 active:bg-slate-200 " +
    "disabled:bg-transparent disabled:text-slate-400",
  link:
    "bg-transparent text-brand-600 border border-transparent " +
    "hover:text-brand-700 hover:underline underline-offset-2 active:text-brand-800 " +
    "disabled:text-slate-400 disabled:no-underline " +
    "shadow-none px-0",
};

/**
 * Size class map.
 *
 * Per Visual Consistency QA Issue 6 every button MUST hit the
 * 44x44 px touch-target floor on mobile (< sm breakpoint). The
 * `min-h-[44px]` utility applies on every viewport but is paired
 * with the natural ``h-8`` / ``h-10`` / ``h-12`` height utilities;
 * since `min-height` always wins over `height` when the natural
 * height is smaller, the button reads 44 px tall at mobile and the
 * stated `h-*` height (e.g., 40 px for `md`) at desktop because we
 * release the floor via `sm:min-h-0`.
 *
 * The width floor is unnecessary because button labels typically
 * carry enough text to exceed 44 px on their own; constraining the
 * width would prevent fluid icon-only buttons (e.g., toolbar `X`
 * dismissal) from staying compact.
 */
const SIZE_CLASSES: Record<ButtonSize, { container: string; icon: string }> = {
  sm: {
    container: "min-h-[44px] sm:min-h-0 h-8 px-3 text-xs gap-1.5 rounded-md",
    icon: "h-3.5 w-3.5",
  },
  md: {
    container: "min-h-[44px] sm:min-h-0 h-10 px-4 text-sm gap-2 rounded-md",
    icon: "h-4 w-4",
  },
  lg: {
    container: "h-12 px-6 text-base gap-2 rounded-md",
    icon: "h-5 w-5",
  },
};

// ---------------------------------------------------------------------------
// Button component
// ---------------------------------------------------------------------------

/**
 * Tailwind-styled, accessible button primitive.
 *
 * @example
 *   // Primary submit button with loading state
 *   <Button variant="primary" size="md" loading={isPending} type="submit">
 *     Save Connection
 *   </Button>
 *
 * @example
 *   // Secondary action with leading icon
 *   <Button variant="secondary" leftIcon={<Plus aria-hidden="true" />}>
 *     Add Connection
 *   </Button>
 *
 * @example
 *   // Destructive action (e.g., delete confirmation in a Modal)
 *   <Button variant="destructive" onClick={handleDelete}>
 *     Delete record
 *   </Button>
 */
export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  {
    variant = "primary",
    size = "md",
    loading = false,
    leftIcon,
    rightIcon,
    fullWidth = false,
    type = "button",
    disabled,
    className,
    children,
    onClick,
    ...rest
  },
  ref,
): ReactElement {
  const sizeClasses = SIZE_CLASSES[size];
  const variantClasses = VARIANT_CLASSES[variant];

  // The button is functionally disabled if either the consumer-supplied
  // `disabled` prop is true OR the component is in the loading state.
  // This is enforced by both the native `disabled` attribute below AND
  // an onClick short-circuit (defense-in-depth).
  const isDisabled = disabled === true || loading;

  // When loading, swap the leftIcon for the Loader2 spinner. If no
  // leftIcon was provided, the spinner still appears (so the wait state
  // is always visible at the start of the button).
  //
  // Per Visual Consistency QA Issue 4: Lucide-React icons render as
  // <svg> with intrinsic ``width="24" height="24"`` attributes, which
  // would overflow a span sized via ``h-4 w-4`` (the wrapper relies on
  // CSS dimensions only). The ``[&>svg]:h-full [&>svg]:w-full`` rule
  // forces any direct <svg> child to fill the wrapper exactly, so the
  // rendered glyph matches both the visual size and the DOM size
  // attributes regardless of what the consumer passes.
  const renderedLeftIcon: ReactNode = loading ? (
    <Loader2
      aria-hidden="true"
      className={clsx("animate-spin", sizeClasses.icon)}
      data-testid="button-loading-spinner"
    />
  ) : leftIcon !== undefined && leftIcon !== null ? (
    <span
      aria-hidden="true"
      className={clsx("inline-flex shrink-0 [&>svg]:h-full [&>svg]:w-full", sizeClasses.icon)}
    >
      {leftIcon}
    </span>
  ) : null;

  // Trailing icon is hidden during loading (so only one icon - the spinner -
  // is visible at a time). Same SVG-sizing arrangement as leftIcon.
  const renderedRightIcon: ReactNode =
    !loading && rightIcon !== undefined && rightIcon !== null ? (
      <span
        aria-hidden="true"
        className={clsx("inline-flex shrink-0 [&>svg]:h-full [&>svg]:w-full", sizeClasses.icon)}
      >
        {rightIcon}
      </span>
    ) : null;

  return (
    <button
      ref={ref}
      type={type}
      disabled={isDisabled}
      aria-busy={loading ? true : undefined}
      onClick={(event) => {
        // Defense-in-depth: even if `disabled` is somehow ignored or
        // overridden, clicks during a loading or disabled state are
        // rejected at the JS layer.
        if (isDisabled) {
          return;
        }
        onClick?.(event);
      }}
      className={clsx(
        // Base layout + typography
        "inline-flex items-center justify-center font-medium leading-none whitespace-nowrap",
        // Animate background / border / text color changes only (avoid
        // accidentally animating layout properties like width/padding).
        "transition-colors",
        // Focus ring: keyboard-only via :focus-visible, consistent across variants.
        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-500",
        // Disabled cursor (the disabled attribute itself blocks pointer events).
        "disabled:cursor-not-allowed",
        // Full-width modifier (used when the button is the sole CTA in a
        // narrow column or in a mobile-stacked form).
        fullWidth && "w-full",
        // Size + variant + consumer-supplied className (consumer wins on
        // tail of the className list per clsx semantics).
        sizeClasses.container,
        variantClasses,
        className,
      )}
      data-testid="button"
      data-variant={variant}
      data-size={size}
      data-loading={loading ? "true" : undefined}
      {...rest}
    >
      {renderedLeftIcon}
      {children}
      {renderedRightIcon}
    </button>
  );
});

// React DevTools display name (forwardRef components need this set
// explicitly because the inner function name is otherwise shadowed by
// the forwardRef wrapper).
Button.displayName = "Button";
