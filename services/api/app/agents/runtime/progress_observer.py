"""任务过程旁路观察者：把 Tool 调用流被动翻译成领域日志与页面节点状态。

设计约束（与 LiveActionTraceSink 同一姿势）：

- **只观察，不干预**：本中间件对 ``context.result`` 只读，任何内部异常都被吞掉并
  记进程日志，绝不改变 Agent 判定链路的行为；护栏拦截（``MiddlewareTermination``）
  原样透传且不产生事件。
- **fire-and-forget 落库**：每个事件在事件循环上排一个写库协程，失败只告警；
  Run 结束时 ``drain()`` 兜底等待。
- **零侵入 Agent 资产**：不读写 instructions / Skill / Tool 的任何实现，所有信息
  来自 Tool 的公开入参与返回值。

启用条件与长期状态 Store 一致：``ALIOTH_AGENT_RUN_STATE_STORE_BACKEND=postgres``
（页面节点表外键依赖 ``qc_agent_run`` 行存在）。
"""

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from agent_framework import FunctionInvocationContext, FunctionMiddleware, MiddlewareTermination

from services.api.app.agents.runtime.action_trace import normalize_result
from services.api.app.agents.runtime.working_memory import WorkingMemory, normalize_arguments
from services.api.app.application.task_progress import (
    normalize_journey_steps,
    page_type_display_name,
    severity_display,
)
from services.api.app.infrastructure.db.task_progress_store import PostgresTaskProgressStore

logger = logging.getLogger(__name__)

_RESULT_PARSE_LIMIT = 2_000_000
_ELEMENT_COUNT_MAX_DOM_BYTES = 8 * 1024 * 1024
_ELEMENT_PATTERN = re.compile(rb"<(?:a|button|input|select|textarea|img|video)\b", re.IGNORECASE)
_PROBLEM_DESC_LOG_LIMIT = 200


class TaskProgressRecorder:
    """单次 Agent Run 的过程记录器：事件入口 + fire-and-forget 写库。"""

    def __init__(
        self,
        *,
        store: PostgresTaskProgressStore,
        task_id: str,
        agent_run_id: str,
        task_config: dict[str, Any] | None = None,
        evidence_store_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._store = store
        self._task_id = task_id
        self._agent_run_id = agent_run_id
        self._task_config = task_config or {}
        self._evidence_store_factory = evidence_store_factory
        self._evidence_store: Any | None = None
        self._evidence_store_failed = False
        self._pending: set[asyncio.Task] = set()
        # 事件写入按产生顺序串行执行：fire-and-forget 协程各自持有池连接并发跑会让
        # attach/set_total 等多语句操作互相交错（实测会丢识别态与候选总数），旁路
        # 写入量小，串行即正确且足够。
        self._write_lock = asyncio.Lock()

    # ---------- Run 生命周期（由 Worker 直接 await，内部自兜底） ----------

    async def on_run_started(self) -> None:
        try:
            raw_steps = self._task_config.get("journey_steps")
            steps = normalize_journey_steps(
                [str(item) for item in raw_steps] if isinstance(raw_steps, list) else []
            )
            async with self._write_lock:
                if steps:
                    await self._store.ensure_journey_nodes(
                        task_id=self._task_id,
                        agent_run_id=self._agent_run_id,
                        steps=steps,
                    )
                await self._log("INFO", f"任务启动成功，任务ID: {self._task_id}")
                if steps:
                    journey_text = " → ".join(step.step_name for step in steps)
                    await self._log("INFO", f"开始执行用户旅程: {journey_text}")
        except Exception:  # noqa: BLE001 - 观察者失败不允许影响任务执行
            logger.warning("task_id=%s 过程观察者启动事件写入失败。", self._task_id, exc_info=True)

    async def on_run_finished(
        self,
        *,
        status: str,
        termination_reason: str | None,
        error_message: str | None,
        working_memory: dict[str, Any] | None = None,
    ) -> None:
        # 先等在途事件全部落库，再做终态收尾；否则迟到的节点更新会盖掉收尾结果。
        await self.drain()
        try:
            async with self._write_lock:
                if working_memory:
                    try:
                        await self._store.checkpoint_page_exploration(
                            task_id=self._task_id,
                            agent_run_id=self._agent_run_id,
                            working_memory=working_memory,
                        )
                    except Exception:  # noqa: BLE001 - Frontier 失败不阻断其他终态审计
                        logger.warning(
                            "task_id=%s 最终页面 Frontier 快照写入失败。",
                            self._task_id,
                            exc_info=True,
                        )
                await self._store.finalize_run_nodes(agent_run_id=self._agent_run_id)
                if status == "completed":
                    await self._log("INFO", "任务执行完成，规则收尾与最终报告已生成")
                elif status == "canceled":
                    await self._log("WARN", "任务已取消")
                else:
                    reason = error_message or termination_reason or "unknown"
                    await self._log("ERROR", f"任务执行失败: {reason}")
        except Exception:  # noqa: BLE001 - 观察者失败不允许影响任务执行
            logger.warning("task_id=%s 过程观察者终态事件写入失败。", self._task_id, exc_info=True)

    async def drain(self) -> None:
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)

    # ---------- App 探索轮次（由 App Workflow Executor 旁路调用） ----------

    def on_app_exploration_published(
        self,
        *,
        objective: str,
        max_actions: int,
        deadline_seconds: int | None = None,
    ) -> None:
        """记录 App 探索工作已发布，进入截图 Explorer 消费循环。"""
        try:
            objective_text = (objective or "").strip() or "（未命名目标）"
            if len(objective_text) > 160:
                objective_text = objective_text[:157] + "..."
            parts = [
                f"已发布 App 截图探索：目标={objective_text}",
                f"预算动作={max_actions}",
            ]
            if deadline_seconds is not None and deadline_seconds > 0:
                parts.append(f"截止={deadline_seconds}s")
            self._spawn(
                self._log(
                    "INFO",
                    "；".join(parts),
                    context={
                        "event": "app_exploration_published",
                        "max_actions": max_actions,
                        "deadline_seconds": deadline_seconds,
                    },
                )
            )
        except Exception:  # noqa: BLE001 - 观察者失败不允许影响任务执行
            logger.debug("task_id=%s on_app_exploration_published 事件构造失败。", self._task_id)

    def on_app_exploration_turn_started(
        self,
        *,
        turn_index: int,
        work_type: str,
    ) -> None:
        """记录即将领取并执行的一轮探索／Probe。"""
        try:
            label = "补采 Probe" if work_type == "probe" else "探索"
            self._spawn(
                self._log(
                    "EXEC",
                    f"开始{label}第 {turn_index} 轮",
                    context={
                        "event": "app_exploration_turn_started",
                        "turn_index": turn_index,
                        "work_type": work_type,
                    },
                )
            )
        except Exception:  # noqa: BLE001 - 观察者失败不允许影响任务执行
            logger.debug("task_id=%s on_app_exploration_turn_started 事件构造失败。", self._task_id)

    def on_app_exploration_aborted(
        self,
        *,
        phase: str,
        reason_code: str,
        error_type: str | None = None,
        error_message: str | None = None,
        retryable: bool | None = None,
        consumed_turns: int | None = None,
        consumed_actions: int | None = None,
    ) -> None:
        """App 因错误未跑完探索流程时写入执行日志（与点击坐标审计并列可检索）。"""
        try:
            phase_text = (phase or "unknown").strip() or "unknown"
            reason_text = (reason_code or "unknown").strip() or "unknown"
            parts = [
                "App 探索未跑完",
                f"阶段={phase_text}",
                f"原因={reason_text}",
            ]
            if error_type:
                parts.append(f"错误类型={error_type}")
            if error_message:
                message = error_message.strip()
                if len(message) > 160:
                    message = message[:157] + "..."
                parts.append(f"详情={message}")
            if retryable is not None:
                parts.append(f"可重试={'是' if retryable else '否'}")
            if consumed_turns is not None:
                parts.append(f"已消耗轮次={consumed_turns}")
            if consumed_actions is not None:
                parts.append(f"已消耗动作={consumed_actions}")
            self._spawn(
                self._log(
                    "ERROR",
                    "；".join(parts),
                    context={
                        "event": "app_exploration_aborted",
                        "phase": phase_text,
                        "reason_code": reason_text,
                        "error_type": error_type,
                        "error_message": error_message,
                        "retryable": retryable,
                        "consumed_turns": consumed_turns,
                        "consumed_actions": consumed_actions,
                    },
                )
            )
        except Exception:  # noqa: BLE001 - 观察者失败不允许影响任务执行
            logger.debug("task_id=%s on_app_exploration_aborted 事件构造失败。", self._task_id)

    def on_app_exploration_turn_finished(
        self,
        *,
        turn_index: int,
        work_type: str,
        claimed: bool,
        work_id: str | None = None,
        status: str | None = None,
        action_type: str | None = None,
        purpose: str | None = None,
        semantic_target: str | None = None,
        terminal_status: str | None = None,
        terminal_reason: str | None = None,
        result_capture_id: str | None = None,
        successor_work_id: str | None = None,
        error_code: str | None = None,
    ) -> None:
        """把 App Explorer 单轮结果写入执行日志，供工作台任务详情查看。"""
        try:
            if not claimed:
                self._spawn(
                    self._log(
                        "INFO",
                        f"{'补采 Probe' if work_type == 'probe' else '探索'}队列已空，停止领取",
                        context={
                            "event": "app_exploration_turn_idle",
                            "turn_index": turn_index,
                            "work_type": work_type,
                        },
                    )
                )
                return

            label = "补采 Probe" if work_type == "probe" else "探索"
            status_text = (status or "unknown").strip() or "unknown"
            level = _app_turn_log_level(status_text)
            parts = [f"{label}第 {turn_index} 轮结束：状态={status_text}"]
            if work_id:
                parts.append(f"work={work_id}")
            if action_type:
                action_bits = [action_type]
                if purpose:
                    action_bits.append(purpose[:80])
                if semantic_target:
                    action_bits.append(f"目标={semantic_target[:60]}")
                parts.append("动作=" + " / ".join(action_bits))
            if terminal_status:
                reason = (terminal_reason or "").strip()
                terminal_text = f"终态={terminal_status}"
                if reason:
                    terminal_text += f"（{reason[:120]}）"
                parts.append(terminal_text)
            if result_capture_id:
                parts.append(f"after_capture={result_capture_id}")
            if successor_work_id:
                parts.append(f"后继={successor_work_id}")
            if error_code:
                parts.append(f"error={error_code}")
            self._spawn(
                self._log(
                    level,
                    "；".join(parts),
                    page_capture_id=result_capture_id,
                    context={
                        "event": "app_exploration_turn_finished",
                        "turn_index": turn_index,
                        "work_type": work_type,
                        "work_id": work_id,
                        "status": status_text,
                        "action_type": action_type,
                        "purpose": purpose,
                        "semantic_target": semantic_target,
                        "terminal_status": terminal_status,
                        "terminal_reason": terminal_reason,
                        "result_capture_id": result_capture_id,
                        "successor_work_id": successor_work_id,
                        "error_code": error_code,
                    },
                )
            )
        except Exception:  # noqa: BLE001 - 观察者失败不允许影响任务执行
            logger.debug("task_id=%s on_app_exploration_turn_finished 事件构造失败。", self._task_id)

    # ---------- Tool 事件入口（由中间件同步调用，内部排协程） ----------

    def before_tool(self, tool_name: str, args: dict[str, Any]) -> None:
        try:
            if tool_name == "page_capture_tool":
                target = args.get("url") or args.get("selector") or "当前页面"
                mode = args.get("mode") or "goto"
                self._spawn(self._log("EXEC", f"正在采集页面: {target}（模式 {mode}）"))
            elif tool_name == "page_detection_tool":
                self._spawn(
                    self._log(
                        "EXEC",
                        "正在识别页面类型",
                    )
                )
            elif tool_name == "rule_judge_tool":
                rule_code = str(args.get("rule_code") or "")
                if rule_code:
                    self._spawn(self._log("EXEC", f"正在执行规则: {rule_code}"))
                    self._set_current_rule(args.get("page_capture_id"), rule_code)
            elif tool_name == "rule_judge_batch_tool":
                codes = _batch_rule_codes(args.get("rules"))
                if codes:
                    shown = "、".join(codes[:5])
                    suffix = f" 等 {len(codes)} 条" if len(codes) > 5 else ""
                    self._spawn(self._log("EXEC", f"正在执行规则批次: {shown}{suffix}"))
                    self._set_current_rule(args.get("page_capture_id"), codes[0])
        except Exception:  # noqa: BLE001 - 观察者失败不允许影响任务执行
            logger.debug("task_id=%s before_tool(%s) 事件构造失败。", self._task_id, tool_name)

    def after_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
        result: dict[str, Any] | None,
        duration_seconds: float,
        error_message: str | None,
        working_memory: WorkingMemory | None = None,
    ) -> None:
        try:
            if error_message is not None:
                self._spawn(self._log("ERROR", f"工具执行异常: {tool_name}: {error_message}"))
                return
            if result is None:
                return
            if tool_name == "page_capture_tool":
                self._on_page_capture(args, result)
            elif tool_name == "page_detection_tool":
                self._on_page_detection(result, duration_seconds)
            elif tool_name == "candidate_rules_tool":
                self._on_candidate_rules(result)
            elif tool_name == "record_rule_execution_tool":
                self._on_record_single(args, result)
            elif tool_name == "record_rule_executions_batch_tool":
                self._on_record_batch(args, result)
            elif tool_name == "report_generation_tool":
                self._spawn(self._log("INFO", "质检报告已生成（预览版）"))
        except Exception:  # noqa: BLE001 - 观察者失败不允许影响任务执行
            logger.debug("task_id=%s after_tool(%s) 事件构造失败。", self._task_id, tool_name)
        finally:
            if result is not None or working_memory is not None:
                try:
                    self._checkpoint_working_memory(result or {}, working_memory=working_memory)
                except Exception:  # noqa: BLE001 - 检查点构造失败也必须保持旁路
                    logger.debug(
                        "task_id=%s 页面 Frontier 检查点构造失败。",
                        self._task_id,
                        exc_info=True,
                    )

    # ---------- 各 Tool 的事件翻译 ----------

    def _on_page_capture(self, args: dict[str, Any], result: dict[str, Any]) -> None:
        if result.get("execution_status") == "blocked":
            message = result.get("message") or result.get("error_message") or "未提供原因"
            self._spawn(self._log("WARN", f"页面采集被边界策略阻断: {message}"))
            return
        page_capture_id = _uuid_or_none(result.get("page_capture_id"))
        if page_capture_id is None:
            error = result.get("error_message") or result.get("execution_status")
            self._spawn(self._log("WARN", f"页面采集未产出采集记录: {error}"))
            return
        title = result.get("title") or result.get("final_url") or page_capture_id
        mode = result.get("mode") or args.get("mode") or "goto"
        self._spawn(
            self._log(
                "INFO",
                f"页面采集完成: {title}（模式 {mode}）",
                page_capture_id=page_capture_id,
                context={"page_seq": result.get("page_seq")},
            )
        )
        self._spawn(self._count_elements(page_capture_id))

    def _on_page_detection(self, result: dict[str, Any], duration_seconds: float) -> None:
        page_capture_id = _uuid_or_none(result.get("page_capture_id"))
        detection_status = str(result.get("detection_status") or "error")
        page_type = result.get("page_type")
        confidence = result.get("confidence")
        if page_capture_id is not None:
            self._spawn(
                self._store.update_capture_detection(
                    page_capture_id=page_capture_id,
                    detection_status=detection_status,
                    page_type=page_type if isinstance(page_type, str) else None,
                    confidence=float(confidence) if isinstance(confidence, (int, float)) else None,
                    duration_ms=int(duration_seconds * 1000),
                )
            )
            display = page_type_display_name(page_type if isinstance(page_type, str) else None)
            recognition = "recognized" if detection_status == "recognized" else "failed"
            step_name = display or result.get("page_url") or "未识别页面"
            self._spawn(
                self._store.attach_capture(
                    task_id=self._task_id,
                    agent_run_id=self._agent_run_id,
                    page_capture_id=page_capture_id,
                    page_type=page_type if isinstance(page_type, str) else None,
                    step_name=str(step_name),
                    recognition_status=recognition,
                )
            )
        if detection_status == "recognized":
            display = page_type_display_name(page_type if isinstance(page_type, str) else None)
            confidence_pct = (
                f"{float(confidence) * 100:.1f}%"
                if isinstance(confidence, (int, float))
                else "未知"
            )
            confidence_label = {
                "model_candidate_self_report": "模型候选自评分",
                "deterministic_refinement_cap": "规则纠偏保守分",
                "model_self_report": "模型自评分",
                "deterministic_structure": "结构规则兜底置信度",
                "unavailable": "识别置信度",
            }.get(str(result.get("confidence_source")), "识别置信度")
            self._spawn(
                self._log(
                    "INFO",
                    f"页面识别成功: {display or page_type}（{page_type}），"
                    f"{confidence_label} {confidence_pct}，耗时 {duration_seconds:.1f}s",
                    page_capture_id=page_capture_id,
                )
            )
        else:
            reason = result.get("reason") or result.get("error_message") or "未提供原因"
            self._spawn(
                self._log(
                    "WARN",
                    f"页面识别未完成: 状态 {detection_status}，原因: {reason}",
                    page_capture_id=page_capture_id,
                )
            )

    def _on_candidate_rules(self, result: dict[str, Any]) -> None:
        page_capture_id = _uuid_or_none(result.get("page_capture_id"))
        error = result.get("error")
        if isinstance(error, dict):
            self._spawn(
                self._log(
                    "WARN",
                    f"规则匹配失败: {error.get('message') or error.get('code')}",
                    page_capture_id=page_capture_id,
                )
            )
            return
        rules = result.get("rules")
        rule_count = len(rules) if isinstance(rules, list) else 0
        plan = result.get("execution_plan")
        batch_count = len(plan.get("batches") or []) if isinstance(plan, dict) else 0
        page_type = result.get("page_type")
        display = page_type_display_name(page_type if isinstance(page_type, str) else None)
        match_message = (
            f"规则匹配完成: {display or page_type} 候选规则 {rule_count} 条，共 {batch_count} 批"
        )
        self._spawn(self._log("INFO", match_message, page_capture_id=page_capture_id))
        if page_capture_id is not None:
            self._spawn(
                self._store.attach_capture(
                    task_id=self._task_id,
                    agent_run_id=self._agent_run_id,
                    page_capture_id=page_capture_id,
                    page_type=page_type if isinstance(page_type, str) else None,
                    step_name=str(display or page_type or "未识别页面"),
                    recognition_status=None,
                )
            )
            self._spawn(
                self._store.set_candidate_total(
                    agent_run_id=self._agent_run_id,
                    page_capture_id=page_capture_id,
                    total=rule_count,
                )
            )

    def _on_record_single(self, args: dict[str, Any], result: dict[str, Any]) -> None:
        if result.get("execution_status") == "blocked" or "execution_id" not in result:
            message = _blocked_message(result)
            self._spawn(self._log("WARN", f"规则结果写回被拒: {message}"))
            return
        page_capture_id = _uuid_or_none(result.get("page_capture_id"))
        self._emit_rule_result_log(
            rule_code=str(result.get("rule_code") or args.get("rule_code") or "unknown"),
            execution_status=str(result.get("execution_status") or ""),
            severity=args.get("severity"),
            problem_desc=args.get("problem_desc"),
            page_capture_id=page_capture_id,
            rule_execution_id=_uuid_or_none(result.get("execution_id")),
        )
        self._set_current_rule(page_capture_id, None)

    def _on_record_batch(self, args: dict[str, Any], result: dict[str, Any]) -> None:
        page_capture_id = _uuid_or_none(result.get("page_capture_id"))
        detail_by_code: dict[str, dict[str, Any]] = {}
        raw_executions = args.get("executions")
        if isinstance(raw_executions, list):
            for raw in raw_executions:
                if isinstance(raw, dict) and raw.get("rule_code"):
                    detail_by_code[str(raw["rule_code"])] = raw
        items = result.get("items")
        counts = {"pass": 0, "failed": 0, "abnormal": 0, "other": 0}
        total = 0
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            if item.get("status") != "ok":
                error = item.get("error") if isinstance(item.get("error"), dict) else {}
                self._spawn(
                    self._log(
                        "WARN",
                        f"规则结果写回被拒: {item.get('rule_code')}: "
                        f"{error.get('message') or '未提供原因'}",
                        page_capture_id=page_capture_id,
                    )
                )
                continue
            total += 1
            rule_code = str(item.get("rule_code") or "unknown")
            execution_status = str(item.get("execution_status") or "")
            detail = detail_by_code.get(rule_code, {})
            self._emit_rule_result_log(
                rule_code=rule_code,
                execution_status=execution_status,
                severity=detail.get("severity"),
                problem_desc=detail.get("problem_desc"),
                page_capture_id=page_capture_id,
                rule_execution_id=_uuid_or_none(item.get("execution_id")),
            )
            if execution_status == "pass":
                counts["pass"] += 1
            elif execution_status in {"fail", "warning"}:
                counts["failed"] += 1
            elif execution_status in {"error", "manual_review", "evidence_insufficient"}:
                counts["abnormal"] += 1
            else:
                counts["other"] += 1
        if total:
            self._spawn(
                self._log(
                    "INFO",
                    f"页面规则检查完成: {counts['pass']}/{total} 通过，"
                    f"{counts['failed']} 未通过，{counts['abnormal']} 异常",
                    page_capture_id=page_capture_id,
                )
            )
        self._set_current_rule(page_capture_id, None)

    def _emit_rule_result_log(
        self,
        *,
        rule_code: str,
        execution_status: str,
        severity: Any,
        problem_desc: Any,
        page_capture_id: str | None,
        rule_execution_id: str | None,
    ) -> None:
        severity_text = severity_display(str(severity)) if severity else None
        desc = str(problem_desc)[:_PROBLEM_DESC_LOG_LIMIT] if problem_desc else ""
        suffix = f"（{severity_text}）" if severity_text else ""
        detail = f"，{desc}" if desc else ""
        if execution_status == "pass":
            level, message = "INFO", f"规则通过: {rule_code}"
        elif execution_status in {"fail", "warning"}:
            if str(severity) in {"critical", "high"}:
                level = "ERROR"
                message = f"规则高风险: {rule_code} 未通过{suffix}{detail}"
            else:
                level = "WARN"
                message = f"规则未通过: {rule_code}{suffix}{detail}"
        elif execution_status == "skipped":
            level, message = "INFO", f"规则未识别到适用场景: {rule_code}"
        elif execution_status in {"manual_review", "evidence_insufficient"}:
            level = "WARN"
            reason = "证据不足" if execution_status == "evidence_insufficient" else "转人工复核"
            message = f"规则待复核: {rule_code}（{reason}）{detail}"
        else:
            level, message = "ERROR", f"规则识别异常: {rule_code}{detail}"
        self._spawn(
            self._log(
                level,
                message,
                page_capture_id=page_capture_id,
                rule_execution_id=rule_execution_id,
            )
        )

    # ---------- 内部工具 ----------

    def _set_current_rule(self, page_capture_id: Any, rule_code: str | None) -> None:
        normalized = _uuid_or_none(page_capture_id)
        if normalized is None and rule_code is not None:
            return
        self._spawn(
            self._store.set_current_rule(
                agent_run_id=self._agent_run_id,
                page_capture_id=normalized,
                rule_code=rule_code,
            )
        )

    def _checkpoint_working_memory(
        self,
        result: dict[str, Any],
        *,
        working_memory: WorkingMemory | None = None,
    ) -> None:
        """每次 Tool 完成后保存完整页面快照，进程异常时仍保留最近进度。"""
        snapshot = (
            working_memory.audit_snapshot()
            if working_memory is not None
            else result.get("working_memory")
        )
        if not isinstance(snapshot, dict):
            return
        self._spawn(
            self._store.checkpoint_page_exploration(
                task_id=self._task_id,
                agent_run_id=self._agent_run_id,
                working_memory=snapshot,
            )
        )

    async def _count_elements(self, page_capture_id: str) -> None:
        store = self._resolve_evidence_store()
        if store is None:
            return
        dom_path = await self._store.fetch_capture_dom_path(page_capture_id=page_capture_id)
        if not dom_path:
            return
        try:
            dom_bytes = await store.read(dom_path)
        except Exception:  # noqa: BLE001 - 证据文件缺失/后端不可用时放弃计数
            logger.debug("page_capture_id=%s 读取 DOM 失败，跳过元素计数。", page_capture_id)
            return
        if not dom_bytes or len(dom_bytes) > _ELEMENT_COUNT_MAX_DOM_BYTES:
            return
        element_count = len(_ELEMENT_PATTERN.findall(dom_bytes))
        await self._store.update_capture_element_count(
            page_capture_id=page_capture_id, element_count=element_count
        )

    def _resolve_evidence_store(self) -> Any | None:
        if self._evidence_store is not None or self._evidence_store_failed:
            return self._evidence_store
        try:
            if self._evidence_store_factory is not None:
                self._evidence_store = self._evidence_store_factory()
            else:
                from services.api.app.core.config import get_settings
                from services.api.app.infrastructure.storage.factory import (
                    build_evidence_artifact_store,
                )

                self._evidence_store = build_evidence_artifact_store(
                    get_settings().evidence_storage
                )
        except Exception:  # noqa: BLE001 - 证据后端配置缺失时禁用元素计数
            self._evidence_store_failed = True
            logger.warning("过程观察者初始化证据存储失败，元素计数已禁用。", exc_info=True)
        return self._evidence_store

    async def _log(
        self,
        level: str,
        message: str,
        *,
        page_capture_id: str | None = None,
        rule_execution_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        await self._store.insert_log(
            task_id=self._task_id,
            agent_run_id=self._agent_run_id,
            level=level,
            message=message,
            page_capture_id=page_capture_id,
            rule_execution_id=rule_execution_id,
            context={key: value for key, value in (context or {}).items() if value is not None},
        )

    def _spawn(self, coroutine: Coroutine[Any, Any, None]) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            coroutine.close()
            return
        task = loop.create_task(_swallow(coroutine, self._write_lock))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)


def _app_turn_log_level(status: str) -> str:
    """把 Explorer Work 终态映射为执行日志级别。"""
    normalized = status.strip().lower()
    if normalized in {"failed", "blocked", "cancelled", "canceled"}:
        return "ERROR"
    if normalized in {"retry_wait", "budget_exhausted", "no_progress", "bounded"}:
        return "WARN"
    return "INFO"


async def _swallow(coroutine: Awaitable[None], lock: asyncio.Lock) -> None:
    try:
        async with lock:
            await coroutine
    except Exception:  # noqa: BLE001 - 观察者写库失败只告警
        logger.warning("任务过程观察者写入失败。", exc_info=True)


class TaskProgressObserverMiddleware(FunctionMiddleware):
    """旁路观察中间件：位于护栏之后、业务中间件之前，对调用链完全透明。"""

    def __init__(self, recorder: TaskProgressRecorder) -> None:
        self._recorder = recorder
        self._working_memory: WorkingMemory | None = None

    def bind_working_memory(self, working_memory: WorkingMemory) -> None:
        """绑定运行时完整工作记忆，用于旁路持久化完整 Frontier。"""
        self._working_memory = working_memory

    async def process(
        self, context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]
    ) -> None:
        tool_name = context.function.name
        args = _parse_arguments(context.arguments)
        self._recorder.before_tool(tool_name, args)
        started_at = time.monotonic()
        try:
            await call_next()
        except MiddlewareTermination:
            raise
        except Exception as exc:  # noqa: BLE001 - 记录后原样上抛，不改变异常语义
            self._recorder.after_tool(
                tool_name, args, None, time.monotonic() - started_at, str(exc)
            )
            raise
        self._recorder.after_tool(
            tool_name,
            args,
            _parse_result(context.result),
            time.monotonic() - started_at,
            None,
            working_memory=self._working_memory,
        )


def build_task_progress_recorder(
    *,
    task_id: str | None,
    agent_run_id: str,
    task_config: dict[str, Any] | None,
) -> TaskProgressRecorder | None:
    """按环境构建过程记录器；未启用 postgres 长期状态时返回 None（完全旁路）。"""
    if task_id is None:
        return None
    backend = os.getenv("ALIOTH_AGENT_RUN_STATE_STORE_BACKEND", "noop").strip().lower()
    if backend != "postgres":
        return None
    if not os.getenv("ALIOTH_DATABASE_URL"):
        return None
    return TaskProgressRecorder(
        store=PostgresTaskProgressStore(database_url=os.getenv("ALIOTH_DATABASE_URL")),
        task_id=task_id,
        agent_run_id=agent_run_id,
        task_config=task_config,
    )


def _parse_arguments(raw_arguments: Any) -> dict[str, Any]:
    try:
        payload = json.loads(normalize_arguments(raw_arguments))
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _parse_result(raw_result: Any) -> dict[str, Any] | None:
    serialized = normalize_result(raw_result, limit=_RESULT_PARSE_LIMIT)
    if serialized is None:
        return None
    try:
        payload = json.loads(serialized)
    except (json.JSONDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _batch_rule_codes(raw_rules: Any) -> list[str]:
    codes: list[str] = []
    for rule in raw_rules if isinstance(raw_rules, list) else []:
        if isinstance(rule, dict) and rule.get("rule_code"):
            codes.append(str(rule["rule_code"]))
    return codes


def _blocked_message(result: dict[str, Any]) -> str:
    error = result.get("error")
    if isinstance(error, dict) and error.get("message"):
        return str(error["message"])
    return str(result.get("message") or result.get("error_message") or "未提供原因")


def _uuid_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
