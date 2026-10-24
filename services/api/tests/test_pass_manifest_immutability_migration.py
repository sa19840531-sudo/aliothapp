"""E25：pass 清单证据不可原地变更约束迁移测试。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260814_0056_pass_manifest_evidence_immutability.py"
)


class RecordingOperations:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(" ".join(statement.split()))


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "pass_manifest_immutability_migration", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _upgrade_sql() -> str:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations
    migration.upgrade()
    return " ".join(operations.statements)


def test_migration_follows_current_head() -> None:
    migration = _load_migration()

    assert migration.revision == "20260814_0056"
    assert migration.down_revision == "20260814_0055"


def test_artifact_guard_blocks_identity_change_under_passed_manifest() -> None:
    sql = _upgrade_sql()

    assert "CREATE OR REPLACE FUNCTION qc_guard_pass_manifest_artifact()" in sql
    assert "BEFORE UPDATE ON qc_evidence_artifact" in sql
    assert "manifest.quality_status = 'pass'" in sql
    # 通过清单载荷里的 artifact_id 判定引用关系
    assert "manifest.manifest_payload -> 'artifacts' @> jsonb_build_array(" in sql
    assert "jsonb_build_object('artifact_id', OLD.id::text)" in sql
    assert "is referenced by a passed manifest and is immutable" in sql


def test_artifact_guard_covers_every_identity_column() -> None:
    sql = _upgrade_sql()

    for column in (
        "task_id",
        "page_capture_id",
        "artifact_type",
        "storage_path",
        "checksum",
        "size_bytes",
    ):
        assert f"NEW.{column} IS DISTINCT FROM OLD.{column}" in sql


def test_artifact_guard_leaves_metadata_editable() -> None:
    """metadata 不是证据本体，运维标注不应被约束误伤。"""
    sql = _upgrade_sql()

    assert "NEW.metadata IS DISTINCT FROM OLD.metadata" not in sql


def test_manifest_guard_blocks_in_place_content_rewrite() -> None:
    sql = _upgrade_sql()

    assert "CREATE OR REPLACE FUNCTION qc_guard_evidence_manifest_immutability()" in sql
    assert "BEFORE UPDATE ON qc_evidence_manifest" in sql
    assert "publish a higher version instead" in sql
    for column in (
        "manifest_version",
        "manifest_hash",
        "payload_hash",
        "quality_status",
        "quality_reason",
        "manifest_payload",
    ):
        assert f"NEW.{column} IS DISTINCT FROM OLD.{column}" in sql


def test_manifest_guard_allows_idempotent_no_op_update() -> None:
    """幂等写入 ON CONFLICT DO UPDATE SET manifest_id = 自身不改内容列，必须放行。"""
    sql = _upgrade_sql()

    # 守卫只按内容列判定，manifest_id 不在拦截集合内
    assert "NEW.manifest_id IS DISTINCT FROM OLD.manifest_id" not in sql


def test_guards_do_not_block_delete_to_preserve_cascade_cleanup() -> None:
    """行删除由归属方级联生命周期治理，拦 DELETE 会打死 task 清理。"""
    sql = _upgrade_sql()

    assert "BEFORE UPDATE OR DELETE" not in sql
    assert "TG_OP = 'DELETE'" not in sql


def test_downgrade_drops_both_guards() -> None:
    migration = _load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.downgrade()

    sql = " ".join(operations.statements)
    assert "DROP TRIGGER IF EXISTS trg_qc_evidence_artifact_pass_manifest_guard" in sql
    assert "DROP TRIGGER IF EXISTS trg_qc_evidence_manifest_immutability_guard" in sql
    assert "DROP FUNCTION IF EXISTS qc_guard_pass_manifest_artifact()" in sql
    assert "DROP FUNCTION IF EXISTS qc_guard_evidence_manifest_immutability()" in sql
