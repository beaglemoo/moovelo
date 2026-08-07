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
    request_body {
        max_size 21MB
    }
    reverse_proxy <docker-host>:17777
}
```

`request_body` is worth setting: route import accepts files up to 20 MB,
and the app refuses anything larger, but rejecting it at the proxy stops
an oversized upload being carried across the network at all.

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
