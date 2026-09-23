"""Small SQL expression helpers that paper over dialect differences.

Kept in one place so a dialect quirk is fixed once rather than wherever it
happens to bite.
"""

from __future__ import annotations

from sqlalchemy import ColumnElement
from sqlalchemy.orm import InstrumentedAttribute

# SQL Server has no boolean type. Booleans are BIT columns holding 0 or 1, and
# it has no IS TRUE / IS FALSE predicate either. SQLAlchemy's `.is_(True)`
# renders as `IS 1`, which SQL Server rejects with "Incorrect syntax near '1'".
#
# SQLite accepts `IS 1` quite happily, so this only fails against the real
# database - which is exactly the kind of difference worth hiding behind a
# named helper instead of rediscovering.
TRUE_BIT = 1
FALSE_BIT = 0


def is_true(column: InstrumentedAttribute[bool]) -> ColumnElement[bool]:
    """Predicate matching rows where a boolean column is set.

    Args:
        column: A boolean-mapped column.

    Returns:
        An expression rendering as `column = 1`, valid on SQL Server and
        SQLite alike.
    """
    return column == TRUE_BIT


def is_false(column: InstrumentedAttribute[bool]) -> ColumnElement[bool]:
    """Predicate matching rows where a boolean column is not set.

    Args:
        column: A boolean-mapped column.

    Returns:
        An expression rendering as `column = 0`.
    """
    return column == FALSE_BIT
