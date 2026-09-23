"""Tests for the job queue the agent process lives on (spec 5.6, NFR-3.3).

The queue is the process boundary (rule R2), so these tests drive the real
`AgentWorker` against a throwaway SQLite database with the real schema, using
an agent double that returns a fixed outcome. That keeps the assertions about
*the queue's* behaviour - what it stores, what it reuses, what it refuses -
rather than about a model's output.

No live model and no network.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from agent_service.explanation import DETERMINISTIC_FALLBACK_MODEL
from agent_service.intent import SearchIntent, intent_to_payload, payload_to_intent
from agent_service.llm_client import StubLanguageModel
from agent_service.loop import AnalysisOutcome
from agent_service.reasoning_session import ReasoningStep
from agent_service.worker import (
    MAX_JOB_ATTEMPTS,
    STRANDED_JOB_TIMEOUT_MINUTES,
    AgentWorker,
)
from app.config import AgentConfiguration, Configuration, DatabaseConfiguration
from app.domain.enums import (
    ActivityLevel,
    AnalysisJobStatus,
    AnalysisJobType,
    AnimalSize,
    DomainEventType,
    MatchDirection,
    Species,
    Temperament,
    UserRole,
)
from app.domain.matching import MatchScore
from app.eventstore.store import EventStore
from app.infrastructure.database import Base
from app.infrastructure.models import (
    AdopterProfile,
    AnalysisJob,
    Animal,
    MatchAnalysis,
    User,
    new_identifier,
)
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.agent

# The keys the web tier reads back out of an INTERPRET_INTENT job. Written
# out rather than derived, so a change to either side fails this test instead
# of silently agreeing with itself.
INTENT_PAYLOAD_KEYS = {
    "understood",
    "species",
    "size",
    "activity_level",
    "temperament",
    "good_with_children",
    "good_with_other_animals",
    "interpretation",
}


def _now() -> datetime:
    """Naive UTC, matching the worker's own timestamps."""
    return datetime.now(UTC).replace(tzinfo=None)


def make_configuration() -> Configuration:
    """Worker configuration with a zero poll interval; nothing here polls."""
    return Configuration(
        database=DatabaseConfiguration(server="u", database="u", user="u", password="u"),
        agent=AgentConfiguration(
            ollama_base_url="http://localhost:11434",
            chat_model="stub",
            fast_chat_model="stub",
            embedding_model="stub",
            tavily_api_key="",
            poll_interval_seconds=0,
            max_reasoning_steps=8,
        ),
        secret_key="k",
        flask_port=5000,
        is_development=False,
        chroma_persist_directory=Path("chroma"),
        rag_collection_name="test",
        upload_directory=Path("uploads"),
        invitation_expiry_hours=72,
    )


@dataclass(frozen=True)
class Pairing:
    """One adopter and one animal that really exist in the queue's database."""

    adopter_profile_id: str
    animal_id: str


class CountingAgent:
    """An agent double that records each call and returns a fixed outcome."""

    def __init__(self, outcome: AnalysisOutcome) -> None:
        """Configure the outcome every call returns."""
        self._outcome = outcome
        self.calls: list[tuple[str, str, MatchDirection]] = []

    def analyse(
        self, adopter_profile_id: str, animal_id: str, direction: MatchDirection
    ) -> AnalysisOutcome:
        """Record the call and return the configured outcome."""
        self.calls.append((adopter_profile_id, animal_id, direction))
        return self._outcome


def make_outcome(score: int = 72) -> AnalysisOutcome:
    """A completed analysis with a trace, and no model involved."""
    return AnalysisOutcome(
        score=MatchScore(
            direction=MatchDirection.ANIMAL_TO_ADOPTER,
            score=score,
            is_disqualified=False,
            disqualification_reason=None,
            criterion_scores=(),
        ),
        reasons=["A deterministic reason."],
        concerns=[],
        missing_information=[],
        evidence_sources=[{"kind": "rag", "reference": "guide.md#section", "cited": True}],
        used_web_search=False,
        model_name="stub-model",
        reasoning_trace=[
            ReasoningStep(1, "get_adopter_profile", "record returned"),
            ReasoningStep(2, "calculate_score", "score=72 disqualified=False"),
        ],
        citations=["guide.md#section"],
    )


@pytest.fixture
def queue() -> Iterator[sessionmaker[Session]]:
    """A throwaway SQLite job queue with the real schema."""
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    yield sessionmaker(engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def pairing(queue: sessionmaker[Session]) -> Pairing:
    """One adopter and one animal, both last updated an hour ago.

    The caching rule compares an analysis against these `updated_at` stamps,
    so they have to be real rows in the past rather than fabricated ids.
    """
    updated_at = _now() - timedelta(hours=1)
    user = User(
        user_id=new_identifier(),
        email="adopter@test.test",
        password_hash="hash",
        full_name="Test Adopter",
        role=UserRole.ADOPTER.value,
        is_active=True,
        created_at=updated_at,
    )
    profile = AdopterProfile(
        adopter_profile_id=new_identifier(),
        user_id=user.user_id,
        home_type="HOUSE",
        has_yard=True,
        household_has_children=False,
        has_other_animals=False,
        experience_level="SOME",
        activity_level=ActivityLevel.MODERATE.value,
        daily_hours_available=4.0,
        city="Haifa",
        open_to_proactive_suggestions=True,
        is_complete=True,
        created_at=updated_at,
        updated_at=updated_at,
    )
    animal = Animal(
        animal_id=new_identifier(),
        name="Luna",
        species=Species.DOG.value,
        age_years=3.0,
        size=AnimalSize.SMALL.value,
        temperament=Temperament.CALM.value,
        activity_level=ActivityLevel.LOW.value,
        good_with_children=True,
        good_with_other_animals=True,
        has_special_needs=False,
        required_space=AnimalSize.SMALL.value,
        city="Haifa",
        status="AVAILABLE",
        created_at=updated_at,
        updated_at=updated_at,
    )

    with queue() as session:
        session.add_all([user, profile, animal])
        session.commit()

    return Pairing(
        adopter_profile_id=profile.adopter_profile_id, animal_id=animal.animal_id
    )


def _insert(factory: sessionmaker[Session], job: AnalysisJob) -> str:
    """Insert one job row and return its identifier."""
    with factory() as session:
        session.add(job)
        session.commit()
        return job.analysis_job_id


def enqueue_analysis(
    factory: sessionmaker[Session],
    pairing: Pairing,
    *,
    status: AnalysisJobStatus = AnalysisJobStatus.PENDING,
    started_at: datetime | None = None,
    attempt_count: int = 0,
) -> str:
    """Insert one applicant-ranking job for a pairing and return its identifier."""
    return _insert(
        factory,
        AnalysisJob(
            analysis_job_id=new_identifier(),
            job_type=AnalysisJobType.RANK_APPLICANT.value,
            status=status.value,
            adopter_profile_id=pairing.adopter_profile_id,
            animal_id=pairing.animal_id,
            application_id=None,
            attempt_count=attempt_count,
            created_at=_now(),
            started_at=started_at,
        ),
    )


def enqueue_intent(factory: sessionmaker[Session], free_text: str | None) -> str:
    """Insert one intent-interpretation job and return its identifier."""
    return _insert(
        factory,
        AnalysisJob(
            analysis_job_id=new_identifier(),
            job_type=AnalysisJobType.INTERPRET_INTENT.value,
            status=AnalysisJobStatus.PENDING.value,
            adopter_profile_id=None,
            animal_id=None,
            application_id=None,
            natural_language_query=free_text,
            attempt_count=0,
            created_at=_now(),
        ),
    )


def read_job(factory: sessionmaker[Session], job_id: str) -> AnalysisJob:
    """Read one job row back."""
    with factory() as session:
        job = session.get(AnalysisJob, job_id)
        assert job is not None
        return job


def stored_analyses(factory: sessionmaker[Session]) -> list[MatchAnalysis]:
    """Every stored analysis."""
    with factory() as session:
        return list(session.execute(select(MatchAnalysis)).scalars().all())


def build_worker(
    factory: sessionmaker[Session],
    agent: Any,  # noqa: ANN401 - any MatchAnalyser-shaped double
    language_model: Any = None,  # noqa: ANN401
) -> AgentWorker:
    """Assemble a worker from doubles."""
    return AgentWorker(make_configuration(), factory, agent, language_model)


class TestIntentJobs:
    """Spec 6.3: natural-language search is interpreted off the request path."""

    def test_an_intent_job_completes_with_the_documented_payload(
        self, queue: sessionmaker[Session]
    ) -> None:
        """Proves the web tier can read back exactly the contract it expects.

        The payload is the whole point of this job type: it is how an
        interpretation crosses the process boundary, so its keys and value
        types are asserted rather than assumed.
        """
        job_id = enqueue_intent(
            queue,
            "a small quiet cat, good with my kids",
        )
        model = StubLanguageModel(
            canned_response={
                "understood": True,
                "species": ["CAT"],
                "size": "SMALL",
                "activity_level": "LOW",
                "temperament": "CALM",
                "good_with_children": True,
                "interpretation": "A small, calm cat that is good with children.",
            }
        )
        worker = build_worker(queue, CountingAgent(make_outcome()), model)

        assert worker.run_once() is True

        job = read_job(queue, job_id)
        assert job.status == AnalysisJobStatus.COMPLETED.value
        assert job.result_payload is not None
        payload = json.loads(job.result_payload)
        assert set(payload) == INTENT_PAYLOAD_KEYS
        assert payload["understood"] is True
        assert payload["species"] == ["CAT"]
        assert payload["size"] == "SMALL"
        assert payload["good_with_other_animals"] is None
        assert isinstance(payload["interpretation"], str)

    def test_an_intent_job_writes_no_analysis_and_no_event(
        self, queue: sessionmaker[Session]
    ) -> None:
        """Proves an interpretation is not a match assessment.

        It scores nothing and changes nothing, so a `MatchAnalysis` row or an
        `AIAnalysisCompleted` event would both be wrong.
        """
        enqueue_intent(
            queue,
            "a big friendly dog",
        )
        worker = build_worker(
            queue,
            CountingAgent(make_outcome()),
            StubLanguageModel(canned_response={"understood": True, "species": ["DOG"]}),
        )

        worker.run_once()

        with queue() as session:
            events = EventStore(session).read_events_of_type(
                DomainEventType.AI_ANALYSIS_COMPLETED
            )
        assert stored_analyses(queue) == []
        assert events == []
        assert worker.statistics.completed == 1

    def test_a_blank_query_fails_the_job_with_a_message(
        self, queue: sessionmaker[Session]
    ) -> None:
        """Proves a malformed job is refused with a reason, not scored blind.

        A negative test: an INTERPRET_INTENT job with nothing to interpret
        cannot be answered, and a 16-second inference must not be spent
        finding that out.
        """
        job_id = enqueue_intent(queue, "   ")
        model = StubLanguageModel()
        worker = build_worker(queue, CountingAgent(make_outcome()), model)

        worker.run_once()

        job = read_job(queue, job_id)
        assert job.error_message is not None
        assert "natural_language_query" in job.error_message
        assert job.result_payload is None
        assert model.prompts_seen == []
        assert worker.statistics.skipped == 1

    def test_an_intent_job_without_a_model_is_refused(
        self, queue: sessionmaker[Session]
    ) -> None:
        """Proves a missing model is reported rather than answered wrongly.

        A negative test for configuration: an unconfigured worker must not
        quietly return "nothing understood" as though the adopter's words
        were the problem.
        """
        job_id = enqueue_intent(queue, "a cat")
        worker = build_worker(queue, CountingAgent(make_outcome()), None)

        worker.run_once()

        job = read_job(queue, job_id)
        assert job.error_message is not None
        assert "language model" in job.error_message
        assert job.result_payload is None

    def test_the_payload_round_trips_through_the_intent(self) -> None:
        """Proves the stored JSON and the domain object mean the same thing.

        Both halves of the contract live in one module for this reason: the
        agent writes the payload and the web tier reads it back, and a
        mismatch would silently change what the adopter asked for.
        """
        rich = SearchIntent(
            understood=True,
            species=(Species.CAT, Species.DOG),
            size=AnimalSize.SMALL,
            activity_level=ActivityLevel.LOW,
            temperament=Temperament.CALM,
            good_with_children=True,
            good_with_other_animals=True,
            interpretation="A small, calm cat or dog.",
        )
        sparse = SearchIntent(understood=True, interpretation="Anything at all.")
        refused = SearchIntent.not_understood("We could not interpret that.")

        for intent in (rich, sparse, refused):
            assert payload_to_intent(intent_to_payload(intent)) == intent

    def test_an_invented_species_does_not_survive_the_round_trip(self) -> None:
        """Proves a stored payload is validated on the way back in.

        A negative test: the stored JSON is data the web tier did not write,
        so an unrecognised species must be dropped rather than becoming a
        filter that matches nothing.
        """
        payload = intent_to_payload(
            SearchIntent(understood=True, species=(Species.CAT,), interpretation="A cat.")
        )
        payload["species"] = ["DRAGON", "CAT"]

        assert payload_to_intent(payload).species == (Species.CAT,)


class TestAnalysisCaching:
    """NFR-3.3: a completed analysis is not recomputed for unchanged inputs."""

    def test_a_repeated_job_for_unchanged_records_reuses_the_analysis(
        self, queue: sessionmaker[Session], pairing: Pairing
    ) -> None:
        """Proves the second identical job costs no inference and no second row.

        On CPU-only inference an explanation costs roughly 16 seconds, and
        the read models would silently hide a duplicate row while it filled
        a free-tier database.
        """
        agent = CountingAgent(make_outcome())
        worker = build_worker(queue, agent)

        enqueue_analysis(queue, pairing)
        worker.run_once()
        enqueue_analysis(queue, pairing)
        worker.run_once()

        assert len(agent.calls) == 1
        assert len(stored_analyses(queue)) == 1
        assert worker.statistics.reused == 1
        assert worker.statistics.completed == 1

    def test_a_reused_analysis_still_completes_its_job(
        self, queue: sessionmaker[Session], pairing: Pairing
    ) -> None:
        """Proves reuse closes the job rather than leaving it claimed forever."""
        worker = build_worker(queue, CountingAgent(make_outcome()))

        enqueue_analysis(queue, pairing)
        worker.run_once()
        second_job_id = enqueue_analysis(queue, pairing)
        worker.run_once()

        assert read_job(queue, second_job_id).status == AnalysisJobStatus.COMPLETED.value

    def test_a_changed_animal_forces_recomputation_into_the_same_row(
        self, queue: sessionmaker[Session], pairing: Pairing
    ) -> None:
        """Proves an edited record invalidates the cache without duplicating it.

        This is the other half of the caching rule: stale explanations are
        worse than slow ones, so a bumped `updated_at` must recompute - and
        update the existing row rather than insert a rival.
        """
        agent = CountingAgent(make_outcome())
        worker = build_worker(queue, agent)

        enqueue_analysis(queue, pairing)
        worker.run_once()
        first_analysis_id = stored_analyses(queue)[0].match_analysis_id

        with queue() as session:
            animal = session.get(Animal, pairing.animal_id)
            assert animal is not None
            animal.updated_at = _now() + timedelta(minutes=1)
            session.commit()

        enqueue_analysis(queue, pairing)
        worker.run_once()

        analyses = stored_analyses(queue)
        assert len(agent.calls) == 2
        assert len(analyses) == 1
        assert analyses[0].match_analysis_id == first_analysis_id
        assert worker.statistics.reused == 0

    def test_a_fresh_computation_always_appends_its_event(
        self, queue: sessionmaker[Session], pairing: Pairing
    ) -> None:
        """Proves the event log records every computation, including a replacement."""
        worker = build_worker(queue, CountingAgent(make_outcome()))

        enqueue_analysis(queue, pairing)
        worker.run_once()
        with queue() as session:
            animal = session.get(Animal, pairing.animal_id)
            assert animal is not None
            animal.updated_at = _now() + timedelta(minutes=1)
            session.commit()
        enqueue_analysis(queue, pairing)
        worker.run_once()

        with queue() as session:
            events = EventStore(session).read_events_of_type(
                DomainEventType.AI_ANALYSIS_COMPLETED
            )
        assert len(events) == 2

    def test_a_deterministic_fallback_row_is_regenerated_in_place(
        self, queue: sessionmaker[Session], pairing: Pairing
    ) -> None:
        """Proves a degraded analysis is not cached as though it had succeeded.

        A fallback row is the score with criterion text and no model prose,
        written because the model was unavailable. Reusing it would make a
        temporary outage permanent for that pairing, so it is recomputed -
        into the same row, so links to /analyses/<id> keep working.
        """
        agent = CountingAgent(make_outcome())
        worker = build_worker(queue, agent)
        enqueue_analysis(queue, pairing)
        worker.run_once()

        with queue() as session:
            stored = session.execute(select(MatchAnalysis)).scalars().one()
            fallback_id = stored.match_analysis_id
            stored.model_name = DETERMINISTIC_FALLBACK_MODEL
            session.commit()

        enqueue_analysis(queue, pairing)
        worker.run_once()

        analyses = stored_analyses(queue)
        assert len(agent.calls) == 2
        assert len(analyses) == 1
        assert analyses[0].match_analysis_id == fallback_id
        assert analyses[0].model_name == "stub-model"
        assert worker.statistics.reused == 0

    def test_a_generated_analysis_is_reused_where_a_fallback_is_not(
        self, queue: sessionmaker[Session], pairing: Pairing
    ) -> None:
        """Proves the two cases are distinguished by `model_name`, not by age.

        Both rows are newer than the records they describe, so age alone
        cannot tell them apart - which is exactly the mistake this guards.
        """
        agent = CountingAgent(make_outcome())
        worker = build_worker(queue, agent)
        enqueue_analysis(queue, pairing)
        worker.run_once()

        enqueue_analysis(queue, pairing)
        worker.run_once()
        assert worker.statistics.reused == 1

        with queue() as session:
            stored = session.execute(select(MatchAnalysis)).scalars().one()
            stored.model_name = DETERMINISTIC_FALLBACK_MODEL
            session.commit()

        enqueue_analysis(queue, pairing)
        worker.run_once()

        assert worker.statistics.reused == 1
        assert len(agent.calls) == 2

    def test_an_analysis_for_unreadable_records_is_not_reused(
        self, queue: sessionmaker[Session]
    ) -> None:
        """Proves freshness is never assumed when it cannot be checked.

        A negative test: with no adopter or animal row to compare against,
        the worker recomputes rather than serving an explanation of facts it
        cannot confirm.
        """
        agent = CountingAgent(make_outcome())
        worker = build_worker(queue, agent)

        absent = Pairing(adopter_profile_id="adopter-1", animal_id="animal-1")
        enqueue_analysis(queue, absent)
        worker.run_once()
        enqueue_analysis(queue, absent)
        worker.run_once()

        assert len(agent.calls) == 2
        assert len(stored_analyses(queue)) == 1
        assert worker.statistics.reused == 0


class TestPersistedTrace:
    """Blueprint 6.2: the steps the agent took are auditable afterwards."""

    def test_the_reasoning_trace_is_stored_as_json(
        self, queue: sessionmaker[Session], pairing: Pairing
    ) -> None:
        """Proves the trace survives the process boundary for the interface.

        It used to be built and thrown away, which left the loop's own
        evidence unavailable to the screen that claims to show it.
        """
        enqueue_analysis(queue, pairing)
        build_worker(queue, CountingAgent(make_outcome())).run_once()

        stored = stored_analyses(queue)[0]
        assert stored.reasoning_trace is not None
        trace = json.loads(stored.reasoning_trace)
        assert [entry["step"] for entry in trace] == [1, 2]
        for entry in trace:
            assert set(entry) == {"step", "action", "detail"}

    def test_the_stored_evidence_keeps_its_cited_flag(
        self, queue: sessionmaker[Session], pairing: Pairing
    ) -> None:
        """Proves the interface can still tell cited sources from consulted ones."""
        enqueue_analysis(queue, pairing)
        build_worker(queue, CountingAgent(make_outcome())).run_once()

        sources = json.loads(stored_analyses(queue)[0].evidence_sources)
        assert sources == [{"kind": "rag", "reference": "guide.md#section", "cited": True}]


class TestStrandedJobs:
    """A worker crash must not strand a job forever."""

    def test_a_stranded_job_returns_to_the_queue_and_runs(
        self, queue: sessionmaker[Session], pairing: Pairing
    ) -> None:
        """Proves an abandoned claim is recoverable without an operator.

        The claim only ever looks at PENDING rows, so a worker killed
        mid-analysis would otherwise leave the adopter's explanation
        permanently unavailable and unreported.
        """
        job_id = enqueue_analysis(
            queue,
            pairing,
            status=AnalysisJobStatus.IN_PROGRESS,
            attempt_count=1,
            started_at=_now() - timedelta(minutes=STRANDED_JOB_TIMEOUT_MINUTES + 5),
        )
        worker = build_worker(queue, CountingAgent(make_outcome()))

        assert worker.run_once() is True
        assert read_job(queue, job_id).status == AnalysisJobStatus.COMPLETED.value

    def test_a_recently_claimed_job_is_left_alone(
        self, queue: sessionmaker[Session], pairing: Pairing
    ) -> None:
        """Proves the sweep does not steal work from a healthy worker.

        A negative test: reclaiming a job that is still being analysed would
        produce two analyses and two events for one pairing.
        """
        job_id = enqueue_analysis(
            queue,
            pairing,
            status=AnalysisJobStatus.IN_PROGRESS,
            attempt_count=1,
            started_at=_now(),
        )
        worker = build_worker(queue, CountingAgent(make_outcome()))

        assert worker.run_once() is False
        assert read_job(queue, job_id).status == AnalysisJobStatus.IN_PROGRESS.value

    def test_a_stranded_job_out_of_attempts_is_failed_with_a_reason(
        self, queue: sessionmaker[Session], pairing: Pairing
    ) -> None:
        """Proves a repeatedly abandoned job is parked, not retried forever."""
        job_id = enqueue_analysis(
            queue,
            pairing,
            status=AnalysisJobStatus.IN_PROGRESS,
            attempt_count=MAX_JOB_ATTEMPTS,
            started_at=_now() - timedelta(hours=6),
        )
        worker = build_worker(queue, CountingAgent(make_outcome()))

        worker.run_once()

        job = read_job(queue, job_id)
        assert job.status == AnalysisJobStatus.FAILED.value
        assert job.error_message is not None
        assert "reclaimed" in job.error_message
