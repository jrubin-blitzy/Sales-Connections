"""Tests for the Flask application factory in :mod:`app.__init__`.

This module exercises the public API contract of
:func:`app.create_app` and :func:`app._resolve_config_class`. The
factory is the canonical wire-up location for the entire backend
(see ``backend/app/__init__.py`` module docstring) so its public
contract is exercised here independently of the per-test
:func:`tests.conftest._build_test_app` helper.

Coverage targets per AAP Section 0.5.2 Layer 0:

* ``create_app(config_object)`` returns a fully-wired Flask
  application with all six API blueprints registered, the metrics
  endpoint mounted, the health probes reachable, and the model
  classes registered on ``Base.metadata``.
* ``_resolve_config_class`` accepts a config class, a dotted import
  string, or ``None`` (FLASK_ENV-driven) and rejects malformed
  inputs with the documented exception types.
* Public API exports (``create_app``, ``__version__``) are present
  and ``__version__`` matches the version pinned in
  ``backend/pyproject.toml``.
* Importing the ``app`` package does NOT eagerly construct a Flask
  application -- callers MUST invoke :func:`create_app` explicitly.

These tests do not exercise the database (TestingConfig points at
the test DSN but no queries are issued through the Flask test client
beyond the public ``/healthz`` / ``/readyz`` / ``/metrics`` probes
that are public per the AAP).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from flask import Flask
import pytest

import app as app_pkg
from app import _resolve_config_class, create_app
from app.config import (
    BaseConfig,
    DevelopmentConfig,
    ProductionConfig,
    TestingConfig,
)
from app.extensions import Base, db
import app.models as models_module

if TYPE_CHECKING:
    from collections.abc import Generator


# ===========================================================================
# Fixture: temporarily override FLASK_ENV
# ===========================================================================


@pytest.fixture
def flask_env() -> Generator[None, None, None]:
    """Yield then restore the prior ``FLASK_ENV`` value.

    Use this fixture to set ``os.environ['FLASK_ENV']`` inside a test
    body without leaking the change to subsequent tests. Tests that
    do NOT touch ``FLASK_ENV`` should not depend on this fixture.
    """
    prior = os.environ.get("FLASK_ENV")
    yield
    if prior is None:
        os.environ.pop("FLASK_ENV", None)
    else:
        os.environ["FLASK_ENV"] = prior


# ===========================================================================
# Fixture: factory_app -- wraps create_app with engine disposal
# ===========================================================================


@pytest.fixture
def factory_app() -> Generator[Flask, None, None]:
    """Build a Flask app via ``create_app(TestingConfig)`` and dispose on teardown.

    Unlike the conftest-level ``app`` fixture (which uses
    ``_build_test_app`` to mirror but not invoke the production
    factory), this fixture exercises the actual
    :func:`app.create_app` public API. It guarantees that the
    SQLAlchemy engine constructed during ``db.init_app(...)`` is
    disposed on teardown so test runs do not leak open psycopg
    connections.
    """
    flask_app = create_app(TestingConfig)
    yield flask_app
    # Dispose the SQLAlchemy engine so pooled psycopg connections
    # are returned to the OS rather than left open until garbage
    # collection (which raises a ResourceWarning under pytest's
    # default unraisable-exception detection).
    db.dispose()


# ===========================================================================
# TestPublicAPI: __version__, __all__, create_app importability
# ===========================================================================


class TestPublicAPI:
    """Validate the public API surface declared by ``app/__init__.py``."""

    def test_create_app_is_callable(self) -> None:
        """``app.create_app`` is importable and callable."""
        assert callable(create_app)

    def test_version_is_non_empty_string(self) -> None:
        """``app.__version__`` is a non-empty string."""
        assert isinstance(app_pkg.__version__, str)
        assert app_pkg.__version__

    def test_version_matches_pyproject(self) -> None:
        """``app.__version__`` matches the version pinned in pyproject.toml."""
        assert app_pkg.__version__ == "0.1.0"

    def test_all_lists_required_exports(self) -> None:
        """``__all__`` includes both required exports per the schema."""
        assert "create_app" in app_pkg.__all__
        assert "__version__" in app_pkg.__all__

    def test_module_does_not_export_flask_app(self) -> None:
        """Importing the package does not eagerly construct a Flask app.

        Per AAP Section 0.7.1 invariant 1 (stateless workers), the
        application factory MUST be invoked explicitly by callers
        (Gunicorn workers via :mod:`backend.wsgi`, tests via
        :mod:`backend.tests.conftest`). Importing the ``app`` package
        directly should NOT have any visible side effect beyond
        loading the factory function and version.
        """
        assert hasattr(app_pkg, "create_app")
        assert hasattr(app_pkg, "__version__")
        # No 'app' Flask instance attribute should exist at module scope.
        assert not hasattr(app_pkg, "app")


# ===========================================================================
# TestResolveConfigClass: argument-shape validation for the helper
# ===========================================================================


class TestResolveConfigClass:
    """Validate the four input shapes of :func:`_resolve_config_class`."""

    def test_class_argument_returned_unchanged(self) -> None:
        """Passing a config class returns the same class identity."""
        resolved = _resolve_config_class(TestingConfig)
        assert resolved is TestingConfig

    def test_dotted_string_resolves_to_class(self) -> None:
        """Passing a dotted import string returns the imported class."""
        resolved = _resolve_config_class("app.config.TestingConfig")
        assert resolved is TestingConfig

    def test_none_argument_with_flask_env_development(self, flask_env: None) -> None:
        """``None`` + ``FLASK_ENV=development`` resolves to DevelopmentConfig."""
        os.environ["FLASK_ENV"] = "development"
        resolved = _resolve_config_class(None)
        assert resolved is DevelopmentConfig

    def test_none_argument_with_flask_env_testing(self, flask_env: None) -> None:
        """``None`` + ``FLASK_ENV=testing`` resolves to TestingConfig."""
        os.environ["FLASK_ENV"] = "testing"
        resolved = _resolve_config_class(None)
        assert resolved is TestingConfig

    def test_none_argument_unset_flask_env_defaults_to_production(self, flask_env: None) -> None:
        """``None`` + unset ``FLASK_ENV`` defaults to ProductionConfig."""
        os.environ.pop("FLASK_ENV", None)
        resolved = _resolve_config_class(None)
        assert resolved is ProductionConfig

    def test_none_argument_unknown_flask_env_defaults_to_production(self, flask_env: None) -> None:
        """``None`` + unknown ``FLASK_ENV`` defaults to ProductionConfig."""
        os.environ["FLASK_ENV"] = "definitely-not-a-known-env-name"
        resolved = _resolve_config_class(None)
        assert resolved is ProductionConfig

    def test_invalid_dotted_string_raises_import_error(self) -> None:
        """A string with no dots raises ``ImportError``."""
        with pytest.raises(ImportError):
            _resolve_config_class("just_a_bare_word")

    def test_dotted_string_missing_attribute_raises_import_error(self) -> None:
        """A string referring to a missing attribute raises ``ImportError``."""
        with pytest.raises(ImportError):
            _resolve_config_class("app.config.DefinitelyDoesNotExistConfig")

    def test_non_subclass_class_raises_type_error(self) -> None:
        """A class not derived from BaseConfig raises ``TypeError``."""
        # ``str`` is a class but not a subclass of ``BaseConfig``.
        # The type-ignore comment below is intentional: this test
        # validates the runtime defensive check that exists precisely
        # because callers can bypass static typing (e.g., dotted-string
        # resolution can return any class object).
        with pytest.raises(TypeError):
            _resolve_config_class(str)  # type: ignore[arg-type]

    def test_baseconfig_itself_is_acceptable(self) -> None:
        """BaseConfig itself satisfies the subclass check (it is its own class)."""
        # ``BaseConfig`` is technically a subclass of itself per
        # Python's ``issubclass`` semantics. The factory accepts it.
        resolved = _resolve_config_class(BaseConfig)
        assert resolved is BaseConfig


# ===========================================================================
# TestCreateAppReturn: factory returns a fully-wired Flask instance
# ===========================================================================


class TestCreateAppReturn:
    """Validate the Flask app instance returned by ``create_app``."""

    def test_returns_flask_instance(self, factory_app: Flask) -> None:
        """``create_app(TestingConfig)`` returns a Flask instance."""
        assert isinstance(factory_app, Flask)

    def test_testing_config_sets_testing_flag(self, factory_app: Flask) -> None:
        """TestingConfig sets ``app.config['TESTING']`` to True."""
        assert factory_app.config.get("TESTING") is True

    def test_two_apps_are_distinct_instances(self) -> None:
        """Calling ``create_app`` twice yields two distinct apps."""
        try:
            app1 = create_app(TestingConfig)
            app2 = create_app(TestingConfig)
            assert app1 is not app2
            assert app1.config is not app2.config
        finally:
            db.dispose()

    def test_dotted_string_argument_works(self) -> None:
        """``create_app('app.config.TestingConfig')`` resolves and returns a Flask app."""
        try:
            app = create_app("app.config.TestingConfig")
            assert isinstance(app, Flask)
            assert app.config.get("TESTING") is True
        finally:
            db.dispose()

    def test_flask_env_drives_config_when_argument_omitted(self, flask_env: None) -> None:
        """Setting ``FLASK_ENV=testing`` lets ``create_app()`` pick TestingConfig."""
        os.environ["FLASK_ENV"] = "testing"
        try:
            app = create_app()
            assert app.config.get("TESTING") is True
        finally:
            db.dispose()


# ===========================================================================
# TestBlueprintRegistration: six required blueprints are registered
# ===========================================================================


class TestBlueprintRegistration:
    """Validate that every required API blueprint is registered."""

    def test_all_six_blueprints_registered(self, factory_app: Flask) -> None:
        """The six required blueprint names appear in ``app.blueprints``."""
        names = set(factory_app.blueprints.keys())
        required = {"auth", "connections", "notes", "tags", "admin", "health"}
        assert required.issubset(names), f"Missing blueprints: {required - names}"


# ===========================================================================
# TestObservabilityEndpoints: /healthz, /readyz, /metrics reachable
# ===========================================================================


class TestObservabilityEndpoints:
    """Validate the endpoints required by the AAP Observability rule."""

    def test_healthz_returns_200(self, factory_app: Flask) -> None:
        """``GET /healthz`` is a public liveness probe returning 200."""
        with factory_app.test_client() as client:
            response = client.get("/healthz")
        assert response.status_code == 200

    def test_readyz_returns_200_or_503(self, factory_app: Flask) -> None:
        """``GET /readyz`` is a public readiness probe returning 200 or 503."""
        with factory_app.test_client() as client:
            response = client.get("/readyz")
        # 200 if DB reachable, 503 otherwise. Both are contract-compliant.
        assert response.status_code in (200, 503)

    def test_metrics_endpoint_registered(self, factory_app: Flask) -> None:
        """``GET /metrics`` is reachable and returns Prometheus text."""
        with factory_app.test_client() as client:
            response = client.get("/metrics")
        # 200 (open) or 401 (bearer-token-protected); both indicate
        # the endpoint is mounted, which is what we are validating.
        assert response.status_code in (200, 401)
        if response.status_code == 200:
            content_type = response.headers.get("Content-Type", "")
            assert "text/plain" in content_type


# ===========================================================================
# TestModelRegistration: app.models import populates Base.metadata
# ===========================================================================


class TestModelRegistration:
    """Validate that all SQLAlchemy models are registered on Base.metadata."""

    def test_required_tables_in_metadata(self, factory_app: Flask) -> None:
        """The six core tables appear in ``Base.metadata.tables`` after factory."""
        # The ``factory_app`` fixture has already invoked
        # ``create_app``; importing ``app.models`` was a side effect of
        # that invocation, so every required table is registered on
        # the shared declarative base.
        _ = factory_app  # silence unused-arg lint
        names = set(Base.metadata.tables.keys())
        required = {
            "organizations",
            "users",
            "records",
            "tags",
            "record_tags",
            "audit_events",
        }
        assert required.issubset(names), f"Missing tables in Base.metadata: {required - names}"


# ===========================================================================
# TestShellContext: flask shell registers db and models bindings
# ===========================================================================


class TestShellContext:
    """Validate that ``flask shell`` exposes the project's runtime objects."""

    def test_shell_context_exposes_db(self, factory_app: Flask) -> None:
        """``flask shell`` context dictionary contains the db singleton."""
        ctx = factory_app.make_shell_context()
        assert "db" in ctx
        assert ctx["db"] is db

    def test_shell_context_exposes_models_module(self, factory_app: Flask) -> None:
        """``flask shell`` context dictionary contains the models package."""
        ctx = factory_app.make_shell_context()
        assert "models" in ctx
        assert ctx["models"] is models_module
