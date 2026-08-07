"""Cached provider probes with explicit keyless/optional-secret states."""

import time
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import ProviderHealthSnapshot
from app.providers.registry import ProviderRegistry, ProviderSecretError
from app.services.http import ProviderUnavailable, ResilientHttpClient


class ProviderHealthMonitor:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        registry: ProviderRegistry,
        http: ResilientHttpClient,
        *,
        cache_seconds: int = 30,
    ) -> None:
        self.sessions = sessions
        self.registry = registry
        self.http = http
        self.cache_seconds = cache_seconds

    async def check(self, provider: str, *, force: bool = False) -> dict[str, object]:
        now = datetime.now(UTC)
        async with self.sessions() as session:
            cached = await session.scalar(
                select(ProviderHealthSnapshot)
                .where(ProviderHealthSnapshot.provider == provider)
                .order_by(ProviderHealthSnapshot.checked_at.desc())
            )
            if (
                cached is not None
                and not force
                and (now - cached.checked_at).total_seconds() <= self.cache_seconds
            ):
                return _snapshot_dict(cached)
            spec = self.registry.get(provider)
            try:
                if spec.auth != "none":
                    self.registry.resolve_secret(provider)
                started = time.perf_counter()
                await self.http.request_json("GET", spec.endpoint, timeout=10)
                latency = int((time.perf_counter() - started) * 1000)
                healthy, status, reason = True, "healthy", "ok"
            except ProviderSecretError:
                latency, healthy, status, reason = None, False, "unavailable", "missing_secret"
            except ProviderUnavailable:
                latency, healthy, status, reason = (
                    None,
                    False,
                    "unavailable",
                    "provider_unavailable",
                )
            except Exception as exc:
                latency, healthy, status, reason = None, False, "error", type(exc).__name__
            snapshot = ProviderHealthSnapshot(
                provider=provider,
                checked_at=now,
                healthy=healthy,
                latency_ms=latency,
                status=status,
                reason=reason,
                metadata_json={"endpoint": spec.endpoint, "classification": spec.classification},
            )
            session.add(snapshot)
            await session.commit()
            return _snapshot_dict(snapshot)

    async def all(self, *, force: bool = False) -> list[dict[str, object]]:
        return [await self.check(spec.name, force=force) for spec in self.registry.specs()]


def _snapshot_dict(snapshot: ProviderHealthSnapshot) -> dict[str, object]:
    return {
        "name": snapshot.provider,
        "status": snapshot.status,
        "healthy": snapshot.healthy,
        "reason": snapshot.reason,
        "latency_ms": snapshot.latency_ms,
        "checked_at": snapshot.checked_at,
        "metadata": snapshot.metadata_json,
    }
