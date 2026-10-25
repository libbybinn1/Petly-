"""Who may be found by semantic adopter search, read from the database.

The vector index must cover exactly the people Find More Adopters may
suggest (spec section 7.3, section 10): a complete profile, opted in to
proactive suggestions, on an active account. The selection mirrors
`FindMoreAdoptersHandler._eligible_profiles` in the web tier. It is restated
here rather than imported because the agent may not import a query handler
(rule R2); the rule itself is three column filters, and
`tests/agent/test_semantic_adopter_search.py` pins both selections to the
same population so they cannot drift apart unnoticed.

Reads only. The agent reaches these rows through `app.infrastructure.models`,
which R2 allows, and never through a command or a query.
"""

from __future__ import annotations

from app.domain.facts import fields_of
from app.domain.semantic_profiles import (
    ADOPTER_PROFILE_TEXT_FIELDS,
    ANIMAL_QUERY_TEXT_FIELDS,
    build_adopter_profile_text,
    build_animal_query_text,
)
from app.infrastructure.models import AdopterProfile, AdoptionApplication, Animal, User
from app.infrastructure.sql_helpers import is_true
from sqlalchemy import select
from sqlalchemy.orm import Session

from agent_service.rag.profile_index import ProfileDocument


def discoverable_profile_documents(session: Session) -> list[ProfileDocument]:
    """Build the résumé of every adopter who may be proactively suggested.

    Args:
        session: A session onto the application database.

    Returns:
        One document per discoverable profile, ordered by identifier so a
        sync is deterministic.
    """
    profiles = (
        session.execute(
            select(AdopterProfile)
            .join(User, User.user_id == AdopterProfile.user_id)
            .where(is_true(AdopterProfile.is_complete))
            .where(is_true(AdopterProfile.open_to_proactive_suggestions))
            .where(is_true(User.is_active))
            .order_by(AdopterProfile.adopter_profile_id)
        )
        .scalars()
        .all()
    )
    return [
        ProfileDocument(
            adopter_profile_id=profile.adopter_profile_id,
            text=build_adopter_profile_text(fields_of(profile, ADOPTER_PROFILE_TEXT_FIELDS)),
        )
        for profile in profiles
    ]


def applicant_profile_ids(session: Session, animal_id: str) -> frozenset[str]:
    """Return the adopters who already applied for an animal.

    Discovery is for people who did *not* apply (spec section 7.3), so the
    semantic search leaves applicants out exactly as the deterministic one
    does.

    Args:
        session: A session onto the application database.
        animal_id: The animal being searched for.

    Returns:
        Every profile with an application for this animal, in any status.
    """
    rows = session.execute(
        select(AdoptionApplication.adopter_profile_id).where(
            AdoptionApplication.animal_id == animal_id
        )
    ).scalars()
    return frozenset(rows)


def animal_query_text(session: Session, animal_id: str) -> str | None:
    """Build the text an animal is searched by, or None when it is missing.

    Args:
        session: A session onto the application database.
        animal_id: The animal to describe.

    Returns:
        The query text, or None when no such animal exists.
    """
    animal = session.get(Animal, animal_id)
    if animal is None:
        return None
    return build_animal_query_text(fields_of(animal, ANIMAL_QUERY_TEXT_FIELDS))
