"""聚合规则配置、封存选择与有界 Judge 的主链合同。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.application.aggregate_inspection_execution import (
    AggregateExecutionLease,
    AggregateJudgeContext,
    evaluate_aggregate_context,
)
from services.api.app.application.aggregate_inspection_publication import (
    AggregateEvidenceSource,
    AggregateInspectionPublicationService,
    AggregatePublicationSnapshot,
    build_aggregate_rule_source,
)
from services.api.app.application.aggregate_rule_config import (
    AggregateRuleConfigurationError,
    resolve_rule_evidence_input_configuration,
    validate_enabled_aggregate_rule_configuration,
)
from services.api.app.domain.evidence import (
    ArtifactReference,
    EvidenceInputCandidate,
    EvidenceInputScope,
    EvidenceQualityStatus,
    build_evidence_manifest_reference,
    seal_evidence_input_set,
)
from services.api.app.domain.inspection.rule_scope import (
    RuleScopeAuthority,
)
from services.api.app.infrastructure.db.aggregate_inspection_execution_repository import (
    AggregateInspectionExecutionPersistenceError,
    PostgresAggregateInspectionExecutionRepository,
)

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def _judge_config(*, scope: str = "task", scope_key: str = "toast_style") -> dict:
    return {
        "capability_config": {
            "evidence_input_scope": scope,
            "evidence_scope_key": scope_key,
        }
    }


def _manifest(capture_id: str, packet_id: str):
    return build_evidence_manifest_reference(
        task_id="task-1",
        session_id="session-1",
        capture_id=capture_id,
        manifest_version=1,
        artifacts=(
            ArtifactReference(
                artifact_id=f"artifact-{capture_id}",
                artifact_type="ui_snapshot",
                role="observation",
                storage_path=f"evidence/{capture_id}.json",
                checksum="sha256:" + "a" * 64,
                size_bytes=10,
                content_type="application/json",
            ),
        ),
        quality_status=EvidenceQualityStatus.PASS,
        quality_reason="artifact_integrity_verified",
        observation_packet_ids=(packet_id,),
        created_at=NOW,
    )


def _candidate(
    capture_id: str,
    packet_id: str,
    *,
    sequence: int,
    environment_profile_id: str | None = None,
    page_type: str | None = "profile",
) -> AggregateEvidenceSource:
    return AggregateEvidenceSource(
        candidate=EvidenceInputCandidate(
            manifest=_manifest(capture_id, packet_id),
            capture_role="observation",
            page_sequence=sequence,
            environment_profile_id=environment_profile_id,
        ),
        page_type=page_type,
    )


def _rule(*, scope: str = "task", scope_key: str = "toast_style"):
    return build_aggregate_rule_source(
        {
            "rule_id": "rule-1",
            "rule_code": "alioth_spec_48",
            "category": "interaction",
            "executor_type": "hybrid",
            "severity": "medium",
            "check_point": "Toast 样式一致",
            "input_contract": {},
            "judge_config": _judge_config(scope=scope, scope_key=scope_key),
            "manual_review_policy": {},
            "steps": [],
            "applicable_page_types": ["profile"],
        }
    )


class FakePublicationRepository:
    def __init__(self, snapshot: AggregatePublicationSnapshot) -> None:
        self.snapshot = snapshot
        self.published: list[Any] = []
        self.dispositions: list[dict[str, Any]] = []

    async def load_snapshot(self, **kwargs):
        assert kwargs["task_id"] == self.snapshot.task_id
        return self.snapshot

    async def persist_published(self, **kwargs):
        self.published.append(kwargs)
        return True

    async def persist_disposition(self, **kwargs):
        self.dispositions.append(kwargs)


async def test_selection_excludes_untyped_captures_and_matches_rule_page_types() -> None:
    """未识别页型的技术性捕获不得稀释聚合成员；页型匹配以真实识别结果为准。"""
    snapshot = AggregatePublicationSnapshot(
        task_id="task-1",
        source_agent_run_id="run-1",
        finalization_epoch=2,
        ruleset_snapshot_hash="ruleset-hash",
        rules=(_rule(),),
        evidence=(
            _candidate("capture-typed", "packet-typed", sequence=1, page_type="profile"),
            _candidate("capture-null", "packet-null", sequence=2, page_type=None),
            _candidate("capture-other", "packet-other", sequence=3, page_type="video_list"),
        ),
    )
    repository = FakePublicationRepository(snapshot)

    await AggregateInspectionPublicationService(repository=repository).prepare(
        task_id="task-1", agent_run_id="run-1", finalization_epoch=2
    )

    assert len(repository.published) == 1
    input_set = repository.published[0]["input_set"]
    assert input_set.eligible_capture_ids == ("capture-typed",)


async def test_selection_takes_all_typed_captures_for_universal_rule() -> None:
    base = _rule()
    universal = type(base)(
        rule_id=base.rule_id,
        rule_code=base.rule_code,
        rule_payload={**base.rule_payload, "applicable_page_types": []},
        applicable_page_types=(),
        evidence=base.evidence,
    )
    snapshot = AggregatePublicationSnapshot(
        task_id="task-1",
        source_agent_run_id="run-1",
        finalization_epoch=2,
        ruleset_snapshot_hash="ruleset-hash",
        rules=(universal,),
        evidence=(
            _candidate("capture-a", "packet-a", sequence=1, page_type="home"),
            _candidate("capture-null", "packet-null", sequence=2, page_type=None),
            _candidate("capture-b", "packet-b", sequence=3, page_type="video_list"),
        ),
    )
    repository = FakePublicationRepository(snapshot)

    await AggregateInspectionPublicationService(repository=repository).prepare(
        task_id="task-1", agent_run_id="run-1", finalization_epoch=2
    )

    assert len(repository.published) == 1
    input_set = repository.published[0]["input_set"]
    assert input_set.eligible_capture_ids == ("capture-a", "capture-b")


async def test_missing_applicable_evidence_stays_unproven_instead_of_not_applicable() -> None:
    """没有采到适用页面不能证明规则不适用，按证据不足转人工复核。"""
    snapshot = AggregatePublicationSnapshot(
        task_id="task-1",
        source_agent_run_id="run-1",
        finalization_epoch=2,
        ruleset_snapshot_hash="ruleset-hash",
        rules=(_rule(),),
        evidence=(
            _candidate(
                "capture-other",
                "packet-other",
                sequence=1,
                page_type="video_list",
            ),
            _candidate(
                "capture-untyped",
                "packet-untyped",
                sequence=2,
                page_type=None,
            ),
        ),
    )
    repository = FakePublicationRepository(snapshot)

    summary = await AggregateInspectionPublicationService(repository=repository).prepare(
        task_id="task-1", agent_run_id="run-1", finalization_epoch=2
    )

    assert repository.published == []
    assert summary.not_applicable_count == 0
    assert summary.evidence_insufficient_count == 1
    assert repository.dispositions[0]["status"] == "evidence_insufficient"
    assert repository.dispositions[0]["code"] == "AGGREGATE_APPLICABILITY_UNPROVEN"


async def test_verified_negative_scope_can_publish_not_applicable_disposition() -> None:
    """同一 Run/Epoch 的冻结负向范围计划可以形成规则不适用终态。"""
    snapshot = AggregatePublicationSnapshot(
        task_id="task-1",
        source_agent_run_id="run-1",
        finalization_epoch=2,
        ruleset_snapshot_hash="ruleset-hash",
        rules=(_rule(),),
        evidence=(
            _candidate(
                "capture-other",
                "packet-other",
                sequence=1,
                page_type="video_list",
            ),
        ),
        scope_authority=RuleScopeAuthority(
            task_id="task-1",
            source_agent_run_id="run-1",
            finalization_epoch=2,
            authority_type="web_coverage_snapshot",
            authority_id="snapshot-1",
            authority_hash="sha256:" + "a" * 64,
            scope_verified=True,
            applicable_page_types=("home", "video_list"),
            not_applicable_page_types=("profile",),
            unresolved_page_types=(),
            negative_evidence_refs=("web-coverage:snapshot-1",),
        ),
    )
    repository = FakePublicationRepository(snapshot)

    summary = await AggregateInspectionPublicationService(repository=repository).prepare(
        task_id="task-1", agent_run_id="run-1", finalization_epoch=2
    )

    assert repository.published == []
    assert summary.not_applicable_count == 1
    assert summary.evidence_insufficient_count == 0
    assert repository.dispositions[0]["status"] == "not_applicable"
    assert repository.dispositions[0]["code"] == "AGGREGATE_RULE_SCOPE_NOT_APPLICABLE"
    plan = repository.dispositions[0]["scope_plan"]
    assert plan.authority_id == "snapshot-1"
    assert plan.matched_not_applicable_page_types == ("profile",)


async def test_scope_conflict_never_publishes_matching_evidence() -> None:
    """负向计划与实际 PASS 主证据冲突时必须失败关闭，不能继续规则判定。"""
    snapshot = AggregatePublicationSnapshot(
        task_id="task-1",
        source_agent_run_id="run-1",
        finalization_epoch=2,
        ruleset_snapshot_hash="ruleset-hash",
        rules=(_rule(),),
        evidence=(
            _candidate(
                "capture-profile",
                "packet-profile",
                sequence=1,
                page_type="profile",
            ),
        ),
        scope_authority=RuleScopeAuthority(
            task_id="task-1",
            source_agent_run_id="run-1",
            finalization_epoch=2,
            authority_type="web_coverage_snapshot",
            authority_id="snapshot-1",
            authority_hash="sha256:" + "a" * 64,
            scope_verified=True,
            applicable_page_types=("home",),
            not_applicable_page_types=("profile",),
            unresolved_page_types=(),
            negative_evidence_refs=("web-coverage:snapshot-1",),
        ),
    )
    repository = FakePublicationRepository(snapshot)

    summary = await AggregateInspectionPublicationService(repository=repository).prepare(
        task_id="task-1", agent_run_id="run-1", finalization_epoch=2
    )

    assert repository.published == []
    assert summary.evidence_insufficient_count == 1
    assert repository.dispositions[0]["status"] == "evidence_insufficient"
    assert repository.dispositions[0]["code"] == "AGGREGATE_RULE_SCOPE_CONFLICT"


async def test_verified_applicable_scope_without_evidence_requires_review() -> None:
    snapshot = AggregatePublicationSnapshot(
        task_id="task-1",
        source_agent_run_id="run-1",
        finalization_epoch=2,
        ruleset_snapshot_hash="ruleset-hash",
        rules=(_rule(),),
        evidence=(),
        scope_authority=RuleScopeAuthority(
            task_id="task-1",
            source_agent_run_id="run-1",
            finalization_epoch=2,
            authority_type="web_coverage_snapshot",
            authority_id="snapshot-1",
            authority_hash="sha256:" + "a" * 64,
            scope_verified=True,
            applicable_page_types=("profile",),
            not_applicable_page_types=(),
            unresolved_page_types=(),
        ),
    )
    repository = FakePublicationRepository(snapshot)

    summary = await AggregateInspectionPublicationService(repository=repository).prepare(
        task_id="task-1", agent_run_id="run-1", finalization_epoch=2
    )

    assert summary.evidence_insufficient_count == 1
    assert repository.dispositions[0]["status"] == "evidence_insufficient"
    assert repository.dispositions[0]["code"] == ("AGGREGATE_APPLICABLE_EVIDENCE_MISSING")


async def test_applicable_scope_plan_is_bound_to_published_candidate() -> None:
    snapshot = AggregatePublicationSnapshot(
        task_id="task-1",
        source_agent_run_id="run-1",
        finalization_epoch=2,
        ruleset_snapshot_hash="ruleset-hash",
        rules=(_rule(),),
        evidence=(
            _candidate(
                "capture-profile",
                "packet-profile",
                sequence=1,
                page_type="profile",
            ),
        ),
        scope_authority=RuleScopeAuthority(
            task_id="task-1",
            source_agent_run_id="run-1",
            finalization_epoch=2,
            authority_type="web_coverage_snapshot",
            authority_id="snapshot-1",
            authority_hash="sha256:" + "a" * 64,
            scope_verified=True,
            applicable_page_types=("profile",),
            not_applicable_page_types=(),
            unresolved_page_types=(),
        ),
    )
    repository = FakePublicationRepository(snapshot)

    summary = await AggregateInspectionPublicationService(repository=repository).prepare(
        task_id="task-1", agent_run_id="run-1", finalization_epoch=2
    )

    assert summary.published_work_item_count == 1
    plan = repository.published[0]["scope_plan"]
    assert plan.status.value == "applicable"
    assert plan.authority_id == "snapshot-1"
    assert plan.plan_hash.startswith("sha256:")


def test_aggregate_scope_must_be_explicit_and_executable() -> None:
    default = resolve_rule_evidence_input_configuration({})
    assert default.scope is EvidenceInputScope.PAGE

    task = validate_enabled_aggregate_rule_configuration(_judge_config())
    assert task.scope is EvidenceInputScope.TASK
    assert task.scope_key == "toast_style"

    with pytest.raises(AggregateRuleConfigurationError, match="可执行"):
        validate_enabled_aggregate_rule_configuration(
            _judge_config(scope="task", scope_key="unknown_strategy")
        )


async def test_publication_seals_task_input_and_persists_environment_gap() -> None:
    task_rule = _rule()
    matrix_rule = _rule(
        scope="environment_matrix",
        scope_key="environment_consistency",
    )
    matrix_rule = type(matrix_rule)(
        rule_id="rule-2",
        rule_code="env-rule",
        rule_payload={**matrix_rule.rule_payload, "rule_id": "rule-2", "rule_code": "env-rule"},
        applicable_page_types=matrix_rule.applicable_page_types,
        evidence=matrix_rule.evidence,
    )
    snapshot = AggregatePublicationSnapshot(
        task_id="task-1",
        source_agent_run_id="run-1",
        finalization_epoch=2,
        ruleset_snapshot_hash="ruleset-hash",
        rules=(task_rule, matrix_rule),
        evidence=(
            _candidate(
                "capture-1",
                "packet-1",
                sequence=1,
                environment_profile_id="prod-a",
            ),
            _candidate(
                "capture-2",
                "packet-2",
                sequence=2,
                environment_profile_id="prod-a",
            ),
        ),
    )
    repository = FakePublicationRepository(snapshot)

    summary = await AggregateInspectionPublicationService(repository=repository).prepare(
        task_id="task-1", agent_run_id="run-1", finalization_epoch=2
    )

    assert summary.expected_rule_count == 2
    assert summary.published_work_item_count == 1
    assert summary.evidence_insufficient_count == 1
    sealed = repository.published[0]["input_set"]
    assert sealed.scope is EvidenceInputScope.TASK
    assert tuple(item.capture_id for item in sealed.members) == (
        "capture-1",
        "capture-2",
    )
    assert repository.dispositions[0]["code"] == ("AGGREGATE_ENVIRONMENT_MATRIX_INSUFFICIENT")


def _context(*, style_two: dict[str, Any], monitor_status: str = "observed"):
    candidates = (
        EvidenceInputCandidate(
            manifest=_manifest("capture-1", "packet-1"),
            capture_role="observation",
            page_sequence=1,
        ),
        EvidenceInputCandidate(
            manifest=_manifest("capture-2", "packet-2"),
            capture_role="observation",
            page_sequence=2,
        ),
    )
    input_set = seal_evidence_input_set(
        task_id="task-1",
        source_agent_run_id="run-1",
        rule_id="rule-1",
        rule_code="alioth_spec_48",
        rule_contract_hash="contract-hash",
        ruleset_snapshot_hash="ruleset-hash",
        scope=EvidenceInputScope.TASK,
        scope_key="toast_style",
        finalization_epoch=2,
        eligibility_snapshot_id="snapshot-1",
        eligibility_snapshot_hash="snapshot-hash",
        selection_policy_hash="policy-hash",
        eligible_capture_ids=("capture-1", "capture-2"),
        candidates=candidates,
        sealed_at=NOW,
    )
    style_one = {
        "font_size": "16px",
        "text_color": "white",
        "background_color": "black",
        "background_opacity": 0.7,
    }

    def packet(style: dict[str, Any]):
        return {
            "facts": {
                "action_log": [
                    {
                        "transient_message_observation": {"status": monitor_status},
                        "transient_messages": [
                            {
                                "event_id": "transient-1",
                                "kind": "toast",
                                "text": "操作成功",
                                "platform_ui_confidence": 0.95,
                                "rect": {"x": 20, "y": 700, "width": 120, "height": 30},
                                "viewport": {"width": 390, "height": 844},
                                "position_zone": "bottom_center",
                                "style": style,
                            }
                        ],
                    }
                ]
            }
        }

    return AggregateJudgeContext(
        lease=AggregateExecutionLease(
            work_item_id="work-1",
            task_id="task-1",
            evidence_input_set_id=input_set.input_set_id,
            input_hash=input_set.input_set_hash,
            rule_id="rule-1",
            rule_code="alioth_spec_48",
            rule_contract_hash="contract-hash",
            scope="task",
            scope_key="toast_style",
            finalization_epoch=2,
            consumer_agent_run_id="run-1",
            lease_token="lease-1",
            attempt_no=1,
            row_version=1,
        ),
        input_set=input_set,
        observation_packets={
            "packet-1": packet(style_one),
            "packet-2": packet(style_two),
        },
    )


def _popup_context(modal_flags: list[bool]):
    """按帧序构造 home_popup_sequence 聚合上下文；True=该帧弹窗在场。"""
    candidates = tuple(
        EvidenceInputCandidate(
            manifest=_manifest(f"capture-{index}", f"packet-{index}"),
            capture_role="observation",
            page_sequence=index,
        )
        for index in range(1, len(modal_flags) + 1)
    )
    input_set = seal_evidence_input_set(
        task_id="task-1",
        source_agent_run_id="run-1",
        rule_id="rule-22",
        rule_code="alioth_22",
        rule_contract_hash="contract-hash",
        ruleset_snapshot_hash="ruleset-hash",
        scope=EvidenceInputScope.TASK,
        scope_key="home_popup_sequence",
        finalization_epoch=2,
        eligibility_snapshot_id="snapshot-1",
        eligibility_snapshot_hash="snapshot-hash",
        selection_policy_hash="policy-hash",
        eligible_capture_ids=tuple(f"capture-{i}" for i in range(1, len(modal_flags) + 1)),
        candidates=candidates,
        sealed_at=NOW,
    )

    def packet(modal_present: bool):
        elements = (
            [{"selector": "#ad-dialog", "class_tokens": ["van-dialog"], "rect": {}}]
            if modal_present
            else [{"selector": "#feed", "class_tokens": ["content"], "rect": {}}]
        )
        return {"facts": {"ui_snapshot": {"elements": elements}}}

    return AggregateJudgeContext(
        lease=AggregateExecutionLease(
            work_item_id="work-22",
            task_id="task-1",
            evidence_input_set_id=input_set.input_set_id,
            input_hash=input_set.input_set_hash,
            rule_id="rule-22",
            rule_code="alioth_22",
            rule_contract_hash="contract-hash",
            scope="task",
            scope_key="home_popup_sequence",
            finalization_epoch=2,
            consumer_agent_run_id="run-1",
            lease_token="lease-22",
            attempt_no=1,
            row_version=1,
        ),
        input_set=input_set,
        observation_packets={
            f"packet-{index}": packet(flag) for index, flag in enumerate(modal_flags, start=1)
        },
    )


def test_home_popup_sequence_judges_chain_continuity() -> None:
    """弹窗链连续清空=pass；清空后短窗内再冒弹窗=稍后载入 fail；链太短=证据不足。"""
    passed = evaluate_aggregate_context(_popup_context([True, True, True, False, False]))
    failed = evaluate_aggregate_context(_popup_context([True, True, False, True]))
    too_short = evaluate_aggregate_context(_popup_context([True, False, False]))
    absent = evaluate_aggregate_context(_popup_context([False, False]))

    assert passed.execution_status == "pass"
    assert failed.execution_status == "fail"
    assert "稍后载入" in (failed.problem_desc or "")
    assert too_short.execution_status == "evidence_insufficient"
    assert too_short.error_code == "POPUP_SEQUENCE_CHAIN_TOO_SHORT"
    assert absent.execution_status == "evidence_insufficient"
    assert absent.error_code == "POPUP_SEQUENCE_CHAIN_ABSENT"


def _with_packets(base: AggregateJudgeContext, packets: list[dict]) -> AggregateJudgeContext:
    return AggregateJudgeContext(
        lease=base.lease,
        input_set=base.input_set,
        observation_packets={
            f"packet-{index}": packet for index, packet in enumerate(packets, start=1)
        },
    )


def _presentation_packet(*, topmost: str, hit_test: str, class_tokens: list[str]) -> dict:
    return {
        "facts": {
            "presentation": {
                "protocol_version": "presentation-stack.v1",
                "topmost_layer_kind": topmost,
                "hit_test_status": hit_test,
            },
            "ui_snapshot": {
                "elements": [{"selector": "#node", "class_tokens": class_tokens, "rect": {}}]
            },
        }
    }


def test_home_popup_sequence_sees_app_popups_through_declared_obstruction() -> None:
    """App 弹窗节点类名里没有 dialog / popup，靠展示层（含采集声明的遮挡）认出来。"""
    app_popup = _presentation_packet(
        topmost="overlay", hit_test="unknown", class_tokens=["android.widget.FrameLayout"]
    )
    app_clean = _presentation_packet(
        topmost="page", hit_test="unknown", class_tokens=["android.widget.FrameLayout"]
    )
    base = _popup_context([True, True, False, False])

    judgement = evaluate_aggregate_context(
        _with_packets(base, [app_popup, app_popup, app_clean, app_clean])
    )

    assert judgement.execution_status == "pass"
    assert judgement.wrapper_payload["chain_length"] == 2


def test_home_popup_sequence_ignores_player_mask_when_page_is_topmost() -> None:
    """类名像遮罩但结构命中测试确认最上层是页面：不算弹窗，不能误报「稍后载入」。"""
    popup = _presentation_packet(topmost="overlay", hit_test="available", class_tokens=["ad"])
    clean = _presentation_packet(topmost="page", hit_test="available", class_tokens=["feed"])
    player_mask = _presentation_packet(
        topmost="page", hit_test="available", class_tokens=["video-mask"]
    )
    base = _popup_context([True, True, False, False])

    judgement = evaluate_aggregate_context(
        _with_packets(base, [popup, popup, clean, player_mask])
    )

    assert judgement.execution_status == "pass"
    assert judgement.wrapper_payload["late_frame_count"] == 0


def test_home_popup_sequence_trusts_page_detection_popup_state() -> None:
    """页面识别说是弹窗状态就算弹窗，哪怕采集声明了「没有遮挡」（H5 黑料网实况）。"""
    declared_clear = {
        "protocol_version": "presentation-stack.v1",
        "topmost_layer_kind": "page",
        "hit_test_status": "available",
    }
    popup = {"page": {"page_state": "popup"}, "facts": {"presentation": declared_clear}}
    clean = {"page": {"page_state": "stable"}, "facts": {"presentation": declared_clear}}
    base = _popup_context([True, True, False, False])

    judgement = evaluate_aggregate_context(_with_packets(base, [popup, popup, clean, clean]))

    assert judgement.execution_status == "pass"
    assert judgement.wrapper_payload["chain_length"] == 2


def _packetless_manifest(capture_id: str):
    """生产形态清单：捕获提交时封存，判定包尚未产生，包引用为空。"""
    return build_evidence_manifest_reference(
        task_id="task-1",
        session_id="session-1",
        capture_id=capture_id,
        manifest_version=1,
        artifacts=(
            ArtifactReference(
                artifact_id=f"artifact-{capture_id}",
                artifact_type="ui_snapshot",
                role="observation",
                storage_path=f"evidence/{capture_id}.json",
                checksum="sha256:" + "a" * 64,
                size_bytes=10,
                content_type="application/json",
            ),
        ),
        quality_status=EvidenceQualityStatus.PASS,
        quality_reason="artifact_integrity_verified",
        observation_packet_ids=(),
        created_at=NOW,
    )


def _popup_context_without_packet_refs(modal_flags: list[bool]):
    """成员清单封存早于判定包产生：包引用为空，依赖 capture 兜底包。"""
    base = _popup_context(modal_flags)
    candidates = tuple(
        EvidenceInputCandidate(
            manifest=_packetless_manifest(f"capture-{index}"),
            capture_role="observation",
            page_sequence=index,
        )
        for index in range(1, len(modal_flags) + 1)
    )
    input_set = seal_evidence_input_set(
        task_id="task-1",
        source_agent_run_id="run-1",
        rule_id="rule-22",
        rule_code="alioth_22",
        rule_contract_hash="contract-hash",
        ruleset_snapshot_hash="ruleset-hash",
        scope=EvidenceInputScope.TASK,
        scope_key="home_popup_sequence",
        finalization_epoch=2,
        eligibility_snapshot_id="snapshot-1",
        eligibility_snapshot_hash="snapshot-hash",
        selection_policy_hash="policy-hash",
        eligible_capture_ids=tuple(f"capture-{i}" for i in range(1, len(modal_flags) + 1)),
        candidates=candidates,
        sealed_at=NOW,
    )
    capture_packets = {
        f"capture-{index}": base.observation_packets[f"packet-{index}"]
        for index in range(1, len(modal_flags) + 1)
    }
    return AggregateJudgeContext(
        lease=base.lease,
        input_set=input_set,
        observation_packets={},
        capture_packets=capture_packets,
    )


def test_home_popup_sequence_reads_capture_fallback_packets() -> None:
    """生产清单不含包引用（封存时判定包尚未产生），judgments 必须仍可用。"""
    passed = evaluate_aggregate_context(
        _popup_context_without_packet_refs([True, True, True, False, False])
    )
    failed = evaluate_aggregate_context(
        _popup_context_without_packet_refs([True, True, False, True])
    )
    starving = evaluate_aggregate_context(
        AggregateJudgeContext(
            lease=_popup_context([True]).lease,
            input_set=_popup_context_without_packet_refs([True]).input_set,
            observation_packets={},
            capture_packets={},
        )
    )

    assert passed.execution_status == "pass"
    assert failed.execution_status == "fail"
    assert starving.execution_status == "evidence_insufficient"
    assert starving.error_code == "POPUP_SEQUENCE_MEMBER_INSUFFICIENT"


def test_toast_aggregate_judge_pass_fail_and_monitor_gap() -> None:
    common_style = {
        "font_size": "16px",
        "text_color": "white",
        "background_color": "black",
        "background_opacity": 0.7,
    }
    passed = evaluate_aggregate_context(_context(style_two=common_style))
    failed = evaluate_aggregate_context(_context(style_two={**common_style, "font_size": "14px"}))
    insufficient = evaluate_aggregate_context(
        _context(style_two=common_style, monitor_status="not_started")
    )

    assert passed.execution_status == "pass"
    assert failed.execution_status == "fail"
    assert insufficient.execution_status == "evidence_insufficient"
    assert insufficient.error_code == "TOAST_MONITOR_NOT_OBSERVED"


class _AsyncContext:
    def __init__(self, value: Any) -> None:
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class _ExecutionConnection:
    def __init__(
        self,
        *,
        frozen_packet_row: dict[str, Any] | None = None,
        frozen_execution_row: dict[str, Any] | None = None,
    ) -> None:
        self.queries: list[tuple[str, tuple[Any, ...]]] = []
        # 注入既有冻结行可模拟「命中同一 Work Item 幂等身份」；缺省回显本次
        # 写入参数，等价首写时 RETURNING 刚插入的内容。
        self.frozen_packet_row = frozen_packet_row
        self.frozen_execution_row = frozen_execution_row
        self.fetchrow_results = [
            {"attempt_count": 1, "max_attempts": 3},
            {"id": "attempt-1", "started_at": NOW},
        ]
        self.fetchval_results = [
            "attempt-1",
            "work-1",
            "candidate-1",
        ]

    def transaction(self) -> _AsyncContext:
        return _AsyncContext(self)

    async def fetchrow(self, query: str, *args: Any):
        normalized = " ".join(query.split())
        self.queries.append((normalized, args))
        if "SELECT task.target_platform" in normalized:
            return {"target_platform": "h5", "task_config": {}}
        if "INSERT INTO qc_observation_packet" in normalized:
            if self.frozen_packet_row is not None:
                return self.frozen_packet_row
            return {
                "packet_id": "packet-aggregate",
                "evidence_artifact_ids": args[2],
                "packet_payload": args[3],
                "quality_gate_status": args[4],
                "quality_gate_reason": args[5],
            }
        if "INSERT INTO qc_rule_execution" in normalized:
            if self.frozen_execution_row is not None:
                return self.frozen_execution_row
            return {
                "execution_id": "execution-aggregate",
                "observation_packet_id": args[1],
                "execution_status": args[2],
                "confidence": args[3],
                "result_summary": args[4],
                "problem_desc": args[5],
                "evidence_artifact_ids": args[6],
                "judge_input": args[7],
                "judge_output": args[8],
                "review_status": args[9],
                "review_reason": args[10],
                "error_code": args[11],
            }
        return self.fetchrow_results.pop(0)

    async def fetchval(self, query: str, *args: Any):
        self.queries.append((" ".join(query.split()), args))
        return self.fetchval_results.pop(0)

    async def execute(self, query: str, *args: Any):
        self.queries.append((" ".join(query.split()), args))
        return "OK"


class _ExecutionPool:
    def __init__(self, connection: _ExecutionConnection) -> None:
        self.connection = connection

    def acquire(self) -> _AsyncContext:
        return _AsyncContext(self.connection)


class _EmptyExecutionConnection:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def transaction(self) -> _AsyncContext:
        return _AsyncContext(self)

    async def fetch(self, query: str, *args: Any):
        del args
        self.queries.append(" ".join(query.split()))
        return []


class _StaleWakeupExecutionConnection:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self._rows = [
            {
                "status": "pending",
                "row_version": 2,
                "attempt_count": 0,
                "max_attempts": 3,
            },
            None,
        ]

    def transaction(self) -> _AsyncContext:
        return _AsyncContext(self)

    async def fetchrow(self, query: str, *args: Any):
        del args
        self.queries.append(" ".join(query.split()))
        return self._rows.pop(0)


async def test_aggregate_wakeup_claim_includes_review_session_draining_tasks() -> None:
    connection = _StaleWakeupExecutionConnection()
    repository = PostgresAggregateInspectionExecutionRepository(
        pool=_ExecutionPool(connection)
    )

    claim = await repository.claim_for_wakeup(
        work_item_id="11111111-1111-1111-1111-111111111111",
        task_id="22222222-2222-2222-2222-222222222222",
        message_version=2,
        lease_owner="worker-1",
        lease_seconds=30,
    )

    assert claim.disposition.value == "stale"
    update_query = next(
        query
        for query in connection.queries
        if "UPDATE qc_aggregate_inspection_work_item AS work_item" in query
    )
    # App／H5 在 review+session_draining 均可领聚合（不得只认 h5）。
    assert "task.status = 'review'" in update_query
    assert "task.target_platform IN ('h5', 'app')" in update_query
    assert "task.finalization_state = 'session_draining'" in update_query


async def test_aggregate_reconcile_includes_review_session_draining_tasks() -> None:
    connection = _EmptyExecutionConnection()
    repository = PostgresAggregateInspectionExecutionRepository(
        pool=_ExecutionPool(connection)
    )

    assert await repository.reconcile() == 0

    reconcile_query = connection.queries[-1]
    assert "task.status = 'review'" in reconcile_query
    assert "task.target_platform IN ('h5', 'app')" in reconcile_query
    assert "task.finalization_state = 'session_draining'" in reconcile_query


async def test_evidence_insufficient_result_creates_review_item_atomically() -> None:
    context = _context(
        style_two={
            "font_size": "16px",
            "text_color": "white",
            "background_color": "black",
            "background_opacity": 0.7,
        },
        monitor_status="not_started",
    )
    judgement = evaluate_aggregate_context(context)
    connection = _ExecutionConnection()
    repository = PostgresAggregateInspectionExecutionRepository(pool=_ExecutionPool(connection))

    await repository.complete_judgement(context=context, judgement=judgement)

    review_query, review_args = next(
        (query, args) for query, args in connection.queries if "INSERT INTO qc_review_item" in query
    )
    candidate_call = next(
        args
        for query, args in connection.queries
        if "UPDATE qc_aggregate_inspection_candidate" in query
    )
    assert "trigger_reason" in review_query
    assert review_args[3] == "evidence_insufficient"
    assert "ON CONFLICT (rule_execution_id)" in review_query
    assert candidate_call[1] == "completed"


def _frozen_packet_row_for(judgement: Any) -> dict[str, Any]:
    return {
        "packet_id": "packet-frozen",
        "evidence_artifact_ids": json.dumps(list(judgement.evidence_artifact_ids)),
        "packet_payload": json.dumps(dict(judgement.wrapper_payload), default=str),
        "quality_gate_status": (
            "fail" if judgement.execution_status == "evidence_insufficient" else "pass"
        ),
        "quality_gate_reason": judgement.problem_desc,
    }


async def test_replayed_judgement_with_identical_content_is_idempotent() -> None:
    """重放携带相同内容命中冻结 Packet：幂等成功，链路继续写终态。"""
    context = _context(
        style_two={
            "font_size": "16px",
            "text_color": "white",
            "background_color": "black",
            "background_opacity": 0.7,
        },
    )
    judgement = evaluate_aggregate_context(context)
    connection = _ExecutionConnection(frozen_packet_row=_frozen_packet_row_for(judgement))
    repository = PostgresAggregateInspectionExecutionRepository(pool=_ExecutionPool(connection))

    await repository.complete_judgement(context=context, judgement=judgement)

    assert any("INSERT INTO qc_rule_execution" in query for query, _ in connection.queries)


async def test_conflicting_judgement_content_fails_closed() -> None:
    """并发消费者携带不同判定命中同一 Work Item：拒绝静默复用并整体回滚。"""
    context = _context(
        style_two={
            "font_size": "16px",
            "text_color": "white",
            "background_color": "black",
            "background_opacity": 0.7,
        },
    )
    judgement = evaluate_aggregate_context(context)
    frozen = _frozen_packet_row_for(judgement)
    frozen["packet_payload"] = json.dumps({"judged_by": "另一个消费者", "verdict": "fail"})
    connection = _ExecutionConnection(frozen_packet_row=frozen)
    repository = PostgresAggregateInspectionExecutionRepository(pool=_ExecutionPool(connection))

    with pytest.raises(AggregateInspectionExecutionPersistenceError, match="冻结"):
        await repository.complete_judgement(context=context, judgement=judgement)

    assert not any("INSERT INTO qc_rule_execution" in query for query, _ in connection.queries)


def _frozen_execution_row_for(
    judgement: Any,
    *,
    packet_id: str = "packet-frozen",
) -> dict[str, Any]:
    lease_scope = {
        "evidence_input_set_id": "input-set-1",
        "input_hash": "hash-1",
        "scope": "task",
        "scope_key": "toast_style",
    }
    return {
        "execution_id": "execution-frozen",
        "observation_packet_id": packet_id,
        "execution_status": judgement.execution_status,
        "confidence": judgement.confidence,
        "result_summary": judgement.result_summary,
        "problem_desc": judgement.problem_desc,
        "evidence_artifact_ids": json.dumps(list(judgement.evidence_artifact_ids)),
        "judge_input": json.dumps(lease_scope),
        "judge_output": json.dumps(judgement.to_result_payload(), default=str),
        "review_status": judgement.review_status,
        "review_reason": judgement.review_reason,
        "error_code": judgement.error_code,
    }


async def test_replayed_rule_execution_with_identical_content_is_idempotent() -> None:
    """重放相同聚合判定命中冻结 RuleExecution：幂等成功。"""
    context = _context(
        style_two={
            "font_size": "16px",
            "text_color": "white",
            "background_color": "black",
            "background_opacity": 0.7,
        },
    )
    judgement = evaluate_aggregate_context(context)
    packet = _frozen_packet_row_for(judgement)
    execution = _frozen_execution_row_for(judgement, packet_id=packet["packet_id"])
    # judge_input 必须与仓储写入字段一致
    execution["judge_input"] = json.dumps(
        {
            "evidence_input_set_id": context.lease.evidence_input_set_id,
            "input_hash": context.lease.input_hash,
            "scope": context.lease.scope,
            "scope_key": context.lease.scope_key,
        }
    )
    connection = _ExecutionConnection(
        frozen_packet_row=packet,
        frozen_execution_row=execution,
    )
    repository = PostgresAggregateInspectionExecutionRepository(pool=_ExecutionPool(connection))

    await repository.complete_judgement(context=context, judgement=judgement)

    assert any("INSERT INTO qc_rule_execution" in query for query, _ in connection.queries)


async def test_conflicting_rule_execution_content_fails_closed() -> None:
    """聚合 RuleExecution 已冻结且判定漂移：拒绝覆盖。"""
    context = _context(
        style_two={
            "font_size": "16px",
            "text_color": "white",
            "background_color": "black",
            "background_opacity": 0.7,
        },
    )
    judgement = evaluate_aggregate_context(context)
    packet = _frozen_packet_row_for(judgement)
    execution = _frozen_execution_row_for(judgement, packet_id=packet["packet_id"])
    execution["execution_status"] = "fail"
    execution["result_summary"] = "另一个消费者的结论"
    connection = _ExecutionConnection(
        frozen_packet_row=packet,
        frozen_execution_row=execution,
    )
    repository = PostgresAggregateInspectionExecutionRepository(pool=_ExecutionPool(connection))

    with pytest.raises(AggregateInspectionExecutionPersistenceError, match="RuleExecution"):
        await repository.complete_judgement(context=context, judgement=judgement)
