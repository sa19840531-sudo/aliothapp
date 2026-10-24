"""页面质检工作记忆：维护当前页、识别门禁、已完成页与页面结果。"""

import json
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from agent_framework import FunctionInvocationContext, FunctionMiddleware

from services.api.app.agents.runtime.business_middlewares.base import extract_arguments
from services.api.app.agents.runtime.page_inspection_memory import (
    TRANSIENT_PAGE_STATES,
    TRANSIENT_PAGE_TYPES,
)
from services.api.app.agents.runtime.run_context import get_agent_run_context
from services.api.app.agents.runtime.working_memory import WorkingMemory
from services.api.app.core.config import get_settings
from services.api.app.domain.exploration.page_type_assessment import (
    PageTypeAssessmentValidationError,
)

_PAGE_FLOW_BLOCKING_EVIDENCE_FAILURE_CODES = frozenset(
    {
        "EVIDENCE_REQUIREMENT_PUBLISH_FAILED",
    }
)


class PageInspectionMemoryMiddleware(FunctionMiddleware):
    """在 Tool 成功后更新页面工作记忆，并向模型返回紧凑进度摘要。

    页面采集、页面识别、候选规则匹配和规则结果写回仍由各自业务 Tool 负责；
    本中间件只观察这些结构化结果，不写数据库，也不从自然语言最终回答猜测执行状态。
    """

    def __init__(
        self,
        working_memory: WorkingMemory,
        *,
        page_capture_tool: str = "page_capture_tool",
        page_detection_tool: str = "page_detection_tool",
        candidate_rules_tool: str = "candidate_rules_tool",
        rule_judge_tool: str = "rule_judge_tool",
        rule_judge_batch_tool: str = "rule_judge_batch_tool",
        record_rule_execution_tool: str = "record_rule_execution_tool",
        record_rule_executions_batch_tool: str = "record_rule_executions_batch_tool",
        exploration_closure_tool: str = "exploration_closure_tool",
        report_tool: str = "report_generation_tool",
    ) -> None:
        self._working_memory = working_memory
        self._page_capture_tool = page_capture_tool
        self._page_detection_tool = page_detection_tool
        self._candidate_rules_tool = candidate_rules_tool
        self._rule_judge_tool = rule_judge_tool
        self._rule_judge_batch_tool = rule_judge_batch_tool
        self._record_rule_execution_tool = record_rule_execution_tool
        self._record_rule_executions_batch_tool = record_rule_executions_batch_tool
        self._exploration_closure_tool = exploration_closure_tool
        self._report_tool = report_tool
        self._completed_coverage_page_capture_ids: set[str] = set()
        self._completed_temporal_page_capture_ids: set[str] = set()

    async def process(
        self, context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]
    ) -> None:
        tool_name = context.function.name
        arguments = extract_arguments(context.arguments)
        if tool_name == self._page_detection_tool:
            requested_capture_id = _text(arguments.get("page_capture_id"))
            if requested_capture_id is None or not self._working_memory.has_page_capture(
                requested_capture_id
            ):
                context.result = {
                    "execution_status": "blocked",
                    "error_code": "WORKING_MEMORY_PAGE_NOT_FOUND",
                    "retryable": True,
                    "requested_page_capture_id": requested_capture_id,
                    "message": (
                        "页面识别必须绑定当前 Run 的真实采集；"
                        "请使用 working_memory.current_page.page_capture_id，禁止编造 ID。"
                    ),
                    "working_memory": self._model_visible_working_memory(),
                }
                return
            blocking_failure = self._blocking_page_flow_failure(
                page_capture_id=requested_capture_id
            )
            if blocking_failure is not None:
                self._terminate_failed_page_flow_retry(context, blocking_failure)
                return
            if self._working_memory.has_started_page_inspection(requested_capture_id):
                context.result = {
                    "execution_status": "blocked",
                    "error_code": "PAGE_DETECTION_LOCKED_AFTER_RULE_MATCHING",
                    "retryable": False,
                    "page_capture_id": requested_capture_id,
                    "message": (
                        "当前采集已开始候选规则执行，页面识别结论已锁定；"
                        "禁止对同一 page_capture_id 重新识别并改写页面类型。"
                    ),
                    "working_memory": self._model_visible_working_memory(),
                }
                return
        if tool_name == self._page_capture_tool:
            agent_runtime = getattr(get_settings(), "agent_runtime", None)
            failure_limit = getattr(
                agent_runtime,
                "max_consecutive_infrastructure_failures",
                3,
            )
            if self._working_memory.consecutive_infrastructure_failures >= failure_limit:
                context.result = {
                    "status": "error",
                    "execution_status": "error",
                    "handled": True,
                    "failure_scope": "run",
                    "failure_kind": "infrastructure",
                    "failure_stage": "page_capture",
                    "error_code": "INFRASTRUCTURE_UNAVAILABLE",
                    "error_message": "连续页面基础设施故障达到阈值，已停止继续调用页面采集。",
                    "retryable": False,
                    "retry_exhausted": True,
                    "working_memory": self._working_memory.model_snapshot(),
                }
                return
            arguments = self._enrich_mandatory_page_capture(context, arguments)
            blocking_failure = self._blocking_page_flow_failure()
            exploration_action_id = _text(arguments.get("exploration_action_id"))
            if (
                blocking_failure is not None
                and not self._working_memory.is_exploration_action_runnable(
                    exploration_action_id
                )
            ):
                self._terminate_failed_page_flow_retry(context, blocking_failure)
                return
            pending_detection_id = self._working_memory.pending_detection_page_capture_id()
            if (
                pending_detection_id is not None
                and _text(arguments.get("exploration_action_id")) is None
            ):
                context.result = {
                    "execution_status": "blocked",
                    "error_code": "PAGE_DETECTION_REQUIRED",
                    "retryable": True,
                    "page_capture_id": pending_detection_id,
                    "message": (
                        "当前新采集页面尚未识别；请先调用 "
                        f"page_detection_tool(page_capture_id={pending_detection_id})，"
                        "禁止继续盲目补采。"
                    ),
                    "working_memory": self._model_visible_working_memory(),
                }
                return
            block_reason = self._working_memory.mandatory_action_block_reason(
                _text(arguments.get("exploration_action_id"))
            )
            if block_reason is not None:
                context.result = {
                    "execution_status": "blocked",
                    "error_code": "SOURCE_PAGE_INSPECTION_REQUIRED",
                    "retryable": True,
                    "message": block_reason,
                    "working_memory": self._model_visible_working_memory(),
                }
                return
            if (
                _text(arguments.get("exploration_action_id")) is None
                and self._working_memory.has_incomplete_page_inspection()
            ):
                context.result = {
                    "execution_status": "blocked",
                    "error_code": "PAGE_INSPECTION_IN_PROGRESS",
                    "retryable": True,
                    "message": (
                        "当前页面仍有识别、候选规则或规则判定未完成；"
                        "请按 working_memory 继续页面识别、规则执行或已绑定的 Frontier 动作，"
                        "禁止无 exploration_action_id 盲目补采。"
                    ),
                    "working_memory": self._model_visible_working_memory(),
                }
                return
        capture_action_id = (
            self._begin_page_capture_attempt(arguments)
            if tool_name == self._page_capture_tool
            else None
        )
        if tool_name == self._candidate_rules_tool:
            page_capture_id = _text(arguments.get("page_capture_id"))
            if page_capture_id is None or not self._working_memory.has_page_capture(
                page_capture_id
            ):
                context.result = {
                    **_page_not_found_result(page_capture_id),
                    "working_memory": self._model_visible_working_memory(),
                }
                return
            page_type = _text(arguments.get("page_type"))
            detection_result = self._working_memory.page_detection_result(page_capture_id)
            if detection_result is not None and arguments.get("page_state") is None:
                arguments = {
                    **arguments,
                    "page_state": _text(detection_result.get("page_state")),
                }
                context.arguments = arguments
            gate_result = _page_detection_gate_result(
                page_capture_id=page_capture_id,
                requested_page_type=page_type,
                detection_result=detection_result,
            )
            if gate_result is None and self._working_memory.has_blocking_mandatory_page_action(
                page_capture_id
            ):
                gate_result = {
                    "execution_status": "blocked",
                    "error_code": "PAGE_OBSTRUCTION_RECOVERY_REQUIRED",
                    "retryable": True,
                    "page_capture_id": page_capture_id,
                    "message": (
                        "当前页面仍有必须先关闭的遮挡层；请执行 working_memory 中对应的 "
                        "mandatory Frontier 动作并重新采集识别，再查询候选规则。"
                    ),
                }
            if gate_result is not None:
                context.result = {
                    **gate_result,
                    "working_memory": self._model_visible_working_memory(),
                }
                return
        elif tool_name in {
            self._rule_judge_tool,
            self._rule_judge_batch_tool,
            self._record_rule_execution_tool,
            self._record_rule_executions_batch_tool,
        }:
            page_capture_id = _text(arguments.get("page_capture_id"))
            if (
                tool_name in {self._rule_judge_tool, self._rule_judge_batch_tool}
                and page_capture_id is not None
                and self._working_memory.has_blocking_mandatory_page_action(page_capture_id)
            ):
                context.result = {
                    "execution_status": "blocked",
                    "error_code": "PLANNED_EVIDENCE_CAPTURE_REQUIRED",
                    "retryable": True,
                    "page_capture_id": page_capture_id,
                    "message": (
                        "当前规则需要先完成滚动或时序证据采集；"
                        "请执行 working_memory 中的 mandatory Frontier 动作。"
                    ),
                    "working_memory": self._model_visible_working_memory(),
                }
                return
            if page_capture_id is not None and not self._working_memory.has_started_page_inspection(
                page_capture_id
            ):
                context.result = {
                    "execution_status": "blocked",
                    "error_code": "PAGE_INSPECTION_NOT_STARTED",
                    "retryable": True,
                    "page_capture_id": page_capture_id,
                    "message": (
                        "该页面尚未完成候选规则匹配；请先使用同一 page_capture_id 调用 "
                        "candidate_rules_tool，再写入页面规则结果。"
                    ),
                    "working_memory": self._model_visible_working_memory(),
                }
                return
            if tool_name == self._rule_judge_tool and page_capture_id is not None:
                existing_result = self._working_memory.page_rule_result(
                    page_capture_id=page_capture_id,
                    rule_id=_text(arguments.get("rule_id")),
                    rule_code=_text(arguments.get("rule_code")),
                )
                if existing_result is not None:
                    context.result = {
                        "status": "ok",
                        **existing_result,
                        "page_capture_id": page_capture_id,
                        "already_terminal": True,
                        "retryable": False,
                        "message": (
                            "该规则已形成终态，本次未重复执行；"
                            "请按 working_memory 继续下一条规则或页面动作。"
                        ),
                        "working_memory": self._model_visible_working_memory(),
                    }
                    return

        await call_next()
        result = _result_mapping(context.result)
        if result is None:
            return
        if tool_name in {
            self._page_capture_tool,
            self._page_detection_tool,
            self._candidate_rules_tool,
            self._rule_judge_tool,
            self._rule_judge_batch_tool,
            self._record_rule_execution_tool,
            self._record_rule_executions_batch_tool,
        }:
            self._working_memory.record_tool_health(
                infrastructure_failure=_contains_infrastructure_failure(result)
            )

        if _is_handled_failure(result):
            self._apply_handled_failure(
                tool_name=tool_name,
                arguments=arguments,
                result=result,
                capture_action_id=capture_action_id,
            )
        elif _is_blocked(result):
            if (
                tool_name == self._page_capture_tool
                and capture_action_id is not None
                and result.get("retryable") is not True
            ):
                failed = self._working_memory.fail_exploration_action(
                    exploration_action_id=capture_action_id,
                    failure_code=_text(result.get("error_code")) or "PAGE_CAPTURE_BLOCKED",
                    failure_message=_text(result.get("error_message") or result.get("message"))
                    or "页面采集被阻断",
                    retryable=False,
                )
                if failed:
                    self._working_memory.ensure_failed_obstruction_reobservation(
                        exploration_action_id=capture_action_id
                    )
            elif tool_name in {
                self._rule_judge_tool,
                self._rule_judge_batch_tool,
                self._record_rule_execution_tool,
                self._record_rule_executions_batch_tool,
            } and self._apply_blocked_rule_results(
                tool_name=tool_name,
                arguments=arguments,
                result=result,
            ):
                result["failure_isolated"] = True
                result["retryable"] = False
                page_capture_id = _text(
                    result.get("page_capture_id") or arguments.get("page_capture_id")
                )
                if tool_name in {
                    self._rule_judge_tool,
                    self._rule_judge_batch_tool,
                }:
                    result["message"] = (
                        "不可重试的规则判定错误已隔离为 error；"
                        "请继续处理其他规则或页面，禁止原参数重试。"
                    )
                    rejection_count = 0
                else:
                    rejection_count = (
                        self._working_memory.register_rule_write_rejection(
                            page_capture_id=page_capture_id
                        )
                        if page_capture_id is not None
                        else 0
                    )
                if rejection_count >= 2 and page_capture_id is not None:
                    self._working_memory.degrade_page_inspection(
                        page_capture_id=page_capture_id,
                        failure_stage="rule_execution",
                        failure_code="RULE_EXECUTION_WRITE_REJECTED_REPEATED",
                        failure_message=(
                            "同一页面规则写回连续被拒绝，已隔离剩余规则并继续动态探索。"
                        ),
                        retryable=False,
                    )
                    result["page_degraded"] = True
                    result["message"] = (
                        "同一页面规则写回已连续两次被拒绝；剩余规则已转 error，"
                        "请继续 mandatory 动作或其他页面，禁止继续提交该页规则。"
                    )
                elif tool_name not in {
                    self._rule_judge_tool,
                    self._rule_judge_batch_tool,
                }:
                    result["message"] = (
                        "本次规则写回失败已在当前页面隔离为 error；"
                        "请继续处理其他候选规则或后续页面，禁止原参数重试。"
                    )
        else:
            if tool_name == self._page_capture_tool:
                capture_arguments = dict(arguments)
                if capture_action_id is not None:
                    capture_arguments["exploration_action_id"] = capture_action_id
                self._remember_page_capture(capture_arguments, result)
            elif tool_name == self._page_detection_tool:
                self._remember_page_detection(arguments, result)
            elif tool_name == self._candidate_rules_tool and "error" not in result:
                self._remember_candidate_rules(arguments, result)
            elif tool_name == self._rule_judge_tool:
                self._apply_nonretryable_judge_results(
                    tool_name=tool_name,
                    arguments=arguments,
                    result=result,
                )
                if result.get("execution_id"):
                    self._remember_rule_result(result, result)
                else:
                    self._remember_rule_judgements(result)
            elif tool_name == self._rule_judge_batch_tool:
                self._apply_nonretryable_judge_results(
                    tool_name=tool_name,
                    arguments=arguments,
                    result=result,
                )
                if self._has_persisted_batch_judgement(result):
                    self._remember_persisted_batch_judgements(result)
                else:
                    self._remember_rule_judgements(result, batch=True)
            elif tool_name == self._record_rule_execution_tool:
                self._remember_rule_result(arguments, result)
            elif tool_name == self._record_rule_executions_batch_tool:
                self._remember_batch_rule_results(arguments, result)
            elif tool_name == self._exploration_closure_tool:
                result.update(self._apply_exploration_closure(arguments))

        if tool_name in {
            self._page_capture_tool,
            self._page_detection_tool,
            self._candidate_rules_tool,
            self._rule_judge_tool,
            self._rule_judge_batch_tool,
            self._record_rule_execution_tool,
            self._record_rule_executions_batch_tool,
            self._exploration_closure_tool,
            self._report_tool,
        }:
            context.result = self._model_visible_result(tool_name, result)

    def _model_visible_working_memory(self) -> dict[str, Any]:
        return _model_visible_working_memory(self._working_memory)

    def _blocking_page_flow_failure(
        self, *, page_capture_id: str | None = None
    ) -> dict[str, Any] | None:
        failure = self._working_memory.page_flow_blocking_failure
        if failure is None:
            return None
        if page_capture_id is not None and failure.get("page_capture_id") != page_capture_id:
            return None
        return dict(failure)

    def _terminate_failed_page_flow_retry(
        self,
        context: FunctionInvocationContext,
        failure: Mapping[str, Any],
    ) -> None:
        """模型忽略不可重试错误继续盲采或重识别时，锁存既有基础设施终态。"""
        self._working_memory.termination_reason = "infrastructure_unavailable"
        context.result = {
            "status": "error",
            "execution_status": "error",
            "failure_scope": "run",
            "failure_kind": "infrastructure",
            "error_code": _text(failure.get("code"))
            or "EVIDENCE_REQUIREMENT_PUBLISH_FAILED",
            "error_message": _text(failure.get("message"))
            or "页面证据需求未能发布，已停止重复页面采集与识别。",
            "retryable": False,
            "retry_exhausted": True,
            "page_capture_id": _text(failure.get("page_capture_id")),
            "working_memory": self._model_visible_working_memory(),
        }

    def _model_visible_result(self, tool_name: str, result: Mapping[str, Any]) -> dict[str, Any]:
        if not _optimized_pipeline_enabled():
            return {**dict(result), "working_memory": self._working_memory.model_snapshot()}

        if tool_name == self._page_capture_tool:
            compact_result = _compact_page_capture_result(result)
        elif tool_name == self._candidate_rules_tool:
            compact_result = _compact_candidate_rules_result(result)
        elif tool_name == self._page_detection_tool:
            compact_result = _compact_page_detection_result(result)
        else:
            compact_result = dict(result)
        compact_result["working_memory"] = self._model_visible_working_memory()
        compact_result["model_context_compacted"] = True
        return compact_result

    def _begin_page_capture_attempt(self, arguments: Mapping[str, Any]) -> str | None:
        """在真正采集前登记入口或 Frontier 动作。"""
        return self._working_memory.begin_page_capture_attempt(
            exploration_action_id=_text(arguments.get("exploration_action_id")),
            action=_text(arguments.get("trigger_action") or arguments.get("mode")) or "capture",
            target=_text(arguments.get("url") or arguments.get("selector")),
            navigation_depth=_integer(arguments.get("navigation_depth")),
        )

    def _enrich_mandatory_page_capture(
        self, context: FunctionInvocationContext, arguments: Mapping[str, Any]
    ) -> dict[str, Any]:
        """强制补采动作绑定被替代页面，避免旧瞬态采集悬空。"""
        enriched = dict(arguments)
        if _text(enriched.get("exploration_action_id")) is None:
            mode = _text(enriched.get("mode"))
            action = _text(enriched.get("trigger_action")) if mode == "single_trigger" else mode
            action_id = self._working_memory.resolve_mandatory_page_action(action=action)
            if action_id is not None:
                enriched["exploration_action_id"] = action_id
        defaults = self._working_memory.mandatory_page_action_capture_defaults(
            _text(enriched.get("exploration_action_id"))
        )
        if defaults is None:
            frontier_defaults = self._working_memory.frontier_action_capture_defaults(
                _text(enriched.get("exploration_action_id"))
            )
            if frontier_defaults is not None:
                if frontier_defaults["action"] == "goto":
                    enriched["mode"] = "goto"
                    target = _text(frontier_defaults.get("target"))
                    if target is not None:
                        enriched["url"] = target
                elif frontier_defaults["action"] == "click":
                    # 已选 Frontier 点击始终复用当前会话，不能因模型回填旧 URL
                    # 被误判为跨页 goto。
                    enriched.pop("url", None)
                    enriched.setdefault("mode", "single_trigger")
                    enriched.setdefault("trigger_action", "click")
                    target = _text(frontier_defaults.get("target"))
                    if target is not None:
                        # 普通动态 Frontier 与遮挡动作一样，以结构化记忆中的目标
                        # 覆盖模型遗漏，文本目标统一转换为 Playwright 文本 selector。
                        enriched["selector"] = _selector_for_target(target)
                elif frontier_defaults["action"] == "back":
                    enriched.pop("url", None)
                    enriched.pop("selector", None)
                    enriched["mode"] = "single_trigger"
                    enriched["trigger_action"] = "back"
                elif frontier_defaults["action"] == "scroll":
                    enriched.pop("url", None)
                    enriched.pop("selector", None)
                    enriched["mode"] = "single_trigger"
                    enriched["trigger_action"] = "scroll"
                elif frontier_defaults["action"] == "timed_capture":
                    enriched.pop("url", None)
                    enriched.pop("selector", None)
                    enriched["mode"] = "timed_capture"
                    enriched["frame_count"] = max(
                        _integer(enriched.get("frame_count")) or 0,
                        12,
                    )
                    enriched.setdefault("interval_ms", 1000)
                navigation_depth = frontier_defaults.get("navigation_depth")
                if isinstance(navigation_depth, int):
                    enriched["navigation_depth"] = navigation_depth
                supersedes_page_capture_id = frontier_defaults.get("supersedes_page_capture_id")
                if isinstance(supersedes_page_capture_id, str):
                    enriched["supersedes_page_capture_id"] = supersedes_page_capture_id
                elif frontier_defaults["action"] == "back":
                    enriched.pop("supersedes_page_capture_id", None)
                if frontier_defaults.get("clear_obstructions") is True:
                    enriched["clear_obstructions"] = True
            context.arguments = enriched
            return enriched
        # 已绑定 Frontier 的恢复/关闭动作必须在同一浏览器会话执行。入口页可能在
        # 首次采集后自动重定向，非 goto 动作不能保留模型携带的旧 URL；但来源页
        # 恢复本身就是 goto，必须强制回填 Frontier 中的可信目标地址。
        action = defaults["action"]
        target = defaults["target"]
        if action == "goto":
            enriched.pop("selector", None)
            enriched["mode"] = "goto"
            if target is not None:
                enriched["url"] = target
        elif action == "click":
            enriched.pop("url", None)
            enriched["mode"] = "single_trigger"
            enriched["trigger_action"] = "click"
            enriched["selector"] = _selector_for_target(target)
        elif action == "observe":
            enriched.pop("url", None)
            enriched.setdefault("mode", "observe")
        elif action == "back":
            enriched.pop("url", None)
            enriched.pop("selector", None)
            enriched["mode"] = "single_trigger"
            enriched["trigger_action"] = "back"
        elif action == "scroll":
            enriched.pop("url", None)
            enriched.pop("selector", None)
            enriched["mode"] = "single_trigger"
            enriched["trigger_action"] = "scroll"
        elif action == "timed_capture":
            enriched.pop("url", None)
            enriched.pop("selector", None)
            enriched["mode"] = "timed_capture"
            enriched["frame_count"] = max(
                _integer(enriched.get("frame_count")) or 0,
                12,
            )
            enriched.setdefault("interval_ms", 1000)
        navigation_depth = defaults.get("navigation_depth")
        if isinstance(navigation_depth, int):
            enriched["navigation_depth"] = navigation_depth
        supersedes_page_capture_id = defaults.get("supersedes_page_capture_id")
        if isinstance(supersedes_page_capture_id, str):
            enriched["supersedes_page_capture_id"] = supersedes_page_capture_id
        elif action == "back":
            enriched.pop("supersedes_page_capture_id", None)
        if defaults.get("clear_obstructions") is True:
            enriched["clear_obstructions"] = True
        context.arguments = enriched
        return enriched

    def _apply_handled_failure(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        result: Mapping[str, Any],
        capture_action_id: str | None,
    ) -> None:
        """把已隔离异常同步为动作、页面或规则结果终态。"""
        failure_scope = _text(result.get("failure_scope"))
        failure_code = _text(result.get("error_code")) or "TOOL_EXECUTION_FAILED"
        failure_message = _text(result.get("error_message")) or "工具执行失败"
        retryable = result.get("retryable") is True
        if failure_scope == "page":
            page_capture_id = _text(
                result.get("page_capture_id") or arguments.get("page_capture_id")
            )
            if page_capture_id is not None:
                self._working_memory.fail_page_inspection(
                    page_capture_id=page_capture_id,
                    failure_stage=_text(result.get("failure_stage")) or "page_processing",
                    failure_code=failure_code,
                    failure_message=failure_message,
                    retryable=retryable,
                )
            elif capture_action_id is not None:
                failed = self._working_memory.fail_exploration_action(
                    exploration_action_id=capture_action_id,
                    failure_code=failure_code,
                    failure_message=failure_message,
                    retryable=retryable,
                )
                if failed:
                    self._working_memory.ensure_failed_obstruction_reobservation(
                        exploration_action_id=capture_action_id
                    )
            return
        if failure_scope != "rule":
            return
        page_capture_id = _text(result.get("page_capture_id") or arguments.get("page_capture_id"))
        if page_capture_id is None:
            return
        raw_items = (
            arguments.get("executions")
            if tool_name == self._record_rule_executions_batch_tool
            else arguments.get("rules")
        )
        if isinstance(raw_items, list):
            for raw_item in raw_items:
                if isinstance(raw_item, Mapping):
                    self._remember_synthetic_rule_failure(
                        page_capture_id=page_capture_id,
                        raw_item=raw_item,
                        failure_code=failure_code,
                        failure_message=failure_message,
                    )
            return
        self._remember_synthetic_rule_failure(
            page_capture_id=page_capture_id,
            raw_item=arguments,
            failure_code=failure_code,
            failure_message=failure_message,
        )

    def _remember_synthetic_rule_failure(
        self,
        *,
        page_capture_id: str,
        raw_item: Mapping[str, Any],
        failure_code: str,
        failure_message: str,
    ) -> None:
        """为未落库的规则故障写入 Run 内结构化占位结果。"""
        rule_id = _text(raw_item.get("rule_id"))
        rule_code = _text(raw_item.get("rule_code"))
        if rule_id is None and rule_code is None:
            return
        arguments = {
            **raw_item,
            "execution_status": "error",
            "result_summary": "规则处理异常，已转人工复核。",
            "problem_desc": failure_message,
            "error_code": failure_code,
            "error_message": failure_message,
        }
        self._working_memory.record_page_rule_result(
            page_capture_id=page_capture_id,
            result={
                "rule_id": rule_id,
                "rule_code": rule_code,
                "execution_status": "error",
                "page_capture_id": page_capture_id,
            },
            arguments=arguments,
        )

    def _apply_blocked_rule_results(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> bool:
        """把不可落库的规则隔离为页面内终态，避免阻断后续探索。"""
        page_capture_id = _text(result.get("page_capture_id") or arguments.get("page_capture_id"))
        if page_capture_id is None:
            return False
        if tool_name in {self._rule_judge_tool, self._rule_judge_batch_tool}:
            return self._apply_nonretryable_judge_results(
                tool_name=tool_name,
                arguments=arguments,
                result=result,
            )
        if tool_name == self._record_rule_execution_tool:
            raw_error = result.get("error")
            error = dict(raw_error) if isinstance(raw_error, Mapping) else {}
            self._remember_synthetic_rule_failure(
                page_capture_id=page_capture_id,
                raw_item=arguments,
                failure_code=_text(error.get("code") or result.get("error_code"))
                or "RULE_EXECUTION_WRITE_REJECTED",
                failure_message=_text(error.get("message") or result.get("message"))
                or "规则结果写回被拒绝",
            )
            return True

        raw_executions = arguments.get("executions")
        raw_items = result.get("items")
        if not isinstance(raw_executions, list) or not isinstance(raw_items, list):
            return False
        isolated = False
        for index, raw_result_item in enumerate(raw_items):
            result_item = dict(raw_result_item) if isinstance(raw_result_item, Mapping) else {}
            if _text(result_item.get("status")) not in {"blocked", "error"}:
                continue
            item_index = _integer(result_item.get("item_index"))
            target_index = item_index if item_index is not None else index
            if target_index < 0 or target_index >= len(raw_executions):
                continue
            raw_execution = raw_executions[target_index]
            execution = dict(raw_execution) if isinstance(raw_execution, Mapping) else {}
            raw_error = result_item.get("error")
            error = dict(raw_error) if isinstance(raw_error, Mapping) else {}
            self._remember_synthetic_rule_failure(
                page_capture_id=page_capture_id,
                raw_item=execution,
                failure_code=_text(error.get("code") or result_item.get("error_code"))
                or "RULE_EXECUTION_WRITE_REJECTED",
                failure_message=_text(error.get("message") or result_item.get("message"))
                or "规则结果写回被拒绝",
            )
            isolated = True
        return isolated

    def _apply_nonretryable_judge_results(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> bool:
        """将确定性契约错误终态化，基础设施瞬断仍保留重试机会。"""
        page_capture_id = _text(result.get("page_capture_id") or arguments.get("page_capture_id"))
        if page_capture_id is None:
            return False
        if tool_name == self._rule_judge_tool:
            if result.get("retryable") is not False:
                return False
            raw_error = result.get("error")
            error = dict(raw_error) if isinstance(raw_error, Mapping) else {}
            self._remember_synthetic_rule_failure(
                page_capture_id=page_capture_id,
                raw_item=arguments,
                failure_code=_text(error.get("code")) or "RULE_JUDGE_FAILED",
                failure_message=_text(error.get("message")) or "规则判定不可重试",
            )
            return True

        raw_rules = arguments.get("rules")
        raw_items = result.get("items")
        if not isinstance(raw_rules, list) or not isinstance(raw_items, list):
            return False
        isolated = False
        for fallback_index, raw_result_item in enumerate(raw_items):
            item = dict(raw_result_item) if isinstance(raw_result_item, Mapping) else {}
            if item.get("retryable") is not False:
                continue
            item_index = _integer(item.get("item_index"))
            target_index = item_index if item_index is not None else fallback_index
            if target_index < 0 or target_index >= len(raw_rules):
                continue
            raw_rule = raw_rules[target_index]
            rule = dict(raw_rule) if isinstance(raw_rule, Mapping) else {}
            raw_error = item.get("error")
            error = dict(raw_error) if isinstance(raw_error, Mapping) else {}
            self._remember_synthetic_rule_failure(
                page_capture_id=page_capture_id,
                raw_item=rule,
                failure_code=_text(error.get("code")) or "RULE_JUDGE_FAILED",
                failure_message=_text(error.get("message")) or "规则判定不可重试",
            )
            isolated = True
        return isolated

    def _remember_page_detection(
        self, arguments: Mapping[str, Any], result: Mapping[str, Any]
    ) -> None:
        requested_capture_id = _text(arguments.get("page_capture_id"))
        result_capture_id = _text(result.get("page_capture_id"))
        detection_status = _text(result.get("detection_status"))
        if (
            requested_capture_id is None
            or result_capture_id != requested_capture_id
            or detection_status is None
        ):
            return
        page_type = _text(result.get("page_type"))
        page_state = _text(result.get("page_state"))
        rule_matching_allowed = result.get("rule_matching_allowed") is True
        self._working_memory.remember_page_detection(
            page_capture_id=result_capture_id,
            detection_status=detection_status,
            page_type=page_type,
            page_state=page_state,
            rule_matching_allowed=rule_matching_allowed,
            failure_code=_text(result.get("error_code")),
            failure_message=_text(result.get("error_message") or result.get("reason")),
            retryable=result.get("retryable") is True,
        )
        failure_code = _text(result.get("error_code"))
        if (
            detection_status == "error"
            and failure_code in _PAGE_FLOW_BLOCKING_EVIDENCE_FAILURE_CODES
        ):
            self._working_memory.remember_page_flow_blocking_failure(
                page_capture_id=result_capture_id,
                failure_code=failure_code,
                failure_message=(
                    _text(result.get("error_message") or result.get("reason"))
                    or "页面证据需求未能发布，当前页面已停止规则匹配。"
                ),
            )
        elif detection_status == "recognized":
            self._working_memory.clear_page_flow_blocking_failure()
        candidate_actions = _mapping_list(result.get("candidate_actions"))
        obstruction_action = _first_obstruction_dismissal_action(candidate_actions)
        auxiliary_resolution = self._working_memory.resolve_auxiliary_page_return_if_ready(
            page_capture_id=result_capture_id,
            detection_status=detection_status,
            page_type=page_type,
            page_state=page_state,
        )
        if auxiliary_resolution == "ready_reused":
            # 原来源页已经完成质检；先用返回后的新鲜 DOM 刷新全局/语义候选，
            # 再把本次复访采集收口，避免重复执行整批规则。
            self._working_memory.remember_exploration_candidates(
                page_capture_id=result_capture_id,
                page_type=page_type,
                candidate_actions=candidate_actions,
                next_page_hypotheses=_mapping_list(result.get("next_page_hypotheses")),
            )
            self._working_memory.abandon_revisited_page(
                page_capture_id=result_capture_id,
                reason="auxiliary_page_return_revisit",
            )
            return
        restore_resolution = self._working_memory.rebind_restored_source_action_if_ready(
            page_capture_id=result_capture_id,
            detection_status=detection_status,
            page_type=page_type,
            page_state=page_state,
        )
        if restore_resolution == "ready_reused":
            # 来源页此前已经完整质检；恢复采集只负责重新取得可交互 DOM，
            # 不再次执行同一批规则，下一轮直接派发被恢复的原 Frontier。
            return
        authentication_required = "authentication_required" in {
            flag.casefold() for flag in _string_list(result.get("risk_flags"))
        }
        if detection_status == "recognized" and authentication_required:
            if (
                page_state == "popup"
                and obstruction_action is not None
                and self._working_memory.is_same_url_obstruction_page(result_capture_id)
            ):
                # “身份凭证/登录二维码”等同地址弹层可能带认证风险，但底层业务页
                # 仍在当前会话中。可信低风险关闭动作优先于浏览器历史返回。
                added_ids = self._working_memory.remember_exploration_candidates(
                    page_capture_id=result_capture_id,
                    page_type=page_type,
                    candidate_actions=[obstruction_action],
                    next_page_hypotheses=[],
                )
                if added_ids:
                    self._working_memory.require_exploration_action(
                        exploration_action_id=added_ids[0],
                        blocks_page_inspection=True,
                        supersedes_source_page=True,
                        requires_source_inspection_complete=False,
                    )
                return
            self._working_memory.skip_authentication_required_page(
                page_capture_id=result_capture_id,
                page_type=page_type,
            )
            return
        is_restore_transit = self._working_memory.is_source_restore_transit(result_capture_id)
        if is_restore_transit:
            if obstruction_action is not None:
                added_ids = self._working_memory.remember_exploration_candidates(
                    page_capture_id=result_capture_id,
                    page_type=page_type,
                    candidate_actions=[obstruction_action],
                    next_page_hypotheses=[],
                )
                if added_ids:
                    self._working_memory.require_exploration_action(
                        exploration_action_id=added_ids[0],
                        blocks_page_inspection=True,
                        supersedes_source_page=True,
                        requires_source_inspection_complete=False,
                    )
                return
            transient_kind = (
                page_type
                if page_type in TRANSIENT_PAGE_TYPES
                else page_state
                if page_state in TRANSIENT_PAGE_STATES
                else None
            )
            if transient_kind is not None:
                self._working_memory.ensure_transient_page_recovery(
                    page_capture_id=result_capture_id,
                    page_type=transient_kind,
                )
                return
            if page_type == "startup_ad" or page_state == "countdown":
                self._working_memory.ensure_source_restore_transit_observation(
                    page_capture_id=result_capture_id,
                    page_type=page_type,
                )
                return
            if detection_status == "recognized":
                self._working_memory.abandon_unmatchable_page(
                    page_capture_id=result_capture_id,
                    reason="source_restore_unexpected_landing",
                )
                return
        if (
            detection_status == "recognized"
            and obstruction_action is not None
            and self._working_memory.is_obstruction_chain_continuation(result_capture_id)
        ):
            # 首层弹窗已经完成过一次相关规则质检。后续层只负责安全清障并保留
            # 动作链证据，禁止再次把同一底层业务页送入候选规则与 LLM Judge。
            added_ids = self._working_memory.remember_exploration_candidates(
                page_capture_id=result_capture_id,
                page_type=page_type,
                candidate_actions=[obstruction_action],
                next_page_hypotheses=[],
            )
            if added_ids:
                self._working_memory.require_exploration_action(
                    exploration_action_id=added_ids[0],
                    blocks_page_inspection=True,
                    supersedes_source_page=True,
                    requires_source_inspection_complete=False,
                )
            return
        reuse_completed_obstruction_source = bool(
            detection_status == "recognized"
            and obstruction_action is None
            and self._working_memory.can_reuse_completed_obstruction_source(
                page_capture_id=result_capture_id,
                page_type=page_type,
                page_state=page_state,
            )
        )
        if detection_status == "recognized" and rule_matching_allowed:
            is_inspectable_ad_page = page_type == "startup_ad"
            is_inspectable_obstruction = obstruction_action is not None
            is_repeated_obstruction = (
                obstruction_action is not None
                and self._working_memory.has_prior_obstruction_capture_in_chain(result_capture_id)
            )
            if obstruction_action is not None:
                actions_to_remember = [obstruction_action]
                hypotheses_to_remember: list[Mapping[str, Any]] = []
            elif is_inspectable_ad_page:
                actions_to_remember = []
                hypotheses_to_remember = []
            else:
                self._working_memory.expire_stale_obstruction_actions(
                    current_page_capture_id=result_capture_id
                )
                actions_to_remember = candidate_actions
                hypotheses_to_remember = _mapping_list(result.get("next_page_hypotheses"))
            added_ids = self._working_memory.remember_exploration_candidates(
                page_capture_id=result_capture_id,
                page_type=page_type,
                candidate_actions=actions_to_remember,
                next_page_hypotheses=hypotheses_to_remember,
            )
            if reuse_completed_obstruction_source:
                # 稳定详情页关闭局部遮挡后仍是同 URL、同类型逻辑页；保留新 DOM
                # 发现的 Frontier，但复用来源页规则终态，禁止再次执行整批规则。
                self._working_memory.abandon_revisited_page(
                    page_capture_id=result_capture_id,
                    reason="obstruction_result_reuses_source_inspection",
                )
                return
            if page_type in {
                "content_list",
                "post_list",
                "video_list",
            } and self._working_memory.is_nested_return_capture(result_capture_id):
                # 返回后的列表采集只用于恢复浏览器位置和发现尚未入队的分支；
                # 同一业务列表已经质检过，避免重复跑规则并再次点击首卡形成循环。
                self._working_memory.abandon_revisited_page(
                    page_capture_id=result_capture_id,
                    reason="nested_detail_return_revisit",
                )
                return
            if obstruction_action is not None and added_ids:
                self._working_memory.require_exploration_action(
                    exploration_action_id=added_ids[0],
                    blocks_page_inspection=is_repeated_obstruction,
                    supersedes_source_page=not is_inspectable_ad_page,
                    requires_source_inspection_complete=(
                        (is_inspectable_ad_page or is_inspectable_obstruction)
                        and not is_repeated_obstruction
                    ),
                )
            elif is_inspectable_ad_page:
                self._working_memory.ensure_inspectable_page_transition(
                    page_capture_id=result_capture_id,
                    page_type=page_type,
                )
            return
        transient_kind = (
            page_type
            if page_type in TRANSIENT_PAGE_TYPES
            else page_state
            if page_state in TRANSIENT_PAGE_STATES
            else None
        )
        if (
            detection_status
            in {
                "recognized",
                "ambiguous",
                "insufficient",
            }
            and transient_kind is not None
        ):
            self._working_memory.ensure_transient_page_recovery(
                page_capture_id=result_capture_id,
                page_type=transient_kind,
            )
        elif detection_status == "recognized":
            self._working_memory.abandon_unmatchable_page(
                page_capture_id=result_capture_id,
                reason="page_type_not_rule_matchable",
            )
        elif detection_status in {"ambiguous", "insufficient"}:
            reason = _text(result.get("reason")) or "页面类型未能稳定映射到目录"
            self._working_memory.fail_page_inspection(
                page_capture_id=result_capture_id,
                failure_stage="page_detection",
                failure_code=(
                    "PAGE_DETECTION_AMBIGUOUS"
                    if detection_status == "ambiguous"
                    else "PAGE_DETECTION_INSUFFICIENT"
                ),
                failure_message=reason,
                retryable=False,
                missing_rule_status="evidence_insufficient",
            )

    def _remember_rule_judgements(
        self,
        result: Mapping[str, Any],
        *,
        batch: bool = False,
    ) -> None:
        """记录判定成功但尚未落库的规则，供下一轮强制写回。"""
        if batch:
            page_capture_id = _text(result.get("page_capture_id"))
            raw_items = result.get("items")
            if page_capture_id is None or not isinstance(raw_items, list):
                return
            for raw_item in raw_items:
                item = dict(raw_item) if isinstance(raw_item, Mapping) else {}
                if _text(item.get("status")) != "ok":
                    continue
                item.setdefault("page_capture_id", page_capture_id)
                self._remember_rule_judgement(item)
            return
        self._remember_rule_judgement(result)

    def _remember_rule_judgement(self, result: Mapping[str, Any]) -> None:
        page_capture_id = _text(result.get("page_capture_id"))
        execution_status = _text(result.get("execution_status"))
        if page_capture_id is None or execution_status is None:
            return
        self._working_memory.record_rule_judgement(
            page_capture_id=page_capture_id,
            judgement=result,
        )

    @staticmethod
    def _has_persisted_batch_judgement(result: Mapping[str, Any]) -> bool:
        raw_items = result.get("items")
        return isinstance(raw_items, list) and any(
            isinstance(item, Mapping) and item.get("execution_id") for item in raw_items
        )

    def _remember_persisted_batch_judgements(self, result: Mapping[str, Any]) -> None:
        """只在原子写回成功后把批量 Judge 结果推进为页面规则终态。"""
        page_capture_id = _text(result.get("page_capture_id"))
        raw_items = result.get("items")
        if page_capture_id is None or not isinstance(raw_items, list):
            return
        for raw_item in raw_items:
            if not isinstance(raw_item, Mapping) or not raw_item.get("execution_id"):
                continue
            self._working_memory.record_page_rule_result(
                page_capture_id=page_capture_id,
                result=raw_item,
                arguments=raw_item,
            )

    def _apply_exploration_closure(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """把通过 Tool 校验的收口请求应用到当前 Run 工作记忆。"""
        reason = _text(arguments.get("reason")) or "unspecified"
        closure_result = self._working_memory.close_exploration_items(
            action_status=_text(arguments.get("action_status")) or "skipped",
            reason=reason,
            exploration_action_ids=_string_list(arguments.get("exploration_action_ids")),
            close_all_pending_actions=(arguments.get("close_all_pending_actions") is True),
            page_capture_ids=_string_list(arguments.get("page_capture_ids")),
            abandon_all_pending_pages=(arguments.get("abandon_all_pending_pages") is True),
        )
        degraded_page_ids = self._working_memory.degrade_mandatory_action_source_pages(
            exploration_action_ids=closure_result["rejected_action_ids"],
            failure_message=(
                f"Agent 已尝试收口未完成的开屏广告规则；剩余规则已隔离为 error。原因：{reason}"
            ),
        )
        response: dict[str, Any] = {"closure_result": closure_result}
        assessments = _mapping_list(arguments.get("page_type_assessments"))
        if assessments:
            assessment_result = self._remember_page_type_assessments(assessments, arguments)
            response["page_type_assessment_result"] = assessment_result
            if assessment_result["execution_status"] == "blocked":
                response.update(
                    {
                        "execution_status": "blocked",
                        "error_code": assessment_result["error_code"],
                        "retryable": assessment_result["retryable"],
                    }
                )
        if degraded_page_ids:
            response.update(
                {
                    "failure_isolated": True,
                    "retryable": False,
                    "degraded_page_capture_ids": degraded_page_ids,
                    "message": (
                        "未完成规则已在开屏广告页隔离为 error；"
                        "mandatory 过渡动作仍保持待执行，请立即继续该动作。"
                    ),
                }
            )
        return response

    def _remember_page_type_assessments(
        self,
        assessments: list[Mapping[str, Any]],
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        """用 Agent Run 上下文把页面类型评估入参收敛成领域合同。"""
        run_context = get_agent_run_context()
        if run_context is None or not run_context.task_id:
            return {
                "execution_status": "blocked",
                "error_code": "PAGE_TYPE_ASSESSMENT_CONTEXT_REQUIRED",
                "retryable": True,
                "message": "页面类型评估必须在绑定 task_id 与 agent_run_id 的运行上下文中写入。",
            }
        try:
            requested_scope_page_types = _string_list(
                arguments.get("assessment_scope_page_types")
            ) or [
                page_type
                for item in assessments
                if (page_type := _text(item.get("page_type"))) is not None
            ]
            scope_page_types = list(
                dict.fromkeys(
                    [
                        *requested_scope_page_types,
                        *self._working_memory.observed_business_page_types(),
                    ]
                )
            )
            assessment_count = self._working_memory.remember_page_type_assessments(
                task_id=run_context.task_id,
                agent_run_id=run_context.agent_run_id,
                assessments=assessments,
                scope_complete=(arguments.get("assessment_scope_complete") is True),
                scope_page_types=scope_page_types,
            )
        except (PageTypeAssessmentValidationError, ValueError) as exc:
            return {
                "execution_status": "blocked",
                "error_code": "PAGE_TYPE_ASSESSMENT_CONTRACT_INVALID",
                "retryable": True,
                "message": str(exc),
            }
        return {
            "execution_status": "accepted",
            "assessment_count": assessment_count,
            "assessment_scope_complete": (arguments.get("assessment_scope_complete") is True),
            "assessment_scope_page_types": scope_page_types,
        }

    def _remember_page_capture(
        self, arguments: Mapping[str, Any], result: Mapping[str, Any]
    ) -> None:
        page_capture_id = _text(result.get("page_capture_id"))
        if page_capture_id is None:
            return
        metrics = result.get("metrics")
        obstruction_chain = (
            metrics.get("obstruction_chain") if isinstance(metrics, Mapping) else None
        )
        obstruction_dismissal_count = (
            _integer(obstruction_chain.get("dismissal_count"))
            if isinstance(obstruction_chain, Mapping)
            else None
        )
        self._working_memory.remember_page_capture(
            page_capture_id=page_capture_id,
            page_url=_text(result.get("final_url")),
            page_title=_text(result.get("title")),
            page_seq=_integer(result.get("page_seq")),
            artifact_ids=_string_list(result.get("artifact_ids")),
            content_fingerprint=_text(result.get("content_fingerprint")),
            visible_text_fingerprint=_text(result.get("visible_text_fingerprint")),
            obstruction_dismissal_count=obstruction_dismissal_count or 0,
            exploration_action_id=_text(
                arguments.get("exploration_action_id") or result.get("exploration_action_id")
            ),
            supersedes_page_capture_id=_text(
                arguments.get("supersedes_page_capture_id")
                or result.get("supersedes_page_capture_id")
            ),
        )
        if (
            isinstance(obstruction_chain, Mapping)
            and obstruction_chain.get("remaining_obstruction") is True
            and obstruction_chain.get("outcome")
            in {"dismissal_limit_reached", "duration_limit_reached"}
        ):
            self._working_memory.fail_page_inspection(
                page_capture_id=page_capture_id,
                failure_stage="page_capture",
                failure_code="PAGE_OBSTRUCTION_CLEAR_LIMIT_REACHED",
                failure_message=(
                    "广告弹窗清障已达到单次安全上限，当前页面停止继续关闭并转人工复核。"
                ),
                retryable=False,
            )
        coverage = result.get("page_coverage_summary")
        if isinstance(coverage, Mapping):
            collection_complete = coverage.get("collection_complete") is True
            if coverage.get("coverage_complete") is False:
                self._working_memory.ensure_scroll_coverage_action(
                    page_capture_id=page_capture_id
                )
            if collection_complete or coverage.get("coverage_complete") is True:
                self._completed_coverage_page_capture_ids.add(page_capture_id)
        raw_artifacts = result.get("artifacts")
        if isinstance(raw_artifacts, list) and any(
            isinstance(item, Mapping) and item.get("artifact_type") == "temporal_trace"
            for item in raw_artifacts
        ):
            self._completed_temporal_page_capture_ids.add(page_capture_id)
        elif (
            get_settings().quality_optimization.pipeline_mode == "on"
            and _looks_like_immediate_temporal_page(arguments=arguments, result=result)
        ):
            # 首采阶段只能记录页面事实，不能在 page_detection 之前直接按补采缺失
            # 降级；否则入口开屏广告会跳过识别、候选规则和后续关闭动作。
            return

    def _remember_candidate_rules(
        self, arguments: Mapping[str, Any], result: Mapping[str, Any]
    ) -> None:
        page_capture_id = _text(arguments.get("page_capture_id"))
        page_type = _text(result.get("page_type") or arguments.get("page_type"))
        raw_rules = result.get("rules")
        if page_capture_id is None or page_type is None or not isinstance(raw_rules, list):
            return
        rules = [item for item in raw_rules if isinstance(item, Mapping)]
        self._working_memory.start_page_inspection(
            page_capture_id=page_capture_id,
            page_type=page_type,
            candidate_rules=rules,
        )
        capture_plan = result.get("capture_plan")
        if (
            isinstance(capture_plan, Mapping)
            and capture_plan.get("scroll") is True
            and page_capture_id not in self._completed_coverage_page_capture_ids
            and get_settings().quality_optimization.pipeline_mode == "on"
        ):
            self._working_memory.ensure_scroll_coverage_action(page_capture_id=page_capture_id)
        if (
            isinstance(capture_plan, Mapping)
            and capture_plan.get("temporal") is True
            and page_capture_id not in self._completed_temporal_page_capture_ids
            and get_settings().quality_optimization.pipeline_mode == "on"
        ):
            self._working_memory.ensure_temporal_capture_action(page_capture_id=page_capture_id)
        if (
            isinstance(capture_plan, Mapping)
            and capture_plan.get("interaction") is True
            and get_settings().quality_optimization.pipeline_mode == "on"
        ):
            self._working_memory.ensure_interaction_evidence_action(
                page_capture_id=page_capture_id,
                match_terms=_interaction_match_terms(rules),
                required_capture_roles=_string_list(capture_plan.get("required_capture_roles")),
            )

    def _remember_rule_result(
        self, arguments: Mapping[str, Any], result: Mapping[str, Any]
    ) -> None:
        page_capture_id = _text(result.get("page_capture_id") or arguments.get("page_capture_id"))
        if page_capture_id is None:
            return
        self._working_memory.record_page_rule_result(
            page_capture_id=page_capture_id,
            result=result,
            arguments=arguments,
        )

    def _remember_batch_rule_results(
        self, arguments: Mapping[str, Any], result: Mapping[str, Any]
    ) -> None:
        page_capture_id = _text(result.get("page_capture_id") or arguments.get("page_capture_id"))
        raw_results = result.get("items")
        raw_arguments = arguments.get("executions")
        if (
            page_capture_id is None
            or not isinstance(raw_results, list)
            or not isinstance(raw_arguments, list)
        ):
            return

        arguments_by_rule: dict[str, Mapping[str, Any]] = {}
        for raw_item in raw_arguments:
            if not isinstance(raw_item, Mapping):
                continue
            key = _rule_key(raw_item)
            if key is not None:
                arguments_by_rule[key] = raw_item
        for raw_result in raw_results:
            if not isinstance(raw_result, Mapping):
                continue
            item_status = _text(raw_result.get("status"))
            if item_status not in {None, "ok"}:
                if item_status == "error" or raw_result.get("retryable") is False:
                    item_arguments = arguments_by_rule.get(_rule_key(raw_result) or "", {})
                    error = raw_result.get("error")
                    error_mapping = error if isinstance(error, Mapping) else {}
                    self._remember_synthetic_rule_failure(
                        page_capture_id=page_capture_id,
                        raw_item={**item_arguments, **raw_result},
                        failure_code=_text(error_mapping.get("code"))
                        or "RULE_EXECUTION_ITEM_FAILED",
                        failure_message=_text(error_mapping.get("message")) or "规则写回失败",
                    )
                continue
            if not raw_result.get("execution_id"):
                continue
            item_arguments = arguments_by_rule.get(_rule_key(raw_result) or "", {})
            self._working_memory.record_page_rule_result(
                page_capture_id=page_capture_id,
                result=raw_result,
                arguments=item_arguments,
            )


_OPTIMIZED_VISIBLE_TEXT_EXCERPT_LIMIT = 800
_OPTIMIZED_UI_ELEMENT_LIMIT = 12
_OPTIMIZED_RICH_UI_ELEMENT_LIMIT = 1
_OPTIMIZED_LIST_LIMIT = 20
_OPTIMIZED_TEXT_FIELD_LIMIT = 100


def _optimized_pipeline_enabled() -> bool:
    return get_settings().quality_optimization.pipeline_mode == "on"


def _model_visible_working_memory(working_memory: WorkingMemory) -> dict[str, Any]:
    snapshot = working_memory.model_snapshot()
    if not _optimized_pipeline_enabled():
        return snapshot

    frontier = snapshot.get("exploration_frontier")
    frontier_mapping = frontier if isinstance(frontier, Mapping) else {}
    return {
        "schema_version": snapshot.get("schema_version"),
        "current_page": _compact_page_snapshot(snapshot.get("current_page")),
        "pending_pages": _compact_page_list(snapshot.get("pending_pages")),
        "exploration_frontier": {
            "pending_actions": _compact_action_list(frontier_mapping.get("pending_actions")),
            "pending_action_count": frontier_mapping.get("pending_action_count", 0),
            "explored_action_count": frontier_mapping.get("explored_action_count", 0),
            "inactive_action_count": frontier_mapping.get("inactive_action_count", 0),
        },
        "inspected_page_count": snapshot.get("inspected_page_count", 0),
        "terminal_page_count": snapshot.get("terminal_page_count", 0),
        "degraded_page_count": snapshot.get("degraded_page_count", 0),
        "failed_page_count": snapshot.get("failed_page_count", 0),
        "pending_page_count": snapshot.get("pending_page_count", 0),
        "inactive_page_count": snapshot.get("inactive_page_count", 0),
        "compacted": True,
        "omitted_sections": [
            "inspected_pages",
            "terminal_pages",
            "inactive_pages",
            "exploration_frontier.explored_actions",
            "exploration_frontier.inactive_actions",
        ],
    }


def _compact_page_capture_result(result: Mapping[str, Any]) -> dict[str, Any]:
    compact = dict(result)
    excerpt = _text(compact.get("visible_text_excerpt"))
    if excerpt is not None:
        compact["visible_text_excerpt"] = _clip_text(excerpt, _OPTIMIZED_VISIBLE_TEXT_EXCERPT_LIMIT)
        compact["visible_text_truncated_for_model"] = len(excerpt) > len(
            compact["visible_text_excerpt"]
        )
    ui_summary = compact.get("ui_summary")
    if isinstance(ui_summary, Mapping):
        compact["ui_summary"] = _compact_ui_summary(ui_summary)
    return compact


def _compact_candidate_rules_result(result: Mapping[str, Any]) -> dict[str, Any]:
    compact = dict(result)
    raw_rules = compact.get("rules")
    if isinstance(raw_rules, list):
        compact["rules"] = [
            _compact_candidate_rule(rule) for rule in raw_rules if isinstance(rule, Mapping)
        ]
        compact["rule_count"] = len(raw_rules)
    execution_plan = compact.pop("execution_plan", None)
    if isinstance(execution_plan, Mapping):
        batches = execution_plan.get("batches")
        compact["execution_plan_summary"] = {
            "batch_count": len(batches) if isinstance(batches, list) else 0,
            "has_execution_plan": True,
        }
    elif execution_plan is not None:
        compact["execution_plan_summary"] = {"has_execution_plan": True}
    return compact


def _compact_page_detection_result(result: Mapping[str, Any]) -> dict[str, Any]:
    compact = dict(result)
    for key in ("candidate_actions", "next_page_hypotheses"):
        raw_items = compact.get(key)
        if isinstance(raw_items, list):
            compact[key] = _compact_action_list(raw_items)
            compact[f"{key}_count"] = len(raw_items)
    return compact


def _compact_page_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [
        page
        for page in (_compact_page_snapshot(item) for item in value[:_OPTIMIZED_LIST_LIMIT])
        if page is not None
    ]


def _compact_page_snapshot(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    allowed_keys = (
        "page_capture_id",
        "page_url",
        "page_title",
        "page_seq",
        "page_type",
        "page_state",
        "status",
        "inspection_status",
        "quality_status",
        "candidate_rule_count",
        "completed_rule_count",
        "pending_rule_count",
    )
    compact = {key: value.get(key) for key in allowed_keys if key in value}
    detection = value.get("page_detection")
    if isinstance(detection, Mapping):
        compact["page_detection"] = {
            key: detection.get(key)
            for key in (
                "detection_status",
                "page_type",
                "page_state",
                "rule_matching_allowed",
            )
            if key in detection
        }
    raw_rules = value.get("candidate_rules")
    if isinstance(raw_rules, list):
        compact["candidate_rules"] = [
            _compact_candidate_rule(rule)
            for rule in raw_rules[:_OPTIMIZED_LIST_LIMIT]
            if isinstance(rule, Mapping)
        ]
    raw_judgements = value.get("pending_rule_judgements")
    if isinstance(raw_judgements, list):
        compact["pending_rule_judgements"] = [
            _compact_rule_judgement(item)
            for item in raw_judgements[:_OPTIMIZED_LIST_LIMIT]
            if isinstance(item, Mapping)
        ]
    result = value.get("result")
    if isinstance(result, Mapping):
        compact["result"] = {
            "overall_status": result.get("overall_status"),
            "status_counts": result.get("status_counts", {}),
            "rule_count": len(result.get("rules", []))
            if isinstance(result.get("rules"), list)
            else 0,
        }
    return compact


def _compact_candidate_rule(rule: Mapping[str, Any]) -> dict[str, Any]:
    compact = {key: rule.get(key) for key in ("rule_id", "rule_code", "category") if key in rule}
    route = rule.get("execution_route")
    if isinstance(route, Mapping):
        compact["execution_route"] = {
            key: route.get(key)
            for key in (
                "strategy",
                "judge_tool_name",
                "session_dependency",
                "rule_engine_ready",
                "route_blocked_reason",
            )
            if key in route
        }
    return compact


def _compact_rule_judgement(judgement: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: judgement.get(key)
        for key in (
            "rule_id",
            "rule_code",
            "status",
            "execution_status",
            "retryable",
        )
        if key in judgement
    }


def _compact_action_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [
        _compact_action(item) for item in value[:_OPTIMIZED_LIST_LIMIT] if isinstance(item, Mapping)
    ]


def _compact_action(action: Mapping[str, Any]) -> dict[str, Any]:
    compact = {
        key: action.get(key)
        for key in (
            "exploration_action_id",
            "source_page_capture_id",
            "result_page_capture_id",
            "action",
            "target",
            "expected_next_page_type",
            "mandatory",
            "status",
            "navigation_depth",
        )
        if key in action
    }
    for key in ("target", "expected_next_page_type"):
        value = _text(compact.get(key))
        if value is not None:
            compact[key] = _clip_text(value, _OPTIMIZED_TEXT_FIELD_LIMIT)
    return compact


def _compact_ui_summary(summary: Mapping[str, Any]) -> dict[str, Any]:
    compact = dict(summary)
    raw_elements = compact.get("elements")
    if isinstance(raw_elements, list):
        elements = [
            _compact_ui_element(item)
            for item in raw_elements[:_OPTIMIZED_UI_ELEMENT_LIMIT]
            if isinstance(item, Mapping)
        ]
        for item in elements[_OPTIMIZED_RICH_UI_ELEMENT_LIMIT:]:
            item.pop("text_metrics", None)
            item.pop("truncation", None)
        compact["elements"] = elements
        compact["element_sample_count"] = len(elements)
    return compact


def _compact_ui_element(element: Mapping[str, Any]) -> dict[str, Any]:
    compact = {
        key: element.get(key)
        for key in ("role", "text", "aria_label", "selector", "text_metrics", "truncation")
        if key in element
    }
    for key in ("text", "aria_label", "selector"):
        value = _text(compact.get(key))
        if value is not None:
            compact[key] = _clip_text(value, _OPTIMIZED_TEXT_FIELD_LIMIT)
    compact["text_metrics"] = _compact_mapping(
        compact.get("text_metrics"),
        (
            "scroll_width",
            "client_width",
            "horizontal_overflow",
            "vertical_overflow",
            "text_overflow_ellipsis",
            "line_clamp",
        ),
    )
    compact["truncation"] = _compact_mapping(
        compact.get("truncation"),
        (
            "horizontal_overflow",
            "vertical_overflow",
            "text_overflow",
            "white_space",
            "line_clamp",
            "clipped",
        ),
    )
    if compact["text_metrics"] is None:
        compact.pop("text_metrics", None)
    if compact["truncation"] is None:
        compact.pop("truncation", None)
    return compact


def _compact_mapping(value: Any, allowed_keys: tuple[str, ...]) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {key: value.get(key) for key in allowed_keys if key in value}


def _clip_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)] + "..."


def _result_mapping(result: Any) -> dict[str, Any] | None:
    """尽力拆开 Agent Framework 的结果包装并返回字典副本。"""
    value = result
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
    text = getattr(value, "text", None)
    if isinstance(text, str):
        value = text
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    return dict(value) if isinstance(value, Mapping) else None


def _is_blocked(result: Mapping[str, Any]) -> bool:
    return result.get("execution_status") == "blocked" or result.get("status") == "blocked"


def _is_handled_failure(result: Mapping[str, Any]) -> bool:
    return result.get("handled") is True and result.get("failure_scope") in {
        "rule",
        "page",
    }


def _contains_infrastructure_failure(result: Mapping[str, Any]) -> bool:
    """识别单条或批量结果中的基础设施不可用信号。"""
    if result.get("failure_kind") == "infrastructure":
        return True
    raw_items = result.get("items")
    if not isinstance(raw_items, list):
        return False
    for item in raw_items:
        if not isinstance(item, Mapping):
            continue
        error = item.get("error")
        if not isinstance(error, Mapping):
            continue
        error_code = _text(error.get("code")) or ""
        if error_code.endswith(("_UNAVAILABLE", "_DEFERRED")):
            return True
    return False


def _selector_for_target(target: str) -> str:
    """把页面识别返回的文本目标转为可执行的单次点击选择器。"""
    if target.startswith(
        ("text=", "css=", "xpath=", "//", "#", ".", "[", ":", ">", "*")
    ) or _is_tagged_css_selector(target):
        return target
    return f"text={target}"


def _is_tagged_css_selector(target: str) -> bool:
    """识别以标签名开头的 CSS，避免把 ``div.van-tabbar`` 当成页面文本。"""
    if not target or not target[0].isascii() or not target[0].isalpha():
        return False
    return any(marker in target for marker in (".", "#", "[", ":", ">"))


def _first_obstruction_dismissal_action(
    actions: list[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    """每轮只选择一个低风险关闭动作，关闭后必须重新采集识别。"""
    for action in actions:
        if (_text(action.get("action")) or "").lower() != "click":
            continue
        risk_level = (_text(action.get("risk_level")) or "").lower()
        if risk_level in {"high", "critical"}:
            continue
        searchable = " ".join(
            filter(
                None,
                (
                    _text(action.get("target")),
                    _text(action.get("purpose")),
                    " ".join(_string_list(action.get("target_keywords"))),
                ),
            )
        ).lower()
        if any(
            keyword in searchable
            for keyword in ("关闭", "跳过", "我知道了", "稍后", "close", "skip")
        ):
            return action
    return None


def _page_not_found_result(page_capture_id: str | None) -> dict[str, Any]:
    return {
        "execution_status": "blocked",
        "error_code": "WORKING_MEMORY_PAGE_NOT_FOUND",
        "retryable": True,
        "page_capture_id": page_capture_id,
        "message": (
            "候选规则必须绑定当前 Run 的真实页面采集；请先调用 page_capture_tool，"
            "再把其返回的 page_capture_id 原样传给 candidate_rules_tool。"
        ),
    }


def _page_detection_gate_result(
    *,
    page_capture_id: str,
    requested_page_type: str | None,
    detection_result: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if detection_result is None:
        return {
            "execution_status": "blocked",
            "error_code": "PAGE_DETECTION_REQUIRED",
            "retryable": True,
            "page_capture_id": page_capture_id,
            "message": (
                "候选规则查询前必须先使用同一 page_capture_id 调用 page_detection_tool，"
                "并取得可进入规则匹配的识别结果。"
            ),
        }

    detection_status = _text(detection_result.get("detection_status"))
    detected_page_type = _text(detection_result.get("page_type"))
    rule_matching_allowed = detection_result.get("rule_matching_allowed") is True
    if detection_status != "recognized" or not rule_matching_allowed:
        return {
            "execution_status": "blocked",
            "error_code": "PAGE_DETECTION_NOT_RULE_MATCHABLE",
            "retryable": True,
            "page_capture_id": page_capture_id,
            "detection_status": detection_status,
            "detected_page_type": detected_page_type,
            "rule_matching_allowed": rule_matching_allowed,
            "message": (
                "该页面识别结果未满足 recognized 且 rule_matching_allowed=true，禁止查询候选规则。"
            ),
        }

    if requested_page_type != detected_page_type:
        return {
            "execution_status": "blocked",
            "error_code": "PAGE_DETECTION_PAGE_TYPE_MISMATCH",
            "retryable": True,
            "page_capture_id": page_capture_id,
            "requested_page_type": requested_page_type,
            "detected_page_type": detected_page_type,
            "message": "候选规则页面类型必须与同一页面采集的识别结果一致。",
        }
    return None


def _interaction_match_terms(rules: list[Mapping[str, Any]]) -> list[str]:
    """从规则契约提取可与页面候选动作核对的保守交互词。"""
    searchable = json.dumps(rules, ensure_ascii=False, default=str).casefold()
    terms: set[str] = set()
    if any(marker in searchable for marker in ("广告", '"category": "ad"', "advert", "promo")):
        terms.update({"广告", "推广", "advert", "banner", "promo"})
        if any(marker in searchable for marker in ("跳过", "skip")):
            terms.update({"跳过", "skip"})
        if any(marker in searchable for marker in ("关闭", "弹窗", "close", "dismiss")):
            terms.update({"关闭", "close", "dismiss"})
    marker_groups = (
        (
            ("关注", "收藏", "点赞", "follow", "favorite", "like"),
            {"关注", "收藏", "点赞", "follow", "favorite", "like"},
        ),
        (
            ("全屏", "fullscreen"),
            {"全屏", "fullscreen"},
        ),
        (
            ("下架", "不可下载", "offline", "unavailable"),
            {"下架", "不可下载", "失效", "offline", "unavailable"},
        ),
        (
            ("活动", "已结束", "已暂停", "已下线", "activity", "ended", "paused"),
            {"已结束", "已暂停", "已下线", "失效", "ended", "paused"},
        ),
    )
    for markers, matched_terms in marker_groups:
        if any(marker in searchable for marker in markers):
            terms.update(matched_terms)
    return sorted(terms)


def _looks_like_immediate_temporal_page(
    *, arguments: Mapping[str, Any], result: Mapping[str, Any]
) -> bool:
    """入口采集出现倒计时时立即多帧取证，禁止等待耗时页面识别。"""
    mode = (_text(result.get("mode")) or _text(arguments.get("mode")) or "").casefold()
    if mode == "timed_capture":
        return False
    text = _text(result.get("visible_text_excerpt")) or ""
    has_count = re.search(r"(?i)(?:\b\d{1,3}\s*s\b|\d{1,3}\s*秒)", text) is not None
    has_temporal_marker = any(
        marker in text.casefold()
        for marker in ("跳过", "关闭", "倒计时", "skip", "close", "countdown")
    )
    return has_count and has_temporal_marker


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _rule_key(value: Mapping[str, Any]) -> str | None:
    """返回批量输入或输出中的稳定规则标识。"""
    return _text(value.get("rule_id")) or _text(value.get("rule_code"))


def _integer(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _mapping_list(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for item in value if (text := _text(item)) is not None]
