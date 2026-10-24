"""H5/App 共用的有限探索调度合同。

本模块只处理候选的队列归类、确定性排序和有界预算计算，不持有设备、浏览器
或队列租约。实际动作和 PostgreSQL 重试状态仍由各平台 Runtime 负责。
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from services.api.app.domain.exploration.contracts import ExplorationWork
from services.api.app.domain.exploration.h5_coverage import H5_COVERAGE_METADATA_KEY
from services.api.app.domain.exploration.snapshots import CandidateAction
from services.api.app.domain.protocol import require_aware_datetime, stable_protocol_hash

BOUNDED_EXPLORATION_POLICY_VERSION = "bounded-exploration.v1"
BOUNDED_EXPLORATION_STATE_VERSION = "bounded-exploration-state.v1"
BOUNDED_EXPLORATION_STATE_KEY = "bounded_exploration_scheduler"


class ExplorationQueue(StrEnum):
    """候选在一次有限探索中的三条逻辑队列。"""

    REQUIRED = "required"
    EVIDENCE_GAP = "evidence_gap"
    DISCOVERY = "discovery"


class ExplorationStopReason(StrEnum):
    """调度器能够确定的停止原因。"""

    NONE = "none"
    ACTION_BUDGET_EXHAUSTED = "action_budget_exhausted"
    DURATION_BUDGET_EXHAUSTED = "duration_budget_exhausted"
    DEADLINE_EXPIRED = "deadline_expired"
    RETRY_BUDGET_EXHAUSTED = "retry_budget_exhausted"
    NO_PROGRESS_LIMIT = "no_progress_limit"
    REPEAT_LIMIT = "repeat_limit"
    REPEATED_PAGE_LIMIT = "repeated_page_limit"
    POPUP_LOOP_LIMIT = "popup_loop_limit"
    RETURN_LOOP_LIMIT = "return_loop_limit"


@dataclass(frozen=True, slots=True)
class BoundedExplorationPolicy:
    """版本化的默认调度策略。

    比例用于解释和预算切分；平台运行时仍可把更严格的动作/时间上限传进
    不能通过候选元数据放宽上限。
    """

    policy_version: str = BOUNDED_EXPLORATION_POLICY_VERSION
    required_ratio: int = 70
    evidence_gap_ratio: int = 20
    discovery_ratio: int = 10
    no_progress_limit: int = 3
    repeat_limit: int = 3
    retry_limit: int = 2

    def __post_init__(self) -> None:
        if self.policy_version != BOUNDED_EXPLORATION_POLICY_VERSION:
            raise ValueError("不支持的有限探索策略版本")
        ratios = (self.required_ratio, self.evidence_gap_ratio, self.discovery_ratio)
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in ratios
        ):
            raise ValueError("队列比例必须为非负整数")
        if sum(ratios) != 100:
            raise ValueError("队列比例之和必须为 100")
        for name in ("no_progress_limit", "repeat_limit", "retry_limit"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} 必须为正整数")

    @property
    def policy_hash(self) -> str:
        """供运行日志和重放使用的策略指纹。"""

        return stable_protocol_hash(self.to_dict())

    def to_dict(self) -> dict[str, int | str]:
        return {
            "policy_version": self.policy_version,
            "required_ratio": self.required_ratio,
            "evidence_gap_ratio": self.evidence_gap_ratio,
            "discovery_ratio": self.discovery_ratio,
            "no_progress_limit": self.no_progress_limit,
            "repeat_limit": self.repeat_limit,
            "retry_limit": self.retry_limit,
        }

    def allocation(self, max_actions: int) -> dict[ExplorationQueue, int]:
        """把总预算切为三条逻辑队列，整数余数统一归入必检队列。"""

        _non_negative_int(max_actions, "max_actions")
        evidence_gap = max_actions * self.evidence_gap_ratio // 100
        discovery = max_actions * self.discovery_ratio // 100
        required = max_actions - evidence_gap - discovery
        return {
            ExplorationQueue.REQUIRED: required,
            ExplorationQueue.EVIDENCE_GAP: evidence_gap,
            ExplorationQueue.DISCOVERY: discovery,
        }

    def retry_allocation(self) -> dict[ExplorationQueue, int]:
        """为每个队列保留独立的重试上限，不能跨队列透支。"""

        return {
            ExplorationQueue.REQUIRED: self.retry_limit,
            ExplorationQueue.EVIDENCE_GAP: self.retry_limit,
            ExplorationQueue.DISCOVERY: self.retry_limit,
        }


@dataclass(frozen=True, slots=True)
class BoundedExplorationState:
    """随 ExplorationWork 保存的三队列累计状态。

    该状态是现有 Frontier/Run payload 中的一个受限 JSON 摘要，不依赖新表。
    每类动作和重试分别累计，失败重试同时按稳定候选身份留痕；未使用的动作额度
    允许在其它队列没有可执行候选时借用，并通过 ``borrowed_actions`` 留下痕迹。
    """

    max_actions: int
    max_duration_ms: int | None = None
    deadline: datetime | None = None
    consumed_actions: Mapping[str, int] | None = None
    consumed_duration_ms: Mapping[str, int] | None = None
    retry_attempts: Mapping[str, int] | None = None
    retry_attempts_by_candidate: Mapping[str, int] | None = None
    borrowed_actions: Mapping[str, int] | None = None
    borrowed_duration_ms: Mapping[str, int] | None = None
    required_page_types: tuple[str, ...] = ()
    evidence_gap_page_types: tuple[str, ...] = ()
    no_progress_count: int = 0
    repeat_count: int = 0
    last_state_fingerprint: str | None = None
    last_page_fingerprint: str | None = None
    page_repeat_count: int = 0
    last_popup_fingerprint: str | None = None
    popup_repeat_count: int = 0
    # 底栏硬覆盖未完时禁止因弹层循环整 run 收口（仍可累计计数供 retain 让路）
    suppress_popup_loop_stop: bool = False
    last_return_fingerprint: str | None = None
    return_repeat_count: int = 0
    return_fingerprint_history: tuple[str, ...] = ()
    policy: BoundedExplorationPolicy = BoundedExplorationPolicy()
    protocol_version: str = BOUNDED_EXPLORATION_STATE_VERSION

    def __post_init__(self) -> None:
        _non_negative_int(self.max_actions, "max_actions")
        if self.max_duration_ms is not None:
            _non_negative_int(self.max_duration_ms, "max_duration_ms")
        for name in (
            "no_progress_count",
            "repeat_count",
            "page_repeat_count",
            "popup_repeat_count",
            "return_repeat_count",
        ):
            _non_negative_int(getattr(self, name), name)
        if self.deadline is not None:
            require_aware_datetime(self.deadline, "deadline")
        for name in (
            "last_state_fingerprint",
            "last_page_fingerprint",
            "last_popup_fingerprint",
            "last_return_fingerprint",
        ):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, str)
                or not value.strip()
                or value != value.strip()
            ):
                raise ValueError(f"{name} 必须为无首尾空白的非空字符串")
        if self.protocol_version != BOUNDED_EXPLORATION_STATE_VERSION:
            raise ValueError("不支持的有限探索状态版本")
        if not isinstance(self.policy, BoundedExplorationPolicy):
            raise TypeError("policy 必须为 BoundedExplorationPolicy")
        for name in (
            "consumed_actions",
            "consumed_duration_ms",
            "retry_attempts",
            "borrowed_actions",
            "borrowed_duration_ms",
        ):
            normalized = _queue_counter(getattr(self, name), name)
            object.__setattr__(self, name, normalized)
        object.__setattr__(
            self,
            "retry_attempts_by_candidate",
            _candidate_retry_counter(
                self.retry_attempts_by_candidate,
                max_entries=self.max_actions,
            ),
        )
        if self.total_consumed_actions > self.max_actions:
            raise ValueError("三队列动作累计不能超过总动作预算")
        if (
            self.max_duration_ms is not None
            and self.total_consumed_duration_ms > self.max_duration_ms
        ):
            raise ValueError("三队列时间累计不能超过总时间预算")
        object.__setattr__(
            self,
            "required_page_types",
            _normalized_text_tuple(self.required_page_types, "required_page_types"),
        )
        object.__setattr__(
            self,
            "evidence_gap_page_types",
            _normalized_text_tuple(
                self.evidence_gap_page_types,
                "evidence_gap_page_types",
            ),
        )
        object.__setattr__(
            self,
            "return_fingerprint_history",
            _normalized_signature_tuple(self.return_fingerprint_history),
        )

    @classmethod
    def create(
        cls,
        *,
        max_actions: int,
        max_duration_ms: int | None = None,
        deadline: datetime | None = None,
        policy: BoundedExplorationPolicy | None = None,
        required_page_types: Collection[str] = (),
        evidence_gap_page_types: Collection[str] = (),
    ) -> BoundedExplorationState:
        return cls(
            max_actions=max_actions,
            max_duration_ms=max_duration_ms,
            deadline=deadline,
            policy=policy or BoundedExplorationPolicy(),
            required_page_types=tuple(required_page_types),
            evidence_gap_page_types=tuple(evidence_gap_page_types),
        )

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        default_max_actions: int,
        default_max_duration_ms: int | None = None,
        default_deadline: datetime | None = None,
        policy: BoundedExplorationPolicy | None = None,
        required_page_types: Collection[str] | None = None,
        evidence_gap_page_types: Collection[str] | None = None,
    ) -> BoundedExplorationState:
        """从历史 Work 恢复；缺少本状态时只创建零计数，不猜旧消费。"""

        if not isinstance(payload, Mapping):
            raise TypeError("scheduler state 必须为对象")
        raw_version = payload.get("protocol_version", BOUNDED_EXPLORATION_STATE_VERSION)
        if raw_version != BOUNDED_EXPLORATION_STATE_VERSION:
            raise ValueError("scheduler state 版本不受支持")
        effective_policy = policy or BoundedExplorationPolicy()
        stored_policy_version = payload.get("policy_version")
        if (
            stored_policy_version is not None
            and stored_policy_version != effective_policy.policy_version
        ):
            raise ValueError("scheduler state 策略版本不受支持")
        stored_policy_hash = payload.get("policy_hash")
        if stored_policy_hash is not None and stored_policy_hash != effective_policy.policy_hash:
            raise ValueError("scheduler state 策略指纹不匹配")
        raw_max = payload.get("max_actions", default_max_actions)
        raw_duration = payload.get("max_duration_ms", default_max_duration_ms)
        deadline = _parse_optional_deadline(payload.get("deadline"), default_deadline)
        stored_required = payload.get("required_page_types", ())
        stored_evidence_gaps = payload.get("evidence_gap_page_types", ())
        effective_required = (
            stored_required if required_page_types is None else required_page_types
        )
        effective_evidence_gaps = (
            stored_evidence_gaps
            if evidence_gap_page_types is None
            else evidence_gap_page_types
        )
        return cls(
            max_actions=raw_max,
            max_duration_ms=raw_duration,
            deadline=deadline,
            consumed_actions=payload.get("consumed_actions"),
            consumed_duration_ms=payload.get("consumed_duration_ms"),
            retry_attempts=payload.get("retry_attempts"),
            retry_attempts_by_candidate=payload.get("retry_attempts_by_candidate"),
            borrowed_actions=payload.get("borrowed_actions"),
            borrowed_duration_ms=payload.get("borrowed_duration_ms"),
            required_page_types=effective_required,
            evidence_gap_page_types=effective_evidence_gaps,
            no_progress_count=payload.get("no_progress_count", 0),
            repeat_count=payload.get("repeat_count", 0),
            last_state_fingerprint=payload.get("last_state_fingerprint"),
            last_page_fingerprint=payload.get("last_page_fingerprint"),
            page_repeat_count=payload.get("page_repeat_count", 0),
            last_popup_fingerprint=payload.get("last_popup_fingerprint"),
            popup_repeat_count=payload.get("popup_repeat_count", 0),
            suppress_popup_loop_stop=bool(payload.get("suppress_popup_loop_stop", False)),
            last_return_fingerprint=payload.get("last_return_fingerprint"),
            return_repeat_count=payload.get("return_repeat_count", 0),
            return_fingerprint_history=payload.get("return_fingerprint_history", ()),
            policy=effective_policy,
        )

    @property
    def total_consumed_actions(self) -> int:
        return sum(self.consumed_actions.values())

    @property
    def total_retry_attempts(self) -> int:
        return sum(self.retry_attempts.values())

    @property
    def total_consumed_duration_ms(self) -> int:
        return sum(self.consumed_duration_ms.values())

    def queue_consumed(self, queue: ExplorationQueue) -> int:
        return int(self.consumed_actions.get(queue.value, 0))

    def queue_retries(self, queue: ExplorationQueue) -> int:
        return int(self.retry_attempts.get(queue.value, 0))

    def queue_retry_budget(self, queue: ExplorationQueue) -> int:
        return self.policy.retry_allocation()[queue]

    def queue_retry_budget_exhausted(self, queue: ExplorationQueue) -> bool:
        return self.queue_retries(queue) >= self.queue_retry_budget(queue)

    def candidate_retry_budget_exhausted(self, candidate: CandidateAction) -> bool:
        identity = _candidate_retry_identity(candidate)
        return int(self.retry_attempts_by_candidate.get(identity, 0)) >= self.policy.retry_limit

    def queue_allocation(self, queue: ExplorationQueue) -> int:
        return self.policy.allocation(self.max_actions)[queue]

    def queue_budget_exhausted(self, queue: ExplorationQueue) -> bool:
        return self.queue_consumed(queue) >= self.queue_allocation(queue)

    def queue_duration_consumed_ms(self, queue: ExplorationQueue) -> int:
        return int(self.consumed_duration_ms.get(queue.value, 0))

    def queue_duration_allocation_ms(self, queue: ExplorationQueue) -> int | None:
        if self.max_duration_ms is None:
            return None
        return self.policy.allocation(self.max_duration_ms)[queue]

    def queue_time_budget_exhausted(self, queue: ExplorationQueue) -> bool:
        allocation = self.queue_duration_allocation_ms(queue)
        return allocation is not None and self.queue_duration_consumed_ms(queue) >= allocation

    def stop_reason(self, *, now: datetime | None = None) -> ExplorationStopReason:
        if self.total_consumed_actions >= self.max_actions:
            return ExplorationStopReason.ACTION_BUDGET_EXHAUSTED
        if self.deadline is not None:
            current = now or datetime.now(UTC)
            require_aware_datetime(current, "now")
            if current >= self.deadline:
                return ExplorationStopReason.DEADLINE_EXPIRED
        if (
            self.max_duration_ms is not None
            and self.total_consumed_duration_ms >= self.max_duration_ms
        ):
            return ExplorationStopReason.DURATION_BUDGET_EXHAUSTED
        if all(self.queue_retry_budget_exhausted(queue) for queue in _canonical_queues()):
            return ExplorationStopReason.RETRY_BUDGET_EXHAUSTED
        if self.page_repeat_count >= self.policy.repeat_limit:
            return ExplorationStopReason.REPEATED_PAGE_LIMIT
        if (
            not self.suppress_popup_loop_stop
            and self.popup_repeat_count >= self.policy.repeat_limit
        ):
            return ExplorationStopReason.POPUP_LOOP_LIMIT
        if self.return_repeat_count >= self.policy.repeat_limit:
            return ExplorationStopReason.RETURN_LOOP_LIMIT
        # 弹层策略即使要求暂时抑制 popup-loop，也不能绕过连续无进展的收口。
        if self.no_progress_count >= self.policy.no_progress_limit:
            return ExplorationStopReason.NO_PROGRESS_LIMIT
        if self.repeat_count >= self.policy.repeat_limit:
            return ExplorationStopReason.REPEAT_LIMIT
        return ExplorationStopReason.NONE

    def advance(
        self,
        scheduler: BoundedExplorationScheduler,
        candidate: CandidateAction,
        *,
        missing_page_types: Collection[str] = (),
        action_claimed: bool,
        retryable_failure: bool = False,
        progressed: bool = False,
        state_fingerprint: str | None = None,
        page_fingerprint: str | None = None,
        popup_fingerprint: str | None = None,
        return_fingerprint: str | None = None,
        elapsed_duration_ms: int = 0,
        other_candidates_available: bool = True,
        suppress_popup_loop_stop: bool | None = None,
    ) -> BoundedExplorationState:
        """按实际结算结果累计所属队列，必要时记录额度借用。"""

        _non_negative_int(elapsed_duration_ms, "elapsed_duration_ms")
        if self.max_duration_ms is not None:
            elapsed_duration_ms = min(
                elapsed_duration_ms,
                max(self.max_duration_ms - self.total_consumed_duration_ms, 0),
            )
        queue = scheduler.classify(
            candidate,
            required_page_types=self.required_page_types,
            evidence_gap_page_types=self.evidence_gap_page_types,
            missing_page_types=missing_page_types,
        )
        consumed = dict(self.consumed_actions)
        consumed_duration = dict(self.consumed_duration_ms)
        retries = dict(self.retry_attempts)
        candidate_retries = dict(self.retry_attempts_by_candidate)
        borrowed = dict(self.borrowed_actions)
        borrowed_duration = dict(self.borrowed_duration_ms)
        if action_claimed:
            consumed[queue.value] = consumed.get(queue.value, 0) + 1
            if self.queue_budget_exhausted(queue) and other_candidates_available:
                borrowed[queue.value] = borrowed.get(queue.value, 0) + 1
        if elapsed_duration_ms:
            consumed_duration[queue.value] = (
                consumed_duration.get(queue.value, 0) + elapsed_duration_ms
            )
            if self.queue_time_budget_exhausted(queue) and other_candidates_available:
                borrowed_duration[queue.value] = (
                    borrowed_duration.get(queue.value, 0) + elapsed_duration_ms
                )
        if retryable_failure:
            retries[queue.value] = retries.get(queue.value, 0) + 1
            identity = _candidate_retry_identity(candidate)
            candidate_retries[identity] = candidate_retries.get(identity, 0) + 1
        no_progress = 0 if progressed else self.no_progress_count + 1
        repeat = _next_repeat_count(
            current=state_fingerprint,
            previous=self.last_state_fingerprint,
            previous_count=self.repeat_count,
        )
        page_repeat = _next_page_repeat_count(
            current=page_fingerprint,
            previous=self.last_page_fingerprint,
            previous_count=self.page_repeat_count,
            current_state=state_fingerprint,
            previous_state=self.last_state_fingerprint,
        )
        # 底栏硬覆盖动作不累计弹层循环，避免未清层切栏仍触发整 run 收口
        bottom_tab_seek = candidate.metadata.get("bottom_navigation_tab_seek") is True
        effective_popup_fingerprint = None if bottom_tab_seek else popup_fingerprint
        if bottom_tab_seek:
            popup_repeat = 0
            last_popup = None
        else:
            popup_repeat = _next_repeat_count(
                current=effective_popup_fingerprint,
                previous=self.last_popup_fingerprint,
                previous_count=self.popup_repeat_count,
            )
            last_popup = effective_popup_fingerprint
        return_history = _next_signature_history(
            ()
            if progressed and return_fingerprint is None
            else self.return_fingerprint_history,
            return_fingerprint,
            max_length=max(8, self.policy.repeat_limit * 4),
        )
        return_repeat = max(
            _next_repeat_count(
                current=return_fingerprint,
                previous=self.last_return_fingerprint,
                previous_count=self.return_repeat_count,
            ),
            _return_cycle_repeat_count(return_history),
        )
        next_suppress = (
            self.suppress_popup_loop_stop
            if suppress_popup_loop_stop is None
            else bool(suppress_popup_loop_stop)
        )
        return replace(
            self,
            consumed_actions=consumed,
            consumed_duration_ms=consumed_duration,
            retry_attempts=retries,
            retry_attempts_by_candidate=candidate_retries,
            borrowed_actions=borrowed,
            borrowed_duration_ms=borrowed_duration,
            no_progress_count=no_progress,
            repeat_count=repeat,
            last_state_fingerprint=state_fingerprint,
            last_page_fingerprint=page_fingerprint,
            page_repeat_count=page_repeat,
            last_popup_fingerprint=last_popup,
            popup_repeat_count=popup_repeat,
            suppress_popup_loop_stop=next_suppress,
            last_return_fingerprint=return_fingerprint,
            return_repeat_count=return_repeat,
            return_fingerprint_history=return_history,
        )

    def advance_observation(
        self,
        *,
        queue: ExplorationQueue,
        elapsed_duration_ms: int = 0,
        progressed: bool = True,
        state_fingerprint: str | None = None,
        page_fingerprint: str | None = None,
        popup_fingerprint: str | None = None,
        return_fingerprint: str | None = None,
        suppress_popup_loop_stop: bool | None = None,
    ) -> BoundedExplorationState:
        """结算没有候选动作的观察轮，只计时间和页面/弹窗/返回循环。"""

        if not isinstance(queue, ExplorationQueue):
            raise TypeError("queue 必须为 ExplorationQueue")
        _non_negative_int(elapsed_duration_ms, "elapsed_duration_ms")
        if self.max_duration_ms is not None:
            elapsed_duration_ms = min(
                elapsed_duration_ms,
                max(self.max_duration_ms - self.total_consumed_duration_ms, 0),
            )
        consumed_duration = dict(self.consumed_duration_ms)
        if elapsed_duration_ms:
            consumed_duration[queue.value] = (
                consumed_duration.get(queue.value, 0) + elapsed_duration_ms
            )
        return_history = _next_signature_history(
            ()
            if progressed and return_fingerprint is None
            else self.return_fingerprint_history,
            return_fingerprint,
            max_length=max(8, self.policy.repeat_limit * 4),
        )
        next_suppress = (
            self.suppress_popup_loop_stop
            if suppress_popup_loop_stop is None
            else bool(suppress_popup_loop_stop)
        )
        return replace(
            self,
            consumed_duration_ms=consumed_duration,
            no_progress_count=0 if progressed else self.no_progress_count + 1,
            repeat_count=_next_repeat_count(
                current=state_fingerprint,
                previous=self.last_state_fingerprint,
                previous_count=self.repeat_count,
            ),
            last_state_fingerprint=state_fingerprint,
            page_repeat_count=_next_page_repeat_count(
                current=page_fingerprint,
                previous=self.last_page_fingerprint,
                previous_count=self.page_repeat_count,
                current_state=state_fingerprint,
                previous_state=self.last_state_fingerprint,
            ),
            last_page_fingerprint=page_fingerprint,
            popup_repeat_count=_next_repeat_count(
                current=popup_fingerprint,
                previous=self.last_popup_fingerprint,
                previous_count=self.popup_repeat_count,
            ),
            last_popup_fingerprint=popup_fingerprint,
            suppress_popup_loop_stop=next_suppress,
            return_repeat_count=max(
                _next_repeat_count(
                    current=return_fingerprint,
                    previous=self.last_return_fingerprint,
                    previous_count=self.return_repeat_count,
                ),
                _return_cycle_repeat_count(return_history),
            ),
            last_return_fingerprint=return_fingerprint,
            return_fingerprint_history=return_history,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "policy_version": self.policy.policy_version,
            "policy_hash": self.policy.policy_hash,
            "max_actions": self.max_actions,
            "max_duration_ms": self.max_duration_ms,
            "deadline": self.deadline.isoformat() if self.deadline is not None else None,
            "consumed_actions": dict(self.consumed_actions),
            "consumed_duration_ms": dict(self.consumed_duration_ms),
            "retry_attempts": dict(self.retry_attempts),
            "retry_attempts_by_candidate": dict(self.retry_attempts_by_candidate),
            "borrowed_actions": dict(self.borrowed_actions),
            "borrowed_duration_ms": dict(self.borrowed_duration_ms),
            "retry_budgets": {
                queue.value: self.queue_retry_budget(queue)
                for queue in _canonical_queues()
            },
            "required_page_types": list(self.required_page_types),
            "evidence_gap_page_types": list(self.evidence_gap_page_types),
            "no_progress_count": self.no_progress_count,
            "repeat_count": self.repeat_count,
            "last_state_fingerprint": self.last_state_fingerprint,
            "last_page_fingerprint": self.last_page_fingerprint,
            "page_repeat_count": self.page_repeat_count,
            "last_popup_fingerprint": self.last_popup_fingerprint,
            "popup_repeat_count": self.popup_repeat_count,
            "suppress_popup_loop_stop": self.suppress_popup_loop_stop,
            "last_return_fingerprint": self.last_return_fingerprint,
            "return_repeat_count": self.return_repeat_count,
            "return_fingerprint_history": list(self.return_fingerprint_history),
        }

    def with_metadata(self, metadata: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(metadata)
        result[BOUNDED_EXPLORATION_STATE_KEY] = self.to_dict()
        return result


class BoundedExplorationScheduler:
    """H5/App 共用的候选队列归类和确定性排序器。"""

    def __init__(self, *, policy: BoundedExplorationPolicy | None = None) -> None:
        self.policy = policy or BoundedExplorationPolicy()

    def classify(
        self,
        candidate: CandidateAction,
        *,
        required_page_types: Collection[str] = (),
        evidence_gap_page_types: Collection[str] = (),
        missing_page_types: Collection[str] = (),
    ) -> ExplorationQueue:
        """按系统冻结的必检集合和证据缺口事实归类候选。

        ``required_page_types`` 和 ``evidence_gap_page_types`` 必须来自 Run/
        Frontier 的持久化事实；候选里的 priority、scheduler_queue 等自报字段
        一律不参与分类。``missing_page_types`` 是旧调用方的兼容别名，按证据
        缺口处理。
        """

        if not isinstance(candidate, CandidateAction):
            raise TypeError("candidate 必须为 CandidateAction")
        if _is_recovery_candidate(candidate):
            return ExplorationQueue.REQUIRED
        # playbook 底栏硬覆盖（含无页型映射的 AI科技）按必检领取，避免掉进 DISCOVERY。
        if candidate.metadata.get("bottom_navigation_tab_seek") is True:
            return ExplorationQueue.REQUIRED
        expected = _expected_page_type(candidate)
        required = _normalized_text_set(required_page_types)
        missing = _normalized_text_set(missing_page_types)
        gaps = _normalized_text_set(evidence_gap_page_types)
        # 仍未覆盖的必检页型必须先完成必检；已经覆盖但证据被现有事实标成
        # 缺口时，不能因为 required 集合包含全量页型而永远压住补采队列。
        if expected is not None and expected in required and (
            expected in missing or expected not in gaps
        ):
            return ExplorationQueue.REQUIRED
        if expected is not None and (expected in gaps or expected in missing):
            return ExplorationQueue.EVIDENCE_GAP
        if expected is not None and expected in required:
            return ExplorationQueue.REQUIRED
        return ExplorationQueue.DISCOVERY

    def coverage_rank(
        self,
        candidate: CandidateAction,
        *,
        required_page_types: Collection[str] = (),
        evidence_gap_page_types: Collection[str] = (),
        missing_page_types: Collection[str] = (),
    ) -> int:
        """返回覆盖优先级：必检页型高于已记录证据缺口。"""

        queue = self.classify(
            candidate,
            required_page_types=required_page_types,
            evidence_gap_page_types=evidence_gap_page_types,
            missing_page_types=missing_page_types,
        )
        return {
            ExplorationQueue.REQUIRED: 0,
            ExplorationQueue.EVIDENCE_GAP: 1,
            ExplorationQueue.DISCOVERY: 2,
        }[queue]

    def queue_rank(
        self,
        candidate: CandidateAction,
        *,
        required_page_types: Collection[str] = (),
        evidence_gap_page_types: Collection[str] = (),
        missing_page_types: Collection[str] = (),
    ) -> int:
        """返回统一队列优先级：清障/恢复、覆盖缺口、自由发现。"""

        queue = self.classify(
            candidate,
            required_page_types=required_page_types,
            evidence_gap_page_types=evidence_gap_page_types,
            missing_page_types=missing_page_types,
        )
        return {
            ExplorationQueue.REQUIRED: 0,
            ExplorationQueue.EVIDENCE_GAP: 1,
            ExplorationQueue.DISCOVERY: 2,
        }[queue]

    def selection_key(
        self,
        candidate: CandidateAction,
        *,
        required_page_types: Collection[str] = (),
        evidence_gap_page_types: Collection[str] = (),
        missing_page_types: Collection[str] = (),
        index: int = 0,
    ) -> tuple[int, int, int, int, str]:
        """给不同平台 selector 使用的共用排序片段。"""

        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ValueError("index 必须为非负整数")
        return (
            self.queue_rank(
                candidate,
                required_page_types=required_page_types,
                evidence_gap_page_types=evidence_gap_page_types,
                missing_page_types=missing_page_types,
            ),
            0 if candidate.risk_level.value == "low" else 1,
            -candidate.exploration_value,
            candidate.prior_failure_count,
            f"{index:08d}:{candidate.candidate_id}",
        )

    def filter_candidates(
        self,
        candidates: Sequence[CandidateAction],
        *,
        required_page_types: Collection[str] | None = None,
        evidence_gap_page_types: Collection[str] | None = None,
        missing_page_types: Collection[str] = (),
        state: BoundedExplorationState | Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> tuple[CandidateAction, ...]:
        """在队列额度仍有其它可选项时让出已用尽的队列。

        如果当前只剩一个队列，允许借用空闲额度；实际借用会在 ``state.advance``
        中留下记录。这样不会因为一条队列为空而把任务卡死，同时仍保持每类
        预算和借用行为可审计。
        """

        values = tuple(candidates)
        if any(not isinstance(candidate, CandidateAction) for candidate in values):
            raise TypeError("candidates 必须由 CandidateAction 组成")
        if state is None or not values:
            return values
        if isinstance(state, Mapping):
            raw_state = state.get(BOUNDED_EXPLORATION_STATE_KEY, state)
            if not isinstance(raw_state, Mapping):
                raise TypeError("scheduler state 必须为对象")
            raw_max = raw_state.get("max_actions")
            if isinstance(raw_max, bool) or not isinstance(raw_max, int):
                raise ValueError("scheduler state 缺少有效 max_actions")
            state = BoundedExplorationState.from_dict(
                raw_state,
                default_max_actions=raw_max,
            )
        required = (
            state.required_page_types
            if required_page_types is None
            else tuple(required_page_types)
        )
        evidence_gaps = (
            state.evidence_gap_page_types
            if evidence_gap_page_types is None
            else tuple(evidence_gap_page_types)
        )
        if state.stop_reason(now=now) is not ExplorationStopReason.NONE:
            return ()

        def eligible(candidate: CandidateAction) -> bool:
            queue = self.classify(
                candidate,
                required_page_types=required,
                evidence_gap_page_types=evidence_gaps,
                missing_page_types=missing_page_types,
            )
            retry_exhausted = state.queue_retry_budget_exhausted(queue)
            if _is_recovery_candidate(candidate):
                retry_exhausted = state.candidate_retry_budget_exhausted(candidate)
            return not (
                state.queue_budget_exhausted(queue)
                or state.queue_time_budget_exhausted(queue)
                or retry_exhausted
            )

        available = tuple(
            candidate for candidate in values if eligible(candidate)
        )
        if available:
            return available
        # 只在其它类别无候选时允许借用动作/时间额度；已经触达本类别重试
        # 上限的候选不再借用，保证失败动作不会把探索拖成死循环。
        retry_available = tuple(
            candidate
            for candidate in values
            if not (
                state.candidate_retry_budget_exhausted(candidate)
                if _is_recovery_candidate(candidate)
                else state.queue_retry_budget_exhausted(
                    self.classify(
                        candidate,
                        required_page_types=required,
                        evidence_gap_page_types=evidence_gaps,
                        missing_page_types=missing_page_types,
                    )
                )
            )
        )
        return retry_available

    def select(
        self,
        candidates: Sequence[CandidateAction],
        *,
        required_page_types: Collection[str] | None = None,
        evidence_gap_page_types: Collection[str] | None = None,
        missing_page_types: Collection[str] = (),
        state: BoundedExplorationState | Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> CandidateAction | None:
        """提供不依赖平台的正式候选选择，供两端 selector 和测试复用。"""

        filtered = self.filter_candidates(
            candidates,
            required_page_types=required_page_types,
            evidence_gap_page_types=evidence_gap_page_types,
            missing_page_types=missing_page_types,
            state=state,
            now=now,
        )
        if not filtered:
            return None
        selection_required = (
            required_page_types
            if required_page_types is not None
            else _state_page_types(state, "required_page_types")
        )
        selection_evidence_gaps = (
            evidence_gap_page_types
            if evidence_gap_page_types is not None
            else _state_page_types(state, "evidence_gap_page_types")
        )
        return min(
            enumerate(filtered),
            key=lambda pair: self.selection_key(
                pair[1],
                required_page_types=selection_required,
                evidence_gap_page_types=selection_evidence_gaps,
                missing_page_types=missing_page_types,
                index=pair[0],
            ),
        )[1]


def _is_recovery_candidate(candidate: CandidateAction) -> bool:
    metadata = candidate.metadata
    if metadata.get("app_frontier_restore_only") is True:
        return True
    if metadata.get("recovery_only") is True or metadata.get("recovery") is True:
        return True
    if metadata.get("coverage_page_seek_contract") is not None:
        return True
    control_role = metadata.get("control_role")
    if isinstance(control_role, str) and control_role.strip().casefold() in {
        "dismiss",
        "skip",
        "restore",
        "reobserve",
    }:
        return True
    parameters = metadata.get("action_parameters")
    if not isinstance(parameters, Mapping):
        return False
    target_mode = parameters.get("target_mode")
    capability_key = {
        "popup_dismissal": "popup_dismissal_capability",
        "startup_ad_dismissal": "startup_ad_capability",
    }.get(target_mode)
    return bool(
        capability_key is not None
        and candidate.action_type.value == "click"
        and candidate.risk_level.value == "low"
        and candidate.navigation_depth == 0
        and isinstance(parameters.get(capability_key), str)
        and parameters[capability_key].strip()
        and isinstance(parameters.get("obstruction_signature"), str)
        and parameters["obstruction_signature"].strip()
    )


def _candidate_retry_identity(candidate: CandidateAction) -> str:
    if not _is_recovery_candidate(candidate):
        return candidate.candidate_id
    parameters = candidate.metadata.get("action_parameters")
    if not isinstance(parameters, Mapping):
        return candidate.candidate_id
    return stable_protocol_hash(
        {
            "action_type": candidate.action_type.value,
            "semantic_target": candidate.semantic_target,
            "target_mode": parameters.get("target_mode"),
            "obstruction_signature": parameters.get("obstruction_signature"),
        }
    )


def _state_page_types(
    state: BoundedExplorationState | Mapping[str, Any] | None,
    field_name: str,
) -> Collection[str]:
    if isinstance(state, BoundedExplorationState):
        return getattr(state, field_name)
    if isinstance(state, Mapping):
        raw = state.get(BOUNDED_EXPLORATION_STATE_KEY, state)
        if isinstance(raw, Mapping):
            value = raw.get(field_name)
            if isinstance(value, (list, tuple, set, frozenset)):
                return tuple(value)
    return ()


def scheduler_state_from_work(work: ExplorationWork) -> BoundedExplorationState | None:
    """读取已持久化的调度账本；旧 Work 无账本时仍由原预算约束。"""

    metadata = work.frontier.observation.metadata
    if BOUNDED_EXPLORATION_STATE_KEY not in metadata:
        return None
    raw = metadata[BOUNDED_EXPLORATION_STATE_KEY]
    if not isinstance(raw, Mapping):
        raise ValueError("Work 调度账本必须为对象，不能静默重置")
    return BoundedExplorationState.from_dict(
        raw,
        default_max_actions=work.budget.max_actions,
        default_max_duration_ms=work.budget.remaining_duration_ms,
        default_deadline=work.deadline,
    )


def scheduler_missing_page_types(
    work: ExplorationWork, state: BoundedExplorationState,
) -> frozenset[str]:
    """使用本 Work 的覆盖事实区分尚未到达与已经到达但缺证的必检页。"""

    metadata = work.frontier.observation.metadata
    coverage = metadata.get(H5_COVERAGE_METADATA_KEY)
    source = coverage if isinstance(coverage, Mapping) else metadata
    raw = source.get("covered_page_types", ())
    covered = _normalized_text_set(raw) if isinstance(raw, (list, tuple, set, frozenset)) else set()
    return frozenset(state.required_page_types) - covered


def scheduler_state_from_metadata(
    metadata: Mapping[str, Any] | None,
    *,
    max_actions: int,
    max_duration_ms: int | None = None,
    deadline: datetime | None = None,
    policy: BoundedExplorationPolicy | None = None,
    required_page_types: Collection[str] | None = None,
    evidence_gap_page_types: Collection[str] | None = None,
) -> BoundedExplorationState:
    """从既有 Frontier 元数据恢复调度状态，供 H5/App 后继 Work 使用。"""

    raw = metadata.get(BOUNDED_EXPLORATION_STATE_KEY) if isinstance(metadata, Mapping) else None
    if isinstance(raw, Mapping):
        return BoundedExplorationState.from_dict(
            raw,
            default_max_actions=max_actions,
            default_max_duration_ms=max_duration_ms,
            default_deadline=deadline,
            policy=policy,
            required_page_types=required_page_types,
            evidence_gap_page_types=evidence_gap_page_types,
        )
    return BoundedExplorationState.create(
        max_actions=max_actions,
        max_duration_ms=max_duration_ms,
        deadline=deadline,
        policy=policy,
        required_page_types=required_page_types or (),
        evidence_gap_page_types=evidence_gap_page_types or (),
    )


def update_scheduler_metadata(
    metadata: Mapping[str, Any] | None,
    *,
    scheduler: BoundedExplorationScheduler,
    max_actions: int,
    max_duration_ms: int | None = None,
    deadline: datetime | None = None,
    candidate: CandidateAction | None = None,
    required_page_types: Collection[str] | None = None,
    evidence_gap_page_types: Collection[str] | None = None,
    missing_page_types: Collection[str] = (),
    action_claimed: bool = False,
    retryable_failure: bool = False,
    progressed: bool = True,
    state_fingerprint: str | None = None,
    page_fingerprint: str | None = None,
    popup_fingerprint: str | None = None,
    return_fingerprint: str | None = None,
    elapsed_duration_ms: int = 0,
    elapsed_queue: ExplorationQueue | None = None,
    other_candidates_available: bool = True,
    now: datetime | None = None,
    suppress_popup_loop_stop: bool | None = None,
) -> dict[str, Any]:
    """在不改变既有 payload 形状的前提下追加/更新调度摘要。"""

    source = metadata if isinstance(metadata, Mapping) else {}
    state = scheduler_state_from_metadata(
        source,
        max_actions=max_actions,
        max_duration_ms=max_duration_ms,
        deadline=deadline,
        policy=scheduler.policy,
        required_page_types=required_page_types,
        evidence_gap_page_types=evidence_gap_page_types,
    )
    if suppress_popup_loop_stop is not None:
        state = replace(state, suppress_popup_loop_stop=bool(suppress_popup_loop_stop))
    stop_reason = state.stop_reason(now=now)
    # 截止时间只阻止启动新操作；截止前已派发的动作或观察仍须结算。
    # 其余已耗尽预算和循环终态保持关闭，不能因补记结果重置后重新选路。
    can_settle = stop_reason is ExplorationStopReason.NONE or (
        stop_reason is ExplorationStopReason.DEADLINE_EXPIRED
        and replace(state, deadline=None).stop_reason(now=now) is ExplorationStopReason.NONE
    )
    if candidate is not None and can_settle:
        state = state.advance(
            scheduler,
            candidate,
            missing_page_types=missing_page_types,
            action_claimed=action_claimed,
            retryable_failure=retryable_failure,
            progressed=progressed,
            state_fingerprint=state_fingerprint,
            page_fingerprint=page_fingerprint,
            popup_fingerprint=popup_fingerprint,
            return_fingerprint=return_fingerprint,
            elapsed_duration_ms=elapsed_duration_ms,
            other_candidates_available=other_candidates_available,
            suppress_popup_loop_stop=suppress_popup_loop_stop,
        )
    elif (
        can_settle
        and (
            elapsed_duration_ms
            or any(
                value is not None
                for value in (
                    state_fingerprint,
                    page_fingerprint,
                    popup_fingerprint,
                    return_fingerprint,
                )
            )
        )
    ):
        state = state.advance_observation(
            queue=elapsed_queue or ExplorationQueue.DISCOVERY,
            elapsed_duration_ms=elapsed_duration_ms,
            progressed=progressed,
            state_fingerprint=state_fingerprint,
            page_fingerprint=page_fingerprint,
            popup_fingerprint=popup_fingerprint,
            return_fingerprint=return_fingerprint,
            suppress_popup_loop_stop=suppress_popup_loop_stop,
        )
    return state.with_metadata(source)


def _expected_page_type(candidate: CandidateAction) -> str | None:
    for key in ("expected_next_page_type", "expected_state_hint", "target_page_type"):
        value = candidate.metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().casefold()
    return None


def _normalized_text_set(values: Collection[str]) -> frozenset[str]:
    if isinstance(values, (str, bytes)):
        raise TypeError("missing_page_types 必须为字符串集合")
    normalized: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("missing_page_types 只能包含非空字符串")
        normalized.add(value.strip().casefold())
    return frozenset(normalized)


def _normalized_text_tuple(values: Collection[str] | None, field_name: str) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, (str, bytes)):
        raise TypeError(f"{field_name} 必须为字符串集合")
    normalized = _normalized_text_set(values)
    return tuple(sorted(normalized))


def _normalized_signature_tuple(values: Collection[str] | None) -> tuple[str, ...]:
    """校验签名历史但保留签名原文，供循环检测和审计重放使用。"""

    if values is None:
        return ()
    if isinstance(values, (str, bytes)):
        raise TypeError("签名历史必须为字符串序列")
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise ValueError("签名历史只能包含无首尾空白的非空字符串")
        result.append(value)
    return tuple(result)


def _canonical_queues() -> tuple[ExplorationQueue, ...]:
    return (
        ExplorationQueue.REQUIRED,
        ExplorationQueue.EVIDENCE_GAP,
        ExplorationQueue.DISCOVERY,
    )


def _next_repeat_count(
    *,
    current: str | None,
    previous: str | None,
    previous_count: int,
) -> int:
    """只累计相邻且同一签名，签名变化即开始新的循环窗口。"""

    if current is not None and current == previous:
        return previous_count + 1
    return 0


def _next_page_repeat_count(
    *,
    current: str | None,
    previous: str | None,
    previous_count: int,
    current_state: str | None,
    previous_state: str | None,
) -> int:
    """同一页面的重复只在状态也未变化时累计。

    滚动、Tab 切换等合法同页状态通常会改变视觉/页面状态签名；即便页面身份
    不变，也不能因为连续采集到同页身份就被重复页止损。没有状态签名时仍保留
    旧的相邻重复语义，兼容历史调用方。
    """

    if current is None or current != previous:
        return 0
    if (
        current_state is not None
        and previous_state is not None
        and current_state != previous_state
    ):
        return 0
    return previous_count + 1


def _next_signature_history(
    history: Collection[str],
    current: str | None,
    *,
    max_length: int,
) -> tuple[str, ...]:
    if current is None:
        # 无进展时保留已有周期窗口；真实进展由调用方传入空历史清理窗口。
        return tuple(history)
    if not isinstance(current, str) or not current.strip() or current != current.strip():
        raise ValueError("当前签名必须为无首尾空白的非空字符串")
    values = (*history, current)
    return tuple(values[-max_length:])


def _return_cycle_repeat_count(history: Sequence[str]) -> int:
    """返回非相邻返回循环已重复的次数（仅检测长度至少为 2 的周期）。"""

    if len(history) < 4:
        return 0
    # 优先选择最短周期，这样 A-B-A-B-A-B 会按两个完整循环累计，而不是
    # 被更长的偶然周期吞掉。相邻同签名由 _next_repeat_count 独立处理。
    for period in range(2, len(history) // 2 + 1):
        last_block = tuple(history[-period:])
        block_count = 1
        cursor = len(history) - period
        while cursor - period >= 0:
            previous_block = tuple(history[cursor - period : cursor])
            if previous_block != last_block:
                break
            block_count += 1
            cursor -= period
        if block_count >= 2:
            return block_count - 1
    return 0


def _queue_counter(value: Mapping[str, int] | None, field_name: str) -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} 必须为对象")
    result: dict[str, int] = {}
    allowed = {item.value for item in _canonical_queues()}
    for raw_queue, raw_count in value.items():
        if not isinstance(raw_queue, str):
            raise ValueError(f"{field_name} 包含不支持的队列")
        if raw_queue not in allowed:
            raise ValueError(f"{field_name} 包含不支持的队列")
        _non_negative_int(raw_count, f"{field_name}.{raw_queue}")
        result[raw_queue] = result.get(raw_queue, 0) + int(raw_count)
    return result


def _candidate_retry_counter(
    value: Mapping[str, int] | None,
    *,
    max_entries: int,
) -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("retry_attempts_by_candidate 必须为对象")
    if len(value) > max_entries:
        raise ValueError("候选重试账本不能超过总动作预算")
    result: dict[str, int] = {}
    for raw_identity, raw_count in value.items():
        if (
            not isinstance(raw_identity, str)
            or not raw_identity.strip()
            or raw_identity != raw_identity.strip()
        ):
            raise ValueError("候选重试账本包含无效身份")
        _non_negative_int(
            raw_count,
            f"retry_attempts_by_candidate.{raw_identity}",
        )
        result[raw_identity] = raw_count
    return result


def _parse_optional_deadline(
    value: object,
    default: datetime | None,
) -> datetime | None:
    if value is None:
        return default
    if isinstance(value, datetime):
        return require_aware_datetime(value, "deadline")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("deadline 必须为 ISO 8601 时间或空")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("deadline 不是有效 ISO 8601 时间") from exc
    return require_aware_datetime(parsed, "deadline")


def _non_negative_int(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} 必须为非负整数")


# 这些键对应已经由 H5 Coverage、App Evidence Index 或 Rule Judge 写入的事实。
# ``evidence_gap_page_types`` 等调度草稿键故意不放进来，避免调度器把自己写
# 出来的摘要当成缺口来源，形成永远不为空的自循环。
_EVIDENCE_FACT_ROOT_KEYS = frozenset(
    {
        "h5_coverage_snapshot",
        "h5_coverage_snapshot_fact",
        "coverage_snapshot",
        "coverage_summary",
        "app_exploration_result",
        "app_evidence_index",
        "evidence_index",
        "evidence_index_reference",
        "evidence_index_payload",
        "rule_judge_output",
        "rule_evidence_requests",
        "app_rule_inventory_coverage",
        "judge_output",
        "evidence",
        "evidence_gap",
        "evidence_gaps",
        "missing_evidence",
    }
)
_EVIDENCE_GAP_FACT_KEYS = frozenset(
    {
        "evidence_gap",
        "evidence_gaps",
        "missing_evidence",
        "missing_page_types",
        "gaps",
        "evidence_requests",
        "rule_evidence_requests",
    }
)
_EVIDENCE_GAP_STATUSES = frozenset(
    {
        "incomplete",
        "evidence_insufficient",
        "capture_missing",
        "pending",
        "missing",
        "failed",
    }
)
_PAGE_TYPE_KEYS = frozenset(
    {
        "page_type",
        "target_page_type",
        "expected_page_type",
        "page_types",
        "target_page_types",
    }
)
_IGNORED_SCHEDULER_FACT_KEYS = frozenset(
    {
        "evidence_gap_page_types",
        "missing_evidence_page_types",
        "pending_evidence_page_types",
    }
)


def evidence_gap_page_types_from_facts(
    metadata: Mapping[str, Any] | None,
    *,
    known_page_types: Collection[str] = (),
) -> tuple[str, ...]:
    """从已有 Coverage/证据/规则事实投影页面证据缺口。

    页面探索过程中不能依赖调度器自己生成的 ``*_page_types`` 摘要；只有
    Coverage scope 的不完整状态、Evidence Gap 条目的显式 page_type，或规则
    输出中恰好匹配任务页型的 missing_evidence，才会进入结果。这样即使本轮
    没有新的 writer，缺口队列也不会被一个永远不存在的元数据键伪造出来。
    """

    if not isinstance(metadata, Mapping):
        return ()
    known = {
        value.strip().casefold()
        for value in known_page_types
        if isinstance(value, str) and value.strip()
    }
    found: set[str] = set()
    for key, payload in metadata.items():
        if not isinstance(key, str):
            continue
        normalized_key = key.strip().casefold()
        if normalized_key in _IGNORED_SCHEDULER_FACT_KEYS:
            continue
        if normalized_key in _EVIDENCE_FACT_ROOT_KEYS:
            _collect_evidence_gap_facts(
                payload,
                found=found,
                known_page_types=known,
                in_gap=normalized_key in _EVIDENCE_GAP_FACT_KEYS,
            )
    return tuple(sorted(found))


def _collect_evidence_gap_facts(
    payload: object,
    *,
    found: set[str],
    known_page_types: set[str],
    in_gap: bool,
    depth: int = 0,
) -> None:
    if depth > 8:
        return
    if isinstance(payload, str):
        normalized = payload.strip().casefold()
        if in_gap and normalized and (not known_page_types or normalized in known_page_types):
            found.add(normalized)
        return
    if isinstance(payload, Mapping):
        status_values = (
            payload.get("assessment_status"),
            payload.get("status"),
            payload.get("outcome"),
        )
        has_gap_status = any(
            isinstance(value, str) and value.strip().casefold() in _EVIDENCE_GAP_STATUSES
            for value in status_values
        )
        for key, value in payload.items():
            if not isinstance(key, str):
                continue
            normalized_key = key.strip().casefold()
            if normalized_key in _IGNORED_SCHEDULER_FACT_KEYS:
                continue
            if normalized_key in _PAGE_TYPE_KEYS and (in_gap or has_gap_status):
                _collect_page_type_values(
                    value,
                    found=found,
                    known_page_types=known_page_types,
                )
                continue
            nested_gap = in_gap or normalized_key in _EVIDENCE_GAP_FACT_KEYS
            _collect_evidence_gap_facts(
                value,
                found=found,
                known_page_types=known_page_types,
                in_gap=nested_gap,
                depth=depth + 1,
            )
        return
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes, bytearray)):
        for item in payload:
            _collect_evidence_gap_facts(
                item,
                found=found,
                known_page_types=known_page_types,
                in_gap=in_gap,
                depth=depth + 1,
            )


def _collect_page_type_values(
    value: object,
    *,
    found: set[str],
    known_page_types: set[str],
) -> None:
    values = (
        value
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))
        else (value,)
    )
    for item in values:
        if not isinstance(item, str) or not item.strip():
            continue
        normalized = item.strip().casefold()
        if not known_page_types or normalized in known_page_types:
            found.add(normalized)


__all__ = [
    "BOUNDED_EXPLORATION_POLICY_VERSION",
    "BOUNDED_EXPLORATION_STATE_KEY",
    "BOUNDED_EXPLORATION_STATE_VERSION",
    "BoundedExplorationPolicy",
    "BoundedExplorationScheduler",
    "BoundedExplorationState",
    "ExplorationQueue",
    "ExplorationStopReason",
    "evidence_gap_page_types_from_facts",
    "scheduler_missing_page_types",
    "scheduler_state_from_metadata",
    "scheduler_state_from_work",
    "update_scheduler_metadata",
]
