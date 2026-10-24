"""E23：双读影子对账与按采集时间分流测试。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from services.api.app.application.manifest_backed_packet import (
    ManifestIntegrityError,
    ManifestUnavailableError,
)
from services.api.app.application.manifest_read_shadow import (
    InMemoryShadowComparisonSink,
    ManifestReadRoute,
    ManifestReadRoutingPolicy,
    ShadowComparisonOutcome,
    ShadowReadReconciler,
    compare_sources,
)
from services.api.app.application.observation_packet import (
    ObservationArtifact,
    ObservationSource,
)

NOW = datetime(2026, 8, 13, 16, 0, tzinfo=UTC)
CUTOVER = datetime(2026, 8, 13, 0, 0, tzinfo=UTC)
TASK_ID = "11111111-1111-1111-1111-111111111111"
CAPTURE_ID = "44444444-4444-4444-4444-444444444444"


def _artifact(
    *,
    artifact_id: str,
    artifact_type: str = "visible_text",
    payload: bytes = b"content",
) -> ObservationArtifact:
    return ObservationArtifact(
        artifact_id=artifact_id,
        artifact_type=artifact_type,
        content_type="text/plain",
        payload=payload,
    )


def _source(artifacts: tuple[ObservationArtifact, ...]) -> ObservationSource:
    return ObservationSource(
        task_id=TASK_ID,
        page_capture_id=CAPTURE_ID,
        page_url="https://example.com/home",
        page_title="首页",
        page_type="home",
        page_state="loaded",
        action_log=(),
        artifacts=artifacts,
        surface_kind="web",
    )


class FakeLoader:
    def __init__(self, source: ObservationSource | None = None, error: Exception | None = None):
        self.source = source
        self.error = error
        self.calls = 0

    async def load_source(self, *, task_id: str, page_capture_id: str):
        del task_id, page_capture_id
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.source


# ---------- 时间分流 ----------


def test_routing_sends_pre_cutover_captures_to_legacy() -> None:
    policy = ManifestReadRoutingPolicy(manifest_cutover_at=CUTOVER)

    assert policy.route_for(CUTOVER - timedelta(seconds=1)) is ManifestReadRoute.LEGACY
    assert policy.route_for(CUTOVER) is ManifestReadRoute.MANIFEST
    assert policy.route_for(CUTOVER + timedelta(days=1)) is ManifestReadRoute.MANIFEST


def test_routing_without_cutover_keeps_everything_on_legacy() -> None:
    policy = ManifestReadRoutingPolicy()

    assert policy.route_for(NOW) is ManifestReadRoute.LEGACY


def test_routing_falls_back_to_legacy_when_capture_time_unknown() -> None:
    policy = ManifestReadRoutingPolicy(manifest_cutover_at=CUTOVER)

    assert policy.route_for(None) is ManifestReadRoute.LEGACY


# ---------- 对账 ----------


def test_compare_sources_reports_matched_when_identities_align() -> None:
    artifacts = (_artifact(artifact_id="a-1"), _artifact(artifact_id="a-2"))

    comparison = compare_sources(
        task_id=TASK_ID,
        capture_id=CAPTURE_ID,
        legacy=artifacts,
        manifest=artifacts,
        compared_at=NOW,
    )

    assert comparison.matched is True
    assert comparison.outcome is ShadowComparisonOutcome.MATCHED
    assert comparison.difference_count == 0
    assert comparison.legacy_artifact_count == 2
    assert comparison.manifest_artifact_count == 2


def test_compare_sources_detects_missing_unexpected_and_size_drift() -> None:
    legacy = (
        _artifact(artifact_id="a-1"),
        _artifact(artifact_id="a-2"),
        _artifact(artifact_id="a-3", payload=b"short"),
    )
    manifest = (
        _artifact(artifact_id="a-1"),
        _artifact(artifact_id="a-4"),
        _artifact(artifact_id="a-3", payload=b"much-longer-payload"),
    )

    comparison = compare_sources(
        task_id=TASK_ID,
        capture_id=CAPTURE_ID,
        legacy=legacy,
        manifest=manifest,
        compared_at=NOW,
    )

    assert comparison.matched is False
    assert [item.artifact_id for item in comparison.missing_in_manifest] == ["a-2"]
    assert [item.artifact_id for item in comparison.unexpected_in_manifest] == ["a-4"]
    assert [item.artifact_id for item in comparison.mismatched_size] == ["a-3"]
    assert comparison.difference_count == 3


def test_comparison_audit_payload_excludes_evidence_content() -> None:
    comparison = compare_sources(
        task_id=TASK_ID,
        capture_id=CAPTURE_ID,
        legacy=(_artifact(artifact_id="a-1", payload="页面文案".encode()),),
        manifest=(),
        compared_at=NOW,
    )

    serialized = json.dumps(comparison.to_audit_payload(), ensure_ascii=False)

    assert "页面文案" not in serialized
    assert "payload" not in serialized
    assert comparison.to_audit_payload()["schema_version"] == "manifest-shadow-comparison.v1"


# ---------- 影子读取 ----------


async def test_shadow_returns_legacy_result_and_records_match() -> None:
    artifacts = (_artifact(artifact_id="a-1"),)
    legacy = FakeLoader(_source(artifacts))
    manifest = FakeLoader(_source(artifacts))
    sink = InMemoryShadowComparisonSink()
    reconciler = ShadowReadReconciler(
        legacy=legacy,
        manifest_backed=manifest,
        sink=sink,
        routing=ManifestReadRoutingPolicy(manifest_cutover_at=CUTOVER),
        now=lambda: NOW,
    )

    source = await reconciler.load_source(
        task_id=TASK_ID,
        page_capture_id=CAPTURE_ID,
        captured_at=NOW,
    )

    assert source is legacy.source
    assert manifest.calls == 1
    assert sink.all_matched is True
    assert sink.comparisons[0].outcome is ShadowComparisonOutcome.MATCHED


async def test_shadow_keeps_legacy_authoritative_on_mismatch() -> None:
    legacy = FakeLoader(_source((_artifact(artifact_id="a-1"),)))
    manifest = FakeLoader(_source((_artifact(artifact_id="a-9"),)))
    sink = InMemoryShadowComparisonSink()
    reconciler = ShadowReadReconciler(
        legacy=legacy,
        manifest_backed=manifest,
        sink=sink,
        routing=ManifestReadRoutingPolicy(manifest_cutover_at=CUTOVER),
        now=lambda: NOW,
    )

    source = await reconciler.load_source(
        task_id=TASK_ID,
        page_capture_id=CAPTURE_ID,
        captured_at=NOW,
    )

    # 影子期旧读取仍是权威，差异只记录
    assert source is legacy.source
    assert sink.all_matched is False
    assert len(sink.mismatched) == 1
    assert sink.mismatched[0].outcome is ShadowComparisonOutcome.MISMATCHED


async def test_shadow_swallows_manifest_read_failure() -> None:
    legacy = FakeLoader(_source((_artifact(artifact_id="a-1"),)))
    manifest = FakeLoader(error=ManifestUnavailableError("没有可用证据清单"))
    sink = InMemoryShadowComparisonSink()
    reconciler = ShadowReadReconciler(
        legacy=legacy,
        manifest_backed=manifest,
        sink=sink,
        routing=ManifestReadRoutingPolicy(manifest_cutover_at=CUTOVER),
        now=lambda: NOW,
    )

    source = await reconciler.load_source(
        task_id=TASK_ID,
        page_capture_id=CAPTURE_ID,
        captured_at=NOW,
    )

    assert source is legacy.source
    assert sink.comparisons[0].outcome is ShadowComparisonOutcome.MANIFEST_READ_FAILED
    assert sink.all_matched is False


async def test_shadow_swallows_integrity_failure_too() -> None:
    legacy = FakeLoader(_source((_artifact(artifact_id="a-1"),)))
    manifest = FakeLoader(error=ManifestIntegrityError("证据与清单校验和不一致"))
    sink = InMemoryShadowComparisonSink()
    reconciler = ShadowReadReconciler(
        legacy=legacy,
        manifest_backed=manifest,
        sink=sink,
        routing=ManifestReadRoutingPolicy(manifest_cutover_at=CUTOVER),
        now=lambda: NOW,
    )

    source = await reconciler.load_source(
        task_id=TASK_ID,
        page_capture_id=CAPTURE_ID,
        captured_at=NOW,
    )

    assert source is legacy.source
    assert sink.comparisons[0].outcome is ShadowComparisonOutcome.MANIFEST_READ_FAILED


async def test_pre_cutover_capture_skips_manifest_read_entirely() -> None:
    legacy = FakeLoader(_source((_artifact(artifact_id="a-1"),)))
    manifest = FakeLoader(_source(()))
    sink = InMemoryShadowComparisonSink()
    reconciler = ShadowReadReconciler(
        legacy=legacy,
        manifest_backed=manifest,
        sink=sink,
        routing=ManifestReadRoutingPolicy(manifest_cutover_at=CUTOVER),
        now=lambda: NOW,
    )

    source = await reconciler.load_source(
        task_id=TASK_ID,
        page_capture_id=CAPTURE_ID,
        captured_at=CUTOVER - timedelta(hours=1),
    )

    assert source is legacy.source
    # 历史数据不去读清单，避免把「没有清单」误报成故障
    assert manifest.calls == 0
    assert sink.comparisons[0].outcome is ShadowComparisonOutcome.SKIPPED


async def test_absent_capture_short_circuits_without_comparison() -> None:
    legacy = FakeLoader(None)
    manifest = FakeLoader(_source(()))
    sink = InMemoryShadowComparisonSink()
    reconciler = ShadowReadReconciler(
        legacy=legacy,
        manifest_backed=manifest,
        sink=sink,
        routing=ManifestReadRoutingPolicy(manifest_cutover_at=CUTOVER),
        now=lambda: NOW,
    )

    source = await reconciler.load_source(
        task_id=TASK_ID,
        page_capture_id=CAPTURE_ID,
        captured_at=NOW,
    )

    assert source is None
    assert manifest.calls == 0
    assert sink.comparisons == []


def test_all_matched_requires_at_least_one_comparison() -> None:
    """E24 切主前置：没跑过对账不算零差异。"""
    sink = InMemoryShadowComparisonSink()

    assert sink.all_matched is False


async def test_shadow_delegates_packet_writes_to_legacy_repository() -> None:
    """判定服务把对账器当完整仓储用；缺 save_packet 委托时 shadow 模式下
    所有规则判定在建包阶段崩溃，质检工作项整批失败。"""

    class WritableLegacy(FakeLoader):
        def __init__(self) -> None:
            super().__init__(source=_source((_artifact(artifact_id="a-1"),)))
            self.saved: list[dict[str, object]] = []

        async def save_packet(self, **kwargs: object) -> str:
            self.saved.append(kwargs)
            return "packet-1"

    legacy = WritableLegacy()
    reconciler = ShadowReadReconciler(
        legacy=legacy,
        manifest_backed=FakeLoader(source=_source((_artifact(artifact_id="a-1"),))),
        sink=InMemoryShadowComparisonSink(),
    )

    packet_id = await reconciler.save_packet(task_id=TASK_ID, rule_id="rule-1")

    assert packet_id == "packet-1"
    assert legacy.saved == [{"task_id": TASK_ID, "rule_id": "rule-1"}]


async def test_shadow_rejects_writes_when_legacy_cannot_persist() -> None:
    import pytest

    reconciler = ShadowReadReconciler(
        legacy=FakeLoader(source=None),
        manifest_backed=FakeLoader(source=None),
        sink=InMemoryShadowComparisonSink(),
    )

    with pytest.raises(TypeError, match="save_packet"):
        await reconciler.save_packet(task_id=TASK_ID)
