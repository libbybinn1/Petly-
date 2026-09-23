"""Read-side query for an adopter's own profile."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cqrs.base import Query, QueryHandler
from app.infrastructure.models import AdopterProfile


@dataclass(frozen=True)
class AdopterProfileView:
    """An adopter's stored profile, shaped for the form."""

    adopter_profile_id: str
    home_type: str
    has_yard: bool
    yard_size_sqm: int | None
    household_has_children: bool
    youngest_child_age: int | None
    has_other_animals: bool
    other_animals_description: str | None
    experience_level: str
    activity_level: str
    daily_hours_available: float
    city: str
    preferred_species: list[str] = field(default_factory=list)
    preferred_age_range: str | None = None
    preferred_size: str | None = None
    open_to_proactive_suggestions: bool = False
    is_complete: bool = False


@dataclass(frozen=True)
class GetMyProfileQuery(Query):
    """Fetch one adopter's profile by the user who owns it.

    Keyed on user rather than profile identifier, so a caller cannot ask for
    somebody else's profile (FR-2.4).
    """

    user_id: str


class GetMyProfileHandler(QueryHandler[AdopterProfileView | None]):
    """Answers GetMyProfileQuery."""

    def handle(self, query: Query, session: Session) -> AdopterProfileView | None:
        """Return the profile, or None when the adopter has not created one."""
        assert isinstance(query, GetMyProfileQuery)

        row = session.execute(
            select(AdopterProfile).where(AdopterProfile.user_id == query.user_id)
        ).scalar_one_or_none()
        if row is None:
            return None

        return AdopterProfileView(
            adopter_profile_id=row.adopter_profile_id,
            home_type=row.home_type,
            has_yard=bool(row.has_yard),
            yard_size_sqm=row.yard_size_sqm,
            household_has_children=bool(row.household_has_children),
            youngest_child_age=row.youngest_child_age,
            has_other_animals=bool(row.has_other_animals),
            other_animals_description=row.other_animals_description,
            experience_level=row.experience_level,
            activity_level=row.activity_level,
            daily_hours_available=float(row.daily_hours_available or 0),
            city=row.city,
            preferred_age_range=row.preferred_age_range,
            preferred_size=row.preferred_size,
            preferred_species=[
                value.strip()
                for value in (row.preferred_species or "").split(",")
                if value.strip()
            ],
            open_to_proactive_suggestions=bool(row.open_to_proactive_suggestions),
            is_complete=bool(row.is_complete),
        )
