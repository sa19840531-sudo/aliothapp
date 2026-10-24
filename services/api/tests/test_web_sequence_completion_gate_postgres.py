"""同锚点稳定与交互序列发布使用独立批次，门禁不把合法用途误作孤立工作。"""

import json
from uuid import uuid4

import pytest

from services.api.tests import test_web_v2_formal_report_e2e as formal
from services.api.tests.test_web_logical_target_postgres import target_database
from services.api.tests.test_web_rule_evidence_contract import _contract
from services.api.tests.web_sequence_consumer_pg_support import consume_sequence_with_actual_work
from services.api.tests.web_sequence_runtime_pg_support import (
    prepare_sequence_with_default_notification,
)
from services.api.tests.web_sequence_stable_pg_support import publish_stable_anchor


@pytest.mark.asyncio
async def test_same_anchor_stable_and_sequence_publications_keep_independent_inventory(monkeypatch):
    async with target_database() as (connection, pool, _, ids):
        rule_id = str(uuid4())
        try:
            contract = _contract()
            contract.update(
                accepted_roles=["stable_page", "interaction_sequence", "failure_sequence"],
                minimum_duration_ms=0,
            )
            await connection.execute(
                "INSERT INTO qc_rule(id,ruleset_id,rule_code,name,category,executor_type,"
                "severity,status,"
                "check_point,input_contract,judge_config,manual_review_policy,"
                "applicable_page_types) "
                "VALUES($1::uuid,$2::uuid,'t10_gate_dual','门禁双用途','interaction','llm',"
                "'medium','enabled','仅按当前真实材料',"
                '$3::jsonb,\'{"tool_refs":["semantic_rule_judge_tool"]}\',\'{"min_confidence":0.8}\',\'["content_detail"]\')',
                rule_id,
                ids["ruleset"],
                json.dumps({"target_clients": ["WEB"], "web_evidence": contract}),
            )
            (
                result,
                published,
                store,
                source,
                notification,
            ) = await prepare_sequence_with_default_notification(
                connection, pool, ids, monkeypatch, interaction=True
            )
            publications = formal.PostgresH5InspectionPublicationRepository(pool=pool)
            _, stable_lease = await publish_stable_anchor(
                pool, ids, source, notification, result.ready_capture_id, publications, monkeypatch
            )
            stable, _ = await consume_sequence_with_actual_work(
                connection, pool, store, ids, stable_lease.consumer_agent_run_id, monkeypatch
            )
            assert stable.completed_count == 1
            version = await connection.fetchval(
                "SELECT row_version FROM qc_h5_inspection_publication WHERE id=$1::uuid",
                published[0]["publication_id"],
            )
            claim = await publications.claim(
                publication_id=published[0]["publication_id"],
                task_id=ids["task"],
                message_version=version,
                lease_owner="t10-dual",
                lease_seconds=60,
            )
            assert claim.lease is not None
            assert len(await publications.process_web_sequence(lease=claim.lease)) == 1
            sequence, _ = await consume_sequence_with_actual_work(
                connection, pool, store, ids, claim.lease.consumer_agent_run_id, monkeypatch
            )
            assert sequence.completed_count == 1
            await connection.execute(
                "UPDATE qc_task SET finalization_agent_run_id=$2::uuid WHERE id=$1::uuid",
                ids["task"],
                ids["run"],
            )
            await connection.execute(
                "UPDATE qc_agent_run SET status='done',stop_reason='completed',"
                "finished_at=now() WHERE id=$1::uuid",
                ids["run"],
            )
            rows = await connection.fetch(
                "SELECT page_capture_id::text,web_evidence_sequence_id::text,"
                "published_work_item_count "
                "FROM qc_h5_inspection_publication WHERE task_id=$1::uuid",
                ids["task"],
            )
            assert len(rows) == 2 and {row["page_capture_id"] for row in rows} == {
                result.ready_capture_id
            }
            assert sum(row["web_evidence_sequence_id"] is not None for row in rows) == 1
            assert all(row["published_work_item_count"] == 1 for row in rows)
            gate_sql = formal._GATE_FACTS_SQL.replace(
                "AS nonterminal_enforced_work_item_count,",
                "AS nonterminal_enforced_work_item_count, row_to_json(h5_publication_gate) "
                "AS publication_inventory,",
            )
            gate = await connection.fetchrow(gate_sql, ids["task"], "completed_with_gaps")
            assert json.loads(gate["publication_inventory"]) == {
                "active_or_inconsistent": 0,
                "review_required": 0,
                "execution_inflight": 0,
            }
            transaction = connection.transaction()
            await transaction.start()
            try:
                await connection.execute(
                    "UPDATE qc_h5_inspection_publication SET status='pending',finished_at=NULL "
                    "WHERE task_id=$1::uuid",
                    ids["task"],
                )
                inflight = await connection.fetchrow(gate_sql, ids["task"], "completed_with_gaps")
                assert json.loads(inflight["publication_inventory"])["execution_inflight"] > 0
            finally:
                await transaction.rollback()
            from services.api.app.infrastructure.db.web_sequence_completion_gate_sql import (
                WEB_SEQUENCE_PUBLICATION_INVENTORY_MATCH_SQL,
            )

            assert await connection.fetchval(
                "SELECT (" + WEB_SEQUENCE_PUBLICATION_INVENTORY_MATCH_SQL + ") "
                "FROM qc_h5_inspection_publication publication JOIN qc_task task ON "
                "task.id=publication.task_id "
                "WHERE publication.id=$1::uuid",
                published[0]["publication_id"],
            )
            for platform in ("web", "h5", "app"):
                transaction = connection.transaction()
                await transaction.start()
                try:
                    await connection.execute(
                        "DELETE FROM qc_h5_evidence_inventory_item WHERE task_id=$1::uuid",
                        ids["task"],
                    )
                    if platform == "app":
                        # 仅构造旧 App 门禁的合法目标字段，不绕过其原始形状约束。
                        await connection.execute(
                            "UPDATE qc_task SET target_platform='app',target_url=NULL,"
                            'task_config=task_config || '
                            '\'{"app":{"platform_build_ref":"t10-gate-fixture"}}\'::jsonb '
                            "WHERE id=$1::uuid",
                            ids["task"],
                        )
                    else:
                        await connection.execute(
                            "UPDATE qc_task SET target_platform=$2 WHERE id=$1::uuid",
                            ids["task"],
                            platform,
                        )
                    denied = await connection.fetchrow(gate_sql, ids["task"], "completed_with_gaps")
                    assert (
                        json.loads(denied["publication_inventory"])["active_or_inconsistent"] >= 1
                    ), platform
                finally:
                    await transaction.rollback()
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
