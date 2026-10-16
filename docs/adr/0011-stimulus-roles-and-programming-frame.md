# ADR 0011 — Prescriptions by stimulus, block roles and a programming frame

Status: accepted (2026-10-16). Amends ADR 0009 (calibration `rir`, review, prompts) and
ADR 0007 (candidate pool). Changes A§4.4 (`specific`), A§4.5 (`Block.role`, field order),
A§6.4 (generation), A§8.1 (`plan_generate.v6`, `plan_revise.v4`).

## Context

A two-day "boxing support" plan generated after ADR 0009 was valid and mostly sensible
exercise by exercise, but not programmed: "Farmer's walk 3 × 40 m, start with 2 kg per hand
or less", "Medicine ball chest pass 4 × 5–6, 3 RIR", a left-hook drill as the general
warm-up in both sessions, a "foot speed" session whose only speed work was quick feet, a
superset showing its 75 s round rest twice, and a week where all the pressing was on one day
and all the squatting and hinging on the other. (The user had configured no exclusions;
squats were not a constraint bug.)

Root causes, in the code:

- **Loads.** Both front-ends rendered a calibration load without `rir` as "start with
  {catalog `start`} or lighter". For a carry (`distance`, so never given an `rir`) that is
  the lightest dumbbell: 2 kg.
- **Intensity.** `planning._default_calibration_rir` gave every calibration load in reps on a
  kg exercise `rir = 3`, and `plan_generate.v5` told the model to do the same, whatever the
  exercise trains. A throw got a strength-training target.
- **Catalog and retrieval.** Six agility exercises and two rotational-power exercises in
  1,099; general shadow boxing had only just been added next to the hook drill, and nothing
  said one was a narrow drill and the other the general activity. The quality pool took four
  per quality, ordered by relevance only.
- **Pipeline order.** The model received a pool and filled `Workout.title`, `blocks`, then
  `qualities` — objectives last, after the exercises (structured output follows field order).
  Nothing stated the week's frame (frequency, main vs. secondary objectives, split or not,
  the sport's load) before selection.
- **Review.** `goal_coverage` accepted any exercise listing the quality, warm-ups included;
  nothing judged roles, power-set prescriptions, weekly distribution or session length.
- **Rendering.** A superset item line always said "rest N", so the last item repeated the
  round rest the heading already gave.

## Decision

**Intensity by stimulus** (`domain.catalog.intensity_mode`, derived, never stored): from the
exercise's first quality and the prescription's measure — `speed` (power, rotational power,
agility), `pace` (cardio, conditioning), `carry` (a kg-loadable exercise in time or distance),
`none` (bodyweight holds, mobility), `effort` (everything else in reps). Only `effort` in
reps takes `rir`. `services.prescribing.repair_intensity` runs in `planning.judge` after load
substitution: it drops an inapplicable `rir` (logged `intensity.rir_dropped`) and gives an
`effort` calibration the default `rir = 3`. The plan view, the workout blocks and the web
share `prescribing.prescription_load_label`: a calibration says how to pick the load for its
stimulus ("a load you can move fast with clean technique", "a weight you can carry or hold for
the whole set upright, with a firm grip and a little in reserve", "a pace and resistance you
can keep for every interval", or reps in reserve); the catalog `start` kg is never shown.
`speed` work with a kg or bodyweight load gets the cue "every rep fast; end the set when speed
or technique drops". Plans saved before parse and train unchanged; an `rir` stored on a throw
is simply not shown. No new numeric default was introduced: an unknown load stays
`calibration`.

**Block roles** (A§4.5): `Block.role` — `warm_up`, `power`, `strength`, `accessory`, `core`,
`conditioning`, `mobility` — optional and declared before `items`; `Workout.qualities` moved
before `title` and `blocks`. The plan view and the web show a role heading when it changes.

**Catalog** (A§4.4): `specific = true` marks a narrow technique drill
(`bodyweight_shadow_boxing_hook`). Added through the usual workflow (TOML + `make
catalog-index`): footwork in every direction (`stance_footwork_drill`,
`bodyweight_lateral_shuffle`, `shuttle_run`, `carioca`, `lateral_line_hop`, `hop_and_stick`)
and rotational throws (`medicine_ball_rotational_side_throw`, `medicine_ball_shot_put_throw`).

**Retrieval** (ADR 0007): requested qualities are ordered by where the request first names
them; the first two get six pooled exercises, the rest four; general exercises rank before
`specific` ones; three general warm-up activities (`exercise_search.is_general_warm_up`) are
always pooled. `exercise_facts` gain `intensity` and, when true, `specific`.

**Programming frame** (`services.programming.brief`, deterministic, no extra LLM call):
`programming_brief` with sessions and length, a rough working-block budget (about 10–15 min
per block), the main (first two) and secondary qualities, the structure
(`full_body_varied_emphasis` up to three sessions a week, `full_body_or_upper_lower` above,
`split_as_requested` when the preference or the request asks for a split — only its
affirmative part counts: "no split", "I don't want a split" or "без сплита" is not a request
for one, and the word must mean the week's structure — "split squats" doesn't count; the
shared negation reading now also knows contractions), the other sport's
fatigue budget and the session order. `plan_generate.v6` works in that order: the week, each
workout's objectives, each block's role, then exercises and intensity by stimulus.
`plan_revise.v4` gets the role and `rir` rules.

**Review** (`services.plan_quality`, still warnings that drive the one repair round, never
refusals; the guards stay the errors):

- *coverage*: an exercise directly trains the qualities its catalog lists, supports a few
  related ones (`SUPPORTS`: strength → power, power → agility and rotation, core → rotation,
  capacity qualities each other) and is otherwise unrelated. Warm-up blocks don't count.
  A stated quality needs a direct working exercise (`goal_coverage`, saying when only the
  warm-up touches it); the main objective needs a score of 1.5 (direct 1, supporting 0.5:
  two exercises, or one plus supporting work; `weak_coverage`);
- *roles*: a stated role its exercises don't fit; in the warm-up, a `specific` drill, a heavy
  lift or anything that isn't general preparation (`role`; and a heavy session whose first
  block holds no real warm-up work still gets `warm_up`); a warm-up after the main work, or
  any fast (`speed`, not heavy) exercise after strength or conditioning work, whatever its
  block is called, except in warm-up, conditioning and mobility blocks (`order`). Tiring work
  is known by its role or by what it is — a heavy lift, or `pace` work outside the warm-up and
  cool-down — so a mislabeled block doesn't hide it. Exercises are checked in their listed
order, within a block too: a throw after a heavy squat in the same superset is reported; a
throw listed before the squat is not (the listed order is the order done). Without stated roles they are inferred (the leading warm-up-like blocks, heavy
  lifts, then the first exercise's main quality);
- *prescription*: `speed` work in sets above 10 reps (unless also conditioning or agility) or
  with less than 45 s rest before its next set — its rest in a single block; in a superset
  every rest of the round (transitions and the round rest; the other exercises' work is work,
  not rest);
- *week*: with two or three sessions, a region (lower body, pressing, pulling) with at least
  six weekly sets all in one session, unless a split is wanted (`split`); a session estimated
  over 1.25× the session length (`duration`; about 4 s a rep, 1.2 s a meter, a minute of setup
  per block).

**Rendering**: in a superset the heading says rounds and the one rest after each round; each
exercise but the last says its transition ("then 20 s to the next exercise"), the last says
no rest. Same in the workout block and on the web. The web edit form now keeps a workout's
`qualities` and a block's `role` (it dropped `qualities` before).

## Consequences

- The thresholds are coarse, documented heuristics (`WorkloadLimits`); they catch the
  obviously off plan and leave legitimate ones alone, and are not a training-load model.
- The review still can't judge exercise *quality* within a role, movement direction within
  agility work, or the real load of the user's sport sessions; the catalog only lets the model
  choose well.
- `programming_brief`, `intensity` and `specific` change `llm_input`, so they are on every
  decision. Prompt versions record the change.
- After merging: nothing to migrate. The new catalog files' embeddings are already in
  `catalog/search_index.sqlite`.
