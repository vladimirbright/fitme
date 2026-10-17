#!/usr/bin/env bash
# Claude Code PostToolUse hook: after an Edit/Write to a Python file in this repo, apply
# ruff's safe autofixes and format that one file. Never blocks; `make precommit` is the gate.
#
# Worktree-aware: the file may live in any worktree of this repository (under
# .claude/worktrees/ or elsewhere), and ruff runs from that worktree's root so it uses that
# checkout's pyproject.toml and environment rather than the session's starting directory.
#
# F401 (unused import) is left alone here: an agent often adds an import in one edit and its
# first use in the next, and autofixing in between would delete the import.
set -u
file=$(jq -r '.tool_input.file_path // empty')
case "$file" in
  *.py) ;;
  *) exit 0 ;;
esac
[ -f "$file" ] || exit 0
top=$(git -C "$(dirname "$file")" rev-parse --show-toplevel 2>/dev/null) || exit 0
# Same repository as the session (its main checkout or any of its worktrees)?
root=${CLAUDE_PROJECT_DIR:-$(pwd)}
repo_common=$(git -C "$root" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || exit 0
file_common=$(git -C "$top" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || exit 0
[ "$repo_common" = "$file_common" ] || exit 0
cd "$top" || exit 0
uv run --quiet ruff check --fix --unfixable F401 --exit-zero --quiet "$file" >/dev/null 2>&1
uv run --quiet ruff format --quiet "$file" >/dev/null 2>&1
exit 0
