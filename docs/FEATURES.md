# FEATURES — PetMatch

Every feature is defined here **before** it is implemented, using the
template from course blueprint §16: name, user story, preconditions, main
flow, alternative and error flows, acceptance criteria, API/data changes, UI
changes, agent interaction and tests.

Status legend: **Done** · **In progress** · **Planned**

| # | Feature | Status |
|---|---|---|
| F-01 | Account registration and sign-in | Done |
| F-02 | Role-based authorization | Done |
| F-03 | Animal catalogue and management | Done |
| F-04 | Structured animal search | Done |
| F-05 | Animal details view | Done |
| F-06 | Adopter profile | Planned |
| F-07 | Adoption application | Planned |
| F-08 | Approval cascade and reopen | Planned |
| F-09 | Deterministic matching engine | In progress |
| F-10 | MCP tool server | In progress |
| F-11 | RAG knowledge base | In progress |
| F-12 | Autonomous agent process | In progress |
| F-13 | Find My Pet | Planned |
| F-14 | Natural-language search | Planned |
| F-15 | Find My Adopter | Planned |
| F-16 | Find More Adopters | Planned |
| F-17 | Invitations | Planned |
| F-18 | Internal notifications | Planned |
| F-19 | Staff dashboard | Planned |
| F-20 | Activity history | Planned |

---

## F-01 Account registration and sign-in — Done

**User story.** As a visitor, I want to create an account and sign in, so
that I can maintain a profile and apply for animals.

**Preconditions.** None.

**Main flow.** Visitor submits name, email and password → server validates →
password is hashed → account created with role ADOPTER → user is signed in.

**Alternative and error flows.**
- Email already registered → 409, form redisplayed with the entered values.
- Password under 8 characters or mismatched confirmation → 400, all problems
  listed at once rather than one at a time.
- Wrong credentials at sign-in → 401 with a single message that does not
  reveal whether the email exists.
- Deactivated account → 403.

**Acceptance criteria.**
- Passwords are never stored or logged in plain text.
- Sign-in failure messages are identical for unknown email and wrong password.
- Sign-out requires POST.

**Data.** `users`. **UI.** `/register`, `/login`.
**Tests.** `test_auth.py`, `test_authorization.py`.

---

## F-02 Role-based authorization — Done

**User story.** As the organization, I want staff operations restricted to
staff, so that adopters cannot alter the catalogue or see other people's data.

**Main flow.** Every protected route carries `@require_staff()` or
`@require_adopter()`, checked server-side on each request.

**Error flows.** Not signed in → 401. Wrong role → 403, including on a
direct POST with forged fields.

**Acceptance criteria.**
- Blueprint §12 is satisfied: hiding a button is never the only control.
- An adopter changing an id in a URL cannot read another adopter's data;
  ownership is checked in the query handler, not only in the route.

**Tests.** `test_authorization.py` — every endpoint against every role.

---

## F-03 Animal catalogue and management — Done

**User story.** As staff, I want to see and manage every animal in our care,
so that the catalogue stays accurate.

**Main flow.** Staff open `/animals/manage` → table of all animals with
thumbnail, name, breed, species, age, size, temperament, location and status
→ filter by status or species → open any animal.

**Acceptance criteria.** Blueprint 4.3 (tabular display) is satisfied.
Adopters receive 403.

**UI.** `/animals/manage`. **Tests.** `test_staff_journey.py`.

---

## F-04 Structured animal search — Done

**User story.** As an adopter, I want to filter animals by the things that
matter to my household, so that I only see realistic options.

**Main flow.** Free-text over name, breed and description, plus species,
size, energy level, good-with-children and good-with-other-animals →
paginated results, 12 per page → click through to details.

**Alternative flows.** No matches → empty state explaining how to widen the
search, with a Clear link.

**Acceptance criteria.** Blueprint 4.1 satisfied. Filters combine with AND.
Pagination preserves the active filters.

**UI.** `/animals/`. **Tests.** `test_search.py`, `test_adopter_journey.py`.

---

## F-05 Animal details view — Done

**User story.** As an adopter, I want to see everything about one animal,
so that I can judge whether it suits me.

**Main flow.** Photograph, status tags, six-field specification grid, and a
"Living with <name>" panel making child and other-animal compatibility
explicit. Special needs appear as a warning callout.

**Acceptance criteria.** Blueprint 4.2 satisfied. Image is mandatory
(spec §24); a missing image falls back to a styled placeholder rather than a
broken image. Staff additionally see the active applicant count.

**UI.** `/animals/<id>`.

---

## F-06 Adopter profile — Planned

**User story.** As an adopter, I want to describe my home and routine once,
so that recommendations reflect my actual situation.

**Preconditions.** Signed in as an adopter.

**Main flow.** Adopter completes home type, yard, children, other animals,
experience, activity level, daily hours, city, preferred species — and the
**opt-in for proactive suggestions** → saved → `AdopterProfileUpdated`
appended → profile marked complete.

**Error flows.** Missing required field → 400 with field-level messages.
Child age given without children present → rejected as inconsistent.

**Acceptance criteria.**
- `open_to_proactive_suggestions` defaults to **false**; it must be chosen
  deliberately (spec §5.1).
- An incomplete profile excludes the adopter from Find More Adopters.
- An adopter can only edit their own profile.

**Data.** `adopter_profiles`. **UI.** `/my/profile`.
**Tests.** `test_profile_validation.py`, `test_eligibility_rules.py`.

---

## F-07 Adoption application — Planned

**User story.** As an adopter, I want to apply for several animals, so that
I am not limited to one chance.

**Preconditions.** Signed in, profile complete, animal AVAILABLE.

**Main flow.** Adopter submits an optional message → `SubmitApplicationCommand`
→ rules checked → `ApplicationSubmitted` appended → projected into
`adoption_applications` → an `analysis_jobs` row is queued for the agent →
adopter redirected to their applications list.

**Error flows.**
- Animal not AVAILABLE → rejected.
- Duplicate active application for the same animal → rejected by the partial
  unique index, surfaced as a clean message.
- Profile incomplete → adopter prompted to finish it first.

**Acceptance criteria.** Multiple concurrent applications to *different*
animals are allowed (spec §5.3). The command returns an identifier only; the
next screen is a separate query.

**Agent interaction.** Enqueues a `RANK_APPLICANT` job. Analysis is
asynchronous — the adopter never waits on the model.

**Tests.** `test_application_state_machine.py`, `test_application_api.py`.

---

## F-08 Approval cascade and reopen — Planned

**User story.** As staff, I want approving one application to close that
adopter's other active applications, without losing the ability to undo it.

**Main flow.** Staff approve application A → `ApplicationApproved(A)` →
every other active application by that adopter gets
`ApplicationClosedDueToOtherApproval(caused_by=A)` → projection sets status
CLOSED and `closed_because_application_id = A` → animal moves to
ADOPTION_IN_PROGRESS → notifications sent.

**Alternative flow — reversal.** If A is later cancelled or withdrawn, the
system queries applications where `closed_because_application_id = A`. Those,
**and only those**, are eligible to reopen via `ApplicationReopened`.

**Acceptance criteria.**
- No application is ever deleted.
- An application withdrawn or rejected for its own reasons is **not**
  reopened by a reversal.
- The full history of every application remains queryable.

> This feature is the concrete justification for event sourcing recorded in
> `ARCHITECTURE.md` §4. A current-state schema cannot distinguish "closed
> because A was approved" from "closed for some other reason".

**Tests.** `test_approval_closes_other_applications.py`,
`test_reopen_only_cascade_closed.py`.

---

## F-09 Deterministic matching engine — In progress

**User story.** As a user of either role, I want match scores I can
interrogate, so that I trust the recommendation.

**Main flow.** Hard constraints are checked first; a violation disqualifies
outright. Otherwise each criterion is scored 0–100 by its own pure function,
and the criteria are combined using a direction-specific weight set.

**Criteria** (spec §8): living environment, daily availability, experience,
children, other animals, size and space, species preference, age preference,
temperament, special-care requirements, location.

**Acceptance criteria.**
- Identical input always produces an identical score. **No LLM is involved**
  (spec §8).
- The two directions use different weights (spec §9): Adopter→Animal favours
  the person's lifestyle and stated preferences; Animal→Adopter favours the
  animal's care needs and temperament.
- Hard constraints disqualify; soft preferences only reduce.
- Every criterion is independently unit-tested.

**Tests.** `test_matching_scores.py`, `test_hard_constraints.py`.

---

## F-10 MCP tool server — In progress

**User story.** As the agent, I need to fetch adopter and animal records
through tools, so that I do not depend on the application's internals.

**Main flow.** The agent spawns `python -m mcp_server` as a subprocess and
speaks MCP over stdio. Two tools: `get_adopter_profile`, `get_animal_profile`.

**Acceptance criteria.** Blueprint §8 satisfied — at least two local tools
over stdio. Each carries an LLM-facing docstring. Both are read-only. A
missing record returns a structured "not found", not an exception.

**Tests.** `test_mcp_stdio.py` — spawns the real server and round-trips both
tools.

---

## F-11 RAG knowledge base — In progress

**User story.** As the agent, I need curated care knowledge, so that my
explanations rest on documented guidance rather than invention.

**Main flow.** Markdown guides under `knowledge/` → chunked by section →
embedded with `nomic-embed-text` → stored in ChromaDB → retrieved
semantically at analysis time.

**Acceptance criteria.** Blueprint §7 satisfied. Retrieval is **semantic**:
a query sharing no keywords with the target chunk still finds it. The Vector
DB holds knowledge only — never adopter or animal records.

**Tests.** `test_rag_retrieval.py`.

---

## F-12 Autonomous agent process — In progress

**User story.** As the organization, I want an assistant that assembles
evidence and explains matches, while people keep the decisions.

**Main flow.** Independent process polls `analysis_jobs` → plans → selects
tools (MCP, RAG, web search) → observes → re-plans → computes the
deterministic score → generates a grounded explanation → writes
`MatchAnalysis` → appends `AIAnalysisCompleted`.

**Error flows.** Model unavailable → job marked FAILED with a message, and
retried on the next pass. Malformed JSON → bounded retry. Tool error → the
agent continues with the evidence it has and records what was missing.

**Acceptance criteria.**
- Runs as a separate OS process; never imports the Flask app (blueprint 4.5).
- Never finalises an adoption (spec §6.4).
- States nothing absent from its sources.
- Uses RAG first and web search only when the knowledge base cannot answer
  (spec §13), and never for the application's own records.
- Output is structured JSON: score, reasons, concerns, missing information,
  evidence.

**Tests.** `tests/agent/` — output structure, tool selection, web-search
gating, failure handling.

---

## F-13 Find My Pet — Planned

**User story.** As an adopter, I want suggestions without typing a query, so
that I can start from who I am rather than what I can describe.

**Main flow.** Adopter presses one button → eligibility filter → deterministic
Adopter→Animal ranking over available animals, returned immediately →
explanations generated asynchronously and filled in as they complete.

**Acceptance criteria.** Requires a complete profile. Ranking appears without
waiting for the model (NFR-3.1).

---

## F-14 Natural-language search — Planned

**User story.** As an adopter, I want to describe what I am looking for in my
own words.

**Main flow.** Free text, for example *"something like a hamster or rabbit, a
small rodent"* → an `INTERPRET_INTENT` job → the agent converts intent into
structured criteria → criteria are merged with the profile when the adopter
opts to use it (spec §6.4) → deterministic ranking.

**Acceptance criteria.** The distinction between stable profile and current
intent is explicit and visible in the interface.

---

## F-15 Find My Adopter — Planned

**User story.** As staff, I want existing applicants for one animal ranked,
so that I review the strongest first.

**Main flow.** Staff open an animal → applicants ranked by the
**Animal→Adopter** score → each row shows score, reasons and concerns → the
full analysis is inspectable.

**Acceptance criteria.** Ranks only people who actually applied. The score is
inspectable, never a bare number. Staff decide; the agent does not.

---

## F-16 Find More Adopters — Planned

**User story.** As staff, I want to find suitable adopters who never applied,
so that a good home is not missed.

**Main flow.** Eligibility filter (complete profile, opted in, active
account, animal available) → deterministic ranking → top N candidates.

**Acceptance criteria.** Deliberately distinct from F-15: this discovers
people who did **not** apply. Only opted-in adopters appear (spec §10).

---

## F-17 Invitations — Planned

**User story.** As staff, I want to invite a candidate to consider an animal;
as an adopter, I want to accept or decline.

**Main flow.** Staff send with an optional message → `InvitationSent` →
notification → adopter views (`InvitationViewed`) → accepts or declines →
acceptance **creates an application**, it does not approve an adoption
(spec §7.4).

**Error flows.** Responding after 72 hours → rejected; invitation is EXPIRED.
Inviting an opted-out adopter → rejected.

**Acceptance criteria.** Expiry is exactly 72 hours, computed with
timezone-aware datetimes.

---

## F-18 Internal notifications — Planned

Internal inbox only, no email (spec §23). Generated for invitations received,
invitation responses, application status changes and completed analyses.

---

## F-19 Staff dashboard — Planned

Blueprint 4.4. Available animals, pending applications, applications needing
attention, open invitations, expired invitations, animals with no suitable
applicants, animals with no applicants at all, recent activity — plus a
**Needs Attention** section linking each figure to the action it implies
(spec §22).

---

## F-20 Activity history — Planned

**User story.** As staff, I want to see how a case reached its current state.

**Main flow.** The event log for an aggregate is rendered as a timeline with
actor and timestamp.

**Acceptance criteria.** Satisfies blueprint §10's requirement that the
system can restore and display action history. Read directly from
`domain_events`, not from a separate audit table.
