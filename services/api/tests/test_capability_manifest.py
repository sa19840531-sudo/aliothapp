from pathlib import Path

import pytest

from services.api.app.agents.bootstrap import build_default_capability_manifest
from services.api.app.agents.capability_manifest import (
    CapabilityKind,
    CapabilityManifestError,
    build_capability_manifest,
)
from services.api.app.agents.tools.registry import ToolRegistry, tool


def test_default_manifest_matches_runtime_tools_and_skill_files() -> None:
    manifest = build_default_capability_manifest()

    tool_items = {item.name: item for item in manifest if item.kind == CapabilityKind.TOOL}
    skill_items = {item.name: item for item in manifest if item.kind == CapabilityKind.SKILL}

    assert set(tool_items) == {
        "app_cloud_back_tool",
        "app_cloud_back_twice_tool",
        "app_screenshot_tool",
        "app_tap_from_profile_tool",
        "app_tap_tool",
        "candidate_rules_tool",
        "deterministic_assertion_tool",
        "exploration_closure_tool",
        "get_action_execution_evidence_tool",
        "load_app_product_profile_tool",
        "page_capture_tool",
        "page_detection_tool",
        "qr_link_validation_tool",
        "record_rule_execution_tool",
        "record_rule_executions_batch_tool",
        "rule_judge_batch_tool",
        "rule_judge_tool",
        "report_generation_tool",
        "report_publish_tool",
        "sensitive_word_detection_tool",
    }
    assert "get_current_time" not in tool_items
    assert "record_conclusion_placeholder" not in tool_items
    assert set(skill_items) == {
        "ad-rule",
        "app-product-capability",
        "app-product-orchestration",
        "app-script-rhythm",
        "app-workflow",
        "evidence-report",
        "green-content-safety",
        "green-process-evidence",
        "h5-explorer-workflow",
        "h5-payment-workflow",
        "h5-player-workflow",
        "input-risk-rule",
        "interaction-rule",
        "navigation-boundary-skill",
        "page-detection",
        "page-exploration",
        "payment-rule",
        "player-rule",
        "qc-workflow",
        "rule-matching",
        "task-planning",
        "text-rule",
        "visual-rule",
        "web-explorer-workflow",
    }
    assert tool_items["record_rule_execution_tool"].input_schema["type"] == "object"
    assert set(tool_items["candidate_rules_tool"].input_schema["required"]) == {
        "page_type",
        "page_capture_id",
    }
    assert "page_capture_id" in tool_items["record_rule_execution_tool"].input_schema["required"]
    assert tool_items["record_rule_execution_tool"].runtime_permission == "sensitive"
    assert (
        tool_items["record_rule_execution_tool"].implementation_ref
        == "services.api.app.agents.tools.rule_tools:record_rule_execution_tool"
    )
    assert skill_items["task-planning"].implementation_ref.endswith(
        "services/api/app/agents/skills/phase/task-planning/SKILL.md"
    )
    assert skill_items["app-workflow"].implementation_ref.endswith(
        "services/api/app/agents/skills/app-workflow/SKILL.md"
    )
    assert skill_items["web-explorer-workflow"].implementation_ref.endswith(
        "services/api/app/agents/skills/web-explorer-workflow/SKILL.md"
    )
    for name in (
        "h5-payment-workflow", "h5-player-workflow",
        "green-content-safety", "green-process-evidence",
    ):
        path = Path(skill_items[name].implementation_ref)
        assert path.is_file()
        assert f"name: {name}" in path.read_text(encoding="utf-8")
        assert skill_items[name].runtime_permission is None
        assert skill_items[name].input_schema == {}


def test_manifest_rejects_duplicate_names_across_tool_and_skill(tmp_path: Path) -> None:
    registry = ToolRegistry()

    @tool(name="duplicate", description="测试 Tool", category="测试", registry=registry)
    def duplicate() -> str:
        return "ok"

    skill_dir = tmp_path / "duplicate"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: duplicate\ndescription: 测试 Skill\n---\n",
        encoding="utf-8",
    )

    with pytest.raises(CapabilityManifestError, match="能力名称重复：duplicate"):
        build_capability_manifest(
            registry=registry,
            skills_root=tmp_path,
            reference_root=tmp_path,
        )


def test_manifest_rejects_skill_without_required_frontmatter(tmp_path: Path) -> None:
    skill_dir = tmp_path / "invalid"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# 缺少 frontmatter", encoding="utf-8")

    with pytest.raises(CapabilityManifestError, match="缺少有效 frontmatter"):
        build_capability_manifest(
            registry=ToolRegistry(),
            skills_root=tmp_path,
            reference_root=tmp_path,
        )
