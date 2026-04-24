# syntax=docker/dockerfile:1.6
#
# feedgate-fetcher container image.
#
# Single-stage on top of the official `uv` slim Python 3.12 image:
# `uv sync` is split into a deps-only pass and a project pass so that
# pyproject.toml/uv.lock changes invalidate only one cache layer.
#
# The same image is reused by every service in docker-compose.yml
# (migrate / api / worker). Behavior is selected entirely by env vars
# (`FEEDGATE_SCHEDULER_ENABLED`, `FEEDGATE_RETENTION_ENABLED`).

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# 1) Dependency manifests first → cached layer reused as long as
#    pyproject.toml / uv.lock are unchanged.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# 2) Project source.
COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./

# 3) Install the project itself into the venv.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# 4) Drop root — run as a non-privileged user.
RUN groupadd --gid 1001 appgroup \
    && useradd --uid 1001 --gid appgroup --no-create-home appuser \
    && chown -R appuser:appgroup /app
USER appuser

EXPOSE 8000

# Default command runs the API server. The worker service overrides
# nothing — it uses the same uvicorn entry point but flips
# `FEEDGATE_SCHEDULER_ENABLED=true` so the lifespan starts the
# background scheduler task.
CMD ["uv", "run", "--no-sync", "uvicorn", "feedgate_fetcher.main:create_app", \
     "--factory", "--host", "0.0.0.0", "--port", "8000"]
