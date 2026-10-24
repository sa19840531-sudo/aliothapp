"""工作记忆：单次 Agent Run 内共享循环护栏状态与页面质检进度索引。

设计要点：
- 每次 ``run_main_agent`` 都创建独立实例并绑定当次 Run 的中间件，Agent 对象本身
  保持无状态、可跨多个 Run 复用。
- 页面记忆以 ``page_capture_id`` 为唯一键，不用页面类型或 URL 合并页面实例；
- 补采替代后的旧采集保留审计生命周期，但不参与活跃完成门禁。
- ``qc_page_capture`` 与 ``qc_rule_execution`` 仍是业务事实源；跨 Run 恢复必须从
  数据库重建，不能把本对象当作结论主表。
"""

import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from services.api.app.agents.runtime.page_inspection_memory import PageInspectionState

_PAGE_RULE_SCOPED_STUCK_TOOLS = frozenset(
    {
        "record_rule_execution_tool",
        "record_rule_executions_batch_tool",
        "rule_judge_tool",
        "rule_judge_batch_tool",
    }
)
_PAGE_FLOW_SCOPED_STUCK_TOOLS = frozenset(
    {
        "page_capture_tool",
        "page_detection_tool",
        "candidate_rules_tool",
    }
)


def normalize_arguments(arguments: Any) -> str:
    """将工具调用参数归一化为稳定、可比较的字符串。

    用于卡死检测（同一工具与参数的连续调用识别）与动作留痕展示。
    """
    if isinstance(arguments, BaseModel):
        payload = arguments.model_dump()
    elif isinstance(arguments, Mapping):
        payload = dict(arguments)
    else:
        return repr(arguments)
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)


def _logical_surface_fingerprint(value: Any) -> str | None:
    normalized = str(value or "").strip()
    if (
        len(normalized) != 71
        or not normalized.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in normalized[7:])
    ):
        return None
    return normalized


@dataclass(frozen=True)
class CallReservation:
    """一次工具调用的预算预占结果。"""

    accepted: bool
    """True 表示调用已占用一步预算，可以继续进入后续中间件。"""

    termination_reason: str | None = None
    """拒绝调用时的终止原因；放行时为 None。"""


@dataclass
class WorkingMemory:
    """单次 Agent Run 的工作记忆。"""

    step_count: int = 0
    """已被循环护栏接纳并占用预算的工具调用步数，始终不超过 max_steps。"""

    action_sequence: int = 0
    """动作留痕序号分配游标，在工具执行前同步递增，保证并发调用序号唯一。"""

    recent_call_signatures: list[tuple[str, str]] = field(default_factory=list)
    """最近已接纳调用的工具名与归一化参数，供卡死检测使用。"""

    termination_reason: str | None = None
    """循环护栏判定的终止原因（最大步数、卡死、取消或超时）；None 表示尚未被护栏终止。"""

    continuation_count: int = 0
    """模型提前结束但页面工作未收敛后，编排层已经追加的续跑次数。"""

    consecutive_infrastructure_failures: int = 0
    """连续发生的页面/规则基础设施故障次数；成功质检工具调用会清零。"""

    page_flow_blocking_failure: dict[str, Any] | None = None
    """当前页面控制面失败；只阻止无 Frontier 身份的盲采和对失败页重复识别。"""

    _page_inspection_state: PageInspectionState = field(
        default_factory=PageInspectionState, repr=False
    )
    """页面质检状态由独立模型维护，避免与循环护栏逻辑混杂。"""

    _visited_logical_page_identities: set[str] = field(default_factory=set, repr=False)
    """逻辑业务页按 identity 指纹去重，局部表面状态不得增加页数。"""

    _observed_logical_surface_states: set[str] = field(default_factory=set, repr=False)
    """逻辑表面状态按 state 指纹留痕，用于动作与弹窗审计。"""

    @property
    def visited_logical_page_count(self) -> int:
        """返回当前 Run 已观察到的唯一逻辑业务页数量。"""
        return len(self._visited_logical_page_identities)

    @property
    def observed_surface_state_count(self) -> int:
        """返回当前 Run 已观察到的唯一逻辑表面状态数量。"""
        return len(self._observed_logical_surface_states)

    def record_observation(self, observation: Any) -> bool:
        """记录 Frontier Observation 中已验签的逻辑身份与表面状态。"""
        metadata = getattr(observation, "metadata", None)
        if not isinstance(metadata, Mapping):
            return False
        identity = _logical_surface_fingerprint(
            metadata.get("logical_surface_identity_fingerprint")
        )
        state = _logical_surface_fingerprint(
            metadata.get("logical_surface_state_fingerprint")
        )
        if identity is None or state is None:
            return False
        self._visited_logical_page_identities.add(identity)
        self._observed_logical_surface_states.add(state)
        return True

    def allocate_action_sequence(self) -> int:
        """为一次工具调用分配唯一且递增的动作序号。"""
        self.action_sequence += 1
        return self.action_sequence

    def reserve_call(
        self,
        tool_name: str,
        arguments_repr: str,
        *,
        max_steps: int,
        stuck_window: int,
    ) -> CallReservation:
        """在工具执行前同步检查并预占预算。

        本方法内不得出现 ``await``。同一 Run 的工具中间件运行在同一个 asyncio
        事件循环中，同步完成终止锁存、上限检查、卡死判断、计数与签名登记，可以保证
        并行工具调用不会基于同一个旧计数同时越过护栏。
        """
        if self.termination_reason is not None:
            return CallReservation(accepted=False, termination_reason=self.termination_reason)

        if self.step_count >= max_steps:
            self.termination_reason = "max_steps_reached"
            return CallReservation(accepted=False, termination_reason=self.termination_reason)

        if self.would_be_stuck(tool_name, arguments_repr, window=stuck_window):
            if tool_name in _PAGE_RULE_SCOPED_STUCK_TOOLS:
                self.recent_call_signatures.clear()
                return CallReservation(
                    accepted=False,
                    termination_reason="page_rule_stuck_isolated",
                )
            if tool_name in _PAGE_FLOW_SCOPED_STUCK_TOOLS:
                self.recent_call_signatures.clear()
                return CallReservation(
                    accepted=False,
                    termination_reason="page_flow_stuck_isolated",
                )
            self.termination_reason = "stuck_detected"
            return CallReservation(accepted=False, termination_reason=self.termination_reason)

        self.step_count += 1
        self.recent_call_signatures.append((tool_name, arguments_repr))
        return CallReservation(accepted=True)

    def would_be_stuck(self, tool_name: str, arguments_repr: str, *, window: int) -> bool:
        """判断放行本次调用后是否会构成连续重复调用。

        必须在 ``reserve_call`` 写入本次调用前执行，避免与自身比较造成误判。
        """
        if window <= 0:
            return False
        recent = self.recent_call_signatures[-window:]
        if len(recent) < window:
            return False
        signature = (tool_name, arguments_repr)
        if all(existing == signature for existing in recent):
            return True
        # 连续重复只挡得住 A,A,A…，挡不住 A,B,A,B… 这种交替型循环。
        # 实测 91短视频（DX-015）在两个底栏 Tab 之间来回点了三十多次，
        # 每次都真的换了页，但页型始终判成 home，护栏一次都没触发，
        # 整个预算耗在两页之间。窗口填满、只剩两种调用且各自都反复
        # 出现时，同样按卡住处理；窗口小于 4 时不启用，避免把
        # 「点击 → 关弹窗 → 再点击」这类正常往返误判成循环。
        if window < 4:
            return False
        projected = [*recent[1:], signature]
        counts = Counter(projected)
        return len(counts) <= 2 and all(count >= 2 for count in counts.values())

    def remember_page_capture(
        self,
        *,
        page_capture_id: str,
        page_url: str | None,
        page_title: str | None,
        page_seq: int | None,
        artifact_ids: list[str],
        content_fingerprint: str | None = None,
        visible_text_fingerprint: str | None = None,
        obstruction_dismissal_count: int = 0,
        exploration_action_id: str | None = None,
        supersedes_page_capture_id: str | None = None,
    ) -> None:
        """记录最新页面采集，并把它设为当前页面。"""
        self._page_inspection_state.remember_page_capture(
            page_capture_id=page_capture_id,
            page_url=page_url,
            page_title=page_title,
            page_seq=page_seq,
            artifact_ids=artifact_ids,
            content_fingerprint=content_fingerprint,
            visible_text_fingerprint=visible_text_fingerprint,
            obstruction_dismissal_count=obstruction_dismissal_count,
            exploration_action_id=exploration_action_id,
            supersedes_page_capture_id=supersedes_page_capture_id,
        )

    def has_page_capture(self, page_capture_id: str) -> bool:
        """判断页面采集是否由当前 Run 产生。"""
        return self._page_inspection_state.has_page_capture(page_capture_id)

    def is_nested_return_capture(self, page_capture_id: str) -> bool:
        """判断页面是否由详情/播放页的受控返回动作产生。"""
        return self._page_inspection_state.is_nested_return_capture(page_capture_id)

    def page_artifact_ids(self, page_capture_id: str) -> list[str]:
        """返回指定页面在当前 Run 内采集到的真实证据 ID。"""
        return self._page_inspection_state.page_artifact_ids(page_capture_id)

    def has_prior_obstruction_capture_in_chain(self, page_capture_id: str) -> bool:
        """判断同一采集链上是否已有同类型遮挡页。"""
        return self._page_inspection_state.has_prior_obstruction_capture_in_chain(page_capture_id)

    def pending_detection_page_capture_id(self) -> str | None:
        """返回必须先完成识别的当前页面采集 ID。"""
        return self._page_inspection_state.pending_detection_page_capture_id()

    def has_page_capture_history(self) -> bool:
        """判断当前 Run 是否至少形成过一次页面采集。"""
        return self._page_inspection_state.has_page_capture_history()

    def remember_page_detection(
        self,
        *,
        page_capture_id: str,
        detection_status: str,
        page_type: str | None,
        rule_matching_allowed: bool,
        page_state: str | None = None,
        failure_code: str | None = None,
        failure_message: str | None = None,
        retryable: bool = False,
    ) -> bool:
        """记录页面识别门禁结果；页面不存在时返回 False。"""
        return self._page_inspection_state.remember_page_detection(
            page_capture_id=page_capture_id,
            detection_status=detection_status,
            page_type=page_type,
            page_state=page_state,
            rule_matching_allowed=rule_matching_allowed,
            failure_code=failure_code,
            failure_message=failure_message,
            retryable=retryable,
        )

    def remember_page_flow_blocking_failure(
        self,
        *,
        page_capture_id: str,
        failure_code: str,
        failure_message: str,
    ) -> None:
        """记录当前页不可重试控制面失败，供页面 Tool 阻止盲目重放。"""
        self.page_flow_blocking_failure = {
            "page_capture_id": page_capture_id,
            "code": failure_code,
            "message": failure_message,
        }

    def clear_page_flow_blocking_failure(self) -> None:
        """新页面证据就绪后解除旧页控制面阻断。"""
        self.page_flow_blocking_failure = None

    def page_detection_result(self, page_capture_id: str) -> dict[str, Any] | None:
        """返回指定页面采集的识别门禁结果。"""
        return self._page_inspection_state.page_detection_result(page_capture_id)

    def remember_exploration_candidates(
        self,
        *,
        page_capture_id: str,
        page_type: str | None,
        candidate_actions: list[Mapping[str, Any]],
        next_page_hypotheses: list[Mapping[str, Any]],
    ) -> list[str]:
        """把页面识别输出的候选动作写入探索 frontier。"""
        return self._page_inspection_state.remember_exploration_candidates(
            page_capture_id=page_capture_id,
            page_type=page_type,
            candidate_actions=candidate_actions,
            next_page_hypotheses=next_page_hypotheses,
        )

    def expire_stale_obstruction_actions(self, *, current_page_capture_id: str) -> list[str]:
        """稳定业务页出现后终结其他弹窗快照遗留的遮挡动作。"""
        return self._page_inspection_state.expire_stale_obstruction_actions(
            current_page_capture_id=current_page_capture_id
        )

    def ensure_transient_page_recovery(
        self, *, page_capture_id: str, page_type: str | None
    ) -> str | None:
        """补采未启用时将瞬态页面收口为证据不足。"""
        return self._page_inspection_state.ensure_transient_page_recovery(
            page_capture_id=page_capture_id,
            page_type=page_type,
        )

    def ensure_source_restore_transit_observation(
        self, *, page_capture_id: str, page_type: str | None
    ) -> str | None:
        """来源恢复经过开屏态时只做有界同会话观测。"""
        return self._page_inspection_state.ensure_source_restore_transit_observation(
            page_capture_id=page_capture_id,
            page_type=page_type,
        )

    def ensure_scroll_coverage_action(self, *, page_capture_id: str) -> str | None:
        """滚动覆盖需要补采时按证据不足收口。"""
        return self._page_inspection_state.ensure_scroll_coverage_action(
            page_capture_id=page_capture_id
        )

    def ensure_temporal_capture_action(self, *, page_capture_id: str) -> str | None:
        """倒计时或多帧规则需要补采时按证据不足收口。"""
        return self._page_inspection_state.ensure_temporal_capture_action(
            page_capture_id=page_capture_id
        )

    def ensure_interaction_evidence_action(
        self,
        *,
        page_capture_id: str,
        match_terms: list[str],
        required_capture_roles: list[str] | None = None,
    ) -> str | None:
        """交互规则需要补采时按证据不足收口，不接管探索候选。"""
        return self._page_inspection_state.ensure_interaction_evidence_action(
            page_capture_id=page_capture_id,
            match_terms=match_terms,
            required_capture_roles=required_capture_roles,
        )

    def defer_snapshot_page_inspection(self, *, page_capture_id: str) -> bool:
        """将纯静态规则移入后台判定，浏览器会话可继续探索。"""
        return self._page_inspection_state.defer_snapshot_page_inspection(
            page_capture_id=page_capture_id
        )

    def ensure_inspectable_page_transition(
        self, *, page_capture_id: str, page_type: str
    ) -> str | None:
        """为可质检过渡页保留规则完成后的同会话推进动作。"""
        return self._page_inspection_state.ensure_inspectable_page_transition(
            page_capture_id=page_capture_id,
            page_type=page_type,
        )

    def mandatory_page_action_capture_defaults(
        self, exploration_action_id: str | None
    ) -> dict[str, str | int | bool] | None:
        """返回必执行页面恢复采集需要绑定的来源页面信息。"""
        return self._page_inspection_state.mandatory_page_action_capture_defaults(
            exploration_action_id
        )

    def frontier_action_capture_defaults(
        self, exploration_action_id: str | None
    ) -> dict[str, str | int] | None:
        """返回已绑定 Frontier 动作执行时必须保留的浏览器边界参数。"""
        return self._page_inspection_state.frontier_action_capture_defaults(exploration_action_id)

    def is_exploration_action_runnable(self, exploration_action_id: str | None) -> bool:
        """确认 Frontier ID 真实存在且当前允许执行。"""
        return self._page_inspection_state.is_exploration_action_runnable(
            exploration_action_id
        )

    def degrade_mandatory_action_source_pages(
        self, *, exploration_action_ids: list[str], failure_message: str
    ) -> list[str]:
        """隔离已明确收口的开屏广告剩余规则，保留后续必执行动作。"""
        return self._page_inspection_state.degrade_mandatory_action_source_pages(
            exploration_action_ids=exploration_action_ids,
            failure_message=failure_message,
        )

    def blocked_mandatory_transition_action_ids(self) -> list[str]:
        """返回仍被来源页未完成规则阻塞的必执行过渡动作。"""
        return self._page_inspection_state.blocked_mandatory_transition_action_ids()

    def degrade_stalled_rule_pages(self, *, failure_message: str) -> list[str]:
        """隔离连续无进展的普通页面规则，继续处理其他动态页面。"""
        return self._page_inspection_state.degrade_stalled_rule_pages(
            failure_message=failure_message
        )

    def fail_unstarted_rule_matching_pages(self, *, failure_message: str) -> list[str]:
        """终结候选规则查询尚未启动但已连续空转的业务页。"""
        return self._page_inspection_state.fail_unstarted_rule_matching_pages(
            failure_message=failure_message
        )

    def register_rule_write_rejection(self, *, page_capture_id: str) -> int:
        """累计页面规则写回拒绝次数，供中间件中断重复无效提交。"""
        return self._page_inspection_state.register_rule_write_rejection(
            page_capture_id=page_capture_id
        )

    def resolve_mandatory_page_action(self, *, action: str | None) -> str | None:
        """在唯一匹配时返回模型遗漏的必执行 Frontier 动作 ID。"""
        return self._page_inspection_state.resolve_mandatory_page_action(action=action)

    def resolve_unique_frontier_page_action(self, *, action: str | None) -> str | None:
        """在唯一可执行 Frontier 时返回模型遗漏的动作 ID。"""
        return self._page_inspection_state.resolve_unique_frontier_page_action(action=action)

    def prepare_next_exploration_action(
        self, *, blocking_page_capture_id: str | None = None
    ) -> dict[str, Any] | None:
        """选择一个满足页面质检顺序约束的 Frontier 动作。"""
        return self._page_inspection_state.prepare_next_exploration_action(
            blocking_page_capture_id=blocking_page_capture_id
        )

    def set_rule_target_page_types(self, page_types: set[str]) -> None:
        """把规则集适用页面传给页面探索调度器。"""
        self._page_inspection_state.set_rule_target_page_types(page_types)

    def is_same_url_obstruction_page(self, page_capture_id: str) -> bool:
        """判断认证风险是否只是来源业务页上的同地址弹层。"""
        return self._page_inspection_state.is_same_url_obstruction_page(page_capture_id)

    def is_obstruction_chain_continuation(self, page_capture_id: str) -> bool:
        """判断当前采集是否仍属于同一弹窗清障链。"""
        return self._page_inspection_state.is_obstruction_chain_continuation(page_capture_id)

    def can_reuse_completed_obstruction_source(
        self,
        *,
        page_capture_id: str,
        page_type: str | None,
        page_state: str | None,
    ) -> bool:
        """判断稳定页关闭局部遮挡后是否可直接复用来源页质检结果。"""
        return self._page_inspection_state.can_reuse_completed_obstruction_source(
            page_capture_id=page_capture_id,
            page_type=page_type,
            page_state=page_state,
        )

    def is_source_restore_transit(self, page_capture_id: str) -> bool:
        """判断当前采集是否处于来源恢复的清障过渡链。"""
        return self._page_inspection_state.is_source_restore_transit(page_capture_id)

    def rebind_restored_source_action_if_ready(
        self,
        *,
        page_capture_id: str,
        detection_status: str,
        page_type: str | None,
        page_state: str | None,
    ) -> str | None:
        """稳定命中原页面后再重绑旧 Frontier。"""
        return self._page_inspection_state.rebind_restored_source_action_if_ready(
            page_capture_id=page_capture_id,
            detection_status=detection_status,
            page_type=page_type,
            page_state=page_state,
        )

    def resolve_auxiliary_page_return_if_ready(
        self,
        *,
        page_capture_id: str,
        detection_status: str,
        page_type: str | None,
        page_state: str | None,
    ) -> str | None:
        """稳定命中真实来源页后再完成辅助页浏览器返回。"""
        return self._page_inspection_state.resolve_auxiliary_page_return_if_ready(
            page_capture_id=page_capture_id,
            detection_status=detection_status,
            page_type=page_type,
            page_state=page_state,
        )

    def fail_next_stalled_exploration_action(
        self, *, failure_code: str, failure_message: str
    ) -> str | None:
        """只隔离一个连续无进展动作，保留其他动态探索分支。"""
        return self._page_inspection_state.fail_next_stalled_exploration_action(
            failure_code=failure_code,
            failure_message=failure_message,
        )

    def has_runnable_frontier_for_closure(
        self,
        *,
        exploration_action_ids: list[str],
        close_all_pending_actions: bool,
        max_depth: int | None,
    ) -> bool:
        """判断收口目标中是否仍有可由运行时放行的低风险 Frontier。"""
        return self._page_inspection_state.has_runnable_frontier_for_closure(
            exploration_action_ids=exploration_action_ids,
            close_all_pending_actions=close_all_pending_actions,
            max_depth=max_depth,
        )

    def require_exploration_action(
        self,
        *,
        exploration_action_id: str,
        blocks_page_inspection: bool,
        supersedes_source_page: bool,
        requires_source_inspection_complete: bool = False,
    ) -> bool:
        """把页面恢复动作升级为不可普通跳过的必执行动作。"""
        return self._page_inspection_state.require_exploration_action(
            exploration_action_id=exploration_action_id,
            blocks_page_inspection=blocks_page_inspection,
            supersedes_source_page=supersedes_source_page,
            requires_source_inspection_complete=requires_source_inspection_complete,
        )

    def mandatory_action_block_reason(self, exploration_action_id: str | None) -> str | None:
        """返回必执行动作尚未满足的页面质检前置条件。"""
        return self._page_inspection_state.mandatory_action_block_reason(exploration_action_id)

    def has_blocking_mandatory_page_action(self, page_capture_id: str) -> bool:
        """判断页面规则匹配前是否仍有必执行遮挡恢复动作。"""
        return self._page_inspection_state.has_blocking_mandatory_page_action(page_capture_id)

    def ensure_entry_exploration_action(
        self,
        *,
        action: str,
        target: str | None,
        navigation_depth: int | None,
    ) -> str:
        """为入口页面导航建立可审计的探索动作。"""
        return self._page_inspection_state.ensure_entry_exploration_action(
            action=action,
            target=target,
            navigation_depth=navigation_depth,
        )

    def seed_entry_exploration_action(
        self,
        *,
        action: str,
        target: str | None,
        navigation_depth: int | None,
    ) -> str:
        """在首轮模型调用前预建入口页面 Frontier。"""
        return self._page_inspection_state.seed_entry_exploration_action(
            action=action,
            target=target,
            navigation_depth=navigation_depth,
        )

    def begin_page_capture_attempt(
        self,
        *,
        exploration_action_id: str | None,
        action: str,
        target: str | None,
        navigation_depth: int | None,
    ) -> str | None:
        """关联页面采集尝试与入口或动态 Frontier 动作。"""
        return self._page_inspection_state.begin_page_capture_attempt(
            exploration_action_id=exploration_action_id,
            action=action,
            target=target,
            navigation_depth=navigation_depth,
        )

    def mark_exploration_action_selected(self, exploration_action_id: str) -> bool:
        """把一个动态 Frontier 动作标记为执行中。"""
        return self._page_inspection_state.mark_exploration_action_selected(exploration_action_id)

    def fail_exploration_action(
        self,
        *,
        exploration_action_id: str,
        failure_code: str,
        failure_message: str,
        retryable: bool,
        failure_reason: str | None = None,
    ) -> bool:
        """把无法继续的探索动作写入失败终态。"""
        return self._page_inspection_state.fail_exploration_action(
            exploration_action_id=exploration_action_id,
            failure_code=failure_code,
            failure_message=failure_message,
            retryable=retryable,
            failure_reason=failure_reason,
        )

    def ensure_failed_obstruction_reobservation(self, *, exploration_action_id: str) -> str | None:
        """关闭遮挡失败后建立同会话重采动作。"""
        return self._page_inspection_state.ensure_failed_obstruction_reobservation(
            exploration_action_id=exploration_action_id
        )

    def has_pending_exploration(self) -> bool:
        """判断当前 Run 是否仍有待探索动作。"""
        return self._page_inspection_state.has_pending_exploration()

    def has_blocked_obstruction(self) -> bool:
        """判断遮挡关闭动作是否被安全策略阻断。"""
        return self._page_inspection_state.has_blocked_obstruction()

    def has_runnable_work(self) -> bool:
        """判断是否仍有页面或 Frontier 动作需要处理。"""
        return self._page_inspection_state.has_runnable_work()

    def discovered_page_work_count(self) -> int:
        """返回动态发现的页面工作量，仅用于扩展安全预算，不作为完成条件。"""
        return self._page_inspection_state.discovered_work_count()

    def is_page_exploration_converged(self) -> bool:
        """判断所有已发现页面工作是否均已进入终态。"""
        return self._page_inspection_state.is_converged()

    def increment_continuation_count(self) -> int:
        """登记一次连续无页面进展的外层续跑并返回计数。"""
        self.continuation_count += 1
        return self.continuation_count

    def reset_continuation_count(self) -> None:
        """页面工作产生新进展后重置连续续跑计数。"""
        self.continuation_count = 0

    def page_progress_signature(self) -> str:
        """返回稳定的页面工作签名，用于区分进展与空转。"""
        return json.dumps(
            self._page_inspection_state.snapshot(compact=True),
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )

    def record_tool_health(self, *, infrastructure_failure: bool) -> None:
        """累计连续基础设施故障；正常质检工具结果会恢复健康计数。"""
        if infrastructure_failure:
            self.consecutive_infrastructure_failures += 1
        else:
            self.consecutive_infrastructure_failures = 0

    def terminalize_remaining_work(
        self,
        *,
        failure_stage: str,
        failure_code: str,
        failure_message: str,
    ) -> dict[str, int]:
        """把续跑或基础设施熔断后的悬空页面工作统一收口。"""
        return self._page_inspection_state.terminalize_remaining_work(
            failure_stage=failure_stage,
            failure_code=failure_code,
            failure_message=failure_message,
        )

    def requires_review(self) -> bool:
        """判断已收敛结果是否存在执行缺口。"""
        return self._page_inspection_state.requires_review()

    def remember_page_type_assessments(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        assessments: list[Mapping[str, Any]],
        scope_complete: bool,
        scope_page_types: list[str] | tuple[str, ...] = (),
    ) -> int:
        """保存当前 Run 的产品业务页面类型评估。"""
        return self._page_inspection_state.remember_page_type_assessments(
            task_id=task_id,
            agent_run_id=agent_run_id,
            assessments=assessments,
            scope_complete=scope_complete,
            scope_page_types=scope_page_types,
        )

    def observed_business_page_types(self) -> tuple[str, ...]:
        """返回当前 Run 已由页面识别确认的业务页面类型。"""

        return self._page_inspection_state.observed_business_page_types()

    def close_exploration_items(
        self,
        *,
        action_status: str,
        reason: str,
        exploration_action_ids: list[str],
        close_all_pending_actions: bool,
        page_capture_ids: list[str],
        abandon_all_pending_pages: bool,
    ) -> dict[str, list[str]]:
        """关闭不再执行的探索动作和未开始规则匹配的页面。"""
        return self._page_inspection_state.close_exploration_items(
            action_status=action_status,
            reason=reason,
            exploration_action_ids=exploration_action_ids,
            close_all_pending_actions=close_all_pending_actions,
            page_capture_ids=page_capture_ids,
            abandon_all_pending_pages=abandon_all_pending_pages,
        )

    def abandon_unmatchable_page(self, *, page_capture_id: str, reason: str) -> bool:
        """关闭已识别但不允许进入规则匹配的页面。"""
        return self._page_inspection_state.abandon_unmatchable_page(
            page_capture_id=page_capture_id,
            reason=reason,
        )

    def skip_authentication_required_page(
        self,
        *,
        page_capture_id: str,
        page_type: str | None,
    ) -> str | None:
        """跳过登录态门禁页，并在动态分支中返回后继续探索。"""
        return self._page_inspection_state.skip_authentication_required_page(
            page_capture_id=page_capture_id,
            page_type=page_type,
        )

    def abandon_revisited_page(self, *, page_capture_id: str, reason: str) -> bool:
        """关闭详情返回后的重复列表采集，同时保留新增探索动作。"""
        return self._page_inspection_state.abandon_revisited_page(
            page_capture_id=page_capture_id,
            reason=reason,
        )

    def has_started_page_inspection(self, page_capture_id: str) -> bool:
        """判断页面是否已经完成候选规则匹配。"""
        return self._page_inspection_state.has_started_page_inspection(page_capture_id)

    def has_incomplete_page_inspection(self) -> bool:
        """判断当前 Run 是否仍有已采集但未完成规则写回的活跃页面。"""
        return self._page_inspection_state.has_incomplete_page_inspection()

    def start_page_inspection(
        self,
        *,
        page_capture_id: str,
        page_type: str,
        candidate_rules: list[Mapping[str, Any]],
    ) -> bool:
        """为指定页面建立候选规则清单；页面不存在时返回 False。"""
        return self._page_inspection_state.start_page_inspection(
            page_capture_id=page_capture_id,
            page_type=page_type,
            candidate_rules=candidate_rules,
        )

    def record_page_rule_result(
        self,
        *,
        page_capture_id: str,
        result: Mapping[str, Any],
        arguments: Mapping[str, Any],
    ) -> bool:
        """把成功规则写回同步到对应页面；页面尚未匹配规则时返回 False。"""
        return self._page_inspection_state.record_page_rule_result(
            page_capture_id=page_capture_id,
            result=result,
            arguments=arguments,
        )

    def page_rule_result(
        self,
        *,
        page_capture_id: str,
        rule_id: str | None,
        rule_code: str | None,
    ) -> dict[str, Any] | None:
        """返回当前页面指定候选规则已经形成的终态。"""
        return self._page_inspection_state.page_rule_result(
            page_capture_id=page_capture_id,
            rule_id=rule_id,
            rule_code=rule_code,
        )

    def record_rule_judgement(
        self,
        *,
        page_capture_id: str,
        judgement: Mapping[str, Any],
    ) -> bool:
        """登记确定性判定与规则落库之间的待交接状态。"""
        return self._page_inspection_state.record_rule_judgement(
            page_capture_id=page_capture_id,
            judgement=judgement,
        )

    def fail_page_inspection(
        self,
        *,
        page_capture_id: str,
        failure_stage: str,
        failure_code: str,
        failure_message: str,
        retryable: bool,
        missing_rule_status: str = "error",
    ) -> bool:
        """把当前页面写入失败终态，不再阻塞后续页面。"""
        return self._page_inspection_state.fail_page_inspection(
            page_capture_id=page_capture_id,
            failure_stage=failure_stage,
            failure_code=failure_code,
            failure_message=failure_message,
            retryable=retryable,
            missing_rule_status=missing_rule_status,
        )

    def degrade_page_inspection(
        self,
        *,
        page_capture_id: str,
        failure_stage: str,
        failure_code: str,
        failure_message: str,
        retryable: bool,
        missing_rule_status: str = "error",
    ) -> bool:
        """把已开始质检的页面写入降级终态。"""
        return self._page_inspection_state.degrade_page_inspection(
            page_capture_id=page_capture_id,
            failure_stage=failure_stage,
            failure_code=failure_code,
            failure_message=failure_message,
            retryable=retryable,
            missing_rule_status=missing_rule_status,
        )

    def model_snapshot(self) -> dict[str, Any]:
        """返回追加到 Tool 结果的紧凑页面进度。"""
        snapshot = self._page_inspection_state.snapshot(compact=True)
        if self.page_flow_blocking_failure is not None:
            snapshot["page_flow_blocking_failure"] = dict(self.page_flow_blocking_failure)
        return snapshot

    def audit_snapshot(self, *, action_trace_count: int | None = None) -> dict[str, Any]:
        """返回可写入 Redis 与 ``qc_agent_run.working_memory`` 的完整审计快照。"""
        snapshot = self._page_inspection_state.snapshot(compact=False)
        snapshot.update(
            {
                "step_count": self.step_count,
                "action_sequence": self.action_sequence,
                "termination_reason": self.termination_reason,
                "continuation_count": self.continuation_count,
                "consecutive_infrastructure_failures": (self.consecutive_infrastructure_failures),
            }
        )
        if self.page_flow_blocking_failure is not None:
            snapshot["page_flow_blocking_failure"] = dict(self.page_flow_blocking_failure)
        if action_trace_count is not None:
            snapshot["action_trace_count"] = action_trace_count
        return snapshot
