"""H5 已验证的隐藏弹层不能被误判为页面跳转失败。"""

from dataclasses import replace

import pytest

from services.api.app.application.capture_coordinator import ExecuteInteractionRequest
from services.api.app.domain.interaction import InteractionResultStatus
from services.api.app.infrastructure.browser.capture_runtime import (
    BrowserInteractionExecutor,
    InMemoryActionEvidenceBuffer,
    _annotate_trusted_popup_clearance,
    _annotate_trusted_startup_ad_layer_clearance,
    _logical_navigation_postcondition_error,
)
from services.api.app.infrastructure.browser.page_capture import (
    POPUP_SKIP_WRAP_DISMISSAL_SELECTOR,
    LogicalTargetRuntimeBinding,
    PageCaptureResult,
    _complete_action_trace,
)
from services.api.app.infrastructure.browser.semantic_target_resolver import (
    ResolvedInteractionTarget,
)
from services.api.tests.test_web_capture_runtime import (
    RecordingResolver,
    _intent,
    _logical_target_contract,
    _page_result,
)


@pytest.mark.parametrize("same_document,verified,signature_matches,h5,accepted", [
    (True, True, True, True, True),
    (False, True, True, True, False),
    (True, False, True, True, False),
    (True, True, False, True, False),
    (True, True, True, False, False),
])
@pytest.mark.parametrize("mode", ["popup_dismissal", "startup_ad_dismissal"])
def test_h5_close_effect_does_not_require_or_forge_business_identity_transition(
    same_document, verified, signature_matches, h5, accepted, mode,
):
    binding = LogicalTargetRuntimeBinding(
        source_identity_fingerprint="sha256:" + "1" * 64,
        source_state_fingerprint="sha256:" + "2" * 64,
        target_signature="sha256:" + "3" * 64,
        target_source="dom", allow_dynamic_state_revalidation=True,
    )
    transition = {
        "protocol_version": "logical_surface_transition.v1",
        "logical_transition_proof": "uncertain", "transition_kind": "unverified",
        "before_identity_fingerprint": binding.source_identity_fingerprint,
        "after_identity_fingerprint": "sha256:" + "4" * 64,
    }
    entry = {
        "selector": POPUP_SKIP_WRAP_DISMISSAL_SELECTOR,
        "dismissal_verified": verified, "popup_dismissal_outcome": "original_layer_absent",
        "logical_target": {
            "target_trust": "trusted", "source_state_fingerprint": binding.source_state_fingerprint,
            "target_signature": binding.target_signature if signature_matches else "invalid",
        },
    }
    _complete_action_trace(
        [entry], before_url="https://fixture.test/", after_url="https://fixture.test/",
        before_page_urls=["https://fixture.test/"], after_page_urls=["https://fixture.test/"],
        before_snapshot={"document": {"time_origin_ms": 1}, "logical_surface": {}},
        after_snapshot={"document": {"time_origin_ms": 1 if same_document else 2},
                        "logical_surface": {}},
        logical_surface_transition=transition, h5_session=h5,
    )
    result = PageCaptureResult(
        execution_status="success", mode="click", reachable=True,
        final_url="https://fixture.test/", title="首页", status_code=200, action_trace=[entry],
    )
    parameters = {
        "target_mode": mode, "popup_dismissal_capability": "rendered_modal_close",
        "logical_target": {"expects_logical_navigation": True},
    }
    if mode == "popup_dismissal":
        _annotate_trusted_popup_clearance(
            result, parameters=parameters, runtime_binding=binding,
            session_id="h5:fixture" if h5 else "web:fixture",
        )
    else:
        _annotate_trusted_startup_ad_layer_clearance(
            result, parameters=parameters, runtime_binding=binding if h5 else None,
        )
    assert (_logical_navigation_postcondition_error(
        parameters=parameters, state_diff=entry["state_diff"],
    ) is None) is accepted
    assert entry["state_diff"]["logical_surface_transition"] == transition


@pytest.mark.parametrize("fault", [
    None, "reload", "url", "new_tab", "missing_origin", "bool_origin", "nonfinite_origin",
])
def test_h5_local_clearance_document_scope_rejects_cross_document_or_window_facts(fault):
    origin = None if fault == "missing_origin" else True if fault == "bool_origin" else (
        float("inf") if fault == "nonfinite_origin" else 1
    )
    entry = {"selector": "#close"}
    _complete_action_trace(
        [entry], before_url="https://fixture.test/",
        after_url="https://fixture.test/other" if fault == "url" else "https://fixture.test/",
        before_page_urls=["https://fixture.test/"],
        after_page_urls=["https://fixture.test/", "about:blank"] if fault == "new_tab" else ["https://fixture.test/"],
        before_snapshot={"document": {"time_origin_ms": origin}, "logical_surface": {}},
        after_snapshot={"document": {"time_origin_ms": 2 if fault == "reload" else origin},
                        "logical_surface": {}}, h5_session=True,
    )
    assert entry["state_diff"]["h5_obstruction_document_scope"]["same_document"] is (fault is None)


@pytest.mark.parametrize(
    "verified,selector_matches,next_layer,dynamic,signature_matches,state_changed,h5,trusted,advanced",
    [
        (True, True, True, True, True, True, True, True, True),
        (False, True, True, True, True, True, True, True, False),
        (True, False, True, True, True, True, True, True, False),
        (True, True, False, True, True, True, True, True, False),
        (True, True, True, False, True, True, True, True, False),
        (True, True, True, True, False, True, True, True, False),
        (True, True, True, True, True, False, True, True, False),
        (True, True, True, True, True, True, True, False, False),
        (True, True, True, True, True, True, False, True, False),
    ],
)
@pytest.mark.parametrize("last_result", [
    "popup_dismissal_target_not_proven", "popup_dismissal_not_observed",
])
@pytest.mark.parametrize("local_scope", [False, True])
def test_h5_chain_preserves_verified_previous_layer_without_claiming_clearance(
    verified, selector_matches, next_layer, dynamic, signature_matches,
    state_changed, h5, trusted, advanced, last_result, local_scope,
):
    binding = LogicalTargetRuntimeBinding(
        source_identity_fingerprint="sha256:" + "1" * 64,
        source_state_fingerprint="sha256:" + "2" * 64,
        target_signature="sha256:" + "3" * 64,
        target_source="dom",
        allow_dynamic_state_revalidation=dynamic,
    )
    selector = "internal:rendered_modal_close:h5-obstruction:fixture"
    previous = {
        "action": "click", "requested_selector": selector if selector_matches else "other",
        "dismissal_verified": verified,
        "popup_dismissal_outcome": "next_popup_layer_visible" if next_layer else "unknown",
        "obstruction_chain_index": 1,
    }
    primary = {
        "action": "click", "selector": "#native-close", "requested_selector": selector,
        "result": last_result,
        "logical_target": {
            "target_trust": "trusted",
            "target_signature": binding.target_signature if signature_matches else "invalid",
            "source_state_fingerprint": binding.source_state_fingerprint,
        },
        "state_diff": {"logical_surface_transition": {
            "protocol_version": "logical_surface_transition.v1",
            "logical_transition_proof": "trusted", "transition_kind": "surface_state_changed",
            "corroborating_signals": ["surface_state"],
            "before_identity_fingerprint": binding.source_identity_fingerprint,
            "after_identity_fingerprint": binding.source_identity_fingerprint,
            "before_state_fingerprint": binding.source_state_fingerprint,
            "after_state_fingerprint": (
                "sha256:" + "4" * 64 if state_changed else binding.source_state_fingerprint
            ),
        }},
    }
    if local_scope:
        primary["state_diff"]["h5_obstruction_document_scope"] = {
            "schema_version": "h5-obstruction-document-scope.v1", "same_document": True,
        }
        primary["state_diff"]["logical_surface_transition"].update({
            "logical_transition_proof": "uncertain", "transition_kind": "unverified",
            "after_identity_fingerprint": "sha256:" + "5" * 64,
        })
        advanced = all((
            verified, selector_matches, next_layer, dynamic, signature_matches, h5, trusted,
        ))
    result = PageCaptureResult(
        execution_status="success", mode="click", reachable=True,
        final_url="https://fixture.test/", title="首页", status_code=200,
        action_trace=[previous, primary],
    )
    parameters = {
        "target_mode": "popup_dismissal",
        "popup_dismissal_capability": "rendered_modal_close" if trusted else "other",
        "logical_target": {"expects_logical_navigation": True},
    }
    _annotate_trusted_popup_clearance(
        result, parameters=parameters, runtime_binding=binding,
        session_id="h5:fixture" if h5 else "web:fixture",
    )
    assert _logical_navigation_postcondition_error(
        parameters=parameters, state_diff=primary["state_diff"],
    ) == ("H5_POPUP_CHAIN_REMAINS_BLOCKED" if advanced else "LOGICAL_TRANSITION_UNVERIFIED")
    assert "dismissal_verified" not in primary
    assert primary["result"] == last_result
    assert previous["dismissal_verified"] is verified


@pytest.mark.parametrize(
    "h5_capability,verified,signature_matches,accepted",
    [
        (True, True, True, True),
        (False, True, True, False),
        (True, False, True, False),
        (True, True, False, False),
    ],
)
@pytest.mark.parametrize("capability,session_id", [
    ("rendered_modal_close", None),
    ("profile_credential_screenshot_confirm", "h5:fixture"),
    ("profile_credential_screenshot_confirm", "web:fixture"),
    ("profile_credential_screenshot_confirm", None),
])
def test_hidden_original_layer_requires_h5_close_proof(
    h5_capability,
    verified,
    signature_matches,
    accepted,
    capability,
    session_id,
) -> None:
    binding = LogicalTargetRuntimeBinding(
        source_identity_fingerprint="sha256:" + "1" * 64,
        source_state_fingerprint="sha256:" + "2" * 64,
        target_signature="sha256:" + "3" * 64,
        target_source="dom",
    )
    entry = {
        "dismissal_verified": verified,
        "popup_dismissal_outcome": "original_layer_hidden",
        "logical_target": {
            "target_trust": "trusted",
            "target_signature": binding.target_signature if signature_matches else "invalid",
            "source_state_fingerprint": binding.source_state_fingerprint,
        },
        "state_diff": {
            "logical_surface_transition": {
                "protocol_version": "logical_surface_transition.v1",
                "logical_transition_proof": "trusted",
                "transition_kind": "surface_state_changed",
                "corroborating_signals": ["surface_state"],
                "before_identity_fingerprint": binding.source_identity_fingerprint,
                "after_identity_fingerprint": binding.source_identity_fingerprint,
                "before_state_fingerprint": binding.source_state_fingerprint,
                "after_state_fingerprint": "sha256:" + "4" * 64,
            }
        },
    }
    _annotate_trusted_popup_clearance(
        PageCaptureResult(
            execution_status="succeeded",
            mode="click",
            reachable=True,
            final_url="https://fixture.test/",
            title="首页",
            status_code=200,
            action_trace=[entry],
        ),
        parameters={
            "target_mode": "popup_dismissal",
            "popup_dismissal_capability": capability if h5_capability else "other",
        },
        runtime_binding=binding,
        session_id=session_id,
    )
    error = _logical_navigation_postcondition_error(
        parameters={
            "target_mode": "popup_dismissal",
            "logical_target": {"expects_logical_navigation": True},
        },
        state_diff=entry["state_diff"],
    )
    if capability == "profile_credential_screenshot_confirm" and session_id != "h5:fixture":
        accepted = False
    assert (error is None) is accepted
    if not accepted:
        assert error == "LOGICAL_TRANSITION_UNVERIFIED"


@pytest.mark.parametrize(
    "binding_present,dynamic,verified,signature_matches,identity_matches,state_changed,accepted",
    [
        (True, True, True, True, True, True, True),
        (False, True, True, True, True, True, False),
        (True, False, True, True, True, True, False),
        (True, True, False, True, True, True, False),
        (True, True, True, False, True, True, False),
        (True, True, True, True, False, True, False),
        (True, True, True, True, True, False, False),
    ],
)
def test_startup_layer_clearance_uses_revalidated_h5_binding(
    binding_present, dynamic, verified, signature_matches,
    identity_matches, state_changed, accepted,
):
    binding = LogicalTargetRuntimeBinding(
        source_identity_fingerprint="sha256:" + "1" * 64,
        source_state_fingerprint="sha256:" + "2" * 64,
        target_signature="sha256:" + "3" * 64,
        target_source="dom",
        allow_dynamic_state_revalidation=dynamic,
    )
    before_state = "sha256:" + "4" * 64
    entry = {
        "selector": POPUP_SKIP_WRAP_DISMISSAL_SELECTOR,
        "dismissal_verified": verified,
        "logical_target": {
            "target_trust": "trusted",
            "target_signature": binding.target_signature if signature_matches else "invalid",
            "source_state_fingerprint": binding.source_state_fingerprint,
        },
        "state_diff": {
            "logical_surface_transition": {
                "protocol_version": "logical_surface_transition.v1",
                "logical_transition_proof": "trusted",
                "transition_kind": "surface_state_changed",
                "corroborating_signals": ["surface_state"],
                "before_identity_fingerprint": binding.source_identity_fingerprint,
                "after_identity_fingerprint": (
                    binding.source_identity_fingerprint
                    if identity_matches else "sha256:" + "6" * 64
                ),
                "before_state_fingerprint": before_state,
                "after_state_fingerprint": "sha256:" + "5" * 64 if state_changed else before_state,
            },
        },
    }
    parameters = {
        "target_mode": "startup_ad_dismissal",
        "logical_target": {"expects_logical_navigation": True},
    }
    result = PageCaptureResult(
        execution_status="success", mode="click", reachable=True,
        final_url="https://fixture.test/", title="开屏", status_code=200, action_trace=[entry],
    )
    _annotate_trusted_startup_ad_layer_clearance(
        result, parameters=parameters, runtime_binding=binding if binding_present else None,
    )
    error = _logical_navigation_postcondition_error(
        parameters=parameters, state_diff=entry["state_diff"],
    )
    assert (error is None) is accepted


@pytest.mark.parametrize("session_id,accepted", [("h5:run-1", True), ("web:run-1", False)])
async def test_executor_dynamic_startup_clearance_is_h5_only(session_id, accepted):
    class Browser:
        async def capture(self, request):
            assert request._logical_target_binding.allow_dynamic_state_revalidation is True
            return _page_result(mode=request.mode, action_trace=[{
                "action": "click",
                "selector": POPUP_SKIP_WRAP_DISMISSAL_SELECTOR,
                "dismissal_verified": True,
                "state_diff": {"logical_surface_transition": {
                    "protocol_version": "logical_surface_transition.v1",
                    "transition_kind": "surface_state_changed",
                    "logical_transition_proof": "trusted",
                    "before_identity_fingerprint": "sha256:" + "1" * 64,
                    "after_identity_fingerprint": "sha256:" + "1" * 64,
                    "before_state_fingerprint": "sha256:" + "4" * 64,
                    "after_state_fingerprint": "sha256:" + "5" * 64,
                    "corroborating_signals": ["surface_state"],
                }},
            }])

    intent = replace(_intent(parameters={
        "target_mode": "startup_ad_dismissal",
        "startup_ad_capability": "countdown_close",
        "logical_target": _logical_target_contract(),
    }, semantic_target="跳过"), session_id=session_id)
    executor = BrowserInteractionExecutor(
        browser=Browser(),
        target_resolver=RecordingResolver(ResolvedInteractionTarget(
            selector=POPUP_SKIP_WRAP_DISMISSAL_SELECTOR,
            target_source="dom", target_signature="sha256:" + "3" * 64,
            source_state_fingerprint="sha256:" + "2" * 64, target_trust="trusted",
        )),
        evidence_buffer=InMemoryActionEvidenceBuffer(),
    )
    result = await executor.execute(ExecuteInteractionRequest(
        agent_run_id="run-1", intent=intent, source_capture_id="capture-before",
    ))
    assert (result.status is InteractionResultStatus.EVIDENCE_PENDING) is accepted
    assert result.error_code == (None if accepted else "LOGICAL_TRANSITION_UNVERIFIED")
