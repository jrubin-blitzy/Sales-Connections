/**
 * connection.test.ts - Vitest tests for frontend/src/schemas/connection.ts.
 *
 * The most extensive schema test suite. Covers:
 *   - 9 Zod schemas (TagRead, TagCreate, ConnectionCreate,
 *     ConnectionUpdate, ConnectionStatusUpdate, ConnectionRead,
 *     PaginatedConnections, ConnectionDuplicateCheckResponse,
 *     ConnectionHistoryEntry)
 *   - 3 constants (INVOLVEMENT_VALUES, OUTREACH_STATUS_VALUES,
 *     AUDIT_EVENT_TYPE_VALUES)
 *   - LinkedIn URL validation (tested indirectly via
 *     ConnectionCreateSchema since the helper is not exported)
 *
 * CRITICAL CONTRACTS PINNED BY THIS FILE:
 *   1. Length caps mirror BACKEND not folder spec:
 *      - full_name, company, job_title: max 255 (NOT 120)
 *      - relationship_context: max 4000
 *      - ai_notes: max 8000
 *      - linkedin_url: max 2048
 *   2. ConnectionCreateSchema does NOT include outreach_status
 *      (server defaults to "Not Started" per F-005).
 *   3. ConnectionCreateSchema does NOT accept owner_user_id or
 *      owner_display_name (per AAP Sec 0.7.4 - derived from session).
 *   4. ConnectionUpdateSchema does NOT include outreach_status
 *      (handled by dedicated PATCH /:id/status endpoint per F-005).
 *   5. All inbound schemas are .strict() (mirror backend extra="forbid").
 *   6. Outbound schemas (Read, Paginated, etc.) are NOT .strict()
 *      (forward-compatible for backend additions).
 *   7. AUDIT_EVENT_TYPE_VALUES has all 8 event types per F-013.
 *
 * Per AAP Sec 0.7.7, Zod schemas mirror backend pydantic exactly.
 *
 * NOTE: This is a PURE unit test - no React rendering, no MSW,
 * no HTTP. Just Zod parse() / safeParse() on known payloads.
 *
 * Conventions per frontend/.prettierrc.json:
 *   - Double quotes (singleQuote: false).
 *   - Trailing commas (trailingComma: "all").
 *   - 2-space indent; line length <= 100.
 *   - import type for type-only imports per verbatimModuleSyntax: true.
 *   - Underscore prefix on destructured-and-ignored variables to satisfy
 *     the eslint @typescript-eslint/no-unused-vars argsIgnorePattern.
 */

import { describe, expect, it } from "vitest";

import {
  AUDIT_EVENT_TYPE_VALUES,
  ConnectionCreateSchema,
  ConnectionDuplicateCheckResponseSchema,
  ConnectionHistoryEntrySchema,
  ConnectionReadSchema,
  ConnectionStatusUpdateSchema,
  ConnectionUpdateSchema,
  INVOLVEMENT_VALUES,
  OUTREACH_STATUS_VALUES,
  PaginatedConnectionsSchema,
  TagCreateSchema,
  TagReadSchema,
} from "@/schemas/connection";
import type { ConnectionCreate, ConnectionRead } from "@/schemas/connection";

// ---------------------------------------------------------------------------
// Constants tests (Phase 3)
// ---------------------------------------------------------------------------

describe("INVOLVEMENT_VALUES constant", () => {
  it("exposes the three values in canonical order", () => {
    expect(INVOLVEMENT_VALUES).toEqual(["Warm Intro", "Soft Reference", "Target Only"]);
  });

  it("has length 3", () => {
    expect(INVOLVEMENT_VALUES.length).toBe(3);
  });

  it("contains Warm Intro", () => {
    expect(INVOLVEMENT_VALUES).toContain("Warm Intro");
  });

  it("contains Soft Reference", () => {
    expect(INVOLVEMENT_VALUES).toContain("Soft Reference");
  });

  it("contains Target Only", () => {
    expect(INVOLVEMENT_VALUES).toContain("Target Only");
  });
});

describe("OUTREACH_STATUS_VALUES constant", () => {
  it("exposes the four values in canonical order", () => {
    expect(OUTREACH_STATUS_VALUES).toEqual(["Not Started", "In Progress", "Contacted", "Closed"]);
  });

  it("has length 4", () => {
    expect(OUTREACH_STATUS_VALUES.length).toBe(4);
  });

  it("contains Not Started", () => {
    expect(OUTREACH_STATUS_VALUES).toContain("Not Started");
  });

  it("contains In Progress", () => {
    expect(OUTREACH_STATUS_VALUES).toContain("In Progress");
  });

  it("contains Contacted", () => {
    expect(OUTREACH_STATUS_VALUES).toContain("Contacted");
  });

  it("contains Closed", () => {
    expect(OUTREACH_STATUS_VALUES).toContain("Closed");
  });
});

describe("AUDIT_EVENT_TYPE_VALUES constant (8 event types per F-013)", () => {
  it("exposes the eight event types in canonical order", () => {
    expect(AUDIT_EVENT_TYPE_VALUES).toEqual([
      "create",
      "status_change",
      "edit",
      "soft_delete",
      "hard_delete",
      "role_change",
      "authentication",
      "admin_op",
    ]);
  });

  it("has length 8", () => {
    expect(AUDIT_EVENT_TYPE_VALUES.length).toBe(8);
  });

  it("contains all eight expected event types", () => {
    expect(AUDIT_EVENT_TYPE_VALUES).toContain("create");
    expect(AUDIT_EVENT_TYPE_VALUES).toContain("status_change");
    expect(AUDIT_EVENT_TYPE_VALUES).toContain("edit");
    expect(AUDIT_EVENT_TYPE_VALUES).toContain("soft_delete");
    expect(AUDIT_EVENT_TYPE_VALUES).toContain("hard_delete");
    expect(AUDIT_EVENT_TYPE_VALUES).toContain("role_change");
    expect(AUDIT_EVENT_TYPE_VALUES).toContain("authentication");
    expect(AUDIT_EVENT_TYPE_VALUES).toContain("admin_op");
  });
});

// ---------------------------------------------------------------------------
// LinkedIn URL validation tests (Phase 4)
// ---------------------------------------------------------------------------
// The isValidLinkedInUrl helper is NOT exported per the source agent's spec
// (it is a private helper). Instead we test through ConnectionCreateSchema
// which uses .refine(isValidLinkedInUrl) on its linkedin_url field. This is
// robust to refactors of the helper as long as the user-facing accept/reject
// behavior is preserved.
//
// IMPORTANT EDGE CASE NOTE: A URL containing literal spaces (e.g. "with
// spaces") is silently percent-encoded by the WHATWG URL constructor in
// Node 20+ and modern browsers, producing "with%20spaces" which matches
// the slug character class /^[A-Za-z0-9._\-%]+$/. We therefore test slug
// rejection through a character that is NOT percent-encoded by URL parsing
// AND is NOT in the slug class, such as "@" (which both Node and browsers
// preserve verbatim in the path). This keeps the test honest: it verifies
// the slug pattern, not the URL constructor's encoding behavior.
// ---------------------------------------------------------------------------

describe("LinkedIn URL validation (via ConnectionCreateSchema.linkedin_url)", () => {
  // Build a valid base payload; tests vary only the linkedin_url field.
  const validBase: ConnectionCreate = {
    submitted_by: "Test User",
    full_name: "Jane Doe",
    linkedin_url: "https://www.linkedin.com/in/jane-doe",
    company: "Acme",
    job_title: "VP Operations",
    relationship_context: "College roommate.",
    involvement: "Warm Intro",
    tag_ids: [],
  };

  function tryUrl(url: string): boolean {
    return ConnectionCreateSchema.safeParse({
      ...validBase,
      linkedin_url: url,
    }).success;
  }

  describe("valid LinkedIn URLs", () => {
    it("accepts https://www.linkedin.com/in/jane-doe", () => {
      expect(tryUrl("https://www.linkedin.com/in/jane-doe")).toBe(true);
    });

    it("accepts https://linkedin.com/in/jane-doe (no www)", () => {
      expect(tryUrl("https://linkedin.com/in/jane-doe")).toBe(true);
    });

    it("accepts https://linkedin.com/in/jane-doe/ (trailing slash)", () => {
      expect(tryUrl("https://linkedin.com/in/jane-doe/")).toBe(true);
    });

    it("accepts http://linkedin.com/in/jane-doe (HTTP not HTTPS)", () => {
      expect(tryUrl("http://linkedin.com/in/jane-doe")).toBe(true);
    });

    it("accepts slug with numbers (jane123)", () => {
      expect(tryUrl("https://www.linkedin.com/in/jane123")).toBe(true);
    });

    it("accepts slug with underscores (jane_doe)", () => {
      expect(tryUrl("https://linkedin.com/in/jane_doe")).toBe(true);
    });

    it("accepts slug with dashes (jane-doe)", () => {
      expect(tryUrl("https://linkedin.com/in/jane-doe")).toBe(true);
    });

    it("accepts slug with dots (jane.doe)", () => {
      expect(tryUrl("https://linkedin.com/in/jane.doe")).toBe(true);
    });

    it("accepts slug with percent-encoded chars (Some%20Slug)", () => {
      expect(tryUrl("https://www.linkedin.com/in/Some%20Slug")).toBe(true);
    });

    it("accepts /pub/ legacy URLs (https://linkedin.com/pub/jane-doe)", () => {
      expect(tryUrl("https://linkedin.com/pub/jane-doe")).toBe(true);
    });

    it("accepts URLs with query parameters (utm tracking)", () => {
      expect(tryUrl("https://www.linkedin.com/in/jane-doe?utm_source=email")).toBe(true);
    });

    it("accepts URLs with trailing path segments (/in/jane-doe/details)", () => {
      expect(tryUrl("https://linkedin.com/in/jane-doe/details")).toBe(true);
    });

    it("accepts country subdomain (https://uk.linkedin.com/in/jane-doe)", () => {
      expect(tryUrl("https://uk.linkedin.com/in/jane-doe")).toBe(true);
    });
  });

  describe("invalid LinkedIn URLs", () => {
    it("rejects wrong host (https://example.com/in/jane)", () => {
      expect(tryUrl("https://example.com/in/jane")).toBe(false);
    });

    it("rejects no profile slug (https://linkedin.com/in)", () => {
      expect(tryUrl("https://linkedin.com/in")).toBe(false);
    });

    it("rejects empty slug (https://linkedin.com/in/)", () => {
      expect(tryUrl("https://linkedin.com/in/")).toBe(false);
    });

    it("rejects wrong path /jobs (https://linkedin.com/jobs/jane)", () => {
      expect(tryUrl("https://linkedin.com/jobs/jane")).toBe(false);
    });

    it("rejects wrong path /company (https://linkedin.com/company/acme)", () => {
      expect(tryUrl("https://linkedin.com/company/acme")).toBe(false);
    });

    it("rejects ftp protocol (ftp://linkedin.com/in/jane)", () => {
      expect(tryUrl("ftp://linkedin.com/in/jane")).toBe(false);
    });

    it("rejects mailto protocol (mailto:jane@linkedin.com)", () => {
      expect(tryUrl("mailto:jane@linkedin.com")).toBe(false);
    });

    it("rejects plain text (not-a-url)", () => {
      expect(tryUrl("not-a-url")).toBe(false);
    });

    it("rejects slug with at-sign / non-allowed char (jane@doe)", () => {
      // The "@" character is preserved verbatim by the URL parser AND is not
      // in the slug character class, so it is the cleanest way to verify
      // slug-pattern enforcement. (A literal space is silently encoded to
      // "%20" which IS in the slug class, so spaces test the URL encoder
      // rather than the slug pattern.)
      expect(tryUrl("https://linkedin.com/in/jane@doe")).toBe(false);
    });

    it("rejects slug with plus sign (jane+doe)", () => {
      expect(tryUrl("https://linkedin.com/in/jane+doe")).toBe(false);
    });

    it("rejects empty string", () => {
      expect(tryUrl("")).toBe(false);
    });

    it("rejects subdomain not in allowlist (https://us.linkedin.com/in/jane)", () => {
      // The allowlist contains www, m, uk, ca, au, etc. but NOT "us".
      expect(tryUrl("https://us.linkedin.com/in/jane")).toBe(false);
    });

    it("rejects multi-level subdomain spoof (attacker.com.linkedin.com)", () => {
      expect(tryUrl("https://attacker.com.linkedin.com/in/jane")).toBe(false);
    });

    it("rejects linkedin.cn (China-specific TLD)", () => {
      expect(tryUrl("https://www.linkedin.cn/in/jane")).toBe(false);
    });
  });
});

// ---------------------------------------------------------------------------
// TagReadSchema tests (Phase 5)
// ---------------------------------------------------------------------------

describe("TagReadSchema", () => {
  const validTag = {
    id: "123e4567-e89b-12d3-a456-426614174000",
    name: "sales",
    created_at: "2026-04-23T10:30:00+00:00",
  };

  it("accepts a valid tag", () => {
    expect(() => TagReadSchema.parse(validTag)).not.toThrow();
  });

  it("rejects an empty name", () => {
    expect(() => TagReadSchema.parse({ ...validTag, name: "" })).toThrow();
  });

  it("rejects a name exceeding 64 characters", () => {
    expect(() => TagReadSchema.parse({ ...validTag, name: "x".repeat(65) })).toThrow();
  });

  it("accepts a name of exactly 64 characters", () => {
    expect(() => TagReadSchema.parse({ ...validTag, name: "x".repeat(64) })).not.toThrow();
  });

  it("rejects a non-UUID id", () => {
    expect(() => TagReadSchema.parse({ ...validTag, id: "not-a-uuid" })).toThrow();
  });

  it("rejects malformed created_at", () => {
    expect(() => TagReadSchema.parse({ ...validTag, created_at: "not-a-date" })).toThrow();
  });

  it("rejects when name field is missing", () => {
    const { name: _omitted, ...withoutName } = validTag;
    expect(() => TagReadSchema.parse(withoutName)).toThrow();
  });

  it("rejects when id field is missing", () => {
    const { id: _omitted, ...withoutId } = validTag;
    expect(() => TagReadSchema.parse(withoutId)).toThrow();
  });

  it("rejects when created_at field is missing", () => {
    const { created_at: _omitted, ...withoutCreatedAt } = validTag;
    expect(() => TagReadSchema.parse(withoutCreatedAt)).toThrow();
  });
});

// ---------------------------------------------------------------------------
// TagCreateSchema tests (Phase 6) - .strict()
// ---------------------------------------------------------------------------

describe("TagCreateSchema (strict)", () => {
  it("accepts a valid name", () => {
    expect(() => TagCreateSchema.parse({ name: "sales" })).not.toThrow();
  });

  it("rejects an empty name", () => {
    expect(() => TagCreateSchema.parse({ name: "" })).toThrow();
  });

  it("rejects a name exceeding 64 characters", () => {
    expect(() => TagCreateSchema.parse({ name: "x".repeat(65) })).toThrow();
  });

  it("accepts a name of exactly 64 characters", () => {
    expect(() => TagCreateSchema.parse({ name: "x".repeat(64) })).not.toThrow();
  });

  it("rejects extra fields (strict mode)", () => {
    expect(() => TagCreateSchema.parse({ name: "sales", extra: "x" })).toThrow();
  });

  it("rejects when name field is missing", () => {
    expect(() => TagCreateSchema.parse({})).toThrow();
  });

  it("rejects when name is not a string", () => {
    expect(() => TagCreateSchema.parse({ name: 42 })).toThrow();
  });
});

// ---------------------------------------------------------------------------
// ConnectionCreateSchema tests (Phase 7) - .strict()
// ---------------------------------------------------------------------------
// The most extensively tested schema in this file. Verifies:
//   - Valid payload acceptance
//   - Strict mode (rejects extra fields, owner fields, outreach_status, id,
//     submission_date)
//   - Length caps (mirror BACKEND values: 255 / 4000 / 8000 / 2048)
//   - Enum validation for involvement
//   - tag_ids array of UUID validation and the [] default
// ---------------------------------------------------------------------------

describe("ConnectionCreateSchema", () => {
  const validPayload: ConnectionCreate = {
    submitted_by: "Test User",
    full_name: "Jane Doe",
    linkedin_url: "https://www.linkedin.com/in/jane-doe",
    company: "Acme Logistics",
    job_title: "VP Operations",
    relationship_context:
      "We went to college together; she is now VP of Ops at a Series B logistics startup.",
    involvement: "Warm Intro",
    tag_ids: [],
  };

  describe("valid payloads", () => {
    it("accepts a fully-valid payload", () => {
      expect(() => ConnectionCreateSchema.parse(validPayload)).not.toThrow();
    });

    it("accepts ai_notes when provided", () => {
      expect(() =>
        ConnectionCreateSchema.parse({
          ...validPayload,
          ai_notes: "Mention the logistics market expansion and her ops focus.",
        }),
      ).not.toThrow();
    });

    it("accepts a payload without ai_notes (optional field)", () => {
      const result = ConnectionCreateSchema.parse(validPayload);
      expect(result.ai_notes).toBeUndefined();
    });

    it("defaults tag_ids to empty array when omitted", () => {
      const { tag_ids: _omitted, ...withoutTags } = validPayload;
      const result = ConnectionCreateSchema.parse(withoutTags);
      expect(result.tag_ids).toEqual([]);
    });

    it("accepts a non-empty tag_ids array", () => {
      const result = ConnectionCreateSchema.parse({
        ...validPayload,
        tag_ids: ["123e4567-e89b-12d3-a456-426614174000"],
      });
      expect(result.tag_ids).toHaveLength(1);
    });

    it("preserves field values verbatim through parse", () => {
      const result = ConnectionCreateSchema.parse(validPayload);
      expect(result.full_name).toBe("Jane Doe");
      expect(result.company).toBe("Acme Logistics");
      expect(result.involvement).toBe("Warm Intro");
    });
  });

  describe("strict mode (rejects unknown keys)", () => {
    it("rejects unknown extra_field", () => {
      expect(() =>
        ConnectionCreateSchema.parse({
          ...validPayload,
          extra_field: "foo",
        }),
      ).toThrow();
    });

    it("rejects owner_user_id (per AAP Sec 0.7.4 - derived from session)", () => {
      expect(() =>
        ConnectionCreateSchema.parse({
          ...validPayload,
          owner_user_id: "123e4567-e89b-12d3-a456-426614174000",
        }),
      ).toThrow();
    });

    it("rejects owner_display_name (per AAP Sec 0.7.4)", () => {
      expect(() =>
        ConnectionCreateSchema.parse({
          ...validPayload,
          owner_display_name: "Mallory",
        }),
      ).toThrow();
    });

    it("rejects outreach_status (server defaults to Not Started per F-005)", () => {
      expect(() =>
        ConnectionCreateSchema.parse({
          ...validPayload,
          outreach_status: "Closed",
        }),
      ).toThrow();
    });

    it("rejects id (server-generated)", () => {
      expect(() =>
        ConnectionCreateSchema.parse({
          ...validPayload,
          id: "123e4567-e89b-12d3-a456-426614174000",
        }),
      ).toThrow();
    });

    it("rejects submission_date (server-set)", () => {
      expect(() =>
        ConnectionCreateSchema.parse({
          ...validPayload,
          submission_date: "2026-04-23T10:30:00+00:00",
        }),
      ).toThrow();
    });

    it("rejects normalized_linkedin_url (server-derived)", () => {
      expect(() =>
        ConnectionCreateSchema.parse({
          ...validPayload,
          normalized_linkedin_url: "linkedin.com/in/jane-doe",
        }),
      ).toThrow();
    });

    it("rejects deleted_at (server-managed)", () => {
      expect(() =>
        ConnectionCreateSchema.parse({
          ...validPayload,
          deleted_at: null,
        }),
      ).toThrow();
    });
  });

  describe("length caps (mirror backend max values)", () => {
    it("rejects full_name exceeding 255 characters", () => {
      expect(() =>
        ConnectionCreateSchema.parse({
          ...validPayload,
          full_name: "a".repeat(256),
        }),
      ).toThrow();
    });

    it("accepts full_name of exactly 255 characters", () => {
      expect(() =>
        ConnectionCreateSchema.parse({
          ...validPayload,
          full_name: "a".repeat(255),
        }),
      ).not.toThrow();
    });

    it("rejects empty full_name", () => {
      expect(() => ConnectionCreateSchema.parse({ ...validPayload, full_name: "" })).toThrow();
    });

    it("rejects company exceeding 255 characters", () => {
      expect(() =>
        ConnectionCreateSchema.parse({
          ...validPayload,
          company: "a".repeat(256),
        }),
      ).toThrow();
    });

    it("accepts company of exactly 255 characters", () => {
      expect(() =>
        ConnectionCreateSchema.parse({
          ...validPayload,
          company: "a".repeat(255),
        }),
      ).not.toThrow();
    });

    it("rejects empty company", () => {
      expect(() => ConnectionCreateSchema.parse({ ...validPayload, company: "" })).toThrow();
    });

    it("rejects job_title exceeding 255 characters", () => {
      expect(() =>
        ConnectionCreateSchema.parse({
          ...validPayload,
          job_title: "a".repeat(256),
        }),
      ).toThrow();
    });

    it("accepts job_title of exactly 255 characters", () => {
      expect(() =>
        ConnectionCreateSchema.parse({
          ...validPayload,
          job_title: "a".repeat(255),
        }),
      ).not.toThrow();
    });

    it("rejects empty job_title", () => {
      expect(() => ConnectionCreateSchema.parse({ ...validPayload, job_title: "" })).toThrow();
    });

    it("rejects linkedin_url exceeding 2048 characters", () => {
      // Construct a URL that would parse but exceeds 2048 chars total.
      const longSuffix = "a".repeat(2050);
      expect(() =>
        ConnectionCreateSchema.parse({
          ...validPayload,
          linkedin_url: `https://linkedin.com/in/${longSuffix}`,
        }),
      ).toThrow();
    });

    it("rejects relationship_context exceeding 4000 characters", () => {
      expect(() =>
        ConnectionCreateSchema.parse({
          ...validPayload,
          relationship_context: "a".repeat(4001),
        }),
      ).toThrow();
    });

    it("accepts relationship_context of exactly 4000 characters", () => {
      expect(() =>
        ConnectionCreateSchema.parse({
          ...validPayload,
          relationship_context: "a".repeat(4000),
        }),
      ).not.toThrow();
    });

    it("rejects empty relationship_context", () => {
      expect(() =>
        ConnectionCreateSchema.parse({
          ...validPayload,
          relationship_context: "",
        }),
      ).toThrow();
    });

    it("rejects ai_notes exceeding 8000 characters", () => {
      expect(() =>
        ConnectionCreateSchema.parse({
          ...validPayload,
          ai_notes: "a".repeat(8001),
        }),
      ).toThrow();
    });

    it("accepts ai_notes of exactly 8000 characters", () => {
      expect(() =>
        ConnectionCreateSchema.parse({
          ...validPayload,
          ai_notes: "a".repeat(8000),
        }),
      ).not.toThrow();
    });
  });

  describe("enum validation", () => {
    it("accepts each of the three INVOLVEMENT_VALUES", () => {
      for (const involvement of INVOLVEMENT_VALUES) {
        expect(() => ConnectionCreateSchema.parse({ ...validPayload, involvement })).not.toThrow();
      }
    });

    it("rejects an unknown involvement value", () => {
      expect(() =>
        ConnectionCreateSchema.parse({
          ...validPayload,
          involvement: "Cold Outreach",
        }),
      ).toThrow();
    });

    it("rejects when involvement is missing", () => {
      const { involvement: _omitted, ...withoutInvolvement } = validPayload;
      expect(() => ConnectionCreateSchema.parse(withoutInvolvement)).toThrow();
    });

    it("rejects when involvement is null", () => {
      expect(() => ConnectionCreateSchema.parse({ ...validPayload, involvement: null })).toThrow();
    });
  });

  describe("tag_ids validation", () => {
    it("rejects non-UUID tag_ids", () => {
      expect(() =>
        ConnectionCreateSchema.parse({
          ...validPayload,
          tag_ids: ["not-a-uuid"],
        }),
      ).toThrow();
    });

    it("accepts an array of valid UUIDs", () => {
      expect(() =>
        ConnectionCreateSchema.parse({
          ...validPayload,
          tag_ids: ["123e4567-e89b-12d3-a456-426614174000", "223e4567-e89b-12d3-a456-426614174000"],
        }),
      ).not.toThrow();
    });

    it("rejects tag_ids that is not an array", () => {
      expect(() =>
        ConnectionCreateSchema.parse({
          ...validPayload,
          tag_ids: "123e4567-e89b-12d3-a456-426614174000",
        }),
      ).toThrow();
    });

    it("rejects tag_ids array with one invalid UUID among valid ones", () => {
      expect(() =>
        ConnectionCreateSchema.parse({
          ...validPayload,
          tag_ids: ["123e4567-e89b-12d3-a456-426614174000", "not-a-uuid"],
        }),
      ).toThrow();
    });
  });

  describe("required fields", () => {
    it("rejects when full_name is missing", () => {
      const { full_name: _omitted, ...withoutFullName } = validPayload;
      expect(() => ConnectionCreateSchema.parse(withoutFullName)).toThrow();
    });

    it("rejects when linkedin_url is missing", () => {
      const { linkedin_url: _omitted, ...withoutUrl } = validPayload;
      expect(() => ConnectionCreateSchema.parse(withoutUrl)).toThrow();
    });

    it("rejects when company is missing", () => {
      const { company: _omitted, ...withoutCompany } = validPayload;
      expect(() => ConnectionCreateSchema.parse(withoutCompany)).toThrow();
    });

    it("rejects when job_title is missing", () => {
      const { job_title: _omitted, ...withoutJobTitle } = validPayload;
      expect(() => ConnectionCreateSchema.parse(withoutJobTitle)).toThrow();
    });

    it("rejects when relationship_context is missing", () => {
      const { relationship_context: _omitted, ...withoutContext } = validPayload;
      expect(() => ConnectionCreateSchema.parse(withoutContext)).toThrow();
    });
  });
});

// ---------------------------------------------------------------------------
// ConnectionUpdateSchema tests (Phase 8) - .strict(), all-optional
// ---------------------------------------------------------------------------
// All fields optional. EXCLUDES outreach_status (handled by dedicated PATCH
// /:id/status endpoint per F-005). Strict mode rejects extras.
// ---------------------------------------------------------------------------

describe("ConnectionUpdateSchema (strict, all-optional)", () => {
  it("accepts an empty payload (all fields optional)", () => {
    expect(() => ConnectionUpdateSchema.parse({})).not.toThrow();
  });

  it("accepts a single-field payload (full_name)", () => {
    expect(() => ConnectionUpdateSchema.parse({ full_name: "New Name" })).not.toThrow();
  });

  it("accepts a single-field payload (involvement)", () => {
    expect(() => ConnectionUpdateSchema.parse({ involvement: "Soft Reference" })).not.toThrow();
  });

  it("accepts multiple optional fields", () => {
    expect(() =>
      ConnectionUpdateSchema.parse({
        full_name: "Updated Name",
        company: "New Co",
        involvement: "Soft Reference",
      }),
    ).not.toThrow();
  });

  it("accepts a valid LinkedIn URL when provided", () => {
    expect(() =>
      ConnectionUpdateSchema.parse({
        linkedin_url: "https://www.linkedin.com/in/jane-doe",
      }),
    ).not.toThrow();
  });

  it("accepts ai_notes when provided", () => {
    expect(() =>
      ConnectionUpdateSchema.parse({
        ai_notes: "Updated notes for the connection.",
      }),
    ).not.toThrow();
  });

  it("rejects extra fields (strict)", () => {
    expect(() => ConnectionUpdateSchema.parse({ extra_field: "foo" })).toThrow();
  });

  it("rejects outreach_status (must use dedicated status endpoint per F-005)", () => {
    expect(() => ConnectionUpdateSchema.parse({ outreach_status: "Contacted" })).toThrow();
  });

  it("rejects owner_user_id (per AAP Sec 0.7.4)", () => {
    expect(() =>
      ConnectionUpdateSchema.parse({
        owner_user_id: "123e4567-e89b-12d3-a456-426614174000",
      }),
    ).toThrow();
  });

  it("rejects owner_display_name (per AAP Sec 0.7.4)", () => {
    expect(() => ConnectionUpdateSchema.parse({ owner_display_name: "Mallory" })).toThrow();
  });

  it("rejects id (server-generated)", () => {
    expect(() =>
      ConnectionUpdateSchema.parse({
        id: "123e4567-e89b-12d3-a456-426614174000",
      }),
    ).toThrow();
  });

  it("still validates length cap when full_name is present", () => {
    expect(() => ConnectionUpdateSchema.parse({ full_name: "a".repeat(256) })).toThrow();
  });

  it("rejects empty full_name when present", () => {
    expect(() => ConnectionUpdateSchema.parse({ full_name: "" })).toThrow();
  });

  it("still validates LinkedIn URL refinement when present", () => {
    expect(() =>
      ConnectionUpdateSchema.parse({
        linkedin_url: "https://example.com/in/jane",
      }),
    ).toThrow();
  });

  it("still validates LinkedIn URL length cap when present", () => {
    expect(() =>
      ConnectionUpdateSchema.parse({
        linkedin_url: `https://linkedin.com/in/${"a".repeat(2050)}`,
      }),
    ).toThrow();
  });

  it("still validates relationship_context length cap when present", () => {
    expect(() =>
      ConnectionUpdateSchema.parse({
        relationship_context: "a".repeat(4001),
      }),
    ).toThrow();
  });

  it("still validates ai_notes length cap when present", () => {
    expect(() => ConnectionUpdateSchema.parse({ ai_notes: "a".repeat(8001) })).toThrow();
  });

  it("still validates involvement enum when present", () => {
    expect(() => ConnectionUpdateSchema.parse({ involvement: "Cold Outreach" })).toThrow();
  });

  it("accepts tag_ids as empty array (replaces tags with empty)", () => {
    expect(() => ConnectionUpdateSchema.parse({ tag_ids: [] })).not.toThrow();
  });

  it("accepts tag_ids as array of valid UUIDs", () => {
    expect(() =>
      ConnectionUpdateSchema.parse({
        tag_ids: ["123e4567-e89b-12d3-a456-426614174000"],
      }),
    ).not.toThrow();
  });

  it("rejects tag_ids array with non-UUID member", () => {
    expect(() => ConnectionUpdateSchema.parse({ tag_ids: ["not-a-uuid"] })).toThrow();
  });
});

// ---------------------------------------------------------------------------
// ConnectionStatusUpdateSchema tests (Phase 9) - .strict(), single field
// ---------------------------------------------------------------------------

describe("ConnectionStatusUpdateSchema (strict)", () => {
  it("accepts each of the four OUTREACH_STATUS_VALUES", () => {
    for (const outreach_status of OUTREACH_STATUS_VALUES) {
      expect(() => ConnectionStatusUpdateSchema.parse({ outreach_status })).not.toThrow();
    }
  });

  it("accepts Not Started", () => {
    expect(() =>
      ConnectionStatusUpdateSchema.parse({ outreach_status: "Not Started" }),
    ).not.toThrow();
  });

  it("accepts In Progress", () => {
    expect(() =>
      ConnectionStatusUpdateSchema.parse({ outreach_status: "In Progress" }),
    ).not.toThrow();
  });

  it("accepts Contacted", () => {
    expect(() =>
      ConnectionStatusUpdateSchema.parse({ outreach_status: "Contacted" }),
    ).not.toThrow();
  });

  it("accepts Closed", () => {
    expect(() => ConnectionStatusUpdateSchema.parse({ outreach_status: "Closed" })).not.toThrow();
  });

  it("rejects an unknown status (Done)", () => {
    expect(() => ConnectionStatusUpdateSchema.parse({ outreach_status: "Done" })).toThrow();
  });

  it("rejects extra fields (strict)", () => {
    expect(() =>
      ConnectionStatusUpdateSchema.parse({
        outreach_status: "Closed",
        extra: "foo",
      }),
    ).toThrow();
  });

  it("rejects empty payload (outreach_status required)", () => {
    expect(() => ConnectionStatusUpdateSchema.parse({})).toThrow();
  });

  it("rejects null status", () => {
    expect(() => ConnectionStatusUpdateSchema.parse({ outreach_status: null })).toThrow();
  });

  it("rejects numeric status", () => {
    expect(() => ConnectionStatusUpdateSchema.parse({ outreach_status: 42 })).toThrow();
  });

  it("rejects empty-string status", () => {
    expect(() => ConnectionStatusUpdateSchema.parse({ outreach_status: "" })).toThrow();
  });
});

// ---------------------------------------------------------------------------
// ConnectionReadSchema tests (Phase 10) - outbound, NOT strict
// ---------------------------------------------------------------------------

describe("ConnectionReadSchema", () => {
  const validRecord: ConnectionRead = {
    id: "123e4567-e89b-12d3-a456-426614174000",
    full_name: "Jane Doe",
    linkedin_url: "https://www.linkedin.com/in/jane-doe",
    normalized_linkedin_url: "linkedin.com/in/jane-doe",
    company: "Acme",
    job_title: "VP Ops",
    relationship_context: "College.",
    ai_notes: "Mention logistics.",
    involvement: "Warm Intro",
    outreach_status: "Not Started",
    submission_date: "2026-04-23T10:30:00+00:00",
    owner_user_id: "223e4567-e89b-12d3-a456-426614174000",
    owner_display_name: "Submitter Name",
    tags: [],
    created_at: "2026-04-23T10:30:00+00:00",
    updated_at: "2026-04-23T10:30:00+00:00",
    deleted_at: null,
  };

  it("accepts a valid record", () => {
    expect(() => ConnectionReadSchema.parse(validRecord)).not.toThrow();
  });

  it("accepts ai_notes as null", () => {
    expect(() => ConnectionReadSchema.parse({ ...validRecord, ai_notes: null })).not.toThrow();
  });

  it("accepts deleted_at as a timestamp (soft-deleted record)", () => {
    expect(() =>
      ConnectionReadSchema.parse({
        ...validRecord,
        deleted_at: "2026-04-25T10:30:00+00:00",
      }),
    ).not.toThrow();
  });

  it("rejects record without deleted_at field (deleted_at is required, may be null)", () => {
    const { deleted_at: _omitted, ...withoutDeletedAt } = validRecord;
    expect(() => ConnectionReadSchema.parse(withoutDeletedAt)).toThrow();
  });

  it("accepts records with embedded tags", () => {
    expect(() =>
      ConnectionReadSchema.parse({
        ...validRecord,
        tags: [
          {
            id: "323e4567-e89b-12d3-a456-426614174000",
            name: "sales",
            created_at: "2026-04-23T10:30:00+00:00",
          },
        ],
      }),
    ).not.toThrow();
  });

  it("accepts multiple embedded tags", () => {
    expect(() =>
      ConnectionReadSchema.parse({
        ...validRecord,
        tags: [
          {
            id: "323e4567-e89b-12d3-a456-426614174000",
            name: "sales",
            created_at: "2026-04-23T10:30:00+00:00",
          },
          {
            id: "423e4567-e89b-12d3-a456-426614174000",
            name: "logistics",
            created_at: "2026-04-23T10:30:00+00:00",
          },
        ],
      }),
    ).not.toThrow();
  });

  it("rejects record with malformed nested tag (non-UUID id)", () => {
    expect(() =>
      ConnectionReadSchema.parse({
        ...validRecord,
        tags: [
          {
            id: "not-a-uuid",
            name: "sales",
            created_at: "2026-04-23T10:30:00+00:00",
          },
        ],
      }),
    ).toThrow();
  });

  it("rejects record with invalid involvement", () => {
    expect(() =>
      ConnectionReadSchema.parse({
        ...validRecord,
        involvement: "Unknown",
      }),
    ).toThrow();
  });

  it("rejects record with invalid outreach_status", () => {
    expect(() =>
      ConnectionReadSchema.parse({
        ...validRecord,
        outreach_status: "Done",
      }),
    ).toThrow();
  });

  it("rejects record with non-UUID id", () => {
    expect(() =>
      ConnectionReadSchema.parse({
        ...validRecord,
        id: "not-a-uuid",
      }),
    ).toThrow();
  });

  it("rejects record with non-UUID owner_user_id", () => {
    expect(() =>
      ConnectionReadSchema.parse({
        ...validRecord,
        owner_user_id: "not-a-uuid",
      }),
    ).toThrow();
  });

  it("rejects record with malformed submission_date", () => {
    expect(() =>
      ConnectionReadSchema.parse({
        ...validRecord,
        submission_date: "not-a-date",
      }),
    ).toThrow();
  });

  it("accepts extra fields (NOT strict, forward-compatible)", () => {
    // Outbound schemas allow extra fields so the backend can add new
    // fields without breaking the SPA.
    expect(() =>
      ConnectionReadSchema.parse({
        ...validRecord,
        future_field: "some_value",
      }),
    ).not.toThrow();
  });

  it("accepts each of the three INVOLVEMENT_VALUES", () => {
    for (const involvement of INVOLVEMENT_VALUES) {
      expect(() => ConnectionReadSchema.parse({ ...validRecord, involvement })).not.toThrow();
    }
  });

  it("accepts each of the four OUTREACH_STATUS_VALUES", () => {
    for (const outreach_status of OUTREACH_STATUS_VALUES) {
      expect(() => ConnectionReadSchema.parse({ ...validRecord, outreach_status })).not.toThrow();
    }
  });
});

// ---------------------------------------------------------------------------
// PaginatedConnectionsSchema tests (Phase 11)
// ---------------------------------------------------------------------------

describe("PaginatedConnectionsSchema", () => {
  const validRecord = {
    id: "123e4567-e89b-12d3-a456-426614174000",
    full_name: "Jane Doe",
    linkedin_url: "https://www.linkedin.com/in/jane-doe",
    normalized_linkedin_url: "linkedin.com/in/jane-doe",
    company: "Acme",
    job_title: "VP Ops",
    relationship_context: "College.",
    ai_notes: null,
    involvement: "Warm Intro" as const,
    outreach_status: "Not Started" as const,
    submission_date: "2026-04-23T10:30:00+00:00",
    owner_user_id: "223e4567-e89b-12d3-a456-426614174000",
    owner_display_name: "Submitter Name",
    tags: [],
    created_at: "2026-04-23T10:30:00+00:00",
    updated_at: "2026-04-23T10:30:00+00:00",
    deleted_at: null,
  };

  it("accepts a valid pagination envelope", () => {
    expect(() =>
      PaginatedConnectionsSchema.parse({
        items: [validRecord],
        total: 100,
        limit: 50,
        offset: 0,
      }),
    ).not.toThrow();
  });

  it("accepts empty items", () => {
    expect(() =>
      PaginatedConnectionsSchema.parse({
        items: [],
        total: 0,
        limit: 50,
        offset: 0,
      }),
    ).not.toThrow();
  });

  it("accepts limit at upper bound (200)", () => {
    expect(() =>
      PaginatedConnectionsSchema.parse({
        items: [],
        total: 0,
        limit: 200,
        offset: 0,
      }),
    ).not.toThrow();
  });

  it("accepts limit at lower bound (1)", () => {
    expect(() =>
      PaginatedConnectionsSchema.parse({
        items: [],
        total: 0,
        limit: 1,
        offset: 0,
      }),
    ).not.toThrow();
  });

  it("accepts large offset", () => {
    expect(() =>
      PaginatedConnectionsSchema.parse({
        items: [],
        total: 10000,
        limit: 50,
        offset: 9950,
      }),
    ).not.toThrow();
  });

  it("rejects negative total", () => {
    expect(() =>
      PaginatedConnectionsSchema.parse({
        items: [],
        total: -1,
        limit: 50,
        offset: 0,
      }),
    ).toThrow();
  });

  it("rejects limit < 1", () => {
    expect(() =>
      PaginatedConnectionsSchema.parse({
        items: [],
        total: 0,
        limit: 0,
        offset: 0,
      }),
    ).toThrow();
  });

  it("rejects limit > 200", () => {
    expect(() =>
      PaginatedConnectionsSchema.parse({
        items: [],
        total: 0,
        limit: 201,
        offset: 0,
      }),
    ).toThrow();
  });

  it("rejects negative offset", () => {
    expect(() =>
      PaginatedConnectionsSchema.parse({
        items: [],
        total: 0,
        limit: 50,
        offset: -1,
      }),
    ).toThrow();
  });

  it("rejects non-integer total", () => {
    expect(() =>
      PaginatedConnectionsSchema.parse({
        items: [],
        total: 1.5,
        limit: 50,
        offset: 0,
      }),
    ).toThrow();
  });

  it("rejects non-integer limit", () => {
    expect(() =>
      PaginatedConnectionsSchema.parse({
        items: [],
        total: 0,
        limit: 1.5,
        offset: 0,
      }),
    ).toThrow();
  });

  it("rejects when items is missing", () => {
    expect(() =>
      PaginatedConnectionsSchema.parse({
        total: 0,
        limit: 50,
        offset: 0,
      }),
    ).toThrow();
  });

  it("rejects when items contains an invalid record", () => {
    expect(() =>
      PaginatedConnectionsSchema.parse({
        items: [{ ...validRecord, id: "not-a-uuid" }],
        total: 1,
        limit: 50,
        offset: 0,
      }),
    ).toThrow();
  });
});

// ---------------------------------------------------------------------------
// ConnectionDuplicateCheckResponseSchema tests (Phase 12)
// ---------------------------------------------------------------------------

describe("ConnectionDuplicateCheckResponseSchema", () => {
  it("accepts a no-duplicate response", () => {
    expect(() =>
      ConnectionDuplicateCheckResponseSchema.parse({
        duplicate_found: false,
        existing_record_id: null,
        existing_owner_display_name: null,
        existing_submission_date: null,
        normalized_linkedin_url: "linkedin.com/in/jane",
      }),
    ).not.toThrow();
  });

  it("accepts a duplicate-found response", () => {
    expect(() =>
      ConnectionDuplicateCheckResponseSchema.parse({
        duplicate_found: true,
        existing_record_id: "123e4567-e89b-12d3-a456-426614174000",
        existing_owner_display_name: "Existing Owner",
        existing_submission_date: "2026-01-01T00:00:00+00:00",
        normalized_linkedin_url: "linkedin.com/in/jane",
      }),
    ).not.toThrow();
  });

  it("rejects when duplicate_found is missing", () => {
    expect(() =>
      ConnectionDuplicateCheckResponseSchema.parse({
        existing_record_id: null,
        existing_owner_display_name: null,
        existing_submission_date: null,
        normalized_linkedin_url: "linkedin.com/in/jane",
      }),
    ).toThrow();
  });

  it("rejects when duplicate_found is non-boolean", () => {
    expect(() =>
      ConnectionDuplicateCheckResponseSchema.parse({
        duplicate_found: "false",
        existing_record_id: null,
        existing_owner_display_name: null,
        existing_submission_date: null,
        normalized_linkedin_url: "linkedin.com/in/jane",
      }),
    ).toThrow();
  });

  it("rejects when normalized_linkedin_url is missing", () => {
    expect(() =>
      ConnectionDuplicateCheckResponseSchema.parse({
        duplicate_found: false,
        existing_record_id: null,
        existing_owner_display_name: null,
        existing_submission_date: null,
      }),
    ).toThrow();
  });

  it("rejects when existing_record_id is non-UUID and non-null", () => {
    expect(() =>
      ConnectionDuplicateCheckResponseSchema.parse({
        duplicate_found: true,
        existing_record_id: "not-a-uuid",
        existing_owner_display_name: "Owner",
        existing_submission_date: "2026-01-01T00:00:00+00:00",
        normalized_linkedin_url: "linkedin.com/in/jane",
      }),
    ).toThrow();
  });

  it("rejects when existing_submission_date is malformed and non-null", () => {
    expect(() =>
      ConnectionDuplicateCheckResponseSchema.parse({
        duplicate_found: true,
        existing_record_id: "123e4567-e89b-12d3-a456-426614174000",
        existing_owner_display_name: "Owner",
        existing_submission_date: "not-a-date",
        normalized_linkedin_url: "linkedin.com/in/jane",
      }),
    ).toThrow();
  });

  it("accepts extra fields (NOT strict, forward-compatible)", () => {
    expect(() =>
      ConnectionDuplicateCheckResponseSchema.parse({
        duplicate_found: false,
        existing_record_id: null,
        existing_owner_display_name: null,
        existing_submission_date: null,
        normalized_linkedin_url: "linkedin.com/in/jane",
        future_field: "value",
      }),
    ).not.toThrow();
  });
});

// ---------------------------------------------------------------------------
// ConnectionHistoryEntrySchema tests (Phase 13)
// ---------------------------------------------------------------------------

describe("ConnectionHistoryEntrySchema", () => {
  const baseEntry = {
    id: "123e4567-e89b-12d3-a456-426614174000",
    event_type: "create" as const,
    event_timestamp: "2026-04-23T10:30:00+00:00",
    actor_user_id: "223e4567-e89b-12d3-a456-426614174000",
    actor_display_name: "Creator",
    before_payload: null,
    after_payload: { full_name: "Jane Doe" },
  };

  it("accepts a create event", () => {
    expect(() => ConnectionHistoryEntrySchema.parse(baseEntry)).not.toThrow();
  });

  it("accepts each of the eight AUDIT_EVENT_TYPE_VALUES", () => {
    for (const event_type of AUDIT_EVENT_TYPE_VALUES) {
      expect(() => ConnectionHistoryEntrySchema.parse({ ...baseEntry, event_type })).not.toThrow();
    }
  });

  it("accepts a status_change event with before/after payloads", () => {
    expect(() =>
      ConnectionHistoryEntrySchema.parse({
        ...baseEntry,
        event_type: "status_change",
        before_payload: { outreach_status: "Not Started" },
        after_payload: { outreach_status: "In Progress" },
      }),
    ).not.toThrow();
  });

  it("accepts an edit event with multi-field payload", () => {
    expect(() =>
      ConnectionHistoryEntrySchema.parse({
        ...baseEntry,
        event_type: "edit",
        before_payload: { full_name: "Old", company: "Old Co" },
        after_payload: { full_name: "New", company: "New Co" },
      }),
    ).not.toThrow();
  });

  it("rejects an unknown event_type", () => {
    expect(() =>
      ConnectionHistoryEntrySchema.parse({
        ...baseEntry,
        event_type: "unknown_event",
      }),
    ).toThrow();
  });

  it("accepts null actor_display_name", () => {
    expect(() =>
      ConnectionHistoryEntrySchema.parse({
        ...baseEntry,
        actor_display_name: null,
      }),
    ).not.toThrow();
  });

  it("accepts null before_payload", () => {
    expect(() =>
      ConnectionHistoryEntrySchema.parse({
        ...baseEntry,
        before_payload: null,
      }),
    ).not.toThrow();
  });

  it("accepts null after_payload", () => {
    expect(() =>
      ConnectionHistoryEntrySchema.parse({
        ...baseEntry,
        after_payload: null,
      }),
    ).not.toThrow();
  });

  it("accepts both payloads as null", () => {
    expect(() =>
      ConnectionHistoryEntrySchema.parse({
        ...baseEntry,
        before_payload: null,
        after_payload: null,
      }),
    ).not.toThrow();
  });

  it("accepts arbitrary keys in payload (z.record(z.string(), z.unknown()))", () => {
    expect(() =>
      ConnectionHistoryEntrySchema.parse({
        ...baseEntry,
        after_payload: {
          arbitrary_key_1: "value",
          nested: { foo: "bar" },
          list: [1, 2, 3],
          number_value: 42,
          boolean_value: true,
          null_value: null,
        },
      }),
    ).not.toThrow();
  });

  it("rejects payload that is not an object (string)", () => {
    expect(() =>
      ConnectionHistoryEntrySchema.parse({
        ...baseEntry,
        after_payload: "not-an-object",
      }),
    ).toThrow();
  });

  it("rejects payload that is not an object (array)", () => {
    expect(() =>
      ConnectionHistoryEntrySchema.parse({
        ...baseEntry,
        after_payload: [1, 2, 3],
      }),
    ).toThrow();
  });

  it("rejects payload that is not an object (number)", () => {
    expect(() =>
      ConnectionHistoryEntrySchema.parse({
        ...baseEntry,
        after_payload: 42,
      }),
    ).toThrow();
  });

  it("rejects payload that is not an object (boolean)", () => {
    expect(() =>
      ConnectionHistoryEntrySchema.parse({
        ...baseEntry,
        after_payload: true,
      }),
    ).toThrow();
  });

  it("rejects malformed event_timestamp", () => {
    expect(() =>
      ConnectionHistoryEntrySchema.parse({
        ...baseEntry,
        event_timestamp: "not-a-datetime",
      }),
    ).toThrow();
  });

  it("rejects non-UUID actor_user_id", () => {
    expect(() =>
      ConnectionHistoryEntrySchema.parse({
        ...baseEntry,
        actor_user_id: "not-a-uuid",
      }),
    ).toThrow();
  });

  it("rejects non-UUID id", () => {
    expect(() =>
      ConnectionHistoryEntrySchema.parse({
        ...baseEntry,
        id: "not-a-uuid",
      }),
    ).toThrow();
  });

  it("rejects when event_type is missing", () => {
    const { event_type: _omitted, ...withoutEventType } = baseEntry;
    expect(() => ConnectionHistoryEntrySchema.parse(withoutEventType)).toThrow();
  });

  it("rejects when event_timestamp is missing", () => {
    const { event_timestamp: _omitted, ...withoutTs } = baseEntry;
    expect(() => ConnectionHistoryEntrySchema.parse(withoutTs)).toThrow();
  });

  it("rejects when actor_user_id is missing", () => {
    const { actor_user_id: _omitted, ...withoutActor } = baseEntry;
    expect(() => ConnectionHistoryEntrySchema.parse(withoutActor)).toThrow();
  });

  it("rejects when before_payload is missing (must be present, may be null)", () => {
    const { before_payload: _omitted, ...withoutBefore } = baseEntry;
    expect(() => ConnectionHistoryEntrySchema.parse(withoutBefore)).toThrow();
  });

  it("rejects when after_payload is missing (must be present, may be null)", () => {
    const { after_payload: _omitted, ...withoutAfter } = baseEntry;
    expect(() => ConnectionHistoryEntrySchema.parse(withoutAfter)).toThrow();
  });

  it("accepts extra fields (NOT strict, forward-compatible)", () => {
    expect(() =>
      ConnectionHistoryEntrySchema.parse({
        ...baseEntry,
        future_field: "value",
      }),
    ).not.toThrow();
  });
});
