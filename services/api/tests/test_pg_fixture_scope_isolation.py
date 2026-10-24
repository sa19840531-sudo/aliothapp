"""PG夹具只作用于对应模块，不能因缺测试库跳过无关单测。"""

import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest


@pytest.mark.parametrize("provider", [
    "test_h5_gate_coverage_postgres.py", "test_h5_formal_report_binding_postgres.py",
    "test_surface_session_close_postgres.py", "test_web_lifecycle_recovery_pg.py",
    "test_web_formal_terminal_ledger_pg.py",
    "test_web_sequence_seed_lineage_postgres.py",
    None,
])
def test_imported_pg_fixture_cannot_skip_unrelated_checks(provider, tmp_path):
    environment = dict(os.environ, ALIOTH_DATABASE_URL="", ALIOTH_LLM_API_KEY="test-only")
    environment.pop("ALIOTH_TEST_DATABASE_URL", None)
    output = tmp_path / "scope.xml"
    selected = [f"services/api/tests/{provider}",
         "services/api/tests/test_capability_manifest.py"
         "::test_default_manifest_matches_runtime_tools_and_skill_files",
         "services/api/tests/test_migration_revision_uniqueness.py"
         "::test_repository_migration_revisions_are_unique"] if provider else [
             "services/api/tests", "-k",
             "test_default_manifest_matches_runtime_tools_and_skill_files or "
             "test_repository_migration_revisions_are_unique",
         ]
    process = subprocess.run(
        [sys.executable, "-m", "pytest", *selected, "-q", f"--junitxml={output}"],
        cwd=Path(__file__).resolve().parents[3], env=environment,
        capture_output=True, text=True, timeout=60,
    )
    assert process.returncode == 0, process.stdout[-6000:] + process.stderr[-2000:]
    checks = [item for item in ET.parse(output).iter("testcase") if item.get("name") in {
        "test_default_manifest_matches_runtime_tools_and_skill_files",
        "test_repository_migration_revisions_are_unique",
    }]
    assert len(checks) == 2
    assert all(len(item) == 0 for item in checks), "普通单测必须实际执行，不能被PG全局插件跳过"
