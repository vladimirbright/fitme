# ADR 0013 — No ✏️ "Enter results" button; the assistant is always on

Status: accepted (2026-10-16). Amends A§6.5 step 5, A§6.5.1, A§8.1, ADR 0003 and ADR 0005.

## Context

Since ADR 0005 the free-text assistant logs a typed report ("8, 8, 6 at 60", "по плану",
"skip") one exercise at a time through `services.training.log_items`, with the same
coverage and plausibility checks a confirmed parse needed. The ✏️ **Enter results** button
kept a second path for the same job: a pending prompt, the `result_parse` agent, a parsed
table and a **Correct** / **Fix** step. The owner finds that flow unnecessary: writing the
results as a message already does it, in fewer taps. With the assistant the only logging
path, the `FITME_ASSISTANT_ENABLED` switch would only turn logging off, so it goes too.

## Decision

- The block keyboard is **✅ Done as planned**, **⏭ Skip**, **⚠ Pain / feeling unwell**
  (plus the YouTube links). ✅ is still hidden on an all-calibration block. The hint under a
  block says to write other results as a message.
- Removed: the `enter` / `parse_ok` / `parse_fix` callback actions, the results kind of the
  pending `/train` prompt (only an adjustment request can be pending now),
  `services.training.parse_results` / `confirm_results` / `next_result_prompt` and their
  types, the `result_parse` agent (factory, tier, settings, retries, its user-prompt fields)
  and `prompts/result_parse.v1–v2.md`, `domain.results.ParsedResults`, and the locale keys
  only that flow used.
- Removed: the `assistant_enabled` setting (`FITME_ASSISTANT_ENABLED`). Unclaimed free text
  always goes to the assistant; only blank text, or text during setup, gets the menu hint.
  When the provider fails, the assistant's existing `LLM_UNAVAILABLE` refusal is shown.
- Kept: `log_items`, `check_item_logs`, the plausibility guard, `SetResult`,
  `MAX_EXTRA_SETS`, and the `result_parse` decision kind, which the assistant writes with
  `event=assistant_log`. No migration: the kind stays in the CHECK constraint, and old
  `result_parse` rows (`event=parse`) stay in the log as they are.

## Consequences

- Results that differ from the plan are logged only by writing them as a message, so
  logging them needs a working LLM provider. ✅ and ⏭ work without one.
- `TrainAction` keeps its `item` / `decision_id` fields (the recap's **Apply** uses them).
  A ✏️, **Correct** or **Fix** button on an old message still unpacks and gets the stale
  toast; nothing is written.
- `FITME_ASSISTANT_ENABLED` left in an environment is ignored: settings read only the
  env vars they declare.
