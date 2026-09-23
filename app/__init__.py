"""PetMatch Flask application factory.

Wires the MVC layers together: blueprints act as Controllers, Jinja2
templates as Views, and `app.domain` as the Model. The message bus sits
between controllers and the data layer so no controller ever touches a
database session directly (rule R2).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from flask import Flask, render_template, request
from flask_login import LoginManager
from flask_wtf.csrf import CSRFError, CSRFProtect
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.config import Configuration, load_configuration
from app.cqrs.base import MessageBus
from app.infrastructure.database import create_database_engine, create_session_factory
from app.infrastructure.models import AdopterProfile, User
from app.security.authorization import AuthenticatedUser

login_manager = LoginManager()


csrf_protection = CSRFProtect()


def _harden_the_session_cookie(application: Flask) -> None:
    """Restrict how the session cookie may be sent (NFR-4.3).

    SameSite=Lax is the second half of CSRF defence and the half that keeps
    working when a form is missed: the browser simply does not attach the
    session cookie to a cross-site POST, so a forged request arrives
    unauthenticated rather than acting as the signed-in user. Lax rather
    than Strict so that an ordinary link into the site from an email still
    arrives signed in.

    HttpOnly keeps the cookie out of JavaScript's reach, limiting what an
    injected script could do with it.

    Secure is deliberately left off. It would stop the cookie being sent
    over plain HTTP, which is exactly how this application is run and
    demonstrated locally; turning it on here would break sign-in on
    127.0.0.1 rather than protect anything. A deployment behind TLS should
    set SESSION_COOKIE_SECURE=True.
    """
    application.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    application.config["SESSION_COOKIE_HTTPONLY"] = True


def create_app(configuration: Configuration | None = None) -> Flask:
    """Build and configure the Flask application.

    Args:
        configuration: Override for tests. Loaded from the environment when
            omitted.

    Returns:
        A fully wired application ready to serve requests.
    """
    settings = configuration or load_configuration()

    application = Flask(__name__)
    application.config["SECRET_KEY"] = settings.secret_key
    application.config["PETMATCH"] = settings
    _harden_the_session_cookie(application)
    csrf_protection.init_app(application)

    engine = create_database_engine(settings)
    session_factory = create_session_factory(engine)
    application.config["SESSION_FACTORY"] = session_factory

    bus = MessageBus(session_factory)
    _register_handlers(bus, settings)
    application.config["BUS"] = bus

    login_manager.init_app(application)
    login_manager.login_view = "auth.login"
    login_manager.login_message = "Please sign in to continue."
    login_manager.login_message_category = "info"

    def load_user(user_id: str) -> AuthenticatedUser | None:
        """Rehydrate the signed-in account for each request."""
        with session_factory() as session:
            user = session.execute(
                select(User).where(User.user_id == user_id)
            ).scalar_one_or_none()
            if user is None or not user.is_active:
                return None

            profile = session.execute(
                select(AdopterProfile).where(AdopterProfile.user_id == user_id)
            ).scalar_one_or_none()

            return AuthenticatedUser.from_model(
                user,
                adopter_profile_id=profile.adopter_profile_id if profile else None,
                is_complete=bool(profile and profile.is_complete),
            )

    # Registered by call rather than with `@login_manager.user_loader`.
    # Flask-Login carries no type information, so its decorator would erase
    # this function's signature and stop the body being type-checked.
    login_manager.user_loader(load_user)

    _register_expiry_sweep(application, bus)
    _register_blueprints(application)
    _register_error_handlers(application)
    _register_template_helpers(application)

    return application


def _register_handlers(bus: MessageBus, settings: Configuration) -> None:
    """Register every command and query handler on the bus.

    Args:
        bus: The bus to register on.
        settings: Loaded configuration, for the handlers that need a
            configured value rather than a hard-coded one.
    """
    from app.cqrs.commands.animal_commands import (
        ChangeAnimalStatusCommand,
        ChangeAnimalStatusHandler,
        CreateAnimalCommand,
        CreateAnimalHandler,
        UpdateAnimalCommand,
        UpdateAnimalHandler,
    )
    from app.cqrs.commands.application_commands import (
        ApproveApplicationCommand,
        ApproveApplicationHandler,
        MarkApplicationUnderReviewCommand,
        MarkApplicationUnderReviewHandler,
        RejectApplicationCommand,
        RejectApplicationHandler,
        ReverseApprovalCommand,
        ReverseApprovalHandler,
        SubmitApplicationCommand,
        SubmitApplicationHandler,
        WithdrawApplicationCommand,
        WithdrawApplicationHandler,
    )
    from app.cqrs.commands.invitation_commands import (
        ExpireOverdueInvitationsCommand,
        ExpireOverdueInvitationsHandler,
        MarkInvitationViewedCommand,
        MarkInvitationViewedHandler,
        RespondToInvitationCommand,
        RespondToInvitationHandler,
        SendInvitationCommand,
        SendInvitationHandler,
    )
    from app.cqrs.commands.profile_commands import (
        SaveAdopterProfileCommand,
        SaveAdopterProfileHandler,
    )
    from app.cqrs.queries.animal_queries import (
        GetAnimalDetailsHandler,
        GetAnimalDetailsQuery,
        ListAllAnimalsHandler,
        ListAllAnimalsQuery,
        SearchAnimalsHandler,
        SearchAnimalsQuery,
    )
    from app.cqrs.queries.dashboard_queries import (
        GetDashboardSummaryHandler,
        GetDashboardSummaryQuery,
    )
    from app.cqrs.queries.history_queries import (
        GetAggregateHistoryHandler,
        GetAggregateHistoryQuery,
    )
    from app.cqrs.queries.match_queries import (
        FindMoreAdoptersHandler,
        FindMoreAdoptersQuery,
        FindMyPetHandler,
        FindMyPetQuery,
        GetMatchAnalysisHandler,
        GetMatchAnalysisQuery,
        RankApplicantsHandler,
        RankApplicantsQuery,
    )
    from app.cqrs.queries.personal_queries import (
        CountUnreadNotificationsHandler,
        CountUnreadNotificationsQuery,
        ListMyApplicationsHandler,
        ListMyApplicationsQuery,
        ListMyInvitationsHandler,
        ListMyInvitationsQuery,
    )
    from app.cqrs.queries.profile_queries import GetMyProfileHandler, GetMyProfileQuery

    bus.register_query(SearchAnimalsQuery, SearchAnimalsHandler())
    bus.register_query(GetAnimalDetailsQuery, GetAnimalDetailsHandler())
    bus.register_query(ListAllAnimalsQuery, ListAllAnimalsHandler())
    bus.register_query(RankApplicantsQuery, RankApplicantsHandler())
    bus.register_query(FindMoreAdoptersQuery, FindMoreAdoptersHandler())
    bus.register_query(FindMyPetQuery, FindMyPetHandler())
    bus.register_query(GetMatchAnalysisQuery, GetMatchAnalysisHandler())

    bus.register_command(SubmitApplicationCommand, SubmitApplicationHandler())
    bus.register_command(WithdrawApplicationCommand, WithdrawApplicationHandler())
    bus.register_command(ApproveApplicationCommand, ApproveApplicationHandler())
    bus.register_command(ReverseApprovalCommand, ReverseApprovalHandler())
    bus.register_command(RejectApplicationCommand, RejectApplicationHandler())
    bus.register_command(CreateAnimalCommand, CreateAnimalHandler())
    bus.register_command(UpdateAnimalCommand, UpdateAnimalHandler())
    bus.register_command(ChangeAnimalStatusCommand, ChangeAnimalStatusHandler())
    bus.register_command(
        MarkApplicationUnderReviewCommand, MarkApplicationUnderReviewHandler()
    )
    bus.register_command(SaveAdopterProfileCommand, SaveAdopterProfileHandler())
    bus.register_command(
        SendInvitationCommand,
        SendInvitationHandler(
            response_window_hours=settings.invitation_expiry_hours
        ),
    )
    bus.register_command(MarkInvitationViewedCommand, MarkInvitationViewedHandler())
    bus.register_command(RespondToInvitationCommand, RespondToInvitationHandler())
    bus.register_command(
        ExpireOverdueInvitationsCommand, ExpireOverdueInvitationsHandler()
    )

    bus.register_query(GetDashboardSummaryQuery, GetDashboardSummaryHandler())
    bus.register_query(GetMyProfileQuery, GetMyProfileHandler())
    bus.register_query(GetAggregateHistoryQuery, GetAggregateHistoryHandler())
    bus.register_query(ListMyInvitationsQuery, ListMyInvitationsHandler())
    bus.register_query(ListMyApplicationsQuery, ListMyApplicationsHandler())
    bus.register_query(
        CountUnreadNotificationsQuery, CountUnreadNotificationsHandler()
    )


# How often the expiry sweep may run, at most. An invitation expiring a few
# minutes late is harmless; a sweep on every request would add a write
# transaction to every page load against a throttled shared database.
EXPIRY_SWEEP_INTERVAL = timedelta(minutes=5)


def _register_expiry_sweep(application: Flask, bus: MessageBus) -> None:
    """Expire overdue invitations periodically, from the request cycle.

    Spec section 7.4 gives an adopter a fixed window, which means something
    has to notice when it closes. The command existed and was registered on
    the bus, but nothing ever dispatched it: an invitation stayed SENT for
    ever, and the "expires in N hours" label eventually counted down past
    zero while the buttons still worked.

    A `before_request` hook rather than a scheduler, because the project
    runs as two processes started by hand and adding a third to tick a
    clock would be more machinery than the rule is worth. The cost is that
    expiry happens only while somebody is using the site - acceptable,
    because the only thing that observes an expired invitation is a page
    someone is looking at.

    The timestamp guard is what makes it affordable: at most one sweep per
    interval, however many requests arrive.

    Args:
        application: The application to attach the hook to.
        bus: The bus the sweep command is dispatched on.
    """
    from app.cqrs.commands.invitation_commands import ExpireOverdueInvitationsCommand

    state = {"last_run": datetime.min.replace(tzinfo=UTC)}

    @application.before_request
    def expire_overdue_invitations() -> None:
        """Run the sweep if enough time has passed since the last one."""
        if request.endpoint == "static":
            return

        now = datetime.now(UTC)
        if now - state["last_run"] < EXPIRY_SWEEP_INTERVAL:
            return

        # Recorded before the attempt, not after: a sweep that fails should
        # not be retried on the very next request.
        state["last_run"] = now

        try:
            bus.dispatch_command(ExpireOverdueInvitationsCommand())
        except SQLAlchemyError:
            # A sweep is housekeeping. If the database is unreachable the
            # page the visitor actually asked for will report that itself,
            # and failing their request over this would be worse.
            application.logger.warning("invitation expiry sweep failed", exc_info=True)


def _register_blueprints(application: Flask) -> None:
    """Attach the controller blueprints."""
    from app.controllers.animal_controller import animal_blueprint
    from app.controllers.auth_controller import auth_blueprint
    from app.controllers.dashboard_controller import dashboard_blueprint
    from app.controllers.home_controller import home_blueprint
    from app.controllers.match_controller import match_blueprint
    from app.controllers.personal_controller import personal_blueprint

    application.register_blueprint(home_blueprint)
    application.register_blueprint(auth_blueprint)
    application.register_blueprint(animal_blueprint)
    application.register_blueprint(match_blueprint)
    application.register_blueprint(personal_blueprint)
    application.register_blueprint(dashboard_blueprint)


def _register_error_handlers(application: Flask) -> None:
    """Render friendly pages for the errors users actually hit."""

    @application.errorhandler(401)
    def unauthorized(_error: object) -> tuple[str, int]:
        return render_template(
            "error.html",
            code=401,
            title="Please sign in",
            message="You need to be signed in to view this page.",
        ), 401

    @application.errorhandler(CSRFError)
    def csrf_token_missing(_error: object) -> tuple[str, int]:
        """Explain a rejected form post in terms a person can act on.

        Flask-WTF answers 400 by default with a bare message. The usual
        innocent cause is a form left open long enough for the session to
        roll over, and "submit it again" is genuinely the fix - so say
        that, rather than showing a security notice to somebody who did
        nothing wrong.
        """
        return render_template(
            "error.html",
            code=400,
            title="That form has expired",
            message=(
                "For your security this form could not be submitted. "
                "Please go back, reload the page and try again."
            ),
        ), 400

    @application.errorhandler(403)
    def forbidden(_error: object) -> tuple[str, int]:
        return render_template(
            "error.html",
            code=403,
            title="Not allowed",
            message="Your account does not have permission to do that.",
        ), 403

    @application.errorhandler(404)
    def not_found(_error: object) -> tuple[str, int]:
        return render_template(
            "error.html",
            code=404,
            title="Page not found",
            message="We could not find what you were looking for.",
        ), 404


def _register_template_helpers(application: Flask) -> None:
    """Expose small formatting helpers to templates.

    Templates display; they do not decide. These helpers only reformat values
    that have already been computed elsewhere.
    """

    @application.template_filter("humanize")
    def humanize(value: str | None) -> str:
        """Turn an enum value such as GUINEA_PIG into 'Guinea Pig'."""
        if not value:
            return ""
        return value.replace("_", " ").title()
