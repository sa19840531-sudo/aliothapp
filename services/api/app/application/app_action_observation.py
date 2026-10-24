"""将同一次 App 动作的前后观察统一为候选、覆盖和 Frontier 的证明。

证明由工作流生成，不能读取视觉模型自报的成功字段。执行成功、观察完成、
局部状态变化和业务到达分别记录；图片变化本身不证明业务到达。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from services.api.app.application.app_deep_link_contract import (
    DEEP_LINK_DIRECT_OBSERVATION_OUTCOME,
    DEEP_LINK_DIRECT_OBSERVATION_REASON,
    DEEP_LINK_HARD_BLOCKED_REASON,
    build_deep_link_observation_fields,
    deep_link_is_hard_blocked,
    deep_link_proves_native_control_click,
)
from services.api.app.application.app_external_transition import (
    external_transition_from_package_hop,
)
from services.api.app.application.app_navigation_executor_contract import (
    is_native_navigation_operation,
    settle_navigation_after_evidence,
    settlement_blocks_business_arrival,
)
from services.api.app.application.app_obstruction_controls import (
    build_app_obstruction_identity,
    is_obstructed_surface,
)
from services.api.app.application.app_package_boundary import (
    app_location_within_package_boundary,
    classify_external_activity,
)
from services.api.app.application.explorer_turn import ExplorerTurnResult
from services.api.app.domain.exploration import AppVisualAnalysis, CandidateAction, ExplorationWork
from services.api.app.domain.exploration.app_visual_page_identity import (
    validate_app_visual_local_state,
    validated_app_visual_page_identity,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    InteractionOperation,
    InteractionResultStatus,
    InteractionVerificationOutcome,
)
from services.api.app.domain.page.registry import get_default_page_type_registry
from services.api.app.domain.protocol import freeze_json_mapping, stable_protocol_hash

APP_ACTION_OBSERVATION_KEY = "app_action_observation"
_VERSION = "app-action-observation.v1"
_COMPLETED = frozenset(
    {
        "business_arrived",
        "local_state_changed",
        "observation_completed",
        "obstruction_removed",
        "surface_restored",
        # 底栏巡检：前后截图已绑定且 after 页型切到目标栏才算完成。
        "tab_seek_screenshot_evidenced",
    }
)


@dataclass(frozen=True)
class AppActionObservation:
    """不可变的动作观察结果，保存实际绑定而不是单一成功布尔值。"""

    binding: Mapping[str, Any]
    outcome: str
    reason: str
    navigation_path: tuple[str, ...]
    business_visible: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "binding", freeze_json_mapping(self.binding, "binding"))

    @property
    def completed(self) -> bool:
        return self.outcome in _COMPLETED

    @property
    def arrival_proven(self) -> bool:
        return self.business_visible and self.outcome in {
            "business_arrived",
            "obstruction_removed",
            "tab_seek_screenshot_evidenced",
        }

    @property
    def tab_seek_evidenced(self) -> bool:
        """底栏巡检须截图对及当前选中态或可信页型变化证明。"""

        return self.outcome == "tab_seek_screenshot_evidenced"

    @property
    def observation_proven(self) -> bool:
        return self.business_visible and self.outcome == "observation_completed"

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "protocol_version": _VERSION,
            "binding": dict(self.binding),
            "outcome": self.outcome,
            "reason": self.reason,
            "business_visible": self.business_visible,
            "navigation_path": list(self.navigation_path),
        }
        return {**payload, "proof_hash": stable_protocol_hash(payload)}


def confirmed_app_identity(analysis: AppVisualAnalysis) -> str | None:
    identity = validated_app_visual_page_identity(
        analysis.surface_facts.get("app_page_identity"),
        capture_id=analysis.capture_id,
        screenshot_artifact_id=analysis.screenshot_artifact_id,
        image_signature=analysis.image_signature,
        package_name=analysis.package_name,
        activity=analysis.activity,
    )
    return str(identity["identity"]) if identity is not None else None


def build_app_action_observation(
    *,
    work: ExplorationWork,
    turn: ExplorerTurnResult,
    candidate: CandidateAction,
    before: AppVisualAnalysis,
    after: AppVisualAnalysis,
    popup_close: bool = False,
) -> AppActionObservation:
    """仅使用本次已保存 Capture 的视觉事实，不使用页型对齐后的推测。"""

    coordination = turn.coordination
    intent = turn.decision
    raw_path = work.frontier.observation.metadata.get("app_verified_navigation_path", ())
    path = (
        tuple(item for item in raw_path if isinstance(item, str))
        if isinstance(raw_path, (list, tuple))
        else ()
    )
    source_identity = (
        confirmed_app_identity(before)
        if app_location_within_package_boundary(
            location=f"app://{before.package_name or ''}/{before.activity or ''}",
            surface_kind=work.surface_kind,
            allowed_hosts=work.boundaries.allowed_hosts,
        )
        else None
    )
    target_identity = confirmed_app_identity(after)
    if source_identity is not None and (not path or path[-1] != source_identity):
        # 前态已经偏离上一轮路径时，不能跨过未知跳转继续拼接历史路线。
        path = (source_identity,)
    binding: dict[str, Any] = {
        "task_id": work.task_id,
        "session_id": work.session_id,
        "parent_work_id": work.work_id,
        "requested_source_capture_id": work.source_capture_id,
        "candidate_id": candidate.candidate_id,
        "before_capture_id": before.capture_id,
        "after_capture_id": after.capture_id,
        "after_image_signature": after.image_signature,
        "after_artifact_id": after.screenshot_artifact_id,
        "after_result_hash": after.result_hash,
        "action_type": candidate.action_type.value,
    }

    def result(outcome: str, reason: str, *, visible: bool = False) -> AppActionObservation:
        return AppActionObservation(binding, outcome, reason, path, visible)

    if coordination is None or not isinstance(intent, ActionIntent):
        return result("unproven", "action_context_missing")
    interaction = coordination.interaction
    source = coordination.before_capture
    target = coordination.after_capture
    binding.update(intent_id=intent.intent_id, action_execution_id=interaction.action_execution_id)
    if (
        target is None
        or turn.work_id != work.work_id
        or turn.task_id != work.task_id
        or intent.source_capture_id != work.source_capture_id
        or intent.parameters.get("candidate_id") != candidate.candidate_id
        or intent.action_type is not candidate.action_type
        or interaction.intent_id != intent.intent_id
        or intent.surface_kind is not work.surface_kind
        or any(
            item.task_id != work.task_id or item.session_id != work.session_id
            for item in (intent, interaction, source, target)
        )
        or source.surface_kind is not work.surface_kind
        or target.surface_kind is not work.surface_kind
        or source.role != "before_action"
        or target.role != "after_action"
        or source.capture_id != before.capture_id
        or target.capture_id != after.capture_id
        or source.capture_id == target.capture_id
        or source.parent_capture_id != work.source_capture_id
        or target.parent_capture_id != source.capture_id
        or target.action_execution_id != interaction.action_execution_id
        or source.action_execution_id not in {None, interaction.action_execution_id}
        or interaction.source_capture_id != source.capture_id
        or interaction.result_capture_id != target.capture_id
        or before.screenshot_artifact_id not in source.artifact_ids
        or after.screenshot_artifact_id not in target.artifact_ids
    ):
        return result("unproven", "action_capture_binding_mismatch")
    if interaction.status is not InteractionResultStatus.SUCCEEDED:
        return result("failed", "execution_not_succeeded")
    if interaction.verification_outcome in {
        InteractionVerificationOutcome.NO_EFFECT,
        InteractionVerificationOutcome.EFFECT_UNEXPECTED,
    }:
        return result("unproven", "execution_verification_negative")
    after_within = app_location_within_package_boundary(
        location=f"app://{after.package_name or ''}/{after.activity or ''}",
        surface_kind=work.surface_kind,
        allowed_hosts=work.boundaries.allowed_hosts,
    )
    if not after_within:
        external = external_transition_from_package_hop(
            source_capture_id=str(before.capture_id),
            before_package=before.package_name,
            before_activity=before.activity,
            after_package=after.package_name,
            after_activity=after.activity,
            allowed_hosts=work.boundaries.allowed_hosts,
            trigger_action_id=str(
                getattr(coordination.interaction, "action_execution_id", "") or ""
            ),
        )
        if external is not None:
            binding["external_transition"] = external
    operation = candidate.action_type
    navigation_settlement = None
    if is_native_navigation_operation(operation):
        bound_epoch = (
            candidate.metadata.get("capture_epoch")
            or intent.parameters.get("capture_epoch")
            or before.surface_facts.get("capture_epoch")
            or before.surface_facts.get("finalization_epoch")
        )
        after_epoch = (
            after.surface_facts.get("capture_epoch")
            or after.surface_facts.get("finalization_epoch")
            or bound_epoch
        )
        # after 已具备 epoch、候选／before 未绑定时回填，避免仅补 after 结算件的
        # 单元夹具被误判为 epoch_missing（仍要求 after 四件套齐全）。
        if not bound_epoch:
            bound_epoch = after_epoch
        settle_facts = dict(after.surface_facts)
        for key in (
            "has_ui_tree",
            "current_window",
            "window",
            "window_title",
            "window_id",
            "ui_tree_artifact_id",
            "capture_epoch",
            "finalization_epoch",
        ):
            if key in candidate.metadata and key not in settle_facts:
                settle_facts[key] = candidate.metadata[key]
            if key in intent.parameters and key not in settle_facts:
                settle_facts[key] = intent.parameters[key]
        navigation_settlement = settle_navigation_after_evidence(
            screenshot_artifact_id=after.screenshot_artifact_id,
            package_name=after.package_name,
            activity=after.activity,
            orientation=after.orientation,
            page_state=after.page_state,
            surface_facts=settle_facts,
            bound_epoch=bound_epoch,
            after_epoch=after_epoch,
            ui_tree_present=(
                True
                if settle_facts.get("has_ui_tree") is True
                or candidate.metadata.get("has_ui_tree") is True
                else None
            ),
            artifact_ids=getattr(target, "artifact_ids", None),
        )
        binding["navigation_executor_settlement"] = navigation_settlement
    recovery_attempt = candidate.metadata.get(
        "package_boundary_recovery"
    ) is True and operation in {
        InteractionOperation.BACK,
        InteractionOperation.WARM_RESTORE,
    }
    if not after_within:
        external_kind = classify_external_activity(
            package_name=after.package_name,
            activity=after.activity,
        ).value
        binding["external_activity_kind"] = external_kind
        binding["external_package_name"] = str(after.package_name or "")
        if recovery_attempt:
            # 平台签发的回源已执行，但 after 仍在外部：明确回源失败，而非泛化 external。
            return result("external_return_failed", "recovery_still_outside_target_app")
        return result("external", "after_package_outside_scope")
    before_obstructed = _obstructed(before)
    after_obstructed = _obstructed(after)
    visible = target_identity is not None and _stable(after) and not after_obstructed
    home_top_seek = candidate.metadata.get("home_top_category_tab_seek") is True
    # 上方分类也带 require_screenshot_evidence，但不得走底栏切栏证明（同页 home 馈流）。
    tab_seek = (
        candidate.metadata.get("bottom_navigation_tab_seek") is True
        or (
            candidate.metadata.get("require_screenshot_evidence") is True
            and not home_top_seek
        )
        or _candidate_looks_like_bottom_tab_seek(candidate)
    )
    if operation in {InteractionOperation.OBSERVE, InteractionOperation.TIMED_CAPTURE}:
        return result("observation_completed", "bound_capture_observed", visible=visible)
    if popup_close:
        if before_obstructed and not after_obstructed and visible:
            if not path or path[-1] != target_identity:
                path = (target_identity,)
            return result("obstruction_removed", "visible_business_after_close", visible=True)
        if after_obstructed and build_app_obstruction_identity(
            before
        ) != build_app_obstruction_identity(after):
            return result("intermediate", "obstruction_replaced")
        return result("unproven", "obstruction_not_removed")
    # 主页上方分类：同页 home 切馈流，截图变化且仍在包内即留证（不要求底栏选中态）。
    if (
        home_top_seek
        and operation in {InteractionOperation.CLICK, InteractionOperation.TAP}
        and before.capture_id != after.capture_id
        and before.screenshot_artifact_id
        and after.screenshot_artifact_id
    ):
        binding["home_top_category_tab_seek"] = True
        after_pt = str(after.visual_page_type or "").strip().casefold()
        before_pt = str(before.visual_page_type or "").strip().casefold()
        signature_changed = before.image_signature != after.image_signature
        still_homeish = after_pt in {
            "home",
            "post_list",
            "video_list",
            "content_list",
            "",
            "unknown",
        }
        if signature_changed and (
            after_pt == "home"
            or (before_pt == "home" and still_homeish)
            or (visible and still_homeish)
        ):
            binding["home_top_category_transition_reason"] = (
                "home_top_category_screenshot_changed"
            )
            return result(
                "tab_seek_screenshot_evidenced",
                "home_top_category_screenshot_changed",
                visible=visible or after_pt == "home",
            )
        binding["home_top_category_transition_reason"] = (
            "home_top_category_effect_unconfirmed"
        )
        return result(
            "unproven",
            "home_top_category_effect_unconfirmed",
            visible=visible,
        )
    # 底栏留证须有 before/after 截图对，并以当前选中态或可信页型变化证明切栏。
    # 禁止「点了仍停在 home|popup」仅因换了一张同页截图就写成 VISITED（R0 d02d8aed）。
    if (
        tab_seek
        and operation in {InteractionOperation.CLICK, InteractionOperation.TAP}
        and before.capture_id != after.capture_id
        and before.screenshot_artifact_id
        and after.screenshot_artifact_id
    ):
        binding["bottom_navigation_tab_seek"] = True
        binding["tab_seek_label"] = str(candidate.semantic_target or "")
        transition_ok, transition_reason = _bottom_tab_seek_transition_proven(
            candidate=candidate,
            before=before,
            after=after,
            after_business_visible=visible,
        )
        binding["tab_seek_transition_reason"] = transition_reason
        if transition_ok:
            # 可信且无遮挡的切栏后态同时形成业务到达，保留底栏专用结算类型。
            if visible and (not path or path[-1] != target_identity):
                path = (*path, target_identity)
            return result(
                "tab_seek_screenshot_evidenced",
                transition_reason,
                visible=visible,
            )
        # 未切栏：立即 unproven，禁止落入 intermediate／SUPERSEDED 把 sibling 烧光。
        return result(
            "unproven",
            transition_reason or "tab_seek_transition_unproven",
            visible=visible,
        )
    if after_obstructed:
        return result("unproven", "after_business_obstructed")
    if not visible:
        if (
            _stable(after)
            and after.visual_page_type in get_default_page_type_registry().lifecycle_codes
            and before.image_signature != after.image_signature
        ):
            return result("intermediate", "lifecycle_observed")
        return result("unproven", "after_business_unconfirmed")
    if recovery_attempt:
        if app_location_within_package_boundary(
            location=f"app://{before.package_name or ''}/{before.activity or ''}",
            surface_kind=work.surface_kind,
            allowed_hosts=work.boundaries.allowed_hosts,
        ):
            return result("unproven", "app_recovery_source_not_external")
        if operation is InteractionOperation.WARM_RESTORE and (
            intent.parameters.get("package_name") != after.package_name
        ):
            return result("unproven", "app_recovery_target_mismatch")
        # 恢复只证明重新进入目标 App，不证明恢复了原页或原状态；断开外跳前路线。
        path = (target_identity,)
        return result("surface_restored", "target_app_visible_after_recovery", visible=True)
    if operation is InteractionOperation.SCROLL:
        if source_identity == target_identity and _scroll_effect_observed(before, after):
            return result("local_state_changed", "bound_scroll_anchors_moved", visible=True)
        return result("unproven", "scroll_effect_unconfirmed")
    expected = candidate.metadata.get("expected_next_page_type")
    if (
        isinstance(expected, str)
        and expected.strip()
        and expected.casefold().strip() != str(after.visual_page_type or "").casefold()
    ):
        return result("intermediate", "different_business_target_observed")
    if operation is InteractionOperation.BACK:
        if source_identity is None or not path or path[-1] != source_identity:
            return result("unproven", "return_source_relationship_unconfirmed")
        predecessors = path[:-1]
        if target_identity not in predecessors:
            return result("unproven", "return_relationship_unconfirmed")
        index = len(predecessors) - 1 - predecessors[::-1].index(target_identity)
        path = predecessors[: index + 1]
        return result("business_arrived", "verified_business_return", visible=True)
    if operation is InteractionOperation.DEEP_LINK:
        # Deep Link 只形成直达观察，不得证明原生控件点击通过。
        assert deep_link_proves_native_control_click() is False
        deep_link_uri = (
            intent.parameters.get("deep_link")
            or intent.parameters.get("uri")
            or intent.parameters.get("url")
            or ""
        )
        target_pkg = (
            str(intent.parameters.get("package_name") or before.package_name or "")
            .strip()
            .casefold()
        )
        after_pkg = str(after.package_name or "").strip().casefold()
        fields = build_deep_link_observation_fields(
            uri=deep_link_uri,
            within_target_package=bool(target_pkg) and target_pkg == after_pkg,
        )
        if deep_link_is_hard_blocked(deep_link_uri) or fields.get("hard_blocked") is True:
            return result("unproven", DEEP_LINK_HARD_BLOCKED_REASON, visible=False)
        visible = bool(target_identity) and bool(target_pkg) and after_pkg == target_pkg
        return result(
            DEEP_LINK_DIRECT_OBSERVATION_OUTCOME,
            DEEP_LINK_DIRECT_OBSERVATION_REASON,
            visible=visible,
        )
    if operation in {
        InteractionOperation.CLICK,
        InteractionOperation.TAP,
        InteractionOperation.NAVIGATE,
    }:
        if source_identity is None and (
            before.visual_page_type in get_default_page_type_registry().lifecycle_codes
            and before.package_name == after.package_name
            and before.image_signature != after.image_signature
        ):
            if settlement_blocks_business_arrival(navigation_settlement):
                return result("unproven", "navigation_after_evidence_incomplete")
            # 启动／引导页不是业务节点；退出后仍需当前帧确认业务身份。
            path = (target_identity,)
            return result("business_arrived", "visible_business_after_lifecycle", visible=True)
        if source_identity is not None and source_identity != target_identity:
            if settlement_blocks_business_arrival(navigation_settlement):
                return result("unproven", "navigation_after_evidence_incomplete")
            path = (*path, target_identity)
            return result("business_arrived", "business_identity_changed", visible=True)
        old = before.surface_facts.get("local_state")
        new = after.surface_facts.get("local_state")
        if (
            source_identity == target_identity
            and isinstance(old, Mapping)
            and isinstance(new, Mapping)
            and old != new
        ):
            return result("local_state_changed", "bound_local_state_changed", visible=True)
    return result("unproven", "intended_effect_unconfirmed")


def action_observation_for_successor(
    *,
    turn: ExplorerTurnResult,
    successor: ExplorationWork,
) -> Mapping[str, Any] | None:
    """Frontier 只消费同一次动作的工作流证明，拒绝缺失或跨轮沿用。"""
    raw = successor.frontier.observation.metadata.get(APP_ACTION_OBSERVATION_KEY)
    intent = turn.decision
    coordination = turn.coordination
    if not isinstance(raw, Mapping) or not isinstance(intent, ActionIntent) or coordination is None:
        return None
    payload = {key: value for key, value in raw.items() if key != "proof_hash"}
    if raw.get("protocol_version") != _VERSION or raw.get("proof_hash") != stable_protocol_hash(
        payload
    ):
        return None
    binding = raw.get("binding")
    after = coordination.after_capture
    if not isinstance(binding, Mapping) or after is None:
        return None
    expected = {
        "task_id": successor.task_id,
        "session_id": successor.session_id,
        "parent_work_id": turn.work_id,
        "requested_source_capture_id": intent.source_capture_id,
        "candidate_id": intent.parameters.get("candidate_id"),
        "intent_id": intent.intent_id,
        "action_execution_id": coordination.interaction.action_execution_id,
        "before_capture_id": coordination.before_capture.capture_id,
        "after_capture_id": after.capture_id,
        "after_image_signature": successor.frontier.observation.state_version,
        "after_artifact_id": successor.frontier.observation.metadata.get("screenshot_artifact_id"),
        "after_result_hash": successor.frontier.observation.metadata.get("visual_result_hash"),
        "action_type": intent.action_type.value,
    }
    if (
        any(binding.get(key) != value for key, value in expected.items())
        or successor.source_capture_id != after.capture_id
        or turn.task_id != successor.task_id
        or coordination.interaction.task_id != successor.task_id
        or coordination.interaction.session_id != successor.session_id
        or after.task_id != successor.task_id
        or after.session_id != successor.session_id
    ):
        return None
    if coordination.interaction.status is not InteractionResultStatus.SUCCEEDED:
        return None
    return raw


def _obstructed(analysis: AppVisualAnalysis) -> bool:
    return is_obstructed_surface(
        page_state=analysis.page_state,
        page_type=analysis.visual_page_type,
        overlay_signature=analysis.overlay_signature,
        surface_facts=analysis.surface_facts,
    )


def _stable(analysis: AppVisualAnalysis) -> bool:
    facts = analysis.surface_facts
    confidence = facts.get("visual_page_type_confidence", facts.get("page_type_confidence", 0))
    quality = facts.get("visual_provider_quality", {})
    return (
        analysis.page_state == "stable"
        and isinstance(confidence, (int, float))
        and not isinstance(confidence, bool)
        and 0.70 <= confidence <= 1
        and not (isinstance(quality, Mapping) and quality.get("status") == "degraded")
    )


def _scroll_effect_observed(before: AppVisualAnalysis, after: AppVisualAnalysis) -> bool:
    """用两帧绑定的多个同名内容锚点确认移动，不接受模型自报偏移。"""
    if before.image_signature == after.image_signature or (
        before.width,
        before.height,
        before.orientation,
    ) != (after.width, after.height, after.orientation):
        return False

    def anchors(analysis):
        result = {}
        duplicates = set()
        for item in analysis.candidates:
            if item.confidence < 0.8 or item.normalized_bounds is None:
                continue
            key = item.semantic_name.strip().casefold()
            if key in result:
                duplicates.add(key)
            result[key] = item.normalized_bounds
        return {key: box for key, box in result.items() if key not in duplicates}

    old, new = anchors(before), anchors(after)
    shifts = []
    for key in old.keys() & new.keys():
        left, right = old[key], new[key]
        dx, dy = right.left - left.left, right.top - left.top
        if (
            abs((right.right - right.left) - (left.right - left.left)) > 0.02
            or abs((right.bottom - right.top) - (left.bottom - left.top)) > 0.02
        ):
            continue
        if abs(dy) >= 0.025 and abs(dx) <= 0.02:
            shifts.append(("vertical", dy))
        elif abs(dx) >= 0.025 and abs(dy) <= 0.02:
            shifts.append(("horizontal", dx))
    return any(
        axis == other_axis and delta * other_delta > 0 and abs(delta - other_delta) <= 0.02
        for index, (axis, delta) in enumerate(shifts)
        for other_axis, other_delta in shifts[index + 1 :]
    )


def _expected_bottom_tab_page_type(candidate: CandidateAction) -> str:
    """仅使用当前候选携带的页型预期，不按栏目名称推导。"""
    for key in ("expected_next_page_type", "expected_state_hint"):
        raw = candidate.metadata.get(key)
        if isinstance(raw, str) and raw.strip():
            expected = raw.strip().casefold()
            # tab:标签描述当前入口身份，不是视觉分类目录中的页型。
            if expected in get_default_page_type_registry().all_codes:
                return expected
    return ""


def _bottom_tab_selected_label_aliases(analysis: AppVisualAnalysis) -> set[str]:
    """汇集当前帧已绑定视觉锚点、像素与 a11y 的底栏选中标签。"""

    from services.api.app.application.app_semantic_progress import (
        _normalize_bottom_tab_label_for_visit,
        expand_bottom_tab_label_aliases,
    )

    facts = analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else {}
    selected: set[str] = set()
    local_observation = facts.get("app_local_state_observation")
    if (
        isinstance(local_observation, Mapping)
        and confirmed_app_identity(analysis)
        and validate_app_visual_local_state(
            facts=facts,
            capture_id=analysis.capture_id,
            screenshot_artifact_id=analysis.screenshot_artifact_id,
            image_signature=analysis.image_signature,
            package_name=analysis.package_name,
            activity=analysis.activity,
        )
    ):
        anchors = local_observation.get("anchors")
        tab_anchors = anchors.get("selected_tab") if isinstance(anchors, Mapping) else None
        if isinstance(tab_anchors, (list, tuple)):
            for anchor in tab_anchors:
                if not isinstance(anchor, Mapping) or not _bounds_in_bottom_nav_band(
                    anchor.get("normalized_bounds")
                ):
                    continue
                label = anchor.get("text")
                if isinstance(label, str) and label.strip():
                    selected.update(
                        expand_bottom_tab_label_aliases(
                            _normalize_bottom_tab_label_for_visit(label)
                        )
                    )
    raw_labels = facts.get("pixel_selected_labels")
    if isinstance(raw_labels, (list, tuple)):
        for item in raw_labels:
            if not isinstance(item, str) or not item.strip():
                continue
            selected.update(
                expand_bottom_tab_label_aliases(_normalize_bottom_tab_label_for_visit(item))
            )
    controls = facts.get("trusted_accessibility_controls")
    if isinstance(controls, Mapping):
        bottom = controls.get("bottom_navigation")
        if isinstance(bottom, (list, tuple)):
            for item in bottom:
                if not isinstance(item, Mapping) or item.get("selected") is not True:
                    continue
                raw = item.get("label") or item.get("text") or item.get("content_desc")
                if not isinstance(raw, str) or not raw.strip():
                    continue
                selected.update(
                    expand_bottom_tab_label_aliases(_normalize_bottom_tab_label_for_visit(raw))
                )
    return selected


def _bottom_tab_seek_transition_proven(
    *,
    candidate: CandidateAction,
    before: AppVisualAnalysis,
    after: AppVisualAnalysis,
    after_business_visible: bool,
) -> tuple[bool, str]:
    """底栏 VISITED 须证明切栏；同页 home|popup 换截图不算。

    Flutter 等常切栏后页型仍被判成 home：以选中态从「非目标→目标」为硬证据。
    """

    from services.api.app.application.app_semantic_progress import (
        _normalize_bottom_tab_label_for_visit,
        expand_bottom_tab_label_aliases,
    )

    target_aliases = expand_bottom_tab_label_aliases(
        _normalize_bottom_tab_label_for_visit(candidate.semantic_target)
    )
    before_selected = _bottom_tab_selected_label_aliases(before) if target_aliases else set()
    after_selected = _bottom_tab_selected_label_aliases(after) if target_aliases else set()
    if target_aliases:
        if target_aliases & after_selected and not (target_aliases & before_selected):
            return True, "after_selected_tab_matches_target"

    # 选中态本身可证明入口切换；仅靠页型则必须已有稳定、未遮挡的可信业务后态。
    # reason 与通用路径对齐：after_business_unconfirmed／different_business_target_observed。
    if not after_business_visible:
        return False, "after_business_unconfirmed"
    expected = _expected_bottom_tab_page_type(candidate)
    before_pt = str(before.visual_page_type or "").strip().casefold()
    after_pt = str(after.visual_page_type or "").strip().casefold()
    # 短剧／短视频／视频：常直接进播放器（video_detail），不得因期望 video_list 而拒收。
    # 但须有真实切栏线索：已离开其它底栏选中，或后态底栏已选中目标。
    # 禁止顶部同名锚点／已在目标栏空点仅靠页型放松冒充 VISITED（视觉选中态合同）。
    target_norm = _normalize_bottom_tab_label_for_visit(candidate.semantic_target)
    short_drama_aliases = expand_bottom_tab_label_aliases(target_norm)
    already_on_target = bool(target_aliases & before_selected)
    left_other_tab = bool(before_selected) and not already_on_target
    landed_on_target_selected = bool(target_aliases & after_selected)
    if (
        not already_on_target
        and short_drama_aliases & {"短剧", "短视频", "视频"}
        and after_pt in {"video_list", "video_detail", "content_detail"}
    ):
        if after_pt == expected:
            return True, "after_page_matches_expected_tab"
        if left_other_tab or landed_on_target_selected:
            return True, "after_page_short_drama_player_or_list"
    if expected:
        if after_pt == expected:
            return True, "after_page_matches_expected_tab"
        if expected == "home" and after_pt == "home":
            # 已在首页再点「首页」：允许同页型，但仍须画面有变化以免空点。
            if before.image_signature != after.image_signature:
                return True, "home_tab_reconfirmed_by_screenshot_change"
            return False, "home_tab_screenshot_unchanged"
        return False, "different_business_target_observed"
    # 当前候选没有页型预期时，使用可信后态变化，不从栏目名称补造预期。
    if after_pt and after_pt not in {"", "unknown"} and after_pt != before_pt:
        return True, "unmapped_tab_page_changed"
    return False, "unmapped_tab_still_on_source_page"


def _candidate_looks_like_bottom_tab_seek(candidate: CandidateAction) -> bool:
    """识别底栏巡检候选：metadata、条带几何，或「底部Tab／底栏」语义文案。

    Vision 常产出 frontier_scope=app 的「底部Tab精选」且缺 normalized_bounds；
    若只认条带几何，点击后仍会写成 ARRIVAL_UNPROVEN 并 failed，截图留证失效。
    """

    if candidate.metadata.get("frontier_scope") == "app" and _candidate_in_bottom_nav_band(
        candidate
    ):
        return True
    terms = " ".join(
        (
            str(candidate.semantic_target or ""),
            str(candidate.metadata.get("purpose") or ""),
            str(candidate.metadata.get("expected_state_hint") or ""),
        )
    ).casefold()
    return any(
        marker in terms
        for marker in (
            "底栏",
            "底部tab",
            "底部导航",
            "bottom navigation",
            "bottom_navigation",
            "tab:",
        )
    ) or (
        # Vision 常写「底部精选／底部片库」，无「Tab」字样。
        candidate.metadata.get("frontier_scope") == "app"
        and ("底部" in terms or terms.startswith("tab"))
    )


def _candidate_in_bottom_nav_band(candidate: CandidateAction) -> bool:
    """判断候选是否落在底部导航条带，供全局 sibling 截图留证。"""

    return _bounds_in_bottom_nav_band(candidate.metadata.get("normalized_bounds"))


def _bounds_in_bottom_nav_band(raw: Any) -> bool:
    """使用同一底栏条带判断候选与已验证视觉锚点。"""
    if not isinstance(raw, Mapping):
        return False
    try:
        top = float(raw.get("top"))
        bottom = float(raw.get("bottom"))
    except (TypeError, ValueError):
        return False
    return top >= 0.78 and bottom <= 1.02
