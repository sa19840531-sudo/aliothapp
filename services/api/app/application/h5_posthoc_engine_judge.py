"""收口后确定性补判：把已留存的交互前后配对采集喂给现有确定性 Handler。

活会话依赖规则中存在一个确定性子集（如弹窗关闭前后截图对比），其证据在探索
期间已按 before_action/after_action 配对留存，判定却因活会话批次无消费面而
整场跳过。本通道在收口补记跳过之前，把这些配对按规则契约的角色词汇映射后
喂给统一判定入口（judge_and_record 同一受监督链路），判定与落库契约零旁路。

范围刻意收敛：仅 executor_type=rule_engine 且 handler 属白名单的规则；
单点失败只损失该条补判，不得阻断收口。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from services.api.app.infrastructure.db.inspection_scope_sql import (
    page_type_in_inspection_scope_sql,
    rule_in_inspection_scope_sql,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool

logger = logging.getLogger(__name__)

# 首批白名单：前后截图对比类。扩白名单前必须确认证据可由被动探索留存。
_SUPPORTED_HANDLERS = frozenset({"screenshot_change_assertion"})
_COVERED_STATUSES = ("pass", "fail", "manual_review")
_MAX_PAIRS_PER_RULE = 3

# 契约角色 -> 留存采集角色。
_ROLE_CAPTURE_MAPPING = {
    "before": "before_action",
    "primary": "after_action",
}


@dataclass(frozen=True)
class H5PosthocTargetRule:
    """补判目标规则的最小快照。"""

    rule_code: str
    applicable_page_types: tuple[str, ...]
    required_page_states: tuple[str, ...]
    before_role: str
    after_role: str


@dataclass(frozen=True)
class H5PosthocActionPair:
    """同一动作的前后配对采集。"""

    action_execution_id: str
    before_capture_id: str
    after_capture_id: str
    page_types: tuple[str, ...]
    page_states: tuple[str, ...]


@dataclass(frozen=True)
class H5PosthocJudgementSummary:
    """单次补判通道运行摘要。"""

    target_rule_count: int
    judged_pair_count: int
    covered_rule_codes: tuple[str, ...]


class H5PosthocEvidenceSource(Protocol):
    """补判取数端口。"""

    async def is_h5_full_inspection(self, *, task_id: str) -> bool: ...

    async def load_target_rules(self, *, task_id: str) -> list[H5PosthocTargetRule]: ...

    async def load_action_pairs(self, *, task_id: str) -> list[H5PosthocActionPair]: ...


class PostgresH5PosthocEvidenceSource:
    """从任务留存证据读取补判目标与配对采集。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def _get_pool(self) -> Any:
        return self._pool or await get_postgres_pool(self._database_url)

    async def is_h5_full_inspection(self, *, task_id: str) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT target_platform, task_config
                FROM qc_task
                WHERE id = $1::uuid
                """,
                task_id,
            )
        if row is None or str(row["target_platform"] or "").casefold() not in {"h5", "web"}:
            return False
        config = row["task_config"]
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except json.JSONDecodeError:
                config = {}
        if not isinstance(config, Mapping):
            config = {}
        return str(config.get("execution_mode") or "full_inspection") == "full_inspection"

    async def load_target_rules(self, *, task_id: str) -> list[H5PosthocTargetRule]:
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                f"""
                SELECT rule.rule_code,
                       rule.applicable_page_types,
                       rule.judge_config
                FROM qc_rule AS rule
                INNER JOIN qc_task AS task
                    ON task.id = $1::uuid
                   AND rule.ruleset_id = ANY(qc_ruleset_scope(task.ruleset_id))
                WHERE rule.status = 'enabled'
                  AND rule.executor_type = 'rule_engine'
                  AND {rule_in_inspection_scope_sql("task.task_config", "rule")}
                  AND NOT EXISTS (
                      SELECT 1
                      FROM qc_rule_execution AS execution
                      WHERE execution.task_id = task.id
                        AND execution.rule_id = rule.id
                        AND execution.execution_status = ANY($2::varchar[])
                  )
                """,
                task_id,
                list(_COVERED_STATUSES),
            )
        targets: list[H5PosthocTargetRule] = []
        for row in rows:
            judge_config = _as_mapping(row["judge_config"])
            engine_config = _as_mapping(judge_config.get("engine_config"))
            handler = str(engine_config.get("handler") or "")
            if handler not in _SUPPORTED_HANDLERS:
                continue
            targets.append(
                H5PosthocTargetRule(
                    rule_code=str(row["rule_code"]),
                    applicable_page_types=_as_text_tuple(row["applicable_page_types"]),
                    required_page_states=_as_text_tuple(
                        engine_config.get("required_page_states")
                    ),
                    before_role=str(engine_config.get("before_role") or "before"),
                    after_role=str(engine_config.get("after_role") or "primary"),
                )
            )
        return targets

    async def load_action_pairs(self, *, task_id: str) -> list[H5PosthocActionPair]:
        before_in_scope = page_type_in_inspection_scope_sql(
            "task.task_config", "before_capture.page_type"
        )
        after_in_scope = page_type_in_inspection_scope_sql(
            "task.task_config", "after_capture.page_type"
        )
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                f"""
                SELECT before_capture.action_execution_id::text AS action_execution_id,
                       before_capture.id::text AS before_capture_id,
                       after_capture.id::text AS after_capture_id,
                       before_capture.page_type AS before_page_type,
                       after_capture.page_type AS after_page_type,
                       before_capture.page_state AS before_page_state,
                       after_capture.page_state AS after_page_state
                FROM qc_page_capture AS before_capture
                INNER JOIN qc_page_capture AS after_capture
                    ON after_capture.action_execution_id
                       = before_capture.action_execution_id
                   AND after_capture.task_id = before_capture.task_id
                   AND after_capture.capture_role = 'after_action'
                INNER JOIN qc_task AS task ON task.id = before_capture.task_id
                WHERE before_capture.task_id = $1::uuid
                  AND before_capture.capture_role = 'before_action'
                  AND before_capture.action_execution_id IS NOT NULL
                  -- 经过的页面不判规则：前后两张都得是质检范围内的页面
                  AND {before_in_scope}
                  AND {after_in_scope}
                  AND EXISTS (
                      SELECT 1
                      FROM qc_evidence_artifact AS evidence
                      WHERE evidence.page_capture_id = before_capture.id
                        AND evidence.artifact_type = 'screenshot'
                  )
                  AND EXISTS (
                      SELECT 1
                      FROM qc_evidence_artifact AS evidence
                      WHERE evidence.page_capture_id = after_capture.id
                        AND evidence.artifact_type = 'screenshot'
                  )
                ORDER BY after_capture.created_at DESC
                """,
                task_id,
            )
        pairs: list[H5PosthocActionPair] = []
        for row in rows:
            pairs.append(
                H5PosthocActionPair(
                    action_execution_id=str(row["action_execution_id"]),
                    before_capture_id=str(row["before_capture_id"]),
                    after_capture_id=str(row["after_capture_id"]),
                    page_types=_as_present_tuple(
                        row["before_page_type"], row["after_page_type"]
                    ),
                    page_states=_as_present_tuple(
                        row["before_page_state"], row["after_page_state"]
                    ),
                )
            )
        return pairs


JudgeAndRecord = Callable[..., Awaitable[dict[str, Any]]]


class H5PosthocEngineJudgeService:
    """收口前的确定性补判编排。"""

    def __init__(
        self,
        *,
        evidence_source: H5PosthocEvidenceSource,
        judge_and_record: JudgeAndRecord,
    ) -> None:
        self._evidence_source = evidence_source
        self._judge_and_record = judge_and_record

    async def judge_pending(self, *, task_id: str) -> H5PosthocJudgementSummary:
        """对无覆盖的白名单规则逐条尝试配对补判；调用方须已绑定 Run 上下文。"""
        if not await self._evidence_source.is_h5_full_inspection(task_id=task_id):
            return H5PosthocJudgementSummary(0, 0, ())
        targets = await self._evidence_source.load_target_rules(task_id=task_id)
        if not targets:
            return H5PosthocJudgementSummary(0, 0, ())
        pairs = await self._evidence_source.load_action_pairs(task_id=task_id)
        judged_pair_count = 0
        covered: list[str] = []
        for target in targets:
            matched = _matching_pairs(target, pairs)[:_MAX_PAIRS_PER_RULE]
            for pair in matched:
                try:
                    outcome = await self._judge_and_record(
                        rule_code=target.rule_code,
                        page_capture_id=pair.after_capture_id,
                        evidence_artifact_ids=[],
                        judge_input={},
                        evidence_capture_roles={
                            target.before_role: pair.before_capture_id,
                            target.after_role: pair.after_capture_id,
                        },
                    )
                except Exception:  # noqa: BLE001 - 单条补判失败不得阻断收口
                    logger.exception(
                        "task_id=%s rule=%s pair=%s 收口确定性补判失败。",
                        task_id,
                        target.rule_code,
                        pair.action_execution_id,
                    )
                    continue
                judged_pair_count += 1
                status = str(
                    (outcome or {}).get("execution_status") or ""
                ).strip()
                if status in ("pass", "fail"):
                    covered.append(target.rule_code)
                    break
        return H5PosthocJudgementSummary(
            target_rule_count=len(targets),
            judged_pair_count=judged_pair_count,
            covered_rule_codes=tuple(covered),
        )


def _matching_pairs(
    target: H5PosthocTargetRule,
    pairs: list[H5PosthocActionPair],
) -> list[H5PosthocActionPair]:
    applicable = {item.casefold() for item in target.applicable_page_types}
    required_states = {item.casefold() for item in target.required_page_states}
    matched: list[H5PosthocActionPair] = []
    for pair in pairs:
        if applicable and not applicable.intersection(
            item.casefold() for item in pair.page_types
        ):
            continue
        if required_states and not required_states.intersection(
            item.casefold() for item in pair.page_states
        ):
            continue
        matched.append(pair)
    return matched


def build_default_h5_posthoc_engine_judge_service(
    *,
    judge_and_record: JudgeAndRecord,
) -> H5PosthocEngineJudgeService:
    """Worker 收口组合根入口；判定回调由调用方注入以避免层次倒挂。"""
    return H5PosthocEngineJudgeService(
        evidence_source=PostgresH5PosthocEvidenceSource(),
        judge_and_record=judge_and_record,
    )


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value if isinstance(value, Mapping) else {}


def _as_text_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return ()
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _as_present_tuple(*values: Any) -> tuple[str, ...]:
    return tuple(str(item).strip() for item in values if str(item or "").strip())
