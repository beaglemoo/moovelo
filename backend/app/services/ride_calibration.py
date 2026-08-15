"""Fit the rider's own flat_speed_kmh from their matched rides.

Suggest, never apply: `compute_ride_time` (services/ride_time.py) stays the
single source of ride-time predictions, and this module has no write path
of its own - the caller applies a suggestion, if it wants to, through the
existing `PATCH /api/settings`.

For every ride the caller can use - `route_id` set and `moving_time_s`
present - this solves for the `flat_speed_kmh` that would have made
`compute_ride_time` over *the matched route's* elevation and surface
predict that ride's actual moving time, holding the rider's other settings
(weight, FTP) fixed. `compute_ride_time`'s total is not simply proportional
to `1/flat_speed_kmh` - `MIN_SPEED_KMH` floors segment speed, and
`effective_flat_speed` rescales by FTP - so the fit is done numerically by
bisection rather than by an algebraic inverse. Bisection is valid because
the predicted total is monotonically decreasing in `flat_speed_kmh` over
the whole 5-60 range (test_ride_calibration.py asserts this directly rather
than assuming it): raising the flat-road speed can only shorten segments,
never lengthen one, since every other factor (gradient, surface, the
MIN_SPEED_KMH floor) is unchanged.

Per-ride results are combined with the median, not the mean, so one
outlier - a long cafe stop folded into `moving_time_s`, a GPS dropout, a
stopped-clock quirk in the source file - cannot drag the suggestion by
itself. A floor of MIN_USABLE_RIDES is required before anything is
offered: a suggestion from one or two rides is closer to noise than to a
calibration.
"""

import statistics
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Activity, Route
from app.schemas import ElevationPoint, SurfaceBreakdown, UserSettingsResponse
from app.services.ride_time import compute_ride_time

# Mirrors UserSettingsPatch.flat_speed_kmh's own bounds (app/schemas.py) -
# a suggestion outside what PATCH /api/settings would accept is useless.
MIN_FLAT_SPEED_KMH = 5.0
MAX_FLAT_SPEED_KMH = 60.0

# Bisection stops once the predicted total is within a second of the
# ride's actual moving time - far tighter than the model claims to be
# accurate to, but cheap: compute_ride_time over a route's elevation series
# is a few hundred additions, and this runs it at most a few dozen times
# per ride.
BISECTION_TOLERANCE_S = 1.0
BISECTION_MAX_ITER = 60

# Below this many usable rides, a per-ride fit is closer to noise than to a
# calibration - see the module docstring.
MIN_USABLE_RIDES = 5


@dataclass
class CalibrationSuggestion:
    suggested_kmh: float
    sample_size: int
    current_kmh: float


def _predicted_total_s(
    elevation: list[ElevationPoint],
    surface: SurfaceBreakdown | None,
    settings: UserSettingsResponse,
    flat_speed_kmh: float,
) -> float:
    trial = settings.model_copy(update={"flat_speed_kmh": flat_speed_kmh})
    points = compute_ride_time(elevation, surface, trial)
    return points[-1].time_s if points else 0.0


def solve_flat_speed_kmh(
    elevation: list[ElevationPoint],
    surface: SurfaceBreakdown | None,
    settings: UserSettingsResponse,
    actual_time_s: float,
) -> float | None:
    """The flat_speed_kmh (5-60) whose predicted total matches actual_time_s,
    found by bisection rather than inverted algebraically - see the module
    docstring for why. None when there is no gradient to walk (fewer than
    two elevation points, compute_ride_time's own [] case) or the ride
    carries no usable moving time.

    A ride slower or faster than the model can express at either end of the
    range is clamped to that end rather than excluded: the predicted total
    is monotonic, so MIN/MAX_FLAT_SPEED_KMH are themselves the closest
    representable answer, not a failure to solve.
    """
    if len(elevation) < 2 or actual_time_s <= 0:
        return None

    time_at_min = _predicted_total_s(elevation, surface, settings, MIN_FLAT_SPEED_KMH)
    time_at_max = _predicted_total_s(elevation, surface, settings, MAX_FLAT_SPEED_KMH)

    # Monotonically decreasing: the slowest setting produces the longest
    # predicted time, the fastest the shortest. A ride slower than even the
    # slowest setting predicts, or faster than even the fastest, has no
    # root inside the range - clamp to the nearer end instead.
    if actual_time_s >= time_at_min:
        return MIN_FLAT_SPEED_KMH
    if actual_time_s <= time_at_max:
        return MAX_FLAT_SPEED_KMH

    lo, hi = MIN_FLAT_SPEED_KMH, MAX_FLAT_SPEED_KMH
    for _ in range(BISECTION_MAX_ITER):
        mid = (lo + hi) / 2
        predicted = _predicted_total_s(elevation, surface, settings, mid)
        if abs(predicted - actual_time_s) < BISECTION_TOLERANCE_S:
            return mid
        if predicted > actual_time_s:
            # Predicted still too slow (too long) - need a faster setting.
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


async def suggest_flat_speed_kmh(
    db: AsyncSession, user_id: uuid.UUID, settings: UserSettingsResponse
) -> CalibrationSuggestion | None:
    """Only ever draws on `user_id`'s own rides and routes - the join is on
    Route.id == Activity.route_id (which already excludes rides with no
    match) filtered by both tables' user_id, the same belt-and-braces
    pattern GET /api/routes/{id}/activities uses, so this endpoint's
    isolation does not depend on route_match.py or set_activity_route never
    changing."""
    rows = (
        await db.execute(
            select(Activity, Route)
            .join(Route, Route.id == Activity.route_id)
            # A locked match can outlive the geometry it was made against:
            # re-routing a saved route re-derives its unlocked rides and
            # deliberately leaves the rider's own picks alone, so the link
            # stays while the road changes. Solving the NEW elevation against
            # the OLD ride's moving time is not a smaller error - it offered a
            # real 26 km/h rider the model's 60 km/h ceiling, one click from
            # writing that into the setting every ETA in the app reads.
            .where(Activity.match_stale.is_(False))
            .where(
                Activity.user_id == user_id,
                Route.user_id == user_id,
                Activity.moving_time_s.is_not(None),
            )
        )
    ).all()

    implied: list[float] = []
    for activity, route in rows:
        if activity.moving_time_s is None:
            continue
        elevation = [ElevationPoint(**p) for p in route.elevation]
        surface = SurfaceBreakdown(**route.surface) if route.surface else None
        solved = solve_flat_speed_kmh(elevation, surface, settings, activity.moving_time_s)
        if solved is not None:
            implied.append(solved)

    if len(implied) < MIN_USABLE_RIDES:
        return None

    suggested = min(max(statistics.median(implied), MIN_FLAT_SPEED_KMH), MAX_FLAT_SPEED_KMH)
    return CalibrationSuggestion(
        suggested_kmh=round(suggested, 1),
        sample_size=len(implied),
        current_kmh=settings.flat_speed_kmh,
    )
