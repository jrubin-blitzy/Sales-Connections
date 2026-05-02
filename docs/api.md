# REST API Catalog

This document is the canonical catalog of every REST endpoint exposed by the Sales-Connections backend. The catalog mirrors the Flask blueprints under `backend/app/api/` and the pydantic schemas under `backend/app/schemas/`. There is no auto-generated OpenAPI document in MVP; this file is authoritative. For RBAC details see `security.md`; for state machines see `architecture.md`. Endpoint discrepancies between this catalog and the implementation are documentation defects to be reported via a pull request.

This file is a reference document, not a tutorial. Each endpoint subsection follows the same fixed shape: HTTP method and path heading, RBAC requirement, brief description, request schema (JSON example plus pydantic class name), response schema (JSON example plus pydantic class name), errors table, audit-event emission note, and (where applicable) performance budget. JSON examples use 2-space indentation. Status codes are HTTP standard. No emoji are used anywhere in this document.

## Table of Contents

- [Conventions](#conventions)
- [Authentication (`/auth/*`, `/api/me`)](#authentication-auth-apime)
- [Connections (`/api/connections`)](#connections-apiconnections)
- [Notes (`/api/notes`)](#notes-apinotes)
- [Tags (`/api/tags`)](#tags-apitags)
- [Admin (`/api/admin/*`)](#admin-apiadmin)
- [Health and Observability (`/healthz`, `/readyz`, `/metrics`)](#health-and-observability-healthz-readyz-metrics)
- [Appendix A: Schema Reference](#appendix-a-schema-reference)
- [See Also](#see-also)

## Conventions

The conventions below apply uniformly to every endpoint documented in this catalog. Deviations are called out inline at the point of deviation; absence of a deviation note means the convention applies as-is.

### Base URL

The base URL is environment-specific. The frontend references `VITE_API_BASE_URL` from `frontend/.env`; the backend has no opinion about the base URL because Flask routes are mounted at the root.

| Environment | Base URL | Notes |
|-------------|----------|-------|
| Local development | `http://localhost:5000` | Backend container port published to the host by `docker-compose.yml`. |
| Staging | `https://staging.sales-connections.example.com` | TLS terminated at the staging ALB with an ACM certificate. |
| Production | `https://sales-connections.example.com` | TLS terminated at the production ALB with an ACM certificate. |

Path prefixes used by this catalog: `/auth/*` for authentication endpoints (anonymous), `/api/*` for authenticated REST endpoints, and `/healthz`, `/readyz`, `/metrics` for observability endpoints (anonymous; restricted to private VPC traffic in production via security groups).

### Authentication

All `/api/*` endpoints require a valid HttpOnly session cookie containing the JWT minted by `POST /auth/login` or `GET /auth/google/callback`. The cookie attributes are `HttpOnly; Secure; SameSite=Lax; Path=/`. The JWT carries three claims relevant to authorization: `user_id`, `org_id`, and `role`. The session expires 8 hours after issuance; expired tokens are rejected with HTTP 401 `unauthorized`.

The `/auth/*` endpoints are anonymous because their purpose is to issue or invalidate a session. The `/healthz`, `/readyz`, and `/metrics` endpoints are also anonymous; production deployments restrict these endpoints to private VPC traffic via the ECS task security group.

`GET /api/me` is the canonical endpoint for hydrating the SPA's `AuthProvider` on page load. It is the only `/api/*` endpoint whose purpose is to confirm a session without performing any business operation.

### Content type

All requests with bodies use `Content-Type: application/json`. All responses are `application/json` except the metrics endpoint, which returns `text/plain; version=0.0.4` per the Prometheus exposition format.

Requests that omit `Content-Type: application/json` on a body-bearing endpoint are rejected with HTTP 400 `bad_request`. Requests that send a malformed JSON body are also rejected with HTTP 400 `bad_request`. Requests that send a JSON body that fails pydantic validation are rejected with HTTP 422 `validation_error`; the `error.fields` array enumerates per-field errors.

### Correlation IDs

Clients SHOULD send `X-Correlation-Id` on every request. If the header is absent, the server generates a UUID v4 in the correlation middleware (`backend/app/middleware/correlation.py`). The correlation ID is reflected in the `error.correlation_id` field of every error envelope and is bound into the structlog context for every log line emitted while handling the request. The correlation ID is also propagated as the OpenTelemetry trace ID (or recorded as a span attribute on the inbound trace).

The frontend fetch wrapper at `frontend/src/api/client.ts` generates a per-request correlation ID from `frontend/src/lib/correlationId.ts` and attaches it to every outbound request. Operators correlating CloudWatch log entries with user-reported issues should ask the user for the correlation ID surfaced in the toast or error dialog.

### Pagination

List endpoints return a uniform envelope:

```json
{
  "items": [],
  "next_cursor": null,
  "total": 0
}
```

The pagination contract is:

- `items` — the page of records, in the requested sort order.
- `next_cursor` — opaque cursor string for the next page; `null` when no further pages exist. Pass `?cursor=<value>` on the next request to fetch the subsequent page.
- `total` — the total record count matching the filter set; computed once per cursor traversal and may be cached across page requests.
- `?limit=<n>` — page size; default 50, maximum 200. Values above 200 are clamped to 200.

The cursor is opaque and clients MUST NOT parse or modify it. The cursor format is server-implementation-private.

### Filtering and sorting

List endpoints accept feature-specific query parameters. Each list endpoint documents its filters and sort dimensions inline. Multi-value filters are expressed by repeating the query parameter (`?tag_ids=a&tag_ids=b`); range filters use a `_from` / `_to` suffix pair (`?submission_date_from=2026-01-01&submission_date_to=2026-12-31`).

Sort dimensions are documented per-endpoint; ascending is the default. Prefix the sort key with `-` to sort descending: `?sort=-submission_date` sorts by `submission_date` in descending order.

### Error envelope

Every non-2xx response uses the same envelope shape, regardless of which handler produced it. The envelope is emitted by the error-handler middleware (`backend/app/middleware/error_handlers.py`) for all known exception classes, and by the catch-all 500 handler for unanticipated exceptions.

```json
{
  "error": {
    "code": "...",
    "message": "...",
    "correlation_id": "...",
    "fields": []
  }
}
```

- `error.code` — stable machine-readable identifier; clients SHOULD branch on this value.
- `error.message` — human-readable summary; intended for log lines and developer tools, not for end-user display.
- `error.correlation_id` — the per-request correlation ID, identical to the value bound in structured logs and traces.
- `error.fields` — array of per-field validation errors; populated only for `validation_error` (422). Each entry has the shape `{ "field": "...", "message": "...", "code": "..." }`.

### Standard error codes

The following table enumerates every `error.code` that may appear in the envelope. Endpoint-specific subsections add no new codes; they reuse the codes listed here.

| HTTP | error.code | When |
|------|------------|------|
| 400 | `bad_request` | Malformed JSON body, missing required header, or invalid `Content-Type` |
| 401 | `unauthorized` | Missing session cookie, expired JWT, or signature verification failure |
| 401 | `invalid_credentials` | Email/password login failed verification |
| 401 | `invalid_id_token` | Google ID token signature verification failed during OAuth callback |
| 400 | `invalid_state` | OAuth `state` mismatch during callback |
| 403 | `forbidden` | RBAC denied; cross-org access attempted; owner-scope check failed |
| 404 | `not_found` | Resource does not exist, has been soft-deleted (for non-admin callers), or is in a different organization |
| 409 | `conflict` | Resource state conflict (for example, tag name already exists in the org) |
| 422 | `validation_error` | pydantic validation failed; `error.fields` enumerates per-field errors |
| 429 | `rate_limited` | Reserved; rate limiting is not implemented in MVP per AAP §0.6.2 |
| 500 | `internal_error` | Unexpected server error; correlation ID present in CloudWatch logs |
| 502 | `ai_upstream_error` | Anthropic Claude API returned a non-2xx response |
| 503 | `not_ready` | Readiness probe detected database connectivity failure |
| 504 | `ai_timeout` | AI request exceeded the 5-second budget enforced by the timeout watchdog |

The 429 `rate_limited` code is reserved so that future addition of rate limiting does not require a breaking client-side change; in MVP no endpoint emits this code.

## Authentication (`/auth/*`, `/api/me`)

The authentication blueprint is implemented in `backend/app/api/auth.py` with helpers in `backend/app/services/auth.py`. The blueprint provides two authentication mechanisms: Google OAuth 2.0 with PKCE (preferred) and email/password (fallback). Both mechanisms terminate in the same session cookie carrying the same JWT format. Logout invalidates the cookie and advances the per-user signing-key version so the prior token cannot be reused. Session hydration on page load is handled by `GET /api/me`.

### POST /auth/login

Email/password login. Used as a fallback when Google OAuth is unavailable or when the user has not linked a Google account.

- RBAC: anonymous (does not require an existing session).
- Performance budget: ≤ 2 seconds end-to-end (AAP §0.7.3).

Request body — pydantic class `LoginRequest`:

```json
{
  "email": "user@example.com",
  "password": "correct horse battery staple"
}
```

Success response — HTTP 200, pydantic class `UserRead` wrapped under `user`. The response also sets the session cookie via `Set-Cookie: session=<jwt>; HttpOnly; Secure; SameSite=Lax; Path=/`.

```json
{
  "user": {
    "id": "11111111-2222-3333-4444-555555555555",
    "email": "user@example.com",
    "display_name": "Pat User",
    "role": "Contributor",
    "org_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    "created_at": "2026-04-23T18:00:00Z"
  }
}
```

Errors:

| HTTP | error.code | Cause |
|------|------------|-------|
| 401 | `invalid_credentials` | Email not found or bcrypt verification failed |
| 422 | `validation_error` | Malformed email, missing field, or empty password |

Audit emission: `event_type = authentication`, `actor_user_id = user.id`, `target_record_id = null`, `before_payload = null`, `after_payload = { "method": "password" }`. The audit row is INSERTed in the same transaction as the session cookie issuance.

### POST /auth/logout

End the current session. Clears the cookie and advances the per-user signing-key version to immediately invalidate any other token that was minted before logout.

- RBAC: authenticated (any role).

Request body: empty object.

```json
{}
```

Success response — HTTP 200. The response also clears the session cookie via `Set-Cookie: session=; Max-Age=0; HttpOnly; Secure; SameSite=Lax; Path=/`.

```json
{
  "ok": true
}
```

Errors: none expected from the handler beyond standard 401 if no session cookie was presented; the middleware short-circuits before the handler runs.

Audit emission: `event_type = authentication`, `actor_user_id = session.user_id`, `target_record_id = null`, `before_payload = null`, `after_payload = { "event": "logout" }`.

### GET /auth/google/start

Begin the Google OAuth 2.0 authorization-code flow with PKCE. Returns an HTTP 302 redirect to Google's authorization endpoint; the SPA performs a full-page navigation to this URL by setting `window.location.href`.

- RBAC: anonymous.

Request: no body, no query parameters.

Behavior:

- Generates a fresh `state` (CSRF token) and PKCE `code_verifier` and `code_challenge` (S256).
- Persists `state` and `code_verifier` in a short-lived signed cookie (`SameSite=Lax`, expires in 10 minutes).
- Redirects to `https://accounts.google.com/o/oauth2/v2/auth?...` with the standard query parameters (`client_id`, `redirect_uri`, `response_type=code`, `scope=openid email profile`, `state`, `code_challenge`, `code_challenge_method=S256`).

Success response — HTTP 302 with `Location: https://accounts.google.com/o/oauth2/v2/auth?...`.

Errors: none expected; configuration errors at startup prevent the route from being registered.

Audit emission: none (no state change yet; the audit event is emitted on the callback when the user is upserted and a session is minted).

### GET /auth/google/callback

Complete the Google OAuth 2.0 authorization-code flow. Google redirects the user's browser back to this endpoint with `code` and `state` query parameters; the server validates state, exchanges the code for tokens, validates the ID token, upserts the user, mints a session JWT, and sets the session cookie.

- RBAC: anonymous (the request carries `state` and `code` from Google but no prior session).

Query parameters:

- `code` — authorization code issued by Google.
- `state` — CSRF token previously persisted in the signed `state` cookie.

Success response — HTTP 302 with `Location: /feed`. The response sets the session cookie via `Set-Cookie: session=<jwt>; HttpOnly; Secure; SameSite=Lax; Path=/`.

Errors:

| HTTP | error.code | Cause |
|------|------------|-------|
| 400 | `invalid_state` | The `state` query parameter does not match the value persisted in the signed cookie |
| 401 | `invalid_id_token` | The Google ID token signature failed verification against Google's JWKS |
| 422 | `validation_error` | `code` or `state` query parameter is missing |

Audit emission: `event_type = authentication`, `actor_user_id = user.id` (the upserted user), `target_record_id = null`, `before_payload = null`, `after_payload = { "method": "google" }`. The audit row is INSERTed in the same transaction as the user upsert and session cookie issuance.

### GET /api/me

Hydrate the SPA's session context on page load. The frontend `AuthProvider` (`frontend/src/auth/AuthProvider.tsx`) calls this endpoint on mount; the response populates `useSession()` and `useRole()`.

- RBAC: authenticated (any role).

Request: no body, no query parameters.

Success response — HTTP 200, pydantic class `UserRead` wrapped under `user`. Mirrors the success body of `POST /auth/login`.

```json
{
  "user": {
    "id": "11111111-2222-3333-4444-555555555555",
    "email": "user@example.com",
    "display_name": "Pat User",
    "role": "Contributor",
    "org_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    "created_at": "2026-04-23T18:00:00Z"
  }
}
```

Errors:

| HTTP | error.code | Cause |
|------|------------|-------|
| 401 | `unauthorized` | No session cookie present, or JWT failed signature verification, or JWT is expired |

Audit emission: none (read).

## Connections (`/api/connections`)

The connections blueprint is implemented in `backend/app/api/connections.py` with helpers in `backend/app/services/connections.py`, `backend/app/services/duplicate_detection.py`, and `backend/app/services/audit.py`. The blueprint covers eight endpoints spanning create, read (list and detail), update (full and status-only), soft delete, history, and pre-submit duplicate detection. Every state-changing endpoint emits an audit event in the same database transaction as its data mutation.

All endpoints are org-scoped: every read and write injects `WHERE org_id = g.session.org_id`. Cross-org access returns HTTP 404 (so that record existence is not leaked across organizational boundaries) for read paths, and HTTP 403 `forbidden` for write paths where the request reaches the handler before the org-scoping check.

All read paths default to `WHERE deleted_at IS NULL`. Soft-deleted records are visible only via the admin-only `GET /api/admin/records?include_deleted=true` endpoint.

### POST /api/connections

Create a new connection record. Implements feature F-001 (Connection Idea Form). The handler persists the record, derives `owner_user_id` from `g.session.user_id` (any client-supplied owner field is rejected at the pydantic layer), normalizes the LinkedIn URL via `backend/app/utils/url.py`, and emits an audit event in the same transaction.

- RBAC: Admin or Contributor.
- Performance budget: ≤ 2 seconds end-to-end excluding any AI generation that the SPA may have performed prior (AAP §0.7.3).

Request body — pydantic class `ConnectionCreate`:

```json
{
  "full_name": "Jordan Example",
  "linkedin_url": "https://www.linkedin.com/in/jordanexample/",
  "company": "Acme Logistics",
  "job_title": "VP of Operations",
  "relationship_context": "We went to college together, he's now VP of Ops at a Series B logistics startup",
  "ai_notes": "Reach out warmly; reference shared alma mater. Offer perspective on operational scaling at Series B.",
  "involvement": "Warm Intro",
  "tag_ids": [
    "11111111-1111-1111-1111-111111111111",
    "22222222-2222-2222-2222-222222222222"
  ],
  "submission_date": "2026-04-23"
}
```

Field notes:

- `linkedin_url` — required; must match the LinkedIn URL format (validated via `backend/app/utils/url.py::is_valid_linkedin_url`). The server stores both the original value and a normalized form (`backend/app/utils/url.py::normalize_linkedin_url`) for duplicate detection.
- `ai_notes` — optional, nullable. The contributor may submit empty `ai_notes` if AI generation timed out or returned an upstream error.
- `involvement` — required; must be one of the three enum values `"Warm Intro"`, `"Soft Reference"`, `"Target Only"`.
- `tag_ids` — optional; array of UUIDs referencing tags belonging to the same organization.
- `submission_date` — optional; defaulted server-side to the current UTC date if absent.
- Forbidden fields: any payload containing `owner_user_id`, `owner_display_name`, `org_id`, `id`, `outreach_status`, `created_at`, or `deleted_at` is rejected with HTTP 422 `validation_error`. The owner is always derived from the session.

Success response — HTTP 201, pydantic class `ConnectionRead`:

```json
{
  "id": "33333333-3333-3333-3333-333333333333",
  "full_name": "Jordan Example",
  "linkedin_url": "https://www.linkedin.com/in/jordanexample/",
  "normalized_linkedin_url": "linkedin.com/in/jordanexample",
  "company": "Acme Logistics",
  "job_title": "VP of Operations",
  "relationship_context": "We went to college together, he's now VP of Ops at a Series B logistics startup",
  "ai_notes": "Reach out warmly; reference shared alma mater. Offer perspective on operational scaling at Series B.",
  "involvement": "Warm Intro",
  "outreach_status": "Not Started",
  "owner_user_id": "11111111-2222-3333-4444-555555555555",
  "owner_display_name": "Pat User",
  "org_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
  "tag_ids": [
    "11111111-1111-1111-1111-111111111111",
    "22222222-2222-2222-2222-222222222222"
  ],
  "submission_date": "2026-04-23",
  "created_at": "2026-04-23T18:00:00Z",
  "deleted_at": null
}
```

Errors:

| HTTP | error.code | Cause |
|------|------------|-------|
| 401 | `unauthorized` | Missing or invalid session cookie |
| 403 | `forbidden` | Caller is a Viewer (Sales Rep) — Viewers cannot create records |
| 422 | `validation_error` | LinkedIn URL malformed; required field missing; client supplied a forbidden field; tag UUID does not belong to the organization |

Audit emission: `event_type = create`, `actor_user_id = session.user_id`, `target_record_id = new record id`, `before_payload = null`, `after_payload = full record snapshot`. The audit row is INSERTed in the same transaction as the record INSERT; either both succeed or both roll back.

### GET /api/connections

List connection records for the caller's organization. Implements feature F-004 (Connection Feed and Dashboard). The query is org-scoped and soft-delete-aware; it leverages the composite index `(org_id, deleted_at, submission_date DESC)` on `records` to remain responsive at 10,000 records per organization.

- RBAC: Admin, Contributor, or Viewer (any authenticated role).

Query parameters:

| Parameter | Type | Notes |
|-----------|------|-------|
| `company` | string | Substring match (case-insensitive) on `company` |
| `involvement` | enum | One of `Warm Intro`, `Soft Reference`, `Target Only` |
| `owner_user_id` | UUID | Restrict to records owned by a specific user |
| `submission_date_from` | date (ISO 8601) | Inclusive lower bound on `submission_date` |
| `submission_date_to` | date (ISO 8601) | Inclusive upper bound on `submission_date` |
| `outreach_status` | enum | One of `Not Started`, `In Progress`, `Contacted`, `Closed` |
| `tag_ids` | UUID (repeatable) | Records that include any of the specified tags ("contains-any-of" semantics) |
| `sort` | string | One of `submission_date`, `full_name`, `company`, `owner_display_name`, `outreach_status`; prefix with `-` for descending. Default: `-submission_date` |
| `cursor` | string | Opaque pagination cursor; absent on first page |
| `limit` | integer | Page size; default 50, maximum 200 |

Success response — HTTP 200:

```json
{
  "items": [
    {
      "id": "33333333-3333-3333-3333-333333333333",
      "full_name": "Jordan Example",
      "linkedin_url": "https://www.linkedin.com/in/jordanexample/",
      "normalized_linkedin_url": "linkedin.com/in/jordanexample",
      "company": "Acme Logistics",
      "job_title": "VP of Operations",
      "relationship_context": "We went to college together, he's now VP of Ops at a Series B logistics startup",
      "ai_notes": "Reach out warmly; reference shared alma mater.",
      "involvement": "Warm Intro",
      "outreach_status": "Not Started",
      "owner_user_id": "11111111-2222-3333-4444-555555555555",
      "owner_display_name": "Pat User",
      "org_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
      "tag_ids": [
        "11111111-1111-1111-1111-111111111111"
      ],
      "submission_date": "2026-04-23",
      "created_at": "2026-04-23T18:00:00Z",
      "deleted_at": null
    }
  ],
  "next_cursor": "eyJsYXN0X2lkIjoiMzMzMyJ9",
  "total": 1247
}
```

Errors:

| HTTP | error.code | Cause |
|------|------------|-------|
| 401 | `unauthorized` | Missing or invalid session cookie |
| 422 | `validation_error` | Query parameter type or enum mismatch; invalid date format |

Audit emission: none (read).

### GET /api/connections/:id

Retrieve a single connection record by id. Implements feature F-011 (Connection Detail View). Returns all nine business fields plus the tag list and server-side metadata.

- RBAC: Admin, Contributor, or Viewer (org-scoped).

Path parameters:

| Parameter | Type | Notes |
|-----------|------|-------|
| `id` | UUID | Record identifier |

Success response — HTTP 200, pydantic class `ConnectionRead`. Identical shape to the `items` element of `GET /api/connections`.

Errors:

| HTTP | error.code | Cause |
|------|------------|-------|
| 401 | `unauthorized` | Missing or invalid session cookie |
| 404 | `not_found` | Record does not exist, has been soft-deleted, or belongs to a different organization |

Audit emission: none (read).

### GET /api/connections/:id/history

Retrieve the audit-event history for a connection record. Implements features F-011 (Connection Detail View with Edit History) and F-013 (Audit Trail). The result set is sourced from the `audit_events` table filtered by `target_record_id` and ordered by `event_timestamp DESC`.

- RBAC: Admin, Contributor, or Viewer (org-scoped).

Path parameters:

| Parameter | Type | Notes |
|-----------|------|-------|
| `id` | UUID | Record identifier |

Query parameters:

| Parameter | Type | Notes |
|-----------|------|-------|
| `cursor` | string | Opaque pagination cursor; absent on first page |
| `limit` | integer | Page size; default 50, maximum 200 |

Success response — HTTP 200:

```json
{
  "items": [
    {
      "id": "44444444-4444-4444-4444-444444444444",
      "event_type": "status_change",
      "event_timestamp": "2026-04-24T09:15:32Z",
      "actor_user_id": "66666666-6666-6666-6666-666666666666",
      "actor_display_name": "Sam Sales",
      "target_record_id": "33333333-3333-3333-3333-333333333333",
      "before_payload": {
        "outreach_status": "Not Started"
      },
      "after_payload": {
        "outreach_status": "In Progress"
      }
    },
    {
      "id": "55555555-5555-5555-5555-555555555555",
      "event_type": "create",
      "event_timestamp": "2026-04-23T18:00:00Z",
      "actor_user_id": "11111111-2222-3333-4444-555555555555",
      "actor_display_name": "Pat User",
      "target_record_id": "33333333-3333-3333-3333-333333333333",
      "before_payload": null,
      "after_payload": {
        "full_name": "Jordan Example",
        "company": "Acme Logistics",
        "job_title": "VP of Operations",
        "involvement": "Warm Intro",
        "outreach_status": "Not Started"
      }
    }
  ],
  "next_cursor": null,
  "total": 2
}
```

Errors:

| HTTP | error.code | Cause |
|------|------------|-------|
| 401 | `unauthorized` | Missing or invalid session cookie |
| 404 | `not_found` | Record does not exist, has been soft-deleted (for non-admin callers), or belongs to a different organization |

Audit emission: none (read).

### PATCH /api/connections/:id

Edit a connection record. Implements feature F-007 (Record Editing). The handler accepts any subset of editable fields and emits an audit event capturing the diff. Owner-scoping is enforced at the service layer: a Contributor may edit only records they own; an Admin may edit any record in the organization.

- RBAC: Admin (any record) or Contributor (own record only).

Path parameters:

| Parameter | Type | Notes |
|-----------|------|-------|
| `id` | UUID | Record identifier |

Request body — pydantic class `ConnectionUpdate`. All fields are optional; the request body must contain at least one editable field. Owner-related fields and immutable fields (`id`, `org_id`, `created_at`, `outreach_status`, `deleted_at`) are rejected.

```json
{
  "full_name": "Jordan O. Example",
  "company": "Acme Logistics, Inc.",
  "job_title": "Senior VP of Operations",
  "ai_notes": "Updated outreach angle: focus on logistics tech stack.",
  "involvement": "Soft Reference",
  "tag_ids": [
    "11111111-1111-1111-1111-111111111111",
    "33333333-3333-3333-3333-333333333333"
  ]
}
```

Success response — HTTP 200, pydantic class `ConnectionRead`. Identical shape to the response of `POST /api/connections`.

Errors:

| HTTP | error.code | Cause |
|------|------------|-------|
| 401 | `unauthorized` | Missing or invalid session cookie |
| 403 | `forbidden` | Contributor attempted to edit a record owned by another user; Viewer attempted to edit any record |
| 404 | `not_found` | Record does not exist, has been soft-deleted, or belongs to a different organization |
| 422 | `validation_error` | LinkedIn URL malformed; client supplied a forbidden field; empty body |

Audit emission: `event_type = edit`, `actor_user_id = session.user_id`, `target_record_id = id`, `before_payload = { changed fields' old values }`, `after_payload = { changed fields' new values }`. Only fields that actually changed are included in the payloads.

### PATCH /api/connections/:id/status

Update the outreach status of a connection record. Implements feature F-005 (Outreach Status Tracking). This endpoint has the most surprising RBAC rule in the catalog: Contributors are explicitly forbidden, even on records they own, to preserve sales team accountability per AAP §0.1.2. The contract is that contributors capture leads and sales reps work them; status mutation is reserved for the sales team.

- RBAC: Admin or Viewer (Sales Rep) ONLY. Contributor is forbidden regardless of record ownership.

Path parameters:

| Parameter | Type | Notes |
|-----------|------|-------|
| `id` | UUID | Record identifier |

Request body — pydantic class `ConnectionStatusUpdate`:

```json
{
  "outreach_status": "In Progress"
}
```

The `outreach_status` field is required and must be one of the four enum values: `"Not Started"`, `"In Progress"`, `"Contacted"`, `"Closed"`.

Success response — HTTP 200, pydantic class `ConnectionRead`. The returned record reflects the new status.

Errors:

| HTTP | error.code | Cause |
|------|------------|-------|
| 401 | `unauthorized` | Missing or invalid session cookie |
| 403 | `forbidden` | Caller is a Contributor (regardless of record ownership) |
| 404 | `not_found` | Record does not exist, has been soft-deleted, or belongs to a different organization |
| 422 | `validation_error` | `outreach_status` value is not in the four-value enum |

Audit emission: `event_type = status_change`, `actor_user_id = session.user_id`, `target_record_id = id`, `before_payload = { "outreach_status": "<old value>" }`, `after_payload = { "outreach_status": "<new value>" }`.

### DELETE /api/connections/:id

Soft delete a connection record. Implements feature F-007 (Record Editing and Soft Deletion). Sets `deleted_at = NOW()`; the record remains in the database and is recoverable by an Admin. Hard delete is exposed only via the admin endpoint `DELETE /api/admin/records/:id`.

- RBAC: Admin (any record), Contributor (own record only), or Viewer (own record only).

Path parameters:

| Parameter | Type | Notes |
|-----------|------|-------|
| `id` | UUID | Record identifier |

Request body: none.

Success response — HTTP 204, empty body.

Errors:

| HTTP | error.code | Cause |
|------|------------|-------|
| 401 | `unauthorized` | Missing or invalid session cookie |
| 403 | `forbidden` | Non-admin caller attempted to soft delete a record they do not own |
| 404 | `not_found` | Record does not exist, has already been soft-deleted, or belongs to a different organization |

Audit emission: `event_type = soft_delete`, `actor_user_id = session.user_id`, `target_record_id = id`, `before_payload = { "deleted_at": null }`, `after_payload = { "deleted_at": "<ISO 8601 timestamp>" }`.

### GET /api/connections/duplicate-check

Pre-submit warning for duplicate LinkedIn URLs. Implements feature F-010 (Duplicate LinkedIn URL Detection). Returns a non-blocking signal indicating whether a record with a matching normalized LinkedIn URL already exists in the caller's organization. The frontend `DuplicateWarning.tsx` component renders a banner; the user may proceed regardless.

- RBAC: Admin or Contributor (org-scoped).
- Performance budget: sub-second at 10,000 records per organization, leveraging the unique partial index `(org_id, normalized_linkedin_url) WHERE deleted_at IS NULL` (AAP §0.7.3).

Query parameters:

| Parameter | Type | Notes |
|-----------|------|-------|
| `linkedin_url` | string | Raw URL; the server normalizes before lookup |

Success response — HTTP 200:

```json
{
  "duplicate": true,
  "existing_record_id": "33333333-3333-3333-3333-333333333333"
}
```

When no duplicate exists:

```json
{
  "duplicate": false,
  "existing_record_id": null
}
```

Errors:

| HTTP | error.code | Cause |
|------|------------|-------|
| 401 | `unauthorized` | Missing or invalid session cookie |
| 403 | `forbidden` | Caller is a Viewer (Sales Rep) |
| 422 | `validation_error` | LinkedIn URL malformed |

Audit emission: none (read; warning only). The duplicate check never blocks submission; the eventual `POST /api/connections` is what records the duplicate (if any) against the audit trail.


## Notes (`/api/notes`)

The notes blueprint is implemented in `backend/app/api/notes.py` with helpers in `backend/app/services/ai_orchestration.py` (Langchain-wrapped Anthropic Claude client) and `backend/app/utils/sanitization.py` (server-side input sanitization). The blueprint exposes a single endpoint that generates AI outreach notes from a relationship context. The Anthropic SDK is imported only in `backend/app/services/ai_orchestration.py`, preserving the provider-replaceability invariant; no other module reaches the SDK directly.

### POST /api/notes/generate

Generate AI outreach notes from a relationship context. Implements feature F-002 (AI Note Generation). The handler reads the Anthropic API key from AWS Secrets Manager (cached for the worker lifetime), sanitizes the user-supplied `relationship_context` server-side, templates it into the system + user prompt, and invokes the Anthropic Claude API via Langchain with a 5-second timeout watchdog.

- RBAC: Admin or Contributor.
- Performance budget: ≤ 5 seconds P95 end-to-end (AAP §0.7.3).

Request body — pydantic class `NoteGenerationRequest`:

```json
{
  "full_name": "Jordan Example",
  "company": "Acme Logistics",
  "job_title": "VP of Operations",
  "relationship_context": "We went to college together, he's now VP of Ops at a Series B logistics startup"
}
```

Field notes:

- `relationship_context` is the only field that semantically affects the AI output; the other three fields are templated into the prompt to give Claude grounding.
- All four fields are required.
- The frontend may sanitize `relationship_context` for UX, but the server `backend/app/utils/sanitization.py` is the authoritative sanitizer: it strips control characters and applies length caps before templating into the prompt.

Success response — HTTP 200, pydantic class `NoteGenerationResponse`:

```json
{
  "ai_notes": "Reach out warmly given your shared alma mater. Acknowledge his progression to VP of Ops at a Series B logistics startup; offer perspective on operational scaling, hiring discipline, or carrier network design depending on his current focus. Keep the first message short and offer to host a 20-minute exchange."
}
```

Errors:

| HTTP | error.code | Cause |
|------|------------|-------|
| 401 | `unauthorized` | Missing or invalid session cookie |
| 403 | `forbidden` | Caller is a Viewer (Sales Rep) — Viewers cannot generate AI notes |
| 422 | `validation_error` | Required field missing; `relationship_context` exceeds maximum length after sanitization |
| 502 | `ai_upstream_error` | Anthropic Claude API returned a non-2xx response (rate-limit, transient error, account suspended) |
| 504 | `ai_timeout` | Anthropic call exceeded the 5-second budget enforced by the timeout watchdog |

Non-blocking by design: a 502 or 504 response is non-fatal. The SPA must allow the contributor to submit the form with empty `ai_notes` even when AI generation fails. The frontend `AddEditConnectionForm.tsx` component renders a "AI unavailable; you can still submit" affordance on a 502 or 504, and the contributor proceeds to `POST /api/connections` with the (possibly empty) `ai_notes` field. This is the behavior required by AAP §0.1.2: AI failure must not block form submission.

Audit emission: none. This endpoint performs no state change; it is a stateless inference call. The eventual `POST /api/connections` (if the contributor saves the form) emits the `create` audit event including the `ai_notes` value at the time of save.

## Tags (`/api/tags`)

The tags blueprint is implemented in `backend/app/api/tags.py`. Tags are scoped per-organization: a tag created by Acme Corp is invisible to other organizations. Tag uniqueness is enforced at the database layer by a composite unique index `(org_id, name)`. Tag dimensions follow the user's example: industry, use case, geography (no enforcement of which dimension a tag belongs to; the dimension is a convention, not a schema).

### GET /api/tags

List the tags belonging to the caller's organization. Implements feature F-008 (Tagging and Categorization).

- RBAC: Admin, Contributor, or Viewer (org-scoped).

Query parameters:

| Parameter | Type | Notes |
|-----------|------|-------|
| `search` | string | Substring match (case-insensitive) on `name`; absent returns all tags |
| `cursor` | string | Opaque pagination cursor; absent on first page |
| `limit` | integer | Page size; default 50, maximum 200 |

Success response — HTTP 200:

```json
{
  "items": [
    {
      "id": "11111111-1111-1111-1111-111111111111",
      "name": "Logistics",
      "org_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
      "created_at": "2026-04-23T18:00:00Z"
    },
    {
      "id": "22222222-2222-2222-2222-222222222222",
      "name": "EMEA",
      "org_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
      "created_at": "2026-04-23T18:00:00Z"
    }
  ],
  "next_cursor": null,
  "total": 2
}
```

Errors:

| HTTP | error.code | Cause |
|------|------------|-------|
| 401 | `unauthorized` | Missing or invalid session cookie |
| 422 | `validation_error` | `limit` out of range; malformed `cursor` |

Audit emission: none (read).

### POST /api/tags

Create a new tag in the caller's organization. The frontend `TagInput.tsx` component calls this endpoint when the contributor types a tag name not present in the autocomplete list.

- RBAC: Admin or Contributor.

Request body — pydantic class `TagCreate`:

```json
{
  "name": "Series B"
}
```

Field notes:

- `name` is required, trimmed of leading and trailing whitespace, and validated against a length cap (1 to 64 characters).
- Tag names are case-sensitive at the database level; the unique index treats `Logistics` and `logistics` as distinct.

Success response — HTTP 201, pydantic class `TagRead`:

```json
{
  "id": "44444444-4444-4444-4444-444444444444",
  "name": "Series B",
  "org_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
  "created_at": "2026-04-23T18:00:00Z"
}
```

Errors:

| HTTP | error.code | Cause |
|------|------------|-------|
| 401 | `unauthorized` | Missing or invalid session cookie |
| 403 | `forbidden` | Caller is a Viewer (Sales Rep) |
| 409 | `conflict` | A tag with the same `name` already exists in the caller's organization |
| 422 | `validation_error` | `name` is empty after trimming, or exceeds the length cap |

Audit emission: none. Tag creation is a low-stakes operation that is implicitly visible via the `record_tags` association on subsequent record edits; the audit trail therefore does not record tag creation independently. This convention is documented in `docs/decision-log.md`.

## Admin (`/api/admin/*`)

The admin blueprint is implemented in `backend/app/api/admin.py` with helpers in `backend/app/services/admin.py`. Every endpoint is gated by the `@requires_role('Admin')` decorator. The blueprint covers user and role management, record moderation including soft-deleted records, hard delete, and basic aggregation analytics. Implements feature F-014 (Admin Panel).

### GET /api/admin/users

List all users in the caller's organization. Powers the Users tab of the Admin Panel.

- RBAC: Admin only.

Query parameters:

| Parameter | Type | Notes |
|-----------|------|-------|
| `role` | enum | Filter by `Admin`, `Contributor`, or `Viewer` |
| `search` | string | Substring match (case-insensitive) on `email` or `display_name` |
| `cursor` | string | Opaque pagination cursor |
| `limit` | integer | Page size; default 50, maximum 200 |

Success response — HTTP 200:

```json
{
  "items": [
    {
      "id": "11111111-2222-3333-4444-555555555555",
      "email": "user@example.com",
      "display_name": "Pat User",
      "role": "Contributor",
      "org_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
      "created_at": "2026-04-23T18:00:00Z"
    },
    {
      "id": "66666666-6666-6666-6666-666666666666",
      "email": "sam@example.com",
      "display_name": "Sam Sales",
      "role": "Viewer",
      "org_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
      "created_at": "2026-04-23T18:00:00Z"
    }
  ],
  "next_cursor": null,
  "total": 2
}
```

Errors:

| HTTP | error.code | Cause |
|------|------------|-------|
| 401 | `unauthorized` | Missing or invalid session cookie |
| 403 | `forbidden` | Caller is not an Admin |

Audit emission: none (read).

### PATCH /api/admin/users/:id

Update the role of a user in the caller's organization. Implements features F-009 (Role-Based Access Control) and F-014 (Admin Panel). The handler enforces the admin-bootstrap invariant: the last remaining Admin in an organization cannot be demoted, ensuring that every organization always has at least one Admin.

- RBAC: Admin only.

Path parameters:

| Parameter | Type | Notes |
|-----------|------|-------|
| `id` | UUID | User identifier |

Request body — pydantic class `UserRoleUpdate`:

```json
{
  "role": "Viewer"
}
```

The `role` field is required and must be one of the three enum values `"Admin"`, `"Contributor"`, `"Viewer"`.

Success response — HTTP 200, pydantic class `UserRead`:

```json
{
  "id": "11111111-2222-3333-4444-555555555555",
  "email": "user@example.com",
  "display_name": "Pat User",
  "role": "Viewer",
  "org_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
  "created_at": "2026-04-23T18:00:00Z"
}
```

Errors:

| HTTP | error.code | Cause |
|------|------------|-------|
| 401 | `unauthorized` | Missing or invalid session cookie |
| 403 | `forbidden` | Caller is not an Admin; or the request would demote the last Admin in the organization |
| 404 | `not_found` | User does not exist or belongs to a different organization |
| 422 | `validation_error` | `role` value is not in the three-value enum |

Audit emission: `event_type = role_change`, `actor_user_id = session.user_id`, `target_record_id = null` (the audit row records the user id as part of the payload rather than via `target_record_id`, which is reserved for the `records` table), `before_payload = { "user_id": "...", "role": "<old role>" }`, `after_payload = { "user_id": "...", "role": "<new role>" }`.

### GET /api/admin/records

List all records in the caller's organization, optionally including soft-deleted records. Powers the Records tab of the Admin Panel and the Record Moderation flow. This is the only read endpoint that opts out of the default `WHERE deleted_at IS NULL` filter.

- RBAC: Admin only.

Query parameters: same set as `GET /api/connections` (`company`, `involvement`, `owner_user_id`, `submission_date_from`, `submission_date_to`, `outreach_status`, `tag_ids`, `sort`, `cursor`, `limit`) plus:

| Parameter | Type | Notes |
|-----------|------|-------|
| `include_deleted` | boolean | When `true` (default for this endpoint), soft-deleted records are included; when `false`, the result mirrors `GET /api/connections` |

Success response — HTTP 200. Same shape as `GET /api/connections`. Soft-deleted records have a non-null `deleted_at` field; non-deleted records have `deleted_at: null`.

Errors:

| HTTP | error.code | Cause |
|------|------------|-------|
| 401 | `unauthorized` | Missing or invalid session cookie |
| 403 | `forbidden` | Caller is not an Admin |
| 422 | `validation_error` | Query parameter type mismatch |

Audit emission: none (read).

### DELETE /api/admin/records/:id

Hard delete a record. Implements features F-007 (Record Editing and Soft Deletion) and F-014 (Admin Panel). The record row is physically removed from the `records` table; cascade rules remove rows from `record_tags`. The audit row remains as the only post-deletion forensic record; per AAP §0.7.4 the `before_payload` is a full snapshot of the record at the moment of deletion. The deletion is irreversible.

- RBAC: Admin only.

Path parameters:

| Parameter | Type | Notes |
|-----------|------|-------|
| `id` | UUID | Record identifier (may be soft-deleted or live) |

Request body: none.

Success response — HTTP 204, empty body.

Errors:

| HTTP | error.code | Cause |
|------|------------|-------|
| 401 | `unauthorized` | Missing or invalid session cookie |
| 403 | `forbidden` | Caller is not an Admin |
| 404 | `not_found` | Record does not exist or belongs to a different organization |

Audit emission: `event_type = hard_delete`, `actor_user_id = session.user_id`, `target_record_id = id`, `before_payload = full record snapshot at the moment of deletion`, `after_payload = null`. The full snapshot is the only post-deletion record of the data; operators recovering from accidental hard delete must reconstruct from the audit row.

### GET /api/admin/analytics

Basic aggregation analytics for the Admin Panel. Implements feature F-014 (Admin Panel). Returns three panels designed to answer the questions: who is contributing the most? where do leads sit in the funnel? and what is the activity trend?

- RBAC: Admin only.
- Performance budget: standard list-load budget at 10,000 records per organization. Aggregations hit the same composite indexes as the feed query.

Query parameters: none.

Success response — HTTP 200, pydantic class `AnalyticsResponse`:

```json
{
  "most_active_contributors": [
    {
      "user_id": "11111111-2222-3333-4444-555555555555",
      "display_name": "Pat User",
      "record_count": 42
    },
    {
      "user_id": "77777777-7777-7777-7777-777777777777",
      "display_name": "Casey Connector",
      "record_count": 31
    }
  ],
  "leads_by_status": {
    "Not Started": 120,
    "In Progress": 47,
    "Contacted": 22,
    "Closed": 8
  },
  "weekly_activity": [
    { "week_start_date": "2026-02-09", "record_count": 14 },
    { "week_start_date": "2026-02-16", "record_count": 18 },
    { "week_start_date": "2026-02-23", "record_count": 22 },
    { "week_start_date": "2026-03-02", "record_count": 17 },
    { "week_start_date": "2026-03-09", "record_count": 19 },
    { "week_start_date": "2026-03-16", "record_count": 25 },
    { "week_start_date": "2026-03-23", "record_count": 24 },
    { "week_start_date": "2026-03-30", "record_count": 20 },
    { "week_start_date": "2026-04-06", "record_count": 28 },
    { "week_start_date": "2026-04-13", "record_count": 31 },
    { "week_start_date": "2026-04-20", "record_count": 19 },
    { "week_start_date": "2026-04-27", "record_count": 11 }
  ]
}
```

Panel notes:

- `most_active_contributors` — top N contributors by record count in the last 90 days, ordered by `record_count` descending. The default N is 10; clients may not adjust the limit in MVP.
- `leads_by_status` — count of records grouped by `outreach_status`. Soft-deleted records are excluded.
- `weekly_activity` — array of objects covering the last 12 weeks, ordered by `week_start_date` ascending. Each `week_start_date` is the Monday of the week.

Errors:

| HTTP | error.code | Cause |
|------|------------|-------|
| 401 | `unauthorized` | Missing or invalid session cookie |
| 403 | `forbidden` | Caller is not an Admin |

Audit emission: none (read).


## Health and Observability (`/healthz`, `/readyz`, `/metrics`)

The health and observability endpoints are exposed at the root path (not under `/api`) because they are infrastructure concerns rather than user-facing application functionality. The blueprints are implemented in `backend/app/api/health.py` and `backend/app/observability/metrics.py`. All three endpoints are anonymous; production deployments restrict access via the ECS task security group so that only the ALB and the CloudWatch agent can reach them.

### GET /healthz

Liveness probe. Returns 200 unconditionally. Used by the ECS task health check (which restarts a task that fails this probe) and by the ALB target group health check (which removes a target from rotation when the probe fails).

- RBAC: anonymous.
- DB dependency: none. The handler returns immediately without touching the database; this is intentional so that a transient database outage does not cascade into restart loops on the application tier.

Request: no body, no query parameters.

Success response — HTTP 200:

```json
{
  "status": "ok"
}
```

Errors: none expected; the handler is deliberately trivial. A 500 from this endpoint indicates that the Flask process itself is unhealthy.

Audit emission: none.

### GET /readyz

Readiness probe. Performs a `SELECT 1` round-trip against RDS with a 1-second timeout. Used by orchestration tooling that wants to confirm database connectivity before routing traffic to a newly started task.

- RBAC: anonymous.
- DB dependency: required. The handler issues a parameterized `SELECT 1` and returns success only when the round-trip completes within 1 second.

Request: no body, no query parameters.

Success response — HTTP 200:

```json
{
  "status": "ready",
  "checks": {
    "db": "ok"
  }
}
```

Failure response — HTTP 503:

```json
{
  "status": "not_ready",
  "checks": {
    "db": "timeout"
  }
}
```

The `checks.db` value is one of `"ok"` (round-trip succeeded), `"timeout"` (round-trip exceeded 1 second), or `"error"` (the connection attempt raised a SQLAlchemy exception).

Errors:

| HTTP | error.code | Cause |
|------|------------|-------|
| 503 | `not_ready` | Database round-trip failed or timed out |

The 503 response uses a top-level shape (not the standard error envelope) because health checks are consumed by automation that expects a stable shape across success and failure.

Audit emission: none.

### GET /metrics

Prometheus exposition. Returns the metric registry maintained by `prometheus_client` at `backend/app/observability/metrics.py`. The output content type is `text/plain; version=0.0.4` per the Prometheus exposition format.

- RBAC: anonymous (recommended to scope to private VPC traffic in production via the ECS task security group).

Request: no body, no query parameters.

Success response — HTTP 200, content type `text/plain; version=0.0.4`. Truncated example:

```text
# HELP http_requests_total Total HTTP requests.
# TYPE http_requests_total counter
http_requests_total{method="POST",path="/api/connections",status="201"} 142.0
http_requests_total{method="GET",path="/api/connections",status="200"} 583.0
# HELP http_request_duration_seconds HTTP request duration.
# TYPE http_request_duration_seconds histogram
http_request_duration_seconds_bucket{method="POST",path="/api/connections",le="0.1"} 80.0
http_request_duration_seconds_bucket{method="POST",path="/api/connections",le="0.5"} 138.0
http_request_duration_seconds_bucket{method="POST",path="/api/connections",le="1.0"} 142.0
# HELP ai_request_duration_seconds Anthropic Claude inference latency.
# TYPE ai_request_duration_seconds histogram
ai_request_duration_seconds_bucket{le="1.0"} 42.0
ai_request_duration_seconds_bucket{le="2.5"} 78.0
ai_request_duration_seconds_bucket{le="5.0"} 91.0
```

The full metric inventory and alarm thresholds are documented in `docs/operations.md` §5. Metrics include HTTP request counters, HTTP request duration histograms, AI request duration histograms, audit emission counters, active session gauges, and database connection pool gauges.

Errors: none expected from the handler. A 500 indicates a metric registry corruption.

Audit emission: none.

## Appendix A: Schema Reference

This appendix lists every pydantic schema referenced by the endpoints above. The canonical definitions live in `backend/app/schemas/`; this catalog reproduces the field inventory for one-stop API documentation. Field types use Python notation; client-side equivalents in `frontend/src/schemas/*.ts` use the corresponding Zod types.

### ConnectionCreate

Defined in `backend/app/schemas/connection.py`. Used by `POST /api/connections`.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `full_name` | `str` | Yes | 1 to 200 characters; trimmed of leading/trailing whitespace |
| `linkedin_url` | `str` | Yes | Validated against the LinkedIn URL format (`is_valid_linkedin_url`) |
| `company` | `str` | Yes | 1 to 200 characters |
| `job_title` | `str` | Yes | 1 to 200 characters |
| `relationship_context` | `str` | Yes | 1 to 4000 characters; sanitized server-side before AI prompt templating |
| `ai_notes` | `str \| None` | No | Up to 8000 characters; nullable when AI generation is skipped or failed |
| `involvement` | `enum` | Yes | One of `"Warm Intro"`, `"Soft Reference"`, `"Target Only"` |
| `tag_ids` | `list[UUID] \| None` | No | UUIDs of tags belonging to the same organization |
| `submission_date` | `date \| None` | No | Defaulted server-side to current UTC date |

Forbidden fields (rejected with HTTP 422 if present): `id`, `org_id`, `owner_user_id`, `owner_display_name`, `outreach_status`, `created_at`, `deleted_at`, `normalized_linkedin_url`.

### ConnectionUpdate

Defined in `backend/app/schemas/connection.py`. Used by `PATCH /api/connections/:id`. Partial of `ConnectionCreate`: every field is optional, but the body must include at least one editable field. Owner-related fields and immutable fields are rejected with HTTP 422.

### ConnectionStatusUpdate

Defined in `backend/app/schemas/connection.py`. Used by `PATCH /api/connections/:id/status`.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `outreach_status` | `enum` | Yes | One of `"Not Started"`, `"In Progress"`, `"Contacted"`, `"Closed"` |

### ConnectionRead

Defined in `backend/app/schemas/connection.py`. Used by all read paths returning a single record (`GET /api/connections/:id`, the `items` element of `GET /api/connections` and `GET /api/admin/records`, and the response of `POST /api/connections`, `PATCH /api/connections/:id`, `PATCH /api/connections/:id/status`).

| Field | Type | Notes |
|-------|------|-------|
| `id` | `UUID` | Server-generated record identifier |
| `full_name` | `str` | As submitted |
| `linkedin_url` | `str` | As submitted (canonical form preserved) |
| `normalized_linkedin_url` | `str` | Server-derived; lower-cased host, no trailing slash, no query parameters |
| `company` | `str` | As submitted |
| `job_title` | `str` | As submitted |
| `relationship_context` | `str` | As submitted |
| `ai_notes` | `str \| None` | As submitted or generated; nullable |
| `involvement` | `enum` | As submitted |
| `outreach_status` | `enum` | Default `"Not Started"` on create; mutated only via `PATCH /api/connections/:id/status` |
| `owner_user_id` | `UUID` | Server-derived from `g.session.user_id` at create time; immutable |
| `owner_display_name` | `str` | Denormalized from `users.display_name` for fast feed rendering |
| `org_id` | `UUID` | Server-derived from `g.session.org_id`; immutable |
| `tag_ids` | `list[UUID]` | UUIDs of tags currently associated with the record |
| `submission_date` | `date` | As submitted or defaulted server-side |
| `created_at` | `datetime` | Server-generated UTC timestamp |
| `deleted_at` | `datetime \| None` | `null` on read by default; non-null only on soft-deleted records visible via the admin endpoint |

### NoteGenerationRequest

Defined in `backend/app/schemas/note_generation.py`. Used by `POST /api/notes/generate`.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `full_name` | `str` | Yes | 1 to 200 characters |
| `company` | `str` | Yes | 1 to 200 characters |
| `job_title` | `str` | Yes | 1 to 200 characters |
| `relationship_context` | `str` | Yes | 1 to 4000 characters; sanitized server-side before AI prompt templating |

### NoteGenerationResponse

Defined in `backend/app/schemas/note_generation.py`. Used by `POST /api/notes/generate`.

| Field | Type | Notes |
|-------|------|-------|
| `ai_notes` | `str` | The AI-generated outreach text; non-empty on success |

### TagCreate

Defined in `backend/app/schemas/connection.py` (or co-located with tag handlers; verify in `backend/app/schemas/`). Used by `POST /api/tags`.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `name` | `str` | Yes | 1 to 64 characters; trimmed; unique per organization |

### TagRead

Defined in `backend/app/schemas/connection.py`. Used by `GET /api/tags` and `POST /api/tags`.

| Field | Type | Notes |
|-------|------|-------|
| `id` | `UUID` | Server-generated tag identifier |
| `name` | `str` | As submitted |
| `org_id` | `UUID` | Server-derived from `g.session.org_id` |
| `created_at` | `datetime` | Server-generated UTC timestamp |

### UserRead

Defined in `backend/app/schemas/admin.py`. Used by `POST /auth/login`, `GET /api/me`, `GET /api/admin/users`, and `PATCH /api/admin/users/:id`.

| Field | Type | Notes |
|-------|------|-------|
| `id` | `UUID` | Server-generated user identifier |
| `email` | `str` | RFC 5322 email format |
| `display_name` | `str` | 1 to 200 characters |
| `role` | `enum` | One of `"Admin"`, `"Contributor"`, `"Viewer"` |
| `org_id` | `UUID` | Server-derived from session at upsert time |
| `created_at` | `datetime` | Server-generated UTC timestamp |

### UserRoleUpdate

Defined in `backend/app/schemas/admin.py`. Used by `PATCH /api/admin/users/:id`.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `role` | `enum` | Yes | One of `"Admin"`, `"Contributor"`, `"Viewer"` |

### LoginRequest

Defined in `backend/app/schemas/auth.py`. Used by `POST /auth/login`.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `email` | `str` | Yes | RFC 5322 email format |
| `password` | `str` | Yes | 1 to 200 characters; never logged |

### AuditEventRead

Defined in `backend/app/schemas/connection.py` (or `backend/app/schemas/audit.py`; verify in `backend/app/schemas/`). Used by `GET /api/connections/:id/history`.

| Field | Type | Notes |
|-------|------|-------|
| `id` | `UUID` | Server-generated audit event identifier |
| `event_type` | `enum` | One of `"create"`, `"status_change"`, `"edit"`, `"soft_delete"`, `"hard_delete"`, `"role_change"`, `"authentication"`, `"admin_op"` |
| `event_timestamp` | `datetime` | Server-generated UTC timestamp at INSERT |
| `actor_user_id` | `UUID` | The user whose action emitted the event |
| `actor_display_name` | `str` | Denormalized from `users.display_name` at emission time |
| `target_record_id` | `UUID \| None` | The `records.id` of the affected record; null for non-record events (`role_change`, `authentication`, `admin_op`) |
| `before_payload` | `dict \| None` | JSON object describing the pre-change state; null for `create` and some `authentication` events |
| `after_payload` | `dict \| None` | JSON object describing the post-change state; null for `hard_delete` |

### AnalyticsResponse

Defined in `backend/app/schemas/admin.py`. Used by `GET /api/admin/analytics`.

| Field | Type | Notes |
|-------|------|-------|
| `most_active_contributors` | `list[ContributorStat]` | Top 10 contributors by record count in the last 90 days |
| `leads_by_status` | `dict[str, int]` | Counts keyed by `outreach_status` enum value |
| `weekly_activity` | `list[WeeklyActivityPoint]` | 12 elements covering the last 12 weeks |

Where `ContributorStat` is `{ user_id: UUID, display_name: str, record_count: int }` and `WeeklyActivityPoint` is `{ week_start_date: date, record_count: int }`.

## See Also

- [`README.md`](../README.md) — project entry point with quick-start, architecture diagram, and feature list.
- [`onboarding.md`](onboarding.md) — clean-machine to running app, domain glossary, common pitfalls, suggested next tasks.
- [`architecture.md`](architecture.md) — detailed component architecture, layering, data flow, and the canonical permission matrix.
- [`security.md`](security.md) — authentication mechanisms, RBAC enforcement, audit invariants, secret management, data scoping.
- [`operations.md`](operations.md) — operational runbook: deploys, rollbacks, secret rotation, observability dashboards, metric inventory.
- [`decision-log.md`](decision-log.md) — rationale for every non-trivial decision; the canonical "why" reference.
- `backend/app/api/` — Flask blueprints; the canonical endpoint implementations. Discrepancies between this catalog and the blueprints are documentation defects.
- `backend/app/schemas/` — pydantic schemas; the canonical schema definitions. Field inventories in Appendix A mirror these files.
- `backend/app/services/` — service-layer implementations including transactional state-change-plus-audit emission patterns.
- `backend/app/middleware/rbac.py` — `@requires_role` decorator; the authoritative RBAC enforcement point.
- `backend/app/middleware/auth.py` — JWT validation on every authenticated request.
- `backend/app/middleware/correlation.py` — correlation ID generation and propagation.
- `backend/app/middleware/error_handlers.py` — exception-to-HTTP mapping and error envelope formatting.
- `backend/app/observability/metrics.py` — Prometheus metric registry exposed at `/metrics`.
- `frontend/src/api/client.ts` — the only frontend module that calls `fetch`; uniform handling of credentials, correlation IDs, and 401 redirects.

