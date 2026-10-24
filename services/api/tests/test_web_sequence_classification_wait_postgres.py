"""纯交互合同不能因默认 Worker 早于分类而永久丢失。"""

import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.agents.runtime.h5_inspection_publication_wakeup import (
    H5InspectionPublicationWakeupHandler,
)
from services.api.app.application.web_page_classification import WebPageAssessment
from services.api.app.application.web_page_inspection_notification import (
    WebPageInspectionNotifyingClassifier,
)
from services.api.app.application.web_page_type_assessment import WebPageTypeAssessmentRecorder
from services.api.app.infrastructure.db.h5_inspection_publication_repository import (
    PostgresH5InspectionPublicationRepository,
)
from services.api.app.infrastructure.db.web_capture_evidence_repository import (
    PostgresWebCaptureEvidenceRepository,
)
from services.api.tests.test_h5_inspection_publication_delivery import _wakeup
from services.api.tests.test_web_logical_target_postgres import target_database
from services.api.tests.test_web_rule_evidence_contract import _contract
from services.api.tests.test_web_run_policy_snapshot_postgres import (
    policy_database_schema as policy_database_schema,
)
from services.api.tests.web_frozen_assessment_support import frozen_assessment_options
from services.api.tests.web_sequence_consumer_pg_support import consume_sequence_with_actual_work
from services.api.tests.web_sequence_runtime_pg_support import (
    prepare_sequence_with_default_notification,
)

pytestmark = pytest.mark.skipif(not os.getenv("ALIOTH_TEST_DATABASE_URL"), reason="仅专用隔离PG")


@pytest.mark.asyncio
@pytest.mark.parametrize("timing", ["before_worker", "after_worker", "unknown"])
async def test_pure_interaction_waits_for_real_classification_and_replays_once(
    timing, monkeypatch, mutation=None
):
    from services.api.tests import test_web_logical_target_postgres as target_fixture

    body = target_fixture.policy_payload()
    started = datetime.now(UTC)
    body["budget"]["started_at"] = started.isoformat()
    body["budget"]["deadline"] = (
        started + timedelta(seconds=body["budget"]["max_duration_seconds"])
    ).isoformat()
    monkeypatch.setattr(target_fixture, "policy_payload", lambda: body)
    async with target_database() as (connection, pool, _other, ids):
        rule_id = str(uuid4())
        extra_id = str(uuid4())
        try:
            contract = _contract()
            contract.update(
                accepted_roles=["interaction_sequence"],
                accept_unclassified=False,
                minimum_duration_ms=0,
            )
            await connection.execute(
                "INSERT INTO qc_rule(id,ruleset_id,rule_code,name,category,executor_type,severity,status,check_point,input_contract,judge_config,manual_review_policy,applicable_page_types) VALUES($1::uuid,$2::uuid,'t9_pure','纯交互','interaction','llm','medium','enabled','检查实际动作',$3::jsonb,'{\"tool_refs\":[\"semantic_rule_judge_tool\"]}','{\"min_confidence\":0.8}','[\"content_detail\"]')",
                rule_id,
                ids["ruleset"],
                json.dumps({"target_clients": ["WEB"], "web_evidence": contract}),
            )

            async def add_rule(*, unclassified):
                extra_contract = dict(contract)
                if unclassified:
                    extra_contract.update(
                        accepted_roles=["interaction_sequence", "failure_sequence"],
                        accept_unclassified=True,
                    )
                await connection.execute(
                    "INSERT INTO qc_rule(id,ruleset_id,rule_code,name,category,executor_type,severity,status,check_point,input_contract,judge_config,manual_review_policy,applicable_page_types) "
                    "SELECT $2::uuid,ruleset_id,'t9_extra',name,category,executor_type,severity,status,check_point,$3::jsonb,judge_config,manual_review_policy,applicable_page_types FROM qc_rule WHERE id=$1::uuid",
                    rule_id,
                    extra_id,
                    json.dumps({"target_clients": ["WEB"], "web_evidence": extra_contract}),
                )

            if mutation == "mixed":
                await add_rule(unclassified=True)
            (
                result,
                published,
                store,
                source,
                notification,
            ) = await prepare_sequence_with_default_notification(
                connection, pool, ids, monkeypatch, interaction=True
            )
            publication_id = published[0]["publication_id"]
            repository = PostgresH5InspectionPublicationRepository(pool=pool, retry_delay_seconds=0)

            class StableMustNotRun:
                async def publish_for_capture(self, **kwargs):
                    raise AssertionError("序列不能走普通发布器")

            handler = H5InspectionPublicationWakeupHandler(
                repository=repository,
                publisher=StableMustNotRun(),
                lease_owner="t9-fix1-classification",
                lease_seconds=60,
            )

            async def deliver():
                version = await connection.fetchval(
                    "SELECT row_version FROM qc_h5_inspection_publication WHERE id=$1::uuid",
                    publication_id,
                )
                message = replace(
                    _wakeup(),
                    task_id=ids["task"],
                    aggregate_id=publication_id,
                    aggregate_version=version,
                )
                assert await handler.handle(message)
                assert await handler.handle(message), "旧版本重复投递只能幂等ACK"

            async def classify():
                options = await frozen_assessment_options(connection, ids)
                class ActualRecorderClassifier:
                    async def classify(self, capture):
                        assessment = WebPageAssessment(
                            page_type=None if timing == "unknown" else "content_detail",
                            confidence=0.95,
                            evidence=("自然正文",),
                            needs_review_reason="无法确认类型" if timing == "unknown" else None,
                        )
                        await WebPageTypeAssessmentRecorder(
                            repository=PostgresWebCaptureEvidenceRepository(pool=pool, **options),
                            **options,
                        ).persist(
                            task_id=ids["task"],
                            agent_run_id=ids["run"],
                            session_id=ids["session"],
                            capture=capture,
                            assessment=assessment,
                        )
                        return assessment

                ready = await source.read_ready(
                    capture_id=result.ready_capture_id, page_role="menu"
                )
                await WebPageInspectionNotifyingClassifier(
                    classifier=ActualRecorderClassifier(),
                    notification=notification,
                    task_id=ids["task"],
                    agent_run_id=ids["run"],
                    session_id=ids["session"],
                ).classify(ready)

            if timing == "before_worker":
                await classify()
            else:
                await deliver()
                pending = await connection.fetchrow(
                    "SELECT status,attempt_count,failure_code FROM qc_h5_inspection_publication WHERE id=$1::uuid",
                    publication_id,
                )
                assert (
                    pending["status"] == "retry_wait"
                    and pending["failure_code"] == "WEB_SEQUENCE_CLASSIFICATION_PENDING"
                ), dict(pending)
                assert pending["attempt_count"] == 0
                if mutation == "retry_budget":
                    for _ in range(5):
                        await deliver()
                        unchanged = await connection.fetchrow(
                            "SELECT status,attempt_count FROM qc_h5_inspection_publication WHERE id=$1::uuid",
                            publication_id,
                        )
                        assert dict(unchanged) == {"status": "retry_wait", "attempt_count": 0}
                frozen = [
                    dict(item)
                    for item in await connection.fetch(
                        "SELECT rule_id,contract_hash FROM qc_h5_inspection_publication_rule WHERE publication_id=$1::uuid ORDER BY rule_id",
                        publication_id,
                    )
                ]
                old_work_ids = await connection.fetch(
                    "SELECT id FROM qc_inspection_work_item WHERE task_id=$1::uuid", ids["task"]
                )
                if mutation == "mixed":
                    assert len(frozen) == 2 and len(old_work_ids) == 1
                if mutation == "modify":
                    await connection.execute(
                        "UPDATE qc_rule SET check_point='修改后的判定标准' WHERE id=$1::uuid",
                        rule_id,
                    )
                elif mutation == "disable":
                    await connection.execute(
                        "UPDATE qc_rule SET status='disabled' WHERE id=$1::uuid", rule_id
                    )
                elif mutation == "add":
                    await add_rule(unclassified=False)
                elif mutation == "delete":
                    with pytest.raises(asyncpg.ForeignKeyViolationError):
                        await connection.execute("DELETE FROM qc_rule WHERE id=$1::uuid", rule_id)
                elif mutation == "closeout":
                    from services.api.app.application.task_finalization import (
                        BeginExplorationDrainingCommand,
                    )
                    from services.api.app.infrastructure.db.task_finalization_repository import (
                        PostgresTaskFinalizationRepository,
                    )

                    await connection.execute(
                        "UPDATE qc_task SET finalization_agent_run_id=$2::uuid WHERE id=$1::uuid",
                        ids["task"],
                        ids["run"],
                    )
                    await PostgresTaskFinalizationRepository(pool=pool).begin_exploration_draining(
                        command=BeginExplorationDrainingCommand(
                            task_id=ids["task"], agent_run_id=ids["run"], expected_row_version=0
                        ),
                    )
                elif mutation == "run_done":
                    await connection.execute(
                        "UPDATE qc_agent_run SET status='done' WHERE id=$1::uuid", ids["run"]
                    )
                elif mutation == "deadline":
                    from services.api.app.infrastructure.db import (
                        web_sequence_classification_wait as wait_module,
                    )

                    class DeadlineClock:
                        @staticmethod
                        def now(tz):
                            return started + timedelta(
                                seconds=body["budget"]["max_duration_seconds"] + 1
                            )

                    monkeypatch.setattr(wait_module, "datetime", DeadlineClock)
                if mutation in {"deadline", "closeout", "run_done"}:
                    await deliver()
                    row = await connection.fetchrow(
                        "SELECT status,failure_code FROM qc_h5_inspection_publication WHERE id=$1::uuid",
                        publication_id,
                    )
                    assert row["status"] == "needs_review", dict(row)
                    plan = json.loads(
                        await connection.fetchval(
                            "SELECT execution_plan FROM qc_h5_inspection_publication_rule WHERE publication_id=$1::uuid AND rule_id=$2::uuid",
                            publication_id,
                            rule_id,
                        )
                    )
                    assert (
                        plan["selection"]["reason_code"] == "WEB_SEQUENCE_CLASSIFICATION_UNRESOLVED"
                    )
                    await classify()
                    await deliver()
                    assert (
                        await connection.fetchval(
                            "SELECT status FROM qc_h5_inspection_publication WHERE id=$1::uuid",
                            publication_id,
                        )
                        == "needs_review"
                    )
                    assert (
                        await connection.fetchval(
                            "SELECT count(*) FROM qc_inspection_work_item WHERE task_id=$1::uuid",
                            ids["task"],
                        )
                        == 0
                    )
                    return
                await classify()
            await deliver()
            if mutation in {"modify", "disable", "add"}:
                row = await connection.fetchrow(
                    "SELECT status,failure_code FROM qc_h5_inspection_publication WHERE id=$1::uuid",
                    publication_id,
                )
                assert dict(row) == {
                    "status": "needs_review",
                    "failure_code": "WEB_SEQUENCE_RULE_SNAPSHOT_DRIFT",
                }
                assert frozen == [
                    dict(item)
                    for item in await connection.fetch(
                        "SELECT rule_id,contract_hash FROM qc_h5_inspection_publication_rule WHERE publication_id=$1::uuid ORDER BY rule_id",
                        publication_id,
                    )
                ]
                assert (
                    await connection.fetchval(
                        "SELECT count(*) FROM qc_inspection_work_item WHERE task_id=$1::uuid",
                        ids["task"],
                    )
                    == 0
                )
                return
            row = await connection.fetchrow(
                "SELECT status,consumer_agent_run_id::text FROM qc_h5_inspection_publication WHERE id=$1::uuid",
                publication_id,
            )
            assert row["status"] == ("needs_review" if timing == "unknown" else "published")
            work_count = await connection.fetchval(
                "SELECT count(*) FROM qc_inspection_work_item WHERE task_id=$1::uuid", ids["task"]
            )
            assert work_count == (0 if timing == "unknown" else 2 if mutation == "mixed" else 1)
            if timing != "unknown":
                for _ in range(work_count):
                    consumed, packets = await consume_sequence_with_actual_work(
                        connection, pool, store, ids, row["consumer_agent_run_id"], monkeypatch
                    )
                    assert consumed.completed_count == 1 and len(packets) == 1
                assert (
                    await connection.fetchval(
                        "SELECT execution_status FROM qc_rule_execution WHERE task_id=$1::uuid AND rule_id=$2::uuid",
                        ids["task"],
                        rule_id,
                    )
                    == "pass"
                )
            if mutation == "mixed":
                for item in old_work_ids:
                    assert await connection.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM qc_inspection_work_item WHERE id=$1)",
                        item["id"],
                    )
                assert (
                    await connection.fetchval(
                        "SELECT count(*) FROM qc_evidence_input_set WHERE task_id=$1::uuid",
                        ids["task"],
                    )
                    == 2
                )
            await classify()
            await deliver()
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM qc_inspection_work_item WHERE task_id=$1::uuid",
                    ids["task"],
                )
                == work_count
            )
        finally:
            await connection.execute(
                "DELETE FROM qc_outbox_event WHERE task_id=$1::uuid", ids["task"]
            )
            await connection.execute(
                "DELETE FROM qc_h5_inspection_publication_work_item WHERE publication_id IN (SELECT id FROM qc_h5_inspection_publication WHERE task_id=$1::uuid)",
                ids["task"],
            )
            await connection.execute(
                "DELETE FROM qc_h5_inspection_publication WHERE task_id=$1::uuid", ids["task"]
            )
            await connection.execute(
                "DELETE FROM qc_inspection_work_item_rule WHERE work_item_id IN (SELECT id FROM qc_inspection_work_item WHERE task_id=$1::uuid)",
                ids["task"],
            )
            await connection.execute("DELETE FROM qc_task WHERE id=$1::uuid", ids["task"])
            await connection.execute("DELETE FROM qc_rule WHERE id=$1::uuid", rule_id)
            await connection.execute("DELETE FROM qc_rule WHERE id=$1::uuid", extra_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "modify",
        "disable",
        "add",
        "delete",
        "mixed",
        "deadline",
        "closeout",
        "run_done",
        "retry_budget",
    ],
)
async def test_wait_keeps_first_members_and_bounded_terminal_semantics(mutation, monkeypatch):
    await test_pure_interaction_waits_for_real_classification_and_replays_once(
        "after_worker", monkeypatch, mutation
    )
