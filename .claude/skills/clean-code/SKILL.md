---
name: clean-code
description: PetMatch code style contract. Use when writing or reviewing any Python file in this project - enforces Single Responsibility, descriptive unabbreviated names, early returns, shallow nesting, extracted helpers, strict typing and comprehensive docstrings.
---

# Clean Code — PetMatch

Every Python file written for this project must satisfy this contract before it is
considered done. `ruff` and `mypy --strict` enforce the mechanical parts; this
document covers the judgement parts they cannot check.

## 1. Single Responsibility

One module, one reason to change. One function, one job.

- A command handler validates, mutates and emits an event. It does **not** render,
  query read models, or call the agent.
- A query handler reads. It never writes, never opens a write transaction.
- A controller parses the request, checks authorization, dispatches, and renders.
  Any `if` statement expressing a *business* rule belongs in the domain layer, not here.

If a function needs a comment saying "and then also...", split it.

## 2. Names

Descriptive, unabbreviated, pronounceable. The reader should never decode.

| Write this | Not this |
|---|---|
| `adopter_profile` | `ap`, `prof`, `adptr` |
| `calculate_adopter_to_animal_score` | `calc_score`, `score2` |
| `eligible_adopters` | `lst`, `res`, `data` |
| `invitation_expires_at` | `exp`, `dt2` |
| `has_completed_profile` | `flag`, `check` |

Booleans read as assertions: `is_`, `has_`, `can_`, `should_`.
Collections are plural. Counts end in `_count`. Times end in `_at`.

Never use `data`, `info`, `item`, `temp`, `obj`, `val`, `result` as a final name.

## 3. Early Returns

Guard clauses first. Handle the exceptional case and leave. Never nest the happy
path inside an `if`.

```python
# NO - happy path buried three levels deep
def send_invitation(staff, adopter, animal):
    if staff.is_staff:
        if animal.is_available:
            if adopter.is_open_to_suggestions:
                return Invitation.create(staff, adopter, animal)
            else:
                raise NotOptedInError()
        else:
            raise AnimalUnavailableError()
    else:
        raise PermissionDeniedError()

# YES - guards leave early, happy path is flat and last
def send_invitation(staff, adopter, animal):
    if not staff.is_staff:
        raise PermissionDeniedError()
    if not animal.is_available:
        raise AnimalUnavailableError()
    if not adopter.is_open_to_suggestions:
        raise NotOptedInError()
    return Invitation.create(staff, adopter, animal)
```

**Maximum nesting depth is 3.** Ruff's `max-branches = 8` backs this up. If you
exceed it, extract a helper — do not reformat to squeeze under the limit.

## 4. Extract Complex Logic

A function longer than ~40 statements, or holding more than one level of
abstraction, gets split. Scoring is the canonical example: the top-level function
reads like the specification, and each criterion is its own tested helper.

```python
def calculate_adopter_to_animal_score(profile, animal) -> MatchScore:
    """Score how well `animal` suits `profile` (spec section 9.1)."""
    if violation := find_hard_constraint_violation(profile, animal):
        return MatchScore.disqualified(reason=violation)

    criterion_scores = [
        score_living_environment(profile, animal),
        score_daily_availability(profile, animal),
        score_experience_level(profile, animal),
        score_children_compatibility(profile, animal),
        score_other_animals_compatibility(profile, animal),
        score_size_and_space(profile, animal),
        score_species_preference(profile, animal),
    ]
    return MatchScore.weighted(criterion_scores, weights=ADOPTER_TO_ANIMAL_WEIGHTS)
```

Each `score_*` helper is pure, typed, and has its own unit test.

## 5. Strict Typing

- Every function annotates parameters **and** return type. No bare `Any`.
- Domain concepts get real types, not primitives: `AdopterId`, `MatchScore`,
  `ApplicationStatus` — not `int`, `float`, `str`.
- Use `Enum` for every fixed set of values (statuses, species, sizes).
- `mypy --strict` must pass with zero errors. Never silence with `# type: ignore`
  unless a third-party stub is genuinely missing, and then add a one-line reason.

## 6. Docstrings

Google convention. Every module, class and public function.

```python
def find_eligible_adopters(animal: Animal, limit: int = 10) -> list[AdopterProfile]:
    """Filter adopters who may be ranked for an animal.

    Applies the deterministic eligibility rules from specification section 10
    *before* the agent ranks anyone, so obviously unsuitable candidates never
    reach the LLM.

    Args:
        animal: The animal staff are seeking adopters for. Must be available.
        limit: Maximum candidates to return.

    Returns:
        Adopters with a completed profile who opted in to proactive suggestions,
        ordered by registration date. Empty if the animal is not available.

    Raises:
        AnimalNotFoundError: If the animal does not exist.
    """
```

Docstrings explain **why** and reference the spec section. Do not restate the
signature in prose.

## 7. Forbidden

- `print()` in application code — use the logger. (Ruff `T20` enforces this.)
- Naive datetimes — always timezone-aware. The 72-hour invitation expiry depends
  on this. (Ruff `DTZ` enforces this.)
- Business logic in Jinja templates. Templates display; they do not decide.
- SQLAlchemy sessions outside `repositories/`.
- Flask imports inside `domain/`. The domain must be testable with no app context.
- Catching bare `Exception` to hide a failure.
- Commented-out code. Git remembers it.

## 8. Checklist before marking a file done

- [ ] `ruff check` clean
- [ ] `mypy --strict` clean
- [ ] No function nests deeper than 3
- [ ] No abbreviated names
- [ ] Every public symbol has a docstring citing its spec section
- [ ] Guard clauses, not nested happy paths
- [ ] One responsibility per unit
