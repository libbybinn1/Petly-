"""Home controller: the landing page.

Routes signed-in users toward the part of the system that belongs to their
role, and shows newcomers what PetMatch does.
"""

from __future__ import annotations

from flask import Blueprint, render_template
from flask_login import current_user

from app.controllers.helpers import get_bus
from app.cqrs.queries.animal_queries import AnimalSearchFilters, SearchAnimalsQuery

home_blueprint = Blueprint("home", __name__)

FEATURED_ANIMAL_COUNT = 6


@home_blueprint.route("/")
def index() -> str:
    """Show the landing page with a few available animals."""
    results = get_bus().dispatch_query(
        SearchAnimalsQuery(
            filters=AnimalSearchFilters(available_only=True),
            page=1,
            page_size=FEATURED_ANIMAL_COUNT,
        )
    )

    return render_template(
        "home.html",
        featured_animals=results.animals,
        available_count=results.total_count,
        is_signed_in=current_user.is_authenticated,
    )
