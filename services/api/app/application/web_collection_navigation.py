"""集合页有界 href 探索；复用可信快照和既有导航协调器。"""

from dataclasses import dataclass
from hashlib import sha256

from services.api.app.application.capture_coordination import CaptureCoordinationCommand
from services.api.app.application.web_detail_candidate_extraction import (
    detail_candidates_from_ready_snapshot,
)
from services.api.app.application.web_exploration_workflow import WebSurfaceBootstrapper
from services.api.app.application.web_navigation_safety import (
    WebNavigationSafetyError,
    validate_web_final_url,
)
from services.api.app.application.web_run_stage_orchestrator import WebStageFailure
from services.api.app.domain.interaction import (
    ActionIntent,
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)


@dataclass(frozen=True)
class WebCollectionEntry:
    candidate_id: str
    source_capture_id: str
    url: str


def collection_entries(snapshot, *, origin):
    """复用重复主体锚点的安全过滤；不把其默认详情提示作为集合页类型证据。"""
    source = str(snapshot.get("capture_id") or "")
    current = str(snapshot.get("url") or "")
    candidates = detail_candidates_from_ready_snapshot(
        snapshot,
        list_page_type="content_list",
        origin=origin,
        current_url=current,
    )
    return tuple(
        WebCollectionEntry(
            candidate_id=sha256(f"{source}\0{item.canonical_url}".encode()).hexdigest(),
            source_capture_id=source,
            url=item.canonical_url,
        )
        for item in candidates
        if item.canonical_url.rstrip("/") != current.rstrip("/")
    )


async def navigate_collection_entry(service, command, *, snapshot, candidate_id, requested_url):
    """执行前重读同一来源快照签发的锚点，不接收任意目的地址。"""
    candidate = next(
        (
            item
            for item in collection_entries(snapshot, origin=command.target_url)
            if item.candidate_id == candidate_id and item.url == requested_url
        ),
        None,
    )
    if candidate is None:
        raise WebStageFailure("WEB_COLLECTION_CANDIDATE_STALE")
    source = candidate.source_capture_id
    key = f"web-collection:{command.agent_run_id}:{candidate.candidate_id}"
    result = await service.coordinate(
        CaptureCoordinationCommand(
            agent_run_id=command.agent_run_id,
            intent=ActionIntent(
                task_id=command.task_id,
                session_id=command.session_id,
                intent_id=key,
                idempotency_key=key,
                surface_kind=SurfaceKind.WEB,
                source_capture_id=source,
                action_type=InteractionOperation.NAVIGATE,
                semantic_target="已验证的集合链路 href",
                purpose="有界集合入口探索",
                capture_profile=WebSurfaceBootstrapper.capture_profile(),
                deadline=command.deadline,
                parameters={
                    "destination_url": candidate.url,
                    "web_capture_stage": "web_page_probe",
                    "clear_obstructions": False,
                    "full_page": False,
                },
                risk_level=InteractionRiskLevel.LOW,
            ),
        )
    )
    capture_id = str(getattr(getattr(result, "after_capture", None), "capture_id", "") or "")
    if not capture_id:
        raise WebStageFailure("WEB_COLLECTION_PROBE_MISSING")
    return capture_id


def validate_collection_arrival(command, *, requested_url, final_url):
    try:
        validate_web_final_url(
            site_url=command.target_url, requested_url=requested_url, final_url=final_url
        )
    except WebNavigationSafetyError as exc:
        raise WebStageFailure(exc.error_code) from exc
