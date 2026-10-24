"""Web 正式报告生成审计迁移的静态契约。"""

from __future__ import annotations

import runpy
from pathlib import Path

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "20260903_0087_web_report_generation_attempt.py"
)


def test_web_report_generation_attempt_migration_extends_single_head() -> None:
    migration = runpy.run_path(str(MIGRATION))

    assert migration["revision"] == "20260903_0087"
    assert migration["down_revision"] == "20260903_0086"
    source = MIGRATION.read_text(encoding="utf-8")
    assert "CREATE TABLE qc_web_report_generation_attempt" in source
    assert "finalization_epoch" in source
    assert "attempt_number" in source
    assert "started_at" in source
    assert "finished_at" in source
    assert "failure_code" in source
    assert "UNIQUE (task_id, agent_run_id, finalization_epoch, attempt_number)" in source
    assert "status IN ('running', 'succeeded', 'failed', 'cancelled')" in source
    assert "DROP TABLE qc_web_report_generation_attempt" in source
