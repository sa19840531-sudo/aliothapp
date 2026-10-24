"""真实 PostgreSQL 上验证规则测试记录表：写入、筛选翻页、详情、按勾选 / 按时间段删除。

默认跳过；迁移后的隔离数据库可通过 ``ALIOTH_TEST_DATABASE_URL`` 显式启用。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.infrastructure.db.rule_test_record_repository import (
    PostgresRuleTestRecordRepository,
)


def _record(record_id: str, *, rule_id: str, rule_code: str, tester_id: str | None, **extra):
    return {
        "id": record_id,
        "rule_id": rule_id,
        "rule_code": rule_code,
        "rule_name": "叹号结尾检查",
        "tester_user_id": tester_id,
        "tester_name": "规则配置员",
        "platform": "h5",
        "check_mode": "relaxed",
        "page_type": "content_detail",
        "page_state": "stable",
        "page_url": "https://example.test/a",
        "page_title": "详情",
        "visible_text": "",
        "dom_html": "",
        "ui_snapshot": None,
        "judge_input": {},
        "screenshots": [],
        "outcome": "pass",
        "result_summary": "没问题",
        "result_payload": {"outcome": "pass", "findings": []},
        **extra,
    }


async def test_rule_test_records_round_trip_filters_and_batch_delete() -> None:
    database_url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL")

    ids = {name: str(uuid4()) for name in ("tenant", "user", "ruleset", "rule")}
    code = f"rule_test_record_{uuid4().hex[:8]}"
    records = {name: str(uuid4()) for name in ("full", "orphan", "old_1", "old_2")}
    connection = await asyncpg.connect(database_url)
    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=2)
    repository = PostgresRuleTestRecordRepository(pool=pool)
    try:
        await _seed_rule(connection, ids, code)
        await repository.insert_record(
            _record(
                records["full"],
                rule_id=ids["rule"],
                rule_code=code,
                tester_id=ids["user"],
                visible_text="操作成功！",
                dom_html="<html><body>操作成功！</body></html>",
                ui_snapshot={"schema_version": "ui-snapshot.v2", "elements": [{"id": 1}]},
                judge_input={"discount_price": 9.9},
                second_location_text="开通月卡 ¥30",
                screenshots=[
                    {"filename": "a.png", "width": 390, "height": 844, "storage_path": "p/1.png"},
                    {"filename": "b.png", "width": 390, "height": 844, "storage_path": "p/2.png"},
                ],
                outcome="fail",
                result_summary="提示文字以叹号结尾",
                result_payload={"outcome": "fail", "findings": [{"summary": "叹号"}]},
            )
        )
        # 测试途中规则、账号被删了：记录照存，只是不挂关联
        await repository.insert_record(
            _record(
                records["orphan"],
                rule_id=str(uuid4()),
                rule_code=code,
                tester_id=str(uuid4()),
            )
        )
        for key in ("old_1", "old_2"):
            await repository.insert_record(
                _record(records[key], rule_id=ids["rule"], rule_code=code, tester_id=None)
            )
        await connection.execute(
            """
            UPDATE qc_rule_test_record
            SET created_at = CASE id
                WHEN $1::uuid THEN '2001-01-01T10:00:00Z'::timestamptz
                ELSE '2001-01-02T10:00:00Z'::timestamptz
            END
            WHERE id = ANY($2::uuid[])
            """,
            records["old_1"],
            [records["old_1"], records["old_2"]],
        )

        links = {
            str(row["id"]): (row["rule_id"], row["tester_user_id"])
            for row in await connection.fetch(
                "SELECT id, rule_id, tester_user_id FROM qc_rule_test_record WHERE rule_code = $1",
                code,
            )
        }
        assert str(links[records["full"]][0]) == ids["rule"]
        assert str(links[records["full"]][1]) == ids["user"]
        assert links[records["orphan"]] == (None, None)

        items, total = await repository.list_records(
            keyword=code.upper(), outcome=None, created_from=None, created_to=None,
            page=1, page_size=2,
        )
        assert total == 4
        # 新的在前；列表只给摘要
        assert {item["id"] for item in items} == {records["full"], records["orphan"]}
        full = next(item for item in items if item["id"] == records["full"])
        assert full["screenshot_count"] == 2
        assert (full["has_visible_text"], full["has_dom"]) == (True, True)
        assert (full["has_ui_snapshot"], full["has_judge_input"]) == (True, True)
        assert "dom_html" not in full

        failed, failed_total = await repository.list_records(
            keyword=code, outcome="fail", created_from=None, created_to=None, page=1, page_size=20,
        )
        assert [item["id"] for item in failed] == [records["full"]] and failed_total == 1
        in_range, range_total = await repository.list_records(
            keyword=code,
            outcome=None,
            created_from=datetime(2001, 1, 1, tzinfo=UTC),
            created_to=datetime(2001, 1, 2, tzinfo=UTC),
            page=1,
            page_size=20,
        )
        assert [item["id"] for item in in_range] == [records["old_1"]] and range_total == 1
        beyond, beyond_total = await repository.list_records(
            keyword=code, outcome=None, created_from=None, created_to=None, page=9, page_size=20,
        )
        assert beyond == [] and beyond_total == 4

        detail = await repository.get_record(records["full"])
        assert detail is not None
        assert detail["dom_html"].startswith("<html>")
        assert detail["ui_snapshot"]["elements"] == [{"id": 1}]
        assert detail["judge_input"] == {"discount_price": 9.9}
        assert detail["second_location_text"] == "开通月卡 ¥30"
        # 老调用方不传这一列时按空文字存
        orphan_detail = await repository.get_record(records["orphan"])
        assert orphan_detail is not None and orphan_detail["second_location_text"] == ""
        assert detail["result_payload"]["findings"] == [{"summary": "叹号"}]
        assert [item["storage_path"] for item in detail["screenshots"]] == ["p/1.png", "p/2.png"]
        assert await repository.get_record(str(uuid4())) is None
        assert await repository.get_screenshots(records["orphan"]) == []
        assert await repository.get_screenshots(str(uuid4())) is None

        by_time = await repository.delete_records(
            record_ids=[],
            created_from=datetime(2001, 1, 1, tzinfo=UTC),
            created_to=datetime(2001, 1, 3, tzinfo=UTC),
        )
        assert {row["id"] for row in by_time} == {records["old_1"], records["old_2"]}

        by_selection = await repository.delete_records(
            record_ids=[records["full"], str(uuid4())], created_from=None, created_to=None
        )
        assert [row["id"] for row in by_selection] == [records["full"]]
        assert [item["storage_path"] for item in by_selection[0]["screenshots"]] == [
            "p/1.png",
            "p/2.png",
        ]

        # 规则删了，记录留着、只断关联
        await connection.execute("DELETE FROM qc_rule WHERE id = $1::uuid", ids["rule"])
        remaining = await connection.fetch(
            "SELECT id::text AS id, rule_id FROM qc_rule_test_record WHERE rule_code = $1", code
        )
        assert [(row["id"], row["rule_id"]) for row in remaining] == [(records["orphan"], None)]
    finally:
        await connection.execute(
            "DELETE FROM qc_rule_test_record WHERE id = ANY($1::uuid[])", list(records.values())
        )
        await connection.execute("DELETE FROM qc_rule WHERE id = $1::uuid", ids["rule"])
        await connection.execute("DELETE FROM qc_ruleset WHERE id = $1::uuid", ids["ruleset"])
        await connection.execute("DELETE FROM qc_user WHERE id = $1::uuid", ids["user"])
        await connection.execute("DELETE FROM qc_tenant WHERE id = $1::uuid", ids["tenant"])
        await pool.close()
        await connection.close()


async def _seed_rule(connection: asyncpg.Connection, ids: dict[str, str], code: str) -> None:
    await connection.execute(
        "INSERT INTO qc_tenant (id, tenant_key, display_name) VALUES ($1::uuid, $2, '测试记录')",
        ids["tenant"],
        f"rule-test-record-{ids['tenant']}",
    )
    await connection.execute(
        """
        INSERT INTO qc_user (id, username, full_name, password_hash, role, tenant_id)
        VALUES ($1::uuid, $2, '规则测试记录', 'not-a-login-secret', '平台管理员', $3::uuid)
        """,
        ids["user"],
        f"rule-test-record-{ids['user']}",
        ids["tenant"],
    )
    await connection.execute(
        """
        INSERT INTO qc_ruleset (
            id, ruleset_code, name, version, status, snapshot_hash, rule_count, source_type
        )
        VALUES ($1::uuid, $2, '规则测试记录', 1, 'draft', 'snapshot-1', 1, 'manual')
        """,
        ids["ruleset"],
        f"rule-test-record-{ids['ruleset']}",
    )
    await connection.execute(
        """
        INSERT INTO qc_rule (
            id, ruleset_id, rule_code, name, category, executor_type, severity, status, check_point
        )
        VALUES ($1::uuid, $2::uuid, $3, '叹号结尾', 'visual', 'hybrid', 'medium', 'draft', '检查')
        """,
        ids["rule"],
        ids["ruleset"],
        code,
    )
