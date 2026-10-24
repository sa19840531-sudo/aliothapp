"""H5 已持久化证据交接门禁测试。"""

import asyncio
import inspect
import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from services.api.app.application.h5_evidence_handoff import (
    H5EvidenceCapture,
    H5EvidenceHandoffService,
)
from services.api.app.application.h5_evidence_inventory import (
    H5EvidenceArtifactRef,
    H5EvidenceInventorySnapshot,
    H5EvidenceInventoryStatus,
)
from services.api.app.application.h5_exploration_workflow import (
    H5_SURFACE_SESSION_OWNER,
)
from services.api.app.domain.task import H5InspectionNotificationMode, TaskExecutionMode
from services.api.app.infrastructure.db.h5_evidence_handoff_repository import (
    PostgresH5EvidenceCaptureReader,
)


class _Reader:
    def __init__(self, captures: list[H5EvidenceCapture]) -> None:
        self.captures = captures
        self.calls: list[tuple[str, str]] = []

    async def list_judgeable_captures(
        self,
        *,
        task_id: str,
        agent_run_id: str,
    ) -> list[H5EvidenceCapture]:
        self.calls.append((task_id, agent_run_id))
        return list(self.captures)


class _Publisher:
    def __init__(
        self,
        published_count: int = 1,
        excluded_active_session_rule_count: int = 0,
    ) -> None:
        self.published_count = published_count
        self.excluded_active_session_rule_count = excluded_active_session_rule_count
        self.calls: list[dict[str, object]] = []

    async def publish_for_capture(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            published_count=self.published_count,
            excluded_active_session_rule_count=(self.excluded_active_session_rule_count),
            prepared_commands=(),
            excluded_active_session_rules=tuple(
                {
                    "rule_id": f"active-rule-{index}",
                    "rule_code": f"ACTIVE_RULE_{index}",
                    "contract_hash": f"active-hash-{index}",
                }
                for index in range(self.excluded_active_session_rule_count)
            ),
        )


class _Inventory:
    def __init__(self) -> None:
        self.items = []
        self.published: list[tuple[str, int]] = []
        self.counts: dict[str, int] = {}

    async def upsert_inventory_item(self, *, item):
        self.items.append(item)
        return H5EvidenceInventorySnapshot(
            "same", 0, self.counts.get(item.action_execution_id, 0), item.status
        )

    async def mark_published(self, *, action_execution_id, published_count):
        self.published.append((action_execution_id, published_count))
        self.counts[action_execution_id] = max(
            self.counts.get(action_execution_id, 0), published_count
        )
        return H5EvidenceInventorySnapshot(
            "same", 0, self.counts[action_execution_id], H5EvidenceInventoryStatus.PASS
        )


class _PublicationWriter:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.shadow_calls: list[dict[str, object]] = []

    async def create_for_capture(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(publication_id=f"publication-{len(self.calls)}", created=True)

    async def record_shadow_for_capture(self, **kwargs):
        self.shadow_calls.append(kwargs)
        return SimpleNamespace(
            publication_id=f"shadow-publication-{len(self.shadow_calls)}",
            created=True,
        )


class _ActiveSessionCoverageReader:
    def __init__(self, unresolved_count: int) -> None:
        self.unresolved_count = unresolved_count
        self.calls: list[dict[str, object]] = []

    async def count_unresolved_active_session_rules_for_capture(self, **kwargs) -> int:
        self.calls.append(kwargs)
        return self.unresolved_count


class _Verifier:
    async def verify(self, **kwargs):
        del kwargs
        return SimpleNamespace(status=H5EvidenceInventoryStatus.PASS)


def _service(
    *,
    reader,
    publisher,
    inventory=None,
    publication_writer=None,
    active_session_coverage_reader=None,
    manifest_finalizer=None,
):
    return H5EvidenceHandoffService(
        reader=reader,
        publisher=publisher,
        object_verifier=_Verifier(),
        inventory=inventory or _Inventory(),
        publication_writer=publication_writer,
        active_session_coverage_reader=active_session_coverage_reader,
        manifest_finalizer=manifest_finalizer,
    )


def _capture(
    capture_id: str = "capture-1",
    *,
    page_type: str | None = "home",
    action_execution_id: str = "action-1",
    rule_matching_allowed: bool = True,
) -> H5EvidenceCapture:
    def ref(capture_id: str, artifact_type: str) -> H5EvidenceArtifactRef:
        return H5EvidenceArtifactRef(
            artifact_id=f"{capture_id}-{artifact_type}",
            task_id="task-1",
            agent_run_id="run-1",
            capture_id=capture_id,
            storage_path=f"task-1/{capture_id}/{artifact_type}",
            artifact_type=artifact_type,
            checksum="sha256:" + "0" * 64,
            size_bytes=1,
        )

    return H5EvidenceCapture(
        capture_id=capture_id,
        inspection_ready=True,
        page_type=page_type,
        page_state="stable",
        detection_status="recognized",
        capture_role="after_action",
        url="https://m.example.test/home",
        action_execution_id=action_execution_id,
        parent_capture_id="before-capture-1",
        before_url="about:blank",
        artifact_types=("dom", "screenshot", "visible_text"),
        before_artifact_types=("dom", "screenshot", "visible_text"),
        task_id="task-1",
        agent_run_id="run-1",
        session_id="h5:run-1",
        session_owner="h5-workflow",
        artifact_refs=(ref(capture_id, "screenshot"), ref(capture_id, "dom")),
        before_artifact_refs=(
            ref("before-capture-1", "screenshot"),
            ref("before-capture-1", "dom"),
        ),
        page_type_assessment_id=f"assessment-{capture_id}",
        assessment_hash="a" * 64,
        before_manifest_id="manifest-before-1",
        before_manifest_hash="b" * 64,
        before_manifest_quality_status="pass",
        after_manifest_id=f"manifest-{capture_id}",
        after_manifest_hash="c" * 64,
        after_manifest_quality_status="pass",
        rule_matching_allowed=rule_matching_allowed,
    )


@pytest.mark.asyncio
async def test_handoff_manifest_finalize_skips_uncommitted_action_placeholder_ids() -> None:
    """未提交动作 Capture 的 capture_id 是 action-execution 逻辑串占位，
    不得作为 uuid 喂给 Manifest 终结（真实回归：任务 ff5b5446 收口 DataError 全灭）。"""
    real_capture_id = str(uuid4())
    real_parent_id = str(uuid4())
    committed = replace(
        _capture(real_capture_id),
        parent_capture_id=real_parent_id,
    )
    placeholder = replace(
        _capture("action-execution:" + "0" * 64),
        parent_capture_id=None,
        page_type=None,
        detection_status="pending",
    )

    class _Finalizer:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def finalize_capture(self, *, task_id: str, capture_id: str) -> None:
            del task_id
            self.calls.append(capture_id)

    finalizer = _Finalizer()
    service = H5EvidenceHandoffService(
        reader=_Reader([committed, placeholder]),
        publisher=_Publisher(),
        object_verifier=_Verifier(),
        inventory=_Inventory(),
        manifest_finalizer=finalizer,
    )

    await service.prepare(
        task_id="task-1",
        agent_run_id="run-1",
        execution_mode=TaskExecutionMode.EXPLORATION_ONLY,
    )

    assert sorted(finalizer.calls) == sorted([real_capture_id, real_parent_id])


@pytest.mark.asyncio
async def test_handoff_rejects_empty_evidence_inventory() -> None:
    """没有预期动作证据时保留可审计的不足结论。"""

    service = _service(reader=_Reader([]), publisher=_Publisher())

    summary = await service.prepare(
        task_id="task-1",
        agent_run_id="run-1",
        execution_mode=TaskExecutionMode.EXPLORATION_ONLY,
    )
    assert summary.evidence_ready is False
    assert summary.total_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "notification_mode",
    [
        H5InspectionNotificationMode.OFF,
        H5InspectionNotificationMode.SHADOW,
        H5InspectionNotificationMode.ENFORCED,
    ],
)
async def test_handoff_does_not_publish_targeted_recapture_capture(
    notification_mode: H5InspectionNotificationMode,
) -> None:
    capture = replace(
        _capture(),
        supplemental_evidence_source="targeted_recapture",
    )
    publisher = _Publisher()
    publication_writer = _PublicationWriter()
    service = _service(
        reader=_Reader([capture]),
        publisher=publisher,
        publication_writer=publication_writer,
    )

    summary = await service.prepare(
        task_id="task-1",
        agent_run_id="run-1",
        execution_mode=TaskExecutionMode.FULL_INSPECTION,
        notification_mode=notification_mode,
    )

    assert summary.evidence_ready is True
    assert summary.published_work_item_count == 0
    assert summary.publication_count == 0
    assert publisher.calls == []
    assert publication_writer.calls == []
    assert publication_writer.shadow_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "notification_mode",
    [
        H5InspectionNotificationMode.OFF,
        H5InspectionNotificationMode.SHADOW,
        H5InspectionNotificationMode.ENFORCED,
    ],
)
async def test_handoff_keeps_non_rule_matchable_capture_without_publishing_rules(
    notification_mode: H5InspectionNotificationMode,
) -> None:
    publisher = _Publisher()
    publication_writer = _PublicationWriter()
    service = _service(
        reader=_Reader([_capture(rule_matching_allowed=False)]),
        publisher=publisher,
        publication_writer=publication_writer,
    )

    summary = await service.prepare(
        task_id="task-1",
        agent_run_id="run-1",
        execution_mode=TaskExecutionMode.FULL_INSPECTION,
        notification_mode=notification_mode,
    )

    assert summary.evidence_ready is True
    assert summary.judgeable_capture_count == 1
    assert summary.published_work_item_count == 0
    assert summary.publication_count == 0
    assert publisher.calls == []
    assert publication_writer.calls == []
    assert publication_writer.shadow_calls == []


@pytest.mark.asyncio
async def test_handoff_rejects_inventory_when_latest_manifest_is_not_pass() -> None:
    capture = replace(
        _capture(),
        after_manifest_quality_status="pending",
    )
    inventory = _Inventory()
    summary = await _service(
        reader=_Reader([capture]),
        publisher=_Publisher(),
        inventory=inventory,
    ).prepare(
        task_id="task-1",
        agent_run_id="run-1",
        execution_mode=TaskExecutionMode.FULL_INSPECTION,
    )

    assert summary.evidence_ready is False
    assert summary.insufficient_count == 1
    assert inventory.items[0].authority_source == "manifest_unverified"
    assert "after:manifest_not_pass" in inventory.items[0].reason_codes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("capture", "message"),
    [
        (replace(_capture(), capture_role="before_action"), "角色"),
        (replace(_capture(), detection_status="error"), "页面判定"),
        (replace(_capture(), page_type=None), "page_type"),
        (replace(_capture(), url=""), "动作后 URL"),
        (replace(_capture(), action_execution_id=None), "动作执行"),
        (replace(_capture(), parent_capture_id=None), "动作前 Capture"),
        (replace(_capture(), before_url=""), "动作前 URL"),
        (replace(_capture(), artifact_types=("dom",)), "screenshot"),
        (replace(_capture(), artifact_types=("screenshot",)), "dom"),
        (replace(_capture(), before_artifact_types=("dom",)), "动作前.*screenshot"),
        (replace(_capture(), before_artifact_types=("screenshot",)), "动作前.*dom"),
    ],
)
async def test_handoff_keeps_incomplete_capture_as_inventory_insufficient(
    capture: H5EvidenceCapture,
    message: str,
) -> None:
    """单项事实不足不得阻断其他 Action 的库存或发布。"""

    service = _service(reader=_Reader([capture]), publisher=_Publisher())

    summary = await service.prepare(
        task_id="task-1",
        agent_run_id="run-1",
        execution_mode=TaskExecutionMode.FULL_INSPECTION,
    )
    assert summary.insufficient_count == 1
    assert summary.pass_count == 0
    assert summary.published_work_item_count == 0


@pytest.mark.asyncio
async def test_exploration_only_handoff_marks_evidence_ready_without_publish() -> None:
    """仅探索只证明证据就绪，绝不能发布正式质检库存。"""

    reader = _Reader([_capture()])
    publisher = _Publisher()
    service = _service(reader=reader, publisher=publisher)

    summary = await service.prepare(
        task_id="task-1",
        agent_run_id="run-1",
        execution_mode=TaskExecutionMode.EXPLORATION_ONLY,
    )

    assert reader.calls == [("task-1", "run-1")]
    assert summary.evidence_ready is True
    assert summary.judgeable_capture_count == 1
    assert summary.page_types == ("home",)
    assert summary.published_work_item_count == 0
    assert publisher.calls == []


@pytest.mark.asyncio
async def test_manifest_finalizer_skips_action_execution_synthetic_identity() -> None:
    """缺少动作后 Capture 时，动作级占位 ID 不能进入 Manifest UUID 查询。"""

    class _ManifestFinalizer:
        def __init__(self) -> None:
            self.calls: list[dict[str, str]] = []

        async def finalize_capture(self, *, task_id: str, capture_id: str) -> None:
            self.calls.append({"task_id": task_id, "capture_id": capture_id})

    before_capture_id = "11111111-1111-1111-1111-111111111111"
    action_execution_id = (
        "action-execution:852352b6283e8d1b24310dbad45bff1a57dfb91919aacb31e3fd2c1e0ce3a7b5"
    )
    finalizer = _ManifestFinalizer()
    service = _service(
        reader=_Reader(
            [
                replace(
                    _capture(
                        capture_id=action_execution_id,
                        action_execution_id=action_execution_id,
                    ),
                    parent_capture_id=before_capture_id,
                )
            ]
        ),
        publisher=_Publisher(),
        manifest_finalizer=finalizer,
    )

    await service.prepare(
        task_id="task-1",
        agent_run_id="run-1",
        execution_mode=TaskExecutionMode.EXPLORATION_ONLY,
    )

    assert finalizer.calls == [
        {"task_id": "task-1", "capture_id": before_capture_id},
    ]


@pytest.mark.asyncio
async def test_full_inspection_handoff_publishes_work_for_every_capture() -> None:
    """完整质检只基于门禁通过的不可变 Capture 发布质检工作项。"""

    reader = _Reader(
        [
            _capture(),
            _capture("capture-2", page_type="video_detail", action_execution_id="action-2"),
        ]
    )
    publisher = _Publisher(
        published_count=2,
        excluded_active_session_rule_count=1,
    )
    service = _service(reader=reader, publisher=publisher)

    summary = await service.prepare(
        task_id="task-1",
        agent_run_id="run-1",
        execution_mode=TaskExecutionMode.FULL_INSPECTION,
    )

    assert summary.evidence_ready is True
    assert summary.judgeable_capture_count == 2
    assert summary.page_types == ("home", "video_detail")
    assert summary.published_work_item_count == 4
    assert summary.excluded_active_session_rule_count == 2
    assert publisher.calls == [
        {
            "task_id": "task-1",
            "capture_id": "capture-1",
            "visual_page_type": "home",
            "page_state": "stable",
            "agent_run_id": "run-1",
            "snapshot_only": True,
        },
        {
            "task_id": "task-1",
            "capture_id": "capture-2",
            "visual_page_type": "video_detail",
            "page_state": "stable",
            "agent_run_id": "run-1",
            "snapshot_only": True,
        },
    ]


@pytest.mark.asyncio
async def test_off_handoff_only_keeps_unresolved_live_rules_as_coverage_gap() -> None:
    publisher = _Publisher(
        published_count=1,
        excluded_active_session_rule_count=2,
    )
    coverage = _ActiveSessionCoverageReader(unresolved_count=1)
    service = _service(
        reader=_Reader([_capture()]),
        publisher=publisher,
        active_session_coverage_reader=coverage,
    )

    summary = await service.prepare(
        task_id="task-1",
        agent_run_id="run-1",
        execution_mode=TaskExecutionMode.FULL_INSPECTION,
        notification_mode=H5InspectionNotificationMode.OFF,
    )

    assert summary.published_work_item_count == 1
    assert summary.excluded_active_session_rule_count == 1
    assert coverage.calls[0]["task_id"] == "task-1"
    assert coverage.calls[0]["source_agent_run_id"] == "run-1"
    assert len(coverage.calls[0]["excluded_rules"]) == 2


@pytest.mark.asyncio
async def test_enforced_handoff_creates_publications_without_sync_work_items() -> None:
    """enforced 必须只创建权威 Publication，禁止与旧同步 Publisher 双写。"""

    publisher = _Publisher()
    publication_writer = _PublicationWriter()
    inventory = _Inventory()
    service = _service(
        reader=_Reader([_capture()]),
        publisher=publisher,
        publication_writer=publication_writer,
        inventory=inventory,
    )

    summary = await service.prepare(
        task_id="task-1",
        agent_run_id="run-1",
        execution_mode=TaskExecutionMode.FULL_INSPECTION,
        notification_mode=H5InspectionNotificationMode.ENFORCED,
    )

    assert summary.publication_count == 1
    assert summary.published_work_item_count == 0
    assert publisher.calls == []
    assert inventory.published == []
    assert publication_writer.calls == [
        {
            "task_id": "task-1",
            "source_agent_run_id": "run-1",
            "capture_id": "capture-1",
            "page_type_assessment_id": "assessment-capture-1",
            "assessment_hash": "a" * 64,
            "page_type": "home",
            "page_state": "stable",
        }
    ]


@pytest.mark.parametrize(
    "notification_mode",
    [
        H5InspectionNotificationMode.OFF,
        H5InspectionNotificationMode.SHADOW,
        H5InspectionNotificationMode.ENFORCED,
    ],
)
async def test_handoff_keeps_duplicate_logical_capture_without_republishing_rules(
    notification_mode: H5InspectionNotificationMode,
) -> None:
    capture = replace(_capture(), logical_state_previously_published=True)
    publisher = _Publisher()
    publication_writer = _PublicationWriter()
    service = _service(
        reader=_Reader([capture]),
        publisher=publisher,
        publication_writer=publication_writer,
    )

    summary = await service.prepare(
        task_id="task-1",
        agent_run_id="run-1",
        execution_mode=TaskExecutionMode.FULL_INSPECTION,
        notification_mode=notification_mode,
    )

    assert summary.evidence_ready is True
    assert summary.judgeable_capture_count == 1
    assert summary.publication_count == 0
    assert summary.published_work_item_count == 0
    assert publisher.calls == []
    assert publication_writer.calls == []
    assert publication_writer.shadow_calls == []


@pytest.mark.asyncio
async def test_shadow_handoff_records_inventory_without_authoritative_side_effects() -> None:
    """shadow 冻结候选计划，但不得创建正式 Work 或通知。"""

    publisher = _Publisher()
    publication_writer = _PublicationWriter()
    inventory = _Inventory()
    service = _service(
        reader=_Reader([_capture()]),
        publisher=publisher,
        publication_writer=publication_writer,
        inventory=inventory,
    )

    summary = await service.prepare(
        task_id="task-1",
        agent_run_id="run-1",
        execution_mode=TaskExecutionMode.FULL_INSPECTION,
        notification_mode=H5InspectionNotificationMode.SHADOW,
    )

    assert summary.evidence_ready is True
    assert summary.publication_count == 1
    assert summary.published_work_item_count == 0
    assert len(inventory.items) == 1
    assert publisher.calls == [
        {
            "task_id": "task-1",
            "capture_id": "capture-1",
            "visual_page_type": "home",
            "page_state": "stable",
            "agent_run_id": "run-1",
            "snapshot_only": True,
            "prepare_only": True,
        }
    ]
    assert publication_writer.calls == []
    assert len(publication_writer.shadow_calls) == 1
    assert publication_writer.shadow_calls[0]["result"].published_count == 1


@pytest.mark.asyncio
async def test_handoff_uses_persisted_failed_snapshot_as_the_only_publish_authority() -> None:
    """failed sticky 重放时，本轮对象通过也不得发布或报告证据就绪。"""

    class _StickyFailedInventory(_Inventory):
        async def upsert_inventory_item(self, *, item):
            self.items.append(item)
            return H5EvidenceInventorySnapshot(
                "failed-original",
                7,
                0,
                H5EvidenceInventoryStatus.FAILED,
            )

    publisher = _Publisher()
    inventory = _StickyFailedInventory()
    first_service = _service(
        reader=_Reader([_capture()]),
        publisher=publisher,
        inventory=inventory,
    )
    second_service = _service(
        reader=_Reader([_capture()]),
        publisher=publisher,
        inventory=inventory,
    )

    first = await first_service.prepare(
        task_id="task-1",
        agent_run_id="run-1",
        execution_mode=TaskExecutionMode.FULL_INSPECTION,
    )
    second = await second_service.prepare(
        task_id="task-1",
        agent_run_id="run-1",
        execution_mode=TaskExecutionMode.FULL_INSPECTION,
    )

    assert first.pass_count == second.pass_count == 0
    assert first.failed_count == second.failed_count == 1
    assert first.insufficient_count == second.insufficient_count == 0
    assert first.evidence_ready is second.evidence_ready is False
    assert publisher.calls == []


@pytest.mark.asyncio
async def test_handoff_replay_persists_every_item_without_republishing() -> None:
    """同一库存版本重放不得重复创建有效质检工作。"""

    class _IdempotentPublisher(_Publisher):
        def __init__(self) -> None:
            super().__init__()
            self.created: set[str] = set()
            self._lock = asyncio.Lock()

        async def publish_for_capture(self, **kwargs):
            self.calls.append(kwargs)
            async with self._lock:
                capture_id = str(kwargs["capture_id"])
                created = capture_id not in self.created
                self.created.add(capture_id)
            return SimpleNamespace(
                published_count=1,
                items=({"created": created},),
                excluded_active_session_rule_count=0,
            )

    inventory = _Inventory()
    publisher = _IdempotentPublisher()
    first_service = _service(reader=_Reader([_capture()]), publisher=publisher, inventory=inventory)
    second_service = _service(
        reader=_Reader([_capture()]), publisher=publisher, inventory=inventory
    )

    first = await first_service.prepare(
        task_id="task-1", agent_run_id="run-1", execution_mode=TaskExecutionMode.FULL_INSPECTION
    )
    second = await second_service.prepare(
        task_id="task-1", agent_run_id="run-1", execution_mode=TaskExecutionMode.FULL_INSPECTION
    )

    assert len(inventory.items) == 2
    assert len(publisher.calls) == 2
    assert len(publisher.created) == 1
    assert first.published_work_item_count == 1
    assert second.published_work_item_count == 0
    assert inventory.published == [("action-1", 1), ("action-1", 1)]


@pytest.mark.asyncio
async def test_handoff_concurrent_prepare_keeps_one_effective_work_item() -> None:
    """并发重放可调用两次发布器，但唯一身份只创建一个有效工作项。"""

    class _IdempotentPublisher(_Publisher):
        def __init__(self) -> None:
            super().__init__()
            self.created: set[str] = set()
            self._lock = asyncio.Lock()

        async def publish_for_capture(self, **kwargs):
            self.calls.append(kwargs)
            await asyncio.sleep(0)
            async with self._lock:
                capture_id = str(kwargs["capture_id"])
                created = capture_id not in self.created
                self.created.add(capture_id)
            return SimpleNamespace(
                published_count=1,
                items=({"created": created},),
                excluded_active_session_rule_count=0,
            )

    inventory = _Inventory()
    publisher = _IdempotentPublisher()
    summaries = await asyncio.gather(
        _service(reader=_Reader([_capture()]), publisher=publisher, inventory=inventory).prepare(
            task_id="task-1",
            agent_run_id="run-1",
            execution_mode=TaskExecutionMode.FULL_INSPECTION,
        ),
        _service(reader=_Reader([_capture()]), publisher=publisher, inventory=inventory).prepare(
            task_id="task-1",
            agent_run_id="run-1",
            execution_mode=TaskExecutionMode.FULL_INSPECTION,
        ),
    )

    assert len(publisher.calls) == 2
    assert len(publisher.created) == 1
    assert sum(summary.published_work_item_count for summary in summaries) == 1
    assert inventory.counts["action-1"] == 1


@pytest.mark.asyncio
async def test_handoff_persists_side_qualified_artifact_audit_and_reasons() -> None:
    """每侧都必须有截图和 DOM，跨侧身份错误保留为具体审计原因。"""

    class _SideVerifier:
        async def verify(self, *, artifact, expected_capture_id, **kwargs):
            del kwargs
            if artifact.capture_id != expected_capture_id:
                return SimpleNamespace(
                    status=H5EvidenceInventoryStatus.FAILED,
                    reason_code="artifact_capture_identity_mismatch",
                    summary={"checked": "identity"},
                )
            return SimpleNamespace(
                status=H5EvidenceInventoryStatus.PASS,
                reason_code=None,
                summary={"checked": "bytes"},
            )

    source = _capture()
    capture = replace(
        source,
        artifact_refs=(source.artifact_refs[0],),
        before_artifact_refs=(
            replace(source.before_artifact_refs[0], capture_id="capture-1"),
            source.before_artifact_refs[1],
        ),
    )
    inventory = _Inventory()
    service = H5EvidenceHandoffService(
        reader=_Reader([capture]),
        publisher=_Publisher(),
        object_verifier=_SideVerifier(),
        inventory=inventory,
    )

    summary = await service.prepare(
        task_id="task-1",
        agent_run_id="run-1",
        execution_mode=TaskExecutionMode.FULL_INSPECTION,
    )

    item = inventory.items[0]
    assert summary.failed_count == 1
    assert "after:dom_missing" in item.reason_codes
    assert "artifact_capture_identity_mismatch" in item.reason_codes
    assert item.required_artifacts == (
        "before:dom",
        "before:screenshot",
        "after:dom",
        "after:screenshot",
    )
    assert item.found_artifacts == ("after:screenshot", "before:dom", "before:screenshot")
    assert any(
        artifact["side"] == "before"
        and artifact["reason_code"] == "artifact_capture_identity_mismatch"
        for artifact in item.verification_summary["artifacts"]
    )
    assert any(
        artifact["status"] == "pass" and artifact["reason_code"] is None
        for artifact in item.verification_summary["artifacts"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("missing_side", "corrupt_side", "expected_status", "expected_reason"),
    [
        ("after", None, H5EvidenceInventoryStatus.INSUFFICIENT, "after:dom_missing"),
        ("after", "before", H5EvidenceInventoryStatus.FAILED, "artifact_checksum_mismatch"),
        ("before", "after", H5EvidenceInventoryStatus.FAILED, "artifact_checksum_mismatch"),
    ],
)
async def test_handoff_rechecks_existing_side_when_the_other_side_is_missing(
    missing_side: str,
    corrupt_side: str | None,
    expected_status: H5EvidenceInventoryStatus,
    expected_reason: str,
) -> None:
    """缺一侧仍须重读存在侧；完整性破坏优先于缺侧的业务不足。"""

    class _RecordingVerifier:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def verify(self, *, artifact, expected_capture_id, **kwargs):
            del kwargs
            self.calls.append((artifact.artifact_id, expected_capture_id))
            status = (
                H5EvidenceInventoryStatus.FAILED
                if (corrupt_side, expected_capture_id)
                in {("before", "before-capture-1"), ("after", "capture-1")}
                else H5EvidenceInventoryStatus.PASS
            )
            return SimpleNamespace(
                status=status,
                reason_code=("artifact_checksum_mismatch" if status.value == "failed" else None),
                summary={"read": artifact.storage_path},
            )

    source = _capture()
    if missing_side == "after":
        capture = replace(
            source,
            capture_id="action-1",
            capture_role="",
            url="",
            artifact_types=(),
            artifact_refs=(),
        )
        expected_side = "before"
        expected_capture = "before-capture-1"
    else:
        capture = replace(
            source,
            parent_capture_id=None,
            before_url="",
            before_artifact_types=(),
            before_artifact_refs=(),
        )
        expected_side = "after"
        expected_capture = "capture-1"
    verifier = _RecordingVerifier()
    inventory = _Inventory()
    service = H5EvidenceHandoffService(
        reader=_Reader([capture]),
        publisher=_Publisher(),
        object_verifier=verifier,
        inventory=inventory,
    )

    summary = await service.prepare(
        task_id="task-1",
        agent_run_id="run-1",
        execution_mode=TaskExecutionMode.FULL_INSPECTION,
    )

    item = inventory.items[0]
    assert item.status is expected_status
    assert expected_reason in item.reason_codes
    assert summary.failed_count == int(expected_status is H5EvidenceInventoryStatus.FAILED)
    assert summary.insufficient_count == int(
        expected_status is H5EvidenceInventoryStatus.INSUFFICIENT
    )
    assert len(verifier.calls) == 2
    assert {expected for _, expected in verifier.calls} == {expected_capture}
    assert {artifact["side"] for artifact in item.verification_summary["artifacts"]} == {
        expected_side
    }


@pytest.mark.asyncio
async def test_handoff_publish_failure_leaves_item_retryable() -> None:
    """发布失败不能提前记账，下一次 prepare 必须能够重试。"""

    class FlakyPublisher(_Publisher):
        def __init__(self) -> None:
            super().__init__()
            self.fail = True

        async def publish_for_capture(self, **kwargs):
            if self.fail:
                self.fail = False
                raise RuntimeError("publisher unavailable")
            return await super().publish_for_capture(**kwargs)

    inventory = _Inventory()
    publisher = FlakyPublisher()
    service = _service(reader=_Reader([_capture()]), publisher=publisher, inventory=inventory)

    with pytest.raises(RuntimeError, match="publisher unavailable"):
        await service.prepare(
            task_id="task-1",
            agent_run_id="run-1",
            execution_mode=TaskExecutionMode.FULL_INSPECTION,
        )
    await service.prepare(
        task_id="task-1",
        agent_run_id="run-1",
        execution_mode=TaskExecutionMode.FULL_INSPECTION,
    )

    assert inventory.published == [("action-1", 1)]


@pytest.mark.asyncio
async def test_postgres_reader_maps_capture_inventory_for_exact_task_and_run() -> None:
    """PostgreSQL 读取器必须使用任务/Run 双重归属并映射实际制品集合。"""

    class Connection:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple[object, ...]]] = []

        async def fetch(self, query: str, *args):
            self.calls.append((query, args))
            return [
                {
                    "capture_id": "capture-1",
                    "page_type": "home",
                    "page_state": "stable",
                    "detection_status": "recognized",
                    "capture_role": "after_action",
                    "url": "https://m.example.test/home",
                    "action_execution_id": "action-1",
                    "session_id": "h5:run-1",
                    "session_owner": "h5-workflow",
                    "session_status": "active",
                    "task_finalization_state": "open",
                    "session_finalization_epoch": 3,
                    "task_finalization_epoch": 3,
                    "supplemental_evidence_source": "targeted_recapture",
                    "parent_capture_id": "before-capture-1",
                    "before_url": "about:blank",
                    "artifact_types": ["dom", "screenshot", "visible_text"],
                    "before_artifact_types": ["dom", "screenshot", "visible_text"],
                    "artifact_refs": (
                        '[{"artifact_id":"artifact-1","task_id":"task-1",'
                        '"agent_run_id":"run-1","capture_id":"capture-1",'
                        '"storage_path":"evidence/dom.json","artifact_type":"dom",'
                        '"checksum":"sha256:abc","size_bytes":0}]'
                    ),
                    "before_artifact_refs": "[]",
                    "page_type_assessment_id": "assessment-record-1",
                    "assessment_hash": "d" * 64,
                }
            ]

    class Acquire:
        def __init__(self, connection: Connection) -> None:
            self.connection = connection

        async def __aenter__(self):
            return self.connection

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class Pool:
        def __init__(self, connection: Connection) -> None:
            self.connection = connection

        def acquire(self):
            return Acquire(self.connection)

    connection = Connection()
    reader = PostgresH5EvidenceCaptureReader(pool=Pool(connection))

    captures = await reader.list_judgeable_captures(
        task_id="task-1",
        agent_run_id="run-1",
    )

    assert captures[0].capture_id == "capture-1"
    assert captures[0].session_id == "h5:run-1"
    assert captures[0].session_status == "active"
    assert captures[0].task_finalization_state == "open"
    assert captures[0].session_finalization_epoch == 3
    assert captures[0].task_finalization_epoch == 3
    assert captures[0].supplemental_evidence_source == "targeted_recapture"
    assert captures[0].artifact_refs[0].artifact_id == "artifact-1"
    assert captures[0].artifact_refs[0].size_bytes == 0
    assert captures[0].before_artifact_refs == ()
    assert captures[0].page_type_assessment_id == "assessment-record-1"
    assert captures[0].assessment_hash == "d" * 64
    assert captures[0].logical_state_previously_published is False
    assert len(connection.calls) == 1
    assert connection.calls[0][1] == ("task-1", "run-1")
    sql = " ".join(connection.calls[0][0].split())
    assert "FROM qc_action_execution AS action" in sql
    assert "INNER JOIN qc_task AS task" in sql
    assert "action.finalization_epoch = surface_session.finalization_epoch" in sql
    assert "surface_session.finalization_epoch = task.finalization_epoch" in sql
    assert "task.finalization_agent_run_id = run.id" in sql
    assert "LEFT JOIN qc_capture AS common_capture" in sql
    assert "LEFT JOIN qc_capture AS before_capture" in sql
    assert "LEFT JOIN LATERAL" in sql
    assert "FROM qc_h5_page_type_assessment AS stored" in sql
    assert "lower(task.target_platform) = 'h5'" in sql
    assert "previous_capture.page_seq < page_capture.page_seq" in sql
    assert "previous_capture.inspection_status IN" in sql
    assert "previous_assessment.assessment_payload" in sql
    assert "identity_fingerprint" in sql
    assert "state_fingerprint" in sql
    assert "before_capture.capture_role = 'before_action'" in sql
    assert "before_page_capture.capture_role = 'before_action'" in sql
    assert "action.result_capture_id" in sql
    assert "action.source_capture_id" in sql
    assert "action.session_id = ('h5:' || $2::text)" in sql
    assert "surface_session.session_owner = 'h5-workflow'" in sql
    assert "surface_session.status AS session_status" in sql
    assert "task.finalization_state AS task_finalization_state" in sql
    assert (
        "action.intent_payload->'parameters'->>'supplemental_evidence_source'" in sql
    )
    assert "LIKE '%:h5-workflow'" not in sql
    assert "before_capture.id" in sql
    assert "ORDER BY action.command_sequence ASC, action.action_execution_id ASC" in sql


@pytest.mark.asyncio
async def test_postgres_reader_keeps_existing_side_identity_when_other_side_is_missing() -> None:
    """LEFT JOIN 缺一侧时，另一侧 source/result Capture 身份仍必须保留。"""

    class Connection:
        async def fetch(self, query: str, *args):
            del query, args
            return [
                {
                    "capture_id": "action-missing-after",
                    "session_id": "h5:run-1",
                    "session_owner": "h5-workflow",
                    "page_type": None,
                    "page_state": None,
                    "detection_status": None,
                    "capture_role": "",
                    "url": "",
                    "action_execution_id": "action-missing-after",
                    "parent_capture_id": "before-kept",
                    "before_url": "about:blank",
                    "artifact_types": [],
                    "before_artifact_types": ["dom", "screenshot"],
                },
                {
                    "capture_id": "after-kept",
                    "session_id": "h5:run-1",
                    "session_owner": "h5-workflow",
                    "page_type": "home",
                    "page_state": "stable",
                    "detection_status": "recognized",
                    "capture_role": "after_action",
                    "url": "https://m.example.test/home",
                    "action_execution_id": "action-missing-before",
                    "parent_capture_id": None,
                    "before_url": "",
                    "artifact_types": ["dom", "screenshot"],
                    "before_artifact_types": [],
                },
            ]

    class Acquire:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *args):
            return False

    class Pool:
        def acquire(self):
            return Acquire()

    captures = await PostgresH5EvidenceCaptureReader(pool=Pool()).list_judgeable_captures(
        task_id="task-1", agent_run_id="run-1"
    )

    assert captures[0].capture_id == "action-missing-after"
    assert captures[0].parent_capture_id == "before-kept"
    assert captures[1].capture_id == "after-kept"
    assert captures[1].parent_capture_id is None


@pytest.mark.asyncio
async def test_postgres_reader_rejects_missing_database_session_identity() -> None:
    """读取器不得用请求 Run 补造缺失 session。"""

    class Connection:
        async def fetch(self, query: str, *args):
            del query, args
            return [{"capture_id": "capture-1", "session_id": None}]

    class Acquire:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *args):
            return False

    class Pool:
        def acquire(self):
            return Acquire()

    reader = PostgresH5EvidenceCaptureReader(pool=Pool())
    with pytest.raises(RuntimeError, match="session"):
        await reader.list_judgeable_captures(task_id="task-1", agent_run_id="run-1")


@pytest.mark.asyncio
async def test_postgres_reader_keeps_action_when_after_capture_is_superseded() -> None:
    """after 侧生命周期不合格只能投影为缺侧，不能从 expected Action 集合删除。"""

    class Connection:
        async def fetch(self, query: str, *args):
            del args
            self.query = query
            return [
                {
                    "capture_id": "action-superseded",
                    "session_id": "h5:run-1",
                    "session_owner": "h5-workflow",
                    "page_type": None,
                    "page_state": None,
                    "detection_status": None,
                    "capture_role": "",
                    "url": "",
                    "action_execution_id": "action-superseded",
                    "parent_capture_id": "before-1",
                    "before_url": "about:blank",
                    "artifact_types": [],
                    "before_artifact_types": ["dom", "screenshot"],
                }
            ]

    class Acquire:
        def __init__(self, connection) -> None:
            self.connection = connection

        async def __aenter__(self):
            return self.connection

        async def __aexit__(self, *args):
            return False

    class Pool:
        def __init__(self) -> None:
            self.connection = Connection()

        def acquire(self):
            return Acquire(self.connection)

    pool = Pool()
    captures = await PostgresH5EvidenceCaptureReader(pool=pool).list_judgeable_captures(
        task_id="task-1", agent_run_id="run-1"
    )

    assert [capture.action_execution_id for capture in captures] == ["action-superseded"]
    assert captures[0].capture_id == "action-superseded"
    sql = " ".join(pool.connection.query.split())
    assert "WHERE action.task_id" in sql
    assert "NOT IN ('superseded', 'abandoned')" not in sql.split("WHERE", 1)[1]
    assert "common_capture_lifecycle" in sql
    assert "before_capture_lifecycle" in sql
    assert "before_capture.action_execution_id = action.action_execution_id" in sql
    assert "auth_run.phase_key = 'h5_auth_bootstrap'" in sql
    assert "auth_run.session_id = common_capture.surface_session_id" in sql
    assert "auth_run.source_capture_id = common_capture.id" in sql
    assert "auth_step.result_capture_id = common_capture.id" in sql
    assert "auth_step.step_key <> 'return_to_task_root'" in sql


# ---------------------------------------------------------------------------
# Session 归属：生产写入的是常量，不带 worker 实例号前缀
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handoff_accepts_production_session_owner_without_prefix() -> None:
    """归属按带前缀的旧格式匹配时，生产 Capture 会被整体滤掉、质检永不启动。"""

    capture = replace(_capture(), session_owner=H5_SURFACE_SESSION_OWNER)
    publisher = _Publisher()
    service = _service(reader=_Reader([capture]), publisher=publisher)

    summary = await service.prepare(
        task_id="task-1",
        agent_run_id="run-1",
        execution_mode=TaskExecutionMode.FULL_INSPECTION,
    )

    assert summary.judgeable_capture_count == 1
    assert summary.published_work_item_count == 1
    assert publisher.calls


@pytest.mark.asyncio
async def test_handoff_accepts_web_v2_formal_session_identity() -> None:
    capture = replace(
        _capture(),
        session_id="web-v2:run-1",
        session_owner="web-workflow",
    )
    publisher = _Publisher()
    service = _service(reader=_Reader([capture]), publisher=publisher)

    summary = await service.prepare(
        task_id="task-1",
        agent_run_id="run-1",
        execution_mode=TaskExecutionMode.FULL_INSPECTION,
    )

    assert summary.judgeable_capture_count == 1
    assert summary.published_work_item_count == 1
    assert publisher.calls


def test_judgeable_capture_query_matches_production_session_owner() -> None:
    """SQL 侧同样不能要求归属带前缀，否则读取器一行都取不到。"""

    source = inspect.getsource(PostgresH5EvidenceCaptureReader)

    assert "LIKE '%:h5-workflow'" not in source
    assert "session_owner = 'h5-workflow'" in source
    assert "session_owner = 'web-workflow'" in source
    assert "action.session_id = ('web-v2:' || $2::text)" in source
    assert "task.target_platform = 'web'" in source
    assert "surface_session.session_owner = 'web-v2-workflow'" in source
    assert "action.session_id = ('web:' || $2::text)" in source


def test_judgeable_capture_query_ignores_only_web_companion_consumer_run() -> None:
    """Web 配套质检 Run 不是竞争主 Run，且例外不能放宽到 H5/App。"""

    source = " ".join(inspect.getsource(PostgresH5EvidenceCaptureReader).split())

    scoped_exception = (
        "AND NOT ( task.target_platform = 'web' "
        "AND competing_run.agent_type = 'rule_judge' "
        "AND competing_run.input_context->>'source' = 'h5_inspection_publication' "
        "AND competing_run.input_context->>'finalization_epoch' "
        "= task.finalization_epoch::text )"
    )
    assert scoped_exception in source


# ---------------------------------------------------------------------------
# jsonb 列取回的是 JSON 字符串：连接池没有注册 jsonb 编解码器
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_postgres_reader_decodes_jsonb_artifact_refs_returned_as_text() -> None:
    """不解码就逐字符遍历字符串，制品会被整组静默丢弃并误判为证据不足。"""

    refs = json.dumps(
        [
            {
                "artifact_id": "artifact-screenshot",
                "task_id": "task-1",
                "agent_run_id": "run-1",
                "capture_id": "capture-1",
                "storage_path": "task-1/capture-1/screenshot",
                "artifact_type": "screenshot",
                "checksum": "sha256:" + "0" * 64,
                "size_bytes": 2048,
                "lifecycle_status": "archived",
            }
        ]
    )

    class Connection:
        async def fetch(self, query: str, *args):
            del query, args
            return [
                {
                    "capture_id": "capture-1",
                    "page_type": "home",
                    "page_state": "stable",
                    "detection_status": "recognized",
                    "capture_role": "after_action",
                    "url": "https://m.example.test/home",
                    "action_execution_id": "action-1",
                    "session_id": "h5:run-1",
                    "session_owner": H5_SURFACE_SESSION_OWNER,
                    "parent_capture_id": "before-capture-1",
                    "before_url": "about:blank",
                    "artifact_types": ["dom", "screenshot"],
                    "before_artifact_types": ["dom", "screenshot"],
                    "artifact_refs": refs,
                    "before_artifact_refs": refs,
                }
            ]

    class Acquire:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *args):
            return False

    class Pool:
        def acquire(self):
            return Acquire()

    captures = await PostgresH5EvidenceCaptureReader(pool=Pool()).list_judgeable_captures(
        task_id="task-1",
        agent_run_id="run-1",
    )

    (capture,) = captures
    assert [ref.artifact_type for ref in capture.artifact_refs] == ["screenshot"]
    assert capture.artifact_refs[0].size_bytes == 2048
    assert capture.artifact_refs[0].lifecycle_status == "archived"
    assert [ref.artifact_type for ref in capture.before_artifact_refs] == ["screenshot"]
    assert capture.before_artifact_refs[0].lifecycle_status == "archived"
