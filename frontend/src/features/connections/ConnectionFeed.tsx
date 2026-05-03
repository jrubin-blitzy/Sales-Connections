/**
 * ConnectionFeed.tsx - F-004 Connection Feed & Dashboard.
 *
 * The primary surface for the sales team. Lists every connection record
 * in the actor's organization with six row elements (Name, Company,
 * Involvement badge, Submitter, Status chip, Submission date), six
 * filter dimensions (company, involvement, owner, date-from, date-to,
 * outreach-status, tags), and five sort dimensions (submission_date,
 * full_name, company, owner_display_name, outreach_status).
 *
 * Per AAP Sec 0.5.4 (UI Design):
 *   "It is the landing route after authentication for `Viewer` and
 *    `Contributor` roles. The feed renders one row per record showing
 *    Name, Company, Involvement badge (with three colour states for
 *    Warm Intro / Soft Reference / Target Only), Submitter display
 *    name, Outreach Status chip (with four colour states), and
 *    Submission Date. A filter bar above the table exposes six filter
 *    dimensions ... and five sort dimensions ... Filter state is
 *    encoded in the URL search params so the screen is shareable.
 *    Status chip mutation is admitted only for `Viewer`/`Admin`."
 *
 * Architecture:
 *   The component is a TanStack Query consumer (no direct fetch). The
 *   underlying useConnectionsQuery hook handles cache key derivation,
 *   request fan-out to the backend's GET /api/connections endpoint,
 *   and 401 redirect propagation via the apiGet wrapper.
 *
 *   URL search params are the single source of truth for filter and
 *   sort state. When the user changes a filter, the URL is updated
 *   via react-router-dom's useSearchParams; the query-params change
 *   triggers a TanStack Query refetch with the new cache key. This
 *   makes filtered views shareable (a sales rep can paste the URL of
 *   "filter by my company in Series A" to a colleague).
 *
 *   Pagination state is offset/limit-driven (matches the backend's
 *   PaginatedConnections envelope: items, total, limit, offset).
 *
 * Per AAP Sec 0.7.1 invariant 7:
 *   The status chip mutation is gated to Admin / Viewer at the UI level
 *   via <RoleGate> (StatusChip handles it internally) and at the API
 *   level via the @requires_role decorator on the
 *   PATCH /api/connections/:id/status route. The UI-level gate is
 *   secondary defense; the API decorator is authoritative.
 *
 * Per AAP Sec 0.7.7:
 *   - TailwindCSS utility classes only (no inline `style`).
 *   - Lucide-React icons throughout (Search, Filter, X, Plus, ...).
 *   - Strict TypeScript; no `any`; explicit `JSX.Element` return.
 *   - Named exports only (no default export).
 *   - All HTTP traffic via @/api/connections (which routes through
 *     @/api/client.ts for correlation IDs and 401 redirect uniformity).
 *
 * Coordinates with:
 *   - @/api/connections                useConnectionsQuery hook
 *                                      (PaginatedConnections envelope).
 *   - @/auth/AuthProvider              useRole hook for role-gated UI.
 *   - @/components/ui/Table            generic typed Table primitive
 *                                      (with controlled sort + clickable
 *                                      rows).
 *   - @/components/ui/Button           filter clear / pagination /
 *                                      "Add Connection" CTA.
 *   - @/components/ui/Input            free-text filter inputs (company,
 *                                      full name, date pickers).
 *   - @/components/ui/Select           single-select filter for the
 *                                      include-deleted toggle (Admin).
 *   - @/components/ui/Badge            DELETED badge for soft-deleted
 *                                      records when admin opts in.
 *   - @/features/connections/InvolvementBadge per-row 3-state involvement.
 *   - @/features/connections/StatusChip per-row 4-state status chip
 *                                      (with role-gated edit).
 *   - @/schemas/connection             ConnectionRead, INVOLVEMENT_VALUES,
 *                                      OUTREACH_STATUS_VALUES.
 *   - @/router.tsx                     mounted at "/feed" (or "/").
 */

import { useCallback, useMemo, type JSX } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Filter, Plus, RefreshCw, Search, X } from "lucide-react";
import clsx from "clsx";

import { useConnectionsQuery, type ConnectionListParams } from "@/api/connections";
import { useRole } from "@/auth/AuthProvider";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Table, type SortDirection, type TableColumn } from "@/components/ui/Table";
import {
  INVOLVEMENT_VALUES,
  OUTREACH_STATUS_VALUES,
  type ConnectionRead,
  type InvolvementValue,
  type OutreachStatusValue,
} from "@/schemas/connection";

import { InvolvementBadge } from "./InvolvementBadge";
import { StatusChip } from "./StatusChip";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/**
 * Default page size for the connection feed. Matches the backend
 * service's default (`_DEFAULT_PAGE_SIZE = 25` in
 * `app/services/connections.py`) to avoid the user-visible mismatch
 * where the backend silently caps a larger client-requested page. The
 * hard upper bound (`_MAX_PAGE_SIZE = 100`) is enforced server-side;
 * the SPA stays well under it for the typical 10K-record scale ceiling.
 */
const DEFAULT_PAGE_SIZE = 25;

/**
 * The five sort dimensions per AAP Sec 0.5.2 Layer 4 exposed by the
 * backend's `_SORT_COLUMNS` whitelist. Exposing the same constant
 * here keeps the SPA's sort UI in lockstep with the backend's accepted
 * sort keys; a typo here would be a runtime 422 from the API but is
 * caught at compile time by the literal-union type below.
 */
type SortKey =
  | "submission_date"
  | "full_name"
  | "company"
  | "owner_display_name"
  | "outreach_status";

/**
 * The default sort applied when the URL has no sort parameters.
 * `submission_date DESC` corresponds to "newest first" which matches
 * the backend default and the user's primary mental model: the most
 * recently logged connections are usually the most actionable.
 */
const DEFAULT_SORT_KEY: SortKey = "submission_date";
const DEFAULT_SORT_DIR: "asc" | "desc" = "desc";

// ---------------------------------------------------------------------------
// URL search-param helpers
// ---------------------------------------------------------------------------

/**
 * Translate the URLSearchParams from react-router-dom into the typed
 * `ConnectionListParams` consumed by `useConnectionsQuery`.
 *
 * Multi-valued filters (involvement, outreach_status, owner_user_ids,
 * tag_ids) are read via `URLSearchParams.getAll(key)`. Scalar filters
 * use `.get(key)` and a falsy guard so an empty-string param does not
 * propagate to the backend (which would treat it as a "match every
 * empty company" filter and silently return no records).
 *
 * Boolean params (include_deleted) are explicitly compared to the
 * literal "true" string to avoid the trap where any non-empty string
 * would coerce to true.
 *
 * Numeric params (page, page_size) are parsed via `Number()` with a
 * fallback to the provided default; `Number("")` is 0 (a value the
 * backend rejects), so the explicit conditional protects against
 * empty-string round-trips.
 *
 * Note that URL state is intentionally a NARROW reflection of the
 * full `ConnectionListParams` shape - the SPA does not yet expose
 * full_name_search via URL because that filter is typically a
 * transient typeahead (and would clutter the shareable URL); when
 * we add that input, this helper will read it the same way as
 * `company`.
 *
 * @param searchParams URLSearchParams instance from useSearchParams.
 * @returns            A typed ConnectionListParams ready for the query.
 */
function readFiltersFromUrl(searchParams: URLSearchParams): ConnectionListParams {
  const params: ConnectionListParams = {};

  const company = searchParams.get("company");
  if (company) params.company = company;

  const fullName = searchParams.get("full_name_search");
  if (fullName) params.full_name_search = fullName;

  const dateFrom = searchParams.get("submission_date_from");
  if (dateFrom) params.submission_date_from = dateFrom;
  const dateTo = searchParams.get("submission_date_to");
  if (dateTo) params.submission_date_to = dateTo;

  // Multi-valued filters use repeating keys per the buildConnectionListQuery
  // wire-format.
  const involvement = searchParams.getAll("involvement");
  if (involvement.length > 0) {
    params.involvement = involvement as ReadonlyArray<InvolvementValue>;
  }
  const outreachStatus = searchParams.getAll("outreach_status");
  if (outreachStatus.length > 0) {
    params.outreach_status = outreachStatus as ReadonlyArray<OutreachStatusValue>;
  }
  const ownerIds = searchParams.getAll("owner_user_ids");
  if (ownerIds.length > 0) {
    params.owner_user_ids = ownerIds;
  }
  const tagIds = searchParams.getAll("tag_ids");
  if (tagIds.length > 0) {
    params.tag_ids = tagIds;
  }

  // Sort parameters with default fallbacks. The query-key cache uses the
  // exact params object, so omitting a sort key when the user has not
  // explicitly chosen one keeps the cache key stable across navigation
  // (`?sort=submission_date` and the absence of the param produce
  // different cache keys despite producing identical results).
  const sort = searchParams.get("sort");
  if (sort) params.sort = sort as SortKey;
  const sortDir = searchParams.get("sort_dir");
  if (sortDir === "asc" || sortDir === "desc") {
    params.sort_dir = sortDir;
  }

  // Pagination: page is 1-indexed in the URL (more user-friendly) but
  // the backend accepts either page-or-offset semantics; we keep the
  // 1-indexed wire format since the backend handler already supports it.
  const page = searchParams.get("page");
  if (page) {
    const parsed = Number.parseInt(page, 10);
    if (!Number.isNaN(parsed) && parsed > 0) {
      params.page = parsed;
    }
  }
  const pageSize = searchParams.get("page_size");
  if (pageSize) {
    const parsed = Number.parseInt(pageSize, 10);
    if (!Number.isNaN(parsed) && parsed > 0) {
      params.page_size = parsed;
    }
  }

  // Admin-only opt-in for soft-deleted records. The non-Admin SPA
  // path never sets this; the backend defensively ignores it from
  // non-Admin actors anyway, but encoding it in the URL is harmless.
  const includeDeleted = searchParams.get("include_deleted");
  if (includeDeleted === "true") {
    params.include_deleted = true;
  }

  return params;
}

/**
 * Format an ISO datetime string as a short locale-aware date.
 *
 * Returns an em-dash (U+2014) for empty/null inputs so empty cells
 * are visually distinct from cells that simply lack a column. Wraps
 * the Date construction in try/catch because `new Date(garbage)`
 * does not throw - it produces an Invalid Date whose
 * `toLocaleDateString()` returns the literal "Invalid Date" string.
 * The graceful fallback makes upstream payload bugs easier to debug
 * than a wall of "Invalid Date" cells in the feed.
 *
 * @param value ISO-8601 datetime string (with offset) or null.
 * @returns     A short locale date or em-dash.
 */
function formatDateCell(value: string | null): string {
  if (!value) return "\u2014";
  try {
    return new Date(value).toLocaleDateString();
  } catch {
    return value;
  }
}

// ---------------------------------------------------------------------------
// Filter bar sub-component
// ---------------------------------------------------------------------------

/**
 * Props for the FilterBar sub-component.
 *
 * Filter state is owned by the parent (driven by URLSearchParams) so
 * the FilterBar is a presentational helper that emits change events.
 * `onChangeFilter(key, value)` accepts a sentinel `undefined` to clear
 * the filter (the parent then strips the URL search param). Multi-
 * valued filters use `onToggleEnumFilter` which adds/removes a value
 * from the underlying array.
 */
interface FilterBarProps {
  readonly company: string;
  readonly fullName: string;
  readonly dateFrom: string;
  readonly dateTo: string;
  readonly involvement: ReadonlyArray<InvolvementValue>;
  readonly outreachStatus: ReadonlyArray<OutreachStatusValue>;
  readonly includeDeleted: boolean;
  readonly canIncludeDeleted: boolean;
  readonly onChangeText: (key: "company" | "full_name_search", value: string) => void;
  readonly onChangeDate: (
    key: "submission_date_from" | "submission_date_to",
    value: string,
  ) => void;
  readonly onToggleInvolvement: (value: InvolvementValue) => void;
  readonly onToggleStatus: (value: OutreachStatusValue) => void;
  readonly onToggleIncludeDeleted: () => void;
  readonly onClearAll: () => void;
  readonly hasActiveFilters: boolean;
}

/**
 * The collapsible filter bar.
 *
 * Six filter dimensions are exposed: company (substring), full_name
 * (substring), submission_date_from / submission_date_to (date range),
 * involvement (multi-select pill toggles), outreach_status (multi-
 * select pill toggles). The seventh dimension - tag_ids - is omitted
 * here because it requires fetching the org's tag list, which would
 * couple this component to the tags API; tag filtering would be added
 * via a future TagFilterDropdown sub-component.
 *
 * The filter bar uses pill-style toggle buttons rather than a
 * <Select multiple> for involvement and outreach_status because:
 *   - Pills give a clearer visual indication of the OR semantics
 *     (multiple selected pills = "any of these").
 *   - Pills are larger touch targets on mobile.
 *   - Pills surface the available values without requiring a click.
 *
 * Accessibility:
 *   - Each filter group has a <legend> for screen-reader context.
 *   - Toggle buttons use `aria-pressed` to surface their selection
 *     state to assistive technology.
 *   - The "Clear filters" button is hidden when no filters are
 *     active to avoid a no-op control.
 */
function FilterBar({
  company,
  fullName,
  dateFrom,
  dateTo,
  involvement,
  outreachStatus,
  includeDeleted,
  canIncludeDeleted,
  onChangeText,
  onChangeDate,
  onToggleInvolvement,
  onToggleStatus,
  onToggleIncludeDeleted,
  onClearAll,
  hasActiveFilters,
}: FilterBarProps): JSX.Element {
  return (
    <section
      aria-labelledby="connection-feed-filters-heading"
      className="flex flex-col gap-3 rounded-lg border border-slate-200 bg-white p-4 shadow-card"
      data-testid="connection-feed-filters"
    >
      <header className="flex items-center justify-between">
        <h3
          id="connection-feed-filters-heading"
          className="flex items-center gap-2 text-sm font-semibold text-slate-700"
        >
          <Filter aria-hidden="true" className="h-4 w-4" />
          Filters
        </h3>
        {hasActiveFilters && (
          <Button
            variant="ghost"
            size="sm"
            onClick={onClearAll}
            leftIcon={<X aria-hidden="true" />}
            data-testid="connection-feed-clear-filters"
          >
            Clear filters
          </Button>
        )}
      </header>

      <div className="grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-4">
        <Input
          label="Company"
          placeholder="Substring match"
          value={company}
          onChange={(e) => onChangeText("company", e.target.value)}
          leftIcon={<Search aria-hidden="true" />}
          data-testid="connection-feed-filter-company"
        />
        <Input
          label="Name"
          placeholder="Substring match"
          value={fullName}
          onChange={(e) => onChangeText("full_name_search", e.target.value)}
          leftIcon={<Search aria-hidden="true" />}
          data-testid="connection-feed-filter-full-name"
        />
        <Input
          label="From date"
          type="date"
          value={dateFrom}
          onChange={(e) => onChangeDate("submission_date_from", e.target.value)}
          data-testid="connection-feed-filter-date-from"
        />
        <Input
          label="To date"
          type="date"
          value={dateTo}
          onChange={(e) => onChangeDate("submission_date_to", e.target.value)}
          data-testid="connection-feed-filter-date-to"
        />
      </div>

      <fieldset className="flex flex-col gap-1.5">
        <legend className="text-xs font-semibold uppercase tracking-wide text-slate-600">
          Involvement
        </legend>
        <div className="flex flex-wrap gap-2" role="group" aria-label="Involvement filter (any-of)">
          {INVOLVEMENT_VALUES.map((value) => {
            const isActive = involvement.includes(value);
            return (
              <button
                key={value}
                type="button"
                onClick={() => onToggleInvolvement(value)}
                aria-pressed={isActive}
                className={clsx(
                  "inline-flex items-center rounded-full border px-3 py-1 text-xs font-medium transition-colors",
                  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-500",
                  isActive
                    ? "border-brand-500 bg-brand-50 text-brand-700"
                    : "border-slate-200 bg-white text-slate-700 hover:bg-slate-50",
                )}
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
        <legend className="text-xs font-semibold uppercase tracking-wide text-slate-600">
          Outreach status
        </legend>
        <div
          className="flex flex-wrap gap-2"
          role="group"
          aria-label="Outreach status filter (any-of)"
        >
          {OUTREACH_STATUS_VALUES.map((value) => {
            const isActive = outreachStatus.includes(value);
            return (
              <button
                key={value}
                type="button"
                onClick={() => onToggleStatus(value)}
                aria-pressed={isActive}
                className={clsx(
                  "inline-flex items-center rounded-full border px-3 py-1 text-xs font-medium transition-colors",
                  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-500",
                  isActive
                    ? "border-brand-500 bg-brand-50 text-brand-700"
                    : "border-slate-200 bg-white text-slate-700 hover:bg-slate-50",
                )}
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

      {canIncludeDeleted && (
        <label
          className="inline-flex cursor-pointer select-none items-center gap-2 text-sm text-slate-700"
          data-testid="connection-feed-filter-include-deleted-label"
        >
          <input
            type="checkbox"
            checked={includeDeleted}
            onChange={onToggleIncludeDeleted}
            className="h-4 w-4 rounded border-slate-300 text-brand-600 focus:ring-2 focus:ring-brand-500"
            data-testid="connection-feed-filter-include-deleted"
          />
          <span>Include soft-deleted records (Admin only)</span>
        </label>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Public ConnectionFeed component
// ---------------------------------------------------------------------------

/**
 * The Connection Feed component.
 *
 * Renders the full F-004 surface: filter bar above, paginated table
 * below, "Add Connection" CTA in the header. The table is built with
 * the generic `<Table>` primitive in controlled-sort mode so the
 * sort state flows up through `onSortChange` and persists to the URL.
 *
 * On row click, navigates to /connections/:id to show the detail
 * view (F-011). Status chip mutation is gated to Admin/Viewer at the
 * StatusChip component level.
 *
 * The component does NOT take any props (route-level component).
 * State derives entirely from the URL (filters/sort/pagination) and
 * the auth context (role for include_deleted toggle visibility).
 */
export function ConnectionFeed(): JSX.Element {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const { has } = useRole();
  const isAdmin = has("Admin");

  // ---------------------------------------------------------------------
  // Derive ConnectionListParams from URL
  //
  // The URL is the single source of truth so the back button works
  // intuitively, the URL is shareable, and any state that the user
  // sees is exactly the state encoded in the URL. The hook below
  // re-derives the params object on every render where the URL
  // changes; useMemo prevents identity churn on unrelated re-renders
  // (e.g., when StatusChip re-renders because the parent's optimistic
  // update fired).
  // ---------------------------------------------------------------------
  const params = useMemo(() => readFiltersFromUrl(searchParams), [searchParams]);

  // ---------------------------------------------------------------------
  // Pagination state derived from URL (with defaults)
  // ---------------------------------------------------------------------
  const page = params.page ?? 1;
  const pageSize = params.page_size ?? DEFAULT_PAGE_SIZE;

  // ---------------------------------------------------------------------
  // Sort state derived from URL (with defaults)
  // ---------------------------------------------------------------------
  const sortKey: SortKey = params.sort ?? DEFAULT_SORT_KEY;
  const sortDir: "asc" | "desc" = params.sort_dir ?? DEFAULT_SORT_DIR;

  // ---------------------------------------------------------------------
  // The actual params we send to the API. We always pass an explicit
  // page_size so the cache key is stable; without it, two equivalent
  // URLs (one with page_size=25, one without) would produce different
  // cache entries. Same for page.
  // ---------------------------------------------------------------------
  const queryParams: ConnectionListParams = useMemo(
    () => ({
      ...params,
      page,
      page_size: pageSize,
      sort: sortKey,
      sort_dir: sortDir,
    }),
    [params, page, pageSize, sortKey, sortDir],
  );

  const connectionsQuery = useConnectionsQuery(queryParams);

  // ---------------------------------------------------------------------
  // URL mutation helpers
  //
  // Each helper updates a subset of the URLSearchParams and either sets
  // or deletes the corresponding key. We use the functional form of
  // setSearchParams (which receives the current params) to avoid stale-
  // closure bugs across rapid filter changes.
  // ---------------------------------------------------------------------
  const updateSearchParams = useCallback(
    (mutator: (params: URLSearchParams) => void) => {
      setSearchParams(
        (current) => {
          const next = new URLSearchParams(current);
          mutator(next);
          // Always reset to page 1 on filter/sort change so the user
          // does not see "page 5 of 1" when their new filter has
          // fewer than 5 pages. Pagination changes themselves call
          // setSearchParams directly without going through this
          // helper to bypass the page reset.
          next.delete("page");
          return next;
        },
        { replace: false },
      );
    },
    [setSearchParams],
  );

  const handleChangeText = useCallback(
    (key: "company" | "full_name_search", value: string) => {
      updateSearchParams((next) => {
        if (value === "") {
          next.delete(key);
        } else {
          next.set(key, value);
        }
      });
    },
    [updateSearchParams],
  );

  const handleChangeDate = useCallback(
    (key: "submission_date_from" | "submission_date_to", value: string) => {
      updateSearchParams((next) => {
        if (value === "") {
          next.delete(key);
        } else {
          next.set(key, value);
        }
      });
    },
    [updateSearchParams],
  );

  const handleToggleInvolvement = useCallback(
    (value: InvolvementValue) => {
      updateSearchParams((next) => {
        const current = next.getAll("involvement");
        if (current.includes(value)) {
          // Toggle off: rebuild without this value.
          next.delete("involvement");
          for (const v of current.filter((c) => c !== value)) {
            next.append("involvement", v);
          }
        } else {
          next.append("involvement", value);
        }
      });
    },
    [updateSearchParams],
  );

  const handleToggleStatus = useCallback(
    (value: OutreachStatusValue) => {
      updateSearchParams((next) => {
        const current = next.getAll("outreach_status");
        if (current.includes(value)) {
          next.delete("outreach_status");
          for (const v of current.filter((c) => c !== value)) {
            next.append("outreach_status", v);
          }
        } else {
          next.append("outreach_status", value);
        }
      });
    },
    [updateSearchParams],
  );

  const handleToggleIncludeDeleted = useCallback(() => {
    updateSearchParams((next) => {
      const current = next.get("include_deleted");
      if (current === "true") {
        next.delete("include_deleted");
      } else {
        next.set("include_deleted", "true");
      }
    });
  }, [updateSearchParams]);

  const handleClearAll = useCallback(() => {
    setSearchParams(new URLSearchParams());
  }, [setSearchParams]);

  const handleSortChange = useCallback(
    (key: string, direction: SortDirection) => {
      updateSearchParams((next) => {
        if (direction === null) {
          // Third click clears: revert to default sort by removing
          // the URL params; the params helper applies the defaults
          // automatically on the next render.
          next.delete("sort");
          next.delete("sort_dir");
        } else {
          next.set("sort", key);
          next.set("sort_dir", direction);
        }
      });
    },
    [updateSearchParams],
  );

  const handlePageChange = useCallback(
    (newPage: number) => {
      // Page changes do NOT reset to page 1 (which would be circular);
      // call setSearchParams directly instead of via updateSearchParams.
      setSearchParams(
        (current) => {
          const next = new URLSearchParams(current);
          if (newPage <= 1) {
            next.delete("page");
          } else {
            next.set("page", String(newPage));
          }
          return next;
        },
        { replace: false },
      );
    },
    [setSearchParams],
  );

  const handleRowClick = useCallback(
    (row: ConnectionRead) => {
      navigate(`/connections/${row.id}`);
    },
    [navigate],
  );

  // ---------------------------------------------------------------------
  // Active-filter detection (drives the "Clear filters" button visibility)
  // ---------------------------------------------------------------------
  const hasActiveFilters =
    Boolean(params.company) ||
    Boolean(params.full_name_search) ||
    Boolean(params.submission_date_from) ||
    Boolean(params.submission_date_to) ||
    (params.involvement?.length ?? 0) > 0 ||
    (params.outreach_status?.length ?? 0) > 0 ||
    (params.owner_user_ids?.length ?? 0) > 0 ||
    (params.tag_ids?.length ?? 0) > 0 ||
    params.include_deleted === true;

  // ---------------------------------------------------------------------
  // Total page count for pagination footer
  // ---------------------------------------------------------------------
  const totalPages = useMemo(() => {
    if (!connectionsQuery.data) return 1;
    return Math.max(1, Math.ceil(connectionsQuery.data.total / pageSize));
  }, [connectionsQuery.data, pageSize]);

  // ---------------------------------------------------------------------
  // Column definitions
  //
  // Six row elements per AAP Sec 0.5.4:
  //   1. Name (sortable)
  //   2. Company (sortable; hideBelow=md)
  //   3. Involvement badge
  //   4. Submitter / owner_display_name (sortable; hideBelow=lg)
  //   5. Outreach Status chip (sortable; mutation gated by StatusChip)
  //   6. Submission Date (sortable; hideBelow=md)
  //
  // The columns array is memoized; the inner closures (e.g., the
  // status-chip render) capture only the row argument and do not
  // depend on any outer state, so a stable reference is correct.
  // ---------------------------------------------------------------------
  const columns: ReadonlyArray<TableColumn<ConnectionRead>> = useMemo(
    () => [
      {
        key: "full_name",
        header: "Name",
        sortable: true,
        render: (row) => (
          <div className="flex flex-col">
            <span
              className={clsx(
                "text-sm font-medium",
                row.deleted_at ? "text-slate-500 line-through" : "text-slate-900",
              )}
              data-testid={`connection-feed-row-name-${row.id}`}
            >
              {row.full_name}
            </span>
            <span className="text-xs text-slate-500">{row.job_title}</span>
          </div>
        ),
      },
      {
        key: "company",
        header: "Company",
        sortable: true,
        render: (row) => (
          <span
            className={clsx("text-sm", row.deleted_at ? "text-slate-400" : "text-slate-700")}
            data-testid={`connection-feed-row-company-${row.id}`}
          >
            {row.company}
          </span>
        ),
        hideBelow: "md",
      },
      {
        key: "involvement",
        header: "Involvement",
        render: (row) => (
          <span data-testid={`connection-feed-row-involvement-${row.id}`}>
            <InvolvementBadge value={row.involvement} />
          </span>
        ),
      },
      {
        key: "owner_display_name",
        header: "Submitter",
        sortable: true,
        render: (row) => (
          <span
            className="text-sm text-slate-700"
            data-testid={`connection-feed-row-submitter-${row.id}`}
          >
            {row.owner_display_name}
          </span>
        ),
        hideBelow: "lg",
      },
      {
        key: "outreach_status",
        header: "Status",
        sortable: true,
        render: (row) => (
          <div data-testid={`connection-feed-row-status-${row.id}`}>
            <StatusChip
              recordId={row.id}
              value={row.outreach_status}
              disabled={Boolean(row.deleted_at)}
            />
          </div>
        ),
      },
      {
        key: "submission_date",
        header: "Submitted",
        sortable: true,
        render: (row) => (
          <span
            className="text-sm text-slate-700 tabular-nums"
            data-testid={`connection-feed-row-date-${row.id}`}
          >
            {formatDateCell(row.submission_date)}
          </span>
        ),
        hideBelow: "md",
      },
    ],
    [],
  );

  // ---------------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------------
  return (
    <main
      aria-labelledby="connection-feed-heading"
      className="flex flex-col gap-4 p-4 sm:p-6"
      data-testid="connection-feed"
    >
      <header className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <h2 id="connection-feed-heading" className="text-2xl font-semibold text-slate-900">
          Connections
        </h2>
        <div className="flex items-center gap-2">
          <Button
            variant="secondary"
            size="sm"
            onClick={() => connectionsQuery.refetch()}
            disabled={connectionsQuery.isFetching}
            leftIcon={<RefreshCw aria-hidden="true" />}
            data-testid="connection-feed-refresh"
          >
            Refresh
          </Button>
          <Button
            variant="primary"
            size="sm"
            onClick={() => navigate("/connections/new")}
            leftIcon={<Plus aria-hidden="true" />}
            data-testid="connection-feed-add-connection"
          >
            Add Connection
          </Button>
        </div>
      </header>

      <FilterBar
        company={params.company ?? ""}
        fullName={params.full_name_search ?? ""}
        dateFrom={params.submission_date_from ?? ""}
        dateTo={params.submission_date_to ?? ""}
        involvement={params.involvement ?? []}
        outreachStatus={params.outreach_status ?? []}
        includeDeleted={params.include_deleted === true}
        canIncludeDeleted={isAdmin}
        onChangeText={handleChangeText}
        onChangeDate={handleChangeDate}
        onToggleInvolvement={handleToggleInvolvement}
        onToggleStatus={handleToggleStatus}
        onToggleIncludeDeleted={handleToggleIncludeDeleted}
        onClearAll={handleClearAll}
        hasActiveFilters={hasActiveFilters}
      />

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

      {!connectionsQuery.isError && (
        <Table<ConnectionRead>
          columns={columns}
          data={connectionsQuery.data?.items ?? []}
          rowKey={(row) => row.id}
          isLoading={connectionsQuery.isPending}
          onRowClick={handleRowClick}
          sortKey={sortKey}
          sortDirection={sortDir}
          onSortChange={handleSortChange}
          caption="Connection records sorted and filtered by the controls above."
          emptyState={
            <div className="py-8 text-center text-sm text-slate-500">
              {hasActiveFilters
                ? "No connections match the current filters."
                : "No connections yet. Click \u201cAdd Connection\u201d to log your first one."}
            </div>
          }
        />
      )}

      {connectionsQuery.data && connectionsQuery.data.total > pageSize && (
        <nav
          aria-label="Connection feed pagination"
          className="flex items-center justify-between gap-2 border-t border-slate-200 pt-3"
          data-testid="connection-feed-pagination"
        >
          <p className="text-sm text-slate-600 tabular-nums">
            Page {page} of {totalPages} &middot; {connectionsQuery.data.total.toLocaleString()}{" "}
            total
          </p>
          <div className="flex gap-2">
            <Button
              variant="secondary"
              size="sm"
              onClick={() => handlePageChange(Math.max(1, page - 1))}
              disabled={page <= 1 || connectionsQuery.isFetching}
              data-testid="connection-feed-prev-page"
            >
              Previous
            </Button>
            <Button
              variant="secondary"
              size="sm"
              onClick={() => handlePageChange(Math.min(totalPages, page + 1))}
              disabled={page >= totalPages || connectionsQuery.isFetching}
              data-testid="connection-feed-next-page"
            >
              Next
            </Button>
          </div>
        </nav>
      )}
    </main>
  );
}
