/**
 * Table.tsx - Strongly-typed table primitive for the Sales-Connections SPA.
 *
 * Generic over the row type so consumers get typed access to every column's
 * cell data. Used by:
 *   - frontend/src/features/connections/ConnectionFeed.tsx (F-004)
 *   - frontend/src/features/admin/UserManagement.tsx (F-014)
 *   - frontend/src/features/admin/RecordModeration.tsx (F-014)
 *
 * Capabilities:
 *   - Column definitions via TableColumn<TRow>[] with typed `render`
 *     callbacks that receive the row and an index.
 *   - Optional `sortable: true` per column; clicks fire `onSortChange`.
 *     The Table is a CONTROLLED sort component: the parent owns sortKey
 *     and sortDirection. The Table only renders indicators and emits clicks.
 *   - Click-to-row callback (`onRowClick`) for detail navigation.
 *   - Empty-state slot (`emptyState`) rendered when `data.length === 0`.
 *   - Loading-state (`isLoading`) renders a 5-row skeleton.
 *   - Mobile overflow: the wrapping <div> applies overflow-x-auto so the
 *     full table scrolls horizontally when the viewport is narrower than
 *     the column collective width.
 *   - Per-column responsive `hideBelow` breakpoint (sm | md | lg | xl) so
 *     low-priority columns can collapse on narrow viewports.
 *
 * What this primitive does NOT do (intentionally):
 *   - Pagination (parent renders pagination controls separately).
 *   - Filtering (parent applies filters before passing data).
 *   - Sorting computation (parent computes sorted data; Table only
 *     emits sort events).
 *   - Row selection (deferred to post-MVP).
 *   - Inline editing (deferred to post-MVP).
 *   - Virtualization (the 10K-record cap per AAP Sec 0.7.3 makes this
 *     unnecessary; the Connection Feed paginates server-side at the API).
 *
 * Conventions per AAP Sec 0.7.7:
 *   - TailwindCSS utility classes only (no inline styles).
 *   - Strict TypeScript; no `any`; generics throughout.
 *   - Lucide-React icons for sort indicators.
 *   - clsx for conditional class composition.
 *   - Semantic HTML (<table>, <thead>, <tbody>, <th scope="col">, <td>,
 *     <caption>) per AAP Sec 0.7.7.
 *   - <button type="button"> for sortable headers (keyboard-accessible
 *     by default; correct semantics for an interactive control).
 *   - Double quotes per project Prettier configuration (singleQuote: false).
 *   - No business logic; pure presentational primitive (no hooks, no
 *     state, no effects, no refs, no context).
 *
 * Coordinates with:
 *   - frontend/src/features/connections/ConnectionFeed.tsx - primary
 *     consumer (F-004); composes <Table<ConnectionRead>> with six columns
 *     and five sort dimensions.
 *   - frontend/src/features/admin/UserManagement.tsx - admin consumer
 *     (F-014); <Table<UserRead>> with role-edit dropdown in one cell.
 *   - frontend/src/features/admin/RecordModeration.tsx - admin consumer
 *     (F-014); <Table<ConnectionRead>> with hard-delete action in one cell.
 *   - frontend/tailwind.config.ts - provides shadow-card, brand-500, and
 *     the slate ramp consumed by the styling here.
 *   - frontend/src/styles/index.css - provides the :focus-visible ring
 *     base that pairs with focus-visible:outline-* utilities below.
 */

import { type JSX, type KeyboardEvent, type ReactNode } from "react";
import { ArrowDown, ArrowUp, ArrowUpDown } from "lucide-react";
import clsx from "clsx";

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

/**
 * Sort direction.
 *
 * `null` represents "not currently sorted by this column" - the third leg
 * of the asc -> desc -> null cycle. Many table libraries skip the null
 * state and toggle only between asc and desc, but that surprises users
 * who want to clear an active sort and restore the data's natural order.
 * Three-state sort matches user expectations for the Connection Feed
 * (F-004), where the default order is `submission_date DESC` from the
 * server but a user might want to temporarily sort by company and then
 * clear that sort.
 */
export type SortDirection = "asc" | "desc" | null;

/**
 * Cell horizontal alignment.
 *
 * `left` is the default and matches the natural reading order for text;
 * `right` is conventional for numeric / date columns; `center` is
 * occasionally used for icon-only columns or short-label badges.
 */
export type ColumnAlign = "left" | "center" | "right";

/**
 * Responsive breakpoint key for collapsing low-priority columns on narrow
 * viewports. Matches Tailwind's default breakpoint scale:
 *   - "sm" -> 640px
 *   - "md" -> 768px
 *   - "lg" -> 1024px
 *   - "xl" -> 1280px
 *
 * A column with `hideBelow: "md"` is hidden below 768px and visible at
 * `md:` and up. The class added is `hidden md:table-cell`.
 */
export type ColumnHideBreakpoint = "sm" | "md" | "lg" | "xl";

/**
 * A typed column definition.
 *
 * Generic over the row type so the `render` callback receives a typed
 * row argument (no `any` casts at the call site). Consumers usually
 * declare the column array as a const so the column.key strings retain
 * their literal types for downstream sort-key correlation:
 *
 *   const columns = [
 *     { key: "full_name", header: "Name", render: (r) => r.full_name },
 *     { key: "company", header: "Company", render: (r) => r.company,
 *       sortable: true },
 *   ] as const satisfies ReadonlyArray<TableColumn<ConnectionRead>>;
 */
export interface TableColumn<TRow> {
  /**
   * Stable string identifier for this column. Used as:
   *   - The React key for the <th> and each <td> in the column.
   *   - The correlation key for the parent's sortKey state.
   *   - The `data-column` attribute on each cell so tests can target a
   *     specific column without relying on positional indices.
   */
  readonly key: string;

  /**
   * Header content. Most columns pass a plain string, but a ReactNode
   * type lets consumers pass JSX (e.g., a tooltip-wrapped label, an
   * icon + text composition) when needed.
   */
  readonly header: ReactNode;

  /**
   * Render the cell for a given row. Called once per row at render time.
   * Receives the typed row plus the row index (zero-based) for cases
   * where the rendering depends on position (e.g., zebra-stripe
   * overrides driven by the consumer rather than CSS).
   */
  readonly render: (row: TRow, rowIndex: number) => ReactNode;

  /**
   * When true, the header becomes a clickable button that emits sort
   * events; the Table renders the appropriate ArrowUp / ArrowDown
   * indicator. The Table is a CONTROLLED sort component - the parent
   * owns the sortKey + sortDirection state and computes the sorted
   * data accordingly. The Table itself never mutates row order.
   */
  readonly sortable?: boolean;

  /** Cell horizontal alignment; defaults to "left". */
  readonly align?: ColumnAlign;

  /**
   * Optional className applied to every <td> in this column. Use for
   * column-specific tweaks (text size, monospace font for IDs, max-width
   * for truncation) without forking the primitive.
   */
  readonly cellClassName?: string;

  /**
   * Optional className applied to the <th>. Use sparingly; most styling
   * should come from the props the Table already exposes.
   */
  readonly headerClassName?: string;

  /**
   * Optional fixed width hint in Tailwind syntax (e.g., "w-32", "w-1/4",
   * "min-w-[12rem]"). Applied to both the <th> and every <td> in the
   * column so the column has a consistent width across header and body.
   */
  readonly widthClassName?: string;

  /**
   * When set, the column is collapsed below this breakpoint via
   * `hidden <bp>:table-cell`. Use to hide low-priority columns on
   * narrow viewports. For example, the Connection Feed hides the
   * "Submitter" and "Date" columns below `md:` so the mobile view
   * shows only the most actionable information (Name, Company,
   * Involvement, Status).
   */
  readonly hideBelow?: ColumnHideBreakpoint;
}

/**
 * Public props for the Table component.
 *
 * Generic over the row type. Pass it explicitly when inference is
 * ambiguous (e.g., when `data` is empty):
 *
 *   <Table<ConnectionRead> columns={columns} data={data} ... />
 */
export interface TableProps<TRow> {
  /** Column definitions. Order in this array dictates left-to-right order. */
  readonly columns: ReadonlyArray<TableColumn<TRow>>;

  /**
   * Row data. The Table renders rows in array order; sorting is the
   * parent's responsibility (see `sortKey` / `sortDirection` /
   * `onSortChange` for the controlled sort cycle).
   */
  readonly data: ReadonlyArray<TRow>;

  /**
   * Stable string id for each row; used as the React key.
   *
   * Typically `(row) => row.id`, but receives the index too for cases
   * where rows have no natural id (e.g., a derived ranking table).
   */
  readonly rowKey: (row: TRow, index: number) => string;

  /**
   * Optional row click handler. When supplied, rows render as
   * `cursor-pointer` with hover and focus-visible affordances and
   * become keyboard-activatable (Enter / Space). Called with the row
   * and its index.
   */
  readonly onRowClick?: (row: TRow, index: number) => void;

  /** Currently-sorted column key, or undefined when no sort is active. */
  readonly sortKey?: string;

  /**
   * Currently-sorted direction, or undefined / null when no sort is
   * active for any column. The parent maintains this state.
   */
  readonly sortDirection?: SortDirection;

  /**
   * Sort change handler. Called with the new key and direction when a
   * sortable header is clicked. Cycle: null -> "asc" -> "desc" -> null.
   * The parent applies the new sort and re-renders the Table with the
   * sorted data; the Table itself never mutates row order.
   */
  readonly onSortChange?: (key: string, direction: SortDirection) => void;

  /**
   * Empty-state slot rendered inside a single full-width cell when
   * `data.length === 0` and `isLoading` is false. Pass a string for
   * a simple message, a ReactNode for richer content (e.g., an "Add
   * Connection" CTA button under the message). Defaults to a plain
   * "No records to display." string when undefined.
   */
  readonly emptyState?: ReactNode;

  /**
   * Loading state. When true, renders five skeleton rows with pulsing
   * placeholders instead of the data. Skeleton rows give a more
   * accurate sense of "data is coming" than a spinner because the user
   * can see the layout taking shape; this is especially valuable for
   * the Connection Feed where the row layout reflects business meaning.
   */
  readonly isLoading?: boolean;

  /**
   * Optional className merged onto the wrapping <div>. Use to control
   * outer layout (margin, max-height with overflow-y-auto for sticky
   * scrolling, etc.) without forking the primitive.
   */
  readonly className?: string;

  /**
   * Optional className merged onto the <table>. Use sparingly; most
   * styling should be column-level via TableColumn.cellClassName /
   * .headerClassName / .widthClassName.
   */
  readonly tableClassName?: string;

  /**
   * Visually-hidden caption read by screen readers. Provides an
   * accessible name for the table that explains its content (e.g.,
   * "Connection records sorted by submission date"). When undefined,
   * no caption element is rendered. Required by WCAG 1.3.1 / 4.1.2 for
   * non-trivial data tables, though small admin-only tables can omit it.
   */
  readonly caption?: string;
}

// ---------------------------------------------------------------------------
// Class-name maps
//
// These maps are module-level constants (not inlined inside the Table
// function) so:
//   1. The JIT Tailwind compiler picks up every utility at build time.
//      Class names assembled inside a function via string concatenation
//      can be missed by the static scanner; by writing the literal class
//      names as plain string values here the scanner sees them and
//      includes the corresponding utilities in the output bundle.
//   2. Tests can iterate over the keys without re-instantiating the
//      component for each case.
//   3. The class strings sit in one place for design review.
// ---------------------------------------------------------------------------

const ALIGN_CLASSES: Record<ColumnAlign, string> = {
  left: "text-left",
  center: "text-center",
  right: "text-right",
};

const HIDE_BELOW_CLASSES: Record<ColumnHideBreakpoint, string> = {
  sm: "hidden sm:table-cell",
  md: "hidden md:table-cell",
  lg: "hidden lg:table-cell",
  xl: "hidden xl:table-cell",
};

// ---------------------------------------------------------------------------
// Sort-cycle helper
// ---------------------------------------------------------------------------

/**
 * Compute the next sort state when a sortable header is clicked.
 *
 * Cycle, given currentKey and clickedKey:
 *   - clickedKey !== currentKey       -> ("clickedKey", "asc")
 *     (Switching to a new column always starts ascending - the most
 *     common user expectation.)
 *   - clickedKey === currentKey
 *     - currentDirection === null     -> ("clickedKey", "asc")
 *     - currentDirection === "asc"    -> ("clickedKey", "desc")
 *     - currentDirection === "desc"   -> ("clickedKey", null)
 *       (Third click clears the sort, restoring the data's natural
 *       order from the server. This third leg is what differentiates
 *       a thoughtful sort UX from a frustrating two-state toggle.)
 *
 * Pure function with no React or DOM dependency, so it is trivially
 * unit-testable from the test file without rendering anything.
 */
function nextSortState(
  clickedKey: string,
  currentKey: string | undefined,
  currentDirection: SortDirection | undefined,
): { key: string; direction: SortDirection } {
  if (clickedKey !== currentKey) {
    return { key: clickedKey, direction: "asc" };
  }
  if (currentDirection === null || currentDirection === undefined) {
    return { key: clickedKey, direction: "asc" };
  }
  if (currentDirection === "asc") {
    return { key: clickedKey, direction: "desc" };
  }
  return { key: clickedKey, direction: null };
}

// ---------------------------------------------------------------------------
// Table component
// ---------------------------------------------------------------------------

/**
 * Resolve which Lucide icon to render in a sortable header.
 *
 * Hoisted helper (no React state) so the Table component body stays
 * focused on JSX composition. Returns the icon component as a value
 * (a JSX-compatible component reference) rather than rendering it
 * directly so the caller can apply size and color classes consistently.
 */
function pickSortIcon(
  isSortedHere: boolean,
  sortDirection: SortDirection | undefined,
): typeof ArrowUp | typeof ArrowDown | typeof ArrowUpDown {
  if (isSortedHere && sortDirection === "asc") {
    return ArrowUp;
  }
  if (isSortedHere && sortDirection === "desc") {
    return ArrowDown;
  }
  return ArrowUpDown;
}

/**
 * Resolve the `aria-sort` attribute value for a header cell.
 *
 * Per WCAG 1.3.1 / 4.1.2 (and the ARIA Authoring Practices for tables),
 * a sortable column header MUST expose its sort state via aria-sort.
 * Possible values:
 *   - "ascending" / "descending" when the column is the active sort.
 *   - "none" when the column is sortable but not the active sort.
 *   - undefined when the column is not sortable (so the attribute is
 *     omitted entirely rather than emitted with a meaningless value).
 *
 * The string-literal return type (rather than `string | undefined`)
 * matches React's typing for the aria-sort prop, which is restricted
 * to the four AriaAttributes-defined values plus undefined.
 */
function pickAriaSort(
  isSortable: boolean,
  isSortedHere: boolean,
  sortDirection: SortDirection | undefined,
): "ascending" | "descending" | "none" | undefined {
  if (isSortedHere && sortDirection === "asc") {
    return "ascending";
  }
  if (isSortedHere && sortDirection === "desc") {
    return "descending";
  }
  if (isSortable) {
    return "none";
  }
  return undefined;
}

/**
 * Generic, typed table primitive.
 *
 * @example
 *   <Table<ConnectionRead>
 *     columns={columns}
 *     data={records}
 *     rowKey={(row) => row.id}
 *     sortKey={sort.key}
 *     sortDirection={sort.direction}
 *     onSortChange={(k, d) => setSort({ key: k, direction: d })}
 *     onRowClick={(row) => navigate(`/connections/${row.id}`)}
 *     emptyState="No connections yet. Add the first one."
 *     caption="Connection records"
 *   />
 *
 * The TRow generic is inferred from `data` when `columns` and `rowKey`
 * agree on the same row type. Pass the generic explicitly when `data`
 * is empty (`<Table<ConnectionRead> data={[]} ... />`) or when working
 * around inference quirks with const tuples.
 */
export function Table<TRow>({
  columns,
  data,
  rowKey,
  onRowClick,
  sortKey,
  sortDirection,
  onSortChange,
  emptyState,
  isLoading,
  className,
  tableClassName,
  caption,
}: TableProps<TRow>): JSX.Element {
  // -------------------------------------------------------------------------
  // Sort-header click + keyboard handlers
  //
  // The Table delegates sort state to the parent via onSortChange; these
  // local handlers compute the next state from the current state via
  // nextSortState() and bubble it up. Doing nothing when the column is
  // not sortable or when no onSortChange handler was supplied makes the
  // sortable flag idempotent (consumers can flip it on/off without also
  // having to wire/unwire the change handler).
  // -------------------------------------------------------------------------
  const handleHeaderClick = (column: TableColumn<TRow>): void => {
    if (column.sortable !== true || onSortChange === undefined) {
      return;
    }
    const next = nextSortState(column.key, sortKey, sortDirection);
    onSortChange(next.key, next.direction);
  };

  const handleHeaderKeyDown = (
    event: KeyboardEvent<HTMLButtonElement>,
    column: TableColumn<TRow>,
  ): void => {
    // The native <button> already activates on Enter and Space, but we
    // intercept the events to call preventDefault explicitly. This stops
    // the browser from scrolling the page on Space (the default browser
    // behavior when Space is pressed on a focused button) and ensures
    // consistency with the row-click keyboard handler below.
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      handleHeaderClick(column);
    }
  };

  // -------------------------------------------------------------------------
  // Row-click keyboard handler
  //
  // Native <tr> is not a focusable element, so onRowClick consumers get
  // tabIndex={0} + a manual Enter/Space handler to make rows operable
  // by keyboard users. The handler is created inline per row only when
  // onRowClick is supplied.
  // -------------------------------------------------------------------------
  const isRowClickable = onRowClick !== undefined;

  // -------------------------------------------------------------------------
  // Body content selection
  //
  // Three mutually exclusive states: loading -> skeleton rows; empty ->
  // single full-width empty-state row; data -> mapped row elements.
  // The conditional is written as nested ternaries so the entire <tbody>
  // child expression evaluates to a single ReactNode (an element, an
  // array, or a fragment) and React reconciles it cleanly.
  // -------------------------------------------------------------------------
  let bodyContent: ReactNode;
  if (isLoading === true) {
    bodyContent = renderLoadingRows(columns);
  } else if (data.length === 0) {
    bodyContent = renderEmptyRow(columns, emptyState);
  } else {
    bodyContent = data.map((row, rowIndex) => {
      const key = rowKey(row, rowIndex);
      return (
        <tr
          key={key}
          onClick={isRowClickable ? () => onRowClick(row, rowIndex) : undefined}
          tabIndex={isRowClickable ? 0 : undefined}
          onKeyDown={
            isRowClickable
              ? (event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    onRowClick(row, rowIndex);
                  }
                }
              : undefined
          }
          // aria-rowindex is 1-indexed and includes the header row, so
          // the first data row is index 2. Helps screen readers announce
          // row position when the user navigates with table commands.
          aria-rowindex={rowIndex + 2}
          className={clsx(
            "transition-colors",
            isRowClickable &&
              "cursor-pointer hover:bg-slate-50 focus-visible:bg-slate-50 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-brand-500",
          )}
          data-testid={`table-row-${key}`}
        >
          {columns.map((column) => (
            <td
              key={column.key}
              data-column={column.key}
              className={clsx(
                "px-4 py-3 text-sm text-slate-900",
                ALIGN_CLASSES[column.align ?? "left"],
                column.widthClassName,
                column.cellClassName,
                column.hideBelow ? HIDE_BELOW_CLASSES[column.hideBelow] : undefined,
              )}
            >
              {column.render(row, rowIndex)}
            </td>
          ))}
        </tr>
      );
    });
  }

  return (
    <div
      className={clsx(
        "w-full overflow-x-auto rounded-lg border border-slate-200 bg-white shadow-card",
        className,
      )}
      data-testid="table-container"
    >
      <table className={clsx("min-w-full divide-y divide-slate-200", tableClassName)}>
        {caption !== undefined && caption !== "" && (
          <caption className="sr-only">{caption}</caption>
        )}
        <thead className="bg-slate-50">
          <tr>
            {columns.map((column) => {
              const isSortedHere = column.key === sortKey;
              const isSortable = column.sortable === true && onSortChange !== undefined;
              const SortIcon = pickSortIcon(isSortedHere, sortDirection);
              const ariaSort = pickAriaSort(isSortable, isSortedHere, sortDirection);

              return (
                <th
                  key={column.key}
                  scope="col"
                  data-column={column.key}
                  aria-sort={ariaSort}
                  className={clsx(
                    "px-4 py-3 text-xs font-semibold uppercase tracking-wide text-slate-600",
                    ALIGN_CLASSES[column.align ?? "left"],
                    column.widthClassName,
                    column.headerClassName,
                    column.hideBelow ? HIDE_BELOW_CLASSES[column.hideBelow] : undefined,
                  )}
                >
                  {isSortable ? (
                    <button
                      type="button"
                      onClick={() => handleHeaderClick(column)}
                      onKeyDown={(event) => handleHeaderKeyDown(event, column)}
                      className={clsx(
                        "inline-flex items-center gap-1 rounded text-xs font-semibold uppercase tracking-wide transition-colors hover:text-slate-900",
                        // Per Visual Consistency QA Issue 6, mobile
                        // touch targets MUST be at least 44x44 px.
                        // Sort buttons in <th> are very narrow at
                        // mobile widths because the column header is
                        // short text; min-h/min-w-[44px] enforces
                        // the floor without expanding header
                        // chrome on desktop (sm:min-h-0/sm:min-w-0
                        // restores the natural compact size).
                        "min-h-[44px] min-w-[44px] sm:min-h-0 sm:min-w-0",
                        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-500",
                        isSortedHere ? "text-slate-900" : "text-slate-600",
                      )}
                      data-testid={`table-header-sort-${column.key}`}
                    >
                      <span>{column.header}</span>
                      <SortIcon
                        aria-hidden="true"
                        className={clsx(
                          "h-3.5 w-3.5",
                          isSortedHere ? "text-slate-700" : "text-slate-400",
                        )}
                      />
                    </button>
                  ) : (
                    column.header
                  )}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-200 bg-white">{bodyContent}</tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Loading + empty row helpers
//
// Hoisted out of the Table function so they do not get re-created on
// every render. Both return a JSX.Element that is splatted directly into
// the <tbody>.
// ---------------------------------------------------------------------------

/**
 * Render five skeleton rows with pulsing placeholder bars. Used when
 * `isLoading` is true. Each cell respects the column's `align` and
 * `hideBelow` so the skeleton layout matches the eventual data layout
 * (no jarring shift when data arrives).
 *
 * `aria-hidden="true"` is applied to each skeleton row so assistive
 * technologies announce only the loading status of the parent surface
 * (typically a busy region), not the placeholder structure itself.
 */
function renderLoadingRows<TRow>(columns: ReadonlyArray<TableColumn<TRow>>): JSX.Element {
  const skeletonRows = [0, 1, 2, 3, 4];
  return (
    <>
      {skeletonRows.map((skeletonIndex) => (
        <tr key={`skeleton-${skeletonIndex}`} aria-hidden="true">
          {columns.map((column) => (
            <td
              key={column.key}
              className={clsx(
                "px-4 py-3",
                ALIGN_CLASSES[column.align ?? "left"],
                column.hideBelow ? HIDE_BELOW_CLASSES[column.hideBelow] : undefined,
              )}
            >
              <div className="h-4 w-3/4 animate-pulse rounded bg-slate-100" />
            </td>
          ))}
        </tr>
      ))}
    </>
  );
}

/**
 * Render a single empty-state row spanning every column. Used when
 * `data.length === 0` and `isLoading` is false.
 *
 * `colSpan={columns.length}` is the correct way to render an empty-state
 * inside a <tbody>; alternatives (extra <div>s sibling to the table)
 * break table semantics and break screen-reader navigation.
 *
 * The placeholder string "No records to display." applies when the
 * consumer did not provide a custom `emptyState` slot.
 */
function renderEmptyRow<TRow>(
  columns: ReadonlyArray<TableColumn<TRow>>,
  emptyState: ReactNode,
): JSX.Element {
  return (
    <tr>
      <td
        colSpan={columns.length}
        className="px-4 py-12 text-center text-sm text-slate-500"
        data-testid="table-empty-state"
      >
        {emptyState !== undefined && emptyState !== null ? emptyState : "No records to display."}
      </td>
    </tr>
  );
}
