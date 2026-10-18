# LLM-first conversational planning — experiment report

> Status: the experiment was accepted and merged into `main` on 2026-10-18 (PR #16). The text below is the report as written during the experiment; the live-model evaluation table remains to be filled by the owner.

Branch `experiment/llm-first-planning`, base `main` 6aa2bf5, 11 commits up to 4ed05d6, plus
the commit that adds this report and ADR 0018. Not merged. Design record:
`docs/adr/0018-llm-first-conversational-planning.md`; detailed path: A§6.4.1.
Everything below was checked against the code and tests at that commit. Where something
could not be checked, the text says "not verified". **No live-model evaluation has been run
yet** (§8).

## 1. Summary of the experimental architecture

```
Telegram text ──► stored + stop-word scan + /plan gate          (unchanged, deterministic)
             ──► active /train session?  yes ──► legacy `assistant` (logging, typed edits)
                                          no  ──► `planner` agent (large tier, one run)
                       context: pseudonymized profile, flags, allowed ids, history,
                                plans, current draft, session turns (≤100)
                       tools:   lookups · find_exercises (optional) · stage_plan · save_draft …
                       │
                       ├─ plain-language proposal / answer / one question   (nothing written)
                       └─ stage_plan(Plan) ──► planning.judge  (guards, load engine,
                                                wording, measure + intensity repair)
                                  fail ──► back to the model, same run
                                  ok   ──► after the run: record_edit_draft (judged again)
                                           ──► whole draft: [Confirm] [Change something] [Cancel]
                                           ──► confirm_plan (judged again) ──► /train, set logs
```

The planner is one pydantic-ai agent. It handles every free-text message that is not sent
during a `/train` session. It answers in plain language, proposes sessions in words, and
stages a structured `Plan` itself when the user agrees. The application does not shape the
plan's content on this path. There is no request reader, brief, candidate pool, quality
review, time budget or plan designer agent. The application only validates the staged plan
with the same deterministic guards every plan passes, records it as the planning session's
draft, and shows it with the existing buttons. Save is the unchanged `confirm_plan`, so the
saved plan is the staged plan after the guards' load substitution. The data model gained
three things so that a plan can say what the user actually does:

- non-catalog activity items (`activity:<slug>` with a name, timed, unloaded);
- a per-plan `location`;
- timed sets up to 3 h.

The legacy agents are scoped so that they cannot produce either of the first two. The legacy
generator is still reachable ("Generate anyway", the website). There is no feature flag: the
branch is the switch.

## 2. Existing components reused

| Component | How it is used on the planner path |
|---|---|
| `services.assistant.handle_message`, `_Turn`, `_apply` (ADR 0003/0004) | Same entry point, state, history, staging and apply-after-run. The planner is a second agent on the same deps. |
| `services.conversations` planning sessions | The session holds the conversation and the current draft. `open_planning` gained `retarget=`. |
| `planning.judge` | Judges every staged plan: `validate_plan`, load-engine substitution, wording, measures, intensity. |
| `planning.record_edit_draft` | Writes the staged plan as a `plan_revise` draft round (`base = planner_stage`). |
| `planning.confirm_plan` | Unchanged save: new `plans` row or new `plan_versions` row, `origin = llm`. |
| Guards (`guards/plan.py`, ceiling, progression, check-ins, screening) | Unchanged rules. Location fit can read `Plan.location`. Activity items get one `plan.activity` verdict. |
| Load engine (`services/loads.py`) | Replaces loads above the guards. The user and the model are told. |
| Catalog search (`services.exercise_search`, ADR 0007) | Behind `find_exercises` / `find_substitutes`, optional, ranked over every location. |
| Exclusions (ADR 0010) | `add_exclusion` / `remove_exclusion`. Judged for the staged plan's target. |
| Bot rendering and callbacks (`draft_markup`, `PlanDraft`, `/plan` menu, `/train`) | Whole-draft view with the existing Confirm / Change something / Cancel. `/train` blocks, set logs and recap are unchanged. |
| `llm/context.py` pseudonymization, `llm/usage.py` | Same prompt rendering and `llm_calls` recording (`purpose = planner`). |
| Decision log | Same tables and kinds. `prompt_template = planner`. |

## 3. New components

| File | Purpose |
|---|---|
| `src/fitme/prompts/planner.v2.md` | The planner prompt (85 lines, 1822 words; v2: one bulk `find_exercises` call): understand first, propose in prose, one question at most, narrow vs broad corrections, catalog vs activity items, staging rules, schema. |
| `src/fitme/llm/agents.py` (`planner_agent`, `_PLANNER_TOOLS`, `_stage_plan`) | The agent and its tool list. |
| `src/fitme/llm/models.py` | `planner` default tier `large`, `max_tokens` 16000, timeout 240 s, 2 output retries. |
| `src/fitme/services/assistant.py` (`_Turn.stage_plan`, `_inputs_for`, `_any_location_ids`, `_facts`) | Staging a whole plan, judging it for its target plan, search over every location, exercise facts in search hits. |
| `src/fitme/services/planning.py` (`AgentScope`, `ActivityAllowance`, `scope_verdicts`, `draft_plan_id`) | Keeps activity items and plan locations out of the legacy agents' output. |
| `src/fitme/domain/models.py` | `ACTIVITY_PREFIX`, `is_activity_id`, `activity_fallback_name`, `Prescription.activity_name`, `Plan.location`, `MAX_ACTIVITY_SECONDS = 10800` (activity items only; catalog exercises keep `MAX_SET_SECONDS = 3600`). |
| `src/fitme/guards/plan.py` (`at_location`, `_activity_verdict`), `src/fitme/domain/catalog.py` (`equipment_at`) | Plan location in location and equipment fit. Activity verdict that fails closed on area flags. |
| `src/fitme/bot/handlers/plan.py` (`show_staged_draft`) | Whole-draft display of a staged plan. Buttons feed the planner. |
| `src/fitme/bot/handlers/start.py` | `/cancel` closes an open planning session. |
| Locales (`en.toml`, `ru.toml`) | `new_first_prompt`, `changes_draft_title`, `location_line`, `activity_line`, `activity_fallback`. |
| `src/fitme/llm/eval_conversations.py`, `src/fitme/cli/llm_eval_conversations.py`, `Makefile` target | Conversation eval harness: replays scenarios in planner and legacy mode, data-only checks, report with transcripts. |
| `tests/fixtures/llm_eval/conversations/*.json` | 6 fixtures: `a_easy_run_saturdays` (ru), `a_easy_run_saturdays_en`, `d_hour_run`, `e_bjj_twice` (ru), `e_bjj_twice_en`, `f_g_strength`. |
| `tests/bot/test_planner_flow.py` | 26 end-to-end bot tests with a scripted planner. |
| `tests/bot/test_activity_plan_flow.py`, `tests/guards/test_activity_and_plan_location.py`, `tests/integration/db/test_activity_plans.py` | Activity items, plan location, `AgentScope`. |
| `tests/integration/db/test_eval_conversations.py`, `tests/unit/test_llm_eval_conversations_fixtures.py` | The harness on scripted models and fixture validation. |

## 4. Legacy components bypassed on the planner path

Not run for a planner message:

- `services.plan_request.read` / `planning.apply_request` (ADR 0017);
- `services.programming` brief (ADR 0011);
- `planning._candidate_pool` (ADR 0007/0011);
- `plan_quality.review` and `workload` (ADR 0009/0016/0017);
- `time_budget` (ADR 0016);
- the `plan_generate` and `plan_revise` agents with their retry, repair and escalation.

Nothing was deleted. They remain reachable through:

- **Generate anyway** under the New plan question: `propose_new_plan` from the profile;
- the **website**: the new-plan form (`propose_new_plan`) and the revise form (`revise_plan`)
  are unchanged;
- **messages during a `/train` session** (precheck, review, in progress): the legacy
  `assistant`, whose `rewrite_plan` / `new_plan` still call `plan_revise` / `plan_generate`;
- `make llm-eval` (the old per-agent eval) and the legacy mode of
  `make llm-eval-conversations`.

The bot's `PendingRevision` branch (`plan.py`, `revise_from_base` on pending text) is still
in the code. No button sets it any more.

## 5. Configuration

- **No feature flag.** The owner decided the branch is the switch. Check out
  `experiment/llm-first-planning` to use the planner and `main` to go back.
- `FITME_LLM_AGENT_PLANNER=<provider:model>` sets the planner's model. Without it the planner
  uses the large tier, `FITME_LLM_TIER_LARGE`. Other agents are configured as before.
- Before going back to `main`, read the data caveat in §9: `main` cannot read a plan
  that uses an activity item or a plan location. Delete
  those plans first. Testing on a copy of the database (`FITME_DB_PATH`) is the safe default.

## 6. Database changes

None. No migration was added.

- `plan_versions.body` and `decisions.proposal` are JSON. `activity_name` and `location` are
  new keys in the body, written only when set (`domain.models.storage_body`), so a body of a
  plan without activity items or a location has `main`'s shape (a catalog exercise's timed
  set keeps `main`'s 3600 s cap; only an activity item may run longer).
- `set_logs.planned_seconds` / `actual_seconds` have only a lower-bound CHECK, so 5400 s
  fits. Logged sets of an activity item keep their `activity:` id.
- Decision kinds are reused: a staged plan is a `plan_revise` draft round with
  `user_report.base = "planner_stage"`, and Save is `plan_confirm`. `prompt_template =
  planner`, `llm_calls.purpose = planner`.
- `make migrate` is a no-op, and no catalog re-index is needed.

## 7. Test results

`make precommit` at 4ed05d6, run for this report:

```
uv run ruff check .                 All checks passed!
uv run ruff format --check .        302 files already formatted
uv run mypy src                     Success: no issues found in 143 source files
uv run fitme catalog check          catalog OK: 1105 exercise(s); locales OK: en, ru
                                    content_version: c37f70165099
uv run pytest                       2266 passed in 109.60s
```

On `main` 6aa2bf5 the suite had 2176 tests, a figure from the earlier work-package reports
that was not re-run here. The branch adds 90.

**What these tests prove.** They prove plumbing, not understanding. Every planner test
scripts the model's answers (`FunctionModel`), so they show:

- a staged plan is judged, shown, saved and trained as staged;
- the conversation and the current draft reach the next message;
- guards limit loads and refuse what they must;
- the legacy agents cannot emit activity items or plan locations;
- the buttons route to the planner.

They do not show that a real model proposes a run when asked for a run. Only the live
evaluation in §8 can answer that.

## 8. Evaluation results for the regression scenarios

### Scripted (deterministic, in CI)

"Scripted run" is the scripted-model run in `tests/integration/db/test_eval_conversations.py::test_every_fixture_runs_in_both_modes_and_is_judged`.
Both modes get the same scripted plans, except that legacy cannot carry a location or an
activity item.

| Scenario | Deterministic bot test (`tests/bot/test_planner_flow.py::…`) | Eval fixture (`tests/fixtures/llm_eval/conversations/…`) | Scripted run |
|---|---|---|---|
| A — easy outdoor run | `test_an_easy_outdoor_run_is_proposed_staged_saved_and_trained` | `a_easy_run_saturdays.json`, `a_easy_run_saturdays_en.json` (turns 1–2) | planner: all checks pass; legacy: pass, `location` n/a |
| B — remove jump rope | `test_a_change_sees_the_draft_and_the_conversation_and_replaces_the_draft` | `a_*` turn 3 | planner: pass; legacy: pass |
| C — no gym equipment on a run | same test (draft and history reach the next message); no dumbbell-specific bot test | `a_*` turn 4 | planner: pass; legacy: pass |
| D — "about an hour" | none | `d_hour_run.json` (question or proposal, then "Ok, do that.") | planner: pass (question reported, not counted); legacy: plan staged, `cardio_minutes` reported |
| E — BJJ twice a week | `test_bjj_twice_a_week_asks_once_then_stages_the_classes_and_trains_them`, `test_an_activity_item_with_an_injury_area_flagged_is_refused_to_the_model` | `e_bjj_twice.json`, `e_bjj_twice_en.json` | planner: pass; legacy: **fails**: the activity item is refused (`no_safe_plan`), nothing is saved, `activity_items_min` n/a |
| F — three-day strength | `test_a_load_above_the_guards_is_limited_in_a_staged_plan` | `f_g_strength.json` turns 1–2 | planner: pass; legacy: pass |
| G — bench → dumbbell bench | `test_a_barbell_to_dumbbell_swap_starts_from_its_own_load` | `f_g_strength.json` turn 3 | planner: pass (`single_change`, `swap_load` = calibration); legacy: pass |
| H — save and execute | `test_save_it_stages_and_saves_in_one_turn`; Confirm and `/train` in the A and E tests | `a_*` turn 5, `e_*` turn 3, `f_g_strength` turn 4 | planner: saved plan = staged plan; legacy: pass except E (nothing to save) |

The scripted planner passes by construction, because its answers were written to pass.
The legacy E failure is a property of the data model and `AgentScope` (legacy cannot hold a
BJJ class), not of a model's judgement.

Two limits of the harness:

- Legacy mode calls `propose_new_plan(guidance=…)` and `revise_plan` directly. It skips the
  bot's ADR 0017 assistant turn before generation, and it skips that turn's clarifying
  question. Legacy call counts are therefore one assistant run per message lower than in the
  bot.
- A clarifying question gets no scripted answer: the next fixture turn is sent as written.

### Live results

**No live-model evaluation has been run.** It costs money, and it is the owner's to run:

```sh
make llm-eval-conversations YES=1                          # every scenario, planner and legacy
make llm-eval-conversations YES=1 MODE=planner             # MODE = planner | legacy | both
make llm-eval-conversations YES=1 SCENARIO=e_bjj_twice     # one fixture by name
make llm-eval-conversations YES=1 MODEL=anthropic:<model>  # one model for every agent and tier
```

It prints a planner-vs-legacy table and writes the full report with transcripts to
`docs/reports/llm_first_eval_<YYYY-MM-DD_HHMMSS>.md`. Paste its results here:

| Scenario | Mode | Checks passed | Model calls | Tokens (in / out) | Cost (USD) | Latency (s) | Human verdict |
|---|---|---|---|---|---|---|---|
| a_easy_run_saturdays | planner | | | | | | |
| a_easy_run_saturdays | legacy | | | | | | |
| a_easy_run_saturdays_en | planner | | | | | | |
| a_easy_run_saturdays_en | legacy | | | | | | |
| d_hour_run | planner | | | | | | |
| d_hour_run | legacy | | | | | | |
| e_bjj_twice | planner | | | | | | |
| e_bjj_twice | legacy | | | | | | |
| e_bjj_twice_en | planner | | | | | | |
| e_bjj_twice_en | legacy | | | | | | |
| f_g_strength | planner | | | | | | |
| f_g_strength | legacy | | | | | | |

The checks are proxies: weekdays, catalog kinds and equipment, load kinds, item diffs. The
human verdict column is for reading the transcripts: did it do what was asked, and how many
corrections did it need?

## 9. Known limitations

Items 1–17 were checked in the code at 4ed05d6, item 18 at the WP5 commits (c0a6547 and its follow-up).

**Data and going back**

1. **`main` cannot read a plan that uses an activity item or a plan location.** `main`'s
   domain models use `extra="forbid"` and cap `Prescription.seconds` at 3600. The branch
   keeps that cap for catalog exercises; only an activity item may run to 10800 s. Stored
   bodies leave `location` and `activity_name` out while unset
   (`domain.models.storage_body`), so every other plan written on the branch parses on
   `main` (`tests/integration/db/test_storage_body.py` checks this against a copy of
   `main`'s models and bounds). A plan with either does not; delete it (`/plan` → delete)
   before switching back. A plan saved by an earlier branch build with a catalog timed set
   over 3600 s no longer parses on the branch either. Logged sets with
   `activity:` ids remain in `set_logs` after the plan is deleted, and `main` shows them by
   their raw id. Bodies written by branch builds before
   `storage_body` existed still carry `"location": null` and `"activity_name": null`; `main`
   refuses those too. Testing on a copy of the database is still the safe default.

**Activity items and area flags**

2. An activity item is refused at Save (`confirm_plan` re-judges) and at `/train` Start
   (`validate_workout`) once any area flag becomes "yes" after it was staged.
   (`guards/plan._activity_verdict`)
3. The web editor cannot save any edit to a plan holding an activity item while an area flag
   is "yes": `plan_edit.blocking_errors` fails closed. The way out is removing the item
   through the bot or a revision.

**Plan location**

4. A legacy revision cannot clear `Plan.location`: `revise_plan` restores the base's location
   when the output has none. The planner can clear it by staging without one.
5. The equipment follows the plan's location (`domain.catalog.equipment_at`) in the guards,
   the planner's search, the typed-op allowed set and the web swap dropdown. The legacy
   `plan_generate` outdoor request (`planning.apply_request`) still offers only what fits
   both outdoors and the profile's own equipment, since that output is judged at the
   profile's location.

**Display**

6. The saved-plan view (`_send_detail`) still shows the profile session-length warning. Only
   the staged draft view omits it.
7. `confirm_plan` never renames an existing plan's record. A re-staged plan's new
   `Plan.name` lives only in the version body.
8. Web `/stats` and the training detail page name an activity from its slug ("Bjj class"),
   because logged sets carry no `activity_name`.
9. Raw ids can reach the user:
   - in blocked-draft lines (`assistant.blocked` plus guard details such as
     "dumbbell_row: missing equipment: bench");
   - in failed set-correction and undo-blocked details;
   - through the `exercise_name` fallback for a bare unknown id.

**Routing and sessions**

10. Messages during precheck or review (a `/train` session exists but is not started) go to
    the legacy assistant, which can still call `rewrite_plan` / `new_plan`.
11. An exclusion stated in a session-less talk about a saved plan stays on that plan even if
    the draft is then cancelled (deliberate). Related, and rare: tapping Change on an older
    draft that is still current, while a draftless new-plan talk holds exclusions (the talk
    opened after the draft's session idled out), moves those exclusions to the draft's
    plan, and Confirm then judges the draft with them.
12. The planner's context is the full profile context: focus, preferences, every exercise's
    history, and the allowed ids for the profile location. ADR 0017's "understood request:
    physical data only" filter is not applied. The prompt says the profile is a default, not
    a limit; whether the model follows that is for the live eval.

**Eval harness**

13. Tool round-trips inside one run are not counted separately.
14. A clarifying question gets no scripted answer.
15. Each scenario and mode runs once.
16. Legacy mode cannot represent a plan location or activity items (those checks are n/a),
    and it skips the bot's assistant turn before generation.

**Prompt versioning**

17. `planner.v1.md` was edited in place in 8446cd8 rather than bumped to v2. It was never
    released, but decisions written on the branch before that commit say `planner` v1 with
    the earlier text.

**Cost**

18. Observed live on 2026-10-18: a strength-plan turn looked exercises up one per model
    request (13 single `find_exercises` calls), hit the 15-request cap and ended in the
    "LLM unavailable" refusal (logged as `provider_error`) after ~15 large-tier inputs. Fix:
    `find_exercises(queries)` takes every name in one call (one embedding request),
    `planner.v2` asks for that, the planner's cap is `PLANNER_REQUEST_LIMIT` = 30 (the
    assistant keeps 15), and the cap is logged as `request_limit`.

## 10. Comparison: experimental vs legacy

| Axis (§14) | Planner (experimental) | Legacy (generation pipeline) |
|---|---|---|
| Intent fidelity | **Only the live eval can answer.** Scripted tests show that what the model stages is what gets saved and trained, with no converter in between. | Same: live eval. Scripted: it cannot represent a BJJ class or an outdoor plan location, so E fails structurally. |
| Conversational coherence | The model sees the session's last 100 messages and the current draft in every turn (tested). Corrections reconsider the whole plan because the model re-stages it whole. Whether it does that sensibly is for the live eval. | The legacy assistant sees history too, but a redesign goes to `plan_revise`, which sees the current plan, the request and the brief (ADR 0017), not the conversation. |
| Simplicity | One agent run per message. The content is the model's. The app validates. | Request reader → brief → pool → `plan_generate` (≤2 attempts) → time budget → review → repair; then `plan_revise` per redesign. |
| Practicality | Plain-language answers, then a plan. Activity items let a class or a walk be logged as what it is. | Every answer is a plan or a refusal. Activities must map to catalog ids. |
| User effort | **Only the live eval can answer** (corrections per scenario, human verdict). Scripted tests show that one message can stage and save ("Looks good. Save it."). | Same: live eval. |
| Structured usability | Same buttons, same save, same `/train` and logging (tested end to end for A and E). | Same. |
| Reliability | Deterministic guards on stage, record and save. A failed stage goes back to the model in the same run. Nothing is written before the run ends. | Same guards, plus retries and escalation. |
| Implementation complexity | See the numbers below. | See the numbers below. |
| Operational cost | One large-tier run per message, ≥2 requests per staging turn, the whole plan as output tokens each time. Not measured live. | Assistant (medium) + `plan_generate` (large, ≤2 attempts) per new plan, assistant + `plan_revise` (medium, +1 escalation to large) per redesign. Not measured live. |

**Numbers**, all computed at 5f4d7ea.

Code that runs only on the generation path:

- `services/planning.py` functions `propose_new_plan`, `revise_plan`, `apply_request`,
  `_candidate_pool`, `review_quality`, `request_qualities`, `_fit_time_budget`,
  `_round_from_escalation`, `existing_plans_summary`, `_log_attempt`, `_record_refusal`,
  `_resolve_base`, `_draft_plan`, `_unwrap`, `_set_aside_later_attempts` and
  `_no_allowed_exercises`: **705 lines**;
- modules: `plan_quality.py` without its display part (861 − 127 = 734), `workload.py`
  286, `plan_request.py` 248, `programming.py` 157: **1425 lines**;
- total: **about 2130 lines**.

Code that runs only on the planner path:

- `_Turn.stage_plan` 64, `_inputs_for` 16, `_any_location_ids` 18 and `_facts` 14 in
  `services/assistant.py`;
- `_stage_plan` and `planner_agent` in `llm/agents.py`: 22, and `_PLANNER_TOOLS`: 22;
- `show_staged_draft` in `bot/handlers/plan.py`: 58;
- `_retarget` and `_has_current_draft` in `services/conversations.py`: 11;
- the routing, auto-open, retarget and `carry` lines in `services/assistant.py` and
  `record_edit_draft`: about 40;
- total: **about 265 lines**, plus the 85-line prompt.

Shared (display): `time_budget.py` (265 lines: `render_plan_text` → `duration_warning`, and
the web `_plan_body.html`) and the `block_roles` / `block_relevance` part of
`plan_quality.py` with its helpers (127 lines), which `time_budget` uses: **392 lines**.

Shared by both paths:

- `services/assistant.py`: 1544 lines. The bot's legacy path also runs the assistant before
  generation (ADR 0017).
- Judging and saving in `services/planning.py`: `read_snapshot`, `gate`, `build_inputs`,
  `judge`, `_judge_with`, `_sanitize_wording`, the engine and declared-load helpers,
  `record_edit_draft`, `confirm_plan` and so on: 612 lines.
- The guards.

What the branch added: the support for activity items and plan locations (domain, guards,
rendering) and `AgentScope` (113 lines, which only protect the legacy agents). Branch total
in `src/`: +2495 / −126 lines, of which the eval harness and CLI account for 1513. The
codebase grew, because the legacy path is kept. What is shorter is the path one message
takes.

LLM calls for scenario A in five messages (request, agree, two corrections, save):

- **Planner**: 5 runs and 5 `llm_calls` rows, all large tier. That is ≥1 + 2 + 2 + 2 + 2 = ≥9
  model requests, plus one per guard correction. The limit is 30 requests per message
  (`PLANNER_REQUEST_LIMIT`; the legacy assistant keeps 15, see §9 item 18).
- **Legacy, bot**:
  - the request: 1 assistant run (medium) + `plan_generate` 1–2 attempts (large);
  - each correction: 1 assistant run (typed edits, or `rewrite_plan` → `plan_revise`
    (medium) with up to one escalation (large));
  - save: a button or an assistant run.
  - Roughly 5–10 runs, depending on how the assistant routes each correction.

Prompt size (`wc -w`):

| Path | Prompts | Words |
|---|---|---|
| Planner | `planner.v2.md` | 1822 |
| Legacy | `plan_generate.v8.md` + `assistant.v12.md` + `plan_revise.v5.md` | 2361 + 1890 + 1140 = 5391 |

The rendered input (context, state, history) is added to both. Its size was not measured.

## 11. How to test through the Telegram bot

**Before you start**

1. Stop the instance running from `main`. Two long-polling processes with one bot token
   conflict.
2. In the worktree (or a checkout of the branch), copy `.env` from the main checkout. Point
   `FITME_DB_PATH` at a **copy** of the database (`fitme backup --out DIR`, or copy
   `fitme.db` while the bot is stopped). This is the safe default: §9 item 1 says which
   plans `main` could not read afterwards.
3. Run `make migrate` (a no-op: the branch adds no migration), then `make serve`.
4. Optional: set `FITME_LLM_AGENT_PLANNER` to pick the planner's model.

**Scenario script**

Send each message as plain text; Russian first, then English. Start each scenario with
`/plan` → **New plan** / **Новый план**. With no plans yet the bot asks "What should the plan
be?" (`plan.new_first_prompt`); with plans it asks "You already have: … What should the new
plan be?" (`plan.new_guidance_prompt`). Answer with the first message.

| Step | Russian | English | Expect |
|---|---|---|---|
| A | Давай разок в неделю по субботам легкую беговую на свежем воздухе. Мне надо понять как разминаться, как бежать | Let's do an easy outdoor run once a week, on Saturdays. I need to learn how to warm up and how to run. | A plain-language proposal (warm-up, run or run/walk, cool-down) ending with an offer to put it into a plan. No buttons, nothing saved. |
| A (agree) | Выглядит хорошо | Looks good | The whole draft "Proposed plan (draft):" with "Where: Outdoors / park", Saturday, no kg, and **Подтвердить / Изменить что-то / Отмена** (**Confirm / Change something / Cancel**). |
| B | Убери скакалку | Remove the jump rope, I won't take one on a run. | (If the draft has one.) A new whole draft. Check that nothing needing equipment replaced the rope. |
| C | Зачем мне брать гантели на пробежку? Убери их | Why would I bring dumbbells on a run? Take them out. | (If the draft has them.) A new draft with no dumbbells, barbells, kettlebells or machines. |
| H | Выглядит хорошо. Сохрани. | Looks good. Save it. | "Plan … saved as version 1", with no Confirm button under the plan. Alternatively, tap **Confirm** on the last draft. |
| H (train) | `/train` on Saturday, or pick the plan | `/train` | **Start this workout** → the safety precheck ("Since your last session, have you had pain…?", answer **No**) → **▶ Start** → blocks one by one with **✅ Done as planned** / **⏭ Skip** / **⚠ Pain / feeling unwell**. Timed items log seconds and no kg. |
| D | Хочу бегать около часа | I want to run for about an hour | A question (an hour of running, or an hour-long session?) or a stated interpretation. Then answer "Ок, давай" / "Ok, do that." and expect a draft with a run and no strength work. The Russian line is not in the fixtures. |
| E | bjj два раза в неделю | BJJ twice a week | At most one short question (put the classes in the plan, or build sessions around them?). |
| E (answer) | просто поставь две тренировки, пн и чт | Just put in the two classes, Monday and Thursday | A draft with two "BJJ class" items (Mon, Thu), no gym sessions. Then "Сохрани." / "Save it.". With an area flag "yes" in `/profile`, the bot should say it cannot include the activity. |
| F | Сделай трёхдневную программу в зале на силу и массу. Используй мои привычные рабочие веса. | Make me a three-day gym strength program. Use my familiar working weights. | A proposal, then after "Хорошо, давай" / "Good, go ahead." a draft. Loads above your logged max plus one increment show a ⚠️ note. |
| G | Замени жим штанги лёжа на жим гантелей лёжа | Replace barbell bench press with dumbbell bench press | A draft titled "Changes to …" (if saved) or a new draft. Only the bench changes, and the dumbbell press has no carried-over barbell kg. |
| H | Сохрани. | Save it. | Saved. `/train` shows the exercises with **Done as planned** and set logging as before. |

**Other entry points**

- **Revise** / **Изменить** on a saved plan (`/plan` → tap a plan) and **Change something** on
  a draft both reply "what should change?". The next text goes to the planner, which sees
  that plan or draft.
- `/cancel` closes an open planning conversation.

**Planner or legacy?**

- `/system` shows calls and cost **per model**, not per agent. The planner runs on the large
  tier, while the legacy assistant runs on medium.
- For the agent, read the database copy:
  `sqlite3 <copy>.db "SELECT id, purpose, model, input_tokens, output_tokens, latency_ms FROM llm_calls ORDER BY id DESC LIMIT 10"`.
  The planner's rows say `purpose = planner`. The planner's drafts are `decisions` with
  `prompt_template = 'planner'`.

**Legacy, for comparison**

- `/plan` → **New plan** → **Generate anyway** / **Сгенерировать без пожеланий** runs the
  legacy generator from the profile.
- The website's new-plan form runs the legacy generator with your text.

**Afterwards**

Run the live eval (§8) and read its report in `docs/reports/llm_first_eval_<timestamp>.md`.
Read the transcripts, not only the table: the checks say whether the plan contained the
right kinds of things, and the transcripts say whether the answer made sense.

## 12. Acceptance criteria (§17) checklist

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 1 | Simple requests → simple, relevant recommendations | met by plumbing only, pending live eval | Prompt rules ("Understand first", "Propose in plain language"). No code shapes the content. Scripted A/D only. |
| 2 | Running requests do not become gym programs | met by plumbing only, pending live eval | No brief, pool or profile focus forces strength. `exclude_kinds`/`no_equipment` checks exist in `a_*`/`d_hour_run`. Not run live. |
| 3 | BJJ not auto-interpreted as strength | met by plumbing only, pending live eval | An activity item represents a class (`test_bjj_twice_a_week_…`). The question is in the prompt. Not run live. |
| 4 | Conversational edits preserve intent | met by plumbing only, pending live eval | Draft and history reach the next turn (`test_a_change_sees_the_draft_…`). Re-staging is whole-plan. Fidelity is untested live. |
| 5 | Activities not in the catalog can be recommended | met | `activity:` items staged, saved, trained and logged (`test_bjj_twice_a_week_…`, `tests/integration/db/test_activity_plans.py`). Refused with an area flag "yes" (by design). |
| 6 | Persistence does not distort the agreed workout | met (for the staged plan) | `confirm_plan` saves the staged plan unchanged except guard load substitution and measure/intensity repair: measure repair and an rir drop are reported; a default rir on a calibration load is added silently (`assert bodies == [staged]` in the A and E tests). Whether the staged plan matches the prose proposal is for the live eval. |
| 7 | Existing Telegram buttons remain useful | met | Confirm / Change something / Cancel, Revise, New plan, `/train` buttons, set logging, all tested in `test_planner_flow.py`. |
| 8 | Strength still supports history and set logging | met | Load engine and ceiling on staged plans (`test_a_load_above_the_guards_…`), `history_loads` check in `f_g_strength`, unchanged `/train`. |
| 9 | History used without fabrication | met by plumbing only, pending live eval | Guards cap any kg at logged max plus one increment. `calibration` without history (`test_a_barbell_to_dumbbell_swap_…`). The model's use of history is untested live. |
| 10 | No new sports-science subsystems | met | The branch adds no rule engine. The new guard is a fail-closed activity verdict. Content layers are bypassed, not added. |
| 11 | Experiment isolated from legacy | met at branch level, partly within the branch | Separate branch, nothing deleted, `AgentScope` keeps legacy output as before. But shared domain, guards and bot handlers changed, there is no flag, and `main` cannot read a branch plan that uses an activity item or a plan location (§9 item 1). |
| 12 | Existing saved workouts remain compatible | met, except plans using the new features | `test_a_plan_body_stored_before_activities_and_location_still_parses`: old bodies parse on the branch. `test_storage_body.py`: branch bodies of plans without activity items or a location parse with `main`'s models and bounds; plans with any of them must be deleted before switching back, and their activity set logs stay (§9 item 1). |
| 13 | Substantially simpler to reason about | met for the per-message path, not for the codebase | About 265 planner-only lines plus an 85-line prompt replace about 2130 generation-only lines on that path (392 more lines are shared display code). The codebase grew by +2495 lines in `src/` because legacy is kept (§10). |
| 14 | Scenarios exercisable end to end | met (scripted); live run pending | A and E run from message to saved plan to `/train` set logs in `test_planner_flow.py`. All six fixtures replay in both modes in `test_eval_conversations.py`. The live harness exists (`make llm-eval-conversations`). |

Plans on this path, like every plan in FitMe, are AI-generated and checked by deterministic
safety rules. This report makes no claim about training outcomes.
