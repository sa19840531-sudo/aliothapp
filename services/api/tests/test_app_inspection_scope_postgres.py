"""隔离 PG 验证范围外的快照交接不产生虚假规则结论。"""

import json
import os
from uuid import uuid4

import pytest

from services.api.app.application.app_inspection_publisher import (
    AppCaptureInspectionResult,
    PostgresAppCaptureInspectionAnnotator,
)
from services.api.app.infrastructure.db.app_inspection_publication_repository import (
    PostgresAppInspectionPublicationRepository,
)
from services.api.tests.test_app_evidence_index_postgres_integration import (
    _database,
    _insert_capture,
    _SingleConnectionPool,
)
from services.api.tests.test_app_publication_handoff_recovery_postgres import _sealed_source

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"), reason="必须显式提供隔离测试数据库",
)


async def test_snapshot_scope_exclusion_preserves_reason_without_rule_execution():
    async with _database() as (connection, ids):
        index_id, entry = await _sealed_source(connection, ids)
        repository = PostgresAppInspectionPublicationRepository(
            pool=_SingleConnectionPool(connection),
        )
        publication = await repository.create_for_index(
            task_id=ids["task"], source_agent_run_id=ids["run"],
            evidence_index_id=index_id, evidence_index_hash="sha256:" + "a" * 64,
            entries=(entry,), expected_finalization_epoch=0,
        )
        version = await connection.fetchval(
            "SELECT row_version FROM qc_app_inspection_publication WHERE id=$1::uuid",
            publication.publication_id,
        )
        claim = await repository.claim(
            publication_id=publication.publication_id, task_id=ids["task"],
            message_version=version, lease_owner="scope-regression", lease_seconds=60,
        )
        await repository.complete(
            lease=claim.lease,
            results=(AppCaptureInspectionResult(
                status="out_of_scope", published_count=0,
                page_capture_id=entry.capture_id, page_type="post_list",
                message="当前页面不在本任务所选质检范围内，仅保留探索记录",
            ),),
        )
        metadata = json.loads(await connection.fetchval(
            "SELECT metadata FROM qc_capture WHERE id=$1::uuid", entry.capture_id,
        ))
        assert metadata["inspection"]["status"] == "out_of_scope"
        assert metadata["inspection"]["reason_code"] == "inspection_scope_excluded"
        assert "范围" in metadata["inspection"]["message"]
        assert metadata["inspection"]["published_count"] == 0
        # 这里只完成当前 Capture 的发布处置，不生成任何规则“不适用”结论。
        assert await connection.fetchval(
            "SELECT count(*) FROM qc_inspection_work_item_rule WHERE work_item_id IN "
            "(SELECT id FROM qc_inspection_work_item WHERE task_id=$1::uuid)", ids["task"],
        ) == 0
        assert await connection.fetchval(
            "SELECT count(*) FROM qc_rule_execution WHERE task_id=$1::uuid", ids["task"],
        ) == 0


@pytest.mark.parametrize("path", ["active", "publication"])
@pytest.mark.parametrize("status_source", ["flat", "nested", "degraded", "stage", "abandon"])
async def test_scope_note_preserves_prior_capture_failure(path, status_source, monkeypatch):
    async with _database() as (connection, ids):
        failure = {
            "inspection": {
                "error_code": "CAPTURE_FAILED",
                "message": "原始采集失败，不能当作检查完成",
                "reason_code": "capture_integrity_failed",
                "diagnostic": {"original": True},
            },
        }
        expected_status = "failed"
        if status_source == "flat":
            failure["inspection_status"] = "failed"
        elif status_source == "nested":
            failure["inspection"]["status"] = "failed"
        else:
            expected_status = "degraded"
            if status_source == "degraded":
                failure["inspection_status"] = "degraded"
            elif status_source == "stage":
                failure["inspection_failure_stage"] = "visual"
            else:
                failure["inspection_abandon_reason"] = "capture_failed"
        async def capture_with_prior_failure(connection, ids, **kwargs):
            capture = await _insert_capture(connection, ids, **kwargs)
            await connection.execute(
                "UPDATE qc_capture SET metadata=metadata || $2::jsonb WHERE id=$1::uuid",
                capture, json.dumps(failure),
            )
            return capture

        # 故障确实先于封存存在；不放宽封存后证据身份不可变门禁。
        with monkeypatch.context() as scoped_patch:
            scoped_patch.setattr(
                "services.api.tests.test_app_publication_handoff_recovery_postgres._insert_capture",
                capture_with_prior_failure,
            )
            index_id, entry = await _sealed_source(connection, ids)
        pool = _SingleConnectionPool(connection)
        if path == "active":
            await PostgresAppCaptureInspectionAnnotator(pool=pool).annotate_inspection(
                task_id=ids["task"], capture_id=entry.capture_id, page_type=None,
                page_state="stable", inspection_status="out_of_scope", published_count=0,
            )
        else:
            repository = PostgresAppInspectionPublicationRepository(pool=pool)
            publication = await repository.create_for_index(
                task_id=ids["task"], source_agent_run_id=ids["run"],
                evidence_index_id=index_id, evidence_index_hash="sha256:" + "a" * 64,
                entries=(entry,), expected_finalization_epoch=0,
            )
            claim = await repository.claim(
                publication_id=publication.publication_id, task_id=ids["task"],
                message_version=1, lease_owner="scope-failed-regression", lease_seconds=60,
            )
            await repository.complete(lease=claim.lease, results=(AppCaptureInspectionResult(
                status="out_of_scope", published_count=0, page_capture_id=entry.capture_id,
            ),))
        actual = json.loads(await connection.fetchval(
            "SELECT metadata FROM qc_capture WHERE id=$1::uuid", entry.capture_id,
        ))
        assert actual["inspection_status"] == actual["inspection"]["status"] == expected_status
        for key, value in failure["inspection"].items():
            assert actual["inspection"][key] == value
        for key, value in failure.items():
            if key != "inspection":
                assert actual[key] == value


async def test_scope_note_preserves_formal_page_failure():
    async with _database() as (connection, ids):
        capture = str(uuid4())
        await connection.execute(
            "UPDATE qc_task SET target_platform='h5',target_url='https://example.com/' "
            "WHERE id=$1::uuid", ids["task"],
        )
        await connection.execute(
            """INSERT INTO qc_capture(id,task_id,agent_run_id,surface_kind,capture_role,
                   capture_status,metadata) VALUES($1::uuid,$2::uuid,$3::uuid,'web',
                   'observation','captured','{"inspection_status":"failed"}'::jsonb)""",
            capture, ids["task"], ids["run"],
        )
        await connection.execute(
            """INSERT INTO qc_page_capture(id,task_id,page_seq,page_slug,url,
                   inspection_status,inspection_failure_stage,inspection_failure_message)
               VALUES($1::uuid,$2::uuid,1,'scope-failure','https://example.com/',
                      'failed','capture','原始页面采集失败')""",
            capture, ids["task"],
        )
        await PostgresAppCaptureInspectionAnnotator(
            pool=_SingleConnectionPool(connection),
        ).annotate_inspection(
            task_id=ids["task"], capture_id=capture, page_type=None, page_state="stable",
            inspection_status="out_of_scope", published_count=0,
        )
        row = await connection.fetchrow(
            "SELECT inspection_status,inspection_failure_message FROM qc_page_capture "
            "WHERE id=$1::uuid", capture,
        )
        assert row["inspection_status"] == "failed"
        assert row["inspection_failure_message"] == "原始页面采集失败"


async def test_scope_annotation_rolls_back_with_failed_publication(monkeypatch):
    async with _database() as (connection, ids):
        index_id, entry = await _sealed_source(connection, ids)
        repository = PostgresAppInspectionPublicationRepository(
            pool=_SingleConnectionPool(connection),
        )
        publication = await repository.create_for_index(
            task_id=ids["task"], source_agent_run_id=ids["run"],
            evidence_index_id=index_id, evidence_index_hash="sha256:" + "a" * 64,
            entries=(entry,), expected_finalization_epoch=0,
        )
        claim = await repository.claim(
            publication_id=publication.publication_id, task_id=ids["task"],
            message_version=1, lease_owner="scope-rollback", lease_seconds=60,
        )
        original = await connection.fetchval(
            "SELECT metadata FROM qc_capture WHERE id=$1::uuid", entry.capture_id,
        )

        async def fail_after_scope_write(connection, **kwargs):
            current = json.loads(await connection.fetchval(
                "SELECT metadata FROM qc_capture WHERE id=$1::uuid", entry.capture_id,
            ))
            assert current["inspection"]["status"] == "out_of_scope"
            raise RuntimeError("交接后续失败")

        monkeypatch.setattr(
            repository, "_load_resolved_active_session_rules", fail_after_scope_write,
        )
        with pytest.raises(RuntimeError, match="交接后续失败"):
            await repository.complete(lease=claim.lease, results=(AppCaptureInspectionResult(
                status="out_of_scope", published_count=0, page_capture_id=entry.capture_id,
            ),))
        assert await connection.fetchval(
            "SELECT metadata FROM qc_capture WHERE id=$1::uuid", entry.capture_id,
        ) == original
        assert await connection.fetchval(
            "SELECT status FROM qc_app_inspection_publication WHERE id=$1::uuid",
            publication.publication_id,
        ) == "leased"
