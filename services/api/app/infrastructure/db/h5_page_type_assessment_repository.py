"""H5 页面类型评估的 PostgreSQL 幂等持久化。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Any
from uuid import UUID

from services.api.app.domain.exploration.page_type_assessment import (
    build_page_type_assessment_id,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool


class H5PageTypeAssessmentPersistenceError(RuntimeError):
    """页面评估无法以不降级的方式持久化。"""


@dataclass(frozen=True, kw_only=True)
class H5PageTypeAssessmentRecord:
    """H5 页面评估事实的只读投影。"""

    assessment_record_id: str
    assessment_id: str
    task_id: str
    agent_run_id: str
    session_id: str
    page_capture_id: str
    detection_status: str
    page_type: str
    page_state: str | None
    status: str
    reason_codes: tuple[str, ...]
    safety_summary: dict[str, object]
    assessment_payload: dict[str, object]
    schema_version: str
    payload_hash: str
    row_version: int


class H5PageTypeAssessmentRepository:
    """以 Capture 与 schema 版本保障 H5 评估重放的审计稳定性。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    @staticmethod
    def build_payload_hash(payload: Mapping[str, object]) -> str:
        """按规范 JSON 对评估事实做稳定哈希，避免键顺序影响幂等。"""
        normalized = _json_dumps(_canonical_json(payload))
        return sha256(normalized.encode("utf-8")).hexdigest()

    async def upsert(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        session_id: str,
        page_capture_id: str,
        detection_result: Mapping[str, object],
        assessment_payload: Mapping[str, object],
        expected_row_version: int | None = None,
    ) -> H5PageTypeAssessmentRecord:
        """写入评估；冲突、篡改或较弱版本一律拒绝而非覆盖。"""
        context = _normalize_context(
            task_id=task_id,
            agent_run_id=agent_run_id,
            session_id=session_id,
            page_capture_id=page_capture_id,
        )
        _validate_expected_row_version(expected_row_version)
        detection = _normalize_detection(
            detection_result,
            task_id=context["task_id"],
            agent_run_id=context["agent_run_id"],
            page_capture_id=context["page_capture_id"],
        )
        payload = _normalize_payload(
            assessment_payload,
            task_id=context["task_id"],
            agent_run_id=context["agent_run_id"],
            page_capture_id=context["page_capture_id"],
            detection_result=detection,
        )
        payload_hash = self.build_payload_hash(
            {
                "task_id": context["task_id"],
                "agent_run_id": context["agent_run_id"],
                "session_id": context["session_id"],
                "page_capture_id": context["page_capture_id"],
                "detection": detection,
                "assessment": payload,
            }
        )
        record_id = _stable_record_id(
            task_id=context["task_id"],
            agent_run_id=context["agent_run_id"],
            session_id=context["session_id"],
            page_capture_id=context["page_capture_id"],
            schema_version=payload["schema_version"],
        )
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                _UPSERT_SQL,
                record_id,
                payload["assessment_id"],
                context["task_id"],
                context["agent_run_id"],
                context["session_id"],
                context["page_capture_id"],
                detection["detection_status"],
                payload["page_type"],
                detection.get("page_state"),
                payload["status"],
                _json_dumps(payload["reason_codes"]),
                _json_dumps(payload["safety_summary"]),
                _json_dumps(payload["assessment_payload"]),
                payload["schema_version"],
                payload_hash,
                expected_row_version,
            )
        if row is None:
            raise H5PageTypeAssessmentPersistenceError(
                "H5 页面评估发生冲突、身份不匹配或较弱终态降级，已拒绝写入"
            )
        record = _record_from_row(row)
        if (
            record.task_id != context["task_id"]
            or record.agent_run_id != context["agent_run_id"]
            or record.session_id != context["session_id"]
            or record.page_capture_id != context["page_capture_id"]
        ):
            raise H5PageTypeAssessmentPersistenceError("H5 页面评估返回了不一致的归属")
        return record

    async def list_for_run(
        self,
        *,
        task_id: str,
        agent_run_id: str,
    ) -> list[H5PageTypeAssessmentRecord]:
        """保留全量版本，仅投影每个 Capture 当前最强且最新的有效评估。"""
        task_id = _require_uuid(_require_text(task_id, "task_id"), "task_id")
        agent_run_id = _require_uuid(
            _require_text(agent_run_id, "agent_run_id"),
            "agent_run_id",
        )
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(_LIST_FOR_RUN_SQL, task_id, agent_run_id)
        return [_record_from_row(row) for row in rows]

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


_UPSERT_SQL = """
INSERT INTO qc_h5_page_type_assessment AS existing (
    assessment_record_id, assessment_id, task_id, agent_run_id, session_id,
    page_capture_id, detection_status, page_type, page_state, status,
    reason_codes, safety_summary, assessment_payload, schema_version, payload_hash
)
VALUES (
    $1, $2, $3::uuid, $4::uuid, $5, $6::uuid, $7, $8, $9, $10,
    $11::jsonb, $12::jsonb, $13::jsonb, $14, $15
)
ON CONFLICT (page_capture_id, schema_version) DO UPDATE
SET assessment_id = EXCLUDED.assessment_id,
    detection_status = EXCLUDED.detection_status,
    page_type = EXCLUDED.page_type,
    page_state = EXCLUDED.page_state,
    status = EXCLUDED.status,
    reason_codes = EXCLUDED.reason_codes,
    safety_summary = EXCLUDED.safety_summary,
    assessment_payload = EXCLUDED.assessment_payload,
    payload_hash = EXCLUDED.payload_hash,
    row_version = CASE
        WHEN existing.payload_hash = EXCLUDED.payload_hash THEN existing.row_version
        ELSE existing.row_version + 1
    END,
    updated_at = CASE
        WHEN existing.payload_hash = EXCLUDED.payload_hash THEN existing.updated_at
        ELSE now()
    END
WHERE existing.task_id = EXCLUDED.task_id
  AND existing.agent_run_id = EXCLUDED.agent_run_id
  AND existing.session_id = EXCLUDED.session_id
  AND (
      existing.payload_hash = EXCLUDED.payload_hash
      OR (
          $16::bigint IS NOT NULL
          AND existing.row_version = $16::bigint
          AND (
              existing.status = EXCLUDED.status
              OR existing.status = 'evidence_insufficient'
              OR (
                  existing.status = 'not_found'
                  AND EXCLUDED.status IN (
                      'covered', 'requires_auth', 'blocked_by_safety', 'not_applicable'
                  )
              )
          )
      )
  )
RETURNING
    existing.assessment_record_id,
    existing.assessment_id,
    existing.task_id::text AS task_id,
    existing.agent_run_id::text AS agent_run_id,
    existing.session_id,
    existing.page_capture_id::text AS page_capture_id,
    existing.detection_status,
    existing.page_type,
    existing.page_state,
    existing.status,
    existing.reason_codes,
    existing.safety_summary,
    existing.assessment_payload,
    existing.schema_version,
    existing.payload_hash,
    existing.row_version
"""

_LIST_FOR_RUN_SQL = """
WITH ranked_assessments AS (
SELECT
    assessment_record_id,
    assessment_id,
    task_id::text AS task_id,
    agent_run_id::text AS agent_run_id,
    session_id,
    page_capture_id::text AS page_capture_id,
    detection_status,
    page_type,
    page_state,
    status,
    reason_codes,
    safety_summary,
    assessment_payload,
    schema_version,
    payload_hash,
    row_version,
    ROW_NUMBER() OVER (
        PARTITION BY page_capture_id
        ORDER BY
            CASE status
                WHEN 'covered' THEN 3
                WHEN 'requires_auth' THEN 3
                WHEN 'blocked_by_safety' THEN 3
                WHEN 'not_applicable' THEN 3
                WHEN 'not_found' THEN 2
                ELSE 1
            END DESC,
            COALESCE(
                (regexp_match(schema_version, '\\.v([0-9]+)$'))[1]::integer,
                0
            ) DESC,
            updated_at DESC,
            assessment_record_id DESC
    ) AS assessment_rank
FROM qc_h5_page_type_assessment
WHERE task_id = $1::uuid
  AND agent_run_id = $2::uuid
)
SELECT
    assessment_record_id, assessment_id, task_id, agent_run_id, session_id,
    page_capture_id, detection_status, page_type, page_state, status,
    reason_codes, safety_summary, assessment_payload, schema_version,
    payload_hash, row_version
FROM ranked_assessments
WHERE assessment_rank = 1
ORDER BY page_capture_id, assessment_record_id
"""


def _normalize_context(**values: str) -> dict[str, str]:
    normalized = {name: _require_text(value, name) for name, value in values.items()}
    for field_name in ("task_id", "agent_run_id", "page_capture_id"):
        normalized[field_name] = _require_uuid(normalized[field_name], field_name)
    return normalized


def _normalize_detection(
    value: Mapping[str, object],
    *,
    task_id: str,
    agent_run_id: str,
    page_capture_id: str,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError("detection_result 必须为对象")
    normalized = _canonical_json(value)
    if not isinstance(normalized, dict):
        raise TypeError("detection_result 必须为 JSON 对象")
    _validate_available_identity(
        normalized,
        task_id=task_id,
        agent_run_id=agent_run_id,
        page_capture_id=page_capture_id,
        field_prefix="detection_result",
        require_capture_array=False,
    )
    normalized["detection_status"] = _require_text(
        normalized.get("detection_status"),
        "detection_result.detection_status",
    )
    page_state = _optional_text(normalized.get("page_state"))
    if page_state is None:
        normalized.pop("page_state", None)
    else:
        normalized["page_state"] = page_state
    return normalized


def _normalize_payload(
    value: Mapping[str, object],
    *,
    task_id: str,
    agent_run_id: str,
    page_capture_id: str,
    detection_result: Mapping[str, object],
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError("assessment_payload 必须为对象")
    adapter_result = _canonical_json(value)
    if not isinstance(adapter_result, dict):
        raise TypeError("assessment_payload 必须为 JSON 对象")
    execution_status = _optional_text(adapter_result.get("execution_status"))
    if execution_status is None:
        raw = adapter_result
        execution_status = "accepted"
    else:
        if execution_status not in {"accepted", "blocked", "skipped"}:
            raise ValueError("assessment_payload.execution_status 不受支持")
        inner = adapter_result.get("page_type_assessment")
        if execution_status == "accepted":
            if not isinstance(inner, Mapping):
                raise TypeError("accepted assessment 缺少 page_type_assessment 对象")
            raw = dict(inner)
        else:
            raw = _nonaccepted_payload(
                adapter_result=adapter_result,
                detection_result=detection_result,
                execution_status=execution_status,
            )
    _validate_available_identity(
        adapter_result,
        task_id=task_id,
        agent_run_id=agent_run_id,
        page_capture_id=page_capture_id,
        field_prefix="assessment_payload",
        require_capture_array=False,
    )
    _validate_nested_identities(
        adapter_result,
        task_id=task_id,
        agent_run_id=agent_run_id,
        page_capture_id=page_capture_id,
        field_path="assessment_payload",
    )
    _validate_inner_identity(
        raw,
        task_id=task_id,
        agent_run_id=agent_run_id,
        page_capture_id=page_capture_id,
        execution_status=execution_status,
    )
    status = _require_text(raw.get("status"), "assessment_payload.status")
    if status not in _ASSESSMENT_STATUSES:
        raise ValueError("assessment_payload.status 不属于 H5 页面评估状态")
    page_type = _require_text(raw.get("page_type"), "assessment_payload.page_type")
    schema_version = _optional_text(raw.get("schema_version")) or "h5-page-assessment.v1"
    if not _SCHEMA_VERSION_RE.fullmatch(schema_version):
        raise ValueError("assessment_payload.schema_version 必须为 h5-page-assessment.vN")
    reason_codes = _reason_codes(raw)
    safety_summary = raw.get("safety_summary", {})
    if not isinstance(safety_summary, Mapping):
        raise TypeError("assessment_payload.safety_summary 必须为对象")
    catalog_hash = _validated_catalog_hash(raw, detection_result=detection_result)
    assessment_id = _derived_assessment_id(
        task_id=task_id,
        agent_run_id=agent_run_id,
        page_type=page_type,
        catalog_hash=catalog_hash,
    )
    payload = dict(raw)
    payload["schema_version"] = schema_version
    payload["assessment_id"] = assessment_id
    if catalog_hash is not None:
        payload["catalog_sha256"] = catalog_hash
    payload["reason_codes"] = list(reason_codes)
    payload["safety_summary"] = dict(safety_summary)
    payload["detection_result"] = _canonical_json(detection_result)
    payload["adapter_result"] = _normalized_adapter_result(
        adapter_result,
        execution_status=execution_status,
        assessment_id=assessment_id,
        catalog_hash=catalog_hash,
    )
    return {
        "assessment_id": assessment_id,
        "page_type": page_type,
        "status": status,
        "reason_codes": reason_codes,
        "safety_summary": dict(safety_summary),
        "assessment_payload": payload,
        "schema_version": schema_version,
    }


def _reason_codes(payload: Mapping[str, object]) -> tuple[str, ...]:
    raw = payload.get("reason_codes")
    if raw is None:
        raw = [payload.get("reason_code")] if payload.get("reason_code") is not None else []
    if not isinstance(raw, list | tuple):
        raise TypeError("assessment_payload.reason_codes 必须为数组")
    return tuple(_require_text(item, "assessment_payload.reason_codes") for item in raw)


def _record_from_row(row: Any) -> H5PageTypeAssessmentRecord:
    return H5PageTypeAssessmentRecord(
        assessment_record_id=str(row["assessment_record_id"]),
        assessment_id=str(row["assessment_id"]),
        task_id=str(row["task_id"]),
        agent_run_id=str(row["agent_run_id"]),
        session_id=str(row["session_id"]),
        page_capture_id=str(row["page_capture_id"]),
        detection_status=str(row["detection_status"]),
        page_type=str(row["page_type"]),
        page_state=_optional_text(row["page_state"]),
        status=str(row["status"]),
        reason_codes=tuple(_json_value(row["reason_codes"])),
        safety_summary=dict(_json_value(row["safety_summary"])),
        assessment_payload=dict(_json_value(row["assessment_payload"])),
        schema_version=str(row["schema_version"]),
        payload_hash=str(row["payload_hash"]),
        row_version=int(row["row_version"]),
    )


def _stable_record_id(**values: str) -> str:
    return "h5-assessment:" + _stable_digest(values)


def _stable_assessment_id(*, task_id: str, agent_run_id: str, page_type: str) -> str:
    return "h5-assessment-payload:" + _stable_digest(
        {"task_id": task_id, "agent_run_id": agent_run_id, "page_type": page_type}
    )


def _stable_digest(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()


def _json_dumps(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("payload 必须是有限值组成的 JSON 结构") from exc


def _json_value(value: object) -> object:
    return json.loads(value) if isinstance(value, str) else value


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} 必须为无首尾空白的非空字符串")
    return value


def _require_uuid(value: str, field_name: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{field_name} 必须为有效 UUID") from exc


def _canonical_json(value: object) -> object:
    """递归复制 JSON，并剔除所有调用方提供的 hash/record identity。"""
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("payload JSON 对象键必须为字符串")
        return {
            key: _canonical_json(item)
            for key, item in value.items()
            if key not in {"payload_hash", "assessment_record_id", "assessment_id"}
        }
    if isinstance(value, list | tuple):
        return [_canonical_json(item) for item in value]
    if value is None or isinstance(value, str | int | float | bool):
        _json_dumps(value)
        return value
    raise TypeError("payload 必须为 JSON 兼容结构")


def _validate_inner_identity(
    payload: Mapping[str, object],
    *,
    task_id: str,
    agent_run_id: str,
    page_capture_id: str,
    execution_status: str,
) -> None:
    _validate_available_identity(
        payload,
        task_id=task_id,
        agent_run_id=agent_run_id,
        page_capture_id=page_capture_id,
        field_prefix="assessment_payload",
        require_capture_array=execution_status == "accepted",
    )
    raw_capture_ids = payload.get("page_capture_ids")
    if execution_status == "accepted":
        if not isinstance(raw_capture_ids, list | tuple):
            raise TypeError("assessment_payload.page_capture_ids 必须为单元素数组")
        capture_ids = tuple(
            _require_uuid(
                _require_text(item, "assessment_payload.page_capture_ids"),
                "assessment_payload.page_capture_ids",
            )
            for item in raw_capture_ids
        )
        if capture_ids != (page_capture_id,):
            raise ValueError("assessment_payload.page_capture_ids 必须等于 page_capture_id")


def _validate_available_identity(
    payload: Mapping[str, object],
    *,
    task_id: str,
    agent_run_id: str,
    page_capture_id: str,
    field_prefix: str,
    require_capture_array: bool,
) -> None:
    for field_name, expected in (
        ("task_id", task_id),
        ("agent_run_id", agent_run_id),
        ("page_capture_id", page_capture_id),
    ):
        supplied = payload.get(field_name)
        if supplied is None:
            continue
        normalized = _require_uuid(
            _require_text(supplied, f"{field_prefix}.{field_name}"),
            f"{field_prefix}.{field_name}",
        )
        if normalized != expected:
            raise ValueError(f"{field_prefix}.{field_name} 与持久化上下文不一致")
    if require_capture_array and "page_capture_ids" not in payload:
        raise TypeError("assessment_payload.page_capture_ids 必须为单元素数组")


def _validate_nested_identities(
    value: object,
    *,
    task_id: str,
    agent_run_id: str,
    page_capture_id: str,
    field_path: str,
) -> None:
    if isinstance(value, Mapping):
        for field_name, expected in (
            ("task_id", task_id),
            ("agent_run_id", agent_run_id),
            ("page_capture_id", page_capture_id),
        ):
            supplied = value.get(field_name)
            if supplied is None:
                continue
            normalized = _require_uuid(
                _require_text(supplied, f"{field_path}.{field_name}"),
                f"{field_path}.{field_name}",
            )
            if normalized != expected:
                raise ValueError(f"{field_path}.{field_name} 与持久化上下文不一致")
        supplied_capture_ids = value.get("page_capture_ids")
        if supplied_capture_ids is not None:
            if not isinstance(supplied_capture_ids, list | tuple):
                raise TypeError(f"{field_path}.page_capture_ids 必须为单元素数组")
            normalized_capture_ids = tuple(
                _require_uuid(
                    _require_text(item, f"{field_path}.page_capture_ids"),
                    f"{field_path}.page_capture_ids",
                )
                for item in supplied_capture_ids
            )
            if normalized_capture_ids != (page_capture_id,):
                raise ValueError(f"{field_path}.page_capture_ids 与持久化上下文不一致")
        for key, item in value.items():
            _validate_nested_identities(
                item,
                task_id=task_id,
                agent_run_id=agent_run_id,
                page_capture_id=page_capture_id,
                field_path=f"{field_path}.{key}",
            )
        return
    if isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _validate_nested_identities(
                item,
                task_id=task_id,
                agent_run_id=agent_run_id,
                page_capture_id=page_capture_id,
                field_path=f"{field_path}[{index}]",
            )


def _derived_assessment_id(
    *,
    task_id: str,
    agent_run_id: str,
    page_type: str,
    catalog_hash: str | None,
) -> str:
    if catalog_hash is not None:
        return build_page_type_assessment_id(
            task_id=task_id,
            agent_run_id=agent_run_id,
            page_type=page_type,
            catalog_sha256=catalog_hash,
        )
    return _stable_assessment_id(
        task_id=task_id,
        agent_run_id=agent_run_id,
        page_type=page_type,
    )


def _validated_catalog_hash(
    payload: Mapping[str, object],
    *,
    detection_result: Mapping[str, object],
) -> str | None:
    payload_catalog_hash = _optional_text(payload.get("catalog_sha256"))
    detection_catalog_hash = _optional_text(detection_result.get("catalog_sha256"))
    if (
        payload_catalog_hash is not None
        and detection_catalog_hash is not None
        and payload_catalog_hash != detection_catalog_hash
    ):
        raise ValueError("assessment_payload.catalog_sha256 与 detection_result 不一致")
    return payload_catalog_hash or detection_catalog_hash


def _normalized_adapter_result(
    adapter_result: Mapping[str, object],
    *,
    execution_status: str,
    assessment_id: str,
    catalog_hash: str | None,
) -> dict[str, object]:
    normalized = dict(adapter_result)
    inner = normalized.get("page_type_assessment")
    target = dict(inner) if isinstance(inner, Mapping) else normalized
    target["assessment_id"] = assessment_id
    if catalog_hash is not None:
        target["catalog_sha256"] = catalog_hash
    if isinstance(inner, Mapping):
        normalized["page_type_assessment"] = target
    elif execution_status != "accepted":
        normalized.pop("assessment_id", None)
    return normalized


def _nonaccepted_payload(
    *,
    adapter_result: Mapping[str, object],
    detection_result: Mapping[str, object],
    execution_status: str,
) -> dict[str, object]:
    page_type = _optional_text(detection_result.get("page_type")) or "unknown"
    if execution_status == "blocked":
        reason_code = _optional_text(adapter_result.get("error_code")) or (
            "H5_PAGE_TYPE_ASSESSMENT_BLOCKED"
        )
        status = "evidence_insufficient"
    else:
        reason_code = "H5_PAGE_TYPE_ASSESSMENT_SKIPPED"
        status = "not_applicable"
    return {
        "schema_version": "h5-page-assessment.v1",
        "page_type": page_type,
        "status": status,
        "reason_codes": [reason_code],
        "safety_summary": {},
    }


def _validate_expected_row_version(value: int | None) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("expected_row_version 必须为非负整数或 None")


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() and value == value.strip() else None


_ASSESSMENT_STATUSES = frozenset(
    {
        "covered",
        "requires_auth",
        "blocked_by_safety",
        "not_found",
        "evidence_insufficient",
        "not_applicable",
    }
)


_SCHEMA_VERSION_RE = re.compile(r"h5-page-assessment\.v[1-9][0-9]*")
