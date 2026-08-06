# Komoot-lite — self-hosted bike route planner

Open-source (MIT), self-hostable route planner for cyclists with
direct Wahoo ELEMNT sync. Docker Compose only: dev on macOS, prod in
a Docker-capable LXC/VM behind Caddy.

## MVP scope — build ONLY this

1. Plan a route in the browser (waypoints, drag-to-reroute)
2. Save routes to a personal library
3. Export GPX and FIT
4. Push a route to the user's Wahoo account (Cloud API) so it appears
   on their ELEMNT after sync

Explicitly OUT of scope for MVP (do not scaffold, stub, or "prepare
for"): social features, comments, photos, highlights, ride
recording/tracking, mobile apps, Strava/Garmin integrations, i18n,
admin panels. (Valhalla's wider toolset — isochrones, map matching —
is deliberately kept available for post-MVP, but build nothing on it
yet.)

## Deployment

- Docker Compose is the only supported path. `docker compose up`
  must work on macOS (Apple Silicon + Intel) and Linux; publish
  multi-arch images (amd64 + arm64).
- Services: valhalla (official image), backend, postgres, frontend
  served by the backend. Named volumes for Postgres data, Valhalla
  tiles, and OSM/elevation downloads.
- Compose profiles: `dev` (hot reload, mounted source) and `prod`.
- App listens on 17777; document the Caddyfile snippet for prod.
- All configuration via env vars; .env.example documents everything.

## Stack

- **Routing: Valhalla** (official Docker image, latest stable).
  - First start downloads the configured Geofabrik extract (default
    england; great-britain documented) plus elevation data, and builds
    tiles into a named volume; subsequent starts reuse the tiles.
  - Bicycle costing exposed to the frontend as three presets —
    `road`, `gravel`, `quiet` — implemented as costing-option
    bundles (bicycle_type, use_roads, use_hills, avoid_bad_surfaces,
    cycleway preference) sent per request. Design the API so
    individual options can later become user-facing sliders;
    presets only in the MVP UI.
  - Turn-by-turn maneuvers from Valhalla responses are preserved
    end-to-end (needed for FIT course points).
  - Monthly data refresh: documented manual command + optional
    scheduled job (compose sidecar cron, off by default).
- **Backend:** Python 3.12 + FastAPI + SQLAlchemy + Postgres/PostGIS.
  FIT encoding via `fit-tool`, embedding Valhalla maneuvers as FIT
  course points so the ELEMNT shows turn-by-turn cues. Alembic
  migrations. The backend proxies all Valhalla calls (never exposed
  directly).
- **Frontend:** SvelteKit + MapLibre GL JS. CyclOSM raster tiles
  default (attribution required), OSM standard fallback. Static
  build served by the backend. Elevation profile chart under the map
  driven by route geometry + elevation.
- **Auth:** email + password (argon2), session cookies. First
  registered user becomes admin; SIGNUPS_ENABLED env flag (default
  false) gates further registrations.

## Wahoo Cloud API integration

- OAuth2 authorization-code flow against api.wahooligan.com.
- Client ID/secret from env; access + refresh tokens stored per-user
  in Postgres, auto-refreshed.
- Push = FIT course file (with course points), base64, POST
  /v1/routes with external_id = our route UUID; updates reuse the
  external_id.
- Sandbox credentials during development; README documents developer
  app registration and notes production approval is only needed for
  third-party users.
- Wahoo calls behind a service module with retries + rate-limit
  handling; pushes are queued with visible status, never blocking
  the UI.

## Conventions

- Type hints everywhere; ruff + mypy clean. Frontend: TypeScript,
  eslint + prettier.
- Tests: pytest (route CRUD, GPX/FIT golden files, mocked Valhalla
  and Wahoo clients); Playwright smoke test "plan → save → export
  GPX" against the dev compose stack.
- Conventional commits. Repo layout: /backend, /frontend, compose
  files at root, /deploy (Caddyfile snippet, prod notes), /docs.
- Never commit secrets; .env.example only.
- When a decision isn't covered here, propose it in chat before
  implementing.

## Definition of done (MVP)

- `docker compose up` on a clean macOS or Linux machine → working
  app with English routing out of the box.
- I can plan a route on my phone, save it, tap "Send to Wahoo", and
  it appears on my ELEMNT after a WiFi sync — with turn-by-turn cues.
- GPX export opens correctly in the ELEMNT companion app.
