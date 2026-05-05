# Operations Runbook

This runbook documents how to operate the Sales-Connections platform in production: deploys, rollbacks, secret rotation, observability dashboards, and incident response. The intended audience is on-call engineers and platform operators. For development workflows see [`onboarding.md`](onboarding.md); for architecture see [`architecture.md`](architecture.md); for security details see [`security.md`](security.md). Decision rationale lives in [`decision-log.md`](decision-log.md); this document is intentionally limited to reproducible procedures, observable signals, and incident playbooks.

The runbook is structured so that every command is copy-paste-ready, every step is idempotent, and every threshold maps to a corresponding alarm. When this document and the deployed infrastructure disagree, the deployed infrastructure is authoritative; treat the disagreement as a documentation defect and submit a pull request.

## Table of Contents

- [1. Environments](#1-environments)
- [2. Deploy Procedure](#2-deploy-procedure)
- [3. Rollback Procedure](#3-rollback-procedure)
- [4. Secret Rotation](#4-secret-rotation)
- [5. Observability](#5-observability)
- [6. Database Operations](#6-database-operations)
- [7. Incident Response](#7-incident-response)
- [8. Routine Tasks](#8-routine-tasks)
- [9. Capacity Planning](#9-capacity-planning)
- [10. See Also](#10-see-also)

## 1. Environments

The platform runs in three independent AWS environments. Each environment is a complete stack composed by a separate Terraform composition under `infra/terraform/envs/`; environments share no AWS resources. The `dev` and `staging` environments are configured to take continuous deployments on every merge to `main`; the `prod` environment is gated behind a manual approval step in the GitHub Actions workflow.

| Environment | URL | Backend Image | DB | Auto-deploy on Merge | Manual Approval |
|-------------|-----|---------------|----|----------------------|-----------------|
| `dev` | `https://dev.sales-connections.example.com` | `sha-<commit>` | RDS Multi-AZ (`dev`) | Yes (every push to main) | No |
| `staging` | `https://staging.sales-connections.example.com` | `sha-<commit>` | RDS Multi-AZ (`staging`) | Yes (every push to main) | No |
| `prod` | `https://sales-connections.example.com` | `sha-<commit>` | RDS Multi-AZ (`prod`) | No (manual gate) | Yes |

DNS records, the Application Load Balancer, the RDS instance, the ECS cluster, the ECR repositories, and Secrets Manager entries are all per-environment. Each environment composition file at `infra/terraform/envs/{dev,staging,prod}/main.tf` references the shared modules under `infra/terraform/modules/` with environment-specific variable values. Workspaces are not used; environments are isolated by directory and by separate Terraform state files.

The CloudWatch Log Groups follow the naming pattern `/sales-connections/{env}/backend` and `/sales-connections/{env}/frontend`. The CloudWatch Dashboard for each environment is named `sales-connections-{env}` and is provisioned by `infra/terraform/modules/observability/main.tf`.

## 2. Deploy Procedure

Deployment to `dev` and `staging` is fully automated on every merge to `main`. Deployment to `prod` requires a manual approval gate in the GitHub Actions UI. Deployment is rolling with zero downtime; ECS keeps the minimum healthy task count at 100 percent and allows up to 200 percent during the swap.

### Continuous deployment to dev and staging

1. Merge a pull request to the `main` branch.
2. `.github/workflows/cd.yml` triggers automatically on the push event for `main`.
3. The CI stage runs first: `ruff check`, `mypy`, `pytest --cov`, `eslint`, `tsc --noEmit`, `vitest run`, `vite build`, `docker build` for both backend and frontend images.
4. The deploy stage authenticates to AWS via OIDC federation; no long-lived AWS access keys are stored in GitHub.
5. The backend image is pushed to ECR with two tags: `sales-connections-backend:sha-<commit>` and `sales-connections-backend:latest`.
6. The frontend image is pushed to ECR (`sales-connections-frontend:sha-<commit>` and `sales-connections-frontend:latest`); alternatively the static bundle is synced to S3 and a CloudFront invalidation is issued for `/index.html` and `/assets/*`.
7. `terraform plan` runs against the `dev` composition; if the plan succeeds and the changes are within policy bounds, `terraform apply` runs automatically.
8. The same plan-and-apply sequence runs against `staging` after the `dev` apply succeeds.
9. ECS performs a rolling deployment of the backend service: minimum healthy 100 percent, maximum 200 percent. The new task count is brought up before any old task is drained.
10. ALB target-group health checks (`/healthz` for liveness, `/readyz` for readiness) gate the deployment. If a new task fails three consecutive readiness probes, ECS rolls the deployment back to the previous task definition.

### Manual production deploy

1. Confirm `staging` has been green for at least 30 minutes by checking the CloudWatch dashboard for the staging environment.
2. Confirm zero open critical alarms across all environments via the CloudWatch alarm console.
3. In the GitHub Actions UI, locate the most recent CD workflow run for the desired commit on `main`.
4. Click "Approve and deploy to prod" on the prod-approval job; this requires the approver to have write access to the repository and to be a member of the `production-deployers` GitHub team.
5. Inspect the `terraform plan` output rendered in the job log; reject the deployment if the plan includes unexpected resource destruction or replacement.
6. Confirm the apply by clicking the approval button; `terraform apply` proceeds and the ECS rolling deployment runs as described above.
7. Post-deploy verification: confirm `/healthz` and `/readyz` for every prod task; observe the CloudWatch dashboard for the prod environment for elevated 5xx rates or P95 latency excursions during the first 15 minutes after the apply completes.

```bash
# Post-deploy smoke check (run from a workstation with prod IAM credentials)
curl -fsS https://sales-connections.example.com/healthz | jq .
curl -fsS https://sales-connections.example.com/readyz  | jq .

# Inspect the most recent ECS task health
aws ecs describe-services \
  --cluster sales-connections-prod \
  --services backend \
  --query 'services[0].deployments'
```

### Database migrations

Database migrations are managed by Alembic and live under `backend/migrations/versions/`. The migration ordering matters: in production, the schema migration runs as a one-shot ECS task before the rolling deployment of the new backend version. Migrations are forward-compatible per the migration policy in [`decision-log.md`](decision-log.md), so the previous application version continues serving traffic against the new schema during the rolling swap.

1. Migrations live under `backend/migrations/versions/` and are named `NNNN_<descriptor>.py`.
2. New migrations are generated locally with `alembic revision --autogenerate -m "<description>"`.
3. Every autogenerated migration is reviewed by hand before merge; never accept an `--autogenerate` output without inspecting the generated `op.*` calls.
4. Migrations apply automatically on backend container startup when the `RUN_MIGRATIONS=true` environment variable is set; this is the default configuration in `dev` and `staging`.
5. In production, `RUN_MIGRATIONS=false` on the long-running backend tasks. The Terraform CD module schedules a separate one-shot ECS `RunTask` invocation with `RUN_MIGRATIONS=true` and the new image SHA before the rolling deployment of the long-running tasks.
6. Migration failures abort the deployment. The one-shot task exits non-zero, the CD workflow halts, and the previous backend version continues serving traffic.

```bash
# Manually run the production migration task with a specific image SHA
aws ecs run-task \
  --cluster sales-connections-prod \
  --task-definition sales-connections-prod-migration \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[subnet-...],securityGroups=[sg-...]}" \
  --overrides '{"containerOverrides":[{"name":"migration","environment":[{"name":"IMAGE_SHA","value":"sha-abc123"}]}]}'
```

## 3. Rollback Procedure

Three rollback paths exist depending on whether a schema change is involved. The simplest case is the application rollback (no schema change); schema rollbacks are genuinely risky and must be done deliberately.

### Application rollback (no schema change)

This path is the right one when the bad deployment is purely application code and the schema is unchanged. Forward-compatible migrations mean the previous application version can run safely against the current schema.

1. Identify the previous-known-good image SHA, for example `sha-abc123`. Use the CloudWatch dashboard to find the last commit before metrics regressed.
2. In GitHub Actions, find the CD workflow run for that SHA on the `main` branch.
3. Click "Re-run prod deployment" on that run. The workflow re-pushes the same image tags to ECR (idempotent) and re-applies Terraform with the prior image SHA pinned.
4. ECS performs a rolling deployment that swaps tasks back to the prior image.
5. Confirm `/healthz` and `/readyz` are green on every task; confirm error rate has returned to baseline on the CloudWatch dashboard.

```bash
# Quick rollback via the AWS CLI when a CD workflow re-run is not available
aws ecs update-service \
  --cluster sales-connections-prod \
  --service backend \
  --task-definition sales-connections-prod-backend:<previous-revision> \
  --force-new-deployment
```

### Schema rollback

Schema rollback is the path of last resort. Migrations are forward-compatible per the migration policy in [`decision-log.md`](decision-log.md): column drops happen in a separate later migration after verification, so the previous application version typically still works against the new schema and a schema downgrade is rarely necessary.

1. Confirm whether the application rollback alone is sufficient. In most cases it is.
2. If a schema downgrade is genuinely required, run `alembic downgrade -1` against the production database via a one-shot ECS task with the same network configuration as the migration task.
3. Column drops require manual coordination. Never run a destructive downgrade automatically; the operator must verify that no application instance is still expecting the dropped column.
4. Take a manual RDS snapshot before any destructive schema change so that point-in-time recovery is supplemented by an explicit checkpoint.

```bash
# Take an explicit pre-rollback snapshot
aws rds create-db-snapshot \
  --db-instance-identifier sales-connections-prod \
  --db-snapshot-identifier sales-connections-prod-pre-rollback-$(date +%Y%m%d-%H%M)

# Run the alembic downgrade as a one-shot task
aws ecs run-task \
  --cluster sales-connections-prod \
  --task-definition sales-connections-prod-migration \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[subnet-...],securityGroups=[sg-...]}" \
  --overrides '{"containerOverrides":[{"name":"migration","command":["alembic","downgrade","-1"]}]}'
```

### Emergency hotfix path

When production is broken and the right fix is small and obvious, the hotfix path bypasses the normal pull-request review burden while preserving the auditability requirements.

1. Branch from `main`: `git checkout -b hotfix/<issue-id>`.
2. Apply the minimal fix; add a row to [`decision-log.md`](decision-log.md) recording the decision and the deferred follow-ups.
3. Open a pull request labeled `hotfix`; expedited review is one approver from the `production-deployers` team.
4. Merge to `main`; the CD pipeline auto-deploys to `dev` and `staging`. Confirm both are green.
5. Approve the prod deployment manually as in section 2.

## 4. Secret Rotation

All secrets live in AWS Secrets Manager and are referenced from the ECS task definition via secret-arn injection. The application reads each secret at process startup via `boto3.client('secretsmanager')` and caches it for the worker lifetime. Rotation requires a rolling restart of the ECS service so workers re-read the rotated value. The JWT signing key is a flat single-value secret in the MVP delivery — there is no zero-downtime rotation path; rotating the signing key forces all users to re-authenticate. Per-user session revocation (logout, forced sign-out) is a separate mechanism that uses the `users.token_version` counter and is not affected by signing-key rotation.

### Secret catalog

| Secret | Storage | Rotation Cadence | Procedure |
|--------|---------|------------------|-----------|
| `ANTHROPIC_API_KEY` | AWS Secrets Manager (`sales-connections/{env}/anthropic-api-key`) | Annual or on suspected leak | Generate new key in Anthropic Console; update Secrets Manager; rolling restart of ECS tasks; revoke old key |
| `GOOGLE_OAUTH_CLIENT_SECRET` | AWS Secrets Manager (`sales-connections/{env}/google-oauth-client-secret`) | Annual or on suspected leak | Rotate in Google Cloud Console; update Secrets Manager; rolling restart |
| `JWT_SIGNING_KEY` | AWS Secrets Manager (`sales-connections/{env}/jwt-signing-key`) | Annual or on suspected leak | Generate new 32-byte random; update Secrets Manager with the new key (single string value, no versioned list); rolling restart; all live sessions invalidated — operators must broadcast the re-authentication requirement before rotation |
| `DB_PASSWORD` | AWS Secrets Manager (`sales-connections/{env}/db-password`) | Annual or on suspected leak | Rotate via RDS console with secret rotation enabled; ECS task role re-reads on next request |

### Step-by-step secret rotation (Anthropic API key as example)

1. Generate the new key in the Anthropic Console; copy the value to a secure scratch location.
2. Update Secrets Manager with the new value.

   ```bash
   aws secretsmanager update-secret \
     --secret-id sales-connections/prod/anthropic-api-key \
     --secret-string '{"api_key":"sk-ant-..."}'
   ```

3. Force a rolling restart of the backend ECS service so workers re-read the rotated secret.

   ```bash
   aws ecs update-service \
     --cluster sales-connections-prod \
     --service backend \
     --force-new-deployment
   ```

4. Verify the new tasks pick up the rotated secret by issuing a real AI generation request.

   ```bash
   curl -sS \
     -H 'Content-Type: application/json' \
     -H 'X-Correlation-Id: rotation-test-1' \
     -b "session=<valid-session-cookie>" \
     -d '{"context":"rotation smoke test"}' \
     https://sales-connections.example.com/api/notes/generate \
     | jq .
   ```

5. Revoke the old key in the Anthropic Console only after the verification request succeeds.
6. Add a row to [`decision-log.md`](decision-log.md) documenting the rotation event, the rotation date, and the operator responsible.

### JWT signing key rotation (special case)

The JWT signing key (`JWT_SIGNING_KEY` in AWS Secrets Manager) is a flat single-value secret in the MVP delivery. There is NO zero-downtime rotation path — rotating the signing key forces every active session to be re-authenticated because all in-flight JWTs were signed with the previous key and will fail signature verification under the new key. Earlier drafts of this runbook described a `current`/`prior` two-slot key list; that mechanism was not implemented (rationale in [`decision-log.md`](decision-log.md) DL-0043 — the platform uses a per-user `users.token_version` counter for session revocation, NOT a global key version, so the two-slot list is not needed for the per-user invalidation use case).

Per-user session revocation (logout, forced sign-out, password change) is a separate mechanism that uses the `users.token_version` counter and does NOT require signing-key rotation. See [`docs/security.md`](../docs/security.md#token-rotation-strategy) §2 for details. Operators should rotate the signing key only for an annual cadence or in response to a suspected key compromise, NOT as a routine "log everyone out" operation.

When rotating the signing key, all users must be informed in advance that their sessions will be terminated.

1. Generate a new 32-byte random key (`openssl rand -base64 32`); copy the value to a secure scratch location.
2. Broadcast the upcoming forced-re-authentication to users (status page, in-product banner, email — operator's choice consistent with the deployment's communication policy). Observe a documented quiet period if required.
3. Update Secrets Manager with the new key as a single string value.

   ```bash
   aws secretsmanager update-secret \
     --secret-id sales-connections/prod/jwt-signing-key \
     --secret-string '<new-key>'
   ```

4. Rolling restart the backend ECS service so workers re-read the rotated secret.

   ```bash
   aws ecs update-service \
     --cluster sales-connections-prod \
     --service backend \
     --force-new-deployment
   ```

5. Verify the rotation took effect: log in as a test account, capture the resulting `session` cookie, and confirm `GET /api/me` succeeds. Optionally take a JWT minted before the rotation (from a prior `curl -i POST /auth/login`) and confirm `GET /api/me` with that pre-rotation cookie returns HTTP 401 `unauthorized`.

   ```bash
   # Confirm a fresh login works under the new key
   curl -sS -c /tmp/post-rotation-session.txt \
     -H 'Content-Type: application/json' \
     -d '{"email":"<test-account>","password":"<test-password>"}' \
     https://sales-connections.example.com/auth/login | jq .
   curl -sS -b /tmp/post-rotation-session.txt \
     https://sales-connections.example.com/api/me | jq .

   # Confirm a pre-rotation cookie is rejected
   curl -sS -b /tmp/pre-rotation-session.txt \
     -o /dev/null -w '%{http_code}\n' \
     https://sales-connections.example.com/api/me
   ```

6. Add a row to [`decision-log.md`](decision-log.md) documenting the rotation event, the rotation date, and the operator responsible.

If a future hardening task introduces a two-slot key list (the AAP §0.7.4 "Tokens rotated on logout" invariant is already satisfied without it), this section should be re-written to describe the overlap window. Until then, plan for the user-visible re-authentication requirement on every signing-key rotation.

## 5. Observability

The platform is engineered to be observable end-to-end. Five distinct mechanisms cover the observability surface: structured logging, distributed tracing, a metrics endpoint, health and readiness probes, and a CloudWatch Dashboard. Each mechanism is verified end-to-end in the local Docker Compose environment before being deployed to AWS so on-call engineers can reproduce production signals locally.

### Structured logs

All application logs are JSON via structlog, written to stdout, and shipped to CloudWatch by the `awslogs` Docker driver configured in the ECS task definition.

Required fields on every log line: `timestamp` (ISO 8601 UTC), `level` (one of `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`), `correlation_id`, `event` (a short human-readable name), `service` (`sales-connections-api`), `version` (the deployed git SHA).

Conditional fields (present when applicable): `user_id`, `org_id`, `route`, `method`, `status_code`, `latency_ms`, `error_code`, `trace_id`, `span_id`.

The structlog redaction processor in `backend/app/observability/logging.py` filters keys matching `*_key`, `*_secret`, `password`, `token`, `authorization` and substitutes `***REDACTED***`. The processor runs before the JSON renderer so redacted values never reach stdout.

Sample query in CloudWatch Logs Insights for the most recent error events on the prod backend:

```
fields @timestamp, correlation_id, event, status_code, latency_ms, route
| filter level = "ERROR"
| sort @timestamp desc
| limit 100
```

Sample query to trace a single request across all log lines using the correlation ID:

```
fields @timestamp, level, event, status_code, latency_ms
| filter correlation_id = "<correlation-id>"
| sort @timestamp asc
```

### Metrics

Application metrics are exposed at `GET /metrics` in Prometheus exposition format via `prometheus_client`. The endpoint is anonymous but restricted to the private VPC subnet by the ECS task security group; the public ALB does not forward `/metrics` to the backend.

Counters:

- `http_requests_total{method, path, status}` — total HTTP requests handled.
- `failed_login_attempts_total{outcome}` — total failed password-login attempts partitioned by outcome (`user_not_found`, `wrong_password`, `oauth_only_user`). Dedicated security signal for SIEM alerting; isolated from the generic `http_requests_total{path="/auth/login",status="401"}` series.

Histograms:

- `http_request_duration_seconds{method, path}` — wall-clock duration of every HTTP request.
- `ai_request_duration_seconds{outcome}` — wall-clock duration of every Anthropic Claude call (with `outcome ∈ {success, timeout, error, validation}`).
- `audit_emit_duration_seconds{event_type}` — wall-clock duration of every audit event emission inside its parent transaction. Per AAP §0.7.3 the P95 must remain under 100 ms. The Prometheus-emitted `_count` series of this histogram is the operative "total audit events emitted" counter.

Gauges:

- `active_sessions` — per-worker count of currently-known session JWTs. Incremented on session JWT mint, decremented on explicit logout. Resets to zero on worker restart and does not track natural JWT expiry.

Built-in collectors (registered against the custom registry in `app/extensions.py`):

- `process_*` — `resident_memory_bytes`, `virtual_memory_bytes`, `cpu_seconds_total`, `open_fds`, `max_fds`, `start_time_seconds`.
- `python_*` — `gc_objects_collected_total`, `gc_objects_uncollectable_total`, `gc_collections_total`, `python_info`.

These are exposed for Prometheus-native operator tooling (Grafana, Alertmanager, kube-prometheus-stack). In production AWS deployments the ECS CloudWatch container insights provides equivalent per-task metrics; both surfaces are kept for redundancy and tooling flexibility.

The metrics endpoint is scraped either by a CloudWatch agent sidecar deployed in the same ECS task or by the AWS Distro for OpenTelemetry collector configured to forward to CloudWatch Metrics. Both topologies are supported; the `infra/terraform/modules/observability/main.tf` module selects between them via the `metrics_collector` Terraform variable.

A sample alarm fires when the AI P95 latency exceeds 5 seconds for 5 consecutive minutes; the alarm publishes to the `sales-connections-prod-alarms` SNS topic which fans out to PagerDuty and the operations Slack channel.

### Distributed tracing

Distributed tracing is implemented with the OpenTelemetry SDK and an OTLP HTTP exporter. The exporter forwards spans to the AWS Distro for OpenTelemetry collector running as a sidecar; the collector forwards to CloudWatch ServiceLens.

Auto-instrumentation is enabled for three subsystems:

- `opentelemetry-instrumentation-flask` produces a request span for every inbound HTTP request.
- `opentelemetry-instrumentation-sqlalchemy` produces a child span for every database query.
- The Anthropic and Authlib outbound HTTP clients produce external-call spans via the requests instrumentation.

The service name is `sales-connections-api`. Trace context is propagated across the ALB via the `traceparent` header (W3C Trace Context). The frontend generates a fresh trace ID for the SPA's correlation ID lifecycle; the backend respects the inbound `traceparent` and continues the same trace.

### Health checks

Two endpoints serve distinct purposes. Both are anonymous and restricted to the private VPC subnet by the ECS task security group.

- `GET /healthz` — Liveness probe. Returns 200 unconditionally as long as the process is running. Used by the ECS task health check (the container is killed and restarted if liveness fails) and by the ALB target-group health check (the target is taken out of rotation if liveness fails). The handler is intentionally minimal so a deadlock or pool exhaustion never causes a liveness failure.
- `GET /readyz` — Readiness probe. Returns 200 only if `SELECT 1` against RDS succeeds within 1 second. Returns 503 with a diagnostic body otherwise. Used by the rolling deployment to gate the cutover; new tasks are not considered healthy until readiness passes three consecutive checks.

```bash
# Liveness from inside the VPC
curl -fsS http://<task-private-ip>:8000/healthz

# Readiness from inside the VPC
curl -fsS http://<task-private-ip>:8000/readyz | jq .
```

### CloudWatch Dashboard

The CloudWatch Dashboard for each environment is named `sales-connections-{env}` and is provisioned by `infra/terraform/modules/observability/main.tf`. The panels are designed so that every performance budget in the platform has a corresponding visualization and alarm. Each panel maps to a specific budget or operational concern.

| Panel | Source | Aligns With Budget |
|-------|--------|---------------------|
| 1. Request rate (requests/sec by route) | `http_requests_total` rate | Capacity awareness |
| 2. Error rate (5xx/sec by route) | `http_requests_total` filtered to status >= 500 | Backend availability |
| 3. P50 / P95 / P99 latency (per route) | `http_request_duration_seconds` quantiles | Form submit ≤ 2 s budget |
| 4. AI latency P95 | `ai_request_duration_seconds` P95 | AI ≤ 5 s P95 budget |
| 5. RBAC P50 | `http_request_duration_seconds` filtered to RBAC-only routes | RBAC ≪ 50 ms budget |
| 6. Audit emission P50 | `db_query_duration_seconds{query_type="INSERT"}` filtered to audit_events | Audit ≤ 100 ms budget |
| 7. Active sessions gauge | `active_sessions` | Capacity awareness |
| 8. Database connection pool saturation | SQLAlchemy pool metrics via OTEL | Pool sizing |
| 9. RDS CPU and memory | AWS/RDS CloudWatch metrics | RDS health |
| 10. ECS task health | AWS/ECS CloudWatch metrics | Task lifecycle |

### Alarms

Alarms are provisioned alongside the dashboard in `infra/terraform/modules/observability/main.tf`. The alarm catalog mirrors the performance budgets so each budget is monitored automatically. The `Page on-call` alarms publish to the PagerDuty integration; the `Notify channel` alarms publish to the operations Slack channel only.

| Alarm | Threshold | Action |
|-------|-----------|--------|
| `BackendErrorRate5xx` | > 1% over 5 min | Page on-call |
| `BackendP95Latency` | > 2s over 5 min (excluding `/api/notes/generate`) | Page on-call |
| `AILatencyP95` | > 5s over 5 min | Notify channel |
| `RBACLatencyP50` | > 50ms over 5 min | Notify channel |
| `AuditEmissionLatency` | > 100ms over 5 min | Notify channel |
| `RDSConnectionsExhaustion` | > 80% over 5 min | Page on-call |
| `RDSCPU` | > 80% for 10 min | Notify channel |
| `ECSTaskUnhealthy` | Any task unhealthy 3 consecutive checks | Page on-call |

### Verifying observability locally

The Docker Compose stack at the repository root reproduces the production observability surface. After `docker compose up --build`, every signal listed above is observable on the operator's workstation.

```bash
# Structured logs (JSON one per line)
docker compose logs -f backend | jq .

# Metrics endpoint
curl -fsS http://localhost:5000/metrics | head -50

# Liveness and readiness
curl -fsS http://localhost:5000/healthz
curl -fsS http://localhost:5000/readyz | jq .

# A single end-to-end traced request
curl -fsS \
  -H 'X-Correlation-Id: local-test-1' \
  http://localhost:5000/api/connections \
  -b "session=<dev-session-cookie>" \
  | jq .
```

## 6. Database Operations

The database is PostgreSQL 17.7 running on RDS Multi-AZ with synchronous replication to the standby. Production and staging instances are not directly reachable from operator workstations; use a bastion host or AWS Session Manager port forwarding.

### Connection details

Hostnames, port (5432), and database name (`sales_connections`) are exposed as Terraform outputs from the `database` module. Credentials live in Secrets Manager only; never fetch the password from the Terraform state. The application user has `INSERT` only on `audit_events`; the migrations user has full DDL privileges and is used exclusively by the Alembic one-shot tasks.

```bash
# Establish a port-forward to RDS through Session Manager (operator workstation)
aws ssm start-session \
  --target <bastion-instance-id> \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters '{"host":["<rds-hostname>"],"portNumber":["5432"],"localPortNumber":["55432"]}'

# Connect via psql with credentials from Secrets Manager
PGPASSWORD=$(aws secretsmanager get-secret-value \
  --secret-id sales-connections/prod/db-password \
  --query SecretString --output text | jq -r .password) \
  psql -h localhost -p 55432 -U app_readonly -d sales_connections
```

### Backup strategy

Automated daily snapshots are taken by RDS at 04:00 UTC. Retention is 7 days for `dev` and `staging`, 30 days for `prod`. Point-in-time recovery is enabled on every environment with a recovery window equal to the snapshot retention. The encryption key is the AWS-managed `aws/rds` KMS key in MVP; a customer-managed key is a planned post-MVP enhancement.

### Manual backup before risky operation

Take a manual snapshot before any destructive schema change, large data backfill, or production-data investigation that may require rollback.

```bash
aws rds create-db-snapshot \
  --db-instance-identifier sales-connections-prod \
  --db-snapshot-identifier sales-connections-prod-pre-$(date +%Y%m%d-%H%M)
```

The snapshot is encrypted at rest with the same KMS key as the source instance and is region-local; cross-region copies are a planned post-MVP enhancement.

### Restore from snapshot

Restore creates a new RDS instance from a snapshot. The new instance is independent of the original; promoting it to take over the production traffic requires updating the Terraform `database` module's `db_instance_identifier` variable and re-applying.

```bash
aws rds restore-db-instance-from-db-snapshot \
  --db-instance-identifier sales-connections-prod-restore \
  --db-snapshot-identifier <snapshot-id>
```

After the restore completes, run `/readyz` against a sample backend task pointed at the restored instance, confirm the schema version matches the deployed application version, and only then update the Terraform variable and apply.

### Audit table inspection

The audit trail is the canonical record of every state change. Inspecting the audit history for a specific record is the first step in investigating data anomalies.

```sql
SELECT id, actor_user_id, target_record_id, event_type, event_timestamp,
       before_payload, after_payload
FROM audit_events
WHERE target_record_id = <id>
ORDER BY event_timestamp DESC;
```

The application user has only `INSERT` on `audit_events`; ad-hoc inspection requires a read-only role. The `app_readonly` role provided in the migration has `SELECT` on `audit_events` and is the right tool for this query.

### Audit immutability invariant — role provisioning order

The F-013 audit-immutability invariant (AAP §0.7.1 invariant 5: "No code path issues `UPDATE` or `DELETE` against `audit_events`") is enforced at the database layer by the `0002_token_version_and_app_role` migration, which runs:

```sql
GRANT SELECT, INSERT ON audit_events TO sales_connections_app;
REVOKE UPDATE, DELETE, TRUNCATE ON audit_events FROM sales_connections_app;
```

**The invariant is silently violated if the `sales_connections_app` role is created with default DML privileges AFTER migration 0002 has already run.** This can happen in CI environments and dev-bootstrap scripts that:

1. Run `alembic upgrade head` first (which fires `REVOKE UPDATE, DELETE, TRUNCATE` while the role does not yet exist — the REVOKE is a NOTICE, not an error, but it has no effect).
2. Provision the `sales_connections_app` role afterwards via a separate setup script.
3. That setup script applies a blanket `GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO sales_connections_app` to make the application work — overwriting the audit-table protection.

When this ordering bug occurs, `DELETE FROM audit_events` succeeds as the application role and the audit trail is no longer append-only. This was QA Issue #9 of Checkpoint 2 and was observed silently corrupting 35 audit rows before detection.

#### Detection

Run this query against the live database (or the CI database) to verify the invariant:

```sql
SELECT grantee, privilege_type
FROM information_schema.role_table_grants
WHERE table_name = 'audit_events'
  AND grantee = 'sales_connections_app'
ORDER BY privilege_type;
```

Expected output (exactly two rows):

```
       grantee        | privilege_type
----------------------+----------------
 sales_connections_app | INSERT
 sales_connections_app | SELECT
```

If `UPDATE`, `DELETE`, or `TRUNCATE` appears in the output, the invariant is violated.

#### Remediation

Re-run migration 0002 to re-apply the GRANT/REVOKE block (the migration's "Phase 3" block is idempotent):

```bash
cd backend
.venv/bin/alembic upgrade head
```

Alternatively apply the GRANT/REVOKE manually:

```sql
GRANT SELECT, INSERT ON audit_events TO sales_connections_app;
REVOKE UPDATE, DELETE, TRUNCATE ON audit_events FROM sales_connections_app;
```

#### Permanent prevention

The recommended provisioning order is:

1. Create the `sales_connections_app` role first (via `infra/terraform/modules/database/main.tf` for production, or via the conftest `_provision_app_role` helper for tests).
2. Run `alembic upgrade head` second.

Additionally, the test `backend/tests/api/test_connections.py::TestAuditInvariantOnAppRole` connects to the database AS the `sales_connections_app` role and asserts that `UPDATE` and `DELETE` against `audit_events` raise permission-denied errors. If a CI environment ever drifts into the violated state, this test fails fast.

### Common queries

Find normalized URLs that appear more than once across the organization:

```sql
SELECT normalized_linkedin_url, COUNT(*) AS hit_count
FROM records
WHERE org_id = <org_id> AND deleted_at IS NULL
GROUP BY normalized_linkedin_url
HAVING COUNT(*) > 1
ORDER BY hit_count DESC;
```

Identify the most-active contributors over the last 30 days (mirrors the Admin Analytics endpoint):

```sql
SELECT u.display_name, u.email, COUNT(r.id) AS record_count
FROM records r
JOIN users u ON u.id = r.owner_user_id
WHERE r.org_id = <org_id>
  AND r.deleted_at IS NULL
  AND r.submission_date >= NOW() - INTERVAL '30 days'
GROUP BY u.id, u.display_name, u.email
ORDER BY record_count DESC
LIMIT 20;
```

Inspect the open lead pipeline by status:

```sql
SELECT outreach_status, COUNT(*) AS lead_count
FROM records
WHERE org_id = <org_id> AND deleted_at IS NULL
GROUP BY outreach_status
ORDER BY outreach_status;
```

## 7. Incident Response

Every incident follows the same lifecycle: detect, triage, mitigate, resolve, postmortem. The on-call engineer is the incident commander by default; the commander may delegate roles (communicator, investigator, scribe) on a SEV-1.

### Severity levels

| Severity | Definition | Response Time |
|----------|------------|---------------|
| SEV-1 | Production unavailable; users cannot log in or load the feed | Page on-call immediately |
| SEV-2 | Significant degradation (high error rate, slow responses); some users impacted | Page on-call within 15 min |
| SEV-3 | Minor issue (single feature broken, non-blocking warning) | Triage during business hours |
| SEV-4 | Cosmetic or low-impact | Backlog |

The severity table aligns with the alarm catalog in section 5: `Page on-call` alarms map to SEV-1 or SEV-2 depending on the magnitude of the breach; `Notify channel` alarms map to SEV-3 unless the on-call escalates them.

### Initial triage checklist

Run through this checklist within the first 5 minutes of any SEV-1 or SEV-2 page. The checklist is designed to be parallelizable; multiple responders can take items concurrently.

1. Confirm scope: which environments are affected, which features are degraded, which user segment is impacted. Use the CloudWatch dashboard's per-route panels to localize the problem.
2. Check the CloudWatch dashboard for the affected environment for spikes in error rate, latency, or saturation.
3. Check the most recent deployment timeline against the start of the incident: did the symptoms begin within minutes of a CD apply?
4. Check `/healthz` and `/readyz` for every running task; isolate any task that fails readiness and inspect its logs.
5. Check the RDS CloudWatch panels for connection exhaustion, CPU, or storage spikes.
6. Check the Anthropic status page (https://status.anthropic.com) for AI-related incidents.
7. Check the Google OAuth status page (https://www.google.com/appsstatus) for authentication-related incidents.
8. Open an incident channel in Slack (`#incident-<short-id>`); declare the severity; assign the incident commander.

### Common incident patterns and mitigations

- AI timeouts cascading. Confirm the Anthropic status page; if the issue is upstream, the system already degrades gracefully (form submission proceeds with empty `ai_notes`). Communicate the degradation to users via the in-app banner; no infrastructure action is required beyond communication.
- OAuth callback failures. Check that the Google client redirect URIs in the Google Cloud Console match the prod callback URL exactly; rotate the client secret if there is any suspicion it was leaked.
- Database connection exhaustion. The first remediation is reducing the Gunicorn worker count to relieve pressure on the connection pool; the durable fix is bumping the RDS instance class so `max_connections` increases. Confirm `pool_size` in SQLAlchemy is aligned with worker count and that long-running queries are not parked on a connection.
- Audit table immutability error. Any error mentioning `permission denied for relation audit_events` on an UPDATE or DELETE indicates either a code defect (a mutation path slipped through) or a security event (an attacker tried to mutate audit history). Treat this as SEV-1 until proven otherwise; pull the offending request's correlation ID and trace it through the audit logs.
- Spike in 401 responses. Check the JWT signing key rotation status; verify that the `prior` slot is still populated if the rotation happened within the last TTL window. Also check the system clock skew on the ECS tasks; large skews cause valid tokens to read as expired.
- Sudden 5xx spike on a single route. Pull the most recent ERROR log lines for that route via the Logs Insights query in section 5; correlate with the most recent deployment SHA; consider rolling back if the errors started with the deploy.
- RDS storage pressure. Increase storage via the `database` Terraform module's `allocated_storage` variable and apply; storage scaling is online and does not require failover.

### Postmortem expectations

Every SEV-1 and SEV-2 requires a postmortem completed within 5 business days of resolution. The postmortem template (kept in the team's incident-response wiki) includes: timeline, detection mechanism, root cause, contributing factors, what went well, what went poorly, and action items. Action items that require an architectural decision become a row in [`decision-log.md`](decision-log.md) (for example, "we will add per-org rate limiting" becomes a decision row); action items that are pure follow-up engineering become tickets in the standard backlog.

## 8. Routine Tasks

The routine tasks below keep the platform healthy between incidents. They are calendar-driven and assigned to the on-call rotation.

### Weekly

- Review the CloudWatch alarm history for the previous week. Tune thresholds if any alarm is consistently noisy and not actionable; record the threshold change as a decision-log row.
- Review the ECR repository sizes. The lifecycle policy auto-cleans non-deployed images older than 30 days; if the policy fails, manually delete unreferenced images to keep storage costs bounded.
- Review the RDS slow query log. New offenders are usually queries introduced by a recent feature; open a backlog ticket and consider an index addition.

### Monthly

- Review IAM role usage via IAM Access Analyzer. Remove unused roles and tighten over-broad permissions.
- Review Secrets Manager secret access logs (CloudTrail data events). Confirm no anomalous read patterns; investigate any secret reads from outside the ECS task role's expected access pattern.
- Review CodeQL, Dependabot, and ECR image scanner findings. Triage open vulnerabilities into the standard backlog; track high-severity findings until resolved.

### Quarterly

- Rotate `JWT_SIGNING_KEY` per the procedure in section 4.
- Run a disaster-recovery drill: restore a recent prod snapshot to a sandbox environment, point a sandbox backend task at it, and confirm `/readyz` returns 200 and a sample login succeeds.
- Re-validate that the `ANTHROPIC_API_KEY` works end-to-end by issuing a real `POST /api/notes/generate` request from a staging or sandbox environment.
- Review the on-call rotation schedule for the next quarter; confirm coverage and PagerDuty integration health.

## 9. Capacity Planning

The platform is engineered for the MVP scale ceiling of 10,000 records per organization with a single organization runtime. Indexes, RDS instance class, and ECS task counts are sized for this ceiling.

### MVP scale ceiling

The composite index `(org_id, deleted_at, submission_date DESC)` on `records` keeps the feed responsive at 10,000 records. The unique partial index `(org_id, normalized_linkedin_url) WHERE deleted_at IS NULL` keeps the duplicate-detection probe sub-second at the same scale. The MVP does not implement table partitioning, RDS read replicas, server-side caching, or multi-region active-active topology; each is a planned post-MVP enhancement triggered by the scaling thresholds below.

### When to scale

- RDS CPU sustained above 70 percent over a 30-minute window: bump to the next instance class via the `database` module's `instance_class` variable. RDS instance class changes are online with a brief failover.
- ECS tasks consistently at or above 70 percent CPU or memory utilization: bump the `desired_count` (horizontal scale) or the task `cpu` and `memory` (vertical scale) via the `ecs` module's variables.
- Database connection pool exhaustion (the `RDSConnectionsExhaustion` alarm): increase `max_connections` in the RDS parameter group and increase `pool_size` in SQLAlchemy to match the new ceiling. The two values must move together.

### Pre-emptive scaling triggers

Three architectural changes are triggered by the post-MVP roadmap rather than a single threshold breach. Each is documented as a suggested next task in [`onboarding.md`](onboarding.md).

- Approaching 10,000 records in any organization: plan table partitioning on `records` and `audit_events` by `org_id` or by month on `submission_date`. Partitioning is a post-MVP enhancement.
- Read-heavy traffic dominating the workload (read-to-write ratio above 10:1 sustained): plan an RDS read replica and route the feed and detail-view queries to it. The audit emit and state-change paths continue to write to the primary. Read replicas are a post-MVP enhancement.
- Multi-region requirement (regulatory or latency-driven): re-architect with global RDS Aurora and a region-aware ALB. Multi-region is a post-MVP architectural change.

## 10. See Also

- [`README.md`](../README.md) — project entry point, quick-start, and pointers into `docs/`.
- [`onboarding.md`](onboarding.md) — clean-machine to running app, domain context, common pitfalls, suggested next tasks.
- [`architecture.md`](architecture.md) — what the architecture is at a glance; this runbook describes how to operate it.
- [`api.md`](api.md) — REST endpoint catalog; useful when investigating a specific endpoint during an incident.
- [`security.md`](security.md) — security model, RBAC matrix, audit invariants, secret handling.
- [`decision-log.md`](decision-log.md) — non-trivial decisions and their rationale; new operational decisions land here as rows.
- `infra/terraform/modules/observability/main.tf` — CloudWatch dashboard and alarms infrastructure-as-code.
- `.github/workflows/cd.yml` — continuous deployment pipeline; the apply step is the entry point for every prod deploy.
- `backend/Dockerfile` — production backend image build; the migration entry point is in this file.
- `backend/migrations/versions/` — Alembic migrations; new schema changes land as numbered files here.
