"""官方群入口聚合规则（ops_7，2026-09-09 拍板）。

整任务判一次：有入口就看跳转；首页和个人中心都采过却哪都没有才判不过。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from services.api.app.application.aggregate_inspection_execution import (
    AggregateExecutionLease,
    AggregateJudgeContext,
    evaluate_aggregate_context,
)
from services.api.app.application.aggregate_rule_config import (
    resolve_rule_evidence_input_configuration,
)
from services.api.app.domain.evidence import (
    ArtifactReference,
    EvidenceInputCandidate,
    EvidenceInputScope,
    EvidenceQualityStatus,
    build_evidence_manifest_reference,
    seal_evidence_input_set,
)

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


def _manifest(capture_id: str, packet_id: str):
    return build_evidence_manifest_reference(
        task_id="task-1",
        session_id="session-1",
        capture_id=capture_id,
        manifest_version=1,
        artifacts=(
            ArtifactReference(
                artifact_id=f"artifact-{capture_id}",
                artifact_type="ui_snapshot",
                role="observation",
                storage_path=f"evidence/{capture_id}.json",
                checksum="sha256:" + "a" * 64,
                size_bytes=10,
                content_type="application/json",
            ),
        ),
        quality_status=EvidenceQualityStatus.PASS,
        quality_reason="artifact_integrity_verified",
        observation_packet_ids=(packet_id,),
        created_at=NOW,
    )


def _packet(
    *,
    page_type: str,
    visible_text: str,
    actions: list[dict[str, Any]] | None = None,
    dom_summary: dict[str, Any] | None = None,
    ui_snapshot: dict[str, Any] | None = None,
):
    facts: dict[str, Any] = {"visible_text": visible_text, "action_log": actions or []}
    if dom_summary is not None:
        facts["dom_summary"] = dom_summary
    if ui_snapshot is not None:
        facts["ui_snapshot"] = ui_snapshot
    return {
        "page": {"page_capture_id": f"capture-{page_type}", "page_type": page_type},
        "facts": facts,
    }


def _context(pages: dict[str, dict[str, Any]]) -> AggregateJudgeContext:
    candidates = tuple(
        EvidenceInputCandidate(
            manifest=_manifest(f"capture-{page_type}", f"packet-{page_type}"),
            capture_role="observation",
            page_sequence=index + 1,
        )
        for index, page_type in enumerate(pages)
    )
    input_set = seal_evidence_input_set(
        task_id="task-1",
        source_agent_run_id="run-1",
        rule_id="rule-ops-7",
        rule_code="alioth_ops_7",
        rule_contract_hash="contract-hash",
        ruleset_snapshot_hash="ruleset-hash",
        scope=EvidenceInputScope.TASK,
        scope_key="official_group_entry",
        finalization_epoch=1,
        eligibility_snapshot_id="snapshot-1",
        eligibility_snapshot_hash="snapshot-hash",
        selection_policy_hash="policy-hash",
        eligible_capture_ids=tuple(f"capture-{page_type}" for page_type in pages),
        candidates=candidates,
        sealed_at=NOW,
    )
    return AggregateJudgeContext(
        lease=AggregateExecutionLease(
            work_item_id="work-1",
            task_id="task-1",
            evidence_input_set_id=input_set.input_set_id,
            input_hash=input_set.input_set_hash,
            rule_id="rule-ops-7",
            rule_code="alioth_ops_7",
            rule_contract_hash="contract-hash",
            scope="task",
            scope_key="official_group_entry",
            finalization_epoch=1,
            consumer_agent_run_id="run-1",
            lease_token="lease-1",
            attempt_no=1,
            row_version=1,
        ),
        input_set=input_set,
        observation_packets={f"packet-{page_type}": packet for page_type, packet in pages.items()},
    )


def test_scope_key_is_registered_as_executable_task_aggregate() -> None:
    configuration = resolve_rule_evidence_input_configuration(
        {
            "capability_config": {
                "evidence_input_scope": "task",
                "evidence_scope_key": "official_group_entry",
            }
        },
        require_executable=True,
    )
    assert configuration.is_aggregate is True
    assert configuration.scope_key == "official_group_entry"


def test_entry_clicked_and_jumped_to_group_passes() -> None:
    context = _context(
        {
            "home": _packet(page_type="home", visible_text="首页 推荐 最新 官方群 客服"),
            "profile": _packet(
                page_type="profile",
                visible_text="我的 会员中心 官方群组 设置",
                actions=[
                    {
                        "action": "click",
                        "semantic_target": "官方群组",
                        "after_url": "https://t.me/example_group",
                        "transition_kind": "external",
                        "action_execution_id": "action-1",
                    }
                ],
            ),
        }
    )

    judgement = evaluate_aggregate_context(context)

    assert judgement.execution_status == "pass"
    assert judgement.wrapper_payload["outcome"] == "entry_usable"
    entries = judgement.wrapper_payload["entries"]
    assert [item["page_type"] for item in entries] == ["home", "profile"]
    assert judgement.wrapper_payload["jumps"][0]["landing_token"] == "t.me/"
    assert judgement.wrapper_payload["jumps"][0]["action_execution_id"] == "action-1"


def test_entry_with_group_link_shown_on_page_passes_without_click() -> None:
    context = _context(
        {
            "home": _packet(page_type="home", visible_text="首页 推荐"),
            "settings": _packet(
                page_type="settings",
                visible_text="设置 官方交流群：https://t.me/example_group 扫码加群",
            ),
        }
    )

    judgement = evaluate_aggregate_context(context)

    assert judgement.execution_status == "pass"
    assert judgement.wrapper_payload["links_shown"][0]["page_type"] == "settings"


def test_visible_entry_without_click_is_evidence_insufficient_not_fail() -> None:
    context = _context(
        {
            "home": _packet(page_type="home", visible_text="首页 推荐"),
            "profile": _packet(page_type="profile", visible_text="我的 官方群 分享推广"),
        }
    )

    judgement = evaluate_aggregate_context(context)

    assert judgement.execution_status == "evidence_insufficient"
    assert judgement.error_code == "OFFICIAL_GROUP_ENTRY_NOT_CLICKED"
    assert judgement.review_status == "pending"


def test_icon_entry_found_in_dom_structure_is_not_judged_missing() -> None:
    """图标入口没有可见文字，无障碍标签说明它是官方群入口：算看见了，只是没点。"""
    context = _context(
        {
            "home": _packet(
                page_type="home",
                visible_text="首页 推荐 最新 排行",
                dom_summary={
                    "elements": [
                        {"tag": "a", "aria_label": "官方群", "href": "/community/join"},
                    ]
                },
            ),
            "profile": _packet(page_type="profile", visible_text="我的 会员中心 设置 客服"),
        }
    )

    judgement = evaluate_aggregate_context(context)

    # 看见了入口但没点：是证据不足，不是「整个产品没入口」的未通过
    assert judgement.execution_status == "evidence_insufficient"
    assert judgement.error_code == "OFFICIAL_GROUP_ENTRY_NOT_CLICKED"
    assert judgement.review_status == "pending"


def test_group_link_in_dom_href_counts_as_entry_and_join_method() -> None:
    context = _context(
        {
            "home": _packet(
                page_type="home",
                visible_text="首页 推荐",
                dom_summary={
                    "elements": [
                        {"tag": "a", "aria_label": "Telegram", "href": "https://t.me/example_group"},
                    ]
                },
            ),
            "profile": _packet(page_type="profile", visible_text="我的 设置"),
        }
    )

    judgement = evaluate_aggregate_context(context)

    assert judgement.execution_status == "pass"
    assert judgement.wrapper_payload["links_shown"][0]["source"] == "structure"
    assert judgement.wrapper_payload["links_shown"][0]["token"] == "t.me/"


def test_app_node_content_desc_counts_as_entry() -> None:
    context = _context(
        {
            "home": _packet(
                page_type="home",
                visible_text="首页 推荐",
                ui_snapshot={
                    "nodes": [
                        {"class_name": "android.widget.ImageView", "content_desc": "加入官方群"},
                    ]
                },
            ),
            "profile": _packet(page_type="profile", visible_text="我的 设置"),
        }
    )

    judgement = evaluate_aggregate_context(context)

    assert judgement.execution_status == "evidence_insufficient"
    assert judgement.error_code == "OFFICIAL_GROUP_ENTRY_NOT_CLICKED"


def test_no_entry_anywhere_after_sampling_home_and_profile_fails_once() -> None:
    context = _context(
        {
            "home": _packet(page_type="home", visible_text="首页 推荐 最新 排行"),
            "profile": _packet(page_type="profile", visible_text="我的 会员中心 设置 客服"),
            "activity": _packet(page_type="activity", visible_text="活动 领福利"),
        }
    )

    judgement = evaluate_aggregate_context(context)

    assert judgement.execution_status == "fail"
    assert judgement.result_summary == "整个产品未发现官方群入口"
    assert "activity、home、profile" in (judgement.problem_desc or "")
    assert judgement.wrapper_payload["outcome"] == "entry_missing"


def test_no_entry_but_home_and_profile_not_sampled_is_evidence_insufficient() -> None:
    context = _context(
        {
            "activity": _packet(page_type="activity", visible_text="活动 领福利"),
            "settings": _packet(page_type="settings", visible_text="设置 清理缓存"),
        }
    )

    judgement = evaluate_aggregate_context(context)

    assert judgement.execution_status == "evidence_insufficient"
    assert judgement.error_code == "OFFICIAL_GROUP_ENTRY_PAGES_NOT_SAMPLED"
