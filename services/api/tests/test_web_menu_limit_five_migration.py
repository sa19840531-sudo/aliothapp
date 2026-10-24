"""Web 菜单上限五个迁移的结构契约测试。"""

from pathlib import Path

MIGRATION = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260903_0088_web_menu_limit_five.py"
)


def test_web_menu_limit_five_migration_preserves_history_and_enforces_budget() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'revision = "20260903_0088"' in source
    assert 'down_revision = "20260903_0087"' in source
    assert "ALTER COLUMN menu_limit SET DEFAULT 5" in source
    assert "CHECK (menu_limit IN (5, 10))" in source
    assert "NEW.execution_slot > configured_menu_limit" in source
    assert "trg_qc_web_menu_execution_slot_budget" in source
    assert "DROP FUNCTION IF EXISTS qc_enforce_web_menu_execution_slot()" in source
    assert "SET menu_limit = 10" in source
