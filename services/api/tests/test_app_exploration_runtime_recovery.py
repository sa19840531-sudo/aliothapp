"""App 探索 Runtime Checkpoint 与 Work 恢复合同测试。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.application.app_exploration_runtime_recovery import (
    AppExplorationRuntimeCheckpoint,
    AppExplorationRuntimeRecoveryError,
    AppExplorationWorkRecovery,
    build_app_exploration_runtime_checkpoint,
)
from services.api.app.application.exploration_work_queue import ExplorerWorkStatus
from services.api.app.domain.exploration import (
    CandidateAction,
    ExplorationBoundaries,
    ExplorationBudget,
    ExplorationTerminal,
    ExplorationTerminalStatus,
    FrontierSnapshot,
    SideEffectPolicy,
    SurfaceObservation,
    build_exploration_work,
)
from services.api.app.domain.interaction import (
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)

NOW = datetime(2026, 8, 14, 10, 0, tzinfo=UTC)


def _policy() -> dict[str, object]:
    return {
        "scan_mode": "full",
        "max_actions": 3,
        "nested": {"z": ["a", 2], "a": {"enabled": True}},
    }


def _checkpoint(
    *,
    policy: dict[str, object] | None = None,
    started_at: datetime = NOW,
    deadline: datetime = NOW + timedelta(minutes=30),
) -> AppExplorationRuntimeCheckpoint:
    return build_app_exploration_runtime_checkpoint(
        task_id="task-1",
        agent_run_id="run-1",
        surface_session_id="app:task-1",
        platform_build_ref="app-build:one",
        bootstrap_capture_id="capture-bootstrap",
        installed_package="com.example.target",
        runtime_policy=policy or _policy(),
        objective="覆盖关键页面",
        started_at=started_at,
        deadline=deadline,
        created_at=started_at,
    )


def _work(
    *,
    task_id: str = "task-1",
    session_id: str = "app:task-1",
    branch_id: str = "app:capture-source",
    capture_id: str = "capture-source",
    remaining_actions: int = 3,
    remaining_duration_ms: int = 1_800_000,
    deadline: datetime = NOW + timedelta(minutes=30),
    observed_at: datetime = NOW,
):
    frontier = FrontierSnapshot(
        snapshot_id=f"frontier-{capture_id}",
        observation=SurfaceObservation(
            capture_id=capture_id,
            surface_kind=SurfaceKind.ANDROID,
            location="app://com.example.target/.MainActivity",
            state_version="sha256:" + "a" * 64,
            observed_at=observed_at,
            page_state="stable",
        ),
        candidates=(
            CandidateAction(
                candidate_id=f"candidate-{capture_id}",
                semantic_target="关键入口",
                action_type=InteractionOperation.CLICK,
                risk_level=InteractionRiskLevel.LOW,
                exploration_value=90,
                navigation_depth=1,
                expected_surface_kind=SurfaceKind.ANDROID,
            ),
        ),
    )
    return build_exploration_work(
        task_id=task_id,
        session_id=session_id,
        branch_id=branch_id,
        objective="覆盖关键页面",
        surface_kind=SurfaceKind.ANDROID,
        source_capture_id=capture_id,
        frontier_snapshot_id=frontier.snapshot_id,
        frontier=frontier,
        boundaries=ExplorationBoundaries(
            allowed_hosts=("com.example.target",),
            allowed_operations=(InteractionOperation.CLICK,),
            max_navigation_depth=3,
            side_effect_policy=SideEffectPolicy.READ_ONLY,
        ),
        budget=ExplorationBudget(
            max_actions=3,
            remaining_actions=remaining_actions,
            max_depth=3,
            remaining_duration_ms=remaining_duration_ms,
        ),
        deadline=deadline,
    )


def _terminal(work, *, outcome: str = "exploration_completed") -> ExplorationTerminal:
    return ExplorationTerminal(
        work_id=work.work_id,
        task_id=work.task_id,
        status=ExplorationTerminalStatus.COMPLETED,
        reason="探索已完成",
        outcome=outcome,
        reason_code="exploration_completed",
        coverage_summary={"covered": 1},
    )


def test_checkpoint_hash_is_canonical_and_policy_embedded_hash_is_supported() -> None:
    first = _checkpoint()
    reordered = {
        "nested": {"a": {"enabled": True}, "z": ["a", 2]},
        "max_actions": 3,
        "scan_mode": "full",
    }
    second = _checkpoint(policy=reordered)

    assert first.runtime_policy_sha256 == second.runtime_policy_sha256
    assert first.objective_hash == second.objective_hash
    assert first.to_dict()["runtime_policy"] == _policy()

    embedded = dict(_policy())
    embedded["policy_hash"] = first.runtime_policy_sha256
    with_embedded = _checkpoint(policy=embedded)
    assert with_embedded.runtime_policy["policy_hash"] == first.runtime_policy_sha256


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("task_id", "task-other"),
        ("agent_run_id", "run-other"),
        ("surface_session_id", "app:task-other"),
        ("platform_build_ref", "app-build:other"),
        ("bootstrap_capture_id", "capture-other"),
        ("installed_package", "com.example.other"),
    ],
)
def test_checkpoint_rejects_identity_drift(field: str, value: str) -> None:
    checkpoint = _checkpoint()
    values = {
        "task_id": checkpoint.task_id,
        "agent_run_id": checkpoint.agent_run_id,
        "surface_session_id": checkpoint.surface_session_id,
        "platform_build_ref": checkpoint.platform_build_ref,
        "bootstrap_capture_id": checkpoint.bootstrap_capture_id,
        "installed_package": checkpoint.installed_package,
        "runtime_policy": checkpoint.runtime_policy,
        "objective": "覆盖关键页面",
    }
    values[field] = value

    with pytest.raises(AppExplorationRuntimeRecoveryError, match="身份或策略"):
        checkpoint.validate_runtime_identity(**values)


def test_checkpoint_rejects_policy_or_objective_hash_drift() -> None:
    checkpoint = _checkpoint()
    common = {
        "task_id": checkpoint.task_id,
        "agent_run_id": checkpoint.agent_run_id,
        "surface_session_id": checkpoint.surface_session_id,
        "platform_build_ref": checkpoint.platform_build_ref,
        "bootstrap_capture_id": checkpoint.bootstrap_capture_id,
        "installed_package": checkpoint.installed_package,
    }

    with pytest.raises(AppExplorationRuntimeRecoveryError, match="身份或策略"):
        checkpoint.validate_runtime_identity(
            **common,
            runtime_policy={**_policy(), "max_actions": 4},
            objective="覆盖另一个目标",
        )
    with pytest.raises(AppExplorationRuntimeRecoveryError, match="身份或策略"):
        checkpoint.validate_runtime_identity(
            **common,
            runtime_policy=_policy(),
            objective="覆盖另一个目标",
        )


def test_checkpoint_rejects_hash_format_mismatch_and_time_window_drift() -> None:
    checkpoint = _checkpoint()
    with pytest.raises(AppExplorationRuntimeRecoveryError, match="Runtime Policy hash"):
        replace(checkpoint, runtime_policy_sha256="sha256:" + "b" * 64)

    with pytest.raises(AppExplorationRuntimeRecoveryError, match="deadline"):
        replace(checkpoint, deadline=checkpoint.started_at)

    with pytest.raises(AppExplorationRuntimeRecoveryError, match="objective_hash"):
        replace(checkpoint, objective_hash="g" * 64)


def _valid_recovery() -> AppExplorationWorkRecovery:
    initial = _work()
    latest = _work(
        branch_id="app:capture-latest",
        capture_id="capture-latest",
        remaining_actions=1,
        remaining_duration_ms=900_000,
    )
    return AppExplorationWorkRecovery(
        initial_work=initial,
        latest_work=latest,
        terminal=None,
        terminal_status=None,
        terminal_error_code=None,
        consumed_turn_count=1,
        consumed_action_count=2,
        has_nonterminal_work=True,
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"initial_work": None},
        {"latest_work": None},
        {"terminal": _terminal(_work()), "terminal_status": None},
        {
            "terminal": None,
            "terminal_status": ExplorerWorkStatus.FAILED,
            "has_nonterminal_work": False,
        },
        {"terminal_status": ExplorerWorkStatus.PENDING},
        {
            "terminal": _terminal(_work()),
            "terminal_status": ExplorerWorkStatus.SUCCEEDED,
            "has_nonterminal_work": True,
        },
        {"consumed_turn_count": -1},
        {"consumed_action_count": -1},
        {"next_claim_at": datetime(2026, 8, 14, 10, 0)},
        {"has_nonterminal_work": "true"},
    ],
)
def test_work_recovery_invariants_are_fail_closed(changes: dict[str, object]) -> None:
    with pytest.raises(AppExplorationRuntimeRecoveryError):
        replace(_valid_recovery(), **changes)


def test_work_recovery_exposes_any_work_only_when_both_ends_exist() -> None:
    recovery = _valid_recovery()
    assert recovery.has_any_work is True
    assert recovery.initial_work is not None
    assert recovery.latest_work is not None


@pytest.mark.parametrize(
    "changes",
    [
        {"has_nonterminal_work": True},
        {"consumed_turn_count": 1},
        {"consumed_action_count": 1},
        {"terminal_error_code": "unexpected"},
        {"next_claim_at": NOW + timedelta(seconds=1)},
    ],
)
def test_empty_work_recovery_rejects_progress_facts(changes: dict[str, object]) -> None:
    values = {
        "initial_work": None,
        "latest_work": None,
        "terminal": None,
        "terminal_status": None,
        "terminal_error_code": None,
        "consumed_turn_count": 0,
        "consumed_action_count": 0,
        "has_nonterminal_work": False,
    }
    with pytest.raises(AppExplorationRuntimeRecoveryError):
        AppExplorationWorkRecovery(**{**values, **changes})
