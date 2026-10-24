"""规则证据缺口：判定侧对「还缺什么证据」的结构化声明。

规则判定得出「证据不足」时，不再只留一句自由文本，而是明确写下：

- 哪个页面：页型、页面标识、来源采集；
- 哪个状态：观测到的页面状态与规则要求的页面状态；
- 哪个区域：规则检查点与证据层级（底层页面 / 浮层）；
- 关闭前还是关闭后：弹窗关闭前的浮层证据，还是关闭后的底层页面证据；
- 规则声明的补采方式：exact / equivalent / fresh / manual（未声明按 manual）。

缺口只描述「要证明什么」，不描述「怎么操作」：这里没有选择器、坐标或设备句柄，
判定流程只提出要求，操作浏览器或设备永远是运行面的事。非 manual 的缺口同时算出
将要提出的 EvidenceRequest（稳定编号与恢复约束），但当前阶段只记录，不执行。

「关闭弹窗后才能取到底层页面」这类缺口，光靠再看一眼是永远补不到的。这种缺口会
带上语义级的交互要求（做什么动作、对什么语义目标、为了证明什么），让运行面知道
必须先关掉遮挡再采。语义目标仍然不是定位器：选哪个元素、点哪个坐标由运行面决定。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from services.api.app.domain.inspection.evidence_request import (
    EvidenceInteractionRequirement,
    EvidenceReplayPolicy,
    EvidenceRequest,
    EvidenceRequiredEquivalence,
    EvidenceSessionDependency,
    EvidenceSideEffectPolicy,
    EvidenceSourceGrade,
    build_evidence_request,
)
from services.api.app.domain.interaction.enums import InteractionOperation
from services.api.app.domain.protocol import (
    parse_aware_datetime,
    require_mapping,
    require_sequence,
    require_text,
    stable_protocol_hash,
)

EVIDENCE_GAP_PROTOCOL_VERSION = "evidence-gap.v1"
RECAPTURE_POLICY_CONTRACT_KEY = "recapture_policy"

# 缺口里不允许出现的操作细节：判定只提要求，不指挥动作。
FORBIDDEN_GAP_KEYS = frozenset(
    {"selector", "xpath", "css", "coordinate", "coordinates", "x", "y", "bounds", "node_id"}
)


class RecapturePolicy(StrEnum):
    """规则合同事先声明的补采方式（专项三 §5.1）。"""

    EXACT = "exact"
    EQUIVALENT = "equivalent"
    FRESH = "fresh"
    MANUAL = "manual"


class OverlayPhase(StrEnum):
    """缺的是弹窗关闭前的浮层证据，还是关闭后的底层页面证据。"""

    BEFORE_CLOSE = "before_close"
    AFTER_CLOSE = "after_close"
    NOT_APPLICABLE = "not_applicable"


class GapDisposition(StrEnum):
    """当前阶段对缺口的处置：只记录，或必须转人工。"""

    RECORDED_ONLY = "recorded_only"
    MANUAL_REVIEW_REQUIRED = "manual_review_required"


_POLICY_TEXT = {
    RecapturePolicy.EXACT: "必须恢复到原状态再补采",
    RecapturePolicy.EQUIVALENT: "允许在等价状态下补采",
    RecapturePolicy.FRESH: "允许重新观察当前页面",
    RecapturePolicy.MANUAL: "规则未声明补采方式，需人工处理",
}

_PHASE_TEXT = {
    OverlayPhase.BEFORE_CLOSE: "弹窗关闭前",
    OverlayPhase.AFTER_CLOSE: "弹窗关闭后",
    OverlayPhase.NOT_APPLICABLE: "",
}

# 非 manual 声明到 EvidenceRequest 恢复约束的固定映射；运行面不得在执行时放宽。
_REQUEST_CONSTRAINTS: dict[
    RecapturePolicy,
    tuple[
        EvidenceSessionDependency,
        EvidenceReplayPolicy,
        EvidenceRequiredEquivalence,
        EvidenceSourceGrade,
    ],
] = {
    RecapturePolicy.EXACT: (
        EvidenceSessionDependency.ACTIVE_SESSION,
        EvidenceReplayPolicy.SAME_SURFACE_ONLY,
        EvidenceRequiredEquivalence.SAME_SURFACE_CONTINUATION,
        EvidenceSourceGrade.A1,
    ),
    RecapturePolicy.EQUIVALENT: (
        EvidenceSessionDependency.ACTIVE_SESSION,
        EvidenceReplayPolicy.SAME_CONTEXT_ALLOWED,
        EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT,
        EvidenceSourceGrade.B,
    ),
    RecapturePolicy.FRESH: (
        EvidenceSessionDependency.SNAPSHOT,
        EvidenceReplayPolicy.NEW_SESSION_ALLOWED,
        EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT,
        EvidenceSourceGrade.C,
    ),
}


# 总设计 §19.1 的正式写法与本模块短名一一对应，规则种子里两种都认。
_POLICY_ALIASES = {
    "exact_state": RecapturePolicy.EXACT,
    "equivalent_state": RecapturePolicy.EQUIVALENT,
    "fresh_observation": RecapturePolicy.FRESH,
    "manual_only": RecapturePolicy.MANUAL,
}


def _policy_from_text(value: Any) -> RecapturePolicy | None:
    text = str(value or "").strip().casefold()
    if not text:
        return None
    if text in _POLICY_ALIASES:
        return _POLICY_ALIASES[text]
    try:
        return RecapturePolicy(text)
    except ValueError:
        return None


def parse_recapture_declaration(
    input_contract: Mapping[str, Any] | None,
) -> tuple[RecapturePolicy, RecapturePolicy | None]:
    """读取规则合同里的补采声明：首选方式与可选备选方式。

    合同写法：``"recapture_policy": "equivalent_state"`` 或
    ``{"kind": "equivalent_state", "fallback": "fresh_observation"}``。缺失或非法一律按
    manual、不带备选，不替规则放宽；备选只在首选做不到时才允许，且 fresh 备选产生的是
    独立新结论，不填补原缺口（总设计 §17）。
    """

    contract = input_contract if isinstance(input_contract, Mapping) else {}
    declared = contract.get(RECAPTURE_POLICY_CONTRACT_KEY)
    fallback_value: Any = None
    if isinstance(declared, Mapping):
        fallback_value = declared.get("fallback")
        declared = declared.get("kind")
    kind = _policy_from_text(declared)
    if kind is None:
        return RecapturePolicy.MANUAL, None
    fallback = _policy_from_text(fallback_value)
    if fallback is kind:
        fallback = None
    return kind, fallback


def parse_recapture_policy(input_contract: Mapping[str, Any] | None) -> RecapturePolicy:
    """只取首选方式；缺失或非法一律按 manual。"""

    return parse_recapture_declaration(input_contract)[0]


@dataclass(frozen=True, kw_only=True)
class EvidenceGapTarget:
    """缺口指向的页面、状态与区域。"""

    page_capture_id: str
    page_type: str = ""
    page_url: str = ""
    page_title: str = ""
    surface_kind: str = ""
    observed_page_state: str = ""
    required_page_states: tuple[str, ...] = ()
    region_kind: str = "page"
    region_label: str = ""
    layer_target: str = "base_page"
    required_interaction: EvidenceInteractionRequirement | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_capture_id": self.page_capture_id,
            "page_type": self.page_type,
            "page_url": self.page_url,
            "page_title": self.page_title,
            "surface_kind": self.surface_kind,
            "observed_page_state": self.observed_page_state,
            "required_page_states": list(self.required_page_states),
            "region": {
                "kind": self.region_kind,
                "label": self.region_label,
                "layer_target": self.layer_target,
            },
            **({"required_interaction": self.required_interaction.to_dict()}
               if self.required_interaction is not None else {}),
        }


@dataclass(frozen=True, kw_only=True)
class EvidenceGapMissing:
    """具体缺什么：产物、采集角色、页面状态、展示层拒绝、模型说明。"""

    artifact_types: tuple[str, ...] = ()
    capture_roles: tuple[str, ...] = ()
    page_states: tuple[str, ...] = ()
    presentation_code: str = ""
    presentation_reason: str = ""
    judge_statements: tuple[str, ...] = ()
    quality_gate_sources: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_types": list(self.artifact_types),
            "capture_roles": list(self.capture_roles),
            "page_states": list(self.page_states),
            "presentation": (
                {"code": self.presentation_code, "reason": self.presentation_reason}
                if self.presentation_code
                else None
            ),
            "judge_statements": list(self.judge_statements),
            "quality_gate_sources": list(self.quality_gate_sources),
        }


@dataclass(frozen=True, kw_only=True)
class EvidenceGap:
    """一条规则在一次采集上的证据缺口（不可变、可落库、可重建）。"""

    gap_id: str
    task_id: str
    rule_id: str
    rule_code: str
    rule_name: str
    category: str
    agent_run_id: str | None
    observation_packet_id: str | None
    target: EvidenceGapTarget
    overlay_phase: OverlayPhase
    recapture_policy: RecapturePolicy
    recapture_fallback: RecapturePolicy | None = None
    missing: EvidenceGapMissing
    reason_code: str
    evidence_goal: str
    disposition: GapDisposition
    requested_request: Mapping[str, Any] | None = None
    protocol_version: str = EVIDENCE_GAP_PROTOCOL_VERSION
    diagnostics: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "gap_id": self.gap_id,
            "task_id": self.task_id,
            "rule_id": self.rule_id,
            "rule_code": self.rule_code,
            "rule_name": self.rule_name,
            "category": self.category,
            "agent_run_id": self.agent_run_id,
            "observation_packet_id": self.observation_packet_id,
            "target": self.target.to_dict(),
            "overlay_phase": self.overlay_phase.value,
            "recapture_policy": self.recapture_policy.value,
            "recapture_fallback": (
                self.recapture_fallback.value if self.recapture_fallback is not None else None
            ),
            "missing": self.missing.to_dict(),
            "reason_code": self.reason_code,
            "evidence_goal": self.evidence_goal,
            "disposition": self.disposition.value,
            "requested_request": (
                dict(self.requested_request) if self.requested_request is not None else None
            ),
            "diagnostics": dict(self.diagnostics),
        }


def build_evidence_gap(
    *,
    task_id: str,
    rule_id: str,
    rule_code: str,
    rule_name: str = "",
    category: str = "",
    check_point: str = "",
    input_contract: Mapping[str, Any] | None = None,
    agent_run_id: str | None = None,
    observation_packet_id: str | None = None,
    page_capture_id: str,
    page: Mapping[str, Any] | None = None,
    layer_target: str = "base_page",
    required_page_states: Sequence[str] = (),
    missing_artifact_types: Sequence[str] = (),
    missing_capture_roles: Sequence[str] = (),
    presentation_code: str | None = None,
    presentation_reason: str | None = None,
    judge_statements: Sequence[str] = (),
    quality_gate_sources: Sequence[str] = (),
    reason_code: str,
    reason: str = "",
    request_expires_at: datetime | None = None,
    work_item_id: str = "",
    attempt_id: str = "",
    required_interaction: EvidenceInteractionRequirement | None = None,
    auto_recapture_block_reason: str | None = None,
) -> EvidenceGap:
    """从判定上下文推导缺口；只用语义信息，不带任何操作细节。"""

    page_payload = page if isinstance(page, Mapping) else {}
    observed_state = _text(page_payload.get("page_state"))
    required_states = _unique(required_page_states)
    missing_states = (
        tuple(state for state in required_states if state and state != observed_state)
        if required_states and observed_state not in required_states
        else ()
    )
    normalized_layer = _text(layer_target) or "base_page"
    overlay_phase = _resolve_overlay_phase(
        layer_target=normalized_layer,
        presentation_code=_text(presentation_code),
        missing_states=missing_states,
        missing_capture_roles=_unique(missing_capture_roles),
    )
    policy, fallback = parse_recapture_declaration(input_contract)
    target = EvidenceGapTarget(
        page_capture_id=page_capture_id,
        page_type=_text(page_payload.get("page_type")),
        page_url=_text(page_payload.get("url"))[:2048],
        page_title=_text(page_payload.get("title"))[:256],
        surface_kind=_text(page_payload.get("surface_kind")),
        observed_page_state=observed_state,
        required_page_states=required_states,
        region_kind="overlay" if normalized_layer == "any" else "page",
        region_label=_region_label(category=category, check_point=check_point),
        layer_target=normalized_layer,
        required_interaction=required_interaction,
    )
    missing = EvidenceGapMissing(
        artifact_types=_unique(missing_artifact_types),
        capture_roles=_unique(missing_capture_roles),
        page_states=missing_states,
        presentation_code=_text(presentation_code),
        presentation_reason=_text(presentation_reason),
        judge_statements=tuple(_text(item)[:500] for item in judge_statements if _text(item)),
        quality_gate_sources=_unique(quality_gate_sources),
    )
    evidence_goal = _evidence_goal(
        target=target,
        missing=missing,
        overlay_phase=overlay_phase,
        policy=policy,
        reason=_text(reason),
    )
    gap_id = "evidence-gap:" + stable_protocol_hash(
        {
            "task_id": task_id,
            "rule_id": rule_id,
            "rule_code": rule_code,
            "page_capture_id": page_capture_id,
            "reason_code": reason_code,
            "missing": missing.to_dict(),
            "overlay_phase": overlay_phase.value,
        }
    )
    disposition = (
        GapDisposition.MANUAL_REVIEW_REQUIRED
        if policy is RecapturePolicy.MANUAL or auto_recapture_block_reason
        else GapDisposition.RECORDED_ONLY
    )
    requested: Mapping[str, Any] | None = None
    if (
        policy is not RecapturePolicy.MANUAL
        and request_expires_at is not None
        and not auto_recapture_block_reason
    ):
        requested = _request_preview(
            build_evidence_request_from_gap(
                policy=policy,
                task_id=task_id,
                rule_id=rule_id,
                rule_code=rule_code,
                page_capture_id=page_capture_id,
                target=target,
                missing=missing,
                overlay_phase=overlay_phase,
                reason_code=reason_code,
                evidence_goal=evidence_goal,
                expires_at=request_expires_at,
                work_item_id=work_item_id or f"pending:{task_id}",
                attempt_id=attempt_id or f"pending:{page_capture_id}",
            )
        )
    gap = EvidenceGap(
        gap_id=gap_id,
        task_id=task_id,
        rule_id=rule_id,
        rule_code=rule_code,
        rule_name=_text(rule_name),
        category=_text(category),
        agent_run_id=_text(agent_run_id) or None,
        observation_packet_id=_text(observation_packet_id) or None,
        target=target,
        overlay_phase=overlay_phase,
        recapture_policy=policy,
        recapture_fallback=fallback,
        missing=missing,
        reason_code=_text(reason_code) or "EVIDENCE_INSUFFICIENT",
        evidence_goal=evidence_goal,
        disposition=disposition,
        requested_request=requested,
        diagnostics=(
            {"auto_recapture_block_reason": auto_recapture_block_reason}
            if auto_recapture_block_reason else {}
        ),
    )
    assert_gap_has_no_operation_details(gap.to_dict())
    return gap


def build_evidence_request_from_gap(
    *,
    policy: RecapturePolicy,
    task_id: str,
    rule_id: str,
    rule_code: str,
    page_capture_id: str,
    target: EvidenceGapTarget,
    missing: EvidenceGapMissing,
    overlay_phase: OverlayPhase,
    reason_code: str,
    evidence_goal: str,
    expires_at: datetime,
    work_item_id: str,
    attempt_id: str,
) -> EvidenceRequest:
    """把非 manual 缺口映射成协议内的 EvidenceRequest；manual 缺口不产生请求。"""

    if policy is RecapturePolicy.MANUAL:
        raise ValueError("manual 缺口不产生补采请求")
    session_dependency, replay_policy, required_equivalence, grade = _REQUEST_CONSTRAINTS[policy]
    artifact_types = missing.artifact_types or ("screenshot", "ui_snapshot")
    business: dict[str, Any] = {}
    if target.page_type:
        business["page_type"] = target.page_type
    if target.page_url:
        business["url_pattern"] = target.page_url
    semantic: dict[str, Any] = {}
    if target.required_page_states:
        semantic["page_state"] = list(target.required_page_states)
    elif target.observed_page_state:
        semantic["page_state"] = target.observed_page_state
    if target.region_label:
        semantic["region"] = target.region_label
    if missing.capture_roles:
        # Capture 角色属于证据要求，不是执行器动作细节。写入不变量后，补采结果
        # 才能在重判时恢复为 before/primary/refreshed 等规则合同角色。
        semantic["capture_roles"] = list(missing.capture_roles)
    target_invariants = {
        "environment": {"surface_kind": target.surface_kind} if target.surface_kind else {},
        "business": business,
        "semantic": semantic,
        "structure": {"layer_target": target.layer_target},
    }
    required_interaction = _resolve_required_interaction(
        overlay_phase=overlay_phase,
        session_dependency=session_dependency,
        target=target,
    )
    return build_evidence_request(
        work_item_id=work_item_id,
        attempt_id=attempt_id,
        task_id=task_id,
        rule_id=rule_id,
        rule_code=rule_code,
        target_capture_id=page_capture_id,
        reason_code=reason_code,
        evidence_goal=evidence_goal,
        required_artifact_types=artifact_types,
        session_dependency=session_dependency,
        replay_policy=replay_policy,
        required_equivalence=required_equivalence,
        side_effect_policy=EvidenceSideEffectPolicy.READ_ONLY,
        required_interaction=required_interaction,
        target_invariants=target_invariants,
        minimum_source_grade=grade,
        expires_at=expires_at,
    )


def materialize_evidence_request(
    *,
    gap: Mapping[str, Any],
    work_item_id: str,
    attempt_id: str,
    task_id: str,
    rule_id: str,
    rule_code: str,
    page_capture_id: str,
    agent_run_id: str | None = None,
) -> EvidenceRequest | None:
    """校验记录态缺口，并用当前 Handoff 身份生成正式 EvidenceRequest。"""

    payload = require_mapping(gap, "evidence_gap")
    assert_gap_has_no_operation_details(payload)
    if payload.get("protocol_version") != EVIDENCE_GAP_PROTOCOL_VERSION:
        raise ValueError("证据缺口协议版本不受支持")
    expected_identities = {
        "task_id": task_id,
        "rule_id": rule_id,
        "rule_code": rule_code,
    }
    for field_name, expected in expected_identities.items():
        if payload.get(field_name) != expected:
            raise ValueError(f"证据缺口 {field_name} 与当前 Handoff 不一致")
    if agent_run_id is not None and payload.get("agent_run_id") not in {
        None,
        agent_run_id,
    }:
        raise ValueError("证据缺口 agent_run_id 与当前 Handoff 不一致")

    try:
        policy = RecapturePolicy(require_text(payload.get("recapture_policy"), "recapture_policy"))
    except ValueError as exc:
        raise ValueError("证据缺口 recapture_policy 不受支持") from exc
    if policy is RecapturePolicy.MANUAL:
        return None
    diagnostics = payload.get("diagnostics")
    if (
        payload.get("disposition") == GapDisposition.MANUAL_REVIEW_REQUIRED.value
        and payload.get("requested_request") is None
        and isinstance(diagnostics, Mapping)
        and _text(diagnostics.get("auto_recapture_block_reason"))
    ):
        # 自动补采不可执行不改变冻结策略，也不能被标准化重新生成为请求。
        return None
    if payload.get("disposition") != GapDisposition.RECORDED_ONLY.value:
        raise ValueError("非人工证据缺口必须先处于 recorded_only")

    target_payload = require_mapping(payload.get("target"), "evidence_gap.target")
    target_capture_id = require_text(
        target_payload.get("page_capture_id"),
        "evidence_gap.target.page_capture_id",
    )
    if target_capture_id != page_capture_id:
        raise ValueError("证据缺口目标 Capture 与当前 Handoff 不一致")
    region = require_mapping(target_payload.get("region"), "evidence_gap.target.region")
    target = EvidenceGapTarget(
        page_capture_id=target_capture_id,
        page_type=_optional_payload_text(target_payload.get("page_type")),
        page_url=_optional_payload_text(target_payload.get("page_url")),
        page_title=_optional_payload_text(target_payload.get("page_title")),
        surface_kind=_optional_payload_text(target_payload.get("surface_kind")),
        observed_page_state=_optional_payload_text(target_payload.get("observed_page_state")),
        required_page_states=_payload_text_tuple(
            target_payload.get("required_page_states", ()),
            "evidence_gap.target.required_page_states",
        ),
        region_kind=require_text(region.get("kind"), "evidence_gap.target.region.kind"),
        region_label=require_text(region.get("label"), "evidence_gap.target.region.label"),
        layer_target=require_text(
            region.get("layer_target"),
            "evidence_gap.target.region.layer_target",
        ),
        required_interaction=(EvidenceInteractionRequirement.from_dict(
            require_mapping(target_payload["required_interaction"], "required_interaction")
        ) if target_payload.get("required_interaction") is not None else None),
    )

    missing_payload = require_mapping(payload.get("missing"), "evidence_gap.missing")
    presentation_payload = missing_payload.get("presentation")
    presentation = (
        require_mapping(presentation_payload, "evidence_gap.missing.presentation")
        if presentation_payload is not None
        else {}
    )
    missing = EvidenceGapMissing(
        artifact_types=_payload_text_tuple(
            missing_payload.get("artifact_types", ()),
            "evidence_gap.missing.artifact_types",
        ),
        capture_roles=_payload_text_tuple(
            missing_payload.get("capture_roles", ()),
            "evidence_gap.missing.capture_roles",
        ),
        page_states=_payload_text_tuple(
            missing_payload.get("page_states", ()),
            "evidence_gap.missing.page_states",
        ),
        presentation_code=_optional_payload_text(presentation.get("code")),
        presentation_reason=_optional_payload_text(presentation.get("reason")),
        judge_statements=_payload_text_tuple(
            missing_payload.get("judge_statements", ()),
            "evidence_gap.missing.judge_statements",
        ),
        quality_gate_sources=_payload_text_tuple(
            missing_payload.get("quality_gate_sources", ()),
            "evidence_gap.missing.quality_gate_sources",
        ),
    )
    try:
        overlay_phase = OverlayPhase(
            require_text(payload.get("overlay_phase"), "evidence_gap.overlay_phase")
        )
    except ValueError as exc:
        raise ValueError("证据缺口 overlay_phase 不受支持") from exc
    reason_code = require_text(payload.get("reason_code"), "evidence_gap.reason_code")
    expected_gap_id = "evidence-gap:" + stable_protocol_hash(
        {
            "task_id": task_id,
            "rule_id": rule_id,
            "rule_code": rule_code,
            "page_capture_id": page_capture_id,
            "reason_code": reason_code,
            "missing": missing.to_dict(),
            "overlay_phase": overlay_phase.value,
        }
    )
    if payload.get("gap_id") != expected_gap_id:
        raise ValueError("证据缺口 gap_id 与缺口事实不一致")
    preview = require_mapping(
        payload.get("requested_request"),
        "evidence_gap.requested_request",
    )
    request = build_evidence_request_from_gap(
        policy=policy,
        task_id=task_id,
        rule_id=rule_id,
        rule_code=rule_code,
        page_capture_id=page_capture_id,
        target=target,
        missing=missing,
        overlay_phase=overlay_phase,
        reason_code=reason_code,
        evidence_goal=require_text(payload.get("evidence_goal"), "evidence_gap.evidence_goal"),
        expires_at=parse_aware_datetime(preview.get("expires_at"), "expires_at"),
        work_item_id=work_item_id,
        attempt_id=attempt_id,
    )
    expected_preview = _request_preview(request)
    for field_name, expected in expected_preview.items():
        if preview.get(field_name) != expected:
            raise ValueError(f"证据缺口请求预览字段漂移：{field_name}")
    return request


def assert_gap_has_no_operation_details(payload: Any, *, path: str = "gap") -> None:
    """缺口是要求而不是操作脚本：出现选择器、坐标等键即视为越界。"""

    if isinstance(payload, Mapping):
        for key, value in payload.items():
            if str(key).strip().casefold() in FORBIDDEN_GAP_KEYS:
                raise ValueError(f"{path}.{key} 是操作细节，缺口不允许携带")
            assert_gap_has_no_operation_details(value, path=f"{path}.{key}")
    elif isinstance(payload, (list, tuple)):
        for index, item in enumerate(payload):
            assert_gap_has_no_operation_details(item, path=f"{path}[{index}]")


def _resolve_overlay_phase(
    *,
    layer_target: str,
    presentation_code: str,
    missing_states: tuple[str, ...],
    missing_capture_roles: tuple[str, ...],
) -> OverlayPhase:
    overlay_rule = layer_target in {"any", "overlay"}
    if presentation_code == "BASE_PAGE_OCCLUDED":
        return OverlayPhase.AFTER_CLOSE
    if presentation_code == "DIAGNOSTIC_EVIDENCE_EXCLUDED":
        return OverlayPhase.BEFORE_CLOSE if overlay_rule else OverlayPhase.AFTER_CLOSE
    if presentation_code == "EXTERNAL_SURFACE":
        return OverlayPhase.NOT_APPLICABLE
    if overlay_rule and (missing_states or "before" in missing_capture_roles):
        return OverlayPhase.BEFORE_CLOSE
    if overlay_rule and any(
        role in {"after", "refreshed", "landing"} for role in missing_capture_roles
    ):
        return OverlayPhase.AFTER_CLOSE
    return OverlayPhase.NOT_APPLICABLE


# 关掉遮挡这件事必须写成动作，不能只写进中文说明：H5 与 App 的执行面读不懂自然语言，
# 交互要求为空时只会按 OBSERVE 再看一眼，弹窗仍然盖在上面，补采必然拿回同一张废证据。
OVERLAY_CLOSE_SEMANTIC_TARGET = "遮挡底层页面的弹窗关闭控件"


def _resolve_required_interaction(
    *,
    overlay_phase: OverlayPhase,
    session_dependency: EvidenceSessionDependency,
    target: EvidenceGapTarget,
) -> EvidenceInteractionRequirement | None:
    """只有「先关掉遮挡才能取到底层页面」的缺口需要定向交互。

    浮层本身的证据（before_close）观察就能拍到，缺的是采集时机而不是动作，交给恢复
    重放处理；快照类补采（fresh → snapshot）会重新走常规清障，也不需要定向交互。
    这里给的是语义目标，选哪个元素、点哪个坐标仍然由运行面决定。
    """

    if overlay_phase is not OverlayPhase.AFTER_CLOSE:
        requirement = target.required_interaction
        if requirement is not None and requirement.operation not in {
            InteractionOperation.CLICK, InteractionOperation.SCROLL,
            InteractionOperation.TIMED_CAPTURE, InteractionOperation.SEEK_MEDIA,
        }:
            raise ValueError("只读补证合同不允许写入或提交操作")
        return requirement
    if session_dependency is not EvidenceSessionDependency.ACTIVE_SESSION:
        return None
    where = target.page_type or "当前页面"
    return EvidenceInteractionRequirement(
        operation=InteractionOperation.CLICK,
        semantic_target=OVERLAY_CLOSE_SEMANTIC_TARGET,
        purpose=f"关闭遮挡后取到 {where} 的「{target.region_label}」底层页面证据",
    )


_REGION_BY_CATEGORY = {
    "ad": "广告位或弹窗浮层",
    "player": "播放器区域",
    "payment": "支付与价格区域",
    "input_risk": "输入与提交控件",
    "interaction": "交互控件与反馈区域",
    "text": "页面可见文案",
    "visual": "页面可见内容",
}


def _region_label(*, category: str, check_point: str) -> str:
    scope = _REGION_BY_CATEGORY.get(_text(category).casefold(), "页面可见内容")
    point = " ".join(_text(check_point).split())[:120]
    return f"{scope}：{point}" if point else scope


def _evidence_goal(
    *,
    target: EvidenceGapTarget,
    missing: EvidenceGapMissing,
    overlay_phase: OverlayPhase,
    policy: RecapturePolicy,
    reason: str,
) -> str:
    where = target.page_type or "当前页面"
    if target.required_page_states and missing.page_states:
        where += f"（需处于 {'/'.join(target.required_page_states)} 状态）"
    elif target.observed_page_state:
        where += f"（当前状态 {target.observed_page_state}）"
    what: list[str] = []
    if missing.presentation_reason:
        what.append(missing.presentation_reason)
    if missing.capture_roles:
        what.append("缺采集角色 " + "、".join(missing.capture_roles))
    if missing.artifact_types:
        what.append("缺产物 " + "、".join(missing.artifact_types))
    if missing.quality_gate_sources:
        what.append("缺来源 " + "、".join(missing.quality_gate_sources))
    if not what and missing.judge_statements:
        what.append("；".join(missing.judge_statements[:3]))
    if not what and reason:
        what.append(reason[:200])
    phase = _PHASE_TEXT[overlay_phase]
    phase_text = f"，需要{phase}的证据" if phase else ""
    detail = "；".join(what) if what else "证据不足"
    return f"{where} 的「{target.region_label}」：{detail}{phase_text}。{_POLICY_TEXT[policy]}。"


def _request_preview(request: EvidenceRequest) -> dict[str, Any]:
    return {
        "request_id": request.request_id,
        "requirement_hash": request.requirement_hash,
        "session_dependency": request.session_dependency.value,
        "replay_policy": request.replay_policy.value,
        "required_equivalence": request.required_equivalence.value,
        "side_effect_policy": request.side_effect_policy.value,
        "minimum_source_grade": request.minimum_source_grade.value,
        "required_artifact_types": list(request.required_artifact_types),
        "expires_at": request.expires_at.isoformat(),
        "disposition": "recorded_only",
    }


def _text(value: Any) -> str:
    return str(value or "").strip()


def _optional_payload_text(value: Any) -> str:
    if value in (None, ""):
        return ""
    return require_text(value, "evidence_gap text")


def _payload_text_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    return tuple(require_text(item, field_name) for item in require_sequence(value, field_name))


def _unique(values: Sequence[Any]) -> tuple[str, ...]:
    seen: list[str] = []
    for value in values or ():
        text = _text(value)
        if text and text not in seen:
            seen.append(text)
    return tuple(seen)


__all__ = [
    "EVIDENCE_GAP_PROTOCOL_VERSION",
    "RECAPTURE_POLICY_CONTRACT_KEY",
    "EvidenceGap",
    "EvidenceGapMissing",
    "EvidenceGapTarget",
    "GapDisposition",
    "OverlayPhase",
    "RecapturePolicy",
    "assert_gap_has_no_operation_details",
    "build_evidence_gap",
    "build_evidence_request_from_gap",
    "materialize_evidence_request",
    "parse_recapture_declaration",
    "parse_recapture_policy",
]
