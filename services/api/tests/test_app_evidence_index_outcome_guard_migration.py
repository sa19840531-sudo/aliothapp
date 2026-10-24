from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260814_0051_app_evidence_index_outcome_guard.py"
)


class RecordingOperations:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(" ".join(statement.split()))


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "app_evidence_index_outcome_guard_migration", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_aligns_latest_manifest_and_outcome_guards() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.upgrade()

    sql = " ".join(operations.statements)
    assert migration.revision == "20260814_0051"
    assert migration.down_revision == "20260813_0050"
    assert "ADD COLUMN result_context_sha256 varchar(71)" in sql
    assert "新封存 Evidence Index 必须绑定探索结果上下文" in sql
    assert "qc_guard_app_evidence_index_entry_latest" in sql
    assert "trg_qc_app_evidence_index_entry_latest_guard" in sql
    assert "Capture 的最新 Manifest" in sql
    assert "exploration_completed 禁止携带 Evidence Gap" in sql
    assert "exploration_completed 要求所有最新 Manifest 均为 PASS" in sql
    assert "exploration_completed 缺少 Final Observation Entry" in sql
    assert "evidence_insufficient 的未索引 Capture 缺少对应 Evidence Gap" in sql
    assert "无 capture_id 的 Evidence Gap 必须声明 run scope" in sql
    assert "Evidence Gap 的 Capture 归属或 semantic_role 不一致" in sql
    assert "Evidence Index Entry 的 semantic_role 与 Capture 角色不一致" in sql
    assert "Evidence Index 头身份字段禁止修改" in sql
    assert "OLD.result_context_sha256 IS NULL" in sql
    assert "存在不符合 0050 合同的已封存 App Evidence Index" in sql
    assert "trg_qc_app_evidence_manifest_sealed_guard" in sql
    assert "BEFORE INSERT OR UPDATE OR DELETE ON qc_evidence_manifest" in sql
    assert "App Evidence Index 已封存，禁止追加、修改或删除 Manifest" in sql
    assert "trg_qc_app_evidence_capture_sealed_guard" in sql
    assert "BEFORE INSERT OR UPDATE OR DELETE ON qc_capture" in sql
    assert "App Evidence Index 已封存，禁止追加、修改或删除 Capture" in sql
    assert "session.surface_kind IN ('android', 'ios')" in sql
    assert "session.platform_build_ref IS NOT DISTINCT FROM NEW.build_id" in sql
    assert "trg_qc_app_evidence_session_sealed_guard" in sql
    assert "禁止修改 Session 证据身份" in sql
    assert "Evidence Index 已封存，禁止修改" in sql


def test_downgrade_guards_new_insufficient_facts_and_restores_legacy_header() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.downgrade()

    sql = " ".join(operations.statements)
    assert "evidence_index.outcome = 'evidence_insufficient'" not in sql
    assert "latest.quality_status IS DISTINCT FROM 'pass'" in sql
    assert "禁止降级到 0049" in sql
    assert "存在尚未 PASS 的最新 Manifest，禁止封存" in sql
    assert "DROP TRIGGER IF EXISTS trg_qc_app_evidence_index_entry_latest_guard" in sql
    assert "DROP FUNCTION IF EXISTS qc_guard_app_evidence_index_entry_latest()" in sql
    assert "DROP TRIGGER IF EXISTS trg_qc_app_evidence_manifest_sealed_guard" in sql
    assert "DROP FUNCTION IF EXISTS qc_guard_sealed_app_evidence_manifest()" in sql
    assert "DROP TRIGGER IF EXISTS trg_qc_app_evidence_session_sealed_guard" in sql
    assert "DROP FUNCTION IF EXISTS qc_guard_sealed_app_evidence_session()" in sql
    assert "旧哈希合同无法表达的 Index" in sql
    assert "DROP COLUMN result_context_sha256" in sql
