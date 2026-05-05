/**
 * handlers.ts - Default MSW (Mock Service Worker) handlers and per-test
 * override factories for the Sales-Connections SPA test suite.
 *
 * Exports two named surfaces:
 *
 *   1. `handlers: HttpHandler[]`
 *      The default array consumed by `./server.ts` to construct the
 *      MSW Node server. Covers EVERY endpoint the SPA calls so that
 *      MSW's `onUnhandledRequest: "error"` configuration in
 *      `frontend/tests/setup.ts` does not throw on unmapped URLs.
 *
 *   2. `overrides`
 *      A namespaced object of factory functions. Each factory returns
 *      one `HttpHandler` configured for a specific failure mode or
 *      edge case (4xx, 5xx, timeout, duplicate-found, etc.). Tests
 *      import this and call `server.use(overrides.connections.list500())`
 *      to swap behavior for a single test.
 *
 * Endpoint coverage (per AAP Sec 0.4.3):
 *   GET    /api/me                                   default 401
 *   POST   /auth/login                               200 with user
 *   POST   /auth/logout                              200 with status
 *   GET    /api/connections                          200 paginated
 *   POST   /api/connections                          201 created record
 *   GET    /api/connections/:id                      200 detail
 *   PATCH  /api/connections/:id                      200 updated
 *   DELETE /api/connections/:id                      200 soft-deleted
 *   PATCH  /api/connections/:id/status               200 updated
 *   GET    /api/connections/:id/history              200 paginated history
 *   GET    /api/connections/duplicate-check          200 (not-duplicate by default)
 *   POST   /api/notes/generate                       200 AI text
 *   GET    /api/tags                                 200 plain array
 *   POST   /api/tags                                 201 new tag
 *   GET    /api/admin/users                          200 plain array
 *   PATCH  /api/admin/users/:id                      200 updated user
 *   GET    /api/admin/records                        200 paginated
 *   DELETE /api/admin/records/:id                    204 no content
 *   GET    /api/admin/analytics                      200 analytics
 *
 * Error envelope contract (per AAP Sec 0.4.3 and api/client.ts):
 *   { error: { code: string, message: string,
 *              correlation_id: string, fields: ApiErrorField[] } }
 *
 * MSW v2 syntax (msw@2.7.0):
 *   - http.get(path, resolver) / http.post(...) / http.patch(...) /
 *     http.delete(...)
 *   - HttpResponse.json(body, init?)
 *   - new HttpResponse(null, { status: 204 }) for empty bodies
 *
 * Conventions per AAP Sec 0.7.7:
 *   - Strict TypeScript; no `any`.
 *   - Double quotes (project Prettier config); trailing commas; 2-space
 *     indent; line length <= 100.
 *   - Named exports only; no default export.
 *   - No emoji; no console.log.
 */

import { http, HttpResponse, type HttpHandler } from "msw";

import {
  makeAdminUserList,
  makeAnalyticsResponse,
  makeConnectionList,
  makeConnectionRead,
  makeDuplicateCheckResponse,
  makeGenerateNotesResponse,
  makeHistoryEntry,
  makePaginatedConnections,
  makePaginatedHistory,
  makeSessionRead,
  makeTagRead,
  makeUserRead,
  type AdminRecordsResponse,
} from "./data";

// ---------------------------------------------------------------------------
// Error envelope helper
// ---------------------------------------------------------------------------

/**
 * The contract shape for individual field errors in the unified error
 * envelope. Mirrors `ApiErrorField` from frontend/src/api/client.ts;
 * the production interface declares `code` and accepts an optional
 * `message`. For tests, the field/message pair is the most useful
 * combination (the SPA renders `message` directly), so we keep the
 * shape minimal here. Test authors may pass a richer payload through
 * the `fields` argument of `errorEnvelope` if they need the `code`
 * field too - the response is serialized as JSON so any extra
 * properties pass through unmodified.
 */
interface ErrorField {
  field: string;
  message: string;
}

/**
 * Build the unified error envelope returned by the backend.
 *
 * Per AAP Sec 0.4.3 and api/client.ts's `parseErrorEnvelope()`, the
 * envelope shape is:
 *
 *   { error: { code, message, correlation_id, fields } }
 *
 * `correlation_id` is populated with a stable test value so tests can
 * assert on it deterministically. Real backend correlation IDs are
 * runtime-generated UUIDs.
 *
 * @param status   HTTP status code (4xx or 5xx).
 * @param code     Machine-readable backend error code
 *                 (e.g., "validation_error", "ai_timeout").
 * @param message  Human-readable description.
 * @param fields   Optional per-field validation errors (for 422).
 * @returns        An MSW HttpResponse with the JSON envelope and status.
 */
function errorEnvelope(
  status: number,
  code: string,
  message: string,
  fields: ErrorField[] = [],
): HttpResponse {
  return HttpResponse.json(
    {
      error: {
        code,
        message,
        correlation_id: "test-correlation-id",
        fields,
      },
    },
    { status },
  );
}

// ---------------------------------------------------------------------------
// Default handlers
// ---------------------------------------------------------------------------

/**
 * Default MSW handlers covering every endpoint the SPA fetches. The
 * defaults represent the happy path or the most-common state (e.g.,
 * unauthenticated session for /api/me). Per-test overrides are added
 * via `server.use(overrides.<group>.<scenario>())`.
 *
 * IMPORTANT - handler ordering: MSW v2 evaluates handlers in DECLARED
 * ORDER. The first matching handler wins. More-specific paths MUST
 * come BEFORE less-specific paths within the same HTTP method:
 *   - /api/connections/duplicate-check  before  /api/connections/:id (GET)
 *   - /api/connections/:id/history       before  /api/connections/:id (GET)
 *   - /api/connections/:id/status        before  /api/connections/:id (PATCH)
 */
export const handlers: HttpHandler[] = [
  // -------------------------------------------------------------------
  // Auth (F-012)
  // -------------------------------------------------------------------

  // GET /api/me - Default 401 (unauthenticated). Tests that need a
  // session must override via `overrides.api.me200(...)`.
  http.get("/api/me", () => errorEnvelope(401, "unauthorized", "Not authenticated")),

  // POST /auth/login - Default 200 with the requested email's user.
  // Tests can override via overrides.auth.loginInvalidCredentials() etc.
  http.post("/auth/login", async ({ request }) => {
    const body = (await request.json()) as { email?: string; password?: string };
    return HttpResponse.json({
      user: makeUserRead({ email: body.email ?? "user@example.com" }),
    });
  }),

  // POST /auth/logout - Default 200 with status envelope.
  // Per useLogoutMutation: returns { status: "ok" }.
  http.post("/auth/logout", () => HttpResponse.json({ status: "ok" })),

  // -------------------------------------------------------------------
  // Connections (F-001, F-004, F-005, F-007, F-010, F-011)
  // -------------------------------------------------------------------

  // GET /api/connections - Paginated list. Default returns 10 records
  // matching the typical feed size.
  http.get("/api/connections", () => {
    const items = makeConnectionList(10);
    return HttpResponse.json(makePaginatedConnections(items));
  }),

  // POST /api/connections - Create. Returns 201 with the persisted record.
  // The body fields are echoed back; server-controlled fields
  // (id, owner_*, timestamps) come from defaults.
  http.post("/api/connections", async ({ request }) => {
    const body = (await request.json()) as Record<string, unknown>;
    const involvement = ((): "Warm Intro" | "Soft Reference" | "Target Only" | undefined => {
      if (
        body.involvement === "Warm Intro" ||
        body.involvement === "Soft Reference" ||
        body.involvement === "Target Only"
      ) {
        return body.involvement;
      }
      return undefined;
    })();
    const record = makeConnectionRead({
      ...(typeof body.full_name === "string" ? { full_name: body.full_name } : {}),
      ...(typeof body.linkedin_url === "string" ? { linkedin_url: body.linkedin_url } : {}),
      ...(typeof body.company === "string" ? { company: body.company } : {}),
      ...(typeof body.job_title === "string" ? { job_title: body.job_title } : {}),
      ...(typeof body.relationship_context === "string"
        ? { relationship_context: body.relationship_context }
        : {}),
      ...(typeof body.ai_notes === "string" || body.ai_notes === null
        ? { ai_notes: body.ai_notes as string | null }
        : {}),
      ...(involvement !== undefined ? { involvement } : {}),
    });
    return HttpResponse.json(record, { status: 201 });
  }),

  // GET /api/connections/duplicate-check - Default: NOT a duplicate.
  // Tests that need duplicate behavior override via
  // overrides.duplicateCheck.duplicateFound(...).
  // Note: this MUST come BEFORE GET /api/connections/:id so MSW's path
  // matcher does not treat "duplicate-check" as a record id.
  http.get("/api/connections/duplicate-check", ({ request }) => {
    const url = new URL(request.url);
    const linkedinUrl = url.searchParams.get("linkedin_url") ?? "";
    return HttpResponse.json(
      makeDuplicateCheckResponse({
        normalized_linkedin_url: linkedinUrl.toLowerCase(),
      }),
    );
  }),

  // GET /api/connections/:id/history - Paginated history. Default 5 entries.
  // Note: more-specific path; MSW evaluates handlers in declared order.
  http.get("/api/connections/:id/history", ({ request }) => {
    const url = new URL(request.url);
    const page = Number(url.searchParams.get("page") ?? "1");
    const pageSize = Number(url.searchParams.get("page_size") ?? "25");
    const items = Array.from({ length: 5 }, () => makeHistoryEntry());
    return HttpResponse.json(makePaginatedHistory(items, { page, page_size: pageSize }));
  }),

  // PATCH /api/connections/:id/status - Status mutation. Returns full record.
  // Note: more-specific path; MUST come before PATCH /api/connections/:id.
  http.patch("/api/connections/:id/status", async ({ params, request }) => {
    const id = String(params.id);
    const body = (await request.json()) as { outreach_status?: unknown };
    const valid =
      body.outreach_status === "Not Started" ||
      body.outreach_status === "In Progress" ||
      body.outreach_status === "Contacted" ||
      body.outreach_status === "Closed";
    return HttpResponse.json(
      makeConnectionRead({
        id,
        outreach_status: valid
          ? (body.outreach_status as "Not Started" | "In Progress" | "Contacted" | "Closed")
          : "In Progress",
      }),
    );
  }),

  // GET /api/connections/:id - Detail.
  http.get("/api/connections/:id", ({ params }) =>
    HttpResponse.json(makeConnectionRead({ id: String(params.id) })),
  ),

  // PATCH /api/connections/:id - Edit. Returns full record with patches applied.
  http.patch("/api/connections/:id", async ({ params, request }) => {
    const id = String(params.id);
    const body = (await request.json()) as Record<string, unknown>;
    return HttpResponse.json(
      makeConnectionRead({
        id,
        ...(typeof body.full_name === "string" ? { full_name: body.full_name } : {}),
        ...(typeof body.company === "string" ? { company: body.company } : {}),
        ...(typeof body.job_title === "string" ? { job_title: body.job_title } : {}),
        ...(typeof body.relationship_context === "string"
          ? { relationship_context: body.relationship_context }
          : {}),
        ...(typeof body.ai_notes === "string" || body.ai_notes === null
          ? { ai_notes: body.ai_notes as string | null }
          : {}),
      }),
    );
  }),

  // DELETE /api/connections/:id - Soft delete. Returns full record with
  // `deleted_at` populated. Per AAP Sec 0.5.2, soft delete sets
  // deleted_at = NOW() and the SPA reads back the record so its cache
  // reflects the soft-deleted state.
  http.delete("/api/connections/:id", ({ params }) =>
    HttpResponse.json(
      makeConnectionRead({
        id: String(params.id),
        deleted_at: "2026-04-23T12:00:00+00:00",
      }),
    ),
  ),

  // -------------------------------------------------------------------
  // Notes (F-002)
  // -------------------------------------------------------------------

  // POST /api/notes/generate - AI note generation. Default success.
  // Tests for AI failure override via overrides.notes.timeout504() etc.
  http.post("/api/notes/generate", async ({ request }) => {
    const body = (await request.json()) as { relationship_context?: unknown };
    const context =
      typeof body.relationship_context === "string"
        ? body.relationship_context.slice(0, 80)
        : "mock context";
    return HttpResponse.json(
      makeGenerateNotesResponse({
        ai_notes:
          "Mock AI talking points based on: " +
          context +
          ". Suggested opener: ask about recent role transition.",
      }),
    );
  }),

  // -------------------------------------------------------------------
  // Tags (F-008)
  // -------------------------------------------------------------------

  // GET /api/tags - Plain array (NOT paginated).
  http.get("/api/tags", () =>
    HttpResponse.json([
      makeTagRead({ name: "industry:saas" }),
      makeTagRead({ name: "use-case:demand-gen" }),
      makeTagRead({ name: "geography:nyc" }),
    ]),
  ),

  // POST /api/tags - Create. Returns 201 with the new tag.
  http.post("/api/tags", async ({ request }) => {
    const body = (await request.json()) as { name?: unknown };
    const name = typeof body.name === "string" ? body.name : "mock-tag";
    return HttpResponse.json(makeTagRead({ name }), { status: 201 });
  }),

  // -------------------------------------------------------------------
  // Admin (F-014)
  // -------------------------------------------------------------------

  // GET /api/admin/users - Plain array (NOT paginated).
  http.get("/api/admin/users", () => HttpResponse.json(makeAdminUserList(5))),

  // PATCH /api/admin/users/:id - Role mutation. Returns updated user.
  http.patch("/api/admin/users/:id", async ({ params, request }) => {
    const id = String(params.id);
    const body = (await request.json()) as { role?: unknown };
    const role =
      body.role === "Admin" || body.role === "Contributor" || body.role === "Viewer"
        ? body.role
        : "Contributor";
    return HttpResponse.json(makeUserRead({ id, role }));
  }),

  // GET /api/admin/records - Paginated. Includes soft-deleted records
  // for moderation; the SPA filters via include_deleted query param.
  http.get("/api/admin/records", () => {
    const items = makeConnectionList(10);
    return HttpResponse.json(makePaginatedConnections(items));
  }),

  // DELETE /api/admin/records/:id - Hard delete; 204 No Content.
  http.delete("/api/admin/records/:id", () => new HttpResponse(null, { status: 204 })),

  // GET /api/admin/analytics - Composite analytics response.
  http.get("/api/admin/analytics", () => HttpResponse.json(makeAnalyticsResponse())),
];

// ---------------------------------------------------------------------------
// Per-test override factories
// ---------------------------------------------------------------------------

/**
 * Per-test override factories. Each returns a single HttpHandler that
 * tests inject via `server.use(overrides.<group>.<scenario>())` to
 * swap a default handler's behavior.
 *
 * Groups:
 *   api           - session and authentication state (/api/me)
 *   auth          - login / logout flows
 *   connections   - connection CRUD/status/history endpoints
 *   duplicateCheck - duplicate-check endpoint
 *   notes         - AI note generation
 *   tags          - tag CRUD
 *   admin         - admin user/record/analytics
 *
 * Naming convention:
 *   <verb><Status>(...)   e.g., list500(), detail404(id), duplicateFound(id)
 */
export const overrides = {
  // -------------------------------------------------------------------
  // /api/me - Session
  // -------------------------------------------------------------------
  api: {
    /** Default 401 explicitly (useful when overriding back to default mid-test). */
    me401: (): HttpHandler =>
      http.get("/api/me", () => errorEnvelope(401, "unauthorized", "Not authenticated")),

    /** Authenticated session for any role. Pass a partial session to override. */
    me200: (session?: Parameters<typeof makeSessionRead>[0]): HttpHandler =>
      http.get("/api/me", () => HttpResponse.json(makeSessionRead(session))),

    /** Server error on session probe. */
    me500: (): HttpHandler =>
      http.get("/api/me", () => errorEnvelope(500, "internal_error", "Internal server error")),
  },

  // -------------------------------------------------------------------
  // /auth/login and /auth/logout - Login/logout flow
  // -------------------------------------------------------------------
  auth: {
    /** Invalid credentials. Returns 401 per backend pydantic. */
    loginInvalidCredentials: (): HttpHandler =>
      http.post("/auth/login", () =>
        errorEnvelope(401, "invalid_credentials", "Invalid credentials"),
      ),

    /** Validation error (e.g., malformed email). Returns 422. */
    loginValidationError: (
      fields: ErrorField[] = [{ field: "email", message: "Invalid email format" }],
    ): HttpHandler =>
      http.post("/auth/login", () =>
        errorEnvelope(422, "validation_error", "Request validation failed", fields),
      ),

    /** Server error on login. */
    login500: (): HttpHandler =>
      http.post("/auth/login", () => errorEnvelope(500, "internal_error", "Internal server error")),

    /** Successful login that returns a specific user. */
    loginSuccess: (user?: Parameters<typeof makeUserRead>[0]): HttpHandler =>
      http.post("/auth/login", () => HttpResponse.json({ user: makeUserRead(user) })),

    /** Logout returns 401 (e.g., session already expired). The SPA tolerates this. */
    logout401: (): HttpHandler =>
      http.post("/auth/logout", () => errorEnvelope(401, "unauthorized", "Not authenticated")),

    /** Logout server error. */
    logout500: (): HttpHandler =>
      http.post("/auth/logout", () =>
        errorEnvelope(500, "internal_error", "Internal server error"),
      ),
  },

  // -------------------------------------------------------------------
  // /api/connections - CRUD/status/history
  // -------------------------------------------------------------------
  connections: {
    /** Empty list response. */
    listEmpty: (): HttpHandler =>
      http.get("/api/connections", () =>
        HttpResponse.json(makePaginatedConnections([], { total: 0 })),
      ),

    /** Server error on list. */
    list500: (): HttpHandler =>
      http.get("/api/connections", () =>
        errorEnvelope(500, "internal_error", "Internal server error"),
      ),

    /** Detail not found. */
    detail404: (id: string): HttpHandler =>
      http.get(`/api/connections/${id}`, () =>
        errorEnvelope(404, "not_found", "Connection record not found"),
      ),

    /** Forbidden on detail. */
    detail403: (id: string): HttpHandler =>
      http.get(`/api/connections/${id}`, () => errorEnvelope(403, "forbidden", "Forbidden")),

    /** Validation error on create (422 with field errors). */
    create422: (fields: ErrorField[]): HttpHandler =>
      http.post("/api/connections", () =>
        errorEnvelope(422, "validation_error", "Request validation failed", fields),
      ),

    /** Status mutation forbidden (e.g., Contributor trying to set status). */
    statusForbidden: (id: string): HttpHandler =>
      http.patch(`/api/connections/${id}/status`, () =>
        errorEnvelope(403, "forbidden", "Only Sales Reps and Admins may change status"),
      ),

    /** Soft-delete forbidden. */
    deleteForbidden: (id: string): HttpHandler =>
      http.delete(`/api/connections/${id}`, () => errorEnvelope(403, "forbidden", "Forbidden")),

    /** History server error. */
    history500: (id: string): HttpHandler =>
      http.get(`/api/connections/${id}/history`, () =>
        errorEnvelope(500, "internal_error", "Internal server error"),
      ),

    /** Empty history. */
    historyEmpty: (id: string): HttpHandler =>
      http.get(`/api/connections/${id}/history`, () =>
        HttpResponse.json(makePaginatedHistory([], { total: 0 })),
      ),
  },

  // -------------------------------------------------------------------
  // /api/connections/duplicate-check - F-010
  // -------------------------------------------------------------------
  duplicateCheck: {
    /** Returns "no duplicate found" explicitly. */
    notDuplicate: (): HttpHandler =>
      http.get("/api/connections/duplicate-check", () =>
        HttpResponse.json(makeDuplicateCheckResponse()),
      ),

    /** Returns a duplicate match with the supplied existing record id. */
    duplicateFound: (existingId: string, ownerName?: string): HttpHandler =>
      http.get("/api/connections/duplicate-check", () =>
        HttpResponse.json(
          makeDuplicateCheckResponse({
            duplicate_found: true,
            existing_record_id: existingId,
            existing_owner_display_name: ownerName ?? "Test Owner",
            existing_submission_date: "2026-04-01T00:00:00+00:00",
          }),
        ),
      ),

    /** Server error on duplicate check. */
    error500: (): HttpHandler =>
      http.get("/api/connections/duplicate-check", () =>
        errorEnvelope(500, "internal_error", "Internal server error"),
      ),
  },

  // -------------------------------------------------------------------
  // /api/notes/generate - F-002
  // -------------------------------------------------------------------
  notes: {
    /** AI service timed out (5s budget exceeded). Returns 504 "ai_timeout". */
    timeout504: (): HttpHandler =>
      http.post("/api/notes/generate", () =>
        errorEnvelope(504, "ai_timeout", "AI service timed out"),
      ),

    /** AI service unavailable (e.g., API key invalid). Returns 503 "ai_unavailable". */
    unavailable503: (): HttpHandler =>
      http.post("/api/notes/generate", () =>
        errorEnvelope(503, "ai_unavailable", "AI service unavailable"),
      ),

    /** Validation error on relationship_context. */
    validation422: (
      fields: ErrorField[] = [
        { field: "relationship_context", message: "Required field is missing" },
      ],
    ): HttpHandler =>
      http.post("/api/notes/generate", () =>
        errorEnvelope(422, "validation_error", "Request validation failed", fields),
      ),

    /** Generic 500 (hard failure; SPA shows toast). */
    error500: (): HttpHandler =>
      http.post("/api/notes/generate", () =>
        errorEnvelope(500, "internal_error", "Internal server error"),
      ),

    /** Successful response with a specific text body. */
    success: (notes: string): HttpHandler =>
      http.post("/api/notes/generate", () =>
        HttpResponse.json(makeGenerateNotesResponse({ ai_notes: notes })),
      ),
  },

  // -------------------------------------------------------------------
  // /api/tags - F-008
  // -------------------------------------------------------------------
  tags: {
    /** Empty tag list. */
    listEmpty: (): HttpHandler => http.get("/api/tags", () => HttpResponse.json([])),

    /** Server error on list. */
    list500: (): HttpHandler =>
      http.get("/api/tags", () => errorEnvelope(500, "internal_error", "Internal server error")),

    /** Tag create returns 200 with an existing tag (duplicate / idempotent). */
    createDuplicate: (existing: ReturnType<typeof makeTagRead>): HttpHandler =>
      http.post("/api/tags", () => HttpResponse.json(existing, { status: 200 })),
  },

  // -------------------------------------------------------------------
  // /api/admin - F-014
  // -------------------------------------------------------------------
  admin: {
    /** Admin endpoint forbidden (caller is not Admin). */
    usersForbidden: (): HttpHandler =>
      http.get("/api/admin/users", () =>
        errorEnvelope(403, "forbidden", "Forbidden: Admin role required"),
      ),

    /** Empty admin user list. */
    usersEmpty: (): HttpHandler => http.get("/api/admin/users", () => HttpResponse.json([])),

    /** Self-demotion blocked. Returns 403 "self_demotion". */
    roleUpdateSelfDemotion: (id: string): HttpHandler =>
      http.patch(`/api/admin/users/${id}`, () =>
        errorEnvelope(403, "self_demotion", "Cannot demote yourself"),
      ),

    /** Last-admin lockout. Returns 409 "last_admin_lockout". */
    roleUpdateLastAdmin: (id: string): HttpHandler =>
      http.patch(`/api/admin/users/${id}`, () =>
        errorEnvelope(409, "last_admin_lockout", "Cannot demote the last Admin"),
      ),

    /** Hard delete forbidden. */
    hardDeleteForbidden: (id: string): HttpHandler =>
      http.delete(`/api/admin/records/${id}`, () =>
        errorEnvelope(403, "forbidden", "Forbidden: Admin role required"),
      ),

    /** Hard delete server error. */
    hardDelete500: (id: string): HttpHandler =>
      http.delete(`/api/admin/records/${id}`, () =>
        errorEnvelope(500, "internal_error", "Internal server error"),
      ),

    /** Analytics 500. */
    analytics500: (): HttpHandler =>
      http.get("/api/admin/analytics", () =>
        errorEnvelope(500, "internal_error", "Internal server error"),
      ),

    /** Custom admin records response. */
    recordsCustom: (records: AdminRecordsResponse): HttpHandler =>
      http.get("/api/admin/records", () => HttpResponse.json(records)),
  },
} as const;
