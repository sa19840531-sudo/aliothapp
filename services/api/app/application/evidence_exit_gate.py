"""Evidence Exit Gate 的应用端口、动作范围与结构化阻断错误。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from services.api.app.application.h5_exploration_recovery import (
    H5FailureStage,
    build_h5_recovery_session_id,
)
from services.api.app.application.h5_viewport import h5_viewport_parameters
from services.api.app.application.surface_sessions import SessionCommandPermit
from services.api.app.domain.exploration import (
    EvidenceExitBlockedOutcome,
    EvidenceExitBusinessOutcome,
    EvidenceExitGateDecision,
    EvidenceExitRequirementSet,
    EvidenceExitReservationStatus,
    build_evidence_exit_outcome_id,
    classify_evidence_exit_business_outcome,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)

LEAVE_CAPABLE_OPERATIONS = frozenset(
    {
        InteractionOperation.NAVIGATE,
        InteractionOperation.CLICK,
        InteractionOperation.TAP,
        InteractionOperation.BACK,
        InteractionOperation.REFRESH,
    }
)


@dataclass(frozen=True, kw_only=True)
class PersistedEvidenceExitRequirementSet:
    """需求快照的幂等持久化结果。"""

    requirement_set: EvidenceExitRequirementSet
    created: bool


class EvidenceExitRequirementRevisionConflict(RuntimeError):
    """需求集 revision 已被并发发布者推进，可重新读取 latest 后重算。"""


class EvidenceExitRequirementRepository(Protocol):
    """Rule Matcher 与 Evidence Pipeline 追加需求收敛快照的端口。"""

    async def add(
        self,
        *,
        requirement_set: EvidenceExitRequirementSet,
    ) -> PersistedEvidenceExitRequirementSet: ...

    async def get_latest(
        self,
        *,
        task_id: str,
        session_id: str,
        capture_id: str,
    ) -> EvidenceExitRequirementSet | None: ...


class EvidenceExitGate(Protocol):
    """Capture Coordinator 在 Session Lease 内调用的确定性门禁。"""

    async def reserve(
        self,
        *,
        intent: ActionIntent,
        permit: SessionCommandPermit,
    ) -> EvidenceExitGateDecision | None: ...

    async def reserve_action_recovery(
        self,
        *,
        intent: ActionIntent,
        permit: SessionCommandPermit,
        action_execution_id: str,
        before_capture_id: str | None,
    ) -> EvidenceExitGateDecision | None:
        """重新校验尚未送达 Driver 的 App 动作，并生成独立恢复预留。"""

    async def finalize_stale_action_reservations(
        self,
        *,
        intent: ActionIntent,
        permit: SessionCommandPermit,
        action_execution_id: str,
        status: EvidenceExitReservationStatus,
    ) -> int:
        """终结崩溃前遗留的原始及恢复预留，避免关闭门禁长期等待。"""

    async def finalize(
        self,
        *,
        decision: EvidenceExitGateDecision,
        status: EvidenceExitReservationStatus,
    ) -> None: ...

    async def expire_stale(self, *, limit: int = 100) -> int: ...


class EvidenceExitGateBlockedError(RuntimeError):
    """离场动作被确定性证据门禁拒绝。"""

    def __init__(
        self,
        decision: EvidenceExitGateDecision,
        outcome: EvidenceExitBlockedOutcome,
    ) -> None:
        if not isinstance(decision, EvidenceExitGateDecision) or decision.allow_leave:
            raise ValueError("阻断错误必须携带不允许离场的门禁结论")
        if not isinstance(outcome, EvidenceExitBlockedOutcome):
            raise ValueError("阻断错误必须携带结构化业务结果")
        if outcome.evaluation_id != decision.evaluation_id:
            raise ValueError("阻断业务结果与门禁结论身份不一致")
        self.decision = decision
        self.outcome = outcome
        self.error_code = outcome.error_code
        self.retryable = outcome.retryable
        reasons = ", ".join(item.value for item in decision.block_reasons)
        super().__init__(f"Evidence Exit Gate 阻断离场动作：{reasons}")


_OUTCOME_ERROR_MAPPING = {
    EvidenceExitBusinessOutcome.EVIDENCE_INSUFFICIENT: (
        "EVIDENCE_INSUFFICIENT",
        False,
    ),
    EvidenceExitBusinessOutcome.EVIDENCE_PENDING: ("EVIDENCE_COMMIT_PENDING", True),
    EvidenceExitBusinessOutcome.CONTROL_PLANE_INCOMPLETE: (
        "EVIDENCE_CONTROL_PLANE_INCOMPLETE",
        True,
    ),
    EvidenceExitBusinessOutcome.EVIDENCE_INTEGRITY_FAILED: (
        "EVIDENCE_INTEGRITY_FAILED",
        False,
    ),
}


def build_evidence_exit_blocked_outcome(
    *,
    decision: EvidenceExitGateDecision,
) -> EvidenceExitBlockedOutcome:
    """从门禁已持久化事实生成稳定的业务阻断结果。"""
    if not isinstance(decision, EvidenceExitGateDecision) or decision.allow_leave:
        raise ValueError("只允许为阻断门禁结论生成业务结果")
    snapshot = decision.snapshot
    business_outcome = classify_evidence_exit_business_outcome(snapshot)
    error_code, retryable = _OUTCOME_ERROR_MAPPING[business_outcome]
    return EvidenceExitBlockedOutcome(
        outcome_id=build_evidence_exit_outcome_id(
            evaluation_id=decision.evaluation_id,
            outcome=business_outcome,
            latest_manifest_id=snapshot.latest_manifest_id,
            latest_manifest_version=snapshot.latest_manifest_version,
            latest_manifest_hash=snapshot.latest_manifest_hash,
            requirement_set_id=snapshot.requirement_set_id,
        ),
        outcome=business_outcome,
        evaluation_id=decision.evaluation_id,
        action_intent_id=decision.action_intent_id,
        task_id=snapshot.task_id,
        session_id=snapshot.session_id,
        source_capture_id=snapshot.source_capture_id,
        requirement_set_id=snapshot.requirement_set_id,
        latest_manifest_id=snapshot.latest_manifest_id,
        latest_manifest_version=snapshot.latest_manifest_version,
        latest_manifest_hash=snapshot.latest_manifest_hash,
        latest_manifest_quality_status=snapshot.latest_manifest_quality_status,
        latest_manifest_quality_reason=snapshot.latest_manifest_quality_reason,
        block_reasons=decision.block_reasons,
        error_code=error_code,
        retryable=retryable,
        created_at=snapshot.evaluated_at,
    )


def requires_evidence_exit_gate(intent: ActionIntent) -> bool:
    """只有可能离开或重载当前 Surface 的动作需要离场门禁。

    E17 范围收敛：观察类动作（OBSERVE/TIMED_CAPTURE/SCROLL/HOVER/INPUT 等）
    不在 `LEAVE_CAPABLE_OPERATIONS` 内，天然不受门禁影响；
    父级为入口哨兵的动作同样放行——哨兵不指向任何已落库 Capture，
    门禁取不到离场事实，强行评估只会以「Capture 不是 Session 最新事实」失败。
    """
    if not isinstance(intent, ActionIntent):
        raise TypeError("intent 必须为 ActionIntent")
    if intent.action_type not in LEAVE_CAPABLE_OPERATIONS:
        return False
    if is_trusted_initial_surface_action(intent):
        return False
    if is_trusted_h5_browser_recovery_action(intent):
        return False
    if is_trusted_h5_targeted_recapture_action(intent):
        return False
    return not is_entry_sentinel_capture(intent.source_capture_id)


# E17：入口哨兵前缀——这些 source_capture_id 并不指向任何已落库 Capture，
# 门禁无从对其计算离场事实，只能放行首个进入动作。
ENTRY_SENTINEL_PREFIXES = ("h5-entry:", "web-entry:", "agent-entry:")
LEGACY_ENTRY_SENTINEL = "legacy-agent-entry"


def is_entry_sentinel_capture(source_capture_id: str) -> bool:
    """判断父 Capture 是否为入口哨兵而非真实采集。"""
    text = str(source_capture_id or "").strip()
    if text == LEGACY_ENTRY_SENTINEL:
        return True
    return any(text.startswith(prefix) for prefix in ENTRY_SENTINEL_PREFIXES)


def is_trusted_initial_surface_action(intent: ActionIntent) -> bool:
    """仅允许标记为首个进入动作的 Web Navigate 使用无父 Capture 入口。

    E17：除 H5 组合根的 `h5-entry:` 外，Main Agent Tool 路径的
    `agent-entry:` 与历史 `legacy-agent-entry` 同属入口哨兵——
    它们不指向任何已落库 Capture，门禁对其取不到离场事实，
    若不放行则首个导航必然以「Capture 不是 Session 最新事实」失败。
    """
    if not isinstance(intent, ActionIntent):
        raise TypeError("intent 必须为 ActionIntent")
    if intent.surface_kind is not SurfaceKind.WEB:
        return False
    if intent.action_type is not InteractionOperation.NAVIGATE:
        return False
    if not is_entry_sentinel_capture(intent.source_capture_id):
        return False
    # H5/Web V2 入口由各自组合根显式标记；agent-entry 哨兵本身即入口语义。
    if intent.source_capture_id.startswith(("h5-entry:", "web-entry:")):
        return intent.parameters.get("initial_surface_action") is True
    return True


def is_trusted_h5_browser_recovery_action(intent: ActionIntent) -> bool:
    """仅允许组合根生成的稳定 recovery Navigate 绕过已 LOST source 的离场门。"""
    if not isinstance(intent, ActionIntent):
        raise TypeError("intent 必须为 ActionIntent")
    return trusted_h5_browser_recovery_identity(intent) is not None


def is_trusted_h5_targeted_recapture_action(intent: ActionIntent) -> bool:
    """H5 定向补采已在恢复门校验状态等价，不再重复要求新 Capture 的需求快照。"""
    if not isinstance(intent, ActionIntent):
        raise TypeError("intent 必须为 ActionIntent")
    parameters = intent.parameters
    required_lineage = (
        "evidence_request_id",
        "exploration_work_id",
        "restoration_plan_hash",
        "restoration_attempt_id",
        "equivalence_assessment_id",
    )
    return bool(
        intent.surface_kind is SurfaceKind.WEB
        and intent.session_id.startswith("h5:")
        and intent.action_type in LEAVE_CAPABLE_OPERATIONS
        and re.fullmatch(r"probe-intent:[0-9a-f]{64}", intent.intent_id)
        and intent.idempotency_key == intent.intent_id
        and intent.capture_profile.before.role == "probe_before"
        and intent.capture_profile.after.role == "probe_result_replay"
        and parameters.get("supplemental_evidence_source") == "targeted_recapture"
        and parameters.get("viewport_width") == 390
        and parameters.get("viewport_height") == 844
        and all(
            isinstance(parameters.get(key), str) and bool(str(parameters[key]).strip())
            for key in required_lineage
        )
    )


def trusted_h5_browser_recovery_identity(
    intent: ActionIntent,
) -> tuple[str, str, str, H5FailureStage, int] | None:
    """返回完全闭合的恢复动作身份；任一字段不一致都拒绝授权。"""
    if not isinstance(intent, ActionIntent):
        raise TypeError("intent 必须为 ActionIntent")
    target = re.fullmatch(r"h5:([^:]+):recovery:[0-9a-f]{20}", intent.session_id)
    identity = re.fullmatch(
        r"h5-browser-recovery:(.+):(before_capture|action|after_capture):([1-9][0-9]*)",
        intent.intent_id,
    )
    source = str(intent.parameters.get("recovery_source_session_id") or "")
    destination = intent.parameters.get("destination_url")
    allowed_keys = {
        "destination_url",
        "h5_browser_recovery",
        "recovery_source_session_id",
        "failure_stage",
    }
    viewport = h5_viewport_parameters()
    parameter_keys_valid = set(intent.parameters) == allowed_keys or (
        set(intent.parameters) == allowed_keys | set(viewport)
        and all(type(intent.parameters[key]) is int and intent.parameters[key] == value
                for key, value in viewport.items())
    )
    try:
        parsed_destination = urlsplit(str(destination or ""))
        stage = H5FailureStage(identity.group(2)) if identity is not None else None
        attempt = int(identity.group(3)) if identity is not None else 0
        attempt_allowed = bool(
            (stage is H5FailureStage.BEFORE_CAPTURE and attempt == 1)
            or (stage in {H5FailureStage.ACTION, H5FailureStage.AFTER_CAPTURE} and attempt <= 2)
        )
        expected_session = (
            build_h5_recovery_session_id(
                agent_run_id=target.group(1),
                work_id=identity.group(1),
                stage=stage,
                recovery_attempt_count=attempt,
            )
            if target is not None and identity is not None and stage is not None
            else None
        )
        validated_port = parsed_destination.port
    except (TypeError, ValueError):
        return None
    trusted = bool(
        intent.surface_kind is SurfaceKind.WEB
        and intent.action_type is InteractionOperation.NAVIGATE
        and intent.parameters.get("h5_browser_recovery") is True
        and target is not None
        and identity is not None
        and expected_session == intent.session_id
        and source != intent.session_id
        and (
            source == f"h5:{target.group(1)}"
            or re.fullmatch(
                rf"h5:{re.escape(target.group(1))}:recovery:[0-9a-f]{{20}}",
                source,
            )
            is not None
        )
        and intent.parameters.get("failure_stage") == identity.group(2)
        and attempt_allowed
        and intent.idempotency_key == intent.intent_id
        and intent.purpose == "恢复丢失的 H5 浏览器会话并重建可信基线"
        and intent.risk_level is InteractionRiskLevel.LOW
        and isinstance(destination, str)
        and destination == intent.semantic_target
        and parsed_destination.scheme.casefold() in {"http", "https"}
        and bool(parsed_destination.hostname)
        and parsed_destination.username is None
        and parsed_destination.password is None
        and (validated_port is None or isinstance(validated_port, int))
        and parameter_keys_valid
    )
    if not trusted or target is None or identity is None or stage is None:
        return None
    return target.group(1), identity.group(1), source, stage, attempt
