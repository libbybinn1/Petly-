"""Controller for the AI-facing screens.

Covers the two matching workflows in spec section 9: adopters discovering
animals, and staff discovering adopters. Authorization is enforced on the
server for every staff route (blueprint section 12).
"""

from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from werkzeug.wrappers import Response

from app.controllers.helpers import get_bus, get_configuration
from app.cqrs.commands.application_commands import RecordNotFoundError
from app.cqrs.commands.invitation_commands import SendInvitationCommand
from app.cqrs.queries.animal_queries import (
    DEFAULT_PAGE_SIZE,
    AnimalSearchFilters,
    SearchAnimalsQuery,
)
from app.cqrs.queries.match_queries import (
    FindMoreAdoptersQuery,
    FindMyPetQuery,
    GetMatchAnalysisQuery,
    RankApplicantsQuery,
)
from app.domain.invitation_rules import InvitationNotAllowedError
from app.security.authorization import require_adopter, require_staff

match_blueprint = Blueprint("matches", __name__)


@match_blueprint.route("/my/matches")
@login_required
@require_adopter()
def find_my_pet() -> str | Response:
    """Rank available animals for the signed-in adopter (spec section 6.1).

    Ranking is computed synchronously and appears immediately. Explanations
    are written by the agent moments later, so some cards may show a pending
    state on first load.
    """
    if not current_user.adopter_profile_id:
        flash("Complete your adoption profile to see personal matches.", "info")
        return redirect(url_for("personal.my_profile"))

    matches = get_bus().dispatch_query(
        FindMyPetQuery(adopter_profile_id=current_user.adopter_profile_id)
    )

    return render_template(
        "matches/find_my_pet.html",
        matches=matches,
        has_complete_profile=current_user.has_complete_profile,
    )


@match_blueprint.route("/animals/<animal_id>/adopters")
@login_required
@require_staff()
def find_my_adopter(animal_id: str) -> str:
    """Rank the adopters who already applied for this animal (spec section 7.2)."""
    ranking = get_bus().dispatch_query(RankApplicantsQuery(animal_id=animal_id))
    if ranking is None:
        abort(404)

    return render_template(
        "matches/find_my_adopter.html",
        ranking=ranking,
        mode="applicants",
    )


@match_blueprint.route("/animals/<animal_id>/discover")
@login_required
@require_staff()
def find_more_adopters(animal_id: str) -> str:
    """Discover eligible adopters who did not apply (spec section 7.3).

    Deliberately distinct from ranking applicants: this searches the opted-in
    population, and only candidates passing every eligibility rule in spec
    section 10 are considered.
    """
    ranking = get_bus().dispatch_query(FindMoreAdoptersQuery(animal_id=animal_id))
    if ranking is None:
        abort(404)

    return render_template(
        "matches/find_my_adopter.html",
        ranking=ranking,
        mode="discovery",
    )


@match_blueprint.route("/analyses/<match_analysis_id>")
@login_required
@require_staff()
def analysis_detail(match_analysis_id: str) -> str:
    """Show one stored analysis in full, with its evidence."""
    analysis = get_bus().dispatch_query(
        GetMatchAnalysisQuery(match_analysis_id=match_analysis_id)
    )
    if analysis is None:
        abort(404)

    return render_template("matches/analysis.html", analysis=analysis)


@match_blueprint.route("/animals/<animal_id>/invite", methods=["POST"])
@login_required
@require_staff()
def send_invitation(animal_id: str) -> Response:
    """Invite one discovered adopter to consider this animal (spec section 7.4).

    The invitation opens a 72-hour window. Eligibility is re-checked in the
    command rather than trusted from the page that offered the button, since
    the roster can change between rendering and clicking.
    """
    adopter_profile_id = request.form.get("adopter_profile_id", "")
    if not adopter_profile_id:
        abort(400)

    try:
        get_bus().dispatch_command(
            SendInvitationCommand(
                animal_id=animal_id,
                adopter_profile_id=adopter_profile_id,
                staff_user_id=current_user.user_id,
                staff_message=request.form.get("staff_message") or None,
            )
        )
    except InvitationNotAllowedError as error:
        flash(str(error), "error")
        return redirect(url_for("matches.find_more_adopters", animal_id=animal_id))
    except RecordNotFoundError:
        abort(404)

    flash("Invitation sent. The adopter has 72 hours to respond.", "success")
    return redirect(url_for("matches.find_more_adopters", animal_id=animal_id))


def _build_interpreter():  # noqa: ANN202 - IntentInterpreter, imported lazily
    """Construct the intent interpreter from configuration.

    Imported inside the function so the web tier does not load the agent's
    modules at start-up. The two run as separate processes (rule R2); this
    is the one narrow place the app borrows the interpretation helper.
    """
    from agent_service.intent import IntentInterpreter
    from agent_service.llm_client import OllamaLanguageModel

    settings = get_configuration()
    return IntentInterpreter(
        OllamaLanguageModel(
            base_url=settings.agent.ollama_base_url,
            name=settings.agent.chat_model,
        )
    )


@match_blueprint.route("/search/describe", methods=["GET", "POST"])
def natural_language_search() -> str:
    """Search by describing what you are looking for (spec section 6.3).

    The model converts the description into criteria; the ordinary
    deterministic search then runs against them. Nothing the model produces
    selects an animal or scores anything.

    Open to anyone, including signed-out visitors: describing what you want
    is a browsing feature, not a personal one.
    """
    described = (request.form.get("description") or request.args.get("q") or "").strip()

    if not described:
        return render_template("matches/describe.html", intent=None, results=None, described="")

    intent = _build_interpreter().interpret(described)

    if not intent.understood or not intent.has_any_criteria:
        return render_template(
            "matches/describe.html", intent=intent, results=None, described=described
        )

    results = get_bus().dispatch_query(
        SearchAnimalsQuery(
            filters=AnimalSearchFilters(
                species=intent.species[0].value if len(intent.species) == 1 else None,
                size=intent.size.value if intent.size else None,
                activity_level=(
                    intent.activity_level.value if intent.activity_level else None
                ),
                good_with_children=bool(intent.good_with_children),
                good_with_other_animals=bool(intent.good_with_other_animals),
                available_only=True,
            ),
            page=1,
            page_size=DEFAULT_PAGE_SIZE,
        )
    )

    return render_template(
        "matches/describe.html", intent=intent, results=results, described=described
    )
