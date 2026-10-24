"""导入平台规则集种子数据（1 期 / 2 期 / 3 期瑞升……）。

规则集 CSV 每行一个规则集，规则 CSV 用 ruleset_code 标明归属；每个规则集的快照哈希按它自己的
规则内容算。已有规则挪了规则集时（比如迁移之前就重播种子），按规则编码把旧行挪过去，不留重复行。
灌完后刷新组合规则集（任务同时跑多个规则集时自动建的）的快照哈希。
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import asyncpg
from dotenv import load_dotenv

from services.api.app.application.execution_ruleset import composite_snapshot_hash
from services.api.app.application.rule_judge import resolve_rule_execution_route

ROOT_DIR = Path(__file__).resolve().parents[1]
RULESET_CSV = ROOT_DIR / "docs/data/alioth_first_ruleset_qc_ruleset_seed.csv"
RULE_CSV = ROOT_DIR / "docs/data/alioth_first_ruleset_qc_rule_seed.csv"
APPLICABLE_PAGE_TYPES = (
    "line_loading",
    "home",
    "content_list",
    "content_detail",
    "video_list",
    "video_detail",
    "profile",
    "settings",
    "payment",
    "post_list",
    "post_publish",
    "activity",
    "startup_ad",
    "guide_page",
)
ALLOWED_APPLICABLE_PAGE_TYPES = frozenset(APPLICABLE_PAGE_TYPES)


def _database_url() -> str:
    load_dotenv(dotenv_path=ROOT_DIR / ".env")
    raw_url = os.getenv("ALIOTH_DATABASE_URL")
    if not raw_url:
        raise RuntimeError("缺少数据库连接配置：请设置 ALIOTH_DATABASE_URL")
    return raw_url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    if not rows:
        raise RuntimeError(f"{path} 未包含任何规则数据")
    return rows


def _validate_rule_rows(rule_rows: list[dict[str, str]]) -> None:
    for row in rule_rows:
        rule_code = row.get("rule_code") or "<unknown>"
        try:
            page_types = json.loads(row["applicable_page_types_json"])
        except (KeyError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"{rule_code} 的 applicable_page_types_json 不是有效 JSON") from exc

        if not isinstance(page_types, list) or not page_types:
            raise RuntimeError(f"{rule_code} 的 applicable_page_types_json 必须是非空数组")
        if not all(isinstance(page_type, str) for page_type in page_types):
            raise RuntimeError(f"{rule_code} 的 applicable_page_types_json 只能包含字符串")
        if len(page_types) != len(set(page_types)):
            raise RuntimeError(f"{rule_code} 的 applicable_page_types_json 不允许重复类型")

        unsupported_page_types = sorted(set(page_types) - ALLOWED_APPLICABLE_PAGE_TYPES)
        if unsupported_page_types:
            unsupported_text = ", ".join(unsupported_page_types)
            raise RuntimeError(
                f"{rule_code} 的 applicable_page_types_json 包含未支持类型：{unsupported_text}"
            )

        primary_page_type = row.get("page_type", "").strip()
        if primary_page_type and primary_page_type not in ALLOWED_APPLICABLE_PAGE_TYPES:
            raise RuntimeError(
                f"{rule_code} 的 page_type={primary_page_type} 不属于页面类型Catalog"
            )
        if primary_page_type and primary_page_type not in page_types:
            raise RuntimeError(
                f"{rule_code} 的 page_type={primary_page_type} 不在 applicable_page_types 中"
            )

        try:
            steps = json.loads(row.get("steps_json") or "[]")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{rule_code} 的 steps_json 不是有效 JSON") from exc
        for step in steps:
            expected_page_type = step.get("expected_page_type")
            if expected_page_type and expected_page_type not in ALLOWED_APPLICABLE_PAGE_TYPES:
                raise RuntimeError(
                    f"{rule_code} 的 expected_page_type={expected_page_type} 不属于页面类型Catalog"
                )

        executor_type = row.get("executor_type", "").strip()
        try:
            judge_config = json.loads(row.get("judge_config_json") or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{rule_code} 的 judge_config_json 不是有效 JSON") from exc
        route = resolve_rule_execution_route(
            executor_type=executor_type,
            judge_config=judge_config,
        )
        if not route.ready:
            reason = route.blocked_reason or {
                "code": "EXECUTION_ROUTE_NOT_READY",
                "message": "执行路由不可用",
            }
            raise RuntimeError(
                f"{rule_code} 的执行路由不可用[{reason['code']}]：{reason['message']}"
            )


def _json_text(row: dict[str, str], field: str) -> str:
    value = row[field]
    parsed: Any = json.loads(value)
    return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))


def _nullable_text(value: str) -> str | None:
    value = value.strip()
    return value or None


def _canonical_row(row: dict[str, str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in sorted(row):
        if key == "snapshot_hash":
            continue
        value = row[key].strip()
        if key.endswith("_json"):
            result[key] = json.loads(value)
        else:
            result[key] = value
    return result


def _prepare_ruleset_row(
    ruleset_row: dict[str, str],
    rule_rows: list[dict[str, str]],
) -> dict[str, str]:
    payload = {
        "ruleset": _canonical_row(ruleset_row),
        "rules": [
            _canonical_row(row)
            for row in sorted(
                rule_rows,
                key=lambda row: (int(row.get("sort_order") or 0), row.get("rule_code", "")),
            )
        ],
    }
    snapshot_text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    prepared_row = dict(ruleset_row)
    prepared_row["snapshot_hash"] = hashlib.sha256(snapshot_text.encode("utf-8")).hexdigest()
    return prepared_row


async def _ensure_schema(conn: asyncpg.Connection) -> None:
    exists = await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'qc_rule'
              AND column_name = 'applicable_page_types'
        )
        """
    )
    if not exists:
        raise RuntimeError("数据库缺少 qc_rule.applicable_page_types，请先执行 Alembic 迁移")


async def _upsert_ruleset(conn: asyncpg.Connection, row: dict[str, str]) -> str:
    return await conn.fetchval(
        """
        INSERT INTO qc_ruleset (
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
            loop_guard
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11::jsonb)
        ON CONFLICT (ruleset_code, version)
        DO UPDATE SET
            name = EXCLUDED.name,
            status = EXCLUDED.status,
            description = EXCLUDED.description,
            source_type = EXCLUDED.source_type,
            source_ref = EXCLUDED.source_ref,
            snapshot_hash = EXCLUDED.snapshot_hash,
            rule_count = EXCLUDED.rule_count,
            page_sampling_policy = EXCLUDED.page_sampling_policy,
            loop_guard = EXCLUDED.loop_guard,
            updated_at = now()
        RETURNING id
        """,
        row["ruleset_code"],
        row["name"],
        int(row["version"]),
        row["status"],
        _nullable_text(row["description"]),
        row["source_type"],
        _nullable_text(row["source_ref"]),
        row["snapshot_hash"],
        int(row["rule_count"]),
        _json_text(row, "page_sampling_policy_json"),
        _json_text(row, "loop_guard_json"),
    )


def _group_rules_by_ruleset(
    ruleset_rows: list[dict[str, str]],
    rule_rows: list[dict[str, str]],
) -> dict[tuple[str, str], list[dict[str, str]]]:
    """按 (ruleset_code, version) 分组；规则指向不存在的规则集、或规则数对不上都直接报错。"""

    groups: dict[tuple[str, str], list[dict[str, str]]] = {
        (row["ruleset_code"], str(row["version"])): [] for row in ruleset_rows
    }
    for row in rule_rows:
        key = (row.get("ruleset_code", ""), str(row.get("ruleset_version", "")))
        if key not in groups:
            raise RuntimeError(f"{row.get('rule_code')} 指向不存在的规则集：{key[0]} v{key[1]}")
        groups[key].append(row)
    for ruleset_row in ruleset_rows:
        key = (ruleset_row["ruleset_code"], str(ruleset_row["version"]))
        expected = int(ruleset_row["rule_count"])
        if len(groups[key]) != expected:
            raise RuntimeError(
                f"规则数量不一致：{key[0]} v{key[1]} rule_count={expected}, CSV={len(groups[key])}"
            )
    return groups


async def _claim_moved_rules(
    conn: asyncpg.Connection,
    ruleset_id: str,
    sibling_ruleset_ids: list[str],
    rule_codes: list[str],
) -> None:
    """规则换了规则集：把同批规则集里的旧行按规则编码挪到新规则集（rule.id 不变，历史判定照常）。"""

    await conn.execute(
        """
        UPDATE qc_rule AS rule
        SET ruleset_id = $1::uuid,
            updated_at = now()
        WHERE rule.rule_code = ANY($3::text[])
          AND rule.ruleset_id = ANY($2::uuid[])
          AND rule.ruleset_id <> $1::uuid
          AND NOT EXISTS (
              SELECT 1 FROM qc_rule AS existing
              WHERE existing.ruleset_id = $1::uuid
                AND existing.rule_code = rule.rule_code
          )
        """,
        ruleset_id,
        sibling_ruleset_ids,
        rule_codes,
    )


async def _refresh_composite_rulesets(conn: asyncpg.Connection) -> int:
    """成员规则集重播后快照变了，组合规则集的快照和规则数跟着更新。"""

    composites = await conn.fetch(
        """
        SELECT ruleset.id::text AS id, ruleset.member_ruleset_ids::text[] AS member_ids
        FROM qc_ruleset AS ruleset
        WHERE ruleset.member_ruleset_ids IS NOT NULL
        """
    )
    for composite in composites:
        members = await conn.fetch(
            """
            SELECT id::text AS id, snapshot_hash, rule_count
            FROM qc_ruleset
            WHERE id = ANY($1::uuid[])
            """,
            list(composite["member_ids"]),
        )
        await conn.execute(
            """
            UPDATE qc_ruleset
            SET snapshot_hash = $2,
                rule_count = $3
            WHERE id = $1::uuid
              AND (snapshot_hash IS DISTINCT FROM $2 OR rule_count <> $3)
            """,
            composite["id"],
            composite_snapshot_hash(
                [(member["id"], member["snapshot_hash"]) for member in members]
            ),
            sum(int(member["rule_count"] or 0) for member in members),
        )
    return len(composites)


async def _upsert_rule(
    conn: asyncpg.Connection,
    ruleset_id: str,
    row: dict[str, str],
) -> None:
    await conn.execute(
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
            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
            $11::jsonb, $12, $13::jsonb, $14::jsonb, $15,
            $16::jsonb, $17::jsonb, $18
        )
        ON CONFLICT (ruleset_id, rule_code)
        DO UPDATE SET
            name = EXCLUDED.name,
            description = EXCLUDED.description,
            category = EXCLUDED.category,
            executor_type = EXCLUDED.executor_type,
            severity = EXCLUDED.severity,
            -- 启停是运维态：compose up 重播种子不得覆盖既有行的 status，只有新行用种子默认值
            status = qc_rule.status,
            scope = EXCLUDED.scope,
            page_type = EXCLUDED.page_type,
            applicable_page_types = EXCLUDED.applicable_page_types,
            url_pattern = EXCLUDED.url_pattern,
            steps = EXCLUDED.steps,
            input_contract = EXCLUDED.input_contract,
            check_point = EXCLUDED.check_point,
            judge_config = EXCLUDED.judge_config,
            manual_review_policy = EXCLUDED.manual_review_policy,
            sort_order = EXCLUDED.sort_order,
            updated_at = now()
        """,
        ruleset_id,
        row["rule_code"],
        row["name"],
        _nullable_text(row["description"]),
        row["category"],
        row["executor_type"],
        row["severity"],
        row["status"],
        _nullable_text(row["scope"]),
        _nullable_text(row["page_type"]),
        _json_text(row, "applicable_page_types_json"),
        _nullable_text(row["url_pattern"]),
        _json_text(row, "steps_json"),
        _json_text(row, "input_contract_json"),
        row["check_point"],
        _json_text(row, "judge_config_json"),
        _json_text(row, "manual_review_policy_json"),
        int(row["sort_order"]),
    )


async def main() -> None:
    raw_ruleset_rows = _read_rows(RULESET_CSV)
    rule_rows = _read_rows(RULE_CSV)
    _validate_rule_rows(rule_rows)
    groups = _group_rules_by_ruleset(raw_ruleset_rows, rule_rows)
    ruleset_rows = [
        _prepare_ruleset_row(row, groups[(row["ruleset_code"], str(row["version"]))])
        for row in raw_ruleset_rows
    ]

    conn = await asyncpg.connect(_database_url())
    try:
        await _ensure_schema(conn)
        async with conn.transaction():
            ruleset_ids = [await _upsert_ruleset(conn, row) for row in ruleset_rows]
            sibling_ids = [str(ruleset_id) for ruleset_id in ruleset_ids]
            for ruleset_row, ruleset_id in zip(ruleset_rows, ruleset_ids, strict=True):
                rows = groups[(ruleset_row["ruleset_code"], str(ruleset_row["version"]))]
                await _claim_moved_rules(
                    conn, str(ruleset_id), sibling_ids, [row["rule_code"] for row in rows]
                )
                for rule_row in rows:
                    await _upsert_rule(conn, ruleset_id, rule_row)
            composite_count = await _refresh_composite_rulesets(conn)
    finally:
        await conn.close()

    for ruleset_row in ruleset_rows:
        key = (ruleset_row["ruleset_code"], str(ruleset_row["version"]))
        print(f"seeded ruleset={key[0]} version={key[1]} rules={len(groups[key])}")
    print(f"refreshed composite rulesets={composite_count}")


if __name__ == "__main__":
    asyncio.run(main())
