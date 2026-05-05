# Security Model

This document describes the security posture of the Sales-Connections platform: authentication mechanisms, role-based authorization, append-only audit, secret management, and data scoping. The platform is engineered against ten explicit security invariants (AAP §0.7.4); each is documented here with its enforcement mechanism and verification steps.

This file is the canonical security reference. It complements `docs/architecture.md`, which states what the security architecture is at a glance, by describing in detail where each invariant is enforced, how the enforcement is verified, and what the failure modes are. Decision rationale (the "why") lives in `docs/decision-log.md`; this document is intentionally limited to definitions, enforcement points, code references, and verification.

## Table of Contents

- [1. Threat Model](#1-threat-model)
- [2. Authentication](#2-authentication)
- [3. Authorization (RBAC)](#3-authorization-rbac)
- [4. Audit Trail](#4-audit-trail)
- [5. Data Scoping](#5-data-scoping)
- [6. Secrets](#6-secrets)
- [7. Input Validation](#7-input-validation)
- [8. Network Security](#8-network-security)
- [9. Frontend Security](#9-frontend-security)
- [10. Compliance and Privacy](#10-compliance-and-privacy)
- [11. Verification](#11-verification)
- [See Also](#see-also)

## 1. Threat Model

### Trust boundaries

The Sales-Connections deployment establishes the following trust boundaries. Each boundary is a point at which inbound traffic is treated as potentially adversarial and validated before the receiving component acts on it.

| Boundary | Origin | Destination | Channel | Authentication |
|----------|--------|-------------|---------|----------------|
| Public Internet ingress | User browser | AWS Application Load Balancer | HTTPS (TLS terminated at ALB via ACM) | No client identity at this hop; identity is asserted by session cookie checked downstream |
| Internal load balancing | ALB | ECS Fargate task | Private VPC subnet, HTTP plaintext over private network | Implicit via security-group restriction (only ALB SG may reach Fargate SG on port 8000) |
| Database access | Fargate task | RDS PostgreSQL | Private VPC subnet, TCP 5432 | Username and password from AWS Secrets Manager; Fargate SG is the only ingress to the RDS SG |
| Anthropic Claude API | Fargate task | `api.anthropic.com` | Public Internet via NAT gateway, HTTPS | Bearer API key from AWS Secrets Manager, server-side only |
| Google OAuth | Fargate task | `accounts.google.com`, `oauth2.googleapis.com`, `www.googleapis.com` | Public Internet via NAT gateway, HTTPS | Client ID and client secret from AWS Secrets Manager |
| Secrets retrieval | Fargate task | AWS Secrets Manager | Private VPC endpoint (preferred) or NAT gateway | IAM task role (no long-lived AWS keys in the container) |

### Identified threats and mitigations

The following threat catalogue maps every concrete attack surface to its enforcement mechanism. Each threat is assumed to be active during normal operation; mitigations are engineered as defense-in-depth so that a failure in any single layer does not produce a security incident.

| Threat | Mitigation |
|--------|------------|
| Form tampering by malicious browser | Three-layer validation: Zod schemas on the client for UX, pydantic schemas on the API for authority, PostgreSQL constraints (NOT NULL, CHECK, FK, UNIQUE) as the last line of defense |
| Cross-org data access | Org-scoped queries enforced server-side; every read and write injects `WHERE org_id = g.session.org_id`, sourced from the JWT claim populated by `backend/app/middleware/auth.py` |
| Privilege escalation via client request | Owner identity always derived from `g.session.user_id` server-side; pydantic schemas in `backend/app/schemas/connection.py` reject any client-supplied `owner_user_id` or `owner_display_name` |
| Audit tampering | Database-level `GRANT INSERT` only on `audit_events` for the application role; `UPDATE` and `DELETE` are revoked at the database privilege layer in `backend/migrations/versions/0001_initial_schema.py` |
| Secret leakage in logs | structlog redaction processor in `backend/app/observability/logging.py` filters keys matching `(?i).*(_key\|_secret\|password\|token\|authorization).*` and substitutes `***REDACTED***` |
| Stolen session token | HttpOnly cookie (no JavaScript access), Secure (HTTPS only), SameSite=Lax (limits CSRF), 8-hour expiry, and per-user signing-key version that allows immediate invalidation on logout |
| OAuth code interception | PKCE (`code_challenge` + `code_challenge_method=S256`) in the authorization-code flow; `state` value validated against a short-lived signed cookie; ID token signature validated against Google's JWKS |
| Anthropic API key leak | Stored only in AWS Secrets Manager; never returned in any API response; never crosses the SPA boundary; redacted from logs by the structlog processor |
| Brute-force password attack | bcrypt cost factor 12 makes each verification expensive; per-user signing-key invalidation closes a stolen token; rate limiting is post-MVP per AAP §0.6.2 |
| SQL injection | Parameterized queries via SQLAlchemy 2.x ORM; no string interpolation into raw SQL anywhere in `backend/app/services/` |
| XSS via stored content (`relationship_context`, `ai_notes`) | React's default JSX escaping renders all user content safely; `dangerouslySetInnerHTML` is forbidden in feature components by an ESLint rule and by code review; CSP `script-src 'self'` blocks injected inline scripts |
| Cross-Site Request Forgery (CSRF) | `SameSite=Lax` cookie attribute plus an Origin/Referer header check on state-changing endpoints in `backend/app/middleware/auth.py` |
| TLS downgrade | TLS 1.2+ only at the ALB; security policy `ELBSecurityPolicy-TLS13-1-2-2021-06` or newer; HTTP listener on port 80 redirects to HTTPS |
| Prompt injection in AI requests | `backend/app/utils/sanitization.py` strips control characters and applies length caps to `relationship_context` before templating into the Claude prompt |
| Replay of expired tokens | JWT `exp` claim validated on every request; expired tokens are rejected with HTTP 401; clock skew tolerance is 0 seconds (strict) — `verify_session_jwt()` invokes `pyjwt.decode` without a `leeway` argument, so the decoder applies the PyJWT default of 0. Clock drift is mitigated operationally via NTP on the ALB and Fargate hosts rather than via `exp` leeway |
| Denial of service via expensive AI requests | 5-second timeout watchdog in `backend/app/services/ai_orchestration.py`; HTTP 504 returned with `error.code = "ai_timeout"`; Prometheus alarm on the 95th percentile of AI latency |
| Privilege escalation via JWT tampering | HS256 signature verified against `JWT_SIGNING_KEY` from Secrets Manager; tampered tokens fail signature verification and are rejected with HTTP 401 |
| Container image supply chain | ECR native scanner runs on every image push; Dependabot raises pull requests for vulnerable Python and npm dependencies |

### Out-of-scope threats

The following threats are explicitly out of scope for the MVP delivery and are documented for traceability rather than for mitigation in this release.

- Distributed denial-of-service (DDoS) at the ALB layer — mitigation deferred; AWS Shield Standard is enabled by default and provides baseline protection.
- Insider threat from holders of the migrations role — mitigated operationally by restricting the role to Alembic-only contexts via Terraform-managed credentials in Secrets Manager.
- Side-channel attacks on bcrypt verification — bcrypt itself is constant-time per the algorithm specification; the surrounding handler does not return verification timing as observable signal.
- Physical attacks on AWS data centers — mitigated by the AWS Shared Responsibility Model.

## 2. Authentication

Authentication is feature F-012 in the AAP. This section documents the authentication mechanisms, the session token format and lifecycle, the OAuth flow detail, the password handling discipline, and the token rotation strategy.

### Mechanisms

The platform provides two authentication mechanisms; either yields the same session JWT once authentication completes.

- Primary: Google OAuth 2.0 authorization-code flow with PKCE.
- Fallback: email plus bcrypt-hashed password.

Both mechanisms culminate in the server minting a session JWT and setting it as an HttpOnly cookie. The frontend never sees the OAuth tokens (access, refresh, ID) — only the locally minted session JWT crosses the SPA boundary.

### Session token

The session JWT is the only credential exposed to the browser after authentication completes. Its format is fully specified to enable independent verification.

- Algorithm: HS256.
- Claims (as minted by `mint_session_jwt()` in `backend/app/services/auth.py`): `user_id`, `org_id`, `role`, `email`, `display_name`, `tv` (the user's `token_version` snapshot — see [Token rotation strategy](#token-rotation-strategy)), `iat` (issued-at, Unix seconds), `exp` (expiry, 8 hours after `iat`, Unix seconds). The `email` and `display_name` claims are denormalized for log enrichment and are not authoritative; the auth middleware treats `user_id`, `org_id`, `role`, and `tv` as authoritative.
- Cookie name: `session`.
- Cookie attributes: `HttpOnly; Secure; SameSite=Lax; Path=/`.
- Cookie domain: the deployment's root domain (for example, `sales-connections.example.com`).
- Signature key: `JWT_SIGNING_KEY` from AWS Secrets Manager.

Cookie attribute meaning:

- `HttpOnly` blocks all JavaScript access (`document.cookie`, `fetch`'s `credentials` notwithstanding the browser still sends the cookie automatically).
- `Secure` forbids transmission over plaintext HTTP, eliminating cleartext interception.
- `SameSite=Lax` bars the cookie from cross-site `POST` and other state-changing requests, providing CSRF protection.
- `Path=/` makes the cookie available to every endpoint of the deployment.

### Password handling

Password handling discipline is encoded in `backend/app/services/auth.py` and is governed by the following invariants.

- Plaintext passwords are received only at `POST /auth/login` and only over TLS to the ALB.
- Passwords are hashed with bcrypt cost factor 12 via `services/auth.py:hash_password()`.
- The hash is the only password representation persisted in the `users` table (column `password_hash`, nullable for OAuth-only users).
- Plaintext passwords are never logged. The structlog redaction processor catches `password` keys in any log payload and substitutes `***REDACTED***`.
- Verification uses `bcrypt.checkpw()`, which is constant-time per the bcrypt specification.
- Failed verification is rejected with HTTP 401; the response body does not distinguish between unknown email and incorrect password (timing of the response is similarly not informative beyond a constant-time comparison).
- Successful verification mints a session JWT and emits an `audit_events.event_type = authentication` row.

### OAuth flow detail

The Google OAuth 2.0 authorization-code flow with PKCE is implemented across `backend/app/api/auth.py` and `backend/app/services/auth.py`. The complete sequence is:

1. The browser navigates to `GET /auth/google/start`.
2. The handler generates a 32-byte random `state`, a 32-byte random `code_verifier`, and the corresponding `code_challenge = SHA256(code_verifier)`.
3. The handler persists `state` and `code_verifier` in a short-lived signed cookie named `oauth_state` (HttpOnly, Secure, SameSite=Lax, expires in 10 minutes).
4. The handler issues HTTP 302 to the Google authorization endpoint with query parameters `client_id`, `redirect_uri`, `response_type=code`, `scope=openid email profile`, `state`, `code_challenge`, and `code_challenge_method=S256`.
5. Google authenticates the user, presents the consent screen, and redirects to `GET /auth/google/callback?code=...&state=...`.
6. The callback handler validates that the `state` parameter matches the value stored in the `oauth_state` cookie; mismatch rejects with HTTP 400 and `error.code = "invalid_state"`.
7. The callback handler exchanges `code` for an ID token at the Google token endpoint, sending `code_verifier` to satisfy PKCE.
8. The handler validates the ID token signature against Google's JWKS at `https://www.googleapis.com/oauth2/v3/certs`, validates the `iss` claim is `accounts.google.com` or `https://accounts.google.com`, validates the `aud` claim matches the configured `GOOGLE_OAUTH_CLIENT_ID`, and validates `exp` is in the future.
9. The handler upserts the user by `email` within the configured organization (`upsert_oauth_user()` in `backend/app/services/auth.py`). All users created via the OAuth flow are assigned the `Contributor` role unconditionally; there is no automatic promotion of any user to `Admin`. The seed `Admin` account must be provisioned out-of-band — see [`docs/onboarding.md`](onboarding.md#step-7--bootstrap-an-admin-account-required-before-step-8) Step 7 for the canonical procedure (psql + bcrypt for email/password Admin, or OAuth login followed by `UPDATE users SET role = 'Admin' WHERE email = '<your-email>'`).
10. The handler emits an `audit_events` row with `event_type = authentication` and `after = { method: "oauth_google" }`.
11. The handler mints a session JWT, sets it as the `session` cookie, clears the `oauth_state` cookie, and issues HTTP 302 to `/feed`.

OAuth tokens (access, refresh, ID) are held only in the local variables of the callback handler. They are never persisted to the database, never returned to the SPA, and never logged. Once the user upsert completes, references to them go out of scope and are garbage-collected.

### Logout

Logout is `POST /auth/logout`. The handler invokes `revoke_session_and_audit()` in `backend/app/services/auth.py`, which performs both effects in a single SQLAlchemy transaction so the cookie clear, the `token_version` increment, and the audit emission either all succeed or all roll back together.

- Clears the `session` cookie by issuing `Set-Cookie: session=; Max-Age=0; HttpOnly; Secure; SameSite=Lax; Path=/`.
- Increments the user's per-user `token_version` column (`UPDATE users SET token_version = token_version + 1 WHERE id = :user_id`). Any previously issued JWT for this user carries a stale `tv` claim; the next request that presents the old JWT is rejected by `_verify_token_version()` in `backend/app/middleware/auth.py` with HTTP 401. The same mechanism applies to BOTH email/password and OAuth-minted JWTs because the same column gates both flows.
- For OAuth users, the user's Google session is unaffected by this server-side revocation; logging out of Google requires a separate operation against `accounts.google.com`.
- Emits an `audit_events` row with `event_type = authentication` and `after_payload = { method: "logout", result: "success", user_id, org_id }`.

### Token rotation strategy

JWT revocation uses a per-user counter (`users.token_version`), NOT a global signing-key version. The signing key (`JWT_SIGNING_KEY` in AWS Secrets Manager) is held flat — there is exactly one active value at any moment — and the per-user counter is what allows individual sessions to be invalidated independently of the key material. Rationale and the alternatives considered live in [`docs/decision-log.md`](decision-log.md) DL-0043.

How the per-user counter mechanism works:

- Each `users` row carries a `token_version: int` column (introduced by `backend/migrations/versions/0002_token_version_and_app_role.py`), defaulting to `0`.
- `mint_session_jwt(user)` snapshots `user.token_version` into the JWT's `tv` claim at issuance.
- The auth middleware's `_verify_token_version(session)` (in `backend/app/middleware/auth.py`) compares the JWT's `tv` claim against the live `users.token_version` value on every protected request via a single PK-lookup query (`SELECT token_version FROM users WHERE id = :user_id`). Equality admits the request; any mismatch (including a stored value greater than `tv`, which means the user has logged out or had their session revoked) rejects with HTTP 401.
- `revoke_session_and_audit()` increments the column atomically via a SQL expression (`User.token_version + 1`, evaluated server-side) so two concurrent logouts cannot race.

Properties of this design:

- Revocation is scoped per-user. Logging out user A does not affect user B's sessions, even though both are signed by the same `JWT_SIGNING_KEY`.
- Revocation is immediate. The next protected request from user A's old session (one DB round-trip later) is rejected; there is no overlap window in which the old JWT remains valid.
- No two-slot key list is required. There is no `current`/`prior` JSON shape, no global "key version identifier", and no `kid` claim — earlier drafts of this document referenced these concepts; they describe a design that was not implemented.

What rotating `JWT_SIGNING_KEY` itself looks like:

- Rotating the signing key (replacing its bytes in AWS Secrets Manager) DOES invalidate every active session because all in-flight JWTs were signed with the previous key and will fail signature verification under the new key. There is no zero-downtime path for signing-key rotation in this design — by intent, since rotation of the signing key is a rare operational event (annual cadence or post-incident) and forcing all users to re-authenticate is an acceptable cost in exchange for the simplicity of a single-key validator.
- Per-user revocation (logout, forced sign-out) and operational signing-key rotation are two independent capabilities. The token-rotation invariant in AAP §0.7.4 is satisfied by the per-user mechanism; signing-key rotation is an additional, infrequent operation.

The operational rotation procedure (when, who, verification, communication) is documented in [`docs/operations.md`](operations.md#4-secret-rotation) §4.

### Where enforced

- `backend/app/api/auth.py` — endpoint handlers `GET /auth/google/start`, `GET /auth/google/callback`, `POST /auth/login`, `POST /auth/logout`, `GET /api/me`.
- `backend/app/services/auth.py` — `hash_password()`, `verify_password()`, `mint_session_jwt()`, `verify_session_jwt()`, `upsert_oauth_user()`.
- `backend/app/middleware/auth.py` — JWT validation on every `/api/*` request; populates `g.session` from the verified claims; rejects unauthenticated requests with HTTP 401.
- `backend/app/extensions.py` — Authlib OAuth client registration with `client_id` and `client_secret` sourced from `backend/app/config.py`.
- `backend/app/config.py` — secret retrieval from AWS Secrets Manager via `boto3.client('secretsmanager')`.

## 3. Authorization (RBAC)

Authorization is feature F-009 in the AAP. The RBAC model is a three-role hierarchy enforced authoritatively at the API layer with a secondary UI defense in the SPA.

### Three roles

- `Admin` — Platform administrator. Full read and write access to all records in the organization. Sole holder of role-management, hard-delete, and admin-panel privileges.
- `Contributor` — Connection submitter. Creates connection records, edits own records, soft-deletes own records. Cannot mutate outreach status (this is reserved for the sales team).
- `Viewer` (Sales Rep) — Sales team member. Reads all records, mutates outreach status on any record, soft-deletes own records (in case of accidental status creation; admins do most cleanup).

The role enum is defined in `backend/app/models/enums.py` as `UserRole.ADMIN`, `UserRole.CONTRIBUTOR`, and `UserRole.VIEWER`.

### Permission matrix

The permission matrix is reproduced here for reference; the canonical at-a-glance version lives in `docs/architecture.md` §8. Surprising rules are highlighted below the table.

| Operation | Endpoint | Admin | Contributor | Viewer (Sales Rep) |
|-----------|----------|-------|-------------|--------------------|
| Create record | `POST /api/connections` | Yes | Yes | No |
| Read record (list and detail) | `GET /api/connections`, `GET /api/connections/:id` | Yes (org-scoped) | Yes (org-scoped) | Yes (org-scoped) |
| Edit record | `PATCH /api/connections/:id` | Yes (any) | Yes (own only) | No |
| Mutate outreach status | `PATCH /api/connections/:id/status` | Yes (any) | No (even on own record) | Yes (any) |
| Soft delete | `DELETE /api/connections/:id` | Yes (any) | Yes (own only) | Yes (own only) |
| Hard delete | `DELETE /api/admin/records/:id` | Yes | No | No |
| Generate AI notes | `POST /api/notes/generate` | Yes | Yes | No |
| Read tags | `GET /api/tags` | Yes | Yes | Yes |
| Create tag | `POST /api/tags` | Yes | Yes | No |
| Read users | `GET /api/admin/users` | Yes | No | No |
| Mutate user role | `PATCH /api/admin/users/:id` | Yes | No | No |
| Read all records (including soft-deleted) | `GET /api/admin/records?include_deleted=true` | Yes | No | No |
| Read analytics | `GET /api/admin/analytics` | Yes | No | No |
| Duplicate check (pre-submit) | `GET /api/connections/duplicate-check` | Yes | Yes | No |
| Read history | `GET /api/connections/:id/history` | Yes | Yes | Yes |

Surprising rules to highlight:

- Outreach status is updatable by `Viewer` (Sales Rep) or `Admin` only; `Contributor` cannot mutate status, even on records they own. The user explicitly stated this rule in AAP §0.1.2 to preserve sales team accountability. The contract is: contributors capture leads, sales reps work them.
- Contributors edit only their own records (matched by `owner_user_id == g.session.user_id`). Only `Admin` edits any record in the organization.
- Soft delete is allowed for `Admin` (any record), `Contributor` (own records), and `Viewer` (own records). The own-record carve-out for Viewer covers the rare case of an accidentally created status row.
- Hard delete is restricted to `Admin` only and is exposed only via the admin-panel endpoint, never via the standard connection endpoints.

### Decorator pattern

Authorization is enforced by the `@requires_role(*roles)` decorator in `backend/app/middleware/rbac.py`. The decorator:

- Reads `g.session.role` (populated by the JWT validation middleware from the JWT claim).
- Compares against the decorator's argument list.
- On mismatch, raises a `PermissionError` that the error-handler middleware maps to HTTP 403 with `error.code = "forbidden"`.
- On match, allows the handler to proceed.

The check is in-process: it consults already-deserialized data on the Flask `g` object and performs no database round-trip. RBAC enforcement therefore costs well under 50 ms, satisfying the budget in AAP §0.7.3.

For owner-scoped operations (such as `Contributor` editing only own records), the service layer in `backend/app/services/connections.py` performs an additional check after the role gate: the loaded record's `owner_user_id` must equal `g.session.user_id`, or the service raises `PermissionError` and the error handler maps to HTTP 403.

### Two-tier defense

RBAC is enforced at two layers; the layers serve distinct purposes.

- Authoritative layer: backend `@requires_role` decorator on every state-changing endpoint plus owner-scoping checks in services. This is the only layer that the platform trusts to keep data safe; the SPA cannot be trusted because it runs on the user's machine.
- Secondary layer: frontend `<RoleGate role="Admin">` component in `frontend/src/auth/RoleGate.tsx`. Hides admin-only or status-mutation buttons in the UI based on the role exposed by `useSession()`. This is a UX courtesy — it prevents users from clicking buttons they will be forbidden from using — and never the only defense.

The frontend role gate is enforced through the cookie-decoded session reflected by the AuthProvider. If a user manually crafts a request, bypasses the SPA, and sends it to the API, the backend `@requires_role` decorator rejects it independently. Tests in `backend/tests/services/test_rbac.py` assert this independence by simulating direct API calls without any SPA involvement.

### Where enforced

- `backend/app/middleware/rbac.py` — `@requires_role` decorator.
- `backend/app/api/auth.py`, `connections.py`, `notes.py`, `tags.py`, `admin.py` — every blueprint applies the decorator at the top of every protected route.
- `backend/app/services/connections.py` — owner-scoping checks for `Contributor` operations.
- `backend/app/services/admin.py` — implicit `Admin`-only access via admin endpoint gating.
- `frontend/src/auth/RoleGate.tsx` — UI-secondary defense.
- `frontend/src/auth/AuthProvider.tsx` — exposes `useSession()` and `useRole()` for component-level decisions.

## 4. Audit Trail

The audit trail is feature F-013 in the AAP. The audit trail records every state-changing operation in an append-only `audit_events` table with eight enum-constrained event types. The append-only invariant is enforced both at the database privilege layer and inside the application code.

### Append-only invariant

The `audit_events` table is engineered as append-only via two complementary mechanisms.

- Database-level: the application database role is granted only `INSERT` on `audit_events`. `UPDATE` and `DELETE` are revoked at the database level. The privileges are configured in `backend/migrations/versions/0001_initial_schema.py` via SQL `GRANT INSERT ON audit_events TO sales_connections_app` and `REVOKE UPDATE, DELETE ON audit_events FROM sales_connections_app`.
- Application-level: the `AuditEvent` SQLAlchemy model in `backend/app/models/audit_event.py` defines `__mapper_args__ = {"confirm_deleted_rows": False}` and exposes no update or delete methods; `backend/app/services/audit.py` exposes only `emit_audit_event(...)` and never calls `db.session.delete(...)` against an audit row.

A separate migrations role retains full privileges on `audit_events` for governance purposes (schema migrations, ad-hoc admin queries). The migrations role is used only by Alembic and is never assumed by application traffic; its credentials are stored in a separate Secrets Manager entry.

### Atomic state-change and audit emission

Every state-changing service function emits its audit event in the same database transaction as its data mutation. The pattern is:

```python
def create_record(payload: ConnectionCreate, actor: Session) -> Record:
    with db.session.begin():
        record = Record(...)
        db.session.add(record)
        db.session.flush()  # populates record.id
        emit_audit_event(
            event_type=AuditEventType.CREATE,
            actor_user_id=actor.user_id,
            target_record_id=record.id,
            before=None,
            after=record_to_dict(record),
        )
    return record
```

If either the record INSERT or the audit INSERT fails, the transaction rolls back and neither row is persisted. PostgreSQL MVCC guarantees that no observer sees a record without its corresponding audit row. This invariant is one of the eight architectural principles in AAP §0.7.1.

### Eight event types

The eight enum-constrained event types are defined in `backend/app/models/enums.py:AuditEventType`. They are documented at length in `docs/architecture.md` §9; the canonical list follows.

| Event Type | Trigger | Emitted By |
|------------|---------|------------|
| `create` | A new record is persisted via `POST /api/connections` | `services/connections.py:create_record()` |
| `status_change` | Outreach status mutated via `PATCH /api/connections/:id/status` | `services/connections.py:mutate_status()` |
| `edit` | Record content edited via `PATCH /api/connections/:id` | `services/connections.py:update_record()` |
| `soft_delete` | Record soft-deleted via `DELETE /api/connections/:id` | `services/connections.py:soft_delete_record()` |
| `hard_delete` | Record hard-deleted via `DELETE /api/admin/records/:id` | `services/admin.py:hard_delete_record()` |
| `role_change` | User role mutated via `PATCH /api/admin/users/:id` | `services/admin.py:mutate_role()` |
| `authentication` | Login (success), logout, or OAuth callback | `services/auth.py:complete_login()`, `services/auth.py:logout()` |
| `admin_op` | Other Admin-only operations (analytics generation reserved for forensics) | `services/admin.py:*` |

Each row carries `actor_user_id`, optional `target_record_id`, `event_type`, `event_timestamp`, and JSONB `before` and `after` payloads. The `before` and `after` payloads capture pre-mutation and post-mutation state for `edit` and `status_change` events; for `create` they carry only `after`; for `soft_delete` and `hard_delete` they carry only `before`; for `authentication` they carry context such as `{ method: "google" }` or `{ event: "logout" }` in `after`.

### Verification

Audit invariants are verified by automated tests.

- Every state-changing test in `backend/tests/api/` and `backend/tests/services/` ends with an assertion that the appropriate audit row exists. A pytest fixture `assert_audit_emitted(event_type, target_record_id)` provides the assertion in one line.
- A specific test in `backend/tests/services/test_audit.py` issues a raw SQL `UPDATE audit_events SET event_timestamp = NOW() WHERE id = :id` from the application role and asserts the database raises `psycopg.errors.InsufficientPrivilege`.
- A second test issues `DELETE FROM audit_events WHERE id = :id` from the application role and asserts the same permission error.
- A third test issues `INSERT INTO audit_events (...) VALUES (...)` from the application role and asserts the row is persisted (positive case).

### Forensic queries

The audit table supports forensic investigation via the following query patterns. The composite index on `(target_record_id, event_timestamp)` keeps these queries sub-second at the MVP scale ceiling.

- All events for a record (most recent first):

  ```sql
  SELECT * FROM audit_events
  WHERE target_record_id = :record_id
  ORDER BY event_timestamp DESC;
  ```

- All events by an actor:

  ```sql
  SELECT * FROM audit_events
  WHERE actor_user_id = :user_id
  ORDER BY event_timestamp DESC;
  ```

- All hard-delete events (rare; investigated whenever they occur):

  ```sql
  SELECT * FROM audit_events
  WHERE event_type = 'hard_delete'
  ORDER BY event_timestamp DESC;
  ```

- All authentication events for a user (login attempts, logout, OAuth completions):

  ```sql
  SELECT event_timestamp, after
  FROM audit_events
  WHERE event_type = 'authentication' AND actor_user_id = :user_id
  ORDER BY event_timestamp DESC;
  ```

## 5. Data Scoping

The data model is engineered for multi-tenancy from day one even though the MVP runtime serves a single organization. Every entity carries an `org_id` foreign key and every read or write injects an org-scoped predicate. Cross-org data access is impossible at the API layer.

### org_id on every entity

Every entity in the schema carries `org_id` either directly or transitively via a foreign key chain.

| Entity | org_id Provenance |
|--------|-------------------|
| `organizations` | The entity itself; `id` is the org identifier |
| `users` | Direct `org_id` column with FK to `organizations.id` |
| `records` | Direct `org_id` column with FK to `organizations.id` |
| `tags` | Direct `org_id` column with FK to `organizations.id` |
| `record_tags` | Transitive: `record_id` links to a record whose `org_id` is the org |
| `audit_events` | Transitive: `target_record_id` links to a record (or `actor_user_id` links to a user); the org is recoverable via either FK |

The migration `backend/migrations/versions/0001_initial_schema.py` enforces NOT NULL on every direct `org_id` column.

### Org-scoped queries

Every query in `backend/app/services/` is org-scoped without exception.

- Every read injects `WHERE org_id = g.session.org_id` (or a transitive equivalent for `record_tags` and `audit_events`).
- Every write derives `org_id` from `g.session.org_id`. Client payloads never carry `org_id`.
- Cross-org access returns HTTP 403 from the API. From the SPA's perspective, the cross-org record does not exist; the SPA renders HTTP 404 for any record the API does not surface.

The org-scoping is enforced at the service layer rather than via PostgreSQL Row-Level Security (RLS). The service-layer choice keeps query debugging straightforward (RLS silently filters rows in ways that are hard to investigate) and aligns with the existing handler-service-model pattern. This is one of the eight architectural principles in AAP §0.7.1.

### Owner attribution

Owner attribution is feature F-006 in the AAP. The `records.owner_user_id` column is non-null and references `users.id`.

- The owner is sourced exclusively from `g.session.user_id` server-side.
- Pydantic schemas in `backend/app/schemas/connection.py` explicitly reject any client-supplied `owner_user_id` or `owner_display_name` with HTTP 422 (per AAP §0.7.6).
- The `owner_display_name` column is denormalized for fast feed rendering. When the user updates their display name, a service-layer migration updates `owner_display_name` on every record they own. The migration runs in a single transaction with the user update.

The non-overwritable owner identity is the foundation of AAP §0.1.2's invariant that "each record must have an owner permanently attached and visible." Anonymous submissions are unsupported and impossible to construct via the API.

### Soft delete and visibility

Soft delete is feature F-007 in the AAP. The platform uses `records.deleted_at` (a nullable timestamp) to tombstone records without removing them.

- Default reads inject `WHERE deleted_at IS NULL`. The injection is enforced at the service layer in `backend/app/services/connections.py:list_records()` and similar functions.
- The Admin moderation view explicitly opts out via `?include_deleted=true`. The admin endpoint `GET /api/admin/records?include_deleted=true` is the only path that surfaces soft-deleted records.
- Cross-org users never see records — soft-deleted or otherwise. The org scoping is applied independently of the soft-delete filter, so even an Admin in organization A cannot see soft-deleted records in organization B.
- Hard delete is restricted to `Admin` and removes the record entirely; the corresponding audit event of type `hard_delete` remains, since the audit table is append-only.

### Where enforced

- `backend/app/services/connections.py` — service-level org-scoping in every query; soft-delete-aware default predicates.
- `backend/app/services/admin.py` — admin-only opt-out for soft-delete filter.
- `backend/app/middleware/auth.py` — populates `g.session.org_id` from the JWT claim on every authenticated request.
- `backend/app/schemas/connection.py` — pydantic rejection of client-supplied `org_id`, `owner_user_id`, `owner_display_name`.
- Database FK constraints — fail-safe layer that prevents `record_tags` from referencing a record outside the org via `record_id`.

## 6. Secrets

The platform's secrets are catalogued, retrieved, redacted, and rotated according to a uniform discipline. No secret is ever embedded in the source repository, in a Docker image, or in any log line.

### Catalog

| Secret | Purpose | Source |
|--------|---------|--------|
| `ANTHROPIC_API_KEY` | F-002 AI note generation; bearer key for `api.anthropic.com` | AWS Secrets Manager (`sales-connections/<env>/anthropic-api-key`) |
| `GOOGLE_OAUTH_CLIENT_ID` | F-012 OAuth client identifier; treated as configuration but stored alongside the secret for cohesion | AWS Secrets Manager (`sales-connections/<env>/google-oauth-client-id`) |
| `GOOGLE_OAUTH_CLIENT_SECRET` | F-012 OAuth client secret; required for the Google token endpoint exchange | AWS Secrets Manager (`sales-connections/<env>/google-oauth-client-secret`) |
| `JWT_SIGNING_KEY` | F-012 session token signing key; flat single-value secret (no two-slot list — see [Token rotation strategy](#token-rotation-strategy) for rationale and the per-user `users.token_version` revocation mechanism that replaces it) | AWS Secrets Manager (`sales-connections/<env>/jwt-signing-key`) |
| `DB_PASSWORD` | RDS authentication for the application role | AWS Secrets Manager (managed by RDS rotation hooks; `sales-connections/<env>/db-password`) |
| `DB_PASSWORD_MIGRATIONS` | RDS authentication for the migrations role | AWS Secrets Manager (`sales-connections/<env>/db-password-migrations`) |

In local development, `.env` files (`backend/.env`, `frontend/.env`) override Secrets Manager. The `.env` files are gitignored; templates are committed at `backend/.env.example` and `frontend/.env.example`.

### Read pattern

Secret retrieval is centralized in `backend/app/config.py`.

- At process startup, the configuration loader calls `boto3.client('secretsmanager').get_secret_value(SecretId=...)` for each catalogued secret.
- Secrets are cached for the worker lifetime; rotation requires a rolling restart of the ECS service. The exception is the JWT signing key, which is refreshed periodically (every 5 minutes) to support zero-downtime rotation.
- Failure to retrieve a required secret at startup raises a fatal exception; the worker fails to boot and ECS marks the task unhealthy. The ALB does not route traffic to unhealthy tasks, preserving the system from operating in a partially configured state.
- In `DevelopmentConfig`, the loader reads from `.env` first and falls back to Secrets Manager only if a key is absent. In `ProductionConfig`, the loader reads exclusively from Secrets Manager.

### Logging redaction

The structlog processor in `backend/app/observability/logging.py` filters secret-like keys from every log record before serialization.

- The redaction pattern is a case-insensitive regex applied via `re.fullmatch` (whole-key match) to every key in the structlog event dict. The pattern matches:
    - any name containing `password` (catches `password`, `db_password`, `user_password`, `password_hash`, etc.) and the alias `passwd`
    - any name containing `secret` (catches `secret`, `client_secret`, `aws_secret_access_key`, `shared_secret`, etc.)
    - `authorization` and `bearer` (HTTP credential header naming)
    - `token` and `*_token` (bearer/access/refresh/id tokens)
    - any name containing `api_key` / `api-key` / `apikey` (covers `ANTHROPIC_API_KEY`, `stripe_api_key`, `my-api-key`, etc.)
    - the specific known-sensitive `*_key` variants `signing_key`, `secret_key`, `private_key`, `encryption_key`, `master_key`, and `session_key`
    - `cookie`, `set_cookie`, and `set-cookie` (raw HTTP cookie values; the cookie *name*, e.g., `session_cookie_name`, is unaffected)
- Matched values are replaced with the literal string `***REDACTED***`.
- Generic `*_key` is intentionally NOT redacted to avoid noisy false positives on benign domain keys (`sort_key`, `cache_key`, `partition_key`, `cursor_key`); contributors logging a new credential name MUST choose a name covered by one of the patterns above OR extend the regex.
- The redactor walks dictionaries recursively up to a bounded depth (chosen to prevent pathological log payloads from causing CPU exhaustion).
- The redactor runs before any renderer, so neither JSON output nor console output ever contains a secret value.
- Verified by `backend/tests/observability/test_logging_redaction.py`, which constructs payloads with every credential-name variant above and asserts the rendered output contains `***REDACTED***` for each.

### Boundary enforcement

- The Anthropic API key never crosses the SPA boundary. The frontend has no awareness of `ANTHROPIC_API_KEY`; AI note generation is invoked exclusively via `POST /api/notes/generate`, which the backend handles entirely server-side.
- Google OAuth tokens (access, refresh, ID) never persist beyond the callback handler. They are local variables in `backend/app/api/auth.py:google_callback()` and are garbage-collected at the end of the request. Only the locally minted session JWT is exposed to the browser.
- The JWT signing key never appears in any HTTP response, any log line, or any environment-variable echo.
- The DB password never appears in any application log; SQLAlchemy's connection-string formatting elides the password from `repr` output.

### Rotation procedures

Step-by-step rotation procedures (cadence, runbook, post-rotation verification) are documented in `docs/operations.md` §4. The high-level model:

- `JWT_SIGNING_KEY`: rotated quarterly; the versioned-key list strategy enables zero-downtime rotation.
- `ANTHROPIC_API_KEY`: rotated when Anthropic publishes a new key or when a leak is suspected; rotation requires a rolling restart of the ECS service.
- `GOOGLE_OAUTH_CLIENT_SECRET`: rotated annually or after a suspected leak; rotation involves regenerating the secret in the Google Cloud Console and updating Secrets Manager.
- `DB_PASSWORD`, `DB_PASSWORD_MIGRATIONS`: rotated by RDS rotation hooks (configured in `infra/terraform/modules/secrets/main.tf`); the application picks up the new value on the next worker restart.

## 7. Input Validation

All user-supplied input is validated at three layers. Each layer serves a distinct purpose and the layers operate independently; a failure in one does not compromise the others.

### Three-layer pattern

- Layer 1 (UX): Zod schemas in `frontend/src/schemas/` validate on the client for fast user feedback. Layer 1 is convenience only; the SPA cannot be trusted to enforce any rule.
- Layer 2 (authoritative): pydantic schemas in `backend/app/schemas/` re-validate every payload server-side. Layer 2 is the only authoritative validation; it runs regardless of Layer 1's outcome.
- Layer 3 (last line): PostgreSQL constraints (NOT NULL, CHECK, FK, UNIQUE) catch any escapee from Layer 2. Layer 3 ensures that even an application-level bug cannot persist invalid data.

The three layers manually mirror each other. Field names, types, and constraints match across layers; there is no code generator between them. Every schema change requires a conscious update to all three layers.

### LinkedIn URL validation (F-001, F-010)

LinkedIn URL handling is shared by feature F-001 (Connection Idea Form) and F-010 (Duplicate LinkedIn URL Detection).

- Format validation: must match the regex `^https?://(www\.)?linkedin\.com/.*$`. The regex is encoded in both the Zod schema (`frontend/src/schemas/connection.ts`) and the pydantic schema (`backend/app/schemas/connection.py`). A malformed URL is rejected with HTTP 422 and a field-scoped error message.
- Normalization: the helper `backend/app/utils/url.py:normalize_linkedin_url(url)` lowercases the host, strips trailing slashes, and drops query parameters and fragments. The normalized form is what is persisted in `records.normalized_linkedin_url`.
- Duplicate detection: the unique partial index `(org_id, normalized_linkedin_url) WHERE deleted_at IS NULL` keeps duplicate detection sub-second at the 10K-record scale ceiling.
- Non-blocking warning: per AAP §0.1.2, duplicate detection is a warning, not a block. The frontend shows an inline banner via `frontend/src/features/connections/DuplicateWarning.tsx`; the user may proceed. The unique partial index is not a hard `UNIQUE` constraint in the DDL; it is a partial index whose violation would surface only if the application attempted to insert a duplicate, which it does not.

### AI prompt sanitization

User-supplied `relationship_context` is sanitized before templating into the Anthropic Claude prompt to mitigate prompt injection.

- `backend/app/utils/sanitization.py:sanitize_for_prompt(text)` strips control characters (Unicode classes `Cc` except `\n`, `\r`, `\t`), strips zero-width characters, and applies a length cap (typically 2000 characters).
- The sanitized text is the only form templated into the prompt; the raw form is persisted in the database column for forensic completeness.
- Client-side sanitization in `frontend/src/schemas/connection.ts` is UX-only (it surfaces errors to the user faster); the server is the only authoritative sanitizer.

### Other validated fields

- Email: validated as a syntactically correct email address by both Zod (using its `z.string().email()` helper) and pydantic (using `pydantic.EmailStr`).
- Display name: capped at 200 characters; trimmed of leading and trailing whitespace; non-empty after trimming.
- Tag name: capped at 64 characters; matched against a permissive `^[\w\s\-]+$` pattern; trimmed.
- Outreach status: enforced as one of the four `OutreachStatus` enum values at all three layers.
- Involvement type: enforced as one of the three `InvolvementType` enum values at all three layers.

## 8. Network Security

Network and transport security is enforced at the AWS infrastructure layer plus a comprehensive set of HTTP response headers emitted by the backend security-headers middleware (`backend/app/middleware/security_headers.py`).

### TLS termination

- All public traffic terminates at the AWS ALB with ACM-issued certificates. The certificate is provisioned via `infra/terraform/modules/alb/main.tf` and renewed automatically by ACM.
- ALB security policy: `ELBSecurityPolicy-TLS13-1-2-2021-06` or newer. TLS 1.0 and TLS 1.1 are explicitly disabled.
- HTTP listener on port 80 redirects to HTTPS via an ALB redirect rule (no application code involved).
- HSTS header `Strict-Transport-Security: max-age=31536000; includeSubDomains` is emitted by the backend security-headers middleware on every response when `SESSION_COOKIE_SECURE=True` (production posture). After the first response, conformant browsers will refuse to negotiate HTTP for the deployment domain. The `preload` directive is intentionally omitted to avoid the irreversible HSTS-preload list submission. In development and testing (`SESSION_COOKIE_SECURE=False`) HSTS is suppressed so the dev server can serve plain HTTP on localhost.

### HTTP response security headers

Every backend response — including 2xx successes from blueprint handlers, 4xx and 5xx error envelopes from `app/middleware/error_handlers.py`, the CORS preflight 204, the auth 401 short-circuit, and the public observability endpoints (`/healthz`, `/readyz`, `/metrics`) — carries a uniform set of defensive HTTP response headers attached by `app/middleware/security_headers.py`. The middleware is registered AFTER the CORS middleware and BEFORE the auth middleware so the entire after-request chain layers correlation → CORS → security headers → handler-set values.

| Header | Value | Scope | Rationale |
|--------|-------|-------|-----------|
| `X-Content-Type-Options` | `nosniff` | Every response | Disables browser MIME sniffing on declared `Content-Type` values. Defense-in-depth: a JSON response cannot be rendered as HTML even if a misconfigured intermediate proxy strips the `Content-Type` header. |
| `X-Frame-Options` | `DENY` | Every response | Forbids embedding any backend response inside an iframe. Closes the clickjacking attack surface; the application has no legitimate embed use case. |
| `Referrer-Policy` | `strict-origin-when-cross-origin` | Every response | Sends full origin on same-origin requests; downgrades to origin-only on cross-origin navigations. Query string and path never leak to third-party origins. |
| `Permissions-Policy` | `geolocation=(), microphone=(), camera=()` | Every response | Explicitly disables browser capabilities the application does not use. Reduces the post-XSS attack surface; modern syntax superseding the deprecated `Feature-Policy` header. |
| `Cache-Control` | `no-store, no-cache, must-revalidate, private` | `/api/*` and `/auth/*` non-OPTIONS responses | Backend API and auth responses carry session-scoped data (user identity at `/api/me`, OAuth tokens at `/auth/google/callback`, etc.). None of these may be cached by the browser, by a CDN, or by an intermediate proxy. OPTIONS preflights are exempt so browsers can honor the CORS `Access-Control-Max-Age` directive. `/healthz`, `/readyz`, and `/metrics` are exempt because their pollers benefit from short-window caching and they carry no user data. |
| `Strict-Transport-Security` | `max-age=31536000; includeSubDomains` | Every response when `SESSION_COOKIE_SECURE=True` | Forces conformant browsers to prefer HTTPS for one year. Suppressed in development and testing where the dev server runs over HTTP. |

The middleware is idempotent: if a route handler explicitly sets any of these headers (for example, a future endpoint that wants `Cache-Control: public, max-age=3600` for a cacheable list), the existing value is preserved verbatim. This matches the convention of the correlation middleware. Content-Security-Policy is intentionally NOT set by the backend because the API serves only JSON; the frontend nginx is responsible for any CSP attached to the SPA shell.

The deployment topology in AAP §0.4.6 routes `/api/*` and `/auth/*` directly from the AWS ALB to Flask without nginx interposition, so these headers are the sole defense layer for those paths. The frontend nginx in `frontend/nginx.conf` sets the same defensive headers on the SPA static-asset path it serves.

### Internal traffic

- ALB to Fargate is over the private VPC subnet; no Internet routing is involved.
- Fargate to RDS is over the private VPC subnet, restricted by security groups (see below).
- Fargate to AWS Secrets Manager goes via VPC endpoint (preferred for cost and latency) or via NAT gateway as fallback.
- Fargate to Anthropic and Google goes via NAT gateway over the public Internet, HTTPS-only, with TLS validated at the SDK level.

### Security groups

Security groups are defined in `infra/terraform/modules/network/main.tf` and applied to the corresponding modules. The least-privilege rules are:

- ALB SG: ingress 443 from `0.0.0.0/0`; ingress 80 from `0.0.0.0/0` (to support HTTP-to-HTTPS redirect); egress to Fargate SG on port 8000.
- Fargate SG: ingress 8000 from ALB SG only; egress to RDS SG on port 5432, to the Secrets Manager VPC endpoint on port 443, and to the NAT gateway for outbound HTTPS.
- RDS SG: ingress 5432 from Fargate SG only; no egress required.
- Secrets Manager VPC endpoint SG: ingress 443 from Fargate SG.

The security-group restriction on RDS guarantees that even if a Fargate task is compromised, the attacker cannot pivot to a different VPC or to other AWS accounts; the database is reachable only from the application tier.

### CORS

In production the SPA and the API share an origin (a single ALB serves both static assets and `/api/*`), so CORS is not invoked.

In local development, the Vite dev server on port 5173 proxies `/api/*` and `/auth/*` to Flask on port 5000 via the proxy configuration in `frontend/vite.config.ts`. From the browser's perspective, both are served from `http://localhost:5173`, again avoiding CORS.

If CORS is ever required in a future environment, allowed origins are explicitly whitelisted in `backend/app/__init__.py`. The wildcard `*` is never used; every origin is enumerated.

## 9. Frontend Security

The SPA's security posture covers Content Security Policy, cookie attributes, and the prohibition on `dangerouslySetInnerHTML`.

### Content Security Policy

Content-Security-Policy is intentionally NOT emitted by the backend in the MVP delivery, consistent with the rationale given in §8 ("Network Security"): the backend serves only JSON, and a CSP attached to JSON responses is irrelevant because the responses are never rendered as HTML by the browser. CSP for the SPA shell is the responsibility of the static-asset host that serves `index.html`.

The current MVP runtime exposes the SPA from the Vite dev server (in local development) or from an nginx static-asset image (in production). Neither host emits a Content-Security-Policy header in the MVP delivery; the `frontend/nginx.conf` baseline is intentionally minimal and does not yet include a CSP block. The SPA therefore relies on its other defenses — React's automatic JSX escaping, the ESLint ban on `dangerouslySetInnerHTML`, the HttpOnly + Secure + SameSite=Lax cookie attributes, the `X-Frame-Options: DENY` header from the backend (and from nginx for the SPA shell), and the cross-origin restrictions of the deployment topology — to mitigate the XSS-and-clickjacking class of attack.

A future `frontend/nginx.conf` change would add the following CSP for the SPA shell:

- `default-src 'self'` — the implicit default; locks all unspecified categories to same-origin.
- `script-src 'self'` — only same-origin scripts are allowed; no inline scripts; no `eval`. The Vite-built bundle is the only executable JavaScript.
- `style-src 'self' 'unsafe-inline'` — Tailwind's utility classes occasionally rely on inline styles for dynamic computations; the `'unsafe-inline'` token is required for these to render. The risk is bounded because no untrusted content is ever templated into a `<style>` tag.
- `img-src 'self' data:` — same-origin images plus inline `data:` URIs (used by Lucide icons).
- `connect-src 'self'` — same-origin XHR and `fetch`. The SPA never calls third-party APIs directly.
- `frame-ancestors 'none'` — prevents the SPA from being embedded in an `<iframe>`, mitigating clickjacking. Currently delivered by the backend's `X-Frame-Options: DENY` header instead.
- `base-uri 'self'` — prevents `<base>` tag injection from redirecting all relative URLs.

The reveal.js executive deck (`blitzy-deck/index.html`) loads from CDN; it is a separate static asset hosted outside the application origin in operational settings and is not in scope of the SPA's CSP discussion above.

### Cookie posture

- Session cookie: `HttpOnly; Secure; SameSite=Lax; Path=/; Domain=<root-domain>`. The HttpOnly attribute makes the cookie unreadable to JavaScript, mitigating XSS-based token theft.
- OAuth state cookie: same attributes plus a 10-minute expiry; cleared after the callback completes.
- No localStorage or sessionStorage is used for any auth-bearing token. Tokens are XSS-resistant by virtue of being unreachable from JavaScript.
- No third-party cookies are set or read.

### No `dangerouslySetInnerHTML`

The use of `dangerouslySetInnerHTML` is forbidden in feature components.

- Enforcement: an ESLint rule (`react/no-danger`) errors on any use of `dangerouslySetInnerHTML` and is configured at error severity in `frontend/.eslintrc` (or its equivalent flat-config file).
- Exception: `EditHistoryFeed.tsx` may render server-sanitized Markdown for audit events. Any such use is flanked by a comment explaining the sanitization source and is reviewed in code review. The sanitization is performed server-side; the React component receives already-safe HTML and renders it.
- React's default JSX text rendering automatically escapes interpolated content, neutralizing XSS via stored fields like `relationship_context` and `ai_notes`.

### Subresource Integrity (SRI)

Subresource Integrity is NOT applied to the reveal.js executive deck's CDN resources in the MVP delivery. The deck loads three third-party libraries (reveal.js 5.1.0, Mermaid 11.4.0, Lucide 0.460.0) from cdnjs.cloudflare.com via plain `<script>` and `<link>` tags without `integrity` attributes.

The compensating posture is:

- The deck is a separate static asset served outside the application origin and does not execute against the SPA's session cookie or backend API. A compromise of one of the CDN bundles cannot exfiltrate user data because the deck is not within the credentialed origin.
- CDN versions are exact-pinned (no `latest`, no caret ranges) so there is no automatic version drift; a tampered bundle would be a deliberate supply-chain attack on cdnjs that affects every downstream consumer simultaneously, which is detectable out-of-band.

Adding SRI to the deck is tracked as a future hardening task: compute `sha384` checksums for each pinned bundle, append `integrity="sha384-..." crossorigin="anonymous"` to each tag in `blitzy-deck/index.html`, and add a CI check that re-computes the hashes on every deck update. Earlier drafts of this document described SRI as already-applied; that description was incorrect and has been removed.

## 10. Compliance and Privacy

The Sales-Connections platform stores personally identifiable information (PII). The compliance and privacy posture documented here is forward-looking; full GDPR and CCPA compliance is post-MVP, but the architecture is engineered to support compliance work in subsequent releases.

### PII handling

The system stores the following categories of PII:

- Subject names (the people whose connection is being tracked).
- Subject LinkedIn URLs.
- Subject company and job title.
- Free-text relationship context describing the connection.
- AI-generated outreach notes (derived from relationship context).
- User account fields (email, display name).

PII never leaves the application database except via the following intentional flows:

- The Anthropic Claude API. Relationship context is included in the prompt sent to `api.anthropic.com`. Per Anthropic's Commercial Terms (current as of the documented version), prompts and completions are not used to train models by default, but operators should verify the policy at `https://www.anthropic.com/legal` before sending production PII.
- The audit log. Audit `before` and `after` payloads carry copies of the same PII for forensic purposes. The audit log is append-only and is governed by the same access controls as the primary records table.

There is no PII export endpoint in MVP. The Admin Panel surfaces user lists and analytics aggregates but does not provide a downloadable CSV of records.

### Data retention

- Soft-deleted records remain indefinitely until an Admin hard-deletes them. There is no automatic purge.
- Audit events are retained indefinitely (append-only).
- User accounts persist indefinitely; deactivated users are not deleted, only marked.
- Concrete retention durations are post-MVP per AAP §0.6.2; no SLA is committed in the MVP delivery.

### Right to be forgotten

Right-to-be-forgotten support is post-MVP. The current MVP behavior is:

- Admin hard-delete removes the record from the primary table. The corresponding audit row remains as a permanent forensic record.
- Admin user-deactivation marks the user but does not remove their identity from records they own (the `owner_user_id` and `owner_display_name` fields persist).
- Full GDPR-style erasure would require a future `delete_user_data` admin operation that scrubs PII from audit rows, retaining the structural skeleton for forensic continuity. The design is TBD; the operation would emit a single `admin_op` audit event documenting the erasure for accountability.

### Anthropic data usage

The Anthropic Claude API is the only third-party PII recipient in the standard data flow.

- Per Anthropic's Commercial Terms (current as of the documented version), prompts and completions are not used to train Anthropic models by default.
- Operators should verify the current policy at `https://www.anthropic.com/legal` before deploying to production.
- The flow is documented for end users in the future privacy policy (post-MVP).
- Users with privacy concerns can decline to invoke AI note generation; the form does not require an AI completion to submit.

### Cross-border data transfer

The MVP runtime is single-region within AWS. AWS Secrets Manager, RDS, Fargate, ALB, and CloudWatch all reside in the deployment region. The Anthropic API is hosted by Anthropic; its data residency depends on Anthropic's deployment topology and is documented in Anthropic's terms.

Cross-border transfer awareness is post-MVP; multi-region deployment, region-pinned data residency, and Standard Contractual Clauses are out of scope for this delivery.

## 11. Verification

The security invariants documented above are verified by automated tests, static analysis, and pre-launch manual review. This section catalogues the verification mechanisms.

### Test coverage for security invariants

- `backend/tests/services/test_rbac.py` — Permission matrix coverage for all three roles across every endpoint. Each cell of the matrix in §3 has a corresponding test that asserts the expected outcome (200, 403, or 404).
- `backend/tests/services/test_audit.py` — Atomic emission test: a record-create raises mid-transaction and asserts neither the record nor the audit row is persisted. Append-only enforcement test: direct SQL `UPDATE` and `DELETE` against `audit_events` from the application role both raise `psycopg.errors.InsufficientPrivilege`.
- `backend/tests/api/test_auth.py` — OAuth happy path; invalid `state` returns 400; expired ID token rejected; tampered ID token rejected; email/password happy path; incorrect password returns 401; expired session JWT returns 401; logout invalidates the session.
- `backend/tests/api/test_connections.py` — Cross-org access returns 404 (the request hits a nonexistent record from the requester's org perspective); client-supplied `owner_user_id` rejected with 422; client-supplied `org_id` rejected with 422.
- `backend/tests/observability/test_logging_redaction.py` — Secret redaction: log payloads with keys named `password`, `api_key`, `client_secret`, `authorization`, `id_token`, `access_token`, `refresh_token` all render with `***REDACTED***`.
- `backend/tests/services/test_duplicate_detection.py` — LinkedIn URL normalization: trailing-slash, query-string, fragment, www-vs-no-www, scheme-relative, and uppercase-host variants all collapse to the same normalized form.
- `frontend/tests/auth/RoleGate.test.tsx` — UI hides admin buttons when the session role is not Admin.
- `frontend/tests/api/client.test.ts` — Fetch wrapper sets `credentials: 'include'`, attaches correlation ID, and redirects to `/login` on HTTP 401.

### Static analysis

- bandit (advisory) for Python security smells. Configured in `backend/pyproject.toml`; runs in CI on every PR.
- `ruff` for general Python linting; configured to error on banned patterns (for example, raw SQL string concatenation).
- `mypy` for advisory static typing.
- `eslint` for TypeScript with `react/no-danger` set to error severity.
- `npm audit` on the frontend; configured in CI to fail on high-severity advisories.
- Dependabot alerts in CI via `.github/workflows/dependabot.yml`. Pull requests are raised automatically for vulnerable dependencies; humans review each upgrade and add a corresponding decision-log row when a non-trivial change is introduced.
- ECR native scanner runs on every image push; vulnerabilities at high severity block deployment via the CD workflow's gating step.

### Penetration testing

- Pre-launch penetration test: out of scope for MVP delivery; scheduled for the post-launch window once the platform reaches production traffic.
- OWASP Top 10 checklist: verified manually before each release as part of the release readiness review. The checklist covers injection, broken authentication, sensitive data exposure, XML external entities, broken access control, security misconfiguration, XSS, insecure deserialization, components with known vulnerabilities, and insufficient logging and monitoring.

### Continuous verification in CI

The CI pipeline (`.github/workflows/ci.yml`) runs the following security-relevant gates on every pull request:

- Lint (ruff, eslint, prettier --check).
- Type-check (`tsc --noEmit`, mypy).
- Unit and integration tests (pytest with coverage threshold 85, vitest with coverage threshold 85).
- Static security scan (bandit advisory).
- Dependency audit (npm audit, pip-audit).
- Docker image build and ECR scanner (CD pipeline only, post-merge).

A pull request that fails any of these gates is blocked from merge. The CD pipeline (`.github/workflows/cd.yml`) reapplies the same gates on the post-merge build and additionally runs the ECR scanner before promoting an image to staging or production.

## See Also

- [`README.md`](../README.md) — project entry point and quick-start.
- [`onboarding.md`](onboarding.md) — clean-machine to running app, domain glossary, common pitfalls, suggested next tasks.
- [`architecture.md`](architecture.md) — what the architecture is (this document explains the security posture in detail).
- [`api.md`](api.md) — REST endpoint catalog with request and response shapes.
- [`operations.md`](operations.md) — operational runbook including secret rotation procedures (§4).
- [`decision-log.md`](decision-log.md) — rationale for every non-trivial decision; the canonical "why" reference.
- `backend/app/middleware/auth.py` — JWT validation on every authenticated request.
- `backend/app/middleware/rbac.py` — `@requires_role` decorator enforcing the permission matrix.
- `backend/app/services/auth.py` — password hashing, JWT mint and verify, OAuth user upsert.
- `backend/app/services/audit.py` — `emit_audit_event(...)` invoked inside the parent transaction of every state-changing operation.
- `backend/app/services/connections.py` — service-level org-scoping and soft-delete-aware queries.
- `backend/app/observability/logging.py` — structlog redaction processor for secret-named keys.
- `backend/app/utils/url.py` — LinkedIn URL validation and normalization helpers.
- `backend/app/utils/sanitization.py` — server-side sanitization of `relationship_context` before AI prompt templating.
- `backend/migrations/versions/0001_initial_schema.py` — database-level `GRANT INSERT` and `REVOKE UPDATE, DELETE` for `audit_events`.
- `infra/terraform/modules/secrets/main.tf` — AWS Secrets Manager entries and rotation hooks.
- `infra/terraform/modules/network/main.tf` — VPC, subnets, and security groups.
- `infra/terraform/modules/alb/main.tf` — ALB security policy and ACM certificate.
- `frontend/src/auth/AuthProvider.tsx` — session context exposing `useSession()` and `useRole()`.
- `frontend/src/auth/RoleGate.tsx` — UI-secondary defense for role-gated buttons.
- `frontend/src/api/client.ts` — fetch wrapper with credential inclusion, correlation propagation, and 401 redirect.
