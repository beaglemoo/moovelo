# Contributing to Moovelo

Thanks for taking an interest. Moovelo is a small, deliberately scoped
project - a self-hosted route planner with Wahoo sync - so contributions
that fit that scope are the most likely to land.

## Development setup

Everything runs through Docker Compose:

```sh
cp .env.example .env
docker compose --profile dev up
```

Open http://localhost:5173. Backend and frontend source are bind-mounted
with hot reload. The first start downloads OSM data and builds routing
tiles; point `VALHALLA_TILE_URL` at a small region (see `.env.example`)
for a build that takes minutes instead of an hour.

## Tests and linters

Backend (Python 3.12, [uv](https://docs.astral.sh/uv/)):

```sh
cd backend
uv sync
uv run pytest          # DB tests need the dev compose stack (Postgres on 5433)
uv run ruff check .
uv run mypy
```

Frontend (TypeScript, npm):

```sh
cd frontend
npm install
npm run check
npm run lint
npm run build
```

All of the above must pass; CI runs the same commands. If you change the
GPX/FIT encoders, regenerate the golden files with
`uv run python -m tests.golden.regen` and eyeball the diff.

## Pull requests

- Branch from `main`, use conventional commit messages
  (`feat:`, `fix:`, `chore:`, `docs:`, `test:`, `refactor:`).
- Keep commits atomic - one logical change per commit.
- New behavior needs a test; bug fixes need a regression test.
- External services (Valhalla, Wahoo) are mocked in tests - never call
  them for real from the test suite.
- Update the docs (`README.md`, `docs/`) when behavior or configuration
  changes.

## Scope

Features outside the core loop (plan, save, export, push to Wahoo) -
social features, ride recording, other head-unit integrations, mobile
apps - are out of scope for now. Open an issue to discuss before building
anything sizable.
