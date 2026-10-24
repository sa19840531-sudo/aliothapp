"""循环护栏：连续重复之外，还要挡住两个动作之间的交替空转。"""

from __future__ import annotations

from services.api.app.agents.runtime.working_memory import WorkingMemory


def _feed(memory: WorkingMemory, signatures: list[str]) -> None:
    for item in signatures:
        memory.recent_call_signatures.append(("click", item))


def test_consecutive_repeat_still_detected() -> None:
    """原有的连续重复判定不得回退。"""
    memory = WorkingMemory()
    _feed(memory, ["A"] * 8)

    assert memory.would_be_stuck("click", "A", window=8) is True


def test_alternating_two_actions_are_detected() -> None:
    """A,B,A,B… 交替空转要判成卡住。

    实测 91短视频（DX-015）在两个底栏 Tab 之间来回点了三十多次：
    每次都真的换了路由，但四个路由的可见文字有四成是同一堵广告墙，
    页型始终判成 home，护栏一次都没触发，预算全耗在两页之间。
    """
    memory = WorkingMemory()
    _feed(memory, ["T3", "T5"] * 4)

    assert memory.would_be_stuck("click", "T3", window=8) is True


def test_alternating_not_flagged_for_small_window() -> None:
    """窗口小于 4 时不启用，「点击 → 关弹窗 → 再点击」是正常往返。"""
    memory = WorkingMemory()
    _feed(memory, ["A", "X", "A"])

    assert memory.would_be_stuck("click", "X", window=3) is False


def test_three_distinct_actions_are_not_stuck() -> None:
    """窗口内还有第三种动作时说明仍在推进，不得误杀。"""
    memory = WorkingMemory()
    _feed(memory, ["A", "B", "C", "A", "B", "C", "A", "B"])

    assert memory.would_be_stuck("click", "C", window=8) is False


def test_single_occurrence_partner_is_not_stuck() -> None:
    """两种动作里有一种只出现一次，不算交替循环。"""
    memory = WorkingMemory()
    _feed(memory, ["A", "A", "A", "A", "A", "A", "A", "B"])

    assert memory.would_be_stuck("click", "A", window=8) is False
