import uuid

from fastapi import APIRouter
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DbDep, UserDep
from app.models import UserSettings
from app.schemas import (
    DEFAULT_FLAT_SPEED_KMH,
    DEFAULT_WEIGHT_KG,
    RideTimeSuggestion,
    UserSettingsPatch,
    UserSettingsResponse,
)
from app.services.ride_calibration import suggest_flat_speed_kmh
from app.version import APP_VERSION

router = APIRouter(prefix="/api/settings")


async def get_or_default_settings(db: AsyncSession, user_id: uuid.UUID) -> UserSettingsResponse:
    """A user who never opens settings never gets a row: read-only defaults
    when none exists, rather than inserting one on every GET."""
    row = (
        await db.execute(select(UserSettings).where(UserSettings.user_id == user_id))
    ).scalar_one_or_none()
    if row is None:
        return UserSettingsResponse(
            weight_kg=DEFAULT_WEIGHT_KG,
            flat_speed_kmh=DEFAULT_FLAT_SPEED_KMH,
            ftp_watts=None,
            version=APP_VERSION,
        )
    return UserSettingsResponse(
        weight_kg=row.weight_kg,
        flat_speed_kmh=row.flat_speed_kmh,
        ftp_watts=row.ftp_watts,
        version=APP_VERSION,
    )


@router.get("")
async def get_settings(db: DbDep, user: UserDep) -> UserSettingsResponse:
    return await get_or_default_settings(db, user.id)


@router.get("/ride-time-suggestion")
async def ride_time_suggestion(db: DbDep, user: UserDep) -> RideTimeSuggestion | None:
    """A flat_speed_kmh value the caller's own matched rides imply, or null
    under services/ride_calibration.py's floor of usable rides. Only ever
    considers the caller's own rides and routes - suggest_flat_speed_kmh
    filters on user.id itself. Applying stays PATCH /api/settings; this
    endpoint has no write path."""
    settings = await get_or_default_settings(db, user.id)
    suggestion = await suggest_flat_speed_kmh(db, user.id, settings)
    if suggestion is None:
        return None
    return RideTimeSuggestion(
        suggested_kmh=suggestion.suggested_kmh,
        sample_size=suggestion.sample_size,
        current_kmh=suggestion.current_kmh,
    )


@router.patch("")
async def update_settings(
    body: UserSettingsPatch, db: DbDep, user: UserDep
) -> UserSettingsResponse:
    row = (
        await db.execute(select(UserSettings).where(UserSettings.user_id == user.id))
    ).scalar_one_or_none()
    changes = body.model_dump(exclude_unset=True)
    if row is None:
        row = UserSettings(
            user_id=user.id,
            weight_kg=changes.get("weight_kg", DEFAULT_WEIGHT_KG),
            flat_speed_kmh=changes.get("flat_speed_kmh", DEFAULT_FLAT_SPEED_KMH),
            ftp_watts=changes.get("ftp_watts"),
        )
        db.add(row)
        try:
            await db.commit()
        except IntegrityError:
            # Two concurrent first PATCHes from the same user both pass the
            # select above (neither row exists yet) and both try to insert
            # the same user_id primary key; the loser hits this rather than
            # blocking on the winner. Recover instead of 500ing: the row now
            # exists, so re-select it and apply this request's changes on
            # top rather than losing them.
            await db.rollback()
            row = (
                await db.execute(select(UserSettings).where(UserSettings.user_id == user.id))
            ).scalar_one()
            for field, value in changes.items():
                setattr(row, field, value)
            await db.commit()
    else:
        for field, value in changes.items():
            setattr(row, field, value)
        await db.commit()
    await db.refresh(row)
    return UserSettingsResponse(
        weight_kg=row.weight_kg,
        flat_speed_kmh=row.flat_speed_kmh,
        ftp_watts=row.ftp_watts,
        version=APP_VERSION,
    )
