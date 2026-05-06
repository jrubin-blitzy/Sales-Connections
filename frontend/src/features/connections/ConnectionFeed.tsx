/**
 * ConnectionFeed.tsx - F-004 Connection Feed and Dashboard.
 *
 * The primary surface for the sales team. Renders a paginated, filterable,
 * sortable table of all org-scoped non-deleted connection records. Mounts
 * at /feed per AAP Sec 0.2.3 (and is the post-login landing route for
 * Viewer and Contributor roles per AAP Sec 0.5.4).
 *
 * Six row elements per AAP Sec 0.5.4:
 *   1. Name (with detail-page link)
 *   2. Company
 *   3. Involvement badge (3-state - delegated to InvolvementBadge)
 *   4. Submitter / owner_display_name
 *   5. Outreach status chip (4-state - delegated to StatusChip; mutation
 *      gated to Admin/Viewer at the StatusChip / RoleGate boundary)
 *   6. Submission date
 * (plus a 7th tags column with the inline +N overflow indicator)
 *
 * Six filter dimensions per AAP Sec 0.5.4:
 *   1. Search (full_name_search) - text contains
 *   2. Company                   - text contains
 *   3. Involvement               - multi-select (3 enum values)
 *   4. Outreach status           - multi-select (4 enum values)
 *   5. Submission date from
 *   6. Submission date to
 *  (plus Tags - multi-select against the org's tag list, fetched via
 *   useTagsQuery; the seventh dimension that completes AAP Sec 0.5.4)
 *
 * Five sort dimensions per AAP Sec 0.4.3:
 *   submission_date | full_name | company | owner_display_name |
 *   outreach_status
 *
 * URL state encoding:
 *   All filter, sort, and pagination state lives in URLSearchParams so
 *   the page is shareable, bookmarkable, and back-button-friendly. The
 *   useSearchParams hook from react-router-dom provides the bidirectional
 *   binding. The component is stateless w.r.t. UI state; React state
 *   lives only inside child UI primitives.
 *
 * Performance:
 *   Backend composite index (org_id, deleted_at, submission_date DESC)
 *   on the records table guarantees responsive feeds at the 10K-record
 *   ceiling per AAP Sec 0.7.3. The feed uses page-based pagination
 *   (page + page_size) rather than infinite scroll for predictable URL
 *   sharing; cursor pagination is deferred per AAP scope discipline.
 *
 * Accessibility:
 *   - Labeled <section aria-labelledby> region (not <main>; the
 *     document's <main> is in App.tsx). <h1> page heading.
 *   - Table primitive uses <th scope="col"> + aria-sort for sortable
 *     columns (delegated to the Table primitive).
 *   - Filter chip toggles use aria-pressed for screen-reader feedback.
 *   - Empty state and error state use role="status" / role="alert".
 *
 * Status chip inline mutation:
 *   Each row's StatusChip carries the recordId so admitted roles
 *   (Admin/Viewer) can mutate the status inline. The optimistic update
 *   in useUpdateStatusMutation flips the chip immediately before
 *   server confirmation; rollback on error is handled by the mutation
 *   hook's onError callback (see api/connections.ts).
 *
 * RBAC and security per AAP Sec 0.7.1 invariant 7:
 *   API-layer authorization is authoritative. The StatusChip's
 *   <RoleGate> (consumed by StatusChip itself) is a UX courtesy; the
 *   backend @requires_role decorator on PATCH /api/connections/:id/status
 *   is the only authoritative gate.
 *
 * Conventions per AAP Sec 0.7.7:
 *   - TailwindCSS utility classes only (no inline `style`).
 *   - Strict TypeScript; no `any`; explicit `JSX.Element` return.
 *   - Named exports only (no default export).
 *   - Lucide-React icons throughout (Filter, Plus, Search, X).
 *   - clsx for conditional class composition.
 *   - Path imports use the `@/` alias declared in vite.config.ts.
 *   - All HTTP traffic via @/api/connections (which routes through
 *     @/api/client.ts for correlation IDs and 401-redirect uniformity).
 *
 * Coordinates with:
 *   - @/api/connections                    useConnectionsQuery,
 *                                          useTagsQuery, and the
 *                                          ConnectionListParams shape.
 *   - @/components/ui/{Badge,Button,Input,Select,Table}
 *                                          Tailwind primitives.
 *   - @/features/connections/InvolvementBadge per-row 3-state badge.
 *   - @/features/connections/StatusChip     per-row 4-state chip.
 *   - @/schemas/connection                  ConnectionRead, enum tuples.
 *   - @/router.tsx (consumer)               mounts this at /feed.
 */

import { useCallback, useEffect, useMemo, useRef, useState, type ChangeEvent, type JSX } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { Filter, Plus, Search, X } from "lucide-react";
import clsx from "clsx";

import { useCompaniesQuery, useConnectionsQuery, useTagsQuery, type ConnectionListParams } from "@/api/connections";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Select } from "@/components/ui/Select";
import { Table, type SortDirection, type TableColumn } from "@/components/ui/Table";
import { InvolvementBadge } from "@/features/connections/InvolvementBadge";
import { StatusChip } from "@/features/connections/StatusChip";
import {
  INVOLVEMENT_VALUES,
  OUTREACH_STATUS_VALUES,
  type ConnectionRead,
  type InvolvementValue,
  type OutreachStatusValue,
} from "@/schemas/connection";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/**
 * Page-size options for pagination. The default 25 matches the backend's
 * default page size (`_DEFAULT_PAGE_SIZE = 25` in
 * `app/services/connections.py`) so a SPA without an explicit page_size
 * URL param stays in lockstep with the backend's behavior. The hard
 * upper bound (`_MAX_PAGE_SIZE = 100`) is enforced server-side; the SPA
 * stays well under it for the typical 10K-record scale ceiling.
 */
const PAGE_SIZE_OPTIONS = [10, 25, 50, 100] as const;
const DEFAULT_PAGE_SIZE = 25;

/**
 * Page-size Select options. Stringified once at module-evaluation time
 * because the native <select> element only accepts string option values;
 * we coerce back to a number on the change handler.
 */
const PAGE_SIZE_SELECT_OPTIONS: ReadonlyArray<{ value: string; label: string }> =
  PAGE_SIZE_OPTIONS.map((sz) => ({ value: String(sz), label: String(sz) }));

/**
 * The five sort dimensions exposed by the backend's `_SORT_COLUMNS`
 * whitelist per AAP Sec 0.4.3. Exposing the same constant here keeps
 * the SPA's sort UI in lockstep with the backend's accepted sort keys;
 * a typo on either side would surface as a 422 from the API.
 */
type SortKey =
  | "submission_date"
  | "full_name"
  | "company"
  | "owner_display_name"
  | "outreach_status";

/**
 * Set of allowed SortKey strings so a runtime guard (URL params come
 * from a user-controlled string) can reject invalid keys before they
 * round-trip to the backend.
 */
const SORT_KEYS: ReadonlySet<string> = new Set<SortKey>([
  "submission_date",
  "full_name",
  "company",
  "owner_display_name",
  "outreach_status",
]);

/**
 * The default sort applied when the URL has no sort parameters.
 * `submission_date DESC` corresponds to "newest first" which matches
 * both the backend default and the user's primary mental model: the
 * most recently logged connections are usually the most actionable.
 */
const DEFAULT_SORT_KEY: SortKey = "submission_date";
const DEFAULT_SORT_DIR: "asc" | "desc" = "desc";

// ---------------------------------------------------------------------------
// URL search-param helpers
// ---------------------------------------------------------------------------

/**
 * Read a scalar search-param value. Returns undefined for missing OR
 * empty-string values (so the consumer can omit the field from the API
 * request entirely; an empty filter is the same as no filter, and the
 * backend would otherwise treat `""` as "match every empty string").
 */
function readScalarParam(params: URLSearchParams, key: string): string | undefined {
  const v = params.get(key);
  return v && v.length > 0 ? v : undefined;
}

/**
 * Read a multi-valued search-param and filter to allowed values.
 *
 * The user-controlled URL could contain stale or hand-rolled values
 * (e.g., a value removed from the enum after a release); this helper
 * silently drops anything that does not appear in `allowedValues` so
 * the resulting array is always type-safe.
 *
 * Returns undefined when no valid values remain so the caller can omit
 * the field entirely from the API request payload.
 */
function readMultiParam<T extends string>(
  params: URLSearchParams,
  key: string,
  allowedValues: ReadonlyArray<T>,
): ReadonlyArray<T> | undefined {
  const all = params
    .getAll(key)
    .filter((v): v is T => (allowedValues as ReadonlyArray<string>).includes(v));
  return all.length > 0 ? all : undefined;
}

/**
 * Read a multi-valued search-param of free-form (non-enum) strings and
 * drop any empty entries. Used for tag_ids and owner_user_ids where the
 * universe of valid values is too large to enumerate client-side.
 */
function readMultiStringParam(params: URLSearchParams, key: string): ReadonlyArray<string> {
  return params.getAll(key).filter((v) => v.length > 0);
}

/**
 * Translate the URLSearchParams into the typed `ConnectionListParams`
 * consumed by `useConnectionsQuery`.
 *
 * The cache key for the connection list query is keyed by the params
 * object so we always pass an explicit `page`, `page_size`, `sort`, and
 * `sort_dir` (defaulted from the constants above) to keep the cache key
 * stable across navigation; without this, equivalent URLs that omit a
 * default param would produce different cache entries.
 */
function paramsToListParams(params: URLSearchParams): ConnectionListParams {
  const sortRaw = readScalarParam(params, "sort");
  const sortDirRaw = readScalarParam(params, "sort_dir");
  const sort: SortKey = sortRaw && SORT_KEYS.has(sortRaw) ? (sortRaw as SortKey) : DEFAULT_SORT_KEY;
  const sort_dir: "asc" | "desc" =
    sortDirRaw === "asc" || sortDirRaw === "desc" ? sortDirRaw : DEFAULT_SORT_DIR;

  const pageStr = readScalarParam(params, "page");
  const pageSizeStr = readScalarParam(params, "page_size");
  const pageParsed = pageStr ? Number.parseInt(pageStr, 10) : Number.NaN;
  const page = Number.isFinite(pageParsed) && pageParsed > 0 ? pageParsed : 1;
  const pageSizeParsed = pageSizeStr ? Number.parseInt(pageSizeStr, 10) : Number.NaN;
  const page_size = (PAGE_SIZE_OPTIONS as ReadonlyArray<number>).includes(pageSizeParsed)
    ? pageSizeParsed
    : DEFAULT_PAGE_SIZE;

  const result: ConnectionListParams = { page, page_size, sort, sort_dir };

  const company = readScalarParam(params, "company");
  if (company !== undefined) result.company = company;
  const fullName = readScalarParam(params, "q");
  if (fullName !== undefined) result.full_name_search = fullName;
  const dateFrom = readScalarParam(params, "from");
  if (dateFrom !== undefined) result.submission_date_from = dateFrom;
  const dateTo = readScalarParam(params, "to");
  if (dateTo !== undefined) result.submission_date_to = dateTo;

  const involvement = readMultiParam(params, "involvement", INVOLVEMENT_VALUES);
  if (involvement !== undefined) result.involvement = involvement;
  const outreachStatus = readMultiParam(params, "outreach_status", OUTREACH_STATUS_VALUES);
  if (outreachStatus !== undefined) result.outreach_status = outreachStatus;

  const ownerIds = readMultiStringParam(params, "owner");
  if (ownerIds.length > 0) result.owner_user_ids = ownerIds;
  const tagIds = readMultiStringParam(params, "tag");
  if (tagIds.length > 0) result.tag_ids = tagIds;

  return result;
}

/**
 * Build a Table-primitive sort state (key + direction) from the URL.
 * Sort is always present (defaulted from DEFAULT_SORT_*) so the Table
 * primitive's controlled-sort indicator always shows the active column.
 */
function paramsToSortState(params: URLSearchParams): { key: SortKey; direction: SortDirection } {
  const sortRaw = readScalarParam(params, "sort");
  const sortDirRaw = readScalarParam(params, "sort_dir");
  const key: SortKey = sortRaw && SORT_KEYS.has(sortRaw) ? (sortRaw as SortKey) : DEFAULT_SORT_KEY;
  const direction: SortDirection =
    sortDirRaw === "asc" || sortDirRaw === "desc" ? sortDirRaw : DEFAULT_SORT_DIR;
  return { key, direction };
}

/**
 * Format an ISO 8601 datetime string as a short locale-aware date.
 *
 * Returns an em-dash (U+2014) for empty/null inputs so empty cells are
 * visually distinct from cells that simply lack a column. The Date
 * constructor does not throw on garbage input - it produces an Invalid
 * Date whose `.getTime()` is NaN; the explicit guard converts that into
 * the same em-dash placeholder as a missing value.
 */
function formatDate(iso: string | null | undefined): string {
  if (!iso) return "\u2014";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "\u2014";
  return date.toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

// ---------------------------------------------------------------------------
// CompanyCombobox subcomponent
// ---------------------------------------------------------------------------

interface CompanyComboboxProps {
  readonly value: string;
  readonly onChange: (value: string) => void;
  readonly options: string[];
}

function CompanyCombobox({ value, onChange, options }: CompanyComboboxProps): JSX.Element {
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  const suggestions = useMemo(
    () => options.filter((o) => o.toLowerCase().includes(value.toLowerCase())).slice(0, 8),
    [options, value],
  );

  useEffect(() => {
    function onPointerDown(e: PointerEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, []);

  return (
    <div ref={containerRef} className="relative">
      <Input
        label="Company"
        placeholder="Acme"
        value={value}
        onChange={(e: ChangeEvent<HTMLInputElement>) => {
          onChange(e.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onKeyDown={(e) => {
          if (e.key === "Escape") setOpen(false);
        }}
        inputSize="sm"
        data-testid="connection-feed-filter-company"
      />
      {open && suggestions.length > 0 && (
        <ul
          role="listbox"
          aria-label="Company suggestions"
          className="absolute top-full left-0 right-0 z-10 mt-1 max-h-48 overflow-auto rounded-md border border-slate-200 bg-white py-1 shadow-lg"
        >
          {suggestions.map((option) => (
            <li
              key={option}
              role="option"
              aria-selected={option === value}
              className={clsx(
                "cursor-pointer px-3 py-1.5 text-xs text-slate-900",
                option === value ? "bg-slate-100 font-medium" : "hover:bg-slate-50",
              )}
              onMouseDown={(e) => {
                e.preventDefault();
                onChange(option);
                setOpen(false);
              }}
            >
              {option}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// FilterBar subcomponent
// ---------------------------------------------------------------------------

/**
 * Props for the FilterBar. The bar is a presentational helper that
 * reads from the parent's URLSearchParams and emits new URLSearchParams
 * via onParamsChange when the user mutates a filter; the parent owns
 * the actual setSearchParams call so the URL writeback path stays
 * centralized.
 */
interface FilterBarProps {
  readonly searchParams: URLSearchParams;
  readonly onParamsChange: (next: URLSearchParams) => void;
}

/**
 * Filter chip class composer. Used for involvement, outreach status,
 * and tag pill toggles. Hoisted out of the component so the JIT
 * Tailwind compiler picks up every utility at build time and so the
 * conditional logic sits in one place.
 *
 * Active state uses the brand palette; inactive state uses the slate
 * palette. Both states share a consistent focus-visible ring for
 * keyboard accessibility (per AAP Sec 0.7.7).
 */
function chipClassName(selected: boolean): string {
  return clsx(
    "inline-flex items-center justify-center rounded-full border px-3 py-1 text-xs font-medium",
    // Per Visual Consistency QA Issue 6, mobile touch targets MUST
    // be at least 44x44 px (WCAG 2.5.5 / Apple HIG / Material) so
    // contributors using a phone do not mis-tap adjacent filter
    // pills. The min-h/min-w only apply at the mobile breakpoint;
    // at >=sm we relax back to the natural height so dense desktop
    // layouts remain compact. Using sm:min-h-0 / sm:min-w-0 keeps
    // the JIT-compiler happy with explicit utilities.
    "min-h-[44px] min-w-[44px] sm:min-h-0 sm:min-w-0",
    "transition-colors",
    "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-500",
    selected
      ? "border-brand-500 bg-brand-50 text-brand-700"
      : "border-slate-200 bg-white text-slate-700 hover:bg-slate-50",
  );
}

/**
 * The collapsible filter bar above the feed table.
 *
 * Six filter dimensions are exposed:
 *   - Search (q)               substring match on full_name
 *   - Company                  substring match on company
 *   - Submission date from / to  inclusive ISO date range
 *   - Involvement              multi-select pills (3 enum values)
 *   - Outreach status          multi-select pills (4 enum values)
 *   - Tags                     multi-select pills (driven by useTagsQuery)
 *
 * Filter changes immediately rewrite the URL (no separate "Apply"
 * button) which both makes the URL the single source of truth and
 * keeps the click count low. The TanStack Query stale-time on
 * useConnectionsQuery prevents a thundering-herd of refetches while
 * the user is typing.
 *
 * Pill toggles use aria-pressed so screen readers announce the
 * selected/unselected state on focus or activation.
 */
function FilterBar({ searchParams, onParamsChange }: FilterBarProps): JSX.Element {
  // Read current filter values from the URL. Empty string defaults are
  // safe because the controls never render undefined into a value
  // attribute (which would mark the input as uncontrolled).
  const company = readScalarParam(searchParams, "company") ?? "";
  const fullNameSearch = readScalarParam(searchParams, "q") ?? "";
  const dateFrom = readScalarParam(searchParams, "from") ?? "";
  const dateTo = readScalarParam(searchParams, "to") ?? "";
  const selectedInvolvement = searchParams.getAll("involvement");
  const selectedStatus = searchParams.getAll("outreach_status");
  const selectedTagIds = searchParams.getAll("tag");

  // The org-scoped tag list. TanStack Query handles caching across
  // navigations so the request fires once per session-org pair.
  const tagsQuery = useTagsQuery();
  const tagOptions = tagsQuery.data ?? [];

  const companiesQuery = useCompaniesQuery();
  const companyOptions = companiesQuery.data ?? [];

  /**
   * Set a scalar URL param to `value`, or delete it when `value` is empty.
   * Always resets `page` to 1 because a new filter narrows the result
   * set and `page=N` could otherwise point past the last page.
   */
  const setScalarParam = useCallback(
    (key: string, value: string): void => {
      const next = new URLSearchParams(searchParams);
      if (value.length > 0) {
        next.set(key, value);
      } else {
        next.delete(key);
      }
      next.delete("page");
      onParamsChange(next);
    },
    [searchParams, onParamsChange],
  );

  /**
   * Toggle a value in a multi-valued URL param. If the value is already
   * present the array shrinks; otherwise it grows. Always resets `page`
   * to 1 for the same reason as `setScalarParam`.
   *
   * The double-loop pattern (delete then re-append) is the simplest
   * way to "set" a multi-valued param given URLSearchParams' API.
   */
  const toggleMultiParam = useCallback(
    (key: string, value: string): void => {
      const next = new URLSearchParams(searchParams);
      const current = next.getAll(key);
      next.delete(key);
      if (current.includes(value)) {
        for (const v of current) {
          if (v !== value) next.append(key, v);
        }
      } else {
        for (const v of current) next.append(key, v);
        next.append(key, value);
      }
      next.delete("page");
      onParamsChange(next);
    },
    [searchParams, onParamsChange],
  );

  /**
   * Clear every filter while preserving the active sort. Pagination is
   * also reset to page 1 by virtue of constructing a new
   * URLSearchParams that omits the `page` key.
   */
  const handleClearAll = useCallback((): void => {
    const next = new URLSearchParams();
    const sort = readScalarParam(searchParams, "sort");
    const sortDir = readScalarParam(searchParams, "sort_dir");
    const pageSize = readScalarParam(searchParams, "page_size");
    if (sort !== undefined) next.set("sort", sort);
    if (sortDir !== undefined) next.set("sort_dir", sortDir);
    if (pageSize !== undefined) next.set("page_size", pageSize);
    onParamsChange(next);
  }, [searchParams, onParamsChange]);

  // Whether any filter is active; drives the visibility of the
  // "Clear all" affordance so a no-op control never appears.
  const hasActiveFilters =
    company.length > 0 ||
    fullNameSearch.length > 0 ||
    dateFrom.length > 0 ||
    dateTo.length > 0 ||
    selectedInvolvement.length > 0 ||
    selectedStatus.length > 0 ||
    selectedTagIds.length > 0;

  return (
    <section
      aria-label="Filters"
      className="flex flex-col gap-4 rounded-lg border border-slate-200 bg-white p-4 shadow-card"
      data-testid="connection-feed-filters"
    >
      <header className="flex items-center justify-between">
        <h2 className="flex items-center gap-2 text-sm font-semibold text-slate-700">
          <Filter aria-hidden="true" className="h-4 w-4" />
          Filters
        </h2>
        {hasActiveFilters && (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={handleClearAll}
            leftIcon={<X aria-hidden="true" />}
            data-testid="connection-feed-clear-filters"
          >
            Clear all
          </Button>
        )}
      </header>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Input
          label="Search name"
          placeholder="Jane Doe"
          value={fullNameSearch}
          onChange={(e: ChangeEvent<HTMLInputElement>) => setScalarParam("q", e.target.value)}
          leftIcon={<Search aria-hidden="true" />}
          inputSize="sm"
          data-testid="connection-feed-filter-search"
        />
        <CompanyCombobox
          value={company}
          onChange={(val) => setScalarParam("company", val)}
          options={companyOptions}
        />
        <Input
          label="Date from"
          type="date"
          value={dateFrom}
          onChange={(e: ChangeEvent<HTMLInputElement>) => setScalarParam("from", e.target.value)}
          inputSize="sm"
          data-testid="connection-feed-filter-date-from"
        />
        <Input
          label="Date to"
          type="date"
          value={dateTo}
          onChange={(e: ChangeEvent<HTMLInputElement>) => setScalarParam("to", e.target.value)}
          inputSize="sm"
          data-testid="connection-feed-filter-date-to"
        />
      </div>

      <fieldset className="flex flex-col gap-1.5">
        <legend className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-600">
          Involvement
        </legend>
        <div className="flex flex-wrap gap-2" role="group" aria-label="Involvement filter (any-of)">
          {INVOLVEMENT_VALUES.map((value: InvolvementValue) => {
            const selected = selectedInvolvement.includes(value);
            return (
              <button
                key={value}
                type="button"
                onClick={() => toggleMultiParam("involvement", value)}
                aria-pressed={selected}
                className={chipClassName(selected)}
                data-testid={`connection-feed-filter-involvement-${value
                  .replace(/\s+/g, "-")
                  .toLowerCase()}`}
              >
                {value}
              </button>
            );
          })}
        </div>
      </fieldset>

      <fieldset className="flex flex-col gap-1.5">
        <legend className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-600">
          Outreach status
        </legend>
        <div
          className="flex flex-wrap gap-2"
          role="group"
          aria-label="Outreach status filter (any-of)"
        >
          {OUTREACH_STATUS_VALUES.map((value: OutreachStatusValue) => {
            const selected = selectedStatus.includes(value);
            return (
              <button
                key={value}
                type="button"
                onClick={() => toggleMultiParam("outreach_status", value)}
                aria-pressed={selected}
                className={chipClassName(selected)}
                data-testid={`connection-feed-filter-status-${value
                  .replace(/\s+/g, "-")
                  .toLowerCase()}`}
              >
                {value}
              </button>
            );
          })}
        </div>
      </fieldset>

      {tagOptions.length > 0 && (
        <fieldset className="flex flex-col gap-1.5">
          <legend className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-600">
            Tags
          </legend>
          <div className="flex flex-wrap gap-2" role="group" aria-label="Tags filter (any-of)">
            {tagOptions.map((tag) => {
              const selected = selectedTagIds.includes(tag.id);
              return (
                <button
                  key={tag.id}
                  type="button"
                  onClick={() => toggleMultiParam("tag", tag.id)}
                  aria-pressed={selected}
                  className={chipClassName(selected)}
                  data-testid={`connection-feed-filter-tag-${tag.id}`}
                >
                  {tag.name}
                </button>
              );
            })}
          </div>
        </fieldset>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// PaginationBar subcomponent
// ---------------------------------------------------------------------------

/**
 * Props for the PaginationBar. Pure presentational; the parent owns the
 * URL writeback for `page` and `page_size`.
 */
interface PaginationBarProps {
  readonly page: number;
  readonly pageSize: number;
  readonly total: number;
  readonly isFetching: boolean;
  readonly onPageChange: (page: number) => void;
  readonly onPageSizeChange: (pageSize: number) => void;
}

/**
 * Page-based pagination controls (Previous / Next + page-size Select).
 *
 * Page-based (offset / limit) was selected over cursor pagination
 * because:
 *   - URL sharing requires arbitrary page jumps (a cursor would not
 *     round-trip cleanly through a copied URL).
 *   - The 10K-record scale ceiling per AAP Sec 0.7.3 keeps offset
 *     pagination performant even on the deepest pages.
 *   - The decision and trade-off are recorded in docs/decision-log.md
 *     per the Explainability rule.
 *
 * The Select primitive is consumed here for the page-size selector
 * (typed as the string union of allowed sizes) so the entire page
 * uses consistent design-system controls rather than mixing in raw
 * <select>.
 */
function PaginationBar({
  page,
  pageSize,
  total,
  isFetching,
  onPageChange,
  onPageSizeChange,
}: PaginationBarProps): JSX.Element {
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const startIdx = total === 0 ? 0 : (page - 1) * pageSize + 1;
  const endIdx = Math.min(total, page * pageSize);

  return (
    <nav
      aria-label="Pagination"
      className={clsx(
        "flex flex-col items-stretch justify-between gap-3",
        "rounded-lg border border-slate-200 bg-white p-3",
        "sm:flex-row sm:items-center",
      )}
      data-testid="connection-feed-pagination"
    >
      <p className="text-sm text-slate-600 tabular-nums">
        Showing <span className="font-medium">{startIdx.toLocaleString()}</span>
        {"\u2013"}
        <span className="font-medium">{endIdx.toLocaleString()}</span> of{" "}
        <span className="font-medium">{total.toLocaleString()}</span>
      </p>

      <div className="flex flex-col items-stretch gap-2 sm:flex-row sm:items-center">
        <div className="flex min-w-[8rem] items-center gap-2">
          <span
            id="connection-feed-page-size-label"
            className="whitespace-nowrap text-xs font-medium text-slate-600"
          >
            Per page
          </span>
          <Select
            value={String(pageSize)}
            onChange={(v: string) => {
              const parsed = Number.parseInt(v, 10);
              if ((PAGE_SIZE_OPTIONS as ReadonlyArray<number>).includes(parsed)) {
                onPageSizeChange(parsed);
              }
            }}
            options={PAGE_SIZE_SELECT_OPTIONS}
            aria-labelledby="connection-feed-page-size-label"
            selectClassName="h-8 py-1 text-xs"
            data-testid="connection-feed-page-size"
          />
        </div>

        <div className="flex items-center gap-1">
          <Button
            type="button"
            variant="secondary"
            size="sm"
            disabled={page <= 1 || isFetching}
            onClick={() => onPageChange(page - 1)}
            data-testid="connection-feed-prev-page"
          >
            Previous
          </Button>
          <span className="px-2 text-xs text-slate-600 tabular-nums">
            Page {page} of {totalPages}
          </span>
          <Button
            type="button"
            variant="secondary"
            size="sm"
            disabled={page >= totalPages || isFetching}
            onClick={() => onPageChange(page + 1)}
            data-testid="connection-feed-next-page"
          >
            Next
          </Button>
        </div>
      </div>
    </nav>
  );
}

// ---------------------------------------------------------------------------
// Public ConnectionFeed component
// ---------------------------------------------------------------------------

/**
 * The Connection Feed component (F-004).
 *
 * Renders the full feed surface: header with the Add Connection CTA,
 * filter bar above the table, paginated table with six row columns
 * plus a tags column, and a pagination bar below.
 *
 * The component takes no props (route-level component). State derives
 * entirely from the URL via useSearchParams; this makes filtered views
 * shareable, bookmarkable, and back-button-friendly (the user can
 * Cmd+click any URL to open it in a new tab and see exactly the same
 * filtered view).
 *
 * Per AAP Sec 0.7.1 invariant 7, role-gated UI is a UX courtesy. The
 * StatusChip handles its own RoleGate for the inline status mutation;
 * the backend RBAC decorator on PATCH /api/connections/:id/status is
 * the authoritative gate.
 */
export function ConnectionFeed(): JSX.Element {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();

  // -------------------------------------------------------------------------
  // Derive query / sort / pagination state from the URL.
  //
  // useMemo keeps the params object reference stable across re-renders
  // when the URL has not changed. TanStack Query keys by deep equality,
  // so reference stability is not strictly required for cache hits, but
  // it does prevent identity churn that would otherwise cascade through
  // the column-render closure dependencies below.
  // -------------------------------------------------------------------------
  const listParams = useMemo(() => paramsToListParams(searchParams), [searchParams]);
  const sortState = useMemo(() => paramsToSortState(searchParams), [searchParams]);

  const connectionsQuery = useConnectionsQuery(listParams);

  // -------------------------------------------------------------------------
  // URL writeback handlers
  //
  // Each handler computes a new URLSearchParams from the current value
  // and calls setSearchParams. We use replace: false so each filter
  // change is a navigation event (the user can press Back to undo),
  // matching the "shareable URL" architectural intent.
  // -------------------------------------------------------------------------
  const handleParamsChange = useCallback(
    (next: URLSearchParams): void => {
      setSearchParams(next, { replace: false });
    },
    [setSearchParams],
  );

  const handleSortChange = useCallback(
    (key: string, direction: SortDirection): void => {
      const next = new URLSearchParams(searchParams);
      if (direction === null) {
        next.delete("sort");
        next.delete("sort_dir");
      } else {
        next.set("sort", key);
        next.set("sort_dir", direction);
      }
      next.delete("page");
      setSearchParams(next, { replace: false });
    },
    [searchParams, setSearchParams],
  );

  const handlePageChange = useCallback(
    (newPage: number): void => {
      const next = new URLSearchParams(searchParams);
      if (newPage <= 1) {
        next.delete("page");
      } else {
        next.set("page", String(newPage));
      }
      setSearchParams(next, { replace: false });
    },
    [searchParams, setSearchParams],
  );

  const handlePageSizeChange = useCallback(
    (newPageSize: number): void => {
      const next = new URLSearchParams(searchParams);
      if (newPageSize === DEFAULT_PAGE_SIZE) {
        next.delete("page_size");
      } else {
        next.set("page_size", String(newPageSize));
      }
      next.delete("page");
      setSearchParams(next, { replace: false });
    },
    [searchParams, setSearchParams],
  );

  const handleRowClick = useCallback(
    (row: ConnectionRead): void => {
      navigate(`/connections/${row.id}`);
    },
    [navigate],
  );

  // -------------------------------------------------------------------------
  // Column definitions
  //
  // Six row elements per AAP Sec 0.5.4 plus a Tags column for the
  // F-008 tag chip display. Each column carries:
  //   - key       Stable id (used by Table for the React key and for
  //               correlating with the parent's sortKey state).
  //   - sortable  True for the five sort dimensions per AAP Sec 0.4.3.
  //   - hideBelow Mobile-responsive collapse breakpoint per AAP "mobile-
  //               responsive web only" constraint.
  //
  // The columns array has no closure dependencies on render-time state
  // (the row itself is the only argument to render), so the empty deps
  // array is correct.
  // -------------------------------------------------------------------------
  const columns = useMemo<ReadonlyArray<TableColumn<ConnectionRead>>>(
    () => [
      {
        key: "full_name",
        header: "Name",
        sortable: true,
        render: (row: ConnectionRead) => (
          <div className="flex flex-col">
            <Link
              to={`/connections/${row.id}`}
              onClick={(e) => e.stopPropagation()}
              className={clsx(
                "text-sm font-medium underline-offset-2 hover:underline",
                "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-500",
                row.deleted_at ? "text-slate-500 line-through" : "text-slate-900",
              )}
              data-testid={`connection-feed-row-name-${row.id}`}
            >
              {row.full_name}
            </Link>
            <span className="text-xs text-slate-500">{row.job_title}</span>
          </div>
        ),
      },
      {
        key: "company",
        header: "Company",
        sortable: true,
        hideBelow: "sm",
        render: (row: ConnectionRead) => (
          <span
            className={clsx("text-sm", row.deleted_at ? "text-slate-400" : "text-slate-700")}
            data-testid={`connection-feed-row-company-${row.id}`}
          >
            {row.company}
          </span>
        ),
      },
      {
        key: "involvement",
        header: "Involvement",
        sortable: false,
        render: (row: ConnectionRead) => (
          <span data-testid={`connection-feed-row-involvement-${row.id}`}>
            <InvolvementBadge value={row.involvement} />
          </span>
        ),
      },
      {
        key: "owner_display_name",
        header: "Submitter",
        sortable: true,
        hideBelow: "md",
        render: (row: ConnectionRead) => (
          <span
            className="text-sm text-slate-700"
            data-testid={`connection-feed-row-submitter-${row.id}`}
          >
            {row.owner_display_name}
          </span>
        ),
      },
      {
        key: "outreach_status",
        header: "Status",
        sortable: true,
        render: (row: ConnectionRead) => (
          <div data-testid={`connection-feed-row-status-${row.id}`}>
            <StatusChip
              recordId={row.id}
              value={row.outreach_status}
              disabled={row.deleted_at !== null}
            />
          </div>
        ),
      },
      {
        key: "submission_date",
        header: "Added",
        sortable: true,
        hideBelow: "lg",
        render: (row: ConnectionRead) => (
          <span
            className="text-xs text-slate-500 tabular-nums"
            data-testid={`connection-feed-row-date-${row.id}`}
          >
            {formatDate(row.submission_date)}
          </span>
        ),
      },
      {
        key: "tags",
        header: "Tags",
        sortable: false,
        hideBelow: "lg",
        render: (row: ConnectionRead) => {
          if (row.tags.length === 0) {
            return <span className="text-xs text-slate-400">{"\u2014"}</span>;
          }
          // Truncate at 3 chips with a +N overflow indicator so a
          // record with many tags does not blow up the row height.
          // The full tag list is visible on the detail view (F-011).
          const visible = row.tags.slice(0, 3);
          const overflow = row.tags.length - visible.length;
          return (
            <ul className="flex flex-wrap gap-1" data-testid={`connection-feed-row-tags-${row.id}`}>
              {visible.map((tag) => (
                <li key={tag.id}>
                  <Badge variant="brand" size="sm">
                    {tag.name}
                  </Badge>
                </li>
              ))}
              {overflow > 0 && (
                <li>
                  <Badge variant="neutral" size="sm">
                    +{overflow}
                  </Badge>
                </li>
              )}
            </ul>
          );
        },
      },
    ],
    [],
  );

  // -------------------------------------------------------------------------
  // Render
  //
  // The page resolves to one of three table states (handled inside the
  // Table primitive: loading -> 5 skeleton rows; empty -> emptyState
  // slot; data -> mapped rows). The error banner renders BELOW the
  // filter bar so the user can still see and adjust filters when the
  // request fails - matching the "error state renders inline error
  // banner (NOT a fallback page)" behavior in the agent prompt.
  // -------------------------------------------------------------------------
  const items: ReadonlyArray<ConnectionRead> = connectionsQuery.data?.items ?? [];
  const total = connectionsQuery.data?.total ?? 0;
  const page = listParams.page ?? 1;
  const pageSize = listParams.page_size ?? DEFAULT_PAGE_SIZE;

  return (
    // Per Visual Consistency QA Issue 7, the route component renders
    // a labeled <section> rather than a second <main> landmark. The
    // outer <main> in App.tsx is the document's primary main; nesting
    // a second <main> violated the HTML5 spec (one <main> per
    // document) and surfaced as duplicate "main" landmarks in
    // assistive technologies.
    <section
      aria-labelledby="connection-feed-heading"
      className="mx-auto flex max-w-7xl flex-col gap-4 p-4 sm:p-6"
      data-testid="connection-feed"
    >
      <header className="flex flex-col items-start justify-between gap-3 sm:flex-row sm:items-center">
        <div>
          <h1
            id="connection-feed-heading"
            className="text-2xl font-semibold tracking-tight text-slate-900"
          >
            Connections
          </h1>
          <p className="mt-1 text-sm text-slate-600">
            Browse and act on every connection idea logged by the team.
          </p>
        </div>
        <Link
          to="/connections/new"
          className="self-stretch sm:self-auto"
          data-testid="connection-feed-add-connection-link"
        >
          <Button
            type="button"
            variant="primary"
            size="md"
            leftIcon={<Plus aria-hidden="true" />}
            fullWidth
            data-testid="connection-feed-add-connection"
          >
            Add Connection
          </Button>
        </Link>
      </header>

      <FilterBar searchParams={searchParams} onParamsChange={handleParamsChange} />

      {connectionsQuery.isError && (
        <div
          role="alert"
          className="flex flex-col gap-2 rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-800"
          data-testid="connection-feed-error"
        >
          <p className="font-semibold">Could not load connections.</p>
          <p>{connectionsQuery.error?.message ?? "Unknown error"}</p>
          <div>
            <Button
              type="button"
              variant="secondary"
              size="sm"
              onClick={() => connectionsQuery.refetch()}
              data-testid="connection-feed-error-retry"
            >
              Retry
            </Button>
          </div>
        </div>
      )}

      <Table<ConnectionRead>
        columns={columns}
        data={items}
        rowKey={(row) => row.id}
        isLoading={connectionsQuery.isPending && !connectionsQuery.isError}
        onRowClick={handleRowClick}
        sortKey={sortState.key}
        sortDirection={sortState.direction}
        onSortChange={handleSortChange}
        caption="Connection records sorted and filtered by the controls above."
        emptyState={
          <p role="status" className="text-sm text-slate-500" data-testid="connection-feed-empty">
            No connections match the current filters. Try clearing filters or adding the first
            connection.
          </p>
        }
      />

      {total > 0 && (
        <PaginationBar
          page={page}
          pageSize={pageSize}
          total={total}
          isFetching={connectionsQuery.isFetching}
          onPageChange={handlePageChange}
          onPageSizeChange={handlePageSizeChange}
        />
      )}
    </section>
  );
}
