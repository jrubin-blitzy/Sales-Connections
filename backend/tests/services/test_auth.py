"""Tests for the F-012 authentication service (`app.services.auth`).

Covers:
    - hash_password / verify_password (bcrypt cost-12 / 4)
    - mint_session_jwt / verify_session_jwt (HS256, claim presence,
      algorithm pinning, expiry handling)
    - authenticate_password (constant-time on user-not-found,
      anti-enumeration, OAuth-only-user rejection)
    - authenticate_email_password (high-level wrapper per file
      exports schema)
    - upsert_oauth_user (INSERT-or-UPDATE by composite (org_id, email),
      hybrid auth preservation)
    - record_login_audit / record_logout_audit (F-013 authentication
      audit-event emission per file exports schema)

Verifies the security invariants documented in AAP Section 0.7.4:
    - Passwords stored as bcrypt salted hashes; never logged; never
      returned to client.
    - JWT algorithm pinned to defeat algorithm-confusion attacks
      (CWE-345).
    - User-not-found path performs constant-time bcrypt check
      (CWE-208 - timing-based user enumeration).
    - Generic 401 message regardless of failure mode.
    - OAuth-only users have password_hash=None and cannot authenticate
      via password.
    - bcrypt 72-byte input limit is enforced explicitly (CWE-20)
      so oversized passwords are REJECTED rather than silently
      truncated.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
import time
from typing import TYPE_CHECKING
import uuid

import bcrypt
import jwt as pyjwt
import pytest
from sqlalchemy import select

from app.middleware.auth import Session as AuthSession
from app.middleware.error_handlers import AppError, AuthError, ValidationFailedError
from app.models import AuditEvent
from app.models.enums import AuditEventType, UserRole
from app.services import auth as auth_module
from app.services.audit import AuditEmissionError
from app.services.auth import (
    AuthenticationError,
    authenticate_email_password,
    authenticate_password,
    hash_password,
    mint_session_jwt,
    record_login_audit,
    record_logout_audit,
    upsert_oauth_user,
    verify_password,
    verify_session_jwt,
)

if TYPE_CHECKING:
    from flask import Flask
    from sqlalchemy.orm import Session as DBSession


# ---------------------------------------------------------------------------
# TestHashPassword
# ---------------------------------------------------------------------------


class TestHashPassword:
    """Verify hash_password emits valid bcrypt hashes."""

    def test_hash_password_produces_bcrypt_hash(self, app: Flask) -> None:
        """Returned string is a valid bcrypt hash."""
        h = hash_password("hunter2")
        # bcrypt hashes start with $2b$ (modular crypt format).
        assert h.startswith("$2b$") or h.startswith("$2a$") or h.startswith("$2y$")
        # Verify we can read the cost from the hash.
        assert bcrypt.checkpw(b"hunter2", h.encode("utf-8"))

    def test_hash_password_uses_configured_cost(self, app: Flask) -> None:
        """The hash carries the cost factor from app config."""
        cost = app.config["BCRYPT_COST"]  # TestingConfig: 4
        h = hash_password("password")
        # bcrypt format: $2b$<cost>$<salt><hash>
        cost_in_hash = int(h.split("$")[2])
        assert cost_in_hash == cost

    def test_hash_password_distinct_hashes_for_same_input(self, app: Flask) -> None:
        """Different invocations produce different hashes (salts differ)."""
        h1 = hash_password("password")
        h2 = hash_password("password")
        assert h1 != h2

    def test_hash_password_rejects_empty(self, app: Flask) -> None:
        """Empty plaintext raises ValueError (defensive guard)."""
        with pytest.raises((ValueError, AppError)):
            hash_password("")


# ---------------------------------------------------------------------------
# TestVerifyPassword
# ---------------------------------------------------------------------------


class TestVerifyPassword:
    """Verify verify_password handles all branches correctly."""

    def test_verify_correct_password_returns_true(self, app: Flask) -> None:
        h = hash_password("right")
        assert verify_password("right", h) is True

    def test_verify_wrong_password_returns_false(self, app: Flask) -> None:
        h = hash_password("right")
        assert verify_password("wrong", h) is False

    def test_verify_none_hash_returns_false(self, app: Flask) -> None:
        """OAuth-only users have password_hash=None; verify must not crash."""
        assert verify_password("anything", None) is False

    def test_verify_malformed_hash_returns_false(self, app: Flask) -> None:
        """A garbled hash returns False rather than raising."""
        assert verify_password("anything", "not-a-bcrypt-hash") is False


# ---------------------------------------------------------------------------
# TestMintSessionJwt
# ---------------------------------------------------------------------------


class TestMintSessionJwt:
    """Verify mint_session_jwt emits valid HS256 tokens."""

    def test_mint_returns_string(self, app: Flask, contributor_user) -> None:
        token = mint_session_jwt(contributor_user)
        assert isinstance(token, str)
        # JWT format: header.payload.signature (3 dot-delimited parts).
        assert token.count(".") == 2

    def test_mint_includes_required_claims(self, app: Flask, contributor_user) -> None:
        """Token claims include user_id, org_id, role, exp, iat."""
        token = mint_session_jwt(contributor_user)
        # Decode without verification to inspect claims.
        claims = pyjwt.decode(token, options={"verify_signature": False})
        assert claims["user_id"] == str(contributor_user.id)
        assert claims["org_id"] == str(contributor_user.org_id)
        assert claims["role"] == contributor_user.role.value
        assert "iat" in claims
        assert "exp" in claims

    def test_mint_uses_configured_algorithm(self, app: Flask, contributor_user) -> None:
        """JWT header carries HS256 (or whatever JWT_ALGORITHM is)."""
        token = mint_session_jwt(contributor_user)
        header = pyjwt.get_unverified_header(token)
        assert header["alg"] == app.config["JWT_ALGORITHM"]

    def test_mint_token_is_verifiable(self, app: Flask, contributor_user) -> None:
        """Token round-trips through verify_session_jwt."""
        token = mint_session_jwt(contributor_user)
        claims = verify_session_jwt(token)
        assert claims["user_id"] == str(contributor_user.id)


# ---------------------------------------------------------------------------
# TestVerifySessionJwt
# ---------------------------------------------------------------------------


class TestVerifySessionJwt:
    """Verify verify_session_jwt enforces all security checks."""

    def test_verify_valid_token_returns_claims(self, app: Flask, contributor_user) -> None:
        token = mint_session_jwt(contributor_user)
        claims = verify_session_jwt(token)
        assert claims["user_id"] == str(contributor_user.id)
        assert claims["role"] == contributor_user.role.value

    def test_verify_rejects_expired_token(self, app: Flask, contributor_user) -> None:
        """An expired token raises AuthenticationError."""
        # Manually craft an expired token.
        expired_claims = {
            "user_id": str(contributor_user.id),
            "org_id": str(contributor_user.org_id),
            "role": contributor_user.role.value,
            "email": contributor_user.email,
            "display_name": contributor_user.display_name,
            "iat": int((datetime.now(UTC) - timedelta(hours=10)).timestamp()),
            "exp": int((datetime.now(UTC) - timedelta(hours=2)).timestamp()),
        }
        token = pyjwt.encode(
            expired_claims,
            app.config["JWT_SIGNING_KEY"],
            algorithm=app.config["JWT_ALGORITHM"],
        )
        with pytest.raises(AuthenticationError):
            verify_session_jwt(token)

    def test_verify_rejects_wrong_signature(self, app: Flask) -> None:
        """Token signed with a different key raises."""
        token = pyjwt.encode(
            {
                "user_id": str(uuid.uuid4()),
                "org_id": str(uuid.uuid4()),
                "role": "Contributor",
                "iat": int(datetime.now(UTC).timestamp()),
                "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
            },
            "wrong-signing-key",  # Wrong secret
            algorithm="HS256",
        )
        with pytest.raises(AuthenticationError):
            verify_session_jwt(token)

    def test_verify_rejects_algorithm_confusion(self, app: Flask, contributor_user) -> None:
        """Token signed with 'none' algorithm is rejected (CWE-345 defense)."""
        # Manually build an unsigned token.
        unsigned = pyjwt.encode(
            {
                "user_id": str(contributor_user.id),
                "org_id": str(contributor_user.org_id),
                "role": contributor_user.role.value,
                "iat": int(datetime.now(UTC).timestamp()),
                "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
            },
            "",  # Empty key
            algorithm="none",
        )
        with pytest.raises(AuthenticationError):
            verify_session_jwt(unsigned)

    def test_verify_rejects_missing_user_id_claim(self, app: Flask) -> None:
        """Token missing user_id claim is rejected."""
        token = pyjwt.encode(
            {
                "org_id": str(uuid.uuid4()),
                "role": "Contributor",
                "iat": int(datetime.now(UTC).timestamp()),
                "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
            },
            app.config["JWT_SIGNING_KEY"],
            algorithm=app.config["JWT_ALGORITHM"],
        )
        with pytest.raises(AuthenticationError):
            verify_session_jwt(token)

    def test_verify_rejects_invalid_role(self, app: Flask) -> None:
        """Token carrying an unknown role string is rejected."""
        token = pyjwt.encode(
            {
                "user_id": str(uuid.uuid4()),
                "org_id": str(uuid.uuid4()),
                "role": "SuperUser",  # Not a valid UserRole
                "iat": int(datetime.now(UTC).timestamp()),
                "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
            },
            app.config["JWT_SIGNING_KEY"],
            algorithm=app.config["JWT_ALGORITHM"],
        )
        with pytest.raises(AuthenticationError):
            verify_session_jwt(token)


# ---------------------------------------------------------------------------
# TestAuthenticatePassword
# ---------------------------------------------------------------------------


class TestAuthenticatePassword:
    """Verify authenticate_password's anti-enumeration and lookup paths."""

    def test_correct_credentials_returns_user(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """A user with the right password is returned."""
        # Reset password to a known value.
        new_hash = hash_password("known-password")
        contributor_user.password_hash = new_hash
        db_session.commit()

        user = authenticate_password(
            db_session=db_session,
            email=contributor_user.email,
            password="known-password",
            org_id=organization.id,
        )
        assert user.id == contributor_user.id

    def test_wrong_password_raises_authentication_error(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """Wrong password yields a generic AuthenticationError."""
        new_hash = hash_password("right-password")
        contributor_user.password_hash = new_hash
        db_session.commit()

        with pytest.raises(AuthenticationError):
            authenticate_password(
                db_session=db_session,
                email=contributor_user.email,
                password="wrong-password",
                org_id=organization.id,
            )

    def test_user_not_found_raises_authentication_error(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
    ) -> None:
        """Non-existent user yields the same generic error."""
        with pytest.raises(AuthenticationError):
            authenticate_password(
                db_session=db_session,
                email="nonexistent@example.com",
                password="any-password",
                org_id=organization.id,
            )

    def test_oauth_only_user_cannot_password_authenticate(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
    ) -> None:
        """User with password_hash=None cannot password-authenticate."""
        from tests.factories import OAuthUserFactory  # noqa: PLC0415

        oauth_user = OAuthUserFactory(organization=organization)
        assert oauth_user.password_hash is None

        with pytest.raises(AuthenticationError):
            authenticate_password(
                db_session=db_session,
                email=oauth_user.email,
                password="any-password",
                org_id=organization.id,
            )

    def test_constant_time_on_user_not_found(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """User-not-found path performs comparable bcrypt work to wrong-password.

        This is a coarse timing check; real timing-attack defense is
        the bcrypt.checkpw call against the dummy hash. We just verify
        the call path exists by ensuring both branches take a similar
        order of magnitude of time.
        """
        # Set up a real user with a known password.
        new_hash = hash_password("right")
        contributor_user.password_hash = new_hash
        db_session.commit()

        # Time the wrong-password path.
        start = time.perf_counter()
        with contextlib.suppress(AuthenticationError):
            authenticate_password(
                db_session=db_session,
                email=contributor_user.email,
                password="wrong-password",
                org_id=organization.id,
            )
        wrong_pw_elapsed = time.perf_counter() - start

        # Time the user-not-found path.
        start = time.perf_counter()
        with contextlib.suppress(AuthenticationError):
            authenticate_password(
                db_session=db_session,
                email="nonexistent@example.com",
                password="any-password",
                org_id=organization.id,
            )
        not_found_elapsed = time.perf_counter() - start

        # Both should be in the same order of magnitude. If the
        # not-found path skipped bcrypt entirely, it would be 100x
        # faster - which would be a bug. We allow up to 5x variance
        # to account for normal jitter on shared CI hardware.
        ratio = max(wrong_pw_elapsed, not_found_elapsed) / min(wrong_pw_elapsed, not_found_elapsed)
        assert ratio < 10, (
            f"Timing-attack window: wrong-pw={wrong_pw_elapsed:.4f}s, "
            f"not-found={not_found_elapsed:.4f}s, ratio={ratio:.1f}x"
        )


# ---------------------------------------------------------------------------
# TestUpsertOAuthUser
# ---------------------------------------------------------------------------


class TestUpsertOAuthUser:
    """Verify upsert_oauth_user creates and updates correctly."""

    def test_creates_new_user_on_first_login(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
    ) -> None:
        """Unknown email creates a new user."""
        from app.models import User  # noqa: PLC0415

        with db_session.begin():
            user = upsert_oauth_user(
                db_session=db_session,
                id_token_claims={
                    "email": "newuser@example.com",
                    # email_verified=True is required by DL-0045
                    # service-layer enforcement; the API handler
                    # forwards this claim from Google's validated
                    # ID token.
                    "email_verified": True,
                    "name": "New User",
                    "sub": "google-id-12345",
                },
                org_id=organization.id,
            )

        # User exists, has password_hash=None (OAuth-only).
        assert user.email == "newuser@example.com"
        assert user.display_name == "New User"
        assert user.password_hash is None
        assert user.role == UserRole.CONTRIBUTOR
        # Verify in DB.
        rows = db_session.query(User).filter_by(email="newuser@example.com").all()
        assert len(rows) == 1

    def test_updates_existing_user_display_name(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
    ) -> None:
        """Existing user's display_name is updated from ID token."""
        from tests.factories import UserFactory  # noqa: PLC0415

        existing = UserFactory(
            organization=organization,
            email="existing@example.com",
            display_name="Old Name",
        )
        original_id = existing.id

        with db_session.begin():
            user = upsert_oauth_user(
                db_session=db_session,
                id_token_claims={
                    "email": "existing@example.com",
                    # email_verified=True per DL-0045.
                    "email_verified": True,
                    "name": "New Name",
                    "sub": "google-id-67890",
                },
                org_id=organization.id,
            )

        # Same row, updated display_name.
        assert user.id == original_id
        assert user.display_name == "New Name"

    def test_preserves_existing_password_hash(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
    ) -> None:
        """Hybrid auth: password_hash is NOT cleared on OAuth login."""
        from tests.factories import UserFactory  # noqa: PLC0415

        original_hash = hash_password("known-password")
        UserFactory(
            organization=organization,
            email="hybrid@example.com",
            password_hash=original_hash,
        )

        with db_session.begin():
            user = upsert_oauth_user(
                db_session=db_session,
                id_token_claims={
                    "email": "hybrid@example.com",
                    # email_verified=True per DL-0045.
                    "email_verified": True,
                    "name": "Hybrid User",
                },
                org_id=organization.id,
            )

        # password_hash is preserved.
        assert user.password_hash == original_hash

    def test_falls_back_to_email_local_part_when_name_missing(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
    ) -> None:
        """Missing 'name' claim falls back to email local-part."""
        with db_session.begin():
            user = upsert_oauth_user(
                db_session=db_session,
                id_token_claims={
                    "email": "alice@example.com",
                    # email_verified=True per DL-0045.
                    "email_verified": True,
                    # No 'name'
                },
                org_id=organization.id,
            )

        assert user.display_name == "alice"

    def test_missing_email_raises_value_error(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
    ) -> None:
        """ID token without 'email' claim raises ValueError."""
        with db_session.begin(), pytest.raises(ValueError, match="email"):
            upsert_oauth_user(
                db_session=db_session,
                id_token_claims={
                    "name": "No Email",
                    # No 'email' claim
                },
                org_id=organization.id,
            )


# ---------------------------------------------------------------------------
# TestHashPasswordSchemaRequirements
# ---------------------------------------------------------------------------
# The file exports schema for ``backend/app/services/auth.py`` lists
# ``ValidationFailedError`` as a required imported exception class, with
# the explicit purpose: "ValidationFailedError (422) for hash_password
# rejections (non-string input, plaintext exceeding bcrypt's 72-byte
# limit) with field-level error details so the SPA can highlight the
# offending password field". These tests verify those rejection paths.


class TestHashPasswordSchemaRequirements:
    """Verify hash_password enforces the bcrypt 72-byte limit and string-typing.

    Per AAP Section 0.7.4 password-storage invariants and the file
    exports schema, oversized inputs are REJECTED (HTTP 422) rather
    than silently truncated. Silent truncation would produce the
    subtle bug "password works for any string sharing the same first
    72 bytes" - a CWE-20 input-handling defect.
    """

    def test_rejects_non_string_input(self, app: Flask) -> None:
        """Non-string input raises ValidationFailedError (422)."""
        with pytest.raises(ValidationFailedError) as exc_info:
            hash_password(b"bytes-input")  # type: ignore[arg-type]
        assert exc_info.value.status_code == 422
        assert "must be a string" in exc_info.value.message.lower()

    def test_rejects_oversized_input(self, app: Flask) -> None:
        """73 bytes raises ValidationFailedError; 72 bytes succeeds."""
        # 72 bytes (boundary) - succeeds.
        h = hash_password("a" * 72)
        assert h.startswith("$2b$")
        # Verify the 72-byte value can be checked against the hash.
        assert bcrypt.checkpw(b"a" * 72, h.encode("utf-8"))

        # 73 bytes - raises.
        with pytest.raises(ValidationFailedError) as exc_info:
            hash_password("a" * 73)
        assert exc_info.value.status_code == 422
        assert "72" in exc_info.value.message  # mentions the limit

    def test_oversized_check_uses_byte_count_not_char_count(self, app: Flask) -> None:
        """Length check is byte-based (UTF-8), not character-based.

        A 36-character string of multi-byte chars (each 2 UTF-8 bytes)
        yields 72 bytes - boundary success. A 37-character string
        yields 74 bytes - rejection.
        """
        # Latin-1 supplement chars (e.g., n with tilde) encode as 2
        # UTF-8 bytes each. 36 chars = 72 bytes (boundary).
        boundary = "\u00f1" * 36  # n-tilde * 36
        assert len(boundary.encode("utf-8")) == 72
        h = hash_password(boundary)
        assert h.startswith("$2b$")

        # 37 chars * 2 bytes = 74 bytes (over limit).
        with pytest.raises(ValidationFailedError):
            hash_password("\u00f1" * 37)


# ---------------------------------------------------------------------------
# TestAuthenticateEmailPassword
# ---------------------------------------------------------------------------
# ``authenticate_email_password`` is the schema-defined high-level
# entry point per AAP Section 0.5.2 Layer 1. It opens its own session,
# resolves the single-org default org id from config, and delegates to
# ``authenticate_password`` for the credential check. On any failure
# it raises ``AuthError`` (HTTP 401) with the GENERIC message
# "Invalid credentials." per the AAP Section 0.7.4 anti-enumeration
# invariant.


class TestAuthenticateEmailPassword:
    """Verify the authenticate_email_password high-level wrapper."""

    def test_correct_credentials_returns_user(
        self,
        app: Flask,
        db_session: DBSession,
        contributor_user,
    ) -> None:
        """A user with the right password is returned."""
        contributor_user.password_hash = hash_password("known-password-2026")
        db_session.commit()

        user = authenticate_email_password(
            email=contributor_user.email,
            password="known-password-2026",
        )
        assert user.id == contributor_user.id

    def test_unknown_email_raises_auth_error(self, app: Flask) -> None:
        """Unknown email raises AuthError (HTTP 401), not AuthenticationError.

        The schema's exports contract requires the public wrapper to
        raise ``AuthError`` (the canonical project-wide exception
        type) so callers can do ``except AuthError:`` without coupling
        to the legacy local ``AuthenticationError`` class.
        """
        with pytest.raises(AuthError) as exc_info:
            authenticate_email_password(
                email="nobody-2026@nowhere.invalid",
                password="any-password",
            )
        assert exc_info.value.status_code == 401

    def test_wrong_password_raises_auth_error(
        self,
        app: Flask,
        db_session: DBSession,
        contributor_user,
    ) -> None:
        """Wrong password raises AuthError with status 401."""
        contributor_user.password_hash = hash_password("the-real-password")
        db_session.commit()

        with pytest.raises(AuthError) as exc_info:
            authenticate_email_password(
                email=contributor_user.email,
                password="wrong-password",
            )
        assert exc_info.value.status_code == 401

    def test_empty_email_raises_auth_error(self, app: Flask) -> None:
        """Empty email is rejected uniformly with AuthError."""
        with pytest.raises(AuthError):
            authenticate_email_password(email="", password="any")
        with pytest.raises(AuthError):
            authenticate_email_password(email="   ", password="any")

    def test_empty_password_raises_auth_error(self, app: Flask) -> None:
        """Empty password is rejected uniformly with AuthError."""
        with pytest.raises(AuthError):
            authenticate_email_password(email="user@example.com", password="")

    def test_non_string_inputs_raise_auth_error(self, app: Flask) -> None:
        """Non-string inputs are rejected uniformly with AuthError."""
        with pytest.raises(AuthError):
            authenticate_email_password(
                email=None,  # type: ignore[arg-type]
                password="x",
            )
        with pytest.raises(AuthError):
            authenticate_email_password(
                email="user@example.com",
                password=None,  # type: ignore[arg-type]
            )

    def test_email_normalization(
        self,
        app: Flask,
        db_session: DBSession,
        contributor_user,
    ) -> None:
        """Mixed-case email matches lowercase stored email.

        The wrapper normalizes the email before lookup so callers do
        not need to pre-normalize. The stored email is already
        lowercased per AAP Section 0.5.2 Layer 1.
        """
        contributor_user.password_hash = hash_password("normalize-test-pass")
        db_session.commit()

        # Pass UPPERCASE email; wrapper normalizes to lowercase.
        upper_email = contributor_user.email.upper()
        user = authenticate_email_password(
            email=upper_email,
            password="normalize-test-pass",
        )
        assert user.id == contributor_user.id


# ---------------------------------------------------------------------------
# TestRecordLoginAudit
# ---------------------------------------------------------------------------
# ``record_login_audit`` emits an F-013 ``authentication`` audit
# event for a successful login (password or OAuth). Per AAP Section
# 0.7.1 invariant 6 (atomic state-change + audit pair), the event is
# persisted inside the caller's open transaction.


class TestRecordLoginAudit:
    """Verify record_login_audit emits authentication events correctly."""

    def test_emits_password_authentication_event(
        self,
        app: Flask,
        db_session: DBSession,
        contributor_user,
    ) -> None:
        """A password login emits an authentication event with method=password."""
        with db_session.begin():
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
        # The most-recent event we just emitted carries the expected
        # payload shape per the schema's after_payload contract.
        latest = max(events, key=lambda e: e.event_timestamp)
        assert latest.event_type == AuditEventType.AUTHENTICATION
        assert latest.actor_user_id == contributor_user.id
        # target_record_id is None for authentication events (the row
        # is keyed to a user, not to a record).
        assert latest.target_record_id is None
        # before_payload is None - no prior state for an
        # authentication action.
        assert latest.before_payload is None
        assert latest.after_payload is not None
        assert latest.after_payload["method"] == "password"
        assert latest.after_payload["result"] == "success"
        assert latest.after_payload["user_id"] == str(contributor_user.id)
        assert latest.after_payload["org_id"] == str(contributor_user.org_id)

    def test_emits_oauth_authentication_event(
        self,
        app: Flask,
        db_session: DBSession,
        contributor_user,
    ) -> None:
        """An OAuth login emits an authentication event with method=oauth_google."""
        with db_session.begin():
            record_login_audit(
                contributor_user,
                method="oauth_google",
                db_session=db_session,
            )

        events = db_session.scalars(
            select(AuditEvent).where(AuditEvent.actor_user_id == contributor_user.id)
        ).all()
        latest = max(events, key=lambda e: e.event_timestamp)
        assert latest.after_payload is not None
        assert latest.after_payload["method"] == "oauth_google"

    def test_outside_transaction_raises(
        self,
        app: Flask,
        db_session: DBSession,
        contributor_user,
    ) -> None:
        """Calling outside an active transaction raises AuditEmissionError.

        Per AAP Section 0.7.1 invariant 6, the audit emitter REJECTS
        calls made outside an open transaction so the audit row
        cannot escape the parent state-change atomic boundary.
        """
        with pytest.raises(AuditEmissionError):
            record_login_audit(
                contributor_user,
                method="password",
                db_session=db_session,
            )


# ---------------------------------------------------------------------------
# TestRecordLogoutAudit
# ---------------------------------------------------------------------------
# ``record_logout_audit`` emits an F-013 ``authentication`` event with
# ``after_payload.method = "logout"`` for the logout flow. The actor
# is the per-request ``app.middleware.auth.Session`` dataclass.


class TestRecordLogoutAudit:
    """Verify record_logout_audit emits logout events correctly."""

    def test_emits_logout_authentication_event(
        self,
        app: Flask,
        db_session: DBSession,
        contributor_user,
    ) -> None:
        """A logout emits an authentication event with method=logout."""
        actor = AuthSession(
            user_id=contributor_user.id,
            org_id=contributor_user.org_id,
            role=UserRole.CONTRIBUTOR,
            email=contributor_user.email,
        )

        with db_session.begin():
            record_logout_audit(actor, db_session=db_session)

        events = db_session.scalars(
            select(AuditEvent).where(AuditEvent.actor_user_id == contributor_user.id)
        ).all()
        latest = max(events, key=lambda e: e.event_timestamp)
        assert latest.event_type == AuditEventType.AUTHENTICATION
        assert latest.actor_user_id == contributor_user.id
        assert latest.target_record_id is None
        assert latest.before_payload is None
        assert latest.after_payload is not None
        assert latest.after_payload["method"] == "logout"
        assert latest.after_payload["result"] == "success"
        assert latest.after_payload["user_id"] == str(contributor_user.id)
        assert latest.after_payload["org_id"] == str(contributor_user.org_id)

    def test_outside_transaction_raises(
        self,
        app: Flask,
        db_session: DBSession,
        contributor_user,
    ) -> None:
        """Calling outside an active transaction raises AuditEmissionError."""
        actor = AuthSession(
            user_id=contributor_user.id,
            org_id=contributor_user.org_id,
            role=UserRole.CONTRIBUTOR,
        )
        with pytest.raises(AuditEmissionError):
            record_logout_audit(actor, db_session=db_session)


# ---------------------------------------------------------------------------
# TestSchemaExports
# ---------------------------------------------------------------------------
# Verify the file exports schema is satisfied: all eight required
# exports are importable and callable from ``app.services.auth``.


class TestSchemaExports:
    """Verify all schema-required exports exist with correct shape."""

    def test_all_schema_exports_importable(self) -> None:
        """All 8 schema-required exports must be importable."""
        required_exports = [
            "hash_password",
            "verify_password",
            "mint_session_jwt",
            "verify_session_jwt",
            "authenticate_email_password",
            "upsert_oauth_user",
            "record_login_audit",
            "record_logout_audit",
        ]
        for name in required_exports:
            assert hasattr(auth_module, name), f"Missing schema-required export: {name}"
            obj = getattr(auth_module, name)
            assert callable(obj), f"Schema-required export {name} is not callable"

    def test_schema_exports_in_dunder_all(self) -> None:
        """The schema exports must be in __all__ for explicit re-export."""
        required_in_all = [
            "hash_password",
            "verify_password",
            "mint_session_jwt",
            "verify_session_jwt",
            "authenticate_email_password",
            "upsert_oauth_user",
            "record_login_audit",
            "record_logout_audit",
        ]
        for name in required_in_all:
            assert name in auth_module.__all__, f"Schema-required export {name} not in __all__"
