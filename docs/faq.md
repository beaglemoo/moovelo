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

## Why is the Alternatives button greyed out?

Because your route has more than two waypoints. Alternate routes are a
property of a straight A-to-B request - once you pin the line through
via points, you've already said which way to go, and the router has no
second opinion left to offer. Remove the via points, or plan the leg you
want alternatives for on its own.

## Why isn't my loop exactly the distance I asked for?

Loops are built out of real roads. Moovelo searches outward from your
point along several bearings, narrowing in on a distance that brings the
round trip close to your target, but it can only pick from routes that
exist - a target of 60 km typically lands within a few percent, and
rural areas with sparse road networks land less precisely than dense
ones. Each candidate shows its actual distance, and since picking one
gives you ordinary editable waypoints, dragging one out a little is the
fastest way to close a gap you care about.

## Are avoided roads saved with a route?

No, and this is deliberate. An avoid is an instruction to the router
while you're planning, and the route you save already reflects it - the
stored line goes the way your avoids shaped it. Reload that route and
you get the same line; edit it and it re-plans without the avoid list,
so re-add anything that still matters. Persisting avoids per route was
considered and rejected: it would mean a saved route could silently
re-plan differently later, which is worse than re-adding a point.

## Why doesn't the isochrone agree with the app's ride time?

They come from different models on purpose. The isochrone is drawn by
the routing engine using its own flat speed assumption for the costing
you picked; the ride time in the stats bar is computed per-rider from
gradient, surface and your settings. Teaching the routing engine your
personal speed curve isn't something it exposes a hook for, so the
isochrone is presented as what it is - a rough reach at routing speed,
useful for "which direction has more in it", not for "I'll be home by
six".

## How do I update?

Pull the new image (or rebuild) and restart - `docker compose --profile
prod up -d` (add `--build` if building from source). Database migrations
run automatically on backend startup, before the app starts serving
requests.

## How do I go back to an older version?

Roll the database back first, then the image. Going straight back to an
older tag leaves the database on a newer schema than that image knows
about, and the backend exits at startup with an alembic error like
`Can't locate revision identified by '0010'` - on the prod compose
service, which restarts automatically, that becomes a crash loop rather
than a single clear failure.

The order that works, downgrading from v0.5.0 to v0.4.0:

```sh
# 1. With the NEW image still in place, step the schema back one revision.
docker compose --profile prod exec backend alembic downgrade 0009
# 2. Then point the image tag at the older version and restart.
docker compose --profile prod up -d
```

Each release notes the revision it adds, so "one revision back" is
whatever the previous release ended on. Take a `pg_dump` first if the
data matters to you - a downgrade drops the columns the newer version
added, and anything stored only in them is gone. Downgrading past v0.5.0
also resets routes planned with the custom costing sliders to the road
preset, since the bundle they used lives in a column that is being
removed.

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
- **The route assistant** - only if an endpoint and model are configured,
  from `/admin` or the `LLM_*` variables. Whatever you type, plus place
  names and route figures the assistant looks up for you, is sent to that
  endpoint. Point it at Ollama or LM Studio on your own hardware and this
  stops being an exception at all - it is the configuration the project
  recommends. Note that the admin page's model browser and its **Test**
  button also call the configured endpoint, so a hosted gateway sees
  those even before anyone opens the planner.

Worth being precise about one more thing: the **map basemap tiles**
(CyclOSM/OSM raster tiles under your route) are, by default, fetched from
the public community CyclOSM/OSM servers as you pan and zoom - that's map
viewport requests, not your route or account data, but it is outbound
traffic revealing roughly where you're looking. Run your own tile server
and set `TILE_URL_CYCLOSM` if you want the map itself to stay fully
internal too (see [docs/self-hosted-tiles.md](self-hosted-tiles.md)).

## Which model should I use for the assistant?

Anything that speaks the OpenAI chat-completions shape **and supports
tool calling**. That second part is not optional: the assistant works
entirely by calling tools, so a model without tool support produces a
chatty, useless panel rather than an error. The model browser on
**/admin** filters to tool-capable models by default, and the **Test**
button makes one real call and tells you whether a tool came back.

Correctness matters less than you would expect - several cheap models
sequence the tools correctly and none of them can invent a coordinate,
because the schemas will not carry one. What you actually feel is
**latency**, and the biggest lever on it is not the model at all:

- A full question that needs five tool calls lands somewhere around
  13-30 seconds whatever you pick, because each round trip carries a
  conversation that keeps growing. A model that answers a bare question
  in one second does not answer *this* in one second.
- On a gateway that routes between providers (OpenRouter and similar),
  which provider serves you can matter more than which model you chose -
  the same model measured about six times slower on one provider than
  another. `LLM_PROVIDER_ORDER` expresses a preference, and the admin
  page lists per-provider price and context.
- That preference is deliberately never a hard pin. Constraining a
  gateway to exactly one provider turns that provider's bad day into a
  failed turn, and one provider was measured rejecting a multi-step tool
  conversation partway through while handling single calls fine.

If you are self-hosting the model, start by checking tool calling works
at all - some local chat templates emit tool-call-shaped JSON as
ordinary text, which this does not try to parse.

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
