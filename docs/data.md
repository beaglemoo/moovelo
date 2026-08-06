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
docker volume rm <project>_valhalla_tiles     # e.g. komoot-lite_valhalla_tiles
docker compose --profile dev up -d            # or --profile prod
```

Routing is unavailable while the build runs; the app shows "routing engine
unavailable" until Valhalla's healthcheck passes.

## Refreshing data (monthly)

OSM data changes constantly; Geofabrik extracts are updated daily. To
refresh, run the same wipe-and-rebuild as above. A scheduled refresh
sidecar (compose cron, off by default) is planned.

## Disk and memory expectations

| Extract | PBF | Volume after build | Build RAM |
|---------|-----|--------------------|-----------|
| West Yorkshire | ~50 MB | ~1 GB | ~2 GB |
| England | ~1.6 GB | ~5 GB | ~6 GB |
| Whole UK | ~2 GB | ~7 GB | ~8 GB |

Build time ranges from a few minutes (fast desktop CPUs) to an hour or
more (small VMs) for England.
