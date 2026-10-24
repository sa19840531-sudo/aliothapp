"""动态页面探索写仓储：持久化 Frontier 动作与真实页面质检终态。"""

import json
from typing import Any

from services.api.app.infrastructure.db.pool import get_postgres_pool


class PostgresPageExplorationRepository:
    """把 WorkingMemory 中可变的探索快照幂等写入 PostgreSQL。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def checkpoint(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        working_memory: dict[str, Any],
    ) -> None:
        """在一个事务内持久化动态 Frontier 与真实页面质检状态。"""
        actions = _exploration_actions(working_memory)
        pages = _page_snapshots(working_memory)
        if not actions and not pages:
            return
        pool = await self._get_pool()
        actions_by_id = {
            action["exploration_action_id"]: action for action in actions
        }
        async with pool.acquire() as connection, connection.transaction():
            for action in actions:
                await connection.execute(
                    """
                    INSERT INTO qc_page_exploration_action (
                        task_id, agent_run_id, exploration_action_id,
                        source_page_capture_id, source_page_type, action, origin,
                        target, target_keywords, risk_level, purpose,
                        expected_next_page_type, navigation_depth, status,
                        result_page_capture_id, failure_reason, failure_code,
                        failure_message, failure_retryable, attempt_count
                    )
                    VALUES (
                        $1::uuid, $2::uuid, $3, $4::uuid, $5, $6, $7,
                        $8, $9::jsonb, $10, $11, $12, $13, $14,
                        $15::uuid, $16, $17, $18, $19, $20
                    )
                    ON CONFLICT (agent_run_id, exploration_action_id) DO UPDATE SET
                        source_page_capture_id = EXCLUDED.source_page_capture_id,
                        source_page_type = EXCLUDED.source_page_type,
                        action = EXCLUDED.action,
                        origin = EXCLUDED.origin,
                        target = EXCLUDED.target,
                        target_keywords = EXCLUDED.target_keywords,
                        risk_level = EXCLUDED.risk_level,
                        purpose = EXCLUDED.purpose,
                        expected_next_page_type = EXCLUDED.expected_next_page_type,
                        navigation_depth = EXCLUDED.navigation_depth,
                        status = EXCLUDED.status,
                        result_page_capture_id = EXCLUDED.result_page_capture_id,
                        failure_reason = EXCLUDED.failure_reason,
                        failure_code = EXCLUDED.failure_code,
                        failure_message = EXCLUDED.failure_message,
                        failure_retryable = EXCLUDED.failure_retryable,
                        attempt_count = EXCLUDED.attempt_count,
                        updated_at = now()
                    """,
                    task_id,
                    agent_run_id,
                    action["exploration_action_id"],
                    action.get("source_page_capture_id"),
                    action.get("source_page_type"),
                    action.get("action"),
                    action.get("origin") or "discovered",
                    action.get("target"),
                    json.dumps(
                        action.get("target_keywords") or [],
                        ensure_ascii=False,
                        default=str,
                    ),
                    action.get("risk_level"),
                    action.get("purpose"),
                    action.get("expected_next_page_type"),
                    action.get("navigation_depth"),
                    action.get("status"),
                    action.get("result_page_capture_id"),
                    action.get("failure_reason"),
                    action.get("failure_code"),
                    action.get("failure_message"),
                    action.get("failure_retryable"),
                    _non_negative_int(action.get("attempt_count")),
                )
            for page in pages:
                failure = page.get("failure")
                failure_payload = failure if isinstance(failure, dict) else {}
                await connection.execute(
                    """
                    UPDATE qc_page_capture
                    SET
                        parent_page_capture_id = COALESCE(
                            parent_page_capture_id, $4::uuid
                        ),
                        inspection_lifecycle_status = $5,
                        inspection_status = $6,
                        inspection_failure_stage = $7,
                        inspection_failure_code = $8,
                        inspection_failure_message = $9,
                        inspection_failure_retryable = $10,
                        inspection_abandon_reason = $11,
                        updated_at = now()
                    WHERE id = $1::uuid
                      AND task_id = $2::uuid
                      AND agent_run_id = $3::uuid
                    """,
                    page["page_capture_id"],
                    task_id,
                    agent_run_id,
                    page.get("supersedes_page_capture_id"),
                    page.get("lifecycle_status") or "active",
                    page.get("inspection_status") or "captured",
                    failure_payload.get("stage"),
                    failure_payload.get("code"),
                    failure_payload.get("message"),
                    failure_payload.get("retryable"),
                    page.get("abandon_reason"),
                )
                if _is_supplemental_capture(page, actions_by_id):
                    await connection.execute(
                        """
                        UPDATE qc_task_page_node
                        SET
                            page_capture_id = $2::uuid,
                            updated_at = now()
                        WHERE agent_run_id = $1::uuid
                          AND page_capture_id = $3::uuid
                        """,
                        agent_run_id,
                        page["page_capture_id"],
                        page.get("supersedes_page_capture_id"),
                    )
                await connection.execute(
                    """
                    UPDATE qc_task_page_node
                    SET
                        status = CASE
                            WHEN $4 = 'recognized'
                                 AND $3 IN ('completed', 'degraded', 'failed')
                                THEN 'recognized'
                            WHEN $4 IN ('error', 'insufficient', 'ambiguous')
                                 AND status = 'visiting'
                                THEN 'failed'
                            ELSE status
                        END,
                        finished_at = CASE
                            WHEN $3 IN ('completed', 'degraded', 'failed')
                                THEN COALESCE(finished_at, now())
                            ELSE finished_at
                        END,
                        updated_at = now()
                    WHERE agent_run_id = $1::uuid
                      AND page_capture_id = $2::uuid
                    """,
                    agent_run_id,
                    page["page_capture_id"],
                    page.get("inspection_status") or "captured",
                    (
                        page.get("page_detection", {}).get("detection_status")
                        if isinstance(page.get("page_detection"), dict)
                        else None
                    ),
                )

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


def _exploration_actions(working_memory: dict[str, Any]) -> list[dict[str, Any]]:
    frontier = working_memory.get("exploration_frontier")
    if not isinstance(frontier, dict):
        return []
    actions: dict[str, dict[str, Any]] = {}
    for key in ("pending_actions", "explored_actions", "inactive_actions"):
        raw_actions = frontier.get(key)
        for raw_action in raw_actions if isinstance(raw_actions, list) else []:
            if not isinstance(raw_action, dict):
                continue
            action_id = _optional_text(raw_action.get("exploration_action_id"))
            action_name = _optional_text(raw_action.get("action"))
            status = _optional_text(raw_action.get("status"))
            if action_id is None or action_name is None or status is None:
                continue
            actions[action_id] = dict(raw_action)
    return list(actions.values())


def _page_snapshots(working_memory: dict[str, Any]) -> list[dict[str, Any]]:
    pages: dict[str, dict[str, Any]] = {}
    raw_current = working_memory.get("current_page")
    candidates: list[Any] = [raw_current] if isinstance(raw_current, dict) else []
    for key in ("inspected_pages", "terminal_pages", "pending_pages", "inactive_pages"):
        raw_pages = working_memory.get(key)
        if isinstance(raw_pages, list):
            candidates.extend(raw_pages)
    for raw_page in candidates:
        if not isinstance(raw_page, dict):
            continue
        page_capture_id = _optional_text(raw_page.get("page_capture_id"))
        if page_capture_id is not None:
            pages[page_capture_id] = dict(raw_page)
    return list(pages.values())


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _is_supplemental_capture(
    page: dict[str, Any], actions_by_id: dict[str, dict[str, Any]]
) -> bool:
    """滚动与定时补采属于同一逻辑页面，页面节点必须迁移到最新证据版本。"""
    action_id = _optional_text(page.get("exploration_action_id"))
    source_capture_id = _optional_text(page.get("supersedes_page_capture_id"))
    if action_id is None or source_capture_id is None:
        return False
    action = actions_by_id.get(action_id)
    return bool(
        action
        and action.get("action") in {"scroll", "timed_capture"}
        and action.get("source_page_capture_id") == source_capture_id
        and action.get("result_page_capture_id") == page.get("page_capture_id")
        and action.get("status") == "explored"
    )


def _non_negative_int(value: Any) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0
