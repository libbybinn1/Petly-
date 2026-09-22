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

**Can:** register, log in, create and edit their own profile, opt in or out of
proactive suggestions, search animals (structured and natural language), use
Find My Pet, view animal details, submit applications to multiple animals, view
their own applications and invitations, accept or decline invitations, withdraw
an application.

**Cannot:** see other adopters, see any applicant rankings, create or edit
animals, send invitations, approve or reject applications, view the staff
dashboard.

### 4.2 Organization staff

An employee of the adoption organization. They manage the animal roster and own
every adoption decision.

**Can:** everything an adopter can see about animals, plus: create and edit
animals, change animal availability, view all applications, open Find My Adopter
to rank existing applicants, run Find More Adopters to discover opted-in
candidates, send invitations, approve or reject applications, view the
operational dashboard and activity history.

**Cannot:** edit an adopter's profile, respond to an invitation on an adopter's
behalf, or bypass the requirement that a human makes the final decision.

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

```
Adopter registers and logs in
   -> completes their adoption profile (incl. proactive opt-in)
   -> searches for animals, or asks in natural language, or uses Find My Pet
   -> opens an animal's details
   -> submits an adoption application            [Command -> ApplicationSubmitted event]
   -> an analysis job is queued

The independent Agent process picks up the job
   -> retrieves the adopter and animal via MCP tools over stdio
   -> applies deterministic eligibility rules
   -> retrieves relevant knowledge from the Vector DB (RAG)
   -> uses Web Search only if the knowledge base cannot answer
   -> evaluates the weighted matching criteria
   -> writes a structured MatchAnalysis          [AIAnalysisCompleted event]

Staff open the animal
   -> see ranked applicants with scores, reasons and concerns
   -> optionally run Find More Adopters to discover opted-in non-applicants
   -> send an invitation                          [InvitationSent event]

The adopter sees the invitation in their inbox and responds within 72 hours
   -> accepts                                     [InvitationAccepted event]

Staff make the final human decision
   -> approve the application                     [ApplicationApproved event]
   -> the adopter's other active applications close, history preserved
                                                  [ApplicationClosedDueToOtherApproval]
```

## 7. Key user journeys

### 7.1 Find My Pet (no query required)

The adopter presses one button. The system uses their stored profile against
available animals and returns a ranked list with explanations. An adopter in an
apartment with limited daily availability who prefers a calm animal sees calm,
low-activity, apartment-suitable animals — and is told *why* each one fits.

### 7.2 Natural-language search

The adopter writes freely: *"I feel like getting something like a hamster or
rabbit, a small rodent"* or *"I want an animal that works well in an apartment
and does not require a lot of activity."* The agent interprets the intent and
converts it into meaningful search criteria.

### 7.3 Profile + current intent

The distinction matters. The **profile** answers "who am I and what generally
suits me?" The **query** answers "what am I looking for right now?" When the
adopter opts to use their profile, the agent combines both.

### 7.4 Find My Adopter (staff)

Inside an animal's page, staff rank the people who already applied for *that*
animal. Each candidate carries a score and an inspectable analysis.

### 7.5 Find More Adopters (staff)

When there are no applicants, or none are suitable, staff search the opted-in
population. This is deliberately different from 7.4: the first ranks people who
applied; the second discovers people who did not.

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

## 10. Success criteria

The product is successful when all nine scenarios run end to end against the
cloud database, with the agent running as a genuinely separate process, and
every match score in the UI can be traced to its criteria and cited sources.
