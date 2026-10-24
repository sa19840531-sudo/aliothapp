"""单次展示补证：动作前复核，原期限内有限取帧，不重放点击或伪造规则通过。"""

import asyncio
import re
from collections.abc import Mapping

from services.api.app.application.h5_probe_journey import probe_scroll_context
from services.api.app.application.h5_probe_targets import (
    PROBE_TARGET_LABELS,
    back_to_top_structure,
    probe_subject_key,
    select_probe_target,
    visible_probe_element,
)


async def execute_display_probe(*, page, request, action_trace: list, timeout_ms: int):
    deadline = asyncio.get_running_loop().time() + min(timeout_ms, request.timeout_ms) / 1000
    async with asyncio.timeout_at(deadline):
        await _execute_display_probe(
            page=page, request=request, action_trace=action_trace, deadline=deadline
        )


def _gallery_image_matches(item, source):
    binding = source.get("h5_probe_gallery_binding") or {}
    gallery = item.get("h5_probe_gallery_item") or {}
    return bool(
        binding.get("protocol") == "viewerjs-data.v1"
        and item.get("src") == binding.get("resource")
        and isinstance(binding.get("instance_id"), str) and binding["instance_id"]
        and binding.get("instance_id") == gallery.get("instance_id")
        and type(binding.get("source_index")) is int
        and type(gallery.get("source_index")) is int
        and binding.get("source_index") == gallery.get("source_index")
        and isinstance(gallery.get("root"), str) and gallery["root"]
        and gallery.get("root") == item.get("h5_probe_preview_root")
    )


def _fullscreen_image(item, snapshot, source):
    image = item.get("image") or {}
    rect, view = item.get("rect") or {}, snapshot.get("viewport") or {}
    return bool(
        item.get("tag") == "img"
        and (
            item.get("src") == source.get("src")
            or ((source.get("h5_probe_image_resources") or {}).get("complete") is True
                and item.get("src") in (source["h5_probe_image_resources"].get("values") or []))
            or _gallery_image_matches(item, source)
        )
        and image.get("complete") is True
        and image.get("natural_width", 0) > 0
        and image.get("natural_height", 0) > 0
        and visible_probe_element(item, snapshot)
        and max(
            0,
            min(rect.get("x", 0) + rect.get("width", 0), view.get("width", 0))
            - max(0, rect.get("x", 0)),
        )
        * max(
            0,
            min(rect.get("y", 0) + rect.get("height", 0), view.get("height", 0))
            - max(0, rect.get("y", 0)),
        )
        >= 0.85 * view.get("width", 1) * view.get("height", 1)
    )


def _image_preview(before, after, source):
    elements = after.get("elements") or []
    original = next((item for item in elements if item.get("selector") == source["selector"]), {})
    if before.get("h5_probe_preview_roots_truncated") is not False:
        return {}
    if after.get("h5_probe_preview_roots_truncated") is not False:
        return {}
    previous = before.get("h5_probe_preview_roots") or []
    current_roots = set(after.get("h5_probe_preview_roots") or [])
    new_roots = current_roots - set(previous)
    left = {item["root"]: item.get("visible")
            for item in before.get("h5_probe_preview_root_states") or []}
    right = {item["root"]: item.get("visible")
             for item in after.get("h5_probe_preview_root_states") or []}
    if set(left) != set(previous) or set(right) != current_roots:
        return {}
    revealed = {root for root in right if left.get(root) is False and right[root] is True}
    if new_roots and revealed:
        return {}
    # 资源声明和控制关系必须仍属于原目标，不按 URL 去参数或同名文件猜高清配对。
    if (original.get("h5_probe_image_resources") != source.get("h5_probe_image_resources")
        or original.get("h5_probe_gallery_binding") != source.get("h5_probe_gallery_binding")
        or original.get("h5_probe_preview_controlled_root")
        != source.get("h5_probe_preview_controlled_root")):
        return {}
    if _fullscreen_image(original, after, source):
        return original if not new_roots else {}
    roots = new_roots
    if not roots:
        controlled = source.get("h5_probe_preview_controlled_root")
        roots = {controlled} if controlled in previous and revealed == {controlled} else set()
        if not roots and source.get("h5_probe_gallery_binding"):
            roots = revealed
    if len(roots) != 1:
        return {}
    previews = [
        item
        for item in elements
        if item.get("h5_probe_preview_root")
        and item["h5_probe_preview_root"] in roots
        and _fullscreen_image(item, after, source)
        # 已声明图库身份不能借相同缩略图地址绕过实际实例和索引证明。
        and (not source.get("h5_probe_gallery_binding") or _gallery_image_matches(item, source))
    ]
    # 一张源图只能绑定一个新登记根，或有明确控制绑定且从隐藏转为可见的旧根。
    return previews[0] if len(previews) == 1 else {}


async def _effect(page, goal, before, after, subject, expected):
    current = next(
        (
            item
            for item in after.get("elements") or []
            if item.get("selector") == subject["selector"]
        ),
        {},
    )
    if before.get("url") != after.get("url") or (
        (before.get("document") or {}).get("time_origin_ms")
        != (after.get("document") or {}).get("time_origin_ms")
    ):
        return False, current
    if goal == "native_video_fullscreen":
        verified = bool(
            (after.get("document_state") or {}).get("fullscreen_active") is True
            and await page.locator(subject["selector"]).evaluate(
                "node=>document.fullscreenElement===node"
            )
            and current.get("h5_media_instance_id") == subject.get("h5_media_instance_id")
            and current.get("src") == subject.get("src")
        )
    elif back_to_top_structure(subject):
        left, right = probe_scroll_context(before), probe_scroll_context(after)
        verified = bool(
            left
            and right
            and left["selector"] == right["selector"]
            and left["scroll_top"] > left["client_height"]
            and right["scroll_top"] <= 2
        )
    elif goal == "scroll_button_feedback":
        left = subject.get("h5_probe_controlled_region") or {}
        right = current.get("h5_probe_controlled_region") or {}
        verified = bool(
            current.get("aria_expanded") in {"true", "false"}
            and current.get("aria_expanded") != subject.get("aria_expanded")
            and left.get("unique") is True
            and right.get("unique") is True
            and left.get("id") == right.get("id")
            and any(left.get(key) != right.get(key) for key in ("visible", "height", "width"))
        )
    elif goal == "payment_plan_selection":
        verified = bool(
            current.get("aria_checked") == "true"
            or current.get("aria_selected") == "true"
            or {"selected", "checked"}.intersection(current.get("class_tokens") or [])
        )
        verified = verified and probe_subject_key(current) == expected
    else:
        current = _image_preview(before, after, subject)
        verified = bool(current)
    return verified, current


async def _execute_display_probe(*, page, request, action_trace, deadline):
    from services.api.app.infrastructure.browser.h5_readonly_display import seal_h5_payment_display
    from services.api.app.infrastructure.browser.page_capture import (
        BlockedPageCaptureActionError,
        PageCaptureMode,
        PageTriggerAction,
        _collect_ui_snapshot,
    )

    contract = request._h5_probe_display_contract
    if (
        not isinstance(contract, Mapping)
        or set(contract) != {"protocol_version", "goal", "page_type", "subject_key"}
        or contract.get("protocol_version") != "h5-probe-display.v1"
        or not isinstance(contract.get("goal"), str)
        or contract["goal"] not in PROBE_TARGET_LABELS
        or not isinstance(contract.get("subject_key"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", contract["subject_key"])
        or not isinstance(contract.get("page_type"), str)
        or not str(request.session_id or "").startswith("h5:")
        or request.mode != PageCaptureMode.SINGLE_TRIGGER
        or request.trigger_action != PageTriggerAction.CLICK
    ):
        raise BlockedPageCaptureActionError(
            "展示补证缺少可信动作合同", error_code="H5_PROBE_DISPLAY_INVALID"
        )
    before = await _collect_ui_snapshot(page, surface_session_id=request.session_id)
    subject = select_probe_target(
        before,
        goal=contract["goal"],
        page_type=contract["page_type"],
        expected_key=contract["subject_key"],
    )
    if subject is None or subject.get("selector") != request.selector:
        raise BlockedPageCaptureActionError(
            "动作时展示目标已丢失、遮挡或漂移", error_code="H5_PROBE_TARGET_STALE"
        )
    goal = contract["goal"]
    guard = await seal_h5_payment_display(page) if goal == "payment_plan_selection" else None
    denied_before = await guard.denied_count(page) if guard else 0
    target = page.locator(subject["selector"])
    if goal == "native_video_fullscreen":
        await target.evaluate("node=>node.requestFullscreen()")
    else:
        await target.click(
            timeout=max(1, int((deadline - asyncio.get_running_loop().time()) * 1000))
        )
    # 真正输入后先记执行事实；超时取消不能把已点击伪装成可重放的未执行动作。
    action_trace[-1]["result"] = "probe_effect_unverified"
    effect = {
        "protocol_version": "h5-probe-display-effect.v1",
        "goal": goal,
        "subject_key": contract["subject_key"],
        "verified": False,
        "before_selector": subject.get("selector"),
        "before_resource": subject.get("src"),
        "before_rect": subject.get("rect"),
        "readonly_isolation": guard is not None,
    }
    action_trace[-1]["state_diff"] = {"h5_probe_effect": effect}
    loop = asyncio.get_running_loop()
    settle_until = min(deadline - 0.05, loop.time() + 1.2)
    stable, previous = 0, None
    for _ in range(12):
        after = await _collect_ui_snapshot(page, surface_session_id=request.session_id)
        verified, current = await _effect(
            page, goal, before, after, subject, contract["subject_key"]
        )
        denied = await guard.denied_count(page) - denied_before if guard else 0
        verified = verified and denied == 0 and not after.get("capture_degraded")
        state = (
            current.get("selector"),
            current.get("src"),
            current.get("h5_probe_preview_root"),
            current.get("rect"),
            current.get("h5_probe_controlled_region"),
            probe_scroll_context(after),
        )
        stable = stable + 1 if verified and previous == state else (1 if verified else 0)
        previous = state
        effect.update(
            url=after.get("url"),
            document_time_origin_ms=(after.get("document") or {}).get("time_origin_ms"),
            after_rect=current.get("rect"),
            after_selector=current.get("selector"),
            after_resource=current.get("src"),
            preview_root_id=current.get("h5_probe_preview_root"),
            readonly_denied_count=denied,
            verified=stable >= 2,
        )
        if stable >= 2 or denied or loop.time() >= settle_until:
            break
        await asyncio.sleep(min(0.1, max(0, settle_until - loop.time())))
    if effect["verified"]:
        action_trace[-1]["result"] = "probe_effect_observed"
