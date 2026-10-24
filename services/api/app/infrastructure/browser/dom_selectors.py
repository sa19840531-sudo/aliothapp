"""快照与媒体观测共享的唯一 DOM 选择器，不裁断路径冒充目标身份。"""

UNIQUE_SELECTOR_FOR_SCRIPT = r"""
const selectorFor = (element) => {
  const unique = (selector, target = element) => {
    if (!selector || selector.length > SELECTOR_LIMIT) return false;
    try {
      const matches = document.querySelectorAll(selector);
      return matches.length === 1 && matches[0] === target;
    } catch (_) {
      return false;
    }
  };
  if (element.id) {
    const selector = `#${CSS.escape(element.id)}`;
    if (unique(selector)) return selector;
  }
  const testId = element.getAttribute("data-testid");
  if (testId) {
    const selector = `[data-testid="${CSS.escape(testId)}"]`;
    if (unique(selector)) return selector;
  }
  const name = element.getAttribute("name");
  if (name) {
    const selector = `${element.tagName.toLowerCase()}[name="${CSS.escape(name)}"]`;
    if (unique(selector)) return selector;
  }
  const parts = [];
  let current = element;
  while (current && current.nodeType === Node.ELEMENT_NODE && parts.length < 64) {
    if (current.id && unique(`#${CSS.escape(current.id)}`, current)) {
      parts.unshift(`#${CSS.escape(current.id)}`);
      const selector = parts.join(" > ");
      return unique(selector) ? selector : "";
    }
    let part = current.tagName.toLowerCase();
    const parent = current.parentElement;
    if (parent) {
      const siblings = [...parent.children].filter(item => item.tagName === current.tagName);
      if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(current) + 1})`;
    }
    parts.unshift(part);
    const selector = parts.join(" > ");
    if (selector.length > SELECTOR_LIMIT) return "";
    if ((parts.length >= 8 || !parent) && unique(selector)) return selector;
    current = parent;
  }
  return "";
};
"""
