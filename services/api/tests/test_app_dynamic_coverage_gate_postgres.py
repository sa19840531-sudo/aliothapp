"""真实 Gate SQL 必须拒绝旧版本误标完成但仍有适用页缺口的 App 结果。"""

import json
import os

import pytest

from services.api.app.infrastructure.db.task_finalization_repository import _GATE_FACTS_SQL
from services.api.tests.test_app_evidence_index_postgres_integration import (
    _database,
    _insert_building_index,
    _insert_capture,
    _insert_entry,
    _insert_manifest,
    _seal,
    _upgrade_isolated_postgres,  # noqa: F401 - 复用隔离库升级 fixture
)

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"), reason="必须显式提供隔离测试数据库",
)


@pytest.mark.parametrize(
    ("unresolved", "uncovered", "expected"),
    [([], ["profile"], False), (["profile"], [], False), ([], [], True)],
)
async def test_app_gate_rechecks_dynamic_uncovered_pages(unresolved, uncovered, expected):
    async with _database() as (connection, ids):
        await connection.execute(
            """
            UPDATE qc_task SET finalization_agent_run_id = $2::uuid,
                task_config = task_config || $3::jsonb
            WHERE id = $1::uuid
            """,
            ids["task"], ids["run"],
            json.dumps({"app_exploration_target": {"target_hash": "locked-target"}}),
        )
        capture = await _insert_capture(
            connection, ids, capture_role="observation", semantic_role="final_observation",
        )
        manifest = await _insert_manifest(
            connection, ids, capture_id=capture, version=1, quality_status="pass",
        )
        index = await _insert_building_index(connection, ids)
        await _insert_entry(
            connection, index_id=index, ids=ids, capture_id=capture,
            manifest_id=manifest, manifest_version=1, semantic_role="final_observation",
        )
        await _seal(connection, index_id=index, outcome="exploration_completed", gaps=())
        # 模拟旧版本真实封存的误完成摘要，Gate 必须独立复核集合，不能只信布尔值。
        coverage = {
            "completion_mode": "dynamic_discovery", "coverage_complete": True,
            "visual_quality_satisfied": True, "assessment_scope_complete": True,
            "app_runtime_policy": {"app_target_snapshot": {"target_hash": "locked-target"}},
            "page_type_applicability": {
                "assessment_scope_complete": True,
                "unresolved_applicability_page_types": unresolved,
                "known_uncovered_page_types": uncovered,
            },
        }
        await connection.execute(
            """
            INSERT INTO qc_app_exploration_result (
                agent_run_id, task_id, surface_session_id, evidence_index_id, protocol_version,
                outcome, terminal_reason_code, evidence_index_hash, coverage_summary,
                evidence_gaps, started_at, completed_at, result_sha256)
            SELECT agent_run_id, task_id, surface_session_id, index_id,
                'app-exploration-result.v1', outcome, terminal_reason_code, content_sha256,
                $2::jsonb, evidence_gaps, sealed_at, sealed_at, $3
            FROM qc_app_evidence_index WHERE index_id = $1
            """,
            index, json.dumps(coverage), "sha256:" + "d" * 64,
        )

        facts = await connection.fetchrow(_GATE_FACTS_SQL, ids["task"], "completed")

        assert facts["app_coverage_satisfied"] is expected
