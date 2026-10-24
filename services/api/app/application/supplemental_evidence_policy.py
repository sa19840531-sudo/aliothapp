"""服务端补采能力的不可变策略合同。

该模块只描述补采是否被允许，不读取环境变量，也不执行任何采集动作；业务组合根应
从配置构造 :class:`SupplementalEvidencePolicy` 后注入到各层。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

SUPPLEMENTAL_EVIDENCE_CONFIG_KEY = "ALIOTH_SUPPLEMENTAL_EVIDENCE_ENABLED"
SUPPLEMENTAL_EVIDENCE_POLICY_VERSION = "supplemental-evidence-policy.v1"
SUPPLEMENTAL_EVIDENCE_DISABLED = "SUPPLEMENTAL_EVIDENCE_DISABLED"
SUPPLEMENTAL_EVIDENCE_NOT_READY = "SUPPLEMENTAL_EVIDENCE_NOT_READY"
_SUPPLEMENTAL_EVIDENCE_LINEAGE_KEYS = frozenset(
    {
        "evidence_request_id",
        "restoration_attempt_id",
        "equivalence_assessment_id",
        "supplemental_evidence_source",
    }
)


class SupplementalEvidenceOperation(StrEnum):
    """受补采总开关保护的业务操作。"""

    PRODUCE_REQUEST = "produce_request"
    ROUTE_REQUEST = "route_request"
    PUBLISH_PROBE = "publish_probe"
    CLAIM_PROBE = "claim_probe"
    EXECUTE_PROBE = "execute_probe"
    CAPTURE_SUPPLEMENT = "capture_supplement"
    ATTACH_SUPPLEMENT = "attach_supplement"


@dataclass(frozen=True, slots=True)
class SupplementalEvidenceDecision:
    """一次补采操作的确定性门禁结果，可直接用于日志和审计载荷。"""

    operation: SupplementalEvidenceOperation
    allowed: bool
    enabled: bool
    reason_code: str | None = None
    message: str | None = None
    retryable: bool = False
    policy_version: str = SUPPLEMENTAL_EVIDENCE_POLICY_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.operation, SupplementalEvidenceOperation):
            raise TypeError("operation 必须为 SupplementalEvidenceOperation")
        if not isinstance(self.allowed, bool):
            raise TypeError("allowed 必须为布尔值")
        if not isinstance(self.enabled, bool):
            raise TypeError("enabled 必须为布尔值")
        if self.allowed and not self.enabled:
            raise ValueError("配置关闭时不能允许补采操作")
        if self.retryable is not False:
            raise ValueError("补采开关门禁结果不可重试")
        if self.policy_version != SUPPLEMENTAL_EVIDENCE_POLICY_VERSION:
            raise ValueError("补采策略版本不受支持")
        if self.allowed:
            if self.reason_code is not None or self.message is not None:
                raise ValueError("允许执行的门禁结果不能携带拒绝原因")
            return
        expected_reason = (
            SUPPLEMENTAL_EVIDENCE_DISABLED
            if not self.enabled
            else SUPPLEMENTAL_EVIDENCE_NOT_READY
        )
        if self.reason_code != expected_reason:
            raise ValueError("拒绝原因与补采配置、运行时就绪状态不一致")
        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError("关闭状态必须携带可诊断消息")

    @property
    def denied(self) -> bool:
        """返回是否因总开关关闭而拒绝操作。"""
        return not self.allowed

    def to_dict(self) -> dict[str, object]:
        """返回稳定的结构化审计载荷。"""
        return {
            "operation": self.operation.value,
            "allowed": self.allowed,
            "enabled": self.enabled,
            "reason_code": self.reason_code,
            "message": self.message,
            "retryable": self.retryable,
            "policy_version": self.policy_version,
        }


class SupplementalEvidenceDisabledError(RuntimeError):
    """补采操作在能力关闭时被拒绝。"""

    def __init__(self, decision: SupplementalEvidenceDecision) -> None:
        if decision.allowed:
            raise ValueError("允许执行的门禁结果不能构造 DisabledError")
        self.decision = decision
        self.operation = decision.operation
        self.reason_code = decision.reason_code
        super().__init__(decision.message)


class SupplementalEvidenceNotReadyError(SupplementalEvidenceDisabledError):
    """补采已显式配置开启，但运行时闭环尚未就绪。"""


@dataclass(frozen=True, slots=True)
class SupplementalEvidencePolicy:
    """服务端补采能力策略；默认关闭且只能通过显式布尔值开启。"""

    enabled: bool = False
    runtime_ready: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise TypeError("enabled 必须为布尔值")
        if not isinstance(self.runtime_ready, bool):
            raise TypeError("runtime_ready 必须为布尔值")

    def decide(self, operation: SupplementalEvidenceOperation) -> SupplementalEvidenceDecision:
        """返回指定操作的不可变门禁结果。"""
        if not isinstance(operation, SupplementalEvidenceOperation):
            raise TypeError("operation 必须为 SupplementalEvidenceOperation")
        if self.enabled and self.runtime_ready:
            return SupplementalEvidenceDecision(
                operation=operation,
                allowed=True,
                enabled=True,
            )
        if self.enabled:
            return SupplementalEvidenceDecision(
                operation=operation,
                allowed=False,
                enabled=True,
                reason_code=SUPPLEMENTAL_EVIDENCE_NOT_READY,
                message=(
                    "补采能力已配置开启，但运行时闭环尚未就绪，操作已被安全拒绝："
                    f"{operation.value}"
                ),
            )
        return SupplementalEvidenceDecision(
            operation=operation,
            allowed=False,
            enabled=False,
            reason_code=SUPPLEMENTAL_EVIDENCE_DISABLED,
            message=(
                "补采能力当前已关闭，操作已被安全拒绝："
                f"{operation.value}（配置键 {SUPPLEMENTAL_EVIDENCE_CONFIG_KEY}）"
            ),
        )

    def require_enabled(
        self,
        operation: SupplementalEvidenceOperation,
    ) -> SupplementalEvidenceDecision:
        """要求操作被允许；关闭时抛出带结构化结果的强类型异常。"""
        decision = self.decide(operation)
        if decision.denied:
            if decision.reason_code == SUPPLEMENTAL_EVIDENCE_NOT_READY:
                raise SupplementalEvidenceNotReadyError(decision)
            raise SupplementalEvidenceDisabledError(decision)
        return decision

    def validate_runtime_ready(self) -> None:
        """显式开启时要求运行时闭环就绪，供 Worker 组合根启动校验。"""
        if not self.enabled or self.runtime_ready:
            return
        decision = self.decide(SupplementalEvidenceOperation.EXECUTE_PROBE)
        raise SupplementalEvidenceNotReadyError(decision)


def build_current_supplemental_evidence_policy(
    *,
    enabled: bool,
) -> SupplementalEvidencePolicy:
    """构造当前版本生产策略；补采闭环未就绪前不允许仅靠配置放行。"""
    return SupplementalEvidencePolicy(enabled=enabled, runtime_ready=False)


def build_h5_supplemental_evidence_policy(
    *,
    enabled: bool,
    h5_v2_mode: str,
) -> SupplementalEvidencePolicy:
    """构造正式 H5 补采策略；仅 H5 V2 正式模式具备执行闭环。"""
    if not isinstance(enabled, bool):
        raise TypeError("enabled 必须为布尔值")
    mode = str(h5_v2_mode or "").strip().casefold()
    h5_runtime_ready = mode == "on"
    return SupplementalEvidencePolicy(
        enabled=enabled and h5_runtime_ready,
        runtime_ready=h5_runtime_ready,
    )


def build_lab_supplemental_evidence_policy(
    *,
    enabled: bool = True,
) -> SupplementalEvidencePolicy:
    """Lab／组合根显式灰度：允许 runtime_ready=True，不得用于生产工厂默认路径。"""
    if not isinstance(enabled, bool):
        raise TypeError("enabled 必须为布尔值")
    return SupplementalEvidencePolicy(enabled=enabled, runtime_ready=True)


def resolve_supplemental_evidence_policy(
    *,
    enabled: bool,
    lab_runtime_ready: bool = False,
) -> SupplementalEvidencePolicy:
    """生产默认走工厂；lab_runtime_ready=True 时注入可执行策略（Lab 刀 B 或生产 M6 authority）。

    注意：Lab M6/M7 的「计划可执行」（刀 A）不得传入本参数；
    须由 ``ALIOTH_APP_LAB_EXECUTES_PROBE`` 单独打开。
    """
    if lab_runtime_ready:
        return build_lab_supplemental_evidence_policy(enabled=enabled)
    return build_current_supplemental_evidence_policy(enabled=enabled)


def has_supplemental_evidence_lineage(parameters: Mapping[str, object]) -> bool:
    """按服务端受信血缘字段识别 Probe/补采动作，避免依赖页面动作名称。"""
    if not isinstance(parameters, Mapping):
        raise TypeError("parameters 必须为 Mapping")
    return any(
        isinstance(parameters.get(key), str) and bool(str(parameters[key]).strip())
        for key in _SUPPLEMENTAL_EVIDENCE_LINEAGE_KEYS
    )
