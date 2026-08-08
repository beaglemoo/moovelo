import uuid

from fastapi import APIRouter, HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.api.deps import DbDep, UserDep
from app.models import CustomPreset
from app.schemas import (
    BicycleCostingOptions,
    CustomPresetCreate,
    CustomPresetPatch,
    CustomPresetResponse,
)

router = APIRouter(prefix="/api/custom-presets")

# A slider popover, not a preset manager: past a couple of dozen the list
# stops being faster to scan than just moving the sliders.
MAX_CUSTOM_PRESETS = 20


def _response(preset: CustomPreset) -> CustomPresetResponse:
    return CustomPresetResponse(
        id=preset.id, name=preset.name, options=BicycleCostingOptions(**preset.options)
    )


async def _get_owned(db: DbDep, user: UserDep, preset_id: uuid.UUID) -> CustomPreset:
    preset = (
        await db.execute(
            select(CustomPreset).where(
                CustomPreset.id == preset_id, CustomPreset.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if preset is None:
        raise HTTPException(status_code=404, detail="Preset not found")
    return preset


@router.get("")
async def list_presets(db: DbDep, user: UserDep) -> list[CustomPresetResponse]:
    rows = (
        await db.execute(
            select(CustomPreset).where(CustomPreset.user_id == user.id).order_by(CustomPreset.name)
        )
    ).scalars()
    return [_response(r) for r in rows]


@router.post("", status_code=201)
async def create_preset(body: CustomPresetCreate, db: DbDep, user: UserDep) -> CustomPresetResponse:
    count = await db.scalar(
        select(func.count()).select_from(CustomPreset).where(CustomPreset.user_id == user.id)
    )
    if (count or 0) >= MAX_CUSTOM_PRESETS:
        raise HTTPException(
            status_code=422, detail=f"You already have {MAX_CUSTOM_PRESETS} saved presets."
        )
    preset = CustomPreset(user_id=user.id, name=body.name, options=body.options.model_dump())
    db.add(preset)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="You already have a preset with that name."
        ) from exc
    await db.refresh(preset)
    return _response(preset)


@router.patch("/{preset_id}")
async def update_preset(
    preset_id: uuid.UUID, body: CustomPresetPatch, db: DbDep, user: UserDep
) -> CustomPresetResponse:
    preset = await _get_owned(db, user, preset_id)
    if body.name is not None:
        preset.name = body.name
    if body.options is not None:
        preset.options = body.options.model_dump()
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="You already have a preset with that name."
        ) from exc
    await db.refresh(preset)
    return _response(preset)


@router.delete("/{preset_id}", status_code=204)
async def delete_preset(preset_id: uuid.UUID, db: DbDep, user: UserDep) -> None:
    preset = await _get_owned(db, user, preset_id)
    await db.delete(preset)
    await db.commit()
