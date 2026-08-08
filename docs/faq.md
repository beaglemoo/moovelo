# FAQ

## Do I need the place index?

No. Search, POIs along a route, reverse geocoding, and the cycle-network
overlay are all built from an optional, opt-in indexer
(`docker compose --profile index run --rm indexer`). Without it, the app
works exactly as a default install: plan, save, export, import, Wahoo
sync, surface breakdown, gradient colouring, climbs and ride time all
work with no index at all. Building it just adds search and POIs on top.
See [docs/data.md](data.md).

## Why does my imported route have no turn cues (or no surface bar)?

Imported files are map-matched back onto the road network - that's what
recovers turn-by-turn maneuvers for a file that didn't have any. If the
track leaves your loaded map extract, or follows a path the routing graph
doesn't have (a permissive trail, a private track, an area with sparse
OSM coverage), matching fails and the track is kept as an unmatched line
rather than rejected. An unmatched route has no maneuvers, so there are no
FIT course points and no surface bar (surface analysis needs the same
exact match onto the road network that matching does) - but distance,
elevation and export still work. The library still marks it "imported";
whether it actually matched is visible only by whether cues and a surface
bar show up.

## Why does the app's ride time differ from Valhalla's duration - and which one does my head unit see?

**Your head unit sees Valhalla's duration, not the in-app estimate.**
The number shown throughout the planner and library is a per-rider
estimate computed from the route's gradient, surface, and your settings
at `/settings` (weight, flat-road speed, optional FTP) - it's
display-only, recalculated fresh every time you view a route, and never
written anywhere. The GPX/FIT export and the Wahoo push always carry
Valhalla's own routing duration untouched, because that's what a head
unit needs for turn-cue timing to make sense. The two numbers are
expected to differ - a steep, gravel-heavy route will show a longer
in-app time than Valhalla's flat estimate for the same distance, and
that's the point of the feature.

## How much RAM/disk do I need?

For the default England extract: roughly 6 GB RAM during the first tile
build and about 5 GB of disk afterwards. The README's quickstart figures
(8 GB RAM allocated to Docker, 10 GB disk) give some headroom on top of
that for Postgres, the app itself, and a place index if you build one
(adds about 150 MB). A single-county extract for fast local development
needs much less - see the table in [docs/data.md](data.md#disk-and-memory-expectations)
for West Yorkshire and whole-UK figures too.

## Can I run a different country, or several extracts?

Yes. `VALHALLA_TILE_URL` takes one or more space-separated Geofabrik
URLs - point it at any Geofabrik region, or list several to cover more
than one area in a single build. See [docs/data.md](data.md#choosing-an-extract)
for the URL format and a note on Geofabrik's UK path change.

## How do I update?

Pull the new image (or rebuild) and restart - `docker compose --profile
prod up -d` (add `--build` if building from source). Database migrations
run automatically on backend startup, before the app starts serving
requests.

If you're using the place index, check whether the release notes mention
a required re-index. In particular, any install indexed before v0.3.0 has
its cycle routes stored unmerged, which makes the network overlay serve
tiles several times larger than necessary - re-running the indexer once
fixes it, and nothing else needs doing.

## How do I back up my data?

Everything that matters - accounts, saved routes, tags, Wahoo tokens, the
place index if built - lives in the `postgres` service's volume. A plain
`pg_dump` is enough:

```sh
docker compose exec -T postgres pg_dump -U bikegps bikegps > backup.sql
```

The `valhalla_tiles` volume (routing tiles, the downloaded OSM extract,
elevation data) doesn't need backing up - it's rebuilt automatically from
its source URLs on a fresh start, it's just slow to rebuild (see "why is
the first start so slow" below).

## Does any of my data leave my network?

Your route data does not, by default. Routing, storage, and (with the
opt-in indexer) search and POIs are all local. Two things are genuine
exceptions, and both are opt-in:

- **Wahoo sync** - only if you configure `WAHOO_CLIENT_ID`/`WAHOO_CLIENT_SECRET`
  and connect an account. Pushing a route uploads its FIT file to Wahoo's
  cloud.
- **Weather** - only if you configure `WEATHER_API_URL`, and only when you
  press "Show wind" - it never fetches automatically. Your route's sample
  coordinates and a start time are sent to whatever forecast service you
  configured.

Worth being precise about one more thing: the **map basemap tiles**
(CyclOSM/OSM raster tiles under your route) are, by default, fetched from
the public community CyclOSM/OSM servers as you pan and zoom - that's map
viewport requests, not your route or account data, but it is outbound
traffic revealing roughly where you're looking. Run your own tile server
and set `TILE_URL_CYCLOSM` if you want the map itself to stay fully
internal too (see [docs/self-hosted-tiles.md](self-hosted-tiles.md)).

## Can I use it without a Wahoo?

Yes. Wahoo integration is entirely optional - leave `WAHOO_CLIENT_ID`/`WAHOO_CLIENT_SECRET`
unset and every Wahoo-related button and endpoint stays hidden. Planning,
saving, importing, and exporting GPX/FIT all work with nothing configured
beyond the basics.

## Why is the first start so slow?

On first start, Valhalla downloads the configured OSM extract (1.6 GB for
the default England extract) plus elevation data, then builds routing
tiles from it - all before it can answer a single routing request. That
build is CPU-bound and can take from a few minutes on a fast desktop to
an hour or more on modest hardware. The app shows "routing engine
unavailable - it may still be building tiles" until it's done; subsequent
starts reuse the built tiles and come up quickly. Point `VALHALLA_TILE_URL`
at a small single-county extract for development if you want a build
measured in minutes.
