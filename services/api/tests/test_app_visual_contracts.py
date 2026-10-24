import math

import pytest

from services.api.app.domain.exploration import (
    APP_VISUAL_ANALYSIS_VERSION,
    AppVisualAnalysis,
    AppVisualCandidate,
    AppVisualValidationError,
    NormalizedBounds,
)
from services.api.app.domain.interaction import (
    InteractionOperation,
    InteractionRiskLevel,
)
from services.api.app.domain.protocol import stable_protocol_hash

_SIGNATURE = "sha256:" + "a" * 64


def _candidate(
    *,
    semantic_name: str = "登录按钮",
    action_type: InteractionOperation = InteractionOperation.CLICK,
    bounds: NormalizedBounds | None = None,
    confidence: float = 0.95,
    source_capture_id: str = "capture-1",
    control_role: str | None = None,
) -> AppVisualCandidate:
    return AppVisualCandidate.build(
        source_capture_id=source_capture_id,
        source_image_signature=_SIGNATURE,
        source_width=1080,
        source_height=1920,
        source_orientation="portrait",
        semantic_name=semantic_name,
        action_type=action_type,
        normalized_bounds=bounds or NormalizedBounds(0.2, 0.7, 0.8, 0.8),
        confidence=confidence,
        risk_level=InteractionRiskLevel.LOW,
        purpose="进入系统",
        control_role=control_role,
        expected_state_hint="首页",
    )


def _analysis(*, candidates: tuple[AppVisualCandidate, ...] | None = None) -> AppVisualAnalysis:
    return AppVisualAnalysis.build(
        capture_id="capture-1",
        screenshot_artifact_id="artifact-1",
        image_signature=_SIGNATURE,
        width=1080,
        height=1920,
        orientation="portrait",
        visual_page_type="login",
        visual_summary="登录页",
        key_elements=("账号输入框", "登录按钮"),
        candidates=candidates or (_candidate(),),
        provider_id="fake-app-vision",
        model_id="deterministic-v1",
    )


def _stateful_analysis(
    *,
    page_state: str = "popup",
    overlay_signature: str | None = "overlay:dialog-1",
    surface_facts: dict[str, object] | None = None,
) -> AppVisualAnalysis:
    """构造带阶段机事实的视觉分析结果。"""
    return AppVisualAnalysis.build(
        capture_id="capture-1",
        screenshot_artifact_id="artifact-1",
        image_signature=_SIGNATURE,
        width=1080,
        height=1920,
        orientation="portrait",
        visual_page_type="home",
        visual_summary="首页被弹窗遮挡",
        key_elements=("关闭按钮", "首页内容"),
        candidates=(_candidate(semantic_name="关闭弹窗"),),
        provider_id="fake-app-vision",
        model_id="deterministic-v1",
        page_state=page_state,
        overlay_signature=overlay_signature,
        surface_facts=surface_facts or {"popup_visible": True, "layer": "dialog"},
    )


def test_app_visual_analysis_round_trip_ignores_future_fields() -> None:
    analysis = _analysis()
    payload = analysis.to_dict()
    payload["future_field"] = {"ignored": True}
    payload["candidates"][0]["future_candidate_field"] = "ignored"

    restored = AppVisualAnalysis.from_dict(payload)

    assert restored == analysis
    assert restored.contract_version == APP_VISUAL_ANALYSIS_VERSION
    assert restored.candidates[0].candidate_id.startswith("candidate:")


@pytest.mark.parametrize(
    "page_state",
    ("stable", "unknown", "startup_ad", "popup", "auth_gate", "system_dialog"),
)
def test_app_visual_analysis_preserves_surface_phase_facts(page_state: str) -> None:
    analysis = _stateful_analysis(page_state=page_state)

    restored = AppVisualAnalysis.from_dict(analysis.to_dict())

    assert restored.page_state == page_state
    assert restored.overlay_signature == "overlay:dialog-1"
    assert restored.surface_facts == {"popup_visible": True, "layer": "dialog"}


def test_app_visual_analysis_v1_payload_defaults_unknown_surface_state() -> None:
    payload = _analysis().to_dict()
    payload.pop("page_state", None)
    payload.pop("overlay_signature", None)
    payload.pop("surface_facts", None)
    payload["contract_version"] = "app-visual-analysis.v1"
    payload["result_hash"] = stable_protocol_hash(
        {
            key: value
            for key, value in payload.items()
            if key not in {"contract_version", "result_hash"}
        }
    )

    restored = AppVisualAnalysis.from_dict(payload)

    assert restored.page_state == "unknown"
    assert restored.overlay_signature is None
    assert restored.surface_facts == {}


def test_app_visual_analysis_rejects_unhashed_surface_facts_in_v1_payload() -> None:
    payload = _analysis().to_dict()
    payload.pop("page_state", None)
    payload.pop("overlay_signature", None)
    payload.pop("surface_facts", None)
    payload["contract_version"] = "app-visual-analysis.v1"
    payload["result_hash"] = stable_protocol_hash(
        {
            key: value
            for key, value in payload.items()
            if key not in {"contract_version", "result_hash"}
        }
    )
    payload["page_state"] = "popup"

    with pytest.raises(AppVisualValidationError, match="v1"):
        AppVisualAnalysis.from_dict(payload)


def test_app_visual_analysis_rejects_unknown_state_and_non_json_surface_facts() -> None:
    with pytest.raises(AppVisualValidationError, match="page_state"):
        _stateful_analysis(page_state="not-a-stable-surface-state")

    with pytest.raises(AppVisualValidationError, match="surface_facts"):
        _stateful_analysis(surface_facts=["not", "an", "object"])


def test_app_visual_analysis_hash_covers_surface_phase_facts() -> None:
    payload = _stateful_analysis().to_dict()
    payload["page_state"] = "stable"

    with pytest.raises(AppVisualValidationError, match="result_hash"):
        AppVisualAnalysis.from_dict(payload)


def test_candidate_identity_is_derived_from_visual_facts() -> None:
    first = _candidate()
    same = _candidate()
    changed = _candidate(semantic_name="注册按钮")

    assert first.candidate_id == same.candidate_id
    assert first.candidate_id != changed.candidate_id


def test_v3_control_role_round_trip_and_hash_tamper_detection() -> None:
    plain = _candidate()
    dismiss = _candidate(control_role="dismiss")
    analysis_plain = _analysis(candidates=(plain,))
    analysis_dismiss = _analysis(candidates=(dismiss,))

    assert plain.control_role is None
    assert plain.to_dict()["control_role"] is None
    assert AppVisualCandidate.from_dict(dismiss.to_dict()) == dismiss
    assert dismiss.to_dict()["control_role"] == "dismiss"
    assert plain.candidate_id != dismiss.candidate_id
    assert analysis_plain.result_hash != analysis_dismiss.result_hash

    tampered = analysis_dismiss.to_dict()
    tampered["candidates"][0]["control_role"] = "skip"
    with pytest.raises(AppVisualValidationError, match="candidate_id|result_hash"):
        AppVisualAnalysis.from_dict(tampered)


def test_candidate_frontier_scope_round_trip_and_v1_compatibility() -> None:
    candidate = AppVisualCandidate.build(
        source_capture_id="capture-1",
        source_image_signature=_SIGNATURE,
        source_width=1080,
        source_height=1920,
        source_orientation="portrait",
        semantic_name="消息 Tab",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(0.2, 0.7, 0.8, 0.8),
        confidence=0.95,
        risk_level=InteractionRiskLevel.LOW,
        purpose="进入消息页",
        frontier_scope="app",
    )

    assert AppVisualCandidate.from_dict(candidate.to_dict()) == candidate
    assert candidate.to_dict()["frontier_scope"] == "app"

    legacy = _candidate()
    legacy_payload = legacy.to_dict()
    legacy_payload["contract_version"] = "app-visual-candidate.v1"
    legacy_payload.pop("frontier_scope", None)
    legacy_payload.pop("control_role", None)
    legacy_payload["candidate_id"] = "candidate:" + stable_protocol_hash(
        {
            "source_capture_id": legacy.source_capture_id,
            "source_image_signature": legacy.source_image_signature,
            "semantic_name": legacy.semantic_name,
            "action_type": legacy.action_type.value,
            "normalized_bounds": legacy.normalized_bounds.to_dict(),
        }
    )
    restored = AppVisualCandidate.from_dict(legacy_payload)
    assert restored.frontier_scope == "local"
    assert restored.control_role is None
    assert "frontier_scope" not in restored.to_dict()
    assert "control_role" not in restored.to_dict()


def test_v2_candidate_round_trip_preserves_historical_identity() -> None:
    legacy_payload = _candidate().to_dict()
    legacy_payload["contract_version"] = "app-visual-candidate.v2"
    legacy_payload.pop("control_role", None)
    legacy_payload["candidate_id"] = "candidate:" + stable_protocol_hash(
        {
            "source_capture_id": legacy_payload["source_capture_id"],
            "source_image_signature": legacy_payload["source_image_signature"],
            "semantic_name": legacy_payload["semantic_name"],
            "action_type": legacy_payload["action_type"],
            "normalized_bounds": legacy_payload["normalized_bounds"],
            "frontier_scope": legacy_payload["frontier_scope"],
        }
    )

    restored = AppVisualCandidate.from_dict(legacy_payload)

    assert restored.control_role is None
    assert restored.to_dict() == legacy_payload


@pytest.mark.parametrize("contract_version", ("app-visual-candidate.v1", "app-visual-candidate.v2"))
@pytest.mark.parametrize("control_role", ("dismiss", None))
def test_legacy_candidate_rejects_new_control_role_field(
    contract_version: str,
    control_role: str | None,
) -> None:
    payload = _candidate().to_dict()
    payload["contract_version"] = contract_version
    payload["control_role"] = control_role
    if contract_version == "app-visual-candidate.v1":
        payload.pop("frontier_scope", None)

    with pytest.raises(AppVisualValidationError, match="旧版"):
        AppVisualCandidate.from_dict(payload)


@pytest.mark.parametrize("control_role", ("details", "关闭", "", False))
def test_candidate_rejects_noncanonical_control_role(control_role: object) -> None:
    with pytest.raises(AppVisualValidationError, match="control_role"):
        _candidate(control_role=control_role)  # type: ignore[arg-type]


def test_v1_candidate_rejects_task_level_frontier_scope() -> None:
    payload = _candidate().to_dict()
    payload["contract_version"] = "app-visual-candidate.v1"
    payload.pop("control_role", None)
    payload["frontier_scope"] = "app"

    with pytest.raises(AppVisualValidationError, match="v1"):
        AppVisualCandidate.from_dict(payload)


@pytest.mark.parametrize(
    "values",
    [
        (-0.1, 0.1, 0.5, 0.5),
        (0.1, 0.1, 1.1, 0.5),
        (0.5, 0.1, 0.5, 0.5),
        (0.6, 0.1, 0.5, 0.5),
        (0.1, 0.5, 0.5, 0.5),
        (0.1, 0.6, 0.5, 0.5),
        (math.nan, 0.1, 0.5, 0.5),
    ],
)
def test_normalized_bounds_reject_invalid_geometry(
    values: tuple[float, float, float, float],
) -> None:
    with pytest.raises(AppVisualValidationError, match="normalized_bounds"):
        NormalizedBounds(*values)


@pytest.mark.parametrize("confidence", [-0.01, 1.01, math.nan, math.inf])
def test_candidate_rejects_invalid_confidence(confidence: float) -> None:
    with pytest.raises(AppVisualValidationError, match="confidence"):
        _candidate(confidence=confidence)


def test_candidate_action_allowlist_and_click_bounds_are_enforced() -> None:
    with pytest.raises(AppVisualValidationError, match="不允许"):
        _candidate(action_type=InteractionOperation.INPUT)

    with pytest.raises(AppVisualValidationError, match="normalized_bounds"):
        AppVisualCandidate.build(
            source_capture_id="capture-1",
            source_image_signature=_SIGNATURE,
            source_width=1080,
            source_height=1920,
            source_orientation="portrait",
            semantic_name="登录按钮",
            action_type=InteractionOperation.CLICK,
            normalized_bounds=None,
            confidence=0.95,
            risk_level=InteractionRiskLevel.LOW,
            purpose="进入系统",
        )


def test_analysis_rejects_candidates_from_another_capture() -> None:
    with pytest.raises(AppVisualValidationError, match="Capture"):
        _analysis(candidates=(_candidate(source_capture_id="capture-2"),))


def test_analysis_rejects_duplicate_candidate_identity() -> None:
    candidate = _candidate()
    with pytest.raises(AppVisualValidationError, match="重复"):
        _analysis(candidates=(candidate, candidate))
