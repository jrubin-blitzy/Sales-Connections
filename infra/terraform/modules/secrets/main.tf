###############################################################################
# infra/terraform/modules/secrets/main.tf
#
# AWS Secrets Manager entries for the Sales-Connections platform's four
# runtime secrets, with resource policies granting read access exclusively
# to the ECS task IAM role (per the least-privilege invariant of AAP Sec 0.7.4).
#
# Secret inventory:
#   1. ANTHROPIC_API_KEY            (F-002 AI Note Generation)
#   2. GOOGLE_OAUTH_CLIENT_SECRET   (F-012 User Authentication)
#   3. JWT_SIGNING_KEY              (F-012 PyJWT mint/verify)
#   4. DB_PASSWORD                  (RDS master password)
#
# Resource pattern per secret:
#   aws_secretsmanager_secret.X            (the secret entry)
#     |-> aws_secretsmanager_secret_version.X  (CONDITIONAL initial value)
#     |-> aws_secretsmanager_secret_policy.X   (resource policy)
#
# For JWT_SIGNING_KEY and DB_PASSWORD additionally:
#   random_password.X                      (CONDITIONAL Terraform-generated value)
#
# For DB_PASSWORD optionally:
#   aws_secretsmanager_secret_rotation.db_password  (CONDITIONAL rotation hook)
#
# CRITICAL SECURITY INVARIANTS (per AAP Sec 0.4.6 / 0.7.4 and folder spec):
#   - NEVER hardcode secret values in this file.
#   - All secret_version resources carry lifecycle { ignore_changes =
#     [secret_string, version_stages] } so Terraform does NOT clobber
#     externally-populated production values on subsequent applies.
#   - Resource policies grant secretsmanager:GetSecretValue ONLY to the
#     principals supplied via var.reader_principal_arns (typically the ECS
#     task role).
#   - All secrets are named ${var.name_prefix}/<secret_name> for path-based
#     hierarchy in the Secrets Manager console.
#
# Resource graph (within this module):
#
#   data.aws_iam_policy_document.standard_secret_access  (defined in data.tf)
#     |-> aws_secretsmanager_secret_policy.anthropic_api_key
#     |-> aws_secretsmanager_secret_policy.google_oauth_client_secret
#     |-> aws_secretsmanager_secret_policy.jwt_signing_key
#
#   data.aws_iam_policy_document.db_password_access  (defined in data.tf)
#     |-> aws_secretsmanager_secret_policy.db_password
#
#   random_password.jwt_signing_key       (conditional)
#     |-> aws_secretsmanager_secret_version.jwt_signing_key
#   random_password.db_password           (conditional)
#     |-> aws_secretsmanager_secret_version.db_password
#
#   aws_secretsmanager_secret_policy.db_password
#     |-> aws_secretsmanager_secret_rotation.db_password (conditional)
#
# Final resource count: 15
#   - 4 aws_secretsmanager_secret           (always created)
#   - 4 aws_secretsmanager_secret_version   (count-gated; up to 4 instances)
#   - 4 aws_secretsmanager_secret_policy    (always created)
#   - 2 random_password                     (count-gated; jwt_signing_key, db_password)
#   - 1 aws_secretsmanager_secret_rotation  (count-gated; db_password only)
###############################################################################

###############################################################################
# 1. ANTHROPIC_API_KEY
#
# Used by: backend/app/services/ai_orchestration.py (F-002).
# Population strategy:
#   - Dev: Terraform writes a literal "placeholder-replace-via-cli" value via
#     aws_secretsmanager_secret_version when var.write_placeholder_versions
#     is true. Operator replaces via:
#       aws secretsmanager put-secret-value \
#         --secret-id sales-connections-dev/anthropic_api_key \
#         --secret-string sk-ant-...
#   - Prod: Terraform creates the secret entry but does NOT write a value
#     (var.write_placeholder_versions = false). Operator populates via the
#     same aws CLI command from a secure workstation.
###############################################################################

resource "aws_secretsmanager_secret" "anthropic_api_key" {
  name                    = local.secret_names.anthropic_api_key
  description             = "Anthropic Claude API key for Sales-Connections backend (F-002 AI Note Generation)"
  kms_key_id              = local.kms_key_id_or_null
  recovery_window_in_days = var.recovery_window_in_days

  tags = merge(local.module_tags, {
    Name       = local.secret_names.anthropic_api_key
    SecretType = "ApiKey"
    Service    = "Anthropic"
  })
}

resource "aws_secretsmanager_secret_version" "anthropic_api_key" {
  count = var.write_placeholder_versions ? 1 : 0

  secret_id     = aws_secretsmanager_secret.anthropic_api_key.id
  secret_string = local.placeholder_value

  # Critical: ignore_changes prevents Terraform from clobbering an
  # operator-populated value on subsequent applies. The placeholder is
  # the SEED value only; once the operator runs put-secret-value, the
  # secret diverges from the placeholder, and Terraform must respect
  # that divergence. version_stages is also ignored because Secrets
  # Manager assigns AWSCURRENT / AWSPREVIOUS automatically and Terraform
  # must not interfere with that lifecycle.
  lifecycle {
    ignore_changes = [secret_string, version_stages]
  }
}

resource "aws_secretsmanager_secret_policy" "anthropic_api_key" {
  secret_arn = aws_secretsmanager_secret.anthropic_api_key.arn
  policy     = data.aws_iam_policy_document.standard_secret_access.json
}

###############################################################################
# 2. GOOGLE_OAUTH_CLIENT_SECRET
#
# Used by: backend/app/extensions.py (Authlib OAuth client registration).
# Population: same dual-pattern as Anthropic (placeholder in dev; external
# CLI in prod).
###############################################################################

resource "aws_secretsmanager_secret" "google_oauth_client_secret" {
  name                    = local.secret_names.google_oauth_client_secret
  description             = "Google OAuth 2.0 client secret for Sales-Connections SSO (F-012 User Authentication)"
  kms_key_id              = local.kms_key_id_or_null
  recovery_window_in_days = var.recovery_window_in_days

  tags = merge(local.module_tags, {
    Name       = local.secret_names.google_oauth_client_secret
    SecretType = "ClientSecret"
    Service    = "GoogleOAuth"
  })
}

resource "aws_secretsmanager_secret_version" "google_oauth_client_secret" {
  count = var.write_placeholder_versions ? 1 : 0

  secret_id     = aws_secretsmanager_secret.google_oauth_client_secret.id
  secret_string = local.placeholder_value

  lifecycle {
    ignore_changes = [secret_string, version_stages]
  }
}

resource "aws_secretsmanager_secret_policy" "google_oauth_client_secret" {
  secret_arn = aws_secretsmanager_secret.google_oauth_client_secret.arn
  policy     = data.aws_iam_policy_document.standard_secret_access.json
}

###############################################################################
# 3. JWT_SIGNING_KEY (with optional Terraform-generated value)
#
# Used by: backend/app/services/auth.py (mint_session_jwt / verify_session_jwt).
# Algorithm: HS256 (symmetric).
#
# Population:
#   - var.generate_jwt_key = true (default): Terraform generates a 64-char
#     URL-safe random value via random_password and writes it as the initial
#     secret_version. Operator can rotate via subsequent put-secret-value.
#   - var.generate_jwt_key = false: Secret entry is created without a value
#     (or with the placeholder when var.write_placeholder_versions = true);
#     operator populates externally.
#
# The override_special argument is included to match the folder spec verbatim.
# Terraform's random_password resource only consults override_special when
# special = true; here special = false means the override has no effect
# (random_password emits only alphanumeric characters in that case). The
# combination still produces a URL-safe alphabet.
###############################################################################

resource "random_password" "jwt_signing_key" {
  count = var.generate_jwt_key ? 1 : 0

  length           = 64
  special          = false
  override_special = "-_"

  # Trigger regeneration only when the secret entry name changes (i.e.,
  # the secret entry is recreated). Without keepers, the value still stays
  # stable across plans (random_password caches in state by default), but
  # the explicit keepers make the regeneration trigger documented and
  # deterministic.
  keepers = {
    secret_name = local.secret_names.jwt_signing_key
  }
}

resource "aws_secretsmanager_secret" "jwt_signing_key" {
  name                    = local.secret_names.jwt_signing_key
  description             = "HS256 signing key for PyJWT session token mint/verify (F-012)"
  kms_key_id              = local.kms_key_id_or_null
  recovery_window_in_days = var.recovery_window_in_days

  tags = merge(local.module_tags, {
    Name       = local.secret_names.jwt_signing_key
    SecretType = "SigningKey"
    Service    = "PyJWT"
  })
}

resource "aws_secretsmanager_secret_version" "jwt_signing_key" {
  # Created when EITHER:
  #   - var.generate_jwt_key (Terraform writes the random_password result), OR
  #   - var.write_placeholder_versions (Terraform writes the placeholder).
  # When both are false, no version is written and the operator populates
  # externally.
  count = var.generate_jwt_key || var.write_placeholder_versions ? 1 : 0

  secret_id = aws_secretsmanager_secret.jwt_signing_key.id

  # Prefer the random_password-generated value when generation is enabled;
  # fall back to the placeholder. The [0] indexing is required because
  # random_password.jwt_signing_key is count-gated.
  secret_string = var.generate_jwt_key ? random_password.jwt_signing_key[0].result : local.placeholder_value

  lifecycle {
    ignore_changes = [secret_string, version_stages]
  }
}

resource "aws_secretsmanager_secret_policy" "jwt_signing_key" {
  secret_arn = aws_secretsmanager_secret.jwt_signing_key.arn
  policy     = data.aws_iam_policy_document.standard_secret_access.json
}

###############################################################################
# 4. DB_PASSWORD (with optional Terraform-generated value and optional
# rotation hook)
#
# Used by:
#   - modules/database/ (master_password at RDS provision time, via
#     data.aws_secretsmanager_secret_version)
#   - backend/app/config.py (DATABASE_URL composition at task launch)
#
# Population:
#   - var.generate_db_password = true (default for dev): Terraform generates
#     a 32-char password with PostgreSQL-safe special chars.
#   - var.generate_db_password = false: Operator populates externally
#     (typical when var.enable_db_password_rotation = true and the
#     rotation Lambda owns the value).
#
# Rotation:
#   - var.enable_db_password_rotation = false (default; out of scope for
#     MVP per AAP Sec 0.4.9): no rotation configured.
#   - var.enable_db_password_rotation = true: rotation_lambda_arn must be
#     supplied; aws_secretsmanager_secret_rotation hooks the Lambda with
#     a 30-day cadence.
#
# Special characters MUST exclude PostgreSQL-unsafe chars (e.g., '/', '@',
# '"', spaces) which would break the connection string format.
###############################################################################

resource "random_password" "db_password" {
  count = var.generate_db_password ? 1 : 0

  length  = 32
  special = true

  # PostgreSQL-safe special characters per the folder spec critical
  # requirements. Excluded: / @ " ' \ space (each would conflict with
  # connection-string parsing or shell escaping in DATABASE_URL).
  override_special = "!#$%&*+-=?_~"

  keepers = {
    secret_name = local.secret_names.db_password
  }
}

resource "aws_secretsmanager_secret" "db_password" {
  name                    = local.secret_names.db_password
  description             = "RDS PostgreSQL master password for Sales-Connections application database"
  kms_key_id              = local.kms_key_id_or_null
  recovery_window_in_days = var.recovery_window_in_days

  tags = merge(local.module_tags, {
    Name       = local.secret_names.db_password
    SecretType = "DatabasePassword"
    Service    = "RDS"
  })
}

resource "aws_secretsmanager_secret_version" "db_password" {
  # Created when EITHER:
  #   - var.generate_db_password (Terraform writes the random_password result), OR
  #   - var.write_placeholder_versions (Terraform writes the placeholder).
  count = var.generate_db_password || var.write_placeholder_versions ? 1 : 0

  secret_id     = aws_secretsmanager_secret.db_password.id
  secret_string = var.generate_db_password ? random_password.db_password[0].result : local.placeholder_value

  lifecycle {
    ignore_changes = [secret_string, version_stages]
  }
}

resource "aws_secretsmanager_secret_policy" "db_password" {
  secret_arn = aws_secretsmanager_secret.db_password.arn

  # Uses the db-password-specific policy document, which adds the rotation
  # Lambda principal when rotation is enabled. See data.tf for details.
  policy = data.aws_iam_policy_document.db_password_access.json
}

###############################################################################
# Optional rotation: hooks an externally-provisioned Lambda function to
# rotate the DB password every 30 days. The Lambda is OUT OF SCOPE for MVP
# (per AAP Sec 0.4.9); when enabled, the operator supplies its ARN via
# var.rotation_lambda_arn.
###############################################################################

resource "aws_secretsmanager_secret_rotation" "db_password" {
  count = var.enable_db_password_rotation ? 1 : 0

  secret_id           = aws_secretsmanager_secret.db_password.id
  rotation_lambda_arn = var.rotation_lambda_arn

  rotation_rules {
    automatically_after_days = 30
  }

  # The rotation Lambda must have permission to call PutSecretValue on the
  # secret. That permission is granted by data.aws_iam_policy_document
  # .db_password_access in data.tf when rotation is enabled. The explicit
  # depends_on ensures the resource policy applies BEFORE the rotation hook
  # activates - otherwise the first rotation attempt would fail with
  # AccessDenied.
  depends_on = [aws_secretsmanager_secret_policy.db_password]
}

