"""Static tests that enforce CLAUDE.md R2's layer-boundary table mechanically.

Every test here works from `ast` alone: no Flask app, no database session, no
application context is built anywhere in this file. That is the point of R2's
own domain rule stated as a test - "the domain layer must be unit-testable
with no Flask app context" - and this file holds itself to the same standard
while checking that everything else does too.

The rules under test are not reimplemented here. They live in
`scripts/verify_requirements.py`, which this file imports them from, so the
CLI checker and this suite can never end up enforcing two different versions
of the same rule. Each real-code test is paired with a synthetic negative
test proving the rule would actually catch the violation it claims to catch,
rather than passing by construction.

Two rules from the R2 table are deliberately not re-tested here:

* Jinja `|safe` usage is already exercised by
  `tests/api/test_qa_input_validation.py::TestOutputEscaping::
  test_no_template_disables_autoescaping`, which greps every template for
  `|safe` and `autoescape false`.
* Bare `print()` in application code is already a hard `ruff check` failure
  under rule T20 (see the comment in `scripts/verify_requirements.py` next to
  where that check used to live). A second AST rule proving the same thing
  would be exactly the kind of padding this project is trying to remove.
"""

from __future__ import annotations

import pytest
from scripts.verify_requirements import (
    agent_decision_violations,
    agent_isolation_violations,
    apply_rule,
    command_return_violations,
    controller_persistence_violations,
    domain_boundary_violations,
    query_write_violations,
    referenced_domain_event_types,
    unused_domain_event_types,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------- domain purity


def test_domain_layer_imports_no_framework_or_upper_layer() -> None:
    """Proves app/domain/ imports no framework or layer above it (CLAUDE.md R2)."""
    violations = apply_rule(domain_boundary_violations, "app/domain")
    assert violations == [], "; ".join(str(violation) for violation in violations)


def test_domain_boundary_rule_flags_a_framework_import() -> None:
    """Proves the rule itself catches a forbidden import rather than always passing."""
    violations = domain_boundary_violations("app/domain/fake.py", "from flask import Blueprint\n")
    assert len(violations) == 1
    assert "flask" in violations[0].message


def test_domain_boundary_rule_flags_every_forbidden_family() -> None:
    """Proves SQLAlchemy, app.controllers and app.cqrs are each independently caught."""
    source = (
        "import sqlalchemy\n"
        "import app.controllers.helpers\n"
        "from app.cqrs.base import MessageBus\n"
    )
    violations = domain_boundary_violations("app/domain/fake.py", source)
    assert len(violations) == 3


# ---------------------------------------------------------------- query purity


def test_query_modules_never_write() -> None:
    """Proves no module under app/cqrs/queries/ writes (CLAUDE.md R2)."""
    violations = apply_rule(query_write_violations, "app/cqrs/queries")
    assert violations == [], "; ".join(str(violation) for violation in violations)


def test_query_write_rule_flags_a_session_commit() -> None:
    """Proves the rule catches `session.commit()` inside a query handler."""
    source = "def handle(self, query):\n    session.commit()\n"
    violations = query_write_violations("app/cqrs/queries/fake.py", source)
    assert len(violations) == 1


def test_query_write_rule_flags_dml_execution_and_dml_import() -> None:
    """Proves `session.execute(update(...))` and a bare DML import are both caught."""
    source = (
        "from sqlalchemy import update\n"
        "def handle(self, query):\n"
        "    session.execute(update(Animal))\n"
    )
    violations = query_write_violations("app/cqrs/queries/fake.py", source)
    assert len(violations) == 2


def test_query_write_rule_flags_a_chained_mutation() -> None:
    """Proves `.delete()` chained onto a `select(...)` statement is caught."""
    source = "def handle(self, q):\n    select(Animal).where(Animal.id == x).delete()\n"
    violations = query_write_violations("app/cqrs/queries/fake.py", source)
    assert len(violations) == 1


# ----------------------------------------------------------- command returns


def test_command_handlers_never_return_read_data() -> None:
    """Proves every handle() under app/cqrs/commands/ returns str/int/None (R2)."""
    violations = apply_rule(command_return_violations, "app/cqrs/commands")
    assert violations == [], "; ".join(str(violation) for violation in violations)


def test_command_return_rule_flags_a_dataclass_return() -> None:
    """Proves the rule catches a handler annotated to return a read DTO."""
    source = (
        "class ApproveApplicationHandler(CommandHandler):\n"
        "    def handle(self, command: ApproveApplicationCommand) -> ApplicationSummary:\n"
        "        ...\n"
    )
    violations = command_return_violations("app/cqrs/commands/fake.py", source)
    assert len(violations) == 1
    assert "ApplicationSummary" in violations[0].message


def test_command_return_rule_flags_a_list_return() -> None:
    """Proves a list return - the shape a read query would use - is flagged too."""
    source = (
        "class ListingHandler(CommandHandler):\n"
        "    def handle(self, c: X) -> list[str]:\n"
        "        ...\n"
    )
    violations = command_return_violations("app/cqrs/commands/fake.py", source)
    assert len(violations) == 1


def test_command_return_rule_allows_every_sanctioned_shape() -> None:
    """Proves str, int, None, an absent annotation and `str | None` all pass."""
    source = (
        "class OneHandler(CommandHandler):\n"
        "    def handle(self, command: X) -> str: ...\n"
        "class TwoHandler(CommandHandler):\n"
        "    def handle(self, command: X) -> int: ...\n"
        "class ThreeHandler(CommandHandler):\n"
        "    def handle(self, command: X) -> None: ...\n"
        "class FourHandler(CommandHandler):\n"
        "    def handle(self, command: X):\n"
        "        pass\n"
        "class FiveHandler(CommandHandler):\n"
        "    def handle(self, command: X) -> str | None: ...\n"
    )
    violations = command_return_violations("app/cqrs/commands/fake.py", source)
    assert violations == []


# ------------------------------------------------------------- controllers


def test_controllers_never_hold_a_database_session() -> None:
    """Proves only app/controllers/helpers.py may touch SQLAlchemy (CLAUDE.md R2)."""
    violations = apply_rule(controller_persistence_violations, "app/controllers")
    assert violations == [], "; ".join(str(violation) for violation in violations)


def test_controller_persistence_rule_flags_a_synthetic_violation() -> None:
    """Proves a direct SQLAlchemy import and a `session.add(...)` call are both caught."""
    source = "import sqlalchemy\n\ndef view():\n    session.add(record)\n"
    violations = controller_persistence_violations("app/controllers/fake_controller.py", source)
    assert len(violations) == 2


def test_controller_persistence_rule_exempts_the_helpers_module() -> None:
    """Proves helpers.py may name SQLAlchemy types - it hands out the session factory."""
    source = "from sqlalchemy.orm import Session, sessionmaker\n"
    violations = controller_persistence_violations("app/controllers/helpers.py", source)
    assert violations == []


# ------------------------------------------------------ agent process isolation


def test_agent_and_mcp_modules_never_import_the_web_tier() -> None:
    """Proves agent_service/ and mcp_server/ never import the web tier (CLAUDE.md R2)."""
    violations = apply_rule(agent_isolation_violations, "agent_service", "mcp_server")
    assert violations == [], "; ".join(str(violation) for violation in violations)


def test_agent_isolation_rule_allows_the_framework_free_layers() -> None:
    """Proves app.domain/config/infrastructure/eventstore are allowed, not banned wholesale."""
    source = (
        "import app.domain.matching\n"
        "import app.config\n"
        "from app.infrastructure.models import Animal\n"
        "from app.eventstore.store import EventStore\n"
    )
    violations = agent_isolation_violations("agent_service/fake.py", source)
    assert violations == []


def test_agent_isolation_rule_flags_every_forbidden_layer() -> None:
    """Proves the Flask package and each forbidden layer are independently caught."""
    source = (
        "import app\n"
        "import app.controllers.helpers\n"
        "import app.cqrs.base\n"
        "import app.security.authorization\n"
        "import app.services\n"
    )
    violations = agent_isolation_violations("agent_service/fake.py", source)
    assert len(violations) == 5


# --------------------------------------------------------- agent cannot decide


def test_agent_never_names_a_decision_command() -> None:
    """Proves the agent never imports or names Approve/Reject/Decide (R4, spec 6.4)."""
    violations = apply_rule(agent_decision_violations, "agent_service", "mcp_server")
    assert violations == [], "; ".join(str(violation) for violation in violations)


def test_agent_decision_rule_flags_an_import_and_a_bare_reference() -> None:
    """Proves an imported decision command and a bare decision-handler name are both caught."""
    source = (
        "from app.cqrs.commands.application_commands import ApproveApplicationCommand\n"
        "\n"
        "def escalate() -> None:\n"
        "    handler = RejectApplicationHandler\n"
    )
    violations = agent_decision_violations("agent_service/fake.py", source)
    assert len(violations) == 2


# ------------------------------------------------------------- event catalogue


def test_every_domain_event_type_is_referenced_somewhere() -> None:
    """Proves every DomainEventType member is appended or read somewhere in the project."""
    unused = unused_domain_event_types()
    assert unused == [], f"unused DomainEventType members: {unused}"


def test_event_reference_scan_does_not_invent_a_reference() -> None:
    """Proves a name that exists nowhere in the project is absent from the referenced set."""
    referenced = referenced_domain_event_types()
    assert "THIS_EVENT_TYPE_DOES_NOT_EXIST_ANYWHERE" not in referenced
