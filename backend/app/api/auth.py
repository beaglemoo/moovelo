from typing import Annotated

from fastapi import APIRouter, Cookie, HTTPException, Response
from pydantic import BaseModel, EmailStr, Field

from app.api.deps import DbDep, UserDep
from app.config import settings
from app.services import auth as auth_service
from app.services.auth import SESSION_COOKIE

router = APIRouter(prefix="/api/auth")


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
async def status(db: DbDep) -> dict[str, bool]:
    return {
        "setup_required": await auth_service.user_count(db) == 0,
        "signups_enabled": settings.signups_enabled,
    }


@router.post("/register")
async def register(body: Credentials, response: Response, db: DbDep) -> UserOut:
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


@router.post("/login")
async def login(body: Credentials, response: Response, db: DbDep) -> UserOut:
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
