from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def _migration_source() -> str:
    path = (
        Path(__file__).parents[1]
        / "migrations/versions/20260823_0076_app_publication_failure_code_split.py"
    )
    return path.read_text(encoding="utf-8")


def test_app_publication_failure_code_migration_uses_persisted_rule_facts() -> None:
    source = _migration_source()

    assert 'revision = "20260823_0076"' in source
    assert 'down_revision = "20260822_0075"' in source
    assert "entry.failure_code = 'active_session_or_page_type_unavailable'" in source
    assert "member.session_dependency = 'active_session'" in source
    assert "member.disposition = 'needs_review'" in source
    assert "THEN 'active_session_rules_unresolved'" in source
    assert "ELSE 'page_type_unavailable'" in source


def test_app_publication_failure_code_migration_is_importable() -> None:
    path = (
        Path(__file__).parents[1]
        / "migrations/versions/20260823_0076_app_publication_failure_code_split.py"
    )
    spec = spec_from_file_location("app_publication_failure_code_split", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.revision == "20260823_0076"
    assert module.down_revision == "20260822_0075"
