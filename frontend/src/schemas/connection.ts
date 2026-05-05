/**
 * connection.ts - Zod 3.x schemas for the Connection Record domain.
 *
 * Mirrors backend/app/schemas/connection.py field-for-field per AAP
 * Sec 0.5.3. The backend pydantic schemas are the authoritative
 * server-side validators; these Zod schemas are a client-side UX
 * optimization that produces instant form feedback AND the TypeScript
 * types consumed throughout the connections feature.
 *
 * Schemas defined here:
 *   - ConnectionCreateSchema             POST /api/connections payload (F-001).
 *                                        Validates the 9 business fields.
 *                                        Excludes outreach_status (server
 *                                        default 'Not Started' enforced).
 *                                        Excludes owner_user_id /
 *                                        owner_display_name (per AAP
 *                                        Sec 0.7.4 - derived from session).
 *   - ConnectionUpdateSchema             PATCH /api/connections/:id payload
 *                                        (F-007). All fields optional.
 *                                        outreach_status NOT included
 *                                        (handled by dedicated endpoint).
 *   - ConnectionStatusUpdateSchema       PATCH /api/connections/:id/status
 *                                        (F-005). RBAC-gated to Sales Rep
 *                                        and Admin server-side; the schema
 *                                        only validates the payload shape.
 *   - ConnectionReadSchema               Server response for any
 *                                        connection-returning endpoint
 *                                        (F-004, F-011). Includes the
 *                                        denormalized owner_display_name
 *                                        and embedded TagRead list.
 *   - PaginatedConnectionsSchema         GET /api/connections envelope.
 *   - ConnectionDuplicateCheckResponseSchema GET /api/connections/duplicate-check
 *                                        response (F-010).
 *   - ConnectionHistoryEntrySchema       GET /api/connections/:id/history
 *                                        per-row shape (F-011).
 *   - TagReadSchema, TagCreateSchema     Tag CRUD (F-008).
 *
 * Type exports (via z.infer):
 *   - ConnectionCreate, ConnectionUpdate, ConnectionStatusUpdate,
 *     ConnectionRead, PaginatedConnections,
 *     ConnectionDuplicateCheckResponse, ConnectionHistoryEntry,
 *     TagRead, TagCreate
 *   - InvolvementValue, OutreachStatusValue, AuditEventType
 *     (literal-union types derived from the const tuples).
 *
 * Constants exported (used by Select / Badge / Chip primitives):
 *   - INVOLVEMENT_VALUES        ['Warm Intro', 'Soft Reference', 'Target Only']
 *   - OUTREACH_STATUS_VALUES    ['Not Started', 'In Progress',
 *                                'Contacted', 'Closed']
 *   - AUDIT_EVENT_TYPE_VALUES   ['create', 'status_change', 'edit', ...]
 *
 * Per AAP Sec 0.7.1 invariants 7 and 8, server-side pydantic
 * re-validation is authoritative; these client-side checks are UX-only.
 */

import { z } from "zod";

// ---------------------------------------------------------------------------
// Enum value tuples (mirror backend app.models.enums)
// ---------------------------------------------------------------------------

/**
 * Three involvement-indicator values (F-003).
 * Match backend `app.models.enums.InvolvementType` EXACTLY:
 *   - 'Warm Intro'      submitter will personally introduce
 *   - 'Soft Reference'  submitter is OK being named/referenced
 *   - 'Target Only'     submitter is only flagging the target
 *
 * Exported as a const tuple so consumers can iterate to render
 * <Select>, <Badge>, etc. without re-deriving the values.
 *
 * IMPORTANT: Capitalization and spaces must match exactly; these
 * strings are the values stored in the PostgreSQL enum type.
 */
export const INVOLVEMENT_VALUES = ["Warm Intro", "Soft Reference", "Target Only"] as const;

/**
 * InvolvementValue - TypeScript literal-union type derived from
 * INVOLVEMENT_VALUES. Equivalent to:
 *   "Warm Intro" | "Soft Reference" | "Target Only"
 *
 * Used by InvolvementBadge.tsx and Select primitives to type-narrow
 * the involvement field of ConnectionCreate / ConnectionRead.
 */
export type InvolvementValue = (typeof INVOLVEMENT_VALUES)[number];

/**
 * Four outreach-status values (F-005).
 * Match backend `app.models.enums.OutreachStatus` EXACTLY:
 *   - 'Not Started'  default for new records
 *   - 'In Progress'  sales rep is actively working the lead
 *   - 'Contacted'    sales rep has reached out
 *   - 'Closed'       lead is closed (won, lost, or unworkable)
 *
 * Exported for use in <StatusChip>, <Select>, and analytics
 * panels. Imported by @/schemas/admin for the LeadsByStatusEntry
 * schema's status field.
 */
export const OUTREACH_STATUS_VALUES = [
  "Not Started",
  "In Progress",
  "Contacted",
  "Closed",
] as const;

/**
 * OutreachStatusValue - TypeScript literal-union type derived from
 * OUTREACH_STATUS_VALUES. Equivalent to:
 *   "Not Started" | "In Progress" | "Contacted" | "Closed"
 *
 * Used by StatusChip.tsx and analytics components to type-narrow the
 * outreach_status field across ConnectionRead and the admin schemas.
 */
export type OutreachStatusValue = (typeof OUTREACH_STATUS_VALUES)[number];

/**
 * Eight audit-event types (F-013).
 * Match backend `app.models.enums.AuditEventType` EXACTLY (snake_case
 * machine-friendly values; not user-facing).
 *
 * Used by EditHistoryFeed.tsx to render type-specific UI for each
 * history row.
 */
export const AUDIT_EVENT_TYPE_VALUES = [
  "create",
  "status_change",
  "edit",
  "soft_delete",
  "hard_delete",
  "role_change",
  "authentication",
  "admin_op",
] as const;

/**
 * AuditEventType - TypeScript literal-union type derived from
 * AUDIT_EVENT_TYPE_VALUES. Equivalent to:
 *   "create" | "status_change" | "edit" | "soft_delete" |
 *   "hard_delete" | "role_change" | "authentication" | "admin_op"
 *
 * Used by EditHistoryFeed.tsx for runtime branching on event_type.
 */
export type AuditEventType = (typeof AUDIT_EVENT_TYPE_VALUES)[number];

// ---------------------------------------------------------------------------
// LinkedIn URL refinement helper
// ---------------------------------------------------------------------------

/**
 * Allowed LinkedIn host subdomains.
 *
 * MUST stay character-exact with backend
 * `app/utils/url.py::_LINKEDIN_SUBDOMAINS` so a URL accepted by the
 * Zod refine on the SPA is also accepted by the pydantic validator on
 * the API (and vice-versa). Per AAP Sec 0.5.3, "Pydantic and Zod
 * schemas mirror each other. Field names and types match exactly so
 * that the React form payload deserializes cleanly server-side." A
 * stricter Zod regex creates a UX defect: contributors whose URLs
 * carry a country subdomain (e.g., `uk.linkedin.com`) would be
 * blocked at the form even though the backend would accept the same
 * URL.
 *
 * The list contains:
 *   - `www`, `m` — global aliases
 *   - Two-letter ISO 3166-1 alpha-2 region codes that LinkedIn
 *     actively serves; non-exhaustive but covers the long tail of
 *     network exports observed in practice.
 *
 * The bare host `linkedin.com` (no subdomain) is also accepted; this
 * Set holds only the leading subdomain labels.
 */
const LINKEDIN_SUBDOMAINS: ReadonlySet<string> = new Set([
  "www",
  "m",
  "uk",
  "ca",
  "au",
  "in",
  "de",
  "fr",
  "br",
  "mx",
  "es",
  "it",
  "nl",
  "be",
  "ch",
  "at",
  "se",
  "no",
  "dk",
  "fi",
  "pl",
  "cz",
  "sk",
  "hu",
  "ru",
  "ua",
  "tr",
  "il",
  "ae",
  "sa",
  "za",
  "eg",
  "ng",
  "ke",
  "jp",
  "kr",
  "cn",
  "hk",
  "tw",
  "sg",
  "my",
  "th",
  "id",
  "ph",
  "vn",
  "nz",
  "cl",
  "ar",
  "co",
  "pe",
  "pt",
  "gr",
  "ie",
]);

/**
 * Canonical LinkedIn host. Mirrors backend
 * `app/utils/url.py::_LINKEDIN_BASE_HOST`.
 */
const LINKEDIN_BASE_HOST = "linkedin.com";

/**
 * Allowed LinkedIn profile path prefixes. Mirrors backend
 * `app/utils/url.py::_LINKEDIN_PATH_PREFIXES`.
 *
 *   - `/in/`  modern public profile URL.
 *   - `/pub/` legacy public profile URL still seen in exported address
 *             books and CRM records; the backend accepts these so the
 *             SPA must also accept them to avoid blocking contributors.
 */
const LINKEDIN_PATH_PREFIXES: readonly string[] = ["/in/", "/pub/"];

/**
 * Slug character class. Mirrors backend
 * `app/utils/url.py::_SLUG_SEGMENT_RE` exactly:
 *   `^[A-Za-z0-9._\-%]+$`
 *
 * Accepts:
 *   - ASCII alphanumerics
 *   - dot (`.`)
 *   - underscore (`_`)
 *   - hyphen (`-`)
 *   - percent-encoding sigil (`%`) — required to round-trip
 *     percent-encoded vanity URLs (e.g., when a non-ASCII profile is
 *     exported with its UTF-8 octets percent-escaped)
 *
 * The pattern is anchored on both ends so partial matches do not
 * accidentally accept slugs containing `/` or whitespace.
 */
const LINKEDIN_SLUG_PATTERN = /^[A-Za-z0-9._\-%]+$/;

/**
 * Pre-compiled regex for collapsing two-or-more consecutive `/`
 * characters in the path. Mirrors backend
 * `app/utils/url.py::_MULTIPLE_SLASH_RE`.
 */
const MULTIPLE_SLASH_PATTERN = /\/{2,}/g;

/**
 * Returns ``true`` iff *host* is `linkedin.com` or one of the allowed
 * `<subdomain>.linkedin.com` shapes.
 *
 * Mirrors backend `app/utils/url.py::_host_is_linkedin` rule-for-rule.
 * Multi-level subdomains (e.g., `foo.bar.linkedin.com`) are rejected
 * to close the spoof vector `attacker.com.linkedin.com` where the
 * attacker controls a host whose name happens to end with the
 * `.linkedin.com` suffix.
 */
function isLinkedInHost(host: string): boolean {
  if (!host) {
    return false;
  }
  if (host === LINKEDIN_BASE_HOST) {
    return true;
  }
  const suffix = `.${LINKEDIN_BASE_HOST}`;
  if (!host.endsWith(suffix)) {
    return false;
  }
  // Slice off the trailing `.linkedin.com` to inspect the leading
  // label(s). Any remaining `.` indicates a multi-level subdomain
  // which is rejected.
  const prefix = host.slice(0, host.length - suffix.length);
  if (prefix.includes(".")) {
    return false;
  }
  return LINKEDIN_SUBDOMAINS.has(prefix);
}

/**
 * Returns ``true`` iff *path* is a well-formed LinkedIn profile path.
 *
 * Mirrors backend `app/utils/url.py::_path_is_linkedin_profile`:
 *   1. Starts with `/in/` or `/pub/`.
 *   2. Has a non-empty slug segment after the prefix.
 *   3. The slug matches `LINKEDIN_SLUG_PATTERN`.
 *
 * Trailing path components (e.g., `/in/jane-doe/details/contact-info`
 * or LinkedIn `/pub/` URLs which include numeric trailing segments
 * such as `/pub/jane-doe/12/345/678`) are tolerated; only the FIRST
 * segment after the prefix is validated.
 */
function isLinkedInProfilePath(path: string): boolean {
  // Collapse accidental `//` runs the user may have pasted, then check
  // the resulting normalized path. Matches backend behavior: both
  // `/in/jane` and `//in//jane` are accepted equivalently.
  const normalized = path.replace(MULTIPLE_SLASH_PATTERN, "/");
  for (const prefix of LINKEDIN_PATH_PREFIXES) {
    if (normalized.startsWith(prefix)) {
      // Take only the first segment after the prefix. LinkedIn
      // `/pub/` URLs sometimes append trailing numeric path segments,
      // and `/in/` URLs may have `/details/contact-info` etc.
      const tail = normalized.slice(prefix.length);
      const slug = tail.split("/", 1)[0] ?? "";
      if (!slug) {
        return false;
      }
      return LINKEDIN_SLUG_PATTERN.test(slug);
    }
  }
  return false;
}

/**
 * Validates that a string looks like a LinkedIn profile URL.
 *
 * MIRRORS backend `app.utils.url.is_valid_linkedin_url` — the two
 * validators MUST accept and reject the same set of URLs per AAP Sec
 * 0.5.3 ("Pydantic and Zod schemas mirror each other"). When the
 * backend rules change, update this function in the same PR; co-
 * located tests in `frontend/tests/schemas/connection.test.ts`
 * exercise the same URL set on both validators.
 *
 * Acceptable shapes (each mirrored from the backend):
 *   - https://linkedin.com/in/<slug>
 *   - https://www.linkedin.com/in/<slug>
 *   - http://www.linkedin.com/in/<slug>            (any allowed scheme)
 *   - https://uk.linkedin.com/in/<slug>            (country subdomain)
 *   - https://de.linkedin.com/in/<slug>?utm=foo    (query allowed)
 *   - https://www.linkedin.com/in/<slug>/          (trailing slash optional)
 *   - https://www.linkedin.com/pub/<slug>/12/345   (legacy /pub/ URLs)
 *   - https://www.linkedin.com/in/Some.Slug-name%20  (URL-safe slug chars)
 *
 * Rejected shapes (each mirrored from the backend):
 *   - linkedin.cn (China-specific domain — different TLD)
 *   - https://example.com/in/jane (wrong host)
 *   - https://attacker.com.linkedin.com/in/jane (spoof: multi-level)
 *   - https://www.linkedin.com/company/<slug> (company pages)
 *   - ftp:// schemes (only http/https accepted)
 *
 * Implementation notes:
 *   - Uses the browser's native `URL` constructor (available in Node
 *     20+ and all modern browsers per the project's browserslist).
 *   - Hostname comparison is lowercase and excludes the port.
 *   - Path validation is delegated to ``isLinkedInProfilePath``,
 *     which preserves the backend's slug character class exactly.
 *
 * @param url The candidate URL string to validate.
 * @returns true when the URL matches a LinkedIn profile shape per the
 *          backend rules; false otherwise. NEVER throws.
 */
function isValidLinkedInUrl(url: string): boolean {
  try {
    const parsed = new URL(url);
    const protocol = parsed.protocol.toLowerCase();
    if (protocol !== "http:" && protocol !== "https:") {
      return false;
    }
    const host = parsed.hostname.toLowerCase();
    if (!isLinkedInHost(host)) {
      return false;
    }
    // `parsed.pathname` excludes the search and fragment portions, so
    // query strings and fragments do not interfere with path
    // validation. They are dropped during normalization on the backend
    // (per `app/utils/url.py::normalize_linkedin_url`).
    return isLinkedInProfilePath(parsed.pathname);
  } catch {
    // URL constructor throws on malformed inputs; treat as invalid.
    return false;
  }
}

// ---------------------------------------------------------------------------
// Tag schemas (defined first because ConnectionRead embeds TagRead)
// ---------------------------------------------------------------------------

/**
 * Outbound shape for an organization-scoped tag (F-008).
 * Returned by GET /api/tags and embedded inside ConnectionRead.tags.
 *
 * Mirrors backend `app.schemas.connection.TagRead`.
 */
export const TagReadSchema = z.object({
  id: z.string().uuid(),
  name: z
    .string()
    .min(1, { message: "Tag name must not be empty" })
    .max(64, { message: "Tag name must be 64 characters or fewer" }),
  created_at: z.string().datetime({ offset: true }),
});

/**
 * TagRead - TypeScript type for an organization-scoped tag.
 */
export type TagRead = z.infer<typeof TagReadSchema>;

/**
 * Inbound payload for POST /api/tags (F-008). Org scope is derived
 * server-side from g.session.org_id; the client cannot set org_id.
 *
 * Mirrors backend `app.schemas.connection.TagCreate`.
 *
 * .strict() rejects any unexpected field (extra='forbid' equivalent)
 * as defense in depth against tampering.
 */
export const TagCreateSchema = z
  .object({
    name: z
      .string()
      .min(1, { message: "Tag name is required" })
      .max(64, { message: "Tag name must be 64 characters or fewer" }),
  })
  .strict();

/**
 * TagCreate - TypeScript type for the tag-create payload.
 */
export type TagCreate = z.infer<typeof TagCreateSchema>;

// ---------------------------------------------------------------------------
// Connection write schemas (Create / Update / Status update)
// ---------------------------------------------------------------------------

/**
 * Inbound payload for POST /api/connections (F-001).
 *
 * Mirrors backend `app.schemas.connection.ConnectionCreate`.
 *
 * Validates the 9 business fields the contributor enters on the Add
 * Connection form. Per AAP Sec 0.7.4 (Security Invariants), this
 * schema EXCLUDES owner_user_id and owner_display_name - owner
 * identity is derived exclusively from g.session.user_id server-side.
 * .strict() rejects any unexpected field as defense in depth.
 *
 * NOTE: outreach_status is NOT in this schema. New records start in
 * 'Not Started' (server default). Letting the contributor set the
 * initial status would let them mark their own record 'Closed' and
 * evade sales-team workflow (F-005 violation).
 *
 * Field validations:
 *   full_name              1-255 chars (matches backend column length).
 *   linkedin_url           1-2048 chars; MUST match LinkedIn profile
 *                          shape via isValidLinkedInUrl refine.
 *   company                1-255 chars.
 *   job_title              1-255 chars.
 *   relationship_context   1-4000 chars (matches backend
 *                          AI_PROMPT_CONTEXT_MAX_CHARS).
 *   ai_notes               0-8000 chars; OPTIONAL (may be undefined
 *                          when AI failed or user chose no AI).
 *   involvement            One of three INVOLVEMENT_VALUES (F-003).
 *   tag_ids                Array of UUIDs (default []).
 *
 * The 9th field is `submission_date` which is server-set; the client
 * does NOT supply it.
 */
export const ConnectionCreateSchema = z
  .object({
    full_name: z
      .string()
      .min(1, { message: "Full name is required" })
      .max(255, { message: "Full name must be 255 characters or fewer" }),
    linkedin_url: z
      .string()
      .min(1, { message: "LinkedIn URL is required" })
      .max(2048, { message: "LinkedIn URL must be 2048 characters or fewer" })
      .refine(isValidLinkedInUrl, {
        message:
          "LinkedIn URL must be a valid LinkedIn profile URL " +
          "(e.g., https://www.linkedin.com/in/jane-doe)",
      }),
    company: z
      .string()
      .min(1, { message: "Company is required" })
      .max(255, { message: "Company must be 255 characters or fewer" }),
    job_title: z
      .string()
      .min(1, { message: "Job title is required" })
      .max(255, { message: "Job title must be 255 characters or fewer" }),
    relationship_context: z
      .string()
      .min(1, { message: "Relationship context is required" })
      .max(4000, {
        message: "Relationship context must be 4000 characters or fewer",
      }),
    ai_notes: z
      .string()
      .max(8000, { message: "AI notes must be 8000 characters or fewer" })
      .optional(),
    involvement: z.enum(INVOLVEMENT_VALUES, {
      errorMap: () => ({
        message: `Involvement must be one of: ${INVOLVEMENT_VALUES.join(", ")}`,
      }),
    }),
    tag_ids: z.array(z.string().uuid()).default([]),
  })
  .strict();

/**
 * ConnectionCreate - TypeScript type derived from ConnectionCreateSchema.
 * Consumed by AddEditConnectionForm (create mode) and api/connections.ts.
 */
export type ConnectionCreate = z.infer<typeof ConnectionCreateSchema>;

/**
 * Inbound payload for PATCH /api/connections/:id (F-007 edit).
 *
 * Mirrors backend `app.schemas.connection.ConnectionUpdate`.
 *
 * All fields are OPTIONAL - only the fields supplied by the client
 * are updated. Validators (LinkedIn URL refine, length caps) still
 * apply when a field is present.
 *
 * outreach_status is NOT included - status changes flow through the
 * dedicated PATCH /api/connections/:id/status endpoint (RBAC-gated).
 *
 * tag_ids semantics:
 *   - omitted (undefined): leave existing tags untouched
 *   - []: replace all tags with empty list
 *   - [...uuids]: replace all tags with the given list
 * This matches backend pydantic Optional[list[UUID]] = None.
 */
export const ConnectionUpdateSchema = z
  .object({
    full_name: z
      .string()
      .min(1, { message: "Full name must not be empty" })
      .max(255, { message: "Full name must be 255 characters or fewer" })
      .optional(),
    linkedin_url: z
      .string()
      .min(1, { message: "LinkedIn URL must not be empty" })
      .max(2048, { message: "LinkedIn URL must be 2048 characters or fewer" })
      .refine(isValidLinkedInUrl, {
        message:
          "LinkedIn URL must be a valid LinkedIn profile URL " +
          "(e.g., https://www.linkedin.com/in/jane-doe)",
      })
      .optional(),
    company: z
      .string()
      .min(1, { message: "Company must not be empty" })
      .max(255, { message: "Company must be 255 characters or fewer" })
      .optional(),
    job_title: z
      .string()
      .min(1, { message: "Job title must not be empty" })
      .max(255, { message: "Job title must be 255 characters or fewer" })
      .optional(),
    relationship_context: z
      .string()
      .min(1, { message: "Relationship context must not be empty" })
      .max(4000, {
        message: "Relationship context must be 4000 characters or fewer",
      })
      .optional(),
    ai_notes: z
      .string()
      .max(8000, { message: "AI notes must be 8000 characters or fewer" })
      .optional(),
    involvement: z
      .enum(INVOLVEMENT_VALUES, {
        errorMap: () => ({
          message: `Involvement must be one of: ${INVOLVEMENT_VALUES.join(", ")}`,
        }),
      })
      .optional(),
    tag_ids: z.array(z.string().uuid()).optional(),
  })
  .strict();

/**
 * ConnectionUpdate - TypeScript type for the patch payload.
 */
export type ConnectionUpdate = z.infer<typeof ConnectionUpdateSchema>;

/**
 * Inbound payload for PATCH /api/connections/:id/status (F-005).
 *
 * Mirrors backend `app.schemas.connection.ConnectionStatusUpdate`.
 *
 * Per AAP Sec 0.7.6 (Business Rules), outreach status is updatable
 * only by Sales Rep or Admin roles (RBAC enforced server-side via
 * the @requires_role decorator). This schema validates the payload
 * shape only - RBAC is the security gate.
 *
 * The handler emits an audit_events row of type 'status_change'
 * with before/after payloads.
 */
export const ConnectionStatusUpdateSchema = z
  .object({
    outreach_status: z.enum(OUTREACH_STATUS_VALUES, {
      errorMap: () => ({
        message: `Status must be one of: ${OUTREACH_STATUS_VALUES.join(", ")}`,
      }),
    }),
  })
  .strict();

/**
 * ConnectionStatusUpdate - TypeScript type for the status-mutation payload.
 */
export type ConnectionStatusUpdate = z.infer<typeof ConnectionStatusUpdateSchema>;

// ---------------------------------------------------------------------------
// Connection read schemas (server response shapes)
// ---------------------------------------------------------------------------

/**
 * Outbound shape for a Connection record.
 *
 * Mirrors backend `app.schemas.connection.ConnectionRead`.
 *
 * Returned by every connection-returning endpoint. Includes the
 * denormalized owner_display_name (per AAP Sec 0.7.6 - fast feed
 * rendering) and the embedded TagRead list (per F-008 - eager-loaded
 * to avoid N+1 queries).
 *
 * NOTE: This schema is NOT .strict(). Outbound (response) schemas
 * allow extra fields for forward-compatibility - the backend can add
 * new fields without breaking the SPA.
 *
 * Fields:
 *   id                       Server-generated UUID v4.
 *   full_name, linkedin_url,
 *   company, job_title,
 *   relationship_context,
 *   ai_notes                 Set by contributor (F-001).
 *   normalized_linkedin_url  Set by service via normalize_linkedin_url;
 *                            used for F-010 duplicate detection.
 *   involvement              Set by contributor (F-003).
 *   outreach_status          Default 'Not Started'; mutable only by
 *                            Sales Rep / Admin (F-005).
 *   submission_date          Server-set on creation; immutable.
 *   owner_user_id            Derived from g.session at creation;
 *                            immutable (F-006).
 *   owner_display_name       Denormalized snapshot of submitter's
 *                            display name AT submission time.
 *   tags                     Embedded TagRead objects (F-008).
 *   created_at, updated_at   Server timestamps.
 *   deleted_at               NULL for active records; populated
 *                            ISO-8601 UTC for soft-deleted records.
 */
export const ConnectionReadSchema = z.object({
  id: z.string().uuid(),
  full_name: z.string(),
  linkedin_url: z.string(),
  normalized_linkedin_url: z.string(),
  company: z.string(),
  job_title: z.string(),
  relationship_context: z.string(),
  ai_notes: z.string().nullable(),
  involvement: z.enum(INVOLVEMENT_VALUES),
  outreach_status: z.enum(OUTREACH_STATUS_VALUES),
  submission_date: z.string().datetime({ offset: true }),
  owner_user_id: z.string().uuid(),
  owner_display_name: z.string(),
  tags: z.array(TagReadSchema),
  created_at: z.string().datetime({ offset: true }),
  updated_at: z.string().datetime({ offset: true }),
  deleted_at: z.string().datetime({ offset: true }).nullable(),
});

/**
 * ConnectionRead - TypeScript type for a connection record.
 */
export type ConnectionRead = z.infer<typeof ConnectionReadSchema>;

/**
 * Pagination envelope for GET /api/connections (F-004).
 *
 * Mirrors backend `app.schemas.connection.PaginatedConnections`.
 *
 * Fields:
 *   items   The page of records (length <= limit).
 *   total   Total count matching the filters (across all pages).
 *           Used for "Showing 50 of 1234" labels and pagination math.
 *   limit   Page size echoed back from the request (1-200).
 *   offset  Offset echoed back from the request (>= 0).
 */
export const PaginatedConnectionsSchema = z.object({
  items: z.array(ConnectionReadSchema),
  total: z.number().int().nonnegative(),
  limit: z.number().int().min(1).max(200),
  offset: z.number().int().nonnegative(),
});

/**
 * PaginatedConnections - TypeScript type for the pagination envelope.
 */
export type PaginatedConnections = z.infer<typeof PaginatedConnectionsSchema>;

/**
 * Response for GET /api/connections/duplicate-check?linkedin_url=...
 * (F-010).
 *
 * Mirrors backend `app.schemas.connection.ConnectionDuplicateCheckResponse`.
 *
 * Per AAP Sec 0.7.6 (Business Rules):
 *   "Duplicate detection is a warning, not a block. The user is
 *   informed but may proceed."
 *
 * The SPA's DuplicateWarning component renders a non-blocking banner
 * when duplicate_found is true. When duplicate_found is false, the
 * three "existing_*" fields are null.
 */
export const ConnectionDuplicateCheckResponseSchema = z.object({
  duplicate_found: z.boolean(),
  existing_record_id: z.string().uuid().nullable(),
  existing_owner_display_name: z.string().nullable(),
  existing_submission_date: z.string().datetime({ offset: true }).nullable(),
  normalized_linkedin_url: z.string(),
});

/**
 * ConnectionDuplicateCheckResponse - TypeScript type for the
 * duplicate-check endpoint response.
 */
export type ConnectionDuplicateCheckResponse = z.infer<
  typeof ConnectionDuplicateCheckResponseSchema
>;

/**
 * Per-row shape for GET /api/connections/:id/history (F-011).
 *
 * Mirrors backend `app.schemas.connection.ConnectionHistoryEntry`.
 *
 * Each entry corresponds to one row in the audit_events table that
 * targets the requested record. The history endpoint returns a list
 * sorted by event_timestamp DESC.
 *
 * before_payload and after_payload are JSON dicts whose shape varies
 * by event_type:
 *   - status_change: { outreach_status: '...' }
 *   - edit: { <field>: <value>, ... }
 *   - soft_delete / hard_delete: full record snapshot
 *   - create: NULL before, full after
 *   - admin_op: implementation-defined
 *
 * The SPA's EditHistoryFeed component does runtime branching on
 * event_type to render the right UI for each row.
 *
 * z.unknown() (NOT z.any()) is the type-safe choice for the JSONB
 * payload values; consumers must explicitly narrow with `typeof` or
 * `in` checks before reading values.
 */
export const ConnectionHistoryEntrySchema = z.object({
  id: z.string().uuid(),
  event_type: z.enum(AUDIT_EVENT_TYPE_VALUES),
  event_timestamp: z.string().datetime({ offset: true }),
  actor_user_id: z.string().uuid(),
  actor_display_name: z.string().nullable(),
  before_payload: z.record(z.string(), z.unknown()).nullable(),
  after_payload: z.record(z.string(), z.unknown()).nullable(),
});

/**
 * ConnectionHistoryEntry - TypeScript type for one audit-history row.
 */
export type ConnectionHistoryEntry = z.infer<typeof ConnectionHistoryEntrySchema>;
