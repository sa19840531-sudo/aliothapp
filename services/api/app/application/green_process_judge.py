"""绿色过程分项的固定分派；事实不足继续保留明确依赖。"""

from services.api.app.application.green_evidence_contracts import GreenEvidenceSet
from services.api.app.application.green_rule_evidence import GreenSubitem


def judge_green_process_subitems(*, contract, evidence, required_facts, frozen_config=None):
    """TASK仅限定冻结检查材料，不以本次动作代表整个产品覆盖。"""
    if not isinstance(evidence, GreenEvidenceSet) or not evidence.verified_read:
        raise ValueError("过程判读仅接受权威只读加载器的原件")
    from services.api.app.application.green_fact_bindings import read_green_fact_views

    config = frozen_config or {}
    views = read_green_fact_views(evidence, config.get("green_shared_bindings"))
    if contract["rule_key"] in {"s72", "s49", "s73", "62"}:
        if views:
            from services.api.app.application.green_temporal_rules import judge_temporal_rule

            return judge_temporal_rule(contract["rule_key"], views, contract["subchecks"])
        from services.api.app.application.green_temporal_judge import judge_temporal_subitems

        return judge_temporal_subitems(
            contract["rule_key"],
            evidence,
            required_facts=required_facts,
            subchecks=contract["subchecks"],
        )
    items = []
    for key in contract["subchecks"]:
        item = None
        if contract["rule_key"] == "s35" and views:
            from services.api.app.application.green_layout_stability_rules import (
                judge_interaction_subitem,
            )

            item = judge_interaction_subitem(key, views)
        if (
            contract["rule_key"] == "s35"
            and key == "correct_response"
            and (
                item is None
                or item.status == "evidence_insufficient"
                and not any(
                    view.get("response_actions") or view.get("repeat_actions") for view in views
                )
            )
        ):
            from services.api.app.application.green_interaction_judge import (
                judge_green_correct_response,
            )

            item = judge_green_correct_response(evidence)
        if item is None:
            item = GreenSubitem(
                key,
                "evidence_insufficient",
                "现有原件尚未生产该分项必需的业务对象、因果或完整窗口",
                tuple(dict.fromkeys((*evidence.issues, *required_facts[key]))),
                tuple(capture.capture_id for capture in evidence.captures),
                tuple(action.action_id for action in evidence.actions),
            )
        items.append(item)
    return tuple(items)
