"""H5 迟到发布与逻辑状态去重的真实 PostgreSQL 围栏回归。"""

import json
import os
from contextlib import asynccontextmanager
from uuid import NAMESPACE_URL, uuid4, uuid5

import asyncpg
import pytest

from services.api.app.infrastructure.db.h5_completion_gate_sql import (
    AUDITABLE_H5_INVENTORY_CTE_SQL,
    H5_ALREADY_INSPECTED_LOGICAL_STATE_SQL,
    QUALIFIED_H5_INVENTORY_CTE_SQL,
)
from services.api.app.infrastructure.db.h5_consumer_run_lifecycle import (
    finish_h5_consumer_run_if_idle_on_connection,
)
from services.api.app.infrastructure.db.h5_evidence_handoff_repository import (
    PostgresH5EvidenceCaptureReader,
)
from services.api.app.infrastructure.db.h5_inspection_publication_repository import (
    PostgresH5InspectionPublicationRepository,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"), reason="需要显式 PostgreSQL 测试连接",
)


@pytest.mark.parametrize("platform,previous_state,duplicate", [
    ("h5", "same", True),
    ("h5", "different", False),
    ("h5", "unpublished", False),
    ("web", "same", False),
])
async def test_capture_reader_executes_complete_grouped_query(
    platform, previous_state, duplicate,
):
    connection = await asyncpg.connect(os.environ["ALIOTH_TEST_DATABASE_URL"])
    transaction = connection.transaction()
    await transaction.start()
    try:
        await connection.execute("""
            CREATE TEMP TABLE qc_task(id uuid PRIMARY KEY,target_platform text,
                status text,finalization_state text,finalization_epoch int,
                finalization_agent_run_id uuid,task_config jsonb);
            CREATE TEMP TABLE qc_agent_run(id uuid PRIMARY KEY,task_id uuid,
                status text,agent_type text,input_context jsonb);
            CREATE TEMP TABLE qc_action_execution(action_execution_id text PRIMARY KEY,
                task_id uuid,session_id text,finalization_epoch int,command_sequence int,
                intent_payload jsonb,result_capture_id uuid,source_capture_id uuid);
            CREATE TEMP TABLE qc_surface_session(task_id uuid,session_id text,
                session_owner text,status text,surface_kind text,finalization_epoch int);
            CREATE TEMP TABLE qc_capture(id uuid PRIMARY KEY,task_id uuid,agent_run_id uuid,
                surface_kind text,surface_session_id text,capture_role text,
                action_execution_id text,metadata jsonb,created_at timestamptz);
            CREATE TEMP TABLE qc_page_capture(id uuid PRIMARY KEY,task_id uuid,
                agent_run_id uuid,page_type text,page_state text,page_seq int,
                detection_status text,metadata jsonb,capture_role text,url text,
                inspection_status text,inspection_lifecycle_status text);
            CREATE TEMP TABLE qc_h5_page_type_assessment(assessment_record_id text PRIMARY KEY,
                task_id uuid,agent_run_id uuid,page_capture_id uuid,detection_status text,
                page_type text,payload_hash text,assessment_payload jsonb,updated_at timestamptz);
            CREATE TEMP TABLE qc_evidence_artifact(id uuid PRIMARY KEY,task_id uuid,
                page_capture_id uuid,artifact_type text,storage_path text,checksum text,
                size_bytes int,lifecycle_status text,metadata jsonb DEFAULT '{}');
            CREATE TEMP TABLE qc_evidence_manifest(manifest_id text PRIMARY KEY,
                task_id uuid,capture_id uuid,manifest_hash text,quality_status text,
                manifest_version int);
            CREATE TEMP TABLE qc_surface_phase_run(phase_run_id text,task_id uuid,
                session_id text,phase_key text,source_capture_id uuid);
            CREATE TEMP TABLE qc_surface_phase_step(phase_run_id text,task_id uuid,
                session_id text,result_capture_id uuid,step_key text);
            CREATE TEMP TABLE qc_h5_evidence_inventory_item(task_id uuid,status text,
                after_capture_id uuid,agent_run_id uuid,published_inspection_count int,
                session_id text,action_execution_id text,page_type text,
                before_manifest_id text,before_capture_id uuid,before_manifest_hash text,
                after_manifest_id text,after_manifest_hash text,authority_source text);
        """)
        task, run, before, after, previous = (str(uuid4()) for _ in range(5))
        session = f"h5:{run}" if platform == "h5" else f"web-v2:{run}"
        owner = "h5-workflow" if platform == "h5" else "web-workflow"
        await connection.execute(
            "INSERT INTO qc_task VALUES($1,$2,'running','open',0,$3,"
            "'{\"execution_mode\":\"full_inspection\","
            "\"h5_inspection_notification_mode\":\"enforced\"}')", task, platform, run,
        )
        await connection.execute(
            "INSERT INTO qc_agent_run VALUES($1,$2,'running','explorer','{}')", run, task,
        )
        await connection.execute(
            "INSERT INTO qc_surface_session VALUES($1,$2,$3,'active','web',0)",
            task, session, owner,
        )
        await connection.execute(
            "INSERT INTO qc_action_execution VALUES('action',$1,$2,0,1,'{}',$3,$4)",
            task, session, after, before,
        )
        for identifier, role, seq, inspected in (
            (before, "before_action", 2, "captured"),
            (after, "after_action", 3, "captured"),
            (previous, "after_action", 1,
             "captured" if previous_state == "unpublished" else "completed"),
        ):
            await connection.execute(
                "INSERT INTO qc_capture VALUES($1,$2,$3,'web',$4,$5,'action','{}',now())",
                identifier, task, run, session, role,
            )
            await connection.execute(
                "INSERT INTO qc_page_capture VALUES($1,$2,$3,'home','stable',$4,"
                "'recognized','{}',$5,'https://product.test/',$6,NULL)",
                identifier, task, run, seq, role, inspected,
            )
            payload = {"detection_result": {"rule_matching_allowed": True,
                "logical_surface": {"identity_fingerprint": "home",
                    "state_fingerprint": "changed" if identifier == previous
                        and previous_state == "different" else "state"}}}
            await connection.execute(
                "INSERT INTO qc_h5_page_type_assessment VALUES($1,$2,$3,$4,"
                "'recognized','home','assessment-hash',$5::jsonb,now())",
                identifier, task, run, identifier, json.dumps(payload),
            )
        for identifier in (before, after):
            await connection.execute(
                "INSERT INTO qc_evidence_manifest VALUES($1,$2,$3,'manifest-hash','pass',1)",
                identifier, task, identifier,
            )
            for kind in ("screenshot", "dom"):
                await connection.execute(
                    "INSERT INTO qc_evidence_artifact VALUES($1,$2,$3,$4,$5,'checksum',1,'active')",
                    str(uuid4()), task, identifier, kind, f"{task}/{identifier}/{kind}",
                )

        class Pool:
            @asynccontextmanager
            async def acquire(self):
                yield connection

        captures = await PostgresH5EvidenceCaptureReader(pool=Pool()).list_judgeable_captures(
            task_id=task, agent_run_id=run,
        )
        assert len(captures) == 1
        capture = captures[0]
        assert capture.capture_id == after
        assert capture.logical_state_previously_published is duplicate
        assert set(capture.artifact_types) == {"screenshot", "dom"}
        assert len(capture.artifact_refs) == 2
        assert len(capture.before_artifact_refs) == 2
        assert capture.before_manifest_id == before
        assert capture.after_manifest_id == after
        assert await PostgresH5EvidenceCaptureReader(pool=Pool()).list_judgeable_captures(
            task_id=str(uuid4()), agent_run_id=run,
        ) == []
        await connection.execute(
            "INSERT INTO qc_h5_evidence_inventory_item VALUES($1,'pass',$2,$3,2,$4,"
            "'action','home',$5,$6,'manifest-hash',$7,'manifest-hash','pass_manifest')",
            task, after, run, session, before, before, after,
        )
        query = (
            "WITH target_task AS (SELECT * FROM qc_task WHERE id=$1::uuid), "
            f"{QUALIFIED_H5_INVENTORY_CTE_SQL}, {AUDITABLE_H5_INVENTORY_CTE_SQL} "
            "SELECT (SELECT count(*) FROM qualified_h5_inventory) AS eligible, "
            "(SELECT count(*) FROM auditable_h5_inventory) AS auditable"
        )
        counts = await connection.fetchrow(query, task)
        assert counts["eligible"] == (0 if duplicate else 1)
        assert counts["auditable"] == 1
        # 不重复发规则不等于免审；Manifest 被篡改后，既有发布仍必须失去证明。
        await connection.execute(
            "UPDATE qc_evidence_manifest SET manifest_hash='tampered' WHERE capture_id=$1", after,
        )
        counts = await connection.fetchrow(query, task)
        assert counts["eligible"] == 0
        assert counts["auditable"] == 0
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.mark.parametrize("identity_delta", [
    {}, {"task_id": "other"}, {"source": "foreign"},
    {"logical_identity": "foreign"}, {"finalization_epoch": 1},
])
@pytest.mark.parametrize("platform,status,stop_reason,sealed,epoch,allowed", [
    ("h5", "done", "h5_inspection_work_drained", False, 0, True),
    ("h5", "running", None, False, 0, True),
    ("h5", "done", "h5_inspection_work_drained", True, 0, False),
    ("h5", "done", "h5_inspection_work_drained", False, 1, False),
    ("h5", "failed", "h5_inspection_chain_failed", False, 0, False),
    ("h5", "canceled", "task_canceled", False, 0, False),
    ("web", "done", "h5_inspection_work_drained", False, 0, False),
    ("app", "done", "h5_inspection_work_drained", False, 0, False),
])
async def test_late_publication_reuses_only_owned_unsealed_h5_drained_run(
    platform, status, stop_reason, sealed, epoch, allowed, identity_delta,
):
    connection = await asyncpg.connect(os.environ["ALIOTH_TEST_DATABASE_URL"])
    transaction = connection.transaction()
    await transaction.start()
    try:
        await connection.execute("""
            CREATE TEMP TABLE qc_task(id uuid,target_platform text,status text,
                finalization_state text,finalization_epoch int);
            CREATE TEMP TABLE qc_agent_run(id uuid PRIMARY KEY,task_id uuid,
                agent_type text,agent_name text,status text,input_context jsonb,
                loop_guard jsonb,started_at timestamptz,finished_at timestamptz,
                updated_at timestamptz DEFAULT now(),stop_reason text,error_message text);
        """)
        task = str(uuid4())
        identity = f"h5-inspection-consumer:{task}:0"
        run = str(uuid5(NAMESPACE_URL, f"alioth:{identity}"))
        await connection.execute(
            "INSERT INTO qc_task VALUES($1,$2,'running',$3,$4)",
            task, platform, "sealed" if sealed else "session_draining", epoch,
        )
        context = {
            "logical_identity": identity, "finalization_epoch": 0,
            "source": "h5_inspection_publication",
            **{key: value for key, value in identity_delta.items() if key != "task_id"},
        }
        owner = str(uuid4()) if "task_id" in identity_delta else task
        await connection.execute(
            "INSERT INTO qc_agent_run(id,task_id,agent_type,status,input_context,"
            "finished_at,stop_reason) VALUES($1,$2,'rule_judge',$3,$4::jsonb,now(),$5)",
            run, owner, status, json.dumps(context), stop_reason,
        )
        repository = PostgresH5InspectionPublicationRepository()
        if allowed and not identity_delta:
            assert await repository._ensure_consumer_run(
                connection, task_id=task, finalization_epoch=0,
            ) == run
            assert await connection.fetchval("SELECT status FROM qc_agent_run") == "running"
            if status == "done":
                assert await connection.fetchval("SELECT finished_at FROM qc_agent_run") is None
            assert await repository._ensure_consumer_run(
                connection, task_id=task, finalization_epoch=0,
            ) == run
            assert await connection.fetchval("SELECT count(*) FROM qc_agent_run") == 1
        else:
            with pytest.raises(RuntimeError, match="身份冲突"):
                await repository._ensure_consumer_run(
                    connection, task_id=task, finalization_epoch=0,
                )
            assert await connection.fetchval("SELECT status FROM qc_agent_run") == status
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.mark.parametrize("platform,missing,expect_finished", [
    ("h5", True, False), ("h5", False, True), ("web", True, True),
])
async def test_consumer_cannot_finish_before_qualified_h5_capture_is_accounted_for(
    platform, missing, expect_finished,
):
    connection = await asyncpg.connect(os.environ["ALIOTH_TEST_DATABASE_URL"])
    transaction = connection.transaction()
    await transaction.start()
    try:
        await connection.execute("""
            CREATE TEMP TABLE qc_task(id uuid,target_platform text,status text,
                finalization_state text,finalization_epoch int,task_config jsonb,
                finalization_agent_run_id uuid,finalization_retry_at timestamptz,
                completion_gate_snapshot_id uuid,finalization_recovery_state text);
            CREATE TEMP TABLE qc_agent_run(id uuid,task_id uuid,agent_type text,status text,
                input_context jsonb,stop_reason text,error_message text,
                finished_at timestamptz,updated_at timestamptz);
            CREATE TEMP TABLE qc_h5_evidence_inventory_item(task_id uuid,status text,
                after_capture_id uuid,agent_run_id uuid,published_inspection_count int,
                session_id text,action_execution_id text,page_type text,
                before_manifest_id text,before_capture_id uuid,before_manifest_hash text,
                after_manifest_id text,after_manifest_hash text,authority_source text);
            CREATE TEMP TABLE qc_action_execution(task_id uuid,session_id text,
                action_execution_id text,finalization_epoch int,intent_payload jsonb);
            CREATE TEMP TABLE qc_surface_session(task_id uuid,session_id text,
                surface_kind text,finalization_epoch int);
            CREATE TEMP TABLE qc_capture(id uuid,task_id uuid,agent_run_id uuid,
                capture_role text,metadata jsonb);
            CREATE TEMP TABLE qc_page_capture(id uuid,task_id uuid,agent_run_id uuid,
                page_type text,page_state text,page_seq int,inspection_status text,
                inspection_lifecycle_status text,updated_at timestamptz);
            CREATE TEMP TABLE qc_evidence_manifest(manifest_id text,task_id uuid,
                capture_id uuid,manifest_hash text,quality_status text,manifest_version int);
            CREATE TEMP TABLE qc_h5_page_type_assessment(task_id uuid,agent_run_id uuid,
                page_capture_id uuid,detection_status text,page_type text,
                assessment_payload jsonb);
            CREATE TEMP TABLE qc_h5_inspection_publication(id uuid,task_id uuid,
                finalization_epoch int,page_capture_id uuid,status text);
            CREATE TEMP TABLE qc_h5_inspection_publication_work_item(publication_id uuid,
                work_item_id uuid);
            CREATE TEMP TABLE qc_inspection_work_item(id uuid,status text);
            CREATE TEMP TABLE qc_inspection_attempt(work_item_id uuid,status text);
            CREATE TEMP TABLE qc_outbox_event(task_id uuid,status text,
                aggregate_type text,aggregate_id text);
            CREATE TEMP TABLE qc_event_inbox(task_id uuid,status text,
                aggregate_type text,aggregate_id text);
        """)
        task, source, consumer, capture, before, previous = (str(uuid4()) for _ in range(6))
        await connection.execute(
            "INSERT INTO qc_task VALUES($1,$2,'running','session_draining',0,"
            "'{\"execution_mode\":\"full_inspection\","
            "\"h5_inspection_notification_mode\":\"enforced\"}',$3,NULL,NULL,'active')",
            task, platform, source,
        )
        await connection.execute(
            "INSERT INTO qc_agent_run VALUES($1,$2,'rule_judge','running',"
            "'{\"source\":\"h5_inspection_publication\",\"finalization_epoch\":0}',"
            "NULL,NULL,NULL,now())", consumer, task,
        )
        await connection.execute(
            "INSERT INTO qc_capture VALUES($1,$2,$3,'after_action','{}')", capture, task, source,
        )
        await connection.execute(
            "INSERT INTO qc_page_capture VALUES($1,$2,$3,'home','stable',2,'captured',NULL,now())",
            capture, task, source,
        )
        await connection.execute(
            "INSERT INTO qc_action_execution VALUES($1,'session','action',0,'{}')", task,
        )
        await connection.execute(
            "INSERT INTO qc_surface_session VALUES($1,'session','web',0)", task,
        )
        for manifest, identifier in (("before", before), ("after", capture)):
            await connection.execute(
                "INSERT INTO qc_evidence_manifest VALUES($1,$2,$3,'hash','pass',1)",
                manifest, task, identifier,
            )
        await connection.execute(
            "INSERT INTO qc_h5_page_type_assessment VALUES($1,$2,$3,'recognized','home','{}')",
            task, source, capture,
        )
        await connection.execute(
            "INSERT INTO qc_h5_evidence_inventory_item VALUES($1,'pass',$2,$3,0,'session',"
            "'action','home','before',$4,'hash','after','hash','pass_manifest')",
            task, capture, source, before,
        )
        await connection.execute(
            "INSERT INTO qc_h5_inspection_publication VALUES($1,$2,0,$3,'not_applicable')",
            str(uuid4()), task, previous if missing else capture,
        )
        assert await finish_h5_consumer_run_if_idle_on_connection(
            connection, task_id=task, consumer_agent_run_id=consumer,
        ) is expect_finished
        assert await connection.fetchval("SELECT status FROM qc_agent_run") == (
            "done" if expect_finished else "running"
        )
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.mark.parametrize("platform,change,duplicate", [
    ("h5", None, True),
    ("h5", "task", False),
    ("h5", "run", False),
    ("h5", "identity", False),
    ("h5", "state", False),
    ("h5", "page_type", False),
    ("h5", "page_state", False),
    ("h5", "evidence_only", False),
    ("h5", "unpublished", False),
    ("h5", "empty", False),
    ("web", None, False),
])
async def test_duplicate_state_requires_same_owned_published_semantic_identity(
    platform, change, duplicate,
):
    connection = await asyncpg.connect(os.environ["ALIOTH_TEST_DATABASE_URL"])
    transaction = connection.transaction()
    await transaction.start()
    try:
        await connection.execute("""
            CREATE TEMP TABLE qc_task(id uuid,target_platform text);
            CREATE TEMP TABLE qc_page_capture(id uuid,task_id uuid,agent_run_id uuid,
                page_seq int,page_type text,page_state text,inspection_status text,
                inspection_lifecycle_status text);
            CREATE TEMP TABLE qc_h5_page_type_assessment(task_id uuid,agent_run_id uuid,
                page_capture_id uuid,detection_status text,page_type text,
                assessment_payload jsonb);
        """)
        task, run, previous, current = (str(uuid4()) for _ in range(4))
        await connection.execute("INSERT INTO qc_task VALUES($1,$2)", task, platform)
        for capture, seq in ((previous, 1), (current, 2)):
            old = capture == previous
            owner_task = str(uuid4()) if old and change == "task" else task
            owner_run = str(uuid4()) if old and change == "run" else run
            page_type = "profile" if old and change == "page_type" else "home"
            page_state = "popup" if old and change == "page_state" else "stable"
            lifecycle = "evidence_only" if old and change == "evidence_only" else "active"
            status = "pending" if old and change == "unpublished" else "completed"
            surface = {
                "identity_fingerprint": "other" if old and change == "identity" else "page",
                "state_fingerprint": "other" if old and change == "state" else "state",
            }
            if change == "empty":
                surface = {"identity_fingerprint": "", "state_fingerprint": ""}
            await connection.execute(
                "INSERT INTO qc_page_capture VALUES($1,$2,$3,$4,$5,$6,$7,$8)",
                capture, owner_task, owner_run, seq, page_type, page_state, status, lifecycle,
            )
            await connection.execute(
                "INSERT INTO qc_h5_page_type_assessment VALUES($1,$2,$3,'recognized',$4,$5)",
                owner_task, owner_run, capture, page_type,
                json.dumps({"detection_result": {"logical_surface": surface}}),
            )
        result = await connection.fetchval(
            f"SELECT ({H5_ALREADY_INSPECTED_LOGICAL_STATE_SQL}) "
            "FROM qc_task AS task JOIN qc_page_capture AS page_capture "
            "ON page_capture.task_id=task.id WHERE page_capture.id=$1::uuid", current,
        )
        assert result is duplicate
    finally:
        await transaction.rollback()
        await connection.close()
