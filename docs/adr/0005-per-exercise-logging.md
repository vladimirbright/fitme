# ADR 0005 — Per-exercise logging from free text

Status: accepted (2026-10-15). Supersedes the `log_current_block` /
`enter_current_block_results` tools of ADR 0004.

## Context

During a workout the assistant could only act on a whole block: ✅ for every exercise in it,
or hand the user's own message to `result_parse`, which then asked for **Correct** / **Fix**.
A superset could not be confirmed one exercise at a time from text, and every typed result
needed one more tap. The ✅ button read "All sets at the top of the range", which the owner
found awkward for the common case.

## Decision

- The ✅ button reads **"Done as planned"**. What it logs does not change: every set at the
  top of the rep range with the prescribed load (A§6.5.1). The hint under every block says so.
- The assistant logs **one exercise of the current block per tool call**
  (`services.training.log_items`):
  - `log_exercise_as_planned`: like ✅. Refused for a calibration load, where the weight used
    is needed.
  - `skip_exercise`: like ⏭.
  - `log_exercise_results`: the user's numbers, one entry per prescribed set.
  - A superset is logged one exercise at a time; the block advances once all are logged.
    Only the block on screen can be logged.
- **No confirmation step.** In place of **Correct**, a staged log must pass, when staged and
  again in the write transaction, the checks a confirmed parse needs: the exercise is open in
  the current block, every prescribed set is named exactly once, a calibration load has a
  weight, and every load passes `guards.plausibility`. A failed check writes nothing; the tool
  answers why, and the model asks the user.
- Each logged exercise is an immutable `result_parse` decision (`event = assistant_log`) with
  the assistant's prompt version and model, the sets written and the plausibility verdicts,
  plus its outcome (AGENTS.md §6).
- **`report_safety_concern`** replaces `result_parse`'s `safety_signal` on this path: during a
  workout, the model reports pain, dizziness, numbness, chest discomfort, a pop, trouble
  breathing or fainting that the stop-word list missed. The workout halts
  (`llm_safety_signal`), the fixed halt message is shown, and nothing else from the turn is
  applied, even if the model also refused or staged logs. The stop-word scan still runs
  first, before any model call (A§7.2: the model can only add a halt).

## Consequences

- A typed result is logged in one message, without a tap.
- A misread number that still passes plausibility is stored without the user seeing it
  first. The reply shows exactly what was written; the user corrects it with another message
  or on the website. The ✏️ button keeps the Correct/Fix flow for those who want it.
- The assistant prompt carries the number-reading rules from `result_parse.v1` (decimal
  comma, per-implement loads, ask when ambiguous). Prompt version `assistant.v5`.
