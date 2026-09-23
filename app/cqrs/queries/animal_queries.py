"""Read-side queries for animals: search, details and staff listings.

Every handler in this module is read-only. None adds, deletes or commits;
the bus rolls their session back, which enforces the rule at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.cqrs.base import Query, QueryHandler
from app.domain.enums import ActivityLevel, AnimalSize, AnimalStatus, ApplicationStatus, Species
from app.infrastructure.models import AdoptionApplication, Animal
from app.infrastructure.sql_helpers import is_true

# A results page stays small enough to scan without scrolling forever.
DEFAULT_PAGE_SIZE = 12


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
        """Species formatted for display."""
        return self.species.replace("_", " ").title()

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
    city: str | None = None
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
                self.city,
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


def _to_card(animal: Animal) -> AnimalCard:
    """Convert an ORM animal into a grid card."""
    return AnimalCard(
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


def _apply_filters(statement: Select, filters: AnimalSearchFilters) -> Select:
    """Narrow a select statement by the supplied filters.

    Extracted from the handler so each condition stays readable and the
    handler itself reads as a sequence of steps rather than a wall of
    conditionals.
    """
    if filters.available_only:
        statement = statement.where(Animal.status == AnimalStatus.AVAILABLE.value)
    if filters.species:
        statement = statement.where(Animal.species == filters.species)
    if filters.size:
        statement = statement.where(Animal.size == filters.size)
    if filters.activity_level:
        statement = statement.where(Animal.activity_level == filters.activity_level)
    if filters.city:
        statement = statement.where(Animal.city == filters.city)
    if filters.good_with_children:
        statement = statement.where(is_true(Animal.good_with_children))
    if filters.good_with_other_animals:
        statement = statement.where(is_true(Animal.good_with_other_animals))
    if filters.text:
        pattern = f"%{filters.text.strip()}%"
        statement = statement.where(
            or_(
                Animal.name.like(pattern),
                Animal.breed.like(pattern),
                Animal.description.like(pattern),
            )
        )
    return statement


class SearchAnimalsHandler(QueryHandler[AnimalSearchResults]):
    """Answers SearchAnimalsQuery."""

    def handle(self, query: Query, session: Session) -> AnimalSearchResults:
        """Return one page of animals matching the filters."""
        assert isinstance(query, SearchAnimalsQuery)

        base = select(Animal).options(selectinload(Animal.images))
        filtered = _apply_filters(base, query.filters)

        count_statement = _apply_filters(select(func.count()).select_from(Animal), query.filters)
        total_count = int(session.execute(count_statement).scalar_one())

        offset = max(0, (query.page - 1) * query.page_size)
        page_statement = (
            filtered.order_by(Animal.name).offset(offset).limit(query.page_size)
        )
        animals = session.execute(page_statement).scalars().all()

        return AnimalSearchResults(
            animals=[_to_card(animal) for animal in animals],
            total_count=total_count,
            page=query.page,
            page_size=query.page_size,
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

        card = _to_card(animal)
        return AnimalDetails(
            **card.__dict__,
            description=animal.description,
            special_needs_description=animal.special_needs_description,
            required_space=animal.required_space,
            image_urls=[image.image_url for image in animal.images],
            active_application_count=active_application_count,
        )


class ListAllAnimalsHandler(QueryHandler[list[AnimalCard]]):
    """Answers ListAllAnimalsQuery for the staff management table."""

    def handle(self, query: Query, session: Session) -> list[AnimalCard]:
        """Return every animal, optionally narrowed by status or species."""
        assert isinstance(query, ListAllAnimalsQuery)

        statement = select(Animal).options(selectinload(Animal.images))
        if query.status:
            statement = statement.where(Animal.status == query.status)
        if query.species:
            statement = statement.where(Animal.species == query.species)

        animals = session.execute(statement.order_by(Animal.name)).scalars().all()
        return [_to_card(animal) for animal in animals]


def available_filter_options() -> dict[str, list[str]]:
    """Enumerate the values offered in the search form's dropdowns."""
    return {
        "species": [member.value for member in Species],
        "size": [member.value for member in AnimalSize],
        "activity_level": [member.value for member in ActivityLevel],
    }
