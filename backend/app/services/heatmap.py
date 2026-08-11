"""The personal heatmap: one rider's own activity traces as vector tiles.

A line overlay, not grid aggregation - a decision already taken (see
docs/architecture.md). Each activity is drawn as its own low-opacity line
in the frontend, so roads ridden more than once darken where traces
overlap.

Same tile-generation shape as services/places.py's cycle-network overlay:
`ST_AsMVT` + `ST_AsMVTGeom` + a zoom-derived `ST_Simplify` tolerance, with
`ST_Transform` applied to the (constant) tile envelope rather than to every
row, so `ix_activities_geom` still serves the bounding-box filter.
Deliberately not a second style - see that module for why each piece is
shaped the way it is.

Unlike the cycle network, no `ST_LineMerge` is needed here: a `CycleWay`
row is a route relation assembled from however many disconnected member
ways the indexer collected (195 on average), which is what defeated
simplification until they were merged. An `Activity` row is already one
continuous GPS trace recorded as a single LINESTRING, so there is nothing
to merge.
"""

import hashlib
import uuid

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Activity

# The same bounds as the cycle network. 16 is where tiles stop being
# generated, not where the layer stops being visible: MapLibre overzooms a
# vector source past its maxzoom, so retracing your own commute at 18 still
# draws, it just reuses the zoom-16 tile.
MIN_HEATMAP_ZOOM = 4
MAX_HEATMAP_ZOOM = 16

# Same asyncpg lesson as services/places.py: :z needs an explicit CAST or
# `2 ^ :z` types the whole statement as double precision and ST_TileEnvelope
# rejects it ("function st_tileenvelope(double precision, unknown, unknown)
# does not exist").
_Z = "CAST(:z AS integer)"
_ENVELOPE = f"ST_TileEnvelope({_Z}, CAST(:x AS integer), CAST(:y AS integer))"

# One tile unit in metres at this zoom (40,075,016.7 m across the world,
# 4,096 units per tile) - simplifying below what the tile can express costs
# nothing visible.
_TOLERANCE = "40075016.7 / (2 ^ CAST(:z AS double precision) * 4096)"

# ST_Transform is applied to the tile envelope, not to every row: the
# envelope is a constant, so ix_activities_geom still serves the
# bounding-box filter. Transforming each row instead would disable it.
#
# The `user_id = :user_id` filter is the one line in this file that
# matters most. Getting it wrong is invisible in single-user testing and
# would silently draw every rider's traces on every rider's map - see
# tests/test_heatmap.py, which proves it bites.
_TILE_SQL = text(f"""
    SELECT ST_AsMVT(tile, 'activities', 4096, 'geom') AS mvt
    FROM (
        SELECT id,
               ST_AsMVTGeom(
                   ST_Simplify(ST_Transform(geom, 3857), {_TOLERANCE}),
                   {_ENVELOPE}, 4096, 64, true
               ) AS geom
        FROM activities
        WHERE user_id = :user_id
          AND geom && ST_Transform({_ENVELOPE}, 4326)
    ) AS tile
    WHERE tile.geom IS NOT NULL
""")


async def heatmap_tile(db: AsyncSession, user_id: uuid.UUID, z: int, x: int, y: int) -> bytes:
    """One vector tile of one rider's own activity traces.

    An empty tile is a normal answer - most tiles are nowhere near anywhere
    this rider has been - and MapLibre handles a zero-length body fine, so
    this never 404s on its own account.
    """
    mvt = await db.scalar(_TILE_SQL, {"user_id": user_id, "z": z, "x": x, "y": y})
    return bytes(mvt) if mvt else b""


async def heatmap_etag(db: AsyncSession, user_id: uuid.UUID, z: int, x: int, y: int) -> str:
    """A cheap fingerprint of one rider's activities, for HTTP caching.

    Cycle-network tiles are install-wide and only change when the indexer
    reruns, so they carry a long max-age. Heatmap tiles are personal and
    change the moment a ride is imported or deleted, so they cannot - the
    tile is revalidated on every request instead, and this is what makes
    that cheap: a row count plus the newest `created_at` move on every
    insert and delete, so one indexed aggregate stands in for hashing the
    tile itself.

    The tile coordinates are part of the fingerprint. HTTP caches key on
    the URL, so in practice one ETag per rider would already be safe - but
    an entity tag that does not vary with the entity is a trap laid for
    whichever future caller revalidates by tag rather than by URL, and
    including z/x/y costs nothing.
    """
    count, latest = (
        await db.execute(
            select(func.count(Activity.id), func.max(Activity.created_at)).where(
                Activity.user_id == user_id
            )
        )
    ).one()
    stamp = latest.isoformat() if latest else "none"
    fingerprint = f"{user_id}:{count}:{stamp}:{z}/{x}/{y}"
    return f'"{hashlib.sha256(fingerprint.encode()).hexdigest()[:16]}"'
