"""App Capture 证据附件类型白名单迁移合同测试。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260820_0073_app_evidence_artifact_types.py"
)

_WEB_AND_RULE_ARTIFACT_TYPES = (
    "screenshot",
    "dom",
    "visible_text",
    "ocr_text",
    "har",
    "console_log",
    "media_state",
    "ai_io",
    "report",
    "action_trace",
    "ui_snapshot",
    "temporal_trace",
    "page_coverage",
    "ui_tree",
)
_APP_ARTIFACT_TYPES = (
    "view_hierarchy",
    "device_log",
    "network_trace",
    "fault_artifact",
)


class RecordingOperations:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(" ".join(statement.split()))


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "app_evidence_artifact_types_migration",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_preserves_web_rule_types_and_adds_full_app_contract() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.upgrade()

    sql = " ".join(operations.statements)
    assert migration.revision == "20260820_0073"
    assert migration.down_revision == "20260820_0072"
    assert "DROP CONSTRAINT IF EXISTS ck_qc_evidence_artifact_type" in sql
    for artifact_type in (*_WEB_AND_RULE_ARTIFACT_TYPES, *_APP_ARTIFACT_TYPES):
        assert f"'{artifact_type}'" in sql


def test_downgrade_refuses_extended_app_history_and_restores_previous_types() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.downgrade()

    sql = " ".join(operations.statements)
    assert (
        "artifact_type IN ('view_hierarchy', 'device_log', 'network_trace', 'fault_artifact')"
    ) in sql
    assert "cannot downgrade while extended App evidence artifacts exist" in sql
    assert "ADD CONSTRAINT ck_qc_evidence_artifact_type CHECK" in sql
    for artifact_type in _WEB_AND_RULE_ARTIFACT_TYPES:
        assert f"'{artifact_type}'" in sql
    for artifact_type in _APP_ARTIFACT_TYPES:
        # 新增 App 类型只应出现在降级前置检查，不应回到旧约束。
        assert sql.count(f"'{artifact_type}'") == 1
