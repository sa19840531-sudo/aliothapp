"""ad 类数据库规则判定场景回归。

具体判断标准由数据库规则管理，运行时由 Main Agent / deterministic-assertion
按候选规则判定。本模块把当前 seed 规则**编码为参考判定函数**（可执行规格），再用模拟的
需求场景（广告证据组合 → 预期结论）逐分支验证：

- 决策表对每类证据组合都有唯一、无矛盾的结论（分支完备、可机械执行）；
- 7 态中确定性可达的 5 态（pass / fail / skipped / manual_review /
  evidence_insufficient）都有场景覆盖，且每条规则至少覆盖 pass/fail/skipped；
- B 模式不变量成立：触发 / 注入预算 ≤1、默认不点的规则（alioth_16/18/19/24/25）
  零触发、alioth_24 的疑似诱导元素任何情况下不点击；
- hybrid 规则把 Main Agent 的视觉 / 时序结论作为输入证据建模，验证的是
  **确定性前置 + 置信度门禁 + 汇总**逻辑（VLM 判定质量本身属端到端评测）。

参考判定函数同时可作为未来 deterministic-assertion Tool 的验收规格；置信度门禁
常量与种子 manual_review_policy 由专门测试锁定一致。若修改数据库规则判定语义，
必须同步修改此处参考实现；category Skill 只维护场景和 Tool 编排。
"""

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

_SEED_CSV = (
    Path(__file__).resolve().parents[3] / "docs" / "data" / "alioth_first_ruleset_qc_rule_seed.csv"
)

# 各规则人工复核置信度门禁（与种子 manual_review_policy.min_confidence 一致，
# 由 test_min_confidence_gates_match_seed 锁定）。
_MIN_CONFIDENCE = {
    "alioth_16": 0.9,
    "alioth_17": 0.9,
    "alioth_18": 0.82,
    "alioth_19": 0.75,
    "alioth_20": 0.75,
    "alioth_21": 0.82,
    "alioth_22": 0.75,
    "alioth_23": 0.82,
    "alioth_24": 0.82,
    "alioth_25": 0.82,
}

# ---------------------------------------------------------------------------
# 参考判定函数（每条分支对应当前数据库规则的一项判定要求）
# ---------------------------------------------------------------------------


def judge_alioth_16(evidence: dict[str, Any]) -> str:
    """广告素材比例：几何断言 → VLM 复核 → 置信度门禁。全程不点击。"""
    if not evidence["ad_media_present"]:
        return "skipped"
    if evidence["screenshot_count"] < 1:
        return "evidence_insufficient"  # quality_gate: min_screenshot_count = 1
    if evidence["ratio_assertion_ok"] is False:
        return "fail"  # 确定性几何断言失比，无需 VLM
    verdict = evidence["agent_verdict"]  # {"ok": bool, "confidence": float}
    if verdict["confidence"] < _MIN_CONFIDENCE["alioth_16"]:
        return "manual_review"
    return "pass" if verdict["ok"] else "fail"


def judge_alioth_17(evidence: dict[str, Any]) -> str:
    """广告与落地页一致：单次点击进落地页 → 断言跳转 → VLM 比对一致性。"""
    if not evidence["popup_ad_present"]:
        return "skipped"
    click = evidence.get("single_click_observation")
    if click is None:
        return "evidence_insufficient"
    if not click["navigated_to_landing"]:
        return "fail"  # 点击未前往落地页
    verdict = evidence["agent_consistency"]  # {"consistent": bool, "confidence": float}
    if verdict["confidence"] < _MIN_CONFIDENCE["alioth_17"]:
        return "manual_review"
    return "pass" if verdict["consistent"] else "fail"


def judge_alioth_18(evidence: dict[str, Any]) -> str:
    """广告倒计时：存在可识别的倒计时数字即通过。"""
    if not evidence["ad_present"]:
        return "skipped"
    countdown_number_present = evidence.get("countdown_number_present")
    if countdown_number_present is not True:
        return "evidence_insufficient"
    return "pass"


def judge_alioth_19(evidence: dict[str, Any]) -> str:
    """跳过按钮热区：可点击热区必须完整覆盖 UI 线框。不点击。"""
    if not evidence["skip_button_present"]:
        return "skipped"
    hotzone = evidence.get("hotzone")  # {"x","y","w","h"}
    ui_frame = evidence.get("ui_frame")
    if hotzone is None or ui_frame is None:
        return "evidence_insufficient"
    covers = (
        hotzone["x"] <= ui_frame["x"]
        and hotzone["y"] <= ui_frame["y"]
        and hotzone["x"] + hotzone["w"] >= ui_frame["x"] + ui_frame["w"]
        and hotzone["y"] + hotzone["h"] >= ui_frame["y"] + ui_frame["h"]
    )
    return "pass" if covers else "fail"


def judge_alioth_20(evidence: dict[str, Any]) -> str:
    """弹窗广告：操作前后可信截图不同即通过。"""
    if not evidence["popup_ad_present"]:
        return "skipped"
    before = evidence.get("before_screenshot")
    after = evidence.get("after_screenshot")
    if before is None or after is None:
        return "evidence_insufficient"
    return "pass" if before != after else "fail"


def judge_alioth_21(evidence: dict[str, Any]) -> str:
    """首页弹窗蒙层：操作前后可信截图不同即通过。"""
    if not evidence["overlay_present"]:
        return "skipped"
    before = evidence.get("before_screenshot")
    after = evidence.get("after_screenshot")
    if before is None or after is None:
        return "evidence_insufficient"
    return "pass" if before != after else "fail"


def judge_alioth_22(evidence: dict[str, Any]) -> str:
    """弹窗连续展示流畅性：单次关闭后下一个应立现，无卡顿 / 空窗。"""
    if not evidence["has_next_popup"]:
        return "skipped"  # 本就只有一个弹窗
    if not evidence["post_close_frames_present"]:
        return "evidence_insufficient"
    verdict = evidence["agent_verdict"]  # {"next_appeared_promptly": bool, "confidence"}
    if verdict["confidence"] < _MIN_CONFIDENCE["alioth_22"]:
        return "manual_review"
    return "pass" if verdict["next_appeared_promptly"] else "fail"


def judge_alioth_23(evidence: dict[str, Any]) -> str:
    """广告加载失败兜底（并入 alioth_26）：单次故障注入 → 观察兜底行为。"""
    if not evidence["ad_present"]:
        return "skipped"
    if not evidence["injection_available"]:
        return "manual_review"  # 注入能力不可用，不得用"未注入"硬判
    if not evidence["post_injection_frames_present"]:
        return "evidence_insufficient"
    verdict = evidence["agent_verdict"]  # {"fallback_ok": bool, "confidence"}
    if verdict["confidence"] < _MIN_CONFIDENCE["alioth_23"]:
        return "manual_review"
    return "pass" if verdict["fallback_ok"] else "fail"


def judge_alioth_24(evidence: dict[str, Any]) -> str:
    """启动广告：旅程后续进入首页即通过。"""
    if not evidence["startup_ad_present"]:
        return "skipped"
    page_types = evidence.get("page_types")
    if not page_types or "startup_ad" not in page_types:
        return "evidence_insufficient"
    startup_index = len(page_types) - 1 - page_types[::-1].index("startup_ad")
    later_pages = page_types[startup_index + 1 :]
    if "home" in later_pages:
        return "pass"
    return "fail" if later_pages else "evidence_insufficient"


def judge_alioth_25(evidence: dict[str, Any]) -> str:
    """播放页广告：截图或结构化快照存在广告特征即通过。"""
    if not evidence["pre_roll_expected"]:
        return "skipped"
    if evidence.get("ad_visible") is not True:
        return "evidence_insufficient"
    return "pass"


_JUDGES = {
    "alioth_16": judge_alioth_16,
    "alioth_17": judge_alioth_17,
    "alioth_18": judge_alioth_18,
    "alioth_19": judge_alioth_19,
    "alioth_20": judge_alioth_20,
    "alioth_21": judge_alioth_21,
    "alioth_22": judge_alioth_22,
    "alioth_23": judge_alioth_23,
    "alioth_24": judge_alioth_24,
    "alioth_25": judge_alioth_25,
}

# 默认不点的规则：任何场景零触发（B 模式"能不触发就不触发"的硬子集）。
_ZERO_TRIGGER_RULES = {"alioth_16", "alioth_18", "alioth_19", "alioth_24", "alioth_25"}

# ---------------------------------------------------------------------------
# 模拟需求场景（证据组合 → 预期结论；triggers_used 含单次点击与单次故障注入）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    rule_code: str
    expected: str
    triggers_used: int
    evidence: dict[str, Any] = field(default_factory=dict)


_SCENARIOS = [
    # ---- alioth_16 广告素材比例 ----
    Scenario(
        "比例断言与视觉复核均达标",
        "alioth_16",
        "pass",
        0,
        {
            "ad_media_present": True,
            "screenshot_count": 1,
            "ratio_assertion_ok": True,
            "agent_verdict": {"ok": True, "confidence": 0.95},
        },
    ),
    Scenario(
        "几何断言检出拉伸失比",
        "alioth_16",
        "fail",
        0,
        {"ad_media_present": True, "screenshot_count": 1, "ratio_assertion_ok": False},
    ),
    Scenario(
        "视觉复核高置信度判失比",
        "alioth_16",
        "fail",
        0,
        {
            "ad_media_present": True,
            "screenshot_count": 1,
            "ratio_assertion_ok": True,
            "agent_verdict": {"ok": False, "confidence": 0.93},
        },
    ),
    Scenario(
        "页面无广告媒体",
        "alioth_16",
        "skipped",
        0,
        {"ad_media_present": False},
    ),
    Scenario(
        "视觉结论低于门禁转人工",
        "alioth_16",
        "manual_review",
        0,
        {
            "ad_media_present": True,
            "screenshot_count": 1,
            "ratio_assertion_ok": True,
            "agent_verdict": {"ok": True, "confidence": 0.7},
        },
    ),
    Scenario(
        "截图缺失不满足质量门禁",
        "alioth_16",
        "evidence_insufficient",
        0,
        {"ad_media_present": True, "screenshot_count": 0},
    ),
    # ---- alioth_17 广告与落地页一致 ----
    Scenario(
        "单次点击进落地页且内容一致",
        "alioth_17",
        "pass",
        1,
        {
            "popup_ad_present": True,
            "single_click_observation": {"navigated_to_landing": True},
            "agent_consistency": {"consistent": True, "confidence": 0.95},
        },
    ),
    Scenario(
        "点击后未前往广告落地页",
        "alioth_17",
        "fail",
        1,
        {
            "popup_ad_present": True,
            "single_click_observation": {"navigated_to_landing": False},
        },
    ),
    Scenario(
        "落地页与广告素材内容无关",
        "alioth_17",
        "fail",
        1,
        {
            "popup_ad_present": True,
            "single_click_observation": {"navigated_to_landing": True},
            "agent_consistency": {"consistent": False, "confidence": 0.92},
        },
    ),
    Scenario(
        "页面无弹窗广告",
        "alioth_17",
        "skipped",
        0,
        {"popup_ad_present": False},
    ),
    Scenario(
        "一致性比对低置信度转人工",
        "alioth_17",
        "manual_review",
        1,
        {
            "popup_ad_present": True,
            "single_click_observation": {"navigated_to_landing": True},
            "agent_consistency": {"consistent": True, "confidence": 0.85},
        },
    ),
    # ---- alioth_18 广告倒计时数字存在 ----
    Scenario(
        "页面存在倒计时数字",
        "alioth_18",
        "pass",
        0,
        {"ad_present": True, "countdown_number_present": True},
    ),
    Scenario(
        "当前页面不是广告场景",
        "alioth_18",
        "skipped",
        0,
        {"ad_present": False},
    ),
    Scenario(
        "未识别到倒计时数字",
        "alioth_18",
        "evidence_insufficient",
        0,
        {"ad_present": True, "countdown_number_present": False},
    ),
    # ---- alioth_19 跳过按钮可点击区域 ----
    Scenario(
        "热区完整覆盖线框",
        "alioth_19",
        "pass",
        0,
        {
            "skip_button_present": True,
            "hotzone": {"x": 8, "y": 8, "w": 84, "h": 44},
            "ui_frame": {"x": 10, "y": 10, "w": 80, "h": 40},
        },
    ),
    Scenario(
        "热区小于线框存在点不中区域",
        "alioth_19",
        "fail",
        0,
        {
            "skip_button_present": True,
            "hotzone": {"x": 20, "y": 15, "w": 40, "h": 30},
            "ui_frame": {"x": 10, "y": 10, "w": 80, "h": 40},
        },
    ),
    Scenario(
        "广告不允许跳过",
        "alioth_19",
        "skipped",
        0,
        {"skip_button_present": False},
    ),
    Scenario(
        "几何证据缺失",
        "alioth_19",
        "evidence_insufficient",
        0,
        {"skip_button_present": True, "hotzone": None, "ui_frame": None},
    ),
    # ---- alioth_20 弹窗广告操作前后截图变化 ----
    Scenario(
        "弹窗操作前后截图变化",
        "alioth_20",
        "pass",
        1,
        {
            "popup_ad_present": True,
            "before_screenshot": "before",
            "after_screenshot": "after",
        },
    ),
    Scenario(
        "弹窗操作前后截图相同",
        "alioth_20",
        "fail",
        1,
        {
            "popup_ad_present": True,
            "before_screenshot": "same",
            "after_screenshot": "same",
        },
    ),
    Scenario(
        "页面无弹窗广告",
        "alioth_20",
        "skipped",
        0,
        {"popup_ad_present": False},
    ),
    Scenario(
        "缺少操作后截图",
        "alioth_20",
        "evidence_insufficient",
        1,
        {"popup_ad_present": True, "before_screenshot": "before"},
    ),
    # ---- alioth_21 首页弹窗蒙层操作前后截图变化 ----
    Scenario(
        "蒙层操作前后截图变化",
        "alioth_21",
        "pass",
        1,
        {
            "overlay_present": True,
            "before_screenshot": "before",
            "after_screenshot": "after",
        },
    ),
    Scenario(
        "蒙层操作前后截图相同",
        "alioth_21",
        "fail",
        1,
        {
            "overlay_present": True,
            "before_screenshot": "same",
            "after_screenshot": "same",
        },
    ),
    Scenario(
        "全屏广告无蒙层设计",
        "alioth_21",
        "skipped",
        0,
        {"overlay_present": False},
    ),
    Scenario(
        "缺少蒙层操作后截图",
        "alioth_21",
        "evidence_insufficient",
        1,
        {"overlay_present": True, "before_screenshot": "before"},
    ),
    # ---- alioth_22 弹窗连续展示流畅性 ----
    Scenario(
        "关闭后下一弹窗立现",
        "alioth_22",
        "pass",
        1,
        {
            "has_next_popup": True,
            "post_close_frames_present": True,
            "agent_verdict": {"next_appeared_promptly": True, "confidence": 0.9},
        },
    ),
    Scenario(
        "关闭后长时间空窗才载入",
        "alioth_22",
        "fail",
        1,
        {
            "has_next_popup": True,
            "post_close_frames_present": True,
            "agent_verdict": {"next_appeared_promptly": False, "confidence": 0.88},
        },
    ),
    Scenario(
        "本就只有一个弹窗",
        "alioth_22",
        "skipped",
        0,
        {"has_next_popup": False},
    ),
    Scenario(
        "关闭后帧序列缺失",
        "alioth_22",
        "evidence_insufficient",
        1,
        {"has_next_popup": True, "post_close_frames_present": False},
    ),
    # ---- alioth_23 广告加载失败兜底（并入 alioth_26）----
    Scenario(
        "弹窗场景注入后自动关闭无空白蒙层",
        "alioth_23",
        "pass",
        1,
        {
            "ad_present": True,
            "injection_available": True,
            "post_injection_frames_present": True,
            "agent_verdict": {"fallback_ok": True, "confidence": 0.9},
        },
    ),
    Scenario(
        "贴片场景注入后不播正片卡在空白",
        "alioth_23",
        "fail",
        1,
        {
            "ad_present": True,
            "injection_available": True,
            "post_injection_frames_present": True,
            "agent_verdict": {"fallback_ok": False, "confidence": 0.9},
        },
    ),
    Scenario(
        "页面无广告可注入",
        "alioth_23",
        "skipped",
        0,
        {"ad_present": False},
    ),
    Scenario(
        "注入能力不可用不硬判",
        "alioth_23",
        "manual_review",
        0,
        {"ad_present": True, "injection_available": False},
    ),
    Scenario(
        "注入后帧序列缺失",
        "alioth_23",
        "evidence_insufficient",
        1,
        {
            "ad_present": True,
            "injection_available": True,
            "post_injection_frames_present": False,
        },
    ),
    # ---- alioth_24 启动广告进入首页 ----
    Scenario(
        "启动广告后进入首页",
        "alioth_24",
        "pass",
        0,
        {
            "startup_ad_present": True,
            "page_types": ["startup_ad", "home"],
        },
    ),
    Scenario(
        "启动广告后进入其他已知页面",
        "alioth_24",
        "fail",
        0,
        {
            "startup_ad_present": True,
            "page_types": ["startup_ad", "content_list"],
        },
    ),
    Scenario(
        "无启动广告",
        "alioth_24",
        "skipped",
        0,
        {"startup_ad_present": False},
    ),
    Scenario(
        "缺少启动广告后续旅程",
        "alioth_24",
        "evidence_insufficient",
        0,
        {"startup_ad_present": True, "page_types": ["startup_ad"]},
    ),
    # ---- alioth_25 播放页广告存在 ----
    Scenario(
        "截图或结构化快照识别到广告",
        "alioth_25",
        "pass",
        0,
        {"pre_roll_expected": True, "ad_visible": True},
    ),
    Scenario(
        "播放页无前贴广告",
        "alioth_25",
        "skipped",
        0,
        {"pre_roll_expected": False},
    ),
    Scenario(
        "未识别到广告特征",
        "alioth_25",
        "evidence_insufficient",
        0,
        {"pre_roll_expected": True, "ad_visible": False},
    ),
]

# ---------------------------------------------------------------------------
# 场景断言
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scenario", _SCENARIOS, ids=lambda s: f"{s.rule_code}-{s.scenario_id}")
def test_scenario_matches_documented_decision(scenario: Scenario) -> None:
    """每个模拟需求场景的结论必须与当前数据库规则一致。"""
    assert _JUDGES[scenario.rule_code](scenario.evidence) == scenario.expected


def test_scenarios_cover_all_deterministically_reachable_statuses() -> None:
    """确定性分支可达的 5 态必须全部有场景覆盖（warning/error 非决策表产物）。"""
    covered = {scenario.expected for scenario in _SCENARIOS}
    assert covered == {"pass", "fail", "skipped", "manual_review", "evidence_insufficient"}


def test_every_rule_covers_its_reachable_outcomes() -> None:
    """每条规则覆盖当前判断口径可达的关键结论。"""
    for rule_code in _JUDGES:
        outcomes = {s.expected for s in _SCENARIOS if s.rule_code == rule_code}
        expected = {"pass", "skipped"}
        if rule_code not in {"alioth_18", "alioth_25"}:
            expected.add("fail")
        assert expected <= outcomes, f"{rule_code} 场景覆盖不足: {outcomes}"


def test_single_trigger_or_injection_budget_never_exceeded() -> None:
    """B 模式硬边界：任何场景的单次点击 + 故障注入总预算不得超过 1。"""
    assert all(scenario.triggers_used <= 1 for scenario in _SCENARIOS)


def test_static_rules_never_consume_trigger() -> None:
    """默认不点的规则（16/18/19/24/25）任何场景零触发。"""
    for scenario in _SCENARIOS:
        if scenario.rule_code in _ZERO_TRIGGER_RULES:
            assert scenario.triggers_used == 0, (
                f"{scenario.rule_code}-{scenario.scenario_id} 消耗了触发预算"
            )


def test_min_confidence_gates_match_seed() -> None:
    """参考判定的置信度门禁必须与种子 manual_review_policy.min_confidence 一致。"""
    with _SEED_CSV.open(newline="", encoding="utf-8") as file:
        seed = {row["rule_code"]: row for row in csv.DictReader(file)}
    for rule_code, gate in _MIN_CONFIDENCE.items():
        policy = json.loads(seed[rule_code]["manual_review_policy_json"])
        assert gate == policy["min_confidence"], (
            f"{rule_code} 门禁 {gate} 与种子 {policy['min_confidence']} 不一致"
        )
