import asyncio
from pathlib import Path

import pytest

from scripts import seed_products


class RecordingConnection:
    """只记录 SQL 与参数，不连库。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    async def execute(self, sql: str, *args: object) -> None:
        self.calls.append((sql, args))


def test_product_seed_csv_is_well_formed() -> None:
    rows = seed_products._read_rows(seed_products.PRODUCT_CSV)

    # 当前锁定的产品种子资产：早期 93 条 + 战略 / 核心两张表新增 51 条。
    assert len(rows) == 144
    codes = [row["project_code"].strip() for row in rows]
    assert len(set(codes)) == len(rows)  # 项目编号唯一
    for row in rows:
        # H5 链接要么空着（只登记了 App / 未上线），要么是完整网址
        h5_url = (row.get("h5_url") or "").strip()
        assert not h5_url or h5_url.startswith(("http://", "https://")), row
        # 平台代号只能是平台认识的
        for code in seed_products._split_platforms(row.get("platforms")):
            assert code in seed_products.PLATFORM_CODES, row


def test_product_seed_csv_carries_sheet_departments() -> None:
    rows = {
        row["project_code"].strip(): row
        for row in seed_products._read_rows(seed_products.PRODUCT_CSV)
    }

    # 战略 / 核心两张表的部门、负责人、平台跟着进种子
    assert rows["TJ-004"]["department_name"] == "蝴蝶效应"
    # 表里只有落地页的 App 产品，H5 链接取自「战略产品产品类型识别」分页
    assert rows["TJ-004"]["h5_url"] == "https://p1.hlw.bet/"
    assert rows["TJ-004"]["platforms"] == "app|h5"
    assert rows["TJ-045"]["platforms"] == "app"  # 只有落地页、没有 H5 的 App 产品
    assert rows["TJ-002"]["department_name"] == "无极"
    assert rows["TJ-002"]["platforms"] == "app|h5"
    assert rows["TJ-011"]["platforms"] == "web"
    assert rows["JHA-102"]["owner_name"] == "运营一部付费1组乐林"
    # 早期演示产品没有部门，只有公司级以上账号能看到
    assert rows["SF51"]["department_name"] == ""
    departments = {row["department_name"] for row in rows.values()} - {""}
    assert departments == {
        "蝴蝶效应", "伊甸维度", "极乐引擎", "悦达", "瑞升", "经纬",
        "紫宸星宇", "星河无界", "无极", "万有引力", "领航",
    }


def test_product_seed_csv_contains_corrected_h5_links() -> None:
    rows = {
        row["project_code"].strip(): (row.get("h5_url") or "").strip()
        for row in seed_products._read_rows(seed_products.PRODUCT_CSV)
    }

    # 9 条排查后确认/修正的 H5 链接
    assert rows["SF51"] == "https://6dkwys8b.top/"
    assert rows["SF58"] == "https://34.80.100.152:2258"
    assert rows["SF20"] == "https://34.80.100.152:2220/movie"
    assert rows["SF55"] == "https://34.80.100.152:2255/"
    assert rows["SF72"] == "https://35.185.152.172:2472"
    assert rows["SF69"] == "https://35.185.152.172:2469"
    assert rows["SF75"] == "https://tknfvnxx.7sqqwhwr.top/product"
    assert rows["SF50"] == "https://soul03.vip/"
    assert rows["SF126"] == "https://35.229.238.103:2015/"


def test_read_rows_rejects_unknown_platform(tmp_path: Path) -> None:
    csv_path = tmp_path / "seed.csv"
    csv_path.write_text(
        "project_code,product_name,h5_url,department_name,owner_name,platforms\nX-1,产品,,,,pc\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="未知平台"):
        seed_products._read_rows(csv_path)


def test_upsert_product_normalizes_blank_fields() -> None:
    conn = RecordingConnection()
    asyncio.run(
        seed_products._upsert_product(
            conn,
            {
                "project_code": " SF99 ",
                "product_name": " 测试 ",
                "h5_url": "  ",
                "department_name": " ",
                "owner_name": "",
                "platforms": "",
            },
        )
    )
    # 去空白 + 空串归一化为 None；没写平台又没有 H5 链接就是空列表
    assert conn.calls[0][1] == ("SF99", "测试", None, None, None, [])


def test_upsert_product_platforms() -> None:
    conn = RecordingConnection()
    asyncio.run(
        seed_products._upsert_product(
            conn,
            {
                "project_code": "SF98",
                "product_name": "测试",
                "h5_url": "https://example.com",
                "department_name": "悦达",
                "owner_name": "小明",
                "platforms": "",
            },
        )
    )
    # 没写平台但有 H5 链接的按 H5 算
    assert conn.calls[0][1] == ("SF98", "测试", "https://example.com", "悦达", "小明", ["h5"])

    conn = RecordingConnection()
    asyncio.run(
        seed_products._upsert_product(
            conn,
            {
                "project_code": "SF97",
                "product_name": "测试",
                "h5_url": "",
                "platforms": "app|H5|app",
            },
        )
    )
    # 「|」分隔、小写、去重保序
    assert conn.calls[0][1][5] == ["app", "h5"]


def test_ensure_departments_registers_sheet_departments_under_default_tenant() -> None:
    conn = RecordingConnection()
    rows = [
        {"department_name": "悦达"},
        {"department_name": " 无极 "},
        {"department_name": "悦达"},
        {"department_name": ""},
    ]

    count = asyncio.run(seed_products._ensure_departments(conn, rows))

    # 去重、去空白，登记到体验中心名下；已有的不动
    assert count == 2
    assert [call[1] for call in conn.calls] == [
        (seed_products.DEFAULT_TENANT_ID, "悦达"),
        (seed_products.DEFAULT_TENANT_ID, "无极"),
    ]
    assert all("ON CONFLICT (tenant_id, name) DO NOTHING" in call[0] for call in conn.calls)
