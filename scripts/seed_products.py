"""导入基础产品数据（项目编号、产品名称、H5链接、所属部门、负责人、平台）。

与 scripts/seed_first_ruleset.py 同一约定：迁移建表，本脚本按自然键幂等 upsert，
可在新环境初始化与每次部署时重复执行。人工在管理平台改过的 product_name / h5_url /
department_name / owner_name / platforms 会被 CSV 覆盖回基线；status 与 tenant_id
不在覆盖范围内：启停保留人工结果，种子产品一律是运营中心维护的共享产品。
CSV 里出现的部门名会同步登记到体验中心的「部门管理」（已有的不动），部门账号才能选到。
"""

from __future__ import annotations

import asyncio
import csv
import os
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

from services.api.app.infrastructure.db.department_repository import DEFAULT_TENANT_ID

ROOT_DIR = Path(__file__).resolve().parents[1]
PRODUCT_CSV = ROOT_DIR / "docs/data/alioth_products_seed.csv"
# 与管理平台产品资料一致的平台代号；CSV 的 platforms 列用「|」分隔
PLATFORM_CODES = ("h5", "web", "app", "ios")


def _database_url() -> str:
    load_dotenv(dotenv_path=ROOT_DIR / ".env")
    raw_url = os.getenv("ALIOTH_DATABASE_URL")
    if not raw_url:
        raise RuntimeError("缺少数据库连接配置：请设置 ALIOTH_DATABASE_URL")
    return raw_url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _split_platforms(raw: str | None) -> list[str]:
    """platforms 列：「|」分隔、去空白、小写、去重保序。"""
    result: list[str] = []
    for token in (raw or "").split("|"):
        code = token.strip().lower()
        if code and code not in result:
            result.append(code)
    return result


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as file:
        rows = list(csv.DictReader(file))
    if not rows:
        raise RuntimeError(f"{path} 未包含任何产品数据")

    seen: set[str] = set()
    for row in rows:
        code = (row.get("project_code") or "").strip()
        name = (row.get("product_name") or "").strip()
        if not code or not name:
            raise RuntimeError(f"存在缺失 project_code 或 product_name 的行：{row}")
        if code in seen:
            raise RuntimeError(f"project_code 重复：{code}")
        seen.add(code)
        platforms = _split_platforms(row.get("platforms"))
        unknown = [item for item in platforms if item not in PLATFORM_CODES]
        if unknown:
            raise RuntimeError(f"{code} 的 platforms 含未知平台：{unknown}")
    return rows


async def _ensure_schema(conn: asyncpg.Connection) -> None:
    exists = await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = 'qc_product'
        )
        """
    )
    if not exists:
        raise RuntimeError("数据库缺少 qc_product 表，请先执行 Alembic 迁移")


def _product_values(
    row: dict[str, str],
) -> tuple[str, str, str | None, str | None, str | None, list[str]]:
    """CSV 行转写库参数：去空白，空串归一化为 None；没写平台但有 H5 链接的按 H5 算。"""
    h5_url = (row.get("h5_url") or "").strip() or None
    platforms = _split_platforms(row.get("platforms"))
    if not platforms and h5_url:
        platforms = ["h5"]
    return (
        row["project_code"].strip(),
        row["product_name"].strip(),
        h5_url,
        (row.get("department_name") or "").strip() or None,
        (row.get("owner_name") or "").strip() or None,
        platforms,
    )


async def _upsert_product(conn: asyncpg.Connection, row: dict[str, str]) -> None:
    await conn.execute(
        """
        INSERT INTO qc_product (
            project_code, product_name, h5_url, department_name, owner_name, platforms
        )
        VALUES ($1, $2, $3, $4, $5, $6::text[])
        ON CONFLICT (project_code)
        DO UPDATE SET
            product_name = EXCLUDED.product_name,
            h5_url = EXCLUDED.h5_url,
            department_name = EXCLUDED.department_name,
            owner_name = EXCLUDED.owner_name,
            platforms = EXCLUDED.platforms,
            updated_at = now()
        """,
        *_product_values(row),
    )


async def _ensure_departments(conn: asyncpg.Connection, rows: list[dict[str, str]]) -> int:
    """CSV 里的部门名登记到体验中心的部门管理；已有的（含停用的）不动。"""
    names = sorted({(row.get("department_name") or "").strip() for row in rows} - {""})
    for name in names:
        await conn.execute(
            """
            INSERT INTO qc_department (tenant_id, name)
            VALUES ($1::uuid, $2)
            ON CONFLICT (tenant_id, name) DO NOTHING
            """,
            DEFAULT_TENANT_ID,
            name,
        )
    return len(names)


async def main() -> None:
    rows = _read_rows(PRODUCT_CSV)
    conn = await asyncpg.connect(_database_url())
    try:
        await _ensure_schema(conn)
        async with conn.transaction():
            for row in rows:
                await _upsert_product(conn, row)
            departments = await _ensure_departments(conn, rows)
    finally:
        await conn.close()

    print(f"seeded products={len(rows)} departments={departments} from {PRODUCT_CSV.name}")


if __name__ == "__main__":
    asyncio.run(main())
