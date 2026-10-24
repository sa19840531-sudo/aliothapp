"""补采请求的跨平台消费与状态流转入口。

把补采回来的证据挂回 Capture → Manifest → Input Set 血缘、按恢复结果结终态，
这段逻辑与执行平台无关：App 走 Probe Work、H5/Web 走各自的恢复运行时，但挂接
前置条件、身份校验与状态流转完全一致。本模块把它收敛成三端共用的入口，平台侧
只需要把自己的执行结果整理成 :class:`ProbeCompletionProof`。

**编排路径（方案 03 §6 标准流程走的就是这条，H5/Web 按它接）**::

    routed --ensure_running--> running --attach_supplement--> satisfied

``attach_supplement`` 成功时，底层写入 Supplement 的同一条语句会把请求原子置为
satisfied（见 ``evidence_supplement_repository``：``WHERE request.status =
'running'`` → ``SET status = 'satisfied'``）。**成功路径不要再调
``settle_terminal``**，那会拿 ``RUNNING → SATISFIED`` 去撞一条已经是 satisfied
的行。``settle_terminal`` 只用于「没能挂上」的收尾：不可满足结 unfulfillable，
不等价或失败保留 running 等重试。

**租约路径（``claim_next`` / ``release_lease`` / ``settle_lease``）**：这三个方法
的 SQL 全程要求 ``status = 'pending'``，租约本身才是「正在被处理」的标记——与方案
第 415 行「动作 owner 是可到期的执行许可，不是永久冻结值」一致。**不要在租约期内
把状态推进到 running**：``transition`` 的 WHERE 明确排斥持有有效租约的行，而一旦
进了 running，``release_lease`` 与 ``settle_lease`` 会因为 status 不再是 pending
而双双失效，补采失败就没法按租约收尾了。

租约路径的成功侧由 ``app_targeted_recapture_consumer`` 走通：``claim_next`` 领 pending
请求 → 解析恢复计划 → ``RestorationCoordinator`` 恢复并过状态等价门禁 →
``RestoredProbePublicationService`` 用真实的 attempt / assessment 身份发布 ProbeWork。
``evidence_request_routing`` 只做受理与选路，不发布 ProbeWork。要给 H5/Web 补的是同一
条路径里的恢复执行端口（就地观察 / 路线重放 → 新 Capture → 状态快照），不是在这里
另开一套领取状态机。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from services.api.app.application.evidence_manifests import EvidenceManifestRepository
from services.api.app.application.evidence_requests import (
    EvidenceRequestClaimResult,
    EvidenceRequestLeaseReleaseResult,
    EvidenceRequestRepository,
    EvidenceRequestStatus,
    ReleaseEvidenceRequestLeaseCommand,
    SettleEvidenceRequestLeaseCommand,
    StoredEvidenceRequest,
    TransitionEvidenceRequestCommand,
)
from services.api.app.application.evidence_supplements import (
    AttachEvidenceSupplementCommand,
    EvidenceSupplementAttacher,
)
from services.api.app.application.supplement_input_set_reseal import (
    SupplementInputSetResealService,
)
from services.api.app.domain.inspection import (
    EvidenceSourceGrade,
    RestorationAttempt,
    RestorationAttemptStatus,
)

logger = logging.getLogger(__name__)

# 只有这两个状态允许挂接补采证据：running 是正常执行中，satisfied 允许幂等重挂。
_ATTACHABLE_STATUSES = frozenset({EvidenceRequestStatus.RUNNING, EvidenceRequestStatus.SATISFIED})


class EvidenceRequestConsumptionError(RuntimeError):
    """补采证明不完整，或请求的权威状态不允许本次操作。"""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True, slots=True, kw_only=True)
class ProbeCompletionProof:
    """一次成功补采的证明血缘，与执行平台无关。

    App 侧由 ``AppExplorationTurnOutcome`` 转换而来（见
    ``app_probe_completion.probe_completion_proof_from_turn``）；H5/Web 侧在自己的
    恢复运行时里直接构造。六个字段都要求非空，缺任一项都不能挂接——补采证据必须
    能追溯到是哪次请求、哪次恢复尝试、哪次等价评估产出的。
    """

    request_id: str
    result_capture_id: str
    work_id: str
    restoration_attempt_id: str
    equivalence_assessment_id: str
    source_grade: EvidenceSourceGrade

    def __post_init__(self) -> None:
        for field_name in (
            "request_id",
            "result_capture_id",
            "work_id",
            "restoration_attempt_id",
            "equivalence_assessment_id",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise EvidenceRequestConsumptionError(f"补采证明缺少 {field_name}", retryable=False)
        if not isinstance(self.source_grade, EvidenceSourceGrade):
            raise EvidenceRequestConsumptionError("补采证明缺少来源等级", retryable=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class ProbeSupplementAttachment:
    """一次幂等挂接的结果。"""

    request_id: str
    manifest_id: str
    attached: bool
    evidence_input_set_id: str | None = None


class EvidenceRequestConsumptionService:
    """补采请求消费闭环的三端共用实现。

    ``manifest_finalizer`` 可选：补采结果 Capture 只有待定 Manifest 时，用它先定稿
    再取 PASS 版本；平台侧没有定稿通道就留空，此时拿不到 PASS Manifest 会按可重试
    错误抛出，等采集侧补齐。
    """

    def __init__(
        self,
        *,
        requests: EvidenceRequestRepository,
        manifests: EvidenceManifestRepository,
        attacher: EvidenceSupplementAttacher,
        manifest_finalizer: object | None = None,
        input_set_resealer: SupplementInputSetResealService | None = None,
    ) -> None:
        self._requests = requests
        self._manifests = manifests
        self._attacher = attacher
        self._manifest_finalizer = manifest_finalizer
        self._input_set_resealer = input_set_resealer

    async def claim_next(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        finalization_epoch: int,
        lease_owner: str,
        lease_seconds: int,
    ) -> EvidenceRequestClaimResult:
        """领一条待本端处理的补采请求，带租约防止多个执行器抢同一条。

        领取范围见模块开头：只覆盖活会话内的 pending 请求，且目标 Capture 必须属于
        传入的 ``agent_run_id``。领不到时返回 ``EMPTY``，不是错误。
        """

        return await self._requests.claim_next(
            task_id=_require_text(task_id, "task_id"),
            agent_run_id=_require_text(agent_run_id, "agent_run_id"),
            finalization_epoch=finalization_epoch,
            lease_owner=_require_text(lease_owner, "lease_owner"),
            lease_seconds=lease_seconds,
        )

    async def release_lease(
        self,
        *,
        command: ReleaseEvidenceRequestLeaseCommand,
    ) -> EvidenceRequestLeaseReleaseResult:
        """这次没做成、但还想再试：交还租约并安排下次尝试。

        重试次数耗尽时由仓储直接转终态，返回的 disposition 会说明是安排了重试还是
        已经耗尽。命令里的租约凭证必须是 ``claim_next`` 那次发的，否则写不进去。
        """

        return await self._requests.release_lease(command=command)

    async def settle_lease(
        self,
        *,
        command: SettleEvidenceRequestLeaseCommand,
    ) -> int:
        """确定做不下去了：在有效租约下把请求直接写成终态，不再重试。"""

        return await self._requests.settle_lease(command=command)

    async def ensure_running(
        self,
        *,
        task_id: str,
        request_id: str,
    ) -> StoredEvidenceRequest:
        """把 routed 的请求推进到 running；已在 running/satisfied 的原样返回。

        平台侧真正开始执行补采前调用一次，保证请求状态与执行事实一致。
        """

        task = _require_text(task_id, "task_id")
        request = _require_text(request_id, "request_id")
        stored = await self._load(task_id=task, request_id=request)
        if stored.status is not EvidenceRequestStatus.ROUTED:
            return stored
        await self._requests.transition(
            command=TransitionEvidenceRequestCommand(
                request_id=request,
                expected_status=EvidenceRequestStatus.ROUTED,
                target_status=EvidenceRequestStatus.RUNNING,
                row_version=stored.row_version,
            )
        )
        return await self._load(
            task_id=task,
            request_id=request,
            missing_message="Evidence Request 进入 running 后无法重载",
            missing_retryable=True,
        )

    async def attach_supplement(
        self,
        *,
        task_id: str,
        proof: ProbeCompletionProof,
    ) -> ProbeSupplementAttachment:
        """把补采回来的 Capture 挂回 Manifest 与 Input Set 血缘。

        顺序：结果 Capture 取 PASS Manifest（必要时先定稿）→ 校验请求身份与状态
        （routed 顺带推进到 running）→ 写入 Evidence Supplement。挂接本身幂等，
        重复调用不会重复计入血缘。

        写入 Supplement 的同一条语句会把请求原子置为 satisfied，所以**成功返回后
        请求已是终态，不要再调 ``settle_terminal``**。
        """

        task = _require_text(task_id, "task_id")
        if not isinstance(proof, ProbeCompletionProof):
            raise EvidenceRequestConsumptionError(
                "proof 必须为 ProbeCompletionProof", retryable=False
            )

        manifest = await self._require_pass_manifest(
            task_id=task,
            capture_id=proof.result_capture_id,
        )
        stored = await self.ensure_running(task_id=task, request_id=proof.request_id)
        if stored.status not in _ATTACHABLE_STATUSES:
            raise EvidenceRequestConsumptionError(
                f"Evidence Request 状态不允许挂接补采证据: {stored.status.value}",
                retryable=False,
            )

        persisted = await self._attacher.attach(
            AttachEvidenceSupplementCommand(
                task_id=task,
                request_id=proof.request_id,
                manifest_id=str(manifest.manifest_id),
                probe_work_id=proof.work_id,
                restoration_attempt_id=proof.restoration_attempt_id,
                equivalence_assessment_id=proof.equivalence_assessment_id,
                source_grade=proof.source_grade,
            )
        )
        attached = bool(getattr(persisted, "supplement", None))
        evidence_input_set_id: str | None = None
        if attached and self._input_set_resealer is not None:
            satisfied = await self._load(task_id=task, request_id=proof.request_id)
            resealed = await self._input_set_resealer.reseal_after_attach(
                stored=satisfied,
                supplement_manifest_id=str(manifest.manifest_id),
                restoration_attempt_id=proof.restoration_attempt_id,
                equivalence_assessment_id=proof.equivalence_assessment_id,
            )
            if resealed is not None:
                evidence_input_set_id = resealed.input_set_id
        return ProbeSupplementAttachment(
            request_id=proof.request_id,
            manifest_id=str(manifest.manifest_id),
            attached=attached,
            evidence_input_set_id=evidence_input_set_id,
        )

    async def settle_terminal(
        self,
        *,
        task_id: str,
        request_id: str,
        attempt: RestorationAttempt,
        pass_manifest_id: str | None = None,
    ) -> EvidenceRequestStatus | None:
        """按恢复尝试结果结请求终态，返回结成的终态；未结终态返回 ``None``。

        只用于「没能挂上」的收尾：不可满足 → unfulfillable；不等价或失败保留
        running 等重试或转人工，只记原因不改状态。等价 → satisfied 这一支是给不经过
        ``attach_supplement`` 的挂回路径留的（必须带 PASS Manifest 作为结论依据）；
        走过 ``attach_supplement`` 的成功路径请求已经是 satisfied，不需要再调本方法。

        请求已经在终态时直接把既成终态返回，不重复流转——这样成功路径误调也不会拿
        ``RUNNING → SATISFIED`` 去撞一条已经 satisfied 的行。
        """

        task = _require_text(task_id, "task_id")
        request = _require_text(request_id, "request_id")
        if not isinstance(attempt, RestorationAttempt):
            raise EvidenceRequestConsumptionError(
                "attempt 必须为 RestorationAttempt", retryable=False
            )
        stored = await self._load(task_id=task, request_id=request)
        if stored.status.is_terminal:
            return stored.status

        if attempt.status is RestorationAttemptStatus.EQUIVALENT:
            manifest_id = _require_text(pass_manifest_id or "", "pass_manifest_id")
            assessment_id = attempt.assessment.assessment_id if attempt.assessment else "none"
            await self._requests.transition(
                command=TransitionEvidenceRequestCommand(
                    request_id=request,
                    expected_status=EvidenceRequestStatus.RUNNING,
                    target_status=EvidenceRequestStatus.SATISFIED,
                    row_version=stored.row_version,
                    resolution_manifest_id=manifest_id,
                    resolution_reason=(
                        f"restoration_equivalent:{attempt.attempt_id}:{assessment_id}"
                    ),
                )
            )
            return EvidenceRequestStatus.SATISFIED

        if attempt.status is RestorationAttemptStatus.UNFULFILLABLE:
            await self._requests.transition(
                command=TransitionEvidenceRequestCommand(
                    request_id=request,
                    expected_status=EvidenceRequestStatus.RUNNING,
                    target_status=EvidenceRequestStatus.UNFULFILLABLE,
                    row_version=stored.row_version,
                    resolution_reason=f"restoration_unfulfillable:{attempt.error_code}",
                )
            )
            return EvidenceRequestStatus.UNFULFILLABLE

        logger.info(
            "evidence_request=%s 恢复未等价 status=%s error=%s",
            request,
            attempt.status.value,
            attempt.error_code,
        )
        return None

    async def _require_pass_manifest(self, *, task_id: str, capture_id: str) -> object:
        manifest = await self._manifests.get_latest_for_capture(
            task_id=task_id,
            capture_id=capture_id,
            require_quality_pass=True,
        )
        if manifest is None and self._manifest_finalizer is not None:
            pending = await self._manifests.get_latest_for_capture(
                task_id=task_id,
                capture_id=capture_id,
                require_quality_pass=False,
            )
            if pending is not None:
                await self._manifest_finalizer.finalize(pending)
                manifest = await self._manifests.get_latest_for_capture(
                    task_id=task_id,
                    capture_id=capture_id,
                    require_quality_pass=True,
                )
        if manifest is None:
            raise EvidenceRequestConsumptionError(
                "补采结果 Capture 尚未形成 PASS Manifest",
                retryable=True,
            )
        return manifest

    async def _load(
        self,
        *,
        task_id: str,
        request_id: str,
        missing_message: str = "Evidence Request 不存在",
        missing_retryable: bool = False,
    ) -> StoredEvidenceRequest:
        stored = await self._requests.get(request_id=request_id)
        if stored is None:
            raise EvidenceRequestConsumptionError(missing_message, retryable=missing_retryable)
        request = stored.request
        if request.request_id != request_id or request.task_id != task_id:
            raise EvidenceRequestConsumptionError(
                "Evidence Request 与补采执行域不一致",
                retryable=False,
            )
        return stored


def _require_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise EvidenceRequestConsumptionError(
            f"{field_name} 必须为无首尾空白的非空字符串", retryable=False
        )
    return value
