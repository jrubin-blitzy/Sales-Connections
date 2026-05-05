/**
 * connection.test.ts - Co-located vitest suite for the Zod connection schemas.
 *
 * Specifically validates that the Zod LinkedIn URL refine accepts and
 * rejects the SAME set of URLs as the backend pydantic validator at
 * `backend/app/utils/url.py::is_valid_linkedin_url`. Per AAP Sec 0.5.3
 * ("Pydantic and Zod schemas mirror each other"), drift between the
 * two layers is a defect: a URL accepted by the backend but rejected
 * by the Zod refine is a UX defect; a URL accepted by the Zod refine
 * but rejected by the backend produces a 422 after the user has
 * already invested in filling out the form.
 *
 * The shared accept/reject URL list below is the SINGLE SOURCE OF
 * TRUTH for the contract between the two validators. When this list
 * changes, update BOTH the Zod helper in
 * `frontend/src/schemas/connection.ts::isValidLinkedInUrl` AND the
 * pydantic helper in `backend/app/utils/url.py::is_valid_linkedin_url`
 * in the same PR.
 *
 * Co-location rationale: this file lives alongside `connection.ts`
 * because the Zod helper itself is module-scoped (not exported) and
 * the cleanest test surface is the public schema's `.refine` failure
 * mode. The vitest config in `frontend/vitest.config.ts` is configured
 * to discover both `tests/` and `src/` test files via:
 *   include: ["tests/**\u002F*.test.{ts,tsx}", "src/**\u002F*.test.{ts,tsx}"]
 */

import { describe, expect, it } from "vitest";

import { ConnectionCreateSchema } from "@/schemas/connection";

/**
 * Shared baseline payload — fills every required ConnectionCreate
 * field with a known-valid value EXCEPT `linkedin_url`, which each
 * test substitutes for the URL under test. Keeps assertions focused
 * on the LinkedIn refine without leaking failures from unrelated
 * fields.
 */
const baselinePayload = {
  full_name: "Jane Doe",
  company: "Acme",
  job_title: "VP of Operations",
  relationship_context: "We worked together at a previous startup.",
  involvement: "Warm Intro" as const,
  tag_ids: [],
};

/**
 * URLs the BACKEND pydantic validator accepts. The Zod refine MUST
 * also accept every entry. Drift in either direction is a defect.
 *
 * Each entry in this list is justified by the corresponding shape
 * documented in `backend/app/utils/url.py`:
 *   - bare host
 *   - www subdomain
 *   - country subdomains (uk, de, ca, jp, au)
 *   - http and https schemes
 *   - trailing slash
 *   - query string
 *   - fragment
 *   - dotted slug
 *   - hyphen and underscore in slug
 *   - percent-encoded slug
 *   - /pub/ legacy path
 *   - /pub/ with trailing numeric components
 *   - mixed case (URL constructor lowercases the host; slug case is
 *     preserved by the validator and lowered only by normalization)
 */
const acceptedUrls: readonly string[] = [
  "https://linkedin.com/in/jane-doe",
  "https://www.linkedin.com/in/jane-doe",
  "http://www.linkedin.com/in/jane-doe",
  "https://uk.linkedin.com/in/jane-doe",
  "https://de.linkedin.com/in/jane-doe",
  "https://ca.linkedin.com/in/jane-doe",
  "https://jp.linkedin.com/in/jane-doe",
  "https://au.linkedin.com/in/jane-doe",
  "https://m.linkedin.com/in/jane-doe",
  "https://www.linkedin.com/in/jane-doe/",
  "https://www.linkedin.com/in/jane-doe?utm_source=newsletter",
  "https://www.linkedin.com/in/jane-doe#bio",
  "https://www.linkedin.com/in/jane.doe",
  "https://www.linkedin.com/in/jane_doe",
  "https://www.linkedin.com/in/JaneDoe",
  "https://www.linkedin.com/in/jane%20doe",
  "https://www.linkedin.com/pub/jane-doe",
  "https://www.linkedin.com/pub/jane-doe/12/345/678",
  "https://www.linkedin.com/in/jane-doe/details/contact-info",
];

/**
 * URLs the BACKEND pydantic validator rejects. The Zod refine MUST
 * also reject every entry.
 *
 * Each entry exercises a specific rejection branch:
 *   - empty / non-URL input
 *   - wrong scheme (ftp, javascript)
 *   - wrong host (example.com)
 *   - spoof multi-level (attacker.com.linkedin.com)
 *   - wrong TLD (linkedin.cn)
 *   - non-allowed subdomain (foo.linkedin.com)
 *   - missing slug
 *   - wrong path family (/company/, /school/)
 *   - slug with whitespace or `/` injection
 */
const rejectedUrls: readonly string[] = [
  "",
  "not a url",
  "javascript:alert(1)",
  "ftp://www.linkedin.com/in/jane-doe",
  "https://example.com/in/jane-doe",
  "https://attacker.com.linkedin.com/in/jane-doe",
  "https://linkedin.cn/in/jane-doe",
  "https://foo.linkedin.com/in/jane-doe",
  "https://www.linkedin.com/in/",
  "https://www.linkedin.com/in",
  "https://www.linkedin.com/company/acme",
  "https://www.linkedin.com/school/mit",
  "https://www.linkedin.com/feed/",
];

describe("ConnectionCreateSchema — LinkedIn URL refine", () => {
  describe("accepts URLs the backend pydantic validator accepts", () => {
    for (const url of acceptedUrls) {
      it(`accepts ${JSON.stringify(url)}`, () => {
        const result = ConnectionCreateSchema.safeParse({
          ...baselinePayload,
          linkedin_url: url,
        });
        // .safeParse failures do not surface from a single field by
        // default; we assert at the top level and, on failure, dump
        // the full error so the test report shows which specific
        // refine fired. This makes regressions surface fast when the
        // backend validator rules drift.
        expect(result.success, `Expected ${url} to pass; errors: ${JSON.stringify(result)}`).toBe(
          true,
        );
      });
    }
  });

  describe("rejects URLs the backend pydantic validator rejects", () => {
    for (const url of rejectedUrls) {
      it(`rejects ${JSON.stringify(url)}`, () => {
        const result = ConnectionCreateSchema.safeParse({
          ...baselinePayload,
          linkedin_url: url,
        });
        expect(result.success, `Expected ${url} to fail Zod validation; backend rejects it`).toBe(
          false,
        );
      });
    }
  });
});
