/**
 * Toast.test.tsx - Vitest tests for the Toast UI primitive.
 *
 * Targets `frontend/src/components/ui/Toast.tsx` - the imperative
 * toast notification system backed by a module-level singleton store
 * and rendered via createPortal into document.body.
 *
 * Coverage goals (>= 90% line per `Toast.tsx`):
 *   - Initial state: no toasts visible.
 *   - useToast().success/error/info/warning render the right variant
 *     with the right ARIA role.
 *   - Auto-dismiss after DEFAULT_TOAST_DURATION_MS (5000ms) using
 *     fake timers.
 *   - durationMs: 0 disables auto-dismiss (toast persists).
 *   - Manual dismiss via the X button removes the toast.
 *   - MAX_VISIBLE_TOASTS = 5 caps the visible toast count.
 *   - __resetToastStoreForTesting clears the queue.
 *   - Container ARIA: role="region", aria-label="Notifications",
 *     aria-live="polite", data-testid="toast-container".
 *   - Portal mounts to document.body (NOT inside the test render
 *     container).
 *   - Multiple variants render side-by-side without conflict.
 *   - Toast.title (when provided) renders alongside the message.
 *
 * Architecture notes:
 *   - The Toast module exposes a module-level singleton store (NOT
 *     React Context). useToast() returns imperative methods; tests must
 *     call those methods from inside a host component.
 *   - The ToastTestHarness component below captures the useToast() API
 *     reference into an external apiRef so tests can fire toasts after
 *     render has flushed.
 *   - Each test starts with a clean store: the top-level beforeEach
 *     calls __resetToastStoreForTesting() so no toast state leaks across
 *     cases.
 *
 * Conventions per AAP Sec 0.7.7 and frontend/.prettierrc.json:
 *   - Strict TypeScript; tests permit unused locals via the eslint
 *     override but we still avoid them.
 *   - Double quotes; trailing commas; 2-space indent; line length <= 100.
 *   - Vitest test.globals: true is configured in vite.config.ts, but we
 *     import describe/it/expect/vi/afterEach/beforeEach explicitly so
 *     editors and ESLint resolve them without ambient-type lookups.
 *   - act from @testing-library/react wraps direct store mutations so
 *     React processes the listener-driven re-renders before assertions.
 *   - userEvent for the X button click; fake timers via vi.useFakeTimers
 *     for the auto-dismiss tests.
 *   - No emoji; no console.log.
 *
 * Coordinates with:
 *   - frontend/src/components/ui/Toast.tsx (system under test)
 *   - frontend/tests/setup.ts (registers @testing-library/jest-dom matchers)
 *   - frontend/tsconfig.json (provides the @/ path alias)
 *   - frontend/vite.config.ts (declares test.globals: true, env jsdom)
 */

import { useEffect, type ReactElement } from "react";
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ToastContainer,
  __resetToastStoreForTesting,
  useToast,
  type UseToastReturn,
} from "@/components/ui/Toast";

// ---------------------------------------------------------------------------
// Test harness
// ---------------------------------------------------------------------------
// `useToast()` is a React hook; it can only be invoked inside a function
// component. The harness mounts a tiny component, calls useToast(), and
// publishes the returned API onto an external ref so each test can fire
// toasts after the initial render has flushed.
//
// The publish happens inside useEffect rather than during render so the
// callback runs exactly once per mount and does not violate React's
// "no side effects during render" rule (which the eslint plugin flags).
// useToast() returns a memoized object so the api reference is stable
// across re-renders triggered by toast dispatch.
// ---------------------------------------------------------------------------

interface ToastTestHarnessProps {
  readonly onReady: (api: UseToastReturn) => void;
}

function ToastTestHarness({ onReady }: ToastTestHarnessProps): ReactElement {
  const toast = useToast();
  useEffect(() => {
    onReady(toast);
  }, [onReady, toast]);
  return <span data-testid="harness-marker" />;
}

interface RenderToastSystemResult {
  readonly toastApi: UseToastReturn;
}

/**
 * Render <ToastTestHarness /> + <ToastContainer /> as siblings and
 * return the captured useToast() API. Throws if the harness did not
 * publish the API (which would indicate a regression in either the
 * harness wiring or React's effect lifecycle).
 */
function renderToastSystem(): RenderToastSystemResult {
  const apiRef: { current: UseToastReturn | null } = { current: null };
  render(
    <>
      <ToastTestHarness
        onReady={(api): void => {
          apiRef.current = api;
        }}
      />
      <ToastContainer />
    </>,
  );
  if (apiRef.current === null) {
    throw new Error("ToastTestHarness did not capture the useToast API.");
  }
  return { toastApi: apiRef.current };
}

// ---------------------------------------------------------------------------
// Top-level test suite
// ---------------------------------------------------------------------------

describe("Toast system", () => {
  beforeEach(() => {
    // Each test starts with an empty queue. The Toast module's store is
    // module-level (a process-wide singleton in the test worker), so
    // without this reset toasts dispatched by an earlier test would be
    // visible to a later one - in particular React Testing Library's
    // automatic cleanup() unmounts components but does NOT clear the
    // module-level toastQueue.
    act(() => {
      __resetToastStoreForTesting();
    });
  });

  afterEach(() => {
    // Restore real timers in case a test enabled fake timers. Without
    // this, a leaked fake timer can deadlock subsequent tests that rely
    // on real setTimeout (e.g., userEvent's internal scheduling).
    vi.useRealTimers();
  });

  // -------------------------------------------------------------------------
  // 1. Initial state
  // -------------------------------------------------------------------------

  describe("initial state", () => {
    it("renders the container with no toast cards initially", () => {
      render(<ToastContainer />);

      expect(screen.getByTestId("toast-container")).toBeInTheDocument();
      expect(screen.queryByTestId("toast-success")).toBeNull();
      expect(screen.queryByTestId("toast-error")).toBeNull();
      expect(screen.queryByTestId("toast-info")).toBeNull();
      expect(screen.queryByTestId("toast-warning")).toBeNull();
    });
  });

  // -------------------------------------------------------------------------
  // 2. Container ARIA and portal mount strategy
  // -------------------------------------------------------------------------

  describe("container ARIA and portal", () => {
    it('renders the container with role="region"', () => {
      render(<ToastContainer />);
      const container = screen.getByTestId("toast-container");
      expect(container).toHaveAttribute("role", "region");
    });

    it('renders the container with aria-label="Notifications"', () => {
      render(<ToastContainer />);
      const container = screen.getByTestId("toast-container");
      expect(container).toHaveAttribute("aria-label", "Notifications");
    });

    it('renders the container with aria-live="polite"', () => {
      render(<ToastContainer />);
      const container = screen.getByTestId("toast-container");
      expect(container).toHaveAttribute("aria-live", "polite");
    });

    it("mounts the container in document.body via createPortal", () => {
      const { container: testContainer } = render(<ToastContainer />);
      const toastContainer = screen.getByTestId("toast-container");

      // Toast container is in document.body...
      expect(document.body.contains(toastContainer)).toBe(true);
      // ...but NOT inside the React Testing Library render container,
      // which would be the case for a non-portal child.
      expect(testContainer.contains(toastContainer)).toBe(false);
    });
  });

  // -------------------------------------------------------------------------
  // 3. useToast variants - one test per convenience method
  // -------------------------------------------------------------------------

  describe("useToast variants", () => {
    it("useToast().success() renders a toast-success card", () => {
      const { toastApi } = renderToastSystem();

      act(() => {
        toastApi.success("Saved!");
      });

      expect(screen.getByTestId("toast-success")).toBeInTheDocument();
      expect(screen.getByText("Saved!")).toBeInTheDocument();
    });

    it("useToast().error() renders a toast-error card", () => {
      const { toastApi } = renderToastSystem();

      act(() => {
        toastApi.error("Boom");
      });

      expect(screen.getByTestId("toast-error")).toBeInTheDocument();
      expect(screen.getByText("Boom")).toBeInTheDocument();
    });

    it("useToast().info() renders a toast-info card", () => {
      const { toastApi } = renderToastSystem();

      act(() => {
        toastApi.info("FYI");
      });

      expect(screen.getByTestId("toast-info")).toBeInTheDocument();
      expect(screen.getByText("FYI")).toBeInTheDocument();
    });

    it("useToast().warning() renders a toast-warning card", () => {
      const { toastApi } = renderToastSystem();

      act(() => {
        toastApi.warning("Heads up");
      });

      expect(screen.getByTestId("toast-warning")).toBeInTheDocument();
      expect(screen.getByText("Heads up")).toBeInTheDocument();
    });

    it("renders multiple variants side-by-side without conflict", () => {
      const { toastApi } = renderToastSystem();

      act(() => {
        toastApi.success("ok");
        toastApi.error("oops");
        toastApi.info("note");
        toastApi.warning("careful");
      });

      expect(screen.getByTestId("toast-success")).toBeInTheDocument();
      expect(screen.getByTestId("toast-error")).toBeInTheDocument();
      expect(screen.getByTestId("toast-info")).toBeInTheDocument();
      expect(screen.getByTestId("toast-warning")).toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // 4. ARIA role per variant
  // -------------------------------------------------------------------------
  // success/info/warning use role="status" (gentle announcement).
  // error uses role="alert" (assertive announcement; WCAG 4.1.3).
  // -------------------------------------------------------------------------

  describe("toast roles", () => {
    it('success toast has role="status"', () => {
      const { toastApi } = renderToastSystem();
      act(() => {
        toastApi.success("ok");
      });
      const card = screen.getByTestId("toast-success");
      expect(card).toHaveAttribute("role", "status");
    });

    it('error toast has role="alert"', () => {
      const { toastApi } = renderToastSystem();
      act(() => {
        toastApi.error("bad");
      });
      const card = screen.getByTestId("toast-error");
      expect(card).toHaveAttribute("role", "alert");
    });

    it('info toast has role="status"', () => {
      const { toastApi } = renderToastSystem();
      act(() => {
        toastApi.info("note");
      });
      const card = screen.getByTestId("toast-info");
      expect(card).toHaveAttribute("role", "status");
    });

    it('warning toast has role="status"', () => {
      const { toastApi } = renderToastSystem();
      act(() => {
        toastApi.warning("careful");
      });
      const card = screen.getByTestId("toast-warning");
      expect(card).toHaveAttribute("role", "status");
    });
  });

  // -------------------------------------------------------------------------
  // 5. Auto-dismiss
  // -------------------------------------------------------------------------
  // Fake timers freeze the wall clock so the test can advance time
  // precisely instead of waiting 5 real-time seconds (which would slow
  // CI to a crawl).
  // -------------------------------------------------------------------------

  describe("auto-dismiss", () => {
    it("toast auto-dismisses after the default 5000ms", () => {
      vi.useFakeTimers();
      const { toastApi } = renderToastSystem();

      act(() => {
        toastApi.success("hello");
      });
      expect(screen.getByTestId("toast-success")).toBeInTheDocument();

      act(() => {
        vi.advanceTimersByTime(5000);
      });
      expect(screen.queryByTestId("toast-success")).toBeNull();
    });

    it("toast auto-dismisses at exactly the custom durationMs boundary", () => {
      vi.useFakeTimers();
      const { toastApi } = renderToastSystem();

      act(() => {
        toastApi.show({ variant: "success", message: "hello", durationMs: 1000 });
      });
      expect(screen.getByTestId("toast-success")).toBeInTheDocument();

      // 1ms before the deadline: still visible.
      act(() => {
        vi.advanceTimersByTime(999);
      });
      expect(screen.getByTestId("toast-success")).toBeInTheDocument();

      // Cross the deadline.
      act(() => {
        vi.advanceTimersByTime(1);
      });
      expect(screen.queryByTestId("toast-success")).toBeNull();
    });
  });

  // -------------------------------------------------------------------------
  // 6. durationMs: 0 disables auto-dismiss
  // -------------------------------------------------------------------------

  describe("durationMs disables auto-dismiss", () => {
    it("durationMs: 0 keeps the toast visible indefinitely", () => {
      vi.useFakeTimers();
      const { toastApi } = renderToastSystem();

      act(() => {
        toastApi.show({ variant: "info", message: "Persistent", durationMs: 0 });
      });
      expect(screen.getByTestId("toast-info")).toBeInTheDocument();

      // Advance way past the default 5000ms; the toast must still be there.
      act(() => {
        vi.advanceTimersByTime(60_000);
      });
      expect(screen.getByTestId("toast-info")).toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // 7. Manual dismiss via the X button
  // -------------------------------------------------------------------------

  describe("manual dismiss", () => {
    it("clicking the dismiss X button removes the toast", async () => {
      const user = userEvent.setup();
      const { toastApi } = renderToastSystem();

      act(() => {
        toastApi.success("Hi");
      });
      expect(screen.getByTestId("toast-success")).toBeInTheDocument();

      const dismissButton = screen.getByRole("button", { name: "Dismiss notification" });
      await user.click(dismissButton);

      expect(screen.queryByTestId("toast-success")).toBeNull();
    });

    it('dismiss button has aria-label="Dismiss notification"', () => {
      const { toastApi } = renderToastSystem();
      act(() => {
        toastApi.info("test");
      });
      expect(screen.getByLabelText("Dismiss notification")).toBeInTheDocument();
    });

    it("each toast has its own dismiss button", () => {
      const { toastApi } = renderToastSystem();
      act(() => {
        toastApi.success("first");
        toastApi.error("second");
        toastApi.info("third");
      });

      const dismissButtons = screen.getAllByLabelText("Dismiss notification");
      expect(dismissButtons).toHaveLength(3);
    });

    it("clicking one dismiss button only removes its own toast", async () => {
      const user = userEvent.setup();
      const { toastApi } = renderToastSystem();
      act(() => {
        toastApi.success("keep me");
        toastApi.error("dismiss me");
      });

      const errorCard = screen.getByTestId("toast-error");
      // Find the dismiss button INSIDE the error card so we click the
      // right one (not the success card's dismiss button).
      const errorDismiss = errorCard.querySelector(
        'button[aria-label="Dismiss notification"]',
      ) as HTMLButtonElement | null;
      expect(errorDismiss).not.toBeNull();
      if (errorDismiss === null) {
        throw new Error("Error toast dismiss button missing");
      }

      await user.click(errorDismiss);

      expect(screen.queryByTestId("toast-error")).toBeNull();
      // The unrelated success toast must still be visible.
      expect(screen.getByTestId("toast-success")).toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // 8. MAX_VISIBLE_TOASTS cap
  // -------------------------------------------------------------------------
  // The container.slice(-MAX_VISIBLE_TOASTS) strategy in Toast.tsx caps
  // the visible card count at 5 even if more are dispatched.
  // -------------------------------------------------------------------------

  describe("MAX_VISIBLE_TOASTS cap", () => {
    it("renders at most 5 toast cards visible at once", () => {
      vi.useFakeTimers();
      const { toastApi } = renderToastSystem();

      act(() => {
        toastApi.success("1");
        toastApi.success("2");
        toastApi.success("3");
        toastApi.success("4");
        toastApi.success("5");
        toastApi.success("6");
        toastApi.success("7");
      });

      const container = screen.getByTestId("toast-container");
      // Filter children: the container itself has data-testid="toast-container",
      // each toast card has data-testid="toast-{variant}". We want the
      // variant cards only.
      const variantCards = Array.from(
        container.querySelectorAll<HTMLElement>("[data-testid^='toast-']"),
      ).filter((el) => el.getAttribute("data-testid") !== "toast-container");

      expect(variantCards.length).toBeLessThanOrEqual(5);
      expect(variantCards.length).toBe(5);
    });

    it("keeps the newest 5 toasts visible (oldest overflow dropped)", () => {
      vi.useFakeTimers();
      const { toastApi } = renderToastSystem();

      act(() => {
        toastApi.success("oldest-1");
        toastApi.success("oldest-2");
        toastApi.success("middle-3");
        toastApi.success("middle-4");
        toastApi.success("middle-5");
        toastApi.success("newest-6");
      });

      // The oldest toast ("oldest-1") must have been pushed off the
      // visible stack. Newer toasts ("newest-6") must remain.
      expect(screen.queryByText("oldest-1")).toBeNull();
      expect(screen.getByText("newest-6")).toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // 9. Toast with title + message
  // -------------------------------------------------------------------------

  describe("toast with title", () => {
    it("renders both title and message when title is provided", () => {
      const { toastApi } = renderToastSystem();

      act(() => {
        toastApi.show({
          variant: "success",
          title: "Saved",
          message: "Connection saved",
        });
      });

      expect(screen.getByText("Saved")).toBeInTheDocument();
      expect(screen.getByText("Connection saved")).toBeInTheDocument();
    });

    it("renders only the message when title is omitted", () => {
      const { toastApi } = renderToastSystem();

      act(() => {
        toastApi.success("Just a message");
      });

      expect(screen.getByText("Just a message")).toBeInTheDocument();
      // No title text should be present.
      expect(screen.queryByText(/^Title$/)).toBeNull();
    });

    it("convenience methods accept an optional title positional argument", () => {
      const { toastApi } = renderToastSystem();

      act(() => {
        toastApi.success("Connection saved", "Saved");
      });

      expect(screen.getByText("Saved")).toBeInTheDocument();
      expect(screen.getByText("Connection saved")).toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // 10. useToast.show explicit form
  // -------------------------------------------------------------------------

  describe("useToast.show explicit form", () => {
    it("show() accepts a full ToastInput with variant and message", () => {
      const { toastApi } = renderToastSystem();

      act(() => {
        toastApi.show({ variant: "error", message: "Failed" });
      });

      expect(screen.getByTestId("toast-error")).toBeInTheDocument();
      expect(screen.getByText("Failed")).toBeInTheDocument();
    });

    it("show() supports a custom durationMs override", () => {
      vi.useFakeTimers();
      const { toastApi } = renderToastSystem();

      act(() => {
        toastApi.show({ variant: "info", message: "Quick", durationMs: 100 });
      });
      expect(screen.getByTestId("toast-info")).toBeInTheDocument();

      act(() => {
        vi.advanceTimersByTime(100);
      });
      expect(screen.queryByTestId("toast-info")).toBeNull();
    });

    it("show() returns a non-empty string id", () => {
      const { toastApi } = renderToastSystem();
      let id = "";
      act(() => {
        id = toastApi.show({ variant: "success", message: "hi" });
      });
      expect(typeof id).toBe("string");
      expect(id.length).toBeGreaterThan(0);
    });
  });

  // -------------------------------------------------------------------------
  // 11. useToast.dismiss programmatic dismissal
  // -------------------------------------------------------------------------

  describe("useToast.dismiss", () => {
    it("dismiss(id) removes the matching toast from the queue", () => {
      const { toastApi } = renderToastSystem();

      let toastId = "";
      act(() => {
        toastId = toastApi.success("hello");
      });
      expect(screen.getByTestId("toast-success")).toBeInTheDocument();

      act(() => {
        toastApi.dismiss(toastId);
      });
      expect(screen.queryByTestId("toast-success")).toBeNull();
    });

    it("dismiss() with an unknown id is a no-op (does not throw)", () => {
      const { toastApi } = renderToastSystem();
      act(() => {
        toastApi.success("hello");
      });
      expect(screen.getByTestId("toast-success")).toBeInTheDocument();

      // Unknown id - should silently do nothing.
      expect(() => {
        act(() => {
          toastApi.dismiss("toast-nonexistent-id");
        });
      }).not.toThrow();

      // The original toast must still be present.
      expect(screen.getByTestId("toast-success")).toBeInTheDocument();
    });

    it("dismiss(id) only removes the specified toast", () => {
      const { toastApi } = renderToastSystem();
      let firstId = "";
      act(() => {
        firstId = toastApi.success("first");
        toastApi.error("second");
      });

      act(() => {
        toastApi.dismiss(firstId);
      });

      expect(screen.queryByTestId("toast-success")).toBeNull();
      expect(screen.getByTestId("toast-error")).toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // 12. __resetToastStoreForTesting
  // -------------------------------------------------------------------------

  describe("__resetToastStoreForTesting", () => {
    it("clears all toasts when called", () => {
      const { toastApi } = renderToastSystem();

      act(() => {
        toastApi.success("hello");
        toastApi.error("boom");
      });
      expect(screen.getByTestId("toast-success")).toBeInTheDocument();
      expect(screen.getByTestId("toast-error")).toBeInTheDocument();

      act(() => {
        __resetToastStoreForTesting();
      });

      expect(screen.queryByTestId("toast-success")).toBeNull();
      expect(screen.queryByTestId("toast-error")).toBeNull();
    });

    it("is safe to call when the queue is already empty", () => {
      render(<ToastContainer />);
      // No toasts dispatched in this test (beforeEach reset already
      // emptied the queue), so calling reset must remain a no-op that
      // does not throw and does not introduce any toast cards.
      expect(() => {
        act(() => {
          __resetToastStoreForTesting();
        });
      }).not.toThrow();
      expect(screen.queryByTestId("toast-success")).toBeNull();
      expect(screen.queryByTestId("toast-error")).toBeNull();
      expect(screen.queryByTestId("toast-info")).toBeNull();
      expect(screen.queryByTestId("toast-warning")).toBeNull();
    });
  });
});
