---
name: testing
description: Run and debug PetMatch test suites. Use when running pytest or Playwright tests, triggering a specific suite (unit, integration, api, agent, e2e), investigating a failure, or checking coverage.
---

# Testing — PetMatch

Run pytest by marker. Note the interpreter path: the virtual environment is
**outside** the project, because OneDrive corrupts a venv it syncs.

```bash
C:\Users\libbyb\venvs\petmatch\Scripts\python.exe -m pytest tests/ -q
C:\Users\libbyb\venvs\petmatch\Scripts\python.exe -m pytest -m unit -q
```

If a test fails with `OSError: [Errno 22]`, an `AttributeError` raised from
inside `site-packages`, or a different import error on each run, the
interpreter in use is a venv inside OneDrive. Use the external one.

## Suites

| Suite | Marker | Scope | Tests | Needs |
|---|---|---|---|---|
| unit | `unit` | Scoring, eligibility, state machines, validation, the architecture and documentation guards | 325 | nothing |
| integration | `integration` | Event store, approval cascade, invitation lifecycle, replay | 87 | in-memory SQLite |
| api | `api` | Endpoints, auth, authorization, validation, CSRF | 300 | in-memory SQLite |
| agent | `agent` | Reason-act loop, RAG, MCP tools, job queue, output structure | 141 | Ollama and a subprocess for the `slow` subset only |
| e2e | `e2e` | Browser journeys and accessibility | 49 | Playwright browser; the suite starts its own server against a seeded SQLite file |

902 tests in total, with no expected failures. `slow` marks the few that need
a live model or a real subprocess.

## Options

| Flag | Effect |
|---|---|
| `-k <expr>` | Run tests matching an expression |
| `--cov` | Coverage report with missing lines |
| `--failed` | Re-run only last run's failures |
| `-x` | Stop at first failure |
| `--headed` | E2E only: watch the browser |
| `--trace` | E2E only: record a Playwright trace for debugging |

## Examples

There is **no `scripts/test.py`**. Run pytest directly, selecting a suite by
path or by marker.

```bash
set PY=C:\Users\libbyb\venvs\petmatch\Scripts\python.exe

# Fast loop while developing scoring logic
%PY% -m pytest tests/unit -k matching -q

# Did I break authorization?
%PY% -m pytest tests/api -k authorization -q

# The pre-commit loop: everything that needs no model and no browser
%PY% -m pytest -m "unit or api" -q

# Watch a journey run in a real browser
%PY% -m pytest tests/e2e -k staff_journey --headed

# Only the tests that need a live model or a subprocess
%PY% -m pytest -m slow -q

# What isn't covered?
%PY% -m pytest tests --cov
```

Run the E2E suite **on its own**. It starts a server subprocess on a free
port and drives a real browser; running it beside the API suite makes both
slower and the failures harder to read.

## Quality gates

A change is not finished until all four pass.

```bash
%PY% -m pytest tests -q
%PY% -m ruff check .
%PY% -m mypy --strict app agent_service mcp_server tests scripts
%PY% scripts\verify_requirements.py
```

`mypy --strict` covers `tests` and `scripts` as well as the application. That
is deliberate: a test double whose signature has drifted from the protocol it
stands in for passes at runtime and proves nothing, and a seeding script that
writes the wrong type fails nine minutes into a reseed.

## What each suite must prove

Per course blueprint §17, every test documents what it proves. Hold to this:

- **Unit** — a scoring function returns identical output for identical input, and
  each criterion is independently correct. Hard constraints disqualify;
  soft preferences only reduce the score.
- **Integration** - state rebuilt by replaying `domain_events` equals the live
  projected state (`app/eventstore/projections.py`), and a cascade-closed
  application - and only a cascade-closed one - reopens when the causing
  approval is reversed.
- **API** — every endpoint rejects every role that must not reach it, returning
  403 even when the UI button is hidden and the request is forged directly.
- **Agent** — output parses as the declared JSON schema; the agent selects tools
  rather than always calling them; RAG is consulted before web search; a failing
  tool degrades gracefully instead of crashing the loop.
- **E2E** - the browsable part of the spec section 28 scenarios completes in a
  real browser: public browsing, the adopter journey, the staff journey,
  authorization, and the accessibility promises. The cascade, replay and the
  agent loop are proven by the integration and agent suites instead.

## Negative tests are mandatory

Blueprint §17 requires failure scenarios, not just the happy path. Every feature
needs at least one test proving it refuses bad input: missing profile, invalid
application, unauthorized staff operation, unavailable animal, expired invitation.

## Debugging a failure

1. Read the assertion, not the stack trace bottom. `pytest -ra` prints a summary.
2. Re-run the single test with `-k` and `-x`.
3. For E2E, add `--headed --trace`, then open the trace to see each browser step.
4. For agent tests, the LLM is non-deterministic: assert on **structure and
   constraints**, never on exact wording.

## Rule: the LLM is never in a deterministic test

Scoring is deterministic and unit-tested with no LLM involved. The agent's
*explanations* are generated, so tests assert that the explanation exists,
references real evidence, and parses — never that it says a specific sentence.
