/**
 * auth.test.ts - Vitest tests for frontend/src/schemas/auth.ts.
 *
 * Verifies all 3 Zod schemas + Session type alias exported by auth.ts:
 *   - LoginRequestSchema (strict; email + password with min(1) NOT min(8))
 *   - LoginResponseSchema ({ user: UserRead })
 *   - SessionReadSchema ({ user: UserRead, authenticated: boolean })
 *   - Session type alias (= SessionRead)
 *
 * CRITICAL CONTRACTS PINNED BY THIS FILE:
 *   1. password min(1) NOT min(8) - mirrors backend pydantic exactly.
 *      Diverging would cause UX confusion (server accepts a 5-char
 *      password but client form blocks it).
 *   2. NO `token` field anywhere - JWT is delivered as HttpOnly cookie
 *      per AAP Sec 0.7.4. Adding a token field to any schema would be
 *      a security regression.
 *   3. Strict mode on LoginRequestSchema - mirrors backend's
 *      extra="forbid" against tampering attacks.
 *   4. Email validation order: min(1) BEFORE email() - produces
 *      "Email is required" for empty input vs "Please enter a valid
 *      email address" for malformed input. Cleaner UX.
 *
 * Per AAP Sec 0.7.7, Zod schemas mirror backend pydantic exactly.
 *
 * NOTE: This is a PURE unit test - no React rendering, no MSW server,
 * no HTTP. Just Zod parse() / safeParse() on known-good and known-bad
 * payloads. The MSW server boot in tests/setup.ts and the jsdom
 * environment from vite.config.ts are harmless overhead here.
 *
 * Conventions per frontend/.prettierrc.json:
 *   - Double quotes (singleQuote: false).
 *   - Trailing commas (trailingComma: "all").
 *   - 2-space indent; printWidth: 100.
 *   - import type for type-only imports per verbatimModuleSyntax: true.
 *   - Only import what is used (noUnusedLocals: true).
 */

import { describe, expect, it } from "vitest";

import { LoginRequestSchema, LoginResponseSchema, SessionReadSchema } from "@/schemas/auth";
import type { Session, SessionRead } from "@/schemas/auth";

// ---------------------------------------------------------------------------
// LoginRequestSchema (Phase 3)
// ---------------------------------------------------------------------------
// The most-tested schema in this file. Verifies:
//   - Valid email + password (including 1-char password)
//   - Email validation order (min(1) before email() before max(320))
//   - Length caps (email max 320, password max 128)
//   - Strict mode (no extra fields)
//   - Specific error messages match the source exactly so a drift
//     between client copy and server behavior is caught immediately
// ---------------------------------------------------------------------------

describe("LoginRequestSchema (strict)", () => {
  describe("valid inputs", () => {
    it("accepts a valid email and 1-char password", () => {
      // CRITICAL: password uses min(1) NOT min(8). 1-char passwords
      // are accepted at the schema layer; bcrypt verification is the
      // actual auth gate. This test pins that contract - if someone
      // changes the source to min(8), this test fails immediately
      // and signals the regression.
      expect(() =>
        LoginRequestSchema.parse({ email: "jane@example.com", password: "p" }),
      ).not.toThrow();
    });

    it("accepts a long password up to 128 characters", () => {
      const longPassword = "a".repeat(128);
      expect(() =>
        LoginRequestSchema.parse({
          email: "jane@example.com",
          password: longPassword,
        }),
      ).not.toThrow();
    });

    it("accepts a typical password", () => {
      expect(() =>
        LoginRequestSchema.parse({
          email: "jane@example.com",
          password: "a-very-long-password",
        }),
      ).not.toThrow();
    });
  });

  describe("email validation", () => {
    it('rejects empty email with message "Email is required"', () => {
      const result = LoginRequestSchema.safeParse({
        email: "",
        password: "secret",
      });
      expect(result.success).toBe(false);
      if (!result.success) {
        // .find() returns the FIRST issue whose path includes "email".
        // Per Zod chain order (.min(1) before .email()), the first
        // emitted message for an empty string is "Email is required".
        const emailIssue = result.error.issues.find((issue) => issue.path.includes("email"));
        expect(emailIssue).toBeDefined();
        expect(emailIssue?.message).toBe("Email is required");
      }
    });

    it('rejects malformed email (no @ sign) with message "Please enter a valid email address"', () => {
      const result = LoginRequestSchema.safeParse({
        email: "not-an-email",
        password: "secret",
      });
      expect(result.success).toBe(false);
      if (!result.success) {
        const emailIssue = result.error.issues.find((issue) => issue.path.includes("email"));
        expect(emailIssue).toBeDefined();
        expect(emailIssue?.message).toBe("Please enter a valid email address");
      }
    });

    it('rejects email exceeding 320 characters with message "Email exceeds 320 characters"', () => {
      // 310-char local part + "@example.com" (12 chars) = 322 chars,
      // safely above the 320 cap.
      const localPart = "a".repeat(310);
      const tooLongEmail = `${localPart}@example.com`;
      expect(tooLongEmail.length).toBeGreaterThan(320);
      const result = LoginRequestSchema.safeParse({
        email: tooLongEmail,
        password: "secret",
      });
      expect(result.success).toBe(false);
      if (!result.success) {
        const emailIssue = result.error.issues.find((issue) => issue.path.includes("email"));
        expect(emailIssue).toBeDefined();
        expect(emailIssue?.message).toBe("Email exceeds 320 characters");
      }
    });

    it("accepts an email of exactly 320 characters", () => {
      // 314-char local part + "@e.com" (6 chars) = 320 chars exactly.
      // Boundary test that pins the "<=320" semantic of .max(320)
      // (Zod's .max() is inclusive).
      const localPart = "a".repeat(314);
      const exactlyMaxEmail = `${localPart}@e.com`;
      expect(exactlyMaxEmail.length).toBe(320);
      expect(() =>
        LoginRequestSchema.parse({
          email: exactlyMaxEmail,
          password: "secret",
        }),
      ).not.toThrow();
    });
  });

  describe("password validation", () => {
    it('rejects empty password with message "Password is required"', () => {
      const result = LoginRequestSchema.safeParse({
        email: "jane@example.com",
        password: "",
      });
      expect(result.success).toBe(false);
      if (!result.success) {
        const passwordIssue = result.error.issues.find((issue) => issue.path.includes("password"));
        expect(passwordIssue).toBeDefined();
        expect(passwordIssue?.message).toBe("Password is required");
      }
    });

    it('rejects password exceeding 128 characters with message "Password must be 128 characters or fewer"', () => {
      const tooLongPassword = "a".repeat(129);
      const result = LoginRequestSchema.safeParse({
        email: "jane@example.com",
        password: tooLongPassword,
      });
      expect(result.success).toBe(false);
      if (!result.success) {
        const passwordIssue = result.error.issues.find((issue) => issue.path.includes("password"));
        expect(passwordIssue).toBeDefined();
        expect(passwordIssue?.message).toBe("Password must be 128 characters or fewer");
      }
    });

    it("accepts a password of exactly 128 characters", () => {
      // Boundary test for .max(128) inclusive semantics.
      const exactlyMaxPassword = "a".repeat(128);
      expect(() =>
        LoginRequestSchema.parse({
          email: "jane@example.com",
          password: exactlyMaxPassword,
        }),
      ).not.toThrow();
    });
  });

  describe("strict mode", () => {
    it('rejects extra fields (mirrors backend extra="forbid")', () => {
      // Per AAP Sec 0.7.4, .strict() defends against pollution
      // attempts client-side too: an attacker who got past CSRF
      // cannot post {role: "Admin"} to escalate privilege - the
      // schema rejects with a parse error before any handler runs.
      expect(() =>
        LoginRequestSchema.parse({
          email: "jane@example.com",
          password: "secret",
          extra: "value",
        }),
      ).toThrow();
    });

    it("rejects when email field is missing", () => {
      expect(() => LoginRequestSchema.parse({ password: "secret" })).toThrow();
    });

    it("rejects when password field is missing", () => {
      expect(() => LoginRequestSchema.parse({ email: "jane@example.com" })).toThrow();
    });

    it("rejects null email", () => {
      expect(() => LoginRequestSchema.parse({ email: null, password: "secret" })).toThrow();
    });

    it("rejects null password", () => {
      expect(() =>
        LoginRequestSchema.parse({
          email: "jane@example.com",
          password: null,
        }),
      ).toThrow();
    });
  });
});

// ---------------------------------------------------------------------------
// LoginResponseSchema (Phase 4)
// ---------------------------------------------------------------------------
// LoginResponseSchema embeds UserReadSchema. Test that the response
// shape parses correctly with a valid user fixture, and rejects when
// the user is missing or malformed. ALSO verifies that no `token`
// field is part of the schema (JWT is HttpOnly cookie per AAP 0.7.4).
// ---------------------------------------------------------------------------

describe("LoginResponseSchema", () => {
  // Construct a UserRead-shaped fixture inline to avoid coupling
  // this test to the @/schemas/admin import. UserReadSchema validates
  // it transitively when LoginResponseSchema parses.
  const validUser = {
    id: "123e4567-e89b-12d3-a456-426614174000",
    email: "jane@example.com",
    display_name: "Jane Doe",
    role: "Admin" as const,
    created_at: "2026-04-23T10:30:00+00:00",
  };

  it("accepts a valid login response with embedded user", () => {
    expect(() => LoginResponseSchema.parse({ user: validUser })).not.toThrow();
  });

  it("rejects when user field is missing", () => {
    expect(() => LoginResponseSchema.parse({})).toThrow();
  });

  it("rejects when user is null", () => {
    expect(() => LoginResponseSchema.parse({ user: null })).toThrow();
  });

  it("rejects when nested user has invalid id", () => {
    expect(() =>
      LoginResponseSchema.parse({
        user: { ...validUser, id: "not-a-uuid" },
      }),
    ).toThrow();
  });

  it("rejects when nested user has invalid email", () => {
    expect(() =>
      LoginResponseSchema.parse({
        user: { ...validUser, email: "no-at-sign" },
      }),
    ).toThrow();
  });

  it("rejects when nested user has invalid role", () => {
    expect(() =>
      LoginResponseSchema.parse({
        user: { ...validUser, role: "NotARole" },
      }),
    ).toThrow();
  });

  it("does NOT contain a token field (JWT is HttpOnly cookie per AAP Sec 0.7.4)", () => {
    // Verify by attempting to parse a response with an extra token
    // field. LoginResponseSchema is NOT .strict() (forward-compatible),
    // so the token field is silently dropped on parse. The result
    // must NOT include it. This locks in the contract that token
    // never crosses the SPA boundary as a body field - any future
    // PR that adds `token: z.string()` to LoginResponseSchema would
    // cause this test to fail because the parsed result would carry
    // the field forward.
    const result = LoginResponseSchema.parse({
      user: validUser,
      token: "eyJhbGciOiJIUzI1NiIs.fake.jwt",
    });
    expect(result).not.toHaveProperty("token");
  });
});

// ---------------------------------------------------------------------------
// SessionReadSchema (Phase 5)
// ---------------------------------------------------------------------------
// SessionReadSchema is the response shape of GET /api/me used by the
// AuthProvider on mount. Same UserRead embed, plus a top-level
// `authenticated: boolean` for explicit type-narrowing in the consumer.
// ---------------------------------------------------------------------------

describe("SessionReadSchema", () => {
  const validUser = {
    id: "123e4567-e89b-12d3-a456-426614174000",
    email: "jane@example.com",
    display_name: "Jane Doe",
    role: "Contributor" as const,
    created_at: "2026-04-23T10:30:00+00:00",
  };

  const validSession = {
    user: validUser,
    authenticated: true,
  };

  it("accepts a valid session payload", () => {
    expect(() => SessionReadSchema.parse(validSession)).not.toThrow();
  });

  it("accepts authenticated: false (future-proofing)", () => {
    // Backend currently always returns true on success, but the schema
    // uses z.boolean() (not z.literal(true)) to support future
    // partial-auth states (MFA pending, password change required) without
    // a schema break. This test pins the z.boolean() contract.
    expect(() => SessionReadSchema.parse({ ...validSession, authenticated: false })).not.toThrow();
  });

  it("rejects when user is missing", () => {
    expect(() => SessionReadSchema.parse({ authenticated: true })).toThrow();
  });

  it("rejects when authenticated field is missing", () => {
    expect(() => SessionReadSchema.parse({ user: validUser })).toThrow();
  });

  it("rejects when authenticated is a string", () => {
    expect(() => SessionReadSchema.parse({ ...validSession, authenticated: "true" })).toThrow();
  });

  it("rejects when nested user is malformed", () => {
    expect(() =>
      SessionReadSchema.parse({
        user: { ...validUser, role: "Hacker" },
        authenticated: true,
      }),
    ).toThrow();
  });

  it("does NOT contain a token field", () => {
    // Same security invariant as LoginResponseSchema: JWT is HttpOnly
    // cookie only. Parsing a payload with a token field must produce
    // a result that drops it (forward-compat policy means no .strict()
    // here, but the schema fields explicitly do NOT include token).
    const result = SessionReadSchema.parse({
      ...validSession,
      token: "fake-jwt",
    });
    expect(result).not.toHaveProperty("token");
  });
});

// ---------------------------------------------------------------------------
// Session type alias (Phase 6)
// ---------------------------------------------------------------------------
// Verify that Session is exported as a type alias for SessionRead.
// This is primarily a TypeScript-compile-time check: if the alias
// is not exported, the import at the top of this file fails to type-
// check and the test never runs. The runtime block below uses both
// types interchangeably to demonstrate they refer to the same shape.
// ---------------------------------------------------------------------------

describe("Session type alias", () => {
  it("is exported as an alias for SessionRead", () => {
    // If `Session` were not exported, the type-only import at the top
    // of this file would emit a TypeScript error and CI's
    // `npm run type-check` would fail before tests ran.
    const validSession: Session = {
      user: {
        id: "123e4567-e89b-12d3-a456-426614174000",
        email: "jane@example.com",
        display_name: "Jane Doe",
        role: "Admin",
        created_at: "2026-04-23T10:30:00+00:00",
      },
      authenticated: true,
    };

    // Use the alias as if it were SessionRead (interchangeable).
    // If Session were a different shape than SessionRead, this
    // assignment would fail to compile.
    const alsoValid: SessionRead = validSession;

    expect(() => SessionReadSchema.parse(alsoValid)).not.toThrow();
  });
});
