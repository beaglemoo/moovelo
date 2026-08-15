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


async def reload_or_404[ModelT](db: AsyncSession, instance: ModelT, detail: str) -> ModelT:
    """Re-read a row after committing, for a response, when it may be gone.

    Every endpoint that mutates an existing row, commits, and then renders
    that row has a window in which a concurrent DELETE removes it - and
    some of those windows are long: re-matching every ride linked to a
    route, or generating a share summary behind a 30s LLM timeout.

    `db.refresh` is the wrong tool for it twice over. It signals the
    missing row by raising, so the call site has to know which exception
    class - and that is genuinely hard to get right: SQLAlchemy raises a
    bare InvalidRequestError("Could not refresh instance") here, not the
    ObjectDeletedError the situation suggests, and the first version of
    this helper caught the wrong one. Worse, a failed refresh leaves the
    instance EXPIRED, so any later attribute read - including one inside
    the `except` block that is trying to log the failure - raises
    MissingGreenlet and escapes the handler meant to contain it. That is
    how a guarded refresh still produced a 500.

    `db.get(..., populate_existing=True)` re-reads the same identity-mapped
    instance and returns None when the row is gone. A return value, not an
    exception class to guess, and nothing is left expired on the way out.

    404 is the honest answer: the caller's write did happen, but the thing
    they asked to be shown no longer exists, and rendering the stale
    in-memory copy would report a resource that is not there.

    Read `instance` BEFORE anything can expire it - sessions here are
    created with expire_on_commit=False, so attributes survive the commit
    this is called after.
    """
    reloaded = await db.get(type(instance), instance.id, populate_existing=True)  # type: ignore[attr-defined]
    if reloaded is None:
        raise HTTPException(status_code=404, detail=detail)
    return reloaded
