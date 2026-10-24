from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def _module():
    path = (
        Path(__file__).parents[1]
        / "migrations/versions/20260818_0066_app_inspection_publication.py"
    )
    spec = spec_from_file_location("app_inspection_publication_migration", path)
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_creates_app_publication_members_and_claim_indexes(monkeypatch) -> None:
    statements: list[str] = []
    module = _module()
    monkeypatch.setattr(module.op, "execute", statements.append)

    module.upgrade()

    sql = "\n".join(statements)
    assert "CREATE TABLE qc_app_inspection_publication (" in sql
    assert "CREATE TABLE qc_app_inspection_publication_entry (" in sql
    assert "CREATE TABLE qc_app_inspection_publication_rule (" in sql
    assert "CREATE TABLE qc_app_inspection_publication_work_item (" in sql
    assert "UNIQUE (task_id, evidence_index_id, finalization_epoch)" in sql
    assert "UNIQUE (publication_id, sequence_no)" in sql
    assert "UNIQUE (publication_id, entry_id)" in sql
    assert "FOREIGN KEY (publication_id, entry_id)" in sql
    assert "UNIQUE (publication_id, entry_id, work_item_id)" in sql
    assert "FOREIGN KEY (publication_id, entry_id, work_item_id)" in sql
    assert "disposition IN ('linked', 'not_applicable', 'needs_review')" in sql
    assert "idx_qc_app_publication_claim" in sql
    assert "idx_qc_app_publication_entry_status" in sql
    assert "idx_qc_app_publication_rule_disposition" in sql


def test_downgrade_guards_audit_rows_and_drops_children_first(monkeypatch) -> None:
    statements: list[str] = []
    module = _module()
    monkeypatch.setattr(module.op, "execute", statements.append)

    module.downgrade()

    sql = "\n".join(statements)
    assert "cannot downgrade App inspection publication" in sql
    assert sql.index("DROP TABLE qc_app_inspection_publication_rule") < sql.rindex(
        "DROP TABLE qc_app_inspection_publication"
    )
