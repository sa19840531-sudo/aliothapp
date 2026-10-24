"""桌面 Web Catalog 菜单导航适配器与生产入口。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime
from typing import Protocol

from services.api.app.application.capture_coordination import (
    CaptureCoordinationCommand,
    CaptureCoordinationError,
)
from services.api.app.application.capture_coordinator import CaptureCoordinator
from services.api.app.application.web_browser_profile import WebBrowserProfileRegistry
from services.api.app.application.web_desktop_navigation import (
    WebNavigationCandidate,
    catalog_navigation_url_matches,
)
from services.api.app.application.web_exploration_budget_ledger import (
    WebExplorationBudgetLedgerPort,
)
from services.api.app.application.web_exploration_workflow import (
    RunWebWorkflowCommand,
    WebCaptureStage,
    WebSurfaceBootstrapper,
)
from services.api.app.application.web_interaction_facts import (
    web_capture_content_facts,
    web_command_accepted,
    web_incomplete_interaction,
    web_receipt_allows_arrival,
)
from services.api.app.application.web_interaction_precision import (
    WebInteractionPlan,
    WebInteractionPostcondition,
    assess_web_interaction_outcome,
)
from services.api.app.application.web_menu_exploration import (
    WebMenuExplorationSummary,
    WebMenuExplorer,
    WebMenuVisitResult,
)
from services.api.app.application.web_navigation_safety import (
    WebCaptureUrlReader,
    WebNavigationSafetyError,
    validate_web_final_url,
)
from services.api.app.domain.exploration import WebMenuTerminalStatus
from services.api.app.domain.interaction import (
    ActionIntent,
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)
from services.api.app.domain.page import WebSiteMenuEntry
from services.api.app.domain.page.web_runtime_navigation_context import RuntimeWebNavigationContext
from services.api.app.domain.page.web_url_identity import DEFAULT_WEB_URL_IDENTITY_POLICY


class WebNavigationCandidateSource(Protocol):
    def navigation_candidates(
        self,
        session_id: str,
    ) -> tuple[WebNavigationCandidate, ...]: ...


class WebDesktopMenuPageVisitor:
    """执行白名单同源导航或受控点击并留下 page_probe；后续服务负责就绪与分类。"""

    def __init__(
        self,
        *,
        coordinator: CaptureCoordinator,
        capture_url_reader: WebCaptureUrlReader,
        profile_registry: WebBrowserProfileRegistry | None = None,
    ) -> None:
        self._coordinator = coordinator
        self._capture_url_reader = capture_url_reader
        self._profile_registry = profile_registry or WebBrowserProfileRegistry()

    async def visit(
        self,
        *,
        command: RunWebWorkflowCommand,
        entry: WebSiteMenuEntry,
        candidate: WebNavigationCandidate,
        source_capture_id: str,
        menu_intent_id: str,
    ) -> WebMenuVisitResult:
        if not isinstance(menu_intent_id, str) or not menu_intent_id.strip():
            raise ValueError("menu_intent_id 不能为空")
        if candidate.url is None:
            raise ValueError("WEB_MENU_DIRECT_URL_REQUIRED")
        profile = self._profile_registry.resolve(command.browser_profile_id)
        from services.api.app.application.web_runtime_discovery import WebRuntimeMenuEntry

        discovered = isinstance(entry, WebRuntimeMenuEntry)
        direct_navigation_url = DEFAULT_WEB_URL_IDENTITY_POLICY.canonical(candidate.url)
        action_parameters = {
            "clear_obstructions": False,
            "full_page": False,
            "web_capture_stage": WebCaptureStage.PAGE_PROBE.value,
            "web_menu_entry_id": entry.entry_id,
            **profile.to_action_parameters(),
        }
        if discovered:
            action_parameters.update(
                {
                    "target_mode": "web_discovery_candidate",
                    "navigation_depth": candidate.menu_depth,
                    "web_discovery_source_capture_id": source_capture_id,
                    "web_discovery_candidate": candidate.to_dict(),
                    "web_discovery_origin": command.target_url,
                }
            )
            action_parameters["destination_url"] = direct_navigation_url
        else:
            action_parameters["destination_url"] = direct_navigation_url
        intent = ActionIntent(
            task_id=command.task_id,
            session_id=command.session_id,
            intent_id=menu_intent_id,
            idempotency_key=menu_intent_id,
            surface_kind=SurfaceKind.WEB,
            source_capture_id=source_capture_id,
            action_type=InteractionOperation.NAVIGATE,
            semantic_target=" / ".join(entry.menu_path),
            purpose=f"探索已核准 Web 菜单 {entry.entry_id}",
            capture_profile=WebSurfaceBootstrapper.capture_profile(),
            deadline=command.deadline,
            parameters=action_parameters,
            risk_level=InteractionRiskLevel.LOW,
        )
        before_url = await self._capture_url_reader.read_url(capture_id=source_capture_id)
        before_content_identity = None
        before_snapshot = None
        after_snapshot = None
        read_snapshot = getattr(self._capture_url_reader, "read_snapshot", None)
        if callable(read_snapshot):
            before_snapshot = await read_snapshot(capture_id=source_capture_id)
            before_content_identity = web_capture_content_facts(
                before_snapshot,
                url=before_url,
            ).content_identity
        plan = WebInteractionPlan(
            intent=intent,
            source_capture_id=source_capture_id,
            source_epoch=str(candidate.source_epoch)
            if candidate.source_epoch is not None
            else source_capture_id,
            target_ref=entry.entry_id,
            max_scroll_steps=0,
            deadline_seconds=max(
                0.001,
                min(
                    300.0,
                    (command.deadline - datetime.now(command.deadline.tzinfo)).total_seconds(),
                ),
            ),
            postcondition=WebInteractionPostcondition.MENU_ARRIVAL,
        )
        try:
            coordination = await self._coordinator.coordinate(
                CaptureCoordinationCommand(
                    agent_run_id=command.agent_run_id,
                    intent=intent,
                )
            )
        except CaptureCoordinationError as exc:
            return WebMenuVisitResult(
                final_capture_id=(str(getattr(exc.after_capture, "capture_id", "") or "") or None),
                page_type=None,
                status=WebMenuTerminalStatus.NAVIGATION_FAILED,
                error_code=exc.error_code,
                menu_action_count=1,
                menu_intent_id=menu_intent_id,
                menu_action_execution_id=_action_execution_id(exc.interaction),
                menu_interaction_outcome=web_incomplete_interaction(
                    exc.interaction,
                    str(getattr(exc.after_capture, "capture_id", "") or "") or None,
                    exc.error_code,
                ),
                diagnostic_type=type(exc).__name__,
                diagnostic_message=str(exc),
            )
        capture_id = str(
            getattr(getattr(coordination, "after_capture", None), "capture_id", "") or ""
        )
        action_execution_id = _action_execution_id(getattr(coordination, "interaction", None))
        incomplete_outcome = web_incomplete_interaction(
            getattr(coordination, "interaction", None),
            capture_id or None,
            "after_observation_pending",
        )
        if not capture_id:
            return WebMenuVisitResult(
                final_capture_id=None,
                page_type=None,
                status=WebMenuTerminalStatus.NAVIGATION_FAILED,
                error_code="WEB_EVIDENCE_INCOMPLETE",
                menu_interaction_outcome=incomplete_outcome,
                menu_action_count=1,
                menu_intent_id=menu_intent_id,
                menu_action_execution_id=action_execution_id,
            )
        try:
            final_url = await self._capture_url_reader.read_url(capture_id=capture_id)
            # URL 已观察到的效果先保留；后续快照读取失败不能抹掉该事实。
            incomplete_outcome = assess_web_interaction_outcome(
                plan=plan,
                command_accepted=incomplete_outcome.command_accepted,
                before_url=before_url,
                after_url=final_url,
                before_content_identity=before_content_identity,
                after_content_identity=None,
                postcondition_satisfied=False,
                after_capture_id=capture_id,
                readiness_accepted=False,
                identity_verified=False,
                business_layer_visible=False,
            )
            after_content_identity = None
            if callable(read_snapshot):
                after_snapshot = await read_snapshot(capture_id=capture_id)
                after_content_identity = web_capture_content_facts(
                    after_snapshot, url=final_url
                ).content_identity
                if (
                    before_content_identity
                    and after_content_identity
                    and (before_content_identity != after_content_identity)
                ):
                    incomplete_outcome = replace(
                        incomplete_outcome,
                        effect_observed=True,
                        failure_reason="postcondition_not_satisfied"
                        if incomplete_outcome.command_accepted
                        else "command_not_accepted",
                    )
        except Exception as exc:  # noqa: BLE001 - 动作已完成，失败结果必须保留血缘
            return WebMenuVisitResult(
                final_capture_id=capture_id,
                page_type=None,
                status=WebMenuTerminalStatus.NAVIGATION_FAILED,
                error_code="WEB_MENU_POST_NAVIGATION_FAILED",
                menu_interaction_outcome=incomplete_outcome,
                menu_action_count=1,
                menu_intent_id=menu_intent_id,
                menu_action_execution_id=action_execution_id,
                diagnostic_type=type(exc).__name__,
                diagnostic_message="菜单动作后 URL 或快照读取失败",
            )
        try:
            validate_web_final_url(
                site_url=command.target_url,
                requested_url=direct_navigation_url,
                final_url=final_url,
            )
        except WebNavigationSafetyError as exc:
            recovered_capture_id = await self._recover_from_unsafe_navigation(
                command=command,
                unsafe_capture_id=capture_id,
                recovery_url=before_url,
                menu_intent_id=menu_intent_id,
                action_parameters=profile.to_action_parameters(),
            )
            return WebMenuVisitResult(
                final_capture_id=capture_id,
                page_type=None,
                status=WebMenuTerminalStatus.UNSAFE_NAVIGATION,
                error_code=exc.error_code,
                menu_interaction_outcome=incomplete_outcome,
                menu_action_count=1,
                menu_intent_id=menu_intent_id,
                menu_action_execution_id=action_execution_id,
                navigation_current_capture_id=recovered_capture_id,
                navigation_handoff_error=(
                    None
                    if recovered_capture_id is not None
                    else "WEB_NAVIGATION_CURRENT_UNTRUSTED"
                ),
            )
        return WebMenuVisitResult(
            final_capture_id=capture_id,
            page_type=None,
            status=WebMenuTerminalStatus.MENU_PAGE_CAPTURED,
            menu_action_count=1,
            menu_intent_id=menu_intent_id,
            menu_action_execution_id=action_execution_id,
            menu_interaction_plan=plan,
            menu_before_url=before_url,
            menu_observed_target_url=final_url if discovered else None,
            menu_before_content_identity=before_content_identity,
            menu_receipt_verified=web_receipt_allows_arrival(
                getattr(coordination, "interaction", None)
            ),
            menu_interaction_outcome=assess_web_interaction_outcome(
                plan=plan,
                command_accepted=web_command_accepted(getattr(coordination, "interaction", None)),
                before_url=before_url,
                after_url=final_url,
                before_content_identity=before_content_identity,
                after_content_identity=after_content_identity,
                postcondition_satisfied=catalog_navigation_url_matches(
                    direct_navigation_url, final_url
                ),
                after_capture_id=capture_id,
                readiness_accepted=False,
                identity_verified=False,
                business_layer_visible=False,
            ),
        )

    async def _recover_from_unsafe_navigation(
        self,
        *,
        command: RunWebWorkflowCommand,
        unsafe_capture_id: str,
        recovery_url: str,
        menu_intent_id: str,
        action_parameters: dict[str, object],
    ) -> str | None:
        """从真实的危险落地页回退，只有同源 URL 校验通过才交还当前页。"""

        recovery_intent_id = f"{menu_intent_id}:safety-recovery"
        recovery_intent = ActionIntent(
            task_id=command.task_id,
            session_id=command.session_id,
            intent_id=recovery_intent_id,
            idempotency_key=recovery_intent_id,
            surface_kind=SurfaceKind.WEB,
            source_capture_id=unsafe_capture_id,
            action_type=InteractionOperation.NAVIGATE,
            semantic_target="跨域跳转前的可信页面",
            purpose="恢复 Web 菜单探索的同源可信上下文",
            capture_profile=WebSurfaceBootstrapper.capture_profile(),
            deadline=command.deadline,
            parameters={
                "clear_obstructions": False,
                "full_page": False,
                "web_capture_stage": WebCaptureStage.PAGE_PROBE.value,
                "destination_url": recovery_url,
                **action_parameters,
            },
            risk_level=InteractionRiskLevel.LOW,
        )
        destinations = [recovery_url]
        recovered_capture_id = unsafe_capture_id
        if not catalog_navigation_url_matches(recovery_url, command.target_url):
            destinations.append(command.target_url)
        for ordinal, destination in enumerate(destinations):
            if ordinal:
                # 上一页本身也可能是动态跳转页；只从已读取的真实后帧再恢复一次授权入口。
                fallback_id = f"{recovery_intent_id}:entry"
                recovery_intent = replace(
                    recovery_intent,
                    intent_id=fallback_id,
                    idempotency_key=fallback_id,
                    source_capture_id=recovered_capture_id,
                    semantic_target="本次探索已授权入口",
                    parameters={**recovery_intent.parameters, "destination_url": destination},
                )
            try:
                coordination = await self._coordinator.coordinate(
                    CaptureCoordinationCommand(
                        agent_run_id=command.agent_run_id,
                        intent=recovery_intent,
                    )
                )
                recovered_capture_id = str(
                    getattr(getattr(coordination, "after_capture", None), "capture_id", "") or ""
                )
                if not recovered_capture_id:
                    return None
                recovered_url = await _read_recovery_url(
                    self._capture_url_reader,
                    capture_id=recovered_capture_id,
                )
            except Exception:  # noqa: BLE001 - 未取得可信后帧时不能再派发恢复动作。
                return None
            try:
                validate_web_final_url(
                    site_url=command.target_url,
                    requested_url=destination,
                    final_url=recovered_url,
                )
            except WebNavigationSafetyError:
                continue
            if catalog_navigation_url_matches(destination, recovered_url):
                return recovered_capture_id
        return None


async def _read_recovery_url(
    reader: WebCaptureUrlReader,
    *,
    capture_id: str,
) -> str:
    """容忍恢复 Capture 刚提交后的短暂证据索引不可见，其他错误保持失败关闭。"""

    for attempt, retry_delay_seconds in enumerate((0.05, 0.15, 0.0), start=1):
        try:
            return await reader.read_url(capture_id=capture_id)
        except RuntimeError as exc:
            if str(exc) != "WEB_CAPTURE_EVIDENCE_MISSING" or attempt == 3:
                raise
            await asyncio.sleep(retry_delay_seconds)
    raise AssertionError("恢复 URL 重读循环未返回")


def _action_execution_id(interaction: object | None) -> str | None:
    value = str(getattr(interaction, "action_execution_id", "") or "").strip()
    return value or None


class WebDesktopNavigationExplorer:
    """兼容既有组合根名称，内部执行 Catalog 驱动的有界菜单探索。"""

    def __init__(
        self,
        *,
        coordinator: CaptureCoordinator,
        candidate_source: WebNavigationCandidateSource,
        capture_url_reader: WebCaptureUrlReader,
        profile_registry: WebBrowserProfileRegistry | None = None,
        navigation_context: RuntimeWebNavigationContext,
        budget_ledger: WebExplorationBudgetLedgerPort,
    ) -> None:
        visitor = _PersistableProbeOnlyMenuPageVisitor(
            visitor=WebDesktopMenuPageVisitor(
                coordinator=coordinator,
                capture_url_reader=capture_url_reader,
                profile_registry=profile_registry,
            )
        )
        self._explorer = WebMenuExplorer(
            navigation_context=navigation_context,
            candidate_source=candidate_source,
            page_visitor=visitor,
            budget_ledger=budget_ledger,
        )

    async def explore(
        self,
        command: RunWebWorkflowCommand,
        *,
        initial_capture_id: str,
    ) -> WebMenuExplorationSummary:
        return await self._explorer.explore(
            command,
            initial_capture_id=initial_capture_id,
        )


class _PersistableProbeOnlyMenuPageVisitor:
    """禁止仅有 probe 的兼容入口冒充已完成菜单流水线。"""

    def __init__(self, *, visitor: WebDesktopMenuPageVisitor) -> None:
        self._visitor = visitor

    async def visit(self, **kwargs: object) -> WebMenuVisitResult:
        result = await self._visitor.visit(**kwargs)
        if result.final_capture_id and not result.menu_probe_capture_id:
            result = replace(result, menu_probe_capture_id=result.final_capture_id)
        if result.status is not WebMenuTerminalStatus.MENU_PAGE_CAPTURED:
            return result
        return replace(
            result,
            page_type=None,
            status=WebMenuTerminalStatus.NAVIGATION_FAILED,
            error_code="WEB_MENU_PIPELINE_INCOMPLETE",
            menu_ready_capture_id=None,
            menu_page_type=None,
            diagnostic_type="ProbeOnlyCompatibilityVisitor",
            diagnostic_message="兼容入口仅完成菜单 probe，未伪造 ready 或页面分类",
        )


WebDesktopExplorationSummary = WebMenuExplorationSummary


__all__ = [
    "WebDesktopExplorationSummary",
    "WebDesktopMenuPageVisitor",
    "WebDesktopNavigationExplorer",
    "WebNavigationCandidateSource",
]
