# ADR 0017 — The request frames the plan; the profile gives physical data

Status: accepted (2026-10-17). Amends ADR 0007 (candidate pool), ADR 0009 (requested
qualities, review), ADR 0011 (brief) and ADR 0004 (the new-plan question). Changes A§6.4
(generation, guidance, revise) and A§8.1 (`plan_generate.v8`, `plan_revise.v5`,
`assistant.v11`).

## Context

Two requests from the owner, both with a gym profile (focus strength, weight training,
three sessions of 60 minutes, barbell history):

- "Давай разок в неделю по субботам легкую беговую на свежем воздухе. Мне надо понять как
  разминаться, как бежать" gave an "outdoor run + strength" session: brisk walking with
  "easy run" in a note, a dumbbell carry and a Romanian deadlift at 65 kg. Three revisions
  later it was a run.
- "bjj два раза в неделю" gave three barbell days (deadlift, bench press 90 kg, rows).

Root causes, in the code:

- **Words.** The request reader knew quality words ("выносл", "кардио"), not activities:
  "бег", "побегать", "bjj" matched nothing, so the first request read as `mobility` (from
  "размин") and the second as nothing.
- **Profile first.** `requested_qualities` always appended focus and preferences, so
  `strength` was a main objective of both plans, and the only one of the BJJ plan.
- **History.** Every exercise with history was put into the candidate pool, and the prompt
  said to keep them "where they fit": the deadlift and the bench press rode in.
- **Frame.** Sessions per week and length came from the profile only. "Two times a week" was
  in the request text but the brief said three, and the model wrote three.
- **Place.** "Outdoors" changed nothing: the allowed set was the gym's.
- **No question.** The answer to "what should the new plan be?" went straight to the
  generator, which can only produce a plan or a refusal. "BJJ twice a week" (two supporting
  sessions, or BJJ twice and support around it?) had no one to ask.

## Decision

**Reading the request** (`services.plan_request.read`, deterministic, no LLM): the
`Activity` it names (running, cycling, swimming, boxing, grappling, climbing; a new enum,
never stored), the qualities, the sessions per week ("2 раза в неделю", "разок в неделю",
"twice a week", or the number of weekdays named), the weekdays, the session length ("часок",
"полтора часа", "45 мин") and whether it is outdoors. Only the affirmative part counts
(`positive_request`). A request is **understood** when it names an activity or a quality.

- An activity the catalog can carry (running: `easy_jog`, `outdoor_interval_jog`, ...) with
  no other goal named is **practice**: the qualities are `conditioning, mobility`, and the
  sessions are the activity itself. A warm-up word ("размяться") doesn't change that.
  Otherwise it is **support**: the named qualities first, then what the activity needs
  (grappling: strength, muscular endurance, core stability, mobility, conditioning; the
  others as ADR 0009's sport table).

**Applying it** (`planning.apply_request`, before the empty-allowed-set check):

- Its sessions and length replace the profile's defaults in the context, so the brief,
  the time budget and the review all use them.
- Outdoors, the allowed set keeps exercises allowed outdoors with outdoor equipment *and*
  at the profile's location; the context says `outdoor`. The guard context is unchanged:
  plans are judged against the profile on Save and Start, so nothing outside it may be
  offered. (`outdoor_interval_jog` is outdoor-only and so is not offered to a gym profile;
  `easy_jog` is.)
- **Understood → physical data only.** Focus and preferences are dropped (`focus` becomes
  nullable on `UserContext`). Age, weight, experience, screening and the training done
  elsewhere (`sport`, a load to recover from) stay: the planner should know a 120 kg owner
  starts running with run–walk intervals. History stays only for exercises that train one
  of the requested qualities (and, on a revision, the plan's own exercises); the pool takes
  its history from that list.
- The activity's catalog exercises (`ACTIVITY_EXERCISES`) are always pooled for practice:
  the per-quality picks were alphabetical among conditioning work (burpees), and "беговую"
  doesn't match "Лёгкий бег" lexically.
- The brief gains `activity: {name, mode}`, `weekdays` and `setting: outdoors`.

**Revisions** read the revision text the same way. One that names no goal ("убери
скакалку") keeps working toward the plan's own declared objectives
(`plan_request.plan_qualities`) with physical data only, instead of falling back to the
profile's focus. `plan_revise` now gets the brief too.

**Review** (`quality.unrequested_objective`): when the request is understood, a workout may
state only the requested objectives or one that supports them (ADR 0011 `SUPPORTS`). The
outdoor filter alone would have removed the barbell from the run; this catches the same
mistake in a gym ("easy run on the treadmill" plus a strength block). No check on barbells
as such: whether one fits is the objective's question, not the equipment's.

**Asking first.** "New plan" with existing plans now opens the new plan's planning session
and records its question there; the answer goes to the assistant (not straight to the
generator). `assistant.v11` asks one short question when the description leaves open what
the plan is for (practice or support of a sport), how many days, or where; otherwise it
calls `new_plan` at once. Requests it passes to `new_plan` / `rewrite_plan` carry what the
conversation established (activity, days, length, place), and a redesign restates what the
plan is for, since the designer sees only that text. **Generate anyway** still generates
directly; **Cancel** closes the session. The website's new-plan form is unchanged (direct).

**Prompts.** `plan_generate.v8`: "what the plan is about" (practice: warm-up, the activity,
cool-down, nothing else; support: never presented as practice of the sport; impact work
scaled to weight, age and experience), history kept only where it serves the objectives,
the other sport only a fatigue budget when an activity is named. `plan_revise.v5`: the same
section. `assistant.v11`: the clarifying question and full requests.

## Consequences

- The word lists are small and explicit, Russian and English. A request in other words
  ("пробежка" is covered, "трусца" is not) falls back to the profile frame as before; the
  assistant's question is the safety net for unclear ones.
- One more model call when a new plan is described after "New plan": the assistant's turn
  before generation.
- A plan made "outdoors" is still judged against the profile's location; there is no
  per-plan location. Outdoor-only catalog entries are therefore not offered to a gym or
  home profile.
- `llm_input` changes (nullable `focus`, filtered history, new brief fields) and the prompt
  versions change `content_version`.
- After merging: nothing to migrate and no re-index needed.
