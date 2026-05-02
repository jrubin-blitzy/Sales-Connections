# Onboarding Guide

This guide walks a new contributor from a clean machine to a running, modifiable Sales-Connections application without requiring any tribal knowledge. The README at the repository root contains the 5-minute Quick Start; this guide goes deeper into domain context, common pitfalls, and how to extend the codebase. Decision rationale (the "why") lives in [`decision-log.md`](decision-log.md) per the user-supplied Explainability rule; this document describes only WHAT and HOW.

## Table of Contents

- [1. Prerequisites](#1-prerequisites)
- [2. First Run](#2-first-run)
- [3. Domain Context](#3-domain-context)
- [4. Repository Layout](#4-repository-layout)
- [5. Common Development Tasks](#5-common-development-tasks)
- [6. Common Pitfalls](#6-common-pitfalls)
- [7. Observability Verification](#7-observability-verification)
- [8. Extending the Project](#8-extending-the-project)
- [9. Suggested Next Tasks](#9-suggested-next-tasks)
- [10. Troubleshooting](#10-troubleshooting)
- [11. Glossary](#11-glossary)
- [12. See Also](#12-see-also)

## 1. Prerequisites

The Sales-Connections platform is supported on three host architectures: x86_64 Linux, x86_64 macOS, and ARM64 (Apple Silicon) macOS. Windows users are supported via WSL2 (Windows Subsystem for Linux 2) running an Ubuntu 22.04 guest; native Windows toolchains are not supported.

### Operating system

| OS | Minimum Version | Notes |
|----|-----------------|-------|
| macOS | 13 (Ventura) | x86_64 and ARM64 (Apple Silicon) both supported. |
| Ubuntu | 22.04 LTS or newer | Tested baseline for Linux contributors. |
| Windows | 11 with WSL2 | Use Ubuntu 22.04 inside WSL2; native Windows toolchains are unsupported. |

For Windows users, install WSL2 from an elevated PowerShell:

```powershell
wsl --install -d Ubuntu-22.04
```

After the WSL2 reboot, all subsequent commands in this guide are run inside the Ubuntu shell, not in PowerShell.

### Required tooling

The local toolchain is anchored to four runtimes. Every contributor needs all four installed before the First Run section will succeed.

| Tool | Required Version | Verified With | Reason |
|------|------------------|---------------|--------|
| Python | 3.12.x | `python3 --version` | Backend runtime; matches the production container. |
| Node.js | 20.x LTS (>= 20.20.2) | `node --version` | Frontend toolchain; pinned by the Vite and Vitest releases. |
| Docker Engine | 24.x or higher (with Compose plugin) | `docker --version` and `docker compose version` | Runs the local PostgreSQL, backend, and frontend stack. |
| Git | 2.40+ | `git --version` | Source control; older versions miss `git switch` semantics. |

#### Python 3.12 via pyenv

The recommended way to manage Python versions is `pyenv`, which avoids polluting the system Python.

macOS:

```bash
brew install pyenv
pyenv install 3.12.7
pyenv global 3.12.7
```

Ubuntu / WSL2:

```bash
sudo apt-get update
sudo apt-get install -y build-essential libssl-dev zlib1g-dev libbz2-dev \
    libreadline-dev libsqlite3-dev curl libncursesw5-dev xz-utils tk-dev \
    libxml2-dev libxmlsec1-dev libffi-dev liblzma-dev
curl https://pyenv.run | bash
```

Add the following to `~/.bashrc` (or `~/.zshrc`), reload the shell, then install Python 3.12:

```bash
export PYENV_ROOT="$HOME/.pyenv"
export PATH="$PYENV_ROOT/bin:$PATH"
eval "$(pyenv init -)"
```

```bash
pyenv install 3.12.7
pyenv global 3.12.7
```

#### Node.js 20 LTS via nvm

```bash
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash
# Reload shell, then:
nvm install 20
nvm use 20
nvm alias default 20
```

Verify:

```bash
node --version    # v20.20.2 or newer
npm --version     # 10.x or newer
```

#### Docker Engine

macOS users install Docker Desktop from `https://www.docker.com/products/docker-desktop/`. Linux and WSL2 users install Docker Engine via the package manager:

```bash
# Ubuntu / WSL2
sudo apt-get install -y docker.io docker-compose-plugin
sudo usermod -aG docker "$USER"
# Log out and back in for the group change to take effect.
```

Verify:

```bash
docker --version            # Docker version 24.x or newer
docker compose version      # Docker Compose version v2.x
```

### Optional tooling

The following are required only for specific work streams. Skip them on first run.

| Tool | Required For | Install |
|------|--------------|---------|
| Terraform 1.7+ | Infrastructure changes under `infra/terraform/` | Install via `tfenv` (`brew install tfenv && tfenv install 1.7.5`). |
| AWS CLI v2 | Production deploys, secret rotation | Install per `https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html`. |
| pgAdmin 4 | DB GUI for debugging | Optional `pgadmin` profile in `docker-compose.yml`; or install on host. |
| jq | JSON pretty-printing in shell scripts | `brew install jq` or `sudo apt-get install -y jq`. |

### Environment variables

The platform reads its configuration from environment variables. Defaults sufficient for local development are baked into `backend/.env.example` and `frontend/.env.example`. The four backend secrets and one frontend variable below are the ones a new contributor must understand.

| Variable | Tier | Required | Purpose |
|----------|------|----------|---------|
| `ANTHROPIC_API_KEY` | backend | For F-002 (AI note generation) | Anthropic Console key; if absent the AI endpoint returns a 504 and the form still submits. |
| `GOOGLE_OAUTH_CLIENT_ID` | backend | For F-012 (Google sign-in) | OAuth 2.0 client ID; the email/password fallback works without it. |
| `GOOGLE_OAUTH_CLIENT_SECRET` | backend | For F-012 (Google sign-in) | OAuth 2.0 client secret; pair with the client ID. |
| `JWT_SIGNING_KEY` | backend | Always | HS256 signing key for the session JWT; use 32 bytes of entropy. |
| `DATABASE_URL` | backend | Always | PostgreSQL DSN; defaults to the local Docker Compose Postgres. |
| `VITE_API_BASE_URL` | frontend | Always | Base URL of the backend API; defaults to `http://localhost:5000`. |

In production, the four backend secrets are sourced from AWS Secrets Manager at process startup; in local development they live in `backend/.env`. The `.env` file is git-ignored; the `.env.example` template is committed.

## 2. First Run

The First Run sequence takes a clean machine to a running Sales-Connections stack. Total elapsed time on a typical broadband connection is approximately 8 minutes (mostly Docker image pulls and `npm install`).

### Step 1 — Clone the repository

```bash
git clone <repository-url> sales-connections
cd sales-connections
```

The `<repository-url>` is the HTTPS or SSH URL of the GitHub remote. Replace the placeholder before running.

### Step 2 — Create environment files

```bash
cp backend/.env.example backend/.env
cp frontend/.env.example frontend/.env
```

The `.env.example` files are committed to source; the `.env` files are git-ignored. The copy step gives each developer a private editable file without exposing secrets through the repository.

### Step 3 — Populate backend secrets

Edit `backend/.env` and set values for the following four keys. Each line documents what to do if the key is empty.

- `ANTHROPIC_API_KEY` — Your Anthropic Console API key. Generate one at `https://console.anthropic.com/settings/keys`. If left empty, AI note generation gracefully fails with HTTP 504 and `error.code = "ai_timeout"`; the SPA renders a non-blocking "AI unavailable; you can still submit" affordance and the form remains submittable. This is intentional per the non-blocking AI failure design.

- `GOOGLE_OAUTH_CLIENT_ID` and `GOOGLE_OAUTH_CLIENT_SECRET` — Create a Google Cloud OAuth 2.0 client at `https://console.cloud.google.com/apis/credentials`. Set the application type to "Web application", authorize the redirect URI `http://localhost:5000/auth/google/callback`, and copy the generated client ID and secret. If left empty, Google sign-in fails but the email/password fallback still works.

- `JWT_SIGNING_KEY` — Any cryptographically random 32-byte string. Generate one with:

  ```bash
  openssl rand -base64 32
  ```

  In production this value is sourced from AWS Secrets Manager and rotated on a schedule documented in [`operations.md`](operations.md).

The remaining keys in `backend/.env` (`DATABASE_URL`, `OTLP_EXPORTER_ENDPOINT`, `LOG_LEVEL`, etc.) have safe development defaults and do not need to be edited for the First Run.

### Step 4 — Populate frontend variables

Edit `frontend/.env` and verify:

```bash
VITE_API_BASE_URL=http://localhost:5000
```

This is the default. Override it only if you are pointing the SPA at a non-local backend.

### Step 5 — Bring up the local stack

```bash
docker compose up --build
```

This command builds and starts three containers in the foreground:

- `postgres` — PostgreSQL 17 on host port 5432, with a healthcheck the backend waits on.
- `backend` — Flask 3.1.3 + Gunicorn on host port 5000. Alembic migrations are auto-applied during startup when `RUN_MIGRATIONS=true` (the default in `docker-compose.yml`).
- `frontend` — Vite-built React SPA served by nginx on host port 5173.

The first build takes 3–5 minutes for `npm install` and the backend image. Subsequent runs are sub-30 seconds because Docker layer caching kicks in.

If you prefer a detached stack, use `docker compose up --build -d` and tail logs with `docker compose logs -f backend frontend postgres`.

### Step 6 — Verify each service

In a second terminal:

```bash
# Backend liveness — returns 200 unconditionally
curl -i http://localhost:5000/healthz

# Backend readiness — returns 200 only after a DB SELECT 1 succeeds within 1 second
curl -i http://localhost:5000/readyz

# Prometheus metrics — returns text exposition format
curl -s http://localhost:5000/metrics | head -20
```

Then open `http://localhost:5173/` in a browser and confirm the Sales-Connections SPA login screen renders.

### Step 7 — Create your first user account

The first user account in a fresh org is automatically promoted to Admin role for bootstrap convenience; this avoids the chicken-and-egg problem where no one has permission to grant Admin. The auto-bootstrap rule is documented in [`security.md`](security.md).

Create the account in one of two ways:

- Click "Sign in with Google" if you populated the OAuth keys; the OAuth handshake creates the user automatically.
- Use the email/password form on the login page; submitting a fresh email address creates the account on the fly.

After login you land on the Connection Feed (empty until you add the first record). Try the "Add Connection" flow to verify the AI note generation and audit trail end-to-end.

### Step 8 — Tear down (when done)

```bash
docker compose down -v
```

The `-v` flag removes the Postgres data volume; omit it to retain database state across restarts.

## 3. Domain Context

This section grounds new contributors in WHAT the system does and WHY it exists. Per the Explainability rule, the rationale belongs in [`decision-log.md`](decision-log.md); the summary below is purely descriptive.

### Product purpose

Sales-Connections turns passive network knowledge into actionable sales leads. Contributors log curated "Connection Idea" records about real people in their networks; the system uses Anthropic Claude to draft outreach talking points; sales reps prioritize and execute against those records. The platform is single-tenant in MVP runtime but multi-tenant at the data layer (every entity carries an `org_id` foreign key), so adding multi-organization onboarding is a future increment rather than a rewrite.

### The fourteen features

The MVP scope is exhaustively the fourteen features listed below. Feature IDs are stable and are referenced from the technical specification, the decision log, and pull request descriptions. Implementation details for each feature are in [`architecture.md`](architecture.md) and [`api.md`](api.md).

| ID | Name | Priority | One-Sentence Description |
|----|------|----------|--------------------------|
| F-001 | Connection Idea Form | Critical | Structured 9-field web form for capturing connection records (name, LinkedIn, company, title, context, AI notes, involvement, owner, date). |
| F-002 | AI Note Generation | Critical | Anthropic Claude integration converting relationship context into editable outreach talking points within a 5-second P95 budget. |
| F-003 | Involvement Indicator | Critical | Required three-valued enum (Warm Intro / Soft Reference / Target Only) signaling outreach participation. |
| F-004 | Connection Feed and Dashboard | Critical | Filterable, sortable view across five filter dimensions, responsive at 10,000 records per organization. |
| F-005 | Outreach Status Tracking | Critical | Four-state status field (Not Started / In Progress / Contacted / Closed) gated to Sales Rep or Admin roles. |
| F-006 | Submitter / Owner Attribution | Critical | Permanent owner identity attached to every record via non-null foreign key plus denormalized display name. |
| F-007 | Record Editing and Soft Deletion | Critical | Edit-in-place semantics with `deleted_at` flag for soft delete; hard delete is Admin-only. |
| F-008 | Tagging and Categorization | High | Many-to-many tag relations supporting industry, use-case, and geography classification. |
| F-009 | Role-Based Access Control | High | Three-role authorization (Admin / Contributor / Viewer-Sales Rep) enforced at the API layer with sub-50 ms checks. |
| F-010 | Duplicate LinkedIn URL Detection | High | Non-blocking pre-submit warning when a normalized LinkedIn URL already exists in the org. |
| F-011 | Connection Detail View with Edit History | High | Full record view exposing all fields plus an edit-history feed sourced from the audit trail. |
| F-012 | User Authentication | Critical | OAuth 2.0 against Google with email/password fallback; server-validated PyJWT cookie completing within 2 seconds. |
| F-013 | Audit Trail | High | Append-only `audit_events` table with eight enum-constrained event types and sub-100 ms emission inside the parent transaction. |
| F-014 | Admin Panel | High | Administrative surface for user/role management, record moderation, and basic aggregation analytics. |

### The three user roles

The platform has three roles: `Admin`, `Contributor`, and `Viewer` (the functional name "Sales Rep" is interchangeable with `Viewer`). The role is encoded as a JWT claim and enforced in two places: the backend RBAC decorator (authoritative) and the frontend `RoleGate` component (UX courtesy). The permission matrix below is the canonical reference; the authoritative source is the implementation in `backend/app/middleware/rbac.py`.

| Action | Admin | Contributor | Viewer (Sales Rep) |
|--------|-------|-------------|--------------------|
| Create connection record | Yes | Yes | No |
| View feed (org-scoped) | Yes | Yes | Yes |
| Edit own record | Yes | Yes | No |
| Edit any record | Yes | No | No |
| Soft-delete own record | Yes | Yes | No |
| Hard-delete any record | Yes | No | No |
| Update outreach status | Yes | No | Yes |
| Manage users / roles | Yes | No | No |
| View admin analytics | Yes | No | No |
| Generate AI notes | Yes | Yes | No |
| Tag records | Yes | Yes | No |
| Run duplicate-check pre-submit | Yes | Yes | No |
| View edit history | Yes | Yes | Yes |

The status-mutation column reflects the user's stated rule that outreach progress belongs to the sales team and cannot be overwritten by the original submitter without Admin rights.

### The audit trail

Every state-changing operation in the platform emits a row in the `audit_events` table inside the same database transaction as the state change itself. The two writes are atomic: failure of either rolls back both. This invariant is the foundation of the platform's accountability story.

The eight event types are:

| Event Type | Emitted By | Payload Notes |
|------------|------------|---------------|
| `create` | `POST /api/connections` | Captures the full new-record payload in `after_payload`. |
| `status_change` | `PATCH /api/connections/:id/status` | Captures `before_status` and `after_status`. |
| `edit` | `PATCH /api/connections/:id` | Captures field-level diff in `before_payload` / `after_payload`. |
| `soft_delete` | `DELETE /api/connections/:id` | Captures the record state at deletion time. |
| `hard_delete` | Admin-only `DELETE /api/admin/records/:id` | Captures the record state at hard-delete time. |
| `role_change` | `PATCH /api/admin/users/:id` | Captures `before_role` and `after_role`. |
| `authentication` | `/auth/login`, `/auth/google/callback`, `/auth/logout` | Captures session-event metadata. |
| `admin_op` | Other Admin-only operations | Captures action description in `after_payload`. |

The append-only invariant is enforced at the database privilege layer: the application database role has only `INSERT` privilege on `audit_events`; `UPDATE` and `DELETE` are revoked. Even SQL injection cannot mutate audit history.

### Multi-tenancy posture

The platform is single-tenant in MVP runtime but multi-tenant at the data layer. Every entity (records, users, tags, audit events) carries an `org_id` foreign key. Every read and every write injects `WHERE org_id = g.session.org_id`; cross-org access returns 403 from the API. The user interface for organization onboarding, the org-switcher, and per-org branding are deferred to a future increment (see [Suggested Next Tasks](#9-suggested-next-tasks)).

### The three primary user flows

The three primary flows are the user's verbatim descriptions, mapped here to actual code paths so a contributor can trace each step from UI to database.

**Flow 1 — Add a Connection Idea (Contributor):**
"Click 'Add Connection' → Fill in name, LinkedIn, company, title → Enter relationship context → Trigger AI note generation → Review/edit AI notes → Select involvement level → Tag and submit → Record appears in team feed."

Maps to: `frontend/src/features/connections/AddEditConnectionForm.tsx` issues `POST /api/notes/generate` (optional, non-blocking) followed by `POST /api/connections`. The backend wires through `backend/app/api/connections.py` to `backend/app/services/connections.py:create_record`, which opens a transaction, persists the record (deriving owner from `g.session`), and emits an `audit_events.event_type = create` row in the same transaction.

**Flow 2 — Browse and Claim a Lead (Sales Rep):**
"Open connection list → Filter by involvement type or industry tag → Select a record → Review AI notes and submitter context → Update outreach status to 'In Progress' → Work the lead."

Maps to: `frontend/src/features/connections/ConnectionFeed.tsx` issues `GET /api/connections?involvement=...&tag_ids=...` driving a TanStack Query cache; click navigates to `frontend/src/features/connections/ConnectionDetail.tsx`; the status chip mounts a `PATCH /api/connections/:id/status` mutation gated to `Viewer` and `Admin` roles. The status change emits an `audit_events.event_type = status_change` row capturing the before / after status.

**Flow 3 — Manage the Platform (Admin):**
"View all submissions → Edit or remove records → Manage users and roles → View activity across contributors."

Maps to: `frontend/src/features/admin/AdminPanel.tsx` is a tabbed surface routing to `UserManagement.tsx`, `RecordModeration.tsx`, and `Analytics.tsx`. All admin endpoints under `/api/admin/*` are gated by `@requires_role('Admin')`. Hard delete (`DELETE /api/admin/records/:id`) emits `event_type = hard_delete`; role mutation emits `event_type = role_change`.

## 4. Repository Layout

The repository is organized by tier. Each tier owns its dependency manifests and its own Dockerfile; the only contract between tiers is the REST/JSON envelope documented in [`api.md`](api.md).

```text
sales-connections/
|-- backend/                Python 3.12 / Flask 3.1.3 / SQLAlchemy 2.x / pydantic 2.x application
|   |-- app/
|   |   |-- __init__.py     Flask app factory create_app()
|   |   |-- config.py       Configuration classes (Development / Testing / Production)
|   |   |-- extensions.py   db, oauth, structlog logger as module-level singletons
|   |   |-- models/         SQLAlchemy declarative models (organization, user, record, tag, audit_event, enums)
|   |   |-- schemas/        pydantic schemas (connection, note_generation, admin, auth)
|   |   |-- api/            Flask blueprints (auth, connections, notes, tags, admin, health)
|   |   |-- services/       Business logic (connections, ai_orchestration, auth, duplicate_detection, audit, admin)
|   |   |-- middleware/     Cross-cutting (auth, rbac, correlation, error_handlers)
|   |   |-- utils/          Helpers (url normalization, sanitization)
|   |   `-- observability/  structlog config, prometheus metrics, OpenTelemetry tracing
|   |-- migrations/         Alembic migrations (env.py, script.py.mako, versions/*.py)
|   |-- tests/              Pytest suite with conftest fixtures and factory-boy factories
|   |-- pyproject.toml      PEP 621 metadata, ruff and mypy configuration
|   |-- requirements.txt    Pinned production dependencies
|   |-- requirements-dev.txt Pinned development dependencies
|   |-- Dockerfile          Multi-stage Python 3.12-slim image
|   |-- wsgi.py             Gunicorn entrypoint exposing app = create_app()
|   `-- .env.example        Documented backend environment variables
|-- frontend/               React 19.2.5 / TypeScript 5.x / Vite SPA
|   |-- src/
|   |   |-- main.tsx        React 19 root mount
|   |   |-- App.tsx         Top-level layout shell
|   |   |-- router.tsx      react-router-dom 6.x route table
|   |   |-- api/            Fetch wrapper and TanStack Query hooks
|   |   |-- auth/           AuthProvider, ProtectedRoute, RoleGate
|   |   |-- features/       Feature components (connections, auth, admin)
|   |   |-- components/ui/  Tailwind-styled primitives (Button, Input, Select, Table, Badge, Modal, Toast)
|   |   |-- schemas/        Zod schemas mirroring backend pydantic
|   |   |-- lib/            queryClient, correlationId
|   |   `-- styles/         Tailwind directives and global CSS
|   |-- tests/              Vitest + Testing Library tests
|   |-- package.json        Pinned npm manifest
|   |-- package-lock.json   Lockfile
|   |-- vite.config.ts      Vite build configuration
|   |-- tailwind.config.ts  Tailwind theme configuration
|   |-- tsconfig.json       TypeScript compiler configuration
|   |-- Dockerfile          Multi-stage nginx image
|   |-- nginx.conf          SPA fallback and gzip configuration
|   `-- .env.example        Documented frontend environment variables
|-- infra/
|   `-- terraform/          Terraform IaC for AWS (modules + per-environment compositions)
|       |-- modules/
|       |   |-- network/    VPC, subnets, security groups
|       |   |-- database/   RDS Multi-AZ, parameter group, subnet group
|       |   |-- ecs/        ECS cluster, Fargate task definition, service
|       |   |-- ecr/        ECR repositories with native scanner
|       |   |-- alb/        ALB, listener, target group, ACM certificate
|       |   |-- secrets/    Secrets Manager entries
|       |   `-- observability/ CloudWatch log groups, alarms, dashboard
|       `-- envs/
|           |-- dev/        Dev workspace composition
|           |-- staging/    Staging workspace composition
|           `-- prod/       Prod workspace composition
|-- .github/
|   |-- workflows/          ci.yml, cd.yml, dependabot.yml
|   |-- CODEOWNERS          Path-based reviewer assignments
|   `-- pull_request_template.md  PR checklist (decision-log, diagram, observability, deck)
|-- docs/
|   |-- README.md           This file family is documented here
|   |-- onboarding.md       This guide
|   |-- architecture.md     Detailed architecture with Mermaid diagrams
|   |-- api.md              REST endpoint catalog
|   |-- operations.md       Runbook (deploy, rollback, secret rotation, dashboards)
|   |-- security.md         Auth, RBAC, audit, secrets, data scoping
|   |-- decision-log.md     Master decision log per Explainability rule
|   `-- diagrams/           Mermaid .mmd sources referenced from architecture.md
|-- blitzy-deck/            Self-contained reveal.js executive summary deck
|-- docker-compose.yml      Local development stack (postgres + backend + frontend)
|-- .gitignore              Polyglot ignore patterns
`-- README.md               Top-level project overview and Quick Start
```

The dependency direction is strictly inward. Frontend depends only on the REST contract; backend depends only on PostgreSQL and outbound HTTPS to Anthropic and Google; infrastructure depends on nothing application-level. There is no shared code between frontend and backend; the equivalent runtime contract (Zod on the client, pydantic on the server) is hand-maintained side-by-side.

## 5. Common Development Tasks

The recipes below cover the most common changes a contributor will make. Each recipe is a complete checklist; do not skip steps.

### Recipe — Add a new REST endpoint

1. Define the pydantic request and response schemas in `backend/app/schemas/<feature>.py`. Reject any client-supplied fields that should come from the session (for example `owner_user_id`, `org_id`).
2. Add the route to the appropriate blueprint at `backend/app/api/<feature>.py`. Keep the handler thin: parse input, call a service, format the response.
3. Implement the business logic in `backend/app/services/<feature>.py`. State changes open an explicit transaction with `with db.session.begin():` and emit an audit event before commit.
4. Apply RBAC via the `@requires_role(...)` decorator from `backend/app/middleware/rbac.py`. Be specific: list every role that may invoke the endpoint.
5. Emit an audit event inside the same transaction via `backend/app/services/audit.py:emit_audit_event`. The event type must come from the `AuditEventType` enum.
6. Add tests in `backend/tests/api/test_<feature>.py` (handler-level) and `backend/tests/services/test_<feature>.py` (service-level). Cover happy path, validation rejection, RBAC denial, and audit-event emission.
7. If the endpoint introduces a non-trivial design choice, append a row to [`decision-log.md`](decision-log.md) with the four required columns: decision, alternatives, rationale, risks.
8. Update [`api.md`](api.md) with the endpoint signature, RBAC requirement, request schema, response schema, and audit-event emission note.

### Recipe — Add a new SQLAlchemy column

1. Update the model in `backend/app/models/<entity>.py`. Add the column with appropriate `nullable`, `default`, and `index` flags.
2. Generate a migration:

   ```bash
   docker compose exec backend alembic revision --autogenerate -m "add <column> to <table>"
   ```

3. Review the generated migration file under `backend/migrations/versions/`. Never accept the auto-generated SQL blindly; verify column names, defaults, and any constraint generation.
4. Test the migration both ways:

   ```bash
   docker compose exec backend alembic upgrade head
   docker compose exec backend alembic downgrade -1
   docker compose exec backend alembic upgrade head
   ```

5. Update affected pydantic schemas in `backend/app/schemas/` to expose the new field.
6. Update affected Zod schemas in `frontend/src/schemas/` to mirror the backend.
7. Update affected React components and any TanStack Query hooks that fetch the new field.
8. Append a row to [`decision-log.md`](decision-log.md) justifying the column addition.

Migrations are forward-compatible by policy. Never drop a column destructively in MVP; if a field is being retired, mark it deprecated in code, ship the deprecation, and drop in a subsequent migration after a grace period.

### Recipe — Add a new React route

1. Add the route to `frontend/src/router.tsx`. Wrap with `<ProtectedRoute>` for authenticated-only routes and `<RoleGate role="Admin">` for admin-only routes.
2. Create the feature component under `frontend/src/features/<feature>/<Component>.tsx`. Compose UI primitives from `frontend/src/components/ui/`; do not duplicate Tailwind classes across feature components.
3. Add a TanStack Query hook in `frontend/src/api/<feature>.ts` for any backend interaction. Do not call `fetch()` directly; always go through `frontend/src/api/client.ts`.
4. Add tests in `frontend/tests/features/<feature>/<Component>.test.tsx` using `@testing-library/react` and `msw` for HTTP mocks.

### Recipe — Update a dependency

1. Pin the new version exactly in `backend/requirements.txt` or `frontend/package.json`. No `^`, no `~`, no `latest` ranges.
2. Recompile or reinstall:

   ```bash
   # Backend
   cd backend
   pip install -r requirements.txt
   # Frontend
   cd frontend
   npm install
   ```

3. Run the full test suite locally and confirm CI passes (lint, type-check, tests, build).
4. Append a row to [`decision-log.md`](decision-log.md) justifying the upgrade. Reference the upstream release notes and any CVE addressed.

### Recipe — Run the full test suite locally

```bash
# Backend tests with coverage threshold
docker compose exec backend pytest --cov=app --cov-report=term-missing --cov-fail-under=85

# Frontend tests with coverage
docker compose exec frontend npm test -- --coverage
```

Backend coverage is enforced at 85 percent in CI via `--cov-fail-under=85`; frontend coverage is enforced at the same threshold via the Vitest configuration.

### Recipe — Lint, format, type-check

```bash
# Backend
docker compose exec backend ruff check .
docker compose exec backend ruff format --check .
docker compose exec backend mypy app

# Frontend
docker compose exec frontend npm run lint
docker compose exec frontend npm run format:check
docker compose exec frontend npm run type-check
```

Every command above runs in CI on every pull request. Local execution is the recommended pre-push step.


| Edit own record | Yes | Yes | No |
| Edit any record | Yes | No | No |
| Soft-delete own record | Yes | Yes | No |
| Hard-delete any record | Yes | No | No |
| Update outreach status | Yes | No | Yes |
| Manage users / roles | Yes | No | No |
| View admin analytics | Yes | No | No |
| Generate AI notes | Yes | Yes | No |
| Tag records | Yes | Yes | No |
| Run duplicate-check pre-submit | Yes | Yes | No |
| View edit history | Yes | Yes | Yes |

The status-mutation column reflects the user's stated rule that outreach progress belongs to the sales team and cannot be overwritten by the original submitter without Admin rights.

### The audit trail

Every state-changing operation in the platform emits a row in the `audit_events` table inside the same database transaction as the state change itself. The two writes are atomic: failure of either rolls back both. This invariant is the foundation of the platform's accountability story.

The eight event types are:

| Event Type | Emitted By | Payload Notes |
|------------|------------|---------------|
| `create` | `POST /api/connections` | Captures the full new-record payload in `after_payload`. |
| `status_change` | `PATCH /api/connections/:id/status` | Captures `before_status` and `after_status`. |
| `edit` | `PATCH /api/connections/:id` | Captures field-level diff in `before_payload` / `after_payload`. |
| `soft_delete` | `DELETE /api/connections/:id` | Captures the record state at deletion time. |
| `hard_delete` | Admin-only `DELETE /api/admin/records/:id` | Captures the record state at hard-delete time. |
| `role_change` | `PATCH /api/admin/users/:id` | Captures `before_role` and `after_role`. |
| `authentication` | `/auth/login`, `/auth/google/callback`, `/auth/logout` | Captures session-event metadata. |
| `admin_op` | Other Admin-only operations | Captures action description in `after_payload`. |

The append-only invariant is enforced at the database privilege layer: the application database role has only `INSERT` privilege on `audit_events`; `UPDATE` and `DELETE` are revoked. Even SQL injection cannot mutate audit history.

### Multi-tenancy posture

The platform is single-tenant in MVP runtime but multi-tenant at the data layer. Every entity (records, users, tags, audit events) carries an `org_id` foreign key. Every read and every write injects `WHERE org_id = g.session.org_id`; cross-org access returns 403 from the API. The user interface for organization onboarding, the org-switcher, and per-org branding are deferred to a future increment (see [Suggested Next Tasks](#9-suggested-next-tasks)).

### The three primary user flows

The three primary flows are the user's verbatim descriptions, mapped here to actual code paths so a contributor can trace each step from UI to database.

**Flow 1 — Add a Connection Idea (Contributor):**
"Click 'Add Connection' → Fill in name, LinkedIn, company, title → Enter relationship context → Trigger AI note generation → Review/edit AI notes → Select involvement level → Tag and submit → Record appears in team feed."

Maps to: `frontend/src/features/connections/AddEditConnectionForm.tsx` issues `POST /api/notes/generate` (optional, non-blocking) followed by `POST /api/connections`. The backend wires through `backend/app/api/connections.py` to `backend/app/services/connections.py:create_record`, which opens a transaction, persists the record (deriving owner from `g.session`), and emits an `audit_events.event_type = create` row in the same transaction.

**Flow 2 — Browse and Claim a Lead (Sales Rep):**
"Open connection list → Filter by involvement type or industry tag → Select a record → Review AI notes and submitter context → Update outreach status to 'In Progress' → Work the lead."

Maps to: `frontend/src/features/connections/ConnectionFeed.tsx` issues `GET /api/connections?involvement=...&tag_ids=...` driving a TanStack Query cache; click navigates to `frontend/src/features/connections/ConnectionDetail.tsx`; the status chip mounts a `PATCH /api/connections/:id/status` mutation gated to `Viewer` and `Admin` roles. The status change emits an `audit_events.event_type = status_change` row capturing the before / after status.

**Flow 3 — Manage the Platform (Admin):**
"View all submissions → Edit or remove records → Manage users and roles → View activity across contributors."

Maps to: `frontend/src/features/admin/AdminPanel.tsx` is a tabbed surface routing to `UserManagement.tsx`, `RecordModeration.tsx`, and `Analytics.tsx`. All admin endpoints under `/api/admin/*` are gated by `@requires_role('Admin')`. Hard delete (`DELETE /api/admin/records/:id`) emits `event_type = hard_delete`; role mutation emits `event_type = role_change`.

## 6. Common Pitfalls

The list below catalogs the small papercuts that have already cost time during development. Read it before your first contribution; each item is a concrete violation of an architectural invariant or a habitual oversight that the test suite does not always catch on the first run.

### Empty `ANTHROPIC_API_KEY`

AI note generation will return HTTP 504 with `error.code = "ai_timeout"`. The form still submits per the non-blocking AI failure design; the SPA renders an "AI unavailable; you can still submit" affordance. This is intentional behaviour, not a bug. Set `ANTHROPIC_API_KEY` in `backend/.env` to enable AI.

### Skipping `alembic upgrade head`

If you start the backend outside Docker Compose (which auto-runs migrations via `RUN_MIGRATIONS=true`), the database may be empty or stale. Symptom: `/healthz` returns 200, but the first API call 500s with a `relation "records" does not exist` or similar error. Fix:

```bash
docker compose exec backend alembic upgrade head
```

### Importing the Anthropic SDK outside `services/ai_orchestration.py`

The provider-replaceability invariant (the user wrote "Anthropic Claude API or equivalent") requires that `import anthropic` appears in exactly one file: `backend/app/services/ai_orchestration.py`. If you need the AI in another module, call the service function rather than the SDK. The test suite asserts this invariant by scanning imports across the package.

### Calling `fetch()` directly in frontend code

Every HTTP call from the SPA must go through `frontend/src/api/client.ts`. The wrapper is the single place that attaches the correlation-ID header, intercepts 401 responses with a redirect to `/login`, and parses the uniform error envelope. Direct `fetch()` invocations bypass these uniformities and produce inconsistent UX on auth failures and observability gaps in tracing.

### Direct `UPDATE` or `DELETE` against `audit_events`

The application database role lacks these privileges. Even successful SQL injection cannot mutate audit history. If you find yourself wanting to "fix" an audit row, you are doing something wrong; emit a compensating audit event instead. The privilege grants are configured in the initial migration and reproduced in `infra/terraform/modules/database/main.tf` for managed environments.

### Hardcoding `org_id` in queries or payloads

Every read and write must derive `org_id` from `g.session.org_id`, which is bound to the request context by `backend/app/middleware/auth.py` after JWT verification. Client-supplied `org_id` is rejected by the pydantic schema layer; bypassing the middleware (for example, in a background job) requires the equivalent session injection.

### Hardcoding `owner_user_id` in payloads

Mirror of the above for record ownership. The pydantic `ConnectionCreate` schema explicitly excludes `owner_user_id` and `owner_display_name` from its accepted fields; the service layer derives both from `g.session.user_id` and the cached display name. Owner identity is permanent and never client-controlled.

### Forgetting to emit audit events

Every state-changing endpoint must emit an audit event inside the same transaction as the state change. The pytest suite has a fixture `assert_audit_emitted` that scans `audit_events` after each state-mutation test; if your new endpoint forgets to call `emit_audit_event`, the test will fail. The PR template also includes a checklist item asking the author to confirm audit emission.

### Forgetting soft-delete-aware queries

Default reads on `records` must include `WHERE deleted_at IS NULL`. The base query helper in `backend/app/services/connections.py` injects this clause automatically; bypass it only in the Admin record-moderation view, which is the single legitimate consumer of soft-deleted records.

### Using `latest`, `^`, or `~` in dependency manifests

The pinning policy is exact versions everywhere. Dependabot raises pull requests for upgrades, which a human reviews and merges. The CI lint job does not currently scan for range specifiers; it is a code-review responsibility.

### Skipping the decision-log row

The Explainability rule (AAP §0.7.5) makes a decision-log entry mandatory for every non-trivial choice. The PR template enforces this via a checklist item; reviewers will block merges that introduce architectural decisions without a corresponding row in [`decision-log.md`](decision-log.md).

### Embedding rationale in code comments

Per the Explainability rule, "Rationale must not be embedded in code comments; the decision log is the single source of truth for 'why' decisions." Code comments describe WHAT the code does and HOW it does it; rationale belongs in [`decision-log.md`](decision-log.md). A comment that says "we use Flask because it is simpler than Django" is a mis-located decision-log entry.

### Forgetting to copy `.env.example` to `.env`

The `.env` file is git-ignored; the `.env.example` template is committed. Always run the copy step before editing. If the backend container starts with `OSError: ANTHROPIC_API_KEY not set` and you have written values to `.env.example`, you have edited the wrong file.

### Bypassing the API-layer authorization in the frontend

The `RoleGate` component in the frontend is a UX courtesy, not a security boundary. The backend's `@requires_role(...)` decorator is the authoritative gate. Never rely on the absence of a UI control to prevent an action; always assume the API may be called directly with curl.

### Bypassing pydantic in favour of Zod

Zod runs in the browser and protects the user from typos; pydantic runs on the server and protects the database from bad input. The backend is authoritative; client validation is a UX optimization. A payload that passes Zod but fails pydantic is a correctness bug, but a payload that fails Zod but passes pydantic is a UX bug.

## 7. Observability Verification

The Observability rule (AAP §0.7.5) states that the application is not complete until it is observable. The five verification steps below confirm the observability surface end-to-end in the local Docker Compose environment. Run all five before merging any change that introduces a new code path.

### Structured logs

```bash
docker compose logs -f backend
```

Each log line must be valid JSON containing at least the keys `timestamp`, `level`, `correlation_id`, and `event`. Authenticated requests additionally include `user_id` and `org_id`. Pipe through `jq` for pretty output:

```bash
docker compose logs -f backend | jq -c 'select(.level == "info")'
```

### Correlation propagation

```bash
curl -H "X-Correlation-Id: test-12345" http://localhost:5000/api/connections
docker compose logs backend | grep test-12345
```

Every log line emitted while handling that request must carry the same `correlation_id` value. A request without an inbound `X-Correlation-Id` header is given a fresh `uuid4()` by the correlation middleware.

### Metrics endpoint

```bash
curl -s http://localhost:5000/metrics | head -40
```

The Prometheus exposition output must contain at minimum:

- Counter `http_requests_total{method,route,status}` — incremented on every response.
- Histogram `http_request_duration_seconds{method,route}` — observed on every response.
- Histogram `ai_request_duration_seconds` — observed on every Anthropic call.
- Counter `audit_events_emitted_total{event_type}` — incremented on every audit event.
- Gauge `active_sessions` — current number of valid session JWTs.

Counters are lazy: they are not exposed until at least one observation has been recorded. If a counter is missing, trigger a sample request first.

### Distributed traces

The default development stack does not start a tracing collector. To enable end-to-end tracing locally:

```bash
docker compose --profile tracing up
```

This adds a Jaeger all-in-one container at `http://localhost:16686/`. Confirm that backend traces cover Flask handler spans, SQLAlchemy query spans, and outbound HTTP spans (for Anthropic and Google OAuth).

### Health checks

```bash
curl -i http://localhost:5000/healthz   # returns 200 unconditionally
curl -i http://localhost:5000/readyz    # returns 200 only if SELECT 1 succeeds within 1 second
```

`/healthz` is the liveness probe (used by ECS task health and `docker compose` healthchecks). `/readyz` is the readiness probe (used by ALB target-group health checks; an ECS task is removed from the ALB target group on `/readyz` failure but is not killed).

### Dashboard template

The CloudWatch dashboard template lives in `infra/terraform/modules/observability/main.tf`. It includes panels for AI P95 latency, RBAC P50, audit-emission P50, request rate by status, and error budget burn-down. The dashboard is provisioned per-environment by the Terraform composition under `infra/terraform/envs/{dev,staging,prod}/`. See [`operations.md`](operations.md) for the full dashboard reference.

## 8. Extending the Project

This section is the contributor's map for adding new behaviour to the platform. Read [Architectural invariants](#architectural-invariants-you-must-preserve) before changing anything; read the [Decision tree](#where-to-add-a-new-feature) to know where the new code lives; read the [PR workflow](#the-pr-workflow) for what to attach to your pull request.

### Architectural invariants you must preserve

The following eight invariants are non-negotiable. Every code path must respect them; tests assert them; pull requests that violate them will be sent back for revision.

1. **Stateless backend workers.** No in-process caches, no in-process session state, no shared mutable globals beyond the SQLAlchemy engine and connection pool. Any worker can be terminated and replaced without data loss.
2. **Single-page application delivery.** All HTML returned by the frontend is the single Vite-bundled `index.html`. Server-rendered fragments are not introduced.
3. **Org-scoped multi-tenancy at the data model layer.** Every read and every write injects `WHERE org_id = g.session.org_id`. Cross-org access returns 403 from the API and 404 from the frontend.
4. **Soft-delete-aware queries by default.** Every read on `records` injects `WHERE deleted_at IS NULL`. Admin-only soft-deleted views must explicitly opt out of this filter.
5. **Append-only audit table.** No code path issues `UPDATE` or `DELETE` against `audit_events`. Database-level grants enforce this in production.
6. **Atomic state-change + audit pair.** Every state change persists itself and emits its audit event inside a single transaction; failure of either rolls back both.
7. **API-layer authorization is authoritative.** The frontend's `RoleGate` is a UX courtesy; the backend RBAC decorator is the only authoritative gate. Never rely on the absence of a UI control to prevent an action.
8. **Server-side validation is authoritative.** Every payload is re-validated by pydantic on the server, regardless of Zod success on the client.

### Where to add a new feature

Use the decision tree below. Each leaf is the canonical location for a category of change.

| Question | Location |
|----------|----------|
| Is it a new HTTP endpoint? | `backend/app/api/<feature>.py` (handler) + `backend/app/services/<feature>.py` (logic) |
| Is it shared business logic? | `backend/app/services/<feature>.py` |
| Is it a new database column? | `backend/app/models/<entity>.py` + `backend/migrations/versions/<n>_<description>.py` |
| Is it a new pydantic schema? | `backend/app/schemas/<feature>.py` |
| Is it a new SPA route? | `frontend/src/router.tsx` + `frontend/src/features/<feature>/` |
| Is it a reusable UI primitive? | `frontend/src/components/ui/<Component>.tsx` |
| Is it a Zod validator? | `frontend/src/schemas/<feature>.ts` |
| Is it a TanStack Query hook? | `frontend/src/api/<feature>.ts` |
| Is it a cross-cutting concern (auth, RBAC, correlation, errors)? | `backend/app/middleware/<concern>.py` |
| Is it observability-related (log field, metric, span)? | `backend/app/observability/{logging,metrics,tracing}.py` |
| Is it infrastructure (network, RDS, ECS, ALB, secret)? | `infra/terraform/modules/<module>/main.tf` |
| Is it a CI or CD step? | `.github/workflows/<workflow>.yml` |
| Is it documentation? | `docs/<topic>.md` (and update [`See Also`](#12-see-also) if a new file) |

### The PR workflow

The pull request template at `.github/pull_request_template.md` is the canonical contributor checklist. The salient items are:

- **Decision log row.** For every non-trivial choice, append a row to [`decision-log.md`](decision-log.md) with the four required columns: decision, alternatives considered, rationale, risks.
- **Mermaid diagram update.** If the architecture changed, update the relevant `.mmd` file under `docs/diagrams/` and any inline references in [`architecture.md`](architecture.md).
- **Observability assertion.** New code path? Add a structured log field, a metric, or a trace span. New error class? Confirm it maps cleanly to the error envelope.
- **Onboarding doc update.** Did the developer experience change? Update this file or the README.
- **Test coverage maintained.** Both backend and frontend coverage must remain at or above 85 percent.
- **Audit-event emission asserted.** Every new state-changing endpoint must emit an audit event; the test suite asserts this with `assert_audit_emitted`.
- **No emoji.** This rule applies to source, documentation, and the executive deck.
- **No fenced code blocks in the deck.** This rule applies only to `blitzy-deck/index.html` per the Executive Presentation rule.


## 9. Suggested Next Tasks

The following backlog items were surfaced during MVP scoping and deliberately deferred. Each is a real candidate for the next development phase; the sketch beside each item is a starting point, not a complete design. New items added to this list during a contribution should follow the same pattern: name, problem statement, sketch.

### Multi-organization runtime

The data model already carries `org_id` on every entity; the runtime is single-org. Build the org-onboarding wizard, the org-switcher in the SPA chrome, per-org branding (logo, theme color, custom domain), per-org admin invitation flow, and cross-org data isolation tests. Implementation depends on adding an organization-creation endpoint, scoping JWT claims to admit org switches without re-authentication, and extending the role model to include cross-org admin (Blitzy administrator role).

### Slack notifications

When a record is added with involvement type `Warm Intro`, post a notification to a configured Slack channel. Implementation: a Slack Webhook URL stored per-org in a new `org_settings` table, a small post-commit handler in `backend/app/services/connections.py`, and an opt-in toggle in the Admin Panel.

### CSV import / export

Bulk upload existing networks via a CSV file conforming to the nine-field record schema. Export filtered feed views as CSV. Implementation: a streaming CSV parser/serializer, a chunked import endpoint with per-row error reporting, and an export endpoint that respects the same filters as the feed.

### LinkedIn URL profile preview

Optional enrichment via the LinkedIn API (subject to LinkedIn TOS) or a third-party provider. Render profile photo, current title, and current company on each feed card. Implementation: a per-record cached enrichment payload, a background refresh job, and a clear opt-in flow because LinkedIn data is sensitive.

### CRM bidirectional sync

Salesforce / HubSpot integration with conflict-resolution rules. Implementation: a sync adapter per CRM, a cursor-based reconciliation loop, and a conflict UI for human resolution. Bidirectional means changes in either system propagate; a one-way export is a simpler interim.

### Calendar / inbox integration

Auto-log outreach activity from Gmail / Outlook by matching the connected person's email address to the record. Implementation: OAuth scope expansion to include email read access, a server-side matcher, and a privacy review gate (because email content is far more sensitive than the data already stored).

### Lead velocity and conversion analytics

Beyond the basic three admin panels: time-to-close histograms per involvement type, conversion rate by tag, contributor leaderboard with monthly cohort overlays. Implementation: pre-aggregated rollup tables refreshed on a schedule, plus dashboard panels in the Admin surface.

### Mobile PWA shell

Offline form drafting via a service worker and IndexedDB queue. Implementation: register a service worker scoped to `/feed` and `/connections/new`, queue submissions during offline windows, and reconcile on reconnect. Native iOS and Android remain out of scope; the PWA is the supported mobile path.

### Per-user notification preferences

Email or in-app notifications when an action affects a record the user owns or has claimed. Implementation: a notifications table, a per-user preferences UI, and a daily digest job.

### API rate limiting

Per-user and per-IP throttling at the ALB or in a Flask middleware. Implementation: leaky-bucket enforcement keyed by `user_id` or `X-Forwarded-For`, with rate-limit headers in the response. Out of MVP because the platform is single-org and the user count is bounded.

### OpenAPI tooling

Auto-generated `openapi.yaml` from Flask + pydantic, served through a Swagger UI page. Implementation: integrate `apispec` or a similar tool, regenerate the spec at build time, and host the UI behind admin RBAC.

### Server-side cache

Redis or ElastiCache for hot feed pages and tag autocomplete. Implementation: cache the top N feed pages per org with a 60-second TTL, invalidate on record state change. Out of MVP because the client-side TanStack Query cache absorbs the typical user pattern.

### RDS read replicas

Once feed reads dominate writes at a scale beyond MVP. Implementation: a read-only SQLAlchemy engine bound to the replica DSN, route GET handlers to the replica engine, retain writes on the primary engine. Out of MVP because the 10K-record ceiling is comfortable on the writer alone.

### Table partitioning

Once a single org exceeds 100K records, partition `records` and `audit_events` by `org_id` or by `event_timestamp`. Implementation: an Alembic migration creating partitioned parent tables and migrating data in chunks during a maintenance window.

### WebAuthn / passkeys

Replace the password fallback with WebAuthn. Implementation: a credential-registration flow during signup, a credential-store table, and a passkey-verification flow during login. Out of MVP because OAuth covers most users; the password fallback is a small surface.

### API versioning scheme

Introduce a `/api/v1/` prefix when a breaking change is needed. Implementation: a Flask blueprint factory keyed by version, route the SPA to the matching version. Out of MVP because the SPA and API ship together; no consumer outside the SPA exists yet.

## 10. Troubleshooting

The table below catalogs the failures most often hit by new contributors. Symptom is what you observe; cause is the most common explanation; fix is the first thing to try. If the listed fix does not resolve the issue, open an issue tagged `troubleshooting` so this table can be extended.

| Symptom | Likely Cause | Fix |
|---------|--------------|-----|
| `docker compose up` fails with "port 5432 already allocated" | Local PostgreSQL is running on the host on port 5432 | Stop the host PostgreSQL (`brew services stop postgresql` or `sudo systemctl stop postgresql`) or change the host port mapping in `docker-compose.yml`. |
| `docker compose up` fails with "port 5000 already allocated" | Another service is using port 5000 (often macOS AirPlay) | Disable AirPlay receiver in System Settings or change the host port mapping in `docker-compose.yml`. |
| `/healthz` returns 200 but `/readyz` returns 503 | Backend started before PostgreSQL became healthy | Wait 10 seconds and retry; if persistent, `docker compose down && docker compose up`. |
| Frontend shows network error on every API call | `VITE_API_BASE_URL` mismatch between `.env` and the running backend | Verify `frontend/.env` contains `VITE_API_BASE_URL=http://localhost:5000`. Restart the frontend container after editing `.env`. |
| OAuth redirect fails with "redirect_uri_mismatch" | Google OAuth client misconfigured | Add `http://localhost:5000/auth/google/callback` to the authorized redirect URIs in the Google Cloud Console. |
| `pytest` fails with `ImportError: app` | Backend venv not activated, or `PYTHONPATH` missing | Run from `backend/` inside the container (`docker compose exec backend pytest`) or activate the venv (`source backend/.venv/bin/activate`). |
| Tests pass locally but fail in CI | Environment drift between local and CI versions | Match CI-pinned versions: Python 3.12, Node 20 LTS. Re-run `pip install -r requirements.txt -r requirements-dev.txt` and `npm ci` to align lockfiles. |
| Prometheus metrics missing some counters | Counter only registers after first observation | Trigger the request type at least once; counters are lazy and not exposed until they have a value. |
| Alembic autogenerate produces empty migration | SQLAlchemy `metadata.create_all` not seeing the model | Ensure the model is imported in `backend/app/models/__init__.py`; `--autogenerate` only sees registered models. |
| `mypy` complains about untyped third-party | Missing type stubs | Add a typed shim under `backend/app/_shims/<package>.pyi` (advisory) or add the package to the mypy `[[tool.mypy.overrides]]` ignore list with justification in [`decision-log.md`](decision-log.md). |
| `ruff format --check` fails with formatting drift | Local editor saved with different settings | Run `ruff format .` to apply formatting. Configure your editor to format-on-save with the project ruff config. |
| Frontend build fails with "Cannot find module '@/...'" | Vite path alias not applied | Verify `frontend/vite.config.ts` declares the `@` alias and `frontend/tsconfig.json` lists the matching `paths` entry. Restart the Vite dev server after config changes. |
| `npm install` fails with EACCES on macOS | nvm-installed Node not on `PATH`, fallback to system Node | Run `nvm use 20` and re-run; ensure `.nvmrc` is honored by your shell. |
| Backend container restarts in a loop | `wsgi.py` import error or invalid `DATABASE_URL` | Tail logs (`docker compose logs backend`); the first traceback line is the cause. Common: `OperationalError: could not translate host name "postgres"` means the postgres container is not yet healthy. |
| AI note generation is slow (over 5 seconds) | Anthropic API contention or large prompt | The 5-second timeout is enforced; the response is HTTP 504 with `error.code = "ai_timeout"`. Retry; reduce the relationship-context length; check Anthropic status. |
| Login succeeds but every subsequent API call returns 401 | JWT cookie not being sent | Confirm `frontend/src/api/client.ts` sets `credentials: 'include'`; verify the backend `Set-Cookie` header includes `SameSite=Lax`; check the browser's DevTools Application tab for the `session` cookie. |
| Soft-deleted records appear in the feed | Query missing `WHERE deleted_at IS NULL` | Audit the SQLAlchemy query; the base query helper in `backend/app/services/connections.py` injects this clause automatically. Bypass it only in the Admin moderation view. |
| Audit row missing after a state change | Service did not call `emit_audit_event` inside the transaction | Add the call inside `with db.session.begin():` before the implicit commit. The pytest fixture `assert_audit_emitted` catches this in CI. |

## 11. Glossary

The terms below are domain-specific or project-specific. Use them consistently in code, comments, commit messages, and pull-request descriptions.

| Term | Definition |
|------|------------|
| AAP | Agent Action Plan — the master delivery brief for this release. Lives in the engagement record; not committed to the repository. |
| Audit event | A row in the `audit_events` table emitted by every state-changing operation in the same transaction as the state change. |
| Connection Idea | The user's term for a single record in the `records` table — a curated lead about a real person in the contributor's network. |
| Contributor | User role that can submit and edit own records but cannot mutate outreach status. |
| Hard delete | Actual SQL `DELETE` on a record; restricted to Admin role; emits `audit_events.event_type = hard_delete`. |
| Involvement indicator | The three-valued enum `Warm Intro` / `Soft Reference` / `Target Only` declared on every record. |
| Org | Organization — the multi-tenant boundary at the data layer. Every entity carries an `org_id` foreign key. |
| Outreach status | The four-valued enum `Not Started` / `In Progress` / `Contacted` / `Closed` tracked on every record. |
| RBAC | Role-Based Access Control via the `@requires_role(...)` decorator on Flask handlers and the `RoleGate` component in the SPA. The decorator is authoritative; the component is UX courtesy. |
| Sales Rep | The functional name for the `Viewer` role; has read access to the org-scoped feed plus the right to mutate outreach status. |
| Soft delete | Setting `records.deleted_at = NOW()` instead of issuing a SQL `DELETE`. The default state for non-Admin record removal. |
| Warm Intro | The strongest involvement category; the contributor is willing to make an introduction to the connection. |
| Soft Reference | Mid-strength involvement; the contributor is willing to vouch for the connection but not actively make the introduction. |
| Target Only | Weakest involvement; the contributor is naming a target the sales team should pursue without contributor participation. |
| Org-scoped | Pertaining to a query or operation that constrains its result to the current session's `org_id`. The default behaviour for every read and write. |
| Append-only | Pertaining to the `audit_events` table; only `INSERT` is permitted by the application database role. |
| Edit-in-place | The semantic for record updates: existing fields are mutated rather than producing a new record version. The audit trail captures the diff. |
| Bootstrap user | The first user account in a fresh org, automatically promoted to Admin role to break the chicken-and-egg permissioning problem. Documented in [`security.md`](security.md). |
| Correlation ID | The per-request UUID injected by `backend/app/middleware/correlation.py` into every log line, span, and outbound HTTP header. Threaded via the `X-Correlation-Id` header. |

## 12. See Also

The documentation set is small and tightly cross-referenced. Each document below is canonical for its topic; redundant explanations across documents are documentation defects to be reported via a pull request.

- [`README.md`](../README.md) — Top-level project overview and 5-minute Quick Start.
- [`architecture.md`](architecture.md) — Detailed component architecture with Mermaid diagrams.
- [`api.md`](api.md) — REST endpoint catalog with request and response shapes.
- [`operations.md`](operations.md) — Deploy, rollback, secret rotation, observability dashboards, and incident response.
- [`security.md`](security.md) — Authentication, RBAC, append-only audit, secrets, and data scoping.
- [`decision-log.md`](decision-log.md) — Master decision log per the Explainability rule. Every non-trivial decision lives here.
- [`diagrams/`](diagrams/) — Mermaid `.mmd` sources: system context, request lifecycle, ERD, and state diagrams for outreach status, record lifecycle, and authentication session.
- `../blitzy-deck/index.html` — Self-contained reveal.js executive summary deck for non-technical leadership.
- `../.github/pull_request_template.md` — The canonical PR checklist.

This file is a living document. Every pull request that surfaces a new pitfall, a new troubleshooting case, or a new extensibility pattern should append to the appropriate section here. Onboarding quality compounds: a small investment in documenting a stumble today saves the next contributor an hour tomorrow.

