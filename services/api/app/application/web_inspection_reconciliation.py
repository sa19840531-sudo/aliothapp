"""Web 正式质检收口对账、Coverage Snapshot 与 Gap 落库。"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from services.api.app.application.rule_execution import (
    RecordRuleExecutionCommand,
    RuleExecutionService,
)
from services.api.app.application.surface_sessions import SessionDrainPendingError
from services.api.app.application.task_finalization import (
    BeginExplorationDrainingCommand,
    BeginSessionDrainingCommand,
    EvaluateCompletionGateCommand,
    TerminateTaskFinalizationCommand,
)
from services.api.app.domain.exploration.web_coverage_snapshot import (
    WebCoverageSnapshot,
)
from services.api.app.domain.page.web_page_catalog import (
    WEB_V3_BUSINESS_PAGE_TYPE_CODES,
)
from services.api.app.domain.task import TaskExecutionStatus, TaskQualityOutcome

_RUN_TERMINAL_STATUSES = frozenset({"done", "failed", "aborted"})
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class ReconcileWebInspectionCommand:
    """绑定当前 Web main Run 与收口 epoch 的对账命令。"""

    task_id: str
    agent_run_id: str
    finalization_epoch: int
    recovery_lease_token: str | None = None
    recovery_generation: int | None = None
    recovery_lease_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_text(self.task_id, "task_id")
        _require_text(self.agent_run_id, "agent_run_id")
        _require_nonnegative(self.finalization_epoch, "finalization_epoch")
        fence = (
            self.recovery_lease_token,
            self.recovery_generation,
            self.recovery_lease_expires_at,
        )
        if any(value is not None for value in fence):
            _require_text(self.recovery_lease_token, "recovery_lease_token")
            _require_nonnegative(self.recovery_generation, "recovery_generation")
            if not isinstance(self.recovery_lease_expires_at, datetime):
                raise ValueError("recovery_lease_expires_at 必须为 datetime")


@dataclass(frozen=True, slots=True, kw_only=True)
class WebInspectionReconciliationState:
    """一次持久化读取所得的权威 Web 收口事实。"""

    task_id: str
    agent_run_id: str
    finalization_epoch: int
    task_status: str
    task_finalization_state: str
    task_row_version: int
    run_status: str
    current_main_run: bool
    execution_mode: str
    notification_mode: str
    exploration_terminal: bool
    frontier_work_count: int
    pending_capture_commit_count: int
    nonterminal_publication_count: int
    nonterminal_work_item_count: int
    active_session_request_count: int
    observed_page_types: tuple[str, ...]
    unrecognized_page_types: tuple[str, ...]
    pending_manifest_count: int = 0
    review_publication_count: int = 0
    failed_publication_count: int = 0
    missing_assessment_count: int = 0
    review_assessment_count: int = 0
    missing_publication_count: int = 0
    failed_manifest_count: int = 0
    open_review_count: int = 0
    foundational_failure_code: str | None = None
    target_platform: str = "web"
    required_page_types: tuple[str, ...] = tuple(sorted(WEB_V3_BUSINESS_PAGE_TYPE_CODES))
    optional_page_types: tuple[str, ...] = ("activity", "search_page")
    not_applicable_page_types: tuple[str, ...] = ()
    applicability_mode: str = "legacy_required_snapshot"
    catalog_version: str = "legacy-task-config"
    catalog_hash: str = "legacy-task-config"
    normalization_version: str | None = None
    snapshot_protocol_version: str | None = None
    frozen_coverage_snapshot: WebCoverageSnapshot | None = None
    observed_lifecycle_page_types: tuple[str, ...] = ()
    unknown_page_capture_ids: tuple[str, ...] = ()
    supplemental_evidence_summary: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        frozen = self.frozen_coverage_snapshot
        frozen_identity = (
            (frozen.task_id, frozen.agent_run_id, frozen.finalization_epoch)
            if frozen is not None
            else None
        )
        if frozen_identity is not None and frozen_identity != (
            self.task_id,
            self.agent_run_id,
            self.finalization_epoch,
        ):
            raise ValueError("冻结覆盖快照不属于当前 task/run/epoch")
        partition = set(
            self.required_page_types
            + self.optional_page_types
            + self.not_applicable_page_types
        )
        normalization = (
            frozen.normalization_version if frozen else self.normalization_version
        ) or (
            "web-page-type-assessment.v4"
            if {"activity", "search_page", "collection_page"} & partition
            else "web-page-type-assessment.v3" if "home" in partition
            else "web-page-type-assessment.v2"
        )
        protocol = (
            frozen.snapshot_protocol_version if frozen else self.snapshot_protocol_version
        ) or (
            "web-coverage-snapshot.v4" if normalization.endswith(".v4")
            else "web-coverage-snapshot.v3" if normalization.endswith(".v3")
            else "web-coverage-snapshot.v2"
        )
        object.__setattr__(self, "normalization_version", normalization)
        object.__setattr__(self, "snapshot_protocol_version", protocol)
        for field_name in (
            "task_id",
            "agent_run_id",
            "task_status",
            "task_finalization_state",
            "run_status",
            "execution_mode",
            "notification_mode",
            "target_platform",
        ):
            _require_text(getattr(self, field_name), field_name)
        for field_name in (
            "finalization_epoch",
            "task_row_version",
            "frontier_work_count",
            "pending_capture_commit_count",
            "pending_manifest_count",
            "review_publication_count",
            "failed_publication_count",
            "nonterminal_publication_count",
            "nonterminal_work_item_count",
            "active_session_request_count",
            "missing_assessment_count",
            "review_assessment_count",
            "missing_publication_count",
            "failed_manifest_count",
            "open_review_count",
        ):
            _require_nonnegative(getattr(self, field_name), field_name)
        if not isinstance(self.current_main_run, bool):
            raise ValueError("current_main_run 必须为布尔值")
        if not isinstance(self.exploration_terminal, bool):
            raise ValueError("exploration_terminal 必须为布尔值")
        if self.foundational_failure_code is not None:
            _require_text(self.foundational_failure_code, "foundational_failure_code")


@dataclass(frozen=True, slots=True, kw_only=True)
class WebCoverageGapRule:
    """一个缺失标准页型精确匹配到的 Web page-level 规则。"""

    rule_id: str
    rule_code: str
    missing_page_type: str

    def __post_init__(self) -> None:
        _require_text(self.rule_id, "rule_id")
        _require_text(self.rule_code, "rule_code")
        _require_text(self.missing_page_type, "missing_page_type")


@dataclass(frozen=True, slots=True)
class WebInspectionReconciliationResult:
    """供恢复器判断下一步的幂等对账结果。"""

    status: str
    execution_status: str | None = None
    coverage_snapshot_id: str | None = None
    coverage_snapshot_hash: str | None = None


class _StateReader(Protocol):
    async def load_reconciliation_state(self, **kwargs: object) -> object: ...


class _KeywordStage(Protocol):
    async def __call__(self, **kwargs: object) -> object: ...


class _SnapshotRepository(Protocol):
    async def create(
        self,
        snapshot: WebCoverageSnapshot,
        **kwargs: object,
    ) -> WebCoverageSnapshot: ...

    async def load_reference(self, **kwargs: object) -> tuple[str, str] | None: ...


class _GapRuleReader(Protocol):
    async def list_gap_rule_candidates(self, **kwargs: object) -> object: ...


class WebInspectionReconciliation:
    """先排空正式工作，再冻结快照并通过正式 RuleExecution 写 Gap。"""

    def __init__(
        self,
        *,
        state_reader: _StateReader,
        manifest_finalizer: object | None,
        publication_publisher: object | None,
        active_session_drainer: object,
        finalization: object,
        snapshot_repository: _SnapshotRepository,
        gap_rule_reader: _GapRuleReader,
        rule_execution_service: RuleExecutionService,
        final_handoff: object | None = None,
        session_closer: object | None = None,
        failed_work_finalizer: object | None = None,
        terminal_ledger_finalizer: object | None = None,
    ) -> None:
        if final_handoff is None and (
            manifest_finalizer is None or publication_publisher is None
        ):
            raise ValueError("Web 最终交接必须注入组合端口或完整的两阶段端口")
        self._state_reader = state_reader
        self._manifest_finalizer = manifest_finalizer
        self._publication_publisher = publication_publisher
        self._active_session_drainer = active_session_drainer
        self._session_closer = session_closer
        self._finalization = finalization
        self._snapshot_repository = snapshot_repository
        self._gap_rule_reader = gap_rule_reader
        self._rule_execution_service = rule_execution_service
        self._final_handoff = final_handoff
        self._failed_work_finalizer = failed_work_finalizer
        self._terminal_ledger_finalizer = terminal_ledger_finalizer

    async def reconcile(
        self,
        command: ReconcileWebInspectionCommand,
    ) -> WebInspectionReconciliationResult:
        if not isinstance(command, ReconcileWebInspectionCommand):
            raise TypeError("command 必须为 ReconcileWebInspectionCommand")
        state = await self._state_reader.load_reconciliation_state(
            task_id=command.task_id,
            agent_run_id=command.agent_run_id,
            finalization_epoch=command.finalization_epoch,
            recovery_lease_token=command.recovery_lease_token,
            recovery_generation=command.recovery_generation,
            recovery_lease_expires_at=command.recovery_lease_expires_at,
        )
        if not isinstance(state, WebInspectionReconciliationState):
            raise RuntimeError("无法读取 Web 正式质检收口事实")
        _validate_current_state(state, command)

        if state.task_finalization_state == "sealed":
            return WebInspectionReconciliationResult(
                status="already_terminal",
                execution_status=_sealed_execution_status(state.task_status),
            )

        if (
            not state.exploration_terminal
            and state.run_status == "done"
            and state.frontier_work_count == 0
            and state.pending_capture_commit_count == 0
        ):
            recover_seal = getattr(self._state_reader, "seal_completed_budget", None)
            if callable(recover_seal) and await recover_seal(
                task_id=command.task_id,
                agent_run_id=command.agent_run_id,
                finalization_epoch=command.finalization_epoch,
                **_recovery_fence_kwargs(command),
            ):
                recovered = await self._state_reader.load_reconciliation_state(
                    task_id=command.task_id,
                    agent_run_id=command.agent_run_id,
                    finalization_epoch=command.finalization_epoch,
                    **_recovery_fence_kwargs(command),
                )
                if not isinstance(recovered, WebInspectionReconciliationState):
                    raise RuntimeError("Web 探索预算恢复封账后无法重读收口事实")
                _validate_current_state(recovered, command)
                state = recovered

        if (
            (not state.exploration_terminal and not state.foundational_failure_code)
            or state.frontier_work_count
            or state.pending_capture_commit_count
        ):
            return WebInspectionReconciliationResult(
                status="waiting_for_exploration_terminal"
            )

        if state.task_finalization_state == "open":
            return await self._handoff_open_task(state, recovery_fence=command)

        if state.task_finalization_state == "exploration_draining":
            return await self._complete_final_handoff(
                state,
                expected_row_version=state.task_row_version,
                recovery_fence=command,
            )

        if state.task_status == "review":
            # 兼容旧流程先写 legacy review、后补 Task Finalization 的任务。
            # 该状态已经是外部可见的人工复核承诺，恢复时不得因派生库存缺口改判 failed。
            reason_code = _review_reason_code(state, include_missing_assessment=True)
            state = await self._terminalize_formal_ledgers(
                state,
                outcome="needs_review",
                reason_code=reason_code,
                recovery_fence=command,
            )
            if (
                state.nonterminal_publication_count
                or state.nonterminal_work_item_count
                or state.active_session_request_count
            ):
                return WebInspectionReconciliationResult(
                    status="waiting_for_formal_work"
                )
            existing_snapshot = await self._load_existing_coverage_reference(state)
            if existing_snapshot is None:
                snapshot = await self._freeze_coverage_and_record_gaps(
                    state,
                    recovery_fence=command,
                )
                snapshot_id = snapshot.snapshot_id
                snapshot_hash = snapshot.snapshot_hash
            else:
                snapshot_id, snapshot_hash = existing_snapshot
            return await self._terminate(
                state,
                execution_status=TaskExecutionStatus.NEEDS_REVIEW,
                reason_code=reason_code,
                coverage_snapshot_id=snapshot_id,
                coverage_snapshot_hash=snapshot_hash,
                recovery_fence=command,
            )

        if state.foundational_failure_code:
            state = await self._terminalize_formal_ledgers(
                state,
                outcome="failed",
                reason_code=state.foundational_failure_code,
                recovery_fence=command,
            )
            if (
                state.nonterminal_publication_count
                or state.nonterminal_work_item_count
                or state.active_session_request_count
            ):
                return WebInspectionReconciliationResult(
                    status="waiting_for_formal_work"
                )
            snapshot = state.frozen_coverage_snapshot or WebCoverageSnapshot.create(
                task_id=state.task_id,
                agent_run_id=state.agent_run_id,
                finalization_epoch=state.finalization_epoch,
                required_page_types=state.required_page_types,
                observed_page_types=state.observed_page_types,
                unrecognized_page_types=state.unrecognized_page_types,
                coverage_status="failed",
                optional_page_types=state.optional_page_types,
                not_applicable_page_types=state.not_applicable_page_types,
                applicability_mode=state.applicability_mode,
                catalog_version=state.catalog_version,
                catalog_hash=state.catalog_hash,
                normalization_version=state.normalization_version,
                snapshot_protocol_version=state.snapshot_protocol_version,
                observed_lifecycle_page_types=state.observed_lifecycle_page_types,
                unknown_page_capture_ids=state.unknown_page_capture_ids,
                supplemental_evidence_summary=state.supplemental_evidence_summary,
            )
            snapshot = await self._snapshot_repository.create(
                snapshot,
                **_recovery_fence_kwargs(command),
            )
            return await self._terminate(
                state,
                execution_status=TaskExecutionStatus.FAILED,
                reason_code=state.foundational_failure_code,
                coverage_snapshot_id=snapshot.snapshot_id,
                coverage_snapshot_hash=snapshot.snapshot_hash,
                recovery_fence=command,
            )

        if (
            state.failed_manifest_count
            or state.missing_assessment_count
            or state.missing_publication_count
            or state.failed_publication_count
        ):
            reason_code = _failure_reason_code(state)
            state = await self._terminalize_formal_ledgers(
                state,
                outcome="failed",
                reason_code=reason_code,
                recovery_fence=command,
            )
            if (
                state.nonterminal_publication_count
                or state.nonterminal_work_item_count
                or state.active_session_request_count
            ):
                return WebInspectionReconciliationResult(
                    status="waiting_for_formal_work"
                )
            return await self._terminate(
                state,
                execution_status=TaskExecutionStatus.FAILED,
                reason_code=reason_code,
                recovery_fence=command,
            )

        if (
            state.review_assessment_count
            or state.review_publication_count
            or state.open_review_count
        ):
            reason_code = _review_reason_code(state)
            state = await self._terminalize_formal_ledgers(
                state,
                outcome="needs_review",
                reason_code=reason_code,
                recovery_fence=command,
            )
            if (
                state.nonterminal_publication_count
                or state.nonterminal_work_item_count
                or state.active_session_request_count
            ):
                return WebInspectionReconciliationResult(
                    status="waiting_for_formal_work"
                )
            snapshot = await self._freeze_coverage_and_record_gaps(
                state,
                recovery_fence=command,
            )
            return await self._terminate(
                state,
                execution_status=TaskExecutionStatus.NEEDS_REVIEW,
                reason_code=reason_code,
                coverage_snapshot_id=snapshot.snapshot_id,
                coverage_snapshot_hash=snapshot.snapshot_hash,
                recovery_fence=command,
            )

        if state.run_status not in _RUN_TERMINAL_STATUSES:
            return WebInspectionReconciliationResult(status="waiting_for_run_terminal")
        if (
            state.nonterminal_publication_count
            or state.nonterminal_work_item_count
            or state.active_session_request_count
        ):
            return WebInspectionReconciliationResult(status="waiting_for_formal_work")

        snapshot = await self._freeze_coverage_and_record_gaps(
            state,
            recovery_fence=command,
        )
        return WebInspectionReconciliationResult(
            status="gate_ready",
            execution_status=(
                "completed"
                if snapshot.coverage_status == "complete"
                else snapshot.coverage_status
            ),
            coverage_snapshot_id=snapshot.snapshot_id,
            coverage_snapshot_hash=snapshot.snapshot_hash,
        )

    async def _freeze_coverage_and_record_gaps(
        self,
        state: WebInspectionReconciliationState,
        *,
        recovery_fence: ReconcileWebInspectionCommand,
    ) -> WebCoverageSnapshot:
        """在进入 Gate 或人工复核终态前冻结覆盖，并幂等写入缺口规则。"""

        record_auth_not_applicable = getattr(
            self._gap_rule_reader,
            "record_auth_not_applicable",
            None,
        )
        if callable(record_auth_not_applicable):
            await record_auth_not_applicable(
                task_id=state.task_id,
                agent_run_id=state.agent_run_id,
                finalization_epoch=state.finalization_epoch,
                **_recovery_fence_kwargs(recovery_fence),
            )

        snapshot = state.frozen_coverage_snapshot or WebCoverageSnapshot.create(
            task_id=state.task_id,
            agent_run_id=state.agent_run_id,
            finalization_epoch=state.finalization_epoch,
            required_page_types=state.required_page_types,
            observed_page_types=state.observed_page_types,
            unrecognized_page_types=state.unrecognized_page_types,
            coverage_status=None,
            optional_page_types=state.optional_page_types,
            not_applicable_page_types=state.not_applicable_page_types,
            applicability_mode=state.applicability_mode,
            catalog_version=state.catalog_version,
            catalog_hash=state.catalog_hash,
            normalization_version=state.normalization_version,
            snapshot_protocol_version=state.snapshot_protocol_version,
            observed_lifecycle_page_types=state.observed_lifecycle_page_types,
            unknown_page_capture_ids=state.unknown_page_capture_ids,
            supplemental_evidence_summary=state.supplemental_evidence_summary,
        )
        snapshot = await self._snapshot_repository.create(
            snapshot,
            **_recovery_fence_kwargs(recovery_fence),
        )

        rules = await self._gap_rule_reader.list_gap_rule_candidates(snapshot=snapshot)
        for rule in tuple(rules):
            if not isinstance(rule, WebCoverageGapRule):
                raise RuntimeError("Web Coverage Gap 规则读取结果无效")
            if rule.missing_page_type not in snapshot.missing_page_types:
                raise RuntimeError("Web Coverage Gap 规则不属于冻结快照缺失页型")
            source_key = (
                f"web-coverage-gap:{snapshot.snapshot_id}:"
                f"{rule.missing_page_type}:{rule.rule_code}"
            )
            await self._rule_execution_service.record_rule_execution(
                RecordRuleExecutionCommand(
                    task_id=state.task_id,
                    agent_run_id=state.agent_run_id,
                    rule_id=rule.rule_id,
                    rule_code=rule.rule_code,
                    execution_status="evidence_insufficient",
                    page_capture_id=None,
                    source_idempotency_key=source_key,
                    result_summary="未发现规则要求的标准页面类型，证据不足。",
                    problem_desc="探索已结束，但没有发现该规则适用的标准页面类型。",
                    judge_input={
                        "schema_version": "web-coverage-gap.v1",
                        "source": "web_coverage_gap",
                        "coverage_snapshot_id": snapshot.snapshot_id,
                        "missing_page_type": rule.missing_page_type,
                        "reason": "required_page_type_not_discovered",
                    },
                    create_review_item=False,
                    recovery_lease_token=recovery_fence.recovery_lease_token,
                    recovery_generation=recovery_fence.recovery_generation,
                    recovery_lease_expires_at=(
                        recovery_fence.recovery_lease_expires_at
                    ),
                )
            )
        return snapshot

    async def _terminalize_formal_ledgers(
        self,
        state: WebInspectionReconciliationState,
        *,
        outcome: str,
        reason_code: str,
        recovery_fence: ReconcileWebInspectionCommand,
    ) -> WebInspectionReconciliationState:
        terminalize = getattr(
            self._terminal_ledger_finalizer,
            "terminalize",
            None,
        )
        if not callable(terminalize) and not (
            state.nonterminal_publication_count
            or state.nonterminal_work_item_count
        ):
            return state
        if callable(terminalize):
            await terminalize(
                task_id=state.task_id,
                agent_run_id=state.agent_run_id,
                outcome=outcome,
                reason_code=reason_code,
                reason_message="Web 正式质检终态已确认，收束未完成的持久账本",
                **_recovery_fence_kwargs(recovery_fence),
            )
        else:
            finalize_task_work = getattr(
                self._failed_work_finalizer,
                "finalize_task_work",
                None,
            )
            if (
                outcome != "failed"
                or state.nonterminal_publication_count
                or not callable(finalize_task_work)
            ):
                raise RuntimeError("Web 正式终态收口缺少持久账本终结器")
            await finalize_task_work(
                task_id=state.task_id,
                error_code=reason_code,
                error_message="Web 基础运行失败，收束未完成的正式质检工作",
                **_recovery_fence_kwargs(recovery_fence),
            )
        refreshed = await self._state_reader.load_reconciliation_state(
            task_id=state.task_id,
            agent_run_id=state.agent_run_id,
            finalization_epoch=state.finalization_epoch,
            **_recovery_fence_kwargs(recovery_fence),
        )
        if not isinstance(refreshed, WebInspectionReconciliationState):
            raise RuntimeError("Web 正式账本终结后无法重读收口事实")
        _validate_current_state(refreshed, recovery_fence)
        if (
            refreshed.task_finalization_state != "session_draining"
            or refreshed.task_row_version != state.task_row_version
        ):
            raise RuntimeError("Web 正式账本终结后收口身份已变化")
        return refreshed

    async def _handoff_open_task(
        self,
        state: WebInspectionReconciliationState,
        *,
        recovery_fence: ReconcileWebInspectionCommand,
    ) -> WebInspectionReconciliationResult:
        exploration = await self._finalization.begin_exploration_draining(
            command=BeginExplorationDrainingCommand(
                task_id=state.task_id,
                agent_run_id=state.agent_run_id,
                expected_row_version=state.task_row_version,
                recovery_lease_token=recovery_fence.recovery_lease_token,
                recovery_generation=recovery_fence.recovery_generation,
                recovery_lease_expires_at=recovery_fence.recovery_lease_expires_at,
            )
        )
        next_row_version = int(
            getattr(exploration, "row_version", state.task_row_version + 1)
        )
        return await self._complete_final_handoff(
            state,
            expected_row_version=next_row_version,
            recovery_fence=recovery_fence,
        )

    async def _complete_final_handoff(
        self,
        state: WebInspectionReconciliationState,
        *,
        expected_row_version: int,
        recovery_fence: ReconcileWebInspectionCommand,
    ) -> WebInspectionReconciliationResult:
        common = {
            "task_id": state.task_id,
            "agent_run_id": state.agent_run_id,
            "finalization_epoch": state.finalization_epoch,
        }
        if self._final_handoff is not None:
            await self._final_handoff.publish_pending_for_final_handoff(
                **common,
                **_recovery_fence_kwargs(recovery_fence),
            )
        else:
            assert self._manifest_finalizer is not None
            assert self._publication_publisher is not None
            await self._manifest_finalizer.finalize_all(
                **common,
                **_recovery_fence_kwargs(recovery_fence),
            )
            await self._publication_publisher.publish_pending_for_final_handoff(
                **common,
                **_recovery_fence_kwargs(recovery_fence),
            )
        refreshed = await self._state_reader.load_reconciliation_state(
            **common,
            **_recovery_fence_kwargs(recovery_fence),
        )
        if not isinstance(refreshed, WebInspectionReconciliationState):
            raise RuntimeError("无法重读 Web 最终补发库存")
        if (
            refreshed.task_id != state.task_id
            or refreshed.agent_run_id != state.agent_run_id
            or refreshed.finalization_epoch != state.finalization_epoch
            or not refreshed.current_main_run
            or refreshed.task_finalization_state != "exploration_draining"
            or refreshed.task_row_version != expected_row_version
        ):
            raise RuntimeError("Web 最终补发后二次读取身份或收口版本已变化")
        if (
            (
                not refreshed.exploration_terminal
                and not refreshed.foundational_failure_code
            )
            or refreshed.frontier_work_count
            or refreshed.pending_capture_commit_count
        ):
            return WebInspectionReconciliationResult(
                status="waiting_for_exploration_terminal"
            )
        if (
            refreshed.pending_manifest_count
            and not refreshed.foundational_failure_code
        ):
            return WebInspectionReconciliationResult(
                status="waiting_for_manifest_finalization"
            )
        if refreshed.missing_publication_count and not _has_terminal_blocker(refreshed):
            return WebInspectionReconciliationResult(
                status="waiting_for_publication_inventory"
            )
        await self._active_session_drainer.drain_before_close(
            **common,
            **_recovery_fence_kwargs(recovery_fence),
        )
        if self._session_closer is not None:
            try:
                await self._session_closer.close(
                    task_id=state.task_id,
                    session_id=f"web:{state.agent_run_id}",
                    **_recovery_fence_kwargs(recovery_fence),
                )
            except SessionDrainPendingError as exc:
                # 保留活浏览器与 exploration_draining，由既有恢复租约下一轮重试。
                # 身份不匹配不是可等待状态，不能吞掉错误的 Session 归属。
                if (
                    exc.task_id != state.task_id
                    or exc.readiness.session_id != f"web:{state.agent_run_id}"
                ):
                    raise
                logger.info("Web 正式收口等待会话排空：%s", exc)
                return WebInspectionReconciliationResult(status="waiting_for_session_drain")
        await self._finalization.begin_session_draining(
            command=BeginSessionDrainingCommand(
                task_id=state.task_id,
                agent_run_id=state.agent_run_id,
                expected_finalization_epoch=state.finalization_epoch,
                expected_row_version=expected_row_version,
                recovery_lease_token=recovery_fence.recovery_lease_token,
                recovery_generation=recovery_fence.recovery_generation,
                recovery_lease_expires_at=recovery_fence.recovery_lease_expires_at,
            )
        )
        return WebInspectionReconciliationResult(status="waiting_for_run_terminal")

    async def _terminate(
        self,
        state: WebInspectionReconciliationState,
        *,
        execution_status: TaskExecutionStatus,
        reason_code: str,
        recovery_fence: ReconcileWebInspectionCommand,
        coverage_snapshot_id: str | None = None,
        coverage_snapshot_hash: str | None = None,
    ) -> WebInspectionReconciliationResult:
        expected_row_version = state.task_row_version
        if execution_status is TaskExecutionStatus.NEEDS_REVIEW:
            expected_row_version = await self._refresh_needs_review_gate_snapshot(
                state,
                recovery_fence=recovery_fence,
            )
            expected_row_version = await self._reload_needs_review_row_version(
                state,
                minimum_row_version=expected_row_version,
            )
        await self._finalization.terminate(
            command=TerminateTaskFinalizationCommand(
                task_id=state.task_id,
                agent_run_id=state.agent_run_id,
                expected_finalization_epoch=state.finalization_epoch,
                expected_row_version=expected_row_version,
                execution_status=execution_status,
                reason_code=reason_code,
                recovery_lease_token=recovery_fence.recovery_lease_token,
                recovery_generation=recovery_fence.recovery_generation,
                recovery_lease_expires_at=recovery_fence.recovery_lease_expires_at,
            )
        )
        return WebInspectionReconciliationResult(
            status=(
                "needs_review"
                if execution_status is TaskExecutionStatus.NEEDS_REVIEW
                else "failed"
            ),
            execution_status=execution_status.value,
            coverage_snapshot_id=coverage_snapshot_id,
            coverage_snapshot_hash=coverage_snapshot_hash,
        )

    async def _reload_needs_review_row_version(
        self,
        state: WebInspectionReconciliationState,
        *,
        minimum_row_version: int,
    ) -> int:
        """Gate 刷新后按同一 owner/epoch 重读 CAS 版本，吸收并发审计写入。"""

        get_snapshot = getattr(self._finalization, "get", None)
        if not callable(get_snapshot):
            return minimum_row_version
        current = await get_snapshot(task_id=state.task_id)
        current_state = getattr(current, "state", None)
        current_state_value = str(
            getattr(current_state, "value", current_state) or ""
        )
        if (
            current is None
            or getattr(current, "task_id", None) != state.task_id
            or getattr(current, "agent_run_id", None) != state.agent_run_id
            or getattr(current, "finalization_epoch", None) != state.finalization_epoch
            or current_state_value != "session_draining"
        ):
            raise RuntimeError("Gate 刷新后 Web 人工复核收口身份已变化")
        row_version = getattr(current, "row_version", None)
        if (
            isinstance(row_version, bool)
            or not isinstance(row_version, int)
            or row_version < minimum_row_version
        ):
            raise RuntimeError("Gate 刷新后 Web 人工复核行版本无效")
        return row_version

    async def _load_existing_coverage_reference(
        self,
        state: WebInspectionReconciliationState,
    ) -> tuple[str, str] | None:
        """兼容读取历史不可变快照身份，不解析或改写旧页型协议。"""

        load_reference = getattr(self._snapshot_repository, "load_reference", None)
        if not callable(load_reference):
            return None
        reference = await load_reference(
            task_id=state.task_id,
            agent_run_id=state.agent_run_id,
            finalization_epoch=state.finalization_epoch,
        )
        if reference is None:
            return None
        if (
            not isinstance(reference, tuple)
            or len(reference) != 2
            or any(not isinstance(item, str) or not item.strip() for item in reference)
        ):
            raise RuntimeError("历史 Web Coverage Snapshot 引用无效")
        return reference

    async def _refresh_needs_review_gate_snapshot(
        self,
        state: WebInspectionReconciliationState,
        *,
        recovery_fence: ReconcileWebInspectionCommand,
    ) -> int:
        """在封存人工复核终态前，以排空后的事实刷新 Gate 审计快照。"""

        preparer = getattr(
            self._finalization,
            "completion_gate_preparer",
            None,
        )
        prepare = getattr(preparer, "prepare", None)
        evaluate = getattr(self._finalization, "evaluate_and_seal", None)
        if not callable(prepare) or not callable(evaluate):
            return state.task_row_version
        await prepare(
            task_id=state.task_id,
            agent_run_id=state.agent_run_id,
            finalization_epoch=state.finalization_epoch,
            **_recovery_fence_kwargs(recovery_fence),
        )
        decision = await evaluate(
            EvaluateCompletionGateCommand(
                task_id=state.task_id,
                agent_run_id=state.agent_run_id,
                expected_finalization_epoch=state.finalization_epoch,
                expected_row_version=state.task_row_version,
                execution_status=TaskExecutionStatus.NEEDS_REVIEW,
                quality_outcome=TaskQualityOutcome.UNKNOWN,
                recovery_lease_token=recovery_fence.recovery_lease_token,
                recovery_generation=recovery_fence.recovery_generation,
                recovery_lease_expires_at=(
                    recovery_fence.recovery_lease_expires_at
                ),
            )
        )
        gate_snapshot = getattr(decision, "snapshot", None)
        if getattr(gate_snapshot, "gate_passed", False):
            raise RuntimeError("存在开放人工复核时 Completion Gate 不得放行")
        finalization = getattr(decision, "finalization", None)
        row_version = getattr(finalization, "row_version", None)
        if isinstance(row_version, bool) or not isinstance(row_version, int):
            raise RuntimeError("Completion Gate 刷新后缺少任务行版本")
        return row_version


def _validate_current_state(
    state: WebInspectionReconciliationState,
    command: ReconcileWebInspectionCommand,
) -> None:
    if (
        state.task_id != command.task_id
        or state.agent_run_id != command.agent_run_id
        or state.finalization_epoch != command.finalization_epoch
        or not state.current_main_run
    ):
        raise RuntimeError("对账命令已不是当前 main Run 或 finalization epoch")
    if state.target_platform != "web":
        raise RuntimeError("Web 正式质检对账只允许 Web Task")
    if (
        state.execution_mode != "full_inspection"
        or state.notification_mode != "enforced"
    ):
        raise RuntimeError("Web 正式质检对账要求 full_inspection/enforced")


def _recovery_fence_kwargs(
    command: ReconcileWebInspectionCommand,
) -> dict[str, object]:
    if command.recovery_lease_token is None:
        return {}
    return {
        "recovery_lease_token": command.recovery_lease_token,
        "recovery_generation": command.recovery_generation,
        "recovery_lease_expires_at": command.recovery_lease_expires_at,
    }


def _failure_reason_code(state: WebInspectionReconciliationState) -> str:
    if state.failed_publication_count:
        return "WEB_INSPECTION_PUBLICATION_FAILED"
    if state.failed_manifest_count:
        return "WEB_EVIDENCE_MANIFEST_FAILED"
    if state.missing_assessment_count:
        return "WEB_PAGE_TYPE_ASSESSMENT_MISSING"
    return "WEB_INSPECTION_PUBLICATION_MISSING"


def _review_reason_code(
    state: WebInspectionReconciliationState,
    *,
    include_missing_assessment: bool = False,
) -> str:
    if state.review_publication_count:
        return "WEB_INSPECTION_PUBLICATION_NEEDS_REVIEW"
    if state.review_assessment_count or (
        include_missing_assessment and state.missing_assessment_count
    ):
        return "WEB_PAGE_TYPE_ASSESSMENT_NEEDS_REVIEW"
    return "WEB_RULE_REVIEW_REQUIRED"


def _sealed_execution_status(task_status: str) -> str:
    """把已封存任务投影为只读执行终态，禁止再次推进收口状态机。"""

    if task_status == "completed":
        return TaskExecutionStatus.COMPLETED.value
    if task_status == "failed":
        return TaskExecutionStatus.FAILED.value
    return TaskExecutionStatus.NEEDS_REVIEW.value


def _has_terminal_blocker(state: WebInspectionReconciliationState) -> bool:
    return bool(
        state.failed_manifest_count
        or state.missing_assessment_count
        or state.review_assessment_count
        or state.review_publication_count
        or state.failed_publication_count
    )


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} 必须为无首尾空白的非空字符串")
    return value


def _require_nonnegative(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} 必须为非负整数")
    return value
