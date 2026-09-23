"""Controller for an adopter's own area: applications and invitations.

Ownership is enforced two different ways here, because the routes differ in
shape (FR-2.4):

- **Listing routes** take no identifier at all. They query on
  `current_user.adopter_profile_id`, so there is nothing for an attacker to
  change - fetching somebody else's list is not expressible.
- **Action routes** must take a record identifier from the URL. Those pass
  the signed-in adopter's profile identifier into the command, and the
  *command handler* verifies the record belongs to them before acting. The
  check lives with the rule rather than in the route, so a second caller
  cannot bypass it by forgetting to repeat it.
"""

from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from werkzeug.wrappers import Response

from app.controllers.helpers import get_bus
from app.cqrs.commands.application_commands import (
    NotYourRecordError,
    RecordNotFoundError,
    WithdrawApplicationCommand,
)
from app.cqrs.commands.invitation_commands import (
    MarkInvitationViewedCommand,
    RespondToInvitationCommand,
)
from app.cqrs.queries.personal_queries import (
    ListMyApplicationsQuery,
    ListMyInvitationsQuery,
)
from app.domain.application_rules import IllegalTransitionError
from app.domain.invitation_rules import InvitationExpiredError, InvitationNotAllowedError
from app.security.authorization import require_adopter

personal_blueprint = Blueprint("personal", __name__, url_prefix="/my")


def _require_profile() -> Response | None:
    """Redirect adopters who have not created a profile yet."""
    if current_user.adopter_profile_id:
        return None
    flash("Create your adoption profile first.", "info")
    return redirect(url_for("animals.search"))


@personal_blueprint.route("/applications")
@login_required
@require_adopter()
def my_applications() -> str | Response:
    """List the signed-in adopter's applications."""
    if (redirection := _require_profile()) is not None:
        return redirection

    applications = get_bus().dispatch_query(
        ListMyApplicationsQuery(adopter_profile_id=current_user.adopter_profile_id)
    )
    return render_template("personal/applications.html", applications=applications)


@personal_blueprint.route("/invitations")
@login_required
@require_adopter()
def my_invitations() -> str | Response:
    """List the signed-in adopter's invitations.

    Opening this page marks unseen invitations as viewed, which is a state
    change, so it dispatches commands before querying. The command is
    idempotent, so a refresh does not append duplicate events.
    """
    if (redirection := _require_profile()) is not None:
        return redirection

    bus = get_bus()
    invitations = bus.dispatch_query(
        ListMyInvitationsQuery(adopter_profile_id=current_user.adopter_profile_id)
    )

    for invitation in invitations:
        if invitation.status == "SENT":
            bus.dispatch_command(
                MarkInvitationViewedCommand(
                    invitation_id=invitation.invitation_id,
                    actor_user_id=current_user.user_id,
                    adopter_profile_id=current_user.adopter_profile_id,
                )
            )

    # Re-query so the page reflects the statuses just written. A command
    # returns no read data, so this second dispatch is the CQRS-correct way
    # to get it (blueprint 9.2).
    invitations = bus.dispatch_query(
        ListMyInvitationsQuery(adopter_profile_id=current_user.adopter_profile_id)
    )

    return render_template("personal/invitations.html", invitations=invitations)


@personal_blueprint.route("/invitations/<invitation_id>/respond", methods=["POST"])
@login_required
@require_adopter()
def respond_to_invitation(invitation_id: str) -> Response:
    """Accept or decline an invitation."""
    accepted = request.form.get("response") == "ACCEPT"

    try:
        get_bus().dispatch_command(
            RespondToInvitationCommand(
                invitation_id=invitation_id,
                actor_user_id=current_user.user_id,
                adopter_profile_id=current_user.adopter_profile_id,
                accepted=accepted,
            )
        )
    except NotYourRecordError:
        # Do not confirm the record exists; an adopter probing identifiers
        # learns nothing from a 403 either way.
        abort(403)
    except (InvitationExpiredError, InvitationNotAllowedError, RecordNotFoundError) as error:
        flash(str(error), "error")
        return redirect(url_for("personal.my_invitations"))

    if accepted:
        flash(
            "Invitation accepted. An application has been created and a staff "
            "member will review it.",
            "success",
        )
        return redirect(url_for("personal.my_applications"))

    flash("Invitation declined.", "info")
    return redirect(url_for("personal.my_invitations"))


@personal_blueprint.route("/applications/<application_id>/withdraw", methods=["POST"])
@login_required
@require_adopter()
def withdraw_application(application_id: str) -> Response:
    """Withdraw one of the signed-in adopter's applications."""
    try:
        get_bus().dispatch_command(
            WithdrawApplicationCommand(
                application_id=application_id,
                actor_user_id=current_user.user_id,
                adopter_profile_id=current_user.adopter_profile_id,
            )
        )
    except NotYourRecordError:
        abort(403)
    except (IllegalTransitionError, RecordNotFoundError) as error:
        flash(str(error), "error")
        return redirect(url_for("personal.my_applications"))

    flash("Application withdrawn.", "info")
    return redirect(url_for("personal.my_applications"))
