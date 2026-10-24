"""H5 本次运行的失败事实：诊断采集不等于有效目标页面，缺口不等于业务成果。"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

from services.api.app.application.task_progress import rule_execution_counts_as_completed


@dataclass(frozen=True)
class H5RunFailureFacts:
    """供 Worker 与报告读侧共用的事实，不创建另一套任务状态机。"""

    valid_target_capture_count: int
    business_result_count: int
    technical_failure_reason: str | None

    @property
    def has_partial_results(self) -> bool:
        return self.valid_target_capture_count > 0 or self.business_result_count > 0

    @property
    def completely_failed(self) -> bool:
        return bool(self.technical_failure_reason) and not self.has_partial_results


def classify_h5_run_failure(
    context: Mapping[str, object],
    pages: Sequence[Mapping[str, object]],
    rule_executions: Sequence[Mapping[str, object]],
    inventory: Sequence[Mapping[str, object]] = (),
    actions: Sequence[Mapping[str, object]] = (),
) -> H5RunFailureFacts:
    """只使用当前 Task/主 Run 事实；主机匹配沿用冻结命令范围的精确 hostname 语义。"""
    run_id = str(context.get("run_id") or context.get("agent_run_id") or "")
    target_host = _hostname(context.get("target_url"))
    valid_inventory = {
        str(item.get("after_capture_id"))
        for item in inventory
        if item.get("status") == "pass" and _same_run(item, run_id, allow_missing=True)
    }
    valid_ids: set[str] = set()
    invalid_ids: set[str] = set()
    outside_target = False
    diagnostic_reason = None
    for page in pages:
        if not _same_run(page, run_id, allow_missing=True):
            continue
        metadata = _mapping(page.get("metadata"))
        host = _hostname(page.get("url") or metadata.get("url"))
        role = page.get("capture_role") or metadata.get("capture_role")
        if role == "before_action":
            continue
        metrics = _mapping(metadata.get("pipeline_metrics") or metadata.get("metrics"))
        scope = _mapping(metadata.get("navigation_scope") or metrics.get("navigation_scope"))
        document = _mapping(metrics.get("navigation_document"))
        capture_id = str(page.get("page_capture_id") or page.get("capture_id") or "")
        allowed_hosts = {target_host} if target_host else set()
        # 可信后态核验已经检查冻结命令范围，只认可同次采集的具体合法落点。
        if host and scope.get("verified") is True and _hostname(scope.get("final_url")) == host:
            allowed_hosts.add(host)
        if host and host not in allowed_hosts:
            outside_target = True
            if capture_id:
                invalid_ids.add(capture_id)
            continue
        diagnostic = (
            metadata.get("diagnostic_capture") is True
            or metrics.get("diagnostic_capture") is True
            or scope.get("verified") is False
            or document.get("inspectable") is False
        )
        if diagnostic:
            diagnostic_reason = str(
                scope.get("error_code") or document.get("error_code")
                or "H5_DIAGNOSTIC_CAPTURE_ONLY"
            )
            if capture_id:
                invalid_ids.add(capture_id)
            continue
        inspectable = capture_id in valid_inventory or (
            page.get("detection_status") == "recognized"
            and page.get("capture_status", "captured") == "captured"
            and page.get("inspection_lifecycle_status") not in {"abandoned", "superseded"}
        )
        if host and host in allowed_hosts and capture_id and inspectable:
            valid_ids.add(capture_id)
    business_count = sum(
        _same_run(item, run_id, allow_missing=False)
        and str(item.get("page_capture_id") or item.get("capture_id") or "") not in invalid_ids
        and rule_execution_counts_as_completed(
            str(item.get("execution_status") or ""),
            item.get("execution_reason"),
            is_bootstrap=bool(item.get("is_bootstrap")),
            result_summary=item.get("result_summary"),
        )
        for item in rule_executions
    )
    technical_reason = "COMMAND_HOST_BOUNDARY_VIOLATION" if outside_target else diagnostic_reason
    for action in actions:
        if not _same_run(action, run_id, allow_missing=True):
            continue
        code = str(action.get("error_code") or action.get("failure_code") or "")
        if code.startswith(("COMMAND_", "BROWSER_", "SURFACE_", "CAPTURE_", "WEB_NAVIGATION_")):
            technical_reason = technical_reason or code
    termination = str(context.get("termination_reason") or context.get("stop_reason") or "")
    if not technical_reason and context.get("run_status") == "failed" and termination == "error":
        technical_reason = "H5_RUNTIME_FAILED"
    return H5RunFailureFacts(len(valid_ids), business_count, technical_reason)


def _same_run(item: Mapping[str, object], run_id: str, *, allow_missing: bool) -> bool:
    sources = [item.get("source_agent_run_id"), item.get("agent_run_id")]
    present = [str(value) for value in sources if value]
    return run_id in present if present else allow_missing


def _hostname(value: object) -> str | None:
    try:
        parsed = urlsplit(str(value or ""))
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            return None
        return parsed.hostname.casefold()
    except ValueError:
        return None


def _mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, Mapping) else {}
