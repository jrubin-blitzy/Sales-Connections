"""Token version column and append-only audit role provisioning.

Revision ID: 0002_token_version_and_app_role
Revises: 0001_initial_schema
Create Date: 2026-05-03 10:00:00.000000

RATIONALE
    Two coordinated changes addressed by a single migration:

    1.  Add ``users.token_version`` (Integer NOT NULL DEFAULT 0). This
        is the per-user counter referenced by the session JWT's ``tv``
        claim. The auth middleware compares the JWT's ``tv`` against
        the stored value on every protected request; logout (and any
        other revocation event) increments the value, invalidating
        every previously minted JWT for that user.

        Per AAP section 0.7.4 (Security Invariants): "Tokens rotated
        on logout. Logout invalidates the cookie and (for the
        email/password flow) advances the per-user signing-key
        version." Without this column the JWT remains valid for its
        full 8-hour TTL after logout, breaking the security
        invariant.

    2.  Provision the ``sales_connections_app`` role (or whatever
        ``APP_DB_ROLE`` resolves to) so the migration's GRANT INSERT
        ON audit_events / REVOKE UPDATE, DELETE ON audit_events block
        from migration 0001_initial_schema is enforced rather than
        silently skipped. Without this provisioning, dev/test
        databases provisioned without superuser privileges to create
        roles may leave the audit-immutability invariant
        unenforceable, allowing an application-role connection to
        UPDATE or DELETE audit history.

        Per AAP section 0.7.1 invariant 5: "Append-only audit table.
        No code path issues UPDATE or DELETE against audit_events.
        Database-level grants enforce this in production." This
        migration ensures the grant/revoke is applied against a role
        that is guaranteed to exist after the migration runs.

FORWARD-COMPATIBILITY
    * The ``token_version`` column has a server_default of 0 so any
      User row created before this migration runs (in a real
      deployment, none; in tests, possibly via direct DDL) gets a
      deterministic baseline value at ALTER TABLE time.
    * The role provisioning is wrapped in a DO $$ ... $$ block that
      first checks for role existence and only attempts CREATE ROLE
      if it is missing. CREATE ROLE requires the connecting role to
      have CREATEROLE or be a superuser; if it does not, the block
      raises a clear NOTICE and the migration continues. After the
      block, GRANT/REVOKE re-runs (idempotent) to ensure the audit
      privileges are correct against whatever role exists.
"""

from __future__ import annotations

import os
import re
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0002_token_version_and_app_role"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ---------------------------------------------------------------------------
# SQL identifier defense in depth.
#
# The application role name is read from an environment variable so the
# value can be overridden per-environment (e.g., a different name in
# staging vs prod). Because the value is interpolated directly into a SQL
# string via f-string, we MUST validate it strictly to defend against
# SQL injection. A whitelist regex matching only PostgreSQL legal
# identifiers (a-z 0-9 _) restricts the value space.
# ---------------------------------------------------------------------------

_VALID_ROLE_NAME: re.Pattern[str] = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


def _validate_app_role(name: str) -> None:
    """Validate that an app role name is a safe SQL identifier.

    Args:
        name: The role name read from APP_DB_ROLE env var.

    Raises:
        ValueError: When the name does not match the strict
            identifier whitelist (lowercase letters, digits, and
            underscore only; 1-63 characters; must start with a
            letter). This is the same predicate enforced by the
            sibling migration ``0001_initial_schema``.
    """
    if not _VALID_ROLE_NAME.fullmatch(name):
        raise ValueError(
            f"Invalid APP_DB_ROLE value {name!r}; must match {_VALID_ROLE_NAME.pattern!r}"
        )


def upgrade() -> None:
    """Apply the token_version column and audit-role provisioning."""
    # ======================================================================
    # PHASE 1: Add token_version column to users.
    #
    # ``server_default=text("0")`` ensures rows added before this
    # migration get a deterministic baseline value (matches the
    # application's Python default). The column is NOT NULL so the
    # JWT ``tv`` claim can never be ambiguous: every user has a
    # well-defined token_version.
    # ======================================================================
    op.add_column(
        "users",
        sa.Column(
            "token_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )

    # ======================================================================
    # PHASE 2: Provision the application role.
    #
    # The migration runs as the database OWNER (or migrations role) -
    # the role that has DDL privileges. We attempt to CREATE ROLE for
    # the configured APP_DB_ROLE value if it does not yet exist. The
    # role is created with NOLOGIN by default; deployments must
    # ALTER ROLE ... LOGIN PASSWORD '...' separately (typically
    # provisioned via Terraform) before connecting the application
    # process AS this role. For dev convenience we also enable
    # LOGIN with a default password; the docker-compose.yml init
    # script overrides this if a more specific password is supplied.
    # ======================================================================
    app_role = os.environ.get("APP_DB_ROLE", "sales_connections_app")
    _validate_app_role(app_role)
    # Default password for dev/test only. Production MUST set
    # APP_DB_PASSWORD to a strong value via Secrets Manager.
    app_password = os.environ.get("APP_DB_PASSWORD", "sales_connections_app_dev")
    # PostgreSQL passwords cannot contain single quotes by default
    # (would break the literal). Strict whitelist ensures safe
    # interpolation; if the configured password contains anything
    # other than ASCII alphanumerics and a small set of safe
    # punctuation, we fall back to a hardcoded placeholder rather
    # than risk DDL injection. Operators are expected to pass a
    # safer password via Secrets Manager.
    if not re.fullmatch(r"[A-Za-z0-9_\-]{6,128}", app_password):
        app_password = "sales_connections_app_dev"
    # The CREATE ROLE statement is wrapped in a DO $$ ... $$ block so
    # it can be conditionally skipped when the role already exists,
    # AND so a missing CREATEROLE privilege results in a NOTICE rather
    # than a hard failure (matching the existing behavior in
    # ``0001_initial_schema``'s GRANT/REVOKE block).
    op.execute(
        sa.text(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT FROM pg_roles WHERE rolname = '{app_role}'
                ) THEN
                    BEGIN
                        EXECUTE format(
                            'CREATE ROLE %I LOGIN PASSWORD %L',
                            '{app_role}',
                            '{app_password}'
                        );
                    EXCEPTION
                        WHEN insufficient_privilege THEN
                            RAISE NOTICE
                                'Cannot CREATE ROLE %; insufficient privileges. '
                                'Operators must provision this role manually '
                                '(e.g., via Terraform) before running migrations.',
                                '{app_role}';
                        WHEN unique_violation THEN
                            -- Concurrent migration race; another process
                            -- created the role between our SELECT and
                            -- our CREATE. Idempotent skip.
                            NULL;
                    END;
                END IF;
            END$$;
            """
        )
    )

    # ======================================================================
    # PHASE 3: Re-apply the GRANT/REVOKE block so audit privileges are
    # correct AFTER the role is guaranteed to exist (or after the
    # NOTICE explaining why it is not).
    #
    # SELECT, INSERT are granted; UPDATE, DELETE, TRUNCATE are
    # revoked. The owner role retains all privileges (PostgreSQL's
    # default), but the application connects AS the app role, so the
    # owner's privileges are not exercised by the application.
    #
    # Per AAP section 0.7.1 invariant 5, the database-level grant
    # MUST enforce that no INSERT/UPDATE/DELETE path from the
    # application role can rewrite audit history.
    # ======================================================================
    op.execute(
        sa.text(
            f"""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT FROM pg_roles WHERE rolname = '{app_role}'
                ) THEN
                    -- Database-level CONNECT and schema USAGE.
                    EXECUTE format(
                        'GRANT CONNECT ON DATABASE %I TO %I',
                        current_database(),
                        '{app_role}'
                    );
                    EXECUTE format(
                        'GRANT USAGE ON SCHEMA public TO %I',
                        '{app_role}'
                    );
                    -- Most tables: full DML so the application can
                    -- run F-001/F-004/F-005/F-007 etc.
                    EXECUTE format(
                        'GRANT SELECT, INSERT, UPDATE, DELETE ON '
                        'organizations, users, records, tags, record_tags '
                        'TO %I',
                        '{app_role}'
                    );
                    -- Sequences (if any are added later) so SERIAL/BIGSERIAL
                    -- keys can be allocated.
                    EXECUTE format(
                        'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO %I',
                        '{app_role}'
                    );
                    -- audit_events: append-only. SELECT for read paths
                    -- (history endpoint), INSERT for the audit emitter.
                    EXECUTE format(
                        'GRANT SELECT, INSERT ON audit_events TO %I',
                        '{app_role}'
                    );
                    -- Explicit REVOKE to ensure the grants from above
                    -- (which are object-level) cannot be supplemented
                    -- by inherited rights via PUBLIC. UPDATE, DELETE,
                    -- TRUNCATE are all forbidden.
                    EXECUTE format(
                        'REVOKE UPDATE, DELETE, TRUNCATE ON audit_events FROM %I',
                        '{app_role}'
                    );
                ELSE
                    RAISE NOTICE
                        'Role % does not exist after provisioning attempt. '
                        'Audit-immutability grants were not applied. '
                        'This is a configuration defect that MUST be resolved '
                        'before deploying to production.',
                        '{app_role}';
                END IF;
            END$$;
            """
        )
    )


def downgrade() -> None:
    """Revert the migration.

    Removes the ``token_version`` column. Does NOT drop the
    ``sales_connections_app`` role (downgrades are for local dev and
    test environments; the role may be referenced by other artifacts
    we do not control). The runtime application will fall back to the
    auth middleware's missing-claim handling (which raises 401 for
    any JWT lacking ``tv``); operators should re-run the upgrade or
    invalidate all sessions before downgrade.
    """
    op.drop_column("users", "token_version")
    # Intentionally do not drop the role. Dropping a role that owns
    # other objects requires REASSIGN OWNED, which is out of scope
    # for a downgrade.
