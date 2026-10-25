"""Tests for semantic adopter search in the agent process (requirement 6).

Two layers. The `ProfileIndex` runs against a real persistent Chroma client
in a temporary directory, with a deterministic bag-of-words embedder that
counts its calls, so the hash-skip and opt-out removal are observable. The
worker branch runs the real `AgentWorker` against an in-memory SQLite queue
with the real schema. No Ollama, no network.

Nothing here asserts on generated text: there is none. The assertions are on
what is stored - the payload's shape, the population it covers, and the
similarity order the vectors imply.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from agent_service.rag.profile_index import ProfileDocument, ProfileIndex
from agent_service.rag.profile_population import discoverable_profile_documents
from agent_service.worker import AgentWorker
from app.config import AgentConfiguration, Configuration, DatabaseConfiguration
from app.cqrs.queries.match_queries import FindMoreAdoptersHandler
from app.domain.enums import (
    ActivityLevel,
    AnalysisJobStatus,
    AnalysisJobType,
    AnimalSize,
    AnimalStatus,
    ApplicationStatus,
    Species,
    Temperament,
    UserRole,
)
from app.domain.semantic_profiles import payload_to_semantic_search
from app.infrastructure.database import Base
from app.infrastructure.models import (
    AdopterProfile,
    AdoptionApplication,
    AnalysisJob,
    Animal,
    DomainEvent,
    MatchAnalysis,
    User,
    new_identifier,
)
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from tests.agent.test_worker_jobs import CountingAgent, make_outcome

pytestmark = pytest.mark.agent

VECTOR_DIMENSIONS = 64
EMBEDDING_MODEL = "fake-bag-of-words"


def _now() -> datetime:
    """Naive UTC, matching the application's stored timestamps."""
    return datetime.now(UTC).replace(tzinfo=None)


class BagOfWordsEmbedder:
    """A deterministic embedder: word counts hashed into a fixed vector.

    Texts that share words point the same way, which is all a similarity
    test needs, and every call is counted so a test can prove that an
    unchanged profile was not embedded again.
    """

    def __init__(self) -> None:
        """Start with no calls recorded."""
        self.embedded_texts: list[str] = []

    def embed(self, text: str) -> list[float]:
        """Embed one text, recording it."""
        self.embedded_texts.append(text)
        vector = [0.0] * VECTOR_DIMENSIONS
        # A constant component keeps every vector non-zero, which cosine
        # distance requires.
        vector[0] = 0.01
        for word in text.lower().split():
            cleaned = "".join(character for character in word if character.isalnum())
            if not cleaned:
                continue
            bucket = int(hashlib.md5(cleaned.encode()).hexdigest(), 16) % VECTOR_DIMENSIONS
            vector[bucket] += 1.0
        return vector

    def embed_all(self, texts: list[str]) -> list[list[float]]:
        """Embed several texts in order."""
        return [self.embed(text) for text in texts]


@pytest.fixture
def embedder() -> BagOfWordsEmbedder:
    """A fresh counting embedder."""
    return BagOfWordsEmbedder()


@pytest.fixture
def profile_index(tmp_path: Path, embedder: BagOfWordsEmbedder) -> ProfileIndex:
    """A profile index over a throwaway Chroma directory."""
    return ProfileIndex(
        persist_directory=tmp_path / "chroma",
        collection_name="test_profiles",
        embedding_client=embedder,
        embedding_model=EMBEDDING_MODEL,
    )


DOG_LOVER = ProfileDocument("dog-lover", "active runner wants an energetic large dog for hiking")
CAT_PERSON = ProfileDocument("cat-person", "quiet apartment wants a calm indoor cat for company")
RABBIT_FAN = ProfileDocument("rabbit-fan", "family garden wants a gentle small rabbit")


class TestProfileIndex:
    """The index embeds only what changed and holds only who is discoverable."""

    def test_a_first_sync_embeds_every_profile(
        self, profile_index: ProfileIndex, embedder: BagOfWordsEmbedder
    ) -> None:
        """Proves the résumés really are stored as vectors (requirement 6)."""
        report = profile_index.sync([DOG_LOVER, CAT_PERSON])

        assert report.embedded_count == 2
        assert profile_index.count() == 2
        assert len(embedder.embedded_texts) == 2

    def test_an_unchanged_profile_is_not_embedded_again(
        self, profile_index: ProfileIndex, embedder: BagOfWordsEmbedder
    ) -> None:
        """Proves the content hash skips work: a second sync embeds nothing."""
        profile_index.sync([DOG_LOVER, CAT_PERSON])
        embedder.embedded_texts.clear()

        report = profile_index.sync([DOG_LOVER, CAT_PERSON])

        assert report.embedded_count == 0
        assert report.unchanged_count == 2
        assert embedder.embedded_texts == []

    def test_only_the_changed_profile_is_embedded_again(
        self, profile_index: ProfileIndex, embedder: BagOfWordsEmbedder
    ) -> None:
        """Proves an edited profile is re-embedded and its neighbours are not."""
        profile_index.sync([DOG_LOVER, CAT_PERSON])
        embedder.embedded_texts.clear()
        edited = ProfileDocument("cat-person", "quiet house wants two calm cats")

        report = profile_index.sync([DOG_LOVER, edited])

        assert report.embedded_count == 1
        assert embedder.embedded_texts == [edited.text]

    def test_a_profile_that_leaves_the_population_is_removed(
        self, profile_index: ProfileIndex
    ) -> None:
        """Negative test: an adopter who opts out stops being searchable."""
        profile_index.sync([DOG_LOVER, CAT_PERSON])

        report = profile_index.sync([DOG_LOVER])

        assert report.removed_count == 1
        assert profile_index.count() == 1
        found = profile_index.nearest("calm indoor cat", 5)
        assert [match.adopter_profile_id for match in found] == ["dog-lover"]

    def test_the_nearest_profile_is_the_one_closest_in_meaning(
        self, profile_index: ProfileIndex
    ) -> None:
        """Proves the search ranks by vector similarity, nearest first."""
        profile_index.sync([DOG_LOVER, CAT_PERSON, RABBIT_FAN])

        found = profile_index.nearest("an energetic large dog who loves hiking", 3)

        assert found[0].adopter_profile_id == "dog-lover"
        similarities = [match.similarity for match in found]
        assert similarities == sorted(similarities, reverse=True)
        assert all(-1.0 <= similarity <= 1.0 for similarity in similarities)

    def test_excluded_profiles_never_appear(self, profile_index: ProfileIndex) -> None:
        """Proves applicants can be left out without shortening the page."""
        profile_index.sync([DOG_LOVER, CAT_PERSON, RABBIT_FAN])

        found = profile_index.nearest(
            "an energetic large dog", 2, excluded_ids=frozenset({"dog-lover"})
        )

        assert len(found) == 2
        assert "dog-lover" not in {match.adopter_profile_id for match in found}

    def test_an_empty_index_or_query_finds_nothing(self, profile_index: ProfileIndex) -> None:
        """Negative test: nothing to search, or nothing to search for, is no matches."""
        assert profile_index.nearest("a dog", 5) == []
        profile_index.sync([DOG_LOVER])
        assert profile_index.nearest("   ", 5) == []


# --------------------------------------------------------------------------
# The worker branch
# --------------------------------------------------------------------------


def make_configuration() -> Configuration:
    """Worker configuration; nothing here polls or reaches a model."""
    return Configuration(
        database=DatabaseConfiguration(server="u", database="u", user="u", password="u"),
        agent=AgentConfiguration(
            ollama_base_url="http://localhost:11434",
            chat_model="stub",
            fast_chat_model="stub",
            embedding_model=EMBEDDING_MODEL,
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


@pytest.fixture
def queue() -> Iterator[sessionmaker[Session]]:
    """A throwaway SQLite job queue with the real schema."""
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    yield sessionmaker(engine, expire_on_commit=False)
    engine.dispose()


def add_adopter(
    session: Session,
    label: str,
    *,
    activity_level: ActivityLevel = ActivityLevel.MODERATE,
    is_open: bool = True,
    is_complete: bool = True,
    is_active: bool = True,
) -> str:
    """Insert one adopter account and profile; return the profile id."""
    user = User(
        user_id=new_identifier(), email=f"{label}@test.test", password_hash="hash",
        full_name=label.title(), role=UserRole.ADOPTER.value,
        is_active=is_active, created_at=_now(),
    )
    profile = AdopterProfile(
        adopter_profile_id=new_identifier(), user_id=user.user_id,
        home_type="HOUSE", has_yard=True, household_has_children=False,
        has_other_animals=False,
        experience_level="SOME", activity_level=activity_level.value,
        daily_hours_available=4.0, city="Haifa",
        open_to_proactive_suggestions=is_open, is_complete=is_complete,
        created_at=_now(), updated_at=_now(),
    )
    session.add_all([user, profile])
    return profile.adopter_profile_id


def add_animal(session: Session) -> str:
    """Insert one available dog with a description; return its id."""
    animal = Animal(
        animal_id=new_identifier(), name="Bolt", species=Species.DOG.value,
        age_years=3.0, size=AnimalSize.LARGE.value,
        temperament=Temperament.ENERGETIC.value, activity_level=ActivityLevel.HIGH.value,
        good_with_children=True, good_with_other_animals=True, has_special_needs=False,
        required_space=AnimalSize.LARGE.value, city="Haifa",
        status=AnimalStatus.AVAILABLE.value,
        description="A high energy dog who needs long runs every day.",
        created_at=_now(), updated_at=_now(),
    )
    session.add(animal)
    return animal.animal_id


def add_search_job(session_factory: sessionmaker[Session], animal_id: str | None) -> str:
    """Queue one SEMANTIC_ADOPTER_SEARCH job; return its id."""
    job_id = new_identifier()
    with session_factory() as session:
        session.add(
            AnalysisJob(
                analysis_job_id=job_id,
                job_type=AnalysisJobType.SEMANTIC_ADOPTER_SEARCH.value,
                status=AnalysisJobStatus.PENDING.value,
                animal_id=animal_id,
                attempt_count=0,
                created_at=_now(),
            )
        )
        session.commit()
    return job_id


def stored_job(session_factory: sessionmaker[Session], job_id: str) -> AnalysisJob:
    """Read one job back."""
    with session_factory() as session:
        job = session.get(AnalysisJob, job_id)
        assert job is not None
        return job


@pytest.fixture
def population(queue: sessionmaker[Session]) -> dict[str, str]:
    """An animal, three discoverable adopters, an applicant and three hidden ones."""
    with queue() as session:
        identifiers = {
            "animal": add_animal(session),
            "runner": add_adopter(session, "runner", activity_level=ActivityLevel.HIGH),
            "walker": add_adopter(session, "walker"),
            "sofa": add_adopter(session, "sofa", activity_level=ActivityLevel.LOW),
            "applicant": add_adopter(session, "applicant", activity_level=ActivityLevel.HIGH),
            "opted_out": add_adopter(session, "optedout", is_open=False),
            "incomplete": add_adopter(session, "incomplete", is_complete=False),
            "inactive": add_adopter(session, "inactive", is_active=False),
        }
        session.flush()
        session.add(
            AdoptionApplication(
                application_id=new_identifier(),
                adopter_profile_id=identifiers["applicant"],
                animal_id=identifiers["animal"],
                status=ApplicationStatus.SUBMITTED.value,
                submitted_at=_now(),
            )
        )
        session.commit()
    return identifiers


def make_worker(
    queue: sessionmaker[Session], profile_index: ProfileIndex | None
) -> AgentWorker:
    """A worker whose analysis agent must never be called by these jobs."""
    return AgentWorker(
        make_configuration(), queue, CountingAgent(make_outcome()), profile_index=profile_index
    )


class TestSemanticSearchJobs:
    """The worker answers a SEMANTIC_ADOPTER_SEARCH job on its own row."""

    def test_a_search_completes_with_the_documented_payload(
        self,
        queue: sessionmaker[Session],
        population: dict[str, str],
        profile_index: ProfileIndex,
    ) -> None:
        """Proves the job stores ranked {adopter_profile_id, similarity} as JSON."""
        job_id = add_search_job(queue, population["animal"])

        assert make_worker(queue, profile_index).run_once() is True

        job = stored_job(queue, job_id)
        assert job.status == AnalysisJobStatus.COMPLETED.value
        assert job.result_payload is not None
        raw = json.loads(job.result_payload)
        assert set(raw) == {"matches", "indexed_profile_count", "embedding_model"}
        outcome = payload_to_semantic_search(raw)
        assert outcome.embedding_model == EMBEDDING_MODEL
        assert all(math.isfinite(match.similarity) for match in outcome.matches)

    def test_the_search_covers_exactly_the_discoverable_non_applicants(
        self,
        queue: sessionmaker[Session],
        population: dict[str, str],
        profile_index: ProfileIndex,
    ) -> None:
        """Proves opted-out, incomplete, inactive and applicant adopters never appear."""
        job_id = add_search_job(queue, population["animal"])
        make_worker(queue, profile_index).run_once()

        payload = stored_job(queue, job_id).result_payload
        assert payload is not None
        outcome = payload_to_semantic_search(json.loads(payload))
        found = {match.adopter_profile_id for match in outcome.matches}

        assert found == {population["runner"], population["walker"], population["sofa"]}
        assert outcome.indexed_profile_count == 4  # applicant indexed, then excluded

    def test_the_population_matches_find_more_adopters(
        self, queue: sessionmaker[Session], population: dict[str, str]
    ) -> None:
        """Proves the agent's index population is the web tier's discovery population.

        The selection is restated in the agent because R2 forbids it from
        importing a query handler; this pins the two to each other.
        """
        with queue() as session:
            indexed = {
                document.adopter_profile_id
                for document in discoverable_profile_documents(session)
            }
            eligible = {
                profile.adopter_profile_id
                for profile in FindMoreAdoptersHandler._eligible_profiles(session, set())
            }

        assert indexed == eligible

    def test_a_search_writes_no_analysis_and_no_event(
        self,
        queue: sessionmaker[Session],
        population: dict[str, str],
        profile_index: ProfileIndex,
    ) -> None:
        """Proves similarity never becomes a score or a decision (rule R4)."""
        add_search_job(queue, population["animal"])
        worker = make_worker(queue, profile_index)
        worker.run_once()

        with queue() as session:
            assert session.execute(select(MatchAnalysis)).first() is None
            assert session.execute(select(DomainEvent)).first() is None

    def test_a_job_without_an_animal_is_parked_with_a_reason(
        self, queue: sessionmaker[Session], profile_index: ProfileIndex
    ) -> None:
        """Negative test: an unrunnable job fails once, with why, and is not retried."""
        job_id = add_search_job(queue, None)
        worker = make_worker(queue, profile_index)
        worker.run_once()

        job = stored_job(queue, job_id)
        assert job.status == AnalysisJobStatus.FAILED.value
        assert job.error_message is not None
        assert "animal_id" in job.error_message
        assert worker.statistics.skipped == 1

    def test_a_job_for_a_missing_animal_is_parked(
        self, queue: sessionmaker[Session], profile_index: ProfileIndex
    ) -> None:
        """Negative test: an animal that does not exist cannot be searched for."""
        job_id = add_search_job(queue, new_identifier())
        make_worker(queue, profile_index).run_once()

        job = stored_job(queue, job_id)
        assert job.status == AnalysisJobStatus.FAILED.value
        assert job.error_message is not None
        assert "does not exist" in job.error_message

    def test_a_worker_without_an_index_refuses_the_job(
        self, queue: sessionmaker[Session], population: dict[str, str]
    ) -> None:
        """Negative test: no index configured is a clear failure, not a wrong answer."""
        job_id = add_search_job(queue, population["animal"])
        make_worker(queue, None).run_once()

        job = stored_job(queue, job_id)
        assert job.status == AnalysisJobStatus.FAILED.value
        assert job.result_payload is None

    def test_the_analysis_agent_is_never_called(
        self,
        queue: sessionmaker[Session],
        population: dict[str, str],
        profile_index: ProfileIndex,
    ) -> None:
        """Proves the semantic branch involves no language model at all."""
        agent = CountingAgent(make_outcome())
        add_search_job(queue, population["animal"])
        AgentWorker(make_configuration(), queue, agent, profile_index=profile_index).run_once()

        assert agent.calls == []
