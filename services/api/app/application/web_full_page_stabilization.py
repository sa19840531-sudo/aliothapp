"""通用长页稳定、资源上限与截图期间的装饰层恢复，不触碰业务 Capture。"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import asdict, dataclass, field, replace
from uuid import uuid4

from services.api.app.domain.exploration.web_full_page_policy import (
    WebFullPageStabilizationPolicy,
)
from services.api.app.domain.exploration.web_run_policy_snapshot import (
    WEB_FULL_PAGE_SUPPLEMENTAL_MAXIMUM_DIMENSION,
    WEB_FULL_PAGE_SUPPLEMENTAL_MAXIMUM_PIXELS,
)
from services.api.app.infrastructure.browser.web_full_page_probe import WEB_FULL_PAGE_PROBE

LIMIT = "WEB_FULL_PAGE_SCREENSHOT_LIMIT_EXCEEDED"
TIMEOUT = "WEB_FULL_PAGE_SCREENSHOT_TIMEOUT"
DRIFT = "WEB_FULL_PAGE_SCREENSHOT_STATE_DRIFT"


@dataclass(frozen=True, kw_only=True)
class WebFullPageStabilizationResult:
    document_height: int = 0
    captured_height: int = 0
    truncated: bool = False
    scroll_steps: int = 0
    sticky_layers: tuple[dict, ...] = ()
    state_hash: str = ""
    semantic_hash: str = ""
    limit_exceeded: bool = False
    stable: bool = False
    reason_code: str | None = None
    wait_elapsed_ms: int = 0
    restored: bool = False
    scroll_restore_status: str = "not_attempted"
    style_restore_status: str = "not_attempted"
    policy: WebFullPageStabilizationPolicy = field(default_factory=WebFullPageStabilizationPolicy)

    @property
    def audit(self):
        return asdict(self)


class WebFullPageStabilizationError(RuntimeError):
    def __init__(self, result):
        self.result = result
        self.reason_code = result.reason_code or DRIFT
        super().__init__(self.reason_code)


@dataclass(frozen=True, kw_only=True)
class WebScrolledViewportCaptureResult:
    """滚动到冻结深度后取得的一张视口证据，并证明滚动位置已恢复。"""

    screenshot_bytes: bytes | None = None
    document_height: int = 0
    viewport_height: int = 0
    requested_scroll_offset_px: int = 1920
    actual_scroll_y: int = 0
    viewport_top_css_px: int = 0
    viewport_bottom_css_px: int = 0
    stable: bool = False
    reason_code: str | None = None
    restored: bool = False
    scroll_restore_status: str = "not_attempted"
    capture_mode: str = "scroll_offset_viewport"

    @property
    def audit(self):
        payload = asdict(self)
        payload.pop("screenshot_bytes", None)
        return payload

    @property
    def manifest(self):
        payload = self.screenshot_bytes or b""
        return {
            "protocol_version": "web-scrolled-viewport-manifest.v1",
            "capture_mode": self.capture_mode,
            "document_height": self.document_height,
            "requested_scroll_offset_px": self.requested_scroll_offset_px,
            "actual_scroll_y": self.actual_scroll_y,
            "viewport_top_css_px": self.viewport_top_css_px,
            "viewport_bottom_css_px": self.viewport_bottom_css_px,
            "screenshot": {
                "size_bytes": len(payload),
                "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
            },
        }


async def _probe(page, operation="sample", **kwargs):
    return await page.evaluate(WEB_FULL_PAGE_PROBE, {"operation": operation, **kwargs})


def _digest(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


async def probe_direct_full_page_candidate(page):
    """只预检整页截图的几何上限；内容是否完整由后续质检判断。"""

    try:
        sample = await asyncio.wait_for(
            page.evaluate(
                """() => ({
                    viewport_width: innerWidth,
                    document_width: Math.max(
                        document.documentElement.scrollWidth,
                        document.body?.scrollWidth || 0,
                        innerWidth
                    ),
                    document_height: Math.max(
                        document.documentElement.scrollHeight,
                        document.body?.scrollHeight || 0
                    )
                })"""
            ),
            timeout=0.5,
        )
    except Exception as exc:  # noqa: BLE001 - 几何探测失败时不生成视口替代图。
        return False, {
            "capture_mode": "unavailable",
            "probe_reason": "timeout" if isinstance(exc, TimeoutError) else "unavailable",
            "reason_code": TIMEOUT if isinstance(exc, TimeoutError) else DRIFT,
        }
    viewport_width = sample.get("viewport_width") if isinstance(sample, dict) else None
    document_width = sample.get("document_width") if isinstance(sample, dict) else None
    height = sample.get("document_height") if isinstance(sample, dict) else None
    direct = (
        type(viewport_width) is int
        and viewport_width == 1920
        and type(document_width) is int
        and viewport_width <= document_width <= WEB_FULL_PAGE_SUPPLEMENTAL_MAXIMUM_DIMENSION
        and type(height) is int
        and 0 < height <= WEB_FULL_PAGE_SUPPLEMENTAL_MAXIMUM_DIMENSION
        and document_width * height <= WEB_FULL_PAGE_SUPPLEMENTAL_MAXIMUM_PIXELS
    )
    exceeded = (
        type(document_width) is int
        and type(height) is int
        and (document_width > WEB_FULL_PAGE_SUPPLEMENTAL_MAXIMUM_DIMENSION
             or height > WEB_FULL_PAGE_SUPPLEMENTAL_MAXIMUM_DIMENSION
             or document_width * height > WEB_FULL_PAGE_SUPPLEMENTAL_MAXIMUM_PIXELS)
    )
    return direct, {
        "capture_mode": "direct_full_page" if direct else "unavailable",
        "document_height": height,
        "viewport_width": viewport_width,
        "document_width": document_width,
        **({"probe_reason": "limit_exceeded" if exceeded else "invalid_geometry",
            "reason_code": LIMIT if exceeded else DRIFT} if not direct else {}),
    }


async def _restore(operation):
    """清理有独立的短时限；原始取消到达后也等待恢复完成。"""
    task = asyncio.create_task(asyncio.wait_for(operation, timeout=0.5))
    cancellation = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
        except Exception:
            break
    if cancellation is not None:
        # 取出清理结果避免遗留未回收异常；取消优先于清理失败。
        try:
            task.result()
            cancellation.restoration_status = "restored"
        except (Exception, asyncio.CancelledError) as exc:
            cancellation.restoration_status = _restoration_status(exc)
        raise cancellation
    return task.result()


async def _scroll_geometry(page, operation="sample", *, x=None, y=None):
    """滚动视口只读几何信息，避免再次执行长页全量 DOM 探测。"""
    return await page.evaluate(
        """({operation, x, y}) => {
          if (operation === 'scroll')
            window.scrollTo({left: x, top: y, behavior: 'instant'});
          const root = document.documentElement;
          return {
            document_height: Math.max(root.scrollHeight, document.body?.scrollHeight || 0),
            viewport_height: innerHeight,
            scroll_x: scrollX,
            scroll_y: scrollY,
          };
        }""",
        {"operation": operation, "x": x, "y": y},
    )


async def capture_scrolled_viewport(page, policy):
    """请求滚动 1920px，滚动停止即截取视口，不要求落点精确相等。"""

    initial = await asyncio.wait_for(_scroll_geometry(page), timeout=0.5)
    requested = policy.scroll_offset_px
    maximum_scroll_y = max(0, initial["document_height"] - initial["viewport_height"])
    target = min(requested, maximum_scroll_y)
    payload = None
    sample = initial
    reason = None
    scroll_restore_status = "not_attempted"
    cancellation = None
    error = None
    try:
        await asyncio.wait_for(
            _scroll_geometry(page, "scroll", x=initial["scroll_x"], y=target), timeout=0.5
        )
        sample = await _wait_for_scroll_stop(page)
        payload = await asyncio.wait_for(
            page.screenshot(full_page=False, type="png"), timeout=10
        )
    except asyncio.CancelledError as exc:
        cancellation = exc
    except TimeoutError:
        reason = TIMEOUT
    except Exception as exc:  # noqa: BLE001 - 由结果对象统一映射为稳定化失败。
        error = exc
        reason = DRIFT
    finally:
        try:
            await _restore(
                _scroll_geometry(page, "scroll", x=initial["scroll_x"], y=initial["scroll_y"])
            )
            scroll_restore_status = "restored"
        except (Exception, asyncio.CancelledError) as exc:
            scroll_restore_status = _restoration_status(exc)
    result = WebScrolledViewportCaptureResult(
        screenshot_bytes=payload,
        document_height=int(sample.get("document_height", 0)),
        viewport_height=int(sample.get("viewport_height", 0)),
        requested_scroll_offset_px=requested,
        actual_scroll_y=int(sample.get("scroll_y", 0)),
        viewport_top_css_px=int(sample.get("scroll_y", 0)),
        viewport_bottom_css_px=min(
            int(sample.get("document_height", 0)),
            int(sample.get("scroll_y", 0)) + int(sample.get("viewport_height", 0)),
        ),
        # 截图已经在滚动停稳后完成；随后恢复失败只记录审计，不能丢弃有效补充证据。
        stable=payload is not None and reason is None and error is None,
        reason_code=reason or (DRIFT if error is not None else None),
        restored=scroll_restore_status == "restored",
        scroll_restore_status=scroll_restore_status,
    )
    if cancellation is not None:
        cancellation.stabilization_result = result
        raise cancellation
    return result


async def _wait_for_scroll_stop(page, *, maximum_samples: int = 10):
    """只等待滚动值停止变化；滚动吸附、短页钳制均接受实际落点。"""

    previous_y = None
    sample = None
    for _ in range(maximum_samples):
        await asyncio.sleep(0.1)
        sample = await asyncio.wait_for(_scroll_geometry(page), timeout=0.5)
        current_y = int(sample.get("scroll_y", 0))
        if previous_y == current_y:
            return sample
        previous_y = current_y
    return sample or await asyncio.wait_for(_scroll_geometry(page), timeout=0.5)


def _restoration_status(error):
    if isinstance(error, TimeoutError):
        return "timeout"
    return getattr(error, "restoration_status", "error")


async def stabilize_for_full_page(page, policy):
    started = time.monotonic()
    sample = {}
    initial = None
    steps, rounds = 0, 0
    stable, reason = False, None
    error = None
    restore_error = None
    scroll_restore_status = "not_attempted"
    try:
        async with asyncio.timeout(policy.lazy_load_timeout_ms / 1000):
            sample = initial = await _probe(page)
            while True:
                height = sample["document_height"]
                if steps >= policy.max_scroll_steps:
                    reason = LIMIT
                    break
                captured_height = min(height, policy.max_document_height)
                bottom = max(0, captured_height - sample["viewport_height"])
                target = min(bottom, sample["scroll_y"] + sample["viewport_height"] * 0.8)
                await _probe(page, "scroll", x=initial["scroll_x"], y=target)
                steps += 1
                await asyncio.sleep(0.1)
                previous = sample
                sample = await _probe(page)
                unchanged = (
                    sample["state_hash"] == previous["state_hash"]
                    and sample["document_height"] == previous["document_height"]
                    and sample["pending_images"] == 0
                    and sample["scroll_y"] + sample["viewport_height"]
                    >= min(sample["document_height"], policy.max_document_height)
                )
                rounds = rounds + 1 if unchanged else 0
                if rounds >= policy.stable_rounds:
                    stable = True
                    break
    except TimeoutError:
        reason = TIMEOUT
    except (Exception, asyncio.CancelledError) as exc:
        error = exc
    finally:
        if initial is not None:
            try:
                await _restore(_probe(page, "scroll", x=initial["scroll_x"], y=initial["scroll_y"]))
                scroll_restore_status = "restored"
            except (Exception, asyncio.CancelledError) as exc:
                restore_error = exc
                scroll_restore_status = _restoration_status(exc)
    if error is not None or restore_error is not None:
        stable, reason = False, DRIFT
    result = WebFullPageStabilizationResult(
        document_height=sample.get("document_height", 0),
        captured_height=min(sample.get("document_height", 0), policy.max_document_height),
        truncated=sample.get("document_height", 0) > policy.max_document_height,
        scroll_steps=steps,
        sticky_layers=tuple(sample.get("sticky_layers", ())),
        state_hash=_digest(sample.get("state_hash", "")),
        semantic_hash=_digest(sample.get("semantic_hash", "")),
        stable=stable,
        limit_exceeded=reason == LIMIT,
        reason_code=reason,
        wait_elapsed_ms=int((time.monotonic() - started) * 1000),
        restored=scroll_restore_status == "restored",
        scroll_restore_status=scroll_restore_status,
        policy=policy,
    )
    for cancellation in (error, restore_error):
        if isinstance(cancellation, asyncio.CancelledError):
            cancellation.stabilization_result = replace(
                result, reason_code="WEB_FULL_PAGE_SCREENSHOT_CANCELLED"
            )
            raise cancellation
    return result


async def capture_stabilized_screenshot(page, stabilized):
    """仅在有界稳定成功时截图，临时隐藏严格惰性装饰层并验证恢复语义。"""
    if not stabilized.stable:
        raise WebFullPageStabilizationError(stabilized)
    key = "__alioth_full_page_" + uuid4().hex
    before = await asyncio.wait_for(_probe(page), timeout=0.5)
    if _digest(before["state_hash"]) != stabilized.state_hash:
        raise WebFullPageStabilizationError(_failure(stabilized, DRIFT))
    layers = []
    error = None
    restoration_error = None
    verification_error = None
    cancellation = None
    style_restore_status = "not_attempted"
    try:
        layers = await asyncio.wait_for(_probe(page, "hide", key=key), timeout=0.5)
        payload = await asyncio.wait_for(page.screenshot(full_page=True), timeout=10)
    except asyncio.CancelledError as exc:
        cancellation = exc
    except Exception as exc:
        error = exc
    finally:
        try:
            await _restore(_probe(page, "restore", key=key))
            style_restore_status = "restored"
        except asyncio.CancelledError as exc:
            if cancellation is None:
                cancellation = exc
            style_restore_status = _restoration_status(exc)
        except Exception as exc:
            restoration_error = exc
            style_restore_status = _restoration_status(exc)
    after = {}
    try:
        after = await asyncio.wait_for(_probe(page), timeout=0.5)
    except asyncio.CancelledError as exc:
        if cancellation is None:
            cancellation = exc
    except Exception as exc:
        verification_error = exc
    layers = tuple(
        {
            **layer,
            "semantic_hash_before": _digest(before["semantic_hash"]),
            "semantic_hash_after": _digest(after["semantic_hash"]) if after else None,
        }
        for layer in layers
    )
    stabilized = replace(
        stabilized,
        sticky_layers=layers,
        style_restore_status=style_restore_status,
        restored=(
            stabilized.scroll_restore_status == "restored" and style_restore_status == "restored"
        ),
    )
    if cancellation is not None:
        cancellation.stabilization_result = stabilized
        raise cancellation
    if (
        restoration_error is not None
        or verification_error is not None
        or _digest(after.get("state_hash", "")) != stabilized.state_hash
    ):
        raise WebFullPageStabilizationError(_failure(stabilized, DRIFT))
    if error is not None:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        reason = TIMEOUT if isinstance(error, (TimeoutError, PlaywrightTimeoutError)) else DRIFT
        raise WebFullPageStabilizationError(_failure(stabilized, reason)) from error
    audit = stabilized.audit
    return payload, audit


async def capture_stabilized_screenshot_segments(page, stabilized):
    """把已稳定长页采成连续视口 PNG，并返回不含图片正文的分段清单。"""

    if not stabilized.stable:
        raise WebFullPageStabilizationError(stabilized)
    key = "__alioth_full_page_segments_" + uuid4().hex
    before = await asyncio.wait_for(_probe(page), timeout=0.5)
    if _digest(before["state_hash"]) != stabilized.state_hash:
        raise WebFullPageStabilizationError(_failure(stabilized, DRIFT))
    document_height = int(before.get("document_height") or 0)
    captured_height = min(document_height, stabilized.policy.max_document_height)
    viewport_height = int(before.get("viewport_height") or 0)
    if document_height <= 0 or viewport_height <= 0:
        raise WebFullPageStabilizationError(_failure(stabilized, DRIFT))
    last_top = max(0, captured_height - viewport_height)
    positions = list(range(0, last_top + 1, viewport_height))
    if not positions or positions[-1] != last_top:
        positions.append(last_top)
    if len(positions) > stabilized.policy.max_scroll_steps:
        raise WebFullPageStabilizationError(_failure(stabilized, LIMIT))

    layers = []
    payloads: list[bytes] = []
    segments: list[dict] = []
    error = None
    cancellation = None
    restoration_error = None
    verification_error = None
    style_restore_status = "not_attempted"
    segment_scroll_restore_status = "not_attempted"
    try:
        layers = await asyncio.wait_for(_probe(page, "hide", key=key), timeout=0.5)
        for index, top in enumerate(positions):
            await asyncio.wait_for(
                _probe(page, "scroll", x=before.get("scroll_x", 0), y=top),
                timeout=0.5,
            )
            await asyncio.sleep(0.05)
            sample = await asyncio.wait_for(_probe(page), timeout=0.5)
            if _digest(sample.get("state_hash", "")) != stabilized.state_hash:
                raise WebFullPageStabilizationError(_failure(stabilized, DRIFT))
            payload = await asyncio.wait_for(
                page.screenshot(full_page=False, type="png"), timeout=10
            )
            if not isinstance(payload, bytes) or not payload.startswith(b"\x89PNG"):
                raise RuntimeError("分段截图不是有效 PNG")
            actual_top = int(sample.get("scroll_y") or top)
            payloads.append(payload)
            segments.append(
                {
                    "index": index,
                    "top_css_px": actual_top,
                    "bottom_css_px": min(captured_height, actual_top + viewport_height),
                    "viewport_height_css_px": viewport_height,
                    "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
                    "size_bytes": len(payload),
                }
            )
    except asyncio.CancelledError as exc:
        cancellation = exc
    except Exception as exc:  # noqa: BLE001 - 统一在恢复后映射稳定化错误
        error = exc
    finally:
        try:
            await _restore(
                _probe(
                    page,
                    "scroll",
                    x=before.get("scroll_x", 0),
                    y=before.get("scroll_y", 0),
                )
            )
            segment_scroll_restore_status = "restored"
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
            segment_scroll_restore_status = _restoration_status(exc)
        except Exception as exc:  # noqa: BLE001 - 恢复失败必须覆盖成功截图
            restoration_error = exc
            segment_scroll_restore_status = _restoration_status(exc)
        try:
            await _restore(_probe(page, "restore", key=key))
            style_restore_status = "restored"
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
            style_restore_status = _restoration_status(exc)
        except Exception as exc:  # noqa: BLE001 - 恢复失败必须覆盖成功截图
            restoration_error = restoration_error or exc
            style_restore_status = _restoration_status(exc)
    after = {}
    try:
        after = await asyncio.wait_for(_probe(page), timeout=0.5)
    except asyncio.CancelledError as exc:
        cancellation = cancellation or exc
    except Exception as exc:  # noqa: BLE001 - 无法复核状态不能签发分段清单
        verification_error = exc
    layers = tuple(
        {
            **layer,
            "semantic_hash_before": _digest(before["semantic_hash"]),
            "semantic_hash_after": _digest(after["semantic_hash"]) if after else None,
        }
        for layer in layers
    )
    stabilized = replace(
        stabilized,
        sticky_layers=layers,
        style_restore_status=style_restore_status,
        restored=(
            segment_scroll_restore_status == "restored" and style_restore_status == "restored"
        ),
    )
    if cancellation is not None:
        cancellation.stabilization_result = stabilized
        raise cancellation
    if (
        restoration_error is not None
        or verification_error is not None
        or _digest(after.get("state_hash", "")) != stabilized.state_hash
    ):
        raise WebFullPageStabilizationError(_failure(stabilized, DRIFT))
    if error is not None:
        if isinstance(error, WebFullPageStabilizationError):
            raise error
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        reason = TIMEOUT if isinstance(error, (TimeoutError, PlaywrightTimeoutError)) else DRIFT
        raise WebFullPageStabilizationError(_failure(stabilized, reason)) from error
    if not payloads or segments[-1]["bottom_css_px"] < captured_height:
        raise WebFullPageStabilizationError(_failure(stabilized, DRIFT))
    audit = {
        **stabilized.audit,
        "segment_scroll_restore_status": segment_scroll_restore_status,
        "segment_manifest": {
            "protocol_version": "web-full-page-segment-manifest.v1",
            "document_height": document_height,
            "captured_height": captured_height,
            "truncated": captured_height < document_height,
            "viewport_height": viewport_height,
            "segment_count": len(segments),
            "segments": segments,
        },
    }
    return tuple(payloads), audit


def _failure(result, reason):
    return replace(result, stable=False, reason_code=reason)
