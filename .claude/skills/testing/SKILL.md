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

| Suite | Marker | Scope | Needs |
|---|---|---|---|
| `unit` | `unit` | Scoring, eligibility, state machines. Pure logic. | nothing |
| `integration` | `integration` | Repositories, event store, projections. | database |
| `api` | `api` | Endpoints, auth, authorization, validation. | database |
| `agent` | `agent` | Agent loop, RAG, MCP tools, output structure. | Ollama, Chroma |
| `e2e` | `e2e` | Full browser journeys. | running app + Playwright |
| `all` | — | Everything | all of the above |
| `fast` | `unit or api` | Pre-commit sanity check, no LLM | database |

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

```bash
# Fast loop while developing scoring logic
scripts/test.py unit -k adopter_to_animal

# Did I break authorization?
scripts/test.py api -k authorization

# Watch the staff journey run in a real browser
scripts/test.py e2e -k staff_journey --headed

# What isn't covered?
scripts/test.py all --cov
```

## What each suite must prove

Per course blueprint §17, every test documents what it proves. Hold to this:

- **Unit** — a scoring function returns identical output for identical input, and
  each criterion is independently correct. Hard constraints disqualify;
  soft preferences only reduce the score.
- **Integration** — state rebuilt by replaying `domain_events` equals the live
  projected state. A closed application can be reopened from history.
- **API** — every endpoint rejects every role that must not reach it, returning
  403 even when the UI button is hidden and the request is forged directly.
- **Agent** — output parses as the declared JSON schema; the agent selects tools
  rather than always calling them; RAG is consulted before web search; a failing
  tool degrades gracefully instead of crashing the loop.
- **E2E** — the nine demo scenarios in spec §28 complete in a real browser.

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
