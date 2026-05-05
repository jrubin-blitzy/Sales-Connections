/**
 * admin.ts - TanStack Query 5.x hooks for the F-014 Admin Panel surface.
 *
 * All endpoints behind these hooks are @requires_role(Admin) on the
 * backend; the API decorator is the AUTHORITATIVE auth gate per AAP
 * Sec 0.7.1 invariant 7. The SPA's <RoleGate role="Admin"> hides the
 * admin routes from non-Admin users as a UX courtesy; never rely on
 * the absence of UI to prevent action.
 *
 * Hooks exposed (queries):
 *   - useAdminUsersQuery()           GET  /api/admin/users
 *   - useAdminRecordsQuery(params)   GET  /api/admin/records
 *   - useAnalyticsQuery()            GET  /api/admin/analytics
 *
 * Hooks exposed (mutations):
 *   - useUpdateUserRoleMutation()    PATCH  /api/admin/users/:id
 *   - useHardDeleteRecordMutation()  DELETE /api/admin/records/:id
 *
 * Cache key conventions (per the assigned folder Conventions, mirroring
 * connectionKeys / tagKeys for consistency):
 *   - All admin queries share the root key 'admin':
 *       ['admin', 'users']
 *       ['admin', 'records', filters]
 *       ['admin', 'analytics']
 *   - Mutations invalidate their specific list cache and any related
 *     connection caches (e.g., hard-delete invalidates BOTH the admin
 *     records root AND the public connections root since the same
 *     record is removed from every view).
 *
 * Toast feedback:
 *   Mutation hooks fire toast.success / toast.error in their onSuccess /
 *   onError callbacks per AAP Sec 0.5.3 cross-cutting concerns. The
 *   toast facade is obtained via the useToast() hook (Toast.tsx exposes
 *   the toast object via that hook rather than as a module-level
 *   singleton, so we call useToast() at the top of each mutation hook -
 *   allowed by the React rules of hooks because the call site is
 *   itself a hook).
 *
 * Optimistic updates:
 *   Neither admin mutation uses optimistic updates. Role mutation has
 *   multiple server-side guards (anti-lockout 409, self-demotion 403)
 *   that cannot be replicated client-side; an optimistic flip then a
 *   roll-back on guard violation would be jarring. Hard-delete is a
 *   destructive operation where waiting for server confirmation before
 *   dimming the row is the right UX.
 *
 * No business logic in this file - pure HTTP plumbing with cache
 * invalidation side effects (per the assigned folder Conventions).
 *
 * Coordination touchpoints:
 *   - @/api/client            apiGet, apiPatch, apiDelete<T> (handles
 *                              204 No Content), ApiError.
 *   - @/api/connections       connectionKeys (cross-cache invalidation
 *                              on hard delete), ConnectionListParams
 *                              (filter/sort shape extended for admin
 *                              records moderation).
 *   - @/schemas/admin         Type-only imports for request/response
 *                              shapes mirroring backend pydantic.
 *   - @/schemas/connection    Type-only import of PaginatedConnections
 *                              (admin records moderation reuses the
 *                              public feed envelope).
 *   - @/components/ui/Toast   useToast() returns the toast facade with
 *                              success(), error(), etc. methods.
 *   - @tanstack/react-query   useQuery, useMutation, useQueryClient,
 *                              UseQueryResult, UseMutationResult.
 */

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";

import { apiDelete, apiGet, apiPatch, type ApiError } from "@/api/client";
import { connectionKeys, type ConnectionListParams } from "@/api/connections";
import { useToast } from "@/components/ui/Toast";
import type { AnalyticsResponse, UserRead, UserRoleUpdate } from "@/schemas/admin";
import type { PaginatedConnections } from "@/schemas/connection";

// ---------------------------------------------------------------------------
// Type definitions
// ---------------------------------------------------------------------------

/**
 * Filter, sort, and pagination parameters for `GET /api/admin/records`.
 *
 * Extends `ConnectionListParams` with the admin-only flag
 * `include_deleted` made REQUIRED (admins always make an explicit
 * choice on whether to include soft-deleted records). All other
 * connection-list params are accepted as-is so admins enjoy the full
 * filter / sort vocabulary the public feed offers.
 *
 * Why `include_deleted` is required (no default) here while it is
 * optional on the public-feed params:
 *   - The SPA's RecordModeration view exposes a checkbox bound to this
 *     flag. Forcing the consumer to thread the explicit value avoids
 *     accidental "show only active" in an admin context.
 *   - Cache keys must distinguish "show deleted" from "hide deleted"
 *     views so toggling the checkbox does not return stale data from
 *     the wrong slice. Requiring the flag means every cache key
 *     deterministically encodes the slice.
 */
export interface AdminRecordsParams extends Omit<ConnectionListParams, "include_deleted"> {
  /**
   * When true (default UX state for admin), the response includes
   * soft-deleted records (`deleted_at IS NOT NULL`). When false, only
   * active records are returned. The SPA's RecordModeration view
   * exposes a checkbox bound to this flag.
   */
  include_deleted: boolean;
}

// ---------------------------------------------------------------------------
// Cache key factory
// ---------------------------------------------------------------------------

/**
 * Cache key factory for admin queries. Mirrors the structure of
 * `connectionKeys` / `tagKeys` from sibling api modules for consistency.
 *
 * The hierarchical structure enables surgical invalidation:
 *   - queryClient.invalidateQueries({ queryKey: adminKeys.all })
 *     refreshes EVERY admin query (users, records, analytics) - useful
 *     after a destructive admin action that touches multiple panels.
 *   - queryClient.invalidateQueries({ queryKey: adminKeys.users() })
 *     refreshes the user-management table only - the right invalidation
 *     after a role mutation since records and analytics are unaffected.
 *   - queryClient.invalidateQueries({ queryKey: adminKeys.recordsAll() })
 *     refreshes ALL record-moderation queries (every filter combination
 *     active in the cache) - the right invalidation after a hard delete
 *     since every cached slice may be affected.
 *
 * `as const` returns produce `readonly [...]` tuples that satisfy
 * TanStack Query 5.x's strict QueryKey type without `any` casts.
 *
 * The factory is exported so admin feature components can build keys
 * for direct queryClient.{getQueryData,setQueryData,prefetchQuery}
 * calls without re-deriving the structure.
 */
export const adminKeys = {
  /** Root key matching every admin query. */
  all: ["admin"] as const,
  /** Specific key for the admin user-listing query. */
  users: () => [...adminKeys.all, "users"] as const,
  /** Specific record-moderation query keyed by its filter params. */
  records: (params: AdminRecordsParams) => [...adminKeys.all, "records", params] as const,
  /** Parent key for ALL record-moderation queries (every filter combo). */
  recordsAll: () => [...adminKeys.all, "records"] as const,
  /** Specific key for the analytics aggregation query. */
  analytics: () => [...adminKeys.all, "analytics"] as const,
};

// ---------------------------------------------------------------------------
// URL builder helper
// ---------------------------------------------------------------------------

/**
 * Build a URL query string from `AdminRecordsParams`.
 *
 * Inlined here rather than reusing the public-feed builder
 * (`buildConnectionListQuery` in `@/api/connections`) because admin's
 * `include_deleted` is required (vs optional on the public feed). The
 * shapes differ enough that DRY would obscure intent: sharing the
 * helper would require type unions and runtime branching to handle the
 * required-vs-optional distinction. Per the assigned folder
 * Conventions, "URL building helpers may be inlined when the param
 * shapes differ enough that DRY would obscure intent."
 *
 * Encoding semantics (identical to the public-feed builder for the
 * fields they share):
 *   - Multi-valued array params use repeating keys
 *     (e.g., `tag_ids=a&tag_ids=b`); Flask's `request.args.getlist`
 *     parses these into a Python list at the parameter level.
 *   - Boolean `include_deleted` serializes to "true" / "false";
 *     pydantic's bool coercion accepts both.
 *   - Numeric params serialize via String() so they round-trip
 *     through pydantic's int validation.
 *   - Undefined optional fields are omitted entirely so the backend's
 *     defaults apply (sort, sort_dir, page, page_size).
 *
 * Returns the empty string when no params are set, so the caller can
 * safely concatenate without producing a trailing "?" with no body.
 *
 * @param params - The filter / sort / pagination parameters with the
 *                 required include_deleted flag.
 * @returns      - Either "" (no params) or "?<encoded body>".
 */
function buildAdminRecordsQuery(params: AdminRecordsParams): string {
  const search = new URLSearchParams();

  // Scalar string filters - skipped when undefined or empty string so
  // the backend treats them as "no filter" (and the cache key stays
  // tight without empty values polluting it).
  if (params.company) {
    search.set("company", params.company);
  }
  if (params.full_name_search) {
    search.set("full_name_search", params.full_name_search);
  }
  if (params.submission_date_from) {
    search.set("submission_date_from", params.submission_date_from);
  }
  if (params.submission_date_to) {
    search.set("submission_date_to", params.submission_date_to);
  }

  // include_deleted is REQUIRED on this hook so we always serialize
  // it - this makes the intent explicit on the wire and keeps cache
  // keys distinct between the two views.
  search.set("include_deleted", String(params.include_deleted));

  // Numeric pagination - explicit-undefined check so 0 (an invalid
  // value the backend will reject with 422) round-trips clearly
  // rather than being silently dropped.
  if (params.page !== undefined) {
    search.set("page", String(params.page));
  }
  if (params.page_size !== undefined) {
    search.set("page_size", String(params.page_size));
  }

  // Sort fields - omit when undefined so the backend default applies.
  if (params.sort) {
    search.set("sort", params.sort);
  }
  if (params.sort_dir) {
    search.set("sort_dir", params.sort_dir);
  }

  // Multi-valued enum / UUID filters - repeated keys for Flask's
  // `getlist`. The `?? []` fallback means an undefined array is
  // treated identically to an empty array.
  for (const v of params.involvement ?? []) {
    search.append("involvement", v);
  }
  for (const v of params.outreach_status ?? []) {
    search.append("outreach_status", v);
  }
  for (const v of params.owner_user_ids ?? []) {
    search.append("owner_user_ids", v);
  }
  for (const v of params.tag_ids ?? []) {
    search.append("tag_ids", v);
  }

  const qs = search.toString();
  return qs ? `?${qs}` : "";
}

// ---------------------------------------------------------------------------
// Admin query hooks
// ---------------------------------------------------------------------------

/**
 * `useQuery` hook for the admin users list (`GET /api/admin/users`).
 *
 * The backend returns a PLAIN ARRAY (not a paginated envelope) per
 * `backend/app/api/admin.py` admin user listing - "no pagination
 * because tenant user counts are bounded at MVP scale (typically
 * tens of users)." The SPA's UserManagement component renders a table
 * with role-edit dropdowns per row.
 *
 * Stale time inherits the QueryClient default (60 s).
 *
 * RBAC: every call is gated server-side to Admin role; a non-Admin
 * caller receives 403 and the api/client wrapper rethrows ApiError
 * (status=403, code="forbidden"). The SPA's <RoleGate role="Admin">
 * hides the admin routes entirely so this case typically does not
 * surface to non-Admin users.
 *
 * @returns The TanStack Query result wrapping `UserRead[]`.
 */
export function useAdminUsersQuery(): UseQueryResult<UserRead[], ApiError> {
  return useQuery<UserRead[], ApiError>({
    queryKey: adminKeys.users(),
    queryFn: () => apiGet<UserRead[]>("/api/admin/users"),
  });
}

/**
 * `useQuery` hook for admin record moderation
 * (`GET /api/admin/records`).
 *
 * Uses the same response shape as the public feed
 * (`PaginatedConnections`), but admins see soft-deleted records when
 * the SPA passes `include_deleted: true`. The backend defaults
 * `include_deleted` to true for admin callers; this hook always sends
 * the explicit value so cache keys are deterministic and the wire
 * shape is unambiguous.
 *
 * `include_deleted` is REQUIRED on `AdminRecordsParams` (no default
 * value) to force admin-side components to make an explicit choice.
 * This also keeps cache keys distinct between "show deleted" and
 * "hide deleted" views, ensuring clean cache transitions when the
 * user toggles the moderation checkbox.
 *
 * Stale time inherits the QueryClient default (60 s).
 *
 * @param params - Filter / sort / pagination parameters with required
 *                 include_deleted flag.
 * @returns      - The TanStack Query result wrapping
 *                 `PaginatedConnections`.
 */
export function useAdminRecordsQuery(
  params: AdminRecordsParams,
): UseQueryResult<PaginatedConnections, ApiError> {
  return useQuery<PaginatedConnections, ApiError>({
    queryKey: adminKeys.records(params),
    queryFn: () =>
      apiGet<PaginatedConnections>(`/api/admin/records${buildAdminRecordsQuery(params)}`),
  });
}

/**
 * `useQuery` hook for the analytics aggregations
 * (`GET /api/admin/analytics`).
 *
 * Returns `AnalyticsResponse` containing three panels (per AAP
 * Sec 0.5.4 Screen 4 specification):
 *   - most_active_contributors  Top contributors by record count.
 *                               Capped at 50 rows server-side.
 *   - leads_by_status           Counts by OutreachStatus (4 entries).
 *   - weekly_activity           Trailing N weeks of submission activity.
 *
 * Stale time is set to 5 minutes (vs the QueryClient default of 60 s)
 * because analytics aggregations are expensive (GROUP BY over the
 * records and audit_events tables) and the data does not need
 * second-by-second freshness - admins glance at the panel
 * infrequently and care about hour-scale trends. The longer stale
 * time reduces backend load while keeping the panel responsive on
 * first navigation.
 *
 * Per AAP Sec 0.7.3, this aggregation hits the same composite index
 * as the F-004 feed so it remains responsive at the 10K-record
 * scale ceiling.
 *
 * @returns The TanStack Query result wrapping `AnalyticsResponse`.
 */
export function useAnalyticsQuery(): UseQueryResult<AnalyticsResponse, ApiError> {
  return useQuery<AnalyticsResponse, ApiError>({
    queryKey: adminKeys.analytics(),
    queryFn: () => apiGet<AnalyticsResponse>("/api/admin/analytics"),
    staleTime: 5 * 60 * 1000,
  });
}

// ---------------------------------------------------------------------------
// Admin mutation hooks
// ---------------------------------------------------------------------------

/**
 * `useMutation` hook for role mutation
 * (`PATCH /api/admin/users/:id`) per F-009 + F-014.
 *
 * RBAC enforcement is server-side via the @requires_role(Admin)
 * decorator. The backend service additionally enforces:
 *   - Anti-lockout: cannot demote the LAST remaining Admin (HTTP 409
 *     Conflict). Without this guard an org could lose its last admin
 *     and become permanently un-administrable.
 *   - Self-demotion blocked: an Admin cannot demote themselves even
 *     when other admins exist (HTTP 403 Forbidden). This avoids the
 *     "I lost my admin rights and can't get them back" footgun.
 *   - Cross-org isolation: target user must be in actor's org (HTTP
 *     404 Not Found - intentionally indistinguishable from
 *     "user does not exist" to avoid leaking cross-org existence).
 *
 * The SPA surfaces the first two guards as friendly toasts via the
 * onError handler so the admin understands precisely WHY their
 * action was rejected. Generic "Failed to update role" would leave
 * admins confused.
 *
 * No optimistic update: role mutations have multiple guards that
 * cannot be replicated client-side without duplicating server
 * logic. An optimistic flip to "Contributor" then a roll-back when
 * the 409 returns would be jarring. Better to wait for the server
 * response.
 *
 * Variables shape: `{ userId, payload }` so a single mutation
 * instance can mutate any user - the SPA's UserManagement table
 * supplies the userId at mutate-time rather than baking it into
 * the hook closure (which would force a separate hook instance
 * per row).
 *
 * On success: invalidates `adminKeys.users()` so the UserManagement
 * table refreshes with the new role, and shows a success toast.
 *
 * @returns The mutation result. Consumers call
 *          mutation.mutate({ userId, payload }).
 */
export function useUpdateUserRoleMutation(): UseMutationResult<
  UserRead,
  ApiError,
  { userId: string; payload: UserRoleUpdate }
> {
  const queryClient = useQueryClient();
  // useToast is itself a React hook; calling it here (inside another
  // hook) is permitted by the React rules of hooks because the call
  // site is reached through the same component tree path on every
  // render. The returned facade is referentially stable across renders.
  const toast = useToast();

  return useMutation<UserRead, ApiError, { userId: string; payload: UserRoleUpdate }>({
    mutationFn: ({ userId, payload }) =>
      apiPatch<UserRead, UserRoleUpdate>(`/api/admin/users/${encodeURIComponent(userId)}`, payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: adminKeys.users() });
      toast.success("User role updated");
    },
    onError: (error) => {
      // Map server-side guard violations to friendly admin toasts.
      // The status code (not the message) is the source of truth for
      // the branch because the backend may localize messages later.
      if (error.status === 409) {
        toast.error("Cannot demote the last remaining Admin.");
        return;
      }
      if (error.status === 403) {
        toast.error("You cannot demote yourself.");
        return;
      }
      // Fallback for 404 (cross-org or non-existent user), 422
      // (invalid role), 500 (server error), and any other non-2xx.
      // ApiError.message is always populated by the api/client
      // wrapper so the `||` branch is defensive belt-and-suspenders.
      toast.error(error.message || "Failed to update role");
    },
  });
}

/**
 * `useMutation` hook for hard-deleting a Connection record
 * (`DELETE /api/admin/records/:id`) per F-007 + F-014.
 *
 * Hard delete is the ONLY path that physically removes data from
 * the records table per AAP Sec 0.7.6. The endpoint emits an
 * `audit_events` row with `event_type=hard_delete` capturing the
 * record's full state in `before_payload` before the DELETE runs
 * (per AAP Sec 0.5.2 Layer 2 atomic state-change + audit-emit
 * transaction).
 *
 * The endpoint returns 204 No Content (empty body); the apiDelete
 * helper handles 204 by skipping JSON parse and returning
 * `undefined as void`. This is why the mutation is typed
 * `UseMutationResult<void, ...>` and `apiDelete<void>` is used.
 *
 * RBAC: server enforces @requires_role(Admin); non-Admin callers
 * receive 403. The SPA's <RoleGate role="Admin"> hides the hard-
 * delete affordance from non-Admin users.
 *
 * Cross-cache invalidation strategy:
 *   - Admin records cache: the moderation table refreshes so the
 *     deleted row disappears.
 *   - Connection caches: the public feed and any open detail views
 *     also refresh - the record is now physically gone EVERYWHERE
 *     and any cached view is stale. Without this invalidation, an
 *     admin who hard-deletes a record then navigates back to the
 *     feed would see a ghost row until the cache eviction TTL.
 *   - Analytics cache: contributor counts and leads-by-status
 *     totals may have shifted; invalidate to keep the panel
 *     accurate.
 *
 * Variables shape: `{ recordId: string }` - the SPA's
 * RecordModeration row component supplies the id at mutate-time.
 *
 * On success: shows a success toast.
 * On error: shows the error message in a toast.
 *
 * @returns The mutation result. Consumers call
 *          mutation.mutate({ recordId }).
 */
export function useHardDeleteRecordMutation(): UseMutationResult<
  void,
  ApiError,
  { recordId: string }
> {
  const queryClient = useQueryClient();
  const toast = useToast();

  return useMutation<void, ApiError, { recordId: string }>({
    mutationFn: ({ recordId }) =>
      apiDelete<void>(`/api/admin/records/${encodeURIComponent(recordId)}`),
    onSuccess: () => {
      // Refresh admin moderation lists (every filter combo cached).
      void queryClient.invalidateQueries({ queryKey: adminKeys.recordsAll() });
      // Also refresh the public feed and detail caches - the record
      // is now physically gone everywhere and any cached view is
      // stale. Cross-cache invalidation across two namespaces
      // (admin and connections) is necessary because the same
      // entity surfaces in both.
      void queryClient.invalidateQueries({ queryKey: connectionKeys.all });
      // Analytics counts may have shifted (contributor record_count,
      // leads_by_status totals, weekly_activity bucket).
      void queryClient.invalidateQueries({ queryKey: adminKeys.analytics() });
      toast.success("Connection permanently deleted");
    },
    onError: (error) => {
      toast.error(error.message || "Failed to delete connection");
    },
  });
}
