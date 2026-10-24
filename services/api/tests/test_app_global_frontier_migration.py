"""0051 App 全局 Frontier 迁移合同测试。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

MIGRATION = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260814_0052_app_global_frontier.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("app_global_frontier_0051", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_app_global_frontier_migration_extends_single_head() -> None:
    module = _module()

    assert module.revision == "20260814_0052"
    assert module.down_revision == "20260814_0051"


def test_app_global_frontier_migration_has_identity_lease_and_recovery_guards() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    for token in (
        "CREATE TABLE qc_app_exploration_frontier",
        "UNIQUE (\n                agent_run_id, candidate_fingerprint",
        "status IN (\n                    'pending', 'leased', 'running', 'retry_wait'",
        "source_work_id varchar(96)",
        "source_capture_id uuid",
        "row_version bigint NOT NULL DEFAULT 0",
        "trg_qc_app_exploration_frontier_guard",
        "session.surface_kind IN ('android', 'ios')",
        "candidate_payload->>'frontier_item_id'",
        "payload_hash !~ '^[0-9a-f]{64}$'",
        "digest(",
        "NEW.candidate_payload::text",
        "App Frontier 候选事实禁止修改",
        "App Frontier 状态迁移不受支持",
        "result_work.work_id = NEW.result_work_id",
        "result_capture.id = NEW.result_capture_id",
        "已有事实，禁止静默降级",
    ):
        assert token in source
