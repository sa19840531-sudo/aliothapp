"""缺前后帧时保留不足终态，不能在判定或观测阶段抛索引异常。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.application.checkpoint_judgement import judge_checkpoint
from services.api.app.application.h5_checkpoint_observation import H5CheckpointObserver
from services.api.app.domain.journey import parse_journey_definition
from services.api.app.domain.journey.definitions import build_run_journey_execution_plan
from services.api.tests.test_h5_checkpoint_consumption import sample
from services.api.tests.test_h5_shortflow_activation import definition_payload


@pytest.mark.parametrize("count", [0, 1])
def test_missing_manifest_pair_returns_insufficient_instead_of_crashing(count):
    checkpoint = parse_journey_definition(definition_payload()).checkpoints[0]
    manifests = [{"quality_status": "pass", "artifacts": []}] * count
    verdict = judge_checkpoint(
        checkpoint,
        before=sample(paused=False)["snapshot"],
        after=sample()["snapshot"],
        manifests=manifests,
    )
    assert verdict["status"] == "evidence_insufficient"
    assert "latest_manifest_not_pass" in verdict["missing"]


@pytest.mark.parametrize("capture_count,manifest_count", [(0, 2), (1, 2), (3, 2), (2, 0), (2, 1)])
async def test_observer_rejects_incomplete_pair_before_reading_any_snapshot(
    capture_count, manifest_count
):
    plan = build_run_journey_execution_plan(
        parse_journey_definition(definition_payload()),
        task_id="task",
        agent_run_id="run",
        scenario_key="pause",
        parameters={},
    )
    reader = SimpleNamespace(read_ui_snapshot=AsyncMock(return_value=sample()["snapshot"]))
    observer = H5CheckpointObserver(
        snapshots=reader,
        plans=SimpleNamespace(get_execution_plan=AsyncMock(return_value=plan)),
    )
    assert (
        await observer.result_matches(
            plan_id=plan.execution_plan_id,
            checkpoint_id=plan.checkpoints[0].checkpoint_id,
            captures=[{"task_id": "task", "id": str(index)} for index in range(capture_count)],
            manifests=[{"quality_status": "pass", "artifacts": []}] * manifest_count,
        )
        is False
    )
    reader.read_ui_snapshot.assert_not_awaited()
