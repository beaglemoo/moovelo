"""Realistic ride-time model: gradient/surface factors and the walked series.

`duration_s` never appears in these tests - it is Valhalla's own estimate
and this model never touches it. `test_api_ride_time.py` covers the API
wiring; this file is pure unit tests of `services/ride_time.py`.
"""

import pytest

from app.schemas import ElevationPoint, SurfaceBreakdown, UserSettingsResponse
from app.services.ride_time import (
    FTP_BASELINE_W_PER_KG,
    compute_ride_time,
    effective_flat_speed,
    gradient_factor,
    surface_factor,
)


def _settings(
    weight_kg: float = 78.0, flat_speed_kmh: float = 22.0, ftp_watts: float | None = None
) -> UserSettingsResponse:
    return UserSettingsResponse(
        weight_kg=weight_kg, flat_speed_kmh=flat_speed_kmh, ftp_watts=ftp_watts
    )


class TestGradientFactor:
    def test_boundary_values(self) -> None:
        assert gradient_factor(-3.0) == pytest.approx(1.35)
        assert gradient_factor(0.0) == pytest.approx(1.0)
        assert gradient_factor(3.0) == pytest.approx(0.75)
        assert gradient_factor(6.0) == pytest.approx(0.5)
        assert gradient_factor(9.0) == pytest.approx(0.35)
        # The deliberate discontinuity: 12% is the floor, not the lerp end.
        assert gradient_factor(12.0) == pytest.approx(0.20)

    def test_midpoints(self) -> None:
        assert gradient_factor(1.5) == pytest.approx(0.875)
        assert gradient_factor(-1.5) == pytest.approx(1.175)
        assert gradient_factor(4.5) == pytest.approx(0.625)
        assert gradient_factor(7.5) == pytest.approx(0.425)
        assert gradient_factor(10.5) == pytest.approx(0.30)

    def test_continuity_within_each_span(self) -> None:
        for lo, hi in [(-3, 0), (0, 3), (3, 6), (6, 9), (9, 12)]:
            just_inside = hi - 0.001
            approaching_from_inside = gradient_factor(just_inside)
            at_boundary = gradient_factor(float(hi))
            if hi == 12:
                # The one span where the boundary is not the lerp's own end.
                continue
            assert approaching_from_inside == pytest.approx(at_boundary, abs=0.001)
            assert gradient_factor(lo) == pytest.approx(gradient_factor(lo + 1e-9), abs=1e-6)

    def test_cap_below_minus_three(self) -> None:
        assert gradient_factor(-3.0) == pytest.approx(1.35)
        assert gradient_factor(-10.0) == pytest.approx(1.35)
        assert gradient_factor(-99.0) == pytest.approx(1.35)

    def test_floor_at_and_above_twelve(self) -> None:
        assert gradient_factor(12.0) == pytest.approx(0.20)
        assert gradient_factor(20.0) == pytest.approx(0.20)
        assert gradient_factor(99.0) == pytest.approx(0.20)


class TestSurfaceFactor:
    def test_none_is_neutral(self) -> None:
        assert surface_factor(None) == pytest.approx(1.0)

    def test_empty_total_is_neutral(self) -> None:
        assert surface_factor(SurfaceBreakdown(total_m=0.0)) == pytest.approx(1.0)

    def test_all_paved_is_neutral(self) -> None:
        surface = SurfaceBreakdown(total_m=1000.0, surface_m={"paved": 1000.0})
        assert surface_factor(surface) == pytest.approx(1.0)

    def test_fifty_fifty_paved_gravel(self) -> None:
        surface = SurfaceBreakdown(total_m=1000.0, surface_m={"paved": 500.0, "gravel": 500.0})
        assert surface_factor(surface) == pytest.approx(0.925)

    def test_unknown_key_contributes_the_unknown_factor(self) -> None:
        surface = SurfaceBreakdown(total_m=1000.0, surface_m={"cobblestone": 1000.0})
        assert surface_factor(surface) == pytest.approx(0.9)

    def test_unaccounted_metres_are_treated_as_unknown(self) -> None:
        # 400 m of surface_m over an 1000 m total - the missing 600 m is
        # edges Valhalla returned no surface tag for at all.
        surface = SurfaceBreakdown(total_m=1000.0, surface_m={"paved": 400.0})
        expected = (400.0 * 1.0 + 600.0 * 0.9) / 1000.0
        assert surface_factor(surface) == pytest.approx(expected)


class TestEffectiveFlatSpeed:
    def test_no_ftp_returns_flat_speed_unchanged(self) -> None:
        assert effective_flat_speed(_settings(flat_speed_kmh=25.0)) == pytest.approx(25.0)

    def test_ftp_at_exactly_baseline_is_unchanged(self) -> None:
        weight_kg = 70.0
        ftp_watts = FTP_BASELINE_W_PER_KG * weight_kg
        settings = _settings(weight_kg=weight_kg, flat_speed_kmh=22.0, ftp_watts=ftp_watts)
        assert effective_flat_speed(settings) == pytest.approx(22.0)

    def test_higher_ftp_scales_by_the_cube_root(self) -> None:
        # 8x the baseline w/kg -> cube root of 8 = 2 -> exactly double speed.
        weight_kg = 70.0
        ftp_watts = FTP_BASELINE_W_PER_KG * weight_kg * 8
        settings = _settings(weight_kg=weight_kg, flat_speed_kmh=22.0, ftp_watts=ftp_watts)
        assert effective_flat_speed(settings) == pytest.approx(44.0)

    def test_zero_ftp_is_ignored_rather_than_cubed_into_zero_speed(self) -> None:
        """The schema now rejects a 0 on write, but a 0 already stored by an
        older row must not be treated as real wattage here either - `if
        settings.ftp_watts` (truthiness), not `is None`."""
        settings = _settings(flat_speed_kmh=25.0, ftp_watts=0.0)
        assert effective_flat_speed(settings) == pytest.approx(25.0)


class TestComputeRideTime:
    def test_flat_ten_km_at_defaults(self) -> None:
        elevation = [
            ElevationPoint(dist_m=0.0, elev_m=100.0),
            ElevationPoint(dist_m=10_000.0, elev_m=100.0),
        ]
        result = compute_ride_time(elevation, None, _settings())
        assert result[0].dist_m == pytest.approx(0.0)
        assert result[0].time_s == pytest.approx(0.0)
        assert result[-1].dist_m == pytest.approx(10_000.0)
        assert result[-1].time_s == pytest.approx((10.0 / 22.0) * 3600.0)

    def test_a_climb_is_slower_than_flat_and_monotonic(self) -> None:
        flat = [
            ElevationPoint(dist_m=0.0, elev_m=100.0),
            ElevationPoint(dist_m=5_000.0, elev_m=100.0),
        ]
        climb = [
            ElevationPoint(dist_m=0.0, elev_m=100.0),
            ElevationPoint(dist_m=1_000.0, elev_m=160.0),  # 6% grade
            ElevationPoint(dist_m=3_000.0, elev_m=280.0),  # 6% grade
            ElevationPoint(dist_m=5_000.0, elev_m=280.0),  # flat again
        ]
        flat_time = compute_ride_time(flat, None, _settings())[-1].time_s
        climb_result = compute_ride_time(climb, None, _settings())
        assert climb_result[-1].time_s > flat_time

        times = [p.time_s for p in climb_result]
        assert times == sorted(times)
        dists = [p.dist_m for p in climb_result]
        assert dists == [p.dist_m for p in climb]

    def test_empty_or_single_point_elevation_is_empty(self) -> None:
        assert compute_ride_time([], None, _settings()) == []
        single = [ElevationPoint(dist_m=0.0, elev_m=100.0)]
        assert compute_ride_time(single, None, _settings()) == []
