"""Weather service and endpoint tests against real Open-Meteo fixtures."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import respx
from httpx import AsyncClient, Response

from app.config import settings
from app.schemas import RideTimePoint
from app.services import weather
from app.services.weather import WeatherError, weather_along_route
from tests.conftest import register

WEATHER_URL = "https://weather.test/v1/forecast"

FIXTURES = Path(__file__).parent / "fixtures"
OPEN_METEO_MULTI = json.loads((FIXTURES / "open_meteo_multi.json").read_text())
OPEN_METEO_SINGLE = json.loads((FIXTURES / "open_meteo_single.json").read_text())

# ~22.24 km due north of (51.0, -1.0) - long enough for three 10 km samples
# (0, 10 km, 20 km) but short enough to stay under MAX_WEATHER_SAMPLES.
LONG_LINE = [(-1.0, 51.0), (-1.0, 51.2)]
# ~556 m due north - short enough that only the start is sampled.
SHORT_LINE = [(-1.0, 51.0), (-1.0, 51.005)]


@pytest.fixture
def weather_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(settings, "weather_api_url", WEATHER_URL)
    yield


@pytest.fixture(autouse=True)
def clear_weather_cache() -> Iterator[None]:
    weather._cache.clear()
    yield
    weather._cache.clear()


def shape_of(line: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """[lon, lat] -> (lat, lon), matching the API's own conversion."""
    return [(lat, lon) for lon, lat in line]


@respx.mock
async def test_multi_sample_route_parses_list_shape(weather_settings: None) -> None:
    respx.get(WEATHER_URL).mock(return_value=Response(200, json=OPEN_METEO_MULTI))
    start = datetime(2026, 8, 7, 10, 0, tzinfo=UTC)

    result = await weather_along_route(
        shape_of(LONG_LINE), start, ride_time=None, duration_s=18000.0
    )

    assert result.truncated is False
    assert len(result.segments) == 3

    first, second, third = result.segments
    assert first.dist_along_m == pytest.approx(0.0)
    assert first.arrival_iso == start.isoformat()
    assert first.wind_speed_ms == pytest.approx(0.9)
    assert first.wind_direction_deg == pytest.approx(352.0)

    assert second.dist_along_m == pytest.approx(10000.0)
    assert second.wind_speed_ms == pytest.approx(1.4)
    assert second.wind_direction_deg == pytest.approx(294.0)

    assert third.dist_along_m == pytest.approx(20000.0)
    assert third.wind_speed_ms == pytest.approx(1.9)
    assert third.wind_direction_deg == pytest.approx(288.0)


@respx.mock
async def test_single_sample_route_handles_object_shape(weather_settings: None) -> None:
    """A short route yields one sample; Open-Meteo answers a bare object for
    one location rather than a one-element list, and the client must cope."""
    respx.get(WEATHER_URL).mock(return_value=Response(200, json=OPEN_METEO_SINGLE))
    start = datetime(2026, 8, 7, 5, 0, tzinfo=UTC)

    result = await weather_along_route(shape_of(SHORT_LINE), start, ride_time=None, duration_s=None)

    assert result.truncated is False
    assert len(result.segments) == 1
    segment = result.segments[0]
    assert segment.dist_along_m == pytest.approx(0.0)
    assert segment.arrival_iso == start.isoformat()
    assert segment.wind_speed_ms == pytest.approx(1.0)
    assert segment.wind_direction_deg == pytest.approx(110.0)


def test_headwind_due_north_travel_wind_from_north(weather_settings: None) -> None:
    # Wind blows FROM the north (direction 0) into a rider heading due
    # north: it slows them head-on, so headwind == wind speed, no crosswind.
    headwind, crosswind = _wind_components(travel_bearing=0.0, wind_direction=0.0, speed=5.0)
    assert headwind == pytest.approx(5.0)
    assert crosswind == pytest.approx(0.0, abs=1e-9)


def test_tailwind_due_north_travel_wind_from_south(weather_settings: None) -> None:
    # Wind FROM the south blows the same way the rider travels: a tailwind,
    # which reads as a negative headwind.
    headwind, crosswind = _wind_components(travel_bearing=0.0, wind_direction=180.0, speed=5.0)
    assert headwind == pytest.approx(-5.0)
    assert crosswind == pytest.approx(0.0, abs=1e-9)


def test_crosswind_due_north_travel_wind_from_east(weather_settings: None) -> None:
    headwind, crosswind = _wind_components(travel_bearing=0.0, wind_direction=90.0, speed=5.0)
    assert headwind == pytest.approx(0.0, abs=1e-9)
    assert abs(crosswind) == pytest.approx(5.0)


def _wind_components(
    travel_bearing: float, wind_direction: float, speed: float
) -> tuple[float, float]:
    import math

    downwind = (wind_direction + 180.0) % 360.0
    relative = weather._smallest_signed_angle(downwind, travel_bearing)
    headwind = -speed * math.cos(math.radians(relative))
    crosswind = speed * math.sin(math.radians(relative))
    return headwind, crosswind


@respx.mock
async def test_retries_once_on_500(weather_settings: None) -> None:
    single = {
        "latitude": 51.0,
        "longitude": -1.0,
        "hourly": {
            "time": ["2026-08-07T09:00"],
            "wind_speed_10m": [3.0],
            "wind_direction_10m": [180.0],
        },
    }
    calls = respx.get(WEATHER_URL)
    calls.side_effect = [Response(500), Response(200, json=single)]
    start = datetime(2026, 8, 7, 9, 0, tzinfo=UTC)

    result = await weather_along_route(shape_of(SHORT_LINE), start, ride_time=None, duration_s=None)

    assert calls.call_count == 2
    assert len(result.segments) == 1
    assert result.segments[0].wind_speed_ms == pytest.approx(3.0)


@respx.mock
async def test_repeated_5xx_raises_weather_error(weather_settings: None) -> None:
    respx.get(WEATHER_URL).mock(return_value=Response(500))
    start = datetime(2026, 8, 7, 9, 0, tzinfo=UTC)

    with pytest.raises(WeatherError):
        await weather_along_route(shape_of(SHORT_LINE), start, ride_time=None, duration_s=None)


@respx.mock
async def test_out_of_range_window_degrades_to_truncated(weather_settings: None) -> None:
    respx.get(WEATHER_URL).mock(
        return_value=Response(
            400,
            json={
                "reason": "Parameter 'start_date' is out of allowed range from "
                "2026-07-01 to 2026-08-21",
                "error": True,
            },
        )
    )
    start = datetime(2026, 8, 7, 9, 0, tzinfo=UTC)

    result = await weather_along_route(shape_of(SHORT_LINE), start, ride_time=None, duration_s=None)

    assert result.truncated is True
    assert result.segments == []


def line_of(pairs: list[tuple[float, float]]) -> list[list[float]]:
    return [[lon, lat] for lon, lat in pairs]


async def test_weather_endpoint_404_when_unconfigured(client: AsyncClient) -> None:
    await register(client)
    with respx.mock:
        # The test client's own self-calls go to host "test" over an ASGI
        # transport, not the network - let those through untouched (as
        # test_wahoo.py's flow test does) and only watch for a real
        # outbound request, which must never happen.
        respx.route(host="test").pass_through()
        catch_all = respx.route().mock(return_value=Response(200, json={}))
        response = await client.post(
            "/api/route/weather",
            json={"line": line_of(SHORT_LINE), "start_time": "2026-08-07T09:00:00+00:00"},
        )
        assert response.status_code == 404
        assert catch_all.call_count == 0


async def test_weather_endpoint_rejects_naive_start_time(client: AsyncClient) -> None:
    await register(client)
    response = await client.post(
        "/api/route/weather",
        json={"line": line_of(SHORT_LINE), "start_time": "2026-08-07T09:00:00"},
    )
    assert response.status_code == 422


@respx.mock
async def test_weather_endpoint_maps_repeated_5xx_to_502(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "weather_api_url", WEATHER_URL)
    respx.get(WEATHER_URL).mock(return_value=Response(500))
    await register(client)

    response = await client.post(
        "/api/route/weather",
        json={"line": line_of(SHORT_LINE), "start_time": "2026-08-07T09:00:00+00:00"},
    )

    assert response.status_code == 502


@respx.mock
async def test_weather_endpoint_returns_segments_when_enabled(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "weather_api_url", WEATHER_URL)
    respx.get(WEATHER_URL).mock(return_value=Response(200, json=OPEN_METEO_SINGLE))
    await register(client)

    response = await client.post(
        "/api/route/weather",
        json={"line": line_of(SHORT_LINE), "start_time": "2026-08-07T05:00:00+00:00"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["truncated"] is False
    assert len(body["segments"]) == 1
    assert body["segments"][0]["wind_speed_ms"] == pytest.approx(1.0)


@respx.mock
async def test_http_date_retry_after_backs_off_instead_of_crashing(
    weather_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retry-After may legally be an HTTP-date, not delay-seconds - it must
    fall back to the exponential default, not raise ValueError into a 500."""
    single = {
        "latitude": 51.0,
        "longitude": -1.0,
        "hourly": {
            "time": ["2026-08-07T09:00"],
            "wind_speed_10m": [3.0],
            "wind_direction_10m": [180.0],
        },
    }
    slept: list[float] = []

    async def no_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(weather.asyncio, "sleep", no_sleep)
    calls = respx.get(WEATHER_URL)
    calls.side_effect = [
        Response(429, headers={"Retry-After": "Wed, 07 Aug 2026 12:00:00 GMT"}),
        Response(200, json=single),
    ]
    start = datetime(2026, 8, 7, 9, 0, tzinfo=UTC)

    result = await weather_along_route(shape_of(SHORT_LINE), start, ride_time=None, duration_s=None)

    assert calls.call_count == 2
    assert slept == [2.0]
    assert len(result.segments) == 1


@respx.mock
async def test_null_hourly_slot_skips_the_sample_not_the_request(weather_settings: None) -> None:
    """Open-Meteo can return null for individual hourly slots at the edge of
    the model's range; the sample is skipped as truncated, never a 500."""
    single = {
        "latitude": 51.0,
        "longitude": -1.0,
        "hourly": {
            "time": ["2026-08-07T09:00"],
            "wind_speed_10m": [None],
            "wind_direction_10m": [None],
        },
    }
    respx.get(WEATHER_URL).mock(return_value=Response(200, json=single))
    start = datetime(2026, 8, 7, 9, 0, tzinfo=UTC)

    result = await weather_along_route(shape_of(SHORT_LINE), start, ride_time=None, duration_s=None)

    assert result.segments == []
    assert result.truncated is True


def test_cache_stays_bounded() -> None:
    empty = weather.WeatherAlongRoute(segments=[], truncated=False)
    for i in range(weather._CACHE_MAX_ENTRIES + 40):
        weather._cache_set(f"key-{i}", empty)
    assert len(weather._cache) <= weather._CACHE_MAX_ENTRIES


@respx.mock
async def test_cache_key_includes_pacing_so_different_durations_dont_collide(
    weather_settings: None,
) -> None:
    """Same shape and start hour, two different durations: the second call
    must ask upstream again rather than reuse the first's arrival times."""
    calls = respx.get(WEATHER_URL)
    calls.side_effect = [
        Response(200, json=OPEN_METEO_MULTI),
        Response(200, json=OPEN_METEO_MULTI),
    ]
    start = datetime(2026, 8, 7, 10, 0, tzinfo=UTC)

    short = await weather_along_route(shape_of(LONG_LINE), start, ride_time=None, duration_s=3600.0)
    long = await weather_along_route(shape_of(LONG_LINE), start, ride_time=None, duration_s=36000.0)

    assert calls.call_count == 2
    assert short.segments[1].arrival_iso != long.segments[1].arrival_iso


def test_sample_route_spreads_evenly_past_the_spacing_cap() -> None:
    """Fixed 10 km spacing would stop at MAX_WEATHER_SAMPLES (20) samples -
    190 km - and silently drop coverage over anything further. Past that,
    samples spread evenly across the whole route instead."""
    # Due-north line of about 300 km.
    end_lat = 51.0 + 300_000 / 111_320
    line = [(51.0, -1.0), (end_lat, -1.0)]

    samples, total = weather._sample_route(line)

    assert total > 290_000.0
    assert len(samples) == weather.MAX_WEATHER_SAMPLES
    assert samples[-1][1] == pytest.approx(total)
    spacing = samples[1][1] - samples[0][1]
    assert spacing == pytest.approx(total / (weather.MAX_WEATHER_SAMPLES - 1))
    assert spacing == pytest.approx(15_800.0, rel=0.05)


def test_sample_route_keeps_fixed_spacing_under_the_cap() -> None:
    """A route short enough to fit within the cap at fixed spacing keeps
    exact 10 km steps rather than being spread evenly."""
    end_lat = 51.0 + 25_000 / 111_320
    line = [(51.0, -1.0), (end_lat, -1.0)]

    samples, _total = weather._sample_route(line)

    dists = [dist for _, dist in samples]
    assert dists == pytest.approx([0.0, 10_000.0, 20_000.0], abs=50.0)


@respx.mock
async def test_forecast_window_sized_from_ride_time_not_duration_s(
    weather_settings: None,
) -> None:
    """duration_s alone (1 hour from a 20:00 start) would never cross
    midnight; the elevation-aware ride_time profile it should be sized from
    instead totals 5 hours, which does - and arrivals are timed against
    ride_time too, so the window must cover the same timeline."""
    mock = respx.get(WEATHER_URL).mock(return_value=Response(200, json=OPEN_METEO_SINGLE))
    start = datetime(2026, 8, 7, 20, 0, tzinfo=UTC)
    ride_time = [
        RideTimePoint(dist_m=0.0, time_s=0.0),
        RideTimePoint(dist_m=556.0, time_s=18_000.0),
    ]

    await weather_along_route(shape_of(SHORT_LINE), start, ride_time=ride_time, duration_s=3600.0)

    sent = mock.calls[0].request.url.params
    assert sent["start_date"] == "2026-08-07"
    assert sent["end_date"] == "2026-08-08"
