# API — PetMatch

**Endpoints, inputs, outputs and authorization.**
Sources: course blueprint §12, §13; PetMatch spec §20, §21.

PetMatch is server-rendered, so most endpoints return HTML rather than JSON.
They are still an API surface with a contract, and every one is
authorization-tested. Status codes matter as much as bodies: blueprint §12
requires the server to enforce permissions, so a wrong-role request must
return 403 rather than a redirect or an empty page.

**This document lists all 30 application routes** (`app.url_map` also carries
Flask's `/static/<path:filename>`). Nothing below is planned; every entry
exists and is tested. `tests/api/test_qa_authorization.py::test_the_documented_read_routes_all_exist`
checks the read surface against the URL map.

---

## 1. Authorization model

| Marker | Meaning |
|---|---|
| **Public** | No authentication required |
| **Signed in** | Any authenticated account, either role |
| **Adopter** | Signed in with role ADOPTER |
| **Staff** | Signed in with role STAFF |
| **Owner** | Signed in *and* the record belongs to this account |

Enforcement is by `@require_sign_in`, `@require_staff()` and
`@require_adopter()` at the controller boundary, plus an ownership check
inside the command or query handler for anything marked **Owner**. Changing
an identifier in a URL therefore does not expose another person's data — the
route check and the data check are separate, and the data check lives with
the rule rather than in the route.

### Standard failure responses

| Code | When |
|---|---|
| **302 → `/login`** | **Not signed in.** `@require_sign_in` is the outer decorator on every protected route, so a signed-out request never reaches the role check. It is redirected with `?next=` so signing in returns the visitor to where they were going. A 401 page would be a dead end for somebody who simply has not signed in yet. |
| 400 | Validation failed, or a required parameter is missing. The form is re-rendered with its errors under 400, never under a misleading 200. |
| 401 | Bad credentials on `POST /login` only; and on `GET /api/analysis-status`, which is JSON and has no page to redirect to. |
| 403 | Signed in with the wrong role, or not the owner. Also returned for a record that does not exist where a 404 would let identifiers be enumerated. |
| 404 | Record does not exist. |
| 409 | Conflicts with existing state — currently only a duplicate registration. |

Business-rule refusals that a person can act on (an unavailable animal, a
duplicate application, an expired invitation) **flash a specific message and
redirect**, rather than returning a bare status. "Animal is no longer
available" is more use than "Bad request".

### CSRF

Every POST form carries a hidden `csrf_token` field
(`{{ csrf_token() }}`), enforced by Flask-WTF's `CSRFProtect` on the whole
application. A POST without a valid token is refused before the view runs and
renders a friendly page under **400**: "That form has expired — go back,
reload the page and try again", because the usual innocent cause is a form
left open past the session rollover. The session cookie is
`SameSite=Lax` and `HttpOnly`, which is the half of the defence that keeps
working if a form is ever missed.

`tests/unit/test_feature_csrf_tokens.py` parses every template and asserts
each POST form carries exactly one token, in the body and not inside the
opening `<form>` tag; GET forms deliberately carry none.

---

## 2. Authentication

### `GET /register` — Public
Registration form.

### `POST /register` — Public

| Field | Type | Rules |
|---|---|---|
| `full_name` | string | required, non-blank, ≤ 150 characters |
| `email` | string | required, must contain `@` and a dotted domain, unique |
| `password` | string | required, ≥ 8 characters |
| `confirm_password` | string | required, must equal `password` |

**302** to `/` on success, signed in · **400** validation failed, form
redisplayed with every problem listed at once · **409** email already
registered.

The 150-character name limit is not arbitrary: `users.full_name` is
`NVARCHAR(150)`, and a longer value truncates on SQL Server 2014 rather than
saving, surfacing as a 500 on a form that looked perfectly valid.

The account is always created with role ADOPTER. A forged `role` field
changes nothing — `RegisterAdopterCommand` has no such parameter
(`test_registration_cannot_grant_itself_staff`).

### `GET /login` — Public
Sign-in form. A signed-in visitor is redirected to `/`.

### `POST /login` — Public

| Field | Type |
|---|---|
| `email` | string, required |
| `password` | string, required |

**302** to `?next=` when it is a relative path within this application,
otherwise to `/` · **401** bad credentials, form re-rendered · **403**
account deactivated.

> The 401 message is identical for an unknown email and a wrong password, so
> the endpoint cannot be used to discover which addresses are registered.
> `test_neither_refusal_hints_that_the_account_exists` asserts the wording.

`next` is parsed, not inspected character by character: anything with a
scheme or a host is off-site, including `//evil.example` and `/\evil.example`,
which some browsers normalise into a full URL.

### `POST /logout` — Signed in
**302** to `/login`.

> POST only. A `GET /logout` would let any third-party page sign the user out
> by embedding an image or a link.

---

## 3. Animals

### `GET /` — Public
Landing page: six available animals and a live count of what is available.

### `GET /animals/` — Public
Search (blueprint 4.1).

| Query parameter | Type | Default |
|---|---|---|
| `q` | free text over name, breed and description | — |
| `species` | `Species` value | any |
| `size` | `AnimalSize` value | any |
| `activity_level` | `ActivityLevel` value | any |
| `temperament` | `Temperament` value (set by the natural-language path; the filter form does not expose it) | any |
| `city` | string | any |
| `age_range` | `0-2`, `2-8` or `8+` | any |
| `good_with_children` | checkbox | off |
| `good_with_other_animals` | checkbox | off |
| `include_unavailable` | checkbox | off (available only) |
| `page` | integer 1–100000 | 1 |

Returns a page of **24** results with the total count. Filters combine with
AND and are preserved across pagination.

Every parameter is attacker-controlled and treated as such: an unrecognised
`species` or `age_range` narrows to nothing or to no constraint rather than
erroring, `page` falls back to 1 when it is not an integer, is below 1, or
exceeds 100000 (an unbounded Python integer reaching the driver is a 500 from
a route anonymous visitors can reach), `%` and `_` in `q` are escaped so they
are searched for literally, and the whole term is a bound parameter.

The age bands do not overlap and leave no gap: `0-2` is age < 2, `2-8` is
2 ≤ age < 8, `8+` is age ≥ 8 (`AGE_RANGE_BOUNDS`).

### `GET /animals/<animal_id>` — Public
Details (blueprint 4.2). **404** if the animal does not exist. Staff
additionally see the active applicant count and the availability controls.

### `GET /animals/manage` — **Staff**
Tabular management (blueprint 4.3). Optional `status`, `species`, `size`,
`age_range`, `q` and `page`; **40** rows per page.

### `GET /animals/new` — **Staff**
The listing form, with every dropdown populated from the enums.

### `POST /animals/new` — **Staff**

| Field | Rules |
|---|---|
| `name` | required, ≤ 100 characters |
| `species` | required, a `Species` value |
| `breed` | optional, ≤ 100 characters |
| `age_years` | required, numeric, above 0 and within a plausible range |
| `size` | required, an `AnimalSize` value |
| `temperament` | required, a `Temperament` value |
| `activity_level` | required, an `ActivityLevel` value |
| `required_space` | required, an `AnimalSize` value |
| `city` | required |
| `status` | optional, an `AnimalStatus` value; blank means AVAILABLE |
| `description` | required |
| `good_with_children` / `good_with_other_animals` / `has_special_needs` | checkboxes |
| `special_needs_description` | required when `has_special_needs` is ticked, discarded when it is not |
| `image_urls` | repeated field, **at least one required** (spec §24) |

**302** to the new animal's details page · **400** validation failed, with
every problem reported at once and the submitted values redisplayed.

Appends `AnimalListed`. The image rule is enforced by
`animal_rules.ensure_animal_has_an_image`, which raises `NoImageError`; it
cannot be a table constraint because it is a cross-table cardinality rule.

### `GET /animals/<animal_id>/edit` — **Staff**
The same form, populated. **404** for an unknown animal.

### `POST /animals/<animal_id>/edit` — **Staff**
Same fields and rules as creation. **302** to details · **400** validation
failed · **404** unknown animal. Appends `AnimalUpdated`.

The image rule is checked here too: an edit that removed the last photograph
is refused with a flash message, not silently accepted
(`test_an_edit_cannot_remove_the_last_photograph`).

### `POST /animals/<animal_id>/status` — **Staff**

| Field | Rules |
|---|---|
| `status` | required, one of the `AnimalStatus` values |

**302** to details · **400** for a value outside the enum · **404** unknown
animal. Appends `AnimalStatusChanged`.

---

## 4. Adopter profile

### `GET /my/profile` — **Adopter**
The profile form. Reachable by an adopter who has no profile yet — that is
how one is created.

### `POST /my/profile` — **Adopter**

| Field | Rules |
|---|---|
| `home_type` | `HomeType` value, required |
| `has_yard` | checkbox |
| `yard_size_sqm` | integer ≥ 0, optional; discarded when `has_yard` is off |
| `household_has_children` | checkbox |
| `youngest_child_age` | 0–18, required when children are present, rejected when they are not |
| `has_other_animals` | checkbox |
| `other_animals_description` | ≤ 500 characters; discarded when `has_other_animals` is off |
| `experience_level` | `ExperienceLevel` value, required |
| `activity_level` | `ActivityLevel` value, required |
| `daily_hours_available` | 0–24, required |
| `city` | required, 2–100 characters |
| `preferred_species` | multi-select of `Species` values, optional |
| `preferred_size` | `AnimalSize` value, optional |
| `preferred_age_range` | `0-2 years`, `2-8 years`, `8+ years` or `any`, optional |
| `open_to_proactive_suggestions` | checkbox, **defaults to false** |

**302** to `/my/matches` on success · **400** on a missing required field or
an inconsistent combination, with the form re-rendered and every error listed.

Appends `AdopterProfileUpdated` and marks the profile complete.

> A staff member cannot open or submit this form — **403**, because
> `@require_adopter()` is on both methods (FR-2.5).

The column widths above are enforced in validation rather than left to the
database: API tests run on SQLite, which accepts any length, while SQL Server
2014 truncates.

---

## 5. Applications

### `POST /my/apply/<animal_id>` — **Adopter**

| Field | Rules |
|---|---|
| `applicant_message` | string ≤ 2000, optional |

**302** to `/my/applications` on success · **302** back to the animal with a
flash message when a rule refuses it (animal not AVAILABLE, a duplicate
active application, an incomplete profile) · **404** unknown animal.

Appends `ApplicationSubmitted` and enqueues a `RANK_APPLICANT` analysis job
in the same transaction.

> The command returns an identifier only. The next screen is served by a
> separate query — a command never returns read data (blueprint §9.2).

### `GET /my/applications` — **Adopter, own**
The signed-in adopter's applications only. The listing takes no identifier,
so fetching somebody else's is not expressible. Each row shows the message the
adopter wrote, its status, the submitted date and a link to its history.

### `POST /my/applications/<application_id>/withdraw` — **Owner**
Appends `ApplicationWithdrawn`. **403** if the application belongs to someone
else — and equally for an application that does not exist, so identifiers
cannot be enumerated by comparing the two answers. A final status flashes a
message and redirects.

### `POST /applications/<application_id>/decide` — **Staff**

The route the whole event-sourced design exists to serve.

| Field | Rules |
|---|---|
| `decision` | required, one of `APPROVE`, `REJECT`, `REVIEW`, `REVERSE` (case-insensitive) |
| `animal_id` | optional; where to return to afterwards |
| `note` | optional free text, recorded with a rejection or a reversal |

**302** back to that animal's applicant ranking, or to `/dashboard` when no
`animal_id` was supplied · **400** for a `decision` outside the four ·
**404** unknown application. A rule refusal — approving twice, reversing
something never approved, an illegal transition — flashes the rule's own
message and redirects, changing nothing.

| Decision | Command | Effect |
|---|---|---|
| `APPROVE` | `ApproveApplicationCommand` | Appends `ApplicationApproved`; closes every **other active application by the same adopter** with `ApplicationClosedDueToOtherApproval(caused_by=…)`; moves the animal to ADOPTION_IN_PROGRESS; notifies the adopter. Returns how many were closed, which the flash message reports. |
| `REJECT` | `RejectApplicationCommand` | Appends `ApplicationRejected` with the optional note. The animal stays available; rival applications are untouched. |
| `REVIEW` | `MarkApplicationUnderReviewCommand` | Appends `ApplicationUnderReview`. Review is a recorded state, not an informal one. |
| `REVERSE` | `ReverseApprovalCommand` | Appends `ApplicationReopened` for exactly those applications the reversed approval closed, derived from the closing events; returns the animal to AVAILABLE. Refused unless the application is currently APPROVED. |

A human presses this button. Nothing the agent produces reaches this path
(rule R4, spec §6.4).

---

## 6. Invitations

### `POST /animals/<animal_id>/invite` — **Staff**

| Field | Rules |
|---|---|
| `adopter_profile_id` | required; the adopter must pass every spec §10 eligibility rule |
| `staff_message` | optional, ≤ 1000 characters |

**302** back to the discovery screen · **400** with no
`adopter_profile_id` · **404** unknown animal. Eligibility failures — not
opted in, incomplete profile, inactive account, animal unavailable, an open
invitation already outstanding, already an applicant — flash the rule's
message and redirect.

Eligibility is re-checked in the command rather than trusted from the page
that offered the button, because the roster can change between rendering and
clicking. Sets `expires_at = sent_at + INVITATION_EXPIRY_HOURS` (default 72,
spec §7.4) and writes a notification.

> `staff_message` is currently a hidden field with a fixed sentence
> ("We thought <name> might be a good fit for your home"). The route accepts
> any value; there is no visible control for staff to write their own.

### `GET /my/invitations` — **Adopter, own**
Opening the page appends `InvitationViewed` for anything still SENT, then
re-queries so the page reflects what it just wrote. The command is idempotent,
so a refresh does not append duplicates.

### `POST /my/invitations/<invitation_id>/respond` — **Owner**

| Field | Rules |
|---|---|
| `response` | `ACCEPT` accepts; **any other value declines** |

**302** to `/my/applications` on acceptance, to `/my/invitations` on a
decline · **403** for another adopter's invitation, and for one that does not
exist. An expired or already-answered invitation flashes its reason.

Accepting creates an application; it does **not** approve an adoption
(spec §7.4), and it is refused if the animal is no longer AVAILABLE.

### The expiry sweep

There is no route for this. `ExpireOverdueInvitationsCommand` is dispatched
from a `before_request` hook, at most once every five minutes and never on a
static-file request. Expiry is recorded as a real `InvitationExpired` event
rather than inferred at read time, so a history shows it. A sweep that fails
is logged and swallowed: housekeeping should not fail the page a visitor
asked for.

---

## 7. Matching and the agent's output

### `GET /my/matches` — **Adopter**
Find My Pet. Deterministic ranking is computed synchronously and appears
immediately; explanations appear as the agent completes them, and the page
polls `/api/analysis-status` to notice. An adopter with no profile is
redirected to the profile form.

### `GET /search/describe` — **Public**
The describe-what-you-want form. Optional `?q=` prefills the textarea. Open to
signed-out visitors: describing what you want is a browsing feature, not a
personal one.

### `POST /search/describe` — **Public**

| Field | Rules |
|---|---|
| `description` | required, ≤ 500 characters |
| `use_profile` | checkbox; honoured only for a signed-in adopter with a **complete** profile |

**302** to `/search/describe/<analysis_job_id>` · **400** with the form
re-rendered and an explanation when the description is blank or over the cap —
and in that case **nothing is enqueued**, so an empty textarea costs no
inference.

The request does no inference and touches no model. It validates, enqueues an
`INTERPRET_INTENT` job and redirects (NFR-3.1). The 500-character cap is
enforced on the server rather than trusted from the textarea's `maxlength`,
because this text becomes a model prompt in another process where its length
is directly a cost, and the route is open to anonymous visitors.

`use_profile` is spec §6.4's fusion: when it is ticked the enqueued job
records the adopter's `adopter_profile_id`, and the result page scores the
animals the intent narrowed against their stored profile
(`FindMyPetWithIntentQuery`) instead of simply listing them. It is opt-in even
for a signed-in adopter, and ignored for one whose profile is incomplete —
scoring against half a household would rank animals by guesswork and present
it as personalisation. The agent itself ignores the profile id entirely; it is
the web tier's marker.

### `GET /search/describe/<analysis_job_id>` — **Public, or Owner when bound**

Three states, decided by the job rather than by the request:

| Job state | Response |
|---|---|
| PENDING or IN_PROGRESS | **200** waiting page with the shared pending block and a `<meta http-equiv="refresh" content="4">`, so it comes back by itself with no JavaScript |
| FAILED | **200** page explaining that the interpreter could not be reached, offering the structured filters instead |
| COMPLETED | **200** results — the filtered search, or the profile-fused ranking when the job carries a profile |

**403** when the job is bound to a different adopter's profile · **404** for
an unknown id, and for a job of any other type.

Who may read it is decided in the query handler: a job bound to a profile is
readable by that adopter or by staff; a job bound to nobody is readable by
anyone holding its identifier, which is what makes the redirect work for a
signed-out visitor.

### `GET /animals/<animal_id>/adopters` — **Staff**
Find My Adopter: ranks the people who already applied, using the
**Animal→Adopter** weights. Each row carries the score, its criterion
breakdown, and the decision buttons the application's current status permits.
**404** unknown animal.

### `GET /animals/<animal_id>/discover` — **Staff**
Find More Adopters: ranks eligible opted-in adopters who did **not** apply.
Candidates failing an eligibility rule are listed separately with the reason
they were excluded, rather than being silently dropped. **404** unknown
animal.

### `GET /analyses/<match_analysis_id>` — **Staff**
One stored analysis in full: score, per-criterion breakdown, reasons,
concerns, missing information, the reasoning trace, and the evidence sources
with cited ones marked. **404** unknown analysis.

### `GET /api/analysis-status` — **Signed in**, JSON

The only JSON endpoint. Exists so a page can poll without re-rendering itself.

| Query parameter | Who | Meaning |
|---|---|---|
| `scope=my-matches` | Adopter | The signed-in adopter's own analyses, using their profile identifier **from the session** |
| `animal_id=<id>` | Staff | One animal's analyses |

```json
{"pending": 2, "completed": 11, "failed": 0,
 "generation": 1758700000, "oldest_pending_at": "2026-09-24T09:15:03"}
```

**200** with that object · **400** with neither parameter · **401** for an
anonymous request, because JSON has no page to redirect to · **403** for an
adopter asking about an animal.

The adopter scope takes no identifier, so one adopter cannot ask about
another: there is no parameter that would let them. The response names no
record, so polling cannot be used to enumerate anything. `generation` is the
newest analysis timestamp in whole seconds, so a caller can compare two
answers with `!=` without parsing dates.

---

## 8. Personal area, dashboard and history

### `GET /my/notifications` — **Signed in, own**
The internal inbox (spec §23). Open to staff as well as adopters: both
receive messages, and an inbox one of them cannot read is worse than no
inbox. The listing is scoped to the signed-in account.

### `POST /my/notifications/<notification_id>/read` — **Owner**

| Field | Rules |
|---|---|
| `target_url` | optional; followed only when it is a relative path that does not start with `//` |

**302** to the target, or to the inbox · **403** for somebody else's message ·
**404** unknown message.

### `POST /my/notifications/read-all` — **Signed in, own**
Marks every unread message read and reports how many. Scoped to the
signed-in account.

The unread count is injected into every template by a context processor, so
the badge cannot silently render as zero on a page whose view forgot to pass
it. An anonymous visitor and a database error both answer 0.

### `GET /dashboard` — **Staff**
Blueprint 4.4 statistics, the Needs Attention section (spec §22), the agent
queue strip and the recent-activity feed read from the event log. **403** for
an adopter, on the server, whatever the navigation offered.

### `GET /history/<aggregate_type>/<aggregate_id>` — **Signed in**

Event timeline for one aggregate, rendered straight from `domain_events`.
Satisfies blueprint §10's requirement to display action history.

**No longer staff-only.** Spec §7.5 is written from the adopter's point of
view — an application closed because another was approved, and reopened when
that approval was reversed — and an adopter who could not see that happen
would have to take it on trust.

| Viewer | May read |
|---|---|
| Staff | Any aggregate |
| Adopter | Their own applications and invitations |
| Adopter | **403** for anybody else's, for an animal's, for a profile's, and for an aggregate that does not exist |
| Anonymous | **302 → `/login`** |

An unknown `aggregate_type` is **404**. The visibility rule lives in
`GetAggregateHistoryQuery`, not in the controller, so a second caller cannot
bypass it by forgetting to repeat it.

---

## 9. Conventions

- **Commands POST, queries GET.** A GET never changes state, so it is safe to
  retry, bookmark or prefetch. The one deliberate exception is
  `GET /my/invitations`, which marks unseen invitations viewed — an idempotent
  write whose whole purpose is that opening the page is the event.
- **Validation happens twice.** The browser check is a convenience; the
  server check is the real one. Every business form has an API test that
  posts what the browser would have blocked and asserts the server refuses it
  and writes nothing.
- **Rejected forms answer 400, not 200.** A re-rendered form under 200 tells
  a client the submission succeeded.
- **Errors are specific.** "Animal is no longer available" rather than "Bad
  request".
- **No LLM call sits in a request path** (NFR-3.1). Anything needing the
  agent is enqueued and polled — and no module under `app/` can even import
  the model client, which a test asserts structurally.
