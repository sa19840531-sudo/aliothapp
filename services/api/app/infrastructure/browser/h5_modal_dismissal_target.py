"""验签后的 H5 弹窗只允许点击其外侧真实遮罩，失败再使用关闭控件。"""

from typing import Any

from services.api.app.infrastructure.browser.h5_local_media_digest import (
    H5_LOCAL_MEDIA_DIGEST_JS,
    ensure_h5_local_media_digest,
)

MODAL_LAYER_SHAPE_JS = H5_LOCAL_MEDIA_DIGEST_JS + """
    const modalLayerShape = (root) => {
      const nodes = [root, ...root.querySelectorAll('*')]
        .filter(node => !node.matches('script,style,template'));
      // 沿用 H5 弹窗普查的 4000 个后代边界，不另设更小的关闭范围。
      if (nodes.length > 4001) return null;
      const indices = new Map(nodes.map((node, index) => [node, index]));
      return JSON.stringify(nodes.map(node => [
        node.localName, node.getAttribute('role') || '',
        node.getAttribute('type') || '',
        node === root ? -1 : indices.get(node.parentElement),
      ]));
    };
    const modalLayerMedia = async (root) => {
      const identities = [];
      for (const image of [...root.querySelectorAll('img')].slice(0, 4)) {
        const src = image.currentSrc || image.src;
        if (!src) continue;
        if (!src.startsWith('blob:') && !src.startsWith('data:')) {
          identities.push(src); continue;
        }
        // Blob 与 data 内容不可变，只缓存成功摘要，避免每 100ms 重新读取媒体。
        const cache = window.__aliothH5ModalMediaDigests ||= new Map();
        if (cache.has(src)) { identities.push(cache.get(src)); continue; }
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 500);
        try {
          const response = await fetch(src, {signal:controller.signal});
          if (!response.ok) return null;
          const blob = await response.blob();
          // 与弹窗普查沿用同一资源大小边界，不下载远端图片。
          if (blob.size > 5 * 1024 * 1024) return null;
          const digest = await h5LocalMediaDigest(await blob.arrayBuffer());
          cache.set(src, digest);
          while (cache.size > 64) cache.delete(cache.keys().next().value);
          identities.push(digest);
        } catch { return null; }
        finally { clearTimeout(timer); }
      }
      return identities.length ? JSON.stringify(identities) : null;
    };
    const replacedModalMedia = async (replacement, nodeToken, layerMedia) => {
      if (!layerMedia || !window.__aliothSignedModalCloseClicks?.has(nodeToken)) return false;
      const media = await modalLayerMedia(replacement);
      return Boolean(media && media !== layerMedia);
    };
    const modalLayerAdvanced = async (original, replacement, nodeToken, layerMedia) => {
      if (!original || !replacement
        || !window.__aliothSignedModalCloseClicks?.has(nodeToken)) return false;
      if (window.__aliothHiddenModalNodes?.has(nodeToken)) return true;
      return !original.isConnected
        && await replacedModalMedia(replacement, nodeToken, layerMedia);
    };
"""


async def bind_modal_layer(page: Any, target: dict) -> dict | None:
    """绑定原节点与有限结构，阻止重绘和动态文案冒充下一层弹窗。"""
    await ensure_h5_local_media_digest(page)
    binding = await page.evaluate(
        "async ({selector, closeSelector, siblingClose}) => {" + MODAL_LAYER_SHAPE_JS + """
          const node = document.querySelector(selector);
          if (!node) return null;
          const shape = modalLayerShape(node);
          if (!shape) return null;
          const nodes = window.__aliothSignedModalNodes ||= new Map();
          const clicks = window.__aliothSignedModalCloseClicks ||= new Set();
          const token = String((window.__aliothSignedModalSequence ||= 0) + 1);
          window.__aliothSignedModalSequence = Number(token);
          nodes.set(token, node);
          while (nodes.size > 64) {
            const oldest = nodes.keys().next().value;
            nodes.delete(oldest); clicks.delete(oldest);
            window.__aliothAdvancedModalNodes?.delete(oldest);
            window.__aliothHiddenModalNodes?.delete(oldest);
            window.__aliothModalObservers?.get(oldest)?.disconnect();
            window.__aliothModalObservers?.delete(oldest);
          }
          let control;
          try { control = document.querySelector(closeSelector); } catch {}
          if (control && (node.contains(control)
            || (siblingClose && node.parentElement?.contains(control)))) {
            // 记录可信关闭之后实际隐藏的生命周期；素材变化本身不构成关闭。
            const observer = new MutationObserver(() => {
              if (!clicks.has(token) || !node.isConnected) return;
              const style = getComputedStyle(node);
              if (style.display === 'none' || style.visibility === 'hidden'
                || Number(style.opacity || 1) <= 0 || !node.getClientRects().length) {
                (window.__aliothHiddenModalNodes ||= new Set()).add(token);
              }
            });
            // 弹层可复用原节点，只隐藏父容器；沿既有八层范围记录真实隐藏，素材变化不作证明。
            for (let scope = node, depth = 0; scope && depth < 8;
              scope = scope.parentElement, depth += 1) {
              observer.observe(scope, {
                attributes:true, attributeFilter:['style','class','hidden'],
              });
              if (scope === document.body) break;
            }
            (window.__aliothModalObservers ||= new Map()).set(token, observer);
            control.addEventListener('click', event => {
              if (event.isTrusted) clicks.add(token);
            }, {once:true, capture:true});
          }
          return {token, shape, media:await modalLayerMedia(node)};
        }
    """, {"selector": target["dialog_selector"], "closeSelector": target.get("selector"),
          "siblingClose": target.get("sibling_close_control", False)})
    if not isinstance(binding, dict):
        return None
    return {**target, "dialog_node_token": binding["token"],
            "dialog_layer_shape": binding["shape"],
            "dialog_layer_media": binding.get("media"), "require_hidden": True}


async def signed_modal_target(page: Any, proof: Any, *, prefer_safe_outside: bool) -> dict | None:
    close_target = {
        "dialog_present": True,
        "selector": proof.selector,
        "kind": "close_control",
        "dialog_selector": proof.modal_selector,
        "dialog_signature": proof.obstruction_signature,
        "verification_required": True,
        "require_hidden": True,
        "pointer_click_required": True,
        "sibling_close_control": getattr(proof, "sibling_close_control", False),
    }
    close_target = await bind_modal_layer(page, close_target)
    if close_target is None:
        return None
    outside_position = getattr(proof, "outside_position", None)
    if outside_position is not None:
        return {
            **close_target, "kind": "overlay",
            "position": {"x": outside_position[0], "y": outside_position[1]},
        }
    if not prefer_safe_outside:
        return close_target
    # 只考虑已验签弹窗的父层和同级遮罩，不搜索任意页面空白或广告点击区域。
    outside = await page.evaluate("""
        ({modalSelector}) => {
          const modal = document.querySelector(modalSelector);
          if (!modal) return null;
          const panel = modal.getBoundingClientRect();
          const candidates = [];
          for (let node = modal, depth = 0; node && node !== document.body && depth < 8;
            node = node.parentElement, depth += 1) {
            candidates.push(node);
            if (node.parentElement) candidates.push(...node.parentElement.children);
          }
          for (const layer of new Set(candidates)) {
            const style = getComputedStyle(layer), rect = layer.getBoundingClientRect();
            if (!['fixed','absolute'].includes(style.position) || style.display === 'none'
              || style.visibility === 'hidden' || Number(style.opacity) <= 0
              || rect.width * rect.height < innerWidth * innerHeight * .8
              || layer.matches('a,button,input,form,[role="link"],[href],[data-href],'
                + '[data-url],[data-route],[onclick*="location"],[onclick*="open("]')) continue;
            const points = [];
            for (const yr of [.05,.5,.95]) for (const xr of [.05,.5,.95]) {
              const x = Math.max(0, rect.left) + (Math.min(innerWidth, rect.right)
                - Math.max(0, rect.left)) * xr;
              const y = Math.max(0, rect.top) + (Math.min(innerHeight, rect.bottom)
                - Math.max(0, rect.top)) * yr;
              if (x >= panel.left && x <= panel.right
                && y >= panel.top && y <= panel.bottom) continue;
              if (document.elementFromPoint(x, y) !== layer) continue;
              points.push({x:x-rect.left,y:y-rect.top});
            }
            if (!points.length) continue;
            const path = [];
            for (let node = layer; node && node !== document.body; node = node.parentElement) {
              const siblings = [...node.parentElement.children]
                .filter(x => x.tagName === node.tagName);
              path.unshift(`${node.localName}:nth-of-type(${siblings.indexOf(node)+1})`);
            }
            return {selector:'body > '+path.join(' > '), positions:points.slice(0,2)};
          }
          return null;
        }
    """, {"modalSelector": proof.modal_selector})
    if not isinstance(outside, dict):
        return close_target
    return {
        **close_target,
        "selector": outside["selector"],
        "kind": "overlay",
        "position": outside["positions"][0],
        "positions": outside["positions"],
        "fallback_close_target": close_target,
    }


async def sample_modal_close_geometry(page: Any, target: dict) -> dict | None:
    """只读采样关闭控件热区；命中事实不等同于各采样点已执行关闭。"""
    return await page.evaluate("""selector => {
        let nodes; try { nodes=document.querySelectorAll(selector); } catch { return null; }
        if (nodes.length !== 1) return null;
        const node=nodes[0], rect=node.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) return null;
        const samples=[];
        for (const yr of [.15,.5,.85]) for (const xr of [.15,.5,.85]) {
            const x=rect.left+rect.width*xr, y=rect.top+rect.height*yr;
            const within=x>=0 && y>=0 && x<innerWidth && y<innerHeight;
            const hit=within ? document.elementFromPoint(x,y) : null;
            samples.push({x,y,within_viewport:within,
                hits_close_control:Boolean(hit && (hit===node || node.contains(hit)))});
        }
        return {schema_version:'h5-close-hit-geometry.v1', read_only:true,
            all_points_clicked:false,
            rect:{x:rect.x,y:rect.y,width:rect.width,height:rect.height}, samples};
    }""", target.get("selector"))
