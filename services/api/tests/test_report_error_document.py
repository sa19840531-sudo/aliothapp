"""旧错误页面结论的报告资格回归，历史机器记录和人工审计保持真实。"""

import copy
import json

import pytest

from services.api.app.application.report_execution_eligibility import qualify_report_execution
from services.api.app.application.report_insight import ReportConfirmationError
from services.api.tests.test_report_insight import FakeReportRepository, _row, _service

ERROR_TITLE = "hifans1.com | 520: Web server is returning an unknown error"
ERROR_TEXT = (
    "Web server is returning an unknown error Error code 520 "
    "Visit cloudflare.com for more information. "
    "Browser Working Cloudflare Working Host Error What happened?"
)


def _input(title=ERROR_TITLE, text=ERROR_TEXT):
    return {
        "schema_version": "rule-judge-input.v1",
        "page": {"title": title, "page_type": "content_detail", "page_capture_id": "cap-1"},
        "fact_summary": {"visible_text_excerpt": text, "visible_text_character_count": len(text)},
        "tool_results": [],
    }


class _TraceableReportRepository(FakeReportRepository):
    async def fetch_rule_execution(self, execution_id, **kwargs):
        result = await super().fetch_rule_execution(execution_id, **kwargs)
        if result:
            source = next(row for row in self.rows if row["execution_id"] == execution_id)
            result.update(
                judge_input=source.get("judge_input"),
                judge_strategy=source.get("judge_strategy"),
            )
        return result


@pytest.mark.parametrize("status", ["pass", "fail", "skipped", "manual_review"])
async def test_error_document_is_not_business_result_even_with_images_or_human_verdict(status):
    repository = _TraceableReportRepository()
    repository.rows = [_row(
        "alioth_30", status, category="input_risk", judge_input=_input(),
        evidence_artifact_ids=["real-shot", "real-dom"], human_verdict="pass",
        human_verdict_note="历史备注", human_verdict_by="历史操作人",
    )]
    original = copy.deepcopy(repository.rows)
    service = _service(repository)
    insight = await service.report_insight("task-1")
    item = insight["rule_items"][0]
    assert item["machine_display_status"] == item["display_status"] == "detect_error"
    assert item["execution_status"] == status
    assert item["human_verdict"] == "pass"
    assert item["human_verdict_note"] == "历史备注"
    assert item["human_verdict_by"] == "历史操作人"
    assert item["evidence_artifact_ids"] == ["real-shot", "real-dom"]
    assert item["missing_evidence"] == ["有效的被检业务正文"]
    assert "Cloudflare" in item["result_summary"]
    assert insight["qc_success_rate"] == 0
    assert insight["summary"]["completed_candidate_rules"] == 0
    assert insight["confirm_items"] == []
    with pytest.raises(ReportConfirmationError):
        await service.confirm_rule("task-1", "exec-alioth_30", verdict="pass")
    assert repository.updates == []
    _, exported = await service.export_html("task-1")
    assert "质检复核报告" in exported
    assert "本报告不代表检测已完整完成" in exported
    assert "规则alioth_30" not in exported
    assert repository.rows == original


@pytest.mark.parametrize("input_value", [
    None, "非JSON", {"page": {"title": ERROR_TITLE}}, _input("正常业务正文", ERROR_TEXT),
    _input(
        "Cloudflare 520: Web server is returning an unknown error 故障排查教程",
        "本文介绍 Cloudflare Error code 520 的排查步骤，先保存 Ray ID，再检查源站日志。",
    ),
])
def test_missing_frozen_facts_or_error_words_in_business_page_do_not_guess_history(input_value):
    row = _row("business", "pass", judge_input=input_value)
    assert qualify_report_execution(row) is row


@pytest.mark.parametrize("diagnostic", [
    {"judge_strategy": "rule_engine"},
    {"judge_strategy": "hybrid", "tool_results": [{"tool_name": "technical_error_text"}]},
])
async def test_explicit_error_page_diagnostic_rule_keeps_real_conclusion(diagnostic):
    repository = _TraceableReportRepository()
    judge_input = _input()
    judge_input["tool_results"] = diagnostic.get("tool_results", [])
    repository.rows = [_row(
        "technical-error", "fail", judge_input=json.dumps(judge_input),
        judge_strategy=diagnostic["judge_strategy"],
    )]
    insight = await _service(repository).report_insight("task-1")
    assert insight["rule_items"][0]["display_status"] == "failed"
    assert insight["qc_success_rate"] == 100


async def test_valid_business_result_remains_completed_alongside_error_document():
    repository = _TraceableReportRepository()
    repository.rows = [
        _row("valid-content", "pass", judge_input=_input("内容详情", "真实已发布业务正文")),
        _row("alioth_27", "pass", judge_input=json.dumps(_input())),
    ]
    insight = await _service(repository).report_insight("task-1")
    assert [item["display_status"] for item in insight["rule_items"]] == ["passed", "detect_error"]
    assert insight["summary"]["completed_candidate_rules"] == 1
    assert insight["qc_success_rate"] == 50


@pytest.mark.parametrize("payload", [{"source": "test"}, '{"source":"test"}', None,
                                    "非法JSON", "[]", '"text"'])
async def test_judge_input_serialization_does_not_crash_or_invent_source(payload):
    repository = _TraceableReportRepository()
    repository.rows = [_row("business", "manual_review", judge_input=payload)]
    before = copy.deepcopy(repository.rows)
    insight = await _service(repository).report_insight("task-1")
    assert insight["rule_items"][0]["execution_status"] == "manual_review"
    assert insight["qc_success_rate"] == 0
    assert repository.rows == before
