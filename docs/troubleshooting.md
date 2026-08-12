# Troubleshooting

Symptom-first. If something here doesn't match what you're seeing, check
[docs/architecture.md](architecture.md) for how the piece in question
actually works, or open an issue.

## Routing fails with "no suitable edges" or "no path could be found"

The backend appends "is this area covered by the loaded map extract?" to
this error for a reason - it usually is the answer. A waypoint (or the
whole route) falls outside the OSM extract Valhalla built its tiles from.
Check `VALHALLA_TILE_URL` in your `.env` against where you're actually
trying to route - the default extract is England, so a waypoint in
Scotland, Wales, or another country will fail this way. See
[docs/data.md](data.md#choosing-an-extract) for picking a wider extract.

## First start appears hung

This is expected, not broken. On first start, Valhalla downloads the
configured OSM extract and elevation data, then builds routing tiles -
which can take from a few minutes to an hour or more depending on
hardware, and the app can't route anything until it's done. Check
progress with:

```sh
docker compose logs -f valhalla
```

`docker compose ps` will show Valhalla as still starting for a long time;
its healthcheck has a 10-minute start period specifically because the
build can take that long or more. The app itself shows "routing engine
unavailable - it may still be building tiles" during this window. Once
tiles are built, subsequent starts reuse them and come up in seconds.

## Elevation chart is empty, and there are no climbs or gradient colours

`VALHALLA_BUILD_ELEVATION` is either set to `False`, or elevation data
failed to download for your extract. Without elevation data, the route
still routes and saves fine, but the elevation profile is empty, the
climbs list has nothing to show, the map line and chart stay in the plain
unbanded colour, and the ride-time estimate falls back to Valhalla's flat
routing duration instead of your per-rider estimate (which needs a
gradient to work from). Set `VALHALLA_BUILD_ELEVATION=True` (the
default) and rebuild the tiles - see
[docs/data.md](data.md#elevation).

## Search box is missing

The place index hasn't been built. Search, the POI panel, and the
cycle-network overlay are all hidden until you run the opt-in indexer:

```sh
docker compose --profile index run --rm indexer
```

See [docs/data.md](data.md#the-place-index-optional).

## Cycle overlay serves huge tiles after upgrading

If your place index was built before v0.3.0, its cycle routes are stored
as many disconnected line segments rather than merged ones, so the
network overlay serves tiles several times larger than they need to be.
Re-run the indexer once after upgrading - it's safe to run again with the
app up, and nothing else needs doing.

## Cycle-network coverage says "needs a re-index"

If your place index was built before Phase 10 (cycle-network coverage), it
has cycle routes but not the per-way lengths coverage needs
(`cycle_way_members`). `/api/coverage/cycle-network` says so explicitly
rather than reporting 0% - re-run the indexer once, the same command as
above, and it starts working. Nothing else needs doing: matching activities
onto OSM way ids does not depend on the place index at all (it only calls
Valhalla), so activities imported before the re-index are already matched
and their coverage becomes visible the moment the index catches up. Rides
imported before this feature existed have never been matched at all - use
the "Match older rides" button on /activities to backfill those.

## All-roads coverage says "needs a re-index"

Usually because you have not asked for it. Road ways are **off by
default**, so `/api/coverage/roads` says "needs a re-index" - rather than
a dishonest 0% - for any install that has never run the indexer with the
switch on:

```sh
INDEX_ROADS=true docker compose --profile index run --rm indexer
```

As with cycle-network coverage, matching does not depend on the index, so
activities matched before the re-index show their coverage the moment it
catches up.

This message is specifically "never built with the switch on" - it is not
shown just because your most recent refresh happened to omit
`INDEX_ROADS`. A refresh that omits it leaves a previously built road
table alone rather than deleting it, so coverage keeps answering off
whatever roads were indexed last time; it just will not reflect anything
OSM has changed since. See [docs/data.md](data.md#refreshing-data-monthly)
if you run all-roads coverage and want to keep it current.

**This re-index takes much longer than the ones before it - about
8 minutes on England, against ~37 seconds before.** Keeping every
bikeable road (not just the signed network) takes the filtered extract
from ~45 MB to ~470 MB; the indexer container's own peak memory stayed
under 800 MB even so, so this is a slower rebuild rather than one that
needs more RAM than a modest VPS has - see
[docs/data.md](data.md#the-place-index-optional) for the measured
numbers. It is still a one-shot batch job with no lock held for most of
its runtime (see [docs/architecture.md](architecture.md#the-place-index)),
just a longer one; plan for the wall-clock time on a small host rather
than running it unattended alongside everything else.

## After refreshing OSM data, the index is missing or stale

Order matters here. Refreshing routing data means wiping the
`valhalla_tiles` volume and letting Valhalla re-download and rebuild -
and that volume is also where the indexer reads its `.pbf` extract from.
Wiping it deletes the extract the indexer needs. Always refresh Valhalla
first, wait for tiles to finish building, *then* re-run the indexer - see
[docs/data.md](data.md#refreshing-data-monthly).

## Build fails with an osmium "invalid BlobHeader size" error

`VALHALLA_TILE_URL` is pointing at a dead Geofabrik path. Geofabrik
reorganised its UK tree; the old `europe/great-britain/...` paths now
redirect to the Geofabrik homepage instead of a `.pbf` file, and Valhalla
tries to parse the redirect page as OSM data. Use `europe/united-kingdom/...`
paths instead - see [docs/data.md](data.md#choosing-an-extract) for the
current URLs.

## "Connect Wahoo" button is missing, or the callback fails

Missing button: `WAHOO_CLIENT_ID`/`WAHOO_CLIENT_SECRET` aren't reaching
the backend - check `docker compose config` and restart after adding
them. Callback failing: Wahoo requires the registered redirect URI to
exactly match `<APP_URL>/api/wahoo/callback`, and requires HTTPS - a
plain-http `APP_URL` (or `localhost`) will not work, even for an
internal-only deployment. Full setup and troubleshooting:
[docs/wahoo-sync.md](wahoo-sync.md).

## Locked out of login with SSO down

`PASSWORD_AUTH_ENABLED=false` is only honoured when OIDC is fully
configured (`OIDC_ISSUER`, `OIDC_CLIENT_ID` and `OIDC_CLIENT_SECRET` all
set) - if OIDC isn't configured, the setting is ignored and password
login stays available regardless, so a misconfiguration can't lock
everyone out by accident. If you deliberately run SSO-only (OIDC
configured *and* `PASSWORD_AUTH_ENABLED=false`) and your identity
provider goes down, that genuinely does lock out password login - the
recovery is to unset `PASSWORD_AUTH_ENABLED` (or set it `true`) and
restart, which brings the password form back without touching accounts
or sessions.

## Weather panel says "Start time is beyond the forecast window"

Forecast providers only look ahead so far - Open-Meteo's own forecast
horizon is about 16 days. Pick a start time within that window. This
isn't a Moovelo limit; the weather service itself rejects the request and
the panel reports it rather than showing wrong numbers.

## Route assistant panel is missing

Both `LLM_BASE_URL` and `LLM_MODEL` must be set - either one alone leaves
the assistant off. The API key is deliberately not part of that check, so
a local endpoint that needs no key still enables the panel.

If you set them and the panel is still absent, check the variables are
actually reaching the container rather than only sitting in `.env`:

```sh
docker compose --profile prod exec backend printenv | grep '^LLM_'
```

An empty result on a deployment that copies files to the server rather
than pulling the repo usually means `docker-compose.yml` itself is out of
date - the four `LLM_*` passthrough lines arrived in v0.6.0, and without
them `.env` is read but never forwarded.

The endpoint can also be configured entirely from `/admin`, which
overrides the environment per field.

## Assistant replies "The assistant service rejected the request"

The endpoint refused the call, and the message after the colon is its own
wording passed through. Almost always the API key: missing, wrong, or
lacking credit. Gateways word this confusingly - OpenRouter answers an
unauthenticated request with "No cookie auth credentials found", which
means no valid key, not a cookie problem.

`/admin` has a test button that sends one tiny completion and reports
latency, cost, and whether tool calls came back.

## Assistant can't find places, or ignores the ones you name

The place index isn't built. The assistant is told so and will say it
plainly rather than inventing coordinates, but it can then only work from
the map centre and waypoints already on screen. Build the index (see
"Search box is missing" above) and it gains place lookup.

## Assistant never proposes a route, and just describes one instead

The model is not calling tools - either it does not support tool calling,
or its provider is not honouring it. Moovelo never accepts a route from
the model's prose, so a model that cannot call tools produces conversation
and nothing else. `/admin`'s model picker filters to tool-capable models
by default; pick one from that list.

## Assistant is slow

Latency is dominated by the number of tool round trips, not by the model's
raw speed - each one grows the conversation. A route-planning turn takes
roughly 10-25 seconds depending on model and provider, and that is normal.

On a gateway that routes between providers, which provider serves you
matters as much as the model: the same model can differ severalfold in
both speed and price. `LLM_PROVIDER_ORDER` expresses a preference (never a
hard pin - fallbacks stay on), and `/admin` lists per-provider price and
context. A model that streams its answer in many small pieces also *feels*
faster than one that delivers everything in a single chunk at the end,
even for the same total time.

## Assistant stops responding after a lot of use

Each account is limited to 30 assistant turns an hour. The limit exists
because every turn spends real money at whatever endpoint you configured,
and one turn can involve several completions. It clears on a rolling
window; nothing needs restarting.

## The first account I registered became admin - is that right?

Yes, by design: the first user to register on a fresh instance is made
admin automatically, and gets access to `/admin`. Plan for this on a
fresh install or after a `TRUNCATE users` - the very first signup, on
either password or SSO, claims the admin slot, so don't leave a
throwaway test account as the first registration on an instance you mean
to keep.
