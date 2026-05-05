/**
 * Toast.tsx - Transient notification primitive for the Sales-Connections SPA.
 *
 * Exports:
 *   - ToastContainer  React component mounted in App.tsx via createPortal.
 *                     Subscribes to the module-level toast store and renders
 *                     active toasts as a fixed-position stack.
 *   - useToast()      React hook returning helpers to fire/dismiss toasts.
 *                     Can be called from any component, regardless of
 *                     provider hierarchy.
 *   - __resetToastStoreForTesting  Test-only escape hatch.
 *   - ToastVariant / Toast / ToastInput / UseToastReturn  TypeScript types.
 *
 * Architecture (module-level store, NOT React Context):
 *   The store is a module-level Set of listeners + an array of active
 *   toasts. useToast() dispatches to the store; ToastContainer subscribes
 *   to it. This decoupling lets the container be a SIBLING of the route
 *   tree (not an ancestor), avoiding context-propagation gymnastics.
 *
 *   App.tsx renders <RouterProvider /> and <ToastContainer /> as siblings
 *   inside the error boundary. A React Context would have to wrap both,
 *   which is impossible without restructuring the tree. The module-level
 *   singleton store sidesteps the issue entirely (similar in spirit to
 *   react-hot-toast, but written from scratch per the AAP scope-discipline
 *   rule that forbids adding undeclared dependencies).
 *
 * Variants (Tailwind class mappings cross-checked against tailwind.config.ts):
 *   success  emerald-tinted; lucide CheckCircle2 icon
 *   error    red-tinted; lucide XCircle icon
 *   info     brand-tinted; lucide Info icon
 *   warning  amber-tinted; lucide AlertTriangle icon
 *
 * Behavior:
 *   - Auto-dismiss after `durationMs` (default 5000 ms; pass 0 to disable).
 *   - Manual dismiss via the close X button on each toast.
 *   - Stack capped at MAX_VISIBLE_TOASTS; older overflow toasts are dropped
 *     from the visible slice (newest are always shown).
 *   - Each toast has a unique id; dismiss(id) removes it from the queue.
 *
 * Portal mount strategy:
 *   createPortal renders into document.body so toasts overlay every route,
 *   modal, and other overlay. Mounting in document.body (not into the
 *   #root container) keeps them visually on top and topologically outside
 *   the route subtree, so they persist across navigation transitions.
 *
 * Accessibility:
 *   - The container region has role="region" aria-label="Notifications".
 *   - Each toast: role="status" for success/info/warning; role="alert" for
 *     error variants (so screen readers announce errors more urgently per
 *     WCAG 4.1.3 - "alert" is reserved for time-sensitive content).
 *   - aria-live="polite" on the region.
 *   - The dismiss button is a real <button type="button"> with an
 *     aria-label for screen readers; the X icon has aria-hidden="true".
 *   - :focus-visible ring rendered consistently with other UI primitives.
 *
 * Conventions per AAP Sec 0.7.7:
 *   - TailwindCSS utility classes only (no inline styles).
 *   - Strict TypeScript; no `any`; explicit JSX.Element return types.
 *   - Lucide-React for icons (CheckCircle2, XCircle, Info, AlertTriangle, X).
 *   - clsx for conditional class composition - never string concatenation.
 *   - Double quotes per project Prettier configuration (singleQuote: false).
 *   - No business logic; pure presentational primitive.
 *
 * Coordinates with:
 *   - frontend/src/App.tsx - mounts <ToastContainer /> as sibling of
 *     <RouterProvider /> inside the error boundary.
 *   - frontend/src/styles/index.css - provides global :focus-visible ring
 *     styling for the dismiss button.
 *   - frontend/tailwind.config.ts - provides brand-* color tokens used by
 *     the info variant; emerald/red/amber are Tailwind defaults; shadow-card
 *     is in theme.extend.boxShadow.
 *   - All feature components - call useToast() to fire toasts on mutation
 *     success/error.
 *   - frontend/tests/components/ui/Toast.test.tsx - verifies render,
 *     auto-dismiss, role assignment, and the test-reset helper.
 */

/*
 * react-refresh/only-export-components is intentionally disabled for this
 * file: the AAP requires <ToastContainer /> (a React component) and
 * useToast() / __resetToastStoreForTesting (utility functions) to be
 * co-located in this single primitive. Splitting them into separate
 * files would violate AAP Sec 0.6.1 ("only create files explicitly
 * mentioned in AAP"). HMR fast-refresh works correctly in practice
 * because the container's render tree is decoupled from the helpers
 * via the module-level store.
 */
/* eslint-disable react-refresh/only-export-components */

import { useCallback, useEffect, useMemo, useRef, useState, type JSX, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { AlertTriangle, CheckCircle2, Info, X, XCircle, type LucideIcon } from "lucide-react";
import clsx from "clsx";

// ---------------------------------------------------------------------------
// Module-level constants
// ---------------------------------------------------------------------------

/**
 * Default auto-dismiss duration in milliseconds. Tuned to give a reader
 * enough time to scan a short success/error message without lingering.
 */
const DEFAULT_TOAST_DURATION_MS = 5000;

/**
 * Maximum number of toasts to render simultaneously. Older overflow
 * toasts remain in the queue but are not rendered until earlier ones
 * dismiss. Caps the visible stack so a runaway dispatch loop cannot
 * fill the viewport.
 */
const MAX_VISIBLE_TOASTS = 5;

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

/**
 * Visual + semantic variant of a toast. Each variant maps to:
 *   - a Lucide icon (VARIANT_ICON)
 *   - a Tailwind container palette (VARIANT_CONTAINER_CLASSES)
 *   - a Tailwind icon color (VARIANT_ICON_CLASSES)
 *   - an ARIA role (VARIANT_ROLE: "alert" for error, "status" otherwise)
 */
export type ToastVariant = "success" | "error" | "info" | "warning";

/**
 * Stored shape of a toast in the queue. Created internally by
 * dispatchToast(); exposed publicly so tests and consumers can inspect
 * the active toast list shape if needed.
 *
 * `title` and `message` are typed as ReactNode so consumers can pass rich
 * content (e.g., a <strong> emphasis) when needed; the convenience
 * helpers (success/error/info/warning) accept plain strings and rely on
 * string-to-ReactNode assignability.
 *
 * Every property is `readonly` so snapshots emitted to listeners cannot
 * be accidentally mutated.
 */
export interface Toast {
  /** Unique id used for dismissal and React keys. */
  readonly id: string;
  /** Visual variant. */
  readonly variant: ToastVariant;
  /** Optional bold title rendered above the message. */
  readonly title?: ReactNode;
  /** Required body content. */
  readonly message: ReactNode;
  /** Auto-dismiss duration in ms. 0 disables auto-dismiss. */
  readonly durationMs: number;
}

/**
 * Input shape accepted by useToast().show(). Mirrors Toast but omits the
 * generated id and lets `durationMs` be optional (it defaults to
 * DEFAULT_TOAST_DURATION_MS when omitted).
 */
export interface ToastInput {
  /** Visual variant. */
  variant: ToastVariant;
  /** Optional bold title rendered above the message. */
  title?: ReactNode;
  /** Required body content. */
  message: ReactNode;
  /**
   * Auto-dismiss duration in ms. Defaults to DEFAULT_TOAST_DURATION_MS.
   * Pass 0 to disable auto-dismiss (toast stays until manually closed).
   */
  durationMs?: number;
}

/**
 * Return shape of the useToast() hook.
 *
 * `show` returns the dispatched toast id so callers can later call
 * dismiss(id) - useful for "Working..." spinners that should disappear
 * once the parent has the result.
 */
export interface UseToastReturn {
  /** Generic dispatch; returns the toast id. */
  show: (input: ToastInput) => string;
  /** Convenience for variant="success". Returns the toast id. */
  success: (message: string, title?: string) => string;
  /** Convenience for variant="error". Returns the toast id. */
  error: (message: string, title?: string) => string;
  /** Convenience for variant="info". Returns the toast id. */
  info: (message: string, title?: string) => string;
  /** Convenience for variant="warning". Returns the toast id. */
  warning: (message: string, title?: string) => string;
  /** Manually dismiss by id. No-op if the id is not in the queue. */
  dismiss: (id: string) => void;
}

// ---------------------------------------------------------------------------
// Module-level singleton store
//
// The store is intentionally module-scoped (not React Context) so that
// useToast() can be called from any descendant of <RouterProvider /> and
// <ToastContainer /> can subscribe independently from a sibling position
// in the React tree. See the file header for the architectural rationale.
// ---------------------------------------------------------------------------

/**
 * Listener callback signature. ToastContainer registers exactly one
 * listener which forwards the snapshot into a useState setter.
 */
type ToastListener = (toasts: ReadonlyArray<Toast>) => void;

/**
 * Active toast queue. Newest toast is at the END of the array so the
 * `slice(-MAX_VISIBLE_TOASTS)` strategy in ToastContainer naturally
 * keeps the most recent toasts visible.
 *
 * Reassigned (not mutated) on every change so React's referential
 * equality short-circuit triggers correctly when the snapshot is set
 * via useState.
 */
let toastQueue: ReadonlyArray<Toast> = [];

/**
 * Set of subscribed listener callbacks. Typically contains exactly one
 * entry (ToastContainer's setState). A Set is used instead of an array
 * so add/remove operations are O(1) and the same listener cannot be
 * registered twice by accident.
 */
const listeners = new Set<ToastListener>();

/** Notify all subscribers with the current queue snapshot. */
function emitChange(): void {
  for (const listener of listeners) {
    listener(toastQueue);
  }
}

/**
 * Monotonically increasing counter used as part of every generated
 * toast id. Combined with Date.now() in base-36 to produce ids that are
 * unique within a single SPA session without depending on the Web
 * Crypto API (which would be overkill for non-secret identifiers).
 */
let toastIdCounter = 0;

/** Generate a unique toast id without depending on crypto.randomUUID. */
function generateToastId(): string {
  toastIdCounter += 1;
  return `toast-${Date.now().toString(36)}-${toastIdCounter}`;
}

/**
 * Append a new toast to the queue and notify listeners. Returns the
 * generated id so callers may later dismiss(id).
 */
function dispatchToast(input: ToastInput): string {
  const id = generateToastId();
  const toast: Toast = {
    id,
    variant: input.variant,
    title: input.title,
    message: input.message,
    durationMs: input.durationMs ?? DEFAULT_TOAST_DURATION_MS,
  };
  toastQueue = [...toastQueue, toast];
  emitChange();
  return id;
}

/** Remove a toast by id; no-op if id is not in the queue. */
function dismissToastById(id: string): void {
  const next = toastQueue.filter((t) => t.id !== id);
  if (next.length === toastQueue.length) {
    return;
  }
  toastQueue = next;
  emitChange();
}

// ---------------------------------------------------------------------------
// useToast() hook
// ---------------------------------------------------------------------------

/**
 * Hook returning helpers to dispatch toasts from any component.
 *
 * The hook is stable across renders (its return object is memoized).
 * Calling it does NOT cause the calling component to re-render when
 * toasts change - only the ToastContainer subscribes to changes.
 *
 * @example
 *   const toast = useToast();
 *   toast.success("Connection saved");
 *   const id = toast.show({ variant: "info", message: "Working...", durationMs: 0 });
 *   // later when the work completes:
 *   toast.dismiss(id);
 */
export function useToast(): UseToastReturn {
  return useMemo<UseToastReturn>(
    () => ({
      show: (input) => dispatchToast(input),
      success: (message, title) => dispatchToast({ variant: "success", message, title }),
      error: (message, title) => dispatchToast({ variant: "error", message, title }),
      info: (message, title) => dispatchToast({ variant: "info", message, title }),
      warning: (message, title) => dispatchToast({ variant: "warning", message, title }),
      dismiss: (id) => dismissToastById(id),
    }),
    [],
  );
}

// ---------------------------------------------------------------------------
// Variant-to-visual maps
//
// Module-level constants so they are not re-allocated per render. Tailwind's
// JIT compiler picks up every utility at build time because it scans the
// source verbatim - inlining string concatenation here would defeat that.
// ---------------------------------------------------------------------------

/** Lucide icon component associated with each variant. */
const VARIANT_ICON: Record<ToastVariant, LucideIcon> = {
  success: CheckCircle2,
  error: XCircle,
  info: Info,
  warning: AlertTriangle,
};

/**
 * Tailwind container classes per variant. Provides a coordinated
 * background, border, and text color for each semantic state.
 *
 * - success: emerald (positive feedback, e.g., "Connection saved")
 * - error: red (failure feedback, e.g., "Failed to save")
 * - info: brand-blue (neutral feedback consistent with primary CTA)
 * - warning: amber (caution feedback, e.g., duplicate URL warning)
 */
const VARIANT_CONTAINER_CLASSES: Record<ToastVariant, string> = {
  success: "bg-emerald-50 border-emerald-200 text-emerald-900",
  error: "bg-red-50 border-red-200 text-red-900",
  info: "bg-brand-50 border-brand-200 text-brand-900",
  warning: "bg-amber-50 border-amber-200 text-amber-900",
};

/** Tailwind icon-color classes per variant; complements the container palette. */
const VARIANT_ICON_CLASSES: Record<ToastVariant, string> = {
  success: "text-emerald-600",
  error: "text-red-600",
  info: "text-brand-600",
  warning: "text-amber-600",
};

/**
 * ARIA role per variant. "alert" is reserved for the error variant so
 * screen readers announce errors with the higher urgency tier; the
 * gentler "status" role is used for success/info/warning to avoid
 * screen-reader fatigue (WCAG 4.1.3).
 */
const VARIANT_ROLE: Record<ToastVariant, "status" | "alert"> = {
  success: "status",
  info: "status",
  warning: "status",
  error: "alert",
};

// ---------------------------------------------------------------------------
// ToastItem (private sub-component)
//
// Renders a single toast row inside the ToastContainer's stack. Kept as
// a separate function so its handleDismissClick callback can use
// useCallback against the toast.id closure without affecting siblings.
// ---------------------------------------------------------------------------

/** Props for the private ToastItem sub-component. */
interface ToastItemProps {
  readonly toast: Toast;
  readonly onDismiss: (id: string) => void;
}

/**
 * Renders a single toast row: variant icon, optional title, message,
 * and dismiss button. Pure presentational; receives onDismiss from
 * the parent ToastContainer.
 */
function ToastItem({ toast, onDismiss }: ToastItemProps): JSX.Element {
  const Icon = VARIANT_ICON[toast.variant];

  // Memoize the click handler so re-renders that produce the same
  // `toast` and `onDismiss` references do not invalidate the button's
  // onClick prop reference (useful when this component is wrapped by
  // a memoized parent in future iterations).
  const handleDismissClick = useCallback(() => {
    onDismiss(toast.id);
  }, [onDismiss, toast.id]);

  return (
    <div
      role={VARIANT_ROLE[toast.variant]}
      className={clsx(
        "pointer-events-auto flex w-full max-w-sm items-start gap-3 rounded-lg border p-4 shadow-card transition-all",
        VARIANT_CONTAINER_CLASSES[toast.variant],
      )}
      data-testid={`toast-${toast.variant}`}
    >
      <Icon
        aria-hidden="true"
        className={clsx("mt-0.5 h-5 w-5 shrink-0", VARIANT_ICON_CLASSES[toast.variant])}
      />
      <div className="min-w-0 flex-1">
        {toast.title ? <p className="text-sm font-semibold leading-5">{toast.title}</p> : null}
        <p className={clsx("text-sm leading-5", toast.title ? "mt-1 opacity-90" : "")}>
          {toast.message}
        </p>
      </div>
      <button
        type="button"
        onClick={handleDismissClick}
        aria-label="Dismiss notification"
        className="ml-2 inline-flex h-6 w-6 shrink-0 items-center justify-center rounded text-current opacity-60 transition-opacity hover:opacity-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-current"
      >
        <X aria-hidden="true" className="h-4 w-4" />
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// ToastContainer (the public render component)
// ---------------------------------------------------------------------------

/**
 * ToastContainer - renders the toast stack via createPortal into
 * document.body. Mount once in App.tsx as a sibling of <RouterProvider />.
 *
 * Lifecycle:
 *   - Subscribes to the module-level store on mount.
 *   - Manages auto-dismiss timers per toast (Map keyed by id so re-renders
 *     do not schedule duplicate timers for the same toast).
 *   - Clears all timers on unmount.
 *
 * Layout:
 *   - Fixed-position wrapper covering the full viewport with
 *     pointer-events-none, so layout below remains clickable.
 *   - Each ToastItem has pointer-events-auto so its dismiss button
 *     remains interactive.
 *   - Content stacks at the top-right on every breakpoint; max-w-sm on
 *     each toast keeps text readable on small screens.
 */
export function ToastContainer(): JSX.Element | null {
  // Initial state mirrors the current store contents so toasts dispatched
  // before the container mounted are not lost (rare in practice but
  // defensive against effect-ordering quirks during route transitions).
  const [toasts, setToasts] = useState<ReadonlyArray<Toast>>(() => toastQueue);

  // ---------------------------------------------------------------------
  // Effect 1: subscribe to the module store on mount, unsubscribe on
  // unmount. Synchronizes the local React state with the singleton.
  // ---------------------------------------------------------------------
  useEffect(() => {
    const listener: ToastListener = (next) => {
      setToasts(next);
    };
    listeners.add(listener);
    // Sync once in case toasts were dispatched between the initial
    // useState read and the effect running.
    listener(toastQueue);
    return () => {
      listeners.delete(listener);
    };
  }, []);

  // ---------------------------------------------------------------------
  // Per-toast auto-dismiss timers.
  //
  // Tracked in a useRef'd Map<id, timeoutHandle> so re-renders do not
  // schedule duplicate timers for the same toast id. The Map lives for
  // the lifetime of the container; entries are added when a toast first
  // appears in the queue and removed when the toast leaves the queue
  // (or when the container unmounts via the cleanup effect below).
  // ---------------------------------------------------------------------
  const timersRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());

  useEffect(() => {
    const timers = timersRef.current;
    const activeIds = new Set(toasts.map((t) => t.id));

    // Clear timers for toasts that have already left the queue (manual
    // dismiss or earlier auto-dismiss).
    for (const [id, handle] of timers) {
      if (!activeIds.has(id)) {
        clearTimeout(handle);
        timers.delete(id);
      }
    }

    // Schedule timers for newly-arrived toasts that have positive duration.
    for (const toast of toasts) {
      if (timers.has(toast.id)) {
        continue;
      }
      if (toast.durationMs > 0) {
        const handle = setTimeout(() => {
          dismissToastById(toast.id);
        }, toast.durationMs);
        timers.set(toast.id, handle);
      }
    }
  }, [toasts]);

  // ---------------------------------------------------------------------
  // Effect 2: clear all timers when the container unmounts.
  // Captures the Map by reference at effect-setup time so the cleanup
  // function does not read a stale ref on a remount.
  // ---------------------------------------------------------------------
  useEffect(() => {
    const timers = timersRef.current;
    return () => {
      for (const handle of timers.values()) {
        clearTimeout(handle);
      }
      timers.clear();
    };
  }, []);

  // Memoized so each ToastItem receives a stable onDismiss reference.
  const handleDismiss = useCallback((id: string) => {
    dismissToastById(id);
  }, []);

  // SSR safety: document is undefined in non-browser environments. Tests
  // running under jsdom do have document; production runs in browsers.
  if (typeof document === "undefined") {
    return null;
  }

  // Cap the visible stack to MAX_VISIBLE_TOASTS. slice(-N) returns the
  // newest N toasts (the queue stores newest at the END), so older
  // overflow toasts are dropped first.
  const visibleToasts = toasts.slice(-MAX_VISIBLE_TOASTS);

  return createPortal(
    <div
      role="region"
      aria-label="Notifications"
      aria-live="polite"
      className="pointer-events-none fixed inset-0 z-50 flex flex-col items-end justify-start gap-2 p-4"
      data-testid="toast-container"
    >
      {visibleToasts.map((toast) => (
        <ToastItem key={toast.id} toast={toast} onDismiss={handleDismiss} />
      ))}
    </div>,
    document.body,
  );
}

// ---------------------------------------------------------------------------
// Test-only escape hatch
// ---------------------------------------------------------------------------

/**
 * Test-only helper: reset the toast queue so each test case starts with
 * a clean slate. Used by frontend/tests/components/ui/Toast.test.tsx
 * (and other tests that fire toasts as a side effect of rendering).
 *
 * The double-underscore prefix and "ForTesting" suffix make it visually
 * obvious this is not a regular API. Application code MUST NOT call it -
 * doing so would clear active toasts unexpectedly.
 *
 * Listeners are intentionally NOT cleared because ToastContainer
 * unmount/mount cycles in tests handle their own subscription state via
 * useEffect cleanup.
 */
export function __resetToastStoreForTesting(): void {
  toastQueue = [];
  toastIdCounter = 0;
  emitChange();
}
