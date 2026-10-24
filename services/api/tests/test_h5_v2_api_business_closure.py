"""H5 V2 API 执行模式到证据交接副作用的跨组件闭环测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.api.app.api.v2.schemas import V2TaskSubmissionRequest
from services.api.app.application.h5_evidence_handoff import (
    H5EvidenceCapture,
    H5EvidenceHandoffService,
)
from services.api.app.application.h5_evidence_inventory import (
    H5EvidenceArtifactRef,
    H5EvidenceInventorySnapshot,
    H5EvidenceInventoryStatus,
)
from services.api.app.application.task_submission import TaskSubmissionResult
from services.api.app.application.v2_task_entries import (
    ExplorationTaskV2EntryService,
    TaskSubmissionV2EntryService,
)
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.domain.task import task_execution_mode_from_config

TEST_ACCESS_SCOPE = AccessScope(
    actor_user_id="test-user",
    tenant_id="tenant-1",
    permissions=frozenset({"taskObjects:read:any"}),
)


class _SubmissionService:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def submit_task(self, **kwargs) -> TaskSubmissionResult:
        self.calls.append(kwargs)
        return TaskSubmissionResult(
            task_id="11111111-1111-1111-1111-111111111111",
            run_id="22222222-2222-2222-2222-222222222222",
            ruleset_id="33333333-3333-3333-3333-333333333333",
            status="queued",
        )


class _EvidenceReader:
    async def list_judgeable_captures(self, *, task_id: str, agent_run_id: str):
        assert task_id == "11111111-1111-1111-1111-111111111111"
        assert agent_run_id == "22222222-2222-2222-2222-222222222222"
        def ref(capture_id: str, artifact_type: str) -> H5EvidenceArtifactRef:
            return H5EvidenceArtifactRef(
                artifact_id=f"{capture_id}-{artifact_type}", task_id=task_id,
                agent_run_id=agent_run_id, capture_id=capture_id,
                storage_path=f"{task_id}/{capture_id}/{artifact_type}",
                artifact_type=artifact_type, checksum="sha256:" + "0" * 64,
                size_bytes=1,
            )
        return [
            H5EvidenceCapture(
                capture_id="after-1",
                inspection_ready=True,
                page_type="home",
                page_state="stable",
                detection_status="recognized",
                capture_role="after_action",
                url="https://m.example.test/home",
                action_execution_id="action-1",
                parent_capture_id="before-1",
                before_url="about:blank",
                artifact_types=("screenshot", "dom", "visible_text"),
                before_artifact_types=("screenshot", "dom", "visible_text"),
                task_id=task_id,
                agent_run_id=agent_run_id,
                session_id=f"h5:{agent_run_id}",
                session_owner="h5-workflow",
                artifact_refs=(ref("after-1", "screenshot"), ref("after-1", "dom")),
                before_artifact_refs=(ref("before-1", "screenshot"), ref("before-1", "dom")),
                before_manifest_id="manifest-before-1",
                before_manifest_hash="b" * 64,
                before_manifest_quality_status="pass",
                after_manifest_id="manifest-after-1",
                after_manifest_hash="c" * 64,
                after_manifest_quality_status="pass",
            )
        ]


class _InspectionPublisher:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def publish_for_capture(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(published_count=2)


class _Verifier:
    async def verify(self, **kwargs):
        del kwargs
        return SimpleNamespace(status=H5EvidenceInventoryStatus.PASS)


class _Inventory:
    async def upsert_inventory_item(self, *, item):
        return H5EvidenceInventorySnapshot("test", 0, 0, item.status)

    async def mark_published(self, *, action_execution_id, published_count):
        del action_execution_id
        return H5EvidenceInventorySnapshot(
            "test", 0, published_count, H5EvidenceInventoryStatus.PASS
        )


def _command():
    return V2TaskSubmissionRequest.model_validate(
        {
            "task_name": "H5 V2 闭环",
            "target": {"platform": "h5", "url": "https://m.example.test/home"},
            "ruleset_code": "alioth_first_ruleset",
            "ruleset_version": 1,
            "channel": "api",
            "priority": 100,
            "product": {
                "department_name": "质量部",
                "product_code": "H5-001",
                "product_name": "移动页面",
            },
            "scan_mode": "full",
            "journey_steps": [],
        }
    ).to_command()


@pytest.mark.asyncio
async def test_full_api_mode_reaches_formal_snapshot_inspection_handoff() -> None:
    """完整入口锁定的模式必须在交接端产生正式质检工作。"""

    submission = _SubmissionService()
    submitted = await TaskSubmissionV2EntryService(
        submission,
        h5_v2_mode="on",
    ).submit(_command(), access_scope=TEST_ACCESS_SCOPE)
    publisher = _InspectionPublisher()
    mode = task_execution_mode_from_config(submission.calls[0]["task_config"])

    summary = await H5EvidenceHandoffService(
        reader=_EvidenceReader(),
        publisher=publisher,
        object_verifier=_Verifier(),
        inventory=_Inventory(),
    ).prepare(
        task_id=submitted.task_id,
        agent_run_id=submitted.run_id,
        execution_mode=mode,
    )

    assert mode.value == "full_inspection"
    assert summary.published_work_item_count == 2
    assert publisher.calls[0]["snapshot_only"] is True


@pytest.mark.asyncio
async def test_exploration_api_mode_stops_after_evidence_ready() -> None:
    """仅探索入口锁定的模式必须通过同一门禁但保持正式质检零副作用。"""

    submission = _SubmissionService()
    submitted = await ExplorationTaskV2EntryService(
        submission,
        h5_v2_mode="on",
    ).submit(_command(), access_scope=TEST_ACCESS_SCOPE)
    publisher = _InspectionPublisher()
    mode = task_execution_mode_from_config(submission.calls[0]["task_config"])

    summary = await H5EvidenceHandoffService(
        reader=_EvidenceReader(),
        publisher=publisher,
        object_verifier=_Verifier(),
        inventory=_Inventory(),
    ).prepare(
        task_id=submitted.task_id,
        agent_run_id=submitted.run_id,
        execution_mode=mode,
    )

    assert mode.value == "exploration_only"
    assert summary.evidence_ready is True
    assert summary.judgeable_capture_count == 1
    assert summary.published_work_item_count == 0
    assert publisher.calls == []
