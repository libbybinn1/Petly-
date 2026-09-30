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
| Fit grade | `.fit-card` — `matches/_fit_grade.html`, `matches/_fit_deduction.html` | Find My Pet, applicant ranking, analysis |
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
  and whether waiting would help. `matches/_pending_analysis.html` is now
  included by **four** screens — Find My Pet, the applicant ranking,
  discovery and the describe result — and each passes a `queued_at_label`,
  so the block says how long this particular job has been waiting. A wait with
  a number on it is the difference between slow and stuck.

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
re-learns a colour: strong, fair, weak. **No template contains a threshold.**
`MatchScore` and `CriterionScore` expose a `band` property that reads the
domain's own constants, and the templates switch on the name — so the
interface and the scorer cannot come to disagree about what a good match is,
which is exactly what happened when five templates each held their own copy of
the numbers. A disqualified pairing shows no ring at all: it failed a rule
rather than scoring badly.

### The fit grade

A number says how good a match is. It does not say why the match is not 100.
The fit grade is built by `build_fit_report` in `app/domain/fit_grade.py`. It
turns the deterministic score into a letter (A+ to F), a verdict ("Strong
fit") and a list of where the points went. Each line is a criterion, the
whole points it cost, and the scorer's own explanation for that criterion.

- **Exact, not illustrative.** A criterion scoring `s` at weight `w` costs
  `(100 − s) × w` points, and the weights sum to one. Rounded by the
  largest-remainder method, the lines add up to exactly `100 − score`. The
  test `test_deductions_add_up_to_exactly_what_the_score_is_missing` checks
  this against the real scorer.
- **The letter cannot contradict the ring.** B starts at the STRONG
  threshold and D at the recommendation threshold, so every A or B is
  green, every C or D amber, and only an F is weak.
- **One meter, 100 points wide.** The kept points come first, then each
  deduction as its own segment, largest first. Each line's badge uses its
  segment's colour. A ranking card lists the three largest deductions and
  folds the rest into a `<details>`, so no JavaScript is needed. The
  analysis page lists them all.
- **A disqualified pairing gets no grade.** It failed a rule rather than
  scoring badly, and an F would suggest otherwise.
- **Not model output.** The agent is given the grade and its deductions in
  its prompt (`_fit_grade_section` in `agent_service/explanation.py`) so
  that its concerns explain the real losses. It never computes or changes
  them.

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
the reported `generation` changes, **stops entirely once nothing is pending**,
pauses while the tab is hidden, and stops dead on any non-200. A page left open
overnight must not keep a request every few seconds against a throttled
free-tier database.

**The describe result page uses none of that.** While its interpretation job
is still running it carries a `<meta http-equiv="refresh" content="4">` and
reloads itself — chosen over the poller because that page can be reached by a
signed-out visitor, has nothing on it but the one job, and should come back by
itself even with scripting blocked entirely.

## 4. Screens and permissions

Permissions are enforced on the **server**, not by hiding links. Blueprint
§12 is explicit that hiding buttons is not sufficient, so every row below was
measured by requesting the route in all three states rather than read off the
decorators.

**An anonymous visitor is redirected, not refused.** `@require_sign_in` wraps
`@require_staff()` / `@require_adopter()`, and it is the outer decorator, so a
signed-out request never reaches the role check: it gets `302 → /login` and,
after signing in, `?next=` returns it to where it was going. A 401 page would
be a dead end for somebody who simply has not signed in yet. Getting the role
wrong *is* a refusal: a signed-in account of the wrong kind gets **403**, on a
direct POST as much as on a page load.

| Screen | Route | Anonymous | Adopter | Staff |
|---|---|---|---|---|
| Home | `/` | yes | yes | yes |
| Register | `/register` | yes | yes | yes |
| Sign in | `/login` | yes | yes | yes |
| Browse animals | `/animals/` | yes | yes | yes |
| Animal details | `/animals/<id>` | yes | yes | yes + applicant count |
| Describe what you want | `/search/describe` | yes | yes | yes |
| Describe result | `/search/describe/<job_id>` | unbound: yes | unbound: yes · own if bound | yes, either |
| Animal management | `/animals/manage` | → sign in | **403** | yes |
| List a new animal | `/animals/new` | → sign in | **403** | yes |
| Edit an animal | `/animals/<id>/edit` | → sign in | **403** | yes |
| Change availability (POST) | `/animals/<id>/status` | → sign in | **403** | yes |
| Adopter profile | `/my/profile` | → sign in | own only | **403** |
| My applications | `/my/applications` | → sign in | own only | **403** |
| My invitations | `/my/invitations` | → sign in | own only | **403** |
| Notification inbox | `/my/notifications` | → sign in | own only | own only |
| Find My Pet | `/my/matches` | → sign in | yes | **403** |
| Find My Adopter | `/animals/<id>/adopters` | → sign in | **403** | yes |
| Find More Adopters | `/animals/<id>/discover` | → sign in | **403** | yes |
| Send an invitation (POST) | `/animals/<id>/invite` | → sign in | **403** | yes |
| Decide an application (POST) | `/applications/<id>/decide` | → sign in | **403** | yes |
| Full match analysis | `/analyses/<id>` | → sign in | **403** | yes |
| Event history | `/history/<type>/<id>` | → sign in | own records only | yes |
| Analysis status (JSON) | `/api/analysis-status` | → sign in | `?scope=my-matches` | either scope |
| Staff dashboard | `/dashboard` | → sign in | **403** | yes |

Four rows are worth spelling out, because the rule is not "staff see more":

- **The inbox is not adopter-only.** Staff receive messages too, and an inbox
  one of the two audiences cannot read is worse than no inbox.
- **History is not staff-only.** Spec §7.5 is written from the adopter's point
  of view — an application closed because another was approved, and reopened
  when that approval was reversed. Which records count as theirs is decided in
  the query handler, so an adopter who changes an id in the address bar gets
  403 rather than somebody else's timeline.
- **`/api/analysis-status` takes its identity from the session.** The
  adopter scope has no parameter that would let one adopter ask about another;
  the `animal_id` scope is staff-only and answers with counts, never names or
  scores.
- **A described search is public until it is personal.** `POST /search/describe`
  is open to anyone, signed in or not, and the result page is readable by
  whoever holds the job's identifier — that is what makes the redirect work
  for a signed-out visitor. But a search where the adopter ticked *use my
  profile* is **bound to that profile**: another adopter asking for it gets
  403, and only its owner or staff may open it.

An adopter can only ever see **their own** profile, applications, invitations
and inbox. Ownership is checked in the query handler, not only in the URL.

## 5. Navigation

The top bar adapts to role. It carries, in this order: the brand, the
navigation, the account, and the light/dark/system control.

- **Anonymous** — Browse animals · Sign in · **Join**
- **Adopter** — Browse animals · My matches · Applications · Invitations ·
  Notifications · Profile · avatar · Sign out
- **Staff** — Browse animals · Dashboard · Manage · Notifications ·
  avatar · Sign out

One `navlink()` macro renders every link, so `aria-current="page"` cannot be
forgotten on one of them, and the word "animals" in "Browse animals" is the
first thing dropped at 640px (see §7).

The avatar shows initials derived from the user's name. Sign-out is a POST
form, not a link, so a third-party page cannot sign the user out by embedding
an image.

**Notifications is in the bar for both roles**, carrying the unread count as
a `.badge`. It has to be: staff receive messages too, and spec §23 makes the
inbox the only channel there is — an invitation nobody notices is an
invitation that expires. The count comes from a context processor, so every
template can see it and no view can forget to pass it; an anonymous visitor,
and a database error, both read zero rather than failing the page.

## 6. Key screens

### Home
A two-column hero: on the left a one-line proposition, a search field that
posts straight to the browse screen, two calls to action and a live count of
available animals; on the right three fanned photographs of real animals.
Below it, six featured animals in a three-column grid. Anonymous visitors also
get a "Create a profile" call to action. Below 860px the fan collapses to a
single photograph behind the copy, with a near-opaque scrim so the text keeps
its contrast whatever the photograph happens to be.

### Browse animals (blueprint 4.1, 4.2)
Free-text search over name, breed and description, plus filters for species,
size, **age band**, energy level and the two compatibility flags. The search
button is the only primary action on the screen; the filter row's "Apply
filters" is deliberately secondary, because the two do almost the same thing
and only one of them should look like the way forward.

Every applied filter appears as a **removable chip** above the results, each
one a link to the same search minus that one parameter, followed by "Clear
all". Without them the screen said "32 animals match your search" and offered
no evidence of what had been searched for. The count sentence knows the
difference: with no filters at all it reads "40 animals looking for a home".

Twenty-four results a page. Every card links through to the details view. The
empty state explains how to widen the search rather than just saying "no
results", and offers the describe-it-instead route as a second way in.

### Animal details (blueprint 4.2)
Two columns on desktop, stacked below 860px.

The left column is the photograph **and the six-field specification grid
underneath it**. That is not decoration: with the photograph alone, the column
ended about 300px above the one beside it, and the page finished with a band
of empty cream.

The right column is the status tags, the name, the species line, the
description, and then one panel per decision:

- **Living with `<name>`** makes the two compatibility facts explicit. Neither
  is painted as a fault any more. "Good with children" and "Good with other
  animals" are successes; the negatives are a caution ("Better suited to a home
  without young children") and a plain statement ("Happiest as the only pet in
  the home"), because an animal who wants a quiet house is not defective.
- **Special needs** appear as a warning callout with an alert icon, inside that
  panel rather than buried in body text.
- **Apply to adopt** — for a signed-in adopter, a message field with a live
  character counter against its 2000-character cap. An anonymous visitor gets
  the same panel explaining that applications are reviewed by a person, and a
  way to sign in.
- **Staff actions** and **Availability** are two panels, not one. Changing
  availability is what removes an animal from search and from matching, so it
  gets its own heading, a sentence saying exactly that, and a confirmation
  before it submits.

A back link with a chevron returns to the browse screen.

### Animal management (blueprint 4.3)
The tabular screen: thumbnail, name with breed underneath, species, age, size,
temperament, location and a colour-coded status tag per row. Filters for
status, species and age band, plus free text over name, breed and city — with
the same chip row and paginator as the browse screen, because a staff member
and an adopter should not have to learn two filter bars.

Forty rows a page, scrolling under a sticky header inside `.table-wrap--tall`.
Below 760px each row becomes a card (`.table--cards`). Every row offers the
same four actions in the same order and the same two weights: **Open** and
**Edit** change or open the record and are secondary; **Rank** and **History**
only look at it and are ghosts. They sit in a fixed two-by-two block, because
four buttons of four different widths wrapped three-and-one and made every row
look like a different set of actions.

### Animal create and edit (spec §20)
One form for both, at `/animals/new` and `/animals/<id>/edit`, grouped into
five sections: who they are, what they need, living with others, photographs,
description. Photographs are one input per address rather than one textarea of
lines — the textarea needed JavaScript to copy its contents into hidden fields
at submit time, which meant listing an animal did not work at all with
JavaScript blocked.

### Adopter profile (spec §5.1)
A 680px form in four sections. Three fields are revealed by the answer above
them (`data-shown-by`): yard size by "I have a secure garden", the youngest
child's age by "children live with me", the description of other pets by "I
already have pets". Numbers get a 160px control, a city 340px, and the labels
and hints keep the form's own measure. The proactive-suggestions opt-in is set
apart in its own callout, because it is the one field that changes who may
contact the adopter.

### My invitations
Open invitations first, under a heading with a count badge; answered and
expired ones below it. Each open invitation carries its own deadline as a
countdown tag rather than one sentence at the top of the page that applies to
all of them and to none in particular. Declining is a `.btn--danger-ghost`
with a confirmation, because it cannot be undone.

### Describe what you want (spec §6.3, §6.4)
A textarea with a 500-character cap, an example of the kind of sentence that
works, and — for a signed-in adopter with a complete profile — a *use my
profile* checkbox. Submitting redirects to the job's own page, which shows the
shared pending block until the agent answers and then shows results: plain
cards for an intent-only search, cards carrying a score when the profile was
fused in. The interpretation is printed above the results in the adopter's
terms, so a reading that missed the point is visible rather than mysterious.

### Event history (blueprint §10)
A vertical timeline, oldest first, one entry per recorded event: what happened,
who did it, and when, in words and in a machine-readable `<time>`. The back
button is role-aware — staff return to the roster, an adopter to their own
applications — because the same screen serves both and sending an adopter to a
staff page they cannot open would be worse than no button.

### Notification inbox (spec §23)
Messages newest first, grouped into the last 24 hours and earlier, each with an
icon for its kind, its type, its age in words, and a small ghost button to open
it or mark it read. An unread row carries a left border and a heavier title as
well as a tint, and says "(unread)" in text for anyone who gets none of those.

### Staff dashboard (blueprint 4.4)
Six stat tiles, a **Needs Attention** section that links each figure to the
action it implies (spec §22), an agent-queue strip, and a recent-activity feed
read from the event log.

**A tile is a link only when there is somewhere to go.** A tile with an
`action_url` renders as `.stat--link` and carries its own action label —
"Manage available animals", "See adoptions in progress" — and opens the roster
already filtered to that status. Two tiles are deliberately **not** links:
*invitations awaiting a reply*, because there is no staff-side invitation
list, and *match analyses completed*, because analyses are reached through the
animal they belong to rather than from an index. A tile that looks clickable
and goes nowhere useful is worse than one that does not, so the difference is
visible.

The **agent-queue strip** is the only place in the interface where the second
process appears as a process: outstanding work, failed work, and how long the
oldest queued job has waited. It is what makes a stuck queue distinguishable
from a busy one.

Every figure and every activity entry reads as prose a person wrote — "1
application needs attention", never "1 application(s)" — and every timestamp
is a `<time datetime=…>` element carrying both a readable phrase and a
machine-readable value.

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

The per-screen pass cleared the findings on the screens it covered: axe
reports **nothing at any impact** on home, browse (including a filtered and an
empty result set), sign in, register, describe, the 404 page, animal details in
all three roles, animal management, the animal create and edit form, the
adopter profile with and without validation errors, the invitations screen and
the notification inbox — in both colour schemes, measured on 2026-09-24.

Two findings remain, both in screens outside that pass:

| Rule | Where | Fix |
|---|---|---|
| `heading-order` | `matches/find_my_pet.html` | The screen jumps `h1` → `h3` |
| `empty-table-header` | `personal/applications.html` | The thumbnail and action columns need `.sr-only` header text |

## 9. Forms (spec §21)

| Form | Route | Validated |
|---|---|---|
| Registration | `POST /register` | client + server |
| Sign in | `POST /login` | client + server |
| Adopter profile | `POST /my/profile` | client + server |
| Animal create | `POST /animals/new` | client + server |
| Animal edit | `POST /animals/<id>/edit` | client + server |
| Animal availability | `POST /animals/<id>/status` | server |
| Adoption application | `POST /my/apply/<animal_id>` | client + server |
| Withdraw an application | `POST /my/applications/<id>/withdraw` | server |
| Invitation response | `POST /my/invitations/<id>/respond` | server |
| Send an invitation | `POST /animals/<id>/invite` | server |
| Staff decision | `POST /applications/<id>/decide` | server |
| Mark a message read | `POST /my/notifications/<id>/read`, `/read-all` | server |
| Search and filters | `GET /animals/`, `GET /animals/manage` | — (GET, no token) |

Client-side validation is a convenience. Server-side validation is the real
check, and every form has an API test proving the server rejects what the
browser would have blocked. Every POST form carries a CSRF token; a unit test
parses all of them, because a token inserted *inside* an opening `<form>` tag
once swallowed the form's own `action` (`tests/unit/test_feature_csrf_tokens.py`).

### What a field looks like

- **A label is a `<label for=...>`.** Where the label would be noise — the
  browse screen's search box — it is `.sr-only` rather than absent, because an
  `aria-label` alone leaves the control unlabelled for voice control and for
  translation tools.
- **A group of checkboxes is a `<fieldset>` with a `<legend class="field__label">`.**
  A `<span>` above three boxes looks identical and groups nothing: the legend
  is the only association a screen reader announces.
- **Every hint and every error has an `id`, and its control points at it**
  with `aria-describedby`; a control with an error also carries
  `aria-invalid="true"`. Both are built conditionally, because pointing
  `aria-describedby` at an element that is not on the page is itself a failure.
- **A hint belongs to the control above it**, not to the next one down.

### Required, and how it is marked

A form that mixes required and optional fields marks each required one with
`<span class="required-mark">*</span>` and explains the asterisk once, at the
top: "\* Required. Everything else is optional." That is the profile form and
the animal form.

A form where *everything* is required says so once — "Both fields are
required" — and uses no asterisks at all. That is sign-in and registration.
An asterisk on every field marks nothing.

### When the server says no

The screen does not say "please fix 3 problems below". It shows an
`.error-summary` with `role="alert"`: one `<li>` per message, each a link to
the control that produced it, worded the way that control is worded. The field
repeats its own message underneath itself, and takes a danger-coloured border.
A rejected submission renders with what was typed, never with the saved values.

### Fields are as wide as their answers

`.form-narrow` caps a long form at 680px, `.field--medium` a city at 340px and
`.field--short` a number at 160px. Where the label or the hint is a sentence,
`.field--wide-text` releases them and caps only the control — a four-line hint
inside a 160px column is not a narrow field, it is a broken one.

### Progressive disclosure

Three of the profile's fields only matter if the answer above them was yes, so
they are revealed by it: `data-shown-by="has_yard"`,
`"household_has_children"`, `"has_other_animals"`, and on the animal form
`"has_special_needs"`. With JavaScript blocked every one of them is simply
visible, which is what these forms did before — never content a visitor cannot
reach.

### Everything else JavaScript adds here

A capped textarea reports what is left (`data-counter`), a password field can
be revealed (`data-toggle-password`), and a confirmation field reports a
mismatch through the browser's own validation (`data-match`). All three are
`hidden` in the markup and unhidden by `petmatch.js`, so a visitor with
JavaScript blocked never meets a control that does nothing. None of them is
the check: the server validates every one of these fields again.
