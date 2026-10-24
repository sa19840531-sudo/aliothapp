"""Web 稳定窗口的有界诊断；不参与稳定性判定，不保存页面文字与完整 URL。"""


def record_window_reset(
    diagnostics,
    *,
    previous,
    current,
    reasons,
    sample_index,
    observed_at_ms,
    previous_stable_count,
):
    events = diagnostics.setdefault("window_reset_events", [])
    diagnostics.setdefault("window_reset_count", 0)
    diagnostics.setdefault("window_reset_events_truncated", False)
    causes = list(reasons)
    if previous is not None:
        for index, name in enumerate(
            (
                "document_url_changed",
                "document_epoch_changed",
                "structure_changed",
            )
        ):
            if previous[index] != current[index]:
                causes.append(name)
    # 初次采样没有旧窗口；持续阻断只在信号变化时记录，避免每轮重复。
    signature = tuple(causes)
    last_signature = diagnostics.get("last_reset_signals")
    diagnostics["last_reset_signals"] = signature
    if not causes or (not previous_stable_count and signature == last_signature):
        return
    diagnostics["window_reset_count"] += 1
    event = {
        "sample_index": sample_index,
        "observed_at_ms": observed_at_ms,
        "reasons": causes,
        "previous_stable_sample_count": previous_stable_count,
        "document_time_origin_ms": current[1],
    }
    if len(events) < 64:
        events.append(event)
    else:
        diagnostics["window_reset_events_truncated"] = True
