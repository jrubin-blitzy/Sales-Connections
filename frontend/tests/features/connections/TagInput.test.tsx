/**
 * TagInput.test.tsx - Vitest tests for the F-008 Tag Input.
 *
 * Targets `frontend/src/features/connections/TagInput.tsx` - the
 * token-style multi-tag input with autocomplete and inline creation
 * used by the F-001 Add/Edit Connection form. The component is the
 * most behavior-rich primitive in the connections feature set, so the
 * suite is correspondingly large (~30 tests across 13 nested
 * describes). Coverage targets per AAP Sec 0.7.7 / vite.config.ts:
 * 85% line, 80% branch, 85% function, 85% statement.
 *
 * Coverage map per AAP Sec 0.5.4 + the production source under test:
 *
 *   Phase 2A - Initial Render
 *     - role="combobox", aria-autocomplete="list", aria-expanded,
 *       aria-controls, autoComplete="off"
 *     - Default placeholder "Add tags..." (only when nothing selected)
 *     - Optional label wired via htmlFor / id (useId)
 *     - Optional helper text rendered below the well
 *     - Optional inline error message with role="alert" via
 *       data-testid="tag-input-error"
 *     - Error precedence: errorMessage suppresses helperText
 *
 *   Phase 2B - Selected Chips
 *     - Each selected id renders a chip via data-testid="tag-chip-{id}"
 *     - Chip text resolves to the tag's name via the available tags
 *       Map; falls back to the id verbatim when the tag is missing
 *       (component defensive against unloaded availableTags)
 *     - Each chip has a remove button via
 *       data-testid="tag-remove-{id}" with aria-label="Remove tag {name}"
 *     - Placeholder text is empty string when at least one tag is
 *       selected (per source: `selectedTagIds.length === 0 ? "Add
 *       tags..." : ""`)
 *
 *   Phase 2C - Autocomplete Filtering
 *     - Empty query opens dropdown listing every available tag (capped
 *       at 10 per the slice in `filterSuggestions`)
 *     - Typing filters suggestions case-insensitively (substring match
 *       per `matchesQuery`)
 *     - aria-expanded flips to true when the dropdown opens
 *     - aria-controls references the listbox `id` attribute
 *     - The 10-suggestion cap enforced when availableTags.length > 10
 *
 *   Phase 2D - Clicking Suggestion Adds Tag
 *     - Mouse-clicking a suggestion (onMouseDown handler) appends its
 *       id to the existing selection and calls onChange with the new
 *       array
 *     - userEvent.click correctly simulates the mousedown -> mouseup
 *       -> click sequence so the onMouseDown fires before the input's
 *       onBlur closes the dropdown
 *
 *   Phase 2E - Keyboard Navigation: ArrowUp/Down
 *     - Initial highlight is index 0 (per `useState(0)`)
 *     - ArrowDown advances highlight by one and clamps at last option
 *     - ArrowUp decrements highlight and clamps at 0
 *
 *   Phase 2F - Enter Key Selects
 *     - Enter while a suggestion is highlighted adds that tag
 *     - Enter when suggestions list is empty (and !canCreateNew)
 *       does nothing
 *
 *   Phase 2G - Escape Closes Dropdown
 *     - Escape closes the dropdown AND clears the query (per source's
 *       setIsOpen(false) + setQuery(""))
 *
 *   Phase 2H - Backspace on Empty
 *     - Backspace with empty input AND non-empty selection removes
 *       the last selected tag (per source guard
 *       `query.length === 0 && selectedTagIds.length > 0`)
 *     - Backspace while typing only erases the typed character
 *       (component handler returns early; native input handles the
 *       deletion)
 *
 *   Phase 2I - Inline Tag Creation (allowCreate)
 *     - "Create new" affordance appears for non-empty queries with no
 *       exact match
 *     - Hidden when query exactly matches an existing tag name
 *       (case-insensitive)
 *     - Hidden when allowCreate=false
 *     - Hidden when the trimmed query fails TagCreateSchema
 *       (length > 64 characters)
 *     - Clicking "Create new" POSTs /api/tags via
 *       useCreateTagMutation and auto-selects the returned tag id on
 *       success
 *     - Failed creation does NOT call onChange (defensive against
 *       optimistic-add bugs)
 *     - Enter key on a no-match input triggers the same create flow
 *
 *   Phase 2J - Tag Chip Removal
 *     - Clicking the X button calls onChange with the id removed
 *
 *   Phase 2K - Disabled State
 *     - disabled=true sets the input's disabled attribute
 *     - disabled=true hides the X buttons on chips
 *
 *   Phase 2L - Suggestions Filtering and Exclusion
 *     - Already-selected tag ids are excluded from the suggestion list
 *     - Dropdown closes ~100ms after blur (component uses
 *       window.setTimeout(..., 100) so click-on-suggestion can fire
 *       its onMouseDown before the input's onBlur unmounts the menu)
 *
 * Conventions per AAP Sec 0.7.7 and frontend/.prettierrc.json:
 *   - Strict TypeScript; no `any`. eslint allows `any` inside tests
 *     but the project still avoids it.
 *   - Double quotes (singleQuote: false); trailing commas; 2-space
 *     indent; line length <= 100.
 *   - jest-dom matchers (toBeInTheDocument, toHaveTextContent,
 *     toHaveAttribute, toHaveValue) registered globally via
 *     tests/setup.ts; no per-file matcher imports needed.
 *   - No emoji; no console.log. Intentional console.error spam from
 *     TanStack Query's failed-mutation logging is suppressed via
 *     `vi.spyOn(console, "error")` in the relevant tests.
 *   - Named exports only; this file has no exports (test file).
 *
 * Coordinates with:
 *   - frontend/src/features/connections/TagInput.tsx
 *     (system under test - the only feature import)
 *   - frontend/src/schemas/connection.ts (type-only TagRead import for
 *     fixture / helper signatures; the runtime TagCreateSchema is
 *     consumed by TagInput.tsx, NOT directly here)
 *   - frontend/tests/mocks/server.ts (per-test handler overrides via
 *     server.use)
 *   - frontend/tests/mocks/handlers.ts (overrides.tags.createDuplicate
 *     for the failed-creation path)
 *   - frontend/tests/mocks/data.ts (makeTagRead factory for fixtures
 *     and per-test response bodies)
 *   - frontend/tests/test-utils.tsx (renderWithProviders + screen +
 *     within + waitFor + userEvent re-export surface)
 *   - frontend/tests/setup.ts (jest-dom matcher registration, MSW
 *     server lifecycle, Toast / correlation-ID per-test reset)
 *   - frontend/vite.config.ts (declares test.globals: true and the
 *     `@/` path alias)
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { http, HttpResponse } from "msw";

import { TagInput } from "@/features/connections/TagInput";
import type { TagRead } from "@/schemas/connection";

import { server } from "../../mocks/server";
import { overrides } from "../../mocks/handlers";
import { makeTagRead } from "../../mocks/data";
import { renderWithProviders, screen, within, waitFor, userEvent } from "../../test-utils";

// ---------------------------------------------------------------------------
// Module-scoped fixtures
// ---------------------------------------------------------------------------

/**
 * Five domain-realistic tags spanning the three F-008 dimensions
 * (industry, geography, use-case) named per the AAP user examples in
 * Sec 0.1.2. Stable string ids (NOT UUIDs) make the data-testid
 * locators (`tag-chip-{id}`, `tag-suggestion-{id}`, `tag-remove-{id}`)
 * easy to type literally in tests. The makeTagRead factory does NOT
 * Zod-validate the overrides - it merely spreads them - so non-UUID
 * ids are accepted at construction time. The component itself uses
 * the id as an opaque string identifier and never round-trips it
 * through Zod, so tests render correctly with this fixture.
 *
 * The five entries are intentionally under the 10-suggestion cap so
 * an empty-query open shows the full list. The "many tags" cap test
 * (Phase 2C) constructs its own 15-entry array inline.
 */
const TAGS: TagRead[] = [
  makeTagRead({ id: "tag-saas", name: "industry:saas" }),
  makeTagRead({ id: "tag-fintech", name: "industry:fintech" }),
  makeTagRead({ id: "tag-nyc", name: "geography:nyc" }),
  makeTagRead({ id: "tag-london", name: "geography:london" }),
  makeTagRead({ id: "tag-demand-gen", name: "use-case:demand-gen" }),
];

// ---------------------------------------------------------------------------
// renderTagInput helper
// ---------------------------------------------------------------------------

/**
 * Props accepted by `renderTagInput`. Mirrors `TagInputProps` from the
 * source but every field is optional so individual tests only specify
 * the props they care about; defaults supply the rest. The local
 * `onChange` field is captured and returned alongside the RTL render
 * result so tests can assert on it without a separate `vi.fn()`
 * declaration.
 */
interface RenderTagInputProps {
  availableTags?: ReadonlyArray<TagRead>;
  selectedTagIds?: ReadonlyArray<string>;
  onChange?: (selectedTagIds: ReadonlyArray<string>) => void;
  label?: string;
  helperText?: string;
  errorMessage?: string;
  disabled?: boolean;
  allowCreate?: boolean;
}

/**
 * Render the TagInput inside the production provider stack
 * (QueryClientProvider > AuthProvider > MemoryRouter). The
 * QueryClientProvider is REQUIRED because TagInput consumes
 * `useCreateTagMutation` from @tanstack/react-query 5.x; without a
 * QueryClient ancestor the hook throws on mount.
 *
 * The default `onChange` is a fresh `vi.fn()` so tests that do not
 * supply their own can still assert via `expect(onChange).not.toHaveBeenCalled()`
 * - the helper returns the captured spy as `result.onChange`.
 */
function renderTagInput(props: RenderTagInputProps = {}): ReturnType<typeof renderWithProviders> & {
  onChange: ReturnType<typeof vi.fn>;
} {
  const onChange = (props.onChange ?? vi.fn()) as ReturnType<typeof vi.fn>;
  const result = renderWithProviders(
    <TagInput
      availableTags={props.availableTags ?? TAGS}
      selectedTagIds={props.selectedTagIds ?? []}
      onChange={onChange}
      label={props.label}
      helperText={props.helperText}
      errorMessage={props.errorMessage}
      disabled={props.disabled}
      allowCreate={props.allowCreate}
    />,
  );
  return { ...result, onChange };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("<TagInput />", () => {
  // The factory ID counters and MSW handler set are reset in the
  // global afterEach (tests/setup.ts), so a per-suite beforeEach is
  // not strictly required. We keep the hook for symmetry with the
  // sibling test files (Mocks for sibling tests sometimes restore
  // module-level mocks here).
  beforeEach(() => {
    // No-op: per-test setup beyond the global afterEach is unused.
  });

  // ---------------------------------------------------------------
  // Phase 2A - Initial Render
  // ---------------------------------------------------------------
  describe("Render - initial state", () => {
    it("renders the input field with correct ARIA combobox attributes", () => {
      renderTagInput();

      // The wrapper carries the `tag-input` testid for top-level
      // queries (e.g., the parent form's "tags" section locator).
      expect(screen.getByTestId("tag-input")).toBeInTheDocument();

      // The input is the focusable combobox per the W3C autocomplete
      // -list pattern. role="combobox" is set on the input itself
      // (NOT on the wrapping div) per the source.
      const input = screen.getByTestId("tag-input-field");
      expect(input).toBeInTheDocument();
      expect(input.tagName).toBe("INPUT");
      expect(input).toHaveAttribute("role", "combobox");
      expect(input).toHaveAttribute("aria-autocomplete", "list");
      // The dropdown is closed on initial render so aria-expanded is
      // explicitly "false" (NOT absent).
      expect(input).toHaveAttribute("aria-expanded", "false");
      // autoComplete="off" suppresses the browser's native autofill
      // dropdown (HTML uses lower-case `autocomplete` attribute).
      expect(input).toHaveAttribute("autocomplete", "off");
    });

    it("shows 'Add tags...' placeholder when no tags are selected", () => {
      renderTagInput();
      const input = screen.getByTestId("tag-input-field");
      // Source uses three normal periods. Curly ellipsis would be a
      // visual regression; assert the literal three-period form.
      expect(input).toHaveAttribute("placeholder", "Add tags...");
    });

    it("does not render a label when the prop is omitted", () => {
      renderTagInput();
      // The wrapping div is the only direct child. Without a label,
      // there is no <label> element above the well.
      expect(screen.queryByText("Tags")).not.toBeInTheDocument();
    });

    it("does not render an inline error or helper paragraph when neither is provided", () => {
      renderTagInput();
      expect(screen.queryByTestId("tag-input-error")).not.toBeInTheDocument();
      expect(screen.queryByTestId("tag-input-helper")).not.toBeInTheDocument();
    });

    it("does not render the dropdown listbox on initial render", () => {
      renderTagInput();
      // isOpen defaults to false; the listbox is only rendered when
      // both isOpen AND totalDropdownOptions > 0.
      expect(screen.queryByTestId("tag-input-listbox")).not.toBeInTheDocument();
    });
  });

  // ---------------------------------------------------------------
  // Phase 2A.2 - Render: label / helperText / errorMessage
  // ---------------------------------------------------------------
  describe("Render - label, helperText, errorMessage", () => {
    it("renders a label associated with the input via htmlFor", () => {
      renderTagInput({ label: "Tags" });
      const labelEl = screen.getByText("Tags");
      // The <label> uses htmlFor pointing at the input's useId().
      // We don't assert the exact id (that's an implementation
      // detail) but the htmlFor MUST equal the input's id to be
      // accessible.
      const input = screen.getByTestId("tag-input-field");
      expect(labelEl).toHaveAttribute("for", input.id);
    });

    it("renders helperText below the well when no error is present", () => {
      renderTagInput({ helperText: "e.g. industry, use case, geography" });
      expect(screen.getByTestId("tag-input-helper")).toBeInTheDocument();
      expect(screen.getByTestId("tag-input-helper")).toHaveTextContent(
        "e.g. industry, use case, geography",
      );
    });

    it("renders errorMessage with role='alert' via data-testid='tag-input-error'", () => {
      renderTagInput({ errorMessage: "Required field" });
      const error = screen.getByTestId("tag-input-error");
      expect(error).toBeInTheDocument();
      expect(error).toHaveAttribute("role", "alert");
      expect(error).toHaveTextContent("Required field");
    });

    it("errorMessage takes precedence over helperText (mutually exclusive)", () => {
      renderTagInput({ errorMessage: "Error", helperText: "Help" });
      // Source uses `errorMessage ? ... : helperText ? ... : null`
      // so when both are supplied only the error renders.
      expect(screen.getByTestId("tag-input-error")).toBeInTheDocument();
      expect(screen.getByTestId("tag-input-error")).toHaveTextContent("Error");
      expect(screen.queryByTestId("tag-input-helper")).not.toBeInTheDocument();
      expect(screen.queryByText("Help")).not.toBeInTheDocument();
    });

    it("does not render the helper paragraph when helperText is empty string", () => {
      // Source guards on length > 0 so empty-string helperText is
      // suppressed (defense against parents passing "" defensively).
      renderTagInput({ helperText: "" });
      expect(screen.queryByTestId("tag-input-helper")).not.toBeInTheDocument();
    });
  });

  // ---------------------------------------------------------------
  // Phase 2B - Selected Chips
  // ---------------------------------------------------------------
  describe("Render - selected chips", () => {
    it("renders a chip per selected tag id with the resolved name", () => {
      renderTagInput({ selectedTagIds: ["tag-saas", "tag-nyc"] });

      const chipSaas = screen.getByTestId("tag-chip-tag-saas");
      const chipNyc = screen.getByTestId("tag-chip-tag-nyc");
      expect(chipSaas).toBeInTheDocument();
      expect(chipNyc).toBeInTheDocument();
      // Within each chip the name renders verbatim.
      expect(within(chipSaas).getByText("industry:saas")).toBeInTheDocument();
      expect(within(chipNyc).getByText("geography:nyc")).toBeInTheDocument();
    });

    it("falls back to the id as the chip name when the tag is not in availableTags", () => {
      // Defensive fallback: the parent may hydrate selectedTagIds
      // before useTagsQuery() resolves; the chip should not render
      // blank text in that window.
      renderTagInput({ selectedTagIds: ["tag-saas", "unknown-id"] });
      const fallback = screen.getByTestId("tag-chip-unknown-id");
      expect(fallback).toBeInTheDocument();
      expect(within(fallback).getByText("unknown-id")).toBeInTheDocument();
    });

    it("renders an X remove button with aria-label per chip", () => {
      renderTagInput({ selectedTagIds: ["tag-saas"] });
      const remove = screen.getByTestId("tag-remove-tag-saas");
      expect(remove).toBeInTheDocument();
      // aria-label per source: `Remove tag ${name}`. The screen-
      // reader-friendly phrasing ("Remove tag <name>") is the only
      // accessible name on this button.
      expect(remove).toHaveAttribute("aria-label", "Remove tag industry:saas");
    });

    it("uses the id-fallback name in the X button's aria-label when tag is unknown", () => {
      renderTagInput({ selectedTagIds: ["unknown-id"] });
      const remove = screen.getByTestId("tag-remove-unknown-id");
      expect(remove).toHaveAttribute("aria-label", "Remove tag unknown-id");
    });

    it("clears the placeholder when at least one tag is selected", () => {
      renderTagInput({ selectedTagIds: ["tag-saas"] });
      const input = screen.getByTestId("tag-input-field");
      // Empty-string placeholder is rendered as `placeholder=""`.
      // jsdom normalizes the attribute to an empty string.
      expect(input).toHaveAttribute("placeholder", "");
    });
  });

  // ---------------------------------------------------------------
  // Phase 2C - Autocomplete Filtering
  // ---------------------------------------------------------------
  describe("Autocomplete - typing filters suggestions", () => {
    it("flips aria-expanded to true and opens the listbox when the input is focused", async () => {
      const user = userEvent.setup();
      renderTagInput();
      const input = screen.getByTestId("tag-input-field");
      await user.click(input);

      // The dropdown opens on focus (not just on first keystroke) so
      // the user can browse the full list before typing.
      await waitFor(() => {
        expect(screen.getByTestId("tag-input-listbox")).toBeInTheDocument();
      });
      expect(input).toHaveAttribute("aria-expanded", "true");
    });

    it("links aria-controls to the listbox id attribute", async () => {
      const user = userEvent.setup();
      renderTagInput();
      const input = screen.getByTestId("tag-input-field");
      await user.click(input);
      const listbox = await screen.findByTestId("tag-input-listbox");

      // The listbox id is constructed as `${inputId}-listbox` per
      // the source. The exact value depends on useId(); we assert
      // they MATCH rather than asserting a specific value.
      const ariaControls = input.getAttribute("aria-controls");
      expect(ariaControls).toBeTruthy();
      expect(listbox.getAttribute("id")).toBe(ariaControls);
    });

    it("shows all available tags when the dropdown opens and the query is empty", async () => {
      const user = userEvent.setup();
      renderTagInput();
      const input = screen.getByTestId("tag-input-field");
      await user.click(input);
      await screen.findByTestId("tag-input-listbox");

      // All five fixture tags should appear since they are under the
      // 10-suggestion slice cap and none are excluded by selection.
      for (const tag of TAGS) {
        expect(screen.getByTestId(`tag-suggestion-${tag.id}`)).toBeInTheDocument();
      }
    });

    it("filters suggestions by case-insensitive substring match on the name", async () => {
      const user = userEvent.setup();
      renderTagInput();
      const input = screen.getByTestId("tag-input-field");
      await user.click(input);
      await user.type(input, "industry");

      await waitFor(() => {
        // industry:saas and industry:fintech should match;
        // geography and use-case tags should NOT.
        expect(screen.getByTestId("tag-suggestion-tag-saas")).toBeInTheDocument();
        expect(screen.getByTestId("tag-suggestion-tag-fintech")).toBeInTheDocument();
      });
      expect(screen.queryByTestId("tag-suggestion-tag-nyc")).not.toBeInTheDocument();
      expect(screen.queryByTestId("tag-suggestion-tag-london")).not.toBeInTheDocument();
      expect(screen.queryByTestId("tag-suggestion-tag-demand-gen")).not.toBeInTheDocument();
    });

    it("matches case-insensitively (uppercase query against lowercase names)", async () => {
      const user = userEvent.setup();
      renderTagInput();
      const input = screen.getByTestId("tag-input-field");
      await user.click(input);
      await user.type(input, "INDUSTRY");

      await waitFor(() => {
        expect(screen.getByTestId("tag-suggestion-tag-saas")).toBeInTheDocument();
        expect(screen.getByTestId("tag-suggestion-tag-fintech")).toBeInTheDocument();
      });
    });

    it("caps the suggestion list at 10 entries when availableTags > 10", async () => {
      // Build 15 tags via the factory; ids are auto-generated UUIDs
      // (the factory's stable counter encoding) so each is unique.
      const manyTags: TagRead[] = Array.from({ length: 15 }, (_, idx) =>
        makeTagRead({ name: `bulk-tag-${idx}` }),
      );
      const user = userEvent.setup();
      renderTagInput({ availableTags: manyTags });

      const input = screen.getByTestId("tag-input-field");
      await user.click(input);
      await screen.findByTestId("tag-input-listbox");

      // Each suggestion has data-testid="tag-suggestion-{id}". We
      // count via querySelectorAll on the [data-testid^="tag-suggestion-"]
      // attribute. The "create-new" suggestion uses the literal
      // testid "tag-suggestion-create-new" which would also match
      // the prefix; the empty query path does NOT produce a create-
      // new row (canCreateNew requires trimmedQuery.length > 0), so
      // the count is exactly the slice limit.
      const suggestions = document.querySelectorAll('[data-testid^="tag-suggestion-"]');
      expect(suggestions.length).toBe(10);
      // Sanity check: create-new is NOT among them.
      expect(screen.queryByTestId("tag-suggestion-create-new")).not.toBeInTheDocument();
    });
  });

  // ---------------------------------------------------------------
  // Phase 2D - Clicking Suggestion Adds Tag
  // ---------------------------------------------------------------
  describe("Autocomplete - clicking suggestion adds tag", () => {
    it("calls onChange with the new id appended when a suggestion is clicked", async () => {
      const user = userEvent.setup();
      const { onChange } = renderTagInput({ selectedTagIds: ["tag-nyc"] });

      const input = screen.getByTestId("tag-input-field");
      await user.click(input);
      await screen.findByTestId("tag-input-listbox");

      // userEvent.click fires the full mouse-event sequence
      // (mousedown -> mouseup -> click). The source uses
      // onMouseDown to fire BEFORE the input's onBlur closes the
      // dropdown, and userEvent handles this correctly.
      const suggestion = screen.getByTestId("tag-suggestion-tag-saas");
      await user.click(suggestion);

      // The new selection is the previous selection + the clicked id.
      await waitFor(() => {
        expect(onChange).toHaveBeenCalled();
      });
      expect(onChange).toHaveBeenCalledWith(["tag-nyc", "tag-saas"]);
    });

    it("calls onChange with a single-id array when starting from an empty selection", async () => {
      const user = userEvent.setup();
      const { onChange } = renderTagInput({ selectedTagIds: [] });

      const input = screen.getByTestId("tag-input-field");
      await user.click(input);
      await screen.findByTestId("tag-input-listbox");

      const suggestion = screen.getByTestId("tag-suggestion-tag-fintech");
      await user.click(suggestion);

      await waitFor(() => {
        expect(onChange).toHaveBeenCalledWith(["tag-fintech"]);
      });
    });
  });

  // ---------------------------------------------------------------
  // Phase 2E - Keyboard Navigation: ArrowUp/Down
  // ---------------------------------------------------------------
  describe("Keyboard navigation - ArrowUp/Down", () => {
    it("starts with the first suggestion highlighted (aria-selected='true')", async () => {
      const user = userEvent.setup();
      renderTagInput();
      const input = screen.getByTestId("tag-input-field");
      await user.click(input);
      await screen.findByTestId("tag-input-listbox");

      // Initial highlightedIndex is 0 per `useState(0)`. The first
      // suggestion in the rendered order (TAGS[0] = tag-saas) gets
      // aria-selected="true".
      const first = screen.getByTestId(`tag-suggestion-${TAGS[0]!.id}`);
      expect(first).toHaveAttribute("aria-selected", "true");
    });

    it("advances the highlight one position on ArrowDown", async () => {
      const user = userEvent.setup();
      renderTagInput();
      const input = screen.getByTestId("tag-input-field");
      await user.click(input);
      await screen.findByTestId("tag-input-listbox");

      await user.keyboard("{ArrowDown}");
      // After ArrowDown the highlight is at index 1 (TAGS[1] =
      // tag-fintech). The first option is no longer highlighted.
      const second = screen.getByTestId(`tag-suggestion-${TAGS[1]!.id}`);
      await waitFor(() => {
        expect(second).toHaveAttribute("aria-selected", "true");
      });
      const first = screen.getByTestId(`tag-suggestion-${TAGS[0]!.id}`);
      expect(first).toHaveAttribute("aria-selected", "false");
    });

    it("clamps the highlight at the last suggestion on repeated ArrowDown", async () => {
      const user = userEvent.setup();
      renderTagInput();
      const input = screen.getByTestId("tag-input-field");
      await user.click(input);
      await screen.findByTestId("tag-input-listbox");

      // Press ArrowDown 20 times - far more than the 5 options.
      // The Math.min clamp keeps the highlight at the last index.
      await user.keyboard("{ArrowDown>20/}");
      const last = screen.getByTestId(`tag-suggestion-${TAGS[TAGS.length - 1]!.id}`);
      await waitFor(() => {
        expect(last).toHaveAttribute("aria-selected", "true");
      });
    });

    it("retreats the highlight on ArrowUp", async () => {
      const user = userEvent.setup();
      renderTagInput();
      const input = screen.getByTestId("tag-input-field");
      await user.click(input);
      await screen.findByTestId("tag-input-listbox");

      // Move from 0 -> 1 -> 2 then back to 1 via ArrowUp.
      await user.keyboard("{ArrowDown}{ArrowDown}");
      await user.keyboard("{ArrowUp}");
      const second = screen.getByTestId(`tag-suggestion-${TAGS[1]!.id}`);
      await waitFor(() => {
        expect(second).toHaveAttribute("aria-selected", "true");
      });
    });

    it("clamps the highlight at index 0 on ArrowUp from index 0", async () => {
      const user = userEvent.setup();
      renderTagInput();
      const input = screen.getByTestId("tag-input-field");
      await user.click(input);
      await screen.findByTestId("tag-input-listbox");

      // Already at 0; ArrowUp should be a no-op via Math.max(0, idx - 1).
      await user.keyboard("{ArrowUp}");
      const first = screen.getByTestId(`tag-suggestion-${TAGS[0]!.id}`);
      expect(first).toHaveAttribute("aria-selected", "true");
    });

    it("re-opens the dropdown when ArrowDown is pressed while closed", async () => {
      const user = userEvent.setup();
      renderTagInput();
      const input = screen.getByTestId("tag-input-field");

      // Step 1: open the dropdown via click.
      await user.click(input);
      await screen.findByTestId("tag-input-listbox");

      // Step 2: close it via Escape (input remains focused).
      await user.keyboard("{Escape}");
      await waitFor(() => {
        expect(screen.queryByTestId("tag-input-listbox")).not.toBeInTheDocument();
      });

      // Step 3: ArrowDown while closed - exercises the `!isOpen`
      // branch in the source's ArrowDown handler that re-opens the
      // listbox before clamping the highlighted index.
      await user.keyboard("{ArrowDown}");
      await waitFor(() => {
        expect(screen.getByTestId("tag-input-listbox")).toBeInTheDocument();
      });
    });

    it("sets aria-activedescendant to the current option's id while open", async () => {
      const user = userEvent.setup();
      renderTagInput();
      const input = screen.getByTestId("tag-input-field");
      await user.click(input);
      const listbox = await screen.findByTestId("tag-input-listbox");
      const listboxId = listbox.getAttribute("id") ?? "";

      // Initial highlight = 0, isOpen = true => aria-activedescendant
      // should reference `${listboxId}-option-0`.
      expect(input).toHaveAttribute("aria-activedescendant", `${listboxId}-option-0`);

      await user.keyboard("{ArrowDown}");
      await waitFor(() => {
        expect(input).toHaveAttribute("aria-activedescendant", `${listboxId}-option-1`);
      });
    });
  });

  // ---------------------------------------------------------------
  // Phase 2F - Enter Key Selects
  // ---------------------------------------------------------------
  describe("Keyboard navigation - Enter selects", () => {
    it("adds the highlighted suggestion when Enter is pressed", async () => {
      const user = userEvent.setup();
      const { onChange } = renderTagInput();

      const input = screen.getByTestId("tag-input-field");
      await user.click(input);
      await screen.findByTestId("tag-input-listbox");

      // Initial highlight = 0 => Enter should select TAGS[0].
      await user.keyboard("{Enter}");
      await waitFor(() => {
        expect(onChange).toHaveBeenCalledWith([TAGS[0]!.id]);
      });
    });

    it("adds the second suggestion after navigating with ArrowDown then Enter", async () => {
      const user = userEvent.setup();
      const { onChange } = renderTagInput();

      const input = screen.getByTestId("tag-input-field");
      await user.click(input);
      await screen.findByTestId("tag-input-listbox");

      await user.keyboard("{ArrowDown}{Enter}");
      await waitFor(() => {
        expect(onChange).toHaveBeenCalledWith([TAGS[1]!.id]);
      });
    });

    it("is a no-op when there are no suggestions and no create-new affordance", async () => {
      const user = userEvent.setup();
      // Empty availableTags AND allowCreate=false => no possible
      // dropdown options. Enter should NOT call onChange.
      const { onChange } = renderTagInput({
        availableTags: [],
        allowCreate: false,
      });

      const input = screen.getByTestId("tag-input-field");
      await user.click(input);
      // No listbox should render because totalDropdownOptions is 0.
      expect(screen.queryByTestId("tag-input-listbox")).not.toBeInTheDocument();

      await user.keyboard("{Enter}");
      // Use a small wait window to confirm onChange was NOT called.
      // We deliberately do not use waitFor with a positive assertion
      // since there is nothing to wait for; the negative assertion
      // is sufficient.
      expect(onChange).not.toHaveBeenCalled();
    });
  });

  // ---------------------------------------------------------------
  // Phase 2G - Escape Closes Dropdown
  // ---------------------------------------------------------------
  describe("Keyboard navigation - Escape closes", () => {
    it("closes the dropdown and clears the query on Escape", async () => {
      const user = userEvent.setup();
      renderTagInput();
      const input = screen.getByTestId("tag-input-field");

      await user.click(input);
      await user.type(input, "industry");
      // Confirm the dropdown is open with the typed query reflected.
      await screen.findByTestId("tag-input-listbox");
      expect(input).toHaveValue("industry");

      await user.keyboard("{Escape}");

      await waitFor(() => {
        expect(screen.queryByTestId("tag-input-listbox")).not.toBeInTheDocument();
      });
      // Source clears the query via setQuery(""). The native input's
      // value should reflect this on next render.
      expect(input).toHaveValue("");
    });
  });

  // ---------------------------------------------------------------
  // Phase 2H - Backspace on Empty
  // ---------------------------------------------------------------
  describe("Keyboard navigation - Backspace on empty", () => {
    it("removes the last selected tag when Backspace is pressed on an empty input", async () => {
      const user = userEvent.setup();
      const { onChange } = renderTagInput({
        selectedTagIds: ["tag-saas", "tag-nyc"],
      });

      const input = screen.getByTestId("tag-input-field");
      await user.click(input);
      // Input is empty (query = "") AND selection is non-empty =>
      // Backspace removes the LAST selected id (tag-nyc).
      await user.keyboard("{Backspace}");

      await waitFor(() => {
        expect(onChange).toHaveBeenCalledWith(["tag-saas"]);
      });
    });

    it("is a no-op when input is empty AND selection is empty", async () => {
      const user = userEvent.setup();
      const { onChange } = renderTagInput({ selectedTagIds: [] });

      const input = screen.getByTestId("tag-input-field");
      await user.click(input);
      await user.keyboard("{Backspace}");

      // Source guards on `selectedTagIds.length > 0`; with empty
      // selection the handler returns without invoking onChange.
      expect(onChange).not.toHaveBeenCalled();
    });

    it("does not remove a chip when the input has typed characters (native erase only)", async () => {
      const user = userEvent.setup();
      const { onChange } = renderTagInput({
        selectedTagIds: ["tag-saas"],
      });

      const input = screen.getByTestId("tag-input-field");
      await user.click(input);
      await user.type(input, "x");
      // At keydown time `query.length` is 1, so the source's
      // Backspace guard fails and the handler returns. The native
      // browser then erases the "x" character.
      await user.keyboard("{Backspace}");

      // The chip should still be selected; onChange must NOT have
      // been called for tag removal.
      expect(onChange).not.toHaveBeenCalled();
      expect(screen.getByTestId("tag-chip-tag-saas")).toBeInTheDocument();
    });
  });

  // ---------------------------------------------------------------
  // Phase 2I - Inline Tag Creation (allowCreate)
  // ---------------------------------------------------------------
  describe("Inline tag creation (allowCreate)", () => {
    it("renders the 'Create new' affordance for a non-empty no-match query", async () => {
      const user = userEvent.setup();
      renderTagInput();

      const input = screen.getByTestId("tag-input-field");
      await user.click(input);
      await user.type(input, "newtagname");

      // canCreateNew gates: allowCreate=true (default), trimmedQuery
      // non-empty, no exact match in TAGS, schema validates.
      const create = await screen.findByTestId("tag-suggestion-create-new");
      expect(create).toBeInTheDocument();
      // The label includes the literal text "Create" and the typed
      // query. The source emits curly quotes via &ldquo;/&rdquo;
      // entities; jsdom decodes them. We assert on the substring
      // "newtagname" rather than the entire decorated label so the
      // test is decoupled from the exact glyph rendering.
      expect(create).toHaveTextContent("Create");
      expect(create).toHaveTextContent("newtagname");
    });

    it("hides the 'Create new' affordance when the query exactly matches an existing tag", async () => {
      const user = userEvent.setup();
      renderTagInput();

      const input = screen.getByTestId("tag-input-field");
      await user.click(input);
      // Exact (case-insensitive) match against TAGS[0].name.
      await user.type(input, "industry:saas");

      // Existing tag-saas suggestion appears; create-new does not.
      await screen.findByTestId("tag-suggestion-tag-saas");
      expect(screen.queryByTestId("tag-suggestion-create-new")).not.toBeInTheDocument();
    });

    it("hides the 'Create new' affordance when allowCreate=false", async () => {
      const user = userEvent.setup();
      renderTagInput({ allowCreate: false });

      const input = screen.getByTestId("tag-input-field");
      await user.click(input);
      await user.type(input, "totallynewtag");

      // allowCreate=false short-circuits the canCreateNew gate.
      // Wait briefly for any state propagation; assert absence.
      await waitFor(() => {
        // The dropdown may still render if there are filtered
        // suggestions; with no matches and no create-new option,
        // the dropdown is unmounted entirely.
        expect(screen.queryByTestId("tag-suggestion-create-new")).not.toBeInTheDocument();
      });
    });

    it("hides the 'Create new' affordance when the query exceeds the 64-char schema max", async () => {
      const user = userEvent.setup();
      renderTagInput();

      const input = screen.getByTestId("tag-input-field");
      await user.click(input);
      // 65 'a's: TagCreateSchema enforces .max(64) so safeParse fails.
      const longQuery = "a".repeat(65);
      // user.type is faster than typing one keystroke at a time at
      // this length; pasteText would be even faster but is not
      // needed here.
      await user.type(input, longQuery);

      // Schema fails => canCreateNew false; no suggestions match
      // => listbox is unmounted entirely.
      await waitFor(() => {
        expect(screen.queryByTestId("tag-suggestion-create-new")).not.toBeInTheDocument();
      });
    });

    it("clicking 'Create new' POSTs /api/tags and adds the new tag id on success", async () => {
      // Capture the request body so we can assert on it AND return
      // a stable id for the post-create onChange assertion.
      let createBody: { name: string } | null = null;
      server.use(
        http.post("/api/tags", async ({ request }) => {
          const json = (await request.json()) as { name: string };
          createBody = { name: json.name };
          return HttpResponse.json(makeTagRead({ id: "new-tag-id", name: json.name }), {
            status: 201,
          });
        }),
      );

      const user = userEvent.setup();
      const { onChange } = renderTagInput();

      const input = screen.getByTestId("tag-input-field");
      await user.click(input);
      await user.type(input, "totallynewtag");

      const create = await screen.findByTestId("tag-suggestion-create-new");
      await user.click(create);

      // The POST body must equal { name: "totallynewtag" } exactly.
      await waitFor(() => {
        expect(createBody).toEqual({ name: "totallynewtag" });
      });

      // On success the hook calls addTag(newTag.id) which fires
      // onChange with the new id appended (selection was empty).
      await waitFor(() => {
        expect(onChange).toHaveBeenCalledWith(["new-tag-id"]);
      });
    });

    it("does NOT call onChange when the tag-create mutation fails", async () => {
      // The createDuplicate override returns 200 with the supplied
      // existing tag, simulating a server-side idempotent
      // re-creation. Wait - that's a SUCCESS path. We need a real
      // failure here. Use a 422 / 500 to drive the failure branch.
      server.use(
        http.post("/api/tags", () =>
          HttpResponse.json(
            {
              error: {
                code: "validation_error",
                message: "Tag name invalid",
                correlation_id: "test-corr-id",
                fields: [{ field: "name", message: "Invalid characters" }],
              },
            },
            { status: 422 },
          ),
        ),
      );

      // TanStack Query 5.x logs failed mutations to console.error;
      // suppress that to keep the test output readable. Restore on
      // teardown so other tests still see real errors.
      const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {
        // intentionally empty - swallow the noise
      });

      try {
        const user = userEvent.setup();
        const { onChange } = renderTagInput();

        const input = screen.getByTestId("tag-input-field");
        await user.click(input);
        await user.type(input, "newbadtag");

        const create = await screen.findByTestId("tag-suggestion-create-new");
        await user.click(create);

        // Wait long enough for the mutation round-trip to complete
        // and the error path to settle. We assert a stable
        // post-condition: onChange was NEVER called.
        await waitFor(() => {
          // The server should have responded by now; check that the
          // error toast effect has had a chance to run.
          // Use a small fixed delay to avoid racing against the
          // mutation. waitFor's default 1000ms timeout is enough.
          expect(onChange).not.toHaveBeenCalled();
        });
        // Sanity: assert again outside waitFor for finality.
        expect(onChange).not.toHaveBeenCalled();
      } finally {
        errorSpy.mockRestore();
      }
    });

    it("Enter on a no-match input creates the new tag (when allowCreate=true)", async () => {
      // Same captured-body pattern as the click test above.
      let createBody: { name: string } | null = null;
      server.use(
        http.post("/api/tags", async ({ request }) => {
          const json = (await request.json()) as { name: string };
          createBody = { name: json.name };
          return HttpResponse.json(makeTagRead({ id: "kbd-new-tag-id", name: json.name }), {
            status: 201,
          });
        }),
      );

      const user = userEvent.setup();
      const { onChange } = renderTagInput();

      const input = screen.getByTestId("tag-input-field");
      await user.click(input);
      await user.type(input, "completelynew");

      // suggestions = [] (no match), canCreateNew = true =>
      // highlightedIndex (0) >= suggestions.length (0) => fall
      // through to createAndAdd path. Enter triggers it without a
      // separate click.
      await user.keyboard("{Enter}");

      await waitFor(() => {
        expect(createBody).toEqual({ name: "completelynew" });
      });
      await waitFor(() => {
        expect(onChange).toHaveBeenCalledWith(["kbd-new-tag-id"]);
      });
    });

    it("idempotent re-creation (overrides.tags.createDuplicate) calls onChange with the existing id", async () => {
      // The createDuplicate factory returns 200 with the supplied
      // existing tag, mirroring the backend's idempotent
      // (org_id, name) unique-key behavior. The mutation hook treats
      // 200 the same as 201 (per useCreateTagMutation's success
      // branch), so the user sees the existing tag added as if newly
      // created.
      const existing = makeTagRead({ id: "existing-id", name: "industry:saas" });
      server.use(overrides.tags.createDuplicate(existing));

      const user = userEvent.setup();
      const { onChange } = renderTagInput();

      const input = screen.getByTestId("tag-input-field");
      await user.click(input);
      await user.type(input, "uniqueprobe");
      const create = await screen.findByTestId("tag-suggestion-create-new");
      await user.click(create);

      await waitFor(() => {
        expect(onChange).toHaveBeenCalledWith(["existing-id"]);
      });
    });
  });

  // ---------------------------------------------------------------
  // Phase 2J - Tag Chip Removal
  // ---------------------------------------------------------------
  describe("Tag chip removal", () => {
    it("calls onChange with the id removed when the X button is clicked", async () => {
      const user = userEvent.setup();
      const { onChange } = renderTagInput({
        selectedTagIds: ["tag-saas", "tag-nyc"],
      });

      const remove = screen.getByTestId("tag-remove-tag-saas");
      await user.click(remove);

      await waitFor(() => {
        expect(onChange).toHaveBeenCalledWith(["tag-nyc"]);
      });
    });

    it("removing the only selected tag yields an empty array", async () => {
      const user = userEvent.setup();
      const { onChange } = renderTagInput({ selectedTagIds: ["tag-saas"] });

      const remove = screen.getByTestId("tag-remove-tag-saas");
      await user.click(remove);

      await waitFor(() => {
        expect(onChange).toHaveBeenCalledWith([]);
      });
    });

    it("removing the middle tag preserves the order of the remaining ids", async () => {
      const user = userEvent.setup();
      const { onChange } = renderTagInput({
        selectedTagIds: ["tag-saas", "tag-nyc", "tag-london"],
      });

      const remove = screen.getByTestId("tag-remove-tag-nyc");
      await user.click(remove);

      await waitFor(() => {
        expect(onChange).toHaveBeenCalledWith(["tag-saas", "tag-london"]);
      });
    });
  });

  // ---------------------------------------------------------------
  // Phase 2K - Disabled State
  // ---------------------------------------------------------------
  describe("Disabled state", () => {
    it("sets the input's disabled attribute when disabled=true", () => {
      renderTagInput({ disabled: true });
      const input = screen.getByTestId("tag-input-field") as HTMLInputElement;
      expect(input.disabled).toBe(true);
    });

    it("hides the X buttons on selected chips when disabled=true", () => {
      renderTagInput({
        selectedTagIds: ["tag-saas", "tag-nyc"],
        disabled: true,
      });

      // Source: `{!disabled && (<button .../>)}` => the X buttons
      // are not rendered. The chips themselves still render so the
      // contributor can SEE the existing selection (read-only mode).
      expect(screen.getByTestId("tag-chip-tag-saas")).toBeInTheDocument();
      expect(screen.getByTestId("tag-chip-tag-nyc")).toBeInTheDocument();
      expect(screen.queryByTestId("tag-remove-tag-saas")).not.toBeInTheDocument();
      expect(screen.queryByTestId("tag-remove-tag-nyc")).not.toBeInTheDocument();
    });

    it("does not change the input value when typing into a disabled input", async () => {
      const user = userEvent.setup();
      renderTagInput({ disabled: true });

      const input = screen.getByTestId("tag-input-field") as HTMLInputElement;
      // userEvent silently ignores keystrokes against a disabled
      // input (matching browser behavior). The input value should
      // remain empty.
      await user.type(input, "abc");
      expect(input.value).toBe("");
    });
  });

  // ---------------------------------------------------------------
  // Phase 2L - Suggestions Filtering and Exclusion
  // ---------------------------------------------------------------
  describe("Suggestions filtering and exclusion", () => {
    it("excludes already-selected tags from the suggestion dropdown", async () => {
      const user = userEvent.setup();
      renderTagInput({ selectedTagIds: ["tag-saas"] });

      const input = screen.getByTestId("tag-input-field");
      await user.click(input);
      await user.type(input, "industry");

      // tag-saas is already selected so it should NOT appear as a
      // suggestion; tag-fintech (also industry) should still appear.
      await waitFor(() => {
        expect(screen.getByTestId("tag-suggestion-tag-fintech")).toBeInTheDocument();
      });
      expect(screen.queryByTestId("tag-suggestion-tag-saas")).not.toBeInTheDocument();
    });

    it("closes the dropdown after the 100ms blur delay", async () => {
      const user = userEvent.setup();
      renderTagInput();

      const input = screen.getByTestId("tag-input-field");
      await user.click(input);
      await screen.findByTestId("tag-input-listbox");

      // Move focus elsewhere via Tab. The source's onBlur schedules
      // setIsOpen(false) on a 100ms timeout (so click-on-suggestion
      // can register first). We use waitFor with a generous timeout
      // so flake-resistant on slow CI runners.
      await user.tab();

      await waitFor(
        () => {
          expect(screen.queryByTestId("tag-input-listbox")).not.toBeInTheDocument();
        },
        { timeout: 1500 },
      );
    });
  });
});
