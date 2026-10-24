"""把页面图逐边恢复适配为现有 RestorationExecutor。"""

from __future__ import annotations

from typing import Any, Protocol

from services.api.app.application.evidence_recovery_replay import (
    EvidenceRecoveryReplayError,
    EvidenceRecoveryReplayExecutor,
    EvidenceRecoveryReplayOutcome,
)
from services.api.app.application.restoration import (
    RestorationExecutionError,
    RestorationExecutionRequest,
    RestorationExecutionResult,
)
from services.api.app.domain.inspection import StateInvariantSnapshot
from services.api.app.domain.interaction import SurfaceKind


class RecoverySnapshotReader(Protocol):
    """按恢复结果 Capture 读取完整实际状态不变量。"""

    async def read(
        self,
        *,
        task_id: str,
        session_id: str,
        surface_kind: SurfaceKind,
        capture_id: str,
    ) -> StateInvariantSnapshot: ...


class VerifiedCaptureReader(Protocol):
    """恢复快照适配器读取已校验 Capture 的最小端口。"""

    async def get_verified_capture(self, *, capture_id: str) -> Any | None: ...


class CaptureSnapshotReader(Protocol):
    """现有 App 恢复快照读取器的最小端口。"""

    async def read(self, *, capture: Any) -> StateInvariantSnapshot: ...


class CaptureBackedRecoverySnapshotReader:
    """从持久化层取回新 Capture，再复用完整状态快照读取器。"""

    def __init__(
        self,
        *,
        captures: VerifiedCaptureReader,
        snapshots: CaptureSnapshotReader,
    ) -> None:
        self._captures = captures
        self._snapshots = snapshots

    async def read(
        self,
        *,
        task_id: str,
        session_id: str,
        surface_kind: SurfaceKind,
        capture_id: str,
    ) -> StateInvariantSnapshot:
        persisted = await self._captures.get_verified_capture(capture_id=capture_id)
        capture = getattr(persisted, "capture", None) if persisted is not None else None
        if (
            capture is None
            or getattr(capture, "capture_id", None) != capture_id
            or getattr(capture, "task_id", None) != task_id
            or getattr(capture, "session_id", None) != session_id
            or getattr(capture, "surface_kind", None) is not surface_kind
        ):
            raise RestorationExecutionError(
                error_code="RESTORED_CAPTURE_IDENTITY_MISMATCH",
                message="恢复结果引用的已校验 Capture 不存在或身份不一致",
                unfulfillable=True,
            )
        return await self._snapshots.read(capture=capture)


class EvidenceRecoveryRestorationExecutor:
    """执行页面图恢复，并用新 Capture 的完整快照形成恢复结果。"""

    def __init__(
        self,
        *,
        replay: EvidenceRecoveryReplayExecutor,
        snapshots: RecoverySnapshotReader,
        agent_run_id: str,
        finalization_epoch: int,
    ) -> None:
        if not isinstance(agent_run_id, str) or not agent_run_id.strip():
            raise ValueError("agent_run_id 必须为非空字符串")
        if agent_run_id != agent_run_id.strip():
            raise ValueError("agent_run_id 不能包含首尾空白")
        if (
            isinstance(finalization_epoch, bool)
            or not isinstance(finalization_epoch, int)
            or finalization_epoch < 0
        ):
            raise ValueError("finalization_epoch 必须为非负整数")
        self._replay = replay
        self._snapshots = snapshots
        self._agent_run_id = agent_run_id
        self._finalization_epoch = finalization_epoch

    async def restore(
        self,
        request: RestorationExecutionRequest,
    ) -> RestorationExecutionResult:
        if not isinstance(request, RestorationExecutionRequest):
            raise TypeError("request 必须为 RestorationExecutionRequest")
        try:
            replayed = await self._replay.execute(
                plan=request.plan,
                agent_run_id=self._agent_run_id,
                finalization_epoch=self._finalization_epoch,
            )
        except EvidenceRecoveryReplayError as exc:
            raise RestorationExecutionError(
                error_code=exc.reason_code,
                message=str(exc),
                unfulfillable=exc.reason_code
                in {
                    "RECOVERY_ROUTE_INVALID",
                    "RECOVERY_ROUTE_NOT_APPLICABLE",
                },
            ) from exc
        _require_replay_outcome(replayed, request=request)
        try:
            snapshot = await self._snapshots.read(
                task_id=request.plan.task_id,
                session_id=request.plan.target_session_id,
                surface_kind=request.plan.surface_kind,
                capture_id=replayed.result_capture_id,
            )
        except RestorationExecutionError:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一形成可持久化恢复错误
            raise RestorationExecutionError(
                error_code="RESTORED_CAPTURE_VERIFICATION_FAILED",
                message="恢复后的新 Capture 无法形成完整状态快照",
                unfulfillable=False,
            ) from exc
        _require_snapshot_identity(snapshot, request=request, replayed=replayed)
        return RestorationExecutionResult(
            restored_snapshot=snapshot,
            action_execution_ids=replayed.action_execution_ids,
        )


def _require_replay_outcome(
    replayed: EvidenceRecoveryReplayOutcome,
    *,
    request: RestorationExecutionRequest,
) -> None:
    if not isinstance(replayed, EvidenceRecoveryReplayOutcome) or not replayed.arrived:
        raise RestorationExecutionError(
            error_code="RECOVERY_ROUTE_NOT_ARRIVED",
            message="页面图恢复没有形成可信到达结果",
            unfulfillable=False,
        )
    if replayed.transition_ids != request.plan.normalization_step_ids:
        raise RestorationExecutionError(
            error_code="RECOVERY_ROUTE_RECEIPT_MISMATCH",
            message="页面图恢复回执与计划步骤不一致",
            unfulfillable=True,
        )


def _require_snapshot_identity(
    snapshot: StateInvariantSnapshot,
    *,
    request: RestorationExecutionRequest,
    replayed: EvidenceRecoveryReplayOutcome,
) -> None:
    plan = request.plan
    if (
        not isinstance(snapshot, StateInvariantSnapshot)
        or snapshot.task_id != plan.task_id
        or snapshot.session_id != plan.target_session_id
        or snapshot.surface_kind is not plan.surface_kind
        or snapshot.capture_id != replayed.result_capture_id
    ):
        raise RestorationExecutionError(
            error_code="RESTORED_CAPTURE_IDENTITY_MISMATCH",
            message="恢复状态快照与页面图到达结果身份不一致",
            unfulfillable=True,
        )
