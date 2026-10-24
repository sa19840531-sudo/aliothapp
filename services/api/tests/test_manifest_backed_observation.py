"""E20：清单化读取链路与 ManifestReadDecision 审计边界测试。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.application.manifest_backed_observation import (
    MANIFEST_READ_AUDIT_KEY,
    ManifestBackedObservationRepository,
    ManifestReadOutcome,
    attach_manifest_read_decision,
)
from services.api.app.application.manifest_backed_packet import (
    ManifestBackedPacketLoader,
    ManifestIntegrityError,
    ManifestUnavailableError,
)
from services.api.app.application.observation_packet import (
    BuildObservationPacketRequest,
    ObservationArtifact,
    ObservationJourneyStep,
    ObservationPacketService,
    ObservationSource,
)
from services.api.app.domain.evidence import (
    ArtifactReference,
    EvidenceQualityStatus,
    build_evidence_manifest_reference,
)

NOW = datetime(2026, 8, 13, 14, 0, tzinfo=UTC)
TASK_ID = "11111111-1111-1111-1111-111111111111"
SESSION_ID = "surface-session-1"
CAPTURE_ID = "44444444-4444-4444-4444-444444444444"

VISIBLE_TEXT = "首页文案".encode()
SCREENSHOT = b"\x89PNG-screenshot-bytes"


def _reference(
    *,
    artifact_id: str,
    artifact_type: str,
    payload: bytes,
    role: str = "primary",
    content_type: str = "application/octet-stream",
) -> ArtifactReference:
    return ArtifactReference(
        artifact_id=artifact_id,
        artifact_type=artifact_type,
        role=role,
        storage_path=f"{TASK_ID}/{CAPTURE_ID}/{artifact_id}.data",
        checksum=f"sha256:{hashlib.sha256(payload).hexdigest()}",
        size_bytes=len(payload),
        content_type=content_type,
    )


TEXT_REF = _reference(
    artifact_id="artifact-text",
    artifact_type="visible_text",
    payload=VISIBLE_TEXT,
    content_type="text/plain; charset=utf-8",
)
SHOT_REF = _reference(
    artifact_id="artifact-shot",
    artifact_type="screenshot",
    payload=SCREENSHOT,
    content_type="image/png",
)


def _manifest(
    references: tuple[ArtifactReference, ...] = (TEXT_REF, SHOT_REF),
    *,
    quality_status: EvidenceQualityStatus = EvidenceQualityStatus.PASS,
):
    return build_evidence_manifest_reference(
        task_id=TASK_ID,
        session_id=SESSION_ID,
        capture_id=CAPTURE_ID,
        manifest_version=2,
        artifacts=references,
        quality_status=quality_status,
        quality_reason=(
            None if quality_status is EvidenceQualityStatus.PASS else "证据不足"
        ),
        created_at=NOW,
    )


class FakeManifestSource:
    def __init__(self, manifest=None) -> None:
        self.manifest = manifest

    async def get(self, *, manifest_id: str):
        return self.manifest

    async def get_latest_for_capture(
        self,
        *,
        task_id: str,
        capture_id: str,
        require_quality_pass: bool = False,
    ):
        if self.manifest is None or self.manifest.capture_id != capture_id:
            return None
        if (
            require_quality_pass
            and self.manifest.quality_status is not EvidenceQualityStatus.PASS
        ):
            return None
        return self.manifest


class FakeArtifactReader:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    async def read(self, storage_path: str) -> bytes:
        if storage_path not in self.objects:
            raise FileNotFoundError(storage_path)
        return self.objects[storage_path]


class FakeInnerRepository:
    """页面上下文来源；附件列表刻意与清单不同，用于证明来源已切换。"""

    def __init__(self, source: ObservationSource | None) -> None:
        self.source = source
        self.saved: list[dict[str, Any]] = []
        self.load_source_calls = 0
        self.context_calls = 0

    async def load_capture_context(self, *, task_id: str, page_capture_id: str):
        """E22：上下文读取不带附件（模拟 include_artifacts=False）。"""
        del task_id, page_capture_id
        self.context_calls += 1
        if self.source is None:
            return None
        return replace(
            self.source,
            artifacts=(),
            journey_steps=tuple(
                replace(step, artifacts=()) for step in self.source.journey_steps
            ),
        )

    async def load_source(self, *, task_id: str, page_capture_id: str):
        del task_id, page_capture_id
        self.load_source_calls += 1
        return self.source

    async def save_packet(self, **kwargs: Any) -> str:
        self.saved.append(kwargs)
        return "observation-packet-1"


def _context_source() -> ObservationSource:
    return ObservationSource(
        task_id=TASK_ID,
        page_capture_id=CAPTURE_ID,
        page_url="https://example.com/home",
        page_title="首页",
        page_type="home",
        page_state="loaded",
        action_log=({"action": "click"},),
        artifacts=(
            ObservationArtifact(
                artifact_id="stale-artifact",
                artifact_type="visible_text",
                content_type="text/plain",
                payload=b"stale-direct-table-read",
            ),
        ),
        surface_kind="web",
    )


_UNSET = object()


def _repository(
    *,
    manifest: Any = _UNSET,
    objects: dict[str, bytes] | None = None,
    context: Any = _UNSET,
    require_quality_pass: bool = True,
) -> tuple[ManifestBackedObservationRepository, FakeInnerRepository]:
    inner = FakeInnerRepository(
        _context_source() if context is _UNSET else context
    )
    loader = ManifestBackedPacketLoader(
        manifests=FakeManifestSource(_manifest() if manifest is _UNSET else manifest),
        artifact_reader=FakeArtifactReader(
            objects
            if objects is not None
            else {
                TEXT_REF.storage_path: VISIBLE_TEXT,
                SHOT_REF.storage_path: SCREENSHOT,
            }
        ),
        require_quality_pass=require_quality_pass,
    )
    repository = ManifestBackedObservationRepository(
        inner=inner,
        packet_loader=loader,
        now=lambda: NOW,
    )
    return repository, inner


async def test_load_source_replaces_artifacts_with_manifest_evidence() -> None:
    repository, _ = _repository()

    source = await repository.load_source(task_id=TASK_ID, page_capture_id=CAPTURE_ID)

    assert source is not None
    # 页面上下文沿用 Capture 事实
    assert source.page_url == "https://example.com/home"
    assert source.page_title == "首页"
    assert source.surface_kind == "web"
    assert source.action_log == ({"action": "click"},)
    # 证据来源已切换到清单：附件表的旧读取结果不再出现
    assert [item.artifact_id for item in source.artifacts] == [
        "artifact-text",
        "artifact-shot",
    ]
    assert all(item.artifact_id != "stale-artifact" for item in source.artifacts)
    assert source.artifacts[0].payload == VISIBLE_TEXT
    assert source.artifacts[0].content_type == "text/plain; charset=utf-8"


async def test_load_source_for_manifest_uses_exact_manifest_identity() -> None:
    manifest = _manifest()
    repository, _ = _repository(manifest=manifest)

    source = await repository.load_source_for_manifest(
        task_id=TASK_ID,
        page_capture_id=CAPTURE_ID,
        manifest_id=manifest.manifest_id,
    )

    assert source is not None
    assert source.page_capture_id == CAPTURE_ID
    assert source.manifest_quality is not None
    assert source.manifest_quality.manifest_id == manifest.manifest_id


@pytest.mark.parametrize("exact", [False, True])
@pytest.mark.parametrize("platform", ["h5", "web", "app", None])
async def test_h5_manifest_projection_preserves_platform_identity(exact: bool, platform) -> None:
    manifest = _manifest()
    repository, _ = _repository(
        manifest=manifest,
        context=replace(_context_source(), target_platform=platform),
    )
    if exact:
        source = await repository.load_source_for_manifest(
            task_id=TASK_ID, page_capture_id=CAPTURE_ID,
            manifest_id=manifest.manifest_id,
        )
    else:
        source = await repository.load_source(task_id=TASK_ID, page_capture_id=CAPTURE_ID)

    assert source is not None
    assert source.target_platform == ("h5" if platform == "h5" else None)
    assert source.surface_kind == "web"
    assert source.manifest_quality.manifest_id == manifest.manifest_id


async def test_load_source_for_manifest_rejects_capture_mismatch() -> None:
    manifest = _manifest()
    repository, _ = _repository(manifest=manifest)

    with pytest.raises(ManifestUnavailableError, match="不属于补采 Capture"):
        await repository.load_source_for_manifest(
            task_id=TASK_ID,
            page_capture_id="55555555-5555-5555-5555-555555555555",
            manifest_id=manifest.manifest_id,
        )


async def test_load_source_preserves_app_capture_facts_from_context() -> None:
    app_facts = {
        "package_name": "com.example",
        "width": 1080,
        "height": 1920,
        "device_snapshot": {"safe_area": {"source": "device_snapshot"}},
    }
    repository, _ = _repository(
        context=replace(
            _context_source(),
            surface_kind="android",
            app_capture_facts=app_facts,
        )
    )

    source = await repository.load_source(task_id=TASK_ID, page_capture_id=CAPTURE_ID)

    assert source is not None
    assert source.app_capture_facts == app_facts


async def test_every_read_records_checksum_comparison_decision() -> None:
    repository, _ = _repository()

    await repository.load_source(task_id=TASK_ID, page_capture_id=CAPTURE_ID)
    decision = repository.decision_for(task_id=TASK_ID, capture_id=CAPTURE_ID)

    assert decision is not None
    assert decision.outcome is ManifestReadOutcome.VERIFIED
    assert decision.verified is True
    assert decision.manifest_version == 2
    assert decision.quality_status is EvidenceQualityStatus.PASS
    assert decision.read_at == NOW
    assert len(decision.artifacts) == 2
    assert all(item.verified for item in decision.artifacts)
    assert {item.checksum for item in decision.artifacts} == {
        TEXT_REF.checksum,
        SHOT_REF.checksum,
    }


async def test_decision_never_reaches_packet_payload_or_model_input() -> None:
    """E20 验收核心：裁决只进审计载荷，不得进入会被模型读到的 packet_payload。"""
    repository, inner = _repository()
    service = ObservationPacketService(repository=repository)

    packet = await service.build(
        BuildObservationPacketRequest(
            task_id=TASK_ID,
            page_capture_id=CAPTURE_ID,
            rule_id="rule-1",
            rule_code="alioth_1",
            rule_name="首页红线",
            category="content",
            executor_type="hybrid",
            check_point="首页不得出现红线内容",
            input_contract={"required_artifacts": ["visible_text"]},
            judge_config={},
            manual_review_policy={},
            requested_evidence_artifact_ids=(),
            agent_input={},
        )
    )

    serialized = json.dumps(packet.packet_payload, ensure_ascii=False, default=str)
    assert MANIFEST_READ_AUDIT_KEY not in serialized
    assert "manifest-read-decision" not in serialized
    assert MANIFEST_READ_AUDIT_KEY not in packet.packet_payload
    # 落库载荷同样不含裁决
    assert inner.saved
    saved_payload = json.dumps(
        inner.saved[0]["packet_payload"], ensure_ascii=False, default=str
    )
    assert MANIFEST_READ_AUDIT_KEY not in saved_payload
    # 事实包本身仍按清单证据构建
    assert packet.packet_payload["facts"]["visible_text"] == "首页文案"


async def test_attach_manifest_read_decision_writes_into_audit_payload() -> None:
    repository, _ = _repository()
    await repository.load_source(task_id=TASK_ID, page_capture_id=CAPTURE_ID)
    decision = repository.decision_for(task_id=TASK_ID, capture_id=CAPTURE_ID)

    audit = attach_manifest_read_decision(
        {"schema_version": "rule-judge-input.v1", "observation_packet_id": "packet-1"},
        decision,
    )

    assert audit["observation_packet_id"] == "packet-1"
    recorded = audit[MANIFEST_READ_AUDIT_KEY]
    assert recorded["schema_version"] == "manifest-read-decision.v1"
    assert recorded["outcome"] == "verified"
    assert recorded["manifest_version"] == 2
    assert recorded["checksum_verified_count"] == 2
    assert recorded["artifact_count"] == 2
    assert {item["artifact_id"] for item in recorded["artifacts"]} == {
        "artifact-text",
        "artifact-shot",
    }
    # 审计投影不得携带证据原文
    assert "payload" not in json.dumps(recorded, ensure_ascii=False)


def test_attach_is_noop_without_decision() -> None:
    audit = {"schema_version": "rule-judge-input.v1"}

    assert attach_manifest_read_decision(audit, None) == audit


async def test_missing_manifest_records_decision_and_propagates() -> None:
    repository, _ = _repository(manifest=None)

    with pytest.raises(ManifestUnavailableError):
        await repository.load_source(task_id=TASK_ID, page_capture_id=CAPTURE_ID)

    decision = repository.decision_for(task_id=TASK_ID, capture_id=CAPTURE_ID)
    assert decision is not None
    assert decision.outcome is ManifestReadOutcome.CONTROL_PLANE_INCOMPLETE
    assert decision.verified is False
    assert decision.manifest_id is None
    assert decision.reason is not None
    payload = decision.to_audit_payload()
    assert payload["outcome"] == "control_plane_incomplete"
    assert payload["artifact_count"] == 0


async def test_tampered_evidence_records_integrity_failure_and_propagates() -> None:
    tampered = b"x" * len(VISIBLE_TEXT)
    repository, _ = _repository(
        objects={
            TEXT_REF.storage_path: tampered,
            SHOT_REF.storage_path: SCREENSHOT,
        }
    )

    with pytest.raises(ManifestIntegrityError):
        await repository.load_source(task_id=TASK_ID, page_capture_id=CAPTURE_ID)

    decision = repository.decision_for(task_id=TASK_ID, capture_id=CAPTURE_ID)
    assert decision is not None
    assert decision.outcome is ManifestReadOutcome.EVIDENCE_INTEGRITY_FAILED
    assert decision.to_audit_payload()["outcome"] == "evidence_integrity_failed"


async def test_failed_quality_manifest_maps_to_evidence_insufficient() -> None:
    """E02 条款 2.1：清单裁决 fail 才是真正的证据不足。"""
    repository, _ = _repository(
        manifest=_manifest(quality_status=EvidenceQualityStatus.FAIL)
    )

    with pytest.raises(ManifestUnavailableError):
        await repository.load_source(task_id=TASK_ID, page_capture_id=CAPTURE_ID)

    decision = repository.decision_for(task_id=TASK_ID, capture_id=CAPTURE_ID)
    assert decision is not None
    assert decision.outcome is ManifestReadOutcome.EVIDENCE_INSUFFICIENT
    assert decision.quality_status is EvidenceQualityStatus.FAIL


async def test_pending_quality_manifest_maps_to_evidence_pending() -> None:
    """E02 条款 2.1：在途不得报证据不足。"""
    repository, _ = _repository(
        manifest=_manifest(quality_status=EvidenceQualityStatus.PENDING)
    )

    with pytest.raises(ManifestUnavailableError):
        await repository.load_source(task_id=TASK_ID, page_capture_id=CAPTURE_ID)

    decision = repository.decision_for(task_id=TASK_ID, capture_id=CAPTURE_ID)
    assert decision is not None
    assert decision.outcome is ManifestReadOutcome.EVIDENCE_PENDING


async def test_absent_capture_context_skips_manifest_read() -> None:
    repository, _ = _repository(context=None)

    source = await repository.load_source(task_id=TASK_ID, page_capture_id=CAPTURE_ID)

    assert source is None
    assert repository.decision_for(task_id=TASK_ID, capture_id=CAPTURE_ID) is None


async def test_save_packet_delegates_without_touching_payload() -> None:
    repository, inner = _repository()
    payload = {"facts": {"visible_text": "首页文案"}}

    packet_id = await repository.save_packet(
        task_id=TASK_ID,
        page_capture_id=CAPTURE_ID,
        rule_id="rule-1",
        evidence_artifact_ids=("artifact-text",),
        packet_payload=payload,
        quality_gate_status="pass",
        quality_gate_reason=None,
        agent_run_id=None,
    )

    assert packet_id == "observation-packet-1"
    assert inner.saved[0]["packet_payload"] == payload
    assert MANIFEST_READ_AUDIT_KEY not in inner.saved[0]["packet_payload"]


async def test_audit_payload_bounds_artifact_records() -> None:
    payloads = {f"artifact-{index}": f"payload-{index}".encode() for index in range(60)}
    references = tuple(
        _reference(
            artifact_id=artifact_id,
            artifact_type="screenshot",
            payload=payload,
        )
        for artifact_id, payload in payloads.items()
    )
    objects = {
        reference.storage_path: payloads[reference.artifact_id]
        for reference in references
    }
    repository, _ = _repository(manifest=_manifest(references), objects=objects)

    await repository.load_source(task_id=TASK_ID, page_capture_id=CAPTURE_ID)
    decision = repository.decision_for(task_id=TASK_ID, capture_id=CAPTURE_ID)

    assert decision is not None
    recorded = decision.to_audit_payload()
    assert recorded["artifact_count"] == 60
    assert len(recorded["artifacts"]) == 50
    assert recorded["artifacts_truncated"] == 10
    assert recorded["checksum_verified_count"] == 60


async def test_context_read_uses_artifact_free_path() -> None:
    """E22：判定读取走 load_capture_context，不经带附件直查的 load_source。"""
    repository, inner = _repository()

    await repository.load_source(task_id=TASK_ID, page_capture_id=CAPTURE_ID)

    assert inner.context_calls == 1
    assert inner.load_source_calls == 0


async def test_journey_step_artifacts_are_sourced_from_manifests() -> None:
    """E22：旅程步骤证据同样按各自清单加载，不查附件表。"""
    context = replace(
        _context_source(),
        journey_steps=(
            ObservationJourneyStep(
                page_capture_id=CAPTURE_ID,
                page_seq=1,
                page_url="https://example.com/home",
                page_title="首页",
                page_type="home",
                page_state="loaded",
                action_log=(),
                artifacts=(
                    ObservationArtifact(
                        artifact_id="stale-journey",
                        artifact_type="visible_text",
                        content_type="text/plain",
                        payload=b"stale-direct-table-read",
                    ),
                ),
            ),
        ),
    )
    repository, _ = _repository(context=context)

    source = await repository.load_source(task_id=TASK_ID, page_capture_id=CAPTURE_ID)

    assert source is not None
    step = source.journey_steps[0]
    assert [item.artifact_id for item in step.artifacts] == [
        "artifact-text",
        "artifact-shot",
    ]
    assert all(item.artifact_id != "stale-journey" for item in step.artifacts)


async def test_journey_step_without_manifest_keeps_empty_evidence() -> None:
    """历史步骤缺清单不否决当前页判定，只留空证据。"""
    other_capture = "66666666-6666-6666-6666-666666666666"
    context = replace(
        _context_source(),
        journey_steps=(
            ObservationJourneyStep(
                page_capture_id=other_capture,
                page_seq=1,
                page_url="https://example.com/old",
                page_title=None,
                page_type=None,
                page_state=None,
                action_log=(),
                artifacts=(),
            ),
        ),
    )
    repository, _ = _repository(context=context)

    source = await repository.load_source(task_id=TASK_ID, page_capture_id=CAPTURE_ID)

    assert source is not None
    assert source.journey_steps[0].artifacts == ()
    # 当前页证据仍然可信
    assert [item.artifact_id for item in source.artifacts] == [
        "artifact-text",
        "artifact-shot",
    ]


async def test_packet_projects_manifest_verdict_without_recomputing() -> None:
    """E02 条款 1.1/1.4：证据来自清单时齐全性只投影不重算。"""
    repository, inner = _repository()
    service = ObservationPacketService(repository=repository)

    packet = await service.build(
        BuildObservationPacketRequest(
            task_id=TASK_ID,
            page_capture_id=CAPTURE_ID,
            rule_id="rule-1",
            rule_code="alioth_1",
            rule_name="首页红线",
            category="content",
            executor_type="hybrid",
            check_point="首页不得出现红线内容",
            # 规则声明了清单里没有的证据类型：若重算齐全性必然 fail
            input_contract={"required_artifacts": ["dom", "har", "console_log"]},
            judge_config={},
            manual_review_policy={},
            requested_evidence_artifact_ids=(),
            agent_input={},
        )
    )

    quality_gate = packet.packet_payload["quality_gate"]
    assert quality_gate["status"] == "pass"
    assert quality_gate["source"] == "evidence_manifest"
    assert quality_gate["manifest_version"] == 2
    assert quality_gate["missing_sources"] == []
    assert packet.quality_gate_status == "pass"
    assert inner.saved[0]["quality_gate_status"] == "pass"


async def test_pending_manifest_readable_when_pass_not_required() -> None:
    repository, _ = _repository(
        manifest=_manifest(quality_status=EvidenceQualityStatus.PENDING),
        require_quality_pass=False,
    )

    source = await repository.load_source(task_id=TASK_ID, page_capture_id=CAPTURE_ID)
    decision = repository.decision_for(task_id=TASK_ID, capture_id=CAPTURE_ID)

    assert source is not None
    assert decision is not None
    assert decision.outcome is ManifestReadOutcome.VERIFIED
    assert decision.quality_status is EvidenceQualityStatus.PENDING
