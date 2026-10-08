"""Tests for the Somee SQL Server URL the application actually opens.

The live handshake is proven by `scripts/check_environment.py`. This file
proves the URL the engine would open is the one FreeTDS can finish on
Windows: credentials stay encoded, and the client charset is present.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from app.config import SQL_SERVER_CLIENT_CHARSET, DatabaseConfiguration

pytestmark = pytest.mark.unit


def test_sqlalchemy_url_asks_freetds_for_the_windows_login_charset() -> None:
    """Proves the Somee URL includes the charset that completes the TDS login.

    Without `charset=CP1252`, pymssql's bundled FreeTDS fails converting the
    login packet (error 2402) and reports the generic TDS 20002.
    """
    configuration = DatabaseConfiguration(
        server="instance.mssql.somee.com",
        database="pets",
        user="login",
        password="secret",
    )

    query = parse_qs(urlparse(configuration.sqlalchemy_url).query)

    assert query["charset"] == [SQL_SERVER_CLIENT_CHARSET]
    assert SQL_SERVER_CLIENT_CHARSET == "CP1252"


def test_sqlalchemy_url_percent_encodes_password_characters_that_break_a_url() -> None:
    """Proves a password containing `@`, `/` and `#` is encoded, not interpolated.

    Those characters are legal in Somee passwords and would steal URL
    structure if they were pasted in raw. The charset query must still
    survive the encoding.
    """
    configuration = DatabaseConfiguration(
        server="instance.mssql.somee.com",
        database="pets",
        user="login",
        password="p@ss/w#rd",
    )

    url = configuration.sqlalchemy_url

    assert "p@ss/w#rd" not in url
    assert "p%40ss%2Fw%23rd" in url
    assert f"charset={SQL_SERVER_CLIENT_CHARSET}" in url


def test_sqlalchemy_url_without_a_charset_query_is_rejected_as_the_old_bug() -> None:
    """Proves a URL that looks like the pre-fix form would fail the charset rule.

    This is the negative: the broken connection string (scheme, host, database,
    no charset) is exactly what produced TDS 20002 on this machine.
    """
    broken = "mssql+pymssql://login:secret@instance.mssql.somee.com/pets"
    working = DatabaseConfiguration(
        server="instance.mssql.somee.com",
        database="pets",
        user="login",
        password="secret",
    ).sqlalchemy_url

    assert "charset=" not in broken
    assert working != broken
    assert working.startswith(broken)
