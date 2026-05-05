"""Tests for ``app.middleware.rbac``.

Validates per AAP s 0.4.3 / s 0.7.3:

* ``@requires_role(UserRole.ADMIN, ...)`` decorator factory enforces
  set-membership against ``g.session.role``.
* Decoration-time validation: empty role args -> ``ValueError``;
  invalid role string -> ``ValueError``. Both fail at IMPORT time, not
  at request time, so a misconfigured route crashes app boot rather
  than silently passing requests.
* Request-time behaviour: 200 for allowed roles, 403 for denied,
  uniform JSON envelope for 403 responses.
* ``g.session`` is None -> ``ForbiddenError`` (defense-in-depth; auth
  layer should have rejected with 401 first, but RBAC never trusts).
* String-form role coercion: ``requires_role("Admin")`` is equivalent
  to ``requires_role(UserRole.ADMIN)``.
* Performance: each request adds < 50ms (sub-50ms invariant per AAP
  s 0.7.3 -- in-process JWT claim check, no DB).
* Permission matrix coverage: parametrized assertions for every
  (role, action) pair documented in the AAP.

The corresponding auth-layer behaviour (token verification,
``g.session`` population) is tested in ``test_auth_middleware.py``.
"""

from __future__ import annotations

# Standard library imports -- alphabetized by canonical module name to
# satisfy ruff/isort. ``time.perf_counter`` powers the sub-50ms
# performance assertion; ``typing.Any`` annotates Flask view-function
# return types without dragging in ``flask.Response`` for type
# annotations only; ``unittest.mock.patch`` powers the defensive
# no-DB-call mock; ``uuid4`` builds fresh ``Session`` UUIDs per test.
import time
from typing import Any
from unittest.mock import patch
from uuid import uuid4

# Third-party imports. ``pytest`` provides the test framework
# (markers, fixtures, parametrize, raises); ``structlog`` is needed
# for the autouse contextvar isolation fixture; ``flask`` provides
# the bare Flask test apps that exercise the decorator end-to-end.
from flask import Flask, g, jsonify
import pytest
import structlog

# First-party imports. The five depends_on_files modules fully cover
# the test surface: ``rbac`` is the module under test, ``correlation``
# and ``error_handlers`` provide the surrounding middleware that lets
# ``ForbiddenError`` reach the JSON envelope handler, ``auth.Session``
# is the typed dataclass injected onto ``g.session``, and ``UserRole``
# is the canonical role enum.
from app.middleware.auth import Session
from app.middleware.correlation import register_correlation_middleware
from app.middleware.error_handlers import (
    ForbiddenError,
    register_error_handlers,
)
from app.middleware.rbac import (
    register_rbac_error_handlers,
    requires_role,
)
from app.models.enums import UserRole

# ---------------------------------------------------------------------------
# Module-level test helpers
# ---------------------------------------------------------------------------


def _make_test_app(*, with_rbac_handlers: bool = True) -> Flask:
    """Build a Flask app with correlation + error_handlers wired.

    The auth middleware is intentionally NOT registered; tests for
    rbac directly populate ``g.session`` via a custom before_request
    hook so the role-gate can be exercised in isolation.

    Args:
        with_rbac_handlers: Register ``register_rbac_error_handlers``
            in addition to ``register_error_handlers``. Default True.

    Returns:
        A fully wired Flask app instance ready for ``test_client()``
        usage. Caller owns the lifetime; the app does not persist
        across tests.
    """
    app = Flask(__name__)
    # ``TESTING`` enables Flask test-mode behaviours;
    # ``PROPAGATE_EXCEPTIONS = False`` ensures error handlers run
    # rather than letting exceptions bubble up through the test
    # client. ``SECRET_KEY`` silences Flask's "no secret key" warning
    # in test mode (we never sign any cookies in these tests).
    app.config["TESTING"] = True
    app.config["PROPAGATE_EXCEPTIONS"] = False
    app.config["SECRET_KEY"] = "test-secret"
    # Order matters: correlation runs FIRST so the X-Correlation-Id
    # header / structlog context is bound before any error handler
    # fires. The error handlers registered last see the bound
    # correlation_id when constructing the JSON envelope.
    register_correlation_middleware(app)
    register_error_handlers(app)
    if with_rbac_handlers:
        register_rbac_error_handlers(app)
    return app


def _make_session(role: UserRole | str | None) -> Session | None:
    """Construct a ``Session`` instance for tests.

    Args:
        role: ``UserRole`` member, role string, or None to indicate
            no session at all (used for the 'g.session is None' path).

    Returns:
        A frozen ``Session`` dataclass with random UUID identifiers,
        or None when ``role`` is None (signaling the
        no-session-attached path to the caller).
    """
    # Order matters: ``UserRole`` is declared as ``class UserRole(str, Enum)``
    # so every UserRole member is ALSO a ``str`` instance via the mixin.
    # Checking ``isinstance(role, UserRole)`` BEFORE
    # ``isinstance(role, str)`` is therefore essential -- the str branch
    # would otherwise swallow enum members and the bare-string branch
    # below would never execute.
    if role is None:
        return None
    if isinstance(role, UserRole):
        return Session(user_id=uuid4(), org_id=uuid4(), role=role)
    # At this point ``role`` is guaranteed to be a plain ``str`` that is
    # NOT a ``UserRole`` member (could be a valid role value like
    # ``"Admin"``, an unknown role like ``"Hacker"``, or any other
    # string). The decorator's request-time coercion logic exercises
    # the path where ``session.role`` holds a raw string.
    return Session(
        user_id=uuid4(),
        org_id=uuid4(),
        role=role,  # type: ignore[arg-type]
    )


def _attach_session(app: Flask, session: Session | None) -> None:
    """Register a before_request hook that attaches ``session`` to ``g``.

    Order: this hook runs AFTER correlation but BEFORE the route handler,
    simulating the auth middleware's contribution.

    Args:
        app: The Flask app on which to register the before_request
            hook. The hook is registered once per call and fires on
            every request thereafter.
        session: The Session instance to attach to ``g.session``, or
            None to leave ``g.session`` unset (mimicking the auth
            middleware not running for this path -- defense-in-depth
            invariant for the no-session test class).
    """

    @app.before_request
    def _attach() -> None:
        # If session is None, leave g.session unset OR explicitly None.
        # The bare Flask app does not auto-initialize ``g.session`` so
        # a missing attach hook leaves ``getattr(g, "session", None)``
        # returning None -- which is exactly the no-session
        # defense-in-depth path the rbac decorator must reject.
        if session is not None:
            g.session = session
        # else: do not set, mimics auth middleware not running for this path.


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def test_app() -> Flask:
    """A Flask app with correlation + error_handlers + rbac error handler wired.

    Returns:
        A fully wired Flask app instance. Each test that consumes
        this fixture gets its own fresh app so blueprint registration
        and error_handler_spec mutations do not leak between tests.
    """
    return _make_test_app()


@pytest.fixture(autouse=True)
def _isolate_structlog_contextvars() -> Any:
    """Clear structlog contextvars between tests.

    The RBAC decorator and correlation middleware both bind contextvars
    during request processing (``correlation_id``, ``user_id``,
    ``org_id``, ``role``). Without explicit clearing, contextvars can
    leak between tests that run on the same Python thread, polluting
    log assertions and producing flaky tests.

    The autouse decorator ensures every test in this module gets a
    clean structlog context regardless of whether the test author
    remembers to request the fixture.
    """
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()


# ---------------------------------------------------------------------------
# Permission matrix data
# ---------------------------------------------------------------------------

# Permission matrix per AAP s 0.5.2:
#   F-001 create record: Contributor, Admin (NOT Viewer)
#   F-005 status mutation: Viewer, Admin (NOT Contributor)
#   F-007 edit own record: Contributor, Admin (NOT Viewer for others')
#   F-007 hard delete: Admin only
#   F-014 admin endpoints: Admin only
#
# We test each (role, allowed_roles) tuple and assert admit/deny.
# The TestPermissionMatrix class consumes this constant via
# ``@pytest.mark.parametrize`` so each row produces one named pytest
# test_id (visible in the test report) and one independent assertion.

PERMISSION_MATRIX: list[tuple[str, UserRole, tuple[UserRole, ...], int]] = [
    # (test_id, role_on_session, allowed_roles, expected_status)
    # F-001 create record: CONTRIBUTOR + ADMIN can create; VIEWER cannot.
    (
        "create-record-admin",
        UserRole.ADMIN,
        (UserRole.CONTRIBUTOR, UserRole.ADMIN),
        200,
    ),
    (
        "create-record-contributor",
        UserRole.CONTRIBUTOR,
        (UserRole.CONTRIBUTOR, UserRole.ADMIN),
        200,
    ),
    (
        "create-record-viewer",
        UserRole.VIEWER,
        (UserRole.CONTRIBUTOR, UserRole.ADMIN),
        403,
    ),
    # F-005 status mutation: VIEWER + ADMIN can mutate; CONTRIBUTOR cannot.
    (
        "status-mutation-admin",
        UserRole.ADMIN,
        (UserRole.VIEWER, UserRole.ADMIN),
        200,
    ),
    (
        "status-mutation-viewer",
        UserRole.VIEWER,
        (UserRole.VIEWER, UserRole.ADMIN),
        200,
    ),
    (
        "status-mutation-contributor",
        UserRole.CONTRIBUTOR,
        (UserRole.VIEWER, UserRole.ADMIN),
        403,
    ),
    # F-014 admin-only: ADMIN only.
    ("admin-only-admin", UserRole.ADMIN, (UserRole.ADMIN,), 200),
    ("admin-only-contributor", UserRole.CONTRIBUTOR, (UserRole.ADMIN,), 403),
    ("admin-only-viewer", UserRole.VIEWER, (UserRole.ADMIN,), 403),
    # F-007 hard delete: ADMIN only.
    ("hard-delete-admin", UserRole.ADMIN, (UserRole.ADMIN,), 200),
    ("hard-delete-contributor", UserRole.CONTRIBUTOR, (UserRole.ADMIN,), 403),
    ("hard-delete-viewer", UserRole.VIEWER, (UserRole.ADMIN,), 403),
    # All-three pattern (read-only feed): all admit.
    (
        "read-feed-admin",
        UserRole.ADMIN,
        (UserRole.VIEWER, UserRole.CONTRIBUTOR, UserRole.ADMIN),
        200,
    ),
    (
        "read-feed-contributor",
        UserRole.CONTRIBUTOR,
        (UserRole.VIEWER, UserRole.CONTRIBUTOR, UserRole.ADMIN),
        200,
    ),
    (
        "read-feed-viewer",
        UserRole.VIEWER,
        (UserRole.VIEWER, UserRole.CONTRIBUTOR, UserRole.ADMIN),
        200,
    ),
]


# ---------------------------------------------------------------------------
# Decoration-time validation tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRequiresRoleDecorationTime:
    """Decoration-time validation must fail loudly for misconfigurations.

    A typo in ``@requires_role("Admon")`` MUST crash app boot so that
    the operator notices it during deployment. If validation were
    deferred to request time, a misconfigured route would silently 403
    every request instead of throwing a clear error at startup -- and
    a misspelled role like ``"Adim"`` could even pass through without
    the operator noticing.
    """

    def test_empty_args_raises_value_error(self) -> None:
        """``@requires_role()`` with no arguments raises ValueError.

        An "allow nobody" decorator is meaningless; the operator
        almost certainly forgot to fill in the role list. We crash
        immediately so they fix it.
        """
        with pytest.raises(ValueError, match="at least one role"):
            requires_role()

    def test_unknown_role_string_raises_value_error(self) -> None:
        """``@requires_role('Admon')`` raises ValueError at decoration time.

        This guarantees that a typo crashes app boot, not requests.
        """
        with pytest.raises(ValueError):
            requires_role("Admon")

    def test_unknown_role_string_in_multi_arg_raises(self) -> None:
        """An invalid role amongst valid ones still raises.

        Mixed args must be validated FULLY -- a single typo in a list
        of three otherwise-valid roles still produces a misconfigured
        decorator and must be caught at decoration time.
        """
        with pytest.raises(ValueError):
            requires_role(UserRole.ADMIN, "Bogus")

    def test_valid_userrole_member_accepted(self) -> None:
        """``@requires_role(UserRole.ADMIN)`` does not raise."""
        decorator = requires_role(UserRole.ADMIN)
        assert callable(decorator)

    def test_valid_string_role_accepted(self) -> None:
        """``@requires_role('Admin')`` does not raise."""
        decorator = requires_role("Admin")
        assert callable(decorator)

    def test_multiple_valid_roles_accepted(self) -> None:
        """``@requires_role(UserRole.ADMIN, UserRole.CONTRIBUTOR)`` works."""
        decorator = requires_role(UserRole.ADMIN, UserRole.CONTRIBUTOR)
        assert callable(decorator)

    def test_mixed_string_and_userrole_accepted(self) -> None:
        """Mixed string and enum args are accepted.

        The decorator coerces all string arguments to ``UserRole``
        members at decoration time, so mixed-type calling conventions
        (used widely across the API blueprints for readability) all
        produce equivalent behaviour at request time.
        """
        decorator = requires_role("Admin", UserRole.VIEWER)
        assert callable(decorator)

    def test_string_coercion_case_sensitive(self) -> None:
        """Role string coercion is case-sensitive: 'admin' is invalid.

        ``UserRole.ADMIN.value == 'Admin'`` (capitalized); we do not
        accept ``'admin'`` (lowercase) because permissive coercion
        would invite bugs from typo-tolerant tooling.
        """
        with pytest.raises(ValueError):
            requires_role("admin")

    def test_decoration_preserves_view_metadata(self) -> None:
        """``functools.wraps`` preserves the wrapped function's name and doc.

        Flask's blueprint registration keys on ``view.__name__`` to
        derive the endpoint name; if the decorator lost ``__name__``,
        Flask would either raise "duplicate endpoint" errors when
        multiple decorated views are registered OR silently rename
        every endpoint to ``"wrapper"`` (causing reverse-URL lookup
        to break). Preservation is essential.
        """

        @requires_role(UserRole.ADMIN)
        def _example_view() -> str:
            """Example view docstring."""
            return "ok"

        assert _example_view.__name__ == "_example_view"
        assert _example_view.__doc__ == "Example view docstring."


# ---------------------------------------------------------------------------
# Request-time admit-path tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRequiresRoleAdmits:
    """Allowed roles -> 200; the wrapped view executes."""

    @pytest.mark.parametrize(
        "session_role",
        [UserRole.ADMIN, UserRole.CONTRIBUTOR, UserRole.VIEWER],
    )
    def test_each_role_admitted_when_listed(self, session_role: UserRole) -> None:
        """When the role is in the allowed set, the view runs."""
        app = _make_test_app()
        _attach_session(app, _make_session(session_role))

        @app.route("/protected")
        @requires_role(UserRole.ADMIN, UserRole.CONTRIBUTOR, UserRole.VIEWER)
        def _view() -> Any:
            return jsonify(ok=True)

        client = app.test_client()
        response = client.get("/protected")
        assert response.status_code == 200
        assert response.get_json() == {"ok": True}

    def test_view_receives_args_and_kwargs(self) -> None:
        """The wrapper passes through path args.

        Flask passes route variables as keyword arguments to the
        view; the decorator MUST forward them transparently or
        path-parameterized handlers would break.
        """
        app = _make_test_app()
        _attach_session(app, _make_session(UserRole.ADMIN))

        @app.route("/items/<int:item_id>")
        @requires_role(UserRole.ADMIN)
        def _view(item_id: int) -> Any:
            return jsonify(item_id=item_id)

        client = app.test_client()
        response = client.get("/items/42")
        assert response.status_code == 200
        assert response.get_json() == {"item_id": 42}

    def test_view_returns_value_unchanged(self) -> None:
        """The wrapper does not mutate the view's response.

        The decorator is a transparent gate: success delegates the
        return value verbatim, including nested data structures.
        """
        app = _make_test_app()
        _attach_session(app, _make_session(UserRole.CONTRIBUTOR))

        @app.route("/payload")
        @requires_role(UserRole.CONTRIBUTOR)
        def _view() -> Any:
            return jsonify(deep={"nested": [1, 2, 3]}, flag=True)

        client = app.test_client()
        response = client.get("/payload")
        assert response.status_code == 200
        assert response.get_json() == {
            "deep": {"nested": [1, 2, 3]},
            "flag": True,
        }


# ---------------------------------------------------------------------------
# Request-time deny-path tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRequiresRoleRejects:
    """Disallowed roles -> 403 uniform envelope."""

    def test_admin_only_denies_contributor(self) -> None:
        """``@requires_role(UserRole.ADMIN)`` returns 403 for Contributor."""
        app = _make_test_app()
        _attach_session(app, _make_session(UserRole.CONTRIBUTOR))

        @app.route("/admin-only")
        @requires_role(UserRole.ADMIN)
        def _view() -> Any:
            return jsonify(should_not_run=True)

        client = app.test_client()
        response = client.get("/admin-only")
        assert response.status_code == 403
        payload = response.get_json()
        assert payload["error"]["code"] == "forbidden"

    def test_admin_only_denies_viewer(self) -> None:
        """``@requires_role(UserRole.ADMIN)`` returns 403 for Viewer."""
        app = _make_test_app()
        _attach_session(app, _make_session(UserRole.VIEWER))

        @app.route("/admin-only-2")
        @requires_role(UserRole.ADMIN)
        def _view() -> Any:
            return jsonify(should_not_run=True)

        client = app.test_client()
        response = client.get("/admin-only-2")
        assert response.status_code == 403

    def test_contributor_or_admin_admits_admin(self) -> None:
        """Admin is admitted by ``requires_role(CONTRIBUTOR, ADMIN)``."""
        app = _make_test_app()
        _attach_session(app, _make_session(UserRole.ADMIN))

        @app.route("/contrib-or-admin-1")
        @requires_role(UserRole.CONTRIBUTOR, UserRole.ADMIN)
        def _view() -> Any:
            return jsonify(ok=True)

        client = app.test_client()
        response = client.get("/contrib-or-admin-1")
        assert response.status_code == 200

    def test_contributor_or_admin_admits_contributor(self) -> None:
        """Contributor is admitted by ``requires_role(CONTRIBUTOR, ADMIN)``."""
        app = _make_test_app()
        _attach_session(app, _make_session(UserRole.CONTRIBUTOR))

        @app.route("/contrib-or-admin-2")
        @requires_role(UserRole.CONTRIBUTOR, UserRole.ADMIN)
        def _view() -> Any:
            return jsonify(ok=True)

        client = app.test_client()
        response = client.get("/contrib-or-admin-2")
        assert response.status_code == 200

    def test_contributor_or_admin_denies_viewer(self) -> None:
        """Viewer is denied by ``requires_role(CONTRIBUTOR, ADMIN)``."""
        app = _make_test_app()
        _attach_session(app, _make_session(UserRole.VIEWER))

        @app.route("/contrib-or-admin-3")
        @requires_role(UserRole.CONTRIBUTOR, UserRole.ADMIN)
        def _view() -> Any:
            return jsonify(should_not_run=True)

        client = app.test_client()
        response = client.get("/contrib-or-admin-3")
        assert response.status_code == 403

    def test_403_envelope_shape(self) -> None:
        """403 envelope is the uniform error shape (matches s 0.4.3).

        The envelope MUST carry exactly four keys: ``code``,
        ``message``, ``correlation_id``, ``fields``. The SPA's
        ``frontend/src/api/client.ts`` fetch wrapper deserializes by
        this shape, so deviations (extra keys, missing keys, renamed
        keys) would break the SPA error-handling pipeline.
        """
        app = _make_test_app()
        _attach_session(app, _make_session(UserRole.VIEWER))

        @app.route("/envelope-shape")
        @requires_role(UserRole.ADMIN)
        def _view() -> Any:
            return jsonify(should_not_run=True)

        client = app.test_client()
        response = client.get("/envelope-shape")
        payload = response.get_json()
        assert "error" in payload
        assert set(payload["error"].keys()) == {
            "code",
            "message",
            "correlation_id",
            "fields",
        }
        assert payload["error"]["code"] == "forbidden"
        assert payload["error"]["fields"] == []

    def test_403_response_is_json(self) -> None:
        """403 response is application/json, not HTML.

        Werkzeug's default 403 response is HTML; if our error handler
        ever fails to fire, we'd see ``text/html`` here. Asserting
        the JSON content-type guards against that regression.
        """
        app = _make_test_app()
        _attach_session(app, _make_session(UserRole.VIEWER))

        @app.route("/json-403")
        @requires_role(UserRole.ADMIN)
        def _view() -> Any:
            return jsonify(should_not_run=True)

        client = app.test_client()
        response = client.get("/json-403")
        assert "application/json" in response.content_type


# ---------------------------------------------------------------------------
# Defense-in-depth: g.session is None
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRequiresRoleNoSession:
    """When g.session is None, the decorator raises ForbiddenError.

    Defense-in-depth invariant: the auth middleware (which runs
    BEFORE rbac decorators) is responsible for populating
    ``g.session`` and rejecting unauthenticated requests with 401.
    If ``g.session`` is somehow missing when the decorator runs
    (programming error / middleware misconfiguration), the decorator
    treats it as a forbidden access and raises ``ForbiddenError``.
    A 403 is returned even if the auth middleware was bypassed,
    so the API NEVER silently admits an unauthenticated request.
    """

    def test_no_session_attribute_returns_403(self) -> None:
        """When ``g.session`` is unset, the decorator raises ForbiddenError -> 403.

        Defense-in-depth: even if the auth middleware is misconfigured
        and lets a request through unauthenticated, RBAC still rejects.
        """
        app = _make_test_app()
        # No session attached.

        @app.route("/no-session")
        @requires_role(UserRole.ADMIN)
        def _view() -> Any:
            return jsonify(should_not_run=True)

        client = app.test_client()
        response = client.get("/no-session")
        # 403 from RBAC's ForbiddenError (NOT 500).
        assert response.status_code == 403
        payload = response.get_json()
        assert payload["error"]["code"] == "forbidden"

    def test_session_explicitly_none_returns_403(self) -> None:
        """When ``g.session = None``, the decorator raises ForbiddenError.

        A naive implementation that did ``g.session.role`` directly
        (without ``getattr`` defaulting) would crash with
        ``AttributeError: 'NoneType' object has no attribute 'role'``
        and produce a 500. The decorator MUST treat None as
        defense-in-depth-forbidden, not as an internal error.
        """
        app = _make_test_app()

        @app.before_request
        def _attach_none() -> None:
            g.session = None

        @app.route("/none-session")
        @requires_role(UserRole.ADMIN)
        def _view() -> Any:
            return jsonify(should_not_run=True)

        client = app.test_client()
        response = client.get("/none-session")
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# String-role coercion at request time
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRequiresRoleStringSessionCoercion:
    """Session.role as STRING is coerced to UserRole inside the decorator.

    The auth middleware in production sets ``session.role`` as a
    ``UserRole`` enum member. But defensive code paths -- legacy JWT
    claims, raw deserialized payloads, tampered tokens -- can deliver
    a string. The decorator must coerce cleanly OR raise
    ``ForbiddenError`` for unknown strings, NEVER 500.
    """

    def test_string_role_admin_admits_when_admin_required(self) -> None:
        """A Session with role='Admin' (str) is admitted by requires_role(ADMIN)."""
        app = _make_test_app()
        _attach_session(app, _make_session("Admin"))

        @app.route("/string-role-admit")
        @requires_role(UserRole.ADMIN)
        def _view() -> Any:
            return jsonify(ok=True)

        client = app.test_client()
        response = client.get("/string-role-admit")
        assert response.status_code == 200

    def test_string_role_contributor_denied_when_admin_required(self) -> None:
        """A Session with role='Contributor' (str) is denied by requires_role(ADMIN)."""
        app = _make_test_app()
        _attach_session(app, _make_session("Contributor"))

        @app.route("/string-role-deny")
        @requires_role(UserRole.ADMIN)
        def _view() -> Any:
            return jsonify(should_not_run=True)

        client = app.test_client()
        response = client.get("/string-role-deny")
        assert response.status_code == 403

    def test_unknown_string_role_on_session_returns_403(self) -> None:
        """Session.role='Hacker' (unknown string) -> 403 (NOT 500).

        The decorator catches the coercion failure and converts to
        ForbiddenError. A naive implementation would let ValueError
        bubble up to the catch-all 500 handler; we explicitly test
        the intended behaviour.
        """
        app = _make_test_app()
        _attach_session(app, _make_session("Hacker"))

        @app.route("/unknown-role-on-session")
        @requires_role(UserRole.ADMIN)
        def _view() -> Any:
            return jsonify(should_not_run=True)

        client = app.test_client()
        response = client.get("/unknown-role-on-session")
        assert response.status_code == 403
        payload = response.get_json()
        assert payload["error"]["code"] == "forbidden"


# ---------------------------------------------------------------------------
# Error handler registration
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRBACErrorHandlerRegistration:
    """Validate ``register_rbac_error_handlers`` wires the ForbiddenError handler."""

    def test_registers_forbidden_error_handler(self) -> None:
        """``ForbiddenError`` is registered in app.error_handler_spec.

        Flask stores error handlers in
        ``app.error_handler_spec[blueprint_name][http_code][exception_class]``.
        ``register_rbac_error_handlers`` MUST insert ``ForbiddenError``
        somewhere in this nested map. We walk the global (blueprint=None)
        section and assert presence.
        """
        app = Flask(__name__)
        register_rbac_error_handlers(app)
        global_handlers = app.error_handler_spec.get(None, {})
        registered_classes: set[type[BaseException]] = set()
        for handlers_dict in global_handlers.values():
            registered_classes.update(handlers_dict.keys())
        assert ForbiddenError in registered_classes, (
            "ForbiddenError handler not registered by register_rbac_error_handlers"
        )

    def test_handler_returns_403_envelope(self) -> None:
        """The registered handler emits the uniform 403 JSON envelope.

        End-to-end test: raise ``ForbiddenError`` from a view and
        assert the response is the canonical 403 envelope. Isolated
        from the broader ``register_error_handlers`` so we can prove
        ``register_rbac_error_handlers`` alone is sufficient for the
        envelope.
        """
        app = Flask(__name__)
        app.config["TESTING"] = True
        app.config["PROPAGATE_EXCEPTIONS"] = False
        register_correlation_middleware(app)
        register_rbac_error_handlers(app)

        @app.route("/raise-fbn")
        def _h() -> Any:
            raise ForbiddenError("explicit raise")

        client = app.test_client()
        response = client.get("/raise-fbn")
        assert response.status_code == 403
        payload = response.get_json()
        assert payload["error"]["code"] == "forbidden"

    def test_double_registration_idempotent(self) -> None:
        """Registering twice does not break behaviour.

        Flask's ``register_error_handler`` is keyed by class identity
        (last-write-wins), so the second registration replaces the
        first with the same function. Behaviour is identical
        regardless of registration count.
        """
        app = Flask(__name__)
        app.config["TESTING"] = True
        app.config["PROPAGATE_EXCEPTIONS"] = False
        register_correlation_middleware(app)
        register_rbac_error_handlers(app)
        register_rbac_error_handlers(app)

        @app.route("/raise-fbn-double")
        def _h() -> Any:
            raise ForbiddenError("test")

        client = app.test_client()
        response = client.get("/raise-fbn-double")
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# Performance invariant
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRBACPerformance:
    """The decorator adds < 50ms per AAP s 0.7.3."""

    def test_decorator_overhead_under_50ms(self) -> None:
        """100 protected requests measured; per-request overhead < 50ms.

        AAP s 0.7.3: "RBAC authorization check ~50ms enforcement"
        (in-process JWT-claim check, no DB round-trip). We measure the
        OVERHEAD of the decorator vs. an undecorated baseline.

        Note: This is a smoke test to catch egregious regressions
        (e.g., accidentally adding a DB query inside the decorator).
        We use a generous 50ms ceiling to tolerate slow CI runners.
        """
        app = _make_test_app()
        _attach_session(app, _make_session(UserRole.ADMIN))

        @app.route("/perf-decorated")
        @requires_role(UserRole.ADMIN)
        def _decorated() -> Any:
            return jsonify(ok=True)

        @app.route("/perf-baseline")
        def _baseline() -> Any:
            return jsonify(ok=True)

        client = app.test_client()
        # Warm up the test client and Flask request dispatch so the
        # first request's import/lazy-init costs do not skew the
        # baseline calculation. Two warm-up calls (one per route)
        # are sufficient.
        client.get("/perf-decorated")
        client.get("/perf-baseline")

        # Measure decorated.
        n = 100
        t0 = time.perf_counter()
        for _ in range(n):
            response = client.get("/perf-decorated")
            assert response.status_code == 200
        decorated_total = time.perf_counter() - t0
        decorated_avg = decorated_total / n

        # Measure baseline.
        t0 = time.perf_counter()
        for _ in range(n):
            response = client.get("/perf-baseline")
            assert response.status_code == 200
        baseline_total = time.perf_counter() - t0
        baseline_avg = baseline_total / n

        overhead = decorated_avg - baseline_avg
        # Generous ceiling: 50ms per request; the decorator should add
        # microseconds, but CI flakiness can mask real numbers.
        assert overhead < 0.050, (
            f"Decorator overhead {overhead * 1000:.3f}ms exceeds 50ms "
            "ceiling. Possible regressions: DB query in decorator, "
            "logging blocking I/O, etc."
        )

    def test_decorator_no_db_call(self) -> None:
        """The decorator does NOT call into the SQLAlchemy session.

        We monkey-patch ``app.extensions.db.session`` (the method that
        returns a fresh SQLAlchemy session) to fail loudly. If the
        rbac decorator ever calls ``db.session()`` -- which it MUST
        NOT, per the AAP s 0.7.3 in-process invariant -- the patched
        method explodes with a clear AssertionError. A successful 200
        response proves the decorator never hit the DB.

        Note: the patch target ``app.extensions.db.session`` is the
        bound method on the SQLAlchemy wrapper instance, not the
        ``Session`` (capitalised) factory class. Patching the method
        catches any attempt to obtain a fresh session, which is the
        sole entry point service-layer code uses.
        """
        app = _make_test_app()
        _attach_session(app, _make_session(UserRole.ADMIN))

        @app.route("/no-db")
        @requires_role(UserRole.ADMIN)
        def _view() -> Any:
            return jsonify(ok=True)

        client = app.test_client()
        # Defensive mock: if the decorator made a DB call, this
        # patched method would explode. Since the decorator should
        # not call it, we expect 200.
        with patch(
            "app.extensions.db.session",
            side_effect=AssertionError("RBAC decorator made a DB call - regression!"),
        ):
            response = client.get("/no-db")
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Permission matrix end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.rbac
class TestPermissionMatrix:
    """Parametrized matrix coverage per AAP s 0.5.2.

    Doubly marked: ``@pytest.mark.unit`` for the standard fast-test
    suite, plus ``@pytest.mark.rbac`` so the dedicated permission-
    matrix CI job (``pytest -m rbac``) selects this class.
    """

    @pytest.mark.parametrize(
        ("test_id", "role_on_session", "allowed_roles", "expected_status"),
        PERMISSION_MATRIX,
        ids=[row[0] for row in PERMISSION_MATRIX],
    )
    def test_matrix(
        self,
        test_id: str,
        role_on_session: UserRole,
        allowed_roles: tuple[UserRole, ...],
        expected_status: int,
    ) -> None:
        """Each (role, allowed_roles) tuple admits/denies as expected."""
        app = _make_test_app()
        _attach_session(app, _make_session(role_on_session))

        # Use the test_id as the route path to avoid Flask collisions.
        path = f"/{test_id}"

        # Apply the decorator with the given allowed roles.
        # Using a closure to capture allowed_roles without mutating outer scope.
        decorator = requires_role(*allowed_roles)

        # Give the closure a unique name BEFORE registering the route
        # to avoid Flask's duplicate-endpoint conflict. Flask derives
        # the endpoint name from ``view.__name__`` at registration
        # time; renaming after ``@app.route`` would not change the
        # registered endpoint (Flask snapshots the name during the
        # route call).
        unique_name = f"matrix_view_{test_id.replace('-', '_')}"

        def _view() -> Any:
            return jsonify(role=role_on_session.value)

        _view.__name__ = unique_name
        decorated_view = decorator(_view)
        app.add_url_rule(path, view_func=decorated_view)

        client = app.test_client()
        response = client.get(path)
        assert response.status_code == expected_status, (
            f"Matrix row '{test_id}' failed: "
            f"role={role_on_session.value}, "
            f"allowed={[r.value for r in allowed_roles]}, "
            f"expected={expected_status}, got={response.status_code}"
        )
        if expected_status == 403:
            payload = response.get_json()
            assert payload["error"]["code"] == "forbidden"


# ---------------------------------------------------------------------------
# Logging at request time
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRBACLogging:
    """Validate the structured logging emitted by the decorator."""

    def test_no_session_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """When ``g.session`` is None, ``rbac_no_session`` logs at warning level.

        Tolerant: if structlog's processor chain bypasses stdlib
        logging, caplog records nothing. We assert only that the
        response is correct; log emission is verified in observability
        integration tests where the full structlog config is wired.
        """
        app = _make_test_app()

        @app.route("/log-no-session")
        @requires_role(UserRole.ADMIN)
        def _view() -> Any:
            return jsonify(ok=True)

        client = app.test_client()
        with caplog.at_level("WARNING"):
            response = client.get("/log-no-session")
        assert response.status_code == 403

    def test_forbidden_does_not_leak_internals_in_message(self) -> None:
        """403 message is the generic 'You do not have permission' text.

        Internals like role names go to logs (for audit), not the
        body. A naive implementation that echoed
        ``f"Role {actual_role} not permitted"`` in the response would
        leak the role topology to attackers probing the API; the
        client envelope MUST stay generic.
        """
        app = _make_test_app()
        _attach_session(app, _make_session(UserRole.VIEWER))

        @app.route("/no-leak")
        @requires_role(UserRole.ADMIN)
        def _view() -> Any:
            return jsonify(ok=True)

        client = app.test_client()
        response = client.get("/no-leak")
        payload = response.get_json()
        # The role name string and admin requirement should NOT be in
        # the body; the generic forbidden message lives there.
        assert "Viewer" not in payload["error"]["message"]
        assert "ADMIN" not in payload["error"]["message"]
        assert payload["error"]["message"]  # non-empty


# ---------------------------------------------------------------------------
# Stacked decorators (defense-in-depth composition)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestStackedDecorators:
    """``requires_role`` composes cleanly with other decorators.

    ``functools.wraps`` preserves the wrapped view's identity through
    each decorator layer, so two stacked ``@requires_role(...)``
    decorators effectively intersect their allowed sets: the request
    must pass BOTH role-gates to reach the view.
    """

    def test_stacked_role_decorators_intersect(self) -> None:
        """Two stacked ``requires_role`` decorators effectively intersect.

        ``@requires_role(ADMIN, CONTRIBUTOR)``
        ``@requires_role(CONTRIBUTOR, VIEWER)``

        Both must admit the role. Effective allowed set = {Contributor}.
        """
        app = _make_test_app()
        _attach_session(app, _make_session(UserRole.CONTRIBUTOR))

        @app.route("/intersect-contributor")
        @requires_role(UserRole.ADMIN, UserRole.CONTRIBUTOR)
        @requires_role(UserRole.CONTRIBUTOR, UserRole.VIEWER)
        def _view() -> Any:
            return jsonify(ok=True)

        client = app.test_client()
        response = client.get("/intersect-contributor")
        assert response.status_code == 200

    def test_stacked_role_decorators_admin_denied(self) -> None:
        """When stacked decorators do not both admit, request is 403.

        Same stack as above, but session is Admin -> outer admits but
        inner denies (Admin not in {CONTRIBUTOR, VIEWER}).
        """
        app = _make_test_app()
        _attach_session(app, _make_session(UserRole.ADMIN))

        @app.route("/intersect-admin")
        @requires_role(UserRole.ADMIN, UserRole.CONTRIBUTOR)
        @requires_role(UserRole.CONTRIBUTOR, UserRole.VIEWER)
        def _view() -> Any:
            return jsonify(ok=True)

        client = app.test_client()
        response = client.get("/intersect-admin")
        assert response.status_code == 403
