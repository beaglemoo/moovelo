"""Geometry helper tests, notably `bearing` (used by weather wind maths)."""

import pytest

from app.services.geo import bearing

ORIGIN = (51.0, -1.0)


def test_bearing_due_north() -> None:
    assert bearing(ORIGIN, (51.1, -1.0)) == pytest.approx(0.0, abs=0.5)


def test_bearing_due_east() -> None:
    assert bearing(ORIGIN, (51.0, -0.9)) == pytest.approx(90.0, abs=0.5)


def test_bearing_due_south() -> None:
    assert bearing(ORIGIN, (50.9, -1.0)) == pytest.approx(180.0, abs=0.5)


def test_bearing_due_west() -> None:
    assert bearing(ORIGIN, (51.0, -1.1)) == pytest.approx(270.0, abs=0.5)
