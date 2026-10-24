"""真实PG冻结配置、原件核验、共享判读和结果写回，不替代三端业务验收。"""

import copy
import json
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from services.api.app.application.aggregate_inspection_execution import AggregateInspectionExecutor
from services.api.app.domain.evidence import (
    ArtifactReference,
    EvidenceQualityStatus,
    build_evidence_manifest_reference,
)
from services.api.app.infrastructure.db.evidence_manifest_repository import (
    PostgresEvidenceManifestRepository,
)
from services.api.tests import test_green_aggregate_execution_postgres as fixtures
from services.api.tests.test_app_postgres_integration import (
    _upgrade_isolated_postgres,  # noqa: F401 - 仅升级显式独立测试库
)
from services.api.tests.test_exploration_scheduler_facts_postgres_integration import (
    pytestmark as pytestmark,
)
from services.api.tests.test_green_temporal_rules import case


@pytest.mark.parametrize("status", ["pass", "fail", "skipped", "evidence_insufficient"])
async def test_frozen_binding_consumes_verified_original_and_writes_actual_verdict(
    tmp_path,
    monkeypatch,
    status,
):
    original_builder = fixtures.build_green_rule_payload
    original_saver = fixtures._save_action_originals
    bindings = {
        "schema_version": "green-shared-bindings.v1",
        "subjects": [
            {
                "subject_id": "module/request",
                "fields": {
                    "temporal": {
                        "artifact_type": "temporal_trace",
                        "capture_role": "after_action",
                        "schema_version": "controlled-pg-temporal.v1",
                        "path": ["facts"],
                    }
                },
            }
        ],
    }

    def build(key):
        rule = original_builder(key)
        rule["judge_config"]["green_shared_bindings"] = copy.deepcopy(bindings)
        return rule

    async def save(connection, pool, ids, store, **options):
        candidates, manifests, intent, artifacts = await original_saver(
            connection,
            pool,
            ids,
            store,
            **options,
        )
        facts = case().get("temporal")
        temporal = {key: facts[key] for key in ("window", "state")}
        if status == "fail":
            temporal["state"]["messages"] = [{"text": "暂无数据", "visible": True}]
            temporal["state"]["targets"] = []
        elif status == "skipped":
            temporal["state"]["outcome"] = "nonempty"
        elif status == "evidence_insufficient":
            temporal["state"]["outcome"] = "unknown"
        old = manifests[-1]
        original = await store.store(
            task_id=ids["task"],
            page_capture_id=old.capture_id,
            artifact_id=str(uuid4()),
            artifact_type="temporal_trace",
            content={"schema_version": "controlled-pg-temporal.v1", "facts": temporal},
        )
        await connection.execute(
            "INSERT INTO qc_evidence_artifact(id,task_id,page_capture_id,artifact_type,source,"
            "storage_path,checksum,size_bytes,content_type) "
            "VALUES($1::uuid,$2::uuid,$3::uuid,'temporal_trace','baseline',$4,$5,$6,$7)",
            original.artifact_id,
            ids["task"],
            old.capture_id,
            original.storage_path,
            original.checksum,
            original.size_bytes,
            original.content_type,
        )
        reference = ArtifactReference(
            artifact_id=original.artifact_id,
            artifact_type="temporal_trace",
            role="after_action",
            storage_path=original.storage_path,
            checksum=original.checksum,
            size_bytes=original.size_bytes,
            content_type=original.content_type,
        )
        manifest = build_evidence_manifest_reference(
            task_id=ids["task"],
            session_id=ids["session"],
            capture_id=old.capture_id,
            manifest_version=2,
            quality_status=EvidenceQualityStatus.PASS,
            quality_reason="受控PG原件与摘要验证",
            created_at=datetime.now(UTC),
            artifacts=(*old.artifacts, reference),
        )
        await PostgresEvidenceManifestRepository(pool=pool).add(manifest=manifest)
        manifests[-1] = manifest
        candidates[-1] = replace(candidates[-1], manifest=manifest)
        return candidates, manifests, intent, [*artifacts, original]

    monkeypatch.setattr(fixtures, "build_green_rule_payload", build)
    monkeypatch.setattr(fixtures, "_save_action_originals", save)
    async with fixtures.green_case(tmp_path, "s72") as (
        connection,
        _pool,
        ids,
        work,
        repository,
        _inputs,
        frozen,
        _manifests,
        _intent,
        _artifacts,
    ):
        assert frozen["judge_config"]["green_shared_bindings"] == bindings
        # 任务冻结后再修改实时规则，不能把新配置偷渡到已有输入。
        await connection.execute(
            "UPDATE qc_rule SET judge_config='{}'::jsonb WHERE id=$1::uuid",
            ids["rule"],
        )
        executed = await AggregateInspectionExecutor(
            repository=repository,
            lease_owner="green-full-dispatch-test",
            lease_seconds=60,
            retry_delay_seconds=1,
        ).execute_wakeup(
            work_item_id=work["id"],
            task_id=ids["task"],
            message_version=work["row_version"],
        )
        assert executed
        row = await connection.fetchrow(
            "SELECT execution_status,judge_output FROM qc_rule_execution WHERE task_id=$1::uuid",
            ids["task"],
        )
        assert row is not None and row["execution_status"] == status
        output = json.loads(row["judge_output"])
        assert output["execution_status"] == status
        green = output["wrapper_payload"]["green"]
        assert len(green["subitems"]) == 2
        assert bool(green["missing_evidence"]) == (status == "evidence_insufficient")
        assert await connection.fetchval(
            "SELECT count(*) FROM qc_review_item WHERE task_id=$1::uuid",
            ids["task"],
        ) == int(status == "evidence_insufficient")
