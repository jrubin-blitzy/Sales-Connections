"""Tests for ``app.observability.logging.redact_secrets_processor``.

Per the user's Explainability rule and AAP §0.7.4, structured logs
must redact credential-bearing keys before rendering. The QA
Checkpoint 10 (Issue 1) widening covered six new variants:

- ``aws_secret_access_key`` (AWS standard naming)
- ``db_password`` (substring on ``password``)
- ``user_password`` (substring on ``password``)
- ``password_hash`` (bcrypt-bearing field name)
- ``Bearer`` (raw Bearer token values)
- ``cookie`` / ``set_cookie`` / ``set-cookie`` (raw HTTP cookie
  header payloads)

This module exercises every pattern the redaction regex supports and
locks in the QA Issue 13 narrowing (no false-positive redaction of
benign ``sort_key`` / ``cache_key`` / ``cursor_key`` identifiers).
A failure in any of these cases is a regression that operators
would notice as either a credential leak (false-negative) or an
operationally noisy log (false-positive on benign keys).

The tests run the processor in isolation against a hand-built
``event_dict`` so they are fast (no Flask app context, no DB) and
robust against unrelated changes in the surrounding logging
configuration.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.observability.logging import _REDACTED_PLACEHOLDER, redact_secrets_processor


def _redact(event: dict[str, Any]) -> dict[str, Any]:
    """Run the redaction processor against ``event`` and return the result.

    Mirrors the structlog processor protocol: the ``logger`` and
    ``method_name`` arguments are unused by the implementation.
    """
    return redact_secrets_processor(None, "info", event)


class TestRedactSecretsProcessor:
    """Behavior of ``redact_secrets_processor``."""

    # -------------------------------------------------------------
    # QA Checkpoint 10 Issue 1: widened pattern catches compound
    # credential names that the previous fullmatch-only regex
    # missed.
    # -------------------------------------------------------------

    def test_aws_secret_access_key_is_redacted(self) -> None:
        """AWS standard naming is redacted via the ``.*secret.*`` pattern."""
        out = _redact({"event": "boot", "aws_secret_access_key": "AKIA-EXAMPLE"})
        assert out["aws_secret_access_key"] == _REDACTED_PLACEHOLDER

    def test_db_password_is_redacted(self) -> None:
        """Compound ``db_password`` is redacted via the ``.*password.*`` pattern."""
        out = _redact({"event": "boot", "db_password": "super-secret"})
        assert out["db_password"] == _REDACTED_PLACEHOLDER

    def test_user_password_is_redacted(self) -> None:
        """Compound ``user_password`` is redacted via the ``.*password.*`` pattern."""
        out = _redact({"event": "auth", "user_password": "hunter2"})
        assert out["user_password"] == _REDACTED_PLACEHOLDER

    def test_password_hash_is_redacted(self) -> None:
        """``password_hash`` (bcrypt) is redacted via the ``.*password.*`` pattern."""
        out = _redact({"event": "user_loaded", "password_hash": "$2b$12$abcdef"})
        assert out["password_hash"] == _REDACTED_PLACEHOLDER

    def test_bearer_is_redacted_case_insensitive(self) -> None:
        """``Bearer`` and ``bearer`` are redacted (case-insensitive match)."""
        out = _redact({"event": "h", "Bearer": "BEARER-XYZ"})
        assert out["Bearer"] == _REDACTED_PLACEHOLDER
        out = _redact({"event": "h", "bearer": "BEARER-XYZ"})
        assert out["bearer"] == _REDACTED_PLACEHOLDER

    def test_cookie_is_redacted(self) -> None:
        """Raw HTTP ``cookie`` payload is redacted."""
        out = _redact({"event": "h", "cookie": "session=abc; Path=/"})
        assert out["cookie"] == _REDACTED_PLACEHOLDER

    def test_set_cookie_underscore_is_redacted(self) -> None:
        """``set_cookie`` form is redacted."""
        out = _redact({"event": "h", "set_cookie": "session=abc; HttpOnly"})
        assert out["set_cookie"] == _REDACTED_PLACEHOLDER

    def test_set_cookie_dash_is_redacted(self) -> None:
        """``set-cookie`` form is redacted (dash variant)."""
        out = _redact({"event": "h", "set-cookie": "session=abc; HttpOnly"})
        assert out["set-cookie"] == _REDACTED_PLACEHOLDER

    # -------------------------------------------------------------
    # AAP §0.7.4 baseline: pre-existing canonical credential names.
    # -------------------------------------------------------------

    def test_password_is_redacted(self) -> None:
        """Bare ``password`` is redacted."""
        out = _redact({"event": "auth", "password": "QATest1234!"})
        assert out["password"] == _REDACTED_PLACEHOLDER

    def test_authorization_header_is_redacted(self) -> None:
        """``authorization`` header value is redacted."""
        out = _redact({"event": "h", "authorization": "Bearer xyz"})
        assert out["authorization"] == _REDACTED_PLACEHOLDER

    def test_token_is_redacted(self) -> None:
        """Bare ``token`` is redacted."""
        out = _redact({"event": "auth", "token": "JWT-XYZ"})
        assert out["token"] == _REDACTED_PLACEHOLDER

    def test_access_token_is_redacted(self) -> None:
        """Compound ``*_token`` is redacted."""
        out = _redact({"event": "auth", "access_token": "JWT-XYZ"})
        assert out["access_token"] == _REDACTED_PLACEHOLDER

    @pytest.mark.parametrize(
        ("key_name", "expected_redacted"),
        [
            ("api_key", True),
            ("api-key", True),
            ("apikey", True),
            ("ANTHROPIC_API_KEY", True),
            ("google_api_key", True),
        ],
    )
    def test_api_key_variants_are_redacted(
        self,
        key_name: str,
        expected_redacted: bool,
    ) -> None:
        """All ``api_key`` / ``api-key`` / ``apikey`` variants are redacted."""
        out = _redact({"event": "config_loaded", key_name: "sk-XXX"})
        if expected_redacted:
            assert out[key_name] == _REDACTED_PLACEHOLDER

    @pytest.mark.parametrize(
        "narrow_key",
        [
            "signing_key",
            "secret_key",
            "private_key",
            "encryption_key",
            "master_key",
            "session_key",
        ],
    )
    def test_narrow_key_allowlist_is_redacted(self, narrow_key: str) -> None:
        """Each ``*_key`` literal in the narrow allowlist is redacted."""
        out = _redact({"event": "boot", narrow_key: "VALUE"})
        assert out[narrow_key] == _REDACTED_PLACEHOLDER

    @pytest.mark.parametrize(
        "secret_key",
        [
            "secret",
            "client_secret",
            "api_secret",
            "shared_secret",
            "webhook_secret",
        ],
    )
    def test_any_secret_substring_is_redacted(self, secret_key: str) -> None:
        """Any key containing the substring ``secret`` is redacted."""
        out = _redact({"event": "h", secret_key: "VALUE"})
        assert out[secret_key] == _REDACTED_PLACEHOLDER

    # -------------------------------------------------------------
    # QA Issue 13 narrowing: benign ``*_key`` identifiers MUST NOT
    # be redacted (otherwise operators lose forensic context).
    # -------------------------------------------------------------

    @pytest.mark.parametrize(
        "benign_key",
        [
            "sort_key",
            "cache_key",
            "partition_key",
            "cursor_key",
            "tag_key",
            "lookup_key",
            "redis_key",
        ],
    )
    def test_benign_key_identifiers_are_not_redacted(self, benign_key: str) -> None:
        """Operationally helpful ``*_key`` identifiers are NOT redacted."""
        sentinel = "VISIBLE-VALUE-1234"
        out = _redact({"event": "h", benign_key: sentinel})
        assert out[benign_key] == sentinel, (
            f"{benign_key!r} should NOT be redacted (QA Issue 13 narrowing)"
        )

    # -------------------------------------------------------------
    # Recursion / nested structures.
    # -------------------------------------------------------------

    def test_nested_dict_credentials_are_redacted(self) -> None:
        """Credentials in nested dicts are redacted up to the bounded depth."""
        event = {
            "event": "config",
            "config": {
                "db": {
                    "host": "localhost",
                    "db_password": "super-secret",
                },
            },
        }
        out = _redact(event)
        assert out["config"]["db"]["db_password"] == _REDACTED_PLACEHOLDER
        assert out["config"]["db"]["host"] == "localhost"

    def test_list_of_dicts_credentials_are_redacted(self) -> None:
        """Credentials inside a list of dicts are redacted."""
        event = {
            "event": "users",
            "items": [
                {"id": 1, "password_hash": "$2b$12$h1"},
                {"id": 2, "password_hash": "$2b$12$h2"},
            ],
        }
        out = _redact(event)
        assert out["items"][0]["password_hash"] == _REDACTED_PLACEHOLDER
        assert out["items"][1]["password_hash"] == _REDACTED_PLACEHOLDER
        assert out["items"][0]["id"] == 1
        assert out["items"][1]["id"] == 2

    # -------------------------------------------------------------
    # Non-credential values pass through untouched.
    # -------------------------------------------------------------

    def test_event_field_passes_through(self) -> None:
        """The ``event`` key itself is NEVER redacted."""
        out = _redact({"event": "user_login_succeeded"})
        assert out["event"] == "user_login_succeeded"

    @pytest.mark.parametrize(
        "benign_key",
        [
            "user_id",
            "org_id",
            "correlation_id",
            "trace_id",
            "span_id",
            "email",
            "display_name",
            "company",
            "outcome",
        ],
    )
    def test_benign_application_fields_pass_through(self, benign_key: str) -> None:
        """Application-domain fields used in structured logs pass through."""
        out = _redact({"event": "h", benign_key: "value-123"})
        assert out[benign_key] == "value-123"

    # -------------------------------------------------------------
    # Defensive edge cases.
    # -------------------------------------------------------------

    def test_empty_event_dict_handled(self) -> None:
        """An empty event dict is returned unchanged."""
        out = _redact({})
        assert out == {}

    def test_none_value_is_not_redacted(self) -> None:
        """A ``None`` value for a credential key is replaced (still safe)."""
        # The processor unconditionally substitutes the placeholder
        # for matching keys; this is acceptable defensive behavior
        # and locked in here so future contributors don't accidentally
        # introduce a "skip None" optimization that would create a
        # value-presence side channel.
        out = _redact({"event": "h", "password": None})
        assert out["password"] == _REDACTED_PLACEHOLDER
