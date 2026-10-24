"""投递处置审计迁移与 Completion Gate 失败关闭合同测试。"""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MIGRATION = (
    ROOT
    / "services/api/migrations/versions/20260818_0071_delivery_operation_audit.py"
)
FINALIZATION_REPOSITORY = (
    ROOT
    / "services/api/app/infrastructure/db/task_finalization_repository.py"
)
DELIVERY_REPOSITORY = (
    ROOT
    / "services/api/app/infrastructure/db/delivery_operation_repository.py"
)
OUTBOX_REPOSITORY = ROOT / "services/api/app/infrastructure/db/outbox_repository.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location("delivery_operation_audit", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_extends_current_head_and_freezes_audit_rows() -> None:
    migration = _load_migration()
    source = MIGRATION.read_text(encoding="utf-8")

    assert migration.revision == "20260818_0071"
    assert migration.down_revision == "20260818_0070"
    assert "CREATE TABLE qc_delivery_operation_audit" in source
    assert "operator_user_id uuid NOT NULL REFERENCES qc_user" in source
    assert "expected_row_version bigint NOT NULL" in source
    assert "before_state jsonb NOT NULL" in source
    assert "after_state jsonb NOT NULL" in source
    assert "trg_qc_delivery_operation_audit_immutable" in source
    assert "BEFORE UPDATE OR DELETE" in source
    assert "cannot downgrade delivery operation audit while audit rows exist" in source


def test_replay_resets_outbox_and_inbox_atomically_and_excludes_supplement() -> None:
    source = DELIVERY_REPOSITORY.read_text(encoding="utf-8")
    sweeper_source = OUTBOX_REPOSITORY.read_text(encoding="utf-8")

    assert "WITH current_aggregates AS" in source
    assert "reset_inbox AS" in source
    assert "reset_outbox AS" in source
    assert "operator_user_id" in source
    assert "source_valid" in source
    assert "expected_row_version" in source
    assert "evidence-supplement" not in source
    assert "reset_dead_inbox AS" in sweeper_source
    assert "inbox.status = 'dead_letter'" in sweeper_source


def test_mark_needs_review_is_explicitly_understood_by_completion_gate() -> None:
    source = FINALIZATION_REPOSITORY.read_text(encoding="utf-8")

    assert "qc_delivery_operation_audit" in source
    assert "operation.action = 'mark_needs_review'" in source
    assert "operation.result = 'applied'" in source
    assert "qc_h5_inspection_publication_work_item" in source
    assert "qc_app_inspection_publication_work_item" in source
