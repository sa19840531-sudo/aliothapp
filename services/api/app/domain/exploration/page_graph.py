"""页面状态图的领域规范化。

页面图是 Capture/Action 事实的可重建投影，不取代既有 Capture、Action 或
Coverage 权威。这个模块只负责把不同平台的元数据收敛成稳定、可审计的
页面身份、局部状态和转场判定；无法确认时保留 ``unknown``，绝不拿
``page_type`` 冒充页面身份。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from services.api.app.domain.exploration.app_visual_page_identity import (
    validated_app_visual_page_identity,
)
from services.api.app.domain.exploration.h5_logical_surface import (
    h5_obstruction_facts_confirm_clearance,
)

PAGE_GRAPH_PROTOCOL_VERSION = "page_graph.v1"
PAGE_GRAPH_STATE_PROTOCOL_VERSION = "page_graph_state.v1"
PAGE_GRAPH_TRANSITION_PROTOCOL_VERSION = "page_graph_transition.v1"
APP_VISUAL_AFTER_PROTOCOL_VERSION = "app_visual_after.v1"
WEB_URL_AFTER_PROTOCOL_VERSION = "web_url_after.v1"

PAGE_IDENTITY_CONFIRMED = "confirmed"
PAGE_IDENTITY_INFERRED = "inferred"
PAGE_IDENTITY_UNKNOWN = "unknown"

PAGE_GRAPH_ACTION_STATUSES = frozenset(
    {
        "planned",
        "executing",
        "evidence_pending",
        "completed",
        "evidence_failed",
        "blocked",
        "failed",
        "timed_out",
        "skipped",
    }
)
PAGE_GRAPH_TERMINAL_ACTION_STATUSES = frozenset(
    {"completed", "evidence_failed", "blocked", "failed", "timed_out", "skipped"}
)
PAGE_GRAPH_RECOVERABLE_ACTION_STATUSES = frozenset({"completed"})

_OVERLAY_STATES = frozenset(
    {
        "ad",
        "advertisement",
        "dialog",
        "login_prompt",
        "modal",
        "overlay",
        "popup",
        "purchase_prompt",
        "startup_ad",
        "system_notice",
        "update_prompt",
    }
)
_EXTERNAL_PACKAGE_MARKERS = frozenset(
    {"browser", "chrome", "firefox", "edge", "com.android.browser"}
)
_UNKNOWN_TEXT = frozenset({"", "none", "null", "unknown", "未识别", "未确认"})
_APP_STABLE_IDENTITY_KEYS = (
    "business_object_id",
    "business_object_key",
    "object_id",
    "content_id",
    "item_id",
    "resource_id",
    "entity_id",
    "page_role",
    "page_responsibility",
    "return_semantics",
    "return_target_identity",
)
_STABLE_STATE_KEYS = (
    "selected_tab",
    "tab_key",
    "selected_filter",
    "filter_key",
    "sort_key",
    "pagination_cursor",
    "page_index",
    "expanded_item_id",
    "selected_item_id",
    "selected_object_id",
    "business_object_id",
    "business_object_key",
    "content_id",
    "object_id",
    "item_id",
    "resource_id",
    "entity_id",
)
_URL_LOCAL_STATE_KEYS = frozenset(
    {
        "tab",
        "filter",
        "sort",
        "order",
        "offset",
        "cursor",
        "limit",
        "expanded",
        "page_size",
    }
)


@dataclass(frozen=True, slots=True)
class PageIdentity:
    """页面身份及其可信度。"""

    key: str
    status: str
    source: str
    display_name: str | None = None


@dataclass(frozen=True, slots=True)
class WebPageGraphFacts:
    """仅由仓储认证 Web Task、冻结策略和 Capture 后注入，元数据不能自证。"""

    identity: PageIdentity
    page_state: str | None
    business_visible: bool
    is_overlay: bool
    is_external: bool
    audit: dict[str, Any]
    after_marker: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class PageStateSnapshot:
    """绑定到一次 Capture 的页面状态版本。"""

    capture_id: str
    task_id: str
    agent_run_id: str | None
    surface_kind: str
    session_id: str | None
    finalization_epoch: int | None
    command_sequence: int | None
    capture_role: str
    identity: PageIdentity
    page_type: str | None
    page_state: str | None
    state_fingerprint: str
    is_overlay: bool
    is_external: bool
    state_payload: dict[str, Any]
    captured_at: Any | None = None

    @property
    def counts_as_business_page(self) -> bool:
        """仅已确认业务身份的页面可计入到达；未知页也只保留证据。"""

        return (
            self.identity.status == PAGE_IDENTITY_CONFIRMED
            and not self.is_overlay
            and not self.is_external
            and self.state_payload.get("web_page_graph", {}).get("business_visible", True)
        )


@dataclass(frozen=True, slots=True)
class PageTransitionDecision:
    """一次动作的图边判定。"""

    status: str
    terminal: bool
    source_capture_id: str | None
    target_capture_id: str | None
    counts_as_business_arrival: bool
    replay_eligible: bool
    proof: str
    reason_codes: tuple[str, ...] = ()


def build_page_state_snapshot(
    *,
    capture_id: str,
    task_id: str,
    agent_run_id: str | None,
    surface_kind: str,
    session_id: str | None,
    finalization_epoch: int | None,
    command_sequence: int | None,
    capture_role: str,
    metadata: Mapping[str, Any] | None = None,
    page_type: str | None = None,
    page_state: str | None = None,
    captured_at: Any | None = None,
    web_facts: WebPageGraphFacts | None = None,
) -> PageStateSnapshot:
    """从 Capture 元数据构造一个确定性的页面状态版本。"""

    normalized_metadata = _mapping(metadata)
    if page_state is not None:
        normalized_metadata["observed_page_state"] = page_state
    identity = web_facts.identity if web_facts is not None else normalize_page_identity(
        metadata=normalized_metadata,
        surface_kind=surface_kind,
        capture_id=capture_id,
    )
    normalized_state = normalize_local_state(
        page_state=web_facts.page_state if web_facts is not None else page_state,
        metadata=normalized_metadata,
    )
    if str(session_id or "").startswith("h5:") and normalized_state in _OVERLAY_STATES:
        detection = _mapping(normalized_metadata.get("page_detection"))
        facts = normalized_metadata.get("obstruction_facts", detection.get("obstruction_facts"))
        logical = _mapping(
            normalized_metadata.get("logical_surface") or detection.get("logical_surface")
        )
        if (
            h5_obstruction_facts_confirm_clearance(facts)
            and _mapping(logical.get("state_signals")).get("obstruction_count") == "0"
        ):
            normalized_state = "stable"
    state_payload = _state_payload(
        metadata=normalized_metadata,
        page_state=normalized_state,
        identity=identity,
    )
    fingerprint_payload = _state_fingerprint_payload(
        metadata=normalized_metadata,
        page_state=normalized_state,
    )
    is_overlay = _is_overlay_state(normalized_state, normalized_metadata)
    if str(session_id or "").startswith("h5:"):
        logical = _mapping(state_payload.get("logical_surface"))
        if (
            identity.status == PAGE_IDENTITY_CONFIRMED
            and logical.get("protocol_version") == "logical_surface.v1"
            and _mapping(logical.get("state_signals")).get("obstruction_count")
            in {"1", "2-5", "6-20", "20+"}
        ):
            # Before 尚未执行页型判定，不能用 loaded 掩盖本帧已确认的遮挡。
            is_overlay = True
    is_external = _is_external_surface(normalized_metadata)
    if web_facts is not None:
        state_payload["web_page_graph"] = {
            **web_facts.audit, "business_visible": web_facts.business_visible,
        }
        state_payload.pop("trusted_url_identity", None)
        if web_facts.after_marker is not None:
            state_payload["trusted_url_identity"] = web_facts.after_marker
        is_overlay = web_facts.is_overlay
        is_external = web_facts.is_external
    return PageStateSnapshot(
        capture_id=str(capture_id),
        task_id=str(task_id),
        agent_run_id=_optional_text(agent_run_id),
        surface_kind=str(surface_kind),
        session_id=_optional_text(session_id),
        finalization_epoch=_optional_nonnegative(finalization_epoch),
        command_sequence=_optional_nonnegative(command_sequence),
        capture_role=str(capture_role or "observation"),
        identity=identity,
        page_type=_optional_text(page_type),
        page_state=normalized_state,
        state_fingerprint=stable_fingerprint(fingerprint_payload),
        is_overlay=is_overlay,
        is_external=is_external,
        state_payload=state_payload,
        captured_at=captured_at,
    )


def normalize_page_identity(
    *,
    metadata: Mapping[str, Any] | None,
    surface_kind: str,
    capture_id: str,
) -> PageIdentity:
    """提取页面身份；不使用页面类型制造身份。"""

    values = _mapping(metadata)
    nested_surface = _mapping(values.get("surface_facts"))
    detection = _mapping(values.get("page_detection"))
    nested_logical = _mapping(values.get("logical_surface"))
    if not nested_logical:
        nested_logical = _mapping(detection.get("logical_surface"))
    candidates = (
        ("business_page_identity", values.get("business_page_identity"), "business"),
        ("page_identity", values.get("page_identity"), "page_identity"),
        (
            "logical_page_identity",
            values.get("logical_page_identity"),
            "logical_page_identity",
        ),
        (
            "business_page_identity",
            nested_surface.get("business_page_identity"),
            "surface_facts",
        ),
        (
            "business_page_identity",
            nested_logical.get("business_page_identity"),
            "logical_surface",
        ),
        (
            "business_page_identity",
            nested_surface.get("app_page_identity"),
            "app_visual_identity",
        ),
    )
    for _field, value, source in candidates:
        # 通用业务身份允许携带显式可信度；App 视觉合同走下方专用解析，
        # 避免把标题或未确认的视觉推断提前当成 confirmed。
        if source == "app_visual_identity" and isinstance(value, Mapping):
            continue
        normalized, declared_status = _identity_candidate(value)
        if normalized is not None:
            identity_status = _identity_status(
                declared_status,
                values=values,
                nested_surface=nested_surface,
                nested_logical=nested_logical,
            )
            if identity_status == PAGE_IDENTITY_UNKNOWN:
                continue
            if str(surface_kind).casefold() in {"android", "ios"}:
                normalized = _augment_app_identity(
                    normalized,
                    values=values,
                    nested_surface=nested_surface,
                    nested_logical=nested_logical,
                )
            return PageIdentity(
                key=f"identity:{normalized}",
                status=identity_status,
                source=source,
                display_name=normalized,
            )

    identity_fingerprint = _logical_identity_fingerprint(
        values.get("identity_fingerprint") or nested_logical.get("identity_fingerprint")
    )
    if identity_fingerprint is not None and _logical_identity_is_effective(nested_logical):
        return PageIdentity(
            key=f"logical:{identity_fingerprint}",
            status=PAGE_IDENTITY_CONFIRMED,
            source="logical_surface",
        )

    url_identity = _trusted_url_identity(values, capture_id=capture_id)
    if url_identity is not None and str(surface_kind).casefold() == "web":
        return url_identity

    app_identity = _trusted_app_visual_identity(
        values,
        surface_kind=surface_kind,
        capture_id=capture_id,
    )
    if app_identity is not None:
        return app_identity

    # URL、包名和 Activity 只能证明运行时位置，不能证明业务页面身份；
    # 不能用它们把分页、详情、Tab 或同 Activity 的不同页面错误合并。
    # Capture ID 只用于保持未知节点可追溯；它不是伪造的业务身份。
    return PageIdentity(
        key=f"unknown:{str(capture_id)}",
        status=PAGE_IDENTITY_UNKNOWN,
        source="unconfirmed",
    )


def normalize_local_state(
    *,
    page_state: Any,
    metadata: Mapping[str, Any] | None,
) -> str | None:
    """把页面局部状态归一化，不把弹窗身份混入页面身份。"""

    values = _mapping(metadata)
    nested_surface = _mapping(values.get("surface_facts"))
    nested_logical = _mapping(values.get("logical_surface"))
    if not nested_logical:
        nested_logical = _mapping(_mapping(values.get("page_detection")).get("logical_surface"))
    raw = (
        page_state
        or values.get("local_state")
        or values.get("state")
        or nested_surface.get("page_state")
        or nested_logical.get("state_fingerprint")
    )
    if isinstance(raw, Mapping):
        return stable_json(raw)
    text = _optional_text(raw)
    if text is None:
        return None
    compact = re.sub(r"\s+", "_", text.casefold())
    return compact[:256]


def build_transition_decision(
    *,
    action_status: str,
    source: PageStateSnapshot | None,
    target: PageStateSnapshot | None,
    result_payload: Mapping[str, Any] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> PageTransitionDecision:
    """为查询和恢复统一判定边状态。

    没有 After Capture 的失败/阻断动作仍保留终态边；只有 completed 且具备
    目标 Capture 的低风险动作才允许作为恢复候选。"""

    status = str(action_status or "unknown").casefold()
    if status not in PAGE_GRAPH_ACTION_STATUSES:
        status = "unknown"
    terminal = status in PAGE_GRAPH_TERMINAL_ACTION_STATUSES
    reasons: list[str] = []
    proof = "insufficient"
    if source is None:
        reasons.append("source_state_missing")
    if target is None:
        reasons.append("target_state_missing")
    if source is not None and target is not None:
        if source.task_id != target.task_id:
            reasons.append("cross_task_capture_pair")
        if source.agent_run_id != target.agent_run_id:
            reasons.append("cross_run_capture_pair")
        if source.finalization_epoch != target.finalization_epoch:
            reasons.append("cross_epoch_capture_pair")
        if source.session_id != target.session_id or source.session_id is None:
            reasons.append("cross_session_capture_pair")
        if source.capture_id == target.capture_id:
            reasons.append("same_capture")
    payload = _with_app_state_diff(
        _mapping(result_payload),
        source=source,
        target=target,
    )
    state_diff = _mapping(payload.get("state_diff"))
    transition = _mapping(
        payload.get("logical_surface_transition") or state_diff.get("logical_surface_transition")
    )
    logical_proof = _trusted_logical_transition(
        transition,
        source=source,
        target=target,
    )
    app_proof = _trusted_app_after_transition(
        source=source,
        target=target,
        action_status=status,
        result_payload=payload,
    )
    url_proof = _trusted_url_after_transition(
        source=source,
        target=target,
        action_status=status,
        result_payload=payload,
    )
    if logical_proof or app_proof or url_proof:
        proof = "trusted"
        if logical_proof:
            reasons.append("logical_surface_after_verified")
        if app_proof:
            reasons.append("app_visual_after_verified")
        if url_proof:
            reasons.append("web_url_after_verified")
    elif transition:
        reasons.append("transition_proof_untrusted_or_mismatched")
        if app_proof is False and source is not None and target is not None:
            reasons.append("after_capture_contract_missing_or_untrusted")
    else:
        reasons.append("transition_proof_missing")
    if source is not None and source.identity.status != PAGE_IDENTITY_CONFIRMED:
        reasons.append("source_identity_unconfirmed")
    if target is not None and target.identity.status != PAGE_IDENTITY_CONFIRMED:
        reasons.append("target_identity_unconfirmed")
    if source is not None and (source.is_overlay or source.is_external):
        reasons.append("source_not_business_page")
    if target is not None and target.is_overlay:
        reasons.append("target_overlay")
    if target is not None and target.is_external:
        reasons.append("external_navigation")
    if error_code:
        reasons.append(f"error:{str(error_code)[:128]}")
    if error_message and not error_code:
        reasons.append("error_message_present")
    verified_business_transition = bool(
        status == "completed"
        and not error_code
        and not error_message
        and source is not None
        and target is not None
        and source.task_id == target.task_id
        and source.agent_run_id == target.agent_run_id
        and source.finalization_epoch == target.finalization_epoch
        and source.finalization_epoch is not None
        and source.session_id is not None
        and source.session_id == target.session_id
        and source.identity.status == PAGE_IDENTITY_CONFIRMED
        and target.identity.status == PAGE_IDENTITY_CONFIRMED
        and source.counts_as_business_page
        and target.counts_as_business_page
        and proof == "trusted"
        and not any(
            code in reasons
            for code in (
                "same_capture",
                "cross_task_capture_pair",
                "cross_run_capture_pair",
                "cross_epoch_capture_pair",
                "cross_session_capture_pair",
                "source_not_business_page",
                "target_overlay",
                "external_navigation",
            )
        )
    )
    counts_as_business = bool(
        verified_business_transition
        and source is not None
        and target is not None
        and _identities_are_distinct_business_pages(source, target)
    )
    # Action 的意图风险会在仓库层额外限制；这里先只给出可审计的基础资格。
    replay_eligible = verified_business_transition
    return PageTransitionDecision(
        status=status,
        terminal=terminal,
        source_capture_id=source.capture_id if source is not None else None,
        target_capture_id=target.capture_id if target is not None else None,
        counts_as_business_arrival=counts_as_business,
        replay_eligible=replay_eligible,
        proof=proof,
        reason_codes=tuple(dict.fromkeys(reasons)),
    )


def _trusted_app_after_transition(
    *,
    source: PageStateSnapshot | None,
    target: PageStateSnapshot | None,
    action_status: str,
    result_payload: Mapping[str, Any],
) -> bool:
    """验证 App 的视觉 After 合同，不把动作成功或页型变化当成到达。"""

    if (
        source is None
        or target is None
        or action_status != "completed"
        or source.surface_kind not in {"android", "ios"}
        or target.surface_kind != source.surface_kind
        or target.capture_role != "after_action"
        or source.identity.status != PAGE_IDENTITY_CONFIRMED
        or target.identity.status != PAGE_IDENTITY_CONFIRMED
    ):
        return False
    proof = _mapping(target.state_payload.get("app_visual_after"))
    state_diff = _mapping(result_payload.get("state_diff"))
    return bool(
        proof.get("protocol_version") == APP_VISUAL_AFTER_PROTOCOL_VERSION
        and proof.get("verified") is True
        and proof.get("verified_by") == "app_visual_observation"
        and proof.get("capture_id") == target.capture_id
        and proof.get("source_capture_id") == source.capture_id
        and proof.get("result_capture_id") == target.capture_id
        and proof.get("after_state_verified") is True
        # 图只证明实际观察到的业务到达或局部状态变化，不替操作意图宣告
        # 成功。图像、页型、加载状态变化均不能单独提供这一证明。
        and state_diff.get("observed_business_transition") is True
        and state_diff.get("postcondition_complete") is not False
        and state_diff.get("visual_action_committed") is not False
    )


def _with_app_state_diff(
    payload: Mapping[str, Any],
    *,
    source: PageStateSnapshot | None,
    target: PageStateSnapshot | None,
) -> dict[str, Any]:
    """补充真实观察差异，绝不从像素变化推导意图完成或命令执行结果。"""

    if (
        source is None
        or target is None
        or source.surface_kind not in {"android", "ios"}
        or target.surface_kind != source.surface_kind
    ):
        return dict(payload)
    derived = _app_state_diff_from_snapshots(source, target)
    if not derived:
        return dict(payload)
    result = dict(payload)
    existing = _mapping(result.get("state_diff"))
    result["state_diff"] = {**derived, **existing}
    return result


def _app_state_diff_from_snapshots(
    source: PageStateSnapshot,
    target: PageStateSnapshot,
) -> dict[str, Any]:
    """根据 App Capture/视觉事实的稳定字段生成可复核状态差异。"""

    before = source.state_payload
    after = target.state_payload
    before_image = _text_value(before.get("image_signature"))
    after_image = _text_value(after.get("image_signature"))
    before_package = _text_value(before.get("package_name"))
    after_package = _text_value(after.get("package_name"))
    before_activity = _text_value(before.get("activity"))
    after_activity = _text_value(after.get("activity"))
    before_type = _text_value(source.page_type or before.get("visual_page_type"))
    after_type = _text_value(target.page_type or after.get("visual_page_type"))
    diff = {
        "image_changed": bool(
            before_image is not None and after_image is not None and before_image != after_image
        ),
        "package_changed": bool(
            before_package is not None
            and after_package is not None
            and before_package != after_package
        ),
        "activity_changed": bool(
            before_activity is not None
            and after_activity is not None
            and before_activity != after_activity
        ),
        "page_type_changed": bool(
            before_type is not None and after_type is not None and before_type != after_type
        ),
        "local_state_changed": source.state_fingerprint != target.state_fingerprint,
    }
    diff["business_identity_changed"] = source.identity.key != target.identity.key
    before_local = _mapping(before.get("local_state_facts"))
    after_local = _mapping(after.get("local_state_facts"))
    diff["observed_business_transition"] = bool(
        source.identity.status == PAGE_IDENTITY_CONFIRMED
        and target.identity.status == PAGE_IDENTITY_CONFIRMED
        and (
            diff["business_identity_changed"]
            or stable_fingerprint(before_local) != stable_fingerprint(after_local)
        )
    )
    return diff


def _trusted_url_after_transition(
    *,
    source: PageStateSnapshot | None,
    target: PageStateSnapshot | None,
    action_status: str,
    result_payload: Mapping[str, Any],
) -> bool:
    """验证普通 H5 URL 的真实 Before/After Capture，不依赖 logical_surface。"""

    if (
        source is None
        or target is None
        or action_status != "completed"
        or source.surface_kind != "web"
        or target.surface_kind != "web"
        or source.identity.status != PAGE_IDENTITY_CONFIRMED
        or target.identity.status != PAGE_IDENTITY_CONFIRMED
        or source.identity.source not in {"trusted_url_after", "trusted_url_observation"}
        or target.identity.source not in {"trusted_url_after", "trusted_url_observation"}
    ):
        return False
    state_diff = _mapping(result_payload.get("state_diff"))
    source_url = _text_value(source.state_payload.get("url"))
    target_url = _text_value(target.state_payload.get("url"))
    source_marker = _mapping(source.state_payload.get("trusted_url_identity"))
    target_marker = _mapping(target.state_payload.get("trusted_url_identity"))
    return bool(
        state_diff.get("location_changed") is True
        and source_url is not None
        and target_url is not None
        and (
            source.identity.key != target.identity.key
            or source.state_fingerprint != target.state_fingerprint
        )
        and source_marker.get("protocol_version") == WEB_URL_AFTER_PROTOCOL_VERSION
        and target_marker.get("protocol_version") == WEB_URL_AFTER_PROTOCOL_VERSION
        and source_marker.get("verified") is True
        and target_marker.get("verified") is True
        and target_marker.get("capture_id") == target.capture_id
        and target_marker.get("source_capture_id") in {None, source.capture_id}
    )


def build_app_visual_after_proof(
    *,
    capture_id: str,
    source_capture_id: str | None,
    capture_role: str,
    page_type: str | None,
    page_state: str | None,
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """把已封存 App 视觉事实收敛成可审计的 After 后置合同。"""

    values = _mapping(metadata)
    facts = _mapping(values.get("surface_facts"))
    identity = normalize_page_identity(
        metadata=values,
        surface_kind=str(values.get("surface_kind") or "android"),
        capture_id=capture_id,
    )
    provider_quality = _mapping(facts.get("visual_provider_quality"))
    confidence = _bounded_confidence(
        facts.get("visual_page_type_confidence")
        if facts.get("visual_page_type_confidence") is not None
        else facts.get("page_type_confidence")
    )
    blocked_reason_codes: list[str] = []
    if capture_role != "after_action":
        blocked_reason_codes.append("capture_not_after_action")
    if _optional_text(page_type) is None:
        blocked_reason_codes.append("page_type_missing")
    if _optional_text(page_state) != "stable":
        blocked_reason_codes.append("page_state_not_stable")
    if confidence < 0.70:
        blocked_reason_codes.append("page_type_confidence_low")
    if provider_quality.get("status") == "degraded":
        blocked_reason_codes.append("visual_provider_degraded")
    if identity.status != PAGE_IDENTITY_CONFIRMED:
        blocked_reason_codes.append("business_identity_unconfirmed")
    if _is_overlay_state(page_state, values):
        blocked_reason_codes.append("target_overlay")
    verified = not blocked_reason_codes
    return {
        "protocol_version": APP_VISUAL_AFTER_PROTOCOL_VERSION,
        "verified": verified,
        "verified_by": "app_visual_observation",
        "capture_id": capture_id,
        "source_capture_id": source_capture_id,
        "result_capture_id": capture_id,
        "after_state_verified": verified,
        "identity_key": identity.key,
        "confidence": confidence,
        "reason_codes": blocked_reason_codes,
    }


def _identities_are_distinct_business_pages(
    source: PageStateSnapshot,
    target: PageStateSnapshot,
) -> bool:
    """同一页面的 Tab/分页/局部变化仍记录边，但不伪造新业务到达。"""

    return source.identity.key != target.identity.key


def _trusted_logical_transition(
    transition: Mapping[str, Any],
    *,
    source: PageStateSnapshot | None,
    target: PageStateSnapshot | None,
) -> bool:
    """验证 Runtime 产出的 after 页面后置条件，而不是相信动作状态。"""

    if source is None or target is None:
        return False
    if transition.get("protocol_version") != "logical_surface_transition.v1":
        return False
    if transition.get("logical_transition_proof") != "trusted":
        return False
    kind = transition.get("transition_kind")
    if kind not in {"logical_page_changed", "surface_state_changed"}:
        return False
    before_identity = _logical_identity_fingerprint(transition.get("before_identity_fingerprint"))
    after_identity = _logical_identity_fingerprint(transition.get("after_identity_fingerprint"))
    if before_identity is None or after_identity is None:
        return False
    identities_match = bool(
        source.identity.key == f"logical:{before_identity}"
        and target.identity.key == f"logical:{after_identity}"
    )
    if kind == "logical_page_changed":
        return identities_match and before_identity != after_identity
    before_state = _logical_identity_fingerprint(transition.get("before_state_fingerprint"))
    after_state = _logical_identity_fingerprint(transition.get("after_state_fingerprint"))
    if (
        identities_match
        and before_identity != after_identity
        and str(source.session_id or "").startswith("h5:")
        and source.session_id == target.session_id
        and transition.get("reason_code") == "trusted_obstruction_clearance"
    ):
        from services.api.app.domain.exploration.h5_logical_surface import (
            compare_h5_logical_surfaces,
        )

        expected = compare_h5_logical_surfaces(
            before=_mapping(source.state_payload.get("logical_surface")),
            after=_mapping(target.state_payload.get("logical_surface")),
        )
        return bool(
            expected.get("reason_code") == "trusted_obstruction_clearance"
            and expected.get("logical_transition_proof") == "trusted"
            and all(transition.get(key) == value for key, value in expected.items())
        )
    return bool(
        identities_match
        and before_identity == after_identity
        and before_state is not None
        and after_state is not None
        and before_state != after_state
        and _snapshot_logical_state(source) == before_state
        and _snapshot_logical_state(target) == after_state
    )


def _snapshot_logical_state(snapshot: PageStateSnapshot) -> str | None:
    payload = snapshot.state_payload
    logical = _mapping(payload.get("logical_surface")) or _mapping(
        _mapping(payload.get("page_detection")).get("logical_surface")
    )
    return _logical_identity_fingerprint(logical.get("state_fingerprint"))


def stable_fingerprint(value: Any) -> str:
    """返回稳定 SHA-256 指纹。"""

    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def stable_json(value: Any) -> str:
    """生成跨进程稳定的 JSON 文本。"""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def canonical_url_identity(value: Any) -> str | None:
    """保留 H5 路由 query/hash 的完整 URL 身份。"""

    text = _optional_text(value)
    if text is None:
        return None
    try:
        parsed = urlsplit(text)
        hostname = parsed.hostname
        if not parsed.scheme or not hostname:
            return None
        port = parsed.port
    except ValueError:
        return None
    host = hostname.casefold()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parsed.username is not None or parsed.password is not None:
        return None
    default_port = (parsed.scheme.casefold() == "http" and port == 80) or (
        parsed.scheme.casefold() == "https" and port == 443
    )
    netloc = host if port is None or default_port else f"{host}:{port}"
    scheme = parsed.scheme.casefold()
    path = parsed.path or "/"
    return urlunsplit((scheme, netloc, path, parsed.query, parsed.fragment))


def _state_payload(
    *,
    metadata: Mapping[str, Any],
    page_state: str | None,
    identity: PageIdentity,
) -> dict[str, Any]:
    """只留与局部状态有关的可审计字段，避免把整份正文写入图。"""

    payload: dict[str, Any] = {
        "protocol_version": PAGE_GRAPH_STATE_PROTOCOL_VERSION,
        "page_state": page_state,
        "identity_key": identity.key,
        "identity_status": identity.status,
        "local_state_facts": _state_fingerprint_payload(metadata=metadata, page_state=None),
    }
    for key in (
        "state_fingerprint",
        "detection_status",
        "detection_confidence",
        "overlay_signature",
        "popup_visible",
        "is_overlay",
        "overlay_kind",
        "external_navigation",
        "external_url",
        "image_signature",
        "package_name",
        "activity",
        "url",
        "final_url",
        "business_page_scope",
        "trusted_url_identity",
        "app_visual_after",
        "web_evidence_admission",
        "page_graph_identity_lineage",
    ):
        if key in metadata:
            payload[key] = metadata[key]
    logical_surface = _mapping(metadata.get("logical_surface"))
    if not logical_surface:
        logical_surface = _mapping(_mapping(metadata.get("page_detection")).get("logical_surface"))
    if logical_surface:
        payload["logical_surface"] = logical_surface
    return payload


def _state_fingerprint_payload(
    *,
    metadata: Mapping[str, Any],
    page_state: str | None,
) -> dict[str, Any]:
    """只用稳定业务状态计算指纹，证据来源留在 ``state_payload``。

    URL、截图摘要、识别置信度、覆盖层标记和 After 合同都属于本次
    观察的证据，不是页面局部状态。把它们放进指纹会让同一页面每次
    重新识别都产生新状态，也会让页型改判改变页面图拓扑。
    """

    values = _mapping(metadata)
    nested_surface = _mapping(values.get("surface_facts"))
    nested_logical = _mapping(values.get("logical_surface"))
    if not nested_logical:
        nested_logical = _mapping(_mapping(values.get("page_detection")).get("logical_surface"))
    payload: dict[str, Any] = {
        "protocol_version": PAGE_GRAPH_STATE_PROTOCOL_VERSION,
        "page_state": page_state,
    }
    raw_local_state = (
        values.get("local_state") if values.get("local_state") is not None else values.get("state")
    )
    if raw_local_state is None:
        raw_local_state = nested_surface.get("local_state")
    if raw_local_state is not None:
        payload["local_state"] = _stable_state_value(raw_local_state)
    for source_name, source in (
        ("metadata", values),
        ("surface_facts", nested_surface),
    ):
        stable_variables = _mapping(source.get("state_variables"))
        if stable_variables:
            payload[f"{source_name}_state_variables"] = _stable_state_value(stable_variables)
        stable_business_state = _mapping(source.get("business_state"))
        if stable_business_state:
            payload[f"{source_name}_business_state"] = _stable_state_value(stable_business_state)
        for key in _STABLE_STATE_KEYS:
            if key in source and (normalized := _stable_state_value(source[key])) is not None:
                payload[key] = normalized
    logical_state_fingerprint = _logical_identity_fingerprint(
        nested_logical.get("state_fingerprint")
    )
    if logical_state_fingerprint is not None:
        payload["logical_state_fingerprint"] = logical_state_fingerprint
    url = canonical_url_identity(values.get("url") or values.get("final_url"))
    if url is not None:
        _, url_state = _business_url_and_state(url)
        if url_state:
            payload["url_state"] = url_state
    return payload


def _stable_state_value(value: Any) -> Any:
    """限制局部业务变量的形状，避免把 DOM/截图等证据塞进指纹。"""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key in sorted(value, key=lambda item: str(item)):
            normalized = _stable_state_value(value[key])
            if normalized is not None:
                result[str(key)] = normalized
        return result
    if isinstance(value, (list, tuple)):
        return [_stable_state_value(item) for item in value]
    return str(value)


def _is_overlay_state(state: str | None, metadata: Mapping[str, Any]) -> bool:
    if state is not None and state in _OVERLAY_STATES:
        return True
    nested_surface = _mapping(metadata.get("surface_facts"))
    nested_logical = _mapping(metadata.get("logical_surface"))
    return bool(
        metadata.get("is_overlay") is True
        or metadata.get("popup_visible") is True
        or metadata.get("overlay_signature")
        or metadata.get("overlay_kind")
        or nested_surface.get("popup_visible") is True
        or nested_surface.get("overlay_signature")
        or nested_logical.get("overlay_signature")
    )


def _is_external_surface(metadata: Mapping[str, Any]) -> bool:
    if metadata.get("is_external") is True or metadata.get("external_navigation") is True:
        return True
    package_name = str(metadata.get("package_name") or "").casefold()
    return any(marker in package_name for marker in _EXTERNAL_PACKAGE_MARKERS)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return None if text.casefold() in _UNKNOWN_TEXT else text


def _identity_text(value: Any) -> str | None:
    if isinstance(value, (Mapping, list, tuple, set)):
        return None
    text = _optional_text(value)
    if text is None:
        return None
    return re.sub(r"\s+", " ", text)[:512]


def _trusted_url_identity(
    values: Mapping[str, Any],
    *,
    capture_id: str,
) -> PageIdentity | None:
    marker = _mapping(values.get("trusted_url_identity"))
    detection = _mapping(values.get("page_detection"))
    url = canonical_url_identity(values.get("url"))
    if (
        url is not None
        and values.get("reachable") is True
        and values.get("observed_page_state") in {"stable", "normal", "loaded", "content_available"}
        and detection.get("detection_status") == "recognized"
        and _bounded_confidence(detection.get("confidence")) >= 0.70
    ):
        return PageIdentity(
            key=f"url:{_business_url_and_state(url)[0]}",
            status=PAGE_IDENTITY_CONFIRMED,
            source="trusted_url_observation",
        )
    if (
        marker.get("protocol_version") != WEB_URL_AFTER_PROTOCOL_VERSION
        or marker.get("verified") is not True
        or marker.get("verified_by") != "action_after_capture"
        or marker.get("capture_id") != capture_id
    ):
        return None
    canonical = canonical_url_identity(marker.get("url") or values.get("url"))
    if canonical is None:
        return None
    return PageIdentity(
        key=f"url:{_business_url_and_state(canonical)[0]}",
        status=PAGE_IDENTITY_CONFIRMED,
        source="trusted_url_after",
    )


def _business_url_and_state(url: str) -> tuple[str, dict[str, Any]]:
    """把常见局部查询参数与页内锚点留作状态，保留对象和路由身份。

    page 仅在数值分页时按状态处理，page=detail 等页面选择仍属于路由。
    hash 路由保留路径；未知参数保留在身份中，避免误合并不同业务对象。
    """
    parsed = urlsplit(url)
    state: dict[str, Any] = {}

    def split_query(query: str, location: str) -> str:
        business, local = [], []
        for key, value in parse_qsl(query, keep_blank_values=True):
            if key.casefold() in _URL_LOCAL_STATE_KEYS or (
                key.casefold() == "page" and value.isdecimal()
            ):
                local.append((key, value))
            else:
                business.append((key, value))
        if local:
            state[location] = local
        return urlencode(business)

    query = split_query(parsed.query, "query")
    fragment = parsed.fragment
    if fragment.startswith(("/", "!/")):
        route = urlsplit(fragment.removeprefix("!"))
        fragment = ("!" if fragment.startswith("!") else "") + urlunsplit(
            ("", "", route.path, split_query(route.query, "hash_query"), route.fragment)
        )
    elif fragment and "=" not in fragment and "?" not in fragment:
        state["anchor"] = fragment
        fragment = ""
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, fragment)), state


def _trusted_app_visual_identity(
    values: Mapping[str, Any],
    *,
    surface_kind: str,
    capture_id: str,
) -> PageIdentity | None:
    if str(surface_kind).casefold() not in {"android", "ios"}:
        return None
    facts = _mapping(values.get("surface_facts"))
    package_name = _identity_text(values.get("package_name") or facts.get("package_name"))
    activity = _identity_text(values.get("activity") or facts.get("activity"))
    if package_name is None or activity is None:
        return None

    explicit = _mapping(values.get("app_page_identity"))
    if not explicit:
        explicit = _mapping(facts.get("app_page_identity"))
    validated = validated_app_visual_page_identity(
        explicit,
        capture_id=capture_id,
        package_name=package_name,
        activity=activity,
        image_signature=_optional_text(values.get("image_signature")),
        screenshot_artifact_id=_optional_text(values.get("screenshot_artifact_id")),
    )
    if validated is not None:
        return PageIdentity(
            key=f"app:{package_name.casefold()}|{activity.casefold()}|{validated['identity']}",
            status=PAGE_IDENTITY_CONFIRMED,
            source="app_visual_identity",
            display_name=str(validated["observation"]["location"]["text"]),
        )

    # 视觉标题、Tab 文案和底栏选中项只能帮助识别页型/导航入口，不能
    # 单独确认业务页身份。真正的页面身份必须由 app_page_identity 或
    # business_page_identity 合同提供，并可附带对象、职责或返回语义。
    return None


def _augment_app_identity(
    identity: str,
    *,
    values: Mapping[str, Any],
    nested_surface: Mapping[str, Any],
    nested_logical: Mapping[str, Any],
) -> str:
    """给已有 App 业务身份补充稳定对象/职责维度，不读取标题或页型。"""

    suffix = _app_identity_component_suffix(
        values,
        nested_surface,
        nested_logical,
    )
    return f"{identity}{suffix}"


def _app_identity_component_suffix(*sources: Mapping[str, Any]) -> str:
    """提取可复现的 App 业务对象、页面职责和返回语义。"""

    components: dict[str, str] = {}
    for source in sources:
        for key in _APP_STABLE_IDENTITY_KEYS:
            if key in components:
                continue
            value = source.get(key)
            if isinstance(value, Mapping):
                value = value.get("id") or value.get("key") or value.get("identity")
            normalized = _identity_component_text(value)
            if normalized is not None:
                components[key] = normalized
        business_object = _mapping(source.get("business_object"))
        for key in _APP_STABLE_IDENTITY_KEYS:
            if key in components:
                continue
            normalized = _identity_component_text(business_object.get(key))
            if normalized is not None:
                components[key] = normalized
    return "".join(
        f"|{key}:{components[key]}" for key in _APP_STABLE_IDENTITY_KEYS if key in components
    )


def _identity_component_text(value: Any) -> str | None:
    if isinstance(value, (Mapping, list, tuple, set, bool)):
        return None
    text = _optional_text(value)
    if text is None:
        return None
    return re.sub(r"\s+", "_", text.casefold())[:128]


def _identity_candidate(value: Any) -> tuple[str | None, Any | None]:
    """读取身份字符串及其可选可信度，不把任意映射转成身份。"""

    if isinstance(value, Mapping):
        candidate = (
            value.get("identity")
            or value.get("key")
            or value.get("business_page_identity")
            or value.get("page_identity")
        )
        return _identity_text(candidate), value.get("status") or value.get("identity_status")
    return _identity_text(value), None


def _identity_status(
    declared: Any,
    *,
    values: Mapping[str, Any],
    nested_surface: Mapping[str, Any],
    nested_logical: Mapping[str, Any],
) -> str:
    """收敛显式身份可信度；缺少声明的正式业务合同默认为 confirmed。"""

    candidate = declared
    if candidate is None:
        for source in (values, nested_surface, nested_logical):
            for key in (
                "identity_status",
                "page_identity_status",
                "business_page_identity_status",
            ):
                if source.get(key) is not None:
                    candidate = source[key]
                    break
            if candidate is not None:
                break
    normalized = _optional_text(candidate)
    if normalized in {
        PAGE_IDENTITY_CONFIRMED,
        PAGE_IDENTITY_INFERRED,
        PAGE_IDENTITY_UNKNOWN,
    }:
        return normalized
    return PAGE_IDENTITY_CONFIRMED


def _app_visual_identity(
    *,
    package_name: str,
    activity: str,
    page_name: str,
    source: str,
) -> PageIdentity:
    normalized_name = re.sub(r"\s+", " ", page_name).strip()[:256]
    return PageIdentity(
        key=(f"app:{package_name.casefold()}|{activity.casefold()}|{normalized_name.casefold()}"),
        status=PAGE_IDENTITY_CONFIRMED,
        source=source,
        display_name=normalized_name,
    )


def _bounded_confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return min(max(float(value), 0.0), 1.0)


def _text_value(value: Any) -> str | None:
    return _optional_text(value)


def _logical_identity_fingerprint(value: Any) -> str | None:
    text = _optional_text(value)
    if text is None or not text.startswith("sha256:") or len(text) != 71:
        return None
    try:
        int(text.removeprefix("sha256:"), 16)
    except ValueError:
        return None
    return text


def _logical_identity_is_effective(value: Mapping[str, Any]) -> bool:
    """仅接受已确认逻辑渲染与高质量身份事实。"""

    if value.get("protocol_version") != "logical_surface.v1":
        return False
    if value.get("shadow") is True:
        return False
    profile = _mapping(value.get("render_profile"))
    return bool(
        profile.get("status") == "confirmed"
        and profile.get("kind") in {"dom_document", "dom_spa", "flutter_web", "hybrid"}
        and value.get("evidence_quality") == "high"
    )


def _optional_nonnegative(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        integer = int(value)
    except (TypeError, ValueError):
        return None
    return integer if integer >= 0 else None
