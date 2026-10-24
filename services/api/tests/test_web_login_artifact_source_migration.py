"""隔离登录附件 source 迁移不得改变既有证据源或删除历史。"""

import importlib.util
from pathlib import Path

from sqlalchemy import text

MIGRATION = (
    Path(__file__).parents[1] / "migrations" / "versions"
    / "20260930_0129_web_login_isolated_artifacts.py"
)


class Operations:
    def __init__(self):
        self.statements = []

    def execute(self, sql):
        self.statements.append(" ".join(sql.split()))


def _migration():
    spec = importlib.util.spec_from_file_location("web_login_artifact_migration", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations()
    return module


def test_upgrade_adds_only_web_login_source_and_bounded_partial_index():
    migration = _migration()
    migration.upgrade()
    sql = " ".join(migration.op.statements)
    assert migration.revision == "20260930_0129"
    assert migration.down_revision == "20260929_0128"
    for source in ("baseline", "rule_supplement", "ai_call", "report", "app_runtime"):
        assert f"'{source}'" in sql
    assert "'web_login_isolated'" in sql
    assert "page_capture_id IS NULL" in sql
    assert "artifact_type IN ('screenshot', 'dom')" in sql
    assert "CREATE UNIQUE INDEX" in sql
    assert "metadata->>'source_capture_id'" in sql
    assert "metadata->>'isolated_session_id'" in sql
    assert "metadata->>'step'" in sql
    assert "IS TRUE" in sql
    assert "WHERE source = 'web_login_isolated'" in sql
    assert all(not text(statement)._bindparams for statement in migration.op.statements)


def test_downgrade_blocks_when_login_artifacts_exist():
    migration = _migration()
    migration.downgrade()
    sql = " ".join(migration.op.statements)
    assert "WEB_LOGIN_ARTIFACT_DOWNGRADE_BLOCKED" in sql
    assert "WHERE source = 'web_login_isolated'" in sql
    assert "DELETE FROM qc_evidence_artifact" not in sql
