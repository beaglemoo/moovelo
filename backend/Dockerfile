# syntax=docker/dockerfile:1
# Build context is the repo root (the prod stage bundles the frontend build).

# The frontend build output is platform-independent static files, so build
# it once on the build host's native platform instead of once per target
# arch (under emulation, where esbuild's binary is unreliable).
FROM --platform=$BUILDPLATFORM node:22-slim AS frontend-build
WORKDIR /fe
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim AS base
COPY --from=ghcr.io/astral-sh/uv:0.12.1 /uv /uvx /bin/
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"
WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/app ./app
COPY backend/alembic ./alembic
COPY backend/alembic.ini backend/entrypoint.sh ./
RUN chmod +x /app/entrypoint.sh
ENTRYPOINT ["/app/entrypoint.sh"]

FROM base AS dev
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "17777", "--reload"]

FROM base AS prod
COPY --from=frontend-build /fe/build /app/static
# Prod only: dev keeps running as root so the bind-mounted host source
# (owned by the developer's UID, not baked into the image) stays writable
# for hot reload. Everything the prod image needs - app code, alembic,
# static build, the /opt/venv site-packages - was COPY'd/built as root in
# the base stage, but nothing at runtime writes to the filesystem (no
# volumes are mounted onto this service - Valhalla is reached over HTTP
# via VALHALLA_URL, not a shared volume - and there's no tempfile/cache
# writing in app code), so a non-root, read-only-capable user is a plain
# permission drop with nothing to chown.
RUN groupadd --gid 10001 moovelo \
    && useradd --uid 10001 --gid moovelo --no-create-home --shell /usr/sbin/nologin moovelo
USER moovelo
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "17777"]
