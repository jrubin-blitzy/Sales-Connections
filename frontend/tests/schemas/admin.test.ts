/**
 * admin.test.ts - Vitest tests for frontend/src/schemas/admin.ts.
 *
 * Verifies all 6 Zod schemas + 1 constant array exported by admin.ts:
 *   - USER_ROLE_VALUES (readonly tuple ["Admin", "Contributor", "Viewer"])
 *   - UserReadSchema (NOT strict; extra fields silently dropped per
 *     forward-compat policy in admin.ts source spec)
 *   - UserRoleUpdateSchema (strict; rejects unknown keys per
 *     AAP Sec 0.7.4 anti-tampering posture)
 *   - ContributorActivitySchema (one row of most-active-contributors)
 *   - LeadsByStatusEntrySchema (status constrained by the 4 outreach
 *     status literals; count is non-negative integer)
 *   - WeeklyActivityEntrySchema (week_start regex /^\d{4}-\d{2}-\d{2}$/)
 *   - AnalyticsResponseSchema (composite; leads_by_status is LIST not
 *     OBJECT - mirrors backend pydantic source-of-truth)
 *
 * Per AAP Sec 0.7.7, Zod schemas mirror backend pydantic exactly.
 * Drift between client and server validation is a defect; this suite
 * pins the authoritative shapes so unintentional schema changes fail
 * CI immediately.
 *
 * NOTE: This is a PURE unit test - no React rendering, no MSW server,
 * no HTTP. Just Zod parse() / safeParse() on known-good and known-bad
 * payloads. The MSW server boot in tests/setup.ts and the jsdom
 * environment from vitest.config.ts are harmless overhead here.
 *
 * Conventions per frontend/.prettierrc.json:
 *   - Double quotes (singleQuote: false).
 *   - Trailing commas (trailingComma: "all").
 *   - 2-space indent; printWidth: 100.
 *   - import type for type-only imports per verbatimModuleSyntax: true.
 *   - Only import what is used (noUnusedLocals: true).
 */

import { describe, expect, it } from "vitest";

import {
  AnalyticsResponseSchema,
  ContributorActivitySchema,
  LeadsByStatusEntrySchema,
  USER_ROLE_VALUES,
  UserReadSchema,
  UserRoleUpdateSchema,
  WeeklyActivityEntrySchema,
} from "@/schemas/admin";
import type { UserRead } from "@/schemas/admin";

// ---------------------------------------------------------------------------
// USER_ROLE_VALUES constant (Phase 3)
// ---------------------------------------------------------------------------

describe("USER_ROLE_VALUES constant", () => {
  it("exposes the three roles in canonical order", () => {
    expect(USER_ROLE_VALUES).toEqual(["Admin", "Contributor", "Viewer"]);
  });

  it("has length 3", () => {
    expect(USER_ROLE_VALUES.length).toBe(3);
  });

  it("contains Admin role", () => {
    expect(USER_ROLE_VALUES).toContain("Admin");
  });

  it("contains Contributor role", () => {
    expect(USER_ROLE_VALUES).toContain("Contributor");
  });

  it("contains Viewer role", () => {
    expect(USER_ROLE_VALUES).toContain("Viewer");
  });
});

// ---------------------------------------------------------------------------
// UserReadSchema (Phase 4)
//
// Outbound shape returned by admin user endpoints. NOT .strict() per the
// source spec - extra fields (e.g. a future last_login_at, or a leaking
// password_hash that the backend should never serialize) are silently
// dropped to keep the SPA forward-compatible with benign backend
// additions. Tests pin both the validation rules AND the strip-don't-
// reject contract.
// ---------------------------------------------------------------------------

describe("UserReadSchema", () => {
  const validUser: UserRead = {
    id: "123e4567-e89b-12d3-a456-426614174000",
    email: "jane@example.com",
    display_name: "Jane Doe",
    role: "Admin",
    created_at: "2026-04-23T10:30:00+00:00",
  };

  it("accepts a valid user record", () => {
    expect(() => UserReadSchema.parse(validUser)).not.toThrow();
  });

  it("rejects a non-UUID id", () => {
    expect(() => UserReadSchema.parse({ ...validUser, id: "not-a-uuid" })).toThrow();
  });

  it("rejects a malformed email (no @ sign)", () => {
    expect(() => UserReadSchema.parse({ ...validUser, email: "no-at-sign" })).toThrow();
  });

  it("rejects an email exceeding 320 characters", () => {
    // RFC 5321 caps email length at 320 chars (local 64 + @ + domain 255).
    // 310 chars + "@example.com" (12 chars) = 322 chars total.
    const localPart = "a".repeat(310);
    const tooLongEmail = `${localPart}@example.com`;
    expect(tooLongEmail.length).toBeGreaterThan(320);
    expect(() => UserReadSchema.parse({ ...validUser, email: tooLongEmail })).toThrow();
  });

  it("rejects an empty display_name", () => {
    expect(() => UserReadSchema.parse({ ...validUser, display_name: "" })).toThrow();
  });

  it("rejects a display_name exceeding 255 characters", () => {
    expect(() =>
      UserReadSchema.parse({
        ...validUser,
        display_name: "a".repeat(256),
      }),
    ).toThrow();
  });

  it("rejects an unknown role value", () => {
    // Build an object that bypasses TypeScript's enum-narrowing so we can
    // verify Zod's runtime rejection. The test-files ESLint override
    // disables no-explicit-any, but we use a typed local instead of `any`
    // to keep intent explicit.
    const invalidUser = { ...validUser, role: "NotARole" };
    expect(() => UserReadSchema.parse(invalidUser)).toThrow();
  });

  it("accepts each of the three USER_ROLE_VALUES", () => {
    for (const role of USER_ROLE_VALUES) {
      expect(() => UserReadSchema.parse({ ...validUser, role })).not.toThrow();
    }
  });

  it("rejects malformed created_at (not an ISO 8601 datetime)", () => {
    expect(() => UserReadSchema.parse({ ...validUser, created_at: "not-a-datetime" })).toThrow();
  });

  it("rejects created_at without a timezone offset (offset:true is required)", () => {
    // The schema declares datetime({ offset: true }), so naive datetimes
    // (no Z, no +HH:MM) must be rejected. This pins the offset:true
    // contract against accidental relaxation.
    expect(() =>
      UserReadSchema.parse({ ...validUser, created_at: "2026-04-23T10:30:00" }),
    ).toThrow();
  });

  it("strips unknown fields like password_hash silently (NOT strict)", () => {
    // UserReadSchema does NOT use .strict() per the source spec, so it is
    // forward-compatible: extra fields are dropped, not rejected. This is
    // the inverse of UserRoleUpdateSchema's anti-tampering strict mode.
    const result = UserReadSchema.parse({
      ...validUser,
      password_hash: "$2b$12$abc...",
      org_id: "123e4567-e89b-12d3-a456-426614174999",
      deleted_at: null,
    });
    expect(result).not.toHaveProperty("password_hash");
    expect(result).not.toHaveProperty("org_id");
    expect(result).not.toHaveProperty("deleted_at");
  });

  it("preserves the five canonical fields after parse", () => {
    // Sanity-check that strip-on-parse does not also drop expected fields.
    const result = UserReadSchema.parse({ ...validUser, last_login_at: "ignored" });
    expect(result.id).toBe(validUser.id);
    expect(result.email).toBe(validUser.email);
    expect(result.display_name).toBe(validUser.display_name);
    expect(result.role).toBe(validUser.role);
    expect(result.created_at).toBe(validUser.created_at);
  });

  it("rejects when id is missing", () => {
    const { id: _id, ...withoutId } = validUser;
    expect(() => UserReadSchema.parse(withoutId)).toThrow();
  });

  it("rejects when email is missing", () => {
    const { email: _email, ...withoutEmail } = validUser;
    expect(() => UserReadSchema.parse(withoutEmail)).toThrow();
  });

  it("rejects when role is missing", () => {
    const { role: _role, ...withoutRole } = validUser;
    expect(() => UserReadSchema.parse(withoutRole)).toThrow();
  });
});

// ---------------------------------------------------------------------------
// UserRoleUpdateSchema (Phase 5)
//
// Inbound payload for PATCH /api/admin/users/:id role mutation.
// IS .strict() per AAP Sec 0.7.4 - the backend pydantic schema uses
// extra='forbid' to defend against role-escalation tampering (e.g. a
// malicious payload {"role":"Admin","email":"victim@x"}). Mirroring
// strict mode here surfaces such bugs in client-side dev tooling
// before the request even leaves the browser.
// ---------------------------------------------------------------------------

describe("UserRoleUpdateSchema (strict)", () => {
  it("accepts a valid role payload (Admin)", () => {
    expect(() => UserRoleUpdateSchema.parse({ role: "Admin" })).not.toThrow();
  });

  it("accepts a valid role payload (Contributor)", () => {
    expect(() => UserRoleUpdateSchema.parse({ role: "Contributor" })).not.toThrow();
  });

  it("accepts a valid role payload (Viewer)", () => {
    expect(() => UserRoleUpdateSchema.parse({ role: "Viewer" })).not.toThrow();
  });

  it("rejects a role value not in USER_ROLE_VALUES", () => {
    expect(() => UserRoleUpdateSchema.parse({ role: "SuperAdmin" })).toThrow();
  });

  it("rejects when role is missing (empty object)", () => {
    expect(() => UserRoleUpdateSchema.parse({})).toThrow();
  });

  it("rejects extra fields (strict mode)", () => {
    // This is the linchpin test for AAP Sec 0.7.4 anti-tampering. If the
    // schema ever accidentally drops .strict(), this test must fail loudly.
    expect(() => UserRoleUpdateSchema.parse({ role: "Admin", extra: "value" })).toThrow();
  });

  it("rejects role-escalation tampering payload (Admin + email override)", () => {
    expect(() =>
      UserRoleUpdateSchema.parse({
        role: "Admin",
        email: "victim@example.com",
      }),
    ).toThrow();
  });

  it("rejects role-escalation tampering payload (Admin + id override)", () => {
    expect(() =>
      UserRoleUpdateSchema.parse({
        role: "Admin",
        id: "00000000-0000-0000-0000-000000000000",
      }),
    ).toThrow();
  });

  it("rejects when role is null", () => {
    expect(() => UserRoleUpdateSchema.parse({ role: null })).toThrow();
  });

  it("rejects when role is a number", () => {
    expect(() => UserRoleUpdateSchema.parse({ role: 0 })).toThrow();
  });

  it("rejects when role has wrong case (admin vs Admin)", () => {
    // PostgreSQL enum types are case-sensitive; the Zod enum mirrors that.
    // A lowercase "admin" must be rejected to keep API deserialization
    // identical between client and server.
    expect(() => UserRoleUpdateSchema.parse({ role: "admin" })).toThrow();
  });
});

// ---------------------------------------------------------------------------
// ContributorActivitySchema (Phase 6)
//
// One row of the "most active contributors" analytics panel. Tests
// pin: UUID user_id, non-empty display_name (max 255), non-negative
// integer record_count.
// ---------------------------------------------------------------------------

describe("ContributorActivitySchema", () => {
  const validRow = {
    user_id: "123e4567-e89b-12d3-a456-426614174000",
    display_name: "Jane Doe",
    record_count: 42,
  };

  it("accepts a valid contributor row", () => {
    expect(() => ContributorActivitySchema.parse(validRow)).not.toThrow();
  });

  it("accepts record_count of zero (edge case for new contributors)", () => {
    expect(() => ContributorActivitySchema.parse({ ...validRow, record_count: 0 })).not.toThrow();
  });

  it("rejects negative record_count", () => {
    expect(() => ContributorActivitySchema.parse({ ...validRow, record_count: -1 })).toThrow();
  });

  it("rejects non-integer record_count (1.5)", () => {
    expect(() => ContributorActivitySchema.parse({ ...validRow, record_count: 1.5 })).toThrow();
  });

  it("rejects a non-UUID user_id", () => {
    expect(() => ContributorActivitySchema.parse({ ...validRow, user_id: "not-a-uuid" })).toThrow();
  });

  it("rejects empty display_name", () => {
    expect(() => ContributorActivitySchema.parse({ ...validRow, display_name: "" })).toThrow();
  });

  it("rejects display_name exceeding 255 characters", () => {
    expect(() =>
      ContributorActivitySchema.parse({
        ...validRow,
        display_name: "a".repeat(256),
      }),
    ).toThrow();
  });

  it("accepts display_name at exactly 255 characters (boundary)", () => {
    expect(() =>
      ContributorActivitySchema.parse({
        ...validRow,
        display_name: "a".repeat(255),
      }),
    ).not.toThrow();
  });

  it("rejects record_count as a string", () => {
    expect(() => ContributorActivitySchema.parse({ ...validRow, record_count: "42" })).toThrow();
  });

  it("rejects when user_id is missing", () => {
    const { user_id: _user_id, ...withoutUserId } = validRow;
    expect(() => ContributorActivitySchema.parse(withoutUserId)).toThrow();
  });

  it("rejects when display_name is missing", () => {
    const { display_name: _display_name, ...withoutDisplay } = validRow;
    expect(() => ContributorActivitySchema.parse(withoutDisplay)).toThrow();
  });

  it("rejects when record_count is missing", () => {
    const { record_count: _record_count, ...withoutCount } = validRow;
    expect(() => ContributorActivitySchema.parse(withoutCount)).toThrow();
  });
});

// ---------------------------------------------------------------------------
// LeadsByStatusEntrySchema (Phase 7)
//
// One row of the "leads by status" analytics panel. The status field is
// constrained by OUTREACH_STATUS_VALUES (re-used from connection.ts as
// the single source of truth). Tests use the four canonical values
// inline as `as const` literals to keep the import surface minimal per
// the file schema purpose statement.
// ---------------------------------------------------------------------------

describe("LeadsByStatusEntrySchema", () => {
  it("accepts each of the four OutreachStatus values", () => {
    const statuses = ["Not Started", "In Progress", "Contacted", "Closed"] as const;
    for (const status of statuses) {
      expect(() => LeadsByStatusEntrySchema.parse({ status, count: 5 })).not.toThrow();
    }
  });

  it("rejects a status value not in OUTREACH_STATUS_VALUES", () => {
    expect(() => LeadsByStatusEntrySchema.parse({ status: "Done", count: 5 })).toThrow();
  });

  it("rejects status with wrong case (closed vs Closed)", () => {
    // Mirrors backend PostgreSQL enum case-sensitivity.
    expect(() => LeadsByStatusEntrySchema.parse({ status: "closed", count: 5 })).toThrow();
  });

  it("rejects negative count", () => {
    expect(() => LeadsByStatusEntrySchema.parse({ status: "Closed", count: -3 })).toThrow();
  });

  it("accepts count of zero (edge case for empty status buckets)", () => {
    expect(() => LeadsByStatusEntrySchema.parse({ status: "Closed", count: 0 })).not.toThrow();
  });

  it("rejects non-integer count (1.5)", () => {
    expect(() => LeadsByStatusEntrySchema.parse({ status: "Closed", count: 1.5 })).toThrow();
  });

  it("rejects when status is missing", () => {
    expect(() => LeadsByStatusEntrySchema.parse({ count: 5 })).toThrow();
  });

  it("rejects when count is missing", () => {
    expect(() => LeadsByStatusEntrySchema.parse({ status: "Closed" })).toThrow();
  });

  it("rejects count as a string", () => {
    expect(() => LeadsByStatusEntrySchema.parse({ status: "Closed", count: "5" })).toThrow();
  });

  it("rejects empty object", () => {
    expect(() => LeadsByStatusEntrySchema.parse({})).toThrow();
  });
});

// ---------------------------------------------------------------------------
// WeeklyActivityEntrySchema (Phase 8)
//
// One row of the weekly-activity sparkline. The week_start field uses
// the regex /^\d{4}-\d{2}-\d{2}$/ per the source spec (NOT Zod's
// .date() method, for portability across Zod 3.x patch versions).
// Tests pin the regex's strict YYYY-MM-DD shape - rejecting unpadded,
// alternative-separator, and full-datetime inputs.
// ---------------------------------------------------------------------------

describe("WeeklyActivityEntrySchema", () => {
  it("accepts a valid YYYY-MM-DD week_start", () => {
    expect(() =>
      WeeklyActivityEntrySchema.parse({
        week_start: "2026-01-01",
        record_count: 10,
      }),
    ).not.toThrow();
  });

  it("rejects week_start in YYYY-M-D format (no zero padding)", () => {
    expect(() =>
      WeeklyActivityEntrySchema.parse({
        week_start: "2026-1-1",
        record_count: 10,
      }),
    ).toThrow();
  });

  it("rejects week_start in YYYY/MM/DD format (wrong separator)", () => {
    expect(() =>
      WeeklyActivityEntrySchema.parse({
        week_start: "2026/01/01",
        record_count: 10,
      }),
    ).toThrow();
  });

  it("rejects week_start that is a full ISO datetime", () => {
    // The regex anchors with ^...$ so the trailing T00:00:00Z must fail.
    expect(() =>
      WeeklyActivityEntrySchema.parse({
        week_start: "2026-01-01T00:00:00Z",
        record_count: 10,
      }),
    ).toThrow();
  });

  it("rejects an empty week_start", () => {
    expect(() =>
      WeeklyActivityEntrySchema.parse({
        week_start: "",
        record_count: 10,
      }),
    ).toThrow();
  });

  it("rejects week_start with extra leading whitespace", () => {
    expect(() =>
      WeeklyActivityEntrySchema.parse({
        week_start: " 2026-01-01",
        record_count: 10,
      }),
    ).toThrow();
  });

  it("rejects week_start with extra trailing whitespace", () => {
    expect(() =>
      WeeklyActivityEntrySchema.parse({
        week_start: "2026-01-01 ",
        record_count: 10,
      }),
    ).toThrow();
  });

  it("rejects negative record_count", () => {
    expect(() =>
      WeeklyActivityEntrySchema.parse({
        week_start: "2026-01-01",
        record_count: -1,
      }),
    ).toThrow();
  });

  it("accepts record_count of zero (weeks with no activity)", () => {
    expect(() =>
      WeeklyActivityEntrySchema.parse({
        week_start: "2026-01-01",
        record_count: 0,
      }),
    ).not.toThrow();
  });

  it("rejects non-integer record_count (3.7)", () => {
    expect(() =>
      WeeklyActivityEntrySchema.parse({
        week_start: "2026-01-01",
        record_count: 3.7,
      }),
    ).toThrow();
  });

  it("rejects when week_start is missing", () => {
    expect(() =>
      WeeklyActivityEntrySchema.parse({
        record_count: 10,
      }),
    ).toThrow();
  });

  it("rejects when record_count is missing", () => {
    expect(() =>
      WeeklyActivityEntrySchema.parse({
        week_start: "2026-01-01",
      }),
    ).toThrow();
  });
});

// ---------------------------------------------------------------------------
// AnalyticsResponseSchema (Phase 9)
//
// Composite response from GET /api/admin/analytics. CRITICAL CONTRACT:
// leads_by_status is a LIST/array of LeadsByStatusEntry, NOT an object
// keyed by status. The folder spec described it as a status->count
// dict; the backend pydantic source-of-truth uses list[
// LeadsByStatusEntry] which preserves Not Started -> In Progress ->
// Contacted -> Closed pipeline ordering and is more extensible. The
// "rejects when leads_by_status is an OBJECT (not array)" test is the
// regression guard against accidental schema drift.
// ---------------------------------------------------------------------------

describe("AnalyticsResponseSchema", () => {
  const validResponse = {
    most_active_contributors: [
      {
        user_id: "123e4567-e89b-12d3-a456-426614174000",
        display_name: "Jane",
        record_count: 5,
      },
    ],
    leads_by_status: [
      { status: "Not Started" as const, count: 1 },
      { status: "In Progress" as const, count: 2 },
      { status: "Contacted" as const, count: 3 },
      { status: "Closed" as const, count: 4 },
    ],
    weekly_activity: [
      { week_start: "2026-01-01", record_count: 0 },
      { week_start: "2026-01-08", record_count: 5 },
    ],
    generated_at: "2026-04-23T10:30:00+00:00",
  };

  it("accepts a fully-populated response", () => {
    expect(() => AnalyticsResponseSchema.parse(validResponse)).not.toThrow();
  });

  it("accepts empty panels (new org with no contributors, leads, or activity)", () => {
    expect(() =>
      AnalyticsResponseSchema.parse({
        most_active_contributors: [],
        leads_by_status: [],
        weekly_activity: [],
        generated_at: "2026-04-23T10:30:00+00:00",
      }),
    ).not.toThrow();
  });

  it("rejects when leads_by_status is an OBJECT (not array)", () => {
    // This is the regression guard for the folder-spec/backend
    // disagreement. Backend wins; the schema mirrors backend exactly.
    // If this test ever passes, the schema has drifted into the wrong
    // shape and admin endpoint deserialization will break.
    expect(() =>
      AnalyticsResponseSchema.parse({
        most_active_contributors: [],
        leads_by_status: {
          "Not Started": 1,
          "In Progress": 2,
          Contacted: 3,
          Closed: 4,
        },
        weekly_activity: [],
        generated_at: "2026-04-23T10:30:00+00:00",
      }),
    ).toThrow();
  });

  it("rejects when most_active_contributors is an OBJECT (not array)", () => {
    expect(() =>
      AnalyticsResponseSchema.parse({
        most_active_contributors: {
          "user-1": { display_name: "Jane", record_count: 5 },
        },
        leads_by_status: [],
        weekly_activity: [],
        generated_at: "2026-04-23T10:30:00+00:00",
      }),
    ).toThrow();
  });

  it("rejects when weekly_activity is an OBJECT (not array)", () => {
    expect(() =>
      AnalyticsResponseSchema.parse({
        most_active_contributors: [],
        leads_by_status: [],
        weekly_activity: {
          "2026-01-01": 0,
          "2026-01-08": 5,
        },
        generated_at: "2026-04-23T10:30:00+00:00",
      }),
    ).toThrow();
  });

  it("rejects when generated_at is missing", () => {
    expect(() =>
      AnalyticsResponseSchema.parse({
        most_active_contributors: [],
        leads_by_status: [],
        weekly_activity: [],
      }),
    ).toThrow();
  });

  it("rejects when generated_at is malformed", () => {
    expect(() =>
      AnalyticsResponseSchema.parse({
        most_active_contributors: [],
        leads_by_status: [],
        weekly_activity: [],
        generated_at: "not-a-datetime",
      }),
    ).toThrow();
  });

  it("rejects when generated_at lacks a timezone offset", () => {
    // datetime({ offset: true }) requires explicit Z or +HH:MM. A naive
    // "2026-04-23T10:30:00" must fail to keep timestamp semantics
    // unambiguous between client and server.
    expect(() =>
      AnalyticsResponseSchema.parse({
        most_active_contributors: [],
        leads_by_status: [],
        weekly_activity: [],
        generated_at: "2026-04-23T10:30:00",
      }),
    ).toThrow();
  });

  it("rejects when most_active_contributors is missing", () => {
    expect(() =>
      AnalyticsResponseSchema.parse({
        leads_by_status: [],
        weekly_activity: [],
        generated_at: "2026-04-23T10:30:00+00:00",
      }),
    ).toThrow();
  });

  it("rejects when leads_by_status is missing", () => {
    expect(() =>
      AnalyticsResponseSchema.parse({
        most_active_contributors: [],
        weekly_activity: [],
        generated_at: "2026-04-23T10:30:00+00:00",
      }),
    ).toThrow();
  });

  it("rejects when weekly_activity is missing", () => {
    expect(() =>
      AnalyticsResponseSchema.parse({
        most_active_contributors: [],
        leads_by_status: [],
        generated_at: "2026-04-23T10:30:00+00:00",
      }),
    ).toThrow();
  });

  it("rejects when a contributor row has invalid UUID", () => {
    expect(() =>
      AnalyticsResponseSchema.parse({
        most_active_contributors: [
          {
            user_id: "invalid-uuid",
            display_name: "Jane",
            record_count: 5,
          },
        ],
        leads_by_status: [],
        weekly_activity: [],
        generated_at: "2026-04-23T10:30:00+00:00",
      }),
    ).toThrow();
  });

  it("rejects when a contributor row has negative record_count", () => {
    expect(() =>
      AnalyticsResponseSchema.parse({
        most_active_contributors: [
          {
            user_id: "123e4567-e89b-12d3-a456-426614174000",
            display_name: "Jane",
            record_count: -1,
          },
        ],
        leads_by_status: [],
        weekly_activity: [],
        generated_at: "2026-04-23T10:30:00+00:00",
      }),
    ).toThrow();
  });

  it("rejects when a leads_by_status row has invalid status", () => {
    expect(() =>
      AnalyticsResponseSchema.parse({
        most_active_contributors: [],
        leads_by_status: [{ status: "Done", count: 1 }],
        weekly_activity: [],
        generated_at: "2026-04-23T10:30:00+00:00",
      }),
    ).toThrow();
  });

  it("rejects when a weekly_activity row has invalid week_start format", () => {
    expect(() =>
      AnalyticsResponseSchema.parse({
        most_active_contributors: [],
        leads_by_status: [],
        weekly_activity: [{ week_start: "2026-1-1", record_count: 0 }],
        generated_at: "2026-04-23T10:30:00+00:00",
      }),
    ).toThrow();
  });

  it("safeParse exposes structured error details on failure", () => {
    // Sanity-check that consumers using safeParse() (vs parse()) receive
    // a well-formed ZodError instead of an exception throw. This pins
    // the Zod public API contract that admin views rely on for inline
    // validation feedback.
    const result = AnalyticsResponseSchema.safeParse({
      most_active_contributors: [],
      leads_by_status: [{ status: "Done", count: 1 }],
      weekly_activity: [],
      generated_at: "2026-04-23T10:30:00+00:00",
    });
    expect(result.success).toBe(false);
    if (!result.success) {
      expect(result.error.issues.length).toBeGreaterThan(0);
    }
  });
});
