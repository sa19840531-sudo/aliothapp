"""App 流程基建 FlowDefinition 注册表（P4.1／P4.2 声明式合同）。

设计依据：2026-10-22 §4.1／§5；冻结 code／version／hash，禁止任意脚本上传。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from services.api.app.domain.exploration.flow_foundation import (
    FORBIDDEN_AUTO_OPEN_CAPABILITY_IDS,
    FlowFoundationContractError,
    TargetPlatform,
    assert_playback_proof_flow_allowed,
)
from services.api.app.domain.protocol import require_text, stable_protocol_hash

FLOW_DEFINITION_CONTRACT_VERSION = "flow-foundation.definition.v1"

APP_DETAIL_PURE_OBSERVATION_CODE = "app_detail_pure_observation"
APP_LOW_RISK_EXPAND_CLOSE_RETURN_CODE = "app_low_risk_expand_close_return"


@dataclass(frozen=True, kw_only=True)
class FlowDefinition:
    """版本化流程定义；启动后以 definition_hash 冻结身份。"""

    code: str
    version: str
    target_platform: TargetPlatform
    capability_requirements: tuple[str, ...]
    forbid_playback_proof: bool = True
    forbid_clear_data: bool = True
    forbid_payment: bool = True
    allow_partial_capabilities: tuple[str, ...] = ()
    notes: str = ""
    definition_hash: str = ""
    contract_version: str = FLOW_DEFINITION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        require_text(self.code, "code")
        require_text(self.version, "version")
        if not isinstance(self.target_platform, TargetPlatform):
            raise FlowFoundationContractError("target_platform 无效")
        if self.forbid_playback_proof is False:
            # 显式关闭禁令时仍须能力矩阵 ready，否则不得宣称可启动。
            try:
                assert_playback_proof_flow_allowed()
            except FlowFoundationContractError as exc:
                raise FlowFoundationContractError(
                    "FlowDefinition 不得关闭播放验证禁令：App 无 ready 进度源"
                ) from exc
        forbidden = set(self.capability_requirements) & FORBIDDEN_AUTO_OPEN_CAPABILITY_IDS
        if forbidden and (
            self.forbid_clear_data
            or self.forbid_payment
            or "unauthorized_deep_link" in forbidden
        ):
            raise FlowFoundationContractError(
                f"FlowDefinition 能力需求含禁止自动开放项: {sorted(forbidden)}"
            )
        digest = stable_protocol_hash(
            {
                "contract_version": self.contract_version,
                "code": self.code,
                "version": self.version,
                "target_platform": self.target_platform.value,
                "capability_requirements": list(self.capability_requirements),
                "forbid_playback_proof": self.forbid_playback_proof,
                "forbid_clear_data": self.forbid_clear_data,
                "forbid_payment": self.forbid_payment,
                "allow_partial_capabilities": list(self.allow_partial_capabilities),
            }
        )
        if self.definition_hash and self.definition_hash != digest:
            raise FlowFoundationContractError("definition_hash 与冻结内容不一致")
        object.__setattr__(self, "definition_hash", digest)

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "version": self.version,
            "definition_hash": self.definition_hash,
            "target_platform": self.target_platform.value,
            "capability_requirements": list(self.capability_requirements),
            "forbid_playback_proof": self.forbid_playback_proof,
            "forbid_clear_data": self.forbid_clear_data,
            "forbid_payment": self.forbid_payment,
            "allow_partial_capabilities": list(self.allow_partial_capabilities),
            "notes": self.notes,
            "contract_version": self.contract_version,
        }


APP_DETAIL_PURE_OBSERVATION = FlowDefinition(
    code=APP_DETAIL_PURE_OBSERVATION_CODE,
    version="1.0.0",
    target_platform=TargetPlatform.APP,
    capability_requirements=("device_screenshot", "uia_tree", "window_process_identity"),
    allow_partial_capabilities=("uia_tree", "media_playback_position"),
    forbid_playback_proof=True,
    forbid_clear_data=True,
    forbid_payment=True,
    notes="P4.1 纯观察：已到详情类业务态的有界连续观察；不冒充播放验证",
)

APP_LOW_RISK_EXPAND_CLOSE_RETURN = FlowDefinition(
    code=APP_LOW_RISK_EXPAND_CLOSE_RETURN_CODE,
    version="1.0.0",
    target_platform=TargetPlatform.APP,
    capability_requirements=(
        "device_screenshot",
        "uia_tree",
        "window_process_identity",
        "warm_restore",
    ),
    allow_partial_capabilities=("uia_tree", "warm_restore"),
    forbid_playback_proof=True,
    forbid_clear_data=True,
    forbid_payment=True,
    notes="P4.2 低风险展开／关闭后受控返回；禁止支付／清数据／未授权 deep link",
)

_REGISTRY: Mapping[str, FlowDefinition] = {
    APP_DETAIL_PURE_OBSERVATION.code: APP_DETAIL_PURE_OBSERVATION,
    APP_LOW_RISK_EXPAND_CLOSE_RETURN.code: APP_LOW_RISK_EXPAND_CLOSE_RETURN,
}


def get_flow_definition(code: str) -> FlowDefinition:
    require_text(code, "code")
    definition = _REGISTRY.get(code)
    if definition is None:
        raise FlowFoundationContractError(f"未知 FlowDefinition: {code}")
    return definition


def list_app_flow_definitions() -> tuple[FlowDefinition, ...]:
    return tuple(_REGISTRY[key] for key in sorted(_REGISTRY))
