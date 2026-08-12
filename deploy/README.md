# Production deployment

The prod compose profile serves the whole app (backend + built frontend)
from one container on port 17777. Run it behind a reverse proxy that
terminates TLS.

```sh
cp .env.example .env
docker compose --profile prod up -d --build
```

## Caddy

Minimal snippet (Caddy handles TLS automatically):

```caddyfile
bike.example.com {
    handle /api/activities/import/archive* {
        request_body {
            max_size 501MiB
        }
        reverse_proxy <docker-host>:17777
    }
    handle {
        request_body {
            max_size 21MiB
        }
        reverse_proxy <docker-host>:17777
    }
}
```

`request_body` is worth setting: a single route or ride is capped at 20 MiB
(`20 * 1024 * 1024` bytes) and the app refuses anything larger, but
rejecting it at the proxy stops an oversized upload being carried across
the network at all.

**Use `MiB`, not `MB`.** Caddy's `max_size` follows go-humanize, where `MB`
is decimal (1 MB = 1,000,000 bytes) but the app's limits are binary
(`MAX_FILE_BYTES` / `MAX_ARCHIVE_BYTES` in `backend/app/services/importer.py`
and `activity_import.py` are `* 1024 * 1024`). A snippet written as `501MB`
looks like 1 MB of headroom over the app's 500 MiB archive cap, but
1,000,000 < 1,048,576 per "MB", so it is actually **~22 MB stricter** than
the app - real Strava exports between roughly 478 MiB and 500 MiB, which
the app would accept, get a `413` from the proxy before the app ever sees
them. Verified against Caddy 2: with `max_size 501MB`, a 510,000,000-byte
(486 MiB) archive upload - well inside the app's 500 MiB / 524,288,000-byte
cap - is rejected by the proxy itself; with `max_size 501MiB` the same
file, and a file at the app's exact 524,288,000-byte cap, both reach the
app intact.

**Scope it per route, and do not apply the 21 MiB limit everywhere.** A
Strava bulk export is one file holding hundreds of rides - the app caps it
at 500 MiB, and a blanket 21 MiB rule rejects every real export at the
proxy before the app ever sees it. Verified against Caddy 2: with the
limit unscoped, a 30 MB POST to the archive route is answered `413 Request
Entity Too Large` by the proxy itself, while a 10 MB one reaches the app.

If you never intend to import a Strava export, the blanket 21 MiB form is
fine and the `handle` block can go.

If you also run a self-hosted tile server (see
[docs/self-hosted-tiles.md](../docs/self-hosted-tiles.md)), give it a TLS
hostname too - the browser loads tiles directly, and an HTTPS app cannot
load HTTP tiles (mixed content):

```caddyfile
tiles.example.com {
    reverse_proxy <docker-host>:8080
}
```

Then in `.env`:

```
TILE_URL_CYCLOSM=https://tiles.example.com/tile/{z}/{x}/{y}.png
```

## Notes

- The backend serves the SPA for any unknown path, so no special
  fallback rules are needed in the proxy.
- Valhalla is internal to the compose network and needs no proxy rules.
- First start builds routing tiles (see [docs/data.md](../docs/data.md));
  the app returns 503 for route requests until Valhalla is healthy.
- Updating the app: `git pull && docker compose --profile prod up -d --build`.
  Routing tiles live in a named volume and survive rebuilds.
