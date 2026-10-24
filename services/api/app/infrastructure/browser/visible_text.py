"""浏览器视觉可见文本采集：只保留当前视口内未被上层元素遮挡的文本。"""

from __future__ import annotations

from typing import Any

MAX_VISIBLE_TEXT_NODES = 5_000
MAX_VISIBLE_TEXT_CHARACTERS = 100_000

_OCCLUSION_AWARE_VISIBLE_TEXT_SCRIPT = f"""
(webCapture = false) => {{
  const maximumNodes = {MAX_VISIBLE_TEXT_NODES};
  const maximumCharacters = {MAX_VISIBLE_TEXT_CHARACTERS};
  const root = document.body || document.documentElement;
  const viewportWidth = Math.max(
    0,
    window.innerWidth || document.documentElement?.clientWidth || 0,
  );
  const viewportHeight = Math.max(
    0,
    window.innerHeight || document.documentElement?.clientHeight || 0,
  );
  if (!root || viewportWidth <= 0 || viewportHeight <= 0) return "";

  const normalizeText = (value) => String(value || "").replace(/\\s+/g, " ").trim();
  const excludedTags = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE"]);
  // Web 站点可能覆盖全局 Element；这里只接受真实 DOM 查询返回的元素节点。
  const isElement = (element) => webCapture
    ? element?.nodeType === 1
    : element instanceof Element;

  const numericOpacity = (value) => {{
    const parsed = Number.parseFloat(String(value || "1"));
    return Number.isFinite(parsed) ? parsed : 1;
  }};

  const colorAlpha = (value) => {{
    const normalized = String(value || "").trim().toLowerCase();
    if (!normalized || normalized === "transparent") return 0;
    const rgba = normalized.match(
      /^rgba?\\([^/)]*(?:\\/\\s*([\\d.]+%?))?\\s*\\)$/,
    );
    if (!rgba) return 1;
    if (rgba[1]) {{
      return rgba[1].endsWith("%")
        ? Number.parseFloat(rgba[1]) / 100
        : Number.parseFloat(rgba[1]);
    }}
    const commaParts = normalized.slice(
      normalized.indexOf("(") + 1,
      normalized.lastIndexOf(")"),
    ).split(",");
    if (commaParts.length === 4) {{
      const parsed = Number.parseFloat(commaParts[3]);
      return Number.isFinite(parsed) ? parsed : 1;
    }}
    return 1;
  }};

  const isStyleVisible = (element) => {{
    if (!isElement(element) || excludedTags.has(element.tagName)) return false;
    for (let current = element; current; current = current.parentElement) {{
      const style = window.getComputedStyle(current);
      if (
        style.display === "none"
        || style.visibility === "hidden"
        || style.visibility === "collapse"
        || style.contentVisibility === "hidden"
        || numericOpacity(style.opacity) <= 0.01
        || current.hidden
        || current.getAttribute("aria-hidden") === "true"
        || current.hasAttribute("inert")
      ) {{
        return false;
      }}
    }}
    return true;
  }};

  const intersectsViewport = (rect) => (
    rect.width > 0
    && rect.height > 0
    && rect.right > 0
    && rect.bottom > 0
    && rect.left < viewportWidth
    && rect.top < viewportHeight
  );

  const pseudoElementPaints = (element, pseudo) => {{
    const style = window.getComputedStyle(element, pseudo);
    const content = String(style.content || "").trim();
    if (!content || content === "none" || content === "normal") return false;
    return (
      numericOpacity(style.opacity) > 0.01
      && (
        colorAlpha(style.backgroundColor) > 0.01
        || style.backgroundImage !== "none"
        || content !== '""'
      )
    );
  }};

  const visuallyPaints = (element) => {{
    if (!isElement(element)) return false;
    const style = window.getComputedStyle(element);
    if (
      style.display === "none"
      || style.visibility === "hidden"
      || numericOpacity(style.opacity) <= 0.01
    ) {{
      return false;
    }}
    const tag = element.tagName;
    if (
      tag === "IMG"
      || tag === "VIDEO"
      || tag === "CANVAS"
      || tag === "IFRAME"
      || (typeof SVGElement !== "undefined" && element instanceof SVGElement)
    ) {{
      return true;
    }}
    if (
      colorAlpha(style.backgroundColor) > 0.01
      || style.backgroundImage !== "none"
      || pseudoElementPaints(element, "::before")
      || pseudoElementPaints(element, "::after")
    ) {{
      return true;
    }}
    return element.childElementCount === 0 && normalizeText(element.textContent).length > 0;
  }};

  const sameOrAncestor = (candidate, owner) => (
    candidate === owner || candidate.contains(owner)
  );

  const isPointExposed = (owner, x, y) => {{
    const stack = typeof document.elementsFromPoint === "function"
      ? document.elementsFromPoint(x, y)
      : [document.elementFromPoint(x, y)].filter(Boolean);
    const ownerIndex = stack.findIndex((candidate) => sameOrAncestor(candidate, owner));
    if (ownerIndex < 0) return false;
    for (let index = 0; index < ownerIndex; index += 1) {{
      const candidate = stack[index];
      if (sameOrAncestor(candidate, owner)) continue;
      if (visuallyPaints(candidate)) return false;
    }}
    return true;
  }};

  const samplePoints = (rect) => {{
    const left = Math.max(0, rect.left);
    const right = Math.min(viewportWidth, rect.right);
    const top = Math.max(0, rect.top);
    const bottom = Math.min(viewportHeight, rect.bottom);
    const width = right - left;
    const height = bottom - top;
    if (width <= 0 || height <= 0) return [];
    const insetX = Math.min(2, width / 4);
    const insetY = Math.min(2, height / 4);
    return [
      [(left + right) / 2, (top + bottom) / 2],
      [left + insetX, (top + bottom) / 2],
      [right - insetX, (top + bottom) / 2],
      [(left + right) / 2, top + insetY],
      [(left + right) / 2, bottom - insetY],
    ];
  }};

  const isTextNodeExposed = (node, owner) => {{
    const range = document.createRange();
    range.selectNodeContents(node);
    const rects = Array.from(range.getClientRects()).filter(intersectsViewport);
    if (typeof range.detach === "function") range.detach();
    return rects.some((rect) => (
      samplePoints(rect).some(([x, y]) => isPointExposed(owner, x, y))
    ));
  }};

  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const chunks = [];
  let characterCount = 0;
  let visitedNodes = 0;
  while (visitedNodes < maximumNodes) {{
    const node = walker.nextNode();
    if (!node) break;
    visitedNodes += 1;
    const text = normalizeText(node.nodeValue);
    const owner = node.parentElement;
    if (!text || !owner || !isStyleVisible(owner) || !isTextNodeExposed(node, owner)) {{
      continue;
    }}
    const remaining = maximumCharacters - characterCount;
    if (remaining <= 0) break;
    const bounded = text.slice(0, remaining);
    chunks.push(bounded);
    characterCount += bounded.length + 1;
  }}
  return chunks.join("\\n").slice(0, maximumCharacters);
}}
"""


async def read_visible_text(page: Any, *, web_capture: bool = False) -> str:
    """Web 显式启用抗全局构造器覆盖；默认采集与几何不可用回退保持原样。"""

    evaluate = getattr(page, "evaluate", None)
    if callable(evaluate):
        try:
            result = (
                await evaluate(_OCCLUSION_AWARE_VISIBLE_TEXT_SCRIPT, True)
                if web_capture
                else await evaluate(_OCCLUSION_AWARE_VISIBLE_TEXT_SCRIPT)
            )
        except Exception:  # noqa: BLE001 - 页面跳转时执行上下文可能瞬时失效
            result = None
        if isinstance(result, str):
            # 空字符串是可信的负证据，不能回退 body.innerText 重新引入被遮挡内容。
            return result[:MAX_VISIBLE_TEXT_CHARACTERS]

    inner_text = getattr(page, "inner_text", None)
    if not callable(inner_text):
        return ""
    try:
        result = await inner_text("body")
    except Exception:  # noqa: BLE001
        return ""
    return str(result or "")[:MAX_VISIBLE_TEXT_CHARACTERS]
