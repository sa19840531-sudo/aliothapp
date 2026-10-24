"""实际独立PG/原件/Writer到只读展示，业务材料为夹具，不代表设备采集验收。"""

import json
from uuid import uuid4

import pytest

from services.api.app.application.aggregate_inspection_execution import evaluate_aggregate_context
from services.api.app.domain.evidence import (
    EvidenceQualityStatus,
    build_evidence_manifest_reference,
)
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.infrastructure.db.evidence_manifest_repository import (
    PostgresEvidenceManifestRepository,
)
from services.api.app.infrastructure.db.task_insight_repository import PostgresTaskInsightRepository
from services.api.app.infrastructure.storage.local_evidence_store import LocalEvidenceArtifactStore
from services.api.tests.test_app_postgres_integration import (
    _upgrade_isolated_postgres,  # noqa: F401 - 只升级显式独立测试库
)
from services.api.tests.test_green_aggregate_execution_postgres import green_case
from services.api.tests.test_green_process_judge_postgres import _local_materials


async def _write_actual(repository, work, ids, *, expected_first="pass"):
    claim = await repository.claim_for_wakeup(
        work_item_id=work["id"],
        task_id=ids["task"],
        message_version=work["row_version"],
        lease_owner="green-display-read-fixture",
        lease_seconds=60,
    )
    context = await repository.load_judge_context(lease=claim.lease)
    judgement = evaluate_aggregate_context(context)
    assert judgement.execution_status == "evidence_insufficient"
    assert judgement.wrapper_payload["green"]["subitems"][0]["status"] == expected_first
    await repository.complete_judgement(context=context, judgement=judgement)
    return judgement


@pytest.mark.parametrize(
    "fault,expected",
    [
        ("none", "linked"),
        ("approved", "linked"),
        ("latest_quality", "evidence_changed"),
        ("foreign_session", "evidence_changed"),
        ("old_epoch_without_gate", "input_mismatch"),
        ("attempt_output", "input_mismatch"),
        ("foreign_scope", None),
        ("missing_png", "evidence_changed"),
        ("action_intent", "evidence_changed"),
        ("action_status", "evidence_changed"),
        ("action_sequence", "evidence_changed"),
        ("action_time", "evidence_changed"),
        ("action_pair", "evidence_changed"),
    ],
)
async def test_formal_green_result_projection_is_readonly_and_rejects_stale_linkage(
    tmp_path, fault, expected
):
    async with green_case(
        tmp_path,
        "s35",
        local_display=_local_materials(transition=False),
        transactional=False,
    ) as (connection, pool, ids, work, writer, _input, _frozen, manifests, _intent, artifacts):
        await _write_actual(writer, work, ids)
        execution_id = str(
            await connection.fetchval(
                "SELECT id FROM qc_rule_execution WHERE task_id=$1::uuid",
                ids["task"],
            )
        )
        scope = AccessScope(
            actor_user_id=ids["user"], tenant_id=ids["tenant"], permissions=frozenset()
        )
        if fault == "approved":
            await connection.execute(
                "UPDATE qc_review_item SET status='approved',reviewed_at=now(),"
                "review_reason='已核对原件；业务缺项仍保留' WHERE task_id=$1::uuid",
                ids["task"],
            )
        elif fault == "latest_quality":
            manifest = manifests[-1]
            await PostgresEvidenceManifestRepository(pool=pool).add(
                manifest=build_evidence_manifest_reference(
                    task_id=manifest.task_id,
                    session_id=manifest.session_id,
                    capture_id=manifest.capture_id,
                    manifest_version=2,
                    quality_status=EvidenceQualityStatus.FAIL,
                    quality_reason="受控晚到质量拒绝",
                    artifacts=manifest.artifacts,
                    created_at=manifest.created_at,
                )
            )
        elif fault == "foreign_session":
            # 使用同任务另一真实Session，使读取不能按相似页面或同Task猜归属。
            session = "h5:foreign-" + uuid4().hex
            await connection.execute(
                """INSERT INTO qc_surface_session(session_id,task_id,surface_kind,
                     session_owner,status,finalization_epoch,activated_at)
                   VALUES($1,$2::uuid,'web','foreign-fixture','active',0,now())""",
                session,
                ids["task"],
            )
            await connection.execute(
                "UPDATE qc_capture SET surface_session_id=$2 WHERE id=$1::uuid",
                manifests[-1].capture_id,
                session,
            )
        elif fault == "old_epoch_without_gate":
            await connection.execute(
                "UPDATE qc_task SET finalization_epoch=finalization_epoch+1 WHERE id=$1::uuid",
                ids["task"],
            )
        elif fault == "attempt_output":
            await connection.execute(
                "UPDATE qc_aggregate_inspection_attempt SET result_payload="
                "jsonb_set(result_payload,'{execution_status}','\"pass\"'::jsonb) "
                "WHERE task_id=$1::uuid",
                ids["task"],
            )
        elif fault == "foreign_scope":
            scope = AccessScope(
                actor_user_id=str(uuid4()), tenant_id=str(uuid4()), permissions=frozenset()
            )
        elif fault == "missing_png":
            for artifact in artifacts:
                if artifact.artifact_type == "screenshot":
                    (tmp_path / "controlled-originals" / artifact.storage_path).unlink()
        elif fault.startswith("action_"):
            mutations = {
                "action_intent": "intent_payload=jsonb_set(intent_payload,'{purpose}',"
                "'\"漂移后的动作意图\"'::jsonb)",
                "action_status": "status='failed',error_code='CONTROLLED_ACTION_STATUS_CHANGED'",
                "action_sequence": "command_sequence=command_sequence+1",
                "action_time": "started_at=started_at-interval '1 millisecond'",
                "action_pair": "result_capture_id=source_capture_id",
            }
            await connection.execute(
                f"UPDATE qc_action_execution SET {mutations[fault]} WHERE task_id=$1::uuid",
                ids["task"],
            )
        reader = PostgresTaskInsightRepository(
            pool=pool,
            artifact_store=LocalEvidenceArtifactStore(root=tmp_path / "controlled-originals"),
        )
        before = await connection.fetchrow(
            """SELECT (SELECT count(*) FROM qc_rule_execution WHERE task_id=$1::uuid) AS executions,
                      (SELECT count(*) FROM qc_review_item WHERE task_id=$1::uuid) AS reviews,
                      (SELECT count(*) FROM qc_capture WHERE task_id=$1::uuid) AS captures,
                      (SELECT count(*) FROM qc_outbox_event WHERE task_id=$1::uuid) AS events""",
            ids["task"],
        )
        projected = await reader.read_green_rule_results(
            task_id=ids["task"], execution_ids=[execution_id], access_scope=scope
        )
        after = await connection.fetchrow(
            """SELECT (SELECT count(*) FROM qc_rule_execution WHERE task_id=$1::uuid) AS executions,
                      (SELECT count(*) FROM qc_review_item WHERE task_id=$1::uuid) AS reviews,
                      (SELECT count(*) FROM qc_capture WHERE task_id=$1::uuid) AS captures,
                      (SELECT count(*) FROM qc_outbox_event WHERE task_id=$1::uuid) AS events""",
            ids["task"],
        )
        assert dict(before) == dict(after)
        if expected is None:
            assert projected == {}
            return
        result = projected[execution_id]
        assert result["linkage_status"] == expected
        if expected != "linked":
            assert result["subitems"] == [] and result["execution_status"] is None
            return
        assert result["execution_status"] == "evidence_insufficient"
        assert result["review_status"] == ("approved" if fault == "approved" else "pending")
        assert result["subitems"][0]["status"] == "pass"
        assert result["missing_evidence"]
        assert len(result["subitems"][0]["screenshot_artifact_ids"]) == (
            0 if fault == "missing_png" else 2
        )
        assert "storage_path" not in json.dumps(result) and "metadata" not in json.dumps(result)
        async with (
            pool.acquire() as readonly,
            readonly.transaction(isolation="repeatable_read", readonly=True),
        ):
            assert await readonly.fetchval("SHOW transaction_read_only") == "on"
            with pytest.raises(Exception, match="read-only"):
                await readonly.execute(
                    "UPDATE qc_task SET updated_at=updated_at WHERE id=$1::uuid", ids["task"]
                )


@pytest.mark.parametrize("key", ["s72", "62", "s49", "s73"])
@pytest.mark.parametrize("fault", ["none", "wrong_scope", "action_intent"])
async def test_actual_temporal_missing_results_use_frozen_scope_and_original_fingerprint(
    tmp_path, key, fault
):
    async with green_case(tmp_path, key, transactional=False) as (
        connection,
        pool,
        ids,
        work,
        writer,
        _input,
        frozen,
        _manifests,
        _intent,
        _artifacts,
    ):
        judgement = await _write_actual(
            writer, work, ids, expected_first="evidence_insufficient"
        )
        execution_id = str(
            await connection.fetchval(
                "SELECT id FROM qc_rule_execution WHERE task_id=$1::uuid", ids["task"]
            )
        )
        if fault == "wrong_scope":
            await connection.execute(
                "UPDATE qc_rule_execution SET judge_input=jsonb_set(judge_input,'{scope_key}',"
                "'\"green_interaction_stability.v1\"'::jsonb) WHERE id=$1::uuid",
                execution_id,
            )
        elif fault == "action_intent":
            await connection.execute(
                "UPDATE qc_action_execution SET intent_payload=jsonb_set(intent_payload,"
                "'{purpose}','\"判后动作台账漂移\"'::jsonb) WHERE task_id=$1::uuid",
                ids["task"],
            )
        reader = PostgresTaskInsightRepository(
            pool=pool,
            artifact_store=LocalEvidenceArtifactStore(root=tmp_path / "controlled-originals"),
        )
        result = (
            await reader.read_green_rule_results(
                task_id=ids["task"],
                execution_ids=[execution_id],
                access_scope=AccessScope(
                    actor_user_id=ids["user"],
                    tenant_id=ids["tenant"],
                    permissions=frozenset(),
                ),
            )
        )[execution_id]
        assert result["rule_key"] == key
        expected = {
            "none": "linked",
            "wrong_scope": "input_mismatch",
            "action_intent": "evidence_changed",
        }[fault]
        assert result["linkage_status"] == expected
        if fault != "none":
            assert result["subitems"] == [] and result["execution_status"] is None
            return
        assert result["execution_status"] == "evidence_insufficient"
        assert result["review_status"] == "pending"
        assert result["missing_evidence"] == judgement.wrapper_payload["green"]["missing_evidence"]
        assert [item["key"] for item in result["subitems"]] == (
            frozen["judge_config"]["green_rule_contract"]["subchecks"]
        )
        assert all(
            item["status"] == "evidence_insufficient"
            and item["evaluated"] is False
            and item["confidence"] is None
            and item["missing_evidence"]
            and item["evidence_artifact_ids"] == []
            and item["screenshot_artifact_ids"] == []
            for item in result["subitems"]
        )
        assert "storage_path" not in json.dumps(result) and "metadata" not in json.dumps(result)
