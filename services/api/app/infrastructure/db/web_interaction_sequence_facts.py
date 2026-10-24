"""按真实持久动作认证 readiness 结算引用，不把 OBSERVE 当原关闭动作。"""

from dataclasses import asdict

from services.api.app.application.web_interaction_precision import (
    WebInteractionEvidenceMode,
    WebInteractionOutcome,
)
from services.api.app.domain.interaction import ActionIntent, build_action_intent_hash
from services.api.app.infrastructure.db.web_sequence_binding_reader import _mapping


async def authenticate_interaction_facts(connection, *, sequence, outcomes, action_refs):
    if not 1 <= len(outcomes) == len(action_refs) <= 12:
        raise ValueError("WEB_INTERACTION_ORIGINAL_ACTION_MISSING")
    frames = sequence["frames"]
    afters = [frame for frame in frames if frame["capture_role"] == "after_action"]
    positions = {frame["capture_id"]: index for index, frame in enumerate(afters)}
    actions = {}
    for frame in afters:
        row = await connection.fetchrow(
            "SELECT action.* FROM qc_action_execution action JOIN qc_capture before_capture "
            "ON before_capture.id=action.source_capture_id AND before_capture.task_id=action.task_id "
            "WHERE action.action_execution_id=$1 AND action.task_id=$2::uuid AND action.session_id=$3 "
            "AND action.finalization_epoch=$4 AND action.status='completed' "
            "AND action.result_capture_id=$5::uuid AND action.source_capture_id=$6::uuid "
            "AND before_capture.parent_capture_id=action.requested_source_capture_id",
            frame["action_execution_id"],
            sequence["task_id"],
            sequence["session_id"],
            sequence["finalization_epoch"],
            frame["capture_id"],
            frame["parent_capture_id"],
        )
        if row is None:
            raise ValueError("WEB_INTERACTION_ACTION_CHAIN_INVALID")
        intent = ActionIntent.from_dict(_mapping(row["intent_payload"]))
        if (
            build_action_intent_hash(intent) != row["intent_hash"]
            or intent.task_id != sequence["task_id"]
            or intent.session_id != sequence["session_id"]
            or intent.intent_id != row["intent_id"]
            or intent.source_capture_id != str(row["requested_source_capture_id"])
        ):
            raise ValueError("WEB_INTERACTION_INTENT_DRIFT")
        actions[frame["capture_id"]] = (row, intent)
    verified = []
    for ordinal, (raw, ref) in enumerate(zip(outcomes, action_refs, strict=True)):
        if ref.get("outcome_ordinal") != ordinal or type(ref.get("outcome_ordinal")) is not int:
            raise ValueError("WEB_INTERACTION_OUTCOME_ORDINAL_INVALID")
        outcome_fields = {
            key: raw.get(key)
            for key in (
                "command_accepted",
                "effect_observed",
                "postcondition_satisfied",
                "transition_arrived",
                "after_capture_id",
                "failure_reason",
            )
        }
        outcome = WebInteractionOutcome(
            **outcome_fields,
            evidence_mode=WebInteractionEvidenceMode(raw.get("evidence_mode", "natural")),
        )
        original = ref.get("action_result_capture_id")
        final = outcome.after_capture_id
        if (
            original not in positions
            or final not in positions
            or positions[original] > positions[final]
        ):
            raise ValueError("WEB_INTERACTION_OBSERVATION_CHAIN_INVALID")
        row, intent = actions[original]
        if (
            intent.action_type.value != "click"
            or ref.get("intent_id") != intent.intent_id
            or ref.get("requested_source_capture_id") != intent.source_capture_id
            or intent.parameters.get("web_sealed_candidate_capability") != "web_obstruction_dismiss"
            or outcome.evidence_mode is not WebInteractionEvidenceMode.NATURAL
        ):
            raise ValueError("WEB_INTERACTION_ORIGINAL_ACTION_INVALID")
        receipt = _mapping(row["result_payload"])
        if outcome.command_accepted != (receipt.get("execution_outcome") == "succeeded"):
            raise ValueError("WEB_INTERACTION_ACCEPTED_FACT_MISMATCH")
        for step in afters[positions[original] + 1 : positions[final] + 1]:
            step_row, step_intent = actions[step["capture_id"]]
            if (
                step_intent.action_type.value != "observe"
                or str(step_row["requested_source_capture_id"])
                != afters[positions[step["capture_id"]] - 1]["capture_id"]
            ):
                raise ValueError("WEB_INTERACTION_FINAL_OBSERVATION_INVALID")
        verified.append(
            {
                **asdict(outcome),
                **dict(ref),
                "action_execution_id": row["action_execution_id"],
                "before_capture_id": str(row["source_capture_id"]),
                "intent_hash": row["intent_hash"],
            }
        )
    return verified
