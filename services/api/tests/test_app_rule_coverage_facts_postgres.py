"""真实 PostgreSQL 验证冻结规则需求按当前 Run/Session/Epoch 刷新。"""

import json
import os
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from services.api.app.application.task_rule_inventory import build_task_rule_inventory
from services.api.app.infrastructure.db.exploration_scheduler_facts_repository import (
    PostgresExplorationSchedulerFactsReader,
)
from services.api.tests.test_exploration_scheduler_facts_postgres_integration import (
    _database as _base_database,
)
from services.api.tests.test_exploration_scheduler_facts_postgres_integration import (
    _upgrade_isolated_postgres,  # noqa: F401 - 明确迁移隔离测试数据库
)

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"),
    reason="需要显式隔离 PostgreSQL",
)


@asynccontextmanager
async def _database(**kwargs):
    """先清理截图引用，避免旧通用夹具按任务级联删除时撞 RESTRICT。"""
    async with _base_database(**kwargs) as context:
        connection, _, ids = context
        try:
            yield context
        finally:
            await connection.execute(
                "DELETE FROM qc_app_capture WHERE capture_id IN "
                "(SELECT id FROM qc_capture WHERE task_id=$1::uuid)",
                ids["task"],
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "no_execution",
        "pass",
        "bootstrap",
        "old_epoch",
        "old_run",
        "prepared",
        "latest_failed",
        "pending_judge",
        "latest_visual_changed",
        "bootstrap_then_pass",
        "waiting_evidence",
        "occluded",
    ],
)
async def test_inventory_gap_refreshes_without_borrowing_old_or_placeholder_execution(mode):
    async with _database(surface_kind="android", target_platform="app") as (connection, pool, ids):
        channel = await connection.fetchval(
            "SELECT channel FROM qc_task WHERE id=$1::uuid", ids["task"]
        )
        rule = {
            "rule_id": ids["rule"],
            "rule_code": "FACT-GAP-1",
            "name": "支付入口",
            "category": "text",
            "executor_type": "rule_engine",
            "check_point": "确认支付入口",
            "applicable_page_types": ["payment"],
            "input_contract": {"target_clients": ["APP"]},
            "judge_config": {
                "capability_config": {
                    "requires_journey": mode
                    not in {"prepared", "latest_failed", "latest_visual_changed", "occluded"}
                }
            },
            "channel": None,
        }
        config = {
            "workflow_version": "v2",
            "execution_mode": "full_inspection",
            "inspection_scope": {"page_types": []},
            "ruleset_rule_total": 1,
        }
        config["rule_inventory"] = build_task_rule_inventory(
            ruleset_id=ids["ruleset"],
            ruleset_version=1,
            ruleset_snapshot_hash=None,
            target_platform="app",
            channel=channel,
            task_config=config,
            rules=[rule],
        )
        await connection.execute(
            "UPDATE qc_task SET task_config=task_config || $2::jsonb WHERE id=$1::uuid",
            ids["task"],
            json.dumps(config),
        )
        reader = PostgresExplorationSchedulerFactsReader(pool=pool)
        arguments = {
            "task_id": ids["task"],
            "agent_run_id": ids["run"],
            "session_id": ids["session"],
        }
        first = (await reader.read_app_facts(**arguments))["app_rule_inventory_coverage"]
        assert first["pending_rule_count"] == 1
        assert first["pending_rules"][0]["target_page_types"] == ["payment"]
        # 清单创建后修改可变规则不能缩小探索需求。
        await connection.execute(
            "UPDATE qc_rule SET applicable_page_types='[\"home\"]'::jsonb, status='disabled' "
            "WHERE id=$1::uuid",
            ids["rule"],
        )
        if mode in {
            "prepared",
            "latest_failed",
            "pending_judge",
            "latest_visual_changed",
            "waiting_evidence",
            "occluded",
        }:
            artifact = str(uuid4())
            await connection.execute(
                """INSERT INTO qc_evidence_artifact(id,task_id,page_capture_id,artifact_type,
                     source,storage_path,content_type,checksum,size_bytes)
                   VALUES($1::uuid,$2::uuid,$3::uuid,'screenshot','baseline','isolated-test.png','image/png',$4,100)""",
                artifact,
                ids["task"],
                ids["capture"],
                "sha256:" + "a" * 64,
            )
            await connection.execute(
                """INSERT INTO qc_app_capture(capture_id,screenshot_artifact_id,package_name,
                     activity,width,height,image_signature)
                   VALUES($1::uuid,$2::uuid,'com.example','.Payment',100,200,$3)""",
                ids["capture"],
                artifact,
                "sha256:" + "a" * 64,
            )
            await connection.execute(
                """INSERT INTO qc_app_visual_observation(
                     observation_id,task_id,session_id,capture_id,
                     screenshot_artifact_id,contract_version,provider_id,model_id,result_hash,
                     visual_page_type,visual_summary,page_state)
                   VALUES($1::uuid,$2::uuid,$3,$4::uuid,$5::uuid,'app-visual-analysis.v2',
                     'isolated-test','isolated-test',$6,'payment','支付页面','stable')""",
                str(uuid4()),
                ids["task"],
                ids["session"],
                ids["capture"],
                artifact,
                "b" * 64,
            )
            if mode == "latest_visual_changed":
                prepared = (await reader.read_app_facts(**arguments))["app_rule_inventory_coverage"]
                assert prepared["pending_rule_count"] == 0
                # 同一 Capture 的视觉行当前受唯一约束，重分析会更新原行，旧识别不得遗留。
                await connection.execute(
                    """UPDATE qc_app_visual_observation SET visual_page_type='home',
                         visual_summary='最新识别为主页',result_hash=$2,created_at=now()
                       WHERE capture_id=$1::uuid""",
                    ids["capture"],
                    "c" * 64,
                )
            if mode == "occluded":
                from services.api.app.application.app_inspection_publisher import (
                    PostgresCapturePresentationReader,
                )

                await connection.execute(
                    "UPDATE qc_app_visual_observation SET page_state='popup', "
                    "surface_facts='{}'::jsonb WHERE capture_id=$1::uuid",
                    ids["capture"],
                )
                presentation = await PostgresCapturePresentationReader(pool=pool).get_presentation(
                    task_id=ids["task"],
                    capture_id=ids["capture"],
                )
                assert presentation.occluding_layer_present
            if mode == "latest_failed":
                await connection.execute(
                    """INSERT INTO qc_evidence_manifest(
                         manifest_id,manifest_version,task_id,session_id,capture_id,protocol_version,
                         manifest_hash,payload_hash,quality_status,quality_reason,
                         manifest_payload,created_at)
                       VALUES($1,2,$2::uuid,$3,$4::uuid,'evidence-manifest.v1','new-hash','new-payload',
                         'fail','isolated-test failure','{}'::jsonb,now())""",
                    "failed:" + str(uuid4()),
                    ids["task"],
                    ids["session"],
                    ids["capture"],
                )
            if mode in {"pending_judge", "waiting_evidence"}:
                await connection.execute(
                    """INSERT INTO qc_inspection_work_item(
                         id,task_id,page_capture_id,producer_agent_run_id,
                         consumer_agent_run_id,session_owner_agent_run_id,ruleset_id,ruleset_version,
                         protocol_version,batch_key,strategy,category,session_dependency,input_hash,
                         status,priority,max_attempts,next_attempt_at,control_mode)
                       VALUES($1::uuid,$2::uuid,$3::uuid,$4::uuid,$4::uuid,$4::uuid,$5::uuid,1,
                         'inspection-work-item.v1','pending-test','rule_engine','text','active_session',
                         'pending-test',$6,100,3,now(),'enforced')""",
                    str(uuid4()),
                    ids["task"],
                    ids["capture"],
                    ids["run"],
                    ids["ruleset"],
                    "waiting_evidence" if mode == "waiting_evidence" else "pending",
                )
        if mode in {"pass", "bootstrap", "old_epoch", "old_run", "bootstrap_then_pass"}:
            capture_id = ids["capture"]
            if mode == "old_epoch":
                await connection.execute(
                    "UPDATE qc_capture SET metadata=metadata || "
                    "'{\"finalization_epoch\": 1}'::jsonb "
                    "WHERE id=$1::uuid",
                    capture_id,
                )
            if mode == "old_run":
                old_run = str(uuid4())
                await connection.execute(
                    "INSERT INTO qc_agent_run(id,task_id,agent_type,agent_name) "
                    "VALUES($1::uuid,$2::uuid,'explorer','旧运行')",
                    old_run,
                    ids["task"],
                )
                await connection.execute(
                    "UPDATE qc_capture SET agent_run_id=$2::uuid WHERE id=$1::uuid",
                    capture_id,
                    old_run,
                )
                # 本测试只核验规则缺口，旧索引应先移除，避免由独立索引归属门禁提前拒绝。
                await connection.execute(
                    "DELETE FROM qc_app_evidence_index WHERE index_id=$1", ids["index"]
                )
            await connection.execute(
                """INSERT INTO qc_rule_execution(task_id,rule_id,page_capture_id,agent_run_id,
                     execution_status,judge_strategy,judge_input)
                   VALUES($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,'rule_engine',$6::jsonb)""",
                ids["task"],
                ids["rule"],
                capture_id,
                ids["run"],
                "skipped" if mode in {"bootstrap", "bootstrap_then_pass"} else "pass",
                json.dumps(
                    {"bootstrap": True} if mode in {"bootstrap", "bootstrap_then_pass"} else {}
                ),
            )
            if mode == "bootstrap_then_pass":
                await connection.execute(
                    """INSERT INTO qc_rule_execution(task_id,rule_id,page_capture_id,agent_run_id,
                         execution_status,judge_strategy,judge_input)
                       VALUES($1::uuid,$2::uuid,$3::uuid,$4::uuid,'pass','rule_engine','{}'::jsonb)""",
                    ids["task"],
                    ids["rule"],
                    ids["bootstrap_capture"],
                    ids["run"],
                )
        if mode == "old_epoch":
            await connection.execute(
                "DELETE FROM qc_app_evidence_index WHERE index_id=$1", ids["index"]
            )
        refreshed = (await reader.read_app_facts(**arguments))["app_rule_inventory_coverage"]
        assert refreshed["pending_rule_count"] == (
            0 if mode in {"pass", "prepared", "bootstrap_then_pass"} else 1
        )
        assert refreshed["pending_inspection_work_count"] == (1 if mode == "pending_judge" else 0)
        if mode == "prepared":
            assert refreshed["deferred_rule_ids"] == [ids["rule"]]
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM qc_rule_execution WHERE task_id=$1::uuid", ids["task"]
                )
                == 0
            )
        elif mode not in {"pass", "bootstrap_then_pass"}:
            assert refreshed["pending_rules"][0]["target_page_types"] == ["payment"]
