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
set on one arm or leg and then the other; the target is per side. Tagged on 204 files, by
explicit id tokens (`one_arm`, `single_leg`, `split_squat`, `step_up`, `pistol`,
`side_plank`, …) or instructions that say so ("on one side before switching", "then
switch"), except where the instructions alternate sides (walking lunges, bicycle crunches,
alternating curls, Cossack squats; a catalog test keeps it so). Search documents don't use it,
so the shipped index stays current. The plan view and the active workout block say "per
side"; `exercise_facts` carry it.

**Session time** (`services.session_time`, pure). Work per set: reps × 3.5 s, the
prescribed seconds, or meters at 0.4 s/m (cardio, speed, pace) or 1.0 s/m (carries).
Unilateral: both sides plus a 10 s switch. Single blocks: the rest *between* sets. Supersets:
each round is work plus transitions, and the round rest falls between rounds. Setup by
equipment: 120 s for a barbell, rack or sled, 45 s for other equipment, 15 s with none.
Warm-up and cool-down count like any block. Tolerance: 15% (`TimeModel.tolerance`). The bot
and the web show each workout's estimate rounded to five minutes, as "About N min
(estimated from the sets and rests, not measured)". Nothing measures real duration.

**Time budget** (`services.time_budget`, generation only). After the guards pass, a
generated workout over `session_minutes × 1.15` is shortened in place, in this order:

1. an accessory or core block that trains none of the workout's stated objectives is removed;
2. one set comes off each remaining trimmable block (never below two);
3. the lowest-priority blocks are removed.

Never touched: the warm-up, any block that directly trains the main objective (without
stated objectives, power and strength blocks), the last working block, and any rest. A
secondary objective nothing trains any more is dropped from `Workout.qualities`. The change
is logged as `plan.time_budget` (ok, informational). Revisions, imports and the user's own
edits are what the user asked for, so the review only reports their length (`quality.duration`).

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
  `set_logs` through `recent_completed_set_logs`, which takes completed app sessions only
  (imported history is never evidence). A session counts only if it was done at the
  *current* target, so the evidence restarts after every change:
  - two sessions in a row with every prescribed set performed at the top → one step up:
    the rep range +1 or +2, or about 10% more seconds or meters (at least 5);
  - two in a row short of the target → one step down;
  - otherwise → hold. One session is not a trend;
  - speed work keeps its volume;
  - at 20 reps, a 120 s hold, a 300 s interval, a 100 m carry or an 800 m run, the next step
    is a harder variation, not more of the same.
- The recap offers the step as a suggestion, ahead of the model's: `change_reps` or the new
  `change_target` (`seconds` or `meters`). It applies only when the exercise appears once in
  today's workout outside a superset and every prescription of it in the plan has the same
  sets and target (a different number of sets is a different prescription, and restarts the
  evidence too). A target change is the app's alone: the model's output schema has no
  `change_target` (`ModelPlanChange`), and its rep or set change for the same exercise is
  dropped (one progression at a time). **Apply** is judged as before and saved as an
  `origin=progression` version; an app-decided rep or target change is stale once any
  prescription of the exercise no longer has the sets and target it was decided from. A session with a skipped or unlogged set is
  inconclusive: it neither earns a step up nor counts towards a step down. `recap.v3` gets
  `next_targets` and is told never to claim the user got faster.

## Consequences

- Durations are labelled estimates with a documented 15% tolerance, and the constants are
  coarse. Real workout duration isn't measured, because `started_at`/`finished_at` include
  pauses.
- The workload units are a transparent weighting, not a fatigue model. They don't know the
  actual load or days of the user's sport sessions, only how many there are per week.
  Session spacing is judged on scheduled weekdays only.
- Reps in reserve can't inform progression until something logs it. RPE stays unwritten.
- Kettlebell swings and throws no longer get automatic load increases. A heavier bell is
  the user's choice (an edit, judged by the same guards).
- The trimmed draft can still carry findings (a removed block can leave a secondary quality
  uncovered); they go to the repair round as before.
- `plan_generate.v7`, `recap.v3`, the brief's new fields and the catalog's `unilateral`
  change `llm_input` and `content_version`.
- After merging: nothing to migrate and no re-index needed.
