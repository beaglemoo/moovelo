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

Measured on England (`england-latest.osm.pbf`, 1.6 GB), before all-roads
coverage (places, POIs and cycle routes only):

| Stage | Time | Peak memory |
|-------|------|-------------|
| `osmium tags-filter` | ~9 s | ~2.2 GB |
| Parse and load | ~25 s | ~200 MB |
| Publish | ~3 s | - |

That yields roughly 73,000 places, 285,000 POIs and 5,500 cycle routes,
adding about 150 MB to the database. The filter pass is the high-water
mark: it reduces 1.6 GB to 45 MB, which is what keeps the parse cheap.

**With all-roads coverage**, which is **off unless you ask for it**:

```sh
INDEX_ROADS=true docker compose --profile index run --rm indexer
```

Off by default because the figures below are the whole reason to make it a
choice. Place search, POIs, the cycle-network overlay and cycle-network
coverage all work without it, and most installs will never want it - so an
index built for the search box should not silently cost eight minutes and
a gigabyte. Setting the variable and re-running the indexer is the only
step; nothing else changes.

With it on (every bikeable OSM way, not just signed cycle-route members -
see [docs/architecture.md](architecture.md#all-roads-coverage) for what
"bikeable" excludes), the same extract measures:

| Stage | Time | Peak memory |
|-------|------|-------------|
| `osmium tags-filter` | ~13 s | ~2.2 GB (unchanged - dominated by the 1.6 GB input) |
| Parse and load (extraction + concurrent COPY) | ~6 min 18 s | ~772 MiB |
| Assemble (simplify geometry, measure length) + publish | ~1 min 22 s | Postgres-side |
| **Total** | **~7 min 53 s** | |

The filtered extract grows from 45 MB to 469 MB, and pass B now resolves
6,477,862 road ways alongside the places/POIs/cycle routes above - two
orders of magnitude more objects than cycle-route members alone, which is
where essentially all of the added time goes. The indexer container's own
peak memory stayed under 800 MB even so: pyosmium's node-location cache is
compact enough that this is a slower rebuild, not one that needs more RAM
than a modest VPS has. Simplifying each way's geometry before it is
written (see [docs/architecture.md](architecture.md#all-roads-coverage))
took the total geometry payload from 882 MB unsimplified to 426 MB - 52%
smaller, for a shape that is only ever summed and bbox-tested, never
drawn. `osm_ways` itself takes 791 MB as a table plus 445 MB across its
three indexes (1.24 GB total) - taking the database from roughly 150 MB
to about 1.5 GB.

An index built before v0.3.0 still works for search and POIs, but its
cycle routes are stored unmerged and the network overlay will serve tiles
several times larger than it needs to. Re-run the indexer once after
upgrading to fix that; nothing else needs doing.

An index built before cycle-network coverage (Phase 10) has cycle routes
but no `cycle_way_members` - the per-way lengths coverage sums. Search,
POIs and the network overlay all still work; `/api/coverage/cycle-network`
degrades to "needs a re-index" rather than a dishonest 0%, and stays that
way until the indexer runs once more. Migration 0014 adds the column that
carries this signal (`search_index_meta.cycle_way_member_count`, null on an
untouched pre-existing row, a real number the moment a rebuild finishes) -
so the fix, again, is just re-running the indexer.

An index that has **never** been built with `INDEX_ROADS` has no road ways
(`search_index_meta.osm_way_count` is null, migration 0015), and
`/api/coverage/roads` degrades to "needs a re-index" the same way
`/api/coverage/cycle-network` does above: set the variable and re-run.
Search, POIs, the network overlay and cycle-network coverage are all
unaffected. This is the biggest re-index yet in wall-clock time - see the
table above - and worth planning around rather than running unattended
alongside other load on a small host.

An index that **has** been built with `INDEX_ROADS` at some point behaves
differently on a later run that omits the flag: `publish()` treats
`osm_ways` as the one table it does not blindly replace on every run, so a
refresh without `INDEX_ROADS` leaves the existing road table - and the
count describing it - exactly as they were, rather than truncating it back
to empty. Road coverage keeps working off whatever the last roads-on build
indexed; it just does not pick up anything OSM has changed since, until a
run with the flag set again refreshes it. "Never indexed" and "was
indexed, then a later refresh forgot the flag" are deliberately distinct
outcomes now - only the first one degrades to "needs a re-index".

## Refreshing data (monthly)

OSM data changes constantly; Geofabrik extracts are updated daily. To
refresh, run the same wipe-and-rebuild as above, then rebuild the place
index if you use it - wiping the volume deletes the extract it reads. If
you run with all-roads coverage enabled, pass `INDEX_ROADS=true` on every
refresh, not just the first: as above, omitting it on a later run does not
delete the road table, but it does leave it un-refreshed.

### The refresh script

`scripts/refresh-data.sh` performs the whole sequence - stop Valhalla,
wipe the tiles volume, bring the stack back, wait for the tile build to
finish, rebuild the place index, and smoke-test one route:

```sh
scripts/refresh-data.sh --profile prod      # or --profile dev
```

Flags:

- `--profile dev|prod` - which compose profile to bring back up (default
  `prod`).
- `--skip-index` - refresh the routing tiles only, leave the place index
  alone.
- `--dry-run` - print the exact command sequence without running anything.
  Use this to see what it will do before scheduling it.

It reads the same `./.env` as `docker compose`, so `INDEX_ROADS`,
`VALHALLA_TILE_URL`, `OSM_DIR` and `WORK_DIR` all carry through unchanged -
if all-roads coverage is on in your `.env`, the refresh rebuilds it too,
with no extra flag. The tiles volume name is derived from `docker compose
config` rather than hardcoded, a lockfile prevents two runs colliding, and
the script refuses to run unless the stack is already up. Requests that hit
routing get a 503 ("routing engine unavailable") during the ~30-minute
England rebuild; the library and everything that does not touch Valhalla
stay up.

Two more knobs are plain shell environment variables, not `.env` keys - the
script does not source `.env` for them, so set them in the calling shell,
cron line or systemd unit, not the compose `.env` file:

- `SMOKE_ROUTE="lat,lon;lat,lon"` - point the final route probe at your
  extract (default is central London, valid for the England default
  extract).
- `HEALTH_TIMEOUT=<seconds>` - cap how long the script waits for Valhalla
  to report healthy before giving up (default `7200` = 2 hours), so an
  unattended run under cron cannot hang forever.

**Why a host script and not a compose sidecar:** a container cannot delete
the `valhalla_tiles` volume while another container has it mounted, and the
docker-socket alternative would hand the host to a container - see the
design-decisions note in [architecture.md](architecture.md#design-decisions).

**On a file-copy deployment** (prod is deployed by rsync, not a checkout),
`scripts/` is not in the image, so the script must be copied to the host
alongside the compose file - e.g. `rsync scripts/refresh-data.sh
host:/opt/bikegps/scripts/` - or it will not exist there to schedule.

### Scheduling it (off by default)

Nothing schedules the refresh unless you set it up. Two common ways, both
running on the host as the user that owns the compose project:

**cron** - the first of each month at 03:00:

```cron
0 3 1 * * cd /opt/bikegps && /opt/bikegps/scripts/refresh-data.sh --profile prod >> /var/log/moovelo-refresh.log 2>&1
```

**systemd timer** - `/etc/systemd/system/moovelo-refresh.service`:

```ini
[Unit]
Description=Moovelo OSM data refresh

[Service]
Type=oneshot
WorkingDirectory=/opt/bikegps
ExecStart=/opt/bikegps/scripts/refresh-data.sh --profile prod
```

and `/etc/systemd/system/moovelo-refresh.timer`:

```ini
[Unit]
Description=Monthly Moovelo OSM data refresh

[Timer]
OnCalendar=*-*-01 03:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

then `sudo systemctl enable --now moovelo-refresh.timer`.

On macOS (a Mac Mini staging host, say) the launchd equivalent is a
`StartCalendarInterval` job in `~/Library/LaunchAgents` calling the same
script; the mechanics are the same, off unless you load it.

## Disk and memory expectations

| Extract | PBF | Volume after build | Build RAM |
|---------|-----|--------------------|-----------|
| West Yorkshire | ~50 MB | ~1 GB | ~2 GB |
| England | ~1.6 GB | ~5 GB | ~6 GB |
| Whole UK | ~2 GB | ~7 GB | ~8 GB |

Build time ranges from a few minutes (fast desktop CPUs) to an hour or
more (small VMs) for England.
