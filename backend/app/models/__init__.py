"""SQLAlchemy declarative model re-exports for the Sales-Connections backend.

Importing this package causes every declarative model in the application to
be registered on ``app.extensions.Base.metadata``. The application factory
in ``app.__init__.create_app()`` and the Alembic environment in
``backend/migrations/env.py`` both import this package as a side-effect to
ensure metadata is fully populated before they query for the table list.

Public re-exports allow handlers, services, schemas, and tests to write::

    from app.models import (
        AuditEvent,
        AuditEventType,
        InvolvementType,
        Organization,
        OutreachStatus,
        Record,
        RecordTag,
        Tag,
        User,
        UserRole,
    )

Per AAP Section 0.5.3, this package contains ONLY declarations. Behavior
lives in ``app.services.*``. The re-exports here perform the side-effect
of registering tables on ``Base.metadata`` (a SQLAlchemy declarative-system
behavior triggered by class definition, not by this module's code) and
provide a stable single import point for downstream consumers.

Why re-exports matter for Alembic autogenerate:
    Alembic's autogenerate works by comparing ``Base.metadata`` (the
    SQLAlchemy-known schema) against the live PostgreSQL schema. If a model
    file is never imported, its tables are absent from ``Base.metadata`` and
    autogenerate produces a migration that DROPS them. The re-exports below
    prevent that disaster by ensuring every model module is imported (and
    therefore every model class is registered) before metadata is queried.

This module deliberately has no module-level side effects beyond model
class registration. Importing ``app.models`` does not log, does not make
HTTP calls, does not touch the filesystem, and does not connect to any
database.
"""

from __future__ import annotations

from app.models.audit_event import AuditEvent
from app.models.enums import (
    AuditEventType,
    InvolvementType,
    OutreachStatus,
    UserRole,
)
from app.models.organization import Organization
from app.models.record import Record
from app.models.tag import RecordTag, Tag
from app.models.user import User

__all__ = [
    "AuditEvent",
    "AuditEventType",
    "InvolvementType",
    "Organization",
    "OutreachStatus",
    "Record",
    "RecordTag",
    "Tag",
    "User",
    "UserRole",
]
