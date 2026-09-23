# UX — PetMatch

**Screens, navigation, permissions and the visual design system.**
Sources: course blueprint §13; PetMatch spec §20, §21.

---

## 1. Design principles

This is a product about finding homes for animals. The interface should feel
warm and calm, not corporate. Five rules follow from that:

1. **Warm neutrals, not cold greys.** Backgrounds are cream and off-white;
   the brand colour is a muted terracotta. Nothing on screen is pure `#fff`
   on pure `#000`.
2. **The animal is the content.** Photographs lead every card, every details
   page and the hero. Spec §24 makes images mandatory, and the layout treats
   them as the primary information, not decoration.
3. **Plain language.** "Good with kids", not "child_compatibility: true".
   Enum values are humanised before they reach the screen.
4. **Explain, never just score.** A match shows its reasons and its concerns.
   A number on its own is not an answer.
5. **Colour is never the only signal.** Every tinted thing also carries a
   word, an icon, a border or a weight, because a colour is unavailable to
   part of the audience and meaningless to all of it on first sight.

Two consequences worth stating, because they are what the rest of this
document enforces:

- **Every colour is a token on `:root`.** No component declares a literal
  colour. That is what makes the dark theme an override rather than a second
  stylesheet, and what makes a contrast fix one edit rather than fifty.
- **No build step.** The design system is one stylesheet, one Jinja partial
  per component, one SVG sprite and one 150-line vanilla JavaScript file.
  There is no Node, no bundler and no external front-end dependency, so the
  interface cannot rot when a toolchain does.

## 2. Design tokens

All defined in `app/static/css/petmatch.css` on `:root`.

| Token group | Purpose |
|---|---|
| `--brand-50…700` | Terracotta ramp. **600 is the action colour**; 500 is the identity colour. |
| `--accent-100/300/500/700` | Muted teal, used sparingly for contrast against the warmth. |
| `--on-brand` | Foreground for anything painted brand or accent. White in light, `#2a1d17` in dark. |
| `--brand-tag-fg` | Brand text on a brand tint, which inverts between themes. |
| `--surface-page / card / sunken / hover` | Layering |
| `--text-strong / normal / muted / inverse` | Type hierarchy |
| `--border-soft / normal / input` | Card edges, dividers, and a real boundary around a control |
| `--success / warning / danger / info` | Status pairs, each a background and a foreground |
| `--focus-ring` | The 3px ring. Aliased to `--brand-600`, so it follows the theme. |
| `--radius-sm/md/lg/pill` | 8 / 12 / 18 / 999px |
| `--shadow-sm/md/lg` | Warm-tinted, never neutral black |
| `--space-1…8` | 4 / 8 / 12 / 16 / 24 / 32 / 48 / 64px |
| `--btn-pad-*`, `--input-pad-*`, `--tag-pad-*`, `--tap-target` | Control metrics, so density is one edit |
| `--weight-medium/semibold/bold` | 500 / 600 / 700 — weights every font in the stack ships |
| `--hero-gradient`, `--hero-scrim`, `--scroll-shadow`, `--skeleton-highlight` | Theme-dependent effects |

### Contrast

Every foreground/background pair in the table above meets WCAG AA (4.5:1 for
text, 3:1 for a control boundary) **in both themes**, measured in the browser
rather than assumed. Three of these values exist because the first audit
measured the obvious choice and found it failing:

| Was | Measured | Now |
|---|---|---|
| white on `--brand-500` | 3.74:1 light, 2.80:1 dark | `.btn--primary` uses `--brand-600` with `--on-brand` |
| `--text-muted: #857772` | 4.07:1 on the page, 3.77:1 on a sunken surface | `#6e615c` — 5.3:1 and 4.9:1 |
| `--border-normal` as an input boundary | 1.51:1 | `--border-input: #8c7d74` — 3.96:1 |

`tests/e2e/test_accessibility.py` runs axe-core over the anonymous screens in
both colour schemes, so a token edited back to an unreadable value fails the
suite instead of waiting for the next audit.

### How dark mode is declared

Dark mode needs two selectors: `@media (prefers-color-scheme: dark)
:root:not([data-theme="light"])` for people who have not chosen, and
`:root[data-theme="dark"]` for people who have. CSS cannot share one
declaration block between a media query and a plain selector.

Writing the palette out twice is what caused the previous bug: the two copies
drifted, the `[data-theme]` copy lost every shadow, and three component fixes
lived only inside the media query so they never applied to a manually chosen
dark theme. So the palette is now declared **once**, as `--dark-*` custom
properties on `:root`, and the two selectors contain nothing but a
byte-identical mapping (`--surface-page: var(--dark-surface-page);` …) with no
literal colours in either. They cannot drift.

A token defined as an alias needs no entry at all: `--focus-ring:
var(--brand-600)` and `--score-hole: var(--surface-card)` are re-resolved
against whichever palette is active.

The brand and accent ramps **invert** in dark mode: the higher numbers become
the lighter, more legible end. That is deliberate, and it is what lets
`.navlink--active`, `.tag--brand` and `.source-kind` be written once with no
media query and read correctly in both themes.

### Choosing a theme

The top bar carries a three-state control: light, dark, or follow the system.
The choice is stored in `localStorage` and applied by a small inline script in
`<head>` **before the first paint**, because reading it after load shows one
frame of the wrong theme on every navigation. Every storage access is wrapped
in `try`/`catch`: it throws outright in a private window, and a theme
preference must never be able to break a page.

## 3. Components

| Component | Class / partial | Used on |
|---|---|---|
| Animal card | `.animal-card` — `_animal_card.html` | Home, search, describe |
| Score ring | `.score` — `matches/_score_ring.html` | Every ranking and analysis screen |
| Criterion breakdown | `.criterion` — `matches/_criteria_bars.html` | Analysis, ranking |
| Pending analysis | `.pending-block` — `matches/_pending_analysis.html` | Any screen waiting on the agent |
| Skeleton | `.skeleton` — `_skeleton.html` | Inside the pending block |
| Icon | `.icon` — `_icons.html` | Everywhere |
| Filter bar | `.filter-bar` | Search, management |
| Filter chip | `.chip`, `.chip-row` | Search, management |
| Data table | `.table`, `.table--cards` | Staff management, applications |
| Status tag | `.tag`, `.tag--success/warning/danger/info/brand` | Everywhere |
| Stat tile | `.stat`, `.stat--link` | Dashboard |
| Empty state | `.empty` | Any zero-result view |
| Alert | `.alert--success/error/info/warning` | Flash messages |
| Notification row | `.notification-item` | Inbox |
| Timeline | `.timeline` | History |
| Badge | `.badge` | Unread counts |
| Auth card | `.auth-card` | Sign in, register |
| Theme toggle | `.theme-toggle` | Top bar |

### Single definitions

The point of a design system is that a thing is defined once.

- **The animal card** is one partial, included by every grid. One definition
  means the home page, search results and describe results cannot drift.
  Its name is a `<p class="animal-card__name">`, not a heading: the card sits
  under an `<h1>` on the browse page and under an `<h2>` on the home page, so
  any fixed level skips one somewhere. The animal is named by the card's own
  link text either way.
- **The score** is one renderer. There used to be three — a flat badge, a
  bespoke inline number on the analysis page, and the ranking cards' own
  markup. `matches/_score_ring.html` is the only one now, and
  `_score_badge.html` is a one-line alias so the screens that already include
  it need no edit.
- **The criterion breakdown** is one renderer. The analysis page used to
  duplicate the markup inline and, in copying it, dropped the word "weight" —
  leaving a bare "18%" beside a score of 64 with nothing to say which was
  which.
- **The pending state** is one partial and one sentence. There used to be
  three different sentences for it across two screens, none of which
  answered what a visitor wanted to know: whether the score was trustworthy,
  and whether waiting would help.

### The score ring

A conic gradient, a hole punched by `::before`, and an animation over
`--ring-fill` — a custom property registered with `@property` so the browser
has a type to interpolate. The partial sets only `--ring-target`, from the
score. The keyframes have **no `to`**, so the implicit one uses the element's
own value: if the animation never runs — reduced motion, or an engine without
`@property` — the ring renders complete rather than empty.

It carries `role="img"` and an explicit label (`"87 out of 100 match"`),
because "87" and "match" read out as two unrelated strings are not a
sentence. Colour bands are the same three the flat badge used, so nobody
re-learns a colour: green ≥ 75, amber ≥ 50, muted below. A disqualified
pairing shows no ring at all — it failed a rule rather than scoring badly.

### Icons, not emoji

`_icons.html` is one sprite of SVG `<symbol>`s, included once by
`base.html`; everything else references a symbol by id. An emoji is drawn by
the platform font, so the same "clipboard" was blue on Windows and grey
elsewhere, none of them matched the terracotta palette, and a screen reader
announced the Unicode name of a decoration. An `.icon` inherits the current
colour and scales with the current font size, so it needs no per-use styling.

### Progressive enhancement

`app/static/js/petmatch.js` is the only JavaScript, and every screen works
with it blocked. It adds four behaviours, each opted into from a template by
one attribute:

| Attribute | Behaviour | Without JavaScript |
|---|---|---|
| `[data-theme-choice]` | Light / dark / system buttons, with `aria-pressed` | The theme follows the operating system |
| `form[data-confirm]` | Confirm before a destructive submit | The form submits; the server is still the authority |
| `[data-shown-by="<id>"]` | Reveal a region when that checkbox is ticked | The region is simply always visible |
| `[data-analysis-status-url]` | Poll for finished explanations and reload | The pending block's Refresh link |

Polling asks every 8 seconds, backs off to 30 after two minutes, reloads when
the reported `generation` changes or `pending` drops to zero, and stops dead
on any non-200 — including the 404 it receives while the endpoint does not yet
exist.

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

A 16px side gutter is preserved at every width, and no page scrolls
horizontally — verified at 320px and 375px, not assumed.

| Breakpoint | Change |
|---|---|
| ≤ 900px | Featured grid drops from three columns to two |
| ≤ 860px | Details page collapses to one column; the hero fan collapses to one photograph behind the copy |
| ≤ 760px | Ranking cards to one column; a `.table--cards` table becomes one card per row |
| ≤ 640px | Card grid to one column; the brand word and the "animals" in "Browse animals" are hidden to keep the top bar one row |
| ≤ 480px | The hero search button goes full width |

`body` is a flex column with `flex: 1` on `main`, so the footer sits at the
bottom of the viewport on a short page instead of floating under the content.

### The top bar is one row at every width

It used to wrap at 375px and orphan the "Join" button on a second line. Three
things fix it, in this order: the brand collapses to its mark, "Browse
animals" shortens to "Browse", and the navigation itself scrolls sideways
rather than wrapping. Nothing is removed permanently and nothing is hidden
behind a menu that has to be discovered.

### Tables

A table scrolls horizontally inside `.table-wrap`, which carries a **scroll
shadow**: a gradient on whichever side has more table to see, so the fact that
it scrolls is visible rather than something to find out. `<thead>` is sticky,
and `.table-wrap--tall` caps the height so a forty-row roster scrolls under
its own header.

That is not enough on a phone. A nine-column roster cannot be read through a
375px window however well it scrolls, so `.table--cards` turns each row into
a card below 760px and shows each cell's column name from its own
`data-label` attribute. It is opt-in per table, because a cell with no
`data-label` would lose its header entirely.

`.table__thumb` sets **`min-width`** as well as `width`: the global
`img { max-width: 100% }` beat a plain `width: 44px` inside a squeezed column
and collapsed every thumbnail to nothing.

### Images

An animal card loads its image eagerly for the first four cards and lazily
after that, via an optional `card_position`. `loading="lazy"` on a card the
visitor is already looking at delays the image rather than saving anything.
Every media box has a fixed `aspect-ratio`, including the three hero
photographs, so nothing moves as images decode.

### Motion

`prefers-reduced-motion: reduce` removes the card lift, the image zoom, the
stat-tile lift and the skeleton shimmer, and lands the score ring on its final
value immediately. Nothing conveys information by movement alone, so removing
all of it loses nothing.

## 8. Accessibility

Every claim here is checked by `tests/e2e/test_accessibility.py`, which drives
a real browser, injects axe-core and fails on any serious or critical
violation — on the home, browse, sign-in and 404 screens, in both colour
schemes. It includes a negative test that injects a 1.2:1 element and requires
the harness to catch it, because a checker nobody has seen fail is a checker
nobody should trust.

- **Focus is always visible.** A 3px `--focus-ring` outline with a 2px offset
  on `:focus-visible`, and nothing anywhere removes it. Inputs keep the ring
  *and* their tinted glow; the glow is decoration, the ring is the accessible
  part. A test tabs through fourteen controls and asserts a measurable
  outline on each.
- **A skip link is the first tab stop** on every page, pointing at
  `<main id="main">`. `html { scroll-padding-top: 88px }` keeps the sticky top
  bar from covering whatever an in-page jump lands on.
- **Landmarks are named.** The navigation is `<nav aria-label="Main">`, and
  the current screen's link carries `aria-current="page"` as well as a tint —
  a screen reader cannot see a tint. One `navlink()` macro in `base.html`
  renders all six links, so `aria-current` cannot be forgotten on one of them.
- **Every page has exactly one `<h1>`**, including the error page, which used
  to open with an `<h3>`.
- **Status is conveyed by text as well as colour.** A red tag also reads "Not
  suited to young children"; an unread notification has a left border and a
  heavier title, not only a tint; a "Special needs" tag carries an alert icon.
- **Links inside prose are underlined**, not merely coloured. Components that
  happen to be anchors — buttons, nav links, cards, chips — opt out by name,
  so a new prose link is underlined by default rather than by remembering to
  ask. A link inside a heading drops the underline until hover: a heading is
  not a text block.
- **Colour pairs meet AA in both themes**, measured in the browser. See §2.
- **Decoration is hidden from assistive technology.** The hero photographs
  are `aria-hidden` with empty `alt` — every animal in them is named and
  linked in the grid below — and so are the skeleton bars, which are
  described by the sentence beside them.
- **Every image has an `alt` describing the animal**, not the file.
- **Form inputs have real `<label>` elements bound by `for`.** The hero search
  field's label is visually hidden rather than absent.
- **An icon is never the whole label.** Each theme-toggle button pairs its
  icon with `.sr-only` text and reports its state with `aria-pressed`.
- **44px minimum tap target** (`--tap-target`) on anything touched on a phone.

### Known remaining violations

Honesty is cheaper than a document that disagrees with the code (rule R5).
After this pass, axe reports nothing on home, browse, sign in, register,
describe, the 404 page, the adopter profile and the dashboard, in both
schemes. Four findings remain, all in per-screen markup rather than the design
system, and all queued for the per-screen pass:

| Rule | Where | Fix |
|---|---|---|
| `heading-order` | `animals/details.html`, `personal/invitations.html`, `matches/find_my_pet.html` | Those screens jump `h1` → `h3` |
| `empty-table-header` | `animals/manage.html`, `personal/applications.html` | The thumbnail and action columns need `.sr-only` header text |

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
