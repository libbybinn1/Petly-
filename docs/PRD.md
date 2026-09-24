# PRD — PetMatch

**Product vision, scope, users and journeys.**
Source: `PetMatch_Final_Project_Specification_EN.docx` §1–§7, §28–§31.

---

## 1. One-sentence definition

PetMatch is an AI-assisted information system for an animal adoption organization
that manages animals, adopters and applications while providing two-directional
personalized matching: adopters discover animals that fit their profile and
current intent, and staff discover the most suitable adopters for each animal,
including proactive outreach to users who opted in to receive suggestions.

## 2. The business problem

A traditional adoption catalog is a filter list. An adopter scrolls through
species and age dropdowns and guesses which animal suits their life. Staff, on
the other side, only ever see the people who happened to apply — a suitable
adopter who never browsed that particular animal is invisible to them.

Both sides lose. Animals with less obvious appeal sit unadopted while
well-matched people never learn they exist. And when a match *is* made, the
reasoning lives in a staff member's head, not in the system.

PetMatch addresses three specific failures:

1. **Discovery is generic.** Filters match attributes, not lifestyles.
2. **Outreach is reactive.** Staff cannot find good adopters who have not applied.
3. **Decisions are unexplained.** There is no recorded, inspectable rationale.

## 3. Goals

| Goal | How PetMatch achieves it |
|---|---|
| Personalized discovery | Profile-driven and natural-language matching, not just filters |
| Explainable matching | Every score carries reasons, concerns and cited evidence |
| Proactive outreach | Staff find opted-in adopters who never applied |
| Human-controlled decisions | The agent recommends; a staff member decides |
| Auditable process | Event history records how every application reached its state |

### Non-goals

PetMatch is **not** an online store, a payment system, a veterinary management
system, or a social network. It serves **one** organization — no marketplace or
multi-tenant functionality. These boundaries are from spec §3 and keep the
project focused on its central business process.

## 4. Users and roles

### 4.1 Adopter

A member of the public seeking to adopt. They maintain a profile describing
their home and lifestyle, search for animals, submit applications, and respond
to invitations.

**Can:** register (`/register`), sign in (`/login`), create and edit their own
profile (`/my/profile`), opt in or out of proactive suggestions, search animals
(`/animals/`) and describe a search in their own words (`/search/describe`),
use Find My Pet (`/my/matches`), view animal details (`/animals/<id>`), submit
applications to several animals, see their own applications
(`/my/applications`) and invitations (`/my/invitations`), read their
**notification inbox** (`/my/notifications`), accept or decline an invitation,
withdraw an application, and read the **history of their own** applications and
invitations (`/history/<type>/<id>`).

**Cannot:** see other adopters, see any applicant ranking, create or edit
animals, send invitations, approve or reject applications, view the staff
dashboard, or read anybody else's history. Each of those is a 403 from the
server, not a missing link.

### 4.2 Organization staff

An employee of the adoption organization. They manage the animal roster and own
every adoption decision.

**Can:** everything an adopter can see about animals, plus: list a new animal
(`/animals/new`), edit one (`/animals/<id>/edit`), change its availability,
work the roster table (`/animals/manage`), open Find My Adopter to rank
existing applicants (`/animals/<id>/adopters`), run Find More Adopters to
discover opted-in candidates (`/animals/<id>/discover`), send invitations,
**approve, reject, mark under review or reverse an approval**
(`POST /applications/<id>/decide`), read any analysis in full
(`/analyses/<id>`), read their own inbox, and view the operational dashboard
(`/dashboard`) and any aggregate's history.

**Cannot:** edit an adopter's profile, apply, withdraw or respond to an
invitation on an adopter's behalf, or bypass the requirement that a human makes
the final decision.

> Authorization is enforced at the server and service layers, not by hiding
> buttons (blueprint §12). Every restriction above has a corresponding API test.

## 5. Core entities

| Entity | Purpose |
|---|---|
| **User** | Account and role. |
| **AdopterProfile** | Stable facts: home type, yard, children, other animals, experience, activity level, time available, location, preferences, and the proactive-suggestions opt-in. |
| **Animal** | Name, species, breed, age, size, temperament, activity level, child and animal compatibility, special needs, location, status, images. |
| **AdoptionApplication** | One adopter's request for one animal, with its own status and history. |
| **AdoptionInvitation** | A staff-initiated proactive suggestion with a 72-hour response window. |
| **MatchAnalysis** | Score, reasons, concerns, missing information and evidence. |
| **Notification** | Internal inbox message. |
| **DomainEvent** | The append-only record of everything that happened. |

## 6. The central end-to-end process

Blueprint §5 requires at least one clear end-to-end process integrating all
components. PetMatch's is:

Every step below is a real route. Nothing in this chain is illustrative.

```
POST /register  ->  POST /login
   -> POST /my/profile          complete the profile, incl. the proactive opt-in
                                                  [AdopterProfileUpdated]
   -> GET  /animals/            structured search, or
      POST /search/describe     describe it in your own words -> 302 to
      GET  /search/describe/<job_id>   which waits for the agent, or
      GET  /my/matches          Find My Pet, ranked from the stored profile
   -> GET  /animals/<animal_id> open the details
   -> POST /my/apply/<animal_id>  submit an application
                                                  [ApplicationSubmitted]
   -> a RANK_APPLICANT row is written to analysis_jobs, in the same transaction

The independent Agent process (python -m agent_service) polls analysis_jobs
   -> claims the job
   -> retrieves the adopter and the animal via MCP tools over stdio
   -> computes the deterministic score before any model is involved
   -> retrieves relevant knowledge from the Vector DB (RAG)
   -> evaluates the web-search gate; searches only if RAG could not answer
   -> runs a bounded reason-act loop in which the model chooses its own tools
   -> writes a structured MatchAnalysis with its reasoning trace
                                                  [AIAnalysisCompleted]

Staff (dana@petmatch.org) work the queue
   -> GET  /dashboard                     what needs attention, and what the
                                          agent still owes
   -> GET  /animals/<id>/adopters         ranked applicants, scores, concerns
   -> GET  /analyses/<id>                 the full analysis and the trace
   -> GET  /animals/<id>/discover         optionally, opted-in non-applicants
   -> POST /animals/<id>/invite           send one an invitation
                                                  [InvitationSent] + notification

The adopter is told, and answers within 72 hours
   -> GET  /my/notifications              the inbox; the message links onward
   -> GET  /my/invitations                opening it records that they saw it
                                                  [InvitationViewed]
   -> POST /my/invitations/<id>/respond   accepting creates an APPLICATION;
                                          it does not approve an adoption
                                                  [InvitationAccepted]

Staff make the final human decision
   -> POST /applications/<id>/decide  decision=APPROVE
                                                  [ApplicationApproved]
   -> that adopter's other active applications close, causation recorded
                                                  [ApplicationClosedDueToOtherApproval]
   -> the animal moves to ADOPTION_IN_PROGRESS    [AnimalStatusChanged]
   -> the adopter is notified

And if it falls through
   -> POST /applications/<id>/decide  decision=REVERSE
   -> exactly those applications closed by *that* approval reopen
                                                  [ApplicationReopened]
   -> GET /history/application/<id>   both the adopter and staff can read what
                                      happened, from the event log itself
```

Two steps are worth pointing at, because they are where the product's claims
live. **The invitation reaches the adopter through the inbox**, not through an
email nobody sent - spec section 23 makes that internal by design. And **the
decision is a human pressing a button**: the agent's analysis sits beside it as
advice, and no code path lets it approve anything (spec section 6.4).

## 7. Key user journeys

### 7.1 Find My Pet (no query required)

The adopter presses one button. The system uses their stored profile against
available animals and returns a ranked list with explanations. An adopter in an
apartment with limited daily availability who prefers a calm animal sees calm,
low-activity, apartment-suitable animals — and is told *why* each one fits.

### 7.2 Natural-language search

On `/search/describe` the adopter writes freely: *"I feel like getting
something like a hamster or rabbit, a small rodent"* or *"I want an animal that
works well in an apartment and does not require a lot of activity."*

The request itself does no thinking. It validates the text, hands an
`INTERPRET_INTENT` job to the agent and redirects to a page that waits — which
refreshes itself, shows how long it has been waiting, and turns into results
the moment the interpretation lands. That indirection is not incidental: a
model call on this hardware takes sixteen seconds, and a search box that hangs
for sixteen seconds is not a search box.

The adopter is shown **how they were understood** — "A small, calm rodent that
is good with children" — beside the results, so an interpretation that missed
the point is visible rather than mysterious.

### 7.3 Profile + current intent

The distinction matters. The **profile** answers "who am I and what generally
suits me?" The **query** answers "what am I looking for right now?" A checkbox
on the describe form — *use my profile* — decides whether they are combined.

It is genuinely opt-in, and genuinely different: without it the intent becomes
a filter and the results are simply the animals that match. With it, the same
filtered set is **scored** against the stored profile and ordered best first,
each card carrying its score. It is offered only to a signed-in adopter with a
complete profile, because scoring against half a household would be guesswork
presented as personalisation.

### 7.4 The inbox and the invitation

An adopter is never expected to notice something by luck. When staff invite
them, a message is written to their **inbox** (`/my/notifications`), with the
unread count visible on every page, and the message links to the invitation
itself. Opening the invitation records that they saw it; they have 72 hours to
answer, and the countdown is on the screen.

Accepting **creates an application**. It does not approve an adoption — that
distinction is the whole point of the feature (spec §7.4), and it is the thing
most likely to be misread by somebody skimming the interface, so the flash
message says it in words.

### 7.5 Find My Adopter (staff)

From an animal's page, staff rank the people who already applied for *that*
animal, using the direction of the score that weighs the animal's needs first.
Each candidate carries a score ring, a criterion breakdown, whatever they wrote
in their application, a link to the full analysis with the agent's reasoning
trace, and a link to that application's history.

### 7.6 Find More Adopters (staff)

When there are no applicants, or none are suitable, staff search the opted-in
population. This is deliberately different from 7.5: the first ranks people who
applied; the second discovers people who did not. Candidates ruled out by an
eligibility rule are listed **with the reason**, because "twelve excluded" is
not something a person can check.

### 7.7 The decision (staff)

Nothing above decides anything. A staff member presses Approve, Reject, Mark
under review, or Reverse, on `POST /applications/<id>/decide`. Approving closes
that adopter's other active applications and moves the animal on; the screen
reports how many closed. A rival adopter's application for the same animal
stays open, for a human to reject with a reason.

This is the step the whole architecture exists to serve, and the step the agent
cannot reach: it has no import path to any of those four commands.

### 7.8 Seeing what happened (both roles)

`/history/<type>/<id>` renders an aggregate's event stream as a timeline.
Staff can read any of them; an adopter can read **their own** applications and
invitations — which matters, because an application closed by somebody else's
approval and later reopened is otherwise something they would have to take on
trust.

## 8. Matching philosophy

From spec §8: PetMatch must not depend on an unexplained number generated by an
LLM. The system defines meaningful criteria and weights; the agent interprets
information, gathers evidence and explains the result.

**Design consequence:** scoring is deterministic Python. The LLM's job is
interpretation and explanation, never arithmetic. This makes the scores
reproducible, unit-testable, and defensible to a user who asks "why 72?"

The two directions use **different** weightings. Adopter→Animal emphasizes the
person's lifestyle and stated preferences. Animal→Adopter emphasizes the
animal's care requirements and temperament, which may outrank what the person
said they wanted.

## 9. Demo scenarios

Per spec §28, the product is demonstrated by several short scenarios rather than
one long one:

1. Adopter creates a profile and uses Find My Pet.
2. Adopter enters a natural-language request and gets personalized results.
3. Adopter submits multiple applications.
4. Staff open an animal and review ranked existing applicants.
5. Staff use Find More Adopters and discover opted-in candidates.
6. Staff send an invitation; the adopter receives it in their personal area.
7. One application is approved; related active applications close while history
   remains available.
8. Staff review dashboard statistics and recent activity.
9. Agent execution demonstrates RAG, MCP tool usage, and Web Search when
   appropriate.

`docs/DEMO.md` is the runbook: for each of the nine, the exact clicks, the
account to use, what to point at on the screen, and which graded pattern it
demonstrates.

## 10. Success criteria

The product is successful when all nine scenarios run end to end against the
cloud database, with the agent running as a genuinely separate process, and
every match score in the interface can be traced to its criteria and its cited
sources.

As of the hardening sprint all nine are demonstrable. Five are also covered by
automated browser journeys; the other four — the approval cascade, the reopen
rule, replay, and the agent's tool loop — are proven by integration and agent
tests instead, because a browser is a poor witness for a write that happens in
one transaction and a loop that happens in another process. `docs/TESTING.md`
§3.5 says which is which.
