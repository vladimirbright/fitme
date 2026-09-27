# ADR 0002 — Single-user instance

Status: accepted (2026-09-27)

## Context

The first product sketch had invite-only registration, a public waiting list (bot and
website), a marketing page, and an admin role that approved invites and deactivated or
deleted accounts. Under AGENTS.md §1 and §8, each of these changes the project's posture:
a second person's data on the operator's infrastructure, onboarding people the operator
does not know, and marketing. Changing the posture brings GDPR controller duties and
product-liability exposure.

## Decision

One instance serves one person, the operator.

- Access: `fitme activate` prints a one-time code, and `/activate <code>` binds exactly one
  Telegram account. The bot ignores every other account and stores nothing about it.
- No invites, waiting list, sign-up, marketing page or roles. The former admin commands
  collapse into operator tooling: `/system` in the bot, plus the `fitme` CLI (`export`,
  `delete`, `activate --rebind`).
- The schema keeps `user_id` foreign keys and a separate `telegram_accounts` link table
  (1:1). The data stays separated by concern (AGENTS.md §5) and export/delete stay trivial.
  The application enforces a single user row.
- Anyone else who wants Fitme runs their own instance. That is what open-sourcing it is for.

## Consequences

- Removed from the original spec: `/invites`, `/approve`, `/deactivate`, the admin
  `/delete`, the waiting list, the invite form and the marketing page.
- Web login needs no nickname input: the code always goes to the bound account.
- Reversing this decision requires updating AGENTS.md §1 first, plus the legal setup it
  describes.
