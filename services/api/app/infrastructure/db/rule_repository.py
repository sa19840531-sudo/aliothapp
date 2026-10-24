"""PostgreSQL 质检规则仓储实现。"""

from __future__ import annotations

import json
from typing import Any

from services.api.app.infrastructure.db.pool import get_postgres_pool


class PostgresRuleRepository:
    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def get_journey_definition(self, *, definition_id, version):
        from services.api.app.infrastructure.db.journey_definition_repository import (
            PostgresJourneyDefinitionRepository,
        )

        return await PostgresJourneyDefinitionRepository(pool=await self._get_pool()).get(
            definition_id=definition_id, version=version,
        )

    async def list_rulesets(
        self,
        *,
        keyword: str | None,
        status: str | None,
        page: int,
        page_size: int,
    ) -> tuple[list[dict[str, Any]], int]:
        pool = await self._get_pool()
        offset = max(page - 1, 0) * page_size
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                WITH filtered AS (
                    SELECT
                        id::text AS id,
                        ruleset_code,
                        name,
                        version,
                        status,
                        description,
                        source_type,
                        source_ref,
                        snapshot_hash,
                        rule_count,
                        page_sampling_policy,
                        loop_guard,
                        created_by::text AS created_by,
                        published_at,
                        archived_at,
                        created_at,
                        updated_at,
                        -- rule_count 是入库时的静态值；卡片展示用实时统计
                        (
                            SELECT count(*)
                            FROM qc_rule AS rule
                            WHERE rule.ruleset_id = qc_ruleset.id
                        )::int AS total_rule_count,
                        (
                            SELECT count(*)
                            FROM qc_rule AS rule
                            WHERE rule.ruleset_id = qc_ruleset.id AND rule.status = 'enabled'
                        )::int AS enabled_rule_count
                    FROM qc_ruleset
                    WHERE (
                        $1::varchar IS NULL
                        OR ruleset_code ILIKE ('%%' || $1 || '%%')
                        OR name ILIKE ('%%' || $1 || '%%')
                        OR COALESCE(description, '') ILIKE ('%%' || $1 || '%%')
                    )
                      AND ($2::varchar IS NULL OR status = $2)
                      -- 组合规则集是任务同时跑多个规则集时自动建的，不在规则配置里单独维护
                      AND member_ruleset_ids IS NULL
                )
                SELECT *, COUNT(*) OVER() AS total
                FROM filtered
                -- 按建立先后排：1 期、2 期、3 期瑞升……
                ORDER BY created_at ASC, ruleset_code ASC
                LIMIT $3 OFFSET $4
                """,
                keyword,
                status,
                page_size,
                offset,
            )
        total = int(rows[0]["total"]) if rows else 0
        return [_normalize_ruleset_row(dict(row)) for row in rows], total

    async def create_ruleset(self, payload: dict[str, Any]) -> dict[str, Any]:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO qc_ruleset (
                    ruleset_code,
                    name,
                    version,
                    status,
                    description,
                    source_type,
                    source_ref,
                    page_sampling_policy,
                    loop_guard
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::jsonb)
                RETURNING
                    id::text AS id,
                    ruleset_code,
                    name,
                    version,
                    status,
                    description,
                    source_type,
                    source_ref,
                    snapshot_hash,
                    rule_count,
                    page_sampling_policy,
                    loop_guard,
                    created_by::text AS created_by,
                    published_at,
                    archived_at,
                    created_at,
                    updated_at
                """,
                payload["ruleset_code"],
                payload["name"],
                payload["version"],
                payload["status"],
                payload.get("description") or None,
                payload["source_type"],
                payload.get("source_ref") or None,
                _json_dumps(payload.get("page_sampling_policy") or {}),
                _json_dumps(payload.get("loop_guard") or {}),
            )
        return _normalize_ruleset_row(dict(row))

    async def update_ruleset(
        self,
        ruleset_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE qc_ruleset
                SET ruleset_code = $2,
                    name = $3,
                    version = $4,
                    status = $5,
                    description = $6,
                    source_type = $7,
                    source_ref = $8,
                    page_sampling_policy = $9::jsonb,
                    loop_guard = $10::jsonb,
                    updated_at = now(),
                    published_at = CASE
                        WHEN $5 = 'enabled' AND published_at IS NULL THEN now()
                        ELSE published_at
                    END,
                    archived_at = CASE WHEN $5 = 'archived' THEN now() ELSE archived_at END
                WHERE id = $1::uuid
                RETURNING
                    id::text AS id,
                    ruleset_code,
                    name,
                    version,
                    status,
                    description,
                    source_type,
                    source_ref,
                    snapshot_hash,
                    rule_count,
                    page_sampling_policy,
                    loop_guard,
                    created_by::text AS created_by,
                    published_at,
                    archived_at,
                    created_at,
                    updated_at
                """,
                ruleset_id,
                payload["ruleset_code"],
                payload["name"],
                payload["version"],
                payload["status"],
                payload.get("description") or None,
                payload["source_type"],
                payload.get("source_ref") or None,
                _json_dumps(payload.get("page_sampling_policy") or {}),
                _json_dumps(payload.get("loop_guard") or {}),
            )
        return _normalize_ruleset_row(dict(row)) if row is not None else None

    async def get_ruleset(self, ruleset_id: str) -> dict[str, Any] | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT
                    id::text AS id,
                    ruleset_code,
                    name,
                    version,
                    status,
                    description,
                    source_type,
                    source_ref,
                    snapshot_hash,
                    rule_count,
                    page_sampling_policy,
                    loop_guard,
                    created_by::text AS created_by,
                    published_at,
                    archived_at,
                    created_at,
                    updated_at
                FROM qc_ruleset
                WHERE id = $1::uuid
                """,
                ruleset_id,
            )
        return _normalize_ruleset_row(dict(row)) if row is not None else None

    async def delete_ruleset(self, ruleset_id: str) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                "DELETE FROM qc_ruleset WHERE id = $1::uuid RETURNING id",
                ruleset_id,
            )
        return row is not None

    async def publish_ruleset(self, ruleset_id: str) -> dict[str, Any] | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE qc_ruleset
                SET status = 'enabled',
                    published_at = COALESCE(published_at, now()),
                    updated_at = now()
                WHERE id = $1::uuid
                RETURNING
                    id::text AS id,
                    ruleset_code,
                    name,
                    version,
                    status,
                    description,
                    source_type,
                    source_ref,
                    snapshot_hash,
                    rule_count,
                    page_sampling_policy,
                    loop_guard,
                    created_by::text AS created_by,
                    published_at,
                    archived_at,
                    created_at,
                    updated_at
                """,
                ruleset_id,
            )
        return _normalize_ruleset_row(dict(row)) if row is not None else None

    async def list_rules(
        self,
        *,
        ruleset_id: str | None,
        keyword: str | None,
        category: str | None,
        status: str | None,
        page: int,
        page_size: int,
        severity: str | None = None,
        source_type: str | None = None,
        page_type: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        pool = await self._get_pool()
        offset = max(page - 1, 0) * page_size
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                WITH filtered AS (
                    SELECT
                        rule.id::text AS id,
                        rule.ruleset_id::text AS ruleset_id,
                        rule.rule_code,
                        rule.name,
                        rule.description,
                        rule.category,
                        rule.executor_type,
                        rule.severity,
                        rule.status,
                        rule.scope,
                        rule.channel,
                        rule.page_type,
                        rule.applicable_page_types,
                        rule.url_pattern,
                        rule.steps,
                        rule.input_contract,
                        rule.check_point,
                        rule.judge_config,
                        rule.manual_review_policy,
                        rule.sort_order,
                        rule.created_by::text AS created_by,
                        rule.created_at,
                        rule.updated_at,
                        ruleset.ruleset_code,
                        ruleset.name AS ruleset_name,
                        ruleset.version AS ruleset_version,
                        ruleset.source_type AS source_type
                    FROM qc_rule AS rule
                    JOIN qc_ruleset AS ruleset ON ruleset.id = rule.ruleset_id
                    -- 不传规则集 = 跨全部规则集（配置中心「全部规则集」筛选）
                    WHERE ($1::uuid IS NULL OR rule.ruleset_id = $1::uuid)
                      AND (
                          $2::varchar IS NULL
                          OR rule.rule_code ILIKE ('%%' || $2 || '%%')
                          OR rule.name ILIKE ('%%' || $2 || '%%')
                          OR COALESCE(rule.check_point, '') ILIKE ('%%' || $2 || '%%')
                      )
                      AND ($3::varchar IS NULL OR rule.category = $3)
                      AND ($4::varchar IS NULL OR rule.status = $4)
                      AND ($7::varchar IS NULL OR rule.severity = $7)
                      AND ($8::varchar IS NULL OR ruleset.source_type = $8)
                      AND (
                          $9::varchar IS NULL
                          OR rule.applicable_page_types ? $9
                          OR (rule.applicable_page_types = '[]'::jsonb AND rule.page_type = $9)
                      )
                )
                SELECT *, COUNT(*) OVER() AS total
                FROM filtered
                ORDER BY sort_order ASC, updated_at DESC
                LIMIT $5 OFFSET $6
                """,
                ruleset_id,
                keyword,
                category,
                status,
                page_size,
                offset,
                severity,
                source_type,
                page_type,
            )
        total = int(rows[0]["total"]) if rows else 0
        return [_normalize_rule_row(dict(row)) for row in rows], total

    async def create_rule(self, payload: dict[str, Any]) -> dict[str, Any]:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO qc_rule (
                    ruleset_id,
                    rule_code,
                    name,
                    description,
                    category,
                    executor_type,
                    severity,
                    status,
                    scope,
                    channel,
                    page_type,
                    applicable_page_types,
                    url_pattern,
                    steps,
                    input_contract,
                    check_point,
                    judge_config,
                    manual_review_policy,
                    sort_order
                )
                VALUES (
                    $1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                    $11, $12::jsonb, $13, $14::jsonb, $15::jsonb,
                    $16, $17::jsonb, $18::jsonb, $19
                )
                RETURNING
                    id::text AS id,
                    ruleset_id::text AS ruleset_id,
                    rule_code,
                    name,
                    description,
                    category,
                    executor_type,
                    severity,
                    status,
                    scope,
                    channel,
                    page_type,
                    applicable_page_types,
                    url_pattern,
                    steps,
                    input_contract,
                    check_point,
                    judge_config,
                    manual_review_policy,
                    sort_order,
                    created_by::text AS created_by,
                    created_at,
                    updated_at
                """,
                payload["ruleset_id"],
                payload["rule_code"],
                payload["name"],
                payload.get("description") or None,
                payload["category"],
                payload["executor_type"],
                payload["severity"],
                payload["status"],
                payload.get("scope") or None,
                payload.get("channel") or None,
                payload.get("page_type") or None,
                _json_dumps(payload.get("applicable_page_types") or []),
                payload.get("url_pattern") or None,
                _json_dumps(payload.get("steps") or []),
                _json_dumps(payload.get("input_contract") or {}),
                payload["check_point"],
                _json_dumps(payload.get("judge_config") or {}),
                _json_dumps(payload.get("manual_review_policy") or {}),
                payload.get("sort_order") if payload.get("sort_order") is not None else 100,
            )
        return _normalize_rule_row(dict(row))

    async def update_rule(self, rule_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE qc_rule
                SET ruleset_id = $2::uuid,
                    rule_code = $3,
                    name = $4,
                    description = $5,
                    category = $6,
                    executor_type = $7,
                    severity = $8,
                    status = $9,
                    scope = $10,
                    channel = $11,
                    page_type = $12,
                    applicable_page_types = $13::jsonb,
                    url_pattern = $14,
                    steps = $15::jsonb,
                    input_contract = $16::jsonb,
                    check_point = $17,
                    judge_config = $18::jsonb,
                    manual_review_policy = $19::jsonb,
                    sort_order = $20,
                    updated_at = now()
                WHERE id = $1::uuid
                RETURNING
                    id::text AS id,
                    ruleset_id::text AS ruleset_id,
                    rule_code,
                    name,
                    description,
                    category,
                    executor_type,
                    severity,
                    status,
                    scope,
                    channel,
                    page_type,
                    applicable_page_types,
                    url_pattern,
                    steps,
                    input_contract,
                    check_point,
                    judge_config,
                    manual_review_policy,
                    sort_order,
                    created_by::text AS created_by,
                    created_at,
                    updated_at
                """,
                rule_id,
                payload["ruleset_id"],
                payload["rule_code"],
                payload["name"],
                payload.get("description") or None,
                payload["category"],
                payload["executor_type"],
                payload["severity"],
                payload["status"],
                payload.get("scope") or None,
                payload.get("channel") or None,
                payload.get("page_type") or None,
                _json_dumps(payload.get("applicable_page_types") or []),
                payload.get("url_pattern") or None,
                _json_dumps(payload.get("steps") or []),
                _json_dumps(payload.get("input_contract") or {}),
                payload["check_point"],
                _json_dumps(payload.get("judge_config") or {}),
                _json_dumps(payload.get("manual_review_policy") or {}),
                payload.get("sort_order") if payload.get("sort_order") is not None else 100,
            )
        return _normalize_rule_row(dict(row)) if row is not None else None

    async def get_rule(self, rule_id: str) -> dict[str, Any] | None:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT
                    id::text AS id,
                    ruleset_id::text AS ruleset_id,
                    rule_code,
                    name,
                    description,
                    category,
                    executor_type,
                    severity,
                    status,
                    scope,
                    channel,
                    page_type,
                    applicable_page_types,
                    url_pattern,
                    steps,
                    input_contract,
                    check_point,
                    judge_config,
                    manual_review_policy,
                    sort_order,
                    created_by::text AS created_by,
                    created_at,
                    updated_at
                FROM qc_rule
                WHERE id = $1::uuid
                """,
                rule_id,
            )
        return _normalize_rule_row(dict(row)) if row is not None else None

    async def list_rules_for_sandbox(self) -> list[dict[str, Any]]:
        """规则测试下拉：未归档规则集下的全部未归档规则（草稿、停用也列出来供测试）。"""
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT
                    rule.id::text AS id,
                    rule.ruleset_id::text AS ruleset_id,
                    rule.rule_code,
                    rule.name,
                    rule.category,
                    rule.executor_type,
                    rule.severity,
                    rule.status,
                    rule.applicable_page_types,
                    rule.url_pattern,
                    rule.steps,
                    rule.input_contract,
                    rule.check_point,
                    rule.judge_config,
                    rule.manual_review_policy,
                    ruleset.ruleset_code,
                    ruleset.name AS ruleset_name,
                    ruleset.version AS ruleset_version,
                    ruleset.status AS ruleset_status
                FROM qc_rule AS rule
                JOIN qc_ruleset AS ruleset ON ruleset.id = rule.ruleset_id
                WHERE ruleset.status <> 'archived'
                  AND rule.status <> 'archived'
                ORDER BY
                    (ruleset.status = 'enabled') DESC,
                    ruleset.ruleset_code ASC,
                    ruleset.version DESC,
                    rule.sort_order ASC,
                    rule.rule_code ASC
                """
            )
        return [_normalize_rule_row(dict(row)) for row in rows]

    async def delete_rule(self, rule_id: str) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                "DELETE FROM qc_rule WHERE id = $1::uuid RETURNING id",
                rule_id,
            )
        return row is not None

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _normalize_ruleset_row(row: dict[str, Any]) -> dict[str, Any]:
    row["page_sampling_policy"] = _json_object_or_default(row.get("page_sampling_policy"), {})
    row["loop_guard"] = _json_object_or_default(row.get("loop_guard"), {})
    return row


def _normalize_rule_row(row: dict[str, Any]) -> dict[str, Any]:
    row["applicable_page_types"] = _json_array_or_default(row.get("applicable_page_types"), [])
    row["steps"] = _json_array_or_default(row.get("steps"), [])
    row["input_contract"] = _json_object_or_default(row.get("input_contract"), {})
    row["judge_config"] = _json_object_or_default(row.get("judge_config"), {})
    row["manual_review_policy"] = _json_object_or_default(row.get("manual_review_policy"), {})
    return row


def _json_object_or_default(value: Any, default: dict[str, Any]) -> dict[str, Any]:
    if value is None:
        return default
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError:
            return default
        return loaded if isinstance(loaded, dict) else default
    return default


def _json_array_or_default(value: Any, default: list[Any]) -> list[Any]:
    if value is None:
        return default
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError:
            return default
        return loaded if isinstance(loaded, list) else default
    return default
