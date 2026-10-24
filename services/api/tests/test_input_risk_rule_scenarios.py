"""input_risk 类数据库规则判定场景回归。

具体判断标准由数据库规则管理，运行时由 Main Agent / sensitive-word-detection /
deterministic-assertion 按候选规则判定。本模块把当前 seed 规则**编码为参考判定函数**
（可执行规格），再用模拟的需求场景（已发布 UGC 证据组合 → 预期结论）逐分支验证：

- 决策表对每类证据组合都有唯一、无矛盾的结论（分支完备、可机械执行）；
- 7 态中确定性可达的 5 态都有场景覆盖，且每条规则至少覆盖 pass/fail/skipped；
- **只扫已发布 UGC 不变量**成立：唯一消耗触发预算的是 alioth_29 防刷子项的一次
  中性文案探测（场景必须标记 probe_content_neutral），其余规则零触发、绝不制造
  违规内容；
- hybrid 规则把词库命中与 Main Agent 语义结论作为输入证据建模，验证的是
  **词库前置 + 例外白名单 + model_uncertainty 转人工 + 汇总**逻辑（词库与 LLM
  判定质量本身属端到端评测）。

参考判定函数同时可作为未来 Tool 链的验收规格；若修改数据库规则判定语义，必须
同步修改此处参考实现；category Skill 只维护场景和 Tool 编排。
"""

from dataclasses import dataclass, field
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# 参考判定函数（每条分支对应当前数据库规则的一项判定要求）
# ---------------------------------------------------------------------------


def _judge_published_ugc(evidence: dict[str, Any]) -> str:
    """hybrid UGC 扫描共用骨架：词库前置 → 提取 → 语义复核（不确定转人工、例外放行）。"""
    if not evidence["ugc_present"]:
        return "skipped"  # 页面无已发布 UGC
    if not evidence["wordlist_available"]:
        return "manual_review"  # 词库未接入不硬判（不得仅凭 LLM 扫描）
    if not evidence.get("text_extracted", True):
        return "evidence_insufficient"  # UGC 文本提取失败
    verdict = evidence["agent_verdict"]  # {"violation","exception_applies","uncertain"}
    if verdict["uncertain"]:
        return "manual_review"  # model_uncertainty 触发人工复核
    if verdict["exception_applies"]:
        return "pass"  # 官方新闻通讯 / 功能区允许，留痕放行
    if evidence.get("scan_hit_unwhitelisted", False) or verdict["violation"]:
        return "fail"  # 词库命中（非白名单）或语义复核确认违规
    return "pass"


def judge_alioth_27(evidence: dict[str, Any]) -> str:
    """恶意链接与外跳诱导：链接校验工具不可用时不硬判，其余同 UGC 骨架。"""
    if (
        evidence["ugc_present"]
        and evidence.get("links_present", False)
        and not evidence.get("link_tool_available", True)
    ):
        return "manual_review"  # qr-link-validation 不可用，可疑链接不亲自访问
    return _judge_published_ugc(evidence)


def judge_alioth_28(evidence: dict[str, Any]) -> str:
    """涉政涉恐与人身攻击（合并①，含 alioth_32 官方新闻例外 / alioth_34 恐怖支持）。"""
    return _judge_published_ugc(evidence)


def judge_alioth_30(evidence: dict[str, Any]) -> str:
    """成人赌博与私人广告（功能区允许即例外放行）。"""
    return _judge_published_ugc(evidence)


_SEVERITY_ORDER = ("fail", "manual_review", "evidence_insufficient", "pass", "skipped")


def judge_alioth_29(evidence: dict[str, Any]) -> str:
    """输入防刷与刷屏：刷屏扫描（零触发）+ 防刷单次探测，两子项取最严重合并。"""
    # 刷屏子项：已发布评论重复 ≤3
    if not evidence["comments_present"]:
        spam = "skipped"
    elif evidence.get("max_duplicate_count") is None:
        spam = "evidence_insufficient"
    else:
        spam = "fail" if evidence["max_duplicate_count"] > 3 else "pass"
    # 防刷子项：单次中性文案探测
    if not evidence["logged_in"]:
        probe = "skipped"
    elif evidence.get("probe_observation") is None:
        probe = "evidence_insufficient"
    else:
        # 出现拦截 → 达标；单次探测未见拦截不足以断定无防刷 → 转人工，不硬判 fail
        probe = "pass" if evidence["probe_observation"]["rate_limited"] else "manual_review"
    for status in _SEVERITY_ORDER:
        if status in (spam, probe):
            return status
    raise AssertionError("unreachable")


def judge_alioth_45(evidence: dict[str, Any]) -> str:
    """上传限制与多选展示：只看已展示规则文案的四要素断言，不跑上传。"""
    if not evidence["is_post_publish_page"]:
        return "skipped"
    elements = evidence.get("displayed_rules")
    # {"count_limit","size_limit","formats","multi_select_rule"} -> bool
    if elements is None:
        return "evidence_insufficient"
    if evidence.get("wording_ambiguous", False):
        return "manual_review"
    return "pass" if all(elements.values()) else "fail"


_JUDGES = {
    "alioth_27": judge_alioth_27,
    "alioth_28": judge_alioth_28,
    "alioth_29": judge_alioth_29,
    "alioth_30": judge_alioth_30,
    "alioth_45": judge_alioth_45,
}

# 除 alioth_29 防刷子项外全部零触发（只扫已发布内容）。
_ZERO_TRIGGER_RULES = {"alioth_27", "alioth_28", "alioth_30", "alioth_45"}

# ---------------------------------------------------------------------------
# 模拟需求场景（证据组合 → 预期结论；triggers_used 仅 alioth_29 探测可为 1）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    rule_code: str
    expected: str
    triggers_used: int
    evidence: dict[str, Any] = field(default_factory=dict)


_CLEAN_VERDICT = {"violation": False, "exception_applies": False, "uncertain": False}
_VIOLATION_VERDICT = {"violation": True, "exception_applies": False, "uncertain": False}


_SCENARIOS = [
    # ---- alioth_27 恶意链接与外跳诱导 ----
    Scenario(
        "评论区干净无恶意链接",
        "alioth_27",
        "pass",
        0,
        {"ugc_present": True, "wordlist_available": True, "agent_verdict": _CLEAN_VERDICT},
    ),
    Scenario(
        "词库命中钓鱼链接黑名单",
        "alioth_27",
        "fail",
        0,
        {
            "ugc_present": True,
            "wordlist_available": True,
            "scan_hit_unwhitelisted": True,
            "agent_verdict": _VIOLATION_VERDICT,
        },
    ),
    Scenario(
        "词库漏检但语义识出诱导外跳变体",
        "alioth_27",
        "fail",
        0,
        {
            "ugc_present": True,
            "wordlist_available": True,
            "scan_hit_unwhitelisted": False,
            "agent_verdict": _VIOLATION_VERDICT,
        },
    ),
    Scenario(
        "评论区无已发布内容",
        "alioth_27",
        "skipped",
        0,
        {"ugc_present": False},
    ),
    Scenario(
        "链接校验工具不可用不硬判",
        "alioth_27",
        "manual_review",
        0,
        {
            "ugc_present": True,
            "wordlist_available": True,
            "links_present": True,
            "link_tool_available": False,
        },
    ),
    Scenario(
        "评论文本提取失败",
        "alioth_27",
        "evidence_insufficient",
        0,
        {"ugc_present": True, "wordlist_available": True, "text_extracted": False},
    ),
    # ---- alioth_28 涉政涉恐与人身攻击（合并①）----
    Scenario(
        "政治红线命中且语义确认",
        "alioth_28",
        "fail",
        0,
        {
            "ugc_present": True,
            "wordlist_available": True,
            "scan_hit_unwhitelisted": True,
            "agent_verdict": _VIOLATION_VERDICT,
        },
    ),
    Scenario(
        "命中涉华词但属官方新闻通讯例外",
        "alioth_28",
        "pass",
        0,
        {
            "ugc_present": True,
            "wordlist_available": True,
            "scan_hit_unwhitelisted": True,
            "agent_verdict": {"violation": False, "exception_applies": True, "uncertain": False},
        },
    ),
    Scenario(
        "输入内容语义干净",
        "alioth_28",
        "pass",
        0,
        {"ugc_present": True, "wordlist_available": True, "agent_verdict": _CLEAN_VERDICT},
    ),
    Scenario(
        "批评性引用还是支持性表达拿不准",
        "alioth_28",
        "manual_review",
        0,
        {
            "ugc_present": True,
            "wordlist_available": True,
            "agent_verdict": {"violation": False, "exception_applies": False, "uncertain": True},
        },
    ),
    Scenario(
        "页面无用户输入内容",
        "alioth_28",
        "skipped",
        0,
        {"ugc_present": False},
    ),
    # ---- alioth_29 输入防刷与刷屏 ----
    Scenario(
        "重复两次且探测被频率限制拦截",
        "alioth_29",
        "pass",
        1,
        {
            "comments_present": True,
            "max_duplicate_count": 2,
            "logged_in": True,
            "probe_observation": {"rate_limited": True},
            "probe_content_neutral": True,
        },
    ),
    Scenario(
        "重复恰好三次不构成刷屏",
        "alioth_29",
        "pass",
        1,
        {
            "comments_present": True,
            "max_duplicate_count": 3,
            "logged_in": True,
            "probe_observation": {"rate_limited": True},
            "probe_content_neutral": True,
        },
    ),
    Scenario(
        "同一用户刷屏四次",
        "alioth_29",
        "fail",
        1,
        {
            "comments_present": True,
            "max_duplicate_count": 4,
            "logged_in": True,
            "probe_observation": {"rate_limited": True},
            "probe_content_neutral": True,
        },
    ),
    Scenario(
        "单次探测未见任何拦截转人工",
        "alioth_29",
        "manual_review",
        1,
        {
            "comments_present": True,
            "max_duplicate_count": 1,
            "logged_in": True,
            "probe_observation": {"rate_limited": False},
            "probe_content_neutral": True,
        },
    ),
    Scenario(
        "无评论区且未登录两子项皆跳过",
        "alioth_29",
        "skipped",
        0,
        {"comments_present": False, "logged_in": False},
    ),
    Scenario(
        "探测已触发但观测缺失",
        "alioth_29",
        "evidence_insufficient",
        1,
        {
            "comments_present": True,
            "max_duplicate_count": 1,
            "logged_in": True,
            "probe_content_neutral": True,
        },
    ),
    # ---- alioth_30 成人赌博与私人广告 ----
    Scenario(
        "评论区无引流违规内容",
        "alioth_30",
        "pass",
        0,
        {"ugc_present": True, "wordlist_available": True, "agent_verdict": _CLEAN_VERDICT},
    ),
    Scenario(
        "赌博引流黑话经复核确认",
        "alioth_30",
        "fail",
        0,
        {
            "ugc_present": True,
            "wordlist_available": True,
            "scan_hit_unwhitelisted": True,
            "agent_verdict": _VIOLATION_VERDICT,
        },
    ),
    Scenario(
        "命中成人词但该功能区允许",
        "alioth_30",
        "pass",
        0,
        {
            "ugc_present": True,
            "wordlist_available": True,
            "scan_hit_unwhitelisted": True,
            "agent_verdict": {"violation": False, "exception_applies": True, "uncertain": False},
        },
    ),
    Scenario(
        "评论区无已发布内容",
        "alioth_30",
        "skipped",
        0,
        {"ugc_present": False},
    ),
    Scenario(
        "禁止内容库未接入不硬判",
        "alioth_30",
        "manual_review",
        0,
        {"ugc_present": True, "wordlist_available": False},
    ),
    # ---- alioth_45 上传限制与多选展示 ----
    Scenario(
        "四要素规则均已明示",
        "alioth_45",
        "pass",
        0,
        {
            "is_post_publish_page": True,
            "displayed_rules": {
                "count_limit": True,
                "size_limit": True,
                "formats": True,
                "multi_select_rule": True,
            },
        },
    ),
    Scenario(
        "缺少大小限制说明",
        "alioth_45",
        "fail",
        0,
        {
            "is_post_publish_page": True,
            "displayed_rules": {
                "count_limit": True,
                "size_limit": False,
                "formats": True,
                "multi_select_rule": True,
            },
        },
    ),
    Scenario(
        "当前非发帖页",
        "alioth_45",
        "skipped",
        0,
        {"is_post_publish_page": False},
    ),
    Scenario(
        "规则文案表述含糊难以断言",
        "alioth_45",
        "manual_review",
        0,
        {
            "is_post_publish_page": True,
            "displayed_rules": {
                "count_limit": True,
                "size_limit": True,
                "formats": True,
                "multi_select_rule": True,
            },
            "wording_ambiguous": True,
        },
    ),
    Scenario(
        "发帖页证据缺失",
        "alioth_45",
        "evidence_insufficient",
        0,
        {"is_post_publish_page": True},
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


def test_every_rule_has_pass_fail_and_skipped_coverage() -> None:
    """每条规则至少覆盖 pass / fail / skipped 三类结论，防止只测快乐路径。"""
    for rule_code in _JUDGES:
        outcomes = {s.expected for s in _SCENARIOS if s.rule_code == rule_code}
        assert {"pass", "fail", "skipped"} <= outcomes, f"{rule_code} 场景覆盖不足: {outcomes}"


def test_only_anti_spam_probe_consumes_trigger_budget() -> None:
    """只扫已发布 UGC 不变量：除 alioth_29 防刷探测外零触发，且探测 ≤1 次。"""
    for scenario in _SCENARIOS:
        assert scenario.triggers_used <= 1
        if scenario.rule_code in _ZERO_TRIGGER_RULES:
            assert scenario.triggers_used == 0, (
                f"{scenario.rule_code}-{scenario.scenario_id} 消耗了触发预算，违反只读扫描边界"
            )


def test_probe_scenarios_must_use_neutral_content() -> None:
    """绝不制造违规内容：任何消耗触发预算的场景必须标记探测文案为中性。"""
    for scenario in _SCENARIOS:
        if scenario.triggers_used > 0:
            assert scenario.evidence.get("probe_content_neutral") is True, (
                f"{scenario.rule_code}-{scenario.scenario_id} 探测未声明中性文案"
            )


def test_exactly_three_duplicates_is_not_spam() -> None:
    """锁定"3 次（不含）以上"语义：重复恰好 3 次必须不判刷屏（防止误改为 >=3）。"""
    outcome = judge_alioth_29(
        {
            "comments_present": True,
            "max_duplicate_count": 3,
            "logged_in": True,
            "probe_observation": {"rate_limited": True},
        }
    )
    assert outcome == "pass"


def test_unblocked_probe_never_hard_fails() -> None:
    """锁定防刷子项语义：单次探测未见拦截只能转人工，绝不硬判 fail。"""
    outcome = judge_alioth_29(
        {
            "comments_present": True,
            "max_duplicate_count": 0,
            "logged_in": True,
            "probe_observation": {"rate_limited": False},
        }
    )
    assert outcome == "manual_review"
