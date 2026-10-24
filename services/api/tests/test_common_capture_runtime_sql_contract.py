"""公共 Capture、App Build 引用和动作幂等的静态回归合同。"""

from pathlib import Path

import pytest

from services.api.app.api.v1.tasks import SubmitTaskRequest
from services.api.app.domain.task import TaskTargetPlatform

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def _read(relative_path: str) -> str:
    return (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")


def test_action_execution_joins_common_capture_identity() -> None:
    source = _read("services/api/app/infrastructure/db/action_execution_repository.py")

    assert "INNER JOIN qc_capture AS source" in source
    assert "FROM qc_page_capture AS source" not in source


def test_evidence_exit_gate_joins_common_capture_identity() -> None:
    source = _read("services/api/app/infrastructure/db/evidence_exit_gate_repository.py")

    assert "INNER JOIN qc_capture AS capture" in source
    assert "INNER JOIN qc_page_capture AS capture" not in source


def test_app_capture_has_business_idempotency_identity() -> None:
    migration = _read(
        "services/api/migrations/versions/20260806_0038_capture_business_idempotency.py"
    )
    repository = _read("services/api/app/infrastructure/db/app_action_capture_repository.py")

    assert "action_execution_id, capture_role" in migration
    assert "HAVING count(*) > 1" in migration
    assert "RAISE EXCEPTION" in migration
    assert "ON CONFLICT (action_execution_id, capture_role)" in repository
    assert "canonical_capture_id = str(inserted_capture_id)" in repository
    assert "Artifact" in repository and "内容或归属不一致" in repository
    assert "qc_evidence_artifact.checksum = EXCLUDED.checksum" in repository
    assert "qc_app_capture.image_signature = EXCLUDED.image_signature" in repository
    assert "DO UPDATE SET\n                    checksum" not in repository


def test_app_capture_runtime_foreign_keys_target_common_capture() -> None:
    migration = _read(
        "services/api/migrations/versions/20260806_0039_capture_reference_targets.py"
    )

    assert '("qc_task_log", "page_capture_id", "SET NULL")' in migration
    assert '("qc_task_page_node", "page_capture_id")' in migration
    assert '("qc_task_page_node", "page_capture_id", "SET NULL")' not in migration
    assert '"qc_capture", on_delete' in migration
    assert "DEFERRABLE INITIALLY DEFERRED" in migration

    legacy_migration = _read(
        "services/api/migrations/versions/20260804_0035_app_workflow_runtime.py"
    )
    for table_name in (
        "qc_evidence_artifact",
        "qc_action_execution",
        "qc_evidence_manifest",
        "qc_inspection_work_item",
        "qc_evidence_exit_requirement_set",
        "qc_journey_execution",
    ):
        assert table_name in legacy_migration

    for relative_path in (
        "services/api/app/infrastructure/db/inspection_work_item_repository.py",
        "services/api/app/infrastructure/db/evidence_manifest_repository.py",
        "services/api/app/infrastructure/db/evidence_supplement_repository.py",
        "services/api/app/infrastructure/browser/ui_snapshot_reader.py",
        "services/api/app/infrastructure/rule_judge_evidence.py",
        "services/api/app/infrastructure/db/task_finalization_repository.py",
    ):
        source = _read(relative_path)
        assert "qc_page_capture" not in source
        assert "qc_capture" in source

    task_repository = _read("services/api/app/infrastructure/db/task_repository.py")
    assert "FROM qc_capture common_capture" in task_repository
    assert "JOIN qc_page_capture capture" not in task_repository
    assert "FROM qc_page_capture capture" not in task_repository


def test_page_only_capture_foreign_keys_are_explicitly_scoped() -> None:
    migration = _read(
        "services/api/migrations/versions/20260806_0039_capture_reference_targets.py"
    )

    for table_name, column_name in (
        ("qc_page_capture", "parent_page_capture_id"),
        ("qc_page_exploration_action", "source_page_capture_id"),
        ("qc_page_exploration_action", "result_page_capture_id"),
        ("qc_task_page_node", "page_capture_id"),
        ("qc_pipeline_shadow_comparison", "page_capture_id"),
    ):
        assert f'("{table_name}", "{column_name}")' in migration

    legacy_migration = _read(
        "services/api/migrations/versions/20260804_0035_app_workflow_runtime.py"
    )
    assert '"qc_journey_run"' not in legacy_migration
    assert '"qc_journey_execution", "current_capture_id"' in legacy_migration


def test_api_rejects_invalid_platform_build_reference() -> None:
    with pytest.raises(ValueError, match="platform_build_ref"):
        SubmitTaskRequest(
            target_platform=TaskTargetPlatform.APP,
            task_config={
                "app": {
                    "platform_build_ref": "app-build:not-a-uuid",
                }
            },
        )
