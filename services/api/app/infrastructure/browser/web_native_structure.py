"""仅供既有 Web Snapshot 使用的闭集原生结构枚举，不新增浏览器动作。"""

WEB_NATIVE_STRUCTURE_CENSUS_SCRIPT = r"""(() => {
  const nodes = document.querySelectorAll('*');
  const reasons = [];
  if (!document.documentElement || !document.body || document.readyState !== 'complete') reasons.push('document_incomplete');
  if (nodes.length > 10000) reasons.push('enumeration_limit');
  if (document.querySelectorAll('canvas').length) reasons.push('canvas');
  if (document.querySelectorAll('iframe').length) reasons.push('iframe_unobserved');
  if (Array.from(nodes).slice(0,10000).some(node => node.shadowRoot || String(node.localName).includes('-'))) reasons.push('custom_or_shadow_structure');
  const count = document.querySelectorAll('video').length;
  return {
    schema_version: 'web-native-structure.v1', scope: 'current_document_light_dom',
    predicate: 'native_video_element_present',
    document_time_origin_ms: Math.round(performance.timeOrigin * 1000) / 1000,
    complete: reasons.length === 0, unobservable_reasons: reasons,
    native_video_count: count,
    search_structure: __WEB_SEARCH_STRUCTURE__,
    secondary_regions: __WEB_SECONDARY_STRUCTURE__,
    auxiliary_regions: __WEB_ARTICLE_AUXILIARY_STRUCTURE__,
    status: reasons.length ? 'unknown' : (count ? 'present' : 'local_absent'),
  };
})()"""

from services.api.app.infrastructure.browser.web_article_auxiliary_structure import (  # noqa: E402
    WEB_ARTICLE_AUXILIARY_STRUCTURE_SCRIPT,
)
from services.api.app.infrastructure.browser.web_search_structure import (  # noqa: E402
    WEB_SEARCH_STRUCTURE_SCRIPT,
)
from services.api.app.infrastructure.browser.web_secondary_structure import (  # noqa: E402
    WEB_SECONDARY_STRUCTURE_SCRIPT,
)

WEB_NATIVE_STRUCTURE_CENSUS_SCRIPT = WEB_NATIVE_STRUCTURE_CENSUS_SCRIPT.replace(
    "__WEB_SEARCH_STRUCTURE__", WEB_SEARCH_STRUCTURE_SCRIPT
).replace("__WEB_SECONDARY_STRUCTURE__", WEB_SECONDARY_STRUCTURE_SCRIPT).replace(
    "__WEB_ARTICLE_AUXILIARY_STRUCTURE__", WEB_ARTICLE_AUXILIARY_STRUCTURE_SCRIPT
)
