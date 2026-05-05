"""Unit tests for ``app.config`` — ProductionConfig fail-fast invariants.

These tests validate the security guard documented in AAP Sec 0.7.4
(Security Invariants) and demanded by the Layer-0 code review:

    "ProductionConfig MUST fail fast with a descriptive error if
    ANTHROPIC_API_KEY, JWT_SIGNING_KEY, GOOGLE_OAUTH_CLIENT_*, or
    DATABASE_URL is missing/empty."

The tests exercise :meth:`app.config.ProductionConfig.init_app` with a
stub Flask-app object whose ``config`` is a plain ``dict``. Using a
stub avoids importing Flask just to run these checks and keeps the
tests fast and side-effect-free.

Coverage targets:

- Each of the four required secrets raises ``RuntimeError`` when
  empty.
- Each of the four required secrets raises ``RuntimeError`` when set
  to a ``PLACEHOLDER_*`` value (the convention used in
  ``backend/.env.example``).
- Each of the four required secrets raises ``RuntimeError`` when set
  to a ``REPLACE_WITH_*`` value (the secondary placeholder convention
  reserved for future use).
- ``JWT_SIGNING_KEY`` raises ``RuntimeError`` when shorter than 32
  bytes of UTF-8 even though all other secrets are valid.
- A fully-configured production environment with real (non-placeholder)
  secrets does NOT raise.
- The error message names every misconfigured secret in a single
  ``RuntimeError`` so operators see all problems at once instead of
  fixing them one-by-one across restart attempts.
- Whitespace-only secret values are treated as empty.
- ``USE_SECRETS_MANAGER=False`` does not bypass the validation.

These tests deliberately do NOT import the Flask application factory
(``app.create_app``) — the validation under test is at the config
class level and must work without a full app context.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.config import (
    _JWT_SIGNING_KEY_MIN_BYTES,
    BaseConfig,
    DevelopmentConfig,
    ProductionConfig,
    TestingConfig,
    _is_placeholder_or_empty,
)

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class _StubApp:
    """Minimal Flask-like stub exposing only the ``config`` mapping.

    ``ProductionConfig.init_app`` interacts with the app exclusively
    through ``app.config.get(...)`` / ``app.config[...] = ...``, so a
    plain ``dict`` is a faithful stand-in. Using a stub avoids spinning
    up a Flask test client per test, which keeps the suite fast and
    independent of the larger app factory.
    """

    def __init__(self, **overrides: Any) -> None:
        # Default to a fully-valid production config, then apply any
        # per-test overrides on top. Tests use this pattern to express
        # "production with X invalid" without restating the entire
        # baseline.
        baseline: dict[str, Any] = {
            # 48-byte URL-safe base64 token; 64 raw bytes after decoding.
            "JWT_SIGNING_KEY": "Gx5kV9dCk9PqZj7tH3w2nR4uYbXa1mLsTzEoAhQpVwIcMdNeRfFb",
            "ANTHROPIC_API_KEY": "sk-ant-api03-real-test-token-for-unit-tests-only-not-real",
            "GOOGLE_OAUTH_CLIENT_SECRET": "GOCSPX-real-test-client-secret-for-unit-tests-only",
            "DATABASE_URL": (
                "postgresql+psycopg://app:realpw@db.internal.example.com:5432/sales_connections"
            ),
            # Secrets-Manager bypass so init_app does not try to call AWS.
            "USE_SECRETS_MANAGER": False,
            # Region needed only when USE_SECRETS_MANAGER=True; included
            # for symmetry and so any code path that reads it gets a
            # plausible value.
            "AWS_REGION": "us-east-1",
        }
        baseline.update(overrides)
        self.config: dict[str, Any] = baseline


# ---------------------------------------------------------------------------
# Helper-function tests
# ---------------------------------------------------------------------------


class TestIsPlaceholderOrEmpty:
    """Tests for the ``_is_placeholder_or_empty`` helper.

    The helper is the single decision point that determines whether a
    secret value is "real" or "still a placeholder", so its behavior is
    pinned with explicit examples.
    """

    @pytest.mark.parametrize(
        "value",
        [
            None,
            "",
            "   ",
            "\t\n",
            "PLACEHOLDER_ANTHROPIC_API_KEY",
            "PLACEHOLDER_X",
            "  PLACEHOLDER_LEADING_WHITESPACE",
            "REPLACE_WITH_REAL_VALUE",
            "  REPLACE_WITH_X  ",
        ],
    )
    def test_invalid_values_return_true(self, value: str | None) -> None:
        """``None``, empty/whitespace, or placeholder prefixes are placeholders."""
        assert _is_placeholder_or_empty(value) is True

    @pytest.mark.parametrize(
        "value",
        [
            "sk-ant-api03-real-token",
            "GOCSPX-real-google-oauth-secret",
            "postgresql://user:pass@host:5432/db",
            "x" * 64,
            "Replaced_with_a_real_value",  # case matters; lower-case prefix is real
            "placeholder_lower_case_is_not_a_sentinel",  # case matters
        ],
    )
    def test_valid_values_return_false(self, value: str) -> None:
        """Real secret values (not matching prefixes) are accepted."""
        assert _is_placeholder_or_empty(value) is False


# ---------------------------------------------------------------------------
# ProductionConfig fail-fast tests
# ---------------------------------------------------------------------------


class TestProductionConfigFailFast:
    """End-to-end tests for ``ProductionConfig.init_app`` validation.

    Each test constructs a stub Flask-like app whose ``config`` reflects
    a specific misconfiguration and asserts that ``init_app`` raises
    ``RuntimeError`` with a message that NAMES the offending secret.
    """

    def test_full_valid_config_does_not_raise(self) -> None:
        """A fully-populated production config initializes without error."""
        app = _StubApp()
        # Should not raise.
        ProductionConfig.init_app(app)

    @pytest.mark.parametrize(
        "key",
        [
            "ANTHROPIC_API_KEY",
            "GOOGLE_OAUTH_CLIENT_SECRET",
            "JWT_SIGNING_KEY",
            "DATABASE_URL",
        ],
    )
    def test_empty_required_secret_raises(self, key: str) -> None:
        """Each of the four required secrets MUST raise when empty."""
        app = _StubApp(**{key: ""})
        with pytest.raises(RuntimeError, match=key):
            ProductionConfig.init_app(app)

    @pytest.mark.parametrize(
        "key",
        [
            "ANTHROPIC_API_KEY",
            "GOOGLE_OAUTH_CLIENT_SECRET",
            "JWT_SIGNING_KEY",
            "DATABASE_URL",
        ],
    )
    def test_placeholder_required_secret_raises(self, key: str) -> None:
        """Each required secret MUST raise when still set to PLACEHOLDER_*."""
        app = _StubApp(**{key: f"PLACEHOLDER_{key}"})
        with pytest.raises(RuntimeError, match=key):
            ProductionConfig.init_app(app)

    @pytest.mark.parametrize(
        "key",
        [
            "ANTHROPIC_API_KEY",
            "GOOGLE_OAUTH_CLIENT_SECRET",
            "JWT_SIGNING_KEY",
            "DATABASE_URL",
        ],
    )
    def test_replace_with_required_secret_raises(self, key: str) -> None:
        """Each required secret MUST raise when set to REPLACE_WITH_*."""
        app = _StubApp(**{key: f"REPLACE_WITH_{key}"})
        with pytest.raises(RuntimeError, match=key):
            ProductionConfig.init_app(app)

    @pytest.mark.parametrize(
        "key",
        [
            "ANTHROPIC_API_KEY",
            "GOOGLE_OAUTH_CLIENT_SECRET",
            "JWT_SIGNING_KEY",
            "DATABASE_URL",
        ],
    )
    def test_whitespace_required_secret_raises(self, key: str) -> None:
        """Whitespace-only values are treated as empty."""
        app = _StubApp(**{key: "   \t\n  "})
        with pytest.raises(RuntimeError, match=key):
            ProductionConfig.init_app(app)

    def test_missing_config_key_raises(self) -> None:
        """Removing a required key entirely (None lookup) raises."""
        app = _StubApp()
        del app.config["ANTHROPIC_API_KEY"]
        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
            ProductionConfig.init_app(app)

    def test_aggregated_error_names_every_missing_secret(self) -> None:
        """A single RuntimeError lists every misconfigured secret at once.

        Operators should see all problems in one go instead of fixing
        them one-by-one across multiple restart attempts.
        """
        app = _StubApp(
            ANTHROPIC_API_KEY="",
            GOOGLE_OAUTH_CLIENT_SECRET="PLACEHOLDER_X",
            DATABASE_URL="REPLACE_WITH_DSN",
            JWT_SIGNING_KEY="",
        )
        with pytest.raises(RuntimeError) as exc_info:
            ProductionConfig.init_app(app)
        message = str(exc_info.value)
        # Every misconfigured key MUST appear in the aggregated error.
        assert "ANTHROPIC_API_KEY" in message
        assert "GOOGLE_OAUTH_CLIENT_SECRET" in message
        assert "DATABASE_URL" in message
        assert "JWT_SIGNING_KEY" in message
        # The error names the file operators should consult.
        assert "backend/.env.example" in message

    def test_use_secrets_manager_false_still_validates(self) -> None:
        """USE_SECRETS_MANAGER=False does not bypass fail-fast checks.

        Hybrid environments (staging, dev-against-prod) inject secrets
        via env vars rather than AWS Secrets Manager; the validation
        invariant must still apply.
        """
        app = _StubApp(USE_SECRETS_MANAGER=False, JWT_SIGNING_KEY="")
        with pytest.raises(RuntimeError, match="JWT_SIGNING_KEY"):
            ProductionConfig.init_app(app)

    def test_error_message_does_not_leak_secret_value(self) -> None:
        """The RuntimeError message must not echo the (placeholder) value.

        Even though placeholders are not real secrets, leaking them in
        logs would set a precedent that real secrets might also be
        echoed. The error message lists keys, not values.
        """
        sentinel_value = "SUPER_SENSITIVE_VALUE_THAT_LOOKS_REAL"
        # Even a value that does not match the placeholder prefix is
        # rejected by length/empty checks if explicitly empty. To make
        # the value appear in the config but trigger an error, set
        # JWT_SIGNING_KEY too short.
        app = _StubApp(JWT_SIGNING_KEY="too_short")
        with pytest.raises(RuntimeError) as exc_info:
            ProductionConfig.init_app(app)
        message = str(exc_info.value)
        # The literal short value should NOT be echoed to the operator.
        assert "too_short" not in message
        # And neither should any pretend "real" value.
        assert sentinel_value not in message


# ---------------------------------------------------------------------------
# JWT_SIGNING_KEY length validation tests (Finding #2)
# ---------------------------------------------------------------------------


class TestJwtSigningKeyLength:
    """Tests for the JWT_SIGNING_KEY 32-byte minimum length invariant."""

    def test_minimum_byte_length_is_32(self) -> None:
        """Sanity-check the constant value matches AAP Sec 0.7.4."""
        assert _JWT_SIGNING_KEY_MIN_BYTES == 32

    @pytest.mark.parametrize(
        "key",
        [
            # 31 ASCII chars = 31 bytes (one byte too short)
            "x" * 31,
            # 16 ASCII chars
            "shorter_key_only",
            # Empty after stripping
            "abc",
        ],
    )
    def test_short_jwt_key_raises(self, key: str) -> None:
        """JWT keys under 32 bytes raise RuntimeError with a specific message."""
        app = _StubApp(JWT_SIGNING_KEY=key)
        with pytest.raises(RuntimeError, match="JWT_SIGNING_KEY"):
            ProductionConfig.init_app(app)

    def test_jwt_key_exactly_32_bytes_passes(self) -> None:
        """32 bytes is the minimum; values at the floor MUST be accepted."""
        # 32 ASCII chars = 32 bytes
        app = _StubApp(JWT_SIGNING_KEY="x" * 32)
        # Should not raise.
        ProductionConfig.init_app(app)

    def test_jwt_key_well_above_floor_passes(self) -> None:
        """Comfortably long keys are accepted."""
        app = _StubApp(JWT_SIGNING_KEY="x" * 64)
        ProductionConfig.init_app(app)

    def test_jwt_key_byte_length_uses_utf8_not_char_count(self) -> None:
        """A 16-character UTF-8 string of multi-byte chars MAY meet 32 bytes.

        Documents the encode('utf-8') choice: a string with 16 chars of
        4-byte emoji satisfies the byte-length check (16 * 4 = 64 bytes
        > 32). Conversely, a 31-character ASCII string is 31 bytes and
        fails the check. The byte-length basis is correct because PyJWT
        signs over the UTF-8 encoded key bytes, not Python's logical
        character count.
        """
        # 8 four-byte chars = 32 bytes (just at the floor)
        # The grinning face emoji U+1F600 encodes to 4 bytes in UTF-8.
        eight_emoji = "\U0001f600" * 8
        assert len(eight_emoji) == 8
        assert len(eight_emoji.encode("utf-8")) == 32
        app = _StubApp(JWT_SIGNING_KEY=eight_emoji)
        ProductionConfig.init_app(app)

        # 7 four-byte chars = 28 bytes (under the floor)
        seven_emoji = "\U0001f600" * 7
        assert len(seven_emoji.encode("utf-8")) == 28
        app2 = _StubApp(JWT_SIGNING_KEY=seven_emoji)
        with pytest.raises(RuntimeError, match="JWT_SIGNING_KEY"):
            ProductionConfig.init_app(app2)

    def test_placeholder_jwt_key_does_not_trigger_length_error(self) -> None:
        """A placeholder JWT key triggers the placeholder error, not length.

        Even though a placeholder string may happen to be longer than
        32 bytes, the placeholder check fires first because it is the
        more actionable error for the operator (the fix is "rotate the
        secret", not "make it longer").
        """
        # Placeholder is 50+ bytes long but should still raise on placeholder.
        placeholder = "PLACEHOLDER_JWT_SIGNING_KEY_" + "x" * 32
        assert len(placeholder.encode("utf-8")) >= 32
        app = _StubApp(JWT_SIGNING_KEY=placeholder)
        with pytest.raises(RuntimeError) as exc_info:
            ProductionConfig.init_app(app)
        message = str(exc_info.value)
        # The placeholder error names the placeholder as the cause; the
        # length error mentions "32 bytes". Only the placeholder error
        # should fire.
        assert "PLACEHOLDER_" in message or "placeholder" in message
        assert "32 bytes for HS256 security" not in message


# ---------------------------------------------------------------------------
# Configuration smoke tests (development/testing do NOT fail-fast)
# ---------------------------------------------------------------------------


class TestNonProductionConfigsDoNotFailFast:
    """DevelopmentConfig and TestingConfig must boot without prod secrets.

    The fail-fast invariant applies to ProductionConfig only; local
    development must remain frictionless even when the contributor has
    not provisioned an Anthropic key (per AAP Sec 0.0 — AI failures are
    non-blocking) or a real OAuth client (per the email/password
    fallback in F-012).
    """

    def test_base_config_init_app_is_noop(self) -> None:
        """BaseConfig.init_app does not validate; subclasses opt in."""
        app = _StubApp(
            ANTHROPIC_API_KEY="",
            GOOGLE_OAUTH_CLIENT_SECRET="",
            JWT_SIGNING_KEY="",
            DATABASE_URL="",
        )
        # BaseConfig is intentionally permissive.
        BaseConfig.init_app(app)

    def test_development_config_init_app_is_noop(self) -> None:
        """DevelopmentConfig does not run production fail-fast checks."""
        app = _StubApp(
            ANTHROPIC_API_KEY="",
            GOOGLE_OAUTH_CLIENT_SECRET="",
            JWT_SIGNING_KEY="change-me-jwt",  # short and dev-only
            DATABASE_URL="",
        )
        # DevelopmentConfig is intentionally permissive (developers
        # iterate locally without real secrets).
        DevelopmentConfig.init_app(app)

    def test_testing_config_init_app_is_noop(self) -> None:
        """TestingConfig does not run production fail-fast checks."""
        app = _StubApp(
            ANTHROPIC_API_KEY="",
            GOOGLE_OAUTH_CLIENT_SECRET="",
            JWT_SIGNING_KEY="test-jwt-signing-key-do-not-use-in-prod",
            DATABASE_URL="",
        )
        TestingConfig.init_app(app)
