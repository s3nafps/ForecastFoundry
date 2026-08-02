import asyncio
import time
from collections.abc import Awaitable, Callable
from email.utils import parsedate_to_datetime
from typing import Any

import httpx


class ProviderUnavailable(RuntimeError):
    pass


class ProviderResponseError(RuntimeError):
    pass


class CircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int,
        reset_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._reset_seconds = reset_seconds
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._lock = asyncio.Lock()

    async def before_request(self) -> None:
        async with self._lock:
            if self._opened_at is None:
                return
            if self._clock() - self._opened_at >= self._reset_seconds:
                self._opened_at = None
                self._failures = 0
                return
            raise ProviderUnavailable("provider circuit is open")

    async def record_success(self) -> None:
        async with self._lock:
            self._failures = 0
            self._opened_at = None

    async def record_failure(self) -> None:
        async with self._lock:
            self._failures += 1
            if self._failures >= self._failure_threshold:
                self._opened_at = self._clock()


class ResilientHttpClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        max_retries: int = 3,
        min_interval_seconds: float = 0.0,
        circuit_breaker: CircuitBreaker | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._max_retries = max_retries
        self._min_interval_seconds = min_interval_seconds
        self._circuit = circuit_breaker or CircuitBreaker(
            failure_threshold=5, reset_seconds=300, clock=clock
        )
        self._sleep = sleep
        self._clock = clock
        self._next_request_at = 0.0
        self._rate_lock = asyncio.Lock()

    async def _pace(self) -> None:
        async with self._rate_lock:
            delay = self._next_request_at - self._clock()
            if delay > 0:
                await self._sleep(delay)
            self._next_request_at = self._clock() + self._min_interval_seconds

    async def request_json(self, method: str, url: str, **kwargs: Any) -> object:
        await self._circuit.before_request()
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            await self._pace()
            try:
                response = await self._client.request(method, url, **kwargs)
                if response.status_code == 429 or response.status_code >= 500:
                    raise ProviderUnavailable(f"provider returned HTTP {response.status_code}")
                response.raise_for_status()
                payload: object = response.json()
            except (httpx.TimeoutException, httpx.NetworkError, ProviderUnavailable) as exc:
                last_error = exc
                if attempt < self._max_retries:
                    await self._sleep(self._retry_delay(attempt, locals().get("response")))
                    continue
                await self._circuit.record_failure()
                raise ProviderUnavailable("provider request failed after retries") from exc
            except (httpx.HTTPStatusError, ValueError) as exc:
                await self._circuit.record_failure()
                raise ProviderResponseError(
                    "provider returned a permanent invalid response"
                ) from exc
            await self._circuit.record_success()
            return payload
        raise ProviderUnavailable("provider request failed") from last_error

    @staticmethod
    def _retry_delay(attempt: int, response: object) -> float:
        if isinstance(response, httpx.Response) and (
            retry_after := response.headers.get("Retry-After")
        ):
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                try:
                    retry_at = float(parsedate_to_datetime(retry_after).timestamp())
                    return max(0.0, retry_at - time.time())
                except (TypeError, ValueError):
                    pass
        return float(min(8.0, 0.25 * (2**attempt)))
