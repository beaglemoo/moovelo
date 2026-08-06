# Komoot-lite

Self-hosted bike route planner with direct Wahoo ELEMNT sync. Plan routes in
the browser on a cycling-oriented map, save them to your library, export
GPX/FIT, and push them straight to your Wahoo head unit.

Status: Phase 1 complete - route planning in the browser (Valhalla routing,
MapLibre map, elevation profiles, routing presets). Persistence, GPX/FIT
export, and Wahoo sync are the next phases.

## Features

- Click-to-plan routing: click the map to add waypoints, drag any waypoint
  marker to move it, or grab the route line itself and drag it to insert a
  via point exactly where you drop it (komoot-style).
- Three bicycle presets - road, gravel, quiet - implemented as Valhalla
  costing bundles with genuinely different routing behavior (see
  [Routing presets](#routing-presets)).
- Elevation profile under the map with total ascent/descent and a hover
  marker synced to the route.
- CyclOSM cycling basemap with an OSM standard fallback toggle, plus
  optional fully self-hosted tiles (see
  [docs/self-hosted-tiles.md](docs/self-hosted-tiles.md)).
- Entirely self-hosted: routing (Valhalla), app, and optionally the map
  tiles run on your own hardware. No accounts, no third-party APIs.

## Quickstart (development)

Requirements: Docker with Compose v2, roughly 8 GB RAM allocated to Docker,
and about 10 GB of disk for routing data.

```sh
cp .env.example .env
docker compose --profile dev up
```

Open http://localhost:5173.

First start: Valhalla downloads the configured OSM extract plus elevation
data and builds routing tiles into a named volume. Build time depends
heavily on hardware - minutes on a fast machine, up to an hour or two on
modest hardware for the default England extract. Subsequent starts reuse
the tiles and are fast.

For quick iteration, point `VALHALLA_TILE_URL` in `.env` at a small region
first (a single county builds in a couple of minutes - see `.env.example`).

## Quickstart (production)

```sh
cp .env.example .env   # adjust as needed
docker compose --profile prod up -d --build
```

The prod profile builds the frontend into the backend image and serves the
whole app from a single container on port 17777 (configurable via
`APP_PORT`). Put a reverse proxy with TLS in front of it - a Caddy snippet
is provided in [deploy/](deploy/).

## Configuration

All configuration is via environment variables, documented in
[.env.example](.env.example):

| Variable | Default | Purpose |
|----------|---------|---------|
| `VALHALLA_TILE_URL` | Geofabrik england | OSM extract(s) for routing, space-separated URLs |
| `VALHALLA_BUILD_ELEVATION` | `True` | Download elevation data (hill-aware routing + profiles) |
| `VALHALLA_SERVER_THREADS` | all cores | Valhalla build/serve threads |
| `TILE_URL_CYCLOSM` | unset | Self-hosted CyclOSM tile server template; unset uses the public servers |
| `APP_PORT` | `17777` | Host port for the prod profile |

## Routing presets

The three presets map to Valhalla bicycle costing-option bundles, chosen so
they produce visibly different routes (exact values and rationale in
[backend/app/services/presets.py](backend/app/services/presets.py)):

| Preset | Bike | Behavior |
|--------|------|----------|
| road | Road | Fast tarmac riding; comfortable on carriageways, hard-avoids unpaved surfaces |
| gravel | Cross | Seeks out unpaved tracks, towpaths, and bridleways; biased away from tarmac |
| quiet | Hybrid | Strongly prefers cycleways and calm streets; softens climbs |

The API accepts the preset name per request, and the option bundles are
plain dictionaries - any single option can become a user-facing slider
later without an API change.

## Map tiles

The map defaults to CyclOSM raster tiles from the public community servers,
with an OSM standard layer as an in-app fallback toggle (bottom-left of the
map). The public CyclOSM servers render uncached high-zoom tiles on demand,
which can take seconds per tile in less-visited areas.

For a fully self-hosted setup, run your own CyclOSM tile server and set
`TILE_URL_CYCLOSM` - the complete guide is in
[docs/self-hosted-tiles.md](docs/self-hosted-tiles.md).

## Documentation

- [docs/architecture.md](docs/architecture.md) - components, data flow, and design decisions
- [docs/data.md](docs/data.md) - OSM extracts, elevation data, refreshing routing data
- [docs/self-hosted-tiles.md](docs/self-hosted-tiles.md) - running your own CyclOSM tile server
- [deploy/](deploy/) - production reverse-proxy notes and Caddy snippet

## Development

Backend (Python 3.12, FastAPI, uv):

```sh
cd backend
uv sync
uv run pytest          # tests
uv run ruff check .    # lint
uv run mypy            # types (strict)
```

Frontend (SvelteKit, TypeScript, npm):

```sh
cd frontend
npm install
npm run check          # svelte-check
npm run lint           # prettier + eslint
npm run build          # production build
```

The dev compose profile runs both with hot reload (backend source and
frontend source are bind-mounted).

## Roadmap

1. Phase 1 (done): Valhalla routing, map UI, presets, elevation profile
2. Phase 2: Postgres persistence, auth, route library, GPX/FIT export with
   turn-by-turn course points
3. Phase 3: Wahoo Cloud API sync ("Send to Wahoo")
4. Phase 4: multi-arch images, CI, mobile polish, share links

## License

MIT (LICENSE file lands with the public-release phase).
