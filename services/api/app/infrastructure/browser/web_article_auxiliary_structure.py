"""同帧采样有明确关系的前后篇导航与文章时间元信息。"""

WEB_ARTICLE_AUXILIARY_STRUCTURE_SCRIPT = r"""(() => {
  if (typeof selectorFor !== 'function') return [];
  const bodySelector = 'article,[role="article"],.article-body,.post-content,'
    + '.entry-content,.markdown-body,.editormd-preview,.markdown-preview';
  const bodies = [...document.querySelectorAll(bodySelector)].slice(0,100);
  const roots = new Map();
  const isolated = el => !el.matches(bodySelector) && !el.querySelector(bodySelector+',h1')
    && !bodies.some(body => body.contains(el));
  for (const anchor of [...document.querySelectorAll('a[href]')].slice(0,2000)) {
    const root = anchor.parentElement;
    if (!root || !root.matches('div,section,nav') || !isolated(root)) continue;
    const links = [...root.querySelectorAll('a[href]')];
    const labelled = link => /^(上一篇|下一篇|前一篇|后一篇|previous article|next article)/i
      .test((link.innerText || '').trim());
    if (links.length >= 1 && links.length <= 2 && links.every(labelled)
      && bodies.some(body => !root.contains(body) && (body.innerText || '').trim().length >= 120)) {
      roots.set(root,'adjacent_article_navigation');
    }
  }
  for (const title of [...document.querySelectorAll('h1')].slice(0,20)) {
    const owner = title.parentElement;
    if (!owner || !owner.querySelector(bodySelector)) continue;
    for (const root of [...owner.children]) {
      if (!root.matches('div') || !isolated(root)
        || root.querySelector('img,video,iframe,h2,h3,h4,ul,ol')) continue;
      const metadataClasses = ['detail-date','article-meta','post-meta','entry-meta'];
      if (![...root.classList].some(token => metadataClasses.includes(token))) continue;
      const text = (root.innerText || '').trim();
      const links = root.querySelectorAll('a[href]');
      if (text.length <= 180 && links.length <= 1
        && /\b\d{4}(?:年|[-/])\d{1,2}(?:月|[-/])\d{1,2}/.test(text)) {
        roots.set(root,'dated_article_metadata');
      }
    }
  }
  return [...roots].filter(([root]) =>
    visible(root,getComputedStyle(root),root.getBoundingClientRect()))
    .slice(0,16).map(([root,kind]) => ({
      selector:selectorFor(root),
      parent_selector:root.parentElement?selectorFor(root.parentElement):null,
      tag:root.tagName.toLowerCase(), visible:true, within_web_navigation:true,
      web_auxiliary_region_kind:kind,
    }));
})()"""
