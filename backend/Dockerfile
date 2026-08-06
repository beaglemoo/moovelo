# syntax=docker/dockerfile:1
# Build context is the repo root (the prod stage bundles the frontend build).

FROM node:22-slim AS frontend-build
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

FROM base AS dev
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "17777", "--reload"]

FROM base AS prod
COPY --from=frontend-build /fe/build /app/static
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "17777"]
