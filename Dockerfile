# Reference deployment image (docs/IMPLEMENTATION_PLAN.md M10, docs/ARCHITECTURE.md §3).
# Multi-stage: the builder resolves and installs dependencies with uv; the runtime stage
# copies only the resulting virtualenv and the app, with no compiler and no package-manager
# cache left behind, running as a non-root user on what compose.yaml then makes a read-only
# root filesystem.
#
# PID 1 / signals: compose.yaml sets `init: true`, so Docker's own tiny init (`docker-init`,
# tini under the hood) is the container's real PID 1 and forwards SIGTERM/SIGINT to its child
# — this entrypoint script. The script's own last step, `exec fitme serve`, then replaces
# *that* child process with the Python process in place (same pid, no extra fork), so the
# signal tini forwards lands on `fitme serve` directly. See compose.yaml for why `init: true`
# is used instead of installing `tini` into the image.
#
# Every dependency in uv.lock installs from a musllinux (or pure-Python) wheel on this base —
# verified by building this image — so there is no need to fall back to `python:3.13-slim`
# (A§ M10 spec: "if one doesn't [install from a wheel], document it and switch that stage").

# Pinned by digest-adjacent tag (matches the `uv` version this repo was developed with;
# ghcr.io/astral-sh/uv images are published per uv release, immutable once tagged).
FROM ghcr.io/astral-sh/uv:0.7.19 AS uv

FROM python:3.13-alpine AS builder

COPY --from=uv /uv /uvx /usr/local/bin/

# UV_COMPILE_BYTECODE is deliberately left unset: pre-compiling .pyc for every dependency
# roughly doubles the installed size of pure-Python packages (source + bytecode both ship)
# for a startup-latency win this long-running server process doesn't need. Runtime bytecode
# caching is separately disabled below (PYTHONDONTWRITEBYTECODE=1), so nothing is written to
# the read-only root filesystem at import time either way.
ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Lockfile + project metadata first, so an edit to application code alone doesn't invalidate
# the (much slower) dependency-resolution/install layer.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --no-install-project

# Now the actual package. README/LICENSE are here only because hatchling's build (triggered
# by the second `uv sync`, which installs the project itself) reads them via pyproject.toml's
# `readme`/`license` fields; they end up in the wheel's metadata, not as extra runtime files.
COPY src ./src
COPY README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# Strip debug symbols from the compiled extension modules a few dependencies ship
# (pydantic-core, aiohttp, multidict, ...). `binutils` is a builder-only tool: this stage is
# discarded entirely, only `/app/.venv` is copied into the runtime stage below.
RUN apk add --no-cache binutils \
    && find /app/.venv -name '*.so' -exec strip --strip-unneeded {} +

FROM python:3.13-alpine AS runtime

# Fixed uid/gid 10001 (M10 spec). `-D`: no password; `-H`: no home directory to create (none
# is needed — HOME is pointed at the /tmp tmpfs below, since the root filesystem is read-only
# at runtime, compose.yaml `read_only: true`).
RUN addgroup -g 10001 fitme \
    && adduser -D -H -u 10001 -G fitme fitme \
    && mkdir -p /data \
    && chown fitme:fitme /data
# ^ /data is created (and chowned) here, in the image, purely so that when compose first
# creates the empty named volume `fitme-data:/data`, Docker seeds it from this image
# directory and the volume inherits fitme:fitme ownership immediately — no root step-up/
# entrypoint chown dance needed to let a non-root process write its database there.

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod 0755 /app/docker-entrypoint.sh

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYDANTIC_AI_NO_BANNER=1 \
    HOME=/tmp

USER fitme
EXPOSE 8080

ENTRYPOINT ["/app/docker-entrypoint.sh"]
