#!/usr/bin/env bash
# Claude Code PostToolUse hook: after an Edit/Write to a Python file in this repo, apply
# ruff's safe autofixes and format that one file. Never blocks; `make precommit` is the gate.
#
# F401 (unused import) is left alone here: an agent often adds an import in one edit and its
# first use in the next, and autofixing in between would delete the import.
set -u
file=$(jq -r '.tool_input.file_path // empty')
root=${CLAUDE_PROJECT_DIR:-$(pwd)}
case "$file" in
  "$root"/*.py) ;;
  *) exit 0 ;;
esac
[ -f "$file" ] || exit 0
cd "$root" || exit 0
uv run --quiet ruff check --fix --unfixable F401 --exit-zero --quiet "$file" >/dev/null 2>&1
uv run --quiet ruff format --quiet "$file" >/dev/null 2>&1
exit 0
