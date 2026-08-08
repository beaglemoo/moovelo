"""Realistic ride-time estimate: additive, display-layer, computed on read.

`RouteResponse.duration_s` is Valhalla's own estimate and is never touched
here - it drives FIT course-point timing and the Wahoo push payload. This
module only ever produces `ride_time`, a separate series computed fresh from
elevation, surface and the rider's settings every time a route is read, and
never persisted. A settings change therefore changes every already-saved
route's displayed time without a migration or a re-save.
"""

from app.schemas import ElevationPoint, RideTimePoint, SurfaceBreakdown, UserSettingsResponse

# Piecewise-linear over grade %, breakpoint -> multiplier on flat-road speed.
# Deliberately discontinuous at the top: 0.25 at 12% steps down to a hard
# 0.20 floor rather than continuing the lerp, because nothing about a climb
# beyond 12% gets meaningfully easier as it steepens further.
_GRADIENT_BREAKPOINTS: list[tuple[float, float]] = [
    (-3.0, 1.35),
    (0.0, 1.0),
    (3.0, 0.75),
    (6.0, 0.5),
    (9.0, 0.35),
    (12.0, 0.25),
]
GRADIENT_CAP = 1.35
GRADIENT_FLOOR = 0.20

SURFACE_FACTORS: dict[str, float] = {
    "paved_smooth": 1.0,
    "paved": 1.0,
    "paved_rough": 0.95,
    "compacted": 0.85,
    "gravel": 0.85,
    "dirt": 0.75,
    "path": 0.75,
}
UNKNOWN_SURFACE_FACTOR = 0.9

# Watts per kilogram the flat_speed_kmh setting is assumed to already
# represent; effective_flat_speed scales around this baseline.
FTP_BASELINE_W_PER_KG = 2.8

# A degenerate/very steep downhill segment must never produce a near-infinite
# speed (and so a near-zero time); this is the slowest a rider is modelled
# to go, regardless of gradient and surface.
MIN_SPEED_KMH = 2.0


def gradient_factor(grade_pct: float) -> float:
    """Speed multiplier for a grade, continuous within each lerp span."""
    if grade_pct <= _GRADIENT_BREAKPOINTS[0][0]:
        return GRADIENT_CAP
    if grade_pct >= _GRADIENT_BREAKPOINTS[-1][0]:
        return GRADIENT_FLOOR
    for (x0, y0), (x1, y1) in zip(_GRADIENT_BREAKPOINTS, _GRADIENT_BREAKPOINTS[1:], strict=False):
        if x0 <= grade_pct <= x1:
            t = (grade_pct - x0) / (x1 - x0)
            return y0 + (y1 - y0) * t
    return GRADIENT_FLOOR  # pragma: no cover - unreachable, breakpoints span the range


def surface_factor(surface: SurfaceBreakdown | None) -> float:
    """Length-weighted mean of per-surface-key factors over the whole route.

    This is one route-level multiplier, not applied per position: the
    breakdown is an aggregate over the whole ride, so there is no way to
    know which stretch of elevation a given surface metre belongs to.
    """
    if surface is None or surface.total_m <= 0:
        return 1.0
    weighted = 0.0
    accounted_m = 0.0
    for key, length_m in surface.surface_m.items():
        weighted += length_m * SURFACE_FACTORS.get(key, UNKNOWN_SURFACE_FACTOR)
        accounted_m += length_m
    # Edges with no surface tag at all contribute to total_m but never
    # appear as a surface_m key; treat that remainder as unknown too.
    remainder_m = surface.total_m - accounted_m
    if remainder_m > 0:
        weighted += remainder_m * UNKNOWN_SURFACE_FACTOR
    return weighted / surface.total_m


def effective_flat_speed(settings: UserSettingsResponse) -> float:
    """flat_speed_kmh, nudged by FTP when set.

    Cube root because aero power scales roughly with v^3 - a one-sentence
    nudge, not a physics model. A rider putting out double the baseline
    watts/kg is modelled as riding about 26% faster on the flat, not twice
    as fast.
    """
    # Truthiness, not `is None`: the schema now rejects a 0 on write
    # (UserSettingsPatch.ftp_watts is gt=0), but a 0 already stored by an
    # older row must not be cubed into a zero speed here either.
    if not settings.ftp_watts:
        return settings.flat_speed_kmh
    w_per_kg = settings.ftp_watts / settings.weight_kg
    scale: float = (w_per_kg / FTP_BASELINE_W_PER_KG) ** (1 / 3)
    return settings.flat_speed_kmh * scale


def compute_ride_time(
    elevation: list[ElevationPoint],
    surface: SurfaceBreakdown | None,
    settings: UserSettingsResponse,
) -> list[RideTimePoint]:
    """One RideTimePoint per elevation point, walking consecutive pairs.

    Fewer than two elevation points means there is no gradient to walk, so
    this returns [] rather than a single degenerate point.
    """
    if len(elevation) < 2:
        return []

    flat_speed_kmh = effective_flat_speed(settings)
    surf_factor = surface_factor(surface)

    points = [RideTimePoint(dist_m=elevation[0].dist_m, time_s=0.0)]
    elapsed_s = 0.0
    for prev, curr in zip(elevation, elevation[1:], strict=False):
        span_m = curr.dist_m - prev.dist_m
        if span_m > 0:
            grade_pct = (curr.elev_m - prev.elev_m) / span_m * 100
            speed_kmh = max(
                flat_speed_kmh * gradient_factor(grade_pct) * surf_factor, MIN_SPEED_KMH
            )
            elapsed_s += span_m / (speed_kmh * 1000 / 3600)
        points.append(RideTimePoint(dist_m=curr.dist_m, time_s=elapsed_s))
    return points
