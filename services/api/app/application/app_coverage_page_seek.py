"""App 动态覆盖寻页：只提升当前 Capture 已确认的真实视觉候选。

保留截图探索、全局 Frontier 和交互运行时所需的兼容入口，但不再按包名、
预设标签或等分栏位生成点击候选，也不再重放旧的 synthetic 坐标合同。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

from services.api.app.application.app_navigation_entry_provenance import (
    DISCOVERY_SOURCE_PROMOTED_BOTTOM_NAV,
    DISCOVERY_SOURCE_PROMOTED_COVERAGE,
    DISCOVERY_SOURCE_PROMOTED_HOME_GRID,
    DISCOVERY_SOURCE_PROMOTED_HOME_TOP_TAB,
    DISCOVERY_SOURCE_UIA_ISSUED,
    DISCOVERY_SOURCE_VISION,
    is_business_navigation_entry,
    tag_candidate_action_provenance,
)
from services.api.app.application.app_obstruction_controls import is_obstructed_surface
from services.api.app.application.app_semantic_progress import (
    bottom_tab_label_already_visited,
    should_suppress_redundant_app_candidate,
)
from services.api.app.application.app_visual_bottom_nav_calibration import (
    extract_pixel_bottom_nav_tabs,
)
from services.api.app.domain.exploration import (
    AppVisualAnalysis,
    AppVisualCandidate,
    CandidateAction,
    NormalizedBounds,
)
from services.api.app.domain.interaction import (
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)
from services.api.app.domain.page.registry import get_default_page_type_registry

_PAGE_TYPE_REGISTRY = get_default_page_type_registry()

# 旧版本常量仍供现有导入方识别历史数据；新代码不再生成该版本合同。
APP_COVERAGE_PAGE_SEEK_VERSION = "app-coverage-page-seek.v1"

_COVERAGE_SEEK_BLOCKING_STATES = frozenset(
    {
        "auth_gate",
        "auth_required",
        "login_required",
        "requires_auth",
        "startup_ad",
        "countdown",
        "loading",
        "line_loading",
        "permission_dialog",
        "system_dialog",
        "browser_permission",
    }
)
_COVERAGE_OVERLAY_STATES = frozenset({"popup", "modal", "overlay", "dialog"})
_COVERAGE_SEEK_TAB_PAGE_TYPES = frozenset(
    {"home", "content_list", "video_list", "post_list", "activity", "profile"}
)
_COVERAGE_SEEK_DETAIL_PAGE_TYPES = frozenset({"content_detail", "video_detail"})

# 仅用于 Frontier 排序，不描述任何 App 的栏位、标签或点击坐标。
_COVERAGE_PAGE_PRIORITY: tuple[str, ...] = (
        "content_list",
        "video_list",
        "activity",
    "post_list",
        "profile",
)

# 弹层帧计数键（与截图探索 coverage_metadata 对齐）。
DYNAMIC_HOME_POPUP_FRAME_COUNT = "dynamic_home_popup_frame_count"
DYNAMIC_BUSINESS_TAB_OVERLAY_FRAME_COUNT = "dynamic_business_tab_overlay_frame_count"
# 连续关层却未扩大业务覆盖：弹窗轮播会重置 same_popup，此计数不随签名变化清零。
CLEARANCE_WITHOUT_BUSINESS_PROGRESS_COUNT = (
    "clearance_without_business_progress_count"
)
# 前几帧仍优先关层（避免点穿广告）；反复关不掉后才让真实底栏／分栏目寻页穿透。
APP_OVERLAY_YIELD_TO_REAL_SEEK_FRAMES = 6
# 覆盖缺口未完且清障已有压力时，可提早让路（仍须满至少本阈值帧，避免首屏即点穿）。
APP_OVERLAY_EARLY_YIELD_FRAMES = 3
# 连续清障动作达到此值且覆盖未完：强制让路，不依赖「关 N 次就停」。
APP_CLEARANCE_FORCE_YIELD_COUNT = 4
_HOME_POPUP_HOST_PAGE_TYPES = frozenset({"home", ""})
_BUSINESS_TAB_OVERLAY_HOST_PAGE_TYPES = frozenset(
    {
    "content_list",
    "video_list",
    "post_list",
        "activity",
    "profile",
    }
)


def _nonneg_meta_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def overlay_budget_allows_real_coverage_seek(
    *,
    page_type: str | None,
    coverage_metadata: Mapping[str, object] | None,
) -> bool:
    """业务底栏页弹层反复不清时，允许真实（非合成）覆盖／分栏目寻页穿透。

    满帧（默认 6）始终让路。覆盖缺口未完时，另认清障无业务进展／noop／
    同层空点等压力信号提早让路——弹窗轮播换图不得无限占用探索预算。
    """

    if not isinstance(coverage_metadata, Mapping):
        return False
    normalized = str(page_type or "").strip().casefold()
    if normalized in _HOME_POPUP_HOST_PAGE_TYPES:
        frames = coverage_metadata.get(DYNAMIC_HOME_POPUP_FRAME_COUNT)
    elif normalized in _BUSINESS_TAB_OVERLAY_HOST_PAGE_TYPES:
        frames = coverage_metadata.get(DYNAMIC_BUSINESS_TAB_OVERLAY_FRAME_COUNT)
    else:
        return False
    frame_count = _nonneg_meta_int(frames)
    if frame_count is not None and frame_count >= APP_OVERLAY_YIELD_TO_REAL_SEEK_FRAMES:
        return True
    incomplete = coverage_incomplete_allows_bottom_nav_through_overlay(
        coverage_metadata=coverage_metadata,
    )
    if not incomplete:
        return False
    clearance = _nonneg_meta_int(
        coverage_metadata.get(CLEARANCE_WITHOUT_BUSINESS_PROGRESS_COUNT)
    )
    if clearance is not None and clearance >= APP_CLEARANCE_FORCE_YIELD_COUNT:
        return True
    noop = _nonneg_meta_int(coverage_metadata.get("dismiss_noop_streak"))
    same_attempts = _nonneg_meta_int(
        coverage_metadata.get("surface_phase_same_popup_attempt_count")
    )
    pressure = (
        (noop is not None and noop >= 2)
        or (same_attempts is not None and same_attempts >= 2)
        or (clearance is not None and clearance >= 3)
    )
    if not pressure:
        return False
    # 首屏仍给至少 EARLY 帧关层；noop／同层空点满阈值时可无视帧数（已证明关不动）。
    hard_waste = (noop is not None and noop >= 2) or (
        same_attempts is not None and same_attempts >= 2
    )
    if hard_waste:
        return True
    return frame_count is not None and frame_count >= APP_OVERLAY_EARLY_YIELD_FRAMES


def coverage_incomplete_allows_bottom_nav_through_overlay(
    *,
    coverage_metadata: Mapping[str, object] | None,
) -> bool:
    """覆盖分母仍有缺口，或底栏巡检尚未截图留证完时，弹层帧可提早用真实底栏寻页。"""

    if not isinstance(coverage_metadata, Mapping):
        return False
    required = coverage_seek_required_page_types(coverage_metadata)
    covered = {
        item.casefold() for item in _text_tuple(coverage_metadata.get("covered_page_types"))
    }
    if required and required - covered:
        return True
    return playbook_bottom_tabs_hard_incomplete(coverage_metadata=coverage_metadata)


def _playbook_bottom_tab_labels(
    coverage_metadata: Mapping[str, object] | None,
    *,
    package_name: str | None = None,
    apk_filename: str | None = None,
) -> set[str]:
    """从课纲 playbook／UI Tree 特征读取应硬覆盖的底栏标签。"""

    ordered = _playbook_bottom_tab_labels_ordered(
        coverage_metadata,
        package_name=package_name,
        apk_filename=apk_filename,
    )
    return set(ordered)


def _playbook_bottom_tab_labels_ordered(
    coverage_metadata: Mapping[str, object] | None,
    *,
    package_name: str | None = None,
    apk_filename: str | None = None,
) -> tuple[str, ...]:
    """按 playbook.tabs／产品特征 JSON 顺序返回归一化底栏标签。"""

    if not isinstance(coverage_metadata, Mapping):
        coverage_metadata = {}
    raw_progress = coverage_metadata.get("app_curriculum_progress")
    playbook_id: str | None = None
    if isinstance(raw_progress, Mapping):
        raw_id = raw_progress.get("playbook_id")
        if isinstance(raw_id, str) and raw_id.strip():
            playbook_id = raw_id.strip()
    resolved_pkg = package_name or coverage_metadata.get("package_name")
    resolved_apk = apk_filename or coverage_metadata.get("apk_filename")
    pkg = resolved_pkg.strip() if isinstance(resolved_pkg, str) else None
    apk = resolved_apk.strip() if isinstance(resolved_apk, str) else None

    # 优先产品 UI Tree 特征（hjll／hlapp 独立 JSON）
    try:
        from services.api.app.application.app_ui_tree_features import (
            ordered_bottom_tab_labels,
            resolve_ui_tree_playbook_id,
        )

        resolved = resolve_ui_tree_playbook_id(
            playbook_id=playbook_id,
            package_name=pkg,
        )
        if resolved:
            feature_labels = ordered_bottom_tab_labels(
                playbook_id=resolved,
                package_name=pkg,
            )
            ordered_feat: list[str] = []
            seen_feat: set[str] = set()
            for tab in feature_labels:
                normalized = _normalize_bottom_tab_label(tab)
                if not normalized or normalized in seen_feat:
                    continue
                seen_feat.add(normalized)
                ordered_feat.append(normalized)
            if ordered_feat:
                return tuple(ordered_feat)
    except Exception:  # noqa: BLE001
        pass

    playbook = None
    if playbook_id:
        try:
            from services.api.app.application.app_script_curriculum import (
                resolve_playbook_by_id,
            )

            playbook = resolve_playbook_by_id(playbook_id)
        except Exception:  # noqa: BLE001
            playbook = None
    if playbook is None:
        try:
            from services.api.app.application.app_capture_playbook import (
                resolve_app_capture_playbook,
            )

            if apk or pkg:
                playbook = resolve_app_capture_playbook(
                    apk_filename=apk,
                    package_name=pkg,
                )
        except Exception:  # noqa: BLE001
            playbook = None
    if playbook is None:
        return ()
    ordered: list[str] = []
    seen: set[str] = set()
    for tab in playbook.tabs:
        normalized = _normalize_bottom_tab_label(tab)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return tuple(ordered)


def playbook_bottom_tabs_hard_incomplete(
    *,
    coverage_metadata: Mapping[str, object] | None,
) -> bool:
    """判断任务课纲／flow contract 的底栏硬覆盖是否未完。"""

    if not isinstance(coverage_metadata, Mapping):
        return False
    policy = coverage_metadata.get("app_runtime_policy")
    if isinstance(policy, Mapping) and policy.get("exploration_mode") == "dynamic_frontier":
        # 动态任务依赖真实 Frontier 和规则缺口，不接受旧课纲的强制巡栏顺序。
        return False
    visited = _visited_bottom_navigation_labels(coverage_metadata)
    package_name = coverage_metadata.get("package_name")
    apk_filename = coverage_metadata.get("apk_filename")
    expected_labels = _playbook_bottom_tab_labels(
        coverage_metadata,
        package_name=str(package_name).strip() if isinstance(package_name, str) else None,
        apk_filename=str(apk_filename).strip() if isinstance(apk_filename, str) else None,
    )
    if expected_labels and any(
        not bottom_tab_label_already_visited(label, visited) for label in expected_labels
    ):
        return True
    flow = coverage_metadata.get("app_flow_contract")
    if isinstance(flow, Mapping) and flow.get("bottom_tabs_satisfied") is False:
        missing = flow.get("missing_bottom_tabs")
        if isinstance(missing, (list, tuple)) and any(
            isinstance(item, str) and item.strip() for item in missing
        ):
            return True
    return False


def candidate_targets_detail_page(candidate: CandidateAction) -> bool:
    """候选是否指向详情／付费类页（会从根页钻入后挡住底栏）。"""

    expected = str(
        candidate.metadata.get("expected_next_page_type")
        or candidate.metadata.get("expected_state_hint")
        or ""
    ).strip().casefold()
    if expected in _COVERAGE_SEEK_DETAIL_PAGE_TYPES | {"payment"}:
        return True
    if expected.startswith("tab:"):
        return False
    terms = " ".join(
        (
            str(candidate.semantic_target or ""),
            str(candidate.metadata.get("purpose") or ""),
            expected,
        )
    ).casefold()
    return any(
        marker in terms
        for marker in (
            "详情",
            "detail",
            "播放",
            "开通",
            "付费",
            "paywall",
            "payment",
            "vip",
            "解锁",
        )
)


def missing_required_page_tab_rank(page_type: str | None) -> int:
    """返回缺失页型的稳定排序值；未知页型不获得预设导航优先级。"""

    if not isinstance(page_type, str) or not page_type.strip():
        return len(_COVERAGE_PAGE_PRIORITY) + 1
    normalized = page_type.strip().casefold()
    try:
        return _COVERAGE_PAGE_PRIORITY.index(normalized)
    except ValueError:
        return len(_COVERAGE_PAGE_PRIORITY)


def missing_required_page_tab_boost(page_type: str | None) -> int:
    """返回缺失页型的排序增益；未知页型和空值不获得增益。"""

    return max(0, len(_COVERAGE_PAGE_PRIORITY) - missing_required_page_tab_rank(page_type))


def uncovered_required_page_rank(
    expected_page_type: str | None,
    *,
    required: set[str],
    covered: set[str],
) -> int:
    """为当前任务中仍缺失的页型提供排序值。"""

    if not isinstance(expected_page_type, str) or not expected_page_type.strip():
        return 0
    normalized = expected_page_type.strip().casefold()
    required_normalized = {item.casefold() for item in required if isinstance(item, str)}
    covered_normalized = {item.casefold() for item in covered if isinstance(item, str)}
    if normalized not in required_normalized - covered_normalized:
        return 0
    return 1 + missing_required_page_tab_boost(normalized)


def _text_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(
        item for item in value if isinstance(item, str) and item.strip() and item == item.strip()
    )


def coverage_seek_required_page_types(
    coverage_metadata: Mapping[str, object],
) -> set[str]:
    """只读取当前任务声明的覆盖分母，不从页面目录推导缺口。"""

    def _from_sequence(value: object) -> set[str]:
        return {item.casefold() for item in _text_tuple(value)}

    # 显式字段即使为空也表示当前任务没有可寻页分母，避免继续回退到别的
    # 模板或全局目录。
    if "required_page_types" in coverage_metadata:
        return _from_sequence(coverage_metadata.get("required_page_types"))

    policy = coverage_metadata.get("app_runtime_policy")
    if not isinstance(policy, Mapping):
        return set()
    snapshot = policy.get("app_target_snapshot")
    if isinstance(snapshot, Mapping) and "required_page_types" in snapshot:
        return _from_sequence(snapshot.get("required_page_types"))
    if "required_page_types" in policy:
        return _from_sequence(policy.get("required_page_types"))
    return set()


def _candidate_expected_page_type(candidate: CandidateAction) -> str:
    expected = candidate.metadata.get("expected_next_page_type")
    if not isinstance(expected, str) or not expected.strip():
        expected = candidate.metadata.get("expected_state_hint")
    return expected.strip().casefold() if isinstance(expected, str) else ""


def _known_page_type_from_hint(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    raw = value.strip().casefold()
    for item in (raw, *raw.replace(":", " ").replace("/", " ").split()):
        if item in _PAGE_TYPE_REGISTRY.all_codes:
            return item
        alias = _PAGE_TYPE_REGISTRY.resolve_alias(item)
        if alias is not None:
            return alias.canonical_code
        normalized = item.replace("-", "_").replace(" ", "_")
        if normalized in _PAGE_TYPE_REGISTRY.all_codes:
            return normalized
    return ""


def _is_loading_safe_close_candidate(candidate: CandidateAction) -> bool:
    if (
        candidate.action_type not in {InteractionOperation.CLICK, InteractionOperation.TAP}
        or candidate.risk_level is not InteractionRiskLevel.LOW
    ):
        return False
    if candidate.metadata.get("popup_close_corner_fallback") is True:
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
            "close_popup",
            "close_overlay",
            "close_popup_x",
            "dismiss popup",
            "dismiss overlay",
            "关闭弹窗",
            "关闭弹层",
            "关闭遮挡",
            "关闭当前遮挡",
            "关闭按钮",
        )
    )


def _is_loading_visible_tab_candidate(candidate: CandidateAction) -> bool:
    if (
        candidate.action_type not in {InteractionOperation.CLICK, InteractionOperation.TAP}
        or candidate.risk_level is not InteractionRiskLevel.LOW
        or _known_page_type_from_hint(_candidate_expected_page_type(candidate))
        not in _COVERAGE_SEEK_TAB_PAGE_TYPES
    ):
        return False
    terms = " ".join(
        (
            str(candidate.semantic_target or ""),
            str(candidate.metadata.get("purpose") or ""),
            str(candidate.metadata.get("expected_state_hint") or ""),
            str(candidate.metadata.get("target_keywords") or ""),
        )
    ).casefold()
    return (
        any(
        marker in terms
        for marker in (
            "底栏",
            "底部导航",
            "bottom_navigation",
            "bottom navigation",
            "tab:",
            "导航",
        )
        )
        or candidate.metadata.get("frontier_scope") == "app"
    )


def _is_loading_post_publish_branch(
    *,
    candidate: CandidateAction,
    analysis: AppVisualAnalysis,
    overlay_blocked: bool,
) -> bool:
    if not overlay_blocked or _candidate_expected_page_type(candidate) != "post_publish":
        return False
    if str(analysis.visual_page_type or "").strip().casefold() not in {
        "post_list",
        "content_list",
    }:
        return False
    terms = " ".join(
        (
            str(candidate.semantic_target or ""),
            str(candidate.metadata.get("purpose") or ""),
        )
    ).casefold()
    return any(marker in terms for marker in ("发帖", "图文帖子", "视频帖子", "post"))


def _filter_loading_candidates(
    *,
    candidates: tuple[CandidateAction, ...],
    analysis: AppVisualAnalysis,
    overlay_blocked: bool,
) -> tuple[CandidateAction, ...]:
    """加载期间只保留观察、恢复、清障和当前帧明确的安全分支。"""

    page_type = str(analysis.visual_page_type or "").strip().casefold()
    has_loading_business_surface = page_type in _COVERAGE_SEEK_TAB_PAGE_TYPES
    retained: list[CandidateAction] = []
    for candidate in candidates:
        expected = _candidate_expected_page_type(candidate)
        if candidate.action_type in {
            InteractionOperation.OBSERVE,
            InteractionOperation.BACK,
            InteractionOperation.WARM_RESTORE,
            InteractionOperation.TIMED_CAPTURE,
        }:
            retained.append(candidate)
            continue
        if _is_loading_safe_close_candidate(candidate):
            retained.append(candidate)
            continue
        if _is_loading_post_publish_branch(
            candidate=candidate,
            analysis=analysis,
            overlay_blocked=overlay_blocked,
        ):
            retained.append(candidate)
            continue
        if (
            has_loading_business_surface
            and not overlay_blocked
            and _is_loading_visible_tab_candidate(candidate)
        ):
            retained.append(candidate)
            continue
        # 加载中的详情和普通业务点击必须等待稳定截图。
        if (
            _known_page_type_from_hint(expected) in _COVERAGE_SEEK_DETAIL_PAGE_TYPES
            or has_loading_business_surface
        ):
            continue
        retained.append(candidate)
    return tuple(retained)


def _has_control_role(
    candidate: CandidateAction,
    visual_candidate: AppVisualCandidate | None = None,
) -> bool:
    return candidate.metadata.get("control_role") in {"dismiss", "skip"} or (
        visual_candidate is not None and visual_candidate.control_role in {"dismiss", "skip"}
    )


def _current_visual_candidates(
    analysis: AppVisualAnalysis,
) -> dict[str, AppVisualCandidate]:
    """索引当前截图的视觉候选，并再次核对源截图身份。"""

    raw_candidates = getattr(analysis, "candidates", ())
    if not isinstance(raw_candidates, (list, tuple)):
        return {}
    result: dict[str, AppVisualCandidate] = {}
    source_fields = (
        ("source_capture_id", "capture_id"),
        ("source_image_signature", "image_signature"),
        ("source_width", "width"),
        ("source_height", "height"),
        ("source_orientation", "orientation"),
    )
    for item in raw_candidates:
        if not isinstance(item, AppVisualCandidate):
            continue
        if any(
            getattr(analysis, analysis_field, None) is not None
            and getattr(item, candidate_field) != getattr(analysis, analysis_field)
            for candidate_field, analysis_field in source_fields
        ):
            continue
        result[item.candidate_id] = item
    return result


def _is_current_visual_candidate(
    *,
    candidate: CandidateAction,
    visual_candidate: AppVisualCandidate | None,
    analysis: AppVisualAnalysis,
) -> bool:
    if visual_candidate is None:
        return False
    if candidate.metadata.get("candidate_id") not in {None, candidate.candidate_id}:
        return False
    source_capture_id = candidate.metadata.get("source_capture_id")
    if source_capture_id is not None and source_capture_id != analysis.capture_id:
        return False
    source_scope = candidate.metadata.get("frontier_scope")
    if source_scope is not None and source_scope != visual_candidate.frontier_scope:
        return False
    return (
        visual_candidate.action_type is candidate.action_type
        and visual_candidate.risk_level is candidate.risk_level
        and visual_candidate.semantic_name == candidate.semantic_target
        and visual_candidate.confidence >= 0.70
    )


def _promotable_page_type(
    *,
    candidate: CandidateAction,
    visual_candidate: AppVisualCandidate | None,
    analysis: AppVisualAnalysis,
    missing: set[str],
) -> str | None:
    if not _is_current_visual_candidate(
        candidate=candidate,
        visual_candidate=visual_candidate,
        analysis=analysis,
    ):
        return None
    if (
        visual_candidate is None
        or candidate.action_type not in {InteractionOperation.CLICK, InteractionOperation.TAP}
        or candidate.risk_level is not InteractionRiskLevel.LOW
        or visual_candidate.normalized_bounds is None
        or _has_control_role(candidate, visual_candidate)
    ):
        return None
    expected = _known_page_type_from_hint(_candidate_expected_page_type(candidate))
    source_expected = _known_page_type_from_hint(visual_candidate.expected_state_hint)
    if not expected or expected not in missing or source_expected != expected:
        return None
    return expected


def _without_coverage_markers(candidate: CandidateAction) -> CandidateAction:
    metadata = dict(candidate.metadata)
    changed = False
    for key in (
        "coverage_page_seek",
        "coverage_page_seek_contract",
        "coverage_parent_restore",
    ):
        if key in metadata:
            metadata.pop(key)
            changed = True
    return replace(candidate, metadata=metadata) if changed else candidate


def _drop_legacy_synthetic_candidates(
    candidates: tuple[CandidateAction, ...],
) -> tuple[CandidateAction, ...]:
    """旧合同没有当前视觉事实，直接丢弃以阻止坐标重放。"""

    return tuple(
        candidate
        for candidate in candidates
        if candidate.metadata.get("coverage_page_seek_contract") is None
    )


def _sanitize_existing_coverage_markers(
    *,
    candidates: tuple[CandidateAction, ...],
    visual_candidates: Mapping[str, AppVisualCandidate],
) -> tuple[CandidateAction, ...]:
    result: list[CandidateAction] = []
    for candidate in candidates:
        if candidate.metadata.get("coverage_page_seek") is not True:
            result.append(candidate)
            continue
        visual_candidate = visual_candidates.get(candidate.candidate_id)
        if visual_candidate is None or _has_control_role(candidate, visual_candidate):
            result.append(_without_coverage_markers(candidate))
        else:
            result.append(candidate)
    return tuple(result)


def _nested_surface_proven(analysis: AppVisualAnalysis) -> bool:
    facts = analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else {}
    page_name = facts.get("visual_page_name")
    return isinstance(page_name, Mapping) and str(
        page_name.get("source") or ""
    ).strip().casefold() in {
        "top_bar",
        "page_landmark",
    }


def _root_tab_surface_proven(analysis: AppVisualAnalysis) -> bool:
    facts = analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else {}
    controls = facts.get("trusted_accessibility_controls")
    if isinstance(controls, Mapping):
        navigation = controls.get("bottom_navigation")
        if isinstance(navigation, (list, tuple)) and navigation:
            return True
    current = str(getattr(analysis, "visual_page_type", "") or "").strip().casefold()
    if current == "home":
        return True
    page_name = facts.get("visual_page_name")
    return (
        isinstance(page_name, Mapping)
        and str(page_name.get("source") or "").strip().casefold() == "selected_tab"
    )


def _is_parent_restore_candidate(candidate: CandidateAction) -> bool:
    if (
        candidate.action_type not in {InteractionOperation.CLICK, InteractionOperation.BACK}
        or candidate.risk_level is not InteractionRiskLevel.LOW
        or _has_control_role(candidate)
    ):
        return False
    terms = " ".join(
        (
            str(candidate.semantic_target or ""),
            str(candidate.metadata.get("purpose") or ""),
            str(candidate.metadata.get("expected_state_hint") or ""),
        )
    ).casefold()
    return any(marker in terms for marker in ("返回", "back", "上一页", "上一级"))


def _promote_parent_restore_candidate(
    *,
    candidates: tuple[CandidateAction, ...],
    analysis: AppVisualAnalysis,
    visual_candidates: Mapping[str, AppVisualCandidate],
    coverage_metadata: Mapping[str, object],
) -> tuple[CandidateAction, ...] | None:
    """嵌套页只提升当前截图已有的低风险返回入口。"""

    for candidate in candidates:
        visual_candidate = visual_candidates.get(candidate.candidate_id)
        if (
            not _is_parent_restore_candidate(candidate)
            or _has_control_role(candidate, visual_candidate)
            or not _is_current_visual_candidate(
                candidate=candidate,
                visual_candidate=visual_candidate,
                analysis=analysis,
            )
        ):
            continue
        if candidate.action_type is InteractionOperation.CLICK and (
            visual_candidate is None or visual_candidate.normalized_bounds is None
        ):
            continue
        promoted = replace(
            candidate,
            exploration_value=max(candidate.exploration_value, 99),
            metadata={
                **dict(candidate.metadata),
                "frontier_scope": "page",
                "coverage_page_seek": True,
                "coverage_parent_restore": True,
            },
        )
        if should_suppress_redundant_app_candidate(
            candidate=promoted,
            coverage_metadata=coverage_metadata,
        ):
            continue
        return (
            promoted,
            *(item for item in candidates if item.candidate_id != candidate.candidate_id),
        )
    return None


def append_coverage_page_seek_candidate(
    *,
    candidates: tuple[CandidateAction, ...],
    analysis: AppVisualAnalysis,
    navigation_depth: int,
    surface_kind: SurfaceKind,
    allowed_hosts: tuple[str, ...],
    coverage_metadata: Mapping[str, object] | None,
) -> tuple[CandidateAction, ...]:
    """按当前截图的真实候选提升覆盖缺口，不创建新的触控几何。"""

    del navigation_depth, surface_kind
    candidates = _drop_legacy_synthetic_candidates(candidates)
    visual_candidates = _current_visual_candidates(analysis)
    candidates = _sanitize_existing_coverage_markers(
        candidates=candidates,
        visual_candidates=visual_candidates,
    )

    page_state = str(getattr(analysis, "page_state", "") or "").strip().casefold()
    facts = analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else {}
    overlay_blocked = (
        is_obstructed_surface(
            page_state=None,
            page_type=analysis.visual_page_type,
            overlay_signature=analysis.overlay_signature,
            surface_facts=facts,
        )
        or page_state in _COVERAGE_OVERLAY_STATES
    )
    if page_state == "loading":
        candidates = _filter_loading_candidates(
            candidates=candidates,
            analysis=analysis,
            overlay_blocked=overlay_blocked,
        )
    yield_through_overlay = overlay_budget_allows_real_coverage_seek(
        page_type=getattr(analysis, "visual_page_type", None),
        coverage_metadata=coverage_metadata if isinstance(coverage_metadata, Mapping) else None,
    )
    # unknown/loading 只能保留已有的安全动作；业务入口必须在稳定、无遮挡
    # 的当前截图中提升。弹层反复关不掉时例外：提升真实底栏／缺口入口以穿透。
    if page_state != "stable" and not (
        yield_through_overlay and page_state in _COVERAGE_OVERLAY_STATES
    ):
        return candidates
    if not isinstance(coverage_metadata, Mapping):
        return candidates
    package_name = getattr(analysis, "package_name", None)
    if allowed_hosts and package_name not in allowed_hosts:
        return candidates
    if overlay_blocked and not yield_through_overlay:
        return candidates
    required = coverage_seek_required_page_types(coverage_metadata)
    if not required:
        return candidates
    covered = {item.casefold() for item in _text_tuple(coverage_metadata.get("covered_page_types"))}
    missing = required - covered
    if not missing:
        return candidates

    tabs_hard_incomplete = playbook_bottom_tabs_hard_incomplete(
        coverage_metadata=coverage_metadata,
    )

    if _nested_surface_proven(analysis) and not _root_tab_surface_proven(analysis):
        restored = _promote_parent_restore_candidate(
            candidates=candidates,
            analysis=analysis,
            visual_candidates=visual_candidates,
            coverage_metadata=coverage_metadata,
        )
        return restored if restored is not None else candidates

    promoted: list[CandidateAction] = []
    journey_target = str(
        coverage_metadata.get("app_journey_next_semantic_target") or ""
    ).strip()
    for candidate in candidates:
        if (
            len(journey_target) >= 2
            and journey_target.casefold() in candidate.semantic_target.casefold()
            and candidate.action_type in {InteractionOperation.CLICK, InteractionOperation.TAP}
        ):
            promoted.append(
                replace(
                    candidate,
                    exploration_value=max(candidate.exploration_value, 97),
                    metadata={
                        **dict(candidate.metadata),
                        "journey_step": True,
                        "coverage_page_seek": True,
                    },
                )
            )
            continue
        expected = _promotable_page_type(
            candidate=candidate,
            visual_candidate=visual_candidates.get(candidate.candidate_id),
            analysis=analysis,
            missing=missing,
        )
        if expected is None:
            promoted.append(candidate)
            continue
        # 底栏硬覆盖未完：禁止提升详情／付费入口，否则 home→详情→BACK 空转烧预算。
        if tabs_hard_incomplete and expected in _COVERAGE_SEEK_DETAIL_PAGE_TYPES | {
            "payment"
        }:
            promoted.append(candidate)
            continue
        # 业务覆盖提升：缺来源 Capture 或非法 discovery_source 不得进入导航队列。
        prior_source = (
            candidate.metadata.get("discovery_source")
            or (
                visual_candidates.get(candidate.candidate_id).discovery_source
                if visual_candidates.get(candidate.candidate_id) is not None
                else None
            )
            or DISCOVERY_SOURCE_VISION
        )
        if not is_business_navigation_entry(
            source_capture_id=candidate.metadata.get("source_capture_id")
            or (
                visual_candidates[candidate.candidate_id].source_capture_id
                if candidate.candidate_id in visual_candidates
                else None
            ),
            discovery_source=prior_source,
        ):
                promoted.append(candidate)
                continue
        metadata = tag_candidate_action_provenance(
            {
                **dict(candidate.metadata),
                "coverage_page_seek": True,
                "expected_next_page_type": expected,
            },
            discovery_source=DISCOVERY_SOURCE_PROMOTED_COVERAGE,
            source_capture_id=str(
                candidate.metadata.get("source_capture_id")
                or (
                    visual_candidates[candidate.candidate_id].source_capture_id
                    if candidate.candidate_id in visual_candidates
                    else ""
                )
            )
            or None,
        )
        promoted_candidate = replace(
                    candidate,
                    exploration_value=max(candidate.exploration_value, 96),
            metadata=metadata,
        )
        if should_suppress_redundant_app_candidate(
            candidate=promoted_candidate,
            coverage_metadata=coverage_metadata,
        ):
            continue
        promoted.append(promoted_candidate)
    return tuple(promoted)


_HOME_CATEGORY_HOST_PAGE_TYPES = frozenset({"home", "video_list", "content_list"})
_HOME_CATEGORY_POSITIVE_MARKERS = (
    "宫格",
    "频道入口",
    "栏目",
    "分栏",
    "分栏目",
    "分类入口",
    "打开内容入口",
    "打开分类内容",
    "打开对应视频",
    "打开频道",
    "打开直播入口",
    "打开网格入口",
    "网格入口",
    "进入频道",
)
_HOME_CATEGORY_NAME_PREFIXES = (
    "宫格",
    "入口_",
    "入口",
    "频道入口",
    "栏目",
    "分类入口",
)
_HOME_CATEGORY_NEGATIVE_MARKERS = (
    "底栏",
    "底部导航",
    "bottom navigation",
    "bottom_navigation",
    "推荐视频",
    "普通内容卡片",
    "内容卡片",
    "搜索框",
    "观察",
    "滚动",
    "关闭弹窗",
    "关闭广告",
    "跳过",
    "筛选",
    "分类tab",
    "顶栏",
    "切换筛选",
)


def is_home_category_grid_candidate(
    *,
    candidate: CandidateAction,
    visual_candidate: AppVisualCandidate | None,
) -> bool:
    """识别首页／列表中部「分栏目／宫格」入口（非底栏、非全宽内容卡）。"""

    if candidate.action_type not in {InteractionOperation.CLICK, InteractionOperation.TAP}:
        return False
    # Vision 常把宫格入口标 medium；仍属可逆内容入口，不得因风险档被整排跳过。
    if candidate.risk_level not in {
        InteractionRiskLevel.LOW,
        InteractionRiskLevel.MEDIUM,
    }:
        return False
    if candidate.metadata.get("control_role") in {"dismiss", "skip"}:
        return False
    if candidate.metadata.get("frontier_scope") == "app":
        # 底栏全局导航不计入中部分栏目。
        return False
    terms = " ".join(
        (
            str(candidate.semantic_target or ""),
            str(candidate.metadata.get("purpose") or ""),
            str(candidate.metadata.get("expected_state_hint") or ""),
            str(getattr(visual_candidate, "semantic_name", "") or ""),
            str(getattr(visual_candidate, "purpose", "") or ""),
        )
    ).casefold()
    if any(marker in terms for marker in _HOME_CATEGORY_NEGATIVE_MARKERS):
        return False
    name = str(candidate.semantic_target or "").strip().casefold()
    positive = any(marker in terms for marker in _HOME_CATEGORY_POSITIVE_MARKERS) or any(
        name.startswith(prefix) for prefix in _HOME_CATEGORY_NAME_PREFIXES
    )
    bounds = None
    if visual_candidate is not None and visual_candidate.normalized_bounds is not None:
        bounds = visual_candidate.normalized_bounds
    elif isinstance(candidate.metadata.get("normalized_bounds"), Mapping):
        raw = candidate.metadata.get("normalized_bounds")
        try:
            bounds = NormalizedBounds.from_dict(raw)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            bounds = None
    if bounds is None:
        return positive
    width = float(bounds.right) - float(bounds.left)
    height = float(bounds.bottom) - float(bounds.top)
    # 中部图标带：排除顶栏筛选与底栏；排除接近全宽的列表大卡。
    mid_band = 0.10 <= float(bounds.top) <= 0.62 and float(bounds.bottom) <= 0.72
    compact = width <= 0.42 and height <= 0.30
    if mid_band and compact and positive:
        return True
    # 无强语义时，仅靠「中部紧凑可点块」不够稳，避免误伤普通卡片。
    return False


def append_home_category_grid_seek_candidate(
    *,
    candidates: tuple[CandidateAction, ...],
    analysis: AppVisualAnalysis,
    navigation_depth: int,
    surface_kind: SurfaceKind,
    allowed_hosts: tuple[str, ...],
    coverage_metadata: Mapping[str, object] | None,
) -> tuple[CandidateAction, ...]:
    """稳定首页／列表上优先轮询未用过的中部分栏目／宫格入口。

    不合成坐标；只提升当前帧真实视觉候选。即使缺页分母已覆盖，仍应巡检分栏目。
    """

    del navigation_depth, surface_kind
    candidates = _drop_legacy_synthetic_candidates(candidates)
    page_state = str(getattr(analysis, "page_state", "") or "").strip().casefold()
    page_type = str(getattr(analysis, "visual_page_type", "") or "").strip().casefold()
    if page_type not in _HOME_CATEGORY_HOST_PAGE_TYPES:
        return candidates
    package_name = getattr(analysis, "package_name", None)
    if allowed_hosts and package_name not in allowed_hosts:
        return candidates
    if not isinstance(coverage_metadata, Mapping):
        coverage_metadata = {}
    # 底栏硬覆盖未完：禁止提升宫格，避免 exploration_value=99 抢走逐栏点击。
    if playbook_bottom_tabs_hard_incomplete(
        coverage_metadata=coverage_metadata,
    ):
        return candidates
    yield_through_overlay = overlay_budget_allows_real_coverage_seek(
        page_type=page_type,
        coverage_metadata=coverage_metadata,
    )
    if page_state != "stable" and not (
        yield_through_overlay and page_state in _COVERAGE_OVERLAY_STATES
    ):
        return candidates
    facts = analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else {}
    if (
        is_obstructed_surface(
            page_state=None,
            page_type=analysis.visual_page_type,
            overlay_signature=analysis.overlay_signature,
            surface_facts=facts,
        )
        or page_state in _COVERAGE_OVERLAY_STATES
    ) and not yield_through_overlay:
        return candidates

    visual_candidates = _current_visual_candidates(analysis)
    grids: list[CandidateAction] = []
    others: list[CandidateAction] = []
    for candidate in candidates:
        visual = visual_candidates.get(candidate.candidate_id)
        if not is_home_category_grid_candidate(
            candidate=candidate,
            visual_candidate=visual,
        ):
            others.append(candidate)
            continue
        expected = _known_page_type_from_hint(_candidate_expected_page_type(candidate))
        label = str(candidate.semantic_target or "").strip() or "分栏目入口"
        capture_id = str(
            candidate.metadata.get("source_capture_id")
            or (visual.source_capture_id if visual is not None else "")
            or getattr(analysis, "capture_id", "")
            or ""
        ).strip() or None
        prior_source = (
            candidate.metadata.get("discovery_source")
            or (visual.discovery_source if visual is not None else None)
            or DISCOVERY_SOURCE_VISION
        )
        if not is_business_navigation_entry(
            source_capture_id=capture_id,
            discovery_source=prior_source,
        ):
            others.append(candidate)
            continue
        metadata = tag_candidate_action_provenance(
            {
                **dict(candidate.metadata),
                "home_category_grid_seek": True,
                # 复用覆盖寻页插队与同入口一次尝试抑制。
                "coverage_page_seek": True,
                # 分栏目必须点进并截图留证，不得只观察或空转下滑。
                "require_screenshot_evidence": True,
                "home_category_grid_evidence": "click_and_screenshot",
                "purpose": f"点击分栏目入口「{label}」并截图留证",
            },
            discovery_source=DISCOVERY_SOURCE_PROMOTED_HOME_GRID,
            source_capture_id=capture_id,
        )
        if expected:
            metadata["expected_next_page_type"] = expected
        if visual is not None and visual.normalized_bounds is not None:
            metadata["normalized_bounds"] = {
                "left": float(visual.normalized_bounds.left),
                "top": float(visual.normalized_bounds.top),
                "right": float(visual.normalized_bounds.right),
                "bottom": float(visual.normalized_bounds.bottom),
            }
        promoted = replace(
            candidate,
            exploration_value=max(candidate.exploration_value, 99),
            metadata=metadata,
        )
        if should_suppress_redundant_app_candidate(
            candidate=promoted,
                    coverage_metadata=coverage_metadata,
                ):
            continue
        grids.append(promoted)
    if not grids:
        return candidates
    # 自上而下、从左到右轮询，保证同一帧多入口都会被点到并截图。
    grids.sort(key=_home_category_grid_order_key)
    return tuple((*grids, *others))


def _home_category_grid_order_key(candidate: CandidateAction) -> tuple[float, float, str]:
    """分栏目巡检顺序：先上后下、先左后右。"""

    raw = candidate.metadata.get("normalized_bounds")
    top = 0.0
    left = 0.0
    if isinstance(raw, Mapping):
        try:
            top = float(raw.get("top") or 0.0)
            left = float(raw.get("left") or 0.0)
        except (TypeError, ValueError):
            top, left = 0.0, 0.0
    return (top, left, candidate.candidate_id)


VISITED_HOME_TOP_CATEGORY_TABS_KEY = "visited_home_top_category_tabs"
HOME_TOP_CATEGORY_SAMPLE_LABEL_KEY = "home_top_category_sample_label"
HOME_TOP_CATEGORY_SAMPLE_PHASE_KEY = "home_top_category_sample_phase"
HOME_TOP_SAMPLE_PHASE_NEED_FIRST_CARD = "need_first_card"
HOME_TOP_SAMPLE_PHASE_NEED_EVIDENCE = "need_evidence"
HOME_TOP_SAMPLE_PHASE_NEED_BACK = "need_back"
_HOME_TOP_SAMPLE_PHASES = frozenset(
    {
        HOME_TOP_SAMPLE_PHASE_NEED_FIRST_CARD,
        HOME_TOP_SAMPLE_PHASE_NEED_EVIDENCE,
        HOME_TOP_SAMPLE_PHASE_NEED_BACK,
    }
)


def _visited_home_top_category_tabs(
    coverage_metadata: Mapping[str, object] | None,
) -> set[str]:
    if not isinstance(coverage_metadata, Mapping):
        return set()
    raw = coverage_metadata.get(VISITED_HOME_TOP_CATEGORY_TABS_KEY)
    if not isinstance(raw, (list, tuple)):
        return set()
    return {
        _normalize_bottom_tab_label(item)
        for item in raw
        if isinstance(item, str) and _normalize_bottom_tab_label(item)
    }


def home_top_category_sample_label(
    coverage_metadata: Mapping[str, object] | None,
) -> str | None:
    if not isinstance(coverage_metadata, Mapping):
        return None
    raw = coverage_metadata.get(HOME_TOP_CATEGORY_SAMPLE_LABEL_KEY)
    if not isinstance(raw, str) or not raw.strip():
        return None
    return raw.strip()


def home_top_category_sample_phase(
    coverage_metadata: Mapping[str, object] | None,
) -> str | None:
    if not isinstance(coverage_metadata, Mapping):
        return None
    raw = coverage_metadata.get(HOME_TOP_CATEGORY_SAMPLE_PHASE_KEY)
    if not isinstance(raw, str):
        return None
    phase = raw.strip()
    return phase if phase in _HOME_TOP_SAMPLE_PHASES else None


def home_top_category_sample_in_progress(
    coverage_metadata: Mapping[str, object] | None,
) -> bool:
    return (
        home_top_category_sample_label(coverage_metadata) is not None
        and home_top_category_sample_phase(coverage_metadata) is not None
    )


def begin_home_top_category_sample(
    coverage_metadata: Mapping[str, object] | None,
    *,
    label: object,
) -> dict[str, object]:
    """上方栏点击成功后进入第一片取证，不记 visited。"""

    from services.api.app.application.app_ui_tree_features import (
        home_top_category_label_is_skip,
    )

    base = dict(coverage_metadata) if isinstance(coverage_metadata, Mapping) else {}
    text = str(label or "").strip()
    if not text or home_top_category_label_is_skip(text):
        return base
    base[HOME_TOP_CATEGORY_SAMPLE_LABEL_KEY] = text
    base[HOME_TOP_CATEGORY_SAMPLE_PHASE_KEY] = HOME_TOP_SAMPLE_PHASE_NEED_FIRST_CARD
    return base


def set_home_top_category_sample_phase(
    coverage_metadata: Mapping[str, object] | None,
    *,
    phase: str,
) -> dict[str, object]:
    base = dict(coverage_metadata) if isinstance(coverage_metadata, Mapping) else {}
    if phase not in _HOME_TOP_SAMPLE_PHASES:
        return base
    if home_top_category_sample_label(base) is None:
        return base
    base[HOME_TOP_CATEGORY_SAMPLE_PHASE_KEY] = phase
    return base


def clear_home_top_category_sample(
    coverage_metadata: Mapping[str, object] | None,
) -> dict[str, object]:
    base = dict(coverage_metadata) if isinstance(coverage_metadata, Mapping) else {}
    base.pop(HOME_TOP_CATEGORY_SAMPLE_LABEL_KEY, None)
    base.pop(HOME_TOP_CATEGORY_SAMPLE_PHASE_KEY, None)
    return base


def complete_home_top_category_sample(
    coverage_metadata: Mapping[str, object] | None,
) -> dict[str, object]:
    """取证 BACK 回主页后：记该栏 visited 并清 sample 状态。"""

    label = home_top_category_sample_label(coverage_metadata)
    base = clear_home_top_category_sample(coverage_metadata)
    if label:
        base[VISITED_HOME_TOP_CATEGORY_TABS_KEY] = record_visited_home_top_category_tab(
            base,
            label=label,
        )
    return base


def record_visited_home_top_category_tab(
    coverage_metadata: Mapping[str, object] | None,
    *,
    label: object,
) -> list[str]:
    """单调记录主页上方分类已点并截图的标签。"""

    from services.api.app.application.app_ui_tree_features import (
        home_top_category_label_is_skip,
    )

    prior = [
        str(item).strip()
        for item in (
            coverage_metadata.get(VISITED_HOME_TOP_CATEGORY_TABS_KEY)
            if isinstance(coverage_metadata, Mapping)
            else ()
        )
        or ()
        if isinstance(item, str) and str(item).strip()
    ]
    # skip 标签（乱伦／日本AV）不得写入课纲 visited。
    if home_top_category_label_is_skip(str(label or "")):
        return prior
    normalized = _normalize_bottom_tab_label(label)
    if not normalized:
        return prior
    prior_normalized = {
        _normalize_bottom_tab_label(item)
        for item in prior
        if _normalize_bottom_tab_label(item)
    }
    if bottom_tab_label_already_visited(normalized, prior_normalized):
        return prior
    return [*prior, str(label or "").strip() or normalized]


def resolve_selected_home_top_category_label(
    analysis: object | None,
    *,
    package_name: str | None = None,
) -> str | None:
    """从像素校准／画面事实读取当前选中的上方分类标签。

    若选中落在 skip 槽（乱伦等），返回该 skip 名（若可知）或 ``__skip__`` 哨兵，
    供匹配函数拒绝记 visited。
    """

    from services.api.app.application.app_visual_home_top_category_calibration import (
        extract_pixel_home_top_category_tabs,
    )

    facts = getattr(analysis, "surface_facts", None)
    if not isinstance(facts, Mapping) and isinstance(analysis, Mapping):
        facts = analysis.get("surface_facts") if isinstance(analysis.get("surface_facts"), Mapping) else analysis
    if isinstance(facts, Mapping):
        if facts.get("home_top_selected_is_skip") is True:
            return "__skip__"
        cal = facts.get("pixel_home_top_category_calibration")
        if isinstance(cal, Mapping) and cal.get("selected_is_skip") is True:
            return "__skip__"
        # 单数事实键（Vision／测试常用）与复数列表都要读。
        for key in (
            "pixel_home_top_selected_label",
            "home_top_selected_label",
            "pixel_home_top_selected_labels",
            "home_top_selected_labels",
        ):
            selected = facts.get(key)
            if isinstance(selected, str) and selected.strip():
                text = selected.strip()
                from services.api.app.application.app_ui_tree_features import (
                    home_top_category_label_is_skip,
                )

                if home_top_category_label_is_skip(text, package_name=package_name):
                    return "__skip__"
                return text
            if isinstance(selected, (list, tuple)):
                for item in selected:
                    text = str(item or "").strip()
                    if text:
                        from services.api.app.application.app_ui_tree_features import (
                            home_top_category_label_is_skip,
                        )

                        if home_top_category_label_is_skip(
                            text, package_name=package_name
                        ):
                            return "__skip__"
                        return text
    tabs = extract_pixel_home_top_category_tabs(analysis)
    for tab in tabs:
        if getattr(tab, "selected", False) is True:
            label = str(getattr(tab, "label", "") or "").strip()
            if label:
                return label
    del package_name
    return None


def home_top_category_seek_selection_matches(
    *,
    expected_label: str,
    selected_label: str | None,
    package_name: str | None = None,
) -> bool:
    """点击后选中态是否与课纲期望一致；skip／错栏一律不算匹配。"""

    from services.api.app.application.app_ui_tree_features import (
        home_top_category_label_is_skip,
        home_top_category_tab_aliases_for_label,
    )

    expected = str(expected_label or "").strip()
    selected = str(selected_label or "").strip()
    if not expected or not selected:
        return False
    if selected == "__skip__" or home_top_category_label_is_skip(
        selected, package_name=package_name
    ):
        return False
    if home_top_category_label_is_skip(expected, package_name=package_name):
        return False
    aliases = home_top_category_tab_aliases_for_label(
        expected, package_name=package_name
    )
    selected_aliases = home_top_category_tab_aliases_for_label(
        selected, package_name=package_name
    )
    if aliases & selected_aliases:
        return True
    return bottom_tab_label_already_visited(
        _normalize_bottom_tab_label(selected),
        {_normalize_bottom_tab_label(a) for a in aliases},
    )


def home_top_category_tabs_incomplete(
    *,
    coverage_metadata: Mapping[str, object] | None,
    package_name: str | None = None,
) -> bool:
    """主页上方分类课纲尚未点完（仅有产品特征时才判定）。

    与 append_home_top_category_tab_seek_candidate 对齐：优先按包名解析产品特征。
    包名已知但未声明上方分类时，不得因 coverage 内 playbook_id（如测试包挂 hlapp）
    误判 incomplete 并拦死底栏提升。
    """

    from services.api.app.application.app_ui_tree_features import (
        ordered_home_top_category_tab_labels,
    )

    if not package_name and isinstance(coverage_metadata, Mapping):
        pkg = coverage_metadata.get("package_name")
        if isinstance(pkg, str) and pkg.strip():
            package_name = pkg.strip()

    ordered = ordered_home_top_category_tab_labels(package_name=package_name)
    if not ordered:
        # 包名已知但无上方分类课纲：不得因 coverage playbook_id 误拦底栏。
        if str(package_name or "").strip():
            return False
        # 无包名时再试 coverage 内 playbook（与上方分类寻页的兜底一致）。
        playbook_id = None
        if isinstance(coverage_metadata, Mapping):
            progress = coverage_metadata.get("app_curriculum_progress")
            if isinstance(progress, Mapping):
                raw_id = progress.get("playbook_id")
                if isinstance(raw_id, str) and raw_id.strip():
                    playbook_id = raw_id.strip()
        ordered = ordered_home_top_category_tab_labels(
            playbook_id=playbook_id,
            package_name=None,
        )
    if not ordered:
        return False
    visited = _visited_home_top_category_tabs(coverage_metadata)
    for label in ordered:
        if not bottom_tab_label_already_visited(label, visited):
            return True
    # 全部栏已点过但当前栏第一片取证未完成，仍算 incomplete（挡底栏）。
    return home_top_category_sample_in_progress(coverage_metadata)


def _next_unvisited_home_top_category_tab_label(
    *,
    visited_labels: set[str],
    ordered_labels: Sequence[str],
) -> str | None:
    for raw in ordered_labels:
        normalized = _normalize_bottom_tab_label(raw)
        if not normalized:
            continue
        if bottom_tab_label_already_visited(normalized, visited_labels):
            continue
        return normalized
    return None


def _candidate_matches_home_top_category_label(
    candidate: CandidateAction,
    *,
    next_label: str,
    package_name: str | None,
    visual: AppVisualCandidate | None,
) -> bool:
    from services.api.app.application.app_ui_tree_features import (
        home_top_category_tab_aliases_for_label,
    )

    aliases = home_top_category_tab_aliases_for_label(
        next_label,
        package_name=package_name,
    )
    terms = " ".join(
        (
            str(candidate.semantic_target or ""),
            str(candidate.metadata.get("purpose") or ""),
            str(candidate.metadata.get("expected_state_hint") or ""),
            str(candidate.metadata.get("text") or ""),
            str(candidate.metadata.get("content_desc") or ""),
            str(getattr(visual, "semantic_name", "") or ""),
            str(getattr(visual, "purpose", "") or ""),
        )
    )
    terms_cf = terms.casefold()
    for alias in aliases:
        if not alias:
            continue
        if alias in terms or alias.casefold() in terms_cf:
            return True
    return bottom_tab_label_already_visited(
        _normalize_bottom_tab_label(candidate.semantic_target),
        {_normalize_bottom_tab_label(a) for a in aliases},
    )


def _home_top_category_band(
    *,
    package_name: str | None,
) -> tuple[float, float]:
    from services.api.app.application.app_ui_tree_features import (
        home_top_category_tabs_ui_tree_features,
    )

    spec = home_top_category_tabs_ui_tree_features(package_name=package_name)
    bounds = spec.get("bounds") if isinstance(spec, Mapping) else None
    if isinstance(bounds, Mapping):
        try:
            return (
                float(bounds.get("min_top") or 0.08),
                float(bounds.get("max_bottom") or 0.18),
            )
        except (TypeError, ValueError):
            pass
    return (0.11, 0.165)


def append_home_top_category_tab_seek_candidate(
    *,
    candidates: tuple[CandidateAction, ...],
    analysis: AppVisualAnalysis,
    navigation_depth: int,
    surface_kind: SurfaceKind,
    allowed_hosts: tuple[str, ...],
    coverage_metadata: Mapping[str, object] | None,
) -> tuple[CandidateAction, ...]:
    """主页上方横向分类：按课纲点最新→国产→动漫→漫画并截图（默认推荐不点）。

    仅当产品 UI Tree 声明了 home_top_category_tabs（目前 hjll）才生效；
    优先提升当前帧真实候选，漏标时用课纲槽位合成同帧入口。
    """

    del navigation_depth
    candidates = _drop_legacy_synthetic_candidates(candidates)
    from services.api.app.application.app_exploration_flow_contract import (
        analysis_blocks_home_top_category_tabs,
        popup_phase_blocks_home_top_category_tabs,
    )
    from services.api.app.application.app_ui_tree_features import (
        home_top_category_tab_slot_bounds,
        ordered_home_top_category_tab_labels,
    )

    def _without_home_top(
        pool: tuple[CandidateAction, ...],
    ) -> tuple[CandidateAction, ...]:
        return tuple(
            item
            for item in pool
            if item.metadata.get("home_top_category_tab_seek") is not True
        )

    # 弹窗流程未满足，或当前帧仍有遮罩事实：禁止上方分类（防点穿广告）。
    # 当前帧主页栏已露出时 popup_phase 让路，避免国庆贴把「最新」从池里剥掉。
    if popup_phase_blocks_home_top_category_tabs(coverage_metadata, analysis=analysis):
        return _without_home_top(candidates)
    if analysis_blocks_home_top_category_tabs(analysis):
        return _without_home_top(candidates)
    page_state = str(getattr(analysis, "page_state", "") or "").strip().casefold()
    page_type = str(getattr(analysis, "visual_page_type", "") or "").strip().casefold()
    if page_type != "home":
        return candidates
    if page_state in {
        "startup_ad",
        "splash",
        "countdown",
        *_COVERAGE_OVERLAY_STATES,
    }:
        return _without_home_top(candidates)
    package_name = getattr(analysis, "package_name", None)
    pkg = str(package_name or "") or None
    if allowed_hosts and package_name not in allowed_hosts:
        return candidates
    if not isinstance(coverage_metadata, Mapping):
        coverage_metadata = {}
    ordered = ordered_home_top_category_tab_labels(package_name=pkg)
    if not ordered:
        return candidates
    # 不再要求 page_state==stable：刚关弹层／馈流微抖时也必须能合成下一上方分类，
    # 否则 incomplete 真但池空，MAF 会旁路切底栏短剧。
    # 但弹层／popup_visible 未清时已在上方 analysis_blocks 闸死。

    visited = _visited_home_top_category_tabs(coverage_metadata)
    next_label = _next_unvisited_home_top_category_tab_label(
        visited_labels=visited,
        ordered_labels=ordered,
    )
    if not next_label:
        return candidates

    band_top, band_bottom = _home_top_category_band(package_name=pkg)
    visual_candidates = _current_visual_candidates(analysis)
    matched: list[CandidateAction] = []
    others: list[CandidateAction] = []
    for candidate in candidates:
        if candidate.metadata.get("home_top_category_tab_seek") is True:
            continue
        visual = visual_candidates.get(candidate.candidate_id)
        bounds = None
        if visual is not None and visual.normalized_bounds is not None:
            bounds = visual.normalized_bounds
        elif isinstance(candidate.metadata.get("normalized_bounds"), Mapping):
            try:
                bounds = NormalizedBounds.from_dict(
                    candidate.metadata.get("normalized_bounds")  # type: ignore[arg-type]
                )
            except Exception:  # noqa: BLE001
                bounds = None
        in_band = False
        if bounds is not None:
            cy = (float(bounds.top) + float(bounds.bottom)) / 2.0
            # 拒绝搜索栏带与中部圆图标行，避免「上方栏目按错位置」进搜索页。
            in_band = (
                band_top <= cy <= band_bottom
                and 0.105 <= cy <= 0.17
                and float(bounds.top) <= band_bottom
            )
        if not (
            in_band
            and _candidate_matches_home_top_category_label(
                candidate,
                next_label=next_label,
                package_name=pkg,
                visual=visual,
            )
        ):
            others.append(candidate)
            continue
        capture_id = str(
            candidate.metadata.get("source_capture_id")
            or (visual.source_capture_id if visual is not None else "")
            or getattr(analysis, "capture_id", "")
            or ""
        ).strip() or None
        if not is_business_navigation_entry(
            source_capture_id=capture_id,
            discovery_source=(
                candidate.metadata.get("discovery_source")
                or (visual.discovery_source if visual is not None else None)
                or DISCOVERY_SOURCE_VISION
            ),
        ):
            others.append(candidate)
            continue
        metadata = tag_candidate_action_provenance(
            {
                **dict(candidate.metadata),
                "frontier_scope": "local",
                "home_top_category_tab_seek": True,
                "coverage_page_seek": True,
                "require_screenshot_evidence": True,
                "purpose": f"点击主页上方分类「{next_label}」并截图留证",
                "expected_state_hint": f"home_top_tab:{next_label}",
            },
            discovery_source=DISCOVERY_SOURCE_PROMOTED_HOME_TOP_TAB,
            source_capture_id=capture_id,
        )
        if bounds is not None:
            metadata["normalized_bounds"] = {
                "left": float(bounds.left),
                "top": float(bounds.top),
                "right": float(bounds.right),
                "bottom": float(bounds.bottom),
            }
        promoted = replace(
            candidate,
            semantic_target=f"上方分类「{next_label}」",
            exploration_value=max(candidate.exploration_value, 100),
            metadata=metadata,
        )
        if should_suppress_redundant_app_candidate(
            candidate=promoted,
            coverage_metadata=coverage_metadata,
        ):
            continue
        matched.append(promoted)

    if matched:
        matched.sort(key=_home_category_grid_order_key)
        return tuple((*matched[:1], *others))

    # 池内漏标：直接扫当前帧 Vision 候选（文字／语义），用真实框点上方分类。
    vision_promoted = _promote_home_top_category_from_vision_candidates(
        analysis=analysis,
        next_label=next_label,
        package_name=pkg,
        band_top=band_top,
        band_bottom=band_bottom,
        surface_kind=surface_kind,
        coverage_metadata=coverage_metadata,
        capture_id=str(getattr(analysis, "capture_id", "") or "").strip() or None,
    )
    if vision_promoted is not None:
        return tuple((vision_promoted, *candidates))

    # Vision 漏标：优先同帧像素上方分类校准（对齐底栏），再才 JSON 槽位。
    from services.api.app.application.app_visual_home_top_category_calibration import (
        extract_pixel_home_top_category_tabs,
    )

    pixel_tabs = extract_pixel_home_top_category_tabs(analysis)
    pixel_hit = next(
        (tab for tab in pixel_tabs if str(tab.label) == str(next_label)),
        None,
    )
    if pixel_hit is None:
        from services.api.app.application.app_semantic_progress import (
            bottom_tab_label_already_visited,
        )

        pixel_hit = next(
            (
                tab
                for tab in pixel_tabs
                if bottom_tab_label_already_visited(tab.label, {next_label})
            ),
            None,
        )
    if pixel_hit is not None:
        capture_id = str(getattr(analysis, "capture_id", "") or "").strip() or None
        if capture_id:
            bounds = pixel_hit.bounds
            visual = AppVisualCandidate.build(
                source_capture_id=capture_id,
                source_image_signature=str(
                    getattr(analysis, "image_signature", "") or "home-top-pixel"
                ),
                source_width=int(getattr(analysis, "width", 0) or 0),
                source_height=int(getattr(analysis, "height", 0) or 0),
                source_orientation=str(
                    getattr(analysis, "orientation", "") or "portrait"
                ),
                semantic_name=f"上方分类「{next_label}」",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=bounds,
                confidence=0.96,
        risk_level=InteractionRiskLevel.LOW,
                purpose=(
                    f"点击主页上方分类「{next_label}」并截图留证；"
                    "当前截图视觉像素上方分类校准"
                ),
                expected_state_hint=f"home_top_tab:{next_label}",
                frontier_scope="local",
                discovery_source=DISCOVERY_SOURCE_PROMOTED_HOME_TOP_TAB,
            )
            metadata = tag_candidate_action_provenance(
                {
                    "candidate_id": visual.candidate_id,
                    "source_capture_id": capture_id,
                    "frontier_scope": "local",
                    "home_top_category_tab_seek": True,
                    "coverage_page_seek": True,
                    "require_screenshot_evidence": True,
                    "home_top_category_pixel_visual": True,
                    "purpose": visual.purpose,
                    "expected_state_hint": f"home_top_tab:{next_label}",
                    "normalized_bounds": bounds.to_dict(),
                },
                discovery_source=DISCOVERY_SOURCE_PROMOTED_HOME_TOP_TAB,
                source_capture_id=capture_id,
            )
            promoted = CandidateAction(
                candidate_id=visual.candidate_id,
                semantic_target=f"上方分类「{next_label}」",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
                exploration_value=100,
                navigation_depth=0,
        expected_surface_kind=surface_kind,
                metadata=metadata,
            )
            if not should_suppress_redundant_app_candidate(
                candidate=promoted,
                coverage_metadata=coverage_metadata,
            ):
                return tuple((promoted, *candidates))

    # Vision／像素均漏：用课纲槽位合成同帧上方分类入口。
    slot = home_top_category_tab_slot_bounds(label=next_label, package_name=pkg)
    capture_id = str(getattr(analysis, "capture_id", "") or "").strip() or None
    if slot is None or not capture_id:
        return candidates
    bounds = NormalizedBounds.from_dict(slot)
    visual = AppVisualCandidate.build(
        source_capture_id=capture_id,
        source_image_signature=str(getattr(analysis, "image_signature", "") or "home-top"),
        source_width=int(getattr(analysis, "width", 0) or 0),
        source_height=int(getattr(analysis, "height", 0) or 0),
        source_orientation=str(getattr(analysis, "orientation", "") or "portrait"),
        semantic_name=f"上方分类「{next_label}」",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=bounds,
        confidence=0.9,
        risk_level=InteractionRiskLevel.LOW,
        purpose=f"点击主页上方分类「{next_label}」并截图留证；课纲槽位校准",
        expected_state_hint=f"home_top_tab:{next_label}",
        frontier_scope="local",
        discovery_source=DISCOVERY_SOURCE_PROMOTED_HOME_TOP_TAB,
    )
    metadata = tag_candidate_action_provenance(
        {
            "candidate_id": visual.candidate_id,
            "source_capture_id": capture_id,
            "frontier_scope": "local",
            "home_top_category_tab_seek": True,
            "coverage_page_seek": True,
            "require_screenshot_evidence": True,
            "purpose": visual.purpose,
            "expected_state_hint": f"home_top_tab:{next_label}",
            "normalized_bounds": dict(slot),
            "home_top_category_slot_synthetic": True,
        },
        discovery_source=DISCOVERY_SOURCE_PROMOTED_HOME_TOP_TAB,
        source_capture_id=capture_id,
    )
    promoted = CandidateAction(
        candidate_id=visual.candidate_id,
        semantic_target=f"上方分类「{next_label}」",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=surface_kind,
        metadata=metadata,
    )
    if should_suppress_redundant_app_candidate(
        candidate=promoted,
        coverage_metadata=coverage_metadata,
    ):
        return candidates
    return tuple((promoted, *candidates))


def _promote_home_top_category_from_vision_candidates(
    *,
    analysis: AppVisualAnalysis,
    next_label: str,
    package_name: str | None,
    band_top: float,
    band_bottom: float,
    surface_kind: SurfaceKind,
    coverage_metadata: Mapping[str, object] | None,
    capture_id: str | None,
) -> CandidateAction | None:
    """从当前帧 Vision 候选池按文字／语义命中上方分类真实框。"""

    if not capture_id:
        return None
    scored: list[tuple[float, AppVisualCandidate]] = []
    for visual in _current_visual_candidates(analysis).values():
        if visual.action_type not in {
            InteractionOperation.CLICK,
            InteractionOperation.TAP,
        }:
            continue
        if visual.normalized_bounds is None:
            continue
        bounds = visual.normalized_bounds
        try:
            cy = (float(bounds.top) + float(bounds.bottom)) / 2.0
            left = float(bounds.left)
            right = float(bounds.right)
        except (TypeError, ValueError):
            continue
        # 上方文字栏：略放宽带，但仍拒搜索栏与中部圆图标。
        in_band = (
            0.09 <= cy <= 0.18
            and band_top - 0.02 <= cy <= band_bottom + 0.02
            and float(bounds.top) <= 0.20
            and left < 0.90
            and right > left
        )
        if not in_band:
            continue
        if not _candidate_matches_home_top_category_label(
            CandidateAction(
                candidate_id=visual.candidate_id,
                semantic_target=str(visual.semantic_name or ""),
                action_type=visual.action_type,
                risk_level=visual.risk_level or InteractionRiskLevel.LOW,
                exploration_value=100,
                navigation_depth=0,
                expected_surface_kind=surface_kind,
                metadata={
                    "purpose": str(visual.purpose or ""),
                    "text": str(getattr(visual, "text", "") or ""),
                    "content_desc": str(getattr(visual, "content_desc", "") or ""),
                    "expected_state_hint": str(visual.expected_state_hint or ""),
                },
            ),
            next_label=next_label,
            package_name=package_name,
            visual=visual,
        ):
            continue
        # 偏好更靠上、更窄的文字栏控件。
        width = max(0.001, right - left)
        score = float(visual.confidence or 0.0) + (0.18 - abs(cy - 0.135)) * 10.0
        score -= width * 2.0
        scored.append((score, visual))
    if not scored:
        return None
    chosen = max(scored, key=lambda item: item[0])[1]
    bounds = chosen.normalized_bounds
    assert bounds is not None
    metadata = tag_candidate_action_provenance(
        {
            "candidate_id": chosen.candidate_id,
            "source_capture_id": capture_id,
            "frontier_scope": "local",
            "home_top_category_tab_seek": True,
            "coverage_page_seek": True,
            "require_screenshot_evidence": True,
            "home_top_category_vision_match": True,
            "purpose": f"点击主页上方分类「{next_label}」并截图留证；当前帧视觉命中",
            "expected_state_hint": f"home_top_tab:{next_label}",
            "normalized_bounds": {
                "left": float(bounds.left),
                "top": float(bounds.top),
                "right": float(bounds.right),
                "bottom": float(bounds.bottom),
            },
        },
        discovery_source=DISCOVERY_SOURCE_PROMOTED_HOME_TOP_TAB,
        source_capture_id=capture_id,
    )
    promoted = CandidateAction(
        candidate_id=chosen.candidate_id,
        semantic_target=f"上方分类「{next_label}」",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=surface_kind,
        metadata=metadata,
    )
    if should_suppress_redundant_app_candidate(
        candidate=promoted,
        coverage_metadata=coverage_metadata,
    ):
        return None
    return promoted


_BOTTOM_NAV_HOST_PAGE_TYPES = frozenset(
    {"home", "video_list", "content_list", "post_list", "activity", "profile"}
)
_BOTTOM_NAV_MARKERS = (
    "底栏",
    "底部tab",
    "底部导航",
    "bottom navigation",
    "bottom_navigation",
    "tab:",
)
VISITED_BOTTOM_NAVIGATION_LABELS_KEY = "visited_bottom_navigation_labels"


def _normalize_bottom_tab_label(raw: object) -> str:
    text = str(raw or "").strip()
    for prefix in (
        "上方分类「",
        "上方分類「",
        "上方分类",
        "上方分類",
        "底栏「",
        "底部导航「",
        "底部Tab",
        "底部tab",
        "底部TAB",
        "底部",
        "底栏",
    ):
        if text.casefold().startswith(prefix.casefold()):
            text = text[len(prefix) :].strip()
            break
    return text.strip("「」[]()（） ").casefold()


def _playbook_tab_order_index(
    normalized_label: str,
    playbook_labels: set[str] | list[str] | tuple[str, ...] | None,
) -> int:
    """返回标签在动态底栏顺序列中的序号；未命中返回 99。

    顺序来自 UI Tree／课纲动态列表；集合无序时仅按字典序，禁止写死产品栏位模板。
    """

    if not normalized_label:
        return 99
    ordered: list[str] = []
    if isinstance(playbook_labels, (list, tuple)):
        ordered = [str(item).strip() for item in playbook_labels if str(item).strip()]
    elif isinstance(playbook_labels, set):
        ordered = sorted(str(item).strip() for item in playbook_labels if str(item).strip())
    for index, expected in enumerate(ordered):
        if bottom_tab_label_already_visited(
            normalized_label,
            {_normalize_bottom_tab_label(expected)},
        ):
            return index
    return 99


def _playbook_label_allows(normalized_label: str, playbook_labels: set[str]) -> bool:
    """playbook 标签相容（提升放行）：精确命中、稳定同义，或 AI／AI科技 前后缀。

    不含暗网↔福利等中槽换皮袋，避免误标栏被当成 playbook 入口空点烧回合。
    """

    if not normalized_label:
        return False
    if not playbook_labels:
        return True
    from services.api.app.application.app_semantic_progress import (
        expand_bottom_tab_promotion_aliases,
    )

    expanded: set[str] = set()
    for label in playbook_labels:
        expanded.update(expand_bottom_tab_promotion_aliases(label))
    if normalized_label in expanded:
        return True
    if expand_bottom_tab_promotion_aliases(normalized_label) & expanded:
        return True
    # AI／AI科技一类前后缀相容（仅对短标签，避免「福」误伤）。
    return any(
        (
            len(expected) >= 2
            and len(normalized_label) >= 2
            and (normalized_label in expected or expected in normalized_label)
        )
        for expected in expanded
        if expected
    )


def _visited_bottom_navigation_labels(
    coverage_metadata: Mapping[str, object] | None,
) -> set[str]:
    if not isinstance(coverage_metadata, Mapping):
        return set()
    raw = coverage_metadata.get(VISITED_BOTTOM_NAVIGATION_LABELS_KEY)
    if not isinstance(raw, (list, tuple)):
        return set()
    return {
        _normalize_bottom_tab_label(item)
        for item in raw
        if isinstance(item, str) and _normalize_bottom_tab_label(item)
    }


def record_visited_bottom_navigation_label(
    coverage_metadata: Mapping[str, object] | None,
    *,
    label: object,
) -> list[str]:
    """单调记录已截图留证的底栏标签，供后续巡检跳过。"""

    normalized = _normalize_bottom_tab_label(label)
    prior = [
        str(item).strip()
        for item in (
            coverage_metadata.get(VISITED_BOTTOM_NAVIGATION_LABELS_KEY)
            if isinstance(coverage_metadata, Mapping)
            else ()
        )
        or ()
        if isinstance(item, str) and str(item).strip()
    ]
    if not normalized:
        return prior
    prior_normalized = {
        _normalize_bottom_tab_label(item)
        for item in prior
        if _normalize_bottom_tab_label(item)
    }
    if bottom_tab_label_already_visited(normalized, prior_normalized):
        return prior
    return [*prior, str(label or "").strip() or normalized]


def is_bottom_navigation_tab_candidate(
    *,
    candidate: CandidateAction,
    visual_candidate: AppVisualCandidate | None,
) -> bool:
    """识别底栏全局 Tab（跨根页 sibling），排除中部分栏目与关层。"""

    if candidate.action_type not in {InteractionOperation.CLICK, InteractionOperation.TAP}:
        return False
    if candidate.risk_level not in {
        InteractionRiskLevel.LOW,
        InteractionRiskLevel.MEDIUM,
    }:
        return False
    if candidate.metadata.get("control_role") in {"dismiss", "skip"}:
        return False
    if candidate.metadata.get("home_category_grid_seek") is True:
        return False
    if candidate.metadata.get("home_top_category_tab_seek") is True:
        return False
    terms = " ".join(
        (
            str(candidate.semantic_target or ""),
            str(candidate.metadata.get("purpose") or ""),
            str(candidate.metadata.get("expected_state_hint") or ""),
            str(getattr(visual_candidate, "semantic_name", "") or ""),
            str(getattr(visual_candidate, "purpose", "") or ""),
        )
    ).casefold()
    # 底栏提升只接受当前 Capture 的视觉候选；CandidateAction 元数据可能来自旧帧。
    if visual_candidate is None or visual_candidate.normalized_bounds is None:
        return False
    bounds = visual_candidate.normalized_bounds
    # 底栏带：靠近屏幕底部的紧凑入口；顶栏等其它全局入口保留普通候选。
    in_bottom_band = float(bounds.top) >= 0.78 and float(bounds.bottom) <= 1.02
    if not in_bottom_band:
        return False
    # app 范围候选仍需当前帧底部几何；其它范围还须有底栏语义标记。
    return candidate.metadata.get("frontier_scope") == "app" or any(
        marker in terms for marker in _BOTTOM_NAV_MARKERS
    )


def _bottom_navigation_tab_order_key(
    candidate: CandidateAction,
) -> tuple[float, str]:
    """底栏从左到右排序；缺坐标时落到末尾。"""

    raw = candidate.metadata.get("normalized_bounds")
    left = 1.0
    if isinstance(raw, Mapping):
        try:
            left = float(raw.get("left") or 1.0)
        except (TypeError, ValueError):
            left = 1.0
    return (left, str(candidate.candidate_id or ""))


def _is_current_selected_bottom_tab(
    *,
    candidate: CandidateAction,
    page_type: str,
    analysis: AppVisualAnalysis | None = None,
) -> bool:
    """仅用当前帧的精确像素／UIA 选中事实判断，不从页型或产品规则猜测。"""

    del page_type
    label = _normalize_bottom_tab_label(candidate.semantic_target)
    if not label or analysis is None:
        return False
    facts = analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else {}
    selected_labels = facts.get("pixel_selected_labels")
    if isinstance(selected_labels, (list, tuple)) and any(
        isinstance(item, str) and _normalize_bottom_tab_label(item) == label
        for item in selected_labels
    ):
        return True
    controls = facts.get("trusted_accessibility_controls")
    if isinstance(controls, Mapping):
        bottom = controls.get("bottom_navigation")
        if isinstance(bottom, (list, tuple)):
            for item in bottom:
                if not isinstance(item, Mapping) or item.get("selected") is not True:
                    continue
                item_label = _normalize_bottom_tab_label(
                    item.get("label") or item.get("text") or item.get("content_desc")
                )
                if item_label == label:
                    return True
    return False


def extract_selected_bottom_tab_labels(
    analysis: AppVisualAnalysis | None,
) -> list[str]:
    """当前帧像素／a11y 已选中底栏标签，供弹层清完后自动记为已访。"""

    if analysis is None:
        return []
    facts = analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else {}
    labels: list[str] = []
    seen: set[str] = set()
    selected_labels = facts.get("pixel_selected_labels")
    if isinstance(selected_labels, (list, tuple)):
        for item in selected_labels:
            if not isinstance(item, str) or not item.strip():
                continue
            key = _normalize_bottom_tab_label(item)
            if not key or key in seen:
                continue
            seen.add(key)
            labels.append(item.strip())
    # 像素底栏校准 tabs[].selected（与 pixel_selected_labels 双写，缺一仍可读）。
    for tab in extract_pixel_bottom_nav_tabs(analysis):
        if not tab.selected:
            continue
        key = _normalize_bottom_tab_label(tab.label)
        if not key or key in seen:
            continue
        seen.add(key)
        labels.append(tab.label)
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
                key = _normalize_bottom_tab_label(raw)
                if not key or key in seen:
                    continue
                seen.add(key)
                labels.append(raw.strip())
    return labels


def _a11y_bottom_navigation_entries(
    analysis: AppVisualAnalysis,
) -> list[tuple[str, NormalizedBounds, str | None]]:
    """读取当前帧 trusted a11y 底栏：标签、归一化 bounds、可选 page_type。"""

    facts = analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else {}
    controls = facts.get("trusted_accessibility_controls")
    if not isinstance(controls, Mapping):
        return []
    navigation = controls.get("bottom_navigation")
    if not isinstance(navigation, (list, tuple)):
        return []
    width = int(getattr(analysis, "width", 0) or 0)
    height = int(getattr(analysis, "height", 0) or 0)
    if width <= 0 or height <= 0:
        return []
    result: list[tuple[str, NormalizedBounds, str | None]] = []
    for item in navigation:
        if not isinstance(item, Mapping):
            continue
        if item.get("action_allowed") is False:
            continue
        label = item.get("label")
        raw_bounds = item.get("bounds")
        if not isinstance(label, str) or not label.strip() or not isinstance(raw_bounds, Mapping):
            continue
        try:
            bounds = NormalizedBounds(
                float(raw_bounds["left"]) / width,
                float(raw_bounds["top"]) / height,
                float(raw_bounds["right"]) / width,
                float(raw_bounds["bottom"]) / height,
            )
        except (KeyError, TypeError, ValueError):
            continue
        if float(bounds.top) < 0.78:
            continue
        page_type = item.get("page_type")
        expected = (
            str(page_type).strip()
            if isinstance(page_type, str) and page_type.strip()
            else None
        )
        result.append((label.strip(), bounds, expected))
    return result


def _bounds_dict(bounds: NormalizedBounds) -> dict[str, float]:
    return {
        "left": float(bounds.left),
        "top": float(bounds.top),
        "right": float(bounds.right),
        "bottom": float(bounds.bottom),
    }


def append_bottom_navigation_tab_seek_candidate(
    *,
    candidates: tuple[CandidateAction, ...],
    analysis: AppVisualAnalysis,
    navigation_depth: int,
    surface_kind: SurfaceKind,
    allowed_hosts: tuple[str, ...],
    coverage_metadata: Mapping[str, object] | None,
) -> tuple[CandidateAction, ...]:
    """根业务页上轮询未点过的底栏 Tab；不合成坐标，只提升当前帧真实候选。

    坐标只来自当前帧 Vision／trusted UIA／像素底栏；课纲 playbook／UI Tree
    只负责标签准入与「下一未访栏」排序（如 hlapp 禁「我的」、按序点精选…）。
    """

    del navigation_depth
    candidates = _drop_legacy_synthetic_candidates(candidates)
    # 弹窗流程未满足：根本不提升底栏候选，避免 action 假 completed。
    from services.api.app.application.app_exploration_flow_contract import (
        popup_phase_blocks_bottom_tabs,
    )

    if popup_phase_blocks_bottom_tabs(coverage_metadata):
        return tuple(
            item
            for item in candidates
            if item.metadata.get("bottom_navigation_tab_seek") is not True
        )
    # 上方分类课纲未完：禁止提升底栏，避免跳过最新→国产→动漫→漫画。
    package_name = getattr(analysis, "package_name", None)
    pkg = str(package_name or "").strip() or None
    if home_top_category_tabs_incomplete(
        coverage_metadata=coverage_metadata if isinstance(coverage_metadata, Mapping) else None,
        package_name=pkg,
    ):
        return tuple(
            item
            for item in candidates
            if item.metadata.get("bottom_navigation_tab_seek") is not True
        )
    page_state = str(getattr(analysis, "page_state", "") or "").strip().casefold()
    page_type = str(getattr(analysis, "visual_page_type", "") or "").strip().casefold()
    package_name = getattr(analysis, "package_name", None)
    # 当前帧已是开屏／弹层时：即使 flow 契约尚未标记 popup_seen（滞后），
    # 也禁止提升底栏，避免点穿广告进浏览器（如 d27791aa）。
    if page_type in {"startup_ad", "splash", "countdown"} or page_state in {
        "startup_ad",
        "splash",
        "countdown",
        *_COVERAGE_OVERLAY_STATES,
    }:
        return tuple(
            item
            for item in candidates
            if item.metadata.get("bottom_navigation_tab_seek") is not True
        )
    playbook_labels_probe = _playbook_bottom_tab_labels(
        coverage_metadata if isinstance(coverage_metadata, Mapping) else {},
        package_name=str(package_name or "") or None,
    )
    playbook_labels_ordered = _playbook_bottom_tab_labels_ordered(
        coverage_metadata if isinstance(coverage_metadata, Mapping) else {},
        package_name=str(package_name or "") or None,
    )
    playbook_labels = playbook_labels_probe
    visual_candidates = _current_visual_candidates(analysis)
    a11y_entries = _a11y_bottom_navigation_entries(analysis)
    has_current_bottom_entry = bool(a11y_entries) or any(
        is_bottom_navigation_tab_candidate(
            candidate=candidate,
            visual_candidate=visual_candidates.get(candidate.candidate_id),
        )
        for candidate in candidates
        if candidate.candidate_id in visual_candidates
    )
    # 未知页型也可在当前帧确实有 Vision/UIA 底栏入口时继续；不得借产品 playbook 放行。
    host_ok = page_type in _BOTTOM_NAV_HOST_PAGE_TYPES or has_current_bottom_entry
    if not host_ok:
        return candidates
    if allowed_hosts and package_name not in allowed_hosts:
        return candidates
    if not isinstance(coverage_metadata, Mapping):
        coverage_metadata = {}
    yield_through_overlay = overlay_budget_allows_real_coverage_seek(
        page_type=page_type or "home",
        coverage_metadata=coverage_metadata,
    )
    early_bottom = coverage_incomplete_allows_bottom_nav_through_overlay(
        coverage_metadata=coverage_metadata,
    )
    # 浮层广告未盖住底栏时（顶边 >=0.88）：允许提早提升真实底栏，避免只关层／点进详情。
    visible_bottom_band = any(
        (
            visual is not None
            and visual.normalized_bounds is not None
            and float(visual.normalized_bounds.top) >= 0.88
            and is_bottom_navigation_tab_candidate(
                candidate=candidate,
                visual_candidate=visual,
            )
        )
        for candidate in candidates
        for visual in (visual_candidates.get(candidate.candidate_id),)
    )
    allow_through_overlay = yield_through_overlay or (
        early_bottom and visible_bottom_band and page_state in _COVERAGE_OVERLAY_STATES
    )
    if page_state != "stable" and not (
        allow_through_overlay and page_state in _COVERAGE_OVERLAY_STATES
    ):
        return candidates
    facts = analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else {}
    if (
        is_obstructed_surface(
            page_state=None,
            page_type=analysis.visual_page_type,
            overlay_signature=analysis.overlay_signature,
            surface_facts=facts,
        )
        or page_state in _COVERAGE_OVERLAY_STATES
    ) and not allow_through_overlay:
        return candidates
    visited_labels = _visited_bottom_navigation_labels(coverage_metadata)
    a11y_by_label = {
        _normalize_bottom_tab_label(label): (label, bounds, expected)
        for label, bounds, expected in a11y_entries
    }
    pixel_tabs = extract_pixel_bottom_nav_tabs(analysis)
    pixel_by_label = {
        _normalize_bottom_tab_label(tab.label): tab
        for tab in pixel_tabs
        if _normalize_bottom_tab_label(tab.label)
    }
    tabs: list[CandidateAction] = []
    others: list[CandidateAction] = []
    seen_promoted: set[str] = set()
    for candidate in candidates:
        visual = visual_candidates.get(candidate.candidate_id)
        if not is_bottom_navigation_tab_candidate(
            candidate=candidate,
            visual_candidate=visual,
        ):
            others.append(candidate)
            continue
        if _is_current_selected_bottom_tab(
            candidate=candidate,
            page_type=page_type,
            analysis=analysis,
        ):
            # 弹层关完已落在当前栏（如主页）：禁止再点同栏；当前帧截图即算到访。
            selected_label = _normalize_bottom_tab_label(candidate.semantic_target)
            if selected_label:
                visited_labels.add(selected_label)
                seen_promoted.add(selected_label)
            others.append(candidate)
            continue
        label = str(candidate.semantic_target or "").strip() or "底栏入口"
        normalized_label = _normalize_bottom_tab_label(label)
        # 当前帧真实可见底栏一律可提升；课纲只影响下一栏排序，不删可见入口。
        # 主页已落地：禁止再提升主页／首页点击（录屏只截不点）。
        if page_type == "home" and bottom_tab_label_already_visited(
            normalized_label,
            {"主页", "首页"},
        ):
            visited_labels.add(normalized_label)
            seen_promoted.add(normalized_label)
            others.append(candidate)
            continue
        if bottom_tab_label_already_visited(normalized_label, visited_labels):
            continue
        expected = _known_page_type_from_hint(_candidate_expected_page_type(candidate))
        # semantic 常已是「底栏「交友」」；purpose 勿再套一层「底栏「…」」。
        label_for_purpose = label
        for prefix in ("底栏「", "底部导航「", "底部Tab", "底部"):
            if label_for_purpose.startswith(prefix):
                remainder = label_for_purpose[len(prefix) :].strip("「」 ")
                if remainder:
                    label_for_purpose = remainder
                break
        capture_id = str(
            candidate.metadata.get("source_capture_id")
            or (visual.source_capture_id if visual is not None else "")
            or getattr(analysis, "capture_id", "")
            or ""
        ).strip() or None
        prior_source = (
            candidate.metadata.get("discovery_source")
            or (visual.discovery_source if visual is not None else None)
            or DISCOVERY_SOURCE_VISION
        )
        if not is_business_navigation_entry(
            source_capture_id=capture_id,
            discovery_source=prior_source,
        ):
            others.append(candidate)
            continue
        metadata = tag_candidate_action_provenance(
            {
                **dict(candidate.metadata),
                "frontier_scope": "app",
                "bottom_navigation_tab_seek": True,
                "coverage_page_seek": True,
                "require_screenshot_evidence": True,
                "purpose": f"点击底栏「{label_for_purpose}」并截图留证",
            },
            discovery_source=DISCOVERY_SOURCE_PROMOTED_BOTTOM_NAV,
            source_capture_id=capture_id,
        )
        if expected:
            metadata["expected_next_page_type"] = expected
        # 落点优先级：同帧视觉像素槽 → a11y 条带 → Vision（常偏手势区）。
        pixel_hit = pixel_by_label.get(normalized_label)
        a11y_hit = a11y_by_label.get(normalized_label)
        if pixel_hit is not None:
            metadata["normalized_bounds"] = _bounds_dict(pixel_hit.bounds)
            metadata["discovery_source"] = DISCOVERY_SOURCE_VISION
            metadata["bottom_nav_pixel_visual"] = True
            metadata["purpose"] = (
                f"点击底栏「{label_for_purpose}」并截图留证；当前截图视觉像素底栏校准"
            )
        elif a11y_hit is not None:
            _, a11y_bounds, a11y_expected = a11y_hit
            metadata["normalized_bounds"] = _bounds_dict(a11y_bounds)
            metadata["discovery_source"] = DISCOVERY_SOURCE_UIA_ISSUED
            if a11y_expected and not expected:
                metadata["expected_next_page_type"] = a11y_expected
        elif visual is not None and visual.normalized_bounds is not None:
            metadata["normalized_bounds"] = _bounds_dict(visual.normalized_bounds)
        promoted = replace(
            candidate,
            exploration_value=max(candidate.exploration_value, 100),
            metadata=metadata,
        )
        if should_suppress_redundant_app_candidate(
            candidate=promoted,
            coverage_metadata=coverage_metadata,
        ):
            continue
        tabs.append(promoted)
        if normalized_label:
            seen_promoted.add(normalized_label)

    # Vision 漏栏时，用当前帧 trusted UIA 底栏补齐真实入口。
    for raw_label, bounds, expected in a11y_entries:
        normalized_label = _normalize_bottom_tab_label(raw_label)
        if not normalized_label or normalized_label in seen_promoted:
            continue
        if playbook_labels and not _playbook_label_allows(normalized_label, playbook_labels):
            continue
        if bottom_tab_label_already_visited(normalized_label, visited_labels):
            continue
        # 当前已选中栏不再补点；弹层清完已在主页时尤其禁止。
        selected_probe = CandidateAction(
            candidate_id=f"a11y-selected-probe:{normalized_label}",
            semantic_target=raw_label,
            action_type=InteractionOperation.CLICK,
            risk_level=InteractionRiskLevel.LOW,
            exploration_value=1,
            navigation_depth=0,
            expected_surface_kind=surface_kind,
            metadata={},
        )
        if _is_current_selected_bottom_tab(
            candidate=selected_probe,
            page_type=page_type,
            analysis=analysis,
        ):
            # 已选中栏（主页）：只记到访，禁止 a11y 再补点。
            visited_labels.add(normalized_label)
            seen_promoted.add(normalized_label)
            continue
        capture_id = str(getattr(analysis, "capture_id", "") or "").strip() or None
        if not capture_id:
            continue
        visual = AppVisualCandidate.build(
            source_capture_id=capture_id,
            source_image_signature=str(getattr(analysis, "image_signature", "") or "a11y"),
            source_width=int(getattr(analysis, "width", 0) or 0),
            source_height=int(getattr(analysis, "height", 0) or 0),
            source_orientation=str(getattr(analysis, "orientation", "") or "portrait"),
            semantic_name=f"底栏「{raw_label}」",
            action_type=InteractionOperation.CLICK,
            normalized_bounds=bounds,
            confidence=1.0,
            risk_level=InteractionRiskLevel.LOW,
            purpose=f"点击底栏「{raw_label}」并截图留证",
            expected_state_hint=expected or f"tab:{raw_label}",
            frontier_scope="app",
            discovery_source=DISCOVERY_SOURCE_UIA_ISSUED,
        )
        metadata = tag_candidate_action_provenance(
            {
                "candidate_id": visual.candidate_id,
                "source_capture_id": capture_id,
                "frontier_scope": "app",
                "bottom_navigation_tab_seek": True,
                "coverage_page_seek": True,
                "require_screenshot_evidence": True,
                "purpose": f"点击底栏「{raw_label}」并截图留证",
                "normalized_bounds": _bounds_dict(bounds),
            },
            discovery_source=DISCOVERY_SOURCE_UIA_ISSUED,
            source_capture_id=capture_id,
        )
        if expected:
            metadata["expected_next_page_type"] = expected
        promoted = CandidateAction(
            candidate_id=visual.candidate_id,
            semantic_target=f"底栏「{raw_label}」",
            action_type=InteractionOperation.CLICK,
            risk_level=InteractionRiskLevel.LOW,
            exploration_value=100,
            navigation_depth=0,
            expected_surface_kind=surface_kind,
            metadata=metadata,
        )
        if should_suppress_redundant_app_candidate(
            candidate=promoted,
            coverage_metadata=coverage_metadata,
        ):
            continue
        tabs.append(promoted)
        seen_promoted.add(normalized_label)

    # Vision／a11y 漏栏时，用同帧像素底栏补齐课纲 Tab（非插值，须 PNG 已检出）。
    for tab in pixel_tabs:
        raw_label = tab.label
        normalized_label = _normalize_bottom_tab_label(raw_label)
        if not normalized_label or normalized_label in seen_promoted:
            continue
        if bottom_tab_label_already_visited(normalized_label, visited_labels):
            continue
        if playbook_labels and not _playbook_label_allows(normalized_label, playbook_labels):
            continue
        if tab.selected:
            visited_labels.add(normalized_label)
            seen_promoted.add(normalized_label)
            continue
        if page_type == "home" and bottom_tab_label_already_visited(
            normalized_label,
            {"主页", "首页"},
        ):
            visited_labels.add(normalized_label)
            seen_promoted.add(normalized_label)
            continue
        capture_id = str(getattr(analysis, "capture_id", "") or "").strip() or None
        if not capture_id:
            continue
        visual = AppVisualCandidate.build(
            source_capture_id=capture_id,
            source_image_signature=str(getattr(analysis, "image_signature", "") or "pixel"),
            source_width=int(getattr(analysis, "width", 0) or 0),
            source_height=int(getattr(analysis, "height", 0) or 0),
            source_orientation=str(getattr(analysis, "orientation", "") or "portrait"),
            semantic_name=f"底栏「{raw_label}」",
            action_type=InteractionOperation.CLICK,
            normalized_bounds=tab.bounds,
            confidence=0.95,
            risk_level=InteractionRiskLevel.LOW,
            purpose=(
                f"点击底栏「{raw_label}」并截图留证；当前截图视觉像素底栏校准"
            ),
            expected_state_hint=f"tab:{raw_label}",
            frontier_scope="app",
            discovery_source=DISCOVERY_SOURCE_VISION,
        )
        metadata = tag_candidate_action_provenance(
            {
                "candidate_id": visual.candidate_id,
                "source_capture_id": capture_id,
                "frontier_scope": "app",
                "bottom_navigation_tab_seek": True,
            "coverage_page_seek": True,
                "require_screenshot_evidence": True,
                "purpose": visual.purpose,
                "normalized_bounds": _bounds_dict(tab.bounds),
                "bottom_nav_pixel_visual": True,
                "playbook_tab_index": _playbook_tab_order_index(
                    normalized_label, playbook_labels_ordered or playbook_labels
                ),
            },
            discovery_source=DISCOVERY_SOURCE_VISION,
            source_capture_id=capture_id,
        )
        promoted = CandidateAction(
            candidate_id=visual.candidate_id,
            semantic_target=f"底栏「{raw_label}」",
            action_type=InteractionOperation.CLICK,
            risk_level=InteractionRiskLevel.LOW,
            exploration_value=100,
            navigation_depth=0,
            expected_surface_kind=surface_kind,
            metadata=metadata,
        )
        if should_suppress_redundant_app_candidate(
            candidate=promoted,
            coverage_metadata=coverage_metadata,
        ):
            continue
        tabs.append(promoted)
        seen_promoted.add(normalized_label)

    # 已选中栏（主页）计入已访，不得再硬补点；禁止插值补栏（会污染其它 App）。
    # 课纲 next 只把主页／首页的选中态并入已访；短剧等当前选中仅用于本帧禁止再点同栏，
    # 不得把误检「暗网」选中态写成已访导致跳到 AI。
    selected_now = {
        _normalize_bottom_tab_label(label)
        for label in extract_selected_bottom_tab_labels(analysis)
        if _normalize_bottom_tab_label(label)
    }
    for label in selected_now:
        if bottom_tab_label_already_visited(label, {"主页", "首页"}):
            visited_labels.add(label)
            seen_promoted.add(label)
    # 本帧真实选中栏：禁止再提升同栏点击（精确标签，不用中槽换皮袋）。
    selected_block = set(selected_now)
    seen_promoted.update(selected_block)
    # 漏栏靠 Vision／a11y／同帧像素条带；禁止无截图证据的 gap-fill／跳点。

    if not tabs:
        return candidates
    # 课纲硬覆盖：每帧只提升「下一未访栏」，禁止一次塞满导致先点「我的」。
    # selected_block 并入以避免再点当前栏；误检「暗网」选中由下方强制打回。
    next_label = _next_unvisited_playbook_bottom_tab_label(
        visited_labels=visited_labels | selected_block,
        ordered_labels=playbook_labels_ordered or tuple(sorted(playbook_labels)),
        page_type=page_type,
    )
    # 短剧课纲后强制暗网：sticky 未留证暗网时，不得因误检选中态跳到 AI。
    next_label = _prefer_anwang_after_short_drama(
        next_label=next_label,
        visited_labels=visited_labels,
        ordered_labels=playbook_labels_ordered or tuple(sorted(playbook_labels)),
        coverage_metadata=coverage_metadata,
    )
    matched = _filter_tabs_to_next_playbook_only(tabs, next_label=next_label)
    if matched:
        matched.sort(key=_bottom_navigation_tab_order_key)
        return tuple((*matched, *others))
    # 下一栏本帧未见或过滤未命中。
    if next_label:
        preferred: list[CandidateAction] = []
        rest: list[CandidateAction] = []
        for tab in tabs:
            label = _normalize_bottom_tab_label(tab.semantic_target)
            purpose = str(tab.metadata.get("purpose") or "")
            hint = str(tab.metadata.get("expected_state_hint") or "")
            is_next = (
                bottom_tab_label_already_visited(label, {next_label})
                or (next_label and next_label in purpose)
                or (
                    tab.metadata.get("bottom_nav_pixel_visual") is True
                    and bottom_tab_label_already_visited(
                        hint.removeprefix("tab:"),
                        {next_label},
                    )
                )
            )
            if is_next:
                preferred.append(tab)
            else:
                rest.append(tab)
        if preferred:
            preferred.sort(key=_bottom_navigation_tab_order_key)
            return tuple((*preferred, *others))
        # hjll 按序巡检（短剧／暗网／我的）：下一栏本帧未见时，禁止把后续栏塞回池。
        # 仅含「我的」的其它产品（杏吧等）仍保留当前帧真实可见底栏。
        ordered_for_gate = playbook_labels_ordered or tuple(sorted(playbook_labels))
        ordered_norms = {
            _normalize_bottom_tab_label(item)
            for item in ordered_for_gate
            if _normalize_bottom_tab_label(item)
        }
        hjll_sequence = {"短剧", "我的"} <= ordered_norms and (
            "暗网" in ordered_norms or "暗網" in ordered_norms
        )
        if hjll_sequence:
            return tuple(others)
        # 其它产品（如 hlapp 禁「我的」）：仍保留当前帧真实可见未访栏。
        rest.sort(key=_bottom_navigation_tab_order_key)
        return tuple((*rest, *others))
    tabs.sort(key=_bottom_navigation_tab_order_key)
    return tuple((*tabs, *others))


def _next_unvisited_playbook_bottom_tab_label(
    *,
    visited_labels: set[str],
    ordered_labels: Sequence[str],
    page_type: str = "",
) -> str | None:
    """课纲下一未访底栏；主页／首页已落地只截不点。"""

    from services.api.app.application.app_semantic_progress import (
        bottom_tab_label_already_visited,
        bottom_tab_playbook_progress_already_visited,
    )

    page = str(page_type or "").casefold()
    for raw in ordered_labels:
        normalized = _normalize_bottom_tab_label(raw)
        if not normalized:
                    continue
        if normalized in {"主页", "首页"} and (
            page == "home"
            or bottom_tab_label_already_visited(normalized, visited_labels)
        ):
                    continue
        # 课纲推进禁止用漫画／福利等同槽袋跳过暗网。
        if bottom_tab_playbook_progress_already_visited(normalized, visited_labels):
                    continue
        return normalized
    return None


def _prefer_anwang_after_short_drama(
    *,
    next_label: str | None,
    visited_labels: set[str],
    ordered_labels: Sequence[str],
    coverage_metadata: Mapping[str, object] | None,
) -> str | None:
    """短剧已留证且暗网未 seek 留证时，强制下一栏为暗网。"""

    from services.api.app.application.app_semantic_progress import (
        bottom_tab_playbook_progress_already_visited,
        is_short_drama_bottom_tab_label,
        short_drama_end_seen,
        short_drama_exit_pending,
    )

    ordered_norms = {
        _normalize_bottom_tab_label(item)
        for item in ordered_labels
        if _normalize_bottom_tab_label(item)
    }
    if "暗网" not in ordered_norms and "暗網" not in ordered_norms:
        return next_label
    if bottom_tab_playbook_progress_already_visited("暗网", visited_labels):
        return next_label
    short_drama_done = any(
        is_short_drama_bottom_tab_label(item) and bottom_tab_playbook_progress_already_visited(item, visited_labels)
        for item in visited_labels
    ) or bottom_tab_playbook_progress_already_visited("短剧", visited_labels)
    if not short_drama_done and not (
        short_drama_end_seen(coverage_metadata)
        or short_drama_exit_pending(coverage_metadata)
    ):
        return next_label
    return "暗网"


def _filter_tabs_to_next_playbook_only(
    tabs: list[CandidateAction],
    *,
    next_label: str | None,
    playbook_id: str | None = None,
    package_name: str | None = None,
) -> list[CandidateAction]:
    """只保留课纲下一栏：匹配 APP JSON 的 UI Tree 特征；找不到则空，禁止插值／乱序回退。

    匹配入口统一在此；Explorer 的 `_pick_next_playbook_bottom_tab` 只委派本函数，
    不得再写一套标签／别名比对。
    """

    if not next_label or not tabs:
        return []
    from services.api.app.application.app_ui_tree_features import (
        bottom_navigation_ui_tree_features,
        candidate_matches_ui_tree_bottom_tab_label,
        resolve_ui_tree_playbook_id,
    )

    resolved = resolve_ui_tree_playbook_id(
        playbook_id=playbook_id,
        package_name=package_name,
    )
    if resolved is None:
        # 由下一栏文案推断产品，避免默认读错 hjll 特征
        probe = str(next_label or "")
        if probe in {"精选", "片库", "直播", "AI科技", "圈子"} or probe.startswith("AI科"):
            resolved = "hlapp"
        elif probe in {"短剧", "暗网", "暗網", "主页", "社区", "社區"}:
            resolved = "hjll"
    features = bottom_navigation_ui_tree_features(
        playbook_id=resolved,
        package_name=package_name,
    )
    return [
        tab
        for tab in tabs
        if candidate_matches_ui_tree_bottom_tab_label(
            tab,
            next_label=next_label,
            features=features,
        )
    ]


def resolve_coverage_page_seek(
    *,
    analysis: AppVisualAnalysis,
    missing: list[str],
    covered: set[str],
) -> tuple[str, str, str, NormalizedBounds] | None:
    """兼容旧导入；旧 synthetic 寻页合同永久 fail closed。"""

    del analysis, missing, covered
    return None


def _trusted_accessibility_page_types(analysis: AppVisualAnalysis) -> set[str]:
    facts = analysis.surface_facts if isinstance(analysis.surface_facts, Mapping) else {}
    controls = facts.get("trusted_accessibility_controls")
    if not isinstance(controls, Mapping):
        return set()
    result: set[str] = set()
    for key in ("page_type", "selected_page_type"):
        value = str(controls.get(key) or "").strip().casefold()
        if value:
            result.add(value)
    return result


def align_visual_page_type_after_coverage_seek(
    analysis: AppVisualAnalysis,
    *,
    completed_action: CandidateAction | None,
    state_changed: bool,
    source_page_type: str | None = None,
) -> AppVisualAnalysis:
    """动作后只使用当前截图的可信页型事实，兼容旧对齐入口。"""

    if completed_action is None or not state_changed:
        return analysis
    # 旧合同没有当前视觉候选身份，不能借页型预期继续推断到达。
    if completed_action.metadata.get("coverage_page_seek_contract") is not None:
        return analysis
    expected_raw = completed_action.metadata.get("expected_next_page_type")
    expected = (
        expected_raw.strip().casefold()
        if isinstance(expected_raw, str) and expected_raw.strip()
        else ""
    )
    page_state = str(analysis.page_state or "").strip().casefold()
    if page_state in _COVERAGE_SEEK_BLOCKING_STATES or page_state in _COVERAGE_OVERLAY_STATES:
        return analysis
    if analysis.overlay_signature is not None:
        return analysis

    vision = str(analysis.visual_page_type or "").strip().casefold()
    source = str(source_page_type or "").strip().casefold()
    # 片库卡片下钻时，保留已有的兼容页型修正；它不创建候选或触控几何。
    if (
        vision == "content_detail"
        and page_state == "stable"
        and (expected == "video_detail" or source == "video_list")
    ):
        return _with_visual_page_type(analysis, "video_detail")

    if completed_action.metadata.get("coverage_page_seek") is not True:
        return analysis
    if expected and expected not in _PAGE_TYPE_REGISTRY.all_codes:
        return analysis
    if vision == expected:
        return analysis
    trusted_types = _trusted_accessibility_page_types(analysis)
    if expected in trusted_types:
        return _with_visual_page_type(analysis, expected)

    # 入口名称、目标页型和顶栏标题都不能单独证明已经到达目标类型。
    return analysis


def _with_visual_page_type(
    analysis: AppVisualAnalysis,
    page_type: str,
) -> AppVisualAnalysis:
    normalized = page_type.strip().casefold()
    if normalized == str(analysis.visual_page_type or "").strip().casefold():
        return analysis
    return AppVisualAnalysis.build(
        capture_id=analysis.capture_id,
        screenshot_artifact_id=analysis.screenshot_artifact_id,
        image_signature=analysis.image_signature,
        width=analysis.width,
        height=analysis.height,
        orientation=analysis.orientation,
        visual_page_type=normalized,
        visual_summary=analysis.visual_summary,
        key_elements=analysis.key_elements,
        candidates=analysis.candidates,
        provider_id=analysis.provider_id,
        model_id=analysis.model_id,
        package_name=analysis.package_name,
        activity=analysis.activity,
        page_state=analysis.page_state,
        overlay_signature=analysis.overlay_signature,
        surface_facts=analysis.surface_facts,
    )


# 兼容旧私有名（截图探索与历史测试仍会导入）。
_append_coverage_page_seek_candidate = append_coverage_page_seek_candidate
_append_home_category_grid_seek_candidate = append_home_category_grid_seek_candidate
_append_home_top_category_tab_seek_candidate = append_home_top_category_tab_seek_candidate
_append_bottom_navigation_tab_seek_candidate = append_bottom_navigation_tab_seek_candidate
_coverage_seek_required_page_types = coverage_seek_required_page_types
_resolve_coverage_page_seek = resolve_coverage_page_seek
_align_visual_page_type_after_coverage_seek = align_visual_page_type_after_coverage_seek
_is_home_category_grid_candidate = is_home_category_grid_candidate
_is_bottom_navigation_tab_candidate = is_bottom_navigation_tab_candidate
