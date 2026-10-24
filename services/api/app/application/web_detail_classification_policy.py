"""详情模型优先与独立主体兜底策略，不执行模型调用或报告写入。"""

from services.api.app.domain.exploration import WEB_DETAIL_PAGE_TYPES


def deterministic_detail_result(evaluations):
    """独立规则只在唯一详情轴证据充分且无强冲突时形成候选。"""
    accepted = [
        code for code in sorted(WEB_DETAIL_PAGE_TYPES)
        if code in evaluations and evaluations[code].required_satisfied
        and not evaluations[code].strong_conflicts
    ]
    return {
        "page_type": accepted[0] if len(accepted) == 1 else "unknown",
        "confidence": .9 if len(accepted) == 1 else 0.0,
        "rules": {code: evaluations[code].to_payload() for code in sorted(WEB_DETAIL_PAGE_TYPES)
                  if code in evaluations},
    }


def classification_decision_audit(assessment):
    """持久化原始双方结论；不把最终签发分数冒充模型原分。"""
    return {
        "decision_source": assessment.decision_source,
        "model_result": assessment.model_result,
        "deterministic_result": assessment.deterministic_result,
    }
