"""真实 PG 验证旧错误页读取资格、确认门禁与租户隔离，保留存储审计。"""

import json
import os
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.application.report_execution_eligibility import qualify_report_execution
from services.api.app.application.report_insight import (
    ReportConfirmationError,
    ReportInsightService,
)
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.infrastructure.db.task_insight_repository import PostgresTaskInsightRepository
from services.api.app.infrastructure.db.test_database_guard import (
    require_isolated_test_database_url,
)
from services.api.tests.test_report_error_document import _input
from services.api.tests.test_report_rule_page_postgres import _Pool


@pytest.mark.parametrize("document_kind", ["error", "business", "tutorial"])
async def test_frozen_error_document_is_not_confirmable_and_original_audit_is_unchanged(
    document_kind,
):
    error_document = document_kind == "error"
    url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not url:
        pytest.skip("需要完整迁移后的隔离 ALIOTH_TEST_DATABASE_URL")
    url = require_isolated_test_database_url(url, os.getenv("ALIOTH_DATABASE_URL") or None)
    connection = await asyncpg.connect(url)
    transaction = connection.transaction()
    await transaction.start()
    try:
        tenant, actor, ruleset, rule, task, run, execution = [str(uuid4()) for _ in range(7)]
        await connection.execute(
            "INSERT INTO qc_tenant(id,tenant_key,display_name) VALUES($1::uuid,$1::text,$1::text)",
            tenant,
        )
        await connection.execute(
            """INSERT INTO qc_user(id,username,full_name,password_hash,role,tenant_id)
               VALUES($1::uuid,$1::text,'错误页报告回归','隔离测试','审计员',$2::uuid)""",
            actor,
            tenant,
        )
        await connection.execute(
            """INSERT INTO qc_ruleset(id,ruleset_code,name,version,status,source_type)
               VALUES($1::uuid,$1::text,'报告资格规则集',1,'enabled','manual')""",
            ruleset,
        )
        await connection.execute(
            """INSERT INTO qc_rule(id,ruleset_id,rule_code,name,category,executor_type,check_point)
               VALUES($1::uuid,$2::uuid,$1::text,'业务内容风险','input_risk','hybrid','实际业务正文')""",
            rule,
            ruleset,
        )
        await connection.execute(
            """INSERT INTO qc_task(id,target_url,ruleset_id,submitter_id,tenant_id,
                                   target_platform,status)
               VALUES($1::uuid,'https://test.invalid',$2::uuid,$3::uuid,$4::uuid,'web','review')""",
            task,
            ruleset,
            actor,
            tenant,
        )
        await connection.execute(
            """INSERT INTO qc_agent_run(id,task_id,agent_type,agent_name,status)
               VALUES($1::uuid,$2::uuid,'main','报告资格回归','done')""",
            run,
            task,
        )
        facts = _input() if error_document else _input("正常业务内容", "实际发布正文")
        if document_kind == "tutorial":
            facts = _input(
                "Cloudflare 520: Web server is returning an unknown error 故障排查教程",
                "本文介绍 Cloudflare Error code 520 的排查步骤，先保存 Ray ID，再检查源站日志。",
            )
        await connection.execute(
            """INSERT INTO qc_rule_execution(id,task_id,agent_run_id,rule_id,
                execution_status,judge_strategy,judge_input,result_summary,human_verdict,
                human_verdict_note,human_verdict_by,human_verdict_at)
               VALUES($1::uuid,$2::uuid,$3::uuid,$4::uuid,'manual_review','hybrid',$5::jsonb,
                      '历史机器记录','fail','历史人工备注',$6::text,now())""",
            execution,
            task,
            run,
            rule,
            json.dumps(facts),
            actor,
        )
        before = await connection.fetchrow(
            "SELECT * FROM qc_rule_execution WHERE id=$1::uuid", execution
        )
        repository = PostgresTaskInsightRepository(pool=_Pool(connection))
        scope = AccessScope(actor_user_id=actor, tenant_id=tenant, permissions=frozenset())
        row = await repository.fetch_rule_execution(execution, task_id=task, access_scope=scope)
        assert row is not None
        assert json.loads(row["judge_input"]) == facts
        assert row["judge_strategy"] == "hybrid"
        rows = await repository.list_rule_execution_rows(
            task_id=task, agent_run_id=run, access_scope=scope
        )
        assert len(rows) == 1
        item = ReportInsightService(repository=repository)._rule_item(
            qualify_report_execution(rows[0])
        )
        assert item["machine_display_status"] == (
            "detect_error" if error_document else "manual_review"
        )
        outsider = AccessScope(
            actor_user_id=str(uuid4()), tenant_id=str(uuid4()), permissions=frozenset()
        )
        assert await repository.fetch_rule_execution(
            execution, task_id=task, access_scope=outsider
        ) is None
        assert await repository.fetch_rule_execution(
            execution, task_id=str(uuid4()), access_scope=scope
        ) is None
        service = ReportInsightService(repository=repository)
        if error_document:
            with pytest.raises(ReportConfirmationError):
                await service.confirm_rule(task, execution, verdict="pass", access_scope=scope)
            after = await connection.fetchrow(
                "SELECT * FROM qc_rule_execution WHERE id=$1::uuid", execution
            )
            assert dict(after) == dict(before)
        else:
            result = await service.confirm_rule(
                task, execution, verdict="pass", note="基于实际正文复核", access_scope=scope
            )
            assert result["human_verdict"] == "pass"
            assert result["human_verdict_by"] == actor
    finally:
        await transaction.rollback()
        await connection.close()
