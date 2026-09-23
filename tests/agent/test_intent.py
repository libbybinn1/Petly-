"""Tests for natural-language intent interpretation (spec section 6.3).

Per rule R3 these never assert on generated wording. The model is stubbed
with fixed responses so the tests cover what the *interpreter* does with a
reply - validation, absence handling and failure behaviour - rather than
whether a particular model phrases things a particular way.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_service.intent import IntentInterpreter, SearchIntent
from agent_service.llm_client import (
    LanguageModelUnavailableError,
    MalformedModelOutputError,
    StubLanguageModel,
)
from app.domain.enums import ActivityLevel, AnimalSize, Species

pytestmark = pytest.mark.agent


def interpreter_returning(response: dict[str, Any]) -> IntentInterpreter:
    """Build an interpreter whose model always returns one fixed reply."""
    return IntentInterpreter(StubLanguageModel(canned_response=response))


class TestExtraction:
    """A well-formed reply becomes typed criteria."""

    def test_species_size_and_activity_are_extracted(self) -> None:
        """Proves the documented example from spec 6.3 produces criteria.

        "something like a hamster or rabbit, a small rodent" should yield
        several species, a small size and low activity.
        """
        intent = interpreter_returning(
            {
                "understood": True,
                "species": ["RABBIT", "HAMSTER", "GUINEA_PIG"],
                "size": "SMALL",
                "activity_level": "LOW",
                "temperament": None,
                "good_with_children": None,
                "good_with_other_animals": None,
                "interpretation": "Looking for a small, low-activity rodent or rabbit.",
            }
        ).interpret("something like a hamster or rabbit, a small rodent")

        assert intent.understood
        assert intent.species == (Species.RABBIT, Species.HAMSTER, Species.GUINEA_PIG)
        assert intent.size is AnimalSize.SMALL
        assert intent.activity_level is ActivityLevel.LOW
        assert intent.has_any_criteria

    def test_interpretation_is_returned_for_display(self) -> None:
        """Proves the adopter can see how they were understood."""
        intent = interpreter_returning(
            {
                "understood": True,
                "species": ["CAT"],
                "interpretation": "A calm indoor cat.",
            }
        ).interpret("a calm indoor cat")

        assert intent.interpretation == "A calm indoor cat."


class TestAbsenceIsPreserved:
    """What the adopter did not say stays unconstrained."""

    def test_unmentioned_fields_stay_none(self) -> None:
        """Proves the interpreter does not invent criteria.

        Guessing would silently narrow the results with no way for the
        adopter to see why, which is worse than a broad result set.
        """
        intent = interpreter_returning(
            {"understood": True, "species": ["DOG"], "interpretation": "A dog."}
        ).interpret("a dog")

        assert intent.species == (Species.DOG,)
        assert intent.size is None
        assert intent.activity_level is None
        assert intent.temperament is None
        assert intent.good_with_children is None

    def test_false_compatibility_is_treated_as_unspecified(self) -> None:
        """Proves `false` never becomes a filter.

        A `good_with_children: false` filter would mean "show me animals
        that are bad with children", which nobody intends. Only `true` is
        meaningful; anything else means no constraint.
        """
        intent = interpreter_returning(
            {
                "understood": True,
                "species": ["DOG"],
                "good_with_children": False,
                "good_with_other_animals": False,
                "interpretation": "A dog.",
            }
        ).interpret("a dog")

        assert intent.good_with_children is None
        assert intent.good_with_other_animals is None

    def test_true_compatibility_is_kept(self) -> None:
        """Proves an explicit requirement does become a filter."""
        intent = interpreter_returning(
            {
                "understood": True,
                "species": ["DOG"],
                "good_with_children": True,
                "interpretation": "A dog that is good with children.",
            }
        ).interpret("a dog for a family with a toddler")

        assert intent.good_with_children is True


class TestInvalidModelOutput:
    """The model's reply is validated, never trusted."""

    def test_invented_species_is_discarded(self) -> None:
        """Proves an unrecognised species cannot become a filter.

        It would match nothing, and the adopter would never learn why their
        search came back empty.
        """
        intent = interpreter_returning(
            {
                "understood": True,
                "species": ["DOG", "DRAGON", "UNICORN"],
                "interpretation": "A dog.",
            }
        ).interpret("a dog")

        assert intent.species == (Species.DOG,)

    def test_invalid_enum_value_becomes_no_constraint(self) -> None:
        """Proves a bad size is dropped rather than stored."""
        intent = interpreter_returning(
            {"understood": True, "species": [], "size": "ENORMOUS",
             "interpretation": "Something big."}
        ).interpret("something big")

        assert intent.size is None

    def test_duplicate_species_are_collapsed(self) -> None:
        """Proves a repeated species does not produce a duplicated filter."""
        intent = interpreter_returning(
            {"understood": True, "species": ["CAT", "CAT", "CAT"],
             "interpretation": "A cat."}
        ).interpret("a cat, definitely a cat")

        assert intent.species == (Species.CAT,)

    def test_species_list_is_capped(self) -> None:
        """Proves a runaway list cannot produce an unbounded filter."""
        intent = interpreter_returning(
            {
                "understood": True,
                "species": ["DOG", "CAT", "RABBIT", "HAMSTER", "BIRD", "GUINEA_PIG"],
                "interpretation": "Anything.",
            }
        ).interpret("anything really")

        assert len(intent.species) <= 4

    def test_species_as_a_string_instead_of_a_list_is_handled(self) -> None:
        """Proves a shape error degrades to no species rather than crashing."""
        intent = interpreter_returning(
            {"understood": True, "species": "DOG", "interpretation": "A dog."}
        ).interpret("a dog")

        assert intent.species == ()

    def test_overlong_interpretation_is_truncated(self) -> None:
        """Proves a rambling summary cannot break the layout."""
        intent = interpreter_returning(
            {"understood": True, "species": ["DOG"], "interpretation": "x" * 5000}
        ).interpret("a dog")

        assert len(intent.interpretation) <= 200


class TestNotUnderstood:
    """Unusable input is an outcome, not an error."""

    def test_blank_text_is_refused_without_calling_the_model(self) -> None:
        """Proves an empty box does not waste an inference call."""
        model = StubLanguageModel()
        intent = IntentInterpreter(model).interpret("   ")

        assert not intent.understood
        assert model.prompts_seen == []

    def test_model_saying_it_did_not_understand_is_respected(self) -> None:
        """Proves the interpreter does not override the model's own verdict."""
        intent = interpreter_returning(
            {"understood": False, "interpretation": "That is not about adopting an animal."}
        ).interpret("what is the weather")

        assert not intent.understood
        assert "not about adopting" in intent.interpretation

    def test_understood_reply_with_no_criteria_reports_nothing_extracted(self) -> None:
        """Proves an empty result is distinguishable from a failure.

        The interface needs to tell "we could not read that" apart from "we
        read it but you did not narrow anything down".
        """
        intent = interpreter_returning(
            {"understood": True, "species": [], "interpretation": "No specifics."}
        ).interpret("I would like a pet")

        assert intent.understood
        assert not intent.has_any_criteria


class TestFailureHandling:
    """A model problem falls back rather than failing the request."""

    class UnavailableModel:
        """A model that cannot be reached."""

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
            """Always fail as if the reply could not be parsed."""
            raise MalformedModelOutputError("not JSON")

    def test_unavailable_model_suggests_the_filters(self) -> None:
        """Proves an outage points the adopter at something that still works."""
        intent = IntentInterpreter(self.UnavailableModel()).interpret("a small dog")

        assert not intent.understood
        assert "filters" in intent.interpretation.lower()

    def test_unparseable_reply_suggests_rephrasing(self) -> None:
        """Proves a malformed reply produces guidance, not a stack trace."""
        intent = IntentInterpreter(self.UnparseableModel()).interpret("a small dog")

        assert not intent.understood
        assert intent.interpretation


class TestIntentValueObject:
    """The intent object behaves sensibly on its own."""

    def test_empty_intent_has_no_criteria(self) -> None:
        """Proves an empty intent does not claim to constrain anything."""
        assert not SearchIntent(understood=True).has_any_criteria

    def test_not_understood_carries_its_reason(self) -> None:
        """Proves the failure constructor records why."""
        intent = SearchIntent.not_understood("Could not read that.")

        assert not intent.understood
        assert intent.interpretation == "Could not read that."
