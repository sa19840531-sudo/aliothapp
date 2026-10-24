"""执行 App sibling 前基于当前截图重新确认来源页面与候选。"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Mapping
from dataclasses import replace

from services.api.app.application.app_exploration_flow_contract import (
    popup_phase_blocks_bottom_tabs,
)
from services.api.app.application.app_global_frontier import (
    APP_FRONTIER_RESTORATION_CANDIDATES,
)
from services.api.app.application.app_obstruction_controls import (
    is_obstructed_surface,
    is_visual_obstruction_control,
)
from services.api.app.application.app_package_boundary import (
    work_within_app_package_boundary,
)
from services.api.app.application.app_synthetic_touch_contracts import (
    attach_synthetic_touch_contracts,
)
from services.api.app.application.capture_profiles import CaptureProfilePolicy
from services.api.app.domain.exploration import (
    APP_FRONTIER_FINGERPRINT_STRATEGY,
    APP_GLOBAL_NAVIGATION_FINGERPRINT_V2,
    APP_GLOBAL_NAVIGATION_FINGERPRINT_V3,
    APP_GLOBAL_NAVIGATION_FINGERPRINT_V4,
    APP_GLOBAL_NAVIGATION_FINGERPRINT_V5,
    APP_GLOBAL_NAVIGATION_FINGERPRINT_V6,
    APP_GLOBAL_NAVIGATION_FINGERPRINT_V7,
    APP_GLOBAL_NAVIGATION_FINGERPRINT_V8,
    AppFrontierItem,
    CandidateAction,
    ExplorationWork,
    build_app_frontier_candidate_fingerprint,
    with_app_frontier_fingerprint_strategy,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    InteractionOperation,
    InteractionRiskLevel,
)


class AppFrontierRestorationError(RuntimeError):
    """当前页面无法安全恢复到 sibling 来源。"""

    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable


class AppFrontierDeferredError(RuntimeError):
    """Frontier 仍有租约或等待重试的事实，当前 Work 只能延迟重领。"""

    error_code = "APP_FRONTIER_NOT_CLAIMABLE"
    retryable = True


# 与 H5 对齐：同一 Frontier item 的 restore_only 路径最多两次，避免 BACK／锚点空转。
APP_FRONTIER_RESTORE_MAX_ATTEMPTS = 2
APP_FRONTIER_RESTORE_ATTEMPTS_KEY = "app_frontier_restore_attempts"
# 底栏 sibling 常需多轮清广告才能露出条带，restore_only 预算单独放宽。
APP_FRONTIER_BOTTOM_NAV_RESTORE_MAX_ATTEMPTS = 8
# 候选在动作前因当前截图暂时未重现时，允许初次领取后再重试两次。
# 第三次领取仍无法重现即视为稳定重复缺失，进入 BLOCKED，而不是继续 RETRY_WAIT。
APP_FRONTIER_SAME_SEMANTIC_RETRY_MAX = 2
APP_FRONTIER_SAME_SEMANTIC_ATTEMPT_LIMIT = APP_FRONTIER_SAME_SEMANTIC_RETRY_MAX + 1
# 底栏全局 sibling 常因 WebView／设备通道抖动失败，多给重试窗口。
APP_FRONTIER_BOTTOM_NAV_RETRY_MAX = 8
APP_FRONTIER_BOTTOM_NAV_ATTEMPT_LIMIT = APP_FRONTIER_BOTTOM_NAV_RETRY_MAX + 1
# 设备通道未知后给云手机通道冷却时间，避免连点烧尽 attempt。
APP_FRONTIER_BOTTOM_NAV_UNKNOWN_RETRY_DELAY_SECONDS = 12


def app_frontier_same_semantic_retry_allowed(
    attempt_count: int,
    *,
    bottom_nav: bool = False,
) -> bool:
    """判断当前领取是否仍可为同一语义候选保留一次重试。"""

    limit = (
        APP_FRONTIER_BOTTOM_NAV_ATTEMPT_LIMIT
        if bottom_nav
        else APP_FRONTIER_SAME_SEMANTIC_ATTEMPT_LIMIT
    )
    return (
        isinstance(attempt_count, int)
        and not isinstance(attempt_count, bool)
        and 0 < attempt_count < limit
    )


def app_frontier_same_semantic_retry_exhausted(
    attempt_count: int,
    *,
    bottom_nav: bool = False,
) -> bool:
    """判断同一语义候选是否已用尽初次领取之外的重试。"""

    limit = (
        APP_FRONTIER_BOTTOM_NAV_ATTEMPT_LIMIT
        if bottom_nav
        else APP_FRONTIER_SAME_SEMANTIC_ATTEMPT_LIMIT
    )
    return (
        isinstance(attempt_count, int)
        and not isinstance(attempt_count, bool)
        and attempt_count >= limit
    )


def app_frontier_restore_attempt_count(
    metadata: Mapping[str, object] | None,
    *,
    frontier_item_id: str,
) -> int:
    """读取某一 Frontier item 已消耗的 restore_only 次数。"""

    if not isinstance(metadata, Mapping):
        return 0
    raw = metadata.get(APP_FRONTIER_RESTORE_ATTEMPTS_KEY)
    if not isinstance(raw, Mapping):
        return 0
    value = raw.get(frontier_item_id)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def app_frontier_restore_attempts_exhausted(
    metadata: Mapping[str, object] | None,
    *,
    frontier_item_id: str,
    bottom_nav: bool = False,
) -> bool:
    limit = (
        APP_FRONTIER_BOTTOM_NAV_RESTORE_MAX_ATTEMPTS
        if bottom_nav
        else APP_FRONTIER_RESTORE_MAX_ATTEMPTS
    )
    return (
        app_frontier_restore_attempt_count(
            metadata,
            frontier_item_id=frontier_item_id,
        )
        >= limit
    )


def with_incremented_app_frontier_restore_attempt(
    metadata: Mapping[str, object],
    *,
    frontier_item_id: str,
) -> dict[str, object]:
    """在覆盖 metadata 中单调累加 restore_only 计数。"""

    result = dict(metadata)
    prior = result.get(APP_FRONTIER_RESTORE_ATTEMPTS_KEY)
    attempts: dict[str, object] = dict(prior) if isinstance(prior, Mapping) else {}
    attempts[frontier_item_id] = (
        app_frontier_restore_attempt_count(
            result,
            frontier_item_id=frontier_item_id,
        )
        + 1
    )
    result[APP_FRONTIER_RESTORE_ATTEMPTS_KEY] = attempts
    return result


_SOURCE_PAGE_PARENT_ROUTES: dict[str, tuple[str, ...]] = {
    "settings": ("profile", "home"),
    "payment": ("profile", "home"),
    "post_publish": ("post_list", "home"),
    "content_detail": ("content_list", "home"),
    "video_detail": ("video_list", "home"),
    "activity": ("home",),
}


def build_app_frontier_restore_intent(
    *,
    work: ExplorationWork,
    item: AppFrontierItem,
) -> ActionIntent:
    """只生成恢复动作或当前视觉重新确认的候选，禁止重放旧坐标。"""

    if (
        item.task_id != work.task_id
        or item.session_id != work.session_id
        or item.surface_kind is not work.surface_kind
    ):
        raise AppFrontierRestorationError(
            "App Frontier sibling 与当前 Work 归属不一致",
            error_code="APP_FRONTIER_SCOPE_MISMATCH",
        )
    # 旧合成覆盖寻页合同不得恢复执行：禁止按合同重放坐标或冒充底栏重绑。
    if item.candidate.metadata.get("coverage_page_seek_contract") is not None:
        raise AppFrontierRestorationError(
            "旧合成覆盖寻页合同不能恢复执行，必须重新识别后再执行",
            error_code="APP_FRONTIER_CANDIDATE_REVALIDATION_REQUIRED",
            retryable=True,
        )
    # 外链／浏览器等越界前台禁止执行任何 sibling 点击；浏览器用 BACK，
    # 系统桌面用 warm_restore 拉回目标包。
    # 否则视觉误判成 home 时会把 Chrome 候选当同源 sibling，直接触发包边界终态。
    outside_package = not work_within_app_package_boundary(work)
    observation = work.frontier.observation
    facts = observation.metadata.get("surface_facts")
    page_state = str(observation.page_state or "").strip().casefold()
    # page_state 缺失时不当遮挡：避免空态把所有 restore 打进清障死胡同。
    obstructed = bool(page_state) and (
        page_state
        not in {
            "stable",
            "normal",
            "loaded",
            "content_available",
        }
        or is_obstructed_surface(
            page_state=observation.page_state,
            page_type=observation.page_type,
            overlay_signature=observation.overlay_signature,
            surface_facts=facts if isinstance(facts, Mapping) else {},
        )
    )
    is_bottom_nav_seek = _frontier_item_is_bottom_nav(item)
    is_global_navigation = (
        item.candidate.metadata.get("frontier_scope") == "app" or is_bottom_nav_seek
    )
    # 账号凭证弹层：任意 sibling（含非底栏关层／上方分类）都不得点下方 X。
    # 已到「我的」后必须保存凭证→我已保存才能收口。
    if not outside_package:
        credential_intent = _profile_credential_restore_intent(work=work, item=item)
        if credential_intent is not None:
            return credential_intent
        from services.api.app.application.app_coverage_page_seek import (
            playbook_bottom_tabs_hard_incomplete,
        )
        from services.api.app.application.app_semantic_progress import (
            VISITED_BOTTOM_NAVIGATION_LABELS_KEY,
            bottom_tab_label_already_visited,
        )

        visited_raw = observation.metadata.get(VISITED_BOTTOM_NAVIGATION_LABELS_KEY)
        visited_labels: set[str] = set()
        if isinstance(visited_raw, (list, tuple, set, frozenset)):
            visited_labels = {
                str(item) for item in visited_raw if str(item or "").strip()
            }
        if (
            bottom_tab_label_already_visited("我的", visited_labels)
            and not playbook_bottom_tabs_hard_incomplete(
                coverage_metadata=observation.metadata
            )
        ):
            # 底栏已到「我的」且硬覆盖完成：禁止再恢复旧关层／上方分类 sibling 空转。
            raise AppFrontierRestorationError(
                "底栏课纲已到「我的」并完成硬覆盖，禁止再恢复旧 sibling 空转。",
                error_code="APP_FRONTIER_CANDIDATE_REVALIDATION_REQUIRED",
                retryable=False,
            )
    # 非底栏 sibling：弹层帧先清遮挡。底栏 sibling 必须先尝试条带重绑，
    # 否则广告 dismiss 会永久占住 restore_only，底栏 attempt 永远到不了点击。
    if not outside_package and obstructed and not is_bottom_nav_seek:
        recovery = next(
            (
                candidate
                for candidate in _restoration_candidate_pool(work)
                if is_visual_obstruction_control(candidate)
                and candidate.metadata.get("source_capture_id") in {None, work.source_capture_id}
            ),
            None,
        )
        if recovery is not None:
            return _intent(
                work=work,
                candidate=recovery,
                item=item,
                purpose="先通过当前视觉控件清除遮挡，再重新确认待探索入口。",
                restoration_only=True,
            )
        # 明确弹层／遮罩帧且无清障控件：禁止重放非底栏业务导航。
        if page_state in {
            "overlay",
            "popup",
            "modal",
            "dialog",
            "blocked",
            "obstructed",
        }:
            raise AppFrontierRestorationError(
                "当前页面被遮挡且缺少可执行恢复控件，禁止重放业务导航",
                error_code="APP_FRONTIER_SOURCE_UNRECOVERABLE",
                retryable=True,
            )
    current_page_identity = work.frontier.observation.metadata.get("business_page_identity")
    # 底栏全局 sibling：弹层帧仍可按水平槽位重验；禁止因 obstructed 直接放弃重绑。
    allow_revalidate = not outside_package and (
        not obstructed or is_global_navigation or is_bottom_nav_seek
    )
    candidate = (
        _revalidated_candidate(work=work, item=item) if allow_revalidate else None
    )
    # 底栏：重验命中已访／已选中／空点耗尽栏时作废，改走 substitute 换栏。
    if (
        is_bottom_nav_seek
        and candidate is not None
        and not _bottom_nav_candidate_still_eligible(work=work, candidate=candidate)
    ):
        candidate = None
    is_v6_same_page_class = (
        item.candidate.metadata.get(APP_FRONTIER_FINGERPRINT_STRATEGY)
        in {
            APP_GLOBAL_NAVIGATION_FINGERPRINT_V6,
            APP_GLOBAL_NAVIGATION_FINGERPRINT_V7,
            APP_GLOBAL_NAVIGATION_FINGERPRINT_V8,
        }
        and isinstance(current_page_identity, str)
        and _source_page_semantic_class(current_page_identity)
        == _source_page_semantic_class(item.source_page_identity)
    )
    source_revalidated = (
        current_page_identity == item.source_page_identity or is_v6_same_page_class
    )
    if not outside_package and not obstructed and source_revalidated and candidate is None:
        # 底栏：同页类但标签未重现时改走当前帧替代栏／阻断，禁止空水平槽。
        if not is_bottom_nav_seek:
            # v6+ 页面／全局候选只要求恢复到同一语义页型；若候选仍未重现，继续点击
            # 同页导航锚点只会因 depth/scope 或别名变化形成循环，应关闭当前 sibling。
            raise AppFrontierRestorationError(
                "来源页面候选已变化，必须重新识别后再执行",
                error_code="APP_FRONTIER_CANDIDATE_REVALIDATION_REQUIRED",
                retryable=True,
            )
    if (
        not outside_package
        and is_bottom_nav_seek
        and _should_back_out_before_bottom_nav_slot_rebind(work)
    ):
        back = _bottom_nav_escape_back_candidate(work=work)
        if back is not None:
            return _intent(
                work=work,
                candidate=back,
                item=item,
                purpose="会员中心先点左上角返回，禁止点套餐或底栏。",
                restoration_only=True,
            )
    if not outside_package and candidate is not None and (
        source_revalidated or is_global_navigation or is_bottom_nav_seek
    ):
        # 弹窗流程未满足：禁止 Frontier sibling 点底栏（假 completed）。
        if is_bottom_nav_seek and popup_phase_blocks_bottom_tabs(
            work.frontier.observation.metadata
        ):
            # 账号凭证：禁止关下方 X，必须走保存凭证→我已保存。
            credential_intent = _profile_credential_restore_intent(
                work=work, item=item
            )
            if credential_intent is not None:
                return credential_intent
            recovery = next(
                (
                    item_candidate
                    for item_candidate in _restoration_candidate_pool(work)
                    if (
                        is_visual_obstruction_control(item_candidate)
                        or item_candidate.metadata.get("popup_close_bottom_x_fallback")
                        is True
                        or item_candidate.metadata.get("popup_close_corner_fallback")
                        is True
                    )
                    and item_candidate.metadata.get("source_capture_id")
                    in {None, work.source_capture_id}
                ),
                None,
            )
            if recovery is not None:
                return _intent(
                    work=work,
                    candidate=recovery,
                    item=item,
                    purpose="弹窗流程未满足，禁止底栏 sibling；先关闭弹层并截图。",
                    restoration_only=True,
                )
            raise AppFrontierRestorationError(
                "弹窗流程未满足（popup_satisfied=false），禁止执行底栏 sibling",
                error_code="APP_POPUP_PHASE_BLOCKS_BOTTOM_TABS",
                retryable=True,
            )
        # 上方分类未完：禁止底栏 sibling 抢跑，必须先点最新→国产→动漫→漫画。
        if is_bottom_nav_seek:
            from services.api.app.application.app_coverage_page_seek import (
                home_top_category_tabs_incomplete,
            )
            from services.api.app.application.app_semantic_progress import (
                short_drama_exit_pending,
                short_drama_should_tap_top_left_back,
            )

            metadata = work.frontier.observation.metadata
            pkg = metadata.get("package_name") if isinstance(metadata, Mapping) else None
            package_name = pkg.strip() if isinstance(pkg, str) and pkg.strip() else None
            if package_name is None and work.app_goal is not None:
                allowed = getattr(work.app_goal, "allowed_packages", ()) or ()
                if allowed:
                    package_name = str(allowed[0]).strip() or None
            if home_top_category_tabs_incomplete(
                coverage_metadata=metadata if isinstance(metadata, Mapping) else None,
                package_name=package_name,
            ):
                raise AppFrontierRestorationError(
                    "主页上方分类课纲未完，禁止执行底栏 sibling",
                    error_code="APP_HOME_TOP_CATEGORY_BLOCKS_BOTTOM_TABS",
                    retryable=True,
                )
            # 仅沉浸播放器（须点左上角返回）才挡底栏；Tab 内嵌无箭头时允许切栏。
            meta_map = metadata if isinstance(metadata, Mapping) else None
            if short_drama_exit_pending(meta_map) and short_drama_should_tap_top_left_back(
                coverage_metadata=meta_map,
                candidates=work.frontier.candidates,
                key_elements=work.frontier.observation.key_elements,
            ):
                raise AppFrontierRestorationError(
                    "短剧待退出（见解锁层须先点左上角返回），禁止执行底栏 sibling",
                    error_code="APP_SHORT_DRAMA_EXIT_BLOCKS_BOTTOM_TABS",
                    retryable=True,
                )
        return _intent(
            work=work,
            candidate=candidate,
            item=item,
            purpose="当前截图已重新确认 sibling 候选，执行任务级 Frontier 分支。",
        )
    # 底栏 sibling 当前帧未重现：弹层帧先清遮挡；否则 warm_restore；
    # 仍无条带时签发源候选并由 Runtime 强制按当前帧底部带重绑（禁止直接点旧坐标）。
    if not outside_package and is_bottom_nav_seek and candidate is None:
        # 付费／详情／上栏取证后：先系统 BACK，禁止点「关闭 X」
        # （广告关闭钮常会外跳浏览器／下载页）。
        if _should_back_out_before_bottom_nav_slot_rebind(work):
            back = _bottom_nav_escape_back_candidate(work=work)
            if back is not None:
                return _intent(
                    work=work,
                    candidate=back,
                    item=item,
                    purpose="底栏条带被付费／全屏遮罩挡住，先返回离开详情再重确认底栏。",
                    restoration_only=True,
                )
            raise AppFrontierRestorationError(
                "底栏条带被遮罩挡住且缺少可执行返回，等待离开详情后再寻栏",
                error_code="APP_FRONTIER_SOURCE_UNRECOVERABLE",
                retryable=True,
            )
        if obstructed:
            # 我的页账号凭证：不得当成普通弹窗下方 X 清障（会永远点不中「我已保存」）。
            credential_intent = _profile_credential_restore_intent(
                work=work, item=item
            )
            if credential_intent is not None:
                return credential_intent
            recovery = next(
                (
                    item_candidate
                    for item_candidate in _restoration_candidate_pool(work)
                    if is_visual_obstruction_control(item_candidate)
                    and item_candidate.metadata.get("source_capture_id")
                    in {None, work.source_capture_id}
                ),
                None,
            )
            if recovery is not None:
                return _intent(
                    work=work,
                    candidate=recovery,
                    item=item,
                    purpose="底栏 sibling 尚未重现，先清除遮挡再重新确认条带。",
                    restoration_only=True,
                )
        # 当前帧完全看不到底栏条带：禁止空水平槽硬重绑（Runtime 必失败，烧回合）。
        if not _current_frame_has_bottom_nav_band(work):
            back = _bottom_nav_escape_back_candidate(work=work)
            if back is not None:
                return _intent(
                    work=work,
                    candidate=back,
                    item=item,
                    purpose="当前帧无底栏条带，先返回根页再重确认底栏 sibling。",
                    restoration_only=True,
                )
            raise AppFrontierRestorationError(
                "当前帧无底栏条带，禁止空水平槽重绑",
                error_code="APP_FRONTIER_SOURCE_UNRECOVERABLE",
                retryable=True,
            )
        # 有底栏条带但无同标签：禁止水平槽硬重绑（标签错了点哪都是乱点）。
        # 优先改点当前帧其它未访 playbook 栏；否则判不可恢复，交给当前帧重新 promote。
        substitute = _current_frame_bottom_nav_substitute(work=work, item=item)
        if substitute is not None:
            return _intent(
                work=work,
                candidate=substitute,
                item=item,
                purpose="历史底栏标签当前帧未重现，改点当前帧可见未访底栏并截图留证。",
            )
        raise AppFrontierRestorationError(
            "当前帧无同标签底栏，禁止空水平槽重绑",
            error_code="APP_FRONTIER_SOURCE_UNRECOVERABLE",
            retryable=True,
        )
    navigation_anchor = (
        None
        if outside_package or obstructed
        else _source_page_navigation_anchor(work=work, item=item)
    )
    if navigation_anchor is not None:
        anchor_scope = navigation_anchor.metadata.get("frontier_scope")
        anchor_name = "全局导航锚点" if anchor_scope == "app" else "页面导航锚点"
        return _intent(
            work=work,
            candidate=navigation_anchor,
            item=item,
            purpose=f"通过当前截图确认的{anchor_name}恢复 sibling 来源页面。",
            restoration_only=True,
        )
    visible_back = (
        None if outside_package else _explicit_visible_back_candidate(work=work)
    )
    if visible_back is not None:
        return _intent(
            work=work,
            candidate=visible_back,
            item=item,
            purpose="通过当前截图确认的显式返回控件恢复 sibling 来源页面。",
            restoration_only=True,
        )
    recovery = next(
        (
            candidate
            for candidate in _restoration_candidate_pool(work)
            if candidate.action_type
            in {InteractionOperation.BACK, InteractionOperation.WARM_RESTORE}
            and candidate.risk_level is not InteractionRiskLevel.HIGH
        ),
        None,
    )
    if recovery is None:
        if outside_package:
            raise AppFrontierRestorationError(
                "当前前台已离开目标 App 包边界，且缺少可执行的恢复候选",
                error_code="APP_FRONTIER_PACKAGE_BOUNDARY",
            )
        # 付费／详情遮罩帧常无 Vision BACK：合成系统返回，避免 sibling 永久卡死。
        if _should_back_out_before_bottom_nav_slot_rebind(work) or str(
            observation.page_type or ""
        ).casefold() in {
            "payment",
            "video_detail",
            "content_detail",
            "post_detail",
        }:
            recovery = _bottom_nav_escape_back_candidate(work=work)
        if recovery is None:
            raise AppFrontierRestorationError(
                "当前页面无法通过受控 BACK 恢复 sibling 来源",
                error_code="APP_FRONTIER_SOURCE_UNRECOVERABLE",
                # 底栏／全局 sibling：当前帧可能暂无 Vision 底栏，允许有界重试等下一帧。
                retryable=is_global_navigation or is_bottom_nav_seek,
            )
    purpose = (
        "当前前台已离开目标 App 包边界，只允许恢复目标包后再执行 sibling。"
        if outside_package
        else "恢复 sibling 来源页面；动作后必须重新截图识别，禁止直接执行旧候选。"
    )
    return _intent(
        work=work,
        candidate=recovery,
        item=item,
        purpose=purpose,
        restoration_only=True,
    )


def bind_app_frontier_restoration_candidate(
    *,
    work: ExplorationWork,
    decision: ActionIntent,
) -> ExplorationWork:
    """把只保留在当前帧恢复池的候选受控回绑到本轮父 Frontier。"""

    candidate_id = decision.parameters.get("candidate_id")
    if any(
        item.candidate_id == candidate_id
        for item in work.frontier.candidates
    ):
        return work
    candidate = next(
        (
            item
            for item in _restoration_candidate_pool(work)
            if item.candidate_id == candidate_id
        ),
        None,
    )
    if candidate is None:
        raw_inject = decision.parameters.get("app_frontier_inject_candidate")
        if isinstance(raw_inject, Mapping):
            try:
                candidate = CandidateAction.from_dict(raw_inject)
            except (TypeError, ValueError):
                candidate = None
        if candidate is None or candidate.candidate_id != candidate_id:
            raise AppFrontierRestorationError(
                "恢复动作候选不属于当前截图的受控恢复池",
                error_code="APP_FRONTIER_RESTORATION_CANDIDATE_UNBOUND",
            )
        # 允许注入：底栏巡检重绑，或账号凭证二段式（保存凭证／我已保存）。
        allow_inject = (
            candidate.metadata.get("bottom_navigation_tab_seek") is True
            or candidate.metadata.get("profile_credential_save") is True
            or candidate.metadata.get("profile_credential_dismiss") is True
        )
        if not allow_inject:
            raise AppFrontierRestorationError(
                "仅允许注入底栏巡检或账号凭证候选",
                error_code="APP_FRONTIER_RESTORATION_CANDIDATE_UNBOUND",
            )
    if (
        decision.task_id != work.task_id
        or decision.session_id != work.session_id
        or decision.surface_kind is not work.surface_kind
        or decision.source_capture_id != work.source_capture_id
        or decision.action_type is not candidate.action_type
        or decision.semantic_target != candidate.semantic_target
        or decision.risk_level is not candidate.risk_level
        or candidate.risk_level is InteractionRiskLevel.HIGH
        or candidate.expected_surface_kind not in {None, work.surface_kind}
    ):
        raise AppFrontierRestorationError(
            "恢复动作与当前截图候选身份不一致",
            error_code="APP_FRONTIER_RESTORATION_CANDIDATE_MISMATCH",
        )
    return replace(
        work,
        frontier=replace(
            work.frontier,
            candidates=(*work.frontier.candidates, candidate),
        ),
    )


def _source_page_navigation_anchor(
    *,
    work: ExplorationWork,
    item: AppFrontierItem,
) -> CandidateAction | None:
    """按目标页型选择当前截图锚点，并通过父页逐跳恢复。"""

    source_page_class = _source_page_semantic_class(item.source_page_identity)
    current_identity = work.frontier.observation.metadata.get("business_page_identity")
    current_page_class = (
        _source_page_semantic_class(current_identity)
        if isinstance(current_identity, str)
        else None
    )
    target_page_classes = (
        source_page_class,
        *_SOURCE_PAGE_PARENT_ROUTES.get(source_page_class, ("home",)),
    )
    ranked: list[tuple[int, int, int, CandidateAction]] = []
    for candidate_index, candidate in enumerate(_restoration_candidate_pool(work)):
        if (
            candidate.action_type is not InteractionOperation.CLICK
            or candidate.risk_level is InteractionRiskLevel.HIGH
            or candidate.metadata.get("frontier_scope") not in {"app", "page"}
        ):
            continue
        expected_page_class = str(
            candidate.metadata.get("expected_next_page_type")
            or candidate.metadata.get("expected_state_hint")
            or ""
        ).strip().casefold()
        if expected_page_class not in target_page_classes:
            continue
        target_rank = target_page_classes.index(expected_page_class)
        if target_rank > 0 and expected_page_class == current_page_class:
            continue
        scope_rank = 0 if candidate.metadata.get("frontier_scope") == "app" else 1
        ranked.append((target_rank, scope_rank, candidate_index, candidate))
    return min(ranked, default=None, key=lambda value: value[:3])[-1] if ranked else None


def _should_back_out_before_bottom_nav_slot_rebind(work: ExplorationWork) -> bool:
    """付费／详情全屏遮罩且无底栏条带时，禁止签发水平槽重绑。"""

    observation = work.frontier.observation
    raw_facts = observation.metadata.get("surface_facts")
    facts = raw_facts if isinstance(raw_facts, Mapping) else {}
    dialog_kind = str(facts.get("dialog_kind") or "").casefold()
    if dialog_kind in {
        "unlock_paywall",
        "paywall",
        "purchase",
        "vip_unlock",
        "vip_purchase",
        "payment",
        "membership",
    }:
        return True
    page_type = str(observation.page_type or "").casefold()
    if page_type == "payment":
        return True
    metadata = observation.metadata if isinstance(observation.metadata, Mapping) else {}
    summary_blob = " ".join(
        (
            str(metadata.get("visual_summary") or ""),
            str(observation.page_type or ""),
            " ".join(str(item) for item in (observation.key_elements or ())),
        )
    )
    if "会员中心" in summary_blob:
        return True
    from services.api.app.application.app_coverage_page_seek import (
        HOME_TOP_SAMPLE_PHASE_NEED_BACK,
        HOME_TOP_SAMPLE_PHASE_NEED_EVIDENCE,
        home_top_category_sample_in_progress,
        home_top_category_sample_phase,
    )

    if home_top_category_sample_in_progress(metadata):
        phase = home_top_category_sample_phase(metadata)
        if phase in {
            HOME_TOP_SAMPLE_PHASE_NEED_BACK,
            HOME_TOP_SAMPLE_PHASE_NEED_EVIDENCE,
        }:
            return True
    if page_type not in {"video_detail", "content_detail"}:
        return False
    page_state = str(observation.page_state or "").casefold()
    overlay_marked = (
        page_state in {"overlay", "popup", "modal", "dialog", "blocked"}
        or facts.get("popup_visible") is True
        or facts.get("dialog_visible") is True
    )
    if not overlay_marked:
        return False
    controls = facts.get("trusted_accessibility_controls")
    if isinstance(controls, Mapping):
        navigation = controls.get("bottom_navigation")
        if isinstance(navigation, (list, tuple)) and len(navigation) > 0:
            return False
    return True


def _current_frame_has_bottom_nav_band(work: ExplorationWork) -> bool:
    """当前帧是否具备可重绑的底栏条带（a11y 或 Vision 底带）。"""

    observation = work.frontier.observation
    raw_facts = observation.metadata.get("surface_facts")
    facts = raw_facts if isinstance(raw_facts, Mapping) else {}
    controls = facts.get("trusted_accessibility_controls")
    if isinstance(controls, Mapping):
        navigation = controls.get("bottom_navigation")
        if isinstance(navigation, (list, tuple)) and len(navigation) > 0:
            return True
        count = controls.get("bottom_navigation_count")
        if isinstance(count, int) and not isinstance(count, bool) and count > 0:
            return True
    for candidate in _restoration_candidate_pool(work):
        if candidate.metadata.get("bottom_navigation_tab_seek") is not True:
            continue
        raw = candidate.metadata.get("normalized_bounds")
        if not isinstance(raw, Mapping):
            continue
        try:
            top = float(raw.get("top") or 0.0)
            bottom = float(raw.get("bottom") or 0.0)
        except (TypeError, ValueError):
            continue
        if top >= 0.78 and bottom > top:
            return True
    return False


def _bottom_nav_selected_norms_from_metadata(
    metadata: Mapping[str, object],
) -> set[str]:
    """从 observation metadata 读取当前选中底栏归一标签。"""

    from services.api.app.application.app_semantic_progress import (
        _normalize_bottom_tab_label_for_visit,
    )

    selected_norms: set[str] = set()
    raw_facts = metadata.get("surface_facts")
    if not isinstance(raw_facts, Mapping):
        return selected_norms
    raw_selected = raw_facts.get("pixel_selected_labels")
    if isinstance(raw_selected, (list, tuple)):
        selected_norms.update(
            _normalize_bottom_tab_label_for_visit(item)
            for item in raw_selected
            if isinstance(item, str)
        )
    controls = raw_facts.get("trusted_accessibility_controls")
    if isinstance(controls, Mapping):
        bottom = controls.get("bottom_navigation")
        if isinstance(bottom, (list, tuple)):
            for entry in bottom:
                if not isinstance(entry, Mapping) or entry.get("selected") is not True:
                    continue
                raw = (
                    entry.get("label")
                    or entry.get("text")
                    or entry.get("content_desc")
                )
                norm = _normalize_bottom_tab_label_for_visit(raw)
                if norm:
                    selected_norms.add(norm)
    return {item for item in selected_norms if item}


def _bottom_nav_candidate_still_eligible(
    *,
    work: ExplorationWork,
    candidate: CandidateAction,
) -> bool:
    """底栏重验命中后仍须未访／未选中／未空点耗尽，否则改走 substitute。"""

    from services.api.app.application.app_semantic_progress import (
        _normalize_bottom_tab_label_for_visit,
        bottom_nav_label_attempts_exhausted,
        bottom_tab_label_already_visited,
    )

    observation = work.frontier.observation
    metadata = observation.metadata if isinstance(observation.metadata, Mapping) else {}
    label = _normalize_bottom_tab_label_for_visit(candidate.semantic_target)
    if not label:
        return False
    visited_raw = metadata.get("visited_bottom_navigation_labels")
    visited = {
        _normalize_bottom_tab_label_for_visit(item_label)
        for item_label in (visited_raw if isinstance(visited_raw, (list, tuple)) else ())
        if isinstance(item_label, str)
    }
    if bottom_tab_label_already_visited(label, visited):
        return False
    if bottom_tab_label_already_visited(
        label, _bottom_nav_selected_norms_from_metadata(metadata)
    ):
        return False
    if bottom_nav_label_attempts_exhausted(label, metadata):
        return False
    return True


def _current_frame_bottom_nav_substitute(
    *,
    work: ExplorationWork,
    item,
) -> CandidateAction | None:
    """历史底栏标签缺失时，改点当前帧其它未访底栏（禁止水平槽猜点）。"""

    from services.api.app.application.app_coverage_page_seek import (
        _normalize_bottom_tab_label,
        _playbook_bottom_tab_labels,
        _playbook_label_allows,
        coverage_incomplete_allows_bottom_nav_through_overlay,
    )
    from services.api.app.application.app_semantic_progress import (
        _normalize_bottom_tab_label_for_visit,
        bottom_tab_label_already_visited,
    )

    observation = work.frontier.observation
    metadata = observation.metadata if isinstance(observation.metadata, Mapping) else {}
    if not coverage_incomplete_allows_bottom_nav_through_overlay(
        coverage_metadata=metadata,
    ):
        return None
    wanted = _normalize_bottom_tab_label_for_visit(
        getattr(item.candidate, "semantic_target", None)
    )
    playbook_labels = _playbook_bottom_tab_labels(metadata)
    pool = [
        candidate
        for candidate in _restoration_candidate_pool(work)
        if candidate.metadata.get("bottom_navigation_tab_seek") is True
        and candidate.action_type
        in {InteractionOperation.CLICK, InteractionOperation.TAP}
        and candidate.risk_level is not InteractionRiskLevel.HIGH
        and candidate.metadata.get("require_runtime_bottom_nav_rebind") is not True
        and _bottom_nav_candidate_still_eligible(work=work, candidate=candidate)
        and (
            not playbook_labels
            or _playbook_label_allows(
                _normalize_bottom_tab_label(candidate.semantic_target),
                playbook_labels,
            )
        )
    ]
    if not pool:
        return None

    def _label(candidate: CandidateAction) -> str:
        return _normalize_bottom_tab_label_for_visit(candidate.semantic_target)

    def _playbook_key(candidate: CandidateAction) -> tuple[int, float, str]:
        raw_index = candidate.metadata.get("playbook_tab_index")
        try:
            index = int(raw_index) if raw_index is not None else 10_000
        except (TypeError, ValueError):
            index = 10_000
        left, cid = _bottom_nav_left_key(candidate)
        return (index, left, cid)

    # 先尽量命中原标签别名（须仍合格）；否则按 playbook 顺序点其它未访栏。
    alias_hits = [
        candidate
        for candidate in pool
        if wanted and bottom_tab_label_already_visited(wanted, {_label(candidate)})
    ]
    if alias_hits:
        return min(alias_hits, key=_playbook_key)
    return min(pool, key=_playbook_key)


def _bottom_nav_left_key(candidate: CandidateAction) -> tuple[float, str]:
    raw = candidate.metadata.get("normalized_bounds")
    left = 1.0
    if isinstance(raw, Mapping):
        try:
            left = float(raw.get("left") or 1.0)
        except (TypeError, ValueError):
            left = 1.0
    return (left, str(candidate.candidate_id or ""))


def _bottom_nav_escape_back_candidate(*, work: ExplorationWork) -> CandidateAction | None:
    """优先点可见左上角「<」；无视觉返回时用语义 BACK（F02 禁止合成坐标点击）。"""

    visible = _explicit_visible_back_candidate(work=work)
    if visible is not None:
        return visible
    system_back = next(
        (
            candidate
            for candidate in _restoration_candidate_pool(work)
            if candidate.action_type is InteractionOperation.BACK
            and candidate.risk_level is not InteractionRiskLevel.HIGH
        ),
        None,
    )
    if system_back is not None:
        return system_back
    capture_id = str(
        work.source_capture_id or work.frontier.observation.capture_id or "unknown"
    )
    return CandidateAction(
        candidate_id=f"forced-system-back:{capture_id}",
        semantic_target="系统 BACK",
        action_type=InteractionOperation.BACK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=work.surface_kind,
        metadata={
            "purpose": (
                "底栏条带被套餐／详情挡住；无左上角视觉返回时使用授权语义 BACK 后再寻栏。"
            ),
            "frontier_scope": "local",
            "expected_state_hint": "back",
            "forced_detail_exit_back": True,
            "post_detail_exit_prefer_tabs": True,
            "authorized_semantic_back": True,
        },
    )


def _explicit_visible_back_candidate(*, work: ExplorationWork) -> CandidateAction | None:
    """只接受当前截图上的明确返回控件，避免把含“返回”的业务入口误作后退。"""

    allowed_targets = {
        "back",
        "backbutton",
        "navigateup",
        "up",
        "后退",
        "返回",
        "返回按钮",
        "返回键",
        "左上角返回",
        "navigateback",
    }
    allowed_purposes = {
        "goback",
        "navigatetopreviouspage",
        "后退一页",
        "回到上一页",
        "返回上一级",
        "返回上一页",
        "返回上一页面",
        "返回前一页",
    }
    return next(
        (
            candidate
            for candidate in _restoration_candidate_pool(work)
            if candidate.action_type is InteractionOperation.CLICK
            and candidate.risk_level is not InteractionRiskLevel.HIGH
            and candidate.metadata.get("frontier_scope") == "local"
            and (
                _normalized_restoration_label(candidate.semantic_target)
                in allowed_targets
                or _normalized_restoration_label(candidate.metadata.get("purpose"))
                in allowed_purposes
                or "<" in str(candidate.semantic_target or "")
                or "左上角" in str(candidate.semantic_target or "")
            )
        ),
        None,
    )


def _normalized_restoration_label(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _revalidated_candidate(
    *,
    work: ExplorationWork,
    item: AppFrontierItem,
) -> CandidateAction | None:
    if item.candidate.metadata.get("coverage_page_seek_contract") is not None:
        # 旧合成合同不能仅凭预期页型匹配另一控件，更不能重放坐标。
        return None

    missing_goal_candidate = _revalidated_missing_post_publish_entry(
        work=work,
        item=item,
    )
    if missing_goal_candidate is not None:
        return missing_goal_candidate

    fingerprint_strategy = item.candidate.metadata.get(
        APP_FRONTIER_FINGERPRINT_STRATEGY
    )
    semantic_fingerprint = fingerprint_strategy in {
        APP_GLOBAL_NAVIGATION_FINGERPRINT_V2,
        APP_GLOBAL_NAVIGATION_FINGERPRINT_V3,
        APP_GLOBAL_NAVIGATION_FINGERPRINT_V4,
        APP_GLOBAL_NAVIGATION_FINGERPRINT_V5,
        APP_GLOBAL_NAVIGATION_FINGERPRINT_V6,
        APP_GLOBAL_NAVIGATION_FINGERPRINT_V7,
        APP_GLOBAL_NAVIGATION_FINGERPRINT_V8,
    }
    stored_scope = item.candidate.metadata.get("frontier_scope")
    for candidate in _restoration_candidate_pool(work):
        if semantic_fingerprint and (
            candidate.risk_level is not InteractionRiskLevel.HIGH
            and candidate.metadata.get("frontier_scope") == stored_scope
            and (
                stored_scope == "app"
                or fingerprint_strategy
                in {
                    APP_GLOBAL_NAVIGATION_FINGERPRINT_V6,
                    APP_GLOBAL_NAVIGATION_FINGERPRINT_V7,
                    APP_GLOBAL_NAVIGATION_FINGERPRINT_V8,
                }
            )
            and build_app_frontier_candidate_fingerprint(
                source_page_identity=item.source_page_identity,
                candidate=with_app_frontier_fingerprint_strategy(
                    candidate,
                    strategy=fingerprint_strategy,
                ),
            )
            == item.candidate_fingerprint
        ):
            return candidate
        if not semantic_fingerprint and (
            candidate.risk_level is not InteractionRiskLevel.HIGH
            and candidate.metadata.get("frontier_scope") in {"page", "app"}
            and candidate.action_type is item.candidate.action_type
            and candidate.semantic_target.casefold() == item.candidate.semantic_target.casefold()
            and str(candidate.metadata.get("expected_state_hint") or "").casefold()
            == str(item.candidate.metadata.get("expected_state_hint") or "").casefold()
        ):
            return candidate
    # 底栏全局：指纹／精确文案常因「底部Tab直播」vs「底栏「直播」」漂移失败；
    # 按归一标签弱匹配当前帧 app 条带候选，避免 SOURCE_UNRECOVERABLE。
    if stored_scope == "app" or item.candidate.metadata.get("bottom_navigation_tab_seek") is True:
        target_label = _bottom_nav_core_label(item.candidate.semantic_target)
        pool = [
            candidate
            for candidate in _restoration_candidate_pool(work)
            if candidate.risk_level is not InteractionRiskLevel.HIGH
            and candidate.action_type is item.candidate.action_type
            and (
                candidate.metadata.get("frontier_scope") == "app"
                or candidate.metadata.get("bottom_navigation_tab_seek") is True
                or _candidate_looks_like_bottom_nav(candidate)
            )
        ]
        if target_label:
            soft = [
                candidate
                for candidate in pool
                if _bottom_nav_labels_compatible(
                    target_label,
                    _bottom_nav_core_label(candidate.semantic_target),
                )
            ]
            if len(soft) == 1:
                return soft[0]
            if len(soft) > 1:
                # 多命中时按水平中心贴近源候选。
                rebound = _nearest_bottom_nav_by_bounds(
                    source=item.candidate,
                    candidates=soft,
                )
                if rebound is not None:
                    return rebound
        # 标签全失配时，仍尝试按源候选水平槽位重绑（WebView 底栏常用）。
        rebound = _nearest_bottom_nav_by_bounds(
            source=item.candidate,
            candidates=pool,
            max_distance=0.12,
        )
        if rebound is not None:
            return rebound
    return None


def _frontier_item_is_bottom_nav(item: AppFrontierItem) -> bool:
    """识别底栏 sibling：metadata 优先，语义文案兜底。"""

    metadata = item.candidate.metadata
    if metadata.get("bottom_navigation_tab_seek") is True:
        return True
    if metadata.get("frontier_scope") == "app" and _candidate_looks_like_bottom_nav(
        item.candidate
    ):
        return True
    return _candidate_looks_like_bottom_nav(item.candidate)


def _candidate_looks_like_bottom_nav(candidate: CandidateAction) -> bool:
    """从语义／purpose／条带位置判断是否底栏入口。"""

    terms = " ".join(
        (
            str(candidate.semantic_target or ""),
            str(candidate.metadata.get("purpose") or ""),
            str(candidate.metadata.get("expected_state_hint") or ""),
        )
    ).casefold()
    if any(
        marker in terms
        for marker in (
            "底栏",
            "底部tab",
            "底部导航",
            "bottom navigation",
            "bottom_navigation",
            "tab:",
        )
    ):
        return True
    bounds = candidate.metadata.get("normalized_bounds")
    if isinstance(bounds, Mapping):
        try:
            top = float(bounds.get("top"))
        except (TypeError, ValueError):
            return False
        return top >= 0.78
    return False


def _bottom_nav_core_label(raw: object) -> str:
    """去掉底栏前缀后的核心标签，供 sibling 跨帧弱匹配。"""

    text = unicodedata.normalize("NFKC", str(raw or "")).strip().casefold()
    for prefix in (
        "底部tab",
        "底部导航",
        "底部栏",
        "底栏「",
        "底部「",
        "底栏",
        "底部",
    ):
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
    return text.strip("「」[]()（） ").casefold()


def _bottom_nav_labels_compatible(left: str, right: str) -> bool:
    """允许 AI／AI科技 一类前后缀相容，避免弱匹配过严。"""

    if not left or not right:
        return False
    if left == right:
        return True
    return left in right or right in left


def _nearest_bottom_nav_by_bounds(
    *,
    source: CandidateAction,
    candidates: list[CandidateAction],
    max_distance: float = 0.18,
) -> CandidateAction | None:
    """按源候选水平中心在当前帧底栏池中选最近且无歧义项。"""

    raw = source.metadata.get("normalized_bounds")
    if not isinstance(raw, Mapping):
        return None
    try:
        source_center = (float(raw["left"]) + float(raw["right"])) / 2.0
    except (KeyError, TypeError, ValueError):
        return None

    ranked: list[tuple[float, CandidateAction]] = []
    for candidate in candidates:
        bounds = candidate.metadata.get("normalized_bounds")
        if not isinstance(bounds, Mapping):
            continue
        try:
            center = (float(bounds["left"]) + float(bounds["right"])) / 2.0
        except (KeyError, TypeError, ValueError):
            continue
        ranked.append((abs(center - source_center), candidate))
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0])
    best_distance, best = ranked[0]
    if best_distance > max_distance:
        return None
    if len(ranked) > 1 and abs(ranked[1][0] - best_distance) < 0.05:
        return None
    return best


def _revalidated_missing_post_publish_entry(
    *,
    work: ExplorationWork,
    item: AppFrontierItem,
) -> CandidateAction | None:
    """在同一稳定列表页上按缺失页型重绑发布入口。

    Provider 可能在重访时把「发布加号按钮/发布新帖或内容」改写为
    「发布按钮/发布新帖」。旧坐标仍禁止重放，但当前截图中同样指向
    缺失 ``post_publish`` 的低风险入口应可重新绑定。最终「提交/确认
    发布」仍必须 fail-closed。
    """

    if work.app_goal is None or "post_publish" not in {
        page_type.casefold() for page_type in work.app_goal.required_page_types
    }:
        return None
    covered = {
        str(page_type).strip().casefold()
        for page_type in work.frontier.observation.metadata.get("covered_page_types", ())
        if isinstance(page_type, str) and page_type.strip()
    }
    if "post_publish" in covered:
        return None
    current_identity = work.frontier.observation.metadata.get("business_page_identity")
    if not isinstance(current_identity, str) or not _same_page_origin_and_class(
        current_identity,
        item.source_page_identity,
        allowed_page_classes={"content_list", "post_list"},
    ):
        return None
    if str(work.frontier.observation.page_state or "").strip().casefold() not in {
        "stable",
        "normal",
        "loaded",
        "content_available",
    }:
        return None
    if not _safe_post_publish_entry(item.candidate, allowed_scopes={"page"}):
        return None
    return next(
        (
            candidate
            for candidate in _restoration_candidate_pool(work)
            if _safe_post_publish_entry(candidate, allowed_scopes={"page"})
        ),
        None,
    )


def _same_page_origin_and_class(
    current_identity: str,
    source_identity: str,
    *,
    allowed_page_classes: set[str],
) -> bool:
    current = current_identity.casefold().split("|")
    source = source_identity.casefold().split("|")
    return (
        len(current) >= 3
        and len(source) >= 3
        and current[:2] == source[:2]
        and current[2] == source[2]
        and current[2] in allowed_page_classes
    )


def _safe_post_publish_entry(
    candidate: CandidateAction,
    *,
    allowed_scopes: set[str],
) -> bool:
    if (
        candidate.action_type
        not in {InteractionOperation.CLICK, InteractionOperation.TAP}
        or candidate.risk_level is not InteractionRiskLevel.LOW
        or candidate.metadata.get("frontier_scope") not in allowed_scopes
        or str(candidate.metadata.get("expected_next_page_type") or "").casefold()
        != "post_publish"
    ):
        return False
    terms = " ".join(
        (
            candidate.semantic_target,
            str(candidate.metadata.get("purpose") or ""),
            str(candidate.metadata.get("expected_state_hint") or ""),
        )
    ).casefold()
    if any(
        marker in terms
        for marker in (
            "提交发布",
            "确认发布",
            "立即发布",
            "submit post",
            "confirm post",
        )
    ):
        return False
    return any(marker in terms for marker in ("发帖", "发布", "post"))


def _source_page_semantic_class(source_page_identity: str) -> str:
    """与 v6 指纹一致地提取页型，供当前截图重新确认页面候选。"""

    parts = source_page_identity.casefold().split("|")
    if len(parts) >= 3 and parts[2].strip():
        return parts[2].strip()
    normalized = source_page_identity.casefold().strip()
    return normalized.removeprefix("page:")


def _restoration_candidate_pool(work: ExplorationWork) -> tuple[CandidateAction, ...]:
    """合并正常候选与去重前的当前帧导航锚点，绝不读取历史坐标。"""

    candidates = list(work.frontier.candidates)
    payloads = work.frontier.observation.metadata.get(
        APP_FRONTIER_RESTORATION_CANDIDATES,
        (),
    )
    if not isinstance(payloads, (list, tuple)):
        payloads = ()
    for payload in payloads:
        if not isinstance(payload, Mapping):
            continue
        try:
            candidates.append(CandidateAction.from_dict(payload))
        except (TypeError, ValueError):
            continue
    unique: dict[str, CandidateAction] = {}
    for candidate in candidates:
        unique.setdefault(candidate.candidate_id, candidate)
    return tuple(unique.values())


def _profile_credential_restore_intent(
    *,
    work: ExplorationWork,
    item: AppFrontierItem,
) -> ActionIntent | None:
    """底栏 restore 遇账号凭证弹层时签发保存／我已保存，禁止关下方 X。"""

    from services.api.app.application.app_semantic_progress import (
        PROFILE_CREDENTIAL_SAVE_ATTEMPT_KEY,
        PROFILE_CREDENTIAL_SAVE_DONE_KEY,
        VISITED_BOTTOM_NAVIGATION_LABELS_KEY,
        bottom_tab_label_already_visited,
    )
    from services.api.app.application.app_synthetic_touch_contracts import (
        profile_credential_dismiss_contract,
        profile_credential_save_contract,
    )
    from services.api.app.application.app_ui_tree_features import (
        observation_matches_profile_credential,
        profile_credential_save_bounds,
        profile_credential_save_phase_ready,
        profile_credential_skip_bounds,
    )
    from services.api.app.domain.exploration import NormalizedBounds

    observation = work.frontier.observation
    metadata = observation.metadata if isinstance(observation.metadata, Mapping) else {}
    pkg = metadata.get("package_name")
    package_name = pkg.strip() if isinstance(pkg, str) and pkg.strip() else None
    if package_name is None and work.app_goal is not None:
        allowed = getattr(work.app_goal, "allowed_packages", ()) or ()
        if allowed:
            package_name = str(allowed[0]).strip() or None
    visited_raw = metadata.get(VISITED_BOTTOM_NAVIGATION_LABELS_KEY)
    visited: set[str] = set()
    if isinstance(visited_raw, (list, tuple, set, frozenset)):
        visited = {str(item) for item in visited_raw if str(item or "").strip()}
    profile_tab_visited = bottom_tab_label_already_visited("我的", visited)
    facts = metadata.get("surface_facts")
    extra = " ".join(
        " ".join(
            (
                candidate.semantic_target,
                str(candidate.metadata.get("purpose") or ""),
                str(candidate.metadata.get("expected_state_hint") or ""),
            )
        )
        for candidate in _restoration_candidate_pool(work)
    )
    blob = " ".join(
        str(part or "")
        for part in (
            metadata.get("visual_summary"),
            metadata.get("ocr_text"),
            metadata.get("page_summary"),
            observation.page_type,
            observation.page_state,
            facts.get("dialog_kind") if isinstance(facts, Mapping) else None,
            " ".join(str(item) for item in (observation.key_elements or ())),
            extra,
        )
    )
    if not observation_matches_profile_credential(
        page_type=observation.page_type,
        page_state=observation.page_state,
        observation_text=blob,
        package_name=package_name,
        profile_tab_visited=profile_tab_visited,
    ):
        return None

    raw_attempts = metadata.get(PROFILE_CREDENTIAL_SAVE_ATTEMPT_KEY)
    attempts = (
        raw_attempts
        if isinstance(raw_attempts, int) and not isinstance(raw_attempts, bool)
        else 0
    )
    save_done = profile_credential_save_phase_ready(
        save_done_flag=metadata.get(PROFILE_CREDENTIAL_SAVE_DONE_KEY) is True,
        save_attempt_count=attempts,
        observation_text=blob,
        package_name=package_name,
    )

    capture_id = str(work.source_capture_id or observation.capture_id or "unknown")
    if save_done:
        preferred = profile_credential_skip_bounds(package_name=package_name)
        if preferred is None:
            return None
        zone_name, raw_bounds = preferred
        bounds = NormalizedBounds.from_dict(raw_bounds)
        purpose = (
            f"账号凭证已保存；点灰色「我已保存」（ui_tree:{zone_name}）关闭弹层；"
            "禁止底栏 sibling 清障点下方 X。"
        )
        contract = profile_credential_dismiss_contract(bounds=bounds, purpose=purpose)
        candidate = CandidateAction(
            candidate_id=f"forced-profile-credential-dismiss:{capture_id}",
            semantic_target="我已保存",
            action_type=InteractionOperation.CLICK,
            risk_level=InteractionRiskLevel.LOW,
            exploration_value=100,
            navigation_depth=0,
            expected_surface_kind=work.surface_kind,
            metadata={
                "source_capture_id": capture_id,
                "control_role": "profile_credential_dismiss",
                "purpose": purpose,
                "frontier_scope": "local",
                "expected_state_hint": "dismiss_profile_credential",
                "normalized_bounds": bounds.to_dict(),
                "profile_credential_dismiss": True,
                "profile_credential_dismiss_contract": contract,
            },
        )
        return _intent(
            work=work,
            candidate=candidate,
            item=item,
            purpose=purpose,
            restoration_only=True,
            inject_candidate=True,
        )

    preferred = profile_credential_save_bounds(package_name=package_name)
    if preferred is None:
        return None
    zone_name, raw_bounds = preferred
    bounds = NormalizedBounds.from_dict(raw_bounds)
    purpose = (
        f"点账号凭证弹层蓝色「保存凭证」（ui_tree:{zone_name}）；"
        "保存成功后再点「我已保存」；禁止底栏 sibling 清障点下方 X。"
    )
    contract = profile_credential_save_contract(bounds=bounds, purpose=purpose)
    candidate = CandidateAction(
        candidate_id=f"forced-profile-credential-save:{capture_id}",
        semantic_target="保存凭证",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=work.surface_kind,
        metadata={
            "source_capture_id": capture_id,
            "control_role": "profile_credential_confirm",
            "purpose": purpose,
            "frontier_scope": "local",
            "expected_state_hint": "save_profile_credential",
            "normalized_bounds": bounds.to_dict(),
            "profile_credential_save": True,
            "profile_credential_save_contract": contract,
        },
    )
    return _intent(
        work=work,
        candidate=candidate,
        item=item,
        purpose=purpose,
        restoration_only=True,
        inject_candidate=True,
    )


def _intent(
    *,
    work: ExplorationWork,
    candidate: CandidateAction,
    item: AppFrontierItem,
    purpose: str,
    restoration_only: bool = False,
    inject_candidate: bool = False,
) -> ActionIntent:
    digest = hashlib.sha256(
        json.dumps(
            [work.work_id, work.source_capture_id, item.frontier_item_id, candidate.candidate_id],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    parameters: dict[str, object] = {
        "candidate_id": candidate.candidate_id,
        "app_frontier_item_id": item.frontier_item_id,
        "app_frontier_candidate_fingerprint": item.candidate_fingerprint,
        "app_frontier_restore_only": restoration_only,
    }
    if candidate.metadata.get("bottom_navigation_tab_seek") is True:
        parameters["bottom_navigation_tab_seek"] = True
        # Runtime 硬闸：弹窗未满足不得执行底栏（禁止假 completed）。
        flow = work.frontier.observation.metadata.get("app_flow_contract")
        if isinstance(flow, Mapping):
            parameters["app_flow_contract"] = dict(flow)
        parameters["coverage_metadata"] = {
            key: work.frontier.observation.metadata[key]
            for key in ("app_flow_contract", "visited_bottom_navigation_labels")
            if key in work.frontier.observation.metadata
        }
    if candidate.metadata.get("require_runtime_bottom_nav_rebind") is True:
        parameters["require_runtime_bottom_nav_rebind"] = True
    if inject_candidate:
        parameters["app_frontier_inject_candidate"] = candidate.to_dict()
    if candidate.metadata.get("short_drama_top_left_back") is True:
        parameters["short_drama_top_left_back"] = True
        bounds = candidate.metadata.get("normalized_bounds")
        if isinstance(bounds, Mapping):
            parameters["normalized_bounds"] = dict(bounds)
    safe_parameters = candidate.metadata.get("action_parameters")
    if isinstance(safe_parameters, Mapping):
        parameters.update(dict(safe_parameters))
    progress = work.frontier.observation.metadata.get("app_curriculum_progress")
    if isinstance(progress, dict):
        phase_id = progress.get("current_phase_id")
    elif hasattr(progress, "get"):
        phase_id = progress.get("current_phase_id")
    else:
        phase_id = None
    if isinstance(phase_id, str) and phase_id.strip():
        parameters["app_curriculum_phase_id"] = phase_id
    attach_synthetic_touch_contracts(parameters, candidate.metadata)
    if candidate.action_type is InteractionOperation.SCROLL:
        parameters["direction"] = "down"
    return ActionIntent(
        task_id=work.task_id,
        session_id=work.session_id,
        intent_id=f"app-frontier-intent:{digest}",
        idempotency_key=f"app-frontier-intent:{digest}",
        surface_kind=work.surface_kind,
        source_capture_id=work.source_capture_id,
        action_type=candidate.action_type,
        semantic_target=candidate.semantic_target,
        parameters=parameters,
        purpose=purpose,
        risk_level=candidate.risk_level,
        capture_profile=CaptureProfilePolicy.for_surface(
            work.surface_kind,
            candidate.action_type,
        ),
        deadline=work.deadline,
    )
