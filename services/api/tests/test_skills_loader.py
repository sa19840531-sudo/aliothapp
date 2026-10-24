"""测试 Skill 加载器：能正确扫描 phase/、rule/ 与 qc-workflow/
下的 SKILL.md 并构建 ContextProvider。"""

import re
from pathlib import Path

import pytest
from agent_framework import AgentSession, ContextProvider, SessionContext

from services.api.app.agents.skills.loader import (
    APP_WORKFLOW_SKILL_VERSION,
    WEB_EXPLORER_WORKFLOW_SKILL_VERSION,
    AppWorkflowSkillConfigurationError,
    WebExplorerWorkflowSkillConfigurationError,
    build_skills_context_provider,
    build_web_explorer_context_provider,
    validate_app_workflow_skill,
    validate_web_explorer_workflow_skill_content,
)


def test_ad_rule_skill_absorbs_ad_popup_rule() -> None:
    """ad-popup-rule 已并入 ad-rule，旧目录不得复活。"""
    legacy_dir = Path("services/api/app/agents/skills/rule/ad-popup-rule")
    skill_path = Path("services/api/app/agents/skills/rule/ad-rule/SKILL.md")

    assert not legacy_dir.exists()
    assert skill_path.exists()
    content = skill_path.read_text(encoding="utf-8")

    assert "占位" not in content
    assert "category=ad" in content
    assert "跳过控件" in content
    assert "弹窗" in content
    assert "倒计时" in content
    assert "record_rule_execution_tool" in content


def test_text_rule_skill_is_production_guidance() -> None:
    """text-rule 不能继续停留在占位示例，需具备规则判定与写回说明。"""
    skill_path = (
        "services/api/app/agents/skills/rule/text-rule/SKILL.md"
    )
    content = open(skill_path, encoding="utf-8").read()

    assert "占位" not in content
    assert "qc_rule.category = text" in content
    assert "record_rule_execution_tool" in content
    assert "manual_review" in content
    assert "evidence_refs" in content


def test_player_rule_skill_is_production_guidance() -> None:
    """player-rule 需具备播放器规则判定与写回说明。"""
    skill_path = Path("services/api/app/agents/skills/rule/player-rule/SKILL.md")

    assert skill_path.exists()
    content = skill_path.read_text(encoding="utf-8")

    assert "占位" not in content
    assert "qc_rule.category = player" in content
    assert "播放进度" in content
    assert "续播" in content
    assert "贴片广告" in content
    assert "record_rule_execution_tool" in content
    assert "evidence_refs" in content


def test_tianshu_skills_do_not_embed_concrete_rule_codes() -> None:
    """规划、匹配、文本和播放器 Skill 不能成为规则编号的第二事实源。"""
    skill_paths = (
        Path("services/api/app/agents/skills/phase/task-planning/SKILL.md"),
        Path("services/api/app/agents/skills/rule/rule-matching/SKILL.md"),
        Path("services/api/app/agents/skills/rule/text-rule/SKILL.md"),
        Path("services/api/app/agents/skills/rule/player-rule/SKILL.md"),
    )

    for skill_path in skill_paths:
        content = skill_path.read_text(encoding="utf-8")
        assert re.search(r"\balioth_\d+\b", content) is None, skill_path


def test_text_rule_uses_capability_manifest_as_tool_source_of_truth() -> None:
    """Tool 上下线由 Capability Manifest 决定，Skill 不固化版本状态。"""
    content = Path(
        "services/api/app/agents/skills/rule/text-rule/SKILL.md"
    ).read_text(encoding="utf-8")

    assert "Capability Manifest" in content
    assert "首期未注册 Tool" not in content
    assert "V1 不得假设可用" not in content


def test_player_rule_documents_batch_writeback() -> None:
    content = Path(
        "services/api/app/agents/skills/rule/player-rule/SKILL.md"
    ).read_text(encoding="utf-8")

    assert "record_rule_executions_batch_tool" in content
    assert "execution_plan" in content


def test_task_planning_does_not_override_database_execution_priority() -> None:
    content = Path(
        "services/api/app/agents/skills/phase/task-planning/SKILL.md"
    ).read_text(encoding="utf-8")

    assert "sort_order" in content
    assert "severity" in content
    assert "execution_plan" in content


def test_build_skills_context_provider_returns_context_provider() -> None:
    provider = build_skills_context_provider()

    assert isinstance(provider, ContextProvider)


def test_build_skills_context_provider_accepts_single_sub_dir() -> None:
    provider = build_skills_context_provider(sub_dirs=("phase",))

    assert isinstance(provider, ContextProvider)


def test_build_skills_context_provider_accepts_qc_workflow_sub_dir() -> None:
    """qc-workflow 是新增的第三个 Skill 分类，需单独验证 loader 能正确扫描到。"""
    provider = build_skills_context_provider(sub_dirs=("qc-workflow",))

    assert isinstance(provider, ContextProvider)


def test_build_skills_context_provider_accepts_app_workflow_sub_dir() -> None:
    """App Workflow Skill 必须能按独立目录挂载给后续 App Orchestrator。"""
    provider = build_skills_context_provider(sub_dirs=("app-workflow",))

    assert isinstance(provider, ContextProvider)


def test_app_workflow_skill_has_required_compatible_version() -> None:
    skill_path = validate_app_workflow_skill()

    assert skill_path.name == "SKILL.md"
    assert APP_WORKFLOW_SKILL_VERSION in skill_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "content",
    [
        None,
        "---\nname: app-workflow\ndescription: invalid\nmetadata:\n  version: '0.9.0'\n---\n",
        "---\nname: wrong-workflow\ndescription: invalid\nmetadata:\n  version: '1.0.0'\n---\n",
    ],
)
def test_app_workflow_skill_missing_or_incompatible_fails_closed(
    tmp_path: Path,
    content: str | None,
) -> None:
    skill_dir = tmp_path / "app-workflow"
    skill_dir.mkdir()
    if content is not None:
        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")

    with pytest.raises(AppWorkflowSkillConfigurationError):
        validate_app_workflow_skill(skill_dir)


def test_build_skills_context_provider_accepts_navigation_boundary_skill_sub_dir() -> None:
    """navigation-boundary-skill 需要可被单独扫描，避免新增目录后静默漏挂载。"""
    provider = build_skills_context_provider(sub_dirs=("navigation-boundary-skill",))

    assert isinstance(provider, ContextProvider)


async def test_read_only_skill_tools_do_not_require_approval_but_scripts_still_do() -> None:
    """只读 Skill 工具不应进入审批流，脚本执行仍必须保留审批。"""
    provider = build_skills_context_provider(sub_dirs=("qc-workflow",))
    session = AgentSession()
    context = SessionContext(input_messages=[])

    await provider.before_run(
        agent=object(),  # type: ignore[arg-type]
        session=session,
        context=context,
        state=session.state.setdefault(provider.source_id, {}),
    )

    tools_by_name = {tool.name: tool for tool in context.tools}

    assert tools_by_name["load_skill"].approval_mode == "never_require"
    assert tools_by_name["read_skill_resource"].approval_mode == "never_require"
    assert tools_by_name["run_skill_script"].approval_mode == "always_require"


def test_web_explorer_workflow_skill_is_discoverable_and_versioned() -> None:
    provider = build_web_explorer_context_provider()
    skill_path = Path(
        "services/api/app/agents/skills/web-explorer-workflow/SKILL.md"
    )

    assert isinstance(provider, ContextProvider)
    assert skill_path.exists()
    content = skill_path.read_text(encoding="utf-8")
    assert WEB_EXPLORER_WORKFLOW_SKILL_VERSION in content
    assert validate_web_explorer_workflow_skill_content(content) == content


@pytest.mark.parametrize(
    "content",
    [
        "",
        "---\nname: wrong\ndescription: invalid\nmetadata:\n  version: '1.0.0'\n---\n",
        "---\nname: web-explorer-workflow\ndescription: invalid\nmetadata:\n"
        "  version: '0.9.0'\n---\n",
    ],
)
def test_web_explorer_workflow_skill_fails_closed_when_invalid(content: str) -> None:
    with pytest.raises(WebExplorerWorkflowSkillConfigurationError):
        validate_web_explorer_workflow_skill_content(content)
