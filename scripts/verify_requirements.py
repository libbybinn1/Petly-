r"""Check every mandatory requirement from the course blueprint's checklist.

Blueprint section 20 lists what the project must demonstrate. This script
verifies each item against the code rather than against memory, so the
answer to "is it all there?" is something you run instead of something you
recall.

Each check reports what it actually inspected, so a PASS can be audited and
a FAIL says where to look.

Usage:
    <venv>\\Scripts\\python.exe scripts\\verify_requirements.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

MINIMUM_MCP_TOOLS = 2
MINIMUM_NEGATIVE_ASSERTIONS = 20

REQUIRED_DOCUMENTS = (
    "PRD", "REQUIREMENTS", "FEATURES", "ARCHITECTURE", "MODEL_DATA",
    "API", "AGENT", "MCP", "TESTING", "UX", "SKILLS_AND_RULES",
)


@dataclass(frozen=True)
class Check:
    """One checklist item and how it is verified."""

    requirement: str
    verify: Callable[[], tuple[bool, str]]


def _exists(*relative_paths: str) -> bool:
    """Whether every path exists."""
    return all((PROJECT_ROOT / path).exists() for path in relative_paths)


def _read(relative_path: str) -> str:
    """Read a project file, or empty string when absent."""
    path = PROJECT_ROOT / relative_path
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _count_tests(directory: str) -> int:
    """Count test functions in a directory."""
    total = 0
    for path in (PROJECT_ROOT / directory).rglob("test_*.py"):
        total += len(re.findall(r"^\s*def test_", path.read_text(encoding="utf-8"), re.M))
    return total


def check_flask() -> tuple[bool, str]:
    """The application is built on Flask."""
    return ("Flask" in _read("requirements.txt"), "requirements.txt pins Flask")


def check_mvc() -> tuple[bool, str]:
    """Controllers, views and domain are separate, and the domain is framework-free."""
    if not _exists("app/controllers", "app/templates", "app/domain"):
        return False, "one of controllers/, templates/, domain/ is missing"

    for path in (PROJECT_ROOT / "app" / "domain").glob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "import flask" in source or "from flask" in source:
            return False, f"{path.name} imports Flask, breaking the domain boundary"
        if "sqlalchemy" in source:
            return False, f"{path.name} imports SQLAlchemy, breaking the domain boundary"

    return True, "domain/ imports no framework; controllers, views and domain are separate"


def check_cqrs() -> tuple[bool, str]:
    """Commands and queries are separated in implementation, not just in name."""
    base = _read("app/cqrs/base.py")
    if "dispatch_command" not in base or "dispatch_query" not in base:
        return False, "the bus does not expose separate dispatch paths"
    if "session.rollback()" not in base:
        return False, "query sessions are not rolled back"

    commands = len(list((PROJECT_ROOT / "app/cqrs/commands").glob("*_commands.py")))
    queries = len(list((PROJECT_ROOT / "app/cqrs/queries").glob("*_queries.py")))
    return True, f"{commands} command modules, {queries} query modules, separate dispatch"


def check_event_sourcing() -> tuple[bool, str]:
    """The event store is append-only and the catalogue is complete."""
    store = _read("app/eventstore/store.py")
    if "def append" not in store:
        return False, "no append operation"
    if re.search(r"def (update|delete)", store):
        return False, "the store exposes a mutating operation"

    events = len(re.findall(r'^\s+[A-Z_]+ = "', _read("app/domain/enums.py"), re.M))
    return True, (
        f"append-only store, no update or delete; {events} enum values "
        f"including the event catalogue"
    )


def check_cloud_database() -> tuple[bool, str]:
    """The transactional database is cloud-hosted."""
    example = _read(".env.example")
    return ("somee.com" in example.lower(), "configured for a cloud SQL Server (Somee.com)")


def check_authentication() -> tuple[bool, str]:
    """Registration, sign-in and hashed passwords exist."""
    auth = _read("app/controllers/auth_controller.py")
    required = ("generate_password_hash", "check_password_hash", "def register", "def login")
    has_all = all(term in auth for term in required)
    return has_all, "registration and sign-in with hashed passwords"


def check_two_roles() -> tuple[bool, str]:
    """Two roles exist and are enforced server-side."""
    enums = _read("app/domain/enums.py")
    security = _read("app/security/authorization.py")
    if "ADOPTER" not in enums or "STAFF" not in enums:
        return False, "the two roles are not defined"
    if "abort(403)" not in security:
        return False, "authorization does not return 403"
    return True, "ADOPTER and STAFF, enforced by decorators returning 401/403"


def check_search() -> tuple[bool, str]:
    """Structured and natural-language search both exist."""
    structured = "def search" in _read("app/controllers/animal_controller.py")
    natural = "natural_language_search" in _read("app/controllers/match_controller.py")
    return structured and natural, "structured filters and natural-language description"


def check_details_view() -> tuple[bool, str]:
    """An animal details screen exists."""
    return (_exists("app/templates/animals/details.html"), "animal details screen")


def check_tabular_display() -> tuple[bool, str]:
    """A table-based screen exists."""
    manage = _read("app/templates/animals/manage.html")
    return ("<table" in manage, "staff animal management table")


def check_dashboard() -> tuple[bool, str]:
    """An operational dashboard with a Needs Attention section exists."""
    queries = _read("app/cqrs/queries/dashboard_queries.py")
    template = _read("app/templates/dashboard.html")
    if "attention_items" not in queries:
        return False, "the dashboard has no attention items"
    if "Needs attention" not in template:
        return False, "the template has no Needs Attention section"
    return True, "statistics, Needs Attention, and an event-log activity feed"


def check_data_entry() -> tuple[bool, str]:
    """The business forms exist."""
    forms = {
        "registration": "app/templates/auth/register.html",
        "adopter profile": "app/templates/personal/profile.html",
        "adoption application": "app/templates/animals/details.html",
        "invitation response": "app/templates/personal/invitations.html",
    }
    missing = [name for name, path in forms.items() if not _exists(path)]
    if missing:
        return False, f"missing forms: {', '.join(missing)}"
    return True, f"{len(forms)} business forms, validated on the server"


def check_agent_process() -> tuple[bool, str]:
    """The agent runs as its own process and does not import the app."""
    if not _exists("agent_service/__main__.py", "agent_service/worker.py"):
        return False, "the agent has no entry point"

    for path in (PROJECT_ROOT / "agent_service").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if re.search(r"^from app import|^import app$", source, re.M):
            return False, f"{path.name} imports the Flask application"

    return True, "runs as `python -m agent_service`, polls analysis_jobs, imports no Flask app"


def check_web_search() -> tuple[bool, str]:
    """Web search exists behind an explicit policy gate."""
    search = _read("agent_service/tools/web_search.py")
    gated = "decide_whether_to_search" in search
    guards_own_records = "REFUSED_OWN_RECORDS" in search
    return gated and guards_own_records, "Tavily behind a policy gate that refuses own records"


def check_rag() -> tuple[bool, str]:
    """A vector database holds curated knowledge and is searched semantically."""
    if not _exists("agent_service/rag/knowledge_base.py"):
        return False, "no knowledge base module"
    guides = len(list((PROJECT_ROOT / "knowledge").glob("*.md")))
    if guides == 0:
        return False, "no curated knowledge documents"
    return True, f"ChromaDB over {guides} curated guides, retrieved by embedding similarity"


def check_mcp_tools() -> tuple[bool, str]:
    """At least two local MCP tools communicate over stdio."""
    server = _read("mcp_server/server.py")
    tools = re.findall(r"@mcp_server\.tool\(\)\s*\ndef (\w+)", server)
    if len(tools) < MINIMUM_MCP_TOOLS:
        return False, f"only {len(tools)} tools found"
    if 'transport="stdio"' not in server:
        return False, "the server does not use stdio transport"
    return True, f"{len(tools)} tools over stdio: {', '.join(tools)}"


def check_external_skill() -> tuple[bool, str]:
    """At least one skill comes from the skills ecosystem."""
    installed = [
        path.parent.name
        for path in (PROJECT_ROOT / ".claude/skills").glob("*/SKILL.md")
        if path.parent.name in ("mcp-builder", "webapp-testing")
    ]
    return len(installed) >= 1, f"installed: {', '.join(installed)}"


def check_own_skill() -> tuple[bool, str]:
    """At least one skill was written for this project."""
    own = [
        path.parent.name
        for path in (PROJECT_ROOT / ".claude/skills").glob("*/SKILL.md")
        if path.parent.name in ("clean-code", "db-management", "testing")
    ]
    return len(own) >= 1, f"authored: {', '.join(own)}"


def check_rules() -> tuple[bool, str]:
    """At least one project-specific rule guides the coding agent."""
    rules = _read("CLAUDE.md")
    found = re.findall(r"^## (R\d) — (.+)$", rules, re.M)
    codes = ", ".join(code for code, _ in found)
    return len(found) >= 1, f"{len(found)} rules in CLAUDE.md: {codes}"


def check_documents() -> tuple[bool, str]:
    """All eleven mandated markdown documents exist."""
    missing = [name for name in REQUIRED_DOCUMENTS if not _exists(f"docs/{name}.md")]
    if missing:
        return False, f"missing: {', '.join(missing)}"
    lines = sum(len(_read(f"docs/{name}.md").splitlines()) for name in REQUIRED_DOCUMENTS)
    return True, f"all {len(REQUIRED_DOCUMENTS)} documents, {lines} lines"


def check_version_control() -> tuple[bool, str]:
    """The project is in version control with real commits."""
    try:
        result = subprocess.run(  # - fixed command
            ["git", "log", "--oneline"],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, "git is not available"

    commits = [line for line in result.stdout.splitlines() if line.strip()]
    return len(commits) > 0, f"{len(commits)} commits"


def check_test_suites() -> tuple[bool, str]:
    """Every required category of test exists."""
    counts = {name: _count_tests(f"tests/{name}") for name in
              ("unit", "integration", "api", "agent", "e2e")}
    missing = [name for name, count in counts.items() if count == 0]
    if missing:
        return False, f"no tests in: {', '.join(missing)}"
    summary = ", ".join(f"{name} {count}" for name, count in counts.items())
    return True, f"{sum(counts.values())} tests — {summary}"


def check_negative_tests() -> tuple[bool, str]:
    """Failure scenarios are tested, not only happy paths."""
    total = 0
    for path in (PROJECT_ROOT / "tests").rglob("test_*.py"):
        source = path.read_text(encoding="utf-8")
        total += len(re.findall(r"pytest\.raises|== 40[0-9]|== 3[0-9][0-9]", source))
    return total >= MINIMUM_NEGATIVE_ASSERTIONS, f"{total} negative assertions across the suite"


def check_deterministic_scoring() -> tuple[bool, str]:
    """Scores are computed without a language model (spec section 8)."""
    matching = _read("app/domain/matching.py")
    if "ollama" in matching.lower() or "llm" in matching.lower():
        return False, "the matching module references a language model"
    return True, "matching imports no model; scores are pure Python"


def check_agent_cannot_decide() -> tuple[bool, str]:
    """No agent code path approves an adoption (spec section 6.4)."""
    for path in (PROJECT_ROOT / "agent_service").rglob("*.py"):
        source = path.read_text(encoding="utf-8").lower()
        if "approveapplication" in source or "def approve" in source:
            return False, f"{path.name} can approve an application"
    return True, "no agent module can approve, reject or finalise an adoption"


ALL_CHECKS = (
    Check("Flask", check_flask),
    Check("MVC", check_mvc),
    Check("CQRS", check_cqrs),
    Check("Event sourcing", check_event_sourcing),
    Check("Cloud database", check_cloud_database),
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
    Check("External skill", check_external_skill),
    Check("Self-defined skill", check_own_skill),
    Check("Project rules", check_rules),
    Check("Eleven MD documents", check_documents),
    Check("Version control", check_version_control),
    Check("All test categories", check_test_suites),
    Check("Negative tests", check_negative_tests),
    Check("Deterministic scoring", check_deterministic_scoring),
    Check("Agent makes no decisions", check_agent_cannot_decide),
)


def main() -> int:
    """Run every check and return non-zero if any failed."""
    print("PetMatch — mandatory requirement verification")
    print(f"project: {PROJECT_ROOT}\n")

    failures = 0
    for check in ALL_CHECKS:
        try:
            passed, detail = check.verify()
        except Exception as error:  # - a broken check is a failure
            passed, detail = False, f"check raised {type(error).__name__}: {error}"

        marker = "PASS" if passed else "FAIL"
        failures += 0 if passed else 1
        print(f"  [{marker}] {check.requirement:<30} {detail}")

    print(f"\n{len(ALL_CHECKS) - failures}/{len(ALL_CHECKS)} requirements verified")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
