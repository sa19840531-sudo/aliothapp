"""不可变 Web Coverage Snapshot 的 PostgreSQL 仓储。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from services.api.app.agents.tools.rule_tools import (
    _rule_matches_required_page_state,
)
from services.api.app.application.rule_auth_gate import AUTH_REQUIREMENT_AUTHENTICATED
from services.api.app.application.web_page_applicability import (
    WebPageApplicabilitySnapshot,
    finalize_web_page_applicability,
)
from services.api.app.domain.exploration.web_coverage_snapshot import (
    WEB_COVERAGE_SNAPSHOT_V1,
    WEB_COVERAGE_SNAPSHOT_V3,
    WEB_COVERAGE_SNAPSHOT_V4,
    WEB_PAGE_TYPE_NORMALIZATION_VERSION,
    WebCoverageSnapshot,
)
from services.api.app.domain.page.web_page_catalog import (
    WEB_BUSINESS_PAGE_TYPE_CODES,
    WEB_LEGACY_PAGE_TYPE_CODES,
    WEB_V3_BUSINESS_PAGE_TYPE_CODES,
)
from services.api.app.domain.rule.url_scope import candidate_rule_matches_page_url
from services.api.app.domain.task.inspection_scope import inspection_scope_page_types
from services.api.app.infrastructure.db.finalization_recovery_fence import (
    assert_finalization_recovery_fence,
)
from services.api.app.infrastructure.db.inspection_scope_sql import (
    page_type_in_inspection_scope_sql,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool
from services.api.app.infrastructure.db.web_classification_reuse import web_classification_reuse_cte
from services.api.app.infrastructure.db.web_gap_rule_sql import (
    WEB_GUEST_GAP_RULE_APPLICABILITY_SQL,
)

_REPLAY_FINALIZATION_STATES = frozenset(
    {"exploration_draining", "session_draining", "finalizing", "sealed"}
)
_FIRST_CREATE_FINALIZATION_STATES = frozenset({"exploration_draining", "session_draining"})
_TASK_STATUSES_BY_FINALIZATION_STATE = {
    "exploration_draining": frozenset({"running"}),
    "session_draining": frozenset({"running", "review"}),
    "finalizing": frozenset({"running", "review"}),
    "sealed": frozenset({"done", "failed", "review"}),
}
_CANCELED_STATUSES = frozenset({"canceled", "cancelled"})
_FOUNDATIONAL_FAILURE_RUN_STATUSES = frozenset({"failed", "aborted"})


class WebCoverageSnapshotPersistenceError(RuntimeError):
    """快照身份、终态或数据库写入不符合持久化合同。"""


class WebCoverageSnapshotConflictError(WebCoverageSnapshotPersistenceError):
    """同一 task/run/epoch 已冻结不同覆盖事实。"""


class PostgresWebCoverageSnapshotRepository:
    """在锁定当前 Web Task 与 main Run 后创建一次不可变快照。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def seal_completed_budget(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        finalization_epoch: int,
        recovery_lease_token: str | None = None,
        recovery_generation: int | None = None,
        recovery_lease_expires_at: datetime | None = None,
    ) -> bool:
        """恢复 Run 已完成但进程未及封存的完整 Web 菜单预算。"""

        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await _assert_optional_recovery_fence(
                connection,
                task_id=task_id,
                recovery_lease_token=recovery_lease_token,
                recovery_generation=recovery_generation,
                recovery_lease_expires_at=recovery_lease_expires_at,
            )
            row = await connection.fetchrow(
                """
                WITH eligible AS (
                    SELECT budget.agent_run_id, budget.site_id
                    FROM qc_web_exploration_budget AS budget
                    INNER JOIN qc_task AS task
                        ON task.id = budget.task_id
                    INNER JOIN qc_agent_run AS run
                        ON run.id = budget.agent_run_id
                       AND run.task_id = budget.task_id
                    INNER JOIN qc_surface_session AS session
                        ON session.task_id = budget.task_id
                       AND session.session_id = budget.session_id
                    WHERE budget.task_id = $1::uuid
                      AND budget.agent_run_id = $2::uuid
                      AND budget.state = 'open'
                      AND budget.surface_kind = 'web'
                      AND budget.session_owner = 'web-v2-workflow'
                      AND task.status = 'running'
                      AND task.finalization_state = 'open'
                      AND task.finalization_epoch = $3
                      AND task.finalization_agent_run_id = budget.agent_run_id
                      AND run.agent_type = 'main'
                      AND run.status = 'done'
                      AND session.surface_kind = 'web'
                      AND session.session_owner = 'web-v2-workflow'
                      AND session.status IN ('active', 'draining')
                      AND EXISTS (
                          SELECT 1
                          FROM qc_web_menu_ledger_entry AS present
                          WHERE present.agent_run_id = budget.agent_run_id
                            AND present.site_id = budget.site_id
                      )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM qc_web_menu_ledger_entry AS entry
                          WHERE entry.agent_run_id = budget.agent_run_id
                            AND entry.site_id = budget.site_id
                            AND entry.status IN (
                                'pending', 'home_pending',
                                'menu_reserved', 'detail_reserved'
                            )
                      )
                    FOR UPDATE OF budget
                )
                UPDATE qc_web_exploration_budget AS budget
                SET state = 'sealed',
                    sealed_at = COALESCE(budget.sealed_at, now()),
                    row_version = budget.row_version + 1,
                    updated_at = now()
                FROM eligible
                WHERE budget.agent_run_id = eligible.agent_run_id
                  AND budget.site_id = eligible.site_id
                RETURNING budget.agent_run_id
                """,
                task_id,
                agent_run_id,
                finalization_epoch,
            )
        return row is not None

    async def create(
        self,
        snapshot: WebCoverageSnapshot,
        *,
        recovery_lease_token: str | None = None,
        recovery_generation: int | None = None,
        recovery_lease_expires_at: datetime | None = None,
    ) -> WebCoverageSnapshot:
        if not isinstance(snapshot, WebCoverageSnapshot):
            raise TypeError("snapshot 必须为 WebCoverageSnapshot")
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await _assert_optional_recovery_fence(
                connection,
                task_id=snapshot.task_id,
                recovery_lease_token=recovery_lease_token,
                recovery_generation=recovery_generation,
                recovery_lease_expires_at=recovery_lease_expires_at,
            )
            identity = await connection.fetchrow(
                """
                SELECT
                    task.id::text AS task_id,
                    task.target_platform,
                    task.status AS task_status,
                    task.finalization_state,
                    task.finalization_epoch,
                    task.finalization_agent_run_id::text AS finalization_agent_run_id,
                    agent_run.id::text AS agent_run_id,
                    agent_run.task_id::text AS agent_run_task_id,
                    agent_run.agent_type,
                    agent_run.status AS agent_run_status,
                    agent_run.error_message AS agent_run_error_message,
                    current_main.id = agent_run.id AS is_latest_main_run
                FROM qc_task AS task
                JOIN qc_agent_run AS agent_run
                  ON agent_run.id = $2::uuid
                LEFT JOIN LATERAL (
                    SELECT latest_main.id
                    FROM qc_agent_run AS latest_main
                    WHERE latest_main.task_id = task.id
                      AND latest_main.agent_type = 'main'
                    ORDER BY latest_main.created_at DESC, latest_main.id DESC
                    LIMIT 1
                ) AS current_main ON TRUE
                WHERE task.id = $1::uuid
                FOR UPDATE OF task, agent_run
                """,
                snapshot.task_id,
                snapshot.agent_run_id,
            )
            _validate_current_identity(identity, snapshot)
            existing = await _fetch_snapshot(
                connection,
                task_id=snapshot.task_id,
                agent_run_id=snapshot.agent_run_id,
                finalization_epoch=snapshot.finalization_epoch,
            )
            if existing is not None:
                return _resolve_replay(existing, snapshot)
            _validate_first_creation(identity)
            row = await connection.fetchrow(
                """
                INSERT INTO qc_web_coverage_snapshot (
                    id, task_id, agent_run_id, finalization_epoch,
                    required_page_types, observed_page_types, missing_page_types,
                    unrecognized_page_types, normalization_version,
                    coverage_status, snapshot_hash, created_at,
                    applicability_snapshot
                )
                VALUES (
                    $1::uuid, $2::uuid, $3::uuid, $4,
                    $5::jsonb, $6::jsonb, $7::jsonb,
                    $8::jsonb, $9, $10, $11, $12, $13::jsonb
                )
                ON CONFLICT (task_id, agent_run_id, finalization_epoch) DO NOTHING
                RETURNING *
                """,
                snapshot.snapshot_id,
                snapshot.task_id,
                snapshot.agent_run_id,
                snapshot.finalization_epoch,
                _json_dumps(snapshot.required_page_types),
                _json_dumps(snapshot.observed_page_types),
                _json_dumps(snapshot.missing_page_types),
                _json_dumps(snapshot.unrecognized_page_types),
                snapshot.normalization_version,
                snapshot.coverage_status,
                snapshot.snapshot_hash,
                snapshot.created_at,
                _json_dumps(_applicability_payload(snapshot)),
            )
            if row is None:
                row = await _fetch_snapshot(
                    connection,
                    task_id=snapshot.task_id,
                    agent_run_id=snapshot.agent_run_id,
                    finalization_epoch=snapshot.finalization_epoch,
                )
                if row is None:
                    raise WebCoverageSnapshotPersistenceError(
                        "Web Coverage Snapshot 并发写入后不可读取"
                    )
                return _resolve_replay(row, snapshot)
            return _parse_snapshot(row)

    async def load_reference(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        finalization_epoch: int,
    ) -> tuple[str, str] | None:
        """只读返回不可变快照身份，供历史协议终态恢复复用。"""

        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT id::text AS snapshot_id, snapshot_hash
                FROM qc_web_coverage_snapshot
                WHERE task_id = $1::uuid
                  AND agent_run_id = $2::uuid
                  AND finalization_epoch = $3
                """,
                task_id,
                agent_run_id,
                finalization_epoch,
            )
        if row is None:
            return None
        return str(row["snapshot_id"]), str(row["snapshot_hash"])

    async def load_reconciliation_state(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        finalization_epoch: int,
        recovery_lease_token: str | None = None,
        recovery_generation: int | None = None,
        recovery_lease_expires_at: datetime | None = None,
    ) -> object | None:
        """一次读取 Web 收口所需的持久化事实，不接受内存补值。"""

        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                _RECONCILIATION_STATE_SQL,
                task_id,
                agent_run_id,
                finalization_epoch,
                recovery_lease_token,
                recovery_generation,
                recovery_lease_expires_at,
            )
        if row is None:
            return None
        from services.api.app.application.web_inspection_reconciliation import (
            WebInspectionReconciliationState,
        )

        frontier_count = int(row.get("frontier_work_count", 0) or 0)
        pending_capture_count = int(row.get("pending_capture_commit_count", 0) or 0)
        foundational_terminal = (
            str(row.get("run_status") or "") in _FOUNDATIONAL_FAILURE_RUN_STATUSES
            and row.get("foundational_failure_code") is not None
        )
        applicability = _task_applicability(
            row.get("task_config"), frozen_policy=row.get("frozen_run_policy")
        )
        frozen_payload = _json_object(row.get("frozen_coverage_snapshot"))
        frozen_snapshot = (
            parse_web_coverage_snapshot_row(frozen_payload) if frozen_payload else None
        )
        inspection_scope = inspection_scope_page_types(_json_object(row.get("task_config")))
        # 选了质检范围时经过的页面只留认路记录，不算页面覆盖
        observed_page_types = tuple(
            page_type
            for page_type in _sorted_text_tuple(row.get("observed_page_types"))
            if not inspection_scope or page_type in inspection_scope
        )
        if frozen_snapshot is not None:
            applicability = _applicability_from_frozen_snapshot(frozen_snapshot)
        elif applicability["applicability_mode"] == "runtime_dynamic":
            applicability = _finalize_runtime_applicability(
                applicability,
                applicable_page_types=_sorted_text_tuple(row.get("runtime_applicable_page_types")),
                covered_page_types=observed_page_types,
                inspection_scope=inspection_scope,
            )
        return WebInspectionReconciliationState(
            task_id=str(row["task_id"]),
            agent_run_id=str(row["agent_run_id"]),
            finalization_epoch=int(row["finalization_epoch"]),
            task_status=str(row["task_status"]),
            task_finalization_state=str(row["finalization_state"]),
            task_row_version=int(row["task_row_version"]),
            run_status=str(row["run_status"]),
            current_main_run=(
                bool(row["current_main_run"])
                and str(row.get("finalization_agent_run_id") or "") == str(row["agent_run_id"])
            ),
            execution_mode=str(row.get("execution_mode") or ""),
            notification_mode=str(row.get("notification_mode") or ""),
            exploration_terminal=(
                (str(row.get("budget_state") or "") == "sealed" or foundational_terminal)
                and frontier_count == 0
                and pending_capture_count == 0
            ),
            frontier_work_count=frontier_count,
            pending_capture_commit_count=pending_capture_count,
            pending_manifest_count=int(row.get("pending_manifest_count", 0) or 0),
            missing_assessment_count=int(row.get("missing_assessment_count", 0) or 0),
            review_assessment_count=int(row.get("review_assessment_count", 0) or 0),
            missing_publication_count=int(row.get("missing_publication_count", 0) or 0),
            failed_manifest_count=int(row.get("failed_manifest_count", 0) or 0),
            review_publication_count=int(row.get("review_publication_count", 0) or 0),
            open_review_count=int(row.get("open_review_count", 0) or 0),
            failed_publication_count=int(row.get("failed_publication_count", 0) or 0),
            nonterminal_publication_count=int(row.get("nonterminal_publication_count", 0) or 0),
            nonterminal_work_item_count=int(row.get("nonterminal_work_item_count", 0) or 0),
            active_session_request_count=int(row.get("active_session_request_count", 0) or 0),
            observed_page_types=observed_page_types,
            unrecognized_page_types=_sorted_text_tuple(row.get("unrecognized_page_types")),
            observed_lifecycle_page_types=_sorted_text_tuple(
                row.get("observed_lifecycle_page_types")
            ),
            unknown_page_capture_ids=_sorted_identifier_tuple(row.get("unknown_page_capture_ids")),
            supplemental_evidence_summary=_integer_summary(
                row.get("supplemental_evidence_summary")
            ),
            foundational_failure_code=(
                str(row["foundational_failure_code"])
                if row.get("foundational_failure_code") is not None
                else None
            ),
            target_platform=str(row["target_platform"]),
            required_page_types=applicability["required_page_types"],
            optional_page_types=applicability["optional_page_types"],
            not_applicable_page_types=applicability["not_applicable_page_types"],
            applicability_mode=str(applicability["applicability_mode"]),
            catalog_version=str(applicability["catalog_version"]),
            catalog_hash=str(applicability["catalog_hash"]),
            normalization_version=str(applicability["normalization_version"]),
            snapshot_protocol_version=str(applicability["snapshot_protocol_version"]),
            frozen_coverage_snapshot=frozen_snapshot,
        )

    async def list_gap_rule_candidates(
        self,
        *,
        snapshot: WebCoverageSnapshot,
    ) -> tuple[object, ...]:
        """精确读取缺失页型对应的 Web page-level 规则。"""

        if not isinstance(snapshot, WebCoverageSnapshot):
            raise TypeError("snapshot 必须为 WebCoverageSnapshot")
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                _GAP_RULE_CANDIDATES_SQL,
                snapshot.task_id,
                snapshot.agent_run_id,
                snapshot.finalization_epoch,
                list(snapshot.missing_page_types),
            )
        from services.api.app.application.web_inspection_reconciliation import (
            WebCoverageGapRule,
        )

        return tuple(
            WebCoverageGapRule(
                rule_id=str(row["rule_id"]),
                rule_code=str(row["rule_code"]),
                missing_page_type=str(row["missing_page_type"]),
            )
            for row in rows
        )

    async def record_auth_not_applicable(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        finalization_epoch: int,
        recovery_lease_token: str | None = None,
        recovery_generation: int | None = None,
        recovery_lease_expires_at: datetime | None = None,
    ) -> int:
        """为 Web 游客态中被认证门禁拦截的规则写入正式不适用结果。"""

        blocked_auth_requirement = AUTH_REQUIREMENT_AUTHENTICATED
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await _assert_optional_recovery_fence(
                connection,
                task_id=task_id,
                recovery_lease_token=recovery_lease_token,
                recovery_generation=recovery_generation,
                recovery_lease_expires_at=recovery_lease_expires_at,
            )
            rows = await connection.fetch(
                _WEB_AUTH_NOT_APPLICABLE_CANDIDATES_SQL,
                task_id,
                agent_run_id,
                finalization_epoch,
                blocked_auth_requirement,
            )
            matched_rows = []
            for row in rows:
                rule = dict(row)
                url_matches, _ = candidate_rule_matches_page_url(
                    page_url=row["page_url"],
                    url_pattern=row["url_pattern"],
                )
                page_state = str(row["page_state"] or "").strip().casefold()
                if url_matches and _rule_matches_required_page_state(rule, page_state):
                    matched_rows.append(row)
            inserted = 0
            for row in matched_rows:
                status = await connection.execute(
                    _INSERT_WEB_AUTH_NOT_APPLICABLE_SQL,
                    task_id,
                    agent_run_id,
                    row["rule_id"],
                    row["page_capture_id"],
                    row["rule_code"],
                    row["executor_type"],
                    row["severity"],
                    row["page_type"],
                    blocked_auth_requirement,
                    finalization_epoch,
                )
                inserted += int(status.rsplit(" ", 1)[-1])
            return inserted

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


async def _assert_optional_recovery_fence(
    connection: Any,
    *,
    task_id: str,
    recovery_lease_token: str | None,
    recovery_generation: int | None,
    recovery_lease_expires_at: datetime | None,
) -> None:
    if recovery_lease_token is None:
        return
    await assert_finalization_recovery_fence(
        connection,
        task_id=task_id,
        recovery_lease_token=recovery_lease_token,
        recovery_generation=recovery_generation,
        recovery_lease_expires_at=recovery_lease_expires_at,
    )


def _validate_current_identity(
    row: Mapping[str, Any] | None,
    snapshot: WebCoverageSnapshot,
) -> None:
    if row is None:
        raise WebCoverageSnapshotPersistenceError("Web Task 或 Agent Run 不存在")
    if str(row["target_platform"]) != "web":
        raise WebCoverageSnapshotPersistenceError("Coverage Snapshot 只允许 Web Task")
    if str(row["task_id"]) != snapshot.task_id or str(row["agent_run_task_id"]) != snapshot.task_id:
        raise WebCoverageSnapshotPersistenceError("Agent Run 不属于当前 Web Task")
    if str(row["agent_run_id"]) != snapshot.agent_run_id:
        raise WebCoverageSnapshotPersistenceError("Agent Run 身份不一致")
    if str(row["agent_type"]) != "main":
        raise WebCoverageSnapshotPersistenceError("Coverage Snapshot 只允许当前 main Run")
    if row["is_latest_main_run"] is not True:
        raise WebCoverageSnapshotPersistenceError("Agent Run 不是当前最新 main Run")
    if str(row["finalization_agent_run_id"]) != snapshot.agent_run_id:
        raise WebCoverageSnapshotPersistenceError("Agent Run 不是当前收口所有者")
    if int(row["finalization_epoch"]) != snapshot.finalization_epoch:
        raise WebCoverageSnapshotPersistenceError("finalization_epoch 已变化")
    finalization_state = str(row["finalization_state"])
    if finalization_state not in _REPLAY_FINALIZATION_STATES:
        raise WebCoverageSnapshotPersistenceError("Web 探索尚未进入终态")
    task_status = str(row["task_status"])
    agent_run_status = str(row["agent_run_status"])
    if task_status in _CANCELED_STATUSES:
        raise WebCoverageSnapshotPersistenceError("已取消的 Web Task 不得冻结覆盖事实")
    if agent_run_status in _CANCELED_STATUSES:
        raise WebCoverageSnapshotPersistenceError("已取消的 main Run 不得冻结覆盖事实")
    if task_status not in _TASK_STATUSES_BY_FINALIZATION_STATE[finalization_state]:
        raise WebCoverageSnapshotPersistenceError("任务状态与当前 Web 收口阶段不一致")
    if snapshot.coverage_status == "failed":
        if agent_run_status not in _FOUNDATIONAL_FAILURE_RUN_STATUSES:
            raise WebCoverageSnapshotPersistenceError(
                "failed Coverage Snapshot 必须绑定持久化基础失败事实"
            )
        return
    if agent_run_status != "done":
        raise WebCoverageSnapshotPersistenceError(
            "正常 Coverage Snapshot 必须绑定成功结束的 main Run"
        )


def _validate_first_creation(row: Mapping[str, Any] | None) -> None:
    if row is None:
        raise WebCoverageSnapshotPersistenceError("Web Task 或 Agent Run 不存在")
    if str(row["finalization_state"]) not in _FIRST_CREATE_FINALIZATION_STATES:
        raise WebCoverageSnapshotPersistenceError("Web Coverage Snapshot 只能在 Gate 前首次创建")


async def _fetch_snapshot(
    connection: Any,
    *,
    task_id: str,
    agent_run_id: str,
    finalization_epoch: int,
) -> Mapping[str, Any] | None:
    return await connection.fetchrow(
        """
        SELECT *
        FROM qc_web_coverage_snapshot
        WHERE task_id = $1::uuid
          AND agent_run_id = $2::uuid
          AND finalization_epoch = $3
        """,
        task_id,
        agent_run_id,
        finalization_epoch,
    )


def _resolve_replay(
    row: Mapping[str, Any],
    snapshot: WebCoverageSnapshot,
) -> WebCoverageSnapshot:
    if str(row["snapshot_hash"]) != snapshot.snapshot_hash:
        raise WebCoverageSnapshotConflictError("同一 task/run/epoch 已冻结不同覆盖事实")
    return _parse_snapshot(row)


def parse_web_coverage_snapshot_row(row: Mapping[str, Any]) -> WebCoverageSnapshot:
    """把完整数据库行还原为领域对象，并据此重算不可变哈希。"""

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
        normalization_version=str(row["normalization_version"]),
    )
    return WebCoverageSnapshot(
        snapshot_id=str(row["id"]),
        task_id=str(row["task_id"]),
        agent_run_id=str(row["agent_run_id"]),
        finalization_epoch=int(row["finalization_epoch"]),
        required_page_types=required,
        observed_page_types=observed,
        missing_page_types=missing,
        unrecognized_page_types=unrecognized,
        normalization_version=str(row["normalization_version"]),
        coverage_status=str(row["coverage_status"]),
        snapshot_hash=str(row["snapshot_hash"]),
        created_at=_datetime(row["created_at"]),
        optional_page_types=applicability["optional_page_types"],
        not_applicable_page_types=applicability["not_applicable_page_types"],
        applicability_mode=str(applicability["applicability_mode"]),
        catalog_version=str(applicability["catalog_version"]),
        catalog_hash=str(applicability["catalog_hash"]),
        snapshot_protocol_version=(str(applicability["snapshot_protocol_version"])),
        observed_lifecycle_page_types=applicability["observed_lifecycle_page_types"],
        unknown_page_capture_ids=applicability["unknown_page_capture_ids"],
        supplemental_evidence_summary=applicability["supplemental_evidence_summary"],
    )


_parse_snapshot = parse_web_coverage_snapshot_row


def _json_text_tuple(value: object) -> tuple[str, ...]:
    parsed = json.loads(value) if isinstance(value, str) else value
    if not isinstance(parsed, list | tuple):
        raise WebCoverageSnapshotPersistenceError("快照页面类型字段必须为 JSON 数组")
    if any(not isinstance(item, str) for item in parsed):
        raise WebCoverageSnapshotPersistenceError("快照页面类型字段只能包含字符串")
    return tuple(parsed)


def _sorted_text_tuple(value: object) -> tuple[str, ...]:
    parsed = json.loads(value) if isinstance(value, str) else value
    if not isinstance(parsed, list | tuple):
        return ()
    return tuple(sorted({str(item).strip().casefold() for item in parsed if str(item).strip()}))


def _sorted_identifier_tuple(value: object) -> tuple[str, ...]:
    parsed = json.loads(value) if isinstance(value, str) else value
    if not isinstance(parsed, list | tuple):
        return ()
    return tuple(sorted({str(item).strip() for item in parsed if str(item).strip()}))


def _strict_sorted_text_tuple(value: object) -> tuple[str, ...]:
    try:
        parsed = _json_text_tuple(value)
    except (TypeError, ValueError) as exc:
        raise WebCoverageSnapshotPersistenceError("Web v4 扩展页型字段无效") from exc
    normalized = tuple(sorted({item.strip().casefold() for item in parsed if item.strip()}))
    if len(normalized) != len(parsed):
        raise WebCoverageSnapshotPersistenceError("Web v4 扩展页型字段必须规范且不重复")
    return normalized


def _strict_sorted_identifier_tuple(value: object) -> tuple[str, ...]:
    try:
        parsed = _json_text_tuple(value)
    except (TypeError, ValueError) as exc:
        raise WebCoverageSnapshotPersistenceError("Web v4 Capture 身份字段无效") from exc
    normalized = tuple(sorted({item.strip() for item in parsed if item.strip()}))
    if len(normalized) != len(parsed):
        raise WebCoverageSnapshotPersistenceError("Web v4 Capture 身份字段必须规范且不重复")
    return normalized


def _json_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _applicability_payload(snapshot: WebCoverageSnapshot) -> dict[str, object]:
    payload: dict[str, object] = {
        "required_page_types": list(snapshot.required_page_types),
        "optional_page_types": list(snapshot.optional_page_types),
        "not_applicable_page_types": list(snapshot.not_applicable_page_types),
        "applicability_mode": snapshot.applicability_mode,
        "catalog_version": snapshot.catalog_version,
        "catalog_hash": snapshot.catalog_hash,
        "snapshot_protocol_version": snapshot.snapshot_protocol_version,
    }
    if snapshot.snapshot_protocol_version == WEB_COVERAGE_SNAPSHOT_V4:
        payload.update(
            observed_page_types=list(snapshot.observed_page_types),
            missing_page_types=list(snapshot.missing_page_types),
            unrecognized_page_types=list(snapshot.unrecognized_page_types),
            lifecycle_observations=list(snapshot.observed_lifecycle_page_types),
            unknown_page_capture_ids=list(snapshot.unknown_page_capture_ids),
            supplemental_evidence_summary=dict(snapshot.supplemental_evidence_summary),
        )
    return payload


def _stored_applicability(
    value: object,
    required: tuple[str, ...],
    *,
    observed: tuple[str, ...] = (),
    missing: tuple[str, ...] = (),
    unrecognized: tuple[str, ...] = (),
    normalization_version: str = WEB_PAGE_TYPE_NORMALIZATION_VERSION,
) -> dict[str, object]:
    payload = json.loads(value) if isinstance(value, str) else value
    canonical = _canonical_types_for_version(normalization_version)
    if not isinstance(payload, Mapping):
        if normalization_version == WEB_PAGE_TYPE_NORMALIZATION_VERSION:
            raise WebCoverageSnapshotPersistenceError(
                "Web v4 applicability_snapshot 必须为完整不可变对象"
            )
        return {
            "required_page_types": required,
            "optional_page_types": (),
            "not_applicable_page_types": tuple(sorted(set(canonical) - set(required))),
            "applicability_mode": "legacy_required_snapshot",
            "catalog_version": "legacy-frozen",
            "catalog_hash": "legacy-frozen",
            "snapshot_protocol_version": (
                WEB_COVERAGE_SNAPSHOT_V4
                if normalization_version == WEB_PAGE_TYPE_NORMALIZATION_VERSION
                else WEB_COVERAGE_SNAPSHOT_V3
                if normalization_version == "web-page-type-assessment.v3"
                else WEB_COVERAGE_SNAPSHOT_V1
            ),
            "observed_lifecycle_page_types": (),
            "unknown_page_capture_ids": (),
            "supplemental_evidence_summary": {},
        }
    protocol = str(payload.get("snapshot_protocol_version") or "")
    if protocol == WEB_COVERAGE_SNAPSHOT_V4:
        paired_facts = {
            "required_page_types": required,
            "observed_page_types": observed,
            "missing_page_types": missing,
            "unrecognized_page_types": unrecognized,
        }
        for field_name, top_level_value in paired_facts.items():
            try:
                stored_value = _json_text_tuple(payload[field_name])
            except (KeyError, TypeError, ValueError) as exc:
                raise WebCoverageSnapshotPersistenceError(
                    f"Web v4 applicability_snapshot.{field_name} 无效"
                ) from exc
            if stored_value != top_level_value:
                raise WebCoverageSnapshotPersistenceError(
                    f"Web v4 applicability_snapshot.{field_name} 与顶层快照不一致"
                )
        for field_name in (
            "optional_page_types",
            "not_applicable_page_types",
            "lifecycle_observations",
            "unknown_page_capture_ids",
            "supplemental_evidence_summary",
        ):
            if field_name not in payload:
                raise WebCoverageSnapshotPersistenceError(
                    f"Web v4 applicability_snapshot.{field_name} 缺失"
                )
    return {
        "required_page_types": _sorted_text_tuple(payload.get("required_page_types")),
        "optional_page_types": (
            _strict_sorted_text_tuple(payload.get("optional_page_types"))
            if protocol == WEB_COVERAGE_SNAPSHOT_V4
            else _sorted_text_tuple(payload.get("optional_page_types"))
        ),
        "not_applicable_page_types": (
            _strict_sorted_text_tuple(payload.get("not_applicable_page_types"))
            if protocol == WEB_COVERAGE_SNAPSHOT_V4
            else _sorted_text_tuple(payload.get("not_applicable_page_types"))
        ),
        "applicability_mode": str(payload.get("applicability_mode") or ""),
        "catalog_version": str(payload.get("catalog_version") or ""),
        "catalog_hash": str(payload.get("catalog_hash") or ""),
        "snapshot_protocol_version": protocol,
        "observed_lifecycle_page_types": (
            _strict_sorted_text_tuple(payload.get("lifecycle_observations"))
            if protocol == WEB_COVERAGE_SNAPSHOT_V4
            else ()
        ),
        "unknown_page_capture_ids": (
            _strict_sorted_identifier_tuple(payload.get("unknown_page_capture_ids"))
            if protocol == WEB_COVERAGE_SNAPSHOT_V4
            else ()
        ),
        "supplemental_evidence_summary": (
            _strict_integer_summary(payload.get("supplemental_evidence_summary"))
            if protocol == WEB_COVERAGE_SNAPSHOT_V4
            else {}
        ),
    }


def _task_applicability(value: object, *, frozen_policy: object = None) -> dict[str, object]:
    task_config = json.loads(value) if isinstance(value, str) else value
    target = task_config.get("web_exploration_target") if isinstance(task_config, Mapping) else None
    if not isinstance(target, Mapping):
        target = {}
    policy = _json_object(frozen_policy)
    page_catalog = _json_object(policy.get("page_catalog"))
    codes = tuple(
        item.get("code") for item in page_catalog.get("page_types", []) if isinstance(item, Mapping)
    )
    target_partitions = (
        tuple(target.get("required_page_types") or ())
        + tuple(target.get("optional_page_types") or ())
        + tuple(target.get("not_applicable_page_types") or ())
    )
    version_facts = set(codes or target_partitions)
    is_v4 = bool({"activity", "search_page", "collection_page"} & version_facts)
    canonical = (
        tuple(code for code in WEB_BUSINESS_PAGE_TYPE_CODES
              if code != "collection_page" or "collection_page" in version_facts)
        if is_v4
        else WEB_V3_BUSINESS_PAGE_TYPE_CODES
        if "home" in version_facts
        else WEB_LEGACY_PAGE_TYPE_CODES
    )
    versions = {
        "normalization_version": "web-page-type-assessment.v4"
        if is_v4
        else "web-page-type-assessment.v3"
        if canonical == WEB_V3_BUSINESS_PAGE_TYPE_CODES
        else "web-page-type-assessment.v2",
        "snapshot_protocol_version": "web-coverage-snapshot.v4"
        if is_v4
        else "web-coverage-snapshot.v3"
        if canonical == WEB_V3_BUSINESS_PAGE_TYPE_CODES
        else "web-coverage-snapshot.v2",
    }
    default_required = (
        tuple(sorted(WEB_V3_BUSINESS_PAGE_TYPE_CODES))
        if is_v4
        else tuple(sorted(canonical))
    )
    default_optional = (
        tuple(sorted(set(canonical) - set(WEB_V3_BUSINESS_PAGE_TYPE_CODES)))
        if is_v4
        else ()
    )
    configured_required = _sorted_text_tuple(target.get("required_page_types"))
    frozen_required = _sorted_text_tuple(
        _json_object(policy.get("discovery_policy")).get("required_page_types")
    )
    if frozen_required and configured_required and frozen_required != configured_required:
        raise WebCoverageSnapshotPersistenceError("Web 任务目标与 Run 冻结目标不一致")
    required = frozen_required or configured_required or default_required
    if all(
        key in target
        for key in (
            "optional_page_types",
            "not_applicable_page_types",
            "applicability_mode",
            "catalog_version",
            "catalog_hash",
        )
    ):
        return {
            **versions,
            "required_page_types": required,
            "optional_page_types": _sorted_text_tuple(target.get("optional_page_types")),
            "not_applicable_page_types": _sorted_text_tuple(
                target.get("not_applicable_page_types")
            ),
            "applicability_mode": str(target.get("applicability_mode") or ""),
            "catalog_version": str(target.get("catalog_version") or ""),
            "catalog_hash": str(target.get("catalog_hash") or ""),
        }
    from services.api.app.application.web_page_type_assessment import WebPageTypeCatalogIdentity

    frozen_identity = (WebPageTypeCatalogIdentity.from_frozen_catalog_payload(page_catalog)
                       if page_catalog.get("version") else None)
    optional = tuple(code for code in default_optional if code not in required)
    return {
        **versions,
        "required_page_types": required,
        "optional_page_types": optional,
        "not_applicable_page_types": tuple(
            sorted(set(canonical) - set(required) - set(optional))
        ),
        "applicability_mode": "legacy_required_snapshot",
        "catalog_version": (
            frozen_identity.catalog_version if frozen_identity else "legacy-task-config"
        ),
        "catalog_hash": frozen_identity.catalog_sha256 if frozen_identity else "legacy-task-config",
    }


def _finalize_runtime_applicability(
    applicability: Mapping[str, object],
    *,
    applicable_page_types: tuple[str, ...],
    covered_page_types: tuple[str, ...],
    inspection_scope: frozenset[str] = frozenset(),
) -> dict[str, object]:
    """把运行时入口和正式分类证据提升为本 Run 的覆盖分母。"""

    snapshot = WebPageApplicabilitySnapshot(
        required_page_types=_sorted_text_tuple(applicability.get("required_page_types")),
        optional_page_types=_sorted_text_tuple(applicability.get("optional_page_types")),
        not_applicable_page_types=_sorted_text_tuple(
            applicability.get("not_applicable_page_types")
        ),
        applicability_mode="runtime_dynamic",
        catalog_version=str(applicability.get("catalog_version") or "runtime-current"),
        catalog_hash=str(applicability.get("catalog_hash") or "runtime-current"),
        resolution_reason="runtime_evidence_initial",
    )
    finalized = finalize_web_page_applicability(
        snapshot,
        applicable_page_types=applicable_page_types,
        covered_page_types=covered_page_types,
        inspection_scope=inspection_scope,
    )
    return {
        **applicability,
        "required_page_types": finalized.required_page_types,
        "optional_page_types": finalized.optional_page_types,
        "not_applicable_page_types": finalized.not_applicable_page_types,
        "page_type_statuses": dict(finalized.page_type_statuses),
        "resolution_reason": finalized.resolution_reason,
    }


def _applicability_from_frozen_snapshot(
    snapshot: WebCoverageSnapshot,
) -> dict[str, object]:
    return {
        "required_page_types": snapshot.required_page_types,
        "optional_page_types": snapshot.optional_page_types,
        "not_applicable_page_types": snapshot.not_applicable_page_types,
        "applicability_mode": snapshot.applicability_mode,
        "catalog_version": snapshot.catalog_version,
        "catalog_hash": snapshot.catalog_hash,
        "normalization_version": snapshot.normalization_version,
        "snapshot_protocol_version": snapshot.snapshot_protocol_version,
    }


def _json_object(value: object) -> Mapping[str, object]:
    parsed = json.loads(value) if isinstance(value, str) else value
    return parsed if isinstance(parsed, Mapping) else {}


def _canonical_types_for_version(version: str) -> tuple[str, ...]:
    if version == "web-page-type-assessment.v4":
        return WEB_BUSINESS_PAGE_TYPE_CODES
    if version == "web-page-type-assessment.v3":
        return WEB_V3_BUSINESS_PAGE_TYPE_CODES
    return WEB_LEGACY_PAGE_TYPE_CODES


def _integer_summary(value: object) -> dict[str, int]:
    parsed = json.loads(value) if isinstance(value, str) else value
    if not isinstance(parsed, Mapping):
        return {}
    return {
        str(key): int(count)
        for key, count in parsed.items()
        if isinstance(key, str)
        and isinstance(count, int)
        and not isinstance(count, bool)
        and count >= 0
    }


def _strict_integer_summary(value: object) -> dict[str, int]:
    parsed = json.loads(value) if isinstance(value, str) else value
    if not isinstance(parsed, Mapping):
        raise WebCoverageSnapshotPersistenceError("Web v4 supplemental_evidence_summary 必须为对象")
    result: dict[str, int] = {}
    for key, count in parsed.items():
        if (
            not isinstance(key, str)
            or not key.strip()
            or key != key.strip()
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count < 0
        ):
            raise WebCoverageSnapshotPersistenceError("Web v4 supplemental_evidence_summary 无效")
        result[key] = count
    return result


def _datetime(value: object) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime):
        raise WebCoverageSnapshotPersistenceError("快照 created_at 无效")
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


_RECONCILIATION_STATE_SQL = (
    "WITH target_task AS (SELECT * FROM qc_task WHERE id=$1::uuid), "
    + web_classification_reuse_cte("$2::uuid")
    + """
    SELECT
        task.id::text AS task_id,
        run.id::text AS agent_run_id,
        task.target_platform,
        task.status AS task_status,
        task.finalization_state,
        task.finalization_row_version AS task_row_version,
        task.finalization_epoch,
        task.finalization_agent_run_id::text AS finalization_agent_run_id,
        run.status AS run_status,
        current_main.id = run.id AS current_main_run,
        task.task_config->>'execution_mode' AS execution_mode,
        task.task_config->>'web_inspection_notification_mode' AS notification_mode,
        task.task_config,
        policy.payload AS frozen_run_policy,
        to_jsonb(existing_coverage) AS frozen_coverage_snapshot,
        COALESCE(budget.state, 'missing') AS budget_state,
        COALESCE(frontier.nonterminal, 0)::integer AS frontier_work_count,
        COALESCE(capture_commit.pending_capture_count, 0)::integer
            AS pending_capture_commit_count,
        COALESCE(capture_commit.pending_manifest, 0)::integer AS pending_manifest_count,
        COALESCE(
            classification_inventory.missing_assessment_count,
            0
        )::integer AS missing_assessment_count,
        COALESCE(
            classification_inventory.review_assessment_count,
            0
        )::integer AS review_assessment_count,
        COALESCE(
            recognized_publication_inventory.missing_publication_count,
            0
        )::integer AS missing_publication_count,
        COALESCE(capture_commit.failed, 0)::integer AS failed_manifest_count,
        COALESCE(publication.nonterminal, 0)::integer AS nonterminal_publication_count,
        COALESCE(publication.needs_review, 0)::integer AS review_publication_count,
        COALESCE(publication.failed, 0)::integer AS failed_publication_count,
        COALESCE(review_queue.open_count, 0)::integer AS open_review_count,
        COALESCE(formal_work.nonterminal, 0)::integer AS nonterminal_work_item_count,
        COALESCE(active_request.nonterminal, 0)::integer AS active_session_request_count,
        COALESCE(
            recognized_publication_inventory.page_types,
            ARRAY[]::text[]
        ) AS observed_page_types,
        COALESCE((
            SELECT array_agg(DISTINCT applicable.page_type ORDER BY applicable.page_type)
            FROM (
                SELECT NULLIF(
                    entry.discovery_manifest#>>'{applicability,page_type}', ''
                ) AS page_type
                FROM qc_web_menu_ledger_entry AS entry
                WHERE entry.agent_run_id = run.id
                  AND entry.source_kind = 'runtime_discovered'
                  AND entry.decision = 'runtime_observe_only'
                  AND entry.discovery_manifest ? 'authorization'
                UNION ALL
                SELECT NULLIF(entry.menu_page_type, '') AS page_type
                FROM qc_web_menu_ledger_entry AS entry
                WHERE entry.agent_run_id = run.id
                UNION ALL
                SELECT NULLIF(entry.detail_page_type, '') AS page_type
                FROM qc_web_menu_ledger_entry AS entry
                WHERE entry.agent_run_id = run.id
            ) AS applicable
            WHERE applicable.page_type IN (
                'home', 'content_list', 'content_detail', 'video_list',
                'video_detail', 'activity', 'search_page', 'collection_page'
            )
        ), ARRAY[]::text[]) AS runtime_applicable_page_types,
        COALESCE(unrecognized.page_types, ARRAY[]::text[]) AS unrecognized_page_types,
        COALESCE((
            SELECT array_agg(DISTINCT lifecycle.page_type ORDER BY lifecycle.page_type)
            FROM qc_h5_page_type_assessment AS lifecycle
            WHERE lifecycle.task_id = task.id
              AND lifecycle.agent_run_id = run.id
              AND lifecycle.schema_version = 'web-page-type-assessment.v4'
              AND lifecycle.page_type = 'age_gate'
              AND lifecycle.detection_status = 'recognized'
              AND lifecycle.assessment_payload->>'coverage_group' = 'lifecycle'
        ), ARRAY[]::text[]) AS observed_lifecycle_page_types,
        COALESCE((
            SELECT array_agg(
                DISTINCT unknown_assessment.page_capture_id::text
                ORDER BY unknown_assessment.page_capture_id::text
            )
            FROM qc_h5_page_type_assessment AS unknown_assessment
            WHERE unknown_assessment.task_id = task.id
              AND unknown_assessment.agent_run_id = run.id
              AND unknown_assessment.schema_version = 'web-page-type-assessment.v4'
              AND unknown_assessment.page_type = 'unknown'
              AND unknown_assessment.detection_status = 'needs_review'
              AND unknown_assessment.assessment_payload->>'coverage_group' = 'neither'
        ), ARRAY[]::text[]) AS unknown_page_capture_ids,
        COALESCE((
            SELECT jsonb_object_agg(supplemental.status, supplemental.total)
            FROM (
                SELECT
                    COALESCE(
                        NULLIF(assessment.assessment_payload->>'supplemental_evidence_status', ''),
                        'not_attempted'
                    ) AS status,
                    count(*)::integer AS total
                FROM qc_h5_page_type_assessment AS assessment
                WHERE assessment.task_id = task.id
                  AND assessment.agent_run_id = run.id
                  AND assessment.schema_version = 'web-page-type-assessment.v4'
                GROUP BY 1
            ) AS supplemental
        ), '{}'::jsonb) AS supplemental_evidence_summary,
        CASE
            WHEN run.status IN ('failed', 'aborted')
            THEN COALESCE(
                NULLIF(run.output_context->>'error_code', ''),
                'WEB_FOUNDATIONAL_FAILURE'
            )
            ELSE NULL
        END AS foundational_failure_code
    FROM qc_task AS task
    INNER JOIN qc_agent_run AS run
        ON run.task_id = task.id
       AND run.id = $2::uuid
       AND run.agent_type = 'main'
    LEFT JOIN qc_web_run_policy_snapshot AS policy
        ON policy.task_id = task.id AND policy.agent_run_id = run.id
    LEFT JOIN qc_web_coverage_snapshot AS existing_coverage
        ON existing_coverage.task_id = task.id AND existing_coverage.agent_run_id = run.id
       AND existing_coverage.finalization_epoch = task.finalization_epoch
    LEFT JOIN LATERAL (
        SELECT latest.id
        FROM qc_agent_run AS latest
        WHERE latest.task_id = task.id
          AND latest.agent_type = 'main'
        ORDER BY latest.created_at DESC, latest.id DESC
        LIMIT 1
    ) AS current_main ON TRUE
    LEFT JOIN LATERAL (
        SELECT CASE WHEN bool_and(item.state = 'sealed') THEN 'sealed' ELSE 'open' END AS state
        FROM qc_web_exploration_budget AS item
        WHERE item.task_id = task.id
          AND item.agent_run_id = run.id
    ) AS budget ON TRUE
    LEFT JOIN LATERAL (
        SELECT count(*)::integer AS nonterminal
        FROM qc_v2_work_item AS work
        WHERE work.task_id = task.id
          AND work.work_type = 'exploration'
          AND work.session_id = ('web:' || run.id::text)
          AND work.status NOT IN (
              'succeeded', 'bounded', 'budget_exhausted', 'blocked',
              'no_progress', 'failed', 'cancelled', 'superseded'
          )
    ) AS frontier ON TRUE
    LEFT JOIN LATERAL (
        SELECT
            count(*) FILTER (
                WHERE capture.capture_status = 'pending'
            ) AS pending_capture_count,
            count(*) FILTER (
                WHERE capture.capture_status = 'captured'
                  AND (
                      latest_manifest.manifest_id IS NULL
                      OR latest_manifest.quality_status = 'pending'
                  )
            ) AS pending_manifest,
            count(*) FILTER (
                WHERE capture.capture_status = 'captured'
                  AND latest_manifest.quality_status = 'fail'
            ) AS failed
        FROM qc_capture AS capture
        LEFT JOIN qc_page_capture AS commit_page
            ON commit_page.task_id = task.id
           AND commit_page.id = capture.id
        LEFT JOIN LATERAL (
            SELECT manifest.manifest_id, manifest.quality_status
            FROM qc_evidence_manifest AS manifest
            WHERE manifest.task_id = task.id
              AND manifest.capture_id = capture.id
            ORDER BY manifest.manifest_version DESC
            LIMIT 1
        ) AS latest_manifest ON TRUE
        WHERE capture.task_id = task.id
          AND capture.agent_run_id = run.id
          AND capture.capture_status IN ('pending', 'captured')
          AND COALESCE(commit_page.metadata->>'capture_purpose', '')
              <> 'web_guide_page'
          AND COALESCE(
                commit_page.inspection_lifecycle_status,
                capture.metadata->>'inspection_lifecycle_status',
                ''
              ) NOT IN ('superseded', 'abandoned')
    ) AS capture_commit ON TRUE
    LEFT JOIN LATERAL (
        SELECT
            count(*) FILTER (
                WHERE item.status NOT IN (
                    'published', 'not_applicable', 'needs_review', 'failed'
                )
            )::integer AS nonterminal,
            count(*) FILTER (
                WHERE item.status = 'needs_review'
            )::integer AS needs_review,
            count(*) FILTER (
                WHERE item.status = 'failed'
            )::integer AS failed
        FROM qc_h5_inspection_publication AS item
        WHERE item.task_id = task.id
          AND item.source_agent_run_id = run.id
          AND item.finalization_epoch = task.finalization_epoch
          AND item.mode = 'enforced'
    ) AS publication ON TRUE
    LEFT JOIN LATERAL (
        SELECT count(*)::integer AS nonterminal
        FROM qc_inspection_work_item AS item
        WHERE item.task_id = task.id
          AND item.producer_agent_run_id = run.id
          AND item.finalization_epoch = task.finalization_epoch
          AND item.control_mode = 'enforced'
          AND item.status NOT IN (
              'succeeded', 'partial', 'failed', 'cancelled', 'superseded'
          )
    ) AS formal_work ON TRUE
    LEFT JOIN LATERAL (
        SELECT count(*)::integer AS nonterminal
        FROM qc_evidence_request AS request
        INNER JOIN qc_capture AS capture
            ON capture.id = request.target_capture_id
           AND capture.agent_run_id = run.id
        WHERE request.task_id = task.id
          AND request.session_dependency = 'active_session'
          AND request.status IN ('pending', 'routed', 'running')
    ) AS active_request ON TRUE
    LEFT JOIN LATERAL (
        SELECT count(*)::integer AS open_count
        FROM qc_review_item AS review
        WHERE review.task_id = task.id
          AND review.status IN ('pending', 'needs_recheck')
    ) AS review_queue ON TRUE
    LEFT JOIN LATERAL (
        SELECT count(DISTINCT classification_capture.id) FILTER (
            WHERE classification_assessment.assessment_record_id IS NULL
        )::integer AS missing_assessment_count,
        count(DISTINCT classification_capture.id) FILTER (
            WHERE classification_assessment.detection_status = 'needs_review'
        )::integer AS review_assessment_count
        FROM qc_capture AS classification_capture
        INNER JOIN qc_page_capture AS classification_page
            ON classification_page.task_id = task.id
           AND classification_page.id = classification_capture.id
        INNER JOIN qc_surface_session AS classification_session
            ON classification_session.task_id = task.id
           AND classification_session.session_id = ('web:' || run.id::text)
           AND classification_session.surface_kind = 'web'
           AND classification_session.session_owner = 'web-v2-workflow'
           AND classification_session.finalization_epoch = task.finalization_epoch
           AND classification_capture.surface_session_id =
               classification_session.session_id
        INNER JOIN LATERAL (
            SELECT manifest.manifest_id, manifest.manifest_hash,
                   manifest.quality_status
            FROM qc_evidence_manifest AS manifest
            WHERE manifest.task_id = task.id
              AND manifest.capture_id = classification_capture.id
            ORDER BY manifest.manifest_version DESC
            LIMIT 1
        ) AS classification_manifest
            ON classification_manifest.quality_status = 'pass'
           AND NULLIF(BTRIM(classification_manifest.manifest_id), '') IS NOT NULL
           AND NULLIF(BTRIM(classification_manifest.manifest_hash), '') IS NOT NULL
        LEFT JOIN qc_h5_page_type_assessment AS classification_assessment
            ON classification_assessment.task_id = task.id
           AND classification_assessment.agent_run_id = run.id
           AND classification_assessment.session_id =
               classification_session.session_id
           AND classification_assessment.page_capture_id =
               classification_capture.id
           AND classification_assessment.schema_version IN (
               'web-page-type-assessment.v1', 'web-page-type-assessment.v2',
               'web-page-type-assessment.v3', 'web-page-type-assessment.v4'
           )
        WHERE classification_capture.task_id = task.id
          AND classification_capture.agent_run_id = run.id
          AND classification_capture.surface_kind = 'web'
          AND classification_capture.capture_status = 'captured'
          AND classification_capture.capture_role = 'after_action'
          AND (EXISTS (
                SELECT 1
                FROM qc_web_menu_ledger_entry AS classification_entry
                WHERE classification_entry.agent_run_id = run.id
                  AND (
                        classification_entry.menu_ready_capture_id =
                            classification_capture.id
                        OR classification_entry.detail_ready_capture_id =
                            classification_capture.id
                      )
              ) OR EXISTS (
                SELECT 1 FROM web_classification_reuse reuse
                WHERE reuse.assessment_capture_id=classification_capture.id
              ))
          AND NOT EXISTS (
                SELECT 1 FROM web_classification_reuse reuse
                JOIN LATERAL (
                    SELECT manifest.quality_status,manifest.manifest_id,manifest.manifest_hash
                    FROM qc_evidence_manifest manifest
                    WHERE manifest.task_id=task.id
                      AND manifest.capture_id=reuse.assessment_capture_id
                    ORDER BY manifest.manifest_version DESC LIMIT 1
                ) source_manifest ON source_manifest.quality_status='pass'
                  AND NULLIF(BTRIM(source_manifest.manifest_id),'') IS NOT NULL
                  AND NULLIF(BTRIM(source_manifest.manifest_hash),'') IS NOT NULL
                WHERE reuse.observed_capture_id=classification_capture.id
              )
          AND COALESCE(
                classification_page.inspection_lifecycle_status,
                classification_capture.metadata->>'inspection_lifecycle_status',
                ''
              ) NOT IN ('superseded', 'abandoned')
    ) AS classification_inventory ON TRUE
    LEFT JOIN LATERAL (
        SELECT
            array_agg(
                DISTINCT assessment.page_type ORDER BY assessment.page_type
            ) AS page_types,
            count(DISTINCT (
                COALESCE(
                    observed_page.metadata->>'logical_page_id',
                    observed_capture.metadata->>'logical_page_id',
                    observed_capture.id::text
                ),
                (
                    SELECT md5(string_agg(
                        observed_artifact.artifact_type || ':' ||
                            observed_artifact.checksum,
                        '|' ORDER BY observed_artifact.artifact_type,
                                     observed_artifact.checksum
                    ))
                    FROM qc_evidence_artifact AS observed_artifact
                    WHERE observed_artifact.task_id = task.id
                      AND observed_artifact.page_capture_id = observed_capture.id
                      AND observed_artifact.artifact_type IN ('dom', 'visible_text')
                      AND observed_artifact.lifecycle_status IN (
                          'active', 'archived'
                      )
                )
            )) FILTER (
                WHERE publication.id IS NULL
            )::integer AS missing_publication_count
        FROM qc_capture AS observed_capture
        INNER JOIN qc_page_capture AS observed_page
            ON observed_page.task_id = task.id
           AND observed_page.id = observed_capture.id
        INNER JOIN qc_surface_session AS observed_session
            ON observed_session.task_id = task.id
           AND observed_session.session_id = ('web:' || run.id::text)
           AND observed_session.surface_kind = 'web'
           AND observed_session.session_owner = 'web-v2-workflow'
           AND observed_session.finalization_epoch = task.finalization_epoch
           AND observed_capture.surface_session_id = observed_session.session_id
        INNER JOIN qc_h5_page_type_assessment AS assessment
            ON assessment.task_id = task.id
           AND assessment.agent_run_id = run.id
           AND assessment.session_id = observed_session.session_id
           AND assessment.page_capture_id = observed_capture.id
           AND assessment.schema_version IN (
               'web-page-type-assessment.v1', 'web-page-type-assessment.v2',
               'web-page-type-assessment.v3', 'web-page-type-assessment.v4'
           )
           AND assessment.detection_status = 'recognized'
           AND assessment.page_type IN (
               'home', 'content_list', 'content_detail', 'video_list',
               'video_detail', 'activity', 'search_page', 'collection_page'
           )
           AND assessment.page_type = observed_page.page_type
        LEFT JOIN qc_h5_inspection_publication AS publication
            ON publication.task_id = task.id
           AND publication.source_agent_run_id = run.id
           AND (
                (
                    publication.page_capture_id = observed_capture.id
                    AND publication.page_type_assessment_id =
                        assessment.assessment_record_id
                    AND publication.assessment_hash = assessment.payload_hash
                )
                OR EXISTS (
                    SELECT 1
                    FROM qc_capture AS published_capture
                    INNER JOIN qc_page_capture AS published_page
                      ON published_page.task_id = publication.task_id
                     AND published_page.id = published_capture.id
                    WHERE published_capture.id = publication.page_capture_id
                      AND COALESCE(
                            published_page.metadata->>'logical_page_id',
                            published_capture.metadata->>'logical_page_id',
                            published_capture.id::text
                          ) = COALESCE(
                            observed_page.metadata->>'logical_page_id',
                            observed_capture.metadata->>'logical_page_id',
                            observed_capture.id::text
                          )
                      AND (
                            SELECT md5(string_agg(
                                published_artifact.artifact_type || ':' ||
                                    published_artifact.checksum,
                                '|' ORDER BY published_artifact.artifact_type,
                                             published_artifact.checksum
                            ))
                            FROM qc_evidence_artifact AS published_artifact
                            WHERE published_artifact.task_id = publication.task_id
                              AND published_artifact.page_capture_id =
                                  publication.page_capture_id
                              AND published_artifact.artifact_type IN (
                                  'dom', 'visible_text'
                              )
                              AND published_artifact.lifecycle_status IN (
                                  'active', 'archived'
                              )
                          ) = (
                            SELECT md5(string_agg(
                                observed_artifact.artifact_type || ':' ||
                                    observed_artifact.checksum,
                                '|' ORDER BY observed_artifact.artifact_type,
                                             observed_artifact.checksum
                            ))
                            FROM qc_evidence_artifact AS observed_artifact
                            WHERE observed_artifact.task_id = task.id
                              AND observed_artifact.page_capture_id =
                                  observed_capture.id
                              AND observed_artifact.artifact_type IN (
                                  'dom', 'visible_text'
                              )
                              AND observed_artifact.lifecycle_status IN (
                                  'active', 'archived'
                              )
                          )
                )
           )
           AND publication.finalization_epoch = task.finalization_epoch
           AND publication.mode = 'enforced'
        WHERE observed_capture.task_id = task.id
          AND observed_capture.agent_run_id = run.id
          AND observed_capture.surface_kind = 'web'
          AND observed_capture.capture_status = 'captured'
          AND observed_capture.capture_role = 'after_action'
          AND (EXISTS (
                SELECT 1
                FROM qc_web_menu_ledger_entry AS observed_entry
                WHERE observed_entry.agent_run_id = run.id
                  AND (
                        observed_entry.menu_ready_capture_id = observed_capture.id
                        OR observed_entry.detail_ready_capture_id = observed_capture.id
                      )
              ) OR EXISTS (
                SELECT 1 FROM web_classification_reuse reuse
                WHERE reuse.assessment_capture_id=observed_capture.id
              ))
          AND COALESCE(
                observed_page.inspection_lifecycle_status,
                observed_capture.metadata->>'inspection_lifecycle_status',
                ''
              ) NOT IN ('superseded', 'abandoned')
    ) AS recognized_publication_inventory ON TRUE
    LEFT JOIN LATERAL (
        SELECT array_agg(DISTINCT normalized.raw_page_type ORDER BY normalized.raw_page_type)
            AS page_types
        FROM qc_page_capture AS page
        INNER JOIN qc_capture AS capture
            ON capture.id = page.id
           AND capture.task_id = task.id
           AND capture.agent_run_id = run.id
        CROSS JOIN LATERAL (
            SELECT NULLIF(
                BTRIM(page.metadata->'web_page_type_assessment'->>'proposed_page_type'),
                ''
            ) AS raw_page_type
        ) AS normalized
        WHERE page.metadata->'web_page_type_assessment'->>'version'
            IN (
               'web-page-type-assessment.v1', 'web-page-type-assessment.v2',
               'web-page-type-assessment.v3', 'web-page-type-assessment.v4'
           )
          AND capture.capture_status = 'captured'
          AND capture.capture_role = 'after_action'
          AND capture.surface_kind = 'web'
          AND capture.surface_session_id = ('web:' || run.id::text)
          AND EXISTS (
                SELECT 1
                FROM qc_web_menu_ledger_entry AS unrecognized_entry
                WHERE unrecognized_entry.agent_run_id = run.id
                  AND (
                        unrecognized_entry.menu_ready_capture_id = capture.id
                        OR unrecognized_entry.detail_ready_capture_id = capture.id
                      )
              )
          AND COALESCE(
                page.inspection_lifecycle_status,
                capture.metadata->>'inspection_lifecycle_status',
                ''
              ) NOT IN ('superseded', 'abandoned')
          AND page.metadata->'web_page_type_assessment'->>'final_page_type'
            IS NULL
          AND normalized.raw_page_type IS NOT NULL
    ) AS unrecognized ON TRUE
    WHERE task.id = $1::uuid
      AND task.finalization_epoch = $3
      AND (
            $4::uuid IS NULL
            OR (
                task.finalization_recovery_lease_token = $4::uuid
                AND task.finalization_recovery_generation = $5
                AND task.finalization_recovery_lease_expires_at > now()
                AND task.finalization_recovery_lease_expires_at
                    >= $6::timestamptz
                AND task.finalization_recovery_state = 'active'
                AND task.target_platform = 'web'
                AND LOWER(COALESCE(
                    task.task_config->>'workflow_version', ''
                )) = 'v2'
                AND task.task_config->>'execution_mode' = 'full_inspection'
                AND task.task_config->>'web_inspection_notification_mode'
                    = 'enforced'
            )
          )
"""


)

_GAP_RULE_CANDIDATES_SQL = f"""
    SELECT
        rule.id::text AS rule_id,
        rule.rule_code,
        missing.page_type AS missing_page_type
    FROM qc_task AS task
    INNER JOIN qc_rule AS rule
        ON rule.ruleset_id = ANY(qc_ruleset_scope(task.ruleset_id))
    CROSS JOIN unnest($4::text[]) AS missing(page_type)
    WHERE task.id = $1::uuid
      AND task.finalization_agent_run_id = $2::uuid
      AND task.finalization_epoch = $3
      AND task.target_platform = 'web'
      AND task.task_config->>'execution_mode' = 'full_inspection'
      AND task.task_config->>'web_inspection_notification_mode' = 'enforced'
      AND {WEB_GUEST_GAP_RULE_APPLICABILITY_SQL}
    ORDER BY missing.page_type, rule.sort_order, rule.rule_code
"""


_WEB_AUTH_NOT_APPLICABLE_CANDIDATES_SQL = f"""
    SELECT DISTINCT
        rule.id::text AS rule_id,
        rule.rule_code,
        rule.executor_type,
        rule.severity,
        rule.url_pattern,
        rule.judge_config,
        page.id::text AS page_capture_id,
        page.page_type,
        COALESCE(page.url, capture.metadata->>'url', '') AS page_url,
        COALESCE(page.page_state, capture.metadata->>'page_state', '') AS page_state
    FROM qc_task AS task
    INNER JOIN qc_h5_page_type_assessment AS assessment
        ON assessment.task_id = task.id
       AND assessment.agent_run_id = task.finalization_agent_run_id
       AND assessment.schema_version IN (
               'web-page-type-assessment.v1', 'web-page-type-assessment.v2',
               'web-page-type-assessment.v3', 'web-page-type-assessment.v4'
           )
       AND assessment.detection_status = 'recognized'
       AND assessment.status = 'covered'
    INNER JOIN qc_page_capture AS page
        ON page.task_id = task.id
       AND page.id = assessment.page_capture_id
       AND page.page_type = assessment.page_type
    INNER JOIN qc_capture AS capture
        ON capture.task_id = task.id
       AND capture.id = page.id
       AND capture.agent_run_id = task.finalization_agent_run_id
       AND capture.capture_status = 'captured'
    INNER JOIN qc_rule AS rule
        ON rule.ruleset_id = ANY(qc_ruleset_scope(task.ruleset_id))
       AND rule.status = 'enabled'
       AND (rule.channel IS NULL OR rule.channel = task.channel)
       AND (
            jsonb_typeof(rule.input_contract->'target_clients')
                IS DISTINCT FROM 'array'
            OR jsonb_array_length(rule.input_contract->'target_clients') = 0
            OR rule.input_contract->'target_clients' ? 'WEB'
       )
       AND jsonb_typeof(rule.applicable_page_types) = 'array'
       AND rule.applicable_page_types ? page.page_type
       AND COALESCE(
            rule.judge_config->'capability_config'->>'evidence_input_scope',
            'page'
       ) = 'page'
       AND rule.input_contract->>'auth_requirement' = $4::text
    WHERE task.id = $1::uuid
      AND task.finalization_agent_run_id = $2::uuid
      AND task.finalization_epoch = $3
      AND task.target_platform = 'web'
      AND task.task_config->>'execution_mode' = 'full_inspection'
      AND task.task_config->>'web_inspection_notification_mode' = 'enforced'
      AND COALESCE(
            page.inspection_lifecycle_status,
            capture.metadata->>'inspection_lifecycle_status',
            'active'
      ) = 'active'
      -- 经过的页面不判规则，也不补「要登录」占位
      AND {page_type_in_inspection_scope_sql("task.task_config", "page.page_type")}
    ORDER BY rule.rule_code, page.id::text
"""


_INSERT_WEB_AUTH_NOT_APPLICABLE_SQL = """
    INSERT INTO qc_rule_execution (
        task_id, rule_id, page_capture_id, source_idempotency_key, agent_run_id,
        execution_status, judge_strategy, severity,
        result_summary, problem_desc, review_status, review_reason,
        judge_input, judge_output, started_at, finished_at
    ) VALUES (
        $1::uuid, $3::uuid, $4::uuid,
        'web-auth-not-applicable:' || $1::text || ':' || $2::text || ':'
            || $10::integer::text || ':' || $3::text || ':' || $4::text,
        $2::uuid,
        'skipped', $6, $7,
        CASE $9::text
            WHEN 'authenticated' THEN
                '该规则需要登录后才能检查，本次为未登录（游客）浏览，不适用'
            ELSE '该规则只在未登录状态下有意义，本次为已登录浏览，不适用'
        END,
        NULL, NULL, NULL,
        jsonb_build_object(
            'schema_version', 'v1',
            'task_id', $1::uuid::text,
            'rule_id', $3::uuid::text,
            'rule_code', $5::text,
            'agent_run_id', $2::uuid::text,
            'page_capture_id', $4::uuid::text,
            'page_type', $8::text,
            'finalization_epoch', $10::integer,
            'bootstrap', false,
            'candidate_matched', false,
            'auth_requirement', $9::text
        ),
        jsonb_build_object(
            'schema_version', 'v1',
            'status', 'skipped',
            'reason', 'auth_requirement_not_met'
        ),
        now(), now()
    )
    ON CONFLICT (task_id, rule_id, page_capture_id) DO NOTHING
"""
