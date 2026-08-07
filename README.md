# Moovelo

Self-hosted bike route planner with direct Wahoo ELEMNT sync. Plan routes in
the browser on a cycling-oriented map, save them to your library, export
GPX/FIT, and push them straight to your Wahoo head unit - with turn-by-turn
cues.

## Features

- Click-to-plan routing: click the map to add waypoints, drag any waypoint
  marker to move it, or grab the route line itself and drag it to insert a
  via point exactly where you drop it. Right-click for a
  context menu (route from here, add waypoint, route to here, remove).
- Three bicycle presets - road, gravel, quiet - implemented as Valhalla
  costing bundles with genuinely different routing behavior (see
  [Routing presets](#routing-presets)).
- Elevation profile under the map with total ascent/descent and a hover
  marker synced to the route.
- Personal route library backed by Postgres/PostGIS: save, rename, reload,
  and share routes, with tags, notes and favourites for organising them.
  Tags rather than folders - a route can be both "gravel" and "with the
  kids". Search across names and notes, filter by tag, favourite or
  planned/imported, and sort by date, name, distance or climbing.
  Duplicate a route, or reverse one to ride it the other way - reversing
  re-routes rather than flipping the line, so one-ways and turn cues stay
  correct.
- GPX and FIT export from either the planner or the library - the FIT
  files carry Valhalla's turn-by-turn maneuvers as course points, so a
  Wahoo ELEMNT shows cues on the ride.
- Import GPX, TCX and FIT files - drop them anywhere in the app or use the
  Import button in the library. Imported tracks are map-matched back onto
  the road network, so a file that arrived with no instructions comes out
  with turn-by-turn cues and can be pushed to a head unit. A track that
  cannot be matched is kept as-is rather than rejected, and says so.
- "Send to Wahoo": push routes to your Wahoo account over the Cloud API;
  they appear on the ELEMNT after its next WiFi sync. Queued in the
  background with per-route status; re-pushing updates the same course.
- Public read-only share links per route - send a route to someone
  without an account.
- Simple auth: email + password, first user becomes admin, signups gated
  by a flag. Optional OIDC single sign-on (Pocket ID, Authelia, Keycloak,
  ...), including an SSO-only mode, plus a minimal /admin page.
- CyclOSM cycling basemap with an OSM standard fallback toggle, plus
  optional fully self-hosted tiles (see
  [docs/self-hosted-tiles.md](docs/self-hosted-tiles.md)).
- Entirely self-hostable: routing (Valhalla), app, database, and
  optionally the map tiles run on your own hardware. The only external
  service is Wahoo's cloud, and only if you use it.

## Screenshots

Planning a route in the Peak District (CyclOSM basemap, elevation profile):

![Route planner](docs/screenshots/planner.jpg)

A public share link - read-only map, elevation, GPX download, no account
needed:

![Shared route](docs/screenshots/share.jpg)

The route library, and the planner and library on a phone:

![Library](docs/screenshots/library.jpg)

<p>
<img src="docs/screenshots/mobile-planner.jpg" alt="Planner on mobile" width="320">
<img src="docs/screenshots/mobile-library.jpg" alt="Library on mobile" width="320">
</p>

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
docker compose --profile prod up -d          # pulls ghcr.io/beaglemoo/moovelo (amd64/arm64)
# ...or build the image yourself from source:
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
| `POSTGRES_PASSWORD` | `bikegps` | Database password - change for prod |
| `SIGNUPS_ENABLED` | `false` | Allow registrations beyond the first (admin) user |
| `COOKIE_SECURE` | `false` | Set `true` when serving over HTTPS |
| `APP_URL` | unset | External base URL, needed for OIDC and Wahoo callbacks |
| `OIDC_ISSUER` / `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` | unset | Enable OIDC SSO (all three required) |
| `OIDC_PROVIDER_NAME` | `Pocket ID` | Label on the SSO login button |
| `PASSWORD_AUTH_ENABLED` | `true` | Set `false` for SSO-only login (ignored unless OIDC is configured) |
| `WAHOO_CLIENT_ID` / `WAHOO_CLIENT_SECRET` | unset | Enable Wahoo sync (see [Wahoo sync](#wahoo-sync)) |

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

## Wahoo sync

Saved routes can be pushed straight to your Wahoo account so they appear
on your ELEMNT after its next WiFi sync - as a FIT course with
turn-by-turn cues. Your instance never needs to be exposed to the
internet for this to work.

The short version: register a (free, instantly-approved) sandbox app at
https://developers.wahooligan.com with redirect URI
`<APP_URL>/api/wahoo/callback`, put the client ID and secret in `.env`,
restart, and hit "Connect Wahoo" in the library.

The full walkthrough - registration form gotchas, how the queue and
statuses work, and troubleshooting - is in
[docs/wahoo-sync.md](docs/wahoo-sync.md).

## Documentation

- [docs/architecture.md](docs/architecture.md) - components, data flow, and design decisions
- [docs/wahoo-sync.md](docs/wahoo-sync.md) - full Wahoo setup walkthrough and troubleshooting
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

## License

[MIT](LICENSE). Contributions welcome - see
[CONTRIBUTING.md](CONTRIBUTING.md).
