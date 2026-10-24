"""按 Action Execution 查询完整前后页面证据的只读 Agent Tool。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from services.api.app.agents.runtime.run_context import require_agent_run_context
from services.api.app.agents.tools.registry import PermissionLevel, tool
from services.api.app.application.evidence_access import (
    EVIDENCE_SOURCE_ARTIFACT_TABLE,
    EVIDENCE_SOURCE_MANIFEST,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool

PROTOCOL_VERSION = "action-evidence-bundle.v1"
REQUIRED_ARTIFACT_TYPES = ("dom", "screenshot", "ui_snapshot")
TERMINAL_FAILURE_STATUSES = frozenset(
    {"blocked", "failed", "timed_out", "evidence_failed"}
)
DEFAULT_TIMEOUT_SECONDS = 60
MAX_TIMEOUT_SECONDS = 180
POLL_INTERVAL_SECONDS = 0.5

logger = logging.getLogger(__name__)
_sleep = asyncio.sleep
_monotonic = time.monotonic


def _missing_requirements(snapshot: Mapping[str, Any] | None) -> list[str]:
    """返回阻止证据包进入 ready 的稳定缺口路径。"""
    if snapshot is None:
        return ["action_execution"]

    missing: list[str] = []
    if snapshot.get("status") != "completed":
        missing.append("action_execution.completed")

    for side, expected_role in (("before", "before_action"), ("after", "after_action")):
        capture = snapshot.get(side)
        if not isinstance(capture, Mapping):
            missing.append(side)
            continue
        if not _capture_identity_valid(
            snapshot,
            capture,
            expected_role=expected_role,
        ):
            missing.append(f"{side}.identity")
            continue
        if not _nonempty(capture.get("url")):
            missing.append(f"{side}.url")
        if not _nonempty(capture.get("page_type")):
            missing.append(f"{side}.page_type")
        artifacts = _valid_artifacts(snapshot, capture)
        for artifact_type in REQUIRED_ARTIFACT_TYPES:
            if not any(item.get("artifact_type") == artifact_type for item in artifacts):
                missing.append(f"{side}.{artifact_type}")
    return missing


def _is_ready(snapshot: Mapping[str, Any] | None) -> bool:
    """仅完整的 completed 动作可供 Agent 消费。"""
    return bool(snapshot) and snapshot.get("status") == "completed" and not _missing_requirements(
        snapshot
    )


def _is_terminal_failure(snapshot: Mapping[str, Any] | None) -> bool:
    """识别已经不可能继续推进到完整成功的动作终态。"""
    return bool(snapshot) and snapshot.get("status") in TERMINAL_FAILURE_STATUSES


def _capture_identity_valid(
    snapshot: Mapping[str, Any],
    capture: Mapping[str, Any],
    *,
    expected_role: str,
) -> bool:
    """验证 Capture 及其全部 Artifact 均属于当前动作。"""
    if not (
        capture.get("task_id") == snapshot.get("task_id")
        and capture.get("surface_kind") == "web"
        and capture.get("action_execution_id") == snapshot.get("action_execution_id")
        and capture.get("capture_role") == expected_role
        and capture.get("capture_status") == "captured"
        and _nonempty(capture.get("capture_id"))
    ):
        return False
    raw_artifacts = capture.get("artifacts")
    if raw_artifacts is None:
        return True
    if not isinstance(raw_artifacts, Sequence) or isinstance(
        raw_artifacts, (str, bytes, bytearray)
    ):
        return False
    return all(
        isinstance(item, Mapping)
        and item.get("task_id") == snapshot.get("task_id")
        and item.get("page_capture_id") == capture.get("capture_id")
        for item in raw_artifacts
    )


def _valid_artifacts(
    snapshot: Mapping[str, Any],
    capture: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    """提取通过任务和 Capture 双重归属校验的 Artifact。"""
    raw = capture.get("artifacts")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        return []
    expected_task_id = snapshot.get("task_id")
    expected_capture_id = capture.get("capture_id")
    return [
        item
        for item in raw
        if isinstance(item, Mapping)
        and item.get("task_id") == expected_task_id
        and item.get("page_capture_id") == expected_capture_id
    ]


def _primary_artifact(
    artifacts: Sequence[Mapping[str, Any]],
    artifact_type: str,
) -> dict[str, Any] | None:
    """按创建时间和 ID 稳定选择单一主 Artifact。"""
    candidates = [item for item in artifacts if item.get("artifact_type") == artifact_type]
    if not candidates:
        return None
    selected = min(
        candidates,
        key=lambda item: (
            _isoformat_utc(item.get("created_at")) or "",
            str(item.get("artifact_id") or ""),
        ),
    )
    return {
        "artifact_id": selected.get("artifact_id"),
        "storage_path": selected.get("storage_path"),
    }


def _latest_action_trace(snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
    """选择 after Capture 中最后一条结构化动作记录。"""
    after = snapshot.get("after")
    action_log = after.get("action_log") if isinstance(after, Mapping) else None
    if not isinstance(action_log, Sequence) or isinstance(
        action_log, (str, bytes, bytearray)
    ):
        return {}
    return next(
        (item for item in reversed(action_log) if isinstance(item, Mapping)),
        {},
    )


def _capture_payload(
    snapshot: Mapping[str, Any],
    *,
    side: str,
    expected_role: str,
) -> dict[str, Any] | None:
    """构造不包含证据正文的 Capture 摘要。"""
    capture = snapshot.get(side)
    if not isinstance(capture, Mapping) or not _capture_identity_valid(
        snapshot,
        capture,
        expected_role=expected_role,
    ):
        return None
    artifacts = _valid_artifacts(snapshot, capture)
    payload = {
        "capture_id": capture.get("capture_id"),
        "url": capture.get("url"),
        "page_type": capture.get("page_type"),
        "captured_at": _isoformat_utc(capture.get("captured_at")),
        "dom": _primary_artifact(artifacts, "dom"),
        "screenshot": _primary_artifact(artifacts, "screenshot"),
        "ui_snapshot": _primary_artifact(artifacts, "ui_snapshot"),
        # 证据来源与清单身份：调用方据此判断拿到的是否为受裁决约束的证据
        "evidence_source": capture.get("evidence_source"),
    }
    if capture.get("manifest_id") is not None:
        payload["manifest"] = {
            "manifest_id": capture.get("manifest_id"),
            "manifest_version": capture.get("manifest_version"),
            "quality_status": capture.get("manifest_quality_status"),
        }
    return payload


def _action_payload(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """从 Intent、Action Trace 与 Capture 回退字段构造动作摘要。"""
    intent = snapshot.get("intent_payload")
    intent = intent if isinstance(intent, Mapping) else {}
    trace = _latest_action_trace(snapshot)
    navigation = trace.get("navigation")
    navigation = navigation if isinstance(navigation, Mapping) else {}
    before = snapshot.get("before")
    before = before if isinstance(before, Mapping) else {}
    after = snapshot.get("after")
    after = after if isinstance(after, Mapping) else {}
    target_before = _mapping_or_none(trace.get("target_before"))
    target_after = _mapping_or_none(trace.get("target_after"))
    before_url = _first_nonempty(
        trace.get("before_url"),
        navigation.get("before_url"),
        before.get("url"),
    )
    after_url = _first_nonempty(
        trace.get("after_url"),
        navigation.get("after_url"),
        after.get("url"),
    )
    raw_new_page_urls = trace.get("new_page_urls", navigation.get("new_page_urls", []))
    new_page_urls = (
        [str(item) for item in raw_new_page_urls if _nonempty(item)]
        if isinstance(raw_new_page_urls, Sequence)
        and not isinstance(raw_new_page_urls, (str, bytes, bytearray))
        else []
    )
    return {
        "type": _first_nonempty(intent.get("action_type"), trace.get("action")),
        "target": _target_label(target_before, target_after, intent),
        "selector": _first_nonempty(
            trace.get("selector"),
            trace.get("requested_selector"),
        ),
        "purpose": intent.get("purpose"),
        "before_url": before_url,
        "after_url": after_url,
        "transition_kind": trace.get("transition_kind"),
        "new_page_urls": new_page_urls,
    }


def _diff_payload(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """构造 URL 与有限 UI 状态差异。"""
    trace = _latest_action_trace(snapshot)
    action = _action_payload(snapshot)
    before_url = action.get("before_url")
    after_url = action.get("after_url")
    return {
        "url_changed": bool(
            before_url is not None and after_url is not None and before_url != after_url
        ),
        "state_diff": (
            trace.get("state_diff")
            if isinstance(trace.get("state_diff"), Mapping)
            else {}
        ),
        "target_before": _mapping_or_none(trace.get("target_before")),
        "target_after": _mapping_or_none(trace.get("target_after")),
    }


def _build_bundle(
    snapshot: Mapping[str, Any] | None,
    *,
    timeout_seconds: int,
    elapsed_ms: int,
    outcome: str,
    action_execution_id: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    """构造所有等待结果共用的稳定版本化协议。"""
    wait = {
        "outcome": outcome,
        "timeout_seconds": timeout_seconds,
        "elapsed_ms": max(int(elapsed_ms), 0),
        "missing_requirements": _missing_requirements(snapshot),
    }
    if error_code is not None:
        wait["error_code"] = error_code
    if error_message is not None:
        wait["error_message"] = error_message

    if snapshot is None:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "action_execution_id": action_execution_id,
            "status": None,
            "wait": wait,
            "before": None,
            "action": None,
            "after": None,
            "diff": None,
        }

    return {
        "protocol_version": PROTOCOL_VERSION,
        "action_execution_id": snapshot.get("action_execution_id") or action_execution_id,
        "status": snapshot.get("status"),
        "wait": wait,
        "before": _capture_payload(
            snapshot,
            side="before",
            expected_role="before_action",
        ),
        "action": _action_payload(snapshot),
        "after": _capture_payload(
            snapshot,
            side="after",
            expected_role="after_action",
        ),
        "diff": _diff_payload(snapshot),
    }


def _target_label(
    target_before: Mapping[str, Any] | None,
    target_after: Mapping[str, Any] | None,
    intent: Mapping[str, Any],
) -> Any:
    return _first_nonempty(
        target_before.get("text") if target_before else None,
        target_before.get("aria_label") if target_before else None,
        target_after.get("text") if target_after else None,
        target_after.get("aria_label") if target_after else None,
        intent.get("semantic_target"),
    )


def _mapping_or_none(value: Any) -> dict[str, Any] | None:
    return dict(value) if isinstance(value, Mapping) else None


def _first_nonempty(*values: Any) -> Any:
    return next((value for value in values if _nonempty(value)), None)


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _isoformat_utc(value: Any) -> str | None:
    if isinstance(value, datetime):
        normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return normalized.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return str(value) if _nonempty(value) else None


async def _query_action_evidence(
    *,
    task_id: str,
    action_execution_id: str,
) -> dict[str, Any] | None:
    """以 Action Execution 为驱动读取允许不完整的前后证据投影。"""
    pool = await get_postgres_pool()
    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            """
            SELECT
                action.action_execution_id,
                action.task_id::text AS task_id,
                action.status,
                action.intent_payload,
                action.error_code,
                action.error_message,
                before_capture.id::text AS before_capture_id,
                before_capture.task_id::text AS before_task_id,
                before_capture.surface_kind AS before_surface_kind,
                before_capture.action_execution_id AS before_action_execution_id,
                before_capture.capture_role AS before_capture_role,
                before_capture.capture_status AS before_capture_status,
                before_capture.captured_at AS before_captured_at,
                before_page.url AS before_url,
                before_page.page_type AS before_page_type,
                before_page.action_log AS before_action_log,
                before_evidence.artifacts AS before_artifacts,
                before_manifest.artifacts AS before_manifest_artifacts,
                before_manifest.manifest_id AS before_manifest_id,
                before_manifest.manifest_version AS before_manifest_version,
                before_manifest.quality_status AS before_manifest_quality_status,
                after_capture.id::text AS after_capture_id,
                after_capture.task_id::text AS after_task_id,
                after_capture.surface_kind AS after_surface_kind,
                after_capture.action_execution_id AS after_action_execution_id,
                after_capture.capture_role AS after_capture_role,
                after_capture.capture_status AS after_capture_status,
                after_capture.captured_at AS after_captured_at,
                after_page.url AS after_url,
                after_page.page_type AS after_page_type,
                after_page.action_log AS after_action_log,
                after_evidence.artifacts AS after_artifacts,
                after_manifest.artifacts AS after_manifest_artifacts,
                after_manifest.manifest_id AS after_manifest_id,
                after_manifest.manifest_version AS after_manifest_version,
                after_manifest.quality_status AS after_manifest_quality_status
            FROM qc_action_execution AS action
            LEFT JOIN qc_capture AS before_capture
                ON before_capture.id = action.source_capture_id
            LEFT JOIN qc_page_capture AS before_page
                ON before_page.id = before_capture.id
            LEFT JOIN LATERAL (
                SELECT jsonb_agg(
                    jsonb_build_object(
                        'artifact_id', artifact.id::text,
                        'artifact_type', artifact.artifact_type,
                        'storage_path', artifact.storage_path,
                        'task_id', artifact.task_id::text,
                        'page_capture_id', artifact.page_capture_id::text,
                        'created_at', artifact.created_at
                    )
                    ORDER BY artifact.created_at ASC, artifact.id ASC
                ) AS artifacts
                FROM qc_evidence_artifact AS artifact
                WHERE artifact.page_capture_id = before_capture.id
            ) AS before_evidence ON TRUE
            LEFT JOIN LATERAL (
                SELECT
                    latest.manifest_id,
                    latest.manifest_version,
                    latest.quality_status,
                    (
                        SELECT jsonb_agg(
                            jsonb_build_object(
                                'artifact_id', item ->> 'artifact_id',
                                'artifact_type', item ->> 'artifact_type',
                                'storage_path', item ->> 'storage_path',
                                'checksum', item ->> 'checksum',
                                'size_bytes', item -> 'size_bytes',
                                'task_id', latest.task_id::text,
                                'page_capture_id', latest.capture_id::text,
                                'created_at', latest.created_at
                            )
                            ORDER BY item ->> 'artifact_id'
                        )
                        FROM jsonb_array_elements(
                            latest.manifest_payload -> 'artifacts'
                        ) AS item
                    ) AS artifacts
                FROM qc_evidence_manifest AS latest
                WHERE latest.capture_id = before_capture.id
                  AND latest.task_id = action.task_id
                ORDER BY latest.manifest_version DESC
                LIMIT 1
            ) AS before_manifest ON TRUE
            LEFT JOIN qc_capture AS after_capture
                ON after_capture.id = action.result_capture_id
            LEFT JOIN qc_page_capture AS after_page
                ON after_page.id = after_capture.id
            LEFT JOIN LATERAL (
                SELECT jsonb_agg(
                    jsonb_build_object(
                        'artifact_id', artifact.id::text,
                        'artifact_type', artifact.artifact_type,
                        'storage_path', artifact.storage_path,
                        'task_id', artifact.task_id::text,
                        'page_capture_id', artifact.page_capture_id::text,
                        'created_at', artifact.created_at
                    )
                    ORDER BY artifact.created_at ASC, artifact.id ASC
                ) AS artifacts
                FROM qc_evidence_artifact AS artifact
                WHERE artifact.page_capture_id = after_capture.id
            ) AS after_evidence ON TRUE
            LEFT JOIN LATERAL (
                SELECT
                    latest.manifest_id,
                    latest.manifest_version,
                    latest.quality_status,
                    (
                        SELECT jsonb_agg(
                            jsonb_build_object(
                                'artifact_id', item ->> 'artifact_id',
                                'artifact_type', item ->> 'artifact_type',
                                'storage_path', item ->> 'storage_path',
                                'checksum', item ->> 'checksum',
                                'size_bytes', item -> 'size_bytes',
                                'task_id', latest.task_id::text,
                                'page_capture_id', latest.capture_id::text,
                                'created_at', latest.created_at
                            )
                            ORDER BY item ->> 'artifact_id'
                        )
                        FROM jsonb_array_elements(
                            latest.manifest_payload -> 'artifacts'
                        ) AS item
                    ) AS artifacts
                FROM qc_evidence_manifest AS latest
                WHERE latest.capture_id = after_capture.id
                  AND latest.task_id = action.task_id
                ORDER BY latest.manifest_version DESC
                LIMIT 1
            ) AS after_manifest ON TRUE
            WHERE action.action_execution_id = $1
              AND action.task_id = $2::uuid
            """,
            action_execution_id,
            task_id,
        )
    return _snapshot_from_row(row) if row is not None else None


def _snapshot_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """将 asyncpg Record 或测试映射转换为内部证据快照。"""
    return {
        "action_execution_id": _row_get(row, "action_execution_id"),
        "task_id": _row_get(row, "task_id"),
        "status": _row_get(row, "status"),
        "intent_payload": _mapping_value(_row_get(row, "intent_payload")),
        "error_code": _row_get(row, "error_code"),
        "error_message": _row_get(row, "error_message"),
        "before": _capture_from_row(row, prefix="before"),
        "after": _capture_from_row(row, prefix="after"),
    }


def _capture_from_row(
    row: Mapping[str, Any],
    *,
    prefix: str,
) -> dict[str, Any] | None:
    capture_id = _row_get(row, f"{prefix}_capture_id")
    if capture_id is None:
        return None
    # 证据来源优先取清单：清单里的附件带 checksum 且受裁决约束；
    # 清单缺席（如 App 旁路写入失败）时回退附件表，并标注来源供排查。
    manifest_artifacts = _sequence_value(_row_get(row, f"{prefix}_manifest_artifacts"))
    manifest_id = _row_get(row, f"{prefix}_manifest_id")
    if manifest_artifacts:
        artifacts = manifest_artifacts
        evidence_source = EVIDENCE_SOURCE_MANIFEST
    else:
        artifacts = _sequence_value(_row_get(row, f"{prefix}_artifacts"))
        evidence_source = EVIDENCE_SOURCE_ARTIFACT_TABLE
    return {
        "capture_id": capture_id,
        "task_id": _row_get(row, f"{prefix}_task_id"),
        "surface_kind": _row_get(row, f"{prefix}_surface_kind"),
        "action_execution_id": _row_get(row, f"{prefix}_action_execution_id"),
        "capture_role": _row_get(row, f"{prefix}_capture_role"),
        "capture_status": _row_get(row, f"{prefix}_capture_status"),
        "captured_at": _row_get(row, f"{prefix}_captured_at"),
        "url": _row_get(row, f"{prefix}_url"),
        "page_type": _row_get(row, f"{prefix}_page_type"),
        "action_log": _sequence_value(_row_get(row, f"{prefix}_action_log")),
        "artifacts": artifacts,
        "evidence_source": evidence_source,
        "manifest_id": manifest_id,
        "manifest_version": _row_get(row, f"{prefix}_manifest_version"),
        "manifest_quality_status": _row_get(row, f"{prefix}_manifest_quality_status"),
    }


async def _wait_for_action_evidence(
    *,
    task_id: str,
    action_execution_id: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    """等待动作终态和完整证据，并把所有异常收敛为稳定协议。"""
    started_at = _monotonic()
    latest: Mapping[str, Any] | None = None
    while True:
        try:
            latest = await _query_action_evidence(
                task_id=task_id,
                action_execution_id=action_execution_id,
            )
        except Exception:  # noqa: BLE001 - Tool 不得向模型暴露数据库异常
            logger.exception(
                "Action 证据查询失败，task_id=%s action_execution_id=%s",
                task_id,
                action_execution_id,
            )
            return _build_bundle(
                latest,
                action_execution_id=action_execution_id,
                timeout_seconds=timeout_seconds,
                elapsed_ms=_elapsed_ms(started_at),
                outcome="error",
                error_code="ACTION_EVIDENCE_QUERY_FAILED",
                error_message="动作证据查询失败",
            )

        observed_at = _monotonic()
        elapsed_ms = max(int(round((observed_at - started_at) * 1000)), 0)
        if _is_terminal_failure(latest):
            return _build_bundle(
                latest,
                action_execution_id=action_execution_id,
                timeout_seconds=timeout_seconds,
                elapsed_ms=elapsed_ms,
                outcome="terminal_failure",
                error_code="ACTION_EVIDENCE_TERMINAL_FAILURE",
                error_message="动作执行未成功完成",
            )
        if _is_ready(latest):
            return _build_bundle(
                latest,
                action_execution_id=action_execution_id,
                timeout_seconds=timeout_seconds,
                elapsed_ms=elapsed_ms,
                outcome="ready",
            )
        if observed_at - started_at >= timeout_seconds:
            return _build_bundle(
                latest,
                action_execution_id=action_execution_id,
                timeout_seconds=timeout_seconds,
                elapsed_ms=elapsed_ms,
                outcome="timeout",
                error_code="ACTION_EVIDENCE_WAIT_TIMEOUT",
                error_message="等待动作证据超时",
            )
        await _sleep(POLL_INTERVAL_SECONDS)


async def get_action_execution_evidence(
    *,
    action_execution_id: str,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """查询当前 Agent Run 所属任务的完整动作证据包。"""
    action_id, normalized_timeout = _normalize_request(
        action_execution_id,
        timeout_seconds,
    )
    if action_id is None or normalized_timeout is None:
        return _build_bundle(
            None,
            action_execution_id=action_id,
            timeout_seconds=(
                min(timeout_seconds, MAX_TIMEOUT_SECONDS)
                if type(timeout_seconds) is int and timeout_seconds >= 1
                else DEFAULT_TIMEOUT_SECONDS
            ),
            elapsed_ms=0,
            outcome="invalid_request",
            error_code="ACTION_EVIDENCE_INVALID_REQUEST",
            error_message="动作证据查询参数无效",
        )
    try:
        context = require_agent_run_context()
    except RuntimeError:
        return _build_bundle(
            None,
            action_execution_id=action_id,
            timeout_seconds=normalized_timeout,
            elapsed_ms=0,
            outcome="blocked",
            error_code="ACTION_EVIDENCE_CONTEXT_REQUIRED",
            error_message="缺少业务 Agent Run 上下文",
        )
    return await _wait_for_action_evidence(
        task_id=str(context.task_id),
        action_execution_id=action_id,
        timeout_seconds=normalized_timeout,
    )


@tool(
    name="get_action_execution_evidence_tool",
    description=(
        "按当前任务下的 action_execution_id 等待动作完成与前后页面证据齐备，"
        "返回页面类型、DOM/截图/UI Snapshot 引用、动作和差异摘要。"
    ),
    category="动作证据查询",
    permission_level=PermissionLevel.SAFE,
)
async def get_action_execution_evidence_tool(
    action_execution_id: str,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """查询当前 Agent Run 所属任务的完整动作证据包。"""
    return await get_action_execution_evidence(
        action_execution_id=action_execution_id,
        timeout_seconds=timeout_seconds,
    )


def _normalize_request(
    action_execution_id: Any,
    timeout_seconds: Any,
) -> tuple[str | None, int | None]:
    action_id = action_execution_id.strip() if isinstance(action_execution_id, str) else ""
    if not action_id or len(action_id) > 96:
        return None, None
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int):
        return action_id, None
    if timeout_seconds < 1:
        return action_id, None
    return action_id, min(timeout_seconds, MAX_TIMEOUT_SECONDS)


def _elapsed_ms(started_at: float) -> int:
    return max(int(round((_monotonic() - started_at) * 1000)), 0)


def _mapping_value(value: Any) -> dict[str, Any]:
    parsed = _json_value(value, default={})
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _sequence_value(value: Any) -> list[Any]:
    parsed = _json_value(value, default=[])
    if not isinstance(parsed, Sequence) or isinstance(parsed, (str, bytes, bytearray)):
        return []
    return list(parsed)


def _json_value(value: Any, *, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value


def _row_get(row: Mapping[str, Any], key: str) -> Any:
    try:
        return row[key]
    except (KeyError, TypeError):
        return None
