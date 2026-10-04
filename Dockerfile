# One image, three commands (pick per service on the host):
#   API (default):  uvicorn on $PORT
#   worker:         arq app.workers.worker.WorkerSettings
#   migrations:     alembic upgrade head   (run once per deploy as a release command,
#                   not on every start: two replicas booting together would race)
#
#   docker build -t outreach-agent .
#   docker run --env-file .env -p 8000:8000 outreach-agent
#   docker run --env-file .env outreach-agent arq app.workers.worker.WorkerSettings
#
# Config comes only from env vars (see .env.example); no secrets are baked in.
# The API reads ./sender.json if it was present in the build context.

# --- build: resolve dependencies from the lockfile with uv ---
FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.21 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /app
# Dependencies first, in their own layer: code changes don't reinstall them.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev --no-install-project
COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

# --- runtime: no uv, no build cache, non-root ---
FROM python:3.12-slim
RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY --from=builder --chown=app:app /app /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    # The host's proxy is the only way in, so trust its X-Forwarded-For; the per-client
    # rate limit needs the real client IP, not the proxy's. Narrow this if the container
    # is ever reachable directly.
    FORWARDED_ALLOW_IPS="*"
USER app
EXPOSE 8000
# sh -c so $PORT (set by Railway/Render/Fly) expands; exec so uvicorn gets SIGTERM.
CMD ["sh", "-c", "exec uvicorn app.api.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers"]
