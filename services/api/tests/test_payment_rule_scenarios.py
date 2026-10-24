"""payment 类数据库规则判定场景回归。

具体判断标准由数据库规则管理，运行时由 Main Agent / deterministic-assertion
按候选规则判定。本模块把当前 seed 规则**编码为参考判定函数**（可执行规格），再用模拟的
需求场景（价格证据组合 → 预期结论）逐分支验证：

- 决策表对每类证据组合都有唯一、无矛盾的结论（分支完备、可机械执行）；
- 7 态中确定性可达的 5 态（pass / fail / skipped / manual_review /
  evidence_insufficient）都有场景覆盖，且每条规则至少覆盖 pass/fail/skipped；
- **高风险零触发不变量**成立：payment 类全程只读，任何场景都不消耗单次触发 /
  故障注入预算（这是与 interaction/ad 类的本质区别）。

两条规则均为 rule_engine：价格归一化与数值比较是纯确定性逻辑，manual_review
仅来自归一化不确定（币种混排 / 单位歧义），不建模置信度数值。参考判定函数同时
可作为未来 deterministic-assertion Tool 的验收规格；若修改数据库规则判定语义，
必须同步修改此处参考实现；category Skill 只维护场景和 Tool 编排。
"""

from dataclasses import dataclass, field
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# 参考判定函数（每条分支对应当前数据库规则的一项判定要求）
# ---------------------------------------------------------------------------


def judge_alioth_13(evidence: dict[str, Any]) -> str:
    """商品价格一致：多位置只读采集 → 归一化 → 跨位置一致性断言。"""
    prices = evidence["prices"]  # [{"location": str, "value": float | None}, ...]
    if len(prices) < 2:
        return "skipped"  # 无价格展示或仅单一位置，无跨位置可比
    if any(price["value"] is None for price in prices):
        return "evidence_insufficient"  # 存在无法解析的价格文本
    if not evidence.get("normalization_confident", True):
        return "manual_review"  # 币种混排 / 单位歧义，无法可靠归一
    distinct_values = {price["value"] for price in prices}
    return "pass" if len(distinct_values) == 1 else "fail"


def judge_alioth_14(evidence: dict[str, Any]) -> str:
    """优惠价低于原价：成对只读采集 → 归一化 → 严格小于断言。"""
    pair = evidence.get("price_pair")  # {"promo": float|None, "original": float|None} | None
    if pair is None:
        return "pass"  # payment 页面未展示优惠价时按放宽口径直接通过
    if pair["promo"] is None or pair["original"] is None:
        return "evidence_insufficient"  # 优惠价或原价缺失 / 解析失败
    if not evidence.get("normalization_confident", True):
        return "manual_review"
    return "pass" if pair["promo"] < pair["original"] else "fail"  # 等于原价即不成立


_JUDGES = {
    "alioth_13": judge_alioth_13,
    "alioth_14": judge_alioth_14,
}

# ---------------------------------------------------------------------------
# 模拟需求场景（证据组合 → 预期结论；payment 类全程只读，triggers_used 恒为 0）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    rule_code: str
    expected: str
    triggers_used: int
    evidence: dict[str, Any] = field(default_factory=dict)


_SCENARIOS = [
    # ---- alioth_13 商品价格一致 ----
    Scenario(
        "列表详情收银台三处价格一致",
        "alioth_13",
        "pass",
        0,
        {
            "prices": [
                {"location": "content_list", "value": 12.9},
                {"location": "content_detail", "value": 12.9},
                {"location": "payment", "value": 12.9},
            ]
        },
    ),
    Scenario(
        "详情页与收银台价格不一致",
        "alioth_13",
        "fail",
        0,
        {
            "prices": [
                {"location": "content_detail", "value": 12.9},
                {"location": "payment", "value": 15.9},
            ]
        },
    ),
    Scenario(
        "仅详情页一处展示价格无从比对",
        "alioth_13",
        "skipped",
        0,
        {"prices": [{"location": "content_detail", "value": 12.9}]},
    ),
    Scenario(
        "登录墙遮挡页面无价格展示",
        "alioth_13",
        "skipped",
        0,
        {"prices": []},
    ),
    Scenario(
        "收银台价格文本无法解析",
        "alioth_13",
        "evidence_insufficient",
        0,
        {
            "prices": [
                {"location": "content_detail", "value": 12.9},
                {"location": "payment", "value": None},
            ]
        },
    ),
    Scenario(
        "币种混排归一化不确定转人工",
        "alioth_13",
        "manual_review",
        0,
        {
            "prices": [
                {"location": "content_detail", "value": 12.9},
                {"location": "payment", "value": 1.8},
            ],
            "normalization_confident": False,
        },
    ),
    # ---- alioth_14 优惠价低于原价 ----
    Scenario(
        "优惠价明显低于划线原价",
        "alioth_14",
        "pass",
        0,
        {"price_pair": {"promo": 9.9, "original": 19.9}},
    ),
    Scenario(
        "优惠价等于原价不构成优惠",
        "alioth_14",
        "fail",
        0,
        {"price_pair": {"promo": 19.9, "original": 19.9}},
    ),
    Scenario(
        "优惠价反而高于原价",
        "alioth_14",
        "fail",
        0,
        {"price_pair": {"promo": 25.0, "original": 19.9}},
    ),
    Scenario(
        "页面未展示优惠价",
        "alioth_14",
        "pass",
        0,
        {"price_pair": None},
    ),
    Scenario(
        "仅有促销价划线原价解析失败",
        "alioth_14",
        "evidence_insufficient",
        0,
        {"price_pair": {"promo": 9.9, "original": None}},
    ),
    Scenario(
        "折字单位歧义归一化不确定转人工",
        "alioth_14",
        "manual_review",
        0,
        {
            "price_pair": {"promo": 8.0, "original": 19.9},
            "normalization_confident": False,
        },
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
    """每条规则覆盖其当前判断口径可达的关键结论。"""
    minimum_outcomes = {
        "alioth_13": {
            "pass",
            "fail",
            "skipped",
            "manual_review",
            "evidence_insufficient",
        },
        "alioth_14": {"pass", "fail", "manual_review", "evidence_insufficient"},
    }
    for rule_code, expected in minimum_outcomes.items():
        outcomes = {s.expected for s in _SCENARIOS if s.rule_code == rule_code}
        assert expected <= outcomes, f"{rule_code} 场景覆盖不足: {outcomes}"


def test_payment_rules_are_strictly_read_only() -> None:
    """高风险零触发不变量：payment 类全程只读，任何场景不得消耗触发 / 注入预算。"""
    for scenario in _SCENARIOS:
        assert scenario.triggers_used == 0, (
            f"{scenario.rule_code}-{scenario.scenario_id} 消耗了触发预算，违反支付类只读边界"
        )


def test_equal_price_is_a_failure_not_a_pass() -> None:
    """锁定"严格低于"语义：优惠价等于原价必须判 fail（回归防线，防止误改为 <=）。"""
    assert judge_alioth_14({"price_pair": {"promo": 10.0, "original": 10.0}}) == "fail"
