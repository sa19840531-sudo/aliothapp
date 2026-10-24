"""H5/App 主证据投递验收脚本测试。"""

from scripts.verify_delivery_acceptance import build_report, verify_delivery_payloads


def _incident(platform: str) -> dict:
    source_identity = (
        {
            "source_agent_run_id": "run-1",
            "page_capture_id": "capture-1",
            "page_type_assessment_id": "assessment-1",
            "assessment_hash": "sha256:assessment",
        }
        if platform == "h5"
        else {
            "source_agent_run_id": "run-1",
            "evidence_index_id": "index-1",
            "evidence_index_hash": "sha256:index",
        }
    )
    return {
        "platform": platform,
        "publication_id": "publication-1",
        "task_id": "task-1",
        "status": "published",
        "source_identity": source_identity,
        "outbox_pending_count": 0,
        "outbox_dead_letter_count": 0,
        "inbox_pending_count": 0,
        "inbox_dead_letter_count": 0,
        "linked_work_active_count": 0,
        "operations": [],
    }


def test_h5_delivery_acceptance_passes_only_for_terminal_main_chain() -> None:
    checks = verify_delivery_payloads(
        task_id="task-1",
        agent_run_id="run-1",
        task={
            "task_id": "task-1",
            "target_platform": "h5",
            "execution_mode": "full_inspection",
        },
        run={"run_id": "run-1"},
        incidents=[_incident("h5")],
    )

    assert all(item.passed for item in checks)


def test_app_delivery_acceptance_blocks_dead_letter_and_active_work() -> None:
    incident = _incident("app")
    incident["outbox_dead_letter_count"] = 1
    incident["linked_work_active_count"] = 2

    report = build_report(
        task_id="task-1",
        agent_run_id="run-1",
        deployed_commit="abc123",
        config_snapshot_id="prod-20260818",
        task={
            "task_id": "task-1",
            "target_platform": "app",
            "execution_mode": "full_inspection",
        },
        run={"run_id": "run-1"},
        incidents=[incident],
    )

    assert report["status"] == "blocked"
    assert report["deployed_commit"] == "abc123"
    assert report["config_snapshot_id"] == "prod-20260818"
    assert report["supplemental_evidence_in_scope"] is False
    failed = {item["code"] for item in report["checks"] if not item["passed"]}
    assert failed == {"OUTBOX_INBOX_TERMINAL", "INSPECTION_WORK_TERMINAL"}


def test_exploration_only_requires_no_publication() -> None:
    checks = verify_delivery_payloads(
        task_id="task-1",
        agent_run_id="run-1",
        task={
            "task_id": "task-1",
            "target_platform": "app",
            "execution_mode": "exploration_only",
        },
        run={"run_id": "run-1"},
        incidents=[],
    )

    assert all(item.passed for item in checks)
