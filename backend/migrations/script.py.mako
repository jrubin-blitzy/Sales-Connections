"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | n,unicode}
Create Date: ${create_date}

RATIONALE
    [Replace this section with a 2-4 sentence explanation of WHY this
    change is being made. Reference the AAP feature ID(s) (F-001 through
    F-014) when applicable. This explanation supplements the
    docs/decision-log.md row for any non-trivial migration.]

FORWARD-COMPATIBILITY (per AAP Sec 0.7.7)
    Alembic migrations in this project MUST be forward-compatible:
        * NEVER drop a column in the same migration that introduces a
          replacement. Add the new column, deploy, backfill, then drop in
          a separate later migration.
        * NEVER alter a column type destructively (e.g., VARCHAR(255) ->
          VARCHAR(64) without first verifying no row exceeds the new
          width).
        * Database privilege grants on audit_events (INSERT-only for the
          application role) are restored at the end of every migration
          if any DDL changed those grants.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

# revision identifiers, used by Alembic.
revision: str = ${repr(up_revision)}
down_revision: Union[str, None] = ${repr(down_revision)}
branch_labels: Union[str, Sequence[str], None] = ${repr(branch_labels)}
depends_on: Union[str, Sequence[str], None] = ${repr(depends_on)}


def upgrade() -> None:
    """Apply the schema/data changes for this migration.

    Conventions:
        * Use op.create_table, op.add_column, op.create_index, and friends
          for DDL.
        * For raw SQL (e.g., GRANT/REVOKE statements), use op.execute.
        * Keep DDL and DML in this single function; do NOT split between
          two migrations unless ordering requires it.
        * If introducing a non-null column with a default, use the
          server_default parameter rather than a separate UPDATE statement
          to avoid table-rewrite locks at scale.
    """
    # ======================================================================
    # AUTO-GENERATED CONTENT BELOW THIS LINE - REVIEW BEFORE COMMIT
    # ======================================================================
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """Revert the changes applied by upgrade().

    Conventions:
        * Reverse the order of operations in upgrade().
        * Drop indexes BEFORE dropping the columns or tables they reference.
        * Drop foreign keys BEFORE dropping the tables they reference.
        * For destructive downgrades, document the data-loss risk in the
          docstring above.

    NOTE (per AAP Sec 0.7.7):
        Forward-compatibility means production migrations are not commonly
        rolled back via downgrade. Downgrade exists primarily for the local
        dev / test environments. Production rollbacks typically use
        point-in-time recovery from RDS snapshots.
    """
    # ======================================================================
    # AUTO-GENERATED CONTENT BELOW THIS LINE - REVIEW BEFORE COMMIT
    # ======================================================================
    ${downgrades if downgrades else "pass"}
