"""App 视觉识别到页面类型评估合同的适配测试。"""

from datetime import UTC, datetime

from services.api.app.application.app_page_type_assessment_adapter import (
    adapt_app_visual_analysis_to_assessment,
)
from services.api.app.domain.exploration import AppVisualAnalysis

_NOW = datetime(2026, 8, 13, 15, 0, tzinfo=UTC)
_SIGNATURE = "sha256:" + "a" * 64


def _analysis(
    *,
    visual_page_type: str | None = "home",
    page_state: str = "stable",
    surface_facts: dict[str, object] | None = None,
) -> AppVisualAnalysis:
    return AppVisualAnalysis.build(
        capture_id="capture-app-home",
        screenshot_artifact_id="artifact-screenshot",
        image_signature=_SIGNATURE,
        width=1080,
        height=1920,
        orientation="portrait",
        visual_page_type=visual_page_type,
        visual_summary="App 首页",
        key_elements=("底部导航", "推荐列表"),
        candidates=(),
        provider_id="fake-app-vision",
        model_id="deterministic-v1",
        package_name="com.example.app",
        activity="MainActivity",
        page_state=page_state,
        surface_facts=surface_facts or {},
    )


def test_recognized_app_business_page_builds_covered_assessment_payload() -> None:
    result = adapt_app_visual_analysis_to_assessment(
        task_id="task-1",
        agent_run_id="run-1",
        analysis=_analysis(
            surface_facts={
                "visual_page_type_confidence": 0.94,
                "evidence_artifact_ids": ["artifact-ui-tree"],
            },
        ),
        assessed_at=_NOW,
    )

    assert result.execution_status == "accepted"
    assert result.page_type_assessment == {
        "page_type": "home",
        "status": "covered",
        "reason_code": "app_visual_page_type_covered",
        "page_capture_ids": ["capture-app-home"],
        "evidence_artifact_ids": ["artifact-screenshot", "artifact-ui-tree"],
        "exploration_action_ids": [],
        "page_states": ["stable"],
    }


def test_low_confidence_app_business_page_is_evidence_insufficient() -> None:
    result = adapt_app_visual_analysis_to_assessment(
        task_id="task-1",
        agent_run_id="run-1",
        analysis=_analysis(surface_facts={"page_type_confidence": 0.45}),
        assessed_at=_NOW,
    )

    assert result.execution_status == "accepted"
    assert result.page_type_assessment is not None
    assert result.page_type_assessment["status"] == "evidence_insufficient"
    assert result.page_type_assessment["reason_code"] == (
        "app_visual_page_type_low_confidence"
    )


def test_unstable_app_page_state_is_evidence_insufficient() -> None:
    result = adapt_app_visual_analysis_to_assessment(
        task_id="task-1",
        agent_run_id="run-1",
        analysis=_analysis(page_state="popup"),
        assessed_at=_NOW,
    )

    assert result.execution_status == "accepted"
    assert result.page_type_assessment is not None
    assert result.page_type_assessment["status"] == "evidence_insufficient"
    assert result.page_type_assessment["reason_code"] == "app_visual_page_state_unstable"


def test_app_auth_required_state_is_requires_auth() -> None:
    result = adapt_app_visual_analysis_to_assessment(
        task_id="task-1",
        agent_run_id="run-1",
        analysis=_analysis(visual_page_type="profile", page_state="login_required"),
        assessed_at=_NOW,
    )

    assert result.execution_status == "accepted"
    assert result.page_type_assessment is not None
    assert result.page_type_assessment["status"] == "requires_auth"
    assert result.page_type_assessment["reason_code"] == "app_visual_requires_auth"


def test_app_system_dialog_state_is_blocked_by_safety() -> None:
    result = adapt_app_visual_analysis_to_assessment(
        task_id="task-1",
        agent_run_id="run-1",
        analysis=_analysis(page_state="system_dialog"),
        assessed_at=_NOW,
    )

    assert result.execution_status == "accepted"
    assert result.page_type_assessment is not None
    assert result.page_type_assessment["status"] == "blocked_by_safety"
    assert result.page_type_assessment["reason_code"] == "app_visual_blocked_by_safety"


def test_app_lifecycle_page_type_is_skipped_without_business_assessment() -> None:
    result = adapt_app_visual_analysis_to_assessment(
        task_id="task-1",
        agent_run_id="run-1",
        analysis=_analysis(visual_page_type="startup_ad", page_state="countdown"),
        assessed_at=_NOW,
    )

    assert result.execution_status == "skipped"
    assert result.page_type_assessment is None


def test_app_unknown_page_type_fails_closed() -> None:
    result = adapt_app_visual_analysis_to_assessment(
        task_id="task-1",
        agent_run_id="run-1",
        analysis=_analysis(visual_page_type="native_unknown"),
        assessed_at=_NOW,
    )

    assert result.execution_status == "blocked"
    assert result.error_code == "APP_PAGE_TYPE_ASSESSMENT_UNKNOWN_PAGE_TYPE"
    assert result.retryable is True


def test_app_missing_page_type_fails_closed() -> None:
    result = adapt_app_visual_analysis_to_assessment(
        task_id="task-1",
        agent_run_id="run-1",
        analysis=_analysis(visual_page_type=None),
        assessed_at=_NOW,
    )

    assert result.execution_status == "blocked"
    assert result.error_code == "APP_PAGE_TYPE_ASSESSMENT_PAGE_TYPE_REQUIRED"
    assert result.retryable is True
