"""隔离 PG 比较明细批量替代与原逐条门禁，覆盖空值和沿用来源权限。"""

import json
import os
import re
from contextlib import asynccontextmanager
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.infrastructure.db.rule_result_scope_sql import (
    rule_replacement_ctes_sql,
    superseded_placeholder_sql,
)
from services.api.app.infrastructure.db.task_insight_repository import (
    PostgresTaskInsightRepository,
    _scope_sql,
)
from services.api.app.infrastructure.db.task_repository import (
    PostgresTaskRepository,
    _optional_read_scope_sql,
)
from services.api.app.infrastructure.db.test_database_guard import (
    require_isolated_test_database_url,
)


class _Pool:
    def __init__(self, connection):
        self.connection = connection
        self.query = None
        self.args = None
        self.rows = None

    @asynccontextmanager
    async def acquire(self):
        yield self

    async def fetch(self, query, *args):
        self.query, self.args = query, args
        self.rows = await self.connection.fetch(query, *args)
        return self.rows


@pytest.fixture
async def execution_case():
    url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not url:
        pytest.skip("需要显式设置完整迁移后的隔离 ALIOTH_TEST_DATABASE_URL")
    connection = await asyncpg.connect(
        require_isolated_test_database_url(url, os.getenv("ALIOTH_DATABASE_URL") or None)
    )
    try:
        for table in (
            "qc_task",
            "qc_agent_run",
            "qc_rule_execution",
            "qc_capture",
            "qc_page_capture",
            "qc_app_capture",
            "qc_app_visual_observation",
            "qc_rule",
        ):
            await connection.execute(
                f"CREATE TEMP TABLE {table} (LIKE public.{table} INCLUDING DEFAULTS)"
            )
        ids = {
            name: str(uuid4())
            for name in ("task", "old", "current", "judge", "rule", "tenant", "actor")
        }
        await connection.execute(
            """INSERT INTO qc_task(id,target_url,ruleset_id,submitter_id,tenant_id,
                   target_platform,department_name,task_config,finalization_agent_run_id)
               VALUES($1::uuid,'https://example.test/',$1::uuid,$2::uuid,$3::uuid,
                      'app','质检部','{"workflow_version":"v2"}',$4::uuid)""",
            ids["task"],
            ids["actor"],
            ids["tenant"],
            ids["current"],
        )
        for name, kind, offset in (
            ("old", "main", 2),
            ("current", "main", 1),
            ("judge", "rule_judge", 0),
        ):
            await connection.execute(
                """INSERT INTO qc_agent_run(id,task_id,agent_type,agent_name,status,created_at)
                   VALUES($1::uuid,$2::uuid,$3,'读取回归','done',now()-$4*interval '1 minute')""",
                ids[name],
                ids["task"],
                kind,
                offset,
            )
        await connection.execute(
            """INSERT INTO qc_rule(id,ruleset_id,rule_code,name,category,executor_type,check_point)
               VALUES($1::uuid,$1::uuid,'查询规则','查询规则','visual','hybrid','检查内容')""",
            ids["rule"],
        )
        yield connection, _Pool(connection), ids
    finally:
        await connection.close()


async def _capture(connection, ids, page_type, run="current", page_record=False):
    capture = str(uuid4())
    await connection.execute(
        """INSERT INTO qc_capture(id,task_id,agent_run_id,surface_kind,capture_role,
                                   capture_status,metadata)
           VALUES($1::uuid,$2::uuid,$3::uuid,'web','after_action','captured',$4::jsonb)""",
        capture,
        ids["task"],
        ids[run],
        json.dumps({"page_type": page_type}),
    )
    if page_record:
        await connection.execute(
            """INSERT INTO qc_page_capture(id,task_id,agent_run_id,page_seq,page_slug,
                                          url,page_type,capture_status)
               VALUES($1::uuid,$2::uuid,$3::uuid,1,$1::text,'https://example.test/',$4,'captured')""",
            capture,
            ids["task"],
            ids[run],
            page_type,
        )
    return capture


async def _execution(
    connection,
    ids,
    *,
    run="current",
    capture=None,
    status="pass",
    output=None,
    bootstrap=False,
    evidence=(),
):
    identity = str(uuid4())
    await connection.execute(
        """INSERT INTO qc_rule_execution(id,task_id,agent_run_id,rule_id,page_capture_id,
                execution_status,judge_strategy,judge_input,judge_output,evidence_artifact_ids,
                started_at,finished_at,created_at,quality_outcome)
           VALUES($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5::uuid,$6,'hybrid',$7::jsonb,$8::jsonb,
                  $9::jsonb,now(),now(),now(),'unknown')""",
        identity,
        ids["task"],
        ids[run],
        ids["rule"],
        capture,
        status,
        json.dumps({"bootstrap": bootstrap}),
        json.dumps(output or {}),
        json.dumps(list(evidence)),
    )
    return identity


_OCCLUDED = {"presentation_rejection": {"reason_code": "BASE_PAGE_OCCLUDED"}}
_PENDING = {"reason": "candidate_not_executed"}


@pytest.mark.parametrize(
    "platform,version,owner,conclusion_run,capture_run,status,output,bootstrap,"
    "placeholder_page,conclusion_page,conclusion_status,expected_removed",
    [
        (
            "app",
            "v2",
            "current",
            "old",
            "old",
            "skipped",
            _PENDING,
            False,
            None,
            None,
            "pass",
            True,
        ),
        ("h5", "v1", "current", "old", "old", "skipped", _PENDING, False, None, None, "pass", True),
        (
            "h5",
            "v2",
            "current",
            "old",
            "old",
            "skipped",
            _PENDING,
            False,
            None,
            None,
            "pass",
            False,
        ),
        (
            "h5",
            "v2",
            "current",
            "judge",
            "current",
            "skipped",
            _PENDING,
            False,
            None,
            "home",
            "pass",
            True,
        ),
        (
            "h5",
            "v2",
            None,
            "judge",
            "current",
            "skipped",
            _PENDING,
            False,
            None,
            "home",
            "pass",
            True,
        ),
        (
            "h5",
            "v2",
            "old",
            "current",
            "current",
            "skipped",
            _PENDING,
            False,
            None,
            "home",
            "pass",
            False,
        ),
        (
            "app",
            "v2",
            "current",
            "judge",
            "current",
            "evidence_insufficient",
            _OCCLUDED,
            False,
            "home",
            "home",
            "pass",
            True,
        ),
        (
            "app",
            "v2",
            "current",
            "judge",
            "current",
            "evidence_insufficient",
            _OCCLUDED,
            False,
            "home",
            "video_list",
            "pass",
            False,
        ),
        (
            "app",
            "v2",
            "current",
            "judge",
            "current",
            "evidence_insufficient",
            _OCCLUDED,
            False,
            None,
            None,
            "pass",
            False,
        ),
        (
            "app",
            "v2",
            "current",
            "judge",
            "current",
            "evidence_insufficient",
            _OCCLUDED,
            False,
            "",
            "",
            "pass",
            True,
        ),
        (
            "app",
            "v2",
            "current",
            "judge",
            "current",
            "evidence_insufficient",
            {},
            False,
            "home",
            "home",
            "pass",
            True,
        ),
        (
            "app",
            "v2",
            "current",
            "judge",
            "current",
            "evidence_insufficient",
            {},
            False,
            None,
            None,
            "pass",
            False,
        ),
        (
            "app",
            "v2",
            "current",
            "judge",
            "current",
            "skipped",
            _PENDING,
            False,
            "home",
            "home",
            "evidence_insufficient",
            False,
        ),
        ("app", "v2", "current", "judge", "current", "skipped", {}, True, None, None, "pass", True),
        ("app", "v2", "current", "judge", "current", "pass", {}, True, None, None, "pass", False),
        (
            "web",
            "v2",
            "current",
            "old",
            "old",
            "skipped",
            _PENDING,
            False,
            None,
            None,
            "pass",
            True,
        ),
    ],
)
async def test_batch_replacement_matches_original_null_page_and_current_run_semantics(
    execution_case,
    platform,
    version,
    owner,
    conclusion_run,
    capture_run,
    status,
    output,
    bootstrap,
    placeholder_page,
    conclusion_page,
    conclusion_status,
    expected_removed,
):
    connection, _pool, ids = execution_case
    await connection.execute(
        "UPDATE qc_task SET target_platform=$2,task_config=$3::jsonb,"
        "finalization_agent_run_id=$4::uuid WHERE id=$1::uuid",
        ids["task"],
        platform,
        json.dumps({"workflow_version": version}),
        ids.get(owner),
    )
    placeholder_capture = await _capture(connection, ids, placeholder_page)
    conclusion_capture = await _capture(connection, ids, conclusion_page, run=capture_run)
    placeholder = await _execution(
        connection,
        ids,
        capture=placeholder_capture,
        status=status,
        output=output,
        bootstrap=bootstrap,
    )
    await _execution(
        connection,
        ids,
        run=conclusion_run,
        capture=conclusion_capture,
        status=conclusion_status,
    )
    reference = await connection.fetch(
        f"SELECT execution.id FROM qc_rule_execution execution WHERE execution.task_id=$1::uuid "
        f"AND NOT {superseded_placeholder_sql('execution')}",
        ids["task"],
    )
    candidate = await connection.fetch(
        rule_replacement_ctes_sql("")
        + "SELECT execution.id FROM qc_rule_execution execution WHERE execution.task_id=$1::uuid "
        "AND execution.id NOT IN (SELECT execution_id FROM superseded_execution_ids)",
        ids["task"],
    )
    assert {row["id"] for row in candidate} == {row["id"] for row in reference}
    assert (placeholder not in {str(row["id"]) for row in candidate}) is expected_removed


def _original_query(query, scope_sql):
    query = query.replace(rule_replacement_ctes_sql(scope_sql), "", 1)
    query = re.sub(
        r"AND execution.id NOT IN \(\s*SELECT execution_id FROM superseded_execution_ids\s*\)",
        "AND NOT " + superseded_placeholder_sql("execution"),
        query,
    )
    query = re.sub(
        r"LEFT JOIN LATERAL \(\s*SELECT source.id, source.evidence_artifact_ids.*?"
        r"LIMIT 1\s*\) AS reuse_source ON TRUE",
        "LEFT JOIN qc_rule_execution AS reuse_source ON reuse_source.task_id=execution.task_id "
        "AND reuse_source.id::text=execution.judge_output->'reuse'->>'source_execution_id'",
        query,
        flags=re.DOTALL,
    )
    return query


@pytest.mark.parametrize("repository_kind", ["insight", "legacy"])
async def test_both_readers_match_original_complete_rows_and_permission_boundaries(
    execution_case,
    repository_kind,
):
    connection, pool, ids = execution_case
    source = await _execution(connection, ids, evidence=[str(uuid4())])
    for raw_source in (
        source,
        source.upper(),
        "{" + source + "}",
        " " + source,
        "非法UUID",
        "",
        None,
    ):
        await _execution(connection, ids, output={"reuse": {"source_execution_id": raw_source}})
    # 跨任务来源即使 UUID 合法也不可借其附件。
    other = str(uuid4())
    other_source = await _execution(connection, ids, evidence=[str(uuid4())])
    await connection.execute(
        "UPDATE qc_rule_execution SET task_id=$2::uuid WHERE id=$1::uuid", other_source, other
    )
    await _execution(connection, ids, output={"reuse": {"source_execution_id": other_source}})
    await _execution(connection, ids, status="skipped", bootstrap=True, output=_PENDING)
    for actor, tenant, department, permissions in (
        (ids["actor"], ids["tenant"], "质检部", ()),
        (str(uuid4()), ids["tenant"], "质检部", ("taskObjects:read:department",)),
        (str(uuid4()), ids["tenant"], "产品部", ("taskObjects:read:tenant",)),
        (str(uuid4()), str(uuid4()), "", ("taskObjects:read:any",)),
        (str(uuid4()), str(uuid4()), "质检部", ()),
    ):
        scope = AccessScope(
            actor_user_id=actor,
            tenant_id=tenant,
            department_name=department,
            permissions=frozenset(permissions),
        )
        if repository_kind == "insight":
            scope_sql, _ = _scope_sql(scope, task_alias="task", first_param=3)
            await PostgresTaskInsightRepository(pool=pool).list_rule_execution_rows(
                task_id=ids["task"],
                agent_run_id=None,
                access_scope=scope,
            )
        else:
            scope_sql, _ = _optional_read_scope_sql(scope, task_alias="task", first_param=3)
            await PostgresTaskRepository(pool=pool).list_rule_executions(
                ids["task"],
                agent_run_id=None,
                access_scope=scope,
            )
        reference = await connection.fetch(_original_query(pool.query, scope_sql), *pool.args)
        assert {row["execution_id"]: dict(row) for row in pool.rows} == {
            row["execution_id"]: dict(row) for row in reference
        }
        if actor != ids["actor"] and tenant != ids["tenant"] and not permissions:
            assert not pool.rows
