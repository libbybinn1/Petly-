"""Small accessors shared by the controller blueprints.

Kept separate so controllers reach application services through named
functions rather than poking at `current_app.config` in every view.
"""

from __future__ import annotations

from typing import cast
from urllib.parse import urlparse

from flask import current_app
from sqlalchemy.orm import Session, sessionmaker
from werkzeug.wrappers import Response

from app.config import Configuration
from app.cqrs.base import MessageBus

# What a view is allowed to hand back. The third form is how a rejected form
# post answers: the page re-rendered with its errors, under an explicit 400
# or 409 rather than a misleading 200.
ViewResult = str | Response | tuple[str, int]


def get_bus() -> MessageBus:
    """Return the request's message bus."""
    return cast("MessageBus", current_app.config["BUS"])


def get_session_factory() -> sessionmaker[Session]:
    """Return the session factory.

    Used only by controllers that legitimately own an authentication concern,
    such as verifying a password. Business reads and writes go through the bus.
    """
    return cast("sessionmaker[Session]", current_app.config["SESSION_FACTORY"])


def get_configuration() -> Configuration:
    """Return the loaded application configuration."""
    return cast("Configuration", current_app.config["PETMATCH"])


# Far past any real result set, and small enough that (page - 1) * page_size
# still fits in a database integer. Python integers are unbounded, so without
# a ceiling a query string like ?page=99999999999999999999 reaches the driver
# and raises - an unhandled 500 from a route anonymous visitors can reach.
MAXIMUM_PAGE_NUMBER = 100_000


def parse_positive_integer(raw_value: str | None, default: int) -> int:
    """Parse a query-string integer, falling back when it is absent or invalid.

    Args:
        raw_value: The raw query-string value.
        default: Value to use when parsing fails, or when the value exceeds
            `MAXIMUM_PAGE_NUMBER`.

    Returns:
        The parsed integer when it is between 1 and the maximum, otherwise
        the default.
    """
    if not raw_value:
        return default
    try:
        parsed = int(raw_value)
    except ValueError:
        return default
    if parsed < 1 or parsed > MAXIMUM_PAGE_NUMBER:
        return default
    return parsed


def is_safe_redirect_target(target: str) -> bool:
    """Whether a submitted destination points inside this application (NFR-4.3).

    Every such value is attacker-controlled. `/login?next=...` is the obvious
    one, but a notification's `target_url` arrives in a hidden field that the
    sender can equally well rewrite - and that route had only a leading-slash
    check, which "//evil.example.com" passes, and so does the same host after
    a slash and a backslash, which some browsers normalise to "//".
    Redirecting a signed-in user off-site immediately after they acted on a
    message from us is a credible phishing step, so one function decides it
    for both routes rather than two spellings of the same rule drifting apart.

    Parsing decides it rather than character inspection: a URL with any
    scheme or any host is off-site, whatever spelling was used to smuggle it
    past, including a backslash that some browsers normalise to "/".

    Args:
        target: The raw submitted destination.

    Returns:
        True only for a relative path within this application.
    """
    if not target.startswith("/"):
        return False
    if target.startswith(("//", "/\\")):
        return False

    parsed = urlparse(target)
    return not parsed.scheme and not parsed.netloc


def parse_checkbox(raw_value: str | None) -> bool:
    """Interpret an HTML checkbox value.

    Unchecked boxes are absent from the submission entirely, so anything
    present and not explicitly falsy counts as checked.
    """
    return raw_value is not None and raw_value.lower() not in ("", "0", "false", "off")
