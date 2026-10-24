"""App Explorer 候选资格的单一事实源。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from services.api.app.application.app_exploration_grayscale import (
    dedup_discovery_candidates_by_page_graph,
    milestone_policy_from_coverage_metadata,
    prioritize_candidates_by_page_graph,
    resolve_app_m5_grayscale_active,
    resolve_app_m5_schedules_from_graph,
)
from services.api.app.application.app_package_boundary import (
    work_within_app_package_boundary,
)
from services.api.app.application.bounded_exploration_scheduler import (
    BOUNDED_EXPLORATION_STATE_KEY,
    BoundedExplorationScheduler,
    scheduler_missing_page_types,
    scheduler_state_from_work,
)
from services.api.app.domain.exploration import (
    CandidateAction,
    ExplorationWork,
    SideEffectPolicy,
    build_action_fingerprint,
)
from services.api.app.domain.exploration.app_visual import NormalizedBounds
from services.api.app.domain.interaction import (
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)

APP_EXPLORATION_OPERATIONS = frozenset(
    {
        InteractionOperation.CLICK,
        InteractionOperation.TAP,
        InteractionOperation.SCROLL,
        InteractionOperation.BACK,
        InteractionOperation.OBSERVE,
        InteractionOperation.WARM_RESTORE,
    }
)
_READ_ONLY_APP_OPERATIONS = frozenset(
    {
        InteractionOperation.NAVIGATE,
        InteractionOperation.CLICK,
        InteractionOperation.TAP,
        InteractionOperation.HOVER,
        InteractionOperation.SCROLL,
        InteractionOperation.BACK,
        InteractionOperation.WARM_RESTORE,
        InteractionOperation.REFRESH,
        InteractionOperation.OBSERVE,
        InteractionOperation.TIMED_CAPTURE,
        InteractionOperation.SEEK_MEDIA,
    }
)
_PROHIBITED_APP_OPERATIONS = frozenset(
    {InteractionOperation.OBSERVE, InteractionOperation.TIMED_CAPTURE}
)
_OBSTRUCTION_MARKERS = (
    "close_popup",
    "close_ad_popup",
    "close_promo_popup",
    "close_promo_ad",
    "关闭弹窗",
    "关闭弹层",
    "关闭广告",
    "关闭推广",
    "关闭宣传",
    "跳过广告",
    "跳过开屏",
)


def eligible_app_candidates(
    work: ExplorationWork,
    *,
    allowed_operations: frozenset[InteractionOperation] | None = None,
    now: datetime | None = None,
) -> tuple[list[CandidateAction], dict[str, str]]:
    """按 App 的状态、边界、副作用与预算筛出真实可执行候选。"""

    if work.surface_kind not in {SurfaceKind.ANDROID, SurfaceKind.IOS}:
        return [], {}
    blocked_ids = set(work.frontier.blocked_candidate_ids)
    visited_candidate_ids = set(work.frontier.visited_candidate_ids)
    visited_action_fingerprints = set(work.frontier.visited_action_fingerprints)
    eligible: list[CandidateAction] = []
    rejected: dict[str, str] = {}
    for candidate in work.frontier.candidates:
        reason = _base_app_candidate_rejection_reason(
            work,
            candidate,
            blocked_ids=blocked_ids,
            visited_candidate_ids=visited_candidate_ids,
            visited_action_fingerprints=visited_action_fingerprints,
            allowed_operations=allowed_operations,
        )
        if reason is None:
            eligible.append(candidate)
        else:
            rejected[candidate.candidate_id] = reason
    try:
        state = scheduler_state_from_work(work)
        filtered = (
            BoundedExplorationScheduler().filter_candidates(
                eligible, state=state, now=now or work.frontier.observation.observed_at,
                missing_page_types=scheduler_missing_page_types(work, state),
            )
            if state is not None else tuple(eligible)
        )
        reason = "候选已触达有界调度的预算或循环限制"
    except (TypeError, ValueError):
        state = None
        filtered = ()
        reason = "Work 调度账本无效，禁止继续探索"
    allowed_ids = {candidate.candidate_id for candidate in filtered}
    rejected.update({
        item.candidate_id: reason for item in eligible if item.candidate_id not in allowed_ids
    })
    # M5：discovery 图去重；Lab authority 时再做图辅助优先排序。Gate／Coverage 权威不变。
    kept = list(filtered)
    metadata = work.frontier.observation.metadata
    meta_dict = metadata if isinstance(metadata, dict) else None
    milestone_policy = milestone_policy_from_coverage_metadata(meta_dict)
    if resolve_app_m5_grayscale_active(milestone_policy):
        graph = meta_dict.get("app_page_graph_snapshot") if meta_dict else None
        kept, suppressed = dedup_discovery_candidates_by_page_graph(
            kept,
            graph=graph if isinstance(graph, dict) else None,
            coverage_metadata=meta_dict,
            missing_page_types=scheduler_missing_page_types(work, state)
            if state is not None
            else (),
        )
        for item in suppressed:
            rejected[item["candidate_id"]] = item["reason"]
        if resolve_app_m5_schedules_from_graph(milestone_policy):
            kept, _boosts = prioritize_candidates_by_page_graph(
                kept,
                graph=graph if isinstance(graph, dict) else None,
                coverage_metadata=meta_dict,
                schedules_from_graph=True,
            )
    return kept, rejected


def count_eligible_app_candidates(work: ExplorationWork) -> int:
    """返回当前 App Work 真正可执行候选数，供终态与技术收口共用。"""

    return len(eligible_app_candidates(work)[0])


def is_app_candidate_eligible(
    work: ExplorationWork,
    candidate: CandidateAction,
) -> bool:
    """判断单个 App 候选是否仍可执行。"""

    if work.surface_kind not in {SurfaceKind.ANDROID, SurfaceKind.IOS}:
        return False
    return (
        app_candidate_rejection_reason(
            work,
            candidate,
            blocked_ids=set(work.frontier.blocked_candidate_ids),
            visited_candidate_ids=set(work.frontier.visited_candidate_ids),
            visited_action_fingerprints=set(work.frontier.visited_action_fingerprints),
        )
        is None
    )


def app_candidate_rejection_reason(
    work: ExplorationWork,
    candidate: CandidateAction,
    *,
    blocked_ids: set[str],
    visited_candidate_ids: set[str],
    visited_action_fingerprints: set[str],
    allowed_operations: frozenset[InteractionOperation] | None = None,
) -> str | None:
    """返回候选不可执行的确定性原因；``None`` 表示候选具备资格。"""

    reason = _base_app_candidate_rejection_reason(
        work, candidate,
        blocked_ids=blocked_ids,
        visited_candidate_ids=visited_candidate_ids,
        visited_action_fingerprints=visited_action_fingerprints,
        allowed_operations=allowed_operations,
    )
    if (
        reason is not None
        or BOUNDED_EXPLORATION_STATE_KEY not in work.frontier.observation.metadata
    ):
        return reason
    eligible, rejected = eligible_app_candidates(work, allowed_operations=allowed_operations)
    if any(item.candidate_id == candidate.candidate_id for item in eligible):
        return None
    return rejected.get(candidate.candidate_id, "候选不属于当前可执行 Frontier")


def _base_app_candidate_rejection_reason(
    work: ExplorationWork,
    candidate: CandidateAction,
    *,
    blocked_ids: set[str],
    visited_candidate_ids: set[str],
    visited_action_fingerprints: set[str],
    allowed_operations: frozenset[InteractionOperation] | None = None,
) -> str | None:
    """先做原有安全过滤，再按可执行候选计算调度额度借用。"""

    if candidate.candidate_id in blocked_ids:
        return "候选已被 Frontier 确定性阻断"
    if candidate.candidate_id in visited_candidate_ids:
        return "候选 ID 已访问"
    if (
        build_action_fingerprint(
            state_version=work.frontier.observation.state_version,
            candidate=candidate,
        )
        in visited_action_fingerprints
    ):
        return "动作指纹已访问"
    if candidate.action_type not in work.boundaries.allowed_operations:
        return "动作类型不在 allowed_operations"
    app_operations = (
        APP_EXPLORATION_OPERATIONS
        if allowed_operations is None
        else allowed_operations
    )
    if candidate.action_type not in app_operations:
        return "动作类型不在 App 固定 allowlist"
    if candidate.navigation_depth > min(
        work.budget.max_depth,
        work.boundaries.max_navigation_depth,
    ):
        return "导航深度超出预算或边界"
    if (
        candidate.expected_surface_kind is not None
        and candidate.expected_surface_kind is not work.surface_kind
    ):
        return "候选 Surface 与当前工作不一致"
    if not work_within_app_package_boundary(work) and candidate.action_type not in {
        InteractionOperation.BACK,
        InteractionOperation.WARM_RESTORE,
    }:
        return "当前 Surface 已超出 host 或 route 边界"
    navigation_rejection = _navigation_provenance_rejection_reason(candidate)
    if navigation_rejection is not None:
        return navigation_rejection
    if not _side_effect_allowed(work.boundaries.side_effect_policy, candidate):
        return "动作副作用或风险级别超出 side_effect_policy"
    uses_obstruction = _candidate_uses_obstruction_budget(candidate)
    charges_obstruction = (
        uses_obstruction and work.budget.resolved_max_obstruction_actions > 0
    )
    if charges_obstruction and work.budget.resolved_remaining_obstruction_actions == 0:
        return "清障动作预算已耗尽"
    if (
        not charges_obstruction
        and candidate.action_type
        not in {InteractionOperation.OBSERVE, InteractionOperation.TIMED_CAPTURE}
        and work.budget.resolved_remaining_business_actions == 0
    ):
        return "业务动作预算已耗尽"
    from services.api.app.application.app_semantic_progress import (
        short_drama_reentry_candidate_blocked,
    )

    metadata = work.frontier.observation.metadata
    meta_map = metadata if isinstance(metadata, Mapping) else None
    if short_drama_reentry_candidate_blocked(
        candidate,
        coverage_metadata=meta_map,
    ):
        return "短剧已播完／待退出：禁止再点查看完整／解锁／重播进播放"
    return None


def _navigation_provenance_rejection_reason(candidate: CandidateAction) -> str | None:
    """导航真实性是共享准入条件，不随动态模式或课纲完成状态改变。"""

    if candidate.metadata.get("coverage_page_seek_contract") is not None:
        return "旧合成寻页合同缺少当前视觉入口，禁止执行坐标重放"
    if candidate.metadata.get("bottom_navigation_tab_seek") is not True:
        return None
    raw = candidate.metadata.get("normalized_bounds")
    if not isinstance(raw, Mapping):
        return "底栏候选缺少当前帧几何，不能仅凭底栏标记执行"
    try:
        bounds = NormalizedBounds.from_dict(raw)
    except (TypeError, ValueError):
        return "底栏候选的当前帧几何无效"
    if bounds.top < 0.78:
        return "底栏标记与当前帧几何不一致；真实顶栏应使用顶栏角色"
    return None


def _side_effect_allowed(
    policy: SideEffectPolicy,
    candidate: CandidateAction,
) -> bool:
    approval_granted = candidate.metadata.get("approval_granted") is True
    if candidate.risk_level is InteractionRiskLevel.HIGH and not approval_granted:
        return False
    if policy is SideEffectPolicy.PROHIBITED:
        return candidate.action_type in _PROHIBITED_APP_OPERATIONS
    if policy is SideEffectPolicy.READ_ONLY:
        return candidate.action_type in _READ_ONLY_APP_OPERATIONS
    if policy is SideEffectPolicy.APPROVAL_REQUIRED:
        return approval_granted
    return True


def _candidate_uses_obstruction_budget(candidate: CandidateAction) -> bool:
    """与 App 动作后的清障分账口径对齐。"""

    if candidate.metadata.get("package_boundary_recovery") is True:
        return False
    if candidate.metadata.get("app_ad_landing_consistency") is True:
        return True
    if candidate.metadata.get("surface_phase_key") is not None:
        return True
    if candidate.metadata.get("phase_key") is not None:
        return True
    if candidate.action_type not in {
        InteractionOperation.CLICK,
        InteractionOperation.TAP,
    }:
        return False
    terms = " ".join(
        (
            candidate.semantic_target,
            str(candidate.metadata.get("purpose") or ""),
            str(candidate.metadata.get("expected_state_hint") or ""),
        )
    ).casefold()
    return any(marker.casefold() in terms for marker in _OBSTRUCTION_MARKERS)
