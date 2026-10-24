"""目标包外底栏不得污染任务流程分母及恢复后的到达证据。"""

import json

from services.api.app.application.app_screenshot_exploration import _next_coverage_metadata
from services.api.app.domain.exploration.app_goal import AppExplorationGoal
from services.api.app.domain.exploration.app_visual import AppVisualAnalysis

TARGET = "com.example.target"
EXTERNAL = "com.example.external"


def _analysis(package: str, *labels: str, controls_package: str | None = None, navigation=None):
    owner = controls_package or package
    return AppVisualAnalysis.build(
        capture_id=f"capture-{package}",
        screenshot_artifact_id=f"artifact-{package}",
        image_signature="sha256:" + "a" * 64,
        width=1080,
        height=1920,
        orientation="portrait",
        package_name=package,
        activity=".MainActivity",
        visual_page_type="home",
        visual_summary="当前页面",
        key_elements=(),
        candidates=(),
        provider_id="test",
        model_id="test",
        page_state="stable",
        surface_facts={
            "trusted_accessibility_controls": {
                "package_name": owner,
                "bottom_navigation": navigation
                if navigation is not None
                else [
                    {
                        "label": label,
                        "package_name": owner,
                        "action_allowed": True,
                        "selected": label == "首页",
                    }
                    for label in labels
                ],
            }
        },
    )


def _next(analysis, prior=None):
    return _next_coverage_metadata(
        goal=AppExplorationGoal(
            required_page_types=("home",),
            required_milestones=("surface:stable",),
            minimum_business_pages=1,
            allowed_packages=(TARGET,),
        ),
        analysis=analysis,
        prior_metadata=prior,
    )


def test_external_navigation_never_enters_task_bottom_tab_denominator():
    metadata = _next(_analysis(EXTERNAL, "外部入口一", "外部入口二"))

    assert metadata["app_flow_contract"]["required_bottom_tabs"] == []
    assert metadata["app_flow_contract"]["bottom_tabs_satisfied"] is True


def test_restored_task_discards_unproven_old_bottom_tabs_without_label_blacklist():
    prior = {
        "visited_bottom_navigation_labels": ["历史未知入口"],
        "app_flow_contract": {
            "protocol_version": "app-exploration-flow-contract.v2",
            "required": True,
            "observed_bottom_tabs": ["历史未知入口"],
            "required_bottom_tabs": ["历史未知入口"],
            "visited_bottom_tabs": ["历史未知入口"],
        },
    }
    restored = _next(_analysis(TARGET, "实际业务入口"), prior)

    assert restored["app_flow_contract"]["required_bottom_tabs"] == ["实际业务入口"]
    assert restored["app_flow_contract"]["visited_bottom_tabs"] == []
    assert restored["visited_bottom_navigation_labels"] == []


def test_target_frame_rejects_navigation_from_a_different_package():
    metadata = _next(_analysis(TARGET, "不属于当前窗口的入口", controls_package=EXTERNAL))

    assert metadata["app_flow_contract"]["required_bottom_tabs"] == []


def test_external_frame_preserves_only_previously_proven_target_navigation():
    first = _next(_analysis(TARGET, "业务栏目一", "业务栏目二"))
    external = _next(_analysis(EXTERNAL, "外部入口"), first)
    restored = _next(_analysis(TARGET, "业务栏目三"), external)

    assert external["app_flow_contract"]["required_bottom_tabs"] == ["业务栏目一", "业务栏目二"]
    assert restored["app_flow_contract"]["required_bottom_tabs"] == [
        "业务栏目一",
        "业务栏目二",
        "业务栏目三",
    ]


def test_restored_json_contract_keeps_target_visits_without_external_visits():
    first = _next(_analysis(TARGET, "首页", "内容"))
    external = _next(_analysis(EXTERNAL, "外部入口"), json.loads(json.dumps(first)))
    restored = _next(_analysis(TARGET, "内容"), json.loads(json.dumps(external)))

    assert first["app_flow_contract"]["visited_bottom_tabs"] == ["首页"]
    assert restored["app_flow_contract"]["visited_bottom_tabs"] == ["首页"]
    assert restored["app_flow_contract"]["missing_bottom_tabs"] == ["内容"]


def test_target_navigation_is_accepted_by_package_provenance_not_label_blacklist():
    metadata = _next(_analysis(TARGET, "Phone", "Browser"))

    assert metadata["app_flow_contract"]["required_bottom_tabs"] == ["Phone", "Browser"]


def test_current_target_controls_reject_individual_external_navigation_items():
    navigation = [
        {"label": "业务入口", "package_name": TARGET, "action_allowed": True},
        {"label": "外部控件", "package_name": EXTERNAL, "action_allowed": True},
    ]
    metadata = _next(_analysis(TARGET, navigation=navigation))

    assert metadata["app_flow_contract"]["required_bottom_tabs"] == ["业务入口"]
