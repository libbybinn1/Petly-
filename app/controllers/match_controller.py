"""Controller for the AI-facing screens.

Covers the two matching workflows in spec section 9: adopters discovering
animals, and staff discovering adopters. Authorization is enforced on the
server for every staff route (blueprint section 12).
"""

from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from werkzeug.wrappers import Response

from app.controllers.helpers import get_bus
from app.cqrs.commands.application_commands import RecordNotFoundError
from app.cqrs.commands.invitation_commands import SendInvitationCommand
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
        return redirect(url_for("animals.search"))

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
