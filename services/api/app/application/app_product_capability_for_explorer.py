"""V2 Explorer 闭环消费 Product 只读能力事实（F04）。

- 观察元数据已有 ``app_product_capability_facts`` 则复用；
- 缺失时按 ``package_name`` 现算；
- 永不授予绝对坐标；不得覆盖当前帧候选身份。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from services.api.app.domain.app_product.profiles import build_app_product_capability_facts
from services.api.app.domain.exploration import CandidateAction, ExplorationWork

APP_PRODUCT_CAPABILITY_FACTS_KEY = "app_product_capability_facts"

_COORDINATE_KEYS = frozenset(
    {
        "taps",
        "key_taps",
        "cloud_back_tap",
        "resolution",
        "gesture_y_max",
        "absolute_xy",
        "normalized_bounds",
        "x",
        "y",
    }
)


def _work_package_name(work: ExplorationWork) -> str | None:
    meta = work.frontier.observation.metadata
    if isinstance(meta, Mapping):
        package = str(meta.get("package_name") or "").strip()
        if package:
            return package
    goal = work.app_goal
    if goal is not None:
        for item in goal.allowed_packages or ():
            text = str(item or "").strip()
            if text:
                return text
    return None


def sanitize_capability_facts(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """剥掉可能混入的坐标字段，保证 Explorer 侧无执行授权。"""

    if not isinstance(raw, Mapping):
        return {
            "ok": False,
            "profile_present": False,
            "grants_absolute_coordinates": False,
            "error": "facts_missing",
        }
    out = {
        key: value
        for key, value in dict(raw).items()
        if key not in _COORDINATE_KEYS
    }
    out["grants_absolute_coordinates"] = False
    if "profile_present" not in out:
        out["profile_present"] = bool(out.get("ok"))
    return out


def resolve_work_product_capability_facts(work: ExplorationWork) -> dict[str, Any]:
    """供 Explorer 确定性路径与模型 payload 共用的只读事实。"""

    package = _work_package_name(work)
    meta = work.frontier.observation.metadata
    if isinstance(meta, Mapping):
        existing = meta.get(APP_PRODUCT_CAPABILITY_FACTS_KEY)
        if isinstance(existing, Mapping) and existing:
            polluted = existing.get("grants_absolute_coordinates") is True or any(
                key in existing for key in _COORDINATE_KEYS
            )
            if polluted:
                return sanitize_capability_facts(
                    build_app_product_capability_facts(package)
                )
            return sanitize_capability_facts(existing)
    return sanitize_capability_facts(build_app_product_capability_facts(package))


def capability_facts_for_agent_payload(work: ExplorationWork) -> dict[str, Any]:
    """模型可见摘要：去掉 markdown／目录等大字段，保留排序提示。"""

    facts = resolve_work_product_capability_facts(work)
    keep = (
        "ok",
        "package_name",
        "slug",
        "display_name",
        "profile_present",
        "grants_absolute_coordinates",
        "execution_policy",
        "popup_close_policy",
        "semantic_recovery_hints",
        "timing_rules",
        "exploration_curriculum",
        "agent_loading_rule",
        "hint",
        "error",
    )
    return {key: facts[key] for key in keep if key in facts}


def rank_eligible_by_capability_facts(
    *,
    work: ExplorationWork,
    eligible: Sequence[CandidateAction],
) -> tuple[CandidateAction, ...]:
    """Agent 合同：按语义恢复提示提升已有候选；不新增、不改 bounds。"""

    if not eligible:
        return ()
    from services.api.app.application.app_product_orchestration_policy import (
        uses_agent_generic_contract,
    )

    package = _work_package_name(work)
    if not uses_agent_generic_contract(package):
        return tuple(eligible)

    facts = resolve_work_product_capability_facts(work)
    if not facts.get("profile_present"):
        return tuple(eligible)

    hints = " ".join(
        str(item) for item in (facts.get("semantic_recovery_hints") or ())
    ).casefold()
    policy = str(facts.get("popup_close_policy") or "").casefold()
    hint_blob = f"{hints} {policy}"

    def _score(candidate: CandidateAction) -> tuple[int, int, str]:
        terms = " ".join(
            (
                str(candidate.semantic_target or ""),
                str(candidate.metadata.get("purpose") or ""),
                str(candidate.metadata.get("expected_state_hint") or ""),
            )
        ).casefold()
        boost = 0
        if "cloud_back" in hint_blob or "watcher" in hint_blob:
            if any(marker in terms for marker in ("返回", "back", "关闭", "close", "<")):
                boost += 30
        if "vision_skip" in hint_blob or "跳过" in hint_blob or "进入" in hint_blob:
            if any(marker in terms for marker in ("跳过", "进入", "skip")):
                boost += 30
        for token in ("会员", "钱包", "我的", "底栏", "tab"):
            if token in hint_blob and token in terms:
                boost += 10
        return (boost, int(candidate.exploration_value), str(candidate.candidate_id))

    ranked = sorted(eligible, key=_score, reverse=True)
    return tuple(ranked)
