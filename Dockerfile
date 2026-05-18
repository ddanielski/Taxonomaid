# syntax=docker/dockerfile:1.7
#
# Multi-stage build:
#   1. `builder`  - install dependencies into a virtualenv with `uv`.
#   2. `runtime`  - slim image with the venv on PATH and an unprivileged user.
#
# Build:  docker build -t taxonomaid:latest .
# Run:    see `compose.yaml` for the canonical bind-mount layout.

ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_NO_INSTALLER_METADATA=1 \
    UV_PYTHON_DOWNLOADS=never

# Pin uv to a specific patch so the build is reproducible. Bump the
# tag deliberately when you've validated the new version locally.
COPY --from=ghcr.io/astral-sh/uv:0.11.14 /uv /uvx /usr/local/bin/

WORKDIR /app

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:${PATH}" \
    TAXONOMAID_CONFIG_DIR=/config \
    TAXONOMAID_DATA_DIR=/data

WORKDIR /app

COPY --from=builder /app /app

RUN useradd --system --create-home --uid 1000 taxonomaid \
 && mkdir -p /config /data \
 && chown -R taxonomaid:taxonomaid /app /config /data

USER taxonomaid

VOLUME ["/config", "/data"]

# `taxonomaid health` issues GET /models against the LLM endpoint and
# GET /getMe against Telegram - both cheap and authenticated. A
# five-minute cadence is plenty for a long-running daemon and won't
# bump into Telegram's rate limit.
HEALTHCHECK --interval=5m --timeout=15s --start-period=20s --retries=3 \
    CMD ["taxonomaid", "health"]

ENTRYPOINT ["taxonomaid"]
CMD ["run", "--json-logs"]
