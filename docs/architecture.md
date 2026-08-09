# Architecture

## Components

```mermaid
flowchart LR
    subgraph Browser
        UI[SvelteKit SPA<br/>MapLibre GL]
    end
    subgraph Compose stack
        BE[FastAPI backend<br/>:17777]
        VH[Valhalla<br/>:8002 internal]
        PG[(Postgres/PostGIS)]
    end
    subgraph External
        PUB[Public CyclOSM / OSM tiles]
        SELF[Self-hosted CyclOSM<br/>optional]
        OIDC[OIDC provider<br/>optional]
        WAHOO[Wahoo Cloud API<br/>optional]
        WEATHER[Open-Meteo-compatible API<br/>optional]
    end

    UI -->|/api/*| BE
    BE -->|/route, /height, /trace_attributes, /isochrone| VH
    BE --> PG
    BE -.->|token exchange| OIDC
    BE -.->|queued FIT pushes| WAHOO
    BE -.->|wind, on "Show wind" only| WEATHER
    UI -->|raster tiles| PUB
    UI -.->|raster tiles when TILE_URL_CYCLOSM set| SELF
```

| Component | Technology | Role |
|-----------|------------|------|
| frontend | SvelteKit + MapLibre GL JS, static SPA build | Planner, library, admin, share pages |
| backend | Python 3.12, FastAPI, SQLAlchemy async, httpx | API + static host; proxies Valhalla, talks to Wahoo |
| postgres | PostGIS 16 | Users, sessions, routes (geometry + snapshot), Wahoo tokens |
| valhalla | Official `valhalla-scripted` image | Bicycle routing and elevation; never exposed directly |

## Request flow for a route

1. The browser POSTs `/api/route` with waypoints and a preset name (plus,
   optionally, a custom costing bundle from the slider popover).
2. The backend resolves what actually goes to Valhalla -
   `services/presets.py`'s `resolve_costing`: the custom bundle when one was
   sent, otherwise the named preset's bundle - and calls Valhalla `/route`.
   `BicycleCostingOptions` (`schemas.py`) is the server-side allowlist for
   custom values: `extra="forbid"` plus the same bounds as the three
   presets, so nothing beyond the five tunable options ever reaches
   Valhalla.
3. The backend decodes the returned polyline6 legs, resamples the shape to
   at most 500 points, and calls Valhalla `/height` for the elevation
   profile. If elevation data was not built, the route still succeeds with
   an empty profile.
4. The response carries per-leg geometry (polyline6) and the raw Valhalla
   maneuvers untouched - these are preserved end-to-end because later
   phases embed them as FIT course points for turn-by-turn cues on the
   head unit - plus summary stats and the elevation series.
5. The frontend decodes the legs, renders the route line, and tracks which
   shape indices belong to which leg so that dragging the line knows where
   to insert the new via waypoint.

## Persistence and auth

Routes are stored with their full response snapshot (legs with geometry
and maneuvers, elevation series, stats) as JSONB plus a PostGIS
linestring of the merged shape. Saving never re-routes; loading a saved
route replays the snapshot. Alembic migrations run on backend startup.

`routes.costing_options` is a sibling of `routes.preset`, not part of the
snapshot: null when a named preset (`road`/`gravel`/`quiet`) was used,
and the resolved `BicycleCostingOptions` bundle when `preset` is
`"custom"`. It is set alongside `preset` at save time (both come from the
same request) and carried forward unchanged by duplicate and reverse, so
either operation re-routes (or, for duplicate, replays) with the same
costing the original was saved with. A user's named custom presets live
in their own `custom_presets` table (`GET/POST/PATCH/DELETE
/api/custom-presets`, capped at 20 per user) and are purely a client-side
convenience for populating the sliders - see
[Design decisions](#design-decisions) for why routes do not reference
them.

Auth is session-cookie based: argon2 password hashes, a sha256-hashed
token in a `sessions` table with a 30-day sliding expiry. The first
registered user becomes admin; further signups are gated by
`SIGNUPS_ENABLED`. Optional OIDC SSO (authorization-code flow, state
cookie, email matching) works with any generic provider;
`PASSWORD_AUTH_ENABLED=false` hides password login entirely but is
ignored unless OIDC is configured, so it can never lock you out. Admin
accounts get a minimal `/admin` page (users, stats, config overview).

Each user optionally has a `user_settings` row (weight, flat-road speed,
optional FTP) edited at `/settings`. `GET /api/settings` returns sane
defaults without inserting a row, so a user who never opens the page
never gets one; the first `PATCH` creates it. These feed the ride-time
model - see [Realistic ride time](#realistic-ride-time).

## The place index

`places`, `pois` and `cycle_ways` hold settlements, useful stops and
signed cycle routes parsed out of the same OpenStreetMap extract Valhalla
already downloads. Nothing external is involved: no Nominatim, no
Overpass, no outbound calls.

These four tables are the only ones the app never writes. An opt-in
indexer sidecar (`indexer/`, compose profile `index`) fills them; the
backend reads them. It mounts the `valhalla_tiles` volume read-only and
works in three stages:

1. **`osmium tags-filter`** streams the extract down to just the tagged
   objects and the nodes and members they reference - 1.6 GB to 45 MB for
   England, in about nine seconds. This is what keeps the rest cheap:
   parsing the whole extract with node-location caching would be the
   largest memory consumer on the machine. (The `-R` flag looks like it
   would help and does the opposite - it *omits* referenced objects,
   which would leave every way without coordinates.)
2. **Two pyosmium passes.** The first reads relations only and flattens
   cycle superroutes, because a route relation can have other relations
   as members and a child is not guaranteed to appear before its parent.
   The second resolves geometry and streams rows out.
3. **COPY into staging, then one publishing transaction.** The minutes of
   work happen outside any lock; only the row move is inside it. Each
   COPY needs its own connection - a `COPY IN` occupies its connection
   for the whole transfer, so three on one connection deadlock silently.

A full England rebuild takes about 37 seconds and answers every request
throughout. `search_index_meta`
holds a single row - enforced by a boolean primary key with a `CHECK` -
recording when the index was last built and from what. `GET /api/config`
reports its existence as `search_enabled`, and the frontend hides the
search box, the POI panel and the cycle-network overlay when it is false,
so a default install is unchanged rather than offering features that
would answer nothing.

Two type choices decide whether the indexes are usable at all:

- **Points are `geography`, not `geometry`.** `ST_DWithin` and
  `ST_Distance` then work in meters and stay index-backed. The same query
  against a geometry column returns degrees - for Tring to Ivinghoe
  Beacon, 0.071 rather than 5900 - which is plausible enough to pass
  unnoticed while being wrong by five orders of magnitude.
- **Cycle routes are `geometry` in 4326**, matching `routes.geom`. Vector
  tiles need web mercator, but the tile query transforms the tile
  envelope, which is a constant, so the GiST index still serves the
  bounding-box filter. Transforming every row instead would not.

### Search ranking

`GET /api/places/search` blends three signals, weighted 0.55 / 0.30 /
0.15:

- **Text**: an exact match on the folded name scores 1.0, a prefix match
  0.85, otherwise trigram similarity. The prefix branch is what makes
  "trin" find Tring - trigrams need three characters to mean anything and
  are noisy on short typeahead input.
- **Importance**: assigned at index time from the `place=` value, nudged
  by population where OSM carries it.
- **Proximity**: distance from the map centre, halving every 80 km.

All three carry weight because 21,848 of England's 73,084 places share a
name with at least one other. Two Newports, both towns, are identical
until you know where the map is pointing.

The trigram branch uses the `%` operator rather than a `similarity()`
comparison because only the operator can use the GIN index; its threshold
is set with `SET LOCAL` so pooled connections are returned unchanged. The
whole query runs in about 5 ms against the full index, with 34 rows
reaching the sort.

#### Why the half-life is 80 km

It was 20 km first, chosen by reasoning about how "local" search ought to
feel. That is the wrong way to think about it. `1/(1 + d/H)` saturates at
both ends - far below H everything scores near 1, far above it everything
scores near 0 - and in both cases proximity stops separating anything,
leaving importance to hand the query to whichever same-named place is
grandest. The term discriminates over distances comparable to H, so H
should match the distances actually being compared, and "which of
England's several Newports did you mean" is a 50-200 km question.

Measured over the 60 most duplicated settlement names from four map
centres, by the median distance of the top result:

| half-life | 5 km | 20 km | 40 km | 80 km | 160 km | 400 km |
|---|---|---|---|---|---|---|
| median top result | 154 km | 109 km | 92 km | **80 km** | 91 km | 110 km |

A clean U with its minimum around 80 km - and note both tails, which is
the saturation showing up from either side. Raising it changed nothing
across 30 unambiguous city queries ("birmingham", "oxford", ...) from two
centres, so the improvement is not bought by burying obvious answers.

The suite passed this fourfold change without noticing, which is its own
finding; `test_proximity_still_separates_places_hundreds_of_km_apart` now
pins it, and fails at the old value.

### Reverse geocoding

`GET /api/places/reverse` names a point, which the planner uses twice:
as a header on the map's right-click menu, and to open the save dialog on
"Tring to Ivinghoe Beacon" rather than "Ride 7 Aug".

**It is not a nearest-neighbour query**, which is what it was written as
first. Over a third of England's 73,084 indexed places are
`place=locality`, OSM's catch-all for named spots nobody lives in: field
corners, bridges, trailheads, sandbanks out at sea. They are almost
always the nearest named thing, so ranking by distance answered
"Ivinghoe Beacon" with "The Ridgeway Trailhead (Northeast Side)" and a
point in open farmland with "Dixon's Gap Bridge". Both are correct
nearest neighbours and neither is where you are. The tests written
alongside that first version passed.

Instead each place gets a **reach** - how far it may lend its name -
scaling with the square of the importance the indexer already assigns:

| | city | town | village | hamlet | locality |
|---|---|---|---|---|---|
| reach | 48 km | 21 km | 8 km | 3 km | 1.1 km |

Ranking by `distance / reach` asks how far into a place's natural range
the point sits, rather than what is closest, and dropping rows beyond
their own reach is what makes "nowhere" an answer. The farmland point
now returns "Wilstone". A locality still wins within a kilometre of it,
so nothing is thrown away - a whitelist would have lost that.

The cost is the KNN operator: `distance / reach` cannot use `<->`, so
the GiST index bounds the candidates at 50 km (reach can never exceed
it) and the sort runs over what survives. Worst case measured is central
Birmingham - 4,792 index rows, 74 within reach, 9 ms.

Null is a normal answer - no index, or nowhere near anywhere - so both
callers decorate something that still works without a name. The frontend
never passes the lookup at all when `search_enabled` is false.

### POIs along a route

`POST /api/places/pois-along-route` answers "where is water and coffee on
this ride". Two choices carry it:

- **The line goes in the request, not a route id.** The question gets
  asked while planning, before anything is saved, so a GET keyed on a
  stored route would miss the moment it matters. A real 50 km route is
  about 1,800 points - roughly 40 KB of JSON - so this is cheap; the cap
  is 20,000 points.
- **Ordered by distance *along* the route, not distance *from* it.** A
  list of nearby POIs is not a plan for a ride; a list in the order you
  will pass them is. `ST_LineLocatePoint` gives the fraction along, which
  the geography length turns into metres. On a route that doubles back it
  reports the first pass.

`ix_pois_geog` is what makes it work, and it is worth being precise about
which index: the roadmap originally said this item would finally exercise
`ix_routes_geom`, and it does not. The route is a literal in the query,
not a stored row. Against a Tring to Oxford ride the POI index offers
1,740 candidates from the line's bounding box, the exact `ST_DWithin`
test keeps 722, and the whole query takes 26 ms. `ST_LineLocatePoint`
needs plain geometry - hence the geom/geog split in the CTE - and runs
only over the survivors.

Results are capped at 300 with a `truncated` flag, so a partial answer
never arrives looking like a complete one.

#### Why the categories are grouped

The indexer writes 16 flat categories; the panel shows eight chips and
defaults to four. That is measured rather than guessed. The same 50 km
ride passes 722 POIs within 250 m:

| bike_parking | food | cafe | pub | ... | water | bike_repair | picnic |
|---|---|---|---|---|---|---|---|
| 232 | 167 | 123 | 68 | | 1 | 1 | 1 |

Only one of those 232 bike parking stands has a name. Show everything at
once and the rare things people actually go looking for - water, a repair
stand - are unfindable, so `bike_parking` is left out of the UI entirely
and the default is water, coffee, toilets and bike.

OSM names and opening hours are rendered as text, never as markup. They
are untrusted input, and Phase 9's prompt-injection rule starts here.

### Cycle-network overlay

`GET /api/places/cycle-network/{z}/{x}/{y}.mvt` serves the signed network
(5,545 relations, 43,897 km in England) as Mapbox vector tiles. A bbox
GeoJSON endpoint would refetch multiple megabytes on every pan and push
simplification onto the browser; MVT is clipped, quantised and cacheable
per tile.

Which networks appear depends on zoom, the way a paper map drops detail
rather than drawing everything at every scale:

| zoom | networks |
|---|---|
| 11+ | all four, including local |
| 8-10 | international, national, regional |
| below 8 | international and national only |

`ST_Transform` is applied to the tile *envelope*, not to every row: the
envelope is a constant, so `ix_cycle_ways_geom` still serves the
bounding-box filter. Transforming each row would disable it.

An empty tile is a normal answer and never a 404 - most of the world has
no NCN, and MapLibre handles a zero-length body fine. A tile outside the
pyramid *is* a 404, because that is a client bug worth seeing.

#### Why the indexer merges the ways

`assemble_cycle_routes` wraps the collected geometry in `ST_LineMerge`,
and that one call is what makes this overlay affordable. A route
collected from its member ways averages 195 separate parts, and NCN 1 has
3,547. Douglas-Peucker preserves both endpoints of every part whatever
the tolerance, so an unmerged route resists simplification entirely: the
England-wide tile measured 228 kB simplified against 50 kB when merged
first, and raising the tolerance eightfold moved it by 3 kB. Merged, the
average route is 5 parts and the whole country is one 50 kB tile. The
merge costs 98 ms for all 5,545 routes, once, at index time.

#### What it is actually for

Worth being plain about: **the CyclOSM basemap already draws the NCN**,
so on the default basemap this overlay mostly recolours lines that are
already there. It earns its keep on the OSM standard basemap, which shows
no cycle routes at all, and it is ours to label and query later - the
tiles carry `ref`, `name` and `network` per feature. Route labels would
need a glyph source, which the style deliberately does not have, so they
are not drawn today.

The overlay covers whatever the Geofabrik extract covers. With the
default England extract it stops at the Welsh and Scottish borders.

### Trigrams and accents

Names are matched with `pg_trgm` trigram indexes, which serve both
`LIKE 'prefix%'` and the `%` similarity operator. `unaccent` is
deliberately unused: it is `STABLE` rather than `IMMUTABLE`, so it cannot
appear in a generated column or an index without a wrapper function. The
indexer folds accents in Python into a `name_norm` column instead, and
the trigram indexes live on that.

Two asyncpg lessons are worth not relearning, both in
`services/places.py`: a bare `:lat` inside `CASE WHEN :lat IS NULL`
cannot be typed ("could not determine data type of parameter $1"), and an
empty array parameter cannot either ("could not determine polymorphic
type because input has type unknown"). Both need an explicit `CAST`.

## Wahoo sync

`services/wahoo.py` implements OAuth against api.wahooligan.com (tokens
per-user in Postgres, proactively refreshed) and the push itself: the
route's FIT file - maneuvers embedded as course points - is uploaded as
a multipart `route.fit` attachment to POST/PUT `/v1/routes` with our
route UUID as `external_id`, so re-pushes update the same course.
(Wahoo rejects the base64 data-URI upload form its docs describe, and
requires `workout_type_family_id` and start coordinates.)

Pushes never block requests: `services/wahoo_queue.py` runs a single
asyncio worker draining an in-process queue, which also serializes calls
under Wahoo's sandbox rate limits (25/5min). Status lives on the route
row (`queued -> pushing -> synced/error`), the UI polls it, and startup
re-enqueues anything stranded mid-push by a restart. Retries honor 429
Retry-After; a 401 triggers one token refresh; PUT on a vanished route
falls back to POST.

## Weather and wind

Off by default (`WEATHER_API_URL` unset -> `settings.weather_enabled` is
`False`), and gated twice: `AppConfig.weather_enabled` hides the panel on
the frontend, and `POST /api/route/weather` itself 404s before doing
anything else when the setting is unset - a client that ignores the config
still cannot reach an unconfigured instance out to the network. On top of
that, the frontend never calls the endpoint from an effect; it is wired to
one button ("Show wind") so nothing goes out unless a rider explicitly asks.

`services/weather.py` samples the route line roughly every
`WEATHER_SAMPLE_SPACING_M` (10 km), capped at `MAX_WEATHER_SAMPLES` (20),
always including the start - a route shorter than the spacing yields one
sample. One batched GET carries every sample's `latitude`/`longitude` as
comma-separated lists plus `hourly=wind_speed_10m,wind_direction_10m`,
`wind_speed_unit=ms` and `timezone=UTC` (so the returned hourly series is
unambiguously UTC, not shifted to the queried location's local time), with
`start_date`/`end_date` covering the ride's estimated start through arrival.
Open-Meteo answers a JSON list for multiple locations but a bare object for
one - both shapes are handled, which matters because a short route is
exactly the one-sample case.

Each sample's arrival time comes from an optional `ride_time` profile
(`{dist_m, time_s}` pairs, interpolated) or, failing that, proportionally
from `duration_s`; the nearest hourly slot to that arrival is read off the
matching location's series. Wind maths: travel bearing at each sample is
the bearing toward the next sample (the last sample reuses the previous
leg's bearing; a single-sample route falls back to the bearing of the whole
route line, since it has no "next sample" at all). Wind direction is
meteorological (where the wind blows *from*), so the relative angle is
taken against the downwind direction (`wind_direction + 180`):
`headwind_ms = -speed * cos(relative)` (positive slows you down, a tailwind
is negative) and `crosswind_ms = speed * sin(relative)`.

A plain dict keyed on the rounded start hour plus a hash of rounded sample
coordinates caches results for `~30 min` (stdlib only, evicted lazily on
access) - a rider nudging the start-time picker back and forth does not
re-hit the provider each time.

Failure handling mirrors surface breakdown's "degrade, don't 500" policy,
but the trigger is different: Open-Meteo rejects a `start_date` outside its
forecast window with a 400 and `{"reason": ..., "error": true}` body (this
is expected, ordinary behaviour when someone asks about a ride next month,
not a fault) - so both a 400 status and any `error: true` body degrade to
`WeatherAlongRoute(segments=[], truncated=True)` rather than raising, and
the panel says the start time is beyond the forecast window. Only a
transport error or repeated 429/5xx (retried up to `MAX_ATTEMPTS=3` with
Retry-After-seeded exponential backoff, capped at 60s, copying
`services/wahoo.py`'s retry shape) raises `WeatherError`, which the endpoint
maps to a 502.

## Share links

A route can be shared read-only: `POST /api/routes/{id}/share` sets a
random `share_token` on the row (rotating it invalidates old links,
DELETE revokes). `GET /api/shared/{token}` and `.../export.gpx` are the
only unauthenticated data endpoints, serving the snapshot and GPX by
token only - no route ids, no owner information.

`share_route` also generates a natural-language summary via
`services/route_summary.py` - one LLM completion, no tools, over the
route's own stats (distance, elevation, surface mix, climbs). This is
the *only* place the summary is ever generated: `share_route` is an
authenticated, owner-triggered write, whereas `GET /api/shared/{token}`
takes no session at all, so generating it there would let anonymous
traffic spend the operator's money on every page view. The summary is
stored alongside a signature hashed from the route's concatenated leg
geometry (`route_geometry_signature`); the read path serves the stored
text only when that signature still matches the route's current
geometry, and suppresses it - rather than showing it stale - after a
re-route. Rotating an unchanged link's token skips the LLM call
entirely, since the stored signature already matches. No assistant
configured (see `services/llm_config.py`) or the call failing both
degrade to no summary; sharing itself never fails because of this.

## Route assistant

Off unless configured. `LLM_BASE_URL` and `LLM_MODEL` (plus
`LLM_API_KEY` where the endpoint wants one) enable it, from the
environment or from `/admin`, which stores the same settings in a
single-row `llm_settings` table and wins per field - so a declarative
install never has to open the UI, and an operator changing model or
provider never has to redeploy. `services/llm_config.py` resolves the
two into one `LLMConfig`; `assistant_enabled` on `GET /api/config` is
what the frontend gates the panel on.

`services/llm.py` is a raw httpx client against
`POST {base_url}/chat/completions` - no SDK, because the whole surface
needed is one endpoint and the same shape works against OpenRouter,
Ollama, LM Studio or anything else speaking it. Retries follow the house
policy in `services/weather.py` and `services/wahoo.py`: 429 and 5xx
only, `Retry-After` honoured when numeric.

### The model never emits coordinates

This is the design's load-bearing property, and it is structural rather
than a matter of asking nicely. Tools return opaque handles -
`place:1`, `poi:2`, `loop:1` - and every tool that places a point
accepts only a string matching
`^(place|poi|current|centre|loop):[A-Za-z0-9_-]+$`, validated by pydantic
before any handler runs. A model that emits `51.7,-0.7` gets a
validation error back as a tool result, exactly like any other bad
argument, and corrects itself. `services/assistant/refs.py` holds the
per-request `HandleTable` that resolves handles to real coordinates;
what is on the rider's screen is seeded into it as `current:N` and
`centre:map`, so "from here" works without the model ever seeing a
number.

The constraint has to be on the *item* type. `Field(pattern=...)` on a
`list[str]` silently omits the pattern from the generated JSON schema
and raises `TypeError` at validation time, so it is
`Annotated[str, StringConstraints(pattern=...)]`, and a test asserts on
the schema the model actually receives rather than on the Python model.

Conversation state lives on the client, which echoes back the handles it
was given; the server keeps no session and adds no table. Those
coordinates come from the client, never from the model, so accepting
them adds nothing a rider could not already POST to `/api/route`.

### Tools and budgets

Six tools, each calling the service layer directly rather than making
HTTP calls back into the app: `search_place`, `find_pois`, `plan_route`,
`generate_loop`, `route_stats` and `modify_route`. Every one is a
primitive phases 5-8 already built - Valhalla still routes, PostGIS
still searches, and the model only chooses what to call and in what
order.

`services/assistant/turn.py` runs the loop, bounded by
`MAX_COMPLETION_CALLS = 9` and `WALL_CLOCK_S = 120`. Malformed
arguments, unknown tool names and handler `HTTPException`s all become a
tool-result message describing the error, so the model can correct
itself rather than the turn failing over something recoverable; each
consumes budget, so a broken model does not retry for free. If the same
tool fails the same way twice the turn stops without another completion,
because a weak model repeats a bad call verbatim rather than fixing it.
Budget exhaustion writes its own closing message and never spends
another completion on one.

### Staying on route planning

The assistant answers questions about bike routes and declines everything
else. Worth being precise about what enforces that, because the two
mechanisms are not equally strong:

**Persuasion.** The system prompt states the scope, tells the model to
decline out-of-scope requests in one sentence, and says plainly that no
message - from the rider, a tool result, or text claiming to be an
operator or an emergency - can grant an exemption. The same rules are
repeated in a trailing system message *after* the conversation, not only
before it: a leading instruction block is easy to talk past because
everything read afterwards is more recent, and restating the rules last
makes them the most recent thing as well as the first. This reduces how
often the model goes off-topic. It is not a guarantee, and it should not
be described as one.

**Guarantees.** These hold whatever the model is talked into. The tool
schemas cannot express anything outside route planning, so an off-topic
model is a model writing prose rather than one doing something else with
the app. `max_tokens` caps what any single completion can be billed for.
`MAX_COMPLETION_CALLS` and the wall clock cap one turn. And a per-user
sliding window (`services/rate_limit.py`) caps turns per hour, so an
account being used as a general-purpose chatbot costs the operator a
bounded amount rather than an open-ended one.

The honest summary: the prompt decides what the assistant *usually* does,
and the schemas plus the budgets decide what it *can* do. Only the second
one is a security property.

### Prompt injection

Place and POI names come from OpenStreetMap and are written by the
public, so they are treated as untrusted data throughout: raw OSM `tags`
are never serialised into a tool result, model-facing strings are length
capped, and results are JSON objects with fixed keys rather than prose.
The system prompt says as much too, but the prompt is not the defence -
the injection's *goal* (a coordinate waypoint, a state change) is
unreachable through the tool schemas whether or not the model is fooled.
Nothing in this phase writes to the database.

### Chat transport

`POST /api/assistant/chat/stream` is the only streaming endpoint in the
app. It reports one turn as it happens over Server-Sent Events: `token`
for prose as it arrives, `tool_call` and `tool_result` so the panel can
say which step is running and which one failed, `handles` (the places
the assistant looked up, with the coordinates the model itself never
sees), `proposal`, `error`, and exactly one `done` on every path so the
client has a single unambiguous stop signal. `POST /api/assistant/chat`
answers the same turn as plain JSON once it is over.

Both consume `run_turn_events()` in `services/assistant/turn.py`, which
is the only implementation of the loop - the budgets, the self-correction
paths and the proposal exist once, not once per transport. The single
difference is whether prose is fetched a fragment at a time
(`LLMClient.stream()`) or awaited whole (`LLMClient.complete()`).

Streaming retries are deliberately narrower than the non-streaming ones:
once a fragment has been handed to the caller it is already on the
rider's screen, so restarting the completion would repeat text rather
than replace it. A failure mid-body is terminal.

### Proposals

A turn that routed anything ends with a `proposal` frame carrying the
waypoints, the preset, and the `RouteResponse` Valhalla already returned
for them. Nothing is saved and no server state changes: the assistant
proposes, and the rider accepts or discards.

`+page.svelte` owns the offer rather than the panel, because two other
things need it - the ghost line on the map (`proposal-preview`, drawn
like the loop and alternates previews so the real route stays on top)
and the staleness check. Accepting runs `applyProposal()`, which mirrors
`useLoop()` exactly: push the current snapshot onto the history stack,
assign the waypoints and preset, `claimRoute()`, put the returned
snapshot straight onto `route` rather than replanning it, and
`markEdited()`. Undo therefore restores the pre-proposal planner like
any other edit, and what lands is ordinary draggable waypoints.

Staleness joins the single shared invalidation `$effect` rather than
adding a second one - this subsystem has produced the same bug more than
once, and the fix each time was to compare the whole `RoutingInputs`
tuple in one place. Waypoints count for a proposal, unlike for a loop: a
loop is anchored to its own origin, whereas a proposal is an offer to
replace what is on screen, so once the rider edits their route the offer
describes something they are no longer looking at.

Every figure on the proposal card is read off the snapshot, never off
the model's prose. The prompt forbids stating a number no tool returned,
but this is what makes it structurally impossible for an invented one to
reach the rider as a measurement.

The browser side is `streamAssistantChat()` in `frontend/src/lib/api.ts`
- `fetch` plus a `ReadableStream` reader, parsing SSE by hand.
`EventSource` is GET-only and this needs a POST body carrying the whole
conversation. Authentication and the configured-or-not check resolve
before the response starts, so those failures are ordinary HTTP errors;
only something that goes wrong after the first byte becomes an `error`
frame. Conversation state lives on the client, which echoes back the
handles it was given, so the server keeps no session.

## Importing route files

`POST /api/routes/import` takes a GPX, TCX or FIT upload.
`services/importer.py` parses it (namespace-agnostic, tracks or route
points, dropping implausible fixes); `services/import_routes.py` then map
matches the track through Valhalla `/trace_route` with
`shape_match=map_snap`, which is what recovers the maneuvers an uploaded
file does not have. Tracks are thinned to roughly one point per 15 m and
chunked, so a failure costs one chunk rather than the whole ride.

A file that parses but cannot be matched - it leaves the map extract, or
follows paths the routing graph lacks - is stored as an unmatched line
with no cues rather than rejected. Elevation comes from Valhalla exactly
as it does for planned routes, so ascent stays comparable across the
library; the file's own elevation is used only when the routing tiles
carry none.

## Surface breakdown

`services/valhalla.py`'s `trace_attributes` calls Valhalla `/trace_attributes`
with `shape_match: edge_walk` over a route's own decoded shape, requesting
`edge.length`, `edge.surface`, `edge.road_class`, `edge.use` and
`edge.cycle_lane`. The edges are aggregated into metres per bucket
(`SurfaceBreakdown`: `surface_m`, `road_class_m`, `use_m`, `cycle_lane_m`,
`total_m`) - metres rather than fractions, so chunks sum and the frontend
derives percentages. Bucket keys are Valhalla's own enum strings, including
ones absent from its documented enum (`service_road`, `parking_aisle`), so
these are plain dicts rather than a closed set. `cycle_lane_m` is kept
separate from `use_m` deliberately: surface mix and marked cycling
infrastructure measure different things.

Chunking reuses `TRACE_MAX_POINTS` but, unlike `trace_route`'s chunks, does
not share a boundary point between chunks - only aggregate metres are
accumulated here, nothing needs to stitch back into continuous geometry, so
a clean split is simpler and correct.

Failure handling is the deliberate opposite of `trace_route`'s: any failure -
a 4xx or the 503 an unreachable engine maps to - degrades to `None` rather
than propagating. `edge_walk` requires the shape to already lie exactly on
the routing graph, so an unmatched imported track fails it by design; surface
is decorative and never touches FIT or export, so there is nothing here worth
retrying for.

`POST /api/route/surface` exposes this for the planner, taking the route's
per-leg lines rather than a route id (like `/api/places/pois-along-route`)
so it can be asked about before anything is saved. Legs, not one
concatenated line: the merged shape is discontinuous at via-waypoint
joins, where edge_walk reliably fails even though each leg matches alone -
chunks never span a leg boundary for the same reason. The request may also
carry the route's elevation, in which case the response includes a
ride_time recomputed against the fresh surface and the rider's settings -
this is what makes the planner's live estimate surface-aware without
adding a Valhalla round trip to `/api/route` itself, which computes its
ride_time paved-equivalent first. The frontend refetches in its own effect
whenever the route or preset changes and writes both results straight onto
the route object - targeted property writes rather than replacing the
whole `route`, so the effect does not retrigger its own dependency.
`import_route` and `POST /api/routes/{id}/reverse` compute the breakdown
once against their final snapshot's legs and attach it the same way, since
neither goes through the planner's own fetch. Saved snapshots carry
`surface: SurfaceBreakdown | null`; the `None` default is what keeps routes
saved before this existed - and any route whose edge_walk match failed -
parsing without a migration of their own.

## Gradient colouring

`frontend/src/lib/gradient.ts` computes a gradient band per step between
consecutive elevation samples (point-to-point, no smoothing - it is a
coarse visual, not climb detection) and merges same-band runs. The
elevation chart draws one coloured stroke per run; the map draws the same
runs as a `route-gradient` GeoJSON FeatureCollection, one feature per
segment with a `band` property consumed by a `match` expression on
`line-color` - the same colouring idiom the POI layer uses. The original
single-feature `route` source is untouched and keeps driving dragging and
hit-testing (`route-hit`); when a route has no elevation, the gradient
source falls back to one unbanded feature so the line renders in the plain
colour rather than disappearing.

## Climb detection

`backend/app/services/climbs.py`'s `detect_climbs` is a pure function of
`elevation`, called inline in `ValhallaClient.route`/`trace_route` right
after `_elevation_profile`, and in `services/import_routes.py` wherever the
elevation fallback profile is built - so a route's `climbs` always describe
whatever elevation the snapshot actually carries. It lives in the backend,
unlike gradient banding, because it needs its own smoothing: the raw
`/height` profile (and imported-file elevation) is noisy enough sample to
sample that an unsmoothed grade series segments an ordinary ride into
dozens of spurious sub-500 m "climbs". `gradient.ts`'s point-to-point bands
and `valhalla.py`'s `ascent_descent` stay deliberately unsmoothed - fine
for a coarse visual or a cumulative total - but climb boundaries need
better than that, and doing the smoothing once here gives it real pytest
coverage instead of re-deriving it per client.

The elevation array is at most 500 points however long the route, so it is
first resampled onto a uniform 25 m grid (`RESAMPLE_STEP_M`) by linear
interpolation, then smoothed with a centred 150 m moving average
(`SMOOTH_WINDOW_M`) - wide enough to iron out `/height`'s noise, narrow
enough not to blur a real climb's start and end by more than a grid cell or
two. Consecutive samples with grade >= 3% (`CLIMB_MIN_GRADE_PCT`) form raw
climbing runs; a dip between two runs is merged into one climb (a roller,
not the end of it) only when it is both short (< 500 m, `MERGE_MAX_GAP_M`)
and shallow (< 15 m lost, `MERGE_MAX_GAP_LOSS_M`) - either threshold alone
would either merge a real valley between two climbs or split a climb at
every cattle grid. Survivors shorter than 500 m or gaining under 20 m
(`MIN_CLIMB_LENGTH_M`/`MIN_CLIMB_GAIN_M`) are dropped; each remaining climb
is scored `length_m * avg_grade_pct**2 / 1000` and categorised HC/1/2/3/4
by `CATEGORY_THRESHOLDS`, checked highest-first. Those two minimums alone do
not guarantee a climb scores anything worth keeping - a climb sitting right
on both of them (500 m at exactly 20 m gain) scores only 8 - so a candidate
that passes them but still scores under category 4's floor of 20 is
discarded as an explicit final filter.

`RouteResponse.climbs` defaults to `[]`, and the `routes.climbs` column is
`NOT NULL DEFAULT '[]'` (migration 0009) - unlike `surface`, there is no
"match failed" state to represent with `None`, since `detect_climbs` always
returns a list. The frontend's `ClimbsList` panel and `ElevationProfile`'s
new inbound `hoveredClimb` prop (a translucent band behind the line) and
`MapView`'s `climb-highlight` source (a casing under the gradient-coloured
line, sliced from `routeLine` with `gradient.ts`'s own `coordsBetween`)
share one `hoveredClimbIndex` in `+page.svelte` - the same
props/hover-callback shape as the POI panel and map already use.
## Realistic ride time

`services/ride_time.py` turns Valhalla's flat routing duration into a
per-rider estimate, walking consecutive `ElevationPoint`s and, for each
segment, computing a speed from three inputs: `effective_flat_speed`
(the rider's `flat_speed_kmh` setting, nudged by FTP - cube root, since
aero power scales roughly with v^3, a one-sentence nudge rather than a
physics model), `gradient_factor` (a piecewise-linear multiplier over the
segment's grade %, capped at 1.35 downhill and floored at a deliberately
discontinuous 0.20 at and above 12% - nothing about a climb gets
meaningfully easier past that point), and `surface_factor` (one
route-level multiplier, the length-weighted mean of per-surface-type
factors over the route's whole `SurfaceBreakdown` - the breakdown is an
aggregate over the ride, not tied to any position, so it cannot vary
along the route the way gradient does). A floor of 2 km/h keeps a
degenerate segment from producing a near-zero time.

**Computed on read, never persisted.** `RouteResponse.ride_time` is a
`list[RideTimePoint]` (`dist_m`, cumulative `time_s`) built fresh on every
request that returns a route - `POST /api/route`, every saved-route read,
`POST /api/routes/import`, `.../duplicate`, `.../reverse`, and
`GET /api/shared/{token}` - from that route's own elevation and surface
plus the viewer's current `user_settings` row (anonymous share viewers get
the plain defaults, never the owner's). Nothing is added to the `routes`
table and no migration was needed: `_snapshot_fields`/`_apply_snapshot`
(the functions that decide what a save persists) were deliberately left
untouched, so a settings change changes the displayed time on every
already-saved route immediately, with no re-save and no backfill.

**The one invariant that matters: `duration_s` is never touched.** It
stays exactly what Valhalla returned, because it drives FIT course-point
timing (`services/fit.py`) and the Wahoo push payload - both need the
routing engine's own estimate for a head unit's cue timing to make sense,
not a rider-specific guess. `ride_time` is purely additive and
display-layer; nothing that reads `duration_s` was changed by this
feature.

## Isochrones

`POST /api/route/isochrone` takes an origin, up to four contours (each a
time in minutes or a distance in km - `IsochroneContour`'s `model_validator`
rejects both or neither being set) and the usual `preset`/`costing_options`
pair, and proxies straight to Valhalla `/isochrone` via
`ValhallaClient.isochrone`. The response - a GeoJSON `FeatureCollection` of
polygons, each carrying Valhalla's own `fill`/`color`/`fillOpacity`
properties per contour - is returned untouched (`IsochroneResponse` uses
`extra="allow"` and only declares `type`/`features`) so the frontend never
has to know Valhalla's exact styling keys to draw it: MapLibre's `fill-color`
and `line-color` paint properties read `['get', 'fill']` and `['get',
'color']` straight off each feature.

Anchored to wherever the rider right-clicked ("Isochrone from here" on the
map's context menu), not to the planned route - a route can be rerouted, or
have waypoints added and removed, without the isochrone changing, and it is
cleared only by `clear()` or the panel's own "Hide isochrone" button. The
frontend never fetches from an effect here either, matching the wind panel's
"only on an explicit click" convention, even though `/isochrone` never
leaves the LAN - a reachability query is still a real Valhalla round trip
per click, not a free live update while dragging.

**Important divergence, documented rather than reconciled:** "how far can I
get" comes entirely from Valhalla's own bicycle costing (the same
`bicycle_type`/`use_hills`/`avoid_bad_surfaces` bundle as `/route`), which
has its own internal speed model. It has no idea about a rider's
`user_settings` (weight, flat-road speed, FTP) or the gradient/surface-aware
model in [Realistic ride time](#realistic-ride-time) - the two estimates can
disagree about how far the same rider gets in the same 60 minutes, and nothing
here tries to make them agree. Reconciling them would mean either teaching
Valhalla's isochrone about a per-rider speed curve (it has no hook for one)
or reimplementing reachability search on top of the ride-time model ourselves
- both bigger than this feature - so the isochrone is presented as what it
is: Valhalla's own answer, not the planner's.

## Avoids

"Not that road": right-clicking a point on the route line itself (rather
than anywhere on the map, like the isochrone and loop entries) offers
"Avoid this road", which adds that point to `RouteRequest.exclude_locations`
and re-plans. Valhalla snaps each point to its nearest road and excludes it
from path computation - `ValhallaClient.route()` forwards the list to
`/route` unchanged when it is set, and omits the key entirely otherwise.
`RouteRequest.exclude_locations` is capped at 10 entries
(`Field(max_length=10)`), well under Valhalla's own service limit (11
accepted in testing against the dev instance) - this is a rider-facing
"not that road" list, not a bulk avoidance tool.

The map only offers the entry when the right-click landed on the
`route-hit` layer (`queryRenderedFeatures` at the click point) - avoiding a
road means nothing off the route, so the menu does not offer it there.
Avoided points are drawn as their own small circle layer (`avoid-points`),
independent of the context menu that added them, and listed as removable
chips near the toolbar.

**Session-only, deliberately**: `avoids` is planner state exactly like
`waypoints` - part of `PlannerSnapshot` (see the undo/redo design decision
below), reset by `clear()` and by loading a saved route - but it is never
written to `routes.exclude_locations` or any other persisted column. An
avoid is a *routing input*, and the route's saved geometry already reflects
whatever avoids shaped it while planning; storing the list separately would
only matter if the rider wanted the route re-planned later with the same
exclusions restored (e.g. after a road reopens), which nothing today asks
for. If that need shows up, persisting is a one-column, additive change -
nothing about the session-only design forecloses it.

**`exclude_polygons` deliberately deferred, not started**: Valhalla also
accepts excluded areas, not just points, for "avoid this whole
neighbourhood". Out of scope for this pass - `RouteRequest` carries no
field, placeholder, or comment for it - because a point-only "not that
road" already answers the common case (a closed bridge, a road with bad
surface) without a polygon-drawing UI, which is a materially bigger
feature.

## Loop generator

`services/loop.py` builds "N km loop from here" out of routing primitives
Valhalla already exposes - `/route` and `/trace_attributes` - since it has
no round-trip API of its own.

For each of `LOOP_BEARINGS` (8) evenly spaced compass bearings around the
origin, a via point is placed on a circle and its radius binary-searched
(`LOOP_MAX_ITERS` = 6 halvings of a `[0.4, 2.0] * target/2.5` radius
window - an out-and-back through one via point measures roughly 2.5x that
via's straight-line distance on real roads, measured rather than derived;
seeding from a full circle's `2*pi*r` put the radius for larger targets
outside the window entirely and every 60 km+ loop came back 15-23% short)
until the out-and-back route through it (`origin -> via -> origin`)
lands within `LOOP_TOLERANCE` (5%) of the target distance, or the iteration
budget runs out - the best (closest-distance) attempt seen is kept either
way, not just the last one tried. The 8 bearings run concurrently
(`asyncio.gather(..., return_exceptions=True)`); a bearing whose every
radius comes back unroutable (a coastal direction, say) simply contributes
nothing rather than failing the whole search - normal, not an error.

Each surviving bearing's best attempt is scored (lower is better):
distance error, a mild per-km ascent penalty, and a surface term from
`trace_attributes` (an unpaved fraction that is penalised for road/quiet
riding and rewarded for gravel, or ignored entirely when the breakdown
degrades to `None`). All the constants live at the top of `loop.py`,
deliberately in one place: distance error dominates by construction (a
typical hilly ride at 30 m/km scores only `ASCENT_WEIGHT * 30/30` = 0.15
against a 20% distance miss's 0.20), so ascent and surface only ever break
ties between similarly-close candidates.

The scored candidates are sorted and greedily deduplicated
(`LOOP_DEDUP_KM` = 3 km between kept via points) down to `LOOP_CANDIDATES`
(3) - adjacent bearings on a small target routinely converge on
essentially the same loop, and without this the results shown would often
just be one loop wearing three colours.

`POST /api/route/loop` wraps the whole search in `asyncio.wait_for` (25 s);
a timeout maps to 504 rather than leaving the rider staring at a spinner.
Each returned `LoopCandidate` carries a full `RouteResponse` snapshot, so
picking one in the frontend needs no second call back through the planner.

## Route alternates

`POST /api/route/alternates` asks Valhalla for other reasonable ways
between the same two points, via its own `alternates` option on `/route`.
This only ever means something for a single origin/destination pair - a
via-waypoint route has no well-defined "alternative" per leg, and Valhalla
does not document (or reliably support) requesting alternates for more than
two locations - so `AlternatesQuery.waypoints` is bounded to exactly 2 by
Pydantic, producing a 422 before Valhalla is ever asked, and the frontend
disables the "Alternatives" button (with an explanatory `title`) the moment
a route has more than two waypoints.

`ValhallaClient.route()` and the new `route_alternates()` share a private
`_parse_trip()` helper that turns one Valhalla `trip` object into a
`RouteResponse` (legs, elevation, ascent/descent, climbs) - `route()` parses
`data["trip"]`, `route_alternates()` parses that plus every entry of
`data.get("alternates", [])`. Valhalla's own top-level shape for this call
is `{"trip": {...}, "alternates": [{"trip": {...}}, ...]}`, undocumented
upstream; a real capture (`tests/fixtures/route_alternates_real.json`,
Tring -> Wendover, `"alternates": 2` requested) came back with only one
alternate - **fewer than requested, or none at all, is normal**, not an
error, and the response is simply as long as whatever Valhalla returned.

Both the primary and every alternate get a `ride_time` via `with_ride_time`
before the response leaves `/api/route/alternates`, so adopting any of them
in the frontend keeps a populated stats row rather than showing nothing
until the next reroute.

The frontend only ever fetches on the explicit "Alternatives" click -
matching the isochrone/loop/wind convention - and draws the results as
muted ghost lines on the map (clickable - clicking one adopts it, same as
its "Use" row in the small list beside the toolbar). Adopting an alternate
is the first real use of `PlannerSnapshot.routeOverride` (see
[Design decisions](#design-decisions)): the waypoints, preset and costing
options are unchanged, only the route *output* is being swapped for one
Valhalla already computed, so undo has to restore the previous response
directly rather than replay `reroute()`, which would just fetch the primary
again. The fetched list is invalidated - cleared, along with the ghost
lines - by an `$effect` over `waypoints`, guarded against firing the instant
a search populates the list by comparing against a plain (non-reactive)
snapshot of the waypoints the fetch was made against.

## Organising the library

Routes carry free-form organisation: `tags` (a Postgres `text[]` with a
GIN index, since filtering by tag is a containment query), `notes` and
`is_favourite`, all edited through `PATCH /api/routes/{id}`. Tags are
trimmed, de-duplicated and length-capped server-side so the library does
not accumulate near-identical tags.

`GET /api/routes` takes `q`, `tag`, `favourite`, `source`, `sort` and
`order`. Searching covers notes as well as names, since recording "cafe
at 12km" is only useful if it can be found again. Filtering is
server-side so it keeps working as the library outgrows one screen, and
`GET /api/routes/tags` returns the tags actually in use.

`POST /api/routes/{id}/duplicate` copies the stored snapshot rather than
re-routing, so a duplicate is identical even if the map data has moved on.
`POST /api/routes/{id}/reverse` does the opposite and deliberately
re-routes: one-way streets and turn instructions are direction-dependent,
so flipping the stored line would hand the rider cues for a journey they
are not making. An imported route has no meaningful waypoints, so its
track is reversed and map matched again. Both create a new route rather
than modifying the original.

Imported routes are marked `source = imported`. Their waypoints are only
the endpoints, so re-routing one in the planner would discard the
imported track: the planner asks before the first edit, and a route that
is re-routed stops being an import.


## Frontend structure

```
frontend/src/
├── routes/
│   ├── +layout.svelte           # auth guard + nav
│   ├── +page.svelte             # planner: state, reroute + save + wahoo orchestration
│   ├── login/+page.svelte       # password and/or SSO login
│   ├── library/+page.svelte     # saved routes, exports, wahoo, share
│   ├── admin/+page.svelte       # users/stats/config (admins)
│   └── s/[token]/+page.svelte   # public read-only shared route
└── lib/
    ├── api.ts                   # backend client + response types
    ├── polyline.ts              # polyline6 decoder
    ├── geo.ts                   # haversine, distance interpolation helpers
    ├── unsaved.svelte.ts        # $state singleton: unsaved-edits flag for the layout's file drop
    ├── history.svelte.ts        # $state singleton: undo/redo stack over planner inputs
    ├── map/MapView.svelte       # MapLibre init, layers, interactions, basemap toggle
    └── components/
        ├── PresetSelector.svelte
        ├── PresetSlidersPopover.svelte  # per-option costing sliders + saved presets
        ├── ElevationProfile.svelte   # custom SVG chart, no chart library
        ├── SurfaceBar.svelte         # paved/gravel/path stacked bar + cycleway %
        ├── ClimbsList.svelte         # categorised climbs, hover-synced with the profile
        ├── PlaceSearch.svelte        # keyboard-navigable search box
        ├── PoiPanel.svelte           # category chips + results along the route
        ├── WeatherPanel.svelte       # per-segment head/tailwind (env-gated)
        ├── LoopPanel.svelte          # loop candidates + preview
        ├── AlternatesPanel.svelte    # Valhalla alternates
        ├── ImportResults.svelte      # per-file upload result rows
        ├── LLMSettingsPanel.svelte   # /admin: endpoint, model browser, provider routing, test
        ├── AssistantPanel.svelte     # chat log, tool status, stop, proposal card
        └── WaypointList.svelte       # route-order list: reorder (drag or up/down), remove, reverse-geocoded names
```

State lives in `+page.svelte` with Svelte 5 runes; `MapView` receives
plain props plus callbacks. Route requests are aborted (AbortController)
when superseded, so rapid dragging never queues stale reroutes.

Drag-to-reroute works with an invisible wide "hit" twin of the route line:
mousedown on it suppresses map panning, a ghost point follows the cursor,
and on drop the grabbed vertex's leg determines the insertion position for
the new via waypoint.

## Backend structure

```
backend/app/
├── main.py                  # app factory, lifespan (valhalla client, wahoo worker), SPA static
├── config.py                # pydantic-settings, all env-driven
├── db.py                    # async engine + session factory
├── version.py               # APP_VERSION, read from pyproject.toml (never installed as a package)
├── models.py                # User, Session, Route, CustomPreset, WahooAccount,
│                            #   Place, Poi, CycleWay, SearchIndexMeta,
│                            #   UserSettings, LLMSettings
├── schemas.py               # request/response models
├── api/
│   ├── route.py             # /api/health, /api/config, /api/route, /api/route/surface,
│   │                        #   /api/route/isochrone, /api/route/loop, /api/route/alternates
│   ├── places.py            # /api/places: search, reverse, pois-along-route
│   ├── auth.py              # register/login/logout/me + OIDC flow
│   ├── routes.py            # route CRUD, GPX/FIT export, share links, ride-time wiring
│   ├── custom_presets.py    # CRUD for saved costing-slider bundles
│   ├── settings.py          # GET/PATCH /api/settings, get_or_default_settings
│   ├── deps.py              # DbDep / UserDep - session and current-user dependencies
│   ├── assistant.py         # /api/assistant: chat, chat/stream (SSE), suggest-name
│   ├── llm_admin.py         # /api/admin/llm: settings, model + provider browser, test probe
│   ├── wahoo.py             # connect/callback/status/push
│   └── admin.py             # /api/admin (admin accounts only)
├── alembic/                 # migrations, run on startup
└── services/
    ├── presets.py           # the three costing bundles + resolve_costing
    ├── polyline.py          # polyline6 decoder
    ├── valhalla.py          # httpx client, error mapping, elevation, ascent calc, _parse_trip (route/alternates)
    ├── ride_time.py         # gradient/surface/FTP model, computed on read only
    ├── loop.py              # "N km loop from here": bearing search + scoring + dedup
    ├── auth.py, oidc.py     # password hashing, sessions, OIDC client
    ├── gpx.py, fit.py       # exporters (FIT embeds maneuvers as course points)
    ├── importer.py          # GPX/TCX/FIT parsing for uploaded files
    ├── import_routes.py     # map matching an imported track back onto the network
    ├── places.py            # place search, reverse geocode, POIs along a route
    ├── climbs.py            # profile segmentation + HC/1-4 categorisation
    ├── geo.py               # shape concatenation and distance helpers
    ├── weather.py           # Open-Meteo-compatible forecast client (env-gated)
    ├── rate_limit.py        # in-process sliding window for the login endpoint
    ├── route_summary.py     # the stored share-page summary
    ├── llm.py               # OpenAI-compatible client: complete() and stream()
    ├── llm_config.py        # DB-over-env resolution into one LLMConfig
    ├── assistant/           # refs.py (handles), tools.py, turn.py (the loop),
    │                        #   prompt.py, naming.py
    └── wahoo.py, wahoo_queue.py  # Wahoo client + background push worker
```

The indexer is a sibling of `backend/`, with its own dependencies and
lockfile, because it shares nothing with the ASGI stack:

```
indexer/indexer/
├── prefilter.py             # osmium tags-filter pre-pass
├── categories.py            # tag -> category tables; also derives the filter
├── extract.py               # the two pyosmium passes
├── geometry.py              # way centroids, accent folding
├── db.py                    # COPY into staging, then publish
└── build.py                 # entrypoint
```

Error mapping: Valhalla connection failures surface as 503 ("routing
engine unavailable - it may still be building tiles"); Valhalla 4xx
responses surface as 422 with the Valhalla error message, plus a hint
about extract coverage when no roads are found near a waypoint.

## Design decisions

- **The assistant is handed handles, never coordinates**: every tool that
  places a point takes an opaque ref the server minted, validated against
  a pattern before any handler runs. A model that invents a latitude gets
  a validation error, not a waypoint. Prompts are the wrong place for a
  guarantee - this one holds whether or not the model cooperates, and it
  is what makes an OSM place name carrying an injection harmless: the
  thing it would need to do is unreachable through the schemas.
- **Valhalla behind the backend**: the routing engine is never exposed;
  one origin serves everything in prod, so no CORS in production and the
  Valhalla admin surface stays private.
- **Polyline6 to the browser** rather than raw coordinates: about 10x
  smaller responses on long routes; the decoder is 25 lines.
- **Elevation via /height** rather than baking heights into route
  responses: keeps the route call fast and lets elevation degrade
  gracefully when tiles were built without it.
- **Maneuvers pass through untouched**: FIT course points need Valhalla's
  original maneuver types, instructions, and shape indices; transforming
  them earlier would lose information.
- **Roundabouts become a single turn**: FIT has no roundabout course
  point. The FIT profile models roundabouts in its `turn_type` enum, but
  no course-file message carries that field, so a course can only say
  "turn right here". Valhalla's enter and exit maneuvers are folded into
  one cue, typed by the heading change through the roundabout and named
  "3rd exit onto Bulbourne Road" - Valhalla's own wording would truncate
  at FIT's 32-character limit and lose the exit number.
- **Snapshot-based persistence**: saved routes replay their stored
  response instead of re-routing, so a library route never silently
  changes when routing data is refreshed.
- **Resolved costing, not a preset foreign key**: a route saved with
  custom sliders stores the resolved `BicycleCostingOptions` bundle
  directly on `routes.costing_options` rather than a reference to a row in
  `custom_presets`. Renaming or deleting a saved preset therefore never
  changes what a route that was saved while it was selected actually
  routes with - the same reasoning as snapshot-based persistence, applied
  to costing instead of the route line.
- **In-process push queue** rather than a broker: a single worker task is
  plenty for a personal instance, serializes calls under Wahoo's rate
  limits, and route-row status makes it restart-safe without extra
  infrastructure.
- **Static SPA served by the backend** in prod: single container, single
  port, no SSR complexity - the app is a map tool, not a content site.
- **Compose profiles over separate files**: `dev` (hot reload, mounted
  source, Vite on 5173) and `prod` (single container on 17777) live in one
  docker-compose.yml.
- **A loop is an out-and-back through one via point**, not a hand-built
  closed shape: Valhalla routes `origin -> via -> origin` as two ordinary
  legs and, on real road topology, very often picks different streets for
  each direction - so what comes back reads as a loop rather than doubling
  back on itself, without the generator ever having to reason about the
  road graph itself. 8 bearings and 6 binary-search iterations per bearing
  is a deliberate budget, not a tuned optimum: enough spread to usually
  find a good loop in a few seconds, few enough LAN round trips that a
  rider is not left waiting minutes for one. Every scoring weight lives as
  a named constant at the top of `services/loop.py`, so retuning "how much
  a climb should hurt the score" is a one-line change, not a hunt through
  the search itself.
- **Undo/redo replays inputs, not outputs**: `history.svelte.ts` snapshots
  waypoints/preset/costing/source/avoidLocations before each mutation and undo/redo
  restores a snapshot by setting those inputs and calling `reroute()` -
  the same path an ordinary edit takes. `PlannerSnapshot.routeOverride` is
  the one deliberate exception, for actions that replace the route
  *output* without changing any input, where replaying inputs through
  `reroute()` cannot reproduce the exact response and the snapshot restores
  it directly instead. Adopting a [route alternate](#route-alternates) is
  the first real user of it: the waypoints/preset/costing are identical to
  what is already on screen, only the route Valhalla returned is being
  swapped for a different one it already computed, so there is no input
  change for `reroute()` to replay. Undo/redo bypass the imported-route
  `mayEdit()` confirm - time travel is not a fresh editorial decision -
  which is safe because `source` itself travels inside the snapshot, so
  undoing back into an imported route's territory still shows it as
  imported. The saved-route identity (`savedId`/`savedName`) travels in
  the snapshot too and is restored *exactly*, null included: undoing past
  the point a route was saved detaches it, because what is on screen is no
  longer that library row. Re-attaching without ever detaching was tried
  and is worse - it leaves the row attached while the waypoints wander
  off, and the next save overwrites a stored route with unrelated content.
  A duplicate row is visible and deletable; a silent overwrite is not.
- **A route is never persisted unless it matches its waypoints**:
  `routeStale` is set when a reroute fails and cleared wherever a matching
  route is assigned (reroute success, an adopted alternate, a chosen loop,
  a route loaded by id), and it disables saving. The failed reroute leaves
  the previous line on screen on purpose - a blank map on a transient 503
  is worse - so `loading` cannot be the guard: the catch that leaves the
  state inconsistent resets `loading` on its way out, and the rider is
  most likely to click Save precisely then, while reading the error.
  `routeStale` also suppresses `routeOverride` capture in
  `timeTravelSnapshot()`: a stale route restored verbatim by redo would
  arrive marked clean, since a routeOverride is by definition a route that
  matched when it was captured. Replaying `reroute()` is truer - it either
  produces a route that really does match, or fails and sets the flag
  again.
- **Waypoint list reordering is native HTML5 drag-and-drop plus
  always-present up/down buttons**, not a drag library: no new dependency,
  and the buttons are not a fallback - HTML5 drag-and-drop fires no events
  at all on touch, so they are the only reordering path on a phone, which
  is where this app is mostly used mid-ride.

## Ports

| Service | Port | Exposure |
|---------|------|----------|
| Vite dev server | 5173 | localhost only, dev profile |
| Backend | 17777 | localhost in dev; host port in prod (reverse proxy in front) |
| Valhalla | 8002 | compose network only, never published |
| Postgres | 5433 | 127.0.0.1 only (used by the backend test suite) |
