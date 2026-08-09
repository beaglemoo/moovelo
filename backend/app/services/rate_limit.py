"""In-process sliding-window limiter for the login endpoint.

Per-process, in-memory state by design: login is the one endpoint cheap
enough to hammer (a password check, no upstream call, no file parsing), and
the prod compose profile (docker-compose.yml) runs one backend process
behind one reverse proxy - there is no multi-worker or multi-instance
topology to coordinate across here. A horizontally-scaled deployment would
need a shared store (Redis, Postgres) instead; this is deliberately not
that.

The table is bounded, and how it is bounded is load-bearing. An earlier
version evicted the least recently active key once the table filled, which
handed an attacker the limiter's own off switch: the key they are being
blocked on is, by construction, the quietest one in the table (their junk
keys are all newer), so one request under a throwaway email evicted their
block and the next request was a fresh guess. Two requests per guess, after
a one-off flood to fill the table - a near-total bypass of a limiter that
still passed every test it shipped with.

The rule that replaces it: **a live entry is never evicted.** Only entries
whose window has fully elapsed can be reclaimed. Nothing an attacker sends
can shorten anyone's block, including their own.

That leaves one case to answer honestly - a table full of entries that are
all still live, i.e. MAX_TRACKED_KEYS distinct keys seen inside one window.
On an install with a handful of riders that is not traffic, it is an attack.
A novel key is refused (429) rather than waved through, because the
alternative is that an attacker fills the table and then brute-forces the
account they actually want with no limiter in front of it at all. The cost
is that while a flood is in progress, riders who are not already tracked
cannot log in; riders who are keep their normal five attempts, and the table
drains WINDOW_S after the flood stops. Availability under attack is the
thing worth losing here - the limiter's whole reason to exist is the other
one.
"""

import logging
import time

logger = logging.getLogger(__name__)

# 5 attempts per 10 minutes. Wide enough that a rider who fat-fingers a
# password a couple of times before checking caps lock never sees a 429;
# tight enough that credential stuffing against one account is throttled to
# a guess every two minutes rather than as fast as the network allows.
WINDOW_S = 600.0
MAX_ATTEMPTS = 5

# Distinct (ip, email) keys otherwise accumulate for the life of the process
# under scanning traffic that varies the email per request - lazy same-key
# pruning alone never removes a key nobody retries.
MAX_TRACKED_KEYS = 10_000

# Reclaiming expired entries walks the whole table, so it is throttled: under
# a flood every request arrives with a novel key and would otherwise pay for
# a full scan that the previous request already proved finds nothing.
SWEEP_INTERVAL_S = 1.0

_attempts: dict[str, list[float]] = {}
_last_sweep = float("-inf")
_last_saturation_log = float("-inf")


def reset() -> None:
    """Drop all limiter state. For tests; nothing in the app calls this."""
    global _last_sweep, _last_saturation_log
    _attempts.clear()
    _last_sweep = float("-inf")
    _last_saturation_log = float("-inf")


def _prune(key: str, now: float) -> list[float]:
    """This key's attempts still inside the window.

    An empty result means the key is no longer tracked at all - it is popped
    here - which is what `check_and_record` reads to tell "known rider" from
    "needs a slot".
    """
    timestamps = [t for t in _attempts.get(key, ()) if now - t < WINDOW_S]
    if timestamps:
        _attempts[key] = timestamps
    else:
        _attempts.pop(key, None)
    return timestamps


def _sweep_expired(now: float) -> None:
    """Reclaim entries whose window has fully elapsed. Live entries stay."""
    global _last_sweep
    if now - _last_sweep < SWEEP_INTERVAL_S:
        return
    _last_sweep = now
    for key in [k for k, ts in _attempts.items() if now - max(ts) >= WINDOW_S]:
        del _attempts[key]


def _log_saturation(now: float) -> None:
    global _last_saturation_log
    if now - _last_saturation_log < WINDOW_S:
        return
    _last_saturation_log = now
    logger.warning(
        "Login rate limiter is tracking %d distinct keys, all within the last "
        "%.0fs - refusing logins from keys it has no room for. This is what a "
        "credential-stuffing flood looks like.",
        len(_attempts),
        WINDOW_S,
    )


def check_and_record(key: str) -> bool:
    """Record an attempt for `key` and report whether it is allowed.

    A rejected attempt is deliberately NOT recorded. Counting blocked
    attempts too would let a script that keeps hammering a blocked key push
    the window's expiry out indefinitely, locking out anyone who shares that
    key (e.g. the real user, behind the same proxy IP) for as long as the
    attacker keeps trying. Only the last `MAX_ATTEMPTS` *allowed* attempts
    count, so the block always clears exactly WINDOW_S after the last one
    that was actually let through - never later, no matter how much traffic
    arrives while blocked.

    Capacity is only consulted for a key that is not already tracked. A rider
    mid-window keeps their slot regardless of how full the table is, so a
    flood cannot evict them and cannot deny them their remaining attempts.
    """
    now = time.monotonic()
    timestamps = _prune(key, now)
    if len(timestamps) >= MAX_ATTEMPTS:
        return False
    if not timestamps and len(_attempts) >= MAX_TRACKED_KEYS:
        _sweep_expired(now)
        if len(_attempts) >= MAX_TRACKED_KEYS:
            _log_saturation(now)
            return False
    timestamps.append(now)
    _attempts[key] = timestamps
    return True
