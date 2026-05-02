"""initial_schema

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-01-01 00:00:00.000000

RATIONALE
    This is the foundational schema migration for the Sales-Connections
    platform. It creates every database object the application needs to
    function:
        * Four PostgreSQL enum types (involvement_type, outreach_status,
          audit_event_type, user_role)
        * Six tables (organizations, users, records, tags, record_tags,
          audit_events) in dependency order
        * Indexes for fast feed queries (F-004), filter dimensions, and
          duplicate detection (F-010)
        * The append-only audit invariant (F-013) via PostgreSQL GRANT/
          REVOKE on audit_events
        * Single default organization seed (per AAP Sec 0.7.2 single-org
          MVP runtime)

    All objects are created in a single transaction; if any step fails,
    the entire migration rolls back and the database is left in its
    pre-migration state. PostgreSQL 17.7 supports transactional DDL,
    making this safe.

    Touches features F-001 (Form), F-003 (Involvement), F-005 (Status),
    F-006 (Owner Attribution), F-007 (Soft Delete), F-008 (Tags),
    F-009 (RBAC), F-010 (Duplicate Detection), F-011 (Detail/History),
    F-012 (Auth users), and F-013 (Audit Trail).

FORWARD-COMPATIBILITY (per AAP Sec 0.7.7)
    Future migrations under backend/migrations/versions/ MUST:
        * NEVER drop a column in the same migration that introduces a
          replacement.
        * NEVER alter a column type destructively.
        * Set down_revision = "0001_initial_schema" (or the previous
          migration's revision ID).
"""

from __future__ import annotations

import os
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# ---------------------------------------------------------------------------
# Module-level enum value lists.
#
# These tuples define the literal values for each of the four PostgreSQL
# enum types created by this migration. They MUST match the values
# declared in ``app.models.enums`` character-for-character (case-sensitive,
# including spaces). Any drift between the Python enums and these
# literals breaks ORM round-trips at runtime.
#
# These values are deliberately HARDCODED here rather than imported from
# app.models.enums. Migrations are FROZEN snapshots of the database
# schema as it existed at a particular point in history; importing
# from app.* would couple this file to current Python code and break
# historical reproducibility (a future enum value rename would silently
# alter what THIS migration creates when applied to a fresh database
# years from now).
# ---------------------------------------------------------------------------

INVOLVEMENT_TYPE_VALUES: tuple[str, ...] = (
    "Warm Intro",
    "Soft Reference",
    "Target Only",
)
OUTREACH_STATUS_VALUES: tuple[str, ...] = (
    "Not Started",
    "In Progress",
    "Contacted",
    "Closed",
)
USER_ROLE_VALUES: tuple[str, ...] = (
    "Admin",
    "Contributor",
    "Viewer",
)
AUDIT_EVENT_TYPE_VALUES: tuple[str, ...] = (
    "create",
    "status_change",
    "edit",
    "soft_delete",
    "hard_delete",
    "role_change",
    "authentication",
    "admin_op",
)


def _validate_app_role(app_role: str) -> None:
    """Validate the application database role name is a safe SQL identifier.

    PostgreSQL does NOT support bound parameters in DDL identifier
    positions (role names, schema names, table names, etc.), which means
    the role name must be string-interpolated into the GRANT/REVOKE
    statements. To prevent SQL injection if the value originates from a
    less-trusted source (e.g., a misconfigured env var), this helper
    restricts the role name to the PostgreSQL identifier grammar:
    starts with a letter, contains only letters, digits, and
    underscores.
    """
    if not app_role:
        raise ValueError("APP_DB_ROLE must not be empty")
    if not app_role[0].isalpha():
        raise ValueError(f"APP_DB_ROLE must start with a letter; got {app_role!r}")
    if not app_role.replace("_", "").isalnum():
        raise ValueError(
            f"APP_DB_ROLE must be a valid PostgreSQL identifier "
            f"(letters, digits, underscores only); got {app_role!r}"
        )


def upgrade() -> None:
    """Apply the schema/data changes for this migration.

    Creates four PostgreSQL enum types, six tables in dependency order, all
    indexes (composite, single-column, and partial unique), seeds the default
    Organization row, and applies the append-only invariant on audit_events
    via GRANT/REVOKE.
    """
    # ======================================================================
    # PHASE A: Create the four PostgreSQL enum types.
    # Order is independent (none reference each other), but they must exist
    # before any table that references them.
    # ======================================================================

    involvement_type = postgresql.ENUM(
        *INVOLVEMENT_TYPE_VALUES,
        name="involvement_type",
        create_type=False,
    )
    outreach_status = postgresql.ENUM(
        *OUTREACH_STATUS_VALUES,
        name="outreach_status",
        create_type=False,
    )
    user_role = postgresql.ENUM(
        *USER_ROLE_VALUES,
        name="user_role",
        create_type=False,
    )
    audit_event_type = postgresql.ENUM(
        *AUDIT_EVENT_TYPE_VALUES,
        name="audit_event_type",
        create_type=False,
    )

    # Create the type objects in PostgreSQL. The ``create_type=False``
    # parameter on each ``postgresql.ENUM(...)`` instance tells SQLAlchemy
    # NOT to auto-create the type when the column referencing it is
    # declared on a Table (which would happen multiple times across the
    # users / records / audit_events tables, causing "type already exists"
    # errors on the second reference). We explicitly create each type
    # exactly once here so ordering is deterministic.
    bind = op.get_bind()
    involvement_type.create(bind, checkfirst=False)
    outreach_status.create(bind, checkfirst=False)
    user_role.create(bind, checkfirst=False)
    audit_event_type.create(bind, checkfirst=False)

    # ======================================================================
    # PHASE B: Create six tables in dependency order.
    #
    # Order:
    #   1. organizations  (root of multi-tenant scope; no FKs to app tables)
    #   2. users          (FK -> organizations)
    #   3. records        (FK -> organizations, FK -> users)
    #   4. tags           (FK -> organizations)
    #   5. record_tags    (FK -> records, FK -> tags)
    #   6. audit_events   (FK -> users, FK -> records)
    # ======================================================================

    # ----------------------------------------------------------------------
    # Table 1 of 6: organizations
    # Root of the multi-tenant scope. Every other table carries an
    # ``org_id`` column whose foreign key resolves here.
    # ----------------------------------------------------------------------
    op.create_table(
        "organizations",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    # ----------------------------------------------------------------------
    # Table 2 of 6: users
    # Carries authentication credentials, role assignment, and the
    # denormalized display name used by the records.owner_display_name
    # column. Email uniqueness is scoped to the organization (a single
    # email may exist in multiple orgs once multi-org runtime is enabled).
    # ----------------------------------------------------------------------
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        # RFC 5321 caps email at 320 chars (64 local + @ + 255 domain).
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        # Nullable: NULL means OAuth-only user (no email/password fallback).
        sa.Column("password_hash", sa.String(length=255), nullable=True),
        sa.Column(
            "role",
            postgresql.ENUM(
                *USER_ROLE_VALUES,
                name="user_role",
                create_type=False,
            ),
            nullable=False,
            server_default=sa.text("'Contributor'::user_role"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_users_org_id_organizations",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("org_id", "email", name="uq_users_org_email"),
    )
    # Single-column ix_users_org_id supports queries that filter on
    # org_id alone (e.g., admin user listing); the composite uniqueness
    # index uq_users_org_email satisfies (org_id, email) lookups but not
    # the bare org_id prefix-only case in some planner scenarios.
    op.create_index("ix_users_org_id", "users", ["org_id"])

    # ----------------------------------------------------------------------
    # Table 3 of 6: records
    # The central business entity. Carries all nine user-visible fields
    # plus org_id, owner_user_id, normalized_linkedin_url for duplicate
    # detection (F-010), deleted_at for soft delete (F-007), and the two
    # PostgreSQL enums for involvement (F-003) and outreach_status (F-005).
    # ----------------------------------------------------------------------
    op.create_table(
        "records",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        # Denormalized display name lets the feed render owner attribution
        # without joining users on every row at 10K-record scale.
        sa.Column("owner_display_name", sa.String(length=255), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        # 2048 is a defensive ceiling for LinkedIn profile URLs which are
        # typically <100 chars but may include tracking parameters.
        sa.Column("linkedin_url", sa.String(length=2048), nullable=False),
        sa.Column(
            "normalized_linkedin_url",
            sa.String(length=2048),
            nullable=False,
        ),
        sa.Column("company", sa.String(length=255), nullable=False),
        sa.Column("job_title", sa.String(length=255), nullable=False),
        sa.Column("relationship_context", sa.Text(), nullable=False),
        # Nullable: NULL means AI generation was skipped or failed (per
        # AAP Sec 0.7.6, AI failure does not block submission).
        sa.Column("ai_notes", sa.Text(), nullable=True),
        sa.Column(
            "involvement",
            postgresql.ENUM(
                *INVOLVEMENT_TYPE_VALUES,
                name="involvement_type",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "outreach_status",
            postgresql.ENUM(
                *OUTREACH_STATUS_VALUES,
                name="outreach_status",
                create_type=False,
            ),
            nullable=False,
            server_default=sa.text("'Not Started'::outreach_status"),
        ),
        sa.Column(
            "submission_date",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Nullable: NULL means active record. Non-null timestamp means
        # the record was soft-deleted at that time. All read paths inject
        # ``WHERE deleted_at IS NULL`` per AAP invariant 4.
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_records_org_id_organizations",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name="fk_records_owner_user_id_users",
            ondelete="RESTRICT",
        ),
    )

    # ----------------------------------------------------------------------
    # records indexes (the most performance-critical table in the system).
    # ----------------------------------------------------------------------
    # Single-column index supporting "records owned by user X" filters.
    op.create_index(
        "ix_records_owner_user_id",
        "records",
        ["owner_user_id"],
    )
    # Primary feed index (F-004). The composite key
    #   (org_id, deleted_at, submission_date DESC)
    # serves the canonical feed query
    #   WHERE org_id = :org AND deleted_at IS NULL
    #   ORDER BY submission_date DESC
    # and works equally well for prefix-only filters on (org_id) or
    # (org_id, deleted_at). Putting submission_date last lets the planner
    # scan the index in reverse for "ORDER BY submission_date DESC".
    op.create_index(
        "ix_records_org_deleted_submission",
        "records",
        ["org_id", "deleted_at", "submission_date"],
    )
    # Single-column indexes for involvement and outreach_status filter
    # dimensions on the feed.
    op.create_index("ix_records_involvement", "records", ["involvement"])
    op.create_index(
        "ix_records_outreach_status",
        "records",
        ["outreach_status"],
    )
    # CRITICAL partial unique index for F-010 duplicate detection.
    # The ``postgresql_where=`` clause restricts uniqueness to ACTIVE
    # (non-soft-deleted) records, which lets a contributor re-create a
    # record after a soft delete with the same URL. Without this clause
    # the uniqueness would apply to ALL rows including soft-deleted ones,
    # blocking the soft-delete-then-recreate flow described in F-007.
    op.create_index(
        "uq_records_org_normalized_linkedin_url_active",
        "records",
        ["org_id", "normalized_linkedin_url"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    # ----------------------------------------------------------------------
    # Table 4 of 6: tags
    # Org-scoped tag dictionary. Tag uniqueness is on (org_id, name);
    # a tag with the same name may exist independently in different orgs.
    # ----------------------------------------------------------------------
    op.create_table(
        "tags",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_tags_org_id_organizations",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("org_id", "name", name="uq_tags_org_name"),
    )
    op.create_index("ix_tags_org_id", "tags", ["org_id"])

    # ----------------------------------------------------------------------
    # Table 5 of 6: record_tags
    # Many-to-many association table between records and tags. The
    # composite (record_id, tag_id) primary key automatically creates an
    # index, so no explicit ix_* is required.
    #
    # Why CASCADE on both FKs: hard-deleting a record (admin-only) or
    # deleting a tag (admin housekeeping) should remove the association
    # rows cleanly; tag associations are pure metadata with no value
    # beyond their parent record/tag.
    # ----------------------------------------------------------------------
    op.create_table(
        "record_tags",
        sa.Column("record_id", sa.Uuid(), nullable=False),
        sa.Column("tag_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["record_id"],
            ["records.id"],
            name="fk_record_tags_record_id_records",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tag_id"],
            ["tags.id"],
            name="fk_record_tags_tag_id_tags",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "record_id",
            "tag_id",
            name="pk_record_tags",
        ),
    )

    # ----------------------------------------------------------------------
    # Table 6 of 6: audit_events
    # Append-only event log (F-013). Every state-changing operation in
    # the application emits exactly one row here in the same transaction
    # as the state change itself (atomic state-change + audit pair).
    #
    # target_record_id is nullable because authentication events have no
    # associated record (the audit row is keyed to the user, not a record).
    # ----------------------------------------------------------------------
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("target_record_id", sa.Uuid(), nullable=True),
        sa.Column(
            "event_type",
            postgresql.ENUM(
                *AUDIT_EVENT_TYPE_VALUES,
                name="audit_event_type",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "event_timestamp",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # JSONB (binary, indexable) over JSON (text). Audit payloads are
        # typically small ({"before": {...}, "after": {...}}) but JSONB's
        # indexing support is valuable for forensic queries even if MVP
        # does not exercise it.
        sa.Column(
            "before_payload",
            postgresql.JSONB(),
            nullable=True,
        ),
        sa.Column(
            "after_payload",
            postgresql.JSONB(),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name="fk_audit_events_actor_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_record_id"],
            ["records.id"],
            name="fk_audit_events_target_record_id_records",
            ondelete="RESTRICT",
        ),
    )
    # audit_events indexes
    op.create_index(
        "ix_audit_events_actor_user_id",
        "audit_events",
        ["actor_user_id"],
    )
    op.create_index(
        "ix_audit_events_event_type",
        "audit_events",
        ["event_type"],
    )
    # Composite index supporting the per-record edit-history query
    # (F-011): WHERE target_record_id = :id ORDER BY event_timestamp DESC.
    op.create_index(
        "ix_audit_events_target_record_event_timestamp",
        "audit_events",
        ["target_record_id", "event_timestamp"],
    )

    # ======================================================================
    # PHASE C: Seed the default organization (single-org MVP runtime).
    #
    # The UUID is read from the DEFAULT_ORG_ID environment variable,
    # defaulting to the well-known sentinel UUID
    # 00000000-0000-0000-0000-000000000001 documented in
    # backend/.env.example. Every record, user, tag, and audit event in
    # the MVP runtime is scoped to this organization.
    #
    # The ON CONFLICT (id) DO NOTHING clause makes the seed idempotent:
    # re-running this migration against an already-seeded database (e.g.,
    # during disaster-recovery rehearsal) does not raise a primary-key
    # violation.
    # ======================================================================
    default_org_id = os.environ.get(
        "DEFAULT_ORG_ID",
        "00000000-0000-0000-0000-000000000001",
    )
    # Explicit ::uuid cast on the org_id bind parameter is required
    # because psycopg sends string-typed parameters as VARCHAR unless
    # told otherwise; the organizations.id column is UUID, so the cast
    # is what makes the bind compatible. The org_name parameter is
    # naturally compatible with the VARCHAR column type.
    op.execute(
        sa.text(
            """
            INSERT INTO organizations (id, name, created_at)
            VALUES (CAST(:org_id AS uuid), :org_name, NOW())
            ON CONFLICT (id) DO NOTHING;
            """
        ).bindparams(
            org_id=default_org_id,
            org_name="Sales-Connections Default Org",
        )
    )

    # ======================================================================
    # PHASE D: Apply the append-only invariant on audit_events.
    #
    # The application database role (default: ``sales_connections_app``,
    # configurable via APP_DB_ROLE) is granted SELECT and INSERT on
    # audit_events; UPDATE and DELETE are revoked. The elevated
    # migrations role (which executes this migration) retains full
    # privileges for governance and recovery.
    #
    # Robustness: if the application role does not yet exist (e.g., in a
    # fresh local dev database that has not yet provisioned the runtime
    # role), the migration emits a NOTICE and continues rather than
    # failing. Production must create this role before running migrations
    # (Terraform module infra/terraform/modules/database/main.tf provisions
    # both roles).
    # ======================================================================
    app_role = os.environ.get("APP_DB_ROLE", "sales_connections_app")
    _validate_app_role(app_role)
    op.execute(
        sa.text(
            f"""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT FROM pg_roles WHERE rolname = '{app_role}'
                ) THEN
                    GRANT SELECT, INSERT ON audit_events TO {app_role};
                    REVOKE UPDATE, DELETE ON audit_events FROM {app_role};
                ELSE
                    RAISE NOTICE
                        'Role % does not exist; skipping audit_events grants. '
                        'This is expected for fresh dev databases. Production '
                        'must create this role before running migrations.',
                        '{app_role}';
                END IF;
            END$$;
            """
        )
    )


def downgrade() -> None:
    """Revert the changes applied by upgrade().

    Drops indexes, tables, and enum types in strict reverse order of
    upgrade(); also revokes the audit_events privileges granted in
    PHASE D (these would be moot after the table is dropped, but the
    explicit revoke keeps the database catalog tidy and serves as
    documentation of the inverse operation).

    Per AAP Sec 0.7.7, downgrades exist primarily for local dev and
    test environments. Production rollback uses point-in-time recovery
    from RDS snapshots, not Alembic downgrade.
    """
    # ======================================================================
    # PHASE D-reverse: Revoke the audit_events grants if the role exists.
    # Wrapped in the same DO $$ block as the upgrade so a missing role
    # doesn't fail the downgrade.
    # ======================================================================
    app_role = os.environ.get("APP_DB_ROLE", "sales_connections_app")
    _validate_app_role(app_role)
    op.execute(
        sa.text(
            f"""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT FROM pg_roles WHERE rolname = '{app_role}'
                ) THEN
                    REVOKE SELECT, INSERT ON audit_events FROM {app_role};
                END IF;
            END$$;
            """
        )
    )

    # ======================================================================
    # PHASE C-reverse: No explicit DELETE for the seed organization; the
    # subsequent table drop removes it implicitly.
    # ======================================================================

    # ======================================================================
    # PHASE B-reverse: Drop indexes (especially the partial unique BEFORE
    # dropping the table), then drop tables in reverse dependency order.
    # ======================================================================

    # Drop audit_events indexes and table.
    op.drop_index(
        "ix_audit_events_target_record_event_timestamp",
        table_name="audit_events",
    )
    op.drop_index(
        "ix_audit_events_event_type",
        table_name="audit_events",
    )
    op.drop_index(
        "ix_audit_events_actor_user_id",
        table_name="audit_events",
    )
    op.drop_table("audit_events")

    # Drop record_tags table (no explicit indexes beyond the composite PK).
    op.drop_table("record_tags")

    # Drop tags indexes and table.
    op.drop_index("ix_tags_org_id", table_name="tags")
    op.drop_table("tags")

    # Drop records indexes (including the partial unique) and table.
    op.drop_index(
        "uq_records_org_normalized_linkedin_url_active",
        table_name="records",
    )
    op.drop_index("ix_records_outreach_status", table_name="records")
    op.drop_index("ix_records_involvement", table_name="records")
    op.drop_index(
        "ix_records_org_deleted_submission",
        table_name="records",
    )
    op.drop_index("ix_records_owner_user_id", table_name="records")
    op.drop_table("records")

    # Drop users indexes and table.
    op.drop_index("ix_users_org_id", table_name="users")
    op.drop_table("users")

    # Drop organizations table.
    op.drop_table("organizations")

    # ======================================================================
    # PHASE A-reverse: Drop the four PostgreSQL enum types.
    # Order is independent (none reference each other), but they must be
    # dropped AFTER all tables that reference them have been dropped above.
    # ======================================================================
    bind = op.get_bind()
    postgresql.ENUM(name="audit_event_type").drop(bind, checkfirst=False)
    postgresql.ENUM(name="user_role").drop(bind, checkfirst=False)
    postgresql.ENUM(name="outreach_status").drop(bind, checkfirst=False)
    postgresql.ENUM(name="involvement_type").drop(bind, checkfirst=False)
