###############################################################################
# infra/terraform/modules/secrets/outputs.tf
#
# Outputs surfaced by the secrets module. Consumed by:
#   - infra/terraform/main.tf (root composition module wiring)
#   - infra/terraform/outputs.tf (re-exported as root composition outputs)
#   - infra/terraform/modules/database/ (db_password_arn for RDS master pwd)
#   - infra/terraform/modules/ecs/ (all four ARNs for task definition
#                                    secrets block resolved at task start)
#
# These outputs are the public CONTRACT of the secrets module. Renaming any
# output here is a breaking change to the entire Terraform composition.
#
# CRITICAL SECURITY INVARIANT (AAP Sec 0.7.4):
#   This file MUST NOT expose secret VALUES. Outputs surface only ARNs and
#   names that point to where the secrets live. The ECS task IAM role reads
#   the secret values at runtime via the AWS Secrets Manager API.
#
# Convention:
#   - Every output declares description (mandatory per Authoring Conventions).
#   - No `sensitive = true` markers on ARNs/names: ARNs are reference
#     identifiers visible in CloudTrail and Terraform state and are not
#     themselves secrets. Marking them sensitive would be misleading and
#     would suppress useful console output during `terraform apply`.
#   - Outputs reference aws_secretsmanager_secret.X.arn / .name ONLY.
#     They MUST NOT reference aws_secretsmanager_secret_version.X.secret_string
#     (which would expose the value), aws_secretsmanager_secret_version.X.id,
#     or random_password.X.result (which would expose the seeded value
#     before AWS owns it).
###############################################################################

###############################################################################
# 1. ANTHROPIC_API_KEY (F-002 AI Note Generation)
#
# Consumed by modules/ecs/ task definition: the ECS task execution role
# fetches this secret at task launch and injects its value as the
# ANTHROPIC_API_KEY environment variable, which backend/app/config.py reads
# and passes to backend/app/services/ai_orchestration.py for the Langchain
# ChatAnthropic client construction.
###############################################################################

output "anthropic_api_key_arn" {
  description = "ARN of the AWS Secrets Manager entry holding the Anthropic Claude API key. Consumed by the ECS task definition's `secrets` block; the ECS task execution role uses this ARN to call secretsmanager:GetSecretValue at task launch and inject ANTHROPIC_API_KEY into the container environment. NEVER expose the secret value itself."
  value       = aws_secretsmanager_secret.anthropic_api_key.arn
}

output "anthropic_api_key_name" {
  description = "Friendly path-style name of the Anthropic API key secret (e.g., 'sales-connections-prod/anthropic_api_key'). Surfaced for the operations runbook so operators can locate the entry in the Secrets Manager console for manual value population via 'aws secretsmanager put-secret-value --secret-id <name>'."
  value       = aws_secretsmanager_secret.anthropic_api_key.name
}

###############################################################################
# 2. GOOGLE_OAUTH_CLIENT_SECRET (F-012 User Authentication)
#
# Consumed by modules/ecs/ task definition: read at task launch and injected
# as GOOGLE_OAUTH_CLIENT_SECRET. The Authlib OAuth client registered in
# backend/app/extensions.py uses this value to exchange the OAuth
# authorization code for an ID token at GET /auth/google/callback.
###############################################################################

output "google_oauth_client_secret_arn" {
  description = "ARN of the AWS Secrets Manager entry holding the Google OAuth 2.0 client secret. Consumed by the ECS task definition's `secrets` block; injected as GOOGLE_OAUTH_CLIENT_SECRET at task launch and read by the Authlib client during the OAuth code-exchange step. NEVER expose the secret value itself."
  value       = aws_secretsmanager_secret.google_oauth_client_secret.arn
}

output "google_oauth_client_secret_name" {
  description = "Friendly path-style name of the Google OAuth client secret (e.g., 'sales-connections-prod/google_oauth_client_secret'). Used by operators to locate the entry in the Secrets Manager console for value population."
  value       = aws_secretsmanager_secret.google_oauth_client_secret.name
}

###############################################################################
# 3. JWT_SIGNING_KEY (F-012 PyJWT session token mint/verify)
#
# Consumed by modules/ecs/ task definition: read at task launch and injected
# as JWT_SIGNING_KEY. backend/app/services/auth.py uses this value as the
# HS256 secret for mint_session_jwt() and verify_session_jwt(). The key
# may be Terraform-generated (var.generate_jwt_key = true) or externally
# populated; this output surfaces only the reference ARN, never the value.
###############################################################################

output "jwt_signing_key_arn" {
  description = "ARN of the AWS Secrets Manager entry holding the HS256 JWT signing key. Consumed by the ECS task definition's `secrets` block; injected as JWT_SIGNING_KEY at task launch and used by backend/app/services/auth.py for session token mint/verify. NEVER expose the secret value itself."
  value       = aws_secretsmanager_secret.jwt_signing_key.arn
}

output "jwt_signing_key_name" {
  description = "Friendly path-style name of the JWT signing key secret (e.g., 'sales-connections-prod/jwt_signing_key'). Used by operators to locate the entry in the Secrets Manager console for manual rotation (advancing the per-deployment signing-key version invalidates all outstanding session tokens)."
  value       = aws_secretsmanager_secret.jwt_signing_key.name
}

###############################################################################
# 4. DB_PASSWORD (RDS PostgreSQL master password)
#
# Dual-consumer:
#   - modules/database/ uses this ARN with data.aws_secretsmanager_secret_version
#     to set the RDS master_password at provision time.
#   - modules/ecs/ task definition reads at task launch and injects as
#     DB_PASSWORD; backend/app/config.py composes DATABASE_URL from this
#     plus the host/port/name/user injected as plaintext env vars.
#
# This dual-consumer pattern keeps a single source of truth for the password
# and supports rotation by an external Lambda (when
# var.enable_db_password_rotation is enabled) without re-applying Terraform.
###############################################################################

output "db_password_arn" {
  description = "ARN of the AWS Secrets Manager entry holding the RDS PostgreSQL master password. Consumed by modules/database/ for RDS master_password resolution at provision time AND by modules/ecs/ task definition for runtime DSN composition. NEVER expose the password value itself."
  value       = aws_secretsmanager_secret.db_password.arn
}

output "db_password_name" {
  description = "Friendly path-style name of the DB master password secret (e.g., 'sales-connections-prod/db_password'). Used by operators for manual value population (when var.generate_db_password = false) and referenced by the optional rotation Lambda when var.enable_db_password_rotation is enabled."
  value       = aws_secretsmanager_secret.db_password.name
}

###############################################################################
# Operational map: friendly identifier -> secret path-name
#
# This output is consumed by docs/operations.md to generate the secret
# inventory section, by CloudWatch dashboards for hyperlinks, and by
# operator scripts that iterate over secrets (e.g., bulk-rotation runs).
#
# Keys mirror the application-side identifiers (matching env var names in
# lowercase). Values are the resolved path-style Secrets Manager names
# (e.g., 'sales-connections-prod/anthropic_api_key', where the prefix is
# var.name_prefix from the parent composition). NEVER contains secret values.
#
# Note: Terraform output descriptions are static strings and do not permit
# $${var.X} interpolation; the illustrative prefix below is given as a
# literal example. The actual values returned at apply time are derived from
# var.name_prefix via local.secret_names.<key> in main.tf.
###############################################################################

output "secret_names" {
  description = "Map of friendly identifier to Secrets Manager secret name for operations tooling. Keys: 'anthropic_api_key', 'google_oauth_client_secret', 'jwt_signing_key', 'db_password'. Values: the resolved path-style names (e.g., 'sales-connections-prod/anthropic_api_key'; the prefix is var.name_prefix from the parent composition). Consumed by the operations runbook and CloudWatch dashboards. NEVER contains secret values."
  value = {
    anthropic_api_key          = aws_secretsmanager_secret.anthropic_api_key.name
    google_oauth_client_secret = aws_secretsmanager_secret.google_oauth_client_secret.name
    jwt_signing_key            = aws_secretsmanager_secret.jwt_signing_key.name
    db_password                = aws_secretsmanager_secret.db_password.name
  }
}

###############################################################################
# Operational map: friendly identifier -> secret ARN
#
# Companion to secret_names; surfaces the ARNs that the ECS task definition
# `secrets` block references. Useful when constructing IAM policy resource
# lists (allow secretsmanager:GetSecretValue on these four ARNs) and when
# the ECS task definition consumer switches to a `for` expression over
# secrets instead of four individual references.
###############################################################################

output "secret_arns" {
  description = "Map of friendly identifier to Secrets Manager secret ARN. Keys: 'anthropic_api_key', 'google_oauth_client_secret', 'jwt_signing_key', 'db_password'. Used by IAM policy authoring (resource scoping for the ECS task role's secretsmanager:GetSecretValue permission) and by operations tooling for bulk operations across the full secret inventory. NEVER contains secret values."
  value = {
    anthropic_api_key          = aws_secretsmanager_secret.anthropic_api_key.arn
    google_oauth_client_secret = aws_secretsmanager_secret.google_oauth_client_secret.arn
    jwt_signing_key            = aws_secretsmanager_secret.jwt_signing_key.arn
    db_password                = aws_secretsmanager_secret.db_password.arn
  }
}
