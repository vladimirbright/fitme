# ADR 0003 — Free-text assistant with read-only tools

Status: accepted (2026-10-05); edits-apply-immediately superseded by ADR 0004

## Context

Free text only reached the LLM after a button asked for it: "Change something" (plan
revision), "Adjust" (today's workout), "Enter results". Any other message got "Use the menu,
or send /help." Simple edits ("squat 62.5 from now on", "move B to Thursday", "last set was
only 6 reps") needed several taps, or the website. The owner found the bot too restrictive.

ADR 0001 said to revisit the harness if the project needed multi-step tool use. This ADR is
that revisit.

## Decision

- **One new agent, `assistant`** (pydantic-ai, medium tier), for messages that no command,
  pending prompt or setup step claimed. It answers with exactly one of `AssistantEdits` (typed
  edit ops), `AssistantAction` (open an existing flow), `AssistantReply` (a short answer or a
  clarifying question) or `Refusal`.
- **Tools are read-only**: `list_plans`, `get_plan`, `list_recent_sessions`, `get_session`,
  `find_exercises`. They return pseudonymized data (ids, numbers, catalog names, scrubbed plan
  names). There is no write tool. The model's edits come back as its typed output, and
  deterministic code applies them (`services.assistant.apply_plan_ops`). The model never
  writes to the database.
- **Edits apply immediately**, with an **Undo** button, because the owner chose that over
  Confirm/Cancel. They go through the website editor's guards (`plan_edit.save_edit`) and are
  saved as a `user_edit` version. The decision records `source = "assistant"`, the ops, and
  the prompt and model that interpreted them (AGENTS.md §6). Logged-set corrections go through
  the plausibility guard (`services.log_edit`).
- **There is no override in chat.** On the website, the weekly cap and the historical-max
  ceiling are overridable warnings behind an explicit checkbox. In chat they refuse, and the
  owner is pointed to the website editor. A model interpreting a message is not the same as
  the owner ticking a box.
- **A whole-plan redesign stays a draft.** `revise_plan` opens the existing `plan_revise` flow
  with Confirm/Change/Cancel, because there the model designs and the owner confirms.
- **Same safety order as all free text** (A§6.3): saved to `chat_messages`, then the stop-word
  scan, then the gate (open hold, screening), and only then the model. Administrative actions
  (export, delete, activation, profile answers) aren't done here: the reply points to the
  command.
- `FITME_ASSISTANT_ENABLED=false` turns the assistant off and brings back the menu hint.
- **Refuse narrowly** (prompt v2, after the first days of use): a refusal is only for medical,
  symptom, nutrition and bodyweight requests. Anything else the assistant can't do gets a
  reply saying where it is done (`/profile`, `/export`, "Paste my plan", ...). Profile answers
  (days per week, session length, focus) are defaults, not limits. Every assistant refusal is
  logged as a `decision(kind=refusal)` with its prompt and model.
- **Workout blocks** (prompt v3): with a started workout, `state.current_block` is sent and
  three actions mirror the block's buttons (log as planned, enter results, skip). Results are
  never paraphrased by the assistant: the user's message itself goes to `result_parse`.

## Consequences

- Still pydantic-ai: tools plus union output types are enough. LangGraph is not needed.
- Guards stay outside the harness. The new paths reuse
  `plan_edit.blocking_errors`/`load_cap_warnings` and `guards.plausibility`. One guard was
  relaxed alongside: `plan.schedule_length` used to require exactly the profile's days per
  week, which made "two workouts a week" impossible without editing the profile. It now
  only requires at least one day; the profile's frequency is the default a plan is
  generated with.
- Every unclaimed message now costs one LLM call (up to `REQUEST_LIMIT` model requests with
  lookups). It is recorded in `llm_calls` and visible in `/system`.
- This is not a companion persona (AGENTS.md §7): the prompt forbids small talk, and replies
  are short, task-bound and marked as AI-generated.
