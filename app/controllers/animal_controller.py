"""Animal controller: search, details and the staff management table.

Covers mandatory requirements 4.1 (search), 4.2 (details view) and 4.3
(tabular display) from the course blueprint.
"""

from __future__ import annotations

from flask import Blueprint, abort, render_template, request

from app.controllers.helpers import (
    get_bus,
    parse_checkbox,
    parse_positive_integer,
)
from app.cqrs.queries.animal_queries import (
    DEFAULT_PAGE_SIZE,
    AnimalSearchFilters,
    GetAnimalDetailsQuery,
    ListAllAnimalsQuery,
    SearchAnimalsQuery,
    available_filter_options,
)
from app.security.authorization import require_sign_in, require_staff

animal_blueprint = Blueprint("animals", __name__, url_prefix="/animals")


def _filters_from_request() -> AnimalSearchFilters:
    """Build search filters from the query string.

    Parsing lives here, in the controller, because it is an HTTP concern. The
    resulting filters object is what the query layer understands.
    """
    return AnimalSearchFilters(
        text=request.args.get("q", "").strip() or None,
        species=request.args.get("species") or None,
        size=request.args.get("size") or None,
        activity_level=request.args.get("activity_level") or None,
        city=request.args.get("city") or None,
        good_with_children=parse_checkbox(request.args.get("good_with_children")),
        good_with_other_animals=parse_checkbox(request.args.get("good_with_other_animals")),
        available_only=not parse_checkbox(request.args.get("include_unavailable")),
    )


@animal_blueprint.route("/")
def search() -> str:
    """Search animals with structured filters (blueprint requirement 4.1)."""
    filters = _filters_from_request()
    page = parse_positive_integer(request.args.get("page"), default=1)

    results = get_bus().dispatch_query(
        SearchAnimalsQuery(filters=filters, page=page, page_size=DEFAULT_PAGE_SIZE)
    )

    return render_template(
        "animals/search.html",
        results=results,
        filters=filters,
        options=available_filter_options(),
    )


@animal_blueprint.route("/<animal_id>")
def details(animal_id: str) -> str:
    """Show one animal's full record (blueprint requirement 4.2)."""
    animal = get_bus().dispatch_query(GetAnimalDetailsQuery(animal_id=animal_id))
    if animal is None:
        abort(404)

    return render_template("animals/details.html", animal=animal)


@animal_blueprint.route("/manage")
@require_sign_in
@require_staff()
def manage() -> str:
    """Staff table of every animal (blueprint requirement 4.3).

    Authorization is enforced here on the server. An adopter who guesses this
    URL receives 403 whether or not the navigation offered them a link.
    """
    animals = get_bus().dispatch_query(
        ListAllAnimalsQuery(
            status=request.args.get("status") or None,
            species=request.args.get("species") or None,
        )
    )

    return render_template(
        "animals/manage.html",
        animals=animals,
        options=available_filter_options(),
        selected_status=request.args.get("status", ""),
        selected_species=request.args.get("species", ""),
    )
