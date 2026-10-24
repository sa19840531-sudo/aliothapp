"""共享结果封装的状态一致性；受控分项仅验证封装，不代表业务判法已完成。"""

import pytest

from services.api.app.application.green_rule_evidence import (
    GreenSubitem,
    evaluate_green_aggregate_context,
)
from services.api.tests.test_green_process_evidence import context
from services.api.tests.test_green_process_judge import process_case


@pytest.mark.parametrize("status", ["pass", "fail", "manual_review"])
def test_complete_subitems_do_not_keep_stale_insufficiency_metadata(monkeypatch, status):
    fixture, evidence = process_case()
    keys = fixture.rule["judge_config"]["green_rule_contract"]["subchecks"]
    items = tuple(GreenSubitem(key, status, "受控封装结论") for key in keys)
    monkeypatch.setattr(
        "services.api.app.application.green_process_judge.judge_green_process_subitems",
        lambda **_: items,
    )

    result = evaluate_green_aggregate_context(context(fixture, evidence))

    assert result.execution_status == status
    assert result.error_code is None
    assert "evidence_insufficient_code" not in result.wrapper_payload
    assert "证据不足" not in result.result_summary
    if status == "pass":
        assert result.problem_desc is None
        assert result.review_status is None and result.review_reason is None
        assert result.confidence == 1.0
    elif status == "manual_review":
        assert result.review_status == "pending"
        assert result.review_reason == "受控封装结论"
        assert result.confidence is None
    else:
        assert result.problem_desc == "受控封装结论"
        assert result.review_status is None
        assert result.confidence == 1.0


def test_proven_failure_keeps_unresolved_obligations_and_actual_review(monkeypatch):
    fixture, evidence = process_case()
    keys = fixture.rule["judge_config"]["green_rule_contract"]["subchecks"]
    items = (
        GreenSubitem(keys[0], "fail", "已确认反例"),
        *(GreenSubitem(key, "evidence_insufficient", "仍缺完整观察", ("full_window",))
          for key in keys[1:]),
    )
    monkeypatch.setattr(
        "services.api.app.application.green_process_judge.judge_green_process_subitems",
        lambda **_: items,
    )

    result = evaluate_green_aggregate_context(context(fixture, evidence))

    assert result.execution_status == "fail"
    assert result.problem_desc == "已确认反例"
    assert result.review_status == "pending"
    assert "full_window" in result.review_reason
    assert result.error_code == "GREEN_NATIVE_PROCESS_FACTS_MISSING"
    assert result.wrapper_payload["green"]["missing_evidence"] == ["full_window"]
