"""PostgreSQL 规则测试记录仓储（qc_rule_test_record）。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from services.api.app.infrastructure.db.pool import get_postgres_pool

# 列表只取摘要：源码、界面快照等大字段只在详情里读
_SUMMARY_COLUMNS = """
    id::text AS id,
    rule_id::text AS rule_id,
    rule_code,
    rule_name,
    tester_name,
    platform,
    page_url,
    page_title,
    outcome,
    result_summary,
    jsonb_array_length(screenshots) AS screenshot_count,
    octet_length(visible_text) > 0 AS has_visible_text,
    octet_length(dom_html) > 0 AS has_dom,
    ui_snapshot IS NOT NULL AS has_ui_snapshot,
    judge_input <> '{}'::jsonb AS has_judge_input,
    created_at
"""

_FILTERS = """
    ($1::text IS NULL OR rule_code ILIKE ('%' || $1 || '%') OR rule_name ILIKE ('%' || $1 || '%'))
    AND ($2::text IS NULL OR outcome = $2)
    AND ($3::timestamptz IS NULL OR created_at >= $3)
    AND ($4::timestamptz IS NULL OR created_at < $4)
"""


class PostgresRuleTestRecordRepository:
    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def insert_record(self, record: dict[str, Any]) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            await connection.execute(
                """
                INSERT INTO qc_rule_test_record (
                    id, rule_id, rule_code, rule_name, tester_user_id, tester_name,
                    platform, check_mode, page_type, page_state, page_url, page_title,
                    visible_text, dom_html, ui_snapshot, judge_input, screenshots,
                    outcome, result_summary, result_payload, second_location_text
                )
                VALUES (
                    $1::uuid,
                    -- 测试途中规则或账号被删了就不挂关联，编码、名字照样留着
                    (SELECT id FROM qc_rule WHERE id = $2::uuid),
                    $3, $4,
                    (SELECT id FROM qc_user WHERE id = $5::uuid),
                    $6, $7, $8, $9, $10, $11, $12, $13, $14,
                    $15::jsonb, $16::jsonb, $17::jsonb, $18, $19, $20::jsonb, $21
                )
                """,
                record["id"],
                record["rule_id"],
                record["rule_code"],
                record["rule_name"],
                record["tester_user_id"],
                record["tester_name"],
                record["platform"],
                record["check_mode"],
                record["page_type"],
                record["page_state"],
                record["page_url"],
                record["page_title"],
                record["visible_text"],
                record["dom_html"],
                _json_or_none(record["ui_snapshot"]),
                _json_dumps(record["judge_input"]),
                _json_dumps(record["screenshots"]),
                record["outcome"],
                record["result_summary"],
                _json_dumps(record["result_payload"]),
                record.get("second_location_text") or "",
            )

    async def list_records(
        self,
        *,
        keyword: str | None,
        outcome: str | None,
        created_from: datetime | None,
        created_to: datetime | None,
        page: int,
        page_size: int,
    ) -> tuple[list[dict[str, Any]], int]:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                f"""
                SELECT {_SUMMARY_COLUMNS}, COUNT(*) OVER() AS total
                FROM qc_rule_test_record
                WHERE {_FILTERS}
                ORDER BY created_at DESC, id DESC
                LIMIT $5 OFFSET $6
                """,
                keyword,
                outcome,
                created_from,
                created_to,
                page_size,
                max(page - 1, 0) * page_size,
            )
            if rows:
                total = int(rows[0]["total"])
            else:
                # 翻过了最后一页也要告诉页面总数
                total = int(
                    await connection.fetchval(
                        f"SELECT count(*) FROM qc_rule_test_record WHERE {_FILTERS}",
                        keyword,
                        outcome,
                        created_from,
                        created_to,
                    )
                )
        return [dict(row) for row in rows], total

    async def get_record(self, record_id: str) -> dict[str, Any] | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                SELECT {_SUMMARY_COLUMNS},
                    check_mode, page_type, page_state, visible_text, dom_html,
                    ui_snapshot, judge_input, screenshots, result_payload, second_location_text
                FROM qc_rule_test_record
                WHERE id = $1::uuid
                """,
                record_id,
            )
        if row is None:
            return None
        record = dict(row)
        for key in ("judge_input", "result_payload"):
            record[key] = _json_value(record[key]) or {}
        record["screenshots"] = _json_value(record["screenshots"]) or []
        record["ui_snapshot"] = _json_value(record["ui_snapshot"])
        return record

    async def get_screenshots(self, record_id: str) -> list[dict[str, Any]] | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            value = await connection.fetchval(
                "SELECT screenshots FROM qc_rule_test_record WHERE id = $1::uuid",
                record_id,
            )
        return None if value is None else list(_json_value(value) or [])

    async def delete_records(
        self,
        *,
        record_ids: list[str],
        created_from: datetime | None,
        created_to: datetime | None,
    ) -> list[dict[str, Any]]:
        """按勾选的记录或按时间段删除，返回删掉的记录（带截图存储路径，供删文件）。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            if record_ids:
                rows = await connection.fetch(
                    """
                    DELETE FROM qc_rule_test_record
                    WHERE id = ANY($1::uuid[])
                    RETURNING id::text AS id, screenshots
                    """,
                    record_ids,
                )
            else:
                rows = await connection.fetch(
                    """
                    DELETE FROM qc_rule_test_record
                    WHERE ($1::timestamptz IS NULL OR created_at >= $1)
                      AND ($2::timestamptz IS NULL OR created_at < $2)
                    RETURNING id::text AS id, screenshots
                    """,
                    created_from,
                    created_to,
                )
        return [
            {"id": row["id"], "screenshots": list(_json_value(row["screenshots"]) or [])}
            for row in rows
        ]

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _json_or_none(value: Any) -> str | None:
    return None if value is None else _json_dumps(value)


def _json_value(value: Any) -> Any:
    # 连接池未注册 jsonb 编解码时读回来是字符串
    return json.loads(value) if isinstance(value, str) else value
