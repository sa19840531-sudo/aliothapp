"""E24：主读取路径切换、零差异前置与回滚开关测试。"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from services.api.app.application.manifest_read_cutover import (
    CutoverBlockReason,
    ManifestReadMode,
    ManifestReadModeResolver,
    ShadowReconciliationSummary,
    build_observation_source_reader,
    summarize_comparisons,
)
from services.api.app.application.manifest_read_shadow import (
    ShadowComparisonOutcome,
    ShadowReadComparison,
)
from services.api.app.core.config import _build_evidence_manifest_read_settings

NOW = datetime(2026, 8, 13, 18, 0, tzinfo=UTC)
LATER = datetime(2026, 8, 13, 19, 0, tzinfo=UTC)
TASK_ID = "11111111-1111-1111-1111-111111111111"


def _comparison(
    outcome: ShadowComparisonOutcome,
    *,
    capture_id: str = "capture-1",
    compared_at: datetime = NOW,
) -> ShadowReadComparison:
    return ShadowReadComparison(
        task_id=TASK_ID,
        capture_id=capture_id,
        outcome=outcome,
        compared_at=compared_at,
    )


# ---------- 对账汇总 ----------


def test_summary_counts_outcomes_and_time_window() -> None:
    summary = summarize_comparisons(
        [
            _comparison(ShadowComparisonOutcome.MATCHED, capture_id="c-1"),
            _comparison(
                ShadowComparisonOutcome.MATCHED, capture_id="c-2", compared_at=LATER
            ),
            _comparison(ShadowComparisonOutcome.MISMATCHED, capture_id="c-3"),
            _comparison(ShadowComparisonOutcome.MANIFEST_READ_FAILED, capture_id="c-4"),
            _comparison(ShadowComparisonOutcome.SKIPPED, capture_id="c-5"),
        ]
    )

    assert summary.matched == 2
    assert summary.mismatched == 1
    assert summary.manifest_read_failed == 1
    assert summary.skipped == 1
    # skipped 没真正比过，不计入对账样本
    assert summary.compared_captures == 4
    assert summary.first_compared_at == NOW
    assert summary.last_compared_at == LATER
    assert summary.zero_difference is False


def test_skipped_only_summary_is_not_zero_difference() -> None:
    """按时间分流跳过的采集不能充当零差异证据。"""
    summary = summarize_comparisons(
        [_comparison(ShadowComparisonOutcome.SKIPPED) for _ in range(10)]
    )

    assert summary.compared_captures == 0
    assert summary.zero_difference is False


def test_all_matched_summary_is_zero_difference() -> None:
    summary = summarize_comparisons(
        [
            _comparison(ShadowComparisonOutcome.MATCHED, capture_id=f"c-{index}")
            for index in range(3)
        ]
    )

    assert summary.compared_captures == 3
    assert summary.zero_difference is True


def test_empty_summary_is_not_zero_difference() -> None:
    assert summarize_comparisons([]).zero_difference is False


# ---------- 切主决议 ----------


def test_off_and_shadow_modes_take_effect_directly() -> None:
    resolver = ManifestReadModeResolver()

    for mode in (ManifestReadMode.OFF, ManifestReadMode.SHADOW):
        decision = resolver.resolve(requested_mode=mode)
        assert decision.effective_mode is mode
        assert decision.downgraded is False
        assert decision.block_reason is None


def test_cutover_requires_shadow_evidence() -> None:
    """E24 验收：没有对账凭证不得切主。"""
    resolver = ManifestReadModeResolver()

    decision = resolver.resolve(requested_mode=ManifestReadMode.ON)

    assert decision.effective_mode is ManifestReadMode.SHADOW
    assert decision.downgraded is True
    assert decision.block_reason is CutoverBlockReason.NO_SHADOW_EVIDENCE


def test_cutover_blocked_by_mismatches() -> None:
    resolver = ManifestReadModeResolver()
    summary = summarize_comparisons(
        [
            _comparison(ShadowComparisonOutcome.MATCHED, capture_id="c-1"),
            _comparison(ShadowComparisonOutcome.MISMATCHED, capture_id="c-2"),
        ]
    )

    decision = resolver.resolve(requested_mode=ManifestReadMode.ON, summary=summary)

    assert decision.effective_mode is ManifestReadMode.SHADOW
    assert decision.block_reason is CutoverBlockReason.MISMATCHES_PRESENT


def test_cutover_blocked_by_manifest_read_failures() -> None:
    resolver = ManifestReadModeResolver()
    summary = summarize_comparisons(
        [
            _comparison(ShadowComparisonOutcome.MATCHED, capture_id="c-1"),
            _comparison(ShadowComparisonOutcome.MANIFEST_READ_FAILED, capture_id="c-2"),
        ]
    )

    decision = resolver.resolve(requested_mode=ManifestReadMode.ON, summary=summary)

    assert decision.effective_mode is ManifestReadMode.SHADOW
    assert decision.block_reason is CutoverBlockReason.MANIFEST_READ_FAILURES


def test_cutover_blocked_by_insufficient_samples() -> None:
    resolver = ManifestReadModeResolver(minimum_compared_captures=5)
    summary = summarize_comparisons(
        [
            _comparison(ShadowComparisonOutcome.MATCHED, capture_id=f"c-{index}")
            for index in range(2)
        ]
    )

    decision = resolver.resolve(requested_mode=ManifestReadMode.ON, summary=summary)

    assert decision.effective_mode is ManifestReadMode.SHADOW
    assert decision.block_reason is CutoverBlockReason.INSUFFICIENT_SAMPLES


def test_cutover_allowed_after_zero_difference() -> None:
    resolver = ManifestReadModeResolver(minimum_compared_captures=3)
    summary = summarize_comparisons(
        [
            _comparison(ShadowComparisonOutcome.MATCHED, capture_id=f"c-{index}")
            for index in range(3)
        ]
    )

    decision = resolver.resolve(requested_mode=ManifestReadMode.ON, summary=summary)

    assert decision.effective_mode is ManifestReadMode.ON
    assert decision.downgraded is False
    assert decision.block_reason is None


def test_decision_audit_payload_records_downgrade() -> None:
    resolver = ManifestReadModeResolver()

    decision = resolver.resolve(requested_mode=ManifestReadMode.ON)
    payload = decision.to_audit_payload()

    assert payload["requested_mode"] == "on"
    assert payload["effective_mode"] == "shadow"
    assert payload["downgraded"] is True
    assert payload["block_reason"] == "no_shadow_evidence"
    assert json.dumps(payload, ensure_ascii=False)


def test_resolver_rejects_invalid_minimum() -> None:
    with pytest.raises(ValueError, match="minimum_compared_captures"):
        ManifestReadModeResolver(minimum_compared_captures=0)


# ---------- 读取来源装配与回滚 ----------


def _readers() -> tuple[object, object, list[tuple[object, object]]]:
    legacy = object()
    manifest_backed = object()
    shadow_calls: list[tuple[object, object]] = []

    return legacy, manifest_backed, shadow_calls


def test_reader_assembly_per_effective_mode() -> None:
    legacy, manifest_backed, shadow_calls = _readers()
    shadow_marker = object()

    def shadow_factory(inner_legacy: object, inner_manifest: object) -> object:
        shadow_calls.append((inner_legacy, inner_manifest))
        return shadow_marker

    resolver = ManifestReadModeResolver()
    zero_diff = summarize_comparisons(
        [_comparison(ShadowComparisonOutcome.MATCHED)]
    )

    off = build_observation_source_reader(
        decision=resolver.resolve(requested_mode=ManifestReadMode.OFF),
        legacy=legacy,
        manifest_backed=manifest_backed,
        shadow_factory=shadow_factory,
    )
    shadow = build_observation_source_reader(
        decision=resolver.resolve(requested_mode=ManifestReadMode.SHADOW),
        legacy=legacy,
        manifest_backed=manifest_backed,
        shadow_factory=shadow_factory,
    )
    on = build_observation_source_reader(
        decision=resolver.resolve(
            requested_mode=ManifestReadMode.ON, summary=zero_diff
        ),
        legacy=legacy,
        manifest_backed=manifest_backed,
        shadow_factory=shadow_factory,
    )

    assert off is legacy
    assert shadow is shadow_marker
    assert shadow_calls == [(legacy, manifest_backed)]
    assert on is manifest_backed


def test_downgraded_cutover_assembles_shadow_not_manifest() -> None:
    """请求 on 但未确认零差异时，生产实际跑的是影子而非清单主路径。"""
    legacy, manifest_backed, _ = _readers()
    shadow_marker = object()
    resolver = ManifestReadModeResolver()

    reader = build_observation_source_reader(
        decision=resolver.resolve(requested_mode=ManifestReadMode.ON),
        legacy=legacy,
        manifest_backed=manifest_backed,
        shadow_factory=lambda _legacy, _manifest: shadow_marker,
    )

    assert reader is shadow_marker
    assert reader is not manifest_backed


def test_rollback_returns_to_legacy_reader() -> None:
    """E24 验收：开关可退回旧读取路径，且无需任何数据修复。"""
    legacy, manifest_backed, _ = _readers()
    resolver = ManifestReadModeResolver()
    zero_diff = summarize_comparisons([_comparison(ShadowComparisonOutcome.MATCHED)])
    shadow_marker = object()

    def shadow_factory(_legacy: object, _manifest: object) -> object:
        return shadow_marker

    cut_over = build_observation_source_reader(
        decision=resolver.resolve(
            requested_mode=ManifestReadMode.ON, summary=zero_diff
        ),
        legacy=legacy,
        manifest_backed=manifest_backed,
        shadow_factory=shadow_factory,
    )
    rolled_back_to_shadow = build_observation_source_reader(
        decision=resolver.resolve(requested_mode=ManifestReadMode.SHADOW),
        legacy=legacy,
        manifest_backed=manifest_backed,
        shadow_factory=shadow_factory,
    )
    rolled_back_to_off = build_observation_source_reader(
        decision=resolver.resolve(requested_mode=ManifestReadMode.OFF),
        legacy=legacy,
        manifest_backed=manifest_backed,
        shadow_factory=shadow_factory,
    )

    assert cut_over is manifest_backed
    assert rolled_back_to_shadow is shadow_marker
    assert rolled_back_to_off is legacy


# ---------- 配置开关 ----------


def test_settings_default_to_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALIOTH_EVIDENCE_MANIFEST_READ_MODE", raising=False)
    monkeypatch.delenv("ALIOTH_EVIDENCE_MANIFEST_CUTOVER_AT", raising=False)

    settings = _build_evidence_manifest_read_settings()

    assert settings.mode == "off"
    assert settings.cutover_at is None
    assert settings.minimum_compared_captures == 1


def test_settings_reject_unknown_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALIOTH_EVIDENCE_MANIFEST_READ_MODE", "yolo")

    assert _build_evidence_manifest_read_settings().mode == "off"


def test_settings_parse_cutover_and_default_to_utc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALIOTH_EVIDENCE_MANIFEST_READ_MODE", "shadow")
    monkeypatch.setenv("ALIOTH_EVIDENCE_MANIFEST_CUTOVER_AT", "2026-08-13T00:00:00")

    settings = _build_evidence_manifest_read_settings()

    assert settings.mode == "shadow"
    assert settings.cutover_at == datetime(2026, 8, 13, 0, 0, tzinfo=UTC)


def test_settings_ignore_invalid_cutover(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALIOTH_EVIDENCE_MANIFEST_CUTOVER_AT", "not-a-time")

    assert _build_evidence_manifest_read_settings().cutover_at is None


def test_composition_assembles_reader_per_mode() -> None:
    """E24：生产组合根按模式装配读取来源，默认 off 与改造前一致。"""
    from services.api.app.application.manifest_backed_observation import (
        ManifestBackedObservationRepository,
    )
    from services.api.app.application.manifest_read_composition import (
        build_manifest_read_composition,
    )
    from services.api.app.application.manifest_read_cutover import (
        ShadowReconciliationSummary,
    )
    from services.api.app.application.manifest_read_shadow import (
        PersistingShadowComparisonSink,
        ShadowReadReconciler,
    )
    from services.api.app.core.config import EvidenceManifestReadSettings
    from services.api.app.infrastructure.db.observation_packet_repository import (
        PostgresObservationPacketRepository,
    )

    class _EmptyShadowRepo:
        async def load_reconciliation_summary(self):
            return ShadowReconciliationSummary()

        async def save_comparison(self, comparison):
            return "shadow-1"

    empty_repo = _EmptyShadowRepo()
    off = build_manifest_read_composition(
        settings=EvidenceManifestReadSettings(mode="off"),
        shadow_repository=empty_repo,  # type: ignore[arg-type]
    )
    shadow = build_manifest_read_composition(
        settings=EvidenceManifestReadSettings(mode="shadow"),
        shadow_repository=empty_repo,  # type: ignore[arg-type]
    )
    requested_on = build_manifest_read_composition(
        settings=EvidenceManifestReadSettings(mode="on"),
        shadow_repository=empty_repo,  # type: ignore[arg-type]
    )
    authoritative_on = build_manifest_read_composition(
        settings=EvidenceManifestReadSettings(mode="on"),
        summary=ShadowReconciliationSummary(
            compared_captures=3,
            matched=3,
            mismatched=0,
            manifest_read_failed=0,
        ),
        shadow_repository=empty_repo,  # type: ignore[arg-type]
    )

    assert isinstance(off.reader, PostgresObservationPacketRepository)
    assert isinstance(shadow.reader, ShadowReadReconciler)
    assert isinstance(shadow.shadow_sink, PersistingShadowComparisonSink)
    # 无零差异凭证时请求 on 也只能拿到影子读取，绝不直接切清单主路径
    assert requested_on.decision.downgraded is True
    assert isinstance(requested_on.reader, ShadowReadReconciler)
    assert not isinstance(requested_on.reader, ManifestBackedObservationRepository)
    # 持久化／传入零差异凭证后才允许 authoritative on
    assert authoritative_on.decision.downgraded is False
    assert isinstance(authoritative_on.reader, ManifestBackedObservationRepository)


def test_composition_summary_starts_empty_and_is_not_zero_difference() -> None:
    from services.api.app.application.manifest_read_composition import (
        build_manifest_read_composition,
    )
    from services.api.app.application.manifest_read_cutover import (
        ShadowReconciliationSummary,
    )
    from services.api.app.core.config import EvidenceManifestReadSettings

    class _EmptyShadowRepo:
        async def load_reconciliation_summary(self):
            return ShadowReconciliationSummary()

        async def save_comparison(self, comparison):
            return "shadow-1"

    composition = build_manifest_read_composition(
        settings=EvidenceManifestReadSettings(mode="shadow"),
        shadow_repository=_EmptyShadowRepo(),  # type: ignore[arg-type]
    )

    summary = composition.current_summary()

    assert summary.compared_captures == 0
    assert summary.zero_difference is False


def test_summary_audit_payload_is_serializable() -> None:
    summary = ShadowReconciliationSummary(
        compared_captures=2,
        matched=2,
        first_compared_at=NOW,
        last_compared_at=LATER,
    )

    payload = summary.to_audit_payload()

    assert payload["zero_difference"] is True
    assert payload["first_compared_at"] == NOW.isoformat()
    assert json.dumps(payload, ensure_ascii=False)
