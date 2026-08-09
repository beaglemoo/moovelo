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

There is also a Playwright smoke test (`npm run e2e`) covering
plan -> save -> export GPX. It runs against the dev compose stack, needs
built Valhalla tiles, and registers a throwaway account - so it wants a
fresh database or `SIGNUPS_ENABLED=true`. It is not part of CI.

## Pull requests

- Branch from `main`, use conventional commit messages
  (`feat:`, `fix:`, `chore:`, `docs:`, `test:`, `refactor:`).
- Keep commits atomic - one logical change per commit.
- New behavior needs a test; bug fixes need a regression test.
- External services (Valhalla, Wahoo, the weather provider) are mocked
  in tests - never call them for real from the test suite. Mock fixtures
  should be captured from real responses, not written from documentation.
- Update the docs (`README.md`, `docs/`) when behavior or configuration
  changes.

## Scope

Moovelo has grown beyond its original core loop (plan, save, export,
push to Wahoo): it now also imports existing files with recovered turn
cues, searches places and finds POIs entirely offline, analyses routes
(surface mix, gradients, climbs, per-rider time, optional wind), and
offers planning tools built on those primitives - costing sliders,
isochrones, a loop generator, undo/redo, alternates and avoids.
Contributions that deepen those areas are welcome.

It also has an optional AI route assistant, off unless an endpoint is
configured. If you work on it, the rule that matters is that the model
never handles coordinates: tools return opaque handles and the schemas
will not carry a latitude, so a change that lets the model supply one -
or lets a tool write to the database - removes the guarantee the whole
design rests on. Place names from OpenStreetMap are untrusted input in
prompts, never instructions. See the Route assistant section of
`docs/architecture.md` before starting.

Permanently out of scope: social features (comments, likes, feeds),
photos, live ride recording / GPS tracking, native mobile apps, and
i18n. Anything that would make the app call out to a third-party
service by default is also out - external integrations must be opt-in
via configuration and off otherwise. Open an issue to discuss before
building anything sizable.
