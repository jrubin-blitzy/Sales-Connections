/**
 * Table.test.tsx - Vitest tests for the Table UI primitive.
 *
 * Targets `frontend/src/components/ui/Table.tsx` - the generic-typed
 * sortable data-table primitive consumed across feeds, admin views,
 * and detail history per AAP Sec 0.2.3:
 *   - frontend/src/features/connections/ConnectionFeed.tsx (F-004)
 *   - frontend/src/features/admin/UserManagement.tsx (F-014)
 *   - frontend/src/features/admin/RecordModeration.tsx (F-014)
 *   - frontend/src/features/admin/Analytics.tsx (F-014)
 *
 * Coverage goals (>= 90% line per `Table.tsx`):
 *   - Basic rendering with 3 columns x 5 rows (table-container,
 *     table semantics, scope="col").
 *   - Generic <TRow> typing preserved at compile time and at runtime.
 *   - data-testid="table-row-{rowKey}" on each data row.
 *   - Sortable headers wrap content in a <button type="button">
 *     with data-testid="table-header-sort-{key}".
 *   - Sort cycle: null -> asc -> desc -> null on the same key;
 *     switches to "asc" when a different key is clicked.
 *   - aria-sort attribute: ascending / descending / none / undefined.
 *   - Sort icons (Lucide React): ArrowUp / ArrowDown / ArrowUpDown.
 *   - Empty state with colSpan = columns.length and the default
 *     "No records to display." string; custom emptyState slot.
 *   - Loading state: 5 skeleton rows with aria-hidden="true" and
 *     animate-pulse placeholder bars; suppresses both data rows
 *     and the empty state.
 *   - Row click handler fires on mouse click and Enter / Space keys.
 *   - tabIndex={0} and cursor-pointer on clickable rows; absent on
 *     non-clickable rows.
 *   - Column align: right -> text-right, center -> text-center,
 *     default left -> text-left (and not text-right / text-center).
 *   - Column hideBelow: sm/md/lg/xl maps to hidden {bp}:table-cell
 *     on both the <th> and the corresponding <td>.
 *   - <caption className="sr-only"> rendered when the caption prop
 *     is provided; absent otherwise.
 *   - Custom className / tableClassName / column.headerClassName /
 *     column.cellClassName / column.widthClassName all merge onto
 *     the correct DOM elements.
 *
 * Conventions per AAP Sec 0.7.7 and frontend/.prettierrc.json:
 *   - Strict TypeScript; no `any`.
 *   - Double quotes; trailing commas; 2-space indent; line length <= 100.
 *   - Uses Vitest globals via test.globals: true (vite.config.ts), but
 *     also imports describe/it/expect/vi explicitly for IDE type-awareness.
 *   - Uses @testing-library/react render and screen directly (Table is a
 *     pure presentational primitive; no provider stack needed).
 *   - Uses @testing-library/user-event for user interaction (NOT
 *     fireEvent) - more realistic event sequences.
 *   - Uses it.each for the sort-cycle and hideBelow test matrices.
 *   - No emoji; no console.log.
 *
 * Coordinates with:
 *   - frontend/src/components/ui/Table.tsx (system under test)
 *   - frontend/tests/setup.ts (registers @testing-library/jest-dom matchers)
 *   - frontend/tsconfig.json (provides the @/ path alias)
 *   - frontend/vite.config.ts (declares test.globals: true)
 */

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { Table, type SortDirection, type TableColumn } from "@/components/ui/Table";

// ---------------------------------------------------------------------------
// Module-scoped test fixtures
// ---------------------------------------------------------------------------

/**
 * Row type for the test fixtures. Mirrors the shape consumers use in the
 * UserManagement admin view (F-014) - id, name, email, role - so the
 * generic-typing test exercises a realistic usage pattern.
 */
interface UserRow {
  readonly id: string;
  readonly name: string;
  readonly email: string;
  readonly role: "Admin" | "Contributor" | "Viewer";
}

/**
 * Five-row sample dataset. Matches the loading-state skeleton row count
 * (5) so coverage of the loading branch is consistent with the data
 * branch's row count.
 */
const SAMPLE_DATA: ReadonlyArray<UserRow> = [
  { id: "u1", name: "Alice", email: "alice@x.com", role: "Admin" },
  { id: "u2", name: "Bob", email: "bob@x.com", role: "Contributor" },
  { id: "u3", name: "Charlie", email: "charlie@x.com", role: "Viewer" },
  { id: "u4", name: "Dana", email: "dana@x.com", role: "Contributor" },
  { id: "u5", name: "Eve", email: "eve@x.com", role: "Viewer" },
];

/**
 * Three-column baseline definition without any sortable / align /
 * hideBelow customizations. Used by the basic-rendering, row-keys,
 * empty-state, and caption blocks where only the bare-minimum shape
 * matters.
 */
const BASIC_COLUMNS: ReadonlyArray<TableColumn<UserRow>> = [
  { key: "name", header: "Name", render: (row) => row.name },
  { key: "email", header: "Email", render: (row) => row.email },
  { key: "role", header: "Role", render: (row) => row.role },
];

/**
 * Stable rowKey resolver. Receives the row and the index but only the
 * row's id is needed because every fixture row carries one.
 */
const ROW_KEY = (row: UserRow): string => row.id;

/**
 * Two-column definition with both columns sortable. Used by the
 * sort-cycle tests so we can exercise the "click on a different
 * column" branch as well as the same-column three-state cycle.
 */
const TWO_SORTABLE_COLUMNS: ReadonlyArray<TableColumn<UserRow>> = [
  { key: "name", header: "Name", render: (row) => row.name, sortable: true },
  { key: "email", header: "Email", render: (row) => row.email, sortable: true },
];

/**
 * Mixed-sortability column definition. The `name` column is sortable;
 * the `email` column is not. Used to verify the absence of a sort
 * button on non-sortable headers and the absence of aria-sort on them.
 */
const MIXED_SORTABLE_COLUMNS: ReadonlyArray<TableColumn<UserRow>> = [
  { key: "name", header: "Name", render: (row) => row.name, sortable: true },
  { key: "email", header: "Email", render: (row) => row.email },
];

// ---------------------------------------------------------------------------
// Test suite
// ---------------------------------------------------------------------------

describe("<Table />", () => {
  // -------------------------------------------------------------------------
  // 1. Basic rendering
  // -------------------------------------------------------------------------
  describe("basic rendering", () => {
    it("renders the table-container wrapper", () => {
      render(<Table columns={BASIC_COLUMNS} data={SAMPLE_DATA} rowKey={ROW_KEY} />);

      const container = screen.getByTestId("table-container");
      expect(container).toBeInTheDocument();
      // The wrapping element is a <div> per the source spec.
      expect(container.tagName).toBe("DIV");
    });

    it("renders <table>, <thead>, and <tbody>", () => {
      render(<Table columns={BASIC_COLUMNS} data={SAMPLE_DATA} rowKey={ROW_KEY} />);

      // <table> exposes role="table" by default in jsdom.
      expect(screen.getByRole("table")).toBeInTheDocument();
      // <thead> and <tbody> both expose role="rowgroup" - so two total.
      expect(screen.getAllByRole("rowgroup")).toHaveLength(2);
    });

    it('renders one <th scope="col"> per column', () => {
      render(<Table columns={BASIC_COLUMNS} data={SAMPLE_DATA} rowKey={ROW_KEY} />);

      const headers = screen.getAllByRole("columnheader");
      expect(headers).toHaveLength(3);
      headers.forEach((header) => {
        expect(header).toHaveAttribute("scope", "col");
      });
    });

    it("renders one <tr> per data row in the tbody", () => {
      render(<Table columns={BASIC_COLUMNS} data={SAMPLE_DATA} rowKey={ROW_KEY} />);

      // getAllByRole('row') returns 1 header row + 5 data rows = 6 total.
      const allRows = screen.getAllByRole("row");
      expect(allRows).toHaveLength(6);
      // Slice off the header row and verify 5 data rows remain.
      const dataRows = allRows.slice(1);
      expect(dataRows).toHaveLength(5);
    });

    it("renders cell content via the column.render callback", () => {
      render(<Table columns={BASIC_COLUMNS} data={SAMPLE_DATA} rowKey={ROW_KEY} />);

      expect(screen.getByText("Alice")).toBeInTheDocument();
      expect(screen.getByText("alice@x.com")).toBeInTheDocument();
      expect(screen.getByText("Admin")).toBeInTheDocument();
    });

    it("renders the column header text", () => {
      render(<Table columns={BASIC_COLUMNS} data={SAMPLE_DATA} rowKey={ROW_KEY} />);

      // Each header text appears in its own <th>.
      expect(screen.getByRole("columnheader", { name: "Name" })).toBeInTheDocument();
      expect(screen.getByRole("columnheader", { name: "Email" })).toBeInTheDocument();
      expect(screen.getByRole("columnheader", { name: "Role" })).toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // 2. Generic typing
  // -------------------------------------------------------------------------
  describe("generic typing", () => {
    it("preserves the generic TRow type in column.render and rowKey (compile-time)", () => {
      // The TypeScript compiler verifies that `row.name` is a string and
      // `row.id` is a string at the call site below. Failure would surface
      // as a `tsc --noEmit` error before the runtime assertion runs.
      const typedColumns: ReadonlyArray<TableColumn<UserRow>> = [
        { key: "name", header: "Name", render: (row) => row.name },
      ];

      render(<Table<UserRow> columns={typedColumns} data={SAMPLE_DATA} rowKey={(row) => row.id} />);

      expect(screen.getByText("Alice")).toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // 3. Row keys (data-testid)
  // -------------------------------------------------------------------------
  describe("row keys (data-testid)", () => {
    it('attaches data-testid="table-row-{rowKey}" to each data row', () => {
      render(<Table columns={BASIC_COLUMNS} data={SAMPLE_DATA} rowKey={ROW_KEY} />);

      // Every row in SAMPLE_DATA should be addressable by its id.
      expect(screen.getByTestId("table-row-u1")).toBeInTheDocument();
      expect(screen.getByTestId("table-row-u2")).toBeInTheDocument();
      expect(screen.getByTestId("table-row-u3")).toBeInTheDocument();
      expect(screen.getByTestId("table-row-u4")).toBeInTheDocument();
      expect(screen.getByTestId("table-row-u5")).toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // 4. Sortable headers
  // -------------------------------------------------------------------------
  describe("sortable headers", () => {
    it('wraps sortable header content in a <button> with data-testid="table-header-sort-{key}"', () => {
      const handleSort = vi.fn();
      render(
        <Table
          columns={MIXED_SORTABLE_COLUMNS}
          data={SAMPLE_DATA}
          rowKey={ROW_KEY}
          onSortChange={handleSort}
        />,
      );

      const sortButton = screen.getByTestId("table-header-sort-name");
      expect(sortButton).toBeInTheDocument();
      expect(sortButton.tagName).toBe("BUTTON");

      // Email is not sortable, so no sort button rendered for it.
      expect(screen.queryByTestId("table-header-sort-email")).not.toBeInTheDocument();
    });

    it('sortable buttons have type="button"', () => {
      const handleSort = vi.fn();
      render(
        <Table
          columns={MIXED_SORTABLE_COLUMNS}
          data={SAMPLE_DATA}
          rowKey={ROW_KEY}
          onSortChange={handleSort}
        />,
      );

      const sortButton = screen.getByTestId("table-header-sort-name");
      expect(sortButton).toHaveAttribute("type", "button");
    });

    it("does not render a sort button when onSortChange is omitted (sortable flag is idempotent)", () => {
      // The Table treats `sortable: true` as inert when onSortChange is not
      // supplied - the parent didn't actually wire up sorting yet, so the
      // header should fall back to the static label.
      render(<Table columns={MIXED_SORTABLE_COLUMNS} data={SAMPLE_DATA} rowKey={ROW_KEY} />);

      expect(screen.queryByTestId("table-header-sort-name")).not.toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // 5. Sort cycle (null -> asc -> desc -> null; new column resets to asc)
  // -------------------------------------------------------------------------
  describe("sort cycle", () => {
    it("clicking on a different column starts at asc", async () => {
      const handleSort = vi.fn();
      render(
        <Table
          columns={TWO_SORTABLE_COLUMNS}
          data={SAMPLE_DATA}
          rowKey={ROW_KEY}
          sortKey="name"
          sortDirection="asc"
          onSortChange={handleSort}
        />,
      );

      await userEvent.click(screen.getByTestId("table-header-sort-email"));

      expect(handleSort).toHaveBeenCalledTimes(1);
      expect(handleSort).toHaveBeenCalledWith("email", "asc");
    });

    it("clicking on an inactive sortable column starts at asc when no sortKey is set", async () => {
      const handleSort = vi.fn();
      render(
        <Table
          columns={TWO_SORTABLE_COLUMNS}
          data={SAMPLE_DATA}
          rowKey={ROW_KEY}
          onSortChange={handleSort}
        />,
      );

      await userEvent.click(screen.getByTestId("table-header-sort-name"));

      expect(handleSort).toHaveBeenCalledWith("name", "asc");
    });

    it("clicking on the active asc column rotates to desc", async () => {
      const handleSort = vi.fn();
      render(
        <Table
          columns={TWO_SORTABLE_COLUMNS}
          data={SAMPLE_DATA}
          rowKey={ROW_KEY}
          sortKey="name"
          sortDirection="asc"
          onSortChange={handleSort}
        />,
      );

      await userEvent.click(screen.getByTestId("table-header-sort-name"));

      expect(handleSort).toHaveBeenCalledWith("name", "desc");
    });

    it("clicking on the active desc column rotates to null", async () => {
      const handleSort = vi.fn();
      render(
        <Table
          columns={TWO_SORTABLE_COLUMNS}
          data={SAMPLE_DATA}
          rowKey={ROW_KEY}
          sortKey="name"
          sortDirection="desc"
          onSortChange={handleSort}
        />,
      );

      await userEvent.click(screen.getByTestId("table-header-sort-name"));

      expect(handleSort).toHaveBeenCalledWith("name", null);
    });

    it("clicking on a column whose direction is null rotates back to asc", async () => {
      const handleSort = vi.fn();
      render(
        <Table
          columns={TWO_SORTABLE_COLUMNS}
          data={SAMPLE_DATA}
          rowKey={ROW_KEY}
          sortKey="name"
          sortDirection={null}
          onSortChange={handleSort}
        />,
      );

      await userEvent.click(screen.getByTestId("table-header-sort-name"));

      expect(handleSort).toHaveBeenCalledWith("name", "asc");
    });

    it("activates sort via Enter key on a focused sort header (keyboard)", async () => {
      const handleSort = vi.fn();
      render(
        <Table
          columns={TWO_SORTABLE_COLUMNS}
          data={SAMPLE_DATA}
          rowKey={ROW_KEY}
          sortKey="name"
          sortDirection="asc"
          onSortChange={handleSort}
        />,
      );

      const button = screen.getByTestId("table-header-sort-name");
      button.focus();
      await userEvent.keyboard("{Enter}");

      expect(handleSort).toHaveBeenCalledWith("name", "desc");
    });

    it("activates sort via Space key on a focused sort header (keyboard)", async () => {
      const handleSort = vi.fn();
      render(
        <Table
          columns={TWO_SORTABLE_COLUMNS}
          data={SAMPLE_DATA}
          rowKey={ROW_KEY}
          sortKey="name"
          sortDirection="asc"
          onSortChange={handleSort}
        />,
      );

      const button = screen.getByTestId("table-header-sort-name");
      button.focus();
      // userEvent.keyboard understands a literal space character.
      await userEvent.keyboard(" ");

      expect(handleSort).toHaveBeenCalledWith("name", "desc");
    });
  });

  // -------------------------------------------------------------------------
  // 6. aria-sort attribute
  // -------------------------------------------------------------------------
  describe("aria-sort attribute", () => {
    it('renders aria-sort="ascending" on the active asc column header', () => {
      const handleSort = vi.fn();
      render(
        <Table
          columns={TWO_SORTABLE_COLUMNS}
          data={SAMPLE_DATA}
          rowKey={ROW_KEY}
          sortKey="name"
          sortDirection="asc"
          onSortChange={handleSort}
        />,
      );

      const headers = screen.getAllByRole("columnheader");
      // First header is "Name" per TWO_SORTABLE_COLUMNS order.
      expect(headers[0]).toHaveAttribute("aria-sort", "ascending");
    });

    it('renders aria-sort="descending" on the active desc column header', () => {
      const handleSort = vi.fn();
      render(
        <Table
          columns={TWO_SORTABLE_COLUMNS}
          data={SAMPLE_DATA}
          rowKey={ROW_KEY}
          sortKey="name"
          sortDirection="desc"
          onSortChange={handleSort}
        />,
      );

      const headers = screen.getAllByRole("columnheader");
      expect(headers[0]).toHaveAttribute("aria-sort", "descending");
    });

    it('renders aria-sort="none" on a sortable but inactive column', () => {
      const handleSort = vi.fn();
      render(
        <Table
          columns={TWO_SORTABLE_COLUMNS}
          data={SAMPLE_DATA}
          rowKey={ROW_KEY}
          sortKey="name"
          sortDirection="asc"
          onSortChange={handleSort}
        />,
      );

      const headers = screen.getAllByRole("columnheader");
      // Email is sortable but the active sort is "name", so aria-sort="none".
      expect(headers[1]).toHaveAttribute("aria-sort", "none");
    });

    it("omits the aria-sort attribute on non-sortable column headers", () => {
      const handleSort = vi.fn();
      render(
        <Table
          columns={MIXED_SORTABLE_COLUMNS}
          data={SAMPLE_DATA}
          rowKey={ROW_KEY}
          onSortChange={handleSort}
        />,
      );

      const headers = screen.getAllByRole("columnheader");
      // Email is non-sortable; the attribute should NOT be present.
      expect(headers[1]).not.toHaveAttribute("aria-sort");
    });
  });

  // -------------------------------------------------------------------------
  // 7. Sort icons (Lucide React: ArrowUp / ArrowDown / ArrowUpDown)
  //
  // Lucide React 0.468.0 emits SVGs with `aria-hidden="true"` (so they are
  // hidden from the accessibility tree) and adds the per-icon class
  // `lucide-{kebab-case-icon-name}` plus the generic `lucide` class to
  // every icon. We locate the SVG via plain DOM querySelector inside the
  // sort button - getByRole("img") would not match because the SVG has
  // no explicit role and is aria-hidden.
  // -------------------------------------------------------------------------
  describe("sort icons", () => {
    it("renders the ArrowUp icon when the column is sorted asc", () => {
      const handleSort = vi.fn();
      render(
        <Table
          columns={TWO_SORTABLE_COLUMNS}
          data={SAMPLE_DATA}
          rowKey={ROW_KEY}
          sortKey="name"
          sortDirection="asc"
          onSortChange={handleSort}
        />,
      );

      const button = screen.getByTestId("table-header-sort-name");
      const svg = button.querySelector("svg");
      expect(svg).not.toBeNull();
      expect(svg).toHaveClass("lucide-arrow-up");
      // Defense-in-depth: assert the inactive icons are NOT present.
      expect(svg).not.toHaveClass("lucide-arrow-down");
      expect(svg).not.toHaveClass("lucide-arrow-up-down");
    });

    it("renders the ArrowDown icon when the column is sorted desc", () => {
      const handleSort = vi.fn();
      render(
        <Table
          columns={TWO_SORTABLE_COLUMNS}
          data={SAMPLE_DATA}
          rowKey={ROW_KEY}
          sortKey="name"
          sortDirection="desc"
          onSortChange={handleSort}
        />,
      );

      const button = screen.getByTestId("table-header-sort-name");
      const svg = button.querySelector("svg");
      expect(svg).not.toBeNull();
      expect(svg).toHaveClass("lucide-arrow-down");
      expect(svg).not.toHaveClass("lucide-arrow-up-down");
    });

    it("renders the ArrowUpDown icon when the column is sortable but not the active sort", () => {
      const handleSort = vi.fn();
      render(
        <Table
          columns={TWO_SORTABLE_COLUMNS}
          data={SAMPLE_DATA}
          rowKey={ROW_KEY}
          sortKey="name"
          sortDirection="asc"
          onSortChange={handleSort}
        />,
      );

      // Email is sortable but the active sort key is "name", so the email
      // header renders the inactive ArrowUpDown indicator.
      const button = screen.getByTestId("table-header-sort-email");
      const svg = button.querySelector("svg");
      expect(svg).not.toBeNull();
      expect(svg).toHaveClass("lucide-arrow-up-down");
    });

    it("renders the ArrowUpDown icon when the column is sortable and direction is null", () => {
      const handleSort = vi.fn();
      render(
        <Table
          columns={TWO_SORTABLE_COLUMNS}
          data={SAMPLE_DATA}
          rowKey={ROW_KEY}
          sortKey="name"
          sortDirection={null}
          onSortChange={handleSort}
        />,
      );

      const button = screen.getByTestId("table-header-sort-name");
      const svg = button.querySelector("svg");
      expect(svg).not.toBeNull();
      expect(svg).toHaveClass("lucide-arrow-up-down");
    });

    it('marks every sort icon with aria-hidden="true" so screen readers ignore the visual glyph', () => {
      const handleSort = vi.fn();
      render(
        <Table
          columns={TWO_SORTABLE_COLUMNS}
          data={SAMPLE_DATA}
          rowKey={ROW_KEY}
          sortKey="name"
          sortDirection="asc"
          onSortChange={handleSort}
        />,
      );

      const nameSvg = screen.getByTestId("table-header-sort-name").querySelector("svg");
      const emailSvg = screen.getByTestId("table-header-sort-email").querySelector("svg");
      expect(nameSvg).toHaveAttribute("aria-hidden", "true");
      expect(emailSvg).toHaveAttribute("aria-hidden", "true");
    });
  });

  // -------------------------------------------------------------------------
  // 8. Empty state
  // -------------------------------------------------------------------------
  describe("empty state", () => {
    it('renders the default "No records to display." message when data is empty', () => {
      render(<Table columns={BASIC_COLUMNS} data={[]} rowKey={ROW_KEY} />);

      expect(screen.getByText("No records to display.")).toBeInTheDocument();
    });

    it("renders the custom emptyState slot when provided", () => {
      render(
        <Table
          columns={BASIC_COLUMNS}
          data={[]}
          rowKey={ROW_KEY}
          emptyState={<span>No users yet</span>}
        />,
      );

      expect(screen.getByText("No users yet")).toBeInTheDocument();
      // The default text must NOT also appear when a custom emptyState wins.
      expect(screen.queryByText("No records to display.")).not.toBeInTheDocument();
    });

    it("renders the empty-state cell with colSpan equal to columns.length", () => {
      render(<Table columns={BASIC_COLUMNS} data={[]} rowKey={ROW_KEY} />);

      // The empty-state row contains exactly one <td> spanning all columns.
      const cell = screen.getByRole("cell");
      // colSpan attribute serializes as lowercase "colspan" in the DOM.
      expect(cell).toHaveAttribute("colspan", "3");
    });

    it("does NOT render any data rows when data is empty", () => {
      render(<Table columns={BASIC_COLUMNS} data={[]} rowKey={ROW_KEY} />);

      // No row carries a `table-row-*` testid in the empty branch.
      expect(screen.queryByTestId(/^table-row-/)).not.toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // 9. Loading state
  // -------------------------------------------------------------------------
  describe("loading state", () => {
    it("renders 5 skeleton rows when isLoading=true", () => {
      render(<Table columns={BASIC_COLUMNS} data={[]} rowKey={ROW_KEY} isLoading={true} />);

      // Scope DOM queries to the table so we never accidentally pick up
      // siblings of the rendered Table that happen to share the role.
      // Skeleton rows carry aria-hidden="true" so they are excluded from
      // the a11y tree by design - we query them via plain DOM querySelector
      // to verify presence + count.
      const table = screen.getByRole("table");
      const skeletonRows = table.querySelectorAll('tbody tr[aria-hidden="true"]');
      expect(skeletonRows).toHaveLength(5);

      // Sanity: the only a11y-visible row in the loading branch is the
      // header row.
      const visibleRows = within(table).getAllByRole("row");
      expect(visibleRows).toHaveLength(1);
    });

    it("each skeleton row contains an animate-pulse placeholder", () => {
      render(<Table columns={BASIC_COLUMNS} data={[]} rowKey={ROW_KEY} isLoading={true} />);

      const table = screen.getByRole("table");
      const skeletonRows = table.querySelectorAll('tbody tr[aria-hidden="true"]');
      // At least one animate-pulse div per skeleton row.
      skeletonRows.forEach((row) => {
        const placeholders = row.querySelectorAll("div.animate-pulse");
        expect(placeholders.length).toBeGreaterThanOrEqual(1);
      });
    });

    it("does NOT render data rows when isLoading=true even when data is non-empty", () => {
      render(
        <Table columns={BASIC_COLUMNS} data={SAMPLE_DATA} rowKey={ROW_KEY} isLoading={true} />,
      );

      // None of the row data should be rendered.
      expect(screen.queryByText("Alice")).not.toBeInTheDocument();
      expect(screen.queryByText("Bob")).not.toBeInTheDocument();
      // No `table-row-*` testids either.
      expect(screen.queryByTestId(/^table-row-/)).not.toBeInTheDocument();
    });

    it("does NOT render the empty state when isLoading=true and data=[]", () => {
      render(<Table columns={BASIC_COLUMNS} data={[]} rowKey={ROW_KEY} isLoading={true} />);

      expect(screen.queryByText("No records to display.")).not.toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // 10. Row click handling (mouse + keyboard)
  // -------------------------------------------------------------------------
  describe("row click handling", () => {
    it("clickable rows have cursor-pointer class and tabIndex=0", () => {
      const handleRowClick = vi.fn();
      render(
        <Table
          columns={BASIC_COLUMNS}
          data={SAMPLE_DATA}
          rowKey={ROW_KEY}
          onRowClick={handleRowClick}
        />,
      );

      const row = screen.getByTestId("table-row-u1");
      expect(row).toHaveClass("cursor-pointer");
      expect(row).toHaveAttribute("tabindex", "0");
    });

    it("non-clickable rows do NOT have cursor-pointer or tabindex", () => {
      render(<Table columns={BASIC_COLUMNS} data={SAMPLE_DATA} rowKey={ROW_KEY} />);

      const row = screen.getByTestId("table-row-u1");
      expect(row).not.toHaveClass("cursor-pointer");
      expect(row).not.toHaveAttribute("tabindex");
    });

    it("clicking a row fires onRowClick with (row, index)", async () => {
      const handleRowClick = vi.fn();
      render(
        <Table
          columns={BASIC_COLUMNS}
          data={SAMPLE_DATA}
          rowKey={ROW_KEY}
          onRowClick={handleRowClick}
        />,
      );

      // u2 is index 1 in SAMPLE_DATA.
      await userEvent.click(screen.getByTestId("table-row-u2"));

      expect(handleRowClick).toHaveBeenCalledTimes(1);
      expect(handleRowClick).toHaveBeenCalledWith(SAMPLE_DATA[1], 1);
    });

    it("pressing Enter on a focused row fires onRowClick", async () => {
      const handleRowClick = vi.fn();
      render(
        <Table
          columns={BASIC_COLUMNS}
          data={SAMPLE_DATA}
          rowKey={ROW_KEY}
          onRowClick={handleRowClick}
        />,
      );

      const row = screen.getByTestId("table-row-u3");
      row.focus();
      await userEvent.keyboard("{Enter}");

      // u3 is index 2.
      expect(handleRowClick).toHaveBeenCalledWith(SAMPLE_DATA[2], 2);
    });

    it("pressing Space on a focused row fires onRowClick", async () => {
      const handleRowClick = vi.fn();
      render(
        <Table
          columns={BASIC_COLUMNS}
          data={SAMPLE_DATA}
          rowKey={ROW_KEY}
          onRowClick={handleRowClick}
        />,
      );

      const row = screen.getByTestId("table-row-u4");
      row.focus();
      // userEvent.keyboard accepts a literal space; the source's onKeyDown
      // checks `event.key === ' '` to recognize the Space activation.
      await userEvent.keyboard(" ");

      // u4 is index 3.
      expect(handleRowClick).toHaveBeenCalledWith(SAMPLE_DATA[3], 3);
    });

    it("pressing an unrelated key on a focused row does NOT fire onRowClick", async () => {
      const handleRowClick = vi.fn();
      render(
        <Table
          columns={BASIC_COLUMNS}
          data={SAMPLE_DATA}
          rowKey={ROW_KEY}
          onRowClick={handleRowClick}
        />,
      );

      const row = screen.getByTestId("table-row-u1");
      row.focus();
      // Tab is a navigation key, not an activation key.
      await userEvent.keyboard("a");

      expect(handleRowClick).not.toHaveBeenCalled();
    });
  });

  // -------------------------------------------------------------------------
  // 11. Column align (left default / right / center)
  // -------------------------------------------------------------------------
  describe("column align", () => {
    it('align="right" adds text-right class to the header and every cell in the column', () => {
      const alignedColumns: ReadonlyArray<TableColumn<UserRow>> = [
        { key: "name", header: "Name", render: (row) => row.name, align: "right" },
      ];
      const { container } = render(
        <Table columns={alignedColumns} data={SAMPLE_DATA} rowKey={ROW_KEY} />,
      );

      const header = screen.getByRole("columnheader");
      expect(header).toHaveClass("text-right");

      // Every <td data-column="name"> should also carry text-right.
      const cells = container.querySelectorAll('td[data-column="name"]');
      expect(cells.length).toBe(5);
      cells.forEach((cell) => {
        expect(cell).toHaveClass("text-right");
      });
    });

    it('align="center" adds text-center class to the header and cells', () => {
      const alignedColumns: ReadonlyArray<TableColumn<UserRow>> = [
        { key: "name", header: "Name", render: (row) => row.name, align: "center" },
      ];
      const { container } = render(
        <Table columns={alignedColumns} data={SAMPLE_DATA} rowKey={ROW_KEY} />,
      );

      const header = screen.getByRole("columnheader");
      expect(header).toHaveClass("text-center");

      const cells = container.querySelectorAll('td[data-column="name"]');
      cells.forEach((cell) => {
        expect(cell).toHaveClass("text-center");
      });
    });

    it("default (no align) renders text-left and not text-right or text-center", () => {
      // BASIC_COLUMNS has no `align` so the source falls back to "left".
      render(<Table columns={BASIC_COLUMNS} data={SAMPLE_DATA} rowKey={ROW_KEY} />);

      const headers = screen.getAllByRole("columnheader");
      headers.forEach((header) => {
        // Source spec maps `align: 'left'` (the default) to `text-left`.
        expect(header).toHaveClass("text-left");
        expect(header).not.toHaveClass("text-right");
        expect(header).not.toHaveClass("text-center");
      });
    });
  });

  // -------------------------------------------------------------------------
  // 12. Column hideBelow (sm/md/lg/xl)
  // -------------------------------------------------------------------------
  describe("column hideBelow", () => {
    it.each(["sm", "md", "lg", "xl"] as const)(
      'hideBelow="%s" applies hidden + {bp}:table-cell to header and cells',
      (bp) => {
        const cols: ReadonlyArray<TableColumn<UserRow>> = [
          { key: "name", header: "Name", render: (row) => row.name },
          { key: "email", header: "Email", render: (row) => row.email, hideBelow: bp },
        ];
        const { container } = render(<Table columns={cols} data={SAMPLE_DATA} rowKey={ROW_KEY} />);

        // Email header
        const emailHeader = container.querySelector('th[data-column="email"]');
        expect(emailHeader).not.toBeNull();
        expect(emailHeader).toHaveClass("hidden");
        expect(emailHeader).toHaveClass(`${bp}:table-cell`);

        // Email cells in every data row
        const emailCells = container.querySelectorAll('td[data-column="email"]');
        expect(emailCells.length).toBe(5);
        emailCells.forEach((cell) => {
          expect(cell).toHaveClass("hidden");
          expect(cell).toHaveClass(`${bp}:table-cell`);
        });

        // Sanity: the non-hideBelow column should NOT have these classes.
        const nameHeader = container.querySelector('th[data-column="name"]');
        expect(nameHeader).not.toHaveClass("hidden");
      },
    );
  });

  // -------------------------------------------------------------------------
  // 13. Caption (sr-only)
  // -------------------------------------------------------------------------
  describe("caption", () => {
    it('renders <caption className="sr-only"> when the caption prop is provided', () => {
      const { container } = render(
        <Table columns={BASIC_COLUMNS} data={SAMPLE_DATA} rowKey={ROW_KEY} caption="Users list" />,
      );

      const captionEl = container.querySelector("caption");
      expect(captionEl).not.toBeNull();
      expect(captionEl).toHaveTextContent("Users list");
      expect(captionEl).toHaveClass("sr-only");
    });

    it("does NOT render <caption> when the caption prop is omitted", () => {
      const { container } = render(
        <Table columns={BASIC_COLUMNS} data={SAMPLE_DATA} rowKey={ROW_KEY} />,
      );

      expect(container.querySelector("caption")).toBeNull();
    });

    it("does NOT render <caption> when the caption prop is the empty string", () => {
      // The source treats `""` the same as `undefined` to avoid an empty
      // <caption> that would silently degrade the accessibility tree.
      const { container } = render(
        <Table columns={BASIC_COLUMNS} data={SAMPLE_DATA} rowKey={ROW_KEY} caption="" />,
      );

      expect(container.querySelector("caption")).toBeNull();
    });
  });

  // -------------------------------------------------------------------------
  // 14. Custom className passthrough (top-level + table + column-scoped)
  // -------------------------------------------------------------------------
  describe("custom className passthrough", () => {
    it("top-level className applies to the wrapping div", () => {
      render(
        <Table
          columns={BASIC_COLUMNS}
          data={SAMPLE_DATA}
          rowKey={ROW_KEY}
          className="custom-wrapper"
        />,
      );

      const wrapper = screen.getByTestId("table-container");
      expect(wrapper).toHaveClass("custom-wrapper");
    });

    it("tableClassName applies to the <table> element", () => {
      render(
        <Table
          columns={BASIC_COLUMNS}
          data={SAMPLE_DATA}
          rowKey={ROW_KEY}
          tableClassName="custom-table"
        />,
      );

      const table = screen.getByRole("table");
      expect(table).toHaveClass("custom-table");
    });

    it("column.headerClassName applies to the <th> for that column", () => {
      const cols: ReadonlyArray<TableColumn<UserRow>> = [
        {
          key: "name",
          header: "Name",
          render: (row) => row.name,
          headerClassName: "custom-th",
        },
        { key: "email", header: "Email", render: (row) => row.email },
      ];
      const { container } = render(<Table columns={cols} data={SAMPLE_DATA} rowKey={ROW_KEY} />);

      const nameHeader = container.querySelector('th[data-column="name"]');
      expect(nameHeader).toHaveClass("custom-th");

      // The other column's header must NOT have inherited the class.
      const emailHeader = container.querySelector('th[data-column="email"]');
      expect(emailHeader).not.toHaveClass("custom-th");
    });

    it("column.cellClassName applies to every <td> in that column", () => {
      const cols: ReadonlyArray<TableColumn<UserRow>> = [
        {
          key: "name",
          header: "Name",
          render: (row) => row.name,
          cellClassName: "custom-td",
        },
        { key: "email", header: "Email", render: (row) => row.email },
      ];
      const { container } = render(<Table columns={cols} data={SAMPLE_DATA} rowKey={ROW_KEY} />);

      const nameCells = container.querySelectorAll('td[data-column="name"]');
      expect(nameCells.length).toBe(5);
      nameCells.forEach((cell) => {
        expect(cell).toHaveClass("custom-td");
      });

      // The cellClassName must NOT leak onto cells in other columns.
      const emailCells = container.querySelectorAll('td[data-column="email"]');
      emailCells.forEach((cell) => {
        expect(cell).not.toHaveClass("custom-td");
      });
    });

    it("column.widthClassName applies to both the header and every cell in that column", () => {
      const cols: ReadonlyArray<TableColumn<UserRow>> = [
        {
          key: "name",
          header: "Name",
          render: (row) => row.name,
          widthClassName: "w-1/4",
        },
        { key: "email", header: "Email", render: (row) => row.email },
      ];
      const { container } = render(<Table columns={cols} data={SAMPLE_DATA} rowKey={ROW_KEY} />);

      const nameHeader = container.querySelector('th[data-column="name"]');
      expect(nameHeader).toHaveClass("w-1/4");

      const nameCells = container.querySelectorAll('td[data-column="name"]');
      nameCells.forEach((cell) => {
        expect(cell).toHaveClass("w-1/4");
      });
    });
  });

  // -------------------------------------------------------------------------
  // 15. Strongly-typed sort transitions (it.each matrix proving the
  //     SortDirection type aligns with the cycle the source emits).
  // -------------------------------------------------------------------------
  describe("strongly-typed sort transitions", () => {
    interface SortCase {
      readonly initial: SortDirection;
      readonly expected: SortDirection;
    }

    const cases: ReadonlyArray<SortCase> = [
      { initial: null, expected: "asc" },
      { initial: "asc", expected: "desc" },
      { initial: "desc", expected: null },
    ];

    it.each(cases)(
      "rotates from $initial to $expected on the same column",
      async ({ initial, expected }) => {
        const handleSort = vi.fn();
        render(
          <Table
            columns={TWO_SORTABLE_COLUMNS}
            data={SAMPLE_DATA}
            rowKey={ROW_KEY}
            sortKey="name"
            sortDirection={initial}
            onSortChange={handleSort}
          />,
        );

        await userEvent.click(screen.getByTestId("table-header-sort-name"));

        expect(handleSort).toHaveBeenCalledWith("name", expected);
      },
    );
  });
});
