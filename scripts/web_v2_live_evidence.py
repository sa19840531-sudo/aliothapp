"""六站 Web V2 实况证据的白名单投影与严格门禁。"""

from __future__ import annotations

import re
import urllib.parse
from types import SimpleNamespace
from typing import Any

from services.api.app.application.web_assessment_evidence import (
    sanitize_web_model_reason,
)
from services.api.app.application.web_evidence_bundle import public_web_ad_landing_evidence
from services.api.app.application.web_navigation_lineage import logical_navigation_identity
from services.api.app.domain.page.web_origin import web_origin_matches

_UUID_ID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
    re.IGNORECASE,
)
_ACTION_EXECUTION_ID_PATTERN = re.compile(r"action-execution:[0-9a-f]{64}")
_SHA256_CHECKSUM_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
_WEB_LIST_PAGE_TYPES = frozenset({"video_list", "content_list"})
_WEB_DETAIL_PAGE_TYPES = frozenset({"video_detail", "content_detail"})
_PUBLIC_ARTIFACT_TYPES = frozenset(
    {"screenshot", "dom", "visible_text", "ui_snapshot", "action_trace"}
)


def normalize_live_site_evidence_bundle(
    raw: dict[str, object],
) -> dict[str, object]:
    """只保留报告所需证据白名单，禁止原始 DOM、路径和凭据进入报告。"""

    summary_raw = raw.get("summary")
    summary_raw = summary_raw if isinstance(summary_raw, dict) else {}
    entries_raw = raw.get("entries")
    entries_raw = entries_raw if isinstance(entries_raw, list) else []
    captures_raw = raw.get("captures")
    captures_raw = captures_raw if isinstance(captures_raw, list) else []
    formal_pages_raw = raw.get("formal_pages")
    formal_pages_raw = formal_pages_raw if isinstance(formal_pages_raw, list) else []
    entry_keys = (
        "menu_entry_id",
        "source_kind",
        "decision",
        "execution_slot",
        "error_code",
        "menu_source_capture_id",
        "parent_menu_entry_id",
        "menu_depth",
        "status",
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
        "logical_target_identity",
        "parent_logical_target_identity",
    )
    entries = [
        {
            **{key: value.get(key) for key in entry_keys},
            "menu_path": [_safe_runtime_text(label) for label in value.get("menu_path", [])]
            if isinstance(value.get("menu_path"), (list, tuple)) else [],
            "canonical_url": _normalize_evidence_url(value.get("canonical_url")),
            "ad_landing_evidence": public_web_ad_landing_evidence(value.get("ad_landing_evidence")),
            "exploration_path": list(value["exploration_path"])
            if isinstance(value.get("exploration_path"), list | tuple)
            and all(isinstance(label, str) for label in value["exploration_path"])
            else [],
        }
        for value in entries_raw
        if isinstance(value, dict)
    ]
    captures: list[dict[str, object]] = []
    for value in captures_raw:
        if not isinstance(value, dict):
            continue
        artifacts_raw = value.get("artifacts")
        artifacts_raw = artifacts_raw if isinstance(artifacts_raw, list) else []
        artifacts = [
            {
                "artifact_id": item.get("artifact_id"),
                "artifact_type": item.get("artifact_type"),
                "content_type": item.get("content_type"),
                "checksum": item.get("checksum"),
            }
            for item in artifacts_raw
            if isinstance(item, dict)
        ]
        captures.append(
            {
                "capture_id": value.get("capture_id"),
                "parent_capture_id": value.get("parent_capture_id"),
                "url": _normalize_evidence_url(value.get("url")),
                "role": value.get("role"),
                "capture_purpose": value.get("capture_purpose"),
                "primary_content_capture": _normalize_primary_content_capture(
                    value.get("primary_content_capture")
                ),
                "page_type": value.get("page_type"),
                "action_execution_id": value.get("action_execution_id"),
                "artifacts": artifacts,
            }
        )
    reason_codes = raw.get("reason_codes")
    reason_codes = reason_codes if isinstance(reason_codes, list) else []
    return {
        "task_id": raw.get("task_id"),
        "agent_run_id": raw.get("agent_run_id"),
        "menu_limit": raw.get("menu_limit"),
        "site_id": raw.get("site_id"),
        "status": raw.get("status"),
        "reason_codes": [
            _safe_runtime_text(item) for item in reason_codes if isinstance(item, str)
        ],
        "summary": {
            "home_count": _safe_int(summary_raw.get("home_count")),
            "menu_count": _safe_int(summary_raw.get("menu_count")),
            "detail_count": _safe_int(summary_raw.get("detail_count")),
            "duplicate_formal_url_count": _safe_int(
                summary_raw.get("duplicate_formal_url_count")
            ),
        },
        "entries": entries,
        "captures": captures,
        "formal_pages": [
            {
                "logical_page_id": value.get("logical_page_id"),
                "logical_page_url": _normalize_evidence_url(
                    value.get("logical_page_url")
                ),
                "primary_capture_id": value.get("primary_capture_id"),
                "capture_purpose": value.get("capture_purpose"),
                "page_type": value.get("page_type"),
                "observation_capture_ids": [
                    item
                    for item in value.get("observation_capture_ids", [])
                    if isinstance(item, str)
                ],
                "product_capabilities": [
                    _safe_runtime_text(item)
                    for item in value.get("product_capabilities", [])
                    if isinstance(item, str)
                ],
                "entry_status": value.get("entry_status"),
                "visible_layer": value.get("visible_layer"),
            }
            for value in formal_pages_raw
            if isinstance(value, dict)
        ],
    }


def _normalize_primary_content_capture(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    from services.api.app.application.web_primary_content_diagnostic import (
        _identifier,
        primary_content_diagnostic,
    )

    return {
        **(primary_content_diagnostic(value) or {}),
        "direct_parent_capture_id": _identifier(value.get("direct_parent_capture_id")),
        "parent_capture_id": _identifier(value.get("parent_capture_id")),
        "scroll_count": _safe_int(value.get("scroll_count")),
        "before_ad_coverage": _safe_coverage(value.get("before_ad_coverage")),
        "after_ad_coverage": _safe_coverage(value.get("after_ad_coverage")),
    }


def _safe_coverage(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return max(0.0, min(float(value), 1.0))


def required_capture_ids_from_ledger(entries: object) -> set[str]:
    """提取公开账本引用的 Capture；跳过和跨域阻断必须是空血缘。"""

    if not isinstance(entries, list):
        return set()
    fields = (
        "menu_probe_capture_id",
        "menu_ready_capture_id",
        "detail_probe_capture_id",
        "detail_ready_capture_id",
    )
    captures = {
        str(entry.get(field_name) or "").strip()
        for entry in entries
        if isinstance(entry, dict)
        for field_name in fields
        if str(entry.get(field_name) or "").strip()
    }
    from services.api.app.application.web_primary_content_diagnostic import (
        primary_content_diagnostics,
    )

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for diagnostic in primary_content_diagnostics(entry.get("primary_content_diagnostics")):
            for field in ("parent_capture_id", "evidence_capture_id"):
                if diagnostic.get(field):
                    captures.add(diagnostic[field])
            for attempt in diagnostic.get("attempts") or []:
                for field in ("source_capture_id", "before_capture_id", "after_capture_id"):
                    if attempt.get(field):
                        captures.add(attempt[field])
    return captures


def validate_live_site_evidence_bundle(
    *,
    target_url: str,
    task_id: str,
    run_id: str,
    site_id: str,
    expected_menu_count: int,
    bundle: dict[str, object],
    allow_count_limit_skips: bool = True,
    expected_detail_count: int | None = None,
) -> list[str]:
    """验证单站证据计数、动作预算以及 URL+PNG+DOM+动作轨迹闭环。"""

    prefix = f"{target_url} evidence"
    expected_detail_count = expected_menu_count if expected_detail_count is None else expected_detail_count
    errors: list[str] = []
    if bundle.get("task_id") != task_id:
        errors.append(f"{prefix}: task_id 不一致")
    if bundle.get("agent_run_id") != run_id:
        errors.append(f"{prefix}: agent_run_id 不一致")
    if bundle.get("site_id") != site_id:
        errors.append(f"{prefix}: site_id 不一致")
    if bundle.get("status") != "evidence_ready":
        errors.append(f"{prefix}: status 必须为 evidence_ready")
    if bundle.get("reason_codes") != []:
        errors.append(f"{prefix}: reason_codes 必须为空")
    summary = bundle.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    expected_summary = {
        "home_count": 1,
        "menu_count": expected_menu_count,
        "detail_count": expected_detail_count,
    }
    for name, expected in expected_summary.items():
        if _safe_int(summary.get(name)) != expected:
            errors.append(
                f"{prefix}: {name} expected={expected}, actual={_safe_int(summary.get(name))}"
            )

    entries = bundle.get("entries")
    if not isinstance(entries, list):
        return [*errors, f"{prefix}: entries 不是数组"]
    home_entries = [
        entry
        for entry in entries
        if isinstance(entry, dict) and entry.get("status") == "home_baseline"
    ]
    detail_entries = [
        entry
        for entry in entries
        if isinstance(entry, dict) and entry.get("status") == "detail_captured"
    ]
    if len(home_entries) != 1:
        errors.append(f"{prefix}: 首页证据条目必须恰好 1 个")
    if len(detail_entries) != expected_detail_count:
        errors.append(
            f"{prefix}: 详情证据条目 expected={expected_detail_count}, actual={len(detail_entries)}"
        )

    public_entry_ids: set[str] = set()
    expected_capture_types: dict[str, str] = {}
    expected_capture_actions: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            errors.append(f"{prefix}: entries 包含非对象")
            continue
        entry_id = str(entry.get("menu_entry_id") or "").strip()
        if not entry_id or entry_id in public_entry_ids:
            errors.append(f"{prefix}: menu_entry_id 缺失或重复")
        public_entry_ids.add(entry_id)
        if not allow_count_limit_skips and entry.get("status") == "menu_limit_skipped":
            errors.append(f"{prefix}: 新实况不得包含 menu_limit_skipped")
        errors.extend(_validate_public_evidence_entry(prefix, entry))
        for capture_key, page_key in (
            ("menu_probe_capture_id", "menu_page_type"),
            ("menu_ready_capture_id", "menu_page_type"),
            ("detail_probe_capture_id", "detail_page_type"),
            ("detail_ready_capture_id", "detail_page_type"),
        ):
            capture_id = str(entry.get(capture_key) or "").strip()
            page_type = str(entry.get(page_key) or "").strip()
            if capture_id and page_type:
                existing_page_type = expected_capture_types.get(capture_id)
                if existing_page_type and existing_page_type != page_type:
                    errors.append(f"{prefix}: {capture_id} 被绑定到冲突页型")
                expected_capture_types[capture_id] = page_type
        for capture_key, action_key in (
            ("menu_probe_capture_id", "menu_action_execution_id"),
            ("detail_probe_capture_id", "detail_action_execution_id"),
        ):
            capture_id = str(entry.get(capture_key) or "").strip()
            action_id = str(entry.get(action_key) or "").strip()
            if capture_id and action_id:
                expected_capture_actions[capture_id] = action_id

    required_capture_ids = required_capture_ids_from_ledger(entries)
    captures = bundle.get("captures")
    if not isinstance(captures, list):
        return [*errors, f"{prefix}: captures 不是数组"]
    capture_by_id: dict[str, dict[str, object]] = {}
    for capture in captures:
        if not isinstance(capture, dict):
            errors.append(f"{prefix}: captures 包含非对象")
            continue
        capture_id = str(capture.get("capture_id") or "").strip()
        if not _valid_uuid_id(capture_id) or capture_id in capture_by_id:
            errors.append(f"{prefix}: Capture ID 缺失、格式无效或重复")
            continue
        capture_by_id[capture_id] = capture
    missing = sorted(required_capture_ids - set(capture_by_id))
    if missing:
        errors.append(f"{prefix}: 缺少账本引用 Capture {missing}")
    for capture_id in sorted(capture_by_id):
        capture = capture_by_id[capture_id]
        if not _normalize_evidence_url(capture.get("url")):
            errors.append(f"{prefix}: {capture_id} 缺少脱敏 URL")
        role = str(capture.get("role") or "").strip()
        if role not in {"observation", "bootstrap", "before_action", "after_action"}:
            errors.append(f"{prefix}: {capture_id} role 无效")
        action_id = str(capture.get("action_execution_id") or "").strip()
        if role in {"before_action", "after_action"} and not valid_action_execution_id(
            action_id
        ):
            errors.append(f"{prefix}: {capture_id} Action Execution ID 无效")
        if role in {"observation", "bootstrap"} and action_id:
            errors.append(f"{prefix}: {capture_id} 初始观察不得绑定 Action")
        expected_action_id = expected_capture_actions.get(capture_id)
        if expected_action_id and action_id != expected_action_id:
            errors.append(f"{prefix}: {capture_id} Action Execution ID 与账本不一致")
        parent_capture_id = capture.get("parent_capture_id")
        if parent_capture_id is not None and not _valid_uuid_id(parent_capture_id):
            errors.append(f"{prefix}: {capture_id} parent Capture ID 无效")
        expected_page_type = expected_capture_types.get(capture_id)
        if expected_page_type and capture.get("page_type") != expected_page_type:
            errors.append(f"{prefix}: {capture_id} 页面类型与账本不一致")
        errors.extend(
            _validate_capture_artifacts(
                prefix,
                capture_id,
                capture,
                require_action_trace=role == "after_action",
            )
        )
    return errors


def validate_report_evidence_identity(
    site: Any, *, require_navigation_identity: bool = False,
    catalog_entry_ids: frozenset[str] = frozenset(),
) -> list[str]:
    """保证最终报告账本与数据库证据包指向同一组菜单及动作。"""

    prefix = f"{site.target_url} report/evidence"
    report_entries = site.exploration_summary.get("menu_ledger")
    evidence_entries = site.evidence_bundle.get("entries")
    if not isinstance(report_entries, list) or not isinstance(evidence_entries, list):
        return [f"{prefix}: 缺少可对账的 entries"]
    runtime_entries = site.exploration_summary.get("runtime_discovered_ledger", [])
    report_entries = [*report_entries, *(
        row.get("execution", row) for row in runtime_entries if isinstance(row, dict)
    )]
    report_by_id = {
        str(entry.get("entry_id") or ""): entry
        for entry in report_entries
        if isinstance(entry, dict) and entry.get("entry_id")
    }
    evidence_by_id = {
        str(entry.get("menu_entry_id") or ""): entry
        for entry in evidence_entries
        if isinstance(entry, dict) and entry.get("menu_entry_id")
    }
    if set(report_by_id) != set(evidence_by_id):
        return [f"{prefix}: 菜单身份集合不一致"]
    errors: list[str] = []
    captures = site.evidence_bundle.get("captures")
    captures = captures if isinstance(captures, list) else []
    capture_by_id = {
        str(capture.get("capture_id") or ""): capture
        for capture in captures if isinstance(capture, dict)
    }
    compared_fields = (
        "status",
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
    for entry_id in sorted(report_by_id):
        report_entry = report_by_id[entry_id]
        evidence_entry = evidence_by_id[entry_id]
        if require_navigation_identity:
            errors.extend(_validate_navigation_identity(
                f"{prefix} [{entry_id}]", report_entry, evidence_entry, capture_by_id,
                runtime_identity_required=entry_id not in catalog_entry_ids,
            ))
        for field_name in compared_fields:
            report_value = report_entry.get(field_name)
            if (
                report_entry.get("status") == "detail_captured"
                and _safe_int(report_entry.get("detail_action_count")) == 0
                and report_entry.get("menu_page_type") in _WEB_DETAIL_PAGE_TYPES
            ):
                if field_name == "detail_ready_capture_id" and not report_value:
                    report_value = report_entry.get("menu_ready_capture_id")
                elif field_name == "detail_page_type" and not report_value:
                    report_value = report_entry.get("menu_page_type")
            if report_value != evidence_entry.get(field_name):
                errors.append(f"{prefix} [{entry_id}]: {field_name} 不一致")
    return errors


def _validate_navigation_identity(
    prefix: str,
    report_entry: dict[str, object],
    evidence_entry: dict[str, object],
    capture_by_id: dict[str, dict[str, object]],
    *,
    runtime_identity_required: bool,
) -> list[str]:
    """以持久导航哈希证明 URL/菜单路径，再核对独立 Capture 的实际落地 URL。"""
    errors: list[str] = []
    entry_id = str(evidence_entry.get("menu_entry_id") or "")
    identity = evidence_entry.get("logical_target_identity")
    if runtime_identity_required or entry_id.startswith("discovery:") or identity:
        menu_path = report_entry.get("menu_path")
        exploration_path = evidence_entry.get("exploration_path")
        parent_identity = evidence_entry.get("parent_logical_target_identity")
        valid_shape = (
            isinstance(identity, str) and re.fullmatch(r"[0-9a-f]{64}", identity)
            and entry_id == "discovery:" + identity
            and isinstance(menu_path, list) and bool(menu_path)
            and all(isinstance(label, str) and label.strip() for label in menu_path)
            and isinstance(exploration_path, list) and bool(exploration_path)
            and all(isinstance(label, str) and label.strip() for label in exploration_path)
            and (parent_identity is None or isinstance(parent_identity, str)
                 and re.fullmatch(r"[0-9a-f]{64}", parent_identity))
            and isinstance(report_entry.get("canonical_url"), str)
            and bool(report_entry["canonical_url"])
        )
        if not valid_shape or logical_navigation_identity(SimpleNamespace(
            url=report_entry["canonical_url"], menu_path=menu_path,
            exploration_path=exploration_path, parent_logical_target_identity=parent_identity,
        )) != identity:
            errors.append(f"{prefix}: URL/menu_path 与持久导航身份不一致或缺失")
        for field_name in ("logical_target_identity", "parent_logical_target_identity", "exploration_path"):
            if report_entry.get(field_name) != evidence_entry.get(field_name):
                errors.append(f"{prefix}: {field_name} 与持久导航身份不一致")

    if evidence_entry.get("status") in {"home_baseline", "detail_captured"}:
        ready_id = str(evidence_entry.get("menu_ready_capture_id") or "")
        landing = capture_by_id.get(ready_id, {}).get("url")
        try:
            target = str(report_entry.get("canonical_url") or "")
            target_path = urllib.parse.urlsplit(target).path or "/"
            landing_url = _normalize_evidence_url(landing)
            same_origin = web_origin_matches(landing_url, target, allow_http_upgrade=True)
            if not same_origin or urllib.parse.urlsplit(landing_url).path != target_path:
                errors.append(f"{prefix}: 菜单落地 URL 与关联 Capture 不一致")
        except ValueError:
            errors.append(f"{prefix}: 菜单落地 URL 无法验证")
    return errors


def build_live_process_nodes(
    *,
    submission_status: str | None,
    task_status: str,
    report_stage: str,
    evidence_bundle: dict[str, object],
) -> tuple[dict[str, object], ...]:
    """构造可审计但不含原始页面内容的过程节点。"""

    nodes: list[dict[str, object]] = [
        {
            "phase": "task_submitted",
            "status": submission_status or "submitted",
            "page_type": None,
        }
    ]
    entries = evidence_bundle.get("entries")
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            entry_id = str(entry.get("menu_entry_id") or "")
            status = str(entry.get("status") or "")
            menu_page_type = entry.get("menu_page_type")
            if status == "home_baseline":
                nodes.extend(
                    (
                        {
                            "phase": "home_probe",
                            "entry_id": entry_id,
                            "status": status,
                            "page_type": menu_page_type,
                            "capture_id": entry.get("menu_probe_capture_id"),
                        },
                        {
                            "phase": "home_ready",
                            "entry_id": entry_id,
                            "status": status,
                            "page_type": menu_page_type,
                            "capture_id": entry.get("menu_ready_capture_id"),
                        },
                    )
                )
                continue
            if status != "detail_captured":
                nodes.append(
                    {
                        "phase": "menu_not_executed",
                        "entry_id": entry_id,
                        "status": status,
                        "page_type": None,
                        "capture_id": None,
                    }
                )
                continue
            nodes.extend(
                (
                    {
                        "phase": "menu_action",
                        "entry_id": entry_id,
                        "status": "completed",
                        "page_type": None,
                        "action_execution_id": entry.get("menu_action_execution_id"),
                    },
                    {
                        "phase": "menu_probe",
                        "entry_id": entry_id,
                        "status": status,
                        "page_type": menu_page_type,
                        "capture_id": entry.get("menu_probe_capture_id"),
                    },
                    {
                        "phase": "menu_ready",
                        "entry_id": entry_id,
                        "status": status,
                        "page_type": menu_page_type,
                        "capture_id": entry.get("menu_ready_capture_id"),
                    },
                    {
                        "phase": "menu_classified",
                        "entry_id": entry_id,
                        "status": status,
                        "page_type": menu_page_type,
                        "capture_id": entry.get("menu_ready_capture_id"),
                    },
                )
            )
            if int(entry.get("detail_action_count") or 0) == 1:
                nodes.extend(
                    (
                        {
                            "phase": "detail_action",
                            "entry_id": entry_id,
                            "status": "completed",
                            "page_type": None,
                            "action_execution_id": entry.get("detail_action_execution_id"),
                        },
                        {
                            "phase": "detail_probe",
                            "entry_id": entry_id,
                            "status": status,
                            "page_type": entry.get("detail_page_type"),
                            "capture_id": entry.get("detail_probe_capture_id"),
                        },
                        {
                            "phase": "detail_ready",
                            "entry_id": entry_id,
                            "status": status,
                            "page_type": entry.get("detail_page_type"),
                            "capture_id": entry.get("detail_ready_capture_id"),
                        },
                        {
                            "phase": "detail_classified",
                            "entry_id": entry_id,
                            "status": status,
                            "page_type": entry.get("detail_page_type"),
                            "capture_id": entry.get("detail_ready_capture_id"),
                        },
                    )
                )
            else:
                nodes.append(
                    {
                        "phase": "detail_projected",
                        "entry_id": entry_id,
                        "status": status,
                        "page_type": entry.get("detail_page_type"),
                        "capture_id": entry.get("detail_ready_capture_id"),
                    }
                )
    nodes.extend(
        (
            {"phase": "task_terminal", "status": task_status, "page_type": None},
            {"phase": "report_sealed", "status": report_stage, "page_type": None},
            {
                "phase": "evidence_sealed",
                "status": evidence_bundle.get("status"),
                "page_type": None,
            },
        )
    )
    return tuple(nodes)


def _normalize_evidence_url(value: object) -> str:
    """仅接受已脱敏 HTTP(S) URL；含凭据、查询或片段时直接拒绝。"""

    try:
        parsed = urllib.parse.urlsplit(str(value or "").strip())
        if (
            parsed.scheme.casefold() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            return ""
        hostname = parsed.hostname
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        netloc = hostname
        if parsed.port is not None:
            netloc = f"{hostname}:{parsed.port}"
        return urllib.parse.urlunsplit(
            (parsed.scheme.casefold(), netloc, parsed.path or "/", "", "")
        )
    except ValueError:
        return ""


def _validate_public_evidence_entry(
    prefix: str,
    entry: dict[str, object],
) -> list[str]:
    errors: list[str] = []
    entry_id = str(entry.get("menu_entry_id") or "-")
    label = f"{prefix} [{entry_id}]"
    status = str(entry.get("status") or "")
    menu_count = entry.get("menu_action_count")
    detail_count = entry.get("detail_action_count")
    menu_probe = str(entry.get("menu_probe_capture_id") or "").strip()
    menu_ready = str(entry.get("menu_ready_capture_id") or "").strip()
    detail_probe = str(entry.get("detail_probe_capture_id") or "").strip()
    detail_ready = str(entry.get("detail_ready_capture_id") or "").strip()
    for action_key in (
        "menu_action_execution_id",
        "detail_action_execution_id",
    ):
        action_id = entry.get(action_key)
        if action_id and not valid_action_execution_id(action_id):
            errors.append(f"{label}: {action_key} 格式无效")
    if status == "home_baseline":
        if (
            menu_count != 0
            or detail_count != 0
            or not menu_probe
            or not menu_ready
            or menu_probe == menu_ready
            or entry.get("menu_page_type") != "home"
            or entry.get("menu_action_execution_id")
            or entry.get("detail_action_execution_id")
            or detail_probe
            or detail_ready
        ):
            errors.append(f"{label}: 首页证据形态无效")
        return errors
    if status in {"menu_limit_skipped", "cross_origin_blocked"}:
        if any(
            (
                menu_count != 0,
                detail_count != 0,
                bool(entry.get("menu_action_execution_id")),
                bool(menu_probe),
                bool(menu_ready),
                bool(entry.get("detail_action_execution_id")),
                bool(detail_probe),
                bool(detail_ready),
            )
        ):
            errors.append(f"{label}: 跳过或跨域阻断不得产生动作/Capture 血缘")
        return errors
    if status != "detail_captured":
        return [f"{label}: 非成功终态 {status}"]
    menu_page_type = str(entry.get("menu_page_type") or "")
    if (
        menu_count != 1
        or not entry.get("menu_action_execution_id")
        or not menu_probe
        or not menu_ready
        or menu_probe == menu_ready
    ):
        errors.append(f"{label}: 菜单动作或 probe/ready 血缘无效")
    if menu_page_type in _WEB_LIST_PAGE_TYPES:
        if (
            detail_count != 1
            or not entry.get("detail_action_execution_id")
            or not detail_probe
            or not detail_ready
            or detail_probe == detail_ready
            or entry.get("detail_page_type") not in _WEB_DETAIL_PAGE_TYPES
        ):
            errors.append(f"{label}: 列表页必须且只能执行 1 次详情动作")
    elif menu_page_type in _WEB_DETAIL_PAGE_TYPES:
        if (
            detail_count != 0
            or entry.get("detail_action_execution_id")
            or detail_probe
            or detail_ready != menu_ready
            or entry.get("detail_page_type") != menu_page_type
        ):
            errors.append(f"{label}: 直接详情不得执行额外详情动作")
    else:
        errors.append(f"{label}: menu_page_type 不属于 Web 菜单四类型")
    return errors


def _validate_capture_artifacts(
    prefix: str,
    capture_id: str,
    capture: dict[str, object],
    *,
    require_action_trace: bool,
) -> list[str]:
    errors: list[str] = []
    artifacts = capture.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, list) else []
    artifact_types: set[str] = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        artifact_type = str(artifact.get("artifact_type") or "").strip()
        artifact_id = str(artifact.get("artifact_id") or "").strip()
        checksum = str(artifact.get("checksum") or "").strip()
        if not _valid_uuid_id(artifact_id):
            errors.append(f"{prefix}: {capture_id} Artifact ID 无效")
        if artifact_type not in _PUBLIC_ARTIFACT_TYPES:
            errors.append(f"{prefix}: {capture_id} 附件类型无效")
            continue
        if not _SHA256_CHECKSUM_PATTERN.fullmatch(checksum):
            errors.append(f"{prefix}: {capture_id} 附件 checksum 无效")
            continue
        artifact_types.add(artifact_type)
        if artifact_type == "screenshot" and artifact.get("content_type") != "image/png":
            errors.append(f"{prefix}: {capture_id} 截图必须为 PNG")
    if "screenshot" not in artifact_types:
        errors.append(f"{prefix}: {capture_id} 缺少 PNG 截图")
    if "dom" not in artifact_types:
        errors.append(f"{prefix}: {capture_id} 缺少 DOM")
    if require_action_trace and "action_trace" not in artifact_types:
        errors.append(f"{prefix}: {capture_id} 缺少 Action Trace")
    return errors


def _safe_runtime_text(value: object) -> str:
    try:
        return sanitize_web_model_reason(str(value))
    except ValueError:
        return "[redacted-sensitive]"


def _safe_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _valid_uuid_id(value: object) -> bool:
    return isinstance(value, str) and _UUID_ID_PATTERN.fullmatch(value.strip()) is not None


def valid_action_execution_id(value: object) -> bool:
    """只接受持久化 ActionExecution 的公开稳定标识格式。"""

    return (
        isinstance(value, str)
        and _ACTION_EXECUTION_ID_PATTERN.fullmatch(value.strip()) is not None
    )


__all__ = [
    "build_live_process_nodes",
    "normalize_live_site_evidence_bundle",
    "required_capture_ids_from_ledger",
    "valid_action_execution_id",
    "validate_live_site_evidence_bundle",
    "validate_report_evidence_identity",
]
