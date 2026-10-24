"""串行创建 H5 验收任务，并保存可复核的运行结果。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.verify_h5_acceptance_run import (  # noqa: E402
    AcceptanceCheck,
    login_for_access_token,
    verify_acceptance_payloads,
)
from scripts.verify_h5_acceptance_scenarios import (  # noqa: E402
    DEFAULT_SCENARIO_PATH,
    load_scenarios,
    validate_scenarios,
)
from services.api.app.domain.page.registry import (  # noqa: E402
    get_default_page_type_registry,
)

TERMINAL_TASK_STATUSES = frozenset({"done", "review", "failed", "canceled"})
CANCELLATION_SCENARIO_ID = "W46-H5-016"
TARGETED_RECAPTURE_SCENARIO_ID = "W46-H5-017"
CAPTURE_RETRY_SCENARIO_ID = "W46-H5-101"
BROWSER_CRASH_SCENARIO_ID = "W46-H5-103"
EVIDENCE_FAILURE_SCENARIO_ID = "W46-H5-104"
MODEL_CONTRACT_FAILURE_SCENARIO_ID = "W46-H5-105"
UNSTABLE_PAGE_SCENARIO_ID = "W46-H5-102"
HIGH_RISK_ACTION_SCENARIO_ID = "W46-H5-010"
SYSTEMIC_GATE_SCENARIO_IDS = frozenset({TARGETED_RECAPTURE_SCENARIO_ID})
PLACEHOLDER_HOSTS = frozenset({"example.test"})
FIXTURE_MANIFEST_SCHEMA_VERSION = "h5-acceptance-fixture-manifest.v1"
JsonRequest = Callable[..., dict[str, Any]]
Sleep = Callable[[float], None]
Monotonic = Callable[[], float]


def select_scenarios(
    rows: Iterable[dict[str, str]],
    *,
    scenario_ids: set[str] | None = None,
    priorities: set[str] | None = None,
    categories: set[str] | None = None,
) -> list[dict[str, str]]:
    """按命令行过滤条件保留场景，同时拒绝拼错的场景编号。"""
    materialized = list(rows)
    known_ids = {row["scenario_id"] for row in materialized}
    unknown_ids = sorted((scenario_ids or set()) - known_ids)
    if unknown_ids:
        raise ValueError(f"未知场景编号：{', '.join(unknown_ids)}")
    return [
        row
        for row in materialized
        if (not scenario_ids or row["scenario_id"] in scenario_ids)
        and (not priorities or row["priority"] in priorities)
        and (not categories or row["target_category"] in categories)
    ]


def load_target_map(path: str | Path | None) -> dict[str, str]:
    """读取场景到真实 URL 的显式映射。"""
    if path is None:
        return {}
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("target map 必须是 JSON object")
    result: dict[str, str] = {}
    for scenario_id, value in payload.items():
        if not isinstance(scenario_id, str) or not isinstance(value, str):
            raise ValueError("target map 的场景编号和 URL 必须是字符串")
        result[scenario_id.strip()] = _require_http_url(value, "target map URL")
    return result


def resolve_target_url(
    row: Mapping[str, str],
    *,
    target_map: Mapping[str, str],
    fixture_base_url: str | None,
) -> str:
    """解析真实验收地址，禁止把 example.test 直接提交为任务。"""
    scenario_id = row["scenario_id"]
    mapped = target_map.get(scenario_id)
    if mapped:
        return _require_http_url(mapped, f"{scenario_id} target map URL")

    source_url = _require_http_url(row["target_url"], f"{scenario_id} target_url")
    source = urllib.parse.urlsplit(source_url)
    if source.hostname not in PLACEHOLDER_HOSTS:
        return source_url
    if not fixture_base_url:
        raise ValueError(
            f"{scenario_id} 使用占位地址 {source_url}；请提供 --target-map 或 --fixture-base-url"
        )

    fixture = urllib.parse.urlsplit(_require_http_url(fixture_base_url, "fixture base URL"))
    prefix = fixture.path.rstrip("/")
    target_path = f"{prefix}/{source.path.lstrip('/')}"
    return urllib.parse.urlunsplit(
        (fixture.scheme, fixture.netloc, target_path, source.query, source.fragment)
    )


def build_task_payload(
    row: Mapping[str, str],
    *,
    target_url: str,
    batch_id: str,
) -> dict[str, Any]:
    """把验收场景转换成工作台任务请求。"""
    scenario_id = row["scenario_id"]
    business_page_types = frozenset(get_default_page_type_registry().business_codes)
    expected_page_types = _stable_unique(_csv_values(row["expected_page_types"]))
    return {
        "task_name": f"[H5验收][{batch_id}] {scenario_id} {row['scenario_name']}",
        "target_url": target_url,
        "ruleset_code": row["ruleset_code"],
        "ruleset_version": int(row["ruleset_version"]),
        "channel": "workbench",
        "priority": 100,
        "product": {
            "department_name": "自动化验收",
            "product_code": row["product_code"],
            "product_name": row["scenario_name"],
        },
        "target_platform": "h5",
        "execution_mode": "full_inspection",
        "required_page_types": [
            page_type for page_type in expected_page_types if page_type in business_page_types
        ],
        "scan_mode": "full",
        "journey_steps": _stable_unique(_csv_values(row["expected_surface_phases"])),
        "remark": (
            f"scenario_id={scenario_id}; pass={row['pass_criteria']}; risk={row['risk_notes']}"
        ),
    }


def validate_base_url(base_url: str, *, allow_non_loopback: bool) -> str:
    """默认只允许本机服务，避免误向测试或生产环境批量建任务。"""
    normalized = _require_http_url(base_url, "Alioth base URL").rstrip("/")
    hostname = (urllib.parse.urlsplit(normalized).hostname or "").casefold()
    if not allow_non_loopback and hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("非本机 Alioth 地址必须显式传入 --allow-non-loopback")
    return normalized


def validate_fixture_capabilities(
    *,
    fixture_base_url: str,
    scenario_ids: set[str],
    request: JsonRequest | None = None,
) -> dict[str, Any]:
    """验证夹具服务声明了本批次所需的页面行为场景。"""
    manifest, blocked = inspect_fixture_capabilities(
        fixture_base_url=fixture_base_url,
        scenario_ids=scenario_ids,
        request=request,
    )
    if blocked:
        details = ", ".join(
            f"{scenario_id}({driver})" for scenario_id, driver in sorted(blocked.items())
        )
        raise ValueError(f"夹具站不能执行以下场景：{details}")
    return manifest


def inspect_fixture_capabilities(
    *,
    fixture_base_url: str,
    scenario_ids: set[str],
    request: JsonRequest | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """区分夹具可执行场景和需要外部受控驱动的场景。"""
    base_url = _require_http_url(fixture_base_url, "fixture base URL").rstrip("/")
    requester = request or request_json
    manifest = requester(
        method="GET",
        url=f"{base_url}/_manifest",
        access_token=None,
    )
    if manifest.get("schema_version") != FIXTURE_MANIFEST_SCHEMA_VERSION:
        raise ValueError("H5 夹具 Manifest 版本不匹配")
    fixtures = manifest.get("fixtures")
    if not isinstance(fixtures, Mapping):
        raise ValueError("H5 夹具 Manifest 缺少 fixtures")
    missing = sorted(scenario_ids - set(fixtures))
    controlled = manifest.get("requires_controlled_driver")
    drivers = controlled if isinstance(controlled, Mapping) else {}
    unsupported = [scenario_id for scenario_id in missing if scenario_id not in drivers]
    if unsupported:
        raise ValueError("夹具站未声明以下场景能力：" + ", ".join(unsupported))
    blocked: dict[str, str] = {}
    for scenario_id in missing:
        driver = drivers.get(scenario_id)
        if not isinstance(driver, str) or not driver.strip():
            raise ValueError(f"{scenario_id} controlled driver 不能为空")
        blocked[scenario_id] = driver.strip()
    return manifest, blocked


def request_json(
    *,
    method: str,
    url: str,
    access_token: str | None,
    payload: Mapping[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """调用 Alioth JSON API，并保留服务端错误正文。"""
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw_body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} 返回 HTTP {exc.code}: {error_body[:1000]}") from exc
    try:
        value = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{method} {url} 未返回合法 JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{method} {url} 未返回 JSON object")
    return value


def run_scenario(
    row: Mapping[str, str],
    *,
    base_url: str,
    access_token: str | None,
    target_url: str,
    batch_id: str,
    timeout_seconds: float,
    projection_timeout_seconds: float,
    poll_interval_seconds: float,
    request: JsonRequest = request_json,
    sleep: Sleep = time.sleep,
    quiescence_seconds: float = 2.0,
    require_business_completion: bool = False,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """执行一个场景，返回摘要和用于审计的全部 API 快照。"""
    scenario_id = row["scenario_id"]
    payload = build_task_payload(row, target_url=target_url, batch_id=batch_id)
    created = request(
        method="POST",
        url=f"{base_url}/api/v1/workstation/tasks",
        access_token=access_token,
        payload=payload,
        idempotency_key=_idempotency_key(batch_id, scenario_id),
    )
    task_id = _required_text(created.get("task_id"), "创建响应 task_id")
    run_id = _optional_text(created.get("run_id"))
    if scenario_id == CANCELLATION_SCENARIO_ID:
        return _run_cancellation_scenario(
            row=row,
            base_url=base_url,
            access_token=access_token,
            target_url=target_url,
            batch_id=batch_id,
            task_id=task_id,
            run_id=run_id,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            quiescence_seconds=quiescence_seconds,
            request=request,
            sleep=sleep,
        )
    deadline = time.monotonic() + timeout_seconds
    controlled_driver: dict[str, Any] | None = None
    if scenario_id == TARGETED_RECAPTURE_SCENARIO_ID:
        controlled_driver = _wait_and_release_targeted_recapture_driver(
            base_url=base_url,
            task_id=task_id,
            run_id=run_id,
            target_url=target_url,
            access_token=access_token,
            deadline=deadline,
            poll_interval_seconds=poll_interval_seconds,
            request=request,
            sleep=sleep,
        )
    task: dict[str, Any]

    while True:
        task = request(
            method="GET",
            url=(f"{base_url}/api/v1/workstation/tasks/{urllib.parse.quote(task_id, safe='')}"),
            access_token=access_token,
        )
        run_id = _optional_text(task.get("run_id")) or run_id
        status = _optional_text(task.get("status"))
        if status in TERMINAL_TASK_STATUSES:
            break
        if time.monotonic() >= deadline:
            raise TimeoutError(f"{scenario_id} 等待任务终态超时：task_id={task_id} status={status}")
        sleep(poll_interval_seconds)

    if not run_id:
        raise RuntimeError(f"{scenario_id} 任务已终态但没有 agent_run_id")

    snapshots = _fetch_acceptance_snapshots_eventually(
        base_url=base_url,
        task_id=task_id,
        run_id=run_id,
        access_token=access_token,
        request=request,
        deadline=time.monotonic() + projection_timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        sleep=sleep,
    )
    if controlled_driver is not None:
        snapshots["controlled_driver"] = controlled_driver
    popup_layers = _expected_popup_layers(scenario_id)
    popup_action_traces = (
        _fetch_popup_action_trace_artifacts(
            base_url=base_url,
            report=snapshots["report"],
            access_token=access_token,
            request=request,
        )
        if popup_layers > 0
        else {}
    )
    if popup_action_traces:
        snapshots["popup_action_traces"] = popup_action_traces
    checks = verify_acceptance_payloads(
        task=snapshots["task"],
        run=snapshots["run"],
        rule_executions=snapshots["rules"],
        report=snapshots["report"],
        timeline=snapshots["timeline"],
        inspection_work=snapshots["inspection_work"],
        popup_action_traces=popup_action_traces,
        expect_popup_chain=popup_layers > 0,
        min_popup_layers=max(popup_layers, 1),
        require_business_completion=require_business_completion,
    )
    checks = _scope_scenario_checks(
        scenario_id=scenario_id,
        checks=checks,
        require_business_completion=require_business_completion,
    )
    if scenario_id != UNSTABLE_PAGE_SCENARIO_ID:
        checks.append(_check_expected_page_types(row, snapshots["timeline"]))
    if scenario_id == HIGH_RISK_ACTION_SCENARIO_ID:
        checks.extend(_check_high_risk_action_blocking(snapshots["timeline"]))
    if scenario_id == TARGETED_RECAPTURE_SCENARIO_ID:
        checks.extend(_check_targeted_recapture(snapshots))
    if scenario_id == CAPTURE_RETRY_SCENARIO_ID:
        checks.extend(_check_capture_retry(snapshots["timeline"]))
    if scenario_id == BROWSER_CRASH_SCENARIO_ID:
        checks.extend(_check_browser_crash_recovery(snapshots["timeline"]))
    if scenario_id == EVIDENCE_FAILURE_SCENARIO_ID:
        checks = [check for check in checks if check.code != "RULE_EVIDENCE"]
        checks.extend(_check_evidence_failure(snapshots))
    if scenario_id == MODEL_CONTRACT_FAILURE_SCENARIO_ID:
        checks.extend(_check_model_contract_failure(snapshots["timeline"]))
    if scenario_id == UNSTABLE_PAGE_SCENARIO_ID:
        checks = [
            check for check in checks if check.code not in {"RULE_EVIDENCE", "USAGE_COST_TRACE"}
        ]
        checks.append(_check_unstable_usage_trace(snapshots["run"]))
        checks.extend(_check_unstable_terminal(snapshots))
    passed = all(check.passed for check in checks)
    return (
        {
            "scenario_id": scenario_id,
            "scenario_name": row["scenario_name"],
            "target_url": target_url,
            "task_id": task_id,
            "agent_run_id": run_id,
            "task_status": snapshots["task"].get("status"),
            "passed": passed,
            "checks": [
                {"code": check.code, "passed": check.passed, "message": check.message}
                for check in checks
            ],
        },
        snapshots,
    )


def _scope_scenario_checks(
    *,
    scenario_id: str,
    checks: list[AcceptanceCheck],
    require_business_completion: bool,
) -> list[AcceptanceCheck]:
    """让专项场景只验自己的技术合同，严格业务验收仍保留全部门禁。"""
    if (
        scenario_id == TARGETED_RECAPTURE_SCENARIO_ID
        and not require_business_completion
    ):
        return [check for check in checks if check.code != "REPORT_READY"]
    return checks


def _run_cancellation_scenario(
    *,
    row: Mapping[str, str],
    base_url: str,
    access_token: str | None,
    target_url: str,
    batch_id: str,
    task_id: str,
    run_id: str,
    timeout_seconds: float,
    poll_interval_seconds: float,
    quiescence_seconds: float,
    request: JsonRequest,
    sleep: Sleep,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """在首批事实落库后取消任务，并验证终态后不再产生新事实。"""
    deadline = time.monotonic() + timeout_seconds
    before_cancel = _wait_for_cancellation_point(
        base_url=base_url,
        task_id=task_id,
        run_id=run_id,
        access_token=access_token,
        request=request,
        deadline=deadline,
        poll_interval_seconds=poll_interval_seconds,
        sleep=sleep,
    )
    run_id = _required_text(
        before_cancel["task"].get("run_id") or before_cancel["run"].get("run_id"),
        "取消场景 agent_run_id",
    )
    cancel_response = request(
        method="POST",
        url=(f"{base_url}/api/v1/workstation/tasks/{urllib.parse.quote(task_id, safe='')}/cancel"),
        access_token=access_token,
        idempotency_key=_idempotency_key(batch_id, f"{row['scenario_id']}-cancel"),
    )
    terminal = _wait_for_cancellation_settlement(
        base_url=base_url,
        task_id=task_id,
        run_id=run_id,
        access_token=access_token,
        request=request,
        deadline=deadline,
        poll_interval_seconds=poll_interval_seconds,
        sleep=sleep,
    )
    sleep(quiescence_seconds)
    quiescent = _fetch_cancellation_snapshots(
        base_url=base_url,
        task_id=task_id,
        run_id=run_id,
        access_token=access_token,
        request=request,
    )
    checks = _verify_cancellation_snapshots(
        before_cancel=before_cancel,
        terminal=terminal,
        quiescent=quiescent,
    )
    snapshots = {
        "before_cancel_task": before_cancel["task"],
        "before_cancel_run": before_cancel["run"],
        "before_cancel_timeline": before_cancel["timeline"],
        "cancel_response": cancel_response,
        "task": terminal["task"],
        "run": terminal["run"],
        "timeline": terminal["timeline"],
        "inspection_work": terminal["inspection_work"],
        "quiescent_task": quiescent["task"],
        "quiescent_run": quiescent["run"],
        "quiescent_timeline": quiescent["timeline"],
        "quiescent_inspection_work": quiescent["inspection_work"],
    }
    return (
        {
            "scenario_id": row["scenario_id"],
            "scenario_name": row["scenario_name"],
            "target_url": target_url,
            "task_id": task_id,
            "agent_run_id": run_id,
            "task_status": terminal["task"].get("status"),
            "passed": all(check.passed for check in checks),
            "checks": [
                {"code": check.code, "passed": check.passed, "message": check.message}
                for check in checks
            ],
        },
        snapshots,
    )


def _wait_for_cancellation_point(
    *,
    base_url: str,
    task_id: str,
    run_id: str,
    access_token: str | None,
    request: JsonRequest,
    deadline: float,
    poll_interval_seconds: float,
    sleep: Sleep,
) -> dict[str, dict[str, Any]]:
    """等待任务进入运行态且至少一项可审计事实已经落库。"""
    current_run_id = run_id
    while True:
        encoded_task = urllib.parse.quote(task_id, safe="")
        task = request(
            method="GET",
            url=f"{base_url}/api/v1/workstation/tasks/{encoded_task}",
            access_token=access_token,
        )
        current_run_id = _optional_text(task.get("run_id")) or current_run_id
        status = _optional_text(task.get("status"))
        if status in TERMINAL_TASK_STATUSES:
            raise RuntimeError(f"任务在取消前提前进入终态：status={status}")
        if current_run_id:
            encoded_run = urllib.parse.quote(current_run_id, safe="")
            run = request(
                method="GET",
                url=f"{base_url}/api/v1/agents/runs/{encoded_run}",
                access_token=access_token,
            )
            timeline = request(
                method="GET",
                url=f"{base_url}/api/v1/workstation/tasks/{encoded_task}/page-timeline",
                access_token=access_token,
            )
            counts = _runtime_fact_counts(run=run, timeline=timeline)
            session_total = _summary_count(timeline, "total_count")
            if status == "running" and session_total > 0 and any(counts.values()):
                return {"task": task, "run": run, "timeline": timeline}
        if time.monotonic() >= deadline:
            raise TimeoutError(f"等待取消触发点超时：task_id={task_id} status={status}")
        sleep(poll_interval_seconds)


def _wait_for_cancellation_settlement(
    *,
    base_url: str,
    task_id: str,
    run_id: str,
    access_token: str | None,
    request: JsonRequest,
    deadline: float,
    poll_interval_seconds: float,
    sleep: Sleep,
) -> dict[str, dict[str, Any]]:
    while True:
        snapshots = _fetch_cancellation_snapshots(
            base_url=base_url,
            task_id=task_id,
            run_id=run_id,
            access_token=access_token,
            request=request,
        )
        task_status = _optional_text(snapshots["task"].get("status"))
        run_status = _optional_text(snapshots["run"].get("status"))
        session_summary = snapshots["timeline"].get("surface_session_summary")
        session_summary = session_summary if isinstance(session_summary, Mapping) else {}
        inspection_summary = snapshots["inspection_work"].get("summary")
        inspection_summary = inspection_summary if isinstance(inspection_summary, Mapping) else {}
        runtime_terminal = task_status in TERMINAL_TASK_STATUSES and run_status not in {
            "queued",
            "running",
            "canceling",
        }
        resources_released = (
            int(session_summary.get("total_count") or 0) > 0
            and int(session_summary.get("nonterminal_count") or 0) == 0
            and int(session_summary.get("nonterminal_reference_count") or 0) == 0
        )
        inspection_settled = int(inspection_summary.get("nonterminal_count") or 0) == 0
        if runtime_terminal and resources_released and inspection_settled:
            return snapshots
        if time.monotonic() >= deadline:
            raise TimeoutError(
                "等待取消终态超时："
                f"task_id={task_id} task_status={task_status} run_status={run_status}"
            )
        sleep(poll_interval_seconds)


def _fetch_cancellation_snapshots(
    *,
    base_url: str,
    task_id: str,
    run_id: str,
    access_token: str | None,
    request: JsonRequest,
) -> dict[str, dict[str, Any]]:
    encoded_task = urllib.parse.quote(task_id, safe="")
    encoded_run = urllib.parse.quote(run_id, safe="")
    endpoints = {
        "task": f"/api/v1/workstation/tasks/{encoded_task}",
        "run": f"/api/v1/agents/runs/{encoded_run}",
        "timeline": f"/api/v1/workstation/tasks/{encoded_task}/page-timeline",
        "inspection_work": (f"/api/v1/workstation/tasks/{encoded_task}/inspection-work-items"),
    }
    return {
        name: request(
            method="GET",
            url=f"{base_url}{path}",
            access_token=access_token,
        )
        for name, path in endpoints.items()
    }


def _verify_cancellation_snapshots(
    *,
    before_cancel: Mapping[str, Mapping[str, Any]],
    terminal: Mapping[str, Mapping[str, Any]],
    quiescent: Mapping[str, Mapping[str, Any]],
) -> list[AcceptanceCheck]:
    before_counts = _runtime_fact_counts(
        run=before_cancel["run"], timeline=before_cancel["timeline"]
    )
    terminal_counts = _runtime_fact_counts(run=terminal["run"], timeline=terminal["timeline"])
    quiescent_counts = _runtime_fact_counts(run=quiescent["run"], timeline=quiescent["timeline"])
    session_summary = quiescent["timeline"].get("surface_session_summary")
    session_summary = session_summary if isinstance(session_summary, Mapping) else {}
    inspection_summary = quiescent["inspection_work"].get("summary")
    inspection_summary = inspection_summary if isinstance(inspection_summary, Mapping) else {}
    canceled = (
        terminal["task"].get("status") == "canceled"
        and terminal["run"].get("status") == "canceled"
        and terminal["run"].get("termination_reason") == "canceled"
    )
    facts_preserved = bool(any(before_counts.values())) and all(
        terminal_counts[key] >= value for key, value in before_counts.items()
    )
    quiescent_ok = terminal_counts == quiescent_counts
    resources_released = (
        int(session_summary.get("total_count") or 0) > 0
        and int(session_summary.get("nonterminal_count") or 0) == 0
        and int(session_summary.get("nonterminal_reference_count") or 0) == 0
    )
    no_pending_work = int(inspection_summary.get("nonterminal_count") or 0) == 0
    return [
        AcceptanceCheck(
            "CANCEL_TERMINAL",
            canceled,
            "任务和 Run 均以 canceled 收口" if canceled else "任务或 Run 未以 canceled 收口",
        ),
        AcceptanceCheck(
            "CANCEL_FACTS_PRESERVED",
            facts_preserved,
            f"取消前后事实计数：before={before_counts} terminal={terminal_counts}",
        ),
        AcceptanceCheck(
            "CANCEL_NO_POST_TERMINAL_GROWTH",
            quiescent_ok,
            f"终态静默复查：terminal={terminal_counts} quiescent={quiescent_counts}",
        ),
        AcceptanceCheck(
            "CANCEL_RESOURCES_RELEASED",
            resources_released,
            f"Surface 收口摘要：{dict(session_summary)}",
        ),
        AcceptanceCheck(
            "CANCEL_NO_PENDING_INSPECTION",
            no_pending_work,
            f"质检工作终态摘要：{dict(inspection_summary)}",
        ),
    ]


def _runtime_fact_counts(
    *,
    run: Mapping[str, Any],
    timeline: Mapping[str, Any],
) -> dict[str, int]:
    return {
        "step_count": int(run.get("step_count") or 0),
        "action_trace_count": len(_mapping_items(run.get("action_trace"))),
        "exploration_action_count": len(_mapping_items(timeline.get("exploration_actions"))),
        "page_node_count": len(_mapping_items(timeline.get("nodes"))),
    }


def _summary_count(timeline: Mapping[str, Any], key: str) -> int:
    summary = timeline.get("surface_session_summary")
    if not isinstance(summary, Mapping):
        return 0
    return int(summary.get(key) or 0)


def _fetch_acceptance_snapshots(
    *,
    base_url: str,
    task_id: str,
    run_id: str,
    access_token: str | None,
    request: JsonRequest,
) -> dict[str, dict[str, Any]]:
    encoded_task = urllib.parse.quote(task_id, safe="")
    encoded_run = urllib.parse.quote(run_id, safe="")
    endpoints = {
        "task": f"/api/v1/workstation/tasks/{encoded_task}",
        "run": f"/api/v1/agents/runs/{encoded_run}",
        "rules": f"/api/v1/tasks/{encoded_task}/rule-executions?agent_run_id={encoded_run}",
        "report": f"/api/v1/workstation/reports/{encoded_task}?agent_run_id={encoded_run}",
        "timeline": f"/api/v1/workstation/tasks/{encoded_task}/page-timeline",
        "inspection_work": (f"/api/v1/workstation/tasks/{encoded_task}/inspection-work-items"),
        "evidence_gaps": (f"/api/v1/workstation/tasks/{encoded_task}/evidence-gaps"),
        "closure_outcome": (f"/api/v1/workstation/tasks/{encoded_task}/closure-outcome"),
    }
    return {
        name: request(
            method="GET",
            url=f"{base_url}{path}",
            access_token=access_token,
        )
        for name, path in endpoints.items()
    }


def _fetch_acceptance_snapshots_eventually(
    *,
    base_url: str,
    task_id: str,
    run_id: str,
    access_token: str | None,
    request: JsonRequest,
    deadline: float,
    poll_interval_seconds: float,
    sleep: Sleep,
) -> dict[str, dict[str, Any]]:
    """等待终态投影可读，避免把短暂一致性延迟误报为验收失败。"""
    while True:
        try:
            return _fetch_acceptance_snapshots(
                base_url=base_url,
                task_id=task_id,
                run_id=run_id,
                access_token=access_token,
                request=request,
            )
        except (RuntimeError, urllib.error.URLError):
            if time.monotonic() >= deadline:
                raise
            sleep(poll_interval_seconds)


def _fetch_popup_action_trace_artifacts(
    *,
    base_url: str,
    report: Mapping[str, Any],
    access_token: str | None,
    request: JsonRequest,
) -> dict[str, Any]:
    """通过受控证据接口回读弹窗结果 Capture 的 JSON action_trace。"""
    page_exploration = report.get("page_exploration")
    if not isinstance(page_exploration, Mapping):
        return {}
    page_results = {
        _optional_text(item.get("page_capture_id")): item
        for item in _mapping_items(page_exploration.get("page_results"))
        if _optional_text(item.get("page_capture_id"))
    }
    result_capture_ids = {
        _optional_text(item.get("result_page_capture_id"))
        for item in _mapping_items(page_exploration.get("action_results"))
        if isinstance(item.get("parameters"), Mapping)
        and item["parameters"].get("target_mode") == "popup_dismissal"
        and _optional_text(item.get("result_page_capture_id"))
    }
    artifact_ids = {
        artifact_id
        for capture_id in result_capture_ids
        for artifact_id in _text_items(page_results.get(capture_id, {}).get("artifact_ids"))
    }
    traces: dict[str, Any] = {}
    for artifact_id in sorted(artifact_ids):
        encoded_artifact_id = urllib.parse.quote(artifact_id, safe="")
        ticket = request(
            method="POST",
            url=f"{base_url}/api/v1/evidence/{encoded_artifact_id}/ticket",
            access_token=access_token,
        )
        ticket_url = _required_text(ticket.get("url"), "证据下载票据 URL")
        query = urllib.parse.urlsplit(ticket_url).query
        file_url = f"{base_url}/api/v1/evidence/{encoded_artifact_id}/file"
        if query:
            file_url = f"{file_url}?{query}"
        response = urllib.request.urlopen(file_url, timeout=30)
        try:
            if response.headers.get_content_type() != "application/json":
                continue
            payload = json.loads(response.read().decode("utf-8"))
        finally:
            response.close()
        if isinstance(payload, list):
            traces[artifact_id] = payload
    return traces


def _check_expected_page_types(
    row: Mapping[str, str], timeline: Mapping[str, Any]
) -> AcceptanceCheck:
    expected = set(_csv_values(row["expected_page_types"]))
    business_page_types = frozenset(get_default_page_type_registry().business_codes)
    expected_business = expected & business_page_types
    expected_lifecycle = expected - business_page_types
    observed = {
        value
        for item in _mapping_items(timeline.get("nodes"))
        if (value := _optional_text(item.get("page_type")))
    }
    observed.update(
        value
        for item in _mapping_items(timeline.get("h5_assessments"))
        if (value := _optional_text(item.get("page_type")))
    )
    coverage = timeline.get("h5_coverage")
    covered = (
        _text_items(coverage.get("covered_page_types")) if isinstance(coverage, Mapping) else set()
    )
    missing_business = sorted(expected_business - covered)
    missing_lifecycle = sorted(expected_lifecycle - observed)
    if missing_business or missing_lifecycle:
        details = []
        if missing_business:
            details.append("未形成可信覆盖=" + ", ".join(missing_business))
        if missing_lifecycle:
            details.append("未观察到生命周期页=" + ", ".join(missing_lifecycle))
        return AcceptanceCheck(
            "EXPECTED_PAGE_TYPES",
            False,
            "预期页面类型未达标："
            + "；".join(details)
            + f"；covered={sorted(covered)}；observed={sorted(observed)}",
        )
    return AcceptanceCheck(
        "EXPECTED_PAGE_TYPES",
        True,
        "业务页已形成可信覆盖，生命周期页已实际观察："
        f"business={sorted(expected_business)}；lifecycle={sorted(expected_lifecycle)}",
    )


def _check_capture_retry(timeline: Mapping[str, Any]) -> list[AcceptanceCheck]:
    """验证首次导航失败只触发一次有界重试，并最终落下真实页面证据。"""
    audited_nodes = [
        item
        for item in _mapping_items(timeline.get("nodes"))
        if isinstance(item.get("navigation_retry"), Mapping)
        and item["navigation_retry"].get("schema_version") == "navigation_retry.v1"
    ]
    if len(audited_nodes) != 1:
        return [
            AcceptanceCheck(
                "CAPTURE_RETRY_AUDIT",
                False,
                f"预期 1 条导航重试审计，实际 {len(audited_nodes)} 条",
            )
        ]

    node = audited_nodes[0]
    audit = node["navigation_retry"]
    bounded = (
        audit.get("schema_version") == "navigation_retry.v1"
        and audit.get("attempt_count") == 2
        and audit.get("retry_count") == 1
        and audit.get("max_attempts") == 2
    )
    recovered = (
        audit.get("outcome") == "succeeded_after_retry"
        and bool(_optional_text(node.get("page_capture_id")))
        and bool(_optional_text(node.get("screenshot_artifact_id")))
    )
    return [
        AcceptanceCheck(
            "CAPTURE_RETRY_AUDIT",
            bounded,
            (
                "首次失败后仅重试一次，尝试次数已持久化"
                if bounded
                else f"重试审计不符合 2 次尝试上限：{dict(audit)}"
            ),
        ),
        AcceptanceCheck(
            "CAPTURE_RETRY_RECOVERED_CAPTURE",
            recovered,
            (
                "第二次导航成功且最终 Capture 绑定截图证据"
                if recovered
                else "重试后未形成可追溯的成功 Capture 与截图证据"
            ),
        ),
    ]


def _check_browser_crash_recovery(
    timeline: Mapping[str, Any],
) -> list[AcceptanceCheck]:
    """验证浏览器崩溃恢复有界、留痕且恢复 Capture 可复核。"""
    audit = timeline.get("browser_recovery")
    if not isinstance(audit, Mapping):
        return [
            AcceptanceCheck(
                "BROWSER_RECOVERY_AUDIT",
                False,
                "任务时间线缺少浏览器恢复审计",
            )
        ]
    attempts = _mapping_items(audit.get("attempts"))
    recoveries = _mapping_items(audit.get("recoveries"))
    bounded = bool(attempts) and all(
        1 <= int(item.get("recovery_attempt_count") or 0) <= 2
        and item.get("status") == "completed"
        and _optional_text(item.get("error_code"))
        for item in attempts
    )
    successful = [
        item
        for item in recoveries
        if item.get("equivalent") is True
        and item.get("reason_code") is None
        and _optional_text(item.get("source_session_id"))
        != _optional_text(item.get("target_session_id"))
    ]
    node_ids = {
        value
        for node in _mapping_items(timeline.get("nodes"))
        if (value := _optional_text(node.get("page_capture_id")))
        and _optional_text(node.get("screenshot_artifact_id"))
    }
    recovered_capture = any(
        _optional_text(item.get("rebaseline_after_capture_id")) in node_ids for item in successful
    )
    action_ids = {
        value
        for action in _mapping_items(timeline.get("exploration_actions"))
        if (value := _optional_text(action.get("action_execution_id")))
    }
    trace_preserved = bool(successful) and all(
        _optional_text(item.get("original_source_capture_id")) in node_ids
        and _optional_text(item.get("restoration_action_execution_id")) in action_ids
        and (
            item.get("failure_stage") == "before_capture"
            or _optional_text(item.get("failed_action_execution_id")) in action_ids
        )
        for item in successful
    )
    return [
        AcceptanceCheck(
            "BROWSER_RECOVERY_AUDIT",
            bounded,
            (
                "浏览器崩溃恢复 attempt 已持久化且未超过两次"
                if bounded
                else f"恢复 attempt 缺失、未完成或超出上限：{attempts}"
            ),
        ),
        AcceptanceCheck(
            "BROWSER_RECOVERY_SESSION_LINEAGE",
            bool(successful),
            (
                "恢复使用独立 Session，且状态等价校验通过"
                if successful
                else "未找到通过状态等价校验的独立恢复 Session"
            ),
        ),
        AcceptanceCheck(
            "BROWSER_RECOVERY_CAPTURE",
            recovered_capture,
            (
                "恢复后的 Capture 已绑定截图证据"
                if recovered_capture
                else "恢复后的 Capture 未出现在任务截图证据中"
            ),
        ),
        AcceptanceCheck(
            "BROWSER_RECOVERY_TRACE_PRESERVED",
            trace_preserved,
            (
                "崩溃前 Capture/Action 与恢复导航 Action 均保留"
                if trace_preserved
                else "浏览器恢复覆盖或遗漏了崩溃前后的动作链路"
            ),
        ),
    ]


def _check_evidence_failure(
    snapshots: Mapping[str, Mapping[str, Any]],
) -> list[AcceptanceCheck]:
    """验证缺失证据被预检阻断，且报告与 Completion Gate 使用同一结论。"""
    timeline = snapshots["timeline"]
    audit = timeline.get("h5_evidence_preflight")
    if not isinstance(audit, Mapping):
        return [
            AcceptanceCheck(
                "EVIDENCE_PREFLIGHT",
                False,
                "任务时间线缺少 H5 证据预检结论",
            )
        ]
    preflight_valid = (
        audit.get("schema_version") == "h5-evidence-preflight.v1"
        and audit.get("status") == "evidence_insufficient"
        and int(audit.get("blocked_item_count") or 0) > 0
        and bool(audit.get("reason_codes"))
    )
    publication_blocked = (
        audit.get("invalid_evidence_publication_blocked") is True
        and audit.get("publication_allowed") is False
        and not audit.get("improperly_published_inventory_item_ids")
        and not audit.get("improper_rule_result_ids")
    )
    gate_blocked = audit.get("gate_passed") is False and bool(audit.get("gate_block_reasons"))
    report = snapshots["report"]
    page_exploration = report.get("page_exploration")
    page_exploration = page_exploration if isinstance(page_exploration, Mapping) else {}
    report_preflight = page_exploration.get("evidence_preflight")
    report_preflight = report_preflight if isinstance(report_preflight, Mapping) else {}
    report_visible = (
        page_exploration.get("complete") is False
        and page_exploration.get("requires_review") is True
        and page_exploration.get("terminal_reason") == "evidence_insufficient"
        and report_preflight.get("status") == "evidence_insufficient"
        and report_preflight.get("reason_codes") == audit.get("reason_codes")
    )
    return [
        AcceptanceCheck(
            "EVIDENCE_PREFLIGHT",
            preflight_valid,
            (
                "缺失证据已形成带原因的正式预检结论"
                if preflight_valid
                else f"证据预检结论不完整：{dict(audit)}"
            ),
        ),
        AcceptanceCheck(
            "INVALID_EVIDENCE_NOT_PUBLISHED",
            publication_blocked,
            (
                "缺失证据未发布正常规则结果"
                if publication_blocked
                else "缺失证据产生了 Publication 或正常规则结果"
            ),
        ),
        AcceptanceCheck(
            "EVIDENCE_GATE_BLOCKED",
            gate_blocked,
            (
                "Completion Gate 已明确阻断缺证任务"
                if gate_blocked
                else "Completion Gate 未留下明确的缺证阻断原因"
            ),
        ),
        AcceptanceCheck(
            "EVIDENCE_REPORT_VISIBLE",
            report_visible,
            (
                "最终报告显示证据不足及具体原因"
                if report_visible
                else "最终报告未使用任务时间线中的证据不足结论"
            ),
        ),
    ]


def _check_model_contract_failure(
    timeline: Mapping[str, Any],
) -> list[AcceptanceCheck]:
    """验证模型合同错误已安全降级，且降级动作形成结果证据。"""
    audit = timeline.get("h5_model_contract")
    if not isinstance(audit, Mapping):
        return [
            AcceptanceCheck(
                "MODEL_CONTRACT_AUDIT",
                False,
                "任务时间线缺少模型合同失败审计",
            )
        ]
    fallback_actions = _mapping_items(audit.get("fallback_actions"))
    audited = (
        audit.get("schema_version") == "h5-model-contract-audit.v1"
        and audit.get("status") == "degraded"
        and int(audit.get("fallback_action_count") or 0) == len(fallback_actions)
        and bool(fallback_actions)
        and int(audit.get("error_work_count") or 0) == 0
    )
    reason_codes = {
        value for item in fallback_actions if (value := _optional_text(item.get("reason_code")))
    }
    reason_preserved = (
        "H5_EXPLORER_OUTPUT_INVALID" in reason_codes
        and set(audit.get("reason_codes") or []) >= reason_codes
    )
    capture_artifacts = {
        capture_id: _optional_text(node.get("screenshot_artifact_id"))
        for node in _mapping_items(timeline.get("nodes"))
        if (capture_id := _optional_text(node.get("page_capture_id")))
    }
    fallback_evidenced = bool(fallback_actions) and all(
        item.get("execution_status") == "completed"
        and bool(capture_artifacts.get(_optional_text(item.get("result_page_capture_id")) or ""))
        for item in fallback_actions
    )
    return [
        AcceptanceCheck(
            "MODEL_CONTRACT_AUDIT",
            audited,
            (
                "模型合同错误已进入确定性降级动作"
                if audited
                else f"模型合同降级审计不完整：{dict(audit)}"
            ),
        ),
        AcceptanceCheck(
            "MODEL_CONTRACT_REASON_PRESERVED",
            reason_preserved,
            (
                "结构化输出错误码已随降级动作持久化"
                if reason_preserved
                else "降级动作未保留模型结构化输出错误码"
            ),
        ),
        AcceptanceCheck(
            "MODEL_CONTRACT_FALLBACK_EVIDENCE",
            fallback_evidenced,
            (
                "降级动作已完成且结果 Capture 绑定截图证据"
                if fallback_evidenced
                else "降级动作未完成或缺少结果截图证据"
            ),
        ),
    ]


def _check_unstable_terminal(
    snapshots: Mapping[str, Mapping[str, Any]],
) -> list[AcceptanceCheck]:
    """验证持续不稳定页面按证据不足收口，而不是伪造完成和规则结果。"""
    task = snapshots["task"]
    run = snapshots["run"]
    report = snapshots["report"]
    timeline = snapshots["timeline"]
    page_exploration = report.get("page_exploration")
    page_exploration = page_exploration if isinstance(page_exploration, Mapping) else {}
    timeline_summary = timeline.get("exploration_summary")
    timeline_summary = timeline_summary if isinstance(timeline_summary, Mapping) else {}
    report_reason = _optional_text(page_exploration.get("terminal_reason"))
    timeline_reason = _optional_text(timeline.get("terminal_reason"))
    terminal_aligned = (
        task.get("status") == "review"
        and run.get("status") == "failed"
        and run.get("termination_reason") == "incomplete"
        and page_exploration.get("complete") is False
        and page_exploration.get("quality_status") == "needs_review"
        and timeline_summary.get("complete") is False
        and timeline_summary.get("quality_status") == "needs_review"
        and report_reason is not None
        and report_reason == timeline_reason
    )

    blocked_captures = []
    for node in _mapping_items(timeline.get("nodes")):
        diagnostics = node.get("page_settle_diagnostics")
        if not isinstance(diagnostics, Mapping):
            continue
        if (
            diagnostics.get("outcome") == "blocked"
            and int(diagnostics.get("sample_count") or 0) > 0
            and bool(diagnostics.get("blocking_reasons"))
            and _optional_text(node.get("screenshot_artifact_id"))
        ):
            blocked_captures.append(node)
    rules_payload = snapshots["rules"]
    raw_rules = rules_payload.get("items", rules_payload.get("rule_executions", []))
    rules = _mapping_items(raw_rules)
    normal_results = [
        item
        for item in rules
        if _optional_text(item.get("execution_status"))
        in {"pass", "fail", "warning", "manual_review"}
    ]
    return [
        AcceptanceCheck(
            "UNSTABLE_TERMINAL_ALIGNMENT",
            terminal_aligned,
            (
                "持续不稳定页面已按 failed/incomplete 映射为 review，报告与时间线一致"
                if terminal_aligned
                else "任务、Run、报告和时间线的不稳定终态不一致"
            ),
        ),
        AcceptanceCheck(
            "UNSTABLE_BUDGET_AUDIT",
            bool(blocked_captures),
            (
                "稳定等待已耗尽，阻断原因和截图证据可追溯"
                if blocked_captures
                else "缺少带采样次数、阻断原因和截图的稳定等待审计"
            ),
        ),
        AcceptanceCheck(
            "UNSTABLE_NO_FAKE_RULE_RESULT",
            not normal_results,
            (
                "不稳定页面未生成正常规则结果"
                if not normal_results
                else "不稳定页面生成了无可信基线的正常规则结果"
            ),
        ),
    ]


def _check_unstable_usage_trace(run: Mapping[str, Any]) -> AcceptanceCheck:
    """持续不稳定页在进入模型前被阻断时，不强制伪造 token 或成本。"""
    trace = _mapping_items(run.get("action_trace"))
    deterministic_block = bool(trace) and all(
        item.get("tool_name") == "h5_exploration_work" and item.get("status") == "skipped"
        for item in trace
    )
    usage = run.get("usage")
    usage = usage if isinstance(usage, Mapping) else {}
    has_billing_values = any(
        key in usage for key in ("total_tokens", "input_tokens", "output_tokens", "total_cost")
    )
    passed = deterministic_block and not has_billing_values
    return AcceptanceCheck(
        "UNSTABLE_PRE_LLM_BLOCK",
        passed,
        (
            "不稳定页面在模型调用前被确定性阻断，无需生成虚假 token 或成本"
            if passed
            else "不稳定页面不是纯确定性阻断，必须保留真实模型用量"
        ),
    )


def _check_high_risk_action_blocking(
    timeline: Mapping[str, Any],
) -> list[AcceptanceCheck]:
    """确认高风险入口已被识别，且没有进入实际动作执行链。"""
    expected_flags = {
        "high_risk_payment_entry",
        "high_risk_publish_entry",
        "high_risk_upload_entry",
    }
    observed_flags: set[str] = set()
    for assessment in _mapping_items(timeline.get("h5_assessments")):
        detection = assessment.get("detection_result")
        if not isinstance(detection, Mapping):
            continue
        observed_flags.update(_text_items(detection.get("risk_flags")))

    risky_markers = ("payment", "post_publish", "upload")
    unsafe_actions = []
    for action in _mapping_items(timeline.get("exploration_actions")):
        if action.get("execution_status") != "completed":
            continue
        if action.get("action") == "navigate" and not action.get("requested_source_capture_id"):
            continue
        serialized = json.dumps(action, ensure_ascii=False).casefold()
        if any(marker in serialized for marker in risky_markers):
            unsafe_actions.append(str(action.get("exploration_action_id") or "<unknown>"))

    return [
        AcceptanceCheck(
            "HIGH_RISK_ENTRIES_RECOGNIZED",
            expected_flags <= observed_flags,
            (
                "支付、发布和上传入口均已识别为高风险"
                if expected_flags <= observed_flags
                else "高风险入口识别不完整：" + ", ".join(sorted(expected_flags - observed_flags))
            ),
        ),
        AcceptanceCheck(
            "HIGH_RISK_ACTIONS_BLOCKED",
            not unsafe_actions,
            (
                "高风险入口未进入动作执行链"
                if not unsafe_actions
                else "发现已执行的高风险动作：" + ", ".join(unsafe_actions[:5])
            ),
        ),
    ]


def _check_targeted_recapture(
    snapshots: Mapping[str, Mapping[str, Any]],
) -> list[AcceptanceCheck]:
    """W46-H5-017 必须证明请求、Probe Capture 和终态三者一致。"""
    evidence_gaps = snapshots.get("evidence_gaps") or {}
    closure = snapshots.get("closure_outcome") or {}
    timeline = snapshots.get("timeline") or {}
    items = _mapping_items(evidence_gaps.get("items"))
    satisfied_routes = [
        item
        for item in items
        if isinstance(item.get("route_plan"), Mapping)
        and item["route_plan"].get("request_status") == "satisfied"
    ]
    probe_nodes = [
        item
        for item in _mapping_items(timeline.get("nodes"))
        if item.get("capture_role") == "probe_result_replay"
    ]
    request_counts = closure.get("supplement_request_counts")
    request_counts = request_counts if isinstance(request_counts, Mapping) else {}
    open_count = sum(
        int(request_counts.get(status) or 0) for status in ("pending", "routed", "running")
    )
    satisfied_count = int(request_counts.get("satisfied") or 0)
    return [
        AcceptanceCheck(
            "TARGETED_RECAPTURE_REQUEST",
            bool(satisfied_routes),
            (
                "Evidence Request 已规划并满足"
                if satisfied_routes
                else "没有找到已满足的定向补采请求"
            ),
        ),
        AcceptanceCheck(
            "TARGETED_RECAPTURE_PROBE_CAPTURE",
            bool(probe_nodes),
            (
                "已产生 probe_result_replay Capture"
                if probe_nodes
                else "没有找到 Probe 结果 Capture"
            ),
        ),
        AcceptanceCheck(
            "TARGETED_RECAPTURE_SETTLED",
            satisfied_count > 0 and open_count == 0,
            f"补采请求状态分布：{dict(request_counts)}",
        ),
    ]


def _bind_fixture_attempt_key(
    *,
    scenario_id: str,
    target_url: str,
    batch_id: str,
    fixture_managed: bool,
) -> str:
    """为有状态夹具绑定批次身份，避免历史请求污染本次结果。"""
    if scenario_id not in {
        CAPTURE_RETRY_SCENARIO_ID,
        TARGETED_RECAPTURE_SCENARIO_ID,
    } or not fixture_managed:
        return target_url
    parsed = urllib.parse.urlsplit(target_url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query.append(("fixture_run", batch_id))
    return urllib.parse.urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urllib.parse.urlencode(query),
            parsed.fragment,
        )
    )


def _wait_and_release_targeted_recapture_driver(
    *,
    base_url: str,
    task_id: str,
    run_id: str | None,
    target_url: str,
    access_token: str | None,
    deadline: float,
    poll_interval_seconds: float,
    request: JsonRequest,
    sleep: Sleep,
    monotonic: Monotonic = time.monotonic,
) -> dict[str, Any]:
    """只在正式定向交互请求已经持久化并完成选路后开放夹具关闭控件。"""
    target = urllib.parse.urlsplit(target_url)
    fixture_run = (urllib.parse.parse_qs(target.query).get("fixture_run") or [""])[0]
    if target.path != "/h5/recapture" or not fixture_run:
        raise RuntimeError("W46-H5-017 必须使用带 fixture_run 的受控补采夹具")
    encoded_task = urllib.parse.quote(task_id, safe="")
    current_run_id = run_id
    while True:
        gaps = request(
            method="GET",
            url=f"{base_url}/api/v1/workstation/tasks/{encoded_task}/evidence-gaps",
            access_token=access_token,
        )
        if any(
            _is_routed_targeted_recapture_gap(item)
            for item in _mapping_items(gaps.get("items"))
        ):
            query = urllib.parse.urlencode({"fixture_run": fixture_run})
            released = request(
                method="POST",
                url=urllib.parse.urlunsplit(
                    (
                        target.scheme,
                        target.netloc,
                        "/_drivers/targeted-recapture/release",
                        query,
                        "",
                    )
                ),
                access_token=None,
                payload={"scenario_id": TARGETED_RECAPTURE_SCENARIO_ID},
            )
            if released.get("released") is not True:
                raise RuntimeError("受控补采夹具未确认关闭控件已开放")
            return released
        task = request(
            method="GET",
            url=f"{base_url}/api/v1/workstation/tasks/{encoded_task}",
            access_token=access_token,
        )
        current_run_id = _optional_text(task.get("run_id")) or current_run_id
        task_status = _optional_text(task.get("status"))
        if task_status in TERMINAL_TASK_STATUSES:
            raise RuntimeError(
                "W46-H5-017 在 Probe 选路前已进入任务终态："
                f"task_id={task_id} status={task_status}"
            )
        if current_run_id:
            encoded_run = urllib.parse.quote(current_run_id, safe="")
            run = request(
                method="GET",
                url=f"{base_url}/api/v1/agents/runs/{encoded_run}",
                access_token=access_token,
            )
            run_status = _optional_text(run.get("status"))
            if run_status and run_status not in {"queued", "running", "canceling"}:
                error = _optional_text(run.get("error_message"))
                detail = f" error={error}" if error else ""
                raise RuntimeError(
                    "W46-H5-017 在 Probe 选路前运行已终止："
                    f"run_id={current_run_id} status={run_status}{detail}"
                )
        if monotonic() >= deadline:
            raise TimeoutError("W46-H5-017 未产生已选路的 active_session 定向交互请求")
        sleep(poll_interval_seconds)


def _is_routed_targeted_recapture_gap(item: Mapping[str, Any]) -> bool:
    gap = item.get("gap")
    gap = gap if isinstance(gap, Mapping) else {}
    requested = gap.get("requested_request")
    requested = requested if isinstance(requested, Mapping) else {}
    route = item.get("route_plan")
    route = route if isinstance(route, Mapping) else {}
    return (
        item.get("rule_code") == "alioth_12"
        and gap.get("overlay_phase") == "after_close"
        and requested.get("session_dependency") == "active_session"
        and route.get("request_status") in {"routed", "running", "satisfied"}
    )


def write_scenario_snapshots(
    output_dir: Path,
    *,
    scenario_id: str,
    snapshots: Mapping[str, Mapping[str, Any]],
) -> dict[str, str]:
    """按场景保存原始快照，返回批次报告中的相对路径。"""
    scenario_dir = output_dir / scenario_id
    scenario_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}
    for name, payload in snapshots.items():
        path = scenario_dir / f"{name}.json"
        _write_json_atomic(path, payload)
        paths[name] = path.relative_to(output_dir).as_posix()
    return paths


def _write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _resolve_access_token(args: argparse.Namespace, *, base_url: str) -> str | None:
    token = _optional_text(args.access_token)
    if token:
        return token
    if not args.username and not args.password_env:
        return None
    if not args.username or not args.password_env:
        raise ValueError("--username 和 --password-env 必须同时提供")
    password = os.getenv(args.password_env)
    if not password:
        raise ValueError(f"环境变量 {args.password_env} 未设置或为空")
    return login_for_access_token(
        base_url=base_url,
        username=args.username,
        password=password,
    )


def _expected_popup_layers(scenario_id: str) -> int:
    if scenario_id in {"W46-H5-005", "W46-H5-011"}:
        return 2
    if scenario_id == "W46-H5-004":
        return 1
    return 0


def _mapping_items(value: object) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _csv_values(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _stable_unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _text_items(value: object) -> set[str]:
    if not isinstance(value, list | tuple | set | frozenset):
        return set()
    return {item.strip() for item in value if isinstance(item, str) and item.strip()}


def _idempotency_key(batch_id: str, scenario_id: str) -> str:
    digest = hashlib.sha256(f"{batch_id}:{scenario_id}".encode()).hexdigest()[:24]
    return f"h5-acceptance-{digest}"


def _required_text(value: object, label: str) -> str:
    text = _optional_text(value)
    if not text:
        raise RuntimeError(f"{label} 为空")
    return text


def _optional_text(value: object) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _require_http_url(value: str, label: str) -> str:
    normalized = value.strip()
    parsed = urllib.parse.urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{label} 必须是完整 HTTP(S) URL：{value!r}")
    return normalized


def _default_batch_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _validate_batch_id(value: str) -> str:
    normalized = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", normalized):
        raise ValueError("batch-id 只能包含字母、数字、点、下划线和连字符，最长64位")
    return normalized


def _print_plan(
    rows: list[dict[str, str]],
    targets: Mapping[str, str],
    *,
    blocked_drivers: Mapping[str, str] | None = None,
) -> None:
    print(f"H5 acceptance plan: {len(rows)} scenarios")
    blocked = blocked_drivers or {}
    for row in rows:
        scenario_id = row["scenario_id"]
        if scenario_id in blocked:
            print(f"- {scenario_id} {row['scenario_name']}: BLOCKED ({blocked[scenario_id]})")
            continue
        print(f"- {scenario_id} {row['scenario_name']}: {targets[scenario_id]}")


def build_controlled_driver_block_result(
    row: Mapping[str, str],
    *,
    target_url: str,
    required_driver: str,
) -> dict[str, Any]:
    """记录外部受控驱动缺失，不创建业务任务冒充验收。"""
    return {
        "scenario_id": row["scenario_id"],
        "scenario_name": row["scenario_name"],
        "target_url": target_url,
        "acceptance_status": "blocked",
        "passed": False,
        "error_code": "CONTROLLED_DRIVER_REQUIRED",
        "required_driver": required_driver,
        "message": "缺少外部受控驱动，本场景未创建验收任务",
    }


def should_abort_batch(result: Mapping[str, Any]) -> bool:
    """运行时错误或系统门禁失败后停止继续创建任务。"""

    if result.get("passed") is True:
        return False
    scenario_id = _optional_text(result.get("scenario_id"))
    return bool(result.get("error")) or scenario_id in SYSTEMIC_GATE_SCENARIO_IDS


def build_batch_abort_result(
    row: Mapping[str, str],
    *,
    target_url: str,
    blocker: Mapping[str, Any],
) -> dict[str, Any]:
    """记录系统性失败后的未执行场景，不再创建业务任务。"""

    blocker_id = _required_text(blocker.get("scenario_id"), "阻塞场景编号")
    blocker_error = _optional_text(blocker.get("error"))
    message = f"前置场景 {blocker_id} 发生系统性失败，本场景未创建验收任务"
    if blocker_error:
        message = f"{message}：{blocker_error}"
    return {
        "scenario_id": row["scenario_id"],
        "scenario_name": row["scenario_name"],
        "target_url": target_url,
        "acceptance_status": "not_executed",
        "passed": False,
        "error_code": "BATCH_ABORTED_AFTER_SYSTEMIC_FAILURE",
        "blocked_by_scenario_id": blocker_id,
        "message": message,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="Alioth API 地址")
    parser.add_argument("--scenario-file", default=str(DEFAULT_SCENARIO_PATH))
    parser.add_argument("--scenario-id", action="append", default=[])
    parser.add_argument("--priority", action="append", default=[])
    parser.add_argument("--category", action="append", default=[])
    parser.add_argument("--target-map", help="场景编号到真实 URL 的 JSON 文件")
    parser.add_argument("--fixture-base-url", help="替换 example.test 的夹具服务根地址")
    parser.add_argument(
        "--fixture-target-base-url",
        help="Worker/浏览器访问夹具的地址；默认与 --fixture-base-url 相同",
    )
    parser.add_argument("--batch-id", default=_default_batch_id())
    parser.add_argument("--output-dir", default="outputs/h5-acceptance")
    parser.add_argument("--timeout-seconds", type=float, default=3600)
    parser.add_argument("--projection-timeout-seconds", type=float, default=120)
    parser.add_argument("--poll-interval-seconds", type=float, default=5)
    parser.add_argument("--access-token")
    parser.add_argument("--username")
    parser.add_argument("--password-env")
    parser.add_argument("--allow-non-loopback", action="store_true")
    parser.add_argument(
        "--require-business-completion",
        action="store_true",
        help="要求任务、Completion Gate 与正式报告全部完成；真实业务验收必须开启",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    try:
        base_url = validate_base_url(
            args.base_url,
            allow_non_loopback=args.allow_non_loopback,
        )
        args.batch_id = _validate_batch_id(args.batch_id)
        if args.timeout_seconds <= 0 or args.projection_timeout_seconds <= 0:
            raise ValueError("任务和投影超时时间必须大于 0")
        if args.poll_interval_seconds < 0:
            raise ValueError("轮询间隔不能小于 0")
        rows = load_scenarios(args.scenario_file)
        issues = validate_scenarios(rows)
        if issues:
            raise ValueError(
                "验收场景清单不合法：" + "; ".join(f"{item.code}={item.message}" for item in issues)
            )
        selected = select_scenarios(
            rows,
            scenario_ids=set(args.scenario_id),
            priorities=set(args.priority),
            categories=set(args.category),
        )
        if not selected:
            raise ValueError("过滤后没有可执行场景")
        target_map = load_target_map(args.target_map)
        fixture_target_base_url = args.fixture_target_base_url or args.fixture_base_url
        targets = {}
        for row in selected:
            scenario_id = row["scenario_id"]
            target_url = resolve_target_url(
                row,
                target_map=target_map,
                fixture_base_url=fixture_target_base_url,
            )
            targets[scenario_id] = _bind_fixture_attempt_key(
                scenario_id=scenario_id,
                target_url=target_url,
                batch_id=args.batch_id,
                fixture_managed=scenario_id not in target_map,
            )
        fixture_scenario_ids = {
            row["scenario_id"]
            for row in selected
            if row["scenario_id"] not in target_map
            and urllib.parse.urlsplit(row["target_url"]).hostname in PLACEHOLDER_HOSTS
        }
        blocked_drivers: dict[str, str] = {}
        if fixture_scenario_ids and not args.dry_run:
            _, blocked_drivers = inspect_fixture_capabilities(
                fixture_base_url=args.fixture_base_url,
                scenario_ids=fixture_scenario_ids,
            )
        _print_plan(selected, targets, blocked_drivers=blocked_drivers)
        if args.dry_run:
            return 0
        access_token = _resolve_access_token(args, base_url=base_url)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"准备验收失败：{exc}", file=sys.stderr)
        return 2

    output_dir = Path(args.output_dir) / args.batch_id
    batch: dict[str, Any] = {
        "schema_version": "h5-acceptance-batch.v1",
        "batch_id": args.batch_id,
        "base_url": base_url,
        "started_at": datetime.now(UTC).isoformat(),
        "finished_at": None,
        "results": [],
    }
    summary_path = output_dir / "batch.json"
    _write_json_atomic(summary_path, batch)

    blocker: dict[str, Any] | None = None
    for row in selected:
        scenario_id = row["scenario_id"]
        print(f"\n=== {scenario_id} {row['scenario_name']} ===", flush=True)
        if blocker is not None:
            result = build_batch_abort_result(
                row,
                target_url=targets[scenario_id],
                blocker=blocker,
            )
            batch["results"].append(result)
            _write_json_atomic(summary_path, batch)
            print("NOT EXECUTED", flush=True)
            continue
        required_driver = blocked_drivers.get(scenario_id)
        if required_driver:
            result = build_controlled_driver_block_result(
                row,
                target_url=targets[scenario_id],
                required_driver=required_driver,
            )
            batch["results"].append(result)
            _write_json_atomic(summary_path, batch)
            print("BLOCKED", flush=True)
            continue
        try:
            result, snapshots = run_scenario(
                row,
                base_url=base_url,
                access_token=access_token,
                target_url=targets[scenario_id],
                batch_id=args.batch_id,
                timeout_seconds=args.timeout_seconds,
                projection_timeout_seconds=args.projection_timeout_seconds,
                poll_interval_seconds=args.poll_interval_seconds,
                require_business_completion=args.require_business_completion,
            )
            result["snapshot_files"] = write_scenario_snapshots(
                output_dir,
                scenario_id=scenario_id,
                snapshots=snapshots,
            )
        except (OSError, RuntimeError, TimeoutError, urllib.error.URLError) as exc:
            result = {
                "scenario_id": scenario_id,
                "scenario_name": row["scenario_name"],
                "target_url": targets[scenario_id],
                "passed": False,
                "error": str(exc),
            }
        batch["results"].append(result)
        _write_json_atomic(summary_path, batch)
        print("PASS" if result["passed"] else "FAIL", flush=True)
        if should_abort_batch(result):
            blocker = result
            print(
                "后续场景停止创建：当前失败属于运行时错误或系统门禁失败。",
                flush=True,
            )

    batch["finished_at"] = datetime.now(UTC).isoformat()
    batch["passed"] = all(item["passed"] for item in batch["results"])
    batch["passed_count"] = sum(1 for item in batch["results"] if item["passed"])
    batch["blocked_count"] = sum(
        1
        for item in batch["results"]
        if item.get("acceptance_status") in {"blocked", "not_executed"}
    )
    batch["total_count"] = len(batch["results"])
    batch["executed_count"] = batch["total_count"] - batch["blocked_count"]
    batch["failed_count"] = batch["executed_count"] - batch["passed_count"]
    _write_json_atomic(summary_path, batch)
    print(
        f"\nH5 acceptance result: {batch['passed_count']} passed, "
        f"{batch['failed_count']} failed, {batch['blocked_count']} blocked"
    )
    print(f"result={summary_path}")
    return 0 if batch["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
