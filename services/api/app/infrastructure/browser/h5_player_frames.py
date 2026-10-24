"""隔离世界记录原生呈现帧和显式控件归属，不读取播放器私有对象。"""

import asyncio
import hashlib
import json
import math
from copy import deepcopy
from io import BytesIO

from PIL import Image

_SCRIPT = r"""(async selectors => {
  const ledger = globalThis.__aliothPresentedVideoV1 ||= {
    records:new WeakMap(), streams:new WeakMap(), next:1, nextStream:1
  };
  const resource = video => {
    if (video.srcObject) {
      if (!ledger.streams.has(video.srcObject)) {
        ledger.streams.set(video.srcObject,ledger.nextStream++);
      }
      return 'stream:'+ledger.streams.get(video.srcObject);
    }
    return video.currentSrc;
  };
  const result=[], firstFrameDeadline=performance.now()+200;
  for (const selector of selectors) {
    let nodes; try {nodes=document.querySelectorAll(selector);} catch {continue;}
    if(nodes.length!==1 || !(nodes[0] instanceof HTMLVideoElement)) continue;
    const video=nodes[0];
    let record=ledger.records.get(video);
    if(!record) {
      record={id:ledger.next++,generation:1,last:null};ledger.records.set(video,record);
      video.addEventListener('emptied',()=>{record.generation++;record.last=null;});
      if(typeof video.requestVideoFrameCallback==='function') {
        const observe=(now,frame)=>{
          record.last={status:'presented',media_time:frame.mediaTime,
            presented_frames:frame.presentedFrames,width:frame.width,height:frame.height,
            resource_key:resource(video),resource_generation:record.generation};
          if(video.isConnected) video.requestVideoFrameCallback(observe);
        };
        video.requestVideoFrameCallback(observe);
      }
    }
    if(!record.last && !video.paused && video.readyState>=2
      && typeof video.requestVideoFrameCallback==='function') {
      const remaining=Math.max(0,firstFrameDeadline-performance.now());
      if(remaining>0) await new Promise(resolve=>{
        const callback=video.requestVideoFrameCallback(()=>{clearTimeout(timer);resolve();});
        const timer=setTimeout(()=>{video.cancelVideoFrameCallback(callback);resolve();},remaining);
      });
    }
    const id=`${performance.timeOrigin}:isolated-video:${record.id}:${record.generation}`;
    const rect=video.getBoundingClientRect(), style=getComputedStyle(video);
    const core=[...document.querySelectorAll(
      "main,[role='main'],#app,#root,.app,.page,.content,.van-tabbar"
    )].find(node=>node.getClientRects().length)||document.body;
    const business=core.contains(video) && !video.closest(
      'header,nav,footer,aside,[role="navigation"],[role="dialog"],'
      +'[aria-modal="true"],.ad,[data-ad],.loader,.loading,.van-loading,.spinner,.skeleton');
    const controlSelector='button[aria-controls],[role="button"][aria-controls]';
    const controls=[...document.querySelectorAll(controlSelector)]
      .filter(node=>video.id && document.querySelectorAll('#'+CSS.escape(video.id)).length===1
        && node.getAttribute('aria-controls').split(/\s+/).includes(video.id)).slice(0,32);
    const fullscreen=document.fullscreenElement;
    const boundFullscreen=fullscreen===video || (fullscreen && fullscreen!==document.body
      && fullscreen!==document.documentElement && fullscreen.contains(video)
      && fullscreen.querySelectorAll('video').length===1
      && rect.width*rect.height>=innerWidth*innerHeight*.5
      && controls.some(x=>fullscreen.contains(x)));
    let clear=true;
    for(const x of [.2,.5,.8]) for(const y of [.2,.5,.8]) {
      const px=rect.x+rect.width*x,py=rect.y+rect.height*y;
      if(px<0||py<0||px>=innerWidth||py>=innerHeight) continue;
      const hit=document.elementFromPoint(px,py);
      if(hit!==video && !video.contains(hit)
        && !controls.some(control=>control===hit || control.contains(hit))) clear=false;
    }
    const currentResource=resource(video);
    let frame=null;
    if(record.last && record.last.resource_key===currentResource) {
      const {resource_key,...facts}=record.last;
      frame={...facts,media_instance_id:id};
    }
    result.push({selector,media_instance_id:id,frame,url:location.href,
      media:{paused:video.paused,current_time:video.currentTime,ready_state:video.readyState,
        seeking:video.seeking,video_width:video.videoWidth,video_height:video.videoHeight},
      time_origin_ms:performance.timeOrigin,
      visible:style.visibility==='visible' && style.display!=='none'
        && Number(style.opacity)>0 && rect.width>0 && rect.height>0,
      clear,rect:{x:rect.x,y:rect.y,width:rect.width,height:rect.height},
      business_surface:business,
      custom_control_count:controls.length,
      fullscreen_media_instance_id:boundFullscreen?id:null,
      fullscreen_surface_kind:boundFullscreen?(fullscreen===video?'native_video':'custom_wrapper'):null});
  }
  return result;
})"""


async def attach_player_frames(page, snapshot, *, max_snapshot_bytes=512 * 1024):
    """仅 H5 调用；没有 CDP 或可靠原生呈现事实时保持未知。"""
    videos = [item for item in snapshot.get("elements", ()) if item.get("tag") == "video"]
    selectors = [item.get("selector") for item in videos]
    if not videos or any(not isinstance(key, str) for key in selectors) or len(videos) > 16:
        return snapshot
    original = deepcopy(snapshot)
    session = None
    try:
        async with asyncio.timeout(2):
            session = await page.context.new_cdp_session(page)
            tree = await session.send("Page.getFrameTree")
            world = await session.send(
                "Page.createIsolatedWorld",
                {
                    "frameId": tree["frameTree"]["frame"]["id"],
                    "worldName": "alioth.h5.player-frame.v1",
                },
            )
            response = await session.send(
                "Runtime.evaluate",
                {
                    "expression": f"{_SCRIPT}({json.dumps(selectors)})",
                    "contextId": world["executionContextId"],
                    "returnByValue": True,
                    "awaitPromise": True,
                    "timeout": 500,
                },
            )
            values = response.get("result", {}).get("value")
        if not isinstance(values, list) or len(values) != len(videos):
            return snapshot
        for video, value in zip(videos, values, strict=True):
            if (
                value.get("selector") != video["selector"]
                or value.get("url") != snapshot.get("url")
                or value.get("time_origin_ms")
                != (snapshot.get("document") or {}).get("time_origin_ms")
            ):
                continue
            video["h5_media_instance_id"] = value["media_instance_id"]
            video["h5_video_frame"] = value.get("frame")
            # 与呈现帧同次读取，不能沿用等待之前的播放时间或解码状态。
            if isinstance(value.get("media"), dict):
                video["media"] = {**(video.get("media") or {}), **value["media"]}
            video["h5_player_presentation"] = {
                "visible": value["visible"],
                "clear": value["clear"],
                "rect": value["rect"],
                "custom_control_count": value["custom_control_count"],
                "business_surface": value.get("business_surface"),
            }
            if value.get("fullscreen_media_instance_id"):
                snapshot.setdefault("document_state", {}).update(
                    {
                        key: value[key]
                        for key in ("fullscreen_media_instance_id", "fullscreen_surface_kind")
                    }
                )
    except Exception:
        # 不影响普通采集；依赖稳定后加载帧的合同明确缺证。
        pass
    finally:
        if session is not None:
            try:
                async with asyncio.timeout(1):
                    await session.detach()
            except Exception:
                pass
    if len(json.dumps(snapshot, ensure_ascii=False).encode("utf-8")) > max_snapshot_bytes:
        return original
    return snapshot


def business_videos_in_view(snapshot):
    """沿用正文媒体边界，屏外/隐藏/明确广告不占用普通视频采样。"""
    viewport = snapshot.get("viewport") or {}
    selected = []
    for video in snapshot.get("elements", ()):
        if not isinstance(video, dict) or video.get("tag") != "video":
            continue
        display = video.get("h5_player_presentation") or {}
        rect = video.get("rect") or video.get("bounds") or {}
        values = [rect.get(key) for key in ("x", "y", "width", "height")]
        values += [viewport.get(key) for key in ("width", "height")]
        if (
            video.get("visible") is not True
            or display.get("visible") is False
            or display.get("business_surface") is False
            or any(type(value) not in (int, float) or not math.isfinite(value) for value in values)
        ):
            continue
        x, y, width, height, vw, vh = values
        if (
            width > 0
            and height > 0
            and vw > 0
            and vh > 0
            and min(vw, x + width) > max(0, x)
            and min(vh, y + height) > max(0, y)
        ):
            selected.append(video)
    return selected


def video_region_digest(png, snapshot):
    """摘要来自实际 PNG 的视频视口区域，不用几何或自报图片摘要代替像素。"""
    videos = [item for item in snapshot.get("elements", ()) if item.get("tag") == "video"]
    if len(videos) != 1:
        return None
    bounds = videos[0].get("rect") or videos[0].get("bounds", {})
    viewport = snapshot.get("viewport") or {}
    values = [bounds.get(k) for k in ("x", "y", "width", "height")]
    values.extend(viewport.get(k) for k in ("width", "height"))
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in values):
        return None
    x, y, w, h, vw, vh = values
    if min(w, h, vw, vh) <= 0:
        return None
    try:
        with Image.open(BytesIO(png)) as im:
            if im.width * im.height > 16_000_000:
                return None
            box = (
                max(0, round(x * im.width / vw)),
                max(0, round(y * im.height / vh)),
                min(im.width, round((x + w) * im.width / vw)),
                min(im.height, round((y + h) * im.height / vh)),
            )
            if box[2] <= box[0] or box[3] <= box[1]:
                return None
            region = im.crop(box).convert("RGB")
            return hashlib.sha256(str(region.size).encode() + region.tobytes()).hexdigest()
    except (OSError, ValueError):
        return None
