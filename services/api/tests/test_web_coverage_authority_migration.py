"""Web 覆盖权威来源迁移合同测试。"""

from pathlib import Path

MIGRATION = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260901_0082_web_coverage_authority.py"
)


def test_web_coverage_authority_migration_extends_shared_snapshot_contract() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'down_revision = "20260831_0081"' in source
    assert "'web_page_type_assessment'" in source
    assert "DROP CONSTRAINT ck_qc_h5_coverage_authority" in source
    assert "H5 与 Web 页面类型评估" in source


def test_web_coverage_authority_downgrade_refuses_data_loss() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert "refusing destructive downgrade" in source
