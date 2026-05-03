"""SQLAlchemy 2.x declarative model for the organizations table.

``Organization`` is the multi-tenant scope (the "tenant") for every
entity in the system. Per AAP section 0.7.1 invariant 3, every other
table carries an ``org_id`` foreign key into this table, and every
read/write injects ``WHERE org_id = g.session.org_id``.

Per AAP section 0.7.2, MVP runtime serves a SINGLE organization. The
``organizations`` table holds exactly one row at MVP runtime - seeded
by the initial Alembic migration with the UUID configured by the
``DEFAULT_ORG_ID`` environment variable. The schema is engineered for
multi-tenant runtime so a future feature can introduce additional rows
without a destructive migration.

Per AAP section 0.5.3, this module declares schema only. There is no
business logic associated with Organization in MVP (no creation flow,
no settings, no branding).

Reciprocal relationships established in this module:

    Organization.users    <- back_populates ->  User.organization
    Organization.records  <- back_populates ->  Record.organization
    Organization.tags     <- back_populates ->  Tag.organization

The sibling-side ``Organization`` references on User/Record/Tag are
declared with ``back_populates="users"``/``"records"``/``"tags"`` and
resolve via SQLAlchemy's class registry during the lazy
``configure_mappers()`` pass. The TYPE_CHECKING-gated imports below
break the otherwise-circular import graph at module load time.

This module deliberately has no module-level side effects beyond class
registration on the SQLAlchemy mapper registry. Importing
``app.models.organization`` does not log, does not make HTTP calls,
does not touch the filesystem, and does not connect to any database.
"""

from __future__ import annotations

# ``datetime`` and ``Mapped``/``mapped_column``/``relationship`` MUST be
# importable at module load time. SQLAlchemy 2.x's declarative system
# parses ``Mapped[...]`` annotations at class-construction time via
# ``de_stringify_annotation`` (see :mod:`sqlalchemy.util.typing`),
# which does ``eval()`` on the annotation string and looks up names in
# the originating module's ``__globals__``. Moving these names into a
# ``TYPE_CHECKING`` block would therefore raise
# ``MappedAnnotationError`` at import. The ``noqa`` suppressions below
# are necessary, not stylistic.
from datetime import datetime  # noqa: TC003
from typing import TYPE_CHECKING
import uuid

from sqlalchemy import DateTime, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship  # noqa: TC002

from app.extensions import Base

if TYPE_CHECKING:
    # Sibling-model imports gated to type-check time only. The
    # SQLAlchemy declarative classes for ``User``, ``Record``, and
    # ``Tag`` reference this class via ``relationship(back_populates=...)``
    # and vice versa, which would form a circular import graph at
    # module load time if these were unconditional imports. The
    # ``Mapped[list[User]]``/``Mapped[list[Record]]``/``Mapped[list[Tag]]``
    # annotations below are stringified by ``from __future__ import
    # annotations`` and resolved lazily by SQLAlchemy's class registry
    # during the ``configure_mappers()`` pass once both sides of every
    # ``back_populates`` pair are loaded.
    from app.models.record import Record
    from app.models.tag import Tag
    from app.models.user import User


__all__ = ["Organization"]


class Organization(Base):
    """A multi-tenant organization scope (the "tenant").

    MVP runtime serves exactly one row in this table - seeded by
    ``backend/migrations/versions/0001_initial_schema.py`` using the
    UUID configured via the ``DEFAULT_ORG_ID`` environment variable
    (default ``00000000-0000-0000-0000-000000000001``).

    The schema is engineered with multi-tenancy in mind:

        * Every other table carries a non-null ``org_id`` FK pointing
          here.
        * Every read/write injects ``WHERE org_id = g.session.org_id``
          (AAP section 0.7.1 invariant 3).
        * ``ondelete="RESTRICT"`` on every reference protects against
          accidental cross-tenant data loss.

    No business logic exists for Organization in MVP. There is no
    creation flow (single-org runtime), no settings (no per-org
    branding), and no admin surface (per AAP section 0.6.2).

    Attributes:
        id: Primary key. UUID v4 generated client-side via the
            ``default=uuid.uuid4`` factory when the caller does not
            supply a value (the typical case). The initial Alembic
            migration seeds the single MVP-runtime row using the
            ``DEFAULT_ORG_ID`` environment-variable UUID rather than
            relying on the factory so the value is deterministic
            across environments.
        name: Display name for the organization. Length 255 matches
            the conservative-but-roomy ceiling used throughout the
            schema for human-readable strings. Not user-visible in
            MVP because there is only one organization.
        created_at: Server-assigned timestamp at row insertion. The
            ``server_default=func.now()`` directive ensures the value
            originates in PostgreSQL (the canonical clock) rather
            than in the Python process, eliminating clock-skew
            discrepancies between Gunicorn workers.
            ``DateTime(timezone=True)`` maps to PostgreSQL
            ``timestamptz`` so the value is stored in UTC and
            converted on retrieval according to the session's
            timezone setting.
        users: One-to-many relationship to :class:`User` rows whose
            ``org_id`` matches this organization's ``id``. Reciprocal:
            ``User.organization`` is declared on the User model
            (sibling) with ``back_populates="users"``.
            ``cascade="all"`` (no ``delete-orphan``): if an organization
            is hard-deleted (post-MVP scenario), its users are cascaded
            with it. The DB-level ``ondelete="RESTRICT"`` on
            ``users.org_id`` would actually prevent that delete unless
            the deletion path explicitly cascades through users first;
            the ORM-level cascade declared here matches that intent
            for any future code path.
        records: One-to-many relationship to :class:`Record` rows
            whose ``org_id`` matches this organization's ``id``.
            Reciprocal: ``Record.organization`` is declared on the
            Record model (sibling) with ``back_populates="records"``.
            Same ``cascade="all"`` rationale as for ``users``.
        tags: One-to-many relationship to :class:`Tag` rows whose
            ``org_id`` matches this organization's ``id``.
            Reciprocal: ``Tag.organization`` is declared on the Tag
            model (sibling) with ``back_populates="tags"``. Same
            ``cascade="all"`` rationale as for ``users``.
    """

    __tablename__ = "organizations"

    # No ``__table_args__`` is needed: there are no composite
    # constraints or indexes specific to organizations beyond the
    # auto-generated primary-key index (named ``pk_organizations`` per
    # the project's :data:`app.extensions.NAMING_CONVENTION`). Forward-
    # looking columns (``slug``, ``domain``, ``branding``, etc.) that
    # would otherwise carry their own constraints are deferred to a
    # post-MVP migration per AAP section 0.6.2.

    # ------------------------------------------------------------------
    # Primary key
    # ------------------------------------------------------------------
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
    )

    # ------------------------------------------------------------------
    # Display column
    # ------------------------------------------------------------------
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # ------------------------------------------------------------------
    # Audit timestamp
    # ------------------------------------------------------------------
    # ``server_default=func.now()`` keeps the canonical clock in
    # PostgreSQL rather than in the Python worker, eliminating
    # clock-skew across Gunicorn processes. ``DateTime(timezone=True)``
    # maps to PostgreSQL ``timestamptz`` so the value is stored in UTC.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------
    # The reciprocal sides of these relationships are declared on the
    # User, Record, and Tag models (siblings) using
    # ``back_populates="organization"``. SQLAlchemy resolves the
    # string-based forward references during the registry's
    # ``configure_mappers()`` pass once both sides of every
    # ``back_populates`` pair are loaded.
    #
    # ``cascade="all"`` propagates SAVE / REFRESH / MERGE / DELETE /
    # EXPIRE operations from this parent to its children. We
    # deliberately do NOT add ``"delete-orphan"`` because an
    # organization is a top-level entity; it cannot be "orphaned" by
    # something above it. ``delete-orphan`` is for child entities
    # that lose meaning when their parent is deleted - the inverse
    # direction here.
    users: Mapped[list[User]] = relationship(
        back_populates="organization",
        cascade="all",
    )
    records: Mapped[list[Record]] = relationship(
        back_populates="organization",
        cascade="all",
    )
    tags: Mapped[list[Tag]] = relationship(
        back_populates="organization",
        cascade="all",
    )

    def __repr__(self) -> str:
        """Return a concise developer-friendly representation.

        Includes only the primary key. Deliberately EXCLUDES ``name``
        because including string fields in ``__repr__`` would set a
        precedent that other models (notably :class:`User`) would then
        break - and User's ``email`` field IS personally identifiable
        information (PII). Keeping all model ``__repr__`` outputs to
        primary-key-only is a uniform project convention.

        The format is intentionally stable so that test assertions and
        operational tooling can match it exactly:
        ``<Organization id={uuid}>``.
        """
        return f"<Organization id={self.id}>"
