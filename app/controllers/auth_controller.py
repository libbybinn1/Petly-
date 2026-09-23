"""Authentication controller: registration, sign-in and sign-out.

Controllers parse the request, check authorization, dispatch, and render.
Password verification lives here because it is an authentication concern
rather than a domain rule; everything else is delegated.
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlparse

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_user, logout_user
from sqlalchemy import select
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.wrappers import Response

from app.controllers.helpers import ViewResult, get_session_factory
from app.domain.enums import UserRole
from app.infrastructure.models import AdopterProfile, User, new_identifier
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

    session_factory = get_session_factory()
    with session_factory() as session:
        user = session.execute(
            select(User).where(User.email == email)
        ).scalar_one_or_none()

        # One message for both "no such account" and "wrong password", so the
        # form cannot be used to discover which addresses are registered.
        if user is None or not check_password_hash(user.password_hash, password):
            flash("Email or password is incorrect.", "error")
            return render_template("auth/login.html", email=email), 401

        if not user.is_active:
            flash("That account has been deactivated.", "error")
            return render_template("auth/login.html", email=email), 403

        profile = session.execute(
            select(AdopterProfile).where(AdopterProfile.user_id == user.user_id)
        ).scalar_one_or_none()

        authenticated = AuthenticatedUser.from_model(
            user,
            adopter_profile_id=profile.adopter_profile_id if profile else None,
            is_complete=bool(profile and profile.is_complete),
        )

    login_user(authenticated)
    flash(f"Welcome back, {authenticated.full_name.split()[0]}.", "success")

    next_url = request.args.get("next")
    if next_url is not None and _is_safe_redirect_target(next_url):
        return redirect(next_url)
    return redirect(url_for("home.index"))


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

    session_factory = get_session_factory()
    with session_factory() as session:
        already_registered = session.execute(
            select(User).where(User.email == email)
        ).scalar_one_or_none()
        if already_registered is not None:
            flash("An account with that email already exists.", "error")
            return render_template("auth/register.html", full_name=full_name, email=email), 409

        now = datetime.now(UTC).replace(tzinfo=None)
        user = User(
            user_id=new_identifier(),
            email=email,
            password_hash=generate_password_hash(password),
            full_name=full_name,
            role=UserRole.ADOPTER.value,
            is_active=True,
            created_at=now,
        )
        session.add(user)
        session.commit()

        authenticated = AuthenticatedUser.from_model(
            user, adopter_profile_id=None, is_complete=False
        )

    login_user(authenticated)
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
