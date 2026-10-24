"""真实失败任务的JSON字段及原停止事实不能被逻辑终态掩盖。"""

import json
from pathlib import Path

import pytest

from services.api.app.infrastructure.db.page_graph_repository import _row_dict


def case():
    return json.loads(
        (Path(__file__).parent / "fixtures/h5_draining_viewer_3ad88563.json").read_text()
    )


@pytest.mark.parametrize("decoded", [False, True])
def test_saved_reason_codes_have_the_same_json_array_contract(decoded):
    reasons = case()["reason_codes"]
    row = {
        "reason_codes": json.loads(reasons) if decoded else reasons,
        "outcome_payload": '{"result":"blocked"}',
        "state_payload": '{"page_state":"popup"}',
    }
    result = _row_dict(row)
    assert result["reason_codes"] == json.loads(reasons)
    assert result["outcome_payload"] == row["outcome_payload"]
    assert result["state_payload"] == row["state_payload"]
    assert row["reason_codes"] == (json.loads(reasons) if decoded else reasons)


@pytest.mark.parametrize("value", ["bad-json", '{"error":"x"}', "[null,1,{}]", 42])
def test_invalid_reason_codes_remain_visible_as_a_projection_error(value):
    assert _row_dict({"reason_codes": value})["reason_codes"] == ["graph_reason_codes_invalid"]
