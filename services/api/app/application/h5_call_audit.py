"""物理请求准入与实际用量分开审计，缺失不补零、不换算币种。"""

from collections import defaultdict
from decimal import Decimal, InvalidOperation


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() and number >= 0 else None


def _totals(rows):
    calls = sum(row["ai_calls"] for row in rows)
    physical = sum(row["ai_calls"] for row in rows if row.get("physical_request") is True)
    usage_missing = sum(row["ai_calls"] for row in rows if row.get("usage_available") is not True)
    prices_missing = sum(
        row["ai_calls"] for row in rows if row.get("pricing_configured") is not True
    )
    tokens = [row.get("total_tokens") for row in rows if row.get("usage_available") is True]
    token_complete = not usage_missing and all(
        type(value) is int and value >= 0 for value in tokens
    )
    costs = [
        _number(row.get("total_cost"))
        for row in rows
        if row.get("usage_available") is True
        and row.get("pricing_configured") is True
        and row.get("cost_currency") == "CNY"
    ]
    known = [cost for cost in costs if cost is not None]
    complete = bool(rows) and len(known) == len(rows) and physical == calls
    return dict(
        usage_calls=calls,
        physical_calls=physical,
        physical_request_unverified_calls=calls - physical,
        usage_unavailable_calls=usage_missing,
        pricing_unconfigured_calls=prices_missing,
        known_tokens=sum(value for value in tokens if type(value) is int and value >= 0),
        total_tokens=sum(tokens) if rows and token_complete else None,
        known_cost_cny=str(sum(known, Decimal("0"))) if known else None,
        total_cost_cny=str(sum(known, Decimal("0"))) if complete else None,
        cost_complete=complete,
    )


def summarize_h5_calls(rows, *, budget):
    rows = [dict(row) for row in rows]
    groups = defaultdict(list)
    for row in rows:
        if type(row.get("ai_calls")) is not int or row["ai_calls"] < 0:
            raise ValueError("模型流水缺少有效物理调用计数")
        key = tuple(row.get(key) for key in ("source", "operation", "provider", "model"))
        groups[key].append(row)
    totals = _totals(rows)
    recorded = totals.pop("usage_calls")
    physical = totals.pop("physical_calls")
    admitted = budget["admitted_calls"] if budget is not None else None
    matched = admitted == recorded
    if not matched or totals["physical_request_unverified_calls"]:
        totals["total_tokens"] = None
        totals["total_cost_cny"] = None
        totals["cost_complete"] = False
    return {
        **totals,
        "recorded_physical_calls": physical,
        "recorded_usage_calls": recorded,
        "budget_status": "not_initialized" if budget is None else "frozen",
        "budget": dict(budget) if budget is not None else None,
        "admitted_without_usage_event": (
            max(0, admitted - recorded) if admitted is not None else None
        ),
        "admission_usage_counts_match": matched,
        "calls_by_source": [
            dict(
                zip(("source", "operation", "provider", "model"), key, strict=True),
                **_totals(items),
            )
            for key, items in sorted(
                groups.items(), key=lambda item: tuple(str(v or "") for v in item[0])
            )
        ],
        "interpretation": "准入尚无流水不单独证明浪费或故障；需结合在途请求和有效证据产出。",
    }
