# ADR 0015 — The recap reads the workout on the large tier and can change set counts

Status: accepted (2026-10-16). Amends A§6.5 step 6 and the A§8.1 tier table; builds on ADR
0012 and 0014.

## Context

The end-of-workout recap ran on the small tier and could suggest only a swap or a rep
range. The owner wants each workout's results analysed for the next sessions, with the
plan following what was actually done: three sets of a two-set exercise should lead to a
three-set prescription, not only to a heavier one.

## Decision

- The `recap` agent's default tier is **large** (`FITME_LLM_AGENT_RECAP` still overrides),
  with a larger output budget and timeout. It still runs once per finished workout,
  without escalation; a provider failure still shows the deterministic recap alone.
- New `PlanChange`: **`change_sets`** (`exercise_id`, `sets` 1..10). Like the others it
  carries no load. At recap time it is dropped unless the exercise is in the plan, the
  count actually changes, it follows today's results (more sets only up to the most done of
  that exercise in one prescription today and at most 2 above the plan; at most 2 fewer)
  and the changed prescription passes `guards.plan.prescription_verdicts`; on **Apply** the whole plan is judged by
  `planning.judge` as before and saved as an `origin=progression` version.
- The recap input gains `extra_sets` per exercise (sets done beyond the prescription).
  Prompt `recap.v2`: doing more than asked (extra sets, reps above the range on a non-kg
  exercise) is a basis for `change_sets` / a higher rep range; doing clearly less, for
  fewer; no basis, no suggestion; never a load.
- The deterministic recap says why a load goes up: "every set reached the top of the
  range" or, for ADR 0014's increase, "you did more than the plan asked"
  (`NextKind.INCREASE_DID_MORE`).

## Consequences

- One large-tier call per finished workout (about a few cents); latency of the recap grows.
- Loads stay the engine's alone: a suggestion never carries a kg, and nothing changes in
  the plan until the user taps Apply.
