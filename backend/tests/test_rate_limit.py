"""Unit tests for the login limiter (app/services/rate_limit.py).

Pure in-process logic, no DB - the endpoint-level 429/recovery behaviour is
covered separately in test_auth_routes.py.
"""

from collections.abc import Iterator

import pytest

from app.services import rate_limit


@pytest.fixture(autouse=True)
def clear_state() -> Iterator[None]:
    rate_limit.reset()
    yield
    rate_limit.reset()


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
    assert len(rate_limit._attempts) == rate_limit.MAX_TRACKED_KEYS

    # Long after every one of those entries has aged out, a fresh key still
    # gets tracked rather than being permanently crowded out.
    set_now(monkeypatch, rate_limit.WINDOW_S * 10)
    assert rate_limit.check_and_record("192.168.1.1|new@example.com") is True


def _flood(count: int) -> None:
    """`count` attempts, each under a key nobody will ever reuse."""
    for i in range(count):
        rate_limit.check_and_record(f"1.2.3.4|junk{i}@example.com")


def test_flooding_distinct_keys_cannot_clear_an_existing_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bug this limiter shipped with, kept as a test.

    Eviction used to drop the least recently active key once the table
    filled. A blocked key is exactly that - the attacker's junk keys are all
    newer than the block they are trying to escape - so a single throwaway
    request evicted the block and the next request was a fresh guess. Two
    requests per guess, and every test in this file still passed.
    """
    target = "1.2.3.4|victim@example.com"
    set_now(monkeypatch, 0.0)
    for _ in range(rate_limit.MAX_ATTEMPTS):
        assert rate_limit.check_and_record(target) is True
    assert rate_limit.check_and_record(target) is False

    # Fill the table, then keep sending novel keys - the steady state where
    # every request used to cost one eviction.
    _flood(rate_limit.MAX_TRACKED_KEYS + 500)

    # Still blocked, and no amount of further flooding buys a guess.
    assert rate_limit.check_and_record(target) is False
    for i in range(20):
        rate_limit.check_and_record(f"1.2.3.4|fresh{i}@example.com")
        assert rate_limit.check_and_record(target) is False


def test_flooding_cannot_reset_a_partially_used_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Evicting a key that is merely *close* to the threshold is the same
    bypass one step earlier: an attacker who resets at four attempts never
    reaches five. Only expiry may clear a counter."""
    target = "1.2.3.4|victim@example.com"
    set_now(monkeypatch, 0.0)
    for _ in range(rate_limit.MAX_ATTEMPTS - 1):
        assert rate_limit.check_and_record(target) is True

    _flood(rate_limit.MAX_TRACKED_KEYS + 500)

    # The fifth attempt is still the last one, and the sixth still trips.
    assert rate_limit.check_and_record(target) is True
    assert rate_limit.check_and_record(target) is False


def test_a_saturated_table_refuses_novel_keys_but_not_tracked_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cost of never evicting a live entry, asserted rather than assumed.

    A rider already inside the window keeps their slot and their remaining
    attempts; only a rider the table has no room for is turned away, and only
    for as long as the flood lasts.
    """
    rider = "1.2.3.4|rider@example.com"
    set_now(monkeypatch, 0.0)
    assert rate_limit.check_and_record(rider) is True

    _flood(rate_limit.MAX_TRACKED_KEYS + 500)

    # Tracked: unaffected, right up to its own threshold.
    for _ in range(rate_limit.MAX_ATTEMPTS - 1):
        assert rate_limit.check_and_record(rider) is True
    assert rate_limit.check_and_record(rider) is False

    # Untracked during the flood: refused rather than waved through.
    assert rate_limit.check_and_record("1.2.3.4|newcomer@example.com") is False

    # And it drains on its own once the flood stops.
    set_now(monkeypatch, rate_limit.WINDOW_S + 1)
    assert rate_limit.check_and_record("1.2.3.4|newcomer@example.com") is True
