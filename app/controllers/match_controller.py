"""Controller for the AI-facing screens.

Covers the two matching workflows in spec section 9: adopters discovering
animals, and staff discovering adopters. Authorization is enforced on the
server for every staff route (blueprint section 12).
"""

from __future__ import annotations

from flask import (
    Blueprint,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user
from werkzeug.wrappers import Response

from app.controllers.helpers import ViewResult, get_bus, parse_checkbox
from app.cqrs.commands.analysis_commands import (
    MAXIMUM_DESCRIPTION_LENGTH,
    EnqueueIntentInterpretationCommand,
)
from app.cqrs.commands.application_commands import (
    ApproveApplicationCommand,
    MarkApplicationUnderReviewCommand,
    RecordNotFoundError,
    RejectApplicationCommand,
    ReverseApprovalCommand,
)
from app.cqrs.commands.invitation_commands import SendInvitationCommand
from app.cqrs.queries.analysis_status_queries import GetAnalysisStatusQuery
from app.cqrs.queries.animal_queries import (
    DEFAULT_PAGE_SIZE,
    AnimalCard,
    AnimalSearchResults,
    SearchAnimalsQuery,
)
from app.cqrs.queries.intent_queries import (
    GetIntentJobQuery,
    IntentJobNotYoursError,
    InterpretedIntentView,
    filters_from_intent,
)
from app.cqrs.queries.match_queries import (
    FindMoreAdoptersQuery,
    FindMyPetQuery,
    FindMyPetWithIntentQuery,
    GetMatchAnalysisQuery,
    RankApplicantsQuery,
)
from app.domain.application_rules import (
    ApplicationNotAllowedError,
    IllegalTransitionError,
)
from app.domain.invitation_rules import InvitationNotAllowedError
from app.domain.search_intent import SearchIntent
from app.security.authorization import require_adopter, require_sign_in, require_staff

match_blueprint = Blueprint("matches", __name__)


@match_blueprint.route("/my/matches")
@require_sign_in
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

    bus = get_bus()
    matches = bus.dispatch_query(
        FindMyPetQuery(adopter_profile_id=current_user.adopter_profile_id)
    )
    # Rendered into the page so the poller knows what it is looking at.
    # Without a baseline it compared the first answer against nothing, so a
    # page whose work had already finished polled every thirty seconds for
    # as long as the tab stayed open (docs/UX.md section 3).
    analysis_status = bus.dispatch_query(
        GetAnalysisStatusQuery(adopter_profile_id=current_user.adopter_profile_id)
    )

    return render_template(
        "matches/find_my_pet.html",
        matches=matches,
        analysis_status=analysis_status,
        has_complete_profile=current_user.has_complete_profile,
    )


@match_blueprint.route("/animals/<animal_id>/adopters")
@require_sign_in
@require_staff()
def find_my_adopter(animal_id: str) -> str:
    """Rank the adopters who already applied for this animal (spec section 7.2)."""
    bus = get_bus()
    ranking = bus.dispatch_query(RankApplicantsQuery(animal_id=animal_id))
    if ranking is None:
        abort(404)

    return render_template(
        "matches/find_my_adopter.html",
        ranking=ranking,
        analysis_status=bus.dispatch_query(GetAnalysisStatusQuery(animal_id=animal_id)),
        mode="applicants",
    )


# The decisions a staff member may record. Field names and the first two
# values come from the contract in docs/API.md section 5; REVIEW and REVERSE
# extend it, because FR-7.4 has four states and the table listed two.
# A membership test rather than a chain of branches, so an unrecognised
# value is refused by lookup instead of falling through to a default.
_DECISIONS = ("APPROVE", "REJECT", "REVIEW", "REVERSE")


@match_blueprint.route("/applications/<application_id>/decide", methods=["POST"])
@require_sign_in
@require_staff()
def decide_application(application_id: str) -> Response:
    """Record a staff decision on one application (FR-7.4, FR-7.5).

    This is the route the whole event-sourced design exists to serve, and
    until now nothing dispatched those commands - the approval cascade
    could only be triggered from a test or a script.

    The decision is a human one. Nothing the agent produces reaches this
    path; the ranking beside the button is advice, and a staff member
    presses it (rule R4).

    Args:
        application_id: The application being decided.

    Returns:
        A redirect back to the ranking screen the decision was made from.
    """
    decision = (request.form.get("decision") or "").strip().upper()
    if decision not in _DECISIONS:
        abort(400)

    animal_id = (request.form.get("animal_id") or "").strip()
    note = (request.form.get("note") or "").strip() or None

    try:
        message = _dispatch_decision(application_id, decision, note)
    except (ApplicationNotAllowedError, IllegalTransitionError) as error:
        flash(str(error), "error")
        return _back_to_ranking(animal_id)
    except RecordNotFoundError:
        abort(404)

    flash(message, "success")
    return _back_to_ranking(animal_id)


def _dispatch_decision(
    application_id: str, decision: str, note: str | None
) -> str:
    """Send the command one decision corresponds to.

    Args:
        application_id: The application being decided.
        decision: One of `_DECISIONS`, already validated.
        note: Optional free text recorded with a rejection or reversal.

    Returns:
        The message to show the staff member.
    """
    bus = get_bus()
    staff_user_id = current_user.user_id

    if decision == "APPROVE":
        closed = bus.dispatch_command(
            ApproveApplicationCommand(
                application_id=application_id, staff_user_id=staff_user_id
            )
        )
        if closed:
            return (
                f"Approved. {closed} other active application"
                f"{'s' if closed != 1 else ''} from this adopter "
                f"{'were' if closed != 1 else 'was'} closed."
            )
        return "Approved. The adopter has been notified."

    if decision == "REJECT":
        bus.dispatch_command(
            RejectApplicationCommand(
                application_id=application_id,
                staff_user_id=staff_user_id,
                reason=note,
            )
        )
        return "Application rejected. The adopter has been notified."

    if decision == "REVIEW":
        bus.dispatch_command(
            MarkApplicationUnderReviewCommand(
                application_id=application_id, staff_user_id=staff_user_id
            )
        )
        return "Marked as under review."

    reopened = bus.dispatch_command(
        ReverseApprovalCommand(
            application_id=application_id,
            staff_user_id=staff_user_id,
            reason=note or "Reversed by staff.",
        )
    )
    if reopened:
        return (
            f"Approval reversed. {reopened} application"
            f"{'s' if reopened != 1 else ''} closed by it "
            f"{'were' if reopened != 1 else 'was'} reopened."
        )
    return "Approval reversed. The animal is available again."


def _back_to_ranking(animal_id: str) -> Response:
    """Return to the applicant ranking, or the dashboard if it is unknown."""
    if not animal_id:
        return redirect(url_for("dashboard.dashboard"))
    return redirect(url_for("matches.find_my_adopter", animal_id=animal_id))


@match_blueprint.route("/animals/<animal_id>/discover")
@require_sign_in
@require_staff()
def find_more_adopters(animal_id: str) -> str:
    """Discover eligible adopters who did not apply (spec section 7.3).

    Deliberately distinct from ranking applicants: this searches the opted-in
    population, and only candidates passing every eligibility rule in spec
    section 10 are considered.
    """
    bus = get_bus()
    ranking = bus.dispatch_query(FindMoreAdoptersQuery(animal_id=animal_id))
    if ranking is None:
        abort(404)

    return render_template(
        "matches/find_my_adopter.html",
        ranking=ranking,
        analysis_status=bus.dispatch_query(GetAnalysisStatusQuery(animal_id=animal_id)),
        mode="discovery",
    )


@match_blueprint.route("/analyses/<match_analysis_id>")
@require_sign_in
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
@require_sign_in
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


@match_blueprint.route("/api/analysis-status")
@require_sign_in
def analysis_status() -> Response:
    """Report how much agent work is outstanding, as JSON.

    Exists so a page can poll without re-rendering itself. The agent
    writes explanations asynchronously, and the alternative - blocking the
    request until it finishes - is what rule R4 forbids.

    Two scopes, and which one applies is decided by the signed-in account
    rather than by the query string:

    - `?scope=my-matches` answers about the adopter's own analyses, using
      their profile identifier from the session. An adopter cannot ask
      about anybody else because there is no parameter that would let
      them.
    - `?animal_id=<id>` answers about one animal, and is staff-only. It
      returns counts and a timestamp, never a name or a score.

    Returns:
        A small JSON object: pending, completed, failed, generation and
        oldest_pending_at.
    """
    scope = (request.args.get("scope") or "").strip().lower()
    animal_id = (request.args.get("animal_id") or "").strip()

    if scope == "my-matches":
        query = GetAnalysisStatusQuery(
            adopter_profile_id=current_user.adopter_profile_id
        )
    elif animal_id:
        if not current_user.is_staff:
            abort(403)
        query = GetAnalysisStatusQuery(animal_id=animal_id)
    else:
        abort(400)

    status = get_bus().dispatch_query(query)
    return jsonify(status.as_dictionary())


@match_blueprint.route("/search/describe", methods=["GET", "POST"])
def natural_language_search() -> ViewResult:
    """Describe what you are looking for, and queue its interpretation.

    Spec section 6.3 wants search in the adopter's own words, and
    interpreting words needs a model. On this hardware a model call costs
    11 to 16 seconds (CLAUDE.md R4), so it cannot happen here: this view
    validates the text, enqueues an `INTERPRET_INTENT` job and redirects to
    the page that waits for it. The request itself does no inference and
    touches no model.

    Open to anyone, including signed-out visitors: describing what you want
    is a browsing feature, not a personal one.

    Returns:
        The empty form on GET, a redirect to the job's page on a good POST,
        or the form again under 400 when the description is unusable.
    """
    if request.method == "GET":
        return _describe_page((request.args.get("q") or "").strip())

    return _queue_interpretation()


def _queue_interpretation() -> ViewResult:
    """Validate a submitted description and enqueue its interpretation.

    The length cap is enforced here rather than trusted from the textarea's
    `maxlength`, which a forged post ignores (NFR-5.2). It matters more than
    the usual field cap: this text becomes a model prompt in another
    process, where its length is directly a cost, and the route is open to
    signed-out visitors.

    Returns:
        A redirect to the new job's page, or the re-rendered form under 400.
    """
    described = (request.form.get("description") or "").strip()
    if not described or len(described) > MAXIMUM_DESCRIPTION_LENGTH:
        return _describe_page(described, error=_description_problem(described)), 400

    use_profile = parse_checkbox(request.form.get("use_profile")) and _may_fuse_profile()

    job_id = get_bus().dispatch_command(
        EnqueueIntentInterpretationCommand(
            natural_language_query=described,
            adopter_profile_id=current_user.adopter_profile_id if use_profile else None,
        )
    )
    return redirect(url_for("matches.describe_result", analysis_job_id=job_id))


@match_blueprint.route("/search/describe/<analysis_job_id>")
def describe_result(analysis_job_id: str) -> ViewResult:
    """Show one described search: waiting, failed, or its results.

    The page a POST redirects to, and the one a visitor may reload. While
    the agent is still working it renders the pending block and refreshes
    itself; once the interpretation lands it runs the *deterministic* search
    against it and shows animals.

    Two kinds of result, and which one appears is decided by the job rather
    than by this request: a job carrying a profile fuses the adopter's
    stable situation with what they just asked for (spec section 6.4), and
    one without runs the ordinary structured search.

    Args:
        analysis_job_id: The interpretation job, from the URL.

    Returns:
        The describe page in whichever state the job is in.
    """
    try:
        job = get_bus().dispatch_query(
            GetIntentJobQuery(
                analysis_job_id=analysis_job_id,
                viewer_adopter_profile_id=_signed_in_profile_id(),
                viewer_is_staff=_signed_in_as_staff(),
            )
        )
    except IntentJobNotYoursError:
        abort(403)

    if job is None:
        abort(404)
    if not job.is_ready:
        return _describe_page(job.described, job=job)

    return _describe_results(job)


def _describe_results(job: InterpretedIntentView) -> str:
    """Run the deterministic search for a completed interpretation.

    Args:
        job: A job whose `is_ready` is true, so its intent is present.

    Returns:
        The describe page showing what the criteria matched.
    """
    intent = job.intent
    assert intent is not None  # guaranteed by InterpretedIntentView.is_ready

    if not intent.understood or not intent.has_any_criteria:
        return _describe_page(job.described, job=job, intent=intent)

    filters = filters_from_intent(intent)

    if job.adopter_profile_id is not None:
        ranked = get_bus().dispatch_query(
            FindMyPetWithIntentQuery(
                adopter_profile_id=job.adopter_profile_id, filters=filters
            )
        )
        return _describe_page(job.described, job=job, intent=intent, ranked=ranked)

    results = get_bus().dispatch_query(
        SearchAnimalsQuery(filters=filters, page=1, page_size=DEFAULT_PAGE_SIZE)
    )
    return _describe_page(job.described, job=job, intent=intent, results=results)


def _describe_page(
    described: str,
    job: InterpretedIntentView | None = None,
    intent: SearchIntent | None = None,
    results: AnimalSearchResults | None = None,
    ranked: list[AnimalCard] | None = None,
    error: str | None = None,
) -> str:
    """Render the describe screen in one of its states.

    Every branch of this feature renders the same template, so they render
    it through one function: a state that forgot to pass `can_use_profile`
    would silently drop the checkbox rather than fail.

    Args:
        described: What the visitor typed, for re-rendering the textarea.
        job: The interpretation job, when there is one.
        intent: The parsed criteria, when the job completed.
        results: Intent-only search results.
        ranked: Profile-fused results, already ordered best first.
        error: What is wrong with the description, when it was refused.

    Returns:
        The rendered page.
    """
    return render_template(
        "matches/describe.html",
        described=described,
        job=job,
        intent=intent,
        results=results,
        ranked=ranked,
        error=error,
        can_use_profile=_may_fuse_profile(),
        maximum_description_length=MAXIMUM_DESCRIPTION_LENGTH,
    )


def _description_problem(described: str) -> str:
    """Say what is wrong with a refused description, in the visitor's terms."""
    if not described:
        return "Please describe what you are looking for."
    return (
        f"Please keep your description under "
        f"{MAXIMUM_DESCRIPTION_LENGTH} characters."
    )


def _signed_in_profile_id() -> str | None:
    """The signed-in adopter's profile identifier, or None for anyone else.

    Anonymous visitors reach these routes, and Flask-Login's anonymous user
    carries none of the attributes `AuthenticatedUser` does.
    """
    if not current_user.is_authenticated:
        return None
    profile_id: str | None = current_user.adopter_profile_id
    return profile_id


def _signed_in_as_staff() -> bool:
    """Whether a staff member is signed in."""
    return bool(current_user.is_authenticated and current_user.is_staff)


def _may_fuse_profile() -> bool:
    """Whether this visitor can combine their profile with an intent.

    Spec section 6.4 fuses *stable* facts with the current request, and an
    incomplete profile has no stable facts worth fusing - scoring against
    half a household would rank animals by guesswork and present it as
    personalisation.
    """
    return bool(
        current_user.is_authenticated
        and current_user.is_adopter
        and current_user.adopter_profile_id
        and current_user.has_complete_profile
    )
