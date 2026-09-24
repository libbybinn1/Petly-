# FEATURES — PetMatch

Every feature is documented with the template from course blueprint §16:
**Feature Name · User Story · Preconditions · Main Flow · Alternative and
Error Flows · Acceptance Criteria · API/Data Changes · UI Changes ·
Agent/Tool Interaction · Tests.**

Where a section reads "None", that is the answer, not an omission. Every file
named under **Tests** exists and is collected —
`tests/unit/test_docs_reference_real_artifacts.py` fails the build otherwise.

Status legend: **Done** · **Partial** · **Not built**

| # | Feature | Status |
|---|---|---|
| F-01 | Account registration and sign-in | Done |
| F-02 | Role-based authorization | Done |
| F-03 | Staff animal roster (tabular display) | Done |
| F-04 | Structured animal search | Done |
| F-05 | Animal details view | Done |
| F-06 | Adopter profile | Done |
| F-07 | Adoption application | Done |
| F-08 | Approval cascade and reopen | Done |
| F-09 | Deterministic matching engine | Done |
| F-10 | MCP tool server | Done |
| F-11 | RAG knowledge base | Done |
| F-12 | Autonomous agent process | Done |
| F-13 | Find My Pet | Done |
| F-14 | Natural-language search with profile fusion | Done |
| F-15 | Find My Adopter | Done |
| F-16 | Find More Adopters | Done |
| F-17 | Invitations and the expiry sweep | Done |
| F-18 | Notification inbox | Done |
| F-19 | Staff dashboard | Done |
| F-20 | Activity history | Done |
| F-21 | Animal lifecycle management | Done |
| F-22 | Staff decision on an application | Done |
| F-23 | Analysis status and the pending state | Done |
| F-24 | Event replay and projection rebuild | Done |
| F-25 | Age-band filter | Done |
| F-26 | Reasoning-trace display | Done |
| F-27 | CSRF protection | Done |

---

## F-01 Account registration and sign-in — Done

**User story.** As a visitor, I want to create an account and sign in, so
that I can maintain a profile and apply for animals.

**Preconditions.** None.

**Main flow.** Visitor submits name, email and password → the server
validates → the password is hashed with Werkzeug's salted algorithm →
`RegisterAdopterCommand` creates the account with role ADOPTER → the user is
signed in and sent to the landing page.

**Alternative and error flows.**
- Email already registered → 409, form redisplayed with the entered values.
  The polite pre-check and the unique index both refuse it; the index is what
  settles two overlapping registrations.
- Password under 8 characters, mismatched confirmation, blank name, a name
  over 150 characters, or a malformed address → 400, with **every** problem
  listed at once rather than one per reload.
- Wrong credentials at sign-in → 401 with a single message that does not
  reveal whether the email exists.
- Deactivated account → 403, with its own distinct message.
- `?next=` pointing off-site → ignored; the visitor goes to `/`.

**Acceptance criteria.**
- Passwords are never stored or logged in plain text.
- Sign-in failure wording is byte-identical for an unknown email and a wrong
  password.
- Sign-out requires POST.
- An extra `role` field in the registration form cannot create a staff account.

**API/Data changes.** `GET/POST /register`, `GET/POST /login`,
`POST /logout`. Writes `users`. No event: an account is not an aggregate with
a lifecycle in this system.

**UI changes.** `auth/register.html`, `auth/login.html`, both on the shared
`.auth-card`.

**Agent/Tool interaction.** None.

**Tests.** `tests/api/test_authorization.py`,
`tests/api/test_feature_auth_boundary.py`,
`tests/api/test_forms_and_validation.py`,
`tests/api/test_qa_authorization.py`.

---

## F-02 Role-based authorization — Done

**User story.** As the organization, I want staff operations restricted to
staff, so that adopters cannot alter the catalogue or see other people's data.

**Preconditions.** Two roles exist: ADOPTER and STAFF.

**Main flow.** Every protected route carries `@require_sign_in` and, where a
role matters, `@require_staff()` or `@require_adopter()`. The decorators run
on the server for every request, regardless of what the rendered page offered.
Ownership is checked a second time, inside the command or query handler, so
the rule travels with the data.

**Alternative and error flows.**
- Not signed in → **302 to `/login?next=…`**, because a visitor who simply
  has not signed in yet should be able to continue, not hit a dead end. The
  one exception is `/api/analysis-status`, which is JSON and answers 401.
- Wrong role → **403**, including on a direct POST with forged fields.
- Another adopter's record → **403**, and a record that does not exist gets
  the same answer, so identifiers cannot be enumerated by comparing the two.
- An account deactivated mid-session → the next request loads no user and the
  session ends.

**Acceptance criteria.**
- Blueprint §12 is satisfied: hiding a button is never the only control.
- The documented permission table in `docs/UX.md` §4 describes the real
  server, asserted mechanically.
- Staff hold more power but not an adopter's decisions: staff cannot apply,
  withdraw, respond to an invitation or edit a profile on an adopter's behalf.

**API/Data changes.** None of its own; `users.role` and
`AuthenticatedUser.is_staff` / `.is_adopter`.

**UI changes.** The navigation is role-aware, but as a convenience only.

**Agent/Tool interaction.** The agent is on the other side of this boundary
entirely: it cannot import a command, so no role check protects against it —
there is nothing to protect against.

**Tests.** `tests/api/test_authorization.py`,
`tests/api/test_qa_authorization.py`,
`tests/e2e/test_journeys.py` (`TestAuthorizationInTheBrowser`).

---

## F-03 Staff animal roster (tabular display) — Done

**User story.** As staff, I want a table of every animal in our care, so that
the catalogue stays accurate and I can find anything quickly.

**Preconditions.** Signed in as staff.

**Main flow.** Staff open `/animals/manage` → a table of every animal with
thumbnail, name, breed, species, age, size, temperament, location and status →
narrow by status, species, size, age band or free text → 40 rows per page →
open any animal, or its edit form.

**Alternative and error flows.** A filter value outside its enum narrows to
nothing rather than erroring. An out-of-range page falls back to page 1. An
adopter reaching the URL gets 403.

**Acceptance criteria.** Blueprint 4.3 (tabular display) is satisfied: a real
`<table>` with a caption and a named header on every column. At 375px the rows
become cards without losing a cell's column name.

**API/Data changes.** `GET /animals/manage`; `ListAllAnimalsQuery` with
`status`, `species`, `size`, `age_range`, `text`, `page`. No writes.

**UI changes.** `animals/manage.html`, `.table` / `.table--cards`.

**Agent/Tool interaction.** None.

**Tests.** `tests/api/test_feature_age_filter.py` (`TestTheBandsOnTheStaffTable`),
`tests/e2e/test_journeys.py::TestStaffJourney::test_staff_animal_table_lists_the_roster`,
`tests/e2e/test_accessibility.py::TestPerScreenMarkupPromises`.

---

## F-04 Structured animal search — Done

**User story.** As an adopter, I want to filter animals by the things that
matter to my household, so that I only see realistic options.

**Preconditions.** None — search is public.

**Main flow.** Free text over name, breed and description, plus species, size,
activity level, city, **age band**, good-with-children and
good-with-other-animals → results combine with AND → 24 per page, filters
preserved across pagination → click through to details.

**Alternative and error flows.**
- No matches → an empty state explaining how to widen the search, with a
  Clear link.
- An unrecognised filter value → treated as no constraint or as no match,
  never as an error.
- A nonsense, negative or astronomically large `page` → falls back to 1.
- `%`, `_` and SQL-shaped text in the search box → escaped and bound, searched
  for literally.

**Acceptance criteria.** Blueprint 4.1 satisfied. Every filter spec §6.2 names
is offered, including age. A search result links to a details view.

**API/Data changes.** `GET /animals/`; `SearchAnimalsQuery`,
`AnimalSearchFilters` (9 fields), `available_filter_options()`.

**UI changes.** `animals/search.html`, `.filter-bar`, `.chip-row` showing the
active filters.

**Agent/Tool interaction.** None. This is the deterministic path, and it is
what the natural-language feature degrades to when the agent is unavailable.

**Tests.** `tests/api/test_forms_and_validation.py` (`TestSearchFilters`),
`tests/api/test_qa_input_validation.py` (`TestSearchQueryString`),
`tests/api/test_feature_age_filter.py`,
`tests/e2e/test_journeys.py::TestPublicBrowsing`.

---

## F-05 Animal details view — Done

**User story.** As an adopter, I want to see everything about one animal, so
that I can judge whether it suits me.

**Preconditions.** None.

**Main flow.** Photograph, status tags, a specification grid, and a "Living
with <name>" panel making child and other-animal compatibility explicit.
Special needs appear as a warning callout. An adopter sees the apply form;
staff see the applicant count and the availability controls.

**Alternative and error flows.** Unknown identifier → 404, a designed page
with an `h1` and a way onward. An animal with no image falls back to a styled
placeholder rather than a broken image — though F-21 makes that unreachable
for anything listed through the application.

**Acceptance criteria.** Blueprint 4.2 satisfied. An adopted animal's page
still loads even though it has left the default search.

**API/Data changes.** `GET /animals/<animal_id>`; `GetAnimalDetailsQuery`.

**UI changes.** `animals/details.html`.

**Agent/Tool interaction.** None.

**Tests.** `tests/api/test_forms_and_validation.py` (`TestNotFoundHandling`,
`TestAnimalStatusVisibility`),
`tests/e2e/test_journeys.py::TestPublicBrowsing::test_visitor_searches_filters_and_opens_an_animal`.

---

## F-06 Adopter profile — Done

**User story.** As an adopter, I want to describe my home and routine once, so
that recommendations reflect my actual situation.

**Preconditions.** Signed in as an adopter. No profile is needed to reach the
form — that is how one is created.

**Main flow.** The adopter completes home type, yard, children, other animals,
experience, activity level, daily hours, city, preferred species, **preferred
size**, **preferred age range** and the **opt-in for proactive suggestions** →
`validate_profile` turns the untrusted submission into typed domain values →
`SaveAdopterProfileCommand` → `AdopterProfileUpdated` appended → the profile is
marked complete → redirect to Find My Pet.

**Alternative and error flows.**
- Any missing required field → 400 with every error listed at once and the
  submitted values redisplayed; the error summary links to the fields.
- Children ticked with no age, or an age with no children → rejected as
  contradictory.
- A yard size with no yard, or a pet description with no pets → discarded
  rather than stored.
- Values outside their CHECK ranges (hours outside 0–24, child age outside
  0–18, a negative yard) → rejected before the insert.
- A forged select value or an unknown species → rejected, not silently stored.
- Text longer than its column → rejected, because SQL Server truncates.

**Acceptance criteria.**
- `open_to_proactive_suggestions` defaults to **false**; it is never inferred
  from an absent field (spec §5.1).
- An incomplete profile excludes the adopter from Find More Adopters.
- An adopter can only edit their own profile; staff get 403 on both methods.
- Zero daily hours is valid input, not a missing value.

**API/Data changes.** `GET/POST /my/profile`; `SaveAdopterProfileCommand`,
`GetMyProfileQuery`. Writes `adopter_profiles`, appends
`AdopterProfileUpdated`.

**UI changes.** `personal/profile.html`, with `[data-shown-by]` progressive
disclosure for the dependent fields.

**Agent/Tool interaction.** The saved profile is what `get_adopter_profile`
returns to the agent over MCP.

**Tests.** `tests/unit/test_profile_validation.py`,
`tests/api/test_qa_input_validation.py` (`TestProfileFormRanges`,
`TestStringsLongerThanTheirColumn`),
`tests/api/test_forms_and_validation.py` (`TestProfileForm`),
`tests/e2e/test_journeys.py::TestAdopterJourney::test_profile_form_rejects_children_without_an_age`.

---

## F-07 Adoption application — Done

**User story.** As an adopter, I want to apply for several animals, so that I
am not limited to one chance.

**Preconditions.** Signed in, profile complete, animal AVAILABLE.

**Main flow.** The adopter writes an optional message on the animal's page →
`SubmitApplicationCommand` → the domain rules run → `ApplicationSubmitted`
appended → projected into `adoption_applications` → a `RANK_APPLICANT` job is
queued for the agent in the same transaction → redirect to the applications
list.

**Alternative and error flows.**
- Animal not AVAILABLE → refused with the rule's own message.
- A duplicate active application → refused by the handler's check *and*, under
  concurrency, by the `uq_active_application` filtered index.
- Profile incomplete or absent → redirected to the profile form.
- A message over 2000 characters → refused before the insert.
- Unknown animal → 404, and nothing is created.

**Acceptance criteria.** Multiple concurrent applications to *different*
animals are allowed (spec §5.3). The command returns an identifier only; the
next screen is a separate query. The adopter's own message is shown back to
them afterwards.

**API/Data changes.** `POST /my/apply/<animal_id>`,
`GET /my/applications`, `POST /my/applications/<id>/withdraw`. Writes
`adoption_applications` and `analysis_jobs`; appends `ApplicationSubmitted`
and `ApplicationWithdrawn`.

**UI changes.** The apply form on `animals/details.html`;
`personal/applications.html` with the message, the status tag, a machine-
readable submitted date, a History link and a confirmed Withdraw button.

**Agent/Tool interaction.** Enqueues a `RANK_APPLICANT` job. Analysis is
asynchronous — the adopter never waits on the model (NFR-3.1).

**Tests.** `tests/integration/test_approval_cascade.py` (`TestSubmission`),
`tests/api/test_forms_and_validation.py` (`TestApplicationSubmission`),
`tests/integration/test_qa_cascade_and_concurrency.py`
(`TestDuplicateActiveApplications`),
`tests/api/test_feature_dashboard_and_history.py` (`TestTheAdoptersApplicationList`).

---

## F-08 Approval cascade and reopen — Done

**User story.** As staff, I want approving one application to close that
adopter's other active applications, without losing the ability to undo it.

**Preconditions.** Signed in as staff; the application is in a status that
permits approval; the animal is still free to promise.

**Main flow.** Approve application A → `ApplicationApproved(A)` → every other
**active application by that same adopter** gets
`ApplicationClosedDueToOtherApproval(caused_by=A)` → the projection sets
CLOSED and `closed_because_application_id = A` → the animal moves to
ADOPTION_IN_PROGRESS → the adopter is notified → the handler returns how many
were closed, which becomes the flash message.

**Alternative and error flows.**
- The application is already APPROVED → refused; a double submit cannot
  re-approve.
- The animal is already in adoption for somebody else → refused, and the rival
  application is left exactly as it was.
- **Reversal.** `ReverseApprovalCommand` reads the closing events, keeps the
  **latest** cause per application, and reopens exactly those closed by *this*
  approval — appending `ApplicationReopened`, clearing the cause column and
  returning the animal to AVAILABLE. An application that was withdrawn,
  rejected, or closed by a *different* approval is untouched. Reversing
  something never approved is refused.

**Acceptance criteria.**
- No application is ever deleted; the full history of each stays queryable.
- **Rival applications stay open.** The cascade is scoped by adopter, not by
  animal. Another adopter's application for the same animal remains for a
  human to reject with a reason — spec §7.5 says "the other applications" of
  the approved adopter, and closing a rival automatically would be the system
  making a decision it is not entitled to make (spec §6.4). A *second*
  approval for the same animal is refused while the first stands, so the
  promise cannot be made twice.
- An application closed by A, reopened, then closed by B is **not** reopened
  by reversing A a second time.

**API/Data changes.** `POST /applications/<id>/decide` with
`decision=APPROVE` or `REVERSE`. Appends `ApplicationApproved`,
`ApplicationClosedDueToOtherApproval`, `ApplicationReopened` and
`AnimalStatusChanged`.

**UI changes.** The decision buttons on `matches/find_my_adopter.html`, shown
according to `RankedCandidate.can_approve` / `.can_reject` /
`.can_mark_under_review` / `.can_reverse`.

**Agent/Tool interaction.** None, and deliberately so. A human presses the
button; the ranking beside it is advice (rule R4).

> This feature is the concrete justification for event sourcing recorded in
> `ARCHITECTURE.md` §4. A current-state schema cannot distinguish "closed
> because A was approved" from "closed for some other reason".

**Tests.** `tests/integration/test_approval_cascade.py`,
`tests/integration/test_event_sourcing.py` (`TestReopeningFromTheEventLog`),
`tests/integration/test_qa_cascade_and_concurrency.py`,
`tests/unit/test_qa_state_machines.py` (`TestReopeningIsGuardedByCause`),
`tests/api/test_feature_staff_decision.py`.

---

## F-09 Deterministic matching engine — Done

**User story.** As a user of either role, I want match scores I can
interrogate, so that I trust the recommendation.

**Preconditions.** A complete adopter profile and an animal record. No model,
no network, no application context.

**Main flow.** Hard constraints are checked first; a violation disqualifies
outright, scores zero and carries its reason. Otherwise each of eleven
criteria is scored 0–100 by its own pure function, and the criteria are
combined with a direction-specific weight set.

**Criteria** (spec §8): living environment, daily availability, experience,
children compatibility, other-animal compatibility, size and space, species
preference, **age preference**, temperament, special-care capacity, location.

**Alternative and error flows.** Absent optional data scores neutral, not
zero. Corrupt stored data fails safe: an unknown child age is treated as the
risky case, an unrecognised species preference is dropped rather than mapped
to OTHER, a negative hours value is clamped rather than dragging the total
below zero.

**Acceptance criteria.**
- Identical input always produces an identical score, **byte-identical
  including the explanation text**. No LLM is involved (spec §8).
- Both weight sets sum to exactly 1.0 and cover every criterion once.
- The two directions genuinely differ (spec §9): Adopter→Animal leads with
  species preference 0.17 and living environment 0.15; Animal→Adopter leads
  with daily availability 0.19 and experience 0.17. Age preference weighs 0.09
  one way and 0.02 the other — what the adopter asked for matters more to the
  adopter than to the animal.
- Hard constraints disqualify; soft preferences only reduce.
- Every criterion supplies an explanation, so no score reaches the interface
  as a bare number (FR-9.7), and the weighted contributions add up to the
  total shown.

**API/Data changes.** None — `app/domain/matching.py` is a pure module,
imported by both the web tier and the agent.

**UI changes.** `matches/_score_ring.html` and `matches/_criteria_bars.html`,
the single renderers for a score and for a criterion breakdown, included by
every ranking screen and by the analysis page. The strong/fair/weak band comes
from a `band` property on `MatchScore` and `CriterionScore`, reading the domain
thresholds — no template repeats a number.

**Agent/Tool interaction.** The agent calls `calculate_match_score` as its
third observation, before any model turn, and the result is injected into the
prompt as an immutable fact. A model claiming a score cannot change the
stored number.

**Tests.** `tests/unit/test_matching.py`,
`tests/unit/test_qa_matching_edges.py`,
`tests/unit/test_feature_age_criterion.py`,
`tests/unit/test_qa_fact_mapping.py`.

---

## F-10 MCP tool server — Done

**User story.** As the agent, I need to fetch adopter and animal records
through tools, so that I do not depend on the application's internals.

**Preconditions.** The database is reachable; the interpreter can spawn a
subprocess.

**Main flow.** The agent spawns `python -m mcp_server` with `sys.executable`
and speaks MCP over stdio. Two tools, `get_adopter_profile` and
`get_animal_profile`, each returning the matching-relevant fields as plain
JSON. Their docstrings are read back at runtime through
`ClientSession.list_tools()` and become the manifest the model sees.

**Alternative and error flows.**
- A missing record returns `{"found": false, "reason": …}`, never an
  exception — an exception across the MCP boundary becomes an opaque protocol
  error the model cannot reason about.
- A server that cannot be started or listed costs the agent two tools, not the
  analysis: the manifest falls back to the local tools and the shortfall is
  recorded.

**Acceptance criteria.** Blueprint §8 satisfied — at least two local tools
over stdio, not a network port. Each carries an LLM-facing description. Both
are read-only: no tool name describes a write.

**API/Data changes.** None. The server reads `adopter_profiles` and `animals`.

**UI changes.** None.

**Agent/Tool interaction.** This *is* the tool interaction. The two record
tools are dispatched both as fixed prerequisites in phase 1 and, later,
whenever the model names one.

**Tests.** `tests/agent/test_mcp_stdio.py` — spawns the real server as a
subprocess and round-trips both tools; `tests/agent/test_reasoning_loop.py`
(`TestTheToolManifest`) covers the client side without a subprocess.

---

## F-11 RAG knowledge base — Done

**User story.** As the agent, I need curated care knowledge, so that my
explanations rest on documented guidance rather than invention.

**Preconditions.** `scripts/ingest_knowledge.py` has been run; Ollama is
serving the embedding model.

**Main flow.** 17 Markdown guides under `knowledge/` → chunked on their own
`##` headings, splitting any section over 1400 characters into windows with
160 characters of overlap → embedded with `nomic-embed-text` → stored in
ChromaDB (104 chunks) → retrieved semantically at analysis time, filtered by a
distance threshold → the agent cites the passages it used.

**Alternative and error flows.** A stopped Ollama host, a missing embedding
model, an empty collection or a corrupt store logs a warning and returns no
evidence. An embedding outage costs the prose, not the analysis. A blank query
is refused without a call.

**Acceptance criteria.** Blueprint §7 satisfied: retrieval is **semantic** — a
query sharing no vocabulary with the target chunk still finds it. The Vector
DB holds knowledge only, never adopter or animal records. Each chunk carries a
citable reference (`file.md#section`), and chunk ids are unique so ingestion
cannot silently overwrite one with another.

**API/Data changes.** None in SQL Server. ChromaDB persists under
`data/chroma/`.

**UI changes.** Cited sources appear on `matches/analysis.html`.

**Agent/Tool interaction.** `rag_search` is in the tool manifest, so the model
can retrieve again with a query of its own choosing after the fixed
prerequisite retrieval.

**Tests.** `tests/agent/test_rag_retrieval.py`.

---

## F-12 Autonomous agent process — Done

**User story.** As the organization, I want an assistant that assembles
evidence and explains matches, while people keep the decisions.

**Preconditions.** The process is started separately with
`python -m agent_service`. Ollama is optional: without it the system degrades
to scores without prose.

**Main flow.** The process polls `analysis_jobs` → claims a job with a
conditional update whose row count decides, so two workers cannot claim the
same one → fetches both records through MCP → computes the deterministic score
→ retrieves knowledge → evaluates the web-search gate → then enters a
**bounded reason-act loop**: the model is sent the system prompt, the facts,
the score, the criterion breakdown, every observation so far *and the tool
manifest*, and replies with either a tool call or its final JSON. A tool call
is dispatched, its observation appended, and the loop repeats. The answer is
checked against what was actually retrieved, then written to `match_analyses`
with its reasoning trace, and `AIAnalysisCompleted` is appended.

**Alternative and error flows.**
- Either record not found → the job fails with its reason; the agent refuses
  to score a pairing it could not read.
- An invented tool name → reported back to the model, which re-plans. A
  hallucinated name does not cost the job.
- Step cap reached → the deterministic explanation plus a
  `missing_information` line naming the budget.
- Model unreachable or unparseable after bounded retries → the deterministic
  explanation, with `model_name` set to `deterministic-fallback`. Text a model
  did not write is never attributed to it.
- Nothing survives the grounding check → same fallback.
- A worker killed mid-job → its claim is reclaimed after 15 minutes.
- An unexpected exception → logged with a traceback, recorded on the job, and
  never propagated out of `run_once`.

**Acceptance criteria.**
- Runs as a separate OS process and imports no controller, command, query or
  Flask application (blueprint §4 item 5).
- Never finalises an adoption (spec §6.4) — it cannot even name a decision
  command.
- States nothing absent from its sources: a citation naming something that was
  not retrieved is dropped and the drop is recorded.
- Uses RAG first and web search only when the gate opens (spec §13), never for
  the application's own records.
- Output is structured: reasons, concerns, missing information, citations.
- A completed analysis is reused for unchanged inputs (NFR-3.3).

**API/Data changes.** Reads `analysis_jobs`; writes `match_analyses`,
`analysis_jobs.result_payload` and `analysis_jobs.status`; appends
`AIAnalysisCompleted`.

**UI changes.** None directly; its output is F-23 and F-26.

**Agent/Tool interaction.** Four tools in the manifest:
`get_adopter_profile` and `get_animal_profile` over MCP/stdio, `rag_search`
in-process, `web_search` over HTTPS behind the gate.

**Tests.** `tests/agent/test_reasoning_loop.py`,
`tests/agent/test_agent_loop.py`, `tests/agent/test_worker_jobs.py`,
`tests/agent/test_qa_agent_robustness.py`.

---

## F-13 Find My Pet — Done

**User story.** As an adopter, I want suggestions without typing a query, so
that I can start from who I am rather than what I can describe.

**Preconditions.** Signed in as an adopter with a complete profile.

**Main flow.** One page → every available animal is scored with the
**Adopter→Animal** weights, deterministically and synchronously → ranked best
first and shown immediately → each card links to the animal, and shows its
explanation once the agent has written one.

**Alternative and error flows.** No profile → redirected to the profile form
with an explanatory flash. A card with no analysis yet shows the shared
pending block with how long it has waited. Disqualified animals are excluded
from the ranking entirely.

**Acceptance criteria.** The ranking appears without waiting for the model
(NFR-3.2). Ranking really is ranked — scores descend. Ties break
deterministically, so repeated ranking over identical data gives the same
order.

**API/Data changes.** `GET /my/matches`; `FindMyPetQuery`. No writes.

**UI changes.** `matches/find_my_pet.html`, `.score` ring, the pending block,
and `data-analysis-status-url` for the poller.

**Agent/Tool interaction.** Reads analyses the agent has already written,
matched on direction so an adopter never sees the staff-side prose beside the
adopter-side score. It does not enqueue work of its own.

**Tests.** `tests/api/test_feature_match_screens.py` (`TestThePollerIsWiredUp`,
`TestThePendingBlock`), `tests/unit/test_matching.py` (`TestRanking`),
`tests/e2e/test_journeys.py::TestAdopterJourney`.

---

## F-14 Natural-language search with profile fusion — Done

**User story.** As an adopter, I want to describe what I am looking for in my
own words — *"something like a hamster or rabbit, a small rodent"* — and
optionally have my profile taken into account.

**Preconditions.** None to describe a search; a **complete** profile to fuse
one.

**Main flow.** The visitor types a description and optionally ticks "use my
profile" → `POST /search/describe` validates it, enqueues an
`INTERPRET_INTENT` job and **redirects** → the result page shows the pending
block and refreshes itself every four seconds with a `<meta>` tag, so it works
with JavaScript disabled → the agent interprets the text into typed criteria
and stores them in `analysis_jobs.result_payload` → the page reads them back,
turns them into filters, and runs the **deterministic** search. With a profile
bound, `FindMyPetWithIntentQuery` scores the narrowed set against the stored
profile instead of merely listing it (spec §6.4).

**Alternative and error flows.**
- Blank or over 500 characters → 400, the form re-rendered with an
  explanation, and **nothing enqueued**: an empty textarea costs no inference.
- The model says it did not understand → the page says so and offers the
  structured filters.
- Understood but nothing extracted → said explicitly, rather than a silent
  empty page.
- The job failed → the page explains that the interpreter could not be reached
  and offers the filters.
- An unknown job id, or a job of another type → 404.
- A job bound to another adopter's profile → 403; one bound to nobody is
  readable by anyone holding its identifier, which is what makes the redirect
  work for a signed-out visitor.
- An invented species in the model's reply → dropped, not turned into a filter
  that matches nothing. A prompt injection cannot produce anything but search
  criteria, because the reply is projected onto the enums whatever it says.

**Acceptance criteria.**
- **No model call happens while an HTTP request is being served** (NFR-3.1),
  and no module under `app/` can even import the model client.
- The distinction between stable profile and current intent is explicit and
  visible: fusion is opt-in, shown as a checkbox, and ignored for an
  incomplete profile.
- The interpretation is shown back to the adopter, so they can see how they
  were understood.

**API/Data changes.** `GET/POST /search/describe`,
`GET /search/describe/<analysis_job_id>`;
`EnqueueIntentInterpretationCommand`, `GetIntentJobQuery`,
`FindMyPetWithIntentQuery`; `analysis_jobs.natural_language_query` and
`.result_payload`. The payload contract lives in
`app/domain/search_intent.py` so neither tier owns it.

**UI changes.** `matches/describe.html`.

**Agent/Tool interaction.** `IntentInterpreter.interpret` — one model call in
the agent process, no tools, no RAG. The job writes no `MatchAnalysis` and no
domain event: it answers a question, it does not assess a match.

**Tests.** `tests/api/test_feature_natural_language_search.py`,
`tests/agent/test_intent.py`,
`tests/agent/test_worker_jobs.py` (`TestIntentJobs`),
`tests/api/test_qa_input_validation.py` (`TestNoLanguageModelInTheRequestPath`).

---

## F-15 Find My Adopter — Done

**User story.** As staff, I want existing applicants for one animal ranked, so
that I review the strongest first.

**Preconditions.** Signed in as staff; the animal exists.

**Main flow.** Open an animal's applicant screen → everyone who applied is
scored with the **Animal→Adopter** weights → each row shows the score, its
criterion breakdown, the applicant's own message, a link to the full analysis,
a link to the application's history, and the decision buttons its current
status permits.

**Alternative and error flows.** Unknown animal → 404. No applicants → an
empty state. An analysis still being written → the shared pending block with
how long it has waited.

**Acceptance criteria.** Ranks only people who actually applied — deliberately
distinct from F-16. The score is inspectable, never a bare number. The screen
states that a human decides (spec §6.4), and an E2E test asserts that sentence
is on the page.

**API/Data changes.** `GET /animals/<animal_id>/adopters`;
`RankApplicantsQuery`, `RankedCandidate`. No writes.

**UI changes.** `matches/find_my_adopter.html` in `applicants` mode.

**Agent/Tool interaction.** Displays stored analyses, matched on direction.

**Tests.** `tests/api/test_feature_match_screens.py`,
`tests/api/test_feature_staff_decision.py`,
`tests/e2e/test_journeys.py::TestStaffJourney::test_staff_ranks_applicants_for_an_animal`.

---

## F-16 Find More Adopters — Done

**User story.** As staff, I want to find suitable adopters who never applied,
so that a good home is not missed.

**Preconditions.** Signed in as staff; the animal is AVAILABLE.

**Main flow.** The eligibility filter runs first (spec §10): complete profile,
opted in to proactive suggestions, active account, not already an applicant,
no open invitation for this animal → the survivors are ranked → each carries
a Send invitation button.

**Alternative and error flows.** Candidates excluded by an eligibility rule
are listed in a collapsed disclosure **with the reason**, rather than silently
dropped — a bare "12 excluded" is not something staff can check. Disqualified
candidates get no invitation button. Unknown animal → 404.

**Acceptance criteria.** Deliberately distinct from F-15: this discovers
people who did **not** apply. Only opted-in adopters appear, and the opt-in is
enforced again in the command when the invitation is actually sent.

**API/Data changes.** `GET /animals/<animal_id>/discover`;
`FindMoreAdoptersQuery`, `RankingResult.excluded_candidates`,
`ExcludedCandidate`. No writes.

**UI changes.** `matches/find_my_adopter.html` in `discovery` mode, with the
exclusion `<details>`.

**Agent/Tool interaction.** None at query time; the ranking is deterministic.

**Tests.** `tests/unit/test_invitation_rules.py` (`TestSendingEligibility`),
`tests/api/test_feature_match_screens.py` (`TestExcludedCandidates`),
`tests/e2e/test_journeys.py::TestStaffJourney::test_discovery_finds_adopters_who_did_not_apply`.

---

## F-17 Invitations and the expiry sweep — Done

**User story.** As staff, I want to invite a candidate to consider an animal;
as an adopter, I want to accept or decline within a known window.

**Preconditions.** Staff; an AVAILABLE animal; an eligible, opted-in adopter.

**Main flow.** Staff send with a message → `InvitationSent` appended,
`expires_at = sent_at + 72h`, a notification written → the adopter opens their
invitations, which appends `InvitationViewed` idempotently → they accept or
decline → accepting **creates an application**, it does not approve an
adoption (spec §7.4).

**Alternative and error flows.**
- Responding after the window → refused, to the microsecond: the boundary
  instant still answers, one microsecond later does not.
- Responding twice → refused; an answered invitation is final.
- Another adopter's invitation → 403, accept and decline alike.
- Inviting an opted-out adopter, an incomplete profile, an inactive account,
  an unavailable animal, or someone already applying or already invited →
  refused with the rule's message.
- Accepting when the animal is no longer AVAILABLE → refused, the same rule
  that governs a direct application.
- **Expiry.** `ExpireOverdueInvitationsCommand` is dispatched from a
  `before_request` hook, at most once every five minutes, never on a static
  request. It appends `InvitationExpired` — expiry is a recorded event, not a
  read-time inference — and is idempotent.

**Acceptance criteria.** The window is exactly `INVITATION_EXPIRY_HOURS`
(default 72), computed with timezone-aware datetimes; a naive `sent_at` and a
window of zero are both refused loudly. The sweep never touches an answered
invitation.

**API/Data changes.** `POST /animals/<id>/invite`, `GET /my/invitations`,
`POST /my/invitations/<id>/respond`. Writes `adoption_invitations`,
`notifications` and (on acceptance) `adoption_applications`; appends the five
invitation events.

**UI changes.** `personal/invitations.html` with a countdown label and a
`data-confirm` on the decline button; the Send invitation form on the
discovery screen.

**Agent/Tool interaction.** An accepted invitation creates an application,
which enqueues a `RANK_APPLICANT` job exactly as a direct application does.

**Tests.** `tests/unit/test_invitation_rules.py`,
`tests/unit/test_qa_state_machines.py` (`TestRespondingAtTheWindowBoundary`),
`tests/integration/test_invitation_flow.py`,
`tests/api/test_feature_expiry_sweep.py`.

---

## F-18 Notification inbox — Done

**User story.** As a user of either role, I want to see the messages the
system wrote for me, so that an invitation or a decision does not depend on my
happening to look at the right page.

**Preconditions.** Signed in.

**Main flow.** `/my/notifications` lists the signed-in account's messages,
newest first, unread ones marked. Opening one marks it read and follows its
link to whatever it is about. "Mark all read" clears the inbox in one action.
An unread count is injected into every template by a context processor, so any
page can show the badge.

**Alternative and error flows.**
- Another user's message → 403; an unknown one → 404.
- The bulk action is scoped too: marking all read does not touch another
  account.
- A stored `link_url` that is off-site or protocol-relative → refused; the
  reader goes to the inbox instead.
- An anonymous visitor → 302 to sign in, and the badge processor answers 0
  rather than failing.
- A database error while counting → 0, logged. A badge is not worth failing a
  page over.

**Acceptance criteria.** Internal inbox only, no email (spec §23). **Not
adopter-only**: staff receive messages too, and an inbox one of the two
audiences cannot read is worse than no inbox. Notifications are generated for
invitations received, invitation responses and application status changes.

**API/Data changes.** `GET /my/notifications`,
`POST /my/notifications/<id>/read`, `POST /my/notifications/read-all`;
`ListMyNotificationsQuery`, `CountUnreadNotificationsQuery`,
`MarkNotificationReadCommand`, `MarkAllNotificationsReadCommand`.

**UI changes.** `personal/notifications.html`, `.notification-item`,
`.badge`.

**Agent/Tool interaction.** None. `NotificationType.ANALYSIS_READY` exists in
the enum and is never written — the screens poll instead (see F-23).

**Navigation.** The top bar carries a **Notifications** entry for both roles,
with the unread count as a `.badge`, so a new message is visible from any page
rather than only by going to look for it.

**Tests.** `tests/api/test_feature_notifications.py`.

---

## F-19 Staff dashboard — Done

**User story.** As staff, I want one screen that tells me what needs doing, so
that I can start work without hunting through lists.

**Preconditions.** Signed in as staff.

**Main flow.** Six headline tiles — available animals, applications awaiting
review, under review, invitations awaiting a reply, adoptions in progress,
match analyses completed — then a **Needs Attention** section, an **agent
queue** strip, and a recent-activity feed read from the event log.

**Alternative and error flows.** An empty database produces zeroes and no
division error. An animal with no analyses is not counted as "no suitable
applicants": absence of evidence is not evidence of a poor match, and the
figure counts the applicants' scores rather than every stored score.

**Acceptance criteria.**
- Blueprint 4.4 satisfied; all eight spec §22 figures are present.
- Every tile that has a screen behind it is a link to that screen, and **a
  tile with nowhere to go is not styled as a link** — see Known gaps.
- Attention headlines read as prose a person wrote: "1 application needs
  attention", never "1 application(s)".
- Every timestamp is a machine-readable `<time datetime=…>`.
- An activity entry never renders a link with no text.

**API/Data changes.** `GET /dashboard`; `GetDashboardSummaryQuery`,
`DashboardSummary` (16 fields), `StatTile`. No writes.

**UI changes.** `dashboard.html`, `.stat` / `.stat--link`.

**Agent/Tool interaction.** The agent-queue strip reports
`analyses_pending`, `analyses_failed` and how long the oldest queued job has
waited — so a stuck queue is distinguishable from a busy one. It is the only
place in the interface where the second process is visible as a process.

**Known gaps.** Two tiles are deliberately unlinked: **invitations awaiting a
reply** (there is no staff-side invitation list) and **match analyses
completed** (analyses are reached through the animal they belong to, not from
an index). There is also no "view all activity" link, because there is no
all-activity route.

**Tests.** `tests/api/test_feature_dashboard_and_history.py`,
`tests/integration/test_qa_cascade_and_concurrency.py` (`TestDashboardCounts`),
`tests/e2e/test_journeys.py::TestStaffJourney`.

---

## F-20 Activity history — Done

**User story.** As staff, I want to see how a case reached its current state;
as an adopter, I want to see what happened to *my* application.

**Preconditions.** Signed in.

**Main flow.** `/history/<aggregate_type>/<aggregate_id>` renders that
aggregate's event stream as a timeline, oldest first, with the actor and a
machine-readable timestamp, read straight from `domain_events`.

**Alternative and error flows.**
- An adopter asking for somebody else's record, an animal's, or a profile's →
  403 — and equally for a record that does not exist, so identifiers cannot be
  enumerated.
- An unknown aggregate type → 404.
- Anonymous → 302 to sign in.

**Acceptance criteria.** Satisfies blueprint §10's requirement that the system
can display action history, read from the log and not from a separate audit
table. **An adopter can see their own application and invitation history**,
because spec §7.5 is written from their point of view and a closure they could
not see would have to be taken on trust. The visibility rule lives in the
query handler, not the controller.

**API/Data changes.** `GET /history/<aggregate_type>/<aggregate_id>`;
`GetAggregateHistoryQuery` with `viewer_adopter_profile_id` and
`viewer_is_staff`, raising `HistoryNotVisibleError`.

**UI changes.** `history.html`, `.timeline`, with a role-aware back button.

**Agent/Tool interaction.** `AIAnalysisCompleted` appears in the timeline like
any other event, with a null actor because the agent is not a person.

**Tests.** `tests/api/test_feature_dashboard_and_history.py`
(`TestWhoMayReadAHistory`),
`tests/integration/test_approval_cascade.py` (`TestEventLogIntegrity`).

---

## F-21 Animal lifecycle management — Done

**User story.** As staff, I want to list a new animal, correct its record and
change its availability, so that the catalogue reflects reality without
anybody running a script.

**Preconditions.** Signed in as staff.

**Main flow.** `/animals/new` → the form, populated from the enums → the
submission is parsed in the controller and validated in
`app/domain/animal_rules.py` → `CreateAnimalCommand` → `AnimalListed`
appended → redirect to the new animal's page, where it is immediately visible
in the public search. `/animals/<id>/edit` does the same through
`UpdateAnimalCommand` and `AnimalUpdated`. `POST /animals/<id>/status` changes
availability through `ChangeAnimalStatusCommand` and `AnimalStatusChanged`.

**Alternative and error flows.**
- **No photograph → refused**, and nothing is written (spec §24, FR-4.2). The
  same guard runs on edit, so the last photograph cannot be removed. The seed
  obeys the same rule: it falls back to a committed placeholder image rather
  than writing an animal without one, and fails loudly if it ever would.
- A species, size, temperament or status outside its enum → refused; the
  vocabulary is closed at the HTTP boundary as well as in the database.
- A non-numeric or implausible age → a message, not an exception.
- "Has special needs" with no description → refused; a description with the
  flag off → discarded.
- Any invalid submission → 400 with every problem listed and the typed values
  redisplayed, so staff do not retype the form.
- An unknown animal on edit or status → 404.
- An adopter posting directly → 403, and nothing is listed.

**Acceptance criteria.** FR-4.1, FR-4.2 and FR-4.4 have an HTTP surface, not
only a documented one. Before this existed, `scripts/seed.py` was the only way
an animal entered the system, which left three MUST requirements unreachable
through the application itself.

**API/Data changes.** `GET/POST /animals/new`,
`GET/POST /animals/<id>/edit`, `POST /animals/<id>/status`. Writes `animals`
and `animal_images`; appends `AnimalListed`, `AnimalUpdated`,
`AnimalStatusChanged`.

**UI changes.** `animals/form.html`, shared by create and edit; the
availability control on `animals/details.html`.

**Agent/Tool interaction.** An edit moves `animals.updated_at`, which
invalidates any cached analysis for that animal — the next job recomputes it
in place rather than serving an explanation of facts that have moved.

**Tests.** `tests/unit/test_feature_animal_rules.py`,
`tests/api/test_feature_animal_lifecycle.py`,
`tests/api/test_qa_authorization.py::test_the_animal_create_and_edit_routes_exist`.

---

## F-22 Staff decision on an application — Done

**User story.** As staff, I want to approve, reject, mark under review or
reverse an approval, so that the adoption process actually moves.

**Preconditions.** Signed in as staff; the application exists.

**Main flow.** From the applicant ranking, staff press one of four buttons →
`POST /applications/<id>/decide` validates `decision` against a tuple of four
permitted values → dispatches the matching command → flashes the outcome →
redirects back to the ranking.

**Alternative and error flows.**
- A `decision` outside the four → 400. The value is checked by lookup, not
  trusted.
- An unknown application → 404.
- An illegal transition — approving twice, rejecting something withdrawn,
  reversing something never approved — → the rule's own message, and nothing
  changes.
- Approving an animal already promised to another adopter → refused, and the
  rival application is untouched.

**Acceptance criteria.** **The decision is a human one.** No code path
auto-approves, and the agent cannot import or name any of these commands
(rule R4, spec §6.4) — a static test asserts it. Each button appears only when
the application's current status permits it.

**API/Data changes.** `POST /applications/<id>/decide` with `decision` ∈
{`APPROVE`, `REJECT`, `REVIEW`, `REVERSE`}, optional `note`, optional
`animal_id` for the return journey. Drives `ApproveApplicationCommand`,
`RejectApplicationCommand`, `MarkApplicationUnderReviewCommand`,
`ReverseApprovalCommand`.

**UI changes.** The four buttons on `matches/find_my_adopter.html`, gated by
`RankedCandidate.can_approve` and its siblings.

**Agent/Tool interaction.** None, and that is the point: the ranking beside
the button is advice.

**Tests.** `tests/api/test_feature_staff_decision.py`,
`tests/unit/test_architecture_guard.py::test_agent_never_names_a_decision_command`.

---

## F-23 Analysis status and the pending state — Done

**User story.** As a user waiting on the agent, I want to see that something
is happening and have the page update itself, rather than wondering whether it
is broken.

**Preconditions.** Signed in.

**Main flow.** Any screen that shows agent output declares
`data-analysis-status-url`. A small script polls `/api/analysis-status`,
compares the `generation` value with the previous answer, and reloads when it
changes. **It stops polling once nothing is pending, and pauses while the tab
is hidden** — a page left open overnight should not keep a request every few
seconds against a throttled free-tier database. Until then each unexplained card renders
`matches/_pending_analysis.html`, which says how long the job has waited.

**Alternative and error flows.**
- No scope parameter → 400; an unscoped poll does not report the whole queue.
- An adopter asking about an animal → 403.
- An anonymous request → 401, because JSON has no page to redirect to.
- Nothing queued → zeroes, which is an answer rather than an error.
- JavaScript disabled → the pending block carries a plain Refresh link, and
  the describe screen uses a `<meta>` refresh instead.

**Acceptance criteria.** The response names **no record** — only counts, a
generation number and a timestamp — so polling cannot be used to enumerate
anything. The adopter scope takes no identifier, so one adopter cannot ask
about another. A completed and a failed job are reported separately, so a
stuck queue is distinguishable from a busy one.

**API/Data changes.** `GET /api/analysis-status?scope=my-matches` or
`?animal_id=<id>`; `GetAnalysisStatusQuery`, `AnalysisStatus.as_dictionary()`.

**UI changes.** `matches/_pending_analysis.html`, `_skeleton.html`,
`petmatch.js`.

**Agent/Tool interaction.** This is how the asynchronous design becomes
visible: it reports the second process's queue without ever blocking on it.

**Tests.** `tests/api/test_feature_analysis_status.py`,
`tests/api/test_feature_match_screens.py`.

---

## F-24 Event replay and projection rebuild — Done

**User story.** As a developer or reviewer, I want to prove that the current
database is exactly what the event log says it should be, and repair it if it
is not.

**Preconditions.** A session with the event log and the projection tables.

**Main flow.** `replay_application(events)` and `replay_invitation(events)`
rebuild one aggregate's current state from its own stream.
`rebuild_projections(session)` replays **every** Application and Invitation
stream, compares each replayed field with the stored row, and returns a
`RebuildReport` listing every disagreement as a `ProjectionMismatch`.

**Alternative and error flows.**
- An aggregate with no events → `EmptyStreamError`, not a default-valued
  projection. An aggregate that was never created has nothing to rebuild, and
  inventing one would be a fabrication.
- A stream with no row → reported as missing, distinctly from a disagreeing
  one.
- `apply=True` writes the replayed values back, inside the caller's
  transaction; it commits nothing, so a repair lands completely or not at all.

**Acceptance criteria.** FR-13.3 — current state is reconstructible by
replaying the log. Read-only by default, so it is safe to run against real
data. Covers Applications and Invitations (`REBUILDABLE_AGGREGATES`); Animals
and adopter profiles are recorded in the log but are not event-sourced
projections, and this document says so rather than implying otherwise.

**API/Data changes.** `app/eventstore/projections.py`. No route. There is no
CLI entry point yet; it is called from tests or a Python shell —
`docs/DEMO.md` has the snippet.

**UI changes.** None.

**Agent/Tool interaction.** None.

**Tests.** `tests/integration/test_event_sourcing.py` — 14 tests, including
`test_a_consistent_world_reports_no_mismatches`,
`test_a_corrupted_row_is_detected` and `test_applying_a_rebuild_repairs_the_row`.

---

## F-25 Age-band filter — Done

**User story.** As an adopter I want to narrow the catalogue to puppies, adults
or older animals; as staff I want the same on the roster.

**Preconditions.** None.

**Main flow.** `?age_range=0-2`, `2-8` or `8+` on `/animals/` and
`/animals/manage`. `parse_age_range` validates the value against
`AGE_RANGE_BOUNDS`, and `_apply_age_band` turns it into a bounded comparison
on `animals.age_years`.

**Alternative and error flows.** An unrecognised band means **no age
constraint** rather than an error or an empty page — the value comes from a
URL anyone can edit.

**Acceptance criteria.** The bands tile the whole range and do not overlap:
`0-2` is age < 2, `2-8` is 2 ≤ age < 8, `8+` is age ≥ 8. No animal falls
between two bands, and a two-year-old is in exactly one. The band combines
with the other filters rather than replacing them. Spec §6.2 (search) and
§7.1 (staff table) both name age; both now offer it.

**API/Data changes.** `AnimalSearchFilters.age_range`,
`ListAllAnimalsQuery.age_range` and `.size`, `available_filter_options()["age_range"]`.

**UI changes.** The age select on `animals/search.html`. **`animals/manage.html`
does not yet render the age or size selects** — the controller passes
`options.age_range`, `selected_age_range` and `selected_size`, and the query
honours them, so the filter works by URL but has no control on the staff
screen.

**Agent/Tool interaction.** The same bands are what an interpreted intent
turns into, so a described search and a filtered one narrow identically.

**Tests.** `tests/api/test_feature_age_filter.py` (20 tests),
`tests/unit/test_feature_age_criterion.py` for the scoring counterpart.

---

## F-26 Reasoning-trace display — Done

**User story.** As staff, I want to see what the agent actually did before it
told me this, so that "the agent uses tools" is something on screen rather
than a claim in a document.

**Preconditions.** An analysis written by a version of the agent that records
a trace.

**Main flow.** Every step the loop takes is recorded as
`{step, action, detail}` — both record fetches, the score, each retrieval with
its query and result count, each web-search gate decision with the rule that
made it, each tool failure, every dropped citation, the manifest offered, and
the final answer or the reason there was none. It is stored as JSON in
`match_analyses.reasoning_trace` and rendered on `/analyses/<id>` under "How
the agent reasoned", beside the evidence list where cited sources are marked
as such.

**Alternative and error flows.** An analysis with no trace (an older row, or a
seeded one) renders **no panel at all**, rather than an empty heading.

**Acceptance criteria.** It is the audit artefact for blueprint §6.2: it shows
what the agent did, in order, **including the turns it spent on something that
did not work**. The evidence list distinguishes a source that was consulted
from one the explanation relied on (spec §6.4).

**API/Data changes.** `match_analyses.reasoning_trace` NVARCHAR(MAX) NULL;
`MatchAnalysisDetail.reasoning_trace` and `.cited_sources`.

**UI changes.** The trace panel on `matches/analysis.html`.

**Agent/Tool interaction.** Written by `agent_service/reasoning_session.py`
and persisted by the worker.

**Tests.** `tests/agent/test_worker_jobs.py` (`TestPersistedTrace`),
`tests/api/test_feature_match_screens.py` (`TestTheAnalysisDetailPage`).

---

## F-27 CSRF protection — Done

**User story.** As a signed-in user, I want a third-party page to be unable to
act as me, so that visiting an unrelated site cannot withdraw my application.

**Preconditions.** None.

**Main flow.** `CSRFProtect` is initialised on the application. Every POST
form carries a hidden `csrf_token` field in its body. A POST without a valid
token is refused before the view runs.

**Alternative and error flows.** A missing or stale token renders a friendly
page under **400** — "That form has expired. Please go back, reload the page
and try again" — because the usual innocent cause is a form left open past the
session rollover, and a security notice would be shown to somebody who did
nothing wrong.

**Acceptance criteria.**
- NFR-4.3 is met, and asserted: a POST with no token performs no action.
- The session cookie is `SameSite=Lax` and `HttpOnly`. Lax rather than Strict
  so an ordinary link from an email still arrives signed in; the cookie is
  simply not attached to a cross-site POST, so a forged request arrives
  unauthenticated rather than acting as the user.
- `Secure` is deliberately **off**: it would stop the cookie being sent over
  plain HTTP, which is exactly how this application is demonstrated locally. A
  deployment behind TLS should set `SESSION_COOKIE_SECURE=True`.
- **No token sits inside an opening `<form>` tag** — a real defect that broke
  the Withdraw button once and now cannot recur.
- GET forms (search) carry no token, and every form in every template is
  balanced.

**API/Data changes.** None. `csrf_protection.init_app(application)` and a
`CSRFError` handler in `app/__init__.py`.

**UI changes.** `{{ csrf_token() }}` in every POST form.

**Agent/Tool interaction.** None.

**Tests.** `tests/unit/test_feature_csrf_tokens.py` — parses every template;
`tests/api/test_qa_authorization.py` (`TestCrossSiteRequestForgery`).
