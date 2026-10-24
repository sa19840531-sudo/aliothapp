"""Web V2 完整质检覆盖快照持久化。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from services.api.app.domain.page.web_page_catalog import WEB_BUSINESS_PAGE_TYPE_CODES
from services.api.app.domain.protocol import stable_protocol_hash
from services.api.app.infrastructure.db.pool import get_postgres_pool

WEB_COVERAGE_AUTHORITY_SOURCE = "web_page_type_assessment"
WEB_COVERAGE_SCHEMA_VERSION = "web-coverage-snapshot.v4"


@dataclass(frozen=True, slots=True)
class WebCoverageSnapshot:
    """Web 有界探索的平台隔离覆盖事实。"""

    revision: int
    required_page_types: tuple[str, ...]
    covered_page_types: tuple[str, ...]
    missing_page_types: tuple[str, ...]
    scopes: tuple[dict[str, object], ...]
    assessment_scope_ready: bool
    frontier_terminal: bool
    completion_ready: bool
    payload_hash: str
    authority_source: str = WEB_COVERAGE_AUTHORITY_SOURCE
    schema_version: str = WEB_COVERAGE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.revision < 1:
            raise ValueError("Web coverage revision 必须大于 0")
        if self.authority_source != WEB_COVERAGE_AUTHORITY_SOURCE:
            raise ValueError("Web coverage authority_source 不受支持")
        if self.schema_version not in {
            "web-coverage-snapshot.v1", "web-coverage-snapshot.v2",
            "web-coverage-snapshot.v3", WEB_COVERAGE_SCHEMA_VERSION,
        }:
            raise ValueError("Web coverage schema_version 不受支持")
        values = self.to_dict()
        expected = stable_protocol_hash(
            {key: value for key, value in values.items() if key != "payload_hash"}
        )
        if self.payload_hash != expected:
            raise ValueError("Web coverage payload_hash 与内容不一致")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "revision": self.revision,
            "authority_source": self.authority_source,
            "required_page_types": list(self.required_page_types),
            "covered_page_types": list(self.covered_page_types),
            "missing_page_types": list(self.missing_page_types),
            "scopes": [dict(scope) for scope in self.scopes],
            "assessment_scope_ready": self.assessment_scope_ready,
            "frontier_terminal": self.frontier_terminal,
            "completion_ready": self.completion_ready,
            "payload_hash": self.payload_hash,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> WebCoverageSnapshot:
        raw_scopes = payload.get("scopes")
        if not isinstance(raw_scopes, list | tuple) or any(
            not isinstance(scope, dict) for scope in raw_scopes
        ):
            raise ValueError("Web coverage scopes 无效")
        return cls(
            schema_version=str(payload.get("schema_version") or ""),
            revision=int(payload.get("revision") or 0),
            authority_source=str(payload.get("authority_source") or ""),
            required_page_types=_texts(payload.get("required_page_types")),
            covered_page_types=_texts(payload.get("covered_page_types")),
            missing_page_types=_texts(payload.get("missing_page_types")),
            scopes=tuple(dict(scope) for scope in raw_scopes),
            assessment_scope_ready=payload.get("assessment_scope_ready") is True,
            frontier_terminal=payload.get("frontier_terminal") is True,
            completion_ready=payload.get("completion_ready") is True,
            payload_hash=str(payload.get("payload_hash") or ""),
        )


class WebCoverageInspection(Protocol):
    page_capture_id: str
    page_type: str | None
    page_state: str | None
    assessment_status: str


class PostgresWebCoverageSnapshotRepository:
    """把 Web 有界探索的正式 Assessment 固化为完成门禁可读快照。"""

    async def finalize(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        inspections: tuple[WebCoverageInspection, ...],
    ) -> str:
        pool = await get_postgres_pool()
        async with pool.acquire() as connection, connection.transaction():
            task_row = await connection.fetchrow(
                """
                SELECT finalization_epoch, task_config
                FROM qc_task
                WHERE id = $1::uuid
                  AND target_platform = 'web'
                  AND status IN ('queued', 'running')
                  AND finalization_state = 'open'
                FOR UPDATE
                """,
                task_id,
            )
            if task_row is None:
                raise RuntimeError("Web coverage 任务已不在可写 epoch")
            finalization_epoch = task_row["finalization_epoch"]
            snapshot = build_web_coverage_snapshot(
                inspections=inspections,
                required_page_types=_locked_required_page_types(task_row["task_config"]),
            )
            latest = await connection.fetchrow(
                """
                SELECT revision, payload_hash
                FROM qc_h5_coverage_snapshot
                WHERE task_id = $1::uuid
                  AND agent_run_id = $2::uuid
                  AND finalization_epoch = $3
                ORDER BY revision DESC
                LIMIT 1
                """,
                task_id,
                agent_run_id,
                int(finalization_epoch),
            )
            if latest is not None and latest["payload_hash"] == snapshot.payload_hash:
                revision = int(latest["revision"])
                return _snapshot_id(
                    task_id=task_id,
                    agent_run_id=agent_run_id,
                    finalization_epoch=int(finalization_epoch),
                    revision=revision,
                )
            if latest is not None:
                snapshot = _with_revision(snapshot, int(latest["revision"]) + 1)
            snapshot_id = _snapshot_id(
                task_id=task_id,
                agent_run_id=agent_run_id,
                finalization_epoch=int(finalization_epoch),
                revision=snapshot.revision,
            )
            row = await connection.fetchrow(
                """
                INSERT INTO qc_h5_coverage_snapshot (
                    coverage_snapshot_id, task_id, agent_run_id, finalization_epoch,
                    revision, authority_source, required_page_types,
                    covered_page_types, missing_page_types, coverage_scopes,
                    assessment_scope_ready, frontier_terminal, completion_ready,
                    schema_version, payload_hash
                )
                VALUES (
                    $1, $2::uuid, $3::uuid, $4, $5, $6, $7::jsonb,
                    $8::jsonb, $9::jsonb, $10::jsonb, $11, $12, $13, $14, $15
                )
                ON CONFLICT (coverage_snapshot_id) DO NOTHING
                RETURNING coverage_snapshot_id
                """,
                snapshot_id,
                task_id,
                agent_run_id,
                int(finalization_epoch),
                snapshot.revision,
                snapshot.authority_source,
                _json(snapshot.required_page_types),
                _json(snapshot.covered_page_types),
                _json(snapshot.missing_page_types),
                _json(snapshot.scopes),
                snapshot.assessment_scope_ready,
                snapshot.frontier_terminal,
                snapshot.completion_ready,
                snapshot.schema_version,
                snapshot.payload_hash,
            )
            if row is None:
                raise RuntimeError("Web coverage snapshot 幂等冲突")
            return str(row["coverage_snapshot_id"])


def build_web_coverage_snapshot(
    *,
    inspections: Sequence[WebCoverageInspection],
    required_page_types: Sequence[str] = (),
) -> WebCoverageSnapshot:
    """按任务锁定的 web 分母形成覆盖范围；未锁定时回退本轮已识别业务页。"""

    business_types = set(WEB_BUSINESS_PAGE_TYPE_CODES)
    scopes = tuple(
        {
            "scope_id": "web-scope:"
            + stable_protocol_hash(
                {
                    "page_capture_id": item.page_capture_id,
                    "page_type": item.page_type,
                    "page_state": item.page_state,
                }
            ),
            "page_capture_id": item.page_capture_id,
            "page_type": (item.page_type or "unknown").strip().casefold(),
            "page_state": (item.page_state or "unknown").strip().casefold(),
            "assessment_status": item.assessment_status,
        }
        for item in inspections
    )
    covered = tuple(
        sorted(
            {
                item.page_type.strip().casefold()
                for item in inspections
                if item.assessment_status == "covered"
                and isinstance(item.page_type, str)
                and item.page_type.strip().casefold() in business_types
            }
        )
    )
    not_applicable = {
        item.page_type.strip().casefold()
        for item in inspections
        if item.assessment_status == "not_applicable"
        and isinstance(item.page_type, str)
        and item.page_type.strip().casefold() in business_types
    }
    locked_required = tuple(
        sorted(
            {
                normalized
                for value in required_page_types
                if (normalized := str(value).strip().casefold()) in business_types
                and normalized in WEB_BUSINESS_PAGE_TYPE_CODES
            }
        )
    )
    if locked_required:
        required = locked_required
        # 缺失页型只进报告口径；完成语义仍按“已覆盖且评估收敛”判定，
        # 站点本身没有某类栏目不会卡死有界探索的收口。
        missing = tuple(sorted(set(required) - set(covered) - not_applicable))
    else:
        # 兼容未锁定分母的存量任务：保持采到啥算啥。
        required = covered
        missing = ()
    assessment_ready = bool(scopes) and all(
        item.assessment_status in {"covered", "not_applicable"}
        for item in inspections
    )
    values: dict[str, Any] = {
        "revision": 1,
        "required_page_types": required,
        "covered_page_types": covered,
        "missing_page_types": missing,
        "scopes": scopes,
        "assessment_scope_ready": assessment_ready,
        "frontier_terminal": True,
        "completion_ready": bool(covered) and assessment_ready,
        "authority_source": WEB_COVERAGE_AUTHORITY_SOURCE,
        "schema_version": WEB_COVERAGE_SCHEMA_VERSION,
    }
    values["payload_hash"] = stable_protocol_hash(values)
    return WebCoverageSnapshot(**values)


def _locked_required_page_types(task_config: object) -> tuple[str, ...]:
    """从任务锁定配置提取 web 分母；缺失或格式异常时视为未锁定。"""

    config = task_config
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except json.JSONDecodeError:
            return ()
    if not isinstance(config, dict):
        return ()
    target = config.get("web_exploration_target")
    if not isinstance(target, dict):
        return ()
    values = target.get("required_page_types")
    if not isinstance(values, list | tuple):
        return ()
    return tuple(str(item) for item in values)


def _with_revision(snapshot: WebCoverageSnapshot, revision: int) -> WebCoverageSnapshot:
    values = snapshot.to_dict()
    values["revision"] = revision
    values["payload_hash"] = stable_protocol_hash(
        {key: value for key, value in values.items() if key != "payload_hash"}
    )
    return WebCoverageSnapshot.from_dict(values)


def _snapshot_id(
    *,
    task_id: str,
    agent_run_id: str,
    finalization_epoch: int,
    revision: int,
) -> str:
    return "web-coverage:" + stable_protocol_hash(
        {
            "task_id": task_id,
            "agent_run_id": agent_run_id,
            "finalization_epoch": finalization_epoch,
            "revision": revision,
        }
    )


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=list)


def _texts(value: object) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        raise ValueError("Web coverage 页面类型集合无效")
    return tuple(str(item) for item in value)
