"""真实序列发布继承旧适用条件，并持久区分不适用与材料不足。"""

import json
import os
from uuid import uuid4

import pytest

from services.api.app.infrastructure.db.h5_inspection_publication_repository import (
    PostgresH5InspectionPublicationRepository,
)
from services.api.app.infrastructure.db.web_evidence_sequence_repository import (
    PostgresWebEvidenceSequenceRepository,
)
from services.api.tests.test_web_logical_target_postgres import target_database
from services.api.tests.test_web_rule_evidence_contract import _contract
from services.api.tests.test_web_run_policy_snapshot_postgres import (
    policy_database_schema as policy_database_schema,
)
from services.api.tests.web_sequence_pg_support import seed_failure_frames

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"), reason="必须显式提供隔离PG双DSN"
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case,reason,status",
    [
        ("channel", "WEB_RULE_CHANNEL_MISMATCH", "not_applicable"),
        ("url", "WEB_RULE_URL_MISMATCH", "not_applicable"),
        ("auth", "WEB_RULE_ACCOUNT_STATE_UNKNOWN", "needs_review"),
        ("aggregate", "WEB_RULE_AGGREGATE_SCOPE_REJECTED", "not_applicable"),
        ("samples", "WEB_SEQUENCE_SAMPLES_INSUFFICIENT", "needs_review"),
        ("duration", "WEB_SEQUENCE_DURATION_INSUFFICIENT", "needs_review"),
    ],
)
async def test_sequence_selection_persists_per_rule_exclusion_or_insufficiency(
    case, reason, status, monkeypatch, h5_auth_enabled=False
):
    from types import SimpleNamespace

    from services.api.app.core import config

    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(h5_auth=SimpleNamespace(enabled=h5_auth_enabled)))
    async with target_database() as (connection, pool, _other, ids):
        rule_id = str(uuid4())
        try:
            captures, _store = await seed_failure_frames(connection, pool, ids)
            contract = _contract()
            contract["minimum_duration_ms"] = 5000 if case == "duration" else 0
            contract["minimum_samples"] = 3 if case == "samples" else 2
            input_contract = {"target_clients": ["WEB"], "web_evidence": contract}
            judge_config = {"tool_refs": ["semantic_rule_judge_tool"]}
            if case == "auth":
                input_contract["auth_requirement"] = "authenticated"
            if case == "aggregate":
                judge_config["capability_config"] = {
                    "evidence_input_scope": "journey",
                    "evidence_scope_key": "journey_observation",
                }
            await connection.execute(
                "UPDATE qc_task SET channel='workbench' WHERE id=$1::uuid", ids["task"]
            )
            await connection.execute(
                "INSERT INTO qc_rule(id,ruleset_id,rule_code,name,category,executor_type,"
                "severity,status,check_point,input_contract,judge_config,channel,url_pattern) "
                "VALUES($1::uuid,$2::uuid,'t9_selection','T9适用选择','interaction','llm','medium','enabled',"
                "'检查加载',$3::jsonb,$4::jsonb,$5,$6)",
                rule_id,
                ids["ruleset"],
                json.dumps(input_contract),
                json.dumps(judge_config),
                "api" if case == "channel" else None,
                r"other\.test/detail" if case == "url" else None,
            )
            publication = await PostgresWebEvidenceSequenceRepository(pool=pool).register_failure(
                task_id=ids["task"],
                agent_run_id=ids["run"],
                session_id=ids["session"],
                source_capture_id=captures[1],
                evidence_capture_ids=(captures[1], captures[3]),
                page_role="menu",
                readiness_status="timeout",
            )
            publications = PostgresH5InspectionPublicationRepository(pool=pool)
            claim = await publications.claim(
                publication_id=publication["publication_id"],
                task_id=ids["task"],
                message_version=1,
                lease_owner="t9-selection",
                lease_seconds=60,
            )
            work_ids = await publications.process_web_sequence(lease=claim.lease)
            assert work_ids == ()
            assert (
                await connection.fetchval(
                    "SELECT status FROM qc_h5_inspection_publication WHERE id=$1::uuid",
                    publication["publication_id"],
                )
                == status
            )
            plan = await connection.fetchval(
                "SELECT execution_plan FROM qc_h5_inspection_publication_rule "
                "WHERE publication_id=$1::uuid AND rule_id=$2::uuid",
                publication["publication_id"],
                rule_id,
            )
            assert plan is not None
            plan = json.loads(plan) if isinstance(plan, str) else plan
            assert plan["selection"]["reason_code"] == reason
            assert plan["selection"]["status"] == (
                "evidence_insufficient" if status == "needs_review" else "not_applicable"
            )
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


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_web_unknown_account_does_not_borrow_h5_login_switch(enabled, monkeypatch):
    await test_sequence_selection_persists_per_rule_exclusion_or_insufficiency(
        "auth", "WEB_RULE_ACCOUNT_STATE_UNKNOWN", "needs_review", monkeypatch, h5_auth_enabled=enabled
    )
