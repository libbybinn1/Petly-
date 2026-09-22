# PetMatch — Rules for the Coding Agent

These are binding rules for anyone (human or AI) writing code in this repository.
They are project-specific instructions, not general advice. Course blueprint §15
requires at least one such rule; this file defines five.

Read `PLANNING.md` for the phased build order and `docs/` for the specifications.

---

## R1 — Ambiguity Rule

**Do not begin planning or implementation while a major requirement is ambiguous.
Stop and ask.**

This is the rule the course blueprint §15 explicitly recommends. It has already
paid for itself twice in this project:

- The spec named the cloud database "SOMY.com". It is actually **Somee.com**, and
  it runs **SQL Server 2014**, not PostgreSQL. Guessing would have produced an
  entire data layer against the wrong engine and the wrong SQL dialect.
- The LLM provider was left open. Guessing a cloud vendor would have made the
  project depend on an API key that does not exist.

When a requirement is unclear: do the work that does not depend on the answer,
state the assumption explicitly, and ask a consolidated question. Never invent a
requirement and never silently pick a default for a decision the spec flagged as
open (see spec §30).

---

## R2 — Architecture Rule

The layer boundaries are not stylistic. Violating them breaks the MVC and CQRS
demonstrations the project is graded on.

| Layer | May import | Must never |
|---|---|---|
| `controllers/` | services, cqrs, security | contain business rules, touch a DB session |
| `views/` (Jinja) | view models | decide anything; call a service; query |
| `domain/` | stdlib, other domain | import Flask, SQLAlchemy, or any framework |
| `cqrs/commands/` | domain, repositories, eventstore | return read DTOs; render |
| `cqrs/queries/` | repositories (read-only) | mutate state; open a write transaction |
| `repositories/` | SQLAlchemy, domain | contain business rules |
| `agent_service/` | its own modules, MCP client | import `app.*` directly |

Specific prohibitions:

- **A command never returns query data.** It returns an identifier or nothing.
  If a screen needs data after a write, the controller dispatches a query next.
- **A query never writes.** No `INSERT`, `UPDATE`, `DELETE`, no `session.commit()`.
- **The domain layer must be unit-testable with no Flask app context.** If a
  domain test needs `app.test_request_context()`, the design is wrong.
- **The agent is a separate OS process.** It communicates through the
  `analysis_jobs` table and MCP stdio — never by importing the Flask app.

---

## R3 — Testing Rule

No feature is done without tests, and the happy path alone is never enough.

Every feature requires:
1. Unit tests for its pure logic.
2. An integration or API test for its wiring.
3. **At least one negative test** — blueprint §17 mandates failure scenarios.

Additional binding constraints:

- **Never assert on generated text.** The LLM is non-deterministic. Assert that
  output parses against the declared schema, that required fields exist, that
  scores fall in range, and that cited evidence refers to real retrieved sources.
- **Scoring tests never invoke an LLM.** Matching math is deterministic by design
  (spec §8); it must be testable offline and give identical output for identical
  input.
- Every test docstring states **what it proves**, per blueprint §17.

---

## R4 — Agent Rule

Derived from spec §6.4 and §11. These are product requirements, not preferences.

- **The agent never makes the final adoption decision.** It scores, explains and
  recommends. A human staff member decides. No code path may auto-approve.
- **The agent never invents facts.** Every claim in an explanation traces to a
  retrieved RAG chunk, an MCP tool result, or a web search result.
- **Sources are recorded.** When external information materially affects an
  explanation, the source is stored in the `MatchAnalysis` and shown in the UI.
- **RAG first, web second.** Web search runs only when the curated knowledge base
  cannot answer (spec §13). Never use web search to retrieve the application's
  own adopter or animal records — those come from MCP tools.
- **Web content never overrides authoritative application data** or explicit
  business rules.

### Performance constraint — measured, not assumed

On this machine (Intel Core Ultra 5 125U, CPU-only inference) the models were
measured at:

| Model | JSON generation | Tool call |
|---|---|---|
| `qwen2.5:3b-instruct` | 16.2 s | 11.3 s |
| `qwen2.5:7b-instruct` | ~2x the above | ~2x the above |

This has a direct design consequence that must be respected:

> **Never put an LLM call in a request/response path.** Ranking N candidates with
> N LLM calls would take minutes. Instead: the *deterministic* scorer ranks every
> candidate instantly, and the agent generates explanations only for the top
> results, asynchronously, via the `analysis_jobs` queue. Results are cached in
> `MatchAnalysis` and never recomputed for unchanged inputs.

This is also why the agent is a separate polling process rather than an inline
call — the architecture and the hardware reality agree.

---

## R5 — Documentation Rule

Code and `docs/` may never contradict each other.

- A feature change updates its `docs/` section in the **same commit**.
- Every public function docstring cites the spec section it implements.
- `PLANNING.md` phase checkboxes are updated as phases complete, not in a batch
  at the end.
- If implementation reveals the spec is wrong, fix the document and say so —
  do not let the code silently diverge.

---

## Style

See the `clean-code` skill. Summary: Single Responsibility, descriptive
unabbreviated names, early returns, maximum nesting depth 3, strict typing,
Google-convention docstrings. `ruff check` and `mypy --strict` must pass.

## Environment notes

- **Corporate TLS interception.** An SSL-inspecting firewall (Palo Alto "Forward
  Trust CA") re-signs HTTPS. Python must call `truststore.inject_into_ssl()` at
  startup or every outbound HTTPS request fails certificate verification. This is
  done once in `app/__init__.py` and `agent_service/__main__.py`.
- **SQL Server 2014 has no JSON type.** Serialize to `NVARCHAR(MAX)` in Python.
  See the `db-management` skill for the full list of engine constraints.
- **Secrets live in `.env`**, which is gitignored. Never commit credentials,
  never hardcode them, never paste them into documentation.
