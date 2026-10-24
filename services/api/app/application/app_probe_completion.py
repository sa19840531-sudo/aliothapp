"""App Probe 成功后的 Manifest 与 Evidence Supplement 挂接。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from services.api.app.application.app_exploration_turn import AppExplorationTurnOutcome
from services.api.app.application.evidence_manifests import EvidenceManifestRepository
from services.api.app.application.evidence_request_consumption import (
    EvidenceRequestConsumptionError,
    EvidenceRequestConsumptionService,
    ProbeCompletionProof,
)
from services.api.app.application.evidence_requests import EvidenceRequestRepository
from services.api.app.application.evidence_supplements import (
    AttachEvidenceSupplementCommand,
    EvidenceSupplementAttacher,
)
from services.api.app.application.exploration_work_queue import ExplorerWorkStatus
from services.api.app.application.supplement_input_set_reseal import (
    SupplementInputSetResealService,
)
from services.api.app.domain.inspection import EvidenceSourceGrade


class AppProbeCompletionError(RuntimeError):
    """Probe 后置证明不完整或权威状态不允许挂接。"""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class EvidenceSupplementAttacherPort(Protocol):
    async def attach(self, command: AttachEvidenceSupplementCommand) -> object: ...


class AppProbeManifestFinalizerPort(Protocol):
    async def finalize(self, manifest: object) -> object: ...


class AppProbeCompletionRecoveryReader(Protocol):
    """读取成功但尚未挂接 Supplement 的 App Probe。"""

    async def find_next(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        finalization_epoch: int,
    ) -> AppExplorationTurnOutcome | None: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class AppProbeCompletionResult:
    """一次幂等挂接结果。"""

    request_id: str
    manifest_id: str
    attached: bool
    evidence_input_set_id: str | None = None


class AppProbeCompletionService:
    """使用 Probe 原始证明引用完成补采挂接，不再执行恢复动作。"""

    def __init__(
        self,
        *,
        requests: EvidenceRequestRepository,
        manifests: EvidenceManifestRepository,
        attacher: EvidenceSupplementAttacherPort | EvidenceSupplementAttacher,
        recovery: AppProbeCompletionRecoveryReader | None = None,
        manifest_finalizer: AppProbeManifestFinalizerPort | None = None,
        input_set_resealer: SupplementInputSetResealService | None = None,
    ) -> None:
        self._requests = requests
        self._manifests = manifests
        self._attacher = attacher
        self._recovery = recovery
        self._manifest_finalizer = manifest_finalizer
        # 挂接与状态流转三端同源，App 侧只负责把 Probe 结果整理成平台无关的证明。
        self._consumption = EvidenceRequestConsumptionService(
            requests=requests,
            manifests=manifests,
            attacher=attacher,
            manifest_finalizer=manifest_finalizer,
            input_set_resealer=input_set_resealer,
        )

    async def recover_next(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        finalization_epoch: int,
    ) -> AppProbeCompletionResult | None:
        """幂等补挂已成功 Probe，覆盖 Work 成功后进程中断。"""

        task = _require_text(task_id, "task_id")
        run_id = _require_text(agent_run_id, "agent_run_id")
        if (
            isinstance(finalization_epoch, bool)
            or not isinstance(finalization_epoch, int)
            or finalization_epoch < 0
        ):
            raise ValueError("finalization_epoch 必须为非负整数")
        if self._recovery is None:
            return None
        outcome = await self._recovery.find_next(
            task_id=task,
            agent_run_id=run_id,
            finalization_epoch=finalization_epoch,
        )
        if outcome is None:
            return None
        return await self.complete(task_id=task, outcome=outcome)

    async def complete(
        self,
        *,
        task_id: str,
        outcome: AppExplorationTurnOutcome,
    ) -> AppProbeCompletionResult:
        task = _require_text(task_id, "task_id")
        proof = probe_completion_proof_from_turn(outcome)
        try:
            attachment = await self._consumption.attach_supplement(task_id=task, proof=proof)
        except EvidenceRequestConsumptionError as exc:
            raise AppProbeCompletionError(str(exc), retryable=exc.retryable) from exc
        return AppProbeCompletionResult(
            request_id=attachment.request_id,
            manifest_id=attachment.manifest_id,
            attached=attachment.attached,
            evidence_input_set_id=attachment.evidence_input_set_id,
        )


def probe_completion_proof_from_turn(
    outcome: AppExplorationTurnOutcome,
) -> ProbeCompletionProof:
    """把 App Probe 的执行结果转成三端共用的补采证明。

    App 特有的前置判断（认领过、是 probe work、已成功）留在这里；证明本身的字段
    完整性由 :class:`ProbeCompletionProof` 统一把关。
    """

    if not isinstance(outcome, AppExplorationTurnOutcome):
        raise TypeError("outcome 必须为 AppExplorationTurnOutcome")
    if (
        not outcome.claimed
        or outcome.work_type != "probe"
        or outcome.final_status is not ExplorerWorkStatus.SUCCEEDED
    ):
        raise AppProbeCompletionError("只有成功 Probe Work 可以挂接补采证据", retryable=False)
    values = {
        "work_id": outcome.work_id,
        "result_capture_id": outcome.result_capture_id,
        "request_id": outcome.evidence_request_id,
        "restoration_attempt_id": outcome.restoration_attempt_id,
        "equivalence_assessment_id": outcome.equivalence_assessment_id,
    }
    if any(not isinstance(value, str) or not value.strip() for value in values.values()):
        raise AppProbeCompletionError("Probe 结果缺少完整证明血缘", retryable=False)
    if not isinstance(outcome.source_grade, EvidenceSourceGrade):
        raise AppProbeCompletionError("Probe 结果缺少来源等级证明", retryable=False)
    return ProbeCompletionProof(
        work_id=str(values["work_id"]),
        result_capture_id=str(values["result_capture_id"]),
        request_id=str(values["request_id"]),
        restoration_attempt_id=str(values["restoration_attempt_id"]),
        equivalence_assessment_id=str(values["equivalence_assessment_id"]),
        source_grade=outcome.source_grade,
    )


def _require_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} 必须为无首尾空白的非空字符串")
    return value
