"""H5 顶层信息弹窗安全确认能力回归。"""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.application.h5_modal_acknowledgement import (
    H5_MODAL_LOCAL_SAFE_ACKNOWLEDGEMENT_CAPABILITY,
    H5_MODAL_LOCAL_SAFE_ACKNOWLEDGEMENT_SELECTOR,
    h5_modal_local_safe_acknowledgement,
)


@pytest.mark.asyncio
async def test_safe_acknowledgement_proof_survives_resolver_to_browser_validation():
    from services.api.app.application.interaction_runtime import SemanticTargetResolutionRequest
    from services.api.app.domain.interaction import InteractionOperation
    from services.api.app.infrastructure.browser.page_capture import (
        PageCaptureMode,
        PageCaptureRequest,
        _validate_request,
    )
    from services.api.app.infrastructure.browser.semantic_target_resolver import (
        UiSnapshotSemanticTargetResolver,
    )

    snapshot = _snapshot()
    proof = h5_modal_local_safe_acknowledgement(snapshot)
    resolver = UiSnapshotSemanticTargetResolver(
        snapshot_reader=SimpleNamespace(read_ui_snapshot=AsyncMock(return_value=snapshot)),
    )
    resolved = await resolver.resolve(SemanticTargetResolutionRequest(
        task_id="task-h5", session_id="h5:run", source_capture_id="source",
        action_type=InteractionOperation.CLICK, semantic_target="关闭当前弹窗",
        parameters={"target_mode": "popup_dismissal",
                    "popup_dismissal_capability": proof.capability,
                    "obstruction_signature": proof.obstruction_signature},
    ))
    assert resolved.selector == (
        f"{H5_MODAL_LOCAL_SAFE_ACKNOWLEDGEMENT_SELECTOR}:{proof.obstruction_signature}"
    )
    assert resolved.target_trust == "trusted"
    assert resolved.trusted_target_role == "dismiss"
    assert resolved.trusted_target_texts == ("关闭当前弹窗",)
    _validate_request(PageCaptureRequest(
        session_id="h5:run", mode=PageCaptureMode.SINGLE_TRIGGER,
        trigger_action="click", selector=resolved.selector,
        clear_obstructions=resolved.clear_obstructions,
        allow_high_z_obstruction_hide=resolved.allow_high_z_obstruction_hide,
        h5_popup_dismissal_strategy=resolved.h5_popup_dismissal_strategy,
    ))


@pytest.mark.asyncio
async def test_safe_acknowledgement_logical_binding_keeps_dismiss_semantics():
    from services.api.app.application.interaction_runtime import SemanticTargetResolutionRequest
    from services.api.app.domain.interaction import InteractionOperation
    from services.api.app.infrastructure.browser.semantic_target_resolver import (
        UiSnapshotSemanticTargetResolver,
        _bind_trusted_logical_target,
    )

    snapshot = _snapshot()
    proof = h5_modal_local_safe_acknowledgement(snapshot)
    request = SemanticTargetResolutionRequest(
        task_id="task-h5", session_id="h5:run", source_capture_id="source",
        action_type=InteractionOperation.CLICK, semantic_target="关闭当前弹窗",
        parameters={"target_mode": "popup_dismissal",
                    "popup_dismissal_capability": proof.capability,
                    "obstruction_signature": proof.obstruction_signature},
    )
    resolver = UiSnapshotSemanticTargetResolver(
        snapshot_reader=SimpleNamespace(read_ui_snapshot=AsyncMock(return_value=snapshot)),
    )
    resolved = await resolver.resolve(request)
    bound = _bind_trusted_logical_target(
        snapshot=snapshot, request=request, resolved=resolved,
        source_state="sha256:" + "1" * 64,
    )
    assert bound.selector == resolved.selector
    assert bound.target_signature is not None
    assert bound.trusted_target_role == "dismiss"
    assert bound.trusted_target_texts == ("关闭当前弹窗",)


@pytest.mark.parametrize("case,accepted", [
    ("same_layer", True), ("payment", False), ("replacement", False),
])
def test_acknowledgement_state_drift_requires_fresh_same_layer_proof(case, accepted):
    from services.api.app.application.interaction_runtime import SemanticTargetResolutionRequest
    from services.api.app.domain.interaction import InteractionOperation
    from services.api.app.infrastructure.browser.semantic_target_resolver import (
        _popup_dismissal_state_drift_revalidated,
    )

    snapshot = _snapshot()
    proof = h5_modal_local_safe_acknowledgement(snapshot)
    if case == "payment":
        snapshot = _snapshot(content_text="确认支付 99 元")
    elif case == "replacement":
        snapshot["elements"][-1]["selector"] = "#replacement"
    request = SemanticTargetResolutionRequest(
        task_id="task-h5", session_id="h5:run", source_capture_id="source",
        action_type=InteractionOperation.CLICK, semantic_target="关闭当前弹窗",
        parameters={"target_mode": "popup_dismissal",
                    "popup_dismissal_capability": proof.capability,
                    "obstruction_signature": proof.obstruction_signature},
    )
    assert _popup_dismissal_state_drift_revalidated(
        snapshot=snapshot, request=request,
    ) is accepted


@pytest.mark.asyncio
@pytest.mark.parametrize("risk_or_stale", ["safe", "risk", "stale"])
async def test_browser_revalidates_acknowledgement_before_native_pointer(
    monkeypatch, risk_or_stale,
):
    from services.api.app.infrastructure.browser import page_capture as module

    snapshot = _snapshot()
    proof = h5_modal_local_safe_acknowledgement(snapshot)
    if risk_or_stale == "risk":
        snapshot = _snapshot(content_text="确认支付 99 元")
    elif risk_or_stale == "stale":
        snapshot["elements"][-1]["selector"] = "#replacement"
    monkeypatch.setattr(module, "_collect_ui_snapshot", AsyncMock(return_value=snapshot))
    target = await module._resolve_popup_dismissal_target(
        page=SimpleNamespace(evaluate=AsyncMock(return_value={
            "token": "node-token", "shape": "fixture-shape",
        })),
        selector=f"{H5_MODAL_LOCAL_SAFE_ACKNOWLEDGEMENT_SELECTOR}:{proof.obstruction_signature}",
    )
    if risk_or_stale != "safe":
        assert target is None
    else:
        assert target["selector"] == "#modal-ack"
        assert target["pointer_click_required"] is True
        assert target["verification_required"] is True


def _snapshot(*, content_text: str = "版本更新公告，更多内容持续上线") -> dict:
    return {
        "schema_version": "ui-snapshot.v2",
        "viewport": {"width": 390, "height": 844},
        "elements": [
            {
                "selector": "#page-transaction",
                "tag": "button",
                "role": "button",
                "text": "确认充值",
                "interactive": True,
                "rect": {"x": 20, "y": 740, "width": 350, "height": 48},
                "hit_test": {
                    "center_target": "#modal-root",
                    "center_hits_target": False,
                },
            },
            {
                "selector": "#modal-root",
                "tag": "div",
                "text": "",
                "interactive": True,
                "rect": {"x": 0, "y": 0, "width": 390, "height": 844},
                "hit_test": {
                    "center_target": "#modal-content",
                    "center_hits_target": False,
                },
            },
            {
                "selector": "#modal-content",
                "tag": "div",
                "role": "text",
                "text": content_text,
                "interactive": False,
                "rect": {"x": 50, "y": 260, "width": 290, "height": 220},
                "hit_test": {
                    "center_target": "#modal-content",
                    "center_hits_target": True,
                },
            },
            {
                "selector": "#modal-ack",
                "tag": "button",
                "role": "button",
                "text": "确认",
                "interactive": True,
                "rect": {"x": 135, "y": 560, "width": 120, "height": 40},
                "hit_test": {
                    "center_target": "#modal-ack",
                    "center_hits_target": True,
                },
            },
        ],
    }


def test_safe_acknowledgement_uses_active_modal_layer_not_underlying_payment_page() -> None:
    proof = h5_modal_local_safe_acknowledgement(_snapshot())

    assert proof is not None
    assert proof.capability == H5_MODAL_LOCAL_SAFE_ACKNOWLEDGEMENT_CAPABILITY
    assert proof.selector == "#modal-ack"


def test_safe_acknowledgement_rejects_transaction_semantics_in_active_modal() -> None:
    assert (
        h5_modal_local_safe_acknowledgement(
            _snapshot(content_text="订单金额 99 元，是否确认支付")
        )
        is None
    )


def test_safe_acknowledgement_rejects_ambiguous_or_truncated_snapshot() -> None:
    ambiguous = _snapshot()
    ambiguous["elements"].append(
        {
            **deepcopy(ambiguous["elements"][-1]),
            "selector": "#modal-ack-second",
        }
    )
    truncated = _snapshot()
    truncated["element_limit_reached"] = True

    assert h5_modal_local_safe_acknowledgement(ambiguous) is None
    assert h5_modal_local_safe_acknowledgement(truncated) is None


def _complete_notice_snapshot():
    snapshot = _snapshot(content_text="支付通道维护，请先注册账号再充值；漫画加载已优化")
    snapshot["h5_modal_census_truncated"] = False
    snapshot["h5_modal_census_selectors"] = ["#modal-root"]
    for element in snapshot["elements"][2:]:
        element["parent_selector"] = "#modal-root"
        element["h5_modal_ancestor_selectors"] = ["#modal-root"]
    snapshot["elements"][-1].update({
        "text": "知道了", "type": "button", "form_associated": False,
        "h5_modal_action_semantics": "acknowledge",
        "navigation_payload_in_subtree": False,
    })
    return snapshot


def test_complete_informational_notice_can_be_dismissed_despite_background_risk_words():
    assert h5_modal_local_safe_acknowledgement(_complete_notice_snapshot()) is not None


@pytest.mark.parametrize("case", ["form", "navigation", "confirm", "consent", "incomplete"])
def test_informational_acknowledgement_does_not_authorize_risky_actions(case):
    snapshot = _complete_notice_snapshot()
    if case == "form":
        snapshot["elements"][2]["tag"] = "input"
    elif case == "navigation":
        snapshot["elements"][-1]["href"] = "/payment"
    elif case == "confirm":
        snapshot["elements"][-1]["text"] = "确认"
    elif case == "consent":
        snapshot["elements"][2]["text"] = "点击知道了表示同意隐私条款与授权"
    else:
        snapshot["h5_modal_census_truncated"] = True
    assert h5_modal_local_safe_acknowledgement(snapshot) is None
