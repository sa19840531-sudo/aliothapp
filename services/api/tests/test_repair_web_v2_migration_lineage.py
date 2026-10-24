"""重编号兼容只接受完整旧链；未知、混合和半迁移状态一律拒绝。"""

import pytest

from scripts.repair_web_v2_migration_lineage import LEGACY, TARGET, validate_audit


def state(version=LEGACY, *, web=True, additions=False):
    return {
        "versions": [version],
        "web_checks": {"checkpoint": web, "error_audit": True},
        "addition_checks": {"ruleset": additions, "rule_test": additions},
    }


def test_complete_legacy_can_be_repaired():
    assert validate_audit(state()) == "repair_required"


def test_current_chain_is_idempotent():
    assert validate_audit(state(TARGET, additions=True)) == "already_current"


@pytest.mark.parametrize(
    "mutation",
    [
        {"versions": []},
        {"versions": [LEGACY, TARGET]},
        {"versions": ["20260914_0119"]},
        {"web_checks": {"checkpoint": False}},
        {"addition_checks": {"ruleset": True, "rule_test": False}},
        {"addition_checks": {"ruleset": True, "rule_test": True}},
    ],
)
def test_unknown_partial_or_mixed_schema_is_rejected(mutation):
    with pytest.raises(RuntimeError):
        validate_audit(state() | mutation)


def test_current_version_with_missing_ddl_is_rejected():
    with pytest.raises(RuntimeError):
        validate_audit(state(TARGET))
