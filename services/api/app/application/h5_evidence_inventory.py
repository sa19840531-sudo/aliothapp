"""H5 预期动作证据库存的不可变应用契约。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

_NORMAL_RULE_EXECUTION_STATUSES = frozenset(
    {"pass", "fail", "warning", "manual_review"}
)


class H5EvidenceInventoryStatus(StrEnum):
    """库存项的稳定业务状态。"""

    PASS = "pass"
    INSUFFICIENT = "insufficient"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class H5EvidenceArtifactRef:
    """不含对象正文的证据制品身份。"""

    artifact_id: str
    task_id: str
    agent_run_id: str
    capture_id: str
    storage_path: str
    artifact_type: str
    checksum: str
    size_bytes: int
    lifecycle_status: str = "active"


class EvidenceArtifactReadPort(Protocol):
    """复用既有本地或 MinIO 证据存储的只读能力。"""

    async def read(self, storage_path: str) -> bytes: ...


@dataclass(frozen=True, slots=True)
class H5EvidenceInventoryItem:
    """一个预期 Action 的只读库存结论。"""

    action_execution_id: str
    before_capture_id: str | None
    after_capture_id: str | None
    after_capture: object | None
    status: H5EvidenceInventoryStatus
    reason_codes: tuple[str, ...]
    verification_summary: Mapping[str, object]
    task_id: str = ""
    agent_run_id: str = ""
    session_id: str = ""
    page_type: str | None = None
    page_state: str | None = None
    required_artifacts: tuple[str, ...] = ("screenshot", "dom")
    found_artifacts: tuple[str, ...] = ()
    published_inspection_count: int = 0
    before_manifest_id: str | None = None
    before_manifest_hash: str | None = None
    after_manifest_id: str | None = None
    after_manifest_hash: str | None = None
    authority_source: str = "legacy_object_verified"


@dataclass(frozen=True, slots=True)
class H5EvidenceInventorySummary:
    """本次运行库存汇总；不得因一项不足丢弃其他事实。"""

    total: int
    pass_count: int
    insufficient_count: int
    failed_count: int
    items: tuple[H5EvidenceInventoryItem, ...]


@dataclass(frozen=True, slots=True)
class H5EvidenceInventorySnapshot:
    """库存持久化后的服务端版本快照。"""

    payload_hash: str
    row_version: int
    published_inspection_count: int
    status: H5EvidenceInventoryStatus


def summarize_items(items: tuple[H5EvidenceInventoryItem, ...]) -> H5EvidenceInventorySummary:
    """按稳定状态汇总，避免调用方把业务不足误作系统异常。"""
    return H5EvidenceInventorySummary(
        total=len(items),
        pass_count=sum(item.status is H5EvidenceInventoryStatus.PASS for item in items),
        insufficient_count=sum(
            item.status is H5EvidenceInventoryStatus.INSUFFICIENT for item in items
        ),
        failed_count=sum(item.status is H5EvidenceInventoryStatus.FAILED for item in items),
        items=items,
    )


def project_h5_evidence_preflight(
    *,
    inventory_items: Iterable[Mapping[str, Any]],
    rule_executions: Iterable[Mapping[str, Any]] = (),
    closure: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """把库存、规则发布与 Completion Gate 归并为一份证据预检结论。"""
    items = [dict(item) for item in inventory_items]
    blocked_items = [
        item
        for item in items
        if str(item.get("status") or "")
        in {
            H5EvidenceInventoryStatus.INSUFFICIENT.value,
            H5EvidenceInventoryStatus.FAILED.value,
        }
    ]
    failed_count = sum(
        item.get("status") == H5EvidenceInventoryStatus.FAILED.value for item in items
    )
    insufficient_count = sum(
        item.get("status") == H5EvidenceInventoryStatus.INSUFFICIENT.value
        for item in items
    )
    if failed_count:
        status = "evidence_integrity_failed"
    elif insufficient_count:
        status = "evidence_insufficient"
    elif items and all(
        item.get("status") == H5EvidenceInventoryStatus.PASS.value for item in items
    ):
        status = "ready"
    else:
        status = "not_started"

    blocked_capture_ids = sorted(
        {
            capture_id
            for item in blocked_items
            for field in ("before_capture_id", "after_capture_id")
            if (capture_id := _optional_text(item.get(field)))
        }
    )
    blocked_capture_id_set = set(blocked_capture_ids)
    improperly_published_items = [
        _optional_text(item.get("inventory_item_id")) or "<unknown>"
        for item in blocked_items
        if _nonnegative_int(item.get("published_inspection_count")) > 0
    ]
    improper_rule_results = [
        _optional_text(execution.get("execution_id"))
        or _optional_text(execution.get("rule_code"))
        or "<unknown>"
        for execution in rule_executions
        if str(execution.get("execution_status") or "")
        in _NORMAL_RULE_EXECUTION_STATUSES
        and _execution_references_blocked_capture(execution, blocked_capture_id_set)
    ]
    reason_codes = sorted(
        {
            reason
            for item in blocked_items
            for value in (item.get("reason_codes") or [])
            if (reason := _optional_text(value))
        }
    )
    closure_payload = dict(closure or {})
    gate_passed = closure_payload.get("gate_passed")
    invalid_evidence_publication_blocked = (
        not improperly_published_items and not improper_rule_results
    )
    return {
        "schema_version": "h5-evidence-preflight.v1",
        "status": status,
        "ready": status == "ready",
        "inventory_item_count": len(items),
        "blocked_item_count": len(blocked_items),
        "insufficient_count": insufficient_count,
        "failed_count": failed_count,
        "reason_codes": reason_codes,
        "blocked_inventory_item_ids": [
            _optional_text(item.get("inventory_item_id")) or "<unknown>"
            for item in blocked_items
        ],
        "blocked_capture_ids": blocked_capture_ids,
        "publication_allowed": (
            status == "ready" and invalid_evidence_publication_blocked
        ),
        "invalid_evidence_publication_blocked": (
            invalid_evidence_publication_blocked
        ),
        "improperly_published_inventory_item_ids": improperly_published_items,
        "improper_rule_result_ids": improper_rule_results,
        "gate_passed": gate_passed,
        "gate_block_reasons": list(closure_payload.get("gate_block_reasons") or []),
    }


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _execution_references_blocked_capture(
    execution: Mapping[str, object] | dict[str, object],
    blocked_capture_ids: set[str],
) -> bool:
    """单页用 page_capture_id；聚合结果用 InputSet 成员 Capture 反查。"""

    page_capture_id = _optional_text(execution.get("page_capture_id"))
    if page_capture_id and page_capture_id in blocked_capture_ids:
        return True
    members = execution.get("input_set_member_capture_ids") or ()
    if isinstance(members, (list, tuple, set)):
        for member in members:
            member_id = _optional_text(member)
            if member_id and member_id in blocked_capture_ids:
                return True
    return False


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0
