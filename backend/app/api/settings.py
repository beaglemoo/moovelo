import uuid

from fastapi import APIRouter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DbDep, UserDep
from app.models import UserSettings
from app.schemas import (
    DEFAULT_FLAT_SPEED_KMH,
    DEFAULT_WEIGHT_KG,
    UserSettingsPatch,
    UserSettingsResponse,
)

router = APIRouter(prefix="/api/settings")


async def get_or_default_settings(db: AsyncSession, user_id: uuid.UUID) -> UserSettingsResponse:
    """A user who never opens settings never gets a row: read-only defaults
    when none exists, rather than inserting one on every GET."""
    row = (
        await db.execute(select(UserSettings).where(UserSettings.user_id == user_id))
    ).scalar_one_or_none()
    if row is None:
        return UserSettingsResponse(
            weight_kg=DEFAULT_WEIGHT_KG, flat_speed_kmh=DEFAULT_FLAT_SPEED_KMH, ftp_watts=None
        )
    return UserSettingsResponse(
        weight_kg=row.weight_kg, flat_speed_kmh=row.flat_speed_kmh, ftp_watts=row.ftp_watts
    )


@router.get("")
async def get_settings(db: DbDep, user: UserDep) -> UserSettingsResponse:
    return await get_or_default_settings(db, user.id)


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
    else:
        for field, value in changes.items():
            setattr(row, field, value)
    await db.commit()
    await db.refresh(row)
    return UserSettingsResponse(
        weight_kg=row.weight_kg, flat_speed_kmh=row.flat_speed_kmh, ftp_watts=row.ftp_watts
    )
