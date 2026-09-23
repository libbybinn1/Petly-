"""PetMatch Flask application factory.

Wires the MVC layers together: blueprints act as Controllers, Jinja2
templates as Views, and `app.domain` as the Model. The message bus sits
between controllers and the data layer so no controller ever touches a
database session directly (rule R2).
"""

from __future__ import annotations

from flask import Flask, render_template
from flask_login import LoginManager
from sqlalchemy import select

from app.config import Configuration, load_configuration
from app.cqrs.base import MessageBus
from app.infrastructure.database import create_database_engine, create_session_factory
from app.infrastructure.models import AdopterProfile, User
from app.security.authorization import AuthenticatedUser

login_manager = LoginManager()


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

    engine = create_database_engine(settings)
    session_factory = create_session_factory(engine)
    application.config["SESSION_FACTORY"] = session_factory

    bus = MessageBus(session_factory)
    _register_handlers(bus)
    application.config["BUS"] = bus

    login_manager.init_app(application)
    login_manager.login_view = "auth.login"
    login_manager.login_message = "Please sign in to continue."
    login_manager.login_message_category = "info"

    @login_manager.user_loader
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

    _register_blueprints(application)
    _register_error_handlers(application)
    _register_template_helpers(application)

    return application


def _register_handlers(bus: MessageBus) -> None:
    """Register every command and query handler on the bus."""
    from app.cqrs.commands.application_commands import (
        ApproveApplicationCommand,
        ApproveApplicationHandler,
        ReverseApprovalCommand,
        ReverseApprovalHandler,
        SubmitApplicationCommand,
        SubmitApplicationHandler,
        WithdrawApplicationCommand,
        WithdrawApplicationHandler,
    )
    from app.cqrs.queries.animal_queries import (
        GetAnimalDetailsHandler,
        GetAnimalDetailsQuery,
        ListAllAnimalsHandler,
        ListAllAnimalsQuery,
        SearchAnimalsHandler,
        SearchAnimalsQuery,
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


def _register_blueprints(application: Flask) -> None:
    """Attach the controller blueprints."""
    from app.controllers.animal_controller import animal_blueprint
    from app.controllers.auth_controller import auth_blueprint
    from app.controllers.home_controller import home_blueprint
    from app.controllers.match_controller import match_blueprint

    application.register_blueprint(home_blueprint)
    application.register_blueprint(auth_blueprint)
    application.register_blueprint(animal_blueprint)
    application.register_blueprint(match_blueprint)


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
