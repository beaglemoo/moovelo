import uuid
from datetime import datetime
from typing import Any

from geoalchemy2 import Geography, Geometry
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)

# The dialect ARRAY, not sqlalchemy.ARRAY: only this one implements the
# containment operator that tag filtering needs.
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
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
    # planned = built from waypoints in the app; imported = read from a file.
    # An imported route's waypoints are only its endpoints, so re-routing it
    # would discard the track that was imported.
    source: Mapped[str] = mapped_column(String(16), default="planned", server_default="planned")
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
    # Free-form organisation. Tags rather than folders: a route can be both
    # "gravel" and "with the kids".
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    notes: Mapped[str | None] = mapped_column(Text, default=None)
    is_favourite: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (Index("ix_routes_tags", "tags", postgresql_using="gin"),)


# The four tables below are written only by the indexer sidecar, which is a
# separate process on its own compose profile. The app reads them and never
# writes them, so they carry no timestamps and no ownership.


class Place(Base):
    """A named settlement or feature: what place search resolves against."""

    __tablename__ = "places"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    osm_type: Mapped[str] = mapped_column(String(1))
    osm_id: Mapped[int] = mapped_column(BigInteger)
    name: Mapped[str] = mapped_column(Text)
    # Accent-folded and casefolded by the indexer. The trigram index lives
    # here rather than on name, because unaccent() is STABLE and so cannot
    # be indexed directly.
    name_norm: Mapped[str] = mapped_column(Text)
    place_type: Mapped[str] = mapped_column(Text)
    county: Mapped[str | None] = mapped_column(Text, default=None)
    population: Mapped[int | None] = mapped_column(Integer, default=None)
    # Ranking weight from place_type and population, computed at index time.
    importance: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    geog: Mapped[Any] = mapped_column(
        Geography(geometry_type="POINT", srid=4326, spatial_index=False)
    )

    __table_args__ = (
        Index("ix_places_osm", "osm_type", "osm_id", unique=True),
        Index(
            "ix_places_name_trgm",
            "name_norm",
            postgresql_using="gin",
            postgresql_ops={"name_norm": "gin_trgm_ops"},
        ),
        Index("ix_places_geog", "geog", postgresql_using="gist"),
    )


class Poi(Base):
    """Somewhere useful on a ride: water, coffee, a bike shop, a viewpoint."""

    __tablename__ = "pois"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    osm_type: Mapped[str] = mapped_column(String(1))
    osm_id: Mapped[int] = mapped_column(BigInteger)
    # Nullable because the unnamed ones - a drinking fountain, a repair
    # stand - are exactly the POIs worth finding.
    name: Mapped[str | None] = mapped_column(Text, default=None)
    name_norm: Mapped[str | None] = mapped_column(Text, default=None)
    category: Mapped[str] = mapped_column(Text)
    osm_tags: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    geog: Mapped[Any] = mapped_column(
        Geography(geometry_type="POINT", srid=4326, spatial_index=False)
    )

    __table_args__ = (
        Index("ix_pois_osm", "osm_type", "osm_id", unique=True),
        Index("ix_pois_geog", "geog", postgresql_using="gist"),
        Index("ix_pois_category", "category"),
        # Partial, so the unnamed majority costs nothing to index.
        Index(
            "ix_pois_name_trgm",
            "name_norm",
            postgresql_using="gin",
            postgresql_ops={"name_norm": "gin_trgm_ops"},
            postgresql_where=text("name_norm IS NOT NULL"),
        ),
    )


class CycleWay(Base):
    """One signed cycle route: an OSM route relation, not a single way.

    Keyed on the relation so the overlay can label a line "NCN 6" rather
    than drawing anonymous segments.
    """

    __tablename__ = "cycle_ways"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    ref: Mapped[str | None] = mapped_column(Text, default=None)
    name: Mapped[str | None] = mapped_column(Text, default=None)
    network: Mapped[str] = mapped_column(Text)
    operator: Mapped[str | None] = mapped_column(Text, default=None)
    geom: Mapped[Any] = mapped_column(
        Geometry(geometry_type="MULTILINESTRING", srid=4326, spatial_index=False)
    )

    __table_args__ = (
        Index("ix_cycle_ways_geom", "geom", postgresql_using="gist"),
        Index("ix_cycle_ways_network", "network"),
    )


class SearchIndexMeta(Base):
    """When the index was last built, and from what.

    A single row, enforced by the type: the app asks "has the index been
    built" and there is only ever one answer.
    """

    __tablename__ = "search_index_meta"

    id: Mapped[bool] = mapped_column(Boolean, primary_key=True, default=True, server_default="true")
    built_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_files: Mapped[list[str]] = mapped_column(ARRAY(Text))
    place_count: Mapped[int] = mapped_column(Integer)
    poi_count: Mapped[int] = mapped_column(Integer)
    cycle_way_count: Mapped[int] = mapped_column(Integer)

    __table_args__ = (CheckConstraint("id", name="ck_search_index_meta_singleton"),)
