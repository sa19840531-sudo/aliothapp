"""绿色正式 TASK 输入到复核写回的隔离 PG 集成；图片与设备事实均为夹具。"""

import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
from uuid import uuid4

import pytest
from PIL import Image

from services.api.app.application.aggregate_inspection_execution import (
    AggregateInspectionExecutor,
    AggregateWakeupClaimDisposition,
    evaluate_aggregate_context,
)
from services.api.app.application.aggregate_inspection_work_items import (
    PublishAggregateInspectionWorkItemCommand,
)
from services.api.app.application.green_rule_catalog import build_green_rule_payload
from services.api.app.application.inspection_work_items import build_inspection_rule_contract_hash
from services.api.app.application.task_rule_inventory import parse_task_rule_inventory
from services.api.app.domain.evidence import (
    ArtifactReference,
    EvidenceInputCandidate,
    EvidenceInputScope,
    EvidenceQualityStatus,
    build_evidence_manifest_reference,
    seal_evidence_input_set,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    CaptureDirective,
    CaptureProfile,
    InteractionOperation,
    SurfaceKind,
    build_action_intent_hash,
)
from services.api.app.domain.protocol import stable_protocol_hash
from services.api.app.domain.task.targets import TaskTarget, TaskTargetPlatform
from services.api.app.infrastructure.db.aggregate_inspection_execution_repository import (
    AggregateInspectionExecutionPersistenceError,
    PostgresAggregateInspectionExecutionRepository,
)
from services.api.app.infrastructure.db.aggregate_inspection_publication_repository import (
    PostgresAggregateInspectionPublicationRepository,
)
from services.api.app.infrastructure.db.evidence_manifest_repository import (
    PostgresEvidenceManifestRepository,
)
from services.api.app.infrastructure.db.task_repository import PostgresTaskRepository
from services.api.app.infrastructure.storage.local_evidence_store import LocalEvidenceArtifactStore
from services.api.tests.test_app_postgres_integration import (
    _upgrade_isolated_postgres,  # noqa: F401 - 只升级显式独立测试库
)
from services.api.tests.test_task_rule_inventory_postgres import _database


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


@asynccontextmanager
async def green_case(tmp_path, key, *, publish=True, local_display=None, transactional=True):
    """复用租户/构建夹具，再通过正式提交仓储创建全新的第 4 版任务。"""
    async with _database(transactional=transactional) as (
        connection,
        pool,
        ids,
        rules,
        _,
        _,
        command,
    ):
        rule = build_green_rule_payload(key)
        await connection.execute(
            """UPDATE qc_rule SET rule_code=$2,name=$3,category=$4,executor_type=$5,
                   check_point=$6,input_contract=$7::jsonb,judge_config=$8::jsonb,
                   manual_review_policy=$9::jsonb,severity=$10,applicable_page_types=$11::jsonb,
                   status='enabled' WHERE id=$1::uuid""",
            rules["eligible"],
            rule["rule_code"],
            rule["name"],
            rule["category"],
            rule["executor_type"],
            rule["check_point"],
            json.dumps(rule["input_contract"]),
            json.dumps(rule["judge_config"]),
            json.dumps(rule["manual_review_policy"]),
            rule["severity"],
            json.dumps(rule["applicable_page_types"]),
        )
        await connection.execute(
            "UPDATE qc_ruleset SET version=4,snapshot_hash='green-fixture-v4' WHERE id=$1::uuid",
            ids["ruleset"],
        )
        if local_display:
            command = replace(
                command,
                target=TaskTarget(
                    platform=TaskTargetPlatform.H5,
                    target_url="https://controlled.invalid/list",
                ),
                task_config={
                    key: value for key, value in command.task_config.items() if key != "app"
                },
            )
        submitted = await PostgresTaskRepository(pool=pool).create_submission(
            replace(
                command,
                ruleset_version=4,
                idempotency_key=f"green-{uuid4()}",
                request_hash=uuid4().hex * 2,
            )
        )
        if not transactional:
            # 跨连接只读测试需提交夹具；底层清理器继续负责本例唯一保留的新任务。
            await connection.execute(
                "DELETE FROM qc_outbox_event WHERE task_id=$1::uuid", ids["task"]
            )
            await connection.execute("DELETE FROM qc_task WHERE id=$1::uuid", ids["task"])
            ids.update(task=submitted.task.task_id, run=submitted.run_id)
        ids = {
            **ids,
            "task": submitted.task.task_id,
            "run": submitted.run_id,
            "rule": rules["eligible"],
            "session": f"h5:green-controlled-{uuid4()}"
            if local_display
            else f"green-controlled-{uuid4()}",
        }
        inventory = parse_task_rule_inventory(submitted.task.task_config)
        assert inventory is not None and inventory["ruleset_version"] == 4
        frozen = next(item for item in inventory["rules"] if item["rule_id"] == ids["rule"])
        assert (
            frozen["judge_config"]["green_rule_contract"]["schema_version"]
            == "green-rule-contract.v2"
        )
        await connection.execute(
            """UPDATE qc_task SET status='running',finalization_state='session_draining',
                   finalization_agent_run_id=$2::uuid,exploration_draining_at=now(),
                   session_draining_at=now()
               WHERE id=$1::uuid""",
            ids["task"],
            ids["run"],
        )
        store = LocalEvidenceArtifactStore(root=tmp_path / "controlled-originals")
        candidates, manifests, intent, artifacts = await _save_action_originals(
            connection,
            pool,
            ids,
            store,
            local_display=local_display,
        )
        contract_hash = build_inspection_rule_contract_hash(frozen)
        scope_key = frozen["judge_config"]["green_rule_contract"]["input_kind"]
        input_set = seal_evidence_input_set(
            task_id=ids["task"],
            source_agent_run_id=ids["run"],
            rule_id=ids["rule"],
            rule_code=rule["rule_code"],
            rule_contract_hash=contract_hash,
            ruleset_snapshot_hash=inventory["ruleset_snapshot_hash"],
            scope=EvidenceInputScope.TASK,
            scope_key=scope_key,
            finalization_epoch=0,
            eligibility_snapshot_id="controlled-all-inputs",
            eligibility_snapshot_hash=stable_protocol_hash(
                [item.manifest.capture_id for item in candidates]
            ),
            selection_policy_hash=stable_protocol_hash(
                {"scope": scope_key, "fixture": "all three captures"}
            ),
            eligible_capture_ids=tuple(item.manifest.capture_id for item in candidates),
            candidates=tuple(candidates),
            sealed_at=datetime.now(UTC),
        )
        if publish:
            publication = PostgresAggregateInspectionPublicationRepository(pool=pool)
            assert await publication.persist_published(
                input_set=input_set,
                expected_member_count=len(candidates),
                scope_plan=None,
                command=PublishAggregateInspectionWorkItemCommand(
                    task_id=ids["task"],
                    evidence_input_set_id=input_set.input_set_id,
                    producer_agent_run_id=ids["run"],
                    rule_id=ids["rule"],
                    rule_code=rule["rule_code"],
                    rule_contract_hash=contract_hash,
                    scope=EvidenceInputScope.TASK,
                    scope_key=scope_key,
                    finalization_epoch=0,
                ),
            )
        work = await connection.fetchrow(
            "SELECT id::text,row_version FROM qc_aggregate_inspection_work_item "
            "WHERE task_id=$1::uuid",
            ids["task"],
        )
        repository = PostgresAggregateInspectionExecutionRepository(pool=pool, artifact_store=store)
        yield (
            connection,
            pool,
            ids,
            work,
            repository,
            input_set,
            frozen,
            manifests,
            intent,
            artifacts,
        )


async def _save_action_originals(connection, pool, ids, store, *, local_display=None):
    now = datetime.now(UTC)
    origin, before, after = (str(uuid4()) for _ in range(3))
    action_id = f"green-action-{uuid4()}"
    surface_kind = SurfaceKind.WEB if local_display else SurfaceKind.ANDROID
    await connection.execute(
        """INSERT INTO qc_surface_session(session_id,task_id,surface_kind,session_owner,status,
               finalization_epoch,platform_build_ref,activated_at)
           VALUES($1,$2::uuid,$4,'controlled-green-integration','active',0,$3,now())""",
        ids["session"],
        ids["task"],
        None if local_display else f"app-build:{ids['build']}",
        surface_kind.value,
    )
    intent = ActionIntent(
        task_id=ids["task"],
        session_id=ids["session"],
        intent_id=f"green-intent-{uuid4()}",
        idempotency_key=f"green-key-{uuid4()}",
        surface_kind=surface_kind,
        source_capture_id=origin,
        action_type=InteractionOperation.CLICK if local_display else InteractionOperation.TAP,
        parameters=local_display["parameters"] if local_display else {},
        semantic_target="受控只读按钮",
        purpose="隔离共享判定集成夹具，不代表设备采集",
        deadline=now + timedelta(hours=1),
        capture_profile=CaptureProfile(
            profile_id="controlled-green",
            before=CaptureDirective(
                role="before_action",
                artifact_types=("screenshot", "ui_snapshot") if local_display else ("screenshot",),
            ),
            after=CaptureDirective(
                role="after_action",
                artifact_types=("screenshot", "ui_snapshot", "action_trace")
                if local_display
                else ("screenshot",),
            ),
        ),
    )
    await connection.execute(
        """INSERT INTO qc_capture(id,task_id,agent_run_id,surface_kind,surface_session_id,
               capture_role,session_command_sequence,capture_status,captured_at,metadata)
           VALUES($1::uuid,$2::uuid,$3::uuid,$6,$4,'bootstrap',1,'captured',$5,
                  '{"finalization_epoch":0,"fixture":"controlled"}'::jsonb)""",
        origin,
        ids["task"],
        ids["run"],
        ids["session"],
        now - timedelta(milliseconds=3),
        surface_kind.value,
    )
    await connection.execute(
        """INSERT INTO qc_action_execution(action_execution_id,task_id,session_id,intent_id,
               idempotency_key,intent_hash,intent_protocol_version,intent_payload,command_sequence,
               finalization_epoch,requested_source_capture_id)
           VALUES($1,$2::uuid,$3,$4,$5,$6,'action-intent.v1',$7::jsonb,2,0,$8::uuid)""",
        action_id,
        ids["task"],
        ids["session"],
        intent.intent_id,
        intent.idempotency_key,
        build_action_intent_hash(intent),
        json.dumps(intent.to_dict()),
        origin,
    )
    for capture, role, parent, instant in (
        (before, "before_action", origin, now - timedelta(milliseconds=1)),
        (after, "after_action", before, now + timedelta(milliseconds=1)),
    ):
        await connection.execute(
            """INSERT INTO qc_capture(id,task_id,agent_run_id,surface_kind,surface_session_id,
                   capture_role,session_command_sequence,capture_status,captured_at,
                   action_execution_id,parent_capture_id,metadata)
               VALUES($1::uuid,$2::uuid,$3::uuid,$9,$4,$5,2,'captured',$6,$7,$8::uuid,
                      '{"finalization_epoch":0,"fixture":"controlled"}'::jsonb)""",
            capture,
            ids["task"],
            ids["run"],
            ids["session"],
            role,
            instant,
            action_id,
            parent,
            surface_kind.value,
        )
    await connection.execute(
        """UPDATE qc_action_execution SET source_capture_id=$2::uuid,result_capture_id=$3::uuid,
               status='completed',started_at=$4,action_finished_at=$4,evidence_finished_at=$4
           WHERE action_execution_id=$1""",
        action_id,
        before,
        after,
        now,
    )
    candidates, manifests, artifacts = [], [], []
    for index, (capture, role) in enumerate(
        ((origin, "bootstrap"), (before, "before_action"), (after, "after_action")),
        1,
    ):
        output = BytesIO()
        Image.new("RGB", (32, 32), (index * 70, 20, 20)).save(output, format="PNG")
        material = [("screenshot", output.getvalue())]
        if local_display and role in {"before_action", "after_action"}:
            snapshot = dict(local_display["before" if role == "before_action" else "after"])
            snapshot["captured_at"] = (
                now + timedelta(milliseconds=-1 if role == "before_action" else 1)
            ).isoformat()
            material.append(("ui_snapshot", snapshot))
            if role == "after_action":
                material.append(("action_trace", local_display["trace"]))
        references = []
        for artifact_type, content in material:
            original = await store.store(
                task_id=ids["task"],
                page_capture_id=capture,
                artifact_id=str(uuid4()),
                artifact_type=artifact_type,
                content=content,
            )
            artifacts.append(original)
            await connection.execute(
                """INSERT INTO qc_evidence_artifact(id,task_id,page_capture_id,artifact_type,source,
                       storage_path,checksum,size_bytes,content_type)
                   VALUES($1::uuid,$2::uuid,$3::uuid,$8,'baseline',$4,$5,$6,$7)""",
                original.artifact_id,
                ids["task"],
                capture,
                original.storage_path,
                original.checksum,
                original.size_bytes,
                original.content_type,
                artifact_type,
            )
            references.append(
                ArtifactReference(
                    artifact_id=original.artifact_id,
                    artifact_type=artifact_type,
                    role=role,
                    storage_path=original.storage_path,
                    checksum=original.checksum,
                    size_bytes=original.size_bytes,
                    content_type=original.content_type,
                )
            )
        manifest = build_evidence_manifest_reference(
            task_id=ids["task"],
            session_id=ids["session"],
            capture_id=capture,
            manifest_version=1,
            quality_status=EvidenceQualityStatus.PASS,
            quality_reason="controlled-original-sha256-verified",
            created_at=now,
            artifacts=tuple(references),
        )
        await PostgresEvidenceManifestRepository(pool=pool).add(manifest=manifest)
        manifests.append(manifest)
        candidates.append(
            EvidenceInputCandidate(manifest=manifest, capture_role=role, page_sequence=index)
        )
    return candidates, manifests, intent, artifacts


@pytest.mark.parametrize("key", ["s72", "s35"])
async def test_frozen_green_task_input_writes_missing_verdict_and_review_atomically(tmp_path, key):
    async with green_case(tmp_path, key) as (
        connection,
        _pool,
        ids,
        work,
        repository,
        input_set,
        frozen,
        _manifests,
        _intent,
        _artifacts,
    ):
        await connection.execute(
            """UPDATE qc_rule SET rule_code='edited_' || rule_code,status='disabled',
                   judge_config='{}',executor_type='rule_engine',severity='low'
               WHERE id=$1::uuid""",
            ids["rule"],
        )
        claim = await repository.claim_for_wakeup(
            work_item_id=work["id"],
            task_id=ids["task"],
            message_version=work["row_version"],
            lease_owner="controlled-green",
            lease_seconds=60,
        )
        assert (
            claim.disposition is AggregateWakeupClaimDisposition.CLAIMED and claim.lease is not None
        )
        context = await repository.load_judge_context(lease=claim.lease)
        assert context.input_set == input_set and context.input_set.scope is EvidenceInputScope.TASK
        assert context.journey_input is None
        assert context.rule_payload["judge_config"] == frozen["judge_config"]
        assert context.green_evidence.verified_read and not context.green_evidence.issues
        assert len(context.green_evidence.actions) == 1
        judgement = evaluate_aggregate_context(context)
        assert judgement.execution_status == "evidence_insufficient"
        await repository.complete_judgement(context=context, judgement=judgement)
        row = await connection.fetchrow(
            """SELECT execution.execution_status,execution.judge_strategy,execution.severity,
                      execution.judge_output,review.status AS review_status,review.priority,
                      candidate.status AS candidate_status,attempt.status AS attempt_status,
                      packet.evidence_input_set_id
               FROM qc_rule_execution execution
               JOIN qc_review_item review ON review.rule_execution_id=execution.id
               JOIN qc_observation_packet packet ON packet.id=execution.observation_packet_id
               JOIN qc_aggregate_inspection_candidate candidate
                 ON candidate.work_item_id=execution.aggregate_work_item_id
               JOIN qc_aggregate_inspection_attempt attempt
                 ON attempt.work_item_id=candidate.work_item_id
               WHERE execution.task_id=$1::uuid""",
            ids["task"],
        )
        assert row["execution_status"] == row["attempt_status"] == "evidence_insufficient"
        assert row["review_status"] == "pending" and row["priority"] == 50
        assert row["judge_strategy"] == "hybrid" and row["severity"] == "medium"
        assert row["candidate_status"] == "completed"
        assert row["evidence_input_set_id"] == input_set.input_set_id
        green = _json(row["judge_output"])["wrapper_payload"]["green"]
        assert all(
            item["status"] == "evidence_insufficient" and item["missing"]
            for item in green["subitems"]
        )
        assert "storage_path" not in json.dumps(green)
        assert await AggregateInspectionExecutor(
            repository=repository,
            lease_owner="duplicate-wakeup",
            lease_seconds=60,
            retry_delay_seconds=1,
        ).execute_wakeup(
            work_item_id=work["id"], task_id=ids["task"], message_version=work["row_version"]
        )
        for table in ("qc_rule_execution", "qc_review_item", "qc_aggregate_inspection_attempt"):
            assert (
                await connection.fetchval(
                    f"SELECT count(*) FROM {table} WHERE task_id=$1::uuid", ids["task"]
                )
                == 1
            )


@pytest.mark.parametrize("drift", ["late_manifest", "original_bytes", "new_action"])
async def test_green_writeback_rejects_late_evidence_or_action_drift(tmp_path, drift):
    async with green_case(tmp_path, "s72") as (
        connection,
        pool,
        ids,
        work,
        repository,
        _input_set,
        _frozen,
        manifests,
        intent,
        artifacts,
    ):
        claim = await repository.claim_for_wakeup(
            work_item_id=work["id"],
            task_id=ids["task"],
            message_version=work["row_version"],
            lease_owner="controlled-drift",
            lease_seconds=60,
        )
        context = await repository.load_judge_context(lease=claim.lease)
        judgement = evaluate_aggregate_context(context)
        assert not context.green_evidence.issues
        if drift == "late_manifest":
            manifest = manifests[-1]
            await PostgresEvidenceManifestRepository(pool=pool).add(
                manifest=build_evidence_manifest_reference(
                    task_id=manifest.task_id,
                    session_id=manifest.session_id,
                    capture_id=manifest.capture_id,
                    manifest_version=2,
                    artifacts=manifest.artifacts,
                    quality_status=EvidenceQualityStatus.FAIL,
                    quality_reason="controlled-late-quality-rejection",
                    created_at=datetime.now(UTC),
                )
            )
        elif drift == "original_bytes":
            # 模拟独立测试目录中的对象损坏；正式存储接口禁止同身份覆盖。
            (tmp_path / "controlled-originals" / artifacts[-1].storage_path).write_bytes(
                b"controlled-drift"
            )
        else:
            extra = replace(
                intent, intent_id=f"late-intent-{uuid4()}", idempotency_key=f"late-key-{uuid4()}"
            )
            await connection.execute(
                """INSERT INTO qc_action_execution(action_execution_id,task_id,session_id,intent_id,
                       idempotency_key,intent_hash,intent_protocol_version,intent_payload,command_sequence,
                       finalization_epoch,requested_source_capture_id)
                   VALUES($1,$2::uuid,$3,$4,$5,$6,'action-intent.v1',$7::jsonb,3,0,$8::uuid)""",
                f"late-action-{uuid4()}",
                ids["task"],
                ids["session"],
                extra.intent_id,
                extra.idempotency_key,
                build_action_intent_hash(extra),
                json.dumps(extra.to_dict()),
                extra.source_capture_id,
            )
        with pytest.raises(
            AggregateInspectionExecutionPersistenceError, match="原件或正式动作已变化"
        ):
            await repository.complete_judgement(context=context, judgement=judgement)
        for table in ("qc_rule_execution", "qc_review_item", "qc_observation_packet"):
            assert (
                await connection.fetchval(
                    f"SELECT count(*) FROM {table} WHERE task_id=$1::uuid", ids["task"]
                )
                == 0
            )
        assert (
            await connection.fetchval(
                "SELECT status FROM qc_aggregate_inspection_work_item WHERE id=$1::uuid",
                work["id"],
            )
            == "leased"
        )
