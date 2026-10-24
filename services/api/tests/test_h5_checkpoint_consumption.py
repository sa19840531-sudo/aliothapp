"""检查点消费与连续观测的受控播放器案例和误通过反例。"""

from copy import deepcopy

import pytest

from services.api.app.domain.journey import parse_journey_definition
from services.api.tests.test_journey_checkpoint_contracts import checkpoint_definition


def sampled_pause_definition():
    payload = checkpoint_definition()
    raw = payload["checkpoints"][1]
    raw["evidence_requirements"][0]["artifact_types"] = ["media_state"]
    raw["evidence_requirements"].append(
        {"role": "pause_action", "artifact_types": ["action_receipt"]}
    )
    return payload


def sample(*, instant=1000, paused=True, media_time=5):
    return {
        "video_frame": {
            "status": "presented", "media_instance_id": "media-1",
            "resource_generation": 1, "presented_frames": 3,
            "media_time": media_time, "width": 640, "height": 360,
        },
        "video_region_sha256": "b" * 64,
        "presentation": {
            "topmost_layer_kind": "page",
            "base_page_visibility": "visible",
            "mutation_mode": "natural",
        },
        "observed_at_ms": instant,
        "screenshot_sha256": "a" * 64,
        "frame_origin": {
            "status": "bracketed",
            "screenshot_sha256": "a" * 64,
            "before": {"url": "https://example.test/video/1", "time_origin_ms": 10},
            "after": {"url": "https://example.test/video/1", "time_origin_ms": 10},
        },
        "snapshot": {
            "url": "https://example.test/video/1",
            "document": {"time_origin_ms": 10},
            "viewport": {"width": 360, "height": 640},
            "elements": [
                {
                    "tag": "video",
                    "visible": True,
                    "h5_media_instance_id": "media-1",
                    "bounds": {"x": 0, "y": 10, "width": 360, "height": 200},
                    "media": {
                        "paused": paused,
                        "current_time": media_time,
                        "ready_state": 4,
                        "seeking": False,
                        "video_width": 640,
                        "video_height": 360,
                    },
                }
            ],
        },
    }


def test_two_post_pause_observations_measure_stability_and_stopped_time():
    from services.api.app.application.checkpoint_judgement import measure_checkpoint_series

    measured = measure_checkpoint_series([sample(), sample(instant=1600)])
    assert measured["stable_frame"] is True
    assert measured["media_time_stopped"] is True


@pytest.mark.parametrize(
    "fault",
    [
        "one",
        "same_clock",
        "negative_clock",
        "instance",
        "document",
        "url",
        "loading",
        "image_frame",
        "broken_frame",
        "truncated",
        "occluded",
        "seeking",
        "offscreen",
    ],
)
def test_untrusted_or_stale_samples_cannot_measure_success(fault):
    from services.api.app.application.checkpoint_judgement import measure_checkpoint_series

    samples = [sample(), sample(instant=1600)]
    last = samples[-1]
    if fault == "one":
        samples.pop()
    elif fault == "same_clock":
        last["observed_at_ms"] = 1000
    elif fault == "negative_clock":
        last["observed_at_ms"] = 900
    elif fault == "instance":
        last["snapshot"]["elements"][0]["h5_media_instance_id"] = "other"
    elif fault == "document":
        last["snapshot"]["document"]["time_origin_ms"] = 20
    elif fault == "url":
        last["snapshot"]["url"] = "https://example.test/video/2"
    elif fault == "loading":
        last["snapshot"]["elements"][0]["media"]["ready_state"] = 1
    elif fault == "image_frame":
        last["frame_origin"]["screenshot_sha256"] = "b" * 64
    elif fault == "broken_frame":
        last["frame_origin"]["status"] = "renderer_unavailable"
    elif fault == "occluded":
        last["presentation"]["topmost_layer_kind"] = "overlay"
    elif fault == "seeking":
        last["snapshot"]["elements"][0]["media"]["seeking"] = True
    elif fault == "offscreen":
        last["snapshot"]["elements"][0]["bounds"]["y"] = 1000
    else:
        last["snapshot"]["truncated"] = True
    assert measure_checkpoint_series(samples) == {}


def test_advancing_time_is_explicit_failure_not_missing_evidence():
    from services.api.app.application.checkpoint_judgement import measure_checkpoint_series

    measured = measure_checkpoint_series([sample(), sample(instant=1600, media_time=6)])
    assert measured["media_time_stopped"] is False


def test_two_observations_cannot_count_as_two_action_receipts():
    from services.api.app.application.checkpoint_judgement import judge_checkpoint
    from services.api.app.application.h5_checkpoint_observation import supports_checkpoint

    payload = checkpoint_definition()
    raw = payload["checkpoints"][1]
    raw["depends_on"] = []
    raw["evidence_requirements"][0]["artifact_types"] = ["action_receipt"]
    raw["evidence_requirements"][0]["minimum_count"] = 2
    payload["checkpoints"] = [raw]
    checkpoint = parse_journey_definition(payload).checkpoints[0]
    manifests = [
        {
            "quality_status": "pass",
            "artifacts": [{"artifact_type": "checkpoint_observation"}],
        }
        for _ in range(2)
    ]
    assert supports_checkpoint(checkpoint) is False
    result = judge_checkpoint(
        checkpoint,
        before=sample(paused=False)["snapshot"],
        after=sample()["snapshot"],
        manifests=manifests,
        series=[sample(), sample(instant=1600)],
    )
    assert result["status"] == "evidence_insufficient"


async def test_sampler_retains_both_real_pngs_and_uses_same_document_clock():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from services.api.app.infrastructure.browser.h5_checkpoint_sampling import (
        collect_checkpoint_samples,
    )

    counter = iter([1.0, 1.6])
    frame = sample()["frame_origin"]
    frames = AsyncMock(return_value=(b"actual-png", frame))
    snapshots = AsyncMock(return_value=sample()["snapshot"])
    delay = AsyncMock()
    result = await collect_checkpoint_samples(
        SimpleNamespace(),
        read_snapshot=snapshots,
        capture_frame=frames,
        sleep=delay,
        clock=lambda: next(counter),
    )
    assert len(result["pngs"]) == 2
    assert result["samples"][1]["observed_at_ms"] - result["samples"][0]["observed_at_ms"] == 600
    delay.assert_awaited_once_with(0.6)
    frames.assert_awaited()


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "missing",
        "pending",
        "contradiction",
        "unknown",
        "other_sample_instance",
    ],
)
def test_generic_judge_does_not_copy_expected_values_or_promote_unknown(fault):
    from services.api.app.application.checkpoint_judgement import judge_checkpoint

    payload = sampled_pause_definition()
    raw = payload["checkpoints"][1]
    raw["depends_on"] = []
    payload["checkpoints"] = [raw]
    checkpoint = parse_journey_definition(payload).checkpoints[0]
    before = sample(paused=False)["snapshot"]
    after = sample()["snapshot"]
    series = [sample(), sample(instant=1600)]
    kinds = [{"artifact_type": "media_state"}, {"artifact_type": "checkpoint_observation"}]
    manifests = [{"quality_status": "pass", "artifacts": deepcopy(kinds)} for _ in range(2)]
    if fault == "missing":
        series = []
    elif fault == "pending":
        manifests[-1]["quality_status"] = "pending"
    elif fault == "contradiction":
        series[-1]["snapshot"]["elements"][0]["media"]["current_time"] = 6
        series[-1]["video_frame"]["media_time"] = 6
    elif fault == "unknown":
        checkpoint = parse_journey_definition(
            {
                **payload,
                "checkpoints": [
                    {**raw, "expected": {"unregistered_fact": True}},
                ],
            }
        ).checkpoints[0]
    elif fault == "other_sample_instance":
        for item in series:
            item["snapshot"]["elements"][0]["h5_media_instance_id"] = "other"
    result = judge_checkpoint(
        checkpoint, before=before, after=after, manifests=manifests, series=series
    )
    assert result["status"] == (
        "pass" if fault is None else "fail" if fault == "contradiction" else "evidence_insufficient"
    )


async def test_journey_publication_cannot_use_unbound_longest_path_as_checkpoint_input():
    from dataclasses import replace

    from services.api.app.application.aggregate_inspection_publication import (
        AggregateInspectionPublicationService,
        AggregatePublicationSnapshot,
    )
    from services.api.tests.test_aggregate_inspection_runtime import (
        FakePublicationRepository,
        _candidate,
        _rule,
    )

    rule = _rule(scope="journey", scope_key="journey_observation")
    entries = tuple(
        _candidate(key, "p-" + key, sequence=index) for index, key in enumerate(("a", "b"), 1)
    )
    entries = tuple(
        replace(
            entry, candidate=replace(entry.candidate, journey_execution_id="unbound-longest-path")
        )
        for entry in entries
    )
    snapshot = AggregatePublicationSnapshot(
        task_id="task-1",
        source_agent_run_id="run-1",
        finalization_epoch=0,
        ruleset_snapshot_hash="snapshot",
        rules=(rule,),
        evidence=entries,
        checkpoint_journeys=True,
    )
    repository = FakePublicationRepository(snapshot)
    result = await AggregateInspectionPublicationService(repository=repository).prepare(
        task_id="task-1",
        agent_run_id="run-1",
        finalization_epoch=0,
    )
    assert result.published_work_item_count == 0
    assert result.evidence_insufficient_count == 1


def journey_context():
    from dataclasses import replace
    from datetime import UTC, datetime

    from services.api.app.domain.evidence import (
        ArtifactReference,
        EvidenceInputCandidate,
        EvidenceInputScope,
        EvidenceQualityStatus,
        build_evidence_manifest_reference,
        seal_evidence_input_set,
    )
    from services.api.app.domain.journey.definitions import build_run_journey_execution_plan
    from services.api.tests.test_aggregate_inspection_runtime import _context

    base = _context(style_two={})
    payload = sampled_pause_definition()
    raw = payload["checkpoints"][1]
    raw["depends_on"] = []
    payload["checkpoints"] = [raw]
    plan = build_run_journey_execution_plan(
        parse_journey_definition(payload),
        task_id="task-1",
        agent_run_id="run-1",
        scenario_key="player",
        parameters={},
        rule_contract_hashes=("contract-hash",),
    )
    manifests = []
    for index, role in enumerate(("before_action", "after_action")):
        manifests.append(
            build_evidence_manifest_reference(
                task_id="task-1",
                session_id="session-1",
                capture_id=f"capture-{index}",
                manifest_version=1,
                quality_status=EvidenceQualityStatus.PASS,
                quality_reason="controlled",
                created_at=datetime.now(UTC),
                artifacts=tuple(
                    ArtifactReference(
                        artifact_id=f"artifact-{index}-{kind}",
                        artifact_type=kind,
                        role=role,
                        storage_path=f"evidence/{index}-{kind}.json",
                        checksum="sha256:" + "a" * 64,
                        size_bytes=10,
                        content_type="application/json",
                    )
                    for kind in ("screenshot", "media_state", "checkpoint_observation")
                ),
            )
        )
    input_set = seal_evidence_input_set(
        task_id="task-1",
        source_agent_run_id="run-1",
        rule_id="rule-1",
        rule_code="code",
        rule_contract_hash="contract-hash",
        ruleset_snapshot_hash="ruleset-hash",
        scope=EvidenceInputScope.JOURNEY,
        scope_key="journey_observation",
        finalization_epoch=2,
        eligibility_snapshot_id="snapshot",
        eligibility_snapshot_hash="snapshot-hash",
        selection_policy_hash="policy-hash",
        eligible_capture_ids=[item.capture_id for item in manifests],
        candidates=[
            EvidenceInputCandidate(
                manifest=item,
                capture_role=role,
                page_sequence=index,
                journey_execution_id=plan.execution_plan_id,
            )
            for index, (item, role) in enumerate(
                zip(manifests, ("before_action", "after_action"), strict=True), 1
            )
        ],
        sealed_at=datetime.now(UTC),
    )
    bindings = [
        dict(
            capture_id=item.capture_id,
            manifest_id=item.manifest_id,
            manifest_version=item.manifest_version,
            role=role,
        )
        for item, role in zip(manifests, ("before_action", "after_action"), strict=True)
    ]
    material = {
        "contract": {
            "plan": plan.to_dict(),
            "epoch": 2,
            "rule_contract_hash": "contract-hash",
            "checkpoints": [
                {
                    "checkpoint_id": "paused",
                    "bindings": bindings,
                    "manifests": [item.to_dict() for item in manifests],
                }
            ],
        },
        "observations": {
            "paused": {
                "before": sample(paused=False)["snapshot"],
                "after": sample()["snapshot"],
                "series": [sample(), sample(instant=1600)],
            }
        },
    }
    return replace(
        base,
        input_set=input_set,
        journey_input=material,
        lease=replace(
            base.lease,
            scope="journey",
            scope_key="journey_observation",
            input_hash=input_set.input_set_hash,
            evidence_input_set_id=input_set.input_set_id,
        ),
    )


@pytest.mark.parametrize("fault", [None, "missing", "epoch", "binding", "series", "contradiction"])
def test_aggregate_judge_consumes_exact_journey_input_and_keeps_gaps(fault):
    from dataclasses import replace

    from services.api.app.application.aggregate_inspection_execution import (
        evaluate_aggregate_context,
    )

    context = journey_context()
    material = deepcopy(context.journey_input)
    if fault == "missing":
        material = None
    elif fault == "epoch":
        material["contract"]["epoch"] = 3
    elif fault == "binding":
        material["contract"]["checkpoints"][0]["bindings"][0]["capture_id"] = "other"
    elif fault == "series":
        material["observations"]["paused"]["series"] = []
    elif fault == "contradiction":
        material["observations"]["paused"]["series"][-1]["snapshot"]["elements"][0]["media"][
            "current_time"
        ] = 6
        material["observations"]["paused"]["series"][-1]["video_frame"]["media_time"] = 6
    verdict = evaluate_aggregate_context(replace(context, journey_input=material))
    assert verdict.execution_status == (
        "pass" if fault is None else "fail" if fault == "contradiction" else "evidence_insufficient"
    )
    if verdict.execution_status == "evidence_insufficient":
        assert verdict.review_status == "pending"


def test_formal_report_projection_preserves_bound_journey_results_and_rejects_tampering():
    from services.api.app.application.report_insight import _bound_final_snapshot_insight
    from services.api.app.domain.journey.validation import stable_hash

    binding = {
        "schema_version": "h5-journey-report.v1",
        "task_id": "task-1",
        "agent_run_id": "run-1",
        "finalization_epoch": 2,
        "gap_count": 0,
        "results": [{"execution_status": "pass", "checkpoints": [{"status": "pass"}]}],
    }
    binding["snapshot_hash"] = stable_hash(binding)
    snapshot = {
        "task": {"task_id": "task-1"},
        "agent_run_id": "run-1",
        "completion_gate": {"finalization_epoch": 2},
        "h5_journeys": binding,
    }
    projected = _bound_final_snapshot_insight(snapshot, target_platform="h5")
    assert projected["h5_journeys"] == binding
    forged = deepcopy(snapshot)
    forged["h5_journeys"]["results"][0]["execution_status"] = "fail"
    with pytest.raises(ValueError):
        _bound_final_snapshot_insight(forged, target_platform="h5")


async def test_adaptive_action_freezes_sampling_in_original_coordinator_contract():
    from dataclasses import replace
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from services.api.app.application.h5_adaptive_journey import (
        ADAPTIVE_BINDING_KEY,
        H5AdaptiveJourneyRunner,
    )
    from services.api.app.application.h5_runtime_composition import (
        _platform_selected_h5_action_intent,
    )
    from services.api.tests.test_h5_adaptive_journey import selected_work

    work = selected_work()
    intent = _platform_selected_h5_action_intent(work)
    context = dict(
        task_id=work.task_id,
        agent_run_id="run",
        session_id=work.session_id,
        work_id=work.work_id,
        source_capture_id=work.source_capture_id,
        attempt_id="attempt",
    )
    work = replace(
        work,
        frontier=replace(
            work.frontier,
            observation=replace(
                work.frontier.observation,
                metadata={**work.frontier.observation.metadata, ADAPTIVE_BINDING_KEY: context},
            ),
        ),
    )
    checkpoint = parse_journey_definition(sampled_pause_definition()).checkpoints[1]
    repo = SimpleNamespace(
        recover_attempt=AsyncMock(return_value=None),
        get_checkpoint=AsyncMock(return_value=checkpoint),
        reserve=AsyncMock(return_value="new"),
        bind=AsyncMock(),
        fail=AsyncMock(),
    )
    turn = SimpleNamespace(coordination=object())
    executor = SimpleNamespace(
        decide=AsyncMock(return_value=intent), execute_decision=AsyncMock(return_value=turn)
    )
    runner = H5AdaptiveJourneyRunner(repository=repo, planner=None)
    assert await runner.execute(work=work, agent_run_id="run", executor=executor) is turn
    frozen = repo.reserve.await_args.kwargs["decision"]
    assert "checkpoint_observation" in frozen.capture_profile.after.artifact_types
    assert frozen.capture_profile.before == intent.capture_profile.before
    assert frozen.deadline == work.deadline
    assert frozen.parameters == intent.parameters
    assert executor.execute_decision.await_args.kwargs["decision"] == frozen
    executor.execute_decision.assert_awaited_once()
