"""Read-side queries for animals: search, details and staff listings.

Every handler in this module is read-only. None adds, deletes or commits;
the bus rolls their session back, which enforces the rule at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, TypeVar

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.cqrs.base import Query, QueryHandler
from app.domain.enums import ActivityLevel, AnimalSize, AnimalStatus, ApplicationStatus, Species
from app.infrastructure.models import AdoptionApplication, Animal
from app.infrastructure.sql_helpers import is_true

# A results page stays small enough to scan without scrolling forever.
# Sized for the real roster rather than the demo one: at twelve, the
# current 157 animals took fourteen pages to look through.
DEFAULT_PAGE_SIZE = 24

# The staff table is denser than the public grid - one row per animal
# rather than a card - so it can carry more before it stops being
# scannable.
STAFF_PAGE_SIZE = 40


@dataclass(frozen=True)
class AnimalCard:
    """An animal as shown in a results grid."""

    animal_id: str
    name: str
    species: str
    breed: str | None
    age_years: float
    size: str
    temperament: str
    activity_level: str
    city: str
    status: str
    image_url: str | None
    good_with_children: bool
    good_with_other_animals: bool
    has_special_needs: bool
    # How well this animal suits the adopter the card is being shown to,
    # 0-100. Set only by the match queries, which know whose profile to
    # score against; a public search has no adopter and leaves it None, so
    # a card can tell "no score was computed" from "scored badly".
    match_score: int | None = None

    @property
    def has_match_score(self) -> bool:
        """Whether a personal score was computed for this card.

        `if animal.match_score` would hide a genuine score of 0, which is
        what a disqualified pairing scores (app/domain/matching.py).
        """
        return self.match_score is not None

    @property
    def age_label(self) -> str:
        """Human-readable age, avoiding '0.5 years old'."""
        if self.age_years < 1:
            months = max(1, round(self.age_years * 12))
            return f"{months} month{'s' if months != 1 else ''}"
        whole_years = int(self.age_years)
        return f"{whole_years} year{'s' if whole_years != 1 else ''}"

    @property
    def species_label(self) -> str:
        """Species formatted for display.

        OTHER is a bucket rather than a kind of animal, and a card reading
        "Other" tells a browsing adopter nothing. The breed carries the
        real description for those records - "Ferret", "Corn snake" - so
        it is shown instead when there is one.
        """
        if self.species == Species.OTHER.value and self.breed:
            return self.breed
        return self.species.replace("_", " ").capitalize()

    @property
    def breed_note(self) -> str | None:
        """The breed, unless it is already doing duty as the species label.

        Templates show "species · breed". For an OTHER-species animal the
        label *is* the breed, so without this the card would read
        "Ferret · Ferret".
        """
        if self.breed and self.breed != self.species_label:
            return self.breed
        return None

    @property
    def is_available(self) -> bool:
        """Whether this animal currently accepts applications."""
        return self.status == AnimalStatus.AVAILABLE.value


@dataclass(frozen=True)
class AnimalDetails(AnimalCard):
    """Everything shown on an animal's details page."""

    description: str | None = None
    special_needs_description: str | None = None
    required_space: str = ""
    image_urls: list[str] = field(default_factory=list)
    active_application_count: int = 0


@dataclass(frozen=True)
class AnimalSearchFilters:
    """Structured search criteria (spec 6.2)."""

    text: str | None = None
    species: str | None = None
    size: str | None = None
    activity_level: str | None = None
    # A `Temperament` value, or None for no constraint. Interpreted intent
    # has always parsed one and the describe screen has always shown it as an
    # understood criterion; without a filter here that tag was a promise the
    # search did not keep (spec section 6.3).
    temperament: str | None = None
    city: str | None = None
    # One of AGE_RANGE_BOUNDS' keys, or None for no age constraint. Spec
    # section 6.2 names age among the filters the search must offer.
    age_range: str | None = None
    good_with_children: bool = False
    good_with_other_animals: bool = False
    available_only: bool = True

    @property
    def is_empty(self) -> bool:
        """Whether the user supplied any criteria beyond the defaults."""
        return not any(
            (
                self.text,
                self.species,
                self.size,
                self.activity_level,
                self.temperament,
                self.city,
                self.age_range,
                self.good_with_children,
                self.good_with_other_animals,
            )
        )


@dataclass(frozen=True)
class SearchAnimalsQuery(Query):
    """Find animals matching structured filters."""

    filters: AnimalSearchFilters
    page: int = 1
    page_size: int = DEFAULT_PAGE_SIZE


@dataclass(frozen=True)
class AnimalSearchResults:
    """A page of search results."""

    animals: list[AnimalCard]
    total_count: int
    page: int
    page_size: int

    @property
    def page_count(self) -> int:
        """Total number of pages, at least one."""
        if self.total_count == 0:
            return 1
        return (self.total_count + self.page_size - 1) // self.page_size

    @property
    def has_previous(self) -> bool:
        """Whether an earlier page exists."""
        return self.page > 1

    @property
    def has_next(self) -> bool:
        """Whether a later page exists."""
        return self.page < self.page_count


@dataclass(frozen=True)
class GetAnimalDetailsQuery(Query):
    """Fetch one animal's full record."""

    animal_id: str


@dataclass(frozen=True)
class ListAllAnimalsQuery(Query):
    """Staff listing of every animal, regardless of status."""

    status: str | None = None
    species: str | None = None
    size: str | None = None
    # One of AGE_RANGE_BOUNDS' keys, or None. Spec section 7.1 names age and
    # size among the filters the staff table must offer.
    age_range: str | None = None
    text: str | None = None
    page: int = 1
    page_size: int = STAFF_PAGE_SIZE


# The age bands both filters offer, matching the vocabulary the seed data
# and `AdopterProfile.preferred_age_range` already use ("0-2 years" and so
# on). Bounds are (minimum inclusive, maximum exclusive); None is unbounded,
# so the three bands tile the whole range with no gap and no overlap - an
# animal of exactly 2.0 years is an adult, not both.
AGE_RANGE_BOUNDS: dict[str, tuple[float | None, float | None]] = {
    "0-2": (None, 2.0),
    "2-8": (2.0, 8.0),
    "8+": (8.0, None),
}


def parse_age_range(raw_value: str | None) -> str | None:
    """Read an age band from a query string, dropping anything unrecognised.

    Spec section 6.2 and section 7.1 both name age as a filter. The value
    arrives from a URL, so it is whatever somebody typed there: an
    unrecognised band becomes None, meaning "no age constraint", rather than
    an error page or a filter matching nothing.

    Args:
        raw_value: The raw `age_range` parameter, which may be absent.

    Returns:
        One of `AGE_RANGE_BOUNDS`' keys, or None.
    """
    candidate = (raw_value or "").strip()
    if candidate in AGE_RANGE_BOUNDS:
        return candidate
    return None


def to_animal_card(animal: Animal, match_score: int | None = None) -> AnimalCard:
    """Convert an ORM animal into a grid card.

    Public because the match queries build the same card for a results grid
    and must not grow a second, drifting copy of it (docs/UX.md section 3:
    the animal card is one definition).

    Args:
        animal: The row to convert.
        match_score: How well this animal suits the adopter being served,
            when there is one. Omitted for an anonymous listing.

    Returns:
        The card view model.
    """
    return AnimalCard(
        match_score=match_score,
        animal_id=animal.animal_id,
        name=animal.name,
        species=animal.species,
        breed=animal.breed,
        age_years=float(animal.age_years),
        size=animal.size,
        temperament=animal.temperament,
        activity_level=animal.activity_level,
        city=animal.city,
        status=animal.status,
        image_url=animal.primary_image_url,
        good_with_children=animal.good_with_children,
        good_with_other_animals=animal.good_with_other_animals,
        has_special_needs=animal.has_special_needs,
    )


SelectT = TypeVar("SelectT", bound=Select[Any])


def apply_search_filters(statement: SelectT, filters: AnimalSearchFilters) -> SelectT:
    """Narrow a select statement by the supplied filters.

    Generic over the statement type because the same filters are applied
    both to the page query and to its `COUNT(*)` companion; the two select
    different things, and each must keep its own row type on the way out.

    Extracted from the handler so each condition stays readable and the
    handler itself reads as a sequence of steps rather than a wall of
    conditionals. Public because the profile-fused search in
    `app.cqrs.queries.match_queries` narrows by the same filters, and two
    implementations of "matching the intent" would eventually disagree
    about what the visitor asked for (spec section 6.4).

    Args:
        statement: The select to narrow.
        filters: The criteria to apply. Absent criteria narrow nothing.

    Returns:
        The statement with one WHERE clause per supplied criterion.
    """
    statement = _apply_age_band(statement, filters.age_range)
    statement = _apply_exact_matches(statement, filters)
    if filters.good_with_children:
        statement = statement.where(is_true(Animal.good_with_children))
    if filters.good_with_other_animals:
        statement = statement.where(is_true(Animal.good_with_other_animals))
    if filters.text:
        pattern = f"%{_escape_like(filters.text.strip())}%"
        statement = statement.where(
            or_(
                Animal.name.like(pattern, escape=LIKE_ESCAPE),
                Animal.breed.like(pattern, escape=LIKE_ESCAPE),
                Animal.description.like(pattern, escape=LIKE_ESCAPE),
            )
        )
    return statement


def _apply_exact_matches(statement: SelectT, filters: AnimalSearchFilters) -> SelectT:
    """Narrow by every criterion that is a plain column equality.

    A table rather than one `if` per column: they all read the same, and the
    list of them belongs next to `AnimalSearchFilters.is_empty`, which has to
    name the same set.

    Args:
        statement: The select to narrow.
        filters: The criteria to apply. An absent criterion narrows nothing.

    Returns:
        The statement with one WHERE clause per supplied criterion.
    """
    if filters.available_only:
        statement = statement.where(Animal.status == AnimalStatus.AVAILABLE.value)

    for column, requested in (
        (Animal.species, filters.species),
        (Animal.size, filters.size),
        (Animal.activity_level, filters.activity_level),
        (Animal.temperament, filters.temperament),
        (Animal.city, filters.city),
    ):
        if requested:
            statement = statement.where(column == requested)
    return statement


def _apply_age_band(statement: SelectT, age_range: str | None) -> SelectT:
    """Narrow a select statement to one age band.

    Shared by the public search and the staff table so "2-8" means the same
    span of years on both screens. An unrecognised band narrows nothing,
    which is what `parse_age_range` has usually already ensured.

    Args:
        statement: The select to narrow.
        age_range: One of `AGE_RANGE_BOUNDS`' keys, or None.

    Returns:
        The statement, narrowed when the band was recognised.
    """
    bounds = AGE_RANGE_BOUNDS.get((age_range or "").strip())
    if bounds is None:
        return statement

    minimum, maximum = bounds
    if minimum is not None:
        statement = statement.where(Animal.age_years >= minimum)
    if maximum is not None:
        statement = statement.where(Animal.age_years < maximum)
    return statement


# Backslash rather than one of the characters being escaped, so the escape
# itself never needs escaping twice over.
LIKE_ESCAPE = "\\"


def _escape_like(text: str) -> str:
    """Make a search term match itself literally inside a LIKE pattern.

    LIKE has its own metacharacters. Without this, searching for "%" matches
    every animal rather than the ones containing a percent sign, and a real
    term such as "100%" or "guinea_pig" quietly matches the wrong rows. This
    is not an injection fix - the value is still bound as a parameter - it is
    a correctness one.

    Args:
        text: The raw search term.

    Returns:
        The term with LIKE's wildcards neutralised.
    """
    escaped = text.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
    for metacharacter in ("%", "_", "["):
        escaped = escaped.replace(metacharacter, LIKE_ESCAPE + metacharacter)
    return escaped


def clamp_pagination(page: int, page_size: int) -> tuple[int, int]:
    """Bring a requested page and size into the range a query can use.

    Both listings paginate and each clamped differently: the staff table
    floored both at one, while the public search clamped only the computed
    offset - so `?page=0` rendered page one's rows while the pager beneath
    them reported page 0, with "previous" enabled and pointing at page -1.
    One rule, applied by both.

    Args:
        page: The requested page number, one-based.
        page_size: The requested rows per page.

    Returns:
        The page and size to use, each at least one.
    """
    return max(1, page), max(1, page_size)


class SearchAnimalsHandler(QueryHandler[AnimalSearchResults]):
    """Answers SearchAnimalsQuery."""

    def handle(self, query: Query, session: Session) -> AnimalSearchResults:
        """Return one page of animals matching the filters."""
        assert isinstance(query, SearchAnimalsQuery)

        base = select(Animal).options(selectinload(Animal.images))
        filtered = apply_search_filters(base, query.filters)

        count_statement = apply_search_filters(
            select(func.count()).select_from(Animal), query.filters
        )
        total_count = int(session.execute(count_statement).scalar_one())

        page, page_size = clamp_pagination(query.page, query.page_size)
        page_statement = (
            filtered.order_by(Animal.name).offset((page - 1) * page_size).limit(page_size)
        )
        animals = session.execute(page_statement).scalars().all()

        return AnimalSearchResults(
            animals=[to_animal_card(animal) for animal in animals],
            total_count=total_count,
            page=page,
            page_size=page_size,
        )


class GetAnimalDetailsHandler(QueryHandler[AnimalDetails | None]):
    """Answers GetAnimalDetailsQuery."""

    def handle(self, query: Query, session: Session) -> AnimalDetails | None:
        """Return the animal's full record, or None when it does not exist."""
        assert isinstance(query, GetAnimalDetailsQuery)

        animal = session.execute(
            select(Animal)
            .options(selectinload(Animal.images))
            .where(Animal.animal_id == query.animal_id)
        ).scalar_one_or_none()

        if animal is None:
            return None

        active_statuses = [
            status.value for status in ApplicationStatus if status.is_active
        ]
        active_application_count = int(
            session.execute(
                select(func.count())
                .select_from(AdoptionApplication)
                .where(AdoptionApplication.animal_id == animal.animal_id)
                .where(AdoptionApplication.status.in_(active_statuses))
            ).scalar_one()
        )

        card = to_animal_card(animal)
        return AnimalDetails(
            **card.__dict__,
            description=animal.description,
            special_needs_description=animal.special_needs_description,
            required_space=animal.required_space,
            image_urls=[image.image_url for image in animal.images],
            active_application_count=active_application_count,
        )


class ListAllAnimalsHandler(QueryHandler[AnimalSearchResults]):
    """Answers ListAllAnimalsQuery for the staff management table.

    Paginated, and returning the same result shape as the public search so
    the template can use the same paginator. Before this the table rendered
    every row: fine for a demo roster of twelve, unusable at the real one
    of a hundred and fifty-seven, and a table nobody can scan is not a
    management tool.
    """

    def handle(self, query: Query, session: Session) -> AnimalSearchResults:
        """Return one page of animals, narrowed by the staff filters."""
        assert isinstance(query, ListAllAnimalsQuery)

        statement = select(Animal).options(selectinload(Animal.images))
        statement = _apply_staff_filters(statement, query)

        total = int(
            session.execute(
                _apply_staff_filters(select(func.count()).select_from(Animal), query)
            ).scalar_one()
        )

        page, page_size = clamp_pagination(query.page, query.page_size)
        animals = (
            session.execute(
                statement.order_by(Animal.name)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            .scalars()
            .all()
        )

        return AnimalSearchResults(
            animals=[to_animal_card(animal) for animal in animals],
            total_count=total,
            page=page,
            page_size=page_size,
        )


def _apply_staff_filters(statement: SelectT, query: ListAllAnimalsQuery) -> SelectT:
    """Narrow a staff listing by status, species, size, age and free text.

    Separate from `apply_search_filters` because the staff table filters on
    different things: every status is visible here, which is the whole
    point of the screen, and the free-text search covers the city rather
    than the description.

    Args:
        statement: The select to narrow.
        query: The staff listing request.

    Returns:
        The statement with one WHERE clause per supplied criterion.
    """
    statement = _apply_age_band(statement, query.age_range)
    if query.status:
        statement = statement.where(Animal.status == query.status)
    if query.species:
        statement = statement.where(Animal.species == query.species)
    if query.size:
        statement = statement.where(Animal.size == query.size)
    if query.text:
        pattern = f"%{_escape_like(query.text.strip())}%"
        statement = statement.where(
            or_(
                Animal.name.like(pattern, escape=LIKE_ESCAPE),
                Animal.breed.like(pattern, escape=LIKE_ESCAPE),
                Animal.city.like(pattern, escape=LIKE_ESCAPE),
            )
        )
    return statement


def available_filter_options() -> dict[str, list[str]]:
    """Enumerate the values offered in the search form's dropdowns.

    The age bands come from `AGE_RANGE_BOUNDS` rather than being written out
    in the template, so a band added here appears in the form and is
    understood by the query in the same edit.
    """
    return {
        "species": [member.value for member in Species],
        "size": [member.value for member in AnimalSize],
        "activity_level": [member.value for member in ActivityLevel],
        "age_range": list(AGE_RANGE_BOUNDS),
    }


def species_filter_label(species_value: str) -> str:
    """Label one species option in the search form.

    OTHER covers everything with no enum of its own - ferrets, reptiles,
    the occasional parrot - so "Other" alone reads like a dead end rather
    than a category worth opening.
    """
    if species_value == Species.OTHER.value:
        return "Other / exotic"
    return species_value.replace("_", " ").capitalize()
