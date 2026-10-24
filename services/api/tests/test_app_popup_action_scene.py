"""完整弹窗操作必须按 before 场景触发，primary 仍指向真实动作后截图。"""

from types import SimpleNamespace

import pytest

from services.api.app.application import inspection_candidate_publish
from services.api.app.application.app_inspection_publisher import AppCaptureInspectionPublisher
from services.api.app.application.rule_judge import RuleJudgeDefinition, RuleJudgeService
from services.api.app.application.rule_page_state import AppActionBeforeScene
from services.api.tests.test_app_inspection_work_item_publish import (
    _FakeAgentRunReader,
    _FakeAnnotator,
    _FakeRule,
    _FakeSnapshotProvider,
    _FakeTaskQuery,
    _FakeWorkItemService,
)
from services.api.tests.test_rule_judge import (
    FakeObservationPacketService,
    FakeRuleJudgeEvidenceReader,
    FakeRuleJudgeRepository,
    _request,
)

_ENGINE = {
    "handler": "screenshot_change_assertion",
    "before_role": "before",
    "after_role": "primary",
    "required_page_states": ["popup", "modal", "overlay", "dialog"],
}
_CONTRACT = {"required_artifacts": ["screenshot"], "required_capture_roles": ["before", "primary"]}


@pytest.mark.parametrize(
    "before_page,before_state,expected",
    [
        ("home", "popup", 1),
        ("popup", "popup", 1),
        (None, "popup", 1),
        ("home", "stable", 0),
        ("home", None, 0),
    ],
)
async def test_app_publisher_triggers_complete_popup_pair_on_before_scene(
    monkeypatch,
    before_page,
    before_state,
    expected,
):
    monkeypatch.setattr(
        inspection_candidate_publish,
        "get_settings",
        lambda: SimpleNamespace(
            inspection_work_items=SimpleNamespace(max_attempts=3),
        ),
    )
    service = _FakeWorkItemService()

    class SceneReader:
        async def get_before_scene(self, **kwargs):
            assert kwargs == {"task_id": "task-1", "capture_id": "cap-1", "agent_run_id": "run-1"}
            return (
                AppActionBeforeScene(
                    capture_id="before-cap",
                    page_type=before_page,
                    page_state=before_state,
                )
                if before_state
                else None
            )

    publisher = AppCaptureInspectionPublisher(
        task_query=_FakeTaskQuery(
            [
                _FakeRule(
                    rule_id="r21",
                    rule_code="alioth_21",
                    executor_type="rule_engine",
                    input_contract=_CONTRACT,
                    judge_config={"engine_config": _ENGINE},
                )
            ]
        ),
        snapshot_provider=_FakeSnapshotProvider(target_platform="app"),
        work_item_service=service,
        annotator=_FakeAnnotator(),
        agent_run_reader=_FakeAgentRunReader(),
        action_scene_reader=SceneReader(),
    )
    result = await publisher.publish_for_capture(
        task_id="task-1",
        capture_id="cap-1",
        visual_page_type="home",
        page_state="stable",
        agent_run_id="run-1",
        active_session_only=True,
    )
    assert result.published_count == expected
    if expected:
        assert service.commands[0].page_capture_id == "cap-1"
        assert service.commands[0].rules[0].rule_code == "alioth_21"


class _PairedPacket(FakeObservationPacketService):
    def __init__(self, *, before_state="popup", before_present=True):
        self._before_state = before_state
        self._before_present = before_present

    async def build(self, request):
        packet = await super().build(request)
        packet.packet_payload["page"]["page_state"] = "stable"
        if self._before_present:
            packet.packet_payload["facts"]["capture_roles"] = {
                "before": {
                    "page_capture_id": "before-cap",
                    "page_type": "home",
                    "page_state": self._before_state,
                    "quality_status": "pass",
                },
                "primary": {
                    "page_capture_id": "capture-1",
                    "page_type": "home",
                    "page_state": "stable",
                    "quality_status": "pass",
                },
            }
        return packet


@pytest.mark.parametrize("batch", [False, True])
async def test_rule_judge_compares_popup_before_and_closed_primary_in_both_entries(batch):
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(
            RuleJudgeDefinition(
                rule_id="r21",
                rule_code="alioth_21",
                executor_type="rule_engine",
                input_contract=_CONTRACT,
                judge_config={"engine_config": _ENGINE},
            )
        ),
        observation_packet_service=_PairedPacket(),
        evidence_reader=FakeRuleJudgeEvidenceReader(
            "",
            role_artifacts={
                "before": {"screenshot": b"before-popup"},
                "primary": {"screenshot": b"after-closed"},
            },
        ),
    )
    request = _request(
        rule_code="alioth_21",
        judge_input={},
        evidence_capture_roles={"before": "before-cap", "primary": "capture-1"},
    )
    result = (await service.judge_many([request]))[0] if batch else await service.judge(request)
    assert result.execution_status == "pass"
    assert result.judge_output.get("applicability") != "not_applicable"


async def test_missing_before_frame_is_evidence_insufficient_instead_of_false_not_applicable():
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(
            RuleJudgeDefinition(
                rule_id="r21",
                rule_code="alioth_21",
                executor_type="rule_engine",
                input_contract=_CONTRACT,
                judge_config={"engine_config": _ENGINE},
            )
        ),
        observation_packet_service=_PairedPacket(before_present=False),
        evidence_reader=FakeRuleJudgeEvidenceReader(
            "",
            role_artifacts={
                "primary": {"screenshot": b"after-closed"},
            },
        ),
    )
    result = await service.judge(_request(rule_code="alioth_21", judge_input={}))
    assert result.execution_status == "evidence_insufficient"
    assert result.judge_output.get("applicability") != "not_applicable"


@pytest.mark.parametrize(
    "page_type,before_page,rule_pages,expected",
    [
        ("startup_ad", None, ["startup_ad", "home"], 0),
        ("home", None, ["startup_ad", "home"], 0),
        ("home", "startup_ad", ["startup_ad"], 1),
        ("home", "startup_ad", ["startup_ad", "home"], 1),
        ("home", "age_gate", ["startup_ad", "home"], 0),
        ("home", None, ["startup_ad"], 0),
    ],
)
async def test_startup_skip_waits_for_result_and_preserves_real_home_anchor(
    monkeypatch,
    page_type,
    before_page,
    rule_pages,
    expected,
):
    monkeypatch.setattr(
        inspection_candidate_publish,
        "get_settings",
        lambda: SimpleNamespace(
            inspection_work_items=SimpleNamespace(max_attempts=3),
        ),
    )
    service = _FakeWorkItemService()

    class SceneReader:
        async def get_before_scene(self, **kwargs):
            return (
                AppActionBeforeScene(
                    capture_id="before-startup",
                    page_type=before_page,
                    page_state="stable",
                )
                if before_page
                else None
            )

    publisher = AppCaptureInspectionPublisher(
        task_query=_FakeTaskQuery(
            [
                _FakeRule(
                    rule_id="r24",
                    rule_code="alioth_24",
                    executor_type="llm",
                    applicable_page_types=rule_pages,
                    judge_config={"capability_config": {"batch_group": "startup-ad-skip"}},
                )
            ]
        ),
        snapshot_provider=_FakeSnapshotProvider(target_platform="app"),
        work_item_service=service,
        annotator=_FakeAnnotator(),
        agent_run_reader=_FakeAgentRunReader(),
        action_scene_reader=SceneReader(),
    )
    result = await publisher.publish_for_capture(
        task_id="task-1",
        capture_id="cap-1",
        visual_page_type=page_type,
        page_state="stable",
        agent_run_id="run-1",
    )
    assert result.published_count == expected
    if expected:
        assert service.commands[0].page_capture_id == "cap-1"
        assert [rule.rule_code for rule in service.commands[0].rules] == ["alioth_24"]
