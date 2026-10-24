"""按冻结能力声明检查 App 业务动作材料；只控制发布时机，不判业务成功。"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from services.api.app.application.evidence_presentation import derive_app_presentation
from services.api.app.application.rule_capability_facts import latest_ui_snapshot

_SCOPE_FIELDS = ("task_id", "agent_run_id", "session_id", "finalization_epoch")
_RETURN_TOP_LABELS = frozenset({"返回顶部", "回到顶部", "置顶", "back to top"})


@dataclass(frozen=True)
class AppRuleActionReadiness:
    """就绪只表示可交 Judge 的最低材料，不证明到底或业务通过。"""

    ready: bool
    requirement: str | None = None
    missing_evidence: tuple[str, ...] = ()

    def to_exploration_requirement(self) -> dict[str, object] | None:
        if self.requirement is None:
            return None
        return {
            "requirement": self.requirement,
            "missing_evidence": list(self.missing_evidence),
        }


def app_rule_action_requirement(rule: Mapping[str, Any]) -> str | None:
    """只解释可由现有 group、工具和阈值确定的行为，不解析规则编号或自由文案。"""
    config = _mapping(rule.get("judge_config"))
    capability = _mapping(config.get("capability_config"))
    if capability.get("requires_journey") is not True:
        return None
    tools = set(capability.get("deterministic_tools") or ())
    thresholds = _mapping(config.get("thresholds"))
    thresholds.update(_mapping(capability.get("thresholds")))
    group = capability.get("batch_group")
    if group == "list-scroll":
        if _number(thresholds.get("back_to_top_min_scroll_viewports")) is not None:
            return "list_scroll_return_to_top"
        if (
            "layout_geometry_assertion_handler" in tools
            and _number(thresholds.get("max_bottom_blank_viewport_ratio")) is not None
        ):
            return "list_bottom_observation"
        if "interaction_state_diff_tool" in tools:
            return "list_scroll_interaction"
    if group == "visual-nav" and tools.intersection(
        {"state_snapshot_tool", "interaction_state_diff_tool"}
    ):
        return "navigation_tab_comparison"
    if group == "cover-content" and "visual_semantic_compare_tool" in tools:
        return "cover_content_same_object"
    return None


def assess_app_rule_action_readiness(
    rule: Mapping[str, Any],
    *,
    primary_capture_id: str,
    scope: Mapping[str, object],
    captures: Sequence[Mapping[str, Any]],
    actions: Sequence[Mapping[str, Any]],
) -> AppRuleActionReadiness:
    """消费同范围可信 Capture/Action 投影；失败效果仍可送 Judge 判缺陷。

    captures 的截图、ui_snapshot 均须由调用方通过 Manifest 读回，不能把视觉模型
    自报的 scroll_y 或 ui_snapshot 当作同名可信制品。这里再校验前后归属及截图清单。
    """
    requirement = app_rule_action_requirement(rule)
    if requirement is None:
        return AppRuleActionReadiness(ready=True)
    by_id = {str(item.get("capture_id") or ""): item for item in captures}
    primary = by_id.get(primary_capture_id)
    if primary is None or not _valid_capture(primary, scope):
        return _pending(requirement, "current_capture")
    if requirement == "cover_content_same_object":
        return _cover_content_readiness(
            requirement=requirement,
            primary=primary,
            by_id=by_id,
            scope=scope,
            actions=actions,
        )
    if requirement == "list_scroll_interaction":
        source = app_list_interaction_source_capture(
            primary_capture_id=primary_capture_id, scope=scope, captures=captures, actions=actions
        )
        return _result(requirement, source is not None, "business_scroll_and_control_action_pairs")
    pairs = [
        (action, by_id[str(action["source_capture_id"])], by_id[str(action["result_capture_id"])])
        for action in actions
        if _valid_pair(action, by_id, scope)
        and _number(action.get("command_sequence")) is not None
        and action["command_sequence"] <= primary.get("session_command_sequence", -1)
    ]
    pairs = [pair for pair in pairs if _business_pair(pair[1], pair[2], primary=primary)]
    if requirement == "navigation_tab_comparison":
        observed = any(
            action.get("action_type") in {"click", "tap"} and _top_tab_target(action)
            for action, _, after in pairs
            if after["capture_id"] == primary_capture_id
        )
        return _result(requirement, observed, "top_directory_action_pair")

    primary_sequence = primary.get("session_command_sequence", -1)
    # 只承接当前动作或紧邻前一步滚动；同页型的遥远历史可能已切换列表或内容对象。
    scrolls = [
        pair
        for pair in pairs
        if pair[0].get("action_type") == "scroll"
        and pair[0]["command_sequence"] in {primary_sequence, primary_sequence - 1}
    ]
    geometry = _scroll_geometry(primary)
    if requirement == "list_bottom_observation":
        # 已证明不满一屏的页面也能交 Judge 判定前提是否适用，不能强迫无意义滚动。
        if geometry is not None and geometry[1] <= geometry[2] + 2:
            return AppRuleActionReadiness(ready=True, requirement=requirement)
        if not any(after["capture_id"] == primary_capture_id for _, _, after in scrolls):
            return _pending(requirement, "business_scroll_action_pair")
        if geometry is None and _native_scroll_pair(scrolls, primary_capture_id):
            return _native_position_unknown(requirement)
        return _result(requirement, _at_bottom(geometry), "trusted_bottom_position")
    if not scrolls:
        return _pending(requirement, "business_scroll_action_pair")
    if geometry is None and _native_scroll_pair(scrolls, primary_capture_id):
        # 原生 hierarchy 不提供 Web 滚动位置；真实前后图交 Judge 判断，绝不推造到底。
        # 已明确出现回顶控件时仍需一次操作尝试；缺控件不阻止 Judge 识别真实缺陷。
        if not _has_return_top_control(primary):
            return _native_position_unknown(requirement)
        return _pending(requirement, "return_to_top_action_pair")
    if geometry is None:
        for action, _before, after in pairs:
            if (
                after["capture_id"] == primary_capture_id
                and action.get("action_type") in {"click", "tap"}
                and _return_top_target(action)
                and any(
                    scroll[0]["command_sequence"] + 1 == action["command_sequence"]
                    and _native_scroll_pair([scroll], scroll[2]["capture_id"])
                    for scroll in scrolls
                )
            ):
                return _native_position_unknown(requirement)
    config = _mapping(rule.get("judge_config"))
    thresholds = _mapping(config.get("thresholds"))
    thresholds.update(_mapping(_mapping(config.get("capability_config")).get("thresholds")))
    minimum = _number(thresholds.get("back_to_top_min_scroll_viewports"))
    positions = [_scroll_geometry(after) for _, _, after in scrolls]
    moved_enough = any(
        position is not None and position[0] > (minimum or 0) * position[2]
        for position in positions
    )
    if not moved_enough:
        return _pending(requirement, "trusted_scroll_over_viewport_position")
    bottoms = [after for _, _, after in scrolls if _at_bottom(_scroll_geometry(after))]
    if not bottoms:
        return _pending(requirement, "trusted_bottom_position")
    # 没有回顶控件也是可判缺陷；不能把“控件存在/操作成功”作为准备条件。
    # 只有底部材料明确提供可点回顶控件时，才再要求一次真实尝试及其 after。
    bottom = max(bottoms, key=lambda item: item["session_command_sequence"])
    if not _has_return_top_control(bottom):
        return AppRuleActionReadiness(ready=True, requirement=requirement)
    attempted = any(
        action.get("action_type") in {"click", "tap"}
        and action["command_sequence"] > bottom["session_command_sequence"]
        and _return_top_target(action)
        for action, _, after in pairs
        if after["capture_id"] == primary_capture_id
    )
    return _result(requirement, attempted, "return_to_top_action_pair")


def app_list_interaction_source_capture(
    *, primary_capture_id: str, scope: Mapping[str, object],
    captures: Sequence[Mapping[str, Any]], actions: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    """列表操作检查以精确点击前页匹配，结果保留真实跳转后的 primary。"""
    by_id = {str(item.get("capture_id") or ""): item for item in captures}
    primary = by_id.get(primary_capture_id)
    if primary is None or not _valid_capture(primary, scope):
        return None
    pairs = [
        (action, by_id[str(action["source_capture_id"])], by_id[str(action["result_capture_id"])])
        for action in actions if _valid_pair(action, by_id, scope)
    ]
    for click, before, after in pairs:
        if (
            after["capture_id"] != primary_capture_id
            or click.get("action_type") not in {"click", "tap"}
            or not before.get("package_name")
            or before.get("package_name") != after.get("package_name")
            or not _unoccluded_app_pair(before, before, primary=before)
        ):
            continue
        for scroll, scroll_before, scroll_after in pairs:
            if (
                scroll.get("action_type") == "scroll"
                and _number(scroll.get("command_sequence")) is not None
                and scroll["command_sequence"] + 1 == click.get("command_sequence")
                and click.get("requested_capture_id") == scroll_after["capture_id"]
                and _business_pair(scroll_before, scroll_after, primary=before)
            ):
                return before
    return None


def _valid_capture(capture: Mapping[str, Any], scope: Mapping[str, object]) -> bool:
    screenshot = capture.get("screenshot_artifact_id")
    return bool(
        all(scope.get(key) is not None and capture.get(key) == scope[key] for key in _SCOPE_FIELDS)
        and capture.get("capture_status") == "captured"
        and capture.get("surface_kind") in {"android", "ios"}
        and capture.get("manifest_quality_status") == "pass"
        and screenshot
        and screenshot in (capture.get("manifest_screenshot_artifact_ids") or ())
    )


def _valid_pair(
    action: Mapping[str, Any],
    by_id: Mapping[str, Mapping[str, Any]],
    scope: Mapping[str, object],
) -> bool:
    before = by_id.get(str(action.get("source_capture_id") or ""))
    after = by_id.get(str(action.get("result_capture_id") or ""))
    if before is None or after is None or before is after:
        return False
    if not all(action.get(key) == scope.get(key) for key in _SCOPE_FIELDS):
        return False
    if action.get("status") not in {"completed", "failed", "timed_out"}:
        return False
    if action.get("execution_outcome") == "rejected":
        return False
    return bool(
        _valid_capture(before, scope)
        and _valid_capture(after, scope)
        and before.get("capture_role") == "before_action"
        and after.get("capture_role") == "after_action"
        and after.get("parent_capture_id") == before.get("capture_id")
        and action.get("action_execution_id")
        and action["action_execution_id"] == before.get("action_execution_id")
        and action["action_execution_id"] == after.get("action_execution_id")
        and action.get("command_sequence") == before.get("session_command_sequence")
        and action.get("command_sequence") == after.get("session_command_sequence")
    )


def _business_pair(
    before: Mapping[str, Any], after: Mapping[str, Any], *, primary: Mapping[str, Any]
) -> bool:
    page_type = primary.get("page_type")
    return bool(
        page_type
        and before.get("page_type") == after.get("page_type") == page_type
        and _unoccluded_app_pair(before, after, primary=primary)
    )


def _unoccluded_app_pair(before, after, *, primary) -> bool:
    package = primary.get("package_name")
    return bool(
        package
        and before.get("package_name") == after.get("package_name") == package
        and all(
            derive_app_presentation(
                page_state=capture.get("page_state"),
                overlay_signature=capture.get("overlay_signature"),
                surface_facts=_mapping(capture.get("surface_facts")),
            ).topmost_layer_kind.value
            == "page"
            for capture in (before, after, primary)
        )
    )


def _scroll_geometry(capture: Mapping[str, Any]) -> tuple[float, float, float] | None:
    snapshot = latest_ui_snapshot({"facts": {"ui_snapshot": capture.get("ui_snapshot")}})
    viewport = _mapping(snapshot.get("viewport"))
    document = _mapping(snapshot.get("document"))
    y = _number(viewport.get("scroll_y"))
    height = _number(document.get("scroll_height"))
    viewport_height = _number(viewport.get("height"))
    if y is None or y < 0 or height is None or height <= 0:
        return None
    if viewport_height is None or viewport_height <= 0 or y > height:
        return None
    return y, height, viewport_height


def _at_bottom(position: tuple[float, float, float] | None) -> bool:
    return bool(position is not None and position[0] + position[2] >= position[1] - 2)


def _native_scroll_pair(pairs, primary_capture_id: str) -> bool:
    return any(
        after["capture_id"] == primary_capture_id
        and all(
            (
                snapshot := latest_ui_snapshot(
                    {"facts": {"ui_snapshot": capture.get("ui_snapshot")}}
                )
            ).get("source")
            == "android_ui_hierarchy"
            and snapshot.get("surface_kind") == "android"
            and isinstance(snapshot.get("elements"), list)
            for capture in (before, after)
        )
        for _, before, after in pairs
    )


def _native_position_unknown(requirement: str) -> AppRuleActionReadiness:
    return AppRuleActionReadiness(
        ready=True,
        requirement=requirement,
        missing_evidence=("native_scroll_position_unavailable",),
    )


def _top_tab_target(action: Mapping[str, Any]) -> bool:
    # home_top_tab 是已有校准候选合同，普通 tab 可能是底栏，不能冒充顶部目录。
    return str(action.get("target_expected_state_hint") or "").startswith("home_top_tab:")


def _return_top_target(action: Mapping[str, Any]) -> bool:
    return (
        action.get("target_expected_state_hint") == "back_to_top"
        or str(action.get("semantic_target") or "").strip().casefold() in _RETURN_TOP_LABELS
    )


def _has_return_top_control(capture: Mapping[str, Any]) -> bool:
    elements = latest_ui_snapshot({"facts": {"ui_snapshot": capture.get("ui_snapshot")}}).get(
        "elements"
    )
    if not isinstance(elements, list):
        return False
    return any(
        isinstance(item, Mapping)
        and item.get("clickable") is True
        and any(
            str(item.get(key) or "").strip().casefold() in _RETURN_TOP_LABELS
            for key in ("text", "aria_label", "content_desc", "name")
        )
        for item in elements
    )


def _cover_content_readiness(
    *,
    requirement: str,
    primary: Mapping[str, Any],
    by_id: Mapping[str, Mapping[str, Any]],
    scope: Mapping[str, object],
    actions: Sequence[Mapping[str, Any]],
) -> AppRuleActionReadiness:
    """点开同一封面后的正文或播放帧才可交 Judge；列表补采和广告图都不算配成套。"""

    from services.api.app.application.cover_content_pair import assess_cover_content_pair

    pairs = [
        (action, by_id[str(action["source_capture_id"])], by_id[str(action["result_capture_id"])])
        for action in actions
        if _valid_pair(action, by_id, scope)
        and action.get("action_type") in {"click", "tap"}
        and by_id[str(action["result_capture_id"])]["capture_id"] == primary["capture_id"]
    ]
    if not pairs:
        observed = {
            "capture_id": primary.get("capture_id"),
            "page_type": primary.get("page_type"),
            "action": "observe",
            "material_kind": "list"
            if str(primary.get("page_type") or "").casefold()
            in {"home", "content_list", "video_list", "post_list"}
            else "",
            "is_ad": primary.get("is_ad"),
        }
        gap = assess_cover_content_pair(cover=None, content=observed)["gap_codes"][0]
        return _pending(requirement, gap)
    action, before, after = pairs[-1]
    pair = assess_cover_content_pair(
        cover={
            "capture_id": before.get("capture_id"),
            "page_type": before.get("page_type"),
            "content_key": before.get("content_key") or action.get("semantic_target"),
            "material_kind": before.get("material_kind") or "cover",
            "is_ad": before.get("is_ad"),
            "semantic_target": action.get("semantic_target"),
        },
        content={
            "capture_id": after.get("capture_id"),
            "page_type": after.get("page_type"),
            "content_key": after.get("content_key") or action.get("semantic_target"),
            "material_kind": after.get("material_kind")
            or ("playback" if after.get("page_type") == "video_detail" else "body"),
            "action": action.get("action_type"),
            "is_ad": after.get("is_ad"),
            "semantic_target": action.get("semantic_target"),
        },
    )
    if pair["complete"]:
        return AppRuleActionReadiness(ready=True, requirement=requirement)
    return _pending(requirement, pair["gap_codes"][0])


def _result(requirement: str, ready: bool, missing: str) -> AppRuleActionReadiness:
    return (
        AppRuleActionReadiness(ready=True, requirement=requirement)
        if ready
        else _pending(requirement, missing)
    )


def _pending(requirement: str, missing: str) -> AppRuleActionReadiness:
    return AppRuleActionReadiness(ready=False, requirement=requirement, missing_evidence=(missing,))


def _mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None
