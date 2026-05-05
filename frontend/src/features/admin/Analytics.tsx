/**
 * Analytics.tsx - F-014 Admin Analytics view.
 *
 * Mounted at /admin/analytics as a child of the AdminPanel layout. Pure
 * read-only view: no mutations, no editable state, just three aggregation
 * panels rendered from a single GET /api/admin/analytics call.
 *
 * Three panels (per AAP Sec 0.5.4 Screen 4):
 *   1. Most Active Contributors - top contributors by record count.
 *   2. Leads by Status - counts for each of the four OutreachStatus values
 *      with colored badges matching the rest of the SPA.
 *   3. Weekly Activity - simple sparkline of records-per-week for the
 *      last N weeks (server determines N; SPA renders whatever is returned).
 *
 * Loading: while the analytics query is pending, render skeleton panels.
 * Error: render an error message with a retry button (calls refetch()).
 * Empty: each panel handles its own empty state (e.g., "No contributors yet").
 *
 * Auth: this component is rendered ONLY inside <RoleGate role="Admin"> per
 * the route configuration in @/router.tsx. The backend's @requires_role(Admin)
 * decorator on /api/admin/analytics is the authoritative gate per AAP Sec 0.7.1
 * invariant 7.
 *
 * Data resilience:
 *   - The Leads-by-Status panel re-orders the response to canonical
 *     OUTREACH_STATUS_VALUES order client-side and defaults missing
 *     entries to count=0, so the four-status pipeline view always shows
 *     four rows regardless of backend response ordering or omissions.
 *   - The Weekly Activity sparkline gracefully handles 0, 1, or N
 *     weeks of data without rendering an invalid SVG path.
 *
 * Per AAP Sec 0.7.7:
 *   - TailwindCSS utility classes only (no inline styles)
 *   - Strict TypeScript; no `any` types
 *   - Lucide-React icons throughout
 *   - Semantic HTML (main, section, header, h2, h3, ul, li)
 *   - No direct fetch (uses useAnalyticsQuery from @/api/admin)
 *
 * Per project Prettier configuration (singleQuote: false, trailingComma:
 * "all", printWidth: 100): double quotes throughout, trailing commas in
 * multi-line collections, lines under 100 characters.
 *
 * Coordinates with:
 *   - @/api/admin                useAnalyticsQuery (5-minute staleTime)
 *   - @/components/ui/Badge      Outreach status pill primitive
 *   - @/components/ui/Button     Retry button for ErrorState
 *   - @/components/ui/Table      Generic typed Table primitive (contributors)
 *   - @/schemas/admin            AnalyticsResponse and panel row types
 *   - @/schemas/connection       OUTREACH_STATUS_VALUES (canonical order)
 *   - @/router.tsx               Mounted at /admin/analytics (Admin-only)
 *   - @/features/admin/AdminPanel.tsx - parent layout shell (BarChart3
 *     tab anchors here)
 */

import type { JSX } from "react";
import { Activity, AlertCircle, BarChart3, RefreshCw, Trophy, Users } from "lucide-react";
import clsx from "clsx";

import { useAnalyticsQuery } from "@/api/admin";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Table, type TableColumn } from "@/components/ui/Table";
import type {
  AnalyticsResponse,
  ContributorActivity,
  LeadsByStatusEntry,
  WeeklyActivityEntry,
} from "@/schemas/admin";
import { OUTREACH_STATUS_VALUES } from "@/schemas/connection";

// ---------------------------------------------------------------------------
// Helper: Map OutreachStatus -> Badge variant
// ---------------------------------------------------------------------------

/**
 * Map an OutreachStatus value to the corresponding Badge variant defined
 * in @/components/ui/Badge. The naming convention is:
 *   "Not Started" -> "outreach-not-started"
 *   "In Progress" -> "outreach-in-progress"
 *   "Contacted"   -> "outreach-contacted"
 *   "Closed"      -> "outreach-closed"
 *
 * This mapping mirrors the conventions used by StatusChip.tsx and
 * RecordModeration.tsx (sibling files) so the same visual treatment
 * for outreach status is consistent across the SPA.
 *
 * The exhaustive switch over the four-value tuple gives TypeScript the
 * confidence to infer an `outreach-*` literal-union return type without
 * a `default` branch.
 */
function statusToBadgeVariant(
  status: (typeof OUTREACH_STATUS_VALUES)[number],
): "outreach-not-started" | "outreach-in-progress" | "outreach-contacted" | "outreach-closed" {
  switch (status) {
    case "Not Started":
      return "outreach-not-started";
    case "In Progress":
      return "outreach-in-progress";
    case "Contacted":
      return "outreach-contacted";
    case "Closed":
      return "outreach-closed";
  }
}

// ---------------------------------------------------------------------------
// Sub-component: MostActiveContributorsPanel
// ---------------------------------------------------------------------------

/**
 * Top contributors leaderboard. Renders the array of ContributorActivity
 * as a small Table. When empty, shows a friendly placeholder.
 *
 * The Table columns are declared inside the component (rather than at
 * module scope) so the typed render callbacks can reference React 19's
 * new transform without a separate file scope. The `widthClassName`
 * tokens keep the rank and record-count columns narrow so the
 * contributor name column gets the visual breathing room.
 *
 * The list is ALREADY ordered by the backend (`record_count DESC`,
 * capped at 50 rows) so we do not re-sort here; the rank index simply
 * follows the array position.
 */
function MostActiveContributorsPanel({
  contributors,
}: {
  readonly contributors: ReadonlyArray<ContributorActivity>;
}): JSX.Element {
  const columns: ReadonlyArray<TableColumn<ContributorActivity>> = [
    {
      key: "rank",
      header: "#",
      render: (_row, index) => (
        <span className="font-mono text-sm text-slate-500 tabular-nums">{index + 1}</span>
      ),
      widthClassName: "w-12",
    },
    {
      key: "display_name",
      header: "Contributor",
      render: (row) => (
        <span className="text-sm font-medium text-slate-900">{row.display_name}</span>
      ),
    },
    {
      key: "record_count",
      header: "Records",
      align: "right",
      render: (row) => (
        <span className="font-mono text-sm font-semibold tabular-nums text-slate-900">
          {row.record_count.toLocaleString()}
        </span>
      ),
      widthClassName: "w-32",
    },
  ];

  return (
    <section
      aria-labelledby="analytics-contributors-heading"
      className="flex flex-col gap-3"
      data-testid="analytics-contributors-panel"
    >
      <header className="flex items-center gap-2">
        <Trophy aria-hidden="true" className="h-5 w-5 text-amber-500" />
        <h3 id="analytics-contributors-heading" className="text-base font-semibold text-slate-900">
          Most Active Contributors
        </h3>
      </header>
      {contributors.length === 0 ? (
        <div
          className="rounded-lg border border-slate-200 bg-white p-6 text-center text-sm text-slate-500"
          data-testid="analytics-contributors-empty"
        >
          No contributors yet. Start adding connections to populate this leaderboard.
        </div>
      ) : (
        <Table<ContributorActivity>
          columns={columns}
          data={contributors}
          rowKey={(row) => row.user_id}
          caption="Most active contributors by record count"
        />
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Sub-component: LeadsByStatusPanel
// ---------------------------------------------------------------------------

/**
 * Pipeline counts panel. Renders the four OutreachStatus values in their
 * canonical order with colored Badges and tabular-nums counts.
 *
 * Backend returns exactly 4 entries (one per status); we re-order client-side
 * via OUTREACH_STATUS_VALUES to ensure stable display order even if the
 * backend response order changes. Defensive defaulting to count=0 covers
 * the edge case where a backend bug omits a status entry, so the panel
 * always shows the complete four-row pipeline.
 *
 * Each row also shows the percentage of total leads represented by its
 * status, providing pipeline-shape context at a glance. The percentage
 * uses the same `tabular-nums` Tailwind utility as the count for stable
 * column alignment when values change.
 */
function LeadsByStatusPanel({
  leads,
}: {
  readonly leads: ReadonlyArray<LeadsByStatusEntry>;
}): JSX.Element {
  // Re-order to canonical OUTREACH_STATUS_VALUES order, defaulting to count=0
  // for any missing status (defensive: backend should always return all four).
  const orderedLeads: ReadonlyArray<LeadsByStatusEntry> = OUTREACH_STATUS_VALUES.map((status) => {
    const found = leads.find((entry) => entry.status === status);
    return found ?? { status, count: 0 };
  });

  const totalCount = orderedLeads.reduce((acc, entry) => acc + entry.count, 0);

  return (
    <section
      aria-labelledby="analytics-leads-heading"
      className="flex flex-col gap-3"
      data-testid="analytics-leads-panel"
    >
      <header className="flex items-center gap-2">
        <BarChart3 aria-hidden="true" className="h-5 w-5 text-brand-600" />
        <h3 id="analytics-leads-heading" className="text-base font-semibold text-slate-900">
          Leads by Status
        </h3>
      </header>
      <div className="rounded-lg border border-slate-200 bg-white">
        <ul className="divide-y divide-slate-200" data-testid="analytics-leads-list">
          {orderedLeads.map((entry) => {
            const percentage = totalCount > 0 ? (entry.count / totalCount) * 100 : 0;
            const slug = entry.status.replace(/\s+/g, "-").toLowerCase();
            return (
              <li
                key={entry.status}
                className="flex items-center justify-between gap-4 px-4 py-3"
                data-testid={`analytics-leads-row-${slug}`}
              >
                <Badge variant={statusToBadgeVariant(entry.status)} withDot>
                  {entry.status}
                </Badge>
                <div className="flex items-baseline gap-3">
                  <span
                    className="font-mono text-base font-semibold tabular-nums text-slate-900"
                    data-testid={`analytics-leads-count-${slug}`}
                  >
                    {entry.count.toLocaleString()}
                  </span>
                  <span className="text-xs tabular-nums text-slate-500">
                    {percentage.toFixed(0)}%
                  </span>
                </div>
              </li>
            );
          })}
        </ul>
        <div className="flex items-center justify-between border-t border-slate-200 bg-slate-50 px-4 py-2">
          <span className="text-xs font-medium uppercase tracking-wide text-slate-600">Total</span>
          <span
            className="font-mono text-sm font-semibold tabular-nums text-slate-900"
            data-testid="analytics-leads-total"
          >
            {totalCount.toLocaleString()}
          </span>
        </div>
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Sub-component: WeeklyActivitySparkline
// ---------------------------------------------------------------------------

/**
 * Simple SVG sparkline of records-per-week over the last N weeks.
 *
 * Implementation strategy: SVG path drawn from the data points, normalized
 * to fit the viewBox. No animation library, no chart library - just plain
 * SVG which keeps the bundle small and matches the AAP scope discipline
 * (per AAP Sec 0.6.2, "Analytics dashboard beyond the basic three panels"
 * is OUT of scope, so adding `recharts` or `victory` would violate scope).
 *
 * Edge cases handled:
 *   - 0 weeks: renders the "No activity yet" placeholder.
 *   - 1 week: renders the summary header but no sparkline (not enough
 *     data points to draw a line; a single dot is visually misleading
 *     for a trend chart).
 *   - All counts zero: renders a flat dashed baseline so the panel still
 *     has visual structure.
 *   - 2+ weeks with data: full sparkline with stroked line and a subtle
 *     filled area underneath for emphasis.
 *
 * Accessibility: the SVG carries `role="img"` and an `aria-label` so
 * screen-reader users hear a summary of what the chart shows. The
 * tabular total + peak-week summary above the SVG carries the
 * informational content - the SVG is a supplementary visualization.
 */
function WeeklyActivitySparkline({
  weeks,
}: {
  readonly weeks: ReadonlyArray<WeeklyActivityEntry>;
}): JSX.Element {
  const totalWeeks = weeks.length;
  const totalRecords = weeks.reduce((acc, week) => acc + week.record_count, 0);
  const maxCount = weeks.reduce((acc, week) => Math.max(acc, week.record_count), 0);
  const peakWeek = weeks.find((week) => week.record_count === maxCount);

  // SVG viewBox dimensions; chosen to be wide enough for legibility but
  // small enough that the sparkline scales nicely in any container with
  // preserveAspectRatio="none".
  const VIEW_W = 600;
  const VIEW_H = 80;
  const PADDING = 4;

  // Compute path d-strings. If there are fewer than 2 weeks, the sparkline
  // cannot be drawn meaningfully; the "all counts zero" fallback handles
  // the case where weeks.length >= 1 but maxCount === 0.
  let pathD = "";
  let areaD = "";
  if (weeks.length >= 2 && maxCount > 0) {
    const stepX = (VIEW_W - PADDING * 2) / (weeks.length - 1);
    const points = weeks.map((week, idx) => {
      const x = PADDING + idx * stepX;
      // Invert the y-axis: SVG (0,0) is top-left, so a higher record_count
      // corresponds to a smaller y value. Normalize each y to the
      // [PADDING, VIEW_H - PADDING] range based on maxCount.
      const y = VIEW_H - PADDING - (week.record_count / maxCount) * (VIEW_H - PADDING * 2);
      return { x, y };
    });
    pathD = points
      .map((point, idx) => `${idx === 0 ? "M" : "L"} ${point.x.toFixed(2)} ${point.y.toFixed(2)}`)
      .join(" ");
    // Closed area path under the line for the subtle fill effect. We
    // re-extract first/last via array indexing (not destructure) so the
    // `noUncheckedIndexedAccess` strict guard is exercised: both could
    // theoretically be undefined if the array were empty, but the outer
    // `weeks.length >= 2` check guarantees at least two entries.
    const firstPoint = points[0];
    const lastPoint = points[points.length - 1];
    if (firstPoint && lastPoint) {
      areaD =
        `M ${firstPoint.x.toFixed(2)} ${(VIEW_H - PADDING).toFixed(2)} ` +
        points.map((point) => `L ${point.x.toFixed(2)} ${point.y.toFixed(2)}`).join(" ") +
        ` L ${lastPoint.x.toFixed(2)} ${(VIEW_H - PADDING).toFixed(2)} Z`;
    }
  }

  return (
    <section
      aria-labelledby="analytics-activity-heading"
      className="flex flex-col gap-3"
      data-testid="analytics-activity-panel"
    >
      <header className="flex items-center gap-2">
        <Activity aria-hidden="true" className="h-5 w-5 text-emerald-600" />
        <h3 id="analytics-activity-heading" className="text-base font-semibold text-slate-900">
          Weekly Activity
        </h3>
      </header>
      <div className="flex flex-col gap-3 rounded-lg border border-slate-200 bg-white p-4">
        {totalWeeks === 0 ? (
          <p className="text-center text-sm text-slate-500" data-testid="analytics-activity-empty">
            No activity yet.
          </p>
        ) : (
          <>
            <div
              className="flex flex-wrap items-baseline justify-between gap-2"
              data-testid="analytics-activity-summary"
            >
              <div className="flex flex-col">
                <span className="text-xs uppercase tracking-wide text-slate-500">
                  Records (last {totalWeeks} {totalWeeks === 1 ? "week" : "weeks"})
                </span>
                <span className="font-mono text-2xl font-semibold tabular-nums text-slate-900">
                  {totalRecords.toLocaleString()}
                </span>
              </div>
              {peakWeek && maxCount > 0 && (
                <div className="flex flex-col text-right">
                  <span className="text-xs uppercase tracking-wide text-slate-500">Peak week</span>
                  <span className="font-mono text-sm tabular-nums text-slate-700">
                    {peakWeek.week_start} ({maxCount.toLocaleString()})
                  </span>
                </div>
              )}
            </div>
            {/* SVG sparkline. role="img" + aria-label provide a summary
                for assistive tech; the tabular summary above carries
                the meaningful information for users who cannot perceive
                the chart visually. */}
            <svg
              role="img"
              aria-label={`Weekly record count over ${totalWeeks} ${
                totalWeeks === 1 ? "week" : "weeks"
              }`}
              viewBox={`0 0 ${VIEW_W} ${VIEW_H}`}
              preserveAspectRatio="none"
              className="h-20 w-full"
              data-testid="analytics-activity-sparkline"
            >
              {/* Subtle horizontal grid: top, middle, bottom. */}
              <line
                x1="0"
                x2={VIEW_W}
                y1={PADDING}
                y2={PADDING}
                className="stroke-slate-100"
                strokeWidth="1"
              />
              <line
                x1="0"
                x2={VIEW_W}
                y1={VIEW_H / 2}
                y2={VIEW_H / 2}
                className="stroke-slate-100"
                strokeWidth="1"
              />
              <line
                x1="0"
                x2={VIEW_W}
                y1={VIEW_H - PADDING}
                y2={VIEW_H - PADDING}
                className="stroke-slate-100"
                strokeWidth="1"
              />
              {areaD !== "" && <path d={areaD} className="fill-emerald-100/60" />}
              {pathD !== "" && (
                <path
                  d={pathD}
                  className="stroke-emerald-600"
                  fill="none"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              )}
              {pathD === "" && weeks.length > 0 && (
                /* All counts zero (or only one week with non-zero count):
                   render a flat dashed baseline so the panel has structure
                   and the user can see the sparkline area was rendered. */
                <line
                  x1={PADDING}
                  x2={VIEW_W - PADDING}
                  y1={VIEW_H - PADDING}
                  y2={VIEW_H - PADDING}
                  className="stroke-slate-300"
                  strokeWidth="2"
                  strokeDasharray="4 4"
                />
              )}
              {/* Per Visual Consistency QA Issue 8: hover-driven
                  tooltip per data point. Each point is a small
                  invisible <circle> with a <title> child that the
                  browser surfaces as a native tooltip on hover or
                  focus. The radius is generous (8 px in the local
                  viewBox, ~16 px on screen at typical sizes) so a
                  pointer can land on it without pixel precision; the
                  fill is fully transparent on the rest state and only
                  becomes visible on hover (via the [&:hover>circle]
                  attribute) so the chart line stays clean.

                  The <title> element is the accessible tooltip
                  surface; it is also exposed to assistive technology
                  via the SVG accessibility tree. We render one
                  <circle> per week regardless of whether pathD or
                  areaD were drawn, so the "all zero" and "single
                  week" edge cases also have hoverable points. */}
              {weeks.length >= 1 &&
                weeks.map((week, idx) => {
                  // X-position: spread evenly across the viewBox.
                  // For weeks.length === 1 we anchor the lone point
                  // at the middle of the chart so it is visible.
                  const stepX =
                    weeks.length === 1
                      ? VIEW_W / 2 - PADDING
                      : (VIEW_W - PADDING * 2) / (weeks.length - 1);
                  const x = weeks.length === 1 ? VIEW_W / 2 : PADDING + idx * stepX;
                  // Y-position: invert as in the path computation; if
                  // every count is zero, place the marker at the
                  // baseline so the dashed-baseline panel has hover
                  // affordances too.
                  const y =
                    maxCount > 0
                      ? VIEW_H - PADDING - (week.record_count / maxCount) * (VIEW_H - PADDING * 2)
                      : VIEW_H - PADDING;
                  return (
                    <g
                      key={`activity-point-${week.week_start}`}
                      className="group"
                      data-testid={`analytics-activity-point-${idx}`}
                    >
                      <circle
                        cx={x}
                        cy={y}
                        r="8"
                        className="fill-transparent group-hover:fill-emerald-600/20 group-focus-within:fill-emerald-600/20"
                      />
                      <circle
                        cx={x}
                        cy={y}
                        r="3"
                        className="fill-emerald-600 opacity-0 group-hover:opacity-100 group-focus-within:opacity-100"
                      />
                      <title>{`Week of ${week.week_start}: ${week.record_count.toLocaleString()} ${
                        week.record_count === 1 ? "record" : "records"
                      }`}</title>
                    </g>
                  );
                })}
            </svg>
          </>
        )}
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Sub-component: LoadingState
// ---------------------------------------------------------------------------

/**
 * Skeleton state shown while useAnalyticsQuery is pending.
 *
 * Renders three placeholder panels matching the rough layout of the
 * loaded view so the page does not jank when data arrives. Each
 * skeleton uses Tailwind's `animate-pulse` utility for the gentle
 * loading shimmer, and `aria-hidden="true"` on each so assistive
 * technology announces only the busy state of the parent surface
 * (typically via the surrounding section's aria-busy or via a
 * separate live region) rather than the placeholder structure.
 */
function LoadingState(): JSX.Element {
  return (
    <div className="flex flex-col gap-6" data-testid="analytics-loading">
      {[0, 1, 2].map((idx) => (
        <div key={`skeleton-panel-${idx}`} className="flex flex-col gap-3" aria-hidden="true">
          <div className="h-6 w-48 animate-pulse rounded bg-slate-200" />
          <div className="h-32 w-full animate-pulse rounded-lg bg-slate-100" />
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sub-component: ErrorState
// ---------------------------------------------------------------------------

/**
 * Error state with retry button.
 *
 * The query's error message may be sensitive (e.g., a backend 500 with a
 * stack trace), so we display whatever message ApiError provides but
 * fall back to a user-friendly generic message when the message is
 * empty. The retry button calls refetch() to give the user agency to
 * recover from transient failures (network blip, brief backend
 * unavailability) without forcing a full page reload.
 *
 * `role="alert"` makes assistive technology announce the error
 * automatically when this component mounts, matching the WAI-ARIA
 * authoring practice for inline error feedback.
 */
function ErrorState({
  errorMessage,
  onRetry,
}: {
  readonly errorMessage: string;
  readonly onRetry: () => void;
}): JSX.Element {
  return (
    <div
      className="flex flex-col items-center gap-3 rounded-lg border border-red-200 bg-red-50 p-8 text-center"
      role="alert"
      data-testid="analytics-error"
    >
      <AlertCircle aria-hidden="true" className="h-8 w-8 text-red-600" />
      <h3 className="text-base font-semibold text-red-900">Could not load analytics</h3>
      <p className="text-sm text-red-700" data-testid="analytics-error-message">
        {errorMessage || "Something went wrong loading the analytics data."}
      </p>
      <Button
        variant="secondary"
        size="sm"
        onClick={onRetry}
        leftIcon={<RefreshCw aria-hidden="true" />}
        data-testid="analytics-retry-button"
      >
        Try again
      </Button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sub-component: AnalyticsPanels (success-state inner layout)
// ---------------------------------------------------------------------------

/**
 * Inner panel layout, extracted for testability and to avoid passing the
 * full query result through every sub-component.
 *
 * Layout:
 *   - Most Active Contributors panel takes full width on every viewport
 *     so the leaderboard table has room to breathe.
 *   - Leads by Status and Weekly Activity sit side-by-side at lg+ via
 *     `lg:grid-cols-2`, stacked on mobile.
 *   - The "Last updated" timestamp at the bottom is right-aligned in
 *     muted slate so it does not draw attention away from the data.
 *
 * The `generated_at` timestamp is parsed via `new Date(...)` and
 * formatted via `toLocaleString()` so the user sees the timestamp in
 * their browser's locale. The wrapper renders as a paragraph element
 * so screen readers announce it as part of the document flow.
 */
function AnalyticsPanels({ data }: { readonly data: AnalyticsResponse }): JSX.Element {
  return (
    <div className="flex flex-col gap-6">
      <MostActiveContributorsPanel contributors={data.most_active_contributors} />
      <div className="grid gap-6 lg:grid-cols-2">
        <LeadsByStatusPanel leads={data.leads_by_status} />
        <WeeklyActivitySparkline weeks={data.weekly_activity} />
      </div>
      <p
        className={clsx("text-xs text-slate-500", "text-right")}
        data-testid="analytics-generated-at"
      >
        Last updated: {new Date(data.generated_at).toLocaleString()}
      </p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Analytics component (exported)
// ---------------------------------------------------------------------------

/**
 * Admin Analytics view. Top-level component mounted at /admin/analytics.
 *
 * Renders three panels (Most Active Contributors, Leads by Status, Weekly
 * Activity) from the composite GET /api/admin/analytics response, with
 * loading skeletons and an error retry path.
 *
 * State machine:
 *   - isPending && !isError && !isSuccess  -> LoadingState
 *   - isError                              -> ErrorState (with retry)
 *   - isSuccess                            -> AnalyticsPanels
 *
 * The TanStack Query hook handles the underlying state transitions
 * (refetching, stale checks, error backoff). The 5-minute staleTime
 * configured on the hook in @/api/admin keeps the panel responsive on
 * navigation while reducing backend load (analytics aggregations are
 * GROUP BY queries over the records and audit_events tables).
 *
 * @returns The Admin Analytics view as a JSX.Element.
 */
export function Analytics(): JSX.Element {
  const query = useAnalyticsQuery();

  return (
    // Per Visual Consistency QA Issue 7 the route component renders
    // <section> rather than nesting a second <main> landmark inside
    // the document's primary <main> in App.tsx.
    <section
      aria-labelledby="admin-analytics-heading"
      className="flex flex-col gap-6"
      data-testid="admin-analytics"
    >
      <div className="flex items-center gap-2">
        <Users aria-hidden="true" className="h-6 w-6 text-slate-700" />
        <h2 id="admin-analytics-heading" className="text-xl font-semibold text-slate-900">
          Analytics
        </h2>
      </div>

      {query.isPending && <LoadingState />}

      {query.isError && (
        <ErrorState
          errorMessage={query.error?.message ?? ""}
          onRetry={() => {
            void query.refetch();
          }}
        />
      )}

      {query.isSuccess && <AnalyticsPanels data={query.data} />}
    </section>
  );
}
