"""页级小批请求里同页事实去重的回归。

背景：实测同一采集下各规则的 Observation Packet 有 94% 是完全相同的页面事实
（ui_snapshot / dom_summary / journey），批量请求逐条重发即成倍放大输入 token。
本文件锁定去重行为，以及"事实不一致就整体放弃去重"这条 fail-safe。
"""

from __future__ import annotations

from typing import Any

from services.api.app.agents.rule_judge.instructions import (
    build_rule_judge_system_instructions,
)
from services.api.app.application.observation_packet import ObservationPacket
from services.api.app.infrastructure.llm_gateway.semantic_rule_judge import (
    _build_batch_messages,
    _estimate_semantic_input_tokens,
    _shared_facts_for_batch,
    estimate_batch_input_tokens,
)

SHARED_FACTS: dict[str, Any] = {
    "ui_snapshot": {"elements": [{"id": f"e{i}", "text": f"元素{i}"} for i in range(40)]},
    "dom_summary": {"nodes": [f"节点{i}" for i in range(40)]},
    "journey": {"steps": [f"步骤{i}" for i in range(30)]},
    "visible_text": "页面可见文本",
}

# 真实 facts 里混着 packet_budget 这类规则级字段：每条规则各不相同，但只有几十字符。
# 早期实现按 facts 整体比较，被它一票否决，导致去重从未触发。
def facts_for(rule_code: str) -> dict[str, Any]:
    return {**SHARED_FACTS, "packet_budget": {"rule": rule_code, "remaining": 1024}}


def packet(rule_code: str, *, facts: dict[str, Any] | None = None) -> ObservationPacket:
    return ObservationPacket(
        observation_packet_id=f"packet-{rule_code}",
        task_id="task-1",
        page_capture_id="capture-1",
        rule_id=f"rule-{rule_code}",
        rule_code=rule_code,
        evidence_artifact_ids=("artifact-1",),
        packet_payload={
            "schema_version": "1.0",
            "identity": {"rule_code": rule_code},
            "rule": {"check_point": f"{rule_code} 的检查点"},
            "facts": facts_for(rule_code) if facts is None else facts,
        },
        quality_gate_status="pass",
        quality_gate_reason=None,
    )


def items(*packets: ObservationPacket) -> list[tuple[ObservationPacket, list[dict[str, Any]]]]:
    return [(item, []) for item in packets]


def _user_text(messages) -> str:
    return "\n".join(
        content.text or ""
        for content in messages[1].contents
        if getattr(content, "text", None)
    )


def test_identical_facts_are_extracted_once() -> None:
    batch = items(packet("alioth_1"), packet("alioth_2"), packet("alioth_3"))

    shared = _shared_facts_for_batch(batch)

    assert shared is not None
    # 页面级事实全部提取，规则级的 packet_budget 必须留在各自 packet 里
    assert set(shared) == set(SHARED_FACTS)
    assert "packet_budget" not in shared


def test_single_item_batch_keeps_its_own_facts() -> None:
    """只有一条时没有重复可去，保持原样避免多一个无谓的块。"""
    assert _shared_facts_for_batch(items(packet("alioth_1"))) is None


def test_divergent_key_is_excluded_but_others_still_shared() -> None:
    """有差异的键留在各自 packet，不能因为它一票否决掉其余共享键。"""
    divergent = items(
        packet("alioth_1"),
        packet("alioth_2", facts={**facts_for("alioth_2"), "visible_text": "另一页"}),
    )

    shared = _shared_facts_for_batch(divergent)

    assert shared is not None
    assert "visible_text" not in shared
    assert "ui_snapshot" in shared and "dom_summary" in shared


def test_all_keys_divergent_yields_no_shared_block() -> None:
    """没有任何共享键时不产出空块，退回各自携带完整 facts。"""
    assert (
        _shared_facts_for_batch(
            items(
                packet("alioth_1", facts={"visible_text": "A"}),
                packet("alioth_2", facts={"visible_text": "B"}),
            )
        )
        is None
    )


def test_missing_or_empty_facts_disables_extraction() -> None:
    assert _shared_facts_for_batch(items(packet("alioth_1"), packet("alioth_2", facts={}))) is None


def test_batch_message_sends_shared_facts_once_and_omits_per_rule_copies() -> None:
    messages = _build_batch_messages(
        items=items(packet("alioth_1"), packet("alioth_2"), packet("alioth_3"))
    )

    text = _user_text(messages)
    assert text.count("【同页共享页面事实】") == 1
    for code in ("alioth_1", "alioth_2", "alioth_3"):
        assert f"【规则 {code}】" in text
    # 大块页面事实全局只出现一次
    assert text.count('"ui_snapshot"') == 1
    assert text.count('"dom_summary"') == 1
    assert text.count('"journey"') == 1
    # 规则级字段仍逐条保留，不能被共享块吞掉
    assert text.count('"packet_budget"') == 3


def test_batch_message_keeps_rule_specific_payload_intact() -> None:
    messages = _build_batch_messages(items=items(packet("alioth_1"), packet("alioth_2")))

    text = _user_text(messages)
    assert "alioth_1 的检查点" in text
    assert "alioth_2 的检查点" in text
    assert '"allowed_evidence_refs": ["artifact-1"]' in text


def test_divergent_key_stays_with_each_rule() -> None:
    """差异键必须回到各自 packet，规则不能读丢自己的那份事实。"""
    messages = _build_batch_messages(
        items=items(
            packet("alioth_1"),
            packet("alioth_2", facts={**facts_for("alioth_2"), "visible_text": "另一页"}),
        )
    )

    text = _user_text(messages)
    assert text.count('"ui_snapshot"') == 1  # 共享
    assert "页面可见文本" in text  # alioth_1 自己的
    assert "另一页" in text  # alioth_2 自己的


def test_no_shared_keys_falls_back_to_full_packets() -> None:
    messages = _build_batch_messages(
        items=items(
            packet("alioth_1", facts={"visible_text": "A"}),
            packet("alioth_2", facts={"visible_text": "B"}),
        )
    )

    text = _user_text(messages)
    assert "【同页共享页面事实】" not in text
    assert '"visible_text": "A"' in text and '"visible_text": "B"' in text


def test_estimate_matches_deduped_shape_and_beats_per_rule_sum() -> None:
    """预算闸门必须用去重后的口径，否则去重后的批次仍会被误判成超预算。"""
    batch = items(packet("alioth_1"), packet("alioth_2"), packet("alioth_3"), packet("alioth_4"))

    deduped = estimate_batch_input_tokens(batch)
    # 改造前的口径：每条规则都完整累加一份 packet（含 facts）
    naive = sum(
        _estimate_semantic_input_tokens(item.packet_payload, tools) for item, tools in batch
    )

    assert deduped < naive / 2


def test_estimate_grows_when_a_key_stops_being_shared() -> None:
    """差异键回落到逐条携带，估算必须跟着变大，闸门才不会低估真实用量。"""
    shared_batch = items(packet("alioth_1"), packet("alioth_2"))
    divergent_batch = items(
        packet("alioth_1"),
        packet("alioth_2", facts={**facts_for("alioth_2"), "ui_snapshot": {"elements": []}}),
    )

    assert estimate_batch_input_tokens(divergent_batch) > estimate_batch_input_tokens(shared_batch)


def test_batch_instructions_explain_the_shared_block() -> None:
    """模型必须知道 facts 缺失是有意为之，否则会误判成证据不足。"""
    text = build_rule_judge_system_instructions(output_schema={}, batch=True)

    assert "【同页共享页面事实】" in text
    assert "不得因为 facts 缺失而判定证据不足" in text
