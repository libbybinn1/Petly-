# DEMO — PetMatch presentation runbook

**What to click, in what order, with which account, and which graded pattern
each step demonstrates.**
Sources: PetMatch spec §28 (the nine scenarios); course blueprint §21 (what
the instructor expects to see).

Blueprint §21 is a list of things an examiner expects to *see*, not a list of
things to claim. This document maps each of them onto a screen, a command or a
file, so nothing is forgotten under time pressure and nothing is asserted
without being shown.

Read §1 the day before. Run §2 the hour before. §3 is the demonstration
itself; §4 answers the blueprint §21 list directly; §5 is the honest list of
what is missing, which is better said first than discovered by a question.

---

## 1. Before the day

- **Do not reseed during the presentation.** `scripts/db.py fresh` **deletes
  every row** and takes **9–12 minutes**, because it fetches a breed-accurate
  photograph for each of 157 animals from free public APIs and paces itself to
  stay inside their rate limits. Reseed the evening before, and confirm it
  worked.
- **Never run `db.py seed` over existing data.** `users.email` is unique and a
  second seed collides part-way through, leaving a half-loaded database. The
  only safe reload is `fresh`.
- **Warm the model.** The first model call after a cold start spends 110–145
  seconds loading `qwen2.5:3b-instruct` into memory. Run one analysis the hour
  before so the model is resident; a warm tool-enabled turn is 36–43 seconds.
- **Set `AGENT_MAX_REASONING_STEPS=4` in `.env`.** The default of 8 is a
  correctness bound, not a latency budget. At ~40 s a turn, eight turns is a
  worst case of several minutes for one analysis. Four is plenty: with the
  prerequisite evidence already in the prompt, the 3B model usually answers on
  its first or second turn.
- **Have two terminals and two browser windows ready** — one signed in as
  staff, one as the adopter. Switching accounts live costs thirty seconds of
  silence each time.

---

## 2. Startup checklist

The interpreter lives **outside** the project, because OneDrive corrupts a
virtual environment it syncs. There is no `.venv` in this repository.

```bash
set PY=C:\Users\libbyb\venvs\petmatch\Scripts\python.exe
```

| # | Command | What it must say |
|---|---|---|
| 1 | `%PY% scripts\check_environment.py` | Five checks pass: the Somee database, the Ollama chat model, the Ollama embedding model, the vector database and web search. This is also the only thing in the project that proves the **cloud** database is reachable, so it is worth running on screen. |
| 2 | `%PY% scripts\db.py tables` | Row counts, non-zero. Use this instead of `fresh` unless something is actually broken. |
| 3 | `%PY% scripts\ingest_knowledge.py` | "ingesting 17 guide(s)… stored 104 chunks". Only needed after editing `knowledge/`. |
| 4 | `%PY% run.py` | Serving on `http://127.0.0.1:5000`. **Terminal 1.** |
| 5 | `%PY% -m agent_service` | The polling loop starts. **Terminal 2 — leave it visible.** |

**Terminal 2 is part of the demonstration.** Blueprint §4 item 5 requires the
agent to run as an independent process; the most direct way to show that is a
second window with its own log, which you can stop and restart while the web
application keeps serving pages.

### The demo accounts

Password `Password123!` for both.

| Role | Email | Use it for |
|---|---|---|
| Staff | `dana@petmatch.org` | Scenarios 4–8 |
| Adopter | `maya@example.com` | Scenarios 1–3, 6 |

**The story is already in the data.** Maya has an **APPROVED** application for
**Smaug**, a bearded dragon, with **three other applications CLOSED by the
§7.5 cascade**. That means scenario 7 has something real to show before you
touch anything, and you can demonstrate the *reversal* on live data rather
than having to build the situation up first.

---

## 3. The nine scenarios

### Scenario 1 — Adopter creates a profile and uses Find My Pet

*Demonstrates: MVC, CQRS, the deterministic scorer, blueprint 4.5 data entry.*

1. Sign in as `maya@example.com` → `/login`.
2. Go to **Profile** (`/my/profile`). The profile already exists; show the
   form and the fields rather than re-saving it.
   - Point at **"Open to proactive suggestions"**. It defaults to off and is
     never inferred from a missing checkbox — that one box is what makes
     scenario 5 possible, and spec §5.1 requires it.
   - Tick **"Household has children"** and watch the youngest-child field
     appear. Untick it and watch it go. That is `[data-shown-by]`, and with
     JavaScript disabled the field is simply always visible.
3. Submit the form with children ticked and **no age**. It comes back under
   **400** with an error summary whose links jump to the offending field.
   *Say:* the browser check is a convenience; this is the real one, and
   `test_children_without_an_age_is_rejected` posts exactly this.
4. Go to **My matches** (`/my/matches`). Ranked animals appear **instantly**.
   - *Say:* no model was involved. The score is arithmetic —
     `app/domain/matching.py`, eleven criteria, weights that sum to 1.0.
   - Open one card's analysis. Point at the criterion bars: **the breakdown
     adds up to the number in the ring.** That is the answer to "why 72?", and
     it is the single strongest artifact in the project.

### Scenario 2 — Adopter describes what they want in their own words

*Demonstrates: the agent off the request path, spec §6.3 and §6.4, NFR-3.1.*

1. Go to `/search/describe` (signed in as Maya, or sign out first to show it
   is public).
2. Type: *"I feel like getting something like a hamster or rabbit, a small
   rodent"*. Tick **use my profile**.
3. Press Search. **Watch the URL change** to `/search/describe/<job_id>`.
   *Say:* the POST did no thinking. It validated the text, wrote a row to
   `analysis_jobs` and redirected. A model call on this machine is sixteen
   seconds, and CLAUDE.md R4 forbids putting one in a request path.
4. **Switch to terminal 2.** The agent picks the job up. Switch back; the page
   has refreshed itself and now shows results. No JavaScript is involved — it
   is a `<meta http-equiv="refresh">`.
5. Point at the line showing **how the description was understood**, and at
   the fact that the results carry **scores**, because the profile was fused
   in. Untick the box and repeat to show the difference: filtered, but not
   ranked.
6. *If asked "what if someone types nonsense?"* — try an empty box. It is
   refused with 400 and **nothing is enqueued**; an empty textarea costs no
   inference.

### Scenario 3 — Adopter submits multiple applications

*Demonstrates: CQRS command/query separation, event sourcing, the queue.*

1. As Maya, open any **AVAILABLE** animal from `/animals/`.
2. Write a short message and apply. You land on `/my/applications` — a
   **different screen, served by a different query**. *Say:* the command
   returned an identifier, not the page's data. That is R2, and
   `test_command_handlers_never_return_read_data` enforces it.
3. Apply for a second animal. Both appear.
4. Try to apply for the **same** animal twice. Refused, with a readable
   message. *Say:* the handler checks, **and** a filtered unique index refuses
   it under concurrency — a check-then-insert is not atomic.
5. **Switch to terminal 2** and show two new `RANK_APPLICANT` jobs being
   claimed and completed.

### Scenario 4 — Staff review ranked applicants for one animal

*Demonstrates: the second scoring direction, the agent's output, spec §9.*

1. Sign in as `dana@petmatch.org` in the second window.
2. `/animals/manage` → find an animal with applicants → **View applicants**
   (`/animals/<id>/adopters`).
3. Each row: a score ring, the **fit grade** (a letter, a verdict and
   "where the points went"), the applicant's own message, **Full analysis**,
   and **History**. *Say:* the deductions add up to exactly 100 minus the
   score. Every missing point is attributed to a criterion, with the
   scorer's own reason. It is arithmetic, not model output.
4. *Say:* this is the **Animal→Adopter** direction. Daily availability weighs
   0.19 here and 0.13 the other way; species preference weighs 0.01 here and
   0.17 the other way. The two directions genuinely disagree, because spec §9
   says the animal's care needs may outrank what the person asked for.
5. Open **Full analysis** (`/analyses/<id>`). This is where you spend time:
   - **Reasons and concerns**, written by the model.
   - **Cited sources**, with the ones the explanation actually relied on
     marked as cited and the merely consulted ones not.
   - **"How the agent reasoned"** — the reasoning trace: each step, the tool it
     called, the query it used, the gate decision and the rule behind it.
     *Say:* including the turns it spent on something that did not work.

### Scenario 5 — Staff discover adopters who never applied

*Demonstrates: spec §7.3 and §10, and the opt-in doing real work.*

1. From an **AVAILABLE** animal, choose **Discover adopters**
   (`/animals/<id>/discover`).
2. Ranked candidates appear — none of whom applied.
3. Expand the **excluded candidates** disclosure. Each carries the rule that
   ruled them out: incomplete profile, not opted in, already applied, already
   invited. *Say:* three of the forty seeded adopters are deliberately
   incomplete, so the filter always has something to exclude.
4. *Contrast it with scenario 4 explicitly.* They look similar and are not:
   one ranks people who applied, the other finds people who did not. The spec
   asks for both, and conflating them is the easiest mistake in the product.

### Scenario 6 — Staff send an invitation; the adopter receives it

*Demonstrates: the 72-hour window, the internal inbox (spec §23), FR-8.4.*

1. On the discovery screen, press **Send invitation** for a candidate.
2. **Switch to the adopter's window.** The **Notifications** badge in the top
   bar has gone up. Open the inbox; the message is there and links onward.
   *Say:* spec §23 says internal inbox, no email — so there is no email.
3. Follow it to `/my/invitations`. The countdown is on the card. *Say:*
   opening this page recorded `InvitationViewed`; the window is
   `INVITATION_EXPIRY_HOURS`, 72 by default, and the boundary is enforced to
   the microsecond.
4. Press **Accept**. Note where it lands: `/my/applications`. **Accepting an
   invitation creates an application — it does not approve an adoption**
   (spec §7.4). The flash message says so.
5. Press **Decline** on a different one and note that it asks first
   (`data-confirm`), and that without JavaScript it simply submits, because
   the server is still the authority.

### Scenario 7 — Approval, the cascade, and the reopen rule

*The centrepiece. Demonstrates: event sourcing, and why it was chosen.*

This is the scenario the whole design exists to serve, so give it the most
time. The seed has already put the world in the state you need.

1. As staff, open **Smaug**'s applicants. Maya's application is **APPROVED**.
2. As Maya (other window), open `/my/applications`. Three applications read
   **CLOSED**. *Say:* she did not withdraw them; approving one closed the
   others — spec §7.5.
3. Open the **History** link on one of the closed applications
   (`/history/application/<id>`). The timeline shows
   `ApplicationSubmitted` → `ApplicationClosedDueToOtherApproval`.
   *Say:* the closing event carries **which approval caused it**. That is the
   fact a current-state schema destroys, and it is the justification for
   event sourcing recorded in `ARCHITECTURE.md` §4.
   - Note that an **adopter** is reading this. History is not staff-only,
     because §7.5 is written from her point of view.
4. As staff, on Smaug's applicant screen, press **Reverse approval**.
5. Back in Maya's window, refresh `/my/applications`. **Exactly those three**
   are back to SUBMITTED. Smaug is AVAILABLE again.
6. *The point to land:* if Maya had also **withdrawn** an application herself,
   it would **not** have come back. The system reopens what *this approval*
   closed, and nothing else — derived from the events, keeping the latest
   cause per application, so an application closed twice is judged by its most
   recent closure. `tests/integration/test_event_sourcing.py` proves all three
   cases.
7. *If asked "could you rebuild the database from the log?"* — yes, and §3.10
   below shows it live.

### Scenario 8 — Staff dashboard

*Demonstrates: blueprint 4.4, spec §22, and the event log as a read source.*

1. `/dashboard`.
2. Six tiles, all non-zero because the seed was sized for it. Click one — it
   goes to the roster **filtered to that status**.
3. **Needs Attention**: prose a person wrote. Point at the singular —
   "1 application needs attention", not "1 application(s)".
4. **The agent queue strip**: outstanding and failed work, and how long the
   oldest job has waited. *Say:* this is the only place the second process is
   visible as a process, and it is what makes a stuck queue distinguishable
   from a busy one.
5. **Recent activity**: read straight from `domain_events`, not from an audit
   table. Every timestamp is a `<time datetime=…>` element.
6. Be ready for the obvious question, and answer it before it is asked: **two
   tiles are not links** — invitations awaiting a reply, and analyses
   completed — because the screens they would open do not exist. A tile that
   looks clickable and goes nowhere is worse than one that does not.

### Scenario 9 — The agent: RAG, MCP tools and web search

*Demonstrates: blueprint §6, §7, §8; spec §11–§14.*

Do this last, with terminal 2 in view, and narrate the log.

1. Trigger an analysis: apply for an animal as Maya, or `db.py events` to find
   a recent one.
2. In terminal 2, walk the log:
   - **MCP over stdio.** The agent spawns `python -m mcp_server` as a
     subprocess and calls `get_adopter_profile` and `get_animal_profile`. *Say:*
     the agent does not own the application's data; it asks for it through
     tools, over stdin/stdout, with nothing on a network port.
   - **The deterministic score** is computed *before* any model turn, and
     injected into the prompt as an immutable fact. A model that returns
     `"score": 9999` changes nothing.
   - **RAG.** `rag_search` against 104 chunks from 17 curated guides,
     retrieved semantically.
   - **The web-search gate.** First a `knowledge_coverage` step says whether
     the curated guides name this animal. Then the gate names its decision:
     `REFUSED_RAG_SUFFICIENT` when they do, `ALLOWED_KNOWLEDGE_GAP` when they
     don't, and `REFUSED_OWN_RECORDS` for a question about our own data.
     *Say:* RAG sufficiency still wins. Generic apartment advice does not
     count as knowing about a Saluki.
   - **To show both outcomes**, analyse two animals. The **Weimaraner** is not
     in the guides, so the web is searched and a web source appears on the
     analysis page with "Web search was used". The **Border Collie** is
     covered by `species-dogs.md`, so the web stays shut.
   - **The loop.** The manifest of four tools goes to the model on every turn,
     and the model decides. An invented tool name is reported back rather than
     raised, because small models invent tool names and one bad turn should
     not cost the job.
3. Open the finished analysis in the browser and show the reasoning trace
   matching the log line for line.
4. **Kill terminal 2 and reload a page.** The site keeps working; new cards
   show the pending state. Restart the agent and they fill in. *Say:* a model
   outage degrades the product to *scores without prose*, not to no service.
   That is the whole reason the number is arithmetic.

### 3.10 Showing replay on demand

If the examiner asks whether the log can rebuild the state — and scenario 7
invites the question — there are two ways.

**Fast and safe:**

```bash
%PY% -m pytest tests/integration/test_event_sourcing.py -v
```

Fourteen tests, names readable aloud:
`test_every_application_replays_to_its_stored_state_after_a_cascade`,
`test_a_corrupted_row_is_detected`, `test_a_read_only_rebuild_changes_nothing`,
`test_applying_a_rebuild_repairs_the_row`.

**Live, against the real database** — read-only, so it is safe:

```python
%PY%
>>> from app.config import load_configuration
>>> from app.infrastructure.database import create_database_engine, create_session_factory
>>> from app.eventstore.projections import rebuild_projections
>>> session = create_session_factory(create_database_engine(load_configuration()))()
>>> report = rebuild_projections(session)     # apply=False by default
>>> report
```

An empty mismatch list is the point: **every row in the database is exactly
what its events say it should be.** `apply=True` would write the replayed
state back, which is the repair path — do not use it on stage.

### 3.11 Showing the guards

Two commands, both quick, both worth running live.

```bash
%PY% scripts\verify_requirements.py
```

23 of 23. Read two lines aloud — the agent-process check and the CQRS check —
and say what makes them worth trusting: **this script inspects behaviour**. It
builds the Flask app and reads its URL map, parses the code with `ast`, and
calls the scorer twice to prove determinism. An earlier version answered most
questions with `"substring" in source` and reported 25/25 while a manual audit
found real gaps.

```bash
%PY% -m pytest tests/unit/test_architecture_guard.py -v
```

Twenty-one tests that turn CLAUDE.md R2 into something that fails the build.
The one to read out is
`test_agent_never_names_a_decision_command`: the agent has no import path to
`ApproveApplicationCommand`, which is a much better answer to "how do you know
the AI cannot decide?" than a policy statement.

---

## 4. The blueprint §21 list, answered

| What the instructor expects to see | Where to show it |
|---|---|
| **A working system, not only partial code** | The whole of §3. Two processes running, 157 animals, 81 applications, real photographs, and every dashboard tile non-zero. |
| **A clear end-to-end user process** | Scenarios 1 → 3 → 4 → 6 → 7 in order, following one adopter. `docs/PRD.md` §6 is the same chain written as routes. |
| **Why this architecture was selected** | `ARCHITECTURE.md` §3 ("why CQRS suits this system": heavily read-skewed, few sharply-defined writes) and §4 ("why event sourcing is right for *this* process": the §7.5 reopen rule). Say the reason, not the pattern name. |
| **A demonstration of CQRS and MVC** | Scenario 3, step 2: the command returned an identifier and a *separate query* served the next screen. Then `tests/unit/test_architecture_guard.py` — the rules are enforced by a test, not by discipline. Then `app/cqrs/base.py`: a query dispatch **rolls back**, so an accidental write in a read path cannot persist. |
| **A demonstration of Event Sourcing** | Scenario 7, end to end, plus a history timeline on screen, plus §3.10's replay. |
| **A demonstration of the Agent with RAG, Tools and search** | Scenario 9, narrated from terminal 2, then the reasoning trace on `/analyses/<id>`. |
| **An explanation of MCP and stdio** | Scenario 9, step 2, plus `docs/MCP.md` §3 and §5. The strongest point: the tool descriptions the model reads are the **server's own docstrings**, read at runtime, so there is no second copy to drift. |
| **An explanation of Skills and Rules** | `docs/SKILLS_AND_RULES.md` §4 — not the list of rules, but what they caught: R1 on the database engine and on the reseed, R2 on the session in the auth controller, R3's negative tests on twelve real bugs. |
| **The documents as the source guiding development** | `PLANNING.md` §2 — six blocking questions asked *before* coding, with their answers and what each changed. Then `docs/FEATURES.md`, where every feature carries the blueprint §16 template. Then `tests/unit/test_docs_reference_real_artifacts.py`: the documents are checked against the code by a test, so a stale claim fails the build. |
| **Good familiarity with the code, especially the patterns** | Have four files open and be able to talk through any of them: `app/domain/matching.py` (the arithmetic behind every number on screen), `app/cqrs/base.py` (commit versus rollback), `app/cqrs/commands/application_commands.py` (the cascade and the event-derived reopen), `agent_service/loop.py` (the reason-act loop and the tool manifest). |

---

## 5. Known limitations — say these before you are asked

A demonstration that names its own gaps is stronger than one that is caught by
them. All of these are documented in the relevant `docs/` file too.

1. **Two dashboard tiles are not links.** "Invitations awaiting a reply" and
   "Match analyses completed" have no screen of their own: there is no
   cross-animal invitation list and no index of analyses. They are figures,
   and they are deliberately not styled as links.
2. **There is no staff invitation-management screen.** Staff send invitations
   from the discovery screen and see the outcome on the dashboard and in the
   event history; there is no list of outstanding invitations to work through.
3. **The staff roster table has no age or size select yet.** The route and the
   query both honour `?age_range=` and `?size=`, and the public search screen
   renders the controls — the staff template does not.
4. **The agent usually answers on its first turn.** Given prerequisite
   evidence already in the prompt, `qwen2.5:3b-instruct` typically produces its
   final JSON immediately rather than calling another tool. That is the
   *correct* decision and the one the manifest asks for, but it means a live
   run often shows a short trace. `tests/agent/test_reasoning_loop.py` drives
   the multi-turn path with scripted models, and the trace always records what
   was offered even when nothing more was called.
5. **Scores cluster.** With eleven weighted criteria and a roster chosen to
   have something for everybody, most non-disqualified pairings land in a
   fairly narrow band. Disqualification is the sharp signal; the ranking order
   is the useful one. Do not promise dramatic spread.
6. **E2E tests run against SQLite, not Somee.** A browser page's request
   fan-out trips the free tier's throttle, and the suite was measuring rate
   limits rather than journeys. What exercises SQL Server 2014 is
   `scripts/check_environment.py`, `scripts/db.py` and the running
   application. `docs/TESTING.md` §1 states the trade-off.
7. **`app/repositories/`, `app/services/` and `app/views/` are empty stubs.**
   If asked "where is your repository layer?", the answer is that there is not
   one: the CQRS handlers hold the session through the bus, and the view
   models live beside their query. `ARCHITECTURE.md` §2 says so rather than
   implying otherwise.
8. **Nesting depth 3 is a review rule, not a tool rule.** Ruff enforces
   `max-branches = 8`, which is related but not the same thing.
9. **Nothing is pushed to GitHub yet.** The repository is local with a remote
   configured; the push waits on the owner's sign-in (`PLANNING.md` §2 Q6).
10. **The invitation's staff message is fixed text.** The route accepts a
    custom `staff_message`, but the discovery screen sends a hidden default;
    there is no control for staff to write their own.

---

## 6. If something goes wrong

| Symptom | Cause and remedy |
|---|---|
| A page hangs, then errors | Somee's free tier throttles or briefly refuses connections. Run `%PY% scripts\db.py check`. Reload; it usually clears within a minute. |
| Flask or `pymssql` reports TDS 20002 | FreeTDS on Windows cannot convert the login packet as UTF-8. The app URL includes `charset=CP1252`. A hand-written `pymssql.connect` must pass the same `charset`. |
| The agent logs a model error | Ollama is not running, or the model was evicted. Start it and let the first call reload the model. **The site keeps working** — that is worth saying out loud rather than apologising for. |
| A form answers "That form has expired" | The CSRF token aged out with the session. Reload the page and submit again; this is the intended behaviour and a reasonable thing to show deliberately. |
| An import error that differs on every run | The wrong interpreter. Use `C:\Users\libbyb\venvs\petmatch\Scripts\python.exe`; a virtual environment inside OneDrive corrupts itself. |
| The describe page never resolves | The agent is not running. Start terminal 2; the page picks the result up on its next refresh. |
