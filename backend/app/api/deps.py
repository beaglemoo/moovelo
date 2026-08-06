from typing import Annotated

from fastapi import Cookie, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import User
from app.services.auth import SESSION_COOKIE, get_session_user

DbDep = Annotated[AsyncSession, Depends(get_db)]


async def current_user(
    db: DbDep,
    bikegps_session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> User:
    if bikegps_session is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = await get_session_user(db, bikegps_session)
    if user is None:
        raise HTTPException(status_code=401, detail="Session expired")
    await db.commit()
    return user


UserDep = Annotated[User, Depends(current_user)]
