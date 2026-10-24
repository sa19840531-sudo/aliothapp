"""从 Catalog 编译 App 视觉用的版本化页型判别指南（影子输入，不改正式页型权威）。"""

from __future__ import annotations

from collections.abc import Sequence

from services.api.app.domain.page.registry import PageTypeRegistry, get_default_page_type_registry
from services.api.app.domain.protocol import stable_protocol_hash

APP_PAGE_DISCRIMINATION_GUIDE_VERSION = "app-page-discrimination-guide.v2"

# 相邻易混页型：指南中显式消歧，避免仅靠代码列表。
_CONFUSABLE_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("home", "content_list", "首页是入口导航；内容列表是可重复卡片流，不能因有菜单就判成列表。"),
    ("content_list", "video_list", "内容列表主体非视频；视频列表需重复视频卡片或明确视频入口。"),
    ("content_detail", "video_detail", "内容详情是单一非视频主体；视频详情必须有播放器或视频资源结构。"),
    ("content_list", "content_detail", "列表是多卡片；详情是单一主体，不能把列表首卡当详情。"),
    ("profile", "settings", "个人中心是身份凭证入口；设置是资料编辑/偏好结果页。"),
    ("startup_ad", "home", "开屏广告是瞬态层；关不掉也不能把广告底当业务首页。"),
    ("activity", "home", "活动/外链落地不等于 App 业务首页。"),
    ("auth_gate", "home", "登录/年龄门是阻断层；不得把门后未知底页写成已到达首页。"),
    ("permission_dialog", "home", "系统权限弹层不是业务首页；未关闭前业务能力未确认。"),
    ("home", "profile", "底栏「我的」入口可见不等于已进入个人中心页。"),
)


def build_app_page_discrimination_guide(
    registry: PageTypeRegistry | None = None,
    *,
    max_entries: int = 16,
) -> dict[str, object]:
    """编译可审计的判别指南载荷，供 App Vision 系统提示与回归断言。"""

    page_types = (registry or get_default_page_type_registry()).catalog.page_types
    entries: list[dict[str, object]] = []
    for item in page_types:
        if item.category != "business" and item.code not in {"startup_ad", "line_loading"}:
            continue
        if len(entries) >= max_entries:
            break
        keywords = tuple(item.signals.ocr_keywords[:6])
        layout = tuple(item.signals.layout_hints[:4])
        entries.append(
            {
                "code": item.code,
                "name": item.name,
                "keywords": list(keywords),
                "layout_hints": list(layout),
                "remark": (item.remark or "").strip()[:160],
                "counter_evidence": _counter_evidence(item.code),
            }
        )
    confusable = [
        {"left": left, "right": right, "rule": rule}
        for left, right, rule in _CONFUSABLE_PAIRS
    ]
    payload = {
        "protocol_version": APP_PAGE_DISCRIMINATION_GUIDE_VERSION,
        "catalog_version": (registry or get_default_page_type_registry()).catalog.version,
        "entries": entries,
        "confusable_pairs": confusable,
        "rules": [
            "页型可修订，但不能单独创建或改写业务页面身份。",
            "被弹窗/广告遮挡时优先判展示层，不得把底层 DOM/列表当当前曝光页型。",
            "证据不足时 visual_page_type=null，禁止编造目录外代码或高置信猜测。",
            "产品没找到某入口不等于能力不存在，不得静默缩小冻结必检分母。",
            "静态产品画像不等于入口后真实界面能力；先观察展示层再确认业务页型。",
            "导航型/广告聚合首页不得强行套成内容列表或详情模板。",
        ],
    }
    return {
        **payload,
        "guide_hash": stable_protocol_hash(payload),
    }


def format_app_page_discrimination_guide_for_prompt(
    guide: dict[str, object] | None = None,
    *,
    max_chars: int = 2800,
) -> str:
    """把指南压成模型可读短文；超长时截断并保留版本与哈希。"""

    payload = guide or build_app_page_discrimination_guide()
    lines = [
        f"判别指南 {payload.get('protocol_version')} / catalog={payload.get('catalog_version')} "
        f"/ hash={str(payload.get('guide_hash') or '')[:16]}",
        "用途：凭什么识别、易混消歧、反证；页型不等于身份。",
    ]
    for rule in payload.get("rules") or ():
        if isinstance(rule, str) and rule.strip():
            lines.append(f"- {rule.strip()}")
    lines.append("类型要点：")
    for entry in payload.get("entries") or ():
        if not isinstance(entry, dict):
            continue
        code = entry.get("code")
        name = entry.get("name")
        keywords = entry.get("keywords") or []
        remark = str(entry.get("remark") or "").strip()
        counter = entry.get("counter_evidence") or []
        kw = "、".join(str(item) for item in keywords[:4] if str(item).strip())
        anti = "、".join(str(item) for item in counter[:3] if str(item).strip())
        piece = f"* {code}（{name}）"
        if kw:
            piece += f" 关键词:{kw}"
        if remark:
            piece += f" 说明:{remark}"
        if anti:
            piece += f" 反证:{anti}"
        lines.append(piece)
    lines.append("易混消歧：")
    for pair in payload.get("confusable_pairs") or ():
        if not isinstance(pair, dict):
            continue
        lines.append(f"* {pair.get('left')} vs {pair.get('right')}：{pair.get('rule')}")
    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 18].rstrip() + "\n…(指南已截断)"


def _counter_evidence(code: str) -> list[str]:
    mapping: dict[str, Sequence[str]] = {
        "home": ("仅有广告素材", "仅有加载转圈", "外链浏览器页"),
        "content_list": ("单一详情主体", "只有底栏无卡片", "纯广告聚合"),
        "video_list": ("无可播放视频卡片", "普通图文列表"),
        "content_detail": ("多卡片列表流", "只有播放器无正文"),
        "video_detail": ("无播放器/无视频资源", "列表多视频卡"),
        "profile": ("他人主播资料详情", "纯设置表单"),
        "settings": ("仅头像昵称入口无编辑项",),
        "startup_ad": ("稳定业务底栏已完全露出且无开屏层",),
        "activity": ("目标包内业务首页",),
    }
    return list(mapping.get(code, ()))
