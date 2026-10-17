# ADR 0016 — Session time, weighted weekly workload, objective priority and progression

Status: accepted (2026-10-17). Amends ADR 0009 and 0011 (review, brief, prompts), ADR 0015
(recap suggestions) and A§7.3 (load engine). Changes A§4.4 (`unilateral`), A§6.4
(generation), A§7.3, A§8.1 (`plan_generate.v7`, `recap.v3`).

## Context

After ADR 0011 a generated two-day boxing-support program had reasonable exercises but no
visible management of the week or of time. Tuesday had a throw, kettlebell swings, bench
press, Romanian deadlifts, rows and cable rotations; Friday, "foot speed + endurance", had
footwork, line jumps, shuttles, deadlifts, a shoulder press, pulldowns and battle ropes.
Sessions of 9–10 exercises came with no sign they fit 60 minutes. Nothing said how a
prescription moves after the user does it, beyond the kg double progression.

Root causes, in the code:

- **Duration.** `plan_quality.estimated_minutes` counted `reps_max × 4 s`, a minute per
  block and the rest after *every* set, including the last. It ignored unilateral exercises
  (the catalog had no such field), equipment setup and superset rests. It fed one warning
  at 1.25 × the session length, and that warning only reached the model's repair round. The
  model was doing the arithmetic, and nobody checked it.
- **Workload.** The weekly review counted raw sets. A heavy deadlift set, a 30 s rope
  interval and a footwork drill weighed the same. "Lower body" meant squat/hinge *patterns*
  only, and the catalog files kettlebell swings as `conditioning`, so swings + Romanian
  deadlifts + deadlifts never registered as one hip-hinge demand.
- **Objectives.** `Workout.qualities[0]` was checked for coverage, but nothing noticed a
  session whose exercises mostly served none of its stated objectives (Friday's three
  strength lifts in an agility/conditioning session).
- **Progression.** The engine progressed every kg-loadable exercise in reps by topping the
  range — a medicine-ball throw or a swing the same as a bench press. Bodyweight reps, timed
  and distance work had no deterministic progression at all, only whatever the recap model
  suggested from one session. RPE is a column that no path ever writes, so reps in reserve
  can't be read back.

What already existed and is kept: double progression with the weekly cap, the ceiling and
the check-in gate; two sessions below the range → −10%; history keyed by catalog id (a
barbell bench press never sets a dumbbell bench press's load); progression applied as
`origin=progression` plan versions of the same plan (a plan, its versions and the session
log are separate, and progression never regenerates the plan).

## Decision

**Catalog.** `Exercise.unilateral` (default false): done one side at a time, every rep of a
set on one arm or leg and then the other; the target is per side. Tagged on 199 files, by
explicit id tokens (`one_arm`, `single_leg`, `split_squat`, `step_up`, `pistol`,
`side_plank`, …) or instructions that say so ("on one side before switching", "then
switch"), except where the instructions alternate sides (walking lunges, bicycle crunches,
alternating curls, Cossack squats, stretches that switch sides every rep; a catalog test
keeps it so). Search documents don't use it,
so the shipped index stays current. The plan view and the active workout block say "per
side"; `exercise_facts` carry it.

**Session time** (`services.session_time`, pure). Work per set: reps × 3.5 s, the
prescribed seconds, or meters at 0.4 s/m (cardio, speed, pace) or 1.0 s/m (carries).
Unilateral: both sides plus a 10 s switch. Single blocks: the rest *between* sets. Supersets:
each round is work plus transitions, and the round rest falls between rounds. Setup by
equipment: 120 s for a barbell, rack or sled, 45 s for other equipment, 15 s with none.
Warm-up and cool-down count like any block. `TimeModel.tolerance` (15%) is the estimate's
uncertainty, never extra training time: the review warns (`quality.duration`) only above
`session_minutes × 1.15`, while the time budget aims at `session_minutes` itself. Duration is
a planning estimate only; nothing tracks, stores or calibrates against how long a workout
actually took. The bot
and the web show each workout's estimate rounded to five minutes, as "About N min
(estimated from the sets and rests, not measured)". Nothing measures real duration.

**Time budget** (`services.time_budget`, generation only). After the guards pass, a
generated workout estimated over `session_minutes` is shortened in place toward it. Each
block is ranked by its relevance to the workout's objectives (`plan_quality.relevance`:
primary, secondary, supporting, none — the definition the review's `off_objective` and
coverage checks use), then by role (accessory, core, cool-down, conditioning, power,
strength), then later first. The estimate is recomputed after every step:

1. accessory or core blocks that serve none of the objectives are removed, lowest first;
2. then one set at a time comes off the lowest-ranked block that still has more than two
   (among equals, the one with the most sets), so 6 sets become 5, 4, 3 before anything
   else goes;
3. only when every trimmable block is at two sets is the lowest-ranked one removed.

Never touched: the warm-up, any block that directly trains the main objective (without
stated objectives, power and strength blocks), the last working block, and any rest. The
loop ends because every step removes a set or a block. A secondary objective nothing trains
any more is dropped from `Workout.qualities`. The result is logged as `plan.time_budget`:
`ok` when the target is reached, `ok=False` with "the rest is the warm-up and the main
objective's work, which are kept" when it can't be reached without them. The plan as
shortened is validated again (`guards.plan.validate_plan`) before the review; its fresh
verdicts are the attempt's, and a shortened plan that failed them would be replaced by the
plan that passed, with a note (never accepted). Revisions, imports and the user's own edits
are what the user asked for, so the review only reports their length.

**Weekly workload** (`services.workload`, pure, units per set):

| what | units |
|------|-------|
| heavy compound (≤ 6 reps) | 1.0 |
| other compound | 0.75 |
| isolation | 0.5 |
| trunk | 0.3 |
| loaded speed work (throws, swings) | 0.6 |
| plyometrics (jumps, hops, sprints) | 0.5 |
| technical drills | 0.25 |
| conditioning | 0.4/min of work, kept between 0.15 and 0.8 |
| warm-up and mobility | 0 |

There are three regions:

- **hip hinge**: the hinge pattern, plus kg-loadable conditioning or accessory work that loads the hips and lower back, which is how swings are caught;
- **knee-dominant**: squats and every plyometric;
- **shoulders**: pressing, pulling, accessories and upper-body conditioning such as battle ropes.

The review's weekly checks now run on units: `shoulder_volume` (22, minus 2 per boxing,
swimming, climbing or racket session), `lower_volume` (hinge 9, knee 12, minus 1.5 per
running, cycling or team-sport session, floor 8), and `consecutive_heavy` (≥ 3 hinge or knee
units on back-to-back days). There is one new check, `hinge_overlap`: three or more distinct
loaded hinges in a week. All remain warnings for the repair round. They are never errors
and never refusals.

**Objective priority.** The new `quality.off_objective` finding fires when two or more
working exercises train none of a workout's stated qualities (directly or via `SUPPORTS`).
The brief adds `session_objectives`, the main objectives spread over the week's sessions in
turn, and `time_budget`. `plan_generate.v7` asks the model to let the main objective take
the first working blocks and most of the time, to keep secondary objectives to one or two
blocks, to plan blocks from rests and sets rather than a count, never to shorten rest to fit
more in, and to treat swings, RDLs, deadlifts and hip thrusts as one hinge demand.

**Progression.**

- *Speed work* (`IntensityMode.SPEED`) holds its load (`LoadKind.HOLD_SPEED`, recap
  "unchanged: fast work progresses by speed and clean technique"). Topping its reps never
  raises the load, and short sets never lower it. Calibration still comes first, and the
  ceiling still clamps.
- *Bodyweight reps, timed and distance work* (`services.progression`, deterministic) read
  `set_logs` through `recent_completed_set_logs`, which takes completed app sessions **of
  the current plan** only (imported history and other programs are never evidence). A
  session counts only if it was done at the *current* number of sets and target, and — for
  a kg-loadable exercise such as a carry — at the same logged resistance as the newer one
  (12 kg then 24 kg per hand over 40 m is a load progression, not two sessions at 40 m; a
  session without a logged load, or with mixed loads, is no evidence). The evidence
  restarts after every change:
  - two sessions in a row with every prescribed set performed at the top → one step up:
    the rep range +1 or +2, or about 10% more seconds or meters (at least 5);
  - two in a row short of the target → one step down;
  - otherwise → hold. One session is not a trend;
  - speed work keeps its volume;
  - at 20 reps, a 120 s hold, a 300 s interval, a 100 m carry or an 800 m run, the next step
    is a harder variation, not more of the same.
- *kg lifts* (`services.loads.scope_history`): a session outcome now carries the plan, the
  rep range and the number of sets it was done at. Where a prescription's load is decided
  for a plan — at review and Start, and in the recap preview — only sessions of that same
  prescription in that plan count as a success or a failure. Others keep their loads (the
  start of a new program, and the ceiling) but earn no increase and trigger no decrease;
  a rep target above the one the last load was used for (a 6-rep load for 10) is
  calibration. Without a prescription (substituting a model's violating load), as before.
- The recap offers the step as a suggestion, ahead of the model's: `change_reps` or the new
  `change_target` (`seconds` or `meters`). It applies only when the exercise appears once in
  today's workout outside a superset and every prescription of it in the plan has the same
  sets and target (a different number of sets is a different prescription, and restarts the
  evidence too). A target change is the app's alone: the model's output schema has no
  `change_target` (`ModelPlanChange`), and its rep or set change for the same exercise is
  dropped (one progression at a time), as is any rep or set change for speed work, which
  keeps its volume. **Apply** is judged as before and saved as an
  `origin=progression` version; an app-decided rep or target change is stale once any
  prescription of the exercise no longer has the sets and target it was decided from. A session with a skipped or unlogged set is
  inconclusive: it neither earns a step up nor counts towards a step down. `recap.v3` gets
  `next_targets` and is told never to claim the user got faster.

## Consequences

- Durations are labelled planning estimates with a documented 15% uncertainty, and the
  constants are coarse. Actual workout duration is out of scope by design.
- The workload units are a transparent weighting, not a fatigue model. They don't know the
  actual load or days of the user's sport sessions, only how many there are per week.
  Session spacing is judged on scheduled weekdays only.
- Reps in reserve can't inform progression: `set_logs.rpe` exists but no path writes it
  (button, assistant and import all store `NULL`), and nothing infers it. Progression stays
  conservative accordingly: an increase needs every set at the top of the range, is one
  increment, and passes the weekly cap, the ceiling and the check-ins; a target change needs
  two sessions. A logging step for effort is out of scope here.
- Progression history is plan-scoped by `plan_versions.plan_id`, with no migration. A
  user who rebuilds the same program as a new plan starts its streak again (holding at the
  known loads).
- Kettlebell swings and throws no longer get automatic load increases. A heavier bell is
  the user's choice (an edit, judged by the same guards).
- The trimmed draft can still carry findings (a removed block can leave a secondary quality
  uncovered); they go to the repair round as before.
- `plan_generate.v7`, `recap.v3`, the brief's new fields and the catalog's `unilateral`
  change `llm_input` and `content_version`.
- After merging: nothing to migrate and no re-index needed.
