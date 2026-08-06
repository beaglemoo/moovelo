# Komoot-lite

Self-hosted bike route planner with direct Wahoo ELEMNT sync. Plan routes in
the browser, save them to your library, export GPX/FIT, and push them straight
to your Wahoo head unit.

Status: Phase 1 — route planning in the browser (Valhalla routing, map UI,
elevation profile). Persistence, export, and Wahoo sync come in later phases.

## Quickstart (development)

Requirements: Docker with Compose v2, ~8 GB RAM allocated to Docker.

```sh
cp .env.example .env
docker compose --profile dev up
```

Then open http://localhost:5173.

First start note: Valhalla downloads the configured OSM extract plus elevation
data and builds routing tiles into a named volume. For the default England
extract this takes roughly 30-90 minutes and several GB of disk. Subsequent
starts reuse the tiles and are fast.

For quick iteration, point `VALHALLA_TILE_URL` in `.env` at a small region
first, e.g. West Yorkshire (see `.env.example`), which builds in a few minutes.

## Routing presets

Three bicycle presets are exposed in the UI, implemented as Valhalla
costing-option bundles (see `backend/app/services/presets.py` for the exact
values and rationale):

- road: sticks to tarmac, comfortable on carriageways, avoids unpaved surfaces
- gravel: happily takes unpaved tracks, towpaths, and bridleways
- quiet: strongly prefers cycleways and quiet streets, softens climbs

## License

MIT (LICENSE file lands with the public-release phase).
