"""Climb detection: a pure function over an elevation profile.

Segments a route's elevation samples into climbs and categorises each with
the same length*grade^2 scoring road cycling uses informally, calibrated
here against real climbs: Alpe d'Huez (13.8 km @ 8.1%) scores ~905 -> HC;
8 km @ 7% ~392 -> 1; 5 km @ 6% ~180 -> 2; 3 km @ 5% ~75 -> 3; 1.5 km @ 4%
~24 -> 4.

This lives in the backend, not gradient.ts, because it needs its own
smoothing. gradient.ts's point-to-point bands (and valhalla.py's
ascent_descent) are deliberately unsmoothed - fine for a coarse visual or a
cumulative total, but the raw /height profile is noisy enough sample to
sample that an unsmoothed grade series segments an ordinary ride into
dozens of spurious sub-500m "climbs" out of nothing but sensor noise.
Smoothing once here, as a pure function of elevation, also gets it real
pytest coverage instead of re-deriving the same thing per client.
"""

from app.schemas import Climb, ElevationPoint

# The raw elevation array is at most MAX_ELEVATION_SAMPLES (500) points
# however long the route, so it is resampled onto a uniform grid first -
# decoupling every constant below from route length.
RESAMPLE_STEP_M = 25.0
# Centred moving average width. Wide enough to smooth over /height's
# point-to-point noise, narrow enough not to blur a real climb's boundaries
# by more than a grid cell or two.
SMOOTH_WINDOW_M = 150.0
CLIMB_MIN_GRADE_PCT = 3.0
# A dip inside a climb is swallowed (treated as a roller, not the end of
# the climb) only if it is both short and shallow. Either threshold alone
# would either merge a real valley between two separate climbs, or split a
# climb at every cattle grid.
MERGE_MAX_GAP_M = 500.0
MERGE_MAX_GAP_LOSS_M = 15.0
MIN_CLIMB_LENGTH_M = 500.0
MIN_CLIMB_GAIN_M = 20.0
# Checked highest-first: the first threshold a climb's score clears wins.
# A candidate that passes the length/gain minimums above but still scores
# under the lowest ("4") threshold is discarded - those minimums alone do
# not guarantee it (500 m at exactly 20 m gain scores only 8).
CATEGORY_THRESHOLDS: list[tuple[float, str]] = [
    (600.0, "HC"),
    (300.0, "1"),
    (150.0, "2"),
    (70.0, "3"),
    (20.0, "4"),
]

# The window used to find a climb's steepest stretch, distinct from the
# smoothing window above.
MAX_GRADE_WINDOW_M = 100.0


def detect_climbs(elevation: list[ElevationPoint]) -> list[Climb]:
    if len(elevation) < 2:
        return []
    total_dist = elevation[-1].dist_m
    if total_dist <= 0:
        return []

    grid_dists, grid_elevs = _resample(elevation, RESAMPLE_STEP_M)
    smoothed = _smooth(grid_dists, grid_elevs, SMOOTH_WINDOW_M)

    runs = _raw_climbing_runs(grid_dists, smoothed, CLIMB_MIN_GRADE_PCT)
    merged = _merge_runs(grid_dists, smoothed, runs)

    climbs = []
    for start_idx, end_idx in merged:
        climb = _build_climb(grid_dists, smoothed, start_idx, end_idx)
        if climb is not None:
            climbs.append(climb)
    return climbs


def _resample(elevation: list[ElevationPoint], step_m: float) -> tuple[list[float], list[float]]:
    """Linear interpolation onto a uniform grid, always including the exact
    start and end distance of the source profile."""
    total = elevation[-1].dist_m
    count = int(total // step_m) + 1
    grid_dists = [i * step_m for i in range(count)]
    if grid_dists[-1] < total:
        grid_dists.append(total)

    src_dists = [p.dist_m for p in elevation]
    src_elevs = [p.elev_m for p in elevation]
    grid_elevs = [_interp(src_dists, src_elevs, d) for d in grid_dists]
    return grid_dists, grid_elevs


def _interp(xs: list[float], ys: list[float], x: float) -> float:
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    lo, hi = 0, len(xs) - 1
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if xs[mid] <= x:
            lo = mid
        else:
            hi = mid
    span = xs[hi] - xs[lo]
    t = (x - xs[lo]) / span if span > 0 else 0.0
    return ys[lo] + (ys[hi] - ys[lo]) * t


def _smooth(dists: list[float], elevs: list[float], window_m: float) -> list[float]:
    """Centred moving average over a distance window rather than a fixed
    sample count, so it behaves the same whether the grid step ever changes.
    Windows truncate at the ends of the series rather than wrapping or
    padding - `detect_climbs` deliberately keeps the profile's exact start
    and end in the grid, so a climb that runs to either edge still gets a
    real (if slightly asymmetric) smoothed value there instead of none."""
    half = window_m / 2.0
    n = len(dists)
    smoothed = []
    for i in range(n):
        lo = i
        while lo > 0 and dists[i] - dists[lo - 1] <= half:
            lo -= 1
        hi = i
        while hi < n - 1 and dists[hi + 1] - dists[i] <= half:
            hi += 1
        window = elevs[lo : hi + 1]
        smoothed.append(sum(window) / len(window))
    return smoothed


def _grade(dists: list[float], elevs: list[float], i: int, j: int) -> float:
    span = dists[j] - dists[i]
    return (elevs[j] - elevs[i]) / span * 100.0 if span > 0 else 0.0


def _raw_climbing_runs(
    dists: list[float], elevs: list[float], min_grade_pct: float
) -> list[tuple[int, int]]:
    """Maximal sample ranges where every consecutive step's grade clears the
    threshold - before any dip-swallowing merge is applied."""
    runs: list[tuple[int, int]] = []
    n = len(dists)
    i = 0
    while i < n - 1:
        if _grade(dists, elevs, i, i + 1) >= min_grade_pct:
            start = i
            while i < n - 1 and _grade(dists, elevs, i, i + 1) >= min_grade_pct:
                i += 1
            runs.append((start, i))
        else:
            i += 1
    return runs


def _merge_runs(
    dists: list[float], elevs: list[float], runs: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """Join consecutive climbing runs across a gap that is both short
    (< MERGE_MAX_GAP_M) and shallow (< MERGE_MAX_GAP_LOSS_M lost) - a roller
    or false flat mid-climb, not a real end to it."""
    if not runs:
        return []
    merged = [runs[0]]
    for start, end in runs[1:]:
        prev_start, prev_end = merged[-1]
        gap_m = dists[start] - dists[prev_end]
        gap_loss_m = elevs[prev_end] - min(elevs[prev_end : start + 1])
        if gap_m < MERGE_MAX_GAP_M and gap_loss_m < MERGE_MAX_GAP_LOSS_M:
            merged[-1] = (prev_start, end)
        else:
            merged.append((start, end))
    return merged


def _max_grade_over_window(
    dists: list[float], elevs: list[float], start_idx: int, end_idx: int, window_m: float
) -> float:
    """The steepest grade over any sub-window of at most `window_m`, sliding
    the window's start across every sample in the climb."""
    best = 0.0
    for i in range(start_idx, end_idx):
        j = i
        while j < end_idx and dists[j + 1] - dists[i] <= window_m:
            j += 1
        grade = _grade(dists, elevs, i, j)
        if grade > best:
            best = grade
    return best


def _category(score: float) -> str | None:
    for threshold, name in CATEGORY_THRESHOLDS:
        if score >= threshold:
            return name
    return None


def _build_climb(
    dists: list[float], elevs: list[float], start_idx: int, end_idx: int
) -> Climb | None:
    length_m = dists[end_idx] - dists[start_idx]
    gain_m = elevs[end_idx] - elevs[start_idx]
    if length_m < MIN_CLIMB_LENGTH_M or gain_m < MIN_CLIMB_GAIN_M:
        return None

    avg_grade_pct = gain_m / length_m * 100.0
    score = length_m * avg_grade_pct**2 / 1000.0
    category = _category(score)
    if category is None:
        return None

    max_grade_pct = _max_grade_over_window(dists, elevs, start_idx, end_idx, MAX_GRADE_WINDOW_M)
    return Climb(
        start_dist_m=dists[start_idx],
        end_dist_m=dists[end_idx],
        length_m=length_m,
        gain_m=gain_m,
        avg_grade_pct=avg_grade_pct,
        max_grade_pct=max(max_grade_pct, avg_grade_pct),
        score=score,
        category=category,
    )
