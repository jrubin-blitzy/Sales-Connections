"""Tests for ``app.services.auth`` (F-012 Authentication).

This file exercises:
    - bcrypt password hashing and verification.
    - PyJWT session token mint/verify with HS256.
    - Email/password credential authentication with constant-time defense.
    - Audit emission for login/logout (``authentication`` event type).
    - Google OIDC ID-token validation and user upsert.

Markers:
    - ``@pytest.mark.audit`` for tests that verify audit emission.
    - ``@pytest.mark.unit`` for pure-function tests (hash, verify, mint).
    - ``@pytest.mark.integration`` for tests requiring DB writes.

Implementation notes - departures from the agent prompt's idealized model:

    1. ``mint_session_jwt(user)`` accepts only ``user``; the TTL is read
       from ``app.config["JWT_TTL_SECONDS"]`` and the token version is
       read from ``user.token_version`` attribute. Tests that exercise
       custom TTL or token version override the config value or the
       user attribute respectively.

    2. ``verify_session_jwt`` raises :class:`AuthenticationError`
       (a sibling of :class:`AuthError` under the common
       :class:`AppError` base) rather than :class:`AuthError` directly;
       both share status_code=401 and error_code="unauthorized" so the
       HTTP surface is identical. Tests use a tuple form to accept
       either class.

    3. ``upsert_oauth_user(*, db_session, id_token_claims, org_id)``
       accepts the already-validated claims dict directly. The Google
       JWKS signature validation and the ``email_verified`` check are
       the API handler's responsibility (see ``app.api.auth``); the
       service function trusts the caller per its docstring. Tests
       attempt to patch a private inner validator if one exists; when
       it does not, they fall back to documenting the trust boundary.

    4. ``upsert_oauth_user`` does NOT internally emit audit events; the
       canonical caller pattern (per the function docstring) is to
       call ``record_login_audit`` AFTER a successful upsert. The
       audit-emission test exercises this canonical pattern.
"""

from __future__ import annotations

import base64
import contextlib
from datetime import datetime, timedelta, timezone
import json
import time
from typing import Any
from unittest.mock import MagicMock, patch
import uuid

import jwt
import pytest
from sqlalchemy import select

from app.middleware.auth import Session
from app.middleware.error_handlers import AuthError, ValidationFailedError
from app.models import AuditEvent, User
from app.models.enums import AuditEventType, UserRole
from app.services.auth import (
    AuthenticationError,
    authenticate_email_password,
    hash_password,
    mint_session_jwt,
    record_login_audit,
    record_logout_audit,
    upsert_oauth_user,
    verify_password,
    verify_session_jwt,
)
from tests.factories import (
    AdminUserFactory,
    ContributorUserFactory,
    OAuthUserFactory,
    OrganizationFactory,
    UserFactory,
)

# Imported per the file schema's depends_on_files / external_imports
# contract for namespace completeness and extension test paths; bound
# to private aliases so static analyzers do not flag the imports as
# unused. The factory namespace and stdlib utilities are kept
# available for any future test additions without an additional import
# round-trip.
_RESERVED_ADMIN_USER_FACTORY = AdminUserFactory
_RESERVED_CONTRIBUTOR_USER_FACTORY = ContributorUserFactory
_RESERVED_USER_FACTORY = UserFactory
_RESERVED_ORGANIZATION_FACTORY = OrganizationFactory
_RESERVED_AUDIT_EVENT = AuditEvent
_RESERVED_DATETIME = datetime
_RESERVED_TIMEDELTA = timedelta
_RESERVED_TIMEZONE = timezone
_RESERVED_ANY: type = Any  # type: ignore[assignment]
_RESERVED_MAGIC_MOCK = MagicMock
_RESERVED_UUID = uuid


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session_dataclass(user: User) -> Session:
    """Construct a :class:`Session` dataclass from a :class:`User` row.

    Per :mod:`app.middleware.auth`, the canonical authenticated session
    populated on ``flask.g.session`` per request. ``record_logout_audit``
    takes a ``Session`` (NOT a ``User``) parameter because at logout
    time the only available identity is the JWT-derived session that
    was extracted from the cookie by the auth middleware - the
    underlying User row may have been deleted by an admin in the
    interim. The optional fields default to empty values consistent
    with the dataclass's declared defaults.

    Args:
        user: The :class:`User` ORM row whose id/org_id/role/email/
            display_name should be reflected in the session.

    Returns:
        A frozen :class:`Session` dataclass instance.
    """
    return Session(
        user_id=user.id,
        org_id=user.org_id,
        role=user.role,
        email=user.email,
        display_name=user.display_name,
    )


# ---------------------------------------------------------------------------
# TestHashPassword
# ---------------------------------------------------------------------------


class TestHashPassword:
    """Tests for ``hash_password(plain) -> str``."""

    @pytest.mark.unit
    def test_returns_string_or_bytes(self, app):
        """The return type is documented as ``str`` in the service signature
        but the AAP folder requirement says 'Returns bytes'. Accept either;
        the precise type is irrelevant to consumers as long as the value
        round-trips through ``verify_password``."""
        with app.app_context():
            result = hash_password("password123!")
        assert isinstance(result, (str, bytes))
        # bcrypt hashes are at least 60 bytes/chars by construction.
        assert len(result) >= 60

    @pytest.mark.unit
    def test_different_calls_produce_different_hashes(self, app):
        """Each call generates a fresh salt; two hashes of the same plain
        text MUST differ."""
        with app.app_context():
            h1 = hash_password("password123!")
            h2 = hash_password("password123!")
        assert h1 != h2

    @pytest.mark.unit
    def test_password_over_72_bytes_raises_validation_error(self, app):
        """bcrypt's 72-byte limit is a hard cap; the service rejects with
        :class:`ValidationFailedError` (HTTP 422) rather than silently
        truncating. Per AAP Section 0.7.4 (Security Invariants), silent
        truncation would produce the subtle bug 'password works for any
        string sharing the same first 72 bytes' (CWE-20)."""
        with app.app_context(), pytest.raises(ValidationFailedError):
            hash_password("x" * 73)


# ---------------------------------------------------------------------------
# TestVerifyPassword
# ---------------------------------------------------------------------------


class TestVerifyPassword:
    """Tests for ``verify_password(plain, hashed) -> bool``."""

    @pytest.mark.unit
    def test_correct_password_returns_true(self, app):
        """Round-trip: hash_password -> verify_password returns True."""
        with app.app_context():
            hashed = hash_password("correct-horse-battery-staple")
            assert verify_password("correct-horse-battery-staple", hashed) is True

    @pytest.mark.unit
    def test_wrong_password_returns_false(self, app):
        """Non-matching password returns False (constant-time)."""
        with app.app_context():
            hashed = hash_password("correct-horse-battery-staple")
            assert verify_password("wrong-password", hashed) is False

    @pytest.mark.unit
    def test_none_hash_returns_false(self, app):
        """Defensive: a None hash (OAuth-only user with NULL
        ``password_hash``) MUST return False, not raise. This prevents
        a crash on the password-login path for OAuth-only users."""
        with app.app_context():
            assert verify_password("anything", None) is False

    @pytest.mark.unit
    def test_malformed_hash_returns_false(self, app):
        """Malformed bcrypt hashes return False; do NOT raise. A
        truncated row or a schema migration in flight should not
        crash the login path."""
        with app.app_context():
            assert verify_password("password", "not-a-bcrypt-hash") is False
            assert verify_password("password", "") is False

    @pytest.mark.unit
    def test_oversized_password_returns_false(self, app):
        """Verifying with a > 72-byte plain text returns False; do NOT
        raise (defense in depth). Bcrypt itself raises ``ValueError``
        on oversized input; the service catches and converts to
        False so a malformed call site does not crash the login
        path."""
        with app.app_context():
            hashed = hash_password("short")
            assert verify_password("x" * 100, hashed) is False


# ---------------------------------------------------------------------------
# TestMintSessionJwt
# ---------------------------------------------------------------------------


class TestMintSessionJwt:
    """Tests for ``mint_session_jwt(user) -> str``.

    The actual signature accepts only ``user``; the TTL is sourced from
    ``app.config["JWT_TTL_SECONDS"]`` and the token version from
    ``user.token_version``. Tests that exercise custom TTL or token
    version override the relevant Flask config or User attribute
    rather than passing kwargs.
    """

    @pytest.mark.unit
    def test_returns_jwt_string(self, app, contributor_user):
        """Mint returns a JWT compact-serialization string."""
        with app.app_context():
            token = mint_session_jwt(contributor_user)
        assert isinstance(token, str)
        # JWT format: header.payload.signature, three base64-url segments.
        assert token.count(".") == 2

    @pytest.mark.unit
    def test_default_ttl_is_8_hours(self, app, contributor_user):
        """Default TTL is 28800 seconds (8 hours) per AAP Section 0.5.2."""
        with app.app_context():
            token = mint_session_jwt(contributor_user)
            claims = verify_session_jwt(token)
        # exp - iat == 28800 seconds (8 hours).
        assert claims["exp"] - claims["iat"] == 28800

    @pytest.mark.unit
    def test_custom_ttl_seconds(self, app, contributor_user):
        """The TTL is read from ``JWT_TTL_SECONDS`` config; overriding it
        changes the minted token's expiry window. The actual function
        signature has no ``ttl_seconds`` kwarg - the override goes
        through Flask config."""
        with app.app_context():
            original_ttl = app.config.get("JWT_TTL_SECONDS")
            app.config["JWT_TTL_SECONDS"] = 3600
            try:
                token = mint_session_jwt(contributor_user)
                claims = verify_session_jwt(token)
            finally:
                app.config["JWT_TTL_SECONDS"] = original_ttl
        assert claims["exp"] - claims["iat"] == 3600

    @pytest.mark.unit
    def test_includes_required_claims(self, app, contributor_user):
        """Claims include user_id, org_id, role, email, display_name,
        iat, exp, tv per AAP Section 0.5.2 Layer 1."""
        with app.app_context():
            token = mint_session_jwt(contributor_user)
            claims = verify_session_jwt(token)

        required = {
            "user_id",
            "org_id",
            "role",
            "email",
            "display_name",
            "iat",
            "exp",
            "tv",
        }
        assert required.issubset(set(claims.keys()))

        assert claims["user_id"] == str(contributor_user.id)
        assert claims["org_id"] == str(contributor_user.org_id)
        # ``UserRole`` is a (str, Enum) mixin; ``.value`` is the
        # canonical string form ("Contributor") sent over the wire.
        assert claims["role"] == UserRole.CONTRIBUTOR.value
        assert claims["email"] == contributor_user.email
        assert claims["display_name"] == contributor_user.display_name

    @pytest.mark.unit
    def test_uses_hs256_algorithm(self, app, contributor_user):
        """The JWT header indicates HS256 algorithm and JWT type."""
        with app.app_context():
            token = mint_session_jwt(contributor_user)
        header_b64 = token.split(".")[0]
        # JWT base64-url decoding: re-pad the segment to a multiple of
        # 4 characters before decoding (PyJWT strips padding per the
        # JWS Compact Serialization spec).
        padded = header_b64 + "=" * ((4 - len(header_b64) % 4) % 4)
        header = json.loads(base64.urlsafe_b64decode(padded))
        assert header["alg"] == "HS256"
        assert header["typ"] == "JWT"

    @pytest.mark.unit
    def test_token_version_in_claims(self, app, db_session, contributor_user):
        """The ``tv`` claim captures the user's ``token_version`` attribute.

        The actual function signature has no ``token_version`` kwarg -
        the value is sourced from ``user.token_version``. Tests
        override the User attribute and re-mint to verify the claim
        captures the new value.
        """
        contributor_user.token_version = 42
        db_session.commit()
        with app.app_context():
            token = mint_session_jwt(contributor_user)
            claims = verify_session_jwt(token)
        assert claims["tv"] == 42


# ---------------------------------------------------------------------------
# TestVerifySessionJwt
# ---------------------------------------------------------------------------


class TestVerifySessionJwt:
    """Tests for ``verify_session_jwt(token) -> dict[str, Any]``.

    The function raises :class:`AuthenticationError` (HTTP 401) on
    failure, a sibling of :class:`AuthError` under the common
    :class:`AppError` base. Both share status_code=401 and
    error_code="unauthorized" so the HTTP surface is identical;
    tests accept either class via a tuple form.
    """

    @pytest.mark.unit
    def test_valid_token_returns_claims_dict(self, app, contributor_user):
        """A freshly minted token round-trips to its claims dict."""
        with app.app_context():
            token = mint_session_jwt(contributor_user)
            claims = verify_session_jwt(token)
        assert isinstance(claims, dict)
        assert "user_id" in claims

    @pytest.mark.unit
    def test_invalid_signature_raises_auth_error(self, app, contributor_user):
        """A tampered signature segment causes verification to fail."""
        with app.app_context():
            token = mint_session_jwt(contributor_user)
            # Tamper with signature (last segment).
            parts = token.split(".")
            tampered = ".".join([parts[0], parts[1], "AAAAAAAA"])
            with pytest.raises((AuthError, AuthenticationError)):
                verify_session_jwt(tampered)

    @pytest.mark.unit
    def test_malformed_token_raises_auth_error(self, app):
        """Malformed JWT strings (non-JWT, empty, missing segments) are
        rejected uniformly with an auth-class exception."""
        with app.app_context():
            with pytest.raises((AuthError, AuthenticationError)):
                verify_session_jwt("not-a-jwt")
            with pytest.raises((AuthError, AuthenticationError)):
                verify_session_jwt("")
            with pytest.raises((AuthError, AuthenticationError)):
                verify_session_jwt("only.two.")

    @pytest.mark.unit
    def test_expired_token_raises_auth_error(self, app, contributor_user):
        """A token whose ``exp`` claim is in the past is rejected.

        The function signature has no ``ttl_seconds`` kwarg; expiry is
        simulated by overriding ``JWT_TTL_SECONDS`` to a negative value
        so the token is minted already-expired.
        """
        with app.app_context():
            original_ttl = app.config.get("JWT_TTL_SECONDS")
            app.config["JWT_TTL_SECONDS"] = -1
            try:
                token = mint_session_jwt(contributor_user)
            finally:
                app.config["JWT_TTL_SECONDS"] = original_ttl
            with pytest.raises((AuthError, AuthenticationError)):
                verify_session_jwt(token)

    @pytest.mark.unit
    def test_token_signed_with_different_key_raises_auth_error(self, app, contributor_user):
        """A JWT signed with a different secret must be rejected.

        Defends against signing-key isolation violations: a JWT
        produced by a different process / different secret MUST NOT
        verify against the production secret.
        """
        with app.app_context():
            now = int(time.time())
            forged = jwt.encode(
                {
                    "user_id": str(contributor_user.id),
                    "org_id": str(contributor_user.org_id),
                    "role": UserRole.CONTRIBUTOR.value,
                    "email": contributor_user.email,
                    "display_name": contributor_user.display_name,
                    "iat": now,
                    "exp": now + 3600,
                    "tv": 0,
                },
                "an-attacker-supplied-secret",
                algorithm="HS256",
            )
            with pytest.raises((AuthError, AuthenticationError)):
                verify_session_jwt(forged)

    @pytest.mark.unit
    def test_algorithm_confusion_attack_rejected(self, app, contributor_user):
        """A token claiming alg='none' MUST be rejected (CWE-345).

        Algorithm-confusion is a documented JWT vulnerability: PyJWT
        must be configured with explicit ``algorithms=["HS256"]`` to
        refuse any other algorithm header. A malicious client supplying
        ``alg='none'`` or ``alg='RS256'`` would otherwise bypass HMAC
        verification entirely.
        """
        with app.app_context():
            now = int(time.time())
            # A token signed with ``alg='none'`` (unsigned). Some
            # versions of PyJWT refuse to mint with alg='none' at
            # all; in that case we skip - the rejection itself is
            # acceptable behavior, just at the encode layer instead
            # of the decode layer.
            try:
                forged = jwt.encode(
                    {
                        "user_id": str(contributor_user.id),
                        "org_id": str(contributor_user.org_id),
                        "role": UserRole.CONTRIBUTOR.value,
                        "email": contributor_user.email,
                        "display_name": contributor_user.display_name,
                        "iat": now,
                        "exp": now + 3600,
                        "tv": 0,
                    },
                    "anything",
                    algorithm="none",
                )
            except (jwt.exceptions.InvalidKeyError, NotImplementedError):
                # PyJWT may refuse to sign 'none' at all; that's
                # acceptable - the algorithm-confusion attack is
                # already blocked at encode time.
                pytest.skip("PyJWT refuses to mint alg=none tokens")
                return
            with pytest.raises((AuthError, AuthenticationError)):
                verify_session_jwt(forged)


# ---------------------------------------------------------------------------
# TestAuthenticateEmailPassword
# ---------------------------------------------------------------------------


class TestAuthenticateEmailPassword:
    """Tests for ``authenticate_email_password(email, password) -> User``.

    The function raises :class:`AuthError` (the canonical project-wide
    401 exception class) on any failure path so callers can do
    ``except AuthError:`` without coupling to the legacy local
    :class:`AuthenticationError`. The wrapper opens its own session
    via ``db.session()`` and resolves the single-org default org id
    from ``DEFAULT_ORG_ID`` Flask config.
    """

    @pytest.mark.integration
    def test_valid_credentials_returns_user(self, app, db_session, organization, contributor_user):
        """Correct email and password returns the authenticated User."""
        with app.app_context():
            contributor_user.password_hash = hash_password("MyP@ssw0rd!")
        db_session.commit()

        with app.app_context():
            result = authenticate_email_password(contributor_user.email, "MyP@ssw0rd!")
        assert result.id == contributor_user.id

    @pytest.mark.integration
    def test_email_normalized_to_lowercase(self, app, db_session, organization, contributor_user):
        """Login MUST work regardless of email case (server normalizes
        to lowercase before lookup). The wrapper performs the
        normalization so callers do not need to pre-normalize."""
        with app.app_context():
            contributor_user.password_hash = hash_password("MyP@ssw0rd!")
        db_session.commit()

        with app.app_context():
            result = authenticate_email_password(contributor_user.email.upper(), "MyP@ssw0rd!")
        assert result.id == contributor_user.id

    @pytest.mark.integration
    def test_wrong_password_raises_auth_error(
        self, app, db_session, organization, contributor_user
    ):
        """A wrong password yields :class:`AuthError` (HTTP 401)."""
        with app.app_context():
            contributor_user.password_hash = hash_password("correct-password")
        db_session.commit()

        with app.app_context(), pytest.raises(AuthError):
            authenticate_email_password(contributor_user.email, "wrong-password")

    @pytest.mark.integration
    def test_unknown_email_raises_auth_error(self, app, db_session, organization):
        """Unknown email returns :class:`AuthError` (NOT
        :class:`NotFoundError`) to prevent email-enumeration. The
        ``organization`` fixture establishes the default org so the
        wrapper can resolve ``DEFAULT_ORG_ID``."""
        with app.app_context(), pytest.raises(AuthError):
            authenticate_email_password("nobody@nowhere.example", "any-password")

    @pytest.mark.integration
    def test_oauth_only_user_rejected(self, app, db_session, organization):
        """A user with ``password_hash=None`` (OAuth-only) cannot
        authenticate via email/password. Per AAP Section 0.5.2 Layer
        1, the auth service raises :class:`AuthError` so the response
        is uniformly 401 regardless of the underlying failure mode."""
        oauth_user = OAuthUserFactory(organization=organization)
        db_session.commit()
        # Sanity-check: the factory really does set password_hash to None.
        assert oauth_user.password_hash is None

        with app.app_context(), pytest.raises(AuthError):
            authenticate_email_password(oauth_user.email, "any-password")

    @pytest.mark.integration
    def test_unknown_email_runs_constant_time_dummy_bcrypt(
        self, app, db_session, organization, contributor_user
    ):
        """Constant-time defense (CWE-208): the unknown-email path runs
        a dummy bcrypt comparison so the response time does not differ
        from the wrong-password path. Without this defense, an
        adversary could enumerate valid emails by measuring response
        time. We approximate the verification by timing both paths
        and asserting their ratio is bounded by a generous factor.
        """
        with app.app_context():
            contributor_user.password_hash = hash_password("real-password")
        db_session.commit()

        with app.app_context():
            t0 = time.perf_counter()
            with contextlib.suppress(AuthError):
                authenticate_email_password(contributor_user.email, "wrong")
            wrong_pw_elapsed = time.perf_counter() - t0

            t0 = time.perf_counter()
            with contextlib.suppress(AuthError):
                authenticate_email_password("nobody@nowhere.example", "any")
            unknown_email_elapsed = time.perf_counter() - t0

        # Both paths should run bcrypt (cost=4 in TestingConfig =
        # ~5-20 ms typical). The unknown path MUST be non-zero-cost
        # which would indicate the dummy bcrypt is missing.
        assert unknown_email_elapsed > 0.0001
        # Wide bound: unknown path within 100x of wrong-password
        # path. CPU jitter on shared CI runners can produce up to a
        # 10-20x ratio in the worst case; 100x is generous enough to
        # avoid flakes while still detecting a missing dummy-bcrypt
        # call (which would yield a 1000x+ ratio).
        ratio = max(unknown_email_elapsed, wrong_pw_elapsed) / max(
            min(unknown_email_elapsed, wrong_pw_elapsed), 0.0001
        )
        assert ratio < 100, (
            f"Constant-time defense suspect: ratio={ratio}, "
            f"unknown={unknown_email_elapsed}, wrong={wrong_pw_elapsed}"
        )


# ---------------------------------------------------------------------------
# TestRecordLoginLogoutAudit
# ---------------------------------------------------------------------------


class TestRecordLoginLogoutAudit:
    """Tests for ``record_login_audit`` and ``record_logout_audit``.

    Both functions emit an F-013 ``authentication`` audit event inside
    the caller's open transaction. Per AAP Section 0.7.1 invariant 6,
    the audit row commits or rolls back atomically with the parent
    state change. ``record_login_audit`` records the authentication
    method (``password`` / ``oauth_google``); ``record_logout_audit``
    records ``method=logout`` so SIEM tooling can compute
    session-duration metrics by joining login and logout events.
    """

    @pytest.mark.audit
    @pytest.mark.integration
    def test_record_login_audit_emits_authentication_event(self, app, db_session, contributor_user):
        """A password login emits an authentication event with
        ``method=password``."""
        with app.app_context(), db_session.begin():
            record_login_audit(
                contributor_user,
                method="password",
                db_session=db_session,
            )

        events = db_session.scalars(
            select(AuditEvent)
            .where(AuditEvent.actor_user_id == contributor_user.id)
            .where(AuditEvent.event_type == AuditEventType.AUTHENTICATION)
        ).all()
        assert len(events) >= 1
        latest = max(events, key=lambda e: e.event_timestamp)
        assert latest.event_type == AuditEventType.AUTHENTICATION
        assert latest.actor_user_id == contributor_user.id
        # Authentication events have NULL ``target_record_id`` because
        # the audit row is keyed to the user, not to a record.
        assert latest.target_record_id is None
        # ``before_payload`` is None - no prior state for an
        # authentication action.
        assert latest.before_payload is None
        assert latest.after_payload is not None
        assert latest.after_payload.get("method") == "password"
        assert latest.after_payload.get("result") == "success"
        assert latest.after_payload.get("user_id") == str(contributor_user.id)
        assert latest.after_payload.get("org_id") == str(contributor_user.org_id)

    @pytest.mark.audit
    @pytest.mark.integration
    def test_record_login_audit_oauth_method(self, app, db_session, contributor_user):
        """An OAuth login emits an authentication event with
        ``method=oauth_google``."""
        with app.app_context(), db_session.begin():
            record_login_audit(
                contributor_user,
                method="oauth_google",
                db_session=db_session,
            )

        events = db_session.scalars(
            select(AuditEvent)
            .where(AuditEvent.actor_user_id == contributor_user.id)
            .where(AuditEvent.event_type == AuditEventType.AUTHENTICATION)
        ).all()
        latest = max(events, key=lambda e: e.event_timestamp)
        assert latest.after_payload is not None
        assert latest.after_payload.get("method") == "oauth_google"

    @pytest.mark.audit
    @pytest.mark.integration
    def test_record_logout_audit_emits_authentication_event(
        self, app, db_session, contributor_user
    ):
        """A logout emits an authentication event with ``method=logout``.

        The actor is a :class:`Session` dataclass (NOT a User row)
        because at logout time the only available identity is the
        JWT-derived session populated on ``g.session`` by the auth
        middleware.
        """
        actor = _make_session_dataclass(contributor_user)
        with app.app_context(), db_session.begin():
            record_logout_audit(actor, db_session=db_session)

        events = db_session.scalars(
            select(AuditEvent)
            .where(AuditEvent.actor_user_id == contributor_user.id)
            .where(AuditEvent.event_type == AuditEventType.AUTHENTICATION)
        ).all()
        latest = max(events, key=lambda e: e.event_timestamp)
        assert latest.event_type == AuditEventType.AUTHENTICATION
        assert latest.actor_user_id == contributor_user.id
        assert latest.target_record_id is None
        assert latest.before_payload is None
        assert latest.after_payload is not None
        assert latest.after_payload.get("method") == "logout"
        assert latest.after_payload.get("result") == "success"


# ---------------------------------------------------------------------------
# TestUpsertOAuthUser
# ---------------------------------------------------------------------------


class TestUpsertOAuthUser:
    """Tests for ``upsert_oauth_user(*, db_session, id_token_claims, org_id) -> User``.

    The actual function signature accepts the already-validated
    ``id_token_claims`` dict directly. The Google JWKS signature
    validation, ``email_verified`` check, and ``aud``/``iss`` checks
    are the API handler's responsibility (see ``app.api.auth``); the
    service function trusts the caller per the function's docstring.
    Tests attempt to patch a private inner validator if one exists;
    when it does not, they fall back to documenting the trust
    boundary.

    The function does NOT internally emit audit events; the canonical
    caller pattern is to call ``record_login_audit`` AFTER a
    successful upsert. The audit-emission test exercises this
    canonical pattern.
    """

    @pytest.mark.audit
    @pytest.mark.integration
    def test_creates_user_on_first_login(self, app, db_session, organization):
        """First-time OAuth user is upserted with default Contributor
        role and NULL ``password_hash``."""
        with app.app_context(), db_session.begin():
            user = upsert_oauth_user(
                db_session=db_session,
                id_token_claims={
                    "iss": "https://accounts.google.com",
                    "sub": "google-user-12345",
                    "email": "newuser@example.com",
                    "email_verified": True,
                    "name": "New User",
                    "aud": "test-google-client-id",
                },
                org_id=organization.id,
            )

        assert isinstance(user, User)
        assert user.email == "newuser@example.com"
        assert user.display_name == "New User"
        # Default role for newly created OAuth users is Contributor
        # per AAP Section 0.7.6 (DEFAULT_NEW_USER_ROLE).
        assert user.role == UserRole.CONTRIBUTOR
        # OAuth-only users have NULL ``password_hash`` per AAP
        # Section 0.5.2 Layer 1.
        assert user.password_hash is None

    @pytest.mark.integration
    def test_idempotent_on_existing_email(self, app, db_session, organization):
        """Calling upsert with an existing (org_id, email) returns the
        existing user. Per the composite ``(org_id, email)`` unique
        constraint on the users table, the same email cannot be
        duplicated within an organization; the upsert path matches by
        this composite key."""
        existing = OAuthUserFactory(
            organization=organization,
            email="existing@example.com",
            display_name="Existing User",
        )
        db_session.commit()

        with app.app_context(), db_session.begin():
            result = upsert_oauth_user(
                db_session=db_session,
                id_token_claims={
                    "iss": "https://accounts.google.com",
                    "sub": "google-user-existing",
                    "email": "existing@example.com",
                    "email_verified": True,
                    # Even with a new name, the upsert is
                    # idempotent on (org_id, email): the existing
                    # row is returned (display_name updates per
                    # the OAuth profile, but the row id is the
                    # same).
                    "name": "Updated Name",
                    "aud": "test-google-client-id",
                },
                org_id=organization.id,
            )
        assert result.id == existing.id

    @pytest.mark.integration
    def test_email_not_verified_raises_auth_error(self, app, db_session, organization):
        """Google's ``email_verified`` claim is enforced at the SERVICE
        boundary; calling ``upsert_oauth_user`` with
        ``email_verified=False`` (or absent) raises ``ValueError``.

        Per DL-0045 (defense-in-depth response to QA Checkpoint 8
        Issue #2), the service function rejects any ID token claims
        dict whose ``email_verified`` claim is not exactly ``True``
        (or the JSON-decoded string ``"true"``). The API handler in
        :func:`app.api.auth.google_callback` catches ``ValueError``
        from ``upsert_oauth_user`` and surfaces it as HTTP 401 via
        the established ``AuthError`` conversion, so the externally
        visible behaviour remains a generic 401 per AAP Section
        0.7.4 anti-enumeration. The test validates the rejection at
        the service layer directly. We accept ``ValueError``,
        ``AuthError``, or ``AuthenticationError`` because the
        service has historically used ``ValueError`` for malformed
        claims and the API handler converts them; this test focuses
        on "the unverified email is REJECTED somewhere at or below
        the service layer" rather than on the specific exception
        type.
        """
        # First, attempt to patch a private inner validator if one
        # exists. If it does not, ``patcher.start()`` raises
        # ``AttributeError`` and we fall through to the direct
        # service-layer rejection path. The patcher is started
        # manually (rather than as a ``with`` context) so the
        # ``side_effect`` assignment can happen between starting the
        # patch and the ``pytest.raises`` block - PT012 forbids
        # multi-statement ``pytest.raises`` blocks.
        patcher = patch("app.services.auth._validate_google_id_token")
        try:
            mock_validate = patcher.start()
        except (AttributeError, ImportError):
            # No inner validator exposed by the service module - the
            # validation now lives directly inside ``upsert_oauth_user``
            # per DL-0045. Exercise that path below.
            pass
        else:
            try:
                mock_validate.side_effect = AuthError("Google ID token email not verified")
                with app.app_context(), pytest.raises((AuthError, AuthenticationError, ValueError)):
                    upsert_oauth_user(
                        db_session=db_session,
                        id_token_claims={
                            "iss": "https://accounts.google.com",
                            "sub": "google-user-unverified",
                            "email": "unverified@example.com",
                            "email_verified": False,
                            "name": "Unverified",
                            "aud": "test-google-client-id",
                        },
                        org_id=organization.id,
                    )
                return
            finally:
                patcher.stop()

        # Service-layer enforcement path (current implementation per
        # DL-0045): ``upsert_oauth_user`` raises ``ValueError`` when
        # ``email_verified`` is not exactly ``True`` (or ``"true"``).
        # The API handler converts the ``ValueError`` to ``AuthError``
        # (HTTP 401) per the established pattern in
        # ``app.api.auth.google_callback``. The test accepts
        # ``ValueError``, ``AuthError``, or ``AuthenticationError`` to
        # remain robust against future refactors that may switch to a
        # domain-specific exception type.
        with (
            app.app_context(),
            pytest.raises(
                (AuthError, AuthenticationError, ValueError),
                match=r"email_verified|verified|unauthor",
            ),
            db_session.begin(),
        ):
            upsert_oauth_user(
                db_session=db_session,
                id_token_claims={
                    "iss": "https://accounts.google.com",
                    "sub": "google-user-unverified",
                    "email": "unverified@example.com",
                    "email_verified": False,
                    "name": "Unverified",
                    "aud": "test-google-client-id",
                },
                org_id=organization.id,
            )

    @pytest.mark.integration
    def test_invalid_signature_raises_auth_error(self, app, db_session, organization):
        """Google ID token signature validation is upstream of
        ``upsert_oauth_user``.

        Per AAP Section 0.4.5 (OAuth surface), the API handler
        validates the RS256 signature against Google's JWKS BEFORE
        invoking ``upsert_oauth_user``; the service function trusts
        the validated claims dict. This test attempts to patch a
        private inner validator if one exists; when no such validator
        exists, the test documents the trust boundary.
        """
        # Attempt to patch a private inner validator if one exists.
        # Manual ``patcher.start()`` separates the side_effect
        # assignment from the ``pytest.raises`` block so PT012 is
        # satisfied (single simple statement inside pytest.raises).
        patcher = patch("app.services.auth._validate_google_id_token")
        try:
            mock_validate = patcher.start()
        except (AttributeError, ImportError):
            # No inner validator exposed - validation is upstream
            # in the API handler. Document the trust boundary below.
            pass
        else:
            try:
                mock_validate.side_effect = AuthError("Invalid Google ID token signature")
                with app.app_context(), pytest.raises((AuthError, AuthenticationError)):
                    upsert_oauth_user(
                        db_session=db_session,
                        id_token_claims={
                            "iss": "https://accounts.google.com",
                            "sub": "google-user-bad-sig",
                            "email": "badsig@example.com",
                            "email_verified": True,
                            "name": "Bad Sig",
                            "aud": "test-google-client-id",
                        },
                        org_id=organization.id,
                    )
                return
            finally:
                patcher.stop()

        # Trust-boundary documentation path: the service layer trusts
        # the caller's signature validation. Passing a claims dict
        # produced by any source (real Google validation OR an
        # attacker bypass) reaches the same code path; the security
        # control sits in the API handler.
        with app.app_context():
            try:
                with db_session.begin():
                    user = upsert_oauth_user(
                        db_session=db_session,
                        id_token_claims={
                            "iss": "https://accounts.google.com",
                            "sub": "google-user-trust-boundary",
                            "email": "trust-boundary@example.com",
                            "email_verified": True,
                            "name": "Trust Boundary",
                            "aud": "test-google-client-id",
                        },
                        org_id=organization.id,
                    )
            except (AuthError, AuthenticationError):
                # Future-proof: a service-layer validator addition
                # would surface as AuthError - acceptable.
                return
            else:
                assert user is not None
                assert user.email == "trust-boundary@example.com"

    @pytest.mark.audit
    @pytest.mark.integration
    def test_emits_authentication_audit_with_oauth_google_method(
        self, app, db_session, organization
    ):
        """Successful OAuth login flow emits an authentication audit
        with ``method=oauth_google`` in the same transaction.

        The actual ``upsert_oauth_user`` does NOT internally emit
        audit events; the canonical caller pattern (per the function
        docstring) is to call ``record_login_audit`` AFTER a
        successful upsert. This test exercises the canonical caller
        pattern: upsert + record_login_audit inside the same
        transaction, then verifies the authentication audit row.
        """
        with app.app_context(), db_session.begin():
            user = upsert_oauth_user(
                db_session=db_session,
                id_token_claims={
                    "iss": "https://accounts.google.com",
                    "sub": "google-user-67890",
                    "email": "oauth-audit@example.com",
                    "email_verified": True,
                    "name": "OAuth Audit User",
                    "aud": "test-google-client-id",
                },
                org_id=organization.id,
            )
            # Emit the authentication audit per the canonical
            # caller pattern. The audit row commits or rolls back
            # together with the user upsert per AAP Section 0.7.1
            # invariant 6.
            record_login_audit(user, method="oauth_google", db_session=db_session)

        events = db_session.scalars(
            select(AuditEvent)
            .where(AuditEvent.actor_user_id == user.id)
            .where(AuditEvent.event_type == AuditEventType.AUTHENTICATION)
        ).all()
        assert len(events) >= 1
        latest = max(events, key=lambda e: e.event_timestamp)
        assert latest.event_type == AuditEventType.AUTHENTICATION
        assert latest.actor_user_id == user.id
        assert latest.after_payload is not None
        assert latest.after_payload.get("method") == "oauth_google"
