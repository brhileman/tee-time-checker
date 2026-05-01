# syntax=docker/dockerfile:1
#
# Single-stage build using uv for fast, reproducible installs.
# The image is small enough on python:3.12-slim that a multi-stage
# build wouldn't pay off — total compressed size is ~150MB.

FROM python:3.12-slim

# Pin uv to the same major version we develop against so builds are
# reproducible. Bump deliberately when ready.
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /usr/local/bin/

# uv compiles bytecode at install time when this is set, which buys a
# little startup latency. Disable Python output buffering so logs hit
# Fly's log stream as soon as they're emitted (otherwise prints can
# vanish into the buffer for minutes when traffic is low).
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# ──────────────────────────────────────────────────────────────────────
# Layer 1: dependencies. Copying just the lockfiles keeps Docker's layer
# cache valid across code changes — only edits to pyproject.toml/uv.lock
# bust this layer.
# ──────────────────────────────────────────────────────────────────────
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# ──────────────────────────────────────────────────────────────────────
# Layer 2: project code. Edit-and-redeploy cycles only rebuild this.
# ──────────────────────────────────────────────────────────────────────
COPY tee_time_checker ./tee_time_checker
COPY courses.toml ./

RUN uv sync --frozen --no-dev

# ──────────────────────────────────────────────────────────────────────
# Runtime config
# ──────────────────────────────────────────────────────────────────────
# State lives on a persistent volume mounted at /data so watches survive
# deploys. fly.toml mounts the volume; this var tells our state.py
# where to put the SQLite file.
ENV TEE_TIME_DB_PATH=/data/tee_time_checker.db

# Fly's HTTP service forwards 443→8080 by default; matches fly.toml.
EXPOSE 8080

# Use the venv binary directly to skip the `uv run` shim (saves ~50ms
# at startup and avoids uv's resolution check on every container start).
CMD ["/app/.venv/bin/tt", "server", "--host", "0.0.0.0", "--port", "8080"]
