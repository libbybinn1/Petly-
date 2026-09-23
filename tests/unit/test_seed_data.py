"""Unit tests for the demo seed data.

These run with no database, no network and no photo fetch. The roster and the
adopter list are hand-written data, and a hand-written mistake in them does
not fail loudly - it reaches the interface as a listing with no name, an
exotic animal the page cannot label, or a "special needs" badge with nothing
behind it. Checking the data here is what stops that from happening in front
of whoever is being shown the demo.

Blueprint section 17 requires failure scenarios as well as happy paths, so
the validators are tested against deliberately malformed input too.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import replace

import pytest
from app.domain.enums import ActivityLevel, AnimalSize, Species, Temperament
from app.infrastructure.models import AdopterProfile, Animal, User
from scripts.seed_people import (
    ADOPTER_SPECIFICATIONS,
    DEMO_ADOPTER_EMAIL,
    DEMO_STAFF_EMAIL,
    STAFF_SPECIFICATIONS,
    AdopterSpecification,
    PreferredAgeRange,
    validate_adopter_specification,
)
from scripts.seed_roster import (
    ANIMAL_SPECIFICATIONS,
    CITIES,
    AnimalSpecification,
    kind_group,
    status_for,
    validate_animal_specification,
)

pytestmark = pytest.mark.unit

MINIMUM_ANIMALS = 120
MINIMUM_ADOPTERS = 35
MINIMUM_KIND_GROUPS = 8
MINIMUM_SPECIAL_NEEDS_SHARE = 0.10
MAXIMUM_SPECIAL_NEEDS_SHARE = 0.30


def sound_animal() -> AnimalSpecification:
    """A valid roster entry, used as the base for malformed variants."""
    return AnimalSpecification(
        "Test Animal",
        Species.DOG,
        "Mixed Breed",
        3.0,
        AnimalSize.MEDIUM,
        Temperament.BALANCED,
        ActivityLevel.MODERATE,
        AnimalSize.MEDIUM,
        "A perfectly ordinary dog, invented for a test.",
    )


def sound_adopter() -> AdopterSpecification:
    """A valid adopter entry, used as the base for malformed variants."""
    return AdopterSpecification(
        "Test Person",
        "test@example.com",
        "Haifa",
        ADOPTER_SPECIFICATIONS[0].home_type,
        ADOPTER_SPECIFICATIONS[0].experience_level,
        ActivityLevel.MODERATE,
        3.0,
        (Species.CAT,),
        AnimalSize.SMALL,
        PreferredAgeRange.ANY,
        "Invented for a test.",
    )


# --------------------------------------------------------------------------
# Scale
# --------------------------------------------------------------------------


def test_the_roster_is_large_enough_to_look_like_a_real_shelter():
    """Proves the catalogue has at least 120 animals.

    A dozen animals makes every screen look like a prototype: search returns
    everything, filters have nothing to filter, and the dashboard's "animals
    with no applicants" figure is meaningless.
    """
    assert len(ANIMAL_SPECIFICATIONS) >= MINIMUM_ANIMALS


def test_there_are_enough_adopters_to_rank():
    """Proves there are at least 35 adopters.

    Ranking candidates for an animal (spec section 10) is only a meaningful
    demonstration when there are enough candidates for the ordering to carry
    information.
    """
    assert len(ADOPTER_SPECIFICATIONS) >= MINIMUM_ADOPTERS


def test_the_roster_spans_many_different_kinds_of_animal():
    """Proves at least 8 distinct kinds of animal are represented.

    A hundred dog breeds would satisfy a count but not the point: the species
    filter, the knowledge base and the agent's species-specific guidance all
    need genuinely different animals to work on.
    """
    groups = {kind_group(specification) for specification in ANIMAL_SPECIFICATIONS}
    assert len(groups) >= MINIMUM_KIND_GROUPS


def test_every_species_in_the_enum_appears_in_the_roster():
    """Proves no species the system offers is absent from the demo.

    The search form lists every `Species` member. One with no animals behind
    it is a filter that always returns nothing.
    """
    present = {specification.species for specification in ANIMAL_SPECIFICATIONS}
    assert present == set(Species)


# --------------------------------------------------------------------------
# Animal integrity
# --------------------------------------------------------------------------


def test_every_animal_has_a_name_and_a_description():
    """Proves no animal would render as a blank listing.

    Spec section 24 treats the listing as part of the product, and a card
    with no name and no text is the most visible possible data fault.
    """
    for specification in ANIMAL_SPECIFICATIONS:
        assert specification.name.strip(), "an animal has no name"
        assert specification.description.strip(), f"{specification.name} has no description"


def test_no_two_animals_share_a_name():
    """Proves animal names are unique, ignoring case.

    Two animals called Luna makes the demo confusing to narrate and makes any
    name-based assertion in a test ambiguous.
    """
    counted = Counter(specification.name.strip().lower() for specification in ANIMAL_SPECIFICATIONS)
    duplicates = sorted(name for name, count in counted.items() if count > 1)
    assert duplicates == []


def test_every_exotic_animal_carries_a_breed():
    """Proves every `Species.OTHER` animal has something to display.

    The interface renders OTHER as the word "Other" and relies on the breed
    beside it to say what the animal actually is. Without a breed, a tortoise
    is listed as an "Other".
    """
    for specification in ANIMAL_SPECIFICATIONS:
        if specification.species is not Species.OTHER:
            continue
        assert (specification.breed or "").strip(), f"{specification.name} has no breed"


def test_special_needs_are_flagged_and_described_together():
    """Proves the special-needs flag and its description never disagree.

    The flag drives matching - `_experience_demanded_by` treats a
    special-needs animal as requiring an experienced owner - while the
    description is what a person reads. One without the other is either an
    unexplained warning or a silent requirement.
    """
    for specification in ANIMAL_SPECIFICATIONS:
        has_description = bool((specification.special_needs_description or "").strip())
        assert specification.has_special_needs == has_description, specification.name


def test_special_needs_are_a_realistic_minority():
    """Proves special-needs animals are a believable share of the roster.

    Too few and the special-care criterion never fires; too many and the
    demo stops looking like a shelter and starts looking like a hospital.
    """
    share = sum(
        1 for specification in ANIMAL_SPECIFICATIONS if specification.has_special_needs
    ) / len(ANIMAL_SPECIFICATIONS)
    assert MINIMUM_SPECIAL_NEEDS_SHARE <= share <= MAXIMUM_SPECIAL_NEEDS_SHARE


def test_every_animal_has_a_positive_age():
    """Proves no animal is recorded as newborn-or-earlier.

    Age drives the "months" or "years" label on every card, and zero would
    render as "1 month" for an animal of unknown age rather than admitting
    the value is missing.
    """
    for specification in ANIMAL_SPECIFICATIONS:
        assert specification.age_years > 0, specification.name


def test_the_roster_reaches_both_extremes_of_the_matching_engine():
    """Proves the roster contains animals for the hardest homes and the easiest.

    Matching (spec section 8) is only demonstrable if the catalogue holds both
    ends: something a low-activity flat can take, and something that needs a
    large home. Without both, the space and availability criteria never
    separate candidates.
    """
    fits_a_quiet_flat = [
        specification
        for specification in ANIMAL_SPECIFICATIONS
        if specification.required_space is AnimalSize.SMALL
        and specification.activity_level is ActivityLevel.LOW
    ]
    needs_real_land = [
        specification
        for specification in ANIMAL_SPECIFICATIONS
        if specification.required_space is AnimalSize.LARGE
    ]
    unsuitable_for_children = [
        specification
        for specification in ANIMAL_SPECIFICATIONS
        if not specification.good_with_children
    ]

    assert fits_a_quiet_flat
    assert needs_real_land
    assert unsuitable_for_children


def test_every_animal_status_is_one_the_pipeline_recognises():
    """Proves the status pattern only ever yields real pipeline states.

    Statuses come from a repeating pattern rather than one value per animal,
    so an editing mistake in that pattern would spread across the roster
    instead of affecting one record.
    """
    statuses = {
        status_for(specification, index)
        for index, specification in enumerate(ANIMAL_SPECIFICATIONS)
    }
    assert len(statuses) > 1
    available = sum(
        1
        for index, specification in enumerate(ANIMAL_SPECIFICATIONS)
        if status_for(specification, index).is_open_for_applications
    )
    assert available > len(ANIMAL_SPECIFICATIONS) / 2


def test_bonded_animals_are_never_left_behind():
    """Proves a pinned status is honoured rather than overwritten.

    Bonded pairs pin themselves to AVAILABLE so a demo never shows one half
    of an inseparable pair adopted and the other half still waiting.
    """
    pinned = [
        (index, specification)
        for index, specification in enumerate(ANIMAL_SPECIFICATIONS)
        if specification.status is not None
    ]
    assert pinned, "the roster has no pinned statuses to honour"
    for index, specification in pinned:
        assert status_for(specification, index) is specification.status


# --------------------------------------------------------------------------
# Adopter integrity
# --------------------------------------------------------------------------


def test_every_adopter_email_is_unique_and_looks_like_an_address():
    """Proves no two adopters could collide on the accounts table.

    `users.email` is unique in the schema, so a duplicate does not produce
    bad data - it aborts the whole seed transaction partway through.
    """
    emails = [specification.email for specification in ADOPTER_SPECIFICATIONS]
    assert len(set(emails)) == len(emails)
    for email in emails:
        local_part, _, domain = email.partition("@")
        assert local_part and "." in domain, email
        assert email == email.strip().lower(), email


def test_staff_and_adopter_emails_do_not_overlap():
    """Proves a staff address is never also an adopter address.

    Authorization is decided by the role on one account (spec section 3). The
    same address used twice would mean two accounts with different roles and
    an ambiguous sign-in.
    """
    adopter_emails = {specification.email for specification in ADOPTER_SPECIFICATIONS}
    staff_emails = {specification.email for specification in STAFF_SPECIFICATIONS}
    assert adopter_emails.isdisjoint(staff_emails)


def test_the_documented_demo_accounts_exist():
    """Proves the two accounts README.md names are still present.

    README.md tells a reviewer to sign in as these two, and the end-to-end
    suite expects them. Renaming either breaks the first thing anybody does
    with the project.
    """
    assert DEMO_ADOPTER_EMAIL == "maya@example.com"
    assert DEMO_STAFF_EMAIL == "dana@petmatch.org"
    assert any(
        specification.email == DEMO_ADOPTER_EMAIL for specification in ADOPTER_SPECIFICATIONS
    )
    assert any(specification.email == DEMO_STAFF_EMAIL for specification in STAFF_SPECIFICATIONS)


def test_some_profiles_are_incomplete_and_some_opted_out():
    """Proves the eligibility filter has something to exclude.

    Proactive discovery only considers completed profiles whose owner opted
    in (spec section 10). If every seeded profile qualified, a filter that
    had stopped working would look exactly like one that worked.
    """
    incomplete = [
        specification for specification in ADOPTER_SPECIFICATIONS if not specification.is_complete
    ]
    opted_out = [
        specification
        for specification in ADOPTER_SPECIFICATIONS
        if not specification.open_to_proactive_suggestions
    ]
    assert incomplete
    assert opted_out
    assert len(incomplete) + len(opted_out) < len(ADOPTER_SPECIFICATIONS)


def test_every_adopter_states_a_size_or_age_preference_and_a_known_city():
    """Proves the preference columns are populated and cities are consistent.

    `preferred_size` and `preferred_age_range` are handed to the agent by the
    MCP `get_adopter_profile` tool. Left null for every adopter - as they
    were before - the tool returns a profile with two empty fields and the
    agent has less to reason about than the schema promises.
    """
    for specification in ADOPTER_SPECIFICATIONS:
        assert specification.preferred_age_range in set(PreferredAgeRange)
        assert specification.city in CITIES, specification.city


def test_adopter_households_cover_the_hard_constraints():
    """Proves the adopter list exercises every hard constraint in matching.

    `find_hard_constraint_violation` has three branches: young children with
    an animal not certified for them, resident pets with an animal that must
    be alone, and a large animal in a flat with no yard. Each needs a
    household that can trigger it.
    """
    with_young_children = [
        specification
        for specification in ADOPTER_SPECIFICATIONS
        if (specification.youngest_child_age or 99) < 12
    ]
    with_resident_pets = [
        specification for specification in ADOPTER_SPECIFICATIONS if specification.has_other_animals
    ]
    flats_without_a_yard = [
        specification
        for specification in ADOPTER_SPECIFICATIONS
        if not specification.has_yard and specification.home_type.value == "APARTMENT"
    ]
    assert with_young_children
    assert with_resident_pets
    assert flats_without_a_yard


def test_the_adopter_list_spans_the_whole_availability_range():
    """Proves the daily-availability criterion has something to discriminate.

    That criterion carries the heaviest weight in the animal-to-adopter
    direction (spec section 9.2). If every adopter reported the same free
    hours it would contribute nothing to any ranking.
    """
    hours = sorted(
        specification.daily_hours_available for specification in ADOPTER_SPECIFICATIONS
    )
    assert hours[0] < 1.0
    assert hours[-1] >= 8.0


# --------------------------------------------------------------------------
# The data fits the schema it will be loaded into
# --------------------------------------------------------------------------


def column_limit(model: type, column_name: str) -> int:
    """The declared character width of one mapped column.

    Read from the model rather than written out here, so the check cannot
    drift away from the schema it is protecting.
    """
    length = model.__table__.columns[column_name].type.length
    assert isinstance(length, int), f"{column_name} has no declared width"
    return length


def test_every_animal_string_fits_its_column():
    """Proves no roster text would be rejected by SQL Server.

    This is the check the SQLite-backed suites cannot make: SQLite silently
    accepts a string longer than a declared NVARCHAR width and SQL Server
    2014 refuses it, so an over-long name or breed would pass every local
    test and then abort the cloud reseed partway through.
    """
    name_limit = column_limit(Animal, "name")
    breed_limit = column_limit(Animal, "breed")
    special_needs_limit = column_limit(Animal, "special_needs_description")

    for specification in ANIMAL_SPECIFICATIONS:
        assert len(specification.name) <= name_limit, specification.name
        assert len(specification.breed or "") <= breed_limit, specification.name
        assert (
            len(specification.special_needs_description or "") <= special_needs_limit
        ), specification.name


def test_every_adopter_string_fits_its_column():
    """Proves no adopter text would be rejected by SQL Server.

    Covers the comma-joined `preferred_species` column in particular, which
    grows with each species an adopter selects and is the one field here whose
    length is not obvious by eye.
    """
    for specification in ADOPTER_SPECIFICATIONS:
        assert len(specification.full_name) <= column_limit(User, "full_name")
        assert len(specification.email) <= column_limit(User, "email")
        assert len(specification.city) <= column_limit(AdopterProfile, "city")
        assert len(specification.other_animals_description or "") <= column_limit(
            AdopterProfile, "other_animals_description"
        )
        assert len(specification.preferred_species_column or "") <= column_limit(
            AdopterProfile, "preferred_species"
        )
        assert len(specification.preferred_age_range.value) <= column_limit(
            AdopterProfile, "preferred_age_range"
        )
        size = specification.preferred_size
        assert len(size.value if size else "") <= column_limit(AdopterProfile, "preferred_size")


def test_every_staff_string_fits_its_column():
    """Proves the staff accounts would load into the real schema too."""
    for specification in STAFF_SPECIFICATIONS:
        assert len(specification.full_name) <= column_limit(User, "full_name")
        assert len(specification.email) <= column_limit(User, "email")


# --------------------------------------------------------------------------
# The validators reject malformed data (blueprint section 17)
# --------------------------------------------------------------------------


def test_the_whole_roster_passes_its_own_validator():
    """Proves every shipped roster entry is sound by its own rules.

    The validator is only worth having if the data it guards actually passes
    it; a green suite with a silently failing roster would be worse than no
    check at all.
    """
    for specification in ANIMAL_SPECIFICATIONS:
        assert validate_animal_specification(specification) == (), specification.name


def test_every_adopter_passes_its_own_validator():
    """Proves every shipped adopter entry is sound by its own rules."""
    for specification in ADOPTER_SPECIFICATIONS:
        assert validate_adopter_specification(specification) == (), specification.email


def test_an_animal_with_no_description_is_rejected():
    """Proves the validator catches a blank description.

    The negative case blueprint section 17 asks for: the check has to fail on
    bad input, not merely pass on good input.
    """
    problems = validate_animal_specification(replace(sound_animal(), description="   "))
    assert problems
    assert any("description" in problem for problem in problems)


def test_an_exotic_animal_with_no_breed_is_rejected():
    """Proves an unlabelled `Species.OTHER` animal cannot slip through."""
    problems = validate_animal_specification(
        replace(sound_animal(), species=Species.OTHER, breed=None)
    )
    assert any("breed" in problem for problem in problems)


def test_special_needs_flagged_without_a_description_is_rejected():
    """Proves a special-needs badge with nothing behind it is caught."""
    problems = validate_animal_specification(replace(sound_animal(), has_special_needs=True))
    assert any("not described" in problem for problem in problems)


def test_a_special_needs_description_without_the_flag_is_rejected():
    """Proves the inverse mismatch is caught too.

    This is the dangerous direction: the text warns a reader about a medical
    need while the unset flag tells matching the animal is straightforward.
    """
    problems = validate_animal_specification(
        replace(sound_animal(), special_needs_description="Needs daily medication.")
    )
    assert any("not flagged" in problem for problem in problems)


def test_an_implausible_age_is_rejected():
    """Proves a zero or absurd age cannot reach the age label."""
    assert validate_animal_specification(replace(sound_animal(), age_years=0.0))
    assert validate_animal_specification(replace(sound_animal(), age_years=400.0))


def test_an_adopter_with_children_but_no_age_is_rejected():
    """Proves a household with children must state the youngest child's age.

    That age is what decides whether an animal not certified with children is
    a hard disqualification, so an unknown value is a safety question rather
    than a missing nicety.
    """
    problems = validate_adopter_specification(
        replace(sound_adopter(), household_has_children=True)
    )
    assert any("no age" in problem for problem in problems)


def test_an_adopter_with_a_child_age_but_no_children_is_rejected():
    """Proves contradictory household answers are caught."""
    problems = validate_adopter_specification(replace(sound_adopter(), youngest_child_age=6))
    assert any("no children" in problem for problem in problems)


def test_a_yard_size_without_a_yard_is_rejected():
    """Proves a yard measurement cannot be recorded for a home with no yard.

    A yard raises a home's effective capacity by a size band, so a stray size
    beside `has_yard=False` is the kind of contradiction that would make a
    space score impossible to explain.
    """
    problems = validate_adopter_specification(replace(sound_adopter(), yard_size_sqm=60))
    assert any("no yard" in problem for problem in problems)


def test_an_impossible_number_of_daily_hours_is_rejected():
    """Proves availability outside a real day is caught."""
    assert validate_adopter_specification(replace(sound_adopter(), daily_hours_available=30.0))


def test_a_malformed_email_is_rejected():
    """Proves an address that would never receive a sign-in is caught."""
    problems = validate_adopter_specification(replace(sound_adopter(), email="Not An Address"))
    assert any("email" in problem for problem in problems)
