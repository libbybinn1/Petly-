# TESTING — PetMatch

**Test strategy, the real inventory, and how to run every suite.**
Sources: course blueprint §17; PetMatch spec §26; CLAUDE.md R3.

Testing is part of the design, not a final cleanup step. Every test states
what it proves, as blueprint §17 requires, and §3 below is generated from the
code rather than written from memory — `tests/unit/test_docs_reference_real_artifacts.py`
fails the build if this document names a test that nothing collects.

---

## 1. The five suites

| Suite | Marker | Scope | Tests | External dependencies |
|---|---|---|---|---|
| Unit | `unit` | Scoring, eligibility, state machines, validation, formatting, the architecture guard | 333 | none |
| Integration | `integration` | Event store, approval cascade, invitation lifecycle, projection replay | 87 | in-memory SQLite |
| API | `api` | Endpoints, authentication, authorization, validation, CSRF | 300 | in-memory SQLite |
| Agent | `agent` | Reason-act loop, RAG, MCP tools, job queue, output structure | 141 | Ollama and ChromaDB for the `slow` subset only |
| E2E | `e2e` | Complete browser journeys and accessibility | 49 | Playwright browser, a seeded SQLite file |

**910 tests collected in total.** No suite carries an expected failure: the
`xfail` markers the QA sweep left behind have all been flipped as their bugs
were fixed, and the documentation guard's marker went with the dead references
it was recording.

Those counts are the collection as this document was written. Reproduce them
with `-m pytest --collect-only -q`, and note that
`scripts/verify_requirements.py` reports a smaller figure - it counts test
*functions*, while pytest counts *cases*, and a parametrized function is one
of the former and many of the latter.

### What runs against what

This is worth being precise about, because the honest answer is not the
flattering one.

- **Integration and API tests run on SQLite**, in memory, built fresh per
  test. What they exercise is routing, authorization, validation, the event
  store's semantics and the cascade — none of which depends on the SQL
  dialect, and all of which would be painfully slow against a shared
  free-tier server.
- **E2E tests also run on SQLite**, a seeded file per session, with the
  server started as a subprocess pointed at it through `LOCAL_DATABASE_URL`
  (`tests/e2e/conftest.py`). They originally ran against Somee; the free tier
  throttles under the request fan-out of a browser page load, so the suite
  was measuring a hosting tier's rate limits rather than user journeys.
- **What genuinely exercises SQL Server 2014** is therefore
  `scripts/check_environment.py`, `scripts/db.py` and the running
  application itself.

The trade-off has a sharp edge and it is named here rather than discovered
later: **SQLite accepts a string longer than its column; SQL Server truncates
or errors.** Every length bug the QA sweep found was invisible to a
SQLite-backed suite. That is why `tests/unit/test_seed_data.py` reads the
column widths off the ORM models and asserts every seeded string fits, and
why `tests/api/test_qa_input_validation.py::TestStringsLongerThanTheirColumn`
checks the same at the HTTP boundary.

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
- the tool the agent selected was one the manifest offered

### 2.2 Scoring tests never touch an LLM

Matching arithmetic is deterministic by design (spec §8). Scoring tests run
offline, with no network and no model, and assert exact values. This is what
makes the score defensible: a reviewer can compute it by hand.
`tests/unit/test_qa_matching_edges.py` sweeps 2,592 input combinations and
asserts every one lands inside 0–100 and scores byte-identically on repeat.

---

## 3. The inventory — what each file proves

Grouped by suite. Each row names a real file and a representative test whose
docstring is quoted verbatim.

### 3.1 Unit — `tests/unit/`, 333 tests, no I/O

| File | Tests | Representative test | Proves |
|---|---|---|---|
| `tests/unit/test_matching.py` | 24 | `test_identical_input_gives_identical_score` | The same pair scores the same every time, with no LLM involved. |
| | | `test_disqualification_scores_zero_and_carries_a_reason` | A violation cannot be masked by strong soft scores. |
| | | `test_weighted_contributions_sum_to_the_total` | The published breakdown actually adds up to the score shown. |
| `tests/unit/test_qa_matching_edges.py` | 26 | `test_every_combination_scores_inside_zero_to_one_hundred` | No input mix can produce a score the database CHECK would refuse. |
| | | `test_children_present_with_an_unknown_age_fails_safe` | A null `youngest_child_age` is treated as the risky case. |
| `tests/unit/test_feature_age_criterion.py` | 19 | `test_each_direction_sums_to_one` | The weights are a distribution, not an arbitrary total. |
| | | `test_being_outside_the_band_is_never_a_disqualification` | An age mismatch is soft, as a preference should be. |
| `tests/unit/test_qa_state_machines.py` | 64 | `test_illegal_move_raises` | The guard refuses every pair absent from the transition table, exhaustively. |
| | | `test_reopen_ignores_a_closure_caused_by_a_different_approval` | The reopen selection keys on the causing approval, not on CLOSED. |
| `tests/unit/test_invitation_rules.py` | 27 | `test_expiry_is_exactly_seventy_two_hours_after_sending` | The arithmetic, not just the constant. |
| | | `test_naive_timestamp_is_rejected` | A naive datetime cannot silently produce a wrong window. |
| `tests/unit/test_profile_validation.py` | 28 | `test_every_problem_is_reported_together` | The form is fixable in one pass. |
| | | `test_opt_in_defaults_to_false` | An unticked box means no, not unspecified. |
| `tests/unit/test_feature_animal_rules.py` | 30 | `test_a_submission_with_no_photo_is_rejected` | FR-4.2 is enforced at validation, with a readable message. |
| | | `test_the_guard_raises_for_an_empty_set` | The write-side guard refuses independently of the form. |
| `tests/unit/test_qa_fact_mapping.py` | 26 | `test_an_unrecognised_species_is_dropped_not_turned_into_other` | A corrupt preference cannot invent a preference for OTHER animals. |
| `tests/unit/test_feature_date_formatting.py` | 14 | `test_a_naive_timestamp_is_read_as_utc` | A value straight out of a DATETIME column formats correctly. |
| `tests/unit/test_feature_csrf_tokens.py` | 5 | `test_every_post_form_carries_a_token` | No form was missed when CSRF protection went in. |
| `tests/unit/test_seed_data.py` | 35 | `test_every_animal_string_fits_its_column` | No roster text would be rejected by SQL Server. |
| `tests/unit/test_architecture_guard.py` | 21 | see §3.6 | CLAUDE.md R2, enforced mechanically. |
| `tests/unit/test_docs_reference_real_artifacts.py` | 8 | see §3.7 | CLAUDE.md R5, enforced mechanically. |
| `tests/unit/test_verify_requirements.py` | 6 | `test_every_check_returns_a_bool_and_a_string_without_raising` | Each entry in `ALL_CHECKS` yields a verdict, never an exception. |

### 3.2 Integration — `tests/integration/`, 87 tests, in-memory SQLite

| File | Tests | Representative test | Proves |
|---|---|---|---|
| `tests/integration/test_approval_cascade.py` | 18 | `test_approval_closes_other_active_applications` | The spec §7.5 cascade runs. |
| | | `test_closure_records_which_approval_caused_it` | Causation is recorded, not merely the CLOSED status. |
| | | `test_withdrawn_application_is_not_resurrected` | The rule that makes event sourcing necessary here. |
| `tests/integration/test_event_sourcing.py` | 14 | `test_every_application_replays_to_its_stored_state_after_a_cascade` | The log explains the database after the most complex write. |
| | | `test_a_corrupted_row_is_detected` | The rebuild is a real check, not a formality. |
| | | `test_a_read_only_rebuild_changes_nothing` | The default mode is safe to run against real data. |
| `tests/integration/test_invitation_flow.py` | 15 | `test_accepting_creates_an_application_not_an_approval` | Spec §7.4, the distinction the whole feature turns on. |
| | | `test_cannot_respond_after_the_window_closes` | The 72-hour deadline is enforced against real stored data. |
| `tests/integration/test_qa_cascade_and_concurrency.py` | 24 | `test_one_animal_cannot_be_approved_for_two_adopters` | An animal already in adoption cannot be approved for a second home. |
| | | `test_the_database_itself_forbids_duplicate_active_applications` | FR-7.2 rests on a constraint, not only on a handler check. |
| `tests/integration/test_qa_event_store.py` | 16 | `test_a_duplicate_sequence_number_is_refused_by_the_database` | The optimistic-concurrency claim in `append`'s docstring holds. |
| | | `test_the_store_exposes_no_way_to_change_or_delete_an_event` | FR-13.2 by the shape of the API, not by convention. |

### 3.3 API — `tests/api/`, 300 tests, in-memory SQLite

| File | Tests | Representative test | Proves |
|---|---|---|---|
| `tests/api/test_authorization.py` | 35 | `test_unknown_email_gives_the_same_response_as_a_wrong_password` | The form cannot be used to discover registered addresses. |
| | | `test_adopter_is_refused_staff_pages` | 403, not a redirect and not a rendered page. |
| `tests/api/test_qa_authorization.py` | 44 | `test_a_post_without_a_csrf_token_is_rejected` | A forged cross-site form post cannot act as the signed-in user. |
| | | `test_anonymous_status_matches_the_documented_table` | The permission table in `docs/UX.md` §4 describes the real server. |
| | | `test_a_nonexistent_record_and_someone_elses_look_the_same` | Identifiers cannot be enumerated by comparing the two answers. |
| `tests/api/test_qa_input_validation.py` | 52 | `test_a_sql_payload_in_the_search_term_is_treated_as_text` | The search is parameterised: the tables survive and nothing leaks. |
| | | `test_no_template_disables_autoescaping` | The escaping guarantee is not undone somewhere in the views. |
| `tests/api/test_forms_and_validation.py` | 25 | `test_application_is_created_and_queues_an_analysis` | The happy path writes the application *and* the agent job. |
| | | `test_registration_cannot_grant_itself_staff` | An extra form field cannot escalate privilege. |
| `tests/api/test_feature_staff_decision.py` | 10 | `test_approving_records_the_approval_and_reserves_the_animal` | The approval reaches the database through the route. |
| | | `test_approving_twice_is_refused_rather_than_repeated` | A double submit cannot approve an already-approved record. |
| `tests/api/test_feature_animal_lifecycle.py` | 18 | `test_an_animal_without_a_photo_is_refused` | FR-4.2 is enforced, and nothing is written when it fails. |
| | | `test_an_edit_cannot_remove_the_last_photograph` | FR-4.2 is checked on edit, not only on creation. |
| `tests/api/test_feature_notifications.py` | 14 | `test_marking_another_users_message_read_is_refused` | The identifier in the URL is not trusted. |
| | | `test_an_off_site_target_is_refused` | The redirect target cannot be turned into an open redirect. |
| `tests/api/test_feature_natural_language_search.py` | 18 | `test_no_module_under_app_imports_the_language_model_client` | The web tier cannot make an inference call at all (NFR-3.1). |
| | | `test_a_profile_bound_job_ranks_its_results` | Spec §6.4: the profile ranks what the intent narrowed. |
| `tests/api/test_feature_analysis_status.py` | 12 | `test_the_response_names_no_records` | Polling cannot be used to enumerate anything. |
| `tests/api/test_feature_age_filter.py` | 20 | `test_the_bands_tile_the_whole_range` is its unit twin; here `test_the_youngest_band_excludes_the_two_year_old` | `0-2` is under two, not up to and including two. |
| `tests/api/test_feature_dashboard_and_history.py` | 17 | `test_an_adopter_may_not_read_somebody_elses` | Relaxing the history route did not open it. |
| | | `test_a_tile_without_a_target_is_not_a_link` | A figure with no screen behind it does not pretend otherwise. |
| `tests/api/test_feature_expiry_sweep.py` | 8 | `test_an_overdue_invitation_is_expired_by_an_ordinary_request` | The sweep is actually dispatched, not merely registered. |
| `tests/api/test_feature_auth_boundary.py` | 18 | `test_the_controller_calls_no_session_methods` | No `session.execute`, `session.add` or `session.commit` in the auth controller. |
| `tests/api/test_feature_match_screens.py` | 10 | `test_the_reasoning_trace_and_cited_markers_are_rendered` | The trace reaches the screen at all. |

### 3.4 Agent — `tests/agent/`, 141 tests

| File | Tests | Representative test | Proves |
|---|---|---|---|
| `tests/agent/test_reasoning_loop.py` | 19 | `test_the_manifest_is_sent_to_the_model_on_every_turn` | The model is actually told what it may call (blueprint §8). |
| | | `test_the_step_cap_terminates_a_model_that_never_answers` | `max_reasoning_steps` is a real bound, not a list slice. |
| | | `test_a_model_request_about_our_own_records_never_reaches_the_web` | Rule 2 of spec §13 is enforced against the model, not just documented. |
| | | `test_an_unknown_citation_is_dropped_and_recorded` | A fabricated source cannot reach the database or the screen. |
| `tests/agent/test_agent_loop.py` | 31 | `test_no_search_when_knowledge_base_answers` | RAG is preferred over the web for curated knowledge. |
| | | `test_agent_exposes_no_decision_capability` | The agent cannot finalise an adoption (spec §6.4, rule R4). |
| `tests/agent/test_qa_agent_robustness.py` | 37 | `test_the_score_never_comes_from_the_model` | A model claiming a score cannot change the stored number. |
| | | `test_two_workers_cannot_claim_the_same_job` | A job is claimed by exactly one worker even when claims interleave. |
| `tests/agent/test_worker_jobs.py` | 18 | `test_a_repeated_job_for_unchanged_records_reuses_the_analysis` | The second identical job costs no inference and no second row (NFR-3.3). |
| | | `test_the_reasoning_trace_is_stored_as_json` | The trace survives the process boundary for the interface. |
| `tests/agent/test_rag_retrieval.py` | 13 | `test_retrieval_succeeds_with_no_keyword_overlap` | Retrieval is semantic, which blueprint §7 demands. |
| `tests/agent/test_intent.py` | 18 | `test_blank_text_is_refused_without_calling_the_model` | An empty box does not waste an inference call. |
| `tests/agent/test_mcp_stdio.py` | 5 | `test_server_advertises_both_tools` | Both required stdio tools exist and carry LLM-facing descriptions. |

`tests/agent/test_mcp_stdio.py` and `tests/agent/test_rag_retrieval.py` carry
the `slow` marker: they start a real subprocess and reach a live Ollama host
respectively. Everything else in this suite runs offline against doubles.

The stdio tests spawn the MCP server against a **temporary SQLite database**
they seed themselves, not against Somee. That makes them deterministic and
means they **never skip**: a transport test that quietly skips because the
cloud database was asleep is exactly the test you cannot afford to lose,
because the transport is the whole graded requirement.

### 3.5 E2E — `tests/e2e/`, 49 tests, a real browser

| File | Tests | Representative test | Proves |
|---|---|---|---|
| `tests/e2e/test_journeys.py` | 19 | `test_visitor_searches_filters_and_opens_an_animal` | Blueprint 4.1 and 4.2: search, filter, then view details. |
| | | `test_adopter_sees_ranked_matches_with_scores` | Find My Pet ranks animals from the stored profile (spec §6.1). |
| | | `test_staff_dashboard_shows_operational_figures` | Blueprint 4.4: a dashboard with real numbers and actions. |
| | | `test_discovery_finds_adopters_who_did_not_apply` | Find More Adopters is distinct from applicant ranking (spec §7.3). |
| | | `test_rankings_state_that_a_human_decides` | Spec §6.4 is visible to staff, not only enforced in code. |
| | | `test_adopter_cannot_reach_the_dashboard` | An adopter typing the URL is refused. |
| `tests/e2e/test_accessibility.py` | 30 | `test_screen_has_no_serious_violations` | Every anonymous screen has no serious or critical axe violation. |
| | | `test_screen_passes_in_dark_mode_too` | The dark theme is checked, not merely supported. |
| | | `test_every_page_offers_a_skip_link_to_the_main_landmark` | The skip link is the first tab stop and targets `<main>`. |
| | | `test_an_injected_low_contrast_element_is_reported` | The axe harness actually fails when contrast is wrong. |

The E2E suite covers the browsable part of the nine spec §28 demo scenarios:
public browsing, the adopter journey through matches and applications, the
staff journey through the roster, rankings and dashboard, and the
authorization boundary in a real session. The scenarios a browser is a poor
witness for — the approval cascade, replay, and the agent's tool loop — are
proven by `tests/integration/test_event_sourcing.py`,
`tests/integration/test_approval_cascade.py` and `tests/agent/`. `docs/DEMO.md`
walks all nine by hand for the presentation.

### 3.6 The architecture guard — `tests/unit/test_architecture_guard.py`

21 static tests that turn CLAUDE.md R2's layer table into something that
fails the build rather than something people remember. They parse the source
with `ast`; nothing is imported, so a rule holds even for a module that needs
a database.

| Test | Proves |
|---|---|
| `test_domain_layer_imports_no_framework_or_upper_layer` | `app/domain/` imports no framework or layer above it. |
| `test_query_modules_never_write` | No module under `app/cqrs/queries/` writes. |
| `test_command_handlers_never_return_read_data` | Every `handle()` under `app/cqrs/commands/` returns `str`, `int` or `None`. |
| `test_controllers_never_hold_a_database_session` | Only `app/controllers/helpers.py` may touch SQLAlchemy. |
| `test_agent_and_mcp_modules_never_import_the_web_tier` | `agent_service/` and `mcp_server/` never import the Flask factory, controllers, cqrs or security. |
| `test_agent_never_names_a_decision_command` | The agent never imports or names Approve/Reject/Decide (R4). |
| `test_every_domain_event_type_is_referenced_somewhere` | Every `DomainEventType` member is appended or read somewhere. |

**Each rule also has a test that the rule itself catches a synthetic
violation** — `test_domain_boundary_rule_flags_a_framework_import`,
`test_query_write_rule_flags_a_session_commit`,
`test_command_return_rule_flags_a_dataclass_return` and so on. A guard nobody
has seen fail is a guard nobody should trust.

### 3.7 The documentation guard — `tests/unit/test_docs_reference_real_artifacts.py`

CLAUDE.md R5 says code and `docs/` may never contradict. Three classes of
claim are machine-checkable, and this file checks all three across `docs/*.md`,
`PLANNING.md`, `README.md` and `CLAUDE.md`:

- a backticked name shaped like a test function must be one pytest would
  collect (found by parsing every test module with `ast`, so an `async def`
  counts and a suite needing a browser is still checked);
- a backticked project path under app, agent_service, mcp_server, tests,
  scripts, knowledge, docs or .claude must exist on disk;
- a backticked identifier ending in Command, Query, Handler or Error must be
  a class defined under `app/`, `agent_service/` or `mcp_server/`.

The failure message is a sorted list of `file:line -> missing <kind>`, precise
enough to open and fix. The scanner lives in `scripts/verify_requirements.py`
and is imported here rather than reimplemented, so the CLI count and the test
failure always concern the same references.

### 3.8 The requirements checker — `scripts/verify_requirements.py`

Not a pytest suite; a command-line audit of the 23 mandatory items from
blueprint §20. It answers each by **inspecting behaviour**, never by
substring search:

- it builds the Flask application exactly as `tests/api/conftest.py` does,
  against a throwaway SQLite file, and reads `app.url_map`, the registered
  handlers and the rendered templates;
- it parses the code with `ast` for the layer rules, the event catalogue and
  the agent's import boundary;
- it calls pure functions directly — scoring the same pair twice to prove
  determinism, and driving `decide_whether_to_search` through each of its
  outcomes to prove the web-search policy.

A PASS states what was inspected; a FAIL names the file to open. It never
touches Somee.com or Ollama.

```bash
C:\Users\libbyb\venvs\petmatch\Scripts\python.exe scripts\verify_requirements.py
```

---

## 4. Negative testing

Blueprint §17 requires failure scenarios, not only the happy path, and
CLAUDE.md R3 makes at least one negative test per feature binding. **127 of
the collected tests prove a failure scenario**, counted by
`scripts/verify_requirements.py`. The standing minimum set:

- missing or incomplete adopter profile
- application to an unavailable animal
- duplicate active application, refused by the handler *and* by the index
- a staff operation attempted by an adopter, by direct POST
- one adopter acting on another's application, invitation or notification
- a response to an expired invitation, tested to the microsecond
- invalid form values at the server boundary, including values longer than
  their column
- a POST with no CSRF token
- an off-site `next=` on sign-in, and an off-site notification target
- agent tool failure, dead MCP subprocess, unreachable model, malformed JSON,
  fabricated citation
- a job claimed twice, and a job stranded by a killed worker

There is deliberately **no** test for "database connection loss mid-request".
An earlier version of this document claimed one; nothing simulated it, and a
test that stubs a driver failure would prove the stub works.

---

## 5. Test data

Unit tests construct their objects inline — no fixtures file, no database, so
a reader can see the whole scenario in one place.

Integration and API tests build exactly the rows they need in an in-memory
SQLite database created per test (`tests/api/conftest.py` and the fixtures at
the top of each integration module). They do **not** read the demo seed, so a
reseed cannot change a count a test asserts on.

E2E tests run against `tests/e2e/seed_e2e.py`, a small deterministic seed
written for the browser suite, and sign in with the accounts that module
exports.

`tests/unit/test_seed_data.py` is the exception: it tests the *demo* seed data
itself — the roster and people definitions in `scripts/seed_roster.py` and
`scripts/seed_people.py` — as pure data, with no database involved.

---

## 6. Conventions

- One behaviour per test. A test name states the behaviour, not the method.
- Docstrings begin "Proves…" and say what, per blueprint §17.
- Test classes group by the property under test and carry their own
  one-line docstring.
- No sleeps in E2E; wait on conditions instead.
- Tests that need the network or a live model carry the `slow` marker.
- A known-broken behaviour is `xfail(strict=True)`, never a deleted
  assertion, so fixing it turns the suite red until the marker goes.

---

## 7. Running the suites

The virtual environment is **outside** the project directory, because
OneDrive corrupts a venv it syncs (README.md explains the symptoms). Use this
interpreter everywhere; there is no `.venv` in this repository and there must
not be one.

```bash
set PY=C:\Users\libbyb\venvs\petmatch\Scripts\python.exe

%PY% -m pytest tests -q                       # everything, 910 tests
%PY% -m pytest tests/unit -q                  # 333, offline, seconds
%PY% -m pytest tests/integration -q           # 87
%PY% -m pytest tests/api -q                   # 300
%PY% -m pytest tests/agent -q                 # 141
%PY% -m pytest tests/e2e -q                   # 49, needs a browser
```

By marker, which is what the `testing` skill drives:

```bash
%PY% -m pytest -m unit -q
%PY% -m pytest -m "api and not slow" -q
%PY% -m pytest -m "unit or api" -q            # the pre-commit loop
%PY% -m pytest -m slow -q                     # live Ollama + a real subprocess
```

Useful flags: `-k <expression>` to select by name, `-x` to stop at the first
failure, `-ra` for a summary of everything not passed, `--cov` for coverage,
and for E2E only `--headed` to watch the browser and `--tracing on` to record
a trace.

**Run the E2E suite on its own.** It starts a server subprocess on a free
port and drives a real browser; running it beside the API suite makes both
slower and the failures harder to read.

---

## 8. Quality gates

A change is not finished until all four pass.

```bash
%PY% -m pytest tests -q
%PY% -m ruff check .
%PY% -m mypy --strict app agent_service mcp_server tests scripts
%PY% scripts\verify_requirements.py
```

`mypy --strict` covers `tests` and `scripts` as well as the application.
That is deliberate: a test double whose signature has drifted from the
protocol it stands in for passes at runtime and proves nothing, and a seeding
script that writes the wrong type fails nine minutes into a reseed.

Ruff's configuration is in `pyproject.toml` and enforces the `clean-code`
skill mechanically — `RET` and `SIM` for early returns, `ANN` for annotations,
`D` for Google-convention docstrings, `T20` against `print()` in application
code, and `DTZ` for timezone-aware datetimes, which is not cosmetic here
because the 72-hour invitation window is wrong if a naive datetime enters it.

---

## 9. Coverage

Coverage is reported over `app`, `agent_service` and `mcp_server`
(`pyproject.toml`, `[tool.coverage.run]`). It is a diagnostic, not a target: a
high number with no negative tests would still be a weak suite. The
meaningful measure is §3 — every row names a property the system must hold,
and §4 names the failures it must survive.
