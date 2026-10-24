"""真实 PostgreSQL 验证提交全清单、冻结候选、登录缺口与历史证明边界。"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.agents.tools.rule_tools import _candidate_rule_to_dict
from services.api.app.application.inspection_work_items import build_inspection_rule_contract_hash
from services.api.app.application.rule_execution import (
    RecordRuleExecutionCommand,
    RuleExecutionService,
)
from services.api.app.application.rule_judge import RuleJudgeRequest, _validate_locked_rule_contract
from services.api.app.application.task_rule_inventory import parse_task_rule_inventory
from services.api.app.application.task_submission import CreateTaskSubmissionCommand
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.domain.task import AppTaskConfig, TaskTarget, TaskTargetPlatform
from services.api.app.domain.task.completion import (
    CompletionGateFacts,
    build_completion_gate_snapshot,
)
from services.api.app.infrastructure.db.aggregate_inspection_publication_repository import (
    PostgresAggregateInspectionPublicationRepository,
)
from services.api.app.infrastructure.db.task_finalization_repository import _insert_gate_snapshot
from services.api.app.infrastructure.db.task_repository import PostgresTaskRepository
from services.api.app.infrastructure.db.task_rule_inventory_repository import (
    PostgresTaskRuleInventoryRepository,
)
from services.api.app.infrastructure.db.test_database_guard import (
    require_isolated_test_database_url,
)
from services.api.tests.test_app_evidence_index_postgres_integration import _SingleConnectionPool


@asynccontextmanager
async def _database(*, include_aggregate=False, transactional=False):
    url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not url:
        pytest.skip("需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL")
    url = require_isolated_test_database_url(url, os.getenv("ALIOTH_DATABASE_URL"))
    ids = {key: str(uuid4()) for key in ("tenant", "user", "ruleset", "build")}
    connection = await asyncpg.connect(url)
    pool = await asyncpg.create_pool(url)
    transaction = connection.transaction(isolation="repeatable_read") if transactional else None
    if transaction is not None:
        await transaction.start()
    active_pool = _SingleConnectionPool(connection) if transactional else pool
    try:
        await connection.execute(
            "INSERT INTO qc_tenant(id,tenant_key,display_name) VALUES($1::uuid,$2,$2)",
            ids["tenant"],
            f"inventory-{ids['tenant']}",
        )
        await connection.execute(
            """INSERT INTO qc_user(id,username,full_name,password_hash,role,tenant_id)
               VALUES($1::uuid,$2,'Inventory','not-a-login-secret','平台管理员',$3::uuid)""",
            ids["user"],
            f"inventory-{ids['user']}",
            ids["tenant"],
        )
        await connection.execute(
            """INSERT INTO qc_ruleset(id,ruleset_code,name,version,status,snapshot_hash,source_type)
               VALUES($1::uuid,$2,'Inventory',1,'enabled','snapshot-1','manual')""",
            ids["ruleset"],
            f"inventory-{ids['ruleset']}",
        )
        await connection.execute(
            """INSERT INTO qc_platform_build(id,original_filename,storage_path,sha256,size_bytes,
                      status,verified_at,created_by,tenant_id)
               VALUES($1::uuid,'inventory.apk','tests/inventory.apk',$2,1,'verified',now(),$3::uuid,$4::uuid)""",
            ids["build"],
            uuid4().hex + uuid4().hex,
            ids["user"],
            ids["tenant"],
        )
        rules = {}
        rule_declarations = [
            ("eligible", ["home"], {}),
            ("platform", ["home"], {"target_clients": ["WEB"]}),
            ("scope", ["video_detail"], {}),
            ("auth", ["home"], {"auth_requirement": "authenticated"}),
        ]
        if include_aggregate:
            rule_declarations.append(("aggregate", ["home"], {}))
        for name, pages, contract in rule_declarations:
            rules[name] = str(uuid4())
            await connection.execute(
                """INSERT INTO qc_rule(id,ruleset_id,rule_code,name,category,executor_type,severity,
                          status,check_point,applicable_page_types,input_contract,judge_config,
                          manual_review_policy)
                   VALUES($1::uuid,$2::uuid,$3,$3,'visual','hybrid','high','enabled','检查内容',
                          $4::jsonb,$5::jsonb,$6::jsonb,'{"min_confidence":0.8}'::jsonb)""",
                rules[name],
                ids["ruleset"],
                f"inventory_{name}",
                json.dumps(pages),
                json.dumps(contract),
                json.dumps(
                    {
                        "capability_config": {
                            "evidence_input_scope": "task",
                            "evidence_scope_key": "toast_style",
                        }
                    }
                    if name == "aggregate"
                    else {}
                ),
            )
        scope = AccessScope(
            actor_user_id=ids["user"],
            tenant_id=ids["tenant"],
            permissions=frozenset({"taskObjects:read:any"}),
        )
        config = {
            "workflow_version": "v2",
            "execution_mode": "full_inspection",
            "inspection_scope": {"page_types": ["home"]},
            "app": {"platform_build_ref": f"app-build:{ids['build']}"},
            "rule_inventory": {"forged": True},
        }
        command = CreateTaskSubmissionCommand(
            access_scope=scope,
            target=TaskTarget(
                platform=TaskTargetPlatform.APP,
                app=AppTaskConfig(platform_build_ref=f"app-build:{ids['build']}"),
            ),
            ruleset_code=f"inventory-{ids['ruleset']}",
            ruleset_version=1,
            channel="workbench",
            priority=100,
            task_config=config,
            page_sampling_policy={},
            loop_guard={},
            submission_kind="v2_full_task",
            idempotency_key=f"inventory-{uuid4()}",
            request_hash="a" * 64,
            request_payload={},
        )
        persisted = await PostgresTaskRepository(pool=active_pool).create_submission(command)
        ids.update(task=persisted.task.task_id, run=persisted.run_id)
        yield connection, active_pool, ids, rules, scope, persisted, command
    finally:
        if transaction is not None:
            await transaction.rollback()
        elif "task" in ids:
            await connection.execute(
                "DELETE FROM qc_outbox_event WHERE task_id=$1::uuid", ids["task"]
            )
            await connection.execute("DELETE FROM qc_task WHERE id=$1::uuid", ids["task"])
        if transaction is None:
            await connection.execute(
                "DELETE FROM qc_rule WHERE ruleset_id=$1::uuid", ids["ruleset"]
            )
            await connection.execute("DELETE FROM qc_ruleset WHERE id=$1::uuid", ids["ruleset"])
            await connection.execute(
                "DELETE FROM qc_platform_build WHERE id=$1::uuid", ids["build"]
            )
            await connection.execute("DELETE FROM qc_user WHERE id=$1::uuid", ids["user"])
            await connection.execute("DELETE FROM qc_tenant WHERE id=$1::uuid", ids["tenant"])
        await pool.close()
        await connection.close()


async def test_submission_freezes_every_rule_and_candidates_ignore_later_live_edits() -> None:
    async with _database() as (connection, pool, ids, rules, scope, persisted, command):
        inventory = parse_task_rule_inventory(persisted.task.task_config)
        assert inventory is not None and inventory["rule_count"] == 4
        assert persisted.task.page_sampling_policy["rule_target_page_types"] == ["home"]
        assert {rule["rule_id"] for rule in inventory["rules"]} == set(rules.values())
        assert {rule["reason_code"] for rule in inventory["rules"]} == {
            "eligible",
            "platform_not_supported",
            "inspection_scope_excluded",
        }
        run_config = json.loads(
            await connection.fetchval(
                "SELECT input_context FROM qc_agent_run WHERE id=$1::uuid", ids["run"]
            )
        )["task_config"]
        assert run_config["rule_inventory"] == inventory
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_rule_execution WHERE task_id=$1::uuid", ids["task"]
            )
            == 0
        )
        await connection.execute(
            """UPDATE qc_rule SET name='后改规则',status='disabled',
                  applicable_page_types='["video_detail"]'::jsonb,updated_at=now()
               WHERE id=$1::uuid""",
            rules["eligible"],
        )
        repository = PostgresTaskRepository(pool=pool)
        candidates = await repository.list_candidate_rules(
            task_id=ids["task"], page_type="home", auth_gate="authenticated", access_scope=scope
        )
        assert [(rule.rule_id, rule.name) for rule in candidates] == [
            (rules["eligible"], "inventory_eligible")
        ]
        definition = await repository.get_rule_judge_definition(
            task_id=ids["task"], rule_code="inventory_eligible"
        )
        assert definition is not None and definition.rule_name == "inventory_eligible"
        assert definition.check_point == "检查内容"
        _validate_locked_rule_contract(
            definition,
            RuleJudgeRequest(
                task_id=ids["task"],
                rule_code="inventory_eligible",
                page_capture_id="capture-1",
                evidence_artifact_ids=(),
                judge_input={},
                expected_contract_hash=(
                    build_inspection_rule_contract_hash(_candidate_rule_to_dict(candidates[0]))
                ),
            ),
        )
        assert (
            await repository.list_candidate_rules(
                task_id=ids["task"], page_type="video_detail", auth_gate=None, access_scope=scope
            )
            == []
        )
        replayed = await repository.create_submission(command)
        assert replayed.replayed and replayed.task.task_config["rule_inventory"] == inventory


async def test_aggregate_publication_consumes_frozen_rules_after_live_disable() -> None:
    async with _database(include_aggregate=True) as (
        connection,
        pool,
        ids,
        rules,
        _scope,
        _persisted,
        _command,
    ):
        await connection.execute(
            """UPDATE qc_task SET status='running',finalization_state='session_draining',
                   finalization_agent_run_id=$2::uuid,exploration_draining_at=now(),
                   session_draining_at=now() WHERE id=$1::uuid""",
            ids["task"],
            ids["run"],
        )
        await connection.execute(
            """UPDATE qc_rule SET status='disabled',judge_config='{}',updated_at=now()
               WHERE id=$1::uuid""",
            rules["aggregate"],
        )
        await connection.execute(
            "UPDATE qc_ruleset SET snapshot_hash=NULL,updated_at=now() WHERE id=$1::uuid",
            ids["ruleset"],
        )
        snapshot = await PostgresAggregateInspectionPublicationRepository(pool=pool).load_snapshot(
            task_id=ids["task"], source_agent_run_id=ids["run"], finalization_epoch=0
        )
        assert len(snapshot.rules) == 1
        assert snapshot.rules[0].rule_id == rules["aggregate"]
        assert snapshot.rules[0].evidence.scope_key == "toast_style"
        assert snapshot.ruleset_snapshot_hash == "snapshot-1"


async def test_frozen_rule_result_writes_after_live_disable_and_code_change() -> None:
    async with _database() as (connection, pool, ids, rules, _scope, _persisted, _command):
        capture, artifact, packet = str(uuid4()), str(uuid4()), str(uuid4())
        await connection.execute(
            """INSERT INTO qc_capture(id,task_id,agent_run_id,surface_kind,capture_role,metadata)
               VALUES($1::uuid,$2::uuid,$3::uuid,'android','observation',
                      '{"finalization_epoch":0}')""",
            capture,
            ids["task"],
            ids["run"],
        )
        await connection.execute(
            """INSERT INTO qc_evidence_artifact(
                   id,task_id,page_capture_id,artifact_type,source,storage_path,size_bytes,checksum)
               VALUES($1::uuid,$2::uuid,$3::uuid,'screenshot','baseline','tests/frozen.png',1,$4)""",
            artifact,
            ids["task"],
            capture,
            "sha256:" + "0" * 64,
        )
        await connection.execute(
            """INSERT INTO qc_observation_packet(
                   id,task_id,page_capture_id,rule_id,evidence_artifact_ids,
                   quality_gate_status,created_by_agent_run_id)
               VALUES($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5::jsonb,'pass',$6::uuid)""",
            packet,
            ids["task"],
            capture,
            rules["eligible"],
            json.dumps([artifact]),
            ids["run"],
        )
        await connection.execute(
            """UPDATE qc_rule SET status='disabled',rule_code='inventory_changed',
                   executor_type='llm',severity='low',manual_review_policy='{"min_confidence":1}',
                   updated_at=now() WHERE id=$1::uuid""",
            rules["eligible"],
        )
        recorded = await RuleExecutionService(
            repository=PostgresTaskRepository(pool=pool)
        ).record_rule_execution(
            RecordRuleExecutionCommand(
                task_id=ids["task"],
                agent_run_id=ids["run"],
                rule_code="inventory_eligible",
                execution_status="pass",
                page_capture_id=capture,
                observation_packet_id=packet,
                confidence=0.99,
                evidence_artifact_ids=[artifact],
                result_summary="通过",
                judge_input={"observation_summary": "页面内容符合规则"},
                judge_output={
                    "schema_version": "1.0",
                    "conclusion": "pass",
                    "reasoning_summary": "可见内容符合规则要求",
                    "tool_results": [{"status": "pass", "summary": "检查通过"}],
                },
            )
        )
        assert recorded is not None and recorded.rule_id == rules["eligible"]
        assert recorded.rule_code == "inventory_eligible"
        stored = await connection.fetchrow(
            """SELECT execution_status,judge_strategy,severity
               FROM qc_rule_execution WHERE id=$1::uuid""",
            recorded.execution_id,
        )
        assert dict(stored) == {
            "execution_status": "pass",
            "judge_strategy": "hybrid",
            "severity": "high",
        }


async def test_snapshot_reads_current_run_only_and_history_requires_unchanged_full_inventory() -> (
    None
):
    async with _database() as (connection, pool, ids, rules, scope, _persisted, _command):
        old_run, current_capture = str(uuid4()), str(uuid4())
        await connection.execute(
            """INSERT INTO qc_agent_run(id,task_id,agent_type,agent_name,status)
               VALUES($1::uuid,$2::uuid,'main','old main','done')""",
            old_run,
            ids["task"],
        )
        await connection.execute(
            "UPDATE qc_task SET finalization_agent_run_id=$2::uuid WHERE id=$1::uuid",
            ids["task"],
            ids["run"],
        )
        await connection.execute(
            """INSERT INTO qc_capture(id,task_id,agent_run_id,surface_kind,capture_role,metadata)
               VALUES($1::uuid,$2::uuid,$3::uuid,'android','observation',
                      '{"session_auth":"unknown","finalization_epoch":0}')""",
            current_capture,
            ids["task"],
            ids["run"],
        )
        await connection.execute(
            """INSERT INTO qc_rule_execution(
                   task_id,rule_id,agent_run_id,execution_status,judge_strategy)
               VALUES($1::uuid,$2::uuid,$3::uuid,'pass','hybrid')""",
            ids["task"],
            rules["eligible"],
            old_run,
        )
        reader = PostgresTaskRuleInventoryRepository(pool=pool)
        snapshot = await reader.load_snapshot(task_id=ids["task"], access_scope=scope)
        assert snapshot["inventory_status"] == "frozen"
        assert snapshot["auth_gate"] == "authenticated"
        assert snapshot["executions"] == []
        assert (
            await reader.load_snapshot(
                task_id=ids["task"], agent_run_id=old_run, access_scope=scope
            )
        )["unavailable_reason"] == "run_owner_mismatch"
        await connection.execute(
            "UPDATE qc_task SET task_config=task_config-'rule_inventory' WHERE id=$1::uuid",
            ids["task"],
        )
        historical = await reader.load_snapshot(task_id=ids["task"], access_scope=scope)
        assert historical["inventory_status"] == "legacy_verified"
        assert historical["inventory"]["source"] == "legacy_verified"
        await connection.execute(
            "UPDATE qc_rule SET updated_at=now(),name='已经变动' WHERE id=$1::uuid",
            rules["eligible"],
        )
        changed = await reader.load_snapshot(task_id=ids["task"], access_scope=scope)
        assert changed["inventory"] is None
        assert changed["unavailable_reason"] == "historical_rules_changed"


async def test_inventory_submission_rolls_back_with_outbox_and_respects_access_scope(
    monkeypatch,
) -> None:
    async with _database() as (connection, pool, ids, _rules, _scope, _persisted, command):

        async def reject_outbox(*args, **kwargs):
            raise RuntimeError("模拟事务提交前的 Outbox 写入失败")

        monkeypatch.setattr(
            "services.api.app.infrastructure.db.task_repository.insert_outbox_event", reject_outbox
        )
        with pytest.raises(RuntimeError, match="Outbox"):
            await PostgresTaskRepository(pool=pool).create_submission(
                replace(command, idempotency_key=f"rollback-{uuid4()}")
            )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_task WHERE submitter_id=$1::uuid", ids["user"]
            )
            == 1
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_agent_run WHERE task_id=$1::uuid", ids["task"]
            )
            == 1
        )
        denied = AccessScope(
            actor_user_id=str(uuid4()), tenant_id=str(uuid4()), permissions=frozenset()
        )
        assert (
            await PostgresTaskRuleInventoryRepository(pool=pool).load_snapshot(
                task_id=ids["task"], access_scope=denied
            )
            is None
        )
        assert (
            await PostgresTaskRepository(pool=pool).list_candidate_rules(
                task_id=ids["task"], page_type="home", auth_gate=None, access_scope=denied
            )
            == []
        )


async def test_snapshot_keeps_independent_judge_lineage_but_rejects_old_epoch_results() -> None:
    async with _database() as (connection, pool, ids, rules, scope, _persisted, _command):
        capture, judge = str(uuid4()), str(uuid4())
        await connection.execute(
            "UPDATE qc_task SET finalization_agent_run_id=$2::uuid WHERE id=$1::uuid",
            ids["task"],
            ids["run"],
        )
        await connection.execute(
            """INSERT INTO qc_agent_run(id,task_id,agent_type,agent_name,status)
               VALUES($1::uuid,$2::uuid,'rule_judge','independent judge','done')""",
            judge,
            ids["task"],
        )
        await connection.execute(
            """INSERT INTO qc_capture(id,task_id,agent_run_id,surface_kind,capture_role,metadata)
               VALUES($1::uuid,$2::uuid,$3::uuid,'android','observation',
                      '{"session_auth":"authenticated","finalization_epoch":0}')""",
            capture,
            ids["task"],
            ids["run"],
        )
        await connection.execute(
            """INSERT INTO qc_rule_execution(task_id,rule_id,page_capture_id,agent_run_id,
                                            execution_status,judge_strategy,judge_input)
               VALUES($1::uuid,$2::uuid,$3::uuid,$4::uuid,'pass','hybrid','{}'),
                     ($1::uuid,$5::uuid,NULL,$6::uuid,'pass','hybrid','{"finalization_epoch":1}')""",
            ids["task"],
            rules["eligible"],
            capture,
            judge,
            rules["auth"],
            ids["run"],
        )
        snapshot = await PostgresTaskRuleInventoryRepository(pool=pool).load_snapshot(
            task_id=ids["task"], access_scope=scope
        )
        assert snapshot["auth_gate"] == "guest"
        assert [(row["rule_id"], row["agent_run_id"]) for row in snapshot["executions"]] == [
            (rules["eligible"], judge)
        ]


async def test_aggregate_snapshot_needs_exact_link_instead_of_same_rule_consumer() -> None:
    async with _database() as (connection, pool, ids, rules, scope, _persisted, _command):
        judge = str(uuid4())
        await connection.execute(
            """INSERT INTO qc_agent_run(id,task_id,agent_type,agent_name,status)
               VALUES($1::uuid,$2::uuid,'rule_judge','aggregate judge','done')""",
            judge,
            ids["task"],
        )
        await connection.execute(
            """UPDATE qc_task SET finalization_agent_run_id=$2::uuid,finalization_epoch=1
               WHERE id=$1::uuid""",
            ids["task"],
            ids["run"],
        )
        work_ids = []
        input_ids = []
        capture = str(uuid4())
        await connection.execute(
            """INSERT INTO qc_capture(id,task_id,agent_run_id,surface_kind,capture_role,metadata)
               VALUES($1::uuid,$2::uuid,$3::uuid,'android','observation',
                      '{"finalization_epoch":0}')""",
            capture,
            ids["task"],
            ids["run"],
        )
        for epoch in (0, 1):
            input_hash, work_id = uuid4().hex + uuid4().hex, str(uuid4())
            input_id = f"evidence-input-set:{input_hash}"
            input_ids.append(input_id)
            work_ids.append(work_id)
            await connection.execute(
                """INSERT INTO qc_evidence_input_set(
                       input_set_id,input_set_hash,task_id,source_agent_run_id,rule_id,
                       rule_code,rule_contract_hash,ruleset_snapshot_hash,scope,scope_key,
                       finalization_epoch,eligibility_snapshot_id,eligibility_snapshot_hash,
                       selection_policy_hash,eligible_capture_ids,protocol_version,
                       input_set_payload,sealed_at)
                   VALUES($1,$2,$3::uuid,$4::uuid,$5::uuid,'inventory_eligible','contract-1',
                          'snapshot-1','task','inventory_aggregate',$6,'eligibility-1','hash-1',
                          'policy-1',$7::jsonb,'evidence-input-set.v1','{}'::jsonb,now())""",
                input_id,
                input_hash,
                ids["task"],
                ids["run"],
                rules["eligible"],
                epoch,
                json.dumps([capture]),
            )
            await connection.execute(
                """INSERT INTO qc_aggregate_inspection_work_item(
                       id,task_id,evidence_input_set_id,producer_agent_run_id,consumer_agent_run_id,
                       rule_id,rule_code,rule_contract_hash,scope,scope_key,protocol_version,
                       input_hash,status,finalization_epoch,lease_owner,lease_token,lease_expires_at)
                   VALUES($1::uuid,$2::uuid,$3,$4::uuid,$5::uuid,$6::uuid,'inventory_eligible',
                          'contract-1','task','inventory_aggregate',
                          'aggregate-inspection-work-item.v1',$7,'running',$8,
                          'inventory-test',gen_random_uuid(),now()+interval '1 minute')""",
                work_id,
                ids["task"],
                input_id,
                ids["run"],
                judge,
                rules["eligible"],
                uuid4().hex + uuid4().hex,
                epoch,
            )
        old_execution = str(uuid4())
        await connection.execute(
            """INSERT INTO qc_rule_execution(
                   id,task_id,rule_id,agent_run_id,execution_status,judge_strategy,
                   evidence_input_set_id,aggregate_work_item_id)
               VALUES($1::uuid,$2::uuid,$3::uuid,$4::uuid,'pass','hybrid',$5,$6::uuid)""",
            old_execution,
            ids["task"],
            rules["eligible"],
            judge,
            input_ids[0],
            work_ids[0],
        )
        reader = PostgresTaskRuleInventoryRepository(pool=pool)
        snapshot = await reader.load_snapshot(task_id=ids["task"], access_scope=scope)
        assert snapshot["executions"] == []
        assert [row["work_item_id"] for row in snapshot["work_rules"]] == [work_ids[1]]
        await connection.execute(
            """UPDATE qc_rule_execution SET evidence_input_set_id=$2,aggregate_work_item_id=$3::uuid
               WHERE id=$1::uuid""",
            old_execution,
            input_ids[1],
            work_ids[1],
        )
        snapshot = await reader.load_snapshot(task_id=ids["task"], access_scope=scope)
        assert [row["execution_id"] for row in snapshot["executions"]] == [old_execution]


@pytest.mark.parametrize(
    ("gate_passed", "state"), [(True, "finalizing"), (True, "sealed"), (False, "sealed")]
)
async def test_verified_gate_binds_sealed_task_to_original_evidence_epoch(
    gate_passed, state
) -> None:
    async with _database(transactional=True) as (
        connection, pool, ids, rules, scope, _persisted, _command
    ):
        capture, work, execution = str(uuid4()), str(uuid4()), str(uuid4())
        await connection.execute(
            "UPDATE qc_task SET finalization_agent_run_id=$2::uuid,status='running' "
            "WHERE id=$1::uuid",
            ids["task"],
            ids["run"],
        )
        await connection.execute(
            """INSERT INTO qc_capture(id,task_id,agent_run_id,surface_kind,capture_role,metadata)
               VALUES($1::uuid,$2::uuid,$3::uuid,'android','observation',
                      '{"session_auth":"authenticated","finalization_epoch":0}')""",
            capture,
            ids["task"],
            ids["run"],
        )
        await connection.execute(
            """INSERT INTO qc_inspection_work_item(
                   id,task_id,page_capture_id,producer_agent_run_id,ruleset_id,ruleset_version,
                   batch_key,strategy,session_dependency,input_hash,finalization_epoch)
               VALUES($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5::uuid,1,
                      'inventory-epoch','hybrid','snapshot',$6,0)""",
            work,
            ids["task"],
            capture,
            ids["run"],
            ids["ruleset"],
            uuid4().hex + uuid4().hex,
        )
        await connection.execute(
            """INSERT INTO qc_inspection_work_item_rule(
                   work_item_id,rule_id,rule_code,ordinal,contract_hash)
               VALUES($1::uuid,$2::uuid,'inventory_eligible',0,'contract-1')""",
            work,
            rules["eligible"],
        )
        await connection.execute(
            """INSERT INTO qc_rule_execution(
                   id,task_id,rule_id,page_capture_id,agent_run_id,execution_status,judge_strategy)
               VALUES($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5::uuid,'pass','hybrid')""",
            execution,
            ids["task"],
            rules["eligible"],
            capture,
            ids["run"],
        )
        reader = PostgresTaskRuleInventoryRepository(pool=pool)
        before = await reader.load_snapshot(task_id=ids["task"], access_scope=scope)
        assert before["task_finalization_epoch"] == before["finalization_epoch"] == 0
        assert [item["execution_id"] for item in before["executions"]] == [execution]
        facts = CompletionGateFacts(
            exploration_terminal=gate_passed,
            pending_action_count=0,
            pending_capture_commit_count=0,
            nonterminal_enforced_work_item_count=0,
            active_session_request_count=0,
            candidate_rule_count=1,
            terminal_candidate_rule_count=1,
            background_execution_count=0,
            review_policy_satisfied=True,
        )
        gate = build_completion_gate_snapshot(
            task_id=ids["task"],
            finalization_epoch=1,
            evaluation_sequence=1,
            facts=facts,
            evaluated_at=datetime.now(UTC),
        )
        await _insert_gate_snapshot(connection, gate)
        # 错阶段绑定在数据库层也必须被拒绝，不能让读侧借此放宽到历史证据。
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            async with connection.transaction():
                await connection.execute(
                    "UPDATE qc_task SET completion_gate_snapshot_id=$2 WHERE id=$1::uuid",
                    ids["task"],
                    gate.gate_snapshot_id,
                )
        await connection.execute(
            """UPDATE qc_task SET finalization_state=$2::varchar,finalization_epoch=1,
                   completion_gate_snapshot_id=$3,exploration_draining_at=now(),
                   session_draining_at=now(),
                   sealed_at=CASE WHEN $2='sealed' THEN now() ELSE NULL END,
                   execution_status=CASE WHEN $2='sealed' THEN 'needs_review' ELSE NULL END,
                   quality_outcome=CASE WHEN $2='sealed' THEN 'unknown' ELSE NULL END,
                   status=CASE WHEN $2='sealed' THEN 'review' ELSE 'running' END
               WHERE id=$1::uuid""",
            ids["task"],
            state,
            gate.gate_snapshot_id,
        )
        after = await reader.load_snapshot(task_id=ids["task"], access_scope=scope)
        assert after["task_finalization_epoch"] == 1
        assert after["finalization_epoch"] == (0 if gate_passed else 1)
        assert [item["execution_id"] for item in after["executions"]] == (
            [execution] if gate_passed else []
        )
        assert [item["work_item_id"] for item in after["work_rules"]] == (
            [work] if gate_passed else []
        )
        assert after["auth_gate"] == ("guest" if gate_passed else "authenticated")
