<!--
Sales-Connections Pull Request Template
=======================================
This file is mandated by AAP §0.6.1 ("PR template enforcing decision-log,
diagram, observability, onboarding, deck checklist"). It enforces the
user's five implementation rules from AAP §0.7.5 plus the project's
coding and quality standards. PRs that fail to address every applicable
item will be requested for changes.

Delete sections that genuinely do not apply (e.g., a docs-only PR may skip
the "Tests" section). Do NOT delete sections to avoid the work; honest
N/A annotations are preferred over silent omission.

The five user-supplied implementation rules (AAP §0.7.5):
  1. Explainability                  -> docs/decision-log.md row per non-trivial choice
  2. Visual Architecture             -> Mermaid diagrams under docs/diagrams/
  3. Observability                   -> logs (correlation IDs), metrics, traces, health
  4. Onboarding & Continued Dev      -> README.md + docs/onboarding.md
  5. Executive Presentation          -> blitzy-deck/index.html reveal.js deck

The eight architectural invariants (AAP §0.7.1) and the security
invariants (AAP §0.7.4) are reinforced under "Security Invariants" below.
-->

## Summary

<!-- One paragraph: WHAT changed, WHY, and which AAP feature(s) it advances. -->
<!-- Example: "Implements the F-002 AI Note Generation endpoint POST /api/notes/generate using Langchain over the Anthropic SDK 0.97.0. Adds a 5-second timeout watchdog and structured-log redaction of prompt content. Closes #123." -->


## Linked Issues / Features

<!-- One per line, e.g.: -->
<!-- - Closes #123 -->
<!-- - Implements F-002 AI Note Generation -->
<!-- - References F-001 Connection Idea Form -->


## Type of Change

<!-- Tick exactly one. Multi-purpose PRs should be split into focused PRs. -->

- [ ] Bug fix (non-breaking change which fixes an issue)
- [ ] New feature (non-breaking change which adds functionality)
- [ ] Breaking change (fix or feature that would cause existing functionality to not work as expected)
- [ ] Documentation update
- [ ] Infrastructure / CI / governance change
- [ ] Refactor (no functional change)
- [ ] Security fix
- [ ] Dependency update (Dependabot or manual)


## Rule 1: Explainability — Decision Log Coverage

> AAP §0.7.5: "Every non-trivial implementation decision must be documented in
> `docs/decision-log.md` as a Markdown table row: what was decided, what
> alternatives existed, why this choice was made, what risks it carries.
> Rationale must not be embedded in code comments."

- [ ] **N/A** — This PR introduces no non-trivial design decisions (e.g., trivial typo fix, dependency bump with default semantics).
- [ ] Added a row to `docs/decision-log.md` for every non-trivial decision in this PR.
- [ ] Each new row records: **Decision** / **Alternatives Considered** / **Rationale** / **Risks** / **Author** / **Date**.
- [ ] No rationale is buried in code comments — comments describe *what*, not *why*.
- [ ] If a previous decision-log row was superseded, the original row is annotated with the new row number rather than deleted.


## Rule 2: Visual Architecture Documentation — Mermaid Diagrams

> AAP §0.7.5: "All visual documentation uses Mermaid diagrams stored as `.mmd`
> files under `docs/diagrams/` and referenced by name from `docs/architecture.md`.
> Each diagram has a descriptive title and a legend."

- [ ] **N/A** — This PR does not change architecture or component relationships.
- [ ] Added/updated `.mmd` source under `docs/diagrams/` for every architecture change.
- [ ] Every new or updated diagram has a descriptive title (`%% Diagram: ...`) and a legend (`%% Legend: ...`).
- [ ] Every diagram is referenced by name from `docs/architecture.md` (or another canonical doc).
- [ ] No architecture is described in prose where a Mermaid diagram communicates more clearly.


## Rule 3: Observability — Logs, Metrics, Traces, Health

> AAP §0.7.5: "The application is not complete until it is observable. The
> deliverable includes structured logging with correlation IDs, distributed
> tracing, a metrics endpoint, health/readiness checks, and a CloudWatch
> dashboard template — verified end-to-end in the local Docker Compose
> environment."

- [ ] **N/A** — This PR adds no new code paths, no new endpoints, and no new state-changing operations.
- [ ] **Logs** — Every new log line uses `structlog` (backend) or `console.warn` / `console.error` (frontend); correlation ID propagated via `frontend/src/lib/correlationId.ts` and `backend/app/middleware/correlation.py`.
- [ ] **Logs** — No secret-named keys (`*_key`, `*_secret`, `password`, `token`, `authorization`) appear in log payloads (the structlog redaction processor in `backend/app/observability/logging.py` should already cover this; confirm via grep).
- [ ] **Metrics** — Every new code path is covered by a Prometheus counter, histogram, or gauge in `backend/app/observability/metrics.py`.
- [ ] **Tracing** — New service-to-service or external-API calls have explicit OpenTelemetry spans (or rely on the auto-instrumentation in `backend/app/observability/tracing.py`).
- [ ] **Health** — `GET /healthz` (liveness) and `GET /readyz` (readiness, includes DB ping) still return 200 after this change.
- [ ] **Local verification** — Ran `docker-compose up --build` locally and verified logs, metrics endpoint (`/metrics`), and `/healthz` and `/readyz` all return as expected.
- [ ] **Dashboard** — Updated `infra/terraform/modules/observability/main.tf` if a new metric or alarm threshold was introduced.


## Rule 4: Onboarding & Continued Development

> AAP §0.7.5: "`README.md` and `docs/onboarding.md` together enable a new
> developer to go from a clean machine to a running, modifiable application
> without asking questions."

- [ ] **N/A** — This PR introduces no developer-workflow change (e.g., no new env var, no new tool, no new entrypoint).
- [ ] Updated `README.md` Quick Start if the run/setup procedure changed.
- [ ] Updated `docs/onboarding.md` if domain context, common pitfalls, or "Suggested Next Tasks" need to evolve.
- [ ] Updated `backend/.env.example` and/or `frontend/.env.example` for any new environment variable, including its purpose, allowed values, and default.
- [ ] Verified by walking through the Quick Start on a clean shell (`docker-compose down -v && docker-compose up --build`) — full stack reaches healthy state without manual intervention.
- [ ] Added a `## Common Pitfalls` entry in `docs/onboarding.md` if this PR addresses a recurring source of contributor confusion.


## Rule 5: Executive Presentation — reveal.js Deck

> AAP §0.7.5: "`blitzy-deck/index.html` is a single self-contained reveal.js
> HTML file targeting non-technical leadership ... 12–18 slides; all four slide
> types (Title, Section Divider, Content, Closing); every slide carries at
> least one non-text visual element; pinned reveal.js 5.1.0, Mermaid 11.4.0,
> Lucide 0.460.0; canonical theme at
> `blitzy-deck/references/blitzy-reveal-theme.css`. No emoji; no fenced
> code blocks; reveal.js configured with `hash: true`, `transition: 'slide'`,
> `controlsTutorial: false`, `width: 1920`, `height: 1080`. Mermaid
> initialization uses `startOnLoad: false` and is invoked on the reveal.js
> `ready` event and every `slidechanged` event; Lucide icons rendered the
> same way."

- [ ] **N/A** — This PR introduces no externally-visible scope change that leadership needs to be briefed on.
- [ ] Updated `blitzy-deck/index.html` for any externally-visible scope change (new feature, breaking change, architectural shift, risk update).
- [ ] Slide count remains within 12–18 (target 16).
- [ ] All four slide types still present (Title, Section Divider, Content, Closing).
- [ ] Every slide carries at least one non-text visual element.
- [ ] No emoji introduced; no fenced code blocks introduced.
- [ ] CDN versions remain pinned: reveal.js 5.1.0, Mermaid 11.4.0, Lucide 0.460.0.
- [ ] Canonical theme file at `blitzy-deck/references/blitzy-reveal-theme.css` still referenced.
- [ ] Reveal.js config remains: `hash: true`, `transition: 'slide'`, `controlsTutorial: false`, `width: 1920`, `height: 1080`.
- [ ] Mermaid `startOnLoad: false`; rendering invoked on `ready` and `slidechanged` events; same lifecycle for Lucide icons.


## Tests

> AAP §0.7.7: "Test coverage threshold 85%. Enforced by pytest `--cov-fail-under`
> and vitest coverage threshold."

- [ ] **N/A** — This PR is documentation-only or scaffolding-only with no executable code change.
- [ ] Backend unit/integration tests added or updated under `backend/tests/`; `pytest --cov-fail-under=85` passes locally.
- [ ] Frontend component tests added or updated under `frontend/tests/`; `npm run test:coverage` meets the 85% statements/lines and 80% branches thresholds.
- [ ] **Audit emission verified** — Per AAP §0.7.7, every state-changing endpoint MUST emit an `audit_events` row. Tests assert this for every endpoint touched in this PR.
- [ ] **No flaky tests introduced** — Tests pass deterministically across at least three local runs.
- [ ] Tests follow the project convention: backend tests use `pytest` + `factory-boy` factories; frontend tests use `vitest` + `@testing-library/react` + `msw`.


## Type Safety

> AAP §0.7.7: "Type safety on the frontend. `tsc --noEmit` is part of CI.
> `any` is forbidden except where third-party types are missing and a typed
> shim has been added under `frontend/src/types/`. Type safety on the backend.
> mypy runs in CI as advisory."

- [ ] Frontend: `cd frontend && npm run typecheck` passes (runs both `tsconfig.json` and `tsconfig.node.json`).
- [ ] Frontend: No new uses of `any` outside `frontend/src/types/` shims (search the diff for `: any` and `as any`).
- [ ] Backend: `cd backend && mypy app tests` passes (or warnings are documented in the decision log).
- [ ] No `# type: ignore` comments added without an accompanying explanation.


## Lint and Format

> AAP §0.7.7: "Lint must pass. ruff and eslint exit-code-zero on every PR;
> prettier --check passes on every TS/TSX file."

- [ ] Backend: `cd backend && ruff check . && ruff format --check .` exits 0.
- [ ] Frontend: `cd frontend && npm run lint && npm run format:check` exits 0.
- [ ] `--max-warnings 0` is honored — zero warnings allowed.


## Pinned Dependency Versions

> AAP §0.7.7: "Pinned versions everywhere. No `latest`, no `^`, no `~`
> ranges in dependency manifests."

- [ ] **N/A** — This PR introduces no dependency changes.
- [ ] Any new entries in `backend/requirements.txt`, `backend/requirements-dev.txt`, `frontend/package.json` use exact `==` (Python) or exact version (npm, no `^` or `~`).
- [ ] `frontend/package-lock.json` regenerated and committed if `frontend/package.json` changed.
- [ ] Anthropic SDK pinned at `0.97.0` (or higher only with a decision-log entry per AAP §0.2.4).
- [ ] Flask pinned at `3.1.3` (or higher only with a decision-log entry per AAP §0.2.4).


## Security Invariants

> AAP §0.7.4: "OAuth tokens never exposed to the client ... Anthropic API
> credential held server-side only ... Audit table writes restricted to
> backend service identity ... Owner identity always derived from session ...
> Org-scoped queries enforced on every read/write."

- [ ] **N/A** — This PR touches no auth, no data access, no audit emission, no AI integration.
- [ ] No client-supplied `owner_user_id` or `owner_display_name` is accepted by any pydantic schema (rejection enforced server-side).
- [ ] Every new database read/write is `org_id`-scoped via `g.session.org_id` (no cross-org reads).
- [ ] Every new read on `records` is soft-delete-aware (`WHERE deleted_at IS NULL`) unless explicitly an admin moderation view that opts out.
- [ ] No code path issues `UPDATE` or `DELETE` on `audit_events`.
- [ ] No new direct `fetch()` calls in frontend code — all HTTP traffic flows through `frontend/src/api/client.ts`.
- [ ] No new direct Anthropic SDK imports outside `backend/app/services/ai_orchestration.py`.
- [ ] Secrets are never logged — confirmed by sample log inspection.


## Performance Budgets

> AAP §0.7.3: "AI ≤ 5s P95, auth ≤ 2s, form submit ≤ 2s, RBAC ≪ 50ms,
> audit ≤ 100ms, duplicate check sub-second at 10K records, feed responsive
> at 10K records."

- [ ] **N/A** — This PR does not change a code path subject to a performance budget.
- [ ] If this PR touches the AI integration: the 5-second timeout watchdog in `backend/app/services/ai_orchestration.py` remains intact.
- [ ] If this PR adds a database query: the `EXPLAIN ANALYZE` plan for the query at 10K records (or larger) does not introduce a sequential scan on `records`.
- [ ] If this PR adds a new endpoint: the corresponding Prometheus histogram alarm is configured in `infra/terraform/modules/observability/main.tf`.


## Migrations and Database Changes

> AAP §0.7.7: "Migrations forward-compatible. Alembic migrations never
> destructively drop columns in MVP. Column drops happen in a separate later
> migration after verification."

- [ ] **N/A** — This PR introduces no database schema change.
- [ ] New migration added under `backend/migrations/versions/` with an idempotent `upgrade()` and `downgrade()`.
- [ ] No destructive operations (DROP COLUMN, DROP TABLE, RENAME) without an explicit decision-log entry.
- [ ] Migration tested locally: `alembic upgrade head` and `alembic downgrade -1` both succeed against a fresh PostgreSQL 17 database.
- [ ] Index additions are concurrent (`CREATE INDEX CONCURRENTLY`) for tables expected to grow.


## Breaking Changes

- [ ] **N/A** — This PR introduces no breaking changes.
- [ ] Breaking change documented in `docs/decision-log.md` with migration guidance.
- [ ] All callers / consumers updated in the same PR.
- [ ] Externally-visible breaks (API contract changes, env-var renames) reflected in `blitzy-deck/index.html` per Rule 5.


## How to Test This PR

<!-- Concrete steps a reviewer can run to validate the change. Example: -->
<!-- 1. `git checkout pr-branch && docker-compose up --build`
     2. Visit http://localhost:5173/feed
     3. Click "Add Connection" and submit a record with relationship context
     4. Confirm the AI notes block appears within 5 seconds (or a non-blocking error message)
     5. Inspect the audit_events table:
        `docker-compose exec postgres psql -U sales_connections -c 'SELECT * FROM audit_events ORDER BY event_timestamp DESC LIMIT 5;'`
     6. Walk the soft-delete path: DELETE the record and confirm it disappears from /feed
        but is still visible in /admin/records (Admin role, "show deleted" toggle on). -->


## Screenshots / Recordings

<!-- For UI changes only; attach before-and-after screenshots or short recordings.
     Include mobile viewport screenshots when the change affects responsive layout
     (per AAP §0.7.2 mobile-responsive constraint). -->


## Reviewer Checklist

<!-- The reviewer ticks these as part of their review pass. -->

- [ ] Read the linked AAP feature(s) and confirmed this PR matches scope.
- [ ] Walked through the "How to Test This PR" steps.
- [ ] Verified the decision-log entries (if any) are clear and complete.
- [ ] Confirmed no scope creep beyond the feature(s) named above.
- [ ] Looked for and rejected any new use of `any`, `latest`, `^`, `~`, or relative-traversal imports.
- [ ] Confirmed audit emission for every state-changing endpoint touched.
- [ ] Confirmed CODEOWNERS routed the right teams to review (auto-handled by GitHub via `.github/CODEOWNERS`).

---

<!--
  Reminder: PRs that bypass any of the five user-supplied implementation
  rules (Explainability, Visual Architecture, Observability, Onboarding,
  Executive Presentation) will be requested for changes. The rules are
  authoritative project requirements per AAP §0.7.5; they are not
  optional and they are not negotiable.
-->
