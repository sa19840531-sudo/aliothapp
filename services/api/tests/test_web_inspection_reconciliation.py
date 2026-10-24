"""Web 正式质检终态对账、Coverage Gap 与恢复合同。"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from services.api.app.application.rule_execution import RecordedRuleExecution
from services.api.app.application.web_inspection_reconciliation import (
    ReconcileWebInspectionCommand,
    WebCoverageGapRule,
    WebInspectionReconciliation,
    WebInspectionReconciliationState,
)
from services.api.app.domain.exploration.web_coverage_snapshot import (
    WebCoverageSnapshot,
)

TASK_ID = "00000000-0000-0000-0000-000000000001"
RUN_ID = "00000000-0000-0000-0000-000000000002"


def _state(**overrides: object) -> WebInspectionReconciliationState:
    values: dict[str, object] = {
        "task_id": TASK_ID,
        "agent_run_id": RUN_ID,
        "finalization_epoch": 4,
        "task_status": "running",
        "task_finalization_state": "open",
        "task_row_version": 7,
        "run_status": "running",
        "current_main_run": True,
        "execution_mode": "full_inspection",
        "notification_mode": "enforced",
        "exploration_terminal": True,
        "frontier_work_count": 0,
        "pending_capture_commit_count": 0,
        "pending_manifest_count": 0,
        "nonterminal_publication_count": 0,
        "review_publication_count": 0,
        "open_review_count": 0,
        "failed_publication_count": 0,
        "nonterminal_work_item_count": 0,
        "active_session_request_count": 0,
        "observed_page_types": ("content_list",),
        "unrecognized_page_types": (),
        "foundational_failure_code": None,
    }
    values.update(overrides)
    return WebInspectionReconciliationState(**values)


@pytest.mark.asyncio
async def test_sealed_task_is_an_idempotent_terminal_replay() -> None:
    """已封存任务不得再次终结账本、冻结快照或推进 Task 状态机。"""

    events: list[str] = []
    state = _state(
        task_status="review",
        task_finalization_state="sealed",
        task_row_version=11,
        run_status="done",
        open_review_count=3,
        review_assessment_count=2,
    )

    class NeverTerminalize:
        async def terminalize(self, **kwargs: object) -> None:
            raise AssertionError(kwargs)

    subject, snapshots, executions, _ = _service(
        StateReader(state),
        events=events,
        terminal_ledger_finalizer=NeverTerminalize(),
    )

    result = await subject.reconcile(
        ReconcileWebInspectionCommand(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            finalization_epoch=4,
        )
    )

    assert result.status == "already_terminal"
    assert result.execution_status == "needs_review"
    assert snapshots.snapshots == []
    assert executions.commands == []
    assert events == []


@pytest.mark.asyncio
async def test_open_rule_reviews_seal_web_task_as_needs_review() -> None:
    events: list[str] = []
    state = _state(
        task_finalization_state="session_draining",
        task_row_version=9,
        run_status="done",
        open_review_count=3,
    )
    class TerminalLedgerFinalizer:
        async def terminalize(self, **kwargs: object) -> None:
            assert kwargs["outcome"] == "needs_review"
            assert kwargs["reason_code"] == "WEB_RULE_REVIEW_REQUIRED"
            events.append("terminalize_formal_ledgers")

    subject, _snapshots, _executions, _ = _service(
        StateReader(state, state),
        events=events,
        terminal_ledger_finalizer=TerminalLedgerFinalizer(),
    )

    result = await subject.reconcile(
        ReconcileWebInspectionCommand(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            finalization_epoch=4,
        )
    )

    assert result.status == "needs_review"
    assert result.execution_status == "needs_review"
    assert events == [
        "terminalize_formal_ledgers",
        "freeze_snapshot",
        "load_gap_rules",
        "terminate:needs_review",
    ]


@pytest.mark.asyncio
async def test_open_reviews_refresh_gate_snapshot_before_needs_review_terminal() -> None:
    events: list[str] = []
    state = _state(
        task_finalization_state="session_draining",
        task_row_version=9,
        run_status="done",
        open_review_count=2,
    )

    class GatePreparer:
        async def prepare(self, **kwargs: object) -> None:
            assert kwargs == {
                "task_id": TASK_ID,
                "agent_run_id": RUN_ID,
                "finalization_epoch": 4,
            }
            events.append("prepare_completion_gate")

    class Finalization:
        completion_gate_preparer = GatePreparer()

        async def evaluate_and_seal(self, command):
            assert command.execution_status.value == "needs_review"
            assert command.quality_outcome.value == "unknown"
            assert command.expected_row_version == 9
            events.append("evaluate_completion_gate")
            return SimpleNamespace(
                snapshot=SimpleNamespace(gate_passed=False),
                finalization=SimpleNamespace(row_version=10),
            )

        async def terminate(self, command) -> None:
            assert command.expected_row_version == 10
            events.append(f"terminate:{command.execution_status.value}")

    class TerminalLedgerFinalizer:
        async def terminalize(self, **kwargs: object) -> None:
            del kwargs
            events.append("terminalize_formal_ledgers")

    subject, _snapshots, _executions, _ = _service(
        StateReader(state, state),
        events=events,
        finalization=Finalization(),
        terminal_ledger_finalizer=TerminalLedgerFinalizer(),
    )

    result = await subject.reconcile(
        ReconcileWebInspectionCommand(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            finalization_epoch=4,
        )
    )

    assert result.status == "needs_review"
    assert events == [
        "terminalize_formal_ledgers",
        "freeze_snapshot",
        "load_gap_rules",
        "prepare_completion_gate",
        "evaluate_completion_gate",
        "terminate:needs_review",
    ]


@pytest.mark.asyncio
async def test_review_terminal_reloads_latest_cas_version_after_gate_refresh() -> None:
    state = _state(
        task_finalization_state="session_draining",
        task_row_version=9,
        run_status="done",
        open_review_count=1,
    )

    class GatePreparer:
        async def prepare(self, **_kwargs: object) -> None:
            return None

    class Finalization:
        completion_gate_preparer = GatePreparer()

        async def evaluate_and_seal(self, _command):
            return SimpleNamespace(
                snapshot=SimpleNamespace(gate_passed=False),
                finalization=SimpleNamespace(row_version=10),
            )

        async def get(self, *, task_id: str):
            assert task_id == TASK_ID
            return SimpleNamespace(
                task_id=TASK_ID,
                agent_run_id=RUN_ID,
                finalization_epoch=4,
                row_version=11,
                state=SimpleNamespace(value="session_draining"),
            )

        async def terminate(self, command) -> None:
            assert command.expected_row_version == 11

    subject, _snapshots, _executions, _ = _service(
        StateReader(state, state),
        events=[],
        finalization=Finalization(),
    )

    result = await subject.reconcile(
        ReconcileWebInspectionCommand(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            finalization_epoch=4,
        )
    )

    assert result.status == "needs_review"


class StateReader:
    def __init__(self, *states: WebInspectionReconciliationState) -> None:
        self.states = list(states)
        self.calls: list[dict[str, object]] = []
        self.last_state: WebInspectionReconciliationState | None = None

    async def load_reconciliation_state(self, **kwargs: object):
        self.calls.append(kwargs)
        if not self.states:
            if self.last_state is None:
                raise AssertionError("状态读取次数超出安排")
            if self.last_state.task_finalization_state == "open":
                self.last_state = replace(
                    self.last_state,
                    task_finalization_state="exploration_draining",
                    task_row_version=self.last_state.task_row_version + 1,
                )
            return self.last_state
        self.last_state = self.states.pop(0)
        return self.last_state


class Recorder:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def finalize_all(self, **kwargs: object) -> None:
        del kwargs
        self.events.append("finalize_manifests")

    async def publish_pending(self, **kwargs: object) -> int:
        del kwargs
        self.events.append("publish_pending")
        return 2

    async def publish_pending_for_final_handoff(self, **kwargs: object) -> int:
        del kwargs
        self.events.append("publish_pending")
        return 2

    async def drain_before_close(self, **kwargs: object) -> None:
        del kwargs
        self.events.append("drain_active_session")

    async def close(self, **kwargs: object) -> None:
        del kwargs
        self.events.append("close_formal_session")

    async def begin_exploration_draining(self, **kwargs: object) -> None:
        del kwargs
        self.events.append("begin_exploration_draining")

    async def begin_session_draining(self, **kwargs: object) -> None:
        del kwargs
        self.events.append("begin_session_draining")

    async def terminate(self, command) -> None:
        self.events.append(f"terminate:{command.execution_status.value}")


class SnapshotRepository:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.snapshots: list[WebCoverageSnapshot] = []

    async def create(self, snapshot: WebCoverageSnapshot) -> WebCoverageSnapshot:
        self.events.append("freeze_snapshot")
        self.snapshots.append(snapshot)
        return snapshot


class ReplaySnapshotRepository(SnapshotRepository):
    async def create(self, snapshot: WebCoverageSnapshot) -> WebCoverageSnapshot:
        self.events.append("freeze_snapshot")
        if self.snapshots:
            return self.snapshots[0]
        self.snapshots.append(snapshot)
        return snapshot


class ExistingSnapshotReferenceRepository(SnapshotRepository):
    async def load_reference(self, **kwargs: object) -> tuple[str, str] | None:
        assert kwargs == {
            "task_id": TASK_ID,
            "agent_run_id": RUN_ID,
            "finalization_epoch": 4,
        }
        self.events.append("load_existing_snapshot")
        return ("historical-snapshot", "a" * 64)

    async def create(self, snapshot: WebCoverageSnapshot) -> WebCoverageSnapshot:
        raise AssertionError(snapshot)


class GapRules:
    def __init__(
        self,
        events: list[str],
        rules: tuple[WebCoverageGapRule, ...] = (),
    ) -> None:
        self.events = events
        self.rules = rules

    async def list_gap_rule_candidates(self, **kwargs: object):
        del kwargs
        self.events.append("load_gap_rules")
        return self.rules


class RuleExecutions:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.commands = []

    async def record_rule_execution(self, command):
        self.events.append(f"write_gap:{command.rule_code}")
        self.commands.append(command)
        return RecordedRuleExecution(
            execution_id=f"execution:{command.source_idempotency_key}",
            rule_id=str(command.rule_id),
            rule_code=str(command.rule_code),
            execution_status=command.execution_status,
            page_capture_id=command.page_capture_id,
        )


def _service(
    state_reader: StateReader,
    *,
    events: list[str] | None = None,
    rules: tuple[WebCoverageGapRule, ...] = (),
    snapshot_repository: SnapshotRepository | None = None,
    manifest_finalizer: object | None = None,
    publication_publisher: object | None = None,
    final_handoff: object | None = None,
    finalization: object | None = None,
    failed_work_finalizer: object | None = None,
    terminal_ledger_finalizer: object | None = None,
):
    recorded = events if events is not None else []
    stages = Recorder(recorded)
    snapshots = snapshot_repository or SnapshotRepository(recorded)
    executions = RuleExecutions(recorded)
    service_kwargs = {
        "state_reader": state_reader,
        "manifest_finalizer": manifest_finalizer or stages,
        "publication_publisher": publication_publisher or stages,
        "active_session_drainer": stages,
        "session_closer": stages,
        "finalization": finalization or stages,
        "snapshot_repository": snapshots,
        "gap_rule_reader": GapRules(recorded, rules),
        "rule_execution_service": executions,
        "final_handoff": final_handoff,
    }
    if failed_work_finalizer is not None:
        service_kwargs["failed_work_finalizer"] = failed_work_finalizer
    if terminal_ledger_finalizer is not None:
        service_kwargs["terminal_ledger_finalizer"] = terminal_ledger_finalizer
    return (
        WebInspectionReconciliation(**service_kwargs),
        snapshots,
        executions,
        recorded,
    )


async def test_one_reconciliation_notifies_final_handoff_only_once() -> None:
    events: list[str] = []

    class FinalHandoffNotification:
        async def publish_pending_for_final_handoff(self, **kwargs: object) -> int:
            assert kwargs == {
                "task_id": TASK_ID,
                "agent_run_id": RUN_ID,
                "finalization_epoch": 4,
            }
            events.append("notify_final_handoff")
            return 1

    notification = FinalHandoffNotification()
    service, _snapshots, _executions, _ = _service(
        StateReader(_state()),
        events=events,
        final_handoff=notification,
    )

    result = await service.reconcile(
        ReconcileWebInspectionCommand(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            finalization_epoch=4,
        )
    )

    assert result.status == "waiting_for_run_terminal"
    assert events.count("notify_final_handoff") == 1


async def test_reconciliation_uses_final_handoff_publication_port() -> None:
    events: list[str] = []

    class FinalHandoffPublisher:
        async def publish_pending_for_final_handoff(self, **kwargs: object) -> int:
            assert kwargs == {
                "task_id": TASK_ID,
                "agent_run_id": RUN_ID,
                "finalization_epoch": 4,
            }
            events.append("publish_pending")
            return 1

    service, _snapshots, _executions, _ = _service(
        StateReader(_state()),
        events=events,
        publication_publisher=FinalHandoffPublisher(),
    )

    result = await service.reconcile(
        ReconcileWebInspectionCommand(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            finalization_epoch=4,
        )
    )

    assert result.status == "waiting_for_run_terminal"
    assert events == [
        "begin_exploration_draining",
        "finalize_manifests",
        "publish_pending",
        "drain_active_session",
        "close_formal_session",
        "begin_session_draining",
    ]


@pytest.mark.parametrize(
    "blocked_state",
    (
        _state(exploration_terminal=False),
        _state(frontier_work_count=1),
        _state(pending_capture_commit_count=1),
    ),
)
async def test_reconciliation_never_freezes_gap_before_exploration_terminal(
    blocked_state: WebInspectionReconciliationState,
) -> None:
    service, snapshots, executions, events = _service(StateReader(blocked_state))

    result = await service.reconcile(
        ReconcileWebInspectionCommand(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            finalization_epoch=4,
        )
    )

    assert result.status == "waiting_for_exploration_terminal"
    assert snapshots.snapshots == []
    assert executions.commands == []
    assert events == []


async def test_done_run_recovers_budget_seal_before_final_handoff() -> None:
    events: list[str] = []

    class RecoverableStateReader(StateReader):
        async def seal_completed_budget(self, **kwargs: object) -> bool:
            assert kwargs == {
                "task_id": TASK_ID,
                "agent_run_id": RUN_ID,
                "finalization_epoch": 4,
            }
            events.append("seal_completed_budget")
            return True

    initial = _state(exploration_terminal=False, run_status="done")
    sealed = _state(exploration_terminal=True, run_status="done")
    service, _snapshots, _executions, _ = _service(
        RecoverableStateReader(initial, sealed),
        events=events,
    )

    result = await service.reconcile(
        ReconcileWebInspectionCommand(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            finalization_epoch=4,
        )
    )

    assert result.status == "waiting_for_run_terminal"
    assert events == [
        "seal_completed_budget",
        "begin_exploration_draining",
        "finalize_manifests",
        "publish_pending",
        "drain_active_session",
        "close_formal_session",
        "begin_session_draining",
    ]


async def test_reconciliation_orders_final_handoff_before_session_draining() -> None:
    events: list[str] = []
    service, snapshots, executions, _ = _service(
        StateReader(_state()),
        events=events,
    )

    result = await service.reconcile(
        ReconcileWebInspectionCommand(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            finalization_epoch=4,
        )
    )

    assert result.status == "waiting_for_run_terminal"
    assert events == [
        "begin_exploration_draining",
        "finalize_manifests",
        "publish_pending",
        "drain_active_session",
        "close_formal_session",
        "begin_session_draining",
    ]
    assert snapshots.snapshots == []
    assert executions.commands == []


async def test_pending_manifest_enters_draining_then_replays_finalizer() -> None:
    initial = _state(pending_manifest_count=2)
    finalized = _state(
        task_finalization_state="exploration_draining",
        task_row_version=8,
        pending_manifest_count=0,
    )
    events: list[str] = []
    service, _snapshots, _executions, _ = _service(
        StateReader(initial, finalized),
        events=events,
    )

    result = await service.reconcile(
        ReconcileWebInspectionCommand(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            finalization_epoch=4,
        )
    )

    assert result.status == "waiting_for_run_terminal"
    assert events == [
        "begin_exploration_draining",
        "finalize_manifests",
        "publish_pending",
        "drain_active_session",
        "close_formal_session",
        "begin_session_draining",
    ]


async def test_manifest_still_pending_after_replay_keeps_session_active() -> None:
    initial = _state(pending_manifest_count=1)
    still_pending = _state(
        task_finalization_state="exploration_draining",
        task_row_version=8,
        pending_manifest_count=1,
    )
    events: list[str] = []
    service, _snapshots, _executions, _ = _service(
        StateReader(initial, still_pending),
        events=events,
    )

    result = await service.reconcile(
        ReconcileWebInspectionCommand(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            finalization_epoch=4,
        )
    )

    assert result.status == "waiting_for_manifest_finalization"
    assert events == [
        "begin_exploration_draining",
        "finalize_manifests",
        "publish_pending",
    ]


async def test_exploration_draining_replays_final_handoff_after_crash() -> None:
    events: list[str] = []
    service, snapshots, executions, _ = _service(
        StateReader(_state(task_finalization_state="exploration_draining")),
        events=events,
    )

    result = await service.reconcile(
        ReconcileWebInspectionCommand(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            finalization_epoch=4,
        )
    )

    assert result.status == "waiting_for_run_terminal"
    assert events == [
        "finalize_manifests",
        "publish_pending",
        "drain_active_session",
        "close_formal_session",
        "begin_session_draining",
    ]
    assert snapshots.snapshots == []
    assert executions.commands == []


async def test_final_handoff_rereads_and_blocks_on_missing_publication_inventory() -> None:
    events: list[str] = []
    initial = _state(task_finalization_state="exploration_draining")
    unresolved = _state(
        task_finalization_state="exploration_draining",
        missing_publication_count=1,
    )
    reader = StateReader(initial, unresolved)
    service, snapshots, executions, _ = _service(reader, events=events)

    result = await service.reconcile(
        ReconcileWebInspectionCommand(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            finalization_epoch=4,
        )
    )

    assert result.status == "waiting_for_publication_inventory"
    assert len(reader.calls) == 2
    assert events == ["finalize_manifests", "publish_pending"]
    assert snapshots.snapshots == []
    assert executions.commands == []


async def test_failed_manifest_closes_session_before_authoritative_failure() -> None:
    initial = _state(failed_manifest_count=1, missing_publication_count=1)
    after_final_handoff = _state(
        task_finalization_state="exploration_draining",
        task_row_version=8,
        failed_manifest_count=1,
        missing_publication_count=1,
    )
    after_session_drain = _state(
        task_finalization_state="session_draining",
        run_status="done",
        task_row_version=9,
        failed_manifest_count=1,
        missing_publication_count=1,
    )
    events: list[str] = []
    service, snapshots, executions, _ = _service(
        StateReader(initial, after_final_handoff, after_session_drain),
        events=events,
    )
    command = ReconcileWebInspectionCommand(
        task_id=TASK_ID,
        agent_run_id=RUN_ID,
        finalization_epoch=4,
    )

    draining = await service.reconcile(command)

    assert draining.status == "waiting_for_run_terminal"
    assert events == [
        "begin_exploration_draining",
        "finalize_manifests",
        "publish_pending",
        "drain_active_session",
        "close_formal_session",
        "begin_session_draining",
    ]

    failed = await service.reconcile(command)

    assert failed.status == "failed"
    assert failed.execution_status == "failed"
    assert events[-1] == "terminate:failed"
    assert snapshots.snapshots == []
    assert executions.commands == []


async def test_transient_final_handoff_failure_keeps_session_active_for_retry() -> None:
    events: list[str] = []

    class TransientFinalHandoff:
        def __init__(self) -> None:
            self.calls = 0

        async def publish_pending_for_final_handoff(self, **_kwargs: object) -> int:
            self.calls += 1
            events.append("final_handoff")
            if self.calls == 1:
                raise RuntimeError("publication transient")
            return 1

    state = _state(task_finalization_state="exploration_draining")
    service, _snapshots, _executions, _ = _service(
        StateReader(state, state),
        events=events,
        final_handoff=TransientFinalHandoff(),
    )
    command = ReconcileWebInspectionCommand(
        task_id=TASK_ID,
        agent_run_id=RUN_ID,
        finalization_epoch=4,
    )

    with pytest.raises(RuntimeError, match="publication transient"):
        await service.reconcile(command)

    assert "close_formal_session" not in events
    retry = await service.reconcile(command)

    assert retry.status == "waiting_for_run_terminal"
    assert events == [
        "final_handoff",
        "final_handoff",
        "drain_active_session",
        "close_formal_session",
        "begin_session_draining",
    ]


async def test_failed_manifest_is_not_converted_into_coverage_gap() -> None:
    state = _state(
        task_finalization_state="session_draining",
        run_status="done",
        failed_manifest_count=1,
        observed_page_types=("content_list",),
    )
    service, snapshots, executions, events = _service(StateReader(state))

    result = await service.reconcile(
        ReconcileWebInspectionCommand(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            finalization_epoch=4,
        )
    )

    assert result.status == "failed"
    assert result.execution_status == "failed"
    assert snapshots.snapshots == []
    assert executions.commands == []
    assert events == ["terminate:failed"]


async def test_terminal_blocker_closes_other_formal_ledgers_before_task_terminal() -> None:
    initial = _state(
        task_finalization_state="session_draining",
        run_status="done",
        failed_manifest_count=1,
        missing_publication_count=1,
        nonterminal_publication_count=1,
        nonterminal_work_item_count=2,
    )
    terminalized = replace(
        initial,
        nonterminal_publication_count=0,
        nonterminal_work_item_count=0,
    )
    events: list[str] = []

    class TerminalLedgerFinalizer:
        async def terminalize(self, **kwargs: object) -> None:
            assert kwargs == {
                "task_id": TASK_ID,
                "agent_run_id": RUN_ID,
                "outcome": "failed",
                "reason_code": "WEB_EVIDENCE_MANIFEST_FAILED",
                "reason_message": "Web 正式质检终态已确认，收束未完成的持久账本",
            }
            events.append("terminalize_formal_ledgers")

    service, snapshots, executions, _ = _service(
        StateReader(initial, terminalized),
        events=events,
        terminal_ledger_finalizer=TerminalLedgerFinalizer(),
    )

    result = await service.reconcile(
        ReconcileWebInspectionCommand(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            finalization_epoch=4,
        )
    )

    assert result.status == "failed"
    assert result.execution_status == "failed"
    assert snapshots.snapshots == []
    assert executions.commands == []
    assert events == ["terminalize_formal_ledgers", "terminate:failed"]


async def test_terminal_blocker_invokes_ledger_even_when_snapshot_counter_is_zero() -> None:
    state = _state(
        task_finalization_state="session_draining",
        run_status="done",
        failed_manifest_count=1,
    )
    events: list[str] = []

    class TerminalLedgerFinalizer:
        async def terminalize(self, **_kwargs: object) -> None:
            events.append("terminalize_formal_ledgers")

    service, _snapshots, _executions, _ = _service(
        StateReader(state, state),
        events=events,
        terminal_ledger_finalizer=TerminalLedgerFinalizer(),
    )

    result = await service.reconcile(
        ReconcileWebInspectionCommand(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            finalization_epoch=4,
        )
    )

    assert result.status == "failed"
    assert events == ["terminalize_formal_ledgers", "terminate:failed"]


async def test_review_blocker_freezes_coverage_after_terminalizing_formal_ledgers() -> None:
    initial = _state(
        task_finalization_state="session_draining",
        run_status="done",
        review_assessment_count=1,
        nonterminal_publication_count=1,
        nonterminal_work_item_count=1,
    )
    terminalized = replace(
        initial,
        nonterminal_publication_count=0,
        nonterminal_work_item_count=0,
        review_publication_count=1,
    )
    events: list[str] = []

    class TerminalLedgerFinalizer:
        async def terminalize(self, **kwargs: object) -> None:
            assert kwargs["outcome"] == "needs_review"
            assert kwargs["reason_code"] == "WEB_PAGE_TYPE_ASSESSMENT_NEEDS_REVIEW"
            events.append("terminalize_formal_ledgers")

    service, snapshots, _executions, _ = _service(
        StateReader(initial, terminalized),
        events=events,
        terminal_ledger_finalizer=TerminalLedgerFinalizer(),
    )

    result = await service.reconcile(
        ReconcileWebInspectionCommand(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            finalization_epoch=4,
        )
    )

    assert result.status == "needs_review"
    assert result.execution_status == "needs_review"
    assert len(snapshots.snapshots) == 1
    assert snapshots.snapshots[0].coverage_status == "completed_with_gaps"
    assert events == [
        "terminalize_formal_ledgers",
        "freeze_snapshot",
        "load_gap_rules",
        "terminate:needs_review",
    ]


async def test_missing_page_type_assessment_fails_closed_before_snapshot() -> None:
    state = _state(
        task_finalization_state="session_draining",
        run_status="done",
        missing_assessment_count=1,
        observed_page_types=(),
    )
    service, snapshots, executions, events = _service(StateReader(state))

    result = await service.reconcile(
        ReconcileWebInspectionCommand(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            finalization_epoch=4,
        )
    )

    assert result.status == "failed"
    assert result.execution_status == "failed"
    assert snapshots.snapshots == []
    assert executions.commands == []
    assert events == ["terminate:failed"]


async def test_legacy_review_task_preserves_review_during_session_draining_recovery() -> None:
    """旧流程已提交 review 时，对账不得因派生库存缺口改判 failed。"""

    state = _state(
        task_status="review",
        task_finalization_state="session_draining",
        run_status="done",
        missing_assessment_count=11,
        open_review_count=84,
        observed_page_types=(),
    )
    service, snapshots, executions, events = _service(StateReader(state))

    result = await service.reconcile(
        ReconcileWebInspectionCommand(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            finalization_epoch=4,
        )
    )

    assert result.status == "needs_review"
    assert result.execution_status == "needs_review"
    assert len(snapshots.snapshots) == 1
    assert executions.commands == []
    assert events == [
        "freeze_snapshot",
        "load_gap_rules",
        "terminate:needs_review",
    ]


async def test_legacy_review_reuses_existing_immutable_coverage_snapshot() -> None:
    """历史协议快照不可覆盖；恢复只复用其身份完成 review 密封。"""

    state = _state(
        task_status="review",
        task_finalization_state="session_draining",
        run_status="done",
        missing_assessment_count=11,
        open_review_count=84,
        observed_page_types=(),
    )
    events: list[str] = []
    snapshots = ExistingSnapshotReferenceRepository(events)
    service, _snapshots, executions, _events = _service(
        StateReader(state),
        events=events,
        snapshot_repository=snapshots,
    )

    result = await service.reconcile(
        ReconcileWebInspectionCommand(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            finalization_epoch=4,
        )
    )

    assert result.status == "needs_review"
    assert result.coverage_snapshot_id == "historical-snapshot"
    assert result.coverage_snapshot_hash == "a" * 64
    assert executions.commands == []
    assert events == ["load_existing_snapshot", "terminate:needs_review"]


@pytest.mark.parametrize(
    ("state_overrides", "expected_status", "expected_execution_status"),
    (
        ({"review_publication_count": 1}, "needs_review", "needs_review"),
        ({"failed_publication_count": 1}, "failed", "failed"),
    ),
)
async def test_terminal_publication_blocker_persists_authoritative_task_outcome(
    state_overrides: dict[str, int],
    expected_status: str,
    expected_execution_status: str,
) -> None:
    state = _state(
        task_finalization_state="session_draining",
        run_status="done",
        **state_overrides,
    )
    service, snapshots, executions, events = _service(StateReader(state))

    result = await service.reconcile(
        ReconcileWebInspectionCommand(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            finalization_epoch=4,
        )
    )

    assert result.status == expected_status
    assert result.execution_status == expected_execution_status
    if expected_status == "needs_review":
        assert events == [
            "freeze_snapshot",
            "load_gap_rules",
            "terminate:needs_review",
        ]
        assert len(snapshots.snapshots) == 1
    else:
        assert events == ["terminate:failed"]
        assert snapshots.snapshots == []
    assert executions.commands == []


async def test_classification_review_freezes_snapshot_before_task_review() -> None:
    state = _state(
        task_finalization_state="session_draining",
        run_status="done",
        missing_assessment_count=0,
        review_assessment_count=1,
        observed_page_types=(),
        unrecognized_page_types=(),
    )
    service, snapshots, executions, events = _service(StateReader(state))

    result = await service.reconcile(
        ReconcileWebInspectionCommand(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            finalization_epoch=4,
        )
    )

    assert result.status == "needs_review"
    assert result.status != "gate_ready"
    assert result.execution_status == "needs_review"
    assert len(snapshots.snapshots) == 1
    assert snapshots.snapshots[0].coverage_status == "completed_with_gaps"
    assert executions.commands == []
    assert events == [
        "freeze_snapshot",
        "load_gap_rules",
        "terminate:needs_review",
    ]


async def test_classification_review_records_coverage_gap_before_task_review() -> None:
    state = _state(
        task_finalization_state="session_draining",
        run_status="done",
        review_assessment_count=1,
        observed_page_types=("content_list",),
        unrecognized_page_types=("web_unknown",),
    )
    rules = (
        WebCoverageGapRule(
            rule_id="rule-detail-a",
            rule_code="alioth_6",
            missing_page_type="content_detail",
        ),
    )
    service, snapshots, executions, events = _service(
        StateReader(state),
        rules=rules,
    )

    result = await service.reconcile(
        ReconcileWebInspectionCommand(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            finalization_epoch=4,
        )
    )

    snapshot = snapshots.snapshots[0]
    assert result.status == "needs_review"
    assert result.coverage_snapshot_id == snapshot.snapshot_id
    assert snapshot.unrecognized_page_types == ("web_unknown",)
    assert [command.execution_status for command in executions.commands] == [
        "evidence_insufficient"
    ]
    assert executions.commands[0].create_review_item is False
    assert events == [
        "freeze_snapshot",
        "load_gap_rules",
        "write_gap:alioth_6",
        "terminate:needs_review",
    ]


async def test_review_assessment_drains_session_before_returning_needs_review() -> None:
    initial = _state(
        task_finalization_state="exploration_draining",
        review_assessment_count=1,
    )
    after_final_handoff = _state(
        task_finalization_state="exploration_draining",
        review_assessment_count=1,
    )
    after_session_drain = _state(
        task_finalization_state="session_draining",
        run_status="done",
        review_assessment_count=1,
    )
    reader = StateReader(initial, after_final_handoff, after_session_drain)
    events: list[str] = []
    service, snapshots, executions, _ = _service(reader, events=events)
    command = ReconcileWebInspectionCommand(
        task_id=TASK_ID,
        agent_run_id=RUN_ID,
        finalization_epoch=4,
    )

    draining_result = await service.reconcile(command)

    assert draining_result.status == "waiting_for_run_terminal"
    assert events == [
        "finalize_manifests",
        "publish_pending",
        "drain_active_session",
        "close_formal_session",
        "begin_session_draining",
    ]
    assert snapshots.snapshots == []
    assert executions.commands == []

    review_result = await service.reconcile(command)

    assert review_result.status == "needs_review"
    assert review_result.execution_status == "needs_review"
    assert len(snapshots.snapshots) == 1
    assert snapshots.snapshots[0].coverage_status == "completed_with_gaps"
    assert executions.commands == []
    assert events == [
        "finalize_manifests",
        "publish_pending",
        "drain_active_session",
        "close_formal_session",
        "begin_session_draining",
        "freeze_snapshot",
        "load_gap_rules",
        "terminate:needs_review",
    ]


async def test_terminal_unrecognized_page_type_remains_a_reportable_coverage_gap() -> None:
    state = _state(
        task_finalization_state="session_draining",
        run_status="done",
        observed_page_types=("content_list",),
        unrecognized_page_types=("web_unknown",),
        missing_assessment_count=0,
    )
    service, snapshots, executions, _events = _service(StateReader(state))

    result = await service.reconcile(
        ReconcileWebInspectionCommand(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            finalization_epoch=4,
        )
    )

    assert result.status == "gate_ready"
    assert result.execution_status == "completed_with_gaps"
    assert snapshots.snapshots[0].unrecognized_page_types == ("web_unknown",)
    assert executions.commands == []


async def test_reconciliation_waits_for_all_publication_and_work_before_snapshot() -> None:
    state = _state(
        task_finalization_state="session_draining",
        run_status="done",
        nonterminal_publication_count=1,
    )
    service, snapshots, executions, events = _service(StateReader(state))

    result = await service.reconcile(
        ReconcileWebInspectionCommand(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            finalization_epoch=4,
        )
    )

    assert result.status == "waiting_for_formal_work"
    assert snapshots.snapshots == []
    assert executions.commands == []
    assert events == []


async def test_gap_writer_uses_formal_rule_service_without_capture_or_review() -> None:
    state = _state(
        task_finalization_state="session_draining",
        run_status="done",
        observed_page_types=("content_list", "video_list"),
        unrecognized_page_types=("raw_unknown",),
    )
    rules = (
        WebCoverageGapRule(
            rule_id="rule-detail-a",
            rule_code="alioth_6",
            missing_page_type="content_detail",
        ),
        WebCoverageGapRule(
            rule_id="rule-video-a",
            rule_code="alioth_7",
            missing_page_type="video_detail",
        ),
    )
    service, snapshots, executions, events = _service(
        StateReader(state),
        rules=rules,
    )

    result = await service.reconcile(
        ReconcileWebInspectionCommand(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            finalization_epoch=4,
        )
    )

    snapshot = snapshots.snapshots[0]
    assert result.status == "gate_ready"
    assert result.execution_status == "completed_with_gaps"
    assert snapshot.missing_page_types == ("content_detail", "home", "video_detail")
    assert snapshot.unrecognized_page_types == ("raw_unknown",)
    assert events == [
        "freeze_snapshot",
        "load_gap_rules",
        "write_gap:alioth_6",
        "write_gap:alioth_7",
    ]
    assert [command.page_capture_id for command in executions.commands] == [None, None]
    assert [command.execution_status for command in executions.commands] == [
        "evidence_insufficient",
        "evidence_insufficient",
    ]
    assert all(command.create_review_item is False for command in executions.commands)
    assert [command.source_idempotency_key for command in executions.commands] == [
        f"web-coverage-gap:{snapshot.snapshot_id}:content_detail:alioth_6",
        f"web-coverage-gap:{snapshot.snapshot_id}:video_detail:alioth_7",
    ]
    assert all(
        command.judge_input["schema_version"] == "web-coverage-gap.v1"
        and command.judge_input["source"] == "web_coverage_gap"
        and command.judge_input["coverage_snapshot_id"] == snapshot.snapshot_id
        and command.judge_input["reason"] == "required_page_type_not_discovered"
        for command in executions.commands
    )


async def test_gap_replay_reuses_persisted_snapshot_and_source_identity() -> None:
    state = _state(
        task_finalization_state="session_draining",
        run_status="done",
        observed_page_types=("content_list", "video_list"),
    )
    rules = (
        WebCoverageGapRule(
            rule_id="rule-detail-a",
            rule_code="alioth_6",
            missing_page_type="content_detail",
        ),
    )
    events: list[str] = []
    persisted_snapshots = ReplaySnapshotRepository(events)
    service, _, executions, _ = _service(
        StateReader(state, state),
        events=events,
        rules=rules,
        snapshot_repository=persisted_snapshots,
    )
    command = ReconcileWebInspectionCommand(
        task_id=TASK_ID,
        agent_run_id=RUN_ID,
        finalization_epoch=4,
    )

    first = await service.reconcile(command)
    second = await service.reconcile(command)

    assert first.coverage_snapshot_id == second.coverage_snapshot_id
    assert [item.source_idempotency_key for item in executions.commands] == [
        executions.commands[0].source_idempotency_key,
        executions.commands[0].source_idempotency_key,
    ]
    assert all(item.create_review_item is False for item in executions.commands)


async def test_missing_page_type_with_zero_rules_is_still_gate_ready() -> None:
    state = _state(
        task_finalization_state="session_draining",
        run_status="done",
        observed_page_types=("content_list",),
    )
    service, snapshots, executions, events = _service(StateReader(state))

    result = await service.reconcile(
        ReconcileWebInspectionCommand(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            finalization_epoch=4,
        )
    )

    assert result.status == "gate_ready"
    assert result.execution_status == "completed_with_gaps"
    assert snapshots.snapshots[0].missing_page_types
    assert executions.commands == []
    assert events == ["freeze_snapshot", "load_gap_rules"]


async def test_complete_snapshot_maps_to_completed_task_execution_status() -> None:
    state = _state(
        task_finalization_state="session_draining",
        run_status="done",
        observed_page_types=(
            "home",
            "content_list",
            "content_detail",
            "video_list",
            "video_detail",
        ),
    )
    service, snapshots, executions, _ = _service(StateReader(state))

    result = await service.reconcile(
        ReconcileWebInspectionCommand(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            finalization_epoch=4,
        )
    )

    assert snapshots.snapshots[0].coverage_status == "complete"
    assert result.status == "gate_ready"


@pytest.mark.asyncio
async def test_catalog_applicability_excludes_absent_types_from_gate_denominator() -> None:
    state = _state(
        task_finalization_state="session_draining",
        task_row_version=9,
        run_status="done",
        observed_page_types=("video_detail", "video_list"),
        required_page_types=("video_detail", "video_list"),
        optional_page_types=(),
        not_applicable_page_types=("content_detail", "content_list"),
        applicability_mode="catalog_declared",
        catalog_version="2026-09-05.1",
        catalog_hash="a" * 64,
    )
    service, snapshots, executions, _ = _service(StateReader(state))

    result = await service.reconcile(
        ReconcileWebInspectionCommand(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            finalization_epoch=4,
        )
    )

    snapshot = snapshots.snapshots[0]
    assert result.status == "gate_ready"
    assert result.execution_status == "completed"
    assert snapshot.coverage_status == "complete"
    assert snapshot.normalization_version == "web-page-type-assessment.v2"
    assert snapshot.snapshot_protocol_version == "web-coverage-snapshot.v2"
    assert snapshot.missing_page_types == ()
    assert snapshot.not_applicable_page_types == (
        "content_detail",
        "content_list",
    )
    assert executions.commands == []
    assert result.execution_status == "completed"
    assert executions.commands == []


@pytest.mark.asyncio
@pytest.mark.parametrize("normalization,protocol", [
    ("web-page-type-normalization.v1", "web-coverage-snapshot.v1"),
    ("web-page-type-assessment.v2", "web-coverage-snapshot.v2"),
])
async def test_restart_reuses_exact_frozen_coverage_despite_new_live_facts(normalization, protocol):
    frozen = WebCoverageSnapshot.create(
        task_id=TASK_ID, agent_run_id=RUN_ID, finalization_epoch=4,
        required_page_types=("video_list",), observed_page_types=("video_list",),
        unrecognized_page_types=(), normalization_version=normalization,
        snapshot_protocol_version=protocol,
    )
    state = _state(task_finalization_state="session_draining", run_status="done",
                   frozen_coverage_snapshot=frozen)
    service, snapshots, _, _ = _service(StateReader(state))
    result = await service.reconcile(ReconcileWebInspectionCommand(
        task_id=TASK_ID, agent_run_id=RUN_ID, finalization_epoch=4))
    assert snapshots.snapshots[0] == frozen
    assert result.coverage_snapshot_hash == frozen.snapshot_hash


async def test_foundational_failure_never_becomes_coverage_gap() -> None:
    state = _state(
        task_finalization_state="session_draining",
        run_status="failed",
        foundational_failure_code="WEB_HOME_UNREACHABLE",
        observed_page_types=(),
    )
    service, snapshots, executions, events = _service(StateReader(state))

    result = await service.reconcile(
        ReconcileWebInspectionCommand(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            finalization_epoch=4,
        )
    )

    assert result.status == "failed"
    assert result.execution_status == "failed"
    assert snapshots.snapshots[0].coverage_status == "failed"
    assert executions.commands == []
    assert events == ["freeze_snapshot", "terminate:failed"]


async def test_foundational_failure_precedes_all_derived_inventory_blockers() -> None:
    state = _state(
        task_finalization_state="session_draining",
        run_status="failed",
        foundational_failure_code="WEB_BROWSER_BOOTSTRAP_FAILED",
        pending_manifest_count=1,
        missing_assessment_count=1,
        review_assessment_count=1,
        missing_publication_count=1,
        review_publication_count=1,
        failed_manifest_count=1,
        failed_publication_count=1,
        nonterminal_publication_count=1,
        nonterminal_work_item_count=1,
        active_session_request_count=1,
        observed_page_types=(),
    )
    class Finalization:
        def __init__(self) -> None:
            self.commands = []

        async def terminate(self, command) -> None:
            self.commands.append(command)
            events.append(f"terminate:{command.execution_status.value}")

    events: list[str] = []
    finalization = Finalization()

    class TerminalLedgerFinalizer:
        async def terminalize(self, **kwargs: object) -> None:
            assert kwargs["outcome"] == "failed"
            assert kwargs["reason_code"] == "WEB_BROWSER_BOOTSTRAP_FAILED"
            events.append("terminalize_formal_ledgers")

    after_ledgers = replace(
        state,
        nonterminal_work_item_count=0,
        nonterminal_publication_count=0,
    )
    after_active_request = replace(after_ledgers, active_session_request_count=0)
    service, snapshots, executions, _ = _service(
        StateReader(state, after_ledgers, after_active_request),
        events=events,
        finalization=finalization,
        terminal_ledger_finalizer=TerminalLedgerFinalizer(),
    )

    waiting_for_active_request = await service.reconcile(
        ReconcileWebInspectionCommand(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            finalization_epoch=4,
        )
    )
    result = await service.reconcile(
        ReconcileWebInspectionCommand(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            finalization_epoch=4,
        )
    )

    assert waiting_for_active_request.status == "waiting_for_formal_work"
    assert result.status == "failed"
    assert result.execution_status == "failed"
    assert snapshots.snapshots[0].coverage_status == "failed"
    assert executions.commands == []
    assert events == [
        "terminalize_formal_ledgers",
        "terminalize_formal_ledgers",
        "freeze_snapshot",
        "terminate:failed",
    ]
    assert finalization.commands[0].reason_code == "WEB_BROWSER_BOOTSTRAP_FAILED"


async def test_foundational_failure_terminalizes_enforced_work_before_sealing() -> None:
    initial = _state(
        task_finalization_state="session_draining",
        run_status="failed",
        foundational_failure_code="WEB_BROWSER_BOOTSTRAP_FAILED",
        nonterminal_work_item_count=1,
        observed_page_types=(),
    )
    after_work_finalization = replace(initial, nonterminal_work_item_count=0)
    events: list[str] = []

    class FailedWorkFinalizer:
        async def finalize_task_work(self, **kwargs: object) -> None:
            assert kwargs == {
                "task_id": TASK_ID,
                "error_code": "WEB_BROWSER_BOOTSTRAP_FAILED",
                "error_message": "Web 基础运行失败，收束未完成的正式质检工作",
            }
            events.append("finalize_failed_work")

    service, snapshots, executions, _ = _service(
        StateReader(initial, after_work_finalization),
        events=events,
        failed_work_finalizer=FailedWorkFinalizer(),
    )

    result = await service.reconcile(
        ReconcileWebInspectionCommand(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            finalization_epoch=4,
        )
    )

    assert result.status == "failed"
    assert snapshots.snapshots[0].coverage_status == "failed"
    assert executions.commands == []
    assert events == ["finalize_failed_work", "freeze_snapshot", "terminate:failed"]


async def test_foundational_failure_without_session_or_budget_reaches_failed() -> None:
    initial = _state(
        exploration_terminal=False,
        foundational_failure_code="WEB_HOME_UNREACHABLE",
        observed_page_types=(),
    )
    after_handoff = _state(
        task_finalization_state="exploration_draining",
        task_row_version=8,
        exploration_terminal=False,
        foundational_failure_code="WEB_HOME_UNREACHABLE",
        observed_page_types=(),
    )
    after_session_drain = _state(
        task_finalization_state="session_draining",
        task_row_version=9,
        exploration_terminal=False,
        run_status="failed",
        foundational_failure_code="WEB_HOME_UNREACHABLE",
        observed_page_types=(),
    )
    events: list[str] = []
    service, snapshots, executions, _ = _service(
        StateReader(initial, after_handoff, after_session_drain),
        events=events,
    )
    command = ReconcileWebInspectionCommand(
        task_id=TASK_ID,
        agent_run_id=RUN_ID,
        finalization_epoch=4,
    )

    draining = await service.reconcile(command)
    failed = await service.reconcile(command)

    assert draining.status == "waiting_for_run_terminal"
    assert failed.status == "failed"
    assert failed.execution_status == "failed"
    assert snapshots.snapshots[0].coverage_status == "failed"
    assert executions.commands == []
    assert events == [
        "begin_exploration_draining",
        "finalize_manifests",
        "publish_pending",
        "drain_active_session",
        "close_formal_session",
        "begin_session_draining",
        "freeze_snapshot",
        "terminate:failed",
    ]


async def test_foundational_failure_ignores_unclassifiable_pending_manifest() -> None:
    """基础失败的诊断采集不可因无法分类的 Manifest 永久阻塞失败收口。"""

    draining = _state(
        task_finalization_state="exploration_draining",
        task_row_version=8,
        run_status="failed",
        foundational_failure_code="WEB_FOUNDATIONAL_FAILURE",
        pending_manifest_count=1,
        observed_page_types=(),
    )
    session_draining = replace(
        draining,
        task_finalization_state="session_draining",
        task_row_version=9,
    )
    events: list[str] = []
    service, snapshots, executions, _ = _service(
        StateReader(draining, draining, session_draining),
        events=events,
    )
    command = ReconcileWebInspectionCommand(
        task_id=TASK_ID,
        agent_run_id=RUN_ID,
        finalization_epoch=4,
    )

    handoff = await service.reconcile(command)
    failed = await service.reconcile(command)

    assert handoff.status == "waiting_for_run_terminal"
    assert failed.status == "failed"
    assert failed.execution_status == "failed"
    assert snapshots.snapshots[0].coverage_status == "failed"
    assert executions.commands == []
    assert events == [
        "finalize_manifests",
        "publish_pending",
        "drain_active_session",
        "close_formal_session",
        "begin_session_draining",
        "freeze_snapshot",
        "terminate:failed",
    ]


async def test_reconciliation_rejects_stale_run_or_epoch_before_side_effects() -> None:
    stale = replace(_state(), current_main_run=False)
    service, snapshots, executions, events = _service(StateReader(stale))

    with pytest.raises(RuntimeError, match="当前 main Run"):
        await service.reconcile(
            ReconcileWebInspectionCommand(
                task_id=TASK_ID,
                agent_run_id=RUN_ID,
                finalization_epoch=4,
            )
        )

    assert snapshots.snapshots == []
    assert executions.commands == []
    assert events == []
