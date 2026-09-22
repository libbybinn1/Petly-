# API — PetMatch

**Endpoints, inputs, outputs and authorization.**
Sources: course blueprint §12, §13; PetMatch spec §20, §21.

PetMatch is server-rendered, so most endpoints return HTML rather than JSON.
They are still an API surface with a contract, and every one is authorization-
tested. Status codes matter as much as bodies: blueprint §12 requires the
server to enforce permissions, so a wrong-role request must return 403 rather
than a redirect or an empty page.

---

## 1. Authorization model

| Marker | Meaning |
|---|---|
| **Public** | No authentication required |
| **Adopter** | Signed in with role ADOPTER |
| **Staff** | Signed in with role STAFF |
| **Owner** | Signed in *and* the record belongs to this user |

Enforcement is by the `@require_staff()` and `@require_adopter()` decorators
at the controller boundary, plus an ownership check inside the query handler
for anything marked **Owner**. Changing an identifier in a URL therefore does
not expose another person's data — the route check and the data check are
separate.

Standard failure responses:

| Code | When |
|---|---|
| 400 | Validation failed |
| 401 | Not signed in |
| 403 | Signed in with the wrong role, or not the owner |
| 404 | Record does not exist |
| 409 | Conflicts with existing state, e.g. duplicate application |

---

## 2. Authentication

### `GET /register` — Public
Registration form.

### `POST /register` — Public

| Field | Type | Rules |
|---|---|---|
| `full_name` | string | required, non-blank |
| `email` | string | required, valid form, unique |
| `password` | string | required, ≥ 8 characters |
| `confirm_password` | string | required, must equal `password` |

**200** form redisplayed with all validation errors at once · **302** to `/`
on success, signed in · **400** validation failed · **409** email already
registered.

The account is always created with role ADOPTER. Staff accounts are not
self-service.

### `GET /login` — Public
Sign-in form.

### `POST /login` — Public

| Field | Type |
|---|---|
| `email` | string, required |
| `password` | string, required |

**302** to `next` (only when it is a relative path) or `/` · **401** bad
credentials · **403** account deactivated.

> The 401 message is identical for an unknown email and a wrong password, so
> the endpoint cannot be used to discover which addresses are registered.

### `POST /logout` — Adopter or Staff
**302** to `/login`.

> POST only. A `GET /logout` would let any third-party page sign the user out
> by embedding an image or link.

---

## 3. Animals

### `GET /` — Public
Landing page with six available animals and a live count.

### `GET /animals/` — Public
Search (blueprint 4.1).

| Query parameter | Type | Default |
|---|---|---|
| `q` | string | — |
| `species` | enum | any |
| `size` | enum | any |
| `activity_level` | enum | any |
| `city` | string | any |
| `good_with_children` | checkbox | off |
| `good_with_other_animals` | checkbox | off |
| `include_unavailable` | checkbox | off |
| `page` | integer ≥ 1 | 1 |

Returns a page of 12 results with the total count. Invalid `page` values fall
back to 1 rather than erroring. Filters combine with AND and are preserved
across pagination.

### `GET /animals/<animal_id>` — Public
Details (blueprint 4.2). **404** if the animal does not exist. Staff
additionally see the active applicant count.

### `GET /animals/manage` — **Staff**
Tabular management (blueprint 4.3). Optional `status` and `species` filters.
**401** anonymous · **403** adopter.

### `POST /animals` — **Staff** *(planned)*
Create an animal. Requires at least one image (spec §24).

### `POST /animals/<animal_id>/status` — **Staff** *(planned)*

| Field | Rules |
|---|---|
| `status` | one of the AnimalStatus values, transition must be legal |

Appends `AnimalStatusChanged`. **400** on an illegal transition.

---

## 4. Adopter profile *(planned)*

### `GET /my/profile` — **Owner**
### `POST /my/profile` — **Owner**

| Field | Rules |
|---|---|
| `home_type` | enum, required |
| `has_yard` | boolean |
| `yard_size_sqm` | integer ≥ 0, optional |
| `household_has_children` | boolean |
| `youngest_child_age` | 0–18, required when children present |
| `has_other_animals` | boolean |
| `experience_level` | enum, required |
| `activity_level` | enum, required |
| `daily_hours_available` | 0–24, required |
| `city` | string, required |
| `preferred_species` | multi-select, optional |
| `open_to_proactive_suggestions` | boolean, **defaults to false** |

Appends `AdopterProfileUpdated` and marks the profile complete. **400** on a
missing required field or an inconsistent combination, such as a child age
given with no children present.

> A staff member cannot edit an adopter's profile — **403** even for STAFF.

---

## 5. Applications *(planned)*

### `POST /animals/<animal_id>/apply` — **Adopter**

| Field | Rules |
|---|---|
| `applicant_message` | string ≤ 2000, optional |

**302** to `/my/applications` · **400** profile incomplete · **409** animal
not AVAILABLE, or a duplicate active application exists.

Appends `ApplicationSubmitted` and enqueues a `RANK_APPLICANT` analysis job.

> The command returns an identifier only. The next screen is served by a
> separate query — a command never returns read data (blueprint §9.2).

### `GET /my/applications` — **Owner**
The signed-in adopter's applications only.

### `POST /applications/<application_id>/withdraw` — **Owner**
Appends `ApplicationWithdrawn`. **403** if the application belongs to someone
else. **409** if it is already final.

### `POST /applications/<application_id>/decide` — **Staff**

| Field | Rules |
|---|---|
| `decision` | `APPROVE` or `REJECT` |
| `note` | string, optional |

On approval: appends `ApplicationApproved`, closes the adopter's other active
applications with `ApplicationClosedDueToOtherApproval(caused_by=...)`, and
moves the animal to ADOPTION_IN_PROGRESS (spec §7.5).

---

## 6. Invitations *(planned)*

### `POST /animals/<animal_id>/invite` — **Staff**

| Field | Rules |
|---|---|
| `adopter_profile_id` | required, must be eligible |
| `staff_message` | string ≤ 1000, optional |

**409** if the adopter has not opted in to proactive suggestions, or the
animal is unavailable. Sets `expires_at = now + 72 hours`.

### `GET /my/invitations` — **Owner**
Viewing one appends `InvitationViewed`.

### `POST /invitations/<invitation_id>/respond` — **Owner**

| Field | Rules |
|---|---|
| `response` | `ACCEPT` or `DECLINE` |

**409** if the invitation has expired. Accepting creates an application; it
does **not** approve an adoption (spec §7.4).

---

## 7. Matching *(planned)*

### `GET /my/matches` — **Adopter**
Find My Pet. Deterministic ranking returns immediately; explanations appear
as the agent completes them.

### `POST /my/matches/search` — **Adopter**

| Field | Rules |
|---|---|
| `query` | string ≤ 1000 |
| `use_profile` | boolean |

Enqueues `INTERPRET_INTENT`. `use_profile` controls whether stable profile
data is combined with the current intent (spec §6.4).

### `GET /animals/<animal_id>/adopters` — **Staff**
Find My Adopter: ranks people who already applied.

### `GET /animals/<animal_id>/discover` — **Staff**
Find More Adopters: ranks eligible opted-in adopters who did **not** apply.
Only adopters passing every eligibility rule in spec §10 appear.

### `GET /analyses/<match_analysis_id>` — **Staff**
Full analysis: score, per-criterion breakdown, reasons, concerns, missing
information and cited sources.

---

## 8. Dashboard and history *(planned)*

### `GET /dashboard` — **Staff**
Blueprint 4.4 statistics plus the Needs Attention section (spec §22).

### `GET /history/<aggregate_type>/<aggregate_id>` — **Staff**
Event timeline for one aggregate, read from `domain_events`. Satisfies
blueprint §10's requirement to display action history.

---

## 9. Conventions

- **Commands POST, queries GET.** A GET never changes state, so it is safe to
  retry, bookmark or prefetch.
- **Validation happens twice.** The browser check is a convenience; the
  server check is the real one, and every form has a test proving the server
  rejects what the browser would have blocked.
- **Errors are specific.** "Animal is no longer available" rather than
  "Bad request".
- **No LLM call sits in a request path** (NFR-3.1). Anything needing the
  agent is enqueued and polled.
