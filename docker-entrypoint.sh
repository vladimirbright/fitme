#!/bin/sh
# Runs pending migrations, then replaces this shell process with `fitme serve` via `exec` —
# no lingering shell wrapper left behind for it. compose.yaml's `init: true` (see its own
# comment, and Dockerfile's) forwards SIGTERM/SIGINT to this script's process; the `exec`
# below means that same process *is* `fitme serve`, so the signal reaches it in one hop, and
# its own signal handling (fitme/cli/commands.py::serve_async) does the rest.
#
# `db upgrade` is idempotent (A§4.7) and `fitme serve` itself refuses to start with any
# migration still pending, so running it here on every container start is safe and cheap.
set -eu

fitme db upgrade
exec fitme serve
