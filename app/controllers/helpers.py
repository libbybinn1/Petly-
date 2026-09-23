"""Small accessors shared by the controller blueprints.

Kept separate so controllers reach application services through named
functions rather than poking at `current_app.config` in every view.
"""

from __future__ import annotations

from typing import cast

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


def parse_checkbox(raw_value: str | None) -> bool:
    """Interpret an HTML checkbox value.

    Unchecked boxes are absent from the submission entirely, so anything
    present and not explicitly falsy counts as checked.
    """
    return raw_value is not None and raw_value.lower() not in ("", "0", "false", "off")
