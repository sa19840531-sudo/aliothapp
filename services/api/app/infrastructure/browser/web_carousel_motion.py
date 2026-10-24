"""Web 采证窗口暂停与恢复组件自动轮播，不改动页型或结构一致性门槛。"""

PAUSE_WEB_CAROUSELS = r"""() => {
  const key = Symbol.for('alioth.web.carousel.motion.v1');
  if (window[key]) return {owl_paused:window[key].paused.length,
    owl_motion_guarded:window[key].movements.length};
  const paused = [];
  const movements = [];
  const jq = window.jQuery || window.Zepto;
  if (typeof jq === 'function') {
    for (const element of [...document.querySelectorAll('.owl-carousel')].slice(0,64)) {
      try {
        const wrapped = jq(element), owl = wrapped.data('owl.carousel');
        if (!owl) continue;
        const autoplay = owl._plugins && owl._plugins.autoplay;
        if (typeof owl.is === 'function' && owl.is('rotating')
            && autoplay && autoplay._paused !== true) {
          wrapped.trigger('stop.owl.autoplay');
          paused.push({element,owl});
        }
        // 外部定时器也可调用轮播 API；采证期间仅暂停组件移动，不停全局 JavaScript。
        for (const name of ['next','prev','to']) {
          if (typeof owl[name] !== 'function') continue;
          const original = owl[name], own = Object.hasOwn(owl,name);
          const entry = {element,owl,name,original,own,suppressed:0};
          const guard = function() { entry.suppressed++; };
          entry.guard = guard;
          owl[name] = guard;
          movements.push(entry);
        }
      } catch (_) {}
    }
  }
  window[key] = {paused,movements};
  return {owl_paused:paused.length,owl_motion_guarded:movements.length};
}"""

RESTORE_WEB_CAROUSELS = r"""() => {
  const key = Symbol.for('alioth.web.carousel.motion.v1');
  const state = window[key] || {paused:[],movements:[]};
  const paused = state.paused;
  const jq = window.jQuery || window.Zepto;
  let restored = 0;
  let suppressed = 0;
  for (const item of state.movements) {
    suppressed += item.suppressed;
    if (item.owl[item.name] !== item.guard) continue;
    if (item.own) item.owl[item.name] = item.original;
    else delete item.owl[item.name];
  }
  for (const item of paused) {
    try {
      if (typeof jq === 'function' && item.element.isConnected
          && jq(item.element).data('owl.carousel') === item.owl) {
        jq(item.element).trigger('play.owl.autoplay');
        restored++;
      }
    } catch (_) {}
  }
  delete window[key];
  return {owl_restored:restored,owl_motion_restored:state.movements.length,
    owl_motion_suppressed:suppressed};
}"""


async def pause_web_carousels(page):
    try:
        result = await page.evaluate(PAUSE_WEB_CAROUSELS)
        return result if isinstance(result,dict) else {}
    except Exception:
        return {'owl_pause_unavailable':True}


async def restore_web_carousels(page):
    try:
        result = await page.evaluate(RESTORE_WEB_CAROUSELS)
        return result if isinstance(result,dict) else {}
    except Exception:
        return {'owl_restore_unavailable':True}
