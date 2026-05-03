/**
 * StatusChip.test.tsx - Vitest tests for F-005 Outreach Status Chip.
 *
 * Targets `frontend/src/features/connections/StatusChip.tsx`. The
 * component (per AAP Sec 0.5.4 Screen 1 / F-005) renders one of the
 * four outreach status values (Not Started / In Progress / Contacted /
 * Closed) and is editable inline via `<RoleGate role={['Admin',
 * 'Viewer']}>` for users with Admin or Viewer (Sales Rep) role,
 * falling back to a static read-only Badge for Contributors. The
 * editable Select fires `useUpdateStatusMutation` (PATCH
 * /api/connections/:id/status) which performs an optimistic cache
 * update with rollback on error.
 *
 * What this file verifies (each test bullet maps to a `describe`/`it`
 * pair below):
 *
 *   - Read-only mode: Contributor and the no-session (null) state both
 *     fall through to the Badge fallback - the editable Select must
 *     NOT be present in the DOM.
 *   - Editable mode: Admin and Viewer (Sales Rep) sessions both render
 *     the editable Select; the read-only fallback must NOT be present.
 *   - Status-to-variant mapping: each of the four outreach status values
 *     renders correctly in read-only mode (text content matches the
 *     value; the Badge primitive's variant testid is also asserted).
 *   - Mutation happy path: changing the Select value fires PATCH
 *     /api/connections/:id/status with the expected body shape; picking
 *     the same value (no-op) does NOT fire the mutation; the
 *     optimistic local mirror immediately reflects the new value.
 *   - Optimistic update + rollback: a 403 from MSW causes the local
 *     mirror to revert to the prior parent value (defense-in-depth on
 *     top of TanStack Query's parent-cache rollback in the hook).
 *   - Pending mutation UI: the Loader2 spinner appears while the
 *     mutation is in flight, the Select is disabled while pending, and
 *     the spinner disappears after the mutation settles successfully.
 *   - Disabled prop: passing `disabled` to the chip in editable mode
 *     disables the Select (UX courtesy for soft-deleted records); in
 *     read-only mode the prop has no visible effect on the badge.
 *   - Click stopPropagation: the editable wrapper span captures click
 *     events and stops their propagation so a parent feed-row click
 *     handler does not also fire when opening the Select.
 *   - Local mirror state sync: when the parent passes a new `value`
 *     prop and no mutation is in flight, the chip's internal local
 *     mirror updates to match (verified via a controlled Harness
 *     component that toggles `value` from the outside).
 *
 * Test infrastructure:
 *   - MSW (msw@2.7.0) intercepts every `fetch` made by the real
 *     `useUpdateStatusMutation` hook so we exercise the full mutation
 *     pipeline (mutationFn, onMutate, onError, onSuccess, onSettled)
 *     end-to-end without stubbing the hook.
 *   - The MSW server is booted in tests/setup.ts with
 *     onUnhandledRequest: "error", so the default handlers in
 *     tests/mocks/handlers.ts must cover every URL the SPA fetches
 *     (they do, including PATCH /api/connections/:id/status).
 *   - Per-test handler overrides via `server.use(...)` capture request
 *     bodies, inject 403 errors, and install pending-forever handlers
 *     for spinner verification. Resets are handled centrally in
 *     tests/setup.ts `afterEach`.
 *   - `renderWithMockedSession` wraps the chip in
 *     QueryClientProvider > MockAuthProvider > MemoryRouter so role
 *     gating resolves synchronously to the supplied session without
 *     waiting for the production GET /api/me round-trip.
 *
 * Conventions per AAP Sec 0.7.7 and frontend/.prettierrc.json:
 *   - Strict TypeScript; eslint allows `any` in tests but the project
 *     still avoids it (the test glob disables the rule but we keep
 *     types tight to surface drift early).
 *   - Double quotes; trailing commas; 2-space indent; line length <= 100.
 *   - Named exports only; no default export.
 *   - No emoji; no console.log.
 *   - jest-dom matchers (`toBeInTheDocument`, `toHaveAttribute`,
 *     `toHaveClass`, etc.) are globally registered in tests/setup.ts.
 *
 * Coordinates with:
 *   - frontend/src/features/connections/StatusChip.tsx (system under test).
 *   - frontend/src/schemas/connection.ts (`OutreachStatusValue` type).
 *   - frontend/tests/test-utils.tsx (renderWithMockedSession + RTL re-exports).
 *   - frontend/tests/mocks/server.ts (MSW Node server instance).
 *   - frontend/tests/mocks/handlers.ts (default + override handlers).
 *   - frontend/tests/mocks/data.ts (typed entity factories).
 *   - frontend/tests/setup.ts (jest-dom, MSW lifecycle, toast/correlation reset).
 */

import { useState } from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { http, HttpResponse } from "msw";

import { StatusChip } from "@/features/connections/StatusChip";
import type { OutreachStatusValue } from "@/schemas/connection";

import { server } from "../../mocks/server";
import { overrides } from "../../mocks/handlers";
import { makeConnectionRead, makeSessionRead, makeUserRead } from "../../mocks/data";
import { renderWithMockedSession, screen, waitFor, userEvent } from "../../test-utils";

// ---------------------------------------------------------------------------
// Module-scoped fixtures and helpers
// ---------------------------------------------------------------------------

/**
 * Stable record id used across all tests. The value is a simple string
 * (NOT a UUID) because the chip itself never validates the id - it
 * passes it through to the mutation hook which encodes it into the
 * request URL via encodeURIComponent. Using a short readable id makes
 * the data-testid locators (status-chip-readonly-rec-1,
 * status-chip-select-rec-1, status-chip-editable-rec-1) trivial to
 * type literally in tests and easy to scan when debugging.
 */
const RECORD_ID = "rec-1";

/**
 * Render the StatusChip inside the production provider stack with a
 * synchronously-mocked session for the requested role. The role
 * argument drives RoleGate's behavior:
 *
 *   - 'Admin' / 'Viewer' -> editable Select
 *   - 'Contributor' / null -> read-only Badge fallback
 *
 * Defaults: recordId = 'rec-1', value = 'Not Started', disabled = false.
 *
 * Returning the result of `renderWithMockedSession` lets tests reach
 * into the underlying RTL render result and the QueryClient when
 * needed (rare for this component; most tests assert via `screen`).
 */
function renderChip(
  props: { recordId?: string; value?: OutreachStatusValue; disabled?: boolean } = {},
  role: "Admin" | "Contributor" | "Viewer" | null = "Contributor",
) {
  const session = role === null ? null : makeSessionRead({ user: makeUserRead({ role }) });
  return renderWithMockedSession(
    <StatusChip
      recordId={props.recordId ?? RECORD_ID}
      value={props.value ?? "Not Started"}
      disabled={props.disabled}
    />,
    session,
  );
}

/**
 * Per-test capture slot for the most recent PATCH
 * /api/connections/:id/status body observed by an installed MSW
 * handler. Cleared in `beforeEach` so a leak from the previous test
 * cannot pass an assertion vacuously.
 *
 * The shape mirrors the handler's `await request.json()` payload; we
 * type it loosely (Record<string, unknown>) so per-test assertions
 * narrow the type with `toMatchObject`.
 */
let lastStatusPatchBody: Record<string, unknown> | null = null;

/**
 * Counter recording how many times the per-test status PATCH handler
 * was invoked. Tests that need to assert "no request was made"
 * (e.g., the no-op selectOptions case) check this counter rather than
 * relying on MSW's internals.
 */
let statusPatchCallCount = 0;

/**
 * Install a per-test PATCH /api/connections/:id/status handler that
 * captures the request body into `lastStatusPatchBody`, increments
 * `statusPatchCallCount`, and returns a syntactically-valid
 * ConnectionRead with the requested outreach_status. Using
 * `makeConnectionRead` ensures the response satisfies the Zod schema
 * the API client validates against.
 *
 * Returns void; the handler is registered as a side effect via
 * `server.use(...)`.
 */
function installCapturingStatusHandler(): void {
  server.use(
    http.patch<{ id: string }>("*/api/connections/:id/status", async ({ params, request }) => {
      statusPatchCallCount += 1;
      const body = (await request.json()) as Record<string, unknown>;
      lastStatusPatchBody = body;
      const outreachStatus =
        body.outreach_status === "Not Started" ||
        body.outreach_status === "In Progress" ||
        body.outreach_status === "Contacted" ||
        body.outreach_status === "Closed"
          ? (body.outreach_status as OutreachStatusValue)
          : "Not Started";
      return HttpResponse.json(
        makeConnectionRead({
          id: String(params.id),
          outreach_status: outreachStatus,
        }),
      );
    }),
  );
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("<StatusChip />", () => {
  beforeEach(() => {
    // Reset per-test capture state so leaks across tests cannot pass
    // assertions vacuously. The MSW handler set is reset by
    // `afterEach` in tests/setup.ts (server.resetHandlers()), so we do
    // NOT need to re-install the default handler here - the default
    // PATCH /api/connections/:id/status handler from
    // tests/mocks/handlers.ts is already in place at the top of every
    // test in this file.
    lastStatusPatchBody = null;
    statusPatchCallCount = 0;
  });

  // -------------------------------------------------------------------------
  // Phase 2A - Read-only mode (Contributor / no session)
  // -------------------------------------------------------------------------

  describe("Read-only mode (Contributor / null role)", () => {
    it("renders the read-only Badge for a Contributor session", () => {
      renderChip({ recordId: RECORD_ID, value: "Not Started" }, "Contributor");
      // The wrapper span carries the status-chip-readonly-${recordId} testid.
      expect(screen.getByTestId(`status-chip-readonly-${RECORD_ID}`)).toBeInTheDocument();
      // The Contributor must NOT see the editable Select.
      expect(screen.queryByTestId(`status-chip-select-${RECORD_ID}`)).not.toBeInTheDocument();
      expect(screen.queryByTestId(`status-chip-editable-${RECORD_ID}`)).not.toBeInTheDocument();
      // The Badge content includes the textual value.
      expect(screen.getByText("Not Started")).toBeInTheDocument();
    });

    it("renders the read-only Badge when there is no session (null)", () => {
      renderChip({ recordId: RECORD_ID, value: "In Progress" }, null);
      expect(screen.getByTestId(`status-chip-readonly-${RECORD_ID}`)).toBeInTheDocument();
      expect(screen.queryByTestId(`status-chip-select-${RECORD_ID}`)).not.toBeInTheDocument();
      // Native <select> exposes role="combobox"; verify it is absent
      // as a secondary check independent of the consumer testid.
      expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // Phase 2B - Editable mode (Admin / Viewer)
  // -------------------------------------------------------------------------

  describe("Editable mode (Admin / Viewer)", () => {
    it("renders the editable Select for an Admin session", () => {
      renderChip({ recordId: RECORD_ID, value: "Not Started" }, "Admin");
      expect(screen.getByTestId(`status-chip-select-${RECORD_ID}`)).toBeInTheDocument();
      // The read-only fallback must NOT be present.
      expect(screen.queryByTestId(`status-chip-readonly-${RECORD_ID}`)).not.toBeInTheDocument();
    });

    it("renders the editable Select for a Viewer (Sales Rep) session", () => {
      renderChip({ recordId: RECORD_ID, value: "In Progress" }, "Viewer");
      expect(screen.getByTestId(`status-chip-select-${RECORD_ID}`)).toBeInTheDocument();
      expect(screen.queryByTestId(`status-chip-readonly-${RECORD_ID}`)).not.toBeInTheDocument();
    });

    it("editable Select exposes all four F-005 status options", () => {
      renderChip({ recordId: RECORD_ID, value: "Not Started" }, "Admin");
      // Native <select> renders <option> for each entry; getAllByRole
      // is the most robust matcher because it does not depend on any
      // particular DOM nesting strategy.
      const options = screen.getAllByRole("option");
      const labels = options.map((opt) => opt.textContent ?? "");
      expect(options).toHaveLength(4);
      expect(labels).toContain("Not Started");
      expect(labels).toContain("In Progress");
      expect(labels).toContain("Contacted");
      expect(labels).toContain("Closed");
    });

    it("editable Select reflects the current value as the selected option", () => {
      renderChip({ recordId: RECORD_ID, value: "Contacted" }, "Admin");
      const select = screen.getByTestId(`status-chip-select-${RECORD_ID}`) as HTMLSelectElement;
      expect(select.value).toBe("Contacted");
    });
  });

  // -------------------------------------------------------------------------
  // Phase 2C - Status value to Badge variant mapping (read-only mode)
  // -------------------------------------------------------------------------
  //
  // For each of the four F-005 outreach status values, the read-only
  // path must render the appropriate Badge variant. The Badge primitive
  // emits `data-testid="badge-${variant}"`, so we assert on both the
  // outer wrapper (status-chip-readonly-<recordId>) and the inner
  // Badge variant testid to catch regressions in either layer.

  describe("Status value to variant mapping", () => {
    it("'Not Started' renders with the outreach-not-started Badge variant", () => {
      renderChip({ recordId: RECORD_ID, value: "Not Started" }, "Contributor");
      expect(screen.getByTestId(`status-chip-readonly-${RECORD_ID}`)).toBeInTheDocument();
      expect(screen.getByTestId("badge-outreach-not-started")).toBeInTheDocument();
      expect(screen.getByText("Not Started")).toBeInTheDocument();
    });

    it("'In Progress' renders with the outreach-in-progress Badge variant", () => {
      renderChip({ recordId: RECORD_ID, value: "In Progress" }, "Contributor");
      expect(screen.getByTestId(`status-chip-readonly-${RECORD_ID}`)).toBeInTheDocument();
      expect(screen.getByTestId("badge-outreach-in-progress")).toBeInTheDocument();
      expect(screen.getByText("In Progress")).toBeInTheDocument();
    });

    it("'Contacted' renders with the outreach-contacted Badge variant", () => {
      renderChip({ recordId: RECORD_ID, value: "Contacted" }, "Contributor");
      expect(screen.getByTestId(`status-chip-readonly-${RECORD_ID}`)).toBeInTheDocument();
      expect(screen.getByTestId("badge-outreach-contacted")).toBeInTheDocument();
      expect(screen.getByText("Contacted")).toBeInTheDocument();
    });

    it("'Closed' renders with the outreach-closed Badge variant", () => {
      renderChip({ recordId: RECORD_ID, value: "Closed" }, "Contributor");
      expect(screen.getByTestId(`status-chip-readonly-${RECORD_ID}`)).toBeInTheDocument();
      expect(screen.getByTestId("badge-outreach-closed")).toBeInTheDocument();
      expect(screen.getByText("Closed")).toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // Phase 2D - Status mutation happy path (PATCH fired with correct body)
  // -------------------------------------------------------------------------

  describe("Status mutation happy path", () => {
    it("selecting a different value fires PATCH /api/connections/:id/status with the new outreach_status", async () => {
      installCapturingStatusHandler();
      const user = userEvent.setup();
      renderChip({ recordId: RECORD_ID, value: "Not Started" }, "Admin");

      const select = screen.getByTestId(`status-chip-select-${RECORD_ID}`) as HTMLSelectElement;
      await user.selectOptions(select, "In Progress");

      // Wait for the mutation to fire and the handler to capture the
      // request body. Polling via waitFor avoids relying on a fixed
      // sleep duration that could be flaky under CI variance.
      await waitFor(() => {
        expect(statusPatchCallCount).toBe(1);
      });
      expect(lastStatusPatchBody).toMatchObject({
        outreach_status: "In Progress",
      });
    });

    it("selecting the same value (no-op) does NOT fire the mutation", async () => {
      installCapturingStatusHandler();
      const user = userEvent.setup();
      renderChip({ recordId: RECORD_ID, value: "Not Started" }, "Admin");

      const select = screen.getByTestId(`status-chip-select-${RECORD_ID}`) as HTMLSelectElement;
      // Re-selecting the already-selected option exercises the source
      // guard `if (next === value || updateStatus.isPending) return`.
      // Even if userEvent suppresses the change event entirely (the
      // option is already selected), the assertion remains correct:
      // no mutation should be fired.
      await user.selectOptions(select, "Not Started");

      // Brief wait to allow any async mutation to fire if the guard
      // were absent. waitFor with a small expectation that always
      // succeeds gives the event loop a chance to drain.
      await new Promise((resolve) => setTimeout(resolve, 50));
      expect(statusPatchCallCount).toBe(0);
      expect(lastStatusPatchBody).toBeNull();
    });
  });

  // -------------------------------------------------------------------------
  // Phase 2E - Optimistic update with rollback on error
  // -------------------------------------------------------------------------

  describe("Optimistic update + rollback", () => {
    it("a 403 response rolls the local mirror back to the prior value", async () => {
      // Inject a 403 forbidden handler via the overrides namespace so
      // tests stay declarative; the factory builds the standard error
      // envelope so the API client's parser does not throw on shape.
      server.use(overrides.connections.statusForbidden(RECORD_ID));
      const user = userEvent.setup();
      renderChip({ recordId: RECORD_ID, value: "Not Started" }, "Admin");

      const select = screen.getByTestId(`status-chip-select-${RECORD_ID}`) as HTMLSelectElement;
      // Trigger the mutation.
      await user.selectOptions(select, "In Progress");

      // After the 403 settles, the source's onError callback resets
      // the local mirror to the parent `value` ("Not Started"). We
      // poll for that final state via waitFor.
      await waitFor(() => {
        expect(select.value).toBe("Not Started");
      });
    });

    it("the optimistic local mirror reflects the new value while the mutation is pending", async () => {
      // A pending-forever handler holds `updateStatus.isPending=true`,
      // which is the precondition that disables the chip's render-time
      // reconciliation (`if (localValue !== value && !isPending)
      // setLocalValue(value)`). Without isPending=true, the
      // reconciliation would snap localValue back to the parent's
      // (unchanged) `value` prop after the mutation settles, hiding
      // the optimistic effect from this isolated test. Production
      // avoids the snap-back because the parent's TanStack Query
      // cache propagates the new value to its `value` prop in the
      // same paint cycle as the mutation settles.
      server.use(http.patch("*/api/connections/:id/status", () => new Promise(() => {})));
      const user = userEvent.setup();
      renderChip({ recordId: RECORD_ID, value: "Not Started" }, "Admin");

      const select = screen.getByTestId(`status-chip-select-${RECORD_ID}`) as HTMLSelectElement;
      // Trigger the mutation; handleChange updates the local mirror
      // synchronously and dispatches mutate(). Because the handler
      // never resolves, the chip stays in the pending state for the
      // remainder of the test so the optimistic local value persists.
      await user.selectOptions(select, "Closed");

      await waitFor(() => {
        expect(select.value).toBe("Closed");
      });
    });

    it("a successful mutation settles cleanly (request sent, spinner removed)", async () => {
      // This test verifies the success branch end-to-end: the PATCH
      // request is captured with the correct body, the spinner appears
      // and then is removed when the mutation settles, and no error
      // is surfaced. We do NOT assert that select.value remains the
      // newly-selected option - in this isolated render the parent's
      // `value` prop is fixed (production would observe the cache
      // update via useConnectionQuery and re-render with the new
      // value), so the chip's render-time reconciliation correctly
      // resets the local mirror to the parent's value once the
      // mutation settles. That reconciliation is exactly the
      // 'Local mirror state sync' suite below.
      installCapturingStatusHandler();
      const user = userEvent.setup();
      renderChip({ recordId: RECORD_ID, value: "Not Started" }, "Admin");

      const select = screen.getByTestId(`status-chip-select-${RECORD_ID}`) as HTMLSelectElement;
      await user.selectOptions(select, "Closed");

      // The PATCH was made with the new outreach_status.
      await waitFor(() => {
        expect(statusPatchCallCount).toBe(1);
      });
      expect(lastStatusPatchBody).toMatchObject({ outreach_status: "Closed" });

      // After settlement the spinner is removed (proving the mutation
      // completed; if the response shape were rejected by the API
      // client's Zod parser, isPending would settle but with an error
      // and the spinner would also be removed - we cover that path in
      // the rollback test above).
      await waitFor(() => {
        expect(screen.queryByTestId("status-chip-spinner")).not.toBeInTheDocument();
      });
    });
  });

  // -------------------------------------------------------------------------
  // Phase 2F - Pending mutation UI (Loader2 spinner)
  // -------------------------------------------------------------------------

  describe("Pending mutation UI (spinner)", () => {
    it("the Loader2 spinner appears while the mutation is in flight", async () => {
      // A handler that never resolves keeps the mutation in the
      // pending state indefinitely. The new Promise(() => {}) idiom
      // is the canonical "pending forever" pattern for MSW v2.
      server.use(http.patch("*/api/connections/:id/status", () => new Promise(() => {})));
      const user = userEvent.setup();
      renderChip({ recordId: RECORD_ID, value: "Not Started" }, "Admin");

      const select = screen.getByTestId(`status-chip-select-${RECORD_ID}`) as HTMLSelectElement;
      await user.selectOptions(select, "In Progress");

      // The spinner is rendered conditionally on
      // `updateStatus.isPending`. Wait for it to appear.
      const spinner = await screen.findByTestId("status-chip-spinner");
      expect(spinner).toBeInTheDocument();
      expect(spinner).toHaveAttribute("aria-hidden", "true");
      // Tailwind's `animate-spin` class is documented as the spin
      // animation; assert it is present so we catch theme regressions.
      expect(spinner).toHaveClass("animate-spin");
    });

    it("the spinner disappears after the mutation settles successfully", async () => {
      installCapturingStatusHandler();
      const user = userEvent.setup();
      renderChip({ recordId: RECORD_ID, value: "Not Started" }, "Admin");

      const select = screen.getByTestId(`status-chip-select-${RECORD_ID}`) as HTMLSelectElement;
      await user.selectOptions(select, "In Progress");

      // Wait for the request to actually fire so we know the mutation
      // entered the pending state. Then poll until it settles - the
      // spinner is removed when isPending transitions to false.
      await waitFor(() => {
        expect(statusPatchCallCount).toBe(1);
      });
      await waitFor(() => {
        expect(screen.queryByTestId("status-chip-spinner")).not.toBeInTheDocument();
      });
    });

    it("the Select is disabled while the mutation is in flight", async () => {
      server.use(http.patch("*/api/connections/:id/status", () => new Promise(() => {})));
      const user = userEvent.setup();
      renderChip({ recordId: RECORD_ID, value: "Not Started" }, "Admin");

      const select = screen.getByTestId(`status-chip-select-${RECORD_ID}`) as HTMLSelectElement;
      await user.selectOptions(select, "In Progress");

      // The source applies `disabled={disabled || updateStatus.isPending}`.
      // Wait for the pending state to propagate, then assert disabled.
      await waitFor(() => {
        expect(select).toBeDisabled();
      });
    });
  });

  // -------------------------------------------------------------------------
  // Phase 2G - Disabled prop (soft-deleted records)
  // -------------------------------------------------------------------------

  describe("Disabled prop (soft-deleted records)", () => {
    it("disabled=true disables the editable Select for an Admin", () => {
      renderChip({ recordId: RECORD_ID, value: "Not Started", disabled: true }, "Admin");
      const select = screen.getByTestId(`status-chip-select-${RECORD_ID}`) as HTMLSelectElement;
      expect(select).toBeDisabled();
    });

    it("disabled defaults to false (Select is enabled when omitted)", () => {
      renderChip({ recordId: RECORD_ID, value: "Not Started" }, "Admin");
      const select = screen.getByTestId(`status-chip-select-${RECORD_ID}`) as HTMLSelectElement;
      expect(select).not.toBeDisabled();
    });

    it("disabled prop has no visible effect on the read-only Badge fallback", () => {
      renderChip({ recordId: RECORD_ID, value: "Contacted", disabled: true }, "Contributor");
      // The read-only path renders a Badge, not an interactive
      // control. The chip should still render with the value text and
      // the read-only wrapper testid; there is no Select to disable.
      expect(screen.getByTestId(`status-chip-readonly-${RECORD_ID}`)).toBeInTheDocument();
      expect(screen.getByText("Contacted")).toBeInTheDocument();
      expect(screen.queryByTestId(`status-chip-select-${RECORD_ID}`)).not.toBeInTheDocument();
    });

    it("an Admin attempting to mutate a disabled chip does NOT fire the mutation", async () => {
      installCapturingStatusHandler();
      const user = userEvent.setup();
      renderChip({ recordId: RECORD_ID, value: "Not Started", disabled: true }, "Admin");

      const select = screen.getByTestId(`status-chip-select-${RECORD_ID}`) as HTMLSelectElement;
      // Native <select> with `disabled` rejects userEvent interactions.
      // user-event v14 throws when targeting a disabled control; we
      // suppress that error since the UX-level assertion is "no
      // mutation fires", not "the interaction succeeds".
      try {
        await user.selectOptions(select, "In Progress");
      } catch {
        // Expected: userEvent refuses to interact with disabled controls.
      }
      // Brief wait to allow any out-of-band mutation to drain.
      await new Promise((resolve) => setTimeout(resolve, 50));
      expect(statusPatchCallCount).toBe(0);
    });
  });

  // -------------------------------------------------------------------------
  // Phase 2H - Click stopPropagation (parent row click handler)
  // -------------------------------------------------------------------------

  describe("Click event stopPropagation", () => {
    it("clicking the chip wrapper does NOT trigger a parent onClick handler", async () => {
      const parentClickSpy = vi.fn();
      const user = userEvent.setup();
      const session = makeSessionRead({ user: makeUserRead({ role: "Admin" }) });
      renderWithMockedSession(
        <div onClick={parentClickSpy} data-testid="parent-row">
          <StatusChip recordId={RECORD_ID} value="Not Started" />
        </div>,
        session,
      );

      // The editable wrapper span carries `status-chip-editable-${recordId}`
      // and the onClick handler that calls event.stopPropagation. A
      // click anywhere inside that wrapper should be absorbed.
      const wrapper = screen.getByTestId(`status-chip-editable-${RECORD_ID}`);
      await user.click(wrapper);
      expect(parentClickSpy).not.toHaveBeenCalled();
    });

    it("clicking a sibling outside the chip DOES bubble to the parent (control)", async () => {
      // Sanity-check the propagation contract: a click on a sibling
      // element at the same level (NOT inside the chip wrapper) must
      // still reach the parent. This guards against an over-broad
      // stopPropagation regression that would silence the entire row.
      const parentClickSpy = vi.fn();
      const user = userEvent.setup();
      const session = makeSessionRead({ user: makeUserRead({ role: "Admin" }) });
      renderWithMockedSession(
        <div onClick={parentClickSpy} data-testid="parent-row">
          <StatusChip recordId={RECORD_ID} value="Not Started" />
          <button data-testid="sibling-btn" type="button">
            sibling
          </button>
        </div>,
        session,
      );

      const sibling = screen.getByTestId("sibling-btn");
      await user.click(sibling);
      // The sibling click bubbles up to the parent's onClick
      // unobstructed (StatusChip does not capture document-level
      // events; only its own wrapper).
      expect(parentClickSpy).toHaveBeenCalled();
    });
  });

  // -------------------------------------------------------------------------
  // Phase 2I - Local mirror state sync (parent value -> internal mirror)
  // -------------------------------------------------------------------------
  //
  // The chip maintains an internal `localValue` mirror so the picker
  // reflects the chosen value before the parent re-renders with the
  // optimistically-updated cache. When the parent passes a new value
  // prop and no mutation is in flight, the source's render-time
  // reconciliation:
  //
  //   if (localValue !== value && !updateStatus.isPending) {
  //     setLocalValue(value);
  //   }
  //
  // ...synchronizes the mirror to the new external value. This test
  // exercises that path via a controlled Harness component.

  describe("Local mirror state sync", () => {
    it("an external value prop change syncs into the local mirror when not pending", async () => {
      /**
       * Controlled Harness: owns a `val` state, renders the chip
       * bound to it, and provides a sibling button to flip the value
       * to "Contacted". Letting the harness own the state mirrors the
       * production data flow (parent feed/detail query owns the
       * record's status) without coupling the test to TanStack Query
       * internals. The return type is inferred so the harness stays
       * portable across React 18/19 JSX namespace variants.
       */
      function Harness() {
        const [val, setVal] = useState<OutreachStatusValue>("Not Started");
        return (
          <>
            <StatusChip recordId={RECORD_ID} value={val} />
            <button type="button" data-testid="external-change" onClick={() => setVal("Contacted")}>
              change externally
            </button>
          </>
        );
      }

      const user = userEvent.setup();
      const session = makeSessionRead({ user: makeUserRead({ role: "Admin" }) });
      renderWithMockedSession(<Harness />, session);

      const select = screen.getByTestId(`status-chip-select-${RECORD_ID}`) as HTMLSelectElement;
      // Initial state: parent val = "Not Started", local mirror
      // synced to the same.
      expect(select.value).toBe("Not Started");

      // External change: the harness button flips val to "Contacted".
      // Because no mutation is in flight (isPending=false), the
      // chip's render-time reconciliation should fire setLocalValue
      // and the Select should reflect the new value after re-render.
      await user.click(screen.getByTestId("external-change"));

      await waitFor(() => {
        expect(select.value).toBe("Contacted");
      });
    });
  });
});
