"""执行前拒绝的触控回传：允许一次新帧重定位，不伪造已执行证据。"""

from collections.abc import Mapping

from services.api.app.application.app_tangguo_flow_state import TangguoFlowState


def reconcile_unexecuted_flow_action(lineage, parameters):
    """保留全局预算，仅纠正未执行动作的阶段；每个目标最多重定位一次。"""
    payload = lineage.get("tangguo_flow_state")
    if not isinstance(payload, Mapping):
        return
    state = TangguoFlowState.from_dict(payload)
    phase = parameters.get("tangguo_flow_phase")
    label = state.video_target if phase == "video_entry" else parameters.get("tangguo_flow_tab")
    if phase not in {"video_entry", "bottom_tabs"} or not label:
        return
    key = f"{phase}:{label}"
    counts = state.contract_runtime.setdefault("unexecuted_rebinds", {})
    count = int(counts.get(key, 0))
    counts[key] = count + 1
    if phase == "video_entry":
        if count == 0:
            state.attempted_video_labels = [
                item for item in state.attempted_video_labels if item != label
            ]
            state.video_attempts = max(0, state.video_attempts - 1)
            state.advance("video_entry")
        else:
            state.video_outcome = "video_target_revalidation_exhausted"
            state.mark_stage("home_video", "blocked")
            state.advance("top_tabs")
    else:
        state.tab_results[label] = {
            **state.tab_results.get(label, {}), "clicked": False, "awaiting_verify": False,
            "reason": (
                "target_revalidation_required" if count == 0 else "target_revalidation_exhausted"
            ),
        }
        if count > 0:
            if label not in state.blocked_bottom_tabs:
                state.blocked_bottom_tabs.append(label)
            state.tab_results[label]["status"] = "blocked"
        state.sync_pending_bottom_tabs()
    lineage["tangguo_flow_state"] = state.to_dict()
