"""验证现有原件事实消费与批量读取；呈现事实不冒充业务结果。"""

import copy
import hashlib
import json
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from services.api.app.application.app_surface_evidence import project_ui_snapshot_from_hierarchy
from services.api.app.application.green_evidence_contracts import (
    GreenCapture,
    GreenEvidenceSet,
    GreenOriginal,
    _verified_evidence_set,
)
from services.api.app.application.green_rule_catalog import GREEN_RULES
from services.api.app.application.green_temporal_facts import (
    PresentationKind,
    read_temporal_observations,
)
from services.api.app.application.green_temporal_judge import (
    judge_temporal_subitem,
    judge_temporal_subitems,
)
from services.api.app.infrastructure.browser.page_capture import _sanitize_and_bound_ui_snapshot

NOW = datetime(2026, 10, 21, tzinfo=UTC)


def ui(*texts):
    """沿浏览器现存脱敏器保存真实字段，不新增认证或业务结果协议。"""
    rows = [{"selector": "#module", "visible": True, "rect": {
        "x": 0, "y": 0, "width": 390, "height": 600,
    }}]
    for index, text in enumerate(texts):
        rows.append({
            "selector": f"#copy-{index}", "parent_selector": "#module",
            "tag": "button" if text == "重试" else "div", "text": text,
            "visible": True, "interactive": text == "重试",
            "rect": {"x": 10, "y": index * 50 + 20, "width": 100, "height": 40},
            "hit_test": {"center_hits_target": True},
        })
    return _sanitize_and_bound_ui_snapshot({
        "url": "https://controlled.invalid/list", "elements": rows,
        "viewport": {"width": 390, "height": 600},
    })


def evidence(*artifacts):
    originals = []
    for index, (kind, payload) in enumerate(artifacts):
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        originals.append(GreenOriginal(
            f"original-{index}", kind, "capture", "sha256:" + hashlib.sha256(raw).hexdigest(), raw,
        ))
    capture = GreenCapture(
        "capture", "session", "primary", None, None, None, NOW, "manifest", 1,
        tuple(originals),
    )
    return _verified_evidence_set(
        task_id="task", run_id="run", epoch=0, input_set_id="input", input_hash="hash",
        scope_key="green_temporal_rules.v1", target_platform="h5", captures=(capture,),
    )


def test_existing_browser_snapshot_reports_visible_copy_and_hittable_entry_only():
    case = evidence(("ui_snapshot", ui("网络异常", "重试", "加载中")))
    facts = read_temporal_observations(case).facts
    assert {item.kind for item in facts} >= {
        PresentationKind.FAILURE_COPY, PresentationKind.REASON_COPY,
        PresentationKind.RETRY_TARGET, PresentationKind.LOADING_COPY,
    }
    result = judge_temporal_subitem("s49", "retry_entry", case)
    assert result.status == "evidence_insufficient"
    assert "可见且可命中" in result.reason
    assert "actual_module_fault" in result.missing
    assert result.capture_ids == ("capture",)
    assert "https://" not in result.reason


def test_real_android_projection_can_supply_copy_without_inventing_hit_test():
    snapshot = project_ui_snapshot_from_hierarchy(
        '<hierarchy><node class="android.widget.FrameLayout" bounds="[0,0][390,600]">'
        '<node class="android.widget.TextView" resource-id="app:id/empty" text="暂无数据" '
        'visible-to-user="true" bounds="[10,20][110,60]" />'
        '<node class="android.widget.Button" resource-id="app:id/retry" text="重试" '
        'clickable="true" enabled="true" visible-to-user="true" bounds="[10,70][110,110]" />'
        '</node></hierarchy>'
    )
    case = evidence(("ui_snapshot", snapshot))
    facts = read_temporal_observations(case).facts
    assert [item.kind for item in facts] == [
        PresentationKind.EMPTY_COPY, PresentationKind.OTHER_COPY,
    ]
    result = judge_temporal_subitem("s72", "empty_state_meaning", case)
    assert result.status == "evidence_insufficient"


@pytest.mark.parametrize("change", ["hidden", "offscreen", "zero_opacity", "disabled", "blocked"])
def test_unusable_retry_is_not_reported_as_usable(change):
    snapshot = ui("重试")
    row = snapshot["elements"][1]
    if change == "hidden":
        row["visible"] = False
    elif change == "offscreen":
        row["rect"]["x"] = 500
    elif change == "zero_opacity":
        row["style"] = {"opacity": "0"}
    elif change == "disabled":
        row["disabled"] = True
    else:
        row["hit_test"]["center_hits_target"] = False
    facts = read_temporal_observations(evidence(("ui_snapshot", snapshot))).facts
    assert PresentationKind.RETRY_TARGET not in {item.kind for item in facts}


@pytest.mark.parametrize(
    "fault", ["duplicate_original", "duplicate_selector", "truncated", "bad_json"],
)
def test_ambiguous_or_broken_ui_cannot_select_healthy_copy(fault):
    snapshot = ui("暂无数据")
    artifacts = [("ui_snapshot", snapshot)]
    if fault == "duplicate_original":
        artifacts.append(("ui_snapshot", snapshot))
    elif fault == "duplicate_selector":
        snapshot["elements"].append(copy.deepcopy(snapshot["elements"][1]))
    elif fault == "truncated":
        snapshot["budget_truncated"] = True
    else:
        artifacts = [("ui_snapshot", b"{bad")]
    case = evidence(*artifacts)
    observed = read_temporal_observations(case)
    assert observed.issues
    assert PresentationKind.EMPTY_COPY not in {item.kind for item in observed.facts}
    assert judge_temporal_subitem("s72", "empty_state_meaning", case).missing


def test_http200_empty_text_driver_success_and_old_toast_do_not_supply_business_state():
    case = evidence(
        ("ui_snapshot", {**ui("暂无数据"), "business_success": True, "actual_empty": True}),
        ("har", {"format": "network_events", "entries": [
            {"event": "response", "url": "https://controlled.invalid/api", "status": 200},
        ]}),
        ("action_trace", [{"result": "success", "transient_messages": [{
            "text": "成功", "first_seen_ms": 0, "observation_id": "old", "event_id": "old",
        }]}]),
    )
    for rule in GREEN_RULES:
        if rule.key in {"s72", "s49", "s73", "62"}:
            for key in rule.subchecks:
                item = judge_temporal_subitem(rule.key, key, case)
                assert item.status == "evidence_insufficient"
                assert item.missing
    item = judge_temporal_subitem("s73", "business_success_feedback", case)
    assert "瞬时提示记录" in item.reason and "trusted_business_success" in item.missing


def test_request_failure_and_media_end_remain_observations_without_module_and_handoff():
    case = evidence(
        ("har", {"format": "network_events", "entries": [{
            "event": "request_failed", "resource_type": "xhr", "url": "https://example.invalid",
        }]}),
        ("media_state", {"schema_version": "media-state.v1", "items": [{
            "selector": "video", "ended": True, "ready_state": 4, "current_time": 10,
        }]}),
    )
    assert "真实请求失败事件" in judge_temporal_subitem("s49", "failure_reason", case).reason
    item = judge_temporal_subitem("62", "presented_first_frame", case)
    assert "媒体状态快照" in item.reason
    assert "matching_first_frame_png" in item.missing
    assert item.status == "evidence_insufficient"


def test_unverified_input_and_wrong_rule_pair_are_rejected():
    with pytest.raises(ValueError, match="已核验"):
        read_temporal_observations(GreenEvidenceSet("task", "run", 0, "input", "hash", "scope"))
    with pytest.raises(ValueError, match="不支持"):
        judge_temporal_subitem("s72", "content_preload", evidence())


@pytest.mark.parametrize("rule_key", ["s72", "s49", "s73", "62"])
def test_batch_reads_originals_once_for_all_frozen_subitems(rule_key):
    case = evidence(("ui_snapshot", ui("网络异常", "重试")))
    expected = next(item.subchecks for item in GREEN_RULES if item.key == rule_key)
    with patch(
        "services.api.app.application.green_temporal_judge.read_temporal_observations",
        wraps=read_temporal_observations,
    ) as reader:
        items = judge_temporal_subitems(rule_key, case)
    reader.assert_called_once_with(case)
    assert tuple(item.key for item in items) == expected
    assert all(item.status == "evidence_insufficient" for item in items)


def test_batch_preserves_requested_order_and_shared_reader_issue():
    case = evidence(("ui_snapshot", b"{bad"))
    keys = ("retry_entry", "failure_reason")
    with patch(
        "services.api.app.application.green_temporal_judge.read_temporal_observations",
        wraps=read_temporal_observations,
    ) as reader:
        items = judge_temporal_subitems("s49", case, subchecks=keys)
    assert reader.call_count == 1
    assert tuple(item.key for item in items) == keys
    assert all("temporal_original_unreadable:original-0" in item.missing for item in items)


@pytest.mark.parametrize("keys", [(), ("content_preload",), ("retry_entry", "retry_entry")])
def test_batch_rejects_invalid_subcheck_selection_before_read(keys):
    with patch(
        "services.api.app.application.green_temporal_judge.read_temporal_observations",
    ) as reader, pytest.raises(ValueError, match="不支持"):
        judge_temporal_subitems("s49", evidence(), subchecks=keys)
    reader.assert_not_called()


def test_single_entry_preserves_injected_requirements_and_reads_once():
    case = evidence(("ui_snapshot", ui("暂无数据")))
    with patch(
        "services.api.app.application.green_temporal_judge.read_temporal_observations",
        wraps=read_temporal_observations,
    ) as reader:
        item = judge_temporal_subitem(
            "s72", "empty_state_meaning", case,
            required_facts={"empty_state_meaning": ("shared_requirement",)},
        )
    reader.assert_called_once_with(case)
    assert item.missing == ("shared_requirement",)


@pytest.mark.parametrize("field", ["selector", "visibility", "media_schema"])
def test_malformed_scalar_does_not_crash_or_promote_rendering(field):
    snapshot = ui("暂无数据")
    if field == "selector":
        snapshot["elements"][1]["selector"] = ["invalid"]
    elif field == "visibility":
        snapshot["elements"][1]["style"] = {"visibility": ["invalid"]}
    case = evidence(
        ("ui_snapshot", snapshot),
        ("media_state", {"schema_version": ["invalid"], "items": [{"ended": True}]}),
    )
    item = judge_temporal_subitem("s72", "empty_state_meaning", case)
    assert item.status == "evidence_insufficient"
    if field != "media_schema":
        assert "可见空态文案" not in item.reason
