"""Wahoo Cloud API client: OAuth2, token refresh, and route pushes."""

import asyncio
import logging
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException

from app.config import settings
from app.models import Route, WahooAccount
from app.services.fit import build_fit
from app.services.polyline import decode_polyline6

logger = logging.getLogger(__name__)

WAHOO_BASE = "https://api.wahooligan.com"
SCOPES = "user_read routes_read routes_write"
REFRESH_MARGIN = timedelta(minutes=10)
MAX_ATTEMPTS = 3


class WahooError(Exception):
    """Terminal push failure whose message is shown to the user."""


def redirect_uri() -> str:
    return f"{settings.app_url}/api/wahoo/callback"


def new_state() -> str:
    return secrets.token_urlsafe(24)


def authorize_url(state: str) -> str:
    params = urlencode(
        {
            "client_id": settings.wahoo_client_id,
            "redirect_uri": redirect_uri(),
            "response_type": "code",
            "scope": SCOPES,
            "state": state,
        }
    )
    return f"{WAHOO_BASE}/oauth/authorize?{params}"


async def exchange_code(code: str) -> dict[str, object]:
    """Exchange the auth code; returns token payload plus athlete info."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            f"{WAHOO_BASE}/oauth/token",
            data={
                "client_id": settings.wahoo_client_id,
                "client_secret": settings.wahoo_client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri(),
            },
        )
        if response.status_code != 200:
            raise HTTPException(status_code=502, detail="Wahoo token exchange failed")
        tokens: dict[str, object] = response.json()

        user_response = await client.get(
            f"{WAHOO_BASE}/v1/user",
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
        tokens["athlete"] = user_response.json() if user_response.status_code == 200 else {}
        return tokens


def apply_tokens(account: WahooAccount, tokens: dict[str, object]) -> None:
    account.access_token = str(tokens["access_token"])
    account.refresh_token = str(tokens["refresh_token"])
    expires_raw = tokens.get("expires_in", 7200)
    expires_in = int(expires_raw) if isinstance(expires_raw, int | str) else 7200
    account.expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)


async def refresh_tokens(account: WahooAccount) -> None:
    """Refresh the account's tokens in place (caller commits)."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            f"{WAHOO_BASE}/oauth/token",
            data={
                "client_id": settings.wahoo_client_id,
                "client_secret": settings.wahoo_client_secret,
                "grant_type": "refresh_token",
                "refresh_token": account.refresh_token,
            },
        )
    if response.status_code != 200:
        raise WahooError("Wahoo token refresh failed - reconnect your account")
    apply_tokens(account, response.json())


async def ensure_fresh(account: WahooAccount) -> None:
    if account.expires_at - datetime.now(UTC) < REFRESH_MARGIN:
        await refresh_tokens(account)


def _route_payload(route: Route) -> dict[str, str | int | float]:
    start_lat, start_lng = decode_polyline6(route.legs[0]["geometry"])[0]
    return {
        "route[name]": route.name,
        "route[external_id]": str(route.id),
        "route[provider_updated_at]": route.updated_at.isoformat(),
        "route[workout_type_family_id]": 0,  # 0 = Biking
        "route[start_lat]": start_lat,
        "route[start_lng]": start_lng,
        "route[distance]": route.distance_m,
        "route[ascent]": route.ascent_m,
        "route[descent]": route.descent_m,
    }


async def push_route(route: Route, account: WahooAccount) -> str:
    """Create or update the route on Wahoo; returns the Wahoo route id."""
    await ensure_fresh(account)
    payload = _route_payload(route)
    # Wahoo validates the upload by filename extension and rejects the
    # base64 data-URI form its docs describe ("bin" not allowed), so the
    # FIT file goes up as a multipart attachment named route.fit.
    fit_bytes = build_fit(
        route.name, route.legs, route.elevation, route.duration_s, route.updated_at
    )
    files = {"route[file]": ("route.fit", fit_bytes, "application/octet-stream")}

    if route.wahoo_route_id:
        method, url = "PUT", f"{WAHOO_BASE}/v1/routes/{route.wahoo_route_id}"
    else:
        method, url = "POST", f"{WAHOO_BASE}/v1/routes"

    refreshed_once = False
    async with httpx.AsyncClient(timeout=30.0) as client:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            response = await client.request(
                method,
                url,
                data=payload,
                files=files,
                headers={"Authorization": f"Bearer {account.access_token}"},
            )
            if response.status_code in (200, 201):
                body = response.json()
                return str(body.get("id", route.wahoo_route_id or ""))
            if response.status_code == 404 and method == "PUT":
                # The Wahoo-side route vanished; recreate it.
                method, url = "POST", f"{WAHOO_BASE}/v1/routes"
                continue
            if response.status_code == 401 and not refreshed_once:
                refreshed_once = True
                await refresh_tokens(account)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == MAX_ATTEMPTS:
                    break
                retry_after = float(response.headers.get("Retry-After", 2**attempt))
                logger.warning(
                    "Wahoo push retry %d for route %s after HTTP %d",
                    attempt,
                    route.id,
                    response.status_code,
                )
                await asyncio.sleep(min(retry_after, 60.0))
                continue
            raise WahooError(f"Wahoo rejected the route (HTTP {response.status_code})")
    raise WahooError("Wahoo is rate-limiting or unavailable - try again later")
