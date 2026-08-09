"""In-process sliding-window limiter for the login endpoint.

Per-process, in-memory state by design: login is the one endpoint cheap
enough to hammer (a password check, no upstream call, no file parsing), and
the prod compose profile (docker-compose.yml) runs one backend process
behind one reverse proxy - there is no multi-worker or multi-instance
topology to coordinate across here. A horizontally-scaled deployment would
need a shared store (Redis, Postgres) instead; this is deliberately not
that.
"""

import time

# 5 attempts per 10 minutes. Wide enough that a rider who fat-fingers a
# password a couple of times before checking caps lock never sees a 429;
# tight enough that credential stuffing against one account is throttled to
# a guess every two minutes rather than as fast as the network allows.
WINDOW_S = 600.0
MAX_ATTEMPTS = 5

# Distinct (ip, email) keys otherwise accumulate for the life of the process
# under scanning traffic that varies the email per request - lazy same-key
# pruning alone never removes a key nobody retries. Same backstop pattern as
# weather.py's _cache_set.
MAX_TRACKED_KEYS = 10_000

_attempts: dict[str, list[float]] = {}


def _prune(key: str, now: float) -> list[float]:
    timestamps = [t for t in _attempts.get(key, ()) if now - t < WINDOW_S]
    if timestamps:
        _attempts[key] = timestamps
    else:
        _attempts.pop(key, None)
    return timestamps


def _evict_if_full(now: float) -> None:
    if len(_attempts) < MAX_TRACKED_KEYS:
        return
    expired = [k for k, ts in _attempts.items() if now - max(ts) >= WINDOW_S]
    for k in expired:
        del _attempts[k]
    if len(_attempts) >= MAX_TRACKED_KEYS:
        # Still full after pruning expired keys: drop whichever key has been
        # quiet the longest rather than refusing to track a new one.
        oldest = min(_attempts, key=lambda k: max(_attempts[k]))
        del _attempts[oldest]


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
    """
    now = time.monotonic()
    timestamps = _prune(key, now)
    if len(timestamps) >= MAX_ATTEMPTS:
        return False
    _evict_if_full(now)
    timestamps.append(now)
    _attempts[key] = timestamps
    return True
