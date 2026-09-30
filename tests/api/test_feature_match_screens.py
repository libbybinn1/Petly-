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
from collections.abc import Iterator
from contextlib import contextmanager
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
from sqlalchemy import event
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
    """Both ranking screens opt into the status endpoint (docs/UX.md 3).

    Opting in is now conditional: a page with nothing outstanding emits no
    poll URL at all. The poller used to start on any results page and, with
    no baseline to compare its first answer against, asked again every thirty
    seconds for as long as the tab stayed open.
    """

    def test_find_my_pet_polls_its_own_scope(
        self,
        adopter_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the adopter's page asks about the adopter's own matches.

        The scope carries no identifier: the endpoint reads the profile from
        the session, so this URL cannot be edited into somebody else's.
        """
        queue_job(
            session_factory,
            adopter_profile_id=world["adopter_profile_id"],
            animal_id=world["available_animal_id"],
        )

        body = adopter_client.get("/my/matches").get_data(as_text=True)

        assert 'data-analysis-status-url="/api/analysis-status?scope=my-matches"' in body

    def test_the_page_carries_the_counts_it_was_rendered_with(
        self,
        adopter_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the poller is given a baseline rather than starting blind."""
        queue_job(
            session_factory,
            adopter_profile_id=world["adopter_profile_id"],
            animal_id=world["available_animal_id"],
        )

        body = adopter_client.get("/my/matches").get_data(as_text=True)

        assert 'data-analysis-pending="1"' in body
        assert "data-analysis-generation=" in body

    def test_a_settled_page_does_not_poll_at_all(
        self, adopter_client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves the opt-in is conditional, which is what stops the loop.

        Negative half of the pair: with nothing queued there is nothing to
        wait for, so the page should carry no poll URL for the script to
        find.
        """
        body = adopter_client.get("/my/matches").get_data(as_text=True)

        assert "data-analysis-status-url" not in body

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
        queue_job(
            session_factory,
            adopter_profile_id=world["adopter_profile_id"],
            animal_id=world["available_animal_id"],
        )

        body = staff_client.get(
            f"/animals/{world['available_animal_id']}/adopters"
        ).get_data(as_text=True)

        assert "data-analysis-status-url=" in body
        assert f"animal_id={world['available_animal_id']}" in body

    def test_a_settled_applicant_ranking_does_not_poll(
        self,
        staff_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the staff screen opts out once the queue is empty too."""
        apply_for(
            session_factory,
            world["adopter_profile_id"],
            world["available_animal_id"],
        )

        body = staff_client.get(
            f"/animals/{world['available_animal_id']}/adopters"
        ).get_data(as_text=True)

        assert "data-analysis-status-url" not in body


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


def add_applicant(session_factory: sessionmaker[Session], animal_id: str, index: int) -> None:
    """Add one more opted-in adopter who has applied for an animal."""
    with session_factory() as session:
        user = User(
            user_id=new_identifier(), email=f"queue{index}@petmatch.test",
            password_hash=generate_password_hash("x"), full_name=f"Applicant {index}",
            role="ADOPTER", is_active=True, created_at=_naive_now(),
        )
        profile = AdopterProfile(
            adopter_profile_id=new_identifier(), user_id=user.user_id,
            home_type=HomeType.HOUSE.value, has_yard=True,
            household_has_children=False, has_other_animals=False,
            experience_level=ExperienceLevel.SOME.value,
            activity_level=ActivityLevel.MODERATE.value,
            daily_hours_available=4.0, city="Haifa",
            open_to_proactive_suggestions=True, is_complete=True,
            created_at=_naive_now(), updated_at=_naive_now(),
        )
        session.add_all([user, profile])
        session.add(
            AdoptionApplication(
                application_id=new_identifier(),
                adopter_profile_id=profile.adopter_profile_id,
                animal_id=animal_id,
                status=ApplicationStatus.SUBMITTED.value,
                submitted_at=_naive_now(),
            )
        )
        session.commit()


@contextmanager
def counted_statements(
    session_factory: sessionmaker[Session],
) -> Iterator[list[str]]:
    """Record every statement the application sends while the block runs."""
    engine = session_factory.kw["bind"]
    statements: list[str] = []

    def record(connection, cursor, statement, parameters, context, executemany):  # noqa: ANN001, ANN202
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


class TestRankingDoesNotQueryPerCandidate:
    """Ranking cost must not grow with the number of people being ranked.

    The candidate rows were assembled with `session.get(User, ...)` inside
    the loop, so ranking twenty applicants issued twenty-five round trips to
    a database that is a shared free-tier server on the far side of the
    internet - and discovery paid that for every eligible adopter before
    slicing the list down to ten.
    """

    def test_ranking_applicants_costs_the_same_for_three_as_for_eight(
        self,
        staff_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the applicant ranking screen issues a fixed number of queries."""
        animal_id = world["available_animal_id"]
        apply_for(session_factory, world["adopter_profile_id"], animal_id)
        apply_for(session_factory, world["other_profile_id"], animal_id)
        add_applicant(session_factory, animal_id, 0)

        # Warm up first: the expiry sweep and Flask's own first-request work
        # would otherwise be counted against the smaller list only.
        staff_client.get(f"/animals/{animal_id}/adopters")

        with counted_statements(session_factory) as few:
            staff_client.get(f"/animals/{animal_id}/adopters")

        for index in range(1, 6):
            add_applicant(session_factory, animal_id, index)

        with counted_statements(session_factory) as many:
            response = staff_client.get(f"/animals/{animal_id}/adopters")

        assert response.status_code == 200
        assert few and len(many) == len(few)

    def test_discovering_adopters_costs_the_same_as_the_roster_grows(
        self,
        staff_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves discovery does not pay per candidate before its limit slice.

        Negative half of the pair: the cost is measured against a roster that
        grew, so a per-candidate query would show up as a larger count rather
        than as a slower page nobody notices in a test.
        """
        animal_id = add_demanding_animal(session_factory)
        staff_client.get(f"/animals/{animal_id}/discover")

        with counted_statements(session_factory) as few:
            staff_client.get(f"/animals/{animal_id}/discover")

        for index in range(10, 18):
            add_applicant(session_factory, world["available_animal_id"], index)

        with counted_statements(session_factory) as many:
            response = staff_client.get(f"/animals/{animal_id}/discover")

        assert response.status_code == 200
        assert few and len(many) == len(few)


class TestSourceReferencesAreNotBlindlyLinked:
    """The reference is a string an agent wrote, so it is not a trusted URL.

    The analysis screen rendered `<a href="{{ source.reference }}">` for
    anything whose kind was "web". A model that wrote "javascript:..." into
    its own citation would have had it rendered as a live link inside the
    staff review screen, which is the one place the record is inspected.
    """

    @staticmethod
    def _store_sources(
        session_factory: sessionmaker[Session],
        world: dict[str, str],
        sources: list[dict[str, object]],
    ) -> str:
        """Store one analysis carrying the given evidence sources."""
        analysis_id = new_identifier()
        with session_factory() as session:
            session.add(
                MatchAnalysis(
                    match_analysis_id=analysis_id,
                    direction=MatchDirection.ANIMAL_TO_ADOPTER.value,
                    adopter_profile_id=world["adopter_profile_id"],
                    animal_id=world["available_animal_id"],
                    score=70,
                    is_disqualified=False,
                    criterion_scores="[]",
                    reasons="[]",
                    concerns="[]",
                    missing_information="[]",
                    evidence_sources=json.dumps(sources),
                    reasoning_trace="[]",
                    used_web_search=True,
                    model_name="qwen2.5:3b-instruct",
                    generated_at=_naive_now(),
                )
            )
            session.commit()
        return analysis_id

    def test_an_https_source_is_still_a_link(
        self,
        staff_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the feature survived: a real web result is still clickable."""
        analysis_id = self._store_sources(
            session_factory,
            world,
            [{"kind": "web", "reference": "https://example.test/guide", "cited": True}],
        )

        body = staff_client.get(f"/analyses/{analysis_id}").get_data(as_text=True)

        assert 'href="https://example.test/guide"' in body

    def test_a_script_scheme_is_rendered_as_text(
        self,
        staff_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves a reference that is not an http address never becomes an href."""
        analysis_id = self._store_sources(
            session_factory,
            world,
            [{"kind": "web", "reference": "javascript:alert(1)", "cited": True}],
        )

        body = staff_client.get(f"/analyses/{analysis_id}").get_data(as_text=True)

        assert 'href="javascript:' not in body
        assert "javascript:alert(1)" in body

    def test_a_knowledge_base_citation_is_rendered_as_text(
        self,
        staff_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves a RAG reference is shown as the reference it is, not a link."""
        analysis_id = self._store_sources(
            session_factory,
            world,
            [{"kind": "rag", "reference": "space-and-housing.md#apartments", "cited": True}],
        )

        body = staff_client.get(f"/analyses/{analysis_id}").get_data(as_text=True)

        assert 'href="space-and-housing.md' not in body
        assert "space-and-housing.md#apartments" in body


class TestTheFitGradeIsShown:
    """Every ranked card and the analysis page carry the grade (spec section 8)."""

    def test_find_my_pet_shows_a_grade_and_where_the_points_went(
        self, adopter_client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves the adopter sees a letter grade, not only a bare number.

        The world's available animal is an eligible, imperfect match, so the
        card must name at least one deduction.
        """
        body = adopter_client.get("/my/matches").get_data(as_text=True)

        assert 'class="fit-card' in body
        assert "Fit grade" in body
        assert "Where the points went" in body

    def test_the_applicant_ranking_shows_the_grade_too(
        self,
        staff_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves staff get the same grade on the screen where they decide."""
        apply_for(session_factory, world["adopter_profile_id"], world["available_animal_id"])

        body = staff_client.get(
            f"/animals/{world['available_animal_id']}/adopters"
        ).get_data(as_text=True)

        assert 'class="fit-card' in body

    def test_the_analysis_page_itemises_the_stored_breakdown(
        self,
        staff_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the detail page grades the stored score and names each deduction.

        82 with one criterion at 40 of 100 on a 30% weight: that criterion
        cost 18 points, which is exactly what 82 is missing.
        """
        analysis_id = add_analysis_with_trace(
            session_factory, world["adopter_profile_id"], world["available_animal_id"]
        )
        with session_factory() as session:
            analysis = session.get(MatchAnalysis, analysis_id)
            assert analysis is not None
            analysis.criterion_scores = json.dumps(
                [
                    {"criterion": "living_environment", "score": 100, "weight": 0.7,
                     "explanation": "Plenty of room."},
                    {"criterion": "daily_availability", "score": 40, "weight": 0.3,
                     "explanation": "Needs 4 hours a day; has 1."},
                ]
            )
            session.commit()

        body = staff_client.get(f"/analyses/{analysis_id}").get_data(as_text=True)

        assert "Strong fit" in body
        assert "\u221218" in body  # a true minus sign, as the page renders it
        assert "Needs 4 hours a day; has 1." in body

    def test_a_disqualified_pairing_shows_no_grade(
        self,
        staff_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves an ineligible pairing is never dressed up with a letter.

        Negative case: a hard-constraint violation has no breakdown, and a
        grade of F would suggest it merely scored badly.
        """
        profile_id = add_disqualified_adopter(session_factory)
        animal_id = add_demanding_animal(session_factory)
        apply_for(session_factory, profile_id, animal_id)

        body = staff_client.get(f"/animals/{animal_id}/adopters").get_data(as_text=True)

        assert "Not eligible" in body
        assert 'class="fit-card' not in body
