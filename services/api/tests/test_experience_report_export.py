"""导出文案与研发回复区域继续使用体验编辑器现有 content 协议。"""

import base64
import json
from datetime import UTC, datetime

from services.api.app.application.experience_report_export import (
    build_platform_issue,
    render_experience_report,
)


def test_verdict_note_is_not_a_suggestion_and_reply_marker_inside_note_stays_in_problem() -> None:
    issue = build_platform_issue(
        {
            "rule_name": "广告关闭入口检查",
            "check_point": "广告必须提供可操作的关闭入口",
            "machine_display_status_label": "未通过",
            "problem_desc": "关闭入口被遮挡",
            "human_verdict": "fail",
            "human_verdict_note": "研发回复：这里仍是判定备注",
        },
        issue_id=1,
        image_data_uris=[],
    )
    assert "规范：广告必须提供可操作的关闭入口" in issue["content"]
    assert "人工判定备注：研发回复：这里仍是判定备注" in issue["content"]
    assert "优化建议：需人工填写" in issue["content"]
    assert "优化建议：研发回复：" not in issue["content"]
    html = render_experience_report(
        title="质检报告", author="玉衡", date="2026-09-30", issues=[issue]
    )
    assert "人工判定备注：研发回复：这里仍是判定备注" in html
    assert 'class="text-lbl sug-lbl">研发回复</p>' in html
    assert '<div class="text-body"><p>待研发填写</p></div>' in html
    start = html.index('id="__edit_data__"')
    encoded = html[html.index(">", start) + 1 : html.index("</div>", start)]
    state = json.loads(base64.b64decode(encoded).decode("utf-8"))
    assert state["issues"][0]["content"] == issue["content"]
    assert set(state["issues"][0]) == set(issue)


def test_export_preserves_rule_contract_and_missing_audit_without_fabricated_advice():
    issue = build_platform_issue(
        {"rule_name": "外部链接新窗口跳转", "machine_display_status_label": "未通过",
         "human_verdict": "fail", "human_verdict_by": "actor-original",
         "human_verdict_at": datetime(2026, 9, 30, 1, 2, tzinfo=UTC)},
        issue_id=1, image_data_uris=["data:image/png;base64,YQ==", "data:image/png;base64,Yg=="],
        evidence_note="判定材料：已内嵌 2 张截图；原材料标识：a、b。",
    )
    assert "规范：未提供检查规范" in issue["content"]
    assert "规范：外部链接新窗口跳转" not in issue["content"]
    assert "确认人：actor-original；确认时间：2026-09-30 01:02:00+00:00" in issue["content"]
    assert "问题：本条规则，AI 判定为未通过" in issue["content"]
    assert "优化建议：需人工填写" in issue["content"]
    assert issue["images"] == ["data:image/png;base64,YQ==", "data:image/png;base64,Yg=="]
    html = render_experience_report(
        title="模拟导出报告", author="测试", date="2026-09-30", issues=[issue],
    )
    assert "确认人：actor-original" in html
    assert "原材料标识：a、b" in html
    assert "研发回复</p>" in html
    historical = build_platform_issue(
        {"rule_name": "旧记录", "human_verdict": "fail"}, issue_id=2, image_data_uris=[],
    )
    assert "确认人：未记录；确认时间：未记录" in historical["content"]
