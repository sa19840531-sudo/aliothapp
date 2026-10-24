"""Web 冻结解释的安全附录，不修改体验报告的问题集或历史正文。"""

import html
import json

from services.api.app.application.web_report_explanation import build_web_report_explanation


def append_web_report_explanation(document, explanation):
    if not isinstance(explanation, dict):
        return document
    public = build_web_report_explanation(
        task_id=str(explanation.get("task_id") or "")[:200],
        agent_run_id=str(explanation.get("agent_run_id") or "")[:200],
        epoch=explanation.get("finalization_epoch"),
        rows=explanation,
    )
    facts = html.escape(json.dumps(public, ensure_ascii=False, indent=2), quote=True)
    appendix = (
        '<section data-web-report-explanation="v1" style="padding:24px;overflow-wrap:anywhere">'
        "<h2>Web 冻结证据与判定说明</h2>"
        "<p>以下引用来自同一正式快照。分类、发布、逐规则判定和批次技术状态分别保留；"
        "有失败序列不代表页面稳定或覆盖完成。局部 scope/predicate 仅说明本次文档，"
        "产品能力和账号没有可信事实时仍为 unknown。正式绑定不等于本轮逐页人工验收。</p>"
        "<p>notifications：分类后通知；sequences：失败序列与局部能力；"
        "rule_materials：逐规则原始判定与保守合并来源；publication_rules：规则选择；"
        "primary_content：主体补图与失败回退。跨页面静态素材复用尚未启用。</p>"
        f'<pre style="white-space:pre-wrap">{facts}</pre></section>'
    )
    if "</body>" not in document:
        raise ValueError("WEB_REPORT_EXPORT_BODY_MISSING")
    return document.replace("</body>", appendix + "</body>", 1)
