"""API tests for semantic adopter search on the discovery screen (requirement 6).

`POST /animals/<id>/discover/semantic` enqueues a `SEMANTIC_ADOPTER_SEARCH`
job and redirects; `GET /animals/<id>/discover` shows that search waiting,
failed, or its matches beside each one's deterministic verdict. No test here
embeds anything: completed jobs are written through
`semantic_search_to_payload`, the same function the agent uses, so the tests
cannot assert against a shape the agent does not produce.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from app.domain.enums import AnalysisJobStatus, AnalysisJobType, ApplicationStatus
from app.domain.semantic_profiles import (
    SemanticAdopterMatch,
    SemanticSearchOutcome,
    semantic_search_to_payload,
)
from app.infrastructure.models import AdoptionApplication, AnalysisJob, new_identifier
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.api


def _naive_now() -> datetime:
    """Naive UTC, as the DATETIME columns store."""
    return datetime.now(UTC).replace(tzinfo=None)


def semantic_jobs(session_factory: sessionmaker[Session]) -> list[AnalysisJob]:
    """Every semantic search job in the database."""
    with session_factory() as session:
        return list(
            session.execute(
                select(AnalysisJob).where(
                    AnalysisJob.job_type == AnalysisJobType.SEMANTIC_ADOPTER_SEARCH.value
                )
            )
            .scalars()
            .all()
        )


def add_search_job(
    session_factory: sessionmaker[Session],
    animal_id: str,
    status: AnalysisJobStatus,
    outcome: SemanticSearchOutcome | None = None,
) -> str:
    """Insert one semantic search job in a given state; return its id."""
    job_id = new_identifier()
    with session_factory() as session:
        session.add(
            AnalysisJob(
                analysis_job_id=job_id,
                job_type=AnalysisJobType.SEMANTIC_ADOPTER_SEARCH.value,
                status=status.value,
                animal_id=animal_id,
                result_payload=(
                    json.dumps(semantic_search_to_payload(outcome)) if outcome else None
                ),
                attempt_count=1,
                created_at=_naive_now(),
                completed_at=_naive_now() if status is AnalysisJobStatus.COMPLETED else None,
            )
        )
        session.commit()
    return job_id


def discover_url(animal_id: str) -> str:
    """The discovery screen for one animal."""
    return f"/animals/{animal_id}/discover"


def semantic_url(animal_id: str) -> str:
    """The route that queues a semantic search for one animal."""
    return f"/animals/{animal_id}/discover/semantic"


class TestQueueingASearch:
    """A staff POST enqueues one job and returns to the panel."""

    def test_staff_queue_exactly_one_job(
        self,
        staff_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the request hands the search to the agent and stops there (R4)."""
        animal_id = world["available_animal_id"]
        response = staff_client.post(semantic_url(animal_id))

        jobs = semantic_jobs(session_factory)
        assert len(jobs) == 1
        assert jobs[0].animal_id == animal_id
        assert jobs[0].status == AnalysisJobStatus.PENDING.value
        assert response.status_code == 302
        assert response.headers["Location"].endswith(f"{discover_url(animal_id)}#semantic-matches")

    def test_a_second_press_reuses_the_waiting_job(
        self,
        staff_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves repeated clicks cannot flood the agent's queue."""
        animal_id = world["available_animal_id"]
        staff_client.post(semantic_url(animal_id))
        staff_client.post(semantic_url(animal_id))

        assert len(semantic_jobs(session_factory)) == 1

    def test_an_adopter_is_refused(
        self,
        adopter_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Negative test: a forged POST by an adopter gets 403 and queues nothing."""
        response = adopter_client.post(semantic_url(world["available_animal_id"]))

        assert response.status_code == 403
        assert semantic_jobs(session_factory) == []

    def test_a_signed_out_visitor_is_sent_to_sign_in(
        self,
        client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Negative test: an anonymous POST is redirected to login and queues nothing."""
        response = client.post(semantic_url(world["available_animal_id"]))

        assert response.status_code == 302
        assert "/login" in response.headers["Location"]
        assert semantic_jobs(session_factory) == []

    def test_an_unknown_animal_is_404(
        self,
        staff_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Negative test: a search for an animal that does not exist is refused."""
        response = staff_client.post(semantic_url(new_identifier()))

        assert response.status_code == 404
        assert semantic_jobs(session_factory) == []


class TestThePanel:
    """The discovery screen shows the search in whichever state it is in."""

    def test_without_a_search_the_panel_offers_one(
        self, staff_client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves the panel and its button render before any search exists."""
        html = staff_client.get(discover_url(world["available_animal_id"])).get_data(as_text=True)

        assert "Semantic Search" in html
        assert "No semantic search has been run" in html
        assert "Run semantic search" in html

    def test_a_waiting_search_shows_the_pending_state(
        self,
        staff_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves a queued search waits visibly and the page refreshes itself."""
        animal_id = world["available_animal_id"]
        add_search_job(session_factory, animal_id, AnalysisJobStatus.PENDING)

        html = staff_client.get(discover_url(animal_id)).get_data(as_text=True)

        assert "Searching through adopter profiles" in html
        assert 'http-equiv="refresh"' in html

    def test_a_failed_search_says_so_and_keeps_the_ranking(
        self,
        staff_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Negative test: an agent failure is explained, and the deterministic list remains."""
        animal_id = world["available_animal_id"]
        add_search_job(session_factory, animal_id, AnalysisJobStatus.FAILED)

        html = staff_client.get(discover_url(animal_id)).get_data(as_text=True)

        assert "could not be completed" in html
        assert "Suggested adopters" in html
        assert 'http-equiv="refresh"' not in html

    def test_ready_matches_show_similarity_beside_the_deterministic_verdict(
        self,
        staff_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves similarity is shown alongside, never instead of, eligibility.

        The "other" adopter applied for the animal, so the deterministic
        rules exclude them from discovery: they must appear flagged, with no
        invitation offered, however similar their profile is.
        """
        animal_id = world["available_animal_id"]
        with session_factory() as session:
            session.add(
                AdoptionApplication(
                    application_id=new_identifier(),
                    adopter_profile_id=world["other_profile_id"],
                    animal_id=animal_id,
                    status=ApplicationStatus.SUBMITTED.value,
                    submitted_at=_naive_now(),
                )
            )
            session.commit()
        add_search_job(
            session_factory,
            animal_id,
            AnalysisJobStatus.COMPLETED,
            SemanticSearchOutcome(
                matches=(
                    SemanticAdopterMatch(world["other_profile_id"], 0.91),
                    SemanticAdopterMatch(world["adopter_profile_id"], 0.83),
                ),
                indexed_profile_count=2,
                embedding_model="nomic-embed-text",
            ),
        )

        html = staff_client.get(discover_url(animal_id)).get_data(as_text=True)
        panel = html[html.index('id="semantic-matches"'):]

        assert "0.91" in panel
        assert "0.83" in panel
        assert "Eligible" in panel
        assert "Not eligible" in panel
        assert "Already applied" in panel
        assert panel.count('name="adopter_profile_id"') == 1
        assert f'value="{world["adopter_profile_id"]}"' in panel
        assert "Suggested adopters" in html

    def test_an_unreadable_payload_shows_no_matches_rather_than_failing(
        self,
        staff_client: FlaskClient,
        world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Negative test: a corrupt result degrades to an empty panel, not a 500."""
        animal_id = world["available_animal_id"]
        job_id = add_search_job(session_factory, animal_id, AnalysisJobStatus.COMPLETED)
        with session_factory() as session:
            job = session.get(AnalysisJob, job_id)
            assert job is not None
            job.result_payload = "{not json"
            session.commit()

        response = staff_client.get(discover_url(animal_id))

        assert response.status_code == 200
        assert "found no opted-in adopter profiles" in response.get_data(as_text=True)

    def test_an_adopter_cannot_see_the_panel(
        self, adopter_client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Negative test: the screen carrying the matches stays staff-only."""
        response = adopter_client.get(discover_url(world["available_animal_id"]))

        assert response.status_code == 403
