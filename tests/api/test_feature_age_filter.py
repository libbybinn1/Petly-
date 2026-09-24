"""Tests for the age-band filter on search and on the staff table (spec 6.2, 7.1).

Spec section 6.2 names age among the filters the public search must offer and
section 7.1 repeats it for the staff table; neither had one, and the
requirements checker reported "the search filters carry no age field".

The bands are `0-2`, `2-8` and `8+`, matching the vocabulary the seed data and
`AdopterProfile.preferred_age_range` already use. They tile the whole range
with no gap and no overlap, which is why the boundary animals matter here:
an animal of exactly two years belongs to one band and not to two.

The value arrives from a URL anyone can edit, so the last test is the one that
keeps this honest - an unrecognised band must mean "no age constraint" rather
than an error page or a filter that silently matches nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from app.cqrs.queries.animal_queries import AGE_RANGE_BOUNDS, parse_age_range
from app.domain.enums import (
    ActivityLevel,
    AnimalSize,
    AnimalStatus,
    Species,
    Temperament,
)
from app.infrastructure.models import Animal, new_identifier
from flask.testing import FlaskClient
from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.api

# One animal per band plus both boundaries, so an off-by-one in either
# comparison moves a named animal from one result set to another.
AGES_BY_NAME = {
    "Pip": 0.5,     # 0-2
    "Juno": 2.0,    # the lower boundary: an adult, not a youngster
    "Rocco": 5.0,   # 2-8
    "Alma": 8.0,    # the upper boundary: a senior, not an adult
    "Barney": 12.0,  # 8+
}


def _naive_now() -> datetime:
    """Naive UTC, as the DATETIME columns store."""
    return datetime.now(UTC).replace(tzinfo=None)


@pytest.fixture
def aged_roster(session_factory: sessionmaker[Session], world: dict[str, str]) -> None:
    """Add one available animal at each age worth testing."""
    with session_factory() as session:
        for name, age_years in AGES_BY_NAME.items():
            session.add(
                Animal(
                    animal_id=new_identifier(), name=name, species=Species.DOG.value,
                    age_years=age_years, size=AnimalSize.MEDIUM.value,
                    temperament=Temperament.BALANCED.value,
                    activity_level=ActivityLevel.MODERATE.value,
                    good_with_children=True, good_with_other_animals=True,
                    has_special_needs=False,
                    required_space=AnimalSize.MEDIUM.value, city="Haifa",
                    status=AnimalStatus.AVAILABLE.value,
                    created_at=_naive_now(), updated_at=_naive_now(),
                )
            )
        session.commit()


def names_in(body: str) -> set[str]:
    """Which of the test animals a rendered page mentions."""
    return {name for name in AGES_BY_NAME if name in body}


class TestParsingTheParameter:
    """The value is checked before it reaches a query (NFR-5.2)."""

    @pytest.mark.parametrize("band", list(AGE_RANGE_BOUNDS))
    def test_each_offered_band_is_accepted(self, band: str) -> None:
        """Proves the form's own options survive the round trip.

        Parametrised over the bands the query understands rather than over a
        written-out list, so a band added to one and not the other fails
        here instead of silently doing nothing on the page.
        """
        assert parse_age_range(band) == band

    @pytest.mark.parametrize(
        "raw_value",
        [None, "", "   ", "any", "0-2 years", "2to8", "-1", "8", "'; DROP TABLE"],
    )
    def test_anything_else_means_no_constraint(self, raw_value: str | None) -> None:
        """Proves an unrecognised band is dropped rather than obeyed or refused.

        A stale bookmark, a hand-edited URL and a probe all arrive the same
        way. Dropping the value records what is actually known about it,
        which is nothing - the same rule the stored species preference
        follows.
        """
        assert parse_age_range(raw_value) is None


class TestTheBandsOnThePublicSearch:
    """Spec 6.2, through the route an adopter uses."""

    def test_the_youngest_band_excludes_the_two_year_old(
        self, client: FlaskClient, aged_roster: None
    ) -> None:
        """Proves `0-2` is under two, not up to and including two.

        Juno is exactly two. The bands tile the range, so she has to fall on
        one side of this boundary and not on both.
        """
        body = client.get("/animals/?age_range=0-2").get_data(as_text=True)

        assert names_in(body) == {"Pip"}

    def test_the_middle_band_includes_its_lower_boundary(
        self, client: FlaskClient, aged_roster: None
    ) -> None:
        """Proves `2-8` runs from two inclusive to eight exclusive."""
        body = client.get("/animals/?age_range=2-8").get_data(as_text=True)

        assert names_in(body) == {"Juno", "Rocco"}

    def test_the_oldest_band_includes_its_lower_boundary(
        self, client: FlaskClient, aged_roster: None
    ) -> None:
        """Proves `8+` starts at eight, so no animal falls between the bands."""
        body = client.get("/animals/?age_range=8%2B").get_data(as_text=True)

        assert names_in(body) == {"Alma", "Barney"}

    def test_an_unknown_band_narrows_nothing(
        self, client: FlaskClient, aged_roster: None
    ) -> None:
        """Proves a bad value browses everything rather than erroring.

        The negative case, and the one an edited URL produces: the page must
        answer 200 with the unfiltered roster, not 400 and not an empty grid
        that looks like "we have no animals".
        """
        response = client.get("/animals/?age_range=ancient")

        assert response.status_code == 200
        assert names_in(response.get_data(as_text=True)) == set(AGES_BY_NAME)

    def test_the_band_combines_with_the_other_filters(
        self, client: FlaskClient, aged_roster: None
    ) -> None:
        """Proves age narrows alongside species rather than replacing it.

        Clover is a two-year-old rabbit from the shared world, so a rabbit
        in the 2-8 band is exactly her and none of the dogs.
        """
        body = client.get("/animals/?age_range=2-8&species=RABBIT").get_data(
            as_text=True
        )

        assert "Clover" in body
        assert names_in(body) == set()


class TestTheBandsOnTheStaffTable:
    """Spec 7.1 asks for age *and* size on the management screen."""

    def test_staff_can_narrow_the_roster_by_age(
        self, staff_client: FlaskClient, aged_roster: None
    ) -> None:
        """Proves the same vocabulary works on the staff table."""
        body = staff_client.get("/animals/manage?age_range=8%2B").get_data(as_text=True)

        assert names_in(body) == {"Alma", "Barney"}

    def test_staff_can_narrow_the_roster_by_size(
        self, staff_client: FlaskClient, aged_roster: None
    ) -> None:
        """Proves the size filter spec 7.1 names is wired up too.

        Every animal in this fixture is medium and Clover is small, so a
        small filter must return her alone.
        """
        body = staff_client.get("/animals/manage?size=SMALL").get_data(as_text=True)

        assert "Clover" in body
        assert names_in(body) == set()

    def test_an_unknown_band_does_not_break_the_table(
        self, staff_client: FlaskClient, aged_roster: None
    ) -> None:
        """Proves the staff route is as forgiving as the public one."""
        response = staff_client.get("/animals/manage?age_range=puppy")

        assert response.status_code == 200
        assert names_in(response.get_data(as_text=True)) == set(AGES_BY_NAME)
