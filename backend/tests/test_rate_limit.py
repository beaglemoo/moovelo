"""Unit tests for the login limiter (app/services/rate_limit.py).

Pure in-process logic, no DB - the endpoint-level 429/recovery behaviour is
covered separately in test_auth_routes.py.
"""

from collections.abc import Iterator

import pytest

from app.services import rate_limit


@pytest.fixture(autouse=True)
def clear_state() -> Iterator[None]:
    rate_limit._attempts.clear()
    yield
    rate_limit._attempts.clear()


def set_now(monkeypatch: pytest.MonkeyPatch, value: float) -> None:
    monkeypatch.setattr(rate_limit.time, "monotonic", lambda: value)


def test_allows_up_to_the_threshold_then_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    set_now(monkeypatch, 0.0)
    for _ in range(rate_limit.MAX_ATTEMPTS):
        assert rate_limit.check_and_record("1.2.3.4|rider@example.com") is True
    assert rate_limit.check_and_record("1.2.3.4|rider@example.com") is False


def test_recovers_once_the_window_elapses(monkeypatch: pytest.MonkeyPatch) -> None:
    set_now(monkeypatch, 0.0)
    for _ in range(rate_limit.MAX_ATTEMPTS):
        rate_limit.check_and_record("1.2.3.4|rider@example.com")
    assert rate_limit.check_and_record("1.2.3.4|rider@example.com") is False

    # Just under the window: still blocked.
    set_now(monkeypatch, rate_limit.WINDOW_S - 1)
    assert rate_limit.check_and_record("1.2.3.4|rider@example.com") is False

    # Past the window from the last *allowed* attempt: open again.
    set_now(monkeypatch, rate_limit.WINDOW_S + 1)
    assert rate_limit.check_and_record("1.2.3.4|rider@example.com") is True


def test_blocked_retries_do_not_extend_the_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rejected attempt is not recorded, so hammering a blocked key cannot
    keep pushing its expiry out and lock out whoever else shares the key
    (e.g. a real user behind the same proxy IP) for longer than WINDOW_S
    after the last attempt that actually went through."""
    set_now(monkeypatch, 0.0)
    for _ in range(rate_limit.MAX_ATTEMPTS):
        rate_limit.check_and_record("1.2.3.4|rider@example.com")

    # Keep hammering the blocked key well past the original window.
    for t in range(1, 50):
        set_now(monkeypatch, float(t))
        assert rate_limit.check_and_record("1.2.3.4|rider@example.com") is False

    # Still exactly WINDOW_S after the *last allowed* attempt (t=0), not
    # after the most recent rejected one (t=49).
    set_now(monkeypatch, rate_limit.WINDOW_S + 1)
    assert rate_limit.check_and_record("1.2.3.4|rider@example.com") is True


def test_different_keys_are_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    set_now(monkeypatch, 0.0)
    for _ in range(rate_limit.MAX_ATTEMPTS):
        rate_limit.check_and_record("1.2.3.4|rider@example.com")
    assert rate_limit.check_and_record("1.2.3.4|rider@example.com") is False
    # A different IP or a different email is a different bucket entirely.
    assert rate_limit.check_and_record("5.6.7.8|rider@example.com") is True
    assert rate_limit.check_and_record("1.2.3.4|other@example.com") is True


def test_prunes_and_stays_bounded_under_many_distinct_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_now(monkeypatch, 0.0)
    for i in range(rate_limit.MAX_TRACKED_KEYS + 500):
        rate_limit.check_and_record(f"10.0.0.{i % 256}|user{i}@example.com")
    assert len(rate_limit._attempts) <= rate_limit.MAX_TRACKED_KEYS

    # Long after every one of those entries has aged out, a fresh key still
    # gets tracked rather than being permanently crowded out.
    set_now(monkeypatch, rate_limit.WINDOW_S * 10)
    assert rate_limit.check_and_record("192.168.1.1|new@example.com") is True
