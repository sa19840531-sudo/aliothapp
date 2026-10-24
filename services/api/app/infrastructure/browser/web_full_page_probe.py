"""长页探测脚本：有限遍历、脱敏摘要与可恢复的惰性装饰层处理。"""

WEB_FULL_PAGE_PROBE = r"""({operation, x, y, key}) => {
  if (operation === 'scroll') {
    window.scrollTo({left: x, top: y, behavior: 'instant'});
    return;
  }
  if (operation === 'restore') {
    for (const [node, style] of (window[key] || [])) {
      if (style === null) node.removeAttribute('style');
      else node.setAttribute('style', style);
    }
    delete window[key];
    return;
  }
  const walker = document.createTreeWalker(document.documentElement, NodeFilter.SHOW_ELEMENT);
  const nodes = [];
  while (nodes.length < 4000 && walker.nextNode()) nodes.push(walker.currentNode);
  let overflow = !!walker.nextNode();
  const signature = [];
  let signatureLength = 0;
  const feed = value => {
    const text = String(value);
    signatureLength += text.length;
    if (signatureLength > 262144) { overflow = true; return; }
    signature.push(text.length + ':' + text);
  };
  feed(location.href); feed(document.title);
  let pending = 0;
  let innerScrollers = 0;
  const layers = [];
  const hidden = [];
  if (operation === 'hide') window[key] = hidden;
  const interactive = 'a,button,input,select,textarea,form,video,audio,iframe,img,svg,canvas,' +
    '[tabindex],[contenteditable],[onclick],[role="button"],[role="link"]';
  const primary = 'main,article,[role="main"],[role="article"]';
  const noGeneratedVisual = style =>
    ['filter', '-webkit-filter', 'backdrop-filter', '-webkit-backdrop-filter'].every(
      property => ['none', ''].includes(style.getPropertyValue(property))) &&
    ['normal', ''].includes(style.mixBlendMode || '') &&
    ['normal', ''].includes(style.backgroundBlendMode || '') &&
    ['none', ''].includes(style.backgroundImage) &&
    ['none', ''].includes(style.maskImage || '') &&
    ['none', ''].includes(style.webkitMaskImage || '') &&
    ['none', ''].includes(style.borderImageSource || '') &&
    ['none', ''].includes(style.boxShadow || '') &&
    ['Top', 'Right', 'Bottom', 'Left'].every(side =>
      parseFloat(style['border' + side + 'Width'] || '0') === 0) &&
    ['none', ''].includes(style.outlineStyle || '') &&
    ['transparent', 'rgba(0, 0, 0, 0)', ''].includes(style.backgroundColor);
  const noPseudoContent = style =>
    ['none', 'normal', '', '""', "''"].includes(style.content) && noGeneratedVisual(style);
  for (let index = 0; index < nodes.length; index++) {
    const node = nodes[index];
    feed(node.tagName);
    for (const name of ['href','src','role','aria-label','aria-hidden','type','value']) {
      feed(node.getAttribute(name) || '');
    }
    let children = 0;
    for (const child of node.childNodes) {
      if (++children > 4000) { overflow = true; break; }
      if (child.nodeType === Node.TEXT_NODE) feed(child.textContent);
    }
    if (node.tagName === 'IMG') {
      pending += !node.complete ? 1 : 0;
      feed(node.complete); feed(node.naturalWidth); feed(node.naturalHeight);
    }
    const style = getComputedStyle(node);
    if (node !== document.documentElement && node !== document.body &&
        node.scrollHeight > node.clientHeight + 1 &&
        /(auto|scroll|hidden)/.test(style.overflowY) &&
        style.display !== 'none' && style.visibility !== 'hidden') innerScrollers++;
    if (!['sticky','fixed'].includes(style.position)) continue;
    const rect = node.getBoundingClientRect();
    const visible = style.visibility !== 'hidden' && style.display !== 'none' &&
      Number(style.opacity) !== 0 && rect.width > 0 && rect.height > 0;
    const noGeneratedContent = noGeneratedVisual(style) &&
      noPseudoContent(getComputedStyle(node, '::before')) &&
      noPseudoContent(getComputedStyle(node, '::after'));
    const noInteraction = style.pointerEvents === 'none' && node.tabIndex < 0 &&
      !node.isContentEditable &&
      !Array.from(node.attributes).some(attribute => attribute.name.startsWith('on')) &&
      [null, 'none', 'presentation'].includes(node.getAttribute('role'));
    // 装饰层只接受叶节点，避免遍历无限后代或遗漏自定义交互控件。
    const inert = node.parentElement === document.body && node.matches('div,span') &&
      node.children.length === 0 && !node.textContent.trim() &&
      (node.getAttribute('aria-hidden') === 'true' ||
       ['none','presentation'].includes(node.getAttribute('role'))) &&
      !node.matches(interactive) && !node.closest(primary) &&
      !node.hasAttribute('aria-label') && !node.hasAttribute('title') &&
      noGeneratedContent && noInteraction;
    const audited = layers.length < 32;
    const tag = ['div','span','header','footer','nav','aside','main','article','section',
      'button','a','input','form','img','video','iframe'].includes(node.localName)
      ? node.localName : 'element';
    if (audited) layers.push({
      selector: tag + ':probe-index(' + index + ')',
      position: style.position, visible,
      action: operation === 'hide' && inert && visible ? 'hidden' : 'retained',
      reason: inert ? 'semantic_inert_decoration' : 'semantic_equivalence_unproven'
    });
    if (operation === 'hide' && inert && visible && audited) {
      hidden.push([node, node.getAttribute('style')]);
      node.style.setProperty('visibility', 'hidden', 'important');
    }
  }
  if (operation === 'hide') { window[key] = hidden; return layers; }
  const height = Math.max(document.documentElement.scrollHeight, document.body?.scrollHeight || 0);
  return {document_height: height, viewport_height: innerHeight,
    viewport_width: innerWidth, ready_state: document.readyState,
    inner_scroll_container_count: innerScrollers,
    scroll_x: scrollX, scroll_y: scrollY, state_hash: signature.join('|') + ':' + height,
    semantic_hash: signature.join('|'), pending_images: pending,
    sticky_layers: layers, scan_limit_exceeded: overflow};
}"""
