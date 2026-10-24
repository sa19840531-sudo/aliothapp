"""回填质检规则的展示元数据：所属规则包（rule_package）与触发条件文案（trigger_display）。

与 seed 脚本同一约定：迁移建列（20260717_0009），本脚本幂等回填，可重复执行。
默认只填 NULL（保留人工在库里覆写过的文案）；`--force` 时按兜底口径全量重写。
兜底口径与读侧一致（`services/api/app/application/task_progress.py`）：

- rule_package：按 category 映射（ad→广告质检 等）；
- trigger_display：按 applicable_page_types 生成"识别到XX页面时触发"，
  覆盖全部受支持页面类型时为"所有页面默认触发"。

产品侧后续若给出逐条规则的更细文案，直接改库或扩展本脚本的 OVERRIDES。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from services.api.app.application.task_progress import (  # noqa: E402
    rule_package_fallback,
    trigger_display_fallback,
)
from services.api.app.domain.page.catalog import (  # noqa: E402
    load_default_page_type_catalog,
)

# 逐条规则覆写位：key=rule_code，value=(rule_package, trigger_display)，None 表示沿用兜底。
OVERRIDES: dict[str, tuple[str | None, str | None]] = {}


def _database_url() -> str:
    load_dotenv(dotenv_path=ROOT_DIR / ".env")
    raw_url = os.getenv("ALIOTH_DATABASE_URL")
    if not raw_url:
        raise RuntimeError("缺少数据库连接配置：请设置 ALIOTH_DATABASE_URL")
    return raw_url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _backfill(*, force: bool) -> None:
    catalog = load_default_page_type_catalog()
    conn = await asyncpg.connect(_database_url())
    try:
        rows = await conn.fetch(
            """
            SELECT id, rule_code, category, applicable_page_types,
                   rule_package, trigger_display
            FROM qc_rule
            ORDER BY rule_code
            """
        )
        updated = 0
        for row in rows:
            override_package, override_trigger = OVERRIDES.get(
                row["rule_code"], (None, None)
            )
            applicable = _json_list(row["applicable_page_types"])
            package = override_package or rule_package_fallback(row["category"])
            trigger = override_trigger or trigger_display_fallback(
                applicable, catalog=catalog
            )
            new_package = package if force or row["rule_package"] is None else None
            new_trigger = trigger if force or row["trigger_display"] is None else None
            if new_package is None and new_trigger is None:
                continue
            await conn.execute(
                """
                UPDATE qc_rule
                SET
                    rule_package = COALESCE($2, rule_package),
                    trigger_display = COALESCE($3, trigger_display),
                    updated_at = now()
                WHERE id = $1
                """,
                row["id"],
                new_package,
                new_trigger,
            )
            updated += 1
        print(f"规则展示元数据回填完成：扫描 {len(rows)} 条，更新 {updated} 条。")
    finally:
        await conn.close()


def _json_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
    else:
        parsed = value
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="全量按兜底口径重写（默认只回填 NULL 列）",
    )
    args = parser.parse_args()
    asyncio.run(_backfill(force=args.force))
