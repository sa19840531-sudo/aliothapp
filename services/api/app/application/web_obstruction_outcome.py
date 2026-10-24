"""Web 普通关闭按原层消失和原业务底页恢复结算。"""

from services.api.app.application.web_interaction_facts import (
    WebInteractionCaptureFacts,
    web_business_identity_stable,
    web_single_action_plan,
)
from services.api.app.application.web_interaction_precision import (
    WebInteractionOutcome,
    WebInteractionPostcondition,
    assess_web_interaction_outcome,
)
from services.api.app.domain.interaction import ActionIntent


def settle_web_obstruction_dismissal(
    *,
    intent: ActionIntent,
    selector: str,
    before: WebInteractionCaptureFacts | None,
    after: WebInteractionCaptureFacts | None,
    after_capture_id: str,
    command_accepted: bool,
) -> WebInteractionOutcome:
    if before is None or after is None:
        return WebInteractionOutcome(
            command_accepted=command_accepted,
            effect_observed=False,
            postcondition_satisfied=False,
            transition_arrived=False,
            after_capture_id=after_capture_id,
            failure_reason="obstruction_facts_missing",
        )
    layer = dict(before.candidate_layers).get(selector)
    disappeared = bool(layer and layer not in after.visible_layers)
    original_body = web_business_identity_stable(before, after)
    return assess_web_interaction_outcome(
        plan=web_single_action_plan(intent, WebInteractionPostcondition.OBSTRUCTION_DISMISSED),
        command_accepted=command_accepted,
        before_url=before.url,
        after_url=after.url,
        before_content_identity=before.content_identity,
        after_content_identity=after.content_identity,
        effect_observed=disappeared
        or bool(
            before.obstruction_content_identity
            and after.obstruction_content_identity
            and before.obstruction_content_identity != after.obstruction_content_identity
        ),
        postcondition_satisfied=disappeared and original_body,
        after_capture_id=after_capture_id,
        readiness_accepted=after.readiness_accepted,
        identity_verified=after.identity_verified
        and original_body
        and after_capture_id != intent.source_capture_id,
        business_layer_visible=after.business_layer_visible,
        evidence_mode=after.evidence_mode,
    )
