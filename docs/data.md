# Routing data

Valhalla builds its routing graph from an OpenStreetMap extract plus
optional elevation data. Both are downloaded automatically on first start
and stored in the `valhalla_tiles` named volume.

## Choosing an extract

`VALHALLA_TILE_URL` accepts one or more space-separated Geofabrik URLs.
The default is England:

```
https://download.geofabrik.de/europe/united-kingdom/england-latest.osm.pbf
```

Useful alternatives:

| Region | URL | Notes |
|--------|-----|-------|
| Whole UK | `https://download.geofabrik.de/europe/united-kingdom-latest.osm.pbf` | ~2 GB PBF |
| Single county (fast dev) | `https://download.geofabrik.de/europe/united-kingdom/england/west-yorkshire-latest.osm.pbf` | builds in minutes |
| Any Geofabrik region | browse https://download.geofabrik.de | |

Note: Geofabrik reorganized its UK tree - the old `europe/great-britain`
paths now redirect to the Geofabrik homepage and will break the build with
an osmium "invalid BlobHeader size" error. Use `europe/united-kingdom`
paths.

## Elevation

`VALHALLA_BUILD_ELEVATION=True` (the default) downloads elevation tiles
covering the extract. This enables:

- hill-aware routing (`use_hills` in the presets)
- the elevation profile chart and ascent/descent stats

Setting it to `False` speeds up the first build; routes still work but the
profile chart is empty.

## Rebuilding after changing the extract

Tiles are only built when none exist, so changing `VALHALLA_TILE_URL`
alone has no effect. Wipe the volume and restart:

```sh
docker compose stop valhalla
docker compose rm -f valhalla
docker volume rm <project>_valhalla_tiles     # e.g. moovelo_valhalla_tiles
docker compose --profile dev up -d            # or --profile prod
```

Routing is unavailable while the build runs; the app shows "routing engine
unavailable" until Valhalla's healthcheck passes.

## The place index (optional)

Place search, reverse geocoding, POIs along a route and the cycle-network
overlay come from the same extract Valhalla downloaded - not from
Nominatim, Overpass or any other service. Nothing leaves your network.

Building the index is opt-in and runs to completion rather than staying
up:

```sh
docker compose --profile index run --rm indexer
```

Until it has run, the app hides the search box, the POI panel and the
network overlay, so a default install behaves exactly as before.

**Run it after Valhalla has finished, never during a tile build.** The
indexer reads the `.pbf` out of the `valhalla_tiles` volume, which is
wiped by the refresh procedure below - so the order is always: wipe,
let Valhalla download and build, then index. Running it alongside a tile
build is the one situation where both jobs want several GB at once.

Re-running is safe with the app up, with one caveat worth stating
precisely. The parse and the COPY - the great majority of the runtime -
hold no lock at all. The final transaction then truncates the live tables
and refills them, and it holds `ACCESS EXCLUSIVE` for that whole move: on
England that is roughly three seconds during which a request touching the
place index waits rather than fails. Requests that do not touch it, and
the routing path, are unaffected. Nobody ever sees a half-loaded index.

Measured on England (`england-latest.osm.pbf`, 1.6 GB):

| Stage | Time | Peak memory |
|-------|------|-------------|
| `osmium tags-filter` | ~9 s | ~2.2 GB |
| Parse and load | ~25 s | ~200 MB |
| Publish | ~3 s | - |

That yields roughly 73,000 places, 285,000 POIs and 5,500 cycle routes,
adding about 150 MB to the database. The filter pass is the high-water
mark: it reduces 1.6 GB to 45 MB, which is what keeps the parse cheap.

An index built before v0.3.0 still works for search and POIs, but its
cycle routes are stored unmerged and the network overlay will serve tiles
several times larger than it needs to. Re-run the indexer once after
upgrading to fix that; nothing else needs doing.

## Refreshing data (monthly)

OSM data changes constantly; Geofabrik extracts are updated daily. To
refresh, run the same wipe-and-rebuild as above, then rebuild the place
index if you use it - wiping the volume deletes the extract it reads. A
scheduled refresh sidecar (compose cron, off by default) is planned.

## Disk and memory expectations

| Extract | PBF | Volume after build | Build RAM |
|---------|-----|--------------------|-----------|
| West Yorkshire | ~50 MB | ~1 GB | ~2 GB |
| England | ~1.6 GB | ~5 GB | ~6 GB |
| Whole UK | ~2 GB | ~7 GB | ~8 GB |

Build time ranges from a few minutes (fast desktop CPUs) to an hour or
more (small VMs) for England.
