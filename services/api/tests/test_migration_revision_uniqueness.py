"""合并迁移时先拒绝重复编号，不能让 Alembic 静默覆盖另一条保护链。"""

import ast
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory


def duplicate_revisions(sources):
    revisions = {}
    for path, source in sources.items():
        # 与 Python 模块加载一致地接受 UTF-8 BOM，不改写历史迁移正文。
        for node in ast.parse(source.encode("utf-8")).body:
            if isinstance(node, ast.Assign):
                names = [target.id for target in node.targets if isinstance(target, ast.Name)]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names = [node.target.id]
            else:
                continue
            if "revision" in names:
                revision = ast.literal_eval(node.value)
                assert isinstance(revision, str) and revision.strip(), path
                revisions.setdefault(revision, []).append(path)
    return {revision: paths for revision, paths in revisions.items() if len(paths) > 1}


def test_repository_migration_revisions_are_unique():
    root = Path(__file__).resolve().parents[1] / "migrations" / "versions"
    sources = {path.name: path.read_text(encoding="utf-8") for path in sorted(root.glob("*.py"))}
    assert duplicate_revisions(sources) == {}, "同编号迁移必须先解决归属与已应用兼容性"


def test_different_revisions_can_share_parent_without_shadowing():
    assert duplicate_revisions({
        "h5.py": 'revision = "h5-a"\ndown_revision = "base"',
        "app.py": 'revision: str = "app-a"\ndown_revision = "base"',
    }) == {}


def test_equal_revision_with_different_body_is_a_conflict():
    assert duplicate_revisions({
        "h5.py": 'revision = "same"\ndef upgrade(): pass',
        "app.py": 'revision = "same"\ndef upgrade(): return 1',
    }) == {"same": ["h5.py", "app.py"]}


def test_utf8_bom_does_not_hide_revision_collision():
    assert duplicate_revisions({
        "old.py": '\ufeffrevision = "same"',
        "new.py": 'revision = "same"',
    }) == {"same": ["old.py", "new.py"]}


def test_integration_has_one_head_and_retains_both_protection_chains():
    scripts = ScriptDirectory.from_config(Config("services/api/migrations/alembic.ini"))
    assert scripts.get_heads() == ["20261023_0146"]
    chain = {item.revision for item in scripts.walk_revisions()}
    assert {
        "20261010_0132", "20261014_0133", "20261014_0134",
        "20261016_0135", "20261016_0136", "20261016_0137", "20261016_0138",
        "20261016_0139", "20261016_0140", "20261017_0141", "20261019_0142",
        "20261022_0143", "20261022_0144", "20261023_0145",
        "20261023_0146",
    } <= chain


class LegacyConnection:
    def __init__(self, revision, columns):
        self.revision = revision
        self.columns = columns
        self.queries = []

    def exec_driver_sql(self, sql):
        self.queries.append(sql)
        if "to_regclass" in sql:
            return self
        if "version_num" in sql:
            return self
        assert "information_schema.columns" in sql
        return self

    def scalar(self):
        return "alembic_version" if self.revision else None

    def fetchall(self):
        if "version_num" in self.queries[-1]:
            return [(self.revision,)]
        return [(name,) for name in self.columns]


@pytest.mark.parametrize("revision", ["20261014_0133", "20261014_0134"])
def test_legacy_app_number_without_h5_schema_is_rejected_before_upgrade(revision):
    from services.api.migrations.legacy_h5_identity import validate_legacy_h5_identity

    connection = LegacyConnection(revision, ())
    with pytest.raises(RuntimeError, match="旧迁移编号与H5结构不符"):
        validate_legacy_h5_identity(connection)
    assert all(sql.lstrip().upper().startswith("SELECT") for sql in connection.queries)


@pytest.mark.parametrize("revision", [None, "20261010_0132"])
def test_new_or_pre_collision_database_does_not_require_h5_tables(revision):
    from services.api.migrations.legacy_h5_identity import validate_legacy_h5_identity

    connection = LegacyConnection(revision, ())
    validate_legacy_h5_identity(connection)
    assert not any("information_schema.columns" in sql for sql in connection.queries)


@pytest.mark.parametrize("revision", ["20261014_0133", "20261014_0134", "20261016_0138"])
def test_real_h5_legacy_schema_keeps_original_revision(revision):
    from services.api.migrations.legacy_h5_identity import validate_legacy_h5_identity

    connection = LegacyConnection(
        revision, ("token", "task_id", "session_id", "status", "drained_at", "physical_stop_proof"),
    )
    validate_legacy_h5_identity(connection)
    assert connection.revision == revision


def test_stop_proof_revision_without_proof_column_is_rejected():
    from services.api.migrations.legacy_h5_identity import validate_legacy_h5_identity

    with pytest.raises(RuntimeError, match="旧迁移编号与H5结构不符"):
        validate_legacy_h5_identity(LegacyConnection(
            "20261014_0134", ("token", "task_id", "session_id", "status", "drained_at"),
        ))
