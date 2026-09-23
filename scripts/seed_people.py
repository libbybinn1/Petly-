"""The demo people: adopter profiles and organization staff.

Separated from `scripts/seed.py` for the same reason as the animal roster -
this is data, and keeping it out of the seeding logic lets
`tests/unit/test_seed_data.py` check it without a database.

The profiles are chosen to exercise the matching engine rather than to fill a
table. Between them they cover every branch of the hard-constraint checks in
`app/domain/matching.py`: apartments with no yard, households with a toddler,
homes that already have pets, people with almost no free time, and people with
a farm and all day. Three profiles are deliberately left incomplete, because
the eligibility filter for proactive discovery excludes incomplete profiles
(spec section 10) and a filter with nothing to exclude proves nothing.

Two accounts are fixed and must not be renamed: `maya@example.com` and
`dana@petmatch.org` are named in README.md and used by the end-to-end suite.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.domain.enums import ActivityLevel, AnimalSize, ExperienceLevel, HomeType, Species

# The same bounds `app/domain/profile_rules.py` enforces on a submitted form.
# Repeated rather than imported because the validation below is a check on
# hand-written seed data, not a second implementation of the form rules.
MINIMUM_DAILY_HOURS = 0.0
MAXIMUM_DAILY_HOURS = 24.0
MINIMUM_CHILD_AGE = 0
MAXIMUM_CHILD_AGE = 18


class PreferredAgeRange(StrEnum):
    """Vocabulary for `adopter_profiles.preferred_age_range`.

    The column is a free NVARCHAR(20) that no rule currently parses: matching
    (spec section 8) scores age indirectly, through activity level and special
    needs, so nothing depends on the exact spelling. It *is* handed to the
    agent by the MCP `get_adopter_profile` tool, so the values are written to
    read as English rather than as codes. If a later rule needs to compare
    them, this enum is the one place to change.
    """

    BABY = "0-2 years"
    ADULT = "2-8 years"
    SENIOR = "8+ years"
    ANY = "any"


@dataclass(frozen=True)
class AdopterSpecification:
    """A demo adopter defined before persistence.

    Fields that differ for nearly every adopter come first; the ones that are
    usually false or absent carry defaults, so each entry states only what is
    distinctive about that household.
    """

    full_name: str
    email: str
    city: str
    home_type: HomeType
    experience_level: ExperienceLevel
    activity_level: ActivityLevel
    daily_hours_available: float
    preferred_species: tuple[Species, ...]
    preferred_size: AnimalSize | None
    preferred_age_range: PreferredAgeRange
    summary: str
    has_yard: bool = False
    yard_size_sqm: int | None = None
    household_has_children: bool = False
    youngest_child_age: int | None = None
    other_animals_description: str | None = None
    open_to_proactive_suggestions: bool = True
    is_complete: bool = True

    @property
    def has_other_animals(self) -> bool:
        """Whether the household already keeps animals.

        Derived from the description rather than stored twice: a resident-pets
        flag that disagreed with the text beside it would be a hard constraint
        (matching, section "Hard constraints") driven by the wrong value.
        """
        return bool((self.other_animals_description or "").strip())

    @property
    def preferred_species_column(self) -> str | None:
        """The comma-separated form stored in `preferred_species`.

        The column is a string because SQL Server 2014 has no array type; the
        read side splits it on commas.
        """
        if not self.preferred_species:
            return None
        return ",".join(species.value for species in self.preferred_species)


ADOPTER_SPECIFICATIONS: tuple[AdopterSpecification, ...] = (
    AdopterSpecification(
        "Maya Cohen", "maya@example.com", "Tel Aviv", HomeType.APARTMENT,
        ExperienceLevel.NONE, ActivityLevel.LOW, 2.0,
        (Species.CAT, Species.RABBIT), AnimalSize.SMALL, PreferredAgeRange.ANY,
        "First-time adopter in a small flat. Quiet evenings, no garden.",
    ),
    AdopterSpecification(
        "Daniel Levi", "daniel@example.com", "Haifa", HomeType.HOUSE,
        ExperienceLevel.EXPERIENCED, ActivityLevel.HIGH, 5.0,
        (Species.DOG,), AnimalSize.LARGE, PreferredAgeRange.ADULT,
        "Family with a seven-year-old, a fenced garden and a resident cat.",
        has_yard=True, yard_size_sqm=150, household_has_children=True,
        youngest_child_age=7, other_animals_description="One resident cat, eight years old",
    ),
    AdopterSpecification(
        "Noa Friedman", "noa@example.com", "Tel Aviv", HomeType.APARTMENT,
        ExperienceLevel.SOME, ActivityLevel.MODERATE, 3.5,
        (Species.CAT,), AnimalSize.SMALL, PreferredAgeRange.ADULT,
        "Works from home three days a week and already has one cat.",
        other_animals_description="A four-year-old tabby who tolerates company",
    ),
    AdopterSpecification(
        "Yossi Mizrahi", "yossi@example.com", "Beer Sheva", HomeType.FARM,
        ExperienceLevel.EXPERIENCED, ActivityLevel.HIGH, 8.0,
        (Species.DOG,), AnimalSize.LARGE, PreferredAgeRange.ANY,
        "Runs a smallholding with two working dogs and a great deal of land.",
        has_yard=True, yard_size_sqm=4000,
        other_animals_description="Two working dogs who live outdoors",
    ),
    AdopterSpecification(
        "Tamar Shapiro", "tamar@example.com", "Jerusalem", HomeType.APARTMENT,
        ExperienceLevel.SOME, ActivityLevel.MODERATE, 4.0,
        (Species.CAT, Species.GUINEA_PIG), AnimalSize.SMALL, PreferredAgeRange.ADULT,
        "Flat in the city with a twelve-year-old who has been asking for two years.",
        household_has_children=True, youngest_child_age=12,
    ),
    AdopterSpecification(
        "Amit Golan", "amit@example.com", "Netanya", HomeType.HOUSE,
        ExperienceLevel.NONE, ActivityLevel.MODERATE, 3.0,
        (Species.DOG, Species.CAT), AnimalSize.MEDIUM, PreferredAgeRange.ADULT,
        "House with a small garden. Wants to be found rather than to search.",
        has_yard=True, yard_size_sqm=40, open_to_proactive_suggestions=False,
    ),
    AdopterSpecification(
        "Shira Ben-David", "shira@example.com", "Rishon LeZion", HomeType.APARTMENT,
        ExperienceLevel.SOME, ActivityLevel.LOW, 2.5,
        (Species.RABBIT, Species.HAMSTER), AnimalSize.SMALL, PreferredAgeRange.ANY,
        "Kept rabbits as a teenager and would like to again.",
    ),
    AdopterSpecification(
        "Eitan Barak", "eitan@example.com", "Ramat Gan", HomeType.HOUSE,
        ExperienceLevel.EXPERIENCED, ActivityLevel.HIGH, 6.0,
        (Species.DOG,), AnimalSize.LARGE, PreferredAgeRange.ADULT,
        "Experienced dog owner with a four-year-old in the house.",
        has_yard=True, yard_size_sqm=80, household_has_children=True,
        youngest_child_age=4,
    ),
    AdopterSpecification(
        "Liora Katz", "liora@example.com", "Haifa", HomeType.APARTMENT,
        ExperienceLevel.EXPERIENCED, ActivityLevel.LOW, 5.0,
        (Species.CAT,), AnimalSize.SMALL, PreferredAgeRange.SENIOR,
        "Retired, home all day, and has nursed two cats through old age.",
        other_animals_description="One elderly cat on thyroid medication",
    ),
    AdopterSpecification(
        "Omer Peretz", "omer@example.com", "Petah Tikva", HomeType.HOUSE,
        ExperienceLevel.SOME, ActivityLevel.HIGH, 4.5,
        (Species.DOG,), AnimalSize.MEDIUM, PreferredAgeRange.ADULT,
        "Runs most mornings and wants a dog that can keep up.",
        has_yard=True, yard_size_sqm=80, open_to_proactive_suggestions=False,
    ),
    AdopterSpecification(
        "Rivka Adler", "rivka@example.com", "Ashdod", HomeType.APARTMENT,
        ExperienceLevel.NONE, ActivityLevel.LOW, 2.0,
        (Species.GUINEA_PIG, Species.RABBIT), AnimalSize.SMALL, PreferredAgeRange.BABY,
        "Looking for a first pet for a careful nine-year-old.",
        household_has_children=True, youngest_child_age=9,
    ),
    AdopterSpecification(
        "Gal Rosen", "gal@example.com", "Herzliya", HomeType.HOUSE,
        ExperienceLevel.EXPERIENCED, ActivityLevel.MODERATE, 5.5,
        (Species.DOG, Species.CAT), AnimalSize.MEDIUM, PreferredAgeRange.ANY,
        "Long-term foster carer with a garden and a very tolerant resident dog.",
        has_yard=True, yard_size_sqm=150,
        other_animals_description="A ten-year-old labrador who likes everybody",
    ),
    AdopterSpecification(
        "Noam Bar-Lev", "noam@example.com", "Tel Aviv", HomeType.APARTMENT,
        ExperienceLevel.NONE, ActivityLevel.LOW, 1.5,
        (Species.HAMSTER, Species.GUINEA_PIG), AnimalSize.SMALL, PreferredAgeRange.ANY,
        "Student in a shared flat. Small, quiet and low-cost is the brief.",
    ),
    AdopterSpecification(
        "Hila Weiss", "hila@example.com", "Netanya", HomeType.APARTMENT,
        ExperienceLevel.SOME, ActivityLevel.LOW, 8.0,
        (Species.CAT,), AnimalSize.SMALL, PreferredAgeRange.SENIOR,
        "Widowed, at home all day, and specifically asking for an older cat.",
    ),
    AdopterSpecification(
        "Avi Shalev", "avi@example.com", "Modiin", HomeType.FARM,
        ExperienceLevel.EXPERIENCED, ActivityLevel.HIGH, 9.0,
        (Species.DOG, Species.OTHER), AnimalSize.LARGE, PreferredAgeRange.ANY,
        "Keeps chickens and two farm dogs on four dunams outside the city.",
        has_yard=True, yard_size_sqm=4000, household_has_children=True,
        youngest_child_age=14,
        other_animals_description="Two farm dogs and a flock of twelve chickens",
    ),
    AdopterSpecification(
        "Michal Dayan", "michal@example.com", "Rehovot", HomeType.HOUSE,
        ExperienceLevel.SOME, ActivityLevel.MODERATE, 3.0,
        (Species.DOG, Species.CAT), AnimalSize.MEDIUM, PreferredAgeRange.ADULT,
        "House with a garden and a two-year-old. Needs an animal proven with toddlers.",
        has_yard=True, yard_size_sqm=80, household_has_children=True,
        youngest_child_age=2,
    ),
    AdopterSpecification(
        "Ronen Azoulay", "ronen@example.com", "Haifa", HomeType.HOUSE,
        ExperienceLevel.EXPERIENCED, ActivityLevel.MODERATE, 6.0,
        (Species.CAT, Species.DOG, Species.RABBIT), None, PreferredAgeRange.SENIOR,
        "Veterinary nurse who takes the animals nobody else will.",
        has_yard=True, yard_size_sqm=150,
        other_animals_description="Three rescue cats and a senior beagle",
    ),
    AdopterSpecification(
        "Yael Sabag", "yael@example.com", "Kfar Saba", HomeType.APARTMENT,
        ExperienceLevel.NONE, ActivityLevel.MODERATE, 2.0,
        (Species.RABBIT,), AnimalSize.SMALL, PreferredAgeRange.BABY,
        "Has read three books about rabbits and owned none.",
        open_to_proactive_suggestions=False,
    ),
    AdopterSpecification(
        "Itamar Ohana", "itamar@example.com", "Tel Aviv", HomeType.APARTMENT,
        ExperienceLevel.SOME, ActivityLevel.LOW, 0.5,
        (Species.CAT,), AnimalSize.SMALL, PreferredAgeRange.SENIOR,
        "Works twelve-hour days. Honest about having very little time.",
        open_to_proactive_suggestions=False,
    ),
    AdopterSpecification(
        "Sivan Elbaz", "sivan@example.com", "Holon", HomeType.HOUSE,
        ExperienceLevel.SOME, ActivityLevel.HIGH, 5.0,
        (Species.DOG,), AnimalSize.LARGE, PreferredAgeRange.BABY,
        "Two teenagers who have promised to do the walking.",
        has_yard=True, yard_size_sqm=80, household_has_children=True,
        youngest_child_age=15,
        other_animals_description="An elderly labrador who sleeps through everything",
    ),
    AdopterSpecification(
        "Dror Halevi", "dror@example.com", "Jerusalem", HomeType.HOUSE,
        ExperienceLevel.EXPERIENCED, ActivityLevel.MODERATE, 4.0,
        (Species.BIRD,), AnimalSize.SMALL, PreferredAgeRange.ANY,
        "Keeps an aviary and knows exactly how loud a conure is.",
        other_animals_description="A cockatiel and two canaries in a garden aviary",
    ),
    AdopterSpecification(
        "Efrat Nissim", "efrat@example.com", "Raanana", HomeType.APARTMENT,
        ExperienceLevel.EXPERIENCED, ActivityLevel.LOW, 3.0,
        (Species.OTHER,), AnimalSize.SMALL, PreferredAgeRange.ANY,
        "Keeps reptiles, has the equipment already, and has a thirteen-year-old helper.",
        household_has_children=True, youngest_child_age=13,
    ),
    AdopterSpecification(
        "Boaz Kaplan", "boaz@example.com", "Tiberias", HomeType.FARM,
        ExperienceLevel.EXPERIENCED, ActivityLevel.HIGH, 10.0,
        (Species.OTHER, Species.DOG), AnimalSize.LARGE, PreferredAgeRange.ANY,
        "Smallholder with goats, geese and space for more.",
        has_yard=True, yard_size_sqm=12000,
        other_animals_description="Four goats, six geese and a donkey",
    ),
    AdopterSpecification(
        "Orit Zohar", "orit@example.com", "Ashkelon", HomeType.APARTMENT,
        ExperienceLevel.SOME, ActivityLevel.LOW, 2.5,
        (Species.CAT, Species.RABBIT), AnimalSize.SMALL, PreferredAgeRange.ADULT,
        "Quiet flat, quiet life, and a balcony with a lot of plants.",
    ),
    AdopterSpecification(
        "Nadav Shemesh", "nadav@example.com", "Ramat Gan", HomeType.HOUSE,
        ExperienceLevel.SOME, ActivityLevel.HIGH, 4.0,
        (Species.DOG,), AnimalSize.MEDIUM, PreferredAgeRange.ADULT,
        "Trail runner looking for a companion who can do fifteen kilometres.",
        has_yard=True, yard_size_sqm=40,
    ),
    AdopterSpecification(
        "Keren Almog", "keren@example.com", "Beer Sheva", HomeType.APARTMENT,
        ExperienceLevel.NONE, ActivityLevel.MODERATE, 3.5,
        (Species.CAT, Species.GUINEA_PIG), AnimalSize.SMALL, PreferredAgeRange.ADULT,
        "Single parent of a five-year-old in a third-floor flat.",
        household_has_children=True, youngest_child_age=5,
    ),
    AdopterSpecification(
        "Uri Ben-Ami", "uri@example.com", "Petah Tikva", HomeType.HOUSE,
        ExperienceLevel.EXPERIENCED, ActivityLevel.HIGH, 7.0,
        (Species.DOG,), AnimalSize.LARGE, PreferredAgeRange.ADULT,
        "Competition obedience trainer. Wants a project, not a pet.",
        has_yard=True, yard_size_sqm=150,
        other_animals_description="A malinois competing in obedience",
    ),
    AdopterSpecification(
        "Talia Mor", "talia@example.com", "Herzliya", HomeType.HOUSE,
        ExperienceLevel.EXPERIENCED, ActivityLevel.LOW, 6.5,
        (Species.CAT, Species.DOG), None, PreferredAgeRange.SENIOR,
        "Takes terminal and geriatric animals so they do not die in a shelter.",
        has_yard=True, yard_size_sqm=80,
        other_animals_description="Two geriatric cats, both on daily medication",
    ),
    AdopterSpecification(
        "Guy Ashkenazi", "guy@example.com", "Tel Aviv", HomeType.APARTMENT,
        ExperienceLevel.NONE, ActivityLevel.MODERATE, 2.0,
        (Species.HAMSTER,), AnimalSize.SMALL, PreferredAgeRange.BABY,
        "Started the form during exam week and never came back to it.",
        open_to_proactive_suggestions=False, is_complete=False,
    ),
    AdopterSpecification(
        "Lior Tzur", "lior@example.com", "Jerusalem", HomeType.APARTMENT,
        ExperienceLevel.SOME, ActivityLevel.LOW, 3.0,
        (), None, PreferredAgeRange.ANY,
        "Registered, chose no species, and has not finished the profile.",
        is_complete=False,
    ),
    AdopterSpecification(
        "Adi Peled", "adi@example.com", "Ashdod", HomeType.HOUSE,
        ExperienceLevel.NONE, ActivityLevel.MODERATE, 4.0,
        (Species.DOG,), AnimalSize.MEDIUM, PreferredAgeRange.ADULT,
        "Profile still incomplete: the household questions are unanswered.",
        has_yard=True, is_complete=False,
    ),
    AdopterSpecification(
        "Shai Yarkoni", "shai@example.com", "Modiin", HomeType.HOUSE,
        ExperienceLevel.SOME, ActivityLevel.MODERATE, 4.0,
        (Species.DOG, Species.RABBIT), AnimalSize.MEDIUM, PreferredAgeRange.ADULT,
        "Garden, trampoline, and an eight-year-old who wants a dog badly.",
        has_yard=True, yard_size_sqm=80, household_has_children=True,
        youngest_child_age=8,
    ),
    AdopterSpecification(
        "Netta Oren", "netta@example.com", "Rehovot", HomeType.APARTMENT,
        ExperienceLevel.SOME, ActivityLevel.LOW, 3.0,
        (Species.RABBIT, Species.GUINEA_PIG), AnimalSize.SMALL, PreferredAgeRange.ANY,
        "Already keeps a bonded rabbit pair and understands the commitment.",
        other_animals_description="A bonded pair of house rabbits",
    ),
    AdopterSpecification(
        "Moran Malka", "moran@example.com", "Nazareth", HomeType.FARM,
        ExperienceLevel.EXPERIENCED, ActivityLevel.HIGH, 8.0,
        (Species.DOG, Species.OTHER), AnimalSize.LARGE, PreferredAgeRange.ANY,
        "Family farm with a ten-year-old, three dogs and a paddock.",
        has_yard=True, yard_size_sqm=8000, household_has_children=True,
        youngest_child_age=10,
        other_animals_description="Three farm dogs and two horses",
    ),
    AdopterSpecification(
        "Eyal Hadad", "eyal@example.com", "Holon", HomeType.APARTMENT,
        ExperienceLevel.SOME, ActivityLevel.HIGH, 3.0,
        (Species.CAT,), AnimalSize.MEDIUM, PreferredAgeRange.BABY,
        "Wants a kitten and is prepared for the consequences.",
        open_to_proactive_suggestions=False,
    ),
    AdopterSpecification(
        "Bat-El Shalom", "batel@example.com", "Akko", HomeType.APARTMENT,
        ExperienceLevel.SOME, ActivityLevel.MODERATE, 4.0,
        (Species.CAT, Species.BIRD), AnimalSize.SMALL, PreferredAgeRange.ANY,
        "Sixteen-year-old in the house who has done all the research.",
        household_has_children=True, youngest_child_age=16,
    ),
    AdopterSpecification(
        "Yonatan Gross", "yonatan@example.com", "Kfar Saba", HomeType.HOUSE,
        ExperienceLevel.NONE, ActivityLevel.MODERATE, 3.0,
        (Species.DOG, Species.CAT), AnimalSize.MEDIUM, PreferredAgeRange.ADULT,
        "First-time adopter with a garden and no fixed idea what he wants.",
        has_yard=True, yard_size_sqm=40,
    ),
    AdopterSpecification(
        "Shlomit Bar-On", "shlomit@example.com", "Raanana", HomeType.HOUSE,
        ExperienceLevel.EXPERIENCED, ActivityLevel.LOW, 7.0,
        (Species.DOG, Species.CAT), AnimalSize.SMALL, PreferredAgeRange.SENIOR,
        "Retired with a walled garden and forty years of dogs behind her.",
        has_yard=True, yard_size_sqm=150,
    ),
    AdopterSpecification(
        "Amir Kadosh", "amir@example.com", "Eilat", HomeType.APARTMENT,
        ExperienceLevel.SOME, ActivityLevel.LOW, 2.0,
        (Species.CAT,), AnimalSize.SMALL, PreferredAgeRange.SENIOR,
        "One cat already, and room for one more if the two get on.",
        other_animals_description="A ten-year-old tabby, indoors only",
        open_to_proactive_suggestions=False,
    ),
    AdopterSpecification(
        "Tehila Rubin", "tehila@example.com", "Jerusalem", HomeType.HOUSE,
        ExperienceLevel.NONE, ActivityLevel.MODERATE, 5.0,
        (Species.GUINEA_PIG, Species.RABBIT), AnimalSize.SMALL, PreferredAgeRange.BABY,
        "Garden, a three-year-old, and a hutch already built.",
        has_yard=True, yard_size_sqm=40, household_has_children=True,
        youngest_child_age=3,
    ),
)


@dataclass(frozen=True)
class StaffSpecification:
    """A member of organization staff."""

    full_name: str
    email: str


# The first entry is the account README.md tells a reviewer to sign in with,
# and the one the end-to-end suite expects, so it stays first and unrenamed.
STAFF_SPECIFICATIONS: tuple[StaffSpecification, ...] = (
    StaffSpecification("Dana Aviv", "dana@petmatch.org"),
    StaffSpecification("Itai Segal", "itai@petmatch.org"),
    StaffSpecification("Rinat Amsalem", "rinat@petmatch.org"),
)

# Named so the seed script can print them and the tests can assert they exist.
DEMO_STAFF_EMAIL = STAFF_SPECIFICATIONS[0].email
DEMO_ADOPTER_EMAIL = ADOPTER_SPECIFICATIONS[0].email


def validate_adopter_specification(
    specification: AdopterSpecification,
) -> tuple[str, ...]:
    """Report everything wrong with one adopter entry.

    Mirrors the rules `app/domain/profile_rules.py` applies to a submitted
    form, so seeded data cannot be something the application would have
    rejected: a child's age is required when children are present and
    forbidden when they are not, and hours must be a real part of a day.

    Args:
        specification: The entry to check.

    Returns:
        A tuple of problem descriptions, empty when the entry is sound.
    """
    problems: list[str] = []

    if not specification.full_name.strip():
        problems.append("name is empty")
    if "@" not in specification.email or specification.email != specification.email.lower():
        problems.append(f"'{specification.email}' is not a normalised email address")
    if not specification.city.strip():
        problems.append("city is empty")
    if not specification.summary.strip():
        problems.append("summary is empty")
    if not MINIMUM_DAILY_HOURS <= specification.daily_hours_available <= MAXIMUM_DAILY_HOURS:
        problems.append(f"{specification.daily_hours_available} is not a plausible daily figure")

    problems.extend(_child_age_problems(specification))
    problems.extend(_yard_problems(specification))
    return tuple(problems)


def _child_age_problems(specification: AdopterSpecification) -> tuple[str, ...]:
    """Check the children flag against the youngest child's age."""
    if specification.household_has_children and specification.youngest_child_age is None:
        return ("children are in the household but no age is given",)
    if not specification.household_has_children and specification.youngest_child_age is not None:
        return ("a child's age is given but no children are in the household",)

    age = specification.youngest_child_age
    if age is not None and not MINIMUM_CHILD_AGE <= age <= MAXIMUM_CHILD_AGE:
        return (f"a youngest child aged {age} is not plausible",)
    return ()


def _yard_problems(specification: AdopterSpecification) -> tuple[str, ...]:
    """Check that a yard size is only recorded where there is a yard."""
    if specification.yard_size_sqm is None:
        return ()
    if not specification.has_yard:
        return ("a yard size is recorded but the household has no yard",)
    if specification.yard_size_sqm <= 0:
        return (f"a yard of {specification.yard_size_sqm} square metres is not plausible",)
    return ()
