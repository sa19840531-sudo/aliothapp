"""真实 PostgreSQL 上验证页面类型判断记录：追加、改判指回、幂等、抄录收口状态、识别写库挂接。

默认跳过；迁移后的隔离数据库可通过 ``ALIOTH_TEST_DATABASE_URL`` 显式启用。
"""

from __future__ import annotations

import os
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.application.page_type_judgements import (
    PageTypeJudgementInput,
    group_judgements,
)
from services.api.app.domain.page.type_judgement import JudgementKind, ResolutionStatus
from services.api.app.infrastructure.db.page_capture_repository import (
    PostgresPageCaptureRepository,
)
from services.api.app.infrastructure.db.page_type_judgement_repository import (
    PostgresPageTypeJudgementRepository,
)
from services.api.app.infrastructure.db.task_insight_repository import (
    PostgresTaskInsightRepository,
)


async def test_judgements_append_revise_and_stay_frozen_after_seal() -> None:
    database_url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL")

    ids = {name: str(uuid4()) for name in ("tenant", "user", "ruleset", "task", "run", "capture")}
    connection = await asyncpg.connect(database_url)
    pool = await asyncpg.create_pool(database_url)
    try:
        await _seed(connection, ids)
        repository = PostgresPageTypeJudgementRepository(pool=pool)

        def _input(source: str, page_type: str | None, **basis) -> PageTypeJudgementInput:
            return PageTypeJudgementInput(
                task_id=ids["task"],
                agent_run_id=ids["run"],
                page_capture_id=ids["capture"],
                source=source,
                page_type=page_type,
                page_state="stable",
                catalog_version="v1",
                basis=basis,
            )

        first = await repository.record(
            inputs=(
                _input("structure_rules", "home", detection_status="recognized"),
                _input("model", "content_list", detection_status="recognized"),
            )
        )
        assert [(item.sequence_no, item.source, item.kind) for item in first] == [
            (1, "structure_rules", JudgementKind.JUDGEMENT),
            (2, "model", JudgementKind.JUDGEMENT),
        ]
        assert first[0].task_finalization_state == "open"

        # 同样内容再记一次：幂等，不新增
        again = await repository.record(
            inputs=(_input("structure_rules", "home", detection_status="recognized"),)
        )
        assert again == []

        page = group_judgements(await repository.list_for_task(task_id=ids["task"]))[0]
        assert page.resolution.status is ResolutionStatus.CONFLICTING
        assert page.resolution.page_type is None

        # 结构规则改判为 content_list：追加一条 revision 指回第一条，不覆盖历史
        revised = await repository.record(
            inputs=(
                _input(
                    "structure_rules",
                    "content_list",
                    detection_status="recognized",
                    reason="登录后重识别",
                ),
            )
        )
        assert revised[0].kind is JudgementKind.REVISION
        assert revised[0].revises_judgement_id == first[0].judgement_id
        assert revised[0].sequence_no == 3
        page = group_judgements(await repository.list_for_task(task_id=ids["task"]))[0]
        assert page.resolution.status is ResolutionStatus.REVISED
        assert page.resolution.page_type == "content_list"
        assert len(page.judgements) == 3

        # 收口后到达的判断只留档：任务收口状态受生命周期约束，这里只验证记录时确实抄了状态，
        # 「收口后不参与裁定」的规则由领域单测覆盖。
        assert all(item.task_finalization_state == "open" for item in first)

        # 任务详情台账查询：带正式页型
        rows = await PostgresTaskInsightRepository(pool=pool).list_page_type_judgement_rows(
            task_id=ids["task"], agent_run_id=None
        )
        assert len(rows) == 3
        assert rows[0]["formal_page_type"] == "home"
        assert rows[0]["page_seq"] == 1

        # 识别写库挂接：同事务追加结构规则与模型两份判断
        second_capture = str(uuid4())
        await connection.execute(
            """
            INSERT INTO qc_page_capture (
                id, task_id, agent_run_id, page_seq, page_slug, url, page_type, capture_status
            )
            VALUES (
                $1::uuid, $2::uuid, $3::uuid, 2, 'list', 'https://example.com/list',
                'video_list', 'captured'
            )
            """,
            second_capture,
            ids["task"],
            ids["run"],
        )
        writer = PostgresPageCaptureRepository(pool=pool, page_graph_repository=None)
        assert await writer.update_detection_result(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            page_capture_id=second_capture,
            page_type="video_list",
            page_state="stable",
            detection_status="recognized",
            confidence=0.9,
            catalog_version="v1",
            catalog_sha256="sha256:catalog",
            confidence_source="deterministic_structure",
            model_page_type="content_list",
            model_confidence=0.6,
        )
        pages = {
            page.page_capture_id: page
            for page in group_judgements(await repository.list_for_task(task_id=ids["task"]))
        }
        hooked = pages[second_capture]
        assert [(item.source, item.page_type) for item in hooked.judgements] == [
            ("structure_rules", "video_list"),
            ("model", "content_list"),
        ]
        assert hooked.resolution.status is ResolutionStatus.CONFLICTING
        formal = await connection.fetchval(
            "SELECT page_type FROM qc_page_capture WHERE id = $1::uuid", second_capture
        )
        assert formal == "video_list"  # 正式页型仍由识别写库决定，台账不改它
    finally:
        await connection.execute("DELETE FROM qc_task WHERE id = $1::uuid", ids["task"])
        await connection.execute("DELETE FROM qc_ruleset WHERE id = $1::uuid", ids["ruleset"])
        await connection.execute("DELETE FROM qc_user WHERE id = $1::uuid", ids["user"])
        await connection.execute("DELETE FROM qc_tenant WHERE id = $1::uuid", ids["tenant"])
        await pool.close()
        await connection.close()


async def _seed(connection: asyncpg.Connection, ids: dict[str, str]) -> None:
    await connection.execute(
        """
        INSERT INTO qc_tenant (id, tenant_key, display_name)
        VALUES ($1::uuid, $2, 'Page Type Judgement')
        """,
        ids["tenant"],
        f"page-type-judgement-{ids['tenant']}",
    )
    await connection.execute(
        """
        INSERT INTO qc_user (id, username, full_name, password_hash, role, tenant_id)
        VALUES ($1::uuid, $2, 'Page Type Judgement', 'not-a-login-secret', '平台管理员', $3::uuid)
        """,
        ids["user"],
        f"page-type-judgement-{ids['user']}",
        ids["tenant"],
    )
    await connection.execute(
        """
        INSERT INTO qc_ruleset (
            id, ruleset_code, name, version, status, snapshot_hash, rule_count, source_type
        )
        VALUES ($1::uuid, $2, 'Page Type Judgement', 1, 'enabled', 'snapshot-1', 0, 'manual')
        """,
        ids["ruleset"],
        f"page-type-judgement-{ids['ruleset']}",
    )
    await connection.execute(
        """
        INSERT INTO qc_task (id, target_url, ruleset_id, status, submitter_id, tenant_id)
        VALUES ($1::uuid, 'https://example.com', $2::uuid, 'running', $3::uuid, $4::uuid)
        """,
        ids["task"],
        ids["ruleset"],
        ids["user"],
        ids["tenant"],
    )
    await connection.execute(
        """
        INSERT INTO qc_agent_run (id, task_id, agent_type, agent_name, status)
        VALUES ($1::uuid, $2::uuid, 'main', 'Page Type Judgement', 'running')
        """,
        ids["run"],
        ids["task"],
    )
    await connection.execute(
        """
        INSERT INTO qc_page_capture (
            id, task_id, agent_run_id, page_seq, page_slug, url, page_type, capture_status
        )
        VALUES ($1::uuid, $2::uuid, $3::uuid, 1, 'home', 'https://example.com', 'home', 'captured')
        """,
        ids["capture"],
        ids["task"],
        ids["run"],
    )
