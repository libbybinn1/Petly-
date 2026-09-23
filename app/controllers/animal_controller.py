"""Animal controller: search, details and the staff management table.

Covers mandatory requirements 4.1 (search), 4.2 (details view) and 4.3
(tabular display) from the course blueprint.
"""

from __future__ import annotations

from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user
from werkzeug.wrappers import Response

from app.controllers.helpers import (
    ViewResult,
    get_bus,
    parse_checkbox,
    parse_positive_integer,
)
from app.cqrs.commands.animal_commands import (
    ChangeAnimalStatusCommand,
    CreateAnimalCommand,
    UpdateAnimalCommand,
)
from app.cqrs.commands.application_commands import RecordNotFoundError
from app.cqrs.queries.animal_queries import (
    DEFAULT_PAGE_SIZE,
    STAFF_PAGE_SIZE,
    AnimalSearchFilters,
    GetAnimalDetailsQuery,
    ListAllAnimalsQuery,
    SearchAnimalsQuery,
    available_filter_options,
    species_filter_label,
)
from app.domain.animal_rules import (
    AnimalSubmission,
    NoImageError,
    validate_animal,
)
from app.domain.enums import (
    ActivityLevel,
    AnimalSize,
    AnimalStatus,
    Species,
    Temperament,
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



def _animal_submission_from_request() -> AnimalSubmission:
    """Read an animal form into the shape the domain rules validate.

    Parsing lives in the controller because it is an HTTP concern; deciding
    whether the values are acceptable lives in `animal_rules` (rule R2).
    """
    return AnimalSubmission(
        name=request.form.get("name"),
        species=request.form.get("species"),
        breed=request.form.get("breed"),
        age_years=request.form.get("age_years"),
        size=request.form.get("size"),
        temperament=request.form.get("temperament"),
        activity_level=request.form.get("activity_level"),
        required_space=request.form.get("required_space"),
        city=request.form.get("city"),
        status=request.form.get("status"),
        description=request.form.get("description"),
        good_with_children=parse_checkbox(request.form.get("good_with_children")),
        good_with_other_animals=parse_checkbox(
            request.form.get("good_with_other_animals")
        ),
        has_special_needs=parse_checkbox(request.form.get("has_special_needs")),
        special_needs_description=request.form.get("special_needs_description"),
        image_urls=tuple(request.form.getlist("image_urls")),
    )


def _animal_form_options() -> dict[str, list[tuple[str, str]]]:
    """The choices each dropdown offers, labelled for display."""
    return {
        "species": [(member.value, member.value.replace("_", " ").capitalize())
                    for member in Species],
        "size": [(member.value, member.value.capitalize()) for member in AnimalSize],
        "temperament": [(member.value, member.value.capitalize())
                        for member in Temperament],
        "activity_level": [(member.value, member.value.capitalize())
                           for member in ActivityLevel],
        "status": [(member.value, member.value.replace("_", " ").capitalize())
                   for member in AnimalStatus],
    }


@animal_blueprint.route("/new", methods=["GET", "POST"])
@require_sign_in
@require_staff()
def new_animal() -> ViewResult:
    """List a new animal (FR-4.1).

    Until this existed the seed script was the only way an animal entered
    the system, which left three MUST requirements unreachable through the
    application itself.
    """
    if request.method == "GET":
        return render_template(
            "animals/form.html",
            animal=None,
            submission=None,
            options=_animal_form_options(),
            errors={},
        )

    submission = _animal_submission_from_request()
    result = validate_animal(submission)

    if not result.is_valid:
        return render_template(
            "animals/form.html",
            animal=None,
            submission=submission,
            options=_animal_form_options(),
            errors=result.errors,
        ), 400

    assert result.animal is not None
    animal_id = get_bus().dispatch_command(
        CreateAnimalCommand(animal=result.animal, staff_user_id=current_user.user_id)
    )

    flash(f"{result.animal.name} has been listed.", "success")
    return redirect(url_for("animals.details", animal_id=animal_id))


@animal_blueprint.route("/<animal_id>/edit", methods=["GET", "POST"])
@require_sign_in
@require_staff()
def edit_animal(animal_id: str) -> ViewResult:
    """Correct or enrich an existing animal's record (FR-4.1)."""
    bus = get_bus()

    if request.method == "GET":
        animal = bus.dispatch_query(GetAnimalDetailsQuery(animal_id=animal_id))
        if animal is None:
            abort(404)
        return render_template(
            "animals/form.html",
            animal=animal,
            submission=None,
            options=_animal_form_options(),
            errors={},
        )

    submission = _animal_submission_from_request()
    result = validate_animal(submission)

    if not result.is_valid:
        existing = bus.dispatch_query(GetAnimalDetailsQuery(animal_id=animal_id))
        return render_template(
            "animals/form.html",
            animal=existing,
            submission=submission,
            options=_animal_form_options(),
            errors=result.errors,
        ), 400

    assert result.animal is not None
    try:
        bus.dispatch_command(
            UpdateAnimalCommand(
                animal_id=animal_id,
                animal=result.animal,
                staff_user_id=current_user.user_id,
            )
        )
    except RecordNotFoundError:
        abort(404)
    except NoImageError as error:
        flash(str(error), "error")
        return redirect(url_for("animals.edit_animal", animal_id=animal_id))

    flash(f"{result.animal.name}'s record has been updated.", "success")
    return redirect(url_for("animals.details", animal_id=animal_id))


@animal_blueprint.route("/<animal_id>/status", methods=["POST"])
@require_sign_in
@require_staff()
def change_animal_status(animal_id: str) -> Response:
    """Change one animal's availability (FR-4.4)."""
    raw_status = (request.form.get("status") or "").strip().upper()
    try:
        new_status = AnimalStatus(raw_status)
    except ValueError:
        abort(400)

    try:
        get_bus().dispatch_command(
            ChangeAnimalStatusCommand(
                animal_id=animal_id,
                new_status=new_status,
                staff_user_id=current_user.user_id,
            )
        )
    except RecordNotFoundError:
        abort(404)

    flash(
        f"Status changed to {new_status.value.replace('_', ' ').lower()}.", "success"
    )
    return redirect(url_for("animals.details", animal_id=animal_id))


@animal_blueprint.route("/manage")
@require_sign_in
@require_staff()
def manage() -> str:
    """Staff table of every animal (blueprint requirement 4.3).

    Authorization is enforced here on the server. An adopter who guesses this
    URL receives 403 whether or not the navigation offered them a link.
    """
    results = get_bus().dispatch_query(
        ListAllAnimalsQuery(
            status=request.args.get("status") or None,
            species=request.args.get("species") or None,
            text=request.args.get("q", "").strip() or None,
            page=parse_positive_integer(request.args.get("page"), 1),
            page_size=STAFF_PAGE_SIZE,
        )
    )

    return render_template(
        "animals/manage.html",
        results=results,
        animals=results.animals,
        options=available_filter_options(),
        species_label=species_filter_label,
        selected_status=request.args.get("status", ""),
        selected_species=request.args.get("species", ""),
        search_text=request.args.get("q", ""),
    )
