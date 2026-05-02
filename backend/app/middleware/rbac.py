"""Role-Based Access Control (RBAC) decorator (F-009).

This module implements the API-layer authoritative authorization gate
for the Sales-Connections backend. It provides:

* ``requires_role(*roles)`` -- a decorator factory that gates a Flask
  handler by checking ``flask.g.session.role`` against an allowlist.
  The decorator runs AFTER the auth middleware has populated
  ``g.session`` (per the strict middleware order:
  ``correlation -> auth -> rbac decorators``).

* ``ForbiddenError`` -- re-exported from
  ``app.middleware.error_handlers`` for ergonomic single-import
  alongside the decorator. Consumers can write
  ``from app.middleware.rbac import ForbiddenError, requires_role``
  rather than splitting imports across two modules.

The Flask error handler that converts ``ForbiddenError`` into a 403
JSON envelope is registered exclusively by
:func:`app.middleware.error_handlers.register_error_handlers`. This
module deliberately does NOT register a duplicate handler: registering
the same exception class twice produces a silent last-write-wins
override at the Flask error_handler_spec level, which makes it harder
to audit which handler actually fires at runtime. Consolidating
registration to a single source of truth in ``error_handlers.py``
eliminates that risk while still producing the canonical
``{"error": {"code": "forbidden", ...}}`` envelope mandated by AAP
Section 0.4.3.

Performance contract (AAP Section 0.7.3):

    "RBAC authorization check << 50 ms -- Decorator runs in-process
     against JWT claims; no DB round-trip."

The decorator does NOT make any database queries, network calls, or
expensive operations. The only work performed per request is one
set-membership check against a small tuple captured by closure at
decoration time -- sub-microsecond cost, well within the 50 ms budget.

Architectural invariant (AAP Section 0.7.1, Invariant 7):

    "API-layer authorization is authoritative. The frontend's
     <RoleGate> is a UX courtesy; the backend RBAC decorator is the
     only authoritative gate. Never rely on the absence of a UI control
     to prevent an action."

Coordination with sibling modules:

* ``app.middleware.auth`` -- runs BEFORE this decorator and populates
  ``g.session`` (a frozen ``Session`` dataclass with ``user_id``,
  ``org_id``, ``role`` fields). Unauthenticated requests are rejected
  with 401 by that middleware before this decorator runs.

* ``app.middleware.error_handlers`` -- owns the ``ForbiddenError``
  exception class, the ``build_error_response`` envelope helper, AND
  the canonical Flask error-handler registration (via
  ``register_error_handlers``) that converts ``ForbiddenError``
  instances raised by the decorator below into 403 JSON envelopes.
  This module re-exports ``ForbiddenError`` for ergonomic imports;
  it does NOT register its own handler so that there is exactly one
  source of truth for error-envelope wiring.

* ``app.api.*`` -- every state-changing endpoint MUST be decorated:

    - ``POST /api/connections``               -> CONTRIBUTOR or ADMIN
    - ``PATCH /api/connections/:id``           -> CONTRIBUTOR or ADMIN
                                                  (own record); ADMIN
                                                  for any record
    - ``PATCH /api/connections/:id/status``    -> VIEWER or ADMIN
    - ``DELETE /api/connections/:id`` (soft)   -> any role for own
                                                  record; ADMIN otherwise
    - ``DELETE /api/admin/records/:id`` (hard) -> ADMIN only
    - All ``/api/admin/*`` endpoints           -> ADMIN

This module deliberately has no module-level side effects beyond the
ParamSpec/TypeVar declarations. Importing ``app.middleware.rbac``
does NOT log, does NOT make HTTP calls, does NOT touch the database,
and does NOT register Flask hooks. Wiring of the ``ForbiddenError``
-> 403 envelope handler is performed by
:func:`app.middleware.error_handlers.register_error_handlers`, which
``app.__init__::create_app`` MUST call as the final middleware
registration step.
"""

from __future__ import annotations

# Standard library imports.
#
# ``functools.wraps`` preserves the wrapped Flask handler's
# ``__name__``, ``__doc__``, and ``__wrapped__`` attributes; Flask's
# blueprint registration keys on view function names, so wraps is
# REQUIRED for correct route registration and pytest test discovery.
#
# ``logging`` provides the module-level logger used to emit structured
# rejection events. The stdlib logger is routed through structlog by
# ``app.observability.logging`` configuration so log lines carry the
# correlation_id, user_id, and org_id bound on contextvars by the
# correlation/auth middleware.
import functools
import logging
from typing import TYPE_CHECKING, ParamSpec, TypeVar, cast

# Third-party runtime imports.
#
# ``g`` is Flask's per-request global proxy. The decorator reads
# ``g.session`` (set by ``app.middleware.auth``) at request time.
from flask import g

# Local imports - sibling middleware and models. Absolute imports per
# the project's ``flake8-tidy-imports`` configuration (relative
# imports are banned).
#
# ``ForbiddenError`` is the canonical "permission denied" exception
# raised by ``requires_role`` and re-exported by this module so
# consumers can ``from app.middleware.rbac import ForbiddenError,
# requires_role`` in a single statement. The Flask error handler that
# converts ``ForbiddenError`` -> 403 JSON envelope is registered by
# :func:`app.middleware.error_handlers.register_error_handlers`; this
# module does NOT register a duplicate handler.
from app.middleware.error_handlers import ForbiddenError

# ``UserRole`` is the three-role authorization enum (ADMIN,
# CONTRIBUTOR, VIEWER). Used both for type-safe argument validation
# at decoration time AND for coercing the raw role claim at request
# time.
from app.models.enums import UserRole

# Type-only imports. Under ``from __future__ import annotations``
# these are NEVER evaluated at runtime (PEP 563), so they live in a
# ``TYPE_CHECKING`` block to satisfy the project's strict
# ``flake8-type-checking`` configuration. ``Callable`` types the
# decorator factory return.
if TYPE_CHECKING:
    from collections.abc import Callable


# ---------------------------------------------------------------------------
# Module logger
# ---------------------------------------------------------------------------

# Stdlib logger - structlog's ``merge_contextvars`` processor surfaces
# the correlation_id, user_id, and org_id bound by upstream middleware
# automatically, so log calls here do not need to duplicate them.
_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Typing primitives for the decorator
# ---------------------------------------------------------------------------

# ParamSpec preserves the wrapped Flask handler's exact parameter
# signature (positional args, keyword args) through the decorator so
# IDE autocomplete and mypy continue to work on decorated handlers.
# Without ParamSpec, mypy would see ``wrapper(*args, **kwargs)`` as
# ``Callable[..., Any]`` and break tooling on the wrapped handler.
P = ParamSpec("P")

# TypeVar preserves the wrapped handler's exact return type. Combined
# with ParamSpec this gives the decorator full signature transparency
# without erasing types.
R = TypeVar("R")


# ---------------------------------------------------------------------------
# Module public API
# ---------------------------------------------------------------------------

# ``__all__`` is sorted alphabetically (RUF022 "isort-style" sorting).
# ``ForbiddenError`` is re-exported so consumers can write
# ``from app.middleware.rbac import ForbiddenError, requires_role``
# rather than splitting imports across two modules. Note: this module
# deliberately does NOT export a ``register_*_error_handlers``
# function -- the canonical ``ForbiddenError`` -> 403 envelope handler
# is registered by :func:`app.middleware.error_handlers.register_error_handlers`.
__all__ = [
    "ForbiddenError",
    "requires_role",
]


# ---------------------------------------------------------------------------
# Private helper: role coercion
# ---------------------------------------------------------------------------


def _coerce_role(role: UserRole | str) -> UserRole:
    """Coerce a role argument to a ``UserRole`` enum member.

    Accepts either a ``UserRole`` member directly OR the string value
    (e.g., ``"Admin"``, ``"Contributor"``, ``"Viewer"``). Raises
    ``ValueError`` if the role is not a valid ``UserRole`` value.

    Used in two contexts:

    1. At DECORATION time, to validate every role passed to
       ``@requires_role(...)``. A typo like ``@requires_role("Adim")``
       MUST crash app startup with a descriptive error rather than
       silently rejecting every request to the decorated handler.

    2. At REQUEST time, to coerce ``g.session.role`` to a ``UserRole``
       for set-membership comparison. The auth middleware MAY set
       ``session.role`` as either a ``UserRole`` enum member (preferred)
       or a raw string; this helper handles both.

    Args:
        role: A ``UserRole`` member or the string value of a member
            (e.g., ``"Admin"``).

    Returns:
        The corresponding ``UserRole`` enum member.

    Raises:
        ValueError: When ``role`` is a string that does not match any
            ``UserRole`` member value. The error message lists the
            valid values so the operator can correct the typo
            immediately.
    """
    if isinstance(role, UserRole):
        return role
    try:
        return UserRole(role)
    except ValueError as exc:
        # Re-raise with a more actionable message that lists the valid
        # values. ``from exc`` preserves the original pydantic-style
        # cause chain for log forensics.
        raise ValueError(
            f"Invalid role {role!r} passed to @requires_role(...); "
            f"must be one of: {[m.value for m in UserRole]}"
        ) from exc


# ---------------------------------------------------------------------------
# Public decorator factory: requires_role
# ---------------------------------------------------------------------------


def requires_role(
    *roles: UserRole | str,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Gate a Flask handler by allowed roles.

    Usage::

        @requires_role(UserRole.ADMIN)
        def admin_only_handler(): ...


        @requires_role(UserRole.VIEWER, UserRole.ADMIN)
        def status_mutation_handler(): ...


        @requires_role("Admin")  # string value also accepted
        def alt_handler(): ...

    Behavior:

    1. Validates each supplied role at DECORATION time (catches typos
       early). Raises ``ValueError`` if any role is unrecognized; this
       surfaces during app startup, NOT at request time.
    2. At REQUEST time, reads ``g.session.role`` (set by the auth
       middleware) and raises ``ForbiddenError`` if the role is NOT in
       the allowlist.
    3. Logs the rejection at INFO level with structured context so
       security audit reviews can identify forbidden-access attempts
       (route, required_roles, actual_role, user_id, org_id).

    Defense-in-depth on missing session:

    The auth middleware (which runs BEFORE rbac decorators) is
    responsible for populating ``g.session`` and rejecting
    unauthenticated requests with 401. If ``g.session`` is somehow
    missing when this decorator runs (programming error / middleware
    misconfiguration), the decorator treats it as a forbidden access
    and raises ``ForbiddenError``. A 403 is returned even if the auth
    middleware was bypassed, so the API never silently admits an
    unauthenticated request.

    Performance:

    The decoration-time work (``_coerce_role`` validation, tuple
    construction) runs ONCE per route at app startup. The
    request-time work is one ``getattr`` against ``g``, one optional
    ``UserRole(...)`` coercion, and one tuple membership check --
    sub-microsecond total cost, well within the AAP Section 0.7.3
    sub-50 ms budget.

    Args:
        *roles: One or more ``UserRole`` enum members OR their string
            values (e.g., ``"Admin"``). Mixed types are accepted and
            normalized to ``UserRole`` at decoration time.

    Returns:
        A decorator that wraps a Flask handler with role-gating
        logic. The wrapped function preserves the original signature
        (via ``ParamSpec``) so IDE autocomplete and mypy continue to
        work.

    Raises:
        ValueError: At DECORATION time, when ``roles`` is empty or
            any role argument is not a valid ``UserRole``.
        ForbiddenError: At REQUEST time, when ``g.session`` is
            missing or its ``role`` is not in the allowlist. Caught
            by the Flask error handler registered by
            :func:`app.middleware.error_handlers.register_error_handlers`
            and converted to a 403 JSON envelope.
    """
    # Empty allowlist is a programming error: an "allow nobody"
    # decorator is meaningless. Crash at app startup so the operator
    # fixes the route definition rather than discovering a permanent
    # 403 in production.
    if not roles:
        raise ValueError("@requires_role(...) must be called with at least one role")

    # Validate each supplied role at DECORATION time so typos crash
    # app startup rather than silently rejecting every request. The
    # resulting tuple is captured by the closure below; tuple is
    # cheaper than a frozenset for membership checks at this scale
    # (1-3 members typically).
    allowed_roles: tuple[UserRole, ...] = tuple(_coerce_role(r) for r in roles)

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        """The decorator returned by ``requires_role(...)``.

        Wraps ``func`` with the per-request role check. ``functools.wraps``
        preserves the wrapped function's metadata so Flask's blueprint
        registration (which keys on view function names) and pytest
        test discovery work correctly.
        """

        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            """Per-request role check.

            Performs three checks in order:

            1. Is ``g.session`` populated? (defense-in-depth against
               middleware misconfiguration)
            2. Does ``session.role`` coerce to a valid ``UserRole``?
               (defense against tampered session payload)
            3. Is the coerced role in the allowlist? (the actual
               authorization decision)

            Any failure raises ``ForbiddenError``; success delegates
            to the wrapped handler.
            """
            # Defensive ``getattr`` rather than direct ``g.session``
            # access. Flask's ``g`` proxy raises ``AttributeError``
            # (NOT ``None``) for missing attributes; ``getattr`` with
            # a default returns ``None`` so we can branch cleanly.
            session = getattr(g, "session", None)
            if session is None:
                # Defense-in-depth: auth middleware should have either
                # populated g.session or returned 401 already. Reaching
                # here means a misconfigured app (e.g., RBAC decorator
                # applied to a route that the auth middleware exempts).
                # Reject as forbidden so the API never admits an
                # unauthenticated request.
                _logger.warning(
                    "rbac_no_session",
                    extra={
                        "required_roles": [r.value for r in allowed_roles],
                        "handler": func.__name__,
                    },
                )
                raise ForbiddenError(
                    message=("No authenticated session for this request; cannot evaluate role.")
                )

            # ``session.role`` may be a ``UserRole`` enum (preferred,
            # set by the auth middleware) or a raw string (defensive
            # fallback). Coerce to ``UserRole`` for type-safe set
            # membership comparison below.
            actual_role_raw = session.role
            try:
                actual_role = (
                    actual_role_raw
                    if isinstance(actual_role_raw, UserRole)
                    else UserRole(actual_role_raw)
                )
            except ValueError:
                # Session carries a role string that does not match
                # any ``UserRole`` member. This indicates a tampered
                # JWT, a stale claim from a deprecated role name, or
                # a programming error in the auth middleware. Reject
                # the request rather than silently admitting it. The
                # ``from None`` clause suppresses the ValueError
                # chain so the user-facing message is clean.
                _logger.warning(
                    "rbac_unknown_role",
                    extra={
                        "actual_role": str(actual_role_raw),
                        "required_roles": [r.value for r in allowed_roles],
                        "handler": func.__name__,
                        "user_id": str(getattr(session, "user_id", "")),
                        "org_id": str(getattr(session, "org_id", "")),
                    },
                )
                raise ForbiddenError(
                    message=(f"Unknown role {actual_role_raw!r} on session; request rejected.")
                ) from None

            # The actual authorization decision: membership check
            # against the closure-captured allowlist.
            if actual_role not in allowed_roles:
                # 403 path: the caller is authenticated but lacks
                # permission for this route. INFO level (not WARNING)
                # because forbidden requests from authenticated
                # callers are normal in a multi-role system; SIEM
                # tools alert on volumetric or pattern anomalies, not
                # individual rejections.
                _logger.info(
                    "rbac_forbidden",
                    extra={
                        "required_roles": [r.value for r in allowed_roles],
                        "actual_role": actual_role.value,
                        "handler": func.__name__,
                        "user_id": str(getattr(session, "user_id", "")),
                        "org_id": str(getattr(session, "org_id", "")),
                    },
                )
                raise ForbiddenError(
                    message=(f"Role {actual_role.value!r} is not permitted for this operation.")
                )

            # Authorized: delegate to the wrapped handler with the
            # original arguments unchanged.
            return func(*args, **kwargs)

        # ``functools.wraps`` returns a wrapper typed by mypy as
        # ``Callable[..., Any]`` because ``wraps`` is a generic
        # decorator over the original signature. The cast restores
        # the precise ``Callable[P, R]`` signature so consumers of the
        # decorated handler get full type information. The cast is
        # a no-op at runtime.
        return cast("Callable[P, R]", wrapper)

    return decorator
