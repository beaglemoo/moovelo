import uuid
from datetime import datetime
from typing import Any

from geoalchemy2 import Geometry
from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    is_admin: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Session(Base):
    __tablename__ = "sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WahooAccount(Base):
    __tablename__ = "wahoo_accounts"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    access_token: Mapped[str] = mapped_column(String(500))
    refresh_token: Mapped[str] = mapped_column(String(500))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    athlete: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Route(Base):
    __tablename__ = "routes"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    preset: Mapped[str] = mapped_column(String(20))
    waypoints: Mapped[list[Any]] = mapped_column(JSONB)
    # Full routing snapshot (RouteResponse shape) so saved routes and their
    # exports stay stable across OSM data refreshes.
    legs: Mapped[list[Any]] = mapped_column(JSONB)
    elevation: Mapped[list[Any]] = mapped_column(JSONB)
    distance_m: Mapped[float]
    duration_s: Mapped[float]
    ascent_m: Mapped[float]
    descent_m: Mapped[float]
    geom: Mapped[Any] = mapped_column(Geometry(geometry_type="LINESTRING", srid=4326))
    # Wahoo sync state: none | queued | pushing | synced | error
    wahoo_status: Mapped[str] = mapped_column(String(10), default="none", server_default="none")
    wahoo_error: Mapped[str | None] = mapped_column(String(500), default=None)
    wahoo_route_id: Mapped[str | None] = mapped_column(String(64), default=None)
    wahoo_pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # Public read-only share link token; null = not shared.
    share_token: Mapped[str | None] = mapped_column(String(64), unique=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
