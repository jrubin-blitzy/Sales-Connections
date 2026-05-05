/**
 * Modal.test.tsx - Vitest tests for the Modal UI primitive.
 *
 * Targets `frontend/src/components/ui/Modal.tsx` - the modal dialog
 * primitive backed by the native HTML <dialog> element.
 *
 * Coverage goals (>= 90% line per `Modal.tsx`):
 *   - Renders a <dialog> element with the documented data-testid.
 *   - isOpen toggles the `open` attribute (via the local <dialog>
 *     polyfill installed below).
 *   - title and description render with id linkage to aria-labelledby /
 *     aria-describedby.
 *   - ariaLabel prop overrides aria-labelledby (the source sets
 *     labelledBy = ariaLabel ? undefined : titleId).
 *   - showCloseButton (default true / explicit false) controls render
 *     of the data-testid="modal-close-button" button.
 *   - Close button click fires onClose exactly once.
 *   - ESC key fires onClose via the native <dialog> cancel event.
 *   - Backdrop click (event.target === dialog) fires onClose when
 *     closeOnBackdropClick is true (default).
 *   - Descendant click does NOT fire onClose (event.target !== dialog).
 *   - closeOnBackdropClick=false suppresses backdrop dismissal.
 *   - size prop applies max-w-* classes (sm/md/lg) to the dialog.
 *   - children render in the body; footer renders only when provided.
 *
 * jsdom 25.x <dialog> polyfill:
 *   jsdom 25.0.1 does NOT implement HTMLDialogElement.prototype.showModal
 *   or .close(). The Modal component calls these methods inside its
 *   useEffect when isOpen toggles, which would throw a TypeError without
 *   a shim. We patch the prototype at module load (idempotent if another
 *   test file has already patched). Setting/removing the `open` attribute
 *   automatically updates the reflected `.open` IDL property in jsdom.
 *
 * Conventions per AAP Sec 0.7.7 and frontend/.prettierrc.json:
 *   - Strict TypeScript; no `any`.
 *   - Double quotes; trailing commas; 2-space indent; line length <= 100.
 *   - Uses Vitest globals via test.globals: true (vite.config.ts), but
 *     imports describe/it/expect/vi explicitly for IDE type-awareness.
 *   - Uses @testing-library/react render and screen directly.
 *   - Uses @testing-library/user-event for the close-button click.
 *   - Uses fireEvent for the cancel event (programmatic dispatch) and
 *     for backdrop clicks (control event.target precisely).
 *   - No emoji; no console.log.
 *
 * Coordinates with:
 *   - frontend/src/components/ui/Modal.tsx (system under test)
 *   - frontend/tests/setup.ts (registers @testing-library/jest-dom matchers)
 *   - frontend/tsconfig.json (provides the @/ path alias)
 *   - frontend/vite.config.ts (declares test.globals: true)
 */

import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { Modal } from "@/components/ui/Modal";

// ---------------------------------------------------------------------------
// jsdom <dialog> polyfill
// ---------------------------------------------------------------------------
// jsdom 25.0.1 does not implement the HTMLDialogElement.prototype.showModal
// and .close() methods. The Modal component calls these from its useEffect
// when the controlled `isOpen` prop flips, so without a shim every test that
// renders Modal with isOpen=true would throw "dialog.showModal is not a
// function". The shim sets/removes the `open` content attribute, and the
// `dialog.open` IDL property reflects this attribute automatically.
//
// We attach to the prototype only if .showModal is missing, so this remains
// idempotent across test file loads and across worker restarts.
// ---------------------------------------------------------------------------

beforeAll(() => {
  const proto = HTMLDialogElement.prototype as HTMLDialogElement & {
    showModal: () => void;
    close: () => void;
  };
  if (typeof proto.showModal !== "function") {
    proto.showModal = function showModal(this: HTMLDialogElement): void {
      this.setAttribute("open", "");
    };
  }
  if (typeof proto.close !== "function") {
    proto.close = function close(this: HTMLDialogElement): void {
      this.removeAttribute("open");
    };
  }
});

// ---------------------------------------------------------------------------
// Test suite
// ---------------------------------------------------------------------------

describe("<Modal />", () => {
  // jsdom occasionally surfaces console.error noise unrelated to assertions
  // (e.g., act() warnings on internal effects). Suppressing during the test
  // body keeps the runner output focused on actual assertion failures. The
  // spy is restored after each test so suppression never leaks.
  let consoleErrorSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    consoleErrorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
  });

  afterEach(() => {
    consoleErrorSpy.mockRestore();
  });

  // -------------------------------------------------------------------------
  // 1. Rendering and <dialog> element
  // -------------------------------------------------------------------------
  describe("rendering and <dialog> element", () => {
    it("renders a <dialog> element with data-testid='modal-dialog'", () => {
      render(
        <Modal isOpen={false} onClose={vi.fn()} title="Confirm">
          Body
        </Modal>,
      );
      const dialog = screen.getByTestId("modal-dialog");
      expect(dialog).toBeInTheDocument();
      expect(dialog.tagName).toBe("DIALOG");
    });
  });

  // -------------------------------------------------------------------------
  // 2. isOpen prop - controlled show/hide via the open attribute
  // -------------------------------------------------------------------------
  describe("isOpen prop", () => {
    it("opens the dialog when isOpen=true (sets the `open` attribute)", () => {
      render(
        <Modal isOpen={true} onClose={vi.fn()} title="Confirm">
          Body
        </Modal>,
      );
      const dialog = screen.getByTestId("modal-dialog");
      expect(dialog).toHaveAttribute("open");
    });

    it("does NOT set the `open` attribute when isOpen=false", () => {
      render(
        <Modal isOpen={false} onClose={vi.fn()} title="Confirm">
          Body
        </Modal>,
      );
      const dialog = screen.getByTestId("modal-dialog");
      expect(dialog).not.toHaveAttribute("open");
    });

    it("toggles the `open` attribute when isOpen changes across rerenders", () => {
      const handleClose = vi.fn();
      const { rerender } = render(
        <Modal isOpen={false} onClose={handleClose} title="Confirm">
          Body
        </Modal>,
      );
      const dialog = screen.getByTestId("modal-dialog");
      expect(dialog).not.toHaveAttribute("open");

      rerender(
        <Modal isOpen={true} onClose={handleClose} title="Confirm">
          Body
        </Modal>,
      );
      expect(dialog).toHaveAttribute("open");

      rerender(
        <Modal isOpen={false} onClose={handleClose} title="Confirm">
          Body
        </Modal>,
      );
      expect(dialog).not.toHaveAttribute("open");
    });
  });

  // -------------------------------------------------------------------------
  // 3. Title and description rendering + ARIA linkage
  // -------------------------------------------------------------------------
  describe("title and description", () => {
    it("renders the title in an <h2>", () => {
      render(
        <Modal isOpen={true} onClose={vi.fn()} title="Confirm deletion">
          Body
        </Modal>,
      );
      const heading = screen.getByRole("heading", { level: 2, name: "Confirm deletion" });
      expect(heading).toBeInTheDocument();
    });

    it("aria-labelledby on the dialog points to the title element id", () => {
      render(
        <Modal isOpen={true} onClose={vi.fn()} title="Confirm deletion">
          Body
        </Modal>,
      );
      const heading = screen.getByRole("heading", { level: 2, name: "Confirm deletion" });
      const dialog = screen.getByTestId("modal-dialog");
      const headingId = heading.getAttribute("id");
      expect(headingId).toBeTruthy();
      expect(dialog).toHaveAttribute("aria-labelledby", headingId);
    });

    it("renders the description as a paragraph below the title", () => {
      render(
        <Modal isOpen={true} onClose={vi.fn()} title="Confirm" description="Are you sure?">
          Body
        </Modal>,
      );
      const description = screen.getByText("Are you sure?");
      expect(description).toBeInTheDocument();
      expect(description.tagName).toBe("P");
    });

    it("aria-describedby on the dialog points to the description element id", () => {
      render(
        <Modal isOpen={true} onClose={vi.fn()} title="Confirm" description="Are you sure?">
          Body
        </Modal>,
      );
      const description = screen.getByText("Are you sure?");
      const dialog = screen.getByTestId("modal-dialog");
      const descriptionId = description.getAttribute("id");
      expect(descriptionId).toBeTruthy();
      expect(dialog).toHaveAttribute("aria-describedby", descriptionId);
    });

    it("does NOT set aria-describedby when description is omitted", () => {
      render(
        <Modal isOpen={true} onClose={vi.fn()} title="Confirm">
          Body
        </Modal>,
      );
      const dialog = screen.getByTestId("modal-dialog");
      expect(dialog).not.toHaveAttribute("aria-describedby");
    });
  });

  // -------------------------------------------------------------------------
  // 4. ariaLabel prop - overrides title-based labeling
  // -------------------------------------------------------------------------
  describe("ariaLabel prop", () => {
    it("uses aria-label instead of aria-labelledby when ariaLabel is set", () => {
      render(
        <Modal
          isOpen={true}
          onClose={vi.fn()}
          title="Visible title"
          ariaLabel="Confirmation dialog"
        >
          Body
        </Modal>,
      );
      const dialog = screen.getByTestId("modal-dialog");
      expect(dialog).toHaveAttribute("aria-label", "Confirmation dialog");
      expect(dialog).not.toHaveAttribute("aria-labelledby");
    });

    it("aria-label takes precedence over title-based labeling even when title is set", () => {
      render(
        <Modal
          isOpen={true}
          onClose={vi.fn()}
          title="Title text"
          ariaLabel="Custom accessible name"
        >
          Body
        </Modal>,
      );
      const dialog = screen.getByTestId("modal-dialog");
      expect(dialog).toHaveAttribute("aria-label", "Custom accessible name");
      expect(dialog).not.toHaveAttribute("aria-labelledby");
      // The visible title still renders so sighted users still see the heading.
      expect(screen.getByRole("heading", { level: 2, name: "Title text" })).toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // 5. Close button - render, ARIA label, click handling
  // -------------------------------------------------------------------------
  describe("close button", () => {
    it("renders the close button by default (showCloseButton omitted)", () => {
      render(
        <Modal isOpen={true} onClose={vi.fn()} title="Confirm">
          Body
        </Modal>,
      );
      expect(screen.getByTestId("modal-close-button")).toBeInTheDocument();
    });

    it("does NOT render the close button when showCloseButton=false", () => {
      render(
        <Modal isOpen={true} onClose={vi.fn()} title="Confirm" showCloseButton={false}>
          Body
        </Modal>,
      );
      expect(screen.queryByTestId("modal-close-button")).toBeNull();
    });

    it("renders the close button with aria-label='Close dialog'", () => {
      render(
        <Modal isOpen={true} onClose={vi.fn()} title="Confirm">
          Body
        </Modal>,
      );
      const closeButton = screen.getByTestId("modal-close-button");
      expect(closeButton).toHaveAttribute("aria-label", "Close dialog");
    });

    it("calls onClose exactly once when the close button is clicked", async () => {
      const handleClose = vi.fn();
      const user = userEvent.setup();
      render(
        <Modal isOpen={true} onClose={handleClose} title="Confirm">
          Body
        </Modal>,
      );
      const closeButton = screen.getByTestId("modal-close-button");
      await user.click(closeButton);
      expect(handleClose).toHaveBeenCalledTimes(1);
    });
  });

  // -------------------------------------------------------------------------
  // 6. ESC key dismissal - native <dialog> cancel event handling
  // -------------------------------------------------------------------------
  describe("ESC key dismissal", () => {
    it("calls onClose when the dialog cancel event fires (ESC pressed)", () => {
      const handleClose = vi.fn();
      render(
        <Modal isOpen={true} onClose={handleClose} title="Confirm">
          Body
        </Modal>,
      );
      const dialog = screen.getByTestId("modal-dialog");
      // The native <dialog> fires a "cancel" event when the user presses ESC
      // on an open modal. Dispatch it programmatically to simulate that
      // interaction. cancelable: true allows the handler's preventDefault()
      // to flip defaultPrevented; bubbles: false matches the native event.
      fireEvent(dialog, new Event("cancel", { cancelable: true, bubbles: false }));
      expect(handleClose).toHaveBeenCalledTimes(1);
    });
  });

  // -------------------------------------------------------------------------
  // 7. Backdrop click dismissal - target equality and closeOnBackdropClick
  // -------------------------------------------------------------------------
  describe("backdrop click dismissal", () => {
    it("calls onClose when the backdrop is clicked (event.target === dialog)", () => {
      const handleClose = vi.fn();
      render(
        <Modal isOpen={true} onClose={handleClose} title="Confirm">
          <p>Body content</p>
        </Modal>,
      );
      const dialog = screen.getByTestId("modal-dialog");
      // fireEvent.click(dialog) sets event.target to the dialog itself, which
      // is exactly how a backdrop click resolves (the visible backdrop is the
      // ::backdrop pseudo-element of the dialog; clicks on it target the
      // dialog node).
      fireEvent.click(dialog);
      expect(handleClose).toHaveBeenCalledTimes(1);
    });

    it("does NOT call onClose when a descendant of the dialog is clicked", () => {
      const handleClose = vi.fn();
      render(
        <Modal isOpen={true} onClose={handleClose} title="Confirm">
          <p data-testid="body-content">Click me</p>
        </Modal>,
      );
      const bodyContent = screen.getByTestId("body-content");
      // Clicking a descendant bubbles the event up to the dialog, but
      // event.target is the descendant - so event.target !== dialogRef.current
      // and the backdrop branch must not fire.
      fireEvent.click(bodyContent);
      expect(handleClose).not.toHaveBeenCalled();
    });

    it("does NOT call onClose on backdrop click when closeOnBackdropClick=false", () => {
      const handleClose = vi.fn();
      render(
        <Modal isOpen={true} onClose={handleClose} title="Confirm" closeOnBackdropClick={false}>
          Body
        </Modal>,
      );
      const dialog = screen.getByTestId("modal-dialog");
      fireEvent.click(dialog);
      expect(handleClose).not.toHaveBeenCalled();
    });
  });

  // -------------------------------------------------------------------------
  // 8. size prop - max-w-* class application
  // -------------------------------------------------------------------------
  describe("size prop", () => {
    it("applies max-w-md class for size='md' (default when size is omitted)", () => {
      render(
        <Modal isOpen={true} onClose={vi.fn()} title="Confirm">
          Body
        </Modal>,
      );
      const dialog = screen.getByTestId("modal-dialog");
      expect(dialog).toHaveClass("max-w-md");
    });

    it("applies max-w-sm class for size='sm'", () => {
      render(
        <Modal isOpen={true} onClose={vi.fn()} title="Confirm" size="sm">
          Body
        </Modal>,
      );
      const dialog = screen.getByTestId("modal-dialog");
      expect(dialog).toHaveClass("max-w-sm");
    });

    it("applies max-w-lg class for size='lg'", () => {
      render(
        <Modal isOpen={true} onClose={vi.fn()} title="Confirm" size="lg">
          Body
        </Modal>,
      );
      const dialog = screen.getByTestId("modal-dialog");
      expect(dialog).toHaveClass("max-w-lg");
    });
  });

  // -------------------------------------------------------------------------
  // 9. Content slots - children and footer
  // -------------------------------------------------------------------------
  describe("content slots", () => {
    it("renders children in the body", () => {
      render(
        <Modal isOpen={true} onClose={vi.fn()} title="Confirm">
          <div data-testid="body">Custom body</div>
        </Modal>,
      );
      const body = screen.getByTestId("body");
      expect(body).toBeInTheDocument();
      expect(body).toHaveTextContent("Custom body");
    });

    it("renders the footer node when the footer prop is provided", () => {
      render(
        <Modal
          isOpen={true}
          onClose={vi.fn()}
          title="Confirm"
          footer={<button data-testid="footer-btn">OK</button>}
        >
          Body
        </Modal>,
      );
      expect(screen.getByTestId("footer-btn")).toBeInTheDocument();
    });

    it("does NOT render the footer container when the footer prop is omitted", () => {
      const { container } = render(
        <Modal isOpen={true} onClose={vi.fn()} title="Confirm">
          Body
        </Modal>,
      );
      // The footer container carries `flex-col-reverse` from the Modal
      // source. When footer is omitted the surrounding div is not rendered,
      // so no element with that class exists in the tree.
      expect(container.querySelector(".flex-col-reverse")).toBeNull();
    });
  });
});
