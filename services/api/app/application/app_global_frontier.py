"""把截图局部候选合并为 App Run 的全局 Frontier。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime

from services.api.app.application.app_coverage_page_seek import (
    missing_required_page_tab_boost,
    missing_required_page_tab_rank,
)
from services.api.app.application.app_exploration_flow_contract import (
    popup_phase_blocks_bottom_tabs,
)
from services.api.app.application.app_frontier_queue import (
    AppFrontierQueue,
    PublishAppFrontierCandidatesCommand,
)
from services.api.app.application.app_navigation_entry_provenance import (
    is_business_navigation_entry,
)
from services.api.app.application.app_package_boundary import (
    work_within_app_package_boundary,
)
from services.api.app.application.app_semantic_progress import (
    should_suppress_redundant_app_candidate,
)
from services.api.app.application.bounded_exploration_scheduler import (
    BoundedExplorationScheduler,
    evidence_gap_page_types_from_facts,
    scheduler_state_from_metadata,
    update_scheduler_metadata,
)
from services.api.app.application.evidence_presentation import derive_app_presentation
from services.api.app.domain.exploration import (
    AppFrontierCounts,
    AppFrontierItem,
    CandidateAction,
    ExplorationWork,
    FrontierSnapshot,
    build_app_frontier_candidate_fingerprint,
    build_app_frontier_item,
    with_app_frontier_fingerprint_strategy,
)
from services.api.app.domain.interaction import (
    InteractionOperation,
    InteractionRiskLevel,
)
from services.api.app.domain.page.registry import get_default_page_type_registry
from services.api.app.domain.protocol import stable_protocol_hash

APP_FRONTIER_RESTORATION_CANDIDATES = "app_frontier_restoration_candidates"
# 每个明确未覆盖页型只保留一条 pending，压低同型重复入口空转。
_GOAL_TARGET_PENDING_LIMIT = 1
_GOAL_UNKNOWN_PENDING_LIMIT = 4
_LOW_VALUE_NAVIGATION_MARKERS = ("search", "搜索", "查找")


class AppGlobalFrontierService:
    """登记全部安全 sibling，并把下一条跨页候选投影到串行 Work。"""

    def __init__(self, *, repository: AppFrontierQueue) -> None:
        self._repository = repository
        self._scheduler = BoundedExplorationScheduler()

    async def register_work_candidates(
        self,
        *,
        work: ExplorationWork,
        agent_run_id: str,
    ) -> tuple[AppFrontierItem, ...]:
        # 外链／浏览器截图上的业务点击不得进入任务级 Frontier，避免越界后 sibling 循环。
        if not work_within_app_package_boundary(work):
            return ()
        page_identity = _source_page_identity(work)
        navigation_path = _navigation_path(work)
        candidates = tuple(
            candidate
            for candidate in work.frontier.candidates
            if _is_global_candidate(candidate)
            and not should_suppress_redundant_app_candidate(
                candidate=candidate,
                coverage_metadata=work.frontier.observation.metadata,
            )
        )
        if work.app_goal is not None:
            candidates = _select_goal_frontier_candidates(
                work,
                agent_run_id=agent_run_id,
                candidates=candidates,
                pending=await self._list_pending(work=work, agent_run_id=agent_run_id),
            )
        items = _unique_frontier_items(
            build_app_frontier_item(
                task_id=work.task_id,
                agent_run_id=agent_run_id,
                session_id=work.session_id,
                source_work_id=work.work_id,
                source_capture_id=work.source_capture_id,
                source_page_identity=page_identity,
                candidate=with_app_frontier_fingerprint_strategy(candidate),
                surface_kind=work.surface_kind,
                navigation_path=navigation_path,
                priority=_candidate_priority(work, candidate),
                discovered_at=work.frontier.observation.observed_at,
                metadata={
                    "frontier_snapshot_id": work.frontier_snapshot_id,
                    "source_state_version": work.frontier.observation.state_version,
                },
            )
            for candidate in candidates
        )
        return await self._repository.publish_candidates(
            command=PublishAppFrontierCandidatesCommand(
                task_id=work.task_id,
                agent_run_id=agent_run_id,
                session_id=work.session_id,
                source_work_id=work.work_id,
                candidates=items,
            )
        )

    async def project_counts(
        self,
        *,
        work: ExplorationWork,
        agent_run_id: str,
    ) -> ExplorationWork:
        return _with_global_counts(
            work,
            await self.counts(work=work, agent_run_id=agent_run_id),
        )

    def retain_registered_candidates(
        self,
        *,
        work: ExplorationWork,
        registered: tuple[AppFrontierItem, ...],
    ) -> ExplorationWork:
        """过滤同一 Run 已终态的全局候选，并保留当前帧恢复锚点。"""

        registered_fingerprints = {item.candidate_fingerprint for item in registered}
        page_identity = _source_page_identity(work)
        restoration_candidates = tuple(
            candidate.to_dict()
            for candidate in work.frontier.candidates
            if _is_restoration_navigation_candidate(candidate)
        )
        candidates = tuple(
            candidate
            for candidate in work.frontier.candidates
            if candidate.metadata.get("coverage_page_seek_contract") is None
            and (
                not _is_global_candidate(candidate)
                or build_app_frontier_candidate_fingerprint(
                    source_page_identity=page_identity,
                    candidate=with_app_frontier_fingerprint_strategy(candidate),
                )
                in registered_fingerprints
            )
        )
        return replace(
            work,
            frontier=_replace_candidates(
                work.frontier,
                candidates=candidates,
                metadata={
                    APP_FRONTIER_RESTORATION_CANDIDATES: restoration_candidates,
                    **update_scheduler_metadata(
                        work.frontier.observation.metadata,
                        scheduler=self._scheduler,
                        max_actions=work.budget.max_actions,
                        max_duration_ms=work.budget.remaining_duration_ms,
                        deadline=work.deadline,
                        required_page_types=_missing_goal_page_types(work),
                        evidence_gap_page_types=_app_evidence_gap_page_types(
                            work.frontier.observation.metadata,
                            known_page_types=_goal_page_types(work)[0],
                        ),
                    ),
                },
            ),
        )

    async def next_pending(
        self,
        *,
        work: ExplorationWork,
        agent_run_id: str,
    ) -> AppFrontierItem | None:
        list_pending = getattr(self._repository, "list_pending", None)
        if not callable(list_pending):
            return await self._repository.next_pending(
                task_id=work.task_id,
                agent_run_id=agent_run_id,
                session_id=work.session_id,
            )
        pending = await list_pending(
            task_id=work.task_id,
            agent_run_id=agent_run_id,
            session_id=work.session_id,
            limit=256,
        )
        if not pending:
            return None
        missing = _missing_goal_page_types(work)
        state = scheduler_state_from_metadata(
            work.frontier.observation.metadata,
            max_actions=work.budget.max_actions,
            max_duration_ms=work.budget.remaining_duration_ms,
            deadline=work.deadline,
            policy=self._scheduler.policy,
            required_page_types=_missing_goal_page_types(work),
            evidence_gap_page_types=_app_evidence_gap_page_types(
                work.frontier.observation.metadata,
                known_page_types=_goal_page_types(work)[0],
            ),
        )
        candidates = self._scheduler.filter_candidates(
            tuple(item.candidate for item in pending),
            required_page_types=state.required_page_types,
            evidence_gap_page_types=state.evidence_gap_page_types,
            missing_page_types=missing,
            state=state,
            now=work.frontier.observation.observed_at,
        )
        allowed_ids = {candidate.candidate_id for candidate in candidates}
        selectable = tuple(item for item in pending if item.candidate.candidate_id in allowed_ids)
        if not selectable:
            return None
        return min(
            selectable,
            key=lambda item: _pending_item_rank(
                work,
                item,
                scheduler=self._scheduler,
                required_page_types=state.required_page_types,
                evidence_gap_page_types=state.evidence_gap_page_types,
                missing_page_types=missing,
            ),
        )

    async def next_pending_for_restore(
        self,
        *,
        work: ExplorationWork,
        agent_run_id: str,
    ) -> AppFrontierItem | None:
        """本地回合已提出终态后的 restore 选路：绕过 scheduler stop_reason 滤光。

        普通 ``next_pending`` 在 stop_reason≠NONE 时会返回空，但 Frontier counts 仍非
        exhausted（pending／running 租约）。restore 必须能直接领 sibling，否则父 Work
        会空转 Deferred 直至尝试耗尽。
        """

        list_pending = getattr(self._repository, "list_pending", None)
        if not callable(list_pending):
            return await self.next_pending(work=work, agent_run_id=agent_run_id)
        pending = await list_pending(
            task_id=work.task_id,
            agent_run_id=agent_run_id,
            session_id=work.session_id,
            limit=256,
        )
        if not pending:
            return None
        missing = _missing_goal_page_types(work)
        state = scheduler_state_from_metadata(
            work.frontier.observation.metadata,
            max_actions=work.budget.max_actions,
            max_duration_ms=work.budget.remaining_duration_ms,
            deadline=work.deadline,
            policy=self._scheduler.policy,
            required_page_types=_missing_goal_page_types(work),
            evidence_gap_page_types=_app_evidence_gap_page_types(
                work.frontier.observation.metadata,
                known_page_types=_goal_page_types(work)[0],
            ),
        )
        # 优先覆盖缺口；否则按原 rank 取第一条可领 pending。
        goal_pending = tuple(
            item
            for item in pending
            if self.pending_item_targets_missing_goal(work=work, item=item)
        )
        pool = goal_pending or tuple(pending)
        return min(
            pool,
            key=lambda item: _pending_item_rank(
                work,
                item,
                scheduler=self._scheduler,
                required_page_types=state.required_page_types,
                evidence_gap_page_types=state.evidence_gap_page_types,
                missing_page_types=missing,
            ),
        )

    async def next_missing_goal_pending(
        self,
        *,
        work: ExplorationWork,
        agent_run_id: str,
    ) -> AppFrontierItem | None:
        """从全部 pending 中直接选择覆盖缺口，避免未知全局入口挡住确定性执行。"""

        pending = await self._list_pending(work=work, agent_run_id=agent_run_id)
        if not pending:
            fallback = await self.next_pending(work=work, agent_run_id=agent_run_id)
            if fallback is None or not self.pending_item_targets_missing_goal(
                work=work,
                item=fallback,
            ):
                return None
            return fallback
        missing = tuple(
            item
            for item in pending
            if self.pending_item_targets_missing_goal(work=work, item=item)
        )
        if not missing:
            return None
        missing_types = _missing_goal_page_types(work)
        state = scheduler_state_from_metadata(
            work.frontier.observation.metadata,
            max_actions=work.budget.max_actions,
            max_duration_ms=work.budget.remaining_duration_ms,
            deadline=work.deadline,
            policy=self._scheduler.policy,
            required_page_types=_missing_goal_page_types(work),
            evidence_gap_page_types=_app_evidence_gap_page_types(
                work.frontier.observation.metadata,
                known_page_types=_goal_page_types(work)[0],
            ),
        )
        candidates = self._scheduler.filter_candidates(
            tuple(item.candidate for item in missing),
            required_page_types=state.required_page_types,
            evidence_gap_page_types=state.evidence_gap_page_types,
            missing_page_types=missing_types,
            state=state,
            now=work.frontier.observation.observed_at,
        )
        allowed_ids = {candidate.candidate_id for candidate in candidates}
        selectable = tuple(item for item in missing if item.candidate.candidate_id in allowed_ids)
        if not selectable:
            return None
        return min(
            selectable,
            key=lambda item: _pending_item_rank(
                work,
                item,
                scheduler=self._scheduler,
                required_page_types=state.required_page_types,
                evidence_gap_page_types=state.evidence_gap_page_types,
                missing_page_types=missing_types,
            ),
        )

    def pending_item_targets_missing_goal(
        self,
        *,
        work: ExplorationWork,
        item: AppFrontierItem,
    ) -> bool:
        """只允许有明确页型证据的覆盖缺口跳过普通模型决策。"""

        # playbook 底栏硬覆盖：无 expected_next_page_type（如 AI科技）时仍算缺口，
        # 否则页型覆盖追上后 next_missing_goal 永远领不到，会被首页宫格饿死。
        if item.candidate.metadata.get("bottom_navigation_tab_seek") is True:
            return True
        expected = _expected_page_type(item.candidate)
        if expected is None or work.app_goal is None:
            return False
        required, covered = _goal_page_types(work)
        if expected in required - covered:
            return True
        # 已覆盖页型仍可能被 Evidence Index/Rule writer 标为证据不足；
        # 这类候选同样属于定向补采，不能只因页型已覆盖就被 next_missing
        # 入口永久排除。
        return expected in _app_evidence_gap_page_types(
            work.frontier.observation.metadata,
            known_page_types=required,
        )

    def pending_item_is_redundant(
        self,
        *,
        work: ExplorationWork,
        item: AppFrontierItem,
    ) -> bool:
        """用候选来源页和最新覆盖事实判断历史 sibling 是否已执行。"""

        if (
            item.task_id != work.task_id
            or item.session_id != work.session_id
            or item.surface_kind is not work.surface_kind
        ):
            return False
        metadata = {
            **dict(work.frontier.observation.metadata),
            "business_page_identity": item.source_page_identity,
        }
        return should_suppress_redundant_app_candidate(
            candidate=item.candidate,
            coverage_metadata=metadata,
        )

    def registered_item_for_candidate(
        self,
        *,
        work: ExplorationWork,
        candidate: CandidateAction,
        registered: tuple[AppFrontierItem, ...],
    ) -> AppFrontierItem | None:
        if not _is_global_candidate(candidate):
            return None
        fingerprint = build_app_frontier_candidate_fingerprint(
            source_page_identity=_source_page_identity(work),
            candidate=with_app_frontier_fingerprint_strategy(candidate),
        )
        return next(
            (item for item in registered if item.candidate_fingerprint == fingerprint),
            None,
        )

    async def counts(
        self,
        *,
        work: ExplorationWork,
        agent_run_id: str,
    ) -> AppFrontierCounts:
        return await self._repository.counts(
            task_id=work.task_id,
            agent_run_id=agent_run_id,
            session_id=work.session_id,
        )

    async def _list_pending(
        self,
        *,
        work: ExplorationWork,
        agent_run_id: str,
    ) -> tuple[AppFrontierItem, ...]:
        list_pending = getattr(self._repository, "list_pending", None)
        if not callable(list_pending):
            return ()
        return tuple(
            await list_pending(
                task_id=work.task_id,
                agent_run_id=agent_run_id,
                session_id=work.session_id,
                limit=256,
            )
        )


def _source_page_identity(work: ExplorationWork) -> str:
    value = work.frontier.observation.metadata.get("business_page_identity")
    if isinstance(value, str) and value.strip():
        return value
    return "app-page:" + stable_protocol_hash(
        {
            "location": work.frontier.observation.location.casefold(),
            "page_type": (work.frontier.observation.page_type or "unknown").casefold(),
            "business_page_scope": str(
                work.frontier.observation.metadata.get("business_page_scope") or "unknown"
            ).casefold(),
        }
    )


def _navigation_path(work: ExplorationWork) -> tuple[str, ...]:
    value = work.frontier.observation.metadata.get("app_navigation_path")
    if isinstance(value, (list, tuple)):
        return tuple(item for item in value if isinstance(item, str) and item.strip())
    return (_source_page_identity(work),)


def _is_global_candidate(candidate: CandidateAction) -> bool:
    """只持久化 Provider 明确声明可恢复的业务分支。"""

    if candidate.risk_level is InteractionRiskLevel.HIGH:
        return False
    # 提升真实视觉入口不会改变其身份；只有旧合成坐标合同不得跨帧持久化。
    if candidate.metadata.get("coverage_page_seek_contract") is not None:
        return False
    if candidate.metadata.get("control_role") in {"dismiss", "skip"}:
        return False
    return candidate.metadata.get("frontier_scope") in {"page", "app"}


def _is_restoration_navigation_candidate(candidate: CandidateAction) -> bool:
    """只投影当前截图可见、可安全导航到已知页型的点击锚点。"""

    expected_page_type = candidate.metadata.get(
        "expected_next_page_type"
    ) or candidate.metadata.get("expected_state_hint")
    return (
        candidate.action_type is InteractionOperation.CLICK
        and candidate.risk_level is not InteractionRiskLevel.HIGH
        and candidate.metadata.get("frontier_scope") in {"page", "app"}
        and candidate.metadata.get("coverage_page_seek_contract") is None
        and candidate.metadata.get("control_role") not in {"dismiss", "skip"}
        and isinstance(expected_page_type, str)
        and bool(expected_page_type.strip())
    )


def _unique_frontier_items(items) -> tuple[AppFrontierItem, ...]:
    """同一截图内的语义别名只登记一次，保持 Provider 原始优先顺序。"""

    unique: dict[str, AppFrontierItem] = {}
    for item in items:
        unique.setdefault(item.candidate_fingerprint, item)
    return tuple(unique.values())


def _select_goal_frontier_candidates(
    work: ExplorationWork,
    *,
    agent_run_id: str,
    candidates: tuple[CandidateAction, ...],
    pending: tuple[AppFrontierItem, ...],
) -> tuple[CandidateAction, ...]:
    """压缩已知无收益候选，并为每个覆盖缺口保留一个可替代入口。"""

    required, covered = _goal_page_types(work)
    evidence_gaps = set(
        _app_evidence_gap_page_types(
            work.frontier.observation.metadata,
            known_page_types=required,
        )
    )
    current = (
        str(work.frontier.observation.page_type or "").strip().casefold()
        if work.frontier.observation.page_state == "stable"
        else ""
    )
    current_rule_gap = _current_page_has_rule_gap(
        work, agent_run_id=agent_run_id, current_page_type=current,
    )
    missing = required - covered
    # 选了质检范围时，目标还没走齐就允许经过没选中的业务页面去找（同一种经过页只进一次）；
    # 选「全部」时目标就是全部业务页面类型，这里不会放进任何额外候选
    transit_page_types = (
        frozenset(get_default_page_type_registry().coverage_required_codes) - required - covered
        if missing
        else frozenset()
    )
    pending_counts: dict[str | None, int] = {}
    for item in pending:
        if item.candidate.metadata.get("bottom_navigation_tab_seek") is True:
            # 底栏硬覆盖按标签独立计数，不与 goal page_type 配额混用。
            key = _bottom_nav_frontier_quota_key(item.candidate)
        else:
            key = _expected_page_type(item.candidate)
        pending_counts[key] = pending_counts.get(key, 0) + 1

    eligible = tuple(
        candidate
        for candidate in candidates
        if (
            candidate.metadata.get("bottom_navigation_tab_seek") is True
            and not popup_phase_blocks_bottom_tabs(work.frontier.observation.metadata)
        )
        or (
            candidate.metadata.get("bottom_navigation_tab_seek") is not True
            and (
                (expected := _expected_page_type(candidate)) is None
                or (
                    (
                        expected in missing
                        or expected in evidence_gaps
                        or expected in transit_page_types
                    )
                    and (
                        expected != current
                        or (
                            current_rule_gap
                            and _current_page_gap_candidate_ready(work, candidate)
                        )
                    )
                )
            )
        )
    )
    ordered = sorted(
        eligible,
        key=lambda candidate: _goal_candidate_rank(
            candidate,
            missing=missing,
            evidence_gaps=evidence_gaps,
        ),
    )
    selected_ids: set[str] = set()
    admitted_counts: dict[str | None, int] = {}
    pending_fingerprints = {item.candidate_fingerprint for item in pending}
    for candidate in ordered:
        fingerprint = build_app_frontier_candidate_fingerprint(
            source_page_identity=_source_page_identity(work),
            candidate=with_app_frontier_fingerprint_strategy(candidate),
        )
        if fingerprint in pending_fingerprints:
            # 同一入口重现只刷新当前视觉绑定，不再次占用目标配额。
            selected_ids.add(candidate.candidate_id)
            continue
        if candidate.metadata.get("bottom_navigation_tab_seek") is True:
            # 底栏硬覆盖：每标签必留一条，不受 required_page_types 压缩。
            key = _bottom_nav_frontier_quota_key(candidate)
            used = pending_counts.get(key, 0) + admitted_counts.get(key, 0)
            if used >= 1:
                continue
            selected_ids.add(candidate.candidate_id)
            admitted_counts[key] = admitted_counts.get(key, 0) + 1
            continue
        expected = _expected_page_type(candidate)
        limit = _GOAL_TARGET_PENDING_LIMIT if expected is not None else _GOAL_UNKNOWN_PENDING_LIMIT
        used = pending_counts.get(expected, 0) + admitted_counts.get(expected, 0)
        if used >= limit:
            continue
        selected_ids.add(candidate.candidate_id)
        admitted_counts[expected] = admitted_counts.get(expected, 0) + 1
    return tuple(candidate for candidate in candidates if candidate.candidate_id in selected_ids)


def _current_page_has_rule_gap(
    work: ExplorationWork, *, agent_run_id: str, current_page_type: str,
) -> bool:
    """同页补证只接收当前数据库范围内的冻结规则需求，不接收模型调度草稿。"""
    metadata = work.frontier.observation.metadata
    coverage = metadata.get("app_rule_inventory_coverage")
    scope = metadata.get("exploration_scheduler_facts_scope")
    if not current_page_type or not isinstance(coverage, Mapping) or not isinstance(scope, Mapping):
        return False
    if (
        coverage.get("protocol_version") != "app-rule-coverage-facts.v1"
        or coverage.get("source") != "frozen_task_rule_inventory_and_current_run_evidence"
        or scope.get("source") != "current_task_surface_session"
        or scope.get("surface_kind") != work.surface_kind.value
        or not coverage.get("inventory_hash")
    ):
        return False
    for key, expected in (
        ("task_id", work.task_id), ("agent_run_id", agent_run_id), ("session_id", work.session_id),
    ):
        if coverage.get(key) != expected or scope.get(key) != expected:
            return False
    epoch = scope.get("finalization_epoch")
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
        return False
    if coverage.get("finalization_epoch") != epoch:
        return False
    pending = coverage.get("pending_rules")
    count = coverage.get("pending_rule_count")
    if (
        not isinstance(pending, (list, tuple))
        or isinstance(count, bool) or not isinstance(count, int)
        or count <= 0 or count != len(pending)
    ):
        return False
    return any(
        isinstance(rule, Mapping) and rule.get("status") == "pending"
        and current_page_type in (rule.get("target_page_types") or ())
        for rule in pending
    )


def _current_page_gap_candidate_ready(work: ExplorationWork, candidate: CandidateAction) -> bool:
    """仅保留当前可见的安全业务动作，继续受原预算、配额及语义去重约束。"""
    if (
        work.budget.resolved_remaining_business_actions <= 0
        or work.budget.remaining_duration_ms <= 0
        or datetime.now(UTC) >= work.deadline
        or candidate.action_type not in work.boundaries.allowed_operations
        or candidate.navigation_depth > work.budget.max_depth
        or candidate.expected_surface_kind is not work.surface_kind
        or candidate.candidate_id in work.frontier.visited_candidate_ids
        or candidate.candidate_id in work.frontier.blocked_candidate_ids
        or candidate.metadata.get("source_capture_id") != work.source_capture_id
        or not is_business_navigation_entry(
            source_capture_id=candidate.metadata.get("source_capture_id"),
            discovery_source=candidate.metadata.get("discovery_source"),
        )
    ):
        return False
    observation = work.frontier.observation
    presentation = derive_app_presentation(
        page_state=observation.page_state,
        overlay_signature=observation.overlay_signature,
        surface_facts=observation.metadata.get("surface_facts"),
    )
    return presentation.base_page_exposed and not presentation.occluding_layer_present


def _bottom_nav_frontier_quota_key(candidate: CandidateAction) -> str:
    """底栏 Frontier 配额键：按语义标签，避免吃瓜／交友同为 post_list 时互斥。"""

    raw = str(candidate.semantic_target or "").strip()
    for prefix in ("底栏「", "底部导航「", "底部", "底栏"):
        if raw.startswith(prefix):
            raw = raw[len(prefix) :].strip()
    label = raw.strip("「」[]()（） ").casefold()
    return f"bottom_nav:{label or candidate.candidate_id}"


def _goal_candidate_rank(
    candidate: CandidateAction,
    *,
    missing: set[str],
    evidence_gaps: set[str] | frozenset[str] = frozenset(),
) -> tuple[int, int, int, int, str]:
    text = " ".join(
        (
            candidate.semantic_target,
            str(candidate.metadata.get("purpose") or ""),
        )
    ).casefold()
    low_value_rank = int(any(marker in text for marker in _LOW_VALUE_NAVIGATION_MARKERS))
    expected = _expected_page_type(candidate)
    if expected is not None and expected in missing:
        missing_tab_rank = missing_required_page_tab_rank(expected)
    elif expected is not None and expected in evidence_gaps:
        missing_tab_rank = missing_required_page_tab_rank(expected)
    else:
        missing_tab_rank = missing_required_page_tab_rank(None)
    scope_rank = 0 if candidate.metadata.get("frontier_scope") == "app" else 1
    # 覆盖缺口是领取级硬优先级；app/page scope 只在同一覆盖级别内打破平局。
    # 否则一个未知 app 入口会因为 scope_rank=0 抢在明确 required 缺页之前。
    return (
        low_value_rank,
        missing_tab_rank,
        scope_rank,
        -candidate.exploration_value,
        candidate.candidate_id,
    )


def _expected_page_type(candidate: CandidateAction) -> str | None:
    value = candidate.metadata.get("expected_next_page_type")
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().casefold()


def _goal_page_types(work: ExplorationWork) -> tuple[set[str], set[str]]:
    required = (
        {value.casefold() for value in work.app_goal.required_page_types}
        if work.app_goal is not None
        else set()
    )
    covered = {
        value.casefold()
        for value in work.frontier.observation.metadata.get("covered_page_types", ())
        if isinstance(value, str)
    }
    return required, covered


def _missing_goal_page_types(work: ExplorationWork) -> frozenset[str]:
    required, covered = _goal_page_types(work)
    return frozenset(required - covered)


def _app_evidence_gap_page_types(
    metadata,
    *,
    known_page_types: Sequence[str] = (),
) -> tuple[str, ...]:
    """读取已持久化的 Evidence/Rule 缺口，不采信候选自报优先级。"""

    return evidence_gap_page_types_from_facts(
        metadata,
        known_page_types=known_page_types,
    )


def _candidate_priority(work: ExplorationWork, candidate: CandidateAction) -> int:
    """用最新覆盖缺口与底栏稀缺序调整队列优先级，候选预期不直接计入覆盖。"""

    base = max(0, 100 - candidate.exploration_value)
    if candidate.metadata.get("frontier_scope") == "app":
        # 底栏、顶栏等全局导航先于当前页面内表单/活动入口，确保先建立页面地图。
        # 这里只给持久化 priority 一个小幅奖励；真正领取时仍由 scope_rank
        # 确定全局导航优先，避免把所有高价值候选都压成同一个零优先级。
        base = max(0, base - 5)
    expected = candidate.metadata.get("expected_next_page_type")
    if not isinstance(expected, str) or not expected.strip() or work.app_goal is None:
        return base
    normalized = expected.strip().casefold()
    required = {item.casefold() for item in work.app_goal.required_page_types}
    covered = {
        item.casefold()
        for item in work.frontier.observation.metadata.get("covered_page_types", ())
        if isinstance(item, str)
    }
    evidence_gaps = set(
        _app_evidence_gap_page_types(
            work.frontier.observation.metadata,
            known_page_types=required,
        )
    )
    if normalized in required - covered:
        # 数字越小越优先；高优先缺页再减 tab_boost。
        return max(0, base - 50 - missing_required_page_tab_boost(normalized))
    if normalized in evidence_gaps:
        return max(0, base - 25 - missing_required_page_tab_boost(normalized))
    if normalized in covered:
        return base + 50
    return base


def _pending_item_rank(
    work: ExplorationWork,
    item: AppFrontierItem,
    *,
    scheduler: BoundedExplorationScheduler | None = None,
    required_page_types: Sequence[str] = (),
    evidence_gap_page_types: Sequence[str] = (),
    missing_page_types: set[str] | frozenset[str] = frozenset(),
) -> tuple[int, int, int, int, int, int, int, int, object, str]:
    """领取前基于最新覆盖快照重排历史 sibling，避免发现时优先级固化。"""

    candidate = item.candidate
    expected = candidate.metadata.get("expected_next_page_type")
    expected = expected.strip().casefold() if isinstance(expected, str) else None
    required = (
        {value.casefold() for value in work.app_goal.required_page_types}
        if work.app_goal is not None
        else set()
    )
    covered = {
        value.casefold()
        for value in work.frontier.observation.metadata.get("covered_page_types", ())
        if isinstance(value, str)
    }
    evidence_gap_set = {value.casefold() for value in evidence_gap_page_types}
    bottom_nav_rank = (
        0 if candidate.metadata.get("bottom_navigation_tab_seek") is True else 1
    )
    if expected is not None and expected in required - covered:
        coverage_rank = 0
        missing_tab_rank = missing_required_page_tab_rank(expected)
    elif expected is not None and expected in evidence_gap_set:
        # 已覆盖页型但 Evidence Writer 标成缺口时，继续让补采队列进入领取
        # 排序；不能把它永久视为已完成页型。
        coverage_rank = 1
        missing_tab_rank = missing_required_page_tab_rank(expected)
    elif expected is None or expected not in required:
        coverage_rank = 1
        missing_tab_rank = 0
    else:
        coverage_rank = 2
        missing_tab_rank = 0
    scheduler_rank = (scheduler or BoundedExplorationScheduler()).queue_rank(
        candidate,
        required_page_types=required_page_types,
        evidence_gap_page_types=evidence_gap_page_types,
        missing_page_types=missing_page_types,
    )
    risk_rank = 0 if candidate.risk_level is InteractionRiskLevel.LOW else 1
    scope_rank = 0 if candidate.metadata.get("frontier_scope") == "app" else 1
    playbook_tab_rank = 99
    raw_tab_index = candidate.metadata.get("playbook_tab_index")
    if (
        candidate.metadata.get("bottom_navigation_tab_seek") is True
        and isinstance(raw_tab_index, int)
        and not isinstance(raw_tab_index, bool)
        and raw_tab_index >= 0
    ):
        playbook_tab_rank = raw_tab_index
    return (
        # 先遵守必检、缺证、自由发现的队列顺序，再用旧规则决定队列内优先级。
        # 未完成的 playbook 底栏硬覆盖优先于首页宫格等 page-scope 入口。
        scheduler_rank,
        bottom_nav_rank,
        playbook_tab_rank,
        coverage_rank,
        missing_tab_rank,
        scope_rank,
        risk_rank,
        len(item.navigation_path),
        -candidate.exploration_value,
        item.discovered_at,
        item.frontier_item_id,
    )


def _replace_candidates(
    frontier: FrontierSnapshot,
    *,
    candidates: tuple[CandidateAction, ...],
    metadata: dict[str, object],
) -> FrontierSnapshot:
    observation = replace(
        frontier.observation,
        metadata={**dict(frontier.observation.metadata), **metadata},
    )
    return replace(frontier, observation=observation, candidates=candidates)


def _with_global_counts(
    work: ExplorationWork,
    counts: AppFrontierCounts,
) -> ExplorationWork:
    metadata: dict[str, object] = {
        "app_global_frontier": {
            "protocol_version": "app-global-frontier-summary.v1",
            **counts.to_dict(),
            "nonterminal_count": counts.nonterminal_count,
            "exhausted": counts.exhausted,
        }
    }
    return replace(
        work,
        frontier=_replace_candidates(
            work.frontier,
            candidates=work.frontier.candidates,
            metadata=metadata,
        ),
    )
