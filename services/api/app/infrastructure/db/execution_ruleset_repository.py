"""把任务要跑的规则集落成一个执行规则集（单个用本身，多个找或建组合规则集）。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from services.api.app.application.execution_ruleset import (
    COMPOSITE_RULESET_SOURCE_TYPE,
    RulesetMember,
    TaskRulesetSelectionError,
    composite_ruleset_spec,
    normalize_ruleset_codes,
)


async def resolve_execution_ruleset_on_connection(
    connection: Any,
    ruleset_codes: Sequence[str] | None,
) -> tuple[str, int]:
    """返回任务要锁定的 (ruleset_code, version)；``ruleset_codes`` 为空表示全部启用规则集。"""

    requested = normalize_ruleset_codes(ruleset_codes)
    rows = await connection.fetch(
        """
        SELECT DISTINCT ON (ruleset.ruleset_code)
            ruleset.id::text AS ruleset_id,
            ruleset.ruleset_code,
            ruleset.version,
            ruleset.name,
            ruleset.snapshot_hash,
            ruleset.rule_count,
            ruleset.created_at
        FROM qc_ruleset AS ruleset
        WHERE ruleset.status = 'enabled'
          AND ruleset.member_ruleset_ids IS NULL
          AND ($1::text[] IS NULL OR ruleset.ruleset_code = ANY($1::text[]))
        ORDER BY ruleset.ruleset_code, ruleset.version DESC
        """,
        list(requested) if requested else None,
    )
    found = {str(row["ruleset_code"]) for row in rows}
    if requested:
        missing = [code for code in requested if code not in found]
        if missing:
            raise TaskRulesetSelectionError(f"规则集不存在或没有开启：{'、'.join(missing)}")
    if not rows:
        raise TaskRulesetSelectionError("没有开启的规则集，请先在规则配置里开启规则集")
    members = [
        RulesetMember(
            ruleset_id=str(row["ruleset_id"]),
            ruleset_code=str(row["ruleset_code"]),
            version=int(row["version"]),
            name=str(row["name"]),
            snapshot_hash=row["snapshot_hash"],
            rule_count=int(row["rule_count"] or 0),
        )
        for row in sorted(rows, key=lambda item: (item["created_at"], str(item["ruleset_code"])))
    ]
    if len(members) == 1:
        return members[0].ruleset_code, members[0].version

    member_ids = [member.ruleset_id for member in members]
    duplicated = await connection.fetch(
        """
        SELECT rule.rule_code
        FROM qc_rule AS rule
        WHERE rule.ruleset_id = ANY($1::uuid[])
          AND rule.status = 'enabled'
        GROUP BY rule.rule_code
        HAVING count(DISTINCT rule.ruleset_id) > 1
        ORDER BY rule.rule_code
        """,
        member_ids,
    )
    if duplicated:
        codes = "、".join(str(row["rule_code"]) for row in duplicated[:5])
        raise TaskRulesetSelectionError(f"所选规则集里有同一条规则（{codes}），不能一起跑")

    spec = composite_ruleset_spec(members)
    # 抽样与防死循环策略沿用排在最前的成员（1 期）；同样的组合复用同一行
    await connection.execute(
        """
        INSERT INTO qc_ruleset (
            ruleset_code, name, version, status, description, source_type, source_ref,
            snapshot_hash, rule_count, page_sampling_policy, loop_guard,
            member_ruleset_ids, published_at
        )
        SELECT
            $1, $2, 1, 'enabled', $3, $4, NULL,
            $5, $6, first.page_sampling_policy, first.loop_guard,
            $7::uuid[], now()
        FROM qc_ruleset AS first
        WHERE first.id = $8::uuid
        ON CONFLICT (ruleset_code, version) DO NOTHING
        """,
        spec.ruleset_code,
        spec.name,
        spec.description,
        COMPOSITE_RULESET_SOURCE_TYPE,
        spec.snapshot_hash,
        spec.rule_count,
        list(spec.member_ruleset_ids),
        members[0].ruleset_id,
    )
    # 成员重播种子后快照变了，组合跟着更新（在途任务按原来的规则集快照做漂移校验）
    await connection.execute(
        """
        UPDATE qc_ruleset
        SET name = $2,
            description = $3,
            snapshot_hash = $4,
            rule_count = $5,
            status = 'enabled'
        WHERE ruleset_code = $1
          AND version = 1
          AND (
              snapshot_hash IS DISTINCT FROM $4
              OR rule_count <> $5
              OR name <> $2
              OR status <> 'enabled'
          )
        """,
        spec.ruleset_code,
        spec.name,
        spec.description,
        spec.snapshot_hash,
        spec.rule_count,
    )
    return spec.ruleset_code, 1
