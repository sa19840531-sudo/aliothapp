"""H5 定向补采精确 Manifest 读取适配测试。"""

from dataclasses import replace

import pytest

from services.api.app.application.h5_targeted_recapture_observation import (
    H5TargetedRecaptureObservationReader,
)
from services.api.app.application.observation_packet import ObservationPacketService
from services.api.app.application.rule_judge import RuleJudgeRequest, RuleJudgeService
from services.api.tests.test_manifest_backed_observation import (
    CAPTURE_ID,
    TASK_ID,
    _context_source,
    _manifest,
    _repository,
)
from services.api.tests.test_rule_judge import FakeRuleJudgeRepository
from services.api.tests.test_rule_judge_evidence_gap import InsufficientSemanticJudge
from services.api.tests.test_rule_judge_presentation_preflight import _hybrid_definition


class _InnerReader:
    async def load_source(self, *, task_id: str, page_capture_id: str) -> tuple[str, str]:
        return task_id, page_capture_id


class _ExactManifestReader:
    async def load_source_for_manifest(
        self,
        *,
        task_id: str,
        page_capture_id: str,
        manifest_id: str,
    ) -> tuple[str, str, str]:
        return task_id, page_capture_id, manifest_id


@pytest.mark.asyncio
async def test_h5_targeted_recapture_reader_preserves_normal_read_path() -> None:
    reader = H5TargetedRecaptureObservationReader(
        inner=_InnerReader(),
        exact_manifest_reader=_ExactManifestReader(),
    )

    assert await reader.load_source(
        task_id="task-1",
        page_capture_id="capture-1",
    ) == ("task-1", "capture-1")


@pytest.mark.asyncio
async def test_h5_targeted_recapture_reader_uses_exact_manifest_path() -> None:
    reader = H5TargetedRecaptureObservationReader(
        inner=_InnerReader(),
        exact_manifest_reader=_ExactManifestReader(),
    )

    assert await reader.load_source_for_manifest(
        task_id="task-1",
        page_capture_id="capture-2",
        manifest_id="manifest-2",
    ) == ("task-1", "capture-2", "manifest-2")


@pytest.mark.asyncio
async def test_h5_manifest_composition_rebinds_followup_gap_to_new_capture() -> None:
    manifest = _manifest()
    exact, inner = _repository(
        manifest=manifest,
        context=replace(_context_source(), target_platform="h5", page_type="content_detail"),
    )
    reader = H5TargetedRecaptureObservationReader(inner=inner, exact_manifest_reader=exact)
    definition = replace(
        _hybrid_definition(category="player"),
        input_contract={"recapture_policy": "equivalent"},
    )
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        observation_packet_service=ObservationPacketService(repository=reader),
        semantic_judge=InsufficientSemanticJudge(),
    )
    result = await service.judge(RuleJudgeRequest(
        task_id=TASK_ID, rule_code=definition.rule_code, page_capture_id="original-capture",
        evidence_artifact_ids=("artifact-text", "artifact-shot"), judge_input={},
        evidence_source_page_capture_id=CAPTURE_ID, evidence_manifest_id=manifest.manifest_id,
    ))
    assert result.execution_status == "evidence_insufficient"
    assert result.page_capture_id == "original-capture"
    assert inner.saved[0]["packet_payload"]["identity"]["target_platform"] == "h5"
    assert result.judge_output["evidence_gap"]["target"]["page_capture_id"] == CAPTURE_ID
    assert result.judge_output["evidence_gap_source"]["manifest_id"] == manifest.manifest_id
