"""串行执行三个 Web V2 仅探索任务并导出完整公开证据详情。"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

DEFAULT_TARGETS = (
    "https://chigua.com/",
    "https://heiliao.com/",
    "https://hl365.com/",
)
TERMINAL_STATUSES = {"completed", "done", "failed", "canceled", "cancelled", "review"}


def _request(
    method: str,
    url: str,
    *,
    token: str,
    payload: dict[str, object] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, object]:
    headers = {"Authorization": f"Bearer {token}"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload else None,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            value = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:2000]
        return {"_http_error": exc.code, "_body": body}
    if not isinstance(value, dict):
        raise RuntimeError(f"{url} 未返回 JSON 对象")
    return value


def _submission_payload(index: int, target_url: str) -> dict[str, object]:
    return {
        "task_name": f"Web V2 动态探索三站验证 {index}",
        "target": {
            "platform": "web",
            "url": target_url,
            "browser_profile_id": "desktop_fhd_1920x1080_v1",
            "adult_self_attestation": {
                "declared": True,
                "statement_version": "adult-v1",
            },
            "exploration_target": {"applicability_mode": "runtime_dynamic"},
        },
        "channel": "api",
        "scan_mode": "full",
        "timeout_minutes": 180,
        "product": {
            "department_name": "质量保障部",
            "product_code": f"WEB-V2-LIVE-{index}",
            "product_name": urllib.parse.urlsplit(target_url).hostname or target_url,
        },
        "remark": "Web V2 动态探索与证据链完整优化三站实况验证。",
    }


def _find_explanation(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        direct = value.get("web_report_explanation")
        if isinstance(direct, dict):
            return direct
        for child in value.values():
            found = _find_explanation(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_explanation(child)
            if found:
                return found
    return {}


def _site_summary(
    *,
    target_url: str,
    submission: dict[str, object],
    task: dict[str, object],
    evidence: dict[str, object],
    timeline: dict[str, object],
    report: dict[str, object],
) -> dict[str, object]:
    explanation = _find_explanation(timeline) or _find_explanation(report)
    entries = evidence.get("entries") if isinstance(evidence.get("entries"), list) else []
    logical_pages = explanation.get("web_logical_pages")
    logical_pages = logical_pages if isinstance(logical_pages, list) else []
    ad_pages = explanation.get("web_ad_pages")
    ad_pages = ad_pages if isinstance(ad_pages, list) else []
    page_rows = []
    for row in logical_pages:
        if not isinstance(row, dict):
            continue
        supplemental = row.get("supplemental_full_page")
        supplemental = supplemental if isinstance(supplemental, dict) else {}
        page_rows.append(
            {
                "url": row.get("canonical_url"),
                "page_type": row.get("page_type"),
                "coverage_group": row.get("coverage_group"),
                "classification_status": row.get("classification_status"),
                "primary_capture_id": row.get("primary_capture_id"),
                "screenshot_artifact_id": row.get("screenshot_artifact_id"),
                "full_page_status": supplemental.get("status") or "not_reported",
                "full_page_capture_id": supplemental.get("capture_id"),
                "full_page_screenshot_artifact_id": supplemental.get("screenshot_artifact_id"),
                "full_page_reason_code": supplemental.get("reason_code"),
            }
        )
    ad_rows = [
        {
            "requested_url": row.get("requested_url"),
            "final_url": row.get("final_url"),
            "page_type": row.get("page_type"),
            "coverage_group": row.get("coverage_group"),
            "capture_id": row.get("capture_id"),
            "parent_capture_id": row.get("parent_capture_id"),
            "screenshot_artifact_id": row.get("screenshot_artifact_id"),
            "artifact_ids": row.get("artifact_ids"),
        }
        for row in ad_pages
        if isinstance(row, dict)
    ]
    entry_rows = [
        {
            key: row.get(key)
            for key in (
                "menu_entry_id",
                "source_kind",
                "canonical_url",
                "status",
                "menu_page_type",
                "detail_page_type",
                "menu_ready_capture_id",
                "detail_ready_capture_id",
                "error_code",
            )
        }
        for row in entries
        if isinstance(row, dict)
    ]
    page_types = Counter(str(row.get("page_type") or "unclassified") for row in page_rows)
    return {
        "target_url": target_url,
        "task_id": submission.get("task_id"),
        "run_id": submission.get("run_id"),
        "task_status": task.get("status"),
        "run_status": task.get("run_status"),
        "execution_phase": task.get("execution_phase"),
        "failure_reason": task.get("failure_reason"),
        "evidence_status": evidence.get("status"),
        "evidence_reason_codes": evidence.get("reason_codes") or [],
        "page_type_counts": dict(page_types),
        "logical_pages": page_rows,
        "ad_pages": ad_rows,
        "menu_ledger": entry_rows,
        "coverage": evidence.get("coverage"),
        "page_type_summary": explanation.get("page_type_summary"),
        "long_screenshot_count": sum(
            bool(row.get("full_page_screenshot_artifact_id")) for row in page_rows
        ),
        "ad_page_count": len(ad_rows),
    }


def _render_site(summary: dict[str, object]) -> str:
    lines = [
        f"# {summary['target_url']} 探索详情",
        "",
        f"- task_id：{summary.get('task_id')}",
        f"- run_id：{summary.get('run_id')}",
        f"- 任务终态：{summary.get('task_status')} / {summary.get('execution_phase')}",
        f"- 证据包：{summary.get('evidence_status')}",
        f"- 页型计数：`{json.dumps(summary.get('page_type_counts'), ensure_ascii=False)}`",
        f"- 长图证据数：{summary.get('long_screenshot_count')}",
        f"- 广告页证据数：{summary.get('ad_page_count')}",
        "",
        "## URL 与页型",
        "",
        "| URL | 页型 | 分组 | 分类 | 主截图 | 长图状态 | 长图截图 |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in summary.get("logical_pages", []):
        lines.append(
            f"| {row.get('url') or '-'} | {row.get('page_type') or '-'} | "
            f"{row.get('coverage_group') or '-'} | {row.get('classification_status') or '-'} | "
            f"{row.get('screenshot_artifact_id') or '-'} | {row.get('full_page_status') or '-'} | "
            f"{row.get('full_page_screenshot_artifact_id') or '-'} |"
        )
    lines.extend(
        [
            "",
            "## 广告页证据",
            "",
            "| 请求 URL | 最终 URL | 页型 | Capture | 父 Capture | 截图 |",
            "|---|---|---|---|---|---|",
        ]
    )
    for row in summary.get("ad_pages", []):
        lines.append(
            f"| {row.get('requested_url') or '-'} | {row.get('final_url') or '-'} | "
            f"{row.get('page_type') or '-'} | {row.get('capture_id') or '-'} | "
            f"{row.get('parent_capture_id') or '-'} | {row.get('screenshot_artifact_id') or '-'} |"
        )
    if not summary.get("ad_pages"):
        lines.append("| - | - | - | - | - | - |")
    lines.extend(["", "## 菜单账本", "", "```json", json.dumps(summary.get("menu_ledger"), ensure_ascii=False, indent=2), "```", ""])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=10800)
    parser.add_argument("--poll-seconds", type=float, default=5)
    parser.add_argument("targets", nargs="*", default=list(DEFAULT_TARGETS))
    args = parser.parse_args()
    token = os.getenv("ALIOTH_LIVE_TOKEN", "").strip()
    if not token:
        raise SystemExit("缺少 ALIOTH_LIVE_TOKEN")
    args.output.mkdir(parents=True, exist_ok=True)
    all_summaries = []
    for index, target_url in enumerate(args.targets, start=1):
        site_name = urllib.parse.urlsplit(target_url).hostname or f"site-{index}"
        print(f"[{index}/{len(args.targets)}] 提交 {target_url}", flush=True)
        submission = _request(
            "POST",
            f"{args.api_base.rstrip('/')}/api/v2/exploration-tasks",
            token=token,
            payload=_submission_payload(index, target_url),
            idempotency_key=f"web-v2-three-site-{uuid4()}",
        )
        if submission.get("_http_error"):
            raise RuntimeError(f"{target_url} 提交失败：{submission}")
        task_id = str(submission.get("task_id") or "")
        run_id = str(submission.get("run_id") or "")
        if not task_id or not run_id:
            raise RuntimeError(f"{target_url} 提交回执不完整：{submission}")
        deadline = time.monotonic() + args.timeout_seconds
        previous = None
        while True:
            task = _request(
                "GET",
                f"{args.api_base.rstrip('/')}/api/v1/workstation/tasks/{task_id}",
                token=token,
            )
            state = (task.get("status"), task.get("execution_phase"), task.get("progress"))
            if state != previous:
                print(f"  状态={state[0]} 阶段={state[1]} 进度={state[2]}", flush=True)
                previous = state
            if str(task.get("status") or "") in TERMINAL_STATUSES:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{target_url} 超过单站等待上限")
            time.sleep(args.poll_seconds)
        base = args.api_base.rstrip("/")
        evidence = _request("GET", f"{base}/api/v1/workstation/tasks/{task_id}/web-evidence", token=token)
        timeline = _request("GET", f"{base}/api/v1/workstation/tasks/{task_id}/page-timeline", token=token)
        report = _request("GET", f"{base}/api/v1/workstation/reports/{task_id}?agent_run_id={run_id}", token=token)
        summary = _site_summary(
            target_url=target_url,
            submission=submission,
            task=task,
            evidence=evidence,
            timeline=timeline,
            report=report,
        )
        site_dir = args.output / site_name
        site_dir.mkdir(parents=True, exist_ok=True)
        for name, value in (
            ("submission.json", submission),
            ("task.json", task),
            ("web-evidence.json", evidence),
            ("page-timeline.json", timeline),
            ("report.json", report),
            ("summary.json", summary),
        ):
            (site_dir / name).write_text(
                json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        (site_dir / "details.md").write_text(_render_site(summary), encoding="utf-8")
        all_summaries.append(summary)
        print(
            f"  完成 task={task_id} 页型={summary['page_type_counts']} "
            f"长图={summary['long_screenshot_count']} 广告={summary['ad_page_count']}",
            flush=True,
        )
    final = {
        "generated_at": datetime.now(UTC).isoformat(),
        "endpoint": "/api/v2/exploration-tasks",
        "execution_mode": "exploration_only",
        "sites": all_summaries,
    }
    (args.output / "final-report.json").write_text(
        json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = ["# Web V2 三站真实探索最终报告", "", "| 站点 | Task | 终态 | 证据 | 页型 | 长图 | 广告 |", "|---|---|---|---|---|---:|---:|"]
    for row in all_summaries:
        lines.append(
            f"| {row['target_url']} | {row['task_id']} | {row['task_status']} | "
            f"{row['evidence_status']} | `{json.dumps(row['page_type_counts'], ensure_ascii=False)}` | "
            f"{row['long_screenshot_count']} | {row['ad_page_count']} |"
        )
    lines.extend(["", "各站完整 URL、页型、菜单账本、长图与广告证据见对应子目录。", ""])
    (args.output / "final-report.md").write_text("\n".join(lines), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
