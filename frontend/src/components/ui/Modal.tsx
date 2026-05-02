/**
 * Modal.tsx - Centered modal/dialog primitive using the native <dialog> element.
 *
 * Why <dialog> over a custom div+role="dialog":
 *   - Built-in focus trap (browser keeps Tab navigation inside the modal).
 *   - Built-in Escape-to-close handling (fires the "cancel" event).
 *   - Built-in ::backdrop pseudo-element for the dim overlay.
 *   - WAI-ARIA dialog semantics handled by the user agent.
 *   - No external focus-trap library dependency.
 *
 * Open/close lifecycle:
 *   The component is a controlled primitive: parent owns `isOpen` and
 *   `onClose`. When `isOpen` flips to true, useEffect calls
 *   dialogRef.current.showModal(). When it flips to false, .close() is
 *   called. The Escape key triggers a "cancel" event which we intercept
 *   to call onClose() (preventing the default close so the parent stays
 *   the source of truth).
 *
 * Backdrop click:
 *   When `closeOnBackdropClick` is true (default), clicking outside the
 *   modal content (i.e., on the backdrop) calls onClose. Implementation
 *   detail: clicks on the dialog element itself (NOT a descendant) are
 *   the backdrop; we filter by event.target === dialogRef.current.
 *
 * Sizes:
 *   sm   max-w-sm  ~24rem (delete confirmation, role change)
 *   md   max-w-md  ~28rem (default)
 *   lg   max-w-lg  ~32rem (longer forms or context)
 *
 * Slots:
 *   children  Body content; the parent renders form fields, copy, etc.
 *   footer    Optional action row at the bottom (typically Cancel + Confirm
 *             buttons composed from the Button primitive).
 *
 * Conventions per AAP Sec 0.7.7:
 *   - TailwindCSS utility classes only (no inline styles).
 *   - Strict TypeScript; no `any`; explicit return types.
 *   - Lucide-React for the close X icon.
 *   - clsx for conditional class composition.
 *   - No business logic; pure presentational primitive.
 *   - Backdrop styling is global (frontend/src/styles/index.css).
 *   - Double quotes per project Prettier configuration (singleQuote: false).
 *
 * Coordinates with:
 *   - frontend/src/styles/index.css - provides the ::backdrop and base
 *     <dialog> styling (shadow-modal, border-slate-200, p-0, rounded-lg).
 *   - frontend/tailwind.config.ts - provides the shadow-modal token used
 *     by the global <dialog> rule and the brand-500 focus ring color.
 *   - frontend/src/components/ui/Button.tsx - sibling primitive composed
 *     by consumers in the `footer` slot.
 *   - frontend/src/features/connections/AddEditConnectionForm.tsx (F-007
 *     delete confirmation).
 *   - frontend/src/features/admin/UserManagement.tsx (F-014 role change
 *     confirmation).
 *   - frontend/tests/components/ui/Modal.test.tsx - verifies open/close,
 *     ARIA attributes, and event handling.
 */

import {
  useCallback,
  useEffect,
  useId,
  useRef,
  type JSX,
  type MouseEvent,
  type ReactNode,
  type SyntheticEvent,
} from "react";
import { X } from "lucide-react";
import clsx from "clsx";

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

/**
 * Modal size variants mapping to a `max-w-*` Tailwind utility.
 *
 * - "sm": max-w-sm (~24rem) - confirmation dialogs (delete record, role change).
 * - "md": max-w-md (~28rem) - default; balanced for short forms or copy.
 * - "lg": max-w-lg (~32rem) - longer forms or context-rich confirmations.
 */
export type ModalSize = "sm" | "md" | "lg";

/**
 * Public props for the Modal component.
 *
 * `readonly` is applied to every prop so consumers cannot accidentally
 * mutate the props object inside event handlers (defensive programming
 * convention shared with Button.tsx, Badge.tsx, Input.tsx, Select.tsx).
 */
export interface ModalProps {
  /** Controlled open state. */
  readonly isOpen: boolean;

  /** Called when the user dismisses the modal (X button, Escape, backdrop). */
  readonly onClose: () => void;

  /** Title rendered as <h2> in the header for accessibility. */
  readonly title: string;

  /** Optional supporting description rendered below the title. */
  readonly description?: string;

  /** Body content (forms, confirmation copy, etc.). */
  readonly children: ReactNode;

  /** Optional footer row (typically action buttons). */
  readonly footer?: ReactNode;

  /** Width sizing; defaults to "md". */
  readonly size?: ModalSize;

  /** When true (default), clicking the backdrop closes the modal. */
  readonly closeOnBackdropClick?: boolean;

  /**
   * When false, hides the X dismiss button (forces an action choice).
   * Defaults to true.
   */
  readonly showCloseButton?: boolean;

  /**
   * Optional className merged onto the <dialog> element for advanced
   * layout. Conflicting Tailwind classes are resolved by source order
   * (later classes win in the cascade).
   */
  readonly className?: string;

  /**
   * Optional aria-label override. By default the title is referenced
   * via aria-labelledby. Pass ariaLabel only when no visible title is
   * appropriate (rare; the `title` prop is required so a title always
   * exists in the DOM, but a consumer may want a different accessible
   * name for assistive technology).
   */
  readonly ariaLabel?: string;
}

// ---------------------------------------------------------------------------
// Size map
//
// This map is a module-level constant (not inlined) so:
//   1. The JIT Tailwind compiler picks up every utility at build time.
//   2. Tests can iterate over the keys to verify all sizes render.
//   3. Future variants (xl, full-screen) can be added in one place.
// ---------------------------------------------------------------------------

const SIZE_CLASSES: Record<ModalSize, string> = {
  sm: "max-w-sm",
  md: "max-w-md",
  lg: "max-w-lg",
};

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

/**
 * Returns a stable, namespaced id. We add a prefix so multiple Modal
 * instances on the same page (rare, but possible during tests or
 * stacked admin flows) produce distinguishable ids whose origin is
 * obvious in DevTools.
 *
 * @internal
 */
function useStableId(prefix: string): string {
  const id = useId();
  return `${prefix}-${id}`;
}

// ---------------------------------------------------------------------------
// Modal component
// ---------------------------------------------------------------------------

/**
 * Centered modal dialog. Renders the native <dialog> element; once
 * mounted, the parent toggles the dialog's open state by flipping the
 * controlled `isOpen` prop. The dialog stays in the React tree even
 * when closed so re-opens are instant.
 *
 * @example Delete confirmation (F-007)
 *   <Modal
 *     isOpen={isConfirming}
 *     onClose={() => setConfirming(false)}
 *     title="Delete connection"
 *     description="This will move the record to the trash."
 *     size="sm"
 *     footer={
 *       <>
 *         <Button variant="secondary" onClick={() => setConfirming(false)}>
 *           Cancel
 *         </Button>
 *         <Button variant="destructive" onClick={handleDelete}>
 *           Delete
 *         </Button>
 *       </>
 *     }
 *   >
 *     <p>Are you sure you want to delete this connection?</p>
 *   </Modal>
 *
 * @example Role change (F-014)
 *   <Modal
 *     isOpen={editingRole}
 *     onClose={cancelEdit}
 *     title="Change user role"
 *     size="sm"
 *     showCloseButton={false}
 *     closeOnBackdropClick={false}
 *     footer={...}
 *   >
 *     ...
 *   </Modal>
 */
export function Modal({
  isOpen,
  onClose,
  title,
  description,
  children,
  footer,
  size = "md",
  closeOnBackdropClick = true,
  showCloseButton = true,
  className,
  ariaLabel,
}: ModalProps): JSX.Element {
  const dialogRef = useRef<HTMLDialogElement>(null);

  // Imperatively open/close the native <dialog> when isOpen changes.
  // .showModal() is the modal variant: it renders the ::backdrop pseudo-
  // element, traps focus inside the dialog, and prevents user interaction
  // with content behind it. .show() is the non-modal variant and would
  // skip those behaviors, so we deliberately use .showModal().
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) {
      return;
    }
    if (isOpen) {
      if (!dialog.open) {
        dialog.showModal();
      }
    } else {
      if (dialog.open) {
        dialog.close();
      }
    }
  }, [isOpen]);

  // Intercept the platform's "cancel" event (fired on Escape) so the
  // parent stays the source of truth for the open state. Without
  // preventDefault() the browser would close the dialog directly,
  // bypassing the controlled `isOpen` prop and causing a brief
  // prop/state desync until the parent's next render.
  const handleCancel = useCallback(
    (event: SyntheticEvent<HTMLDialogElement, Event>) => {
      event.preventDefault();
      onClose();
    },
    [onClose],
  );

  // Distinguish backdrop clicks from descendant clicks. The native
  // <dialog> element does not produce a separate backdrop element in
  // the DOM (the backdrop is a CSS ::backdrop pseudo-element); however,
  // clicks on the visible backdrop area still target the <dialog>
  // element itself. Clicks on descendants (header, body, footer)
  // bubble up but their event.target is the descendant node. Therefore
  // event.target === dialogRef.current uniquely identifies a backdrop
  // click.
  const handleDialogClick = useCallback(
    (event: MouseEvent<HTMLDialogElement>) => {
      if (!closeOnBackdropClick) {
        return;
      }
      if (event.target === dialogRef.current) {
        onClose();
      }
    },
    [closeOnBackdropClick, onClose],
  );

  // Stable id pair used by aria-labelledby / aria-describedby. We
  // generate ids unconditionally so the rules of hooks are satisfied;
  // unused ids do not appear in the DOM because the corresponding
  // attribute is set to undefined when the consumer opts out.
  const titleId = useStableId("modal-title");
  const descriptionId = useStableId("modal-description");

  // When the consumer supplies an ariaLabel, use it as the accessible
  // name and skip aria-labelledby. Otherwise reference the visible
  // title via aria-labelledby so the title and announcement stay in
  // sync (the assistive technology speaks the literal heading text).
  const labelledBy = ariaLabel ? undefined : titleId;
  const describedBy = description ? descriptionId : undefined;

  return (
    <dialog
      ref={dialogRef}
      onCancel={handleCancel}
      onClick={handleDialogClick}
      aria-labelledby={labelledBy}
      aria-label={ariaLabel}
      aria-describedby={describedBy}
      className={clsx(
        // Reset native <dialog> defaults so Tailwind owns layout. The
        // global rule in src/styles/index.css applies p-0, rounded-lg,
        // shadow-modal, and the slate-200 border to every <dialog>; we
        // layer the size-specific max-width and a white background.
        "w-full bg-white text-slate-900",
        SIZE_CLASSES[size],
        className,
      )}
      data-testid="modal-dialog"
    >
      {/*
       * Inner padding wrapper. Because the global rule sets p-0 on
       * <dialog>, all spacing lives on this child <div> so the structural
       * component remains decoupled from the global base-layer styles.
       */}
      <div className="flex flex-col gap-4 p-6">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0 flex-1">
            <h2 id={titleId} className="text-lg font-semibold leading-6 text-slate-900">
              {title}
            </h2>
            {description !== undefined && description !== "" && (
              <p id={descriptionId} className="mt-1 text-sm leading-5 text-slate-600">
                {description}
              </p>
            )}
          </div>
          {showCloseButton && (
            <button
              type="button"
              onClick={onClose}
              aria-label="Close dialog"
              className={clsx(
                "inline-flex h-8 w-8 shrink-0 items-center justify-center rounded",
                "text-slate-500 transition-colors",
                "hover:bg-slate-100 hover:text-slate-700",
                "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-500",
              )}
              data-testid="modal-close-button"
            >
              <X aria-hidden="true" className="h-5 w-5" />
            </button>
          )}
        </div>

        <div className="text-sm leading-6 text-slate-700">{children}</div>

        {footer !== undefined && footer !== null && (
          <div className="flex flex-col-reverse gap-2 pt-2 sm:flex-row sm:justify-end">
            {footer}
          </div>
        )}
      </div>
    </dialog>
  );
}
