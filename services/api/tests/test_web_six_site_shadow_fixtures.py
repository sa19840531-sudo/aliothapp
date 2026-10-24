"""六站审计菜单固件与生产 Catalog 的离线 shadow 对账。"""

import json
import math
import urllib.error
import urllib.parse
from collections import Counter
from dataclasses import replace
from hashlib import sha256
from io import BytesIO
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import pytest

from scripts.web_v2_six_site_shadow import (
    DEFAULT_CATALOG_PATH,
    DEFAULT_LIVE_SITE_TIMEOUT_SECONDS,
    EXPECTED_LIVE_EXECUTED_MENU_COUNT,
    EXPECTED_LIVE_MENU_LIMIT_SKIPPED_COUNT,
    SIX_SITE_ACCEPTANCE_FIXTURES,
    SIX_SITE_TARGETS,
    LiveSiteResult,
    LiveSixSiteReport,
    _aggregate_historical_fixture_results,
    _aggregate_live_results,
    _apply_manual_report_review,
    _build_live_process_nodes,
    _build_live_screenshot_selections,
    _hydrate_preview_exploration_summary,
    _markdown_cell,
    _markdown_identifier,
    _normalize_live_site_evidence_bundle,
    _normalize_live_targets,
    _RejectRedirectHandler,
    _required_capture_ids_from_ledger,
    _validate_api_base_url,
    _validate_live_acceptance_dimensions,
    _validate_live_ledger_entry,
    _validate_live_preview_site,
    _validate_live_site_evidence_bundle,
    _write_stdout_utf8,
    main,
    reconcile_all_fixture_sites,
    render_live_html,
    render_live_markdown,
    run_live_sites,
    submit_live_sites,
)
from scripts.web_v2_six_site_shadow import (
    WEB_MENU_COLLECTION_LIMIT as LIVE_WEB_MENU_COLLECTION_LIMIT,
)
from services.api.app.application.v2_task_entries import (
    WEB_FULL_SCAN_TIMEOUT_MINUTES,
)
from services.api.app.application.web_list_detail_navigation import (
    detail_candidates_from_ready_snapshot,
)
from services.api.app.application.web_logical_page import (
    build_web_logical_pages,
    canonical_web_logical_page_url,
    resolve_web_logical_page_role,
    web_logical_page_id,
)
from services.api.app.application.web_menu_exploration import plan_web_menu_collection
from services.api.app.application.web_page_classification import (
    web_page_structure_facts,
)
from services.api.app.domain.page.web_site_menu_registry import WebSiteMenuRegistry

# 六站离线证据固定使用历史十菜单策略，不随新实况默认值改变。
WEB_MENU_COLLECTION_LIMIT = 10


def _fixture_uuid(label: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"alioth:web-v2-test:{label}"))


def _fixture_action_execution_id(label: str) -> str:
    digest = sha256(f"alioth:web-v2-test:{label}".encode()).hexdigest()
    return f"action-execution:{digest}"


def _fixture_web_evidence_bundle() -> dict[str, object]:
    home_probe = _fixture_uuid("bundle:home:probe")
    home_ready = _fixture_uuid("bundle:home:ready")
    menu_probe = _fixture_uuid("bundle:menu:probe")
    menu_ready = _fixture_uuid("bundle:menu:ready")
    capture_ids = (home_probe, home_ready, menu_probe, menu_ready)
    return {
        "task_id": _fixture_uuid("bundle:task"),
        "agent_run_id": _fixture_uuid("bundle:run"),
        "site_id": "heiliao_com",
        "status": "evidence_ready",
        "reason_codes": [],
        "summary": {
            "home_count": 1,
            "menu_count": 1,
            "detail_count": 1,
            "duplicate_formal_url_count": 0,
        },
        "entries": [
            {
                "menu_entry_id": "home",
                "status": "home_baseline",
                "menu_action_count": 0,
                "menu_action_execution_id": None,
                "menu_probe_capture_id": home_probe,
                "menu_ready_capture_id": home_ready,
                "menu_page_type": "home",
                "detail_action_count": 0,
                "detail_action_execution_id": None,
                "detail_probe_capture_id": None,
                "detail_ready_capture_id": None,
                "detail_page_type": None,
            },
            {
                "menu_entry_id": "menu-1",
                "status": "detail_captured",
                "menu_action_count": 1,
                "menu_action_execution_id": _fixture_action_execution_id("bundle:menu:action"),
                "menu_probe_capture_id": menu_probe,
                "menu_ready_capture_id": menu_ready,
                "menu_page_type": "content_detail",
                "detail_action_count": 0,
                "detail_action_execution_id": None,
                "detail_probe_capture_id": None,
                "detail_ready_capture_id": menu_ready,
                "detail_page_type": "content_detail",
            },
        ],
        "captures": [
            {
                "capture_id": capture_id,
                "parent_capture_id": None,
                "url": (
                    "https://heiliao.com/"
                    if capture_id in {home_probe, home_ready}
                    else "https://heiliao.com/path"
                ),
                "role": "after_action",
                "page_type": (
                    "home" if capture_id in {home_probe, home_ready} else "content_detail"
                ),
                "action_execution_id": (
                    _fixture_action_execution_id("bundle:menu:action")
                    if capture_id == menu_probe
                    else _fixture_action_execution_id(f"bundle:capture-action:{index}")
                ),
                "artifacts": [
                    {
                        "artifact_id": _fixture_uuid(f"bundle:{capture_id}:screenshot"),
                        "artifact_type": "screenshot",
                        "content_type": "image/png",
                        "checksum": "sha256:" + "a" * 64,
                    },
                    {
                        "artifact_id": _fixture_uuid(f"bundle:{capture_id}:dom"),
                        "artifact_type": "dom",
                        "content_type": "text/html; charset=utf-8",
                        "checksum": "sha256:" + "b" * 64,
                    },
                    {
                        "artifact_id": _fixture_uuid(f"bundle:{capture_id}:action"),
                        "artifact_type": "action_trace",
                        "content_type": "application/json",
                        "checksum": "sha256:" + "c" * 64,
                    },
                ],
            }
            for index, capture_id in enumerate(capture_ids, start=1)
        ],
        "formal_pages": [
            {
                "logical_page_id": web_logical_page_id("https://heiliao.com/"),
                "logical_page_url": "https://heiliao.com/",
                "primary_capture_id": home_ready,
                "capture_purpose": "web_page_ready_baseline",
                "page_type": "content_list",
                "observation_capture_ids": [home_ready],
                "product_capabilities": ["content", "navigation"],
                "entry_status": "known",
                "visible_layer": "business",
            },
            {
                "logical_page_id": web_logical_page_id("https://heiliao.com/path"),
                "logical_page_url": "https://heiliao.com/path",
                "primary_capture_id": menu_ready,
                "capture_purpose": "web_page_ready_baseline",
                "page_type": "content_detail",
                "observation_capture_ids": [menu_ready],
                "product_capabilities": ["content"],
                "entry_status": "known",
                "visible_layer": "business",
            },
        ],
        "storage_path": "secret/path",
        "raw_dom": "<script>token=secret</script>",
    }


def _fixture_web_evidence_from_report_ledger(
    *,
    task_id: str,
    run_id: str,
    site_id: str,
    target_url: str,
    ledger: list[dict[str, object]],
) -> dict[str, object]:
    public_keys = (
        "status",
        "logical_target_identity",
        "parent_logical_target_identity",
        "exploration_path",
        "menu_action_count",
        "menu_action_execution_id",
        "menu_probe_capture_id",
        "menu_ready_capture_id",
        "menu_page_type",
        "detail_action_count",
        "detail_action_execution_id",
        "detail_probe_capture_id",
        "detail_ready_capture_id",
        "detail_page_type",
    )
    entries = [
        {
            "menu_entry_id": item["entry_id"],
            **{key: item.get(key) for key in public_keys},
        }
        for item in ledger
    ]
    capture_types: dict[str, str] = {}
    capture_actions: dict[str, str] = {}
    capture_urls: dict[str, str] = {}
    for ledger_entry, entry in zip(ledger, entries, strict=True):
        for capture_key, page_key in (
            ("menu_probe_capture_id", "menu_page_type"),
            ("menu_ready_capture_id", "menu_page_type"),
            ("detail_probe_capture_id", "detail_page_type"),
            ("detail_ready_capture_id", "detail_page_type"),
        ):
            capture_id = str(entry.get(capture_key) or "")
            page_type = str(entry.get(page_key) or "")
            if capture_id and page_type:
                capture_types[capture_id] = page_type
                parsed_capture_url = urllib.parse.urlsplit(
                    str(ledger_entry.get("canonical_url") or target_url)
                )
                capture_urls[capture_id] = urllib.parse.urlunsplit(
                    (
                        parsed_capture_url.scheme,
                        parsed_capture_url.netloc,
                        parsed_capture_url.path or "/",
                        "",
                        "",
                    )
                )
        for capture_key, action_key in (
            ("menu_probe_capture_id", "menu_action_execution_id"),
            ("detail_probe_capture_id", "detail_action_execution_id"),
        ):
            capture_id = str(entry.get(capture_key) or "")
            action_id = str(entry.get(action_key) or "")
            if capture_id and action_id:
                capture_actions[capture_id] = action_id
    captures = []
    for capture_id, page_type in sorted(capture_types.items()):
        captures.append(
            {
                "capture_id": capture_id,
                "parent_capture_id": None,
                "url": capture_urls.get(capture_id, target_url),
                "role": "after_action",
                "capture_purpose": "web_page_ready_baseline",
                "page_type": page_type,
                "action_execution_id": capture_actions.get(
                    capture_id,
                    _fixture_action_execution_id(f"evidence-action:{capture_id}"),
                ),
                "artifacts": [
                    {
                        "artifact_id": _fixture_uuid(f"evidence:{capture_id}:screenshot"),
                        "artifact_type": "screenshot",
                        "content_type": "image/png",
                        "checksum": "sha256:" + "a" * 64,
                    },
                    {
                        "artifact_id": _fixture_uuid(f"evidence:{capture_id}:dom"),
                        "artifact_type": "dom",
                        "content_type": "text/html; charset=utf-8",
                        "checksum": "sha256:" + "b" * 64,
                    },
                    {
                        "artifact_id": _fixture_uuid(f"evidence:{capture_id}:action"),
                        "artifact_type": "action_trace",
                        "content_type": "application/json",
                        "checksum": "sha256:" + "c" * 64,
                    },
                ],
            }
        )
    formal_pages_by_identity: dict[str, dict[str, object]] = {}
    for ledger_entry, entry in zip(ledger, entries, strict=True):
        primary_capture_id = str(
            entry.get("detail_ready_capture_id")
            or entry.get("menu_ready_capture_id")
            or ""
        )
        page_type = str(
            entry.get("detail_page_type") or entry.get("menu_page_type") or ""
        )
        canonical_url = str(ledger_entry.get("canonical_url") or "")
        if not primary_capture_id or not page_type or not canonical_url:
            continue
        parsed_url = urllib.parse.urlsplit(canonical_url)
        public_url = urllib.parse.urlunsplit(
            (parsed_url.scheme, parsed_url.netloc, parsed_url.path or "/", "", "")
        )
        logical_page_id = web_logical_page_id(canonical_url)
        formal_pages_by_identity.setdefault(
            logical_page_id,
            {
                "logical_page_id": logical_page_id,
                "logical_page_url": public_url,
                "primary_capture_id": primary_capture_id,
                "capture_purpose": "web_page_ready_baseline",
                "page_type": "content_list" if page_type == "home" else page_type,
                "observation_capture_ids": [primary_capture_id],
                "product_capabilities": ["content", "navigation"],
                "entry_status": "known",
                "visible_layer": "business",
            },
        )
    return _normalize_live_site_evidence_bundle(
        {
            "task_id": task_id,
            "agent_run_id": run_id,
            "site_id": site_id,
            "status": "evidence_ready",
            "reason_codes": [],
            "summary": {
                "home_count": sum(item["status"] == "home_baseline" for item in ledger),
                "menu_count": sum(int(item.get("menu_action_count") or 0) for item in ledger),
                "detail_count": sum(item["status"] == "detail_captured" for item in ledger),
                "duplicate_formal_url_count": 0,
            },
            "entries": entries,
            "captures": captures,
            "formal_pages": list(formal_pages_by_identity.values()),
        }
    )


def test_web_evidence_bundle_requires_url_png_dom_and_exact_counts() -> None:
    raw = _fixture_web_evidence_bundle()
    raw["captures"].append(
        {
            "capture_id": _fixture_uuid("bundle:bootstrap"),
            "parent_capture_id": None,
            "url": "https://heiliao.com/",
            "role": "observation",
            "page_type": "home",
            "action_execution_id": None,
            "artifacts": [
                {
                    "artifact_id": _fixture_uuid("bundle:bootstrap:screenshot"),
                    "artifact_type": "screenshot",
                    "content_type": "image/png",
                    "checksum": "sha256:" + "a" * 64,
                },
                {
                    "artifact_id": _fixture_uuid("bundle:bootstrap:dom"),
                    "artifact_type": "dom",
                    "content_type": "text/html; charset=utf-8",
                    "checksum": "sha256:" + "b" * 64,
                },
            ],
        }
    )
    normalized = _normalize_live_site_evidence_bundle(raw)

    assert "storage_path" not in repr(normalized)
    assert "raw_dom" not in repr(normalized)
    assert _required_capture_ids_from_ledger(normalized["entries"]) == {
        _fixture_uuid("bundle:home:probe"),
        _fixture_uuid("bundle:home:ready"),
        _fixture_uuid("bundle:menu:probe"),
        _fixture_uuid("bundle:menu:ready"),
    }
    errors = _validate_live_site_evidence_bundle(
        target_url=SIX_SITE_TARGETS[0],
        task_id=_fixture_uuid("bundle:task"),
        run_id=_fixture_uuid("bundle:run"),
        site_id="heiliao_com",
        expected_menu_count=1,
        bundle=normalized,
    )
    assert errors == []

    normalized["captures"][0]["artifacts"] = [
        item for item in normalized["captures"][0]["artifacts"] if item["artifact_type"] != "dom"
    ]
    errors = _validate_live_site_evidence_bundle(
        target_url=SIX_SITE_TARGETS[0],
        task_id=_fixture_uuid("bundle:task"),
        run_id=_fixture_uuid("bundle:run"),
        site_id="heiliao_com",
        expected_menu_count=1,
        bundle=normalized,
    )
    assert any("DOM" in item for item in errors)


def test_web_evidence_bundle_validates_unreferenced_action_closure_capture() -> None:
    raw = _fixture_web_evidence_bundle()
    raw["captures"].append(
        {
            "capture_id": _fixture_uuid("bundle:extra-before"),
            "parent_capture_id": _fixture_uuid("bundle:extra-parent"),
            "url": "https://heiliao.com/path?token=secret",
            "role": "before_action",
            "page_type": None,
            "action_execution_id": _fixture_action_execution_id("bundle:extra-action"),
            "artifacts": [
                {
                    "artifact_id": _fixture_uuid("bundle:extra:screenshot"),
                    "artifact_type": "screenshot",
                    "content_type": "image/png",
                    "checksum": "sha256:" + "a" * 64,
                },
                {
                    "artifact_id": _fixture_uuid("bundle:extra:dom"),
                    "artifact_type": "dom",
                    "content_type": "text/html; charset=utf-8",
                    "checksum": "sha256:" + "b" * 64,
                },
            ],
        }
    )

    errors = _validate_live_site_evidence_bundle(
        target_url=SIX_SITE_TARGETS[0],
        task_id=_fixture_uuid("bundle:task"),
        run_id=_fixture_uuid("bundle:run"),
        site_id="heiliao_com",
        expected_menu_count=1,
        bundle=_normalize_live_site_evidence_bundle(raw),
    )

    assert any("缺少脱敏 URL" in item for item in errors)


def test_live_html_is_static_and_escapes_untrusted_values() -> None:
    bundle = _normalize_live_site_evidence_bundle(_fixture_web_evidence_bundle())
    site = LiveSiteResult(
        target_url=SIX_SITE_TARGETS[0],
        task_id=_fixture_uuid("bundle:task"),
        run_id=_fixture_uuid("bundle:run"),
        task_status="done",
        report_status="not_applicable",
        failure_reason="<script>token=secret</script>",
        report_stage="final",
        is_final=True,
        exploration_summary={},
        evidence_bundle=bundle,
    )

    rendered = render_live_html(_aggregate_historical_fixture_results((site,)))

    assert "<script" not in rendered.casefold()
    assert "token=secret" not in rendered
    assert "storage_path" not in rendered
    assert "raw_dom" not in rendered
    assert "&lt;" in rendered or "redacted-sensitive" in rendered


def test_live_report_exposes_required_acceptance_dimensions_without_claiming_manual_review(
) -> None:
    """删除任一验收维度时，本测试应阻止六站报告退化成只有聚合数字。"""

    site = LiveSiteResult(
        target_url=SIX_SITE_TARGETS[0],
        task_id=_fixture_uuid("bundle:task"),
        run_id=_fixture_uuid("bundle:run"),
        task_status="review",
        report_status="ready",
        failure_reason=None,
        report_stage="preview",
        is_final=False,
        exploration_summary={
            "menu_ledger": [
                {
                    "menu_path": ["首页"],
                    "ad_landing_evidence": {
                        "status": "observed",
                        "source_url": "https://heiliao.com/path?secret=source",
                        "landing_url": "https://ads.example.net/campaign?secret=landing",
                        "candidate_kind": "popup",
                        "capture_id": _fixture_uuid("bundle:ad:capture"),
                        "artifact_ids": [
                            _fixture_uuid("bundle:ad:source-dom"),
                            _fixture_uuid("bundle:ad:landing-dom"),
                        ],
                        "artifact_refs": {
                            "source_dom": _fixture_uuid("bundle:ad:source-dom"),
                            "source_screenshot": _fixture_uuid(
                                "bundle:ad:source-screenshot"
                            ),
                            "landing_dom": _fixture_uuid("bundle:ad:landing-dom"),
                            "landing_screenshot": _fixture_uuid(
                                "bundle:ad:landing-screenshot"
                            ),
                        },
                    },
                },
                {
                    "menu_path": ["分类"],
                    "ad_landing_evidence": {
                        "status": "no_candidate",
                        "source_url": "https://heiliao.com/category",
                        "landing_url": None,
                        "candidate_kind": None,
                        "capture_id": None,
                        "artifact_ids": [],
                        "reason_code": "WEB_AD_CANDIDATE_NOT_FOUND",
                    },
                },
            ]
        },
        report_url=(
            "https://api.example.test/api/v1/workstation/reports/"
            f"{_fixture_uuid('bundle:task')}?agent_run_id={_fixture_uuid('bundle:run')}"
        ),
        workstation_report_path=f"/reports?task={_fixture_uuid('bundle:task')}",
        page_type_review_count=2,
        pending_confirmation_count=1,
        duplicate_url_count=0,
        screenshot_selections=(
            {
                "logical_page_url": "https://heiliao.com/path",
                "primary_capture_id": _fixture_uuid("bundle:home:ready"),
                "screenshot_artifact_id": _fixture_uuid(
                    f"bundle:{_fixture_uuid('bundle:home:ready')}:screenshot"
                ),
                "capture_purpose": "web_page_ready_baseline",
                "obstruction_capture_id": None,
                "before_ad_coverage": None,
                "after_ad_coverage": None,
            },
        ),
    )
    report = _aggregate_historical_fixture_results((site,))

    markdown = render_live_markdown(report)
    html_text = render_live_html(report)

    assert report.manual_report_review_passed is False
    assert report.passed is False

    for required in (
        "代码测试",
        "离线固定样本",
        "真实站点运行",
        "人工复核",
        "Completion Gate",
        "报告入口",
        "页面类型待复核：2",
        "重复正式 URL：0",
        "截图选择",
        "任务列表：未验证",
        "任务详情：未验证",
    ):
        assert required in markdown
    for required in (
        "代码测试",
        "离线固定样本",
        "真实站点运行",
        "人工复核",
        "Completion Gate",
        "报告入口",
        "页面类型待复核",
        "重复正式 URL",
        "截图选择",
        "任务列表：未验证",
        "任务详情：未验证",
    ):
        assert required in html_text
    assert ">2</dd>" in html_text
    assert ">0</dd>" in html_text
    assert site.report_url in markdown
    assert site.workstation_report_path in markdown
    assert "广告落地证据" in markdown
    assert "popup" in markdown
    assert "source\\_dom=" in markdown
    assert "landing\\_screenshot=" in markdown
    assert "WEB\\_AD\\_CANDIDATE\\_NOT\\_FOUND" in markdown
    assert "ads.example.net/campaign" in markdown
    assert "secret=source" not in markdown
    assert "secret=landing" not in markdown
    assert "广告落地证据" in html_text
    assert "WEB_AD_CANDIDATE_NOT_FOUND" in html_text
    assert "ads.example.net/campaign" in html_text
    assert "secret=source" not in html_text
    assert "secret=landing" not in html_text


def test_manual_report_review_requires_explicit_per_site_confirmation() -> None:
    site = LiveSiteResult(
        target_url=SIX_SITE_TARGETS[0],
        task_id=_fixture_uuid("manual-review:task"),
        run_id=_fixture_uuid("manual-review:run"),
        task_status="done",
        report_status="not_applicable",
        failure_reason=None,
        report_stage="final",
        is_final=True,
        exploration_summary={},
        server_completion_gate_passed=True,
        server_report_bound=True,
    )
    report = LiveSixSiteReport(
        site_count=1,
        menu_total=0,
        menu_limit=10,
        executed_same_origin_count=0,
        expected_executed_same_origin_count=0,
        menu_limit_skipped_count=0,
        expected_menu_limit_skipped_count=0,
        home_baseline_count=0,
        detail_count=0,
        blocked_cross_origin_count=0,
        frontier_remaining=0,
        validation_errors=(),
        sites=(site,),
    )

    reviewed = _apply_manual_report_review(
        report,
        reviewer=lambda _site: (True, True),
    )

    assert report.passed is False
    assert reviewed.sites[0].task_list_report_verified is True
    assert reviewed.sites[0].task_detail_report_verified is True
    assert reviewed.passed is False
    assert reviewed.sites[0].acceptance_dimensions["manual_review"]["status"] == "not_recorded"


def test_completion_gate_is_reported_independently_from_live_and_manual_review() -> None:
    """人工入口通过不能替代服务端 Gate 与不可变报告绑定。"""

    site = LiveSiteResult(
        target_url=SIX_SITE_TARGETS[0],
        task_id=_fixture_uuid("gate-layer:task"),
        run_id=_fixture_uuid("gate-layer:run"),
        task_status="review",
        report_status="ready",
        failure_reason=None,
        report_stage="preview",
        is_final=False,
        exploration_summary={},
        task_list_report_verified=True,
        task_detail_report_verified=True,
        server_completion_gate_passed=False,
        server_report_bound=False,
    )
    report = LiveSixSiteReport(
        site_count=1,
        menu_total=0,
        menu_limit=10,
        executed_same_origin_count=0,
        expected_executed_same_origin_count=0,
        menu_limit_skipped_count=0,
        expected_menu_limit_skipped_count=0,
        home_baseline_count=0,
        detail_count=0,
        blocked_cross_origin_count=0,
        frontier_remaining=0,
        validation_errors=(),
        sites=(site,),
    )

    assert report.live_validation_passed is True
    assert report.manual_report_review_passed is False
    assert report.server_completion_gate_passed is False
    assert report.passed is False
    assert "Completion Gate：未通过" in render_live_markdown(report)


def test_live_acceptance_requires_every_formal_page_typed_inspected_and_screenshoted() -> None:
    bundle = _normalize_live_site_evidence_bundle(_fixture_web_evidence_bundle())
    pages = bundle["formal_pages"]
    site = LiveSiteResult(
        target_url=SIX_SITE_TARGETS[0],
        task_id=_fixture_uuid("bundle:task"),
        run_id=_fixture_uuid("bundle:run"),
        task_status="done",
        report_status="ready",
        failure_reason=None,
        report_stage="final",
        is_final=True,
        exploration_summary={},
        evidence_bundle=bundle,
        page_type_review_count=0,
        pending_confirmation_count=0,
        duplicate_url_count=0,
        inspection_total_count=2,
        inspection_pending_count=0,
        logical_page_assessments=tuple(
            {
                "logical_page_id": page["logical_page_id"],
                "canonical_url": page["logical_page_url"],
                "primary_capture_id": page["primary_capture_id"],
                "page_type": page["page_type"],
                "classification_status": "classified",
                "inspection_status": "completed",
            }
            for page in pages
        ),
        screenshot_selections=_build_live_screenshot_selections(bundle),
        server_completion_gate_passed=True,
        server_report_bound=True,
    )

    assert _validate_live_acceptance_dimensions(site) == []

    duplicate = replace(site, duplicate_url_count=1)
    assert any(
        "重复正式 URL" in error
        for error in _validate_live_acceptance_dimensions(duplicate)
    )

    untyped = replace(
        site,
        logical_page_assessments=(
            {**site.logical_page_assessments[0], "page_type": None},
            site.logical_page_assessments[1],
        ),
    )
    assert any(
        "未完成页面类型判定" in error
        for error in _validate_live_acceptance_dimensions(untyped)
    )


def test_live_process_nodes_expand_probe_ready_and_direct_detail_projection() -> None:
    bundle = _normalize_live_site_evidence_bundle(_fixture_web_evidence_bundle())

    nodes = _build_live_process_nodes(
        submission_status="queued",
        task_status="done",
        report_stage="final",
        evidence_bundle=bundle,
    )
    phases = [str(node["phase"]) for node in nodes]

    assert phases[0] == "task_submitted"
    assert phases[-3:] == ["task_terminal", "report_sealed", "evidence_sealed"]
    assert "home_probe" in phases
    assert "home_ready" in phases
    assert "menu_action" in phases
    assert "menu_probe" in phases
    assert "menu_ready" in phases
    assert "menu_classified" in phases
    assert "detail_projected" in phases
    assert "detail_action" not in phases


def test_video_list_may_capture_content_detail_with_exactly_one_detail_action() -> None:
    menu_probe = _fixture_uuid("cross-type:menu-probe")
    menu_ready = _fixture_uuid("cross-type:menu-ready")
    detail_probe = _fixture_uuid("cross-type:detail-probe")
    detail_ready = _fixture_uuid("cross-type:detail-ready")
    candidate_id = "web-candidate:" + "d" * 64
    errors = _validate_live_ledger_entry(
        "https://example.test/",
        "https://example.test",
        {
            "entry_id": "menu-list",
            "menu_path": ["视频列表"],
            "href": "/videos",
            "decision": "allowlisted_observe_only",
        },
        {
            "entry_id": "menu-list",
            "menu_path": ["视频列表"],
            "canonical_url": "https://example.test/videos",
            "status": "detail_captured",
            "source_capture_id": _fixture_uuid("cross-type:source"),
            "final_capture_id": menu_ready,
            "page_type": "video_list",
            "menu_action_count": 1,
            "menu_intent_id": _fixture_uuid("cross-type:menu-intent"),
            "menu_action_execution_id": _fixture_action_execution_id("cross-type:menu-action"),
            "menu_probe_capture_id": menu_probe,
            "menu_ready_capture_id": menu_ready,
            "menu_page_type": "video_list",
            "menu_readiness_evidence_capture_ids": [menu_probe, menu_ready],
            "detail_action_count": 1,
            "detail_intent_id": _fixture_uuid("cross-type:detail-intent"),
            "detail_action_execution_id": _fixture_action_execution_id("cross-type:detail-action"),
            "detail_candidate_ids": [candidate_id],
            "selected_detail_candidate_id": candidate_id,
            "detail_selection_confidence": 0.96,
            "detail_selection_reason": "首个可信重复内容卡片",
            "detail_capture_id": detail_ready,
            "detail_probe_capture_id": detail_probe,
            "detail_ready_capture_id": detail_ready,
            "detail_page_type": "content_detail",
            "detail_readiness_evidence_capture_ids": [detail_probe, detail_ready],
        },
    )

    assert errors == []


@pytest.mark.parametrize(
    ("origin", "href", "ancestor_tokens"),
    (
        ("https://heiliao.com", "/archives/113850/", ["video-item", "video-list"]),
        ("https://hl365.com", "/archives/220555.html", ["archive", "post-list"]),
        ("https://www.91jav1.com", "/videos/FSTU-032", ["video-img-box", "grid"]),
        ("https://www.91jav1.com", "/article/58600", ["article-card", "grid"]),
        ("https://avhub360.com", "/article/index/id/58344", ["video-card", "grid"]),
        ("http://huangguoai.com", "/detail/3690/", ["hg-drama-card", "grid"]),
    ),
)
def test_six_site_detail_patterns_survive_offscreen_dom_extraction(
    origin: str,
    href: str,
    ancestor_tokens: list[str],
) -> None:
    candidates = detail_candidates_from_ready_snapshot(
        {
            "capture_id": _fixture_uuid(f"detail:{origin}:{href}"),
            "structure_signature": "six-site-detail-pattern",
            "elements": [
                {
                    "selector": "main article a.primary-detail",
                    "tag": "a",
                    "role": "link",
                    "text": "主详情",
                    "href": href,
                    "rect": {"x": 100, "y": 1600, "width": 300, "height": 180},
                    "interactive": True,
                    "visible": True,
                    "hit_test": {"center_hits_target": False},
                    "within_repeated_structure": True,
                    "repeat_group_size": 8,
                    "ancestor_class_tokens": ancestor_tokens,
                }
            ],
        },
        list_page_type="video_list",
        origin=origin,
    )

    assert len(candidates) == 1
    assert urllib.parse.urlsplit(candidates[0].canonical_url).path == href


def test_huangguo_drama_cards_are_media_primary_cards_without_video_tag() -> None:
    facts = web_page_structure_facts(
        {
            "elements": [
                {
                    "selector": f"#drama-{index} a",
                    "tag": "a",
                    "role": "link",
                    "text": f"剧集 {index}",
                    "href": f"/detail/{3690 + index}/",
                    "visible": True,
                    "interactive": True,
                    "within_repeated_structure": True,
                    "repeat_group_size": 4,
                    "ancestor_class_tokens": ["hg-drama-card", "poster-grid"],
                    "rect": {
                        "x": 100 + index * 320,
                        "y": 220,
                        "width": 300,
                        "height": 180,
                    },
                }
                for index in range(4)
            ]
        },
        origin="http://huangguoai.com",
        current_url="http://huangguoai.com/drama",
    )

    assert facts.video_element_count == 0
    assert facts.primary_detail_link_count == 4
    assert facts.repeated_primary_card_count == 4
    assert facts.media_card_count == 4


def test_live_site_wait_budget_exceeds_web_full_scan_deadline() -> None:
    assert WEB_FULL_SCAN_TIMEOUT_MINUTES == 180
    assert DEFAULT_LIVE_SITE_TIMEOUT_SECONDS == 11_400.0
    assert DEFAULT_LIVE_SITE_TIMEOUT_SECONDS > WEB_FULL_SCAN_TIMEOUT_MINUTES * 60
    assert LIVE_WEB_MENU_COLLECTION_LIMIT is None


def test_cli_writes_unicode_json_as_utf8_bytes_on_non_utf8_console() -> None:
    stream = SimpleNamespace(buffer=BytesIO())

    _write_stdout_utf8('{"marker":"•"}\n', stream=stream)

    assert stream.buffer.getvalue().decode("utf-8") == '{"marker":"•"}\n'


@pytest.mark.parametrize("invalid", (math.nan, math.inf, -math.inf))
def test_live_runner_rejects_non_finite_wait_budgets(invalid: float) -> None:
    with pytest.raises(ValueError, match="必须为有限值"):
        run_live_sites(
            api_base_url="https://alioth.example.test",
            access_token="secret-token",
            adult_self_attestation="adult-v1",
            requester=lambda *_args: {},
            poll_interval_seconds=invalid,
        )


@pytest.mark.parametrize(
    "field_name",
    (
        "menu_readiness_evidence_capture_ids",
        "detail_readiness_evidence_capture_ids",
        "menu_dismissed_layers",
        "detail_dismissed_layers",
    ),
)
def test_limit_skipped_ledger_rejects_any_capture_or_dismissal_evidence(
    field_name: str,
) -> None:
    actual = {
        "entry_id": "menu-over-limit",
        "menu_path": ["超限菜单"],
        "canonical_url": "https://example.test/over-limit",
        "status": "menu_limit_skipped",
        "source_capture_id": "capture-home",
        "error_code": "WEB_MENU_LIMIT_SKIPPED",
        field_name: ["unexpected-evidence"],
    }

    errors = _validate_live_ledger_entry(
        "https://example.test/",
        "https://example.test",
        {
            "entry_id": "menu-over-limit",
            "menu_path": ["超限菜单"],
            "href": "/over-limit",
            "decision": "allowlisted_observe_only",
        },
        actual,
        should_execute=False,
    )

    assert any("限额跳过不得产生页面采集证据" in item for item in errors)


def test_home_ledger_accepts_canonical_business_page_type() -> None:
    """首页是角色而非第五页型，runner 必须接受四类业务页型。"""

    source_capture_id = _fixture_uuid("canonical-home:source")
    probe_capture_id = _fixture_uuid("canonical-home:probe")
    ready_capture_id = _fixture_uuid("canonical-home:ready")
    errors = _validate_live_ledger_entry(
        "https://example.test/",
        "https://example.test",
        {
            "entry_id": "home",
            "menu_path": ["首页"],
            "href": "/",
            "decision": "home_baseline",
        },
        {
            "entry_id": "home",
            "menu_path": ["首页"],
            "canonical_url": "https://example.test/",
            "status": "home_baseline",
            "source_capture_id": source_capture_id,
            "final_capture_id": ready_capture_id,
            "page_type": "video_list",
            "menu_probe_capture_id": probe_capture_id,
            "menu_ready_capture_id": ready_capture_id,
            "menu_page_type": "video_list",
            "menu_readiness_evidence_capture_ids": [probe_capture_id, ready_capture_id],
            "menu_dismissed_layers": [],
            "menu_action_count": 0,
            "detail_action_count": 0,
        },
    )

    assert errors == []


@pytest.mark.parametrize(
    ("decision", "status", "base_fields", "forbidden_fields", "expected_error"),
    (
        (
            "home_baseline",
            "home_baseline",
            {
                "final_capture_id": _fixture_uuid("home:final"),
                "page_type": "home",
            },
            {
                "detail_capture_id": _fixture_uuid("home:detail"),
                "menu_probe_capture_id": _fixture_uuid("home:menu-probe"),
                "menu_ready_capture_id": _fixture_uuid("home:menu-ready"),
                "menu_page_type": "content_list",
                "menu_readiness_evidence_capture_ids": [
                    _fixture_uuid("home:menu-probe"),
                    _fixture_uuid("home:menu-ready"),
                ],
                "menu_dismissed_layers": ["unexpected-layer"],
                "detail_candidate_ids": ["web-candidate:" + "a" * 64],
                "selected_detail_candidate_id": "web-candidate:" + "a" * 64,
                "detail_selection_confidence": 0.96,
                "detail_selection_reason": "不应存在的详情选择",
                "detail_probe_capture_id": _fixture_uuid("home:detail-probe"),
                "detail_ready_capture_id": _fixture_uuid("home:detail-ready"),
                "detail_page_type": "content_detail",
                "detail_readiness_evidence_capture_ids": [
                    _fixture_uuid("home:detail-probe"),
                    _fixture_uuid("home:detail-ready"),
                ],
                "detail_dismissed_layers": ["unexpected-detail-layer"],
                "error_code": "WEB_UNEXPECTED_HOME_ERROR",
            },
            "首页基线不得产生菜单或详情证据",
        ),
        (
            "blocked_cross_origin",
            "cross_origin_blocked",
            {"error_code": "WEB_MENU_CROSS_ORIGIN_BLOCKED"},
            {
                "final_capture_id": _fixture_uuid("cross:final"),
                "page_type": "content_detail",
                "detail_capture_id": _fixture_uuid("cross:detail"),
                "menu_probe_capture_id": _fixture_uuid("cross:menu-probe"),
                "menu_ready_capture_id": _fixture_uuid("cross:menu-ready"),
                "menu_page_type": "content_list",
                "menu_readiness_evidence_capture_ids": [
                    _fixture_uuid("cross:menu-probe"),
                    _fixture_uuid("cross:menu-ready"),
                ],
                "menu_dismissed_layers": ["unexpected-layer"],
                "detail_candidate_ids": ["web-candidate:" + "b" * 64],
                "selected_detail_candidate_id": "web-candidate:" + "b" * 64,
                "detail_selection_confidence": 0.96,
                "detail_selection_reason": "不应存在的详情选择",
                "detail_probe_capture_id": _fixture_uuid("cross:detail-probe"),
                "detail_ready_capture_id": _fixture_uuid("cross:detail-ready"),
                "detail_page_type": "content_detail",
                "detail_readiness_evidence_capture_ids": [
                    _fixture_uuid("cross:detail-probe"),
                    _fixture_uuid("cross:detail-ready"),
                ],
                "detail_dismissed_layers": ["unexpected-detail-layer"],
            },
            "跨域阻断不得产生页面采集证据",
        ),
    ),
)
def test_home_and_cross_origin_ledger_use_strict_evidence_whitelists(
    decision: str,
    status: str,
    base_fields: dict[str, object],
    forbidden_fields: dict[str, object],
    expected_error: str,
) -> None:
    actual = {
        "entry_id": "strict-entry",
        "menu_path": ["严格分支"],
        "canonical_url": "https://example.test/strict",
        "status": status,
        "detail_action_count": 0,
        "source_capture_id": _fixture_uuid(f"{decision}:source"),
        **base_fields,
    }
    actual.update(forbidden_fields)

    errors = _validate_live_ledger_entry(
        "https://example.test/",
        "https://example.test",
        {
            "entry_id": "strict-entry",
            "menu_path": ["严格分支"],
            "href": "/strict",
            "decision": decision,
        },
        actual,
    )

    assert any(expected_error in item for item in errors)


@pytest.mark.parametrize(
    ("decision", "status", "should_execute", "extra_fields"),
    (
        (
            "home_baseline",
            "home_baseline",
            True,
            {
                "final_capture_id": _fixture_uuid("count:home:final"),
                "page_type": "home",
            },
        ),
        (
            "blocked_cross_origin",
            "cross_origin_blocked",
            True,
            {"error_code": "WEB_MENU_CROSS_ORIGIN_BLOCKED"},
        ),
        (
            "allowlisted_observe_only",
            "menu_limit_skipped",
            False,
            {"error_code": "WEB_MENU_LIMIT_SKIPPED"},
        ),
    ),
)
def test_nonexecuted_ledger_rejects_detail_action_count_one(
    decision: str,
    status: str,
    should_execute: bool,
    extra_fields: dict[str, object],
) -> None:
    errors = _validate_live_ledger_entry(
        "https://example.test/",
        "https://example.test",
        {
            "entry_id": "count-entry",
            "menu_path": ["动作次数"],
            "href": "/count",
            "decision": decision,
        },
        {
            "entry_id": "count-entry",
            "menu_path": ["动作次数"],
            "canonical_url": "https://example.test/count",
            "status": status,
            "source_capture_id": _fixture_uuid(f"count:{decision}:source"),
            "detail_action_count": 1,
            **extra_fields,
        },
        should_execute=should_execute,
    )

    assert any("详情动作次数必须为 0" in item for item in errors)


def test_six_site_fixture_aggregate_matches_catalog() -> None:
    report = reconcile_all_fixture_sites()

    assert len(SIX_SITE_TARGETS) == 6
    assert report.site_count == 6
    assert report.active_anchor_count == 166
    assert report.home_baseline_count == 6
    assert report.allowlisted_observe_only_count == 157
    assert report.blocked_cross_origin_count == 3
    assert report.comment_only_anchor_count == 4
    assert report.drift_count == 0


def test_six_site_acceptance_matrix_freezes_home_types_limits_and_reports() -> None:
    catalog = json.loads(DEFAULT_CATALOG_PATH.read_text(encoding="utf-8"))
    sites_by_id = {item["site_id"]: item for item in catalog["sites"]}

    assert tuple(item.entry_url for item in SIX_SITE_ACCEPTANCE_FIXTURES) == (SIX_SITE_TARGETS)
    assert len({item.site_id for item in SIX_SITE_ACCEPTANCE_FIXTURES}) == 6
    for fixture in SIX_SITE_ACCEPTANCE_FIXTURES:
        assert canonical_web_logical_page_url(fixture.entry_url) == (fixture.canonical_home_url)
        assert resolve_web_logical_page_role(canonical_url=fixture.canonical_home_url) == "home"
        assert fixture.menu_limit == WEB_MENU_COLLECTION_LIMIT == 10
        assert set(fixture.required_page_types) == set(
            sites_by_id[fixture.site_id]["applicability"]["required_page_types"]
        )
        assert set(fixture.required_page_types) <= {
            "home",
            "content_list",
            "content_detail",
            "video_list",
            "video_detail",
        }
        assert fixture.detail_target_required is True
        assert "advertisement" in fixture.expected_obstructions
        assert "age_gate" in fixture.expected_obstructions
        assert "preview" in fixture.accepted_report_stages
        assert "human_verification_blocked" in (fixture.allowed_manual_blocking_reasons)


def test_protocols_stay_distinct_while_tracking_and_anchor_captures_share_one_page() -> None:
    pages = build_web_logical_pages(
        (
            {
                "capture_id": "capture-entry",
                "url": "http://91porna.com",
                "capture_purpose": "web_entry_probe",
            },
            {
                "capture_id": "capture-ready",
                "url": "https://91porna.com/?utm_source=menu#top",
                "capture_purpose": "web_page_ready_baseline",
            },
            {
                "capture_id": "capture-evidence",
                "url": "https://91porna.com/",
                "capture_purpose": "web_primary_content_evidence",
            },
        )
    )

    assert len(pages) == 2
    assert pages[0].canonical_url == "http://91porna.com/"
    assert pages[0].capture_ids == ("capture-entry",)
    assert pages[1].capture_ids == ("capture-ready", "capture-evidence")
    assert pages[1].primary_capture_id == "capture-evidence"


def test_every_site_has_exact_fixture_and_catalog_identity_match() -> None:
    report = reconcile_all_fixture_sites()

    assert {item.target_url for item in report.sites} == set(SIX_SITE_TARGETS)
    assert all(item.missing_entry_ids == () for item in report.sites)
    assert all(item.extra_entry_ids == () for item in report.sites)
    assert all(item.changed_entry_ids == () for item in report.sites)


def test_live_submission_requires_explicit_adult_statement() -> None:
    with pytest.raises(ValueError, match="成年声明"):
        submit_live_sites(
            api_base_url="https://alioth.example.test",
            access_token="token",
            adult_self_attestation="",
            sender=lambda *_args: {},
        )


def test_live_submission_uses_full_inspection_api_in_fixed_order() -> None:
    calls = []
    targets = ("https://both.pnsmxatxp.cc/", "https://heiliao.com/")

    def sender(endpoint, payload, headers):
        calls.append((endpoint, payload, headers))
        index = len(calls)
        return {"task_id": f"task-{index}", "run_id": f"run-{index}", "status": "queued"}

    submissions = submit_live_sites(
        api_base_url="https://alioth.example.test/",
        access_token="secret-token",
        adult_self_attestation="adult-v1",
        sender=sender,
        targets=targets,
    )

    assert [item.target_url for item in submissions] == list(targets)
    assert all(
        endpoint == "https://alioth.example.test/api/v2/tasks"
        for endpoint, _payload, _headers in calls
    )
    assert [call[1]["target"]["url"] for call in calls] == list(targets)
    assert all(
        call[1]["target"]["adult_self_attestation"]
        == {"declared": True, "statement_version": "adult-v1"}
        for call in calls
    )


def test_cli_live_without_statement_fails_before_network() -> None:
    with pytest.raises(SystemExit):
        main(["--live"])


def test_remote_live_api_requires_https_and_rejects_credentials() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        _validate_api_base_url("http://alioth.example.test")
    with pytest.raises(ValueError, match="凭据"):
        _validate_api_base_url("https://user:pass@alioth.example.test")
    assert _validate_api_base_url("http://127.0.0.1:8000/") == ("http://127.0.0.1:8000")


def test_live_http_client_rejects_redirect_before_forwarding_token() -> None:
    with pytest.raises(RuntimeError, match="REDIRECT_BLOCKED"):
        _RejectRedirectHandler().redirect_request(
            None,
            None,
            302,
            "Found",
            {},
            "https://other.example.test/",
        )


def test_live_target_subset_rejects_empty_and_duplicate_targets() -> None:
    with pytest.raises(ValueError, match="至少需要一个站点"):
        _normalize_live_targets(())
    with pytest.raises(ValueError, match="不得包含重复站点"):
        _normalize_live_targets((SIX_SITE_TARGETS[0], SIX_SITE_TARGETS[0]))


def test_live_targets_accept_unique_https_origins_outside_frozen_catalog() -> None:
    assert _normalize_live_targets((
        "https://heiliao.com/", "https://hl365.com/", "https://both.pnsmxatxp.cc/",
    )) == ("https://heiliao.com/", "https://hl365.com/", "https://both.pnsmxatxp.cc/")


def test_live_targets_canonicalize_and_preserve_request_order() -> None:
    assert _normalize_live_targets(("https://BOTH.pnsmxatxp.cc:443", "https://heiliao.com/")) == (
        "https://both.pnsmxatxp.cc/", "https://heiliao.com/",
    )
    with pytest.raises(ValueError, match="重复"):
        _normalize_live_targets(("https://example.test", "https://EXAMPLE.test:443/"))


def test_live_targets_reject_browser_equivalent_dot_segment_duplicates() -> None:
    with pytest.raises(ValueError, match="重复"):
        _normalize_live_targets((
            "https://example.test/", "https://example.test/a/../", "https://example.test/%2e/",
        ))


@pytest.mark.parametrize("raw_path,encoded_path", (("/é", "/%C3%A9"), ("/`", "/%60")))
def test_live_targets_reject_browser_serialization_duplicates(raw_path, encoded_path) -> None:
    with pytest.raises(ValueError, match="重复"):
        _normalize_live_targets((f"https://example.test{raw_path}", f"https://example.test{encoded_path}"))
    assert _normalize_live_targets((f"https://example.test{raw_path}",)) == (f"https://example.test{encoded_path}",)


@pytest.mark.parametrize("paths", (
    ("/a/%2f/b", "/a///b"),
    ("/a/%252e/b", "/a/b"),
))
def test_live_target_serialization_preserves_encoded_path_noncollisions(paths) -> None:
    targets = tuple(f"https://example.test{path}" for path in paths)
    assert _normalize_live_targets(targets) == targets


@pytest.mark.parametrize("path,expected", (
    ("/a/../", "/"), ("/%2e/", "/"), ("/a/%2E%2e/b", "/b"),
    ("/a/.%2E/b", "/b"), ("/a/%2e./b", "/b"),
    ("/a/..", "/"), ("/../b", "/b"), ("/a//b", "/a//b"),
    ("/a/%252e/b", "/a/%252e/b"), ("/a/%2f/b", "/a/%2f/b"),
))
def test_live_targets_match_browser_dot_segment_normalization(path, expected) -> None:
    assert _normalize_live_targets((f"https://example.test{path}",)) == (f"https://example.test{expected}",)


@pytest.mark.parametrize("target", (
    "http://example.test/", "ftp://example.test/", "https://user:secret@example.test/",
    "https://example.test/#fragment", "https://example.test/?token=secret",
    "https://example.test:invalid/", "https://example.test/\\evil", "https://exa\nmple.test/",
))
def test_live_targets_reject_unsafe_urls(target: str) -> None:
    with pytest.raises(ValueError):
        _normalize_live_targets((target,))


def test_live_report_renders_null_menu_limit_as_unbounded(tmp_path) -> None:
    from scripts.web_v2_six_site_shadow import _safe_live_stdout_payload

    report = LiveSixSiteReport(
        site_count=1, menu_total=20, menu_limit=None,
        executed_same_origin_count=19, expected_executed_same_origin_count=19,
        menu_limit_skipped_count=0, expected_menu_limit_skipped_count=0,
        home_baseline_count=1, detail_count=19, blocked_cross_origin_count=0,
        frontier_remaining=0, validation_errors=(), sites=(),
        requested_targets=("https://both.pnsmxatxp.cc/",),
    )
    markdown = render_live_markdown(report)
    assert "菜单数量上限：不限" in markdown
    assert "站点：1/1" in markdown
    assert "/166" not in markdown and "/6" not in markdown
    assert "菜单数量上限：不限" in render_live_html(report)
    assert _safe_live_stdout_payload(
        report, report_path=tmp_path / "live.md", html_report_path=tmp_path / "live.html",
    )["menu_limit"] is None


def _unbounded_runtime_live_site(target: str = "https://both.pnsmxatxp.cc/") -> LiveSiteResult:
    historical = _complete_catalog_live_sites()[0]
    context = WebSiteMenuRegistry(DEFAULT_CATALOG_PATH).resolve_site_context(target).site
    source = historical.exploration_summary["menu_ledger"]
    home = next(dict(entry) for entry in source if entry["status"] == "home_baseline")
    home.update(entry_id=context.entries[0].entry_id, canonical_url=target)
    detail = next(entry for entry in source if entry["status"] == "detail_captured")
    ledger = [home]
    for index in range(12):
        entry = dict(detail, entry_id=f"runtime-{index}", menu_path=[f"内容{index}"],
                     canonical_url=f"{target}article/{index}")
        # 在报告篡改前冻结来源身份，使用生产公开导航哈希的独立固定结构。
        identity = sha256(json.dumps(
            (("url", entry["canonical_url"]), entry["menu_path"], entry["menu_path"], None),
            ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        entry.update(entry_id="discovery:" + identity, logical_target_identity=identity,
                     exploration_path=list(entry["menu_path"]), parent_logical_target_identity=None)
        for key, value in tuple(entry.items()):
            if isinstance(value, str) and "capture_id" in key:
                entry[key] = _fixture_uuid(f"runtime-{index}:{value}")
        entry["menu_readiness_evidence_capture_ids"] = [
            entry["menu_probe_capture_id"], entry["menu_ready_capture_id"],
        ]
        entry["menu_action_execution_id"] = _fixture_action_execution_id(f"runtime-{index}")
        ledger.append(entry)
    evidence = _fixture_web_evidence_from_report_ledger(
        task_id=historical.task_id, run_id=historical.run_id, site_id=context.site_id,
        target_url=target, ledger=ledger,
    )
    return replace(
        historical, target_url=target, evidence_bundle=evidence,
        exploration_summary={"site_id": context.site_id, "menu_total": 13, "menu_limit": None,
                             "executed_same_origin_count": 12, "menu_limit_skipped_count": 0,
                             "frontier_remaining": 0, "status_counts": {"home_baseline": 1, "detail_captured": 12},
                             "menu_ledger": ledger},
        inspection_total_count=len(evidence["formal_pages"]),
        logical_page_assessments=tuple({
            "logical_page_id": page["logical_page_id"], "canonical_url": page["logical_page_url"],
            "primary_capture_id": page["primary_capture_id"], "page_type": page["page_type"],
            "classification_status": "classified", "inspection_status": "completed",
        } for page in evidence["formal_pages"]),
        screenshot_selections=_build_live_screenshot_selections(evidence),
    )


@pytest.mark.parametrize("target", ("https://both.pnsmxatxp.cc/", "https://heiliao.com/"))
def test_live_aggregate_uses_runtime_ledger_beyond_ten_menus(target: str) -> None:
    site = _unbounded_runtime_live_site(target)
    report = _aggregate_live_results((site,), expected_targets=(target,))
    assert report.validation_errors == ()
    assert report.site_count == 1 and report.menu_total == 13
    assert report.menu_limit is None
    assert report.expected_executed_same_origin_count == report.executed_same_origin_count == 12
    assert report.expected_menu_limit_skipped_count == report.menu_limit_skipped_count == 0


def test_live_report_reads_real_flat_runtime_serialization_and_menu_ad_evidence() -> None:
    from dataclasses import fields

    from services.api.app.application.web_menu_exploration import (
        WebMenuLedgerEntry,
        WebMenuTerminalStatus,
        WebRuntimeDiscoveredLedgerEntry,
    )

    site = _unbounded_runtime_live_site()
    ledger = site.exploration_summary["menu_ledger"]
    runtime = []
    for slot, raw in enumerate(ledger[1:], 1):
        values = {item.name: raw[item.name] for item in fields(WebMenuLedgerEntry) if item.name in raw}
        values["status"] = WebMenuTerminalStatus.DETAIL_CAPTURED
        values["menu_depth"] = 1
        values["ad_landing_evidence"] = {
            "status": "no_candidate", "reason_code": "WEB_AD_CANDIDATE_NOT_FOUND",
            "source_url": raw["canonical_url"],
        }
        execution = WebMenuLedgerEntry(**values)
        runtime.append(WebRuntimeDiscoveredLedgerEntry(
            entry_id=execution.entry_id, menu_path=tuple(raw["menu_path"]),
            canonical_url=execution.canonical_url, menu_position=slot,
            source_capture_id=execution.source_capture_id, execution=execution,
            execution_slot=slot, status=WebMenuTerminalStatus.DETAIL_CAPTURED,
            error_code=None, logical_target_identity=raw["logical_target_identity"],
            menu_depth=1,
            exploration_path=tuple(raw["exploration_path"]),
        ).to_dict())
    site.exploration_summary["menu_ledger"] = ledger[:1]
    site.exploration_summary["runtime_discovered_ledger"] = runtime
    report = _aggregate_live_results((site,), expected_targets=(site.target_url,))
    assert report.validation_errors == ()
    assert report.menu_total == 13 and report.executed_same_origin_count == 12
    assert "WEB_AD_CANDIDATE_NOT_FOUND" in render_live_markdown(report).replace("\\_", "_")
    assert "WEB_AD_CANDIDATE_NOT_FOUND" in render_live_html(report)


def test_preview_hydration_preserves_menu_frontier_slots_and_isolated_ad_audit():
    bundle = _normalize_live_site_evidence_bundle({
        "menu_limit": None, "summary": {"menu_count": 1}, "entries": [
            {"menu_entry_id": "home", "status": "home_baseline", "menu_path": ["首页"],
             "ad_landing_evidence": {"status": "no_candidate", "reason_code": "WEB_AD_CANDIDATE_NOT_FOUND"}},
            {"menu_entry_id": "discovery:abc", "source_kind": "runtime_discovered",
             "status": "navigation_failed", "menu_path": ["资讯"], "execution_slot": 12,
             "canonical_url": "https://example.test/news", "menu_action_count": 1,
             "error_code": "WEB_DISCOVERY_NOT_AUTHORIZED",
             "ad_landing_evidence": {"status": "no_candidate", "reason_code": "WEB_AD_CANDIDATE_NOT_FOUND"}},
        ],
    })
    summary = _hydrate_preview_exploration_summary(
        target_url="https://example.test/", report_exploration={}, evidence_bundle=bundle,
    )
    assert summary["menu_total"] == 2 and summary["menu_limit"] is None
    assert summary["menu_ledger"][0]["canonical_url"] == "https://example.test/"
    assert summary["menu_ledger"][0]["ad_landing_evidence"]["status"] == "no_candidate"
    runtime = summary["runtime_discovered_ledger"][0]
    assert runtime["execution_slot"] == 12
    assert runtime["canonical_url"] == "https://example.test/news"
    assert runtime["error_code"] == "WEB_DISCOVERY_NOT_AUTHORIZED"
    assert runtime["ad_landing_evidence"]["reason_code"] == "WEB_AD_CANDIDATE_NOT_FOUND"


@pytest.mark.parametrize("mutation,expected", (
    ("limit", "menu_limit"), ("skip_count", "menu_limit_skipped_count"),
    ("skip_status", "menu_limit_skipped"), ("missing_capture", "Capture"),
    ("identity", "site_id"), ("missing_site", "目标"),
    ("duplicate_entry", "重复"), ("missing_limit", "menu_limit"),
    ("foreign_home", "canonical_url"), ("invalid_menu_path", "menu_path"),
    ("forged_url", "导航身份"), ("forged_path", "导航身份"),
    ("forged_url_and_path", "导航身份"), ("forged_landing", "落地 URL"),
    ("missing_navigation_identity", "导航身份"),
    ("unproven_runtime_identity", "导航身份"),
))
def test_unbounded_live_aggregate_rejects_contract_corruption(mutation: str, expected: str) -> None:
    site = _unbounded_runtime_live_site()
    if mutation == "limit":
        site.exploration_summary["menu_limit"] = 10
    elif mutation == "missing_limit":
        site.exploration_summary.pop("menu_limit")
    elif mutation == "skip_count":
        site.exploration_summary["menu_limit_skipped_count"] = 1
    elif mutation == "skip_status":
        site.evidence_bundle["entries"][1]["status"] = "menu_limit_skipped"
    elif mutation == "missing_capture":
        site.evidence_bundle["captures"].pop()
    elif mutation == "identity":
        site.evidence_bundle["site_id"] = "heiliao_com"
    elif mutation == "duplicate_entry":
        site.exploration_summary["menu_ledger"].append(site.exploration_summary["menu_ledger"][1])
    elif mutation == "foreign_home":
        site.exploration_summary["menu_ledger"][0]["canonical_url"] = "https://other.test/"
    elif mutation == "invalid_menu_path":
        site.exploration_summary["menu_ledger"][1]["menu_path"] = 123
    elif mutation in {"forged_url", "forged_path", "forged_url_and_path"}:
        entry = site.exploration_summary["menu_ledger"][1]
        if mutation != "forged_path":
            entry["canonical_url"] = site.target_url + "never-captured"
        if mutation != "forged_url":
            entry["menu_path"] = ["伪造栏目"]
    elif mutation == "forged_landing":
        capture_id = site.evidence_bundle["entries"][1]["menu_ready_capture_id"]
        next(item for item in site.evidence_bundle["captures"] if item["capture_id"] == capture_id)["url"] = site.target_url + "other-page"
    elif mutation == "missing_navigation_identity":
        site.evidence_bundle["entries"][1].pop("logical_target_identity")
    elif mutation == "unproven_runtime_identity":
        site.exploration_summary["menu_ledger"][1]["entry_id"] = "unproven-menu"
        site.evidence_bundle["entries"][1]["menu_entry_id"] = "unproven-menu"
        site.evidence_bundle["entries"][1].pop("logical_target_identity")
    targets = (site.target_url, "https://hl365.com/") if mutation == "missing_site" else (site.target_url,)
    report = _aggregate_live_results((site,), expected_targets=targets)
    assert any(expected in error for error in report.validation_errors)


def test_live_cli_target_url_submits_generic_targets_and_rejects_site_id_mix(monkeypatch, tmp_path) -> None:
    site = _unbounded_runtime_live_site()
    report = _aggregate_live_results((site,), expected_targets=(site.target_url,))
    calls = []
    monkeypatch.setenv("ALIOTH_WEB_V2_ACCESS_TOKEN", "fixture-token")
    monkeypatch.setattr("scripts.web_v2_six_site_shadow.run_live_sites",
                        lambda **kwargs: calls.append(kwargs) or report)
    args = ["--live", "--adult-self-attestation", "adult-v1", "--target-url", site.target_url,
            "--target-url", "https://hl365.com/",
            "--live-report-path", str(tmp_path / "live.md"),
            "--live-html-report-path", str(tmp_path / "live.html")]
    assert main(args) == 1
    assert calls[0]["targets"] == (site.target_url, "https://hl365.com/")
    with pytest.raises(SystemExit):
        main([*args, "--site-id", "heiliao_com"])
    assert len(calls) == 1


@pytest.mark.parametrize(
    "targets",
    (
        tuple(target.replace("http://", "https://", 1) for target in SIX_SITE_TARGETS),
        (SIX_SITE_TARGETS[0], SIX_SITE_TARGETS[2]),
        ("https://heiliao.com/", "https://hl365.com/", "https://both.pnsmxatxp.cc/"),
    ),
)
def test_live_runner_waits_for_report_before_submitting_next_site(
    targets: tuple[str, ...],
) -> None:
    calls: list[tuple[str, str]] = []
    post_count = 0

    def requester(method, endpoint, payload, headers):
        nonlocal post_count
        assert headers["Authorization"] == "Bearer secret-token"
        calls.append((method, endpoint))
        if method == "POST":
            post_count += 1
            return {
                "task_id": f"task-{post_count}",
                "run_id": f"run-{post_count}",
                "status": "queued",
            }
        if endpoint.endswith("/web-evidence"):
            return {
                "task_id": f"task-{post_count}",
                "agent_run_id": f"run-{post_count}",
                "site_id": f"site-{post_count}",
                "status": "needs_review",
                "reason_codes": ["evidence_incomplete"],
                "summary": {"home_count": 0, "menu_count": 0, "detail_count": 0},
                "entries": [],
                "captures": [],
            }
        if "/workstation/tasks/" in endpoint:
            return {
                "task_id": f"task-{post_count}",
                "run_id": f"run-{post_count}",
                "status": "done",
                "report_status": "not_applicable",
                "failure_reason": None,
            }
        assert "/workstation/reports/" in endpoint
        return {
            "report_stage": "final",
            "is_final": True,
            "export_allowed": True,
            "page_exploration": {
                "site_id": f"site-{post_count}",
                "menu_total": 0,
                "executed_same_origin_count": 0,
                "frontier_remaining": 0,
                "status_counts": {},
                "menu_ledger": [],
            },
            "web_logical_pages": [
                {
                    "logical_page_id": "web-logical:" + "b" * 64,
                    "canonical_url": "https://heiliao.com/",
                    "primary_capture_id": _fixture_uuid("final:primary"),
                    "page_type": "content_list",
                    "classification_status": "classified",
                    "inspection_status": "completed",
                }
            ],
        }

    report = run_live_sites(
        api_base_url="https://alioth.example.test",
        access_token="secret-token",
        adult_self_attestation="adult-v1",
        requester=requester,
        sleep=lambda _seconds: None,
        monotonic=lambda: 0.0,
        site_timeout_seconds=60,
        targets=targets,
    )

    assert tuple(item.target_url for item in report.sites) == targets
    assert all(
        endpoint == "https://alioth.example.test/api/v2/tasks"
        for method, endpoint in calls
        if method == "POST"
    )
    for index in range(len(targets)):
        offset = index * 4
        assert calls[offset][0] == "POST"
        assert calls[offset + 1][0] == "GET"
        assert "/workstation/tasks/" in calls[offset + 1][1]
        assert calls[offset + 2][0] == "GET"
        assert "/workstation/reports/" in calls[offset + 2][1]
        assert calls[offset + 3][0] == "GET"
        assert calls[offset + 3][1].endswith("/web-evidence")
    assert all(site.server_completion_gate_passed is True for site in report.sites)
    assert all(site.server_report_bound is True for site in report.sites)
    assert all(site.page_type_review_count == 0 for site in report.sites)
    assert all(site.inspection_total_count == 1 for site in report.sites)
    assert all(site.inspection_pending_count == 0 for site in report.sites)
    assert report.validation_errors


def test_live_runner_collects_terminal_preview_instead_of_waiting_for_final() -> None:
    """人工复核任务只有 preview，实况 runner 仍须留存报告并继续后续站点。"""

    target = SIX_SITE_TARGETS[0]

    def requester(method, endpoint, payload, headers):
        assert headers["Authorization"] == "Bearer secret-token"
        if method == "POST":
            return {"task_id": "task-preview", "run_id": "run-preview", "status": "queued"}
        if endpoint.endswith("/web-evidence"):
            return {
                "task_id": "task-preview",
                "agent_run_id": "run-preview",
                "site_id": "heiliao_com",
                "status": "needs_review",
                "reason_codes": ["evidence_incomplete"],
                "summary": {
                    "home_count": 1,
                    "menu_count": 0,
                    "detail_count": 0,
                    "duplicate_formal_url_count": 2,
                },
                "entries": [],
                "captures": [],
            }
        if "/workstation/tasks/" in endpoint:
            return {
                "task_id": "task-preview",
                "run_id": "run-preview",
                "status": "review",
                "report_status": "failed",
                "failure_reason": None,
            }
        assert "/workstation/reports/" in endpoint
        return {
            "report_stage": "preview",
            "report_availability": "preview",
            "is_final": False,
            "page_exploration": {
                "site_id": "heiliao_com",
                "menu_total": 0,
                "executed_same_origin_count": 0,
                "frontier_remaining": 0,
                "status_counts": {},
                "menu_ledger": [],
            },
            "summary": {"overall_status": "incomplete"},
            "web_preview": {
                "inspection_progress": {"total": 3, "completed": 2, "pending": 1},
                "page_type_review": {"total": 1, "completed": 0, "pending": 1},
                "completion_gate": {
                    "gate_passed": False,
                    "report_bound": False,
                    "pending_confirmations": 1,
                },
            },
            "web_logical_pages": [
                {
                    "logical_page_id": "web-logical:" + "a" * 64,
                    "canonical_url": "https://heiliao.com/?token=redacted",
                    "primary_capture_id": _fixture_uuid("preview:primary"),
                    "page_type": None,
                    "classification_status": "needs_review",
                    "inspection_status": "evaluating",
                    "unsafe_reason": "不应进入实况报告",
                }
            ],
        }

    ticks = iter((0.0, 2.0))
    report = run_live_sites(
        api_base_url="https://alioth.example.test",
        access_token="secret-token",
        adult_self_attestation="adult-v1",
        requester=requester,
        sleep=lambda _seconds: None,
        monotonic=lambda: next(ticks),
        site_timeout_seconds=1,
        targets=(target,),
    )

    assert report.sites[0].task_status == "review"
    assert report.sites[0].report_stage == "preview"
    assert report.sites[0].is_final is False
    assert report.sites[0].evidence_bundle["status"] == "needs_review"
    assert report.sites[0].page_type_review_count == 1
    assert report.sites[0].pending_confirmation_count == 1
    assert report.sites[0].duplicate_url_count == 2
    assert report.sites[0].inspection_total_count == 3
    assert report.sites[0].inspection_pending_count == 1
    assert report.sites[0].server_completion_gate_passed is False
    assert report.sites[0].server_report_bound is False
    assert report.sites[0].logical_page_assessments == (
        {
            "logical_page_id": "web-logical:" + "a" * 64,
            "canonical_url": "https://heiliao.com/",
            "primary_capture_id": _fixture_uuid("preview:primary"),
            "page_type": None,
            "classification_status": "needs_review",
            "inspection_status": "evaluating",
        },
    )


def test_live_runner_records_terminal_task_when_evidence_bundle_is_missing() -> None:
    """终态任务缺少证据包时也必须生成失败报告，不能让整批六站回归异常退出。"""

    target = SIX_SITE_TARGETS[0]

    def requester(method, endpoint, payload, headers):
        assert headers["Authorization"] == "Bearer secret-token"
        if method == "POST":
            return {"task_id": "task-failed", "run_id": "run-failed", "status": "queued"}
        if endpoint.endswith("/web-evidence"):
            raise urllib.error.HTTPError(endpoint, 404, "Not Found", {}, None)
        if "/workstation/tasks/" in endpoint:
            return {
                "task_id": "task-failed",
                "run_id": "run-failed",
                "status": "failed",
                "report_status": "failed",
                "failure_reason": "runtime_version_drift",
            }
        assert "/workstation/reports/" in endpoint
        return {
            "report_stage": "final",
            "report_availability": "final",
            "is_final": True,
            "page_exploration": {
                "site_id": "heiliao_com",
                "menu_total": 0,
                "executed_same_origin_count": 0,
                "frontier_remaining": 0,
                "status_counts": {},
                "menu_ledger": [],
            },
            "summary": {
                "overall_status": "failed",
                "error_code": "runtime_version_drift",
            },
        }

    report = run_live_sites(
        api_base_url="https://alioth.example.test",
        access_token="secret-token",
        adult_self_attestation="adult-v1",
        requester=requester,
        sleep=lambda _seconds: None,
        monotonic=lambda: 0.0,
        site_timeout_seconds=60,
        targets=(target,),
    )

    assert report.sites[0].task_status == "failed"
    assert report.sites[0].failure_reason == "runtime_version_drift"
    assert report.sites[0].evidence_bundle["status"] is None
    assert any("evidence" in error for error in report.validation_errors)


def test_preview_summary_uses_evidence_ledger_instead_of_generic_timeline() -> None:
    """预览报告的通用时间线不得把已执行菜单错误统计为零。"""

    catalog = json.loads(DEFAULT_CATALOG_PATH.read_text(encoding="utf-8"))
    catalog_site = next(site for site in catalog["sites"] if site["site_id"] == "91porna_com")
    allowlisted_index = 0
    entries = []
    for catalog_entry in catalog_site["entries"]:
        decision = catalog_entry["decision"]
        if decision == "home_baseline":
            status = "home_baseline"
        elif decision == "blocked_cross_origin":
            status = "cross_origin_blocked"
        elif allowlisted_index < WEB_MENU_COLLECTION_LIMIT:
            status = "detail_captured"
            allowlisted_index += 1
        else:
            status = "menu_limit_skipped"
            allowlisted_index += 1
        entries.append(
            {
                "menu_entry_id": catalog_entry["entry_id"],
                "status": status,
            }
        )

    summary = _hydrate_preview_exploration_summary(
        target_url="http://91porna.com/",
        report_exploration={"tracking_available": True, "page_total": 11},
        evidence_bundle={
            "site_id": "91porna_com",
            "summary": {
                "home_count": 1,
                "menu_count": WEB_MENU_COLLECTION_LIMIT,
                "detail_count": WEB_MENU_COLLECTION_LIMIT,
            },
            "entries": entries,
        },
    )

    assert summary["site_id"] == "91porna_com"
    assert summary["menu_total"] == len(catalog_site["entries"])
    assert summary["executed_same_origin_count"] == WEB_MENU_COLLECTION_LIMIT
    assert summary["menu_limit_skipped_count"] == sum(
        entry["status"] == "menu_limit_skipped" for entry in entries
    )
    assert summary["status_counts"] == dict(Counter(entry["status"] for entry in entries))
    assert summary["frontier_remaining"] == 0


def test_preview_validation_accepts_review_evidence_without_legacy_report_ledger() -> None:
    """预览应验证证据身份，不应要求旧版正式报告的完整 menu_ledger。"""

    catalog = json.loads(DEFAULT_CATALOG_PATH.read_text(encoding="utf-8"))
    catalog_site = next(site for site in catalog["sites"] if site["site_id"] == "91porna_com")
    allowlisted_index = 0
    entries = []
    for catalog_entry in catalog_site["entries"]:
        decision = catalog_entry["decision"]
        if decision == "home_baseline":
            status = "home_baseline"
        elif decision == "blocked_cross_origin":
            status = "cross_origin_blocked"
        elif allowlisted_index < WEB_MENU_COLLECTION_LIMIT:
            status = "detail_captured"
            allowlisted_index += 1
        else:
            status = "menu_limit_skipped"
            allowlisted_index += 1
        entries.append(
            {
                "menu_entry_id": catalog_entry["entry_id"],
                "status": status,
                "menu_action_count": 0 if status != "detail_captured" else 1,
                "detail_action_count": 0 if status != "detail_captured" else 1,
                "menu_page_type": (
                    "video_list" if status in {"home_baseline", "detail_captured"} else None
                ),
                "detail_page_type": "video_detail" if status == "detail_captured" else None,
            }
        )
    task_id = _fixture_uuid("preview:task")
    run_id = _fixture_uuid("preview:run")
    evidence = {
        "task_id": task_id,
        "agent_run_id": run_id,
        "site_id": "91porna_com",
        "status": "needs_review",
        "reason_codes": ["evidence_incomplete"],
        "summary": {
            "home_count": 1,
            "menu_count": WEB_MENU_COLLECTION_LIMIT,
            "detail_count": WEB_MENU_COLLECTION_LIMIT,
        },
        "entries": entries,
        "captures": [],
    }
    site = LiveSiteResult(
        target_url="http://91porna.com/",
        task_id=task_id,
        run_id=run_id,
        task_status="review",
        report_status="failed",
        failure_reason=None,
        report_stage="preview",
        is_final=False,
        exploration_summary=_hydrate_preview_exploration_summary(
            target_url="http://91porna.com/",
            report_exploration={"tracking_available": True},
            evidence_bundle=evidence,
        ),
        report_overall_status="incomplete",
        report_error_code=None,
        submission_status="queued",
        evidence_bundle=evidence,
    )

    assert _validate_live_preview_site(site, catalog_site) == []


def _complete_catalog_live_sites():
    catalog = json.loads(DEFAULT_CATALOG_PATH.read_text(encoding="utf-8"))
    registry = WebSiteMenuRegistry(DEFAULT_CATALOG_PATH)
    registry_sites = {site.site_id: site for site in registry.sites}
    sites: list[LiveSiteResult] = []
    targets_by_site_id = {
        site_id: target_url
        for target_url, site_id in zip(
            SIX_SITE_TARGETS,
            (
                "heiliao_com",
                "hl365_com",
                "www_91jav1_com",
                "91porna_com",
                "huangguoai_com",
                "avhub360_com",
            ),
            strict=True,
        )
    }
    for index, catalog_site in enumerate(catalog["sites"], start=1):
        collection_plan = plan_web_menu_collection(
            registry_sites[catalog_site["site_id"]],
            menu_limit=WEB_MENU_COLLECTION_LIMIT,
        )
        selected_entry_ids = {entry.entry_id for entry in collection_plan.selected_entries}
        skipped_entry_ids = {entry.entry_id for entry in collection_plan.skipped_entries}
        ledger = []
        for entry in catalog_site["entries"]:
            capture_prefix = f"{index}:{entry['entry_id']}"
            source_capture_id = _fixture_uuid(f"{capture_prefix}:source")
            probe_capture_id = _fixture_uuid(f"{capture_prefix}:probe")
            ready_capture_id = _fixture_uuid(f"{capture_prefix}:ready")
            decision = entry["decision"]
            is_selected = entry["entry_id"] in selected_entry_ids
            is_skipped = entry["entry_id"] in skipped_entry_ids
            typed_entry = next(
                item
                for item in registry_sites[catalog_site["site_id"]].entries
                if item.entry_id == entry["entry_id"]
            )
            skipped_error_code = (
                "WEB_MENU_LOGICAL_ALIAS"
                if entry["entry_id"] in collection_plan.alias_of
                else "WEB_MENU_CATALOG_DEPRECATED"
                if typed_entry.status.value == "deprecated"
                else "WEB_MENU_COLLECTION_INELIGIBLE"
                if not typed_entry.collection_eligibility.eligible
                else "WEB_MENU_LIMIT_SKIPPED"
            )
            status = (
                "home_baseline"
                if decision == "home_baseline"
                else "cross_origin_blocked"
                if decision == "blocked_cross_origin"
                else "detail_captured"
                if is_selected
                else "menu_limit_skipped"
            )
            ledger.append(
                {
                    "entry_id": entry["entry_id"],
                    "menu_path": entry["menu_path"],
                    "canonical_url": urllib.parse.urljoin(
                        f"{catalog_site['origin'].rstrip('/')}/",
                        entry["href"],
                    ),
                    "status": status,
                    "menu_action_count": 1 if is_selected else 0,
                    "menu_intent_id": (
                        _fixture_uuid(f"{capture_prefix}:menu-intent") if is_selected else None
                    ),
                    "menu_action_execution_id": (
                        _fixture_action_execution_id(f"{capture_prefix}:menu-action")
                        if is_selected
                        else None
                    ),
                    "detail_action_count": 0,
                    "detail_intent_id": None,
                    "detail_action_execution_id": None,
                    "source_capture_id": (
                        source_capture_id if decision == "home_baseline" or is_selected else None
                    ),
                    "final_capture_id": (
                        ready_capture_id
                        if is_selected
                        else ready_capture_id
                        if decision == "home_baseline"
                        else None
                    ),
                    "page_type": (
                        "home"
                        if decision == "home_baseline"
                        else "content_detail"
                        if is_selected
                        else None
                    ),
                    "menu_probe_capture_id": (
                        probe_capture_id if is_selected or decision == "home_baseline" else None
                    ),
                    "menu_ready_capture_id": (
                        ready_capture_id if is_selected or decision == "home_baseline" else None
                    ),
                    "menu_readiness_evidence_capture_ids": (
                        [
                            probe_capture_id,
                            ready_capture_id,
                        ]
                        if is_selected or decision == "home_baseline"
                        else []
                    ),
                    "menu_page_type": (
                        "content_detail"
                        if is_selected
                        else "home"
                        if decision == "home_baseline"
                        else None
                    ),
                    "detail_ready_capture_id": (ready_capture_id if is_selected else None),
                    "detail_capture_id": (ready_capture_id if is_selected else None),
                    "detail_page_type": ("content_detail" if is_selected else None),
                    "error_code": (
                        "WEB_MENU_CROSS_ORIGIN_BLOCKED"
                        if decision == "blocked_cross_origin"
                        else skipped_error_code
                        if is_skipped
                        else None
                    ),
                    "alias_of": collection_plan.alias_of.get(entry["entry_id"]),
                }
            )
        counts = Counter(item["status"] for item in ledger)
        target_url = targets_by_site_id[catalog_site["site_id"]]
        task_id = _fixture_uuid(f"task:{index}")
        run_id = _fixture_uuid(f"run:{index}")
        evidence_bundle = _fixture_web_evidence_from_report_ledger(
            task_id=task_id,
            run_id=run_id,
            site_id=catalog_site["site_id"],
            target_url=target_url,
            ledger=ledger,
        )
        sites.append(
            LiveSiteResult(
                target_url=target_url,
                task_id=task_id,
                run_id=run_id,
                task_status="done",
                report_status="not_applicable",
                failure_reason=None,
                report_stage="final",
                is_final=True,
                exploration_summary={
                    "site_id": catalog_site["site_id"],
                    "menu_total": len(ledger),
                    "menu_limit": WEB_MENU_COLLECTION_LIMIT,
                    "executed_same_origin_count": min(
                        WEB_MENU_COLLECTION_LIMIT, len(collection_plan.selected_entries)
                    ),
                    "menu_limit_skipped_count": len(collection_plan.skipped_entries),
                    "frontier_remaining": 0,
                    "status_counts": dict(counts),
                    "menu_ledger": ledger,
                },
                report_overall_status="incomplete",
                report_error_code=None,
                evidence_bundle=evidence_bundle,
                page_type_review_count=0,
                pending_confirmation_count=0,
                duplicate_url_count=0,
                inspection_total_count=len(evidence_bundle["formal_pages"]),
                inspection_pending_count=0,
                logical_page_assessments=tuple(
                    {
                        "logical_page_id": page["logical_page_id"],
                        "canonical_url": page["logical_page_url"],
                        "primary_capture_id": page["primary_capture_id"],
                        "page_type": page["page_type"],
                        "classification_status": "classified",
                        "inspection_status": "completed",
                    }
                    for page in evidence_bundle["formal_pages"]
                ),
                screenshot_selections=_build_live_screenshot_selections(evidence_bundle),
                server_completion_gate_passed=True,
                server_report_bound=True,
                task_list_report_verified=True,
                task_detail_report_verified=True,
            )
        )

    return tuple(sites)


def test_live_aggregate_accepts_only_complete_catalog_ledger() -> None:
    sites = _complete_catalog_live_sites()
    report = _aggregate_historical_fixture_results(tuple(sites))

    assert report.validation_errors == (), json.dumps(
        report.validation_errors,
        ensure_ascii=True,
        indent=2,
    )
    assert report.live_validation_passed is True
    assert report.passed is False
    assert report.executed_same_origin_count == EXPECTED_LIVE_EXECUTED_MENU_COUNT == 51
    assert report.expected_executed_same_origin_count == (EXPECTED_LIVE_EXECUTED_MENU_COUNT)
    assert report.menu_limit_skipped_count == (EXPECTED_LIVE_MENU_LIMIT_SKIPPED_COUNT) == 106
    assert report.expected_menu_limit_skipped_count == (EXPECTED_LIVE_MENU_LIMIT_SKIPPED_COUNT)

    selected_targets = (SIX_SITE_TARGETS[0], SIX_SITE_TARGETS[2])
    selected_report = _aggregate_historical_fixture_results(
        tuple(site for site in sites if site.target_url in selected_targets),
        expected_targets=selected_targets,
    )

    assert selected_report.live_validation_passed is True
    assert selected_report.passed is False
    assert selected_report.site_count == 2
    assert selected_report.executed_same_origin_count == (
        selected_report.expected_executed_same_origin_count
    )
    assert selected_report.detail_count == selected_report.expected_executed_same_origin_count

    porna_ledger = next(
        item.exploration_summary["menu_ledger"]
        for item in sites
        if item.target_url == "http://91porna.com/"
    )
    duplicate_entry = next(
        item for item in porna_ledger if item["entry_id"] == "91porna.sets.latest"
    )
    assert duplicate_entry["status"] == "menu_limit_skipped"
    assert duplicate_entry["error_code"] == "WEB_MENU_LOGICAL_ALIAS"
    assert duplicate_entry["alias_of"] == "91porna.sets"
    assert duplicate_entry["menu_action_count"] == 0
    assert duplicate_entry["source_capture_id"] is None
    assert duplicate_entry["final_capture_id"] is None


@pytest.mark.parametrize("case", ["complete", "missing", "unverified", "review", "pending", "manual", "binding", "contract", "subset"])
def test_live_overall_requires_all_six_dimensions(case, monkeypatch, tmp_path, capsys):
    """内存合成逐页人审只验证聚合，不是真实人员验收或外站请求。"""
    sites = []
    for site in _complete_catalog_live_sites():
        coverage = {
            "task_id": site.task_id, "agent_run_id": site.run_id,
            "coverage_snapshot_id": "fixture-coverage", "snapshot_hash": "a" * 64,
            "finalization_epoch": 0, "required_page_types": ["content_list", "content_detail"],
            "observed_page_types": ["content_list", "content_detail"],
            "missing_page_types": [], "unrecognized_page_types": [], "coverage_status": "complete",
        }
        changes = {"coverage_snapshot": coverage, "manual_page_reviews": tuple({
            "task_id": site.task_id, "run_id": site.run_id,
            "logical_page_id": page["logical_page_id"], "reviewer": "fixture-human",
            "reviewed_at": "2026-09-11T00:00:00Z", "reviewed": True,
        } for page in site.logical_page_assessments)}
        if case == "missing":
            coverage.update(observed_page_types=["content_list"], missing_page_types=["content_detail"], coverage_status="completed_with_gaps")
        elif case == "unverified":
            changes["coverage_snapshot"] = {}
        elif case == "review":
            changes["page_type_review_count"] = 1
        elif case == "pending":
            changes["inspection_pending_count"] = 1
        elif case == "manual":
            changes["manual_page_reviews"] = ()
        elif case == "binding":
            changes["server_report_bound"] = False
        elif case == "contract":
            changes["evidence_bundle"] = {}
        sites.append(replace(site, **changes))
    targets = SIX_SITE_TARGETS[:1] if case == "subset" else SIX_SITE_TARGETS
    report = _aggregate_historical_fixture_results(tuple(s for s in sites if s.target_url in targets), expected_targets=targets)
    expected = case in {"complete", "subset"}
    assert report.passed is expected
    assert bool(report.acceptance_failures) is not expected
    assert report.acceptance_scope == ("selected_sites" if case == "subset" else "all_six_sites")
    monkeypatch.setenv("ALIOTH_WEB_V2_ACCESS_TOKEN", "fixture-token")
    monkeypatch.setattr("scripts.web_v2_six_site_shadow.run_live_sites", lambda **kwargs: report)
    args = ["--live", "--adult-self-attestation", "adult-v1",
            "--live-report-path", str(tmp_path / "live.md"),
            "--live-html-report-path", str(tmp_path / "live.html")]
    args.extend(["--site-id", "heiliao_com"])
    assert main(args) == (0 if expected else 1)
    payload = json.loads(capsys.readouterr().out)
    assert payload["passed"] is expected
    assert payload["acceptance_scope"] == report.acceptance_scope
    assert payload["acceptance_failure_count"] == len(report.acceptance_failures)
    assert "验收范围" in (tmp_path / "live.md").read_text(encoding="utf-8")
    assert "验收范围" in (tmp_path / "live.html").read_text(encoding="utf-8")


def test_offline_catalog_precheck_succeeds_without_live_facts(capsys):
    assert main(["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary_mismatch_fields"] == []
    assert payload["missing_site_ids"] == payload["extra_site_ids"] == []


def test_live_aggregate_rejects_duplicate_target_replacing_missing_site() -> None:
    duplicate = LiveSiteResult(
        target_url=SIX_SITE_TARGETS[0],
        task_id=_fixture_uuid("duplicate:task"),
        run_id=_fixture_uuid("duplicate:run"),
        task_status="done",
        report_status="not_applicable",
        failure_reason=None,
        report_stage="final",
        is_final=True,
        exploration_summary={
            "site_id": "heiliao_com",
            "menu_total": 0,
            "menu_limit": WEB_MENU_COLLECTION_LIMIT,
            "executed_same_origin_count": 0,
            "menu_limit_skipped_count": 0,
            "frontier_remaining": 0,
            "status_counts": {},
            "menu_ledger": [],
        },
    )

    report = _aggregate_historical_fixture_results((duplicate,) * 6)

    assert any("六站目标或执行顺序" in item for item in report.validation_errors)


def test_live_aggregate_rejects_fallback_error_report_with_complete_ledger() -> None:
    catalog = json.loads(DEFAULT_CATALOG_PATH.read_text(encoding="utf-8"))
    catalog_site = catalog["sites"][0]
    site = LiveSiteResult(
        target_url=SIX_SITE_TARGETS[0],
        task_id="task-fallback-report",
        run_id="run-fallback-report",
        task_status="done",
        report_status="not_applicable",
        failure_reason=None,
        report_stage="final",
        is_final=True,
        exploration_summary={
            "site_id": catalog_site["site_id"],
            "menu_total": 0,
            "executed_same_origin_count": 0,
            "frontier_remaining": 0,
            "status_counts": {},
            "menu_ledger": [],
        },
        report_overall_status="error",
        report_error_code="WEB_FINAL_REPORT_GENERATION_FAILED",
    )

    report = _aggregate_historical_fixture_results((site,))

    assert any("最终报告错误" in item for item in report.validation_errors)


@pytest.mark.parametrize(
    "evidence_ids",
    (
        [],
        ["capture-other", "capture-menu-ready"],
        ["capture-menu-probe", "capture-other"],
    ),
)
def test_live_ledger_rejects_broken_menu_readiness_lineage(evidence_ids) -> None:
    errors = _validate_live_ledger_entry(
        "https://example.test/",
        "https://example.test",
        {
            "entry_id": "menu-direct-detail",
            "menu_path": ["详情"],
            "href": "/detail/1",
            "decision": "allowlisted_observe_only",
        },
        {
            "entry_id": "menu-direct-detail",
            "menu_path": ["详情"],
            "canonical_url": "https://example.test/detail/1",
            "status": "detail_captured",
            "source_capture_id": "capture-source",
            "final_capture_id": "capture-menu-ready",
            "page_type": "content_detail",
            "menu_probe_capture_id": "capture-menu-probe",
            "menu_ready_capture_id": "capture-menu-ready",
            "menu_page_type": "content_detail",
            "menu_readiness_evidence_capture_ids": evidence_ids,
            "detail_capture_id": "capture-menu-ready",
            "detail_ready_capture_id": "capture-menu-ready",
            "detail_page_type": "content_detail",
        },
    )

    assert any("菜单 readiness 血缘" in item for item in errors)


@pytest.mark.parametrize("action_count", (None, True, -1, 2))
def test_live_ledger_rejects_missing_or_invalid_detail_action_count(
    action_count,
) -> None:
    actual = {
        "entry_id": "menu-direct-detail",
        "menu_path": ["详情"],
        "canonical_url": "https://example.test/detail/1",
        "status": "detail_captured",
        "source_capture_id": "capture-source",
        "final_capture_id": "capture-menu-ready",
        "page_type": "content_detail",
        "menu_probe_capture_id": "capture-menu-probe",
        "menu_ready_capture_id": "capture-menu-ready",
        "menu_page_type": "content_detail",
        "menu_readiness_evidence_capture_ids": [
            "capture-menu-probe",
            "capture-menu-ready",
        ],
        "detail_capture_id": "capture-menu-ready",
        "detail_ready_capture_id": "capture-menu-ready",
        "detail_page_type": "content_detail",
    }
    if action_count is not None:
        actual["detail_action_count"] = action_count

    errors = _validate_live_ledger_entry(
        "https://example.test/",
        "https://example.test",
        {
            "entry_id": "menu-direct-detail",
            "menu_path": ["详情"],
            "href": "/detail/1",
            "decision": "allowlisted_observe_only",
        },
        actual,
    )

    assert any("详情动作次数" in item for item in errors)


@pytest.mark.parametrize(
    ("detail_capture_id", "detail_evidence_ids"),
    (
        ("capture-other", ["capture-detail-probe", "capture-detail-ready"]),
        ("capture-detail-ready", []),
        (
            "capture-detail-ready",
            ["capture-other", "capture-detail-ready"],
        ),
        (
            "capture-detail-ready",
            ["capture-detail-probe", "capture-other"],
        ),
    ),
)
def test_live_ledger_rejects_broken_list_detail_readiness_lineage(
    detail_capture_id,
    detail_evidence_ids,
) -> None:
    errors = _validate_live_ledger_entry(
        "https://example.test/",
        "https://example.test",
        {
            "entry_id": "menu-list",
            "menu_path": ["列表"],
            "href": "/list",
            "decision": "allowlisted_observe_only",
        },
        {
            "entry_id": "menu-list",
            "menu_path": ["列表"],
            "canonical_url": "https://example.test/list",
            "status": "detail_captured",
            "source_capture_id": "capture-source",
            "final_capture_id": "capture-menu-ready",
            "page_type": "content_list",
            "menu_probe_capture_id": "capture-menu-probe",
            "menu_ready_capture_id": "capture-menu-ready",
            "menu_page_type": "content_list",
            "menu_readiness_evidence_capture_ids": [
                "capture-menu-probe",
                "capture-menu-ready",
            ],
            "selected_detail_candidate_id": "candidate-detail",
            "detail_candidate_ids": ["candidate-detail", "candidate-other"],
            "detail_selection_confidence": 0.96,
            "detail_selection_reason": "首个可信重复内容卡片",
            "detail_probe_capture_id": "capture-detail-probe",
            "detail_ready_capture_id": "capture-detail-ready",
            "detail_capture_id": detail_capture_id,
            "detail_page_type": "content_detail",
            "detail_readiness_evidence_capture_ids": detail_evidence_ids,
        },
    )

    assert any("详情 readiness 血缘" in item for item in errors)


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    (
        ({"detail_candidate_ids": ["candidate-other"]}, "候选清单"),
        (
            {"detail_candidate_ids": ["candidate-detail", "candidate-detail"]},
            "候选清单",
        ),
        ({"detail_selection_confidence": 0.69}, "置信度"),
        ({"detail_selection_reason": "第一行\n伪造报告"}, "选择理由"),
        ({"detail_selection_reason": "联系 public@example.com"}, "选择理由"),
        ({"detail_selection_reason": "Bearer secret-value"}, "选择理由"),
        (
            {"detail_selection_reason": "abcdefghijkl.mnopqrstuvwx.yz0123456789"},
            "选择理由",
        ),
        (
            {"detail_selection_reason": "0123456789abcdef0123456789abcdef"},
            "选择理由",
        ),
        (
            {"detail_selection_reason": "AbCdEfGhIjKlMnOpQrStUvWxYz012345"},
            "选择理由",
        ),
        ({"detail_selection_reason": "Bearer%2520secret-value"}, "选择理由"),
    ),
)
def test_live_ledger_rejects_invalid_detail_selection_evidence(
    mutation,
    expected_error,
) -> None:
    actual = {
        "entry_id": "menu-list",
        "menu_path": ["列表"],
        "canonical_url": "https://example.test/list",
        "status": "detail_captured",
        "source_capture_id": "capture-source",
        "final_capture_id": "capture-menu-ready",
        "page_type": "content_list",
        "menu_probe_capture_id": "capture-menu-probe",
        "menu_ready_capture_id": "capture-menu-ready",
        "menu_page_type": "content_list",
        "menu_readiness_evidence_capture_ids": [
            "capture-menu-probe",
            "capture-menu-ready",
        ],
        "detail_candidate_ids": ["candidate-detail", "candidate-other"],
        "selected_detail_candidate_id": "candidate-detail",
        "detail_selection_confidence": 0.96,
        "detail_selection_reason": "首个可信重复内容卡片",
        "detail_probe_capture_id": "capture-detail-probe",
        "detail_ready_capture_id": "capture-detail-ready",
        "detail_capture_id": "capture-detail-ready",
        "detail_page_type": "content_detail",
        "detail_readiness_evidence_capture_ids": [
            "capture-detail-probe",
            "capture-detail-ready",
        ],
    }
    actual.update(mutation)

    errors = _validate_live_ledger_entry(
        "https://example.test/",
        "https://example.test",
        {
            "entry_id": "menu-list",
            "menu_path": ["列表"],
            "href": "/list",
            "decision": "allowlisted_observe_only",
        },
        actual,
    )

    assert any(expected_error in item for item in errors)


def test_markdown_cell_escapes_untrusted_table_and_html_syntax() -> None:
    rendered = _markdown_cell("第一行\n| <script> [链接] \\ 尾部")

    assert "\n" not in rendered
    assert "<script>" not in rendered
    assert "\\|" in rendered
    assert "\\[链接\\]" in rendered


def test_live_aggregate_rejects_failure_reason_on_success_status() -> None:
    catalog = json.loads(DEFAULT_CATALOG_PATH.read_text(encoding="utf-8"))
    catalog_site = catalog["sites"][0]
    site = LiveSiteResult(
        target_url=SIX_SITE_TARGETS[0],
        task_id="task-failure-reason",
        run_id="run-failure-reason",
        task_status="done",
        report_status="not_applicable",
        failure_reason="unexpected warning",
        report_stage="final",
        is_final=True,
        exploration_summary={
            "site_id": catalog_site["site_id"],
            "menu_total": 0,
            "executed_same_origin_count": 0,
            "frontier_remaining": 0,
            "status_counts": {},
            "menu_ledger": [],
        },
    )

    report = _aggregate_historical_fixture_results((site,))

    assert any("failure_reason 非空" in item for item in report.validation_errors)


def test_live_markdown_sanitizes_all_runtime_summary_fields() -> None:
    site = LiveSiteResult(
        target_url="https://example.test/<script>|site",
        task_id="123e4567-e89b-12d3-a456-426614174000",
        run_id="223e4567-e89b-12d3-a456-426614174000",
        task_status="done\n## forged",
        report_status="Bearer%2520secret-value",
        failure_reason=(
            "failure\n<script> token=credential-value "
            "AbCdEfGhIjKlMnOpQrStUvWxYz012345 Bearer%2520secret-value"
        ),
        report_stage="final|stage",
        is_final=True,
        exploration_summary={
            "site_id": "example",
            "menu_total": 0,
            "executed_same_origin_count": 0,
            "menu_limit_skipped_count": 0,
            "frontier_remaining": 0,
            "page_type_keys": ["home\n## forged"],
            "status_counts": {"done\n## forged": 1},
            "menu_ledger": [
                {
                    "menu_path": ["详情"],
                    "canonical_url": (
                        "https://report-user:report-pass@example.test/path"
                        "?opaque_parameter=visible#fragment"
                    ),
                    "status": "detail_captured",
                }
            ],
        },
        report_overall_status="AbCdEfGhIjKlMnOpQrStUvWxYz012345",
        report_error_code="0123456789abcdef0123456789abcdef",
    )

    rendered = render_live_markdown(_aggregate_historical_fixture_results((site,)))

    assert "secret-value" not in rendered
    assert "credential-value" not in rendered
    assert "0123456789abcdef0123456789abcdef" not in rendered
    assert "AbCdEfGhIjKlMnOpQrStUvWxYz012345" not in rendered
    assert "Bearer%2520secret-value" not in rendered
    assert "report-user" not in rendered
    assert "report-pass" not in rendered
    assert "opaque_parameter" not in rendered
    assert "fragment" not in rendered
    assert "https://example.test/path" in rendered
    assert "<script>" not in rendered
    assert "\n## forged" not in rendered
    assert r"\|site" in rendered
    assert "123e4567-e89b-12d3-a456-426614174000" in rendered
    assert "223e4567-e89b-12d3-a456-426614174000" in rendered


def test_live_cli_stdout_uses_safe_summary_and_never_emits_raw_runtime_data(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path,
) -> None:
    malicious_task = "AbCdEfGhIjKlMnOpQrStUvWxYz012345"
    malicious_run = "BearerSecretValueAbCdEfGhIjKlMnOpQr"
    malicious_summary = "token:ZyXwVuTsRqPoNmLkJiHgFeDcBa987654"
    site = LiveSiteResult(
        target_url=SIX_SITE_TARGETS[0],
        task_id=malicious_task,
        run_id=malicious_run,
        task_status="done",
        report_status="not_applicable",
        failure_reason=malicious_summary,
        report_stage="final",
        is_final=True,
        exploration_summary={
            "site_id": malicious_summary,
            "menu_total": 0,
            "executed_same_origin_count": 0,
            "menu_limit_skipped_count": 0,
            "frontier_remaining": 0,
            "status_counts": {malicious_summary: 1},
            "menu_ledger": [{"detail_selection_reason": malicious_summary}],
        },
    )
    live_report = _aggregate_historical_fixture_results((site,))
    monkeypatch.setenv("ALIOTH_WEB_V2_ACCESS_TOKEN", "test-access-token")
    live_kwargs: dict[str, object] = {}

    def _run_selected_sites(**kwargs):
        live_kwargs.update(kwargs)
        return live_report

    monkeypatch.setattr(
        "scripts.web_v2_six_site_shadow.run_live_sites",
        _run_selected_sites,
    )

    exit_code = main(
        [
            "--live",
            "--adult-self-attestation",
            "adult-v1",
            "--site-id",
            "www_91jav1_com",
            "--site-id",
            "heiliao_com",
            "--live-report-path",
            str(tmp_path / "live-report.md"),
            "--live-html-report-path",
            str(tmp_path / "live-report.html"),
        ]
    )
    stdout = capsys.readouterr().out

    assert exit_code == 1
    assert malicious_task not in stdout
    assert malicious_run not in stdout
    assert malicious_summary not in stdout
    assert '"validation_error_count"' in stdout
    assert '"sites"' in stdout
    assert live_kwargs["targets"] == (SIX_SITE_TARGETS[0], SIX_SITE_TARGETS[2])
    assert (tmp_path / "live-report.html").read_text(encoding="utf-8").startswith("<!doctype html>")


@pytest.mark.parametrize(
    ("identifier_kind", "malicious"),
    (
        ("task_run", "AbCdEfGhIjKlMnOpQrStUvWxYz012345"),
        ("capture", "BearerSecretValueAbCdEfGhIjKlMnOpQr"),
        ("candidate", "token:AbCdEfGhIjKlMnOpQrStUvWxYz012345"),
        ("action", "action-execution:BearerSecretValueAbCdEfGhIjKlMnOpQr"),
    ),
)
def test_markdown_identifier_rejects_high_entropy_values_by_field(
    identifier_kind: str,
    malicious: str,
) -> None:
    rendered = _markdown_identifier(
        malicious,
        identifier_kind=identifier_kind,
    )

    assert malicious not in rendered
    assert "redacted-sensitive" in rendered


def test_markdown_identifier_accepts_production_action_execution_id() -> None:
    action_execution_id = _fixture_action_execution_id("report:menu-action")

    assert (
        _markdown_identifier(action_execution_id, identifier_kind="action") == action_execution_id
    )
    assert "redacted-sensitive" in _markdown_identifier(
        _fixture_uuid("report:not-an-action"),
        identifier_kind="action",
    )


def test_live_ledger_rejects_forged_candidate_and_capture_ids() -> None:
    candidate_id = "token:AbCdEfGhIjKlMnOpQrStUvWxYz012345"
    actual = {
        "entry_id": "menu-list",
        "menu_path": ["列表"],
        "canonical_url": "https://example.test/list",
        "status": "detail_captured",
        "source_capture_id": "BearerSecretValueAbCdEfGhIjKlMnOpQr",
        "final_capture_id": "123e4567-e89b-12d3-a456-426614174000",
        "page_type": "content_list",
        "menu_action_count": 1,
        "menu_intent_id": _fixture_uuid("forged:menu-intent"),
        "menu_action_execution_id": "action-execution:BearerSecretValue",
        "menu_probe_capture_id": "323e4567-e89b-12d3-a456-426614174000",
        "menu_ready_capture_id": "123e4567-e89b-12d3-a456-426614174000",
        "menu_page_type": "content_list",
        "menu_readiness_evidence_capture_ids": [
            "323e4567-e89b-12d3-a456-426614174000",
            "123e4567-e89b-12d3-a456-426614174000",
        ],
        "detail_candidate_ids": [candidate_id],
        "selected_detail_candidate_id": candidate_id,
        "detail_selection_confidence": 0.96,
        "detail_selection_reason": "首个可信重复内容卡片",
        "detail_probe_capture_id": "423e4567-e89b-12d3-a456-426614174000",
        "detail_ready_capture_id": "523e4567-e89b-12d3-a456-426614174000",
        "detail_capture_id": "523e4567-e89b-12d3-a456-426614174000",
        "detail_action_count": 1,
        "detail_intent_id": _fixture_uuid("forged:detail-intent"),
        "detail_action_execution_id": "action-execution:BearerSecretValue",
        "detail_page_type": "content_detail",
        "detail_readiness_evidence_capture_ids": [
            "423e4567-e89b-12d3-a456-426614174000",
            "523e4567-e89b-12d3-a456-426614174000",
        ],
    }

    errors = _validate_live_ledger_entry(
        "https://example.test/",
        "https://example.test",
        {
            "entry_id": "menu-list",
            "menu_path": ["列表"],
            "href": "/list",
            "decision": "allowlisted_observe_only",
        },
        actual,
    )

    assert any("Capture ID 格式" in item for item in errors)
    assert any("Action ID 格式" in item for item in errors)
    assert any("候选 ID 格式" in item for item in errors)


def test_live_aggregate_rejects_forged_task_and_run_ids() -> None:
    site = LiveSiteResult(
        target_url=SIX_SITE_TARGETS[0],
        task_id="AbCdEfGhIjKlMnOpQrStUvWxYz012345",
        run_id="BearerSecretValueAbCdEfGhIjKlMnOpQr",
        task_status="done",
        report_status="not_applicable",
        failure_reason=None,
        report_stage="final",
        is_final=True,
        exploration_summary={
            "site_id": "heiliao_com",
            "menu_total": 0,
            "executed_same_origin_count": 0,
            "menu_limit_skipped_count": 0,
            "frontier_remaining": 0,
            "status_counts": {},
            "menu_ledger": [],
        },
    )

    report = _aggregate_historical_fixture_results((site,))

    assert any("task_id 格式" in item for item in report.validation_errors)
    assert any("run_id 格式" in item for item in report.validation_errors)


def test_live_aggregate_rejects_navigation_failures_even_when_totals_match() -> None:
    catalog = json.loads(DEFAULT_CATALOG_PATH.read_text(encoding="utf-8"))
    catalog_site = catalog["sites"][0]
    ledger = [
        {
            "entry_id": entry["entry_id"],
            "menu_path": entry["menu_path"],
            "canonical_url": entry["href"],
            "status": (
                "home_baseline" if entry["decision"] == "home_baseline" else "navigation_failed"
            ),
            "source_capture_id": "capture-source",
        }
        for entry in catalog_site["entries"]
    ]
    site = LiveSiteResult(
        target_url=SIX_SITE_TARGETS[0],
        task_id="task-failed",
        run_id="run-failed",
        task_status="done",
        report_status="not_applicable",
        failure_reason=None,
        report_stage="final",
        is_final=True,
        exploration_summary={
            "site_id": catalog_site["site_id"],
            "menu_total": len(ledger),
            "executed_same_origin_count": len(ledger) - 1,
            "frontier_remaining": 0,
            "status_counts": dict(Counter(item["status"] for item in ledger)),
            "menu_ledger": ledger,
        },
    )

    report = _aggregate_historical_fixture_results((site,))

    assert any("navigation_failed" in error for error in report.validation_errors)


@pytest.mark.parametrize("task_status", ("review", "cancelled"))
def test_live_aggregate_explicitly_rejects_non_success_task_terminal(
    task_status: str,
) -> None:
    catalog = json.loads(DEFAULT_CATALOG_PATH.read_text(encoding="utf-8"))
    catalog_site = catalog["sites"][0]
    site = LiveSiteResult(
        target_url=SIX_SITE_TARGETS[0],
        task_id=f"task-{task_status}",
        run_id=f"run-{task_status}",
        task_status=task_status,
        report_status="not_applicable",
        failure_reason=None,
        report_stage="final",
        is_final=True,
        exploration_summary={
            "site_id": catalog_site["site_id"],
            "menu_total": 0,
            "executed_same_origin_count": 0,
            "frontier_remaining": 0,
            "status_counts": {},
            "menu_ledger": [],
        },
    )

    report = _aggregate_historical_fixture_results((site,))

    assert any(f"任务终态为 {task_status}" in item for item in report.validation_errors)
def test_acceptance_separates_contract_from_real_coverage_and_manual_review():
    from scripts.web_v2_six_site_shadow import LiveSiteResult

    site = LiveSiteResult(target_url="https://example.test", task_id="task", run_id="run",
        task_status="review", report_status="ready", failure_reason=None, report_stage="preview",
        is_final=False, exploration_summary={}, page_type_review_count=1, pending_confirmation_count=0,
        inspection_total_count=1, inspection_pending_count=1, duplicate_url_count=0)
    dimensions = site.acceptance_dimensions
    assert set(dimensions) == {"contract_integrity", "actual_coverage", "ledger_integrity", "manual_review", "server_gate", "report_binding"}
    assert dimensions["actual_coverage"]["status"] == "unverified"
    assert dimensions["manual_review"]["status"] == "not_recorded"
    assert dimensions["report_binding"]["status"] == "preview"


def test_primary_capture_public_normalizer_keeps_failure_lineage_without_raw_diagnostics():
    from scripts.web_v2_live_evidence import _normalize_primary_content_capture

    result = _normalize_primary_content_capture({"status": "failed", "reason_code": "WEB_PRIMARY_CONTENT_CAPTURE_FAILED",
        "parent_capture_id": "baseline", "direct_parent_capture_id": "before", "raw": "PRIVATE",
        "attempts": [{"source_capture_id": "source", "before_capture_id": "before", "after_capture_id": "after",
                      "action_execution_id": "action", "stage": "observe", "selector": "PRIVATE"}]})
    assert result["reason_code"] == "WEB_PRIMARY_CONTENT_CAPTURE_FAILED"
    assert result["direct_parent_capture_id"] == "before"
    assert result["attempts"][0]["action_execution_id"] == "action"
    assert "PRIVATE" not in str(result)


def test_identity_only_is_not_contract_pass_and_invalid_parent_does_not_leak():
    from scripts.web_v2_live_evidence import _normalize_primary_content_capture

    site = LiveSiteResult(target_url="https://example.test", task_id="task", run_id="run",
        task_status="review", report_status="ready", failure_reason=None, report_stage="preview",
        is_final=False, exploration_summary={}, duplicate_url_count=0,
        logical_page_assessments=({"logical_page_id": "logical"},))
    assert site.acceptance_dimensions["contract_integrity"]["status"] == "unverified"
    assert "PRIVATE" not in str(_normalize_primary_content_capture({"parent_capture_id": "https://x/?PRIVATE"}))


def test_live_html_carries_all_six_dimensions_and_manual_requires_this_run_records():
    from dataclasses import replace

    site = LiveSiteResult(target_url="https://example.test", task_id="task", run_id="run",
        task_status="review", report_status="ready", failure_reason=None, report_stage="preview",
        is_final=False, exploration_summary={}, logical_page_assessments=({"logical_page_id": "logical"},),
        task_list_report_verified=True, task_detail_report_verified=True,
        manual_page_reviews=({"task_id": "task", "run_id": "old", "logical_page_id": "logical",
                             "reviewer": "实际复核者", "reviewed_at": "2026-09-11T00:00:00+08:00", "reviewed": True},))
    assert site.acceptance_dimensions["manual_review"]["status"] == "not_recorded"
    current = replace(site, manual_page_reviews=(dict(site.manual_page_reviews[0], run_id="run"),))
    assert current.acceptance_dimensions["manual_review"]["status"] == "completed"
    assert "缺明确" not in current.acceptance_dimensions["manual_review"]["reason"]
    rendered = render_live_html(_aggregate_historical_fixture_results((site,)))
    assert all(name in rendered for name in site.acceptance_dimensions)


@pytest.mark.parametrize("case,expected,completed,missing,blocked", [
    ("zero", "incomplete", 0, 4, 1),
    ("partial", "incomplete", 1, 3, 0),
    ("blocked", "incomplete", 4, 0, 1),
    ("complete", "complete", 4, 0, 0),
    ("wrong_run", "unverified", None, None, 0),
    ("missing_field", "unverified", None, None, 0),
    ("conflict", "unverified", None, None, 0),
    ("absent", "unverified", None, None, 0),
    ("wrong_task", "unverified", None, None, 0),
    ("missing_run", "unverified", None, None, 0),
    ("missing_hash", "unverified", None, None, 0),
    ("boolean_epoch", "unverified", None, None, 0),
    ("unknown_type", "unverified", None, None, 0),
    ("review", "incomplete", 4, 0, 0),
    ("pending", "incomplete", 4, 0, 0),
])
def test_live_actual_coverage_uses_frozen_targets_not_empty_inspection_queue(
    case, expected, completed, missing, blocked
):
    required = ["content_detail", "content_list", "video_detail", "video_list"]
    observed = [] if case == "zero" else required[:1] if case == "partial" else required
    coverage = {
        "coverage_snapshot_id": "snapshot-1", "snapshot_hash": "a" * 64,
        "finalization_epoch": 2, "required_page_types": required,
        "observed_page_types": observed, "missing_page_types": sorted(set(required) - set(observed)),
        "unrecognized_page_types": [],
        "coverage_status": "completed_with_gaps" if len(observed) < 4 else "complete",
    }
    if case == "missing_field":
        coverage.pop("required_page_types")
    if case == "conflict":
        coverage["missing_page_types"] = ["content_list"]
    if case == "missing_hash":
        coverage.pop("snapshot_hash")
    if case == "boolean_epoch":
        coverage["finalization_epoch"] = True
    if case == "unknown_type":
        coverage["observed_page_types"] = required + ["landing"]
    statuses = ["human_verification_blocked"] if blocked else ["detail_captured"]
    statuses += ["menu_limit_skipped", "cross_origin_blocked"]

    def requester(method, endpoint, payload, headers):
        if method == "POST":
            return {"task_id": "task", "run_id": "run", "status": "queued"}
        if endpoint.endswith("/web-evidence"):
            return {}
        if "/workstation/tasks/" in endpoint:
            return {"task_id": "task", "run_id": "run", "status": "done"}
        return {
            "task_id": "other" if case == "wrong_task" else "task",
            "summary": {"run_id": None if case == "missing_run" else "other" if case == "wrong_run" else "run"},
            "report_stage": "final", "is_final": True, "export_allowed": True,
            "page_exploration": {
                "web_coverage": coverage if case != "absent" else None,
                "menu_ledger": [{"status": status} for status in statuses],
            },
            "web_preview": {"inspection_progress": {"total": len(observed), "pending": 1 if case == "pending" else 0},
                            "page_type_review": {"pending": 1 if case == "review" else 0}},
        }

    site = run_live_sites(
        api_base_url="https://alioth.example.test", access_token="fixture-token",
        adult_self_attestation="adult-v1", requester=requester,
        sleep=lambda _: None, monotonic=lambda: 0.0, site_timeout_seconds=60,
        targets=(SIX_SITE_TARGETS[0],),
    ).sites[0]
    actual = site.acceptance_dimensions["actual_coverage"]
    assert actual["status"] == expected
    assert actual["completed"] == completed
    assert actual["missing"] == missing
    assert actual["unit"] == "page_type"
    assert actual["menu_entries"]["blocked"] == blocked
    assert actual["menu_entries"]["excluded"] == 2
    assert actual["menu_entries"]["unit"] == "menu_entry"
    assert actual["inspection_queue"]["pending"] == (1 if case == "pending" else 0)
    if completed is not None:
        assert actual["required"] == 4
        assert actual["coverage_snapshot_id"] == "snapshot-1"
        assert actual["snapshot_hash"] == "a" * 64
        assert actual["finalization_epoch"] == 2
