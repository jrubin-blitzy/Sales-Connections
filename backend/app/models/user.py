"""SQLAlchemy 2.x declarative model for the users table.

``User`` is the actor identity for every authenticated request:

    F-006 Owner Attribution    owner_user_id on Record
    F-009 RBAC                 role enum (Admin/Contributor/Viewer)
    F-012 Authentication       password_hash for email/password,
                               NULL for OAuth-only users
    F-013 Audit Trail          actor on every AuditEvent

The ``email`` column is unique within an organization (composite unique
constraint with ``org_id``) so the same email may be used across
organizations once multi-tenant runtime is enabled. ``password_hash`` is
bcrypt-encoded (per AAP section 0.7.4); NULL means "OAuth-only user -
never authenticated via password".

Per AAP section 0.5.3, this module declares schema only. Authentication
logic (hashing, JWT mint/verify, OAuth upsert) lives in
:mod:`app.services.auth`. Per AAP section 0.7.1 invariant 7 ("API-layer
authorization is authoritative"), the ``role`` column on this model is
the single source of truth for RBAC decisions; the JWT claim containing
the role is derived from this column at session-mint time and is
re-validated against this column on every request that calls
:func:`app.services.auth.verify_session_jwt`.

The append-only audit invariant (AAP section 0.7.1 invariant 5) means
deleting a User is operationally fraught: the FK from
``audit_events.actor_user_id`` to ``users.id`` is
``ondelete="RESTRICT"``, so any attempt to drop a user with audit
history will fail at the database layer until the admin-panel
user-deletion flow (post-MVP) archives or redacts the historical rows
first. This model does NOT implement that flow; it merely declares the
schema that makes the constraint enforceable.

This module deliberately has no module-level side effects beyond class
registration on the SQLAlchemy mapper registry. Importing
``app.models.user`` does not log, does not make HTTP calls, does not
touch the filesystem, and does not connect to any database.
"""

from __future__ import annotations

# ``datetime`` and ``Mapped`` / ``mapped_column`` / ``relationship`` MUST
# be importable at module load time. SQLAlchemy 2.x's declarative system
# parses ``Mapped[...]`` annotations at class-construction time via
# ``de_stringify_annotation`` (see :mod:`sqlalchemy.util.typing`), which
# does ``eval()`` on the annotation string and looks up names in the
# originating module's ``__globals__``. Moving these names into a
# ``TYPE_CHECKING`` block would therefore raise
# ``MappedAnnotationError`` at import. The ``noqa`` suppressions below
# are necessary, not stylistic.
from datetime import datetime  # noqa: TC003
from typing import TYPE_CHECKING
import uuid

from sqlalchemy import (
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship  # noqa: TC002

from app.extensions import Base
from app.models.enums import UserRole

if TYPE_CHECKING:
    # Sibling-model imports are gated to type-check time only. The
    # SQLAlchemy declarative classes for ``Organization``, ``Record``,
    # and ``AuditEvent`` reference this class via
    # ``relationship(back_populates=...)`` and vice versa, which would
    # form a circular import graph at module load time if these were
    # unconditional imports. The string-based ``primaryjoin`` and
    # ``foreign_keys`` arguments on the :func:`relationship` calls
    # below are forward references that SQLAlchemy resolves lazily
    # during the registry's ``configure_mappers()`` pass once both
    # sides of every ``back_populates`` pair are loaded.
    from app.models.audit_event import AuditEvent
    from app.models.organization import Organization
    from app.models.record import Record


__all__ = ["User"]


class User(Base):
    """An authenticated user belonging to an organization.

    Authentication mechanisms (per F-012):

        * Google OAuth 2.0 - primary mechanism. Authlib upserts a row on
          first sign-in; ``password_hash`` remains NULL for these users
          and any subsequent password-login attempt rejects with 401.
        * Email/password fallback - bcrypt cost-12 hashes stored in
          ``password_hash``; ``app.services.auth.hash_password`` is the
          sole writer of this column.

    Authorization (per F-009): exactly one of three roles is assigned to
    every user, with the column NOT NULL and a Python-level default of
    :data:`UserRole.CONTRIBUTOR` so that direct INSERTs (e.g., test
    fixtures, ad-hoc scripts) cannot leave the column unset.

        * ``Admin``        Full platform control, including hard delete
                           and admin-panel access. Promoted explicitly
                           by another admin via the F-014 admin-panel
                           role-change flow; never the default.
        * ``Contributor``  Default role for newly upserted users. Can
                           create records and edit their own records.
                           Cannot mutate outreach status, cannot
                           hard-delete, cannot access the admin panel,
                           cannot edit other users' records.
        * ``Viewer``       Sales-rep role. Can browse all records in
                           the organization and mutate the outreach
                           status on any record. Cannot edit records
                           they do not own (only status), cannot
                           hard-delete, cannot access the admin panel.

    Owner attribution (per F-006): every Record has ``owner_user_id``
    pointing back to a User; that FK has ``ondelete=RESTRICT`` so
    accidental user deletion cannot orphan records. The reciprocal
    ``records`` collection on this model exposes the owner-side view
    of the relationship.

    Audit trail (per F-013): every AuditEvent has ``actor_user_id``
    pointing back to a User; that FK has ``ondelete=RESTRICT`` so a
    user with audit history cannot be hard-deleted without first
    archiving or redacting the historical rows. The reciprocal
    ``audit_events`` collection exposes the actor-side view; CRITICAL:
    no ``cascade`` is configured on either side because audit history
    must outlive the user record itself.

    Multi-tenancy (per AAP section 0.7.1 invariant 3): every user is
    scoped to exactly one organization via the ``org_id`` foreign key.
    The composite ``UniqueConstraint("org_id", "email")`` named
    ``uq_users_org_email`` allows the same email address to be used
    across different organizations once multi-tenant runtime is
    enabled, while preventing duplicate accounts within a single org.

    Attributes:
        id: Primary key. UUID v4 generated client-side via the
            ``default=uuid.uuid4`` factory when the caller does not
            supply a value (the typical case).
        org_id: Foreign key to ``organizations.id``. Non-null;
            ``ondelete="RESTRICT"`` because organization deletion is
            out of scope for MVP. Indexed via ``index=True`` for
            single-column ``org_id`` filters; the leading position of
            ``org_id`` in the composite ``uq_users_org_email``
            constraint also serves equality lookups, but the explicit
            secondary index makes the intent unambiguous and keeps
            join-plan stability across PostgreSQL versions.
        email: User-supplied email address. Length 320 matches RFC 5321
            (64 local-part + 1 ``@`` + 255 domain). Uniqueness is
            scoped to the parent organization via
            ``uq_users_org_email`` so the same email can validly
            belong to multiple organizations - this is a deliberate
            multi-tenant design decision per AAP section 0.7.1
            invariant 3.
        display_name: Human-readable name shown in the UI (feed,
            detail view, admin panel). Sourced from the Google OAuth
            ID token's ``name`` claim on first sign-in, or from the
            registration form for password-based users. Length 255
            matches the conservative-but-roomy ceiling used by
            ``records.owner_display_name`` for denormalized snapshots.
        password_hash: bcrypt-encoded password hash, NULLABLE.
            ``None`` indicates an OAuth-only user who never set a
            password; the auth service rejects password-login
            attempts on such users with 401. Length 255 leaves
            headroom for future hash schemes (Argon2id ~96 chars,
            scrypt ~120 chars) without a schema migration; bcrypt 4.x
            output is typically 60 chars (``$2b$12$<22-salt><31-hash>``).
        role: One of the three :class:`UserRole` enum values. Default
            ``Contributor`` per AAP section 0.7.6
            (``DEFAULT_NEW_USER_ROLE``). Bound to the PostgreSQL
            ``user_role`` enum type via ``values_callable`` so the
            human-friendly value strings (``"Admin"``,
            ``"Contributor"``, ``"Viewer"``) round-trip to the
            database exactly. ``create_type=False`` because the
            initial Alembic migration owns the enum-type DDL; the
            ORM must NOT speculatively re-issue ``CREATE TYPE``.
        created_at: Server-assigned timestamp at row insertion.
            ``server_default=func.now()`` ensures the value
            originates in PostgreSQL (the canonical clock) rather
            than in the Python process, eliminating clock-skew
            discrepancies between Gunicorn workers.
        organization: Many-to-one relationship to
            :class:`app.models.organization.Organization`. Bound via
            the ``organization.users`` collection on the organization
            side using ``back_populates="users"``. The string forward
            reference for ``Organization`` is resolved by the
            SQLAlchemy class registry during the lazy
            ``configure_mappers()`` pass.
        records: One-to-many relationship to
            :class:`app.models.record.Record` rows whose
            ``owner_user_id`` matches this user's ``id``. The
            reciprocal ``Record.owner`` is declared on the Record
            model with ``back_populates="records"``. Explicit
            ``primaryjoin`` and ``foreign_keys`` keep the mapping
            unambiguous if the Record schema ever evolves to include
            additional FKs to ``users`` (e.g., a future
            ``last_modified_by_user_id``). NO ``cascade`` configured -
            soft-delete (the typical F-007 path) leaves rows in the
            table; hard-delete is admin-only and goes through a
            dedicated service-layer flow that handles audit-event
            archival first.
        audit_events: One-to-many relationship to
            :class:`app.models.audit_event.AuditEvent` rows whose
            ``actor_user_id`` matches this user's ``id``. The
            reciprocal ``AuditEvent.actor`` is declared on the
            AuditEvent model with ``back_populates="audit_events"``.
            CRITICAL: NO ``cascade`` configured. Audit events MUST
            persist even if the user is hard-deleted. The DB-level
            ``ondelete="RESTRICT"`` on ``audit_events.actor_user_id``
            physically prevents user hard-delete while audit rows
            still reference the user; the admin-panel user-deletion
            flow (post-MVP) is responsible for archiving or
            redacting the historical rows first if required.
    """

    __tablename__ = "users"

    # The composite ``UniqueConstraint("org_id", "email")`` enforces
    # per-organization email uniqueness while allowing the same email
    # to be reused across organizations. PostgreSQL automatically
    # creates a B-tree index supporting the unique constraint, which
    # also satisfies single-column ``org_id`` equality lookups via the
    # leading-column property. The constraint is named explicitly
    # (``uq_users_org_email``) per the project's naming convention so
    # Alembic autogenerate produces deterministic output across
    # environments. The migration at
    # ``backend/migrations/versions/0001_initial_schema.py`` declares
    # the constraint with this exact name; renaming here without
    # updating the migration would cause the next autogenerate run to
    # emit a spurious drop-and-recreate diff.
    __table_args__ = (UniqueConstraint("org_id", "email", name="uq_users_org_email"),)

    # ------------------------------------------------------------------
    # Primary key + multi-tenant scope
    # ------------------------------------------------------------------
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # ------------------------------------------------------------------
    # Identity columns
    # ------------------------------------------------------------------
    # Length 320: RFC 5321 maximum email length (64 local + @ + 255
    # domain). Many systems use VARCHAR(255) and silently truncate; 320
    # is the strictly-correct limit.
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)

    # ------------------------------------------------------------------
    # Authentication credentials
    # ------------------------------------------------------------------
    # NULLABLE per AAP section 0.5.2 Layer 1: OAuth-only users never set
    # a password; the column is NULL for them. Length 255 leaves
    # headroom for future hash schemes (Argon2id ~96 chars, scrypt ~120
    # chars) without a schema migration; bcrypt 4.x output is typically
    # 60 chars.
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # ------------------------------------------------------------------
    # Authorization role (F-009)
    # ------------------------------------------------------------------
    # ``native_enum=True`` binds the column to the PostgreSQL enum type
    # ``user_role`` (created by the initial Alembic migration).
    # ``create_type=False`` prevents SQLAlchemy from speculatively
    # issuing ``CREATE TYPE`` during ``Base.metadata.create_all``; the
    # migration owns the enum-type DDL exclusively.
    # ``values_callable`` ensures the Python enum's ``.value`` strings
    # ("Admin", "Contributor", "Viewer") are sent to PostgreSQL
    # verbatim, NOT the Python member names ("ADMIN", etc.).
    # The Python-level ``default=UserRole.CONTRIBUTOR`` ensures direct
    # INSERTs (test fixtures, ad-hoc scripts) cannot leave the column
    # unset; the auth service can override per OAuth upsert if needed.
    role: Mapped[UserRole] = mapped_column(
        SQLEnum(
            UserRole,
            name="user_role",
            native_enum=True,
            create_type=False,
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
        ),
        nullable=False,
        default=UserRole.CONTRIBUTOR,
    )

    # ------------------------------------------------------------------
    # Audit timestamp
    # ------------------------------------------------------------------
    # ``server_default=func.now()`` keeps the canonical clock in
    # PostgreSQL rather than in the Python worker, eliminating
    # clock-skew across Gunicorn processes. ``DateTime(timezone=True)``
    # maps to PostgreSQL ``timestamptz`` so the value is stored in UTC
    # and converted on retrieval according to the session's timezone
    # setting.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------
    # ``organization`` is the many-to-one parent in the multi-tenant
    # scope. The reciprocal ``Organization.users`` collection is
    # declared on the Organization model (sibling) with
    # ``back_populates="organization"``. SQLAlchemy can infer the join
    # condition from the single FK (``org_id``), so no explicit
    # ``primaryjoin`` or ``foreign_keys`` is needed here.
    organization: Mapped[Organization] = relationship(back_populates="users")

    # ``records`` is the F-006 owner-side view of the Record-User
    # relationship. The reciprocal ``Record.owner`` is declared on the
    # Record model (sibling) with ``back_populates="records"`` and
    # ``foreign_keys="Record.owner_user_id"``. Explicit ``primaryjoin``
    # and ``foreign_keys`` are declared with string forward references
    # so the mapping remains unambiguous if the Record schema ever
    # evolves to include another FK to ``users`` (e.g., a future
    # ``last_modified_by_user_id``). NO ``cascade`` is configured -
    # the typical F-007 path is soft-delete (sets ``deleted_at``), and
    # hard-delete (admin only) goes through a dedicated service-layer
    # flow that explicitly handles audit-event archival first.
    records: Mapped[list[Record]] = relationship(
        back_populates="owner",
        primaryjoin="User.id == Record.owner_user_id",
        foreign_keys="Record.owner_user_id",
    )

    # ``audit_events`` is the F-013 actor-side view of the
    # AuditEvent-User relationship. The reciprocal ``AuditEvent.actor``
    # is declared on the AuditEvent model with
    # ``back_populates="audit_events"`` and
    # ``primaryjoin="AuditEvent.actor_user_id == User.id"``. CRITICAL:
    # NO cascade is configured here. Audit events MUST persist even if
    # the user is hard-deleted. The database-level
    # ``ondelete="RESTRICT"`` on ``audit_events.actor_user_id``
    # physically prevents a user hard-delete from succeeding while
    # audit rows still reference the user; the admin-panel
    # user-deletion flow (post-MVP) is responsible for archiving or
    # redacting the historical rows first if required. The
    # string-based ``primaryjoin`` and ``foreign_keys`` are required
    # to break the circular import cycle between this module and
    # ``app.models.audit_event``.
    audit_events: Mapped[list[AuditEvent]] = relationship(
        back_populates="actor",
        primaryjoin="User.id == AuditEvent.actor_user_id",
        foreign_keys="AuditEvent.actor_user_id",
    )

    def __repr__(self) -> str:
        """Return a concise developer-friendly representation.

        Includes only the primary key and the role as a discriminator.
        Deliberately EXCLUDES ``email``, ``display_name``, and
        ``password_hash`` because those fields are personally
        identifiable information (PII) or credentials. Even though the
        structlog redactor configured in
        :mod:`app.observability.logging` filters secret-named keys at
        the logging boundary, the safer pattern is to never put PII or
        credentials in ``__repr__`` in the first place: ``repr(user)``
        appears in exception tracebacks, debugger output, and ad-hoc
        logging that may bypass the redactor.

        The format is intentionally stable so that test assertions and
        operational tooling can match it exactly:
        ``<User id={uuid} role={role_value}>``.
        """
        return f"<User id={self.id} role={self.role.value}>"
