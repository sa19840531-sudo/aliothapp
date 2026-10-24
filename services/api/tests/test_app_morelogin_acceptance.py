"""MoreLogin App 真实验收清单与 Publication Entry 对账测试。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = ROOT / "scripts/verify_app_morelogin_acceptance.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "verify_app_morelogin_acceptance",
        SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _base_payloads() -> dict:
    return {
        "task_id": "task-1",
        "agent_run_id": "run-1",
        "task": {
            "task_id": "task-1",
            "target_platform": "app",
            "target": {"device": {"backend": "cloud_phone", "profile_id": "qa-b"}},
        },
        "run": {
            "run_id": "run-1",
            "usage": {
                "app_inspection_publication_count": 1,
                "app_publication_expected_entry_count": 14,
                "app_publication_terminal_entry_count": 14,
                "app_capture_count": 14,
                "app_distinct_image_signature_count": 13,
            },
        },
        "report": {
            "summary": {
                "overall_status": "incomplete",
                "task_goal_completed": False,
                "reached_target_pages": 0,
                "pending_target_pages": 12,
                "fail": 0,
            },
            "completion_gate": {
                "gate_passed": False,
                "block_reasons": ["app_coverage_or_visual_quality_incomplete"],
            },
            "termination_reason": "error",
        },
        "session": {"status": "closed", "device_ref": "cloud_phone:qa-b"},
        "lease": {
            "device_ref": "cloud_phone:qa-b",
            "backend": "cloud_phone",
            "state": "released",
            "health_state": "available",
            "capabilities": {"vendor": "morelogin"},
        },
        "evidence_index": {
            "index_id": "app-evidence-index:1",
            "status": "sealed",
        },
        "publication_summary": {
            "total_count": 1,
            "nonterminal_count": 0,
            "expected_entry_count": 14,
            "terminal_entry_count": 14,
            "needs_review_count": 1,
            "failed_count": 0,
        },
        "captures": [
            {"capture_id": "c1", "image_signature": "sig-a"},
            {"capture_id": "c2", "image_signature": "sig-b"},
        ],
    }


def test_platform_tech_passes_when_publication_counts_match() -> None:
    module = _load_module()
    payload = _base_payloads()
    report = module.build_report(
        deployed_commit="abc123",
        config_snapshot_id="lab-20260821",
        **payload,
    )

    assert report["platform_tech_status"] == "passed"
    assert report["app_quality_status"] == "blocked"
    assert report["status"] == "blocked"
    codes = {item["code"]: item["passed"] for item in report["checks"]}
    assert codes["PUBLICATION_ENTRY_COUNTS"] is True
    assert codes["INDEX_SEALED"] is True
    assert codes["LEASE_RELEASED"] is True
    assert codes["APP_QUALITY"] is False
    assert any("不等于" in item or "≠" in item for item in report["limitations"])


def test_publication_entry_count_mismatch_blocks_tech_gate() -> None:
    module = _load_module()
    payload = _base_payloads()
    payload["publication_summary"]["terminal_entry_count"] = 0
    payload["run"]["usage"]["app_publication_terminal_entry_count"] = 0

    checks = module.verify_morelogin_acceptance_payloads(**payload)
    failed = {item.code for item in checks if not item.passed}
    assert "PUBLICATION_ENTRY_COUNTS" in failed


def test_emulator_backend_is_rejected() -> None:
    module = _load_module()
    payload = _base_payloads()
    payload["lease"]["device_ref"] = "emulator:local"
    payload["lease"]["backend"] = "emulator"

    checks = module.verify_morelogin_acceptance_payloads(**payload)
    failed = {item.code: item.message for item in checks if not item.passed}
    assert "MORELOGIN_BACKEND" in failed


def test_fixture_cli_writes_json(tmp_path: Path) -> None:
    module = _load_module()
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    payload = _base_payloads()
    (fixture / "task.json").write_text(
        json.dumps(payload["task"], ensure_ascii=False),
        encoding="utf-8",
    )
    (fixture / "run.json").write_text(
        json.dumps(payload["run"], ensure_ascii=False),
        encoding="utf-8",
    )
    (fixture / "report.json").write_text(
        json.dumps(payload["report"], ensure_ascii=False),
        encoding="utf-8",
    )
    (fixture / "session.json").write_text(
        json.dumps(payload["session"], ensure_ascii=False),
        encoding="utf-8",
    )
    (fixture / "lease.json").write_text(
        json.dumps(payload["lease"], ensure_ascii=False),
        encoding="utf-8",
    )
    (fixture / "evidence_index.json").write_text(
        json.dumps(payload["evidence_index"], ensure_ascii=False),
        encoding="utf-8",
    )
    (fixture / "publication_summary.json").write_text(
        json.dumps(payload["publication_summary"], ensure_ascii=False),
        encoding="utf-8",
    )
    (fixture / "captures.json").write_text(
        json.dumps(payload["captures"], ensure_ascii=False),
        encoding="utf-8",
    )
    output = tmp_path / "out.json"
    code = module.main(
        [
            "--task-id",
            "task-1",
            "--agent-run-id",
            "run-1",
            "--deployed-commit",
            "abc",
            "--config-snapshot-id",
            "lab",
            "--fixture-dir",
            str(fixture),
            "--output",
            str(output),
        ]
    )
    assert code == 0
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["platform_tech_status"] == "passed"
    assert saved["protocol_version"] == "app-morelogin-acceptance.v1"
