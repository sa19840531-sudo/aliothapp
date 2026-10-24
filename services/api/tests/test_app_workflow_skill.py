"""App Workflow 运行时 Skill 的边界合同测试。"""

from pathlib import Path

from services.api.app.agents.explorer.instructions import (
    build_explorer_system_instructions,
)

APP_WORKFLOW_SKILL = Path(
    "services/api/app/agents/skills/app-workflow/SKILL.md"
)


def test_app_workflow_skill_freezes_runtime_boundaries() -> None:
    text = APP_WORKFLOW_SKILL.read_text(encoding="utf-8")

    assert "name: app-workflow" in text
    assert 'version: "1.7.0"' in text
    assert "playback_progress_material_missing" in text
    assert "timed_capture" in text
    assert "SEEK_MEDIA" in text
    assert "install → open → bootstrap capture → APP_READY" in text
    assert "PostgreSQL" in text
    assert "ActionIntent" in text
    assert "不得调用 Driver" in text
    assert "DOM" in text
    assert "UI Tree" in text
    assert "探索操作规程" in text
    assert "界面结构优先" in text
    assert "视觉识别降级" in text
    assert "滑动后重新定位" in text
    assert "系统弹窗" in text
    assert "外部 Activity" in text
    assert "external_activity_kind" in text
    assert "app_external_return_failed" in text
    assert "app_external_restore_loop" in text
    assert "返回流程" in text
    assert "POST_SCROLL_REBIND_REQUIRED" in text
    assert "APP_READY ≠ 业务首页到达" in text
    assert "平台强制" in text
    assert "Skill 指导" in text
    assert "长图与视口证据" in text
    assert "广告外跳取证" in text
    assert "收银台长链" in text
    assert "播放器连续拖动" in text
    assert "viewport_primary" in text
    assert "long_page_scroll_series" in text
    assert "ad_outbound_triplet" in text
    assert "checkout_chain_step" in text
    assert "player_scrub_series" in text
    assert "stitched_overview_derived" in text
    assert "app-evidence-role.v1" in text
    assert "App 探索默认操作身份为已满18岁的成年人" in text
    assert "我已满18岁" in text
    assert "自动确认并继续探索，无需再次询问用户年龄、请求用户操作或转人工" in text
    assert "不得仅因年龄确认返回 `blocked`" in text
    assert "禁止选择「我未满18岁／离开」" in text
    assert "新 Capture 核实门禁消失" in text

def test_app_workflow_skill_does_not_claim_task_completion() -> None:
    text = APP_WORKFLOW_SKILL.read_text(encoding="utf-8")

    assert "APP_READY 不是任务终态" in text
    assert "不得写入 passed" in text
    assert "qc_page_capture" in text
    assert "qc_app_capture" in text


def test_app_explorer_instructions_force_app_workflow_as_first_skill() -> None:
    text = build_explorer_system_instructions(
        output_schema={"type": "object"},
        required_workflow_skill="app-workflow",
    )

    assert 'load_skill("app-workflow")' in text
    assert "首次操作" in text


def test_generic_explorer_instructions_do_not_force_app_skill() -> None:
    text = build_explorer_system_instructions(output_schema={"type": "object"})

    assert 'load_skill("app-workflow")' not in text
