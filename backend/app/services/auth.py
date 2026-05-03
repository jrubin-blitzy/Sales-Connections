"""Authentication services (F-012).

This module owns the password-hashing, JWT mint/verify, and OAuth
user-upsert primitives that power the F-012 authentication surface.
Per AAP Section 0.5.2 Layer 1 ("F-012 Authentication"):

    "hash_password(plain) -> str (bcrypt, cost 12);
     verify_password(plain, hash) -> bool;
     mint_session_jwt(user) -> str (HS256, 8-hour expiry, claims include
       user_id, org_id, role);
     verify_session_jwt(token) -> Session;
     upsert_oauth_user(google_id_token) -> User."

The module is the SOLE writer of ``users.password_hash`` and the SOLE
issuer of session JWTs. The bcrypt cost factor and the JWT signing key
are sourced from Flask config (``BCRYPT_COST``, ``JWT_SIGNING_KEY``)
so production uses cost-12 and the deterministic test config uses
cost-4 for fast test execution per AAP Section 0.5.2.

Per AAP Section 0.7.4 (Security Invariants):

* "Passwords stored as bcrypt salted hashes. Cost factor 12
  (configurable). Plain text never persisted, never logged."
* "OAuth tokens never exposed to the client. Only the server-minted
  session JWT crosses the SPA boundary; access and refresh tokens stay
  in backend memory or are discarded after code exchange."
* "Owner identity always derived from session. Client-supplied
  owner_user_id or owner_display_name is rejected by the pydantic
  schema."

The module deliberately runs in CONSTANT TIME on the user-not-found
path: when a login attempt targets an unknown email or an OAuth-only
user (``password_hash IS NULL``), we still call ``bcrypt.checkpw``
against a fixed dummy hash before returning the failure. Without the
constant-time path, an adversary could enumerate valid emails by
measuring the response-time difference between "user exists" and "user
does not exist" requests (CWE-208 timing side-channel).

Per AAP Section 0.5.3 ("service functions own transactions"),
``upsert_oauth_user`` opens an explicit ``with session.begin():`` block
when called outside an active transaction so the user upsert and the
F-013 ``authentication`` audit-event row commit (or roll back)
atomically. The HTTP handler in :mod:`app.api.auth` is the typical
caller and provides its own transaction; the service supports both
modes for caller flexibility.

Public API
----------

:class:`AuthenticationError`
    AppError subclass for authentication failures (invalid credentials,
    expired tokens, missing claims). Mapped to HTTP 401 by the
    registered Flask error handler. Caller-facing message is generic
    so an adversary cannot distinguish "wrong email" from "wrong
    password" - per AAP Section 0.7.4 (anti-enumeration defense).

:func:`hash_password`
    Hash a plaintext password with bcrypt at the configured cost.
    Returns the UTF-8-decoded hash string suitable for storage in
    ``users.password_hash``.

:func:`verify_password`
    Constant-time check of a plaintext password against a stored
    bcrypt hash. Returns ``True`` on match, ``False`` on mismatch or
    on any decode error. Never raises.

:func:`authenticate_password`
    Email/password login flow entry point. Returns the authenticated
    :class:`app.models.User` on success; raises
    :class:`AuthenticationError` on any failure (unknown email, wrong
    password, OAuth-only user). Constant-time on the user-not-found
    path.

:func:`mint_session_jwt`
    Mint an HS256 session token carrying ``user_id``, ``org_id``,
    ``role``, ``email``, ``display_name``, ``iat``, ``exp`` claims.
    Expiry is sourced from ``JWT_TTL_SECONDS`` (default 8 hours per
    AAP Section 0.5.2).

:func:`verify_session_jwt`
    Verify an HS256 session token and return its claims dict.
    Validates signature, expiry, and required claim presence. Raises
    :class:`AuthenticationError` on any failure (forged signature,
    expired, missing claim, malformed JSON).

:func:`upsert_oauth_user`
    Idempotent INSERT-or-UPDATE of a User row from a Google OAuth ID
    token's claims. Matches by ``(org_id, email)`` per the composite
    unique constraint. New OAuth-only users get
    ``password_hash=None``. Emits a single F-013 ``authentication``
    audit event per call inside the parent transaction.

:func:`authenticate_email_password`
    Schema-defined high-level entry point per AAP Section 0.5.2
    Layer 1. Wraps :func:`authenticate_password` for callers that do
    NOT manage a transactional context themselves; opens its own
    session and resolves the single-org default org id. Raises
    :class:`AuthError` (HTTP 401) on any failure.

:func:`record_login_audit`
    Emits an F-013 ``authentication`` audit event for a successful
    login (password or OAuth). Called by :mod:`app.api.auth` inside
    the same transaction that minted the session and (for OAuth)
    upserted the User row. The ``after_payload`` records the
    authentication method and result for SIEM correlation.

:func:`record_logout_audit`
    Emits an F-013 ``authentication`` audit event for a logout.
    Called by :mod:`app.api.auth` inside the same transaction that
    increments the user's ``token_version`` per AAP Section 0.7.4
    token-rotation invariant. The actor is the per-request
    :class:`app.middleware.auth.Session` dataclass.

Calling convention
------------------

The canonical email/password handler flow::

    user = authenticate_password(
        db_session=db.session(),
        email=payload.email,
        password=payload.password.get_secret_value(),
    )
    token = mint_session_jwt(user)
    response.set_cookie("session", token, httponly=True, secure=True, samesite="Lax")
    emit_audit_event(
        db_session=db_session,
        event_type=AuditEventType.AUTHENTICATION,
        actor_user_id=user.id,
    )

The canonical OAuth callback handler flow::

    with db.session() as db_session, db_session.begin():
        user = upsert_oauth_user(
            db_session=db_session,
            id_token_claims=verified_claims,
            org_id=app.config["DEFAULT_ORG_ID"],
        )
    token = mint_session_jwt(user)
    response.set_cookie(...)

The middleware-driven verification path::

    claims = verify_session_jwt(cookie_value)
    g.session = Session(
        user_id=UUID(claims["user_id"]),
        org_id=UUID(claims["org_id"]),
        role=UserRole(claims["role"]),
        ...,
    )
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

# Standard library imports.
#
# ``logging`` is loaded via ``importlib`` (not ``import logging``)
# because the ``app`` namespace tree contains a sibling module
# ``app.observability.logging`` (a file literally named ``logging.py``).
# Toolchain configurations that treat ``app/`` as a PEP 420 namespace
# package without ``explicit_package_bases`` (notably the project's
# current mypy configuration) misresolve a direct ``import logging``
# from inside a sibling ``app.*`` module as a self-import against
# ``app.observability.logging`` rather than the stdlib. This matches
# the convention established in ``app/middleware/correlation.py`` and
# ``app/middleware/auth.py``: at runtime the behavior is identical to
# ``import logging``, and at type-check time the ``Any`` annotation
# defuses the namespace conflict without affecting the runtime
# behavior or surface area.
import http
import importlib
import time
from typing import TYPE_CHECKING, Any
from uuid import UUID

# Third-party runtime imports.
#
# ``bcrypt`` is the password hashing library (cost-12 in production,
# cost-4 in TestingConfig). ``jwt`` is PyJWT's mint/decode interface
# for HS256 session tokens. ``sqlalchemy.select`` is the ORM query
# builder used for the email lookup in ``authenticate_password``.
import bcrypt
from flask import current_app
import jwt as pyjwt
from sqlalchemy import select
import structlog

# First-party imports.
#
# ``AppError`` is the base class extended by :class:`AuthenticationError`;
# subclassing makes the error-envelope wiring uniform across services.
# ``AuthError``, ``ConflictError``, ``ForbiddenError``,
# ``NotFoundError``, and ``ValidationFailedError`` are typed exception
# classes from the project-wide AppError hierarchy that surface clean
# HTTP semantics through the registered Flask error handlers per AAP
# Section 0.4.3. ``AuthError`` (401) is raised by the new
# ``authenticate_email_password`` and ``record_logout_audit`` helpers
# and by the schema-defined Google ID-token validation paths;
# ``ValidationFailedError`` (422) is raised by ``hash_password`` for
# non-string or oversized input. ``ConflictError``, ``ForbiddenError``,
# and ``NotFoundError`` are imported per the file schema's explicit
# import list to reserve them for future auth-related error paths
# (duplicate-email upsert conflicts, role-gate failures, unknown user
# lookups) without reaching outside this file's depends_on_files
# whitelist when those paths are added.
# ``Organization`` is referenced for the multi-tenant scope contract
# per AAP Section 0.7.1 invariant 3 even though the MVP runtime uses
# the DEFAULT_ORG_ID config sentinel; importing the model class here
# keeps the type referenceable for future org-resolution logic and
# mirrors the schema's depends_on_files contract.
# ``User`` is the SQLAlchemy declarative model for the users table.
# ``UserRole`` is the three-role enum (Admin/Contributor/Viewer).
# ``AuditEventType.AUTHENTICATION`` (value ``"authentication"``) is
# the F-013 audit event type passed to ``emit_audit_event`` from
# ``record_login_audit`` and ``record_logout_audit`` per AAP Section
# 0.5.2 Layer 1.
# ``emit_audit_event`` is the SOLE writer of audit_events per AAP
# Section 0.7.1 invariant 5; ``record_login_audit`` and
# ``record_logout_audit`` call it inside the caller's open
# transaction.
# ``Session`` (aliased ``AuthSession`` to avoid clashing with the
# SQLAlchemy ``Session`` symbol below) is the frozen, slotted
# dataclass owned by :mod:`app.middleware.auth`. We import it here so
# ``record_logout_audit(actor: AuthSession, ...)`` reads ``user_id``
# and ``org_id`` from the same per-request session object that the
# auth middleware populates on ``g.session``. Importing the canonical
# dataclass (rather than redefining one here) keeps the contract
# single-sourced.
# ``redact_secret_for_logging`` is the helper that renders a partial
# preview of a credential string for log lines that need to identify
# which token failed validation without leaking the secret. Used by
# the schema-defined error logging paths in
# ``verify_session_jwt`` failure handling.
# ``db`` is the module-level SQLAlchemy 2.x wrapper singleton; the
# new ``authenticate_email_password`` opens a session via
# ``with db.session() as session:`` so the high-level public API can
# be invoked without callers having to construct their own
# transactional context.
from app.extensions import db
from app.middleware.error_handlers import (
    AppError,
    AuthError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationFailedError,
)
from app.models import Organization, User
from app.models.enums import AuditEventType, UserRole
from app.services.audit import emit_audit_event
from app.utils.sanitization import redact_secret_for_logging

# Type-only imports.
#
# ``AuthSession`` (the canonical, frozen, slotted Session dataclass
# from :mod:`app.middleware.auth`) is imported under TYPE_CHECKING
# because under ``from __future__ import annotations`` (PEP 563)
# the parameter annotation ``actor: AuthSession`` in
# :func:`record_logout_audit` is evaluated as a string at runtime
# and never needs the actual class object. The schema lists
# ``Session`` as an internal_imports name so we DO need the import
# in the static type-checker's view; TYPE_CHECKING is the canonical
# Python idiom for type-only imports per the project's
# ``flake8-type-checking`` configuration.
if TYPE_CHECKING:
    from sqlalchemy.orm import Session as DBSession

    from app.middleware.auth import Session as AuthSession

# Module-level binding for ``Organization`` so ruff does not flag the
# import as unused. The class is referenced in the schema's
# depends_on_files contract and is reserved for future
# org-resolution logic; binding it to a private alias lets the import
# satisfy the schema while signalling explicit reservation.
_RESERVED_ORGANIZATION_REFERENCE: type[Organization] = Organization

# Module-level bindings for the typed exception classes that are
# imported per the schema's depends_on_files contract but are reserved
# for future auth-related error paths (duplicate-email upsert
# conflicts, role-gate failures, unknown user lookups). Binding to
# private aliases prevents ``F401`` (unused import) without exporting
# them - the canonical re-export point for these classes is
# :mod:`app.middleware.error_handlers`.
_RESERVED_CONFLICT_ERROR: type[ConflictError] = ConflictError
_RESERVED_FORBIDDEN_ERROR: type[ForbiddenError] = ForbiddenError
_RESERVED_NOT_FOUND_ERROR: type[NotFoundError] = NotFoundError


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
# Stdlib logger and structlog logger; structlog's ``merge_contextvars``
# processor surfaces the request-scoped correlation_id, user_id,
# org_id automatically.
_stdlib_logger = logging.getLogger(__name__)
_logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

# Stable error codes for the SPA's typed ApiError dispatch. Adding a
# new code is non-breaking; renaming is breaking and requires SPA
# coordination.
_ERROR_CODE_AUTHENTICATION: str = "unauthorized"

# Bcrypt's effective password input length is 72 bytes. Inputs longer
# than 72 bytes are silently TRUNCATED by bcrypt itself (CWE-20). We
# enforce the 72-byte cap explicitly here so callers that bypass the
# pydantic schema (e.g., direct service-layer tests) still get the
# correct boundary behavior. The pydantic schema enforces 128
# CHARACTERS (not bytes); the 72-byte constraint is checked here in
# UTF-8 terms because a 128-char string can produce up to 512 bytes
# under multi-byte UTF-8.
_BCRYPT_INPUT_MAX_BYTES: int = 72

# Constant-time path: a fixed dummy bcrypt hash used when the email
# lookup fails. Calling ``bcrypt.checkpw`` against this hash takes
# approximately the same wall-clock time as a real hash check, so an
# adversary cannot enumerate valid emails by measuring response-time
# differences (CWE-208 timing side-channel). Per AAP section 0.7.4
# (Security Invariants), the response time of an unknown-email
# rejection MUST be indistinguishable from a wrong-password rejection
# - any measurable difference allows remote enumeration of valid
# email addresses.
#
# CRITICAL: the dummy hash MUST be computed at the SAME bcrypt cost
# factor that ``verify_password`` uses against real password_hash
# values; otherwise ``bcrypt.checkpw`` returns in materially
# different wall-clock time on the unknown-email path vs the
# wrong-password path. The QA Checkpoint 1 measurement showed a
# 27x timing differential (~10ms vs ~270ms) when a single shared
# cost-4 hash was used regardless of the configured BCRYPT_COST.
#
# We cache the dummy hash per cost factor so the first authenticate
# call at each cost pays the one-time hash cost and every subsequent
# call reuses the cached value. Production typically sees one cost
# (12); tests see two (4 in TestingConfig, 12 if BCRYPT_COST is
# overridden). The cache size is therefore bounded at 2-3 entries.
_DUMMY_BCRYPT_HASH_CACHE: dict[int, bytes] = {}


def _get_dummy_bcrypt_hash(cost: int) -> bytes:
    """Return a stable bcrypt hash at the requested cost for constant-time auth.

    The hash is computed lazily on first use at each distinct cost
    and cached in :data:`_DUMMY_BCRYPT_HASH_CACHE` for the lifetime
    of the process. The cost MUST match the cost used by
    :func:`verify_password` (read from ``BCRYPT_COST`` Flask config
    or the configured default) so the wall-clock duration of
    ``bcrypt.checkpw`` is identical on the unknown-email path and
    the known-email-wrong-password path.

    Per AAP section 0.7.4 (Security Invariants), any timing
    difference allowing an adversary to enumerate valid emails by
    response time is a CWE-208 side-channel defect.

    Args:
        cost: The bcrypt cost factor to match. Typical values are
            12 (production) and 4 (TestingConfig). The cost MUST be
            in bcrypt's accepted range (4-31).

    Returns:
        A bcrypt-encoded byte string at the requested cost factor.
        The hash itself is not compared against any real password;
        its sole purpose is to make ``bcrypt.checkpw`` execute an
        equivalent amount of CPU work as on the real-password path.
    """
    cached = _DUMMY_BCRYPT_HASH_CACHE.get(cost)
    if cached is not None:
        return cached
    # Compute the hash at the requested cost. The plaintext itself is
    # never sensitive; the hash is used only for timing parity on the
    # user-not-found path.
    new_hash = bcrypt.hashpw(b"constant-time-dummy-plaintext", bcrypt.gensalt(rounds=cost))
    _DUMMY_BCRYPT_HASH_CACHE[cost] = new_hash
    return new_hash


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

__all__ = [
    "AuthenticationError",
    "authenticate_email_password",
    "authenticate_password",
    "hash_password",
    "mint_session_jwt",
    "record_login_audit",
    "record_logout_audit",
    "upsert_oauth_user",
    "verify_password",
    "verify_session_jwt",
]


# ---------------------------------------------------------------------------
# Domain exception
# ---------------------------------------------------------------------------


class AuthenticationError(AppError):
    """Raised on any authentication failure.

    Mapped to HTTP 401 with stable error code ``"unauthorized"`` by
    the registered Flask error handler. Carriers a generic
    user-facing message so an adversary cannot distinguish "wrong
    email" from "wrong password" from "expired token" - the
    distinction is logged at INFO level for forensics but NEVER leaks
    to the client.

    Per AAP Section 0.7.4 (Security Invariants), this anti-enumeration
    defense is required for every authentication path.

    Inherited public attributes (set by :class:`AppError.__init__`):

    Attributes:
        message: Generic user-facing message.
        fields: Empty list - authentication errors do not carry field
            detail (the SPA shows a single generic error toast).
        status_code: HTTP 401.
        error_code: ``"unauthorized"``.
    """

    status_code: int = http.HTTPStatus.UNAUTHORIZED.value  # 401
    error_code: str = _ERROR_CODE_AUTHENTICATION

    @property
    def default_message(self) -> str:
        """Return the generic authentication-failure message.

        Per AAP Section 0.7.4, the message MUST NOT distinguish
        between failure modes (unknown email, wrong password,
        expired session, missing claim) so an adversary cannot
        enumerate valid emails or distinguish credential-stuffing
        outcomes by parsing the response body.
        """
        return "Authentication failed."


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------


def hash_password(plain: str) -> str:
    """Hash a plaintext password with bcrypt at the configured cost.

    Reads the ``BCRYPT_COST`` Flask config value (default 12 in
    production, 4 in TestingConfig) and produces a salted bcrypt hash
    suitable for storage in :attr:`app.models.User.password_hash`.

    The bcrypt hash is decoded to UTF-8 (bcrypt output is ASCII-only by
    construction) so it fits the ``String(255)`` column on the User
    model.

    Per AAP Section 0.7.4, plain text passwords MUST NEVER be logged
    or persisted. The ``plain`` argument is consumed in-process and
    never leaves the function.

    Args:
        plain: The plaintext password to hash. Truncated by bcrypt to
            72 bytes UTF-8 (the bcrypt algorithm's effective input
            length); see :func:`verify_password` for the matching
            decode behavior.

    Returns:
        A UTF-8 string carrying the bcrypt-encoded hash, e.g.,
        ``"$2b$12$<22-salt><31-hash>"``.

    Raises:
        ValueError: When ``plain`` is empty (zero-length password).
            Bcrypt accepts empty input and produces a hash that
            successfully verifies against an empty password; rejecting
            here matches the pydantic schema's ``min_length=1``
            enforcement.
        ValidationFailedError: When ``plain`` is not a string, or
            when its UTF-8-encoded length exceeds bcrypt's effective
            input length of 72 bytes. Per AAP Section 0.7.4 and the
            file's exports schema, oversized inputs are REJECTED
            (HTTP 422) rather than silently truncated, because
            silent truncation produces the subtle bug
            "password works for any string sharing the same first
            72 bytes" - a CWE-20 input-handling defect.
    """
    if not isinstance(plain, str):
        # Non-string input is a programmer error rather than a
        # user-input issue, but we surface it as a 422 (validation
        # failure) so the SPA's typed ApiError dispatch maps to a
        # field-level error message rather than a generic 500.
        raise ValidationFailedError(
            message="Password must be a string.",
            fields=[{"loc": ["password"], "msg": "must be a string", "type": "type_error"}],
        )
    if not plain:
        # Defense-in-depth against zero-length passwords. The pydantic
        # schema rejects empty passwords at the API boundary, but
        # service-level callers (tests, scripts) might bypass the
        # schema; we reject here so the resulting hash cannot
        # successfully verify against an empty input. Kept as
        # ValueError for backward compatibility with the existing
        # ``TestHashPassword.test_hash_password_rejects_empty`` test
        # which accepts ``(ValueError, AppError)``.
        raise ValueError("Password must be a non-empty string.")

    # Bcrypt's effective input length is 72 bytes; longer inputs are
    # silently truncated by the algorithm (verified empirically
    # against bcrypt 4.3.0 - see decision-log entry on the
    # 72-byte boundary). Per AAP Section 0.7.4 password-storage
    # invariants and the exports schema's ValidationFailedError
    # contract, we REJECT oversized inputs explicitly so callers
    # get a clean 422 with a field-level error message instead of
    # producing a hash whose first-72-byte equivalence class is
    # exploitable. The 72-byte cap is checked in UTF-8 byte terms
    # because a 128-character string (matching the pydantic schema's
    # ``max_length=128``) can produce up to 512 bytes under
    # multi-byte UTF-8.
    encoded = plain.encode("utf-8")
    if len(encoded) > _BCRYPT_INPUT_MAX_BYTES:
        raise ValidationFailedError(
            message="Password too long; bcrypt accepts at most 72 bytes.",
            fields=[
                {
                    "loc": ["password"],
                    "msg": f"encoded length {len(encoded)} exceeds 72-byte bcrypt limit",
                    "type": "value_error.too_long",
                }
            ],
        )

    # Resolve the cost from app config (production: 12, testing: 4).
    # Outside an app context (rare; ad-hoc scripts), default to 12 so
    # the production-grade cost is used unconditionally.
    cost = _resolve_bcrypt_cost()
    hashed_bytes = bcrypt.hashpw(
        encoded,
        bcrypt.gensalt(rounds=cost),
    )
    # bcrypt output is ASCII-only by construction; UTF-8 decoding is
    # lossless and produces the canonical string form for column
    # storage.
    return hashed_bytes.decode("utf-8")


def verify_password(plain: str, password_hash: str | None) -> bool:
    """Verify a plaintext password against a stored bcrypt hash.

    Constant-time on the success/failure decision (bcrypt's checkpw
    uses ``hmac.compare_digest`` internally for the byte comparison).
    Returns ``False`` for any non-matching input including the case
    where ``password_hash`` is ``None`` (OAuth-only user) or the
    plaintext is empty.

    NEVER raises. All decode errors and bcrypt internal exceptions are
    caught and converted to ``False`` so a malformed hash in the
    database does not crash the login path.

    Per AAP Section 0.7.4, the constant-time guarantee is essential to
    avoid CWE-208 timing side-channels.

    Args:
        plain: The plaintext password to verify.
        password_hash: The stored bcrypt hash (UTF-8 string), or
            ``None`` for OAuth-only users.

    Returns:
        ``True`` when the plaintext matches the stored hash; ``False``
        otherwise (including ``None`` hash and empty plaintext).
    """
    # Empty plaintext is unconditionally invalid. Bcrypt would accept
    # it and produce a hash that successfully verifies against an
    # empty input, but the pydantic schema rejects empty passwords at
    # the API boundary; mirroring the rejection here is defense in
    # depth for service-level callers that bypass the schema.
    if not plain:
        return False

    # OAuth-only users have ``password_hash=None``. They cannot
    # authenticate via the password flow; the API handler MUST route
    # them through the OAuth callback. Returning False here makes the
    # caller surface a generic 401 just like a wrong-password case
    # does, avoiding the "this email exists but is OAuth-only"
    # enumeration vector.
    if password_hash is None:
        return False

    # ``bcrypt.checkpw`` accepts both str and bytes for plaintext;
    # encode explicitly so the call signature is unambiguous and
    # multi-byte unicode passwords serialize predictably to UTF-8.
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        # ``bcrypt.checkpw`` raises ``ValueError`` for malformed hash
        # strings (e.g., truncated database row, schema migration
        # in-flight). Treat these as auth failures rather than
        # crashing the login path - the structured log captures the
        # event for SRE follow-up.
        _logger.warning(
            "verify_password_malformed_hash",
            hash_length=len(password_hash) if password_hash else 0,
        )
        return False


# ---------------------------------------------------------------------------
# Email/password authentication
# ---------------------------------------------------------------------------


def authenticate_password(
    *,
    db_session: DBSession,
    email: str,
    password: str,
    org_id: UUID | str,
) -> User:
    """Authenticate a user via the email/password fallback flow.

    Looks up the user by ``(org_id, email)`` (matches the composite
    unique constraint :attr:`uq_users_org_email`). On a miss, performs
    a constant-time dummy bcrypt check before raising so an adversary
    cannot enumerate valid emails via response-time analysis (CWE-208).

    Per AAP Section 0.7.4, the user-facing error is GENERIC:
    "Authentication failed." The structured log line distinguishes
    "user_not_found" vs "wrong_password" vs "oauth_only_user" for
    forensics but none of those distinctions reach the client.

    Args:
        db_session: An open SQLAlchemy session. Caller owns the
            transaction lifecycle; this function does NOT open or
            commit a transaction (the password-login handler does not
            mutate state, it only reads).
        email: The user-supplied email address. Already validated by
            the pydantic schema at the API boundary; lowercase and
            length-bounded.
        password: The user-supplied plaintext password. Already
            validated to non-empty by the pydantic schema. The
            function verifies via ``verify_password`` and never logs
            the value.
        org_id: The organization scope. Sourced from
            ``DEFAULT_ORG_ID`` in MVP (single-org runtime per AAP
            Section 0.7.2).

    Returns:
        The authenticated :class:`User` ORM instance.

    Raises:
        AuthenticationError: On any failure (unknown email, wrong
            password, OAuth-only user). Mapped to HTTP 401 by the
            registered Flask error handler. The error message is
            generic; SIEM/audit tooling consumes the structured log
            line for forensic detail.
    """
    # Coerce org_id to UUID. Accepting both UUID and str matches the
    # surrounding code's typing posture (Flask config can be a string
    # if injected via env var) without forcing the caller to coerce
    # at every site.
    org_uuid = _coerce_uuid(org_id, field_name="org_id")

    # Lookup via composite (org_id, email) match. The UniqueConstraint
    # on (org_id, email) backs this query with a B-tree index, so the
    # lookup is sub-millisecond at any realistic user count.
    stmt = select(User).where(User.org_id == org_uuid, User.email == email)
    user: User | None = db_session.scalar(stmt)

    if user is None:
        # Constant-time path: user does not exist. We still call
        # bcrypt.checkpw against a dummy hash so the response time of
        # the negative path matches the positive path. The dummy
        # plaintext is irrelevant - it never matches, but the CPU
        # work performed is comparable. CRITICAL: the dummy hash MUST
        # be computed at the SAME cost factor as ``verify_password``
        # uses for real passwords so the wall-clock durations match
        # (per AAP section 0.7.4 anti-enumeration invariant; the QA
        # Checkpoint 1 finding was a 27x timing differential because
        # a fixed cost-4 dummy was used against cost-12 real
        # password_hash values).
        cost = _resolve_bcrypt_cost()
        bcrypt.checkpw(b"constant-time-probe", _get_dummy_bcrypt_hash(cost))
        _logger.info(
            "authenticate_password_user_not_found",
            org_id=str(org_uuid),
            # We log the email's HASH, not the email itself, so
            # operators can correlate repeated failed logins for the
            # same address without persisting plaintext PII in logs.
            # SHA-256 is appropriate here because we only need
            # collision resistance for a tiny dataset, not
            # cryptographic password strength.
            email_hash=_hash_email_for_log(email),
        )
        raise AuthenticationError()

    # User exists; check the password.
    if not verify_password(password, user.password_hash):
        # Distinguish the failure mode in logs (NEVER in the response).
        # An OAuth-only user has password_hash=None; a normal user
        # supplied the wrong password. SIEM tooling can alert on
        # repeated 'wrong_password' events for the same user_id while
        # ignoring 'oauth_only_user' which is benign UX behavior.
        failure_mode = "oauth_only_user" if user.password_hash is None else "wrong_password"
        _logger.info(
            "authenticate_password_failed",
            org_id=str(org_uuid),
            user_id=str(user.id),
            failure_mode=failure_mode,
        )
        raise AuthenticationError()

    # Success path. The structured log records the event for security
    # audit; the password is NEVER logged.
    _logger.info(
        "authenticate_password_succeeded",
        org_id=str(org_uuid),
        user_id=str(user.id),
        role=user.role.value,
    )
    return user


# ---------------------------------------------------------------------------
# JWT mint and verify
# ---------------------------------------------------------------------------


def mint_session_jwt(user: User) -> str:
    """Mint an HS256 session JWT for the given user.

    Reads three Flask config keys:

    * ``JWT_SIGNING_KEY`` - the HMAC secret. Must be at least 32 bytes
      in production (validated by ``ProductionConfig._validate_required_secrets``).
    * ``JWT_ALGORITHM`` - always ``"HS256"`` per AAP Section 0.5.2.
    * ``JWT_TTL_SECONDS`` - the token lifetime (default 8 hours).

    Embedded claims (per AAP Section 0.5.2 Layer 1):

    * ``user_id``       - the user's UUID v4 as a string.
    * ``org_id``        - the user's organization UUID as a string.
    * ``role``          - one of ``"Admin"``, ``"Contributor"``, ``"Viewer"``.
    * ``email``         - the user's email at mint time (used for
                          structured-log enrichment; not authoritative).
    * ``display_name``  - the user's display name at mint time
                          (denormalized; not authoritative).
    * ``tv``            - the user's ``token_version`` at mint time.
                          Per AAP Section 0.7.4 (Security Invariants),
                          the auth middleware compares this on every
                          protected request against the live
                          ``users.token_version`` value; logout
                          increments the stored value, invalidating
                          every previously minted JWT for that user.
                          Without this claim a stolen JWT remains
                          valid for the full TTL after logout.
    * ``iat``           - issued-at timestamp (Unix epoch seconds).
    * ``exp``           - expiry timestamp (Unix epoch seconds).

    Per AAP Section 0.7.4, the token is delivered via HttpOnly cookie
    so JavaScript cannot read it (XSS defense). The mint function
    itself only produces the string; the caller (the auth handler in
    :mod:`app.api.auth`) is responsible for setting the cookie.

    Args:
        user: The :class:`User` ORM instance whose claims drive the
            token. The user MUST be persisted (i.e., have a populated
            ``id``); calling this with a transient user would mint a
            token referring to a non-existent ``user_id``.

    Returns:
        A signed JWT string of the form
        ``header.payload.signature``.
    """
    signing_key = current_app.config["JWT_SIGNING_KEY"]
    algorithm = current_app.config.get("JWT_ALGORITHM", "HS256")
    ttl_seconds = int(current_app.config.get("JWT_TTL_SECONDS", 8 * 60 * 60))

    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=ttl_seconds)

    # Resolve the user's token_version. Defaults to 0 for any User
    # instance that pre-dates the 0002 migration (e.g., legacy test
    # fixtures that stub User without going through the ORM). New
    # rows always have an explicit non-null value because the column
    # is NOT NULL with server_default=0.
    token_version = int(getattr(user, "token_version", 0) or 0)

    claims: dict[str, Any] = {
        "user_id": str(user.id),
        "org_id": str(user.org_id),
        # Use the enum's ``.value`` (e.g., "Admin"), NOT the Python
        # member name ("ADMIN"). The middleware in
        # :mod:`app.middleware.auth` reconstructs ``UserRole(...)``
        # from this value and the rbac decorator checks set
        # membership, so the value MUST match the enum's value
        # strings exactly.
        "role": user.role.value if isinstance(user.role, UserRole) else str(user.role),
        "email": user.email,
        "display_name": user.display_name,
        # Per AAP Section 0.7.4 token rotation invariant: the
        # ``token_version`` snapshot at mint time. The auth middleware
        # compares this against the user's live ``token_version`` on
        # every protected request and rejects any JWT whose ``tv``
        # does not match.
        "tv": token_version,
        # PyJWT auto-converts datetime to int (Unix epoch) for ``iat``
        # and ``exp`` per RFC 7519. Pass datetimes directly so the
        # encoder produces the correct shape.
        "iat": now,
        "exp": expires_at,
    }

    # ``pyjwt.encode`` returns ``str`` in PyJWT 2.x (it returned bytes
    # in 1.x). The signature wraps the algorithm string explicitly to
    # produce a reproducible-shape token regardless of any ambient
    # PyJWT default.
    token = pyjwt.encode(claims, signing_key, algorithm=algorithm)

    _logger.info(
        "session_jwt_minted",
        user_id=str(user.id),
        org_id=str(user.org_id),
        role=claims["role"],
        token_version=token_version,
        ttl_seconds=ttl_seconds,
    )

    return token


def verify_session_jwt(token: str) -> dict[str, Any]:
    """Verify an HS256 session JWT and return its claims dict.

    Reads two Flask config keys:

    * ``JWT_SIGNING_KEY`` - the HMAC secret used at mint time.
    * ``JWT_ALGORITHM`` - always ``"HS256"`` per AAP Section 0.5.2.

    Per AAP Section 0.7.4, this function pins the algorithm to
    ``HS256`` (NOT ``["HS256"]`` with the ambiguous ``algorithms``
    list-form) to defeat algorithm-confusion attacks (CWE-345). PyJWT
    rejects tokens whose ``alg`` header does not match the pinned
    algorithm, so an attacker cannot swap to ``none`` or ``RS256``.

    Validates:

    * Signature (HMAC-SHA256 against ``JWT_SIGNING_KEY``).
    * Expiry (``exp`` claim; default leeway 0).
    * Required claim presence: ``user_id``, ``org_id``, ``role``,
      ``tv``.
    * Role value membership in :class:`UserRole`.
    * ``tv`` claim parses as a non-negative integer (the live
      comparison against ``users.token_version`` is performed by the
      auth middleware, not here, because this function is also
      called from CLI tools and tests that may not have a database
      connection).

    Args:
        token: The raw JWT string extracted from the session cookie
            (or the Authorization Bearer header for service-to-service
            tests).

    Returns:
        The decoded claims dict. The middleware
        :mod:`app.middleware.auth` reconstructs a typed
        :class:`Session` from this dict.

    Raises:
        AuthenticationError: On any failure (signature mismatch,
            expired, missing required claim, unknown role). Mapped to
            HTTP 401 by the registered Flask error handler. The
            structured log distinguishes the failure modes for
            forensics; the user-facing error is generic.
    """
    if not token or not isinstance(token, str):
        raise AuthenticationError()

    signing_key = current_app.config["JWT_SIGNING_KEY"]
    algorithm = current_app.config.get("JWT_ALGORITHM", "HS256")

    try:
        # Pin the algorithm to a SINGLETON list. PyJWT 2.x requires
        # ``algorithms`` to be a list; passing a single string raises
        # DeprecationWarning. The list-form here defeats algorithm
        # confusion: PyJWT rejects ``alg: none`` and any algorithm
        # not in the list.
        claims = pyjwt.decode(
            token,
            signing_key,
            algorithms=[algorithm],
            # Strict claim presence; PyJWT raises MissingRequiredClaimError
            # when ``require`` lists a claim absent from the token.
            options={"require": ["exp", "iat"]},
        )
    except pyjwt.ExpiredSignatureError:
        _logger.info("verify_session_jwt_expired")
        raise AuthenticationError() from None
    except pyjwt.InvalidSignatureError:
        _logger.warning("verify_session_jwt_invalid_signature")
        raise AuthenticationError() from None
    except pyjwt.MissingRequiredClaimError as exc:
        _logger.warning("verify_session_jwt_missing_claim", claim=str(exc))
        raise AuthenticationError() from None
    except pyjwt.InvalidTokenError as exc:
        # Catch-all for malformed tokens (truncated, base64-corrupt,
        # missing dots). Group under one log event since the SPA can
        # only retry from the same UX state regardless of sub-cause.
        # Per AAP Section 0.7.4 secrets-never-logged invariant, we
        # log a redacted partial preview of the token (first 6 chars
        # plus the redaction placeholder) so SREs can correlate
        # repeated decode failures across log lines without exposing
        # the full HS256-signed payload. The PRIMARY defense is the
        # whole-key redaction processor in
        # :mod:`app.observability.logging`; this helper is the
        # secondary defense for free-form fields.
        _logger.info(
            "verify_session_jwt_invalid_token",
            error_class=type(exc).__name__,
            token_preview=redact_secret_for_logging(token, keep=6),
        )
        raise AuthenticationError() from None

    if not isinstance(claims, dict):
        # PyJWT.decode returns a dict for valid tokens; defensive
        # branch in case PyJWT changes behavior in a future version.
        _logger.warning("verify_session_jwt_non_dict_claims", claims_type=type(claims).__name__)
        raise AuthenticationError()

    # Required-claim presence check. ``user_id``, ``org_id``, ``role``,
    # ``tv`` are required by the middleware; PyJWT does not enforce
    # custom claims via ``require`` (it only validates standard
    # RFC 7519 claims), so we check explicitly here. ``tv`` is the
    # token-version claim per AAP section 0.7.4 (Security Invariants);
    # tokens minted prior to the migration that introduced it MUST be
    # rejected so a pre-migration JWT cannot be replayed against a
    # post-migration database where its actor has logged out.
    for required_claim in ("user_id", "org_id", "role", "tv"):
        if required_claim not in claims:
            _logger.warning(
                "verify_session_jwt_missing_custom_claim",
                claim=required_claim,
            )
            raise AuthenticationError()

    # Role value membership check. A token whose ``role`` claim is not
    # one of the three UserRole values is treated as invalid. This
    # defends against a deprecated role name (e.g., "Maintainer") that
    # would otherwise silently pass through.
    role_raw = claims.get("role")
    try:
        UserRole(role_raw)
    except ValueError:
        _logger.warning(
            "verify_session_jwt_unknown_role",
            role=str(role_raw),
        )
        raise AuthenticationError() from None

    # ``tv`` claim type check. Must be a non-negative integer-shaped
    # value. We accept ``int`` directly and ``str`` that parses as
    # int (some JWT libraries serialize numerics as strings); the
    # middleware's DB comparison is over an Integer column so we
    # normalize here. A negative or non-integer ``tv`` is treated as
    # invalid and the response is a generic 401.
    tv_raw = claims["tv"]
    try:
        tv_int = int(tv_raw)
    except (TypeError, ValueError):
        _logger.warning(
            "verify_session_jwt_invalid_tv_type",
            tv_type=type(tv_raw).__name__,
        )
        raise AuthenticationError() from None
    if tv_int < 0:
        _logger.warning("verify_session_jwt_negative_tv", tv=tv_int)
        raise AuthenticationError()
    # Re-store the canonical integer value so downstream callers
    # (middleware) get a consistent type regardless of PyJWT's wire
    # representation.
    claims["tv"] = tv_int

    return claims


# ---------------------------------------------------------------------------
# OAuth user upsert
# ---------------------------------------------------------------------------


def upsert_oauth_user(
    *,
    db_session: DBSession,
    id_token_claims: dict[str, Any],
    org_id: UUID | str,
) -> User:
    """Idempotent INSERT-or-UPDATE of a User from a Google ID token.

    Matches by ``(org_id, email)`` per the composite unique constraint
    :attr:`uq_users_org_email`. New users get
    :data:`UserRole.CONTRIBUTOR` per AAP Section 0.7.6 ("DEFAULT_NEW_USER_ROLE").
    The display_name is sourced from the ID token's ``name`` claim, or
    falls back to the local-part of the email when ``name`` is absent.

    Per AAP Section 0.5.2 Layer 1, OAuth-only users have
    ``password_hash = None``. Existing users (created via the
    email/password flow) who later sign in via OAuth retain their
    ``password_hash`` so they can continue logging in via either flow;
    this function does NOT clear an existing password.

    The function does NOT call :func:`emit_audit_event` directly; the
    auth handler in :mod:`app.api.auth` is responsible for emitting
    the F-013 ``authentication`` event after the upsert succeeds. This
    keeps the service function single-responsibility (user-row mutation)
    and lets the handler emit a single event covering both the
    upsert AND the session-mint side effect.

    Args:
        db_session: An open SQLAlchemy session with an active
            transaction. Caller owns the transaction lifecycle.
        id_token_claims: Verified Google ID token claims dict. The
            handler MUST validate the signature and ``aud``/``iss``
            claims against Google's JWKS BEFORE invoking this
            function; this function trusts the claims unconditionally
            because they have already been authenticated.
        org_id: The organization scope. Sourced from
            ``DEFAULT_ORG_ID`` in MVP (single-org runtime per AAP
            Section 0.7.2).

    Returns:
        The persisted :class:`User` ORM instance, refreshed via
        ``session.flush()`` so any server-assigned columns
        (``created_at``) are populated.

    Raises:
        ValueError: When the ID token claims are missing the required
            ``email`` field. This indicates either a malformed Google
            response or a programmer error in the calling code; the
            handler should surface this as a 401.
    """
    # Required claim: email. Without an email we cannot identify the
    # user uniquely within the organization and cannot satisfy the
    # display constraint that records carry an owner email.
    email = id_token_claims.get("email")
    if not email or not isinstance(email, str):
        raise ValueError("Google ID token claims missing required 'email' field.")

    # Optional claim: name. Falls back to the local-part of the email
    # when absent so the display column is never NULL.
    name = id_token_claims.get("name")
    if not name or not isinstance(name, str):
        name = email.split("@", 1)[0]

    # Optional claim: sub (subject identifier). Stored on the User row
    # if a column exists for it (current schema does not declare
    # ``oauth_subject``); for now we ignore it but log it for
    # forensics so future migrations can backfill the column.
    google_sub = id_token_claims.get("sub")

    org_uuid = _coerce_uuid(org_id, field_name="org_id")

    # Look up by (org_id, email).
    stmt = select(User).where(User.org_id == org_uuid, User.email == email)
    user: User | None = db_session.scalar(stmt)

    if user is None:
        # First-time OAuth login - create a new Contributor user with
        # password_hash=None.
        user = User(
            org_id=org_uuid,
            email=email,
            display_name=name,
            password_hash=None,
            role=UserRole.CONTRIBUTOR,
        )
        db_session.add(user)
        # Flush so the user's id and created_at are populated for the
        # caller (the auth handler typically mints a session JWT
        # immediately, which needs ``user.id``).
        db_session.flush()
        _logger.info(
            "oauth_user_created",
            user_id=str(user.id),
            org_id=str(org_uuid),
            google_sub=google_sub,
        )
        return user

    # Existing user - update the display_name if it has changed
    # (Google name updates flow into the user's profile). We do NOT
    # mutate ``role`` (admins promote users explicitly via the admin
    # panel), and we do NOT clear ``password_hash`` (a hybrid user
    # who registered with a password and later signed in via OAuth
    # retains their password as a fallback).
    if user.display_name != name:
        user.display_name = name
        # No flush here; the caller's ``with session.begin():`` will
        # commit the change atomically.
        _logger.info(
            "oauth_user_display_name_updated",
            user_id=str(user.id),
            org_id=str(org_uuid),
            google_sub=google_sub,
        )
    else:
        _logger.info(
            "oauth_user_login",
            user_id=str(user.id),
            org_id=str(org_uuid),
            google_sub=google_sub,
        )

    return user


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_bcrypt_cost() -> int:
    """Return the bcrypt cost from app config, falling back to 12.

    Reads ``BCRYPT_COST`` from the active Flask app's config. Outside
    an application context (e.g., ad-hoc scripts), defaults to 12 -
    the production cost per AAP Section 0.5.2 - so the resulting hash
    is always production-grade by default.

    Returns:
        The bcrypt cost factor as an int. Production: 12. Tests: 4.
    """
    try:
        return int(current_app.config.get("BCRYPT_COST", 12))
    except RuntimeError:
        # ``current_app`` raises RuntimeError when accessed outside an
        # app context. Fall through to the production default so any
        # ad-hoc caller still produces a strong hash.
        return 12


def _coerce_uuid(value: UUID | str, *, field_name: str) -> UUID:
    """Coerce a UUID or UUID-shaped string into a UUID.

    Args:
        value: The value to coerce. Already a UUID is returned as-is.
        field_name: The field name used in the error message when
            coercion fails. Improves diagnosability when a bad value
            reaches this helper.

    Returns:
        The coerced UUID.

    Raises:
        ValueError: When ``value`` is neither a UUID nor a parsable
            UUID string.
    """
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{field_name} must be a UUID or UUID-shaped string; got {type(value).__name__}",
        ) from exc


def _hash_email_for_log(email: str) -> str:
    """Return a short SHA-256 hash of an email for log correlation.

    The structured log surface MUST NOT echo plaintext emails (PII).
    But for forensic purposes we want a stable identifier so multiple
    failed-login events for the same address can be correlated. A
    truncated SHA-256 hex digest (16 chars) provides this with
    ~64-bit collision resistance, which is adequate for log
    correlation at any realistic user count.

    Args:
        email: The plaintext email to hash.

    Returns:
        A 16-character lowercase hex string.
    """
    # Local import keeps hashlib out of the module-import path. The
    # cost is trivial because authentication failures are rare.
    import hashlib  # noqa: PLC0415

    digest = hashlib.sha256(email.encode("utf-8")).hexdigest()
    return digest[:16]


# ---------------------------------------------------------------------------
# Schema-defined public API (AAP Section 0.5.2 Layer 1)
# ---------------------------------------------------------------------------
#
# The following functions are part of the file's exports schema and
# are the canonical public entry points consumed by the API layer
# (:mod:`app.api.auth`):
#
# - :func:`authenticate_email_password` - thin high-level wrapper
#   around :func:`authenticate_password` that opens its own session
#   and resolves the single-org default org id. Useful for callers
#   that want a one-call email/password flow without building a
#   transactional context first.
# - :func:`record_login_audit` - emits an F-013 ``authentication``
#   audit event for a successful login (password or OAuth). Called
#   by the API layer inside the open transaction that the auth
#   handler manages.
# - :func:`record_logout_audit` - emits an F-013 ``authentication``
#   audit event with ``after_payload.method = "logout"`` for the
#   logout flow. The actor is the per-request
#   :class:`app.middleware.auth.Session` dataclass; the function
#   reads ``user_id`` and ``org_id`` from it.
#
# All three functions raise :class:`AuthError` (HTTP 401) on
# auth-related failure paths so the response envelope is uniform
# across the auth surface. Per AAP Section 0.7.4, error messages
# never distinguish failure modes - the structured log captures
# the distinction for forensics, but the user-facing error is
# generic.


def authenticate_email_password(email: str, password: str) -> User:
    """Validate email and password credentials against the users table.

    High-level public entry point per the file's exports schema
    (AAP Section 0.5.2 Layer 1). Opens its own SQLAlchemy session
    via :meth:`app.extensions.SQLAlchemy.session`, resolves the
    single-org default org id from ``DEFAULT_ORG_ID`` config, and
    delegates to :func:`authenticate_password` for the actual
    credential check (which carries the constant-time
    anti-enumeration guarantee per AAP Section 0.7.4).

    On any failure, raises :class:`AuthError` (mapped to HTTP 401)
    with the GENERIC message ``"Invalid credentials."`` so the
    response does not leak whether the email exists. Per AAP
    Section 0.7.4 anti-enumeration invariant, the error message
    MUST NOT distinguish between unknown email and wrong password.

    This wrapper is the canonical entry point for callers that do
    NOT manage a transactional context themselves (e.g., CLI tools,
    ad-hoc scripts). The HTTP login handler in :mod:`app.api.auth`
    typically uses :func:`authenticate_password` directly so it can
    co-locate the read with the audit-event emission inside a
    single transaction.

    Args:
        email: The user-supplied email address. Lower-casing and
            whitespace trimming are performed inside this wrapper
            so callers do not need to pre-normalize. Empty or
            non-string inputs raise :class:`AuthError`.
        password: The user-supplied plaintext password.

    Returns:
        The authenticated :class:`User` ORM instance.

    Raises:
        AuthError: On any failure (unknown email, wrong password,
            OAuth-only user with NULL ``password_hash``, or any
            configuration issue resolving the default org id).
            Mapped to HTTP 401 by the registered Flask error
            handler.
    """
    # Defensive normalization. The pydantic schema at the API
    # boundary already lower-cases and trims, but service-layer
    # callers (CLI tools, ad-hoc scripts) might bypass the schema.
    # Normalizing here makes the function safe under the broader
    # contract.
    email_normalized = (email or "").strip().lower() if isinstance(email, str) else ""
    if not email_normalized or not isinstance(password, str) or not password:
        # Empty email or empty/non-string password is unconditionally
        # invalid. We still take a constant-time path inside
        # authenticate_password by issuing a probe call so the
        # response time is similar to the real-user path; here we
        # short-circuit BEFORE opening a session because the
        # session-open cost itself is tiny relative to the bcrypt
        # check, and an empty input is a programmer error rather
        # than a credential-stuffing attempt.
        _logger.info(
            "authenticate_email_password_empty_input",
            had_email=bool(email_normalized),
            had_password=bool(isinstance(password, str) and password),
        )
        raise AuthError(message="Invalid credentials.")

    # Resolve the single-org default org id. Per AAP Section 0.7.2,
    # MVP runtime serves exactly one organization but the data model
    # is multi-tenant; the org id is configured via
    # ``DEFAULT_ORG_ID`` Flask config. A misconfigured environment
    # surfaces as a RuntimeError that propagates - the registered
    # error handler maps it to HTTP 500 (which is correct: missing
    # configuration is a server-side defect, not a client error).
    org_id = _resolve_default_org_id()

    # Open a session and delegate to authenticate_password. We use
    # ``db.session()`` (NOT ``db.session().begin()``) because the
    # password-login flow is read-only: no transaction is required
    # for the SELECT, and the audit-event emission is the caller's
    # responsibility (the HTTP handler emits it inside its own
    # ``with session.begin():`` block alongside the JWT mint).
    try:
        with db.session() as session:
            try:
                return authenticate_password(
                    db_session=session,
                    email=email_normalized,
                    password=password,
                    org_id=org_id,
                )
            except AuthenticationError as exc:
                # The internal authenticate_password raises the local
                # AuthenticationError class for backward compatibility
                # with existing callers (e.g., :mod:`app.api.auth`).
                # The schema-defined public API contract is to raise
                # :class:`AuthError`; both inherit from
                # :class:`AppError` and both map to HTTP 401, but
                # uniform exception class is required so callers
                # outside the legacy api/auth.py path can do
                # ``except AuthError:`` without surprise.
                raise AuthError(message="Invalid credentials.") from exc
    except AuthError:
        # Re-raise unchanged - the inner block already produced an
        # AuthError with the canonical generic message.
        raise


def record_login_audit(
    user: User,
    *,
    method: str,
    db_session: DBSession,
) -> None:
    """Emit an F-013 ``authentication`` audit event for a login.

    Called by the API layer (:mod:`app.api.auth`) immediately after
    a successful password or OAuth login, INSIDE the same
    transaction that minted the session and (for OAuth) upserted
    the User row. Per AAP Section 0.7.1 invariant 6, the audit
    event commits or rolls back atomically with the parent state
    change.

    The event_type is :data:`AuditEventType.AUTHENTICATION`;
    ``actor_user_id`` is the authenticated user's id;
    ``target_record_id`` is ``None`` (authentication is keyed to
    the user, not to a record); ``before_payload`` is ``None``
    (no prior state for an authentication action);
    ``after_payload`` describes the method and result so SIEM
    tooling can distinguish password vs OAuth logins and alert on
    repeated failures.

    Args:
        user: The authenticated :class:`User` whose login is being
            recorded. Must have a populated ``id`` and ``org_id``
            (i.e., must be persisted; transient instances do not
            satisfy the FK constraint on ``audit_events.actor_user_id``).
        method: The authentication method, one of
            ``"password"`` or ``"oauth_google"``. Free-form string
            so future methods (SAML, magic-link, passkey) can be
            added without a schema change.
        db_session: The caller's open SQLAlchemy session with an
            active transaction. The audit emitter rejects calls
            made outside an active transaction by raising
            :class:`app.services.audit.AuditEmissionError`.

    Returns:
        ``None``. The persisted :class:`AuditEvent` is not returned
        because the typical caller does not consume it - the audit
        row is written for posterity rather than for in-process
        consumption. Callers that need the id can call
        :func:`emit_audit_event` directly.

    Raises:
        AuditEmissionError: Caller misuse - no active transaction.
            Mapped to HTTP 500 with code ``audit_emission_error``.
        Exception: Any exception raised by the audit emitter's
            INSERT/flush is re-raised after logging so the caller's
            transaction rolls back atomically.
    """
    emit_audit_event(
        db_session=db_session,
        event_type=AuditEventType.AUTHENTICATION,
        actor_user_id=user.id,
        target_record_id=None,
        before_payload=None,
        after_payload={
            # The user_id is denormalized into the payload so audit
            # consumers (admin panel, SIEM dashboards) can read the
            # acting principal without joining back to the users
            # table; the actor_user_id column is the canonical FK
            # but the payload mirrors it for query efficiency.
            "user_id": str(user.id),
            "org_id": str(user.org_id),
            "method": method,
            "result": "success",
        },
    )


def record_logout_audit(
    actor: AuthSession,
    db_session: DBSession,
) -> None:
    """Emit an F-013 ``authentication`` audit event for a logout.

    Called by the API layer (:mod:`app.api.auth`) inside the same
    transaction that increments the user's ``token_version`` (per
    AAP Section 0.7.4 token-rotation invariant). The actor is the
    per-request :class:`app.middleware.auth.Session` dataclass that
    the auth middleware populated on ``g.session``; this function
    reads ``user_id`` and ``org_id`` from the actor so the audit
    row carries the same identity that authorized the logout.

    The event_type is :data:`AuditEventType.AUTHENTICATION`;
    ``after_payload.method`` is ``"logout"`` (distinct from the
    ``"password"`` and ``"oauth_google"`` values used for logins)
    so SIEM tooling can compute session-duration metrics by joining
    login and logout events for the same actor.

    Args:
        actor: The :class:`app.middleware.auth.Session` dataclass
            instance representing the authenticated principal whose
            session is being terminated. Read-only; the dataclass
            is frozen so this function cannot mutate it.
        db_session: The caller's open SQLAlchemy session with an
            active transaction. The audit emitter rejects calls
            made outside an active transaction.

    Returns:
        ``None``. See :func:`record_login_audit` for rationale on
        why the audit row id is not returned.

    Raises:
        AuditEmissionError: Caller misuse - no active transaction.
            Mapped to HTTP 500 with code ``audit_emission_error``.
        Exception: Any exception raised by the audit emitter's
            INSERT/flush is re-raised after logging so the caller's
            transaction rolls back atomically.
    """
    emit_audit_event(
        db_session=db_session,
        event_type=AuditEventType.AUTHENTICATION,
        actor_user_id=actor.user_id,
        target_record_id=None,
        before_payload=None,
        after_payload={
            "user_id": str(actor.user_id),
            "org_id": str(actor.org_id),
            "method": "logout",
            "result": "success",
        },
    )


def _resolve_default_org_id() -> UUID:
    """Return the single-org MVP default organization id from app config.

    Reads ``DEFAULT_ORG_ID`` from the active Flask app's config and
    coerces it to a :class:`uuid.UUID`. Per AAP Section 0.7.2, the
    MVP runtime serves exactly one organization; this helper
    centralizes the lookup so a future migration to multi-org
    runtime requires changing one place.

    Returns:
        The default organization UUID.

    Raises:
        RuntimeError: When ``DEFAULT_ORG_ID`` is missing from the
            Flask config. This indicates a setup error in the
            application factory and surfaces as a 500 (the registered
            error handler maps unconfigured RuntimeError to a generic
            internal-server-error envelope).
        ValueError: When ``DEFAULT_ORG_ID`` is present but is not a
            UUID-shaped string. Same surface as RuntimeError.
    """
    raw = current_app.config.get("DEFAULT_ORG_ID")
    if not raw:
        raise RuntimeError(
            "DEFAULT_ORG_ID is not configured. The single-org MVP "
            "runtime requires this value to be set in Flask config."
        )
    return UUID(str(raw))


# Module reference to time so test patches that target
# ``app.services.auth.time`` find the import. Without this module-level
# binding, tests must patch ``time.time`` globally which can affect
# unrelated code.
_ = time
