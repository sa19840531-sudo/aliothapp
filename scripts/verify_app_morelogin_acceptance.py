#!/usr/bin/env python3
"""只读核对 MoreLogin App 真实验收清单与 Publication Entry 对账。

本脚本不发起云机动作、不注入故障、不把「平台技术闭环」升级为「APP 质量通过」。
``platform_tech_passed`` 仅表示清单内可审计平台事实齐备；``app_quality_passed``
必须单独由规则/覆盖门禁判定，缺省为未评估。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = "app-morelogin-acceptance.v1"
TERMINAL_PUBLICATION_STATUSES = {
    "published",
    "not_applicable",
    "needs_review",
    "failed",
    "cancelled",
    "superseded",
}
TERMINAL_SESSION_STATUSES = {"closed", "lost"}
RELEASED_LEASE_STATES = {"released"}
SEALED_INDEX_STATUSES = {"sealed"}

_LIMITATIONS = (
    "本脚本只读核对已落库/已返回 API 的事实，不执行 MoreLogin Open API、ADB、Worker 或故障注入",
    "platform_tech_passed 不等于 APP 质量通过；覆盖不足、"
    "evidence_insufficient、needs_review 仍须 fail-closed",
    "仓库 pytest 与本脚本通过不替代真实云机端到端验收；须先有已完成任务的 task_id/run_id",
    "截图回退通道、ADB 刷新次数等瞬态细节若未写入可审计字段，只能以 Capture/签名多样性作弱证明",
)


@dataclass(frozen=True, kw_only=True)
class AcceptanceCheck:
    """单项可追溯验收检查。"""

    code: str
    passed: bool
    message: str


def verify_morelogin_acceptance_payloads(
    *,
    task_id: str,
    agent_run_id: str,
    task: dict[str, Any],
    run: dict[str, Any],
    report: dict[str, Any] | None = None,
    session: dict[str, Any] | None = None,
    lease: dict[str, Any] | None = None,
    evidence_index: dict[str, Any] | None = None,
    publication_summary: dict[str, Any] | None = None,
    captures: list[dict[str, Any]] | None = None,
    require_multi_frame: bool = True,
) -> list[AcceptanceCheck]:
    """核对 MoreLogin 真实验收口径；Publication terminal/expected 必须对账。"""

    report = report or {}
    session = session or {}
    lease = lease or {}
    evidence_index = evidence_index or {}
    publication_summary = publication_summary or {}
    captures = list(captures or [])
    usage = _mapping(run.get("usage"))
    summary = _mapping(report.get("summary"))
    checks = [
        AcceptanceCheck(
            code="TASK_IDENTITY",
            passed=_text(task.get("task_id") or task.get("id")) == task_id,
            message=f"task_id={task.get('task_id') or task.get('id') or '<missing>'}",
        ),
        AcceptanceCheck(
            code="RUN_IDENTITY",
            passed=_text(run.get("run_id") or run.get("agent_run_id") or run.get("id"))
            == agent_run_id,
            message=(
                "agent_run_id="
                f"{run.get('run_id') or run.get('agent_run_id') or run.get('id') or '<missing>'}"
            ),
        ),
        AcceptanceCheck(
            code="PLATFORM_APP",
            passed=_text(task.get("target_platform")).casefold() == "app",
            message=f"target_platform={task.get('target_platform') or '<missing>'}",
        ),
        _check_morelogin_backend(task=task, session=session, lease=lease),
        _check_session_closed(session=session, run=run),
        _check_lease_released(lease=lease, run=run),
        _check_index_sealed(evidence_index=evidence_index, run=run, report=report),
        _check_publication_entry_counts(
            publication_summary=publication_summary,
            usage=usage,
            report=report,
        ),
        _check_publication_terminal(publication_summary=publication_summary, usage=usage),
        _check_capture_freshness(
            captures=captures,
            usage=usage,
            require_multi_frame=require_multi_frame,
        ),
        _check_completion_gate_audited(report=report, summary=summary),
    ]
    return checks


def evaluate_app_quality(
    *,
    report: dict[str, Any] | None,
    platform_tech_passed: bool,
) -> AcceptanceCheck:
    """单独评估 APP 质量；技术闭环通过不自动升格为质量通过。"""

    report = report or {}
    summary = _mapping(report.get("summary"))
    overall = _text(summary.get("overall_status")).casefold()
    goal_done = bool(summary.get("task_goal_completed"))
    coverage = summary.get("target_page_coverage_rate")
    reached = int(summary.get("reached_target_pages") or 0)
    pending = int(summary.get("pending_target_pages") or 0)
    fail = int(summary.get("fail") or 0)
    if not platform_tech_passed:
        return AcceptanceCheck(
            code="APP_QUALITY",
            passed=False,
            message="平台技术闭环未通过，禁止评估为 APP 质量通过",
        )
    if overall in {"pass", "passed"} and goal_done and fail == 0 and pending == 0:
        return AcceptanceCheck(
            code="APP_QUALITY",
            passed=True,
            message=(
                f"overall={overall or '<missing>'} goal_completed={goal_done} "
                f"reached={reached} coverage={coverage}"
            ),
        )
    return AcceptanceCheck(
        code="APP_QUALITY",
        passed=False,
        message=(
            f"overall={overall or '<missing>'} goal_completed={goal_done} "
            f"reached={reached} pending={pending} fail={fail}；"
            "技术闭环≠质量通过"
        ),
    )


def build_report(
    *,
    task_id: str,
    agent_run_id: str,
    deployed_commit: str,
    config_snapshot_id: str,
    task: dict[str, Any],
    run: dict[str, Any],
    report: dict[str, Any] | None = None,
    session: dict[str, Any] | None = None,
    lease: dict[str, Any] | None = None,
    evidence_index: dict[str, Any] | None = None,
    publication_summary: dict[str, Any] | None = None,
    captures: list[dict[str, Any]] | None = None,
    require_multi_frame: bool = True,
) -> dict[str, Any]:
    checks = verify_morelogin_acceptance_payloads(
        task_id=task_id,
        agent_run_id=agent_run_id,
        task=task,
        run=run,
        report=report,
        session=session,
        lease=lease,
        evidence_index=evidence_index,
        publication_summary=publication_summary,
        captures=captures,
        require_multi_frame=require_multi_frame,
    )
    platform_tech_passed = all(item.passed for item in checks)
    quality = evaluate_app_quality(
        report=report,
        platform_tech_passed=platform_tech_passed,
    )
    return {
        "protocol_version": PROTOCOL_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": "app_morelogin_real_acceptance",
        "task_id": task_id,
        "agent_run_id": agent_run_id,
        "deployed_commit": deployed_commit,
        "config_snapshot_id": config_snapshot_id,
        "platform_tech_status": "passed" if platform_tech_passed else "blocked",
        "app_quality_status": "passed" if quality.passed else "blocked",
        "status": "passed" if platform_tech_passed and quality.passed else "blocked",
        "checks": [asdict(item) for item in checks] + [asdict(quality)],
        "limitations": list(_LIMITATIONS),
    }


def _check_morelogin_backend(
    *,
    task: dict[str, Any],
    session: dict[str, Any],
    lease: dict[str, Any],
) -> AcceptanceCheck:
    device_ref = _text(
        lease.get("device_ref")
        or session.get("device_ref")
        or _mapping(task.get("device")).get("device_ref")
        or _nested(task, "target", "device", "profile_id")
    )
    backend = _text(
        lease.get("backend")
        or session.get("backend")
        or _mapping(task.get("device")).get("backend")
        or _nested(task, "target", "device", "backend")
    ).casefold()
    vendor = _text(
        lease.get("vendor")
        or _mapping(lease.get("capabilities")).get("vendor")
        or _mapping(session.get("capabilities")).get("vendor")
    ).casefold()
    profile = _text(
        lease.get("profile_id")
        or _nested(task, "target", "device", "profile_id")
        or device_ref
    )
    ok = backend in {"cloud_phone", "morelogin"} or device_ref.startswith("cloud_phone:")
    if vendor and vendor not in {"morelogin", ""}:
        ok = False
    if any(token in device_ref.casefold() for token in ("emulator", "local_adb", "genymotion")):
        ok = False
    return AcceptanceCheck(
        code="MORELOGIN_BACKEND",
        passed=ok,
        message=(
            f"backend={backend or '<missing>'} vendor={vendor or '<missing>'} "
            f"profile={profile or '<missing>'} device_ref={device_ref or '<missing>'}"
        ),
    )


def _check_session_closed(
    *,
    session: dict[str, Any],
    run: dict[str, Any],
) -> AcceptanceCheck:
    status = _text(session.get("status") or run.get("session_status")).casefold()
    return AcceptanceCheck(
        code="SESSION_CLOSED",
        passed=status in TERMINAL_SESSION_STATUSES,
        message=f"session.status={status or '<missing>'}",
    )


def _check_lease_released(
    *,
    lease: dict[str, Any],
    run: dict[str, Any],
) -> AcceptanceCheck:
    state = _text(lease.get("state") or run.get("device_lease_state")).casefold()
    health = _text(lease.get("health_state") or run.get("device_health_state")).casefold()
    released = state in RELEASED_LEASE_STATES
    # 租约已释放即可；资源可能随后被隔离，不强制 available
    return AcceptanceCheck(
        code="LEASE_RELEASED",
        passed=released,
        message=f"lease.state={state or '<missing>'} health_state={health or '<missing>'}",
    )


def _check_index_sealed(
    *,
    evidence_index: dict[str, Any],
    run: dict[str, Any],
    report: dict[str, Any],
) -> AcceptanceCheck:
    status = _text(
        evidence_index.get("status")
        or run.get("evidence_index_status")
        or _mapping(report.get("evidence_index")).get("status")
    ).casefold()
    index_id = _text(
        evidence_index.get("index_id")
        or evidence_index.get("evidence_index_id")
        or run.get("evidence_index_id")
    )
    return AcceptanceCheck(
        code="INDEX_SEALED",
        passed=status in SEALED_INDEX_STATUSES and bool(index_id),
        message=f"index_id={index_id or '<missing>'} status={status or '<missing>'}",
    )


def _check_publication_entry_counts(
    *,
    publication_summary: dict[str, Any],
    usage: dict[str, Any],
    report: dict[str, Any],
) -> AcceptanceCheck:
    expected = _int(
        publication_summary.get("expected_entry_count"),
        usage.get("app_publication_expected_entry_count"),
        _mapping(report.get("publication")).get("expected_entry_count"),
    )
    terminal = _int(
        publication_summary.get("terminal_entry_count"),
        usage.get("app_publication_terminal_entry_count"),
        _mapping(report.get("publication")).get("terminal_entry_count"),
    )
    total = _int(
        publication_summary.get("total_count"),
        usage.get("app_inspection_publication_count"),
    )
    passed = expected is not None and terminal is not None and expected == terminal and total != 0
    return AcceptanceCheck(
        code="PUBLICATION_ENTRY_COUNTS",
        passed=bool(passed),
        message=(
            f"expected={expected if expected is not None else '<missing>'} "
            f"terminal={terminal if terminal is not None else '<missing>'} "
            f"publication_total={total if total is not None else '<missing>'}"
        ),
    )


def _check_publication_terminal(
    *,
    publication_summary: dict[str, Any],
    usage: dict[str, Any],
) -> AcceptanceCheck:
    nonterminal = _int(publication_summary.get("nonterminal_count"))
    if nonterminal is None:
        # 无显式非终态计数时，用 usage 失败/复核计数不阻塞本项；由 ENTRY_COUNTS 主控
        failed = _int(usage.get("app_inspection_publication_failed_count")) or 0
        review = _int(usage.get("app_inspection_publication_needs_review_count")) or 0
        total = _int(usage.get("app_inspection_publication_count")) or 0
        passed = total > 0 and nonterminal is None
        return AcceptanceCheck(
            code="PUBLICATION_TERMINAL",
            passed=passed,
            message=(
                f"publication_total={total} failed={failed} needs_review={review} "
                "(无 nonterminal_count 字段时仅要求已产生 Publication)"
            ),
        )
    return AcceptanceCheck(
        code="PUBLICATION_TERMINAL",
        passed=nonterminal == 0,
        message=f"nonterminal_count={nonterminal}",
    )


def _check_capture_freshness(
    *,
    captures: list[dict[str, Any]],
    usage: dict[str, Any],
    require_multi_frame: bool,
) -> AcceptanceCheck:
    if captures:
        signatures = {
            _text(item.get("image_signature") or item.get("signature"))
            for item in captures
            if _text(item.get("image_signature") or item.get("signature"))
        }
        capture_ids = {
            _text(item.get("capture_id") or item.get("id"))
            for item in captures
            if _text(item.get("capture_id") or item.get("id"))
        }
        count = len(capture_ids) or len(captures)
        distinct = len(signatures) or count
    else:
        count = _int(usage.get("app_capture_count"), usage.get("capture_count")) or 0
        distinct = (
            _int(
                usage.get("app_distinct_image_signature_count"),
                usage.get("distinct_signature_count"),
            )
            or count
        )
    if require_multi_frame:
        passed = count >= 2 and distinct >= 2
        detail = "要求至少 2 帧且签名不全部相同"
    else:
        passed = count >= 1
        detail = "至少 1 帧"
    return AcceptanceCheck(
        code="CAPTURE_FRESH_MULTI_FRAME",
        passed=passed,
        message=f"captures={count} distinct_signatures={distinct}；{detail}",
    )


def _check_completion_gate_audited(
    *,
    report: dict[str, Any],
    summary: dict[str, Any],
) -> AcceptanceCheck:
    gate = _mapping(report.get("completion_gate"))
    if gate:
        gate_passed = bool(gate.get("gate_passed"))
        reasons = gate.get("block_reasons") or gate.get("reasons") or []
        return AcceptanceCheck(
            code="COMPLETION_GATE_AUDITED",
            passed=True,
            message=(
                f"gate_passed={gate_passed} block_reasons="
                f"{reasons if isinstance(reasons, list) else reasons}"
            ),
        )
    # 无 gate 快照时，至少要有可审计 overall/termination
    overall = _text(summary.get("overall_status"))
    termination = _text(report.get("termination_reason") or summary.get("termination_reason"))
    passed = bool(overall or termination)
    return AcceptanceCheck(
        code="COMPLETION_GATE_AUDITED",
        passed=passed,
        message=(
            f"overall_status={overall or '<missing>'} "
            f"termination_reason={termination or '<missing>'}"
        ),
    )


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _nested(payload: dict[str, Any], *keys: str) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _int(*values: Any) -> int | None:
    for value in values:
        if value is None or value == "":
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} 必须是 JSON object")
    return payload


def _load_json_url(url: str, *, token: str | None) -> Any:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def _login(base_url: str, username: str, password: str) -> str:
    body = json.dumps(
        {"username": username, "password": password},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/v1/auth/login",
        data=body,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    token = _text(payload.get("access_token") or payload.get("token"))
    if not token:
        raise ValueError("登录未返回 access_token")
    return token


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--agent-run-id", required=True)
    parser.add_argument("--deployed-commit", required=True)
    parser.add_argument("--config-snapshot-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fixture-dir", type=Path, help="离线夹具目录，优先于 API")
    parser.add_argument("--base-url", help="在线核对时的 API 根地址")
    parser.add_argument("--username")
    parser.add_argument("--password")
    parser.add_argument(
        "--access-token-env",
        default="ALIOTH_ACCEPTANCE_ACCESS_TOKEN",
    )
    parser.add_argument(
        "--allow-single-frame",
        action="store_true",
        help="允许单帧（默认要求多帧不同签名）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    task_id = args.task_id
    run_id = args.agent_run_id

    if args.fixture_dir is not None:
        fixture = args.fixture_dir
        task = _load_json(fixture / "task.json")
        run = _load_json(fixture / "run.json")
        report = _load_json(fixture / "report.json") if (fixture / "report.json").is_file() else {}
        session = (
            _load_json(fixture / "session.json") if (fixture / "session.json").is_file() else {}
        )
        lease = _load_json(fixture / "lease.json") if (fixture / "lease.json").is_file() else {}
        evidence_index = (
            _load_json(fixture / "evidence_index.json")
            if (fixture / "evidence_index.json").is_file()
            else {}
        )
        publication_summary = (
            _load_json(fixture / "publication_summary.json")
            if (fixture / "publication_summary.json").is_file()
            else {}
        )
        captures_path = fixture / "captures.json"
        captures_payload = (
            json.loads(captures_path.read_text(encoding="utf-8"))
            if captures_path.is_file()
            else []
        )
        captures = captures_payload if isinstance(captures_payload, list) else []
    else:
        if not args.base_url:
            raise SystemExit("在线模式必须提供 --base-url，或改用 --fixture-dir")
        token = os.environ.get(args.access_token_env) or ""
        if not token:
            if not args.username or not args.password:
                raise SystemExit("缺少 access token 或 username/password")
            token = _login(args.base_url, args.username, args.password)
        base = args.base_url.rstrip("/")
        encoded_task = urllib.parse.quote(task_id, safe="")
        task = _load_json_url(
            f"{base}/api/v1/workstation/tasks/{encoded_task}",
            token=token,
        )
        agent = _load_json_url(
            f"{base}/api/v1/tasks/{encoded_task}/agent-run",
            token=token,
        )
        run = (
            agent.get("run")
            if isinstance(agent, dict) and isinstance(agent.get("run"), dict)
            else agent
        )
        report = _load_json_url(
            f"{base}/api/v1/workstation/reports/{encoded_task}",
            token=token,
        )
        # 下列端点可能尚未统一暴露；缺失时以 run/report 字段兜底
        session = _mapping(run.get("session")) if isinstance(run, dict) else {}
        lease = _mapping(run.get("device_lease")) if isinstance(run, dict) else {}
        evidence_index = _mapping(run.get("evidence_index")) if isinstance(run, dict) else {}
        publication_summary = (
            _mapping(run.get("publication_summary")) if isinstance(run, dict) else {}
        )
        captures = []

    payload = build_report(
        task_id=task_id,
        agent_run_id=run_id,
        deployed_commit=args.deployed_commit,
        config_snapshot_id=args.config_snapshot_id,
        task=task if isinstance(task, dict) else {},
        run=run if isinstance(run, dict) else {},
        report=report if isinstance(report, dict) else {},
        session=session if isinstance(session, dict) else {},
        lease=lease if isinstance(lease, dict) else {},
        evidence_index=evidence_index if isinstance(evidence_index, dict) else {},
        publication_summary=(
            publication_summary if isinstance(publication_summary, dict) else {}
        ),
        captures=captures if isinstance(captures, list) else [],
        require_multi_frame=not args.allow_single_frame,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"status": payload["status"], "output": str(args.output)}, ensure_ascii=False))
    return 0 if payload["platform_tech_status"] == "passed" else 3


if __name__ == "__main__":
    sys.exit(main())
