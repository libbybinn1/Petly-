"""PetMatch MCP tool server, speaking MCP over stdio.

Course blueprint section 8 requires at least two local tools communicating
over stdio, each carrying a description that tells a language model what it
can do. This module provides the two the product specification names
(spec section 14): `get_adopter_profile` and `get_animal_profile`.

The architectural point, from spec section 14: the agent does not own the
application's data. It requests records through tools, exactly as it would
for any external service. That boundary is what keeps the agent a genuinely
separate component rather than a background thread with extra steps.

Two rules hold throughout:

- **Read-only.** Neither tool writes. State changes belong to commands in the
  Flask application, where the business rules and the event store live.
- **Never print to stdout.** stdout carries the JSON-RPC protocol. A stray
  print corrupts the stream and the client hangs rather than erroring, which
  is painful to diagnose. Diagnostics go to stderr.
"""

from __future__ import annotations

import sys
from typing import Any

from app.config import load_configuration
from app.infrastructure.database import create_database_engine, create_session_factory
from app.infrastructure.models import AdopterProfile, Animal
from mcp.server.mcpserver import MCPServer
from sqlalchemy import select
from sqlalchemy.orm import Session

SERVER_NAME = "petmatch_mcp"

mcp_server = MCPServer(SERVER_NAME)

# Built once at import. Each tool call borrows a short-lived session from it
# rather than opening a new engine per request.
_configuration = load_configuration()
_session_factory = create_session_factory(create_database_engine(_configuration))


def _log(message: str) -> None:
    """Write a diagnostic line to stderr.

    stdout is reserved for the MCP protocol, so nothing may be written there.
    """
    print(message, file=sys.stderr)  # noqa: T201 - stderr is the diagnostic channel


def _not_found(record_kind: str, identifier: str) -> dict[str, Any]:
    """Build a structured not-found result.

    Returned instead of raising, because an exception crossing the MCP
    boundary reaches the model as an opaque protocol error it cannot reason
    about. A structured answer is something the agent can act on and report
    under `missing_information`.
    """
    return {
        "found": False,
        "reason": f"No {record_kind} exists with identifier {identifier}.",
    }


def _open_session() -> Session:
    """Open a read-only session for one tool call."""
    return _session_factory()


@mcp_server.tool()
def get_adopter_profile(adopter_profile_id: str) -> dict[str, Any]:
    """Retrieve an adopter's profile for compatibility matching.

    Use this when you need to know about the person: their home, household,
    experience and daily availability. These are the stable facts that
    determine whether an animal would suit them.

    Do not use this to search for adopters; it fetches exactly one record by
    identifier.

    Args:
        adopter_profile_id: The adopter profile's UUID.

    Returns:
        A dictionary with `found: true` and the adopter's matching-relevant
        fields: home type, whether they have a yard, whether children live in
        the household and the youngest child's age, whether other animals are
        present, experience level, activity level, daily hours available,
        city, preferred species, whether they accept proactive suggestions,
        and whether the profile is complete.

        If no such adopter exists, returns `found: false` with a reason.
    """
    with _open_session() as session:
        profile = session.execute(
            select(AdopterProfile).where(
                AdopterProfile.adopter_profile_id == adopter_profile_id
            )
        ).scalar_one_or_none()

        if profile is None:
            _log(f"get_adopter_profile: no record for {adopter_profile_id}")
            return _not_found("adopter profile", adopter_profile_id)

        return {
            "found": True,
            "adopter_profile_id": profile.adopter_profile_id,
            "home_type": profile.home_type,
            "has_yard": bool(profile.has_yard),
            "yard_size_sqm": profile.yard_size_sqm,
            "household_has_children": bool(profile.household_has_children),
            "youngest_child_age": profile.youngest_child_age,
            "has_other_animals": bool(profile.has_other_animals),
            "other_animals_description": profile.other_animals_description,
            "experience_level": profile.experience_level,
            "activity_level": profile.activity_level,
            "daily_hours_available": float(profile.daily_hours_available),
            "city": profile.city,
            "preferred_species": _split_preferences(profile.preferred_species),
            "preferred_size": profile.preferred_size,
            "preferred_age_range": profile.preferred_age_range,
            "open_to_proactive_suggestions": bool(profile.open_to_proactive_suggestions),
            "is_complete": bool(profile.is_complete),
        }


@mcp_server.tool()
def get_animal_profile(animal_id: str) -> dict[str, Any]:
    """Retrieve an animal's profile for compatibility matching.

    Use this when you need to know about the animal: its species, size,
    temperament, energy level, compatibility with children and other animals,
    and any special care requirements.

    Do not use this to search for animals; it fetches exactly one record by
    identifier.

    Args:
        animal_id: The animal's UUID.

    Returns:
        A dictionary with `found: true` and the animal's matching-relevant
        fields: name, species, breed, age in years, size, temperament,
        activity level, whether it is good with children, whether it is good
        with other animals, whether it has special needs and their
        description, the space it requires, its city and its adoption status.

        If no such animal exists, returns `found: false` with a reason.
    """
    with _open_session() as session:
        animal = session.execute(
            select(Animal).where(Animal.animal_id == animal_id)
        ).scalar_one_or_none()

        if animal is None:
            _log(f"get_animal_profile: no record for {animal_id}")
            return _not_found("animal", animal_id)

        return {
            "found": True,
            "animal_id": animal.animal_id,
            "name": animal.name,
            "species": animal.species,
            "breed": animal.breed,
            "age_years": float(animal.age_years),
            "size": animal.size,
            "temperament": animal.temperament,
            "activity_level": animal.activity_level,
            "good_with_children": bool(animal.good_with_children),
            "good_with_other_animals": bool(animal.good_with_other_animals),
            "has_special_needs": bool(animal.has_special_needs),
            "special_needs_description": animal.special_needs_description,
            "required_space": animal.required_space,
            "city": animal.city,
            "status": animal.status,
            "description": animal.description,
        }


def _split_preferences(raw_value: str | None) -> list[str]:
    """Split the stored comma-separated species preferences into a list.

    Stored as a delimited string because SQL Server 2014 has no array type.
    """
    if not raw_value:
        return []
    return [item.strip() for item in raw_value.split(",") if item.strip()]


def main() -> None:
    """Run the tool server over stdio."""
    _log(f"{SERVER_NAME}: starting on stdio")
    mcp_server.run(transport="stdio")


if __name__ == "__main__":
    main()
