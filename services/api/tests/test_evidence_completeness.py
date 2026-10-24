"""E14 齐全性裁决器契约测试（EvidenceCompletenessAdjudicator）。

验收口径（任务表 E14 + E02 条款 1.1/1.6）：门禁与清单裁决读取同一需求集；
齐全性只在清单侧裁决一次，裁决器输出 pass/fail 两态并携带可核对的缺口明细。
口径变更必须先修订冻结文档并重走三方评审，再同步本测试。
"""

from datetime import UTC, datetime

import pytest

from services.api.app.application.evidence_completeness import (
    EvidenceCompletenessAdjudicator,
    RequirementShortfall,
)
from services.api.app.domain.evidence.contracts import (
    ArtifactReference,
    EvidenceQualityStatus,
)
from services.api.app.domain.exploration.evidence_exit_requirements import (
    EvidenceExitRequirementSet,
    EvidenceExitRequirementStatus,
    build_active_session_evidence_requirement,
    build_evidence_exit_requirement_set,
)

NOW = datetime(2026, 8, 11, 14, 0, tzinfo=UTC)


def _requirement(
    *,
    rule_id: str = "rule-1",
    rule_code: str = "alioth_1",
    artifact_types: tuple[str, ...] = ("screenshot",),
    roles: tuple[str, ...] = ("primary",),
    status: EvidenceExitRequirementStatus = EvidenceExitRequirementStatus.PENDING,
    min_screenshot_count: int = 0,
    allow_empty_visible_text: bool = True,
    resolution_manifest_id: str | None = None,
):
    return build_active_session_evidence_requirement(
        task_id="task-1",
        capture_id="capture-1",
        rule_id=rule_id,
        rule_code=rule_code,
        required_artifact_types=artifact_types,
        required_capture_roles=roles,
        status=status,
        min_screenshot_count=min_screenshot_count,
        allow_empty_visible_text=allow_empty_visible_text,
        resolution_manifest_id=resolution_manifest_id,
    )


def _set(*requirements) -> EvidenceExitRequirementSet:
    return build_evidence_exit_requirement_set(
        revision=1,
        task_id="task-1",
        session_id="session-1",
        capture_id="capture-1",
        matcher_snapshot_hash="matcher-1",
        requirements=requirements,
        created_at=NOW,
    )


def _artifact(
    *,
    artifact_type: str = "screenshot",
    role: str = "primary",
    size_bytes: int = 128,
) -> ArtifactReference:
    return ArtifactReference(
        artifact_id=f"artifact-{artifact_type}-{role}-{size_bytes}",
        artifact_type=artifact_type,
        role=role,
        storage_path=f"evidence/task-1/capture-1/{artifact_type}-{role}",
        checksum="sha256:abc",
        size_bytes=size_bytes,
    )


def _adjudicate(requirement_set, artifacts):
    return EvidenceCompletenessAdjudicator().adjudicate(
        requirement_set=requirement_set,
        artifacts=artifacts,
    )


def test_pass_when_all_requirements_covered() -> None:
    result = _adjudicate(
        _set(
            _requirement(
                artifact_types=("dom", "screenshot"),
                roles=("before_action", "primary"),
                min_screenshot_count=1,
                allow_empty_visible_text=False,
            )
        ),
        [
            _artifact(artifact_type="screenshot", role="primary"),
            _artifact(artifact_type="dom", role="before_action"),
            _artifact(artifact_type="visible_text", role="primary", size_bytes=64),
        ],
    )

    assert result.quality_status is EvidenceQualityStatus.PASS
    assert result.quality_reason is None
    assert result.shortfalls == ()


def test_fail_lists_missing_artifact_types_per_rule() -> None:
    result = _adjudicate(
        _set(_requirement(artifact_types=("dom", "har", "screenshot"))),
        [_artifact(artifact_type="screenshot")],
    )

    assert result.quality_status is EvidenceQualityStatus.FAIL
    (shortfall,) = result.shortfalls
    assert shortfall.rule_code == "alioth_1"
    assert shortfall.missing_artifact_types == ("dom", "har")
    assert "dom" in result.quality_reason
    assert "alioth_1" in result.quality_reason


def test_fail_lists_missing_capture_roles() -> None:
    result = _adjudicate(
        _set(_requirement(roles=("before_action", "primary"))),
        [_artifact(artifact_type="screenshot", role="primary")],
    )

    (shortfall,) = result.shortfalls
    assert shortfall.missing_capture_roles == ("before_action",)
    assert "before_action" in result.quality_reason


def test_fail_on_screenshot_gate_without_artifact_declaration() -> None:
    result = _adjudicate(
        _set(_requirement(artifact_types=("dom",), min_screenshot_count=1)),
        [_artifact(artifact_type="dom")],
    )

    (shortfall,) = result.shortfalls
    assert shortfall.screenshot_shortfall is True
    assert shortfall.missing_artifact_types == ()
    assert "截图数量不足" in result.quality_reason


def test_fail_when_visible_text_required_non_empty_but_empty() -> None:
    result = _adjudicate(
        _set(_requirement(allow_empty_visible_text=False)),
        [
            _artifact(artifact_type="screenshot"),
            _artifact(artifact_type="visible_text", size_bytes=0),
        ],
    )

    (shortfall,) = result.shortfalls
    assert shortfall.visible_text_empty is True
    assert "可见文本为空" in result.quality_reason


def test_fail_when_visible_text_required_non_empty_but_absent() -> None:
    result = _adjudicate(
        _set(_requirement(allow_empty_visible_text=False)),
        [_artifact(artifact_type="screenshot")],
    )

    assert result.quality_status is EvidenceQualityStatus.FAIL
    assert result.shortfalls[0].visible_text_empty is True


def test_pass_when_empty_visible_text_allowed() -> None:
    result = _adjudicate(
        _set(_requirement(allow_empty_visible_text=True)),
        [
            _artifact(artifact_type="screenshot"),
            _artifact(artifact_type="visible_text", size_bytes=0),
        ],
    )

    assert result.quality_status is EvidenceQualityStatus.PASS


def test_empty_requirement_set_passes() -> None:
    result = _adjudicate(_set(), [])

    assert result.quality_status is EvidenceQualityStatus.PASS
    assert result.quality_reason is None


def test_terminal_requirements_are_not_re_adjudicated() -> None:
    satisfied = _requirement(
        rule_id="rule-9",
        rule_code="alioth_9",
        artifact_types=("har",),
        status=EvidenceExitRequirementStatus.SATISFIED,
        resolution_manifest_id="manifest-9",
    )
    result = _adjudicate(
        _set(satisfied, _requirement()),
        [_artifact(artifact_type="screenshot")],
    )

    assert result.quality_status is EvidenceQualityStatus.PASS


def test_aggregate_fail_only_reports_broken_rules() -> None:
    result = _adjudicate(
        _set(
            _requirement(rule_id="rule-1", rule_code="alioth_1"),
            _requirement(
                rule_id="rule-2",
                rule_code="alioth_2",
                artifact_types=("har",),
            ),
        ),
        [_artifact(artifact_type="screenshot")],
    )

    assert result.quality_status is EvidenceQualityStatus.FAIL
    assert [item.rule_code for item in result.shortfalls] == ["alioth_2"]


def test_reason_caps_listed_shortfalls_but_counts_rest() -> None:
    requirements = tuple(
        _requirement(
            rule_id=f"rule-{index}",
            rule_code=f"alioth_{index}",
            artifact_types=("har",),
        )
        for index in range(1, 26)
    )
    result = _adjudicate(_set(*requirements), [])

    assert len(result.shortfalls) == 25
    assert "其余 5 条缺口略" in result.quality_reason


def test_adjudicator_rejects_foreign_inputs() -> None:
    with pytest.raises(TypeError):
        _adjudicate({"not": "a-set"}, [])
    with pytest.raises(TypeError):
        _adjudicate(_set(_requirement()), [{"artifact_type": "screenshot"}])


def test_shortfall_description_is_auditable() -> None:
    shortfall = RequirementShortfall(
        requirement_id="exit-requirement:abc",
        rule_code="alioth_7",
        missing_artifact_types=("dom",),
        missing_capture_roles=("before_action",),
        screenshot_shortfall=True,
        visible_text_empty=True,
    )

    text = shortfall.describe()
    assert text.startswith("alioth_7：")
    for fragment in ("dom", "before_action", "截图数量不足", "可见文本为空"):
        assert fragment in text
