"""E22 收口：规则引擎取证改由清单来源，附件表不参与判定读取。"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.agents.rule_judge.maf_agent import MafRuleJudgeAgent
from services.api.app.application.inspection_handoff import build_inspection_handoff
from services.api.app.application.inspection_work_items import (
    InspectionRuleEvidenceBinding,
    InspectionWorkItemLease,
    InspectionWorkItemRule,
)
from services.api.app.application.manifest_backed_packet import (
    ManifestFactArtifact,
    ManifestFactPacket,
    ManifestUnavailableError,
)
from services.api.app.application.rule_judge import (
    RuleJudgeEvidenceInsufficientError,
    RuleJudgeResult,
    RuleJudgeValidationError,
)
from services.api.app.domain.evidence import EvidenceQualityStatus
from services.api.app.domain.inspection import EvidenceBundleReference
from services.api.app.infrastructure.rule_judge_evidence import (
    PostgresRuleJudgeEvidenceReader,
)

NOW = datetime(2026, 8, 14, 13, 0, tzinfo=UTC)
TASK_ID = "11111111-1111-1111-1111-111111111111"
CAPTURE_ID = "44444444-4444-4444-4444-444444444444"
RULE_ID = "33333333-3333-3333-3333-333333333333"


def _fact(
    *,
    artifact_id: str,
    artifact_type: str,
    payload: bytes = b"evidence",
) -> ManifestFactArtifact:
    return ManifestFactArtifact(
        artifact_id=artifact_id,
        artifact_type=artifact_type,
        role="primary",
        checksum=f"sha256:{hashlib.sha256(payload).hexdigest()}",
        size_bytes=len(payload),
        payload=payload,
        content_type="text/plain",
    )


def _packet(
    artifacts: tuple[ManifestFactArtifact, ...],
    *,
    quality_status: EvidenceQualityStatus = EvidenceQualityStatus.PASS,
    capture_id: str = CAPTURE_ID,
    manifest_id: str = "evidence-manifest:abc",
) -> ManifestFactPacket:
    return ManifestFactPacket(
        task_id=TASK_ID,
        session_id="surface-session-1",
        capture_id=capture_id,
        manifest_id=manifest_id,
        manifest_version=2,
        manifest_hash="hash-abc",
        quality_status=quality_status,
        artifacts=artifacts,
    )


class FakeLoader:
    def __init__(self, packet: ManifestFactPacket | None, error: Exception | None = None):
        self.packet = packet
        self.error = error
        self.calls: list[str] = []
        self.manifest_calls: list[str] = []

    async def load_for_capture(self, *, task_id: str, capture_id: str):
        del task_id
        self.calls.append(capture_id)
        if self.error is not None:
            raise self.error
        return self.packet

    async def load_for_manifest(self, *, task_id: str, manifest_id: str):
        del task_id
        self.manifest_calls.append(manifest_id)
        if self.error is not None:
            raise self.error
        return self.packet


class RecordingConnection:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.args: list[tuple[Any, ...]] = []

    async def fetch(self, query: str, *args: Any) -> list[Any]:
        self.queries.append(" ".join(query.split()))
        self.args.append(args)
        return []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any]:
        self.queries.append(" ".join(query.split()))
        self.args.append(args)
        # 观察包插入是唯一的 fetchrow：按"新插入成功"回显写入内容，与真实 RETURNING 一致
        return {
            "observation_packet_id": "packet-1",
            "evidence_artifact_ids": args[3],
            "packet_payload": args[4],
            "quality_gate_status": args[6],
            "quality_gate_reason": args[7],
        }


class FakeAcquire:
    def __init__(self, connection: RecordingConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> RecordingConnection:
        return self.connection

    async def __aexit__(self, *args: Any) -> None:
        return None


class FakePool:
    def __init__(self, connection: RecordingConnection) -> None:
        self.connection = connection

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.connection)


def _reader(loader: FakeLoader, connection: RecordingConnection):
    return PostgresRuleJudgeEvidenceReader(
        pool=FakePool(connection),
        artifact_store=object(),  # type: ignore[arg-type]
        manifest_loader=loader,
    )


async def test_manifest_source_never_queries_artifact_table() -> None:
    """E22 验收：判定生产路径不存在对附件表的直接查询。"""
    connection = RecordingConnection()
    loader = FakeLoader(
        _packet((_fact(artifact_id="a-1", artifact_type="visible_text"),))
    )
    reader = _reader(loader, connection)

    bundle = await reader.prepare_observation_packet(
        task_id=TASK_ID,
        agent_run_id=None,
        rule_id=RULE_ID,
        page_capture_id=CAPTURE_ID,
        evidence_capture_roles=None,
        evidence_artifact_ids=(),
        required_artifact_types=("visible_text",),
    )

    assert loader.calls == [CAPTURE_ID]
    assert all("qc_evidence_artifact" not in query for query in connection.queries)
    assert [item.artifact_id for item in bundle.artifacts] == ["a-1"]
    assert bundle.artifacts[0].capture_role == "primary"


async def test_exact_supplement_manifest_is_read_but_packet_keeps_result_target() -> None:
    connection = RecordingConnection()
    supplemental_capture_id = "55555555-5555-5555-5555-555555555555"
    loader = FakeLoader(
        _packet(
            (_fact(artifact_id="a-2", artifact_type="visible_text"),),
            capture_id=supplemental_capture_id,
        )
    )
    reader = _reader(loader, connection)

    bundle = await reader.prepare_observation_packet(
        task_id=TASK_ID,
        agent_run_id=None,
        rule_id=RULE_ID,
        page_capture_id=CAPTURE_ID,
        evidence_source_page_capture_id=supplemental_capture_id,
        evidence_manifest_id="evidence-manifest:abc",
        evidence_capture_roles={"primary": supplemental_capture_id},
        evidence_artifact_ids=("a-2",),
        required_artifact_types=("visible_text",),
    )

    assert loader.calls == []
    assert loader.manifest_calls == ["evidence-manifest:abc"]
    assert bundle.page_capture_id == CAPTURE_ID
    assert bundle.capture_roles == {"primary": supplemental_capture_id}
    assert connection.args[-1][2] == CAPTURE_ID


async def test_exact_supplement_manifest_keeps_frozen_before_and_after_roles() -> None:
    before_capture_id = "66666666-6666-6666-6666-666666666666"
    after_capture_id = "77777777-7777-7777-7777-777777777777"

    class MultiRoleLoader(FakeLoader):
        def __init__(self) -> None:
            super().__init__(None)
            self.packets = {
                "before-manifest": _packet(
                    (
                        _fact(artifact_id="before-shot", artifact_type="visible_text"),
                        _fact(artifact_id="before-extra", artifact_type="screenshot"),
                    ),
                    capture_id=before_capture_id,
                    manifest_id="before-manifest",
                ),
                "after-manifest": _packet(
                    (
                        _fact(artifact_id="after-shot", artifact_type="visible_text"),
                        _fact(artifact_id="after-extra", artifact_type="screenshot"),
                    ),
                    capture_id=after_capture_id,
                    manifest_id="after-manifest",
                ),
            }

        async def load_for_capture(self, *, task_id: str, capture_id: str):
            del task_id
            self.calls.append(capture_id)
            raise AssertionError("冻结多角色输入不应退回按 Capture 读取最新清单")

        async def load_for_manifest(self, *, task_id: str, manifest_id: str):
            del task_id
            self.manifest_calls.append(manifest_id)
            return self.packets[manifest_id]

    connection = RecordingConnection()
    loader = MultiRoleLoader()
    reader = _reader(loader, connection)

    bundle = await reader.prepare_observation_packet(
        task_id=TASK_ID,
        agent_run_id=None,
        rule_id=RULE_ID,
        page_capture_id=CAPTURE_ID,
        evidence_source_page_capture_id=after_capture_id,
        evidence_manifest_id="after-manifest",
        evidence_manifest_refs={
            "before": {
                "manifest_id": "before-manifest",
                "manifest_version": "2",
            },
            "primary": {
                "manifest_id": "after-manifest",
                "manifest_version": "2",
            },
        },
        evidence_capture_roles={
            "before": before_capture_id,
            "primary": after_capture_id,
        },
        evidence_artifact_ids=("before-shot", "after-shot"),
        required_artifact_types=("visible_text",),
    )

    assert loader.calls == []
    assert loader.manifest_calls == ["before-manifest", "after-manifest"]
    assert bundle.capture_roles == {
        "before": before_capture_id,
        "primary": after_capture_id,
    }
    assert [item.artifact_id for item in bundle.artifacts] == [
        "before-shot",
        "after-shot",
    ]


async def test_ordinary_multi_role_manifest_keeps_legacy_type_projection() -> None:
    """普通多角色输入沿用按必需类型投影的旧行为，不新增角色非空限制。"""
    before_capture_id = "66666666-6666-6666-6666-666666666666"
    primary_capture_id = "77777777-7777-7777-7777-777777777777"

    class LegacyMultiRoleLoader(FakeLoader):
        def __init__(self) -> None:
            super().__init__(None)
            self.packets = {
                before_capture_id: _packet(
                    (_fact(artifact_id="before-shot", artifact_type="screenshot"),),
                    capture_id=before_capture_id,
                    manifest_id="before-manifest",
                ),
                primary_capture_id: _packet(
                    (_fact(artifact_id="primary-text", artifact_type="visible_text"),),
                    capture_id=primary_capture_id,
                    manifest_id="primary-manifest",
                ),
            }

        async def load_for_capture(self, *, task_id: str, capture_id: str):
            del task_id
            self.calls.append(capture_id)
            return self.packets[capture_id]

    loader = LegacyMultiRoleLoader()
    reader = _reader(loader, RecordingConnection())

    bundle = await reader.prepare_observation_packet(
        task_id=TASK_ID,
        agent_run_id=None,
        rule_id=RULE_ID,
        page_capture_id=primary_capture_id,
        evidence_capture_roles={
            "before": before_capture_id,
            "primary": primary_capture_id,
        },
        evidence_artifact_ids=(),
        required_artifact_types=("visible_text",),
    )

    assert loader.calls == [before_capture_id, primary_capture_id]
    assert [item.artifact_id for item in bundle.artifacts] == ["primary-text"]


@pytest.mark.parametrize("explicit_baseline_artifacts", [False, True])
async def test_built_handoff_to_maf_and_reader_keeps_before_material(
    explicit_baseline_artifacts: bool,
) -> None:
    """补采 primary 替换时，真实交接链不能丢 before，也不能带回旧 primary。"""
    before_capture_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    baseline_primary_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    after_capture_id = "cccccccc-cccc-cccc-cccc-cccccccccccc"

    lease = InspectionWorkItemLease(
        work_item_id="work-1",
        task_id=TASK_ID,
        page_capture_id=baseline_primary_id,
        batch_key="hybrid:visual:snapshot",
        strategy="hybrid",
        category="visual",
        session_dependency="snapshot",
        input_hash="input-1",
        producer_agent_run_id="producer-run-1",
        consumer_agent_run_id="consumer-run-1",
        lease_token="lease-token",
        attempt_no=1,
        row_version=1,
        rules=(
            InspectionWorkItemRule(
                rule_id=RULE_ID,
                rule_code="alioth_visual_1",
                ordinal=0,
                contract_hash="contract-1",
            ),
        ),
        rule_evidence=(
            InspectionRuleEvidenceBinding(
                rule_id=RULE_ID,
                rule_code="alioth_visual_1",
                page_capture_id=after_capture_id,
                producer_agent_run_id="producer-run-1",
                manifest_id="after-manifest",
                manifest_version=2,
                artifact_ids=("after-shot",),
            ),
        ),
    )
    handoff = build_inspection_handoff(
        lease,
        attempt_id="attempt-1",
        evidence_capture_roles={
            "before": before_capture_id,
            "primary": baseline_primary_id,
        },
    )
    if explicit_baseline_artifacts:
        handoff = replace(
            handoff,
            evidence=EvidenceBundleReference(
                page_capture_id=baseline_primary_id,
                producer_agent_run_id="producer-run-1",
                artifact_ids=("before-shot", "old-primary-shot"),
                evidence_capture_roles={
                    "before": before_capture_id,
                    "primary": baseline_primary_id,
                },
                evidence_manifest_refs={
                    "before": {
                        "manifest_id": "before-manifest",
                        "manifest_version": "2",
                    },
                    "primary": {
                        "manifest_id": "old-primary-manifest",
                        "manifest_version": "1",
                    },
                },
            ),
        )

    evidence = handoff.evidence_for_rule("alioth_visual_1")
    assert evidence.evidence_capture_roles == {
        "before": before_capture_id,
        "primary": after_capture_id,
    }
    assert evidence.artifact_ids == ("after-shot",)
    assert "old-primary-shot" not in evidence.artifact_ids

    class RecordingJudgeService:
        def __init__(self) -> None:
            self.requests = []

        async def judge(self, request):
            self.requests.append(request)
            return RuleJudgeResult(
                rule_id=RULE_ID,
                rule_code="alioth_visual_1",
                executor_type="rule_engine",
                judge_strategy="rule_engine",
                execution_status="pass",
                confidence=1.0,
                result_summary="证据满足规则。",
                problem_desc=None,
                page_capture_id=request.page_capture_id,
                evidence_artifact_ids=request.evidence_artifact_ids,
                judge_input={},
                judge_output={"conclusion": "pass"},
            )

    judge_service = RecordingJudgeService()
    outcome = await MafRuleJudgeAgent(service=judge_service).judge(handoff)  # type: ignore[arg-type]
    assert outcome.decisions[0].execution_status == "pass"
    request = judge_service.requests[0]
    assert request.evidence_artifact_ids == ("after-shot",)
    if explicit_baseline_artifacts:
        assert request.retained_baseline_artifact_ids == {
            "before": ("before-shot", "old-primary-shot")
        }
    else:
        assert request.retained_baseline_artifact_ids is None

    class ChainLoader(FakeLoader):
        def __init__(self) -> None:
            super().__init__(None)
            self.by_capture = {
                before_capture_id: _packet(
                    (
                        _fact(artifact_id="before-shot", artifact_type="visible_text"),
                        _fact(artifact_id="before-extra", artifact_type="screenshot"),
                    ),
                    capture_id=before_capture_id,
                    manifest_id="before-manifest",
                )
            }
            self.by_manifest = {
                "after-manifest": _packet(
                    (_fact(artifact_id="after-shot", artifact_type="visible_text"),),
                    capture_id=after_capture_id,
                    manifest_id="after-manifest",
                ),
                "before-manifest": self.by_capture[before_capture_id],
            }

        async def load_for_capture(self, *, task_id: str, capture_id: str):
            del task_id
            self.calls.append(capture_id)
            return self.by_capture[capture_id]

        async def load_for_manifest(self, *, task_id: str, manifest_id: str):
            del task_id
            self.manifest_calls.append(manifest_id)
            return self.by_manifest[manifest_id]

    class OwnershipConnection(RecordingConnection):
        async def fetch(self, query: str, *args: Any) -> list[Any]:
            normalized = " ".join(query.split())
            self.queries.append(normalized)
            self.args.append(args)
            if "agent_run_id::text AS agent_run_id" in normalized:
                return [
                    {"capture_id": before_capture_id, "agent_run_id": "producer-run-1"},
                    {"capture_id": after_capture_id, "agent_run_id": "producer-run-1"},
                ]
            return []

    loader = ChainLoader()
    reader = _reader(loader, OwnershipConnection())
    bundle = await reader.prepare_observation_packet(
        task_id=request.task_id,
        agent_run_id=request.agent_run_id,
        source_agent_run_id=request.source_agent_run_id,
        rule_id=RULE_ID,
        page_capture_id=request.page_capture_id,
        evidence_source_page_capture_id=request.evidence_source_page_capture_id,
        evidence_manifest_id=request.evidence_manifest_id,
        evidence_manifest_refs=request.evidence_manifest_refs,
        retained_baseline_artifact_ids=request.retained_baseline_artifact_ids,
        evidence_capture_roles=request.evidence_capture_roles,
        evidence_artifact_ids=request.evidence_artifact_ids,
        required_artifact_types=("visible_text",),
    )

    expected_artifact_ids = ["before-shot", "after-shot"]
    if not explicit_baseline_artifacts:
        expected_artifact_ids.insert(1, "before-extra")
    assert [item.artifact_id for item in bundle.artifacts] == expected_artifact_ids
    assert "old-primary-shot" not in [item.artifact_id for item in bundle.artifacts]
    if explicit_baseline_artifacts:
        assert loader.calls == []
        assert loader.manifest_calls == ["before-manifest", "after-manifest"]
    else:
        assert loader.calls == [before_capture_id]
        assert loader.manifest_calls == ["after-manifest"]


@pytest.mark.parametrize("manifest_read_mode", ["off", "shadow"])
@pytest.mark.parametrize("explicit_baseline_artifacts", [False, True])
async def test_built_handoff_to_legacy_reader_keeps_before_material(
    manifest_read_mode: str,
    explicit_baseline_artifacts: bool,
) -> None:
    """off/shadow 旧附件读取也必须投影补采保留的 before 角色。"""
    before_capture_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    baseline_primary_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    after_capture_id = "cccccccc-cccc-cccc-cccc-cccccccccccc"

    lease = InspectionWorkItemLease(
        work_item_id="work-legacy-1",
        task_id=TASK_ID,
        page_capture_id=baseline_primary_id,
        batch_key="hybrid:visual:snapshot",
        strategy="hybrid",
        category="visual",
        session_dependency="snapshot",
        input_hash="input-legacy-1",
        producer_agent_run_id="producer-run-1",
        consumer_agent_run_id="consumer-run-1",
        lease_token="lease-token",
        attempt_no=1,
        row_version=1,
        rules=(
            InspectionWorkItemRule(
                rule_id=RULE_ID,
                rule_code="alioth_visual_legacy_1",
                ordinal=0,
                contract_hash="contract-legacy-1",
            ),
        ),
        rule_evidence=(
            InspectionRuleEvidenceBinding(
                rule_id=RULE_ID,
                rule_code="alioth_visual_legacy_1",
                page_capture_id=after_capture_id,
                producer_agent_run_id="producer-run-1",
                manifest_id="after-manifest",
                manifest_version=2,
                artifact_ids=("after-shot",),
            ),
        ),
    )
    handoff = build_inspection_handoff(
        lease,
        attempt_id="attempt-legacy-1",
        evidence_capture_roles={
            "before": before_capture_id,
            "primary": baseline_primary_id,
        },
    )
    if explicit_baseline_artifacts:
        handoff = replace(
            handoff,
            evidence=EvidenceBundleReference(
                page_capture_id=baseline_primary_id,
                producer_agent_run_id="producer-run-1",
                artifact_ids=("before-shot", "old-primary-shot"),
                evidence_capture_roles={
                    "before": before_capture_id,
                    "primary": baseline_primary_id,
                },
                evidence_manifest_refs={
                    "before": {
                        "manifest_id": "before-manifest",
                        "manifest_version": "2",
                    },
                    "primary": {
                        "manifest_id": "old-primary-manifest",
                        "manifest_version": "1",
                    },
                },
            ),
        )

    class RecordingJudgeService:
        def __init__(self) -> None:
            self.requests = []

        async def judge(self, request):
            self.requests.append(request)
            return RuleJudgeResult(
                rule_id=RULE_ID,
                rule_code="alioth_visual_legacy_1",
                executor_type="rule_engine",
                judge_strategy="rule_engine",
                execution_status="pass",
                confidence=1.0,
                result_summary="证据满足规则。",
                problem_desc=None,
                page_capture_id=request.page_capture_id,
                evidence_artifact_ids=request.evidence_artifact_ids,
                judge_input={},
                judge_output={"conclusion": "pass"},
            )

    judge_service = RecordingJudgeService()
    await MafRuleJudgeAgent(service=judge_service).judge(handoff)  # type: ignore[arg-type]
    request = judge_service.requests[0]
    assert request.evidence_artifact_ids == ("after-shot",)

    def legacy_row(artifact_id: str, capture_id: str, payload: bytes) -> dict[str, Any]:
        return {
            "artifact_id": artifact_id,
            "page_capture_id": capture_id,
            "artifact_type": "visible_text",
            "source": "baseline",
            "storage_path": f"{TASK_ID}/{capture_id}/{artifact_id}.txt",
            "content_type": "text/plain",
            "checksum": f"sha256:{hashlib.sha256(payload).hexdigest()}",
            "size_bytes": len(payload),
        }

    before_payload = b"before legacy evidence"
    before_extra_payload = b"before legacy extra evidence"
    after_payload = b"after legacy evidence"
    rows = [
        legacy_row("before-shot", before_capture_id, before_payload),
        legacy_row("before-extra", before_capture_id, before_extra_payload),
        legacy_row("after-shot", after_capture_id, after_payload),
    ]

    class LegacyConnection(RecordingConnection):
        async def fetch(self, query: str, *args: Any) -> list[Any]:
            normalized = " ".join(query.split())
            self.queries.append(normalized)
            self.args.append(args)
            if "agent_run_id::text AS agent_run_id" in normalized:
                return [
                    {"capture_id": before_capture_id, "agent_run_id": "producer-run-1"},
                    {"capture_id": after_capture_id, "agent_run_id": "producer-run-1"},
                ]
            if "qc_evidence_artifact" in normalized:
                return rows
            return []

    class LegacyArtifactStore:
        async def read(self, storage_path: str) -> bytes:
            return {
                str(rows[0]["storage_path"]): before_payload,
                str(rows[1]["storage_path"]): before_extra_payload,
                str(rows[2]["storage_path"]): after_payload,
            }[storage_path]

    connection = LegacyConnection()
    reader = PostgresRuleJudgeEvidenceReader(
        pool=FakePool(connection),
        artifact_store=LegacyArtifactStore(),  # type: ignore[arg-type]
        manifest_loader=None,
    )
    bundle = await reader.prepare_observation_packet(
        task_id=request.task_id,
        agent_run_id=request.agent_run_id,
        source_agent_run_id=request.source_agent_run_id,
        rule_id=RULE_ID,
        page_capture_id=request.page_capture_id,
        evidence_source_page_capture_id=request.evidence_source_page_capture_id,
        evidence_manifest_id=request.evidence_manifest_id,
        evidence_manifest_refs=request.evidence_manifest_refs,
        retained_baseline_artifact_ids=request.retained_baseline_artifact_ids,
        evidence_capture_roles=request.evidence_capture_roles,
        evidence_artifact_ids=request.evidence_artifact_ids,
        required_artifact_types=("visible_text",),
    )

    expected_artifact_ids = ["before-shot", "after-shot"]
    if not explicit_baseline_artifacts:
        expected_artifact_ids.insert(1, "before-extra")
    assert [item.artifact_id for item in bundle.artifacts] == expected_artifact_ids
    assert "old-primary-shot" not in [item.artifact_id for item in bundle.artifacts]
    artifact_query = next(
        query for query in connection.queries if "qc_evidence_artifact" in query
    )
    assert "evidence.page_capture_id = ANY($2::uuid[])" in artifact_query
    assert manifest_read_mode in {"off", "shadow"}


async def test_role_manifest_rejects_packet_for_another_capture() -> None:
    requested_capture_id = "88888888-8888-8888-8888-888888888888"
    foreign_capture_id = "99999999-9999-9999-9999-999999999999"
    loader = FakeLoader(
        _packet(
            (_fact(artifact_id="foreign-shot", artifact_type="visible_text"),),
            capture_id=foreign_capture_id,
            manifest_id="requested-manifest",
        )
    )
    reader = _reader(loader, RecordingConnection())

    with pytest.raises(RuleJudgeValidationError, match="不属于声明的证据 Capture"):
        await reader.prepare_observation_packet(
            task_id=TASK_ID,
            agent_run_id=None,
            rule_id=RULE_ID,
            page_capture_id=requested_capture_id,
            evidence_source_page_capture_id=requested_capture_id,
            evidence_manifest_refs={
                "primary": {
                    "manifest_id": "requested-manifest",
                    "manifest_version": "2",
                }
            },
            evidence_capture_roles={"primary": requested_capture_id},
            evidence_artifact_ids=("foreign-shot",),
            required_artifact_types=("visible_text",),
        )


async def test_packet_quality_projects_manifest_verdict() -> None:
    """E02 条款 1.4：判定侧不再硬编码 pass，只投影清单结论。"""
    connection = RecordingConnection()
    loader = FakeLoader(
        _packet(
            (_fact(artifact_id="a-1", artifact_type="visible_text"),),
            quality_status=EvidenceQualityStatus.PENDING,
        )
    )
    reader = _reader(loader, connection)

    await reader.prepare_observation_packet(
        task_id=TASK_ID,
        agent_run_id=None,
        rule_id=RULE_ID,
        page_capture_id=CAPTURE_ID,
        evidence_capture_roles=None,
        evidence_artifact_ids=(),
        required_artifact_types=("visible_text",),
    )

    insert_args = connection.args[-1]
    assert insert_args[6] == "pending"
    assert "投影自证据清单裁决" in insert_args[7]


async def test_pass_manifest_keeps_pass_packet_status() -> None:
    connection = RecordingConnection()
    loader = FakeLoader(
        _packet((_fact(artifact_id="a-1", artifact_type="visible_text"),))
    )
    reader = _reader(loader, connection)

    await reader.prepare_observation_packet(
        task_id=TASK_ID,
        agent_run_id=None,
        rule_id=RULE_ID,
        page_capture_id=CAPTURE_ID,
        evidence_capture_roles=None,
        evidence_artifact_ids=(),
        required_artifact_types=("visible_text",),
    )

    assert connection.args[-1][6] == "pass"


async def test_missing_manifest_maps_to_evidence_insufficient() -> None:
    connection = RecordingConnection()
    loader = FakeLoader(None, error=ManifestUnavailableError("没有可用证据清单"))
    reader = _reader(loader, connection)

    with pytest.raises(RuleJudgeEvidenceInsufficientError, match="证据清单不可用"):
        await reader.prepare_observation_packet(
            task_id=TASK_ID,
            agent_run_id=None,
            rule_id=RULE_ID,
            page_capture_id=CAPTURE_ID,
            evidence_capture_roles=None,
            evidence_artifact_ids=(),
            required_artifact_types=("visible_text",),
        )


async def test_missing_required_type_is_evidence_insufficient() -> None:
    connection = RecordingConnection()
    loader = FakeLoader(
        _packet((_fact(artifact_id="a-1", artifact_type="screenshot"),))
    )
    reader = _reader(loader, connection)

    with pytest.raises(RuleJudgeEvidenceInsufficientError, match="缺少必需证据类型"):
        await reader.prepare_observation_packet(
            task_id=TASK_ID,
            agent_run_id=None,
            rule_id=RULE_ID,
            page_capture_id=CAPTURE_ID,
            evidence_capture_roles=None,
            evidence_artifact_ids=(),
            required_artifact_types=("visible_text",),
        )


async def test_requested_artifact_absent_from_manifest_is_rejected() -> None:
    connection = RecordingConnection()
    loader = FakeLoader(
        _packet((_fact(artifact_id="a-1", artifact_type="visible_text"),))
    )
    reader = _reader(loader, connection)

    with pytest.raises(RuleJudgeValidationError, match="未出现在对应 Capture 的清单中"):
        await reader.prepare_observation_packet(
            task_id=TASK_ID,
            agent_run_id=None,
            rule_id=RULE_ID,
            page_capture_id=CAPTURE_ID,
            evidence_capture_roles=None,
            evidence_artifact_ids=("a-missing",),
            required_artifact_types=(),
        )


async def test_requested_artifact_ids_filter_manifest_contents() -> None:
    connection = RecordingConnection()
    loader = FakeLoader(
        _packet(
            (
                _fact(artifact_id="a-1", artifact_type="visible_text"),
                _fact(artifact_id="a-2", artifact_type="screenshot"),
            )
        )
    )
    reader = _reader(loader, connection)

    bundle = await reader.prepare_observation_packet(
        task_id=TASK_ID,
        agent_run_id=None,
        rule_id=RULE_ID,
        page_capture_id=CAPTURE_ID,
        evidence_capture_roles=None,
        evidence_artifact_ids=("a-2",),
        required_artifact_types=(),
    )

    assert [item.artifact_id for item in bundle.artifacts] == ["a-2"]


async def test_legacy_path_still_used_without_loader() -> None:
    """未配置清单加载器时沿用旧读取，默认 off 行为不变。"""
    connection = RecordingConnection()
    reader = PostgresRuleJudgeEvidenceReader(
        pool=FakePool(connection),
        artifact_store=object(),  # type: ignore[arg-type]
    )

    with pytest.raises(RuleJudgeValidationError):
        await reader.prepare_observation_packet(
            task_id=TASK_ID,
            agent_run_id=None,
            rule_id=RULE_ID,
            page_capture_id=CAPTURE_ID,
            evidence_capture_roles=None,
            evidence_artifact_ids=("a-1",),
            required_artifact_types=(),
        )

    assert any("qc_evidence_artifact" in query for query in connection.queries)


async def test_empty_contract_keeps_all_manifest_artifacts() -> None:
    """空证据契约按类型过滤会滤空清单，与附件表读取路径必须同口径。"""
    connection = RecordingConnection()
    loader = FakeLoader(
        _packet(
            (
                _fact(artifact_id="a-1", artifact_type="visible_text"),
                _fact(artifact_id="a-2", artifact_type="screenshot"),
            )
        )
    )
    reader = _reader(loader, connection)

    bundle = await reader.prepare_observation_packet(
        task_id=TASK_ID,
        agent_run_id=None,
        rule_id=RULE_ID,
        page_capture_id=CAPTURE_ID,
        evidence_capture_roles=None,
        evidence_artifact_ids=(),
        required_artifact_types=(),
    )

    assert [item.artifact_id for item in bundle.artifacts] == ["a-1", "a-2"]
