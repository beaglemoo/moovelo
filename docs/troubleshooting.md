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

## The first account I registered became admin - is that right?

Yes, by design: the first user to register on a fresh instance is made
admin automatically, and gets access to `/admin`. Plan for this on a
fresh install or after a `TRUNCATE users` - the very first signup, on
either password or SSO, claims the admin slot, so don't leave a
throwaway test account as the first registration on an instance you mean
to keep.
