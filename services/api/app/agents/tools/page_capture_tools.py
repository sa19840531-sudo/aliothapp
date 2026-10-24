"""页面采集 Tool：Agent 可调用的统一浏览器观测入口。"""

import asyncio
import hashlib
import json
import logging
import re
from functools import lru_cache, wraps
from typing import Annotated, Any, Literal
from uuid import uuid4
from weakref import WeakKeyDictionary

from pydantic import Field

from services.api.app.agents.runtime.run_context import get_agent_run_context
from services.api.app.agents.tools.registry import PermissionLevel, tool
from services.api.app.application.page_capture_persistence import (
    PageCapturePersistenceService,
    PersistedPageCapture,
)
from services.api.app.application.pipeline_shadow import (
    PipelineShadowService,
    ShadowComparison,
    should_sample_shadow,
)
from services.api.app.application.web_agent_page_capture import (
    AgentPageCaptureIntentError,
    AgentPageCaptureOutcome,
    AgentPageCaptureSpec,
    classify_agent_page_capture_supplemental_source,
    execute_agent_page_capture_with_browser,
)
from services.api.app.core.config import get_settings
from services.api.app.infrastructure.browser.page_capture import (
    TRANSIENT_GOTO_ERROR_MARKERS,
    BlockedPageCaptureActionError,
    PageCaptureBrowser,
    PageCaptureMode,
    PageCaptureRequest,
    PageInteractionFailedError,
    PlaywrightPageCaptureBrowser,
    capture_page_with_timeout,
)
from services.api.app.infrastructure.db.page_capture_repository import (
    PostgresPageCaptureRepository,
)
from services.api.app.infrastructure.db.pipeline_shadow_repository import (
    PostgresPipelineShadowRepository,
)
from services.api.app.infrastructure.storage.factory import build_evidence_artifact_store

logger = logging.getLogger(__name__)

VISIBLE_TEXT_EXCERPT_LIMIT = 4000
# Playwright 的 timeout_ms 只约束单个动作，DOM 序列化、截图等组合步骤仍可能
# 独立悬挂。Tool 必须有比单步更大的总时限，避免一个页面占满整个 Run 墙钟。
PAGE_CAPTURE_OPERATION_TIMEOUT_SECONDS = 180.0
# 线上 MinIO 或数据库写入同样必须有页面级边界；超时后当前动作失败，Frontier
# 继续派发下一页，不能把存储抖动升级为整任务停止。
PAGE_CAPTURE_PERSISTENCE_TIMEOUT_SECONDS = 60.0
_PAGE_CAPTURE_PIPELINE_GATES: WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    dict[int, asyncio.Semaphore],
] = WeakKeyDictionary()
_TRUSTED_TARGET_URL_CONTEXT_KEY = "_trusted_target_url"
_TARGET_URL_PLACEHOLDER_TOKENS = {
    "目标url",
    "原始任务上下文中的目标url",
    "原始任务上下文目标url",
    "任务上下文中的目标url",
    "任务目标url",
    "targeturl",
    "tasktargeturl",
}

PageCaptureModeParameter = Annotated[
    str,
    Field(
        description=(
            "采集模式，仅允许 goto、observe、timed_capture、single_trigger；"
            "新 URL 导航必须使用 goto"
        )
    ),
]
NavigationDepthParameter = Annotated[
    int | None,
    Field(
        ge=0,
        description=(
            "goto 目标页或 click 可能到达页相对任务入口的导航深度；"
            "入口页为 0，任务启用深度限制时必填；"
            "observe/timed_capture/single_trigger 只能作用于当前会话页面"
        ),
    ),
]
PageSelectorParameter = Annotated[
    str | None,
    Field(
        description=(
            "single_trigger 的 click/hover 目标选择器，或 submit_probe 的输入框选择器；"
            "必须与 trigger_action 分开传递；"
            "例如 trigger_action='click' 时 selector='text=关闭'。"
            "禁止把动作拼进 selector"
        )
    ),
]
PageTriggerActionParameter = Annotated[
    Literal["click", "back", "scroll", "hover", "network_fault", "submit_probe"] | None,
    Field(
        description=(
            "single_trigger 的单次动作，只能填写 click、back、scroll、hover、"
            "network_fault 或 submit_probe；back 表示浏览器历史返回且不需要 selector；"
            "click/hover 目标放 selector，network_fault 目标放 fault_url_pattern，"
            "submit_probe 另传输入框 selector、submit_selector 与 probe_text。"
            "正确：trigger_action='click', selector='text=关闭'；"
            "错误：trigger_action='click text=关闭'"
        )
    ),
]
ProbeTextParameter = Annotated[
    str | None,
    Field(
        max_length=80,
        description=(
            "submit_probe 专用中性探针文本，仅允许 ALIOTH_RATE_PROBE_ 或 "
            "ALIOTH_COMMENT_PROBE_ 前缀；其他动作不得使用"
        ),
    ),
]
SubmitSelectorParameter = Annotated[
    str | None,
    Field(description="submit_probe 专用提交按钮选择器，与输入框 selector 分开传递"),
]
FaultUrlPatternParameter = Annotated[
    str | None,
    Field(
        max_length=500,
        description=(
            "network_fault 专用目标 URL 正则，必须限定广告资源域名或路径，禁止 .* 等全请求匹配"
        ),
    ),
]


ExplorationActionIdParameter = Annotated[
    str | None,
    Field(description="从 working_memory frontier 中选择的本次探索动作 ID"),
]
SupersedesPageCaptureIdParameter = Annotated[
    str | None,
    Field(description="本次补采显式替代的旧 page_capture_id"),
]
CaptureRoleParameter = Annotated[
    str,
    Field(
        pattern=r"^[a-z][a-z0-9_]{0,63}$",
        description="本次采集在多帧证据链中的角色；普通单帧采集使用 primary",
    ),
]


def _get_page_capture_browser() -> PageCaptureBrowser:
    """兼容旧测试 monkeypatch；生产路径应走 ActionIntent Runtime。"""
    from services.api.app.infrastructure.browser.capture_runtime import (
        get_or_create_web_capture_runtime,
    )

    return get_or_create_web_capture_runtime().borrow_browser_for_legacy_agent_tool()


@lru_cache
def _get_page_capture_persistence_service() -> PageCapturePersistenceService:
    settings = get_settings()
    return PageCapturePersistenceService(
        repository=PostgresPageCaptureRepository(),
        artifact_store=build_evidence_artifact_store(settings.evidence_storage),
    )


@lru_cache
def _get_pipeline_shadow_service() -> PipelineShadowService:
    return PipelineShadowService(repository=PostgresPipelineShadowRepository())


def _get_page_capture_pipeline_gate() -> asyncio.Semaphore:
    settings = get_settings().quality_optimization
    concurrency = max(int(getattr(settings, "page_capture_concurrency", 1)), 1)
    loop = asyncio.get_running_loop()
    gates_by_concurrency = _PAGE_CAPTURE_PIPELINE_GATES.setdefault(loop, {})
    return gates_by_concurrency.setdefault(
        concurrency,
        asyncio.Semaphore(concurrency),
    )


def _limit_page_capture_pipeline(function: Any) -> Any:
    """把浏览器采集、证据持久化与影子采集纳入同一资源槽。"""

    @wraps(function)
    async def wrapped(*args: Any, **kwargs: Any) -> dict[str, Any]:
        async with _get_page_capture_pipeline_gate():
            return await function(*args, **kwargs)

    return wrapped


@_limit_page_capture_pipeline
async def capture_page_for_agent(
    *,
    mode: PageCaptureMode | str,
    url: str | None = None,
    selector: str | None = None,
    trigger_action: str | None = None,
    probe_text: str | None = None,
    submit_selector: str | None = None,
    fault_url_pattern: str | None = None,
    navigation_depth: int | None = None,
    exploration_action_id: str | None = None,
    supersedes_page_capture_id: str | None = None,
    clear_obstructions: bool = False,
    capture_role: str = "primary",
    inherit_parent_semantics: bool = False,
    capture_dom: bool = True,
    capture_visible_text: bool = True,
    capture_screenshot: bool = True,
    full_page: bool = True,
    frame_count: int = 3,
    interval_ms: int = 1000,
    timeout_ms: int = 30000,
    browser: PageCaptureBrowser | None = None,
    persistence_service: Any | None = None,
    shadow_service: Any | None = None,
    browser_operation_timeout_seconds: float | None = None,
    persistence_operation_timeout_seconds: float = PAGE_CAPTURE_PERSISTENCE_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """执行页面采集，持久化完整产物后仅返回适合 LLM 消费的摘要。"""
    context = get_agent_run_context()
    quality_optimization = get_settings().quality_optimization
    resolved_browser_operation_timeout_seconds = (
        browser_operation_timeout_seconds
        if browser_operation_timeout_seconds is not None
        else getattr(
            quality_optimization,
            "page_capture_timeout_seconds",
            PAGE_CAPTURE_OPERATION_TIMEOUT_SECONDS,
        )
    )
    viewport_width, viewport_height = _task_viewport(context)
    resolved_url = _resolve_trusted_navigation_url(mode=mode, url=url, context=context)
    resolved_mode = mode if isinstance(mode, PageCaptureMode) else PageCaptureMode(mode)
    task_id = (
        str(context.task_id).strip()
        if context is not None and getattr(context, "task_id", None)
        else "unbound-task"
    )
    session_id = (
        str(context.agent_run_id).strip()
        if context is not None and getattr(context, "agent_run_id", None)
        else f"unbound-session:{uuid4()}"
    )
    agent_run_id = session_id
    spec = AgentPageCaptureSpec(
        task_id=task_id,
        session_id=session_id,
        agent_run_id=agent_run_id,
        mode=resolved_mode,
        url=resolved_url,
        selector=selector,
        trigger_action=trigger_action,
        probe_text=probe_text,
        submit_selector=submit_selector,
        fault_url_pattern=fault_url_pattern,
        navigation_depth=navigation_depth,
        source_capture_id=supersedes_page_capture_id,
        clear_obstructions=clear_obstructions,
        capture_role=capture_role,
        inherit_parent_semantics=inherit_parent_semantics,
        capture_dom=capture_dom,
        capture_visible_text=capture_visible_text,
        capture_screenshot=capture_screenshot,
        full_page=full_page,
        frame_count=frame_count,
        interval_ms=interval_ms,
        timeout_ms=timeout_ms,
        viewport_width=viewport_width,
        viewport_height=viewport_height,
        timeout_seconds=float(resolved_browser_operation_timeout_seconds),
        persistence_timeout_seconds=float(persistence_operation_timeout_seconds),
        supplemental_evidence_source=classify_agent_page_capture_supplemental_source(
            mode=resolved_mode,
            trigger_action=trigger_action,
            capture_role=capture_role,
        ),
    )
    outcome: AgentPageCaptureOutcome | None = None
    try:
        # 显式注入或测试 Fake browser → 临时 Coordinator；生产 Playwright → Runtime
        effective_browser = browser
        if effective_browser is None:
            candidate = _get_page_capture_browser()
            if not isinstance(candidate, PlaywrightPageCaptureBrowser):
                effective_browser = candidate
        persist_for_coordinator = (
            persistence_service
            if persistence_service is not None
            else (
                _get_page_capture_persistence_service()
                if context is not None and getattr(context, "task_id", None)
                else None
            )
        )
        if effective_browser is not None:
            outcome = await execute_agent_page_capture_with_browser(
                browser=effective_browser,
                spec=spec,
                persistence_service=persist_for_coordinator,
            )
        else:
            from services.api.app.infrastructure.browser.capture_runtime import (
                get_or_create_web_capture_runtime,
            )

            outcome = await get_or_create_web_capture_runtime().run_agent_page_capture(
                spec,
                persistence_service=persist_for_coordinator,
            )
        result = outcome.action_browser_result
    except AgentPageCaptureIntentError as exc:
        error_code = exc.error_code
        error_message = str(exc)
        if error_code.startswith("SUPPLEMENTAL_EVIDENCE_"):
            logger.info(
                "task_id=%s agent_run_id=%s H5 补采动作已按服务端策略抑制，"
                "mode=%s trigger_action=%s reason_code=%s",
                task_id,
                agent_run_id,
                resolved_mode.value,
                trigger_action,
                error_code,
            )
        if error_code in {
            "PAGE_CAPTURE_PERSISTENCE_TIMEOUT",
            "PAGE_CAPTURE_PERSISTENCE_FAILED",
        }:
            return _page_failure_result(
                mode=mode,
                failure_stage="artifact_persistence",
                error_code=error_code,
                error_message=error_message,
                exploration_action_id=exploration_action_id,
                supersedes_page_capture_id=supersedes_page_capture_id,
                failure_kind="infrastructure",
                retryable=False,
            )
        if error_code in {"ACTION_TIMED_OUT", "ACTION_DEADLINE_EXCEEDED"}:
            return _page_failure_result(
                mode=mode,
                failure_stage="browser_capture",
                error_code="PAGE_CAPTURE_TIMEOUT",
                error_message=(
                    f"页面组合采集超过 {resolved_browser_operation_timeout_seconds:g} 秒，"
                    "已跳过当前动作并继续后续页面"
                ),
                exploration_action_id=exploration_action_id,
                supersedes_page_capture_id=supersedes_page_capture_id,
                retryable=False,
            )
        if error_code == "PAGE_CAPTURE_TIMEOUT":
            return _page_failure_result(
                mode=mode,
                failure_stage="browser_capture",
                error_code="PAGE_CAPTURE_TIMEOUT",
                error_message=(
                    error_message
                    if "页面组合采集超过" in error_message
                    else (
                        f"页面组合采集超过 {resolved_browser_operation_timeout_seconds:g} 秒，"
                        "已跳过当前动作并继续后续页面"
                    )
                ),
                exploration_action_id=exploration_action_id,
                supersedes_page_capture_id=supersedes_page_capture_id,
                retryable=False,
            )
        if error_code == "SURFACE_DRIVER_FAILED":
            transient_goto_failure = _is_transient_goto_error_message(error_message)
            return _page_failure_result(
                mode=mode,
                failure_stage="browser_capture",
                error_code="PAGE_CAPTURE_FAILED",
                error_message=(
                    error_message
                    if error_message.startswith("页面组合采集失败")
                    else f"页面组合采集失败：{error_message}"
                ),
                exploration_action_id=exploration_action_id,
                supersedes_page_capture_id=supersedes_page_capture_id,
                failure_kind=("infrastructure" if transient_goto_failure else None),
                retryable=transient_goto_failure,
            )
        if (
            error_code.startswith("SUPPLEMENTAL_EVIDENCE_")
            or "BLOCKED" in error_code
            or error_code in {
                "ACTION_REQUEST_REJECTED",
                "WEB_ACTION_UNSUPPORTED",
                "UNSAFE_INPUT_SUBMISSION_BLOCKED",
                "SEMANTIC_TARGET_MISSING",
                "SEMANTIC_TARGET_NOT_FOUND",
                "SEMANTIC_TARGET_AMBIGUOUS",
                "PAGE_CAPTURE_ACTION_BLOCKED",
            }
        ):
            return _blocked_result(
                mode=mode,
                error_message=error_message,
                error_code=(
                    error_code
                    if error_code.startswith(("PAGE_", "SUPPLEMENTAL_EVIDENCE_"))
                    or error_code.endswith("BLOCKED")
                    else "PAGE_CAPTURE_ACTION_BLOCKED"
                ),
                exploration_action_id=exploration_action_id,
                supersedes_page_capture_id=supersedes_page_capture_id,
            )
        return _page_failure_result(
            mode=mode,
            failure_stage=exc.failure_stage,
            error_code=error_code,
            error_message=error_message,
            exploration_action_id=exploration_action_id,
            supersedes_page_capture_id=supersedes_page_capture_id,
            failure_kind=("infrastructure" if exc.retryable else "platform_capability"),
            retryable=exc.retryable,
        )
    except BlockedPageCaptureActionError as exc:
        return _blocked_result(
            mode=mode,
            error_message=str(exc),
            error_code=exc.error_code,
            exploration_action_id=exploration_action_id,
            supersedes_page_capture_id=supersedes_page_capture_id,
        )
    except PageInteractionFailedError as exc:
        return _page_failure_result(
            mode=mode,
            failure_stage="browser_interaction",
            error_code=exc.error_code,
            error_message=str(exc),
            exploration_action_id=exploration_action_id,
            supersedes_page_capture_id=supersedes_page_capture_id,
            failure_kind="platform_capability",
            retryable=exc.retryable,
        )
    except TimeoutError:
        return _page_failure_result(
            mode=mode,
            failure_stage="browser_capture",
            error_code="PAGE_CAPTURE_TIMEOUT",
            error_message=(
                f"页面组合采集超过 {resolved_browser_operation_timeout_seconds:g} 秒，"
                "已跳过当前动作并继续后续页面"
            ),
            exploration_action_id=exploration_action_id,
            supersedes_page_capture_id=supersedes_page_capture_id,
        )
    except Exception as exc:  # noqa: BLE001  # 页面级异常必须结构化隔离
        transient_goto_failure = _is_transient_goto_error_message(str(exc))
        return _page_failure_result(
            mode=mode,
            failure_stage="browser_capture",
            error_code="PAGE_CAPTURE_FAILED",
            error_message=f"页面组合采集失败：{exc}",
            exploration_action_id=exploration_action_id,
            supersedes_page_capture_id=supersedes_page_capture_id,
            failure_kind=("infrastructure" if transient_goto_failure else None),
            retryable=transient_goto_failure,
        )

    unproven_dismissal = _unproven_obstruction_dismissal(result)
    if unproven_dismissal is not None:
        result_code = str(unproven_dismissal.get("result") or "unknown")
        failed_selector = str(
            unproven_dismissal.get("requested_selector")
            or unproven_dismissal.get("selector")
            or selector
            or ""
        )
        return _page_failure_result(
            mode=mode,
            failure_stage="browser_interaction",
            error_code="PAGE_OBSTRUCTION_DISMISSAL_NOT_PROVEN",
            error_message=(
                "页面遮挡关闭动作未证明命中有效关闭控件；"
                f"selector={failed_selector!r}, result={result_code}"
            ),
            exploration_action_id=exploration_action_id,
            supersedes_page_capture_id=supersedes_page_capture_id,
            failure_kind="interaction",
        )

    # CaptureCoordinator 已在 after Capture 完成持久化；无业务 task 时不对外暴露 id
    persisted: PersistedPageCapture | None = None
    if (
        outcome is not None
        and context is not None
        and getattr(context, "task_id", None)
    ):
        persisted = outcome.persisted
        if persisted is not None and persisted.page_capture_id != outcome.after_capture_id:
            persisted = PersistedPageCapture(
                page_capture_id=outcome.after_capture_id,
                page_seq=persisted.page_seq,
                artifacts=list(persisted.artifacts),
            )

    shadow_persisted: PersistedPageCapture | None = None
    settings = quality_optimization
    if (
        persisted is not None
        and context is not None
        and settings.pipeline_mode == "shadow"
        and should_sample_shadow(
            task_id=context.task_id,
            sample_rate=settings.shadow_sample_rate,
        )
        and resolved_mode in {PageCaptureMode.GOTO, PageCaptureMode.OBSERVE}
        and not _is_temporal_page_result(result)
    ):
        try:
            shadow_browser = (
                browser
                if browser is not None
                else _get_page_capture_browser()
            )
            shadow_result = await capture_page_with_timeout(
                request=PageCaptureRequest(
                    mode=PageCaptureMode.OBSERVE,
                    capture_dom=spec.capture_dom,
                    capture_visible_text=spec.capture_visible_text,
                    capture_screenshot=spec.capture_screenshot,
                    full_page=spec.full_page,
                    timeout_ms=spec.timeout_ms,
                    session_id=spec.session_id,
                    viewport_width=spec.viewport_width,
                    viewport_height=spec.viewport_height,
                ),
                browser=shadow_browser,
                timeout_seconds=resolved_browser_operation_timeout_seconds,
            )
            shadow_persisted = await asyncio.wait_for(
                (
                    persistence_service
                    if persistence_service is not None
                    else _get_page_capture_persistence_service()
                ).persist(
                    task_id=context.task_id,
                    agent_run_id=context.agent_run_id,
                    result=shadow_result,
                    parent_page_capture_id=persisted.page_capture_id,
                    capture_role="shadow_candidate",
                ),
                timeout=persistence_operation_timeout_seconds,
            )
            comparison_service = shadow_service or _get_pipeline_shadow_service()
            await comparison_service.record(
                ShadowComparison(
                    task_id=context.task_id,
                    agent_run_id=context.agent_run_id,
                    page_capture_id=persisted.page_capture_id,
                    comparison_mode="same_state_dual_capture",
                    legacy_conclusion=None,
                    candidate_conclusion=None,
                    legacy_duration_ms=_total_duration_ms(result),
                    candidate_duration_ms=_total_duration_ms(shadow_result),
                    legacy_evidence_artifact_ids=tuple(
                        item.artifact_id for item in persisted.artifacts
                    ),
                    candidate_evidence_artifact_ids=tuple(
                        item.artifact_id for item in shadow_persisted.artifacts
                    ),
                    summary={
                        "legacy_page_capture_id": persisted.page_capture_id,
                        "candidate_page_capture_id": shadow_persisted.page_capture_id,
                        "business_action_repeated": False,
                    },
                )
            )
        except Exception:  # noqa: BLE001 - 影子失败不得影响主流程结论
            shadow_persisted = None

    return _build_safe_result(
        result=result,
        persisted=persisted,
        exploration_action_id=exploration_action_id,
        supersedes_page_capture_id=supersedes_page_capture_id,
    )


def _is_temporal_page_result(result: Any) -> bool:
    visible_text = str(_artifact_content(result.artifacts, "visible_text") or "").casefold()
    return any(
        marker in visible_text for marker in ("倒计时", "秒后", "跳过广告", "countdown", "skip ad")
    )


def _task_viewport(context: Any) -> tuple[int | None, int | None]:
    """从任务配置读取 `宽x高`，无效值保持浏览器默认视口。"""
    task_config = getattr(context, "task_config", None)
    raw = task_config.get("viewport") if isinstance(task_config, dict) else None
    match = re.fullmatch(r"\s*(\d{3,4})\s*[xX×]\s*(\d{3,4})\s*", str(raw or ""))
    if match is None:
        return None, None
    width, height = int(match.group(1)), int(match.group(2))
    if width < 240 or height < 320 or width > 3840 or height > 3840:
        return None, None
    return width, height


def _resolve_trusted_navigation_url(
    *,
    mode: PageCaptureMode | str,
    url: str | None,
    context: Any,
) -> str | None:
    """将模型生成的目标地址占位符收敛到任务上下文中的可信入口。"""
    mode_value = mode.value if isinstance(mode, PageCaptureMode) else str(mode)
    if mode_value != PageCaptureMode.GOTO.value:
        return url
    task_config = getattr(context, "task_config", None)
    trusted_url = (
        task_config.get(_TRUSTED_TARGET_URL_CONTEXT_KEY)
        if isinstance(task_config, dict)
        else None
    )
    trusted_url = trusted_url.strip() if isinstance(trusted_url, str) else ""
    if not trusted_url:
        return url
    if not isinstance(url, str) or not url.strip():
        return trusted_url
    token = re.sub(r"[\s_`'\"<>\[\]【】《》（）()]+", "", url).casefold()
    if token not in _TARGET_URL_PLACEHOLDER_TOKENS:
        return url
    return trusted_url


def _total_duration_ms(result: Any) -> int | None:
    metrics = getattr(result, "metrics", None)
    timings = metrics.get("timings_ms") if isinstance(metrics, dict) else None
    value = timings.get("total") if isinstance(timings, dict) else None
    return int(round(float(value))) if isinstance(value, (int, float)) else None


async def close_page_capture_session(agent_run_id: str) -> None:
    """释放指定 Agent Run 持有的浏览器会话（经 Web Interaction Runtime）。"""
    from services.api.app.infrastructure.browser.capture_runtime import (
        get_or_create_web_capture_runtime,
    )

    await get_or_create_web_capture_runtime().close_browser_session(agent_run_id)


@tool(
    name="page_capture_tool",
    description=(
        "通过真实浏览器执行页面导航、只读观测、多帧定时采集或单次最小触发，"
        "完整 DOM、截图和日志写入证据存储，仅返回摘要与 artifact_id。"
    ),
    category="页面采集",
    permission_level=PermissionLevel.SENSITIVE,
)
async def page_capture_tool(
    mode: PageCaptureModeParameter = "goto",
    url: str | None = None,
    navigation_depth: NavigationDepthParameter = None,
    selector: PageSelectorParameter = None,
    trigger_action: PageTriggerActionParameter = None,
    probe_text: ProbeTextParameter = None,
    submit_selector: SubmitSelectorParameter = None,
    fault_url_pattern: FaultUrlPatternParameter = None,
    exploration_action_id: ExplorationActionIdParameter = None,
    supersedes_page_capture_id: SupersedesPageCaptureIdParameter = None,
    clear_obstructions: bool = False,
    capture_role: CaptureRoleParameter = "primary",
    inherit_parent_semantics: bool = False,
    capture_dom: bool = True,
    capture_visible_text: bool = True,
    capture_screenshot: bool = True,
    full_page: bool = True,
    frame_count: int = 3,
    interval_ms: int = 1000,
    timeout_ms: int = 30000,
) -> dict[str, Any]:
    """Agent 调用的页面采集入口；动作名和 selector 必须作为独立参数传递。"""
    return await capture_page_for_agent(
        mode=mode,
        url=url,
        selector=selector,
        trigger_action=trigger_action,
        probe_text=probe_text,
        submit_selector=submit_selector,
        fault_url_pattern=fault_url_pattern,
        navigation_depth=navigation_depth,
        exploration_action_id=exploration_action_id,
        supersedes_page_capture_id=supersedes_page_capture_id,
        clear_obstructions=clear_obstructions,
        capture_role=capture_role,
        inherit_parent_semantics=inherit_parent_semantics,
        capture_dom=capture_dom,
        capture_visible_text=capture_visible_text,
        capture_screenshot=capture_screenshot,
        full_page=full_page,
        frame_count=frame_count,
        interval_ms=interval_ms,
        timeout_ms=timeout_ms,
    )


_UNPROVEN_OBSTRUCTION_DISMISSAL_RESULTS = {
    "popup_dismissal_target_not_proven",
    "popup_overlay_outside_point_not_proven",
}


def _unproven_obstruction_dismissal(result: Any) -> dict[str, Any] | None:
    """提取未证明成功的遮挡关闭动作，避免把无效点击当成新页面。"""
    for item in getattr(result, "action_trace", []) or []:
        if not isinstance(item, dict):
            continue
        if item.get("result") in _UNPROVEN_OBSTRUCTION_DISMISSAL_RESULTS:
            return item
    return None


def _blocked_result(
    *,
    mode: str,
    error_message: str,
    error_code: str = "PAGE_CAPTURE_ACTION_BLOCKED",
    exploration_action_id: str | None = None,
    supersedes_page_capture_id: str | None = None,
) -> dict[str, Any]:
    return {
        "execution_status": "blocked",
        "error_code": error_code,
        "message": error_message,
        "page_capture_id": None,
        "artifact_ids": [],
        "mode": mode,
        "exploration_action_id": exploration_action_id,
        "supersedes_page_capture_id": supersedes_page_capture_id,
        "reachable": None,
        "final_url": None,
        "title": None,
        "status_code": None,
        "artifacts": [],
        "console_logs": [],
        "network_requests": [],
        "action_trace": [],
        "post_action_snapshot": None,
        "error_message": error_message,
    }


def _is_transient_goto_error_message(message: str) -> bool:
    """识别浏览器入口导航阶段的瞬态网络关闭错误。"""
    return any(marker in message for marker in TRANSIENT_GOTO_ERROR_MARKERS)

def _page_failure_result(
    *,
    mode: str,
    failure_stage: str,
    error_code: str,
    error_message: str,
    exploration_action_id: str | None,
    supersedes_page_capture_id: str | None,
    failure_kind: str | None = None,
    retryable: bool = False,
) -> dict[str, Any]:
    """返回可由页面记忆中间件隔离并关闭当前动作的失败结果。"""
    return {
        "execution_status": "error",
        "handled": True,
        "failure_scope": "page",
        "failure_stage": failure_stage,
        "failure_kind": failure_kind,
        "error_code": error_code,
        "error_message": error_message,
        "message": error_message,
        "retryable": retryable,
        "failure_isolated": True,
        "page_capture_id": None,
        "artifact_ids": [],
        "mode": mode,
        "exploration_action_id": exploration_action_id,
        "supersedes_page_capture_id": supersedes_page_capture_id,
        "reachable": None,
        "final_url": None,
        "title": None,
        "status_code": None,
        "artifacts": [],
        "console_logs": [],
        "network_requests": [],
        "action_trace": [],
        "post_action_snapshot": None,
    }


def _build_safe_result(
    *,
    result: Any,
    persisted: PersistedPageCapture | None,
    exploration_action_id: str | None = None,
    supersedes_page_capture_id: str | None = None,
) -> dict[str, Any]:
    persisted_by_type: dict[str, list[Any]] = {}
    if persisted is not None:
        for artifact in persisted.artifacts:
            persisted_by_type.setdefault(artifact.artifact_type, []).append(artifact)
    visible_text = _artifact_content(result.artifacts, "visible_text")
    dom = _artifact_content(result.artifacts, "dom")
    ui_snapshot = _artifact_content(result.artifacts, "ui_snapshot")
    page_coverage = _artifact_content(result.artifacts, "page_coverage")
    content_fingerprint = _content_fingerprint(dom=dom, visible_text=visible_text)
    visible_text_fingerprint = _visible_text_fingerprint(visible_text)
    screenshots = [
        artifact for artifact in result.artifacts if artifact.artifact_type == "screenshot"
    ]
    artifacts = []
    for artifact in result.artifacts:
        if artifact.artifact_type == "action_trace":
            continue
        stored_candidates = persisted_by_type.get(artifact.artifact_type, [])
        stored = stored_candidates.pop(0) if stored_candidates else None
        artifacts.append(
            {
                "artifact_type": artifact.artifact_type,
                "artifact_id": stored.artifact_id if stored is not None else None,
                "content_type": stored.content_type if stored is not None else None,
                "size_bytes": (
                    stored.size_bytes
                    if stored is not None
                    else len(json.dumps(artifact.content, ensure_ascii=False, default=str).encode())
                ),
            }
        )
    status_counts: dict[str, int] = {}
    for event in result.network_requests:
        status = event.get("status")
        if status is not None:
            key = str(status)
            status_counts[key] = status_counts.get(key, 0) + 1
    artifact_ids = [item["artifact_id"] for item in artifacts if item["artifact_id"]]
    return {
        "execution_status": result.execution_status,
        "page_capture_id": persisted.page_capture_id if persisted is not None else None,
        "artifact_ids": artifact_ids,
        "mode": result.mode,
        "exploration_action_id": exploration_action_id,
        "supersedes_page_capture_id": supersedes_page_capture_id,
        "reachable": result.reachable,
        "final_url": result.final_url,
        "title": result.title,
        "status_code": result.status_code,
        "page_seq": persisted.page_seq if persisted is not None else None,
        "content_fingerprint": content_fingerprint,
        "visible_text_fingerprint": visible_text_fingerprint,
        "artifacts": artifacts,
        "visible_text_excerpt": str(visible_text or "")[:VISIBLE_TEXT_EXCERPT_LIMIT],
        "visible_text_truncated": len(str(visible_text or "")) > VISIBLE_TEXT_EXCERPT_LIMIT,
        "dom_summary": {"character_count": len(str(dom or ""))},
        "ui_summary": _safe_ui_summary(ui_snapshot),
        "screenshot_summary": {"count": len(screenshots)},
        "page_coverage_summary": (
            {
                key: page_coverage.get(key)
                for key in (
                    "schema_version",
                    "container_selector",
                    "covered_content_fingerprint_count",
                    "termination_reason",
                    "collection_complete",
                    "bottom_reached",
                    "coverage_complete",
                    "coverage_status",
                    "continuity_segment_count",
                    "continuity_gap_count",
                    "coverage_gap_reason",
                    "terminal_capture_role",
                    "terminal_ui_snapshot_frame_index",
                    "terminal_screenshot_frame_index",
                    "terminal_scroll_state",
                )
            }
            if isinstance(page_coverage, dict)
            else None
        ),
        "console_summary": {
            "count": len(result.console_logs),
            "error_count": sum(1 for item in result.console_logs if item.get("type") == "error"),
        },
        "network_summary": {
            "event_count": len(result.network_requests),
            "request_count": sum(
                1 for item in result.network_requests if item.get("event") == "request"
            ),
            "response_count": sum(
                1 for item in result.network_requests if item.get("event") == "response"
            ),
            "status_counts": status_counts,
        },
        "action_summary": {
            "count": len(result.action_trace),
            "transition_kinds": [
                str(item["transition_kind"])
                for item in result.action_trace
                if isinstance(item, dict) and item.get("transition_kind")
            ],
            "before_urls": [
                str(item["before_url"])
                for item in result.action_trace
                if isinstance(item, dict) and item.get("before_url")
            ],
            "after_urls": [
                str(item["after_url"])
                for item in result.action_trace
                if isinstance(item, dict) and item.get("after_url")
            ],
        },
        "metrics": dict(result.metrics),
        "error_message": result.error_message,
    }


def _artifact_content(artifacts: list[Any], artifact_type: str) -> Any:
    for artifact in artifacts:
        if artifact.artifact_type == artifact_type:
            return artifact.content
    return None


def _content_fingerprint(*, dom: Any, visible_text: Any) -> str | None:
    """生成不可逆页面内容指纹，用于区分多层同选择器遮挡。"""
    if dom is None and visible_text is None:
        return None
    payload = f"{dom or ''}\0{visible_text or ''}".encode("utf-8", errors="replace")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _visible_text_fingerprint(visible_text: Any) -> str | None:
    """生成忽略空白波动的可见文本指纹，用于识别动态页面上的无效交互。"""
    if visible_text is None:
        return None
    normalized = " ".join(str(visible_text).split())
    if not normalized:
        return None
    payload = normalized.encode("utf-8", errors="replace")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"

def _safe_ui_summary(ui_snapshot: Any) -> dict[str, Any]:
    """返回可供 Agent 选择目标索引的有界非敏感几何摘要。"""
    if not isinstance(ui_snapshot, dict):
        return {
            "count": 0,
            "truncated": False,
            "elements": [],
            "document_state": None,
            "media": [],
        }
    raw_elements = ui_snapshot.get("elements")
    if not isinstance(raw_elements, list):
        return {
            "count": 0,
            "truncated": False,
            "elements": [],
            "document_state": None,
            "media": [],
        }
    elements: list[dict[str, Any]] = []
    for item in raw_elements[:100]:
        if not isinstance(item, dict):
            continue
        elements.append(
            {
                "element_index": item.get("element_index"),
                "tag": item.get("tag"),
                "role": item.get("role"),
                "text": str(item.get("text") or "")[:100],
                "aria_label": str(item.get("aria_label") or "")[:100],
                "disabled": bool(item.get("disabled")),
                "hit_bounds": item.get("hit_bounds"),
                "visual_bounds": item.get("visual_bounds"),
                "text_metrics": _safe_text_metrics(item.get("text_metrics")),
                "truncation": _safe_truncation_summary(item),
            }
        )
    return {
        "count": len(raw_elements),
        "truncated": bool(ui_snapshot.get("truncated")) or len(raw_elements) > 100,
        "elements": elements,
        "document_state": _safe_document_state(ui_snapshot.get("document_state")),
        "media": _safe_media_summary(ui_snapshot.get("media")),
    }


def _safe_document_state(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        "fullscreen_active": bool(value.get("fullscreen_active")),
        "fullscreen_tag": str(value.get("fullscreen_tag") or "")[:32] or None,
    }


def _safe_text_metrics(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    allowed_keys = (
        "scroll_width",
        "client_width",
        "offset_width",
        "scroll_height",
        "client_height",
        "offset_height",
        "horizontal_overflow",
        "vertical_overflow",
        "text_overflow_ellipsis",
        "line_clamp",
    )
    return {key: value.get(key) for key in allowed_keys if key in value}


def _safe_truncation_summary(item: dict[str, Any]) -> dict[str, Any] | None:
    metrics = item.get("text_metrics")
    style = item.get("style")
    if not isinstance(metrics, dict) and not isinstance(style, dict):
        return None
    style_mapping = style if isinstance(style, dict) else {}
    metrics_mapping = metrics if isinstance(metrics, dict) else {}
    horizontal_overflow = bool(metrics_mapping.get("horizontal_overflow"))
    vertical_overflow = bool(metrics_mapping.get("vertical_overflow"))
    text_overflow = str(style_mapping.get("text_overflow") or "")
    white_space = str(style_mapping.get("white_space") or "")
    line_clamp = (
        metrics_mapping.get("line_clamp")
        or style_mapping.get("webkit_line_clamp")
        or style_mapping.get("line_clamp")
    )
    line_clamp_text = str(line_clamp)[:32] if line_clamp not in (None, "") else None
    clipped = horizontal_overflow or vertical_overflow or text_overflow == "ellipsis"
    return {
        "horizontal_overflow": horizontal_overflow,
        "vertical_overflow": vertical_overflow,
        "text_overflow": text_overflow[:32] or None,
        "white_space": white_space[:32] or None,
        "line_clamp": line_clamp_text,
        "clipped": clipped,
    }


def _safe_media_summary(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [
        {
            "media_index": item.get("media_index"),
            "tag": item.get("tag"),
            "class_tokens": (
                [str(token)[:100] for token in item.get("class_tokens", [])[:10]]
                if isinstance(item.get("class_tokens"), list)
                else []
            ),
            "bounds": item.get("bounds"),
            "viewport_coverage": item.get("viewport_coverage"),
            "state": _safe_media_state(item.get("state")),
        }
        for item in value[:20]
        if isinstance(item, dict)
    ]


def _safe_media_state(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        key: value.get(key)
        for key in (
            "paused",
            "ended",
            "muted",
            "controls",
            "current_time",
            "duration",
            "ready_state",
        )
        if key in value
    }
