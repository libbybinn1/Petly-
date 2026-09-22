# SKILLS AND RULES — PetMatch

**Instructions that guide the coding agent on this project.**
Source: course blueprint §15.

Blueprint §15 requires: at least one skill from `sh.skills`, at least one
skill defined by the students, at least one Rule, and that Rules guide the
coding agent on *how to work on this project* rather than containing only
general advice.

---

## 1. Skills

### 1.1 Installed from the skills.sh ecosystem

`skills.sh` resolves skills from GitHub repositories and installs them as
`SKILL.md` folders under `.claude/skills/`. Both of these were installed
that way, from `anthropics/skills`.

| Skill | Why this project needs it |
|---|---|
| **`mcp-builder`** | "Guide for creating high-quality MCP servers... in Python (FastMCP)". Directly serves blueprint §8 — our two local stdio tools. |
| **`webapp-testing`** | "Toolkit for interacting with and testing local web applications using Playwright." Directly serves blueprint §17 — the E2E suite. |

Both were chosen because they map onto graded checklist items, not because
they were convenient to install.

> Note: `skills.sh` itself was returning HTTP 503 when this project was
> built, so the skills were installed from their upstream repository using
> the same mechanism the CLI uses. The install path and format are identical.

### 1.2 Defined for this project

| Skill | Purpose |
|---|---|
| **`clean-code`** | The style contract: Single Responsibility, descriptive unabbreviated names, guard clauses, maximum nesting depth of three, extracted helpers, strict typing, Google-convention docstrings. Includes a before/after example and a pre-commit checklist. |
| **`db-management`** | One command surface for the database: `check`, `create`, `reset`, `seed`, `fresh`, `tables`, `events`. Also records the SQL Server 2014 constraints that shaped the schema, so they are not rediscovered painfully. |
| **`testing`** | Runs a named suite (`unit`, `integration`, `api`, `agent`, `e2e`, `fast`, `all`) and states what each suite must prove. Encodes two rules: never assert on generated text, and scoring tests never touch an LLM. |

`clean-code` is not advisory. Its rules are enforced mechanically by ruff:

| Rule in the skill | Ruff rule |
|---|---|
| Early returns, no nested happy paths | `RET`, `SIM` |
| Maximum nesting depth | `PLR` `max-branches = 8` |
| Strict typing | `ANN` + `mypy --strict` |
| Comprehensive docstrings | `D` (google convention) |
| No `print()` in application code | `T20` |
| Timezone-aware datetimes | `DTZ` |

The `DTZ` rule is not cosmetic here: the 72-hour invitation expiry is wrong
if a naive datetime ever enters the calculation.

---

## 2. Rules

The five binding rules live in `CLAUDE.md` at the repository root, where the
coding agent reads them automatically. Summarised:

### R1 — Ambiguity Rule
Do not begin planning or implementation while a major requirement is
ambiguous. Stop and ask.

Blueprint §15 names this as the recommended rule. It has already paid for
itself twice on this project:

- The specification named the cloud database "SOMY.com". It is actually
  **Somee.com**, running **SQL Server 2014**, not PostgreSQL. Guessing would
  have produced an entire data layer against the wrong engine and dialect.
- The LLM provider was left open. Guessing a cloud vendor would have made
  the project depend on an API key that does not exist.

### R2 — Architecture Rule
Layer boundaries with an explicit import table: controllers hold no business
logic; the domain imports no framework; only repositories touch a session;
commands never return read data; the agent never imports the Flask app.

### R3 — Testing Rule
Every feature needs unit coverage, integration or API coverage, and **at
least one negative test**. Never assert on generated text. Scoring tests run
with no LLM.

### R4 — Agent Rule
The agent never makes the final adoption decision, never states facts absent
from its sources, and always records which sources it used. RAG first, web
search only when the knowledge base cannot answer, never for the
application's own records.

R4 also carries a measured performance constraint: **no LLM call may sit in
a request/response path**, because CPU-only inference on this machine takes
11–16 seconds per call.

### R5 — Documentation Rule
Code and `docs/` may never contradict. A feature change updates both in the
same commit.

---

## 3. How the rules shaped the code

Rules that never change a decision are decoration. These did:

| Rule | Concrete consequence |
|---|---|
| R1 | Stopped before Phase 1 and asked six questions; discovered the database was SQL Server, not PostgreSQL. |
| R2 | `MessageBus` dispatches commands and queries through separate paths, and **rolls query sessions back** so an accidental write cannot persist. |
| R2 | The agent talks to the app through the `analysis_jobs` table, not an import. |
| R3 | `test_cqrs_separation.py` fails the build if a query handler opens a write transaction. |
| R4 | Web search sits behind an explicit gate function, so "did the agent correctly decide *not* to search?" is testable. |
| R4 | The deterministic scorer ranks synchronously; the agent explains asynchronously. |
| R5 | `MODEL_DATA.md` records the SQL Server 2014 limitations that dictated `NVARCHAR(MAX)` payloads and UUID keys. |

---

## 4. Environment notes carried in the rules

Two environment facts cost real debugging time and are now written down so
they cost nothing again:

- **Corporate TLS interception.** A Palo Alto "Forward Trust CA" re-signs
  HTTPS. Python must call `truststore.inject_into_ssl()` before any HTTPS
  client is constructed, or every outbound request fails certificate
  verification. Windows trusts the CA; Python's bundled list does not.
- **SQL Server constraint naming.** Constraint names must be unique per
  *database*, not per table. Four tables share a `status` column, so
  hand-written names collided. A metadata naming convention now prefixes
  every name with its table.
