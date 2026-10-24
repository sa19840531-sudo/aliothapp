"""本轮缺文案重复补证案例及不可共享的反例。"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.application.h5_recapture_coalescing import (
    H5GapEvidence,
    compatible_observation_key,
    compatible_probe_pair,
    unchanged_gap,
)
from services.api.app.domain.exploration import build_probe_work
from services.api.app.domain.inspection import (
    EvidenceRequiredEquivalence,
    EvidenceSideEffectPolicy,
    build_evidence_request,
)
from services.api.tests.test_h5_probe_work import _work
from services.api.tests.test_h5_targeted_recapture import _snapshot_request


def request(*, capture="source", rule="rule-one", missing="visible_text", **changes):
    base = _snapshot_request()
    payload = base.to_dict()
    for name in ("request_id", "requirement_hash", "protocol_version"):
        payload.pop(name)
    for name in (
        "session_dependency",
        "replay_policy",
        "required_equivalence",
        "side_effect_policy",
        "minimum_source_grade",
    ):
        payload[name] = getattr(base, name)
    payload.update(
        target_capture_id=capture,
        rule_id=rule,
        rule_code=rule,
        reason_code="EVIDENCE_INSUFFICIENT",
        required_artifact_types=("screenshot", "dom", "visible_text"),
        evidence_goal=(f"video_list 的「文案规则-{rule}」：缺来源 {missing}(non_empty)。"
                       "允许在等价状态下补采。"),
        target_invariants={
            "environment": {"surface_kind": "web"},
            "business": {"page_type": "video_list", "url_pattern": "https://example.test/list"},
            "semantic": {"page_state": "popup", "region": f"文案规则-{rule}"},
            "structure": {"layer_target": "base_page"},
        },
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    payload.update(changes)
    return build_evidence_request(**payload)


def test_real_visible_text_requests_share_observation_not_rule_identity():
    first, second = request(), request(rule="rule-two")
    assert first.request_id != second.request_id
    assert first.requirement_hash != second.requirement_hash
    assert compatible_observation_key(first) == compatible_observation_key(second)
    assert compatible_observation_key(first) is not None


def test_one_full_read_only_capture_can_supply_different_missing_artifacts():
    artifacts = ("visible_text", "dom", "ui_snapshot", "screenshot")
    first = request(missing="visible_text", required_artifact_types=artifacts)
    second = request(rule="rule-two", missing="dom", required_artifact_types=artifacts)
    assert compatible_observation_key(first) == compatible_observation_key(second)
    assert compatible_observation_key(first) is not None


def test_missing_artifact_not_in_requested_profile_is_not_shareable():
    assert compatible_observation_key(
        request(missing="dom", required_artifact_types=("screenshot",))
    ) is None


def test_observed_128_missing_text_profile_is_completed_before_sharing():
    from services.api.app.application.h5_recapture_coalescing import complete_observation_profile

    # 本轮三条 profile 请求的缺来源明确，但原请求只带 screenshot/ui_snapshot。
    original = request(required_artifact_types=("screenshot", "ui_snapshot"))
    updated = complete_observation_profile(original)
    assert "visible_text" in updated.required_artifact_types
    assert original.request_id != updated.request_id
    assert original.requirement_hash != updated.requirement_hash
    assert compatible_observation_key(updated) is not None
    assert original.required_artifact_types == ("screenshot", "ui_snapshot")


@pytest.mark.parametrize("goal", [
    "广告加载失败时的页面。允许重新观察当前页面。",
    "采集列表底部和滚动前后截图。允许重新观察当前页面。",
    "缺来源 media_state。允许在等价状态下补采。",
])
def test_completing_profile_does_not_convert_conditional_goals_to_snapshot(goal):
    from services.api.app.application.h5_recapture_coalescing import complete_observation_profile

    original = request(evidence_goal=goal)
    assert complete_observation_profile(original) is original


@pytest.mark.parametrize(
    "changes",
    [
        {"target_capture_id": "another-object"},
        {"required_equivalence": EvidenceRequiredEquivalence.ORIGINAL_ATOMIC},
        {"side_effect_policy": EvidenceSideEffectPolicy.APPROVAL_REQUIRED},
        {"target_invariants": {"structure": {"layer_target": "overlay"}}},
        {"evidence_goal": "广告加载失败时的页面。允许重新观察当前页面。"},
        {"evidence_goal": "采集到列表底部，再对比切换标签前后截图"},
        {"evidence_goal": "来源采集未证明目标内容处于失效状态。允许在等价状态下补采。"},
        {"target_invariants": {"semantic": {"capture_roles": ["before"]}}},
    ],
)
def test_observation_sharing_does_not_guess_implicit_goal(changes):
    assert compatible_observation_key(request(**changes)) != compatible_observation_key(request())


def evidence(**changes):
    return replace(
        H5GapEvidence(
            task_id="task-h5",
            agent_run_id="run-h5",
            finalization_epoch=4,
            session_id="h5:run-h5",
            logical_page_key="object-one",
            state_fingerprint="stable-one",
            contract_hash="contract-one",
            content_hash="all-relevant-artifacts-one",
            identity_confirmed=True,
            page_state="stable",
            is_overlay=False,
            is_external=False,
        ),
        **changes,
    )


def test_same_gap_across_new_captures_converges_only_with_same_evidence():
    first, second = request(), request(capture="new-frame")
    assert first.request_id != second.request_id
    assert unchanged_gap(first, evidence(), second, evidence())


@pytest.mark.parametrize(
    "changes",
    [
        {"task_id": "other-task"},
        {"agent_run_id": "other-run"},
        {"finalization_epoch": 5},
        {"session_id": "h5:other-session"},
        {"logical_page_key": "other-object"},
        {"state_fingerprint": "new-state"},
        {"contract_hash": "new-rule-version"},
        {"content_hash": "changed-text-or-image"},
        {"identity_confirmed": False},
        {"page_state": "loading"},
        {"is_overlay": True},
        {"is_external": True},
        {"is_overlay": None},
        {"is_external": None},
        {"content_hash": ""},
    ],
)
def test_changed_evidence_must_allow_another_probe(changes):
    assert not unchanged_gap(
        request(), evidence(), request(capture="new-frame"), evidence(**changes)
    )


def test_new_condition_or_rule_is_not_same_gap():
    assert not unchanged_gap(request(), evidence(), request(rule="another-rule"), evidence())
    assert not unchanged_gap(request(), evidence(), request(missing="screenshot"), evidence())


def test_unchanged_conditional_gap_can_converge_but_cannot_share_or_invent_condition():
    first = request(evidence_goal="补采广告加载失败条件下的页面")
    second = request(capture="new-frame", evidence_goal=first.evidence_goal)
    assert compatible_observation_key(first) is None
    assert unchanged_gap(first, evidence(), second, evidence())


def test_timing_and_interaction_requirements_cannot_converge():
    item = request(target_invariants={"semantic": {"capture_roles": ["before", "after"]}})
    assert not unchanged_gap(item, evidence(), item, evidence())


def test_shared_probe_keeps_each_request_and_restoration_identity():
    first = _work(request=request())
    second = probe(
        request(rule="rule-two"),
        restoration_attempt_id="another-restoration",
        equivalence_assessment_id="another-assessment",
    )
    assert compatible_probe_pair(first, second)
    assert first.work_id != second.work_id
    assert first.evidence_request.request_id != second.evidence_request.request_id
    assert first.restoration_attempt_id != second.restoration_attempt_id


@pytest.mark.parametrize(
    "changes",
    [
        {"source_capture_id": "new-restored-frame"},
        {"session_id": "h5:another-session"},
        {"evidence_request": request(capture="another-original")},
        {"evidence_request": request(evidence_goal="滚动至广告失败条件出现")},
    ],
)
def test_shared_probe_rejects_other_source_or_conditional_goal(changes):
    leader = _work(request=request())
    follower = probe(request(rule="rule-two"), **changes)
    assert not compatible_probe_pair(leader, follower)


def probe(item, **changes):
    from dataclasses import fields

    base = _work(request=item)
    values = {
        field.name: getattr(base, field.name)
        for field in fields(base)
        if field.name not in {"work_id", "task_id", "protocol_version"}
    }
    values.update(changes)
    return build_probe_work(**values)
