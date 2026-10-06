"""The only module that touches the TypeSafe SDK.

The SDK already does retries/backoff (RetryPolicy), HTTP and response validation. This adds what
a service needs on top: a circuit breaker, a global concurrency bulkhead, and error mapping.
Exceptions are classified by class name so this module (and the tests) import without the SDK.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Protocol

from .resilience import CircuitBreaker

log = logging.getLogger("incident_eval")


class JevError(Exception):
    reason = "error"


class JevUnavailable(JevError):
    """Timeouts, 5xx, 429, connection errors, or an open circuit."""
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class JevRejected(JevError):
    """4xx: our request or credentials are wrong. Alert on this; retrying will not help."""
    reason = "rejected"


class JevBadResponse(JevError):
    """HTTP success but the body failed SDK validation."""
    reason = "bad_response"


_TRANSIENT = {"TypeSafeRateLimitError", "TypeSafeInternalServerError", "TypeSafeAPIConnectionError"}
_REJECTED = {"TypeSafeBadRequestError", "TypeSafeAuthenticationError", "TypeSafePermissionDeniedError",
             "TypeSafeNotFoundError", "TypeSafeUnprocessableEntityError"}
_BAD_RESPONSE = {"TypeSafeAPIResponseValidationError"}


def classify(exc: BaseException) -> str | None:
    names = {c.__name__ for c in type(exc).__mro__}  # APITimeoutError subclasses APIConnectionError
    if names & _BAD_RESPONSE:
        return "bad_response"
    if names & _REJECTED:
        return "rejected"
    if names & _TRANSIENT:
        return "transient"
    if "TypeSafeError" in names:  # any other SDK error (e.g. an unexpected 4xx status)
        return "rejected"
    return None  # not an SDK error: a bug, let it surface


class Metrics(Protocol):
    def jev(self, outcome: str, seconds: float) -> None: ...


class JevGateway:
    def __init__(self, client: Any, *, breaker: CircuitBreaker, max_concurrency: int,
                 metrics: Metrics | None = None) -> None:
        self._client, self._breaker, self._metrics = client, breaker, metrics
        self._sem = asyncio.Semaphore(max_concurrency)

    def _record(self, outcome: str, seconds: float = 0.0) -> None:
        if self._metrics:
            self._metrics.jev(outcome, seconds)

    async def ask(self, state: dict, questions: dict) -> Any:
        if not self._breaker.allow():
            self._record("circuit_open")
            raise JevUnavailable("circuit_open")
        async with self._sem:
            t0 = time.perf_counter()
            try:
                response = await self._client.system_one(state, questions)
            except asyncio.CancelledError:
                self._breaker.release_probe()
                raise
            except Exception as exc:
                kind = classify(exc)
                dt = time.perf_counter() - t0
                if kind == "transient":
                    self._breaker.record_failure()
                    self._record("unavailable", dt)
                    raise JevUnavailable("transient") from exc
                self._breaker.release_probe()
                if kind == "bad_response":
                    self._record("bad_response", dt)
                    raise JevBadResponse(str(exc)) from exc
                if kind == "rejected":
                    self._record("rejected", dt)
                    log.error("jev rejected request (check API key / question shape): %s", exc)
                    raise JevRejected(str(exc)) from exc
                raise
            self._breaker.record_success()
            self._record("ok", time.perf_counter() - t0)
            return response


def build_client(settings) -> Any:
    """AsyncTypeSafeClient reads TYPESAFE_API_KEY itself. Use `async with` around it."""
    from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy  # lazy: tests run without the SDK

    kwargs: dict[str, Any] = {"retry": RetryPolicy(max_retries=settings.max_retries,
                                                   backoff_max=settings.backoff_max_s,
                                                   timeout=settings.timeout_s)}
    if settings.model:
        kwargs["model"] = settings.model
    return AsyncTypeSafeClient(**kwargs)
