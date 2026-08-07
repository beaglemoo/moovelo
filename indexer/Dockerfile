# The indexer is a one-shot batch job on its own compose profile, so it
# gets its own image rather than adding osmium-tool and pyosmium to the
# backend image that runs all the time.
FROM python:3.12-slim

# osmium-tool is the C++ streaming filter that shrinks the extract before
# Python sees it. pyosmium ships manylinux wheels for amd64 and arm64, so
# nothing here needs a compiler.
RUN apt-get update \
    && apt-get install -y --no-install-recommends osmium-tool \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.12.1 /uv /usr/local/bin/uv

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY indexer/pyproject.toml indexer/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY indexer/indexer ./indexer

CMD ["python", "-m", "indexer.build"]
