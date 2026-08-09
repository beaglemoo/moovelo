import secrets as py_secrets
from typing import Annotated, Any

from fastapi import APIRouter, Cookie, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr, Field

from app.api.deps import DbDep, UserDep
from app.config import settings
from app.services import auth as auth_service
from app.services import oidc, rate_limit
from app.services.auth import SESSION_COOKIE

router = APIRouter(prefix="/api/auth")

OIDC_STATE_COOKIE = "bikegps_oidc_state"


class Credentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)


class UserOut(BaseModel):
    email: str
    is_admin: bool


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_ttl_days * 86400,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
    )


@router.get("/status")
async def status(db: DbDep) -> dict[str, Any]:
    return {
        "setup_required": await auth_service.user_count(db) == 0,
        "signups_enabled": settings.signups_enabled,
        "password_login": settings.password_login_active,
        "oidc": (
            {"enabled": True, "name": settings.oidc_provider_name}
            if settings.oidc_enabled
            else {"enabled": False, "name": None}
        ),
    }


@router.post("/register")
async def register(body: Credentials, response: Response, db: DbDep) -> UserOut:
    if not settings.password_login_active:
        raise HTTPException(status_code=403, detail="Password login is disabled - use SSO")
    first_user = await auth_service.user_count(db) == 0
    if not first_user and not settings.signups_enabled:
        raise HTTPException(status_code=403, detail="Signups are disabled")
    if await auth_service.get_user_by_email(db, body.email) is not None:
        raise HTTPException(status_code=409, detail="Email already registered")
    user = await auth_service.create_user(db, body.email, body.password, is_admin=first_user)
    token = await auth_service.create_session(db, user)
    await db.commit()
    _set_session_cookie(response, token)
    return UserOut(email=user.email, is_admin=user.is_admin)


def _login_rate_limit_key(request: Request, email: str) -> str:
    """(peer address, submitted email).

    The app runs with no proxy-header trust configured anywhere (no
    ProxyHeadersMiddleware, no X-Forwarded-For parsing) - see main.py - so
    `request.client` is whatever TCP peer connected to uvicorn. Behind the
    documented prod deployment (deploy/README.md's Caddy `reverse_proxy`),
    that peer is Caddy itself for every request, not the visitor's real
    address: in that topology every login shares the same IP component, and
    the email half of the key is what actually separates callers. Trusting a
    forwarded-for header instead would need a configured, trusted proxy
    allowlist - without one, a client can put any address it likes in that
    header and pick its own rate-limit bucket, which is worse than ignoring
    it. The IP component still earns its place for direct (non-proxied) dev
    and test setups, and for any future deployment that exposes uvicorn
    itself.
    """
    ip = request.client.host if request.client else "unknown"
    return f"{ip}|{email.strip().lower()}"


@router.post("/login")
async def login(body: Credentials, request: Request, response: Response, db: DbDep) -> UserOut:
    if not settings.password_login_active:
        raise HTTPException(status_code=403, detail="Password login is disabled - use SSO")
    if not rate_limit.check_and_record(_login_rate_limit_key(request, body.email)):
        raise HTTPException(
            status_code=429, detail="Too many login attempts - try again in a few minutes"
        )
    user = await auth_service.get_user_by_email(db, body.email)
    if user is None or not auth_service.verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    token = await auth_service.create_session(db, user)
    await db.commit()
    _set_session_cookie(response, token)
    return UserOut(email=user.email, is_admin=user.is_admin)


@router.post("/logout")
async def logout(
    response: Response,
    db: DbDep,
    bikegps_session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> dict[str, str]:
    if bikegps_session:
        await auth_service.delete_session(db, bikegps_session)
        await db.commit()
    response.delete_cookie(SESSION_COOKIE)
    return {"status": "ok"}


@router.get("/me")
async def me(user: UserDep) -> UserOut:
    return UserOut(email=user.email, is_admin=user.is_admin)


@router.get("/oidc/login")
async def oidc_login() -> RedirectResponse:
    if not settings.oidc_enabled:
        raise HTTPException(status_code=404, detail="OIDC is not configured")
    state = oidc.new_state()
    response = RedirectResponse(await oidc.authorize_url(state), status_code=302)
    response.set_cookie(
        OIDC_STATE_COOKIE,
        state,
        max_age=600,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
    )
    return response


@router.get("/oidc/callback")
async def oidc_callback(
    code: str,
    state: str,
    db: DbDep,
    bikegps_oidc_state: Annotated[str | None, Cookie(alias=OIDC_STATE_COOKIE)] = None,
) -> RedirectResponse:
    if not settings.oidc_enabled:
        raise HTTPException(status_code=404, detail="OIDC is not configured")
    if bikegps_oidc_state is None or not py_secrets.compare_digest(state, bikegps_oidc_state):
        raise HTTPException(status_code=400, detail="OIDC state mismatch")

    email = (await oidc.exchange_code_for_email(code)).strip().lower()
    user = await auth_service.get_user_by_email(db, email)
    if user is None:
        first_user = await auth_service.user_count(db) == 0
        if not first_user and not settings.signups_enabled:
            raise HTTPException(status_code=403, detail="No account for this identity")
        # OIDC-provisioned users get an unusable random password.
        user = await auth_service.create_user(
            db, email, py_secrets.token_urlsafe(32), is_admin=first_user
        )
    token = await auth_service.create_session(db, user)
    await db.commit()

    response = RedirectResponse("/", status_code=302)
    response.delete_cookie(OIDC_STATE_COOKIE)
    _set_session_cookie(response, token)
    return response
