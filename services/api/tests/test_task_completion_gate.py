"""任务完成门禁的八项合取、双维终态与快照身份测试。"""

import inspect
from datetime import UTC, datetime

import pytest

from services.api.app import worker as worker_module
from services.api.app.core.config import Settings, WorkflowSettings
from services.api.app.domain.task import (
    CompletionBlockReason,
    CompletionGateFacts,
    TaskExecutionStatus,
    TaskFinalizationSnapshot,
    TaskFinalizationState,
    TaskQualityOutcome,
    build_completion_gate_snapshot,
)
from services.api.app.infrastructure.db.task_finalization_repository import (
    PostgresTaskFinalizationRepository,
)
from services.api.app.infrastructure.queue.state_store import (
    InMemoryAgentRunStateStore,
    PostgresAgentRunStateStore,
)

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


def test_terminal_recovery_accepts_matching_presealed_legacy_status() -> None:
    """旧版本已写 review/failed 但未 sealed 时必须能够幂等续封。"""

    entry = inspect.getsource(PostgresTaskFinalizationRepository.terminate)
    assert "self._terminate(command=command)" in entry
    source = inspect.getsource(PostgresTaskFinalizationRepository._terminate)

    assert source.count("status IN ('running', $5)") == 2


@pytest.mark.parametrize(
    ("backend", "database_url", "state_store", "finalization_service", "message"),
    (
        ("noop", None, InMemoryAgentRunStateStore(), object(), "PostgreSQL"),
        ("postgres", None, PostgresAgentRunStateStore(), object(), "ALIOTH_DATABASE_URL"),
        (
            "postgres",
            "postgresql://example.test/alioth",
            PostgresAgentRunStateStore(),
            None,
            "Service",
        ),
    ),
)
def test_h5_only_completion_dependencies_fail_with_stable_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    database_url: str | None,
    state_store,
    finalization_service,
    message: str,
) -> None:
    monkeypatch.setenv("ALIOTH_AGENT_RUN_STATE_STORE_BACKEND", backend)
    if database_url is None:
        monkeypatch.delenv("ALIOTH_DATABASE_URL", raising=False)
    else:
        monkeypatch.setenv("ALIOTH_DATABASE_URL", database_url)

    with pytest.raises(worker_module.CompletionGateConfigurationError, match=message):
        worker_module.validate_completion_gate_configuration(
            Settings(workflow=WorkflowSettings(h5_v2_mode="on")),
            state_store=state_store,
            task_finalization_service=finalization_service,
        )


def test_all_completion_paths_off_remain_noop_compatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALIOTH_AGENT_RUN_STATE_STORE_BACKEND", "noop")
    monkeypatch.delenv("ALIOTH_DATABASE_URL", raising=False)

    worker_module.validate_completion_gate_configuration(
        Settings(workflow=WorkflowSettings()),
        state_store=InMemoryAgentRunStateStore(),
        task_finalization_service=None,
    )


def _ready_facts(**overrides) -> CompletionGateFacts:
    values = {
        "exploration_terminal": True,
        "pending_action_count": 0,
        "pending_capture_commit_count": 0,
        "nonterminal_enforced_work_item_count": 0,
        "active_session_request_count": 0,
        "candidate_rule_count": 3,
        "terminal_candidate_rule_count": 3,
        "background_execution_count": 0,
        "review_policy_satisfied": True,
        "open_review_count": 0,
        "review_generation": 2,
    }
    values.update(overrides)
    return CompletionGateFacts(**values)


def test_h5_coverage_identity_changes_gate_facts_hash() -> None:
    from services.api.app.domain.protocol import stable_protocol_hash

    first = _ready_facts(
        h5_coverage_snapshot_id="coverage-1",
        h5_coverage_snapshot_hash="hash-1",
        h5_coverage_snapshot_epoch=0,
    )
    second = _ready_facts(
        h5_coverage_snapshot_id="coverage-2",
        h5_coverage_snapshot_hash="hash-2",
        h5_coverage_snapshot_epoch=0,
    )
    assert first.gate_passed and second.gate_passed
    assert stable_protocol_hash(first.to_dict()) != stable_protocol_hash(second.to_dict())
    assert "h5_coverage_snapshot_id" not in _ready_facts().to_dict()


@pytest.mark.parametrize(
    "identity",
    [
        {"h5_coverage_snapshot_id": "coverage-1"},
        {"h5_coverage_snapshot_hash": "hash-1", "h5_coverage_snapshot_epoch": 0},
        {
            "h5_coverage_snapshot_id": "coverage-1",
            "h5_coverage_snapshot_hash": "hash-1",
            "h5_coverage_snapshot_epoch": -1,
        },
    ],
)
def test_h5_coverage_identity_must_be_complete(identity) -> None:
    with pytest.raises(ValueError):
        _ready_facts(**identity)


def test_all_eight_completion_facts_are_required() -> None:
    facts = _ready_facts()

    assert facts.gate_passed is True
    assert facts.block_reasons == ()
    assert facts.to_dict() == {
        "exploration_terminal": True,
        "no_pending_action": True,
        "no_pending_capture_commit": True,
        "no_nonterminal_enforced_work_item": True,
        "no_active_session_request": True,
        "candidate_rule_coverage_terminal": True,
        "candidate_inventory_satisfied": True,
        "no_background_execution": True,
        "review_policy_satisfied": True,
        "pending_action_count": 0,
        "pending_capture_commit_count": 0,
        "nonterminal_enforced_work_item_count": 0,
        "active_session_request_count": 0,
        "candidate_rule_count": 3,
        "terminal_candidate_rule_count": 3,
        "background_execution_count": 0,
        "open_review_count": 0,
        "review_generation": 2,
        "review_policy_decision_id": None,
        "require_nonempty_candidate_inventory": False,
        "no_rules_applicable": False,
        "app_coverage_satisfied": True,
        "h5_coverage_satisfied": True,
    }


def test_h5_coverage_scope_is_an_explicit_completion_gate_fact() -> None:
    facts = _ready_facts(h5_coverage_satisfied=False)

    assert facts.gate_passed is False
    assert CompletionBlockReason.H5_COVERAGE_SCOPE_INCOMPLETE in facts.block_reasons


def test_web_coverage_snapshot_is_a_dedicated_completion_gate_fact() -> None:
    facts = _ready_facts(
        web_coverage_required=True,
        web_coverage_satisfied=False,
    )

    assert facts.gate_passed is False
    assert facts.block_reasons == (
        CompletionBlockReason.WEB_COVERAGE_SNAPSHOT_INCOMPLETE,
    )
    assert facts.to_dict()["web_coverage_snapshot_id"] is None


def test_web_coverage_identity_is_bound_into_gate_facts() -> None:
    facts = _ready_facts(
        web_coverage_required=True,
        web_coverage_satisfied=True,
        web_coverage_snapshot_id="snapshot-web-1",
        web_coverage_snapshot_hash="a" * 64,
        web_coverage_snapshot_epoch=4,
        web_coverage_status="completed_with_gaps",
    )

    assert facts.gate_passed is True
    assert facts.to_dict() | {} == {
        **_ready_facts().to_dict(),
        "web_coverage_required": True,
        "web_coverage_satisfied": True,
        "web_coverage_snapshot_id": "snapshot-web-1",
        "web_coverage_snapshot_hash": "a" * 64,
        "web_coverage_snapshot_epoch": 4,
        "web_coverage_status": "completed_with_gaps",
    }


def test_non_web_gate_facts_keep_the_v1_hash_material_unchanged() -> None:
    facts = _ready_facts()

    assert build_completion_gate_snapshot(
        task_id="task-1",
        finalization_epoch=4,
        evaluation_sequence=2,
        facts=facts,
        evaluated_at=NOW,
    ).facts_hash == "da764d5f4b8e7a32d82b614b5431fb72cb8abe4cd8e7f968c7f81f6a2bf112e6"


def test_app_coverage_and_visual_quality_are_an_explicit_completion_gate_fact() -> None:
    facts = _ready_facts(app_coverage_satisfied=False)

    assert facts.gate_passed is False
    assert CompletionBlockReason.APP_COVERAGE_OR_VISUAL_QUALITY_INCOMPLETE in facts.block_reasons


def test_canceled_is_a_first_class_task_execution_status() -> None:
    assert TaskExecutionStatus.CANCELED.value == "canceled"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        (
            {"exploration_terminal": False},
            CompletionBlockReason.EXPLORATION_NOT_TERMINAL,
        ),
        ({"pending_action_count": 1}, CompletionBlockReason.PENDING_ACTION),
        (
            {"pending_capture_commit_count": 1},
            CompletionBlockReason.PENDING_CAPTURE_COMMIT,
        ),
        (
            {"nonterminal_enforced_work_item_count": 1},
            CompletionBlockReason.NONTERMINAL_ENFORCED_WORK_ITEM,
        ),
        (
            {"active_session_request_count": 1},
            CompletionBlockReason.ACTIVE_SESSION_REQUEST,
        ),
        (
            {"terminal_candidate_rule_count": 2},
            CompletionBlockReason.CANDIDATE_RULE_COVERAGE_NONTERMINAL,
        ),
        (
            {"background_execution_count": 1},
            CompletionBlockReason.BACKGROUND_EXECUTION,
        ),
        (
            {"review_policy_satisfied": False},
            CompletionBlockReason.REVIEW_POLICY_UNSATISFIED,
        ),
    ],
)
def test_each_html_completion_fact_blocks_independently(overrides, reason) -> None:
    facts = _ready_facts(**overrides)

    assert facts.gate_passed is False
    assert facts.block_reasons == (reason,)


def test_frontier_like_empty_candidate_inventory_does_not_replace_exploration_terminal() -> None:
    facts = _ready_facts(
        exploration_terminal=False,
        candidate_rule_count=0,
        terminal_candidate_rule_count=0,
    )

    assert facts.candidate_rule_coverage_terminal is True
    assert facts.candidate_inventory_satisfied is True
    assert facts.gate_passed is False
    assert facts.block_reasons == (CompletionBlockReason.EXPLORATION_NOT_TERMINAL,)


def test_app_empty_candidate_inventory_blocks_without_explicit_no_rules() -> None:
    facts = _ready_facts(
        candidate_rule_count=0,
        terminal_candidate_rule_count=0,
        require_nonempty_candidate_inventory=True,
        no_rules_applicable=False,
    )

    assert facts.candidate_rule_coverage_terminal is True
    assert facts.candidate_inventory_satisfied is False
    assert facts.gate_passed is False
    assert facts.block_reasons == (CompletionBlockReason.APP_CANDIDATE_INVENTORY_EMPTY,)


def test_app_empty_candidate_inventory_passes_with_explicit_no_rules() -> None:
    facts = _ready_facts(
        candidate_rule_count=0,
        terminal_candidate_rule_count=0,
        require_nonempty_candidate_inventory=True,
        no_rules_applicable=True,
    )

    assert facts.candidate_inventory_satisfied is True
    assert facts.gate_passed is True
    assert facts.block_reasons == ()


def test_gate_snapshot_identity_is_reproducible_and_binds_epoch() -> None:
    facts = _ready_facts()

    first = build_completion_gate_snapshot(
        task_id="task-1",
        finalization_epoch=4,
        evaluation_sequence=2,
        facts=facts,
        evaluated_at=NOW,
    )
    second = build_completion_gate_snapshot(
        task_id="task-1",
        finalization_epoch=4,
        evaluation_sequence=2,
        facts=facts,
        evaluated_at=NOW,
    )
    next_epoch = build_completion_gate_snapshot(
        task_id="task-1",
        finalization_epoch=5,
        evaluation_sequence=2,
        facts=facts,
        evaluated_at=NOW,
    )

    assert first.gate_snapshot_id == second.gate_snapshot_id
    assert first.facts_hash == second.facts_hash
    assert next_epoch.gate_snapshot_id != first.gate_snapshot_id


def test_execution_status_and_quality_outcome_are_separate_dimensions() -> None:
    assert TaskExecutionStatus.COMPLETED_WITH_GAPS.value == "completed_with_gaps"
    assert TaskExecutionStatus.FAILED.value == "failed"
    assert TaskQualityOutcome.FAIL.value == "fail"
    assert TaskQualityOutcome.UNKNOWN.value == "unknown"
    assert set(TaskExecutionStatus).isdisjoint(set(TaskQualityOutcome))


def test_terminal_candidate_count_cannot_exceed_inventory() -> None:
    with pytest.raises(ValueError, match="不能超过"):
        _ready_facts(candidate_rule_count=1, terminal_candidate_rule_count=2)


def test_nonsealed_finalization_cannot_publish_outcome_early() -> None:
    with pytest.raises(ValueError, match="不能提前携带"):
        TaskFinalizationSnapshot(
            task_id="task-1",
            state=TaskFinalizationState.SESSION_DRAINING,
            finalization_epoch=0,
            row_version=2,
            execution_status=TaskExecutionStatus.COMPLETED,
        )


def test_completion_gate_sql_keeps_h5_scope_and_adds_web_gap_inventory() -> None:
    from services.api.app.infrastructure.db.task_finalization_repository import (
        _GATE_FACTS_SQL,
    )

    compact = " ".join(_GATE_FACTS_SQL.split())
    assert (
        "task.task_config, task.ruleset_id, task.channel, task.finalization_epoch"
        in compact
    )
    assert "WHEN task.target_platform <> 'h5' THEN TRUE" in compact
    assert "web_coverage AS" in compact
    assert "FROM qc_web_coverage_snapshot AS snapshot" in compact
    assert "snapshot.agent_run_id = task.finalization_agent_run_id" in compact
    assert "snapshot.finalization_epoch = task.finalization_epoch" in compact
    assert "snapshot.coverage_status IN ('complete', 'completed_with_gaps')" in compact
    assert "web_gap_inventory AS" in compact
    assert "rule.input_contract->'target_clients' ? 'WEB'" in compact
    assert "rule.applicable_page_types ? missing.page_type" in compact
    assert "web-coverage-gap:" in compact
    assert "web_gap_inventory.candidate_count" in compact
    assert "web_gap_inventory.terminal_count" in compact


def test_completion_gate_independently_checks_web_publication_and_member_inventory() -> None:
    from services.api.app.infrastructure.db.task_finalization_repository import (
        _GATE_FACTS_SQL,
    )

    compact = " ".join(_GATE_FACTS_SQL.split())
    assert "web_publishable_inventory AS" in compact
    assert "task.target_platform = 'web'" in compact
    assert (
        "task.task_config->>'web_inspection_notification_mode' = 'enforced'"
        in compact
    )
    assert "capture.capture_status = 'captured'" in compact
    assert (
        "assessment.schema_version IN "
        "('web-page-type-assessment.v1', 'web-page-type-assessment.v2', "
        "'web-page-type-assessment.v3', 'web-page-type-assessment.v4')"
        in compact
    )
    assert "assessment.detection_status = 'recognized'" in compact
    assert "NOT IN ('superseded', 'abandoned')" in compact
    assert "after_manifest.quality_status = 'pass'" in compact
    assert "web_publication_gate AS" in compact
    assert (
        "publication.source_agent_run_id = task.finalization_agent_run_id"
        in compact
    )
    assert "publication.finalization_epoch = task.finalization_epoch" in compact
    assert "publication.mode = 'enforced'" in compact
    assert "qc_h5_inspection_publication_rule AS member" in compact
    assert "web_publication_gate.missing_publication_count" in compact
    assert "web_publication_gate.missing_member_count" in compact


def test_web_gate_matches_member_hash_to_formal_publisher_contract_snapshot() -> None:
    from services.api.app.infrastructure.db.task_finalization_repository import (
        _GATE_FACTS_SQL,
    )

    compact = " ".join(_GATE_FACTS_SQL.split())
    assert "rule.contract_hash" not in compact
    assert "qc_h5_inspection_publication_work_item AS contract_link" in compact
    assert "qc_inspection_work_item_rule AS expected_member" in compact
    assert "member.rule_id = expected_member.rule_id" in compact
    assert "member.rule_code = expected_member.rule_code" in compact
    assert "member.contract_hash = expected_member.contract_hash" in compact


def test_web_gate_counts_only_ledger_ready_capture_without_terminal_assessment() -> None:
    from services.api.app.infrastructure.db.task_finalization_repository import (
        _GATE_FACTS_SQL,
    )

    compact = " ".join(_GATE_FACTS_SQL.split())
    assert "web_raw_capture_inventory AS" in compact
    raw_inventory_sql = compact.split("web_raw_capture_inventory AS (", maxsplit=1)[
        1
    ].split("), web_classification_gate AS", maxsplit=1)[0]
    assert "qc_web_menu_ledger_entry AS ready_entry" in raw_inventory_sql
    assert "ready_entry.agent_run_id = task.finalization_agent_run_id" in (
        raw_inventory_sql
    )
    assert "ready_entry.menu_ready_capture_id = capture.id" in raw_inventory_sql
    assert "ready_entry.detail_ready_capture_id = capture.id" in raw_inventory_sql
    assert "web_classification_gate AS" in compact
    assert "LEFT JOIN qc_h5_page_type_assessment AS classification_assessment" in compact
    classification_gate_sql = compact.split("web_classification_gate AS (", maxsplit=1)[
        1
    ].split("), web_publishable_inventory AS", maxsplit=1)[0]
    assert "classification_assessment.detection_status = 'needs_review'" in (
        classification_gate_sql
    )
    assert "detection_status IN ('recognized', 'unrecognized')" not in (
        classification_gate_sql
    )
    assert "classification_assessment.assessment_record_id IS NULL" in compact
    assert "web_classification_gate.missing_assessment_count" in compact
    assert "web_classification_gate.review_assessment_count" in compact
    assert (
        "+ web_classification_gate.missing_assessment_count + "
        "web_classification_gate.review_assessment_count"
        in compact
    )


def test_web_gate_treats_unrecognized_as_terminal_but_never_publishable() -> None:
    from services.api.app.infrastructure.db.task_finalization_repository import (
        _GATE_FACTS_SQL,
    )

    compact = " ".join(_GATE_FACTS_SQL.split())
    classification_gate_sql = compact.split("web_classification_gate AS (", maxsplit=1)[
        1
    ].split("), web_publishable_inventory AS", maxsplit=1)[0]
    assert "classification_assessment.assessment_record_id IS NULL" in (
        classification_gate_sql
    )
    assert "classification_assessment.detection_status = 'needs_review'" in (
        classification_gate_sql
    )
    assert "assessment.detection_status = 'recognized'" in compact


def test_natural_evidence_presentation_violation_is_a_completion_gate_fact() -> None:
    facts = _ready_facts(natural_evidence_presentation_violation_count=2)

    assert facts.gate_passed is False
    assert facts.block_reasons == (
        CompletionBlockReason.NATURAL_EVIDENCE_PRESENTATION_VIOLATION,
    )
    assert facts.to_dict()["natural_evidence_presentation_violation_count"] == 2
    # 没有违规时不进入哈希材料，历史快照身份不变
    assert "natural_evidence_presentation_violation_count" not in _ready_facts().to_dict()
    with pytest.raises(ValueError):
        _ready_facts(natural_evidence_presentation_violation_count=-1)
