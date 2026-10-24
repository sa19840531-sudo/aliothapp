"""H5 原生主体浅层结构普查，不采正文、不触发浏览器动作。"""

H5_NATIVE_MAIN_SHELL_SCRIPT = r"""(() => {
  const nativeRoots = [...document.querySelectorAll('main,[role="main"]')];
  const scrollRoots = h5ScrollRegions.map(region => document.querySelector(region.selector))
    .filter(element => element && element.matches('div,section')
      && !nativeRoots.some(root => root.contains(element)));
  const appRoot = document.querySelector('#app,[data-v-app],#root,[data-reactroot]');
  const pageRoots = appRoot ? [...appRoot.querySelectorAll('[class~="main"]')]
    .filter(element => element.matches('div,section')
      && !nativeRoots.some(root => root.contains(element))) : [];
  const roots = [...new Set([...nativeRoots, ...scrollRoots, ...pageRoots])];
  let complete = document.readyState === 'complete'
    && document.querySelectorAll('*').length <= 4000 && roots.length <= 4;
  const facts = element => ({
    tag: element.tagName.toLowerCase(),
    role: String(element.getAttribute('role') || '').toLowerCase(),
    class_tokens: [...element.classList].slice(0, 32),
  });
  const shown = element => visible(element, getComputedStyle(element),
    element.getBoundingClientRect());
  const blockedContext = element => {
    for (let current = element; current; current = current.parentElement) {
      if (current.matches('dialog,[role="dialog"],[aria-modal="true"],article')
        || [...current.classList].some(token =>
          /(?:^|[-_])(?:card|item|carousel|banner|advert|advertisement|ad|dialog|modal|overlay|popup|toast|countdown|count-down)(?:$|[-_])/i.test(token))
        || h5ModalLayerSet.has(current)) return true;
    }
    return false;
  };
  const hit = element => {
    const rect = element.getBoundingClientRect();
    const x = Math.max(0, Math.min(innerWidth - 1, rect.left + rect.width / 2));
    const y = Math.max(0, Math.min(innerHeight - 1, rect.top + rect.height / 2));
    const target = document.elementFromPoint(x, y);
    return Boolean(target && (target === element || element.contains(target)));
  };
  const result = [];
  for (const root of roots.slice(0, 4)) {
    if (!shown(root)) continue;
    if (root.closest('[role="dialog"],[aria-modal="true"]') || root.shadowRoot
      || [...h5ModalLayerSet].some(layer => layer.contains(root))) {
      complete = false;
      continue;
    }
    const native = nativeRoots.includes(root);
    const bounds = root.getBoundingClientRect();
    if (!native && (blockedContext(root) || !hit(root)
      || bounds.width < innerWidth * .6 || bounds.height < innerHeight * .35)) continue;
    const children = [...root.children];
    if (root.classList.length > 32 || children.length > 32) complete = false;
    const rows = [];
    for (const child of children.slice(0, 32)) {
      if (!shown(child) || ['script', 'style', 'template'].includes(child.localName)) continue;
      if (child.shadowRoot || child.classList.length > 32) complete = false;
      if (h5ModalLayerSet.has(child)) continue;
      rows.push(facts(child));
    }
    result.push({...facts(root), children: rows, ...(!native ? {
      root_basis: scrollRoots.includes(root) ? 'viewport_scroll_region' : 'viewport_page_container',
      visible: true, center_hits_root: true,
      viewport_coverage: viewportCoverage(boundsFor(bounds)),
    } : {})});
  }
  // 无原生标题标签的页面只采顶部独立栏，不把正文、卡片或弹窗标题投影为业务锚点。
  const headings = [];
  const bars = [...document.querySelectorAll(
    'header,[role="banner"],[class~="header"],[class~="app-bar"],[class~="navbar"],[class~="van-nav-bar"]',
  )];
  if (bars.length > 12) complete = false;
  for (const bar of bars.slice(0, 12)) {
    const rect = bar.getBoundingClientRect();
    if (!shown(bar) || blockedContext(bar) || !hit(bar)
      || rect.top < 0 || rect.top > innerHeight * .2
      || rect.width < innerWidth * .6 || rect.height > innerHeight * .2
      || roots.some(root => root.contains(bar))) continue;
    const titles = [...bar.querySelectorAll(
      '[class~="title"],[class~="page-title"],[class~="van-nav-bar__title"]',
    )];
    if (titles.length > 12) complete = false;
    for (const title of titles.slice(0, 12)) {
      const text = String(title.innerText || '').replace(/\s+/g, ' ').trim();
      if (!shown(title) || blockedContext(title) || !hit(title)
        || !text || text.length > 80 || title.children.length
        || title.matches('a,button,input,[role],[onclick],[tabindex]')) continue;
      if (headings.length >= 12) { complete = false; break; }
      headings.push({...facts(title), text, heading_basis: 'visible_page_chrome',
        visible: true, center_hits_heading: true});
    }
  }
  return {
    schema_version: 'h5-native-main-shell.v1', complete,
    document_time_origin_ms: Math.round(performance.timeOrigin * 1000) / 1000,
    roots: result,
    headings,
  };
})()"""
