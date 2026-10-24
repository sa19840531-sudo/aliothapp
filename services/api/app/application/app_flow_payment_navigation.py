"""钱包到支付方式页之间的受限导航。"""

from services.api.app.domain.interaction import InteractionOperation, InteractionRiskLevel


def payment_navigation(work, state, config):
    """返回可点击的入口／方案，或明确的阻挡原因；不执行设备操作。"""
    candidates = [
        c
        for c in work.frontier.candidates
        if c.action_type in {InteractionOperation.CLICK, InteractionOperation.TAP}
    ]
    # 已出现渠道时交给原有支付宝选择与验证逻辑。
    if any(
        any(
            word in str(c.semantic_target)
            for word in (
                "支付宝",
                "支付寶",
                "Alipay",
                "微信支付",
                "微信",
                "WeChat",
                "更多支付",
            )
        )
        for c in candidates
    ):
        return None, None
    recharge = next(
        (
            c
            for c in candidates
            if str(c.semantic_target).strip() in {"充值", "儲值", "储值", "立即充值", "去充值"}
        ),
        None,
    )
    plan = config.get("test_payment_plan") or config.get("test_amount")
    plans = [c for c in candidates if c.metadata.get("payment_plan") is True]
    selected = recharge
    if selected is None and (
        plans or str(work.frontier.observation.page_type) in {"recharge", "recharge_page", "wallet"}
    ):
        if not plan:
            return None, "blocked_missing_payment_config"
        selected = next(
            (c for c in candidates if str(c.semantic_target).strip() == str(plan)), None
        )
        if selected is None:
            return None, "blocked_payment_plan_unavailable"
    if selected is None:
        return None, None
    if selected.semantic_target == state.payment_navigation_target:
        return None, "blocked_payment_navigation_unconfirmed"
    if selected.risk_level != InteractionRiskLevel.LOW or any(
        selected.metadata.get(key) is True
        for key in ("immediate_charge", "submit_order", "confirm_pay")
    ):
        return None, "blocked_payment_side_effect"
    if selected.metadata.get("creates_order") is True and not state.allow_pending_order_creation:
        return None, "blocked_order_creation"
    return selected, None
