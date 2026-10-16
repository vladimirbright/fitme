# ADR 0010 — Exclusions apply to one plan unless the user says "every plan"

Status: accepted (2026-10-16). Amends ADR 0009 (exclusions), A§4.2 (`exercise_exclusions`),
A§6.4, A§8.1 (`assistant.v8`).

## Context

In a planning session the owner asked to take squats out of the plan being worked on. The
assistant stored `pattern:squat` as an exclusion for every plan: 92 exercises (every
squat, but also every lunge, step-up and leg press), listed under /profile and applied to
every future plan. ADR 0009 had one scope only, and the tool documentation itself suggested
the pattern for "no squats".

## Decision

**Scope (migration 0012).** `exercise_exclusions.scope`:

- `all` — every plan; only when the user says so explicitly ("я никогда не хочу делать
  приседания", "исключи работу с гирей из всех тренировок") or on /profile. Rows stored
  before 0012 become `all` (that is what they meant).
- `plan` — one plan (`plan_id`, deleted with the plan): applied whenever that plan is
  drafted, revised, edited (bot, assistant, web) or confirmed.
- `new_plan` — the new plan being drafted. `confirm_plan` turns these rows into `plan` rows
  of the plan it creates; a new-plan planning session that closes unsaved (discarded, idle,
  replaced) deletes them.

`planning.read_snapshot(plan_id=, new_plan=)` keeps only the rows that apply
(`exclusions.applicable`): every `all` row plus the plan's own, or the new draft's; without
either, the default plan's (training and recaps). `validate_workout` still ignores
exclusions, so a saved plan trains as saved.

**Assistant.** `add_exclusion(kind, value, for_all_plans=false)`: by default it applies to the
plan this turn works on (a `new_plan`/`rewrite_plan`/`edit_plan` staged this turn, else the
open planning session's plan or new-plan draft); with no such plan and no
`for_all_plans` it is refused, so it is never silently global. The answer and the notice say
where it applies ("Excluded from this plan only" / "from every plan"). `state.exclusions`
carry `scope` and `plan_id`. `assistant.v8` and the tool description ask for the narrowest
kind matching the user's words — "squats" is `name:squat` (62 exercises), a `pattern` only
when the user names the whole movement category.

/profile and the web account page list and remove the `all` rows only.

## Consequences

- A plan-scoped exclusion doesn't change the plan's current content by itself: the plan's
  next draft must leave those exercises out (`plan.excluded`), so the assistant also edits or
  redesigns the plan.
- Plan-scoped exclusions are visible and removable through the assistant only.
- A new-plan draft confirmed from its own button after its session went idle loses its
  exclusions (the session's close deleted them); the saved plan itself has none of the
  excluded exercises.
