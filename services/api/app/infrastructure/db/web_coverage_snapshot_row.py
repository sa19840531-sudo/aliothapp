"""Web Coverage Snapshot 数据库完整行的无依赖领域还原。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from services.api.app.domain.exploration.web_coverage_snapshot import (
    WEB_COVERAGE_SNAPSHOT_V2,
    WEB_COVERAGE_SNAPSHOT_V3,
    WEB_COVERAGE_SNAPSHOT_V4,
    WebCoverageSnapshot,
)


class InvalidWebCoverageSnapshotRowError(ValueError):
    """数据库 Coverage 行缺字段、非规范或哈希重算失败。"""


def parse_web_coverage_snapshot_row(row: Mapping[str, Any]) -> WebCoverageSnapshot:
    """读取完整不可变行并构造领域对象，构造过程会重算稳定哈希。"""

    try:
        required = _json_text_tuple(row["required_page_types"])
        observed = _json_text_tuple(row["observed_page_types"])
        missing = _json_text_tuple(row["missing_page_types"])
        unrecognized = _json_text_tuple(row["unrecognized_page_types"])
        applicability = _stored_applicability(
            row.get("applicability_snapshot"),
            required,
            observed=observed,
            missing=missing,
            unrecognized=unrecognized,
        )
        return WebCoverageSnapshot(
            snapshot_id=_required_text(row.get("id"), "id"),
            task_id=_required_text(row.get("task_id"), "task_id"),
            agent_run_id=_required_text(row.get("agent_run_id"), "agent_run_id"),
            finalization_epoch=_required_integer(
                row.get("finalization_epoch"),
                "finalization_epoch",
            ),
            required_page_types=required,
            observed_page_types=observed,
            missing_page_types=missing,
            unrecognized_page_types=unrecognized,
            normalization_version=_required_text(
                row.get("normalization_version"),
                "normalization_version",
            ),
            coverage_status=_required_text(
                row.get("coverage_status"),
                "coverage_status",
            ),
            snapshot_hash=_required_text(row.get("snapshot_hash"), "snapshot_hash"),
            created_at=_datetime(row.get("created_at")),
            optional_page_types=applicability["optional_page_types"],
            not_applicable_page_types=applicability[
                "not_applicable_page_types"
            ],
            applicability_mode=_required_text(
                applicability.get("applicability_mode"),
                "applicability_mode",
            ),
            catalog_version=_required_text(
                applicability.get("catalog_version"),
                "catalog_version",
            ),
            catalog_hash=_required_text(
                applicability.get("catalog_hash"),
                "catalog_hash",
            ),
            snapshot_protocol_version=applicability["snapshot_protocol_version"],
            observed_lifecycle_page_types=applicability["observed_lifecycle_page_types"],
            unknown_page_capture_ids=applicability["unknown_page_capture_ids"],
            supplemental_evidence_summary=applicability["supplemental_evidence_summary"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidWebCoverageSnapshotRowError(
            "Web Coverage Snapshot 数据库行不可变校验失败"
        ) from exc


def _json_text_tuple(value: object) -> tuple[str, ...]:
    parsed = json.loads(value) if isinstance(value, str) else value
    if not isinstance(parsed, list | tuple) or any(
        not isinstance(item, str) for item in parsed
    ):
        raise ValueError("快照页面类型字段必须为字符串 JSON 数组")
    return tuple(parsed)


def _stored_applicability(
    value: object,
    required: tuple[str, ...],
    *,
    observed: tuple[str, ...] = (),
    missing: tuple[str, ...] = (),
    unrecognized: tuple[str, ...] = (),
) -> dict[str, object]:
    payload = json.loads(value) if isinstance(value, str) else value
    if not isinstance(payload, Mapping):
        raise ValueError("applicability_snapshot 必须为完整不可变对象")
    if _json_text_tuple(payload.get("required_page_types")) != required:
        raise ValueError("applicability required 与快照顶层字段不一致")
    if payload.get("snapshot_protocol_version") not in {
        WEB_COVERAGE_SNAPSHOT_V2, WEB_COVERAGE_SNAPSHOT_V3,
        WEB_COVERAGE_SNAPSHOT_V4,
    }:
        raise ValueError("applicability snapshot_protocol_version 无效")
    protocol = payload["snapshot_protocol_version"]
    if protocol == WEB_COVERAGE_SNAPSHOT_V4:
        paired_facts = {
            "observed_page_types": observed,
            "missing_page_types": missing,
            "unrecognized_page_types": unrecognized,
        }
        for field_name, top_level_value in paired_facts.items():
            if _json_text_tuple(payload.get(field_name)) != top_level_value:
                raise ValueError(f"applicability {field_name} 与快照顶层字段不一致")
        for field_name in (
            "lifecycle_observations",
            "unknown_page_capture_ids",
            "supplemental_evidence_summary",
        ):
            if field_name not in payload:
                raise ValueError(f"applicability {field_name} 缺失")
    return {
        "snapshot_protocol_version": payload["snapshot_protocol_version"],
        "optional_page_types": _json_text_tuple(payload.get("optional_page_types")),
        "not_applicable_page_types": _json_text_tuple(
            payload.get("not_applicable_page_types")
        ),
        "applicability_mode": payload.get("applicability_mode"),
        "catalog_version": payload.get("catalog_version"),
        "catalog_hash": payload.get("catalog_hash"),
        "observed_lifecycle_page_types": (
            _json_text_tuple(payload.get("lifecycle_observations"))
            if protocol == WEB_COVERAGE_SNAPSHOT_V4 else ()
        ),
        "unknown_page_capture_ids": (
            _json_text_tuple(payload.get("unknown_page_capture_ids"))
            if protocol == WEB_COVERAGE_SNAPSHOT_V4 else ()
        ),
        "supplemental_evidence_summary": (
            _integer_summary(payload.get("supplemental_evidence_summary"))
            if protocol == WEB_COVERAGE_SNAPSHOT_V4 else {}
        ),
    }


def _integer_summary(value: object) -> dict[str, int]:
    parsed = json.loads(value) if isinstance(value, str) else value
    if not isinstance(parsed, Mapping):
        raise ValueError("supplemental_evidence_summary 必须为对象")
    result: dict[str, int] = {}
    for key, count in parsed.items():
        if (
            not isinstance(key, str)
            or not key.strip()
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count < 0
        ):
            raise ValueError("supplemental_evidence_summary 无效")
        result[key] = count
    return result


def _required_text(value: object, field_name: str) -> str:
    if value is None or isinstance(value, bool):
        raise ValueError(f"{field_name} 不能为空")
    text = str(value)
    if not text.strip():
        raise ValueError(f"{field_name} 不能为空")
    return text


def _required_integer(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} 必须为非负整数")
    return value


def _datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    raise ValueError("created_at 必须为 datetime")
