"""How much of the signed cycle network a rider has actually ridden,
measured from their own map-matched activities.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from app.api.deps import DbDep, UserDep
from app.schemas import CoverageBackfillStatus, CoverageResponse, NetworkCoverage
from app.services.coverage import cycle_network_coverage, default_bbox, index_meta
from app.services.way_matching import MatchJob
from app.services.way_matching import queue as match_queue

router = APIRouter(prefix="/api/coverage", tags=["coverage"])


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
    rider's own activities are (services/coverage.py:default_bbox), which is
    what the /activities card uses since it has no map to draw one from.
    Given at all, all four are required - a half-specified bbox is a client
    bug worth a 422, not a query silently run against the whole planet.
    """
    given = (min_lat, min_lon, max_lat, max_lon)
    if any(v is not None for v in given) and any(v is None for v in given):
        raise HTTPException(
            status_code=422,
            detail="Provide all of min_lat, min_lon, max_lat, max_lon, or none of them.",
        )

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

    # The all-or-nothing check above means that by this point either every
    # bound was given, or none was - so testing just one is enough to tell
    # "an explicit bbox" from "derive the default" apart. The asserts are for
    # mypy: it cannot infer that fact from a check three lines up on a
    # different variable.
    bbox: tuple[float, float, float, float] | None
    if min_lat is not None:
        assert min_lon is not None and max_lat is not None and max_lon is not None
        bbox = (min_lon, min_lat, max_lon, max_lat)
    else:
        bbox = await default_bbox(db, user.id)
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
