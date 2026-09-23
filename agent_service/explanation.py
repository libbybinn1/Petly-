"""Building the model's prompt, and grounding what it replies.

Two responsibilities, both about prose rather than about control flow, which
is why they live beside the loop rather than inside it:

1. **What the model may reason from.** The prompt is the boundary of what
   the agent can truthfully say, so it is assembled explicitly: both records
   attribute by attribute, the calculated score, its per-criterion
   arithmetic, and every passage retrieved so far with the reference to cite
   it by.
2. **What survives.** Rule R4 says the agent never invents facts. A prompt
   saying so is not an enforcement mechanism - a small model will cite a
   plausible filename it never saw - so every citation is checked against
   what this task actually retrieved, and anything unaccounted for is
   dropped before it reaches the database or the screen.

When nothing usable survives, the deterministic criterion sentences are used
instead and the analysis is labelled as such. Text a model did not write is
never attributed to it (docs/AGENT.md section 10).
"""

from __future__ import annotations

import re
from typing import Any

from app.domain.matching import MatchScore

from agent_service.reasoning_session import ReasoningSession

MAX_REASONS = 4
MAX_CONCERNS = 3
MAX_MISSING_INFORMATION = 3
MAX_CITATIONS = 6

# A criterion at or above this reads as a genuine strength worth citing;
# below the lower bound it is a reservation worth raising. Used only by the
# fallback explanation, when no model is available to phrase them.
STRENGTH_SCORE = 80
CONCERN_SCORE = 60

# Recorded as the model name whenever the prose came from the deterministic
# criterion text rather than from a model. An audit column naming a live
# model for text that model did not write is worse than no column at all.
DETERMINISTIC_FALLBACK_MODEL = "deterministic-fallback"

# Passages are pasted into the prompt of a 3-billion-parameter model with a
# finite context window, so each one is previewed rather than included whole.
MAX_PASSAGE_PREVIEW_CHARACTERS = 400


def build_explanation_prompt(
    adopter_payload: dict[str, Any],
    animal_payload: dict[str, Any],
    score: MatchScore,
    session: ReasoningSession,
) -> str:
    """Assemble everything the model is allowed to reason from.

    Args:
        adopter_payload: The adopter record as the MCP tool returned it.
        animal_payload: The animal record as the MCP tool returned it.
        score: The calculated score and its breakdown.
        session: The task state, for the evidence already gathered.

    Returns:
        The first user message of the conversation.
    """
    return "\n".join(
        [
            *_animal_section(animal_payload),
            "",
            *_adopter_section(adopter_payload),
            "",
            "Note: ENERGY LEVEL appears for both. Never attribute one side's value "
            "to the other, and never contradict the values listed above.",
            "",
            f"=== CALCULATED SCORE: {score.score} out of 100 ===",
            f"Direction: {score.direction.value}. This number is final - explain "
            f"it, do not change it or restate it as a different number.",
            *_breakdown_section(score),
            *_evidence_section(session),
            "",
            "You may call a tool to gather more evidence, or answer now. When you "
            "answer, reply with a single JSON object holding reasons, concerns, "
            "missing_information and citations. Use only the information above and "
            "what your tool calls return.",
        ]
    )


def _animal_section(animal_payload: dict[str, Any]) -> list[str]:
    """Write the animal's attributes, each labelled with whose it is.

    A prose paragraph mixing both sides caused a small model to attribute the
    animal's energy level to the adopter and then contradict itself, so every
    fact now carries an unambiguous owner.

    Args:
        animal_payload: The animal record as the tool returned it.

    Returns:
        One line per attribute.
    """
    name = animal_payload.get("name")
    return [
        f"=== THE ANIMAL ({name}) ===",
        f"{name}'s species: {animal_payload.get('species')}",
        f"{name}'s breed: {animal_payload.get('breed') or 'unknown'}",
        f"{name}'s age: {animal_payload.get('age_years')} years",
        f"{name}'s size: {animal_payload.get('size')}",
        f"{name}'s temperament: {animal_payload.get('temperament')}",
        f"{name}'s ENERGY LEVEL: {animal_payload.get('activity_level')}",
        f"{name} is good with children: {animal_payload.get('good_with_children')}",
        f"{name} is good with other animals: {animal_payload.get('good_with_other_animals')}",
        f"{name}'s special needs: {animal_payload.get('special_needs_description') or 'none'}",
    ]


def _adopter_section(adopter_payload: dict[str, Any]) -> list[str]:
    """Write the adopter's attributes, each labelled with whose it is."""
    return [
        "=== THE ADOPTER (a person, not an animal) ===",
        f"The adopter's home: {adopter_payload.get('home_type')}",
        f"The adopter has a yard: {adopter_payload.get('has_yard')}",
        f"The adopter has children at home: "
        f"{adopter_payload.get('household_has_children')} "
        f"(youngest age: {adopter_payload.get('youngest_child_age') or 'not applicable'})",
        f"The adopter has other pets already: {adopter_payload.get('has_other_animals')}",
        f"The adopter's experience with animals: {adopter_payload.get('experience_level')}",
        f"The adopter's OWN ENERGY LEVEL: {adopter_payload.get('activity_level')}",
        f"The adopter's free time per day: {adopter_payload.get('daily_hours_available')} hours",
        f"The adopter's city: {adopter_payload.get('city')}",
    ]


def _breakdown_section(score: MatchScore) -> list[str]:
    """Write the per-criterion arithmetic the explanation has to match."""
    if score.is_disqualified:
        return ["", f"DISQUALIFIED: {score.disqualification_reason}"]

    return [
        "",
        "CRITERION BREAKDOWN:",
        *[
            f"  - {item.criterion.value}: {item.score}/100 "
            f"(weight {item.weight:.0%}) - {item.explanation}"
            for item in score.criterion_scores
        ],
    ]


def _evidence_section(session: ReasoningSession) -> list[str]:
    """Write the evidence gathered so far, with the references to cite."""
    lines: list[str] = []

    if session.retrieved:
        lines += ["", "RETRIEVED GUIDANCE (cite these by reference):"]
        lines += [
            f"  [{passage.chunk.citation}] {shorten(passage.chunk.text)}"
            for passage in session.retrieved
        ]
    else:
        lines += [
            "",
            "RETRIEVED GUIDANCE: none yet. Call rag_search if curated guidance "
            "would sharpen the assessment.",
        ]

    if session.search_results:
        lines += ["", "EXTERNAL SEARCH RESULTS (cite by URL):"]
        lines += [
            f"  [{result.url}] {result.title}: {shorten(result.snippet)}"
            for result in session.search_results
        ]

    if session.gaps:
        lines += ["", "ALREADY KNOWN TO BE MISSING:"]
        lines += [f"  - {gap}" for gap in session.gaps]

    return lines


def ground_explanation(
    answer: dict[str, Any],
    score: MatchScore,
    session: ReasoningSession,
    model_name: str,
) -> dict[str, Any]:
    """Keep only the parts of a model's answer that trace to real evidence.

    Args:
        answer: The model's parsed JSON answer.
        score: The calculated score, for the fallback text.
        session: The task state, holding the references actually retrieved.
            Receives a trace entry for anything dropped.
        model_name: The live model's identifier.

    Returns:
        The explanation fields, with `model_name` naming whoever really wrote
        the prose. When nothing survives grounding, the deterministic
        explanation.
    """
    known = session.known_references
    offered = _clean_sentences(answer.get("citations"), MAX_CITATIONS)
    citations = [citation for citation in offered if is_known_reference(citation, known)]
    _record_dropped_citations(offered, citations, session)

    reasons = _grounded_sentences(_clean_sentences(answer.get("reasons"), MAX_REASONS), known)
    concerns = _grounded_sentences(_clean_sentences(answer.get("concerns"), MAX_CONCERNS), known)

    if not reasons:
        # No usable prose survived. The criterion sentences the scorer already
        # wrote are a genuine explanation, so they are used instead - and the
        # analysis says plainly that no model wrote this text (FR-9.7).
        session.record("explanation_fallback", "no grounded reasons survived")
        return deterministic_explanation(score)

    return {
        "reasons": reasons,
        "concerns": concerns,
        "missing_information": _clean_sentences(
            answer.get("missing_information"), MAX_MISSING_INFORMATION
        ),
        "citations": citations,
        "model_name": model_name,
    }


def _record_dropped_citations(
    offered: list[str], kept: list[str], session: ReasoningSession
) -> None:
    """Record any citation that named a source the agent never retrieved."""
    dropped = [citation for citation in offered if citation not in kept]
    if not dropped:
        return

    session.record("dropped_citations", ", ".join(dropped))
    session.note_gap(
        "Part of the generated explanation cited a source the agent never "
        "retrieved, and was discarded."
    )


def _grounded_sentences(sentences: list[str], known_references: list[str]) -> list[str]:
    """Drop sentences citing a source the agent never retrieved.

    A sentence with no bracketed reference is kept: it is grounded in the
    records and criterion scores the prompt supplied. A sentence quoting
    `[some-guide.md]` that was never retrieved is not, and goes.

    Args:
        sentences: The model's cleaned sentences.
        known_references: Every reference actually retrieved this task.

    Returns:
        Only the sentences whose references all check out.
    """
    return [
        sentence
        for sentence in sentences
        if all(
            is_known_reference(reference, known_references)
            for reference in re.findall(r"\[([^\]]+)\]", sentence)
        )
    ]


def is_known_reference(reference: str, known_references: list[str]) -> bool:
    """Whether a reference names one of the sources actually retrieved.

    Compared loosely, because a model that writes `space-and-housing.md` for
    the retrieved `space-and-housing.md#apartments` has cited a real source,
    while an invented filename still matches nothing.

    Args:
        reference: What the model wrote.
        known_references: Every reference actually retrieved this task.

    Returns:
        True when the reference corresponds to a retrieved source.
    """
    candidate = reference.strip().strip("[]").lower()
    if not candidate:
        return False
    return any(
        candidate in known.lower() or known.lower() in candidate
        for known in known_references
        if known
    )


def deterministic_explanation(score: MatchScore) -> dict[str, Any]:
    """Build an explanation from the criterion text alone.

    Used when the model is unavailable, unparseable, out of steps, or wrote
    nothing that survived grounding. The scorer already writes a sentence per
    criterion, so this is a genuine explanation rather than a placeholder -
    it simply reads less fluently.

    A disqualified pairing is explained by its disqualification: that reason
    is the whole assessment, and FR-9.7 forbids showing a bare number. There
    is no column for it, so it is stored as the analysis's one concern.

    Args:
        score: The calculated score and its breakdown.

    Returns:
        The explanation fields, named as the deterministic fallback.
    """
    if score.is_disqualified:
        return {
            "reasons": [],
            "concerns": [score.disqualification_reason or "This pairing was disqualified."],
            "missing_information": [],
            "citations": [],
            "model_name": DETERMINISTIC_FALLBACK_MODEL,
        }

    ranked = sorted(score.criterion_scores, key=lambda item: item.score, reverse=True)
    strong = [item for item in ranked if item.score >= STRENGTH_SCORE]
    weak = [item for item in reversed(ranked) if item.score < CONCERN_SCORE]

    return {
        "reasons": [item.explanation for item in strong[:MAX_REASONS]],
        "concerns": [item.explanation for item in weak[:MAX_CONCERNS]],
        "missing_information": [],
        "citations": [],
        "model_name": DETERMINISTIC_FALLBACK_MODEL,
    }


def merge_missing_information(gaps: list[str], model_items: list[str]) -> list[str]:
    """Combine the gaps the agent observed with the ones the model named.

    The agent's own gaps come first: they are facts about the data, while the
    model's are its opinion of what it would have liked. Both are capped
    together, so a rambling model cannot flood the interface.

    Args:
        gaps: What the loop observed to be missing.
        model_items: What the model said was missing.

    Returns:
        The combined list, de-duplicated and capped.
    """
    combined: list[str] = []
    for item in [*gaps, *model_items]:
        if item and item not in combined:
            combined.append(item)
    return combined[:MAX_MISSING_INFORMATION]


def build_evidence(session: ReasoningSession, citations: list[str]) -> list[dict[str, object]]:
    """List every source the agent retrieved, marking the ones it cited.

    Spec section 6.4 requires sources to be recorded when they materially
    affect an explanation. Recording everything retrieved and flagging what
    was cited keeps both facts: what the agent read, and what it used.

    Args:
        session: The task state holding the retrieved evidence.
        citations: The references the model cited, already validated.

    Returns:
        One entry per source, each with `kind`, `reference` and `cited`.
    """
    sources = [
        *[("rag", passage.chunk.citation) for passage in session.retrieved],
        *[("web", result.url) for result in session.search_results],
    ]
    return [
        {
            "kind": kind,
            "reference": reference,
            "cited": is_known_reference(reference, citations),
        }
        for kind, reference in sources
    ]


def _clean_sentences(raw_value: object, limit: int) -> list[str]:
    """Coerce a model's list field into clean sentences.

    Models occasionally return a bare string, nested lists, or empty entries.
    Normalising here keeps that noise out of the database.

    Args:
        raw_value: Whatever the model put under the key.
        limit: How many entries to keep.

    Returns:
        Non-empty stripped strings, at most `limit` of them.
    """
    if isinstance(raw_value, str):
        candidates = [raw_value]
    elif isinstance(raw_value, list):
        candidates = [str(item) for item in raw_value]
    else:
        return []

    cleaned = [text.strip() for text in candidates if str(text).strip()]
    return cleaned[:limit]


def shorten(text: str, limit: int = MAX_PASSAGE_PREVIEW_CHARACTERS) -> str:
    """Collapse whitespace and trim text to a limit, marking what was cut.

    Args:
        text: The text to trim.
        limit: The maximum length to keep.

    Returns:
        The trimmed single-line text.
    """
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"
