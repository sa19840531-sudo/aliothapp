"""体验平台兼容的质检报告导出：黑金样式正文 + ``__edit_data__`` 数据载体。

移植自体验平台（ty.dudao.ai）编辑器 ``buildReportHTMLString``/``buildOutputHTML``
生成器（v91）：CSS 原样保留，正文结构与平台自产报告一致。平台「导入」
只读取 ``</title>`` 后隐藏 div 里的 base64(UTF-8 JSON)，因此数据契约是硬约束，
正文样式只求观感一致。只实现玉衡需要的子集：纯质检分区、text 条目、无雷达、
无 LOGO、无体验/质检混合分区。

与平台模板的一处刻意偏离：交互不用内联 ``onclick`` 属性，统一改为 <script>
里的事件委托——部分查看环境（App 内嵌预览等）的 CSP 禁内联属性处理器但不拦
脚本内注册的监听，平台原版在这类环境下侧边栏点不动。
"""

import base64
import html
import json
import re
from datetime import datetime
from typing import Any

# 平台编辑器预置分类（exp 10 项 + qc 14 项，许遥清单 0729）。导入时顶层
# categories 非空会整体替换编辑器预置，因此导出必须带全量预置而非只带用到的。
_PLATFORM_EXP_CATEGORIES = [
    "背景",
    "总结",
    "亮点",
    "商业转化",
    "用户运营",
    "内容浏览",
    "商业化",
    "搜索&推荐",
    "社区",
    "其他",
]

_PLATFORM_QC_CATEGORIES = [
    "播放内容规范",
    "播放器规范",
    "播放视频前广告规范",
    "产品启动广告规范",
    "产品启动规范",
    "充值、支付、会员、金币类规范",
    "广告通用规范",
    "交互规范",
    "内容展示规范与规避山寨感",
    "全局的权限与隐私",
    "首页弹窗广告规范",
    "用户输入控件与内容规范(含涉未、恐、政)",
    "App套壳规范",
    "UI兼容性规范",
]

PLATFORM_DEFAULT_CATEGORIES = [
    *({"name": name, "sec": "exp"} for name in _PLATFORM_EXP_CATEGORIES),
    *({"name": name, "sec": "qc"} for name in _PLATFORM_QC_CATEGORIES),
]

# 玉衡 7 个规则分类 → 平台 14 项质检分类的归属映射；映射不到时回落规则包名。
_CATEGORY_TO_PLATFORM_QC = {
    "text": "内容展示规范与规避山寨感",
    "visual": "内容展示规范与规避山寨感",
    "interaction": "交互规范",
    "payment": "充值、支付、会员、金币类规范",
    "ad": "广告通用规范",
    "input_risk": "用户输入控件与内容规范(含涉未、恐、政)",
    "player": "播放器规范",
}

# 平台 CAT_COLORS / PRI_COLORS 原样移植。
_CAT_COLORS = [
    {"bg": "rgba(239,68,68,.18)", "border": "rgba(239,68,68,.4)", "text": "#ef4444", "dot": "#ef4444"},
    {"bg": "rgba(251,191,36,.14)", "border": "rgba(251,191,36,.35)", "text": "#fbbf24", "dot": "#fbbf24"},
    {"bg": "rgba(34,197,94,.14)", "border": "rgba(34,197,94,.35)", "text": "#22c55e", "dot": "#22c55e"},
    {"bg": "rgba(99,102,241,.18)", "border": "rgba(99,102,241,.4)", "text": "#818cf8", "dot": "#818cf8"},
    {"bg": "rgba(14,165,233,.14)", "border": "rgba(14,165,233,.35)", "text": "#38bdf8", "dot": "#38bdf8"},
    {"bg": "rgba(244,114,182,.14)", "border": "rgba(244,114,182,.35)", "text": "#f472b6", "dot": "#f472b6"},
]

_PRI_COLORS = {
    "P0": {"bg": "rgba(239,68,68,.18)", "border": "rgba(239,68,68,.45)", "text": "#ef4444"},
    "P1": {"bg": "rgba(251,146,60,.15)", "border": "rgba(251,146,60,.4)", "text": "#fb923c"},
    "P2": {"bg": "rgba(251,191,36,.14)", "border": "rgba(251,191,36,.35)", "text": "#fbbf24"},
    "P3": {"bg": "rgba(148,163,184,.1)", "border": "rgba(148,163,184,.3)", "text": "#94a3b8"},
}

_PRI_SEQ = ["P0", "P1", "P2", "P3"]

_BULLET_LINE = re.compile(r"^([◆◇•‣·▪✦●○\-—]+)\s*(.*)$")


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def platform_qc_category(category: str | None, fallback: str) -> str:
    """规则分类 code → 平台质检分类名；映射不到回落规则包名（平台按自定义分类展示）。"""
    return _CATEGORY_TO_PLATFORM_QC.get(category or "", fallback)


def build_platform_issue(
    item: dict[str, Any],
    *,
    issue_id: int,
    image_data_uris: list[str],
    evidence_note: str = "",
) -> dict[str, Any]:
    """把玉衡规则结果映射为平台 text 型 issue。

    sec 必须显式 'qc'（平台导入缺 sec 按体验处理）；content 按对方 2026-08-06
    模板行文：规范 / 问题（AI 判定+原因+人工二次确认+所属页面）/ 优化建议；
    images 只接受 data: URI。
    """
    check_point = str(item.get("check_point") or "").strip()
    reason = str(item.get("problem_desc") or item.get("result_summary") or "").strip()
    if not reason:
        reason = "AI 未给出具体说明"
    machine_label = str(
        item.get("machine_display_status_label") or item.get("display_status_label") or "不通过"
    )
    problem = f"问题：本条规则，AI 判定为{machine_label}（原因：{reason}）"
    verdict = item.get("human_verdict")
    if verdict is not None:
        label = {"pass": "通过", "fail": "不通过", "ignored": "本产品不适用"}.get(verdict, "未确认")
        problem += f"，人工二次确认为：{label}"
    page_name = str(item.get("bound_page_type_name") or "").strip()
    if page_name:
        problem += f"；所属页面：{page_name}"
    verdict_note = str(item.get("human_verdict_note") or "").strip()
    if verdict_note:
        problem += f"；人工判定备注：{verdict_note}"
    paragraphs = [f"规范：{check_point or '未提供检查规范'}", problem]
    if verdict is not None:
        actor = str(item.get("human_verdict_by") or "").strip() or "未记录"
        confirmed_at = item.get("human_verdict_at")
        confirmed_at_text = (
            confirmed_at.isoformat(sep=" ", timespec="seconds")
            if isinstance(confirmed_at, datetime)
            else str(confirmed_at or "").strip() or "未记录"
        )
        paragraphs.append(f"人工确认记录：确认人：{actor}；确认时间：{confirmed_at_text}")
    if evidence_note:
        paragraphs.append(evidence_note)
    paragraphs.extend(["优化建议：需人工填写", "研发回复：待研发填写"])
    severity_display = item.get("severity_display")
    return {
        "id": issue_id,
        "type": "text",
        "title": str(item.get("rule_name") or item.get("rule_code") or "未命名规则"),
        "categories": [
            platform_qc_category(item.get("category"), str(item.get("rule_package") or "通用质检"))
        ],
        "priority": severity_display if severity_display in _PRI_SEQ else "",
        "sec": "qc",
        "content": "\n\n".join(paragraphs),
        "images": list(image_data_uris),
    }


def render_experience_report(
    *,
    title: str,
    author: str,
    date: str,
    issues: list[dict[str, Any]],
    inspection_note: str = "",
) -> str:
    """生成平台样式的自包含报告 HTML（含 ``__edit_data__``）。"""
    ordered = _sort_issues(issues)
    edit_state = {
        "title": title,
        "author": author,
        "date": date,
        "categories": PLATFORM_DEFAULT_CATEGORIES,
        "issues": ordered,
        "idCounter": len(ordered) + 1,
        "logoData": None,
        "radar": None,
    }
    edit_data = base64.b64encode(
        json.dumps(edit_state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")

    meta_sep = " · " if author and date else ""
    sub_sep = "&nbsp;·&nbsp;" if author and date else ""
    count = len(ordered)

    return (
        _PAGE_TEMPLATE.replace("%%TITLE%%", _esc(title))
        .replace("%%EDIT_DATA%%", edit_data)
        .replace("%%AUTHOR%%", _esc(author))
        .replace("%%SUB_SEP%%", sub_sep)
        .replace("%%META_SEP%%", meta_sep)
        .replace("%%DATE%%", _esc(date))
        .replace("%%COUNT%%", str(count))
        .replace("%%PRI_STATS%%", _pri_stats_html(ordered))
        .replace("%%PRI_FILTER%%", _pri_filter_html(ordered))
        .replace("%%NAV_ITEMS%%", _nav_items_html(ordered))
        .replace("%%SLIDES%%", _cover_slide_html(title, author, date, count, inspection_note) + _issue_slides_html(ordered))
    )


# ---------- 内部 ----------


def _sort_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """平台同款排序：按首分类首次出现顺序稳定聚合（全部为质检条目）。"""
    order: dict[str, int] = {}
    for issue in issues:
        cat = _first_category(issue)
        if cat not in order:
            order[cat] = len(order)
    return [
        issue
        for _, issue in sorted(
            enumerate(issues),
            key=lambda pair: (order.get(_first_category(pair[1]), 999), pair[0]),
        )
    ]


def _first_category(issue: dict[str, Any]) -> str:
    categories = issue.get("categories") or []
    return str(categories[0]).strip() if categories else ""


def _category_index(name: str) -> int:
    for index, category in enumerate(PLATFORM_DEFAULT_CATEGORIES):
        if category["name"] == name:
            return index
    return -1


def _pri_of(issue: dict[str, Any]) -> str:
    priority = issue.get("priority") or ""
    return priority if priority in _PRI_SEQ else ""


def _pri_stats_html(issues: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for issue in issues:
        pri = _pri_of(issue)
        if pri:
            counts[pri] = counts.get(pri, 0) + 1
    spans = [
        f'<span class="pri-stat pri-stat-{pri.lower()}"><span class="pri-stat-k">{pri}</span>'
        f'<span class="pri-stat-n">{counts[pri]}</span></span>'
        for pri in _PRI_SEQ
        if counts.get(pri)
    ]
    if not spans:
        return ""
    return f'<div class="pri-stats" id="priStats">{"".join(spans)}</div>'


def _pri_filter_html(issues: list[dict[str, Any]]) -> str:
    # 交互统一走事件委托（data-* 属性），不用内联 onclick：部分查看环境的 CSP
    # 会禁用内联属性处理器，但不拦 <script> 里注册的监听。
    present = {_pri_of(issue) for issue in issues if _pri_of(issue)}
    buttons = ['<button class="pri-btn all active" data-pri-filter="">全部</button>']
    buttons.extend(
        f'<button class="pri-btn {pri.lower()}" data-pri-filter="{pri}">{pri}</button>'
        for pri in _PRI_SEQ
        if pri in present
    )
    return "".join(buttons)


def _nav_items_html(issues: list[dict[str, Any]]) -> str:
    items = [
        '<div class="nav-item active" data-goto="0" data-pri="" data-always="1">'
        '<span class="nav-num">—</span><span class="nav-title">封面</span></div>'
    ]
    last_cat = "__init__"
    for i, issue in enumerate(issues):
        nav_cat = _first_category(issue)
        divider = ""
        if nav_cat and nav_cat != last_cat:
            divider = f'<div class="nav-cat-div">{_esc(nav_cat)}</div>'
        last_cat = nav_cat
        categories = issue.get("categories") or []
        cat_index = _category_index(str(categories[0])) if categories else -1
        dot = (
            f'<span style="width:6px;height:6px;border-radius:50%;'
            f'background:{_CAT_COLORS[cat_index % len(_CAT_COLORS)]["dot"]};flex-shrink:0;margin-top:3px"></span>'
            if cat_index >= 0
            else ""
        )
        pri = _pri_of(issue)
        pri_tag = (
            f'<span style="font-size:9px;font-weight:700;color:{_PRI_COLORS[pri]["text"]};'
            f'flex-shrink:0;margin-top:2px">{pri}</span>'
            if pri
            else ""
        )
        cats_line = (
            '<span style="font-size:10px;opacity:.6;display:block;line-height:1.3">'
            + " · ".join(_esc(cat) for cat in categories)
            + "</span>"
            if categories
            else ""
        )
        title = _esc(issue.get("title")) or "纯文字页"
        items.append(
            divider
            + f'<div class="nav-item" data-goto="{i + 1}" data-pri="{pri}" '
            f'data-issue-id="{_esc(str(issue.get("id") or ""))}">\n'
            f'      <span class="nav-num">{str(i + 1).zfill(2)}</span>\n'
            f"      {dot}\n"
            f'      <span class="nav-title">{cats_line}{title}</span>\n'
            f"      {pri_tag}\n    </div>"
        )
    return "".join(items)


def _cover_slide_html(title: str, author: str, date: str, count: int, inspection_note: str = "") -> str:
    meta_sep = " · " if author and date else ""
    return (
        '<div class="slide cover-slide active" id="slide-0">\n'
        '    <div class="cover-inner">\n'
        '      <div class="cover-icon">📋</div>\n'
        f'      <div class="cover-title">{_esc(title)}</div>\n'
        f'      <div class="cover-meta">{_esc(author)}{meta_sep}{_esc(date)}</div>\n'
        f'      <div class="cover-badge">共 {count} 个问题</div>\n'
        + (f'      <p class="cover-meta">{_esc(inspection_note)}</p>\n' if inspection_note else "")
        +
        "    </div>\n  </div>"
    )


def _fmt_line(line: str) -> str:
    match = _BULLET_LINE.match(line)
    if match and match.group(2):
        return f'<p><span class="diam">◆</span>{_esc(match.group(2))}</p>'
    return f"<p>{_esc(line)}</p>"


def _issue_slides_html(issues: list[dict[str, Any]]) -> str:
    slides = []
    for i, issue in enumerate(issues):
        categories = issue.get("categories") or []
        badges = []
        for cat in categories:
            cat_index = _category_index(str(cat))
            if cat_index < 0:
                continue
            color = _CAT_COLORS[cat_index % len(_CAT_COLORS)]
            badges.append(
                f'<span class="cat-badge" style="background:{color["bg"]};'
                f'border:1px solid {color["border"]};color:{color["text"]}">{_esc(cat)}</span>'
            )
        pri = _pri_of(issue)
        if pri:
            color = _PRI_COLORS[pri]
            badges.append(
                f'<span class="cat-badge" style="background:{color["bg"]};'
                f'border:1px solid {color["border"]};color:{color["text"]}">{pri}</span>'
            )
        cat_prefix = (
            "【" + "·".join(_esc(cat) for cat in categories) + "】" if categories else ""
        )
        raw_content = str(issue.get("content") or "")
        # 只识别最后一个独立段落，判定备注中提到研发回复时仍属于问题说明。
        content, separator, reply = raw_content.rpartition("\n\n研发回复：")
        if not separator:
            content = raw_content
        lines = "".join(
            _fmt_line(line.strip()) if line.strip() else "<br>"
            for line in content.rstrip().split("\n")
        )
        images = issue.get("images") or []
        img_grid = ""
        if images:
            cells = "".join(
                f'<div class="ti-img-item"><img src="{src}"></div>' for src in images
            )
            img_grid = f'<div class="ti-grid ti-grid-{min(len(images), 3)}">{cells}</div>'
        title = _esc(issue.get("title")) or "纯文字页"
        # 回复继续放在现有 content 文本协议中，导入体验编辑器后可续写，不引入业务字段。
        reply_html = (
            '<div class="text-col"><p class="text-lbl sug-lbl">研发回复</p>'
            f'<div class="text-body">{_fmt_line(reply.strip() or "待研发填写")}</div></div>'
            if separator else ""
        )
        body = (
            '<div class="text-area"><div class="text-col">'
            '<p class="text-lbl prob-lbl">质检问题与建议</p>'
            f'<div class="text-body">{lines}{img_grid}</div></div>{reply_html}</div>'
            if reply_html else f'<div class="text-page-content">{lines}{img_grid}</div>'
        )
        slides.append(
            f'<div class="slide text-slide" id="slide-{i + 1}" data-pri="{pri}" '
            f'data-issue-id="{issue.get("id")}">\n'
            f'        <div class="slide-hdr">{"".join(badges)}<h2>{cat_prefix}{title}</h2></div>\n'
            f'        {body}\n'
            "      </div>"
        )
    return "".join(slides)


# 页面骨架：CSS 与交互 JS 逐字节取自平台编辑器 buildOutputHTML（v91），勿手工调样式；
# 平台模板升级时整段替换。占位符 %%...%% 由 render_experience_report 填充。
_PAGE_TEMPLATE = r"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>%%TITLE%%</title>
<div id="__edit_data__" style="display:none">%%EDIT_DATA%%</div>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=Noto+Serif+SC:wght@500;700;900&family=Space+Grotesk:wght@500;700&display=swap" rel="stylesheet">
<style>
:root{--bg:#0b0a0d;--panel:#141218;--panel2:#17151c;--ink:#ece7dd;--muted:rgba(236,231,221,.5);--faint:rgba(236,231,221,.32);--gold:#e3c07a;--gold2:#caa256;--line:rgba(227,192,122,.16);--line2:rgba(255,255,255,.06)}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;background:var(--bg);color:var(--ink);font-family:"PingFang SC","Microsoft YaHei",sans-serif;overflow:hidden}
body{background:radial-gradient(1200px 700px at 78% -8%,rgba(227,192,122,.07),transparent 60%),radial-gradient(900px 600px at 110% 120%,rgba(120,180,140,.04),transparent 55%),var(--bg);-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
img{max-width:100%;display:block}
#bgfx{position:fixed;inset:0;width:100%;height:100%;z-index:0;pointer-events:none}
.serif{font-family:"Noto Serif SC","Songti SC",serif}
.shell{display:flex;height:100vh;overflow:hidden;position:relative;z-index:1}
.sidebar{width:270px;flex-shrink:0;background:linear-gradient(180deg,rgba(20,18,24,.96),rgba(11,10,13,.96));border-right:1px solid var(--line);display:flex;flex-direction:column;overflow:hidden;backdrop-filter:blur(4px)}
.sidebar-hdr{padding:20px 18px 12px;border-bottom:1px solid var(--line)}
.s-title{font-family:"Noto Serif SC","Songti SC",serif;color:#fff;font-size:15px;font-weight:700;margin-bottom:5px;line-height:1.4}
.s-sub{color:var(--faint);font-size:11px;margin-bottom:9px;letter-spacing:.3px}
.s-count{display:inline-block;background:rgba(227,192,122,.12);border:1px solid var(--line);color:var(--gold);font-size:11px;padding:3px 12px;border-radius:20px;letter-spacing:.5px}
.pri-stats{display:flex;gap:6px;flex-wrap:wrap;padding:12px 16px 4px}
.pri-stat{display:inline-flex;align-items:center;gap:0;border-radius:7px;overflow:hidden;border:1px solid var(--line2);font-family:"Space Grotesk",monospace;line-height:1}
.pri-stat-k{font-size:10px;font-weight:700;padding:4px 7px;letter-spacing:.3px}
.pri-stat-n{font-size:12px;font-weight:700;padding:4px 8px;background:rgba(0,0,0,.25);color:#fff;min-width:22px;text-align:center}
.pri-stat-hl{border-color:rgba(227,192,122,.45)} .pri-stat-hl .pri-stat-k{background:rgba(227,192,122,.16);color:var(--gold)}
.pri-stat-p0{border-color:rgba(216,99,99,.45)} .pri-stat-p0 .pri-stat-k{background:rgba(216,99,99,.18);color:#e08a8a}
.pri-stat-p1{border-color:rgba(214,158,86,.4)} .pri-stat-p1 .pri-stat-k{background:rgba(214,158,86,.16);color:#e0b06a}
.pri-stat-p2{border-color:rgba(227,192,122,.35)} .pri-stat-p2 .pri-stat-k{background:rgba(227,192,122,.14);color:var(--gold)}
.pri-stat-p3{border-color:rgba(160,160,150,.3)} .pri-stat-p3 .pri-stat-k{background:rgba(160,160,150,.12);color:#a9a499}
.pri-filter{display:flex;gap:5px;padding:11px 16px;border-bottom:1px solid var(--line2);flex-wrap:wrap}
.pri-btn{background:rgba(255,255,255,.04);border:1px solid var(--line2);color:var(--faint);font-size:10px;font-weight:700;padding:3px 11px;border-radius:20px;cursor:pointer;transition:all .15s;font-family:"Space Grotesk",monospace}
.pri-btn:hover{background:rgba(227,192,122,.1);color:var(--gold)}
.pri-btn.active{color:#fff}
.pri-btn.all.active{background:rgba(227,192,122,.16);border-color:var(--gold2);color:var(--gold)}
.pri-btn.hl.active{background:rgba(227,192,122,.18);border-color:rgba(227,192,122,.55);color:var(--gold)}
.pri-btn.p0.active{background:rgba(216,99,99,.2);border-color:rgba(216,99,99,.5);color:#e08a8a}
.pri-btn.p1.active{background:rgba(214,158,86,.16);border-color:rgba(214,158,86,.4);color:#e0b06a}
.pri-btn.p2.active{background:rgba(227,192,122,.14);border-color:rgba(227,192,122,.35);color:var(--gold)}
.pri-btn.p3.active{background:rgba(160,160,150,.1);border-color:rgba(160,160,150,.3);color:#a9a499}
.nav-list{flex:1;overflow-y:auto;padding:6px 0}
.nav-list::-webkit-scrollbar{width:3px}
.nav-list::-webkit-scrollbar-thumb{background:var(--line)}
.nav-item{display:flex;align-items:flex-start;gap:7px;padding:10px 14px;cursor:pointer;border-left:2px solid transparent;transition:all .15s}
.nav-item:hover{background:rgba(227,192,122,.05)}
.nav-item.active{background:rgba(227,192,122,.08);border-left-color:var(--gold)}
.nav-item.hidden{display:none}
.nav-cat-div{padding:13px 18px 5px;font-size:10.5px;font-weight:700;letter-spacing:1.5px;color:var(--gold);opacity:.75;display:flex;align-items:center;gap:7px}
.nav-cat-div::after{content:'';flex:1;height:1px;background:linear-gradient(90deg,rgba(227,192,122,.25),transparent)}
.nav-cat-div.hidden{display:none}
.nav-num{flex-shrink:0;color:var(--faint);font-size:11px;font-weight:700;margin-top:1px;width:20px;font-family:"Space Grotesk",monospace}
.nav-item.active .nav-num{color:var(--gold)}
.nav-title{color:var(--muted);font-size:12px;line-height:1.5;flex:1}
.nav-item.active .nav-title{color:#fff}
.main{flex:1;overflow-y:auto;min-width:0;background:transparent}
.main::-webkit-scrollbar{width:5px}
.main::-webkit-scrollbar-thumb{background:var(--line);border-radius:3px}
.slide{display:none;padding:26px 30px 40px;max-width:1180px}
.slide.active{display:block}
.cover-slide{display:none;padding:0}
.cover-slide.active{display:flex;align-items:center;justify-content:center;min-height:100%}
.cover-inner{text-align:center;padding:60px 40px}
.cover-icon{font-size:52px;margin-bottom:20px;filter:drop-shadow(0 0 18px rgba(227,192,122,.4))}
.cover-logo{width:84px;height:84px;object-fit:contain;border-radius:16px;margin:0 auto 22px;display:block}
.cover-title{font-family:"Noto Serif SC","Songti SC",serif;font-weight:900;font-size:40px;margin-bottom:14px;line-height:1.2;background:linear-gradient(180deg,#f4e4bf 0%,#e3c07a 55%,#b5893f 100%);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;text-shadow:0 0 50px rgba(227,192,122,.25)}
.cover-meta{color:var(--muted);font-size:13px;margin-bottom:24px;letter-spacing:2px;text-transform:uppercase;font-family:"Space Grotesk",monospace}
.cover-badge{display:inline-block;background:rgba(227,192,122,.1);border:1px solid var(--line);color:var(--gold);padding:8px 24px;border-radius:24px;font-size:13px;letter-spacing:1px}
.slide-hdr{margin-bottom:18px;padding-bottom:14px;border-bottom:1px solid var(--line)}
.slide-hdr h2{font-family:"Noto Serif SC","Songti SC",serif;color:#fff;font-size:19px;font-weight:700;line-height:1.5}
.mod-tag{display:inline-block;background:rgba(227,192,122,.12);color:var(--gold);font-size:10px;font-weight:700;padding:3px 11px;border-radius:20px;border:1px solid var(--line);letter-spacing:1px;font-family:"Space Grotesk",monospace}
.cat-badge{display:inline-block;font-size:10px;font-weight:700;padding:3px 10px;border-radius:20px}
.compare-block{margin-bottom:16px}
.pair-num{color:var(--faint);font-size:10px;font-weight:700;letter-spacing:.5px;margin-bottom:7px;padding-left:2px;font-family:"Space Grotesk",monospace}
.compare-row{display:grid;grid-template-columns:1fr 1px 1fr;align-items:start;background:rgba(255,255,255,.02);border:1px solid var(--line);border-radius:12px;overflow:hidden}
.col-div{background:var(--line)}
.col{min-width:0;display:flex;flex-direction:column}
.col-hdr{font-size:11px;font-weight:700;letter-spacing:1.5px;text-align:center;padding:9px 0}
.hdr-before{background:rgba(200,84,84,.12);color:#dd8d8d}
.hdr-after{background:rgba(96,170,124,.12);color:#84c79b}
.img-area{padding:12px;background:rgba(255,255,255,.015);display:flex;justify-content:center}
.img-item{position:relative;border-radius:8px;overflow:hidden;background:rgba(0,0,0,.25);width:100%;display:flex;justify-content:center}
.img-item img{max-width:100%;width:auto;height:auto;display:block;margin:0 auto;cursor:zoom-in;object-fit:contain;max-height:78vh}
.no-img{color:var(--faint);font-size:12px;text-align:center;padding:40px 0;width:100%}
.text-area{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin:12px 0 18px}
.text-col{border-radius:13px;overflow:hidden;display:flex;flex-direction:column;border:1px solid rgba(221,141,141,.26);background:linear-gradient(180deg,rgba(221,141,141,.06),rgba(221,141,141,.012) 55%,transparent)}
.text-col:last-child{border-color:rgba(96,170,124,.26);background:linear-gradient(180deg,rgba(96,170,124,.06),rgba(96,170,124,.012) 55%,transparent)}
.text-lbl{font-size:11px;font-weight:700;letter-spacing:2px;text-align:center;padding:10px 16px;margin:0;font-family:"Space Grotesk",monospace}
.prob-lbl{color:#dd8d8d;background:rgba(221,141,141,.09);border-bottom:1px solid rgba(221,141,141,.2)}
.sug-lbl{color:#84c79b;background:rgba(96,170,124,.09);border-bottom:1px solid rgba(96,170,124,.18)}
.text-body{padding:15px 20px 17px}
.text-body p{color:rgba(248,246,241,.93);font-size:14.5px;line-height:1.85;margin-bottom:8px}
.text-body p:last-child{margin-bottom:0}
.diam{color:var(--gold);margin-right:8px;font-size:10px;vertical-align:1.5px;text-shadow:0 0 6px rgba(227,192,122,.45)}
.text-slide .slide-hdr h2{font-size:19px}
.text-page-content{padding:4px 0;max-width:820px}
.text-page-content p{color:rgba(248,246,241,.93);font-size:14.5px;line-height:1.95;margin-bottom:9px}
.ti-grid{display:grid;gap:14px;margin-top:20px}
.ti-grid-1{grid-template-columns:1fr}
.ti-grid-2{grid-template-columns:1fr 1fr}
.ti-grid-3{grid-template-columns:1fr 1fr 1fr}
.ti-img-item{border-radius:12px;overflow:hidden;background:rgba(255,255,255,.02);border:1px solid var(--line);display:flex;justify-content:center;padding:10px}
.ti-img-item img{max-width:100%;height:auto;display:block;cursor:zoom-in;border-radius:6px}
.ti-grid-2 .ti-img-item img,.ti-grid-3 .ti-img-item img{width:100%;object-fit:contain;max-height:520px}
.lb{display:none;position:fixed;inset:0;background:rgba(8,7,9,.96);z-index:999;flex-direction:column}
.lb.on{display:flex}
.lb-bar{display:flex;align-items:center;justify-content:center;gap:10px;padding:11px;border-bottom:1px solid var(--line)}
.lb-bt{background:rgba(227,192,122,.1);border:1px solid var(--line);color:var(--gold);min-width:34px;height:32px;border-radius:8px;cursor:pointer;font-size:17px;line-height:1;padding:0 10px}
.lb-bt:hover{background:rgba(227,192,122,.22)}
.lb-bt.lb-x{font-size:20px}
#lb-pct{min-width:56px;text-align:center;color:var(--gold);font-size:13px;font-family:"Space Grotesk",monospace}
.lb-stage{flex:1;overflow:hidden;display:flex;align-items:center;justify-content:center;cursor:grab;touch-action:none}
.lb-stage img{max-width:90%;max-height:100%;border-radius:6px;transform-origin:center center;will-change:transform;user-select:none;-webkit-user-drag:none}
.mob-nav-btn{display:none;position:fixed;top:10px;left:10px;z-index:300;background:var(--panel);border:1px solid var(--line);color:var(--gold);width:40px;height:40px;border-radius:8px;font-size:20px;cursor:pointer;align-items:center;justify-content:center;line-height:1}
.mob-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:250}
@media(max-width:768px){
  html,body{overflow:auto;height:auto}
  .shell{flex-direction:column;height:auto;min-height:100vh;overflow:visible}
  .sidebar{width:280px;position:fixed;left:-280px;top:0;height:100%;z-index:260;transition:left .25s;overflow-y:auto}
  .sidebar.mob-open{left:0}
  .mob-nav-btn{display:flex}
  .mob-overlay.mob-open{display:block}
  .main{overflow:visible;min-height:100vh;padding-top:58px}
  .slide{padding:16px 14px 28px}
  .cover-inner{padding:40px 20px}
  .cover-title{font-size:28px}
  .cover-meta{font-size:11px}
  .slide-hdr h2{font-size:16px}
  .compare-row{grid-template-columns:1fr}
  .col-div{display:none}
  .text-area{grid-template-columns:1fr;gap:12px}
  .text-body p{font-size:14px;line-height:1.8}
  .ti-grid-2,.ti-grid-3{grid-template-columns:1fr}
  .text-page-content p{font-size:14px}
  .ti-grid-2 .ti-img-item img,.ti-grid-3 .ti-img-item img{max-height:300px}
  .img-item img{max-height:62vh}
}
</style></head><body>
<canvas id="bgfx"></canvas>
<button class="mob-nav-btn" id="mobNavBtn">&#9776;</button>
<div class="mob-overlay" id="mobOverlay"></div>
<div class="shell">
<aside class="sidebar">
  <div class="sidebar-hdr">
    <div class="s-title">%%TITLE%%</div>
    <div class="s-sub">%%AUTHOR%%%%SUB_SEP%%%%DATE%%</div>
    <div class="s-count">共 %%COUNT%% 个问题</div>
  </div>
  %%PRI_STATS%%
  <div class="pri-filter" id="priFilter">
    %%PRI_FILTER%%
  </div>
  <div class="nav-list" id="navList">%%NAV_ITEMS%%</div>
</aside>
<div class="main" id="main">%%SLIDES%%</div>
</div>
<div class="lb" id="lb"><div class="lb-bar"><button class="lb-bt" data-lb="minus">−</button><span id="lb-pct">100%</span><button class="lb-bt" data-lb="plus">＋</button><button class="lb-bt" data-lb="reset" style="font-size:12px">重置</button><button class="lb-bt lb-x" data-lb="close">×</button></div><div class="lb-stage" id="lb-stage"><img id="lb-img"></div></div>
<script>
let _curPri = '';
let _curSection = '';
function _navItemsArr(){ return Array.from(document.querySelectorAll('.nav-item')); }
function filterPri(pri) {
  _curPri = pri;
  document.querySelectorAll('.pri-btn').forEach(btn => {
    btn.classList.toggle('active', (btn.dataset.priFilter || '') === pri);
  });
  document.querySelectorAll('.nav-item').forEach(item => {
    const itemPri = item.dataset.pri || '';
    const isCover = item.dataset.always === '1' || (itemPri === '' && item.querySelector('.nav-num')?.textContent === '—');
    const priHidden = !isCover && pri !== '' && itemPri !== pri;
    item.classList.toggle('pri-hidden', priHidden);
    item.classList.toggle('hidden', priHidden);
  });
  document.querySelectorAll('.nav-cat-div').forEach(div => {
    let n = div.nextElementSibling, any = false;
    while (n && !n.classList.contains('nav-cat-div')) { if (n.classList.contains('nav-item') && !n.classList.contains('hidden')) { any = true; break; } n = n.nextElementSibling; }
    div.classList.toggle('hidden', !any);
  });
  const activeNav = document.querySelector('.nav-item.active');
  if (activeNav && activeNav.classList.contains('hidden')) {
    goTo(0);
  }
}
function go(i){
  const allNavItems = document.querySelectorAll('.nav-item');
  const allSlides = document.querySelectorAll('.slide');
  allNavItems.forEach((el,j)=>el.classList.toggle('active',i===j));
  allSlides.forEach((el,j)=>el.classList.toggle('active',i===j));
  document.getElementById('main').scrollTop=0;
}
let _curSlide = 0;
function _liveSlide(){ const ss = document.querySelectorAll('.slide'); for (let i = 0; i < ss.length; i++){ if (ss[i].classList.contains('active')) return i; } return _curSlide; }
function goTo(i){
  const total = document.querySelectorAll('.slide').length;
  _curSlide = Math.max(0, Math.min(i, total-1));
  go(_curSlide);
  const navItems = document.querySelectorAll('.nav-item');
  if (navItems[_curSlide]) navItems[_curSlide].scrollIntoView({block:'nearest'});
}
document.addEventListener('keydown', e => {
  if (document.getElementById('lb').classList.contains('on')) {
    if (e.key === 'Escape') closeLb();
    else if (e.key === '+' || e.key === '=') { e.preventDefault(); lbZoom(1); }
    else if (e.key === '-' || e.key === '_') { e.preventDefault(); lbZoom(-1); }
    else if (e.key === '0') { lbReset(); }
    return;
  }
  if (e.key === 'ArrowRight' || e.key === 'ArrowDown') { e.preventDefault(); goTo(_liveSlide() + 1); }
  else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') { e.preventDefault(); goTo(_liveSlide() - 1); }
});
document.querySelectorAll('.img-item img,.ti-img-item img').forEach(img=>{img.style.cursor='zoom-in';img.onclick=()=>openLb(img)});
var _lbS=1,_lbX=0,_lbY=0,_lbDrag=false,_lbPX=0,_lbPY=0,_lbPinch=0;
function _lbApply(){var im=document.getElementById('lb-img');im.style.transform='translate('+_lbX+'px,'+_lbY+'px) scale('+_lbS+')';var p=document.getElementById('lb-pct');if(p)p.textContent=Math.round(_lbS*100)+'%'}
function openLb(img){var src=(img&&img.src)?img.src:img;document.getElementById('lb-img').src=src;_lbS=1;_lbX=0;_lbY=0;_lbApply();document.getElementById('lb').classList.add('on')}
function closeLb(){document.getElementById('lb').classList.remove('on')}
function lbZoom(dir){var ns=Math.min(8,Math.max(.2,_lbS*(dir>0?1.25:1/1.25)));_lbS=ns;_lbApply()}
function lbReset(){_lbS=1;_lbX=0;_lbY=0;_lbApply()}
(function(){var st=document.getElementById('lb-stage');if(!st)return;
st.addEventListener('wheel',function(e){e.preventDefault();var r=st.getBoundingClientRect();var ox=e.clientX-r.left-r.width/2,oy=e.clientY-r.top-r.height/2;var pre=_lbS;var ns=Math.min(8,Math.max(.2,_lbS*(e.deltaY<0?1.15:1/1.15)));var k=ns/pre;_lbX=ox-(ox-_lbX)*k;_lbY=oy-(oy-_lbY)*k;_lbS=ns;_lbApply()},{passive:false});
st.addEventListener('mousedown',function(e){_lbDrag=true;_lbPX=e.clientX;_lbPY=e.clientY;st.style.cursor='grabbing'});
window.addEventListener('mousemove',function(e){if(!_lbDrag)return;_lbX+=e.clientX-_lbPX;_lbY+=e.clientY-_lbPY;_lbPX=e.clientX;_lbPY=e.clientY;_lbApply()});
window.addEventListener('mouseup',function(){_lbDrag=false;st.style.cursor='grab'});
document.getElementById('lb-img').addEventListener('dblclick',function(e){e.stopPropagation();if(_lbS>1){lbReset()}else{_lbS=2.2;_lbApply()}});
st.addEventListener('touchstart',function(e){if(e.touches.length===2){_lbPinch=Math.hypot(e.touches[0].clientX-e.touches[1].clientX,e.touches[0].clientY-e.touches[1].clientY)}else if(e.touches.length===1){_lbDrag=true;_lbPX=e.touches[0].clientX;_lbPY=e.touches[0].clientY}},{passive:false});
st.addEventListener('touchmove',function(e){if(e.touches.length===2){e.preventDefault();var d=Math.hypot(e.touches[0].clientX-e.touches[1].clientX,e.touches[0].clientY-e.touches[1].clientY);if(_lbPinch){_lbS=Math.min(8,Math.max(.2,_lbS*(d/_lbPinch)));_lbApply()}_lbPinch=d}else if(e.touches.length===1&&_lbDrag){e.preventDefault();_lbX+=e.touches[0].clientX-_lbPX;_lbY+=e.touches[0].clientY-_lbPY;_lbPX=e.touches[0].clientX;_lbPY=e.touches[0].clientY;_lbApply()}},{passive:false});
st.addEventListener('touchend',function(e){if(e.touches.length<2)_lbPinch=0;if(e.touches.length===0)_lbDrag=false});
})();
(function(){var c=document.getElementById('bgfx');if(!c)return;var x=c.getContext('2d'),w,h,ps;function rs(){w=c.width=innerWidth;h=c.height=innerHeight}rs();window.addEventListener('resize',rs);ps=[];for(var i=0;i<20;i++)ps.push({x:Math.random()*w,y:Math.random()*h,r:Math.random()*1.2+.3,vy:-(Math.random()*.14+.03),vx:(Math.random()-.5)*.08,a:Math.random()*.14+.05});function tick(){x.clearRect(0,0,w,h);for(var i=0;i<ps.length;i++){var p=ps[i];p.y+=p.vy;p.x+=p.vx;if(p.y<-6){p.y=h+6;p.x=Math.random()*w}var g=x.createRadialGradient(p.x,p.y,0,p.x,p.y,p.r*4);g.addColorStop(0,'rgba(227,192,122,'+p.a+')');g.addColorStop(1,'rgba(227,192,122,0)');x.fillStyle=g;x.beginPath();x.arc(p.x,p.y,p.r*4,0,7);x.fill()}requestAnimationFrame(tick)}tick()})();
function toggleMobNav(){document.querySelector('.sidebar').classList.toggle('mob-open');document.getElementById('mobOverlay').classList.toggle('mob-open')}
function closeMobNav(){document.querySelector('.sidebar').classList.remove('mob-open');document.getElementById('mobOverlay').classList.remove('mob-open')}
// 交互统一走事件委托：部分查看环境（内嵌预览等）的 CSP 会禁用内联 onclick 属性。
document.addEventListener('click', function(e){
  var nav = e.target.closest ? e.target.closest('.nav-item[data-goto]') : null;
  if (nav) { goTo(parseInt(nav.dataset.goto, 10) || 0); if (window.innerWidth <= 768) closeMobNav(); return; }
  var pb = e.target.closest ? e.target.closest('.pri-btn') : null;
  if (pb) { filterPri(pb.dataset.priFilter || ''); return; }
  var lbBtn = e.target.closest ? e.target.closest('.lb-bt') : null;
  if (lbBtn) {
    var act = lbBtn.dataset.lb;
    if (act === 'minus') lbZoom(-1);
    else if (act === 'plus') lbZoom(1);
    else if (act === 'reset') lbReset();
    else if (act === 'close') closeLb();
  }
});
document.getElementById('mobNavBtn').addEventListener('click', toggleMobNav);
document.getElementById('mobOverlay').addEventListener('click', closeMobNav);
(function(){var tx=0,ty=0;var m=document.getElementById('main');m.addEventListener('touchstart',function(e){tx=e.touches[0].clientX;ty=e.touches[0].clientY},{passive:true});m.addEventListener('touchend',function(e){var dx=e.changedTouches[0].clientX-tx,dy=e.changedTouches[0].clientY-ty;if(Math.abs(dx)>Math.abs(dy)&&Math.abs(dx)>55){dx<0?goTo(_liveSlide()+1):goTo(_liveSlide()-1)}},{passive:true})})();
</script>
</body></html>"""
