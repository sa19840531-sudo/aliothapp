"""主阶段的有效计时与局部超限恢复，不把子流程耗时转嫁给父流程。"""

from datetime import datetime


def phase_key(state):
    key = state.phase
    if key == "bottom_tabs":
        key += ":" + (state.pending_bottom_tabs[0] if state.pending_bottom_tabs else "discover")
    elif key == "top_tabs":
        key += ":" + str(len(state.visited_top_tabs))
    return key


def charge_phase_time(runtime, key, now):
    elapsed = runtime.setdefault("phase_elapsed_seconds", {})
    previous = runtime.get("active_phase_key")
    since = runtime.get("active_since")
    if previous and since:
        seconds = max(0, (now - datetime.fromisoformat(since)).total_seconds())
        elapsed[previous] = float(elapsed.get(previous, 0)) + seconds
    elif key in runtime.get("phase_started_at", {}):
        # 兼容旧版单阶段时钟；新进度只累计实际处于该阶段的时间。
        elapsed[key] = max(0, (
            now - datetime.fromisoformat(runtime["phase_started_at"][key])
        ).total_seconds())
    runtime["active_phase_key"] = key
    runtime["active_since"] = now.isoformat()
    return float(elapsed.get(key, 0))


def recover_phase_limit(state, reason):
    """业务局部失败先恢复，返回／清障失败与总预算耗尽才终止整体。"""
    phase = state.phase
    if phase in {"video_entry", "video_verify"}:
        state.mark_stage("home_video", "blocked")
        state.video_outcome = reason
        state.advance("back_home")
    elif phase == "back_home":
        # 影片阶段已结束却卡在返回：直接进入上方分类，避免底栏永远不点。
        state.mark_stage("back_home", "blocked")
        state.advance("top_tabs")
    elif phase == "top_tabs":
        state.mark_stage("top_tabs", "blocked")
        state.top_tabs_status = "blocked"
        state.advance("top_tab_restore")
    elif phase == "bottom_tabs" and state.pending_bottom_tabs:
        label = state.pending_bottom_tabs[0]
        state.blocked_bottom_tabs.append(label)
        state.tab_results[label] = {
            **state.tab_results.get(label, {}), "status": "blocked", "reason": reason,
        }
        state.sync_pending_bottom_tabs()
    elif phase in {"comic_entry", "comic_detail", "comic_reader", "comic_scroll"}:
        state.comic_flow_status = "blocked"
        state.comic_outcome = reason
        state.mark_stage("comic", "blocked")
        state.advance("comic_back_home")
    elif phase in {"wallet_search", "payment_alipay"}:
        state.mark_stage("mine_wallet" if phase == "wallet_search" else "payment_alipay", "blocked")
        state.wallet_search_done = True
        state.payment_blocked_reason = reason
        state.resume_after_payment()
    else:
        return False
    state.recovery_events.append(reason)
    return True
