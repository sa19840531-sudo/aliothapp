"""全产品共用的有状态阶段控制器（保留历史模块名以兼容已有进度）。

影片 → 上方分类 →（条件）漫画 → 底栏 → 我的 → 支付宝 inspect_only。
保留：①限步与超时 ②无状态 AI Tool ③约束输出。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from services.api.app.application.app_constrained_locate import ConstrainedLocateResult
from services.api.app.application.app_flow_navigation import (
    content_ready,
    tab_destination_confirmed,
    tab_role,
    visible_bottom_tabs,
)
from services.api.app.application.app_synthetic_touch_contracts import (
    attach_synthetic_touch_contracts,
)
from services.api.app.application.app_tangguo_flow_state import (
    TangguoFlowState,
    apply_flow_config,
    load_tangguo_flow_config,
    package_uses_tangguo_flow,
)
from services.api.app.application.app_tangguo_recognize import (
    is_comic_detail_not_reader,
    playback_progress_advanced,
    recognize_alipay,
    recognize_bottom_tab,
    recognize_comic_entry,
    recognize_page_state,
    recognize_start_read,
    recognize_top_tabs,
    recognize_video_entry,
    recognize_wallet_entry,
    select_semantic_video_candidate,
)
from services.api.app.application.capture_profiles import CaptureProfilePolicy
from services.api.app.domain.exploration import CandidateAction, ExplorationWork
from services.api.app.domain.interaction import (
    ActionIntent,
    InteractionOperation,
    InteractionRiskLevel,
)

_HOME_PAGES = frozenset({"home"})
_PROFILE_PAGES = frozenset({"profile", "mine"})
_VIDEO_OK = frozenset({"video_detail", "player", "content_detail"})
_DEFAULT_TAB_ORDER: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("首页", ("首页", "首頁", "主页", "主頁", "home")),
    ("剧库", ("剧库", "劇庫", "剧场", "劇場", "library")),
    ("投票", ("投票", "vote", "voting")),
    ("专题", ("专题", "專題", "topic", "topics")),
    ("我的", ("我的", "我", "mine", "profile", "me")),
)
_MORE_PAY = ("更多支付", "更多方式", "其他支付", "更多")


def try_tangguo_flow_driver(work: ExplorationWork) -> ActionIntent | None:
    """主入口：有状态课纲；预算耗尽或 done 时返回 None。

    全产品统一采用：①限步 ②无状态辨识 ③约束输出；
    阶段仅由本控制器推进，Agent／Tool 不能自由规划整条路径。
    底栏语义图标＋相对位置。开屏／弹窗清障由 ``try_bounded_dismiss_tool`` 先行。
    """

    if not isinstance(work, ExplorationWork):
        return None
    package = _package_name(work)
    unified = getattr(work, "_app_three_contracts", False)
    if not unified and not package_uses_tangguo_flow(package_name=package):
        return None

    state = _load_state(work)
    # 墙钟用真实当前时间；不得用观察帧的 fixture／历史 observed_at。
    state.ensure_started_at()
    config = load_tangguo_flow_config(package)
    if config:
        apply_flow_config(state, config)
    profile_cfg = _profile_tangguo_flow(work)
    if profile_cfg:
        apply_flow_config(state, profile_cfg)
    if unified:
        # 配置只能收紧全局硬上限，不能关闭超时或改选其他支付渠道。
        state.budgets.device_actions_max = min(state.budgets.device_actions_max, 100)
        state.budgets.ai_calls_max = min(state.budgets.ai_calls_max, 100)
        state.budgets.wall_clock_timeout_sec = min(
            state.budgets.wall_clock_timeout_sec or 3600, 3600,
        )
        state.payment_method = "alipay"
        state.payment_mode = "inspect_only"
        state.allow_pending_order_creation = False
    if not unified and (config.get("enabled") is False or profile_cfg.get("enabled") is False):
        return None

    # 产品 playbook 覆盖合同（hjll 等）主导时，不抢确定性寻页／取样相位。
    # 糖果课纲仍服务无专属编排的通用 Agent 包（playbook 空或非产品课纲）。
    meta = work.frontier.observation.metadata
    if isinstance(meta, Mapping):
        from services.api.app.application.app_coverage_page_seek import (
            VISITED_BOTTOM_NAVIGATION_LABELS_KEY,
            VISITED_HOME_TOP_CATEGORY_TABS_KEY,
            home_top_category_sample_in_progress,
        )

        progress = meta.get("app_curriculum_progress")
        playbook = ""
        if isinstance(progress, Mapping):
            playbook = str(progress.get("playbook_id") or "").strip().casefold()
        if not unified and playbook in _PRODUCT_COVERAGE_PLAYBOOKS:
            _stash_state(work, state)
            return None
        if not unified and home_top_category_sample_in_progress(meta) and not isinstance(
            meta.get("tangguo_flow_state"), Mapping
        ):
            _stash_state(work, state)
            return None
        _hydrate_visited_from_coverage(
            state,
            visited_top=meta.get(VISITED_HOME_TOP_CATEGORY_TABS_KEY),
            visited_bottom=meta.get(VISITED_BOTTOM_NAVIGATION_LABELS_KEY),
        )

    object.__setattr__(work, "_app_flow_claimed", True)
    clock_now = getattr(work, "_app_flow_now", None)
    if state.budget_exhausted(now=clock_now) or state.phase == "done":
        if state.phase != "done":
            if state.wall_clock_exceeded(now=clock_now):
                state.stop_reason = state.stop_reason or "wall_clock_timeout"
            else:
                state.stop_reason = state.stop_reason or "budget_exhausted"
            state.advance("done")
        state.mark_stage("done", "blocked" if state.stop_reason else "passed")
        _stash_state(work, state)
        return None

    action: ActionIntent | None = None
    for _ in range(8):
        phase_before = state.phase
        if state.phase == "home_confirm":
            action = _phase_home_confirm(work, state)
        elif state.phase == "video_entry":
            action = _phase_video_entry(work, state)
        elif state.phase == "video_verify":
            action = _phase_video_verify(work, state)
        elif state.phase == "back_home":
            action = _phase_back_home(work, state)
        elif state.phase == "top_tabs":
            action = _phase_top_tabs(work, state)
        elif state.phase == "top_tab_restore":
            action = _phase_top_tab_restore(work, state)
        elif state.phase == "comic_entry":
            action = _phase_comic_entry(work, state)
        elif state.phase == "comic_detail":
            action = _phase_comic_detail(work, state)
        elif state.phase == "comic_reader":
            action = _phase_comic_reader(work, state)
        elif state.phase == "comic_scroll":
            action = _phase_comic_scroll(work, state)
        elif state.phase == "comic_back_home":
            action = _phase_comic_back_home(work, state)
        elif state.phase == "bottom_tabs":
            action = _phase_bottom_tabs(work, state)
        elif state.phase == "wallet_search":
            action = _phase_wallet_search(work, state)
        elif state.phase == "payment_alipay":
            action = _phase_payment_alipay(work, state)
        elif state.phase == "payment_back_home":
            action = _phase_payment_back_home(work, state)
        elif state.phase == "final_home":
            action = _phase_final_home(work, state)
        else:
            state.mark_stage("done", "blocked" if state.stop_reason else "passed")
            action = None
            break
        if action is not None:
            break
        if state.phase == "done":
            state.mark_stage("done", "blocked" if state.stop_reason else "passed")
            break
        # 阶段已前进：同轮继续推进（如 home→video）。
        # 同阶段无动作：交还下一帧，避免一轮空转烧完所有分頁／重试。
        if state.phase == phase_before:
            break

    _stash_state(work, state)
    return action


def try_agent_app_flow_driver(work: ExplorationWork):
    """生产入口：等待和终态也是显式决策，不向自由探索让出控制。"""
    from services.api.app.application.app_flow_decisions import flow_passive_decision

    object.__setattr__(work, "_app_flow_claimed", False)
    # 通用探索不等同于完整媒体课纲；仅显式启用或续跑本课纲时独占控制。
    meta = work.frontier.observation.metadata
    config = load_tangguo_flow_config(_package_name(work))
    profile = _profile_tangguo_flow(work)
    if not (
        isinstance(meta.get("tangguo_flow_state"), Mapping)
        or config.get("enabled") is True
        or profile.get("enabled") is True
    ):
        return None
    action = try_tangguo_flow_driver(work)
    if action is not None or not getattr(work, "_app_flow_claimed", False):
        return action
    return flow_passive_decision(work, read_tangguo_flow_state(work))


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------


def _phase_home_confirm(
    work: ExplorationWork, state: TangguoFlowState
) -> ActionIntent | None:
    page_type, page_state = _page(work)
    if page_type in _HOME_PAGES and page_state not in {
        "popup",
        "modal",
        "dialog",
        "startup_ad",
        "splash",
        "countdown",
    }:
        state.mark_stage("home", "passed")
        state.advance("video_entry")
        return None
    if not state.consume_ai_call():
        state.stop_reason = "budget_ai_calls_exhausted"
        state.advance("done")
        return None
    classified = recognize_page_state(work)
    if classified.status == "found" and classified.page == "home":
        state.mark_stage("home", "passed")
        state.advance("video_entry")
        return None
    if classified.status == "uncertain" and state.can_retry():
        state.consume_retry()
        return None
    state.mark_stage("home", "failed")
    state.stop_reason = classified.reason or "home_not_confirmed"
    state.advance("done")
    return None


def _phase_video_entry(
    work: ExplorationWork, state: TangguoFlowState
) -> ActionIntent | None:
    page_type, _ = _page(work)
    if page_type not in _HOME_PAGES:
        # 可能已点入：转验证
        state.advance("video_verify")
        return None
    if state.video_attempts >= state.budgets.video_candidates_max:
        state.mark_stage("home_video", "failed")
        state.video_outcome = "candidates_exhausted"
        state.advance("top_tabs")
        return None
    if not state.consume_ai_call():
        state.stop_reason = "budget_ai_calls_exhausted"
        state.advance("done")
        return None
    candidate = select_semantic_video_candidate(
        work,
        title=state.video_title,
        keywords=state.video_keywords,
        attempted_labels=state.attempted_video_labels,
    )
    if candidate is not None:
        if not state.consume_device_action():
            state.stop_reason = "budget_device_actions_exhausted"
            state.advance("done")
            return None
        label = str(candidate.semantic_target)
        state.video_attempts += 1
        state.attempted_video_labels.append(label)
        state.video_target = label
        state.advance("video_verify")
        candidate = replace(candidate, metadata={
            **candidate.metadata,
            "home_content_entry_seek": True,
            "tangguo_flow_phase": "video_entry",
        })
        return _build_intent(
            work, state, candidate=candidate, purpose=f"首页影片入口「{label}」点进取证",
        )
    located = recognize_video_entry(
        work,
        title=state.video_title,
        keywords=state.video_keywords,
        attempted_labels=state.attempted_video_labels,
    )
    if located.status == "found" and (located.coordinate is not None or located.candidate_id):
        label = located.label or located.target or "video"
        state.video_attempts += 1
        state.attempted_video_labels.append(str(label))
        state.video_target = str(label)
        state.advance("video_verify")
        extra = {
            "home_content_entry_seek": True,
            "tangguo_flow_phase": "video_entry",
            "require_screenshot_evidence": True,
        }
        if located.reason == "semantic_watch_or_first_card":
            extra["forced_pixel_home_content_entry"] = True
            extra["expected_state_hint"] = "home_content_entry_first_card"
        return _tap_intent(
            work,
            state,
            coordinate=located.coordinate, candidate_id=located.candidate_id,
            semantic_target=str(label),
            purpose=f"首页影片入口「{label}」点进取证",
            extra=extra,
        )
    if located.status == "uncertain" and state.can_retry():
        state.consume_retry()
        return None
    state.mark_stage("home_video", "failed")
    state.video_outcome = "not_found" if located.status == "not_found" else "uncertain"
    state.advance("top_tabs")
    return None


def _phase_video_verify(
    work: ExplorationWork, state: TangguoFlowState
) -> ActionIntent | None:
    page_type, _ = _page(work)
    if page_type in _HOME_PAGES and state.video_attempts > 0:
        # 点完仍在首页：尝试下一候选
        if state.video_attempts < state.budgets.video_candidates_max:
            state.advance("video_entry")
            return None
        state.mark_stage("home_video", "failed")
        state.video_outcome = "tap_no_navigation"
        state.advance("top_tabs")
        return None
    # 观察帧已标注详情／播放器：直接取证通过并返回，禁止 uncertain 空转截图。
    if page_type in _VIDEO_OK:
        state.video_outcome = (
            "entered_player" if page_type == "player" else "entered_detail"
        )
        state.playback_confirmed = (
            page_type == "player" and playback_progress_advanced(work)
        )
        if state.playback_confirmed:
            state.video_outcome = "playback_confirmed"
        state.mark_stage("home_video", "passed")
        state.advance("back_home")
        return None
    if not state.consume_ai_call():
        state.stop_reason = "budget_ai_calls_exhausted"
        state.advance("done")
        return None
    classified = recognize_page_state(work)
    if classified.page in _VIDEO_OK:
        state.video_outcome = (
            "entered_player" if classified.page == "player" else "entered_detail"
        )
        # 播放成功须可观测进度；仅播放器出现不算 playback_confirmed
        state.playback_confirmed = (
            classified.page == "player" and playback_progress_advanced(work)
        )
        if state.playback_confirmed:
            state.video_outcome = "playback_confirmed"
        state.mark_stage("home_video", "passed")
        state.advance("back_home")
        return None
    if classified.page == "login_gate":
        state.video_outcome = "blocked_login"
        state.mark_stage("home_video", "blocked")
        state.advance("back_home")
        return None
    if classified.page == "paywall":
        state.video_outcome = "blocked_paywall"
        state.mark_stage("home_video", "blocked")
        state.advance("back_home")
        return None
    if classified.page == "load_error":
        state.video_outcome = "load_failed"
        state.mark_stage("home_video", "failed")
        state.advance("back_home")
        return None
    if state.can_retry():
        state.consume_retry()
        return None
    # 未确认：返回后换候选或结束影片阶段
    if state.video_attempts < state.budgets.video_candidates_max:
        state.video_outcome = "uncertain_after_tap"
        return _back_intent(work, state, purpose="影片验证不确定，返回换候选")
    state.video_outcome = "uncertain_after_tap"
    state.mark_stage("home_video", "failed")
    state.advance("back_home")
    return None


def _phase_back_home(
    work: ExplorationWork, state: TangguoFlowState
) -> ActionIntent | None:
    page_type, _ = _page(work)
    if page_type in _HOME_PAGES:
        if state.consume_ai_call():
            classified = recognize_page_state(work)
            if classified.status == "found" and classified.page == "home":
                state.mark_stage("back_home", "passed")
                state.advance("top_tabs")
                return None
        else:
            state.mark_stage("back_home", "passed")
            state.advance("top_tabs")
            return None
        # page_type 已是 home 仍 uncertain：接受
        state.mark_stage("back_home", "passed")
        state.advance("top_tabs")
        return None
    if state.back_home_attempts >= state.budgets.back_home_max:
        state.mark_stage("back_home", "failed")
        state.stop_reason = "back_home_exhausted"
        if not state.record_recovery("back_home_exhausted"):
            state.advance("done")
            return None
        state.advance("done")
        return None
    state.back_home_attempts += 1
    state.record_recovery("back_home")
    return _back_intent(work, state, purpose="影片取证后返回首页并验证")


def _maybe_enter_comic(
    work: ExplorationWork,
    state: TangguoFlowState,
    *,
    note: str,
) -> ActionIntent | None:
    """发现漫画且本任务尚未跑过漫画子流程时进入；整任务最多一轮。"""
    if state.comic_already_terminal() or state.comic_flow_status == "in_progress":
        return None
    if not state.consume_ai_call():
        return None
    located = recognize_comic_entry(
        work,
        title=state.comic_configured_title,
        attempted_labels=state.attempted_comic_labels,
    )
    if located.status != "found" or (located.coordinate is None and not located.candidate_id):
        return None
    state.push_comic_checkpoint(note=note)
    state.comic_flow_status = "in_progress"
    state.comic_attempts = 1
    label = str(located.label or "漫画")
    state.attempted_comic_labels.append(label)
    state.comic_title = label
    state.advance("comic_detail")
    state.comic_device_actions_used += 1
    return _tap_intent(
        work,
        state,
        coordinate=located.coordinate, candidate_id=located.candidate_id,
        semantic_target=label,
        purpose=f"触发漫画子流程：点入「{label}」",
        extra={"tangguo_flow_phase": "comic_entry", "comic_entry_seek": True},
    )


def _phase_top_tabs(
    work: ExplorationWork, state: TangguoFlowState
) -> ActionIntent | None:
    """上方分类：影片后、底栏前；默认点 3 个未选中分类。"""
    page_type, _ = _page(work)
    if page_type not in _HOME_PAGES:
        # 尝试回首页再分类
        return _back_intent(work, state, purpose="上方分类前返回首页")

    if state.top_tab_device_actions_used >= state.budgets.top_tab_device_actions_max:
        state.top_tabs_status = "blocked"
        state.mark_stage("top_tabs", "blocked")
        state.advance("top_tab_restore")
        return None

    if not state.consume_ai_call():
        state.stop_reason = "budget_ai_calls_exhausted"
        state.advance("done")
        return None
    tabs = recognize_top_tabs(work)
    if not tabs:
        # 本帧完全辨识不到上方分类：跳过，禁止无证据合成横滑。
        # （通用 Agent 包在无顶栏 UI 的回归帧上横滑会劫持 MAF 底栏／宫格寻页。）
        state.top_tabs_status = "skipped"
        state.mark_stage("top_tabs", "skipped")
        state.advance("top_tab_restore")
        return None

    # 记录发现清单与默认选中
    for tab in tabs:
        if tab.label not in state.discovered_top_tabs:
            state.discovered_top_tabs.append(tab.label)
        if tab.selected and state.home_default_top_tab is None:
            state.home_default_top_tab = tab.label

    # 漫画分类可触发子流程
    comic_tab = next((t for t in tabs if t.is_comic), None)
    if comic_tab is not None and not state.comic_already_terminal():
        # 先点漫画分类再找入口，或直接找漫画入口
        entered = _maybe_enter_comic(work, state, note="from_top_tabs")
        if entered is not None:
            return entered
        if (
            (comic_tab.coordinate is not None or comic_tab.candidate_id)
            and comic_tab.label not in state.visited_top_tabs
            and state.top_tab_results.get(comic_tab.label, {}).get("tapped") is not True
        ):
            state.top_tab_results[comic_tab.label] = {
                **state.top_tab_results.get(comic_tab.label, {}),
                "tapped": True,
            }
            state.top_tab_device_actions_used += 1
            return _tap_intent(
                work,
                state,
                coordinate=comic_tab.coordinate, candidate_id=comic_tab.candidate_id,
                semantic_target=comic_tab.label,
                purpose=f"上方分类点选漫画栏「{comic_tab.label}」",
                extra={
                    "home_top_category_tab_seek": True,
                    "tangguo_flow_phase": "top_tabs",
                },
            )

    visited_count = len(
        [t for t in state.visited_top_tabs if t != state.home_default_top_tab]
    )
    limit = state.budgets.top_tab_visit_limit
    unselected = [
        t
        for t in tabs
        if not t.selected
        and t.label not in state.visited_top_tabs
        and t.label != state.home_default_top_tab
    ]
    # 刚点过的：验证选中
    for tab in tabs:
        last = state.top_tab_results.get(tab.label, {})
        if last.get("tapped") is True and tab.label not in state.visited_top_tabs:
            if tab.selected or last.get("awaiting_verify") is True:
                # 内容加载：用 page_state／候选非空粗判
                page_state = str(work.frontier.observation.page_state or "").casefold()
                content_ok = content_ready(page_state)
                status = "passed" if (tab.selected and content_ok) else "failed"
                if (not tab.selected or not content_ok) and state.can_retry():
                    state.consume_retry()
                    return None
                state.visited_top_tabs.append(tab.label)
                state.top_tab_results[tab.label] = {
                    "status": status,
                    "selected": tab.selected,
                    "content_ok": content_ok,
                }
                return None

    if visited_count >= limit or not unselected:
        # 尝试横滑找更多
        if (
            visited_count < limit
            and state.top_tab_scrolls < state.budgets.top_tab_scroll_max
        ):
            state.top_tab_scrolls += 1
            state.top_tab_device_actions_used += 1
            return _horizontal_scroll_intent(
                work,
                state,
                purpose="上方分类列横向滑动以发现更多未选中分类",
                direction="left",
            )
        outcomes = [state.top_tab_results.get(label, {}).get("status")
                    for label in state.visited_top_tabs]
        state.top_tabs_status = (
            "skipped" if not outcomes else
            "passed" if all(value == "passed" for value in outcomes) else "failed"
        )
        state.mark_stage("top_tabs", state.top_tabs_status)
        state.advance("top_tab_restore")
        return None

    target = unselected[0]
    if target.coordinate is None and not target.candidate_id:
        state.visited_top_tabs.append(target.label)
        state.top_tab_results[target.label] = {
            "status": "failed",
            "reason": "missing_coordinate",
        }
        return None
    state.top_tab_results[target.label] = {
        "tapped": True,
        "awaiting_verify": True,
        "label": target.label,
        "tab_id": target.tab_id,
    }
    state.top_tab_device_actions_used += 1
    return _tap_intent(
        work,
        state,
        coordinate=target.coordinate, candidate_id=target.candidate_id,
        semantic_target=target.label,
        purpose=f"上方分类点选「{target.label}」并验证选中与内容",
        extra={
            "home_top_category_tab_seek": True,
            "tangguo_flow_phase": "top_tabs",
            "require_screenshot_evidence": True,
        },
    )


def _phase_top_tab_restore(
    work: ExplorationWork, state: TangguoFlowState
) -> ActionIntent | None:
    """恢复首页原本分类后再进底栏。"""
    page_type, _ = _page(work)
    if page_type not in _HOME_PAGES:
        return _back_intent(work, state, purpose="恢复原分类前返回首页")
    default = state.home_default_top_tab
    if not default:
        state.mark_stage("top_tab_restore", "skipped")
        state.advance("bottom_tabs")
        return None
    if not state.consume_ai_call():
        state.stop_reason = "budget_ai_calls_exhausted"
        state.advance("done")
        return None
    tabs = recognize_top_tabs(work)
    current = next((t for t in tabs if t.label == default), None)
    if current is not None and current.selected:
        state.mark_stage("top_tab_restore", "passed")
        state.advance("bottom_tabs")
        return None
    if current is not None and (current.coordinate is not None or current.candidate_id):
        state.top_tab_device_actions_used += 1
        return _tap_intent(
            work,
            state,
            coordinate=current.coordinate, candidate_id=current.candidate_id,
            semantic_target=default,
            purpose=f"恢复首页原分类「{default}」",
            extra={
                "home_top_category_tab_seek": True,
                "tangguo_flow_phase": "top_tab_restore",
            },
        )
    # 找不到原分类：记录失败仍继续底栏
    state.mark_stage("top_tab_restore", "failed")
    state.advance("bottom_tabs")
    return None


def _phase_comic_entry(
    work: ExplorationWork, state: TangguoFlowState
) -> ActionIntent | None:
    if state.comic_already_terminal():
        state.resume_after_comic()
        return None
    if state.comic_attempts >= state.budgets.comic_candidates_max:
        state.comic_flow_status = "failed"
        state.comic_outcome = "candidates_exhausted"
        state.mark_stage("comic", "failed")
        state.advance("comic_back_home")
        return None
    if not state.consume_ai_call():
        state.stop_reason = "budget_ai_calls_exhausted"
        state.advance("done")
        return None
    located = recognize_comic_entry(
        work,
        title=state.comic_configured_title,
        attempted_labels=state.attempted_comic_labels,
    )
    if located.status == "found" and (located.coordinate is not None or located.candidate_id):
        label = str(located.label or "漫画")
        state.comic_attempts += 1
        state.attempted_comic_labels.append(label)
        state.comic_title = label
        state.comic_flow_status = "in_progress"
        state.advance("comic_detail")
        state.comic_device_actions_used += 1
        return _tap_intent(
            work,
            state,
            coordinate=located.coordinate, candidate_id=located.candidate_id,
            semantic_target=label,
            purpose=f"选择漫画「{label}」进入",
            extra={"comic_entry_seek": True, "tangguo_flow_phase": "comic_entry"},
        )
    if located.status == "uncertain" and state.can_retry():
        state.consume_retry()
        return None
    state.comic_flow_status = "skipped"
    state.comic_outcome = "not_found"
    state.mark_stage("comic", "skipped")
    state.resume_after_comic()
    return None


def _phase_comic_detail(
    work: ExplorationWork, state: TangguoFlowState
) -> ActionIntent | None:
    if not state.consume_ai_call():
        state.stop_reason = "budget_ai_calls_exhausted"
        state.advance("done")
        return None
    classified = recognize_page_state(work)
    if classified.page == "login_gate":
        state.comic_flow_status = "blocked"
        state.comic_outcome = "blocked_login"
        state.mark_stage("comic", "blocked")
        state.advance("comic_back_home")
        return None
    if classified.page == "paywall":
        state.comic_flow_status = "blocked"
        state.comic_outcome = "blocked_paywall"
        state.mark_stage("comic", "blocked")
        state.advance("comic_back_home")
        return None
    if classified.page == "comic_reader":
        state.advance("comic_scroll")
        return None
    if classified.page == "comic_detail" or is_comic_detail_not_reader(work):
        start = recognize_start_read(work)
        if start.status == "found" and (start.coordinate is not None or start.candidate_id):
            state.advance("comic_reader")
            state.comic_device_actions_used += 1
            return _tap_intent(
                work,
                state,
                coordinate=start.coordinate, candidate_id=start.candidate_id,
                semantic_target=start.label or "开始阅读",
                purpose="漫画详情页点击开始／继续阅读（不购买）",
                extra={"tangguo_flow_phase": "comic_detail"},
            )
        if state.can_retry():
            state.consume_retry()
            return None
        # 详情页滑不算阅读成功
        state.comic_flow_status = "failed"
        state.comic_outcome = "detail_without_reader"
        state.mark_stage("comic", "failed")
        state.advance("comic_back_home")
        return None
    if classified.page == "load_error":
        state.comic_flow_status = "failed"
        state.comic_outcome = "load_failed"
        state.mark_stage("comic", "failed")
        state.advance("comic_back_home")
        return None
    if state.can_retry():
        state.consume_retry()
        return None
    if state.comic_attempts < state.budgets.comic_candidates_max:
        state.advance("comic_entry")
        return _back_intent(work, state, purpose="漫画验证失败，返回换候选")
    state.comic_flow_status = "failed"
    state.comic_outcome = "uncertain"
    state.mark_stage("comic", "failed")
    state.advance("comic_back_home")
    return None


def _phase_comic_reader(
    work: ExplorationWork, state: TangguoFlowState
) -> ActionIntent | None:
    """确认真正进入阅读页（详情介绍不算）。"""
    if not state.consume_ai_call():
        state.stop_reason = "budget_ai_calls_exhausted"
        state.advance("done")
        return None
    classified = recognize_page_state(work)
    if classified.page == "comic_reader":
        state.advance("comic_scroll")
        return None
    if classified.page == "comic_detail":
        # 仍在详情：勿把详情滑动当阅读
        state.comic_outcome = "still_on_detail"
        return _phase_comic_detail(work, state)
    if classified.page == "paywall":
        state.comic_flow_status = "blocked"
        state.comic_outcome = "blocked_paywall"
        state.mark_stage("comic", "blocked")
        state.advance("comic_back_home")
        return None
    if state.can_retry():
        state.consume_retry()
        return None
    state.comic_flow_status = "failed"
    state.comic_outcome = "reader_not_confirmed"
    state.mark_stage("comic", "failed")
    state.advance("comic_back_home")
    return None


def _phase_comic_scroll(
    work: ExplorationWork, state: TangguoFlowState
) -> ActionIntent | None:
    """向下阅读：手指由下往上滑；记录执行／有效次数。"""
    if not state.consume_ai_call():
        state.stop_reason = "budget_ai_calls_exhausted"
        state.advance("done")
        return None
    classified = recognize_page_state(work)
    if classified.page == "comic_detail":
        state.comic_flow_status = "failed"
        state.comic_outcome = "scrolled_on_detail_invalid"
        state.mark_stage("comic", "failed")
        state.advance("comic_back_home")
        return None
    if classified.page == "paywall":
        state.comic_flow_status = "blocked"
        state.comic_outcome = "blocked_paywall"
        state.mark_stage("comic", "blocked")
        state.advance("comic_back_home")
        return None
    # 仅横翻：模式不符
    meta = work.frontier.observation.metadata
    if isinstance(meta, Mapping):
        mode = str(meta.get("comic_read_mode") or "").casefold()
        if mode in {"horizontal", "ltr", "rtl", "page_flip"}:
            state.comic_mode_mismatch = True
            state.comic_flow_status = "blocked"
            state.comic_outcome = "mode_horizontal_only"
            state.mark_stage("comic", "blocked")
            state.advance("comic_back_home")
            return None
        if meta.get("comic_chapter_ended") is True:
            state.comic_hit_end = True
            state.comic_flow_status = "passed"
            state.comic_outcome = "hit_chapter_end"
            state.mark_stage("comic", "passed")
            state.advance("comic_back_home")
            return None

    if classified.page != "comic_reader" or not content_ready(_page(work)[1]):
        if state.can_retry():
            state.consume_retry()
            return None
        state.comic_flow_status = "blocked"
        state.comic_outcome = "reader_not_ready"
        state.mark_stage("comic", "blocked")
        state.advance("comic_back_home")
        return None

    from services.api.app.application.app_visual_content_fingerprint import (
        content_fingerprint_value,
    )

    fingerprint = content_fingerprint_value(meta.get("surface_facts"))
    if state.comic_swipes_done > state.comic_swipes_verified:
        # 必须有动作后的新截图；同一截图或无证据不能算滑动成功。
        fresh = bool(state.comic_scroll_source_capture and
                     work.source_capture_id != state.comic_scroll_source_capture)
        changed = meta.get("comic_scroll_content_changed") if fresh else None
        if fresh and changed is None and fingerprint and state.comic_scroll_source_fingerprint:
            changed = fingerprint != state.comic_scroll_source_fingerprint
        if changed is True:
            state.comic_swipes_verified += 1
            state.comic_swipes_effective += 1
        else:
            if state.can_retry():
                state.consume_retry()
                return None
            state.comic_flow_status = "blocked"
            state.comic_outcome = "scroll_not_confirmed"
            state.mark_stage("comic", "blocked")
            state.advance("comic_back_home")
            return None

    planned = state.budgets.comic_read_swipes
    if state.comic_swipes_done >= planned:
        state.comic_flow_status = "passed"
        state.comic_outcome = "read_swipes_verified"
        state.mark_stage("comic", "passed")
        state.advance("comic_back_home")
        return None

    state.comic_scroll_source_capture = work.source_capture_id
    state.comic_scroll_source_fingerprint = fingerprint
    state.comic_swipes_done += 1
    state.comic_device_actions_used += 1
    ratio = state.budgets.comic_swipe_ratio
    return _comic_read_scroll_intent(
        work,
        state,
        purpose=(
            f"漫画阅读向下滑第 {state.comic_swipes_done}/{planned} 次"
            f"（手指由下往上，约阅读区 {int(ratio * 100)}%）"
        ),
        ratio=ratio,
    )


def _phase_comic_back_home(
    work: ExplorationWork, state: TangguoFlowState
) -> ActionIntent | None:
    page_type, _ = _page(work)
    if page_type in _HOME_PAGES:
        if state.comic_flow_status == "in_progress":
            state.comic_flow_status = "passed"
            state.mark_stage("comic", "passed")
        # 终态已标记，回首页后接续，不重置 comic
        state.resume_after_comic()
        return None
    if state.comic_back_home_attempts >= state.budgets.comic_back_home_max:
        state.record_recovery("comic_back_home_exhausted")
        # 未恢复首页不能在阅读页继续验证底栏，保留剩余清单并明确阻塞。
        if state.comic_flow_status == "in_progress":
            state.comic_flow_status = "failed"
            state.comic_outcome = (state.comic_outcome or "") + "|back_home_failed"
            state.mark_stage("comic", "failed")
        state.mark_stage("comic_back_home", "blocked")
        state.stop_reason = "comic_back_home_exhausted"
        state.advance("done")
        return None
    state.comic_back_home_attempts += 1
    state.comic_device_actions_used += 1
    return _back_intent(work, state, purpose="漫画子流程结束后返回 APP 首页")


def _phase_bottom_tabs(
    work: ExplorationWork, state: TangguoFlowState
) -> ActionIntent | None:
    """底栏：完整清单全部实际点击（含首页），非抽样。"""
    tabs = _ordered_tabs(work)
    if not tabs and not state.discovered_bottom_tabs:
        if state.can_retry():
            state.consume_retry()
            return None
        state.mark_stage("bottom_tabs", "blocked")
        state.advance("final_home")
        return None
    if tabs and not state.discovered_bottom_tabs:
        state.discovered_bottom_tabs = [label for label, _ in tabs]
        state.ensure_bottom_tab_budget()
    elif tabs:
        for label, _ in tabs:
            if label not in state.discovered_bottom_tabs:
                state.discovered_bottom_tabs.append(label)
        state.ensure_bottom_tab_budget()

    state.sync_pending_bottom_tabs()

    if state.tab_device_actions_used >= state.budgets.tab_device_actions_max:
        state.compute_bottom_coverage()
        state.finalize_bottom_tabs_stage()
        state.stop_reason = state.stop_reason or "budget_bottom_tabs_exhausted"
        state.advance("final_home")
        return None

    # 底栏发现漫画：条件触发（不取代本轮正式检查）
    comic_action = _maybe_enter_comic(work, state, note="from_bottom_tabs")
    if comic_action is not None:
        return comic_action

    if not state.pending_bottom_tabs:
        state.finalize_bottom_tabs_stage()
        state.advance("final_home")
        return None

    label = state.pending_bottom_tabs[0]
    aliases = next((a for lab, a in tabs if lab == label), (label,))
    last = state.tab_results.get(label, {})

    # 已点击后验证选中／目的页／内容（即使原先已选中也必须先点过）
    if last.get("clicked") is True and label not in state.visited_bottom_tabs:
        return _verify_bottom_tab_after_click(work, state, label=label)

    if not state.consume_ai_call():
        state.stop_reason = "budget_ai_calls_exhausted"
        state.compute_bottom_coverage()
        state.finalize_bottom_tabs_stage()
        state.advance("final_home")
        return None

    located = recognize_bottom_tab(work, wanted_label=label, aliases=aliases)
    if located.status == "found" and (located.coordinate is not None or located.candidate_id):
        # 即使当前已选中也必须实际点击一次
        state.tab_results[label] = {
            **last,
            "clicked": True,
            "awaiting_verify": True,
            "label": label,
        }
        state.tab_device_actions_used += 1
        return _tap_intent(
            work,
            state,
            coordinate=located.coordinate, candidate_id=located.candidate_id,
            semantic_target=label,
            purpose=(
                f"底部分页「{label}」必须实际点击并验证"
                "（含已选中刷新；不跳过首页按钮）"
            ),
            extra={
                "bottom_navigation_tab_seek": True,
                "tangguo_flow_phase": "bottom_tabs",
                "tangguo_flow_tab": label,
                "require_screenshot_evidence": True,
            },
        )
    if located.status == "uncertain" and state.can_retry():
        state.consume_retry()
        return None

    # 停用／无法点击
    if located.status == "not_found":
        state.blocked_bottom_tabs.append(label)
        state.tab_results[label] = {
            "status": "blocked",
            "clicked": False,
            "reason": "tab_unavailable",
        }
    else:
        state.failed_bottom_tabs.append(label)
        state.tab_results[label] = {
            "status": "failed",
            "clicked": False,
            "reason": located.reason or located.status,
        }
    state.record_recovery(f"tab_failed:{label}")
    state.sync_pending_bottom_tabs()
    return None


def _verify_bottom_tab_after_click(
    work: ExplorationWork,
    state: TangguoFlowState,
    *,
    label: str,
) -> ActionIntent | None:
    """点击后验证选中／目的页／内容；首页不重跑影片／上方分类。"""
    page_type, page_state = _page(work)
    on_target = tab_destination_confirmed(work, label)
    content_ok = content_ready(page_state)
    empty_ok = page_state in {"empty", "empty_state"}
    if on_target and (content_ok or empty_ok):
        status = "passed"
        state.visited_bottom_tabs.append(label)
        state.tab_results[label] = {
            "status": status,
            "clicked": True,
            "selected_or_destination": True,
            "page_type": page_type,
            "content": "empty" if empty_ok else "loaded",
            # 首页内容链路已完成时不重跑
            "skip_home_content_rerun": tab_role(label) == "home",
        }
        state.sync_pending_bottom_tabs()
        # 「我的」：正式检查完成后做钱包搜寻；支付后继续剩余分頁
        if tab_role(label) == "profile" and not state.wallet_search_done:
            state.push_payment_checkpoint(note="wallet_from_mine_tab")
            state.advance("wallet_search")
            return None
        return None

    if state.can_retry():
        state.consume_retry()
        return None
    state.failed_bottom_tabs.append(label)
    state.tab_results[label] = {
        "status": "failed",
        "clicked": True,
        "selected_or_destination": on_target,
        "page_type": page_type,
        "reason": "verify_failed",
    }
    state.record_recovery(f"tab_verify_failed:{label}")
    state.sync_pending_bottom_tabs()
    return None


def _phase_wallet_search(
    work: ExplorationWork, state: TangguoFlowState
) -> ActionIntent | None:
    page_type, _ = _page(work)
    if page_type not in _PROFILE_PAGES:
        # 应已在「我的」页；尝试点「我的」再搜（不记为正式底栏检查替代）
        if not state.consume_ai_call():
            state.stop_reason = "budget_ai_calls_exhausted"
            state.resume_after_payment()
            return None
        located = recognize_bottom_tab(
            work,
            wanted_label="我的",
            aliases=("我的", "我", "mine", "profile", "me"),
        )
        if located.status == "found" and (located.coordinate is not None or located.candidate_id):
            return _tap_intent(
                work,
                state,
                coordinate=located.coordinate, candidate_id=located.candidate_id,
                semantic_target="我的",
                purpose="支付入口搜寻前进入我的页（恢复，不替代正式底栏检查）",
                extra={
                    "bottom_navigation_tab_seek": True,
                    "tangguo_flow_phase": "wallet_search",
                    "wallet_nav_recovery": True,
                },
            )
        state.wallet_search_done = True
        state.wallet_search_result = "profile_unavailable"
        state.mark_stage("mine_wallet", "blocked")
        state.resume_after_payment()
        return None

    if not state.consume_ai_call():
        state.stop_reason = "budget_ai_calls_exhausted"
        state.wallet_search_done = True
        state.resume_after_payment()
        return None
    located = recognize_wallet_entry(work)
    if located.status == "found" and (located.coordinate is not None or located.candidate_id):
        label = str(located.label or located.target or "")
        if any(m in label for m in ("提现", "提現", "交易记录", "交易紀錄")):
            state.wallet_search_result = "found_non_recharge"
            state.wallet_search_done = True
            state.mark_stage("mine_wallet", "blocked")
            state.resume_after_payment()
            return None
        state.wallet_search_result = "found"
        state.wallet_entry_label = label
        state.mark_stage("mine_wallet", "passed")
        state.advance("payment_alipay")
        return _tap_intent(
            work,
            state,
            coordinate=located.coordinate, candidate_id=located.candidate_id,
            semantic_target=label or "钱包",
            purpose=f"点击钱包／充值入口「{label}」进入支付链路（inspect_only）",
            extra={
                "tangguo_flow_phase": "wallet_search",
                "wallet_entry_target": located.target,
            },
        )
    if state.profile_scrolls < state.budgets.profile_scroll_max:
        state.profile_scrolls += 1
        state.record_recovery("wallet_scroll")
        return _scroll_intent(
            work,
            state,
            purpose="我的页向下捲动搜寻钱包／充值入口",
        )
    state.wallet_search_result = "not_found_in_scope"
    state.wallet_search_done = True
    state.mark_stage("mine_wallet", "skipped")
    # 正常结束支付分支，继续未完成底栏
    state.resume_after_payment()
    return None


def _phase_payment_alipay(
    work: ExplorationWork, state: TangguoFlowState
) -> ActionIntent | None:
    if state.payment_method != "alipay":
        return _block_payment(state, "blocked_non_alipay_method")
    if state.payment_device_actions_used >= state.budgets.payment_device_actions_max:
        return _block_payment(state, "budget_payment_device_exhausted")
    if state.payment_ai_calls_used >= state.budgets.payment_ai_calls_max:
        return _block_payment(state, "budget_payment_ai_exhausted")

    if not state.consume_ai_call():
        state.stop_reason = "budget_ai_calls_exhausted"
        state.wallet_search_done = True
        state.resume_after_payment()
        return None
    state.payment_ai_calls_used += 1
    classified = recognize_page_state(work)

    if classified.page == "login_gate":
        return _block_payment(state, "blocked_login")

    if classified.page == "alipay_cashier":
        # 跳转成功 ≠ 付款成功
        state.payment_farthest_stage = "alipay_cashier"
        state.alipay_available = True
        state.mark_stage("payment_alipay", "passed")
        state.wallet_search_done = True
        state.resume_after_payment()
        return None

    if classified.page == "order_confirm":
        # 独立订单确认页且支付宝已验证选中才可收口；否则继续下方渠道语意相对选点。
        if (
            state.payment_mode == "inspect_only"
            and not state.allow_pending_order_creation
            and state.alipay_selected
        ):
            state.payment_farthest_stage = "order_confirm"
            # 停在确认前，不点最终支付／不建单
            state.mark_stage("payment_alipay", "passed")
            state.wallet_search_done = True
            state.resume_after_payment()
            return None

    # 钱包、充值方案与渠道是不同页面，不能在尚未到达渠道页时判渠道缺失。
    from services.api.app.application.app_flow_payment_navigation import payment_navigation

    payment_config = {
        **load_tangguo_flow_config(_package_name(work)), **_profile_tangguo_flow(work)
    }
    candidate, blocked = payment_navigation(work, state, payment_config)
    if blocked:
        return _block_payment(state, blocked)
    if candidate is not None:
        from services.api.app.application.app_constrained_locate import (
            center_from_normalized_bounds,
        )

        center = center_from_normalized_bounds(candidate.metadata.get("normalized_bounds"))
        state.payment_navigation_target = candidate.semantic_target
        state.payment_device_actions_used += 1
        return _tap_intent(
            work, state, coordinate=center, candidate_id=candidate.candidate_id,
            semantic_target=candidate.semantic_target,
            purpose="进入充值页或选择已配置的测试方案，不提交订单",
            extra={"tangguo_flow_phase": "payment_alipay"},
        )

    # 缺测试方案配置时：可继续选支付宝验证，但不选金额建单
    cfg = load_tangguo_flow_config(_package_name(work))
    profile_cfg = _profile_tangguo_flow(work)
    plan = (
        cfg.get("test_payment_plan")
        or cfg.get("test_amount")
        or profile_cfg.get("test_payment_plan")
        or profile_cfg.get("test_amount")
    )
    if (
        not plan
        and state.payment_mode != "inspect_only"
        and classified.page in {"recharge_page", "paywall"}
    ):
        return _block_payment(state, "blocked_missing_payment_config")

    if not state.consume_ai_call():
        state.stop_reason = "budget_ai_calls_exhausted"
        state.wallet_search_done = True
        state.resume_after_payment()
        return None
    state.payment_ai_calls_used += 1

    # 若已点过支付宝：验证选中
    if state.payment_farthest_stage == "alipay_tap":
        selected = recognize_alipay(work, require_selected=True)
        if selected.status == "found":
            state.alipay_available = True
            state.alipay_selected = True
            state.payment_farthest_stage = "alipay_selected_confirmed"
            return _finish_after_alipay_selected(state)
        if selected.status == "uncertain":
            if state.can_retry():
                state.consume_retry()
                return None
            return _block_payment(state, "blocked_alipay_unconfirmed")
        return _block_payment(state, "blocked_alipay_unconfirmed")

    alipay = recognize_alipay(work, require_selected=False)
    if alipay.status == "not_found":
        more = _locate_more_payments(work)
        if more is not None and (more.coordinate is not None or more.candidate_id):
            state.payment_device_actions_used += 1
            state.payment_farthest_stage = "expand_more_payments"
            return _tap_intent(
                work,
                state,
                coordinate=more.coordinate, candidate_id=more.candidate_id,
                semantic_target=more.label or "更多支付方式",
                purpose="展开更多支付方式以寻找支付宝",
                extra={"tangguo_flow_phase": "payment_alipay", "expand_more_pay": True},
            )
        state.alipay_available = False
        return _block_payment(state, "blocked_alipay_unavailable")

    if alipay.status == "uncertain":
        if alipay.reason == "alipay_may_be_under_more":
            more = _locate_more_payments(work)
            if more is not None and (more.coordinate is not None or more.candidate_id):
                state.payment_device_actions_used += 1
                return _tap_intent(
                    work,
                    state,
                    coordinate=more.coordinate, candidate_id=more.candidate_id,
                    semantic_target=more.label or "更多支付方式",
                    purpose="支付宝可能在更多选项内，展开寻找",
                    extra={
                        "tangguo_flow_phase": "payment_alipay",
                        "expand_more_pay": True,
                    },
                )
        if state.can_retry():
            state.consume_retry()
            return None
        return _block_payment(state, "blocked_alipay_unconfirmed")

    # found：若已显示选中则完成；否则点击后必须再验证
    already = alipay.reason == "selected"
    state.alipay_available = True
    if already:
        state.alipay_selected = True
        state.payment_farthest_stage = "alipay_selected_confirmed"
        return _finish_after_alipay_selected(state)

    if alipay.coordinate is None and not alipay.candidate_id:
        return _block_payment(state, "blocked_alipay_unconfirmed")

    # 默认其他渠道时切换到支付宝
    state.payment_farthest_stage = "alipay_tap"
    state.payment_device_actions_used += 1
    return _tap_intent(
        work,
        state,
        coordinate=alipay.coordinate, candidate_id=alipay.candidate_id,
        semantic_target=alipay.label or "支付宝",
        purpose="支付方式一律选择支付宝并验证选中（inspect_only，不付款）",
        extra={
            "tangguo_flow_phase": "payment_alipay",
            "payment_method": "alipay",
            "inspect_only": state.payment_mode == "inspect_only",
        },
    )


def _finish_after_alipay_selected(state: TangguoFlowState) -> None:
    """支付宝已确认选中：inspect_only 下停止，不点可能扣款的继续；再继续底栏。"""
    state.mark_stage("payment_alipay", "passed")
    state.wallet_search_done = True
    # inspect_only 通过仅表示检查范围完成，不代表付款成功
    state.resume_after_payment()
    return None


def _block_payment(state: TangguoFlowState, code: str) -> None:
    state.payment_blocked_reason = code
    state.alipay_selected = (
        False
        if "unconfirmed" in code or "unavailable" in code
        else state.alipay_selected
    )
    state.mark_stage("payment_alipay", "blocked")
    state.wallet_search_done = True
    state.resume_after_payment()
    return None


def _phase_payment_back_home(work, state) -> ActionIntent | None:
    """支付分支退出后先恢复首页，不在收银台上判底栏缺失。"""
    page_type, page_state = _page(work)
    if page_type in _HOME_PAGES and content_ready(page_state):
        state.mark_stage("payment_back_home", "passed")
        state.complete_payment_return()
        return None
    if state.payment_back_attempts >= state.budgets.final_home_back_max:
        state.mark_stage("payment_back_home", "failed")
        state.stop_reason = "payment_back_home_exhausted"
        state.compute_bottom_coverage()
        state.advance("done")
        return None
    state.payment_back_attempts += 1
    return _back_intent(work, state, purpose="退出支付分支并返回原 APP 首页")


def _phase_final_home(
    work: ExplorationWork, state: TangguoFlowState
) -> ActionIntent | None:
    """全部链路结束后返回首页并产出报告。"""
    page_type, page_state = _page(work)
    if page_type in _HOME_PAGES:
        if not content_ready(page_state):
            return None
        state.mark_stage("final_home", "passed")
        state.compute_bottom_coverage()
        # 漫画为条件链路；完成栏位探索仍未发现入口时正常跳过，不算失败。
        # 单帧没找到不能提前封死，后续上方／底部栏目仍可能发现漫画。
        if (state.comic_flow_status == "not_started"
                and state.bottom_coverage_status == "complete"):
            state.comic_flow_status = "skipped"
            state.comic_outcome = "not_found_in_explored_scope"
            state.mark_stage("comic", "skipped")
        state.advance("done")
        return None
    if state.final_home_attempts >= state.budgets.final_home_back_max:
        state.mark_stage("final_home", "failed")
        state.compute_bottom_coverage()
        state.advance("done")
        return None
    # 优先点底部首页（重新定位），否则 BACK
    if state.consume_ai_call():
        located = recognize_bottom_tab(
            work,
            wanted_label="首页",
            aliases=("首页", "首頁", "主页", "主頁", "home"),
        )
        if located.status == "found" and (located.coordinate is not None or located.candidate_id):
            state.final_home_attempts += 1
            return _tap_intent(
                work,
                state,
                coordinate=located.coordinate, candidate_id=located.candidate_id,
                semantic_target="首页",
                purpose="全部完成后点击底栏首页返回并验证",
                extra={
                    "bottom_navigation_tab_seek": True,
                    "tangguo_flow_phase": "final_home",
                },
            )
    state.final_home_attempts += 1
    return _back_intent(work, state, purpose="全部完成后返回 APP 首页")


# ---------------------------------------------------------------------------
# Pure helpers (tests)
# ---------------------------------------------------------------------------


def apply_alipay_selection_verification(
    state: TangguoFlowState,
    result: ConstrainedLocateResult,
) -> str:
    if result.status == "not_found":
        state.alipay_available = False
        state.payment_blocked_reason = "blocked_alipay_unavailable"
        return "blocked_alipay_unavailable"
    if result.status == "uncertain":
        state.payment_blocked_reason = "blocked_alipay_unconfirmed"
        return "blocked_alipay_unconfirmed"
    state.alipay_available = True
    if result.reason == "selected":
        state.alipay_selected = True
        state.payment_farthest_stage = "alipay_selected_confirmed"
        return "selected_confirmed"
    state.payment_farthest_stage = "alipay_found"
    return "found_not_confirmed"


def inspect_only_blocks_payment(
    *,
    payment_mode: str,
    allow_pending_order_creation: bool,
    action_kind: str,
) -> bool:
    if payment_mode != "inspect_only":
        return False
    risky = action_kind in {
        "confirm_pay",
        "pay",
        "submit_order",
        "password_free_pay",
        "bind_card",
        "withdraw",
    }
    if risky:
        return True
    if action_kind in {"create_order", "continue_checkout"} and not allow_pending_order_creation:
        return True
    return False


def alipay_jump_is_not_payment_success(farthest_stage: str | None) -> bool:
    """直接跳转支付宝收银台不得误判付款成功。"""
    return farthest_stage in {"alipay_cashier", "alipay_external_app", "alipay_webview"}


# ---------------------------------------------------------------------------
# Intent builders
# ---------------------------------------------------------------------------


# 仍走产品覆盖合同／取样相位的 playbook；Agent 三合同课纲对其让路。
_PRODUCT_COVERAGE_PLAYBOOKS = frozenset(
    {
        "hjll",
        "hlapp",
        "hlwapp",
        "hlwwhite",
        "51pc",
        "xingba",
        "jmcomic",
        "mama",
        "jinguo",
        "pzhan",
        "yms",
        "head",
        "latest",
    }
)


def _hydrate_visited_from_coverage(
    state: TangguoFlowState,
    *,
    visited_top: object,
    visited_bottom: object,
) -> None:
    """把覆盖合同已访顶栏／底栏并入主流程状态，避免重复点已完成栏目。"""

    # 已访问标签没有本轮动作与验证证据，不得跳过正式检查。
    return None


def _prefer_frontier_seek_candidate(
    work: ExplorationWork,
    *,
    semantic_target: str,
    coordinate: tuple[float, float],
    extra: Mapping[str, Any] | None,
) -> CandidateAction | None:
    """优先复用当前帧已提升的寻页候选，避免合成 id 与覆盖合同脱节。"""

    flags = {
        key
        for key in (
            "home_top_category_tab_seek",
            "bottom_navigation_tab_seek",
            "home_category_grid_seek",
            "home_content_entry_seek",
        )
        if isinstance(extra, Mapping) and extra.get(key) is True
    }
    if not flags:
        return None
    label = str(semantic_target or "").strip()
    x, y = coordinate
    best: CandidateAction | None = None
    best_dist = 1e9
    for item in work.frontier.candidates:
        if item.action_type not in {
            InteractionOperation.CLICK,
            InteractionOperation.TAP,
        }:
            continue
        if not any(item.metadata.get(flag) is True for flag in flags):
            continue
        target = str(item.semantic_target or "")
        if label and label not in target and target not in label:
            continue
        raw = item.metadata.get("normalized_bounds")
        if not isinstance(raw, Mapping):
            continue
        try:
            left = float(raw["left"])
            top = float(raw["top"])
            right = float(raw["right"])
            bottom = float(raw["bottom"])
        except (KeyError, TypeError, ValueError):
            continue
        cx = (left + right) / 2.0
        cy = (top + bottom) / 2.0
        dist = abs(cx - x) + abs(cy - y)
        if dist < best_dist:
            best_dist = dist
            best = item
    if best is None or best_dist > 0.35:
        return None
    return best


def _tap_intent(
    work: ExplorationWork,
    state: TangguoFlowState,
    *,
    coordinate: tuple[float, float] | None,
    candidate_id: str | None = None,
    semantic_target: str,
    purpose: str,
    extra: Mapping[str, Any] | None = None,
) -> ActionIntent | None:
    preferred = next((item for item in work.frontier.candidates
                      if item.candidate_id == candidate_id), None) if candidate_id else None
    if candidate_id and (preferred is None or preferred.action_type not in {
        InteractionOperation.CLICK, InteractionOperation.TAP,
    } or preferred.risk_level is not InteractionRiskLevel.LOW
        or preferred.metadata.get("source_capture_id", work.source_capture_id)
        != work.source_capture_id):
        state.stop_reason = "invalid_current_frame_candidate"
        state.advance("done")
        return None
    if preferred is None and coordinate is None:
        state.stop_reason = "missing_locator"
        state.advance("done")
        return None
    if not state.consume_device_action():
        state.stop_reason = "budget_device_actions_exhausted"
        state.advance("done")
        return None
    preferred = preferred or _prefer_frontier_seek_candidate(
        work,
        semantic_target=semantic_target,
        coordinate=coordinate,
        extra=extra,
    )
    if preferred is not None:
        meta = {
            **dict(preferred.metadata),
            "purpose": purpose,
            "tangguo_flow_driver": True,
            **(dict(extra) if extra else {}),
        }
        candidate = CandidateAction(
            candidate_id=preferred.candidate_id,
            semantic_target=preferred.semantic_target or semantic_target,
            action_type=preferred.action_type,
            risk_level=preferred.risk_level,
            exploration_value=max(preferred.exploration_value, 95),
            navigation_depth=preferred.navigation_depth,
            expected_surface_kind=preferred.expected_surface_kind or work.surface_kind,
            metadata=meta,
        )
        return _build_intent(work, state, candidate=candidate, purpose=purpose)
    x, y = coordinate
    half = 0.03
    bounds = {
        "left": max(0.0, x - half),
        "top": max(0.0, y - half),
        "right": min(1.0, x + half),
        "bottom": min(1.0, y + half),
    }
    capture_id = str(work.source_capture_id or work.frontier.observation.capture_id or "unknown")
    candidate = CandidateAction(
        candidate_id=f"tangguo-flow-tap:{state.phase}:{capture_id}:{semantic_target}",
        semantic_target=semantic_target,
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=95,
        navigation_depth=0,
        expected_surface_kind=work.surface_kind,
        metadata={
            "purpose": purpose,
            "frontier_scope": "local",
            "normalized_bounds": bounds,
            "tangguo_flow_driver": True,
            **(dict(extra) if extra else {}),
        },
    )
    return _build_intent(work, state, candidate=candidate, purpose=purpose)


def _back_intent(
    work: ExplorationWork, state: TangguoFlowState, *, purpose: str
) -> ActionIntent | None:
    if not state.consume_device_action():
        state.stop_reason = "budget_device_actions_exhausted"
        state.advance("done")
        return None
    capture_id = str(work.source_capture_id or work.frontier.observation.capture_id or "unknown")
    candidate = CandidateAction(
        candidate_id=f"tangguo-flow-back:{state.phase}:{capture_id}",
        semantic_target="系统 BACK",
        action_type=InteractionOperation.BACK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=90,
        navigation_depth=0,
        expected_surface_kind=work.surface_kind,
        metadata={
            "purpose": purpose,
            "frontier_scope": "local",
            "expected_state_hint": "back",
            "tangguo_flow_driver": True,
            "forced_detail_exit_back": True,
        },
    )
    return _build_intent(work, state, candidate=candidate, purpose=purpose)


def _scroll_intent(
    work: ExplorationWork, state: TangguoFlowState, *, purpose: str
) -> ActionIntent | None:
    if not state.consume_device_action():
        state.stop_reason = "budget_device_actions_exhausted"
        state.advance("done")
        return None
    capture_id = str(work.source_capture_id or work.frontier.observation.capture_id or "unknown")
    candidate = CandidateAction(
        candidate_id=f"tangguo-flow-scroll:{state.phase}:{capture_id}",
        semantic_target="我的页向下捲动",
        action_type=InteractionOperation.SCROLL,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=80,
        navigation_depth=0,
        expected_surface_kind=work.surface_kind,
        metadata={
            "purpose": purpose,
            "frontier_scope": "local",
            "tangguo_flow_driver": True,
            "normalized_bounds": {
                "left": 0.15,
                "top": 0.35,
                "right": 0.85,
                "bottom": 0.75,
            },
            "action_parameters": {
                "from": {"x": 0.5, "y": 0.68},
                "to": {"x": 0.5, "y": 0.38},
                "duration_ms": 420,
            },
        },
    )
    return _build_intent(work, state, candidate=candidate, purpose=purpose)


def _horizontal_scroll_intent(
    work: ExplorationWork,
    state: TangguoFlowState,
    *,
    purpose: str,
    direction: str = "left",
) -> ActionIntent | None:
    """上方分类列横向滑动（避免系统手势区）。"""
    if not state.consume_device_action():
        state.stop_reason = "budget_device_actions_exhausted"
        state.advance("done")
        return None
    capture_id = str(
        work.source_capture_id or work.frontier.observation.capture_id or "unknown"
    )
    # left = 内容向左移，露出右侧分类（手指右→左）
    if direction == "left":
        fr, to = {"x": 0.78, "y": 0.14}, {"x": 0.28, "y": 0.14}
    else:
        fr, to = {"x": 0.28, "y": 0.14}, {"x": 0.78, "y": 0.14}
    candidate = CandidateAction(
        candidate_id=f"tangguo-flow-hscroll:{state.phase}:{capture_id}",
        semantic_target="上方分类横向滑动",
        action_type=InteractionOperation.SCROLL,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=75,
        navigation_depth=0,
        expected_surface_kind=work.surface_kind,
        metadata={
            "purpose": purpose,
            "frontier_scope": "local",
            "tangguo_flow_driver": True,
            "home_top_category_tab_seek": True,
            "normalized_bounds": {
                "left": 0.08,
                "top": 0.10,
                "right": 0.92,
                "bottom": 0.20,
            },
            "action_parameters": {"from": fr, "to": to, "duration_ms": 380},
        },
    )
    return _build_intent(work, state, candidate=candidate, purpose=purpose)


def _comic_read_scroll_intent(
    work: ExplorationWork,
    state: TangguoFlowState,
    *,
    purpose: str,
    ratio: float = 0.6,
) -> ActionIntent | None:
    """漫画向下阅读：手指由下往上滑，约阅读区高度 ratio；避开系统手势区。"""
    if not state.consume_device_action():
        state.stop_reason = "budget_device_actions_exhausted"
        state.advance("done")
        return None
    capture_id = str(
        work.source_capture_id or work.frontier.observation.capture_id or "unknown"
    )
    # 阅读区约 0.18~0.82；滑动距离 = 高度 * ratio
    top, bottom = 0.18, 0.82
    height = bottom - top
    start_y = min(0.78, bottom - 0.02)
    end_y = max(0.20, start_y - height * ratio)
    candidate = CandidateAction(
        candidate_id=f"tangguo-flow-comic-scroll:{state.comic_swipes_done}:{capture_id}",
        semantic_target="漫画阅读向下滑",
        action_type=InteractionOperation.SCROLL,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=85,
        navigation_depth=0,
        expected_surface_kind=work.surface_kind,
        metadata={
            "purpose": purpose,
            "frontier_scope": "local",
            "tangguo_flow_driver": True,
            "comic_read_scroll": True,
            "normalized_bounds": {
                "left": 0.12,
                "top": top,
                "right": 0.88,
                "bottom": bottom,
            },
            "action_parameters": {
                "from": {"x": 0.5, "y": round(start_y, 3)},
                "to": {"x": 0.5, "y": round(end_y, 3)},
                "duration_ms": 480,
            },
        },
    )
    return _build_intent(work, state, candidate=candidate, purpose=purpose)


def _build_intent(
    work: ExplorationWork,
    state: TangguoFlowState,
    *,
    candidate: CandidateAction,
    purpose: str,
) -> ActionIntent:
    digest = hashlib.sha256(
        json.dumps(
            [
                work.work_id,
                work.frontier_snapshot_id,
                work.source_capture_id,
                candidate.candidate_id,
                purpose,
                state.phase,
                state.device_actions_used,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    parameters: dict[str, object] = {
        "candidate_id": candidate.candidate_id,
        "exploration_work_id": work.work_id,
        "tangguo_flow_driver": True,
        "tangguo_flow_device_action": True,
        "tangguo_flow_state": state.to_dict(),
        "require_screenshot_evidence": True,
    }
    package = _package_name(work)
    if package:
        parameters["package_name"] = package
    if "normalized_bounds" in candidate.metadata:
        parameters["normalized_bounds"] = candidate.metadata["normalized_bounds"]
    if "action_parameters" in candidate.metadata:
        parameters["action_parameters"] = candidate.metadata["action_parameters"]
    for key in (
        "home_content_entry_seek",
        "home_top_category_tab_seek",
        "bottom_navigation_tab_seek",
        "tangguo_flow_phase",
        "tangguo_flow_tab",
        "wallet_entry_target",
        "payment_method",
        "inspect_only",
        "expand_more_pay",
        "forced_detail_exit_back",
        "comic_entry_seek",
        "comic_read_scroll",
    ):
        if key in candidate.metadata:
            parameters[key] = candidate.metadata[key]
    attach_synthetic_touch_contracts(parameters, candidate.metadata)
    return ActionIntent(
        task_id=work.task_id,
        session_id=work.session_id,
        intent_id=f"action-intent:{digest}",
        idempotency_key=f"action-intent:{digest}",
        surface_kind=work.surface_kind,
        source_capture_id=work.source_capture_id,
        action_type=candidate.action_type,
        semantic_target=candidate.semantic_target,
        parameters=parameters,
        purpose=purpose,
        risk_level=InteractionRiskLevel.LOW,
        capture_profile=CaptureProfilePolicy.for_surface(
            work.surface_kind,
            candidate.action_type,
        ),
        deadline=work.deadline,
    )


# ---------------------------------------------------------------------------
# State / config helpers
# ---------------------------------------------------------------------------


def _load_state(work: ExplorationWork) -> TangguoFlowState:
    meta = work.frontier.observation.metadata
    raw = meta.get("tangguo_flow_state") if isinstance(meta, Mapping) else None
    return TangguoFlowState.from_dict(raw if isinstance(raw, Mapping) else None)


def _stash_state(work: ExplorationWork, state: TangguoFlowState) -> None:
    """挂到 work 上供同轮／测试读取；跨帧靠 intent.parameters → lineage 写回。

    observation.metadata 为 MappingProxy，不可就地改写。
    """
    object.__setattr__(work, "_tangguo_flow_state_cache", state)


def read_tangguo_flow_state(work: ExplorationWork) -> TangguoFlowState:
    """读取主流程状态：优先同轮 cache，其次 metadata／intent 写回。"""
    cached = getattr(work, "_tangguo_flow_state_cache", None)
    if isinstance(cached, TangguoFlowState):
        return cached
    return _load_state(work)


def _package_name(work: ExplorationWork) -> str | None:
    meta = work.frontier.observation.metadata
    if isinstance(meta, Mapping):
        pkg = meta.get("package_name")
        if isinstance(pkg, str) and pkg.strip():
            return pkg.strip()
    if work.app_goal is not None:
        allowed = getattr(work.app_goal, "allowed_packages", ()) or ()
        if allowed:
            return str(allowed[0]).strip() or None
    return None


def _profile_tangguo_flow(work: ExplorationWork) -> dict[str, Any]:
    """合并 Profile／观察中的 flow 配置（不授坐标）。"""

    meta = work.frontier.observation.metadata
    if isinstance(meta, Mapping):
        profile = meta.get("agent_profile")
        if isinstance(profile, Mapping):
            section = profile.get("tangguo_flow")
            if isinstance(section, Mapping):
                return dict(section)
        section = meta.get("tangguo_flow")
        if isinstance(section, Mapping):
            return dict(section)
    package = _package_name(work)
    if not package:
        return {}
    try:
        from services.api.app.domain.app_product.profiles import load_profile

        loaded = load_profile(package)
    except Exception:  # noqa: BLE001 - Profile 缺失不得阻断主流程
        return {}
    if loaded is None:
        return {}
    section = loaded.data.get("tangguo_flow")
    return dict(section) if isinstance(section, Mapping) else {}


def _page(work: ExplorationWork) -> tuple[str, str]:
    obs = work.frontier.observation
    return str(obs.page_type or "").casefold(), str(obs.page_state or "").casefold()


def _ordered_tabs(work: ExplorationWork) -> list[tuple[str, tuple[str, ...]]]:
    observed = visible_bottom_tabs(work)
    if observed:
        return [(label, (label,)) for _, label, _ in observed]
    # 已发现清单由 state 保存；空帧不凭产品模板捏造必跑栏目。
    return []


def _locate_more_payments(work: ExplorationWork) -> ConstrainedLocateResult | None:
    from services.api.app.application.app_constrained_locate import (
        center_from_normalized_bounds,
    )

    for candidate in work.frontier.candidates:
        if candidate.action_type not in {
            InteractionOperation.CLICK,
            InteractionOperation.TAP,
        }:
            continue
        blob = " ".join(
            str(item or "")
            for item in (
                candidate.semantic_target,
                candidate.metadata.get("text"),
                candidate.metadata.get("content_desc"),
                candidate.metadata.get("label"),
            )
        )
        if not any(m in blob for m in _MORE_PAY):
            continue
        center = center_from_normalized_bounds(
            candidate.metadata.get("normalized_bounds")
            if isinstance(candidate.metadata.get("normalized_bounds"), Mapping)
            else None
        )
        return ConstrainedLocateResult(
            status="found",
            target="alipay",
            label=str(candidate.semantic_target or "更多支付方式"),
            coordinate=center,
            candidate_id=candidate.candidate_id,
            reason=None,
        )
    return None
