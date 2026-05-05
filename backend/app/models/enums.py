"""Python enums mirroring the PostgreSQL enum types used by the application.

Each Python enum here is bound 1:1 to a PostgreSQL enum type created by
the initial Alembic migration
(``backend/migrations/versions/0001_initial_schema.py``). The Python
enum **values** must match the PostgreSQL enum values EXACTLY
(case-sensitive, including spaces).

The SQLAlchemy ``Enum`` column type binds Python enums to PostgreSQL
enum types via the ``name`` and ``values_callable`` parameters; see
``app.models.record``, ``app.models.user``, and ``app.models.audit_event``
for the binding sites.

Mappings::

    InvolvementType  -> postgres enum 'involvement_type'  (F-003)
    OutreachStatus   -> postgres enum 'outreach_status'   (F-005)
    UserRole         -> postgres enum 'user_role'         (F-009)
    AuditEventType   -> postgres enum 'audit_event_type'  (F-013)

Why ``str, Enum`` mixin (not plain ``Enum``):

    * Each enum member IS a string
      (``InvolvementType.WARM_INTRO == "Warm Intro"`` is True), which
      lets pydantic accept the raw string from API requests and coerce
      it to the enum cleanly without custom validators.
    * JSON serialization Just Works:
      ``json.dumps(InvolvementType.WARM_INTRO.value)`` produces
      ``"Warm Intro"``, no custom encoder needed.
    * Comparison with bare strings is supported, easing test
      assertions and database round-trip equality checks.
    * SQLAlchemy ``Enum(..., values_callable=lambda c: [m.value for m in c])``
      sends the human-friendly value string to PostgreSQL, NOT the
      Python member name.

Naming convention:

    * Python member name: ``UPPER_SNAKE_CASE`` (Python convention,
      enforced by ruff rule ``N801``/``N815``).
    * Python member value: matches the EXACT PostgreSQL enum value
      (which may include spaces and mixed case for human-friendly
      enums like ``Warm Intro``, or lowercase snake_case for machine
      enums like ``status_change``).

Stability contract:
    Renaming any value (e.g., from ``"Warm Intro"`` to ``"Warm-Intro"``)
    would silently invalidate every existing PostgreSQL row whose
    column stores the old value. Any value change therefore requires a
    coordinated migration sequence: add new value to the PostgreSQL
    enum, migrate row data, remove old value, update Python enum. Per
    the AAP Explainability rule, such a change requires a decision-log
    entry.

This module deliberately has no module-level side effects. Importing
``app.models.enums`` does not log, does not make HTTP calls, does not
touch the filesystem, and does not connect to any database. Only the
four enum class definitions and ``__all__`` are evaluated.
"""

from __future__ import annotations

from enum import Enum

__all__ = [
    "AuditEventType",
    "InvolvementType",
    "OutreachStatus",
    "UserRole",
]


class InvolvementType(str, Enum):
    """How the submitter wishes to participate in outreach (F-003).

    Three states with semantic meaning:

    * ``WARM_INTRO`` (``"Warm Intro"``)
        Submitter will personally introduce the contributor to the
        target. Highest-touch involvement; sales rep typically schedules
        a three-way introduction call or email thread.
    * ``SOFT_REFERENCE`` (``"Soft Reference"``)
        Submitter is comfortable being named or referenced when the
        sales rep reaches out. Mid-touch involvement; the submitter is
        endorsing the connection but is not orchestrating the
        introduction directly.
    * ``TARGET_ONLY`` (``"Target Only"``)
        Submitter is only flagging the target as worth pursuing. The
        submitter does not want to be referenced personally; the sales
        team owns all outreach.

    The PostgreSQL enum type ``involvement_type`` is created by the
    initial migration with these exact values.
    """

    WARM_INTRO = "Warm Intro"
    SOFT_REFERENCE = "Soft Reference"
    TARGET_ONLY = "Target Only"


class OutreachStatus(str, Enum):
    """Sales-team progression state for a connection record (F-005).

    Four-state progression with the canonical happy-path order::

        Not Started -> In Progress -> Contacted -> Closed

    Status mutation requires Sales Rep (``UserRole.VIEWER``) or Admin
    role per the AAP RBAC matrix (Layer 4 of section 0.5.2). The
    original submitter of a record cannot mutate this field unless they
    also hold one of those two roles, in order to preserve sales-team
    accountability.

    The default value on record creation is ``NOT_STARTED`` (set by the
    PostgreSQL ``DEFAULT 'Not Started'`` clause on the
    ``records.outreach_status`` column).

    The PostgreSQL enum type ``outreach_status`` is created by the
    initial migration with these exact values.
    """

    NOT_STARTED = "Not Started"
    IN_PROGRESS = "In Progress"
    CONTACTED = "Contacted"
    CLOSED = "Closed"


class UserRole(str, Enum):
    """Authorization role assigned to each user (F-009).

    Three roles whose permission matrix is documented in section 6.2.6
    of the technical specification:

    * ``ADMIN``
        Full platform control. Can hard-delete records, mutate any
        user's role, access the admin panel, and edit any record
        regardless of ownership.
    * ``CONTRIBUTOR``
        Default role for newly upserted users. Can create records and
        edit their own records. Cannot mutate outreach status, cannot
        hard-delete, cannot access the admin panel, cannot edit other
        users' records.
    * ``VIEWER``
        Sales-rep role. Can browse all records in the organization and
        mutate the outreach status on any record. Cannot edit records
        they do not own (only status), cannot hard-delete, cannot
        access the admin panel.

    The default for newly upserted OAuth users is ``CONTRIBUTOR`` (per
    AAP section 0.7.6); admins promote users explicitly via the admin
    panel.

    The PostgreSQL enum type ``user_role`` is created by the initial
    migration with these exact values.
    """

    ADMIN = "Admin"
    CONTRIBUTOR = "Contributor"
    VIEWER = "Viewer"


class AuditEventType(str, Enum):
    """Eight event types tracked in the append-only audit_events table (F-013).

    Categories of events:

    Record state changes:

    * ``CREATE`` (``"create"``)
        A record was created. Emitted by ``POST /api/connections``.
    * ``STATUS_CHANGE`` (``"status_change"``)
        Outreach status was mutated. Emitted by
        ``PATCH /api/connections/:id/status``. The ``before_payload``
        and ``after_payload`` capture the prior and new status values.
    * ``EDIT`` (``"edit"``)
        Any non-status field was edited (name, LinkedIn URL, company,
        title, relationship context, AI notes, involvement type, or
        tags). Emitted by ``PATCH /api/connections/:id``.
    * ``SOFT_DELETE`` (``"soft_delete"``)
        Record was soft-deleted (``deleted_at`` set to ``NOW()``).
        Emitted by ``DELETE /api/connections/:id``.
    * ``HARD_DELETE`` (``"hard_delete"``)
        Record was hard-deleted (row physically removed). Restricted
        to ``UserRole.ADMIN``. Emitted by
        ``DELETE /api/admin/records/:id``.

    User and governance events:

    * ``ROLE_CHANGE`` (``"role_change"``)
        A user's role was updated by an admin. Emitted by
        ``PATCH /api/admin/users/:id``.
    * ``AUTHENTICATION`` (``"authentication"``)
        Login or logout event. The ``target_record_id`` column is NULL
        for these events because the audit row is keyed to the user,
        not to a record.
    * ``ADMIN_OP`` (``"admin_op"``)
        Catch-all for admin operations not covered above (e.g., bulk
        moderation, configuration changes). Emitted by various
        ``/api/admin/*`` endpoints.

    Each value is the canonical lowercase snake_case identifier stored
    in PostgreSQL; the Python member NAME is UPPER_SNAKE_CASE per
    Python convention. The two diverge intentionally because audit
    event types are machine-friendly identifiers, in contrast to the
    human-friendly mixed-case values used by ``InvolvementType``,
    ``OutreachStatus``, and ``UserRole``.

    These eight types together cover every state-changing operation in
    the platform per AAP section 0.7.1 invariant 6 ("every state change
    emits an audit event").

    The PostgreSQL enum type ``audit_event_type`` is created by the
    initial migration with these exact values.
    """

    CREATE = "create"
    STATUS_CHANGE = "status_change"
    EDIT = "edit"
    SOFT_DELETE = "soft_delete"
    HARD_DELETE = "hard_delete"
    ROLE_CHANGE = "role_change"
    AUTHENTICATION = "authentication"
    ADMIN_OP = "admin_op"
