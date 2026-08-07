"""Durable, idempotent pause/resume control shared by all runtimes."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ExecutionControlRequest, ExecutionControlState, KillSwitchEvent


class ExecutionControlConflict(RuntimeError):
    code = "execution_control_conflict"

    def __init__(self, message: str, *, request_id: str) -> None:
        super().__init__(message)
        self.request_id = request_id

    def as_dict(self) -> dict[str, object]:
        return {"type": self.code, "message": str(self), "request_id": self.request_id}


class IdempotencyConflict(ExecutionControlConflict):
    code = "idempotency_conflict"


class RevisionConflict(ExecutionControlConflict):
    code = "revision_conflict"


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
            try:
                async with self.session.begin_nested():
                    self.session.add(
                        ExecutionControlState(
                            id=1,
                            paused=True,
                            revision=0,
                            request_id="bootstrap",
                            actor="system",
                            reason="startup safety default",
                            updated_at=datetime.now(UTC),
                        )
                    )
                    await self.session.flush()
            except IntegrityError:
                pass
            row = await self.session.get(ExecutionControlState, 1)
            assert row is not None
        return row

    @staticmethod
    def _snapshot_from_request(row: ExecutionControlRequest) -> ControlSnapshot:
        return ControlSnapshot(
            paused=row.result_paused,
            revision=row.result_revision,
            request_id=row.request_id,
            actor=row.result_actor,
            reason=row.result_reason,
            updated_at=row.result_updated_at,
        )

    @staticmethod
    def _matches(
        row: ExecutionControlRequest,
        *,
        paused: bool,
        actor: str,
        operation: str,
        reason: str,
        expected_revision: int | None,
    ) -> bool:
        return (
            row.target_paused == paused
            and row.actor == actor
            and row.operation == operation
            and row.reason == reason
            and row.expected_revision == expected_revision
        )

    async def _retry(
        self,
        request_id: str,
        *,
        paused: bool,
        actor: str,
        operation: str,
        reason: str,
        expected_revision: int | None,
    ) -> ControlSnapshot | None:
        row = await self.session.get(ExecutionControlRequest, request_id)
        if row is None:
            return None
        if not self._matches(
            row,
            paused=paused,
            actor=actor,
            operation=operation,
            reason=reason,
            expected_revision=expected_revision,
        ):
            raise IdempotencyConflict(
                "request ID is already bound to a different execution control request",
                request_id=request_id,
            )
        return self._snapshot_from_request(row)

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
        actor = actor.strip()
        request_id = request_id.strip() if request_id and request_id.strip() else str(uuid4())
        operation = "pause" if paused else "resume"
        retry = await self._retry(
            request_id,
            paused=paused,
            actor=actor,
            operation=operation,
            reason=reason,
            expected_revision=expected_revision,
        )
        if retry is not None:
            return retry
        row = await self._row()
        retry = await self._retry(
            request_id,
            paused=paused,
            actor=actor,
            operation=operation,
            reason=reason,
            expected_revision=expected_revision,
        )
        if retry is not None:
            return retry
        if expected_revision is not None and expected_revision != row.revision:
            raise RevisionConflict("execution control revision conflict", request_id=request_id)
        previous = row.paused
        next_revision = row.revision + 1
        updated_at = datetime.now(UTC)
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
                updated_at=updated_at,
            )
        )
        if getattr(result, "rowcount", 0) != 1:
            await self.session.rollback()
            retry = await self._retry(
                request_id,
                paused=paused,
                actor=actor,
                operation=operation,
                reason=reason,
                expected_revision=expected_revision,
            )
            if retry is not None:
                return retry
            raise RevisionConflict("execution control revision conflict", request_id=request_id)
        self.session.add(
            KillSwitchEvent(
                active=paused,
                actor=actor,
                reason=reason,
                triggered_at=updated_at,
                metadata_json={"previous_paused": previous, "new_paused": paused},
                request_id=request_id,
                revision=next_revision,
            )
        )
        self.session.add(
            ExecutionControlRequest(
                request_id=request_id,
                target_paused=paused,
                actor=actor,
                operation=operation,
                reason=reason,
                expected_revision=expected_revision,
                result_paused=paused,
                result_revision=next_revision,
                result_actor=actor,
                result_reason=reason,
                result_updated_at=updated_at,
            )
        )
        await self.session.flush()
        return ControlSnapshot(
            paused=paused,
            revision=next_revision,
            request_id=request_id,
            actor=actor,
            reason=reason,
            updated_at=updated_at,
        )

    async def assert_entries_allowed(self) -> None:
        snapshot = await self.snapshot()
        if snapshot.paused:
            raise RuntimeError(f"new entries paused: {snapshot.reason}")
