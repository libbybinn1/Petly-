"""Small accessors shared by the controller blueprints.

Kept separate so controllers reach application services through named
functions rather than poking at `current_app.config` in every view.
"""

from __future__ import annotations

from flask import current_app
from sqlalchemy.orm import Session, sessionmaker

from app.config import Configuration
from app.cqrs.base import MessageBus


def get_bus() -> MessageBus:
    """Return the request's message bus."""
    return current_app.config["BUS"]


def get_session_factory() -> sessionmaker[Session]:
    """Return the session factory.

    Used only by controllers that legitimately own an authentication concern,
    such as verifying a password. Business reads and writes go through the bus.
    """
    return current_app.config["SESSION_FACTORY"]


def get_configuration() -> Configuration:
    """Return the loaded application configuration."""
    return current_app.config["PETMATCH"]


def parse_positive_integer(raw_value: str | None, default: int) -> int:
    """Parse a query-string integer, falling back when it is absent or invalid.

    Args:
        raw_value: The raw query-string value.
        default: Value to use when parsing fails.

    Returns:
        The parsed integer when it is at least 1, otherwise the default.
    """
    if not raw_value:
        return default
    try:
        parsed = int(raw_value)
    except ValueError:
        return default
    return parsed if parsed >= 1 else default


def parse_checkbox(raw_value: str | None) -> bool:
    """Interpret an HTML checkbox value.

    Unchecked boxes are absent from the submission entirely, so anything
    present and not explicitly falsy counts as checked.
    """
    return raw_value is not None and raw_value.lower() not in ("", "0", "false", "off")
