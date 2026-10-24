"""总控与证据 Skill 必须使用真实 Tool 字段传递证据。"""

import re
from pathlib import Path

_SKILLS_ROOT = Path(__file__).resolve().parents[1] / "app" / "agents" / "skills"


def _read(relative_path: str) -> str:
    return (_SKILLS_ROOT / relative_path).read_text(encoding="utf-8")


def test_qc_workflow_preserves_page_capture_artifact_ids() -> None:
    text = _read("qc-workflow/SKILL.md")
    assert "page_capture_tool" in text
    assert "artifact_ids" in text
    assert "evidence_artifact_ids" in text
    assert "相同参数" in text


def test_page_capture_skills_split_trigger_action_and_selector() -> None:
    workflow = _read("qc-workflow/SKILL.md")
    navigation = _read("navigation-boundary-skill/SKILL.md")

    for text in (workflow, navigation):
        assert 'trigger_action="click"' in text
        assert 'selector="text=关闭"' in text
        assert "不得把动作和目标拼" in text or "禁止把" in text


def test_evidence_report_maps_refs_to_record_tool_field() -> None:
    text = _read("phase/evidence-report/SKILL.md")
    assert "page_capture_tool" in text
    assert "candidate_rules_tool" in text
    assert "artifact_ids" in text
    assert "evidence_artifact_ids" in text
    assert "EVIDENCE_REQUIRED" in text
    assert "evidence_insufficient" in text
    assert "record_rule_execution_tool" in text
    assert "record_rule_executions_batch_tool" in text
    assert "report_generation_tool" in text
    assert re.search(r"\balioth_\d+\b", text, re.I) is None
    assert re.search(r"\b[A-Z]+-\d+\b", text) is None
