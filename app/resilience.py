"""Circuit breaker and single-flight TTL cache. Stdlib only."""
from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable


class CircuitBreaker:
    """closed -> open after N consecutive failures -> half-open after cooldown (one probe)."""

    def __init__(self, failure_threshold: int, cooldown_s: float,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._threshold, self._cooldown, self._clock = failure_threshold, cooldown_s, clock
        self._failures = 0
        self._opened_at: float | None = None
        self._probing = False

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        return "half_open" if self._clock() - self._opened_at >= self._cooldown else "open"

    def allow(self) -> bool:
        if self._opened_at is None:
            return True
        if self._clock() - self._opened_at >= self._cooldown and not self._probing:
            self._probing = True  # exactly one probe
            return True
        return False

    def record_success(self) -> None:
        self._failures, self._opened_at, self._probing = 0, None, False

    def record_failure(self) -> None:
        self._probing = False
        self._failures += 1
        if self._failures >= self._threshold:
            self._opened_at = self._clock()

    def release_probe(self) -> None:
        """A non-health outcome (4xx, malformed body) must not leave the breaker stuck half-open."""
        self._probing = False


class SingleFlightCache:
    """TTL cache; concurrent identical requests share one computation.

    compute() returns (value, cacheable). In-process only: use Redis if you run several replicas.
    """

    def __init__(self, ttl_s: float, max_items: int,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl, self._max, self._clock = ttl_s, max_items, clock
        self._data: dict[str, tuple[float, Any]] = {}
        self._inflight: dict[str, asyncio.Future] = {}

    async def get_or_compute(self, key: str,
                             compute: Callable[[], Awaitable[tuple[Any, bool]]]) -> tuple[Any, bool]:
        entry = self._data.get(key)
        if entry and entry[0] > self._clock():
            return entry[1], True
        pending = self._inflight.get(key)
        if pending is not None:
            return await pending, True

        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        try:
            value, cacheable = await compute()
        except asyncio.CancelledError:
            fut.cancel()
            raise
        except Exception as exc:
            fut.set_exception(exc)
            fut.exception()  # mark retrieved so asyncio does not warn when nobody waits
            raise
        else:
            fut.set_result(value)
            if cacheable:
                self._data[key] = (self._clock() + self._ttl, value)
                while len(self._data) > self._max:
                    self._data.pop(next(iter(self._data)))
            return value, False
        finally:
            self._inflight.pop(key, None)
