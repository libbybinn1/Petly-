"""Tests for the agent loop (course blueprint section 6, spec sections 11-13).

Per rule R3 these never assert on generated wording. The language model is
non-deterministic, so the assertions are about structure, constraints, tool
selection and grounding - the properties that must hold whatever the model
happens to say.
"""

from __future__ import annotations

from typing import Any

import pytest
from agent_service.llm_client import (
    LanguageModelUnavailableError,
    MalformedModelOutputError,
    StubLanguageModel,
)
from agent_service.loop import (
    MatchAnalysisAgent,
    MissingRecordError,
    build_adopter_facts,
    build_animal_facts,
    load_prompt,
)
from agent_service.rag.knowledge_base import KnowledgeChunk, RetrievedChunk
from agent_service.tools.web_search import (
    SearchDecision,
    SearchResult,
    StubSearchProvider,
    decide_whether_to_search,
)
from app.domain.enums import HomeType, MatchDirection, Species

pytestmark = pytest.mark.agent


ADOPTER_PAYLOAD: dict[str, Any] = {
    "found": True,
    "adopter_profile_id": "adopter-1",
    "home_type": "APARTMENT",
    "has_yard": False,
    "household_has_children": False,
    "youngest_child_age": None,
    "has_other_animals": False,
    "experience_level": "NONE",
    "activity_level": "LOW",
    "daily_hours_available": 1.5,
    "city": "Haifa",
    "preferred_species": ["CAT"],
    "open_to_proactive_suggestions": True,
    "is_complete": True,
}

ANIMAL_PAYLOAD: dict[str, Any] = {
    "found": True,
    "animal_id": "animal-1",
    "name": "Luna",
    "species": "DOG",
    "breed": "Border Collie",
    "age_years": 2.0,
    "size": "MEDIUM",
    "temperament": "ENERGETIC",
    "activity_level": "HIGH",
    "good_with_children": True,
    "good_with_other_animals": True,
    "has_special_needs": False,
    "required_space": "LARGE",
    "city": "Haifa",
    "status": "AVAILABLE",
    "description": "Needs a job to do.",
}


class FakeMcpClient:
    """Stands in for the MCP client, recording which tools were called."""

    def __init__(
        self,
        adopter: dict[str, Any] | None = None,
        animal: dict[str, Any] | None = None,
    ) -> None:
        """Configure the payloads the fake will return."""
        self._adopter = adopter if adopter is not None else dict(ADOPTER_PAYLOAD)
        self._animal = animal if animal is not None else dict(ANIMAL_PAYLOAD)
        self.calls: list[str] = []

    def get_adopter_profile(self, adopter_profile_id: str) -> dict[str, Any]:
        """Record the call and return the configured adopter payload."""
        self.calls.append(f"get_adopter_profile({adopter_profile_id})")
        return self._adopter

    def get_animal_profile(self, animal_id: str) -> dict[str, Any]:
        """Record the call and return the configured animal payload."""
        self.calls.append(f"get_animal_profile({animal_id})")
        return self._animal


class FakeKnowledgeBase:
    """Returns a fixed set of retrieved chunks."""

    def __init__(self, chunks: list[RetrievedChunk] | None = None) -> None:
        """Configure what retrieval will return."""
        self._chunks = chunks or []
        self.queries: list[str] = []

    def search_relevant(self, query: str, result_count: int = 4) -> list[RetrievedChunk]:
        """Record the query and return the configured chunks."""
        self.queries.append(query)
        return self._chunks


def make_chunk(citation_document: str = "space-and-housing.md") -> RetrievedChunk:
    """Build one retrieved chunk for tests.

    It names the test animal's breed, so by default the curated guides
    *cover* the animal and the web gate stays shut (spec section 13). A test
    about a knowledge gap builds its own chunk that does not.
    """
    return RetrievedChunk(
        chunk=KnowledgeChunk(
            chunk_id="c1",
            document_name=citation_document,
            heading="Space: Apartments",
            text=(
                "Apartments suit small animals; large breeds need outdoor access. "
                "A Border Collie needs hours of daily work."
            ),
        ),
        distance=120.0,
    )


def build_agent(
    language_model: Any = None,  # noqa: ANN401 - any LanguageModel-shaped double
    knowledge_base: Any = None,  # noqa: ANN401
    mcp_client: Any = None,  # noqa: ANN401
    search_provider: Any = None,  # noqa: ANN401
) -> MatchAnalysisAgent:
    """Assemble an agent from test doubles."""
    return MatchAnalysisAgent(
        language_model=language_model or StubLanguageModel(),
        knowledge_base=knowledge_base or FakeKnowledgeBase([make_chunk()]),
        mcp_client=mcp_client or FakeMcpClient(),
        search_provider=search_provider or StubSearchProvider(),
    )


class TestOutputStructure:
    """The analysis must always have the declared shape (blueprint 6.3)."""

    def test_analysis_has_every_declared_field(self) -> None:
        """Proves the output contract in docs/AGENT.md section 5 is honoured."""
        outcome = build_agent().analyse("adopter-1", "animal-1", MatchDirection.ADOPTER_TO_ANIMAL)

        assert isinstance(outcome.score.score, int)
        assert isinstance(outcome.reasons, list)
        assert isinstance(outcome.concerns, list)
        assert isinstance(outcome.missing_information, list)
        assert isinstance(outcome.evidence_sources, list)
        assert isinstance(outcome.used_web_search, bool)
        assert outcome.model_name

    def test_score_stays_in_range(self) -> None:
        """Proves the stored score can never violate the database CHECK."""
        outcome = build_agent().analyse("adopter-1", "animal-1", MatchDirection.ADOPTER_TO_ANIMAL)
        assert 0 <= outcome.score.score <= 100

    def test_model_cannot_alter_the_score(self) -> None:
        """Proves the model's output never overrides the calculated score.

        This is the core guarantee of spec section 8. Even a model insisting
        the score is 99 cannot change what is stored.
        """
        liar = StubLanguageModel(
            canned_response={
                "score": 99,
                "reasons": ["Everything is wonderful."],
                "concerns": [],
                "missing_information": [],
            }
        )
        outcome = build_agent(language_model=liar).analyse(
            "adopter-1", "animal-1", MatchDirection.ADOPTER_TO_ANIMAL
        )

        assert outcome.score.score != 99

    def test_field_lengths_are_capped(self) -> None:
        """Proves a rambling model cannot flood the database or the interface."""
        verbose = StubLanguageModel(
            canned_response={
                "reasons": [f"Reason {index}." for index in range(40)],
                "concerns": [f"Concern {index}." for index in range(40)],
                "missing_information": [f"Gap {index}." for index in range(40)],
            }
        )
        outcome = build_agent(language_model=verbose).analyse(
            "adopter-1", "animal-1", MatchDirection.ADOPTER_TO_ANIMAL
        )

        assert len(outcome.reasons) <= 4
        assert len(outcome.concerns) <= 3
        assert len(outcome.missing_information) <= 3

    def test_string_instead_of_list_is_normalised(self) -> None:
        """Proves a model returning a bare string does not corrupt storage."""
        sloppy = StubLanguageModel(
            canned_response={
                "reasons": "A single sentence, not a list.",
                "concerns": None,
                "missing_information": [],
            }
        )
        outcome = build_agent(language_model=sloppy).analyse(
            "adopter-1", "animal-1", MatchDirection.ADOPTER_TO_ANIMAL
        )

        assert outcome.reasons == ["A single sentence, not a list."]
        assert outcome.concerns == []


class TestToolSelection:
    """The agent must obtain records through MCP, never elsewhere."""

    def test_both_mcp_tools_are_called(self) -> None:
        """Proves the agent fetches records through the tool boundary."""
        mcp_client = FakeMcpClient()
        build_agent(mcp_client=mcp_client).analyse(
            "adopter-1", "animal-1", MatchDirection.ADOPTER_TO_ANIMAL
        )

        assert any("get_adopter_profile" in call for call in mcp_client.calls)
        assert any("get_animal_profile" in call for call in mcp_client.calls)

    def test_knowledge_base_is_consulted(self) -> None:
        """Proves RAG participates in every assessment (blueprint section 7)."""
        knowledge_base = FakeKnowledgeBase([make_chunk()])
        build_agent(knowledge_base=knowledge_base).analyse(
            "adopter-1", "animal-1", MatchDirection.ADOPTER_TO_ANIMAL
        )

        assert knowledge_base.queries, "the agent never consulted the knowledge base"

    def test_knowledge_query_reflects_this_pairing(self) -> None:
        """Proves retrieval is about the specific match, not a generic query."""
        knowledge_base = FakeKnowledgeBase([make_chunk()])
        build_agent(knowledge_base=knowledge_base).analyse(
            "adopter-1", "animal-1", MatchDirection.ADOPTER_TO_ANIMAL
        )

        query = knowledge_base.queries[0].lower()
        assert "apartment" in query
        assert "dog" in query

    def test_missing_record_raises_rather_than_scoring_blind(self) -> None:
        """Proves the agent refuses to score on absent data.

        Scoring a record it could not read would produce a confident number
        from nothing, which is exactly what spec section 6.4 forbids.
        """
        absent = FakeMcpClient(animal={"found": False, "reason": "no such animal"})

        with pytest.raises(MissingRecordError):
            build_agent(mcp_client=absent).analyse(
                "adopter-1", "missing", MatchDirection.ADOPTER_TO_ANIMAL
            )


class TestWebSearchPolicy:
    """Spec section 13: search is gated, not automatic."""

    def test_no_search_when_knowledge_base_answers(self) -> None:
        """Proves RAG is preferred over the web for curated knowledge."""
        decision = decide_whether_to_search(
            "How much exercise does a collie need?",
            relevant_knowledge_found=True,
            already_searched_this_task=False,
        )
        assert decision is SearchDecision.REFUSED_RAG_SUFFICIENT
        assert not decision.is_allowed

    def test_search_allowed_when_knowledge_base_is_silent(self) -> None:
        """Proves a genuine knowledge gap opens the gate."""
        decision = decide_whether_to_search(
            "How much exercise does a collie need?",
            relevant_knowledge_found=False,
            already_searched_this_task=False,
        )
        assert decision is SearchDecision.ALLOWED_KNOWLEDGE_GAP
        assert decision.is_allowed

    def test_curated_guidance_outranks_an_external_sounding_topic(self) -> None:
        """Proves "RAG first, web second" is a rule and not a preference.

        The external-topic markers are keyword matches on a phrase the agent
        composed itself, and they used to be tested before RAG sufficiency:
        the single word "current" sent a question the knowledge base had
        already answered to the web, contradicting CLAUDE.md R4, the system
        prompt and the tool's own description. The decision recorded is now
        the refusal.
        """
        decision = decide_whether_to_search(
            "What are the current vaccination schedule requirements?",
            relevant_knowledge_found=True,
            already_searched_this_task=False,
        )
        assert decision is SearchDecision.REFUSED_RAG_SUFFICIENT
        assert not decision.is_allowed

    def test_an_external_topic_still_opens_the_gate_when_rag_is_silent(self) -> None:
        """Proves the external-topic rule survived, under the curated one.

        A current fact the guides do not cover is exactly what the web is
        for, and the decision says which kind of gap opened the gate so the
        reasoning trace records the distinction.
        """
        decision = decide_whether_to_search(
            "What are the current vaccination schedule requirements?",
            relevant_knowledge_found=False,
            already_searched_this_task=False,
        )
        assert decision is SearchDecision.ALLOWED_EXTERNAL_TOPIC
        assert decision.is_allowed

    def test_never_searches_for_the_applications_own_records(self) -> None:
        """Proves rule 3 of spec section 13, which holds even with no RAG hit."""
        decision = decide_whether_to_search(
            "What is in this adopter profile in our database?",
            relevant_knowledge_found=False,
            already_searched_this_task=False,
        )
        assert decision is SearchDecision.REFUSED_OWN_RECORDS
        assert not decision.is_allowed

    def test_does_not_search_twice_for_one_task(self) -> None:
        """Proves the loop cannot spend its budget repeating a search."""
        decision = decide_whether_to_search(
            "Anything at all", relevant_knowledge_found=False, already_searched_this_task=True
        )
        assert not decision.is_allowed

    def test_empty_question_is_refused(self) -> None:
        """Proves a blank question never reaches the search provider."""
        decision = decide_whether_to_search(
            "   ", relevant_knowledge_found=False, already_searched_this_task=False
        )
        assert decision is SearchDecision.REFUSED_EMPTY_QUESTION

    def test_agent_does_not_search_when_rag_suffices(self) -> None:
        """Proves the gate is actually wired into the loop, not just defined."""
        search_provider = StubSearchProvider()
        outcome = build_agent(
            knowledge_base=FakeKnowledgeBase([make_chunk()]),
            search_provider=search_provider,
        ).analyse("adopter-1", "animal-1", MatchDirection.ADOPTER_TO_ANIMAL)

        assert search_provider.queries == []
        assert outcome.used_web_search is False

    def test_agent_searches_when_knowledge_base_is_empty(self) -> None:
        """Proves the gate opens in the loop when RAG returns nothing."""
        search_provider = StubSearchProvider()
        build_agent(
            knowledge_base=FakeKnowledgeBase([]), search_provider=search_provider
        ).analyse("adopter-1", "animal-1", MatchDirection.ADOPTER_TO_ANIMAL)

        assert search_provider.queries, "the gate should have opened"


class TestGrounding:
    """Every cited source must be one the agent actually retrieved."""

    def test_evidence_lists_only_retrieved_sources(self) -> None:
        """Proves citations trace to real retrievals (spec section 6.4)."""
        chunk = make_chunk("multi-pet-households.md")
        outcome = build_agent(knowledge_base=FakeKnowledgeBase([chunk])).analyse(
            "adopter-1", "animal-1", MatchDirection.ADOPTER_TO_ANIMAL
        )

        assert outcome.evidence_sources
        for source in outcome.evidence_sources:
            assert source["kind"] in ("rag", "web")
            assert source["reference"]
        assert any(
            "multi-pet-households.md" in str(source["reference"])
            for source in outcome.evidence_sources
        )

    def test_no_evidence_when_nothing_was_retrieved(self) -> None:
        """Proves the agent does not fabricate sources when it found none."""
        outcome = build_agent(
            knowledge_base=FakeKnowledgeBase([]), search_provider=StubSearchProvider()
        ).analyse("adopter-1", "animal-1", MatchDirection.ADOPTER_TO_ANIMAL)

        assert outcome.evidence_sources == []

    def test_prompt_contains_only_supplied_material(self) -> None:
        """Proves the model is given an explicit, bounded set of facts."""
        model = StubLanguageModel()
        build_agent(language_model=model).analyse(
            "adopter-1", "animal-1", MatchDirection.ADOPTER_TO_ANIMAL
        )

        _system_prompt, user_prompt = model.prompts_seen[0]
        assert "Luna" in user_prompt
        assert "APARTMENT" in user_prompt
        assert "CALCULATED SCORE" in user_prompt
        # Match on intent rather than exact wording, so rephrasing the prompt
        # does not fail a test that is really about the instruction existing.
        assert "do not change it" in user_prompt.lower()

    def test_prompt_labels_whose_attribute_is_whose(self) -> None:
        """Proves each side's attributes are unambiguously owned.

        A prose paragraph mixing both sides led a small model to attribute the
        animal's energy level to the adopter and then contradict itself, so
        the prompt now labels every fact with whose it is.
        """
        model = StubLanguageModel()
        build_agent(language_model=model).analyse(
            "adopter-1", "animal-1", MatchDirection.ADOPTER_TO_ANIMAL
        )

        _system_prompt, user_prompt = model.prompts_seen[0]
        assert "Luna's ENERGY LEVEL: HIGH" in user_prompt
        assert "The adopter's OWN ENERGY LEVEL: LOW" in user_prompt


class TestFailureHandling:
    """Failures degrade; they do not crash the worker."""

    class UnavailableModel:
        """A model that is always unreachable."""

        @property
        def model_name(self) -> str:
            """Identifier for the unreachable model."""
            return "unreachable"

        def complete_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
            """Always fail as if the host were down."""
            raise LanguageModelUnavailableError("connection refused")

    class UnparseableModel:
        """A model that never returns valid JSON."""

        @property
        def model_name(self) -> str:
            """Identifier for the malfunctioning model."""
            return "unparseable"

        def complete_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
            """Always fail as if the output could not be parsed."""
            raise MalformedModelOutputError("not JSON")

    def test_unavailable_model_still_produces_an_analysis(self) -> None:
        """Proves a model outage costs prose, not function.

        The deterministic scorer already writes a sentence per criterion, so
        the system degrades to scores with plainer explanations rather than
        to no service.
        """
        outcome = build_agent(language_model=self.UnavailableModel()).analyse(
            "adopter-1", "animal-1", MatchDirection.ADOPTER_TO_ANIMAL
        )

        assert 0 <= outcome.score.score <= 100
        assert outcome.model_name == "deterministic-fallback"
        assert not outcome.explanation_is_generated
        assert outcome.reasons or outcome.concerns

    def test_unparseable_output_falls_back_cleanly(self) -> None:
        """Proves malformed JSON does not propagate out of the loop."""
        outcome = build_agent(language_model=self.UnparseableModel()).analyse(
            "adopter-1", "animal-1", MatchDirection.ADOPTER_TO_ANIMAL
        )

        assert outcome.model_name == "deterministic-fallback"
        assert isinstance(outcome.reasons, list)


class TestAgentLimitations:
    """The binding rules from spec section 6.4."""

    def test_agent_exposes_no_decision_capability(self) -> None:
        """Proves the agent cannot finalise an adoption (spec 6.4, rule R4).

        There is no approve, reject or decide method on the agent at all -
        the capability is absent rather than merely unused.
        """
        forbidden = ("approve", "reject", "decide", "finalise", "finalize", "adopt")
        exposed = [name for name in dir(MatchAnalysisAgent) if not name.startswith("_")]

        for name in exposed:
            assert not any(verb in name.lower() for verb in forbidden), (
                f"MatchAnalysisAgent.{name} looks like a decision-making capability"
            )

    def test_outcome_carries_no_decision_field(self) -> None:
        """Proves the result records an assessment, not a verdict."""
        outcome = build_agent().analyse("adopter-1", "animal-1", MatchDirection.ADOPTER_TO_ANIMAL)

        for forbidden_field in ("approved", "decision", "verdict", "accepted"):
            assert not hasattr(outcome, forbidden_field)

    def test_system_prompt_forbids_deciding_and_inventing(self) -> None:
        """Proves the prompt states the limitations, not only the code."""
        system_prompt = load_prompt("explanation_system.md").lower()

        assert "never produce a score" in system_prompt
        assert "never make the adoption decision" in system_prompt
        assert "never state a fact" in system_prompt


class TestPayloadConversion:
    """Tool payloads convert safely into domain facts."""

    def test_valid_payload_converts(self) -> None:
        """Proves a well-formed payload maps onto the domain types."""
        adopter = build_adopter_facts(ADOPTER_PAYLOAD)
        animal = build_animal_facts(ANIMAL_PAYLOAD)

        assert adopter.home_type is HomeType.APARTMENT
        assert adopter.preferred_species == frozenset({Species.CAT})
        assert animal.species is Species.DOG

    def test_unknown_enum_value_falls_back_instead_of_raising(self) -> None:
        """Proves one unexpected string cannot stall the whole queue."""
        animal = build_animal_facts({**ANIMAL_PAYLOAD, "species": "DRAGON"})
        assert animal.species is Species.OTHER

    def test_absent_fields_use_safe_defaults(self) -> None:
        """Proves a sparse payload converts without raising."""
        adopter = build_adopter_facts({"found": True})

        assert adopter.daily_hours_available == 0.0
        assert adopter.preferred_species == frozenset()
        assert adopter.is_complete is False


class TestSearchResultShape:
    """Search results carry what a citation needs."""

    def test_stub_provider_records_queries(self) -> None:
        """Proves the offline provider is inspectable by tests."""
        provider = StubSearchProvider()
        assert provider.search("anything", max_results=3) == []
        assert provider.queries == ["anything"]

    def test_result_fields_are_present(self) -> None:
        """Proves a result carries a citable URL alongside its text."""
        result = SearchResult(title="T", url="https://example.org", snippet="S")
        assert result.url.startswith("https://")
