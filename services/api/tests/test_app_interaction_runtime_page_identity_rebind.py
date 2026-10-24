"""验证 App 动作重绑定以业务页面身份为准，不以视觉页型为准。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from services.api.app.application.app_interaction_runtime import (
    APP_CANDIDATE_REVALIDATION_REQUIRED,
)
from services.api.app.domain.exploration.app_visual import (
    AppVisualAnalysis,
    AppVisualValidationError,
    NormalizedBounds,
)
from services.api.app.domain.exploration.app_visual_page_identity import (
    build_app_visual_page_facts,
)
from services.api.app.domain.interaction import (
    InteractionResultStatus,
    InteractionRiskLevel,
)
from services.api.tests.test_app_interaction_runtime import (
    BEFORE_CAPTURE_ID,
    OBSERVATION_CAPTURE_ID,
    SIGNATURE,
    _analysis,
    _before_visual_analysis,
    _candidate,
    _executor,
    _request,
)
from services.api.tests.test_app_visual_page_identity import _observation

_SOURCE_SCREENSHOT_ARTIFACT_ID = "66666666-6666-6666-6666-666666666666"
_BEFORE_SCREENSHOT_ARTIFACT_ID = "77777777-7777-7777-7777-777777777777"


def _identity_facts(
    *,
    capture_id: str,
    screenshot_artifact_id: str,
    observation: Any = None,
    image_signature: str = SIGNATURE,
    package_name: str = "com.example.target",
    activity: str = ".MainActivity",
) -> dict[str, Any]:
    return build_app_visual_page_facts(
        identity_observation=observation if observation is not None else _observation(),
        local_state_observation=None,
        capture_id=capture_id,
        screenshot_artifact_id=screenshot_artifact_id,
        image_signature=image_signature,
        package_name=package_name,
        activity=activity,
        page_state="stable",
        overlay_signature=None,
        surface_facts={},
    )


def _source_candidate(*, frontier_scope: str = "local"):
    return _candidate(
        semantic_name="打开内容",
        expected_state_hint="content_detail",
        purpose="打开内容详情",
        frontier_scope=frontier_scope,
        normalized_bounds=NormalizedBounds(0.10, 0.10, 0.30, 0.20),
    )


@pytest.mark.parametrize(
    ("source_page_type", "current_page_type"),
    (
        ("post_list", "content_list"),
        ("profile", "settings"),
    ),
)
@pytest.mark.parametrize("frontier_scope", ("local", "page"))
async def test_rebind_allows_page_type_change_when_business_identity_is_same(
    source_page_type: str,
    current_page_type: str,
    frontier_scope: str,
) -> None:
    """同一业务页面身份在新截图中页型变化时仍使用当前候选坐标。"""

    source = _source_candidate(frontier_scope=frontier_scope)
    source_facts = _identity_facts(
        capture_id=OBSERVATION_CAPTURE_ID,
        screenshot_artifact_id=_SOURCE_SCREENSHOT_ARTIFACT_ID,
    )
    current_facts = _identity_facts(
        capture_id=BEFORE_CAPTURE_ID,
        screenshot_artifact_id=_BEFORE_SCREENSHOT_ARTIFACT_ID,
    )
    assert (
        source_facts["app_page_identity"]["identity"]
        == current_facts["app_page_identity"]["identity"]
    )
    current_bounds = NormalizedBounds(0.31, 0.52, 0.61, 0.62)
    executor, driver, _ = _executor(
        candidate=source,
        analysis=_analysis(
            source,
            visual_page_type=source_page_type,
            surface_facts=source_facts,
        ),
        before_analysis=_before_visual_analysis(
            source,
            visual_page_type=current_page_type,
            normalized_bounds=current_bounds,
            surface_facts=current_facts,
        ),
    )

    result = await executor.execute(_request(source))

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert driver.execute_count == 1
    assert driver.last_intent is not None
    assert driver.last_intent.parameters["resolved_target"] == {
        "x": 497,
        "y": 1094,
        "space": "capture_px",
        "base_width": 1080,
        "base_height": 1920,
    }


@pytest.mark.parametrize("missing_side", ("source", "current"))
async def test_rebind_requires_identity_on_both_observations(missing_side: str) -> None:
    source = _source_candidate()
    source_facts = (
        {}
        if missing_side == "source"
        else _identity_facts(
            capture_id=OBSERVATION_CAPTURE_ID,
            screenshot_artifact_id=_SOURCE_SCREENSHOT_ARTIFACT_ID,
        )
    )
    current_facts = (
        {}
        if missing_side == "current"
        else _identity_facts(
            capture_id=BEFORE_CAPTURE_ID,
            screenshot_artifact_id=_BEFORE_SCREENSHOT_ARTIFACT_ID,
        )
    )
    executor, driver, _ = _executor(
        candidate=source,
        analysis=_analysis(
            source,
            visual_page_type="post_list",
            surface_facts=source_facts,
        ),
        before_analysis=_before_visual_analysis(
            source,
            visual_page_type="content_list",
            surface_facts=current_facts,
        ),
    )

    result = await executor.execute(_request(source))

    assert result.status is InteractionResultStatus.FAILED
    assert result.error_code == APP_CANDIDATE_REVALIDATION_REQUIRED
    assert result.retryable is True
    assert driver.execute_count == 0


async def test_rebind_rejects_page_type_change_when_business_identity_changes() -> None:
    source = _source_candidate()
    changed_observation = _observation()
    changed_observation["return_semantics"] = "root"
    executor, driver, _ = _executor(
        candidate=source,
        analysis=_analysis(
            source,
            visual_page_type="post_list",
            surface_facts=_identity_facts(
                capture_id=OBSERVATION_CAPTURE_ID,
                screenshot_artifact_id=_SOURCE_SCREENSHOT_ARTIFACT_ID,
            ),
        ),
        before_analysis=_before_visual_analysis(
            source,
            visual_page_type="content_list",
            surface_facts=_identity_facts(
                capture_id=BEFORE_CAPTURE_ID,
                screenshot_artifact_id=_BEFORE_SCREENSHOT_ARTIFACT_ID,
                observation=changed_observation,
            ),
        ),
    )

    result = await executor.execute(_request(source))

    assert result.status is InteractionResultStatus.FAILED
    assert result.error_code == APP_CANDIDATE_REVALIDATION_REQUIRED
    assert result.retryable is True
    assert driver.execute_count == 0


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("capture_id", "foreign-capture"),
        ("screenshot_artifact_id", "foreign-artifact"),
        ("image_signature", "sha256:" + "0" * 64),
        ("package_name", "com.other.target"),
        ("activity", ".OtherActivity"),
    ),
)
def test_tampered_identity_binding_is_rejected_before_runtime_rebind(
    field: str,
    value: str,
) -> None:
    analysis = _analysis(
        _source_candidate(),
        surface_facts=_identity_facts(
            capture_id=OBSERVATION_CAPTURE_ID,
            screenshot_artifact_id=_SOURCE_SCREENSHOT_ARTIFACT_ID,
        ),
    )
    payload = deepcopy(analysis.to_dict())
    payload["surface_facts"]["app_page_identity"][field] = value

    with pytest.raises(AppVisualValidationError, match="业务页面身份"):
        AppVisualAnalysis.from_dict(payload)


async def test_rebind_rejects_non_unique_current_candidates_with_same_identity() -> None:
    source = _source_candidate()
    current_a = _candidate(
        semantic_name=source.semantic_name,
        expected_state_hint=source.expected_state_hint,
        purpose=source.purpose,
        source_capture_id=BEFORE_CAPTURE_ID,
        normalized_bounds=NormalizedBounds(0.31, 0.52, 0.51, 0.62),
    )
    current_b = _candidate(
        semantic_name=source.semantic_name,
        expected_state_hint=source.expected_state_hint,
        purpose=source.purpose,
        source_capture_id=BEFORE_CAPTURE_ID,
        normalized_bounds=NormalizedBounds(0.61, 0.52, 0.81, 0.62),
    )
    executor, driver, _ = _executor(
        candidate=source,
        analysis=_analysis(
            source,
            visual_page_type="post_list",
            surface_facts=_identity_facts(
                capture_id=OBSERVATION_CAPTURE_ID,
                screenshot_artifact_id=_SOURCE_SCREENSHOT_ARTIFACT_ID,
            ),
        ),
        before_analysis=_before_visual_analysis(
            source,
            visual_page_type="content_list",
            candidates=(current_a, current_b),
            surface_facts=_identity_facts(
                capture_id=BEFORE_CAPTURE_ID,
                screenshot_artifact_id=_BEFORE_SCREENSHOT_ARTIFACT_ID,
            ),
        ),
    )

    result = await executor.execute(_request(source))

    assert result.status is InteractionResultStatus.FAILED
    assert result.error_code == APP_CANDIDATE_REVALIDATION_REQUIRED
    assert result.retryable is True
    assert driver.execute_count == 0


async def test_rebind_rejects_current_risk_escalation_with_same_identity() -> None:
    source = _source_candidate()
    current = _candidate(
        semantic_name=source.semantic_name,
        expected_state_hint=source.expected_state_hint,
        purpose=source.purpose,
        risk_level=InteractionRiskLevel.MEDIUM,
        source_capture_id=BEFORE_CAPTURE_ID,
        normalized_bounds=NormalizedBounds(0.31, 0.52, 0.61, 0.62),
    )
    executor, driver, _ = _executor(
        candidate=source,
        analysis=_analysis(
            source,
            visual_page_type="profile",
            surface_facts=_identity_facts(
                capture_id=OBSERVATION_CAPTURE_ID,
                screenshot_artifact_id=_SOURCE_SCREENSHOT_ARTIFACT_ID,
            ),
        ),
        before_analysis=_before_visual_analysis(
            source,
            visual_page_type="settings",
            candidates=(current,),
            surface_facts=_identity_facts(
                capture_id=BEFORE_CAPTURE_ID,
                screenshot_artifact_id=_BEFORE_SCREENSHOT_ARTIFACT_ID,
            ),
        ),
    )

    result = await executor.execute(_request(source))

    assert result.status is InteractionResultStatus.FAILED
    assert result.error_code == APP_CANDIDATE_REVALIDATION_REQUIRED
    assert result.retryable is True
    assert driver.execute_count == 0
