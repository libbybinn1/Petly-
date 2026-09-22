# TESTING — PetMatch

**Test strategy, suites and scenarios.**
Sources: course blueprint §17; PetMatch spec §26.

Testing is part of the design, not a final cleanup step. Every test states
what it proves, as blueprint §17 requires.

---

## 1. The five suites

| Suite | Marker | Scope | External dependencies |
|---|---|---|---|
| Unit | `unit` | Scoring, eligibility, state machines, pure logic | none |
| Integration | `integration` | Repositories, event store, projections | database |
| API | `api` | Endpoints, authentication, authorization, validation | database |
| Agent | `agent` | Agent loop, RAG, MCP tools, output structure | Ollama, ChromaDB |
| E2E | `e2e` | Complete browser journeys | running app, Playwright |

Run any of them through the `testing` skill:

```bash
.venv/Scripts/python.exe scripts/test.py unit
.venv/Scripts/python.exe scripts/test.py api -k authorization
.venv/Scripts/python.exe scripts/test.py e2e --headed
```

---

## 2. Two rules that shape every test

### 2.1 Never assert on generated text

The LLM is non-deterministic. A test that asserts an explanation contains a
particular sentence will fail randomly and teach the team to ignore failures.

Assert instead on:
- the output parses as the declared JSON schema
- required fields are present and correctly typed
- the score falls in 0–100
- cited evidence refers to sources that were actually retrieved
- the tool the agent selected was a legal choice for that task

### 2.2 Scoring tests never touch an LLM

Matching arithmetic is deterministic by design (spec §8). Scoring tests run
offline, with no network and no model, and assert exact values. This is what
makes the score defensible: a reviewer can compute it by hand.

---

## 3. What each suite proves

### 3.1 Unit

| Test | Proves |
|---|---|
| `test_adopter_to_animal_scoring` | Identical input yields an identical score; each criterion contributes its documented weight. |
| `test_animal_to_adopter_scoring` | The reverse direction uses different weights (spec §9), so the same pair can score differently each way. |
| `test_hard_constraints_disqualify` | A hard-constraint violation disqualifies outright rather than merely lowering the score. |
| `test_soft_preferences_reduce_only` | A soft mismatch reduces the score but never disqualifies. |
| `test_eligibility_rules` | Incomplete profile, opted-out, inactive account and unavailable animal each exclude a candidate (spec §10). |
| `test_application_state_machine` | Every legal transition is allowed and every illegal one raises. |
| `test_invitation_expiry` | An invitation expires exactly 72 hours after sending, using timezone-aware arithmetic. |
| `test_cqrs_separation` | No query handler opens a write transaction; no command handler returns a read DTO. |

### 3.2 Integration

| Test | Proves |
|---|---|
| `test_event_append_and_sequence` | Sequence numbers increment per aggregate, and the unique constraint rejects a duplicate. |
| `test_replay_equals_live_state` | State rebuilt by replaying `domain_events` is identical to the projected current state. |
| `test_approval_closes_other_applications` | Approving one application closes the adopter's other active ones and records the causing id (spec §7.5). |
| `test_reopen_only_cascade_closed` | Reversing an approval makes exactly those applications reopenable — not ones withdrawn or rejected for other reasons. |
| `test_event_log_is_append_only` | The repository exposes no update or delete path. |
| `test_animal_requires_image` | An animal cannot be published without at least one image (spec §24). |

### 3.3 API

| Test | Proves |
|---|---|
| `test_login_rejects_bad_password` | Failure returns 401 and does not reveal whether the email exists. |
| `test_inactive_account_refused` | A deactivated account cannot sign in. |
| `test_adopter_cannot_reach_staff_routes` | Every staff endpoint returns 403 to an adopter, including direct POSTs with forged fields. |
| `test_anonymous_redirected` | Protected routes return 401 rather than leaking content. |
| `test_adopter_cannot_read_another_profile` | Changing an id in the URL does not expose another person's data. |
| `test_application_requires_available_animal` | Applying to a reserved or adopted animal is rejected. |
| `test_duplicate_active_application_rejected` | The partial unique index is enforced and surfaces a clean error. |
| `test_form_validation_server_side` | The server rejects everything the browser would have blocked. |

### 3.4 Agent

| Test | Proves |
|---|---|
| `test_output_matches_schema` | Every response parses into the declared structure with all required fields. |
| `test_rag_retrieval_is_semantic` | A query sharing no keywords with the target chunk still retrieves it (blueprint §7). |
| `test_mcp_tools_over_stdio` | The MCP server is spawned as a real subprocess and both tools round-trip over stdio. |
| `test_web_search_gated_by_policy` | The agent does **not** search the web when RAG answers the question, and does when it cannot (spec §13). |
| `test_web_search_never_used_for_own_records` | Adopter and animal data are fetched through MCP, never through search. |
| `test_agent_never_auto_approves` | No code path lets the agent finalise an adoption (spec §6.4). |
| `test_evidence_traces_to_sources` | Every cited source appears in what was actually retrieved. |
| `test_llm_failure_degrades_gracefully` | A model timeout marks the job failed with a message; it does not crash the process. |
| `test_malformed_json_is_retried` | An unparseable response triggers a bounded retry rather than propagating. |

### 3.5 E2E

Covering the nine demo scenarios from spec §28:

| Test | Journey |
|---|---|
| `test_adopter_registers_and_completes_profile` | Register → profile → opt in |
| `test_find_my_pet` | Profile-based recommendations with explanations |
| `test_natural_language_search` | Free-text intent → relevant results |
| `test_adopter_submits_multiple_applications` | Several applications, all visible |
| `test_staff_reviews_ranked_applicants` | Find My Adopter with scores and analysis |
| `test_staff_discovers_more_adopters` | Find More Adopters returns opted-in non-applicants |
| `test_invitation_round_trip` | Staff sends → adopter sees in inbox → responds |
| `test_approval_closes_other_applications` | Cascade visible in the interface, history preserved |
| `test_staff_dashboard` | Statistics and recent activity render |

---

## 4. Negative testing

Blueprint §17 requires failure scenarios, not only the happy path. Every
feature carries at least one negative test. The minimum set:

- missing or incomplete adopter profile
- application to an unavailable animal
- duplicate active application
- unauthorized staff operation attempted by an adopter
- expired invitation response attempt
- invalid form values at the server boundary
- agent tool failure and malformed model output
- database connection loss mid-request

## 5. Test data

Unit tests construct their objects inline — no fixtures file, no database, so
a reader can see the whole scenario in one place.

Integration, API and agent tests use the seed data, which is deterministic
(`RANDOM_SEED = 20260922`), so counts and orderings are stable across runs.

E2E tests run against a freshly seeded database and sign in with the demo
accounts.

## 6. Conventions

- One behaviour per test. A test name states the behaviour, not the method.
- Docstrings state what the test proves, per blueprint §17.
- No sleeps in E2E; wait on conditions instead.
- Tests that need the network or a model are marked `slow` so the fast loop
  stays fast.

## 7. Coverage

Coverage is reported over `app`, `agent_service` and `mcp_server`. It is a
diagnostic, not a target: a high number with no negative tests would still be
a weak suite. The meaningful measure is the table in §3 — every row names a
property the system must hold.
