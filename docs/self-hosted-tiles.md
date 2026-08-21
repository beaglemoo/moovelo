# Self-hosted CyclOSM tiles

The app defaults to the public CyclOSM raster servers. They are
community-run and render uncached high-zoom tiles on demand, which can
take seconds per tile in less-visited areas. Running your own CyclOSM tile
server gives you the same cartography at LAN speed with a permanent
private cache, and removes the last third-party dependency.

This is entirely optional and lives outside this repository's compose
stack - it is a reusable piece of infrastructure, not part of the app.

## What you need

- amd64 Docker host
- ~100 GB free disk for an England-sized region (PostGIS database ~50 GB,
  plus the tile cache)
- ~8 GB RAM available during the import
- ~2-3 GB steady-state, **plus a memory cap** - `renderd` grows over days
  of uptime and will not stop on its own. See [Memory](#memory) below.

## Setup

The simplest path is the prebuilt
[openstreetmap-tile-server-cyclosm](https://github.com/mhajder/openstreetmap-tile-server-cyclosm)
image (a [Overv/openstreetmap-tile-server](https://github.com/Overv/openstreetmap-tile-server)
derivative with the CyclOSM style preinstalled).

docker-compose.yml:

```yaml
name: cyclosm

services:
  cyclosm:
    image: mhajder/openstreetmap-tile-server-cyclosm:v2.2.0
    command: run
    ports:
      - "8080:80"
    environment:
      THREADS: "4"
      OSM2PGSQL_EXTRA_ARGS: "-C 2048"
      ALLOW_CORS: "enabled"
    volumes:
      - cyclosm-db:/var/lib/postgresql/12/main
      - cyclosm-tiles:/var/lib/mod_tile
    shm_size: 512m
    # renderd grows over days and does not stop on its own. Without a cap
    # it will eventually consume the host. See Memory below.
    mem_limit: 4g
    restart: unless-stopped

volumes:
  cyclosm-db:
  cyclosm-tiles:
```

Import your region once (hours for a country-sized extract; ~40 minutes
for England on 4 cores):

```sh
curl -LO https://download.geofabrik.de/europe/united-kingdom/england-latest.osm.pbf

docker compose create   # creates the named volumes

docker run --rm \
  -v $PWD/england-latest.osm.pbf:/data.osm.pbf:ro \
  -v cyclosm_cyclosm-db:/var/lib/postgresql/12/main \
  -e THREADS=4 -e "OSM2PGSQL_EXTRA_ARGS=-C 4096" \
  --shm-size 512m \
  mhajder/openstreetmap-tile-server-cyclosm:v2.2.0 import

docker compose up -d
```

Tiles serve at `http://<host>:8080/tile/{z}/{x}/{y}.png`.

## Pointing the app at it

Set in `.env` and restart the backend:

```
TILE_URL_CYCLOSM=http://<host>:8080/tile/{z}/{x}/{y}.png
```

If the app is served over HTTPS, the tile URL must be HTTPS too (browsers
block mixed content) - put the tile server behind the same reverse proxy
as the app.

## Pre-rendering low zooms

Tiles render on first view and are then cached forever. Low-zoom tiles
(country-level views) are the slowest, so pre-rendering them once makes
the initial map view instant. Loop over zoom levels with `render_list`,
computing the tile range for your region's bounding box per zoom:

```sh
# England bbox: lon -6.5..2.0, lat 49.8..56.0
for z in $(seq 0 10); do
  read x0 x1 y0 y1 <<< $(python3 -c "
import math
z=$z
def xt(lon): return int((lon+180)/360*2**z)
def yt(lat):
    r=math.radians(lat)
    return int((1-math.asinh(math.tan(r))/math.pi)/2*2**z)
print(max(0,xt(-6.5)), min(2**z-1,xt(2.0)), max(0,yt(56.0)), min(2**z-1,yt(49.8)))")
  docker exec cyclosm-cyclosm-1 render_list -m ajt -a -z $z -Z $z -x $x0 -X $x1 -y $y0 -Y $y1 -n 2
done
```

Note the `-m ajt`: this image names its renderd style `ajt`, while
`render_list` defaults to `default` - without the flag renderd rejects
every job with "No map for: default" and render_list still exits 0.

Higher zooms (11+) are rendered on demand as you browse; each area only
ever pays that cost once.

## Memory

`renderd` grows over days of uptime and does not level off. On one
12 GB host it reached **8 GB after about seven days** of ordinary use,
having started at 1.5 GB (its baseline with `THREADS: 8`; fewer threads
start lower). Set `mem_limit` on the service - it is in the compose
snippet above - and size it as the idle baseline plus a couple of GB of
headroom. Check your own baseline with:

```sh
docker exec <container> ps -eo rss,comm | awk '$2=="renderd"{print $1/1048576" GB"}'
```

The cap does not constrain the import, which runs as a separate
`docker run` above, so it can be tighter than the import's peak.

When the cap is hit the kernel kills the largest process, which is
`renderd`, and `restart: unless-stopped` brings the container back. The
on-disk tile cache is in a volume and survives, so the cost is only the
renders in flight. If those restarts become disruptive, a scheduled
restart at a quiet hour is preferable to an unplanned one.

### The symptom, which is easy to misread

Without a cap, the failure does not look like a tile server problem. The
host runs out of page cache, and **every process on it starts
re-faulting its own executable pages from disk** - so the machine
saturates its storage, `ssh` can time out during banner exchange, and
unrelated services on the same host stop responding.

The tell is that the IO is spread **evenly across every process**,
including idle daemons that do no data work. A genuine render storm
shows `renderd` and Postgres dominating; a flat distribution means the
page cache is gone, and you should look at what is holding the memory
rather than at the storage. On Linux, check the cgroup: a large `anon`
against a near-zero `active_file`, with a high `workingset_refault_file`
and `pgscan` far exceeding `pgsteal`, is thrashing.

To recover, kill `renderd` - the container restarts and reclaims the
memory immediately.

## Postgres tuning

The image ships PostgreSQL's stock `shared_buffers` of 128 MB, which is
very small against a country-sized database and leaves most tile queries
reading through to disk. On one England-sized install, raising it moved
the heap cache hit ratio from about 20% to about 93%.

Change it with `ALTER SYSTEM`, **not** by editing `postgresql.conf`:
that file lives inside the image and any edit is lost on the next
`docker compose up`, whereas `postgresql.auto.conf` lives in the
database volume and overrides it.

```sh
docker exec -i -u postgres <container> psql -d gis <<'SQL'
ALTER SYSTEM SET shared_buffers = '1500MB';
ALTER SYSTEM SET effective_cache_size = '3GB';
SQL
docker compose restart   # shared_buffers needs a restart, not a reload
```

Size `shared_buffers` against your `mem_limit`, not the host's total RAM:
it is shared memory and counts toward the container's cap, so it comes
out of the same budget as `renderd`. Leave `random_page_cost` alone -
tile queries are bbox-filtered and want the spatial index.

## Updating the data

Re-import the same way with a fresh PBF (the import replaces the
database), then optionally clear the tile cache volume so stale tiles
re-render:

```sh
docker compose down
docker volume rm cyclosm_cyclosm-tiles
# re-run the import with the new PBF, then:
docker compose up -d
```
