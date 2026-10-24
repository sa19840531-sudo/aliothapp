"""在唯一派发入口复核持久化调度限制，不触碰设备或改写覆盖事实。"""

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime

from services.api.app.application.app_candidate_eligibility import eligible_app_candidates
from services.api.app.application.bounded_exploration_scheduler import (
    BoundedExplorationScheduler,
    ExplorationStopReason,
    scheduler_missing_page_types,
    scheduler_state_from_work,
)
from services.api.app.domain.exploration import (
    H5_TASK_FRONTIER_METADATA_KEY,
    ExplorationTerminal,
    ExplorationTerminalStatus,
    ExplorationWork,
    ExplorerWork,
)
from services.api.app.domain.interaction import ActionIntent, SurfaceKind, TimedObservationIntent


def _popup_phase_allows_synthetic_decision(
    *,
    work: ExplorationWork,
    decision: ActionIntent,
) -> bool:
    """弹窗未清时，放行合成关层／清障观察，禁止绑定拒收收口。"""

    from services.api.app.application.app_exploration_flow_contract import (
        popup_phase_blocks_frontier_exhaustion,
    )

    if work.budget.remaining_actions <= 0:
        return False
    if not popup_phase_blocks_frontier_exhaustion(
        work.frontier.observation.metadata
    ):
        return False
    candidate_id = decision.parameters.get("candidate_id")
    if isinstance(candidate_id, str) and (
        candidate_id.startswith("forced-pixel-popup-close:")
        or candidate_id.startswith("forced-popup-clearance-observe:")
        or candidate_id.startswith("forced-popup-scrim-dismiss:")
        or candidate_id.startswith("forced-pixel-bottom-nav:")
    ):
        return True
    if decision.parameters.get("app_popup_close_corner") is not None:
        return True
    if decision.parameters.get("app_popup_clearance_observe") is not None:
        return True
    if decision.parameters.get("app_popup_scrim_dismiss") is not None:
        return True
    return False


def bounded_dispatch_terminal(
    *,
    work: ExplorerWork,
    decision: ActionIntent | TimedObservationIntent | ExplorationTerminal,
    now: datetime,
) -> ExplorationTerminal | None:
    """恢复的旧决策也要重验；Probe 继续使用自己的补采规则。"""

    if not isinstance(work, ExplorationWork) or isinstance(decision, ExplorationTerminal):
        return None
    reason = None
    try:
        state = scheduler_state_from_work(work)
        if state is None:
            return None
        h5_frontier_bound = isinstance(
            work.frontier.observation.metadata.get(H5_TASK_FRONTIER_METADATA_KEY),
            Mapping,
        )
        dispatch_state = (
            replace(state, retry_attempts={}) if h5_frontier_bound else state
        )
        stopped = dispatch_state.stop_reason(now=now)
        if stopped is not ExplorationStopReason.NONE:
            reason = stopped.value
        elif isinstance(decision, ActionIntent):
            candidate_id = decision.parameters.get("candidate_id")
            # 无候选 ID 的服务端阶段操作仍由阶段预算约束；全局停止条件不能豁免。
            if candidate_id is not None:
                if work.surface_kind in {SurfaceKind.ANDROID, SurfaceKind.IOS}:
                    # 借用预算只能基于安全候选，不能让已阻断入口挤占可用额度。
                    allowed, _ = eligible_app_candidates(work, now=now)
                else:
                    # H5 已在 Frontier 中按稳定候选身份关闭失败入口；队列累计
                    # 失败只保留审计，不得在全局停止或派发过滤时连坐其它栏目。
                    filter_state = dispatch_state
                    allowed = BoundedExplorationScheduler().filter_candidates(
                        work.frontier.candidates,
                        state=filter_state,
                        missing_page_types=scheduler_missing_page_types(work, state),
                        now=now,
                    )
                if not any(item.candidate_id == candidate_id for item in allowed):
                    frontier_restore = (
                        decision.parameters.get("app_frontier_item_id") is not None
                        or decision.parameters.get("app_frontier_inject_candidate")
                        is not None
                        or decision.parameters.get("bottom_navigation_tab_seek") is True
                        or decision.parameters.get("require_runtime_bottom_nav_rebind")
                        is True
                    )
                    synthetic_exit = (
                        decision.parameters.get("short_drama_top_left_back") is True
                        or decision.parameters.get("home_top_category_tab_seek") is True
                        or decision.parameters.get("home_top_category_sample_seek") is True
                        or (
                            isinstance(candidate_id, str)
                            and (
                                candidate_id.startswith("forced-system-back:")
                                # F02：不再放行合成左上角坐标点击。
                                or candidate_id.startswith("forced-pixel-bottom-nav:")
                                or candidate_id.startswith("forced-home-top-category:")
                                or candidate_id.startswith(
                                    "forced-pixel-home-top-category:"
                                )
                                or candidate_id.startswith(
                                    "forced-home-top-sample-first-card:"
                                )
                                or candidate_id.startswith(
                                    "forced-home-top-sample-match:"
                                )
                                or candidate_id.startswith(
                                    "forced-pixel-home-top-sample-first-card:"
                                )
                                or candidate_id.startswith(
                                    "forced-pixel-home-top-sample-match:"
                                )
                                or candidate_id.startswith(
                                    "forced-pixel-home-content-entry:"
                                )
                            )
                        )
                    )
                    # 任务级 Frontier／底栏 inject：当前帧调度池可能暂无同 ID，
                    # Runtime 会按条带重绑；不得因此 BOUNDED 终态并 WORKFLOW_CANCELED 未巡栏。
                    if frontier_restore and any(
                        item.candidate_id == candidate_id
                        for item in work.frontier.candidates
                    ):
                        reason = None
                    elif (
                        decision.parameters.get("hjll_script") is True
                        or decision.parameters.get("hlapp_script") is True
                        or decision.parameters.get("hlwapp_script") is True
                        or decision.parameters.get("product_script_orchestrated") is True
                        or (
                            isinstance(candidate_id, str)
                            and candidate_id.startswith(
                                ("hjll-script:", "hlapp-script:", "hlwapp-script:")
                            )
                        )
                        or (
                            isinstance(candidate_id, str)
                            and "-script:" in candidate_id
                            and decision.parameters.get("product_script_orchestrated") is True
                        )
                    ):
                        # 产品脚本步不在通用候选池里；不得因此收口。
                        reason = None
                    elif synthetic_exit:
                        # 短剧播完退出／详情强制 BACK：合成候选不在 eligible 池属预期。
                        reason = None
                    elif _popup_phase_allows_synthetic_decision(
                        work=work, decision=decision
                    ):
                        # 弹窗未清：合成关层／观察不在 eligible 池内属预期，禁止收口。
                        reason = None
                    else:
                        reason = "candidate_budget_or_binding_rejected"
                        # 兜底：弹窗流程未满足时，任何绑定拒收都不得结束探索。
                        from services.api.app.application.app_exploration_flow_contract import (
                            popup_phase_blocks_frontier_exhaustion,
                        )

                        if (
                            work.budget.remaining_actions > 0
                            and popup_phase_blocks_frontier_exhaustion(
                                work.frontier.observation.metadata
                            )
                        ):
                            reason = None
                        # 上方分类未巡完：绑定拒收不得直接 BOUNDED 收口（会停在终身卡层上）。
                        if reason is not None and work.budget.remaining_actions > 0:
                            from services.api.app.application.app_coverage_page_seek import (
                                home_top_category_tabs_incomplete,
                            )

                            observation = work.frontier.observation
                            metadata = (
                                observation.metadata
                                if isinstance(getattr(observation, "metadata", None), Mapping)
                                else {}
                            )
                            pkg = metadata.get("package_name") if isinstance(metadata, Mapping) else None
                            package_name = (
                                pkg.strip() if isinstance(pkg, str) and pkg.strip() else None
                            )
                            if package_name is None and work.app_goal is not None:
                                allowed = getattr(work.app_goal, "allowed_packages", ()) or ()
                                if allowed:
                                    package_name = str(allowed[0]).strip() or None
                            if home_top_category_tabs_incomplete(
                                coverage_metadata=metadata,
                                package_name=package_name,
                            ):
                                reason = None
    except (TypeError, ValueError):
        reason = "state_invalid"
    if (
        isinstance(decision, ActionIntent)
        and (
            decision.parameters.get("hjll_script") is True
            or decision.parameters.get("hlapp_script") is True
            or decision.parameters.get("hlwapp_script") is True
            or decision.parameters.get("product_script_orchestrated") is True
        )
        and reason
        not in {
            ExplorationStopReason.ACTION_BUDGET_EXHAUSTED.value,
            ExplorationStopReason.DEADLINE_EXPIRED.value,
            ExplorationStopReason.DURATION_BUDGET_EXHAUSTED.value,
            "state_invalid",
        }
    ):
        # 关弹窗期间画面重复是脚本本身，不能当成无进展或弹窗循环停掉。
        reason = None
    if reason is None:
        return None
    return ExplorationTerminal(
        task_id=work.task_id,
        work_id=work.work_id,
        status=ExplorationTerminalStatus.BOUNDED,
        outcome="evidence_insufficient",
        reason_code=f"bounded_exploration_{reason}",
        reason=f"有界探索已停止：{reason}；未完成目标仍保留，未发送操作。",
        coverage_summary={"scheduler_stop_reason": reason},
    )
