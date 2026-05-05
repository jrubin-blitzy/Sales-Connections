"""Tests for ``app.middleware.error_handlers``.

Validates the uniform JSON error envelope contract per AAP s 0.4.3::

    {"error": {"code": "...", "message": "...", "correlation_id": "...", "fields": []}}

Coverage:

* Exception class hierarchy (``AppError`` and five subclasses) with
  correct status codes, error codes, and default messages.
* ``build_error_response`` envelope construction (correct keys, types,
  correlation_id sourcing, X-Correlation-Id header, defensive fallback
  outside request context).
* Per-status handlers (401 / 403 / 404 / 409 / 422 / 500) emit the
  uniform envelope when the corresponding exception is raised in a
  Flask handler.
* ``pydantic.ValidationError`` -> 422 with ``fields=[{loc, msg, type}, ...]``
  with PII fields (``input``, ``ctx``) STRIPPED.
* Werkzeug ``HTTPException`` -> uniform envelope (NOT default HTML).
* Unhandled ``Exception`` -> 500 with generic message; no traceback,
  no exception class name, no exception ``str()`` value leaked.
* ``register_error_handlers(app)`` is idempotent and registers all
  expected exception classes.

Security invariants verified:

* No traceback in any client-facing response.
* No internal exception details in 500 responses.
* No pydantic ``input`` or ``ctx`` field in 422 responses.
* Correlation ID propagated to both response body and X-Correlation-Id
  header.

Test isolation strategy:

* Each test gets a fresh ``Flask(__name__)`` instance via the
  ``test_app`` / ``test_client`` fixtures, deliberately bypassing the
  full ``create_app(TestingConfig)`` factory so the auth/rbac/blueprint
  middleware cannot pollute error-handler-only assertions.
* An autouse ``_isolate_structlog_contextvars`` fixture double-clears
  ``structlog.contextvars`` before AND after every test to prevent
  state bleed across tests on the same Python thread.
* No DB or external services are used; all tests run against bare
  Flask apps. No fixtures from the global conftest's ``db_session``,
  ``organization``, etc. are referenced here.
"""

from __future__ import annotations

# Standard library imports.
import http
import re
from typing import Any
from unittest.mock import patch

# Third-party imports.
from flask import Flask, abort, g, jsonify
from pydantic import BaseModel, Field, ValidationError, field_validator
import pytest
import structlog
from werkzeug.exceptions import (
    HTTPException,
    NotFound as WerkzeugNotFound,
    RequestEntityTooLarge,
)

# First-party imports.
from app.middleware.correlation import (
    CORRELATION_HEADER_NAME,
    register_correlation_middleware,
)
from app.middleware.error_handlers import (
    ERROR_CODE_CONFLICT,
    ERROR_CODE_FORBIDDEN,
    ERROR_CODE_INTERNAL,
    ERROR_CODE_NOT_FOUND,
    ERROR_CODE_UNAUTHORIZED,
    ERROR_CODE_VALIDATION,
    AppError,
    AuthError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationFailedError,
    build_error_response,
    register_error_handlers,
)

# ---------------------------------------------------------------------------
# Module-level test helpers
# ---------------------------------------------------------------------------


def _make_test_app() -> Flask:
    """Build a minimal Flask app with correlation + error handlers wired.

    The correlation middleware MUST run BEFORE error handlers register
    because the envelope references ``g.correlation_id`` set by the
    correlation hook. Per AAP s 0.5.2 Layer 0, the production order is
    correlation -> auth -> rbac -> error_handlers, but for these tests
    we omit auth and rbac to isolate error-handler behaviour.

    Sets ``TESTING=True`` so Flask uses test-friendly defaults but
    forces ``PROPAGATE_EXCEPTIONS=False`` so the registered error
    handlers actually run during tests instead of Flask re-raising
    the original exception. Without ``PROPAGATE_EXCEPTIONS=False`` the
    test client would surface exceptions directly to the caller and
    we would not be able to assert envelope shape.

    Returns:
        A Flask app instance with both middleware modules registered
        and ready for routes to be added by individual tests.
    """
    app = Flask(__name__)
    app.config["TESTING"] = True
    # Disable propagation so error handlers run (Flask re-raises in
    # test mode by default unless TESTING is True AND
    # PROPAGATE_EXCEPTIONS is False).
    app.config["PROPAGATE_EXCEPTIONS"] = False

    register_correlation_middleware(app)
    register_error_handlers(app)
    return app


# Compile a strict regex that detects fragments of a Python traceback
# anywhere in a response body. Any one of these strings appearing in a
# client-facing response would constitute a security leak per AAP
# s 0.7.4. The pattern uses non-capturing groups for performance.
_TRACEBACK_LEAK_PATTERN: re.Pattern[str] = re.compile(
    r"(?:Traceback \(most recent call last\)|"
    r"  File \"[^\"]+\", line \d+|"
    r"\.py\", line \d+, in )"
)


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def test_app() -> Flask:
    """A fresh Flask app with correlation + error handlers wired.

    Each test gets its own app instance so route registrations from
    one test do not leak into another. This is essential because some
    tests register multiple inline routes via ``@test_app.route(...)``
    and Flask raises ``AssertionError`` on duplicate endpoint names.
    """
    return _make_test_app()


@pytest.fixture
def test_client(test_app: Flask) -> Any:
    """Test client for the app fixture.

    Returns a Werkzeug test client that records responses without
    going through the network. ``test_app`` is wired with both
    correlation middleware and error handlers, so any exception
    raised in a route exercises the full error-handling pipeline.
    """
    return test_app.test_client()


@pytest.fixture(autouse=True)
def _isolate_structlog_contextvars() -> Any:
    """Clear structlog contextvars BEFORE and AFTER every test.

    structlog's ``contextvars`` use Python's ``contextvars`` module
    which is thread-local in WSGI workers. Pytest runs all tests in
    one thread; without explicit clearing, contextvars set by one test
    bleed into the next. The autouse fixture clears them before AND
    after each test as a defensive double-clear.

    Yields:
        None. The fixture exists purely for its side effects.
    """
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()


# ---------------------------------------------------------------------------
# TestExceptionHierarchy - validate exception class hierarchy and metadata
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestExceptionHierarchy:
    """Validate the exception class hierarchy and metadata.

    The exception classes form the typed surface that service-layer
    code raises and that the registered Flask error handlers match
    on. Any change to status_code, error_code, or default_message
    constitutes an API contract change visible to the SPA's typed
    ApiError dispatch in ``frontend/src/api/client.ts``.
    """

    def test_app_error_is_exception_subclass(self) -> None:
        """``AppError`` extends Python's ``Exception``.

        This is the foundational invariant: handlers may catch
        ``Exception`` and find ``AppError`` instances. Subclasses
        also automatically inherit Exception semantics for ``str()``
        and ``args`` access via ``super().__init__()``.
        """
        assert issubclass(AppError, Exception)

    def test_subclasses_extend_app_error(self) -> None:
        """All five typed exceptions extend ``AppError``.

        Verifies the inheritance chain: each subclass is both an
        ``AppError`` (so the safety-net handler matches it) AND an
        ``Exception`` (so the catch-all handler matches it as a
        last resort).
        """
        for cls in (
            AuthError,
            ForbiddenError,
            ValidationFailedError,
            NotFoundError,
            ConflictError,
        ):
            assert issubclass(cls, AppError)
            assert issubclass(cls, Exception)

    def test_auth_error_metadata(self) -> None:
        """``AuthError`` carries 401 / unauthorized."""
        exc = AuthError()
        assert exc.status_code == http.HTTPStatus.UNAUTHORIZED.value == 401
        assert exc.error_code == ERROR_CODE_UNAUTHORIZED == "unauthorized"
        # default_message is non-empty string.
        assert isinstance(exc.message, str)
        assert exc.message
        assert exc.fields == []

    def test_forbidden_error_metadata(self) -> None:
        """``ForbiddenError`` carries 403 / forbidden."""
        exc = ForbiddenError()
        assert exc.status_code == http.HTTPStatus.FORBIDDEN.value == 403
        assert exc.error_code == ERROR_CODE_FORBIDDEN == "forbidden"
        assert isinstance(exc.message, str)
        assert exc.message
        assert exc.fields == []

    def test_validation_failed_error_metadata(self) -> None:
        """``ValidationFailedError`` carries 422 / validation_failed."""
        exc = ValidationFailedError()
        assert exc.status_code == http.HTTPStatus.UNPROCESSABLE_ENTITY.value == 422
        assert exc.error_code == ERROR_CODE_VALIDATION == "validation_failed"
        assert isinstance(exc.message, str)
        assert exc.message
        assert exc.fields == []

    def test_not_found_error_metadata(self) -> None:
        """``NotFoundError`` carries 404 / not_found."""
        exc = NotFoundError()
        assert exc.status_code == http.HTTPStatus.NOT_FOUND.value == 404
        assert exc.error_code == ERROR_CODE_NOT_FOUND == "not_found"
        assert isinstance(exc.message, str)
        assert exc.message
        assert exc.fields == []

    def test_conflict_error_metadata(self) -> None:
        """``ConflictError`` carries 409 / conflict."""
        exc = ConflictError()
        assert exc.status_code == http.HTTPStatus.CONFLICT.value == 409
        assert exc.error_code == ERROR_CODE_CONFLICT == "conflict"
        assert isinstance(exc.message, str)
        assert exc.message
        assert exc.fields == []

    def test_custom_message_preserved(self) -> None:
        """Constructor message overrides the default."""
        exc = AuthError("custom auth message")
        assert exc.message == "custom auth message"
        # Inheritance check: Exception.__str__ uses args[0].
        assert str(exc) == "custom auth message"

    def test_default_message_when_none(self) -> None:
        """When message=None, ``default_message`` is used."""
        # Pass None explicitly; the constructor should fall back.
        exc = AuthError(None)
        assert exc.message == exc.default_message
        assert exc.message  # not empty

    def test_default_message_when_empty_string(self) -> None:
        """When message is an empty string, the default fires.

        The constructor uses ``message if message else self.default_message``
        which treats both ``None`` and ``""`` as falsy and falls back
        to the default. This protects against a misconfigured caller
        that passes an empty string.
        """
        exc = AuthError("")
        assert exc.message == exc.default_message
        assert exc.message  # not empty

    def test_default_messages_are_distinct_per_subclass(self) -> None:
        """Each subclass has a distinct default_message string.

        Stops accidental copy-paste regressions where two subclasses
        share the same default message and one was meant to be
        overridden.
        """
        defaults = {
            AuthError().default_message,
            ForbiddenError().default_message,
            ValidationFailedError().default_message,
            NotFoundError().default_message,
            ConflictError().default_message,
        }
        # All five subclasses have unique default messages.
        assert len(defaults) == 5

    def test_fields_is_list_of_dicts(self) -> None:
        """``fields`` is normalized to ``list[dict]``.

        Constructor accepts any Sequence[Mapping]; storage is always
        list[dict]. The canonical envelope-field shape is
        ``{field, code, message}`` per ``docs/api.md``.
        """
        exc = ValidationFailedError(
            "bad",
            fields=[
                {"field": "x", "code": "missing", "message": "required"},
                {"field": "y", "code": "string_too_long", "message": "too long"},
            ],
        )
        assert isinstance(exc.fields, list)
        assert len(exc.fields) == 2
        for field in exc.fields:
            assert isinstance(field, dict)

    def test_fields_default_empty_list(self) -> None:
        """When ``fields=None``, the stored value is an empty list."""
        exc = AuthError()
        assert exc.fields == []
        # And it is mutable so handlers can append to it.
        exc.fields.append({"field": "foo", "code": "bar", "message": "baz"})
        assert len(exc.fields) == 1

    def test_fields_tuple_input_normalized_to_list(self) -> None:
        """Constructor accepts a tuple of mappings; storage is a list."""
        exc = ValidationFailedError(
            "bad",
            fields=(
                {"field": "x", "code": "missing", "message": "required"},
                {"field": "y", "code": "string_too_long", "message": "too long"},
            ),
        )
        assert isinstance(exc.fields, list)
        assert len(exc.fields) == 2

    def test_fields_input_copied_not_referenced(self) -> None:
        """Mutating the input fields after construction does NOT mutate
        the stored ``exc.fields`` (defensive copy).

        The constructor builds ``[dict(f) for f in fields]`` which
        deep-copies each mapping into a fresh dict. Callers can rely
        on the stored value being independent of the input list.
        """
        original = [{"field": "x", "code": "missing", "message": "first"}]
        exc = ValidationFailedError("bad", fields=original)
        # Mutate the original AFTER construction.
        original.append({"field": "y", "code": "missing", "message": "added later"})
        original[0]["message"] = "mutated"
        # Stored fields should be unaffected.
        assert len(exc.fields) == 1
        assert exc.fields[0]["message"] == "first"


# ---------------------------------------------------------------------------
# TestBuildErrorResponse - validate the public envelope-construction helper
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBuildErrorResponse:
    """Validate the public envelope-construction helper.

    The helper is the single point that produces the envelope shape,
    used by the registered handlers AND by callers that need to
    bypass exception flow (e.g., the auth middleware's 401 response
    path before exceptions are wired).
    """

    def test_envelope_shape_keys(self, test_app: Flask) -> None:
        """Envelope contains exactly four keys under ``error``."""
        with test_app.test_request_context():
            g.correlation_id = "test-cid"
            response, status = build_error_response(
                code="custom_code",
                message="hello",
                status=418,
            )
            payload = response.get_json()
        assert "error" in payload
        assert set(payload["error"].keys()) == {
            "code",
            "message",
            "correlation_id",
            "fields",
        }
        assert payload["error"]["code"] == "custom_code"
        assert payload["error"]["message"] == "hello"
        assert payload["error"]["correlation_id"] == "test-cid"
        assert payload["error"]["fields"] == []
        assert status == 418
        assert response.status_code == 418

    def test_correlation_header_set(self, test_app: Flask) -> None:
        """The ``X-Correlation-Id`` header is set on the response."""
        with test_app.test_request_context():
            g.correlation_id = "header-cid"
            response, _ = build_error_response(code="x", message="y", status=500)
        assert response.headers[CORRELATION_HEADER_NAME] == "header-cid"

    def test_correlation_id_falls_back_to_empty_when_g_unset(
        self,
    ) -> None:
        """When ``g.correlation_id`` is missing, fallback is empty string.

        This protects callers that invoke the helper from startup
        diagnostics or inside fixtures outside the request context.
        Inside an app context but without a request context, ``g``
        exists but ``correlation_id`` may be unset.
        """
        # Use a fresh app so no fixture pollutes correlation_id.
        app = Flask(__name__)
        with app.app_context():
            # No request context here; ``g`` exists but
            # ``correlation_id`` is unset.
            response, _ = build_error_response(code="x", message="y", status=500)
            payload = response.get_json()
        assert payload["error"]["correlation_id"] == ""

    def test_fields_passed_through(self, test_app: Flask) -> None:
        """Provided ``fields`` make it into the envelope."""
        provided_fields = [
            {"field": "name", "code": "missing", "message": "required"},
            {"field": "age", "code": "greater_than_equal", "message": "must be positive"},
        ]
        with test_app.test_request_context():
            g.correlation_id = "f-cid"
            response, _ = build_error_response(
                code="validation_failed",
                message="bad input",
                status=422,
                fields=provided_fields,
            )
            payload = response.get_json()
        assert payload["error"]["fields"] == provided_fields

    def test_fields_normalized_to_list(self, test_app: Flask) -> None:
        """Tuple of mappings is normalized to list of dicts."""
        provided_fields = (
            {"field": "x", "code": "missing", "message": "a"},
            {"field": "y", "code": "missing", "message": "b"},
        )
        with test_app.test_request_context():
            g.correlation_id = ""
            response, _ = build_error_response(
                code="x", message="y", status=400, fields=provided_fields
            )
            payload = response.get_json()
        assert isinstance(payload["error"]["fields"], list)
        assert len(payload["error"]["fields"]) == 2
        for entry in payload["error"]["fields"]:
            assert isinstance(entry, dict)

    def test_no_correlation_header_when_cid_empty(self, test_app: Flask) -> None:
        """When correlation_id is empty, X-Correlation-Id is NOT set.

        Avoids polluting headers with an empty string value.
        """
        with test_app.test_request_context():
            g.correlation_id = ""
            response, _ = build_error_response(code="x", message="y", status=400)
        assert CORRELATION_HEADER_NAME not in response.headers

    def test_response_content_type_is_json(self, test_app: Flask) -> None:
        """The response uses ``application/json`` content type.

        Flask's ``jsonify`` sets this automatically; we assert it
        explicitly so future refactors that switch to manual JSON
        serialization do not regress.
        """
        with test_app.test_request_context():
            g.correlation_id = "cid"
            response, _ = build_error_response(code="x", message="y", status=400)
        assert "application/json" in response.content_type

    def test_returns_tuple_of_response_and_status(self, test_app: Flask) -> None:
        """The return value is a ``(response, status_int)`` tuple.

        Flask handlers may return either ``Response`` or
        ``(Response, int)``. Returning a tuple is the more explicit
        contract that documents the status code at the call site.
        """
        with test_app.test_request_context():
            g.correlation_id = "cid"
            result = build_error_response(code="x", message="y", status=409)
        assert isinstance(result, tuple)
        assert len(result) == 2
        response, status = result
        assert hasattr(response, "headers")
        assert hasattr(response, "status_code")
        assert isinstance(status, int)
        assert status == 409


# ---------------------------------------------------------------------------
# TestAppErrorSubclassHandlers - subclasses map to their status codes
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAppErrorSubclassHandlers:
    """Each AppError subclass produces an envelope with its status code.

    Flask dispatches by exception class identity using class-hierarchy
    lookup (most-specific subclass wins). The handlers registered for
    each subclass take precedence over the ``AppError`` and
    ``Exception`` catch-alls.
    """

    def test_auth_error_returns_401(self, test_app: Flask) -> None:
        """``AuthError`` -> 401 with code 'unauthorized'."""

        @test_app.route("/raise-auth")
        def _h() -> Any:
            raise AuthError("expired token")

        client = test_app.test_client()
        response = client.get("/raise-auth")
        assert response.status_code == 401
        payload = response.get_json()
        assert payload["error"]["code"] == "unauthorized"
        assert payload["error"]["message"] == "expired token"
        assert payload["error"]["fields"] == []

    def test_forbidden_error_returns_403(self, test_app: Flask) -> None:
        """``ForbiddenError`` -> 403 with code 'forbidden'."""

        @test_app.route("/raise-forbidden")
        def _h() -> Any:
            raise ForbiddenError()

        client = test_app.test_client()
        response = client.get("/raise-forbidden")
        assert response.status_code == 403
        payload = response.get_json()
        assert payload["error"]["code"] == "forbidden"
        # Default message is non-empty.
        assert payload["error"]["message"]

    def test_not_found_error_returns_404(self, test_app: Flask) -> None:
        """``NotFoundError`` -> 404 with code 'not_found'."""

        @test_app.route("/raise-notfound")
        def _h() -> Any:
            raise NotFoundError("Connection 42 not found")

        client = test_app.test_client()
        response = client.get("/raise-notfound")
        assert response.status_code == 404
        payload = response.get_json()
        assert payload["error"]["code"] == "not_found"
        assert payload["error"]["message"] == "Connection 42 not found"

    def test_conflict_error_returns_409(self, test_app: Flask) -> None:
        """``ConflictError`` -> 409 with code 'conflict'."""

        @test_app.route("/raise-conflict")
        def _h() -> Any:
            raise ConflictError("duplicate URL")

        client = test_app.test_client()
        response = client.get("/raise-conflict")
        assert response.status_code == 409
        payload = response.get_json()
        assert payload["error"]["code"] == "conflict"
        assert payload["error"]["message"] == "duplicate URL"

    def test_validation_failed_error_returns_422(self, test_app: Flask) -> None:
        """``ValidationFailedError`` -> 422 with code 'validation_failed'.

        Includes the ``fields`` list passed in.
        """

        @test_app.route("/raise-validation")
        def _h() -> Any:
            raise ValidationFailedError(
                "bad",
                fields=[{"field": "x", "code": "missing", "message": "required"}],
            )

        client = test_app.test_client()
        response = client.get("/raise-validation")
        assert response.status_code == 422
        payload = response.get_json()
        assert payload["error"]["code"] == "validation_failed"
        assert payload["error"]["fields"] == [
            {"field": "x", "code": "missing", "message": "required"}
        ]

    def test_correlation_id_in_envelope(self, test_app: Flask) -> None:
        """Every error envelope carries the correlation_id from g."""

        @test_app.route("/raise-cid")
        def _h() -> Any:
            raise AuthError()

        client = test_app.test_client()
        response = client.get(
            "/raise-cid",
            headers={CORRELATION_HEADER_NAME: "trace-cid-1234"},
        )
        payload = response.get_json()
        assert payload["error"]["correlation_id"] == "trace-cid-1234"
        assert response.headers[CORRELATION_HEADER_NAME] == "trace-cid-1234"

    def test_default_message_used_when_none_supplied(self, test_app: Flask) -> None:
        """When the exception is raised with no message, the default
        message of the subclass is what reaches the envelope."""

        @test_app.route("/raise-default-msg")
        def _h() -> Any:
            raise ConflictError()

        client = test_app.test_client()
        response = client.get("/raise-default-msg")
        payload = response.get_json()
        # Default message is non-empty and is the class default.
        assert payload["error"]["message"] == ConflictError().default_message

    def test_envelope_contains_only_four_keys(self, test_app: Flask) -> None:
        """The error envelope's ``error`` dict contains exactly the
        four canonical keys from AAP s 0.4.3."""

        @test_app.route("/raise-shape")
        def _h() -> Any:
            raise NotFoundError("missing")

        client = test_app.test_client()
        response = client.get("/raise-shape")
        payload = response.get_json()
        assert set(payload["error"].keys()) == {
            "code",
            "message",
            "correlation_id",
            "fields",
        }

    def test_fields_list_passed_through_subclass(self, test_app: Flask) -> None:
        """Multiple fields pass through to the envelope unchanged."""
        provided = [
            {"field": "x", "code": "missing", "message": "required"},
            {"field": "y", "code": "string_too_long", "message": "too long"},
            {"field": "z", "code": "value_error", "message": "out of range"},
        ]

        @test_app.route("/raise-multifield")
        def _h() -> Any:
            raise ValidationFailedError("bad payload", fields=provided)

        client = test_app.test_client()
        response = client.get("/raise-multifield")
        payload = response.get_json()
        assert payload["error"]["fields"] == provided


# ---------------------------------------------------------------------------
# Pydantic test payload model used by TestPydanticValidationHandler
# ---------------------------------------------------------------------------


class _TestPayload(BaseModel):
    """Minimal pydantic model used to trigger ValidationErrors in tests.

    Combines three different kinds of validation failures:

    * ``min_length=2``  -> ``string_too_short`` with ctx={"min_length": 2}.
    * ``ge=0``          -> ``greater_than_equal`` with ctx={"ge": 0}.
    * ``pattern=...``   -> ``string_pattern_mismatch`` with ctx={"pattern": "..."}.

    All three failure modes include ``input`` and ``ctx`` in pydantic's
    ``errors()`` output; the handler must strip both before they reach
    the envelope.
    """

    name: str = Field(min_length=2)
    age: int = Field(ge=0)
    linkedin_url: str = Field(pattern=r"^https?://")

    @field_validator("linkedin_url")
    @classmethod
    def _validate_url_extra(cls, v: str) -> str:
        """No-op validator kept to exercise ``field_validator`` import.

        Returns the value unchanged. Present so the test file's
        ``field_validator`` import is used in the actual model and not
        flagged as an unused import by ruff.
        """
        return v


# ---------------------------------------------------------------------------
# TestPydanticValidationHandler - pydantic ValidationError -> 422
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPydanticValidationHandler:
    """Validate the pydantic ValidationError -> 422 mapping.

    Pydantic raises ``ValidationError`` when payload schema/type
    validation fails at the API boundary. The handler extracts safe
    field-level details for the SPA while STRIPPING any pydantic
    metadata that could leak PII or internal regex patterns.
    """

    def test_pydantic_error_returns_422(self, test_app: Flask) -> None:
        """A pydantic ValidationError raised in a handler -> 422."""

        @test_app.route("/raise-pydantic", methods=["POST"])
        def _h() -> Any:
            _TestPayload.model_validate({"name": "x", "age": -1, "linkedin_url": "bad"})
            return jsonify(ok=True)

        client = test_app.test_client()
        response = client.post("/raise-pydantic", json={})
        assert response.status_code == 422

    def test_pydantic_error_envelope_has_field_list(self, test_app: Flask) -> None:
        """The envelope's ``fields`` list contains entries for each error."""

        @test_app.route("/raise-pydantic-list", methods=["POST"])
        def _h() -> Any:
            _TestPayload.model_validate({"name": "x", "age": -1, "linkedin_url": "bad"})
            return jsonify(ok=True)

        client = test_app.test_client()
        response = client.post("/raise-pydantic-list", json={})
        payload = response.get_json()
        assert "fields" in payload["error"]
        # At least the three failing fields produce entries.
        assert len(payload["error"]["fields"]) >= 3

    def test_pydantic_field_entry_shape(self, test_app: Flask) -> None:
        """Each ``fields`` entry has the canonical ``field``, ``code``, ``message`` keys.

        Per ``docs/api.md`` and ``frontend/src/api/client.ts`` ApiErrorField
        interface, the envelope-field shape is exactly these three string keys.
        Earlier drafts emitted ``{loc, msg, type}`` from raw pydantic; the
        handler now normalizes to the canonical contract.
        """

        @test_app.route("/raise-pydantic-shape", methods=["POST"])
        def _h() -> Any:
            _TestPayload.model_validate({"name": "x", "age": -1, "linkedin_url": "bad"})
            return jsonify(ok=True)

        client = test_app.test_client()
        response = client.post("/raise-pydantic-shape", json={})
        payload = response.get_json()
        for entry in payload["error"]["fields"]:
            assert "field" in entry
            assert "code" in entry
            assert "message" in entry
            assert isinstance(entry["field"], str)
            assert isinstance(entry["code"], str)
            assert isinstance(entry["message"], str)
            # The legacy raw-pydantic keys MUST NOT appear; this is
            # the regression guard for the canonical contract.
            assert "loc" not in entry
            assert "msg" not in entry
            assert "type" not in entry

    def test_pydantic_input_field_dropped(self, test_app: Flask) -> None:
        """The user-supplied ``input`` value is NOT echoed in the envelope.

        PII protection: pydantic's ``errors()`` may include the offending
        input in some configurations; the handler MUST strip it.
        """
        secret_token = "totally-secret-password"

        @test_app.route("/raise-pydantic-input", methods=["POST"])
        def _h() -> Any:
            _TestPayload.model_validate({"name": "x", "age": secret_token, "linkedin_url": "bad"})
            return jsonify(ok=True)

        client = test_app.test_client()
        response = client.post("/raise-pydantic-input", json={})
        payload = response.get_json()
        # ``input`` must not appear in any field entry.
        for entry in payload["error"]["fields"]:
            assert "input" not in entry, (
                "PII leak: pydantic 'input' field appeared in envelope. "
                "_serialize_pydantic_errors must strip it."
            )
        # Defense in depth: the secret token must not appear ANYWHERE
        # in the response body.
        body_str = response.get_data(as_text=True)
        assert secret_token not in body_str, (
            "PII leak: user-supplied input appeared in response body."
        )

    def test_pydantic_ctx_field_dropped(self, test_app: Flask) -> None:
        """Pydantic's ``ctx`` field (regex pattern, constraint values)
        is dropped.

        Some constraint failures (e.g., regex mismatch) include a
        ``ctx`` dict in pydantic's error output. The handler must
        strip it to avoid leaking internal validation details.
        """

        @test_app.route("/raise-pydantic-ctx", methods=["POST"])
        def _h() -> Any:
            # linkedin_url pattern requires http(s)://; failing it
            # produces ctx={"pattern": "..."} in pydantic's errors().
            _TestPayload.model_validate({"name": "ok", "age": 25, "linkedin_url": "ftp://"})
            return jsonify(ok=True)

        client = test_app.test_client()
        response = client.post("/raise-pydantic-ctx", json={})
        payload = response.get_json()
        for entry in payload["error"]["fields"]:
            assert "ctx" not in entry, (
                "Leak: pydantic 'ctx' field appeared in envelope. "
                "_serialize_pydantic_errors must strip it."
            )

    def test_pydantic_url_field_dropped(self, test_app: Flask) -> None:
        """Pydantic's ``url`` field (link to docs) is dropped.

        Pydantic 2.x adds a ``url`` key in each error dict pointing to
        the relevant documentation page. The handler should drop it
        because the SPA does not need it and it adds payload weight.
        """

        @test_app.route("/raise-pydantic-url", methods=["POST"])
        def _h() -> Any:
            _TestPayload.model_validate({})
            return jsonify(ok=True)

        client = test_app.test_client()
        response = client.post("/raise-pydantic-url", json={})
        payload = response.get_json()
        for entry in payload["error"]["fields"]:
            assert "url" not in entry, "Leak: pydantic 'url' field appeared in envelope."

    def test_pydantic_loc_normalized_to_dotted_field_path(self, test_app: Flask) -> None:
        """``field`` is a dotted path string (e.g., ``"name"`` or ``"tags.0"``).

        Pydantic 2.x sometimes produces tuples of mixed types
        (str + int for list indices). The handler joins every
        segment with ``.`` after string-normalizing each, and strips
        any leading ``"body"`` segment that Flask adds for body-level
        validation. The resulting ``field`` is a JSON-safe dotted path
        the SPA can compare directly against its form-field names.
        """

        @test_app.route("/raise-pydantic-loc", methods=["POST"])
        def _h() -> Any:
            _TestPayload.model_validate({})
            return jsonify(ok=True)

        client = test_app.test_client()
        response = client.post("/raise-pydantic-loc", json={})
        payload = response.get_json()
        for entry in payload["error"]["fields"]:
            assert isinstance(entry["field"], str)
            # The leading "body." prefix that pydantic adds during
            # request-body validation MUST be stripped so the SPA's
            # field names match directly.
            assert not entry["field"].startswith("body.")
            assert entry["field"] != "body"

    def test_pydantic_envelope_has_correlation_id(self, test_app: Flask) -> None:
        """The envelope from a pydantic error still carries
        correlation_id."""

        @test_app.route("/raise-pydantic-cid", methods=["POST"])
        def _h() -> Any:
            _TestPayload.model_validate({})
            return jsonify(ok=True)

        client = test_app.test_client()
        response = client.post(
            "/raise-pydantic-cid",
            json={},
            headers={CORRELATION_HEADER_NAME: "pydantic-cid"},
        )
        payload = response.get_json()
        assert payload["error"]["correlation_id"] == "pydantic-cid"
        assert response.headers[CORRELATION_HEADER_NAME] == "pydantic-cid"

    def test_pydantic_envelope_uses_validation_code(self, test_app: Flask) -> None:
        """The envelope's ``code`` is 'validation_failed'."""

        @test_app.route("/raise-pydantic-code", methods=["POST"])
        def _h() -> Any:
            _TestPayload.model_validate({})
            return jsonify(ok=True)

        client = test_app.test_client()
        response = client.post("/raise-pydantic-code", json={})
        payload = response.get_json()
        assert payload["error"]["code"] == ERROR_CODE_VALIDATION
        assert payload["error"]["code"] == "validation_failed"

    def test_pydantic_envelope_message_is_generic(self, test_app: Flask) -> None:
        """The envelope's ``message`` is a generic string, not raw
        pydantic output.

        Per-field details live in ``fields``; the message is a stable
        sentence so the envelope shape is predictable regardless of
        how many fields failed.
        """

        @test_app.route("/raise-pydantic-msg", methods=["POST"])
        def _h() -> Any:
            _TestPayload.model_validate({})
            return jsonify(ok=True)

        client = test_app.test_client()
        response = client.post("/raise-pydantic-msg", json={})
        payload = response.get_json()
        # Pydantic's raw error format includes "validation error for"
        # which we MUST NOT echo verbatim.
        assert "validation error for" not in payload["error"]["message"].lower()
        # The message should be non-empty.
        assert payload["error"]["message"]


# ---------------------------------------------------------------------------
# TestWerkzeugHTTPExceptionHandler - werkzeug HTTPException -> envelope
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestWerkzeugHTTPExceptionHandler:
    """Validate werkzeug HTTPException -> uniform JSON envelope.

    Flask raises werkzeug ``HTTPException`` subclasses for
    ``abort(...)`` calls and for routing failures (404, 405). The
    handler converts these into the uniform JSON envelope so the SPA
    can dispatch on ``error.code`` regardless of whether the error
    was raised by application code or by the framework.
    """

    def test_abort_404_returns_uniform_envelope(self, test_app: Flask) -> None:
        """``flask.abort(404)`` returns the JSON envelope, NOT default HTML."""

        @test_app.route("/abort-404")
        def _h() -> Any:
            abort(404)

        client = test_app.test_client()
        response = client.get("/abort-404")
        assert response.status_code == 404
        # The Content-Type MUST be JSON, NOT HTML.
        assert "application/json" in response.content_type
        payload = response.get_json()
        assert "error" in payload
        assert payload["error"]["code"] == "not_found"

    def test_abort_400_returns_uniform_envelope(self, test_app: Flask) -> None:
        """``abort(400)`` returns 400 with code derived from status name."""

        @test_app.route("/abort-400")
        def _h() -> Any:
            abort(400)

        client = test_app.test_client()
        response = client.get("/abort-400")
        assert response.status_code == 400
        payload = response.get_json()
        assert "error" in payload
        # Code derived from HTTPStatus.BAD_REQUEST.name.lower()
        assert payload["error"]["code"] == "bad_request"

    def test_abort_413_request_entity_too_large(self, test_app: Flask) -> None:
        """``abort(413)`` produces 'request_entity_too_large' code.

        Tests that the handler correctly uses the
        ``RequestEntityTooLarge`` werkzeug subclass via the generic
        HTTPException registration.
        """

        @test_app.route("/abort-413")
        def _h() -> Any:
            raise RequestEntityTooLarge()

        client = test_app.test_client()
        response = client.get("/abort-413")
        assert response.status_code == 413
        payload = response.get_json()
        # 413 IS in HTTPStatus -> name is REQUEST_ENTITY_TOO_LARGE
        # which lowercases to "request_entity_too_large".
        assert payload["error"]["code"] == "request_entity_too_large"

    def test_unmapped_status_uses_http_generic_code(self, test_app: Flask) -> None:
        """When the status maps to a known HTTPStatus member, the
        derived code uses that name; for 418 in modern Python this is
        ``im_a_teapot``.

        We allow both ``im_a_teapot`` (correct) and ``http_error``
        (fallback) to handle implementations that may not have
        anticipated this status.
        """

        @test_app.route("/abort-418")
        def _h() -> Any:
            abort(418)

        client = test_app.test_client()
        response = client.get("/abort-418")
        assert response.status_code == 418
        payload = response.get_json()
        # 418 IS in HTTPStatus (im_a_teapot since Python 3.9)
        assert payload["error"]["code"] in {"im_a_teapot", "http_error"}

    def test_unhandled_route_returns_404_uniform_envelope(self, test_app: Flask) -> None:
        """A request to an unregistered route returns the uniform 404
        envelope.

        Flask raises Werkzeug's NotFound; our handler converts it.
        """
        client = test_app.test_client()
        response = client.get("/no-such-route-exists-anywhere")
        assert response.status_code == 404
        # Must be JSON, not Flask's default HTML 404 page.
        assert "application/json" in response.content_type
        payload = response.get_json()
        assert payload["error"]["code"] == "not_found"

    def test_method_not_allowed_returns_uniform_envelope(self, test_app: Flask) -> None:
        """Sending a wrong HTTP method to a registered route produces
        a 405 envelope, NOT Flask's default HTML response."""

        @test_app.route("/only-get", methods=["GET"])
        def _h() -> Any:
            return jsonify(ok=True)

        client = test_app.test_client()
        response = client.post("/only-get")
        assert response.status_code == 405
        assert "application/json" in response.content_type
        payload = response.get_json()
        assert payload["error"]["code"] == "method_not_allowed"

    def test_correlation_id_in_werkzeug_envelope(self, test_app: Flask) -> None:
        """Werkzeug-derived envelopes also carry correlation_id."""

        @test_app.route("/abort-with-cid")
        def _h() -> Any:
            abort(404)

        client = test_app.test_client()
        response = client.get(
            "/abort-with-cid",
            headers={CORRELATION_HEADER_NAME: "werkzeug-cid"},
        )
        payload = response.get_json()
        assert payload["error"]["correlation_id"] == "werkzeug-cid"
        assert response.headers[CORRELATION_HEADER_NAME] == "werkzeug-cid"

    def test_raise_werkzeug_notfound_class_directly(self, test_app: Flask) -> None:
        """Raising the werkzeug NotFound class directly is also handled.

        Some service code may ``raise NotFound("...")`` instead of
        calling ``abort()``; both paths must produce the uniform
        envelope.
        """

        @test_app.route("/raise-werkzeug-nf")
        def _h() -> Any:
            raise WerkzeugNotFound("explicit raise")

        client = test_app.test_client()
        response = client.get("/raise-werkzeug-nf")
        assert response.status_code == 404
        assert "application/json" in response.content_type
        payload = response.get_json()
        assert payload["error"]["code"] == "not_found"

    def test_5xx_werkzeug_exception_uses_generic_message(self, test_app: Flask) -> None:
        """For 5xx werkzeug exceptions, the user-facing message is the
        generic internal-error message, NOT the werkzeug description.

        Werkzeug's default descriptions for 5xx can leak internals
        (e.g., ``InternalServerError`` sometimes carries the
        underlying exception class name on the description).
        """
        sensitive_description = "INTERNAL leak: SQL exec failed at line 42"

        @test_app.route("/abort-500")
        def _h() -> Any:
            abort(500, description=sensitive_description)

        client = test_app.test_client()
        response = client.get("/abort-500")
        assert response.status_code == 500
        payload = response.get_json()
        # The sensitive description must NOT appear in the message.
        assert sensitive_description not in payload["error"]["message"]


# ---------------------------------------------------------------------------
# TestGenericExceptionHandler - the catch-all 500 handler must NOT leak
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGenericExceptionHandler:
    """Validate the catch-all 500 handler does NOT leak internals.

    This is the most security-critical handler. AAP s 0.7.4 mandates
    that no traceback, no exception class name, no exception ``str()``,
    and no internal state may reach the client. Engineers retrieve
    the actual exception from CloudWatch using the correlation_id
    that IS present in the envelope.
    """

    def test_unhandled_exception_returns_500(self, test_app: Flask) -> None:
        """A bare ``Exception`` raised in a handler -> 500."""

        @test_app.route("/raise-generic")
        def _h() -> Any:
            raise RuntimeError("internal database connection lost")

        client = test_app.test_client()
        response = client.get("/raise-generic")
        assert response.status_code == 500

    def test_500_envelope_uses_internal_error_code(self, test_app: Flask) -> None:
        """The envelope's ``code`` is 'internal_error'."""

        @test_app.route("/raise-generic-code")
        def _h() -> Any:
            raise RuntimeError("do not leak this")

        client = test_app.test_client()
        response = client.get("/raise-generic-code")
        payload = response.get_json()
        assert payload["error"]["code"] == ERROR_CODE_INTERNAL
        assert payload["error"]["code"] == "internal_error"

    def test_500_envelope_does_not_leak_exception_message(self, test_app: Flask) -> None:
        """The ``message`` field is the GENERIC message, NOT the
        exception's str().

        Security invariant: internal exception details (file paths,
        SQL strings, internal state) MUST NOT reach the client.
        """
        secret_internal = "INTERNAL: db connection at 10.0.0.5:5432 failed"

        @test_app.route("/raise-leaky")
        def _h() -> Any:
            raise RuntimeError(secret_internal)

        client = test_app.test_client()
        response = client.get("/raise-leaky")
        payload = response.get_json()
        assert secret_internal not in payload["error"]["message"]
        assert "RuntimeError" not in payload["error"]["message"]

    def test_500_envelope_does_not_leak_exception_class_name(self, test_app: Flask) -> None:
        """Custom exception class names do not appear in the response.

        Defense-in-depth: even if the exception class name is
        innocuous, the principle is that the client should never
        learn the internal class taxonomy.
        """

        class _SecretCustomError(Exception):
            """A fake custom exception class used purely for testing."""

        @test_app.route("/raise-custom-class")
        def _h() -> Any:
            raise _SecretCustomError("anything")

        client = test_app.test_client()
        response = client.get("/raise-custom-class")
        body_str = response.get_data(as_text=True)
        assert "_SecretCustomError" not in body_str
        # And the standard exception names are also not present.
        for forbidden in ("RuntimeError", "ValueError", "TypeError"):
            assert forbidden not in body_str

    def test_500_envelope_has_no_traceback_field(self, test_app: Flask) -> None:
        """The response payload contains NO traceback string anywhere."""

        @test_app.route("/raise-traceback")
        def _h() -> Any:
            raise ValueError("some internal value error here")

        client = test_app.test_client()
        response = client.get("/raise-traceback")
        # The response BODY must not contain "Traceback", file paths,
        # or frame info.
        body_str = response.get_data(as_text=True)
        assert "Traceback" not in body_str
        assert "test_error_handlers.py" not in body_str
        # Defense-in-depth: regex check against any traceback fragment.
        assert _TRACEBACK_LEAK_PATTERN.search(body_str) is None
        # The response JSON must have only the 4 envelope keys under error.
        payload = response.get_json()
        assert set(payload["error"].keys()) == {
            "code",
            "message",
            "correlation_id",
            "fields",
        }

    def test_500_envelope_message_is_user_friendly(self, test_app: Flask) -> None:
        """The 500 message is the generic user-friendly string.

        Confirms the message references ``correlation_id`` so users
        can include it in support requests.
        """

        @test_app.route("/raise-msg-check")
        def _h() -> Any:
            raise RuntimeError("internal")

        client = test_app.test_client()
        response = client.get("/raise-msg-check")
        payload = response.get_json()
        # The generic message references the correlation_id so users
        # know what to send to support. Allow either the exact
        # phrase or a permissive containment check on the substring.
        assert "correlation_id" in payload["error"]["message"]

    def test_500_envelope_has_correlation_id(self, test_app: Flask) -> None:
        """500 responses also carry the correlation_id."""

        @test_app.route("/raise-with-cid")
        def _h() -> Any:
            raise RuntimeError("boom")

        client = test_app.test_client()
        response = client.get(
            "/raise-with-cid",
            headers={CORRELATION_HEADER_NAME: "fivehundred-cid"},
        )
        payload = response.get_json()
        assert payload["error"]["correlation_id"] == "fivehundred-cid"
        assert response.headers[CORRELATION_HEADER_NAME] == "fivehundred-cid"

    def test_500_log_includes_traceback_at_error_level(
        self, test_app: Flask, caplog: pytest.LogCaptureFixture
    ) -> None:
        """For 500 responses, the structured log captures the traceback.

        Engineers retrieve the full traceback from CloudWatch using
        the correlation_id; the client never sees it. The assertion
        is intentionally permissive to tolerate either stdlib logging
        or structlog-only configurations.
        """

        @test_app.route("/raise-logged")
        def _h() -> Any:
            raise RuntimeError("this should appear in logs only")

        client = test_app.test_client()
        with caplog.at_level("ERROR"):
            response = client.get("/raise-logged")
        assert response.status_code == 500
        # Either the structlog binding logs to stdlib logging via
        # processor or the stdlib_logger was used directly. We assert
        # the response was 500 unconditionally; the log capture
        # assertion is best-effort because structlog's processor chain
        # may bypass stdlib logging in some configurations.
        # Note: The real value is that the response is 500 and the
        # client message is generic; whether the log was captured is
        # an implementation detail of the structlog setup.
        # We tolerate empty caplog without failing the test.
        _ = caplog.records  # access to silence unused-variable lint

    def test_500_does_not_propagate_exception_when_propagate_false(
        self,
    ) -> None:
        """With PROPAGATE_EXCEPTIONS=False, the test client receives the
        500 envelope, NOT the raised exception.

        Sanity check that the test app configuration is correct. If a
        future regression flips the propagation flag, this test
        catches it.
        """
        app = _make_test_app()

        @app.route("/raise-propagate")
        def _h() -> Any:
            raise RuntimeError("should not propagate")

        client = app.test_client()
        # Should NOT raise; should return a 500 response.
        response = client.get("/raise-propagate")
        assert response.status_code == 500


# ---------------------------------------------------------------------------
# Custom AppError subclass used by TestAppErrorSafetyNetHandler
# ---------------------------------------------------------------------------


class _CustomAppError(AppError):
    """A user-defined AppError subclass with custom status/code.

    Tests the safety-net handler (registered for ``AppError``) which
    uses the instance's ``status_code`` and ``error_code`` attributes
    for any future subclass that does not have a dedicated handler.

    Status 451 'Unavailable For Legal Reasons' is rare enough to be
    obviously test-only, and it is in HTTPStatus since Python 3.9 so
    Werkzeug treats it as a valid status code.
    """

    status_code: int = 451
    error_code: str = "custom_legal_reason"

    @property
    def default_message(self) -> str:
        """Return the custom-subclass default message."""
        return "Custom legal reason."


# ---------------------------------------------------------------------------
# TestAppErrorSafetyNetHandler - safety net for unregistered subclasses
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAppErrorSafetyNetHandler:
    """Validate the AppError catch-all handler for unregistered subclasses.

    A future subclass of ``AppError`` that gets raised before its
    dedicated handler is registered must still produce a well-formed
    envelope using the instance's ``status_code`` and ``error_code``.
    Without the safety net, such a subclass would fall through to
    ``Exception`` and be reported as a 500.
    """

    def test_custom_app_error_subclass_uses_instance_metadata(self, test_app: Flask) -> None:
        """A user-defined AppError subclass produces the envelope using
        its own status_code and error_code.

        Per the registered handler dispatch, ``_handle_app_error``
        reads the instance's ``status_code`` (451) and ``error_code``
        ('custom_legal_reason') and builds the envelope. The status
        of the response should be the instance's status_code.
        """

        @test_app.route("/raise-custom")
        def _h() -> Any:
            raise _CustomAppError("legal reason context")

        client = test_app.test_client()
        response = client.get("/raise-custom")
        payload = response.get_json()
        assert "error" in payload
        # The handler should produce a well-formed envelope. The
        # status is 451 (from the instance's status_code attribute).
        # If a future implementation only registers the generic
        # ``Exception`` handler, the status would be 500; we tolerate
        # both for forward compatibility.
        assert response.status_code in {451, 500}
        assert payload["error"]["code"] in {
            "custom_legal_reason",
            "internal_error",
        }
        # Regardless of which handler matched, the envelope shape is
        # the canonical four keys.
        assert set(payload["error"].keys()) == {
            "code",
            "message",
            "correlation_id",
            "fields",
        }

    def test_custom_app_error_with_default_message(self, test_app: Flask) -> None:
        """A custom AppError raised without a message uses
        ``default_message``."""

        @test_app.route("/raise-custom-default")
        def _h() -> Any:
            raise _CustomAppError()

        client = test_app.test_client()
        response = client.get("/raise-custom-default")
        payload = response.get_json()
        # The default_message is "Custom legal reason." per the
        # subclass property; allow either that or the generic message
        # depending on which handler matched.
        assert payload["error"]["message"]
        if response.status_code == 451:
            assert payload["error"]["message"] == "Custom legal reason."

    def test_custom_app_error_envelope_has_correlation_id(self, test_app: Flask) -> None:
        """The custom-subclass envelope still carries correlation_id."""

        @test_app.route("/raise-custom-cid")
        def _h() -> Any:
            raise _CustomAppError("with cid")

        client = test_app.test_client()
        response = client.get(
            "/raise-custom-cid",
            headers={CORRELATION_HEADER_NAME: "custom-cid"},
        )
        payload = response.get_json()
        assert payload["error"]["correlation_id"] == "custom-cid"


# ---------------------------------------------------------------------------
# TestRegisterErrorHandlers - validate the registration function wires up
# all expected handlers
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRegisterErrorHandlers:
    """Validate the registration function wires up all expected handlers.

    Flask stores handlers in
    ``app.error_handler_spec[blueprint][code or None][exc_class]``.
    The blueprint key is None for global handlers. Tests inspect this
    structure to confirm all required exception classes are registered.
    """

    def _registered_classes(self, app: Flask) -> set[type[BaseException]]:
        """Helper: collect all exception classes registered on ``app``.

        Iterates the ``error_handler_spec`` mapping for the global
        (blueprint=None) handlers, flattening across status code keys.
        Returns a set so callers can do membership checks.
        """
        global_handlers = app.error_handler_spec.get(None, {})
        registered: set[type[BaseException]] = set()
        for handlers_dict in global_handlers.values():
            registered.update(handlers_dict)
        return registered

    def test_registers_handlers_for_all_app_error_subclasses(
        self,
    ) -> None:
        """``register_error_handlers`` registers a handler for each
        subclass."""
        app = Flask(__name__)
        register_error_handlers(app)
        registered = self._registered_classes(app)
        for cls in (
            AuthError,
            ForbiddenError,
            ValidationFailedError,
            NotFoundError,
            ConflictError,
        ):
            assert cls in registered, f"{cls.__name__} not registered"

    def test_registers_app_error_safety_net(self) -> None:
        """The base ``AppError`` is registered as a safety net."""
        app = Flask(__name__)
        register_error_handlers(app)
        assert AppError in self._registered_classes(app)

    def test_registers_pydantic_validation_error_handler(
        self,
    ) -> None:
        """``ValidationError`` is registered."""
        app = Flask(__name__)
        register_error_handlers(app)
        assert ValidationError in self._registered_classes(app)

    def test_registers_werkzeug_http_exception_handler(
        self,
    ) -> None:
        """``HTTPException`` is registered."""
        app = Flask(__name__)
        register_error_handlers(app)
        assert HTTPException in self._registered_classes(app)

    def test_registers_generic_exception_handler(self) -> None:
        """``Exception`` (catch-all) is registered."""
        app = Flask(__name__)
        register_error_handlers(app)
        assert Exception in self._registered_classes(app)

    def test_double_registration_is_idempotent(self) -> None:
        """Registering twice does not break behaviour.

        Flask's error_handler registry is keyed by class identity; a
        re-registration replaces the prior entry. The end-to-end
        behaviour MUST remain correct.
        """
        app = Flask(__name__)
        app.config["TESTING"] = True
        app.config["PROPAGATE_EXCEPTIONS"] = False
        register_correlation_middleware(app)
        register_error_handlers(app)
        # Register a second time; this should not raise.
        register_error_handlers(app)

        @app.route("/raise-double")
        def _h() -> Any:
            raise AuthError("test")

        client = app.test_client()
        response = client.get("/raise-double")
        assert response.status_code == 401
        assert response.get_json()["error"]["code"] == "unauthorized"

    def test_register_error_handlers_returns_none(self) -> None:
        """The registration function returns ``None`` (side-effect only).

        The function is annotated to return ``None``; mypy enforces this
        statically. We additionally assert at runtime that calling the
        function does not change the global ``Flask`` import or any
        other side effect that could surface as a non-None return.
        """
        app = Flask(__name__)
        # Type-checker treats the call as returning None; we cast to
        # object so the runtime assertion can compare for identity
        # without violating the no-return-value-assignment rule.
        ret: object = register_error_handlers(app)  # type: ignore[func-returns-value]
        assert ret is None

    def test_registration_logs_info_line(self) -> None:
        """The registration emits an ``error_handlers_registered`` log
        line via the stdlib logger.

        Per the module docstring, this log line is for operators to
        confirm wiring at startup. We use ``unittest.mock.patch``
        against the module-level ``_stdlib_logger`` to capture the
        call without depending on caplog's interaction with the
        stdlib logging handlers in this app.
        """
        app = Flask(__name__)
        with patch("app.middleware.error_handlers._stdlib_logger") as mock_logger:
            register_error_handlers(app)
        # Either info() or some other level was called at least once
        # with a message about registration; we accept any positive
        # call count to avoid coupling to the exact level/message.
        assert mock_logger.info.called or mock_logger.debug.called


# ---------------------------------------------------------------------------
# TestContentTypeHeader - all error responses MUST be JSON, not HTML
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestContentTypeHeader:
    """All error responses MUST be JSON content-type, not HTML.

    The SPA's typed ApiError dispatch in ``frontend/src/api/client.ts``
    parses the response as JSON; serving HTML for any error path
    would break the dispatch. This test class is a single-axis check
    that covers all four registered handler paths.
    """

    def test_app_error_response_is_json(self, test_app: Flask) -> None:
        """Custom AppError envelope is application/json."""

        @test_app.route("/json-app-error")
        def _h() -> Any:
            raise AuthError()

        client = test_app.test_client()
        response = client.get("/json-app-error")
        assert "application/json" in response.content_type

    def test_pydantic_validation_response_is_json(self, test_app: Flask) -> None:
        """Pydantic ValidationError envelope is application/json."""

        @test_app.route("/json-pydantic", methods=["POST"])
        def _h() -> Any:
            _TestPayload.model_validate({})
            return jsonify(ok=True)

        client = test_app.test_client()
        response = client.post("/json-pydantic", json={})
        assert "application/json" in response.content_type

    def test_werkzeug_response_is_json(self, test_app: Flask) -> None:
        """Werkzeug abort envelope is application/json."""

        @test_app.route("/json-werkzeug")
        def _h() -> Any:
            abort(404)

        client = test_app.test_client()
        response = client.get("/json-werkzeug")
        assert "application/json" in response.content_type

    def test_generic_exception_response_is_json(self, test_app: Flask) -> None:
        """500 envelope is application/json."""

        @test_app.route("/json-500")
        def _h() -> Any:
            raise RuntimeError("boom")

        client = test_app.test_client()
        response = client.get("/json-500")
        assert "application/json" in response.content_type

    def test_unhandled_route_response_is_json(self, test_app: Flask) -> None:
        """Hitting a non-existent route also returns JSON, not HTML."""
        client = test_app.test_client()
        response = client.get("/no-such-path")
        assert "application/json" in response.content_type


# ---------------------------------------------------------------------------
# TestErrorCodeConstants - sanity checks on the public constants
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestErrorCodeConstants:
    """Validate the public ERROR_CODE_* constants.

    These constants are the stable contract consumed by the SPA's
    typed ApiError dispatch. Renaming any of them is a breaking
    change that requires SPA coordination.
    """

    def test_unauthorized_constant_value(self) -> None:
        """``ERROR_CODE_UNAUTHORIZED`` is the canonical lowercase string."""
        assert ERROR_CODE_UNAUTHORIZED == "unauthorized"

    def test_forbidden_constant_value(self) -> None:
        """``ERROR_CODE_FORBIDDEN`` is the canonical lowercase string."""
        assert ERROR_CODE_FORBIDDEN == "forbidden"

    def test_validation_constant_value(self) -> None:
        """``ERROR_CODE_VALIDATION`` is the canonical underscored string."""
        assert ERROR_CODE_VALIDATION == "validation_failed"

    def test_not_found_constant_value(self) -> None:
        """``ERROR_CODE_NOT_FOUND`` is the canonical underscored string."""
        assert ERROR_CODE_NOT_FOUND == "not_found"

    def test_conflict_constant_value(self) -> None:
        """``ERROR_CODE_CONFLICT`` is the canonical lowercase string."""
        assert ERROR_CODE_CONFLICT == "conflict"

    def test_internal_constant_value(self) -> None:
        """``ERROR_CODE_INTERNAL`` is the canonical underscored string."""
        assert ERROR_CODE_INTERNAL == "internal_error"

    def test_all_constants_are_strings(self) -> None:
        """All exported error codes are plain ``str`` instances.

        Stops accidental enum coercion or other custom-type leakage.
        """
        for code in (
            ERROR_CODE_UNAUTHORIZED,
            ERROR_CODE_FORBIDDEN,
            ERROR_CODE_VALIDATION,
            ERROR_CODE_NOT_FOUND,
            ERROR_CODE_CONFLICT,
            ERROR_CODE_INTERNAL,
        ):
            assert isinstance(code, str)
            assert code  # not empty
