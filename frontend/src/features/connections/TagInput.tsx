/**
 * TagInput.tsx - F-008 Multi-Tag Token Input.
 *
 * Renders a token-style multi-tag input with autocomplete against the
 * org's tag list, plus inline creation of new tags. Used by the
 * AddEditConnectionForm (F-001) to capture the F-008 tag dimensions.
 *
 * Behavior:
 *   - User types in the input; matching tags appear in a dropdown.
 *   - Click on a suggestion or press Enter while highlighted to add.
 *   - Pressing Enter when no suggestion matches creates a new tag via
 *     useCreateTagMutation, then auto-selects it.
 *   - Each selected tag renders as a Badge chip with an X button that
 *     removes it from selection.
 *   - Backspace on empty input removes the last selected tag.
 *
 * Per AAP Sec 0.5.4, the user's tag dimensions are "industry, use
 * case, geography" - these are NOT enforced as separate tag types;
 * they are suggested as conventional examples in helper text.
 *
 * Validation:
 *   - Tag names are validated against TagCreateSchema (max 64 chars,
 *     non-empty after trim) before creation, mirroring the backend
 *     pydantic validation to prevent doomed network round-trips.
 *   - The available tag list comes from useTagsQuery() in the parent
 *     (passed in via the `availableTags` prop for testability).
 *
 * Accessibility (W3C ARIA combobox autocomplete-list pattern):
 *   - <label htmlFor> linked to the search input via useId().
 *   - role="combobox" with aria-autocomplete="list", aria-expanded,
 *     aria-controls, aria-activedescendant on the input.
 *   - role="listbox" + role="option" + aria-selected on suggestions.
 *   - Keyboard: ArrowUp/ArrowDown move highlight; Enter selects;
 *     Escape closes; Tab closes and moves focus naturally; Backspace
 *     on empty input removes last chip.
 *   - Each selected chip has an aria-labeled remove button so screen
 *     readers announce "Remove tag <name>" rather than just "X".
 *
 * Conventions per AAP Sec 0.7.7 and frontend/.prettierrc.json:
 *   - Strict TypeScript; no `any`; explicit `JSX.Element` return type.
 *   - Named exports only (no default export).
 *   - clsx for conditional className composition.
 *   - TailwindCSS utility classes only (no inline `style`).
 *   - Lucide-React for icons (Plus, X).
 *   - Double quotes per project Prettier configuration
 *     (singleQuote: false, trailingComma: "all", printWidth: 100).
 *   - Path imports use the `@/` alias declared in `vite.config.ts`
 *     and mirrored in `tsconfig.json`.
 *   - Type-only imports use the inline `type` modifier per
 *     `verbatimModuleSyntax: true` in `tsconfig.json`.
 *
 * Coordinates with:
 *   - frontend/src/api/connections.ts - useCreateTagMutation hook for
 *     inline tag creation (POST /api/tags); the hook fires its own
 *     toast on error so this component does not duplicate it.
 *   - frontend/src/components/ui/Badge.tsx - the brand-variant Badge
 *     primitive used for each removable tag chip.
 *   - frontend/src/schemas/connection.ts - TagCreateSchema (runtime
 *     Zod validation) and the TagRead shape (type-only).
 *   - frontend/src/features/connections/AddEditConnectionForm.tsx -
 *     the consumer; passes the org's tags and the current selection.
 *   - frontend/tests/features/connections/TagInput.test.tsx (future)
 *     - verifies autocomplete filtering, keyboard navigation, chip
 *     removal, inline creation, and disabled-state behavior.
 */

import { useId, useMemo, useRef, useState, type JSX, type KeyboardEvent } from "react";
import { Plus, X } from "lucide-react";
import clsx from "clsx";

import { useCreateTagMutation } from "@/api/connections";
import { Badge } from "@/components/ui/Badge";
import { TagCreateSchema, type TagRead } from "@/schemas/connection";

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

/**
 * Public props for the TagInput component.
 *
 * `readonly` is applied to every prop so consumers cannot accidentally
 * mutate the props object inside event handlers; this convention is
 * shared with the UI primitives (Button, Badge, Modal, Toast) and
 * with sibling feature components (InvolvementBadge, DuplicateWarning).
 *
 * Members exposed (per the file schema in the AAP):
 *   - availableTags    All tags available for autocomplete.
 *   - selectedTagIds   Currently selected tag IDs.
 *   - onChange         Called with the new selected tag IDs array.
 *   - label            Optional visible label above the input.
 *   - helperText       Optional helper text below the input.
 *   - errorMessage     Optional inline error message.
 *   - disabled         Optional disable flag (default false).
 *   - allowCreate      Optional inline-creation flag (default true).
 */
export interface TagInputProps {
  /**
   * All tags available for autocomplete (typically org-scoped via
   * useTagsQuery). The order is preserved when filtering matches;
   * the parent typically passes them sorted by name ascending.
   */
  readonly availableTags: ReadonlyArray<TagRead>;

  /**
   * Currently selected tag IDs. The component renders a Badge chip
   * per id and excludes already-selected ids from the suggestion
   * dropdown so the user cannot add the same tag twice.
   */
  readonly selectedTagIds: ReadonlyArray<string>;

  /**
   * Called with the new selected tag IDs array on add or remove.
   * The component is fully controlled - it does not maintain an
   * internal copy of the selection.
   */
  readonly onChange: (selectedTagIds: ReadonlyArray<string>) => void;

  /**
   * Optional visible label above the input. When present, the label
   * is wired to the search input via htmlFor / id (useId) so screen
   * readers announce it on focus.
   */
  readonly label?: string;

  /**
   * Optional helper text below the input. Suppressed when an
   * errorMessage is present (errors take precedence). Suggested use:
   * conventional tag dimension examples ("e.g. industry, use case,
   * geography") per AAP Sec 0.5.4.
   */
  readonly helperText?: string;

  /**
   * Optional inline error message. When present, the well's border
   * turns red and the message is rendered with role="alert" below
   * the well, replacing the helperText.
   */
  readonly errorMessage?: string;

  /**
   * When true, the input is disabled (cannot type, cannot create new
   * tags) and the X buttons on selected chips are hidden so the
   * selection is immutable. Defaults to false.
   */
  readonly disabled?: boolean;

  /**
   * When true (the default), users may create new tags inline via
   * the "Create new" dropdown affordance. When false, only existing
   * available tags can be selected. Pass false in admin / read-mostly
   * surfaces (e.g., a filter chip-toggle that should not pollute the
   * tag table with one-off filter terms).
   */
  readonly allowCreate?: boolean;
}

// ---------------------------------------------------------------------------
// Helper functions
// ---------------------------------------------------------------------------

/**
 * Case-insensitive substring match used by the autocomplete filter.
 *
 * Returns true unconditionally when the query is empty (after trim)
 * so the dropdown shows the full available-tags list as soon as the
 * user focuses the input - matching the standard combobox UX of
 * "show me everything I can pick".
 */
function matchesQuery(name: string, query: string): boolean {
  const trimmed = query.trim();
  if (trimmed.length === 0) {
    return true;
  }
  return name.toLowerCase().includes(trimmed.toLowerCase());
}

/**
 * Filter the available tags for autocomplete suggestions.
 *
 * Pipeline:
 *   1. Exclude tags already in the selectedTagIds set so the user
 *      cannot select the same tag twice.
 *   2. Apply the case-insensitive query substring match.
 *   3. Cap the result at 10 entries to keep the dropdown manageable;
 *      power users who need a less-frequent tag can keep typing to
 *      narrow further.
 *
 * Returns a ReadonlyArray so the caller cannot mutate it back into
 * the source `available` reference.
 */
function filterSuggestions(
  available: ReadonlyArray<TagRead>,
  selected: ReadonlyArray<string>,
  query: string,
): ReadonlyArray<TagRead> {
  const selectedSet = new Set(selected);
  return available
    .filter((tag) => !selectedSet.has(tag.id) && matchesQuery(tag.name, query))
    .slice(0, 10);
}

/**
 * Find a tag by exact name match (case-insensitive).
 *
 * Returns null when no exact match exists or when the query is
 * effectively empty. Used to gate the "Create new" dropdown
 * affordance: when an exact match already exists in the available
 * list, the affordance is hidden because the user can simply select
 * the existing tag.
 */
function findExactMatch(available: ReadonlyArray<TagRead>, query: string): TagRead | null {
  const normalized = query.trim().toLowerCase();
  if (normalized.length === 0) {
    return null;
  }
  return available.find((tag) => tag.name.toLowerCase() === normalized) ?? null;
}

// ---------------------------------------------------------------------------
// TagInput component
// ---------------------------------------------------------------------------

/**
 * Token-style multi-tag input with autocomplete and inline creation.
 *
 * The component renders three regions:
 *   1. Optional <label> wired to the search input via useId().
 *   2. The "well" - a flex-wrap container that holds all selected
 *      Badge chips followed by the search <input>. Clicking anywhere
 *      in the well forwards focus to the input.
 *   3. Optional helper / error text below the well.
 *   4. The suggestions dropdown <ul> rendered when the input is
 *      focused and at least one option is available.
 *
 * The dropdown options are the filtered available tags (up to 10)
 * followed by an optional "Create new <query>" affordance when the
 * query does not exactly match any existing tag and inline creation
 * is allowed (allowCreate prop, default true). Keyboard navigation
 * cycles through all dropdown options including the create-new row
 * via a single highlightedIndex state.
 *
 * @example In AddEditConnectionForm.tsx (F-001 + F-008)
 *   const tags = useTagsQuery();
 *   const [tagIds, setTagIds] = useState<ReadonlyArray<string>>([]);
 *   <TagInput
 *     availableTags={tags.data ?? []}
 *     selectedTagIds={tagIds}
 *     onChange={setTagIds}
 *     label="Tags"
 *     helperText="e.g. industry, use case, geography"
 *   />
 */
export function TagInput({
  availableTags,
  selectedTagIds,
  onChange,
  label,
  helperText,
  errorMessage,
  disabled = false,
  allowCreate = true,
}: TagInputProps): JSX.Element {
  // -------------------------------------------------------------------------
  // Stable IDs and refs
  // -------------------------------------------------------------------------

  // useId() generates an SSR-safe stable ID that survives rerenders;
  // critical for ARIA combobox/listbox relationships (aria-controls
  // and aria-activedescendant). Using random strings would break the
  // linkage on rerenders and confuse screen readers.
  const inputId = useId();
  const listboxId = `${inputId}-listbox`;

  // Holds the input DOM node so the component can:
  //   1. Refocus the input after a chip is removed (preserving the
  //      typing flow without making the user re-click).
  //   2. Forward focus from the well's onClick handler so clicking
  //      on whitespace inside the well still focuses the input.
  const inputRef = useRef<HTMLInputElement>(null);

  // -------------------------------------------------------------------------
  // Local UI state
  // -------------------------------------------------------------------------

  // The current text in the search input. Used both to filter
  // suggestions and (when non-empty) to drive the "Create new" gate.
  const [query, setQuery] = useState("");

  // Whether the suggestion dropdown is open. Open on focus, close on
  // blur (with a small timeout so click events on suggestions can
  // still register before the dropdown unmounts), Escape, and Tab.
  const [isOpen, setIsOpen] = useState(false);

  // Index of the currently keyboard-highlighted dropdown option.
  // Spans suggestions + the optional create-new row as a single
  // contiguous list so ArrowUp / ArrowDown cycle through everything.
  const [highlightedIndex, setHighlightedIndex] = useState(0);

  // -------------------------------------------------------------------------
  // Mutation hook for inline tag creation (F-008)
  // -------------------------------------------------------------------------

  // useCreateTagMutation handles the HTTP POST /api/tags round-trip,
  // surfaces toast notifications on error/success, and exposes
  // isPending so we can disable the input during the round-trip.
  const createTag = useCreateTagMutation();

  // -------------------------------------------------------------------------
  // Derived state (memoized)
  // -------------------------------------------------------------------------

  // Map id -> tag for chip rendering. Recomputed only when the
  // available list changes; avoids an O(n) lookup per chip on every
  // keystroke.
  const idToTag = useMemo(() => {
    const map = new Map<string, TagRead>();
    for (const tag of availableTags) {
      map.set(tag.id, tag);
    }
    return map;
  }, [availableTags]);

  // Filtered suggestions for the dropdown. Recomputed when the
  // available list, the selection, or the query changes.
  const suggestions = useMemo(
    () => filterSuggestions(availableTags, selectedTagIds, query),
    [availableTags, selectedTagIds, query],
  );

  // Whether an exact (case-insensitive) name match exists in the
  // available tags. Determines whether the "Create new" affordance
  // is visible.
  const exactMatch = useMemo(() => findExactMatch(availableTags, query), [availableTags, query]);

  // Trimmed query used by both the create-new gate and the create
  // mutation payload. Held as a derived constant rather than state
  // to avoid double-bookkeeping with `query`.
  const trimmedQuery = query.trim();

  // Whether to show the "Create new" dropdown row. Four gates must
  // all pass:
  //   1. allowCreate prop is true (the default).
  //   2. Query is non-empty after trim.
  //   3. No existing tag matches the query exactly.
  //   4. The trimmed query passes TagCreateSchema (max 64 chars).
  //      This last gate prevents firing a doomed mutation that the
  //      backend would reject with a 422.
  const canCreateNew =
    allowCreate &&
    trimmedQuery.length > 0 &&
    exactMatch === null &&
    TagCreateSchema.safeParse({ name: trimmedQuery }).success;

  // Total number of dropdown options. Used to clamp the highlighted
  // index on ArrowDown and to gate the dropdown's visibility.
  const totalDropdownOptions = suggestions.length + (canCreateNew ? 1 : 0);

  // -------------------------------------------------------------------------
  // Mutators
  // -------------------------------------------------------------------------

  /**
   * Add a tag id to the selection. Idempotent: a no-op when the id
   * is already selected. Clears the query so the user can immediately
   * type to find the next tag, and resets the highlight to the top.
   */
  const addTag = (tagId: string): void => {
    if (selectedTagIds.includes(tagId)) {
      return;
    }
    onChange([...selectedTagIds, tagId]);
    setQuery("");
    setHighlightedIndex(0);
  };

  /**
   * Remove a tag id from the selection. Returns focus to the input
   * so the user does not have to re-click after a removal - common
   * token-input idiom.
   */
  const removeTag = (tagId: string): void => {
    onChange(selectedTagIds.filter((id) => id !== tagId));
    inputRef.current?.focus();
  };

  /**
   * Validate the trimmed name against TagCreateSchema, then fire the
   * useCreateTagMutation. On success, auto-add the newly created tag
   * to the selection so the user does not have to click again. On
   * error, the mutation hook surfaces a toast (we do not duplicate).
   */
  const createAndAdd = (name: string): void => {
    const trimmed = name.trim();
    const parse = TagCreateSchema.safeParse({ name: trimmed });
    if (!parse.success) {
      return;
    }
    createTag.mutate(parse.data, {
      onSuccess: (newTag) => {
        addTag(newTag.id);
      },
      // onError already toasts via the hook (see useCreateTagMutation
      // in @/api/connections); we deliberately do not duplicate it.
    });
  };

  // -------------------------------------------------------------------------
  // Keyboard handler
  // -------------------------------------------------------------------------

  /**
   * Route key events to the appropriate dropdown / chip behavior.
   *
   * The handler is exhaustive across the documented keys:
   *   - Backspace on empty input  -> remove last selected chip
   *   - Escape                    -> close dropdown, clear query
   *   - ArrowDown                 -> move highlight down (open if closed)
   *   - ArrowUp                   -> move highlight up
   *   - Enter                     -> select highlighted option (or create)
   *   - Tab when open             -> close dropdown (default Tab behavior
   *                                   then moves focus naturally)
   *
   * All other keys fall through to the native input's default
   * handling so typing characters works normally.
   */
  const handleKeyDown = (e: KeyboardEvent<HTMLInputElement>): void => {
    if (e.key === "Backspace" && query.length === 0 && selectedTagIds.length > 0) {
      e.preventDefault();
      // noUncheckedIndexedAccess in tsconfig.json makes this lookup
      // return string | undefined, so we guard explicitly. In
      // practice the surrounding length check guarantees defined.
      const lastId = selectedTagIds[selectedTagIds.length - 1];
      if (lastId !== undefined) {
        removeTag(lastId);
      }
      return;
    }

    if (e.key === "Escape") {
      e.preventDefault();
      setIsOpen(false);
      setQuery("");
      return;
    }

    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (!isOpen) {
        setIsOpen(true);
      }
      setHighlightedIndex((idx) => Math.max(0, Math.min(totalDropdownOptions - 1, idx + 1)));
      return;
    }

    if (e.key === "ArrowUp") {
      e.preventDefault();
      setHighlightedIndex((idx) => Math.max(0, idx - 1));
      return;
    }

    if (e.key === "Enter") {
      // Stop the surrounding form's default submit behavior; the
      // Enter key here is reserved for tag selection inside the
      // combobox per the W3C autocomplete-list pattern.
      e.preventDefault();
      if (highlightedIndex < suggestions.length) {
        const target = suggestions[highlightedIndex];
        if (target !== undefined) {
          addTag(target.id);
        }
      } else if (canCreateNew) {
        createAndAdd(trimmedQuery);
      }
      return;
    }

    if (e.key === "Tab" && isOpen) {
      // Tab closes the listbox (no preventDefault) so the browser
      // moves focus to the next focusable element naturally.
      setIsOpen(false);
    }
  };

  /**
   * Close the dropdown on blur, with a small timeout so that a
   * click on a suggestion's onMouseDown can register before the
   * dropdown unmounts. Without this delay, the click target would
   * disappear between mousedown and click and the selection would
   * silently fail.
   */
  const handleBlur = (): void => {
    window.setTimeout(() => setIsOpen(false), 100);
  };

  // -------------------------------------------------------------------------
  // Render
  // -------------------------------------------------------------------------

  return (
    <div className="flex flex-col gap-1" data-testid="tag-input">
      {label !== undefined && label.length > 0 && (
        <label htmlFor={inputId} className="text-sm font-medium leading-5 text-slate-700">
          {label}
        </label>
      )}

      {/* The "well" - selected chips followed by the search input. */}
      <div
        className={clsx(
          "flex flex-wrap items-center gap-1.5 rounded-md border bg-white px-2 py-1.5 transition-colors",
          "focus-within:outline focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-brand-500",
          errorMessage !== undefined && errorMessage.length > 0
            ? "border-red-400 hover:border-red-500"
            : "border-slate-300 hover:border-slate-400",
          disabled && "cursor-not-allowed bg-slate-100",
        )}
        onClick={() => inputRef.current?.focus()}
        data-testid="tag-input-well"
      >
        {/* Selected tag chips */}
        {selectedTagIds.map((id) => {
          const tag = idToTag.get(id);
          // Defensive fallback: if availableTags has not loaded yet
          // (e.g., the form was hydrated with tag IDs before the
          // useTagsQuery resolved), render the id as a placeholder
          // so the chip is not blank. The chip rerenders with the
          // proper name as soon as the available list arrives.
          const name = tag !== undefined ? tag.name : id;
          return (
            <span key={id} className="inline-flex items-center" data-testid={`tag-chip-${id}`}>
              <Badge variant="brand" size="sm" withBorder>
                <span>{name}</span>
                {!disabled && (
                  <button
                    type="button"
                    onClick={(e) => {
                      // Stop the well's onClick from firing - it
                      // would refocus the input, which is confusing
                      // immediately after clicking the X.
                      e.stopPropagation();
                      removeTag(id);
                    }}
                    aria-label={`Remove tag ${name}`}
                    className="-mr-1 ml-1 inline-flex h-4 w-4 items-center justify-center rounded-full text-brand-700 transition-colors hover:bg-brand-200 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-brand-500"
                    data-testid={`tag-remove-${id}`}
                  >
                    <X aria-hidden="true" className="h-3 w-3" />
                  </button>
                )}
              </Badge>
            </span>
          );
        })}

        {/* Search input */}
        <input
          ref={inputRef}
          id={inputId}
          type="text"
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setIsOpen(true);
            setHighlightedIndex(0);
          }}
          onKeyDown={handleKeyDown}
          onFocus={() => setIsOpen(true)}
          onBlur={handleBlur}
          disabled={disabled || createTag.isPending}
          // autoComplete="off" disables the browser's native autofill
          // dropdown which conflicts with our custom autocomplete.
          autoComplete="off"
          // ARIA combobox autocomplete-list pattern (W3C).
          role="combobox"
          aria-autocomplete="list"
          aria-expanded={isOpen}
          aria-controls={listboxId}
          aria-activedescendant={
            isOpen && highlightedIndex < totalDropdownOptions
              ? `${listboxId}-option-${highlightedIndex}`
              : undefined
          }
          placeholder={selectedTagIds.length === 0 ? "Add tags..." : ""}
          className="min-w-[120px] flex-1 border-0 bg-transparent px-1 py-0.5 text-sm leading-5 text-slate-900 outline-none placeholder:text-slate-400 disabled:cursor-not-allowed"
          data-testid="tag-input-field"
        />
      </div>

      {/* Helper text or inline error message (errors take precedence). */}
      {errorMessage !== undefined && errorMessage.length > 0 ? (
        <p role="alert" className="text-xs text-red-600" data-testid="tag-input-error">
          {errorMessage}
        </p>
      ) : helperText !== undefined && helperText.length > 0 ? (
        <p className="text-xs text-slate-500" data-testid="tag-input-helper">
          {helperText}
        </p>
      ) : null}

      {/* Suggestions dropdown - only when open AND has at least one option. */}
      {isOpen && totalDropdownOptions > 0 && (
        <ul
          id={listboxId}
          role="listbox"
          className="relative mt-1 max-h-56 overflow-y-auto rounded-md border border-slate-200 bg-white shadow-card"
          data-testid="tag-input-listbox"
        >
          {suggestions.map((tag, idx) => {
            const optionId = `${listboxId}-option-${idx}`;
            const highlighted = idx === highlightedIndex;
            return (
              <li
                key={tag.id}
                id={optionId}
                role="option"
                aria-selected={highlighted}
                className={clsx(
                  "cursor-pointer px-3 py-2 text-sm text-slate-700",
                  highlighted ? "bg-brand-50 text-brand-900" : "hover:bg-slate-50",
                )}
                onMouseDown={(e) => {
                  // onMouseDown (not onClick) so this fires BEFORE
                  // the input's onBlur handler closes the dropdown.
                  // preventDefault keeps focus on the input so the
                  // user can keep typing after selecting.
                  e.preventDefault();
                  addTag(tag.id);
                }}
                onMouseEnter={() => setHighlightedIndex(idx)}
                data-testid={`tag-suggestion-${tag.id}`}
              >
                {tag.name}
              </li>
            );
          })}
          {canCreateNew && (
            <li
              id={`${listboxId}-option-${suggestions.length}`}
              role="option"
              aria-selected={highlightedIndex === suggestions.length}
              className={clsx(
                "flex cursor-pointer items-center gap-2 border-t border-slate-100 px-3 py-2 text-sm font-medium",
                highlightedIndex === suggestions.length
                  ? "bg-brand-50 text-brand-900"
                  : "text-brand-700 hover:bg-slate-50",
              )}
              onMouseDown={(e) => {
                e.preventDefault();
                createAndAdd(trimmedQuery);
              }}
              onMouseEnter={() => setHighlightedIndex(suggestions.length)}
              data-testid="tag-suggestion-create-new"
            >
              <Plus aria-hidden="true" className="h-4 w-4" />
              {/* HTML entities for curly quotes - universally safe in JSX. */}
              Create &ldquo;{trimmedQuery}&rdquo;
            </li>
          )}
        </ul>
      )}
    </div>
  );
}
