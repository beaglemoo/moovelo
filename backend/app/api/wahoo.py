import logging
import secrets as py_secrets
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Cookie, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from app.api.deps import DbDep, UserDep, reload_or_404
from app.api.routes import get_owned_route
from app.config import settings
from app.models import WahooAccount
from app.schemas import RouteSummary
from app.services import wahoo
from app.services.wahoo_queue import queue

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/wahoo")

WAHOO_STATE_COOKIE = "bikegps_wahoo_state"


async def _account(db: DbDep, user: UserDep) -> WahooAccount | None:
    return (
        await db.execute(select(WahooAccount).where(WahooAccount.user_id == user.id))
    ).scalar_one_or_none()


@router.get("/status")
async def status(db: DbDep, user: UserDep) -> dict[str, Any]:
    account = await _account(db, user)
    athlete = None
    if account is not None:
        athlete = {
            "name": f"{account.athlete.get('first', '')} {account.athlete.get('last', '')}".strip()
            or account.athlete.get("email")
            or "connected"
        }
    return {
        "configured": settings.wahoo_enabled,
        "connected": account is not None,
        "athlete": athlete,
    }


@router.get("/connect")
async def connect(user: UserDep) -> RedirectResponse:
    if not settings.wahoo_enabled:
        raise HTTPException(status_code=404, detail="Wahoo is not configured")
    state = wahoo.new_state()
    response = RedirectResponse(wahoo.authorize_url(state), status_code=302)
    response.set_cookie(
        WAHOO_STATE_COOKIE,
        state,
        max_age=600,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
    )
    return response


@router.get("/callback")
async def callback(
    code: str,
    state: str,
    db: DbDep,
    user: UserDep,
    bikegps_wahoo_state: Annotated[str | None, Cookie(alias=WAHOO_STATE_COOKIE)] = None,
) -> RedirectResponse:
    if not settings.wahoo_enabled:
        raise HTTPException(status_code=404, detail="Wahoo is not configured")
    if bikegps_wahoo_state is None or not py_secrets.compare_digest(state, bikegps_wahoo_state):
        raise HTTPException(status_code=400, detail="Wahoo state mismatch")

    tokens = await wahoo.exchange_code(code)
    account = await _account(db, user)
    if account is None:
        account = WahooAccount(user_id=user.id, access_token="", refresh_token="")
        db.add(account)
    wahoo.apply_tokens(account, tokens)
    athlete = tokens.get("athlete")
    account.athlete = athlete if isinstance(athlete, dict) else {}
    try:
        await db.commit()
    except (StaleDataError, IntegrityError):
        # Two shapes, one answer. StaleDataError: a concurrent disconnect
        # deleted the account row between the read above and this write.
        # IntegrityError: there was no row, so this branch INSERTS one, and a
        # second connect got there first - the unique constraint on user_id
        # fires. The first version of this fix only caught the update path,
        # because the scenario in its own commit message was a disconnect;
        # the insert path is the one a double-clicked Connect actually takes.
        #
        # Caught here rather than left to main.py's app-wide 404 policy, for
        # the same reason current_user catches its own: this is a browser
        # landing at the end of an OAuth redirect chain, not a fetch(). The
        # blanket rule would replace the 302 with a bare JSON body and no
        # Location header, so the rider would end up looking at
        # {"detail": "Not found"} instead of anywhere. Nothing they asked for
        # is missing either - the connection they were making was removed
        # from under them, and the honest thing is to send them back to the
        # page they came from, where the Wahoo panel will show disconnected.
        #
        # A blanket policy is only safe while every layer beneath it means
        # the same thing by the error, and answers in the same medium.
        logger.info("wahoo account removed mid-callback for user %s", user.id)
        await db.rollback()

    response = RedirectResponse("/library", status_code=302)
    response.delete_cookie(WAHOO_STATE_COOKIE)
    return response


@router.post("/disconnect")
async def disconnect(db: DbDep, user: UserDep) -> dict[str, str]:
    account = await _account(db, user)
    if account is not None:
        await db.delete(account)
        await db.commit()
    return {"status": "ok"}


@router.post("/push/{route_id}")
async def push(route_id: uuid.UUID, db: DbDep, user: UserDep) -> RouteSummary:
    if not settings.wahoo_enabled:
        raise HTTPException(status_code=404, detail="Wahoo is not configured")
    if await _account(db, user) is None:
        raise HTTPException(status_code=400, detail="Connect your Wahoo account first")
    route = await get_owned_route(db, user, route_id)
    if route.wahoo_status in ("queued", "pushing"):
        raise HTTPException(status_code=409, detail="Push already in progress")
    route.wahoo_status = "queued"
    route.wahoo_error = None
    await db.commit()
    route = await reload_or_404(db, route, "Route not found")
    queue.enqueue(route.id)
    return RouteSummary.from_route(route)
