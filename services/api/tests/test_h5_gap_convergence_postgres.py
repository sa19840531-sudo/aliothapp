"""跨 Capture 同缺口用真实 SQL 验证，只收敛已证明无收益的独立取证。"""

import json
from uuid import UUID, uuid4

import pytest

from services.api.app.infrastructure.db.h5_recapture_coalescing_repository import (
    PostgresH5RecaptureCoalescingRepository,
)
from services.api.tests.test_h5_llm_budget_postgres import database as database
from services.api.tests.test_h5_llm_budget_postgres import seed
from services.api.tests.test_h5_recapture_coalescing import request


@pytest.mark.parametrize(
    "proof,fault",
    [
        ("blocked", None),
        ("succeeded", None),
        ("succeeded", "attachment"),
        ("succeeded", "action"),
        ("blocked", "bytes"),
        ("blocked", "single"),
        ("blocked", "condition"),
        ("blocked", "overlay"),
        ("blocked", "latest_manifest_failed"),
        ("blocked", "latest_manifest_pending"),
        ("blocked", "source_manifest_failed"),
    ],
)
async def test_same_gap_uses_actual_completed_or_unproductive_probe(database, proof, fault):
    task, run, _ = await seed(database)
    rule = str(uuid4())
    session = f"h5:{run}"
    current = request(
        task_id=task,
        rule_id=rule,
        target_capture_id=str(uuid4()),
        required_artifact_types=("ui_snapshot",),
    )
    async with database.acquire() as connection:
        await connection.execute("""
            ALTER TABLE qc_task ADD finalization_state text DEFAULT 'open';
            CREATE TABLE qc_capture(id uuid,task_id uuid,agent_run_id uuid,surface_session_id text,
                capture_status text,surface_kind text,action_execution_id text,capture_role text);
            CREATE TABLE qc_page_capture(id uuid,task_id uuid,page_state text);
            CREATE TABLE qc_page_graph_state(state_id uuid,task_id uuid,agent_run_id uuid,
                finalization_epoch int,surface_session_id text,identity_source text,
                logical_page_key text,state_fingerprint text,
                identity_status text,is_overlay bool,is_external bool);
            CREATE TABLE qc_inspection_work_item(id text,task_id uuid,finalization_epoch int,
                ruleset_snapshot_hash text);
            CREATE TABLE qc_inspection_work_item_rule(work_item_id text,rule_id uuid,
                rule_code text,contract_hash text);
            CREATE TABLE qc_evidence_manifest(manifest_id text,task_id uuid,capture_id uuid,
                quality_status text,
                manifest_version int,manifest_payload jsonb);
            CREATE TABLE qc_evidence_request(request_id text,request_payload jsonb,task_id uuid,
                target_capture_id uuid,rule_id uuid,rule_code text,work_item_id text,
                requirement_hash text,status text,resolution_reason text,
                finalization_epoch int,created_at timestamptz DEFAULT now());
            CREATE TABLE qc_v2_work_item(work_id text,task_id uuid,evidence_request_id text,
                work_type text,finalization_epoch int,session_id text,source_capture_id uuid,
                status text,last_error_code text,
                terminal_payload jsonb,finished_at timestamptz DEFAULT now());
            CREATE TABLE qc_action_execution(action_execution_id text,task_id uuid,session_id text,
                finalization_epoch int,
                status text,intent_payload jsonb,result_capture_id uuid);
            CREATE TABLE qc_evidence_supplement(task_id uuid,request_id text,probe_work_id text,
                manifest_id text);
        """)

        async def frame(capture, *, action=None, result=False, changed=False):
            await connection.execute(
                "INSERT INTO qc_capture VALUES($1,$2,$3,$4,'captured','web',$5,$6)",
                UUID(capture),
                UUID(task),
                UUID(run),
                session,
                action,
                "probe_result_replay" if result else "after_action",
            )
            await connection.execute(
                "INSERT INTO qc_page_capture VALUES($1,$2,'stable')", UUID(capture), UUID(task)
            )
            await connection.execute(
                """INSERT INTO qc_page_graph_state
                VALUES($1,$2,$3,4,$4,'logical_surface','object','state','confirmed',$5,false)""",
                UUID(capture),
                UUID(task),
                UUID(run),
                session,
                changed and fault == "overlay",
            )
            await connection.execute(
                "INSERT INTO qc_evidence_manifest VALUES($1,$2,$3,'pass',1,$4)",
                capture,
                UUID(task),
                UUID(capture),
                json.dumps(
                    {
                        "artifacts": [
                            {
                                "artifact_type": "ui_snapshot",
                                "checksum": "new-content"
                                if changed and fault == "bytes"
                                else "same-content",
                                "role": "probe_result_replay" if result else "after_action",
                                "size_bytes": 12,
                                "metadata": {
                                    "presentation": {"document_epoch_ms": 200 if result else 100}
                                },
                            }
                        ]
                    }
                ),
            )

        async def requirement(item, status):
            work = item.request_id
            await connection.execute(
                "INSERT INTO qc_inspection_work_item VALUES($1,$2,4,'ruleset')", work, UUID(task)
            )
            await connection.execute(
                "INSERT INTO qc_inspection_work_item_rule VALUES($1,$2,$3,'contract')",
                work,
                UUID(rule),
                item.rule_code,
            )
            await connection.execute(
                """INSERT INTO qc_evidence_request
                (request_id,request_payload,task_id,target_capture_id,rule_id,rule_code,
                 work_item_id,requirement_hash,
                 status,resolution_reason,finalization_epoch)
                 VALUES($1,$2,$3,$4,$5,$6,$1,$7,$8,$9,4)""",
                item.request_id,
                json.dumps(item.to_dict()),
                UUID(task),
                UUID(item.target_capture_id),
                UUID(rule),
                item.rule_code,
                item.requirement_hash,
                status,
                "h5_probe_terminal:blocked" if status == "unfulfillable" else None,
            )

        await frame(current.target_capture_id, changed=True)
        if fault in {"latest_manifest_failed", "latest_manifest_pending"}:
            await connection.execute(
                "INSERT INTO qc_evidence_manifest SELECT manifest_id||'-failed',task_id,capture_id,"
                "$2,2,manifest_payload FROM qc_evidence_manifest WHERE capture_id=$1",
                UUID(current.target_capture_id),
                "pending" if fault == "latest_manifest_pending" else "fail",
            )
        await requirement(current, "running")
        priors = []
        for index in range(1 if fault == "single" else 2):
            prior = request(
                task_id=task,
                rule_id=rule,
                target_capture_id=str(uuid4()),
                required_artifact_types=("ui_snapshot",),
            )
            priors.append(prior.request_id)
            source = str(uuid4())
            probe = f"probe-{index}"
            action = f"action-{index}" if proof == "succeeded" else None
            await frame(prior.target_capture_id)
            await frame(source, action=action, result=proof == "succeeded")
            if fault == "source_manifest_failed":
                await connection.execute(
                    "INSERT INTO qc_evidence_manifest SELECT manifest_id||'-failed',task_id,"
                    "capture_id,'fail',2,manifest_payload FROM qc_evidence_manifest "
                    "WHERE capture_id=$1", UUID(source),
                )
            await requirement(prior, "satisfied" if proof == "succeeded" else "unfulfillable")
            await connection.execute(
                """INSERT INTO qc_v2_work_item
                (work_id,task_id,evidence_request_id,work_type,finalization_epoch,session_id,source_capture_id,status,terminal_payload)
                VALUES($1,$2,$3,'probe',4,$4,$5,$6,$7)""",
                probe,
                UUID(task),
                prior.request_id,
                session,
                UUID(prior.target_capture_id) if proof == "succeeded" else UUID(source),
                proof,
                json.dumps({"status": proof}),
            )
            if proof == "succeeded" and fault != "action":
                await connection.execute(
                    "INSERT INTO qc_action_execution VALUES($1,$2,$3,4,'completed',$4,$5)",
                    action,
                    UUID(task),
                    session,
                    json.dumps({"parameters": {"exploration_work_id": probe}}),
                    UUID(source),
                )
                if fault != "attachment":
                    await connection.execute(
                        "INSERT INTO qc_evidence_supplement VALUES($1,$2,$3,$4)",
                        UUID(task),
                        prior.request_id,
                        probe,
                        source,
                    )
        if fault == "condition":
            current = request(
                task_id=task,
                rule_id=rule,
                target_capture_id=current.target_capture_id,
                evidence_goal="必须等到真实广告失败条件",
                required_artifact_types=("ui_snapshot",),
            )
    result = await PostgresH5RecaptureCoalescingRepository(pool=database).has_unchanged_gap(
        request=current,
        agent_run_id=run,
        finalization_epoch=4,
    )
    assert result == (",".join(sorted(priors)) if fault is None else None)
