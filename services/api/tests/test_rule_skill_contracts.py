"""本次指定的规则 Skill 只维护场景、Tool 编排和稳定边界。"""

import re
from pathlib import Path

from agent_framework import ContextProvider

from services.api.app.agents.skills.loader import build_skills_context_provider

_SKILLS_ROOT = Path(__file__).resolve().parents[1] / "app" / "agents" / "skills"
_CATEGORY_SKILLS = {
    "text-rule": {
        "category": "text",
        "scene_markers": ("敏感词", "品牌词"),
        "tools": (
            "sensitive_word_detection_tool",
            "brand_word_detection_tool",
            "knowledge_retrieval_tool",
            "rule_judge_tool",
            "record_rule_executions_batch_tool",
        ),
    },
    "visual-rule": {
        "category": "visual",
        "scene_markers": ("媒体", "多帧"),
        "tools": (
            "page_capture_tool",
            "deterministic_assertion_tool",
            "rule_judge_tool",
            "record_rule_executions_batch_tool",
        ),
    },
    "interaction-rule": {
        "category": "interaction",
        "scene_markers": ("链接", "状态变化"),
        "tools": (
            "page_capture_tool",
            "deterministic_assertion_tool",
            "qr_link_validation_tool",
            "rule_judge_tool",
            "record_rule_executions_batch_tool",
        ),
    },
    "ad-rule": {
        "category": "ad",
        "scene_markers": ("广告素材", "倒计时", "蒙层"),
        "tools": (
            "page_capture_tool",
            "deterministic_assertion_tool",
            "rule_judge_tool",
            "record_rule_executions_batch_tool",
        ),
    },
    "payment-rule": {
        "category": "payment",
        "scene_markers": ("价格展示", "收银台"),
        "tools": (
            "page_capture_tool",
            "deterministic_assertion_tool",
            "rule_judge_tool",
            "record_rule_executions_batch_tool",
        ),
    },
    "input-risk-rule": {
        "category": "input_risk",
        "scene_markers": ("已发布 UGC", "恶意链接"),
        "tools": (
            "page_capture_tool",
            "sensitive_word_detection_tool",
            "qr_link_validation_tool",
            "deterministic_assertion_tool",
            "rule_judge_tool",
            "record_rule_executions_batch_tool",
        ),
    },
    "player-rule": {
        "category": "player",
        "scene_markers": ("播放入口", "贴片广告"),
        "tools": (
            "page_capture_tool",
            "deterministic_assertion_tool",
            "rule_judge_tool",
            "record_rule_executions_batch_tool",
        ),
    },
}
_DATABASE_RULE_FIELDS = (
    "rule_id",
    "rule_code",
    "check_point",
    "steps",
    "input_contract",
    "judge_config",
    "manual_review_policy",
    "execution_route",
)
_GUIDANCE_EXPANSIONS = {
    "visual-rule": "布局几何与截断",
    "ad-rule": "关闭按钮可点性",
    "interaction-rule": "滚动与瞬时提示",
    "player-rule": "进度条",
}


def _read_skill(skill_name: str) -> tuple[str, str]:
    path = _SKILLS_ROOT / "rule" / skill_name / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
    assert match is not None, f"{skill_name} 缺少 frontmatter"
    return match.group(1), text


def test_rule_skills_have_valid_names_and_trigger_descriptions() -> None:
    for skill_name, expectation in _CATEGORY_SKILLS.items():
        frontmatter, text = _read_skill(skill_name)
        assert re.search(rf"^name:\s*{re.escape(skill_name)}$", frontmatter, re.M)
        description = re.search(r"^description:\s*(.+)$", frontmatter, re.M)
        assert description is not None and len(description.group(1).strip()) >= 40
        assert f"category={expectation['category']}" in text
        assert "candidate_rules_tool" in text


def test_rule_skills_describe_scenes_and_registered_tools() -> None:
    for skill_name, expectation in _CATEGORY_SKILLS.items():
        _, text = _read_skill(skill_name)
        for marker in expectation["scene_markers"]:
            assert marker in text, f"{skill_name} 缺少适用场景 {marker}"
        for tool_name in expectation["tools"]:
            assert tool_name in text, f"{skill_name} 缺少 Tool 指导 {tool_name}"


def test_rule_skills_read_database_contract_instead_of_copying_rules() -> None:
    for skill_name in _CATEGORY_SKILLS:
        _, text = _read_skill(skill_name)
        for field in _DATABASE_RULE_FIELDS:
            assert field in text, f"{skill_name} 未声明读取数据库字段 {field}"

        assert re.search(r"\balioth_\d+\b", text, re.I) is None
        assert re.search(r"\b[A-Z]+-\d+\b", text) is None
        assert "min_confidence =" not in text
        assert "min_screenshot_count =" not in text
        assert "| rule_code |" not in text
        assert "合并①" not in text
        assert "合并②" not in text
        assert "合并③" not in text


def test_category_safety_boundaries_remain_explicit() -> None:
    _, visual = _read_skill("visual-rule")
    _, interaction = _read_skill("interaction-rule")
    _, ad = _read_skill("ad-rule")
    _, payment = _read_skill("payment-rule")
    _, input_risk = _read_skill("input-risk-rule")

    assert "默认只读" in visual
    assert "单次最小触发" in interaction
    assert "绝不反复点击刷广告" in ad
    assert "绝不提交订单" in payment
    assert "绝不制造、输入或发布违规内容" in input_risk


def test_page_exploration_skills_disable_supplemental_capture() -> None:
    skill_paths = (
        _SKILLS_ROOT / "phase" / "page-detection" / "SKILL.md",
        _SKILLS_ROOT / "phase" / "evidence-report" / "SKILL.md",
        _SKILLS_ROOT / "rule" / "rule-matching" / "SKILL.md",
        *(
            _SKILLS_ROOT / "rule" / skill_name / "SKILL.md"
            for skill_name in _CATEGORY_SKILLS
        ),
    )

    for path in skill_paths:
        text = path.read_text(encoding="utf-8")
        assert "当前版本页面探索不主动调用 `page_capture_tool` 补采" in text, path

    combined = "\n".join(path.read_text(encoding="utf-8") for path in skill_paths)
    assert "证据不足且允许补采时" not in combined
    assert "先用 `page_capture_tool(mode=\"observe\")`" not in combined
    assert "时序或闪烁类场景仅在规则要求时用 `timed_capture`" not in combined


def test_rule_skill_guidance_expansions_are_conditional() -> None:
    for skill_name, section_title in _GUIDANCE_EXPANSIONS.items():
        _, text = _read_skill(skill_name)
        assert f"## {section_title}" in text
        section = text.split(f"## {section_title}", 1)[1].split("\n## ", 1)[0]
        assert "候选规则" in section
        assert "声明" in section
        assert "不得" in section


def test_rule_skills_are_discovered_by_loader() -> None:
    provider = build_skills_context_provider(sub_dirs=("rule",))
    assert isinstance(provider, ContextProvider)
