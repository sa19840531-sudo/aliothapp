"""在 Web 同帧采样中补齐搜索组合事实，不扩大通用节点预算。"""

WEB_SEARCH_STRUCTURE_SCRIPT = r"""(() => {
  const visible = node => {
    if (!node.getClientRects().length) return false;
    for (let current = node; current; current = current.parentElement) {
      const style = getComputedStyle(current);
      if (current.hidden || current.getAttribute('aria-hidden') === 'true'
          || style.display === 'none' || ['hidden','collapse'].includes(style.visibility)
          || style.contentVisibility === 'hidden' || Number(style.opacity) === 0) return false;
    }
    return true;
  };
  const excluded = node => Boolean(node.closest(
    'nav,aside,footer,[role="navigation"],[role="complementary"],[role="banner"],'
    + '[aria-modal="true"],.advertisement,.advertising,.ad-slot,.sponsored'
  )) || Boolean(node.closest('header') && !node.closest('header').closest('main,article,section'));
  const namedSearchContainer = node => [...node.classList].some(token =>
    /(?:^|[-_])search(?:[-_]+[a-z0-9]+)*[-_]+(?:bar|box|form|panel)(?:[-_]|$)/i.test(token));
  const roots = [...new Set([...document.querySelectorAll(
    '[role="search"],form,.search-box,.search-form,.query-form'
  ), ...[...document.querySelectorAll('main [class*="search"],[role="main"] [class*="search"]')]
    .filter(namedSearchContainer)])];
  const inputs = new Set();
  const triggers = new Set();
  const complete = roots.length <= 100;
  for (const root of roots.slice(0,100)) {
    if (excluded(root) || !visible(root)) continue;
    const explicit = root.matches('[role="search"],.search-box,.search-form,.query-form')
      || namedSearchContainer(root);
    const fields = [...root.querySelectorAll('input')].filter(node =>
      !node.disabled && !node.readOnly && visible(node) && !excluded(node)
      && (node.type === 'search' || node.getAttribute('role') === 'searchbox'
        || node.matches('.search-input,.query-input') || (explicit && node.type === 'text')));
    if (!fields.length) continue;
    fields.forEach(node => inputs.add(node));
    for (const node of root.querySelectorAll('button,input[type="submit"],[role="button"]')) {
      if (node.disabled || node.getAttribute('aria-disabled') === 'true'
          || !visible(node) || excluded(node)) continue;
      const label = (node.getAttribute('aria-label') || node.textContent || '')
        .trim().toLowerCase();
      if (node.type === 'submit' || node.matches('.search-submit,.search-trigger,.search-button')
          || (explicit && (['搜索','搜尋','search'].includes(label)
            || [...node.classList].some(token =>
              /(?:^|[-_])search(?:[-_]+[a-z0-9]+)*[-_]+(?:submit|trigger|button|btn)(?:[-_]|$)/i
                .test(token))))) triggers.add(node);
    }
  }
  return {schema_version:'web-search-structure.v1', complete,
    document_time_origin_ms:Math.round(performance.timeOrigin * 1000) / 1000,
    search_input_count: inputs.size, search_trigger_count: triggers.size};
})()"""
