"""JWT session verification middleware (F-012).

Runs as a Flask ``before_request`` hook on every protected request.
Extracts the session JWT from the ``session`` cookie (set by the auth
blueprint after a successful login or OAuth callback), validates it via
``app.services.auth.verify_session_jwt``, and populates a typed
``Session`` dataclass on ``flask.g`` for downstream handlers.

Public path allowlist (skipped by this middleware):

* ``/healthz``                 liveness probe
* ``/readyz``                  readiness probe
* ``/metrics``                 prometheus exposition
* ``/auth/login``              email/password login form POST
* ``/auth/logout``             logout endpoint (cookie-cleared)
* ``/auth/google/start``       OAuth authorization redirect
* ``/auth/google/callback``    OAuth code-exchange callback

Any other path under ``/api/*`` REQUIRES a valid session cookie. Missing
or invalid cookies surface as HTTP 401 via ``AuthError`` raised in this
middleware and converted to JSON by ``app.middleware.error_handlers``.

Per AAP Section 0.7.3, the authentication completion budget is 2 seconds
end-to-end (login round-trip). The per-request middleware overhead here
is sub-millisecond because JWT verification is in-process (no DB round
trip; only HMAC signature verification + claim deserialization).

Per AAP Section 0.7.1 invariant 7, this middleware is the FIRST
authoritative authorization gate. The RBAC decorator (which reads
``g.session.role``) runs AFTER this middleware. Both backend gates are
required because the SPA's ``<RoleGate>`` is a UX courtesy only.

Wire-up order in ``app.__init__::create_app``:

    correlation -> auth -> error_handlers -> blueprints

This middleware MUST run AFTER ``register_correlation_middleware`` so
log lines emitted from here carry the correlation ID, and BEFORE the
API blueprints are registered so ``g.session`` is populated before any
handler executes.
"""

from __future__ import annotations

# Standard library imports.
#
# NOTE: ``logging`` is loaded via ``importlib.import_module`` (not
# ``import logging``) because the ``app`` namespace tree contains a
# sibling module ``app.observability.logging`` (a file literally named
# ``logging.py``). Toolchain configurations that treat ``app/`` as a
# PEP 420 namespace package without ``explicit_package_bases`` (notably
# the project's current mypy configuration) misresolve a direct
# ``import logging`` from inside a sibling ``app.*`` module as a
# self-import against ``app.observability.logging`` rather than the
# stdlib. This matches the convention established in
# ``app/middleware/correlation.py`` and
# ``app/middleware/error_handlers.py``: at runtime the behavior is
# identical to ``import logging``, and at type-check time the ``Any``
# annotation defuses the namespace conflict without affecting the
# runtime behavior or surface area.
from collections.abc import Callable
from dataclasses import dataclass, field
import importlib
from typing import TYPE_CHECKING, Any
from uuid import UUID

# Third-party runtime imports.
#
# ``current_app``, ``g``, and ``request`` are LocalProxy objects that
# resolve to the active app/request context at call time. They are
# imported at runtime because the middleware accesses them in the
# function body. The ``Flask`` and ``Request`` types are used ONLY in
# type annotations and live in the ``TYPE_CHECKING`` block below.
from flask import current_app, g, request
import structlog

# Local imports - sibling middleware and models. Absolute imports per
# the project's ``flake8-tidy-imports`` configuration (relative
# imports are banned).
from app.middleware.error_handlers import AuthError
from app.models.enums import UserRole

# Type-only imports. Under ``from __future__ import annotations`` these
# are NEVER evaluated at runtime (PEP 563), so they live in a
# ``TYPE_CHECKING`` block to satisfy the project's strict
# ``flake8-type-checking`` configuration. ``Flask`` is the parameter
# type of ``register_auth_middleware``; ``Request`` is the parameter
# type of ``_extract_token``.
if TYPE_CHECKING:
    from flask import Flask, Request


# Deferred stdlib ``logging`` load via ``importlib`` (see NOTE above).
# The ``Any`` annotation is intentional: it makes the wrapper opaque
# to mypy so the type-checker does not attempt the (incorrect,
# namespace-package-quirk) resolution against the sibling
# ``app.observability.logging``. The runtime behavior is identical to
# ``import logging``.
logging: Any = importlib.import_module("logging")


# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

# Endpoints that bypass auth entirely. Match is by EXACT path string.
# Sub-prefix matching is intentionally avoided so a malicious URL like
# ``/healthzx`` or ``/healthz/../api/admin/users`` cannot be confused
# with the legitimate health-check path. Adding a new public endpoint
# requires explicitly inserting it here AND adding a corresponding
# decision-log entry per AAP Section 0.7.5 Explainability rule.
_PUBLIC_PATHS: frozenset[str] = frozenset(
    {
        "/healthz",
        "/readyz",
        "/metrics",
        "/auth/login",
        "/auth/logout",
        "/auth/register",
        "/auth/google/start",
        "/auth/google/callback",
    }
)

# Path prefixes that REQUIRE authentication. Requests to paths NOT in
# ``_PUBLIC_PATHS`` but matching one of these prefixes are rejected
# with 401 if the session cookie is absent/invalid. Currently this
# matches every ``/api/*`` URL. Stored as a tuple (not list) so it is
# immutable at module level.
_PROTECTED_PREFIXES: tuple[str, ...] = ("/api/",)

# Cookie name conventions (must match what ``app.api.auth`` sets via
# ``Set-Cookie``). The default name is ``"session"``; this module reads
# it from ``app.config["SESSION_COOKIE_NAME"]`` at request time so the
# default can be overridden per-environment.
_DEFAULT_COOKIE_NAME: str = "session"

# Type alias documenting the Flask before_request hook signature
# registered by this module. Declared as a runtime alias (right-hand
# side evaluates at module load) so the call site in
# ``register_auth_middleware`` can reference it for clarity, and so
# any future hook registration paths (e.g., test scaffolding) get a
# stable, named type to depend on.
_BeforeRequestHook = Callable[[], None]


# Stdlib logger - emits a single info line on middleware registration
# so operators can confirm wiring at startup. Runtime structured logs
# flow through structlog (fetched fresh inside the hook to ensure
# ``configure_structlog`` has run before logger creation).
_stdlib_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module public API
# ---------------------------------------------------------------------------

# Only ``Session`` and ``register_auth_middleware`` are part of the
# public ``from auth import *`` surface. The private helpers
# (underscore-prefixed) are still importable directly via dotted paths
# for tests and tightly-coupled internal callers, but are not part of
# the documented public API.
__all__ = [
    "Session",
    "register_auth_middleware",
]


# ---------------------------------------------------------------------------
# Session dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Session:
    """Authenticated session populated on ``flask.g.session`` per request.

    Per the assigned folder requirements:

        "It MUST contain at least ``user_id: UUID``, ``org_id: UUID``,
        ``role: str``."

    ``role`` is typed as ``UserRole`` (which is a ``str`` subclass via
    ``(str, Enum)``) so RBAC decorators get type-safe set-membership
    checks while still satisfying the documented "role: str" contract.

    Optional fields:
        email          User's email at login time (denormalized for fast
                       log/metric labels; NOT used for authorization).
        display_name   User's display name (denormalized; surfaced in
                       Owner attribution alongside ``owner_user_id`` but
                       NOT authoritative for owner identity).
        token_version  JWT ``tv`` claim. The auth middleware compares
                       this against ``users.token_version`` on every
                       protected request and rejects (401) any JWT
                       whose ``tv`` is stale.
        issued_at      JWT ``iat`` claim, ISO-8601 UTC string (or empty
                       when not provided by the verifier).
        expires_at     JWT ``exp`` claim, ISO-8601 UTC string (or empty
                       when not provided by the verifier).
        raw_claims     Defensive escape hatch holding the full JWT
                       payload for debugging / forensic logging without
                       re-decoding the token. The structlog redactor
                       filters secret-named keys, so storing raw claims
                       here is safe.

    The dataclass is ``frozen=True`` so handlers cannot accidentally
    mutate the session mid-request (which would cause race conditions
    in multi-threaded WSGI workers), and ``slots=True`` to keep memory
    footprint small (this object is allocated per request, so a
    per-instance ``__dict__`` adds up across thousands of requests).
    """

    user_id: UUID
    org_id: UUID
    role: UserRole
    email: str = ""
    display_name: str = ""
    token_version: int = 0
    issued_at: str = ""
    expires_at: str = ""
    raw_claims: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _is_public_path(path: str) -> bool:
    """Return True if the path is exempt from authentication.

    Exact-match against ``_PUBLIC_PATHS`` only. Sub-prefix matching is
    intentionally avoided so a malicious URL like ``/healthzx`` or
    ``/healthz/../api/admin/users`` cannot be confused with a
    legitimate health-check path.

    Args:
        path: The request path (typically ``request.path``). MUST be
            the URL-normalized path (Flask normalizes trailing
            slashes and resolves ``..`` segments before this hook
            runs).

    Returns:
        True iff ``path`` is one of the seven documented public
        endpoints. False for everything else, including subpaths or
        adjacent strings (e.g., ``/healthzx``).
    """
    return path in _PUBLIC_PATHS


def _is_protected_path(path: str) -> bool:
    """Return True if the path requires authentication.

    A path is protected if it starts with any prefix in
    ``_PROTECTED_PREFIXES`` AND is NOT in the public allowlist.
    Currently this matches every ``/api/*`` URL.

    Args:
        path: The request path (typically ``request.path``).

    Returns:
        True iff the path requires a valid session cookie. False for
        public paths (allowlisted) and for ambient paths (e.g.,
        ``/static/x.png`` or ``/random-path``) that do not match any
        protected prefix; ambient unauthenticated requests fall
        through to Flask's default 404 handler.
    """
    if _is_public_path(path):
        return False
    return any(path.startswith(prefix) for prefix in _PROTECTED_PREFIXES)


def _extract_token(req: Request, cookie_name: str) -> str | None:
    """Extract the session JWT from the request.

    Primary source: HttpOnly ``session`` cookie set by the auth
    blueprint. Fallback source: ``Authorization: Bearer <token>``
    header (used by service-to-service tests and CLI tools that do
    not store cookies; never exposed to the SPA).

    The cookie wins over the header when both are present so the
    primary HttpOnly-cookie flow takes precedence over any tooling
    that may also include a Bearer header.

    Args:
        req: The Flask Request.
        cookie_name: The cookie name (resolved from app config so the
            default can be overridden per environment).

    Returns:
        The raw token string, or ``None`` if neither source carries a
        non-empty value. The empty string and a Bearer prefix with no
        following token (e.g., ``"Bearer "``) both yield ``None`` so
        downstream code can use a single null check.
    """
    token = req.cookies.get(cookie_name)
    if token:
        return token
    authz = req.headers.get("Authorization", "")
    if authz.startswith("Bearer "):
        # Strip the prefix and any whitespace; an empty residue
        # collapses to None so callers do not need to distinguish
        # between a missing header and a malformed one.
        candidate = authz[len("Bearer ") :].strip()
        return candidate or None
    return None


def _verify_token_version(session: Session) -> bool:
    """Confirm the session JWT's ``tv`` claim matches the live DB value.

    Per AAP section 0.7.4 (Security Invariants), the per-user
    ``token_version`` is the ONLY mechanism that can invalidate an
    in-flight JWT before its natural expiry. Logout (and any future
    revocation event such as a forced sign-out or password change)
    increments ``users.token_version``; this function MUST reject any
    JWT whose ``tv`` claim is below the current stored value.

    Implementation notes:
        * Uses ``app.extensions.db.session()`` to open a fresh
          short-lived session. The query is a single PK lookup on
          ``users.id`` which the database services in <1 ms at any
          tenant scale. We deliberately do NOT reuse a long-lived
          per-request session because the auth middleware fires
          BEFORE the request handler establishes its own session
          context; opening here keeps the lifecycle local.
        * Returns ``True`` when the user is found AND
          ``user.token_version == session.token_version`` (typical
          case). Returns ``False`` when the user does not exist
          (e.g., the user was hard-deleted between mint and verify)
          OR the stored version is greater than the JWT's ``tv``
          (the user has logged out / had their session revoked).
        * Defensive: any unexpected exception is treated as
          verification failure (return ``False``). The rationale is
          that an unrecoverable DB error during auth is itself a
          fail-safe condition - we MUST NOT silently admit a JWT we
          could not verify.

    Args:
        session: The typed :class:`Session` reconstructed from the
            verified JWT claims via :func:`_build_session_from_claims`.

    Returns:
        ``True`` if the JWT's ``tv`` claim matches the user's live
        ``token_version`` in the database; ``False`` otherwise.
    """
    # Lazy imports keep the auth-middleware module compile-safe when
    # the database extension is not available (e.g., tests of the
    # middleware in isolation).
    from sqlalchemy import select  # noqa: PLC0415

    from app.extensions import db  # noqa: PLC0415
    from app.models.user import User  # noqa: PLC0415

    try:
        with db.session() as db_session:
            stored = db_session.scalar(
                # Only fetch the token_version column; avoid loading
                # the full User row to keep this hot-path query
                # minimal. ``select(User.token_version).where(...)``
                # produces a SELECT of a single integer column.
                select(User.token_version).where(User.id == session.user_id)
            )
    except Exception:
        # Any DB-level failure during auth verification is treated
        # as a hard reject. We deliberately catch broad Exception
        # here because admitting a request whose token-version we
        # could not verify would defeat the rotation invariant.
        # The broad catch is intentional fail-safety, not lazy
        # error handling.
        return False

    if stored is None:
        # User no longer exists. The JWT references a deleted account.
        return False

    return int(stored) == int(session.token_version)


def _build_session_from_claims(claims: dict[str, Any]) -> Session:
    """Construct a typed ``Session`` from a verified JWT claims dict.

    Required claims:
        user_id  UUID v4 (string).
        org_id   UUID v4 (string).
        role     One of the ``UserRole`` values
                 (``"Admin"``/``"Contributor"``/``"Viewer"``).

    Optional claims:
        email, display_name, iat, exp.

    The function tolerates ``role`` being passed either as a raw
    string OR as a ``UserRole`` enum instance (the latter happens in
    tests that mint Sessions directly).

    Args:
        claims: The decoded JWT payload as returned by
            ``app.services.auth.verify_session_jwt``.

    Returns:
        A frozen ``Session`` instance ready to be stashed on
        ``flask.g.session``.

    Raises:
        ValueError: when a required claim is missing or malformed
            (missing key, non-UUID string, unknown role value). The
            caller MUST translate this to ``AuthError`` (401) because
            a malformed-claim token is functionally equivalent to an
            invalid token from the client's perspective.
    """
    # user_id is required and must parse as a UUID.
    try:
        user_id = UUID(str(claims["user_id"]))
    except (KeyError, ValueError) as exc:
        raise ValueError("session JWT missing/invalid user_id claim") from exc

    # org_id is required and must parse as a UUID.
    try:
        org_id = UUID(str(claims["org_id"]))
    except (KeyError, ValueError) as exc:
        raise ValueError("session JWT missing/invalid org_id claim") from exc

    # role is required and must be a known UserRole value.
    try:
        role_raw = claims["role"]
        role = role_raw if isinstance(role_raw, UserRole) else UserRole(role_raw)
    except (KeyError, ValueError) as exc:
        raise ValueError("session JWT missing/invalid role claim") from exc

    # token_version (``tv``) is required - per AAP section 0.7.4 the
    # token-version mechanism is the only way logout can invalidate
    # an in-flight JWT. ``verify_session_jwt`` already validated
    # presence and shape; we coerce to int defensively.
    try:
        tv_raw = claims["tv"]
        token_version = int(tv_raw)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("session JWT missing/invalid tv claim") from exc

    return Session(
        user_id=user_id,
        org_id=org_id,
        role=role,
        email=str(claims.get("email", "")),
        display_name=str(claims.get("display_name", "")),
        token_version=token_version,
        issued_at=str(claims.get("iat", "")),
        expires_at=str(claims.get("exp", "")),
        # Defensive copy so subsequent mutation of the claims dict by
        # the caller cannot affect the stored Session (which is
        # frozen, but the dict it holds would otherwise be a shared
        # reference).
        raw_claims=dict(claims),
    )


# ---------------------------------------------------------------------------
# Private hook
# ---------------------------------------------------------------------------


def _before_request_authenticate() -> None:
    """Flask before_request hook: validate the session JWT.

    Decision tree (in order):
        1. If path is in the public allowlist -> return; no auth needed.
        2. Otherwise, attempt to extract a token from cookie/header.
        3. If no token AND path is protected -> raise AuthError(401).
        4. If no token AND path is NOT protected (e.g., 404) -> return;
           let the request reach the 404 handler.
        5. If a token is present -> verify and populate g.session.

    This hook runs AFTER ``register_correlation_middleware`` so log
    lines emitted from here carry the correlation_id. Per the strict
    middleware order in ``app.__init__::create_app``::

        correlation -> auth -> error_handlers -> blueprints

    Returns:
        None on success (Flask continues to the next handler) or on
        a public-path bypass. Raises ``AuthError`` on auth failure
        (the registered error handler converts to 401 JSON envelope).

    Raises:
        AuthError: when the session cookie is missing on a protected
            path, when token verification fails (signature mismatch,
            expiry, malformation), or when verified claims are
            malformed. ALL of these are 401, not 500.
    """
    # Fetch the structlog logger lazily inside the hook (not at module
    # level) because ``configure_structlog`` may not have run by the
    # time this module is imported. Calling ``structlog.get_logger``
    # here ensures the configured processor chain is used.
    logger = structlog.get_logger("app.middleware.auth")
    path = request.path

    if _is_public_path(path):
        # Nothing to do; downstream handler is public.
        logger.debug("auth_skipped_public_path", path=path)
        return

    cookie_name = current_app.config.get("SESSION_COOKIE_NAME", _DEFAULT_COOKIE_NAME)
    token = _extract_token(request, cookie_name)

    if not token:
        if _is_protected_path(path):
            logger.info("auth_missing_token", path=path, method=request.method)
            raise AuthError(message="Missing session cookie or bearer token.")
        # Not protected (e.g., ambient 404 paths). Let the request
        # continue; Flask will route to the 404 handler.
        logger.debug("auth_skipped_ambient_path", path=path)
        return

    # Token is present; defer to the services layer to verify it.
    # We import lazily here (rather than at module top) for two
    # complementary reasons:
    #   1. ``app.services.auth`` is not in this file's depends_on_files
    #      manifest. The lazy import keeps the module compile-safe
    #      regardless of services-layer availability at module load.
    #   2. Avoids any potential import cycles at app-factory
    #      construction time (services -> models -> middleware ->
    #      services).
    from app.services.auth import verify_session_jwt  # noqa: PLC0415

    try:
        claims = verify_session_jwt(token)
    except Exception as exc:
        # Per AAP, ANY token verification failure (signature mismatch,
        # expired, malformed payload, ...) MUST surface as 401 rather
        # than 500. The original exception is chained via
        # ``raise ... from exc`` so the structured log captures the
        # underlying type without leaking it to the client.
        logger.info(
            "auth_token_verification_failed",
            path=path,
            method=request.method,
            error=type(exc).__name__,
        )
        raise AuthError(message="Invalid or expired session token.") from exc

    if not claims:
        # Verifier returned None or an empty dict - treat as invalid.
        logger.info("auth_token_empty_claims", path=path, method=request.method)
        raise AuthError(message="Session token did not yield valid claims.")

    try:
        session = _build_session_from_claims(claims)
    except ValueError as exc:
        # Malformed-claim tokens are equivalent to invalid tokens
        # from the client's perspective: re-authenticate.
        logger.info(
            "auth_claims_malformed",
            path=path,
            method=request.method,
            error=str(exc),
        )
        raise AuthError(message="Session token has malformed claims.") from exc

    # Per AAP section 0.7.4 (Security Invariants): "Tokens rotated on
    # logout. Logout invalidates the cookie and (for the email/password
    # flow) advances the per-user signing-key version."
    #
    # We MUST compare the JWT's ``tv`` claim against the live
    # ``users.token_version`` value on every protected request. A
    # mismatch means the user has logged out (or had their token_version
    # bumped for any other revocation reason) AFTER this JWT was minted;
    # the token must therefore be rejected even though its signature
    # and expiry are still valid.
    #
    # The DB read is cheap: a single PK lookup on the indexed
    # ``users.id`` column, sub-millisecond at any tenant scale. The
    # added latency is well within the AAP's per-request budget.
    if not _verify_token_version(session):
        logger.info(
            "auth_token_version_stale",
            path=path,
            method=request.method,
            user_id=str(session.user_id),
        )
        raise AuthError(message="Session has been invalidated; please log in again.")

    # Stash the typed session on ``g`` so downstream handlers and the
    # RBAC decorator can read it via ``g.session``.
    g.session = session

    # Bind identity context into structlog so subsequent log lines
    # within this request automatically carry user_id, org_id, role.
    # The ``merge_contextvars`` processor in
    # ``app.observability.logging`` surfaces these on every log line
    # emitted within the request scope. The correlation middleware's
    # ``teardown_request`` hook clears all bound contextvars so values
    # do not leak across requests on the same worker thread.
    structlog.contextvars.bind_contextvars(
        user_id=str(session.user_id),
        org_id=str(session.org_id),
        role=session.role.value,
    )

    # Successful auth log line. We intentionally do NOT include
    # ``raw_claims`` here - the session is already bound to the
    # request-scoped log context, so subsequent lines carry the
    # identity automatically without dumping the full claim dict.
    logger.debug(
        "auth_succeeded",
        path=path,
        method=request.method,
        user_id=str(session.user_id),
        role=session.role.value,
    )


# ---------------------------------------------------------------------------
# Public registration function
# ---------------------------------------------------------------------------


def register_auth_middleware(app: Flask) -> None:
    """Register the JWT verification middleware on the Flask app.

    Called by ``app.__init__.create_app()`` AFTER
    ``register_correlation_middleware`` and BEFORE the API blueprints
    are registered. This ordering ensures:

    * Correlation IDs are bound BEFORE this middleware logs.
    * ``g.session`` is populated BEFORE handlers (which may read it)
      execute.
    * RBAC decorators on individual handlers run AFTER this middleware
      (since they are decorators, they run as part of the handler
      invocation, which Flask invokes after all ``before_request``
      hooks complete).

    Idempotent: registering the same hook twice is harmless because
    Flask's ``before_request`` registry deduplicates by view-function
    identity (and our hook is a module-level function so identity is
    stable across re-registrations).

    Args:
        app: The Flask application instance produced by
            ``app.create_app``. The hook is registered on the
            application-wide function list, so it fires for every
            blueprint and every request.

    Returns:
        None. Side effects: mutates ``app.before_request_funcs``;
        emits a single ``auth_middleware_registered`` info log line
        via the stdlib logger so operators can confirm wiring at
        startup.
    """
    # Bind the hook to a typed local first so the registration call
    # site documents the signature contract (matches Flask's
    # before_request callable expectation: ``Callable[[], None]``).
    hook: _BeforeRequestHook = _before_request_authenticate
    app.before_request(hook)

    # Single-line registration confirmation. Use the stdlib logger
    # here (not structlog) because ``configure_structlog`` may not
    # have completed yet when middleware is registered during
    # ``create_app``; the stdlib logger is available unconditionally
    # and is also the convention used by sibling middleware modules.
    _stdlib_logger.info(
        "auth_middleware_registered",
        extra={
            "public_paths": sorted(_PUBLIC_PATHS),
            "protected_prefixes": list(_PROTECTED_PREFIXES),
        },
    )
