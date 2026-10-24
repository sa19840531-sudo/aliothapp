"""只读套餐反馈以实际动作回执及前后目标身份为准，不推断交易事实。"""

import hashlib
import math

from services.api.app.application.h5_probe_targets import (
    payment_plan_structure,
    probe_subject_key,
    visible_probe_element,
)


def payment_checkpoint_facts(*, before, after, action_receipt):
    if not isinstance(action_receipt, dict) or not isinstance(after.get("url"), str):
        return {}
    if (
        action_receipt.get("status") != "succeeded"
        or action_receipt.get("execution_outcome") != "succeeded"
        or action_receipt.get("error_code") is not None
    ):
        return {}
    diff = action_receipt.get("state_diff")
    effect = diff.get("h5_probe_effect") if isinstance(diff, dict) else None
    origin = (after.get("document") or {}).get("time_origin_ms")
    if not isinstance(effect, dict) or (
        effect.get("protocol_version") != "h5-probe-display-effect.v1"
        or effect.get("goal") != "payment_plan_selection"
        or effect.get("verified") is not True
        or effect.get("readonly_isolation") is not True
        or type(effect.get("readonly_denied_count")) is not int
        or effect["readonly_denied_count"] != 0
        or type(origin) not in (int, float)
        or not math.isfinite(origin)
        or effect.get("document_time_origin_ms") != origin
        or effect.get("url_hash") != hashlib.sha256(after["url"].encode("utf-8")).hexdigest()
        or after.get("loading") is True
        or after.get("capture_degraded")
        or after.get("collection_error")
    ):
        return {}
    subject = effect.get("subject_key")
    prior = [
        item
        for item in before.get("elements", ())
        if isinstance(item, dict)
        and payment_plan_structure(item)
        and visible_probe_element(item, before)
        and probe_subject_key(item) == subject
    ]
    current = [
        item
        for item in after.get("elements", ())
        if isinstance(item, dict)
        and payment_plan_structure(item)
        and visible_probe_element(item, after)
        and probe_subject_key(item) == subject
    ]
    if len(prior) != 1 or len(current) != 1:
        return {}
    item = current[0]
    selected = (
        item.get("aria_checked") == "true"
        or item.get("aria_selected") == "true"
        or bool({"selected", "checked"}.intersection(item.get("class_tokens") or []))
    )
    return {"payment_plan_selected": selected}
