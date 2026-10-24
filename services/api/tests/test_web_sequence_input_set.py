"""Web 时间序列显式封存，不放宽 page/journey 的既有边界。"""

import pytest

from services.api.app.domain.evidence.input_set import (
    EvidenceInputScope,
    EvidenceInputSet,
    EvidenceInputSetValidationError,
)
from services.api.tests.test_evidence_input_set import _candidate, _seal


def test_web_sequence_preserves_exact_ordered_capture_roles_and_hash():
    roles = ("before_action", "after_action", "before_action", "after_action")
    ids = ("before-1", "after-1", "before-2", "after-2")
    candidates = tuple(
        _candidate(capture_id, page_sequence=index, capture_role=role, artifact_role=role)
        for index, (capture_id, role) in enumerate(zip(ids, roles, strict=True), start=1)
    )
    result = _seal(
        scope=EvidenceInputScope("web_sequence"), eligible_capture_ids=ids, candidates=candidates
    )
    assert tuple(item.capture_role for item in result.members) == roles
    assert EvidenceInputSet.from_dict(result.to_dict()) == result
    with pytest.raises(EvidenceInputSetValidationError, match="一个 Capture"):
        _seal(scope=EvidenceInputScope.PAGE, eligible_capture_ids=ids, candidates=candidates)


def test_web_sequence_rejects_arbitrary_primary_material():
    with pytest.raises(EvidenceInputSetValidationError, match="Web"):
        _seal(scope=EvidenceInputScope("web_sequence"))


@pytest.mark.parametrize("enabled", [False, True])
def test_web_sequence_cannot_enter_legacy_aggregate_scope(enabled):
    from services.api.app.application.aggregate_rule_config import (
        AggregateRuleConfigurationError,
        resolve_rule_evidence_input_configuration,
    )

    with pytest.raises(AggregateRuleConfigurationError, match="web_sequence"):
        resolve_rule_evidence_input_configuration(
            {
                "capability_config": {
                    "evidence_input_scope": "web_sequence",
                    "evidence_scope_key": "journey_observation",
                }
            },
            require_executable=enabled,
        )


@pytest.mark.parametrize("status", ["draft", "enabled"])
def test_api_rejects_web_sequence_disguised_as_aggregate(status):
    from pydantic import ValidationError

    from services.api.app.api.v1.rules import RulePayload
    from services.api.tests.test_web_rule_evidence_contract import _contract, _payload

    payload = _payload(_contract())
    payload.update(
        status=status,
        judge_config={
            "capability_config": {
                "evidence_input_scope": "web_sequence",
                "evidence_scope_key": "journey_observation",
            }
        },
    )
    with pytest.raises(ValidationError, match="web_sequence"):
        RulePayload(**payload)
