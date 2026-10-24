"""E21：无清单或校验失败时的拒判通路与审计告警测试。"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.application.manifest_backed_observation import (
    ManifestBackedObservationRepository,
    ManifestReadDecision,
    ManifestReadOutcome,
)
from services.api.app.application.manifest_backed_packet import ManifestBackedPacketLoader
from services.api.app.application.manifest_read_guard import (
    ALERT_CODE_EVIDENCE_INTEGRITY_FAILED,
    InMemoryManifestReadAlertSink,
    LoggingManifestReadAlertSink,
    ManifestReadAlert,
    ManifestReadGuard,
    load_verified_source,
)
from services.api.app.application.observation_packet import (
    ObservationArtifact,
    ObservationSource,
)
from services.api.app.domain.evidence import (
    ArtifactReference,
    EvidenceQualityStatus,
    build_evidence_manifest_reference,
)

NOW = datetime(2026, 8, 13, 15, 0, tzinfo=UTC)
TASK_ID = "11111111-1111-1111-1111-111111111111"
SESSION_ID = "surface-session-1"
CAPTURE_ID = "44444444-4444-4444-4444-444444444444"
PAYLOAD = "首页文案".encode()


def _reference() -> ArtifactReference:
    return ArtifactReference(
        artifact_id="artifact-text",
        artifact_type="visible_text",
        role="primary",
        storage_path=f"{TASK_ID}/{CAPTURE_ID}/artifact-text.data",
        checksum=f"sha256:{hashlib.sha256(PAYLOAD).hexdigest()}",
        size_bytes=len(PAYLOAD),
        content_type="text/plain; charset=utf-8",
    )


def _manifest(quality_status: EvidenceQualityStatus = EvidenceQualityStatus.PASS):
    return build_evidence_manifest_reference(
        task_id=TASK_ID,
        session_id=SESSION_ID,
        capture_id=CAPTURE_ID,
        manifest_version=3,
        artifacts=(_reference(),),
        quality_status=quality_status,
        quality_reason=(
            None if quality_status is EvidenceQualityStatus.PASS else "证据不足"
        ),
        created_at=NOW,
    )


def _decision(
    outcome: ManifestReadOutcome,
    *,
    reason: str | None = "证据与清单校验和不一致",
) -> ManifestReadDecision:
    verified = outcome is ManifestReadOutcome.VERIFIED
    return ManifestReadDecision(
        task_id=TASK_ID,
        capture_id=CAPTURE_ID,
        outcome=outcome,
        read_at=NOW,
        manifest_id="evidence-manifest:abc",
        manifest_version=3,
        manifest_hash="hash-abc",
        quality_status=EvidenceQualityStatus.PASS,
        reason=None if verified else reason,
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
        if self.manifest is None:
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
    def __init__(self, source: ObservationSource | None) -> None:
        self.source = source

    async def load_source(self, *, task_id: str, page_capture_id: str):
        del task_id, page_capture_id
        return self.source

    async def save_packet(self, **kwargs: Any) -> str:
        del kwargs
        return "observation-packet-1"


def _context_source() -> ObservationSource:
    return ObservationSource(
        task_id=TASK_ID,
        page_capture_id=CAPTURE_ID,
        page_url="https://example.com/home",
        page_title="首页",
        page_type="home",
        page_state="loaded",
        action_log=(),
        artifacts=(
            ObservationArtifact(
                artifact_id="stale",
                artifact_type="visible_text",
                content_type="text/plain",
                payload=b"stale",
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
) -> ManifestBackedObservationRepository:
    reference = _reference()
    loader = ManifestBackedPacketLoader(
        manifests=FakeManifestSource(_manifest() if manifest is _UNSET else manifest),
        artifact_reader=FakeArtifactReader(
            objects if objects is not None else {reference.storage_path: PAYLOAD}
        ),
    )
    return ManifestBackedObservationRepository(
        inner=FakeInnerRepository(
            _context_source() if context is _UNSET else context
        ),
        packet_loader=loader,
        now=lambda: NOW,
    )


def test_verified_decision_produces_no_rejection_and_no_alert() -> None:
    sink = InMemoryManifestReadAlertSink()
    guard = ManifestReadGuard(alert_sink=sink, now=lambda: NOW)

    assert guard.evaluate(_decision(ManifestReadOutcome.VERIFIED)) is None
    assert sink.alerts == []


def test_missing_manifest_is_control_plane_fault_not_evidence_insufficient() -> None:
    """E02 条款 2.1：控制面未就绪不得伪装成证据不足。"""
    sink = InMemoryManifestReadAlertSink()
    guard = ManifestReadGuard(alert_sink=sink, now=lambda: NOW)

    rejection = guard.evaluate(
        _decision(ManifestReadOutcome.CONTROL_PLANE_INCOMPLETE, reason="没有可用证据清单")
    )

    assert rejection is not None
    assert rejection.execution_status == "error"
    assert rejection.execution_status != "evidence_insufficient"
    assert rejection.reason_code == "manifest_absent"
    assert rejection.alerted is False
    assert rejection.reason == "没有可用证据清单"
    # 缺清单属流程未就绪，不发告警
    assert sink.alerts == []


def test_only_failed_manifest_adjudication_reports_evidence_insufficient() -> None:
    """E02 条款 1.1/2.1：唯有清单裁决 fail 才占用 evidence_insufficient。"""
    sink = InMemoryManifestReadAlertSink()
    guard = ManifestReadGuard(alert_sink=sink, now=lambda: NOW)

    rejection = guard.evaluate(
        _decision(ManifestReadOutcome.EVIDENCE_INSUFFICIENT, reason="清单裁决未通过")
    )

    assert rejection is not None
    assert rejection.execution_status == "evidence_insufficient"
    assert rejection.reason_code == "manifest_quality_failed"
    assert rejection.alerted is False
    assert sink.alerts == []


def test_pending_manifest_is_not_reported_as_evidence_insufficient() -> None:
    sink = InMemoryManifestReadAlertSink()
    guard = ManifestReadGuard(alert_sink=sink, now=lambda: NOW)

    rejection = guard.evaluate(
        _decision(ManifestReadOutcome.EVIDENCE_PENDING, reason="证据提交在途")
    )

    assert rejection is not None
    assert rejection.execution_status == "error"
    assert rejection.execution_status != "evidence_insufficient"
    assert rejection.reason_code == "manifest_pending"
    assert sink.alerts == []


def test_payload_budget_exceeded_is_not_reported_as_evidence_insufficient() -> None:
    """E02 条款 1.5：预算超限不是证据不足，用独立原因码表达。"""
    sink = InMemoryManifestReadAlertSink()
    guard = ManifestReadGuard(alert_sink=sink, now=lambda: NOW)

    rejection = guard.evaluate(
        _decision(ManifestReadOutcome.PAYLOAD_BUDGET_EXCEEDED, reason="判定载荷超出预算")
    )

    assert rejection is not None
    assert rejection.execution_status != "evidence_insufficient"
    assert rejection.reason_code == "payload_budget_exceeded"
    assert rejection.to_audit_payload()["business_outcome"] == "payload_budget_exceeded"
    assert sink.alerts == []


def test_every_outcome_has_distinct_reason_code() -> None:
    """E02 条款 2.1：各类结果必须可区分，不得共用同一原因码。"""
    guard = ManifestReadGuard(alert_sink=InMemoryManifestReadAlertSink(), now=lambda: NOW)
    failure_outcomes = [
        outcome for outcome in ManifestReadOutcome if outcome is not ManifestReadOutcome.VERIFIED
    ]

    codes = []
    for outcome in failure_outcomes:
        rejection = guard.evaluate(_decision(outcome))
        assert rejection is not None
        codes.append(rejection.reason_code)

    assert len(set(codes)) == len(failure_outcomes)
    # 只有一类占用 evidence_insufficient
    statuses = []
    for outcome in failure_outcomes:
        rejection = guard.evaluate(_decision(outcome))
        assert rejection is not None
        statuses.append(rejection.execution_status)
    assert statuses.count("evidence_insufficient") == 1


def test_integrity_failure_rejects_as_error_and_alerts() -> None:
    sink = InMemoryManifestReadAlertSink()
    guard = ManifestReadGuard(alert_sink=sink, now=lambda: NOW)

    rejection = guard.evaluate(_decision(ManifestReadOutcome.EVIDENCE_INTEGRITY_FAILED))

    assert rejection is not None
    assert rejection.execution_status == "error"
    assert rejection.alerted is True
    assert len(sink.alerts) == 1
    alert = sink.alerts[0]
    assert alert.alert_code == ALERT_CODE_EVIDENCE_INTEGRITY_FAILED
    assert alert.task_id == TASK_ID
    assert alert.capture_id == CAPTURE_ID
    assert alert.manifest_id == "evidence-manifest:abc"
    assert alert.manifest_version == 3
    assert alert.detected_at == NOW


def test_rejection_never_produces_pass_or_fail_conclusion() -> None:
    guard = ManifestReadGuard(alert_sink=InMemoryManifestReadAlertSink(), now=lambda: NOW)

    for outcome in ManifestReadOutcome:
        if outcome is ManifestReadOutcome.VERIFIED:
            continue
        rejection = guard.evaluate(_decision(outcome))
        assert rejection is not None
        assert rejection.execution_status not in {"pass", "fail", "warning"}
        judge_output = rejection.to_judge_output()
        assert judge_output["conclusion"] == rejection.execution_status
        assert judge_output["evidence_trusted"] is False


def test_rejection_statuses_are_within_rule_execution_check_constraint() -> None:
    """拒判状态必须落在 ck_qc_rule_execution_status 允许集合内。"""
    allowed = {
        "pass",
        "fail",
        "warning",
        "manual_review",
        "evidence_insufficient",
        "skipped",
        "error",
    }
    guard = ManifestReadGuard(alert_sink=InMemoryManifestReadAlertSink(), now=lambda: NOW)

    for outcome in ManifestReadOutcome:
        if outcome is ManifestReadOutcome.VERIFIED:
            continue
        rejection = guard.evaluate(_decision(outcome))
        assert rejection is not None
        assert rejection.execution_status in allowed


def test_rejection_audit_payload_carries_decision_and_alert() -> None:
    guard = ManifestReadGuard(alert_sink=InMemoryManifestReadAlertSink(), now=lambda: NOW)

    rejection = guard.evaluate(_decision(ManifestReadOutcome.EVIDENCE_INTEGRITY_FAILED))

    assert rejection is not None
    payload = rejection.to_audit_payload()
    assert payload["schema_version"] == "manifest-read-rejection.v1"
    assert payload["execution_status"] == "error"
    assert payload["decision"]["outcome"] == "evidence_integrity_failed"
    assert payload["reason_code"] == "evidence_tampered"
    assert payload["alert"]["alert_code"] == ALERT_CODE_EVIDENCE_INTEGRITY_FAILED
    # 审计与告警都不得携带证据原文
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "首页文案" not in serialized
    assert "payload" not in serialized


def test_logging_alert_sink_emits_stable_event_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sink = LoggingManifestReadAlertSink()
    alert = ManifestReadAlert(
        alert_code=ALERT_CODE_EVIDENCE_INTEGRITY_FAILED,
        task_id=TASK_ID,
        capture_id=CAPTURE_ID,
        outcome=ManifestReadOutcome.EVIDENCE_INTEGRITY_FAILED,
        detected_at=NOW,
        manifest_id="evidence-manifest:abc",
        manifest_version=3,
        reason="checksum 不一致",
    )

    with caplog.at_level(logging.ERROR):
        sink.record(alert)

    assert ALERT_CODE_EVIDENCE_INTEGRITY_FAILED in caplog.text
    assert TASK_ID in caplog.text
    assert CAPTURE_ID in caplog.text


async def test_load_verified_source_returns_trusted_evidence() -> None:
    sink = InMemoryManifestReadAlertSink()
    guard = ManifestReadGuard(alert_sink=sink, now=lambda: NOW)

    source, rejection = await load_verified_source(
        _repository(),
        task_id=TASK_ID,
        capture_id=CAPTURE_ID,
        guard=guard,
    )

    assert rejection is None
    assert source is not None
    assert [item.artifact_id for item in source.artifacts] == ["artifact-text"]
    assert sink.alerts == []


async def test_load_verified_source_rejects_without_manifest() -> None:
    sink = InMemoryManifestReadAlertSink()
    guard = ManifestReadGuard(alert_sink=sink, now=lambda: NOW)

    source, rejection = await load_verified_source(
        _repository(manifest=None),
        task_id=TASK_ID,
        capture_id=CAPTURE_ID,
        guard=guard,
    )

    assert source is None
    assert rejection is not None
    # 无清单＝控制面未就绪，不是证据不足（E02 条款 2.1）
    assert rejection.business_outcome is ManifestReadOutcome.CONTROL_PLANE_INCOMPLETE
    assert rejection.execution_status == "error"
    assert rejection.reason_code == "manifest_absent"
    assert sink.alerts == []


async def test_load_verified_source_rejects_and_alerts_on_tampered_evidence() -> None:
    sink = InMemoryManifestReadAlertSink()
    guard = ManifestReadGuard(alert_sink=sink, now=lambda: NOW)
    reference = _reference()

    source, rejection = await load_verified_source(
        _repository(objects={reference.storage_path: b"x" * len(PAYLOAD)}),
        task_id=TASK_ID,
        capture_id=CAPTURE_ID,
        guard=guard,
    )

    assert source is None
    assert rejection is not None
    assert rejection.execution_status == "error"
    assert rejection.decision.outcome is ManifestReadOutcome.EVIDENCE_INTEGRITY_FAILED
    assert len(sink.alerts) == 1
    assert sink.alerts[0].alert_code == ALERT_CODE_EVIDENCE_INTEGRITY_FAILED


async def test_load_verified_source_rejects_when_manifest_not_adjudicated() -> None:
    sink = InMemoryManifestReadAlertSink()
    guard = ManifestReadGuard(alert_sink=sink, now=lambda: NOW)

    source, rejection = await load_verified_source(
        _repository(manifest=_manifest(EvidenceQualityStatus.FAIL)),
        task_id=TASK_ID,
        capture_id=CAPTURE_ID,
        guard=guard,
    )

    assert source is None
    assert rejection is not None
    assert rejection.execution_status == "evidence_insufficient"
    assert sink.alerts == []


async def test_load_verified_source_passes_through_missing_capture() -> None:
    """Capture 上下文不存在不是证据问题，交回既有页面缺失路径。"""
    sink = InMemoryManifestReadAlertSink()
    guard = ManifestReadGuard(alert_sink=sink, now=lambda: NOW)

    source, rejection = await load_verified_source(
        _repository(context=None),
        task_id=TASK_ID,
        capture_id=CAPTURE_ID,
        guard=guard,
    )

    assert source is None
    assert rejection is None
    assert sink.alerts == []


async def test_read_failure_does_not_escape_as_runtime_exception() -> None:
    """拒判通路必须吃掉读取异常，避免证据不可信被当成运行故障吞掉。"""
    guard = ManifestReadGuard(alert_sink=InMemoryManifestReadAlertSink(), now=lambda: NOW)
    reference = _reference()

    source, rejection = await load_verified_source(
        _repository(objects={reference.storage_path: b"short"}),
        task_id=TASK_ID,
        capture_id=CAPTURE_ID,
        guard=guard,
    )

    assert source is None
    assert rejection is not None
    assert rejection.execution_status == "error"


def test_default_guard_uses_logging_sink() -> None:
    guard = ManifestReadGuard(now=lambda: NOW)

    rejection = guard.evaluate(_decision(ManifestReadOutcome.EVIDENCE_INTEGRITY_FAILED))

    assert rejection is not None
    assert rejection.alerted is True
