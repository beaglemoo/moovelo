# Wahoo sync setup

Moovelo can push saved routes to your Wahoo account so they appear on
your ELEMNT (or any Wahoo head unit that syncs routes) after its next
WiFi sync - as FIT courses with turn-by-turn cues at every junction.

This guide takes you from nothing to a route on your handlebars.

## How it works, briefly

Moovelo talks to the Wahoo Cloud API: you authorize it once via OAuth,
and from then on "Send to Wahoo" uploads the route's FIT file (with
Valhalla's maneuvers embedded as course points) to Wahoo's cloud. The
ELEMNT downloads it from there when it syncs - your Moovelo instance and
the head unit never talk to each other directly.

Two useful consequences:

- **Your instance does not need to be exposed to the internet.** The
  OAuth redirect happens in your browser, which can resolve internal
  hostnames, and every API call is outbound from the backend. A LAN-only
  deployment behind an internal reverse proxy works fully.
- **HTTPS is still required** - Wahoo rejects plain-http redirect URIs
  (and `localhost`), so put your instance behind a reverse proxy with
  TLS even if it is internal-only.

## 1. Register a Wahoo developer app

1. Sign in at [developers.wahooligan.com](https://developers.wahooligan.com)
   with your normal Wahoo account and choose to register a new app.
2. Fill in the form:
   - **Environment**: Sandbox. Sandbox apps are approved instantly and
     work fully with your own Wahoo account. Production approval is a
     manual review and only matters if *other people* will connect
     their Wahoo accounts to your instance.
   - **App type**: confidential (not PKCE) - Moovelo keeps the secret
     server-side.
   - **Description**: maximum 191 characters (the form rejects more).
   - **Callback / redirect URI**: `<APP_URL>/api/wahoo/callback`, e.g.
     `https://bike.example.com/api/wahoo/callback`. Must be https and
     must exactly match your instance's `APP_URL` setting.
   - **Scopes** (if asked): `user_read routes_read routes_write`.
3. After creating the app you get a **Client ID** and **Secret**.

## 2. Configure Moovelo

Add to your `.env`:

```sh
APP_URL=https://bike.example.com     # external base URL of your instance
WAHOO_CLIENT_ID=...
WAHOO_CLIENT_SECRET=...
```

Restart the stack (`docker compose --profile prod up -d`). With the
variables unset, all Wahoo UI and endpoints are hidden - so if you do
not see a "Connect Wahoo" button after restarting, the backend did not
receive the env vars.

## 3. Connect your account

Open **Library** and click **Connect Wahoo**. You are sent to Wahoo's
login/consent page; after authorizing, you land back in the library with
your athlete name in the header. Tokens are stored per-user in the
database and refreshed automatically. **Disconnect** deletes them.

## 4. Send a route

Every saved route gets a **Send to Wahoo** action (library rows and the
planner toolbar). Pushes are queued in the background:

| Badge | Meaning |
|-------|---------|
| queued | Waiting for the background worker |
| pushing | Uploading to Wahoo's cloud |
| synced | On Wahoo's cloud; hover for the timestamp |
| error | Failed; hover for the reason, click Send again to retry |

Re-pushing an edited or renamed route **updates the same course** on
Wahoo's side rather than creating a duplicate (the route's UUID is used
as Wahoo's `external_id`).

## 5. Get it on the head unit

Sync the ELEMNT (WiFi sync from the device, or open the companion app).
The route appears under Routes with the name you gave it, and because
the FIT file carries course points, the ELEMNT shows turn-by-turn cues
as you ride.

## Troubleshooting

- **No "Connect Wahoo" button**: `WAHOO_CLIENT_ID`/`WAHOO_CLIENT_SECRET`
  not reaching the backend - check `docker compose config` output and
  restart.
- **Wahoo shows an error page during connect**: the registered callback
  URI does not exactly match `<APP_URL>/api/wahoo/callback`. Check
  scheme, host, and path character-for-character.
- **error badge, "token refresh failed"**: Wahoo revoked or expired the
  refresh token. Disconnect and reconnect the account.
- **Pushes seem slow with many routes**: intentional - pushes are
  serialized through a single worker and back off on HTTP 429, because
  the sandbox rate limits are 25 requests/5 min, 100/hour, 250/day.
- **HTTP 422 from Wahoo**: should not happen - Moovelo already works
  around the API's undocumented requirements (the route upload must be
  a multipart file literally named `route.fit`, not the base64 data-URI
  the docs describe, and `workout_type_family_id`/`start_lat`/
  `start_lng` are mandatory). If you see a 422, open an issue with the
  backend logs.
- **Route on Wahoo but not on the ELEMNT**: the head unit has not
  synced yet, or is paired to a different Wahoo account than the one
  you authorized.
