"""interaction 类数据库规则判定场景回归。

具体判断标准由数据库规则管理，运行时由 Main Agent / deterministic-assertion
按候选规则判定。本模块把当前 seed 规则**编码为参考判定函数**（可执行规格），再用模拟的
需求场景（页面证据组合 → 预期结论）逐分支验证：

- 决策表对每类证据组合都有唯一、无矛盾的结论（分支完备、可机械执行）；
- 7 态中确定性可达的 5 态（pass / fail / skipped / manual_review /
  evidence_insufficient）都有场景覆盖；
- B 模式不变量成立：单次触发预算 ≤1、能不触发就不触发（alioth_7 属性可判时
  0 触发）、alioth_48 绝不点击退出登录。

参考判定函数同时可作为未来 deterministic-assertion Tool 的验收规格。真实 LLM 的
判定质量属于需要真实页面的端到端评测，不在单测范围。若修改数据库规则判定语义，
必须同步修改此处的参考实现；category Skill 只维护场景和 Tool 编排。
"""

import re
from dataclasses import dataclass, field
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# 参考判定函数（每条分支对应当前数据库规则的一项判定要求）
# ---------------------------------------------------------------------------


def judge_alioth_7(evidence: dict[str, Any]) -> str:
    """外链操作：操作前后可信截图不同即通过。"""
    if not evidence["external_link_present"]:
        return "skipped"
    before = evidence.get("before_screenshot")
    after = evidence.get("after_screenshot")
    if before is None or after is None:
        return "evidence_insufficient"
    return "pass" if before != after else "fail"


def judge_alioth_9(evidence: dict[str, Any]) -> str:
    """互动指标：存在评论数或点赞数等可见数字即通过。"""
    if not evidence["metrics_expected"]:
        return "skipped"
    visible_text = evidence.get("visible_text")
    if visible_text is None:
        return "evidence_insufficient"
    pattern = re.compile(
        r"(?i)(?:评论(?:数|量)?|点赞(?:数|量)?|comment(?:s| count)?|"
        r"like(?:s| count)?)\s*[:：]?\s*\d+"
    )
    return "pass" if pattern.search(visible_text) else "evidence_insufficient"


def judge_alioth_41(evidence: dict[str, Any]) -> str:
    """二维码与网址有效性：解码 + 可达性；校验 Tool 不可用时不硬判。"""
    if not evidence["qr_or_url_present"]:
        return "skipped"
    if not evidence["validation_tool_available"]:
        # 解码与网络可达性 Agent 推理无法替代；当前规则允许
        # evidence_insufficient / manual_review，参考实现取人工兜底。
        return "manual_review"
    if not evidence["decode_ok"]:
        return "fail"
    if not evidence["url_reachable"]:
        return "fail"
    if evidence.get("misleading_redirect", False):
        return "fail"  # 跳转与展示不符（误导）
    return "pass"


def judge_alioth_46(evidence: dict[str, Any]) -> str:
    """帖子媒体全屏浏览：单次点击 → 观察是否进入全屏态。"""
    if not evidence["has_media_post"]:
        return "skipped"
    click = evidence.get("single_click_observation")
    if click is None:
        return "evidence_insufficient"
    return "pass" if click["entered_fullscreen"] else "fail"


def judge_alioth_48(evidence: dict[str, Any]) -> str:
    """退出登录入口：登录态下纯存在性断言，绝不实际点击退出。"""
    if not evidence["logged_in"]:
        return "skipped"
    visible_text = evidence.get("visible_text")
    if visible_text is None:
        return "evidence_insufficient"
    lowered = visible_text.lower()
    return "pass" if any(k in lowered for k in ("退出登录", "登出", "sign out")) else "fail"


_JUDGES = {
    "alioth_7": judge_alioth_7,
    "alioth_9": judge_alioth_9,
    "alioth_41": judge_alioth_41,
    "alioth_46": judge_alioth_46,
    "alioth_48": judge_alioth_48,
}


# ---------------------------------------------------------------------------
# 模拟需求场景（证据组合 → 预期结论；triggers_used 记录该场景消耗的最小触发次数）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    rule_code: str
    expected: str
    triggers_used: int
    evidence: dict[str, Any] = field(default_factory=dict)


_SCENARIOS = [
    # ---- alioth_7 外链操作前后截图变化 ----
    Scenario(
        "外链操作前后截图发生变化",
        "alioth_7",
        "pass",
        1,
        {
            "external_link_present": True,
            "before_screenshot": "before",
            "after_screenshot": "after",
        },
    ),
    Scenario(
        "页面无外链",
        "alioth_7",
        "skipped",
        0,
        {"external_link_present": False},
    ),
    Scenario(
        "外链操作前后截图相同",
        "alioth_7",
        "fail",
        1,
        {
            "external_link_present": True,
            "before_screenshot": "same",
            "after_screenshot": "same",
        },
    ),
    Scenario(
        "缺少外链操作后截图",
        "alioth_7",
        "evidence_insufficient",
        1,
        {"external_link_present": True, "before_screenshot": "before"},
    ),
    # ---- alioth_9 评论数或点赞数可见 ----
    Scenario(
        "页面不要求展示互动指标",
        "alioth_9",
        "skipped",
        0,
        {"metrics_expected": False},
    ),
    Scenario(
        "页面存在点赞数字",
        "alioth_9",
        "pass",
        0,
        {"metrics_expected": True, "visible_text": "点赞 11"},
    ),
    Scenario(
        "页面存在评论数字",
        "alioth_9",
        "pass",
        0,
        {"metrics_expected": True, "visible_text": "评论数：5"},
    ),
    Scenario(
        "页面文本不含互动数字",
        "alioth_9",
        "evidence_insufficient",
        0,
        {"metrics_expected": True, "visible_text": "欢迎浏览详情"},
    ),
    Scenario(
        "页面可见文本缺失",
        "alioth_9",
        "evidence_insufficient",
        0,
        {"metrics_expected": True},
    ),
    # ---- alioth_41 二维码与网址有效性 ----
    Scenario(
        "解码成功且第三方可访问",
        "alioth_41",
        "pass",
        0,
        {
            "qr_or_url_present": True,
            "validation_tool_available": True,
            "decode_ok": True,
            "url_reachable": True,
        },
    ),
    Scenario(
        "二维码无法解码",
        "alioth_41",
        "fail",
        0,
        {
            "qr_or_url_present": True,
            "validation_tool_available": True,
            "decode_ok": False,
            "url_reachable": False,
        },
    ),
    Scenario(
        "解码成功但网址不可达",
        "alioth_41",
        "fail",
        0,
        {
            "qr_or_url_present": True,
            "validation_tool_available": True,
            "decode_ok": True,
            "url_reachable": False,
        },
    ),
    Scenario(
        "解码可达但跳转与展示不符",
        "alioth_41",
        "fail",
        0,
        {
            "qr_or_url_present": True,
            "validation_tool_available": True,
            "decode_ok": True,
            "url_reachable": True,
            "misleading_redirect": True,
        },
    ),
    Scenario(
        "页面无二维码与网址",
        "alioth_41",
        "skipped",
        0,
        {"qr_or_url_present": False},
    ),
    Scenario(
        "校验 Tool 未上线不硬判",
        "alioth_41",
        "manual_review",
        0,
        {"qr_or_url_present": True, "validation_tool_available": False},
    ),
    # ---- alioth_46 帖子媒体全屏浏览 ----
    Scenario(
        "单次点击进入全屏",
        "alioth_46",
        "pass",
        1,
        {"has_media_post": True, "single_click_observation": {"entered_fullscreen": True}},
    ),
    Scenario(
        "仅缩略图点击无全屏",
        "alioth_46",
        "fail",
        1,
        {"has_media_post": True, "single_click_observation": {"entered_fullscreen": False}},
    ),
    Scenario(
        "列表无图片视频帖",
        "alioth_46",
        "skipped",
        0,
        {"has_media_post": False},
    ),
    # ---- alioth_48 退出登录入口 ----
    Scenario(
        "设置页存在退出登录入口",
        "alioth_48",
        "pass",
        0,
        {"logged_in": True, "visible_text": "账号与安全 通知设置 退出登录"},
    ),
    Scenario(
        "英文站点存在 Sign out 入口",
        "alioth_48",
        "pass",
        0,
        {"logged_in": True, "visible_text": "Account Notifications Sign Out"},
    ),
    Scenario(
        "设置页缺失登出入口",
        "alioth_48",
        "fail",
        0,
        {"logged_in": True, "visible_text": "账号与安全 通知设置 关于我们"},
    ),
    Scenario(
        "未登录无法验证登出入口",
        "alioth_48",
        "skipped",
        0,
        {"logged_in": False},
    ),
    Scenario(
        "登录态页面可见文本缺失",
        "alioth_48",
        "evidence_insufficient",
        0,
        {"logged_in": True},
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
        "alioth_7": {"pass", "fail", "skipped", "evidence_insufficient"},
        "alioth_9": {"pass", "skipped", "evidence_insufficient"},
        "alioth_41": {"pass", "fail", "skipped", "manual_review"},
        "alioth_46": {"pass", "fail", "skipped"},
        "alioth_48": {"pass", "fail", "skipped", "evidence_insufficient"},
    }
    for rule_code, expected in minimum_outcomes.items():
        outcomes = {s.expected for s in _SCENARIOS if s.rule_code == rule_code}
        assert expected <= outcomes, f"{rule_code} 场景覆盖不足: {outcomes}"


def test_single_trigger_budget_never_exceeded() -> None:
    """B 模式硬边界：任何场景的最小触发次数不得超过 1。"""
    assert all(scenario.triggers_used <= 1 for scenario in _SCENARIOS)


def test_logout_rule_never_consumes_trigger() -> None:
    """alioth_48 只做存在性断言，任何场景都不允许点击退出登录。"""
    assert all(s.triggers_used == 0 for s in _SCENARIOS if s.rule_code == "alioth_48")


def test_external_link_rule_only_triggers_when_link_is_present() -> None:
    """alioth_7 仅在确有外链时允许一次操作，以取得前后截图。"""
    for scenario in _SCENARIOS:
        if scenario.rule_code != "alioth_7":
            continue
        if scenario.evidence["external_link_present"]:
            assert scenario.triggers_used == 1, scenario.scenario_id
        else:
            assert scenario.triggers_used == 0, scenario.scenario_id
