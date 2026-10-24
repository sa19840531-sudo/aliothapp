"""聚合质检发布 PostgreSQL 仓储 SQL 合同。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.domain.evidence import (
    AppExplorationResult,
    AppExplorationResultOutcome,
)
from services.api.app.domain.exploration.web_coverage_snapshot import (
    WebCoverageSnapshot,
)
from services.api.app.infrastructure.db.aggregate_inspection_publication_repository import (
    AggregateInspectionPublicationPersistenceError,
    PostgresAggregateInspectionPublicationRepository,
)


class _Context:
    def __init__(self, value: Any) -> None:
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakePool:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    def acquire(self) -> _Context:
        return _Context(self.connection)


class FakeConnection:
    def __init__(self, *, task_row: dict[str, Any] | None = None) -> None:
        self.queries: list[tuple[str, tuple[Any, ...]]] = []
        self.task_row = task_row or {
            "task_id": "11111111-1111-1111-1111-111111111111",
            "source_agent_run_id": "22222222-2222-2222-2222-222222222222",
            "finalization_epoch": 3,
            "ruleset_snapshot_hash": "snapshot-hash",
        }

    def transaction(self) -> _Context:
        return _Context(self)

    async def execute(self, query: str, *args: Any) -> str:
        self.queries.append((" ".join(query.split()), args))
        return "OK"

    async def fetchrow(self, query: str, *args: Any):
        self.queries.append((" ".join(query.split()), args))
        return self.task_row

    async def fetch(self, query: str, *args: Any):
        self.queries.append((" ".join(query.split()), args))
        return []

    async def fetchval(self, query: str, *args: Any):
        self.queries.append((" ".join(query.split()), args))
        return "55555555-5555-5555-5555-555555555555"


def _disposition_snapshot_and_rule():
    from services.api.app.application.aggregate_inspection_publication import (
        AggregatePublicationSnapshot,
        build_aggregate_rule_source,
    )

    rule = build_aggregate_rule_source(
        {
            "rule_id": "44444444-4444-4444-4444-444444444444",
            "rule_code": "alioth_spec_48",
            "category": "interaction",
            "executor_type": "hybrid",
            "severity": "medium",
            "check_point": "Toast 样式一致",
            "input_contract": {},
            "judge_config": {
                "capability_config": {
                    "evidence_input_scope": "task",
                    "evidence_scope_key": "toast_style",
                }
            },
            "manual_review_policy": {},
            "steps": [],
            "applicable_page_types": ["profile"],
        }
    )
    snapshot = AggregatePublicationSnapshot(
        task_id="11111111-1111-1111-1111-111111111111",
        source_agent_run_id="22222222-2222-2222-2222-222222222222",
        finalization_epoch=3,
        ruleset_snapshot_hash="snapshot-hash",
        rules=(rule,),
        evidence=(),
    )
    return snapshot, rule


async def test_evidence_insufficient_disposition_records_execution_and_review_item() -> None:
    """证据不足必须同一事务留下规则执行与待复核项；重放不能把已处理的复核项重新打开。"""
    snapshot, rule = _disposition_snapshot_and_rule()
    connection = FakeConnection()
    repository = PostgresAggregateInspectionPublicationRepository(pool=FakePool(connection))

    await repository.persist_disposition(
        snapshot=snapshot,
        rule=rule,
        status="evidence_insufficient",
        expected_member_count=0,
        code="AGGREGATE_PRESENTATION_EVIDENCE_REJECTED",
        message="截图全部被弹窗挡住，已转人工确认",
        scope_plan=None,
    )

    candidate_sql, candidate_args = next(
        item for item in connection.queries if "qc_aggregate_inspection_candidate" in item[0]
    )
    assert candidate_args[8] == "evidence_insufficient"
    # 旧版本借用「不适用」或停在待复核的同一处置可以升级，其余身份漂移照旧拒写
    assert "existing.disposition_code = 'AGGREGATE_PRESENTATION_EVIDENCE_REJECTED'" in (
        candidate_sql
    )
    assert "existing.status = 'needs_review'" in candidate_sql
    execution_sql, execution_args = next(
        item for item in connection.queries if "INSERT INTO qc_rule_execution" in item[0]
    )
    assert "'evidence_insufficient'" in execution_sql
    assert "DO NOTHING" in execution_sql
    assert execution_args[2] == (
        "aggregate-disposition:22222222-2222-2222-2222-222222222222:3:toast_style"
    )
    assert execution_args[3] == snapshot.source_agent_run_id
    assert json.loads(execution_args[5])["disposition_code"] == (
        "AGGREGATE_PRESENTATION_EVIDENCE_REJECTED"
    )
    assert "bootstrap" not in json.loads(execution_args[5])
    review_sql, review_args = next(
        item for item in connection.queries if "INSERT INTO qc_review_item" in item[0]
    )
    assert "'evidence_insufficient', 'pending'" in review_sql
    assert "ON CONFLICT (rule_execution_id) DO NOTHING" in review_sql
    assert review_args == ("55555555-5555-5555-5555-555555555555",)


async def test_not_applicable_disposition_writes_no_rule_execution() -> None:
    snapshot, rule = _disposition_snapshot_and_rule()
    connection = FakeConnection()
    repository = PostgresAggregateInspectionPublicationRepository(pool=FakePool(connection))

    await repository.persist_disposition(
        snapshot=snapshot,
        rule=rule,
        status="not_applicable",
        expected_member_count=0,
        code="AGGREGATE_RULE_SCOPE_NOT_APPLICABLE",
        message="范围计划已证明不适用",
        scope_plan=None,
    )

    assert not any("qc_rule_execution" in sql for sql, _ in connection.queries)
    assert not any("qc_review_item" in sql for sql, _ in connection.queries)
    with pytest.raises(ValueError, match="不受支持"):
        await repository.persist_disposition(
            snapshot=snapshot,
            rule=rule,
            status="needs_review",
            expected_member_count=0,
            code="AGGREGATE_APPLICABILITY_UNPROVEN",
            message="旧处置不再写入",
            scope_plan=None,
        )


async def test_load_snapshot_reads_app_page_type_from_visual_observation() -> None:
    connection = FakeConnection()
    repository = PostgresAggregateInspectionPublicationRepository(pool=FakePool(connection))

    await repository.load_snapshot(
        task_id="11111111-1111-1111-1111-111111111111",
        source_agent_run_id="22222222-2222-2222-2222-222222222222",
        finalization_epoch=3,
    )

    task_query = next(
        sql
        for sql, _ in connection.queries
        if "ruleset.snapshot_hash AS ruleset_snapshot_hash" in sql
    )
    assert "task.status IN ('running', 'review')" in task_query
    query = next(sql for sql, _ in connection.queries if "current_app_entry AS" in sql)
    app_entry_projection = query.split("FROM qc_app_evidence_index AS index", maxsplit=1)[0]
    assert "LEFT JOIN qc_app_visual_observation AS visual" in query
    assert "NULLIF(visual.visual_page_type, '')" in query
    assert "entry.metadata->>'visual_page_type'" in query
    assert "entry.page_type" not in app_entry_projection
    assert "manifest.manifest_id = entry.evidence_manifest_id" in query
    assert "manifest.manifest_version = entry.evidence_manifest_version" in query
    assert "manifest.session_id = index.surface_session_id" in query
    assert "manifest.quality_status = 'pass'" in query
    assert "app_entry.manifest_payload" in query
    assert "latest.manifest_payload" in query
    assert "capture.surface_kind IN ('android', 'ios')" in query
    assert "app_entry.capture_id IS NOT NULL" in query


async def test_load_snapshot_reads_exact_web_scope_authority() -> None:
    web_scope = WebCoverageSnapshot.create(
        task_id="11111111-1111-1111-1111-111111111111",
        agent_run_id="22222222-2222-2222-2222-222222222222",
        finalization_epoch=3,
        required_page_types=("video_list",),
        optional_page_types=("video_detail",),
        not_applicable_page_types=("content_list", "content_detail"),
        observed_page_types=("video_list",),
        unrecognized_page_types=(),
        applicability_mode="catalog_declared",
        catalog_version="test-v1",
        catalog_hash="sha256:" + "c" * 64,
        normalization_version="web-page-type-assessment.v2",
        snapshot_protocol_version="web-coverage-snapshot.v2",
        snapshot_id="33333333-3333-3333-3333-333333333333",
        created_at=datetime(2026, 9, 8, tzinfo=UTC),
    )
    connection = FakeConnection(
        task_row={
            "task_id": "11111111-1111-1111-1111-111111111111",
            "source_agent_run_id": "22222222-2222-2222-2222-222222222222",
            "finalization_epoch": 3,
            "ruleset_snapshot_hash": "snapshot-hash",
            "web_scope_id": "33333333-3333-3333-3333-333333333333",
            "web_scope_task_id": web_scope.task_id,
            "web_scope_agent_run_id": web_scope.agent_run_id,
            "web_scope_finalization_epoch": web_scope.finalization_epoch,
            "web_required_page_types": list(web_scope.required_page_types),
            "web_observed_page_types": list(web_scope.observed_page_types),
            "web_missing_page_types": list(web_scope.missing_page_types),
            "web_unrecognized_page_types": list(web_scope.unrecognized_page_types),
            "web_normalization_version": web_scope.normalization_version,
            "web_coverage_status": web_scope.coverage_status,
            "web_scope_hash": web_scope.snapshot_hash,
            "web_scope_created_at": web_scope.created_at,
            "web_applicability_snapshot": {
                "required_page_types": list(web_scope.required_page_types),
                "optional_page_types": list(web_scope.optional_page_types),
                "not_applicable_page_types": list(web_scope.not_applicable_page_types),
                "applicability_mode": web_scope.applicability_mode,
                "catalog_version": web_scope.catalog_version,
                "catalog_hash": web_scope.catalog_hash,
                "snapshot_protocol_version": web_scope.snapshot_protocol_version,
            },
            "app_scope_hash": None,
            "app_coverage_summary": None,
        }
    )
    repository = PostgresAggregateInspectionPublicationRepository(pool=FakePool(connection))

    snapshot = await repository.load_snapshot(
        task_id="11111111-1111-1111-1111-111111111111",
        source_agent_run_id="22222222-2222-2222-2222-222222222222",
        finalization_epoch=3,
    )

    authority = snapshot.scope_authority
    assert authority is not None
    assert authority.authority_type == "web_coverage_snapshot"
    assert authority.applicable_page_types == ("video_detail", "video_list")
    assert authority.not_applicable_page_types == ("content_detail", "content_list")
    assert authority.negative_evidence_refs == (
        "web-coverage:33333333-3333-3333-3333-333333333333",
    )
    task_query = next(
        sql
        for sql, _ in connection.queries
        if "ruleset.snapshot_hash AS ruleset_snapshot_hash" in sql
    )
    assert "web_scope.agent_run_id = task.finalization_agent_run_id" in task_query
    assert "web_scope.finalization_epoch = task.finalization_epoch" in task_query
    assert "app_scope.agent_run_id = task.finalization_agent_run_id" in task_query


@pytest.mark.parametrize(
    (
        "scope_fields",
        "source_not_applicable_page_types",
        "expected_scope_verified",
        "expected_not_applicable_page_types",
        "expected_unresolved_page_types",
    ),
    [
        pytest.param(
            {
                "assessment_scope_verified": True,
                "assessment_scope_complete": True,
                "assessment_scope_kind": "observed_frontier",
                "unresolved_applicability_page_types": [],
            },
            ["payment"],
            True,
            ("payment", "settings"),
            (),
            id="complete-observed-frontier",
        ),
        pytest.param(
            {
                "assessment_scope_verified": True,
                "assessment_scope_complete": True,
                "assessment_scope_kind": "observed_frontier",
            },
            ["payment"],
            True,
            ("payment",),
            ("search", "settings"),
            id="contradictory-complete-with-unresolved",
        ),
        pytest.param(
            {
                "assessment_scope_verified": True,
                "not_observed_page_types": [],
            },
            ["payment"],
            True,
            ("payment",),
            ("search",),
            id="historical-scope-without-observed-field",
        ),
        pytest.param(
            {
                "assessment_scope_verified": False,
                "assessment_scope_complete": True,
                "assessment_scope_kind": "observed_frontier",
            },
            [],
            False,
            (),
            ("search", "settings"),
            id="scope-verified-false",
        ),
        pytest.param(
            {
                "assessment_scope_complete": True,
                "assessment_scope_kind": "observed_frontier",
            },
            [],
            False,
            (),
            ("search", "settings"),
            id="scope-verified-missing",
        ),
        pytest.param(
            {
                "assessment_scope_verified": True,
                "assessment_scope_complete": False,
                "assessment_scope_kind": "observed_frontier",
            },
            ["payment"],
            True,
            ("payment",),
            ("search", "settings"),
            id="scope-complete-false",
        ),
        pytest.param(
            {
                "assessment_scope_verified": True,
                "assessment_scope_kind": "observed_frontier",
            },
            ["payment"],
            True,
            ("payment",),
            ("search", "settings"),
            id="scope-complete-missing",
        ),
        pytest.param(
            {
                "assessment_scope_verified": True,
                "assessment_scope_complete": True,
                "assessment_scope_kind": "other",
            },
            ["payment"],
            True,
            ("payment",),
            ("search", "settings"),
            id="scope-kind-invalid",
        ),
        pytest.param(
            {
                "assessment_scope_verified": True,
                "assessment_scope_complete": True,
            },
            ["payment"],
            True,
            ("payment",),
            ("search", "settings"),
            id="scope-kind-missing",
        ),
    ],
)
async def test_load_snapshot_reads_same_run_app_scope_authority(
    scope_fields: dict[str, object],
    source_not_applicable_page_types: list[str],
    expected_scope_verified: bool,
    expected_not_applicable_page_types: tuple[str, ...],
    expected_unresolved_page_types: tuple[str, ...],
) -> None:
    app_result = AppExplorationResult(
        task_id="11111111-1111-1111-1111-111111111111",
        run_id="22222222-2222-2222-2222-222222222222",
        surface_session_id="session-1",
        outcome=AppExplorationResultOutcome.EXPLORATION_COMPLETED,
        terminal_reason_code="frontier_exhausted",
        evidence_index_id="index-1",
        evidence_index_hash="sha256:" + "d" * 64,
        coverage_summary={
            "page_type_applicability": {
                "protocol_version": "app-page-type-applicability.v2",
                "applicable_page_types": ["home", "profile"],
                "not_applicable_page_types": source_not_applicable_page_types,
                "not_observed_page_types": ["settings"],
                "unresolved_applicability_page_types": ["search"],
                **scope_fields,
            }
        },
        evidence_gaps=(),
        started_at=datetime(2026, 9, 8, tzinfo=UTC),
        completed_at=datetime(2026, 9, 8, 0, 1, tzinfo=UTC),
    )
    connection = FakeConnection(
        task_row={
            "task_id": "11111111-1111-1111-1111-111111111111",
            "source_agent_run_id": "22222222-2222-2222-2222-222222222222",
            "finalization_epoch": 3,
            "ruleset_snapshot_hash": "snapshot-hash",
            "web_scope_id": None,
            "web_scope_hash": None,
            "web_applicability_snapshot": None,
            "app_scope_session_id": app_result.surface_session_id,
            "app_scope_evidence_index_id": app_result.evidence_index_id,
            "app_scope_protocol_version": app_result.protocol_version,
            "app_scope_outcome": app_result.outcome.value,
            "app_scope_terminal_reason_code": app_result.terminal_reason_code,
            "app_scope_evidence_index_hash": app_result.evidence_index_hash,
            "app_scope_hash": app_result.result_sha256,
            "app_coverage_summary": dict(app_result.coverage_summary),
            "app_scope_evidence_gaps": [],
            "app_scope_started_at": app_result.started_at,
            "app_scope_completed_at": app_result.completed_at,
        }
    )
    repository = PostgresAggregateInspectionPublicationRepository(pool=FakePool(connection))

    snapshot = await repository.load_snapshot(
        task_id="11111111-1111-1111-1111-111111111111",
        source_agent_run_id="22222222-2222-2222-2222-222222222222",
        finalization_epoch=3,
    )

    authority = snapshot.scope_authority
    assert authority is not None
    assert authority.authority_type == "app_exploration_result"
    assert authority.source_agent_run_id == app_result.run_id
    assert authority.authority_id == f"app-result:{app_result.run_id}"
    assert authority.authority_hash == app_result.result_sha256
    assert authority.scope_verified is expected_scope_verified
    assert authority.applicable_page_types == ("home", "profile")
    assert authority.not_applicable_page_types == expected_not_applicable_page_types
    assert authority.unresolved_page_types == expected_unresolved_page_types
    expected_negative_refs = (
        (f"app-exploration-result:{app_result.run_id}",)
        if expected_not_applicable_page_types
        else ()
    )
    assert authority.negative_evidence_refs == expected_negative_refs
    task_query, task_args = next(
        (sql, args)
        for sql, args in connection.queries
        if "ruleset.snapshot_hash AS ruleset_snapshot_hash" in sql
    )
    assert task_args[1:3] == (app_result.run_id, 3)
    assert "app_scope.agent_run_id = task.finalization_agent_run_id" in task_query


async def test_load_snapshot_rejects_tampered_scope_authority() -> None:
    web_scope = WebCoverageSnapshot.create(
        task_id="11111111-1111-1111-1111-111111111111",
        agent_run_id="22222222-2222-2222-2222-222222222222",
        finalization_epoch=3,
        required_page_types=("video_list",),
        optional_page_types=(),
        not_applicable_page_types=(
            "content_detail",
            "content_list",
            "video_detail",
        ),
        observed_page_types=("video_list",),
        unrecognized_page_types=(),
        applicability_mode="strict_four",
        catalog_version="test-v1",
        catalog_hash="sha256:" + "c" * 64,
        normalization_version="web-page-type-assessment.v2",
        snapshot_protocol_version="web-coverage-snapshot.v2",
        snapshot_id="33333333-3333-3333-3333-333333333333",
        created_at=datetime(2026, 9, 8, tzinfo=UTC),
    )
    connection = FakeConnection(
        task_row={
            "task_id": web_scope.task_id,
            "source_agent_run_id": web_scope.agent_run_id,
            "finalization_epoch": web_scope.finalization_epoch,
            "ruleset_snapshot_hash": "snapshot-hash",
            "web_scope_id": web_scope.snapshot_id,
            "web_scope_task_id": web_scope.task_id,
            "web_scope_agent_run_id": web_scope.agent_run_id,
            "web_scope_finalization_epoch": web_scope.finalization_epoch,
            "web_required_page_types": list(web_scope.required_page_types),
            "web_observed_page_types": list(web_scope.observed_page_types),
            "web_missing_page_types": list(web_scope.missing_page_types),
            "web_unrecognized_page_types": [],
            "web_normalization_version": web_scope.normalization_version,
            "web_coverage_status": web_scope.coverage_status,
            "web_scope_hash": "0" * 64,
            "web_scope_created_at": web_scope.created_at,
            "web_applicability_snapshot": {
                "required_page_types": list(web_scope.required_page_types),
                "optional_page_types": list(web_scope.optional_page_types),
                "not_applicable_page_types": list(web_scope.not_applicable_page_types),
                "applicability_mode": web_scope.applicability_mode,
                "catalog_version": web_scope.catalog_version,
                "catalog_hash": web_scope.catalog_hash,
                "snapshot_protocol_version": web_scope.snapshot_protocol_version,
            },
            "app_scope_hash": None,
        }
    )
    repository = PostgresAggregateInspectionPublicationRepository(pool=FakePool(connection))

    with pytest.raises(
        AggregateInspectionPublicationPersistenceError,
        match="权威快照校验失败",
    ):
        await repository.load_snapshot(
            task_id=web_scope.task_id,
            source_agent_run_id=web_scope.agent_run_id,
            finalization_epoch=web_scope.finalization_epoch,
        )


async def test_list_session_draining_includes_review_tasks() -> None:
    connection = FakeConnection()
    repository = PostgresAggregateInspectionPublicationRepository(pool=FakePool(connection))

    assert await repository.list_session_draining() == ()

    query = connection.queries[-1][0]
    assert "status = 'review' AND target_platform = 'h5'" in query
    assert "finalization_state = 'session_draining'" in query
