/**
 * data.ts - Type-safe factory functions for mock entities used by the
 * Sales-Connections SPA test suite.
 *
 * Why this file exists:
 *   Every test that exercises a feature talking to the backend needs
 *   realistic mock data: a User, a Connection record, a Tag, an audit
 *   history entry, a paginated envelope, an AnalyticsResponse, etc.
 *   Hand-rolling these in every test file leads to drift and tedious
 *   boilerplate. This file centralizes them as pure factory functions
 *   that produce strongly-typed entities with stable defaults; any
 *   field can be overridden via a Partial<...> argument.
 *
 * Design principles:
 *   - PURE: No side effects beyond bumping module-level ID counters.
 *     No HTTP, no MSW, no React, no test runner imports.
 *   - PREDICTABLE IDS: Each factory bumps a per-type counter and
 *     encodes the count into a syntactically-valid UUID v4 string.
 *     Tests can import and assert on these IDs deterministically.
 *   - STABLE TIMESTAMPS: All default timestamps use a fixed seed
 *     (2026-04-01T12:00:00+00:00). Tests that need different times
 *     pass overrides; tests using vi.useFakeTimers() can override
 *     the system clock and call factories that read it (none do
 *     today, but the design accommodates).
 *   - COUNTER RESET: Exports __resetFactoryCountersForTesting()
 *     called by tests/setup.ts in afterEach so per-test counter
 *     state does not leak.
 *
 * Factories exported:
 *   - makeUserRead                    one user
 *   - makeSessionRead                 one session (nested user)
 *   - makeTagRead                     one tag
 *   - makeConnectionRead              one connection record
 *   - makeConnectionList              N connection records (default 10)
 *   - makePaginatedConnections        connections wrapped in pagination
 *   - makeDuplicateCheckResponse      duplicate-check envelope
 *   - makeHistoryEntry                one audit/history entry
 *   - makePaginatedHistory            history wrapped in pagination
 *   - makeAdminUserList               N users with mixed roles
 *   - makeAnalyticsResponse           composite analytics
 *   - makeContributorActivity         one contributor activity row
 *   - makeLeadsByStatusEntry          one leads-by-status row
 *   - makeWeeklyActivityEntry         one weekly-activity row
 *   - makeGenerateNotesResponse       AI note generation response
 *   - __resetFactoryCountersForTesting  reset all module counters
 *
 * Type re-exports for handler convenience:
 *   - AdminRecordsResponse  alias for PaginatedConnections used by the
 *     admin records mock.
 *   - PaginatedHistory      pagination envelope for history endpoints
 *     (page/page_size, NOT limit/offset).
 *   - GenerateNotesResponse  AI note-generation response shape.
 *
 * Conventions per AAP Sec 0.7.7:
 *   - Strict TypeScript; no any.
 *   - Double quotes (project's prettier setting); trailing commas;
 *     2-space indent; line length <= 100.
 *   - Named exports only; no default export.
 *   - No emoji; no console.log.
 */

import type {
  ConnectionDuplicateCheckResponse,
  ConnectionHistoryEntry,
  ConnectionRead,
  PaginatedConnections,
  TagRead,
} from "@/schemas/connection";
import {
  AUDIT_EVENT_TYPE_VALUES,
  INVOLVEMENT_VALUES,
  OUTREACH_STATUS_VALUES,
} from "@/schemas/connection";
import type { SessionRead } from "@/schemas/auth";
import type {
  AnalyticsResponse,
  ContributorActivity,
  LeadsByStatusEntry,
  UserRead,
  WeeklyActivityEntry,
} from "@/schemas/admin";
import { USER_ROLE_VALUES } from "@/schemas/admin";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/**
 * Fixed seed timestamp (ISO 8601 with offset). Default for created_at,
 * updated_at, submission_date, etc. Tests that need different values
 * pass overrides; tests using vi.useFakeTimers can rely on a different
 * time-of-day if a factory ever reads the system clock (none do today).
 */
const DEFAULT_TIMESTAMP = "2026-04-01T12:00:00+00:00";

/** Fixed seed date (YYYY-MM-DD) for week_start fields. */
const DEFAULT_WEEK_START = "2026-03-30";

/** Default model identifier returned by the AI note-generation endpoint. */
const DEFAULT_AI_MODEL = "claude-sonnet-4-5";

/** Pagination defaults matching the API client expectations. */
const DEFAULT_PAGE_LIMIT = 50;
const DEFAULT_PAGE_OFFSET = 0;

// ---------------------------------------------------------------------------
// Enum-anchored defaults
// ---------------------------------------------------------------------------
//
// We reference the runtime enum constants here (rather than hardcoding
// the string literals) so that any future schema change to the upstream
// tuples surfaces as a type error in this file at compile time. This
// gives us a single source of truth and detects schema drift before
// tests run.
//
// Index access at literal positions (e.g. INVOLVEMENT_VALUES[0]) is
// safe under noUncheckedIndexedAccess: the compiler knows the tuple
// length and returns the literal type directly.

/** Default involvement value (first entry of the enum tuple: 'Warm Intro'). */
const DEFAULT_INVOLVEMENT = INVOLVEMENT_VALUES[0];

/** Default outreach status (first entry of the enum tuple: 'Not Started'). */
const DEFAULT_OUTREACH_STATUS = OUTREACH_STATUS_VALUES[0];

/**
 * Default audit event type ('edit' is the most common audit event in
 * tests). We .find() it at module load and fall back to the first
 * entry only if 'edit' is ever removed from the enum; the type
 * annotation guarantees correctness either way.
 */
const DEFAULT_AUDIT_EVENT_TYPE: (typeof AUDIT_EVENT_TYPE_VALUES)[number] =
  AUDIT_EVENT_TYPE_VALUES.find((v) => v === "edit") ?? AUDIT_EVENT_TYPE_VALUES[0];

// ---------------------------------------------------------------------------
// UUID helper
// ---------------------------------------------------------------------------

/**
 * Build a syntactically-valid UUID v4 string with a stable,
 * deterministic payload. The first segment encodes a textual prefix
 * (used for visual grouping when reading test output); the last
 * segment encodes a 12-digit hex counter so tests can assert on exact
 * IDs.
 *
 * Examples (counter = 1):
 *   makeStableUuid('user', 1)  => '75736572-0000-4000-8000-000000000001'
 *   makeStableUuid('conn', 1)  => '636f6e6e-0000-4000-8000-000000000001'
 *   makeStableUuid('tag', 1)   => '7461675f-0000-4000-8000-000000000001'
 *   makeStableUuid('hist', 1)  => '68697374-0000-4000-8000-000000000001'
 *
 * The format follows UUID v4 layout:
 *   xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx   where y in [8, 9, a, b]
 *
 * The first segment is the prefix's first 4 ASCII characters as hex
 * (zero-padded to 4 chars with '_' if shorter). This is purely
 * cosmetic; nothing in the SPA depends on decoding the prefix back.
 *
 * Validation: Passes z.string().uuid() at the schema layer.
 */
function makeStableUuid(prefix: string, counter: number): string {
  // Encode the first 4 characters of the prefix as 8 hex digits.
  const padded = prefix.padEnd(4, "_").slice(0, 4);
  let prefixHex = "";
  for (let i = 0; i < padded.length; i += 1) {
    prefixHex += padded.charCodeAt(i).toString(16).padStart(2, "0");
  }
  // Counter as 12 hex digits, zero-padded. slice(-12) trims any
  // overflow at very large counter values to keep the segment exactly
  // 12 chars; this is not a concern for tests (we never reach 2^48).
  const counterHex = counter.toString(16).padStart(12, "0").slice(-12);
  return `${prefixHex}-0000-4000-8000-${counterHex}`;
}

// ---------------------------------------------------------------------------
// Module-level ID counters
// ---------------------------------------------------------------------------

let userIdCounter = 0;
let connectionIdCounter = 0;
let tagIdCounter = 0;
let historyIdCounter = 0;

/**
 * Test-only: reset all factory ID counters to zero. Called from
 * frontend/tests/setup.ts in afterEach so per-test counter state
 * does not leak across tests. The exported name uses the
 * double-underscore + ForTesting suffix convention to make it
 * obvious this is a test-only escape hatch.
 */
export function __resetFactoryCountersForTesting(): void {
  userIdCounter = 0;
  connectionIdCounter = 0;
  tagIdCounter = 0;
  historyIdCounter = 0;
}

// ---------------------------------------------------------------------------
// makeUserRead
// ---------------------------------------------------------------------------

/**
 * Build a UserRead with stable defaults. Every call increments the
 * user-id counter so consecutive calls produce distinct entities.
 *
 * Defaults:
 *   id            UUID encoding the next user-id counter value
 *   email         user-<n>@example.com
 *   display_name  User <n>
 *   role          'Contributor' (most common role)
 *   created_at    DEFAULT_TIMESTAMP
 *
 * The schema validates email format (max 320), display_name (1-255),
 * role enum, created_at ISO 8601 with offset, and id UUID.
 */
export function makeUserRead(overrides: Partial<UserRead> = {}): UserRead {
  userIdCounter += 1;
  const counter = userIdCounter;
  return {
    id: makeStableUuid("user", counter),
    email: `user-${counter}@example.com`,
    display_name: `User ${counter}`,
    role: "Contributor",
    created_at: DEFAULT_TIMESTAMP,
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// makeSessionRead
// ---------------------------------------------------------------------------

/**
 * Build a SessionRead with a nested user. The nested shape matches the
 * backend pydantic SessionRead exactly: { user: UserRead,
 * authenticated: boolean = true }.
 *
 * The optional `user` partial is merged onto a default UserRead, letting
 * tests do e.g.:
 *   makeSessionRead({ user: { role: 'Admin' } })
 *
 * Behavior:
 *   - When `user` is a complete UserRead, all defaults are overridden,
 *     producing an equivalent (but freshly constructed) UserRead.
 *   - When `user` is a Partial<UserRead> with at least one field set,
 *     a new UserRead is created with those overrides.
 *   - When `user` is undefined, a fresh default UserRead is created.
 *
 * authenticated defaults to true (an authenticated session); pass
 * { authenticated: false } to exercise unauth UI paths in tests.
 */
export function makeSessionRead(
  overrides: { user?: Partial<UserRead>; authenticated?: boolean } = {},
): SessionRead {
  const user = makeUserRead(overrides.user ?? {});
  return {
    user,
    authenticated: overrides.authenticated ?? true,
  };
}

// ---------------------------------------------------------------------------
// makeTagRead
// ---------------------------------------------------------------------------

/**
 * Build a TagRead with stable defaults.
 *
 * Defaults:
 *   id          UUID encoding the next tag-id counter
 *   name        tag-<n>
 *   created_at  DEFAULT_TIMESTAMP
 *
 * The schema validates name (1-64 chars).
 */
export function makeTagRead(overrides: Partial<TagRead> = {}): TagRead {
  tagIdCounter += 1;
  const counter = tagIdCounter;
  return {
    id: makeStableUuid("tag", counter),
    name: `tag-${counter}`,
    created_at: DEFAULT_TIMESTAMP,
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// makeConnectionRead
// ---------------------------------------------------------------------------

/**
 * Build a ConnectionRead with stable defaults. Every call increments
 * the connection-id counter and (unless owner_user_id is overridden)
 * an internal user counter so consecutive calls produce distinct
 * records with distinct owners.
 *
 * Defaults:
 *   id                       UUID encoding the next connection counter
 *   full_name                Contact <n>
 *   linkedin_url             https://www.linkedin.com/in/contact-<n>
 *   normalized_linkedin_url  same URL (already lowercase, no query)
 *   company                  Acme Corp <n>
 *   job_title                'VP of Engineering'
 *   relationship_context     Met at conference; mutual interest in topic <n>.
 *   ai_notes                 'Suggested opener: ask about scaling challenges.'
 *   involvement              DEFAULT_INVOLVEMENT ('Warm Intro')
 *   outreach_status          DEFAULT_OUTREACH_STATUS ('Not Started')
 *   submission_date          DEFAULT_TIMESTAMP
 *   owner_user_id            UUID encoding the next user counter
 *   owner_display_name       User <n>
 *   tags                     []
 *   created_at               DEFAULT_TIMESTAMP
 *   updated_at               DEFAULT_TIMESTAMP
 *   deleted_at               null
 *
 * Tests can override individual fields via the partial argument.
 */
export function makeConnectionRead(overrides: Partial<ConnectionRead> = {}): ConnectionRead {
  connectionIdCounter += 1;
  const counter = connectionIdCounter;
  // Bump the user counter so the connection's owner has a distinct UUID
  // unless the caller overrides it explicitly. We compute these BEFORE
  // the spread so user-supplied overrides win, but we only consume a
  // counter slot if the override is absent.
  let ownerUserId: string;
  let ownerDisplayName: string;
  if (overrides.owner_user_id !== undefined) {
    ownerUserId = overrides.owner_user_id;
    ownerDisplayName = overrides.owner_display_name ?? `User ${userIdCounter}`;
  } else {
    userIdCounter += 1;
    ownerUserId = makeStableUuid("user", userIdCounter);
    ownerDisplayName = overrides.owner_display_name ?? `User ${userIdCounter}`;
  }
  const linkedinUrl = `https://www.linkedin.com/in/contact-${counter}`;
  return {
    id: makeStableUuid("conn", counter),
    full_name: `Contact ${counter}`,
    linkedin_url: linkedinUrl,
    normalized_linkedin_url: linkedinUrl,
    company: `Acme Corp ${counter}`,
    job_title: "VP of Engineering",
    relationship_context: `Met at conference; mutual interest in topic ${counter}.`,
    ai_notes: "Suggested opener: ask about scaling challenges.",
    involvement: DEFAULT_INVOLVEMENT,
    outreach_status: DEFAULT_OUTREACH_STATUS,
    submission_date: DEFAULT_TIMESTAMP,
    owner_user_id: ownerUserId,
    owner_display_name: ownerDisplayName,
    tags: [],
    created_at: DEFAULT_TIMESTAMP,
    updated_at: DEFAULT_TIMESTAMP,
    deleted_at: null,
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// makeConnectionList
// ---------------------------------------------------------------------------

/**
 * Build an array of N ConnectionRead records.
 *
 * Default: 10 records with stable IDs and incrementing counters.
 *
 * Optional perItem callback: lets the caller customize per-row fields
 * based on the row index (0-based). The returned partial is merged
 * onto the default record. Useful for tests like:
 *   makeConnectionList(3, (i) => ({
 *     full_name: `Row ${i}`,
 *     outreach_status: i === 0 ? 'In Progress' : 'Not Started',
 *   }))
 */
export function makeConnectionList(
  count = 10,
  perItem?: (idx: number) => Partial<ConnectionRead>,
): ConnectionRead[] {
  return Array.from({ length: count }, (_, idx) => {
    const overrides = perItem ? perItem(idx) : {};
    return makeConnectionRead(overrides);
  });
}

// ---------------------------------------------------------------------------
// makePaginatedConnections + AdminRecordsResponse alias
// ---------------------------------------------------------------------------

/**
 * Wrap a list of records in the pagination envelope used by both
 * GET /api/connections and GET /api/admin/records.
 *
 * Defaults:
 *   total   items.length
 *   limit   DEFAULT_PAGE_LIMIT (50)
 *   offset  DEFAULT_PAGE_OFFSET (0)
 *
 * The items array is taken AS-IS (no slicing/pagination simulation
 * here); tests that need to exercise pagination boundaries should
 * compute the exact items + total themselves.
 */
export function makePaginatedConnections(
  items: ConnectionRead[],
  overrides: Partial<{ total: number; limit: number; offset: number }> = {},
): PaginatedConnections {
  return {
    items,
    total: overrides.total ?? items.length,
    limit: overrides.limit ?? DEFAULT_PAGE_LIMIT,
    offset: overrides.offset ?? DEFAULT_PAGE_OFFSET,
  };
}

/**
 * Type alias used by handlers.ts for the admin records mock.
 * The shape is identical to PaginatedConnections; the alias exists
 * to make handler signatures self-documenting.
 */
export type AdminRecordsResponse = PaginatedConnections;

// ---------------------------------------------------------------------------
// makeDuplicateCheckResponse
// ---------------------------------------------------------------------------

/**
 * Build a ConnectionDuplicateCheckResponse.
 *
 * Defaults represent the "no duplicate found" case:
 *   duplicate_found              false
 *   existing_record_id           null
 *   existing_owner_display_name  null
 *   existing_submission_date     null
 *   normalized_linkedin_url      'https://www.linkedin.com/in/test'
 *
 * Tests for the duplicate-found path pass overrides:
 *   makeDuplicateCheckResponse({
 *     duplicate_found: true,
 *     existing_record_id: '<uuid>',
 *     existing_owner_display_name: 'Jane',
 *     existing_submission_date: '2026-01-01T00:00:00+00:00',
 *   })
 */
export function makeDuplicateCheckResponse(
  overrides: Partial<ConnectionDuplicateCheckResponse> = {},
): ConnectionDuplicateCheckResponse {
  return {
    duplicate_found: false,
    existing_record_id: null,
    existing_owner_display_name: null,
    existing_submission_date: null,
    normalized_linkedin_url: "https://www.linkedin.com/in/test",
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// makeHistoryEntry + PaginatedHistory + makePaginatedHistory
// ---------------------------------------------------------------------------

/**
 * Build a ConnectionHistoryEntry (audit trail row).
 *
 * Defaults:
 *   id                  UUID encoding the next history-id counter
 *   event_type          DEFAULT_AUDIT_EVENT_TYPE ('edit')
 *   event_timestamp     DEFAULT_TIMESTAMP
 *   actor_user_id       UUID encoding a fresh user counter (unless
 *                       caller overrides actor_user_id explicitly)
 *   actor_display_name  'User <n>'
 *   before_payload      null
 *   after_payload       null
 *
 * The before_payload and after_payload defaults are null; tests for
 * status-change or edit events should pass concrete dicts:
 *   makeHistoryEntry({
 *     event_type: 'status_change',
 *     before_payload: { outreach_status: 'Not Started' },
 *     after_payload: { outreach_status: 'In Progress' },
 *   })
 */
export function makeHistoryEntry(
  overrides: Partial<ConnectionHistoryEntry> = {},
): ConnectionHistoryEntry {
  historyIdCounter += 1;
  const counter = historyIdCounter;
  let actorId: string;
  let actorName: string | null;
  if (overrides.actor_user_id !== undefined) {
    actorId = overrides.actor_user_id;
    actorName = overrides.actor_display_name ?? `User ${userIdCounter}`;
  } else {
    userIdCounter += 1;
    actorId = makeStableUuid("user", userIdCounter);
    actorName = overrides.actor_display_name ?? `User ${userIdCounter}`;
  }
  return {
    id: makeStableUuid("hist", counter),
    event_type: DEFAULT_AUDIT_EVENT_TYPE,
    event_timestamp: DEFAULT_TIMESTAMP,
    actor_user_id: actorId,
    actor_display_name: actorName,
    before_payload: null,
    after_payload: null,
    ...overrides,
  };
}

/**
 * Pagination envelope shape for GET /api/connections/:id/history.
 *
 * NOTE: This shape uses page/page_size, NOT limit/offset like
 * PaginatedConnections does. Mirrors the backend's distinct envelope
 * for history queries.
 */
export interface PaginatedHistory {
  items: ConnectionHistoryEntry[];
  total: number;
  page: number;
  page_size: number;
}

/**
 * Wrap a list of history entries in the page/page_size envelope.
 *
 * Defaults:
 *   total      items.length
 *   page       1
 *   page_size  25
 */
export function makePaginatedHistory(
  items: ConnectionHistoryEntry[],
  overrides: Partial<{ total: number; page: number; page_size: number }> = {},
): PaginatedHistory {
  return {
    items,
    total: overrides.total ?? items.length,
    page: overrides.page ?? 1,
    page_size: overrides.page_size ?? 25,
  };
}

// ---------------------------------------------------------------------------
// makeAdminUserList
// ---------------------------------------------------------------------------

/**
 * Build an array of N admin-panel users with mixed roles.
 *
 * Distribution strategy: cycle through USER_ROLE_VALUES in order so
 * that any 3+ list contains at least one of each role. This makes
 * admin-panel role-filter tests easier to write.
 *
 * Default: 5 users.
 *
 * Note on noUncheckedIndexedAccess: a computed-index access into a
 * tuple returns T | undefined, but `idx % length` is always a valid
 * index when length > 0. We assert non-null with `!` because
 * USER_ROLE_VALUES is a non-empty const tuple by construction.
 */
export function makeAdminUserList(count = 5): UserRead[] {
  return Array.from({ length: count }, (_, idx) => {
    const role = USER_ROLE_VALUES[idx % USER_ROLE_VALUES.length]!;
    return makeUserRead({ role });
  });
}

// ---------------------------------------------------------------------------
// makeContributorActivity
// ---------------------------------------------------------------------------

/**
 * Build a ContributorActivity row for the analytics panel.
 *
 * Defaults:
 *   user_id       UUID for a fresh user counter
 *   display_name  User <n>
 *   record_count  10
 *
 * This factory always bumps the user counter to produce a distinct
 * UUID per call. Tests that need a specific UUID pass overrides.
 */
export function makeContributorActivity(
  overrides: Partial<ContributorActivity> = {},
): ContributorActivity {
  userIdCounter += 1;
  const counter = userIdCounter;
  return {
    user_id: makeStableUuid("user", counter),
    display_name: `User ${counter}`,
    record_count: 10,
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// makeLeadsByStatusEntry
// ---------------------------------------------------------------------------

/**
 * Build a LeadsByStatusEntry row for the analytics panel.
 *
 * Defaults:
 *   status  DEFAULT_OUTREACH_STATUS ('Not Started')
 *   count   0
 *
 * The schema accepts only the four OutreachStatus enum values.
 */
export function makeLeadsByStatusEntry(
  overrides: Partial<LeadsByStatusEntry> = {},
): LeadsByStatusEntry {
  return {
    status: DEFAULT_OUTREACH_STATUS,
    count: 0,
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// makeWeeklyActivityEntry
// ---------------------------------------------------------------------------

/**
 * Build a WeeklyActivityEntry row for the analytics sparkline.
 *
 * Defaults:
 *   week_start    DEFAULT_WEEK_START ('2026-03-30', a Monday)
 *   record_count  0
 *
 * The schema validates week_start matches /^\d{4}-\d{2}-\d{2}$/; any
 * override must satisfy that regex.
 */
export function makeWeeklyActivityEntry(
  overrides: Partial<WeeklyActivityEntry> = {},
): WeeklyActivityEntry {
  return {
    week_start: DEFAULT_WEEK_START,
    record_count: 0,
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// makeAnalyticsResponse
// ---------------------------------------------------------------------------

/**
 * Build a composite AnalyticsResponse for the admin analytics endpoint.
 *
 * Defaults:
 *   most_active_contributors  3 contributor rows with descending counts
 *                             (25, 15, 8) -- realistic for chart sorting
 *   leads_by_status           4 entries (one per OutreachStatus value),
 *                             zero counts unless overridden
 *   weekly_activity           4 weeks of zero-count rows ending at
 *                             DEFAULT_WEEK_START
 *   generated_at              DEFAULT_TIMESTAMP
 *
 * Tests that need realistic numbers pass the corresponding fields
 * through the overrides argument.
 */
export function makeAnalyticsResponse(
  overrides: Partial<AnalyticsResponse> = {},
): AnalyticsResponse {
  const defaultContributors: ContributorActivity[] = [
    makeContributorActivity({ record_count: 25 }),
    makeContributorActivity({ record_count: 15 }),
    makeContributorActivity({ record_count: 8 }),
  ];
  // Produces exactly 4 rows (one per OutreachStatus value) so the SPA
  // can render a complete chart even when a status has zero leads.
  const defaultLeadsByStatus: LeadsByStatusEntry[] = OUTREACH_STATUS_VALUES.map((status) =>
    makeLeadsByStatusEntry({ status, count: 0 }),
  );
  // Four weeks ending at DEFAULT_WEEK_START. The week_start values are
  // real Mondays preceding DEFAULT_WEEK_START so tests reading the
  // sparkline see chronologically meaningful dates.
  const defaultWeeklyActivity: WeeklyActivityEntry[] = [
    makeWeeklyActivityEntry({ week_start: "2026-03-09" }),
    makeWeeklyActivityEntry({ week_start: "2026-03-16" }),
    makeWeeklyActivityEntry({ week_start: "2026-03-23" }),
    makeWeeklyActivityEntry({ week_start: DEFAULT_WEEK_START }),
  ];
  return {
    most_active_contributors: defaultContributors,
    leads_by_status: defaultLeadsByStatus,
    weekly_activity: defaultWeeklyActivity,
    generated_at: DEFAULT_TIMESTAMP,
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// GenerateNotesResponse + makeGenerateNotesResponse
// ---------------------------------------------------------------------------

/**
 * Inline shape for the AI note-generation endpoint response.
 * Backend returns this shape from POST /api/notes/generate.
 *
 * Mirrored from the inline GenerateNotesResponse type in
 * frontend/src/api/notes.ts. We do NOT import that type to keep
 * data.ts dependency-light (the depends_on_files allowlist excludes
 * the api/* layer); if drift happens between the two declarations,
 * tests will surface it through type errors at the consumer.
 */
export interface GenerateNotesResponse {
  ai_notes: string;
  model: string;
  generated_at: string;
}

/**
 * Build a GenerateNotesResponse for the AI note-generation endpoint.
 *
 * Defaults:
 *   ai_notes      a concise mock talking-points string
 *   model         DEFAULT_AI_MODEL ('claude-sonnet-4-5')
 *   generated_at  DEFAULT_TIMESTAMP
 *
 * The string concatenation in ai_notes (rather than a single long
 * literal) keeps the line length under 100 characters per the
 * project's Prettier config.
 */
export function makeGenerateNotesResponse(
  overrides: Partial<GenerateNotesResponse> = {},
): GenerateNotesResponse {
  return {
    ai_notes:
      "Suggested talking points: " +
      "1. Acknowledge their recent role transition. " +
      "2. Reference our shared experience. " +
      "3. Open with a specific value-add question.",
    model: DEFAULT_AI_MODEL,
    generated_at: DEFAULT_TIMESTAMP,
    ...overrides,
  };
}
