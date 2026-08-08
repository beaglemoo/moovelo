"""Turn an uploaded file into a route snapshot.

Parsing, map matching and elevation are separate services; this is the glue
that runs them in order and decides what to do when matching fails.
"""

from fastapi import HTTPException

from app.schemas import ElevationPoint, Preset, RouteLeg, RouteResponse, Waypoint
from app.services.climbs import detect_climbs
from app.services.geo import Point, cumulative_distances, resample_by_distance
from app.services.importer import ImportedTrack, elevation_profile, parse_route_file
from app.services.polyline import decode_polyline6, encode_polyline6
from app.services.valhalla import TRACE_SPACING_M, ValhallaClient, ascent_descent

# Used only to give an unmatched track a plausible duration, since FIT course
# timestamps are derived from it. Matched routes get Valhalla's own estimate.
FALLBACK_SPEED_KMH = 18.0


class ImportedRoute:
    """A parsed file, routed if possible, ready to persist."""

    def __init__(self, track: ImportedTrack, snapshot: RouteResponse, matched: bool) -> None:
        self.track = track
        self.snapshot = snapshot
        self.matched = matched

    @property
    def waypoints(self) -> list[Waypoint]:
        """Only the endpoints. An imported route's line is the track itself,
        not something reconstructible from waypoints - which is exactly why
        editing one in the planner has to be guarded."""
        first, last = self.track.points[0], self.track.points[-1]
        return [
            Waypoint(lat=first.lat, lon=first.lon),
            Waypoint(lat=last.lat, lon=last.lon),
        ]

    @property
    def cue_count(self) -> int:
        return sum(len(leg.maneuvers) for leg in self.snapshot.legs)


async def match_or_keep(
    shape: list[Point], preset: Preset, valhalla: ValhallaClient
) -> tuple[RouteResponse, bool]:
    """Map match a track, falling back to keeping it unmatched.

    Shared by import and reverse so both treat an unmatchable track the same
    way. A track may leave the map extract or follow paths the routing graph
    does not have: the ride still exists, so keep the line and lose only the
    turn cues.

    A routing engine that is unreachable or still building tiles is a
    different thing entirely - that is retryable, and silently storing a
    cueless route would make the loss permanent - so 5xx propagates.
    """
    try:
        return await valhalla.trace_route(shape, preset), True
    except HTTPException as exc:
        if exc.status_code >= 500:
            raise
        return _unmatched_snapshot(shape), False


async def import_route(
    filename: str, data: bytes, preset: Preset, valhalla: ValhallaClient
) -> ImportedRoute:
    """Parse an uploaded file and route it. Raises RouteImportError if the
    file itself is unusable; a file that parses but cannot be matched is kept
    as an unmatched track rather than rejected."""
    track = parse_route_file(filename, data)
    snapshot, matched = await match_or_keep(track.shape, preset, valhalla)

    if not snapshot.elevation:
        profile = elevation_profile(track, snapshot.distance_m)
        if profile:
            ascent, descent = ascent_descent(profile)
            snapshot = snapshot.model_copy(
                update={
                    "elevation": profile,
                    "ascent_m": ascent,
                    "descent_m": descent,
                    "climbs": detect_climbs(profile),
                }
            )

    # Surface is decorative: an unmatched track fails edge_walk by design and
    # must not block the import, so this is fetched after everything that
    # can raise has already succeeded. Legs, not one concatenated shape -
    # see ValhallaClient.trace_attributes.
    legs = [decode_polyline6(leg.geometry) for leg in snapshot.legs]
    surface = await valhalla.trace_attributes(legs, preset)
    if surface is not None:
        snapshot = snapshot.model_copy(update={"surface": surface})

    return ImportedRoute(track, snapshot, matched)


def _unmatched_snapshot(track_shape: list[Point]) -> RouteResponse:
    shape = resample_by_distance(track_shape, TRACE_SPACING_M)
    distance_m = cumulative_distances(shape)[-1]
    profile: list[ElevationPoint] = []
    return RouteResponse(
        legs=[RouteLeg(geometry=encode_polyline6(shape), maneuvers=[])],
        distance_m=distance_m,
        duration_s=distance_m / (FALLBACK_SPEED_KMH * 1000 / 3600),
        ascent_m=0.0,
        descent_m=0.0,
        elevation=profile,
    )
