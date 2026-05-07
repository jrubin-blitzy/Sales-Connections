"""Insert system user for anonymous (no-auth) access mode.

Revision ID: 0003_anon_access
Revises: 0002_token_version_and_app_role
Create Date: 2026-05-06 00:00:00.000000

RATIONALE
    The app now operates in anonymous-access mode: all /api/* endpoints
    are open without JWT authentication. A fixed "system" user row is
    inserted so that the records.owner_user_id FK constraint is satisfied
    for every anonymously-submitted connection. The system user belongs to
    the default organization (00000000-0000-0000-0000-000000000001) and
    carries the Admin role so service-layer ownership checks are bypassed.
"""

from alembic import op
import sqlalchemy as sa

revision = "0003_anon_access"
down_revision = "0002_token_version_and_app_role"
branch_labels = None
depends_on = None

_SYSTEM_USER_ID = "00000000-0000-0000-0000-000000000002"
_DEFAULT_ORG_ID = "00000000-0000-0000-0000-000000000001"


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            INSERT INTO users (id, org_id, email, display_name, role, password_hash, token_version, created_at)
            VALUES (
                CAST(:user_id AS uuid),
                CAST(:org_id AS uuid),
                :email,
                :display_name,
                :role,
                NULL,
                0,
                NOW()
            )
            ON CONFLICT (id) DO NOTHING;
            """
        ).bindparams(
            user_id=_SYSTEM_USER_ID,
            org_id=_DEFAULT_ORG_ID,
            email="system@internal",
            display_name="System",
            role="Admin",
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM users WHERE id = CAST(:user_id AS uuid)"
        ).bindparams(user_id=_SYSTEM_USER_ID)
    )
