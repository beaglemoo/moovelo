"""Personal climb detection on activities, plus a backfill.

Adds Activity.climbs, mirroring Route.climbs exactly - same JSONB shape
(services/climbs.py's Climb, dumped by detect_climbs), same NOT NULL
DEFAULT '[]'::jsonb (migration 0009) so an existing row reads back as an
empty list rather than null. A newly imported ride gets this computed once
at import time (services/activities.py:activity_from_track, shared by both
the single-file and the Strava-archive import paths) - this migration only
has to backfill rides that were already stored before the column existed.

Prod and staging carry zero activities today (2026-08-14), so the backfill
below is a no-op there - but anyone who imported a Strava archive before
this PR shipped would otherwise be left with a permanently empty climb log
for every ride they already have, since nothing else ever re-derives a
stored column. The loop is defensive per row: `elevation` that is missing,
not a list, or otherwise unparseable as ElevationPoint gets `climbs = []`
rather than failing the whole migration - the same "skip, don't crash"
posture services/route_match.py takes for a trace too large to buffer.

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-14

"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.schemas import ElevationPoint
from app.services.climbs import detect_climbs

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "activities",
        sa.Column(
            "climbs",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    _backfill()


def downgrade() -> None:
    op.drop_column("activities", "climbs")


def _backfill() -> None:
    """One UPDATE per existing row rather than a single bulk statement:
    detect_climbs is nontrivial numerical logic (resampling, smoothing,
    scoring) that already exists once in services/climbs.py, and
    re-deriving even a rough equivalent of it in SQL would be a second,
    divergence-prone copy of the same algorithm for what is, on every
    installation that matters (prod and staging both have zero activities
    at the time this migration was written), an empty table anyway."""
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, elevation FROM activities")).fetchall()
    for row in rows:
        climbs = _detect_climbs_defensively(row.elevation)
        bind.execute(
            sa.text("UPDATE activities SET climbs = CAST(:climbs AS jsonb) WHERE id = :id"),
            {"climbs": json.dumps(climbs), "id": row.id},
        )


def _detect_climbs_defensively(raw: object) -> list[dict[str, object]]:
    """`elevation` as read straight back out of Postgres - a JSON string or
    an already-decoded list/dict, depending on the driver's own codec -
    turned into detect_climbs' own Climb dicts, or `[]` for anything that
    does not parse as a clean list of `{dist_m, elev_m}` points. A
    malformed or missing profile must not fail the migration; it gets an
    empty climb log instead, same as a ride genuinely recorded dead flat.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return []
    if not isinstance(raw, list):
        return []
    try:
        points = [ElevationPoint(dist_m=item["dist_m"], elev_m=item["elev_m"]) for item in raw]
    except (TypeError, KeyError, ValueError):
        return []
    try:
        climbs = detect_climbs(points)
    except Exception:  # noqa: BLE001 - a migration must never fail on one bad row
        return []
    return [climb.model_dump() for climb in climbs]
