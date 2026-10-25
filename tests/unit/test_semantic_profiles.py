"""Unit tests for adopter résumés and the semantic-search payload contract.

Lecturer requirement 6 embeds adopter profiles and searches them by meaning.
The pure parts of that feature live in `app.domain.semantic_profiles` and are
tested here with no database, no Chroma and no model: the résumé text is
deterministic, it carries no identity, its hash changes exactly when it
should, and the payload the agent writes is validated on the way back in.
"""

from __future__ import annotations

import json
import math

import pytest
from app.domain.enums import AnalysisJobType
from app.domain.semantic_profiles import (
    ADOPTER_PROFILE_TEXT_FIELDS,
    MAX_DECODED_MATCH_COUNT,
    SemanticAdopterMatch,
    SemanticSearchOutcome,
    build_adopter_profile_text,
    build_animal_query_text,
    payload_to_semantic_search,
    profile_content_hash,
    semantic_search_to_payload,
)
from scripts.db import JOB_TYPE_CHECK_NAME, job_type_check_statements

pytestmark = pytest.mark.unit

# Written out rather than derived, so a change to either side of the
# contract fails here instead of silently agreeing with itself.
PAYLOAD_KEYS = {"matches", "indexed_profile_count", "embedding_model"}
MATCH_KEYS = {"adopter_profile_id", "similarity"}


def make_profile(**overrides: object) -> dict[str, object]:
    """A complete adopter record keyed by the résumé's own field list."""
    record: dict[str, object] = {
        "home_type": "HOUSE",
        "has_yard": True,
        "yard_size_sqm": 120,
        "household_has_children": True,
        "youngest_child_age": 6,
        "has_other_animals": True,
        "other_animals_description": "an elderly cat",
        "experience_level": "EXPERIENCED",
        "activity_level": "HIGH",
        "daily_hours_available": 5.5,
        "city": "Haifa",
        "preferred_species": "DOG,GUINEA_PIG",
        "preferred_size": "MEDIUM",
        "preferred_age_range": "2-8 years",
    }
    record.update(overrides)
    return record


class TestProfileText:
    """The résumé is a faithful, deterministic, identity-free description."""

    def test_the_same_record_always_produces_the_same_text(self) -> None:
        """Proves the builder is deterministic, which the hash-skip relies on."""
        assert build_adopter_profile_text(make_profile()) == build_adopter_profile_text(
            make_profile()
        )

    def test_the_text_states_the_household_facts(self) -> None:
        """Proves each recorded fact reaches the text an embedding model reads."""
        text = build_adopter_profile_text(make_profile())

        for expected in ("house", "Haifa", "120 square metres", "6 years old",
                         "an elderly cat", "experienced", "high", "5.5 hours",
                         "dog or guinea pig", "medium", "2-8 years"):
            assert expected in text, expected

    def test_the_text_carries_no_identity(self) -> None:
        """Proves names and emails never reach the vector store.

        The résumé is built only from `ADOPTER_PROFILE_TEXT_FIELDS`; extra
        keys on the record, such as an account's name, are ignored.
        """
        record = make_profile(full_name="Dana Levi", email="dana@example.test")
        text = build_adopter_profile_text(record)

        assert "Dana" not in text
        assert "example.test" not in text
        assert "full_name" not in ADOPTER_PROFILE_TEXT_FIELDS
        assert "email" not in ADOPTER_PROFILE_TEXT_FIELDS

    def test_an_absent_preference_is_not_invented(self) -> None:
        """Proves a field left empty becomes openness, not a made-up claim."""
        text = build_adopter_profile_text(
            make_profile(preferred_species=None, preferred_size=None, preferred_age_range=None)
        )

        assert "Open to any kind of animal." in text
        assert "dog" not in text

    def test_a_household_without_extras_says_so(self) -> None:
        """Proves the negative facts are stated rather than silently dropped."""
        text = build_adopter_profile_text(
            make_profile(has_yard=False, household_has_children=False, has_other_animals=False)
        )

        assert "no yard" in text
        assert "No children" in text
        assert "no other pets" in text

    def test_unreadable_values_degrade_instead_of_raising(self) -> None:
        """Negative test: a malformed record still yields text, never a crash."""
        text = build_adopter_profile_text(
            {"youngest_child_age": "six", "household_has_children": True,
             "daily_hours_available": "lots"}
        )

        assert "Children live in the household." in text
        assert "0 hours" in text


class TestAnimalQueryText:
    """The animal is searched by its facts and its own description."""

    def test_the_description_is_included_verbatim(self) -> None:
        """Proves the animal's own description text is what gets embedded."""
        text = build_animal_query_text(
            {"name": "Clover", "species": "RABBIT", "age_years": 2.0, "size": "SMALL",
             "description": "Loves quiet evenings and gentle handling."}
        )

        assert text.startswith("Clover is a 2 year old small rabbit.")
        assert "Loves quiet evenings and gentle handling." in text

    def test_an_animal_without_a_description_still_has_a_query(self) -> None:
        """Proves a blank description cannot produce an empty, unsearchable query."""
        assert build_animal_query_text({"name": "Gus", "species": "DOG"}).strip()


class TestContentHash:
    """The hash decides whether a profile must be embedded again."""

    def test_an_unchanged_text_keeps_its_hash(self) -> None:
        """Proves an unchanged profile is recognised as unchanged."""
        text = build_adopter_profile_text(make_profile())
        assert profile_content_hash(text, "nomic") == profile_content_hash(text, "nomic")

    def test_a_changed_profile_changes_its_hash(self) -> None:
        """Proves an edited profile is re-embedded rather than served stale."""
        before = build_adopter_profile_text(make_profile())
        after = build_adopter_profile_text(make_profile(city="Tel Aviv"))
        assert profile_content_hash(before, "nomic") != profile_content_hash(after, "nomic")

    def test_a_different_embedding_model_changes_the_hash(self) -> None:
        """Proves a model change re-embeds everything: vectors from two models never mix."""
        text = build_adopter_profile_text(make_profile())
        assert profile_content_hash(text, "nomic") != profile_content_hash(text, "other-model")


class TestPayloadContract:
    """What the agent writes is exactly what the web tier reads back."""

    def test_the_payload_has_the_documented_keys(self) -> None:
        """Proves the JSON shape matches docs/MODEL_DATA.md section 2.10."""
        outcome = SemanticSearchOutcome(
            matches=(SemanticAdopterMatch("profile-a", 0.81234567),),
            indexed_profile_count=7,
            embedding_model="nomic-embed-text",
        )
        payload = semantic_search_to_payload(outcome)

        assert set(payload) == PAYLOAD_KEYS
        matches = payload["matches"]
        assert isinstance(matches, list)
        assert set(matches[0]) == MATCH_KEYS
        assert matches[0]["similarity"] == 0.8123

    def test_the_payload_round_trips_through_json(self) -> None:
        """Proves encode, the NVARCHAR(MAX) text column and decode lose nothing."""
        outcome = SemanticSearchOutcome(
            matches=(
                SemanticAdopterMatch("profile-a", 0.9),
                SemanticAdopterMatch("profile-b", 0.4),
            ),
            indexed_profile_count=2,
            embedding_model="nomic-embed-text",
        )
        stored = json.dumps(semantic_search_to_payload(outcome))

        assert payload_to_semantic_search(json.loads(stored)) == outcome

    def test_decoding_orders_matches_nearest_first(self) -> None:
        """Proves the screen's order does not depend on how the payload was written."""
        decoded = payload_to_semantic_search(
            {"matches": [{"adopter_profile_id": "low", "similarity": 0.2},
                         {"adopter_profile_id": "high", "similarity": 0.9}]}
        )
        assert [match.adopter_profile_id for match in decoded.matches] == ["high", "low"]

    @pytest.mark.parametrize(
        "bad_entry",
        [
            {"similarity": 0.5},
            {"adopter_profile_id": "", "similarity": 0.5},
            {"adopter_profile_id": "a", "similarity": "0.5"},
            {"adopter_profile_id": "a", "similarity": True},
            {"adopter_profile_id": "a", "similarity": 1.5},
            {"adopter_profile_id": "a", "similarity": math.nan},
            "not a mapping",
        ],
    )
    def test_an_invalid_entry_is_dropped(self, bad_entry: object) -> None:
        """Negative test: an entry the web tier cannot trust never reaches the screen."""
        decoded = payload_to_semantic_search(
            {"matches": [bad_entry, {"adopter_profile_id": "good", "similarity": 0.7}]}
        )
        assert [match.adopter_profile_id for match in decoded.matches] == ["good"]

    def test_a_repeated_adopter_appears_once(self) -> None:
        """Negative test: a duplicated id cannot list one person twice."""
        decoded = payload_to_semantic_search(
            {"matches": [{"adopter_profile_id": "a", "similarity": 0.7},
                         {"adopter_profile_id": "a", "similarity": 0.1}]}
        )
        assert len(decoded.matches) == 1

    def test_an_oversized_payload_is_capped(self) -> None:
        """Negative test: a payload's size is bounded rather than trusted."""
        entries = [{"adopter_profile_id": f"p{index}", "similarity": 0.5} for index in range(500)]
        decoded = payload_to_semantic_search({"matches": entries})
        assert len(decoded.matches) == MAX_DECODED_MATCH_COUNT

    def test_a_payload_of_the_wrong_shape_decodes_to_no_matches(self) -> None:
        """Negative test: nonsense degrades to an empty result, not an exception."""
        decoded = payload_to_semantic_search({"matches": "nope", "indexed_profile_count": "x"})
        assert decoded == SemanticSearchOutcome()


class TestJobTypeMigration:
    """The live CHECK constraint can be brought into step with the enum."""

    def test_the_recreated_check_admits_every_job_type(self) -> None:
        """Proves the migration allows every AnalysisJobType, including the new one."""
        add_statement = job_type_check_statements()[-1]

        assert JOB_TYPE_CHECK_NAME in add_statement
        for job_type in AnalysisJobType:
            assert f"'{job_type.value}'" in add_statement

    def test_the_drop_is_guarded_so_a_rerun_is_harmless(self) -> None:
        """Proves the drop only runs while a matching constraint exists (idempotent)."""
        drop_statement = job_type_check_statements()[0]

        assert "WHILE @constraint_name IS NOT NULL" in drop_statement
        assert "sys.check_constraints" in drop_statement
        assert "%" not in drop_statement
