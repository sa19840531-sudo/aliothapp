"""Web 序列沿用现有适用条件，独立保留材料不足和不适用原因。"""

from services.api.app.application.aggregate_rule_config import (
    resolve_rule_evidence_input_configuration,
)
from services.api.app.domain.rule.url_scope import candidate_rule_matches_page_url


def select_web_sequence_rule(
    rule, contract, sequence, *, task_channel, page_url, page_type, auth_gate="auto"
):
    """调用方认证真实 Task/锚点/冻结身份；本函数不创建分类或修改分母。"""

    def excluded(reason):
        return {"status": "not_applicable", "reason_code": reason}

    if rule.get("channel") is not None and rule["channel"] != task_channel:
        return excluded("WEB_RULE_CHANNEL_MISMATCH")
    if resolve_rule_evidence_input_configuration(rule["judge_config"]).is_aggregate:
        return excluded("WEB_RULE_AGGREGATE_SCOPE_REJECTED")
    if not candidate_rule_matches_page_url(page_url=page_url, url_pattern=rule.get("url_pattern"))[
        0
    ]:
        return excluded("WEB_RULE_URL_MISMATCH")
    if sequence["purpose"] not in contract.accepted_roles:
        return excluded("WEB_SEQUENCE_ROLE_REJECTED")
    # 当前没有已接通的 Web 账号证明；H5 环境开关不能替代当前页面事实。
    if rule["input_contract"].get("auth_requirement") in {"authenticated", "guest"}:
        return {"status": "evidence_insufficient", "reason_code": "WEB_RULE_ACCOUNT_STATE_UNKNOWN"}
    if page_type is None and not contract.accept_unclassified:
        return excluded("WEB_RULE_UNCLASSIFIED_REJECTED")
    if (
        page_type is not None
        and rule["applicable_page_types"]
        and page_type not in rule["applicable_page_types"]
    ):
        return excluded("WEB_RULE_PAGE_TYPE_MISMATCH")
    result = contract.validate_sequence(
        sequence,
        task_id=sequence["task_id"],
        agent_run_id=sequence["agent_run_id"],
        finalization_epoch=sequence["finalization_epoch"],
    )
    return {"status": result.status, "reason_code": result.reason_code}
