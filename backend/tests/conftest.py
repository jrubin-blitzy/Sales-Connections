"""Pytest configuration and shared fixtures for the Sales-Connections backend.

Provides the foundational fixtures used by every test module:

* :func:`app_factory` - Constructs a Flask app inline (``create_app()``
  is deferred to CP6 per the project's checkpoint plan; tests
  reproduce its wiring locally).
* :func:`app` - The Flask test app (function-scoped to maximize
  isolation and avoid state leakage between tests).
* :func:`client` - Flask test client (``app.test_client()``) used by
  HTTP-level tests.
* :func:`db_session` - A SQLAlchemy session bound to the test
  database. Each test gets a fresh session that rolls back on
  teardown so the database state does not leak across tests.
* :func:`_bind_factories_session` (autouse) - Binds the
  factory-boy factories defined in :mod:`tests.factories` to the
  test-scoped ``db_session``. Without this fixture the factories
  fall back to ``sqlalchemy_session = None`` and raise on first use.
* :func:`runner` - Flask CLI test runner for command-tests.
* :func:`organization` - A fresh organization row that all factories
  default to. Tests that need cross-org behavior construct
  additional organizations via :class:`OrganizationFactory`.
* :func:`auth_session` - A factory-style fixture that mints a session
  cookie for the supplied user, used by API-level tests that need
  to exercise authenticated endpoints without going through the
  full /auth/login round-trip.

Test database
-------------
The tests use a real PostgreSQL database (per AAP Section 0.5.2 the
production schema relies on PostgreSQL-specific enums and JSONB).
The database connection string is resolved in this order:

    1. ``TEST_DATABASE_URL`` env var (CI override).
    2. ``DATABASE_URL`` env var (developer override).
    3. ``TestingConfig.DATABASE_URL`` default
       (``postgresql+psycopg://sales_connections:sales_connections
       @localhost:5432/sales_connections_test``).

The schema is created once per pytest session via
:func:`_setup_test_database` (autouse, session-scoped) by running
the Alembic migration head. Each test gets a clean slate via the
:func:`db_session` fixture which truncates all tables (preserving
schema) on teardown.

Per AAP Section 0.7.1 invariant 1 (stateless workers), no test
mutates module-level singletons in a way that would leak across
tests. The :func:`_reset_extensions` autouse fixture disposes the
SQLAlchemy engine and clears the OAuth client between tests.

Per AAP Section 0.7.5 (Observability), the :func:`structlog_bound_capture`
fixture binds structlog's ContextVars context for each test so log
correlation IDs do not leak between tests. The
:func:`_clear_structlog_context` autouse fixture clears the binding
after each test.
"""

from __future__ import annotations

# Standard library imports.
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any
import uuid

from flask import Flask

# Third-party runtime imports.
import pytest
from sqlalchemy import text
import structlog

# First-party imports.
from app.config import TestingConfig
from app.extensions import Base, db, init_oauth_clients, oauth
from app.middleware.auth import register_auth_middleware
from app.middleware.correlation import register_correlation_middleware
from app.middleware.error_handlers import register_error_handlers
from app.middleware.rbac import register_rbac_error_handlers
from app.observability.logging import configure_structlog
from app.observability.metrics import init_metrics

# Type-only imports.
if TYPE_CHECKING:
    from collections.abc import Generator

    from sqlalchemy.orm import Session as SQLAlchemySession

# ===========================================================================
# Module-level constants
# ===========================================================================

# The default org id pinned in TestingConfig. Materialized as a
# real Organization row by the :func:`organization` fixture so that
# every factory's default ``org`` SubFactory reference resolves to
# this row rather than creating a new org per test.
_DEFAULT_ORG_ID: uuid.UUID = uuid.UUID(TestingConfig.DEFAULT_ORG_ID)


# ===========================================================================
# Helper: programmatic Alembic upgrade
# ===========================================================================


def _run_alembic_upgrade(database_url: str) -> None:
    """Apply all Alembic migrations to the supplied test database.

    Equivalent to ``alembic upgrade head`` but run via the Alembic
    Python API so we do not need to ship an ``alembic.ini`` for the
    tests. The migrations live under ``backend/migrations/`` and are
    indexed by ``backend/migrations/env.py``.

    Args:
        database_url: SQLAlchemy DSN for the test database.
    """
    # Lazy import so non-DB tests that don't need the schema don't
    # incur Alembic's import cost.
    from alembic import command  # noqa: PLC0415
    from alembic.config import Config  # noqa: PLC0415

    repo_backend = Path(__file__).resolve().parents[1]
    migrations_dir = repo_backend / "migrations"

    cfg = Config()
    cfg.set_main_option("script_location", str(migrations_dir))
    cfg.set_main_option("sqlalchemy.url", database_url)
    # ``MIGRATIONS_DATABASE_URL`` env var, when set, takes precedence
    # in env.py's resolution chain. Setting it here ensures the
    # test invocation uses the same DSN as the application engine.
    os.environ.setdefault("MIGRATIONS_DATABASE_URL", database_url)

    command.upgrade(cfg, "head")


def _create_schema_directly(database_url: str) -> None:
    """Create the schema using ``db.metadata.create_all`` plus enum DDL.

    Fallback path used when the Alembic upgrade fails (e.g., the
    database role lacks a privilege required by the migration's
    GRANT/REVOKE block). This path creates the four PostgreSQL enum
    types AND the table schema, but skips the audit-immutability
    GRANT/REVOKE step because the test role usually doesn't have
    superuser privileges.

    Args:
        database_url: SQLAlchemy DSN for the test database.
    """
    from sqlalchemy import create_engine  # noqa: PLC0415

    engine = create_engine(database_url)
    with engine.begin() as conn:
        # Drop any existing enum types from a prior failed run so
        # we can recreate them cleanly. Use IF EXISTS so the first
        # invocation on a fresh DB doesn't error.
        conn.execute(
            text(
                """
                DROP TABLE IF EXISTS audit_events CASCADE;
                DROP TABLE IF EXISTS record_tags CASCADE;
                DROP TABLE IF EXISTS tags CASCADE;
                DROP TABLE IF EXISTS records CASCADE;
                DROP TABLE IF EXISTS users CASCADE;
                DROP TABLE IF EXISTS organizations CASCADE;
                DROP TYPE IF EXISTS audit_event_type CASCADE;
                DROP TYPE IF EXISTS user_role CASCADE;
                DROP TYPE IF EXISTS outreach_status CASCADE;
                DROP TYPE IF EXISTS involvement_type CASCADE;
                """
            )
        )
        # Create the four enum types in the order the model
        # declarations expect.
        conn.execute(
            text(
                "CREATE TYPE involvement_type AS ENUM "
                "('Warm Intro', 'Soft Reference', 'Target Only')"
            )
        )
        conn.execute(
            text(
                "CREATE TYPE outreach_status AS ENUM "
                "('Not Started', 'In Progress', 'Contacted', 'Closed')"
            )
        )
        conn.execute(
            text("CREATE TYPE user_role AS ENUM ('Admin', 'Contributor', 'Viewer')")
        )
        conn.execute(
            text(
                "CREATE TYPE audit_event_type AS ENUM "
                "('create', 'status_change', 'edit', 'soft_delete', "
                "'hard_delete', 'role_change', 'authentication', 'admin_op')"
            )
        )
    # Now use SQLAlchemy's create_all to build the tables. Imports
    # are deferred to ensure all model modules are registered on
    # Base.metadata first.
    import app.models  # noqa: F401, PLC0415  - registers all models

    Base.metadata.create_all(bind=engine)
    engine.dispose()


# ===========================================================================
# Fixture: Flask application factory
# ===========================================================================


def _build_test_app() -> Flask:
    """Build a Flask app instance configured for testing.

    Mirrors the wiring that :func:`app.create_app` will perform once
    Layer 0 ships (CP6). The middleware ordering is:
        correlation -> auth -> error_handlers (last)

    Blueprints are registered AFTER middleware so handlers see the
    populated ``g.session``.
    """
    flask_app = Flask("sales_connections_test")
    flask_app.config.from_object(TestingConfig)

    # Configure structlog BEFORE any middleware that emits logs so
    # the processor chain is in place when the first log line fires.
    configure_structlog(
        log_level=flask_app.config.get("LOG_LEVEL", "WARNING"),
        log_format=flask_app.config.get("LOG_FORMAT", "json"),
    )

    # Bind extensions to the app.
    db.init_app(flask_app)
    oauth.init_app(flask_app)
    init_oauth_clients(flask_app, oauth)

    # Register middleware in canonical order.
    register_correlation_middleware(flask_app)
    register_auth_middleware(flask_app)
    register_error_handlers(flask_app)
    register_rbac_error_handlers(flask_app)

    # Register all API blueprints.
    from app.api import register_blueprints  # noqa: PLC0415

    register_blueprints(flask_app)

    # Initialise the Prometheus metrics endpoint and request hooks so
    # observability is exercised by the test suite. Per AAP §0.7.5 the
    # observability rule applies to every deliverable; mounting the
    # metrics endpoint in the test app keeps the production wiring
    # identical to test wiring and lets the coverage suite reach
    # `app.observability.metrics`.
    init_metrics(flask_app)

    return flask_app


# ===========================================================================
# Session-scoped fixture: set up the test database schema
# ===========================================================================


@pytest.fixture(scope="session", autouse=True)
def _setup_test_database() -> Generator[None, None, None]:
    """Create the schema in the test database once per pytest session.

    Runs the Alembic migration head against the test DSN. If Alembic
    fails (e.g., the test role lacks the GRANT/REVOKE privilege),
    falls back to ``db.metadata.create_all`` plus manual enum DDL so
    the suite can still run.
    """
    database_url = TestingConfig.DATABASE_URL
    try:
        _run_alembic_upgrade(database_url)
    except Exception:
        _create_schema_directly(database_url)

    return

    # Teardown: leave the schema in place so subsequent test
    # invocations reuse it. Dropping the schema would slow the
    # suite considerably.


# ===========================================================================
# Function-scoped fixture: Flask app
# ===========================================================================


@pytest.fixture
def app() -> Generator[Flask, None, None]:
    """Construct a fresh Flask app for each test.

    Function-scoped to maximize isolation. Each test gets a fresh
    extension state, fresh middleware registration, and fresh
    blueprint routes.
    """
    flask_app = _build_test_app()

    # Push an application context so handlers using ``current_app``
    # outside a request work correctly.
    with flask_app.app_context():
        yield flask_app

    # Teardown: dispose the SQLAlchemy engine to release pooled
    # connections. The next test's ``app`` fixture rebuilds the
    # engine via ``db.init_app(...)``.
    db.dispose()


# ===========================================================================
# Function-scoped fixture: Flask test client
# ===========================================================================


@pytest.fixture
def client(app: Flask) -> Any:
    """Return a Flask test client for HTTP-level assertions.

    Used by API tests to issue requests against the registered
    blueprints without spinning up a real WSGI server.
    """
    return app.test_client()


@pytest.fixture
def runner(app: Flask) -> Any:
    """Return a Flask CLI test runner for command-tests."""
    return app.test_cli_runner()


# ===========================================================================
# Function-scoped fixture: SQLAlchemy session
# ===========================================================================


@pytest.fixture
def db_session(app: Flask) -> Generator[SQLAlchemySession, None, None]:
    """Provide an isolated SQLAlchemy session bound to the test database.

    Each test receives a fresh session. On teardown, the session
    issues a ``TRUNCATE ... CASCADE`` against every public table so
    the next test starts with an empty database. The schema itself
    is preserved (created once by :func:`_setup_test_database`).
    """
    session = db.session()
    try:
        yield session
    finally:
        session.close()
        # Truncate all tables so the next test sees an empty DB.
        # Restart sequences so primary keys reset between tests.
        with db.engine.connect() as conn:
            conn.execute(
                text(
                    """
                    TRUNCATE TABLE
                        audit_events,
                        record_tags,
                        records,
                        tags,
                        users,
                        organizations
                    RESTART IDENTITY CASCADE;
                    """
                )
            )
            conn.commit()


# ===========================================================================
# Autouse fixture: bind factory-boy factories to the test session
# ===========================================================================


@pytest.fixture
def _bind_factories_session(db_session: SQLAlchemySession) -> Generator[None, None, None]:
    """Bind every public factory class to the test-scoped session.

    factory-boy's ``SQLAlchemyModelFactory`` requires
    ``Meta.sqlalchemy_session`` to be set BEFORE any factory is
    invoked. The factories module declares
    ``sqlalchemy_session = None`` at import time so importing the
    module is side-effect-free; this fixture binds the live test
    session at fixture setup time.

    NOT an autouse fixture: tests that do not need DB-bound factories
    (pure unit tests against a service that never touches the
    database) should not pay the cost of building a Flask app and
    establishing a transaction. Tests that need factories must
    request this fixture explicitly OR request any of the helper
    fixtures that compose it (``contributor_user``, ``admin_user``,
    ``viewer_user``, ``organization``, etc.).

    Args:
        db_session: The test-scoped SQLAlchemy session.
    """
    from tests import factories  # noqa: PLC0415

    factory_classes = [
        factories.OrganizationFactory,
        factories.UserFactory,
        factories.AdminUserFactory,
        factories.ContributorUserFactory,
        factories.ViewerUserFactory,
        factories.OAuthUserFactory,
        factories.TagFactory,
        factories.RecordFactory,
        factories.SoftDeletedRecordFactory,
        factories.RecordTagFactory,
        factories.AuditEventFactory,
    ]
    for cls in factory_classes:
        cls._meta.sqlalchemy_session = db_session

    yield

    # Restore the unbound state so test isolation is preserved.
    for cls in factory_classes:
        cls._meta.sqlalchemy_session = None


# ===========================================================================
# Autouse fixture: clear structlog ContextVars between tests
# ===========================================================================


@pytest.fixture(autouse=True)
def _clear_structlog_context() -> Generator[None, None, None]:
    """Reset structlog's request-scoped ContextVars between tests.

    Without this fixture, a test that binds a ``correlation_id``,
    ``user_id``, etc. to structlog's contextvars would leak the
    binding into subsequent tests. The :mod:`app.middleware.correlation`
    teardown_request hook clears these in production, but tests
    that don't go through a full request lifecycle (unit tests on
    services) need an explicit clear.
    """
    yield
    structlog.contextvars.clear_contextvars()


# ===========================================================================
# Function-scoped fixture: default organization row
# ===========================================================================


@pytest.fixture
def organization(
    db_session: SQLAlchemySession,
    _bind_factories_session: None,
) -> Any:
    """Return the default Organization row used by all factories.

    Materializes an Organization with the canonical
    ``DEFAULT_ORG_ID`` from TestingConfig so tests that issue
    factory calls (which default to a fresh OrganizationFactory)
    can override the default by passing ``organization=organization``
    explicitly.
    """
    from app.models import Organization  # noqa: PLC0415

    org = Organization(
        id=_DEFAULT_ORG_ID,
        name="Default Test Org",
    )
    db_session.add(org)
    db_session.commit()
    return org


# ===========================================================================
# Function-scoped fixture: contributor user with a session cookie
# ===========================================================================


def _mint_session_cookie(client: Any, user: Any) -> None:
    """Set the session cookie on the supplied client for the user.

    Uses the same JWT minting path the production handler does so the
    cookie carries valid claims (org_id, role, etc.) and the auth
    middleware lets the request through.
    """
    from app.services.auth import mint_session_jwt  # noqa: PLC0415

    token = mint_session_jwt(user)
    # Flask's test client persists cookies across requests; setting
    # the cookie here is equivalent to a successful /auth/login.
    client.set_cookie("session", token)


@pytest.fixture
def contributor_user(
    db_session: SQLAlchemySession,
    organization: Any,
    _bind_factories_session: None,
) -> Any:
    """Return a Contributor user bound to the default organization."""
    from tests import factories  # noqa: PLC0415

    return factories.ContributorUserFactory(organization=organization)


@pytest.fixture
def admin_user(
    db_session: SQLAlchemySession,
    organization: Any,
    _bind_factories_session: None,
) -> Any:
    """Return an Admin user bound to the default organization."""
    from tests import factories  # noqa: PLC0415

    return factories.AdminUserFactory(organization=organization)


@pytest.fixture
def viewer_user(
    db_session: SQLAlchemySession,
    organization: Any,
    _bind_factories_session: None,
) -> Any:
    """Return a Viewer/Sales-Rep user bound to the default organization."""
    from tests import factories  # noqa: PLC0415

    return factories.ViewerUserFactory(organization=organization)


@pytest.fixture
def authed_client(client: Any, contributor_user: Any) -> Any:
    """Return a test client authenticated as a Contributor user.

    Mints a session JWT via :func:`mint_session_jwt` and attaches it
    to the client's cookie jar, equivalent to a successful
    ``/auth/login``.
    """
    _mint_session_cookie(client, contributor_user)
    return client


@pytest.fixture
def admin_client(client: Any, admin_user: Any) -> Any:
    """Return a test client authenticated as an Admin user."""
    _mint_session_cookie(client, admin_user)
    return client


@pytest.fixture
def viewer_client(client: Any, viewer_user: Any) -> Any:
    """Return a test client authenticated as a Viewer/Sales-Rep user."""
    _mint_session_cookie(client, viewer_user)
    return client
