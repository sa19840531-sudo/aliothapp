"""Web 阶段与现有截图、分类、会话服务的窄适配边界。"""

from __future__ import annotations

import logging
from dataclasses import is_dataclass, replace
from datetime import UTC, datetime

from services.api.app.application.capture_coordination import CaptureCoordinationError
from services.api.app.application.web_age_gate import WebAgeGateReviewRequiredError
from services.api.app.application.web_capture_stage_failure import (
    bootstrap_coordination_failure,
    coordination_stage_failure,
)
from services.api.app.application.web_desktop_exploration import WebDesktopExplorationSummary
from services.api.app.application.web_full_page_supplemental_capture import (
    WebFullPageSupplementalCommand,
)
from services.api.app.application.web_home_login_gate import is_web_home_login_gate
from services.api.app.application.web_page_readiness import WEB_READINESS_DIAGNOSTIC_CODES
from services.api.app.application.web_run_stage_orchestrator import (
    WebStageEffectResult,
    WebStageFailure,
)
from services.api.app.application.web_runtime_stage_ads import WebRuntimeStageAds
from services.api.app.application.web_runtime_stage_auxiliary import WebRuntimeStageAuxiliary
from services.api.app.application.web_runtime_stage_collection import WebRuntimeStageCollection
from services.api.app.application.web_runtime_stage_discovery import WebRuntimeStageDiscovery
from services.api.app.application.web_runtime_stage_login import (
    run_web_login_branch,
    web_login_cursor_detail,
)
from services.api.app.application.web_runtime_stage_primary import WebRuntimeStagePrimary
from services.api.app.application.web_stage_classification_reuse import (
    read_reused_classification,
    validate_reused_classification,
)
from services.api.app.domain.exploration.web_run_stage_checkpoint import WebRunStage, WebStageCursor

_LOGGER = logging.getLogger(__name__)
_SESSION_CLOSE_DEFERRED = "deferred_to_formal_reconciliation"


def _readiness_stage_failure(result) -> tuple[str, bool]:
    """保留 readiness 真实终态；仅可能通过重采恢复的状态进入阶段重试。"""

    status = str(getattr(result, "status", "capture_failed"))
    reasons = tuple(getattr(result, "blocking_reasons", ()) or ())
    if "blank_document" in reasons:
        return "WEB_READINESS_BLANK_DOCUMENT", True
    mapping = {
        "timeout": ("WEB_READINESS_TIMEOUT", True),
        "loading": ("WEB_READINESS_LOADING", True),
        "transitional": ("WEB_READINESS_TRANSITIONAL", True),
        "capture_failed": ("WEB_READINESS_CAPTURE_FAILED", True),
        "identity_drift": ("WEB_READINESS_IDENTITY_DRIFT", True),
        "artifact_skew": ("WEB_READINESS_ARTIFACT_SKEW", True),
        "popup_blocked": ("WEB_READINESS_POPUP_BLOCKED", False),
        "human_verification_blocked": (
            "WEB_READINESS_HUMAN_VERIFICATION_BLOCKED",
            False,
        ),
        "degraded": ("WEB_READINESS_DEGRADED", False),
    }
    return mapping.get(status, ("WEB_READINESS_CAPTURE_FAILED", True))


class WebRuntimeStageEffects:
    def __init__(
        self,
        *,
        runtime,
        evidence,
        command,
        adult_self_attestation=None,
        frontier=None,
        checkpoints=None,
    ):
        self._runtime = runtime
        self._evidence = evidence
        self._command = command
        self._adult_self_attestation = adult_self_attestation
        self._frontier = frontier
        self._checkpoints = checkpoints
        self._ads = WebRuntimeStageAds(runtime=runtime, command=command, evidence=evidence)
        self._collection = WebRuntimeStageCollection(
            runtime=runtime, command=command, evidence=evidence, checkpoints=checkpoints,
            ads=self._ads,
        )
        self._auxiliary = WebRuntimeStageAuxiliary(
            runtime=runtime, command=command, evidence=evidence, checkpoints=checkpoints,
            ads=self._ads, collection=self._collection,
        )
        self._discovery = WebRuntimeStageDiscovery(
            command=command,
            evidence=evidence,
            frontier=frontier,
            checkpoints=checkpoints,
            ads=self._ads,
        )
        self._primary = WebRuntimeStagePrimary(
            runtime=runtime,
            evidence=evidence,
            command=command,
            before_detail=self._discovery.before_detail,
        )

    async def initialize(self):
        epoch = await self._evidence.read_epoch()
        await self._checkpoints.create(
            WebStageCursor.start(
                run_id=self._command.agent_run_id,
                logical_page_key="home",
                cursor_payload={
                    "session_id": self._command.session_id,
                    "finalization_epoch": epoch,
                    "current_url": self._command.target_url,
                },
            )
        )

    async def final_result(self, run_id):
        if run_id != self._command.agent_run_id:
            raise ValueError("阶段结果 Run 归属不一致")
        guide = getattr(self._runtime, "web_guide_dispatcher", None)
        if guide is not None:
            try:
                await guide.finish()
            except Exception:  # noqa: BLE001 - 可选引导支线不得改变主线终态
                _LOGGER.warning("web_guide_finish_failed run_id=%s", run_id)
        result = await self._read_summary()
        read_captures = getattr(self._evidence, "report_captures", None)
        if read_captures is not None and is_dataclass(result):
            captures = await read_captures()
            result = replace(
                result,
                page_timeline=tuple(
                    dict.fromkeys((*result.page_timeline, *(item[0] for item in captures)))
                ),
                page_type_keys=tuple(
                    dict.fromkeys(
                        (*result.page_type_keys, *(item[1] for item in captures if item[1]))
                    )
                ),
            )
        return result

    async def _read_summary(self):
        reader = getattr(self._frontier, "has_budget", None)
        if reader is not None and not await reader(self._command):
            return WebDesktopExplorationSummary(page_timeline=(), page_type_keys=())
        return await self._runtime.read_persisted_exploration(self._command)

    async def find_completed(self, cursor):
        await self._evidence.assert_active(cursor)
        saved = await self._evidence.find_completed(cursor)
        if saved is not None:
            if cursor.current_stage is WebRunStage.CLASSIFICATION:
                if (
                    saved.cursor_payload.get("capture_id")
                    != cursor.cursor_payload.get("capture_id")
                ):
                    raise WebStageFailure("WEB_CLASSIFICATION_CAPTURE_MISMATCH")
                if saved.reused_capture_id is not None:
                    await validate_reused_classification(self._evidence, cursor, saved)
                elif not await self._evidence.has_durable_page_type_assessment(
                        capture_id=saved.cursor_payload["capture_id"],
                        finalization_epoch=cursor.cursor_payload["finalization_epoch"],
                ):
                    return None
            if (
                cursor.current_stage is WebRunStage.BOOTSTRAP_NAVIGATION
                and self._collection.owns(cursor)
            ):
                self._collection.validate_restored_navigation(cursor, saved)
            if (
                cursor.current_stage is WebRunStage.BOOTSTRAP_NAVIGATION
                and "menu_ledger_id" in cursor.cursor_payload
                and saved.cursor_payload.get("capture_id")
            ):
                # 旧完成标记中的 Capture 仍是原导航结果，恢复时补齐独立血缘。
                saved = replace(saved, cursor_payload={
                    **saved.cursor_payload,
                    "menu_navigation_capture_id": saved.cursor_payload["capture_id"],
                })
            if (
                cursor.current_stage is WebRunStage.SESSION_CLOSE
                and cursor.logical_page_key == "home"
            ):
                if getattr(self._runtime, "defer_session_close_to_reconciliation", False):
                    self._log_session_close_deferred()
            return saved
        if cursor.current_stage is WebRunStage.BOOTSTRAP_NAVIGATION:
            read_navigation = getattr(self._evidence, "find_navigation", None)
            if read_navigation is not None:
                restored = await read_navigation(cursor)
                if restored is not None and self._auxiliary.owns(cursor):
                    self._auxiliary.validate_restored_navigation(cursor, restored)
                if restored is not None and self._collection.owns(cursor):
                    self._collection.validate_restored_navigation(cursor, restored)
                return restored
        if cursor.current_stage is WebRunStage.FULL_PAGE_CAPTURE:
            service = self._runtime.pre_gate_supplemental_capture
            if service is not None:
                completed = await service.read_completed(self._full_page_command(cursor))
                refresh = getattr(
                    getattr(self._runtime, "page_readiness", None),
                    "refresh_ready_baseline",
                    None,
                )
                if completed is not None and not callable(refresh):
                    return WebStageEffectResult(
                        artifact_ids=completed.screenshot_artifact_ids,
                        cursor_payload={"capture_id": completed.source_capture_id},
                    )
        if cursor.current_stage is WebRunStage.SESSION_CLOSE:
            if await self._evidence.session_closed(session_id=self._command.session_id):
                return WebStageEffectResult()
        return None

    def _log_session_close_deferred(self) -> None:
        """记录关闭责任交接，不把浏览器状态伪装成已关闭。"""

        _LOGGER.info(
            "web_session_close_status task_id=%s agent_run_id=%s session_id=%s status=%s",
            self._command.task_id,
            self._command.agent_run_id,
            self._command.session_id,
            _SESSION_CLOSE_DEFERRED,
            extra={
                "task_id": self._command.task_id,
                "agent_run_id": self._command.agent_run_id,
                "session_id": self._command.session_id,
                "session_close_status": _SESSION_CLOSE_DEFERRED,
            },
        )

    async def _classify_ready_capture(self, cursor):
        """仅在原帧没有持久评估时，读取仍为 current 的 ready Capture。"""
        try:
            ready = await self._runtime.stage_ready_capture_reader.read(
                capture_id=cursor.cursor_payload["capture_id"],
                page_role="home" if cursor.logical_page_key == "home" else "menu",
            )
        except RuntimeError as exc:
            if str(exc) == "WEB_CAPTURE_IDENTITY_MISMATCH":
                raise WebStageFailure("WEB_CLASSIFICATION_READY_CAPTURE_UNAVAILABLE") from exc
            raise
        if cursor.artifact_ids:
            ready = replace(
                ready,
                supplemental_screenshot_artifact_ids=cursor.artifact_ids,
                supplemental_evidence_status="captured",
                supplemental_evidence_reason_code=None,
            )
        elif full_page_gaps := [
            gap for gap in cursor.gaps if gap.stage is WebRunStage.FULL_PAGE_CAPTURE
        ]:
            ready = replace(
                ready,
                supplemental_screenshot_artifact_ids=(),
                supplemental_evidence_status="failed",
                supplemental_evidence_reason_code=full_page_gaps[-1].error_code,
            )
        assessment = await self._runtime.page_type_classifier.classify(ready)
        page_type = getattr(assessment, "page_type", None)
        reused = getattr(assessment, "reused_capture_id", None)
        classified = WebStageEffectResult(
            cursor_payload={"capture_id": ready.capture_id},
            reused_capture_id=reused if reused != ready.capture_id else None,
            reused_logical_page_id=(
                getattr(assessment, "reused_logical_page_id", None)
                if reused and reused != ready.capture_id else None
            ),
        )
        if classified.reused_capture_id is not None:
            facts = await validate_reused_classification(self._evidence, cursor, classified)
            page_type = facts["page_type"]
        elif not await self._evidence.has_durable_page_type_assessment(
            capture_id=ready.capture_id,
            finalization_epoch=cursor.cursor_payload["finalization_epoch"],
        ):
            raise WebStageFailure("WEB_PAGE_TYPE_ASSESSMENT_MISSING", transient=True)
        await self._evidence.save_completed(cursor, classified, operation="classification")
        return classified, page_type, ready

    async def execute(self, cursor):
        await self._evidence.assert_active(cursor)
        cleanup = cursor.current_stage in {
            WebRunStage.FRONTIER_DRAIN,
            WebRunStage.EVIDENCE_RECONCILIATION,
            WebRunStage.COVERAGE_SNAPSHOT,
            WebRunStage.REPORT_FINALIZATION,
            WebRunStage.SESSION_CLOSE,
            WebRunStage.AD_CLOSEOUT,
        }
        if not cleanup and self._deadline_expired():
            raise WebStageFailure("WEB_EXPLORATION_DEADLINE_EXHAUSTED")
        try:
            return await self._execute(cursor)
        except CaptureCoordinationError as exc:
            reference = exc.after_capture or exc.before_capture
            detail = (
                await self._evidence.capture_reference(capture_id=reference.capture_id)
                if reference is not None
                else {}
            )
            failure = coordination_stage_failure(
                exc, reference=detail, stage=cursor.current_stage.value
            )
            if not cleanup and self._deadline_expired():
                failure = WebStageFailure(
                    "WEB_EXPLORATION_DEADLINE_EXHAUSTED", detail=failure.detail
                )
            raise failure from exc
        except (WebStageFailure, TimeoutError, ConnectionError) as exc:
            if not cleanup and self._deadline_expired():
                raise WebStageFailure(
                    "WEB_EXPLORATION_DEADLINE_EXHAUSTED",
                    detail=exc.detail if isinstance(exc, WebStageFailure) else {},
                ) from exc
            raise

    def _deadline_expired(self):
        return datetime.now(UTC) >= self._command.deadline

    async def _execute(self, cursor):
        if (
            self._collection.owns(cursor)
            and cursor.current_stage is not WebRunStage.OBSTRUCTION_CLEAR
        ):
            outcome = await self._collection.execute(cursor)
            await self._evidence.save_completed(cursor, outcome)
            return outcome
        if self._auxiliary.owns(cursor):
            outcome = await self._auxiliary.execute(cursor)
            await self._evidence.save_completed(cursor, outcome)
            return outcome
        stage = cursor.current_stage
        if stage is WebRunStage.BOOTSTRAP_NAVIGATION:
            if "menu_ledger_id" in cursor.cursor_payload:
                candidate = await self._frontier.get_candidate(
                    self._command, entry_id=cursor.cursor_payload["menu_ledger_id"]
                )
                result = await self._frontier.navigate(
                    self._command,
                    candidate=candidate,
                    source_capture_id=cursor.cursor_payload["capture_id"],
                )
                capture_id = result.final_capture_id
            else:
                try:
                    result = await self._runtime.start(self._command)
                except CaptureCoordinationError as exc:
                    reference = exc.after_capture or exc.before_capture
                    detail = {}
                    if reference is not None:
                        detail = await self._evidence.capture_reference(
                            capture_id=reference.capture_id
                        )
                    raise bootstrap_coordination_failure(exc, reference=detail) from exc
                capture_id = getattr(getattr(result, "after_capture", None), "capture_id", None)
            if not capture_id:
                raise WebStageFailure("WEB_PRIMARY_CAPTURE_FAILED", transient=True)
            outcome = WebStageEffectResult(
                cursor_payload={
                    **await self._evidence.capture_reference(capture_id=capture_id),
                    **({"menu_navigation_capture_id": capture_id}
                       if "menu_ledger_id" in cursor.cursor_payload else {}),
                }
            )
        elif stage is WebRunStage.OBSTRUCTION_CLEAR:
            capture_id = cursor.cursor_payload["capture_id"]
            evidence = await self._runtime.age_evidence_reader.read(capture_id=capture_id)
            try:
                result = await self._runtime.age_gate_handler.handle(
                    evidence, adult_self_attestation=self._adult_self_attestation
                )
            except WebAgeGateReviewRequiredError:
                raise WebStageFailure("WEB_OBSTRUCTION_REVIEW_REQUIRED") from None
            current_capture_id = result.after_capture_id or capture_id
            outcome = WebStageEffectResult(
                cursor_payload=await self._evidence.capture_reference(
                    capture_id=current_capture_id
                )
            )
        elif stage is WebRunStage.READINESS:
            result = await self._runtime.page_readiness.prepare(
                source_capture_id=cursor.cursor_payload["capture_id"],
                page_role="home" if cursor.logical_page_key == "home" else "menu",
            )
            if result.status != "stable" or not result.ready_capture_id:
                code, transient = _readiness_stage_failure(result)
                diagnostic = next((
                    reason for reason in getattr(result, "blocking_reasons", ())
                    if reason in WEB_READINESS_DIAGNOSTIC_CODES
                ), None)
                # prepare 可在失败前完成观察换帧；只从当前会话仓储取得恢复引用。
                reference = await self._evidence.current_capture_reference()
                raise WebStageFailure(
                    code, transient=transient,
                    detail={
                        **reference,
                        **({"raw_error_code": diagnostic} if diagnostic else {}),
                    },
                )
            outcome = WebStageEffectResult(
                cursor_payload=await self._evidence.capture_reference(
                    capture_id=result.ready_capture_id
                )
            )
            login_branch = getattr(self._runtime, "web_login_branch", None)
            outcome = WebStageEffectResult(
                cursor_payload={
                    **outcome.cursor_payload,
                    "parent_capture_id": cursor.cursor_payload["capture_id"],
                    **({"login_source_capture_id": result.ready_capture_id}
                       if cursor.logical_page_key == "home" and login_branch is not None
                       else {}),
                }
            )
        elif stage is WebRunStage.PRIMARY_CAPTURE:
            ready = await self._runtime.stage_ready_capture_reader.read(
                capture_id=cursor.cursor_payload["capture_id"],
                page_role="home" if cursor.logical_page_key == "home" else "menu",
            )
            outcome = WebStageEffectResult(cursor_payload={"capture_id": ready.capture_id})
        elif stage is WebRunStage.FULL_PAGE_CAPTURE:
            service = self._runtime.pre_gate_supplemental_capture
            if service is None:
                outcome = WebStageEffectResult()
            else:
                result = await service.capture(self._full_page_command(cursor))
                if result.status != "captured":
                    raise WebStageFailure(
                        result.reason_code,
                        transient=result.reason_code
                        in {
                            "WEB_FULL_PAGE_SCREENSHOT_TIMEOUT",
                            "WEB_FULL_PAGE_SCREENSHOT_PERSIST_FAILED",
                        },
                    )
                refresh = getattr(
                    getattr(self._runtime, "page_readiness", None),
                    "refresh_ready_baseline",
                    None,
                )
                cursor_payload = {}
                if callable(refresh):
                    refreshed_capture_id = await refresh(
                        source_capture_id=result.source_capture_id,
                        page_role=("home" if cursor.logical_page_key == "home" else "menu"),
                    )
                    cursor_payload = await self._evidence.capture_reference(
                        capture_id=refreshed_capture_id
                    )
                outcome = WebStageEffectResult(
                    artifact_ids=result.screenshot_artifact_ids,
                    cursor_payload=cursor_payload,
                )
        elif stage is WebRunStage.CLASSIFICATION:
            ready = None
            classified = await self._evidence.find_completed(cursor, operation="classification")
            if (
                classified is not None
                and classified.cursor_payload.get("capture_id")
                != cursor.cursor_payload.get("capture_id")
            ):
                raise WebStageFailure("WEB_CLASSIFICATION_CAPTURE_MISMATCH")
            reused_facts = await validate_reused_classification(self._evidence, cursor, classified)
            if (
                classified is not None
                and reused_facts is None
                and not await self._evidence.has_durable_page_type_assessment(
                    capture_id=classified.cursor_payload["capture_id"],
                    finalization_epoch=cursor.cursor_payload["finalization_epoch"],
                )
            ):
                classified = None
            if classified is None:
                persisted = await self._evidence.read_durable_page_type_assessment(
                    capture_id=cursor.cursor_payload["capture_id"],
                    finalization_epoch=cursor.cursor_payload["finalization_epoch"],
                )
                if persisted is not None:
                    page_type = persisted["page_type"]
                    classified = WebStageEffectResult(
                        cursor_payload={"capture_id": cursor.cursor_payload["capture_id"]}
                    )
                    await self._evidence.save_completed(
                        cursor, classified, operation="classification"
                    )
                else:
                    classified, page_type, ready = await self._classify_ready_capture(cursor)
            else:
                facts = await self._evidence.capture_outcome(
                    capture_id=cursor.cursor_payload["capture_id"]
                )
                page_type = facts["page_type"]
            outcome = classified
            if classified.reused_capture_id is not None:
                # 已认证复用不重做正文、广告和详情工作；本次动作帧继续留在 marker 中。
                await self._evidence.save_completed(cursor, outcome)
                return outcome
            if self._checkpoints is not None and not self._deadline_expired():
                await self._ads.schedule(
                    replace(
                        cursor,
                        cursor_payload={
                            **cursor.cursor_payload,
                            **classified.cursor_payload,
                        },
                    ),
                    self._checkpoints,
                )
            await self._schedule_guide_page(classified.cursor_payload["capture_id"])
            if (
                self._frontier is not None
                and self._checkpoints is not None
                and not self._deadline_expired()
            ):
                # 顶部导航候选需要 ready Capture 仍为 current 时完成授权；
                # 这里只登记 href，不执行菜单 GOTO，广告分支仍先于下一页。
                if cursor.logical_page_key == "home":
                    if page_type is None:
                        # 兼容仅落盘而不返回评估对象的分类端口，仍以持久事实为准。
                        facts = await self._evidence.capture_outcome(
                            capture_id=classified.cursor_payload["capture_id"]
                        )
                        page_type = facts["page_type"]
                    if page_type != "home":
                        reason = "WEB_HOME_PAGE_TYPE_UNCLASSIFIED"
                        if page_type in {None, "unknown"}:
                            reader = self._runtime.stage_ready_capture_reader
                            read_snapshot = getattr(reader, "read_snapshot", None)
                            if callable(read_snapshot):
                                try:
                                    current_ready = ready or await reader.read(
                                        capture_id=classified.cursor_payload["capture_id"],
                                        page_role="home",
                                    )
                                    snapshot = await read_snapshot(
                                        capture_id=current_ready.capture_id
                                    )
                                    if is_web_home_login_gate(
                                        target_url=self._command.target_url,
                                        ready_url=current_ready.url,
                                        snapshot=snapshot,
                                    ):
                                        reason = "WEB_HOME_LOGIN_GATE"
                                except Exception:  # noqa: BLE001 - 诊断读取失败沿用原始未知终态
                                    _LOGGER.warning(
                                        "web_home_login_gate_diagnostic_failed run_id=%s",
                                        self._command.agent_run_id,
                                    )
                        raise WebStageFailure(reason)
                await self._discovery.before_primary_scroll(cursor)
            if (
                getattr(self._runtime, "primary_content_capture", None) is not None
                and not self._deadline_expired()
            ):
                try:
                    await self._primary.enhance(
                        cursor, page_type=page_type, frontier=self._frontier
                    )
                except WebStageFailure as exc:
                    # 可选正文失败留在阶段证据中，分类与后续 href 仍保持成功。
                    outcome = replace(outcome, optional_primary_error_code=exc.code)
        elif stage is WebRunStage.HREF_DISCOVERY:
            if await read_reused_classification(self._evidence, cursor) is not None:
                outcome = WebStageEffectResult()
                await self._evidence.save_completed(cursor, outcome)
                return outcome
            await self._discovery.execute(cursor)
            await self._collection.schedule(cursor)
            outcome = WebStageEffectResult()
        elif stage is WebRunStage.CANDIDATE_NAVIGATION:
            if await read_reused_classification(self._evidence, cursor) is not None:
                outcome = WebStageEffectResult()
                await self._evidence.save_completed(cursor, outcome)
                return outcome
            detail = await self._primary.advance_detail(cursor, frontier=self._frontier)
            if detail is not None:
                await self._discovery.schedule_detail(cursor, detail)
            outcome = WebStageEffectResult()
        elif stage is WebRunStage.FRONTIER_DRAIN:
            root_blocked = cursor.logical_page_key == "home" and any(
                gap.stage in {
                    WebRunStage.BOOTSTRAP_NAVIGATION,
                    WebRunStage.READINESS,
                    WebRunStage.PRIMARY_CAPTURE,
                    WebRunStage.CLASSIFICATION,
                }
                for gap in cursor.gaps
            )
            # 未获得可信首页只做对账和关闭，不从未知或空白页签发辅助导航。
            if not root_blocked and (
                cursor.logical_page_key == "home"
                or cursor.logical_page_key.startswith("detail:")
            ):
                await self._schedule_next(cursor)
            if (
                cursor.logical_page_key == "home"
                and not root_blocked
                and not self._deadline_expired()
            ):
                if self._auxiliary.enabled:
                    await self._auxiliary.schedule_next(cursor)
                else:
                    await self._primary.auxiliary(cursor)
            outcome = WebStageEffectResult()
        elif stage is WebRunStage.EVIDENCE_RECONCILIATION:
            await self._frontier.reconcile(self._command, cursor)
            if (
                not self._deadline_expired()
                and cursor.logical_page_key != "home"
                and not cursor.logical_page_key.startswith("detail:")
                and not any(
                    item.logical_page_key == "detail:" + cursor.logical_page_key
                    for item in await self._checkpoints.list_for_run(run_id=cursor.run_id)
                )
            ):
                # 无可用详情子页时由父菜单结算后续行；不能只看尝试 marker。
                await self._schedule_next(cursor)
            outcome = WebStageEffectResult()
        elif stage in {WebRunStage.COVERAGE_SNAPSHOT, WebRunStage.REPORT_FINALIZATION}:
            await self._read_summary()
            login_detail = {}
            login_branch = getattr(self._runtime, "web_login_branch", None)
            if (
                stage is WebRunStage.REPORT_FINALIZATION
                and cursor.logical_page_key == "home"
                and login_branch is not None
                and cursor.cursor_payload.get("login_source_capture_id")
            ):
                login = await run_web_login_branch(
                    branch=login_branch,
                    command=self._command,
                    cursor=cursor,
                    adult_self_attestation=self._adult_self_attestation,
                )
                login_detail = web_login_cursor_detail(login, cursor=cursor)
                _LOGGER.info(
                    "web_login_branch status=%s reason=%s run_id=%s",
                    login.status, login.reason_code, self._command.agent_run_id,
                )
            outcome = WebStageEffectResult(cursor_payload=login_detail)
        elif stage is WebRunStage.SESSION_CLOSE:
            if cursor.logical_page_key == "home":
                if getattr(self._runtime, "defer_session_close_to_reconciliation", False):
                    self._log_session_close_deferred()
                    outcome = WebStageEffectResult()
                else:
                    await self._runtime.close(
                        task_id=self._command.task_id, session_id=self._command.session_id
                    )
                    outcome = WebStageEffectResult()
            else:
                outcome = WebStageEffectResult()
        elif stage in {
            WebRunStage.AD_ISOLATED_NAVIGATION,
            WebRunStage.AD_CAPTURE,
            WebRunStage.AD_CLOSEOUT,
        }:
            outcome = await self._ads.execute(cursor)
        else:
            raise RuntimeError("WEB_STAGE_HANDLER_NOT_CONFIGURED")
        await self._evidence.save_completed(cursor, outcome)
        return outcome

    async def _schedule_guide_page(self, capture_id: str) -> None:
        """可信 ready 首屏登记可选入口；读取或调度失败不影响主线。"""
        guide = getattr(self._runtime, "web_guide_dispatcher", None)
        reader = getattr(self._runtime, "ad_snapshot_reader", None)
        if guide is None or reader is None or self._deadline_expired():
            return
        try:
            snapshot = await reader.read_snapshot(capture_id=capture_id)
            source_url = await reader.read_url(capture_id=capture_id)
            await guide.schedule(
                source_capture_id=capture_id, source_url=source_url, snapshot=snapshot,
            )
        except Exception:  # noqa: BLE001 - 引导支线既不污染 Stage gap 也不触发重采
            _LOGGER.warning("web_guide_schedule_failed run_id=%s", self._command.agent_run_id)

    async def _schedule_next(self, cursor):
        # 冻结期限只终止探索，不阻止对账与会话关闭，也不续期。
        if self._deadline_expired():
            return
        existing = await self._checkpoints.list_for_run(run_id=cursor.run_id)
        next_candidate = await self._frontier.next_candidate(
            self._command,
            excluded_entry_ids=tuple(
                item.cursor_payload.get("menu_ledger_id") for item in existing
            ),
        )
        if next_candidate is None:
            return
        entry_id, candidate = next_candidate
        await self._checkpoints.create(
            WebStageCursor.start(
                run_id=cursor.run_id,
                logical_page_key=entry_id,
                cursor_payload={
                    "session_id": self._command.session_id,
                    "finalization_epoch": cursor.cursor_payload["finalization_epoch"],
                    "menu_ledger_id": entry_id,
                    "capture_id": cursor.cursor_payload["capture_id"],
                    "parent_capture_id": cursor.cursor_payload["capture_id"],
                    "current_url": candidate.url,
                    "requested_url": candidate.url,
                },
            )
        )

    def _full_page_command(self, cursor):
        return WebFullPageSupplementalCommand(
            task_id=self._command.task_id,
            agent_run_id=self._command.agent_run_id,
            session_id=self._command.session_id,
            source_capture_id=cursor.cursor_payload["capture_id"],
            source_url=cursor.cursor_payload["current_url"],
            deadline=self._command.deadline,
            stage_cursor=cursor,
        )
