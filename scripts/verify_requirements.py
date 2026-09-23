r"""Verify every mandatory requirement by inspecting behaviour, not strings.

The previous version of this script answered most questions with
`"substring" in source`, which is why it reported 25/25 while a manual audit
found real gaps: it counted four form *templates* as blueprint 4.5, missed
every `async def test_`, and "proved" the agent cannot decide by grepping for
`def approve`.

Every check here instead runs the code (the Flask app is built exactly as
`tests/api/conftest.py` builds it, against a throwaway SQLite file, never
touching Somee.com or Ollama), parses the code with `ast`, or calls a pure
function directly. A PASS states what was inspected; a FAIL names the file to
open.

The static-analysis helpers below are shared with
`tests/unit/test_architecture_guard.py` and
`tests/unit/test_docs_reference_real_artifacts.py`, which import them from
here so the two copies of each rule can never drift apart.

Usage:
    <venv>\\Scripts\\python.exe scripts\\verify_requirements.py
"""

from __future__ import annotations

import ast
import builtins
import dataclasses
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.config import Configuration
    from flask import Flask

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Generic static-analysis helpers, shared with tests/unit/.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, order=True)
class Violation:
    """One rule breach, precise enough to fix: `location:line -> message`."""

    location: str
    line: int
    message: str

    def __str__(self) -> str:
        """Render as `path:line -> message`, the form an editor can jump to."""
        return f"{self.location}:{self.line} -> {self.message}"


#: A rule that inspects one module's `(label, source)` and reports what it
#: found wrong. Taking source rather than a path is what makes a rule
#: testable against a synthetic module that never has to exist on disk.
ModuleRule = Callable[[str, str], "list[Violation]"]


def project_relative(path: Path) -> str:
    """Return `path` relative to the project root, forward-slashed."""
    return path.resolve().relative_to(PROJECT_ROOT).as_posix()


def python_files_under(*relative_directories: str) -> list[Path]:
    """Return every `.py` file under the given project-relative directories.

    A missing directory contributes nothing rather than raising, so a rule
    can name one that is legitimately absent.
    """
    found: list[Path] = []
    for directory in relative_directories:
        root = PROJECT_ROOT / directory
        if root.is_dir():
            found.extend(root.rglob("*.py"))
        elif root.is_file():
            found.append(root)
    return sorted(found)


def apply_rule(rule: ModuleRule, *relative_directories: str) -> list[Violation]:
    """Run one module rule over every Python file under the directories."""
    findings: list[Violation] = []
    for path in python_files_under(*relative_directories):
        source = path.read_text(encoding="utf-8")
        findings.extend(rule(project_relative(path), source))
    return sorted(findings)


def dotted_name(node: ast.expr) -> str:
    """Return the dotted name of a Name/Attribute expression, else `""`.

    `session.execute` yields `"session.execute"`; anything built from a call
    or subscript yields `""` - there is no stable name to report.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else ""
    return ""


def parse_source(label: str, source: str) -> ast.Module:
    """Parse a module, attributing a syntax error to `label`."""
    return ast.parse(source, filename=label)


def imported_modules(tree: ast.Module) -> list[tuple[str, int]]:
    """Return every absolutely-imported module path with its line number.

    Relative imports are skipped: they cannot cross a layer boundary.
    """
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.append((node.module, node.lineno))
    return found


def imported_symbols(tree: ast.Module) -> list[tuple[str, str, int]]:
    """Return `(module, symbol, line)` for every `from X import Y`."""
    found: list[tuple[str, str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.extend((node.module, alias.name, node.lineno) for alias in node.names)
    return found


def attribute_calls(tree: ast.Module) -> list[tuple[str, str, int]]:
    """Return `(receiver, attribute, line)` for every `receiver.attribute(...)`.

    The receiver is `""` when the call is chained onto an expression, such as
    `select(Animal).where(...)`.
    """
    return [
        (dotted_name(node.func.value), node.func.attr, node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]


def class_definitions(tree: ast.Module) -> list[ast.ClassDef]:
    """Return every class defined anywhere in the module."""
    return [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]


def decorator_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """Return each decorator's dotted name, ignoring its call arguments.

    `@animal_blueprint.route("/x")` yields `"animal_blueprint.route"`.
    """
    names: list[str] = []
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        name = dotted_name(target)
        if name:
            names.append(name)
    return names


def functions_in(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Return every function and coroutine defined anywhere in the module."""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]


# ---------------------------------------------------------------------------
# Rule R2 - layer boundaries. One function per row of the CLAUDE.md table.
# ---------------------------------------------------------------------------

#: Frameworks and upper layers `app/domain/` may never import (CLAUDE.md R2).
DOMAIN_FORBIDDEN_IMPORTS = (
    "flask", "sqlalchemy", "werkzeug", "jinja2",
    "app.infrastructure", "app.cqrs", "app.controllers",
)

#: Mutating session methods. A query handler calling any of these has opened
#: a write path, whether or not the transaction is later rolled back.
SESSION_WRITE_METHODS = frozenset(
    {"add", "add_all", "delete", "commit", "flush", "merge", "bulk_save_objects"}
)

#: SQLAlchemy DML constructors - there is no read-only use for them in a query module.
DML_CONSTRUCTORS = frozenset({"insert", "update", "delete"})

#: What `CommandHandler.handle` may be annotated to return (CLAUDE.md R2): an
#: identifier, a count, or nothing. `int` covers handlers that return a count
#: of rows changed, which is a write result, not read data.
ALLOWED_COMMAND_RETURNS = frozenset({"str", "int", "None"})

#: The one controller module allowed to name SQLAlchemy types: it hands out
#: the session factory for the one authentication concern controllers own.
CONTROLLER_PERSISTENCE_ALLOWLIST = frozenset({"app/controllers/helpers.py"})

#: The agent is a separate OS process (CLAUDE.md R2); it may share only the
#: framework-free layers.
AGENT_FORBIDDEN_APP_IMPORTS = (
    "app.controllers", "app.cqrs", "app.security", "app.services",
)

#: Names that would mean the agent had reached for a decision command (R4).
DECISION_NAME_PATTERN = re.compile(r"^(Approve|Reject|Decide)\w*(Command|Handler)$")


def domain_boundary_violations(label: str, source: str) -> list[Violation]:
    """Report framework or upper-layer imports inside the domain layer (R2).

    The domain may import stdlib and other domain modules only, so a domain
    test never needs an application context.
    """
    tree = parse_source(label, source)
    return [
        Violation(
            label, line,
            f"domain module imports `{module}`; the domain layer must be "
            f"framework-free and must not depend on a layer above it",
        )
        for module, line in imported_modules(tree)
        for forbidden in DOMAIN_FORBIDDEN_IMPORTS
        if module == forbidden or module.startswith(f"{forbidden}.")
    ]


def _session_write_calls(label: str, tree: ast.Module) -> list[Violation]:
    """Report mutating method calls on anything named like a session."""
    return [
        Violation(
            label, line,
            f"query handler calls `{receiver}.{attribute}(...)`; a query "
            f"must never open a write path",
        )
        for receiver, attribute, line in attribute_calls(tree)
        if attribute in SESSION_WRITE_METHODS
        and (receiver == "session" or receiver.endswith(("_session", ".session")))
    ]


def _dml_execute_calls(label: str, tree: ast.Module) -> list[Violation]:
    """Report `session.execute(insert(...)/update(...)/delete(...))`."""
    findings: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or dotted_name(node.func) != "session.execute":
            continue
        for argument in node.args:
            constructor = dotted_name(argument.func) if isinstance(argument, ast.Call) else ""
            if constructor in DML_CONSTRUCTORS:
                findings.append(
                    Violation(
                        label, node.lineno,
                        f"query handler executes `{constructor}(...)`; a "
                        f"query must never issue DML",
                    )
                )
    return findings


def _chained_mutation_calls(label: str, tree: ast.Module) -> list[Violation]:
    """Report `.update(...)`/`.delete(...)` chained onto a query object.

    Restricted to receivers built from a call or named like a statement, so
    `.update(...)` on a plain dictionary is not mistaken for a query mutation.
    """
    findings: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in {"update", "delete"}:
            continue
        receiver = node.func.value
        receiver_name = dotted_name(receiver)
        looks_like_a_statement = isinstance(receiver, ast.Call) or any(
            marker in receiver_name.lower() for marker in ("query", "statement", "stmt", "select")
        )
        if looks_like_a_statement:
            findings.append(
                Violation(
                    label, node.lineno,
                    f"query handler calls `.{node.func.attr}(...)` on a query "
                    f"object; a query must never mutate state",
                )
            )
    return findings


def _dml_imports(label: str, tree: ast.Module) -> list[Violation]:
    """Report `from sqlalchemy import insert/update/delete` in a query module."""
    return [
        Violation(
            label, line,
            f"query module imports `{symbol}` from `{module}`; there is no "
            f"read-only use for a DML constructor",
        )
        for module, symbol, line in imported_symbols(tree)
        if module.startswith("sqlalchemy") and symbol in DML_CONSTRUCTORS
    ]


def query_write_violations(label: str, source: str) -> list[Violation]:
    """Report any write performed by a query module (CLAUDE.md R2).

    The bus rolls a query session back, which makes an accidental write
    unpersistable but not absent; this rule catches the attempt itself.
    """
    tree = parse_source(label, source)
    return [
        *_session_write_calls(label, tree),
        *_dml_execute_calls(label, tree),
        *_chained_mutation_calls(label, tree),
        *_dml_imports(label, tree),
    ]


def _return_annotation_is_allowed(annotation: ast.expr | None) -> bool:
    """Whether a `handle` return annotation is an identifier, count or None."""
    if annotation is None:
        return True
    if isinstance(annotation, ast.Constant) and annotation.value is None:
        return True
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return _return_annotation_is_allowed(
            annotation.left
        ) and _return_annotation_is_allowed(annotation.right)
    return dotted_name(annotation) in ALLOWED_COMMAND_RETURNS


def command_return_violations(label: str, source: str) -> list[Violation]:
    """Report a command handler annotated to return read data (CLAUDE.md R2).

    A command returns an identifier or nothing; if a screen needs data after
    a write, the controller dispatches a query next.
    """
    tree = parse_source(label, source)
    findings: list[Violation] = []
    for class_definition in class_definitions(tree):
        bases = class_definition.bases
        if not any(dotted_name(base).endswith("CommandHandler") for base in bases):
            continue
        for member in class_definition.body:
            if not isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if member.name != "handle" or _return_annotation_is_allowed(member.returns):
                continue
            rendered = ast.unparse(member.returns) if member.returns else "None"
            findings.append(
                Violation(
                    label, member.lineno,
                    f"{class_definition.name}.handle returns `{rendered}`; a "
                    f"command returns str, int or None, never a read DTO",
                )
            )
    return findings


def controller_persistence_violations(label: str, source: str) -> list[Violation]:
    """Report a controller reaching past `helpers.py` to the database.

    `app/controllers/helpers.py` is the one sanctioned surface: it hands out
    the bus, the configuration and the session factory for the one
    authentication concern controllers legitimately own.
    """
    if label in CONTROLLER_PERSISTENCE_ALLOWLIST:
        return []

    tree = parse_source(label, source)
    findings = [
        Violation(
            label, line,
            f"controller imports `{module}` directly; persistence belongs "
            f"behind app/controllers/helpers.py",
        )
        for module, line in imported_modules(tree)
        if module == "sqlalchemy" or module.startswith("sqlalchemy.")
    ]
    findings.extend(
        Violation(
            label, node.lineno,
            f"controller uses `session.{node.attr}`; a controller dispatches "
            f"on the bus and never holds a session",
        )
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and dotted_name(node.value) == "session"
    )
    return findings


def agent_isolation_violations(label: str, source: str) -> list[Violation]:
    """Report the agent process importing the web tier (CLAUDE.md R2).

    `app.domain`, `app.config`, `app.infrastructure` and `app.eventstore` are
    shared deliberately: they are framework-free or pure configuration.
    """
    tree = parse_source(label, source)
    findings: list[Violation] = []
    for module, line in imported_modules(tree):
        if module == "app":
            findings.append(
                Violation(
                    label, line,
                    "imports the Flask application package; the agent is a "
                    "separate OS process and talks through analysis_jobs and MCP",
                )
            )
            continue
        for forbidden in AGENT_FORBIDDEN_APP_IMPORTS:
            if module == forbidden or module.startswith(f"{forbidden}."):
                findings.append(
                    Violation(
                        label, line,
                        f"imports `{module}`; the agent may share only "
                        f"app.domain, app.config, app.infrastructure and "
                        f"app.eventstore",
                    )
                )
    return findings


def agent_decision_violations(label: str, source: str) -> list[Violation]:
    """Report the agent naming a decision command (CLAUDE.md R4, spec 6.4).

    The agent scores, explains and recommends; a human staff member decides.
    """
    tree = parse_source(label, source)
    findings = [
        Violation(
            label, line,
            f"imports `{symbol}` from `{module}`; the agent never makes the "
            f"final adoption decision",
        )
        for module, symbol, line in imported_symbols(tree)
        if module.startswith("app.cqrs") and DECISION_NAME_PATTERN.match(symbol)
    ]
    findings.extend(
        Violation(
            label, node.lineno,
            f"references `{node.id}`; the agent never makes the final "
            f"adoption decision",
        )
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and DECISION_NAME_PATTERN.match(node.id)
    )
    return findings


#: No `stdout_print_violations` rule lives here: ruff's T20 (a mandatory gate
#: for the whole tree) already bans print() outright, and the single
#: sanctioned exception - mcp_server/server.py's stderr diagnostic line - is
#: already annotated to silence that rule there. A second AST rule reproving
#: what the gate already enforces would be the kind of padding this rewrite
#: exists to remove.


def declared_domain_event_types() -> list[str]:
    """Return the member names of `DomainEventType`, in declaration order."""
    enums_path = PROJECT_ROOT / "app" / "domain" / "enums.py"
    tree = parse_source("app/domain/enums.py", enums_path.read_text(encoding="utf-8"))
    for class_definition in class_definitions(tree):
        if class_definition.name != "DomainEventType":
            continue
        return [
            target.id
            for member in class_definition.body
            if isinstance(member, ast.Assign)
            for target in member.targets
            if isinstance(target, ast.Name)
        ]
    return []


def referenced_domain_event_types() -> set[str]:
    """Return every `DomainEventType.X` member named anywhere in the project."""
    referenced: set[str] = set()
    for path in python_files_under("app", "agent_service", "scripts", "tests"):
        tree = parse_source(project_relative(path), path.read_text(encoding="utf-8"))
        referenced.update(
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and dotted_name(node.value) == "DomainEventType"
        )
    return referenced


def unused_domain_event_types() -> list[str]:
    """Return declared event types that no code ever appends or reads.

    A member nothing references is a dead entry in the event catalogue,
    which makes the documented catalogue wider than the behaviour.
    """
    referenced = referenced_domain_event_types()
    return [name for name in declared_domain_event_types() if name not in referenced]


# ---------------------------------------------------------------------------
# The documentation reference scanner, shared with tests/unit/.
# ---------------------------------------------------------------------------

#: Anything inside single backticks, one line at a time.
_BACKTICKED = re.compile(r"`([^`\n]+)`")

_TEST_NAME = re.compile(r"^test_[a-z0-9_]+$")
_PROJECT_PATH = re.compile(
    r"^(app|agent_service|mcp_server|tests|scripts|knowledge|docs|alembic|\.claude)"
    r"/[\w./-]*$"
)
_CODE_IDENTIFIER = re.compile(r"^[A-Z][A-Za-z0-9]*(Command|Query|Handler|Error)$")

#: References that look dead to the scanner but are not documentation
#: defects. Each entry states why, because an unexplained allowlist is how a
#: guard quietly stops guarding.
DOCUMENTATION_REFERENCE_ALLOWLIST = frozenset(
    {
        # Third-party exceptions the documents legitimately name; not defined
        # under app/ and not builtins either.
        "SQLAlchemyError", "CSRFError", "IntegrityError", "OperationalError",
        # CQRS *concepts* written in backticks as prose, not a claim that a
        # class literally named "Commands" or "Queries" exists.
        "Commands", "Queries",
        # A shell-example path, not a claim the directory is tracked.
        "scripts/__pycache__",
    }
)


@dataclass(frozen=True, order=True)
class DeadReference:
    """A documentation reference to something that does not exist."""

    document: str
    line: int
    reference: str
    kind: str

    def __str__(self) -> str:
        """Render as `path:line -> missing <kind> `<reference>``."""
        return f"{self.document}:{self.line} -> missing {self.kind} `{self.reference}`"


def documentation_files() -> list[Path]:
    """Return every document whose references are checked.

    The eleven mandated documents under `docs/`, plus PLANNING.md, README.md
    and CLAUDE.md, which also make claims about the code.
    """
    files = sorted((PROJECT_ROOT / "docs").rglob("*.md"))
    files.extend(
        PROJECT_ROOT / name
        for name in ("PLANNING.md", "README.md", "CLAUDE.md")
        if (PROJECT_ROOT / name).is_file()
    )
    return files


def collected_test_names() -> set[str]:
    """Return every test function name pytest would collect.

    Parsed rather than run, so this works even when a suite cannot be
    imported (E2E needs a browser), and `async def test_...` counts - a
    `^def test_` grep silently drops it.
    """
    names: set[str] = set()
    for path in sorted((PROJECT_ROOT / "tests").rglob("test_*.py")):
        tree = parse_source(project_relative(path), path.read_text(encoding="utf-8"))
        names.update(
            function.name for function in functions_in(tree)
            if function.name.startswith("test_")
        )
    return names


def defined_class_names() -> set[str]:
    """Return every class defined under `app/`, `agent_service/` or `mcp_server/`."""
    names: set[str] = set()
    for path in python_files_under("app", "agent_service", "mcp_server"):
        tree = parse_source(project_relative(path), path.read_text(encoding="utf-8"))
        names.update(class_definition.name for class_definition in class_definitions(tree))
    return names


def _classify_reference(reference: str, tests: set[str], classes: set[str]) -> str | None:
    """Return `"test"`, `"path"` or `"identifier"` for a dead reference, else None."""
    if reference in DOCUMENTATION_REFERENCE_ALLOWLIST:
        return None
    if _TEST_NAME.match(reference):
        return None if reference in tests else "test"
    if _PROJECT_PATH.match(reference):
        return None if (PROJECT_ROOT / reference).exists() else "path"
    if _CODE_IDENTIFIER.match(reference):
        if reference in classes or hasattr(builtins, reference):
            return None
        return "identifier"
    return None


def find_dead_references() -> list[DeadReference]:
    """Return every documentation reference to a non-existent artifact.

    CLAUDE.md R5 and blueprint 18: code and `docs/` may never contradict.
    Three claims are machine-checkable: a named test must be collectible, a
    backticked project path must exist, and a backticked
    `SomethingCommand`/`Query`/`Handler`/`Error` must be a real class.
    """
    tests = collected_test_names()
    classes = defined_class_names()

    dead: list[DeadReference] = []
    for path in documentation_files():
        document = project_relative(path)
        lines = path.read_text(encoding="utf-8").splitlines()
        for number, text in enumerate(lines, 1):
            for reference in _BACKTICKED.findall(text):
                kind = _classify_reference(reference.strip(), tests, classes)
                if kind is not None:
                    dead.append(DeadReference(document, number, reference.strip(), kind))
    return sorted(dead)


# ---------------------------------------------------------------------------
# The application under verification.
# ---------------------------------------------------------------------------

_BUILT_APPLICATION: list[Flask] = []


def _verification_configuration(scratch_directory: Path) -> Configuration:
    """Build a configuration pointed entirely at throwaway local resources.

    Built by hand rather than loaded from `.env`, so verification works on a
    machine with no cloud credentials and can never write to the shared
    database.
    """
    from app.config import AgentConfiguration, Configuration, DatabaseConfiguration

    return Configuration(
        database=DatabaseConfiguration(
            server="unused", database="unused", user="unused", password="unused"
        ),
        agent=AgentConfiguration(
            ollama_base_url="http://localhost:11434",
            chat_model="stub",
            fast_chat_model="stub",
            embedding_model="stub",
            tavily_api_key="",
            poll_interval_seconds=3,
            max_reasoning_steps=8,
        ),
        secret_key="verification-secret-key",
        flask_port=5000,
        is_development=False,
        chroma_persist_directory=scratch_directory / "chroma",
        rag_collection_name="verification",
        upload_directory=scratch_directory / "uploads",
        invitation_expiry_hours=72,
    )


def application_under_verification() -> Flask:
    """Build the real Flask application once, against a throwaway SQLite file.

    Mirrors `tests/api/conftest.py`: `LOCAL_DATABASE_URL` sends the engine to
    SQLite, the schema is created from the ORM metadata, and CSRF is
    disabled because no check here posts a form. Cached so every check
    shares one build.
    """
    if _BUILT_APPLICATION:
        return _BUILT_APPLICATION[0]

    import os

    from app import create_app
    from app.infrastructure.database import Base
    from sqlalchemy import create_engine

    scratch_directory = Path(tempfile.mkdtemp(prefix="petmatch-verify-"))
    database_url = f"sqlite:///{(scratch_directory / 'verify.db').as_posix()}"
    os.environ["LOCAL_DATABASE_URL"] = database_url

    engine = create_engine(database_url, future=True)
    Base.metadata.create_all(engine)
    engine.dispose()

    application = create_app(_verification_configuration(scratch_directory))
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False
    _BUILT_APPLICATION.append(application)
    return application


def _built_application_object() -> object:
    """Return the built application typed as `object`.

    `check_flask` asks whether `create_app()` really returned a `Flask`
    instance; typing this loosely (rather than as `Flask`) is what keeps
    that a dynamic question instead of one mypy would answer from the
    annotation alone.
    """
    return application_under_verification()


def _routes() -> dict[str, frozenset[str]]:
    """Return every registered URL rule mapped to its HTTP methods."""
    return {
        rule.rule: frozenset(rule.methods or ())
        for rule in application_under_verification().url_map.iter_rules()
    }


def _normalise_route(rule: str) -> str:
    """Collapse URL parameters so a document and Flask can be compared.

    `/animals/<animal_id>/adopters` and a document's `/animals/<id>/adopters`
    are the same route; only the parameter name differs.
    """
    return re.sub(r"<[^>]+>", "<>", rule.rstrip("/") or "/")


def _read(relative_path: str) -> str:
    """Read a project file, or return `""` when it is absent."""
    path = PROJECT_ROOT / relative_path
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _verdict(problems: Iterable[str], success_detail: str) -> tuple[bool, str]:
    """Turn a list of problems into a check result.

    `success_detail` states what was inspected, so a PASS can be audited.
    """
    listed = list(problems)
    if listed:
        return False, "; ".join(listed)
    return True, success_detail


# ---------------------------------------------------------------------------
# The checks.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Check:
    """One checklist item and how it is verified."""

    requirement: str
    verify: Callable[[], tuple[bool, str]]


def check_flask() -> tuple[bool, str]:
    """Blueprint 9: the system is built on Flask, proven by building it."""
    from flask import Flask

    application = _built_application_object()
    if not isinstance(application, Flask):
        return False, f"create_app() returned {type(application).__name__}"
    route_count = len(list(application.url_map.iter_rules()))
    return True, f"create_app() returned a Flask instance with {route_count} registered routes"


def check_authentication() -> tuple[bool, str]:
    """Blueprint 4.2: authentication, sign-out POST-only, a real hash check."""
    routes = _routes()
    problems = [
        f"no {path} route" for path in ("/register", "/login", "/logout") if path not in routes
    ]

    logout_methods = routes.get("/logout", frozenset())
    if "GET" in logout_methods:
        problems.append("/logout accepts GET; sign-out must be POST-only")

    tree = parse_source("auth", _read("app/controllers/auth_controller.py"))
    hash_checks = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and dotted_name(node.func) == "check_password_hash"
    ]
    imports_werkzeug = any(
        module == "werkzeug.security" and symbol == "check_password_hash"
        for module, symbol, _ in imported_symbols(tree)
    )
    if not hash_checks or not imports_werkzeug:
        problems.append("the login view does not call werkzeug check_password_hash")

    return _verdict(
        problems,
        f"/register, /login and /logout registered; /logout is "
        f"{sorted(logout_methods - {'HEAD', 'OPTIONS'})}; login calls "
        f"werkzeug check_password_hash at line "
        f"{hash_checks[0].lineno if hash_checks else 0}",
    )


#: docs/UX.md's permission table has this many `|`-delimited cells per row.
PERMISSION_TABLE_COLUMN_COUNT = 5


def _staff_only_routes_from_ux_document() -> list[str]:
    """Return the routes docs/UX.md marks staff-only, from its own table."""
    routes: list[str] = []
    for line in _read("docs/UX.md").splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != PERMISSION_TABLE_COLUMN_COUNT:
            continue
        _, route, _, adopter, staff = cells
        if "403" in adopter and staff.startswith("yes"):
            routes.append(_normalise_route(route.strip("`")))
    return routes


def _view_function_name(route: str) -> str:
    """Return the view function name serving a normalised route, or `""`."""
    application = application_under_verification()
    for rule in application.url_map.iter_rules():
        if _normalise_route(rule.rule) == route:
            view = application.view_functions[rule.endpoint]
            return str(getattr(view, "__name__", ""))
    return ""


def _controller_decorators() -> dict[str, list[str]]:
    """Return each controller view function's decorator names."""
    decorated: dict[str, list[str]] = {}
    for path in python_files_under("app/controllers"):
        tree = parse_source(project_relative(path), path.read_text(encoding="utf-8"))
        for function in functions_in(tree):
            decorated[function.name] = decorator_names(function)
    return decorated


def check_two_roles() -> tuple[bool, str]:
    """Blueprint 4.3 and 12: two roles, staff-only routes enforced server-side.

    Reads every staff-only row out of docs/UX.md's own permission table and
    proves the view serving it carries `@require_staff()` - hiding a button
    is not enough.
    """
    from app.domain.enums import UserRole

    members = {member.name for member in UserRole}
    problems = (
        []
        if members == {"ADOPTER", "STAFF"}
        else [f"UserRole is {sorted(members)}, expected exactly ADOPTER and STAFF"]
    )

    decorated = _controller_decorators()
    staff_routes = _staff_only_routes_from_ux_document()
    for route in staff_routes:
        view_name = _view_function_name(route)
        if not view_name:
            problems.append(f"docs/UX.md marks {route} staff-only but no route serves it")
        elif "require_staff" not in decorated.get(view_name, []):
            problems.append(f"{route} ({view_name}) is not decorated with require_staff")

    return _verdict(
        problems,
        f"UserRole is exactly ADOPTER and STAFF; all {len(staff_routes)} "
        f"staff-only routes in docs/UX.md carry @require_staff()",
    )


#: Spec 6.2's filters, mapped to the field name(s) the query dataclass would carry it under.
REQUIRED_SEARCH_FILTERS = {
    "species": ("species",),
    "size": ("size",),
    "activity level": ("activity_level",),
    "age": ("age", "age_band", "age_range", "max_age_years", "min_age_years", "age_years"),
    "good with children": ("good_with_children",),
    "good with other animals": ("good_with_other_animals",),
}


def check_search() -> tuple[bool, str]:
    """Blueprint 4.1 and spec 6.2/6.3: structured and natural-language search.

    Inspects the filter dataclass's declared fields, so a filter shown in the
    form but dropped before the query is not counted.
    """
    from app.cqrs.queries.animal_queries import AnimalSearchFilters

    declared = {field.name for field in dataclasses.fields(AnimalSearchFilters)}
    problems = [
        f"the search filters carry no {wording} field"
        for wording, candidates in REQUIRED_SEARCH_FILTERS.items()
        if not declared.intersection(candidates)
    ]

    routes = _routes()
    if "/animals/" not in routes:
        problems.append("no /animals/ search route")
    if "/search/describe" not in routes:
        problems.append("no natural-language search route")

    return _verdict(
        problems,
        f"AnimalSearchFilters declares {len(declared)} fields covering every "
        f"spec 6.2 filter; /animals/ and /search/describe both registered",
    )


def check_details_view() -> tuple[bool, str]:
    """Blueprint 4.2 and spec 24: a details screen that shows the mandatory image."""
    problems: list[str] = []
    if "/animals/<>" not in {_normalise_route(rule) for rule in _routes()}:
        problems.append("no /animals/<animal_id> route")

    template = _read("app/templates/animals/details.html")
    image_count = len(re.findall(r"<img\b", template))
    if not image_count:
        problems.append("animals/details.html renders no <img> element")

    return _verdict(
        problems,
        f"/animals/<animal_id> registered; details.html renders "
        f"{image_count} <img> element(s)",
    )


MINIMUM_TABLE_HEADINGS = 6


def check_tabular_display() -> tuple[bool, str]:
    """Blueprint 4.3 and 13: a real table, not a layout `<table>` with no headings."""
    template = _read("app/templates/animals/manage.html")
    headings = len(re.findall(r"<th\b", template))
    problems: list[str] = []
    if "<table" not in template:
        problems.append("animals/manage.html has no <table>")
    if "<thead" not in template:
        problems.append("animals/manage.html has no <thead>")
    if headings < MINIMUM_TABLE_HEADINGS:
        problems.append(
            f"animals/manage.html has {headings} <th> cells, fewer than "
            f"{MINIMUM_TABLE_HEADINGS}"
        )
    return _verdict(
        problems,
        f"animals/manage.html is a <table> with <thead> and {headings} <th> cells",
    )


#: Spec 22's figures, mapped to the field name(s) that would carry them.
REQUIRED_DASHBOARD_FIGURES = {
    "available animals": ("available_animals",),
    "pending applications": ("pending_applications",),
    "applications needing attention": ("applications_under_review", "stale_applications"),
    "open invitations": ("open_invitations",),
    "expired invitations": ("expired_invitations",),
    "animals with no suitable applicants": ("animals_without_suitable_applicants",),
    "animals with no applicants": ("animals_without_applicants",),
    "recent activity": ("recent_activity",),
}


def check_dashboard() -> tuple[bool, str]:
    """Blueprint 4.4 and spec 22: the read model carries all eight figures.

    Checks the DTO rather than the page, and requires a Needs Attention
    section backed by a real list rather than by prose.
    """
    from app.cqrs.queries.dashboard_queries import DashboardSummary

    declared = {field.name for field in dataclasses.fields(DashboardSummary)}
    problems = [
        f"the dashboard summary carries no {figure} figure"
        for figure, candidates in REQUIRED_DASHBOARD_FIGURES.items()
        if not declared.intersection(candidates)
    ]
    if "attention_items" not in declared:
        problems.append("the dashboard summary carries no attention_items list")
    if "needs attention" not in _read("app/templates/dashboard.html").lower():
        problems.append("dashboard.html renders no Needs Attention section")

    return _verdict(
        problems,
        f"DashboardSummary declares {len(declared)} fields covering all "
        f"{len(REQUIRED_DASHBOARD_FIGURES)} spec 22 figures plus "
        f"attention_items; dashboard.html renders Needs Attention",
    )


#: Spec 21's seven forms, mapped to the Flask endpoint(s) their
#: `<form method="post">` must target.
REQUIRED_FORM_ENDPOINTS = {
    "adopter registration": ("auth.register",),
    "adopter profile": ("personal.my_profile",),
    "animal create/edit": ("animals.new_animal", "animals.edit_animal"),
    "animal availability/status": ("animals.change_animal_status",),
    "adoption application": ("personal.apply_to_animal",),
    "invitation response": ("personal.respond_to_invitation",),
    "staff action/decision": ("matches.decide_application",),
}

MINIMUM_BUSINESS_FORMS = 7

_POST_FORM_TAG = re.compile(r"<form\b[^>]*>", re.IGNORECASE | re.DOTALL)
_URL_FOR_ENDPOINT = re.compile(r"url_for\(\s*['\"]([\w.]+)['\"]")


def post_form_endpoints() -> dict[str, list[str]]:
    """Return every endpoint a real `<form method="post">` targets, with where.

    A conditional `action` contributes every endpoint it can resolve to,
    which is how a shared create/edit form is counted as the two forms it is.
    """
    endpoints: dict[str, set[str]] = {}
    for path in sorted((PROJECT_ROOT / "app" / "templates").rglob("*.html")):
        template = path.read_text(encoding="utf-8")
        for tag in _POST_FORM_TAG.findall(template):
            if 'method="post"' not in tag.lower():
                continue
            for endpoint in _URL_FOR_ENDPOINT.findall(tag):
                endpoints.setdefault(endpoint, set()).add(project_relative(path))
    return {name: sorted(paths) for name, paths in sorted(endpoints.items())}


def check_data_entry() -> tuple[bool, str]:
    """Blueprint 4.5 and spec 21: seven meaningful business forms.

    Counts real `<form method="post">` elements by distinct action endpoint,
    not template files - the previous checker counted four templates and passed.
    """
    posted = post_form_endpoints()
    missing = [
        name
        for name, candidates in REQUIRED_FORM_ENDPOINTS.items()
        if not set(candidates).intersection(posted)
    ]
    problems = [f"no form posts to {name}" for name in missing]
    if len(posted) < MINIMUM_BUSINESS_FORMS:
        problems.append(
            f"only {len(posted)} distinct POST targets, fewer than {MINIMUM_BUSINESS_FORMS}"
        )
    return _verdict(
        problems,
        f"{len(posted)} distinct POST form targets; all seven spec 21 forms "
        f"present: {', '.join(REQUIRED_FORM_ENDPOINTS)}",
    )


def _loop_encloses_model_turns() -> bool:
    """Whether agent_service/loop.py drives the model from a real loop, not one call."""
    tree = parse_source("loop", _read("agent_service/loop.py"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.While | ast.For):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call) and "turn" in dotted_name(inner.func).lower():
                return True
    return False


def _model_is_offered_tools() -> bool:
    """Whether the LLM client ever passes a tool manifest to the model."""
    tree = parse_source("llm_client", _read("agent_service/llm_client.py"))
    return any(
        isinstance(node, ast.Call) and any(keyword.arg == "tools" for keyword in node.keywords)
        for node in ast.walk(tree)
    )


def check_agent_process() -> tuple[bool, str]:
    """Blueprint 4.5/6/9.3: the agent is an independent process that chooses tools.

    Four claims: its own entry point, no import from the web tier, a real
    loop around the model's turns, and a tool manifest actually offered.
    """
    problems: list[str] = []
    if not (PROJECT_ROOT / "agent_service" / "__main__.py").is_file():
        problems.append("agent_service/__main__.py is missing")

    isolation = apply_rule(agent_isolation_violations, "agent_service", "mcp_server")
    problems.extend(str(violation) for violation in isolation)

    if not _loop_encloses_model_turns():
        problems.append("agent_service/loop.py has no loop around the model turns")
    if not _model_is_offered_tools():
        problems.append("agent_service/llm_client.py never passes tools= to the model")
    if "def build_tool_manifest" not in _read("agent_service/loop.py"):
        problems.append("no tool manifest builder")

    return _verdict(
        problems,
        "runs as `python -m agent_service`; imports no module above "
        "app.domain/config/infrastructure/eventstore; loop.py drives the "
        "model from a while loop and llm_client.py offers it a tool manifest",
    )


def check_web_search() -> tuple[bool, str]:
    """Blueprint 4.5 and spec 13: web search behind a RAG-first policy gate.

    The policy is pure, so it is called directly: our own records must be
    refused, an unanswered question allowed, an answered one refused.
    """
    from agent_service.tools.web_search import SearchDecision, decide_whether_to_search

    own_records = decide_whether_to_search(
        "what does this adopter profile say about their yard",
        relevant_knowledge_found=False,
        already_searched_this_task=False,
    )
    knowledge_gap = decide_whether_to_search(
        "how much exercise does a lop-eared rabbit need",
        relevant_knowledge_found=False,
        already_searched_this_task=False,
    )
    rag_sufficient = decide_whether_to_search(
        "how much exercise does a lop-eared rabbit need",
        relevant_knowledge_found=True,
        already_searched_this_task=False,
    )

    problems: list[str] = []
    if own_records is not SearchDecision.REFUSED_OWN_RECORDS:
        problems.append(f"a question about our own records returned {own_records}")
    if not knowledge_gap.is_allowed:
        problems.append(f"an unanswered question returned {knowledge_gap}")
    if rag_sufficient.is_allowed:
        problems.append(f"an answered question still returned {rag_sufficient}")

    return _verdict(
        problems,
        f"decide_whether_to_search() returns {own_records.name} for our own "
        f"records, {knowledge_gap.name} for a knowledge gap and "
        f"{rag_sufficient.name} when RAG answered - RAG first, web second",
    )


MINIMUM_KNOWLEDGE_DOCUMENTS = 10


def check_rag() -> tuple[bool, str]:
    """Blueprint 7: a vector database of curated knowledge, searched semantically.

    Verifies the corpus, the retrieval surface and the ingestion path exist
    without opening Chroma or Ollama; the agent suite proves retrieval quality.
    """
    from agent_service.rag.knowledge_base import KnowledgeBase

    guides = sorted((PROJECT_ROOT / "knowledge").glob("*.md"))
    problems: list[str] = []
    if len(guides) < MINIMUM_KNOWLEDGE_DOCUMENTS:
        problems.append(
            f"only {len(guides)} curated guides, fewer than {MINIMUM_KNOWLEDGE_DOCUMENTS}"
        )

    retrieval_methods = [
        name for name in ("search", "search_relevant") if hasattr(KnowledgeBase, name)
    ]
    if not retrieval_methods:
        problems.append("KnowledgeBase exposes no search method")
    if not hasattr(KnowledgeBase, "ingest_directory"):
        problems.append("KnowledgeBase exposes no ingestion method")
    if not (PROJECT_ROOT / "scripts" / "ingest_knowledge.py").is_file():
        problems.append("scripts/ingest_knowledge.py is missing")

    return _verdict(
        problems,
        f"{len(guides)} curated guides; KnowledgeBase exposes "
        f"{', '.join(retrieval_methods)} and ingest_directory; "
        f"scripts/ingest_knowledge.py present",
    )


MINIMUM_MCP_TOOLS = 2
MINIMUM_TOOL_DOCSTRING_WORDS = 20


def _mcp_tool_functions() -> list[tuple[str, int]]:
    """Return each `@mcp_server.tool()` function with its docstring word count.

    Read with `ast`, not imported - importing the server builds a database
    engine from `.env`, which verification must not require.
    """
    tree = parse_source("mcp_server", _read("mcp_server/server.py"))
    tools: list[tuple[str, int]] = []
    for function in functions_in(tree):
        if not any(name.endswith(".tool") for name in decorator_names(function)):
            continue
        docstring = ast.get_docstring(function) or ""
        tools.append((function.name, len(docstring.split())))
    return tools


def _mcp_transport() -> str:
    """Return the transport named in the server's `run(...)` call."""
    tree = parse_source("mcp_server", _read("mcp_server/server.py"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not dotted_name(node.func).endswith(".run"):
            continue
        for keyword in node.keywords:
            if keyword.arg == "transport" and isinstance(keyword.value, ast.Constant):
                return str(keyword.value.value)
    return ""


def check_mcp_tools() -> tuple[bool, str]:
    """Blueprint 8: at least two local MCP tools over stdio, each usefully described.

    The docstring is what the model sees when choosing a tool, so twenty
    words is the floor at which it says something.
    """
    tools = _mcp_tool_functions()
    transport = _mcp_transport()
    problems: list[str] = []
    if len(tools) < MINIMUM_MCP_TOOLS:
        problems.append(f"only {len(tools)} tools decorated as MCP tools")
    problems.extend(
        f"{name} has a {words}-word docstring, under {MINIMUM_TOOL_DOCSTRING_WORDS}"
        for name, words in tools
        if words < MINIMUM_TOOL_DOCSTRING_WORDS
    )
    if transport != "stdio":
        problems.append(f"the server runs with transport={transport!r}, not 'stdio'")

    described = ", ".join(f"{name} ({words} words)" for name, words in tools)
    return _verdict(problems, f"{len(tools)} tools over stdio: {described}")


#: The four aggregates the domain is built from; each must be both written
#: and read through the bus or CQRS is decorative on that aggregate.
AGGREGATE_NAME_MARKERS = {
    "Application": ("Application",),
    "Invitation": ("Invitation",),
    "Animal": ("Animal",),
    "AdopterProfile": ("Profile",),
}


def _registered_message_types() -> tuple[list[str], list[str]]:
    """Return the command and query class names registered on the live bus."""
    bus = application_under_verification().config["BUS"]
    commands = getattr(bus, "_command_handlers", {})
    queries = getattr(bus, "_query_handlers", {})
    return (
        sorted(message.__name__ for message in commands),
        sorted(message.__name__ for message in queries),
    )


def _aggregates_missing_a_handler(registered: list[str]) -> list[str]:
    """Return aggregates with no registered message of their own."""
    return [
        aggregate
        for aggregate, markers in AGGREGATE_NAME_MARKERS.items()
        if not any(marker in name for name in registered for marker in markers)
    ]


def check_mvc_and_cqrs() -> tuple[bool, str]:
    """Blueprint 9/9.1/9.2: MVC layers and a real command/query split.

    Duplicates the R2 guard logic used above (rather than importing tests/,
    which would make the script depend on the suite it is unrelated to) and
    adds one dynamic check: every aggregate has a registered handler on both
    sides of the live bus.
    """
    from app.cqrs.base import MessageBus

    problems = [
        str(violation)
        for violation in (
            *apply_rule(domain_boundary_violations, "app/domain"),
            *apply_rule(query_write_violations, "app/cqrs/queries"),
            *apply_rule(command_return_violations, "app/cqrs/commands"),
        )
    ]
    if not (hasattr(MessageBus, "dispatch_command") and hasattr(MessageBus, "dispatch_query")):
        problems.append("the bus does not expose separate dispatch methods")

    commands, queries = _registered_message_types()
    problems.extend(
        f"no command handler registered for {aggregate}"
        for aggregate in _aggregates_missing_a_handler(commands)
    )
    problems.extend(
        f"no query handler registered for {aggregate}"
        for aggregate in _aggregates_missing_a_handler(queries)
    )

    return _verdict(
        problems,
        f"domain imports no framework; no query writes; no command returns a "
        f"DTO; {len(commands)} command and {len(queries)} query handlers on "
        f"the bus, covering all four aggregates",
    )


MINIMUM_DOMAIN_EVENT_TYPES = 12


def _replay_claim_problems() -> list[str]:
    """Return a problem for each replay function docs/ARCHITECTURE.md invents."""
    architecture = _read("docs/ARCHITECTURE.md")
    claimed = set(re.findall(r"`(\w+)\(\)`", architecture))
    defined: set[str] = set()
    for path in python_files_under("app", "agent_service"):
        tree = parse_source(project_relative(path), path.read_text(encoding="utf-8"))
        defined.update(function.name for function in functions_in(tree))
    return [
        f"docs/ARCHITECTURE.md claims `{name}()` but no such function exists"
        for name in sorted(claimed)
        if ("replay" in name or "projection" in name) and name not in defined
    ]


def check_event_sourcing() -> tuple[bool, str]:
    """Blueprint 10: an append-only event store with a real catalogue.

    Also holds docs/ARCHITECTURE.md to account (R5): if it claims a
    projection rebuild, the function must actually exist.
    """
    from app.eventstore.store import EventStore

    problems: list[str] = []
    if not hasattr(EventStore, "append"):
        problems.append("EventStore exposes no append")
    readers = [name for name in dir(EventStore) if name.startswith("read")]
    if not readers:
        problems.append("EventStore exposes no read method")
    mutators = [
        name for name in dir(EventStore) if name.startswith(("update", "delete", "remove"))
    ]
    if mutators:
        problems.append(f"EventStore exposes mutating operations: {mutators}")

    declared = declared_domain_event_types()
    if len(declared) < MINIMUM_DOMAIN_EVENT_TYPES:
        problems.append(
            f"DomainEventType has {len(declared)} members, fewer than "
            f"{MINIMUM_DOMAIN_EVENT_TYPES}"
        )
    problems.extend(_replay_claim_problems())

    unused = unused_domain_event_types()
    detail = (
        f"append-only store with {len(readers)} read methods and no mutating "
        f"operation; {len(declared)} event types"
    )
    if unused:
        detail += f"; unused event types: {', '.join(unused)}"
    return _verdict(problems, detail)


def check_cloud_database() -> tuple[bool, str]:
    """Blueprint 11: the transactional database is cloud-hosted (Somee.com).

    Reads `app/config.py`, not `.env`, so this works with no credentials.
    """
    tree = parse_source("config", _read("app/config.py"))
    builds_mssql_url = any(
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "mssql+pymssql://" in node.value
        for node in ast.walk(tree)
    )
    required_variables = {
        argument.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and dotted_name(node.func) == "_required"
        for argument in node.args
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
    }
    expected = {"DB_SERVER", "DB_NAME", "DB_USER", "DB_PASSWORD"}

    problems: list[str] = []
    if not builds_mssql_url:
        problems.append("app/config.py builds no mssql+pymssql URL")
    if missing := expected - required_variables:
        problems.append(f"app/config.py does not require {sorted(missing)}")
    if "somee" not in _read(".env.example").lower():
        problems.append(".env.example does not document Somee.com")

    return _verdict(
        problems,
        "app/config.py builds an mssql+pymssql URL from DB_SERVER, DB_NAME, "
        "DB_USER and DB_PASSWORD; .env.example documents Somee.com",
    )


EXTERNAL_SKILLS = ("mcp-builder", "webapp-testing")
OWN_SKILLS = ("clean-code", "db-management", "testing")


def _skills_with_valid_frontmatter() -> list[str]:
    """Return skill directory names whose SKILL.md declares name and description."""
    valid: list[str] = []
    for path in sorted((PROJECT_ROOT / ".claude" / "skills").glob("*/SKILL.md")):
        lines = path.read_text(encoding="utf-8").splitlines()
        if not lines or lines[0].strip() != "---":
            continue
        closing = next(
            (index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"), 0
        )
        frontmatter = lines[1:closing]
        declares = {line.split(":", 1)[0].strip() for line in frontmatter if ":" in line}
        if {"name", "description"} <= declares:
            valid.append(path.parent.name)
    return valid


def check_skills_and_rules() -> tuple[bool, str]:
    """Blueprint 15: an ecosystem skill, an authored skill and a real rule.

    Validates the SKILL.md frontmatter rather than just the directory
    listing - a file with no name/description is not a loadable skill.
    """
    installed = _skills_with_valid_frontmatter()
    external = [name for name in EXTERNAL_SKILLS if name in installed]
    own = [name for name in OWN_SKILLS if name in installed]
    rules = re.findall(r"^## (R\d+)\b", _read("CLAUDE.md"), re.MULTILINE)

    problems: list[str] = []
    if not external:
        problems.append(f"no ecosystem skill installed (looked for {EXTERNAL_SKILLS})")
    if not own:
        problems.append(f"no authored skill present (looked for {OWN_SKILLS})")
    if not rules:
        problems.append("CLAUDE.md declares no `## R<n>` rule")

    return _verdict(
        problems,
        f"{len(installed)} valid skills; ecosystem: {', '.join(external)}; "
        f"authored: {', '.join(own)}; rules: {', '.join(rules)}",
    )


REQUIRED_DOCUMENTS = (
    "PRD", "REQUIREMENTS", "FEATURES", "ARCHITECTURE", "MODEL_DATA",
    "API", "AGENT", "MCP", "TESTING", "UX", "SKILLS_AND_RULES",
)
MINIMUM_DOCUMENT_LINES = 40


def check_documents() -> tuple[bool, str]:
    """Blueprint 14: eleven substantive markdown documents, each linked from the index.

    A four-line file is not a document, and one nothing links to is not part
    of the set.
    """
    index = _read("README.md") + _read("PLANNING.md")
    problems: list[str] = []
    total_lines = 0
    for name in REQUIRED_DOCUMENTS:
        text = _read(f"docs/{name}.md")
        if not text:
            problems.append(f"docs/{name}.md is missing")
            continue
        lines = [line for line in text.splitlines() if line.strip()]
        total_lines += len(lines)
        if len(lines) < MINIMUM_DOCUMENT_LINES:
            problems.append(
                f"docs/{name}.md has {len(lines)} non-empty lines, under "
                f"{MINIMUM_DOCUMENT_LINES}"
            )
        if f"{name}.md" not in index:
            problems.append(f"{name}.md is referenced from neither README.md nor PLANNING.md")

    return _verdict(
        problems,
        f"all {len(REQUIRED_DOCUMENTS)} documents present, {total_lines} "
        f"non-empty lines, each referenced from README.md or PLANNING.md",
    )


def check_version_control() -> tuple[bool, str]:
    """Blueprint 19: the project is in version control with real history."""
    try:
        result = subprocess.run(
            ["git", "log", "--oneline"],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, "git is not available"

    commits = [line for line in result.stdout.splitlines() if line.strip()]
    return len(commits) > 0, f"{len(commits)} commits"


TEST_SUITES = ("unit", "integration", "api", "agent", "e2e")
MINIMUM_NEGATIVE_TESTS = 40

#: Inclusive bounds of the HTTP client-error range - a status code inside it
#: is a refusal being proved, which is blueprint 17's failure scenario.
CLIENT_ERROR_RANGE = (400, 499)


def _test_functions_in(suite: str) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Return every test function in one suite, coroutines included."""
    functions: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for path in sorted((PROJECT_ROOT / "tests" / suite).rglob("test_*.py")):
        tree = parse_source(project_relative(path), path.read_text(encoding="utf-8"))
        functions.extend(
            function for function in functions_in(tree) if function.name.startswith("test_")
        )
    return functions


def _compares_status_code_to_client_error(node: ast.AST) -> bool:
    """Whether a comparison asserts a 4xx HTTP status code, either operand order."""
    if not isinstance(node, ast.Compare):
        return False
    operands = [node.left, *node.comparators]
    names_status = any(
        isinstance(operand, ast.Attribute) and operand.attr == "status_code"
        for operand in operands
    )
    client_error = any(
        isinstance(operand, ast.Constant)
        and isinstance(operand.value, int)
        and CLIENT_ERROR_RANGE[0] <= operand.value <= CLIENT_ERROR_RANGE[1]
        for operand in operands
    )
    contains_client_errors = any(
        isinstance(operand, ast.Tuple | ast.List | ast.Set)
        and any(
            isinstance(element, ast.Constant)
            and isinstance(element.value, int)
            and CLIENT_ERROR_RANGE[0] <= element.value <= CLIENT_ERROR_RANGE[1]
            for element in operand.elts
        )
        for operand in operands
    )
    return names_status and (client_error or contains_client_errors)


def _is_negative_test(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether a test proves a failure rather than a happy path.

    Either it expects an exception or it asserts a 4xx status code; merely
    reading `response.status_code` for a 200 does not count.
    """
    for node in ast.walk(function):
        if isinstance(node, ast.Call) and dotted_name(node.func) == "pytest.raises":
            return True
        if _compares_status_code_to_client_error(node):
            return True
    return False


def check_test_suites() -> tuple[bool, str]:
    """Blueprint 17: every test category exists and failure scenarios are tested.

    Counted with `ast`, so `async def test_...` counts - a `^def test_` grep
    silently drops every one.
    """
    counts = {suite: _test_functions_in(suite) for suite in TEST_SUITES}
    negative = sum(
        1
        for functions in counts.values()
        for function in functions
        if _is_negative_test(function)
    )

    problems = [
        f"no tests in tests/{suite}" for suite, functions in counts.items() if not functions
    ]
    if negative < MINIMUM_NEGATIVE_TESTS:
        problems.append(f"{negative} negative tests, fewer than {MINIMUM_NEGATIVE_TESTS}")

    summary = ", ".join(f"{suite} {len(functions)}" for suite, functions in counts.items())
    total = sum(len(functions) for functions in counts.values())
    return _verdict(
        problems,
        f"{total} test functions ({summary}); {negative} of them prove a failure scenario",
    )


def check_deterministic_scoring() -> tuple[bool, str]:
    """Spec 8 and CLAUDE.md R3: the scorer is deterministic and model-free.

    The matching module may import stdlib and other domain modules only, and
    identical input must score identically both times.
    """
    from app.domain.enums import (
        ActivityLevel,
        AnimalSize,
        ExperienceLevel,
        HomeType,
        MatchDirection,
        Species,
        Temperament,
    )
    from app.domain.matching import AdopterFacts, AnimalFacts, calculate_match_score

    problems = [
        str(violation)
        for violation in apply_rule(domain_boundary_violations, "app/domain/matching.py")
    ]

    adopter = AdopterFacts(
        home_type=HomeType.APARTMENT,
        has_yard=False,
        household_has_children=False,
        youngest_child_age=None,
        has_other_animals=False,
        experience_level=ExperienceLevel.SOME,
        activity_level=ActivityLevel.LOW,
        daily_hours_available=3.0,
        city="Haifa",
        preferred_species=frozenset({Species.RABBIT}),
    )
    animal = AnimalFacts(
        species=Species.RABBIT,
        age_years=2.0,
        size=AnimalSize.SMALL,
        temperament=Temperament.CALM,
        activity_level=ActivityLevel.LOW,
        good_with_children=True,
        good_with_other_animals=True,
        has_special_needs=False,
        required_space=AnimalSize.SMALL,
        city="Haifa",
    )
    first = calculate_match_score(adopter, animal, MatchDirection.ADOPTER_TO_ANIMAL)
    second = calculate_match_score(adopter, animal, MatchDirection.ADOPTER_TO_ANIMAL)
    if first != second:
        problems.append(f"the same input scored {first.score} then {second.score}")

    return _verdict(
        problems,
        f"app/domain/matching.py imports only stdlib and app.domain; the same "
        f"input scored {first.score} twice with no model involved",
    )


def check_agent_cannot_decide() -> tuple[bool, str]:
    """Spec 6.4 and CLAUDE.md R4: no agent code path can name a decision command."""
    violations = apply_rule(agent_decision_violations, "agent_service", "mcp_server")
    modules = len(python_files_under("agent_service", "mcp_server"))
    return _verdict(
        [str(violation) for violation in violations],
        f"none of the {modules} agent or MCP modules imports or names a "
        f"decision command; a human staff member decides",
    )


def check_controller_boundary() -> tuple[bool, str]:
    """CLAUDE.md R2: controllers dispatch on the bus; they never hold a session."""
    violations = apply_rule(controller_persistence_violations, "app/controllers")
    return _verdict(
        [str(violation) for violation in violations],
        "no controller imports SQLAlchemy or holds a session; persistence is "
        "reached through app/controllers/helpers.py",
    )


def check_documentation_references() -> tuple[bool, str]:
    """CLAUDE.md R5 and blueprint 18: docs and code may not contradict.

    Every backticked test name, project path and CQRS class name across the
    documents is resolved against the repository.
    """
    dead = find_dead_references()
    if not dead:
        return True, (
            f"every backticked test, path and class name across "
            f"{len(documentation_files())} documents resolves"
        )
    by_kind: dict[str, int] = {}
    for reference in dead:
        by_kind[reference.kind] = by_kind.get(reference.kind, 0) + 1
    breakdown = ", ".join(f"{count} {kind}" for kind, count in sorted(by_kind.items()))
    return False, (
        f"{len(dead)} dead references ({breakdown}); first: {dead[0]} "
        f"- run tests/unit/test_docs_reference_real_artifacts.py for the full list"
    )


ALL_CHECKS = (
    Check("Flask", check_flask),
    Check("Authentication", check_authentication),
    Check("Two roles + authorization", check_two_roles),
    Check("4.1 Search", check_search),
    Check("4.2 Details view", check_details_view),
    Check("4.3 Tabular display", check_tabular_display),
    Check("4.4 Dashboard", check_dashboard),
    Check("4.5 Data entry forms", check_data_entry),
    Check("AI agent, own process", check_agent_process),
    Check("Web search", check_web_search),
    Check("Vector DB + RAG", check_rag),
    Check("Two local MCP tools (stdio)", check_mcp_tools),
    Check("MVC + CQRS", check_mvc_and_cqrs),
    Check("Controller boundary", check_controller_boundary),
    Check("Event sourcing", check_event_sourcing),
    Check("Cloud database", check_cloud_database),
    Check("Skills and rules", check_skills_and_rules),
    Check("Eleven MD documents", check_documents),
    Check("Version control", check_version_control),
    Check("All test categories", check_test_suites),
    Check("Deterministic scoring", check_deterministic_scoring),
    Check("Agent makes no decisions", check_agent_cannot_decide),
    Check("Docs reference real artifacts", check_documentation_references),
)


def main() -> int:
    """Run every check and return non-zero if any failed."""
    print("PetMatch - mandatory requirement verification")
    print(f"project: {PROJECT_ROOT}\n")

    failures = 0
    for check in ALL_CHECKS:
        try:
            passed, detail = check.verify()
        except Exception as error:
            passed, detail = False, f"check raised {type(error).__name__}: {error}"

        marker = "PASS" if passed else "FAIL"
        failures += 0 if passed else 1
        print(f"  [{marker}] {check.requirement:<30} {detail}")

    print(f"\n{len(ALL_CHECKS) - failures}/{len(ALL_CHECKS)} requirements verified")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
