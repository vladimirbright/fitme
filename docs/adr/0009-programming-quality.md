# ADR 0009 — Programming quality: measures, qualities, exclusions and a plan review

Status: accepted (2026-10-16). Changes A§4.2 (`set_logs`, `profiles`, `exercise_exclusions`),
A§4.4 (catalog fields), A§4.5 (`Prescription`, `Block`, `Workout`), A§5.1 (setup steps),
A§6.4 (plan generation), A§7 (`plan.excluded`), A§8.1 (prompt versions).

## Context

A three-day "strength and conditioning for boxing" plan came out valid for the guards but
poorly programmed: an "explosive" day with no power work, a "foot speed" day of presses and
pulldowns, planks and battle ropes as "3 × 1" with the duration in a note, generic
"start with 2 kg" calibration hints, squats although the user had said no squats, a lot of
overlapping shoulder work on top of boxing, and arbitrary supersets with a long rest
between their exercises.

Root causes, in the code:

- **Retrieval.** `plan_candidates` ignored the whole request as soon as it contained a
  negation ("…после разминки, **а не** суперсетами"), so the model got a 40-exercise pool
  without shadow boxing, jump rope or throws. Nothing described what an exercise *trains*.
- **Schema.** A prescription had only reps; three prompts told the model to put seconds into
  the reps fields.
- **Constraints.** No exclusion was stored anywhere; "no squats" lived only in chat text.
- **Validation.** Nothing checked session goals against exercises, order, supersets or
  weekly load. The calibration hint was the catalog's `start` weight.

## Decision

**Catalog (A§4.4).** Every exercise has `measures` (`reps`, `time`, `distance`; the first is
the default) and `qualities` — what it *directly* trains: `strength`, `hypertrophy`,
`power`, `rotational_power`, `core_stability`, `agility`, `muscular_endurance`,
`conditioning`, `mobility`. Tagged once in a model-assisted pass over all 1,098 files,
validated and spot-checked; `fitme catalog check` requires `qualities`. Search documents are
unchanged, so the shipped index stays current.

**Prescriptions (A§4.5).** A `Prescription` has one `measure` with exactly its target:
`reps_min`/`reps_max`, `seconds` or `meters`; plus an optional `rir` (reps in reserve).
Bodies stored before this parse as reps. `set_logs` gains planned/actual seconds and meters
(migration 0010). "Done as planned", parsed results and the assistant log the set's own
measure. The load engine never progresses a timed or distance exercise on reps: it holds.
`planning.judge` moves a prescription in a measure its exercise doesn't accept to the
exercise's default with a neutral target (`services/measures`, logged as
`plan.measure_fit` + `measure.substituted`), like a load substitution. The `/train` review
doesn't, so plans saved before still train as saved. A calibration load on a kg reps
exercise gets `rir = 3` unless the model set one; the display says "pick a weight that
leaves about N reps in reserve" instead of a start weight.

**Supersets.** Done in rounds in the listed order. Each exercise's `rest_seconds` is the
transition to the next one; the last exercise's is the rest after the round. The plan view
says so ("Superset, in this order, 3 rounds; rest 90 s after each round").

**Exclusions (A§4.2, A§7).** `exercise_exclusions` (migration 0011) holds the user's hard
exclusions: a movement `pattern`, one `exercise`, or a `name` word for a family the patterns
don't separate ("lunge", "выпад"). `services/exclusions.resolve` gives the excluded ids;
`available_exercises` removes them from everything a model or tool is offered, and
`guards.plan.validate_plan` fails `plan.excluded` for any plan that names one (generation,
revision, import, assistant and web edits, confirm). `validate_workout` doesn't, so a plan
saved before an exclusion still trains. The assistant stages `add_exclusion` /
`remove_exclusion` (applied before a redesign or new plan in the same turn) and reports
what an exclusion covers; `/profile` and the web account page list them with a remove
button. An exclusion is a preference, never a screening fact.

**Other sport (A§5.1).** `profiles.sport` and `sport_sessions_per_week`, two optional setup
steps after session length. They reach the planner in `UserContext` and lower the review's
weekly limits.

**Retrieval (ADR 0007 amended).** A negation drops only its own clause
(`positive_request`). `requested_qualities` reads the qualities a request names (small
en/ru word lists), plus those implied by the other sport, the focus and the preferences.
`plan_candidates` gives each requested quality up to four exercises that directly train it,
on top of the pattern quotas. `exercise_facts` carry `qualities` and `measures`;
`plan_generate` gets `requested_qualities`.

**Session goals.** `Workout.qualities` (optional, up to four) is what the planner says a
session is for; the plan view shows it.

**Plan review (`services/plan_quality`).** Deterministic findings, not guards:
`goal_coverage` (a stated quality no exercise directly trains), `request_coverage` (a
requested quality nothing in the plan trains), `declared` (the request names goals but a
workout states none), `warm_up` (heavy lifting with no warm-up block first), `order` (power
or speed work after heavy strength work), `superset` (two heavy lifts, mismatched sets, a
long transition), `shoulder_volume` / `lower_volume` (weekly sets above 36 / 24, lowered per
session of a shoulder- or leg-heavy sport, never below 12) and `consecutive_heavy`. "Heavy"
is a loaded compound strength lift at six reps or fewer. The limits are named constants in
`WorkloadLimits`.

**Targeted repair.** In `propose_new_plan`, a plan that passes the guards but has findings
gets the existing retry as a targeted repair (the findings as `guard_feedback`). The passing
attempt with the fewest findings is the draft, so a repair that fails the guards never loses
the valid plan; remaining findings are logged in `guards_fired`, never a refusal. A revision
logs its findings without a retry (the user asked for that change; the guards decide).

**Prompts.** `plan_generate.v5` (programming from qualities, session order, measures,
`rir`, supersets, fatigue, exclusions), `plan_revise.v3`; `plan_import.v2`,
`plan_revise.v2`, `session_adjust.v2`, `result_parse.v2` (measures); `assistant.v7`
(exclusions, measures, the open draft).

**Evaluation.** `tests/unit/test_plan_quality.py` pins the three scenarios (boxing support,
hypertrophy, hotel) as fixed plans through the real catalog, guards and review; the same
scenarios are `fitme llm eval` fixtures, which now also report a `findings` outcome.

## Consequences

- Exclusions and measures are enforced in code; the review only shapes the model's second
  attempt and the log. A plan with findings can still be shown.
- What the review can't judge is left out on purpose: exercise selection beyond direct
  qualities, intensity distribution across the week, periodization, and the actual load of
  the user's sport sessions. The limits are conservative, coarse numbers.
- Correcting a logged timed or distance set through the assistant isn't supported yet (it is
  refused with a reason); entering such results during a workout is.
- The catalog has few dedicated footwork drills (shadow boxing, skater hops, quick feet, jump
  rope); adding ladder or shuffle drills needs `make catalog-index` for their embeddings.
