"""Web V2 新任务恢复十菜单预算的迁移行为测试。"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path
from types import ModuleType

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "20260905_0090_web_menu_limit_ten.py"
)


class RecordingOp:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(" ".join(statement.split()))


def test_web_menu_limit_ten_migration_changes_only_the_default(monkeypatch) -> None:
    operation = RecordingOp()
    alembic = ModuleType("alembic")
    alembic.op = operation  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "alembic", alembic)

    migration = runpy.run_path(str(MIGRATION))
    assert migration["down_revision"] == "20260904_0089"

    migration["upgrade"]()
    assert operation.statements == [
        "ALTER TABLE qc_web_exploration_budget ALTER COLUMN menu_limit SET DEFAULT 10"
    ]

    operation.statements.clear()
    migration["downgrade"]()
    assert operation.statements == [
        "ALTER TABLE qc_web_exploration_budget ALTER COLUMN menu_limit SET DEFAULT 5"
    ]
