"""Staff dashboard controller (blueprint requirement 4.4)."""

from __future__ import annotations

from flask import Blueprint, abort, render_template
from flask_login import login_required

from app.controllers.helpers import get_bus
from app.cqrs.queries.dashboard_queries import GetDashboardSummaryQuery
from app.cqrs.queries.history_queries import GetAggregateHistoryQuery
from app.security.authorization import require_staff

dashboard_blueprint = Blueprint("dashboard", __name__)


@dashboard_blueprint.route("/dashboard")
@login_required
@require_staff()
def dashboard() -> str:
    """Show the operational dashboard.

    Staff only. An adopter reaching this URL receives 403 from the decorator,
    on the server, regardless of what the navigation offered them.
    """
    summary = get_bus().dispatch_query(GetDashboardSummaryQuery())
    return render_template("dashboard.html", summary=summary)


@dashboard_blueprint.route("/history/<aggregate_type>/<aggregate_id>")
@login_required
@require_staff()
def aggregate_history(aggregate_type: str, aggregate_id: str) -> str:
    """Show one aggregate's recorded history (blueprint section 10).

    Rendered straight from the event log. Blueprint section 10 requires the
    system to be able to display how a case reached its current state, and
    the log is the only record that actually holds that.
    """
    history = get_bus().dispatch_query(
        GetAggregateHistoryQuery(
            aggregate_type=aggregate_type, aggregate_id=aggregate_id
        )
    )
    if history is None:
        abort(404)

    return render_template("history.html", history=history)
