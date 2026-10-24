"""将候选规则批次幂等发布为 enforced Inspection Work Item。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from services.api.app.agents.runtime.business_middlewares.base import (
    TaskRuntimeSnapshot,
)
from services.api.app.application.inspection_work_items import (
    InspectionWorkItemRule,
    InspectionWorkItemService,
    PublishInspectionWorkItemCommand,
    build_inspection_rule_contract_hash,
)
from services.api.app.application.rule_batching import MAX_RULE_JUDGE_BATCH_SIZE
from services.api.app.core.config import get_settings


@dataclass(frozen=True, kw_only=True)
class PublishedInspectionBatchSummary:
    """一次候选结果发布后的结构化摘要。"""

    status: str
    published_count: int
    items: tuple[dict[str, Any], ...] = ()
    commands: tuple[PublishInspectionWorkItemCommand, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "published_count": self.published_count,
            "items": list(self.items),
        }


def prepare_enforced_batch_commands_from_candidate_result(
    *,
    snapshot: TaskRuntimeSnapshot,
    task_id: str,
    agent_run_id: str,
    page_capture_id: str,
    rules: Sequence[Mapping[str, Any]],
    batches: Sequence[Mapping[str, Any]],
    control_mode: str = "enforced",
    defer_wakeup: bool = False,
) -> tuple[PublishInspectionWorkItemCommand, ...]:
    """把候选规则计划编译为稳定命令，供普通发布或 Publication 单事务发布复用。"""

    task = str(task_id or "").strip()
    run_id = str(agent_run_id or "").strip()
    capture_id = str(page_capture_id or "").strip()
    if not task or not run_id or not capture_id:
        raise ValueError("发布质检工作项缺少 task_id、agent_run_id 或 page_capture_id")
    if snapshot.ruleset_id is None or not str(snapshot.ruleset_id).strip():
        raise RuntimeError("任务锁定规则集快照不存在")
    if not rules or not batches:
        return ()

    rules_by_code = {
        str(rule.get("rule_code") or ""): dict(rule)
        for rule in rules
        if str(rule.get("rule_code") or "").strip()
    }
    commands: list[PublishInspectionWorkItemCommand] = []
    max_attempts = get_settings().inspection_work_items.max_attempts
    for batch in batches:
        base_batch_key = str(batch.get("batch_id") or "").strip()
        rule_codes = [
            str(value).strip() for value in batch.get("rule_codes") or [] if str(value).strip()
        ]
        batch_rules = [rules_by_code[rule_code] for rule_code in rule_codes]
        from services.api.app.application.web_rule_evidence_contract import (
            read_web_rule_evidence_contract,
        )

        for rule in batch_rules:
            if rule.get("evidence_input_set_id") is not None and snapshot.target_platform != "h5":
                raise ValueError("普通原页面 InputSet 传递仅用于 H5；Web 序列使用专属发布入口")
            web_contract = read_web_rule_evidence_contract(rule.get("input_contract") or {})
            if web_contract is not None and "stable_page" not in web_contract.accepted_roles:
                raise ValueError("Web 普通发布材料必须由规则明确接受 stable_page")
        if not base_batch_key or not batch_rules or len(batch_rules) != len(rule_codes):
            raise RuntimeError("候选规则 execution_plan 批次不完整")
        session_dependency = str(batch.get("session_dependency") or "snapshot").strip()
        chunks = [
            batch_rules[start : start + MAX_RULE_JUDGE_BATCH_SIZE]
            for start in range(0, len(batch_rules), MAX_RULE_JUDGE_BATCH_SIZE)
        ]
        for chunk_index, chunk_rules in enumerate(chunks, start=1):
            batch_key = (
                base_batch_key if len(chunks) == 1 else f"{base_batch_key}:chunk:{chunk_index}"
            )
            commands.append(
                PublishInspectionWorkItemCommand(
                    task_id=task,
                    page_capture_id=capture_id,
                    producer_agent_run_id=run_id,
                    session_owner_agent_run_id=(
                        run_id if session_dependency == "active_session" else None
                    ),
                    ruleset_id=snapshot.ruleset_id,
                    ruleset_version=snapshot.ruleset_version,
                    ruleset_snapshot_hash=snapshot.ruleset_snapshot_hash,
                    batch_key=batch_key,
                    strategy=str(batch.get("strategy") or "").strip(),
                    category=_optional_text(batch.get("category")),
                    session_dependency=session_dependency,
                    rules=tuple(
                        InspectionWorkItemRule(
                            rule_id=str(rule.get("rule_id") or "").strip(),
                            rule_code=str(rule.get("rule_code") or "").strip(),
                            ordinal=index,
                            contract_hash=build_inspection_rule_contract_hash(rule),
                            evidence_input_set_id=rule.get("evidence_input_set_id"),
                        )
                        for index, rule in enumerate(chunk_rules)
                    ),
                    priority=snapshot.priority,
                    max_attempts=max_attempts,
                    control_mode=control_mode,
                    defer_wakeup=defer_wakeup,
                )
            )
    return tuple(commands)


async def publish_enforced_batches_from_candidate_result(
    *,
    service: InspectionWorkItemService,
    snapshot: TaskRuntimeSnapshot,
    task_id: str,
    agent_run_id: str,
    page_capture_id: str,
    rules: Sequence[Mapping[str, Any]],
    batches: Sequence[Mapping[str, Any]],
    control_mode: str = "enforced",
    defer_wakeup: bool = False,
) -> PublishedInspectionBatchSummary:
    """把候选规则 execution_plan 批次发布为持久化工作项。"""
    commands = prepare_enforced_batch_commands_from_candidate_result(
        snapshot=snapshot,
        task_id=task_id,
        agent_run_id=agent_run_id,
        page_capture_id=page_capture_id,
        rules=rules,
        batches=batches,
        control_mode=control_mode,
        defer_wakeup=defer_wakeup,
    )
    if not commands:
        return PublishedInspectionBatchSummary(
            status="not_applicable",
            published_count=0,
        )
    published_items: list[dict[str, Any]] = []
    for command in commands:
        published = await service.publish(command)
        published_items.append(
            {
                "work_item_id": published.work_item_id,
                "batch_key": command.batch_key,
                "status": published.status.value,
                "created": published.created,
            }
        )
    return PublishedInspectionBatchSummary(
        status="published",
        published_count=len(published_items),
        items=tuple(published_items),
        commands=commands,
    )


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None
