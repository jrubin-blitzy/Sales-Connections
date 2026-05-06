/**
 * connections.ts - TanStack Query 5.x hooks for the Connection record domain.
 *
 * Covers F-001, F-004, F-005, F-007, F-010, F-011 endpoints PLUS the F-008
 * Tag CRUD endpoints (co-located here per AAP Sec 0.4.3). Every hook routes
 * through `@/api/client` so correlation IDs, 401 redirects, and error envelope
 * parsing happen uniformly.
 *
 * Hooks exposed (queries):
 *   - useConnectionsQuery(params)              GET /api/connections
 *   - useConnectionQuery(id)                   GET /api/connections/:id
 *   - useConnectionHistoryQuery(id, page)      GET /api/connections/:id/history
 *   - useDuplicateCheckQuery(url, opts)        GET /api/connections/duplicate-check
 *   - useTagsQuery()                           GET /api/tags
 *
 * Hooks exposed (mutations):
 *   - useCreateConnectionMutation()            POST /api/connections
 *   - useUpdateConnectionMutation()            PATCH /api/connections/:id
 *   - useUpdateStatusMutation()                PATCH /api/connections/:id/status
 *                                              (with optimistic update)
 *   - useSoftDeleteConnectionMutation()        DELETE /api/connections/:id
 *   - useCreateTagMutation()                   POST /api/tags
 *
 * Cache key conventions (per the assigned folder Conventions):
 *   - All connection queries share the root key 'connections':
 *       ['connections', 'list', filters]
 *       ['connections', 'detail', id]
 *       ['connections', 'detail', id, 'history', page]
 *       ['connections', 'duplicateCheck', url, excludeId]
 *   - Tag queries use the root key 'tags': ['tags', 'list']
 *   - Surgical invalidation: invalidate ['connections'] to refresh all
 *     connection queries; invalidate ['connections', 'list'] for just lists.
 *
 * Toast feedback:
 *   Mutation hooks fire toast.success/error in their onSuccess/onError
 *   callbacks per AAP Sec 0.5.3 cross-cutting concerns. The toast facade
 *   is obtained via the useToast() hook (Toast.tsx exposes the toast
 *   helpers via that hook rather than as a module-level singleton, so we
 *   call useToast() at the top of each mutation hook - allowed by the
 *   React rules of hooks because the call site is itself a hook).
 *
 * Optimistic updates:
 *   useUpdateStatusMutation applies an optimistic update via onMutate so
 *   the StatusChip flips instantly. On error, onError rolls back via the
 *   captured previous value.
 *
 * No business logic in this file. Validation, formatting, and derived
 * state live in feature components (per the assigned folder Conventions).
 *
 * Coordination touchpoints:
 *   - @/api/client            apiGet, apiPost, apiPatch, apiDelete, ApiError.
 *   - @/schemas/connection    Type-only imports for request/response shapes.
 *   - @/components/ui/Toast   useToast() returns the toast facade with
 *                              success(), error(), etc. methods.
 *   - @tanstack/react-query   useQuery, useMutation, useQueryClient,
 *                              UseQueryResult, UseMutationResult (types).
 */

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";

import { apiDelete, apiGet, apiPatch, apiPost, type ApiError } from "@/api/client";
import { useToast } from "@/components/ui/Toast";
import type {
  ConnectionCreate,
  ConnectionDuplicateCheckResponse,
  ConnectionHistoryEntry,
  ConnectionRead,
  ConnectionStatusUpdate,
  ConnectionUpdate,
  PaginatedConnections,
  TagCreate,
  TagRead,
} from "@/schemas/connection";

// ---------------------------------------------------------------------------
// Type definitions
// ---------------------------------------------------------------------------

/**
 * Filter and pagination parameters for `GET /api/connections`.
 * Mirrors the backend's accepted query parameters per
 * `backend/app/api/connections.py::_parse_filters`.
 *
 * All fields are optional. Multi-valued enum/UUID filters use array
 * params (URLSearchParams supports repeating the same key); the backend
 * accepts repeated `tag_ids`, `involvement`, `outreach_status`, and
 * `owner_user_ids` keys and concatenates them at parse time.
 */
export interface ConnectionListParams {
  /** Substring match on company name (ILIKE on the backend). */
  company?: string;
  /** Substring match on full name (ILIKE on the backend). */
  full_name_search?: string;
  /** Multi-valued involvement filter; values OR'd together server-side. */
  involvement?: ReadonlyArray<"Warm Intro" | "Soft Reference" | "Target Only">;
  /** Multi-valued outreach status filter; values OR'd together. */
  outreach_status?: ReadonlyArray<"Not Started" | "In Progress" | "Contacted" | "Closed">;
  /** Multi-valued owner UUID filter. */
  owner_user_ids?: ReadonlyArray<string>;
  /** Multi-valued tag UUID filter; "any-of" semantics on the backend. */
  tag_ids?: ReadonlyArray<string>;
  /** ISO date YYYY-MM-DD lower bound (inclusive). */
  submission_date_from?: string;
  /** ISO date YYYY-MM-DD upper bound (inclusive). */
  submission_date_to?: string;
  /** Admin-only flag; non-Admin requests are silently downgraded to false. */
  include_deleted?: boolean;
  /** 1-indexed page number; backend default 1. */
  page?: number;
  /** Items per page; backend default 25, max 100. */
  page_size?: number;
  /** Sort key; backend default "submission_date". */
  sort?: "submission_date" | "full_name" | "company" | "owner_display_name" | "outreach_status";
  /** Sort direction; backend default "desc". */
  sort_dir?: "asc" | "desc";
}

/**
 * Response shape for `GET /api/connections/:id/history`.
 *
 * IMPORTANT: This is NOT the same envelope as `PaginatedConnections`.
 * The list endpoint returns `{items, total, limit, offset}` (limit/offset
 * pagination). The history endpoint returns `{items, total, page, page_size}`
 * (page/page_size pagination) per `backend/app/api/connections.py`. Keep
 * this interface local rather than reusing `PaginatedConnections`.
 */
export interface PaginatedHistory {
  /** Audit history entries for the target record, newest first. */
  items: ConnectionHistoryEntry[];
  /** Total number of history rows matching this query (across all pages). */
  total: number;
  /** 1-indexed page number echoed from the request. */
  page: number;
  /** Items per page echoed from the request. */
  page_size: number;
}

// ---------------------------------------------------------------------------
// Cache key factories
// ---------------------------------------------------------------------------

/**
 * Cache key factory for connection queries.
 *
 * The hierarchical structure enables surgical invalidation:
 *   - queryClient.invalidateQueries({ queryKey: connectionKeys.all })
 *     refreshes EVERY connection query (lists, details, history,
 *     duplicate-check) - useful after a destructive admin action.
 *   - queryClient.invalidateQueries({ queryKey: connectionKeys.lists() })
 *     refreshes only list queries - the right invalidation after
 *     create/delete since detail caches are unaffected.
 *   - queryClient.invalidateQueries({ queryKey: connectionKeys.detail(id) })
 *     refreshes a specific record's detail and ALL history pages for it
 *     (because history keys are nested under detail).
 *
 * `as const` returns produce `readonly [...]` tuples that satisfy
 * TanStack Query 5.x's strict QueryKey type without `any` casts.
 *
 * The factory is exported so feature components can build keys for
 * direct queryClient.{getQueryData,setQueryData,prefetchQuery} calls
 * without re-deriving the structure.
 */
export const connectionKeys = {
  /** Root key matching every connection query. */
  all: ["connections"] as const,
  /** Parent key for list queries. */
  lists: () => [...connectionKeys.all, "list"] as const,
  /** Specific list query keyed by its filter/sort/pagination params. */
  list: (params: ConnectionListParams) => [...connectionKeys.lists(), params] as const,
  /** Parent key for detail queries (used to invalidate ALL details at once). */
  details: () => [...connectionKeys.all, "detail"] as const,
  /** Specific detail query keyed by record id. */
  detail: (id: string) => [...connectionKeys.details(), id] as const,
  /** History query for a specific record at a specific page. */
  history: (id: string, page: number) => [...connectionKeys.detail(id), "history", page] as const,
  /** Duplicate-check query keyed by URL + optional exclude id. */
  duplicateCheck: (url: string, excludeId: string | undefined) =>
    [...connectionKeys.all, "duplicateCheck", url, excludeId ?? null] as const,
};

/**
 * Cache key factory for tag queries (co-located here per AAP Sec 0.4.3).
 *
 * Tags are simpler than connections - the backend exposes only a list
 * endpoint and a create endpoint - so the factory is correspondingly
 * shallow. The structure mirrors connectionKeys for consistency:
 * invalidateQueries({ queryKey: tagKeys.all }) refreshes every tag
 * query; invalidateQueries({ queryKey: tagKeys.lists() }) targets just
 * the list (the only kind of tag query in MVP).
 */
export const tagKeys = {
  /** Root key matching every tag query. */
  all: ["tags"] as const,
  /** Parent key for tag list queries. */
  lists: () => [...tagKeys.all, "list"] as const,
};


// ---------------------------------------------------------------------------
// URL helpers
// ---------------------------------------------------------------------------

function buildConnectionListQuery(params: ConnectionListParams): string {
  const q = new URLSearchParams();
  if (params.company) q.set("company", params.company);
  if (params.full_name_search) q.set("full_name_search", params.full_name_search);
  params.involvement?.forEach((v) => q.append("involvement", v));
  params.outreach_status?.forEach((v) => q.append("outreach_status", v));
  params.owner_user_ids?.forEach((v) => q.append("owner_user_ids", v));
  params.tag_ids?.forEach((v) => q.append("tag_ids", v));
  if (params.submission_date_from) q.set("submission_date_from", params.submission_date_from);
  if (params.submission_date_to) q.set("submission_date_to", params.submission_date_to);
  if (params.include_deleted) q.set("include_deleted", "true");
  if (params.page !== undefined) q.set("page", String(params.page));
  if (params.page_size !== undefined) q.set("page_size", String(params.page_size));
  if (params.sort) q.set("sort", params.sort);
  if (params.sort_dir) q.set("sort_dir", params.sort_dir);
  const str = q.toString();
  return str ? `/api/connections?${str}` : "/api/connections";
}

// ---------------------------------------------------------------------------
// Connection query hooks
// ---------------------------------------------------------------------------

/**
 * `useQuery` hook for the Connection Feed (F-004).
 *
 * The cache key encodes the params object so different filter
 * combinations have independent cache entries (and so the back button
 * can return to a filtered view without re-fetching). Stale time
 * inherits the QueryClient default (60 s per `lib/queryClient.ts`).
 *
 * Default-args param `params = {}` lets callers fetch the unfiltered
 * default page without constructing an explicit empty object.
 *
 * @param params Filter / sort / pagination parameters (default: empty).
 * @returns      The TanStack Query result wrapping `PaginatedConnections`.
 */
export function useConnectionsQuery(
  params: ConnectionListParams = {},
): UseQueryResult<PaginatedConnections, ApiError> {
  return useQuery<PaginatedConnections, ApiError>({
    queryKey: connectionKeys.list(params),
    queryFn: () => apiGet<PaginatedConnections>(buildConnectionListQuery(params)),
  });
}

/**
 * `useQuery` hook for the Connection Detail view (F-011).
 *
 * Disabled when `id` is empty (e.g., during route transitions before
 * the URL parameter is populated) so we don't fire a request for
 * `/api/connections/` (which would 404). The component can render its
 * loading state until `id` becomes truthy.
 *
 * @param id Connection record UUID. Empty string disables the query.
 * @returns  The TanStack Query result wrapping `ConnectionRead`.
 */
export function useConnectionQuery(id: string): UseQueryResult<ConnectionRead, ApiError> {
  return useQuery<ConnectionRead, ApiError>({
    queryKey: connectionKeys.detail(id),
    queryFn: () => apiGet<ConnectionRead>(`/api/connections/${id}`),
    enabled: id.length > 0,
  });
}

/**
 * `useQuery` hook for the Connection edit-history feed (F-011).
 *
 * Disabled when `id` is empty for the same reason as `useConnectionQuery`.
 * The cache key includes the page number so each page is cached
 * independently; consumers paginate by re-rendering with a different
 * `page` argument.
 *
 * @param id       Connection record UUID. Empty string disables the query.
 * @param page     1-indexed page number (default 1).
 * @param pageSize Items per page (default 25).
 * @returns        The TanStack Query result wrapping `PaginatedHistory`.
 */
export function useConnectionHistoryQuery(
  id: string,
  page: number = 1,
  _pageSize: number = 25,
): UseQueryResult<PaginatedHistory, ApiError> {
  return useQuery<PaginatedHistory, ApiError>({
    queryKey: connectionKeys.history(id, page),
    queryFn: () => apiGet<PaginatedHistory>(`/api/connections/${id}/history?page=${page}`),
    enabled: id.length > 0,
  });
}

/**
 * `useQuery` hook for the F-010 duplicate LinkedIn URL pre-submit check.
 *
 * The caller is responsible for debouncing the `linkedinUrl` argument
 * via React state + setTimeout (kept out of this hook so it stays
 * decoupled from input UX). Pass `enabled: false` while the user is
 * still typing or the input is empty; pass `enabled: true` after the
 * debounce settles so the request fires.
 *
 * Stale time is set to 5 minutes (vs the QueryClient default of 1
 * minute) because a duplicate-check response is stable for a given
 * URL within a short window - if the user navigates away from the
 * form and comes back, we don't need to re-fire the same request.
 *
 * @param linkedinUrl                 The raw URL to check (the server
 *                                    normalizes it before lookup).
 * @param options.enabled             Whether to actually fire the request.
 *                                    Default `false` so callers must opt
 *                                    in deliberately.
 * @param options.excludeRecordId     Pass when editing an existing
 *                                    record so the record doesn't flag
 *                                    itself as a duplicate of itself.
 * @returns                           The TanStack Query result wrapping
 *                                    `ConnectionDuplicateCheckResponse`.
 */
export function useDuplicateCheckQuery(
  linkedinUrl: string,
  options: { enabled: boolean; excludeRecordId?: string } = { enabled: false },
): UseQueryResult<ConnectionDuplicateCheckResponse, ApiError> {
  return useQuery<ConnectionDuplicateCheckResponse, ApiError>({
    queryKey: connectionKeys.duplicateCheck(linkedinUrl, options.excludeRecordId),
    queryFn: () => {
      const q = new URLSearchParams({ linkedin_url: linkedinUrl });
      if (options.excludeRecordId) q.set("exclude_id", options.excludeRecordId);
      return apiGet<ConnectionDuplicateCheckResponse>(`/api/connections/duplicate-check?${q}`);
    },
    enabled: options.enabled && linkedinUrl.length > 0,
    // Per-hook stale time override (longer than the QueryClient default
    // of 60 s) - duplicate-check responses are stable enough that
    // re-firing on remount is wasteful.
    staleTime: 5 * 60 * 1000,
  });
}

// ---------------------------------------------------------------------------
// Connection mutation hooks
// ---------------------------------------------------------------------------

/**
 * `useMutation` hook for creating a Connection record (F-001).
 *
 * RBAC: backend rejects non-(Contributor / Admin) calls with 403. The
 * client-side route already gates access to the form, but the API
 * decorator is the authoritative gate per AAP Sec 0.7.1.
 *
 * On success: invalidates the connection list cache (so the new record
 * appears in the feed without a manual refresh) and shows a success
 * toast. The detail cache is not pre-populated; the user is expected
 * to navigate to the detail view (which fires its own query) or to
 * remain on the form with a "Connection added" toast.
 *
 * On error: shows an error toast with the API error message.
 *
 * @returns The mutation result. Consumers call mutation.mutate(payload).
 */
export function useCreateConnectionMutation(): UseMutationResult<
  ConnectionRead,
  ApiError,
  ConnectionCreate
> {
  const queryClient = useQueryClient();
  // useToast is itself a React hook; calling it here (inside another
  // hook) is permitted by the React rules of hooks because the call
  // site is reached through the same component tree path on every
  // render. The returned facade is referentially stable across renders.
  const toast = useToast();

  return useMutation<ConnectionRead, ApiError, ConnectionCreate>({
    mutationFn: (payload) => apiPost<ConnectionRead, ConnectionCreate>("/api/connections", payload),
    onSuccess: () => {
      // Invalidate every list cache; the next render of the feed
      // refetches and the new record appears at the appropriate sort
      // position (the backend sorts by submission_date DESC by default).
      void queryClient.invalidateQueries({ queryKey: connectionKeys.lists() });
      toast.success("Connection added");
    },
    onError: (error) => {
      // ApiError.message is always populated (the client.ts wrapper
      // falls back to a default-message-for-status string when the
      // backend envelope is malformed). The `||` branch is defensive.
      toast.error(error.message || "Failed to add connection");
    },
  });
}

/**
 * `useMutation` hook for editing a Connection record (F-007).
 *
 * Variables shape: `{ id, payload }` so a single mutation instance can
 * edit any record - the SPA's edit form supplies the id at mutate-time
 * rather than baking it into the hook closure (which would force a
 * separate hook instance per record).
 *
 * RBAC: backend allows the record owner OR an Admin per F-007. The
 * backend rejects forbidden calls with 403; the SPA hides the edit
 * affordance when `useRole()` denies it as a secondary defense.
 *
 * On success: invalidates BOTH the list cache (in case sort fields
 * changed) AND the specific detail cache (so any open detail view
 * refreshes with the updated payload). Shows a success toast.
 *
 * @returns The mutation result. Consumers call mutation.mutate({ id, payload }).
 */
export function useUpdateConnectionMutation(): UseMutationResult<
  ConnectionRead,
  ApiError,
  { id: string; payload: ConnectionUpdate }
> {
  const queryClient = useQueryClient();
  const toast = useToast();

  return useMutation<ConnectionRead, ApiError, { id: string; payload: ConnectionUpdate }>({
    mutationFn: ({ id, payload }) => apiPatch<ConnectionRead, ConnectionUpdate>(`/api/connections/${id}`, payload),
    // The unused `data` parameter is required because TanStack Query
    // passes (data, variables, context) to the onSuccess callback in
    // that order; we destructure variables to read the id.
    onSuccess: (_data, { id }) => {
      void queryClient.invalidateQueries({ queryKey: connectionKeys.lists() });
      void queryClient.invalidateQueries({ queryKey: connectionKeys.detail(id) });
      toast.success("Connection updated");
    },
    onError: (error) => {
      toast.error(error.message || "Failed to update connection");
    },
  });
}

/**
 * `useMutation` hook for outreach-status mutation (F-005), with
 * optimistic update so the StatusChip flips instantly.
 *
 * RBAC: backend rejects non-(Sales Rep / Admin) calls with 403. The
 * client-side `<RoleGate>` hides the trigger, but the API decorator is
 * the authoritative gate per AAP Sec 0.7.1.
 *
 * Optimistic update strategy (canonical TanStack Query 5.x pattern):
 *
 *   onMutate    Cancel any in-flight detail refetches so the optimistic
 *               value isn't overwritten before the user sees it. Snapshot
 *               the current detail cache, then write the new status into
 *               the cached record. Return the snapshot so onError has
 *               something to roll back to.
 *   onError     Restore the snapshot via setQueryData (NOT
 *               invalidateQueries - we want the immediate rollback, not
 *               a refetch that races with the still-pending mutation).
 *               Then show an error toast.
 *   onSuccess   Show a success toast. The cache will converge to the
 *               server's authoritative value via onSettled.
 *   onSettled   ALWAYS invalidate detail and list caches so the cache
 *               reflects the server's authoritative state regardless of
 *               success or failure path.
 *
 * The 4th type parameter on UseMutationResult / useMutation is the
 * `TContext` returned by onMutate and consumed by onError - critical
 * for type-safe rollback.
 *
 * @returns The mutation result. Consumers call
 *          mutation.mutate({ id, payload: { outreach_status } }).
 */
export function useUpdateStatusMutation(): UseMutationResult<
  ConnectionRead,
  ApiError,
  { id: string; payload: ConnectionStatusUpdate },
  { previousDetail: ConnectionRead | undefined }
> {
  const queryClient = useQueryClient();
  const toast = useToast();

  return useMutation<
    ConnectionRead,
    ApiError,
    { id: string; payload: ConnectionStatusUpdate },
    { previousDetail: ConnectionRead | undefined }
  >({
    mutationFn: ({ id, payload }) => apiPatch<ConnectionRead, ConnectionStatusUpdate>(`/api/connections/${id}/status`, payload),
    onMutate: async ({ id, payload }) => {
      // Cancel any in-flight detail refetches so the optimistic value
      // does not race with a stale server response and lose.
      await queryClient.cancelQueries({ queryKey: connectionKeys.detail(id) });

      // Snapshot the current detail cache so onError can restore it.
      // May be undefined if the detail query was never triggered (e.g.,
      // status mutation invoked from the feed row without ever opening
      // the detail page). The undefined branch is handled in onError.
      const previousDetail = queryClient.getQueryData<ConnectionRead>(connectionKeys.detail(id));

      // Apply the optimistic update only when there's a cached record
      // to update. If no detail cache exists, the optimistic update
      // would be a no-op anyway (the feed row reads from the list cache).
      if (previousDetail) {
        queryClient.setQueryData<ConnectionRead>(connectionKeys.detail(id), {
          ...previousDetail,
          outreach_status: payload.outreach_status,
        });
      }

      // Returned context is typed as `{ previousDetail: ConnectionRead | undefined }`
      // and forwarded to onError as the third positional argument.
      return { previousDetail };
    },
    onError: (error, { id }, context) => {
      // Roll back the optimistic update via setQueryData. We do NOT
      // call invalidateQueries here because that would race with the
      // pending mutation's settlement and cause a UI flicker.
      if (context?.previousDetail) {
        queryClient.setQueryData(connectionKeys.detail(id), context.previousDetail);
      }
      toast.error(error.message || "Failed to update status");
    },
    onSuccess: () => {
      toast.success("Status updated");
    },
    onSettled: (_data, _error, { id }) => {
      // Always converge to server state regardless of success or failure.
      // This catches edge cases where the optimistic value drifted from
      // the authoritative server payload (e.g., the server normalized
      // capitalization, or another field changed alongside the status).
      void queryClient.invalidateQueries({ queryKey: connectionKeys.detail(id) });
      void queryClient.invalidateQueries({ queryKey: connectionKeys.lists() });
    },
  });
}

/**
 * `useMutation` hook for soft-deleting a Connection record (F-007).
 *
 * The backend returns 200 with the soft-deleted record (deleted_at
 * populated to the current timestamp). On success: invalidates the
 * list cache (so the row disappears from the default feed view) and
 * the specific detail cache (so any open detail view reflects the
 * soft-deleted state). Shows a success toast.
 *
 * The detail cache is NOT removed (only invalidated) so an admin
 * navigating to a soft-deleted record's detail page can still see it
 * and potentially un-delete it.
 *
 * RBAC: backend allows the record owner OR an Admin per F-007.
 *
 * @returns The mutation result. Consumers call mutation.mutate({ id }).
 */
export function useSoftDeleteConnectionMutation(): UseMutationResult<
  ConnectionRead,
  ApiError,
  { id: string }
> {
  const queryClient = useQueryClient();
  const toast = useToast();

  return useMutation<ConnectionRead, ApiError, { id: string }>({
    mutationFn: ({ id }) => apiDelete<ConnectionRead>(`/api/connections/${id}`),
    onSuccess: (_data, { id }) => {
      void queryClient.invalidateQueries({ queryKey: connectionKeys.lists() });
      void queryClient.invalidateQueries({ queryKey: connectionKeys.detail(id) });
      toast.success("Connection deleted");
    },
    onError: (error) => {
      toast.error(error.message || "Failed to delete connection");
    },
  });
}

// ---------------------------------------------------------------------------
// Tag hooks (co-located here per AAP Sec 0.4.3)
// ---------------------------------------------------------------------------

/**
 * `useQuery` hook for tag listing (F-008).
 *
 * The backend returns a plain array (NOT a paginated envelope) sorted
 * by name ascending. Used by the Connection Feed filter UI and the
 * AddEditConnectionForm's tag autocomplete control.
 *
 * Stale time inherits the QueryClient default (60 s). Tags rarely
 * change so this could be longer; left at the default for consistency
 * with the rest of the api/ folder. A future enhancement could use
 * a longer stale time and a manual refetch on tag-create.
 *
 * @returns The TanStack Query result wrapping `TagRead[]`.
 */
export function useTagsQuery(): UseQueryResult<TagRead[], ApiError> {
  return useQuery<TagRead[], ApiError>({
    queryKey: tagKeys.lists(),
    queryFn: () => apiGet<TagRead[]>("/api/tags"),
  });
}

export function useCompaniesQuery(): UseQueryResult<string[], ApiError> {
  return useQuery<string[], ApiError>({
    queryKey: [...connectionKeys.lists(), "companies"] as const,
    queryFn: () => Promise.resolve([]),
    staleTime: Infinity,
  });
}

/**
 * `useMutation` hook for tag creation (F-008).
 *
 * The backend returns 201 (newly created) or 200 (existing tag matched
 * the case-insensitive `(org_id, name)` unique key - idempotent
 * behavior). Both cases yield a `TagRead` body; the hook does not
 * differentiate, so the consumer sees a single uniform success path.
 *
 * On success: invalidates the tag list cache so any open filter UI or
 * autocomplete control picks up the new tag, then shows a success
 * toast. The toast appears even on idempotent re-fetches because from
 * the user's perspective the tag is now present in the list.
 *
 * @returns The mutation result. Consumers call mutation.mutate(payload).
 */
export function useCreateTagMutation(): UseMutationResult<TagRead, ApiError, TagCreate> {
  const queryClient = useQueryClient();
  const toast = useToast();

  return useMutation<TagRead, ApiError, TagCreate>({
    mutationFn: (payload) => apiPost<TagRead, TagCreate>("/api/tags", payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: tagKeys.lists() });
      toast.success("Tag added");
    },
    onError: (error) => {
      toast.error(error.message || "Failed to add tag");
    },
  });
}
