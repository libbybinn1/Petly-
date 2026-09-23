"""Adversarial API tests for untrusted input on every form and query string.

Spec section 21 and NFR-5.2 make the server the real validator, so every
request here is one a browser would have refused to send: values outside their
range, values longer than the column that stores them, script tags, and enum
values no dropdown offers.

The database is throwaway SQLite (see tests/api/conftest.py), which accepts
over-long strings that SQL Server 2014 would refuse. Where that difference
matters a test says so, because it is precisely the class of bug a SQLite-only
suite hides.
"""

from __future__ import annotations

from typing import Any

import pytest
from app.infrastructure.models import AdopterProfile, AdoptionApplication, User
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.api

VALID_PROFILE_FORM = {
    "home_type": "HOUSE",
    "experience_level": "SOME",
    "activity_level": "MODERATE",
    "daily_hours_available": "3",
    "city": "Haifa",
}

# The declared widths from docs/MODEL_DATA.md section 2, which the form
# validation in app/domain/profile_rules.py does not currently know about.
COLUMN_WIDTHS = {
    "adopter_profiles.city": 100,
    "adopter_profiles.other_animals_description": 500,
    "users.full_name": 150,
    "adoption_applications.applicant_message": 2000,
}


def profile_form(**overrides: str) -> dict[str, str]:
    """A valid profile submission with the named fields replaced."""
    return {**VALID_PROFILE_FORM, **overrides}


class TestProfileFormRanges:
    """Numbers outside their documented CHECK constraints."""

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("daily_hours_available", "25"),
            ("daily_hours_available", "-1"),
            ("daily_hours_available", "1e400"),
            ("daily_hours_available", "not a number"),
            ("daily_hours_available", ""),
        ],
    )
    def test_an_out_of_range_hours_value_is_rejected_with_a_four_hundred(
        self, adopter_client: FlaskClient, field: str, value: str
    ) -> None:
        """Proves the DECIMAL CHECK BETWEEN 0 AND 24 is enforced before the insert."""
        response = adopter_client.post("/my/profile", data=profile_form(**{field: value}))

        assert response.status_code == 400

    @pytest.mark.parametrize("age", ["200", "-1", "19", "abc"])
    def test_an_implausible_child_age_is_rejected(
        self, adopter_client: FlaskClient, age: str
    ) -> None:
        """Proves the CHECK BETWEEN 0 AND 18 on youngest_child_age is enforced."""
        response = adopter_client.post(
            "/my/profile",
            data=profile_form(household_has_children="on", youngest_child_age=age),
        )

        assert response.status_code == 400

    @pytest.mark.parametrize("size", ["-5", "100001", "abc"])
    def test_an_impossible_yard_size_is_rejected(
        self, adopter_client: FlaskClient, size: str
    ) -> None:
        """Proves the yard size is validated, since the column has CHECK >= 0."""
        response = adopter_client.post(
            "/my/profile", data=profile_form(has_yard="on", yard_size_sqm=size)
        )

        assert response.status_code == 400

    @pytest.mark.parametrize("city", ["", " ", "x"])
    def test_an_empty_or_one_character_city_is_rejected(
        self, adopter_client: FlaskClient, city: str
    ) -> None:
        """Proves the NOT NULL city column cannot be filled with nothing."""
        response = adopter_client.post("/my/profile", data=profile_form(city=city))

        assert response.status_code == 400

    @pytest.mark.parametrize(
        "field", ["home_type", "experience_level", "activity_level"]
    )
    def test_a_value_outside_the_enum_is_rejected(
        self, adopter_client: FlaskClient, field: str
    ) -> None:
        """Proves a forged select value cannot violate the CHECK constraint."""
        response = adopter_client.post(
            "/my/profile", data=profile_form(**{field: "NOT_A_REAL_VALUE"})
        )

        assert response.status_code == 400

    def test_an_unknown_species_preference_is_rejected(
        self, adopter_client: FlaskClient
    ) -> None:
        """Proves a bad species is refused rather than silently stored."""
        response = adopter_client.post(
            "/my/profile", data={**VALID_PROFILE_FORM, "preferred_species": "DRAGON"}
        )

        assert response.status_code == 400

    def test_an_overlong_pet_description_is_rejected(
        self, adopter_client: FlaskClient
    ) -> None:
        """Proves the NVARCHAR(500) description column is protected by validation."""
        response = adopter_client.post(
            "/my/profile",
            data=profile_form(
                has_other_animals="on",
                other_animals_description="x"
                * (COLUMN_WIDTHS["adopter_profiles.other_animals_description"] + 1),
            ),
        )

        assert response.status_code == 400

    def test_every_error_is_reported_in_one_pass(
        self, adopter_client: FlaskClient
    ) -> None:
        """Proves a wrong form does not have to be fixed one field per reload."""
        response = adopter_client.post(
            "/my/profile",
            data={
                "home_type": "", "experience_level": "", "activity_level": "",
                "daily_hours_available": "99", "city": "",
            },
        )
        body = response.get_data(as_text=True)

        assert response.status_code == 400
        assert body.count("field-error") >= 4 or body.count("error") >= 4


class TestStringsLongerThanTheirColumn:
    """Validation must know the widths in docs/MODEL_DATA.md section 2."""

    def test_a_city_longer_than_the_column_is_rejected(
        self, adopter_client: FlaskClient
    ) -> None:
        """Proves city length is validated against NVARCHAR(100)."""
        response = adopter_client.post(
            "/my/profile",
            data=profile_form(city="x" * (COLUMN_WIDTHS["adopter_profiles.city"] + 1)),
        )

        assert response.status_code == 400

    def test_an_overlong_city_is_never_stored(
        self, adopter_client: FlaskClient, session_factory: sessionmaker[Session]
    ) -> None:
        """Proves the rejected value does not reach the column at all.

        A 400 that still wrote the row would be the worst outcome: SQLite
        stores 500 characters happily and SQL Server 2014 refuses them, so
        the bug would stay invisible until the cloud database saw it.
        """
        adopter_client.post("/my/profile", data=profile_form(city="x" * 500))

        with session_factory() as session:
            stored = session.execute(
                select(AdopterProfile.city).where(AdopterProfile.city.like("xxx%"))
            ).scalars().all()

        assert stored == []

    def test_a_registration_name_longer_than_the_column_is_rejected(
        self, client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves full_name length is validated against NVARCHAR(150)."""
        response = client.post(
            "/register",
            data={
                "full_name": "x" * (COLUMN_WIDTHS["users.full_name"] + 1),
                "email": "long.name@petmatch.test",
                "password": "Password123!",
                "confirm_password": "Password123!",
            },
        )

        assert response.status_code == 400

    def test_an_overlong_applicant_message_is_rejected(
        self, adopter_client: FlaskClient, world: dict[str, str],
        session_factory: sessionmaker[Session],
    ) -> None:
        """Proves applicant_message length is validated against NVARCHAR(2000)."""
        adopter_client.post(
            f"/my/apply/{world['available_animal_id']}",
            data={
                "applicant_message": "y"
                * (COLUMN_WIDTHS["adoption_applications.applicant_message"] + 1)
            },
        )

        with session_factory() as session:
            messages = session.execute(
                select(AdoptionApplication.applicant_message)
            ).scalars().all()

        assert all(
            message is None
            or len(message) <= COLUMN_WIDTHS["adoption_applications.applicant_message"]
            for message in messages
        )


class TestOutputEscaping:
    """NFR-4.4: user-supplied content MUST be escaped on output."""

    @pytest.mark.parametrize(
        "payload",
        [
            "<script>alert(1)</script>",
            "\"><script>alert(1)</script>",
            "<img src=x onerror=alert(1)>",
        ],
    )
    def test_a_script_payload_in_the_search_box_comes_back_escaped(
        self, client: FlaskClient, world: dict[str, str], payload: str
    ) -> None:
        """Proves the search term is reflected as text, not as markup.

        Jinja2 autoescaping is on and no template uses `|safe`, so this
        should hold - but the search term is reflected into the page on every
        request, which makes it the highest-traffic reflection point.
        """
        response = client.get("/animals/", query_string={"q": payload})
        body = response.get_data(as_text=True)

        assert response.status_code == 200
        assert payload not in body
        assert "<script>alert(1)</script>" not in body

    def test_a_javascript_url_is_never_reflected_into_a_link(
        self, client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves a `javascript:` term is echoed as text, never as an href.

        The term itself is harmless in a text node or an input value, which
        is where it does appear. What would matter is it reaching an
        attribute the browser navigates to.
        """
        body = client.get(
            "/animals/", query_string={"q": "javascript:alert(1)"}
        ).get_data(as_text=True)

        assert 'href="javascript:' not in body
        assert "href='javascript:" not in body

    def test_a_script_payload_stored_in_a_profile_is_escaped_when_shown(
        self, adopter_client: FlaskClient
    ) -> None:
        """Proves a stored payload is escaped on the way out, not only on input."""
        adopter_client.post(
            "/my/profile", data=profile_form(city="<script>alert('xss')</script>Haifa")
        )

        body = adopter_client.get("/my/profile").get_data(as_text=True)

        assert "<script>alert('xss')</script>" not in body

    def test_no_template_disables_autoescaping(self) -> None:
        """Proves the escaping guarantee is not undone somewhere in the views.

        A single `|safe` on a user-supplied value would defeat every test
        above, and it is a one-character change to make.
        """
        from pathlib import Path

        templates = Path("app/templates")
        offenders = [
            path.name
            for path in templates.rglob("*.html")
            if "|safe" in path.read_text(encoding="utf-8")
            or "autoescape false" in path.read_text(encoding="utf-8")
        ]

        assert offenders == []


class TestSearchQueryString:
    """Everything after the `?` is attacker-controlled."""

    @pytest.mark.parametrize("page", ["-1", "0", "abc", "", "1e9", "2147483648"])
    def test_a_nonsense_page_number_falls_back_instead_of_erroring(
        self, client: FlaskClient, world: dict[str, str], page: str
    ) -> None:
        """Proves pagination cannot be driven into a negative offset or a crash."""
        response = client.get("/animals/", query_string={"page": page})

        assert response.status_code == 200

    def test_an_astronomically_large_page_number_does_not_crash(
        self, client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves an unbounded page value is clamped rather than sent to the database."""
        response = client.get(
            "/animals/", query_string={"page": "99999999999999999999"}
        )

        assert response.status_code < 500

    def test_a_page_far_beyond_the_results_renders_an_empty_page(
        self, client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves a huge offset is answered, not refused."""
        response = client.get("/animals/", query_string={"page": "999999"})

        assert response.status_code == 200

    def test_a_five_thousand_character_search_term_is_handled(
        self, client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves a long term is a parameter, not a statement, and does not error."""
        response = client.get("/animals/", query_string={"q": "z" * 5000})

        assert response.status_code == 200

    @pytest.mark.parametrize(
        "injection",
        [
            "'; DROP TABLE animals; --",
            "' OR 1=1 --",
            "Rex') UNION SELECT password_hash FROM users --",
        ],
    )
    def test_a_sql_payload_in_the_search_term_is_treated_as_text(
        self, client: FlaskClient, world: dict[str, str],
        injection: str, session_factory: sessionmaker[Session],
    ) -> None:
        """Proves the search is parameterised: the tables survive and nothing leaks."""
        response = client.get("/animals/", query_string={"q": injection})

        body = response.get_data(as_text=True)

        assert response.status_code == 200
        with session_factory() as session:
            assert session.execute(select(User)).scalars().all() != []
        # The term itself is echoed into the filter box, so the words appear.
        # What must not appear is any actual credential material.
        assert "pbkdf2:" not in body
        assert "scrypt:" not in body

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("species", "NOT_A_SPECIES"),
            ("size", "ENORMOUS"),
            ("activity_level", "FRANTIC"),
            ("city", "'; --"),
        ],
    )
    def test_an_invalid_filter_value_returns_no_results_rather_than_erroring(
        self, client: FlaskClient, world: dict[str, str], field: str, value: str
    ) -> None:
        """Proves a forged filter narrows the search instead of breaking it."""
        response = client.get("/animals/", query_string={field: value})

        assert response.status_code == 200

    def test_a_percent_sign_is_searched_for_literally(
        self, client: FlaskClient, world: dict[str, str]
    ) -> None:
        """Proves LIKE metacharacters in a search term are escaped.

        The fixture animals are named Clover and Gus, neither of which
        contains a percent sign, so a literal search must find nothing.
        """
        response = client.get("/animals/", query_string={"q": "%"})
        body = response.get_data(as_text=True)

        assert "Clover" not in body


class TestNoLanguageModelInTheRequestPath:
    """NFR-3.1, on the one route that reaches for a model synchronously."""

    def test_an_empty_description_does_not_reach_a_model(
        self, client: FlaskClient, world: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Proves the blank case is short-circuited before any inference."""
        calls = _record_model_calls(monkeypatch)

        response = client.post("/search/describe", data={"description": "   "})

        assert response.status_code == 200
        assert calls == []

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "BUG: match_controller.natural_language_search "
            "(app/controllers/match_controller.py line 167-205) calls "
            "_build_interpreter().interpret(described) inside the request. "
            "NFR-3.1 and CLAUDE.md R4 both say no LLM call may occur in a "
            "request/response path, with measured CPU-only inference at "
            "11-16 s. The route is also open to anonymous visitors and applies "
            "no length cap, so an unauthenticated caller can pin a worker "
            "thread for ~16 s per request with an arbitrarily long prompt. "
            "AnalysisJobType.INTERPRET_INTENT already exists in the enum and is "
            "unused, which suggests this was meant to go through the job queue."
        ),
    )
    def test_describing_a_search_does_not_call_a_model_in_the_request(
        self, client: FlaskClient, world: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Proves no inference happens while an HTTP request is being served."""
        calls = _record_model_calls(monkeypatch)

        response = client.post(
            "/search/describe", data={"description": "a small calm rabbit"}
        )

        assert response.status_code == 200
        assert calls == []

    def test_an_unreachable_model_still_renders_the_search_page(
        self, client: FlaskClient, world: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Proves the route degrades to an explanation rather than a 500.

        Worth having whatever happens to NFR-3.1: with Ollama not running,
        which is the normal state on a machine that is only serving the web
        app, this route must not error.
        """
        _record_model_calls(monkeypatch)

        response = client.post(
            "/search/describe", data={"description": "a small calm rabbit"}
        )

        assert response.status_code == 200

    def test_the_ranking_screens_call_no_model(
        self, adopter_client: FlaskClient, staff_client: FlaskClient,
        world: dict[str, str], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Proves NFR-3.2: ranking is deterministic and synchronous, prose is not."""
        calls = _record_model_calls(monkeypatch)

        adopter_client.get("/my/matches")
        staff_client.get(f"/animals/{world['available_animal_id']}/adopters")
        staff_client.get(f"/animals/{world['available_animal_id']}/discover")

        assert calls == []


def _record_model_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the Ollama client with a recorder that reports itself unavailable.

    Returns the list the recorder appends to, so a test can assert that no
    inference was attempted while the request was being served. Nothing here
    contacts a model.
    """
    calls: list[str] = []

    def recording_complete_json(
        self: Any,  # noqa: ANN401 - stands in for OllamaLanguageModel
        system_prompt: str,
        user_prompt: str,
    ) -> dict[str, Any]:
        """Record the attempt, then behave as an unreachable model would."""
        from agent_service.llm_client import LanguageModelUnavailableError

        calls.append(user_prompt)
        raise LanguageModelUnavailableError("no model in tests")

    monkeypatch.setattr(
        "agent_service.llm_client.OllamaLanguageModel.complete_json",
        recording_complete_json,
    )
    return calls
