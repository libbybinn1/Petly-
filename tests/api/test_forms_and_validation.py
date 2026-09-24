"""API tests for forms, validation and business-rule errors (spec section 21).

Spec section 21 requires validation at both the UI and the server. The
browser's half is a convenience; these tests exercise the half that actually
protects the data, by posting what a browser would have refused to send.
"""

from __future__ import annotations

import pytest
from app.domain.enums import AnimalStatus
from app.infrastructure.models import AdopterProfile, AdoptionApplication, AnalysisJob, User
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tests.api.conftest import PROFILELESS_EMAIL, sign_in

pytestmark = pytest.mark.api


VALID_PROFILE_FORM = {
    "home_type": "APARTMENT",
    "experience_level": "NONE",
    "activity_level": "LOW",
    "daily_hours_available": "2",
    "city": "Tel Aviv",
}


class TestRegistration:
    """The registration form validates server-side."""

    def test_valid_registration_creates_an_account(
        self, client: FlaskClient, world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the happy path actually persists a user."""
        response = client.post(
            "/register",
            data={
                "full_name": "New Person",
                "email": "brand.new@petmatch.test",
                "password": "LongEnough1",
                "confirm_password": "LongEnough1",
            },
        )

        assert response.status_code == 302
        with session_factory() as session:
            created = session.execute(
                select(User).where(User.email == "brand.new@petmatch.test")
            ).scalar_one_or_none()
            assert created is not None
            assert created.role == "ADOPTER"
            # The password must not be recoverable from what was stored.
            assert created.password_hash != "LongEnough1"
            assert "LongEnough1" not in created.password_hash

    def test_short_password_is_rejected(
        self, client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves the length rule is enforced by the server, not just `minlength`."""
        response = client.post(
            "/register",
            data={
                "full_name": "Short Pass",
                "email": "short@petmatch.test",
                "password": "abc",
                "confirm_password": "abc",
            },
        )
        assert response.status_code == 400

    def test_mismatched_confirmation_is_rejected(
        self, client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves the two password fields must agree."""
        response = client.post(
            "/register",
            data={
                "full_name": "Mismatch",
                "email": "mismatch@petmatch.test",
                "password": "LongEnough1",
                "confirm_password": "LongEnough2",
            },
        )
        assert response.status_code == 400

    def test_duplicate_email_is_rejected(
        self, client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves an address cannot be registered twice."""
        response = client.post(
            "/register",
            data={
                "full_name": "Duplicate",
                "email": "adopter@petmatch.test",
                "password": "LongEnough1",
                "confirm_password": "LongEnough1",
            },
        )
        assert response.status_code == 409

    def test_registration_cannot_grant_itself_staff(
        self, client: FlaskClient, world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves an extra form field cannot escalate privilege.

        The handler assigns ADOPTER unconditionally rather than reading a
        role from the request, so a crafted POST gains nothing.
        """
        client.post(
            "/register",
            data={
                "full_name": "Sneaky",
                "email": "sneaky@petmatch.test",
                "password": "LongEnough1",
                "confirm_password": "LongEnough1",
                "role": "STAFF",
            },
        )

        with session_factory() as session:
            created = session.execute(
                select(User).where(User.email == "sneaky@petmatch.test")
            ).scalar_one_or_none()
            assert created is not None
            assert created.role == "ADOPTER"


class TestProfileForm:
    """The adopter profile form (spec section 5.1)."""

    def test_valid_profile_is_saved_and_marked_complete(
        self, application: Flask, world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves a newcomer can create a profile that unlocks matching."""
        test_client = application.test_client()
        sign_in(test_client, PROFILELESS_EMAIL)

        response = test_client.post("/my/profile", data=VALID_PROFILE_FORM)
        assert response.status_code == 302

        with session_factory() as session:
            profile = session.execute(
                select(AdopterProfile).where(
                    AdopterProfile.user_id == world["newcomer_user_id"]
                )
            ).scalar_one()
            assert profile.is_complete is True
            assert profile.city == "Tel Aviv"

    def test_missing_required_field_is_rejected(
        self, application: Flask, world: dict[str, str]
    ) -> None:
        """Proves the server refuses an incomplete profile."""
        test_client = application.test_client()
        sign_in(test_client, PROFILELESS_EMAIL)

        incomplete = dict(VALID_PROFILE_FORM)
        del incomplete["city"]

        assert test_client.post("/my/profile", data=incomplete).status_code == 400

    def test_children_without_an_age_is_rejected(
        self, application: Flask, world: dict[str, str]
    ) -> None:
        """Proves the dependent-field rule is enforced over HTTP."""
        test_client = application.test_client()
        sign_in(test_client, PROFILELESS_EMAIL)

        form = {**VALID_PROFILE_FORM, "household_has_children": "1"}
        assert test_client.post("/my/profile", data=form).status_code == 400

    def test_opt_in_defaults_to_off_when_the_box_is_absent(
        self, application: Flask, world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves consent is never inferred from a missing field.

        An unticked checkbox is simply absent from a form submission, so a
        handler that treated "absent" as "unspecified, assume yes" would
        opt people in silently.
        """
        test_client = application.test_client()
        sign_in(test_client, PROFILELESS_EMAIL)
        test_client.post("/my/profile", data=VALID_PROFILE_FORM)

        with session_factory() as session:
            profile = session.execute(
                select(AdopterProfile).where(
                    AdopterProfile.user_id == world["newcomer_user_id"]
                )
            ).scalar_one()
            assert profile.open_to_proactive_suggestions is False

    def test_opt_in_is_recorded_when_ticked(
        self, application: Flask, world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves a deliberate opt-in is stored."""
        test_client = application.test_client()
        sign_in(test_client, PROFILELESS_EMAIL)
        test_client.post(
            "/my/profile",
            data={**VALID_PROFILE_FORM, "open_to_proactive_suggestions": "1"},
        )

        with session_factory() as session:
            profile = session.execute(
                select(AdopterProfile).where(
                    AdopterProfile.user_id == world["newcomer_user_id"]
                )
            ).scalar_one()
            assert profile.open_to_proactive_suggestions is True


class TestApplicationSubmission:
    """Applying for an animal, and the rules that refuse it."""

    def test_application_is_created_and_queues_an_analysis(
        self, adopter_client: FlaskClient, world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the happy path writes the application *and* the agent job.

        Asserted positively, because the ownership tests elsewhere check only
        that the other adopter cannot see it - which would also pass if
        applying silently did nothing.
        """
        response = adopter_client.post(
            f"/my/apply/{world['available_animal_id']}",
            data={"applicant_message": "We would love to meet Clover."},
        )
        assert response.status_code == 302

        with session_factory() as session:
            application = session.execute(
                select(AdoptionApplication).where(
                    AdoptionApplication.animal_id == world["available_animal_id"]
                )
            ).scalar_one()
            assert application.adopter_profile_id == world["adopter_profile_id"]
            assert application.status == "SUBMITTED"

            job = session.execute(
                select(AnalysisJob).where(
                    AnalysisJob.application_id == application.application_id
                )
            ).scalar_one()
            assert job.status == "PENDING"

    def test_applicant_sees_their_own_application(
        self, adopter_client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves the application reaches the adopter's own list."""
        adopter_client.post(
            f"/my/apply/{world['available_animal_id']}",
            data={"applicant_message": "Mine"},
        )

        listing = adopter_client.get("/my/applications")
        assert b"Clover" in listing.data

    def test_cannot_apply_for_an_adopted_animal(
        self, adopter_client: FlaskClient, world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves an unavailable animal refuses applications."""
        adopter_client.post(
            f"/my/apply/{world['adopted_animal_id']}", follow_redirects=True
        )

        with session_factory() as session:
            created = session.execute(
                select(AdoptionApplication).where(
                    AdoptionApplication.animal_id == world["adopted_animal_id"]
                )
            ).first()
            assert created is None

    def test_duplicate_application_is_refused(
        self, adopter_client: FlaskClient, world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves an adopter cannot hold two live applications for one animal."""
        for _ in range(2):
            adopter_client.post(
                f"/my/apply/{world['available_animal_id']}", follow_redirects=True
            )

        with session_factory() as session:
            applications = session.execute(
                select(AdoptionApplication).where(
                    AdoptionApplication.animal_id == world["available_animal_id"]
                )
            ).scalars().all()
            assert len(applications) == 1

    def test_adopter_without_a_profile_is_sent_to_the_form(
        self, application: Flask, world: dict[str, str]
    ) -> None:
        """Proves applying requires a completed profile."""
        test_client = application.test_client()
        sign_in(test_client, PROFILELESS_EMAIL)

        response = test_client.post(f"/my/apply/{world['available_animal_id']}")

        assert response.status_code == 302
        assert "/my/profile" in response.headers["Location"]


class TestNotFoundHandling:
    """Unknown identifiers produce 404, not a crash."""

    def test_unknown_animal_detail_is_404(
        self, client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves a bad identifier in a URL is handled."""
        assert client.get("/animals/does-not-exist").status_code == 404

    def test_unknown_animal_ranking_is_404(
        self, staff_client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves the staff ranking pages handle a bad identifier."""
        assert staff_client.get("/animals/nope/adopters").status_code == 404

    def test_unknown_analysis_is_404(
        self, staff_client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves the analysis page handles a bad identifier."""
        assert staff_client.get("/analyses/nope").status_code == 404


class TestSearchFilters:
    """Search handles every filter and malformed input."""

    def test_checkbox_filters_execute(
        self, client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves the boolean filters run against the database.

        These were broken for a while by an `IS 1` predicate that SQL Server
        rejects, and stayed hidden because nothing exercised them.
        """
        response = client.get(
            "/animals/?good_with_children=1&good_with_other_animals=1"
        )
        assert response.status_code == 200
        assert b"Clover" in response.data

    def test_every_filter_combination_is_accepted(
        self, client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves the full filter set does not error."""
        response = client.get(
            "/animals/?q=rabbit&species=RABBIT&size=SMALL&activity_level=LOW"
            "&good_with_children=1&include_unavailable=1"
        )
        assert response.status_code == 200

    def test_invalid_page_number_falls_back(
        self, client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves a nonsense page parameter does not produce a 500."""
        assert client.get("/animals/?page=abc").status_code == 200
        assert client.get("/animals/?page=-5").status_code == 200

    def test_unknown_species_filter_returns_no_results_rather_than_erroring(
        self, client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves an unrecognised filter value is handled gracefully."""
        response = client.get("/animals/?species=DRAGON")
        assert response.status_code == 200


class TestAnimalStatusVisibility:
    """Availability governs what an adopter can act on."""

    def test_adopted_animal_is_hidden_from_default_search(
        self, client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves search shows available animals by default."""
        response = client.get("/animals/")
        assert b"Clover" in response.data
        assert b"Gus" not in response.data

    def test_adopted_animal_appears_when_explicitly_included(
        self, client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves staff can still find non-available animals."""
        response = client.get("/animals/?include_unavailable=1")
        assert b"Gus" in response.data

    def test_adopted_animal_details_remain_viewable(
        self, client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves an adopted animal's page still loads.

        Hiding it from search is a listing decision; a direct link, for
        instance from an old application, must not 404.
        """
        response = client.get(f"/animals/{world['adopted_animal_id']}")
        assert response.status_code == 200
        assert AnimalStatus.ADOPTED.value.encode() in response.data.upper()


class TestPaginationIsClampedTheSameWayEverywhere:
    """Both listings paginate, and each used to clamp differently.

    The staff table floored the page at one; the public search clamped only
    the computed offset, so `?page=0` rendered page one's rows underneath a
    pager that reported page 0 - with "previous" enabled and pointing at
    page -1.
    """

    def test_a_zero_page_reads_as_the_first_page(
        self, application: Flask, world: dict[str, str]
    ) -> None:
        """Proves the reported page is the one that was actually shown."""
        from app.cqrs.queries.animal_queries import AnimalSearchFilters, SearchAnimalsQuery

        results = application.config["BUS"].dispatch_query(
            SearchAnimalsQuery(filters=AnimalSearchFilters(), page=0)
        )

        assert results.page == 1
        assert results.has_previous is False

    def test_a_negative_page_size_cannot_reach_the_query(
        self, application: Flask, world: dict[str, str]
    ) -> None:
        """Proves a forged page size is floored rather than passed to LIMIT.

        Negative half of the pair: a negative LIMIT is a database error on
        one engine and an unbounded read on another.
        """
        from app.cqrs.queries.animal_queries import AnimalSearchFilters, SearchAnimalsQuery

        results = application.config["BUS"].dispatch_query(
            SearchAnimalsQuery(filters=AnimalSearchFilters(), page=1, page_size=-10)
        )

        assert results.page_size == 1
        assert len(results.animals) <= 1
