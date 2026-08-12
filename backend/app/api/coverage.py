"""How much of the roads and paths near a rider they have actually ridden,
measured from their own map-matched activities - two denominators, the
signed cycle network and every bikeable road, sharing one bbox-resolution
dance between them.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from app.api.deps import DbDep, UserDep
from app.schemas import (
    CoverageBackfillStatus,
    CoverageResponse,
    HighwayCoverage,
    NetworkCoverage,
    RoadCoverageResponse,
)
from app.services.coverage import (
    BBox,
    cycle_network_coverage,
    default_bbox,
    index_meta,
    road_coverage,
)
from app.services.way_matching import MatchJob
from app.services.way_matching import queue as match_queue

router = APIRouter(prefix="/api/coverage", tags=["coverage"])


def _explicit_bbox(
    min_lat: float | None,
    min_lon: float | None,
    max_lat: float | None,
    max_lon: float | None,
) -> BBox | None:
    """The bbox a client asked for, or None meaning "derive the default".

    All four are required if any is given - a half-specified bbox is a
    client bug worth a 422, not a query silently run against the whole
    planet - so this is the one place both /cycle-network and /roads check
    that, rather than each repeating it.
    """
    given = (min_lat, min_lon, max_lat, max_lon)
    if any(v is not None for v in given) and any(v is None for v in given):
        raise HTTPException(
            status_code=422,
            detail="Provide all of min_lat, min_lon, max_lat, max_lon, or none of them.",
        )
    if min_lat is None:
        return None
    # The all-or-nothing check above means that by this point every bound
    # was given - the asserts are for mypy, which cannot infer that from a
    # check on a different variable three lines up.
    assert min_lon is not None and max_lat is not None and max_lon is not None
    return (min_lon, min_lat, max_lon, max_lat)


async def _resolve_bbox(db: DbDep, user: UserDep, explicit: BBox | None) -> BBox | None:
    """The explicit bbox if one was given, otherwise one centred on the
    rider's own activities (services/coverage.py:default_bbox) - what the
    /activities card uses, since it has no map to draw one from. None means
    there is nothing to centre a default on."""
    return explicit if explicit is not None else await default_bbox(db, user.id)


@router.get("/cycle-network")
async def cycle_network(
    db: DbDep,
    user: UserDep,
    min_lat: Annotated[float | None, Query(ge=-90, le=90)] = None,
    min_lon: Annotated[float | None, Query(ge=-180, le=180)] = None,
    max_lat: Annotated[float | None, Query(ge=-90, le=90)] = None,
    max_lon: Annotated[float | None, Query(ge=-180, le=180)] = None,
) -> CoverageResponse:
    """Ridden vs total metres of the signed network, by tier, within a bbox.

    The bbox is optional: omit all four and this centres one on wherever the
    rider's own activities are, which is what the /activities card uses
    since it has no map to draw one from. Given at all, all four are
    required - see _explicit_bbox.
    """
    explicit = _explicit_bbox(min_lat, min_lon, max_lat, max_lon)

    meta = await index_meta(db)
    if meta is None:
        return CoverageResponse(
            available=False,
            reason="The place index has not been built yet - an admin needs to run the indexer.",
        )
    if meta.cycle_way_member_count is None:
        return CoverageResponse(
            available=False,
            reason=(
                "The place index predates cycle-network coverage and needs a re-index "
                "(see docs/troubleshooting.md)."
            ),
        )

    bbox = await _resolve_bbox(db, user, explicit)
    if bbox is None:
        return CoverageResponse(available=False, reason="Import a ride to see coverage near you.")

    rows = await cycle_network_coverage(db, user.id, bbox)
    return CoverageResponse(
        available=True,
        networks=[
            NetworkCoverage(network=network, ridden_m=ridden_m, total_m=total_m)
            for network, total_m, ridden_m in rows
        ],
    )


@router.get("/roads")
async def roads(
    db: DbDep,
    user: UserDep,
    min_lat: Annotated[float | None, Query(ge=-90, le=90)] = None,
    min_lon: Annotated[float | None, Query(ge=-180, le=180)] = None,
    max_lat: Annotated[float | None, Query(ge=-90, le=90)] = None,
    max_lon: Annotated[float | None, Query(ge=-180, le=180)] = None,
) -> RoadCoverageResponse:
    """Ridden vs total metres of every bikeable road, by highway class,
    within a bbox - the wider denominator alongside /cycle-network. Bbox
    handling is identical; see _explicit_bbox.
    """
    explicit = _explicit_bbox(min_lat, min_lon, max_lat, max_lon)

    meta = await index_meta(db)
    if meta is None:
        return RoadCoverageResponse(
            available=False,
            reason="The place index has not been built yet - an admin needs to run the indexer.",
        )
    if meta.osm_way_count is None:
        return RoadCoverageResponse(
            available=False,
            reason=(
                "The place index predates all-roads coverage and needs a re-index "
                "(see docs/troubleshooting.md)."
            ),
        )
    if meta.osm_ways_source_files is not None and meta.osm_ways_source_files != meta.source_files:
        # A rebuild with INDEX_ROADS off leaves osm_ways untouched but still
        # overwrites source_files with whatever extract that run used
        # (indexer/db.py:publish) - so a real mismatch here means "osm_ways
        # was last built from a different extract than the rest of the
        # index", not a transient glitch. Reporting coverage against it would
        # silently mix two different countries' worth of roads and rides;
        # see migration 0016.
        #
        # NULL is treated as "unknown, not mismatched" rather than a second
        # reason to refuse: an install upgrading from before this column
        # existed has real rows in osm_ways and a real osm_way_count, but
        # nothing yet recorded about which extract built them, until its own
        # next roads-on rebuild fills the column in for the first time.
        # Blocking that transitional state would regress every existing
        # install's coverage the moment this migration lands, to guard
        # against a mismatch nothing has actually observed yet.
        return RoadCoverageResponse(
            available=False,
            reason=(
                "The road index was last built from a different extract than the rest "
                "of the place index and needs a re-index with roads enabled "
                "(see docs/troubleshooting.md)."
            ),
        )

    bbox = await _resolve_bbox(db, user, explicit)
    if bbox is None:
        return RoadCoverageResponse(
            available=False, reason="Import a ride to see coverage near you."
        )

    rows = await road_coverage(db, user.id, bbox)
    return RoadCoverageResponse(
        available=True,
        highways=[
            HighwayCoverage(highway=highway, ridden_m=ridden_m, total_m=total_m)
            for highway, total_m, ridden_m in rows
        ],
    )


@router.post("/backfill", status_code=202)
async def backfill(user: UserDep) -> CoverageBackfillStatus:
    """Match every activity that has never been attempted - imported before
    coverage existed, or added since but not yet reached by the queue.

    202, queued rather than run inline: a rider with years of rides could be
    hundreds of Valhalla round trips, the same reasoning as the archive
    import endpoint. See services/way_matching.py.
    """
    return _job(match_queue.submit(user.id))


@router.get("/backfill/{job_id}")
async def backfill_status(job_id: uuid.UUID, user: UserDep) -> CoverageBackfillStatus:
    job = match_queue.get(job_id, user.id)
    if job is None:
        raise HTTPException(status_code=404, detail="Backfill job not found")
    return _job(job)


def _job(job: MatchJob) -> CoverageBackfillStatus:
    return CoverageBackfillStatus(
        id=job.id,
        status=job.status,
        total=job.total,
        matched=job.matched,
        unmatched=job.unmatched,
        error=job.error,
    )
