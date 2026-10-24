#!/usr/bin/env python3
"""只读验证 H5/App 主证据规则投递闭环；当前版本明确不检查补采。"""

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

PROTOCOL_VERSION = "delivery-acceptance.v1"
TERMINAL_PUBLICATION_STATUSES = {
    "published",
    "not_applicable",
    "needs_review",
    "failed",
    "cancelled",
    "superseded",
}


@dataclass(frozen=True, kw_only=True)
class DeliveryAcceptanceCheck:
    """一项可追溯的投递闭环检查。"""

    code: str
    passed: bool
    message: str


def verify_delivery_payloads(
    *,
    task_id: str,
    agent_run_id: str,
    task: dict[str, Any],
    run: dict[str, Any],
    incidents: list[dict[str, Any]],
) -> list[DeliveryAcceptanceCheck]:
    """验证 Publication→Outbox→Inbox→Work 主链，绝不读取补采字段。"""

    platform = str(task.get("target_platform") or "").casefold()
    execution_mode = str(task.get("execution_mode") or "")
    checks = [
        DeliveryAcceptanceCheck(
            code="TASK_IDENTITY",
            passed=str(task.get("task_id") or "") == task_id,
            message=f"task_id={task.get('task_id') or '<missing>'}",
        ),
        DeliveryAcceptanceCheck(
            code="RUN_IDENTITY",
            passed=(
                str(run.get("run_id") or run.get("agent_run_id") or "")
                == agent_run_id
            ),
            message=(
                "agent_run_id="
                f"{run.get('run_id') or run.get('agent_run_id') or '<missing>'}"
            ),
        ),
        DeliveryAcceptanceCheck(
            code="PLATFORM",
            passed=platform in {"h5", "app"},
            message=f"target_platform={platform or '<missing>'}",
        ),
    ]
    if execution_mode == "exploration_only":
        checks.append(
            DeliveryAcceptanceCheck(
                code="PUBLICATION_SCOPE",
                passed=not incidents,
                message="纯探索任务不应产生规则 Publication",
            )
        )
        return checks
    if not incidents:
        checks.append(
            DeliveryAcceptanceCheck(
                code="PUBLICATION_SCOPE",
                passed=False,
                message="完整质检任务缺少 Publication 投递投影",
            )
        )
        return checks

    scoped = all(
        str(item.get("task_id") or "") == task_id
        and str(item.get("platform") or "").casefold() == platform
        for item in incidents
    )
    checks.append(
        DeliveryAcceptanceCheck(
            code="PUBLICATION_SCOPE",
            passed=scoped,
            message=f"{len(incidents)} 条 Publication 均绑定 task/platform={scoped}",
        )
    )
    nonterminal = [
        str(item.get("publication_id") or "<unknown>")
        for item in incidents
        if str(item.get("status") or "") not in TERMINAL_PUBLICATION_STATUSES
    ]
    checks.append(
        DeliveryAcceptanceCheck(
            code="PUBLICATION_TERMINAL",
            passed=not nonterminal,
            message=(
                "Publication 均已终态"
                if not nonterminal
                else f"非终态 Publication：{', '.join(nonterminal[:5])}"
            ),
        )
    )
    pending_delivery = sum(
        _count(item, key)
        for item in incidents
        for key in (
            "outbox_pending_count",
            "outbox_dead_letter_count",
            "inbox_pending_count",
            "inbox_dead_letter_count",
        )
    )
    checks.append(
        DeliveryAcceptanceCheck(
            code="OUTBOX_INBOX_TERMINAL",
            passed=pending_delivery == 0,
            message=f"待处理或死信总数={pending_delivery}",
        )
    )
    active_work = sum(_count(item, "linked_work_active_count") for item in incidents)
    checks.append(
        DeliveryAcceptanceCheck(
            code="INSPECTION_WORK_TERMINAL",
            passed=active_work == 0,
            message=f"未终态规则工作项={active_work}",
        )
    )
    missing_identity = [
        str(item.get("publication_id") or "<unknown>")
        for item in incidents
        if not _source_identity_complete(item, platform)
    ]
    checks.append(
        DeliveryAcceptanceCheck(
            code="SOURCE_IDENTITY",
            passed=not missing_identity,
            message=(
                "主证据身份齐全"
                if not missing_identity
                else f"身份不完整：{', '.join(missing_identity[:5])}"
            ),
        )
    )
    invalid_audits = sum(
        1
        for item in incidents
        for audit in _items(item.get("operations"))
        if not all(
            audit.get(key)
            for key in (
                "operation_id",
                "action",
                "operator_user_id",
                "reason",
                "result",
                "before_state",
                "after_state",
                "created_at",
            )
        )
    )
    checks.append(
        DeliveryAcceptanceCheck(
            code="OPERATION_AUDIT",
            passed=invalid_audits == 0,
            message=f"不完整运维审计={invalid_audits}",
        )
    )
    return checks


def build_report(
    *,
    task_id: str,
    agent_run_id: str,
    deployed_commit: str,
    config_snapshot_id: str,
    task: dict[str, Any],
    run: dict[str, Any],
    incidents: list[dict[str, Any]],
) -> dict[str, Any]:
    checks = verify_delivery_payloads(
        task_id=task_id,
        agent_run_id=agent_run_id,
        task=task,
        run=run,
        incidents=incidents,
    )
    return {
        "protocol_version": PROTOCOL_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": "h5_app_primary_evidence_delivery",
        "supplemental_evidence_in_scope": False,
        "task_id": task_id,
        "agent_run_id": agent_run_id,
        "deployed_commit": deployed_commit,
        "config_snapshot_id": config_snapshot_id,
        "status": "passed" if all(item.passed for item in checks) else "blocked",
        "checks": [asdict(item) for item in checks],
        "incidents": incidents,
        "limitations": [
            "本脚本只读核对已部署 API 的持久化投递事实，不执行故障注入",
            "仓库测试、Mock 和脚本通过不替代真实 PostgreSQL/Redis/Worker 验收",
            "App 仍需 Official Provider 与真机/模拟器验收，H5 仍需真实浏览器验收",
            "当前版本明确排除补采链路",
        ],
    }


def _load_json(url: str, token: str) -> Any:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--agent-run-id", required=True)
    parser.add_argument("--deployed-commit", required=True)
    parser.add_argument("--config-snapshot-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--access-token-env",
        default="ALIOTH_ACCEPTANCE_ACCESS_TOKEN",
        help="Bearer Token 所在环境变量名；Token 不写入报告或 URL",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    token = os.getenv(args.access_token_env, "").strip()
    if not token:
        print(f"缺少环境变量 {args.access_token_env}", file=sys.stderr)
        return 1
    base = args.base_url.rstrip("/")
    task_id = urllib.parse.quote(args.task_id, safe="")
    run_id = urllib.parse.quote(args.agent_run_id, safe="")
    try:
        task = _load_json(f"{base}/api/v1/workstation/tasks/{task_id}", token)
        run = _load_json(f"{base}/api/v1/agents/runs/{run_id}", token)
        incidents = _load_json(
            f"{base}/api/v1/workstation/system/delivery-incidents?task_id={task_id}",
            token,
        )
        if not isinstance(task, dict) or not isinstance(run, dict):
            raise ValueError("任务或 Run 接口没有返回对象")
        if not isinstance(incidents, list) or not all(
            isinstance(item, dict) for item in incidents
        ):
            raise ValueError("投递事故接口没有返回对象数组")
        report = build_report(
            task_id=args.task_id,
            agent_run_id=args.agent_run_id,
            deployed_commit=args.deployed_commit,
            config_snapshot_id=args.config_snapshot_id,
            task=task,
            run=run,
            incidents=incidents,
        )
    except Exception as exc:  # noqa: BLE001 - CLI 需要稳定退出码和脱敏错误
        print(f"读取验收 API 失败：{exc}", file=sys.stderr)
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 2


def _source_identity_complete(item: dict[str, Any], platform: str) -> bool:
    source = item.get("source_identity")
    if not isinstance(source, dict):
        return False
    if platform == "h5":
        return all(
            source.get(key)
            for key in (
                "source_agent_run_id",
                "page_capture_id",
                "page_type_assessment_id",
                "assessment_hash",
            )
        )
    return all(
        source.get(key)
        for key in (
            "source_agent_run_id",
            "evidence_index_id",
            "evidence_index_hash",
        )
    )


def _count(item: dict[str, Any], key: str) -> int:
    value = item.get(key)
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0


def _items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


if __name__ == "__main__":
    raise SystemExit(main())
