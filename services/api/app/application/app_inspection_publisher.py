"""App Capture 视觉理解后发布 Inspection Work Item，并标注库存事实。"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from services.api.app.agents.runtime.business_middlewares.base import (
    DefaultTaskRuntimeStateProvider,
    TaskRuntimeStateProvider,
)
from services.api.app.agents.tools.rule_tools import (
    _build_execution_plan,
    _candidate_rule_to_dict,
    _is_popup_relevant_rule,
    _rule_matches_required_page_state,
)
from services.api.app.application.aggregate_rule_config import (
    resolve_rule_evidence_input_configuration,
)
from services.api.app.application.app_rule_action_readiness import (
    app_list_interaction_source_capture,
    app_rule_action_requirement,
    assess_app_rule_action_readiness,
)
from services.api.app.application.evidence_presentation import (
    app_presentation_ready_for_rule as _app_presentation_ready_for_rule,
)
from services.api.app.application.evidence_presentation import (
    presentation_from_capture_row,
    rule_accepts_diagnostic_evidence,
    rule_evidence_layer_target,
)
from services.api.app.application.inspection_candidate_publish import (
    prepare_enforced_batch_commands_from_candidate_result,
    publish_enforced_batches_from_candidate_result,
)
from services.api.app.application.inspection_work_items import (
    InspectionWorkItemService,
    PublishInspectionWorkItemCommand,
    build_inspection_rule_contract_hash,
)
from services.api.app.application.rule_auth_gate import (
    AUTH_GATE_AUTO,
    AUTH_REQUIREMENT_AUTHENTICATED,
    resolve_blocked_auth_requirement_from_session_auth,
)
from services.api.app.application.rule_batching import build_rule_execution_batch_key
from services.api.app.application.rule_page_state import (
    AppActionBeforeSceneReader,
    is_startup_ad_skip_rule,
    required_state_before_role,
    requires_completed_app_action_pair,
)
from services.api.app.application.task_query import TaskQueryService
from services.api.app.domain.evidence.presentation import (
    BasePageVisibility,
    BlockingScope,
    PresentationAssessment,
    TopmostLayerKind,
    evaluate_presentation_for_rule,
)
from services.api.app.domain.page.catalog import InvalidPageTypeCodeError
from services.api.app.domain.page.registry import get_default_page_type_registry
from services.api.app.infrastructure.db.app_action_scene_repository import (
    PostgresAppActionBeforeSceneReader,
)
from services.api.app.infrastructure.db.app_rule_action_facts_repository import (
    PostgresAppRuleActionFactsReader,
)
from services.api.app.infrastructure.db.inspection_work_item_repository import (
    PostgresInspectionWorkItemRepository,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool
from services.api.app.infrastructure.db.task_repository import PostgresTaskRepository

logger = logging.getLogger(__name__)


class AppCaptureInspectionAnnotator(Protocol):
    """把候选评估结果写回 Capture metadata，供 Completion Gate 读取。"""

    async def annotate_inspection(
        self,
        *,
        task_id: str,
        capture_id: str,
        page_type: str | None,
        page_state: str,
        inspection_status: str,
        published_count: int,
    ) -> None: ...


class CaptureAgentRunReader(Protocol):
    """读取 Capture 所属 Agent Run。"""

    async def get_agent_run_id(self, *, capture_id: str) -> str | None: ...


class AppAdLandingRuleScopeReader(Protocol):
    """识别 App Capture 是否为 alioth_17 的完整浏览器落地证据帧。"""

    async def has_complete_pair(
        self,
        *,
        task_id: str,
        capture_id: str,
    ) -> bool | None: ...


class CapturePresentationReader(Protocol):
    """读取 Capture 已持久化的展示层事实。"""

    async def get_presentation(
        self,
        *,
        task_id: str,
        capture_id: str,
    ) -> PresentationAssessment | None: ...


@dataclass(frozen=True, kw_only=True)
class AppCaptureInspectionResult:
    """单次 App Capture 质检库存评估结果。"""

    status: str
    published_count: int
    page_type: str | None = None
    page_capture_id: str | None = None
    items: tuple[dict[str, Any], ...] = ()
    message: str | None = None
    excluded_active_session_rule_count: int = 0
    excluded_active_session_rules: tuple[dict[str, Any], ...] = ()
    prepared_commands: tuple[PublishInspectionWorkItemCommand, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "published_count": self.published_count,
            "page_type": self.page_type,
            "page_capture_id": self.page_capture_id,
            "items": list(self.items),
            "message": self.message,
            "excluded_active_session_rule_count": self.excluded_active_session_rule_count,
            "excluded_active_session_rules": list(self.excluded_active_session_rules),
        }


_EXISTING_DIAGNOSTIC_STATUS_SQL = """CASE
    WHEN capture.metadata->>'inspection_status' = 'failed'
      OR capture.metadata #>> '{inspection,status}' = 'failed' THEN 'failed'
    WHEN capture.metadata->>'inspection_status' = 'degraded'
      OR capture.metadata #>> '{inspection,status}' = 'degraded' THEN 'degraded'
    ELSE COALESCE(NULLIF(capture.metadata #>> '{inspection,status}', ''),
                  NULLIF(capture.metadata->>'inspection_status', ''), 'degraded') END"""

# 范围说明只是注解，不能覆盖已存失败或降级的原始诊断。
# 两个正式写入口共用相同的原子表达式，避免 prepare_only 交接语义漂移。
CAPTURE_INSPECTION_METADATA_MERGE_SQL = """CASE
    WHEN $3::jsonb->>'inspection_status' = 'out_of_scope' AND (
        capture.metadata->>'inspection_status' IN ('failed', 'degraded')
        OR capture.metadata #>> '{inspection,status}' IN ('failed', 'degraded')
        OR NULLIF(capture.metadata->>'inspection_failure_stage', '') IS NOT NULL
        OR NULLIF(capture.metadata->>'inspection_failure_code', '') IS NOT NULL
        OR NULLIF(capture.metadata->>'inspection_failure_message', '') IS NOT NULL
        OR NULLIF(capture.metadata->>'inspection_abandon_reason', '') IS NOT NULL
    ) THEN COALESCE(capture.metadata, '{}'::jsonb) || ($3::jsonb - 'inspection')
        || jsonb_build_object(
            'inspection_status', __STATUS__,
            'inspection', ($3::jsonb->'inspection')
                || COALESCE(capture.metadata->'inspection', '{}'::jsonb)
                || jsonb_build_object('status', __STATUS__)
        )
    ELSE COALESCE(capture.metadata, '{}'::jsonb) || $3::jsonb
    END""".replace("__STATUS__", _EXISTING_DIAGNOSTIC_STATUS_SQL)


class PostgresAppCaptureInspectionAnnotator:
    """合并写入 qc_capture.metadata 的 page_type 与 inspection 标注。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def annotate_inspection(
        self,
        *,
        task_id: str,
        capture_id: str,
        page_type: str | None,
        page_state: str,
        inspection_status: str,
        published_count: int,
    ) -> None:
        payload = build_capture_metadata_patch(
            page_type=page_type,
            page_state=page_state,
            inspection_status=inspection_status,
            published_count=published_count,
        )
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection, connection.transaction():
            formal_capture_id = await connection.fetchval(
                """
                UPDATE qc_page_capture
                SET inspection_status = CASE WHEN $4::text = 'out_of_scope' AND (
                        inspection_status IN ('failed', 'degraded')
                        OR NULLIF(inspection_failure_stage, '') IS NOT NULL
                        OR NULLIF(inspection_failure_code, '') IS NOT NULL
                        OR NULLIF(inspection_failure_message, '') IS NOT NULL
                        OR NULLIF(inspection_abandon_reason, '') IS NOT NULL
                    ) THEN inspection_status ELSE $3 END,
                    updated_at = now()
                WHERE id = $1::uuid
                  AND task_id = $2::uuid
                RETURNING id::text
                """,
                capture_id,
                task_id,
                formal_inspection_status(inspection_status),
                inspection_status,
            )
            updated = await connection.fetchrow(
                f"""
                UPDATE qc_capture AS capture
                SET metadata = {CAPTURE_INSPECTION_METADATA_MERGE_SQL}
                FROM qc_task AS task
                WHERE capture.id = $1::uuid
                  AND capture.task_id = $2::uuid
                  AND task.id = capture.task_id
                RETURNING
                    capture.id::text AS capture_id,
                    task.target_platform
                """,
                capture_id,
                task_id,
                json.dumps(payload, ensure_ascii=False),
            )
            if updated is None:
                raise RuntimeError("App Capture 质检标注失败：Capture 不存在或不属于当前任务")
            if (
                str(updated["target_platform"] or "").casefold() in {"h5", "web"}
                and formal_capture_id is None
            ):
                raise RuntimeError("H5 Capture 缺少 qc_page_capture 正式子类型，质检标注已回滚")


_FORMAL_INSPECTION_STATUS_BY_BUSINESS_STATUS = {
    "unmatched_page_type": "degraded",
    "no_rules_applicable": "completed",
    "out_of_scope": "degraded",
    "snapshot_rules_deferred": "evaluating",
    "evidence_rules_deferred": "evaluating",
    "active_session_rules_excluded": "degraded",
    "published": "evaluating",
}


def formal_inspection_status(business_status: str) -> str:
    """把发布业务状态映射为 0011 约束允许的页面级质检生命周期。"""
    try:
        return _FORMAL_INSPECTION_STATUS_BY_BUSINESS_STATUS[business_status]
    except KeyError as exc:
        raise ValueError(f"不支持的质检发布业务状态: {business_status}") from exc


def build_capture_metadata_patch(
    *,
    inspection_status: str,
    page_type: str | None = None,
    page_state: str | None = None,
    published_count: int = 0,
) -> dict[str, Any]:
    """构建 nested + flat 兼容的 Capture 质检元数据补丁。"""
    payload: dict[str, Any] = {
        "inspection": {
            "status": inspection_status,
            "published_count": published_count,
        },
        "inspection_status": inspection_status,
    }
    if page_state is not None:
        payload["page_state"] = page_state
    if inspection_status == "out_of_scope":
        payload["inspection"].update(
            reason_code="inspection_scope_excluded",
            message="当前页面不在本任务所选质检范围内，仅保留探索记录",
        )
    if page_type is not None:
        payload["page_type"] = page_type
        # 报告页覆盖率要求 detection_status=recognized；仅投影 page_type
        # 会导致已识别业务页永久计为 0%。目录已解析的 page_type 同步标记。
        if inspection_status != "unmatched_page_type":
            payload["detection_status"] = "recognized"
    return payload


class PostgresCaptureAgentRunReader:
    """从公共 Capture 读取 producer Agent Run。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def get_agent_run_id(self, *, capture_id: str) -> str | None:
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection:
            row = await connection.fetchval(
                """
                SELECT agent_run_id::text
                FROM qc_capture
                WHERE id = $1::uuid
                """,
                capture_id,
            )
        text = str(row or "").strip()
        return text or None


class PostgresAppAdLandingRuleScopeReader:
    """只从已落库 App action_log 判定广告旅程规则作用域。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def has_complete_pair(
        self,
        *,
        task_id: str,
        capture_id: str,
    ) -> bool | None:
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT surface_kind::text AS surface_kind,
                    capture_role::text AS capture_role,
                    metadata->'action_log' AS action_log
                FROM qc_capture
                WHERE id = $1::uuid
                  AND task_id = $2::uuid
                  AND capture_status = 'captured'
                """,
                capture_id,
                task_id,
            )
        if row is None or str(row["surface_kind"] or "").casefold() not in {
            "android",
            "ios",
        }:
            return None
        if str(row["capture_role"] or "") != "after_action":
            return False
        actions = row["action_log"]
        if isinstance(actions, str):
            try:
                actions = json.loads(actions)
            except json.JSONDecodeError:
                return False
        if not isinstance(actions, list):
            return False
        return any(
            isinstance(action, Mapping)
            and isinstance(action.get("ad_navigation_evidence"), Mapping)
            and action["ad_navigation_evidence"].get("status") == "observed"
            and action["ad_navigation_evidence"].get("pair_complete") is True
            and action["ad_navigation_evidence"].get("navigation_mode") == "external_browser"
            for action in actions
        )


class PostgresCapturePresentationReader:
    """从公共 Capture 与 H5 页面子类型读取统一展示层事实。"""

    def __init__(self, *, database_url: str | None = None, pool: Any | None = None) -> None:
        self._database_url = database_url
        self._pool = pool

    async def get_presentation(
        self,
        *,
        task_id: str,
        capture_id: str,
    ) -> PresentationAssessment | None:
        pool = self._pool or await get_postgres_pool(self._database_url)
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT
                    capture.surface_kind::text AS surface_kind,
                    capture.metadata,
                    page_capture.page_state::text AS page_state,
                    visual.page_state AS visual_page_state,
                    visual.overlay_signature AS visual_overlay_signature,
                    visual.surface_facts AS visual_surface_facts
                FROM qc_capture AS capture
                LEFT JOIN qc_page_capture AS page_capture ON page_capture.id = capture.id
                LEFT JOIN qc_app_capture AS app_capture ON app_capture.capture_id = capture.id
                LEFT JOIN LATERAL (
                    SELECT observation.page_state, observation.overlay_signature,
                           observation.surface_facts
                    FROM qc_app_visual_observation AS observation
                    WHERE observation.capture_id = capture.id
                      AND observation.task_id = capture.task_id
                      AND observation.session_id = capture.surface_session_id
                      AND observation.screenshot_artifact_id = app_capture.screenshot_artifact_id
                    ORDER BY observation.created_at DESC, observation.observation_id DESC
                    LIMIT 1
                ) AS visual ON TRUE
                WHERE capture.id = $1::uuid
                  AND capture.task_id = $2::uuid
                """,
                capture_id,
                task_id,
            )
        if row is None:
            return None
        metadata = row["metadata"]
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        visual_facts = row["visual_surface_facts"]
        if isinstance(visual_facts, str):
            visual_facts = json.loads(visual_facts)
        return presentation_from_capture_row(
            surface_kind=str(row["surface_kind"] or "") or None,
            metadata=metadata if isinstance(metadata, Mapping) else None,
            page_state=str(row["page_state"] or "") or None,
            visual_page_state=row["visual_page_state"],
            visual_overlay_signature=row["visual_overlay_signature"],
            visual_surface_facts=(visual_facts if isinstance(visual_facts, Mapping) else None),
        )


_POPUP_OBSTRUCTION_PAGE_STATES = frozenset(
    {"popup", "modal", "overlay", "dialog"}
)
_POPUP_HOST_PAGE_TYPE_FALLBACKS = (
    "home",
    "video_list",
    "content_list",
    "activity",
    "video_detail",
)
_INVALID_VISUAL_PAGE_TYPE_MARKERS = frozenset(
    {"app", "android", "unknown", "null", "none"}
)


def _is_popup_obstruction_page_state(page_state: str | None) -> bool:
    return str(page_state or "").strip().casefold() in _POPUP_OBSTRUCTION_PAGE_STATES


def _is_unusable_visual_page_type(page_type: str | None) -> bool:
    text = str(page_type or "").strip()
    if not text:
        return True
    return text.casefold() in _INVALID_VISUAL_PAGE_TYPE_MARKERS


def _presentation_indicates_popup_obstruction(
    presentation: PresentationAssessment | None,
) -> bool:
    if presentation is None:
        return False
    if presentation.topmost_layer_kind in {
        TopmostLayerKind.OVERLAY,
        TopmostLayerKind.SYSTEM_DIALOG,
    }:
        return True
    if presentation.base_page_visibility is BasePageVisibility.FULLY_OCCLUDED:
        return True
    return presentation.blocking_scope in {
        BlockingScope.PAGE,
        BlockingScope.APPLICATION,
    }


def _resolve_popup_host_page_type(
    *,
    registry: Any,
    selected_page_types: frozenset[str],
    before_scene_page_type: str | None,
) -> str | None:
    """遮挡帧宿主页型：优先动作前场景，再任务范围内常见宿主，最后 home。"""

    catalog = registry.catalog
    ordered: list[str] = []
    before = str(before_scene_page_type or "").strip()
    if before:
        ordered.append(before)
    scope = selected_page_types or frozenset(_POPUP_HOST_PAGE_TYPE_FALLBACKS)
    for code in _POPUP_HOST_PAGE_TYPE_FALLBACKS:
        if code in scope:
            ordered.append(code)
    if "home" not in ordered:
        ordered.append("home")
    seen: set[str] = set()
    for raw in ordered:
        text = str(raw or "").strip()
        if not text or text.casefold() in seen:
            continue
        seen.add(text.casefold())
        try:
            return catalog.require_code(text).code
        except InvalidPageTypeCodeError:
            alias = registry.resolve_alias(text)
            if alias is not None:
                return alias.canonical_code
    return None


class AppCaptureInspectionPublisher:
    """正式 App 质检：视觉 page_type → 候选规则 → enforced Work Item。"""

    def __init__(
        self,
        *,
        task_query: TaskQueryService | None = None,
        snapshot_provider: TaskRuntimeStateProvider | None = None,
        work_item_service: InspectionWorkItemService | None = None,
        annotator: AppCaptureInspectionAnnotator | None = None,
        agent_run_reader: CaptureAgentRunReader | None = None,
        ad_landing_rule_scope_reader: AppAdLandingRuleScopeReader | None = None,
        presentation_reader: CapturePresentationReader | None = None,
        action_scene_reader: AppActionBeforeSceneReader | None = None,
        action_facts_reader: Any | None = None,
    ) -> None:
        self._task_query = task_query or TaskQueryService(repository=PostgresTaskRepository())
        self._snapshot_provider = snapshot_provider or DefaultTaskRuntimeStateProvider()
        self._work_item_service = work_item_service or InspectionWorkItemService(
            repository=PostgresInspectionWorkItemRepository()
        )
        self._annotator = annotator or PostgresAppCaptureInspectionAnnotator()
        self._agent_run_reader = agent_run_reader or PostgresCaptureAgentRunReader()
        self._ad_landing_rule_scope_reader = ad_landing_rule_scope_reader
        self._presentation_reader = presentation_reader
        self._action_scene_reader = action_scene_reader or PostgresAppActionBeforeSceneReader()
        # 组合根显式注入；单测未注入时不连全局库，避免误读与 Event loop 噪声。
        self._action_facts_reader = action_facts_reader

    async def publish_for_capture(
        self,
        *,
        task_id: str,
        capture_id: str,
        visual_page_type: str | None,
        page_state: str = "stable",
        agent_run_id: str | None = None,
        snapshot_only: bool = False,
        active_session_only: bool = False,
        defer_wakeup: bool = False,
        prepare_only: bool = False,
    ) -> AppCaptureInspectionResult:
        task = str(task_id or "").strip()
        capture = str(capture_id or "").strip()
        state = str(page_state or "stable").strip() or "stable"
        if not task or not capture:
            raise ValueError("App 质检发布缺少 task_id 或 capture_id")
        if not isinstance(snapshot_only, bool):
            raise TypeError("snapshot_only 必须为布尔值")
        if not isinstance(active_session_only, bool):
            raise TypeError("active_session_only 必须为布尔值")
        if snapshot_only and active_session_only:
            raise ValueError("snapshot_only 与 active_session_only 不能同时启用")
        if not isinstance(defer_wakeup, bool):
            raise TypeError("defer_wakeup 必须为布尔值")
        if not isinstance(prepare_only, bool):
            raise TypeError("prepare_only 必须为布尔值")

        run_id = str(agent_run_id or "").strip()
        if not run_id:
            loaded = await self._agent_run_reader.get_agent_run_id(capture_id=capture)
            run_id = str(loaded or "").strip()
        if not run_id:
            raise RuntimeError("App Capture 缺少 agent_run_id，无法发布质检工作项")

        snapshot = await self._snapshot_provider.get_task_snapshot(task)
        if snapshot is None:
            raise RuntimeError("任务锁定规则集快照不存在")
        is_app = str(snapshot.target_platform or "").casefold() == "app"
        action_facts = None
        action_facts_loaded = False

        async def read_action_facts_once():
            nonlocal action_facts, action_facts_loaded
            if not action_facts_loaded:
                if self._action_facts_reader is None:
                    action_facts_loaded = True
                    return None
                try:
                    action_facts = await self._action_facts_reader.get_facts(
                        task_id=task, capture_id=capture, agent_run_id=run_id,
                    )
                except Exception:  # noqa: BLE001 - 缺可信连续动作不能补充候选或准备状态
                    logger.warning("读取 App 业务动作材料失败，相关规则暂缓发布", exc_info=True)
                action_facts_loaded = True
            return action_facts

        before_scene = None
        if is_app:
            try:
                before_scene = await self._action_scene_reader.get_before_scene(
                    task_id=task, capture_id=capture, agent_run_id=run_id,
                )
            except Exception:  # noqa: BLE001 - 场景读失败只延后配对规则，不补造可信前帧
                logger.warning("读取 App 动作前场景失败，配对规则暂缓发布", exc_info=True)
        startup_result_pair = before_scene is not None and before_scene.page_type == "startup_ad"
        annotation_page_type = str(visual_page_type or "").strip() or None
        ad_landing_pair = (
            await self._ad_landing_rule_scope_reader.has_complete_pair(
                task_id=task,
                capture_id=capture,
            )
            if self._ad_landing_rule_scope_reader is not None
            else None
        )
        page_type_text = "startup_ad" if ad_landing_pair is True else annotation_page_type
        startup_transition_only = page_type_text is None and startup_result_pair
        list_transition_only = False
        list_interaction_source = None
        if is_app and before_scene is not None and not startup_transition_only:
            facts = await read_action_facts_once()
            list_interaction_source = (
                app_list_interaction_source_capture(**facts) if facts is not None else None
            )
            if list_interaction_source is not None and (
                list_interaction_source["capture_id"] != before_scene.capture_id
            ):
                list_interaction_source = None
            if page_type_text is None and list_interaction_source is not None:
                page_type_text = before_scene.page_type
                list_transition_only = True
        if startup_transition_only:
            # 只按可信动作前开屏页匹配跳过规则，结果帧页型和 primary 均不伪造。
            page_type_text = "startup_ad"
        registry = get_default_page_type_registry()
        catalog = registry.catalog
        selected_page_types = frozenset(getattr(snapshot, "inspection_scope", ()))
        # page_state=popup：稍后只发布弹窗／广告契约（须在展示层延后之后过滤）。
        # 页型不可用时另做宿主回退，避免时间轴 page_type=app 空跑。
        force_popup_relevant_rules = _is_popup_obstruction_page_state(state)
        popup_host_fallback = False
        if is_app and (
            _is_unusable_visual_page_type(page_type_text) or force_popup_relevant_rules
        ):
            obstruction = force_popup_relevant_rules
            if not obstruction and self._presentation_reader is not None:
                try:
                    early_presentation = await self._presentation_reader.get_presentation(
                        task_id=task,
                        capture_id=capture,
                    )
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "读取 App 展示层失败，遮挡回退仅依据 page_state／页型",
                        exc_info=True,
                    )
                    early_presentation = None
                if _presentation_indicates_popup_obstruction(early_presentation):
                    obstruction = True
                    force_popup_relevant_rules = True
            if _is_unusable_visual_page_type(page_type_text) and (
                obstruction or _is_unusable_visual_page_type(annotation_page_type)
            ):
                # 时间轴上的 page_type=app／空值：按宿主回退，至少挂上弹窗／广告规则。
                force_popup_relevant_rules = True
                host = _resolve_popup_host_page_type(
                    registry=registry,
                    selected_page_types=selected_page_types,
                    before_scene_page_type=(
                        before_scene.page_type if before_scene is not None else None
                    ),
                )
                if host is not None:
                    page_type_text = host
                    popup_host_fallback = True
        if page_type_text is None or _is_unusable_visual_page_type(page_type_text):
            if not prepare_only:
                await self._annotator.annotate_inspection(
                    task_id=task,
                    capture_id=capture,
                    page_type=None if page_type_text is None else str(page_type_text),
                    page_state=state,
                    inspection_status="unmatched_page_type",
                    published_count=0,
                )
            return AppCaptureInspectionResult(
                status="unmatched_page_type",
                published_count=0,
                page_capture_id=capture,
                page_type=None if page_type_text is None else str(page_type_text),
                message="视觉理解未给出可匹配的 page_type",
            )

        try:
            page_type = catalog.require_code(page_type_text).code
        except InvalidPageTypeCodeError:
            alias = registry.resolve_alias(page_type_text)
            if alias is None and is_app:
                host = _resolve_popup_host_page_type(
                    registry=registry,
                    selected_page_types=selected_page_types,
                    before_scene_page_type=(
                        before_scene.page_type if before_scene is not None else None
                    ),
                )
                if host is not None:
                    page_type = host
                    popup_host_fallback = True
                    force_popup_relevant_rules = True
                    alias = None
                else:
                    alias = None
            if alias is None and not popup_host_fallback:
                if not prepare_only:
                    await self._annotator.annotate_inspection(
                        task_id=task,
                        capture_id=capture,
                        page_type=page_type_text,
                        page_state=state,
                        inspection_status="unmatched_page_type",
                        published_count=0,
                    )
                return AppCaptureInspectionResult(
                    status="unmatched_page_type",
                    published_count=0,
                    page_type=page_type_text,
                    page_capture_id=capture,
                    message=f"page_type 不在目录且无法通过别名解析: {page_type_text}",
                )
            if not popup_host_fallback:
                page_type = alias.canonical_code

        effective_annotation_page_type = (
            annotation_page_type
            if (
                ad_landing_pair is True
                or startup_transition_only
                or list_transition_only
            )
            and not popup_host_fallback
            else page_type
        )
        if popup_host_fallback and _is_popup_obstruction_page_state(state) is False:
            # 宿主回退帧按遮挡态收成弹窗规则，避免底层内容规则空跑宿主。
            state = "popup"
            force_popup_relevant_rules = True

        result_page_type = None if startup_transition_only or list_transition_only else page_type
        capture_page_out_of_scope = bool(
            is_app
            and selected_page_types
            and page_type not in selected_page_types
            and not popup_host_fallback
        )
        auth_gate = await self._resolve_auth_gate(
            snapshot=snapshot,
            task_id=task,
            capture_id=capture,
        )
        rules = await self._task_query.list_candidate_rules(
            task_id=task,
            page_type=page_type,
            page_url=await self._task_query.get_page_capture_url(
                task_id=task,
                page_capture_id=capture,
            ),
            auth_gate=auth_gate,
        )
        serialized_rules = [_candidate_rule_to_dict(rule) for rule in rules]
        if capture_page_out_of_scope:
            # 范围外只留探索记录；后续只可承接合同允许的可信范围内动作前帧。
            serialized_rules = []
        # 弹窗态过滤挪到展示层延后之后，避免遮挡业务规则被误判成 no_rules_applicable。
        pending_popup_relevant_filter = bool(force_popup_relevant_rules)
        if ad_landing_pair is True:
            # 浏览器落地帧只承担成对广告旅程规则，不能顺带执行其他 startup_ad 规则。
            serialized_rules = [
                rule for rule in serialized_rules if rule.get("rule_code") == "alioth_17"
            ]
        elif ad_landing_pair is False:
            # App 广告源帧先留证，待同一次点击形成浏览器落地帧后再唯一执行。
            serialized_rules = [
                rule for rule in serialized_rules if rule.get("rule_code") != "alioth_17"
            ]
        # 页面 Work 只能消费 page scope。显式聚合规则统一由任务收口阶段
        # EvidenceInputSet 发布器处理，禁止借用当前 Capture 作为伪锚点。
        serialized_rules = [
            rule
            for rule in serialized_rules
            if not resolve_rule_evidence_input_configuration(rule.get("judge_config")).is_aggregate
        ]
        if is_app and before_scene is not None and before_scene.page_type != page_type:
            if list_interaction_source is not None:
                # 列表点击可正常进入详情；候选只取精确列表前帧，判定仍锚定真实结果帧。
                source_rules = await self._task_query.list_candidate_rules(
                    task_id=task, page_type=before_scene.page_type,
                    page_url=await self._task_query.get_page_capture_url(
                        task_id=task, page_capture_id=before_scene.capture_id,
                    ), auth_gate=auth_gate,
                )
                existing_codes = {rule["rule_code"] for rule in serialized_rules}
                serialized_rules.extend(
                    rule for candidate in source_rules
                    if app_rule_action_requirement(rule := _candidate_rule_to_dict(candidate))
                    == "list_scroll_interaction"
                    and rule["rule_code"] not in existing_codes
                    and not resolve_rule_evidence_input_configuration(
                        rule.get("judge_config")
                    ).is_aggregate
                )
        if list_transition_only:
            serialized_rules = [
                rule for rule in serialized_rules
                if app_rule_action_requirement(rule) == "list_scroll_interaction"
            ]
        normalized_state = state.casefold()
        app_before_rules = {
            rule["rule_code"]
            for rule in serialized_rules
            if str(snapshot.target_platform or "").casefold() == "app"
            and required_state_before_role(
                judge_config=rule.get("judge_config") or {},
                input_contract=rule.get("input_contract") or {},
            )
            is not None
        }
        app_pair_rules = {
            rule["rule_code"]
            for rule in serialized_rules
            if is_app and requires_completed_app_action_pair(rule.get("input_contract") or {})
        }
        if (
            is_app
            and before_scene is not None
            and before_scene.page_type == "startup_ad"
        ):
            # 精确跳过动作的真实结果帧承接开屏合同，正常与假跳过路径均保留原始 primary。
            before_rules = await self._task_query.list_candidate_rules(
                task_id=task,
                page_type=before_scene.page_type,
                page_url=await self._task_query.get_page_capture_url(
                    task_id=task,
                    page_capture_id=before_scene.capture_id,
                ),
                auth_gate=auth_gate,
            )
            existing_codes = {rule["rule_code"] for rule in serialized_rules}
            serialized_rules.extend(
                rule
                for candidate in before_rules
                if is_startup_ad_skip_rule(rule := _candidate_rule_to_dict(candidate))
                and rule["rule_code"] not in existing_codes
            )
        if startup_transition_only:
            serialized_rules = [rule for rule in serialized_rules if is_startup_ad_skip_rule(rule)]
        startup_result_deferred = is_app and not startup_result_pair and any(
            is_startup_ad_skip_rule(rule) for rule in serialized_rules
        )
        action_pair_deferred = bool(app_pair_rules) and before_scene is None
        serialized_rules = [
            rule
            for rule in serialized_rules
            # 规则显式要求前后帧时，bootstrap、动作前帧和未完成动作只留证。
            if rule["rule_code"] not in app_pair_rules or before_scene is not None
            # 跳过规则只交接本次完整开屏动作双图，不能拿后续单帧或旅程文字代替。
            if not (is_app and is_startup_ad_skip_rule(rule) and not startup_result_pair)
            if (
                before_scene is not None
                and _rule_matches_required_page_state(rule, before_scene.page_state.casefold())
                if rule["rule_code"] in app_before_rules
                else _rule_matches_required_page_state(rule, normalized_state)
            )
        ]
        presentation_deferred = False
        if (is_app or str(snapshot.target_platform or "").casefold() == "h5") and (
            self._presentation_reader is not None
        ):
            presentation = await self._presentation_reader.get_presentation(
                task_id=task,
                capture_id=capture,
            )
            presentation_rules = [
                rule
                for rule in serialized_rules
                if (
                    _app_presentation_ready_for_rule(
                        presentation,
                        rule,
                        paired_before=(rule["rule_code"] in app_before_rules or (
                            list_interaction_source is not None
                            and app_rule_action_requirement(rule) == "list_scroll_interaction"
                        )),
                        page_state=normalized_state,
                    )
                    if is_app
                    else _presentation_accepts_rule(presentation, rule)
                )
            ]
            presentation_deferred = is_app and len(presentation_rules) < len(serialized_rules)
            serialized_rules = presentation_rules
        if pending_popup_relevant_filter:
            # 遮挡／弹窗态默认只留广告／显式弹窗契约；完整动作配对、开屏跳过双图、
            # 列表滑动源帧已就绪的交互规则仍可发布，避免把成对证据误延后。
            def _keep_under_popup(rule: dict[str, Any]) -> bool:
                if _is_popup_relevant_rule(rule):
                    return True
                code = rule.get("rule_code")
                if (
                    before_scene is not None
                    and isinstance(code, str)
                    and code in app_pair_rules
                ):
                    return True
                if startup_result_pair and is_startup_ad_skip_rule(rule):
                    return True
                return (
                    list_interaction_source is not None
                    and app_rule_action_requirement(rule) == "list_scroll_interaction"
                )

            popup_rules = [rule for rule in serialized_rules if _keep_under_popup(rule)]
            if len(popup_rules) < len(serialized_rules):
                presentation_deferred = presentation_deferred or is_app
            serialized_rules = popup_rules
        action_readiness_deferred = False
        if is_app and any(app_rule_action_requirement(rule) for rule in serialized_rules):
            action_facts = await read_action_facts_once()
            ready_rules = [
                rule for rule in serialized_rules
                if app_rule_action_requirement(rule) is None
                or (
                    action_facts is not None
                    and assess_app_rule_action_readiness(rule, **action_facts).ready
                )
            ]
            action_readiness_deferred = len(ready_rules) < len(serialized_rules)
            serialized_rules = ready_rules
        plan = _build_execution_plan(serialized_rules)
        batches = [dict(item) for item in plan.get("batches") or [] if isinstance(item, Mapping)]
        if snapshot_only and ad_landing_pair is True:
            # alioth_17 的点击已经完成，原广告与外部浏览器落地页也已成对封存。
            # 收口阶段只需消费冻结证据，不再依赖已关闭的云手机会话。
            batches = _freeze_complete_ad_landing_batches(batches)
        has_snapshot_batches = any(
            str(item.get("session_dependency") or "snapshot") == "snapshot" for item in batches
        )
        excluded_active_session_rules: tuple[dict[str, Any], ...] = ()
        if snapshot_only:
            excluded_active_session_rules = _build_excluded_active_session_rule_snapshots(
                rules=serialized_rules,
                batches=batches,
            )
            batches = [
                item
                for item in batches
                if str(item.get("session_dependency") or "snapshot") == "snapshot"
            ]
        elif active_session_only:
            # 活会话增量链只执行必须持有 Session 的规则；快照规则统一留给
            # H5 收口 Publication，避免同一 Capture 出现两个正式发布入口。
            batches = [
                item
                for item in batches
                if str(item.get("session_dependency") or "snapshot") == "active_session"
            ]
        if not serialized_rules or not batches:
            inspection_status = (
                "out_of_scope"
                if capture_page_out_of_scope and not serialized_rules
                else "evidence_rules_deferred"
                if (
                    presentation_deferred or action_pair_deferred
                    or startup_result_deferred or action_readiness_deferred
                )
                and not serialized_rules
                else "snapshot_rules_deferred"
                if active_session_only and has_snapshot_batches
                else (
                    "active_session_rules_excluded"
                    if excluded_active_session_rules
                    else "no_rules_applicable"
                )
            )
            if not prepare_only:
                await self._annotator.annotate_inspection(
                    task_id=task,
                    capture_id=capture,
                    page_type=effective_annotation_page_type,
                    page_state=state,
                    inspection_status=inspection_status,
                    published_count=0,
                )
            return AppCaptureInspectionResult(
                status=inspection_status,
                published_count=0,
                page_type=result_page_type,
                page_capture_id=capture,
                excluded_active_session_rule_count=len(excluded_active_session_rules),
                excluded_active_session_rules=excluded_active_session_rules,
                message=(
                    "当前页面不在本任务所选质检范围内，仅保留探索记录"
                    if inspection_status == "out_of_scope" else None
                ),
            )

        if snapshot.target_platform == "h5":
            from services.api.app.application.inspection_work_items import (
                build_inspection_rule_contract_hash,
            )

            reader = getattr(self._work_item_service, "read_original_page_inputs", None)
            bindings = await reader(
                task_id=task, source_agent_run_id=run_id, page_capture_id=capture,
                rule_contracts={rule["rule_id"]: build_inspection_rule_contract_hash(rule)
                                for rule in serialized_rules}) if reader is not None else {}
            serialized_rules = [{**rule, **({"evidence_input_set_id": bindings[rule["rule_id"]]}
                if rule["rule_id"] in bindings else {})} for rule in serialized_rules]
        if prepare_only:
            commands = prepare_enforced_batch_commands_from_candidate_result(
                snapshot=snapshot,
                task_id=task,
                agent_run_id=run_id,
                page_capture_id=capture,
                rules=serialized_rules,
                batches=batches,
                defer_wakeup=True,
            )
            published_status = "published" if commands else "not_applicable"
            published_count = len(commands)
            published_items: tuple[dict[str, Any], ...] = ()
        else:
            published = await publish_enforced_batches_from_candidate_result(
                service=self._work_item_service,
                snapshot=snapshot,
                task_id=task,
                agent_run_id=run_id,
                page_capture_id=capture,
                rules=serialized_rules,
                batches=batches,
                defer_wakeup=defer_wakeup,
            )
            commands = published.commands
            published_status = published.status
            published_count = published.published_count
            published_items = published.items
            await self._annotator.annotate_inspection(
                task_id=task,
                capture_id=capture,
                page_type=effective_annotation_page_type,
                page_state=state,
                inspection_status="published",
                published_count=published_count,
            )
        logger.info(
            "task_id=%s capture_id=%s page_type=%s 发布页面质检工作项 %s 个",
            task,
            capture,
            page_type,
            published_count,
        )
        return AppCaptureInspectionResult(
            status=published_status,
            published_count=published_count,
            page_type=result_page_type,
            page_capture_id=capture,
            items=published_items,
            excluded_active_session_rule_count=len(excluded_active_session_rules),
            excluded_active_session_rules=excluded_active_session_rules,
            prepared_commands=commands if prepare_only else (),
        )

    async def _resolve_auth_gate(
        self,
        *,
        snapshot: Any,
        task_id: str,
        capture_id: str,
    ) -> str | None:
        """按平台会话事实解析规则登录闸门。"""

        platform = str(getattr(snapshot, "target_platform", None) or "").strip().casefold()
        if platform == "web":
            return AUTH_REQUIREMENT_AUTHENTICATED
        if platform not in {"app", "h5"}:
            return AUTH_GATE_AUTO
        session_auth = "unknown"
        no_test_account = False
        pool = None
        try:
            pool = await get_postgres_pool()
            async with pool.acquire() as connection:
                row = await connection.fetchrow(
                    _capture_auth_state_query(platform),
                    capture_id,
                    task_id,
                )
            if row is not None:
                session_auth = _normalized_capture_session_auth(
                    row["session_auth"],
                    platform=platform,
                )
                no_test_account = bool(row["no_test_account"]) or (row["has_test_account"] is False)
        except Exception:  # noqa: BLE001 - 读失败时按游客保守拦截需登录规则
            session_auth = "unknown"
            no_test_account = True
        # 无登录事实或无测试账号：保守阻止需登录规则，收口保留缺证未执行事实。
        if session_auth in {"", "unknown"} and not no_test_account:
            no_test_account = True
        blocked = resolve_blocked_auth_requirement_from_session_auth(
            session_auth,
            no_test_account=no_test_account,
        )
        return blocked


def _capture_auth_state_query(platform: str) -> str:
    if platform != "h5":
        return """
            SELECT
                capture.metadata->>'session_auth' AS session_auth,
                COALESCE(
                    (task.task_config->>'no_test_account')::boolean,
                    false
                ) AS no_test_account,
                COALESCE(
                    (task.task_config->>'has_test_account')::boolean,
                    true
                ) AS has_test_account
            FROM qc_capture AS capture
            JOIN qc_task AS task ON task.id = capture.task_id
            WHERE capture.id = $1::uuid
              AND capture.task_id = $2::uuid
        """
    return """
        SELECT
            COALESCE(
                (
                    SELECT step.decision_status
                    FROM qc_surface_phase_run AS phase_run
                    JOIN qc_surface_phase_step AS step
                      ON step.phase_run_id = phase_run.phase_run_id
                    WHERE phase_run.task_id = page_capture.task_id
                      AND phase_run.session_id = page_capture.surface_session_id
                      AND phase_run.phase_key = 'h5_auth_bootstrap'
                    ORDER BY phase_run.started_at DESC, step.step_index DESC
                    LIMIT 1
                ),
                common_capture.metadata->>'session_auth'
            ) AS session_auth,
            COALESCE(
                (task.task_config->>'no_test_account')::boolean,
                false
            ) AS no_test_account,
            COALESCE(
                (task.task_config->>'has_test_account')::boolean,
                true
            ) AS has_test_account
        FROM qc_page_capture AS page_capture
        JOIN qc_capture AS common_capture ON common_capture.id = page_capture.id
        JOIN qc_task AS task ON task.id = page_capture.task_id
        WHERE page_capture.id = $1::uuid
          AND page_capture.task_id = $2::uuid
    """


def _normalized_capture_session_auth(value: object, *, platform: str) -> str:
    text = str(value or "unknown").strip().casefold() or "unknown"
    if platform != "h5":
        return text
    if text in {"already_authenticated", "authenticated"}:
        return "authenticated"
    if text in {"disabled", "guest_only", "failed"}:
        return "guest"
    return "unknown"


def _build_excluded_active_session_rule_snapshots(
    *,
    rules: list[dict[str, Any]],
    batches: list[dict[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """冻结 H5 关闭会话后无法执行的 active-session 规则及人工复核原因。"""

    rules_by_code = {
        str(rule.get("rule_code") or "").strip(): rule
        for rule in rules
        if str(rule.get("rule_code") or "").strip()
    }
    snapshots: list[dict[str, Any]] = []
    seen_codes: set[str] = set()
    for batch in batches:
        if str(batch.get("session_dependency") or "snapshot") != "active_session":
            continue
        batch_key = str(batch.get("batch_id") or "").strip()
        for rule_code_value in batch.get("rule_codes") or []:
            rule_code = str(rule_code_value or "").strip()
            if not rule_code or rule_code in seen_codes:
                continue
            rule = rules_by_code.get(rule_code)
            if rule is None:
                raise RuntimeError("active-session 批次引用了不存在的候选规则")
            seen_codes.add(rule_code)
            snapshots.append(
                {
                    "rule_id": str(rule.get("rule_id") or "").strip(),
                    "rule_code": rule_code,
                    "contract_hash": build_inspection_rule_contract_hash(rule),
                    "strategy": str(batch.get("strategy") or "").strip(),
                    "category": str(batch.get("category") or "").strip() or None,
                    "session_dependency": "active_session",
                    "execution_plan": {
                        "batch_key": batch_key,
                        "disposition": "needs_review",
                        "reason": "h5_exploration_session_closed",
                    },
                }
            )
    return tuple(snapshots)


def _freeze_complete_ad_landing_batches(
    batches: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """把完整广告旅程的唯一 alioth_17 批次转换为冻结证据判定。"""

    frozen: list[dict[str, Any]] = []
    for batch in batches:
        rule_codes = [
            str(value or "").strip()
            for value in batch.get("rule_codes") or []
            if str(value or "").strip()
        ]
        if rule_codes != ["alioth_17"]:
            raise RuntimeError("完整广告落地证据批次只能包含 alioth_17")
        strategy = str(batch.get("strategy") or "").strip()
        category = str(batch.get("category") or "").strip() or None
        snapshot_batch = dict(batch)
        snapshot_batch["batch_id"] = build_rule_execution_batch_key(
            strategy=strategy,
            category=category,
            session_dependency="snapshot",
        ).batch_id
        snapshot_batch["session_dependency"] = "snapshot"
        frozen.append(snapshot_batch)
    return frozen


def build_default_app_capture_inspection_publisher(
    *,
    work_item_service: InspectionWorkItemService | None = None,
) -> AppCaptureInspectionPublisher:
    """Worker / Runtime 组合根默认入口。"""
    return AppCaptureInspectionPublisher(
        work_item_service=work_item_service,
        ad_landing_rule_scope_reader=PostgresAppAdLandingRuleScopeReader(),
        presentation_reader=PostgresCapturePresentationReader(),
        action_facts_reader=PostgresAppRuleActionFactsReader(),
    )


def _presentation_accepts_rule(
    presentation: PresentationAssessment | None,
    rule: Mapping[str, Any],
) -> bool:
    layer_target = rule_evidence_layer_target(
        category=str(rule.get("category") or "") or None,
        judge_config=(
            rule.get("judge_config") if isinstance(rule.get("judge_config"), Mapping) else None
        ),
        check_point=str(rule.get("check_point") or "") or None,
        rule_name=str(rule.get("name") or "") or None,
    )
    judge_config = rule.get("judge_config")
    return evaluate_presentation_for_rule(
        presentation,
        layer_target=layer_target,
        accepts_diagnostic=rule_accepts_diagnostic_evidence(
            judge_config if isinstance(judge_config, Mapping) else None
        ),
    ).accepted


# 通用别名供 H5 复用同一套“页面类型 → 候选规则 → 工作项”协议；
# 保留 App 名称，避免破坏既有 App 组合根和公开导入。
CaptureInspectionResult = AppCaptureInspectionResult
CaptureInspectionPublisher = AppCaptureInspectionPublisher
