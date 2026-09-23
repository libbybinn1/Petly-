"""Authentication controller: registration, sign-in and sign-out.

Controllers parse the request, check authorization, dispatch, and render.

This one used to hold a database session, on the argument that
authentication is not a business read. CLAUDE.md R2 says a controller
must never touch a session, and the exception did not survive being
looked at: verifying a password is a query with an unusual result, not a
different kind of thing, and "this one is special" is how a layer
boundary erodes.

What remains here is `check_password_hash` and `generate_password_hash`,
which compare and derive strings and touch no storage. Everything that
reads or writes goes through the bus.
"""

from __future__ import annotations

from urllib.parse import urlparse

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_user, logout_user
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.wrappers import Response

from app.controllers.helpers import ViewResult, get_bus
from app.cqrs.commands.account_commands import (
    EmailAlreadyRegisteredError,
    RegisterAdopterCommand,
)
from app.cqrs.queries.auth_queries import (
    AccountForSignIn,
    EmailIsRegisteredQuery,
    GetAccountForSignInQuery,
)
from app.domain.enums import UserRole
from app.security.authorization import AuthenticatedUser, require_sign_in

auth_blueprint = Blueprint("auth", __name__)

MINIMUM_PASSWORD_LENGTH = 8

# Matches users.full_name, NVARCHAR(150). Longer input truncates on SQL
# Server 2014 rather than saving, which surfaces as a 500 on a form that
# looked perfectly valid.
MAXIMUM_FULL_NAME_LENGTH = 150


def _validate_registration(
    full_name: str, email: str, password: str, confirm_password: str
) -> list[str]:
    """Collect every problem with a registration submission.

    All errors are gathered rather than returned one at a time, so the user
    fixes the whole form in one pass.
    """
    errors: list[str] = []
    if not full_name.strip():
        errors.append("Please enter your full name.")
    elif len(full_name.strip()) > MAXIMUM_FULL_NAME_LENGTH:
        errors.append(
            f"Please keep your name under {MAXIMUM_FULL_NAME_LENGTH} characters."
        )
    if "@" not in email or "." not in email.split("@")[-1]:
        errors.append("Please enter a valid email address.")
    if len(password) < MINIMUM_PASSWORD_LENGTH:
        errors.append(f"Password must be at least {MINIMUM_PASSWORD_LENGTH} characters.")
    if password != confirm_password:
        errors.append("The two passwords do not match.")
    return errors


@auth_blueprint.route("/login", methods=["GET", "POST"])
def login() -> ViewResult:
    """Sign a user in."""
    if current_user.is_authenticated:
        return redirect(url_for("home.index"))

    if request.method == "GET":
        return render_template("auth/login.html")

    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")

    account = get_bus().dispatch_query(GetAccountForSignInQuery(email=email))

    # One message for both "no such account" and "wrong password", so the
    # form cannot be used to discover which addresses are registered.
    if account is None or not check_password_hash(account.password_hash, password):
        flash("Email or password is incorrect.", "error")
        return render_template("auth/login.html", email=email), 401

    if not account.is_active:
        flash("That account has been deactivated.", "error")
        return render_template("auth/login.html", email=email), 403

    authenticated = _signed_in_user(account)
    login_user(authenticated)
    flash(f"Welcome back, {authenticated.full_name.split()[0]}.", "success")

    next_url = request.args.get("next")
    if next_url is not None and _is_safe_redirect_target(next_url):
        return redirect(next_url)
    return redirect(url_for("home.index"))


def _signed_in_user(account: AccountForSignIn) -> AuthenticatedUser:
    """Build Flask-Login's view of the account that just signed in.

    Built from the read model rather than an ORM row on purpose: the
    session outlives any database session, and a detached ORM instance
    raises when its attributes are touched later in the request.
    """
    return AuthenticatedUser(
        user_id=account.user_id,
        email=account.email,
        full_name=account.full_name,
        role=UserRole(account.role),
        adopter_profile_id=account.adopter_profile_id,
        has_complete_profile=account.has_complete_profile,
    )


def _is_safe_redirect_target(target: str) -> bool:
    """Whether a `next` value points inside this application.

    The value is attacker-controlled: anyone can send a victim a link to
    /login?next=... . A check for a leading "/" is not enough, because
    "//evil.example.com" also starts with one and is a fully qualified
    off-site address. That makes the sign-in form redirect a user to an
    attacker's page immediately after they typed their password on a
    genuine screen, which is a credible phishing step.

    Parsing decides it rather than character inspection: a URL with any
    scheme or any host is off-site, whatever spelling was used to smuggle
    it past, including a backslash that some browsers normalise to "/".

    Args:
        target: The raw `next` query parameter.

    Returns:
        True only for a relative path within this application.
    """
    if not target.startswith("/"):
        return False
    if target.startswith(("//", "/\\")):
        return False

    parsed = urlparse(target)
    return not parsed.scheme and not parsed.netloc


@auth_blueprint.route("/register", methods=["GET", "POST"])
def register() -> ViewResult:
    """Create a new adopter account."""
    if current_user.is_authenticated:
        return redirect(url_for("home.index"))

    if request.method == "GET":
        return render_template("auth/register.html")

    full_name = request.form.get("full_name", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    confirm_password = request.form.get("confirm_password", "")

    errors = _validate_registration(full_name, email, password, confirm_password)
    if errors:
        for message in errors:
            flash(message, "error")
        return render_template("auth/register.html", full_name=full_name, email=email), 400

    bus = get_bus()

    if bus.dispatch_query(EmailIsRegisteredQuery(email=email)):
        flash("An account with that email already exists.", "error")
        return render_template(
            "auth/register.html", full_name=full_name, email=email
        ), 409

    try:
        user_id = bus.dispatch_command(
            RegisterAdopterCommand(
                email=email,
                full_name=full_name,
                password_hash=generate_password_hash(password),
            )
        )
    except EmailAlreadyRegisteredError:
        # The check above is polite, not authoritative: two overlapping
        # registrations both pass it, and the unique index is what
        # settles the race. Answer the loser the same way.
        flash("An account with that email already exists.", "error")
        return render_template(
            "auth/register.html", full_name=full_name, email=email
        ), 409

    login_user(
        AuthenticatedUser(
            user_id=user_id,
            email=email,
            full_name=full_name,
            role=UserRole.ADOPTER,
            adopter_profile_id=None,
            has_complete_profile=False,
        )
    )
    flash("Welcome to PetMatch. Complete your profile to get personal matches.", "success")
    return redirect(url_for("home.index"))


@auth_blueprint.route("/logout", methods=["POST"])
@require_sign_in
def logout() -> Response:
    """Sign the current user out.

    POST only: a GET would let a third-party page sign the user out simply by
    embedding a link or image.
    """
    logout_user()
    flash("You have been signed out.", "info")
    return redirect(url_for("auth.login"))
