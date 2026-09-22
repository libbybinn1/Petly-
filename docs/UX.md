# UX — PetMatch

**Screens, navigation, permissions and the visual design system.**
Sources: course blueprint §13; PetMatch spec §20, §21.

---

## 1. Design principles

This is a product about finding homes for animals. The interface should feel
warm and calm, not corporate. Four rules follow from that:

1. **Warm neutrals, not cold greys.** Backgrounds are cream and off-white;
   the brand colour is a muted terracotta. Nothing on screen is pure `#fff`
   on pure `#000`.
2. **The animal is the content.** Photographs lead every card and every
   details page. Spec §24 makes images mandatory, and the layout treats them
   as the primary information, not decoration.
3. **Plain language.** "Good with kids", not "child_compatibility: true".
   Enum values are humanised before they reach the screen.
4. **Explain, never just score.** A match shows its reasons and its concerns.
   A number on its own is not an answer.

## 2. Design tokens

All defined in `app/static/css/petmatch.css` on `:root`, with a dark-mode
override under `@media (prefers-color-scheme: dark)`.

| Token group | Purpose |
|---|---|
| `--brand-50…700` | Terracotta ramp. 500 is the action colour. |
| `--accent-*` | Muted teal, used sparingly for contrast against the warmth. |
| `--surface-page / card / sunken / hover` | Layering |
| `--text-strong / normal / muted / inverse` | Type hierarchy |
| `--success / warning / danger / info` | Status pairs, each a background and foreground |
| `--radius-sm/md/lg/pill` | 8 / 12 / 18 / 999px |
| `--shadow-sm/md/lg` | Warm-tinted, never neutral black |
| `--space-1…8` | 4 / 8 / 12 / 16 / 24 / 32 / 48 / 64px |

Dark mode is supported and tested. Because every colour is a token, the dark
theme is a token override rather than a second stylesheet.

## 3. Components

| Component | Class | Used on |
|---|---|---|
| Animal card | `.animal-card` | Home, search, matches |
| Filter bar | `.filter-bar` | Search, management |
| Data table | `.table` | Staff management, applications |
| Status tag | `.tag`, `.tag--success/warning/danger/info` | Everywhere |
| Stat tile | `.stat` | Dashboard |
| Empty state | `.empty` | Any zero-result view |
| Alert | `.alert--success/error/info/warning` | Flash messages |
| Auth card | `.auth-card` | Sign in, register |

The animal card is a single reusable partial, `_animal_card.html`, included
by every grid. One definition means the home page, search results and match
lists cannot drift apart visually.

## 4. Screens and permissions

Permissions are enforced on the **server**, not by hiding links. Blueprint
§12 is explicit that hiding buttons is not sufficient. Every row marked Staff
below is protected by `@require_staff()`, which returns 403 to an adopter
even on a direct POST.

| Screen | Route | Anonymous | Adopter | Staff |
|---|---|---|---|---|
| Home | `/` | yes | yes | yes |
| Register | `/register` | yes | — | — |
| Sign in | `/login` | yes | — | — |
| Browse animals | `/animals/` | yes | yes | yes |
| Animal details | `/animals/<id>` | yes | yes | yes + applicant count |
| Animal management | `/animals/manage` | **403** | **403** | yes |
| Adopter profile | `/my/profile` | **401** | own only | — |
| My applications | `/my/applications` | **401** | own only | — |
| My invitations | `/my/invitations` | **401** | own only | — |
| Find My Pet | `/my/matches` | **401** | yes | — |
| Find My Adopter | `/animals/<id>/adopters` | **403** | **403** | yes |
| Find More Adopters | `/animals/<id>/discover` | **403** | **403** | yes |
| Staff dashboard | `/dashboard` | **403** | **403** | yes |

An adopter can only ever see **their own** profile, applications and
invitations. Ownership is checked in the query handler, not only in the URL,
so changing an id in the address bar does not expose another person's data.

## 5. Navigation

The top bar adapts to role:

- **Anonymous** — Browse animals · Sign in · Join
- **Adopter** — Browse · My matches · My applications · My invitations · avatar
- **Staff** — Browse · Manage · Dashboard · avatar

The avatar shows initials derived from the user's name. Sign-out is a POST
form, not a link, so a third-party page cannot sign the user out by embedding
an image.

## 6. Key screens

### Home
Hero with a one-line proposition and a live count of available animals,
followed by six featured animals. Anonymous visitors also get a "Create a
profile" call to action.

### Browse animals (blueprint 4.1, 4.2)
Free-text search over name, breed and description, plus filters for species,
size, energy level and the two compatibility flags. Twelve results a page.
Every card links through to the details view. The empty state explains how to
widen the search rather than just saying "no results".

### Animal details (blueprint 4.2)
Two-column on desktop, stacked on mobile. Large photograph, status tags, a
six-field specification grid, and a "Living with <name>" panel making
child and other-animal compatibility explicit. Special needs appear as a
warning callout rather than buried in body text.

### Animal management (blueprint 4.3)
The tabular screen. Thumbnail, name, breed, species, age, size, temperament,
location and a colour-coded status tag per row, with status and species
filters above.

### Staff dashboard (blueprint 4.4)
Stat tiles plus a **Needs Attention** section that links each figure to the
action it implies, per spec §22.

## 7. Responsive behaviour

| Breakpoint | Change |
|---|---|
| ≤ 860px | Details page collapses to one column |
| ≤ 640px | Card grid to `minmax(150px, 1fr)`; tighter nav and padding |

Tables scroll horizontally inside `.table-wrap` rather than breaking the
layout. A 16px side gutter is preserved at every width, and no page scrolls
horizontally.

## 8. Accessibility

- Every image has an `alt` describing the animal, not the file.
- Focus states are a visible 3px brand-tinted ring, never removed.
- Status is conveyed by text as well as colour — a red tag also reads
  "Not suited to young children".
- `prefers-reduced-motion` disables card lift and image zoom.
- Form inputs have real `<label>` elements bound by `for`.
- Colour pairs are chosen for contrast in both themes.

## 9. Forms (spec §21)

| Form | Screen | Validated |
|---|---|---|
| Registration | `/register` | client + server |
| Adopter profile | `/my/profile` | client + server |
| Animal create/edit | `/animals/new`, `/animals/<id>/edit` | client + server |
| Animal status | animal details, staff | server |
| Adoption application | animal details | client + server |
| Invitation response | `/my/invitations` | server |
| Staff decision | applicant ranking | server |

Client-side validation is a convenience. Server-side validation is the real
check, and every form has an API test proving the server rejects what the
browser would have blocked.
