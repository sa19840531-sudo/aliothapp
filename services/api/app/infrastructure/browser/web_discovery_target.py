"""目录外 Web 导航的密封来源及动作前当前候选复验。"""

from collections.abc import Mapping
from dataclasses import replace
from urllib.parse import urlsplit, urlunsplit

from services.api.app.application.interaction_runtime import (
    ResolvedInteractionTarget,
    SemanticTargetResolutionError,
)
from services.api.app.application.web_desktop_navigation import (
    WebNavigationCandidate,
)
from services.api.app.application.web_runtime_discovery import discovery_safety_reason
from services.api.app.domain.interaction import InteractionOperation
from services.api.app.domain.page.web_origin import web_origin_matches
from services.api.app.domain.page.web_url_identity import DEFAULT_WEB_URL_IDENTITY_POLICY


def _reject():
    return SemanticTargetResolutionError(
        "Web 目录外导航候选已漂移、不唯一或包含未授权动作",
        error_code="WEB_DISCOVERY_CANDIDATE_UNTRUSTED",
    )


def _candidate(raw):
    if not isinstance(raw, Mapping):
        raise _reject()
    try:
        return WebNavigationCandidate(**{**raw, "menu_path": tuple(raw["menu_path"])})
    except (KeyError, TypeError, ValueError) as exc:
        raise _reject() from exc


def _candidates(snapshot):
    navigation = snapshot.get("web_navigation")
    raw = navigation.get("candidates") if isinstance(navigation, Mapping) else None
    if not isinstance(raw, list):
        raise _reject()
    return tuple(_candidate(item) for item in raw)


def _stable_candidate_structure(candidate):
    """返回冻结快照中不随来源绑定和 URL 规范化变化的菜单结构身份。"""
    return (
        candidate.label,
        candidate.menu_path,
        candidate.dom_fingerprint,
        candidate.source_kind,
        candidate.target_hint,
        candidate.menu_position,
        candidate.parent_menu_entry_id,
        candidate.parent_logical_target_identity,
        candidate.menu_depth,
        candidate.exploration_path,
        candidate.navigation_container_id,
    )


def _canonical_destination(url: str) -> str:
    """与运行时 frontier 一致地忽略非根路径末尾斜杠。"""
    canonical = DEFAULT_WEB_URL_IDENTITY_POLICY.canonical(url)
    parsed = urlsplit(canonical)
    path = parsed.path
    if path != "/":
        path = path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))


async def resolve_web_discovery_target(request, *, snapshot_reader):
    """从冻结发现 Capture 复验同源 href，并由当前 Capture 直接导航。"""
    parameters = request.parameters
    source_id = parameters.get("web_discovery_source_capture_id")
    origin = parameters.get("web_discovery_origin")
    declared = _candidate(parameters.get("web_discovery_candidate"))
    effective_origin = declared.frontier_site_origin or origin
    candidate_source_id = declared.source_capture_id
    if (
        not isinstance(source_id, str)
        or not source_id
        or not isinstance(candidate_source_id, str)
        or not candidate_source_id
        or not isinstance(origin, str)
        or not origin
        or "selector" in parameters
        or discovery_safety_reason(declared, site_origin=origin)
    ):
        raise _reject()
    sealed = await snapshot_reader.read_ui_snapshot(
        task_id=request.task_id,
        page_capture_id=candidate_source_id,
    )
    current = await snapshot_reader.read_ui_snapshot(
        task_id=request.task_id,
        page_capture_id=request.source_capture_id,
    )
    if (
        sealed.get("capture_id", candidate_source_id) != candidate_source_id
        or current.get("capture_id", request.source_capture_id) != request.source_capture_id
        or not web_origin_matches(
            str(sealed.get("url") or ""),
            effective_origin,
        )
        or not web_origin_matches(
            str(current.get("url") or ""),
            effective_origin,
        )
    ):
        raise _reject()
    try:
        destination_url = _canonical_destination(declared.url)
    except ValueError as exc:
        raise _reject() from exc
    originals = [
        item
        for item in _candidates(sealed)
        if item.url is not None
        and _canonical_destination(item.url) == destination_url
        and _stable_candidate_structure(item) == _stable_candidate_structure(declared)
    ]
    if len(originals) != 1:
        raise _reject()
    original = originals[0]
    normalized_original = replace(original, url=destination_url)
    if (
        original.source_capture_id not in {None, candidate_source_id}
        or normalized_original.logical_target_identity != declared.logical_target_identity
        or discovery_safety_reason(original, site_origin=effective_origin)
    ):
        raise _reject()
    if str(request.semantic_target).strip() != " / ".join(declared.menu_path):
        raise _reject()
    if request.action_type is not InteractionOperation.NAVIGATE or declared.url is None:
        raise _reject()
    try:
        requested_destination = _canonical_destination(parameters.get("destination_url"))
    except (TypeError, ValueError) as exc:
        raise _reject() from exc
    if requested_destination != destination_url:
        raise _reject()
    return ResolvedInteractionTarget(
        destination_url=destination_url,
        target_source="web_discovery_candidate",
        target_signature=declared.entry_id,
        target_trust="trusted",
        trusted_target_texts=(original.label,),
    )
