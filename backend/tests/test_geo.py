"""Geometry helper tests, notably `bearing` (used by weather wind maths) and
`destination_point` (used by the loop generator, services/loop.py)."""

import pytest

from app.services.geo import bearing, destination_point, haversine

ORIGIN = (51.0, -1.0)


def test_bearing_due_north() -> None:
    assert bearing(ORIGIN, (51.1, -1.0)) == pytest.approx(0.0, abs=0.5)


def test_bearing_due_east() -> None:
    assert bearing(ORIGIN, (51.0, -0.9)) == pytest.approx(90.0, abs=0.5)


def test_bearing_due_south() -> None:
    assert bearing(ORIGIN, (50.9, -1.0)) == pytest.approx(180.0, abs=0.5)


def test_bearing_due_west() -> None:
    assert bearing(ORIGIN, (51.0, -1.1)) == pytest.approx(270.0, abs=0.5)


def test_destination_point_round_trips_with_haversine_and_bearing() -> None:
    """The inverse relationship destination_point's docstring claims: going
    5 km at bearing 90 (due east) from a known point lands 5000 m away by
    haversine, and bearing() from the origin back to that point reads ~90."""
    destination = destination_point(ORIGIN, 90.0, 5000.0)
    assert haversine(ORIGIN, destination) == pytest.approx(5000.0, rel=0.001)
    assert bearing(ORIGIN, destination) == pytest.approx(90.0, abs=0.1)


def test_destination_point_due_north_keeps_longitude() -> None:
    destination = destination_point(ORIGIN, 0.0, 1000.0)
    assert destination[0] > ORIGIN[0]
    assert destination[1] == pytest.approx(ORIGIN[1], abs=1e-9)
