"""Pytest configuration and shared fixtures for the Sales-Connections test suite.

Fixture taxonomy
----------------
- **Application** (session-scoped):
    ``app``, ``db_engine``
- **Schema management** (session-scoped, autouse):
    ``_create_schema``
- **Per-test session isolation** (function-scoped, autouse for
  ``_bind_factories_session``):
    ``db_session``, ``_bind_factories_session``
- **Test client** (function-scoped):
    ``client``, ``authenticated_client``,
    ``admin_client``, ``contributor_client``, ``viewer_client``
- **Seeded entities** (function-scoped):
    ``organization``, ``admin_user``, ``contributor_user``, ``viewer_user``
- **Helpers** (function-scoped):
    ``audit_assertion``, ``mock_anthropic``, ``mock_google_oauth``,
    ``frozen_time``

Test isolation strategy
-----------------------
Tests run inside a SAVEPOINT nested under an enclosing connection-level
transaction. The ``db_session`` fixture replaces the application's
SQLAlchemy session factory with a sessionmaker bound to a single
test-scoped ``Connection`` configured with
``join_transaction_mode="create_savepoint"``. Every session produced
by either the test fixtures OR the production service-layer code
(``with db.session() as session:``) shares the same connection and
opens its transactional work as a SAVEPOINT under the outer
transaction. Service-layer ``commit()`` calls release the SAVEPOINT
(making writes visible at the connection level) without committing
the outer transaction. At test teardown the outer transaction is
rolled back, undoing every database write performed during the test.

This produces:
    - Fast tests (no schema rebuild per test, no TRUNCATE between tests).
    - Total isolation (one test's writes are invisible to subsequent
      tests).
    - Correct semantics for service code that relies on
      ``commit()``-then-read patterns (SAVEPOINTs preserve
      atomic-state-change-plus-audit invariants from AAP Section
      0.7.1).

Coverage of architectural invariants
------------------------------------
Every fixture in this file supports at least one architectural
invariant documented in AAP Section 0.7.1:

    1. Stateless workers (``app`` is constructed once per session via
       :func:`app.create_app`).
    2. Org-scoping (``organization`` is the canonical default org for
       all user/record fixtures and uses
       ``BaseConfig.DEFAULT_ORG_ID``).
    3. Soft-delete defaults (``RecordFactory.deleted_at = None``;
       ``SoftDeletedRecordFactory`` opts in explicitly).
    4. Append-only audit (``audit_assertion`` verifies the after-state
       row exists; combined with the SAVEPOINT pattern this validates
       atomic state-change + audit emission).
    5. Atomic state-change + audit pair (SAVEPOINT-based isolation
       preserves the same commit semantics that production runs).
    6. RBAC matrix (``admin_client``, ``contributor_client``,
       ``viewer_client`` cover all three roles).
    7. Owner attribution from session (clients carry server-derived
       JWT minted by ``app.services.auth.mint_session_jwt``).
    8. Server-side validation (``ValidationError`` -> 422 verified by
       dedicated tests using these clients).
"""

from __future__ import annotations

import contextlib
import os
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock, patch
import uuid

from freezegun import freeze_time
import pytest
import responses as responses_lib  # noqa: F401 -- reserved for HTTP-level OAuth/AI mocking
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session as SQLAlchemySession, sessionmaker
import structlog

from app import create_app
from app.extensions import Base, db, oauth
from app.models import AuditEvent, Organization
from app.services.auth import mint_session_jwt
from tests import factories
from tests.factories import (
    AdminUserFactory,
    ContributorUserFactory,
    OrganizationFactory,
    ViewerUserFactory,
)

if TYPE_CHECKING:
    from collections.abc import (
        Callable,
        Generator,
        Iterator,  # noqa: F401 -- reserved for iterator-style fixtures (see agent prompt)
    )

    from flask import Flask
    from flask.testing import FlaskClient
    from sqlalchemy.engine import Engine

    from app.models import User
    from app.models.enums import AuditEventType


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# Default Postgres DSN used when neither ``TEST_DATABASE_URL`` nor
# ``DATABASE_URL`` is set in the shell environment. The hostname matches
# the local docker-compose convention; the database name carries the
# ``_test`` suffix so production datasets are NEVER touched. CI overrides
# this via ``TEST_DATABASE_URL`` in ``.github/workflows/ci.yml``.
_DEFAULT_TEST_DATABASE_URL: str = (
    "postgresql+psycopg://sales_connections:sales_connections@localhost:5432/sales_connections_test"
)


def _resolve_test_database_url() -> str:
    """Pick the DSN for the test database.

    Priority order (highest first):

        1. ``TEST_DATABASE_URL`` env var — explicit test-DB override
           used by CI.
        2. ``DATABASE_URL`` env var — local-dev override; only used
           when ``TEST_DATABASE_URL`` is absent.
        3. :data:`_DEFAULT_TEST_DATABASE_URL` — the documented default
           that matches the docker-compose Postgres service.

    The function is pure: it does not mutate the environment, does
    not perform any I/O, and is safe to call from any context. The
    ``app`` fixture invokes it before constructing the Flask app so
    ``TestingConfig.DATABASE_URL`` (read at config-class creation
    time from ``TEST_DATABASE_URL``) and the test fixtures agree on
    the DSN.
    """
    return (
        os.environ.get("TEST_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or _DEFAULT_TEST_DATABASE_URL
    )


# Canonical default-org UUID matching ``BaseConfig.DEFAULT_ORG_ID``. The
# ``organization`` fixture seeds an Organization row with this exact
# UUID so single-org runtime endpoints (which look up the default org
# by id) find a real row to scope their queries against.
_DEFAULT_ORG_UUID: uuid.UUID = uuid.UUID("00000000-0000-0000-0000-000000000001")


# Tuple of every public factory class declared in :mod:`tests.factories`.
# Used by :func:`_bind_factories_session` to walk the full set and
# install / remove the per-test session binding. Keeping the tuple at
# module scope (rather than rebuilding it per test) means that adding
# a new factory class to the module is a one-line edit here.
_FACTORY_CLASSES: tuple[Any, ...] = (
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
)


# Names of the four PostgreSQL enum types created (and dropped) when the
# Alembic-fallback schema bootstrap runs. The values mirror the
# ``name=...`` argument on every ``SQLAlchemy.Enum(...)`` column
# declaration in ``app.models.*``; keeping them in one place makes the
# fallback path's DDL easy to audit.
_PG_ENUM_TYPES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("involvement_type", ("Warm Intro", "Soft Reference", "Target Only")),
    ("outreach_status", ("Not Started", "In Progress", "Contacted", "Closed")),
    ("user_role", ("Admin", "Contributor", "Viewer")),
    (
        "audit_event_type",
        (
            "create",
            "status_change",
            "edit",
            "soft_delete",
            "hard_delete",
            "role_change",
            "authentication",
            "admin_op",
        ),
    ),
)


# ---------------------------------------------------------------------------
# Pytest configuration hook
# ---------------------------------------------------------------------------


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers programmatically.

    The same markers are also declared in
    ``backend/pyproject.toml`` under
    ``[tool.pytest.ini_options].markers``; this hook ensures pytest
    does not warn about unknown markers if the config file is missed
    by some test runner (e.g., a contributor runs pytest directly
    against a single test file with ``--no-header --strict-markers``).
    Registering twice is a no-op for pytest's marker registry.
    """
    markers = (
        ("slow", "Long-running tests; deselected by default with -m 'not slow'."),
        ("integration", "Tests requiring real Postgres connectivity."),
        ("unit", "Pure unit tests with no I/O."),
        ("rbac", "RBAC permission-matrix tests."),
        ("audit", "Audit-trail invariant tests."),
    )
    for name, description in markers:
        config.addinivalue_line("markers", f"{name}: {description}")


# ---------------------------------------------------------------------------
# Helper functions (used by multiple fixtures)
# ---------------------------------------------------------------------------


def _set_session_cookie(
    test_client: FlaskClient,
    jwt_token: str,
    flask_app: Flask,
) -> None:
    """Attach the session JWT cookie to the test client.

    Mirrors the production cookie attributes set by
    :func:`app.api.auth._set_session_cookie` but uses the Flask 3.x
    test-client ``set_cookie`` API which accepts ``key`` and ``value``
    positionally followed by keyword-only attributes.

    Args:
        test_client: The :class:`flask.testing.FlaskClient` whose
            cookie jar receives the session JWT.
        jwt_token: The JWT string produced by
            :func:`app.services.auth.mint_session_jwt`.
        flask_app: The Flask application whose config provides the
            cookie name (``SESSION_COOKIE_NAME``; default
            ``"session"``).
    """
    cookie_name = flask_app.config.get("SESSION_COOKIE_NAME", "session")
    test_client.set_cookie(
        cookie_name,
        jwt_token,
        domain="localhost",
        path="/",
    )


def _make_authenticated_client(
    flask_app: Flask,
    base_client: FlaskClient,
    user: User,
) -> FlaskClient:
    """Mint a session JWT for ``user`` and attach it to ``base_client``.

    Uses the production minting function (``mint_session_jwt``) rather
    than constructing tokens manually so tests verify the SAME JWT
    format that real users carry, including ``user_id``, ``org_id``,
    ``role``, ``email``, ``display_name``, ``tv``, ``iat``, and
    ``exp`` claims.

    Args:
        flask_app: The Flask application whose config supplies the
            JWT signing key and cookie attributes.
        base_client: The unauthenticated test client to mutate.
        user: The :class:`User` ORM instance whose claims drive the
            token.

    Returns:
        The same ``base_client`` (returned for fluent fixture-style
        chaining) with the session cookie attached.
    """
    jwt_token = mint_session_jwt(user)
    _set_session_cookie(base_client, jwt_token, flask_app)
    return base_client


# ---------------------------------------------------------------------------
# Application and engine fixtures (session-scoped)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def app() -> Generator[Flask, None, None]:
    """Construct the Flask application once per test session.

    Uses ``TestingConfig`` which sets:

        - ``BCRYPT_COST = 4`` (fast password hashing)
        - ``JWT_SIGNING_KEY = "test-jwt-signing-key-do-not-use-in-prod"``
        - ``ANTHROPIC_API_KEY = ""`` (empty; AI tests must mock the
          service via the ``mock_anthropic`` fixture)
        - ``GOOGLE_OAUTH_CLIENT_ID/_SECRET = ""`` (empty; OAuth tests
          must mock via the ``mock_google_oauth`` fixture)
        - ``LOG_LEVEL = "WARNING"`` (suppress chatty INFO logs in
          test output)
        - ``SQLALCHEMY_DATABASE_URI`` -> ``TEST_DATABASE_URL`` env var
          (resolved by :func:`_resolve_test_database_url`)
        - ``USE_SECRETS_MANAGER = False`` (tests never touch AWS)

    The fixture pushes a long-lived ``app_context`` for the entire
    session so any test-fixture call to ``current_app.config`` (e.g.,
    inside :func:`app.services.auth.mint_session_jwt`) succeeds
    without each test having to push its own context.

    Construction goes through :func:`app.create_app` so the test app
    exercises the EXACT same wiring (config -> extensions ->
    middleware -> blueprints -> observability) that the production
    app uses. This is required by AAP Section 0.5.3 ("Tests construct
    test apps with an in-memory or per-test PostgreSQL database via
    the same factory.").
    """
    # Ensure ``DATABASE_URL`` matches the resolved test DSN before any
    # additional code path that reads it. ``TestingConfig.DATABASE_URL``
    # was already evaluated at class-definition time from
    # ``TEST_DATABASE_URL``; setting ``DATABASE_URL`` here is defensive
    # for any code that reads ``os.environ["DATABASE_URL"]`` directly.
    resolved_dsn = _resolve_test_database_url()
    os.environ["DATABASE_URL"] = resolved_dsn

    flask_app = create_app(config_object="app.config.TestingConfig")
    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False

    # Override DATABASE_URL on the constructed app so the SQLAlchemy
    # engine (which was initialized inside create_app from
    # ``TestingConfig.DATABASE_URL``) and the test's resolved DSN are
    # aligned even if the developer set ``TEST_DATABASE_URL`` AFTER
    # the config class was loaded by some earlier import.
    flask_app.config["DATABASE_URL"] = resolved_dsn
    # If the engine was created against a different DSN than the
    # resolved one, dispose and reinitialize. ``db.engine.url`` is the
    # SQLAlchemy URL object representing the bound DSN.
    if str(db.engine.url) != resolved_dsn:
        db.dispose()
        db.init_app(flask_app)

    with flask_app.app_context():
        yield flask_app


@pytest.fixture(scope="session")
def db_engine() -> Generator[Engine, None, None]:
    """Yield a session-scoped SQLAlchemy engine pointing at the test DB.

    The engine is shared across the entire test session; per-test
    isolation is achieved at the connection/transaction level via
    the :func:`db_session` fixture which opens a connection on this
    engine, begins an outer transaction, and replaces the
    application's session factory with one bound to that connection.

    Implementation note: this fixture intentionally does NOT depend
    on the :func:`app` fixture. Test modules under
    ``tests/services/`` may override :func:`app` at function scope
    to construct minimal Flask apps for service-layer testing; if
    :func:`db_engine` depended on the session-scoped :func:`app`,
    pytest would raise ``ScopeMismatch`` when a function-scoped
    test in those modules requests a fixture that transitively
    depends on :func:`db_engine`. By building the engine directly
    from the resolved DSN, this fixture decouples the database
    layer from Flask's application-factory wiring.

    The engine is disposed at session teardown so the connection
    pool is closed cleanly even when pytest exits abnormally.
    """
    engine = create_engine(_resolve_test_database_url())
    try:
        yield engine
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Schema setup and teardown (session-scoped, autouse)
# ---------------------------------------------------------------------------


def _drop_test_schema(engine: Engine) -> None:
    """Drop every application table and PostgreSQL enum type.

    Idempotent: uses ``IF EXISTS`` clauses so calling against an
    empty database is a no-op. Tables are dropped first via
    :meth:`Base.metadata.drop_all` (which discovers the right DROP
    order from foreign-key relationships); then a defensive manual
    ``DROP TYPE IF EXISTS ... CASCADE`` sweep removes any enum types
    left behind from a previous aborted run.

    The defensive enum sweep is required because:

        - In SQLAlchemy 2.x, the generic :class:`sqlalchemy.Enum`
          class does NOT define a ``create_type`` parameter; the
          ``create_type=False`` kwarg used in the model declarations
          (per the migration's manual-DDL convention) is silently
          absorbed via ``**kwargs``. As a side effect,
          :meth:`Base.metadata.drop_all` DOES drop the enum types
          via its metadata-level listeners. The manual sweep is
          therefore usually redundant — but cheap and defensive,
          and required when an Alembic migration created the schema
          with ``create_type=False`` honored at the dialect level
          (in which case :meth:`drop_all` would NOT drop the
          types).

    Running this at session start removes leftovers from a previous
    aborted run; running it at session end leaves the DB clean for
    the next run.
    """
    # Tables first; ``Base.metadata.drop_all`` discovers them via the
    # SQLAlchemy declarative registry and emits the right DROP order.
    # When the metadata layer drops enum types via its before/after
    # listeners (the ``create_type=False`` flag is silently ignored
    # by the generic ``sqlalchemy.Enum``), this also removes them.
    Base.metadata.drop_all(bind=engine, checkfirst=True)
    # Defensive enum sweep: handle the case where a prior Alembic
    # migration created the enums under a separate transaction and
    # ``metadata.drop_all`` did not pick them up (e.g., if the model
    # registry was not loaded when the previous run created the
    # types, or if ``create_type=False`` was honored under a
    # different SQLAlchemy version).
    with engine.begin() as conn:
        for enum_name, _values in _PG_ENUM_TYPES:
            conn.execute(text(f"DROP TYPE IF EXISTS {enum_name} CASCADE"))


def _create_test_schema(engine: Engine) -> None:
    """Create every application table and PostgreSQL enum type.

    Bypasses Alembic for speed: ``Base.metadata.create_all`` emits
    DDL directly from the declarative model registry, which is
    significantly faster than running every migration sequentially
    and does not require alembic.ini, env.py, or migration scripts
    to be importable from the test process.

    The enum types are created automatically as part of
    :meth:`Base.metadata.create_all` via SQLAlchemy's
    metadata-level ``before_create`` listener on each Enum-typed
    column. The ``create_type=False`` kwarg on the model
    declarations is silently absorbed by the generic
    :class:`sqlalchemy.Enum` constructor (the parameter exists only
    on the dialect-specific :class:`sqlalchemy.dialects.postgresql.ENUM`
    class), so the metadata layer DOES create them — exactly what
    we want for a fast in-test schema bootstrap that does not need
    Alembic.

    ``checkfirst=True`` is used as a defensive measure so concurrent
    test sessions or a partial prior-run cleanup do not produce
    ``DuplicateObject`` errors.
    """
    Base.metadata.create_all(bind=engine, checkfirst=True)
    _provision_app_role_privileges(engine)


def _provision_app_role_privileges(engine: Engine) -> None:
    """Mirror the Alembic migration's audit-table privilege grants.

    Per AAP section 0.7.4 (Security invariants):

        "Audit table writes restricted to backend service identity
        at the database privilege layer. The application role can
        ``INSERT`` only; ``UPDATE`` and ``DELETE`` are revoked."

    The Alembic migration (``0001_initial_schema``) issues these
    GRANT/REVOKE statements as the final phase of its DDL, so a
    schema produced by ``alembic upgrade head`` carries the
    privileges. Because :func:`_create_test_schema` uses
    :meth:`Base.metadata.create_all` instead of Alembic for speed,
    the privileges would not otherwise be set, and integration
    tests that connect AS the app role to verify the
    immutability invariant (``test_app_role_can_insert_audit_events``,
    ``test_app_role_cannot_update_audit_events``,
    ``test_app_role_cannot_delete_audit_events``) would fail with
    misleading ``InsufficientPrivilege`` errors.

    The grants are applied only when the ``sales_connections_app``
    role (or the role named in ``APP_DB_ROLE``) exists; absence of
    the role is a no-op so test sessions in environments without
    role provisioning (e.g., ephemeral Postgres containers) do not
    fail to bootstrap. Errors during the GRANT/REVOKE are logged
    via the stdlib ``logging`` module and otherwise swallowed:
    integration tests that depend on the privilege configuration
    will surface a clear ``permission denied`` error if the grants
    cannot be applied.

    Args:
        engine: A SQLAlchemy engine connected as a role with enough
            privileges to ``GRANT`` and ``REVOKE`` on the
            ``audit_events`` table. Typically the same engine used
            for schema setup.
    """
    app_role = os.environ.get("APP_DB_ROLE", "sales_connections_app")
    try:
        with engine.begin() as conn:
            # Skip silently when the role does not exist so test
            # sessions on minimal Postgres containers (without
            # pre-provisioned roles) still bootstrap.
            role_exists = conn.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = :rolname"),
                {"rolname": app_role},
            ).scalar()
            if not role_exists:
                return

            # GRANT SELECT, INSERT on every application table so the
            # app role can read records and emit audit events.
            for table_name in (
                "organizations",
                "users",
                "records",
                "tags",
                "record_tags",
                "audit_events",
            ):
                conn.execute(text(f'GRANT SELECT, INSERT ON TABLE {table_name} TO "{app_role}"'))

            # GRANT UPDATE/DELETE on the mutable application tables.
            # Records and tags are mutable; users and organizations
            # are mutated only through admin endpoints that connect
            # as the same role.
            for table_name in (
                "organizations",
                "users",
                "records",
                "tags",
                "record_tags",
            ):
                conn.execute(text(f'GRANT UPDATE, DELETE ON TABLE {table_name} TO "{app_role}"'))

            # REVOKE UPDATE, DELETE, TRUNCATE on audit_events to
            # enforce the append-only invariant at the database
            # privilege layer (AAP §0.7.1 invariant 5, §0.7.4
            # security invariant).
            conn.execute(
                text(f'REVOKE UPDATE, DELETE, TRUNCATE ON TABLE audit_events FROM "{app_role}"')
            )
    except Exception as exc:  # pragma: no cover -- best-effort grant
        # Tests that depend on the privilege configuration will
        # surface a clear ``permission denied`` if the grants were
        # not applied, so swallowing the GRANT failure here is
        # safer than crashing the whole test session.
        import logging as _logging  # noqa: PLC0415

        _logging.getLogger(__name__).warning(
            "test_app_role_privileges_provisioning_failed: %s",
            type(exc).__name__,
        )


@pytest.fixture(scope="session", autouse=True)
def _create_schema() -> Generator[None, None, None]:
    """Create the schema once per session; drop at session end.

    Uses ``Base.metadata.create_all`` plus :func:`_drop_test_schema`
    (rather than ``alembic upgrade head``) because:

        1. Speed: ``metadata.create_all`` is faster than running
           every migration sequentially. The test suite is run
           thousands of times during development; per-second savings
           add up to per-day savings.
        2. Independence: the schema definition lives in the model
           classes, not the migration scripts. Tests should validate
           against the models directly so a faulty migration cannot
           silently produce a schema that disagrees with the model
           layer.

    A separate dedicated test (``tests/services/test_migrations.py``)
    compares the migration-produced schema against the model schema
    so the two layers are kept in sync, but that test is independent
    from the rest of the suite.

    Implementation note: this fixture intentionally does NOT depend
    on the :func:`db_engine` (and transitively on :func:`app`)
    fixtures. It builds and disposes its own bootstrap engine
    directly from the resolved DSN. This decoupling is critical
    because test modules under ``tests/services/`` may override
    :func:`app` at function scope to construct minimal Flask apps
    with bespoke configuration; if :func:`_create_schema` depended
    on :func:`db_engine` -> :func:`app`, pytest would raise
    ``ScopeMismatch`` when fingerprinting the fixture chain for any
    test in those modules. The schema setup is at the database
    level, so a separate engine here points at the same database
    that the production fixtures (:func:`db_engine`,
    :func:`db_session`, :func:`client`) connect to via the
    application-factory wiring.
    """
    bootstrap_engine = create_engine(_resolve_test_database_url())
    try:
        # Drop any leftover schema from a previously-aborted run so
        # the CREATE statements below cannot fail on
        # ``DuplicateObject: type "involvement_type" already exists``.
        _drop_test_schema(bootstrap_engine)
        _create_test_schema(bootstrap_engine)
    finally:
        # The bootstrap engine is no longer needed for the running
        # session: production fixtures will use ``db.engine`` (set
        # up by :func:`app`) which has its own connection pool. We
        # dispose the bootstrap engine immediately to avoid leaving
        # idle connections open in the connection pool.
        bootstrap_engine.dispose()

    yield

    # Teardown: drop everything so the next session starts clean.
    # Build a fresh bootstrap engine for the teardown DDL because
    # the original was disposed at setup. Failure here is non-fatal:
    # pytest is already exiting and a leftover schema will be
    # cleaned up by the next session's setup. ``contextlib.suppress``
    # discards the exception silently while keeping SIM105 happy.
    with contextlib.suppress(Exception):  # pragma: no cover -- best-effort teardown
        teardown_engine = create_engine(_resolve_test_database_url())
        try:
            _drop_test_schema(teardown_engine)
        finally:
            teardown_engine.dispose()


# ---------------------------------------------------------------------------
# Per-test database session (SAVEPOINT pattern)
# ---------------------------------------------------------------------------


@pytest.fixture
def db_session(
    app: Flask,
    db_engine: Engine,
) -> Generator[SQLAlchemySession, None, None]:
    """Yield an isolated SQLAlchemy session for one test.

    Implementation: open a connection, begin an outer transaction,
    install a sessionmaker bound to that connection with
    ``join_transaction_mode="create_savepoint"`` as the application's
    ``db._session_factory``. Every session produced by either the
    test fixtures OR by service-layer code (which calls
    ``db.session()``) shares the same connection and opens its
    transactional work as a SAVEPOINT under the outer transaction.

    Service-layer ``commit()`` calls release the SAVEPOINT (making
    writes visible at the connection level) without committing the
    outer transaction. At test teardown the outer transaction is
    rolled back, undoing every database write performed during the
    test.

    Reference pattern: SQLAlchemy 2.x docs, "Joining a Session into
    an External Transaction (such as for test suites)".

    The yielded session is the same instance the test interacts with
    directly via ``db_session.query(...)`` etc. Service code that
    creates its own sessions via ``db.session()`` produces NEW
    Session instances that share the connection — this is correct
    and required for testing transactional service-layer behavior.

    Args:
        app: The session-scoped Flask app fixture (depended on so
            the SQLAlchemy engine is already initialized).
        db_engine: The session-scoped engine fixture.

    Yields:
        A SAVEPOINT-isolated SQLAlchemy session bound to the test
        connection. The session is closed and the outer transaction
        rolled back at fixture teardown.
    """
    connection = db_engine.connect()
    # Begin the outer transaction. All test writes happen inside this
    # transaction; the rollback at teardown discards them all.
    transaction = connection.begin()

    # Build a sessionmaker bound to this specific connection.
    # ``join_transaction_mode="create_savepoint"`` is the SQLAlchemy
    # 2.x mechanism for "joining a Session into an external
    # transaction": every Session begun on this connection emits its
    # transactional work as a SAVEPOINT under the outer transaction
    # rather than as a fresh top-level BEGIN, so service-layer
    # ``commit()`` calls release the SAVEPOINT without committing the
    # outer transaction.
    test_session_factory = sessionmaker(
        bind=connection,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        class_=SQLAlchemySession,
        join_transaction_mode="create_savepoint",
    )

    # Replace the application's session factory with the test factory.
    # Service code that calls ``db.session()`` (which reads
    # ``self._session_factory``) now gets sessions bound to the test
    # connection.
    original_factory = db._session_factory
    db._session_factory = test_session_factory

    # Construct a session for the test to use directly. This is the
    # same session yielded to the test body; factories bound via
    # :func:`_bind_factories_session` use it for their commits.
    test_session = test_session_factory()

    try:
        yield test_session
    finally:
        # Tear down: close the test session, roll back the outer
        # transaction, close the connection, and restore the original
        # session factory. Order matters: closing the session before
        # rolling back the transaction discards any in-flight
        # SAVEPOINT state cleanly. Session.close() may raise in
        # exotic states (engine already disposed, connection
        # killed); silence it via ``contextlib.suppress`` so the
        # transaction rollback always runs.
        with contextlib.suppress(Exception):  # pragma: no cover -- best-effort cleanup
            test_session.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()
        db._session_factory = original_factory


# ---------------------------------------------------------------------------
# Factory session binding (function-scoped, autouse)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _bind_factories_session(
    db_session: SQLAlchemySession,
) -> Generator[None, None, None]:
    """Bind every public factory class to the test-scoped session.

    factory-boy's ``SQLAlchemyModelFactory`` reads
    ``Meta.sqlalchemy_session`` at instantiation time; setting it
    here ensures every factory call within the test commits its rows
    to the SAVEPOINT-isolated test session, so the writes are rolled
    back automatically at test teardown.

    This fixture is autouse: tests do NOT need to declare it
    explicitly. Without the binding, calling any factory would raise
    ``factory.errors.FactoryError: Can't store an object without a
    session.``

    Also clears any structlog ``ContextVars`` after the test so
    correlation IDs / user IDs / org IDs do not leak between tests.
    The :mod:`app.middleware.correlation` teardown_request hook
    clears them in production, but tests that exercise services
    directly (without going through a full request lifecycle) need
    an explicit clear.
    """
    for cls in _FACTORY_CLASSES:
        cls._meta.sqlalchemy_session = db_session

    try:
        yield
    finally:
        # Restore the unbound state so test isolation is preserved
        # across function-scoped fixture invocations. Factories that
        # are accidentally invoked outside a test (e.g., in a module
        # body) will raise the documented "no session" error rather
        # than silently writing to a stale connection.
        for cls in _FACTORY_CLASSES:
            cls._meta.sqlalchemy_session = None
        # Clear structlog request-scoped contextvars so subsequent
        # tests do not see the previous test's correlation_id /
        # user_id bindings.
        structlog.contextvars.clear_contextvars()


# ---------------------------------------------------------------------------
# Flask test client fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client(
    app: Flask,
    db_session: SQLAlchemySession,
) -> Generator[FlaskClient, None, None]:
    """Yield a Flask test client (unauthenticated).

    Depending on ``db_session`` ensures the SAVEPOINT-based test
    isolation is active before any HTTP request runs, so handlers
    that touch the database see the test transaction (not the
    production engine's pool).

    Use ``admin_client``, ``contributor_client``, ``viewer_client``,
    or ``authenticated_client`` for tests that need a session cookie
    attached.

    Implementation note: this fixture resets the Flask
    ``_got_first_request`` flag to ``False`` BEFORE yielding the
    test client. Without this reset, tests that need to dynamically
    register additional routes on the shared session-scoped ``app``
    (e.g., probe-route helpers in ``tests/middleware/test_auth.py``)
    would fail with ``AssertionError: setup method 'route' can no
    longer be called`` on every test after the first. Resetting
    the flag is safe because the test session re-uses the SAME
    URL map for every request — the rule that "routes added after
    the first request are not applied consistently" applies to
    long-lived production servers, not isolated pytest invocations
    where each test creates its own URL state.
    """
    # Reset the "first request handled" flag so tests can register
    # routes per test. ``_got_first_request`` is a documented Flask
    # private attribute that gates ``_check_setup_finished`` in
    # ``flask/sansio/app.py``; resetting it allows ``app.route(...)``
    # decorators to succeed on already-used apps.
    app._got_first_request = False
    with app.test_client() as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Default org and role-specific user fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def organization(db_session: SQLAlchemySession) -> Organization:
    """Return the default Organization seeded with the canonical UUID.

    Uses ``BaseConfig.DEFAULT_ORG_ID`` so single-org runtime
    endpoints (which look up the default org by id) find a real row
    to scope their queries against.

    Idempotent: when an Organization with the canonical UUID already
    exists in the test session (e.g., because a previous fixture in
    the same test seeded it via :class:`OrganizationFactory`), the
    existing row is returned. Otherwise a new row is created via
    :class:`OrganizationFactory` and committed to the SAVEPOINT.
    """
    existing = db_session.get(Organization, _DEFAULT_ORG_UUID)
    if existing is not None:
        return existing
    # ``OrganizationFactory()`` returns an :class:`Organization` ORM
    # instance at runtime per factory-boy's ``Meta.model`` contract;
    # the static type system does not model this so an explicit
    # ``cast`` is required to keep mypy strict-mode happy without
    # weakening the fixture's typed return signature.
    org = cast("Organization", OrganizationFactory(id=_DEFAULT_ORG_UUID, name="Test Organization"))
    db_session.commit()
    return org


@pytest.fixture
def admin_user(
    db_session: SQLAlchemySession,
    organization: Organization,
) -> User:
    """An ``Admin`` user in the default organization.

    Used to cover the AAP RBAC matrix (Section 0.7.1 invariant 6).
    The user is bound to the canonical default org via the
    ``org=organization`` SubFactory override, so a SubFactory chain
    does not silently create a different organization.
    """
    # Factory-boy returns the ``Meta.model`` instance at runtime; mypy
    # cannot infer this so a ``cast`` carries the typed contract.
    user = cast("User", AdminUserFactory(org=organization))
    db_session.commit()
    return user


@pytest.fixture
def contributor_user(
    db_session: SQLAlchemySession,
    organization: Organization,
) -> User:
    """A ``Contributor`` user in the default organization.

    The default user role per AAP Section 0.7.6
    (``DEFAULT_NEW_USER_ROLE``). Most authenticated tests use this
    role.
    """
    user = cast("User", ContributorUserFactory(org=organization))
    db_session.commit()
    return user


@pytest.fixture
def viewer_user(
    db_session: SQLAlchemySession,
    organization: Organization,
) -> User:
    """A ``Viewer`` (Sales Rep) user in the default organization.

    Sales Reps can browse all records in the organization and
    mutate the ``outreach_status`` field on any record. They cannot
    edit other users' record fields or access the admin panel.
    """
    user = cast("User", ViewerUserFactory(org=organization))
    db_session.commit()
    return user


# ---------------------------------------------------------------------------
# Authenticated test client fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_client(
    app: Flask,
    client: FlaskClient,
    admin_user: User,
) -> FlaskClient:
    """Test client carrying a valid JWT for the ``admin_user``.

    Issues the SAME JWT format that production users carry (HS256,
    8-hour TTL, claims include user_id/org_id/role/email/
    display_name/tv/iat/exp), via :func:`app.services.auth.mint_session_jwt`.
    The cookie is set with the production cookie name (from
    ``SESSION_COOKIE_NAME`` config; default ``"session"``).
    """
    return _make_authenticated_client(app, client, admin_user)


@pytest.fixture
def contributor_client(
    app: Flask,
    client: FlaskClient,
    contributor_user: User,
) -> FlaskClient:
    """Test client carrying a valid JWT for the ``contributor_user``.

    The Contributor role is the default for authenticated tests; the
    :func:`authenticated_client` fixture aliases this one for
    contexts where the specific role does not matter.
    """
    return _make_authenticated_client(app, client, contributor_user)


@pytest.fixture
def viewer_client(
    app: Flask,
    client: FlaskClient,
    viewer_user: User,
) -> FlaskClient:
    """Test client carrying a valid JWT for the ``viewer_user``.

    The Viewer role corresponds to the "Sales Rep" user type per
    AAP Section 0.5.2 Layer 1. Tests that exercise the
    status-mutation paths or the read-only browsing paths use this
    fixture.
    """
    return _make_authenticated_client(app, client, viewer_user)


@pytest.fixture
def authenticated_client(contributor_client: FlaskClient) -> FlaskClient:
    """Default authenticated client (alias for ``contributor_client``).

    Most tests use this fixture when they don't care about the
    specific role; the Contributor role represents the most common
    authenticated user in the system. Tests that need a specific
    role should request ``admin_client``, ``contributor_client``, or
    ``viewer_client`` directly.
    """
    return contributor_client


@pytest.fixture
def authed_client(contributor_client: FlaskClient) -> FlaskClient:
    """Short-name alias for ``contributor_client`` / ``authenticated_client``.

    Some sibling test modules under ``tests/api/`` adopt ``authed_client``
    as a more concise fixture name with the same semantics as
    :func:`authenticated_client`: a Flask test client carrying a valid
    JWT for a Contributor-role user in the default organization.
    Exposing the alias here lets those test modules import the fixture
    by their preferred name without forcing a project-wide rename.

    Returns:
        The same test client as :func:`contributor_client`.
    """
    return contributor_client


# ---------------------------------------------------------------------------
# Audit-trail assertion helper
# ---------------------------------------------------------------------------


@pytest.fixture
def audit_assertion(
    db_session: SQLAlchemySession,
) -> Callable[..., AuditEvent]:
    """Return a helper that asserts an audit event was emitted.

    Usage in a state-mutation test::

        def test_create_record_emits_audit(contributor_client, audit_assertion):
            response = contributor_client.post("/api/connections", json=valid_payload)
            assert response.status_code == 201
            record_id = response.get_json()["id"]
            audit = audit_assertion(
                event_type=AuditEventType.CREATE,
                target_record_id=uuid.UUID(record_id),
            )
            assert audit.actor_user_id is not None

    The helper queries ``audit_events`` for the most-recent row
    matching the provided filters and raises ``AssertionError`` if
    none is found. Returning the row lets callers chain additional
    assertions on its ``before_payload``, ``after_payload``, or
    ``event_timestamp``.

    This fixture realizes AAP Section 0.7.7 invariant: "Every
    state-changing endpoint emits an audit event. Verified by a
    pytest fixture that asserts an audit row exists at the end of
    each state-mutation test."

    Returns:
        A callable ``_assert_audit_emitted(*, event_type,
        target_record_id=None, actor_user_id=None) -> AuditEvent``.
    """

    def _assert_audit_emitted(
        *,
        event_type: AuditEventType,
        target_record_id: uuid.UUID | None = None,
        actor_user_id: uuid.UUID | None = None,
    ) -> AuditEvent:
        """Find the most-recent matching audit event or raise.

        Args:
            event_type: The :class:`AuditEventType` to filter by.
                Required.
            target_record_id: Optional record id to filter by.
                Used for record-keyed events (CREATE, EDIT,
                STATUS_CHANGE, SOFT_DELETE, HARD_DELETE).
                Authentication and admin events typically have
                ``target_record_id IS NULL``.
            actor_user_id: Optional actor id to filter by. Useful
                when the test seeds multiple users and wants to
                assert that the audit row was emitted under a
                specific user's identity.

        Returns:
            The matching :class:`AuditEvent` ORM instance, the most
            recent one when multiple match.

        Raises:
            AssertionError: When no row matches the supplied filters.
                The error message names every filter for fast
                triage.
        """
        stmt = db_session.query(AuditEvent).filter(AuditEvent.event_type == event_type)
        if target_record_id is not None:
            stmt = stmt.filter(AuditEvent.target_record_id == target_record_id)
        if actor_user_id is not None:
            stmt = stmt.filter(AuditEvent.actor_user_id == actor_user_id)
        stmt = stmt.order_by(AuditEvent.event_timestamp.desc())
        row = stmt.first()
        assert row is not None, (
            f"Expected an audit_events row with event_type={event_type.value}"
            f" target_record_id={target_record_id} actor_user_id={actor_user_id},"
            f" but none was found. The test mutation may have failed or the"
            f" service-layer audit emission may have been skipped."
        )
        return row

    return _assert_audit_emitted


# ---------------------------------------------------------------------------
# External-service mocks
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_anthropic() -> Generator[MagicMock, None, None]:
    """Mock the Anthropic Claude API so AI tests do not hit the real service.

    Patches :func:`app.services.ai_orchestration.generate_outreach_notes`
    to return a deterministic :class:`NoteGenerationResponse`.
    Tests can override the return value via the yielded
    ``MagicMock`` instance.

    Usage::

        def test_ai_path(mock_anthropic, contributor_client):
            mock_anthropic.return_value = NoteGenerationResponse(
                ai_notes="Deterministic test text",
                model="claude-test",
                generated_at=datetime.now(timezone.utc),
            )
            # ... call /api/notes/generate or service code ...
            assert mock_anthropic.called

    The default response uses a sentinel string so test assertions
    against the unmocked-default response can detect "AI was called
    but the test didn't override the mock" cases distinctly from
    "AI was never called".

    Lazy imports of :class:`NoteGenerationResponse` and
    :class:`datetime` keep the conftest module's top-level import
    surface minimal; both are only needed inside this fixture body
    when a test actually requests AI mocking.
    """
    # Lazy imports per the agent prompt: these are only needed when a
    # test actually requests the fixture. Importing them at module
    # scope would couple every test (including unit tests with no AI
    # path) to the schema package's import graph.
    from datetime import UTC, datetime  # noqa: PLC0415

    from app.schemas import NoteGenerationResponse  # noqa: PLC0415

    default_response = NoteGenerationResponse(
        ai_notes="Test AI notes (mocked).",
        model="claude-mock-test",
        generated_at=datetime.now(UTC),
    )
    with patch(
        "app.services.ai_orchestration.generate_outreach_notes",
        return_value=default_response,
    ) as mock:
        yield mock


@pytest.fixture
def mock_google_oauth(
    app: Flask,
) -> Generator[dict[str, MagicMock], None, None]:
    """Mock the Authlib Google OAuth client and the user-upsert helper.

    Patches three callables so OAuth tests do not hit the real
    Google endpoints:

        - ``oauth.google.authorize_redirect`` returns a Flask redirect
          to a fake Google authorization URL.
        - ``oauth.google.authorize_access_token`` returns a fake
          token dict containing ``access_token``, ``id_token``, and
          ``expires_in``.
        - :func:`app.services.auth.upsert_oauth_user` returns
          whatever the test sets via
          ``mock_google_oauth["upsert_oauth_user"].return_value``.

    Yields a dict mapping callable names to their ``MagicMock``
    instances so tests can override return values and inspect call
    counts::

        def test_oauth_callback(mock_google_oauth, client, organization):
            user = OAuthUserFactory(org=organization)
            mock_google_oauth["upsert_oauth_user"].return_value = user
            response = client.get("/auth/google/callback?code=test")
            assert mock_google_oauth["authorize_access_token"].called

    Implementation note: the mocks are installed via
    ``patch.object(oauth, "google", create=True, ...)`` because the
    ``google`` attribute resolves through Authlib's ``__getattr__``
    -> ``create_client`` lookup; instance-level attribute assignment
    (which ``patch.object`` performs) shadows the ``__getattr__``
    fallback so the mock wins for the duration of the patch context.
    The ``create=True`` flag is necessary because TestingConfig has
    empty Google OAuth credentials, so ``init_oauth_clients``
    skipped registration and ``oauth.google`` does not yet exist.
    """
    from flask import redirect as flask_redirect  # noqa: PLC0415

    # Construct a MagicMock that emulates the registered Google OAuth
    # client. Authlib's real client exposes ``authorize_redirect`` and
    # ``authorize_access_token`` methods; the mock provides matching
    # attributes so service-layer code that calls them produces
    # deterministic test values rather than HTTP requests.
    mock_google = MagicMock(name="oauth.google")
    mock_google.authorize_redirect = MagicMock(
        return_value=flask_redirect("https://accounts.google.com/o/oauth2/v2/auth?fake_state=test")
    )
    mock_google.authorize_access_token = MagicMock(
        return_value={
            "access_token": "fake_access_token",
            "id_token": "fake_id_token_jwt",
            "expires_in": 3600,
            "userinfo": {
                "sub": "fake_google_subject",
                "email": "test.oauth.user@example.com",
                "email_verified": True,
                "name": "Test OAuth User",
            },
        }
    )

    # Patch the ``google`` attribute on the module-level ``oauth``
    # singleton AND the ``upsert_oauth_user`` helper in a single
    # ``with`` block. ``create=True`` on the first patch is required
    # because TestingConfig has empty Google OAuth credentials and
    # ``init_oauth_clients`` therefore skipped registration without
    # ``create=True``; ``patch.object`` would raise
    # ``AttributeError: <OAuth> does not have the attribute 'google'``.
    # Tests that want a specific User return value override
    # ``mock_google_oauth["upsert_oauth_user"].return_value``
    # explicitly.
    with (
        patch.object(oauth, "google", mock_google, create=True),
        patch("app.services.auth.upsert_oauth_user") as mock_upsert,
    ):
        yield {
            "google": mock_google,
            "authorize_redirect": mock_google.authorize_redirect,
            "authorize_access_token": mock_google.authorize_access_token,
            "upsert_oauth_user": mock_upsert,
        }


# ---------------------------------------------------------------------------
# Time-mocking helper
# ---------------------------------------------------------------------------


@pytest.fixture
def frozen_time() -> Callable[..., Any]:
    """Return :func:`freezegun.freeze_time` for explicit time control.

    The fixture is a thin wrapper that returns the ``freeze_time``
    callable directly, so tests use it as a context manager or
    decorator the same way they would with the library function::

        def test_audit_timestamp_deterministic(frozen_time, db_session):
            with frozen_time("2026-01-01 12:00:00"):
                # ... operation that emits audit row ...
                pass
            audit = db_session.query(AuditEvent).first()
            assert audit.event_timestamp.isoformat().startswith("2026-01-01T12:00:00")

    Required by AAP Section 0.7.1 invariant 5 (atomic state-change
    + audit pair) verification with deterministic timestamps.

    Returns:
        The :func:`freezegun.freeze_time` callable. Tests should
        treat the return value as opaque and use it via the standard
        freezegun API.
    """
    return freeze_time
