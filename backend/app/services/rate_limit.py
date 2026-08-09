"""In-process sliding-window limiters.

Per-process, in-memory state by design: the things limited here are cheap to
hammer and the prod compose profile (docker-compose.yml) runs one backend
process behind one reverse proxy - there is no multi-worker or multi-instance
topology to coordinate across. A horizontally-scaled deployment would need a
shared store (Redis, Postgres) instead; this is deliberately not that.

The table is bounded, and how it is bounded is load-bearing. An earlier
version evicted the least recently active key once the table filled, which
handed an attacker the limiter's own off switch: the key they are being
blocked on is, by construction, the quietest one in the table (their junk
keys are all newer), so one request under a throwaway email evicted their
block and the next request was a fresh guess. Two requests per guess, after
a one-off flood to fill the table - a near-total bypass of a limiter that
still passed every test it shipped with.

The rule that replaces it: **a live entry is never evicted.** Only entries
whose window has fully elapsed can be reclaimed. Nothing a caller sends can
shorten anyone's block, including their own.

That leaves one case to answer honestly - a table full of entries that are
all still live. On an install with a handful of riders that is not traffic,
it is an attack. A novel key is refused rather than waved through, because
the alternative is that an attacker fills the table and then hammers the
thing they actually want with no limiter in front of it. The cost is that
while a flood is in progress, callers who are not already tracked are turned
away; tracked ones keep their normal allowance, and the table drains one
window after the flood stops.
"""

import logging
import time

logger = logging.getLogger(__name__)

# --- login -----------------------------------------------------------------

# 5 attempts per 10 minutes. Wide enough that a rider who fat-fingers a
# password a couple of times before checking caps lock never sees a 429;
# tight enough that credential stuffing against one account is throttled to
# a guess every two minutes rather than as fast as the network allows.
WINDOW_S = 600.0
MAX_ATTEMPTS = 5

# --- assistant -------------------------------------------------------------

# Every assistant turn costs the operator real money at whatever endpoint
# they configured, and one turn is up to MAX_COMPLETION_CALLS completions.
# 30 an hour is far more than anyone plans routes at and still bounds what a
# single account can spend - including an account being used as a free
# general-purpose chatbot, which the system prompt discourages but cannot
# prevent.
ASSISTANT_WINDOW_S = 3600.0
ASSISTANT_MAX_TURNS = 30

# Distinct keys otherwise accumulate for the life of the process under
# traffic that varies the key per request - lazy same-key pruning alone never
# removes a key nobody retries.
MAX_TRACKED_KEYS = 10_000

# Reclaiming expired entries walks the whole table, so it is throttled: under
# a flood every request arrives with a novel key and would otherwise pay for
# a full scan that the previous request already proved finds nothing.
SWEEP_INTERVAL_S = 1.0


class SlidingWindowLimiter:
    """One bounded sliding window over string keys."""

    def __init__(
        self,
        window_s: float,
        max_events: int,
        label: str,
        max_tracked_keys: int = MAX_TRACKED_KEYS,
    ) -> None:
        self.window_s = window_s
        self.max_events = max_events
        self.label = label
        self.max_tracked_keys = max_tracked_keys
        self._attempts: dict[str, list[float]] = {}
        self._last_sweep = float("-inf")
        self._last_saturation_log = float("-inf")

    def reset(self) -> None:
        """Drop all state. For tests; nothing in the app calls this."""
        self._attempts.clear()
        self._last_sweep = float("-inf")
        self._last_saturation_log = float("-inf")

    def _prune(self, key: str, now: float) -> list[float]:
        """This key's events still inside the window.

        An empty result means the key is no longer tracked at all - it is
        popped here - which is what `check_and_record` reads to tell a known
        caller from one that needs a slot.
        """
        timestamps = [t for t in self._attempts.get(key, ()) if now - t < self.window_s]
        if timestamps:
            self._attempts[key] = timestamps
        else:
            self._attempts.pop(key, None)
        return timestamps

    def _sweep_expired(self, now: float) -> None:
        """Reclaim entries whose window has fully elapsed. Live entries stay."""
        if now - self._last_sweep < SWEEP_INTERVAL_S:
            return
        self._last_sweep = now
        for key in [k for k, ts in self._attempts.items() if now - max(ts) >= self.window_s]:
            del self._attempts[key]

    def _log_saturation(self, now: float) -> None:
        if now - self._last_saturation_log < self.window_s:
            return
        self._last_saturation_log = now
        logger.warning(
            "%s rate limiter is tracking %d distinct keys, all within the last "
            "%.0fs - refusing callers it has no room for.",
            self.label,
            len(self._attempts),
            self.window_s,
        )

    def refund(self, key: str) -> None:
        """Give back the most recent event for `key`.

        For work that was charged for up front and then never happened - an
        endpoint that turned out to be unreachable, say. Only ever removes an
        event this caller just added, so it cannot be used to clear someone
        else's window.
        """
        timestamps = self._attempts.get(key)
        if not timestamps:
            return
        timestamps.pop()
        if not timestamps:
            self._attempts.pop(key, None)

    def check_and_record(self, key: str) -> bool:
        """Record an event for `key` and report whether it is allowed.

        A rejected event is deliberately NOT recorded. Counting blocked
        attempts too would let a script that keeps hammering a blocked key
        push the window's expiry out indefinitely, locking out anyone who
        shares that key for as long as the attacker keeps trying. Only the
        last `max_events` *allowed* events count, so the block always clears
        exactly one window after the last one actually let through.

        Capacity is only consulted for a key that is not already tracked. A
        caller mid-window keeps their slot regardless of how full the table
        is, so a flood cannot evict them or deny them their remaining
        allowance.
        """
        now = time.monotonic()
        timestamps = self._prune(key, now)
        if len(timestamps) >= self.max_events:
            return False
        if not timestamps and len(self._attempts) >= self.max_tracked_keys:
            self._sweep_expired(now)
            if len(self._attempts) >= self.max_tracked_keys:
                self._log_saturation(now)
                return False
        timestamps.append(now)
        self._attempts[key] = timestamps
        return True


login = SlidingWindowLimiter(WINDOW_S, MAX_ATTEMPTS, "Login")
assistant = SlidingWindowLimiter(ASSISTANT_WINDOW_S, ASSISTANT_MAX_TURNS, "Assistant")


def check_and_record(key: str) -> bool:
    """The login limiter, kept as a module function for its existing callers."""
    return login.check_and_record(key)


def reset() -> None:
    """Reset every limiter. For tests."""
    login.reset()
    assistant.reset()
