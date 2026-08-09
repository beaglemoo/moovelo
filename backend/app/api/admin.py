import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select

from app.api.deps import DbDep, current_user
from app.config import settings
from app.models import Route, User, WahooAccount
from app.version import APP_VERSION

router = APIRouter(prefix="/api/admin")


async def admin_user(user: Annotated[User, Depends(current_user)]) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


AdminDep = Annotated[User, Depends(admin_user)]


class AdminUser(BaseModel):
    id: uuid.UUID
    email: str
    is_admin: bool
    created_at: datetime
    route_count: int
    wahoo_connected: bool


@router.get("/overview")
async def overview(db: DbDep, _admin: AdminDep) -> dict[str, Any]:
    route_counts = dict(
        (await db.execute(select(Route.user_id, func.count(Route.id)).group_by(Route.user_id)))
        .tuples()
        .all()
    )
    wahoo_users = set((await db.execute(select(WahooAccount.user_id))).scalars().all())
    users = (await db.execute(select(User).order_by(User.created_at))).scalars().all()
    return {
        "users": [
            AdminUser(
                id=u.id,
                email=u.email,
                is_admin=u.is_admin,
                created_at=u.created_at,
                route_count=route_counts.get(u.id, 0),
                wahoo_connected=u.id in wahoo_users,
            )
            for u in users
        ],
        "stats": {
            "user_count": len(users),
            "route_count": sum(route_counts.values()),
        },
        "config": {
            "signups_enabled": settings.signups_enabled,
            "password_login": settings.password_login_active,
            "oidc_enabled": settings.oidc_enabled,
            "oidc_provider": settings.oidc_provider_name if settings.oidc_enabled else None,
            "wahoo_configured": settings.wahoo_enabled,
            "version": APP_VERSION,
        },
    }


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(user_id: uuid.UUID, db: DbDep, admin: AdminDep) -> None:
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="You cannot delete your own account")
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    await db.delete(user)
    await db.commit()
