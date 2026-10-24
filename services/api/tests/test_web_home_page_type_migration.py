"""五类协议迁移保留历史行并约束新首页基线。"""

import importlib.util
from pathlib import Path

import pytest


def _top_level_postgres_commands(sql: str) -> tuple[str, ...]:
    """按 PostgreSQL 顶层分号切分，忽略引号及 dollar-quoted 函数体。"""

    commands: list[str] = []
    start = 0
    index = 0
    quote: str | None = None
    dollar_tag: str | None = None
    line_comment = False
    block_comment = False
    while index < len(sql):
        if line_comment:
            if sql[index] == "\n":
                line_comment = False
            index += 1
            continue
        if block_comment:
            if sql.startswith("*/", index):
                block_comment = False
                index += 2
            else:
                index += 1
            continue
        if dollar_tag is not None:
            if sql.startswith(dollar_tag, index):
                index += len(dollar_tag)
                dollar_tag = None
            else:
                index += 1
            continue
        if quote is not None:
            if sql[index] == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if sql.startswith("--", index):
            line_comment = True
            index += 2
            continue
        if sql.startswith("/*", index):
            block_comment = True
            index += 2
            continue
        if sql[index] in {"'", '"'}:
            quote = sql[index]
            index += 1
            continue
        if sql[index] == "$":
            tag_end = sql.find("$", index + 1)
            if tag_end != -1:
                candidate = sql[index : tag_end + 1]
                tag_name = candidate[1:-1]
                if not tag_name or tag_name.replace("_", "a").isalnum():
                    dollar_tag = candidate
                    index = tag_end + 1
                    continue
        if sql[index] == ";":
            command = sql[start:index].strip()
            if command:
                commands.append(command)
            start = index + 1
        index += 1
    trailing = sql[start:].strip()
    if trailing:
        commands.append(trailing)
    return tuple(commands)


def test_migration_accepts_v3_and_preserves_legacy_rows():
    path = Path(__file__).parents[1] / "migrations/versions/20260914_0113_web_home_page_type.py"
    spec = importlib.util.spec_from_file_location("web_home_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    statements = []

    class Operations:
        def execute(self, sql):
            statements.append(sql)

    module.op = Operations()
    module.upgrade()
    sql = " ".join(statements)
    assert module.revision == "20260914_0113"
    assert module.down_revision == "20260912_0112"
    assert "web-page-type-assessment.v3" in sql
    assert "web-page-type-assessment.v2" in sql
    assert "web-page-type-normalization.v1" in sql
    assert "NEW.menu_page_type IS DISTINCT FROM 'home'" in sql
    assert "OLD.status = 'home_baseline'" in sql
    assert "BEFORE INSERT OR UPDATE" in sql
    statements.clear()
    module.downgrade()
    assert "web-page-type-assessment.v3" in " ".join(statements)


def test_applicability_constraint_accepts_v3_snapshot_protocol() -> None:
    path = (
        Path(__file__).parents[1]
        / "migrations/versions/20260914_0115_web_coverage_applicability_v3.py"
    )
    spec = importlib.util.spec_from_file_location("web_applicability_v3_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    statements: list[str] = []

    class Operations:
        def execute(self, sql: str) -> None:
            statements.append(sql)

    module.op = Operations()
    module.upgrade()
    sql = " ".join(statements)
    assert module.down_revision == "20260914_0114"
    assert "DROP CONSTRAINT IF EXISTS ck_qc_web_coverage_applicability_snapshot" in sql
    assert "web-coverage-snapshot.v2" in sql
    assert "web-coverage-snapshot.v3" in sql
    statements.clear()
    module.downgrade()
    assert "web-coverage-snapshot.v3" in statements[0]
    assert "web-coverage-snapshot.v3" not in statements[-1]
    assert "= 'web-coverage-snapshot.v2'" in statements[-1]


def test_web_v4_migration_upgrades_home_and_coverage_database_guards() -> None:
    """升级到 v4 时数据库门禁须与应用层冻结协议同步。"""

    path = (
        Path(__file__).parents[1]
        / "migrations/versions/20260914_0118_web_page_type_v4.py"
    )
    assert path.exists(), "缺少 Web v4 数据库协议迁移"
    spec = importlib.util.spec_from_file_location("web_page_type_v4_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    statements: list[str] = []

    class Operations:
        def execute(self, sql: str) -> None:
            statements.append(sql)

    module.op = Operations()
    module.upgrade()
    sql = " ".join(statements)
    assert module.revision == "20260914_0118"
    assert module.down_revision == "20260914_0117"
    assert "ck_qc_web_coverage_snapshot_normalization" in sql
    assert "web-page-type-assessment.v4" in sql
    assert "ck_qc_web_coverage_applicability_snapshot" in sql
    assert "web-coverage-snapshot.v4" in sql
    assert "CREATE OR REPLACE FUNCTION qc_web_require_formal_home_type" in sql
    assert "NEW.normalization_version IN (" in sql
    assert "assessment.schema_version=NEW.normalization_version" in sql
    assert all(len(_top_level_postgres_commands(item)) == 1 for item in statements)

    statements.clear()
    module.downgrade()
    downgrade_sql = " ".join(statements)
    assert "Cannot downgrade immutable Web v4 evidence" in downgrade_sql
    assert "web-page-type-assessment.v4" in downgrade_sql
    assert "web-coverage-snapshot.v4" in downgrade_sql
    assert all(len(_top_level_postgres_commands(item)) == 1 for item in statements)


def test_web_v4_success_evidence_migration_accepts_formal_home() -> None:
    """已发布 0112 后以新 revision 放行 v4 首页成功证据。"""

    path = (
        Path(__file__).parents[1]
        / "migrations/versions/20260914_0119_web_v4_success_evidence.py"
    )
    assert path.exists(), "缺少 Web v4 成功证据约束迁移"
    spec = importlib.util.spec_from_file_location("web_v4_success_evidence_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    statements: list[str] = []

    class Operations:
        def execute(self, sql: str) -> None:
            statements.append(sql)

    module.op = Operations()
    module.upgrade()
    sql = " ".join(statements)
    assert module.revision == "20260914_0119"
    assert module.down_revision == "20260914_0118"
    assert "DROP CONSTRAINT ck_qc_web_menu_success_evidence" in sql
    assert "'web-page-type-assessment.v3', 'web-page-type-assessment.v4'" in sql
    assert "AND menu_page_type = 'home'" in sql
    assert "'web-page-type-assessment.v2'" in sql
    assert "'content_list','video_list','content_detail','video_detail'" in sql
    assert all(len(_top_level_postgres_commands(item)) == 1 for item in statements)

    statements.clear()
    module.downgrade()
    downgrade_sql = " ".join(statements)
    assert "web-page-type-assessment.v4" in downgrade_sql
    assert "Cannot downgrade immutable Web v4 success evidence" in downgrade_sql
    assert all(len(_top_level_postgres_commands(item)) == 1 for item in statements)


@pytest.mark.parametrize(
    ("filename", "module_name"),
    (
        ("20260914_0113_web_home_page_type.py", "web_home_migration_commands"),
        ("20260914_0114_web_menu_lineage.py", "web_menu_migration_commands"),
        (
            "20260914_0115_web_coverage_applicability_v3.py",
            "web_applicability_migration_commands",
        ),
    ),
)
def test_migration_sends_one_top_level_postgres_command_per_execute(
    filename: str,
    module_name: str,
) -> None:
    """asyncpg prepared statements 禁止一次 execute 混入多个顶层命令。"""

    path = Path(__file__).parents[1] / "migrations/versions" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    statements: list[str] = []

    class Operations:
        def execute(self, sql: str) -> None:
            statements.append(sql)

    module.op = Operations()
    module.upgrade()
    assert all(len(_top_level_postgres_commands(sql)) == 1 for sql in statements)
    statements.clear()
    module.downgrade()
    assert all(len(_top_level_postgres_commands(sql)) == 1 for sql in statements)
