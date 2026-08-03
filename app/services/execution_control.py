"""Durable, idempotent pause/resume control shared by all runtimes."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ExecutionControlState, KillSwitchEvent


@dataclass(frozen=True)
class ControlSnapshot:
    paused: bool
    revision: int
    request_id: str
    actor: str
    reason: str
    updated_at: datetime

    def as_dict(self) -> dict[str, object]:
        return {
            "paused": self.paused,
            "kill_switch_active": self.paused,
            "revision": self.revision,
            "request_id": self.request_id,
            "actor": self.actor,
            "reason": self.reason,
            "updated_at": self.updated_at.isoformat(),
        }


class ExecutionControl:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _row(self) -> ExecutionControlState:
        row = await self.session.get(ExecutionControlState, 1)
        if row is None:
            row = ExecutionControlState(
                id=1,
                paused=True,
                revision=0,
                request_id="bootstrap",
                actor="system",
                reason="startup safety default",
                updated_at=datetime.now(UTC),
            )
            self.session.add(row)
            await self.session.flush()
        return row

    async def snapshot(self) -> ControlSnapshot:
        row = await self._row()
        return ControlSnapshot(
            paused=row.paused,
            revision=row.revision,
            request_id=row.request_id,
            actor=row.actor,
            reason=row.reason,
            updated_at=row.updated_at,
        )

    async def set_paused(
        self,
        paused: bool,
        *,
        actor: str,
        reason: str,
        request_id: str | None = None,
        expected_revision: int | None = None,
    ) -> ControlSnapshot:
        reason = reason.strip()
        if not reason:
            raise ValueError("control transition reason is required")
        request_id = request_id or str(uuid4())
        row = await self._row()
        if row.request_id == request_id:
            return await self.snapshot()
        if expected_revision is not None and expected_revision != row.revision:
            raise RuntimeError("execution control revision conflict")
        previous = row.paused
        next_revision = row.revision + 1
        result = await self.session.execute(
            update(ExecutionControlState)
            .where(
                ExecutionControlState.id == 1,
                ExecutionControlState.revision == row.revision,
            )
            .values(
                paused=paused,
                revision=next_revision,
                request_id=request_id,
                actor=actor,
                reason=reason,
                updated_at=datetime.now(UTC),
            )
        )
        if getattr(result, "rowcount", 0) != 1:
            raise RuntimeError("execution control revision conflict")
        self.session.add(
            KillSwitchEvent(
                active=paused,
                actor=actor,
                reason=reason,
                triggered_at=datetime.now(UTC),
                metadata_json={"previous_paused": previous, "new_paused": paused},
                request_id=request_id,
                revision=next_revision,
            )
        )
        await self.session.flush()
        return await self.snapshot()

    async def assert_entries_allowed(self) -> None:
        snapshot = await self.snapshot()
        if snapshot.paused:
            raise RuntimeError(f"new entries paused: {snapshot.reason}")
