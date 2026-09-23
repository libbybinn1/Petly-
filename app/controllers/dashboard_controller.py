"""Staff dashboard controller (blueprint requirement 4.4)."""

from __future__ import annotations

from flask import Blueprint, render_template
from flask_login import login_required

from app.controllers.helpers import get_bus
from app.cqrs.queries.dashboard_queries import GetDashboardSummaryQuery
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
