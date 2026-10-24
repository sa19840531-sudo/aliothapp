"""根据候选规则证据契约生成页面级采集计划。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class CapturePlan:
    """同一页面候选规则共享的最小证据需求。"""

    screenshot: bool
    dom: bool
    ui_snapshot: bool
    visible_text: bool
    temporal: bool
    interaction: bool
    scroll: bool
    page_coverage: bool
    requires_journey: bool
    session_dependency: str
    required_artifacts: tuple[str, ...]
    required_capture_roles: tuple[str, ...]
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["required_artifacts"] = list(self.required_artifacts)
        payload["required_capture_roles"] = list(self.required_capture_roles)
        payload["reasons"] = list(self.reasons)
        return payload


def build_capture_plan(
    *, page_type: str, rules: list[dict[str, Any]], page_state: str | None = None
) -> CapturePlan:
    """合并规则 input_contract，禁止无条件采集所有证据。"""
    required: set[str] = set()
    fields: set[str] = set()
    tool_refs: set[str] = set()
    roles: set[str] = set()
    requires_journey = False
    for rule in rules:
        contract = _mapping(rule.get("input_contract"))
        required.update(_strings(contract.get("required_artifacts")))
        fields.update(_strings(contract.get("required_fields")))
        roles.update(_strings(contract.get("required_capture_roles")))
        judge_config = _mapping(rule.get("judge_config"))
        tool_refs.update(_strings(judge_config.get("tool_refs")))
        capability = _mapping(judge_config.get("capability_config"))
        tool_refs.update(_strings(capability.get("deterministic_tools")))
        requires_journey = requires_journey or capability.get("requires_journey") is True

    normalized = {"dom" if item == "dom_snapshot" else item for item in required}
    temporal = bool(
        page_type == "startup_ad"
        or {"temporal_trace"}.intersection(normalized)
        or any("countdown" in item for item in tool_refs)
    )
    interaction = bool(
        "action_trace" in normalized or requires_journey or roles.difference({"primary"})
    )
    player = bool(
        "media_state" in normalized
        or any(
            marker in tool.casefold()
            for tool in tool_refs
            for marker in ("player", "playback", "media_state", "video_state")
        )
    )
    normalized_page_state = str(page_state or "").strip().casefold()
    obstructed = normalized_page_state in {"popup", "modal", "overlay", "dialog"}
    list_page = not obstructed and page_type in {
        "content_list",
        "video_list",
        "post_list",
    }
    collection_field = any(
        marker in field.casefold()
        for field in fields
        for marker in ("items", "labels", "entries", "collection")
    )
    scroll = "page_coverage" in normalized or (list_page and collection_field)
    reasons: list[str] = []
    if temporal:
        reasons.append("temporal_contract")
    if interaction:
        reasons.append("active_session_contract")
    if player:
        reasons.append("player_session_contract")
    if scroll:
        reasons.append("list_coverage_contract")
    if not reasons:
        reasons.append("static_snapshot_contract")
    return CapturePlan(
        screenshot="screenshot" in normalized,
        dom="dom" in normalized,
        ui_snapshot="ui_snapshot" in normalized,
        visible_text="visible_text" in normalized,
        temporal=temporal,
        interaction=interaction,
        scroll=scroll,
        page_coverage=scroll,
        requires_journey=requires_journey,
        session_dependency=("active_session" if temporal or interaction or player else "snapshot"),
        required_artifacts=tuple(sorted(normalized)),
        required_capture_roles=tuple(sorted(roles)),
        reasons=tuple(reasons),
    )


def derive_session_dependency(
    *, input_contract: dict[str, Any] | None, judge_config: dict[str, Any] | None
) -> str:
    """从规则契约派生会话依赖，不修改规则种子数据。"""
    rule = {
        "input_contract": input_contract or {},
        "judge_config": judge_config or {},
    }
    return build_capture_plan(page_type="", rules=[rule]).session_dependency


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _strings(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {str(item).strip() for item in value if str(item).strip()}
