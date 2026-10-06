FROM node:24.16.0-bookworm-slim@sha256:2c87ef9bd3c6a3bd4b472b4bec2ce9d16354b0c574f736c476489d09f560a203 AS frontend
WORKDIR /build
COPY frontend/package*.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.13.15-slim-bookworm@sha256:ed86c82274b3c69b52fb5820f358f0bd7df0b603332063cb5c6e32bd220c3e6e
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/backend PATH=/app/.venv/bin:$PATH
# PostgreSQL 18 clients ensure backup tools match the database major version.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl --fail --silent --show-error https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    && echo 'deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main' > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update && apt-get install -y --no-install-recommends postgresql-client-18 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
RUN pip install --no-cache-dir uv==0.12.10 && groupadd -g 10001 iirp && useradd -u 10001 -g iirp -m iirp
COPY pyproject.toml uv.lock ./
# Dependencies before code, so code-only changes reuse this layer. The BuildKit
# cache keeps downloaded wheels across lockfile changes (PyPI can be slow);
# installs remain pinned by uv.lock hashes and the cache never enters the image.
ENV UV_LINK_MODE=copy
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-editable --no-install-project
COPY backend/ backend/
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-editable
COPY migrations/ migrations/
COPY alembic.ini ./
COPY scripts/ scripts/
COPY config/ config/
COPY --from=frontend /build/dist frontend/dist
RUN mkdir -p /app/runtime && chown -R 10001:10001 /app/runtime
USER 10001:10001
EXPOSE 18081
CMD ["python", "-m", "uvicorn", "iirp.api.app:app", "--host", "0.0.0.0", "--port", "18081"]
