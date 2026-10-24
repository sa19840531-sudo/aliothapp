"""年龄确认门禁交互识别。

用于把「我年满18岁-进入」类控件识别为可清障的门禁确认，
避免 Vision 标成 medium 后被 Surface Phase 与动态 Frontier 安全降级吞掉；
同时必须排除「我未满18岁-离开」。

APP 链补充：`docs/data/alioth_app_rule_call_contracts.json` 的
`ui_tree_features.age_gate`（如海角「我已满18岁」）参与匹配与门禁判定。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from services.api.app.domain.interaction import InteractionOperation, InteractionRiskLevel

# 只认“确认已成年并进入”，禁止裸匹配「满18」（会误伤「未满18岁-离开」）。
AGE_CONFIRM_MARKERS: tuple[str, ...] = (
    "confirm_age_18_enter",
    "confirm_age_18",
    "confirm_age",
    "age_18_enter",
    "我年满18岁-进入",
    "我已满18岁-进入",
    "我已满18岁",
    "我年满18岁进入",
    "我已满18岁进入",
    "我年满18",
    "我已满18",
    "年满18岁-进入",
    "已满18岁-进入",
    "年满18",
    "已满18",
    "年满十八",
    "已满十八",
    "确认已满18",
    "确认年满18",
)

AGE_CONFIRM_LEAVE_MARKERS: tuple[str, ...] = (
    "未满18",
    "未满十八",
    "未年满",
    "under 18",
    "under18",
    "未成年",
    "离开",
    "exit",
    "leave",
)

_AGE_CONFIRM_ACTION_TYPES = frozenset({InteractionOperation.CLICK, InteractionOperation.TAP})
_AGE_CONFIRM_RISKS = frozenset({InteractionRiskLevel.LOW, InteractionRiskLevel.MEDIUM})

# 仅允许质检运行点击“已成年并进入”，不得扩展为任意进入或确认按钮。
# 容器与按钮的类名各站不同（实测 91吃瓜用 age-verify-overlay / age-verify-btn），
# 因此按 age-gate / age-confirm / age-verify 三组前缀取并集；仍要求命中年龄门
# 容器内部的按钮，不放宽成任意「进入」控件，离开键另由语义 marker 排除。
AGE_CONFIRM_ENTER_SELECTOR = (
    ":is("
    "#age-gate, .age-gate, [data-age-gate], "
    "#age-verify, .age-verify, [data-age-verify], "
    "#age-verify-overlay, .age-verify-overlay, "
    "#age-confirm, .age-confirm, [data-age-confirm]"
    "):visible "
    ":is("
    ".age-gate-enter, .age-confirm-enter, .confirm-age-enter, "
    ".age-verify-btn, .age-verify-enter, .age-gate-btn, .age-confirm-btn"
    "):visible"
)


def age_confirm_terms(*parts: object) -> str:
    """拼接候选语义文本，供 marker 匹配。"""

    return " ".join(str(part or "") for part in parts).casefold()


def _age_gate_json_markers() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """从 APP JSON 读取进入／离开按钮文案；失败则空。"""

    try:
        from services.api.app.application.app_ui_tree_features import (
            age_gate_ui_tree_features,
        )

        features = age_gate_ui_tree_features()
        enter = tuple(
            str(item)
            for item in (features.get("enter_button_texts") or ())
            if str(item).strip()
        )
        leave = tuple(
            str(item)
            for item in (features.get("leave_button_texts") or ())
            if str(item).strip()
        )
        return enter, leave
    except Exception:  # noqa: BLE001
        return (), ()


def text_matches_age_leave(*parts: object) -> bool:
    """判断语义是否指向未满十八离开，禁止当作确认进入。"""

    joined = age_confirm_terms(*parts)
    if not joined.strip():
        return False
    _, json_leave = _age_gate_json_markers()
    markers = (*AGE_CONFIRM_LEAVE_MARKERS, *json_leave)
    return any(marker.casefold() in joined for marker in markers)


def text_matches_age_confirm(*parts: object) -> bool:
    """判断语义文本是否指向「我年满18岁-进入」类确认。"""

    # parts 约定：semantic_target, purpose, expected_state_hint
    semantic_target = parts[0] if parts else ""
    primary = age_confirm_terms(semantic_target)
    joined = age_confirm_terms(*parts)
    if not joined.strip():
        return False
    json_enter, json_leave = _age_gate_json_markers()
    leave_markers = (*AGE_CONFIRM_LEAVE_MARKERS, *json_leave)
    enter_markers = (*AGE_CONFIRM_MARKERS, *json_enter)
    # 离开键只看控件语义目标，避免 purpose 免责声明里的「未满／离开」误杀进入键。
    if primary and any(marker.casefold() in primary for marker in leave_markers):
        return False
    return any(marker.casefold() in joined for marker in enter_markers)


def is_age_confirm_candidate(
    *,
    action_type: InteractionOperation,
    risk_level: InteractionRiskLevel,
    semantic_target: str | None = None,
    purpose: object = None,
    expected_state_hint: object = None,
    metadata: Mapping[str, object] | None = None,
) -> bool:
    """当前截图上的年龄确认进入候选（允许 medium，拒绝 high／离开）。"""

    if action_type not in _AGE_CONFIRM_ACTION_TYPES:
        return False
    if risk_level not in _AGE_CONFIRM_RISKS:
        return False
    if text_matches_age_confirm(semantic_target, purpose, expected_state_hint):
        return True
    if metadata is None:
        return False
    try:
        from services.api.app.application.app_ui_tree_features import (
            candidate_matches_age_gate_enter,
        )
        from services.api.app.domain.exploration import CandidateAction

        probe = CandidateAction(
            candidate_id="age-gate-probe",
            semantic_target=str(semantic_target or ""),
            action_type=action_type,
            risk_level=risk_level,
            exploration_value=0,
            navigation_depth=0,
            metadata=dict(metadata),
        )
        return candidate_matches_age_gate_enter(probe)
    except Exception:  # noqa: BLE001
        return False


def observation_needs_age_confirm(
    *,
    page_state: object = None,
    page_type: object = None,
    surface_facts: Mapping[str, object] | None = None,
    observation_text: object = None,
) -> bool:
    """当前页面事实是否仍停在年龄确认门禁。"""

    facts = surface_facts or {}
    states = {
        str(page_state or "").casefold(),
        str(page_type or "").casefold(),
    }
    dialog_kind = str(facts.get("dialog_kind") or "").casefold()
    if (
        bool(states & {"auth_gate", "auth_required", "login_required", "requires_auth"})
        or dialog_kind in {"age_verification", "age_confirm", "age_gate"}
        or facts.get("popup_visible") is True
        or facts.get("dialog_visible") is True
        or bool(states & {"popup", "modal", "overlay", "dialog"})
    ):
        return True
    # 海角等：Vision 可能只给 stable＋文案，未标 dialog；用 APP JSON 上下文识别。
    text_parts = (
        observation_text,
        facts.get("visual_summary"),
        facts.get("visible_text"),
        facts.get("ocr_text"),
        facts.get("ui_tree_text"),
        facts.get("page_summary"),
    )
    try:
        from services.api.app.application.app_ui_tree_features import (
            observation_text_matches_age_gate_context,
        )

        if observation_text_matches_age_gate_context(*text_parts):
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def prefer_age_confirm_over_observe(
    *,
    page_state: object = None,
    page_type: object = None,
    surface_facts: Mapping[str, object] | None = None,
    candidates: Sequence[object],
    observation_text: object = None,
) -> bool:
    """是否应优先清年龄门禁，而不是继续 OBSERVE。"""

    if not observation_needs_age_confirm(
        page_state=page_state,
        page_type=page_type,
        surface_facts=surface_facts,
        observation_text=observation_text,
    ):
        return False
    for item in candidates:
        action_type = getattr(item, "action_type", None)
        risk_level = getattr(item, "risk_level", None)
        if not isinstance(action_type, InteractionOperation):
            continue
        if not isinstance(risk_level, InteractionRiskLevel):
            continue
        metadata = getattr(item, "metadata", {}) or {}
        if not isinstance(metadata, Mapping):
            metadata = {}
        if is_age_confirm_candidate(
            action_type=action_type,
            risk_level=risk_level,
            semantic_target=getattr(item, "semantic_target", None),
            purpose=metadata.get("purpose"),
            expected_state_hint=metadata.get("expected_state_hint"),
            metadata=metadata,
        ):
            return True
    return False
