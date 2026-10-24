"""动作序列保留原关闭动作和最终 OBSERVE 两种引用。"""

import json
import os
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from services.api.app.application.web_exploration_workflow import (
    WebCaptureStage,
    web_capture_profile,
)
from services.api.app.application.web_interaction_precision import WebInteractionOutcome
from services.api.app.domain.interaction import (
    ActionIntent,
    InteractionOperation,
    SurfaceKind,
    build_action_intent_hash,
)
from services.api.app.infrastructure.db.web_evidence_sequence_repository import (
    PostgresWebEvidenceSequenceRepository,
)
from services.api.tests.test_web_capture_readiness_metrics import _stable_capture_metrics
from services.api.tests.test_web_logical_target_postgres import target_database
from services.api.tests.test_web_run_policy_snapshot_postgres import (
    policy_database_schema as policy_database_schema,
)
from services.api.tests.web_sequence_pg_support import seed_failure_frames

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"), reason="必须显式提供隔离PG双DSN"
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption",
    [None, "missing_refs", "wrong_original_result", "observation_as_click", "consumer_drift"],
)
async def test_interaction_registration_authenticates_original_action_and_observation(
    corruption, monkeypatch
):
    async with target_database() as (connection, pool, _other, ids):
        rule_id = str(uuid4())
        try:
            captures, _store = await seed_failure_frames(connection, pool, ids)
            await connection.execute(
                "UPDATE qc_capture SET metadata=jsonb_set(metadata,'{pipeline_metrics,web_readiness}',"
                "$2::jsonb) WHERE id=ANY($1::uuid[])",
                list(captures),
                json.dumps(_stable_capture_metrics()),
            )
            refs = []
            for ordinal, after in enumerate((captures[1], captures[3])):
                row = await connection.fetchrow(
                    "SELECT * FROM qc_action_execution WHERE result_capture_id=$1::uuid", after
                )
                intent = ActionIntent(
                    task_id=ids["task"],
                    session_id=ids["session"],
                    intent_id=row["intent_id"],
                    idempotency_key=row["idempotency_key"],
                    surface_kind=SurfaceKind.WEB,
                    source_capture_id=str(row["requested_source_capture_id"]),
                    action_type=InteractionOperation.CLICK
                    if ordinal == 0
                    else InteractionOperation.OBSERVE,
                    semantic_target="关闭原弹窗" if ordinal == 0 else "复检原业务页",
                    purpose="T9动作来源证明",
                    parameters={"web_sealed_candidate_capability": "web_obstruction_dismiss"}
                    if ordinal == 0
                    else {},
                    capture_profile=web_capture_profile(WebCaptureStage.PAGE_PROBE),
                    deadline=datetime.now(UTC) + timedelta(minutes=1),
                )
                await connection.execute(
                    "UPDATE qc_action_execution SET intent_payload=$2::jsonb,intent_hash=$3,"
                    "intent_protocol_version=$4,result_payload=$5::jsonb WHERE action_execution_id=$1",
                    row["action_execution_id"],
                    json.dumps(intent.to_dict()),
                    build_action_intent_hash(intent),
                    intent.protocol_version,
                    json.dumps(
                        {
                            "status": "succeeded",
                            "execution_outcome": "succeeded",
                            "error_code": None,
                        }
                    ),
                )
                refs.append(
                    {
                        "outcome_ordinal": 0,
                        "intent_id": intent.intent_id,
                        "requested_source_capture_id": intent.source_capture_id,
                        "action_result_capture_id": after,
                    }
                )
            outcome = WebInteractionOutcome(
                command_accepted=True,
                effect_observed=True,
                postcondition_satisfied=True,
                transition_arrived=True,
                after_capture_id=captures[3],
                failure_reason=None,
            )
            selected_refs = (
                []
                if corruption == "missing_refs"
                else [refs[1] if corruption == "observation_as_click" else refs[0]]
            )
            if corruption == "wrong_original_result":
                selected_refs = [dict(refs[0], action_result_capture_id=captures[3])]
            result = await PostgresWebEvidenceSequenceRepository(pool=pool).register_interaction(
                task_id=ids["task"],
                agent_run_id=ids["run"],
                session_id=ids["session"],
                source_capture_id=ids["capture"],
                evidence_capture_ids=(captures[1], captures[3]),
                page_role="menu",
                readiness_status="stable",
                interaction_outcomes=(asdict(outcome),),
                interaction_action_refs=tuple(selected_refs),
            )
            assert result["status"] == (
                "published" if corruption in (None, "consumer_drift") else "evidence_insufficient"
            ), result
            if corruption in (None, "consumer_drift"):
                payload = json.loads(
                    await connection.fetchval(
                        "SELECT sequence_payload FROM qc_web_evidence_sequence "
                        "WHERE sequence_id=$1::uuid",
                        result["sequence_id"],
                    )
                )
                assert payload["purpose"] == "interaction_sequence"
                fact = payload["interaction_outcomes"][0]
                assert fact["action_result_capture_id"] == captures[1]
                assert fact["after_capture_id"] == captures[3]
                assert fact["postcondition_satisfied"] is True
                from services.api.app.infrastructure.db.h5_inspection_publication_repository import (
                    PostgresH5InspectionPublicationRepository,
                )
                from services.api.tests.test_web_rule_evidence_contract import _contract
                from services.api.tests.web_sequence_consumer_pg_support import (
                    consume_sequence_with_actual_work,
                )

                contract = _contract()
                contract["accepted_roles"] = [
                    "stable_page",
                    "failure_sequence",
                    "interaction_sequence",
                ]
                await connection.execute(
                    "INSERT INTO qc_rule(id,ruleset_id,rule_code,name,category,executor_type,"
                    "severity,status,check_point,input_contract,judge_config) VALUES($1::uuid,$2::uuid,'t9_interaction',"
                    "'T9关闭序列','interaction','llm','medium','enabled','原弹窗关闭情况',$3::jsonb,$4::jsonb)",
                    rule_id,
                    ids["ruleset"],
                    json.dumps({"target_clients": ["WEB"], "web_evidence": contract}),
                    json.dumps({"tool_refs": ["semantic_rule_judge_tool"]}),
                )
                publications = PostgresH5InspectionPublicationRepository(pool=pool)
                claim = await publications.claim(
                    publication_id=result["publication_id"],
                    task_id=ids["task"],
                    message_version=1,
                    lease_owner="t9-interaction",
                    lease_seconds=60,
                )
                assert await publications.process_web_sequence(lease=claim.lease)
                if corruption == "consumer_drift":
                    await connection.execute(
                        "UPDATE qc_action_execution SET intent_hash=$2 WHERE result_capture_id=$1::uuid",
                        captures[1],
                        "f" * 64,
                    )
                consumed, packets = await consume_sequence_with_actual_work(
                    connection, pool, _store, ids, claim.lease.consumer_agent_run_id, monkeypatch
                )
                assert len(packets) == (0 if corruption == "consumer_drift" else 1), consumed
                if packets:
                    model_facts = packets[0].packet_payload["facts"]["web_sequence"][
                        "interaction_outcomes"
                    ][0]
                    assert model_facts["action_result_capture_id"] == captures[1]
                    assert model_facts["after_capture_id"] == captures[3]
        finally:
            await connection.execute(
                "DELETE FROM qc_outbox_event WHERE task_id=$1::uuid", ids["task"]
            )
            await connection.execute(
                "DELETE FROM qc_h5_inspection_publication_work_item WHERE publication_id IN "
                "(SELECT id FROM qc_h5_inspection_publication WHERE task_id=$1::uuid)",
                ids["task"],
            )
            await connection.execute(
                "DELETE FROM qc_h5_inspection_publication WHERE task_id=$1::uuid", ids["task"]
            )
            await connection.execute(
                "DELETE FROM qc_inspection_work_item_rule WHERE work_item_id IN "
                "(SELECT id FROM qc_inspection_work_item WHERE task_id=$1::uuid)",
                ids["task"],
            )
            await connection.execute("DELETE FROM qc_task WHERE id=$1::uuid", ids["task"])
            await connection.execute("DELETE FROM qc_rule WHERE id=$1::uuid", rule_id)
