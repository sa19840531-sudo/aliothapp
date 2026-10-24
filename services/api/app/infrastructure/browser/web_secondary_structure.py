"""补采推荐标题所属容器，避免节点预算丢失次要区域边界。"""

WEB_SECONDARY_STRUCTURE_SCRIPT = r"""(() => {
  if (typeof selectorFor !== 'function') return [];
  const labels = new Set(['相关推荐','相關推薦','猜你喜欢','猜你喜歡','更多内容',
    '更多內容','related content','recommended','you may also like']);
  const contextualLabels = new Set(['大家都在搜','大家都在看','相关阅读','相關文章','热门推荐']);
  const bodySelector = 'article,[role="article"],.article-body,.post-content,'
    + '.entry-content,.markdown-body,.editormd-preview,.markdown-preview';
  const roots = new Set();
  const linkedCardTitle = (node,root) => {
    const anchor = node.closest('a[href]');
    if (node.tagName !== 'H3' || !anchor || !root.contains(anchor)) return false;
    return [anchor,anchor.parentElement].some(card => card && card !== root && card.parentElement
      && [...card.parentElement.children].filter(peer => peer.tagName === card.tagName
        && peer.className === card.className
        && (peer.matches('a[href]') || peer.querySelectorAll('a[href]').length === 1)).length >= 2);
  };
  for (const heading of [...document.querySelectorAll('h2,h3')].slice(0,200)) {
    const label = (heading.innerText || '').trim().toLowerCase();
    if (!labels.has(label) && !contextualLabels.has(label)) continue;
    let root = heading.parentElement;
    for (let depth = 0; root && depth < 4; depth++, root = root.parentElement) {
      if (!root.matches('section,aside,div') || root.matches(
        '[role="main"],' + bodySelector
      ) || root.querySelector('h1,' + bodySelector)) {
        root = null; break;
      }
      if ([...root.querySelectorAll('h2,h3')].some(node =>
        !labels.has((node.innerText || '').trim().toLowerCase())
        && !contextualLabels.has((node.innerText || '').trim().toLowerCase())
        && !linkedCardTitle(node,root))) {
        root = null; break;
      }
      if (root.querySelectorAll('a[href]').length >= 2) break;
      if (depth === 3) { root = null; break; }
    }
    if (!root) continue;
    if (contextualLabels.has(label) && ![...document.querySelectorAll(bodySelector)]
      .slice(0,100).some(body => !root.contains(body) && !body.contains(root)
        && (body.innerText || '').trim().length >= 120)) continue;
    const rect = root.getBoundingClientRect();
    if (!visible(root, getComputedStyle(root), rect)) continue;
    roots.add(root);
  }
  return [...roots].slice(0,16).map(root => ({
    selector:selectorFor(root),
    parent_selector:root.parentElement ? selectorFor(root.parentElement) : null,
    tag:root.tagName.toLowerCase(), visible:true, web_secondary_recommendation:true,
    class_tokens:[...root.classList].slice(0,24),
  }));
})()"""
