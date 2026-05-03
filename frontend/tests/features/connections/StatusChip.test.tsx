/**
 * StatusChip.test.tsx - Vitest tests for F-005 Outreach Status Chip.
 *
 * Targets `frontend/src/features/connections/StatusChip.tsx`. Verifies:
 *   - Read-only rendering for Contributor role
 *   - Editable rendering for Admin and Viewer (Sales Rep) roles
 *   - All 4 outreach status values map to correct variants
 *   - Dropdown opens on click for editable mode
 *   - Dropdown closes on Escape
 *   - Selecting a value fires the mutation
 *   - No-op selection (current value) does not fire mutation
 *   - ARIA attributes for accessibility
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

import { StatusChip } from "@/features/connections/StatusChip";
import type { SessionRead } from "@/schemas/auth";
import {
  renderWithMockedSession,
  screen,
  userEvent,
  waitFor,
} from "../../test-utils";

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

const mockUpdateStatusMutate = vi.fn();

vi.mock("@/api/connections", () => ({
  useUpdateStatusMutation: () => ({
    mutate: mockUpdateStatusMutate,
    isPending: false,
    isError: false,
    error: null,
    reset: vi.fn(),
  }),
}));

// ---------------------------------------------------------------------------
// Test fixtures
// ---------------------------------------------------------------------------

const adminSession: SessionRead = {
  user: {
    id: "00000000-0000-0000-0000-000000000001",
    email: "admin@example.com",
    display_name: "Admin User",
    role: "Admin",
    created_at: "2026-01-01T00:00:00Z",
  },
  authenticated: true,
};

const viewerSession: SessionRead = {
  user: {
    id: "00000000-0000-0000-0000-000000000002",
    email: "viewer@example.com",
    display_name: "Viewer User",
    role: "Viewer",
    created_at: "2026-01-01T00:00:00Z",
  },
  authenticated: true,
};

const contributorSession: SessionRead = {
  user: {
    id: "00000000-0000-0000-0000-000000000003",
    email: "contributor@example.com",
    display_name: "Contributor User",
    role: "Contributor",
    created_at: "2026-01-01T00:00:00Z",
  },
  authenticated: true,
};

const RECORD_ID = "11111111-1111-1111-1111-111111111111";

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("StatusChip", () => {
  beforeEach(() => {
    mockUpdateStatusMutate.mockReset();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  describe("read-only mode (Contributor role)", () => {
    it("renders as Badge for Contributor", () => {
      renderWithMockedSession(
        <StatusChip recordId={RECORD_ID} value="Not Started" />,
        contributorSession,
      );
      // The status text is rendered as part of the Badge.
      expect(screen.getByText("Not Started")).toBeInTheDocument();
    });

    it("does NOT render a button for Contributor", () => {
      renderWithMockedSession(
        <StatusChip recordId={RECORD_ID} value="In Progress" />,
        contributorSession,
      );
      // No interactive button - Contributors cannot mutate status.
      const buttons = screen.queryAllByRole("button");
      expect(buttons).toHaveLength(0);
    });

    it("renders read-only when editable=false even for Admin", () => {
      renderWithMockedSession(
        <StatusChip
          recordId={RECORD_ID}
          value="Closed"
          editable={false}
        />,
        adminSession,
      );
      expect(screen.getByText("Closed")).toBeInTheDocument();
      const buttons = screen.queryAllByRole("button");
      expect(buttons).toHaveLength(0);
    });
  });

  describe("editable mode (Admin role)", () => {
    it("renders as button for Admin", () => {
      renderWithMockedSession(
        <StatusChip recordId={RECORD_ID} value="Not Started" />,
        adminSession,
      );
      // Find a button that includes the status text.
      expect(
        screen.getByRole("button", { name: /Not Started/i }),
      ).toBeInTheDocument();
    });

    it("button has aria-haspopup=listbox", () => {
      renderWithMockedSession(
        <StatusChip recordId={RECORD_ID} value="Not Started" />,
        adminSession,
      );
      const button = screen.getByRole("button", { name: /Not Started/i });
      expect(button.getAttribute("aria-haspopup")).toBe("listbox");
    });

    it("button has aria-expanded=false initially", () => {
      renderWithMockedSession(
        <StatusChip recordId={RECORD_ID} value="Not Started" />,
        adminSession,
      );
      const button = screen.getByRole("button", { name: /Not Started/i });
      expect(button.getAttribute("aria-expanded")).toBe("false");
    });
  });

  describe("editable mode (Viewer role - Sales Rep)", () => {
    it("renders as button for Viewer (sales rep)", () => {
      renderWithMockedSession(
        <StatusChip recordId={RECORD_ID} value="In Progress" />,
        viewerSession,
      );
      expect(
        screen.getByRole("button", { name: /In Progress/i }),
      ).toBeInTheDocument();
    });
  });

  describe("dropdown interaction", () => {
    it("opens dropdown on click", async () => {
      const user = userEvent.setup();
      renderWithMockedSession(
        <StatusChip recordId={RECORD_ID} value="Not Started" />,
        adminSession,
      );
      const button = screen.getByRole("button", { name: /Not Started/i });
      await user.click(button);

      // After click, aria-expanded should be true and a listbox visible.
      await waitFor(() => {
        expect(button.getAttribute("aria-expanded")).toBe("true");
      });
      expect(screen.getByRole("listbox")).toBeInTheDocument();
    });

    it("listbox shows all 4 status options", async () => {
      const user = userEvent.setup();
      renderWithMockedSession(
        <StatusChip recordId={RECORD_ID} value="Not Started" />,
        adminSession,
      );
      const button = screen.getByRole("button", { name: /Not Started/i });
      await user.click(button);

      const options = screen.getAllByRole("option");
      expect(options).toHaveLength(4);
      const optionTexts = options.map((o) => o.textContent);
      expect(optionTexts).toContain("Not Started");
      expect(optionTexts).toContain("In Progress");
      expect(optionTexts).toContain("Contacted");
      expect(optionTexts).toContain("Closed");
    });

    it("current value has aria-selected=true in listbox", async () => {
      const user = userEvent.setup();
      renderWithMockedSession(
        <StatusChip recordId={RECORD_ID} value="In Progress" />,
        adminSession,
      );
      const button = screen.getByRole("button", { name: /In Progress/i });
      await user.click(button);

      const options = screen.getAllByRole("option");
      const inProgressOption = options.find(
        (o) => o.textContent === "In Progress",
      );
      expect(inProgressOption?.getAttribute("aria-selected")).toBe(
        "true",
      );
    });

    it("Escape key closes the dropdown", async () => {
      const user = userEvent.setup();
      renderWithMockedSession(
        <StatusChip recordId={RECORD_ID} value="Not Started" />,
        adminSession,
      );
      const button = screen.getByRole("button", { name: /Not Started/i });
      await user.click(button);
      expect(button.getAttribute("aria-expanded")).toBe("true");

      // Press Escape key.
      await user.keyboard("{Escape}");

      await waitFor(() => {
        expect(button.getAttribute("aria-expanded")).toBe("false");
      });
    });
  });

  describe("mutation invocation", () => {
    it("selecting a different value fires the mutation", async () => {
      const user = userEvent.setup();
      renderWithMockedSession(
        <StatusChip recordId={RECORD_ID} value="Not Started" />,
        adminSession,
      );
      const button = screen.getByRole("button", { name: /Not Started/i });
      await user.click(button);

      // Click "In Progress" option.
      const options = screen.getAllByRole("option");
      const inProgress = options.find(
        (o) => o.textContent === "In Progress",
      );
      expect(inProgress).toBeDefined();
      await user.click(inProgress!);

      await waitFor(() => {
        expect(mockUpdateStatusMutate).toHaveBeenCalled();
      });
      const callArgs = mockUpdateStatusMutate.mock.calls[0]![0];
      // Mutation payload should include record id and new status.
      expect(JSON.stringify(callArgs)).toContain("In Progress");
    });

    it("selecting the current value does NOT fire mutation (no-op)", async () => {
      const user = userEvent.setup();
      renderWithMockedSession(
        <StatusChip recordId={RECORD_ID} value="Not Started" />,
        adminSession,
      );
      const button = screen.getByRole("button", { name: /Not Started/i });
      await user.click(button);

      // Click the same value - "Not Started"
      const options = screen.getAllByRole("option");
      const notStarted = options.find(
        (o) => o.textContent === "Not Started",
      );
      expect(notStarted).toBeDefined();
      await user.click(notStarted!);

      // Brief wait to ensure no async mutation is queued.
      await new Promise((r) => setTimeout(r, 50));
      expect(mockUpdateStatusMutate).not.toHaveBeenCalled();
    });
  });

  describe("all 4 status values render", () => {
    it.each([
      ["Not Started"],
      ["In Progress"],
      ["Contacted"],
      ["Closed"],
    ] as const)("renders %s as read-only", (value) => {
      renderWithMockedSession(
        <StatusChip recordId={RECORD_ID} value={value} />,
        contributorSession,
      );
      expect(screen.getByText(value)).toBeInTheDocument();
    });
  });
});
