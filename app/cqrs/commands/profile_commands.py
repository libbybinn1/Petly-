"""Command for creating and updating an adopter profile (spec section 5.1)."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cqrs.base import Command, CommandHandler
from app.cqrs.commands.application_commands import RecordNotFoundError
from app.domain.enums import AggregateType, DomainEventType
from app.domain.profile_rules import ValidatedProfile
from app.eventstore.store import EventStore
from app.infrastructure.clock import utc_now
from app.infrastructure.models import AdopterProfile, User, new_identifier


@dataclass(frozen=True)
class SaveAdopterProfileCommand(Command):
    """Create or update the signed-in adopter's profile.

    One command for both cases: from the adopter's point of view there is a
    single "my profile" form, and splitting it would push the create-or-update
    decision into the controller for no benefit.
    """

    user_id: str
    profile: ValidatedProfile


class SaveAdopterProfileHandler(CommandHandler[str]):
    """Persists a validated profile and records the change."""

    def handle(self, command: Command, session: Session) -> str:
        """Write the profile, marking it complete and appending its event.

        Returns:
            The adopter profile's identifier.

        Raises:
            RecordNotFoundError: The user does not exist.
        """
        assert isinstance(command, SaveAdopterProfileCommand)

        user = session.get(User, command.user_id)
        if user is None:
            raise RecordNotFoundError("User does not exist.")

        existing = session.execute(
            select(AdopterProfile).where(AdopterProfile.user_id == command.user_id)
        ).scalar_one_or_none()

        was_already_complete = bool(existing and existing.is_complete)
        profile = existing or self._create_row(command.user_id, command.profile, session)

        self._apply(profile, command.profile)
        profile.updated_at = utc_now()

        # Reaching this point means validation passed, so every field the
        # matching engine needs is present. Completeness is derived from that
        # rather than asked of the adopter.
        profile.is_complete = True

        EventStore(session).append(
            DomainEventType.ADOPTER_PROFILE_UPDATED,
            AggregateType.ADOPTER_PROFILE,
            profile.adopter_profile_id,
            payload={
                "first_completion": not was_already_complete,
                "open_to_proactive_suggestions": (
                    command.profile.open_to_proactive_suggestions
                ),
            },
            actor_user_id=command.user_id,
        )

        return profile.adopter_profile_id

    @staticmethod
    def _create_row(
        user_id: str, validated: ValidatedProfile, session: Session
    ) -> AdopterProfile:
        """Add a profile row for a first-time submission.

        Created with the validated values already in place rather than blank
        and then updated. A blank row cannot be flushed: the enum columns
        carry CHECK constraints, and an empty string satisfies none of them.
        """
        profile = AdopterProfile(
            adopter_profile_id=new_identifier(),
            user_id=user_id,
            home_type=validated.home_type.value,
            has_yard=validated.has_yard,
            household_has_children=validated.household_has_children,
            has_other_animals=validated.has_other_animals,
            experience_level=validated.experience_level.value,
            activity_level=validated.activity_level.value,
            daily_hours_available=validated.daily_hours_available,
            city=validated.city,
            open_to_proactive_suggestions=validated.open_to_proactive_suggestions,
            is_complete=False,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        session.add(profile)
        session.flush()
        return profile

    @staticmethod
    def _apply(row: AdopterProfile, validated: ValidatedProfile) -> None:
        """Copy validated values onto the stored row."""
        row.home_type = validated.home_type.value
        row.has_yard = validated.has_yard
        row.yard_size_sqm = validated.yard_size_sqm
        row.household_has_children = validated.household_has_children
        row.youngest_child_age = validated.youngest_child_age
        row.has_other_animals = validated.has_other_animals
        row.other_animals_description = validated.other_animals_description
        row.experience_level = validated.experience_level.value
        row.activity_level = validated.activity_level.value
        row.daily_hours_available = validated.daily_hours_available
        row.city = validated.city
        row.preferred_age_range = (
            validated.preferred_age_range.value
            if validated.preferred_age_range
            else None
        )
        row.preferred_size = (
            validated.preferred_size.value if validated.preferred_size else None
        )
        row.preferred_species = ",".join(
            species.value for species in validated.preferred_species
        )
        row.open_to_proactive_suggestions = validated.open_to_proactive_suggestions
