"""Controller for an adopter's own area: profile, applications and invitations.

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
from flask_login import current_user
from werkzeug.wrappers import Response

from app.controllers.helpers import ViewResult, get_bus, is_safe_redirect_target
from app.cqrs.commands.application_commands import (
    NotYourRecordError,
    RecordNotFoundError,
    SubmitApplicationCommand,
    WithdrawApplicationCommand,
)
from app.cqrs.commands.invitation_commands import (
    MarkInvitationViewedCommand,
    RespondToInvitationCommand,
)
from app.cqrs.commands.notification_commands import (
    MarkAllNotificationsReadCommand,
    MarkNotificationReadCommand,
)
from app.cqrs.commands.profile_commands import SaveAdopterProfileCommand
from app.cqrs.queries.notification_queries import ListMyNotificationsQuery
from app.cqrs.queries.personal_queries import (
    ListMyApplicationsQuery,
    ListMyInvitationsQuery,
)
from app.cqrs.queries.profile_queries import GetMyProfileQuery
from app.domain.application_rules import (
    ApplicationNotAllowedError,
    IllegalTransitionError,
)
from app.domain.enums import (
    ActivityLevel,
    AnimalSize,
    ExperienceLevel,
    HomeType,
    Species,
)
from app.domain.invitation_rules import InvitationExpiredError, InvitationNotAllowedError
from app.domain.matching import AgePreference
from app.domain.profile_rules import ProfileSubmission, validate_profile
from app.security.authorization import require_adopter, require_sign_in

personal_blueprint = Blueprint("personal", __name__, url_prefix="/my")

# Matches adoption_applications.applicant_message, NVARCHAR(2000). This is
# the one free-text field an adopter controls entirely, so it is also the
# natural place to push an oversized payload.
MAXIMUM_APPLICANT_MESSAGE_LENGTH = 2000


def _require_profile() -> Response | None:
    """Redirect adopters who have not created a profile yet."""
    if current_user.adopter_profile_id:
        return None
    flash("Create your adoption profile first.", "info")
    return redirect(url_for("personal.my_profile"))


@personal_blueprint.route("/applications")
@require_sign_in
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
@require_sign_in
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
@require_sign_in
@require_adopter()
def respond_to_invitation(invitation_id: str) -> Response:
    """Accept or decline an invitation (spec section 7.4).

    The answer must be one of the two words the form offers. A missing or
    unrecognised value is refused rather than read as a decline: silently
    turning a garbled request into "no" would record an answer the adopter
    never gave.
    """
    answer = (request.form.get("response") or "").strip().upper()
    if answer not in ("ACCEPT", "DECLINE"):
        abort(400)
    accepted = answer == "ACCEPT"

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
@require_sign_in
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


def _profile_form_options() -> dict[str, list[str]]:
    """The values the profile form's selects offer."""
    return {
        "home_type": [member.value for member in HomeType],
        "experience_level": [member.value for member in ExperienceLevel],
        "activity_level": [member.value for member in ActivityLevel],
        "species": [member.value for member in Species],
        "preferred_age_range": [member.value for member in AgePreference],
        "preferred_size": [member.value for member in AnimalSize],
    }


def _submission_from_request() -> ProfileSubmission:
    """Read the profile form into an untrusted submission object.

    Parsing lives here because it is an HTTP concern; validating lives in the
    domain, so the same rules apply to any future caller (rule R2).
    """
    return ProfileSubmission(
        home_type=request.form.get("home_type"),
        has_yard=request.form.get("has_yard") is not None,
        yard_size_sqm=request.form.get("yard_size_sqm"),
        household_has_children=request.form.get("household_has_children") is not None,
        youngest_child_age=request.form.get("youngest_child_age"),
        has_other_animals=request.form.get("has_other_animals") is not None,
        other_animals_description=request.form.get("other_animals_description"),
        experience_level=request.form.get("experience_level"),
        activity_level=request.form.get("activity_level"),
        daily_hours_available=request.form.get("daily_hours_available"),
        city=request.form.get("city"),
        preferred_species=tuple(request.form.getlist("preferred_species")),
        preferred_age_range=request.form.get("preferred_age_range"),
        preferred_size=request.form.get("preferred_size"),
        open_to_proactive_suggestions=(
            request.form.get("open_to_proactive_suggestions") is not None
        ),
    )



@personal_blueprint.route("/notifications")
@require_sign_in
def my_notifications() -> str:
    """Show the signed-in user's inbox (spec section 23).

    Open to staff as well as adopters: both receive messages, and an
    inbox one of them cannot read is worse than no inbox.
    """
    notifications = get_bus().dispatch_query(
        ListMyNotificationsQuery(user_id=current_user.user_id)
    )
    return render_template("personal/notifications.html", notifications=notifications)


@personal_blueprint.route("/notifications/<notification_id>/read", methods=["POST"])
@require_sign_in
def mark_notification_read(notification_id: str) -> Response:
    """Mark one message read and follow it to wherever it points."""
    try:
        get_bus().dispatch_command(
            MarkNotificationReadCommand(
                notification_id=notification_id, user_id=current_user.user_id
            )
        )
    except RecordNotFoundError:
        abort(404)
    except NotYourRecordError:
        abort(403)

    target = request.form.get("target_url")
    if target and is_safe_redirect_target(target):
        return redirect(target)
    return redirect(url_for("personal.my_notifications"))


@personal_blueprint.route("/notifications/read-all", methods=["POST"])
@require_sign_in
def mark_all_notifications_read() -> Response:
    """Clear the whole inbox in one action."""
    marked = get_bus().dispatch_command(
        MarkAllNotificationsReadCommand(user_id=current_user.user_id)
    )
    if marked:
        flash(
            f"{marked} message{'s' if marked != 1 else ''} marked as read.", "success"
        )
    return redirect(url_for("personal.my_notifications"))


@personal_blueprint.route("/profile", methods=["GET", "POST"])
@require_sign_in
@require_adopter()
def my_profile() -> ViewResult:
    """Create or update the signed-in adopter's profile (spec section 5.1).

    A complete profile is what unlocks personal matching, and the opt-in it
    carries is what makes an adopter visible to proactive discovery.
    """
    bus = get_bus()

    if request.method == "GET":
        profile = bus.dispatch_query(GetMyProfileQuery(user_id=current_user.user_id))
        return render_template(
            "personal/profile.html",
            profile=profile,
            options=_profile_form_options(),
            errors={},
        )

    submission = _submission_from_request()
    result = validate_profile(submission)

    if not result.is_valid:
        # Server-side validation is the real check. The browser's is a
        # convenience, and a forged post bypasses it entirely (NFR-5.2).
        return render_template(
            "personal/profile.html",
            profile=None,
            submission=submission,
            options=_profile_form_options(),
            errors=result.errors,
        ), 400

    assert result.profile is not None
    bus.dispatch_command(
        SaveAdopterProfileCommand(user_id=current_user.user_id, profile=result.profile)
    )

    flash("Profile saved. We can now match animals to your situation.", "success")
    return redirect(url_for("matches.find_my_pet"))


@personal_blueprint.route("/apply/<animal_id>", methods=["POST"])
@require_sign_in
@require_adopter()
def apply_to_animal(animal_id: str) -> Response:
    """Submit an adoption application for one animal (feature F-07).

    Queues an analysis job as a side effect but does not wait for it: the
    adopter is redirected immediately, and the explanation appears once the
    agent has written it (NFR-3.1).
    """
    if not current_user.adopter_profile_id:
        flash("Complete your adoption profile before applying.", "info")
        return redirect(url_for("personal.my_profile"))

    applicant_message = (request.form.get("applicant_message") or "").strip()
    if len(applicant_message) > MAXIMUM_APPLICANT_MESSAGE_LENGTH:
        flash(
            "Please keep your message under "
            f"{MAXIMUM_APPLICANT_MESSAGE_LENGTH} characters.",
            "error",
        )
        return redirect(url_for("animals.details", animal_id=animal_id))

    try:
        get_bus().dispatch_command(
            SubmitApplicationCommand(
                adopter_profile_id=current_user.adopter_profile_id,
                animal_id=animal_id,
                actor_user_id=current_user.user_id,
                applicant_message=applicant_message or None,
            )
        )
    except ApplicationNotAllowedError as error:
        flash(str(error), "error")
        return redirect(url_for("animals.details", animal_id=animal_id))
    except RecordNotFoundError:
        abort(404)

    flash("Application submitted. A staff member will review it.", "success")
    return redirect(url_for("personal.my_applications"))
