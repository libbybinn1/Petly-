"""Tests for the dashboard's actions and for who may read a history (E-8, BG-5).

Spec section 22 asks for an *operational* dashboard: a figure with nowhere to
click is a report, not a dashboard. These check that the tiles carry somewhere
to go, that the agent's queue is visible at all (staff have no window onto the
agent's process, so a stopped worker used to look exactly like an idle one),
and that the attention headlines are written in English rather than with
"(s)".

The history half covers the permission change blueprint section 10 needed: an
adopter whose application was closed by somebody else's approval can now read
how that happened, and still cannot read anybody else's.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.domain.enums import AnalysisJobStatus, AnalysisJobType, ApplicationStatus
from app.infrastructure.models import AdoptionApplication, AnalysisJob, new_identifier
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy.orm import Session, sessionmaker

from tests.api.conftest import PROFILELESS_EMAIL, sign_in

pytestmark = pytest.mark.api


def _naive_now() -> datetime:
    """Naive UTC, as the DATETIME columns store."""
    return datetime.now(UTC).replace(tzinfo=None)


def add_application(
    session_factory: sessionmaker[Session],
    adopter_profile_id: str,
    animal_id: str,
    *,
    days_ago: int = 0,
    applicant_message: str | None = None,
) -> str:
    """Record one submitted application and return its identifier."""
    application_id = new_identifier()
    with session_factory() as session:
        session.add(
            AdoptionApplication(
                application_id=application_id,
                adopter_profile_id=adopter_profile_id,
                animal_id=animal_id,
                status=ApplicationStatus.SUBMITTED.value,
                applicant_message=applicant_message,
                submitted_at=_naive_now() - timedelta(days=days_ago),
            )
        )
        session.commit()
    return application_id


def queue_agent_job(
    session_factory: sessionmaker[Session],
    *,
    status: AnalysisJobStatus,
    minutes_ago: int = 30,
) -> None:
    """Put one job on the agent's queue in a given state."""
    with session_factory() as session:
        session.add(
            AnalysisJob(
                analysis_job_id=new_identifier(),
                job_type=AnalysisJobType.FIND_MY_PET.value,
                status=status.value,
                attempt_count=0,
                created_at=_naive_now() - timedelta(minutes=minutes_ago),
            )
        )
        session.commit()


class TestTheStatTiles:
    """Every figure that has somewhere to go is the way to get there."""

    def test_a_tile_with_a_target_renders_as_a_link(
        self, staff_client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves the roster is reachable from the figure that describes it.

        Spec section 22 wants statistics connected to actions; before this
        the only way from "12 available" to the twelve animals was the
        navigation bar.
        """
        body = staff_client.get("/dashboard").get_data(as_text=True)

        assert 'class="stat stat--link" href="/animals/manage?status=AVAILABLE"' in body
        assert "Manage available animals" in body

    def test_a_tile_without_a_target_is_not_a_link(
        self, staff_client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves a figure with no screen behind it does not pretend otherwise.

        There is no staff-side invitation list yet, so that tile is a
        number. A link that went somewhere unrelated would be worse than
        none - the negative half of making tiles actionable.
        """
        body = staff_client.get("/dashboard").get_data(as_text=True)

        invitations = body.index("Invitations awaiting a reply")
        tile_start = body.rindex("<div class=\"stat\"", 0, invitations)

        assert "stat--link" not in body[tile_start:invitations]


class TestTheAgentQueueStrip:
    """The agent runs elsewhere; the dashboard is where staff can see it."""

    def test_outstanding_and_failed_work_are_reported_separately(
        self,
        staff_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves a stuck queue is distinguishable from a busy one.

        A pending count alone reads the same whether the worker is thirty
        seconds behind or was killed yesterday, so the age of the oldest
        waiting job is shown too.
        """
        queue_agent_job(session_factory, status=AnalysisJobStatus.PENDING, minutes_ago=30)
        queue_agent_job(session_factory, status=AnalysisJobStatus.FAILED)

        body = staff_client.get("/dashboard").get_data(as_text=True)

        assert "Agent queue" in body
        assert "Explanations still to write" in body
        assert "Jobs that gave up" in body
        assert "30 minutes ago" in body

    def test_an_empty_queue_says_so(
        self, staff_client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves nothing queued reads as finished rather than as broken.

        Zero pending and zero failed is the healthy state, and a strip of
        bare zeroes does not say which it is.
        """
        body = staff_client.get("/dashboard").get_data(as_text=True)

        assert "Nothing is queued" in body


class TestAttentionHeadlines:
    """Prose a person wrote, not a format string (E-8)."""

    def test_a_single_item_is_phrased_in_the_singular(
        self,
        staff_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the most senior screen no longer says "1 application(s)".

        One stale application, so both the count and the noun are decided by
        the same fact and either would be visible if they disagreed.
        """
        add_application(
            session_factory,
            world["adopter_profile_id"],
            world["available_animal_id"],
            days_ago=30,
        )

        body = staff_client.get("/dashboard").get_data(as_text=True)

        assert "1 application waiting over a week" in body
        assert "(s)" not in body

    def test_several_items_are_phrased_in_the_plural(
        self,
        staff_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the plural branch is the one that reads naturally too."""
        add_application(
            session_factory,
            world["adopter_profile_id"],
            world["available_animal_id"],
            days_ago=30,
        )
        add_application(
            session_factory,
            world["other_profile_id"],
            world["available_animal_id"],
            days_ago=30,
        )

        body = staff_client.get("/dashboard").get_data(as_text=True)

        assert "2 applications waiting over a week" in body


class TestTheActivityFeed:
    """A sentence renders whole, or it does not render an object at all."""

    def test_an_entry_names_the_animal_it_links_to(
        self,
        staff_client: FlaskClient,
        world: dict[str, str],
        adopter_client: FlaskClient,
    ) -> None:
        """Proves the feed does not emit a link with no text.

        An event naming a deleted animal used to carry the identifier with
        no name, producing "Adopter applied for" followed by an invisible
        link. The two now travel together, which this checks by exercising
        the normal path: the name must appear inside the link.
        """
        adopter_client.post(f"/my/apply/{world['available_animal_id']}")

        body = staff_client.get("/dashboard").get_data(as_text=True)

        assert f'href="/animals/{world["available_animal_id"]}">Clover</a>' in body

    def test_each_entry_carries_a_machine_readable_time(
        self,
        staff_client: FlaskClient,
        world: dict[str, str],
        adopter_client: FlaskClient,
    ) -> None:
        """Proves E-9: the feed emits `<time datetime=...>` rather than bare text."""
        adopter_client.post(f"/my/apply/{world['available_animal_id']}")

        body = staff_client.get("/dashboard").get_data(as_text=True)

        assert '<time class="activity__time" datetime="' in body


class TestTheAdoptersApplicationList:
    """What an adopter already told the organization, and what happened next."""

    def test_the_message_the_adopter_wrote_is_shown_back_to_them(
        self,
        adopter_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves E-11: a stored message is no longer write-only.

        It was captured on the application form and displayed nowhere, so
        an adopter had no way to recall what they had already said.
        """
        add_application(
            session_factory,
            world["adopter_profile_id"],
            world["available_animal_id"],
            applicant_message="We have a quiet house and a large garden.",
        )

        body = adopter_client.get("/my/applications").get_data(as_text=True)

        assert "We have a quiet house and a large garden." in body

    def test_each_row_links_to_its_own_history(
        self,
        adopter_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the adopter can reach the record spec 7.5 is written about."""
        application_id = add_application(
            session_factory,
            world["adopter_profile_id"],
            world["available_animal_id"],
        )

        body = adopter_client.get("/my/applications").get_data(as_text=True)

        assert f"/history/Application/{application_id}" in body

    def test_the_submitted_date_is_machine_readable(
        self,
        adopter_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves E-9 reached this table as well as the dashboard."""
        add_application(
            session_factory,
            world["adopter_profile_id"],
            world["available_animal_id"],
        )

        body = adopter_client.get("/my/applications").get_data(as_text=True)

        assert "<time datetime=" in body


class TestWhoMayReadAHistory:
    """The permission change, and the line it did not cross."""

    def test_an_adopter_may_read_their_own_application_history(
        self,
        adopter_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves blueprint section 10 reaches the person it is written for.

        Spec 7.5 describes an application being closed by somebody else's
        approval and reopened when that approval is reversed. An adopter who
        could not see that happen would have to take it on trust.
        """
        application_id = add_application(
            session_factory,
            world["adopter_profile_id"],
            world["available_animal_id"],
        )

        response = adopter_client.get(f"/history/Application/{application_id}")

        assert response.status_code == 200

    def test_an_adopter_may_not_read_somebody_elses(
        self,
        other_adopter_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves relaxing the route did not open it.

        The history names the animal, the staff member and every decision
        taken - it is the fullest record of somebody's dealings with the
        organization that exists.
        """
        application_id = add_application(
            session_factory,
            world["adopter_profile_id"],
            world["available_animal_id"],
        )

        response = other_adopter_client.get(f"/history/Application/{application_id}")

        assert response.status_code == 403

    def test_an_adopter_may_not_read_an_animals_history(
        self, adopter_client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves an animal's timeline stays staff-only.

        It lists every applicant for that animal, so it is several people's
        data rather than one's.
        """
        response = adopter_client.get(
            f"/history/Animal/{world['available_animal_id']}"
        )

        assert response.status_code == 403

    def test_staff_may_read_any_history(
        self,
        staff_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the staff path is unchanged by the relaxation."""
        application_id = add_application(
            session_factory,
            world["adopter_profile_id"],
            world["available_animal_id"],
        )

        assert staff_client.get(f"/history/Application/{application_id}").status_code == 200
        assert (
            staff_client.get(f"/history/Animal/{world['available_animal_id']}").status_code
            == 200
        )

    def test_an_anonymous_visitor_is_sent_to_sign_in(
        self, client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves the route is still behind authentication, as every other is."""
        response = client.get(f"/history/Animal/{world['available_animal_id']}")

        assert response.status_code in (302, 401)

    def test_an_adopter_with_no_profile_is_refused(
        self,
        application: Flask,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the staff test is on the role, not on the absence of a profile.

        Both a staff member and an adopter who has not filled in a profile
        carry `adopter_profile_id = None`. A rule written as "no profile
        means see everything" would read identically for the two and hand a
        brand-new account the run of the log.
        """
        application_id = add_application(
            session_factory,
            world["adopter_profile_id"],
            world["available_animal_id"],
        )
        profileless = application.test_client()
        sign_in(profileless, PROFILELESS_EMAIL)

        response = profileless.get(f"/history/Application/{application_id}")

        assert response.status_code == 403
