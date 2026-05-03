"""Alembic environment script for the Sales-Connections backend.

This file is the bridge between the Alembic CLI and the application.
It is invoked by every Alembic command (``alembic upgrade``,
``alembic downgrade``, ``alembic revision --autogenerate``).

Responsibilities:
    1. Import ``app.models`` to populate ``Base.metadata`` for autogenerate.
       Each declarative model class registers itself on
       ``Base.metadata`` via SQLAlchemy class-body side effects when its
       module is imported. The ``app.models`` package re-exports every
       model class, so a single ``import app.models`` is sufficient to
       register all six tables (organizations, users, records, tags,
       record_tags, audit_events).
    2. Resolve the database URL for migrations:
           * MIGRATIONS_DATABASE_URL (preferred -- elevated role with full
             DDL/DML privileges, used for migrations only).
           * DATABASE_URL            (fallback -- application role with
             INSERT-only on audit_events; sufficient for the very first
             migration run on a fresh database before the GRANT/REVOKE
             block is applied, and for local development where a single
             shared DSN is used).
           * sqlalchemy.url from alembic.ini  (legacy fallback).
       Per AAP Section 0.4.7, the application role at runtime has only
       INSERT privileges on ``audit_events``; the migrations role retains
       full DDL/DML.
    3. Configure autogenerate with:
           * compare_type=True            (detect column type changes)
           * compare_server_default=True  (detect default-value changes)
           * include_schemas=False        (single 'public' schema)
           * include_object / include_name (filter system objects)
           * render_as_batch=False        (PostgreSQL supports
                                           transactional ALTER TABLE;
                                           batch mode is for SQLite)
    4. Configure structlog so any migration script that uses
       ``structlog.get_logger`` emits JSON, matching the application's
       runtime log format and enabling CI to parse migration events
       cleanly.

ONLINE MODE
    Used by the standard ``alembic upgrade head`` invocation. Builds a
    fresh SQLAlchemy Engine (NullPool, short-lived connection) from
    the resolved DSN and runs migrations inside a single transaction.

OFFLINE MODE
    Used by ``alembic upgrade --sql`` to emit SQL statements without
    executing them. Useful for code review and dry-run change-control
    workflows.

CRITICAL COORDINATION POINTS:
    * ``app.extensions.Base.metadata`` -- the source of truth for
      autogenerate. Carries the project's stable naming convention
      (NAMING_CONVENTION in app/extensions.py) so generated constraint
      names are deterministic across environments.
    * ``app.models`` package -- imported for its re-export side effects.
      Every declarative model registers itself on ``Base.metadata`` upon
      import. Without this import, ``Base.metadata.tables`` would be
      empty and autogenerate would emit DROP statements for every
      existing table -- a disaster.
    * ``backend/alembic.ini`` -- declares ``script_location = migrations``
      and supplies the [loggers] section consumed by fileConfig().
    * The application role's INSERT-only grants on ``audit_events`` are
      applied by the initial migration (0001_initial_schema.py); the
      elevated migrations role retains full DDL/DML privileges.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.config import fileConfig
from pathlib import Path
from typing import Any

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import Connection  # noqa: TC002 - used at runtime in annotation

# ---------------------------------------------------------------------------
# Path setup -- ensure `from app.<module> import ...` resolves regardless of
# the working directory from which `alembic` is invoked.
#
# Alembic's CLI may be invoked from various working directories: from the
# repository root (``alembic -c backend/alembic.ini upgrade head``), from
# the backend directory (``cd backend && alembic upgrade head``), or from
# inside a Docker container where the layout is different again. Adding
# the parent directory of this file (the ``backend/`` package root) to
# ``sys.path`` makes ``from app.extensions import Base`` resolve in every
# case without requiring a ``prepend_sys_path`` directive in alembic.ini.
# ---------------------------------------------------------------------------
_MIGRATIONS_DIR = Path(__file__).resolve().parent
_BACKEND_DIR = _MIGRATIONS_DIR.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# ---------------------------------------------------------------------------
# First-party imports (after sys.path setup).
#
# The noqa directives on the imports below are required because ruff's
# E402 rule flags imports that are not at the top of the file. Here,
# the ordering is intentional: the sys.path manipulation MUST run
# before these imports can succeed, so they cannot live in the standard
# top-of-file import block.
# ---------------------------------------------------------------------------
from app.extensions import Base  # noqa: E402

# Importing app.models triggers the side-effect re-exports in
# app/models/__init__.py, which in turn imports every declarative model
# module. Each module's class-body execution registers the model on
# Base.metadata via SQLAlchemy's declarative system. Without this import
# Base.metadata.tables would be empty and autogenerate would emit DROP
# statements for every existing table -- catastrophic data loss.
import app.models  # noqa: E402, F401

# ---------------------------------------------------------------------------
# Alembic Config singleton.
#
# Alembic provides ``context.config`` after env.py is loaded by the CLI.
# The Config object exposes alembic.ini settings via ``get_main_option``
# and the ini path itself via ``config_file_name``.
# ---------------------------------------------------------------------------
config = context.config

# ---------------------------------------------------------------------------
# Standard logging configuration.
#
# Wired from alembic.ini's [loggers] / [handlers] / [formatters] sections
# when present. Falls back to basicConfig if alembic.ini is absent or
# does not define a [loggers] section. The fallback path keeps Alembic's
# progress messages visible in stripped-down deployments (e.g., a slim
# CI image that ships alembic.ini without a full logging config).
# ---------------------------------------------------------------------------
if config.config_file_name is not None:
    try:
        fileConfig(config.config_file_name)
    except Exception:  # pragma: no cover - defensive
        # alembic.ini may not have a [loggers] section in some
        # deployments; fall back to a basic config rather than
        # crashing the migration run.
        logging.basicConfig(level=logging.INFO)
else:
    logging.basicConfig(level=logging.INFO)

# ---------------------------------------------------------------------------
# structlog configuration -- best effort.
#
# Configures structlog to emit JSON so that any migration script using
# ``structlog.get_logger`` produces logs in the same format as the
# application runtime. structlog is in the application's
# requirements.txt but might be absent from a stripped-down migrations-
# only installation surface (e.g., a slim CI image that runs alembic
# without the full app dependencies). Wrapping in try/except ImportError
# keeps the migrations functional in either environment.
# ---------------------------------------------------------------------------
try:
    import structlog

    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        cache_logger_on_first_use=True,
    )
except ImportError:  # pragma: no cover
    pass


# ---------------------------------------------------------------------------
# DSN resolution.
#
# Resolution order (per AAP Section 0.4.6):
#     1. MIGRATIONS_DATABASE_URL  -- elevated role with full DDL/DML
#        privileges, used for migrations to apply GRANT/REVOKE blocks
#        that the application role lacks privilege to issue.
#     2. DATABASE_URL             -- application role; sufficient when
#        the GRANT/REVOKE block has not yet been applied (e.g., the
#        very first run on a fresh database) and for local development
#        where a single shared DSN is used.
#     3. sqlalchemy.url from alembic.ini  -- legacy fallback; usually
#        empty in this project so the env vars take precedence.
#
# We deliberately read from os.environ rather than importing
# app.config.BaseConfig. Reasons:
#   1. Avoids loading the full Flask config machinery (dotenv loading,
#      AWS Secrets Manager lookups) just to get a DSN.
#   2. Keeps the migration runnable in environments where the
#      application code is incompletely installed (e.g., a CI image
#      that ships only Alembic + SQLAlchemy).
#   3. Mirrors the pattern Flask itself uses:
#      ``os.environ.get('DATABASE_URL')``.
# ---------------------------------------------------------------------------


def _resolve_database_url() -> str:
    """Resolve the DSN to use for this migration run.

    Resolution order (per AAP Section 0.4.6):
        1. ``MIGRATIONS_DATABASE_URL`` -- elevated role with full DDL/DML
           privileges, used for migrations to apply GRANT/REVOKE.
        2. ``DATABASE_URL`` -- application role; sufficient when the
           GRANT/REVOKE block has not yet been applied (e.g., the very
           first run on a fresh database) or for local development where
           a single shared DSN is used.
        3. ``sqlalchemy.url`` from alembic.ini -- legacy fallback;
           usually empty in this project.

    Returns:
        The first non-empty DSN string from the resolution order, with
        leading and trailing whitespace stripped.

    Raises:
        RuntimeError: When no DSN can be resolved. This causes the
            migration run to fail fast with a clear error rather than
            silently reading a wrong URL or crashing later in the
            engine construction with a less helpful message.
    """
    candidates = (
        os.environ.get("MIGRATIONS_DATABASE_URL"),
        os.environ.get("DATABASE_URL"),
        config.get_main_option("sqlalchemy.url"),
    )
    for candidate in candidates:
        if candidate and candidate.strip():
            return candidate.strip()
    raise RuntimeError(
        "Cannot run migrations: neither MIGRATIONS_DATABASE_URL nor "
        "DATABASE_URL is set, and alembic.ini's sqlalchemy.url is empty. "
        "Set one of these environment variables before invoking alembic."
    )


# Inject the resolved URL into the Alembic config so downstream
# ``engine_from_config`` calls see it as ``sqlalchemy.url``.
config.set_main_option("sqlalchemy.url", _resolve_database_url())


# ---------------------------------------------------------------------------
# Target metadata.
#
# ``Base.metadata`` is the SQLAlchemy MetaData object that all six
# declarative models register themselves on. Setting this as the
# ``target_metadata`` enables autogenerate to compare the model-declared
# schema against the live database schema and produce accurate
# migration revisions.
#
# The ``naming_convention`` key on ``Base.metadata`` (defined in
# app/extensions.py as NAMING_CONVENTION) ensures generated constraint
# names are deterministic: ``ix_*`` for indexes, ``uq_*`` for unique
# constraints, ``ck_*`` for check constraints, ``fk_*`` for foreign
# keys, ``pk_*`` for primary keys. This keeps autogenerated migrations
# clean and reviewable across environments.
# ---------------------------------------------------------------------------
target_metadata = Base.metadata


# ---------------------------------------------------------------------------
# Autogenerate filters.
#
# ``include_object`` and ``include_name`` are passed to
# ``context.configure`` so that autogenerate can be told to ignore
# database objects that are NOT owned by this application. Without
# these filters, autogenerate would propose to drop tables created by
# other tools (PostgreSQL extensions, monitoring helpers,
# pg_stat_statements, etc.) -- a disaster.
# ---------------------------------------------------------------------------


def _include_object(
    object_: Any,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to: Any,
) -> bool:
    """Filter for autogenerate: include only application-owned objects.

    Per AAP Section 0.4.7, the schema consists of six application
    tables and four PostgreSQL enum types. Anything else (e.g.,
    extensions, system tables, third-party monitoring helpers) must be
    excluded so autogenerate does not propose to drop or alter them.

    The ``alembic_version`` table is included because Alembic itself
    creates and maintains it; excluding it would cause autogenerate to
    propose dropping Alembic's own bookkeeping table.

    Args:
        object_: The schema object being considered (Table, Column,
            Index, etc.). Type is dynamic per Alembic's API.
        name: The name of the object as a string, or None for unnamed
            objects.
        type_: A string indicating the kind of object: 'table', 'column',
            'index', 'unique_constraint', 'foreign_key_constraint',
            'check_constraint', 'schema'.
        reflected: True if the object was reflected from the database
            (i.e., exists in the live schema); False if it was declared
            in the application's metadata. Application-declared objects
            are always included; reflected objects are filtered.
        compare_to: The corresponding object on the other side of the
            comparison (declared if reflected is True; reflected if
            reflected is False), or None if no counterpart exists.
            Type is dynamic per Alembic's API.

    Returns:
        True to include the object in the autogenerate diff, False to
        exclude it.
    """
    # Always include items that come from our metadata (i.e., declared
    # in app.models). The reflected==False branch covers everything
    # the application declares; we never want to filter those out.
    if not reflected:
        return True
    # For reflected items (read from the live database), accept only
    # known table names. This guards against autogenerate proposing to
    # drop tables that exist for legitimate reasons (extensions,
    # monitoring) but are not part of our declarative schema.
    known_tables = {
        "alembic_version",
        "audit_events",
        "organizations",
        "record_tags",
        "records",
        "tags",
        "users",
    }
    # Reject only reflected tables whose name is not one of ours; every
    # other reflected object kind (columns, indexes, constraints) flows
    # through the parent table's filter decision.
    return not (type_ == "table" and name not in known_tables)


def _include_name(
    name: str | None,
    type_: str,
    parent_names: dict[str, str | None],
) -> bool:
    """Filter for autogenerate at the schema/name level.

    Locks autogenerate to the ``public`` schema, ignoring system
    schemas like ``information_schema`` and ``pg_catalog`` that
    PostgreSQL exposes by default.

    Args:
        name: The name of the schema-level object being considered, or
            None for the default schema. This may be a schema name, a
            table name (within a schema), or a column name (within a
            table) depending on type_.
        type_: A string indicating the kind of object being filtered:
            'schema', 'table', 'column'.
        parent_names: A dict mapping parent kinds to their names; for a
            table this contains 'schema_name', for a column it contains
            'schema_name' and 'table_name'.

    Returns:
        True to include the object in autogenerate's reflection scope,
        False to exclude it.
    """
    if type_ == "schema":
        # Accept the default schema (None) and the explicit 'public'
        # schema only; reject every other schema (information_schema,
        # pg_catalog, third-party monitoring schemas, etc.).
        return name in (None, "public")
    return True


# ---------------------------------------------------------------------------
# Offline migration mode.
#
# Used by ``alembic upgrade --sql`` to emit SQL statements without
# executing them. Useful for code review, dry-run change-control
# workflows, and producing a SQL artifact that can be applied manually
# by a DBA in restricted environments.
# ---------------------------------------------------------------------------


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL without executing).

    Configures the Alembic context with just a URL (no Engine, no
    DBAPI required). Calls to ``context.execute()`` emit the given
    SQL string to the script output rather than executing against a
    connection.

    ``literal_binds=True`` causes Alembic to render parameter values
    inline rather than as ``%s`` / ``$1`` placeholders, producing
    human-readable SQL output suitable for code review.

    ``dialect_opts={"paramstyle": "named"}`` ensures any remaining
    parameters are rendered as ``:name`` (named) rather than
    positional placeholders, which is also more readable in offline
    output.

    Returns:
        None.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_schemas=False,
        include_object=_include_object,
        include_name=_include_name,
        render_as_batch=False,
    )

    with context.begin_transaction():
        context.run_migrations()


# ---------------------------------------------------------------------------
# Online migration mode.
#
# Used by the standard ``alembic upgrade head`` invocation. Builds a
# fresh SQLAlchemy Engine from the resolved DSN and runs migrations
# inside a single transaction. PostgreSQL supports transactional DDL,
# so all DDL emitted by a migration either commits atomically or rolls
# back atomically.
# ---------------------------------------------------------------------------


def do_run_migrations(connection: Connection) -> None:
    """Configure context with an active Connection and run migrations.

    Separated from :func:`run_migrations_online` so async-style
    adapters or future test fixtures can supply their own Connection
    without duplicating the configure/run logic. The split also makes
    the function trivially testable: a test fixture can construct an
    in-memory SQLite engine, hand the connection to this function, and
    verify the migration produces the expected schema.

    Args:
        connection: An active SQLAlchemy :class:`Connection` bound to
            the target database. The caller is responsible for the
            lifetime of this connection (open and close).

    Returns:
        None.
    """
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        include_schemas=False,
        include_object=_include_object,
        include_name=_include_name,
        render_as_batch=False,
        # process_revision_directives is set to None (the default) but
        # stated explicitly to leave room for future customizations
        # (e.g., a hook that rejects migrations dropping columns to
        # enforce the AAP Section 0.7.7 forward-compatibility rule
        # programmatically).
        process_revision_directives=None,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (connected to a live database).

    Builds a SQLAlchemy Engine from the resolved DSN and runs
    migrations inside a single transaction. Connection pooling is set
    to :class:`pool.NullPool` because:

    * Migrations are short-lived processes; pool recycling provides
      no benefit and adds the risk of stale connections during
      long-running DDL.
    * NullPool ensures every migration uses a fresh connection, so a
      crashed migration cannot leave a tainted connection in a pool.

    ``future=True`` opts the engine into SQLAlchemy 2.x execution
    semantics, matching the application runtime's engine
    configuration in :mod:`app.extensions`.

    Returns:
        None.
    """
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = config.get_main_option(
        "sqlalchemy.url",
        "",
    )
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        future=True,
    )

    with connectable.connect() as connection:
        do_run_migrations(connection)

    connectable.dispose()


# ---------------------------------------------------------------------------
# Mode dispatch.
#
# Alembic's ``context.is_offline_mode()`` returns True when the CLI
# was invoked with ``--sql`` (or equivalent). This block runs at module
# import time -- Alembic's CLI imports env.py for its side effects, so
# the actual migration runs as part of the import.
# ---------------------------------------------------------------------------
if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
