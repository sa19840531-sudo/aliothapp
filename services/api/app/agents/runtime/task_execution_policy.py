"""任务级执行策略：解析护栏覆盖，并对页面探索边界做代码级约束。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from agent_framework import FunctionInvocationContext, FunctionMiddleware

from services.api.app.agents.runtime.business_middlewares.base import extract_arguments
from services.api.app.agents.runtime.working_memory import WorkingMemory
from services.api.app.core.config import AgentRuntimeSettings


class InvalidTaskExecutionPolicyError(ValueError):
    """任务执行策略包含无法安全应用的值。"""


def resolve_task_runtime_settings(
    defaults: AgentRuntimeSettings,
    loop_guard: Mapping[str, Any],
    page_sampling_policy: Mapping[str, Any] | None = None,
) -> AgentRuntimeSettings:
    """把任务级 ``loop_guard`` 覆盖到全局默认值，返回当次 Run 的独立配置。"""
    values = defaults.model_dump()
    _apply_positive_int(values, "max_steps", loop_guard, "max_steps")
    has_explicit_max_steps = loop_guard.get("max_steps") is not None
    if has_explicit_max_steps:
        values["adaptive_page_budget"] = False
    _apply_non_negative_int(
        values,
        "max_continuation_attempts",
        loop_guard,
        "max_continuation_attempts",
    )
    _apply_positive_int(
        values,
        "max_consecutive_infrastructure_failures",
        loop_guard,
        "max_consecutive_infrastructure_failures",
    )
    _apply_first_positive_int(
        values,
        "max_iterations",
        loop_guard,
        ("max_iterations", "max_ai_calls"),
    )
    _apply_first_positive_int(
        values,
        "stuck_window",
        loop_guard,
        ("stuck_window", "no_progress_limit"),
    )
    _apply_positive_int(
        values,
        "retry_max_attempts",
        loop_guard,
        "retry_max_attempts",
    )
    if loop_guard.get("retry_backoff_seconds") is not None:
        values["retry_backoff_seconds"] = _positive_number(
            loop_guard["retry_backoff_seconds"],
            "retry_backoff_seconds",
        )

    duration_value = loop_guard.get("max_duration_seconds")
    duration_key = "max_duration_seconds"
    if duration_value is None and "timeout_minutes" in loop_guard:
        duration_value = _positive_number(loop_guard["timeout_minutes"], "timeout_minutes") * 60
        duration_key = "timeout_minutes"
    if duration_value is not None:
        values["max_duration_seconds"] = _positive_number(duration_value, duration_key)

    sampling_policy = page_sampling_policy or {}
    max_pages = _optional_positive_int(sampling_policy, "max_pages")
    if not has_explicit_max_steps and max_pages is not None:
        values["max_steps"] = max(
            values["max_steps"],
            values["page_budget_base_steps"] + max_pages * values["page_budget_steps_per_page"],
        )
    values["max_steps"] = min(values["max_steps"], values["max_steps_hard_cap"])

    return AgentRuntimeSettings.model_validate(values)


class TaskExecutionPolicyMiddleware(FunctionMiddleware):
    """约束页面采集页数、页面类型范围和显式导航深度。

    页面图本身尚未由确定性爬虫维护，因此深度由 ``page_capture_tool`` 的
    ``navigation_depth`` 显式声明；中间件强制入口页为 0、后续新页面必须声明正深度，
    并拒绝超过任务上限的调用。页数按成功导航到的不同 URL 计数。
    """

    def __init__(
        self,
        *,
        task_config: Mapping[str, Any],
        page_sampling_policy: Mapping[str, Any],
        loop_guard: Mapping[str, Any],
        page_capture_tool: str = "page_capture_tool",
        candidate_rules_tool: str = "candidate_rules_tool",
        exploration_closure_tool: str = "exploration_closure_tool",
    ) -> None:
        self._page_capture_tool = page_capture_tool
        self._candidate_rules_tool = candidate_rules_tool
        self._exploration_closure_tool = exploration_closure_tool
        self._max_pages = _optional_positive_int(page_sampling_policy, "max_pages")
        depth_limits = [
            value
            for value in (
                _optional_non_negative_int(page_sampling_policy, "depth_limit"),
                _optional_non_negative_int(loop_guard, "max_depth"),
                _optional_non_negative_int(task_config, "max_depth"),
            )
            if value is not None
        ]
        self._max_depth = min(depth_limits) if depth_limits else None
        self._included_page_types = _string_set(page_sampling_policy.get("include_page_types"))
        self._excluded_page_types = _string_set(page_sampling_policy.get("exclude_page_types"))
        self._rule_target_page_types = _string_set(
            page_sampling_policy.get("rule_target_page_types")
        )
        self._visited_urls: set[str] = set()
        self._visited_page_count = 0
        self._navigation_lock = asyncio.Lock()
        self._working_memory: WorkingMemory | None = None

    def bind_working_memory(self, working_memory: WorkingMemory) -> None:
        """绑定当次 Run 的工作记忆，用于把策略阻断同步为探索终态。"""
        self._working_memory = working_memory
        working_memory.set_rule_target_page_types(self._rule_target_page_types)

    def _effective_page_count(self) -> int:
        if self._working_memory is None:
            return self._visited_page_count
        return max(self._visited_page_count, self._working_memory.discovered_page_work_count())

    async def process(
        self,
        context: FunctionInvocationContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        tool_name = context.function.name
        arguments = extract_arguments(context.arguments)
        is_page_capture = tool_name == self._page_capture_tool
        if is_page_capture:
            arguments = self._enrich_bound_frontier_capture(context, arguments)
        requested_url = _normalize_url(arguments.get("url"))
        is_url_navigation = is_page_capture and requested_url is not None
        is_interactive_trigger = arguments.get("mode") == "single_trigger" and arguments.get(
            "trigger_action"
        ) in {"click", "submit_probe"}
        is_declared_click_navigation = (
            arguments.get("trigger_action") == "click"
            and is_interactive_trigger
            and arguments.get("navigation_depth") is not None
        )
        if is_page_capture and (is_url_navigation or is_declared_click_navigation):
            async with self._navigation_lock:
                if blocked := self._navigation_block(
                    arguments,
                    click_may_navigate=is_declared_click_navigation and not is_url_navigation,
                ):
                    self._close_blocked_exploration(arguments, blocked)
                    context.result = blocked
                    return
                await call_next()
                self._remember_successful_navigation(arguments, context.result)
            return

        if is_page_capture and is_interactive_trigger:
            async with self._navigation_lock:
                await call_next()
                if blocked := self._post_click_navigation_block(arguments, context.result):
                    context.result = blocked
                    return
                self._remember_successful_navigation(arguments, context.result)
            return

        if tool_name == self._candidate_rules_tool:
            if blocked := self._page_type_block(arguments):
                self._abandon_excluded_page(arguments, blocked)
                context.result = blocked
                return

        if tool_name == self._exploration_closure_tool:
            if blocked := self._exploration_closure_block(arguments):
                context.result = blocked
                return

        await call_next()

    def _exploration_closure_block(self, arguments: Mapping[str, Any]) -> dict[str, Any] | None:
        """禁止模型以自然语言理由提前关闭仍可执行的低风险 Frontier。"""
        if self._working_memory is None:
            return None
        if self._max_pages is not None and self._effective_page_count() >= self._max_pages:
            return None
        if not self._working_memory.has_runnable_frontier_for_closure(
            exploration_action_ids=_string_list(arguments.get("exploration_action_ids")),
            close_all_pending_actions=(arguments.get("close_all_pending_actions") is True),
            max_depth=self._max_depth,
        ):
            return None
        return _blocked(
            "EXPLORATION_CLOSURE_RUNNABLE_FRONTIER",
            "仍存在未超过运行时深度限制的低风险 Frontier；"
            "必须先执行该动作，不能以模型推断的页序或预算理由提前收口。",
            retryable=True,
        )

    def _enrich_bound_frontier_capture(
        self,
        context: FunctionInvocationContext,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        """在页面预算校验前补齐唯一 Frontier 的受控同会话参数。"""
        enriched = dict(arguments)
        if self._working_memory is None:
            context.arguments = enriched
            return enriched

        action_id = str(enriched.get("exploration_action_id") or "").strip() or None
        if action_id is None:
            mode = str(enriched.get("mode") or "").strip()
            action = (
                str(enriched.get("trigger_action") or "").strip()
                if mode == "single_trigger"
                else mode
            )
            action_id = self._working_memory.resolve_mandatory_page_action(action=action)
            if action_id is None:
                action_id = self._working_memory.resolve_unique_frontier_page_action(action=action)
            if action_id is not None:
                enriched["exploration_action_id"] = action_id

        mandatory_defaults = self._working_memory.mandatory_page_action_capture_defaults(action_id)
        frontier_defaults = self._working_memory.frontier_action_capture_defaults(action_id)
        if mandatory_defaults is not None:
            action = mandatory_defaults["action"]
            target = mandatory_defaults["target"]
            if action == "click":
                # 自动重定向后的关闭动作必须在当前页面执行，旧 URL 不得参与导航判定。
                enriched.pop("url", None)
                enriched.setdefault("mode", "single_trigger")
                enriched.setdefault("trigger_action", "click")
                enriched.setdefault("selector", _selector_for_target(target))
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
                frame_count = enriched.get("frame_count")
                enriched["frame_count"] = max(
                    frame_count if isinstance(frame_count, int) else 0,
                    8,
                )
                enriched.setdefault("interval_ms", 1000)
            supersedes_page_capture_id = mandatory_defaults.get("supersedes_page_capture_id")
            if supersedes_page_capture_id is not None:
                enriched.setdefault("supersedes_page_capture_id", supersedes_page_capture_id)
            if mandatory_defaults.get("clear_obstructions") is True:
                enriched["clear_obstructions"] = True
        elif frontier_defaults is not None:
            action = frontier_defaults["action"]
            target = frontier_defaults["target"]
            if action == "click":
                # 普通 Frontier 也只能执行其已记录的目标，防止模型漏传或改写 selector。
                enriched.pop("url", None)
                enriched["mode"] = "single_trigger"
                enriched["trigger_action"] = "click"
                enriched["selector"] = _selector_for_target(str(target))
            elif action == "observe":
                enriched.pop("url", None)
                enriched["mode"] = "observe"
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
                frame_count = enriched.get("frame_count")
                enriched["frame_count"] = max(
                    frame_count if isinstance(frame_count, int) else 0,
                    8,
                )
                enriched.setdefault("interval_ms", 1000)

        if frontier_defaults is not None:
            navigation_depth = frontier_defaults.get("navigation_depth")
            if isinstance(navigation_depth, int):
                enriched["navigation_depth"] = navigation_depth
            supersedes_page_capture_id = frontier_defaults.get("supersedes_page_capture_id")
            if isinstance(supersedes_page_capture_id, str):
                enriched["supersedes_page_capture_id"] = supersedes_page_capture_id
            if frontier_defaults.get("clear_obstructions") is True:
                enriched["clear_obstructions"] = True

        context.arguments = enriched
        return enriched

    def _close_blocked_exploration(
        self, arguments: Mapping[str, Any], blocked: Mapping[str, Any]
    ) -> None:
        """把达到硬预算的当前 Frontier 动作同步为 skipped。"""
        if self._working_memory is None or blocked.get("retryable") is True:
            return
        action_id = str(arguments.get("exploration_action_id") or "").strip()
        if not action_id:
            return
        self._working_memory.close_exploration_items(
            action_status="skipped",
            reason=str(blocked.get("error_code") or "execution_policy_blocked").lower(),
            exploration_action_ids=[action_id],
            close_all_pending_actions=False,
            page_capture_ids=[],
            abandon_all_pending_pages=False,
        )

    def _abandon_excluded_page(
        self, arguments: Mapping[str, Any], blocked: Mapping[str, Any]
    ) -> None:
        """页面类型被抽样策略排除时关闭对应未检查页面。"""
        if self._working_memory is None:
            return
        page_capture_id = str(arguments.get("page_capture_id") or "").strip()
        if not page_capture_id:
            return
        self._working_memory.abandon_unmatchable_page(
            page_capture_id=page_capture_id,
            reason=str(blocked.get("error_code") or "page_type_sampling_blocked").lower(),
        )

    def _navigation_block(
        self,
        arguments: Mapping[str, Any],
        *,
        click_may_navigate: bool,
    ) -> dict[str, Any] | None:
        requested_url = _normalize_url(arguments.get("url"))
        is_new_page = click_may_navigate or (
            requested_url is not None and requested_url not in self._visited_urls
        )
        if not is_new_page:
            return None

        if self._max_pages is not None and self._effective_page_count() >= self._max_pages:
            return _blocked(
                "PAGE_SAMPLING_LIMIT_REACHED",
                f"页面采样上限为 {self._max_pages} 个逻辑页面，本次新页面导航已阻断。",
            )

        if self._max_depth is None:
            return None
        raw_depth = arguments.get("navigation_depth")
        if raw_depth is None:
            if not self._visited_urls:
                depth = 0
            else:
                return _blocked(
                    "NAVIGATION_DEPTH_REQUIRED",
                    "任务启用了深度限制，后续新页面导航必须显式传入 navigation_depth。",
                    retryable=True,
                )
        else:
            depth = _non_negative_int(raw_depth, "navigation_depth")

        if not self._visited_urls and depth != 0:
            return _blocked(
                "INVALID_ENTRY_NAVIGATION_DEPTH",
                "入口页 navigation_depth 必须为 0。",
                retryable=True,
            )
        if self._visited_urls and depth == 0:
            return _blocked(
                "INVALID_NAVIGATION_DEPTH",
                "后续新页面 navigation_depth 必须大于 0。",
                retryable=True,
            )
        if depth > self._max_depth:
            return _blocked(
                "NAVIGATION_DEPTH_LIMIT_REACHED",
                f"任务最大导航深度为 {self._max_depth}，本次 depth={depth} 已阻断。",
            )
        return None

    def _post_click_navigation_block(
        self, arguments: Mapping[str, Any], result: Any
    ) -> dict[str, Any] | None:
        payload = _result_mapping(result)
        if payload is None or payload.get("execution_status") != "success":
            return None
        action_summary = payload.get("action_summary")
        if isinstance(action_summary, Mapping):
            transition_kinds = action_summary.get("transition_kinds")
            if isinstance(transition_kinds, list) and "lifecycle" in transition_kinds:
                return None
            # 稳定等待期间页面可能先完成生命周期跳转，随后才执行关闭遮挡动作。
            # 此时最终 URL 已经是动作执行前 URL，不能把这次自然跳转误判为点击导航。
            before_urls = action_summary.get("before_urls")
            final_url = _normalize_url(payload.get("final_url"))
            normalized_before_urls = (
                {
                    normalized
                    for item in before_urls
                    if (normalized := _normalize_url(item)) is not None
                }
                if isinstance(before_urls, list)
                else set()
            )
            if final_url is not None and final_url in normalized_before_urls:
                return None
        final_url = _normalize_url(payload.get("final_url"))
        requested_url = _normalize_url(arguments.get("url"))
        if final_url is None and requested_url is None:
            return None
        post_arguments = dict(arguments)
        post_arguments["url"] = final_url or requested_url
        return self._navigation_block(post_arguments, click_may_navigate=False)

    def _remember_successful_navigation(self, arguments: Mapping[str, Any], result: Any) -> None:
        payload = _result_mapping(result)
        if payload is None or payload.get("execution_status") != "success":
            return
        normalized_urls = {
            normalized
            for candidate in (arguments.get("url"), payload.get("final_url"))
            if (normalized := _normalize_url(candidate)) is not None
        }
        if normalized_urls and normalized_urls.isdisjoint(self._visited_urls):
            self._visited_page_count += 1
        self._visited_urls.update(normalized_urls)

    def _page_type_block(self, arguments: Mapping[str, Any]) -> dict[str, Any] | None:
        page_type = str(arguments.get("page_type") or "").strip()
        if not page_type:
            return None
        if page_type in self._excluded_page_types:
            return _blocked(
                "PAGE_TYPE_EXCLUDED_BY_SAMPLING",
                f"page_type={page_type} 已被任务页面抽样策略排除。",
            )
        if self._included_page_types and page_type not in self._included_page_types:
            return _blocked(
                "PAGE_TYPE_NOT_INCLUDED_BY_SAMPLING",
                f"page_type={page_type} 不在任务页面抽样白名单内。",
            )
        return None


def _apply_positive_int(
    target: dict[str, Any],
    target_key: str,
    source: Mapping[str, Any],
    source_key: str,
) -> None:
    value = source.get(source_key)
    if value is not None:
        target[target_key] = _positive_int(value, source_key)


def _apply_non_negative_int(
    target: dict[str, Any],
    target_key: str,
    source: Mapping[str, Any],
    source_key: str,
) -> None:
    value = source.get(source_key)
    if value is not None:
        target[target_key] = _non_negative_int(value, source_key)


def _apply_first_positive_int(
    target: dict[str, Any],
    target_key: str,
    source: Mapping[str, Any],
    source_keys: tuple[str, ...],
) -> None:
    for source_key in source_keys:
        if source.get(source_key) is None:
            continue
        target[target_key] = _positive_int(source[source_key], source_key)
        return


def _optional_positive_int(source: Mapping[str, Any], key: str) -> int | None:
    return _positive_int(source[key], key) if source.get(key) is not None else None


def _optional_non_negative_int(source: Mapping[str, Any], key: str) -> int | None:
    return _non_negative_int(source[key], key) if source.get(key) is not None else None


def _positive_int(value: Any, key: str) -> int:
    parsed = _integer(value, key)
    if parsed <= 0:
        raise InvalidTaskExecutionPolicyError(f"{key} 必须为正整数")
    return parsed


def _non_negative_int(value: Any, key: str) -> int:
    parsed = _integer(value, key)
    if parsed < 0:
        raise InvalidTaskExecutionPolicyError(f"{key} 必须为非负整数")
    return parsed


def _integer(value: Any, key: str) -> int:
    if isinstance(value, bool):
        raise InvalidTaskExecutionPolicyError(f"{key} 必须为整数")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidTaskExecutionPolicyError(f"{key} 必须为整数") from exc
    if isinstance(value, float) and not value.is_integer():
        raise InvalidTaskExecutionPolicyError(f"{key} 必须为整数")
    return parsed


def _positive_number(value: Any, key: str) -> float:
    if isinstance(value, bool):
        raise InvalidTaskExecutionPolicyError(f"{key} 必须为正数")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise InvalidTaskExecutionPolicyError(f"{key} 必须为正数") from exc
    if parsed <= 0:
        raise InvalidTaskExecutionPolicyError(f"{key} 必须为正数")
    return parsed


def _string_set(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {str(item).strip() for item in value if str(item).strip()}


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _normalize_url(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    parsed = urlsplit(text)
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query, ""))


def _selector_for_target(target: str) -> str:
    """将 Frontier 的文本目标转换为页面采集 Tool 可执行的点击选择器。"""
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


def _result_mapping(result: Any) -> dict[str, Any] | None:
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


def _blocked(
    error_code: str,
    message: str,
    *,
    retryable: bool = False,
) -> dict[str, Any]:
    return {
        "execution_status": "blocked",
        "error_code": error_code,
        "retryable": retryable,
        "message": message,
    }
