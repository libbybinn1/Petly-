"""Staff dashboard controller (blueprint requirement 4.4)."""

from __future__ import annotations

from flask import Blueprint, abort, render_template
from flask_login import current_user

from app.controllers.helpers import get_bus
from app.cqrs.queries.dashboard_queries import GetDashboardSummaryQuery
from app.cqrs.queries.history_queries import (
    GetAggregateHistoryQuery,
    HistoryNotVisibleError,
)
from app.security.authorization import require_sign_in, require_staff

dashboard_blueprint = Blueprint("dashboard", __name__)


@dashboard_blueprint.route("/dashboard")
@require_sign_in
@require_staff()
def dashboard() -> str:
    """Show the operational dashboard.

    Staff only. An adopter reaching this URL receives 403 from the decorator,
    on the server, regardless of what the navigation offered them.
    """
    summary = get_bus().dispatch_query(GetDashboardSummaryQuery())
    return render_template("dashboard.html", summary=summary)


@dashboard_blueprint.route("/history/<aggregate_type>/<aggregate_id>")
@require_sign_in
def aggregate_history(aggregate_type: str, aggregate_id: str) -> str:
    """Show one aggregate's recorded history (blueprint section 10).

    Rendered straight from the event log. Blueprint section 10 requires the
    system to be able to display how a case reached its current state, and
    the log is the only record that actually holds that.

    No longer staff-only. Spec section 7.5 is written from the adopter's
    point of view - an application closed because another was approved, and
    reopened when that approval was reversed - and an adopter who could not
    see that happen would have to take it on trust. Which records count as
    theirs is decided in the query, not here: an adopter sees their own
    applications and invitations, staff see everything, and an anonymous
    visitor is redirected to sign in by the decorator.

    Args:
        aggregate_type: One of `AggregateType`'s values, from the URL.
        aggregate_id: The record to show.

    Returns:
        The rendered timeline.
    """
    try:
        history = get_bus().dispatch_query(
            GetAggregateHistoryQuery(
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                viewer_adopter_profile_id=_signed_in_profile_id(),
                viewer_is_staff=bool(current_user.is_staff),
            )
        )
    except HistoryNotVisibleError:
        abort(403)

    if history is None:
        abort(404)

    return render_template("history.html", history=history)


def _signed_in_profile_id() -> str | None:
    """The signed-in adopter's profile identifier, or None for staff.

    `@require_sign_in` guarantees somebody is signed in by the time this
    runs, so the anonymous case cannot arise here.
    """
    profile_id: str | None = current_user.adopter_profile_id
    return profile_id
