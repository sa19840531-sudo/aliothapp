"""有界只读帧来源诊断；诊断不能替代视觉命中或业务身份。"""

import asyncio
import base64
import hashlib
from dataclasses import replace
from io import BytesIO

from PIL import Image

_CANVAS_OBSERVER_SCRIPT = """(() => {
  if (globalThis.__aliothNativeCanvasLifecycle) return;
  const records = new WeakMap();
  Object.defineProperty(globalThis,'__aliothNativeCanvasLifecycle',{value:records});
  for (const type of ['contextlost','contextrestored','webglcontextlost','webglcontextrestored']) {
    document.addEventListener(type, event => {
      if (!event.isTrusted || !(event.target instanceof HTMLCanvasElement)) return;
      records.set(event.target,{active_lost:type.endsWith('lost'),type,
        observed_at_ms:performance.now()});
    }, true);
  }
})()"""


async def install_h5_canvas_observer(page):
    """隔离世界只记录原生丢失事件，不初始化画布、不调用站点上下文方法。"""
    session = await page.context.new_cdp_session(page)
    try:
        async with asyncio.timeout(2):
            await session.send("Page.enable")
            await session.send("Page.addScriptToEvaluateOnNewDocument", {
                "source": _CANVAS_OBSERVER_SCRIPT, "worldName": "alioth.h5.frame-origin.v1",
                "runImmediately": True,
            })
        return session
    except BaseException:
        await session.detach()
        raise


async def read_h5_frame_origin(page, *, tracker=None):
    session = tracker.session if tracker is not None else None
    try:
        script = """() => {
          const canvases = [...document.querySelectorAll('canvas')];
          const images = [...document.images];
          const markup = document.body?.outerHTML || '';
          return {url: location.href.slice(0,2000), time_origin_ms: performance.timeOrigin,
            document_content_type: document.contentType,
            ready_state: document.readyState, observed_at_ms: performance.now(),
            viewport: {width:innerWidth,height:innerHeight},
            lifecycle_observer_installed:!!globalThis.__aliothNativeCanvasLifecycle,
            body_markup: markup.slice(0,16000), dom_truncated: markup.length > 16000,
            canvas_count: canvases.length,
            broken_image_count: images.filter(x => x.complete && x.naturalWidth === 0).length,
            canvases: canvases.slice(0,8).map((canvas, index) => {
              const rect = canvas.getBoundingClientRect();
              const result = {width: canvas.width, height: canvas.height,
                rect: {x:rect.x,y:rect.y,width:rect.width,height:rect.height}};
              result.lifecycle=globalThis.__aliothNativeCanvasLifecycle?.get(canvas) || null;
              try {
                // 不调用getContext，避免为尚未初始化的WebGL画布创建错误上下文。
                if (index === 0 && canvas.width > 0 && canvas.height > 0
                    && canvas.width <= 512 && canvas.height <= 512) {
                  const data = canvas.toDataURL('image/png');
                  if (data.length <= 65536) result.sample_png = data;
                  else result.sample_error = 'diagnostic_size_limit';
                }
              } catch (error) {result.sample_error = error.name;}
              return result;
            })};
        }"""
        # 原站可改写主世界的画布编码器；诊断只读隔离世界的原生DOM接口。
        async with asyncio.timeout(1):
            if tracker is None:
                session = await page.context.new_cdp_session(page)
                tree = await session.send("Page.getFrameTree")
                world = await session.send("Page.createIsolatedWorld", {
                    "frameId": tree["frameTree"]["frame"]["id"],
                    "worldName": "alioth.h5.frame-origin.v1",
                })
                context_id = world["executionContextId"]
            else:
                context_id = tracker.execution_context_id
            response = await session.send("Runtime.evaluate", {
                "expression": f"({script})()", "contextId": context_id,
                "returnByValue": True, "timeout": 500,
            })
            value = response.get("result", {}).get("value")
    except Exception as exc:
        return {"status": "unavailable", "reason": type(exc).__name__}
    finally:
        if session is not None and tracker is None:
            try:
                async with asyncio.timeout(1):
                    await session.detach()
            except Exception:
                pass
    if not isinstance(value, dict) or not isinstance(value.get("body_markup"), str):
        return {"status": "unavailable", "reason": "invalid_dom_diagnostic"}
    markup = value.pop("body_markup")
    value["body_markup_sha256"] = hashlib.sha256(markup.encode("utf-8")).hexdigest()
    value["status"] = "observed"
    value["atomic_frame_binding"] = False
    for canvas in value.get("canvases", []):
        png = canvas.pop("sample_png", None)
        if isinstance(png, str) and png.startswith("data:image/png;base64,"):
            try:
                raw = base64.b64decode(png.split(",", 1)[1], validate=True)
                canvas["sample_png_sha256"] = hashlib.sha256(raw).hexdigest()
                with Image.open(BytesIO(raw)) as im:
                    canvas["sample_rgba"] = list(im.convert("RGBA").getpixel((0, 0)))
            except Exception as exc:
                canvas["sample_error"] = type(exc).__name__
    return value


async def capture_h5_frame(page, *, full_page):
    """围住实际截图读取来源；顺序观测不冒充原子绑定或加载就绪。"""
    before = await read_h5_frame_origin(page)
    png = await page.screenshot(full_page=full_page)
    after = await read_h5_frame_origin(page)
    keys = ("url", "time_origin_ms", "body_markup_sha256", "canvas_count", "canvases")
    status = "unavailable"
    if before.get("status") == after.get("status") == "observed":
        status = ("bracketed" if all(before.get(k) == after.get(k) for k in keys)
                  else "source_changed")
        if any(_dominant_canvas_lost(origin) for origin in (before, after)):
            status = "renderer_unavailable"
    return png, {
        "status": status, "atomic_frame_binding": False,
        "scope": "main_document_light_dom", "full_page": full_page,
        "screenshot_sha256": hashlib.sha256(png).hexdigest(),
        "before": before, "after": after,
    }


def _dominant_canvas_lost(origin):
    viewport = origin.get("viewport", {})
    width, height = viewport.get("width", 0), viewport.get("height", 0)
    if width <= 0 or height <= 0:
        return False
    for canvas in origin.get("canvases", []):
        if not (canvas.get("lifecycle") or {}).get("active_lost"):
            continue
        rect = canvas["rect"]
        visible_width = max(0, min(width, rect["x"] + rect["width"]) - max(0, rect["x"]))
        visible_height = max(0, min(height, rect["y"] + rect["height"]) - max(0, rect["y"]))
        if visible_width * visible_height >= width * height / 2:
            return True
    return False


def annotate_h5_frame_origins(result):
    origins = [
        {"screenshot_index": index, "purpose": artifact.content.get("purpose", "primary"),
         "frame_origin": artifact.content["frame_origin"]}
        for index, artifact in enumerate(
            (a for a in result.artifacts if a.artifact_type == "screenshot"), start=1,
        )
        if isinstance(artifact.content, dict)
        and isinstance(artifact.content.get("frame_origin"), dict)
    ]
    # 图片存储会解码base64，来源元数据另落采集metrics，按索引和PNG摘要绑定。
    result.metrics["h5_screenshot_origins"] = origins
    if not any(item["frame_origin"]["status"] == "renderer_unavailable" for item in origins):
        return result
    code = "H5_CANVAS_CONTEXT_LOST"
    return replace(
        result, execution_status="failed",
        error_message=f"{code}：原生主画布上下文丢失，帧不可验收",
        artifacts=[
            replace(a, content={**a.content, "capture_degraded": code})
            if a.artifact_type == "ui_snapshot" and isinstance(a.content, dict) else a
            for a in result.artifacts
        ],
        post_action_snapshot=(
            {**result.post_action_snapshot, "capture_degraded": code}
            if result.post_action_snapshot is not None else None
        ),
        metrics={**result.metrics, "browser_rendering_error": code},
    )
