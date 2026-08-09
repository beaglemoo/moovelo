"""Loop generator: "N km loop from here", built from primitives Valhalla
already has rather than a round-trip API it doesn't.

The approach: for each of a handful of compass bearings, put a via point on
a circle around the origin and binary-search its radius until the resulting
out-and-back route (origin -> via -> origin) lands near the target
distance, then score every bearing's best attempt and keep the top few that
are meaningfully different from each other.
"""

import asyncio
import math

from fastapi import HTTPException

from app.schemas import (
    BicycleCostingOptions,
    LoopCandidate,
    Preset,
    RouteRequest,
    RouteResponse,
    Waypoint,
)
from app.services.geo import destination_point, haversine
from app.services.polyline import decode_polyline6
from app.services.valhalla import ValhallaClient

LOOP_BEARINGS = 8  # compass spread; the dedup pass collapses lookalikes
# An out-and-back through one via point measures roughly 2.5x the via's
# straight-line radius on real roads (outbound + a mostly-different return,
# each a wiggly ~1.25x of the crow-flies distance). The first guess assumed
# a full circle (2*pi*r) instead, which put the radius that actually hits
# larger targets entirely outside the search window - every 60 km+ candidate
# saturated at the upper bound and undershot by 15-23%. Measured live, not
# derived: see review pass 2.
OUT_AND_BACK_RATIO = 2.5
LOOP_MAX_ITERS = 6  # halvings of a 10x radius window: ~3% resolution
LOOP_TOLERANCE = 0.05  # stop a bearing early within 5% of target
LOOP_CANDIDATES = 3
LOOP_DEDUP_KM = 3.0  # min via-point separation between returned loops
ASCENT_WEIGHT = 0.15  # mild per-km climbing penalty, not a cutoff
SURFACE_WEIGHT = 0.5  # unpaved-fraction penalty (road/quiet) or bonus (gravel)

# Mirrors SurfaceBar.svelte's PAVED group: what a rider actually decides on
# is "can a road bike cope with this", not Valhalla's full surface enum.
PAVED_SURFACES = ("paved_smooth", "paved", "paved_rough")


class _Attempt:
    """One bearing's best via point and the route it produced."""

    def __init__(self, via: Waypoint, snapshot: RouteResponse) -> None:
        self.via = via
        self.snapshot = snapshot


async def generate_loop_candidates(
    origin: Waypoint,
    target_km: float,
    preset: Preset,
    costing_options: BicycleCostingOptions | None,
    valhalla: ValhallaClient,
) -> list[LoopCandidate]:
    target_m = target_km * 1000.0
    bearings = [i * (360.0 / LOOP_BEARINGS) for i in range(LOOP_BEARINGS)]
    # Independent per bearing, so they run together rather than one after
    # another - only the binary search *within* a bearing is sequential.
    results = await asyncio.gather(
        *(
            _search_bearing(origin, b, target_m, preset, costing_options, valhalla)
            for b in bearings
        ),
        return_exceptions=True,
    )
    survivors: list[_Attempt] = []
    for result in results:
        if isinstance(result, HTTPException):
            # No road that way at any radius tried - a bearing pointing out
            # to sea, say. Normal, not an error.
            continue
        if isinstance(result, BaseException):
            raise result
        survivors.append(result)

    if not survivors:
        raise HTTPException(
            status_code=422,
            detail=(
                "No rideable loop found from here - the surrounding area may be outside the "
                "map extract."
            ),
        )

    scored = await asyncio.gather(
        *(
            _attach_surface_and_score(attempt, target_km, preset, costing_options, valhalla)
            for attempt in survivors
        )
    )
    candidates = [
        LoopCandidate(
            waypoints=[origin, attempt.via, origin],
            snapshot=snapshot,
            distance_error_km=abs(snapshot.distance_m - target_m) / 1000.0,
            score=score,
        )
        for attempt, (snapshot, score) in zip(survivors, scored, strict=True)
    ]
    candidates.sort(key=lambda c: c.score)
    return _dedup_by_via(candidates, LOOP_CANDIDATES, LOOP_DEDUP_KM * 1000.0)


async def _search_bearing(
    origin: Waypoint,
    bearing_deg: float,
    target_m: float,
    preset: Preset,
    costing_options: BicycleCostingOptions | None,
    valhalla: ValhallaClient,
) -> _Attempt:
    """Binary search a via point's radius along `bearing_deg` until the
    out-and-back route through it lands near `target_m`, keeping the best
    (closest-distance) attempt seen rather than only the last one tried.

    Raises the last HTTPException when every radius tried was unroutable -
    the caller treats a whole bearing failing as "nothing this way", not a
    hard error.
    """
    r0 = target_m / OUT_AND_BACK_RATIO
    # Covers road networks from ratio ~1.25 (grid-perfect loops) to ~6
    # (dense trail webs) around the 2.5 seed.
    lo, hi = 0.4 * r0, 2.0 * r0
    r = r0
    best: _Attempt | None = None
    best_error = math.inf
    last_error: HTTPException | None = None
    for _ in range(LOOP_MAX_ITERS):
        via_lat, via_lon = destination_point((origin.lat, origin.lon), bearing_deg, r)
        via = Waypoint(lat=via_lat, lon=via_lon)
        try:
            snapshot = await valhalla.route(
                RouteRequest(
                    waypoints=[origin, via, origin],
                    preset=preset,
                    costing_options=costing_options,
                )
            )
        except HTTPException as exc:
            last_error = exc
            # No route out that far (or too close to find one at all) - the
            # smaller radius is the more likely direction to still work.
            hi = r
            r = (lo + r) / 2
            continue
        error = abs(snapshot.distance_m - target_m) / target_m
        if error < best_error:
            best_error = error
            best = _Attempt(via=via, snapshot=snapshot)
        if error <= LOOP_TOLERANCE:
            break
        if snapshot.distance_m < target_m:
            lo = r
            r = (r + hi) / 2
        else:
            hi = r
            r = (lo + r) / 2
    if best is None:
        assert last_error is not None  # LOOP_MAX_ITERS >= 1, so one of the two is always set
        raise last_error
    return best


async def _attach_surface_and_score(
    attempt: _Attempt,
    target_km: float,
    preset: Preset,
    costing_options: BicycleCostingOptions | None,
    valhalla: ValhallaClient,
) -> tuple[RouteResponse, float]:
    """The surviving snapshot with its surface breakdown attached (when
    trace_attributes succeeds - see valhalla.py, it degrades to None on any
    failure and that is not penalised here either), and its score."""
    snapshot = attempt.snapshot
    actual_km = snapshot.distance_m / 1000.0
    legs = [decode_polyline6(leg.geometry) for leg in snapshot.legs]
    surface = await valhalla.trace_attributes(legs)
    unpaved_fraction = 0.0
    if surface is not None and surface.total_m > 0:
        paved_m = sum(surface.surface_m.get(key, 0.0) for key in PAVED_SURFACES)
        unpaved_fraction = max(0.0, (surface.total_m - paved_m) / surface.total_m)
        snapshot = snapshot.model_copy(update={"surface": surface})
    score = score_loop(
        actual_km, target_km, snapshot.ascent_m, unpaved_fraction, preset, costing_options
    )
    return snapshot, score


def score_loop(
    actual_km: float,
    target_km: float,
    ascent_m: float,
    unpaved_fraction: float,
    preset: Preset,
    costing_options: BicycleCostingOptions | None,
) -> float:
    """Lower is better. Distance error dominates; ascent and surface are
    tie-breakers rather than cutoffs - even a mountainous 30 m/km ride adds
    only ~0.15 (ASCENT_WEIGHT * 30 / 30), never enough to outweigh a 20%
    distance miss (0.20) on its own."""
    distance_term = abs(actual_km - target_km) / target_km
    ascent_term = ASCENT_WEIGHT * (ascent_m / max(actual_km, 1.0)) / 30.0
    # Custom costing scores like "road" (avoid unpaved) unless it has
    # actually been tuned to want rough surfaces - avoid_bad_surfaces <= 0.2
    # is the gravel preset's own value.
    wants_unpaved = preset == "gravel" or (
        costing_options is not None and costing_options.avoid_bad_surfaces <= 0.2
    )
    signed_unpaved_term = -unpaved_fraction if wants_unpaved else unpaved_fraction
    return distance_term + ascent_term + SURFACE_WEIGHT * signed_unpaved_term


def _dedup_by_via(
    candidates: list[LoopCandidate], limit: int, min_sep_m: float
) -> list[LoopCandidate]:
    """Greedily keep the best-scoring candidates (input already sorted by
    score) whose via points are more than `min_sep_m` apart, so adjacent
    bearings converging on essentially the same loop don't crowd out a
    genuinely different one."""
    kept: list[LoopCandidate] = []
    for candidate in candidates:
        via = candidate.waypoints[1]
        if all(
            haversine((via.lat, via.lon), (k.waypoints[1].lat, k.waypoints[1].lon)) > min_sep_m
            for k in kept
        ):
            kept.append(candidate)
        if len(kept) >= limit:
            break
    return kept
