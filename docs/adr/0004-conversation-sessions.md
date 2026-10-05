# ADR 0004 — Conversation sessions and a tool-using assistant

Status: accepted (2026-10-05). Supersedes the "edits apply immediately" part of ADR 0003.

## Context

The ADR 0003 assistant answered each message on its own, with no memory. A clarifying
question was lost: the answer arrived as a new message without context. It could also take
only one action per message. Compared with talking to an agent, the owner found it
restrictive.

AGENTS.md §7 asks for a plan-first interface with a small dialog for adjustments, not a
chat-first companion. Memory should therefore belong to a piece of work, not to an endless
chat.

## Decision

**Sessions** (`conversations` table, migration 0008; `services/conversations.py`):

- **Planning.** A new plan, or a change to one existing plan. Every step is a draft round:
  the same `plan_generate`/`plan_revise` decisions `/plan` uses. Edits made through the
  assistant are judged like any draft (`planning.record_edit_draft`). The session tracks its
  current draft. It ends on **Save** (`planning.confirm_plan`, a new plan version), on
  **Close without saving**, or after 12 hours idle. Any entry point opens one: the buttons,
  the assistant's tools, "carry today's changes into the plan". A message about a plan sent
  outside a session opens one too.
- **Training.** From Start until the workout is completed, aborted or halted. The assistant
  sees the current block and the ones after it. It can log the block, send the owner's own
  words to the result parser, skip, or change the blocks not yet started, for today only
  (`training.edit_remaining`: judged like Start, only the changed loads counted). At the end
  the bot offers to carry those changes into the plan, as a planning-session draft.
- At most one session is active. A training session wins.
- **Memory:** the session's last 20 messages in both directions. The bot's replies are now
  stored too (`chat_messages.direction = 'out'`, linked by `conversation_id`), with the same
  365-day retention. Closed sessions go with their messages, and sessions are included in
  export and delete.

**Tools.** The assistant has read-only lookups and *staging* tools: `edit_plan`,
`rewrite_plan`, `new_plan`, `save_draft`, `discard_draft`, `rename_plan`, `set_default_plan`,
`fix_logged_sets`, `log_current_block`, `enter_current_block_results`, `edit_today`, `show`.

- It may call any number of them in one turn.
- A staging tool checks the action right away (the op applies, the guards judge it) and
  answers the model, so the model can correct itself within the turn.
- Nothing is written until the run ends. Then deterministic code applies what was staged,
  through the same guards as the buttons.
- The final output is only what the assistant says (`AssistantTurn`), or a `Refusal`.

## Consequences

- A plan change through the assistant is now a draft. The owner sees a diff and saves it.
  Loads above the weekly cap or the ceiling are limited to what the guards allow, and the
  owner is told. Nothing is saved as asked. The website editor's explicit checkbox is still
  the only override.
- The model never writes. A refused or failed run applies nothing.
- AGENTS.md §7: memory is scoped to a plan or a workout and ends with it. There is no
  persona and no small talk. Replies are marked as AI-generated.
- More model requests per message (up to 15, including tool calls). They are recorded in
  `llm_calls` and visible in `/system`.
