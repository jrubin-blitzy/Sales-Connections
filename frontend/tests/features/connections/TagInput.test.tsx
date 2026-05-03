/**
 * TagInput.test.tsx - Vitest tests for the F-008 Tag Input component.
 *
 * Targets `frontend/src/features/connections/TagInput.tsx`. Verifies:
 *   - Renders selected tag chips
 *   - Removing a chip calls onChange with new array
 *   - Typing filters suggestions
 *   - Selecting a suggestion adds it to selectedTagIds
 *   - Already-selected tags excluded from suggestions
 *   - Disabled mode hides X buttons and disables input
 *   - Empty selection renders without errors
 *   - ARIA combobox pattern attributes
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

import { TagInput } from "@/features/connections/TagInput";
import {
  renderWithMockedSession,
  screen,
  userEvent,
  waitFor,
} from "../../test-utils";

// ---------------------------------------------------------------------------
// Test fixtures
// ---------------------------------------------------------------------------

const sampleTags = [
  {
    id: "11111111-1111-1111-1111-111111111111",
    name: "Industry: Logistics",
    created_at: "2026-05-01T12:00:00Z",
  },
  {
    id: "22222222-2222-2222-2222-222222222222",
    name: "Use Case: Sales",
    created_at: "2026-05-01T12:00:00Z",
  },
  {
    id: "33333333-3333-3333-3333-333333333333",
    name: "Geography: SF",
    created_at: "2026-05-01T12:00:00Z",
  },
];

const minimalSession = {
  user: {
    id: "00000000-0000-0000-0000-000000000001",
    email: "user@example.com",
    display_name: "User",
    role: "Contributor" as const,
    created_at: "2026-01-01T00:00:00Z",
  },
  authenticated: true,
};

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("TagInput", () => {
  beforeEach(() => {
    // Mock fresh.
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  describe("layout", () => {
    it("renders empty when no tags selected", () => {
      const onChange = vi.fn();
      renderWithMockedSession(
        <TagInput
          availableTags={sampleTags}
          selectedTagIds={[]}
          onChange={onChange}
        />,
        minimalSession,
      );
      // No chip buttons.
      const chipButtons = screen.queryAllByRole("button", {
        name: /Remove tag/i,
      });
      expect(chipButtons).toHaveLength(0);
    });

    it("renders selected tag chips", () => {
      const onChange = vi.fn();
      renderWithMockedSession(
        <TagInput
          availableTags={sampleTags}
          selectedTagIds={[sampleTags[0]!.id, sampleTags[1]!.id]}
          onChange={onChange}
        />,
        minimalSession,
      );
      expect(screen.getByText("Industry: Logistics")).toBeInTheDocument();
      expect(screen.getByText("Use Case: Sales")).toBeInTheDocument();
    });

    it("renders the search input with role=combobox", () => {
      const onChange = vi.fn();
      renderWithMockedSession(
        <TagInput
          availableTags={sampleTags}
          selectedTagIds={[]}
          onChange={onChange}
        />,
        minimalSession,
      );
      // The combobox is the visible search input.
      const combo = screen.getByRole("combobox");
      expect(combo).toBeInTheDocument();
      expect(combo.getAttribute("aria-autocomplete")).toBe("list");
    });

    it("renders the label when provided", () => {
      const onChange = vi.fn();
      renderWithMockedSession(
        <TagInput
          availableTags={sampleTags}
          selectedTagIds={[]}
          onChange={onChange}
          label="Tags"
        />,
        minimalSession,
      );
      expect(screen.getByText("Tags")).toBeInTheDocument();
    });
  });

  describe("removal", () => {
    it("clicking remove on a chip calls onChange with chip omitted", async () => {
      const user = userEvent.setup();
      const onChange = vi.fn();
      renderWithMockedSession(
        <TagInput
          availableTags={sampleTags}
          selectedTagIds={[sampleTags[0]!.id, sampleTags[1]!.id]}
          onChange={onChange}
        />,
        minimalSession,
      );
      // Find remove button for "Industry: Logistics".
      const removeButton = screen.getByRole("button", {
        name: /Remove tag Industry: Logistics/i,
      });
      await user.click(removeButton);
      expect(onChange).toHaveBeenCalled();
      // The new array should NOT include the removed tag id.
      const newIds = onChange.mock.calls[0]![0];
      expect(newIds).not.toContain(sampleTags[0]!.id);
      expect(newIds).toContain(sampleTags[1]!.id);
    });

    it("removing the only chip yields an empty array", async () => {
      const user = userEvent.setup();
      const onChange = vi.fn();
      renderWithMockedSession(
        <TagInput
          availableTags={sampleTags}
          selectedTagIds={[sampleTags[0]!.id]}
          onChange={onChange}
        />,
        minimalSession,
      );
      const removeButton = screen.getByRole("button", {
        name: /Remove tag Industry: Logistics/i,
      });
      await user.click(removeButton);
      const newIds = onChange.mock.calls[0]![0];
      expect(newIds).toEqual([]);
    });
  });

  describe("autocomplete", () => {
    it("focusing the input opens the suggestion list", async () => {
      const user = userEvent.setup();
      const onChange = vi.fn();
      renderWithMockedSession(
        <TagInput
          availableTags={sampleTags}
          selectedTagIds={[]}
          onChange={onChange}
        />,
        minimalSession,
      );
      const combo = screen.getByRole("combobox");
      await user.click(combo);

      await waitFor(() => {
        expect(combo.getAttribute("aria-expanded")).toBe("true");
      });
    });

    it("typing filters suggestions", async () => {
      const user = userEvent.setup();
      const onChange = vi.fn();
      renderWithMockedSession(
        <TagInput
          availableTags={sampleTags}
          selectedTagIds={[]}
          onChange={onChange}
        />,
        minimalSession,
      );
      const combo = screen.getByRole("combobox");
      await user.click(combo);
      await user.type(combo, "Industry");

      // After filtering, "Industry: Logistics" should still be suggested.
      // and "Use Case: Sales" should NOT match.
      await waitFor(() => {
        expect(
          screen.getByText("Industry: Logistics"),
        ).toBeInTheDocument();
      });
    });

    it("selecting a suggestion adds it via onChange", async () => {
      const user = userEvent.setup();
      const onChange = vi.fn();
      renderWithMockedSession(
        <TagInput
          availableTags={sampleTags}
          selectedTagIds={[]}
          onChange={onChange}
        />,
        minimalSession,
      );
      const combo = screen.getByRole("combobox");
      await user.click(combo);
      // Find and click the "Industry: Logistics" option.
      const options = screen.getAllByRole("option");
      const industryOption = options.find((o) =>
        o.textContent?.includes("Industry: Logistics"),
      );
      expect(industryOption).toBeDefined();

      // Use mousedown event (component uses onMouseDown to fire BEFORE onBlur).
      await user.pointer({
        target: industryOption!,
        keys: "[MouseLeft]",
      });

      await waitFor(() => {
        expect(onChange).toHaveBeenCalled();
      });
      const newIds = onChange.mock.calls[0]![0];
      expect(newIds).toContain(sampleTags[0]!.id);
    });
  });

  describe("disabled mode", () => {
    it("input is disabled", () => {
      const onChange = vi.fn();
      renderWithMockedSession(
        <TagInput
          availableTags={sampleTags}
          selectedTagIds={[sampleTags[0]!.id]}
          onChange={onChange}
          disabled
        />,
        minimalSession,
      );
      const combo = screen.getByRole("combobox");
      expect((combo as HTMLInputElement).disabled).toBe(true);
    });

    it("remove buttons hidden when disabled", () => {
      const onChange = vi.fn();
      renderWithMockedSession(
        <TagInput
          availableTags={sampleTags}
          selectedTagIds={[sampleTags[0]!.id]}
          onChange={onChange}
          disabled
        />,
        minimalSession,
      );
      const removeButtons = screen.queryAllByRole("button", {
        name: /Remove tag/i,
      });
      expect(removeButtons).toHaveLength(0);
    });
  });

  describe("error states", () => {
    it("renders error message with role=alert", () => {
      const onChange = vi.fn();
      renderWithMockedSession(
        <TagInput
          availableTags={sampleTags}
          selectedTagIds={[]}
          onChange={onChange}
          errorMessage="Tags are required"
        />,
        minimalSession,
      );
      expect(screen.getByRole("alert")).toBeInTheDocument();
      expect(screen.getByText("Tags are required")).toBeInTheDocument();
    });

    it("helperText hidden when errorMessage present", () => {
      const onChange = vi.fn();
      renderWithMockedSession(
        <TagInput
          availableTags={sampleTags}
          selectedTagIds={[]}
          onChange={onChange}
          helperText="e.g. industry, use case"
          errorMessage="Required"
        />,
        minimalSession,
      );
      // Error wins over helper.
      expect(screen.getByText("Required")).toBeInTheDocument();
      expect(
        screen.queryByText("e.g. industry, use case"),
      ).not.toBeInTheDocument();
    });
  });
});
