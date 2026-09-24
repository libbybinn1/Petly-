# SKILLS AND RULES — PetMatch

**Instructions that guide the coding agent on this project, and what they
actually changed.**
Source: course blueprint §15.

Blueprint §15 requires: at least one skill from the `sh.skills` ecosystem, at
least one skill defined by the students, at least one Rule, and that Rules
guide the coding agent on *how to work on this project* rather than containing
only general advice.

This project has **five skills and five rules**. §4 below is the part worth
reading: what each rule cost, and what it caught.

---

## 1. Skills

Five skills live under `.claude/skills/`, each as a `SKILL.md` with YAML
frontmatter naming it and describing when to use it.
`scripts/verify_requirements.py` validates all five and reports which are
from the ecosystem and which were authored here.

### 1.1 Installed from the skills.sh ecosystem

`skills.sh` resolves skills from GitHub repositories and installs them as
`SKILL.md` folders. Both of these came from `anthropics/skills`.

| Skill | What it is for |
|---|---|
| **`mcp-builder`** | "Guide for creating high-quality MCP servers that enable LLMs to interact with external services through well-designed tools… whether in Python (FastMCP) or Node/TypeScript." It shaped `mcp_server/server.py`: tool descriptions written for a model rather than a developer, structured not-found results instead of exceptions across the boundary, and stdout reserved for the protocol. Directly serves blueprint §8. |
| **`webapp-testing`** | "Toolkit for interacting with and testing local web applications using Playwright. Supports verifying frontend functionality, debugging UI behavior, capturing browser screenshots, and viewing browser logs." It shaped `tests/e2e/` — waiting on conditions rather than sleeping, and reading browser console errors as test failures. Directly serves blueprint §17. |

Both were chosen because they map onto graded checklist items, not because
they were convenient to install.

> Note: `skills.sh` itself was returning HTTP 503 when this project was built,
> so the skills were installed from their upstream repository using the same
> mechanism the CLI uses. The install path and format are identical.

### 1.2 Defined for this project

| Skill | What it is for |
|---|---|
| **`clean-code`** | The style contract, applied to every Python file written here: Single Responsibility, descriptive unabbreviated names, guard clauses and early returns, extracted helpers, maximum nesting depth of three, strict typing, Google-convention docstrings. Carries a before/after example and a pre-commit checklist, so it is a thing to apply rather than a thing to agree with. |
| **`db-management`** | One command surface for the database — `scripts/db.py check / create / reset / seed / fresh / tables / events` — plus the SQL Server 2014 constraints that shaped the schema, so they are not rediscovered painfully. It is the reason "no JSON type" appears in the model file as a comment rather than as a bug report. |
| **`testing`** | How to run each suite by marker with the external interpreter, what each suite must prove, and two hard rules: never assert on generated text, and scoring tests never touch an LLM. It also records the symptom of using the wrong interpreter, which cost real time once. |

`clean-code` is not advisory. Most of it is enforced mechanically by ruff:

| Rule in the skill | Ruff rule |
|---|---|
| Early returns, no nested happy paths | `RET`, `SIM` |
| Small functions, few branches | `PL` — `max-branches = 8`, `max-args = 6`, `max-statements = 40` |
| Strict typing | `ANN`, plus `mypy --strict` |
| Comprehensive docstrings | `D`, Google convention |
| No `print()` in application code | `T20` |
| Timezone-aware datetimes | `DTZ` |
| Descriptive names | `N` |

**One honest qualification.** The skill says "maximum nesting depth of three",
and **nothing enforces depth**. `max-branches = 8` limits how many branches a
function has, which correlates with depth but is not the same rule: eight
sequential guard clauses pass it, and a triple-nested loop with two branches
also passes. Depth 3 is a **review rule**, checked by a person. Saying it is
"enforced mechanically" would be the kind of small overstatement this
document exists to remove.

The `DTZ` rule, by contrast, is not cosmetic at all: the 72-hour invitation
expiry is wrong if a naive datetime ever enters the calculation, and
`test_calculate_expiry_refuses_a_naive_sent_at` is the test that exists
because of it.

---

## 2. The five rules

The rules live in `CLAUDE.md` at the repository root, where the coding agent
reads them automatically. They are quoted here as they stand today.

### R1 — Ambiguity Rule

> **Do not begin planning or implementation while a major requirement is
> ambiguous. Stop and ask.**

Blueprint §15 names this as the recommended rule. When a requirement is
unclear: do the work that does not depend on the answer, state the assumption
explicitly, and ask a consolidated question. Never invent a requirement, and
never silently pick a default for a decision the spec itself flagged as open
(spec §30).

### R2 — Architecture Rule

> The layer boundaries are not stylistic. Violating them breaks the MVC and
> CQRS demonstrations the project is graded on.

| Layer | May import | Must never |
|---|---|---|
| `controllers/` | services, cqrs, security | contain business rules, touch a DB session |
| `views/` (Jinja) | view models | decide anything; call a service; query |
| `domain/` | stdlib, other domain | import Flask, SQLAlchemy, or any framework |
| `cqrs/commands/` | domain, repositories, eventstore | return read DTOs; render |
| `cqrs/queries/` | repositories (read-only) | mutate state; open a write transaction |
| `repositories/` | SQLAlchemy, domain | contain business rules |
| `agent_service/` | its own modules, MCP client, and from `app` only `domain/`, `config`, `infrastructure/models` and `eventstore/` (shared vocabulary and queue access) | import the Flask factory (`app/__init__.py`), `controllers/`, `cqrs/`, `security/` or `services/`; call any command or query |

With four specific prohibitions:

- **A command never returns query data.** It returns an identifier or nothing.
  If a screen needs data after a write, the controller dispatches a query next.
- **A query never writes.** No `INSERT`, `UPDATE`, `DELETE`, no
  `session.commit()`.
- **The domain layer must be unit-testable with no Flask app context.**
- **The agent is a separate OS process.** It communicates through the
  `analysis_jobs` table and MCP stdio — never by importing the Flask app.

Two notes on how the table reads against the code, because R5 forbids letting
them drift:

1. `repositories/`, `services/` and `views/` are **empty stubs**. The row for
   each states what that layer would be permitted to do if it existed. The
   session is in fact held by the CQRS handlers, through the bus, which is the
   substance of the "controllers never touch a session" rule; `ARCHITECTURE.md`
   §2 and §3 describe the arrangement as it is.
2. The `agent_service` row was **rewritten during the hardening sprint**. It
   used to say the agent imports nothing from `app`, which was never true: the
   queue is a database table and the scorer is the domain layer. The row now
   states precisely what is allowed and what is forbidden, and the forbidden
   half — no controller, no cqrs, no security, no Flask factory — is the half
   that matters, because it is why the agent has no path to a decision.

### R3 — Testing Rule

> No feature is done without tests, and the happy path alone is never enough.

Every feature requires unit tests for its pure logic, an integration or API
test for its wiring, and **at least one negative test** (blueprint §17
mandates failure scenarios). Plus three binding constraints: never assert on
generated text; scoring tests never invoke an LLM; every test docstring states
what it proves.

### R4 — Agent Rule

> Derived from spec §6.4 and §11. These are product requirements, not
> preferences.

The agent never makes the final adoption decision. It never invents facts —
every claim traces to a retrieved RAG chunk, an MCP tool result or a web
search result. Sources are recorded. **RAG first, web second:** web search runs
only when the curated knowledge base cannot answer, and never to retrieve the
application's own records. Web content never overrides authoritative
application data.

R4 also carries a measured performance constraint, and it is the rule with the
most architectural consequence:

> **Never put an LLM call in a request/response path.** Ranking N candidates
> with N LLM calls would take minutes. Instead: the *deterministic* scorer
> ranks every candidate instantly, and the agent generates explanations only
> for the top results, asynchronously, via the `analysis_jobs` queue.

### R5 — Documentation Rule

> Code and `docs/` may never contradict each other.

A feature change updates its `docs/` section in the same commit. Every public
function docstring cites the spec section it implements. `PLANNING.md` phase
checkboxes are updated as phases complete. If implementation reveals the spec
is wrong, fix the document and say so — do not let the code silently diverge.

---

## 3. How the rules are enforced

Rules enforced only by goodwill decay. Three artifacts check them:

| Artifact | What it enforces |
|---|---|
| **`tests/unit/test_architecture_guard.py`** | Every row of R2, statically. It parses the source with `ast` — nothing is imported, so a rule holds even for a module that needs a database. 21 tests: the domain imports no framework; no query module writes; every command handler returns `str`, `int` or `None`; no controller but `helpers.py` touches SQLAlchemy; the agent imports no forbidden layer; the agent never names a decision command; every declared event type is used. **Each rule also has a test proving the rule itself catches a synthetic violation**, because a guard nobody has seen fail is a guard nobody should trust. |
| **`tests/unit/test_docs_reference_real_artifacts.py`** | R5, mechanically. Every backticked test name, project path and CQRS class name in `docs/`, `PLANNING.md`, `README.md` and `CLAUDE.md` must resolve to something real. The failure message is a sorted list of `file:line -> missing <kind>`. |
| **`scripts/verify_requirements.py`** | All 23 mandatory blueprint §20 items, from the command line, by **inspecting behaviour** — building the Flask app and reading its URL map, parsing code with `ast`, calling pure functions — never by substring search. It imports the same helpers the two test files use, so the CLI and the suite can never disagree. |

> An earlier version of this document named `test_cqrs_separation.py` as its
> flagship evidence. That file never existed. It has been replaced by the two
> real artifacts above, and the guard test is now what makes such a claim
> impossible to make again.

---

## 4. How the rules paid off

Rules that never change a decision are decoration. These did, and the
hardening sprint of 2026-09-23/24 is where it is easiest to see.

### R1 caught two things that would have been expensive

- **The database.** The specification named the cloud provider "SOMY.com". It
  is **Somee.com**, running **SQL Server 2014**, not PostgreSQL. Guessing would
  have produced an entire data layer against the wrong engine and the wrong
  dialect: no `JSON` type, no `STRING_AGG`, naive `DATETIME`, and
  application-generated UUID keys instead of identities. Every one of those is
  now a documented constraint in `MODEL_DATA.md` §0 rather than a rewrite.
- **The LLM provider.** Left open. Guessing a cloud vendor would have made the
  whole project depend on an API key that does not exist. Asking produced
  local Ollama, which is why the test suite runs offline.
- **The reseed.** During the sprint, the data engineer needed to reload the
  cloud database — a destructive operation over live demonstration data that
  takes 9–12 minutes. R1 says stop and ask rather than assume; the reseed was
  validated against a throwaway SQLite file first and the cloud database was
  left alone until the owner said so. A rule that produces a question at
  exactly the moment an irreversible action is proposed is doing its job.

### R2 caught a real violation in code that had already been reviewed

The authentication controller held a SQLAlchemy session. It had a plausible
justification — "verifying a password is not a business read" — and it had
survived several passes because the argument sounded reasonable. Writing the
rule down as a table, and then writing a test that reads the table, is what
ended it: the exception did not survive being looked at. Verifying a password
is a query with an unusual result, not a different kind of thing. It now goes
through `GetAccountForSignInQuery` like everything else, and
`test_controllers_never_hold_a_database_session` means it cannot come back.

The same rule produced the agent's boundary. Because R2 forbids importing
`cqrs/`, there is **no code path by which the agent could approve anything** —
which is a far stronger answer to "how do you know the AI cannot decide?" than
a policy statement.

### R3's negative tests found real bugs

The adversarial sweep the testing rule mandates found twelve distinct defects
that the happy-path suite had not. Among them: an animal could be approved for
two adopters at once; reversing an approval accepted an application that had
never been approved; accepting an invitation bypassed the
animal-must-be-available rule that the direct path enforced; a job could be
claimed by two workers; a completed analysis was never reused, so the table
grew on every poll; and `INVITATION_EXPIRY_HOURS` was loaded from
configuration and then ignored in favour of a hard-coded 72.

None of those is visible from a happy path. Each is now a passing regression
test — and the ones that were still open were left as `xfail(strict=True)`, so
fixing one turned the suite red until the marker went, which is how the sprint
knew when it was done.

### R4 shaped the architecture, not just the prompt

"No LLM call in a request path" is why the agent is a polling process rather
than an inline call, why the scorer is deterministic Python shared by both
tiers, and why natural-language search enqueues a job and redirects instead of
blocking for sixteen seconds. It is also why an Ollama outage degrades the
product to *scores without prose* rather than to no service.

"Never invents facts" became a **check**, not an instruction: the agent's
answer is verified against the passages it actually retrieved, and a citation
naming something that was not retrieved is dropped and the drop recorded.

### R5 is why this document is shorter than it was

A requirements audit found roughly fifty documentation claims the code did not
satisfy — tests that did not exist, wrong filenames, wrong routes, features
marked planned that were built and built that were not. Every one is fixed,
and the guard test now makes that class of error fail the build rather than
fail in front of an examiner.

---

## 5. Environment facts carried in the rules

Two things cost real debugging time and are now written down so they cost
nothing again:

- **Corporate TLS interception.** A Palo Alto "Forward Trust CA" re-signs
  HTTPS. Python must call `truststore.inject_into_ssl()` before any HTTPS
  client is constructed, or every outbound request fails certificate
  verification — Windows trusts the CA; Python's bundled list does not. It is
  done at import time in `app/config.py`, which the Flask factory, the scripts
  and the tests all import first, and again in `agent_service/__main__.py` for
  the standalone agent process.
- **The virtual environment lives outside the project.** The project sits in
  OneDrive, and OneDrive syncing a venv corrupts it: Python fails to read its
  own `site-packages` and raises a *different* error on each run. There must
  never be a `.venv` inside this directory. The interpreter is
  `C:\Users\libbyb\venvs\petmatch\Scripts\python.exe`, and both the `testing`
  and `db-management` skills state it.
- **SQL Server constraint naming.** Constraint names must be unique per
  *database*, not per table. Four tables share a `status` column, so
  hand-written names collided. A metadata naming convention now prefixes every
  name with its table.
