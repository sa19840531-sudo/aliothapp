"""套餐只读反馈丢失复现；选中样式、旧目标和不可信隔离不能证明动作成功。"""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from services.api.app.application.checkpoint_judgement import judge_checkpoint
from services.api.app.application.h5_probe_targets import probe_subject_key
from services.api.app.domain.journey import parse_journey_definition
from services.api.app.infrastructure.browser.capture_runtime import _public_state_diff
from services.api.tests.test_h5_pair_probe_targets import _snapshot


def payment_case():
    before = _snapshot("payment_plan_selection")
    before.update(url="https://fixture.invalid/member", document={"time_origin_ms": 123})
    after = deepcopy(before)
    after["elements"][0]["aria_checked"] = "true"
    effect = dict(
        protocol_version="h5-probe-display-effect.v1",
        goal="payment_plan_selection",
        subject_key=probe_subject_key(before["elements"][0]),
        verified=True,
        readonly_isolation=True,
        readonly_denied_count=0,
        url=after["url"],
        document_time_origin_ms=123,
        before_selector="#private",
        after_selector="#private",
    )
    trace = [dict(result="probe_effect_observed", state_diff={"h5_probe_effect": effect})]
    checkpoint = parse_journey_definition(
        dict(
            schema_version="journey-dsl.v2",
            definition_id="payment-display",
            version=1,
            name="套餐只读展示",
            surface_kinds=["web"],
            checkpoints=[
                dict(
                    id="selected",
                    entry_condition={"payment_display_visible": True},
                    expected={"payment_plan_selected": True},
                    exit_condition={"observation_complete": True},
                    depends_on=[],
                    recovery_policy="same_session",
                    side_effect_policy="read_only",
                    evidence_requirements=[
                        dict(
                            role="selection", artifact_types=["screenshot", "dom", "action_receipt"]
                        )
                    ],
                )
            ],
        )
    ).checkpoints[0]
    manifests = [
        dict(
            quality_status="pass",
            artifacts=[dict(artifact_type=kind) for kind in ("screenshot", "dom")],
        )
        for _ in range(2)
    ]
    return checkpoint, before, after, trace, manifests


def test_driver_receipt_retains_verified_feedback_but_no_locator_or_raw_url():
    _, _, _, trace, _ = payment_case()
    diff = _public_state_diff(SimpleNamespace(action_trace=trace))
    feedback = diff["h5_probe_effect"]
    assert feedback["verified"] is True
    assert len(feedback["url_hash"]) == 64
    assert "url" not in feedback
    assert "before_selector" not in feedback
    assert "after_selector" not in feedback


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "missing_receipt",
        "isolation",
        "denied",
        "document",
        "target",
        "loading",
        "selected_style_only",
        "unverified",
        "failed_action",
        "invalid_document",
    ],
)
def test_payment_selection_requires_same_action_subject_document_and_readonly_feedback(fault):
    checkpoint, before, after, trace, manifests = payment_case()
    effect = trace[0]["state_diff"]["h5_probe_effect"]
    if fault == "isolation":
        effect["readonly_isolation"] = False
    elif fault == "denied":
        effect["readonly_denied_count"] = 1
    elif fault == "document":
        effect["document_time_origin_ms"] = 10
    elif fault == "target":
        after["elements"][0]["text"] = "另一个套餐 999元"
    elif fault == "loading":
        after["loading"] = True
    elif fault == "unverified":
        effect["verified"] = False
    elif fault == "invalid_document":
        before["document"]["time_origin_ms"] = True
        after["document"]["time_origin_ms"] = True
        effect["document_time_origin_ms"] = True
    receipt = _public_state_diff(SimpleNamespace(action_trace=trace))
    if fault in {"missing_receipt", "selected_style_only"}:
        receipt = {}
    result = judge_checkpoint(
        checkpoint,
        before=before,
        after=after,
        manifests=manifests,
        action_receipt={
            "state_diff": receipt,
            "status": "blocked" if fault == "failed_action" else "succeeded",
            "execution_outcome": "succeeded",
        },
    )
    assert result["status"] == ("pass" if fault is None else "evidence_insufficient")
    assert "order_created" not in result["checks"]
