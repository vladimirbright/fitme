# ADR 0012 — Sets done beyond the prescription

Status: accepted (2026-10-16). Amends A§4.3 (`set_logs`) and ADR 0005.

## Context

`set_logs` held one row per **prescribed** set, and `services.training.assign_rows` matched
rows back onto the workout by position: each item took the next `sets` rows of its exercise.
The assistant's `log_exercise_results` therefore had to name exactly `set_index 1..sets`.
When the user said "сделал три подхода" of a two-set shadow boxing, the model had no way to
write the third set, fell back to `log_exercise_as_planned` and replied that it logged the
exercise as planned (two sets). The log was wrong, and so was the reply.

## Decision

- A set done beyond the prescription is a `set_logs` row of its own (migration 0013):
  `extra_for_block` / `extra_for_item` name the block and item it belongs to, the planned
  reps/time/distance columns are NULL, `planned_load_kg` is the item's prescribed load (the
  reference a later correction's plausibility check uses, like a prescribed row's), and the
  actual result is written when the row is created. Prescribed
  rows keep both columns NULL and keep being matched by position, so an extra row never
  shifts that matching; `assign_rows` appends an item's extra rows after its prescribed ones.
- `log_exercise_results` takes the prescribed sets (each exactly once, as before), then
  sets numbered on from `sets + 1`: at most 5 extra (`domain.results.MAX_EXTRA_SETS`; a
  set index goes up to 15, as a prescription has at most 10 sets), none of them skipped. They pass the
  same plausibility checks as the prescribed sets.
- `log_exercise_as_planned` is not for a report that differs from the plan, including the
  number of sets (prompt `assistant.v9`).
- Progression (A§7.3) judges prescribed sets only: the outcome selectors skip extra rows, so
  an extra set neither makes nor breaks a success. Extra sets count everywhere else a
  performed set does: volume, the recap, the historical max, the website.
- "Planned sets" counts (session list, completion) count prescribed rows only.

## Consequences

- What the user did is what is stored; the reply lists every set written, extra ones too.
- The ✅ button and the ✏️ Correct/Fix flow still log exactly the prescribed sets; extra sets
  go through the assistant.
- A post-session correction (`fix_logged_sets`) can change an extra set's reps and kg like
  any other reps set, checked against the same prescribed load; a timed or distance extra
  set is refused like a prescribed one. Its `set_number` counts across the session, so it
  is no longer bounded by one prescription's sets.
