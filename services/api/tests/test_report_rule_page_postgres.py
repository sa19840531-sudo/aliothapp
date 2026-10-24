"""完整迁移的隔离 PG 验证报告读取实际 App 页型及租户范围，结束后回滚。"""

import json
import os
from contextlib import asynccontextmanager
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.application.report_insight import ReportInsightService
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.infrastructure.db.task_insight_repository import PostgresTaskInsightRepository
from services.api.app.infrastructure.db.test_database_guard import (
    require_isolated_test_database_url,
)


class _Pool:
    def __init__(self, connection):
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


@pytest.mark.parametrize(
    "stored_type,expected", [(None, "video_list"), ("", "video_list"), ("home", "home")]
)
async def test_report_uses_persisted_app_page_type_without_guessing_applicability(
    stored_type, expected
):
    url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not url:
        pytest.skip("需要显式设置完整迁移后的隔离 ALIOTH_TEST_DATABASE_URL")
    url = require_isolated_test_database_url(url, os.getenv("ALIOTH_DATABASE_URL") or None)
    connection = await asyncpg.connect(url)
    transaction = connection.transaction()
    await transaction.start()
    try:
        tenant, actor, ruleset, rule, task, run, capture, artifact = [
            str(uuid4()) for _ in range(8)
        ]
        await connection.execute(
            "INSERT INTO qc_tenant(id,tenant_key,display_name) VALUES($1::uuid,$1::text,$1::text)",
            tenant,
        )
        await connection.execute(
            """INSERT INTO qc_user(id,username,full_name,password_hash,role,tenant_id)
               VALUES($1::uuid,$1::text,'报告读取回归','仅用于隔离测试','审计员',$2::uuid)""",
            actor,
            tenant,
        )
        await connection.execute(
            """INSERT INTO qc_ruleset(id,ruleset_code,name,version,status,source_type)
               VALUES($1::uuid,$1::text,'报告读取规则集',1,'enabled','manual')""",
            ruleset,
        )
        await connection.execute(
            """INSERT INTO qc_rule(id,ruleset_id,rule_code,name,category,executor_type,
                                   check_point,applicable_page_types)
               VALUES($1::uuid,$2::uuid,$1::text,'视频规则','visual','hybrid','检查内容','["payment"]')""",
            rule,
            ruleset,
        )
        await connection.execute(
            """INSERT INTO qc_task(id,target_url,ruleset_id,submitter_id,tenant_id,
                                   target_platform,status,task_config)
               VALUES($1::uuid,NULL,$2::uuid,$3::uuid,$4::uuid,'app','done',
                      '{"app":{"platform_build_ref":"report-test-build"}}')""",
            task,
            ruleset,
            actor,
            tenant,
        )
        await connection.execute(
            """INSERT INTO qc_agent_run(id,task_id,agent_type,agent_name,status)
               VALUES($1::uuid,$2::uuid,'main','报告读取回归','done')""",
            run,
            task,
        )
        await connection.execute(
            """INSERT INTO qc_surface_session(task_id,session_id,surface_kind,session_owner,status,
                                               activated_at,draining_at,closed_at)
               VALUES($1::uuid,'report-pg-session','android','回归测试','closed',now(),now(),now())""",
            task,
        )
        await connection.execute(
            """INSERT INTO qc_capture(id,task_id,agent_run_id,surface_kind,capture_role,
                                      capture_status,metadata)
               VALUES($1::uuid,$2::uuid,$3::uuid,'android','after_action','captured',$4::jsonb)""",
            capture,
            task,
            run,
            json.dumps({"page_type": stored_type}),
        )
        await connection.execute(
            """INSERT INTO qc_evidence_artifact(id,task_id,page_capture_id,artifact_type,source,
                                               storage_path,checksum,size_bytes)
               VALUES($1::uuid,$2::uuid,$3::uuid,'screenshot','app_runtime','test/report.png',$4,10)""",
            artifact,
            task,
            capture,
            "sha256:" + "a" * 64,
        )
        await connection.execute(
            """INSERT INTO qc_app_capture(capture_id,screenshot_artifact_id,width,height,
                                          image_signature)
               VALUES($1::uuid,$2::uuid,320,640,'report-pg-image')""",
            capture,
            artifact,
        )
        await connection.execute(
            """INSERT INTO qc_app_visual_observation(observation_id,task_id,session_id,capture_id,
                screenshot_artifact_id,contract_version,provider_id,model_id,result_hash,
                visual_page_type,visual_summary)
               VALUES($1::uuid,$2::uuid,'report-pg-session',$3::uuid,$4::uuid,
                      'v1','test','test',$5,'video_list','视频列表页')""",
            str(uuid4()),
            task,
            capture,
            artifact,
            "b" * 64,
        )
        await connection.execute(
            """INSERT INTO qc_rule_execution(task_id,agent_run_id,rule_id,page_capture_id,
                execution_status,judge_strategy,evidence_artifact_ids,human_verdict,
                human_verdict_by,human_verdict_at)
               VALUES($1::uuid,$2::uuid,$3::uuid,$4::uuid,'manual_review','hybrid',$5::jsonb,
                      'fail',$6::text,now())""",
            task,
            run,
            rule,
            capture,
            json.dumps([artifact]),
            actor,
        )
        repository = PostgresTaskInsightRepository(pool=_Pool(connection))
        scope = AccessScope(actor_user_id=actor, tenant_id=tenant, permissions=frozenset())
        rows = await repository.list_rule_execution_rows(
            task_id=task, agent_run_id=run, access_scope=scope
        )
        assert len(rows) == 1
        assert rows[0]["capture_page_type"] == expected
        item = ReportInsightService(repository=repository)._rule_item(rows[0])
        assert item["bound_page_type_name"] == (
            "视频列表页" if expected == "video_list" else "首页"
        )
        assert item["evidence_artifact_ids"] == [artifact]
        assert item["human_verdict_by"] == actor
        assert item["human_verdict_at"] == rows[0]["human_verdict_at"]
        assert item["human_verdict_at"] is not None
        confirmed = await repository.update_human_verdict(
            task_id=task, execution_id=rows[0]["execution_id"], verdict="fail",
            note="复核现有采集截图", operator=actor, access_scope=scope,
        )
        assert confirmed["human_verdict_by"] == actor
        assert confirmed["human_verdict_at"] is not None
        outsider = AccessScope(
            actor_user_id=str(uuid4()), tenant_id=str(uuid4()), permissions=frozenset()
        )
        assert (
            await repository.list_rule_execution_rows(
                task_id=task,
                agent_run_id=run,
                access_scope=outsider,
            )
            == []
        )
        assert await repository.update_human_verdict(
            task_id=task, execution_id=rows[0]["execution_id"], verdict="pass",
            note="越权不能改判", operator=outsider.actor_user_id, access_scope=outsider,
        ) is None
    finally:
        await transaction.rollback()
        await connection.close()
