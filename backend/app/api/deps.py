from typing import Annotated

from fastapi import Cookie, Depends, HTTPException
from sqlalchemy.exc import InvalidRequestError
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


async def refresh_or_404(db: AsyncSession, instance: object, detail: str) -> None:
    """Re-read a row that another request may have deleted in the meantime.

    Both call sites do slow work between their own commit and their
    response - re-matching every ride linked to a route, or re-running one
    match - and the row can be deleted in that gap. A plain `db.refresh`
    then raises ObjectDeletedError, which surfaces as a 500 for a request
    whose own write already succeeded.

    404 is the honest answer: the caller's edit did happen, but the thing
    they asked to be shown no longer exists, and inventing a body from the
    stale in-memory copy would report a resource that is gone.
    """
    try:
        await db.refresh(instance)
    except InvalidRequestError as exc:
        raise HTTPException(status_code=404, detail=detail) from exc
