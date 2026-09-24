"""Tests for what the ranking screens show while the agent is still working.

Ranking is instant and deterministic; explanations arrive tens of seconds
later from another process (CLAUDE.md R4). Three things follow, and each is
checked here:

- the page opts into polling, so a finished explanation appears without the
  visitor guessing when to reload;
- a card with no explanation yet shows the shared pending block, including
  how long it has been waiting, rather than one of the three different
  sentences these screens used to carry;
- discovery says *who* it excluded and why, instead of only how many.

No test here runs a model. The pending state is produced by queueing a job and
not running it, which is exactly the state a visitor sees in practice.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from app.domain.enums import (
    ActivityLevel,
    AnalysisJobStatus,
    AnalysisJobType,
    AnimalSize,
    AnimalStatus,
    ApplicationStatus,
    ExperienceLevel,
    HomeType,
    MatchDirection,
    Species,
    Temperament,
)
from app.infrastructure.models import (
    AdopterProfile,
    AdoptionApplication,
    AnalysisJob,
    Animal,
    MatchAnalysis,
    User,
    new_identifier,
)
from flask.testing import FlaskClient
from sqlalchemy.orm import Session, sessionmaker
from werkzeug.security import generate_password_hash

pytestmark = pytest.mark.api

# One step of a recorded trace, shaped as agent_service.worker stores it.
DEFAULT_TRACE: list[dict[str, object]] = [
    {"step": 1, "action": "retrieve_guidance", "detail": "rabbit housing needs"},
    {"step": 2, "action": "score", "detail": "deterministic scorer"},
]


def _naive_now() -> datetime:
    """Naive UTC, as the DATETIME columns store."""
    return datetime.now(UTC).replace(tzinfo=None)


def queue_job(
    session_factory: sessionmaker[Session],
    *,
    adopter_profile_id: str,
    animal_id: str,
    minutes_ago: int = 4,
) -> None:
    """Queue one unworked analysis job, as submitting an application does."""
    with session_factory() as session:
        session.add(
            AnalysisJob(
                analysis_job_id=new_identifier(),
                job_type=AnalysisJobType.RANK_APPLICANT.value,
                status=AnalysisJobStatus.PENDING.value,
                adopter_profile_id=adopter_profile_id,
                animal_id=animal_id,
                attempt_count=0,
                created_at=_naive_now() - timedelta(minutes=minutes_ago),
            )
        )
        session.commit()


def apply_for(
    session_factory: sessionmaker[Session], adopter_profile_id: str, animal_id: str
) -> str:
    """Record an application directly, without queueing its analysis."""
    application_id = new_identifier()
    with session_factory() as session:
        session.add(
            AdoptionApplication(
                application_id=application_id,
                adopter_profile_id=adopter_profile_id,
                animal_id=animal_id,
                status=ApplicationStatus.SUBMITTED.value,
                submitted_at=_naive_now(),
            )
        )
        session.commit()
    return application_id


def add_disqualified_adopter(session_factory: sessionmaker[Session]) -> str:
    """Add an opted-in adopter that no dog can be matched to.

    An apartment with no yard fails the space rule against a large dog
    needing a large space, which is a hard constraint rather than a low
    score (spec section 8).
    """
    with session_factory() as session:
        user = User(
            user_id=new_identifier(), email="cramped@petmatch.test",
            password_hash=generate_password_hash("x"), full_name="Cramped Quarters",
            role="ADOPTER", is_active=True, created_at=_naive_now(),
        )
        profile = AdopterProfile(
            adopter_profile_id=new_identifier(), user_id=user.user_id,
            home_type=HomeType.APARTMENT.value, has_yard=False,
            household_has_children=False, has_other_animals=False,
            experience_level=ExperienceLevel.NONE.value,
            activity_level=ActivityLevel.LOW.value,
            daily_hours_available=0.5, city="Haifa",
            open_to_proactive_suggestions=True, is_complete=True,
            created_at=_naive_now(), updated_at=_naive_now(),
        )
        session.add_all([user, profile])
        session.commit()
        return profile.adopter_profile_id


def add_demanding_animal(session_factory: sessionmaker[Session]) -> str:
    """Add an available animal whose space needs exclude a small home."""
    animal_id = new_identifier()
    with session_factory() as session:
        session.add(
            Animal(
                animal_id=animal_id, name="Bruno", species=Species.DOG.value,
                age_years=4.0, size=AnimalSize.LARGE.value,
                temperament=Temperament.ENERGETIC.value,
                activity_level=ActivityLevel.HIGH.value,
                good_with_children=True, good_with_other_animals=True,
                has_special_needs=False,
                required_space=AnimalSize.LARGE.value, city="Haifa",
                status=AnimalStatus.AVAILABLE.value,
                created_at=_naive_now(), updated_at=_naive_now(),
            )
        )
        session.commit()
    return animal_id


def add_analysis_with_trace(
    session_factory: sessionmaker[Session],
    adopter_profile_id: str,
    animal_id: str,
    trace: list[dict[str, object]] | None = DEFAULT_TRACE,
) -> str:
    """Store one analysis, with or without a recorded reasoning trace.

    Written directly rather than by running the agent: the trace is the
    agent's own output and running a model here would make the test
    non-deterministic and slow (rule R3).
    """
    analysis_id = new_identifier()
    with session_factory() as session:
        session.add(
            MatchAnalysis(
                match_analysis_id=analysis_id,
                direction=MatchDirection.ANIMAL_TO_ADOPTER.value,
                adopter_profile_id=adopter_profile_id,
                animal_id=animal_id,
                score=82,
                is_disqualified=False,
                criterion_scores="[]",
                reasons=json.dumps(["The household has room to spare."]),
                concerns="[]",
                missing_information="[]",
                evidence_sources=json.dumps(
                    [
                        {"kind": "rag", "reference": "small-mammal-care.md", "cited": True},
                        {"kind": "rag", "reference": "unused-note.md", "cited": False},
                    ]
                ),
                reasoning_trace=json.dumps(trace) if trace is not None else None,
                used_web_search=False,
                model_name="qwen2.5:3b-instruct",
                generated_at=_naive_now(),
            )
        )
        session.commit()
    return analysis_id


class TestThePollerIsWiredUp:
    """Both ranking screens opt into the status endpoint (docs/UX.md 3)."""

    def test_find_my_pet_polls_its_own_scope(
        self, adopter_client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves the adopter's page asks about the adopter's own matches.

        The scope carries no identifier: the endpoint reads the profile from
        the session, so this URL cannot be edited into somebody else's.
        """
        body = adopter_client.get("/my/matches").get_data(as_text=True)

        assert 'data-analysis-status-url="/api/analysis-status?scope=my-matches"' in body

    def test_the_applicant_ranking_polls_for_one_animal(
        self,
        staff_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the staff page watches the animal it is about.

        The animal-scoped endpoint is staff-only on the server, which the
        analysis-status tests cover; this only checks the page asks for it.
        """
        apply_for(
            session_factory,
            world["adopter_profile_id"],
            world["available_animal_id"],
        )

        body = staff_client.get(
            f"/animals/{world['available_animal_id']}/adopters"
        ).get_data(as_text=True)

        assert "data-analysis-status-url=" in body
        assert f"animal_id={world['available_animal_id']}" in body


class TestThePendingBlock:
    """One waiting state, in one partial, on both screens."""

    def test_an_adopter_sees_the_shared_pending_block(
        self,
        adopter_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves Find My Pet uses the partial rather than its own sentence.

        The block answers what the old sentence did not: that the score is
        already final, and that a Refresh link exists for a browser with
        JavaScript blocked.
        """
        body = adopter_client.get("/my/matches").get_data(as_text=True)

        assert "pending-block" in body
        assert "Refresh" in body

    def test_a_queued_job_is_reported_with_how_long_it_has_waited(
        self,
        adopter_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the wait is visible, which is what distinguishes slow from stuck.

        "Queued 4 minutes ago" is the difference between a visitor waiting
        and a visitor concluding the feature is broken.
        """
        queue_job(
            session_factory,
            adopter_profile_id=world["adopter_profile_id"],
            animal_id=world["available_animal_id"],
            minutes_ago=4,
        )

        body = adopter_client.get("/my/matches").get_data(as_text=True)

        assert "Queued 4 minutes ago" in body

    def test_no_queued_job_means_no_queued_label(
        self,
        adopter_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves a card does not claim to be queued when nothing is.

        The negative half: an explanation that was never asked for is a
        different state from one that is on its way, and saying "Queued" for
        it would be a plain untruth.
        """
        body = adopter_client.get("/my/matches").get_data(as_text=True)

        assert "pending-block" in body
        assert "Queued " not in body

    def test_the_applicant_ranking_shows_the_same_block(
        self,
        staff_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the staff screen shares the partial, not a second wording."""
        apply_for(
            session_factory,
            world["adopter_profile_id"],
            world["available_animal_id"],
        )
        queue_job(
            session_factory,
            adopter_profile_id=world["adopter_profile_id"],
            animal_id=world["available_animal_id"],
            minutes_ago=1,
        )

        body = staff_client.get(
            f"/animals/{world['available_animal_id']}/adopters"
        ).get_data(as_text=True)

        assert "pending-block" in body
        assert "Queued 1 minute ago" in body


class TestExcludedCandidates:
    """Discovery reports who it ruled out and why (E-12, spec section 10)."""

    def test_a_disqualified_adopter_is_named_with_their_reason(
        self,
        staff_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves a bare count is replaced by something staff can check.

        A hard constraint is the one part of the scoring a staff member may
        legitimately want to overrule by hand - by talking to the adopter -
        and they cannot if the screen will not say who was excluded.
        """
        add_disqualified_adopter(session_factory)
        animal_id = add_demanding_animal(session_factory)

        body = staff_client.get(f"/animals/{animal_id}/discover").get_data(as_text=True)

        assert "Cramped Quarters" in body
        assert "<details" in body

    def test_an_eligible_roster_shows_no_exclusion_list(
        self,
        staff_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the disclosure appears only when something was excluded.

        The negative case: an empty `<details>` reading "0 adopters were
        excluded" would be noise on every healthy animal.
        """
        body = staff_client.get(
            f"/animals/{world['available_animal_id']}/discover"
        ).get_data(as_text=True)

        assert "excluded by a hard eligibility rule" not in body


class TestTheAnalysisDetailPage:
    """The reasoning trace the agent records is shown, not just stored."""

    def test_the_reasoning_trace_and_cited_markers_are_rendered(
        self,
        staff_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the trace reaches the screen at all.

        `matches/analysis.html` guarded on `analysis.reasoning_trace`, and
        the detail view model had no such field - so the panel was dead
        markup and the agent's steps were written to the database and read
        by nobody. Rule R4 requires the reasoning and its sources to be
        inspectable, which means rendered.
        """
        analysis_id = add_analysis_with_trace(
            session_factory,
            world["adopter_profile_id"],
            world["available_animal_id"],
        )

        body = staff_client.get(f"/analyses/{analysis_id}").get_data(as_text=True)

        assert "How the agent reasoned" in body
        assert "retrieve_guidance" in body
        assert "cited" in body

    def test_an_analysis_without_a_trace_shows_no_empty_panel(
        self,
        staff_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves an older analysis degrades rather than showing a blank heading.

        Analyses written before the agent recorded a trace have none, and a
        panel titled "How the agent reasoned" with nothing under it would
        claim the agent reasoned in no steps.
        """
        analysis_id = add_analysis_with_trace(
            session_factory,
            world["adopter_profile_id"],
            world["available_animal_id"],
            trace=None,
        )

        body = staff_client.get(f"/analyses/{analysis_id}").get_data(as_text=True)

        assert "How the agent reasoned" not in body
