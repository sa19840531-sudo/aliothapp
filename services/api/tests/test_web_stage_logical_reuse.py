"""阶段化菜单复用真实旧评估，不把本次动作帧伪装成已分类页面。"""

from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from services.api.app.application.web_logical_target import WebLogicalTargetClassifier
from services.api.app.application.web_page_classification import WebPageAssessment
from services.api.app.application.web_runtime_stage_effects import WebRuntimeStageEffects
from services.api.app.application.web_runtime_stage_frontier import WebRuntimeStageFrontier
from services.api.app.domain.exploration import WebMenuTerminalStatus
from services.api.app.domain.exploration.web_run_stage_checkpoint import WebRunStage
from services.api.tests.test_web_page_type_assessment import _capture
from services.api.tests.test_web_runtime_stage_effects import CAPTURE, Evidence, command, cursor

ORIGINAL = "00000000-0000-0000-0000-000000000021"
LOGICAL = "web-logical:" + "a" * 64


class ReuseEvidence(Evidence):
    def __init__(self):
        super().__init__()
        self.assessment_recorded = False
        self.original_available = True

    async def read_reused_page_type_assessment(
        self, *, capture_id, finalization_epoch, reused_capture_id, reused_logical_page_id
    ):
        if (capture_id, finalization_epoch, reused_capture_id, reused_logical_page_id) != (
            CAPTURE,
            self.epoch,
            ORIGINAL,
            LOGICAL,
        ) or not self.original_available:
            return None
        return {"page_type": "collection_page"}

    async def capture_outcome(self, *, capture_id):
        return {"page_type": None, "action_execution_id": "fresh-menu-action"}


def menu_cursor(stage=WebRunStage.CLASSIFICATION):
    source = cursor(stage)
    return replace(
        source,
        logical_page_key="menu:topics",
        cursor_payload={
            **source.cursor_payload,
            "menu_ledger_id": "topics",
            "menu_navigation_capture_id": CAPTURE,
        },
    )


def runtime():
    class Targets:
        async def claim(self, **kwargs):
            return {
                "created": False,
                "state": "materialized",
                "logical_page_id": LOGICAL,
                "assessment_capture_id": ORIGINAL,
                "assessment": asdict(
                    WebPageAssessment(
                        page_type="collection_page", confidence=0.95, evidence=("原页面证据",)
                    )
                ),
            }

    class Inner:
        async def classify(self, _capture):
            raise AssertionError("同一逻辑页面不得重复模型分类或通知")

    class Reader:
        async def read(self, **kwargs):
            return replace(_capture(), capture_id=CAPTURE, url="https://example.test/topics/")

    return SimpleNamespace(
        page_type_classifier=WebLogicalTargetClassifier(
            classifier=Inner(),
            repository=Targets(),
            task_id=command().task_id,
            agent_run_id=command().agent_run_id,
            session_id=command().session_id,
        ),
        stage_ready_capture_reader=Reader(),
    )


async def test_classification_reuse_keeps_fresh_frame_and_original_assessment_marker():
    evidence = ReuseEvidence()
    effects = WebRuntimeStageEffects(runtime=runtime(), evidence=evidence, command=command())
    result = await effects.execute(menu_cursor())
    assert result.cursor_payload["capture_id"] == CAPTURE
    assert result.reused_capture_id == ORIGINAL
    assert result.reused_logical_page_id == LOGICAL
    assert not evidence.assessment_recorded
    marker = evidence.saved[menu_cursor().idempotency_key, "classification"]
    assert marker.reused_capture_id == ORIGINAL


async def test_restored_classification_rechecks_reused_material_without_current_frame():
    evidence = ReuseEvidence()
    effects = WebRuntimeStageEffects(runtime=runtime(), evidence=evidence, command=command())
    result = await effects.execute(menu_cursor())
    evidence.saved[menu_cursor().idempotency_key, None] = result
    restored = await effects.find_completed(menu_cursor())
    assert restored.reused_capture_id == ORIGINAL
    evidence.original_available = False
    with pytest.raises(Exception, match="WEB_CLASSIFICATION_REUSE_INVALID"):
        await effects.find_completed(menu_cursor())


async def test_menu_reconciliation_preserves_fresh_action_and_records_reused_source():
    evidence = ReuseEvidence()
    effects = WebRuntimeStageEffects(runtime=runtime(), evidence=evidence, command=command())
    await effects.execute(menu_cursor())

    class Ledger:
        record = None

        async def read_summary(self, **kwargs):
            return SimpleNamespace(runtime_discovered_ledger=())

        async def finalize(self, record):
            self.record = record

    ledger = Ledger()
    frontier = WebRuntimeStageFrontier(
        ledger=ledger,
        candidate_source=None,
        navigator=None,
        site=SimpleNamespace(site_id="runtime"),
        evidence=evidence,
    )
    await frontier.reconcile(command(), menu_cursor(WebRunStage.EVIDENCE_RECONCILIATION))
    assert ledger.record.status is WebMenuTerminalStatus.LOGICAL_TARGET_REUSED
    assert ledger.record.menu_ready_capture_id == CAPTURE
    assert ledger.record.menu_action_execution_id == "fresh-menu-action"
    assert ledger.record.menu_page_type == "collection_page"
    assert ledger.record.diagnostic == {
        "reused_target_stage": "menu",
        "reused_capture_id": ORIGINAL,
        "reused_logical_page_id": LOGICAL,
    }


async def test_invalid_reuse_during_recovery_settles_gap_and_advances():
    from services.api.app.application.web_run_stage_orchestrator import WebRunStageOrchestrator
    from services.api.tests.test_web_runtime_recovery import Checkpoints

    evidence = ReuseEvidence()
    effects = WebRuntimeStageEffects(runtime=runtime(), evidence=evidence, command=command())
    await effects.execute(menu_cursor())
    evidence.original_available = False
    checkpoints = Checkpoints(WebRunStage.CLASSIFICATION)
    checkpoints.cursor = type(menu_cursor()).start(
        run_id=menu_cursor().run_id,
        logical_page_key=menu_cursor().logical_page_key,
        stage=WebRunStage.CLASSIFICATION,
        cursor_payload=menu_cursor().cursor_payload,
    )
    result = await WebRunStageOrchestrator(
        checkpoints=checkpoints,
        effects=effects,
        worker_id="recovery",
    ).advance_once(checkpoints.cursor.run_id, checkpoints.cursor.logical_page_key)
    assert checkpoints.commits == ["claim", "gap"]
    assert result.cursor.gaps[-1].error_code == "WEB_CLASSIFICATION_REUSE_INVALID"
    assert result.cursor.next_stage is WebRunStage.FRONTIER_DRAIN
