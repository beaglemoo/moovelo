"""Minimal OIDC authorization-code client (works with Pocket ID and peers)."""

import secrets
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException

from app.config import settings

_discovery_cache: dict[str, dict[str, Any]] = {}


def redirect_uri() -> str:
    return f"{settings.app_url}/api/auth/oidc/callback"


async def _discovery() -> dict[str, Any]:
    issuer = settings.oidc_issuer.rstrip("/")
    if issuer not in _discovery_cache:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{issuer}/.well-known/openid-configuration")
            response.raise_for_status()
            _discovery_cache[issuer] = response.json()
    return _discovery_cache[issuer]


def new_state() -> str:
    return secrets.token_urlsafe(24)


async def authorize_url(state: str) -> str:
    discovery = await _discovery()
    params = urlencode(
        {
            "response_type": "code",
            "client_id": settings.oidc_client_id,
            "redirect_uri": redirect_uri(),
            "scope": "openid email profile",
            "state": state,
        }
    )
    return f"{discovery['authorization_endpoint']}?{params}"


async def exchange_code_for_email(code: str) -> str:
    """Exchange the auth code and return the verified email from userinfo."""
    discovery = await _discovery()
    async with httpx.AsyncClient(timeout=15.0) as client:
        token_response = await client.post(
            discovery["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri(),
                "client_id": settings.oidc_client_id,
                "client_secret": settings.oidc_client_secret,
            },
        )
        if token_response.status_code != 200:
            raise HTTPException(status_code=502, detail="OIDC token exchange failed")
        access_token = token_response.json().get("access_token")
        if not access_token:
            raise HTTPException(status_code=502, detail="OIDC token response missing access_token")

        userinfo_response = await client.get(
            discovery["userinfo_endpoint"],
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if userinfo_response.status_code != 200:
            raise HTTPException(status_code=502, detail="OIDC userinfo request failed")
        email = userinfo_response.json().get("email")
        if not email:
            raise HTTPException(status_code=502, detail="OIDC userinfo has no email claim")
        return str(email)
