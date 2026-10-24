"""E11 需求集生成服务契约测试（EvidenceExitRequirementResolver）。

冻结文档：
- docs/design/2026-08-10-m0-e01-evidence-role-value-table.md（条款 3.3 映射、附录 5 别名）
- docs/design/2026-08-10-m0-e02-quality-status-authority.md（条款 1.6 需求声明化）

口径变更必须先修订冻结文档并重走三方评审，再同步本测试。
"""

import csv
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from services.api.app.application.evidence_exit_requirements import (
    EvidenceExitRequirementResolutionError,
    EvidenceExitRequirementResolver,
)
from services.api.app.application.task_query import CandidateRule
from services.api.app.domain.evidence import vocabulary
from services.api.app.domain.exploration.evidence_exit_requirements import (
    ActiveSessionEvidenceRequirement,
    EvidenceExitRequirementStatus,
    build_active_session_evidence_requirement,
    build_evidence_exit_requirement_set,
)
from services.api.app.infrastructure.db.evidence_exit_gate_repository import (
    EvidenceExitGatePersistenceError,
    _validate_requirement_revision,
)

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SEED_CSV = _REPO_ROOT / "docs" / "data" / "alioth_first_ruleset_qc_rule_seed.csv"

# 独立字面量钉住生产词汇表（E01），防止生产侧词汇被悄悄改动
_E01_SUPPLY_ROLES = frozenset(
    {
        "primary",
        "before_action",
        "after_action",
        "bootstrap",
        "probe_before",
        "probe_result_replay",
        "shadow_candidate",
        "refreshed",
        "exploration_terminal",
        "evidence_insufficient",
    }
)
_E01_REQUIREMENT_ROLE_NAMES = frozenset(
    {"primary", "before", "after", "source", "refreshed", "landing", "fault"}
)
_E01_REQUIREMENT_ROLE_PATTERNS = (
    r"^attempt_[1-9]\d*$",
    r"^location_[1-9]\d*$",
    r"^entry_[1-9]\d*$",
)
_E01_ARTIFACT_TYPES = frozenset(
    {
        "screenshot",
        "dom",
        "visible_text",
        "ocr_text",
        "har",
        "console_log",
        "media_state",
        "ai_io",
        "report",
        "action_trace",
        "ui_snapshot",
        "temporal_trace",
        "page_coverage",
        "ui_tree",
        "view_hierarchy",
        "device_log",
        "network_trace",
        "fault_artifact",
    }
)
_E01_ARTIFACT_ALIASES = {
    "dom_snapshot": "dom",
    "network_log": "har",
    "network_events": "har",
}
_E01_ROLE_MAP = {"before": "before_action", "after": "after_action"}


def _candidate(
    *,
    rule_id: str = "rule-1",
    rule_code: str = "alioth_demo",
    input_contract: dict | None = None,
    judge_config: dict | None = None,
) -> CandidateRule:
    return CandidateRule(
        rule_id=rule_id,
        rule_code=rule_code,
        name="测试规则",
        category="text",
        severity="low",
        executor_type="hybrid",
        page_type=None,
        applicable_page_types=["content_list"],
        check_point="",
        judge_config=judge_config or {},
        sort_order=1,
        match_reason="applicable_page_types",
        input_contract=input_contract or {},
    )


def _resolve(rules, *, revision: int = 1):
    return EvidenceExitRequirementResolver().resolve(
        task_id="task-1",
        session_id="session-1",
        capture_id="capture-1",
        candidate_rules=rules,
        created_at=NOW,
        revision=revision,
    )


def _seed_rules() -> list[tuple[str, dict]]:
    rows: list[tuple[str, dict]] = []
    with _SEED_CSV.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            raw = (row.get("input_contract_json") or "").strip()
            if raw:
                rows.append(((row.get("rule_code") or "").strip(), json.loads(raw)))
    assert rows, "种子 CSV 未读到任何 input_contract_json"
    return rows


# ---------------------------------------------------------------------------
# 生产词汇表与 E01 冻结字面量一致
# ---------------------------------------------------------------------------


def test_vocabulary_matches_e01_freeze() -> None:
    assert vocabulary.FROZEN_SUPPLY_CAPTURE_ROLES == _E01_SUPPLY_ROLES
    assert vocabulary.FROZEN_REQUIREMENT_ROLE_NAMES == _E01_REQUIREMENT_ROLE_NAMES
    assert tuple(pattern.pattern for pattern in vocabulary.REQUIREMENT_ROLE_PATTERNS) == (
        _E01_REQUIREMENT_ROLE_PATTERNS
    )
    assert vocabulary.FROZEN_ARTIFACT_TYPES == _E01_ARTIFACT_TYPES | {"checkpoint_observation"}
    assert vocabulary.ARTIFACT_TYPE_ALIASES == _E01_ARTIFACT_ALIASES
    assert vocabulary.REQUIREMENT_TO_SUPPLY_ROLE_MAP == _E01_ROLE_MAP
    assert vocabulary.CAPTURE_ROLE_GRAMMAR.pattern == r"^[a-z][a-z0-9_]{0,31}$"


# ---------------------------------------------------------------------------
# E01 条款 3.3：需求简名归一为供给角色（缺陷 D1 收口）
# ---------------------------------------------------------------------------


def test_resolver_maps_before_after_to_supply_roles() -> None:
    result = _resolve(
        [
            _candidate(
                input_contract={
                    "required_artifacts": ["screenshot"],
                    "required_capture_roles": ["before", "after", "primary"],
                }
            )
        ]
    )
    (requirement,) = result.requirements
    assert requirement.required_capture_roles == ("after_action", "before_action", "primary")
    assert "before" not in requirement.required_capture_roles
    assert "after" not in requirement.required_capture_roles


def test_resolver_collapses_roles_to_current_capture_role() -> None:
    """E14 按单 Capture 对账，本帧清单只有一个供给角色。

    离场需求若保留跨帧角色（source/before/attempt_N），裁决必然 fail、
    需求永不销账；attempt_N 更是后续动作才产生的帧——拿未来的帧阻止
    当前离场是结构性死锁。
    """
    result = EvidenceExitRequirementResolver().resolve(
        task_id="task-1",
        session_id="session-1",
        capture_id="capture-1",
        candidate_rules=[
            _candidate(
                input_contract={
                    "required_artifacts": ["screenshot"],
                    "required_capture_roles": ["primary", "before", "source", "attempt_1"],
                }
            )
        ],
        created_at=NOW,
        capture_role="after_action",
    )

    (requirement,) = result.requirements
    assert requirement.required_capture_roles == ("after_action",)


def test_resolver_rejects_current_capture_role_outside_supply_vocabulary() -> None:
    with pytest.raises(EvidenceExitRequirementResolutionError):
        EvidenceExitRequirementResolver().resolve(
            task_id="task-1",
            session_id="session-1",
            capture_id="capture-1",
            candidate_rules=[_candidate()],
            created_at=NOW,
            capture_role="before",
        )


def test_resolver_keeps_grammar_roles_verbatim() -> None:
    result = _resolve(
        [
            _candidate(
                input_contract={
                    "required_capture_roles": ["attempt_1", "attempt_2", "primary"],
                }
            )
        ]
    )
    (requirement,) = result.requirements
    assert requirement.required_capture_roles == ("attempt_1", "attempt_2", "primary")


# ---------------------------------------------------------------------------
# E01 附录 5：artifact 别名归一（缺陷 C3 收口）
# ---------------------------------------------------------------------------


def test_resolver_normalizes_artifact_aliases() -> None:
    result = _resolve(
        [
            _candidate(
                input_contract={
                    "required_artifacts": ["dom_snapshot", "network_events", "network_log", "har"],
                }
            )
        ]
    )
    (requirement,) = result.requirements
    assert requirement.required_artifact_types == ("dom", "har")


# ---------------------------------------------------------------------------
# 缺省角色：未声明（或声明为空）时需求为主采集帧
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "contract",
    (
        {"required_artifacts": ["screenshot"]},
        {"required_artifacts": ["screenshot"], "required_capture_roles": []},
    ),
)
def test_resolver_defaults_missing_roles_to_primary(contract: dict) -> None:
    result = _resolve([_candidate(input_contract=contract)])
    (requirement,) = result.requirements
    assert requirement.required_capture_roles == ("primary",)


# ---------------------------------------------------------------------------
# E02 条款 1.6：quality_gate 两键按声明收敛，键集封闭
# ---------------------------------------------------------------------------


def test_resolver_collapses_quality_gate_verbatim() -> None:
    result = _resolve(
        [
            _candidate(
                input_contract={
                    "required_artifacts": ["screenshot", "visible_text"],
                    "quality_gate": {
                        "min_screenshot_count": 1,
                        "allow_empty_visible_text": False,
                    },
                }
            )
        ]
    )
    (requirement,) = result.requirements
    assert requirement.min_screenshot_count == 1
    assert requirement.allow_empty_visible_text is False


def test_resolver_defaults_quality_gate_when_absent() -> None:
    result = _resolve([_candidate(input_contract={"required_artifacts": ["screenshot"]})])
    (requirement,) = result.requirements
    assert requirement.min_screenshot_count == 0
    assert requirement.allow_empty_visible_text is True


def test_resolver_rejects_quality_gate_keys_outside_freeze() -> None:
    with pytest.raises(EvidenceExitRequirementResolutionError):
        _resolve(
            [
                _candidate(
                    input_contract={
                        "required_artifacts": ["screenshot"],
                        "quality_gate": {"min_screenshot_count": 1, "hard_fail": True},
                    }
                )
            ]
        )


# ---------------------------------------------------------------------------
# E01 条款 3.1：词汇封闭，表外取值拒绝
# ---------------------------------------------------------------------------


def test_resolver_rejects_unknown_artifact_type() -> None:
    with pytest.raises(EvidenceExitRequirementResolutionError):
        _resolve([_candidate(input_contract={"required_artifacts": ["hologram"]})])


def test_resolver_rejects_unknown_capture_role() -> None:
    with pytest.raises(EvidenceExitRequirementResolutionError):
        _resolve(
            [
                _candidate(
                    input_contract={
                        "required_artifacts": ["screenshot"],
                        "required_capture_roles": ["attempt_0"],
                    }
                )
            ]
        )


def test_resolver_rejects_duplicate_rule_ids() -> None:
    rules = [
        _candidate(rule_id="rule-1", rule_code="alioth_1"),
        _candidate(rule_id="rule-1", rule_code="alioth_2"),
    ]
    with pytest.raises(EvidenceExitRequirementResolutionError):
        _resolve(rules)


# ---------------------------------------------------------------------------
# E11 验收红线：只声明需求，不输出质量结论
# ---------------------------------------------------------------------------


def test_resolver_outputs_only_pending_declarations() -> None:
    result = _resolve(
        [
            _candidate(rule_id="rule-1", rule_code="alioth_1"),
            _candidate(
                rule_id="rule-2",
                rule_code="alioth_2",
                input_contract={"required_artifacts": ["screenshot", "dom"]},
            ),
        ]
    )
    assert result.requirements
    for requirement in result.requirements:
        assert requirement.status is EvidenceExitRequirementStatus.PENDING
        assert requirement.evidence_request_id is None
        assert requirement.resolution_manifest_id is None
        assert requirement.resolution_reason is None
    assert result.nonterminal_count == len(result.requirements)


# ---------------------------------------------------------------------------
# 确定性：输入乱序不影响需求集身份与匹配视图哈希
# ---------------------------------------------------------------------------


def test_resolver_is_deterministic_and_input_order_insensitive() -> None:
    rules = [
        _candidate(
            rule_id="rule-1",
            rule_code="alioth_1",
            input_contract={"required_artifacts": ["screenshot"]},
        ),
        _candidate(
            rule_id="rule-2",
            rule_code="alioth_2",
            input_contract={
                "required_artifacts": ["dom"],
                "required_capture_roles": ["before", "primary"],
            },
        ),
    ]
    forward = _resolve(rules)
    reversed_result = _resolve(list(reversed(rules)))
    assert forward.requirement_set_id == reversed_result.requirement_set_id
    assert forward.matcher_snapshot_hash == reversed_result.matcher_snapshot_hash
    assert forward == reversed_result


def test_resolver_builds_empty_set_without_candidates() -> None:
    result = _resolve([])
    assert result.requirements == ()
    assert result.revision == 1
    assert result.all_terminal


# ---------------------------------------------------------------------------
# 种子全量回归：62 条规则全部可收敛且落在冻结词汇内
# ---------------------------------------------------------------------------


def test_resolver_resolves_entire_seed_ruleset() -> None:
    seed = _seed_rules()
    rules = [
        _candidate(
            rule_id=f"rule-{index}",
            rule_code=rule_code or f"code-{index}",
            input_contract=contract,
        )
        for index, (rule_code, contract) in enumerate(seed, start=1)
    ]
    result = _resolve(rules)
    assert len(result.requirements) == len(seed)
    patterns = tuple(re.compile(item) for item in _E01_REQUIREMENT_ROLE_PATTERNS)
    for requirement in result.requirements:
        assert requirement.status is EvidenceExitRequirementStatus.PENDING
        assert set(requirement.required_artifact_types) <= _E01_ARTIFACT_TYPES
        assert requirement.required_capture_roles
        for role in requirement.required_capture_roles:
            assert role not in ("before", "after"), "需求简名必须已归一为供给角色"
            assert (
                role in _E01_SUPPLY_ROLES
                or role in _E01_REQUIREMENT_ROLE_NAMES
                or any(pattern.match(role) for pattern in patterns)
            ), f"角色 {role!r} 超出冻结词汇"
        assert requirement.min_screenshot_count in (0, 1)


# ---------------------------------------------------------------------------
# 域契约：quality_gate 两键入 payload，round-trip 不丢；不参与 requirement_id 身份
# ---------------------------------------------------------------------------


def _requirement(**overrides) -> ActiveSessionEvidenceRequirement:
    values = {
        "task_id": "task-1",
        "capture_id": "capture-1",
        "rule_id": "rule-1",
        "rule_code": "alioth_1",
        "required_artifact_types": ("screenshot",),
        "required_capture_roles": ("primary",),
        "status": EvidenceExitRequirementStatus.PENDING,
    }
    values.update(overrides)
    return build_active_session_evidence_requirement(**values)


def test_requirement_round_trip_keeps_quality_gate_fields() -> None:
    requirement = _requirement(min_screenshot_count=1, allow_empty_visible_text=False)
    assert requirement.to_dict()["min_screenshot_count"] == 1
    assert requirement.to_dict()["allow_empty_visible_text"] is False
    assert ActiveSessionEvidenceRequirement.from_dict(requirement.to_dict()) == requirement


def test_requirement_id_ignores_quality_gate_fields() -> None:
    relaxed = _requirement()
    strict = _requirement(min_screenshot_count=1, allow_empty_visible_text=False)
    assert relaxed.requirement_id == strict.requirement_id
    assert relaxed != strict


def test_revision_drift_check_covers_quality_gate() -> None:
    previous = build_evidence_exit_requirement_set(
        revision=1,
        task_id="task-1",
        session_id="session-1",
        capture_id="capture-1",
        matcher_snapshot_hash="matcher-1",
        requirements=(_requirement(),),
        created_at=NOW,
    )
    drifted = build_evidence_exit_requirement_set(
        revision=2,
        task_id="task-1",
        session_id="session-1",
        capture_id="capture-1",
        matcher_snapshot_hash="matcher-1",
        requirements=(_requirement(allow_empty_visible_text=False),),
        created_at=NOW,
    )
    with pytest.raises(EvidenceExitGatePersistenceError):
        _validate_requirement_revision(previous=previous, current=drifted)
