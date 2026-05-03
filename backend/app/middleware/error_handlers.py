"""Uniform JSON error envelope handlers and exception class hierarchy.

This module is the single source of truth for how the Sales-Connections
backend reports errors to the SPA. Every error response leaves the
server in the form mandated by AAP Section 0.4.3::

    {"error": {"code": "...", "message": "...", "correlation_id": "...", "fields": []}}

The frontend's ``frontend/src/api/client.ts`` wrapper consumes this
envelope and dispatches typed exceptions or a global toast.

Public surface:

* Exception classes: ``AuthError``, ``ForbiddenError``,
  ``ValidationFailedError``, ``NotFoundError``, ``ConflictError``.
  Service-layer code raises these; the API layer does not need to
  catch them because Flask's error-handler mechanism converts them
  automatically.
* ``build_error_response(code, message, status, fields)`` -- helper
  that constructs an envelope deterministically. Used by the
  registered handlers AND by callers that need to bypass exception
  flow (e.g., the auth middleware's 401 response).
* ``register_error_handlers(app)`` -- the registration function
  invoked by ``app.__init__.create_app()`` LAST in the middleware
  registration sequence.

Security invariants:

* NEVER leak stack traces to the client.
* NEVER leak SQL strings, schema details, or internal type names to
  the client.
* NEVER leak secrets to the client (the ``message`` field is set by
  the application, not by the exception's repr).
* ALWAYS log the underlying exception with structlog at the
  appropriate level (``warning`` for client errors, ``error`` for
  server errors) before returning the envelope.
* ALWAYS attach the correlation ID so support engineers can find the
  log line that corresponds to the user-visible error.
"""

from __future__ import annotations

# Standard library imports.
#
# NOTE: ``logging`` is imported via ``importlib`` (not ``import logging``)
# because the ``app`` namespace tree contains a sibling module
# ``app.observability.logging`` (a file literally named ``logging.py``).
# Toolchain configurations that treat ``app/`` as a PEP 420 namespace
# package without ``explicit_package_bases`` (notably the project's
# current mypy configuration) misresolve a direct ``import logging``
# from inside a sibling ``app.*`` module as a self-import against
# ``app.observability.logging`` rather than the stdlib. This matches
# the convention established in ``app/middleware/correlation.py`` and
# ``app/observability/tracing.py``: at runtime the behavior is
# identical to ``import logging``, and at type-check time the ``Any``
# annotation defuses the namespace conflict without affecting the
# runtime behavior or surface area.
import http
import importlib
import traceback
from typing import TYPE_CHECKING, Any

# Third-party runtime imports.
#
# ``Flask``-app-only ``request`` and ``g`` are LocalProxy objects that
# resolve to the active request/app context at call time. ``jsonify``
# is a helper that produces a ``Response`` object with the
# ``application/json`` content type.
from flask import g, jsonify, request
from pydantic import ValidationError
import structlog
from werkzeug.exceptions import HTTPException

# Local imports - sibling middleware. Absolute imports per the
# project's ``flake8-tidy-imports`` configuration (relative imports
# are banned).
from app.middleware.correlation import CORRELATION_HEADER_NAME

# Type-only imports. Under ``from __future__ import annotations`` these
# are NEVER evaluated at runtime (PEP 563), so they live in a
# ``TYPE_CHECKING`` block to satisfy the project's strict
# ``flake8-type-checking`` configuration. ``Mapping`` and ``Sequence``
# are used as parameter type hints; ``Flask`` and ``Response`` are
# used as parameter and return type hints.
if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from flask import Flask, Response


# Deferred stdlib ``logging`` load via ``importlib`` (see NOTE above).
# The ``Any`` annotation is intentional: it makes the wrapper opaque
# to mypy so the type-checker does not attempt the (incorrect,
# namespace-package-quirk) resolution against the sibling
# ``app.observability.logging``. The runtime behavior is identical to
# ``import logging``.
logging: Any = importlib.import_module("logging")


# ---------------------------------------------------------------------------
# Module loggers
# ---------------------------------------------------------------------------

# Stdlib logger - used to emit a single info line on registration so
# operators can confirm wiring at startup. Runtime structured logs
# flow through ``_logger`` (structlog) instead.
_stdlib_logger = logging.getLogger(__name__)

# Structlog logger - used by ``_log_exception`` for every error log
# line. structlog's ``merge_contextvars`` processor (configured in
# ``app.observability.logging``) automatically surfaces the
# request-scoped ``correlation_id``, ``user_id``, ``org_id`` bound by
# the correlation/auth middleware so we do not need to duplicate them
# in the payload.
_logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Module constants - error codes
# ---------------------------------------------------------------------------

# Stable error codes consumed by the SPA's typed ApiError dispatch.
# Adding a new code is a non-breaking change; renaming an existing
# code is breaking and requires SPA coordination.
ERROR_CODE_UNAUTHORIZED: str = "unauthorized"
ERROR_CODE_FORBIDDEN: str = "forbidden"
ERROR_CODE_VALIDATION: str = "validation_failed"
ERROR_CODE_NOT_FOUND: str = "not_found"
ERROR_CODE_CONFLICT: str = "conflict"
ERROR_CODE_INTERNAL: str = "internal_error"
ERROR_CODE_HTTP_GENERIC: str = "http_error"
# Stable code for HTTP 503 (Service Unavailable). Used when an
# upstream dependency (Google OAuth, Anthropic, RDS) is misconfigured
# or temporarily unreachable. Per AAP section 0.4.3, configuration
# gaps on optional dependencies surface as 503, never 500: a 500
# implies a server-side bug needing engineering attention, while a
# 503 lets the SPA render a non-blocking "feature unavailable"
# affordance and lets the operator know the issue is environmental.
ERROR_CODE_SERVICE_UNAVAILABLE: str = "service_unavailable"


# ---------------------------------------------------------------------------
# Module constants - generic user-facing messages
# ---------------------------------------------------------------------------

# Generic 500 message: never expose internal details to the client.
# Engineers retrieve the actual exception from the structured log line
# using the correlation_id echoed in the response envelope.
_GENERIC_INTERNAL_ERROR_MESSAGE: str = (
    "An unexpected error occurred. Please try again or contact support "
    "with the correlation_id from this response."
)

# Generic 503 message used when an optional upstream dependency
# (Google OAuth, Anthropic, RDS) is misconfigured or temporarily
# unreachable. The SPA renders a non-blocking "feature unavailable"
# affordance for these so the user can continue with alternative
# flows (e.g., email/password fallback for OAuth).
_GENERIC_SERVICE_UNAVAILABLE_MESSAGE: str = (
    "This feature is temporarily unavailable. Please try again later."
)

# Generic 401 message used when the auth middleware rejects a request
# without specifying a sub-reason (avoids leaking whether the cookie
# was missing vs. expired vs. malformed).
_GENERIC_UNAUTHORIZED_MESSAGE: str = "Authentication required."

# Generic 403 message used when RBAC rejects a request.
_GENERIC_FORBIDDEN_MESSAGE: str = "You do not have permission to perform this action."


# ---------------------------------------------------------------------------
# Module public API
# ---------------------------------------------------------------------------

# NOTE: ``__all__`` is sorted alphabetically (RUF022 "isort-style"
# sorting). The categories - error-code constants, exception classes,
# helpers, and the registration entry point - are documented in the
# module docstring at the top of this file rather than as inline
# section comments here, because RUF022 forbids the section comments
# that would otherwise group them visually within the list.
__all__ = [
    "ERROR_CODE_CONFLICT",
    "ERROR_CODE_FORBIDDEN",
    "ERROR_CODE_HTTP_GENERIC",
    "ERROR_CODE_INTERNAL",
    "ERROR_CODE_NOT_FOUND",
    "ERROR_CODE_SERVICE_UNAVAILABLE",
    "ERROR_CODE_UNAUTHORIZED",
    "ERROR_CODE_VALIDATION",
    "AppError",
    "AuthError",
    "ConflictError",
    "ForbiddenError",
    "NotFoundError",
    "ServiceUnavailableError",
    "ValidationFailedError",
    "build_error_response",
    "register_error_handlers",
]


# ---------------------------------------------------------------------------
# Exception class hierarchy
# ---------------------------------------------------------------------------


class AppError(Exception):
    """Base class for all application-defined HTTP-mapped exceptions.

    Carries the HTTP status code, the stable error code, the
    user-facing message, and an optional structured ``fields`` list
    describing field-level validation problems.

    Subclasses set ``status_code`` and ``error_code`` as class
    attributes so the registered Flask error handlers can produce a
    response without per-instance configuration.

    Service-layer code raises subclasses (e.g.,
    ``raise NotFoundError("Connection not found")``); the API layer
    does not need to catch them because the registered handlers
    convert them automatically.

    Attributes:
        status_code: HTTP status code for the response (e.g., 401).
        error_code: Stable string code for the SPA's typed dispatch
            (e.g., ``"unauthorized"``).
        message: User-facing message included in the envelope.
        fields: Optional list of field-level error dicts. Each dict
            SHOULD contain at minimum ``loc`` (string or list) and
            ``msg`` (string).
    """

    # Default status/code; subclasses override. These are immutable
    # primitives, so they do not require ``ClassVar`` (RUF012 only
    # flags mutable defaults).
    status_code: int = http.HTTPStatus.INTERNAL_SERVER_ERROR.value  # 500
    error_code: str = ERROR_CODE_INTERNAL

    def __init__(
        self,
        message: str | None = None,
        *,
        fields: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        """Initialize the exception with a user-facing message and fields.

        Args:
            message: User-facing message. When ``None`` the class's
                ``default_message`` property is used so a misconfigured
                subclass does not leak Python's default ``Exception``
                repr.
            fields: Optional iterable of field-level error dicts. Copied
                into a fresh ``list[dict]`` so the caller cannot mutate
                the stored value after construction. ``None`` becomes
                an empty list.
        """
        # Use the class default message if none provided. Subclasses
        # override ``default_message`` to provide a stable default.
        self.message: str = message if message else self.default_message
        # Normalize ``fields`` into a mutable list of dicts so callers
        # can append to the stored list (e.g., when accumulating
        # multiple validation problems). The constructor parameter is
        # ``Sequence[Mapping[...]]`` (more permissive: tuples, frozen
        # dicts, etc.) while the stored attribute is the canonical
        # ``list[dict]``.
        self.fields: list[dict[str, Any]] = [dict(f) for f in fields] if fields else []
        super().__init__(self.message)

    @property
    def default_message(self) -> str:
        """Return the user-facing message used when none is supplied.

        Subclasses override this to provide a stable default. The base
        class returns the generic 500 message so a misconfigured
        subclass does not leak Python's default ``Exception`` repr.
        """
        return _GENERIC_INTERNAL_ERROR_MESSAGE


class AuthError(AppError):
    """Raised when authentication is required but missing or invalid.

    Mapped to HTTP 401 with ``error.code = "unauthorized"``. Raised by
    ``app.middleware.auth`` when the session cookie is absent,
    malformed, or carries an expired JWT.

    Carrying a default ``WWW-Authenticate`` semantics is OPTIONAL; we
    omit it because the SPA does not use HTTP Basic challenges.
    """

    status_code: int = http.HTTPStatus.UNAUTHORIZED.value  # 401
    error_code: str = ERROR_CODE_UNAUTHORIZED

    @property
    def default_message(self) -> str:
        """Return the generic 401 message."""
        return _GENERIC_UNAUTHORIZED_MESSAGE


class ForbiddenError(AppError):
    """Raised when the caller is authenticated but lacks permission.

    Mapped to HTTP 403 with ``error.code = "forbidden"``. Raised by
    ``app.middleware.rbac.requires_role(...)`` when the caller's role
    is not in the allowed set, and by service-layer code that detects
    org-scope violations.

    Note: this is a CUSTOM exception class, NOT Python's built-in
    ``PermissionError`` (which is an ``OSError`` subclass for
    filesystem operations). Using a custom class avoids accidental
    catch-by-base in unrelated code paths and lets us attach
    HTTP-specific metadata (status_code, error_code) without
    violating LSP.
    """

    status_code: int = http.HTTPStatus.FORBIDDEN.value  # 403
    error_code: str = ERROR_CODE_FORBIDDEN

    @property
    def default_message(self) -> str:
        """Return the generic 403 message."""
        return _GENERIC_FORBIDDEN_MESSAGE


class ValidationFailedError(AppError):
    """Raised by services to report business-rule validation failures.

    Mapped to HTTP 422 with ``error.code = "validation_failed"``.
    Distinct from pydantic's ``ValidationError`` (which is also
    mapped to 422 by a separate handler): pydantic catches
    schema/type violations at the API boundary, while this class
    reports business-rule violations from the service layer (e.g.,
    "tag name already exists in this org").
    """

    status_code: int = http.HTTPStatus.UNPROCESSABLE_ENTITY.value  # 422
    error_code: str = ERROR_CODE_VALIDATION

    @property
    def default_message(self) -> str:
        """Return the default validation-failed message."""
        return "The request payload failed validation."


class NotFoundError(AppError):
    """Raised by services when a requested entity does not exist.

    Mapped to HTTP 404 with ``error.code = "not_found"``. Org-scope
    violations (e.g., requesting a record that exists in a different
    org) are also mapped to 404 by services rather than 403, so the
    response does not leak the existence of cross-org records.
    """

    status_code: int = http.HTTPStatus.NOT_FOUND.value  # 404
    error_code: str = ERROR_CODE_NOT_FOUND

    @property
    def default_message(self) -> str:
        """Return the default not-found message."""
        return "The requested resource was not found."


class ConflictError(AppError):
    """Raised by services when a request conflicts with current state.

    Mapped to HTTP 409 with ``error.code = "conflict"``. Used for
    uniqueness violations (e.g., creating a tag that already exists
    under a different casing within the same org) and concurrent
    modifications.
    """

    status_code: int = http.HTTPStatus.CONFLICT.value  # 409
    error_code: str = ERROR_CODE_CONFLICT

    @property
    def default_message(self) -> str:
        """Return the default conflict message."""
        return "The request conflicts with the current state of the resource."


class ServiceUnavailableError(AppError):
    """Raised when an upstream dependency is misconfigured or unreachable.

    Mapped to HTTP 503 with ``error.code = "service_unavailable"``.
    Per AAP section 0.4.3, configuration gaps on optional dependencies
    (e.g., ``GOOGLE_OAUTH_CLIENT_ID`` empty) surface as 503, never as
    500. A 500 implies a server-side defect that engineers must
    debug; a 503 communicates a known environmental condition that
    operators must address by configuring the dependency.

    Use cases:
        * Google OAuth not configured (``oauth.create_client('google')``
          returns ``None``) - the SPA falls back to email/password.
        * Anthropic API key empty (``ANTHROPIC_API_KEY`` not set) -
          the SPA renders a non-blocking "AI unavailable" affordance.
        * RDS readiness probe failure - downstream load balancers
          drain traffic from the unhealthy task.

    Subclasses or callers SHOULD pass an explicit ``message`` and
    optionally override ``error_code`` for finer-grained dispatch
    (e.g., ``"oauth_not_configured"``).
    """

    status_code: int = http.HTTPStatus.SERVICE_UNAVAILABLE.value  # 503
    error_code: str = ERROR_CODE_SERVICE_UNAVAILABLE

    @property
    def default_message(self) -> str:
        """Return the generic 503 message."""
        return _GENERIC_SERVICE_UNAVAILABLE_MESSAGE


# ---------------------------------------------------------------------------
# Public helper: build_error_response
# ---------------------------------------------------------------------------


def build_error_response(
    code: str,
    message: str,
    status: int,
    fields: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[Response, int]:
    """Build a uniform error envelope and return ``(response, status)``.

    The envelope schema is fixed by AAP Section 0.4.3::

        {"error": {"code": "...", "message": "...", "correlation_id": "...", "fields": []}}

    The ``correlation_id`` field is sourced from ``g.correlation_id``
    (set by ``app.middleware.correlation``). When ``g`` is not
    available (e.g., the helper is called outside a request context),
    the correlation_id falls back to an empty string rather than
    raising; this keeps the helper safe for use in startup/teardown
    diagnostics.

    The response sets the ``Content-Type: application/json`` header
    automatically via Flask's ``jsonify``. It also echoes the
    correlation ID via the ``X-Correlation-Id`` response header so
    callers can find the request in logs WITHOUT parsing the body.

    Args:
        code: Stable error code from the ``ERROR_CODE_*`` constants.
        message: User-facing message; MUST NOT leak internal details.
        status: HTTP status code (e.g., 401, 403, 404, 409, 422, 500).
        fields: Optional iterable of field-level error dicts.

    Returns:
        A tuple of (Flask Response, status int) suitable for direct
        return from a Flask error handler or view function.
    """
    # Resolve the correlation ID. Outside a request context, Flask's
    # ``g`` proxy raises ``RuntimeError("Working outside of application
    # context")`` on attribute access; the try/except is the canonical
    # pattern. We coerce ``None`` and empty values to ``""`` so the
    # envelope always carries a string (never ``null``).
    try:
        raw = getattr(g, "correlation_id", "")
        correlation_id = str(raw) if raw else ""
    except RuntimeError:
        correlation_id = ""

    # Build the envelope. ``fields`` is normalized to a fresh list of
    # dicts so callers cannot mutate it after the response is built.
    envelope: dict[str, Any] = {
        "error": {
            "code": code,
            "message": message,
            "correlation_id": correlation_id,
            "fields": [dict(f) for f in (fields or [])],
        }
    }
    response = jsonify(envelope)
    response.status_code = status
    # Echo the correlation ID via the response header. This is
    # defense-in-depth: the correlation middleware's after_request
    # hook also sets this header on every response, but error
    # responses sometimes bypass that hook (e.g., when an error fires
    # inside teardown or before a request context is fully
    # established). Setting the header here guarantees the SPA sees
    # it on every error response.
    if correlation_id:
        response.headers[CORRELATION_HEADER_NAME] = correlation_id
    return response, status


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _serialize_pydantic_errors(exc: ValidationError) -> list[dict[str, Any]]:
    """Convert a pydantic ``ValidationError`` to envelope ``fields``.

    Each entry contains:
        * ``loc``  -- list of path segments (strings/ints) into the
                      payload, e.g., ``["body", "linkedin_url"]``.
        * ``msg``  -- pydantic's human-readable error message.
        * ``type`` -- pydantic's stable error code, e.g.,
                      ``"value_error.url"``.

    Notes:
        * pydantic's ``errors()`` method may include an ``input``
          field carrying user-supplied data; we DROP it because user
          input must never be echoed in error envelopes (could leak
          PII).
        * ``ctx`` (additional context) is also dropped because it can
          contain internal regex patterns or constraint values that
          should not leak.
        * ``url`` (link to pydantic docs) is also dropped because the
          SPA does not need it and it adds payload weight.

    Args:
        exc: The pydantic ``ValidationError`` whose ``errors()`` are
            being normalized.

    Returns:
        A list of dicts with the safe-to-echo subset of fields.
    """
    output: list[dict[str, Any]] = []
    for err in exc.errors():
        loc = err.get("loc", ())
        # Convert every loc segment to str for JSON safety while
        # preserving structure. pydantic 2.x sometimes uses tuples
        # of mixed types (str + int for list indices).
        loc_list = [str(seg) for seg in loc]
        output.append(
            {
                "loc": loc_list,
                "msg": str(err.get("msg", "Invalid value.")),
                "type": str(err.get("type", "value_error")),
            }
        )
    return output


def _safe_request_path() -> str | None:
    """Return ``request.path`` or ``None`` if unavailable.

    Flask's ``request`` is a LocalProxy that raises ``RuntimeError``
    on attribute access outside a request context. ``getattr`` does
    not catch ``RuntimeError`` (only ``AttributeError``), so we wrap
    explicitly. Used by ``_log_exception`` so log emission outside a
    request context (e.g., from a background task) does not crash.
    """
    try:
        return str(request.path)
    except RuntimeError:
        return None


def _safe_request_method() -> str | None:
    """Return ``request.method`` or ``None`` if unavailable.

    See ``_safe_request_path`` for rationale.
    """
    try:
        return str(request.method)
    except RuntimeError:
        return None


def _log_exception(
    level: str,
    *,
    exc: BaseException,
    code: str,
    status: int,
    message: str,
    include_traceback: bool = False,
) -> None:
    """Emit a structured log line for an error response.

    Logs at ``warning`` for client errors (4xx) and ``error`` for
    server errors (5xx). The correlation_id and (when available)
    user_id/org_id are surfaced automatically via structlog's
    ``merge_contextvars`` processor (configured in
    ``app.observability.logging``).

    For 5xx responses, ``include_traceback=True`` causes the full
    traceback to be included as a structured ``traceback`` field so
    it lands in CloudWatch but never reaches the client.

    The exception's class name and ``str()`` are logged as
    ``exception_class`` and ``exception_message`` so support engineers
    can identify the failure mode without the full traceback for 4xx
    cases.

    Args:
        level: Structlog level name. Expected values: ``"warning"``,
            ``"error"``, ``"info"``. Unknown levels fall back to
            ``error``.
        exc: The exception being handled.
        code: The stable error code being returned to the client.
        status: HTTP status code.
        message: Client-facing message (logged for completeness; the
            structlog processor chain may redact it before output).
        include_traceback: When True, attach a ``traceback`` field
            with the full formatted traceback. Used for 5xx
            responses; NEVER reaches the client because the client
            envelope is built separately by ``build_error_response``.
    """
    # structlog's BoundLogger exposes ``info``, ``warning``,
    # ``error`` etc. as instance methods. ``getattr`` resolves the
    # name dynamically and falls back to ``error`` for safety. Using
    # ``Any`` annotation hint internally because structlog stubs are
    # ignored per pyproject.toml mypy override.
    log_method = getattr(_logger, level, _logger.error)
    payload: dict[str, Any] = {
        "event": "http_error_response",
        "error_code": code,
        "status_code": status,
        "client_message": message,
        "exception_class": type(exc).__name__,
        "exception_message": str(exc),
        "path": _safe_request_path(),
        "method": _safe_request_method(),
    }
    if include_traceback:
        # ``traceback.format_exception`` returns a list of strings;
        # joining produces the canonical Python traceback format. We
        # log the full traceback for 5xx responses so engineers can
        # debug from CloudWatch without needing local repro. The
        # client envelope NEVER receives this field.
        payload["traceback"] = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )
    log_method(**payload)


# ---------------------------------------------------------------------------
# Per-exception handler functions
# ---------------------------------------------------------------------------


def _handle_auth_error(exc: AuthError) -> tuple[Response, int]:
    """Map ``AuthError`` to a 401 envelope.

    Logs at ``warning`` level (4xx is a client error). Carries the
    instance's ``message`` and ``fields`` into the envelope.
    """
    _log_exception(
        "warning",
        exc=exc,
        code=exc.error_code,
        status=exc.status_code,
        message=exc.message,
    )
    return build_error_response(
        code=exc.error_code,
        message=exc.message,
        status=exc.status_code,
        fields=exc.fields,
    )


def _handle_forbidden_error(exc: ForbiddenError) -> tuple[Response, int]:
    """Map ``ForbiddenError`` to a 403 envelope.

    Logs at ``warning`` level (4xx is a client error). Carries the
    instance's ``message`` and ``fields`` into the envelope.
    """
    _log_exception(
        "warning",
        exc=exc,
        code=exc.error_code,
        status=exc.status_code,
        message=exc.message,
    )
    return build_error_response(
        code=exc.error_code,
        message=exc.message,
        status=exc.status_code,
        fields=exc.fields,
    )


def _handle_not_found_error(exc: NotFoundError) -> tuple[Response, int]:
    """Map ``NotFoundError`` to a 404 envelope.

    Logs at ``warning`` level (4xx is a client error). Carries the
    instance's ``message`` and ``fields`` into the envelope.
    """
    _log_exception(
        "warning",
        exc=exc,
        code=exc.error_code,
        status=exc.status_code,
        message=exc.message,
    )
    return build_error_response(
        code=exc.error_code,
        message=exc.message,
        status=exc.status_code,
        fields=exc.fields,
    )


def _handle_conflict_error(exc: ConflictError) -> tuple[Response, int]:
    """Map ``ConflictError`` to a 409 envelope.

    Logs at ``warning`` level (4xx is a client error). Carries the
    instance's ``message`` and ``fields`` into the envelope.
    """
    _log_exception(
        "warning",
        exc=exc,
        code=exc.error_code,
        status=exc.status_code,
        message=exc.message,
    )
    return build_error_response(
        code=exc.error_code,
        message=exc.message,
        status=exc.status_code,
        fields=exc.fields,
    )


def _handle_validation_failed_error(
    exc: ValidationFailedError,
) -> tuple[Response, int]:
    """Map ``ValidationFailedError`` to a 422 envelope.

    Distinct from the pydantic handler: this fires when a service
    raises ``ValidationFailedError`` for a business-rule violation
    (e.g., ``"tag name already exists in this org"``); the pydantic
    handler fires when the request payload fails schema/type
    validation at the API boundary.
    """
    _log_exception(
        "warning",
        exc=exc,
        code=exc.error_code,
        status=exc.status_code,
        message=exc.message,
    )
    return build_error_response(
        code=exc.error_code,
        message=exc.message,
        status=exc.status_code,
        fields=exc.fields,
    )


def _handle_pydantic_validation_error(
    exc: ValidationError,
) -> tuple[Response, int]:
    """Map pydantic ``ValidationError`` to a 422 envelope.

    pydantic raises ``ValidationError`` when a payload fails
    schema/type validation at the API boundary (e.g., a malformed
    LinkedIn URL or a missing required field). The handler extracts
    field-level details via ``_serialize_pydantic_errors`` so the SPA
    can highlight the offending fields.

    The ``message`` is a stable generic string so the envelope shape
    is predictable regardless of how many fields failed; per-field
    detail lives in ``fields``.
    """
    fields = _serialize_pydantic_errors(exc)
    message = "The request payload failed validation."
    status = http.HTTPStatus.UNPROCESSABLE_ENTITY.value
    _log_exception(
        "warning",
        exc=exc,
        code=ERROR_CODE_VALIDATION,
        status=status,
        message=message,
    )
    return build_error_response(
        code=ERROR_CODE_VALIDATION,
        message=message,
        status=status,
        fields=fields,
    )


def _handle_werkzeug_http_exception(
    exc: HTTPException,
) -> tuple[Response, int]:
    """Map Flask/werkzeug ``HTTPException`` to a uniform envelope.

    Catches ``abort(404)`` style raises and any other werkzeug
    exception (e.g., ``RequestEntityTooLarge`` -> 413). The status
    code and description come from the exception itself; the error
    code is derived from the HTTP status name (lower-cased) for
    stability.

    For 5xx werkzeug exceptions the user-facing message is replaced
    with ``_GENERIC_INTERNAL_ERROR_MESSAGE`` because werkzeug's
    default descriptions can leak internals (e.g., ``InternalServerError``
    sometimes carries the underlying exception class name on the
    description). 4xx descriptions are user-actionable and safe to
    echo.
    """
    status = int(exc.code or http.HTTPStatus.INTERNAL_SERVER_ERROR.value)
    # Derive a stable code from the HTTP status name when known;
    # fall back to ``"http_error"`` for unmapped codes.
    try:
        status_enum = http.HTTPStatus(status)
        code = status_enum.name.lower()
        phrase = status_enum.phrase
    except ValueError:
        code = ERROR_CODE_HTTP_GENERIC
        phrase = "HTTP error"
    if status >= http.HTTPStatus.INTERNAL_SERVER_ERROR.value:
        # 5xx: do not echo werkzeug's description; use the generic
        # message so we cannot accidentally leak internals.
        message = _GENERIC_INTERNAL_ERROR_MESSAGE
    else:
        # 4xx: prefer the exception's description when present
        # (werkzeug's HTTP-specific defaults are user-friendly), and
        # fall back to the canonical phrase from the HTTPStatus enum.
        message = exc.description if exc.description else phrase
    level = "warning" if status < http.HTTPStatus.INTERNAL_SERVER_ERROR.value else "error"
    _log_exception(
        level,
        exc=exc,
        code=code,
        status=status,
        message=message,
        include_traceback=status >= http.HTTPStatus.INTERNAL_SERVER_ERROR.value,
    )
    return build_error_response(
        code=code,
        message=message,
        status=status,
    )


def _handle_app_error(exc: AppError) -> tuple[Response, int]:
    """Handle any ``AppError`` subclass that lacks a more specific handler.

    Reads ``status_code`` and ``error_code`` from the instance and
    builds the envelope. This is the safety net for any future
    subclass of ``AppError`` that gets raised before its dedicated
    handler is registered. Without this safety net, such a subclass
    would fall through to the generic ``Exception`` handler and be
    reported as a 500 even though the instance carries a
    well-defined 4xx status.
    """
    is_server_error = exc.status_code >= http.HTTPStatus.INTERNAL_SERVER_ERROR.value
    level = "error" if is_server_error else "warning"
    _log_exception(
        level,
        exc=exc,
        code=exc.error_code,
        status=exc.status_code,
        message=exc.message,
        include_traceback=is_server_error,
    )
    return build_error_response(
        code=exc.error_code,
        message=exc.message,
        status=exc.status_code,
        fields=exc.fields,
    )


def _handle_unexpected_exception(exc: Exception) -> tuple[Response, int]:
    """Map any unhandled exception to a 500 envelope.

    The handler:
        1. Logs the FULL traceback at ``error`` level for engineers.
        2. Returns a GENERIC user-facing message; the actual
           exception class and message are NEVER leaked to the
           client. Engineers correlate via the ``correlation_id``
           echoed in the response.

    Per AAP Section 0.7.4 (Security Invariants), this is the last
    line of defense against information leakage. NEVER echo
    ``str(exc)``, ``repr(exc)``, ``type(exc).__name__``, or any
    internal detail in the user-facing envelope.
    """
    status = http.HTTPStatus.INTERNAL_SERVER_ERROR.value
    _log_exception(
        "error",
        exc=exc,
        code=ERROR_CODE_INTERNAL,
        status=status,
        message=_GENERIC_INTERNAL_ERROR_MESSAGE,
        include_traceback=True,
    )
    return build_error_response(
        code=ERROR_CODE_INTERNAL,
        message=_GENERIC_INTERNAL_ERROR_MESSAGE,
        status=status,
    )


# ---------------------------------------------------------------------------
# Public registration entry point
# ---------------------------------------------------------------------------


def register_error_handlers(app: Flask) -> None:
    """Register all error handlers on the Flask app.

    Per AAP Section 0.5.2 (Layer 0), this MUST be the LAST middleware
    registration so it sees exceptions raised by all upstream
    middleware (correlation, auth, rbac).

    Flask dispatches by exception class identity using class
    hierarchy lookup (most-specific subclass wins). Registering
    ``Exception`` first sets the catch-all; subsequent specific
    registrations override for their classes. The order in which we
    register is least-specific to most-specific for human
    readability; Flask's dispatch is identity-based, not order-based,
    so the registration order does not affect runtime behavior.

    Idempotent: registering the same handler twice is harmless
    because Flask's error-handler registry replaces by class
    identity.

    Args:
        app: The Flask application instance produced by
            ``app.create_app``. Handlers are registered on the
            application-wide registry, so they fire for every
            blueprint and every request.

    Returns:
        None. Side effects: mutates ``app.error_handler_spec``;
        emits a single ``error_handlers_registered`` info log line
        via the stdlib logger so operators can confirm wiring at
        startup.
    """
    # Catch-all 500 handler for any unhandled ``Exception``.
    app.register_error_handler(Exception, _handle_unexpected_exception)

    # Werkzeug HTTP exceptions (``abort(404)``, ``MethodNotAllowed``,
    # ``RequestEntityTooLarge``, etc.). Flask uses ``HTTPException``
    # for its built-in error responses; catching it here ensures
    # those produce the uniform JSON envelope rather than werkzeug's
    # default HTML error pages.
    app.register_error_handler(HTTPException, _handle_werkzeug_http_exception)

    # Pydantic validation errors raised at the API boundary. Mapped
    # to 422 with field-level details extracted via
    # ``_serialize_pydantic_errors``.
    app.register_error_handler(ValidationError, _handle_pydantic_validation_error)

    # ``AppError`` base class: safety net for any subclass that
    # gets raised before its dedicated handler is registered. Reads
    # ``status_code`` and ``error_code`` from the instance.
    app.register_error_handler(AppError, _handle_app_error)

    # App-defined exception subclasses. Each gets a dedicated
    # handler; Flask dispatches by class hierarchy so these take
    # precedence over the ``AppError`` and ``Exception`` handlers.
    app.register_error_handler(AuthError, _handle_auth_error)
    app.register_error_handler(ForbiddenError, _handle_forbidden_error)
    app.register_error_handler(ValidationFailedError, _handle_validation_failed_error)
    app.register_error_handler(NotFoundError, _handle_not_found_error)
    app.register_error_handler(ConflictError, _handle_conflict_error)

    _stdlib_logger.info("error_handlers_registered")
