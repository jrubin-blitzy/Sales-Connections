/**
 * auth.ts - Zod 3.x schemas for the F-012 Authentication feature.
 *
 * Mirrors backend/app/schemas/auth.py field-for-field per AAP Sec 0.5.3.
 * The backend pydantic schemas are the authoritative server-side validators;
 * these Zod schemas are a client-side UX optimization that produces
 * instant form feedback AND the TypeScript types consumed by the
 * AuthProvider, LoginScreen, and api/auth.ts.
 *
 * Schemas defined here:
 *   - LoginRequestSchema     Payload for POST /auth/login (email +
 *                            password fallback flow). Backend rejects
 *                            extra fields via extra='forbid'; mirrored
 *                            here via .strict().
 *   - LoginResponseSchema    Response body for POST /auth/login.
 *                            Carries the authenticated UserRead so
 *                            the AuthProvider can hydrate without an
 *                            extra GET /api/me round-trip.
 *   - SessionReadSchema      Response body for GET /api/me used by
 *                            AuthProvider on mount and after every
 *                            navigation if the session may have
 *                            expired.
 *
 * Type exports (via z.infer):
 *   - LoginRequest, LoginResponse, SessionRead
 *
 * Aliases:
 *   - Session is a type alias for SessionRead; both refer to the same
 *     shape. Some consumers (notably AuthProvider) prefer the shorter
 *     name; raw API hooks use SessionRead. Either is fine.
 *
 * Per AAP Sec 0.7.4 (Security Invariants), the backend NEVER returns
 * the JWT in the response body - it sets it as an HttpOnly Secure
 * SameSite=Lax cookie. These schemas reflect that: there is no
 * `token` field anywhere. Including one would create a temptation
 * to mishandle the JWT in JavaScript and would defeat the HttpOnly
 * cookie protection (XSS would then steal the token).
 *
 * Per AAP Sec 0.7.1 invariant 8, server-side pydantic re-validation
 * is authoritative; the Zod checks here are a UX courtesy only.
 *
 * Forward-compatibility: outbound (response) schemas do NOT use
 * .strict() so that benign backend additions (e.g. a future
 * last_login_at field on UserRead) do not force an immediate frontend
 * release. Inbound (request) schemas DO use .strict() to surface form
 * bugs and tampering attempts early.
 */

import { z } from "zod";

import { UserReadSchema } from "@/schemas/admin";

// ---------------------------------------------------------------------------
// LoginRequestSchema - inbound payload for POST /auth/login
// ---------------------------------------------------------------------------

/**
 * Inbound payload for `POST /auth/login` (email + password fallback flow).
 *
 * Mirrors backend `app.schemas.auth.LoginRequest`.
 *
 * Field validations (mirror backend pydantic exactly):
 *   email     RFC 5322 email; max 320 chars (RFC 5321 maximum).
 *             Validated client-side as a UX courtesy; backend
 *             pydantic re-validates authoritatively. Mirrors backend
 *             `_EMAIL_MAX_CHARS = 320`.
 *   password  Min 1 char (any non-empty input is accepted at the
 *             schema layer). The bcrypt verification step in the
 *             handler is the actual auth gate. Mirrors backend
 *             `_PASSWORD_MIN_CHARS = 1`.
 *             Max 128 chars (bcrypt's effective input is 72 bytes;
 *             capping at 128 protects against DoS-style very-long-
 *             password submissions). Mirrors backend
 *             `_PASSWORD_MAX_CHARS = 128`.
 *
 * NOTE on min_length=1 (NOT 8): The backend pydantic uses
 * `min_length=1` so any non-empty password is accepted at the schema
 * layer; bcrypt verification is the actual auth gate. The folder spec
 * suggested `min(8)` which would DIFFER from the backend - mismatched
 * constraints cause UX confusion (server accepts a 5-char password
 * but client form blocks it). Password-strength advice (e.g.
 * "consider a longer password") belongs in the LoginScreen UI as a
 * soft suggestion, not as a hard schema constraint. Drift between
 * the two layers is a defect per AAP Sec 0.5.3.
 *
 * Per AAP Sec 0.7.4, the backend rejects unknown keys via
 * extra='forbid'. Mirrored via .strict() to reject pollution
 * attempts client-side too (e.g. an attacker who got a CSRF token
 * cannot post extra fields like `role: 'Admin'` to escalate
 * privilege - the schema rejects with HTTP 422 before any handler
 * logic runs).
 *
 * Validation message order: `.min(1)` BEFORE `.email()` matters.
 * `.min(1)` produces "Email is required" for empty input;
 * `.email()` produces "Please enter a valid email address" for
 * malformed input. Reverse order would produce "Invalid email" for
 * both cases (since empty strings fail email regex) - less helpful UX.
 */
export const LoginRequestSchema = z
  .object({
    email: z
      .string()
      .min(1, { message: "Email is required" })
      .email({ message: "Please enter a valid email address" })
      .max(320, { message: "Email exceeds 320 characters" }),
    password: z
      .string()
      .min(1, { message: "Password is required" })
      .max(128, { message: "Password must be 128 characters or fewer" }),
  })
  .strict();

/**
 * LoginRequest - TypeScript type derived from LoginRequestSchema.
 * Consumed by frontend/src/features/auth/LoginScreen.tsx and
 * frontend/src/api/auth.ts.
 */
export type LoginRequest = z.infer<typeof LoginRequestSchema>;

// ---------------------------------------------------------------------------
// LoginResponseSchema - outbound response for POST /auth/login
// ---------------------------------------------------------------------------

/**
 * Response body for `POST /auth/login` and `GET /auth/google/callback`.
 *
 * Mirrors backend `app.schemas.auth.LoginResponse`.
 *
 * The session JWT cookie is delivered out-of-band via the Set-Cookie
 * header (HttpOnly, Secure, SameSite=Lax, Path=/), NOT in this
 * response body. The body carries the authenticated user's UserRead
 * so the SPA's AuthProvider can immediately hydrate its auth state
 * without an extra GET /api/me round-trip.
 *
 * Per AAP Sec 0.4.3, the SPA fetch wrapper at
 * frontend/src/api/client.ts includes credentials: 'include' so the
 * cookie is sent on subsequent requests automatically.
 *
 * No .strict() on outbound (response) schemas - forward-compatibility
 * lets the backend add fields (e.g. a future server-clock timestamp
 * for clock-skew correction) without breaking the SPA.
 */
export const LoginResponseSchema = z.object({
  user: UserReadSchema,
});

/**
 * LoginResponse - TypeScript type for the login endpoint response.
 * Consumed by frontend/src/api/auth.ts (TanStack Query mutation
 * result type) and by AuthProvider for state hydration after
 * successful login.
 */
export type LoginResponse = z.infer<typeof LoginResponseSchema>;

// ---------------------------------------------------------------------------
// SessionReadSchema - outbound response for GET /api/me
// ---------------------------------------------------------------------------

/**
 * Response body for `GET /api/me` (lightweight session hydration).
 *
 * Mirrors backend `app.schemas.auth.SessionRead`.
 *
 * Used by the SPA's AuthProvider (per AAP Sec 0.5.2 Layer 1) to
 * hydrate auth state on initial page load. If the session cookie is
 * missing or invalid, the endpoint returns 401 and the AuthProvider
 * redirects to /login.
 *
 * The payload includes the user's full UserRead shape so the SPA
 * can render the user's display name and gate UI elements by role
 * (RoleGate component) without a second round-trip.
 *
 * Fields:
 *   user           Full UserRead (id, email, display_name, role,
 *                  created_at). Embedded as the imported
 *                  UserReadSchema so the user shape has a single
 *                  source of truth in `@/schemas/admin`.
 *   authenticated  Always true when this schema is returned. The
 *                  field exists for explicit type-narrowing in the
 *                  consumer; the backend pydantic default is True.
 *                  Modeled as z.boolean() (not z.literal(true)) for
 *                  forward-compatibility with future partial-auth
 *                  states (MFA pending, password change required,
 *                  etc.) without a schema break.
 */
export const SessionReadSchema = z.object({
  user: UserReadSchema,
  authenticated: z.boolean(),
});

/**
 * SessionRead - TypeScript type for the GET /api/me response.
 * Consumed by frontend/src/auth/AuthProvider.tsx as the session
 * context value and by frontend/src/api/auth.ts (TanStack Query
 * query result type).
 */
export type SessionRead = z.infer<typeof SessionReadSchema>;

/**
 * Session - Alias for SessionRead used by ergonomic consumers.
 * Provides a shorter type name for AuthProvider's context value.
 * Both `Session` and `SessionRead` refer to the exact same shape;
 * they are interchangeable. AuthProvider conventionally uses
 * `Session`; raw API hooks conventionally use `SessionRead`.
 */
export type Session = SessionRead;

// ---------------------------------------------------------------------------
// RegisterRequestSchema - inbound payload for POST /auth/register
// ---------------------------------------------------------------------------

export const RegisterRequestSchema = z
  .object({
    email: z
      .string()
      .min(1, { message: "Email is required" })
      .email({ message: "Please enter a valid email address" })
      .max(320, { message: "Email exceeds 320 characters" }),
    display_name: z
      .string()
      .min(1, { message: "Name is required" })
      .max(100, { message: "Name must be 100 characters or fewer" }),
    password: z
      .string()
      .min(8, { message: "Password must be at least 8 characters" })
      .max(128, { message: "Password must be 128 characters or fewer" }),
    confirm_password: z.string().min(1, { message: "Please confirm your password" }),
  })
  .strict()
  .refine((data: { password: string; confirm_password: string }) => data.password === data.confirm_password, {
    message: "Passwords do not match",
    path: ["confirm_password"],
  });

export type RegisterRequest = z.infer<typeof RegisterRequestSchema>;
