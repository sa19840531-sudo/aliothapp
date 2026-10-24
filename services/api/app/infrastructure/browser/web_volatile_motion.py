"""Web 稳定窗口中可忽略的播放器控件与装饰动画。"""

WEB_VOLATILE_MOTION_JS = """
const isVolatileWebMotion = node => {
  const element = node && (node.nodeType === Node.ELEMENT_NODE
    ? node : node.parentElement);
  if (!element || typeof element.closest !== "function") return false;
  if (element.closest(".lottie-root svg,.lottie-wrapper svg")) return true;
  const miniProgress = element.closest(
    "xg-mini-progress-played,.xg-mini-progress-played"
  );
  if (miniProgress && (miniProgress.childElementCount > 0
    || (miniProgress.textContent || "").trim())) return false;
  const player = element.closest(".xgplayer,xgplayer");
  return Boolean(player && player.querySelector("video,audio")
    && (miniProgress || element.closest("xg-progress,.xgplayer-time")));
};
"""
