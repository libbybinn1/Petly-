"""Adopter "profile résumés" for vector search, and the job payload contract.

Lecturer requirement 6 asks that résumés be embedded in a vector database and
that a semantic search can match a candidate to a job. In PetMatch the
candidate is an adopter, the job is an animal, and the résumé is the adopter
profile, rendered here as plain language so an embedding model can read it.

Shared vocabulary for two processes, which is why it lives in the domain
(rule R2). The agent builds the texts, embeds them and writes the ranked
answer onto the `SEMANTIC_ADOPTER_SEARCH` job row; the web tier reads that
answer back through `payload_to_semantic_search`. Neither side imports the
other, and this module imports no framework and no model.

Two boundaries hold on both sides of the contract.

**Similarity is evidence, not eligibility.** A vector search knows nothing of
the hard rules in spec section 10. The screen shows each semantic match
beside the deterministic verdict, and only the deterministic scorer decides
whether somebody may be invited (spec section 8, rule R4).

**The résumé holds no identity.** Names, emails and account details are never
embedded. The text describes a household, and the profile identifier rides
alongside it as metadata, so the vector store holds nothing that identifies a
person on its own.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from app.domain.facts import (
    parse_number,
    parse_optional_whole_number,
    parse_text,
    split_stored_values,
)

# How many semantic matches one job records. Discovery shows ten
# deterministic candidates (DEFAULT_DISCOVERY_LIMIT), so the two panels
# answer at the same scale.
DEFAULT_SEMANTIC_MATCH_COUNT = 10

# The most matches a stored payload may carry back. A payload is data the web
# tier did not write, so its size is bounded rather than trusted.
MAX_DECODED_MATCH_COUNT = 50

# Cosine similarity is defined on [-1, 1]. Anything outside is not a
# similarity, so a decoded value beyond it is discarded rather than shown.
MINIMUM_SIMILARITY = -1.0
MAXIMUM_SIMILARITY = 1.0

# The profile fields a résumé is written from. Declared once so the agent
# reads exactly these off a row, and so a field added here is a field the
# content hash covers. Identity fields are absent on purpose.
ADOPTER_PROFILE_TEXT_FIELDS: tuple[str, ...] = (
    "home_type",
    "has_yard",
    "yard_size_sqm",
    "household_has_children",
    "youngest_child_age",
    "has_other_animals",
    "other_animals_description",
    "experience_level",
    "activity_level",
    "daily_hours_available",
    "city",
    "preferred_species",
    "preferred_size",
    "preferred_age_range",
)

# The animal fields the search query is written from.
ANIMAL_QUERY_TEXT_FIELDS: tuple[str, ...] = (
    "name",
    "species",
    "breed",
    "age_years",
    "size",
    "temperament",
    "activity_level",
    "good_with_children",
    "good_with_other_animals",
    "has_special_needs",
    "special_needs_description",
    "required_space",
    "city",
    "description",
)


# --------------------------------------------------------------------------
# The texts that are embedded
# --------------------------------------------------------------------------


def build_adopter_profile_text(record: Mapping[str, object]) -> str:
    """Write one adopter profile as a plain-language résumé (requirement 6).

    Deterministic: the same record always produces the same text, which is
    what lets `profile_content_hash` decide that an unchanged profile need
    not be embedded again. A field the adopter left empty is left out rather
    than written as "unknown", so absence does not become a claim.

    Args:
        record: The profile's fields, keyed by `ADOPTER_PROFILE_TEXT_FIELDS`.

    Returns:
        A short paragraph describing the household.
    """
    sentences = [
        _home_sentence(record),
        _household_sentence(record),
        _other_animals_sentence(record),
        _lifestyle_sentence(record),
        _preference_sentence(record),
    ]
    return " ".join(sentence for sentence in sentences if sentence)


def build_animal_query_text(record: Mapping[str, object]) -> str:
    """Write the text an animal is searched by: its facts and its description.

    The animal's own description is the richest text it has, so it is
    included verbatim; the structured facts lead, so an animal with no
    description still produces a usable query.

    Args:
        record: The animal's fields, keyed by `ANIMAL_QUERY_TEXT_FIELDS`.

    Returns:
        A short paragraph describing the animal and the home it needs.
    """
    sentences = [
        _animal_identity_sentence(record),
        _animal_temperament_sentence(record),
        _animal_compatibility_sentence(record),
        _animal_needs_sentence(record),
        parse_text(record.get("description")).strip(),
    ]
    return " ".join(sentence for sentence in sentences if sentence)


def profile_content_hash(profile_text: str, embedding_model: str) -> str:
    """Fingerprint a résumé so an unchanged profile is not re-embedded.

    The model name is part of the fingerprint: a vector made by a different
    embedding model lives in a different space, so changing the model must
    re-embed everything even though no profile changed.

    Args:
        profile_text: The output of `build_adopter_profile_text`.
        embedding_model: The embedding model's name.

    Returns:
        A hex SHA-256 digest.
    """
    digest = hashlib.sha256()
    digest.update(embedding_model.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(profile_text.encode("utf-8"))
    return digest.hexdigest()


def _humanize(raw_value: object) -> str:
    """Turn a stored enum value such as `GUINEA_PIG` into `guinea pig`."""
    return parse_text(raw_value).strip().replace("_", " ").lower()


def _home_sentence(record: Mapping[str, object]) -> str:
    """Describe where the adopter lives."""
    home = _humanize(record.get("home_type")) or "home"
    city = parse_text(record.get("city")).strip()
    sentence = f"Lives in a {home}" + (f" in {city}" if city else "")
    if not record.get("has_yard"):
        return sentence + " with no yard."
    yard_size = parse_optional_whole_number(record.get("yard_size_sqm"))
    if yard_size:
        return sentence + f" with a yard of about {yard_size} square metres."
    return sentence + " with a yard."


def _household_sentence(record: Mapping[str, object]) -> str:
    """Describe whether children live in the household."""
    if not record.get("household_has_children"):
        return "No children live in the household."
    youngest = parse_optional_whole_number(record.get("youngest_child_age"))
    if youngest is None:
        return "Children live in the household."
    return f"Children live in the household; the youngest is {youngest} years old."


def _other_animals_sentence(record: Mapping[str, object]) -> str:
    """Describe the animals already in the home."""
    if not record.get("has_other_animals"):
        return "There are no other pets at home."
    description = parse_text(record.get("other_animals_description")).strip()
    if not description:
        return "Already has other pets at home."
    return f"Already has other pets at home: {description}."


def _lifestyle_sentence(record: Mapping[str, object]) -> str:
    """Describe experience, activity and available time."""
    experience = _humanize(record.get("experience_level")) or "unstated"
    activity = _humanize(record.get("activity_level")) or "unstated"
    hours = parse_number(record.get("daily_hours_available"))
    return (
        f"Has {experience} experience caring for animals, a {activity} "
        f"activity level, and about {hours:g} hours a day available for a pet."
    )


def _preference_sentence(record: Mapping[str, object]) -> str:
    """Describe what kind of animal the adopter is hoping for."""
    stored_species = split_stored_values(parse_text(record.get("preferred_species")))
    species = [_humanize(value) for value in stored_species]
    size = _humanize(record.get("preferred_size"))
    age_range = parse_text(record.get("preferred_age_range")).strip()

    preferences: list[str] = []
    if species:
        preferences.append("a " + " or ".join(species))
    if size:
        preferences.append(f"{size} in size")
    if age_range and age_range.lower() != "any":
        preferences.append(f"aged {age_range}")
    if not preferences:
        return "Open to any kind of animal."
    return "Hoping to adopt " + ", ".join(preferences) + "."


def _animal_identity_sentence(record: Mapping[str, object]) -> str:
    """Name the animal, its species, breed, age and size."""
    name = parse_text(record.get("name")).strip() or "This animal"
    species = _humanize(record.get("species")) or "animal"
    breed = parse_text(record.get("breed")).strip()
    size = _humanize(record.get("size"))
    age = parse_number(record.get("age_years"))
    breed_part = f" ({breed})" if breed else ""
    size_part = f"{size} " if size else ""
    return f"{name} is a {age:g} year old {size_part}{species}{breed_part}."


def _animal_temperament_sentence(record: Mapping[str, object]) -> str:
    """Describe the animal's temperament and energy."""
    temperament = _humanize(record.get("temperament"))
    activity = _humanize(record.get("activity_level"))
    if not temperament and not activity:
        return ""
    return f"Temperament is {temperament or 'unstated'}, with {activity or 'unstated'} energy."


def _animal_compatibility_sentence(record: Mapping[str, object]) -> str:
    """Describe who the animal lives well with."""
    children = "good" if record.get("good_with_children") else "not suited to living"
    animals = "good" if record.get("good_with_other_animals") else "not suited to living"
    return f"It is {children} with children and {animals} with other animals."


def _animal_needs_sentence(record: Mapping[str, object]) -> str:
    """Describe the space and care the animal needs."""
    space = _humanize(record.get("required_space"))
    city = parse_text(record.get("city")).strip()
    sentence = f"Needs {space or 'some'} living space" + (f", located in {city}." if city else ".")
    if not record.get("has_special_needs"):
        return sentence
    needs = parse_text(record.get("special_needs_description")).strip()
    return sentence + (f" Special needs: {needs}." if needs else " Has special needs.")


# --------------------------------------------------------------------------
# The job payload contract
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SemanticAdopterMatch:
    """One adopter the vector search placed near an animal.

    Attributes:
        adopter_profile_id: The adopter, as recorded in the profile metadata.
        similarity: Cosine similarity between the animal's query text and
            the adopter's résumé, in [-1, 1]; higher is closer.
    """

    adopter_profile_id: str
    similarity: float


@dataclass(frozen=True)
class SemanticSearchOutcome:
    """What one `SEMANTIC_ADOPTER_SEARCH` job found.

    Attributes:
        matches: Nearest first.
        indexed_profile_count: How many résumés the search ran over, so the
            screen can say "the 10 closest of 42" rather than imply a total.
        embedding_model: Which model made the vectors.
    """

    matches: tuple[SemanticAdopterMatch, ...] = ()
    indexed_profile_count: int = 0
    embedding_model: str = ""


def semantic_search_to_payload(outcome: SemanticSearchOutcome) -> dict[str, object]:
    """Render a search outcome as the JSON a `SEMANTIC_ADOPTER_SEARCH` job stores.

    Kept beside `payload_to_semantic_search` so the two halves of the
    contract cannot drift (docs/MODEL_DATA.md section 2.10). Similarities are
    rounded to four places: more precision than that is noise from the
    embedding model, not information.

    Args:
        outcome: What the agent's search produced.

    Returns:
        A JSON-serialisable mapping with keys `matches`,
        `indexed_profile_count` and `embedding_model`.
    """
    return {
        "matches": [
            {
                "adopter_profile_id": match.adopter_profile_id,
                "similarity": round(match.similarity, 4),
            }
            for match in outcome.matches
        ],
        "indexed_profile_count": outcome.indexed_profile_count,
        "embedding_model": outcome.embedding_model,
    }


def payload_to_semantic_search(payload: Mapping[str, object]) -> SemanticSearchOutcome:
    """Read back an outcome stored by a `SEMANTIC_ADOPTER_SEARCH` job.

    The payload crossed a process boundary, so every entry is checked rather
    than trusted: an entry with no identifier, a similarity that is not a
    finite number in [-1, 1], or a repeated adopter is dropped. The worst
    case is a shorter list, never a wrong one.

    Args:
        payload: The decoded `result_payload` of a completed job.

    Returns:
        The validated outcome, nearest first.
    """
    raw_matches = payload.get("matches")
    entries = raw_matches if isinstance(raw_matches, list) else []

    matches: dict[str, SemanticAdopterMatch] = {}
    for entry in entries[:MAX_DECODED_MATCH_COUNT]:
        match = _match_from_entry(entry)
        if match is not None:
            matches.setdefault(match.adopter_profile_id, match)

    return SemanticSearchOutcome(
        matches=_nearest_first(matches.values()),
        indexed_profile_count=max(
            parse_optional_whole_number(payload.get("indexed_profile_count")) or 0, 0
        ),
        embedding_model=parse_text(payload.get("embedding_model")).strip(),
    )


def _match_from_entry(entry: object) -> SemanticAdopterMatch | None:
    """Validate one stored match, or return None when it is unusable."""
    if not isinstance(entry, Mapping):
        return None
    adopter_profile_id = parse_text(entry.get("adopter_profile_id")).strip()
    raw_similarity = entry.get("similarity")
    if not adopter_profile_id or isinstance(raw_similarity, bool):
        return None
    if not isinstance(raw_similarity, int | float):
        return None
    similarity = float(raw_similarity)
    if not math.isfinite(similarity):
        return None
    if not MINIMUM_SIMILARITY <= similarity <= MAXIMUM_SIMILARITY:
        return None
    return SemanticAdopterMatch(adopter_profile_id, similarity)


def _nearest_first(
    matches: Iterable[SemanticAdopterMatch],
) -> tuple[SemanticAdopterMatch, ...]:
    """Order matches by similarity, highest first, ties by identifier."""
    return tuple(
        sorted(matches, key=lambda match: (-match.similarity, match.adopter_profile_id))
    )
