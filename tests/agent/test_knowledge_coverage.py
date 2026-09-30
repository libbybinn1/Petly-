"""The web gate asks whether the guides cover *this animal* (spec section 13).

A situational question - "an energetic dog in an apartment" - always matches
some generic apartment or exercise guidance, so "retrieval returned
something" kept the web shut for every pairing, a Saluki the curated guides
never mention included. The gate now opens only when no retrieved passage
names the animal's breed (or species, when no breed is recorded).

Every test here uses doubles: no model, no vector store, no network. The
assertions are about which tools ran and what they were asked, never about
generated wording (rule R3).
"""

from __future__ import annotations

from typing import Any

import pytest
from agent_service.llm_client import StubLanguageModel
from agent_service.loop import build_animal_facts, knowledge_covers_animal
from agent_service.rag.knowledge_base import KnowledgeChunk, RetrievedChunk
from agent_service.reasoning_session import ReasoningSession
from agent_service.tools.web_search import SearchResult, StubSearchProvider
from app.domain.enums import MatchDirection

from tests.agent.test_agent_loop import (
    ADOPTER_PAYLOAD,
    ANIMAL_PAYLOAD,
    FakeKnowledgeBase,
    FakeMcpClient,
    build_agent,
)

pytestmark = pytest.mark.agent

WEB_RESULT = SearchResult(
    title="Saluki breed profile", url="https://example.test/saluki", snippet="Sighthound."
)


def chunk_saying(text: str, heading: str = "Dogs: exercise") -> RetrievedChunk:
    """One relevant retrieved passage with the given text."""
    return RetrievedChunk(
        chunk=KnowledgeChunk(
            chunk_id=f"c-{abs(hash(text))}",
            document_name="species-dogs.md",
            heading=heading,
            text=text,
        ),
        distance=100.0,
    )


def animal(**overrides: Any) -> dict[str, Any]:  # noqa: ANN401 - payload values vary
    """The test animal's MCP payload with some fields changed."""
    payload = dict(ANIMAL_PAYLOAD)
    payload.update(overrides)
    return payload


def analyse(
    knowledge_base: FakeKnowledgeBase,
    search_provider: StubSearchProvider,
    animal_payload: dict[str, Any] | None = None,
    language_model: StubLanguageModel | None = None,
) -> Any:  # noqa: ANN401 - returns an AnalysisOutcome
    """Run one analysis over doubles."""
    return build_agent(
        language_model=language_model,
        knowledge_base=knowledge_base,
        mcp_client=FakeMcpClient(animal=animal_payload),
        search_provider=search_provider,
    ).analyse("adopter-1", "animal-1", MatchDirection.ADOPTER_TO_ANIMAL)


class TestTheGateOpensForAnUncoveredBreed:
    """Positive half: a real knowledge gap reaches the web."""

    def test_generic_guidance_does_not_count_as_covering_a_breed(self) -> None:
        """Proves the web is searched when the guides never name the breed.

        The passage is relevant - it is about dogs and exercise - but says
        nothing about Salukis, which is exactly the gap spec 13 means.
        """
        search_provider = StubSearchProvider([WEB_RESULT])
        knowledge_base = FakeKnowledgeBase([chunk_saying("Most dogs need a daily walk.")])

        outcome = analyse(knowledge_base, search_provider, animal(breed="Saluki"))

        assert len(search_provider.queries) == 1
        assert "Saluki" in search_provider.queries[0]
        assert outcome.used_web_search is True
        assert any(
            source["kind"] == "web" for source in outcome.evidence_sources
        ), "the web result must be recorded as a source (rule R4)"

    def test_the_decision_is_recorded_in_the_trace(self) -> None:
        """Proves the reasoning trace says why the web was used."""
        outcome = analyse(
            FakeKnowledgeBase([chunk_saying("Most dogs need a daily walk.")]),
            StubSearchProvider([WEB_RESULT]),
            animal(breed="Saluki"),
        )

        coverage = [step for step in outcome.reasoning_trace if step.action == "knowledge_coverage"]
        assert coverage and "do not mention" in coverage[0].detail

    def test_the_breed_leads_the_knowledge_questions(self) -> None:
        """Proves retrieval is about the animal, not "a calm other"."""
        knowledge_base = FakeKnowledgeBase([])

        tortoise = animal(species="OTHER", breed="Greek Tortoise")

        analyse(knowledge_base, StubSearchProvider(), tortoise)

        assert knowledge_base.queries
        assert all("Greek Tortoise" in query for query in knowledge_base.queries)
        assert all(" other" not in query.lower() for query in knowledge_base.queries)


class TestTheGateStaysShutWhenTheGuidesAnswer:
    """Negative half: RAG first still means RAG first (CLAUDE.md R4)."""

    def test_a_passage_naming_the_breed_keeps_the_web_shut(self) -> None:
        """Proves a covered breed never reaches the web."""
        search_provider = StubSearchProvider([WEB_RESULT])

        outcome = analyse(
            FakeKnowledgeBase([chunk_saying("A Saluki is a sprinter and needs a fenced run.")]),
            search_provider,
            animal(breed="Saluki"),
        )

        assert search_provider.queries == []
        assert outcome.used_web_search is False

    def test_a_breed_inside_another_word_does_not_count(self) -> None:
        """Proves coverage matches whole words: "pugnacious" is not about pugs."""
        session = ReasoningSession()
        session.add_passages([chunk_saying("Some terriers can be pugnacious.")])

        assert not knowledge_covers_animal(session, build_animal_facts(ANIMAL_PAYLOAD), "Pug")

    def test_punctuation_in_a_breed_name_does_not_hide_coverage(self) -> None:
        """Proves "Hermann's Tortoise" matches however the guide wrote it."""
        session = ReasoningSession()
        session.add_passages([chunk_saying("Hermann\u2019s tortoise hibernates in winter.")])

        assert knowledge_covers_animal(
            session, build_animal_facts(animal(species="OTHER")), "Hermann's Tortoise"
        )

    def test_a_plural_in_the_guide_still_counts(self) -> None:
        """Proves "Border Collies" in a guide covers a Border Collie.

        Found against the real corpus: species-dogs.md says "Border Collies,
        Australian Shepherds and similar breeds", and an exact whole-word
        match missed it and sent a covered breed to the web.
        """
        session = ReasoningSession()
        session.add_passages([chunk_saying("Border Collies were bred to work all day.")])

        assert knowledge_covers_animal(
            session, build_animal_facts(ANIMAL_PAYLOAD), "Border Collie"
        )

    def test_nothing_retrieved_never_counts_as_covered(self) -> None:
        """Proves an empty retrieval is a gap even for an unnamed animal."""
        assert not knowledge_covers_animal(
            ReasoningSession(), build_animal_facts(animal(species="OTHER")), None
        )


class TestTheWebQueryCarriesNothingPersonal:
    """What leaves the machine is about the animal only."""

    def test_the_web_query_names_no_adopter_detail(self) -> None:
        """Proves the adopter's city and home never go into a web query.

        The query is sent to a third party. It needs the breed and nothing
        else, and it must not read as a request for our own records.
        """
        search_provider = StubSearchProvider([WEB_RESULT])

        analyse(FakeKnowledgeBase([]), search_provider, animal(breed="Saluki"))

        query = search_provider.queries[0].lower()
        assert str(ADOPTER_PAYLOAD["city"]).lower() not in query
        assert str(ADOPTER_PAYLOAD["home_type"]).lower() not in query
        assert "adopter" not in query


class TestTheModelIsGivenTheFitGrade:
    """The deductions reach the prompt, so the concerns can explain them."""

    def test_the_prompt_carries_the_grade_and_its_deductions(self) -> None:
        """Proves the model receives the arithmetic rather than inventing it.

        Checked on the prompt, not the reply: what the model writes is not
        assertable (rule R3), what it was given is.
        """
        model = StubLanguageModel()
        # The default test dog needs a large space and the adopter lives in
        # an apartment: disqualified, with no breakdown to grade.
        eligible = animal(size="MEDIUM", required_space="MEDIUM")

        analyse(FakeKnowledgeBase([]), StubSearchProvider(), eligible, language_model=model)

        prompts = " ".join(user_prompt for _system, user_prompt in model.prompts_seen)
        assert "FIT GRADE:" in prompts
        assert "minus " in prompts
