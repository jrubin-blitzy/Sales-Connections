"""Tests for the F-010 duplicate-LinkedIn-URL detection service.

Covers:
    - find_duplicate: low-level lookup with org-scoping and
      soft-delete-aware filtering
    - check_duplicate: high-level entry point with URL validation,
      normalization, and ConnectionDuplicateCheckResponse shaping
    - exclude_record_id parameter for the edit flow
    - normalization edge cases (trailing slash, query params, etc.)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from app.middleware.auth import Session as AuthSession
from app.middleware.error_handlers import ValidationFailedError
from app.services.duplicate_detection import check_duplicate, find_duplicate
from app.utils.url import normalize_linkedin_url

if TYPE_CHECKING:
    from flask import Flask
    from sqlalchemy.orm import Session as DBSession


def _make_actor_session(user) -> AuthSession:
    """Construct an AuthSession from a User for service-layer calls."""
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    now = datetime.now(UTC)
    return AuthSession(
        user_id=user.id,
        org_id=user.org_id,
        role=user.role,
        email=user.email,
        display_name=user.display_name,
        issued_at=now,
        expires_at=now + timedelta(hours=8),
        raw_claims={
            "user_id": str(user.id),
            "org_id": str(user.org_id),
            "role": user.role.value,
            "email": user.email,
            "display_name": user.display_name,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=8)).timestamp()),
        },
    )


# ---------------------------------------------------------------------------
# TestFindDuplicate
# ---------------------------------------------------------------------------


class TestFindDuplicate:
    """Verify the low-level find_duplicate function."""

    def test_returns_record_when_match_exists(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """find_duplicate returns the matching record."""
        from tests.factories import RecordFactory  # noqa: PLC0415

        url = "https://www.linkedin.com/in/jane-doe"
        normalized = normalize_linkedin_url(url)

        record = RecordFactory(
            organization=organization,
            owner=contributor_user,
            linkedin_url=url,
            normalized_linkedin_url=normalized,
        )

        result = find_duplicate(
            organization.id, normalized, db_session=db_session
        )
        assert result is not None
        assert result.id == record.id

    def test_returns_none_when_no_match(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
    ) -> None:
        """find_duplicate returns None when no match."""
        result = find_duplicate(
            organization.id,
            "https://linkedin.com/in/unknown",
            db_session=db_session,
        )
        assert result is None

    def test_returns_none_for_empty_normalized_url(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
    ) -> None:
        """Empty string short-circuits without DB round-trip."""
        result = find_duplicate(organization.id, "", db_session=db_session)
        assert result is None

    def test_excludes_soft_deleted_records(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """Soft-deleted records do NOT count as duplicates (F-010 invariant)."""
        from tests.factories import SoftDeletedRecordFactory  # noqa: PLC0415

        url = "https://www.linkedin.com/in/john-doe"
        normalized = normalize_linkedin_url(url)

        SoftDeletedRecordFactory(
            organization=organization,
            owner=contributor_user,
            linkedin_url=url,
            normalized_linkedin_url=normalized,
        )

        # Soft-deleted record is excluded from duplicate lookup.
        result = find_duplicate(
            organization.id, normalized, db_session=db_session
        )
        assert result is None

    def test_org_scoped_lookup(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """A record in a different org does NOT count as a duplicate."""
        from tests.factories import (  # noqa: PLC0415
            OrganizationFactory,
            RecordFactory,
            UserFactory,
        )

        # Create another org with a record having the same URL.
        other_org = OrganizationFactory()
        other_user = UserFactory(organization=other_org)

        url = "https://www.linkedin.com/in/cross-org"
        normalized = normalize_linkedin_url(url)
        RecordFactory(
            organization=other_org,
            owner=other_user,
            linkedin_url=url,
            normalized_linkedin_url=normalized,
        )

        # Lookup in the original org returns None.
        result = find_duplicate(
            organization.id, normalized, db_session=db_session
        )
        assert result is None

    def test_exclude_record_id_skips_self(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """exclude_record_id makes the edit-flow self-match-free."""
        from tests.factories import RecordFactory  # noqa: PLC0415

        url = "https://www.linkedin.com/in/edit-flow"
        normalized = normalize_linkedin_url(url)

        record = RecordFactory(
            organization=organization,
            owner=contributor_user,
            linkedin_url=url,
            normalized_linkedin_url=normalized,
        )

        # Without exclude_record_id, the record matches.
        result_with_match = find_duplicate(
            organization.id, normalized, db_session=db_session
        )
        assert result_with_match is not None

        # With exclude_record_id, no match.
        result_excluded = find_duplicate(
            organization.id,
            normalized,
            exclude_record_id=record.id,
            db_session=db_session,
        )
        assert result_excluded is None


# ---------------------------------------------------------------------------
# TestCheckDuplicate
# ---------------------------------------------------------------------------


class TestCheckDuplicate:
    """Verify check_duplicate's high-level entry point."""

    def test_returns_response_when_match_exists(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """check_duplicate populates duplicate_found and existing_record_id."""
        from tests.factories import RecordFactory  # noqa: PLC0415

        url = "https://www.linkedin.com/in/known"
        normalized = normalize_linkedin_url(url)
        record = RecordFactory(
            organization=organization,
            owner=contributor_user,
            linkedin_url=url,
            normalized_linkedin_url=normalized,
        )

        actor = _make_actor_session(contributor_user)
        response = check_duplicate(url, actor)

        assert response.duplicate_found is True
        assert response.existing_record_id == record.id
        assert response.normalized_linkedin_url == normalized

    def test_returns_clean_when_no_match(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """check_duplicate returns duplicate_found=False when no match."""
        url = "https://www.linkedin.com/in/never-seen"

        actor = _make_actor_session(contributor_user)
        response = check_duplicate(url, actor)

        assert response.duplicate_found is False
        assert response.existing_record_id is None

    def test_invalid_url_format_raises_validation_error(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """A non-LinkedIn URL is rejected with 422."""
        actor = _make_actor_session(contributor_user)

        with pytest.raises(ValidationFailedError):
            check_duplicate("https://twitter.com/jane", actor)

    def test_empty_string_raises_validation_error(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """An empty URL is rejected."""
        actor = _make_actor_session(contributor_user)

        with pytest.raises(ValidationFailedError):
            check_duplicate("", actor)


# ---------------------------------------------------------------------------
# TestNormalizationEdgeCases
# ---------------------------------------------------------------------------


class TestNormalizationEdgeCases:
    """Verify the duplicate check matches across normalization variants."""

    def test_trailing_slash_variants_match(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """Two URLs differing only by trailing slash are duplicates."""
        from tests.factories import RecordFactory  # noqa: PLC0415

        # Persist with trailing slash.
        url_with_slash = "https://www.linkedin.com/in/jane/"
        normalized = normalize_linkedin_url(url_with_slash)
        RecordFactory(
            organization=organization,
            owner=contributor_user,
            linkedin_url=url_with_slash,
            normalized_linkedin_url=normalized,
        )

        # Check with no trailing slash.
        actor = _make_actor_session(contributor_user)
        response = check_duplicate("https://www.linkedin.com/in/jane", actor)
        assert response.duplicate_found is True

    def test_query_string_stripped(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """A URL with tracking query params still matches the canonical form."""
        from tests.factories import RecordFactory  # noqa: PLC0415

        canonical = "https://www.linkedin.com/in/janewithquery"
        normalized = normalize_linkedin_url(canonical)
        RecordFactory(
            organization=organization,
            owner=contributor_user,
            linkedin_url=canonical,
            normalized_linkedin_url=normalized,
        )

        # Query params are stripped during normalization.
        actor = _make_actor_session(contributor_user)
        response = check_duplicate(
            "https://www.linkedin.com/in/janewithquery?utm_source=email",
            actor,
        )
        assert response.duplicate_found is True

    def test_case_normalization_works(
        self,
        app: Flask,
        db_session: DBSession,
        organization,
        contributor_user,
    ) -> None:
        """Case differences in scheme/host don't bypass duplicate detection."""
        from tests.factories import RecordFactory  # noqa: PLC0415

        canonical = "https://www.linkedin.com/in/casetest"
        normalized = normalize_linkedin_url(canonical)
        RecordFactory(
            organization=organization,
            owner=contributor_user,
            linkedin_url=canonical,
            normalized_linkedin_url=normalized,
        )

        # Mixed-case host normalizes to lower-case host.
        actor = _make_actor_session(contributor_user)
        response = check_duplicate(
            "https://WWW.LINKEDIN.COM/in/casetest",
            actor,
        )
        # Note: path case may or may not be normalized; the normalizer
        # documents what it does. We assert based on observable behavior.
        # If the path is case-preserved, this should still match because
        # the actual path 'casetest' is already lowercase.
        assert response.duplicate_found is True
