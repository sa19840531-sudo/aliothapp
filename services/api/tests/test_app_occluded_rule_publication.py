"""App 先清除真实遮挡再发布业务规则，配对与广告规则仍正常判定。"""

import pytest

from services.api.app.application.app_inspection_publisher import (
    AppCaptureInspectionPublisher,
    _app_presentation_ready_for_rule,
    formal_inspection_status,
)
from services.api.app.application.rule_page_state import AppActionBeforeScene
from services.api.app.domain.evidence.presentation import (
    BasePageVisibility,
    BlockingScope,
    PresentationAssessment,
    TopmostLayerKind,
)
from services.api.tests.test_app_inspection_work_item_publish import (
    _FakeAgentRunReader,
    _FakeAnnotator,
    _FakePresentationReader,
    _FakeRule,
    _FakeSnapshotProvider,
    _FakeTaskQuery,
    _FakeWorkItemService,
    _stub_settings,  # noqa: F401 - 复用正式发布的设置 fixture
)


def _overlay(visibility=BasePageVisibility.PARTIALLY_OCCLUDED):
    return PresentationAssessment(
        topmost_layer_kind=TopmostLayerKind.OVERLAY,
        base_page_visibility=visibility,
        blocking_scope=BlockingScope.REGION,
    )


@pytest.mark.parametrize("snapshot_only", [False, True])
@pytest.mark.parametrize(
    "visibility", [BasePageVisibility.PARTIALLY_OCCLUDED, BasePageVisibility.FULLY_OCCLUDED]
)
async def test_occluded_business_rules_wait_then_publish_on_clear_capture(
    snapshot_only, visibility
):
    rules = [_FakeRule(rule_id=f"base-{n}", rule_code=f"BUSINESS-{n}") for n in range(5)]
    service = _FakeWorkItemService()
    annotator = _FakeAnnotator()
    reader = _FakePresentationReader(_overlay(visibility))

    async def read_presentation(**kwargs):
        return reader.presentation

    reader.get_presentation = read_presentation
    publisher = AppCaptureInspectionPublisher(
        task_query=_FakeTaskQuery(rules),
        snapshot_provider=_FakeSnapshotProvider(target_platform="app"),
        work_item_service=service,
        annotator=annotator,
        agent_run_reader=_FakeAgentRunReader(),
        presentation_reader=reader,
    )
    blocked = await publisher.publish_for_capture(
        task_id="task-1",
        capture_id="cap-1",
        visual_page_type="home",
        page_state="popup",
        snapshot_only=snapshot_only,
    )
    assert blocked.status == "evidence_rules_deferred"
    assert formal_inspection_status(blocked.status) == "evaluating"
    assert blocked.published_count == 0
    assert not service.commands
    assert not blocked.excluded_active_session_rules
    reader.presentation = PresentationAssessment(
        topmost_layer_kind=TopmostLayerKind.PAGE,
        base_page_visibility=BasePageVisibility.VISIBLE,
        blocking_scope=BlockingScope.NONE,
    )
    published = await publisher.publish_for_capture(
        task_id="task-1",
        capture_id="cap-2",
        visual_page_type="home",
        page_state="stable",
        snapshot_only=snapshot_only,
    )
    assert published.status == "published"
    assert all(command.page_capture_id == "cap-2" for command in service.commands)
    assert {rule.rule_code for command in service.commands for rule in command.rules} == {
        f"BUSINESS-{n}" for n in range(5)
    }


@pytest.mark.parametrize(
    "target_rule",
    [
        {"category": "ad"},
        {"judge_config": {"evidence_layer_target": "overlay"}},
        {"judge_config": {"evidence_layer_target": "any"}},
        {"check_point": "首页弹窗操作前后截图应改变"},
        {"name": "弹窗展示检查"},
    ],
)
def test_ad_and_overlay_declared_rules_are_kept(target_rule):
    assert _app_presentation_ready_for_rule(_overlay(), target_rule)


def test_unknown_presentation_does_not_hide_missing_rule_evidence():
    assert _app_presentation_ready_for_rule(None, {"category": "visual"})
    unknown = PresentationAssessment(
        topmost_layer_kind=TopmostLayerKind.UNKNOWN,
        base_page_visibility=BasePageVisibility.UNKNOWN,
        blocking_scope=BlockingScope.UNKNOWN,
    )
    assert _app_presentation_ready_for_rule(unknown, {"category": "visual"})


async def test_after_popup_preserves_explicit_before_paired_rule21():
    class BeforeReader:
        async def get_before_scene(self, **kwargs):
            return AppActionBeforeScene(capture_id="before-1", page_type="home", page_state="popup")

    service = _FakeWorkItemService()
    paired = _FakeRule(
        rule_id="paired",
        rule_code="PAIRED-21",
        category="visual",
        input_contract={
            "required_artifacts": ["screenshot"],
            "required_capture_roles": ["before", "primary"],
        },
        judge_config={
            "engine_config": {
                "before_role": "before",
                "after_role": "primary",
                "required_page_states": ["popup"],
            }
        },
    )
    publisher = AppCaptureInspectionPublisher(
        task_query=_FakeTaskQuery([paired]),
        snapshot_provider=_FakeSnapshotProvider(target_platform="app"),
        work_item_service=service,
        annotator=_FakeAnnotator(),
        agent_run_reader=_FakeAgentRunReader(),
        presentation_reader=_FakePresentationReader(_overlay(BasePageVisibility.FULLY_OCCLUDED)),
        action_scene_reader=BeforeReader(),
    )
    result = await publisher.publish_for_capture(
        task_id="task-1", capture_id="cap-1", visual_page_type="home", page_state="popup"
    )
    assert result.published_count == 1
    assert [r.rule_code for c in service.commands for r in c.rules] == ["PAIRED-21"]


def test_explicit_base_target_overrides_ad_or_popup_name():
    rule = {
        "category": "ad",
        "name": "弹窗后的列表检查",
        "judge_config": {"evidence_layer_target": "base_page"},
    }
    assert not _app_presentation_ready_for_rule(_overlay(), rule)


@pytest.mark.parametrize("state", ["popup", "auth_gate", "login_form"])
def test_rule_declaring_current_input_or_auth_surface_is_kept(state):
    rule = {
        "category": "interaction",
        "judge_config": {"engine_config": {"required_page_states": [state]}},
    }
    assert _app_presentation_ready_for_rule(
        _overlay(BasePageVisibility.FULLY_OCCLUDED), rule, page_state=state
    )
    assert not _app_presentation_ready_for_rule(_overlay(), rule, page_state="other")


def test_stable_state_requirement_does_not_override_real_overlay():
    rule = {"judge_config": {"engine_config": {"required_page_states": ["stable"]}}}
    assert not _app_presentation_ready_for_rule(_overlay(), rule, page_state="stable")


def test_normalized_explicit_base_target_overrides_popup_state_requirement():
    rule = {
        "judge_config": {
            "evidence_layer_target": " BASE_PAGE ",
            "engine_config": {"required_page_states": ["popup"]},
        }
    }
    assert not _app_presentation_ready_for_rule(_overlay(), rule, page_state="popup")
