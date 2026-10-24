"""真实 PostgreSQL 上验证质检范围：为找目标经过的页面不判规则、不当证据、不算页面。

默认跳过；迁移后的隔离数据库可通过 ``ALIOTH_TEST_DATABASE_URL`` 显式启用。
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.application.h5_posthoc_engine_judge import PostgresH5PosthocEvidenceSource
from services.api.app.application.rule_execution import CandidatePageReference
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.infrastructure.db.task_insight_repository import (
    PostgresTaskInsightRepository,
)
from services.api.app.infrastructure.db.task_repository import PostgresTaskRepository
from services.api.app.infrastructure.db.test_database_guard import (
    require_isolated_test_database_url,
)
from services.api.app.infrastructure.db.workstation_query_repository import (
    PostgresWorkstationQueryRepository,
)

_SCOPED_CONFIG = {"inspection_scope": {"page_types": ["video_detail"]}}
_UNSCOPED_CONFIG = {"inspection_scope": {"page_types": []}}
_SCREENSHOT_HANDLER = {"engine_config": {"handler": "screenshot_change_assertion"}}


def _test_database_url() -> str:
    database_url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL")
    return require_isolated_test_database_url(
        database_url,
        os.getenv("ALIOTH_DATABASE_URL"),
    )


async def test_passed_through_pages_stay_out_of_rules_evidence_and_page_counts() -> None:
    database_url = _test_database_url()
    ids = {name: str(uuid4()) for name in ("tenant", "user", "ruleset", "task", "run")}
    connection = await asyncpg.connect(database_url)
    pool = await asyncpg.create_pool(database_url)
    try:
        await _seed_task(connection, ids)
        rules = {
            "detail": await _seed_rule(connection, ids, "scope_detail", ["video_detail"]),
            "list": await _seed_rule(connection, ids, "scope_list", ["video_list"]),
            "detail_engine": await _seed_rule(
                connection, ids, "scope_detail_engine", ["video_detail"], engine=True
            ),
            "list_engine": await _seed_rule(
                connection, ids, "scope_list_engine", ["video_list"], engine=True
            ),
        }
        # 整任务判一次的聚合规则：由聚合发布收口，收尾不该按页面给它补「候选没执行」
        await connection.execute(
            """
            INSERT INTO qc_rule (
                id, ruleset_id, rule_code, name, category, executor_type,
                severity, status, check_point, applicable_page_types, judge_config
            )
            VALUES (
                $1::uuid, $2::uuid, 'scope_aggregate', 'scope_aggregate', 'interaction', 'hybrid',
                'high', 'enabled', '整任务检查', '["video_detail"]'::jsonb, $3::jsonb
            )
            """,
            str(uuid4()),
            ids["ruleset"],
            json.dumps(
                {
                    "capability_config": {
                        "evidence_input_scope": "task",
                        "evidence_scope_key": "official_group_entry",
                    }
                }
            ),
        )
        actions = {
            name: await _seed_action(connection, ids, name, sequence)
            for sequence, name in enumerate(("act-detail", "act-transit"), start=1)
        }
        detail_action, transit_action = actions["act-detail"], actions["act-transit"]
        captures = {
            "detail": await _seed_capture(connection, ids, 1, "video_detail"),
            "list": await _seed_capture(connection, ids, 2, "video_list"),
            "detail_before": await _seed_capture(
                connection, ids, 3, "video_detail", role="before_action", action=detail_action
            ),
            "detail_after": await _seed_capture(
                connection, ids, 4, "video_detail", role="after_action", action=detail_action
            ),
            "transit_before": await _seed_capture(
                connection, ids, 5, "video_list", role="before_action", action=transit_action
            ),
            "transit_after": await _seed_capture(
                connection, ids, 6, "video_detail", role="after_action", action=transit_action
            ),
        }
        shots = {
            name: await _seed_screenshot(connection, ids, capture)
            for name, capture in captures.items()
        }
        # 详情页规则的结论用到了经过页的截图：这张不能从证据里消失
        await connection.execute(
            """
            INSERT INTO qc_rule_execution (
                task_id, rule_id, page_capture_id, agent_run_id,
                execution_status, judge_strategy, evidence_artifact_ids
            )
            VALUES ($1::uuid, $2::uuid, $3::uuid, $4::uuid, 'pass', 'hybrid', $5::jsonb)
            """,
            ids["task"],
            rules["detail"],
            captures["detail"],
            ids["run"],
            json.dumps([shots["transit_before"]]),
        )
        task_repository = PostgresTaskRepository(pool=pool)
        insight_repository = PostgresTaskInsightRepository(pool=pool)
        posthoc_source = PostgresH5PosthocEvidenceSource(pool=pool)

        scoped = await _read_views(task_repository, insight_repository, posthoc_source, ids=ids)

        assert scoped["candidates"]["video_detail"] == {"scope_detail", "scope_detail_engine"}
        assert scoped["candidates"]["video_list"] == set()
        assert shots["list"] not in scoped["evidence"]
        assert {shots["detail"], shots["transit_before"]} <= scoped["evidence"]
        assert shots["list"] not in scoped["screenshots"]
        assert {shots["detail"], shots["transit_before"]} <= scoped["screenshots"]
        assert captures["list"] not in scoped["page_rows"]
        assert captures["transit_before"] not in scoped["page_rows"]
        assert captures["detail"] in scoped["page_rows"]
        assert scoped["recognized_pages"] == 1
        assert scoped["posthoc_rules"] == {"scope_detail_engine"}
        assert scoped["posthoc_actions"] == {actions["act-detail"]}
        # 重跑、复制要按原任务的质检范围建新任务
        detail = await PostgresWorkstationQueryRepository(pool=pool).get_task_detail(
            ids["task"],
            access_scope=AccessScope(
                actor_user_id=ids["user"],
                tenant_id=ids["tenant"],
                permissions=frozenset({"taskObjects:read:any"}),
            ),
        )
        assert detail is not None
        assert detail.inspection_scope_page_types == ("video_detail",)

        await _set_task_config(connection, ids, _UNSCOPED_CONFIG)
        unscoped = await _read_views(task_repository, insight_repository, posthoc_source, ids=ids)

        assert unscoped["candidates"]["video_list"] == {"scope_list", "scope_list_engine"}
        assert shots["list"] in unscoped["evidence"]
        assert shots["list"] in unscoped["screenshots"]
        assert captures["list"] in unscoped["page_rows"]
        assert unscoped["recognized_pages"] == 2
        assert unscoped["posthoc_rules"] == {"scope_detail_engine", "scope_list_engine"}
        assert unscoped["posthoc_actions"] == set(actions.values())

        await _set_task_config(connection, ids, _SCOPED_CONFIG)
        await task_repository.finalize_missing_rule_executions(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            finished_at=datetime.now(UTC),
            candidate_pages=[
                CandidatePageReference(
                    page_capture_id=captures["detail"], page_type="video_detail"
                ),
                CandidatePageReference(page_capture_id=captures["list"], page_type="video_list"),
            ],
            runtime_rule_fallbacks=[],
            runtime_page_failure_fallbacks=[],
        )

        placeholders = await connection.fetch(
            """
            SELECT rule.rule_code, execution.page_capture_id::text AS page_capture_id
            FROM qc_rule_execution execution
            JOIN qc_rule rule ON rule.id = execution.rule_id
            WHERE execution.task_id = $1::uuid
              AND execution.execution_status = 'skipped'
            """,
            ids["task"],
        )
        # 只给范围内还没结论的规则补占位；经过页和只挂在经过页上的规则都不补
        assert {(row["rule_code"], row["page_capture_id"]) for row in placeholders} == {
            ("scope_detail_engine", captures["detail"]),
        }
    finally:
        await connection.execute("DELETE FROM qc_task WHERE id = $1::uuid", ids["task"])
        await connection.execute("DELETE FROM qc_rule WHERE ruleset_id = $1::uuid", ids["ruleset"])
        await connection.execute("DELETE FROM qc_ruleset WHERE id = $1::uuid", ids["ruleset"])
        await connection.execute("DELETE FROM qc_user WHERE id = $1::uuid", ids["user"])
        await connection.execute("DELETE FROM qc_tenant WHERE id = $1::uuid", ids["tenant"])
        await pool.close()
        await connection.close()


async def _read_views(
    task_repository: PostgresTaskRepository,
    insight_repository: PostgresTaskInsightRepository,
    posthoc_source: PostgresH5PosthocEvidenceSource,
    *,
    ids: dict[str, str],
) -> dict[str, object]:
    candidates = {
        page_type: {
            rule.rule_code
            for rule in await task_repository.list_candidate_rules(
                task_id=ids["task"], page_type=page_type, auth_gate=None
            )
        }
        for page_type in ("video_detail", "video_list")
    }
    return {
        "candidates": candidates,
        "evidence": {
            item.artifact_id for item in await task_repository.list_evidence_artifacts(ids["task"])
        },
        "screenshots": {
            row["artifact_id"]
            for row in await insight_repository.list_screenshot_artifacts(
                task_id=ids["task"], agent_run_id=ids["run"]
            )
        },
        "page_rows": {
            row["page_capture_id"]
            for row in await insight_repository.list_page_capture_inspection_rows(
                task_id=ids["task"], agent_run_id=ids["run"]
            )
        },
        "recognized_pages": await insight_repository.count_recognized_pages(
            task_id=ids["task"], agent_run_id=ids["run"]
        ),
        "posthoc_rules": {
            rule.rule_code for rule in await posthoc_source.load_target_rules(task_id=ids["task"])
        },
        "posthoc_actions": {
            pair.action_execution_id
            for pair in await posthoc_source.load_action_pairs(task_id=ids["task"])
        },
    }


async def _set_task_config(
    connection: asyncpg.Connection, ids: dict[str, str], config: dict[str, object]
) -> None:
    await connection.execute(
        "UPDATE qc_task SET task_config = $2::jsonb WHERE id = $1::uuid",
        ids["task"],
        json.dumps(config),
    )


async def _seed_task(connection: asyncpg.Connection, ids: dict[str, str]) -> None:
    await connection.execute(
        "INSERT INTO qc_tenant (id, tenant_key, display_name) VALUES ($1::uuid, $2, 'Scope')",
        ids["tenant"],
        f"inspection-scope-{ids['tenant']}",
    )
    await connection.execute(
        """
        INSERT INTO qc_user (id, username, full_name, password_hash, role, tenant_id)
        VALUES ($1::uuid, $2, 'Scope', 'not-a-login-secret', '平台管理员', $3::uuid)
        """,
        ids["user"],
        f"inspection-scope-{ids['user']}",
        ids["tenant"],
    )
    await connection.execute(
        """
        INSERT INTO qc_ruleset (
            id, ruleset_code, name, version, status, snapshot_hash, rule_count, source_type
        )
        VALUES ($1::uuid, $2, 'Scope', 1, 'enabled', 'snapshot-1', 4, 'manual')
        """,
        ids["ruleset"],
        f"inspection-scope-{ids['ruleset']}",
    )
    await connection.execute(
        """
        INSERT INTO qc_task (
            id, target_url, ruleset_id, status, submitter_id, tenant_id, task_config
        )
        VALUES ($1::uuid, 'https://example.com', $2::uuid, 'running', $3::uuid, $4::uuid, $5::jsonb)
        """,
        ids["task"],
        ids["ruleset"],
        ids["user"],
        ids["tenant"],
        json.dumps(_SCOPED_CONFIG),
    )
    await connection.execute(
        """
        INSERT INTO qc_agent_run (id, task_id, agent_type, agent_name, status)
        VALUES ($1::uuid, $2::uuid, 'main', 'Inspection Scope', 'running')
        """,
        ids["run"],
        ids["task"],
    )
    await connection.execute(
        """
        INSERT INTO qc_surface_session (
            session_id, task_id, surface_kind, session_owner, status, finalization_epoch
        ) VALUES ($1, $2::uuid, 'web', 'inspection-scope-test', 'starting', 0)
        """,
        f"web:{ids['run']}",
        ids["task"],
    )


async def _seed_rule(
    connection: asyncpg.Connection,
    ids: dict[str, str],
    rule_code: str,
    page_types: list[str],
    *,
    engine: bool = False,
) -> str:
    rule_id = str(uuid4())
    await connection.execute(
        """
        INSERT INTO qc_rule (
            id, ruleset_id, rule_code, name, category, executor_type,
            severity, status, check_point, applicable_page_types, judge_config
        )
        VALUES (
            $1::uuid, $2::uuid, $3, $3, 'visual', $4,
            'high', 'enabled', '检查页面', $5::jsonb, $6::jsonb
        )
        """,
        rule_id,
        ids["ruleset"],
        rule_code,
        "rule_engine" if engine else "hybrid",
        json.dumps(page_types),
        json.dumps(_SCREENSHOT_HANDLER if engine else {}),
    )
    return rule_id


async def _seed_action(
    connection: asyncpg.Connection, ids: dict[str, str], name: str, sequence: int
) -> str:
    action_id = f"{name}-{ids['task']}"
    await connection.execute(
        """
        INSERT INTO qc_action_execution (
            action_execution_id, task_id, session_id, intent_id, idempotency_key,
            intent_hash, intent_protocol_version, intent_payload, command_sequence,
            finalization_epoch
        ) VALUES ($1, $2::uuid, $3, $4, $5, 'intent-hash', 'h5/v1', '{}'::jsonb, $6, 0)
        """,
        action_id,
        ids["task"],
        f"web:{ids['run']}",
        f"intent-{action_id}",
        f"idempotency-{action_id}",
        sequence,
    )
    return action_id


async def _seed_capture(
    connection: asyncpg.Connection,
    ids: dict[str, str],
    page_seq: int,
    page_type: str,
    *,
    role: str | None = None,
    action: str | None = None,
) -> str:
    capture_id = str(uuid4())
    await connection.execute(
        """
        INSERT INTO qc_page_capture (
            id, task_id, agent_run_id, page_seq, page_slug, url, page_type, capture_status,
            detection_status, surface_session_id, capture_role, action_execution_id,
            session_command_sequence
        )
        VALUES (
            $1::uuid, $2::uuid, $3::uuid, $4, $5, 'https://example.com', $6, 'captured',
            $7, $8, $9, $10, $11
        )
        """,
        capture_id,
        ids["task"],
        ids["run"],
        page_seq,
        f"page-{page_seq}",
        page_type,
        None if role else "recognized",
        f"web:{ids['run']}" if role else None,
        role,
        action,
        page_seq if role else None,
    )
    return capture_id


async def _seed_screenshot(
    connection: asyncpg.Connection, ids: dict[str, str], capture_id: str
) -> str:
    artifact_id = str(uuid4())
    await connection.execute(
        """
        INSERT INTO qc_evidence_artifact (
            id, task_id, page_capture_id, artifact_type, source, storage_path,
            content_type, checksum, size_bytes
        )
        VALUES (
            $1::uuid, $2::uuid, $3::uuid, 'screenshot', 'baseline', $4,
            'image/png', $5, 1
        )
        """,
        artifact_id,
        ids["task"],
        capture_id,
        f"tests/{artifact_id}.png",
        "sha256:" + "0" * 64,
    )
    return artifact_id
