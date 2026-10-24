"""工作台执行范围预览仓储（只读，聚合规则集内启用规则）。"""

from __future__ import annotations

import json
from typing import Any

from services.api.app.infrastructure.db.pool import get_postgres_pool


class PostgresWorkstationInspectionRepository:
    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def list_enabled_rules(
        self, *, ruleset_code: str, version: int
    ) -> list[dict[str, Any]]:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT
                    rule.rule_code,
                    rule.name,
                    rule.category,
                    rule.severity,
                    rule.applicable_page_types,
                    rule.sort_order
                FROM qc_rule rule
                JOIN qc_ruleset ruleset ON ruleset.id = rule.ruleset_id
                WHERE ruleset.ruleset_code = $1
                  AND ruleset.version = $2
                  AND rule.status = 'enabled'
                ORDER BY rule.sort_order, rule.rule_code
                """,
                ruleset_code,
                version,
            )
        return [
            {
                "rule_code": row["rule_code"],
                "name": row["name"],
                "category": row["category"],
                "severity": row["severity"],
                "applicable_page_types": _as_str_list(row["applicable_page_types"]),
            }
            for row in rows
        ]

    async def list_rules_in_scope(
        self, *, ruleset_code: str | None, version: int | None
    ) -> list[dict[str, Any]]:
        """指定了规则集就只看它的启用规则；没指定就看全部已发布规则（同一规则编码只算一次）。"""
        if ruleset_code:
            return await self.list_enabled_rules(ruleset_code=ruleset_code, version=version or 1)
        seen: set[str] = set()
        rules: list[dict[str, Any]] = []
        for rule in await self.list_published_rules():
            if rule["rule_code"] in seen:
                continue
            seen.add(rule["rule_code"])
            rules.append(rule)
        return rules

    async def list_published_rules(self) -> list[dict[str, Any]]:
        """已发布口径：启用规则集里的启用规则（规则中心、创建任务选择器共用）。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT
                    rule.id::text AS rule_id,
                    rule.rule_code,
                    rule.name,
                    rule.category,
                    rule.severity,
                    rule.executor_type,
                    rule.check_point,
                    rule.applicable_page_types,
                    rule.sort_order,
                    ruleset.id::text AS ruleset_id,
                    ruleset.source_type,
                    ruleset.ruleset_code,
                    ruleset.version AS ruleset_version
                FROM qc_rule rule
                JOIN qc_ruleset ruleset ON ruleset.id = rule.ruleset_id
                WHERE ruleset.status = 'enabled'
                  AND rule.status = 'enabled'
                ORDER BY ruleset.ruleset_code, ruleset.version DESC, rule.sort_order, rule.rule_code
                """
            )
        return [
            {
                **{key: row[key] for key in row.keys() if key != "applicable_page_types"},
                "applicable_page_types": _as_str_list(row["applicable_page_types"]),
            }
            for row in rows
        ]

    async def list_published_rulesets(self) -> list[dict[str, Any]]:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT
                    ruleset.id::text AS ruleset_id,
                    ruleset.ruleset_code,
                    ruleset.version,
                    ruleset.name,
                    COALESCE(ruleset.description, '') AS description,
                    ruleset.status
                FROM qc_ruleset ruleset
                WHERE ruleset.status = 'enabled'
                  -- 组合规则集是同时跑多个规则集时自动建的，不作为可选规则集展示
                  AND ruleset.member_ruleset_ids IS NULL
                ORDER BY ruleset.created_at, ruleset.ruleset_code, ruleset.version DESC
                """
            )
        return [dict(row) for row in rows]

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


def _as_str_list(value: Any) -> list[str]:
    """qc_rule.applicable_page_types 为 jsonb 数组，asyncpg 默认返回 JSON 文本。"""
    if value is None:
        return []
    if isinstance(value, str):
        parsed = json.loads(value)
    else:
        parsed = value
    if isinstance(parsed, list):
        return [str(item) for item in parsed]
    return []
