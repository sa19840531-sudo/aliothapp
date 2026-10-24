"""M0 冻结口径契约测试（任务表 E01/E02，作为 E03 的契约测试输出）。

冻结文档：
- docs/design/2026-08-10-m0-e01-evidence-role-value-table.md
- docs/design/2026-08-10-m0-e02-quality-status-authority.md

口径变更必须先修订冻结文档并重走三方评审，再同步本测试。
"""

import csv
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from services.api.app.application.capture_profiles import CaptureProfilePolicy
from services.api.app.domain.evidence import (
    ArtifactReference,
    EvidenceContractValidationError,
    EvidenceManifestReference,
    EvidenceQualityStatus,
    build_evidence_bundle_ready,
    build_evidence_manifest_reference,
)
from services.api.app.domain.interaction.enums import InteractionOperation, SurfaceKind
from services.api.app.domain.task.targets import TaskTargetPlatform

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SEED_CSV = _REPO_ROOT / "docs" / "data" / "alioth_first_ruleset_qc_rule_seed.csv"

# E01 取值表 2.1：供给角色（运行时产生）
FROZEN_SUPPLY_ROLES = frozenset(
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

# E01 取值表 2.2：需求角色（规则契约声明）
FROZEN_REQUIREMENT_ROLE_NAMES = frozenset(
    {"primary", "before", "after", "source", "refreshed", "landing", "fault"}
)
FROZEN_REQUIREMENT_ROLE_PATTERNS = (
    re.compile(r"^attempt_[1-9]\d*$"),
    re.compile(r"^location_[1-9]\d*$"),
    re.compile(r"^entry_[1-9]\d*$"),
)

# E01 取值表 2.3：持久层占位值
FROZEN_PLACEHOLDER_ROLES = frozenset({"observation", "supplemental"})

# E01 条款 3.2：命名文法
ROLE_GRAMMAR = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

# E01 附录 5：artifact_type 词汇基线（DB 13 值 + App 运行时 ui_tree，见缺陷 C1）
FROZEN_ARTIFACT_TYPES = frozenset(
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
ARTIFACT_TYPE_ALIASES = {
    "dom_snapshot": "dom",
    "network_log": "har",
    "network_events": "har",
}

# E02 条款 1.6：规则级 quality_gate 键集封闭
FROZEN_RULE_QUALITY_GATE_KEYS = frozenset({"min_screenshot_count", "allow_empty_visible_text"})


def _seed_input_contracts() -> list[dict]:
    contracts: list[dict] = []
    with _SEED_CSV.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            raw = (row.get("input_contract_json") or "").strip()
            if raw:
                contracts.append(json.loads(raw))
    assert contracts, "种子 CSV 未读到任何 input_contract_json"
    return contracts


def _is_frozen_requirement_role(value: str) -> bool:
    if value in FROZEN_REQUIREMENT_ROLE_NAMES:
        return True
    return any(pattern.match(value) for pattern in FROZEN_REQUIREMENT_ROLE_PATTERNS)


def _manifest(
    *,
    quality_status: EvidenceQualityStatus,
    quality_reason: str | None = None,
) -> EvidenceManifestReference:
    return build_evidence_manifest_reference(
        task_id="task-1",
        session_id="session-1",
        capture_id="capture-1",
        manifest_version=1,
        artifacts=(
            ArtifactReference(
                artifact_id="artifact-1",
                artifact_type="screenshot",
                role="after_action",
                storage_path="evidence/task-1/capture-1/screenshot.png",
                checksum="sha256:abc",
                size_bytes=128,
                content_type="image/png",
            ),
        ),
        quality_status=quality_status,
        quality_reason=quality_reason,
        observation_packet_ids=("packet-1",),
        created_at=NOW,
    )


# ---------------------------------------------------------------------------
# E02 条款 1.2：quality_status 取值域封闭为 pending/pass/fail
# ---------------------------------------------------------------------------


def test_e02_quality_status_domain_is_frozen() -> None:
    assert {member.value for member in EvidenceQualityStatus} == {"pending", "pass", "fail"}


def test_e02_fail_requires_quality_reason() -> None:
    with pytest.raises(EvidenceContractValidationError):
        _manifest(quality_status=EvidenceQualityStatus.FAIL, quality_reason=None)
    manifest = _manifest(quality_status=EvidenceQualityStatus.FAIL, quality_reason="截图缺失")
    assert manifest.quality_reason == "截图缺失"


def test_e02_only_pass_publishes_bundle_ready() -> None:
    ready = build_evidence_bundle_ready(
        _manifest(quality_status=EvidenceQualityStatus.PASS),
        occurred_at=NOW,
    )
    assert ready.manifest_version == 1
    for blocked in (
        _manifest(quality_status=EvidenceQualityStatus.PENDING),
        _manifest(quality_status=EvidenceQualityStatus.FAIL, quality_reason="截图缺失"),
    ):
        with pytest.raises(EvidenceContractValidationError):
            build_evidence_bundle_ready(blocked, occurred_at=NOW)


# ---------------------------------------------------------------------------
# E02 条款 1.7：四套同名词汇禁止互串 —— 清单解析器必须拒绝
# 覆盖度词汇（B：page_exploration_summary）与判定聚合词汇（C：page_inspection_memory）
# ---------------------------------------------------------------------------

_FOREIGN_QUALITY_WORDS = (
    # B：页面探索覆盖度
    "unknown",
    "in_progress",
    "needs_review",
    "complete",
    # C：单页判定聚合（与 B 重叠的除外）
    "no_applicable_rules",
    "warning",
    "skipped",
    "not_started",
)


@pytest.mark.parametrize("foreign_value", _FOREIGN_QUALITY_WORDS)
def test_e02_manifest_parser_rejects_foreign_vocabulary(foreign_value: str) -> None:
    payload = _manifest(quality_status=EvidenceQualityStatus.PASS).to_dict()
    payload["quality_status"] = foreign_value
    with pytest.raises(EvidenceContractValidationError):
        EvidenceManifestReference.from_dict(payload)


# ---------------------------------------------------------------------------
# E02 条款 1.6：规则级 quality_gate 是需求声明，键集封闭
# ---------------------------------------------------------------------------


def test_e02_seed_rule_quality_gate_keys_are_frozen() -> None:
    for contract in _seed_input_contracts():
        gate = contract.get("quality_gate") or {}
        assert set(gate) <= FROZEN_RULE_QUALITY_GATE_KEYS, f"超出冻结键集: {sorted(gate)}"


# ---------------------------------------------------------------------------
# E01 条款 3.1/3.2：证据角色词汇封闭，命名文法统一
# ---------------------------------------------------------------------------


def test_e01_frozen_role_vocabulary_matches_grammar() -> None:
    samples = ("attempt_1", "attempt_12", "location_1", "entry_3")
    for value in (
        *FROZEN_SUPPLY_ROLES,
        *FROZEN_REQUIREMENT_ROLE_NAMES,
        *FROZEN_PLACEHOLDER_ROLES,
        *samples,
    ):
        assert ROLE_GRAMMAR.match(value), f"角色 {value!r} 不满足命名文法"


def test_e01_capture_profiles_emit_only_frozen_supply_roles() -> None:
    roles: set[str] = set()
    operations = (None, *InteractionOperation)
    for surface in SurfaceKind:
        for operation in operations:
            profile = CaptureProfilePolicy.for_surface(surface, operation)
            roles.update({profile.before.role, profile.after.role})
    for platform in TaskTargetPlatform:
        profile = CaptureProfilePolicy.for_platform(platform)
        roles.update({profile.before.role, profile.after.role})
    probe = CaptureProfilePolicy.for_app_probe()
    roles.update({probe.before.role, probe.after.role})
    # 2026-08-11 实况更新（见 E01 取值表 2.1 注记）：App 探针按 D5「对齐白名单」处置
    # 改发 before_action/after_action，probe_* 暂无生产方、转预留。
    assert roles == {"before_action", "after_action"}
    assert roles <= FROZEN_SUPPLY_ROLES


def test_e01_artifact_reference_accepts_every_frozen_supply_role() -> None:
    for role in sorted(FROZEN_SUPPLY_ROLES):
        reference = ArtifactReference(
            artifact_id=f"artifact-{role}",
            artifact_type="screenshot",
            role=role,
            storage_path=f"evidence/task-1/capture-1/{role}.png",
            checksum="sha256:abc",
            size_bytes=128,
        )
        assert reference.role == role


# ---------------------------------------------------------------------------
# E01 取值表 2.2：种子规则声明的需求角色不得超出冻结词汇
# ---------------------------------------------------------------------------


def test_e01_seed_required_capture_roles_within_frozen_vocabulary() -> None:
    seen: set[str] = set()
    for contract in _seed_input_contracts():
        for role in contract.get("required_capture_roles") or ():
            seen.add(role)
            assert _is_frozen_requirement_role(role), f"种子规则使用了表外角色: {role!r}"
    assert "primary" in seen


# ---------------------------------------------------------------------------
# E01 附录 5：artifact_type 词汇基线（别名归一后封闭）
# ---------------------------------------------------------------------------


def test_e01_seed_artifact_vocabulary_within_frozen_types() -> None:
    for contract in _seed_input_contracts():
        declared = (
            *(contract.get("required_artifacts") or ()),
            *(contract.get("optional_artifacts") or ()),
        )
        for item in declared:
            normalized = ARTIFACT_TYPE_ALIASES.get(item, item)
            assert normalized in FROZEN_ARTIFACT_TYPES, f"种子规则使用了表外证据类型: {item!r}"


def test_e01_capture_profile_artifact_types_within_frozen_types() -> None:
    types: set[str] = set()
    operations = (None, *InteractionOperation)
    for surface in SurfaceKind:
        for operation in operations:
            profile = CaptureProfilePolicy.for_surface(surface, operation)
            types.update(profile.before.artifact_types)
            types.update(profile.after.artifact_types)
    probe = CaptureProfilePolicy.for_app_probe(
        required_artifact_types=("ui_tree",),
    )
    types.update(probe.before.artifact_types)
    types.update(probe.after.artifact_types)
    assert types <= FROZEN_ARTIFACT_TYPES
