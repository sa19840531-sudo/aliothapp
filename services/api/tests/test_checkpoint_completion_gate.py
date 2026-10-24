"""检查点执行与绑定不能替代正式判定，也不能被页面完成率掩盖。"""

from dataclasses import replace

from services.api.tests.test_task_completion_gate import _ready_facts


def test_checkpoint_result_gap_blocks_gate_and_preserves_legacy_zero_gap_hash_material():
    facts = _ready_facts()
    assert "journey_result_gap_count" not in facts.to_dict()
    blocked = replace(facts, journey_result_gap_count=1)
    assert not blocked.gate_passed
    assert "journey_results_incomplete" in {reason.value for reason in blocked.block_reasons}
    assert blocked.to_dict()["journey_result_gap_count"] == 1
