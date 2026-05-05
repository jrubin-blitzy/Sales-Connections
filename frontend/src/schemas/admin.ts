/**
 * admin.ts - Zod 3.x schemas for the F-014 Admin Panel surface.
 *
 * Mirrors backend/app/schemas/admin.py field-for-field per AAP Sec 0.5.3.
 * The backend pydantic schemas are the authoritative server-side validators;
 * these Zod schemas are a client-side UX optimization that produces instant
 * form feedback AND the TypeScript types consumed throughout the admin panel.
 *
 * Schemas defined here:
 *   - UserReadSchema             User row returned by admin endpoints.
 *                                Excludes password_hash, org_id (server-only
 *                                fields per AAP Sec 0.7.4) and deleted_at
 *                                (no soft-delete on users in MVP). The
 *                                backend pydantic UserRead is the
 *                                authoritative contract; mirroring it here
 *                                guarantees deserialization compatibility.
 *   - UserRoleUpdateSchema       Payload for PATCH /api/admin/users/:id
 *                                role mutation (F-009 + F-014). Strict mode
 *                                rejects extra keys to mirror the backend's
 *                                extra='forbid' anti-tampering posture.
 *   - ContributorActivitySchema  One row of the most-active-contributors
 *                                analytics panel.
 *   - LeadsByStatusEntrySchema   One row of the leads-by-status panel.
 *                                The status field reuses
 *                                OUTREACH_STATUS_VALUES from the
 *                                connection schemas as the single source of
 *                                truth for the four-value enum.
 *   - WeeklyActivityEntrySchema  One row of the weekly-activity sparkline.
 *   - AnalyticsResponseSchema    Composite response from
 *                                GET /api/admin/analytics combining the
 *                                three analytics panels plus a
 *                                generated_at snapshot timestamp.
 *
 * Type exports (via z.infer):
 *   - UserRead, UserRoleUpdate, ContributorActivity,
 *     LeadsByStatusEntry, WeeklyActivityEntry, AnalyticsResponse
 *
 * Constants exported:
 *   - USER_ROLE_VALUES - readonly tuple ['Admin', 'Contributor', 'Viewer']
 *     used by Select primitive options and the role enum schemas.
 *
 * Per AAP Sec 0.7.1 invariant 8, server-side pydantic re-validation is
 * authoritative; these client-side checks are UX-only.
 *
 * Forward-compatibility: outbound (response) schemas do NOT use .strict()
 * so that benign backend additions (e.g. a future last_login_at field on
 * UserRead) do not force an immediate frontend release. Inbound (request)
 * schemas DO use .strict() to surface form bugs early.
 */

import { z } from "zod";

import { OUTREACH_STATUS_VALUES } from "@/schemas/connection";

// ---------------------------------------------------------------------------
// Enum value tuples (mirror backend app.models.enums.UserRole)
// ---------------------------------------------------------------------------

/**
 * Three-role authorization model per F-009 (AAP Sec 0.5.2 Layer 1).
 * Values match backend `app.models.enums.UserRole` EXACTLY:
 *   - "Admin"        full platform control, role mutation, hard delete
 *   - "Contributor"  can create/edit own records (no status mutation)
 *   - "Viewer"       sales-rep role; can mutate outreach status
 *
 * Exported as a const tuple so consumers can iterate to render
 * <Select> options without re-deriving the values. The capitalization
 * (PascalCase, no abbreviations) matches the PostgreSQL enum type
 * `user_role` created by migration 0001 -- drift here would break API
 * deserialization on every admin endpoint.
 */
export const USER_ROLE_VALUES = ["Admin", "Contributor", "Viewer"] as const;

// ---------------------------------------------------------------------------
// UserReadSchema - outbound shape for admin user endpoints
// ---------------------------------------------------------------------------

/**
 * Outbound shape for a User record from `GET /api/admin/users` and
 * `PATCH /api/admin/users/:id` (after role mutation). Also embedded in
 * `LoginResponse.user` and `SessionRead.user`.
 *
 * Mirrors backend `app.schemas.admin.UserRead`.
 *
 * SECURITY: Backend NEVER exposes `password_hash` or `org_id` even to
 * admin clients. Per AAP Sec 0.7.4 ("Passwords stored as bcrypt salted
 * hashes... never logged") and the multi-tenant scope-leak prevention
 * principle, those fields are explicitly omitted from this schema. If
 * the backend ever accidentally serialized them, they would be silently
 * dropped at parse time (default Zod behavior is permissive on extra
 * keys), keeping the SPA tolerant of non-breaking backend additions
 * such as a future `last_login_at` field.
 *
 * NOTE on `deleted_at`: there is no soft-delete on users in MVP. If
 * user soft-delete is added later, this schema must add the field at
 * the same time as the backend `UserRead` to keep parsing aligned.
 */
export const UserReadSchema = z.object({
  id: z.string().uuid({ message: "id must be a valid UUID" }),
  email: z
    .string()
    .email({ message: "email must be a valid email address" })
    .max(320, { message: "email exceeds RFC 5321 maximum (320 characters)" }),
  display_name: z
    .string()
    .min(1, { message: "display_name must not be empty" })
    .max(255, { message: "display_name exceeds 255 characters" }),
  role: z.enum(USER_ROLE_VALUES, {
    errorMap: () => ({
      message: `role must be one of: ${USER_ROLE_VALUES.join(", ")}`,
    }),
  }),
  created_at: z.string().datetime({
    offset: true,
    message: "created_at must be an ISO 8601 timestamp with offset",
  }),
});

/**
 * UserRead - TypeScript type derived from UserReadSchema.
 * Consumed by AdminPanel, UserManagement, RecordModeration, and the
 * AuthProvider session context.
 */
export type UserRead = z.infer<typeof UserReadSchema>;

// ---------------------------------------------------------------------------
// UserRoleUpdateSchema - inbound payload for role mutation
// ---------------------------------------------------------------------------

/**
 * Inbound payload for `PATCH /api/admin/users/:id` (F-009 + F-014).
 * Single-field schema -- only the new role is mutable here. The path
 * parameter `:id` identifies the target user.
 *
 * Mirrors backend `app.schemas.admin.UserRoleUpdate`.
 *
 * Per AAP Sec 0.7.4 (Security Invariants), the backend's pydantic
 * schema rejects extra fields via `extra='forbid'` to defend against
 * role-escalation tampering (e.g., a malicious attempt to send
 * `{"role": "Admin", "email": "victim@x"}` to mutate fields beyond
 * role). The Zod schema here uses `.strict()` to mirror that behavior
 * on the client (instant feedback if a stale form somehow includes
 * other fields). This is a UX optimization; the backend remains the
 * authoritative gate per AAP Sec 0.7.1 invariant 8.
 */
export const UserRoleUpdateSchema = z
  .object({
    role: z.enum(USER_ROLE_VALUES, {
      errorMap: () => ({
        message: `role must be one of: ${USER_ROLE_VALUES.join(", ")}`,
      }),
    }),
  })
  .strict();

/**
 * UserRoleUpdate - TypeScript type derived from UserRoleUpdateSchema.
 * Consumed by UserManagement.tsx for the role-edit form.
 */
export type UserRoleUpdate = z.infer<typeof UserRoleUpdateSchema>;

// ---------------------------------------------------------------------------
// ContributorActivitySchema - one row of the most-active-contributors panel
// ---------------------------------------------------------------------------

/**
 * One row of the "most active contributors" analytics panel. Returned
 * as part of `AnalyticsResponse.most_active_contributors`. The list is
 * ordered by `record_count DESC` and capped server-side at 50 rows.
 *
 * Mirrors backend `app.schemas.admin.ContributorActivity`.
 *
 * Fields:
 *   user_id       UUID of the contributor.
 *   display_name  Display name at the time of aggregation. Note this
 *                 reads from `users.display_name` (NOT from
 *                 `records.owner_display_name`) so a recent rename
 *                 surfaces in the analytics panel on the next refresh.
 *   record_count  Number of NON-soft-deleted records owned by this
 *                 user in the contributor's organization. Filtered by
 *                 `deleted_at IS NULL` server-side.
 */
export const ContributorActivitySchema = z.object({
  user_id: z.string().uuid({ message: "user_id must be a valid UUID" }),
  display_name: z
    .string()
    .min(1, { message: "display_name must not be empty" })
    .max(255, { message: "display_name exceeds 255 characters" }),
  record_count: z
    .number()
    .int({ message: "record_count must be an integer" })
    .nonnegative({ message: "record_count must be >= 0" }),
});

/**
 * ContributorActivity - TypeScript type for one analytics row.
 * Consumed by Analytics.tsx for the most-active-contributors panel.
 */
export type ContributorActivity = z.infer<typeof ContributorActivitySchema>;

// ---------------------------------------------------------------------------
// LeadsByStatusEntrySchema - one row of the leads-by-status panel
// ---------------------------------------------------------------------------

/**
 * One row of the "leads by status" analytics panel. Returned as part
 * of `AnalyticsResponse.leads_by_status`. The list contains exactly
 * four entries (one per OutreachStatus value) so the SPA can render a
 * complete chart even when a status has zero leads.
 *
 * Mirrors backend `app.schemas.admin.LeadsByStatusEntry`.
 *
 * Why an array and not an object keyed by status:
 *   The backend pydantic source-of-truth uses a list[LeadsByStatusEntry]
 *   which preserves ordering (Not Started -> In Progress -> Contacted
 *   -> Closed for pipeline rendering) and is more extensible than a
 *   dict (the entry shape can grow per-status metadata without a
 *   schema break). Mirror the backend exactly.
 *
 * Fields:
 *   status  One of the four OutreachStatus values (Not Started,
 *           In Progress, Contacted, Closed). Imported from
 *           @/schemas/connection as the single source of truth tuple.
 *   count   Number of NON-soft-deleted records in the org with this
 *           status. Filtered by `deleted_at IS NULL` server-side.
 */
export const LeadsByStatusEntrySchema = z.object({
  status: z.enum(OUTREACH_STATUS_VALUES, {
    errorMap: () => ({
      message: `status must be one of: ${OUTREACH_STATUS_VALUES.join(", ")}`,
    }),
  }),
  count: z
    .number()
    .int({ message: "count must be an integer" })
    .nonnegative({ message: "count must be >= 0" }),
});

/**
 * LeadsByStatusEntry - TypeScript type for one analytics row.
 * Consumed by Analytics.tsx for the leads-by-status panel.
 */
export type LeadsByStatusEntry = z.infer<typeof LeadsByStatusEntrySchema>;

// ---------------------------------------------------------------------------
// WeeklyActivityEntrySchema - one row of the weekly-activity sparkline
// ---------------------------------------------------------------------------

/**
 * One row of the weekly activity sparkline panel. Returned as part of
 * `AnalyticsResponse.weekly_activity`. The sparkline shows record-
 * creation activity over the trailing N weeks (12 by default; capped
 * at 52 server-side).
 *
 * Mirrors backend `app.schemas.admin.WeeklyActivityEntry`.
 *
 * Fields:
 *   week_start    ISO date (YYYY-MM-DD) of the Monday that begins the
 *                 week (UTC). Backend bins by
 *                 DATE_TRUNC('week', submission_date). Validated with
 *                 a regex rather than Zod's `.date()` method to remain
 *                 portable across all Zod 3.x patch versions.
 *   record_count  Number of records CREATED during this week.
 *                 INCLUDES soft-deleted records because the sparkline
 *                 measures contributor *activity*, not active
 *                 inventory (a record submitted then later removed
 *                 still counts as a contribution).
 */
export const WeeklyActivityEntrySchema = z.object({
  week_start: z.string().regex(/^\d{4}-\d{2}-\d{2}$/, {
    message: "week_start must be ISO date in YYYY-MM-DD format",
  }),
  record_count: z
    .number()
    .int({ message: "record_count must be an integer" })
    .nonnegative({ message: "record_count must be >= 0" }),
});

/**
 * WeeklyActivityEntry - TypeScript type for one analytics row.
 * Consumed by Analytics.tsx for the weekly-activity sparkline.
 */
export type WeeklyActivityEntry = z.infer<typeof WeeklyActivityEntrySchema>;

// ---------------------------------------------------------------------------
// AnalyticsResponseSchema - composite response for GET /api/admin/analytics
// ---------------------------------------------------------------------------

/**
 * Composite response for `GET /api/admin/analytics` (F-014). Aggregates
 * three panels in a single round-trip so the Analytics view renders
 * without a waterfall of three sequential requests.
 *
 * Mirrors backend `app.schemas.admin.AnalyticsResponse`.
 *
 * Per AAP Sec 0.5.4 (Screen 4 - Admin Panel):
 *   "The Analytics tab renders three small panels: most active
 *   contributors, leads by status, and a simple weekly activity
 *   sparkline."
 *
 * Per AAP Sec 0.7.3, this aggregation hits the same indexes as the
 * F-004 feed (composite (org_id, deleted_at, submission_date DESC))
 * so it remains responsive at the 10K-record scale ceiling.
 *
 * Fields:
 *   most_active_contributors  Ordered by record_count DESC, capped at
 *                             50 rows. Empty array for new orgs.
 *   leads_by_status           One entry per OutreachStatus value (4
 *                             entries total). Empty array for new orgs.
 *   weekly_activity           Trailing N weeks (default 12, max 52).
 *                             Empty array for new orgs.
 *   generated_at              Timezone-aware UTC timestamp recorded
 *                             when the aggregation snapshot was taken.
 *                             Lets the SPA show "Refreshed N minutes
 *                             ago" hints AND lets the handler add
 *                             `Cache-Control: max-age=60` headers
 *                             without ambiguity.
 */
export const AnalyticsResponseSchema = z.object({
  most_active_contributors: z.array(ContributorActivitySchema),
  leads_by_status: z.array(LeadsByStatusEntrySchema),
  weekly_activity: z.array(WeeklyActivityEntrySchema),
  generated_at: z.string().datetime({
    offset: true,
    message: "generated_at must be an ISO 8601 timestamp with offset",
  }),
});

/**
 * AnalyticsResponse - TypeScript type for the composite analytics
 * response. Consumed by frontend/src/features/admin/Analytics.tsx.
 */
export type AnalyticsResponse = z.infer<typeof AnalyticsResponseSchema>;
