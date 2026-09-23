"""The agent's reason-act-observe loop.

Course blueprint section 6 requires a real agent rather than a single model
call: it plans, selects tools, observes results and revises its plan. This
module implements that cycle for one analysis task.

The division of labour is the important part, and it is not negotiable
(spec section 8, docs/AGENT.md section 1):

    deterministic Python  ->  eligibility, every criterion score, the total
    the language model    ->  the words explaining that total

So a model outage degrades PetMatch to *scores without prose*, not to no
service at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.domain.enums import (
    ActivityLevel,
    AnimalSize,
    ExperienceLevel,
    HomeType,
    MatchDirection,
    Species,
    Temperament,
)
from app.domain.matching import AdopterFacts, AnimalFacts, MatchScore, calculate_match_score

from agent_service.llm_client import (
    LanguageModel,
    LanguageModelUnavailableError,
    MalformedModelOutputError,
)
from agent_service.rag.knowledge_base import KnowledgeRetriever, RetrievedChunk
from agent_service.tools.mcp_tools import ProfileLookup
from agent_service.tools.web_search import (
    SearchDecision,
    SearchResult,
    WebSearchProvider,
    decide_whether_to_search,
)

PROMPT_DIRECTORY = Path(__file__).resolve().parent / "prompts"

MAX_REASONS = 4
MAX_CONCERNS = 3
MAX_MISSING_INFORMATION = 3

# A criterion at or above this reads as a genuine strength worth citing;
# below the lower bound it is a reservation worth raising. Used only by the
# fallback explanation, when no model is available to phrase them.
STRENGTH_SCORE = 80
CONCERN_SCORE = 60


@dataclass
class ReasoningStep:
    """One observable step of the loop, kept for auditability."""

    step_number: int
    action: str
    detail: str


@dataclass
class AnalysisOutcome:
    """Everything one completed analysis produced."""

    score: MatchScore
    reasons: list[str]
    concerns: list[str]
    missing_information: list[str]
    evidence_sources: list[dict[str, str]]
    used_web_search: bool
    model_name: str
    reasoning_trace: list[ReasoningStep] = field(default_factory=list)
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def explanation_is_generated(self) -> bool:
        """Whether a model produced the prose, or it fell back to criteria text."""
        return self.model_name != "deterministic-fallback"


class MissingRecordError(LookupError):
    """Raised when a record the task depends on does not exist."""


def load_prompt(file_name: str) -> str:
    """Read a versioned prompt file.

    Prompts live in version-controlled files rather than string literals, per
    spec section 16, so a change to the agent's instructions shows up in a
    diff like any other change.
    """
    return (PROMPT_DIRECTORY / file_name).read_text(encoding="utf-8")


class MatchAnalysisAgent:
    """Runs one matching analysis end to end."""

    def __init__(
        self,
        language_model: LanguageModel,
        knowledge_base: KnowledgeRetriever,
        mcp_client: ProfileLookup,
        search_provider: WebSearchProvider,
        max_reasoning_steps: int = 8,
    ) -> None:
        """Wire the agent to its tools."""
        self._language_model = language_model
        self._knowledge_base = knowledge_base
        self._mcp_client = mcp_client
        self._search_provider = search_provider
        self._max_reasoning_steps = max_reasoning_steps

    def analyse(
        self,
        adopter_profile_id: str,
        animal_id: str,
        direction: MatchDirection,
    ) -> AnalysisOutcome:
        """Assess one adopter against one animal.

        Args:
            adopter_profile_id: Whose profile to assess.
            animal_id: Which animal to assess them against.
            direction: Which weighting to apply.

        Returns:
            The score plus its grounded explanation.

        Raises:
            MissingRecordError: If either record cannot be retrieved.
        """
        trace: list[ReasoningStep] = []

        # Act: fetch both records through MCP tools. The agent never reads the
        # application's database directly (rule R2).
        adopter_payload = self._mcp_client.get_adopter_profile(adopter_profile_id)
        trace.append(ReasoningStep(1, "get_adopter_profile", _describe(adopter_payload)))

        animal_payload = self._mcp_client.get_animal_profile(animal_id)
        trace.append(ReasoningStep(2, "get_animal_profile", _describe(animal_payload)))

        if not adopter_payload.get("found") or not animal_payload.get("found"):
            raise MissingRecordError(
                f"adopter found={adopter_payload.get('found')}, "
                f"animal found={animal_payload.get('found')}"
            )

        adopter = build_adopter_facts(adopter_payload)
        animal = build_animal_facts(animal_payload)

        # Deterministic scoring. No model involved; this always succeeds.
        score = calculate_match_score(adopter, animal, direction)
        trace.append(
            ReasoningStep(
                3,
                "calculate_score",
                f"score={score.score} disqualified={score.is_disqualified}",
            )
        )

        # Act: consult the curated knowledge base.
        knowledge_question = _knowledge_question_for(adopter, animal)
        retrieved = self._knowledge_base.search_relevant(knowledge_question)
        trace.append(
            ReasoningStep(4, "rag_search", f"{len(retrieved)} relevant chunk(s)")
        )

        # Re-plan: only now, knowing whether RAG answered, decide about the web.
        search_results, decision = self._maybe_search_web(knowledge_question, retrieved)
        trace.append(ReasoningStep(5, "web_search_gate", decision.value))

        evidence = _build_evidence(retrieved, search_results)

        explanation = self._generate_explanation(
            adopter_payload, animal_payload, score, retrieved, search_results
        )
        trace.append(
            ReasoningStep(6, "generate_explanation", f"model={explanation['model_name']}")
        )

        return AnalysisOutcome(
            score=score,
            reasons=explanation["reasons"],
            concerns=explanation["concerns"],
            missing_information=explanation["missing_information"],
            evidence_sources=evidence,
            used_web_search=bool(search_results),
            model_name=explanation["model_name"],
            reasoning_trace=trace[: self._max_reasoning_steps],
        )

    def _maybe_search_web(
        self, question: str, retrieved: list[RetrievedChunk]
    ) -> tuple[list[SearchResult], SearchDecision]:
        """Apply the spec section 13 gate, and search only if it opens."""
        decision = decide_whether_to_search(
            question,
            relevant_knowledge_found=bool(retrieved),
            already_searched_this_task=False,
        )
        if not decision.is_allowed:
            return [], decision

        return self._search_provider.search(question, max_results=3), decision

    def _generate_explanation(
        self,
        adopter_payload: dict[str, Any],
        animal_payload: dict[str, Any],
        score: MatchScore,
        retrieved: list[RetrievedChunk],
        search_results: list[SearchResult],
    ) -> dict[str, Any]:
        """Ask the model to explain the score, falling back if it cannot.

        The fallback matters: the criterion explanations are already written
        by the deterministic scorer, so an unavailable model costs polish
        rather than function.
        """
        user_prompt = _build_explanation_prompt(
            adopter_payload, animal_payload, score, retrieved, search_results
        )

        try:
            raw = self._language_model.complete_json(
                load_prompt("explanation_system.md"), user_prompt
            )
        except (LanguageModelUnavailableError, MalformedModelOutputError):
            return _deterministic_explanation(score)

        return {
            "reasons": _clean_sentences(raw.get("reasons"), MAX_REASONS)
            or _deterministic_explanation(score)["reasons"],
            "concerns": _clean_sentences(raw.get("concerns"), MAX_CONCERNS),
            "missing_information": _clean_sentences(
                raw.get("missing_information"), MAX_MISSING_INFORMATION
            ),
            "model_name": self._language_model.model_name,
        }


# --------------------------------------------------------------------------
# Converting tool payloads into domain value objects
# --------------------------------------------------------------------------


def build_adopter_facts(payload: dict[str, Any]) -> AdopterFacts:
    """Convert an MCP adopter payload into domain facts.

    Unknown enum values fall back to a safe default rather than raising: a
    single unexpected string in one record should not stop the queue.
    """
    return AdopterFacts(
        home_type=_to_enum(HomeType, payload.get("home_type"), HomeType.APARTMENT),
        has_yard=bool(payload.get("has_yard")),
        household_has_children=bool(payload.get("household_has_children")),
        youngest_child_age=_to_optional_int(payload.get("youngest_child_age")),
        has_other_animals=bool(payload.get("has_other_animals")),
        experience_level=_to_enum(
            ExperienceLevel, payload.get("experience_level"), ExperienceLevel.NONE
        ),
        activity_level=_to_enum(
            ActivityLevel, payload.get("activity_level"), ActivityLevel.MODERATE
        ),
        daily_hours_available=float(payload.get("daily_hours_available") or 0.0),
        city=str(payload.get("city") or ""),
        preferred_species=_payload_species(payload.get("preferred_species")),
        open_to_proactive_suggestions=bool(payload.get("open_to_proactive_suggestions")),
        is_complete=bool(payload.get("is_complete")),
    )


def build_animal_facts(payload: dict[str, Any]) -> AnimalFacts:
    """Convert an MCP animal payload into domain facts."""
    return AnimalFacts(
        species=_to_enum(Species, payload.get("species"), Species.OTHER),
        age_years=float(payload.get("age_years") or 0.0),
        size=_to_enum(AnimalSize, payload.get("size"), AnimalSize.MEDIUM),
        temperament=_to_enum(Temperament, payload.get("temperament"), Temperament.BALANCED),
        activity_level=_to_enum(
            ActivityLevel, payload.get("activity_level"), ActivityLevel.MODERATE
        ),
        good_with_children=bool(payload.get("good_with_children")),
        good_with_other_animals=bool(payload.get("good_with_other_animals")),
        has_special_needs=bool(payload.get("has_special_needs")),
        required_space=_to_enum(
            AnimalSize, payload.get("required_space"), AnimalSize.MEDIUM
        ),
        city=str(payload.get("city") or ""),
    )


def _payload_species(raw_values: object) -> frozenset[Species]:
    """Read species preferences from an MCP payload, dropping unrecognised ones.

    The same rule as the web tier's `_stored_species`, and for the same
    reason: `Species.OTHER` is a preference an adopter can hold, not a
    stand-in for a value that failed to parse. Translating one into the
    other would hand the heaviest criterion a preference nobody expressed.

    Args:
        raw_values: Whatever the payload held, which may not be a list.

    Returns:
        The species that parsed, empty if the field was absent or malformed.
    """
    if not isinstance(raw_values, list):
        return frozenset()

    parsed: list[Species] = []
    for value in raw_values:
        try:
            parsed.append(Species(str(value).strip()))
        except ValueError:
            continue
    return frozenset(parsed)


def _to_optional_int(raw_value: object) -> int | None:
    """Coerce a payload value to a whole number, or None when unusable.

    Every other numeric field in the payload is coerced; this one was
    passed through untouched, so a JSON string age reached the scorer and
    was compared against an int - raising TypeError from inside the child
    safety rule and failing the whole analysis job. A field the agent
    cannot read should degrade to "not stated", not crash the queue.

    Args:
        raw_value: The payload value, of whatever type arrived.

    Returns:
        The value as an int, or None if absent or not numeric.
    """
    if raw_value is None:
        return None
    try:
        return int(float(str(raw_value)))
    except (TypeError, ValueError):
        return None


def _to_enum(enum_class: type, raw_value: object, default: Any) -> Any:  # noqa: ANN401
    """Convert a string to an enum member, falling back to a default."""
    if raw_value is None:
        return default
    try:
        return enum_class(str(raw_value))
    except ValueError:
        return default


# --------------------------------------------------------------------------
# Prompt and evidence assembly
# --------------------------------------------------------------------------


def _knowledge_question_for(adopter: AdopterFacts, animal: AnimalFacts) -> str:
    """Compose the question put to the knowledge base.

    Built from the pairing's actual characteristics, so retrieval is about
    this specific match rather than a generic query.
    """
    parts = [
        f"A {animal.temperament.value.lower()} {animal.species.value.replace('_', ' ').lower()}",
        f"with {animal.activity_level.value.lower()} activity needs",
        f"joining a {adopter.home_type.value.lower()} home",
        f"with {adopter.daily_hours_available:g} hours available daily",
    ]
    if adopter.household_has_children:
        parts.append("where children live")
    if adopter.has_other_animals:
        parts.append("that already has other animals")
    if animal.has_special_needs:
        parts.append("and the animal has special care needs")
    return ", ".join(parts) + "."


def _build_explanation_prompt(
    adopter_payload: dict[str, Any],
    animal_payload: dict[str, Any],
    score: MatchScore,
    retrieved: list[RetrievedChunk],
    search_results: list[SearchResult],
) -> str:
    """Assemble everything the model is allowed to reason from.

    Deliberately explicit: the model may only use what appears here, so the
    prompt is the boundary of what it can truthfully say.
    """
    # Attributes are written one per line and prefixed with whose they are.
    # A prose paragraph mixing both sides caused a small model to attribute
    # the animal's energy level to the adopter and then contradict itself, so
    # every fact now carries an unambiguous owner.
    animal_name = animal_payload.get("name")
    sections = [
        f"=== THE ANIMAL ({animal_name}) ===",
        f"{animal_name}'s species: {animal_payload.get('species')}",
        f"{animal_name}'s breed: {animal_payload.get('breed') or 'unknown'}",
        f"{animal_name}'s age: {animal_payload.get('age_years')} years",
        f"{animal_name}'s size: {animal_payload.get('size')}",
        f"{animal_name}'s temperament: {animal_payload.get('temperament')}",
        f"{animal_name}'s ENERGY LEVEL: {animal_payload.get('activity_level')}",
        f"{animal_name} is good with children: {animal_payload.get('good_with_children')}",
        f"{animal_name} is good with other animals: "
        f"{animal_payload.get('good_with_other_animals')}",
        f"{animal_name}'s special needs: "
        f"{animal_payload.get('special_needs_description') or 'none'}",
        "",
        "=== THE ADOPTER (a person, not an animal) ===",
        f"The adopter's home: {adopter_payload.get('home_type')}",
        f"The adopter has a yard: {adopter_payload.get('has_yard')}",
        f"The adopter has children at home: "
        f"{adopter_payload.get('household_has_children')} "
        f"(youngest age: {adopter_payload.get('youngest_child_age') or 'not applicable'})",
        f"The adopter has other pets already: {adopter_payload.get('has_other_animals')}",
        f"The adopter's experience with animals: {adopter_payload.get('experience_level')}",
        f"The adopter's OWN ENERGY LEVEL: {adopter_payload.get('activity_level')}",
        f"The adopter's free time per day: "
        f"{adopter_payload.get('daily_hours_available')} hours",
        f"The adopter's city: {adopter_payload.get('city')}",
        "",
        "Note: ENERGY LEVEL appears for both. Never attribute one side's value "
        "to the other, and never contradict the values listed above.",
        "",
        f"=== CALCULATED SCORE: {score.score} out of 100 ===",
        f"Direction: {score.direction.value}. This number is final - explain "
        f"it, do not change it or restate it as a different number.",
    ]

    if score.is_disqualified:
        sections += ["", f"DISQUALIFIED: {score.disqualification_reason}"]
    else:
        sections += ["", "CRITERION BREAKDOWN:"]
        sections += [
            f"  - {item.criterion.value}: {item.score}/100 "
            f"(weight {item.weight:.0%}) - {item.explanation}"
            for item in score.criterion_scores
        ]

    if retrieved:
        sections += ["", "RETRIEVED GUIDANCE (cite these by reference):"]
        sections += [
            f"  [{chunk.chunk.citation}] {chunk.chunk.text[:500]}" for chunk in retrieved
        ]

    if search_results:
        sections += ["", "EXTERNAL SEARCH RESULTS (cite by URL):"]
        sections += [
            f"  [{item.url}] {item.title}: {item.snippet[:300]}"
            for item in search_results
        ]

    sections += [
        "",
        "Explain this assessment as JSON with keys reasons, concerns and "
        "missing_information. Use only the information above.",
    ]
    return "\n".join(sections)


def _build_evidence(
    retrieved: list[RetrievedChunk], search_results: list[SearchResult]
) -> list[dict[str, str]]:
    """List every source that was available to the explanation."""
    evidence = [
        {"kind": "rag", "reference": chunk.chunk.citation} for chunk in retrieved
    ]
    evidence += [{"kind": "web", "reference": item.url} for item in search_results]
    return evidence


def _deterministic_explanation(score: MatchScore) -> dict[str, Any]:
    """Build an explanation from the criterion text alone.

    Used when the model is unavailable or unparseable. The scorer already
    writes a sentence per criterion, so this is a genuine explanation rather
    than a placeholder - it simply reads less fluently.
    """
    if score.is_disqualified:
        return {
            "reasons": [],
            "concerns": [score.disqualification_reason or "This pairing was disqualified."],
            "missing_information": [],
            "model_name": "deterministic-fallback",
        }

    ranked = sorted(score.criterion_scores, key=lambda item: item.score, reverse=True)
    strong = [item for item in ranked if item.score >= STRENGTH_SCORE]
    weak = [item for item in reversed(ranked) if item.score < CONCERN_SCORE]

    return {
        "reasons": [item.explanation for item in strong[:MAX_REASONS]],
        "concerns": [item.explanation for item in weak[:MAX_CONCERNS]],
        "missing_information": [],
        "model_name": "deterministic-fallback",
    }


def _clean_sentences(raw_value: object, limit: int) -> list[str]:
    """Coerce a model's list field into clean sentences.

    Models occasionally return a bare string, nested lists, or empty entries.
    Normalising here keeps that noise out of the database.
    """
    if isinstance(raw_value, str):
        candidates = [raw_value]
    elif isinstance(raw_value, list):
        candidates = [str(item) for item in raw_value]
    else:
        return []

    cleaned = [text.strip() for text in candidates if str(text).strip()]
    return cleaned[:limit]


def _describe(payload: dict[str, Any]) -> str:
    """Summarise a tool payload for the reasoning trace."""
    if payload.get("found"):
        return payload.get("name") or payload.get("adopter_profile_id") or "record returned"
    return str(payload.get("reason", "not found"))
