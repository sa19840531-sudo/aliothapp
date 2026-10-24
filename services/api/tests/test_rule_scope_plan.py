"""聚合规则适用范围计划的领域合同。"""

from __future__ import annotations

import pytest

from services.api.app.domain.inspection.rule_scope import (
    RuleScopeAuthority,
    RuleScopeStatus,
    RuleScopeValidationError,
    build_rule_scope_plan,
)


def _authority(**overrides: object) -> RuleScopeAuthority:
    values = {
        "task_id": "task-1",
        "source_agent_run_id": "run-1",
        "finalization_epoch": 2,
        "authority_type": "web_coverage_snapshot",
        "authority_id": "snapshot-1",
        "authority_hash": "sha256:" + "a" * 64,
        "scope_verified": True,
        "applicable_page_types": ("home", "profile"),
        "not_applicable_page_types": ("payment",),
        "unresolved_page_types": (),
        "negative_evidence_refs": ("web-coverage:snapshot-1",),
    }
    values.update(overrides)
    return RuleScopeAuthority(**values)


def test_rule_scope_plan_is_stable_and_marks_matching_scope_applicable() -> None:
    first = build_rule_scope_plan(
        authority=_authority(),
        rule_id="rule-1",
        applicable_page_types=("profile",),
    )
    replay = build_rule_scope_plan(
        authority=_authority(),
        rule_id="rule-1",
        applicable_page_types=("profile",),
    )

    assert first.status is RuleScopeStatus.APPLICABLE
    assert first.matched_applicable_page_types == ("profile",)
    assert first.plan_hash == replay.plan_hash


def test_rule_scope_plan_only_marks_fully_proven_negative_scope_not_applicable() -> None:
    plan = build_rule_scope_plan(
        authority=_authority(),
        rule_id="rule-1",
        applicable_page_types=("payment",),
    )

    assert plan.status is RuleScopeStatus.NOT_APPLICABLE
    assert plan.matched_not_applicable_page_types == ("payment",)
    assert plan.negative_evidence_refs == ("web-coverage:snapshot-1",)


def test_rule_scope_plan_keeps_partial_or_unknown_scope_unproven() -> None:
    plan = build_rule_scope_plan(
        authority=_authority(),
        rule_id="rule-1",
        applicable_page_types=("payment", "unknown_type"),
    )

    assert plan.status is RuleScopeStatus.UNPROVEN
    assert plan.unresolved_page_types == ("unknown_type",)


def test_negative_scope_requires_verified_authority_and_evidence_reference() -> None:
    with pytest.raises(RuleScopeValidationError, match="负向依据"):
        _authority(negative_evidence_refs=())

    with pytest.raises(RuleScopeValidationError, match="已验证"):
        _authority(scope_verified=False)
