# ADR 0018 — LLM-first conversational planning (experiment)

Status: **accepted** (2026-10-18; experimental 2026-10-17 on branch `experiment/llm-first-planning`,
base `main` 6aa2bf5, merged via PR #16). Relates to ADR 0003/0004
(assistant, sessions: same staging pattern, new agent), ADR 0007 (catalog retrieval is
optional on this path), ADR 0010 (exclusions follow the staged plan), and ADR 0011/0016/0017
(brief, time budget, request reading: not run on this path). Changes A§4.5 (activity items,
`Plan.location`, `MAX_ACTIVITY_SECONDS`), A§6.3 (history length, routing), A§6.4 (new §6.4.1),
A§8.1 (`planner` row, `planner.v2`) and A§11 (`llm eval-conversations`).

## Context

ADR 0017 addressed the owner's two requests ("легкую беговую по субботам" with a gym
profile, "bjj два раза в неделю") inside the generation pipeline. It did this with word
lists, a request reader, an objective filter in the review and one clarifying question in
the assistant. The plans it produces are formally valid; nothing in this ADR verifies
whether they are what was asked for. The owner's
assignment for this experiment says that this direction makes FitMe better at valid plans
and no better at what the user asked for. It asks for **LLM-first, structure-second**: the
model reads the request and talks it through, the structure records what was agreed, and
the deterministic layers keep only technical and safety validation. The experiment is a
separate branch so that it can be compared with the legacy pipeline and thrown away.

The data model could not hold the scenarios. A BJJ class or a 90-minute walk has no catalog
id. "Outdoors" for a gym profile was judged at the gym. A timed set was capped below 90
minutes. A multi-turn talk was cut at 20 messages.

## Decision

**Activity items** (`domain/models.py`). `Prescription.exercise_id` may be
`activity:<slug>` (`ACTIVITY_PREFIX`, `is_activity_id`; slug 3..60 characters of `a-z0-9_`)
with a required `activity_name` (trimmed to 60 characters). Such an item is exactly one set
measured in time, takes a `bodyweight` load, no `rir` and no `declared_kg`. Any other item must leave
`activity_name` empty. In the guards (`guards/plan.py`), an activity item gets one
`plan.activity` verdict instead of the catalog, location, equipment, screening, experience
and load checks. That verdict **fails when any area flag is "yes"**: there is no
contraindication data to check the flag against (AGENTS.md §2, fail closed). The plan-wide
screening gate still runs, and a bare unknown id still fails `plan.catalog_id`. Wording
replaces a forbidden term in `activity_name` (`planning._sanitize_wording`). The bot and the
web show the name, never the id (`bot/plan_rendering.py`, `bot/train_rendering.py`,
`web/deps.display_name`).

**The legacy agents keep their old output** (`planning.AgentScope`, `scope_verdicts`).
`plan_generate`, `plan_import` and the history import get `AgentScope()`, which allows no
activity items and no plan location. `plan_revise` gets `AgentScope.of(base)`, and
`session_adjust` gets `AgentScope.of_workout`. Under those scopes an `activity:` id is
allowed only if the base already holds it, with the same name, no more often and with no
more total `sets × seconds`. Anything else fails `plan.activity_not_allowed` or
`plan.location_not_allowed`. `existing_plans_summary` hides activity ids from
`plan_generate`. Plans the user stages, confirms or edits are judged with no scope.

**Plan location** (`Plan.location`, optional). It is where this plan is trained. The
profile location is the default, not a limit. `guards.plan.at_location` swaps it into the
guard context together with that location's equipment (`domain.catalog.equipment_at`: the
location default, or the user's own home selection, `GuardContext.home_equipment`, for
`home_equipment`), so a gym plan of an outdoor profile is judged with the gym's kit. It
applies wherever a plan or a workout is judged:

- `validate_plan`;
- `validate_workout(..., location=)` for the `/train` review, Start, Save to plan and
  mid-session edits;
- `plan_edit.blocking_errors` (web editor);
- the recap's suggestion filter.

A legacy revision whose output has no location keeps the base plan's location.

**Set length and history.** An activity item's one timed set may be up to
`MAX_ACTIVITY_SECONDS` = 10800, so a 90-minute class is one set. A catalog exercise keeps
`MAX_SET_SECONDS` = 3600, in a prescription and in a logged result
(`guards.plausibility.check_parsed_seconds`). `set_logs` seconds columns have only a
lower-bound CHECK. `conversations.HISTORY_LIMIT`
is 100 messages (it was 20). Retention is unchanged.

**The `planner` agent** (`llm/agents.planner_agent`, `prompts/planner.v2.md`, default tier
large, `max_tokens` 16000, timeout 240 s, 2 output retries; override with
`FITME_LLM_AGENT_PLANNER`). It has the same deps, input and output as `assistant`
(`AssistantTurn | Refusal`). Its tools are the lookups (`list_plans`, `get_plan`,
`list_recent_sessions`, `get_session`, `find_exercises`, `find_substitutes`), `stage_plan`,
`save_draft`, `discard_draft`, `rename_plan`, `set_default_plan`, the exclusions,
`fix_logged_sets`, `report_safety_concern` and `show`. It has no typed edit ops, no
`rewrite_plan`/`new_plan` and no workout logging. The prompt tells it to:

- propose in plain language, state its assumptions, and ask at most one question;
- tell a narrow correction from a broader constraint;
- never fit a catalog id to a different activity;
- stage the agreed plan exactly.

**`stage_plan(plan, plan_id)`** (`services/assistant._Turn.stage_plan`) takes a whole
`Plan`: catalog ids, `activity:` items and an optional `location`. `plan_id` is the saved
plan it changes, and null means a new plan. A current draft of another plan must be saved or
closed first. It replaces any `declared_kg` with the base's. It judges a copy with
`planning.judge` (no scope), using inputs read for the target plan (`_inputs_for`: that plan's
exclusions, plus a draftless new-plan talk's exclusions when they will move to it). A failure
goes back to the model in the same run, and nothing is staged. A success answers only load
notes and repairs. The planner's search ranks exercises allowed at any location
(`_any_location_ids`, each location with its own equipment) and returns `kg_loadable`, `load_unit`, `equipment` and `locations`.

**Nothing is written until the run ends** (ADR 0004 pattern). After the run, `_apply`
records the staged plan with `planning.record_edit_draft`: a `plan_revise` draft round with
`user_report.base = "planner_stage"`, `prompt_template = planner`, judged again. The plan
becomes the planning session's draft (`conversations.planning_draft_shown(retarget=True)`).
The bot shows it whole with Confirm / Change something / Cancel
(`bot/handlers/plan.show_staged_draft`, no session-length warning), then any load the
guards limited. Confirm, or `save_draft` in the same turn, is the unchanged
`planning.confirm_plan`. A "save it" whose draft fails when recorded saves nothing.

**Routing** (`services.assistant.handle_message`). With an active `/train` session (status
`draft`, `confirmed` or `in_progress`: precheck, review or started), a message goes to the
legacy `assistant`. Every other message goes to `planner`. A planner exchange outside any
session opens a new plan's planning session, so the next message sees it. A plan staged
later in that session moves the draftless session to the staged plan, and its new-plan
exclusions move with it (`open_planning(retarget=)`). `/cancel` closes an open planning
session.

**Buttons** (`bot/handlers/plan.py`):

- New plan always asks first, including when there are no plans yet
  (`plan.new_first_prompt`).
- Change something on a draft and Revise on a saved plan post their prompt into that plan's
  planning session. They set no pending revision, so the next text reaches the planner.
- **Generate anyway** still runs the legacy `propose_new_plan` from the profile.

**Not run on the planner path:**

- the request reader and `apply_request` (ADR 0017);
- the programming brief (ADR 0011);
- the candidate pool (ADR 0007/0011);
- `plan_quality.review` (ADR 0009/0017);
- `time_budget` (ADR 0016);
- `plan_generate` and `plan_revise`.

These are the layers that shape a plan's content. The experiment tests whether the model
can do that from the conversation alone.

**Still deterministic on the planner path:**

- the gate and the stop-word scan before the model;
- `validate_plan` with load-engine substitution and limited-load notes (A§7.3);
- wording;
- measure repair (`measures.repair_measures`) and intensity repair
  (`prescribing.repair_intensity`);
- the judging again in `record_edit_draft` and in `confirm_plan`;
- `/train` with `validate_workout` at the plan's location;
- set logging, the load engine and the recap.

## Consequences

- **AGENTS.md §7 posture.** §7 asks for plan-first: a structured plan with a small dialog for
  adjustments, and no chat-first companion. The planner proposes in prose first and
  structures the plan on agreement. This is a deliberate experiment within §7's intent:
  every planning flow still ends in a structured plan with buttons, and the prompt rules out
  persona, small talk and encouragement. Whether this fits §7 is the owner's decision. It
  must be made before any of this is merged. Decision (2026-10-18): the owner accepted the
  prose-first proposal within §7's intent — the dialog still ends in a structured plan with
  buttons; no persona.
- **Cost shape.** One agent run per message, on the large tier, with no escalation. A
  staging turn takes at least two model requests (the plan as tool arguments, then the
  closing message), plus one per guard correction and up to two output retries. The whole
  plan passes through tool arguments as output tokens, and again on every re-stage. The
  input carries the context (`allowed_exercise_ids` for the profile location, history) and
  up to 100 history messages each time. Not measured on a live model.
- **Known limitations** (verified in the code, not fixed):
  - A saved plan with an activity item is refused at Save and at `/train` Start once any
    area flag becomes "yes".
  - The web editor cannot save any edit to such a plan while a flag is "yes". The way out is
    removing the item in the bot or a revision.
  - A legacy revision cannot clear `Plan.location`.
  - The saved-plan view still shows the profile session-length warning. Only the staged
    draft view omits it.
  - The web swap dropdown and the legacy assistant's typed-op allowed set use the profile
    location, not the plan's.
  - `/stats` on the web shows an activity by its slug in words ("Bjj class") because logged
    sets carry no `activity_name`.
  - Raw ids can reach the user in blocked-draft lines, failed set-correction and
    undo-blocked details, and the `exercise_name` fallback.
  - Messages during precheck or review go to the legacy assistant, which can still call
    `rewrite_plan`/`new_plan`.
  - An exclusion stated in a session-less talk about a saved plan stays on that plan when
    the draft is cancelled.
  - `confirm_plan` never renames an existing plan's record: a re-staged plan's new name
    lives only in the version body.
  - The planner's context carries the profile's focus and preferences and the full
    history. ADR 0017's filtering is not applied; the prompt says the profile is a default.
- **Still legacy.** The website's new-plan and revise forms, Generate anyway, and the legacy
  assistant's `rewrite_plan`/`new_plan`. The bot's `PendingRevision` branch is now
  unreachable from any button but kept.
- **`content_version` changes**: guard sources, `planner.v2.md`, domain models.
- **No migration**: plan bodies are JSON, decision kinds are reused (`plan_revise` draft
  rounds, `plan_confirm`), and `llm_calls.purpose` / `decisions.prompt_template` say
  `planner`. No re-index.
- **Going back works for plans that use none of the new features.** `main`'s domain models
  use `extra="forbid"` and cap a timed set at 3600 s. Every stored body (`plan_versions.body`,
  the plans and workouts in `decisions.proposal`) is written through
  `domain.models.storage_body`, which leaves `location` and `activity_name` out while they
  are unset. So a body written on the branch parses on `main` unless the plan has an
  activity item, a plan location, or a timed set longer than 60 minutes; such plans have to
  be deleted (`/plan` → delete) before switching back. Logged sets with `activity:` ids stay
  in `set_logs` after the plan is deleted, and `main` shows them by their raw id. Checked by
  `tests/integration/db/test_storage_body.py` against a copy of `main`'s models and bounds.
  Testing against a copy of the database (`FITME_DB_PATH`, `fitme backup`) is still the safe
  default.
