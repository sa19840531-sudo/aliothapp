"""糖果短剧：无状态辨识 Tool（不操作设备、不保留对话）。

优先从候选／文案／页型推断；也可校验外部 VLM JSON（同一约束契约）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from services.api.app.application.app_constrained_locate import (
    ConstrainedLocateError,
    ConstrainedLocateResult,
    center_from_normalized_bounds,
    validate_constrained_locate,
)
from services.api.app.domain.exploration import CandidateAction, ExplorationWork
from services.api.app.domain.interaction import InteractionOperation, InteractionRiskLevel

_VIDEO_TARGETS = ("watch_now", "fresh_card", "video_entry")
_TAB_TARGETS = ("tab_home", "tab_library", "tab_vote", "tab_topic", "tab_mine")
_WALLET_TARGETS = ("wallet", "balance", "recharge", "pay_entry")
_ALIPAY_TARGETS = ("alipay",)
_PAGE_TARGETS = (
    "home",
    "video_detail",
    "player",
    "paywall",
    "login_gate",
    "load_error",
    "profile",
    "recharge_page",
    "order_confirm",
    "alipay_cashier",
    "comic_detail",
    "comic_reader",
    "unknown",
)
_TOP_TAB_TARGETS = ("top_tab",)
_COMIC_TARGETS = ("comic_entry", "comic_title", "start_read", "chapter")
_COMIC_MARKERS = ("漫画", "漫畫", "comic", "manga")
_START_READ_MARKERS = (
    "开始阅读",
    "開始閱讀",
    "立即阅读",
    "立即閱讀",
    "继续阅读",
    "繼續閱讀",
    "阅读",
    "閱讀",
)
_TOP_TAB_SKIP = (
    "搜索",
    "搜尋",
    "签到",
    "簽到",
    "余额",
    "餘額",
    "金币",
    "廣告",
    "广告",
)
_DEFAULT_TOP_TAB_HINTS = (
    "推荐",
    "推薦",
    "原创",
    "原創",
    "短剧",
    "短劇",
    "漫画",
    "漫畫",
    "最新",
    "热门",
    "熱門",
)

# 不含「最新」／短英文 watch|play：会与上方分类「最新」撞名，或误命中 display／player。
_WATCH_MARKERS = ("立即看", "立即觀看", "今日新剧", "今日新劇")
_WALLET_MARKERS = (
    "钱包",
    "錢包",
    "我的钱包",
    "我的錢包",
    "余额",
    "餘額",
    "储值",
    "儲值",
    "充值",
    "支付",
)
_RECHARGE_MARKERS = ("充值", "储值", "儲值", "开通会员", "金币充值")
_WITHDRAW_MARKERS = ("提现", "提現", "交易记录", "交易紀錄", "账单", "帳單")
_ALIPAY_MARKERS = ("支付宝", "支付寶", "Alipay", "alipay")
_WECHAT_MARKERS = ("微信", "微信支付", "WeChat", "wechat", "Weixin")
_MORE_PAY_MARKERS = ("更多支付", "更多方式", "其他支付")
_ORDER_CONFIRM_STRICT = ("订单确认", "訂單確認")
_CONFIRM_PAY_MARKERS = ("确认支付", "確認支付")


def select_semantic_video_candidate(
    work: ExplorationWork,
    *,
    title: str | None = None,
    keywords: Sequence[str] = (),
    attempted_labels: Sequence[str] = (),
) -> CandidateAction | None:
    """按当前帧语义选片，保留候选 ID，由执行层解析视觉坐标。"""
    preferred = tuple(value for value in (title, *keywords) if value)
    ranked: list[tuple[int, CandidateAction]] = []
    for candidate in _current_frame_candidates(work):
        if candidate.action_type not in {InteractionOperation.CLICK, InteractionOperation.TAP}:
            continue
        if candidate.risk_level is not InteractionRiskLevel.LOW:
            continue
        source = candidate.metadata.get("source_capture_id")
        if source and source != work.source_capture_id:
            continue
        label = str(candidate.semantic_target or "").strip()
        if not label or label in attempted_labels:
            continue
        if any(candidate.metadata.get(flag) is True for flag in (
            "home_top_category_tab_seek", "bottom_navigation_tab_seek", "home_category_grid_seek",
        )):
            continue
        blob = _candidate_blob(candidate)
        if any(word in blob for word in (
            "上方分类", "上方分類", "签到", "追剧", "追劇", "搜索", "搜尋",
        )):
            continue
        expected = str(candidate.metadata.get("expected_next_page_type")
                       or candidate.metadata.get("expected_page_type")
                       or candidate.metadata.get("expected_state_hint") or "").casefold()
        watch = any(word in label for word in ("立即看", "立即觀看"))
        detail = expected in {"video_detail", "player", "content_detail"}
        if not watch and not detail:
            continue
        # 明确播放入口优先于置信度略高的海报；指定片名优先。
        score = (200 if any(word in blob for word in preferred) else 0)
        score += 100 if watch else 0
        score += min(99, max(0, int(candidate.exploration_value)))
        ranked.append((score, candidate))
    return max(ranked, key=lambda item: item[0])[1] if ranked else None


def recognize_video_entry(
    work: ExplorationWork,
    *,
    title: str | None = None,
    keywords: Sequence[str] = (),
    attempted_labels: Sequence[str] = (),
) -> ConstrainedLocateResult:
    attempted = {str(item).strip() for item in attempted_labels if str(item or "").strip()}
    preferred = [str(title).strip()] if title and str(title).strip() else []
    preferred.extend(str(item).strip() for item in keywords if str(item or "").strip())
    scored: list[tuple[int, CandidateAction, tuple[float, float]]] = []
    for candidate in _current_frame_candidates(work):
        if candidate.action_type not in {
            InteractionOperation.CLICK,
            InteractionOperation.TAP,
        }:
            continue
        # 顶栏／底栏寻页不得充当影片入口。
        if (
            candidate.metadata.get("home_top_category_tab_seek") is True
            or candidate.metadata.get("bottom_navigation_tab_seek") is True
            or candidate.metadata.get("home_category_grid_seek") is True
        ):
            continue
        label = str(candidate.semantic_target or "").strip()
        if label in attempted:
            continue
        blob = _candidate_blob(candidate)
        # 「上方分类」一律排除：不得因同屏「今日新剧」等 watch 词绕过。
        if "上方分类" in blob or "上方分類" in blob:
            continue
        # 顶栏功能钮排除；「会员」仅作剧卡角标时不得整卡丢弃（QC：今日新剧多卡带会员标却全不点）。
        if any(m in blob for m in (
            "签到", "追劇", "追剧", "搜索", "搜尋", "金币", "金幣",
        )) and not any(
            m in blob for m in _WATCH_MARKERS
        ):
            continue
        if label.strip() in {"会员", "會員", "开通会员", "開通會員"} and not any(
            m in blob for m in _WATCH_MARKERS
        ):
            continue
        center = center_from_normalized_bounds(
            candidate.metadata.get("normalized_bounds")
            if isinstance(candidate.metadata.get("normalized_bounds"), Mapping)
            else None
        )
        if center is None:
            continue
        # 上半屏且文案命中分类豆（最新／推荐／原创…）：顶栏不是影片卡。
        if center[1] <= 0.28 and _label_looks_like_top_tab(label):
            continue
        score = 0
        for marker in preferred:
            if marker and marker in blob:
                score += 50
        for marker in _WATCH_MARKERS:
            if marker.casefold() in blob.casefold():
                score += 30 if marker in {"立即看", "立即觀看"} else 15
        expected = str(candidate.metadata.get("expected_page_type") or "").casefold()
        if expected in {"video_detail", "content_detail"}:
            score += 20
        if score <= 0 and not preferred:
            # 无配置时：仍可收带坐标的低分影片卡（首页相对第一卡／海报带）
            if candidate.metadata.get("forced_pixel_home_content_entry") is True:
                score = 10
            elif candidate.metadata.get("home_content_entry_seek") is True:
                score = 10
            elif expected in {"video_detail", "content_detail"}:
                score = 12
            elif _looks_like_home_poster_card(label=label, center=center):
                score = 8
        if score > 0:
            scored.append((score + int(candidate.exploration_value), candidate, center))
    if not scored:
        # 语义候选皆空：回落相对第一卡（与 MAF home_content_entry 合同一致）。
        fallback_label = "首页今日新剧第一片"
        if fallback_label not in attempted:
            relative = _home_video_first_card_coordinate(work)
            if relative is not None:
                return ConstrainedLocateResult(
                    status="found",
                    target="video_entry",
                    label=fallback_label,
                    coordinate=relative,
                    reason="semantic_watch_or_first_card",
                )
        return ConstrainedLocateResult(
            status="not_found",
            target=None,
            label=None,
            coordinate=None,
            reason=None,
        )
    scored.sort(key=lambda item: item[0], reverse=True)
    _, chosen, center = scored[0]
    blob = _candidate_blob(chosen)
    target = (
        "watch_now"
        if any(marker in blob for marker in ("立即看", "立即觀看"))
        else "video_entry"
    )
    return ConstrainedLocateResult(
        status="found",
        target=target if target in _VIDEO_TARGETS else "video_entry",
        label=str(chosen.semantic_target or "").strip() or None,
        coordinate=center,
        reason=None,
    )


@dataclass(frozen=True, kw_only=True)
class PageClassifyResult:
    """页面状态独立严格 schema（不必套坐标定位格式）。"""

    status: str
    page: str
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"found", "not_found", "uncertain"} or self.page not in _PAGE_TARGETS:
            raise ConstrainedLocateError("invalid page classification")

    def as_dict(self) -> dict[str, object]:
        return {"status": self.status, "page": self.page, "reason": self.reason}


def recognize_page_state(work: ExplorationWork) -> PageClassifyResult:
    observation = work.frontier.observation
    page_type = str(observation.page_type or "").casefold()
    page_state = str(observation.page_state or "").casefold()
    blob = _frame_text_blob(work)
    if page_type in {"home"} and page_state not in {"popup", "modal", "dialog", "startup_ad"}:
        return PageClassifyResult(status="found", page="home")
    if page_type in {"video_detail", "content_detail"}:
        return PageClassifyResult(status="found", page="video_detail")
    if page_type in {"player", "video_player", "playback"}:
        return PageClassifyResult(status="found", page="player")
    # 阅读页须有阅读内容证据；详情介绍页不算阅读成功
    if page_type in {"comic_reader", "reader", "manga_reader"} or (
        any(m in blob for m in ("第", "话", "話", "页", "頁"))
        and any(m in blob for m in _COMIC_MARKERS)
        and any(m in blob for m in ("阅读中", "閱讀中", "上一话", "下一话", "上一話", "下一話"))
    ):
        return PageClassifyResult(status="found", page="comic_reader")
    if page_type in {"comic_detail", "manga_detail"} or (
        any(m in blob for m in _COMIC_MARKERS)
        and any(m in blob for m in _START_READ_MARKERS + ("目录", "目錄", "章节", "章節"))
        and "阅读中" not in blob
        and "閱讀中" not in blob
    ):
        return PageClassifyResult(status="found", page="comic_detail")
    # 支付宝收银台／订单确认优先于泛化 paywall，避免误判付款页。
    if any(m in blob for m in _ALIPAY_MARKERS) and any(
        m in blob for m in ("收银台", "收銀臺", "支付宝收银台")
    ):
        return PageClassifyResult(status="found", page="alipay_cashier")
    # 钱包／充值页底部常有「确认支付」+ 微信｜支付宝并排；不得当成独立订单确认而提前收口。
    channel_row = _has_payment_channel_row(blob)
    if any(m in blob for m in _ORDER_CONFIRM_STRICT):
        return PageClassifyResult(status="found", page="order_confirm")
    if any(m in blob for m in _CONFIRM_PAY_MARKERS) and not channel_row:
        return PageClassifyResult(status="found", page="order_confirm")
    if channel_row and (
        page_type in {"payment", "wallet", "recharge", "recharge_page"}
        or any(m in blob for m in _RECHARGE_MARKERS + ("钱包", "錢包", "我的钱包", "我的錢包"))
    ):
        return PageClassifyResult(status="found", page="recharge_page")
    if page_type in {"payment", "paywall"} or any(
        m in blob for m in ("开通会员", "立即开通", "付费")
    ):
        return PageClassifyResult(status="found", page="paywall")
    if page_type in {"login", "auth"} or any(m in blob for m in ("登录", "登入", "验证码")):
        return PageClassifyResult(status="found", page="login_gate")
    if any(m in blob for m in ("加载失败", "網路異常", "网络异常", "重试")):
        return PageClassifyResult(status="found", page="load_error")
    if page_type in {"profile", "mine"}:
        return PageClassifyResult(status="found", page="profile")
    if any(m in blob for m in _RECHARGE_MARKERS) and "提现" not in blob and "提現" not in blob:
        return PageClassifyResult(status="found", page="recharge_page")
    if page_state in {"popup", "modal", "overlay"}:
        return PageClassifyResult(
            status="uncertain",
            page="unknown",
            reason="obstruction_or_loading",
        )
    return PageClassifyResult(
        status="uncertain",
        page="unknown",
        reason="page_unrecognized",
    )


@dataclass(frozen=True, kw_only=True)
class TopTabItem:
    """上方分类清单项（独立 schema，非单点定位）。"""

    tab_id: str
    label: str
    selected: bool
    coordinate: tuple[float, float] | None
    is_comic: bool = False
    candidate_id: str | None = None

    def __post_init__(self) -> None:
        if not self.tab_id or not self.label or type(self.selected) is not bool:
            raise ConstrainedLocateError("invalid top tab")

    def as_dict(self) -> dict[str, object]:
        return {
            "tab_id": self.tab_id,
            "label": self.label,
            "selected": self.selected,
            "coordinate": (
                [self.coordinate[0], self.coordinate[1]]
                if self.coordinate is not None
                else None
            ),
            "is_comic": self.is_comic,
            "candidate_id": self.candidate_id,
        }


def recognize_top_tabs(work: ExplorationWork) -> list[TopTabItem]:
    """辨识首页上方可切换分类；排除搜索／签到／余额／广告／内容卡。"""
    items: list[TopTabItem] = []
    seen: set[str] = set()
    for candidate in _current_frame_candidates(work):
        if candidate.action_type not in {
            InteractionOperation.CLICK,
            InteractionOperation.TAP,
        }:
            continue
        blob = _candidate_blob(candidate)
        label = str(candidate.semantic_target or candidate.metadata.get("text") or "").strip()
        if not label:
            continue
        if any(m in blob for m in _TOP_TAB_SKIP):
            continue
        # 上方分类带标记，或文案命中常见分类且位于上半屏
        is_top = candidate.metadata.get("home_top_category_tab_seek") is True or any(
            marker in label for marker in ("上方分类", "上方分類")
        )
        center = center_from_normalized_bounds(
            candidate.metadata.get("normalized_bounds")
            if isinstance(candidate.metadata.get("normalized_bounds"), Mapping)
            else None
        )
        if not is_top:
            if not any(h in label or h in blob for h in _DEFAULT_TOP_TAB_HINTS):
                continue
            if center is None or center[1] > 0.28:
                continue
        tab_id = str(
            candidate.metadata.get("resource_id")
            or candidate.metadata.get("accessibility_id")
            or candidate.candidate_id
            or label
        ).strip()
        dedupe_key = label.casefold()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        selected = (
            candidate.metadata.get("selected") is True
            or "已选" in blob
            or "已選" in blob
            or "selected" in blob.casefold()
        )
        is_comic = any(m in label or m in blob for m in _COMIC_MARKERS)
        items.append(
            TopTabItem(
                tab_id=tab_id,
                label=label,
                selected=selected,
                coordinate=center,
                is_comic=is_comic,
                candidate_id=candidate.candidate_id,
            )
        )
    # 左到右
    items.sort(
        key=lambda item: item.coordinate[0] if item.coordinate is not None else 1.0
    )
    return items


def recognize_comic_entry(
    work: ExplorationWork,
    *,
    title: str | None = None,
    attempted_labels: Sequence[str] = (),
) -> ConstrainedLocateResult:
    attempted = {str(x).strip() for x in attempted_labels if str(x or "").strip()}
    preferred = str(title or "").strip()
    scored: list[tuple[int, CandidateAction, tuple[float, float]]] = []
    for candidate in _current_frame_candidates(work):
        if any(candidate.metadata.get(flag) is True for flag in (
            "home_top_category_tab_seek", "bottom_navigation_tab_seek",
        )):
            continue
        if candidate.action_type not in {
            InteractionOperation.CLICK,
            InteractionOperation.TAP,
        }:
            continue
        label = str(candidate.semantic_target or "").strip()
        if label in attempted:
            continue
        blob = _candidate_blob(candidate)
        expected = candidate.metadata.get("expected_next_page_type") or candidate.metadata.get(
            "expected_state_hint"
        )
        if (not any(m in blob or m in label for m in _COMIC_MARKERS) and not preferred
                and expected not in {"comic_detail", "manga_detail", "comic_reader"}):
            # 无漫画关键字时仍可收明确漫画卡元数据
            if candidate.metadata.get("comic_entry_seek") is not True:
                continue
        if preferred and preferred not in blob and preferred not in label:
            if candidate.metadata.get("comic_entry_seek") is not True:
                continue
        center = center_from_normalized_bounds(
            candidate.metadata.get("normalized_bounds")
            if isinstance(candidate.metadata.get("normalized_bounds"), Mapping)
            else None
        )
        score = 10
        if preferred and (preferred in blob or preferred in label):
            score += 50
        if any(m in blob for m in _COMIC_MARKERS):
            score += 20
        if candidate.metadata.get("comic_entry_seek") is True:
            score += 15
        scored.append((score, candidate, center))
    if not scored:
        return ConstrainedLocateResult(
            status="not_found",
            target=None,
            label=None,
            coordinate=None,
            reason=None,
        )
    scored.sort(key=lambda item: item[0], reverse=True)
    _, chosen, center = scored[0]
    return ConstrainedLocateResult(
        status="found",
        target="comic_entry",
        label=str(chosen.semantic_target or "").strip() or "漫画",
        coordinate=center,
        candidate_id=chosen.candidate_id,
        reason=None,
    )


def recognize_start_read(work: ExplorationWork) -> ConstrainedLocateResult:
    for candidate in _current_frame_candidates(work):
        if candidate.action_type not in {
            InteractionOperation.CLICK,
            InteractionOperation.TAP,
        }:
            continue
        blob = _candidate_blob(candidate)
        if not any(m in blob for m in _START_READ_MARKERS):
            continue
        # 排除购买类
        if any(m in blob for m in ("购买", "購買", "解锁", "解鎖", "开通")):
            continue
        center = center_from_normalized_bounds(
            candidate.metadata.get("normalized_bounds")
            if isinstance(candidate.metadata.get("normalized_bounds"), Mapping)
            else None
        )
        return ConstrainedLocateResult(
            status="found",
            target="start_read",
            label=str(candidate.semantic_target or "开始阅读").strip() or "开始阅读",
            coordinate=center,
            candidate_id=candidate.candidate_id,
            reason=None,
        )
    return ConstrainedLocateResult(
        status="not_found",
        target=None,
        label=None,
        coordinate=None,
        reason=None,
    )


def is_comic_detail_not_reader(work: ExplorationWork) -> bool:
    """详情介绍页不得冒充阅读页。"""
    classified = recognize_page_state(work)
    return classified.page == "comic_detail"


def playback_progress_advanced(work: ExplorationWork) -> bool:
    """可观测播放进度前进证据；静态画面不算播放成功。"""
    meta = work.frontier.observation.metadata
    if not isinstance(meta, Mapping):
        return False
    if meta.get("playback_progress_advanced") is True:
        return True
    facts = meta.get("surface_facts")
    if isinstance(facts, Mapping):
        if facts.get("playback_progress_advanced") is True:
            return True
        before = facts.get("playback_position_before")
        after = facts.get("playback_position_after")
        try:
            if before is not None and after is not None and float(after) > float(before):
                return True
        except (TypeError, ValueError):
            pass
    return False


def recognize_bottom_tab(
    work: ExplorationWork,
    *,
    wanted_label: str,
    aliases: Sequence[str],
) -> ConstrainedLocateResult:
    from services.api.app.application.app_flow_navigation import tab_label

    markers = {tab_label(value).casefold() for value in (wanted_label, *aliases) if value}
    # 优先采用当前帧明确标注的底栏；短标签「我」不得命中「我的收藏」等正文。
    candidates = sorted(
        _current_frame_candidates(work),
        key=lambda item: item.metadata.get("bottom_navigation_tab_seek") is not True,
    )
    for candidate in candidates:
        if candidate.action_type not in {
            InteractionOperation.CLICK,
            InteractionOperation.TAP,
        }:
            continue
        label = tab_label(candidate.semantic_target or candidate.metadata.get("text"))
        if label.casefold() not in markers:
            continue
        center = center_from_normalized_bounds(
            candidate.metadata.get("normalized_bounds")
            if isinstance(candidate.metadata.get("normalized_bounds"), Mapping)
            else None
        )
        target = _tab_target_for_label(wanted_label)
        return ConstrainedLocateResult(
            status="found",
            target=target,
            label=wanted_label,
            coordinate=center,
            candidate_id=candidate.candidate_id,
            reason=None,
        )
    return ConstrainedLocateResult(
        status="not_found",
        target=None,
        label=wanted_label,
        coordinate=None,
        reason=None,
    )


def recognize_wallet_entry(work: ExplorationWork) -> ConstrainedLocateResult:
    for candidate in _current_frame_candidates(work):
        if candidate.action_type not in {
            InteractionOperation.CLICK,
            InteractionOperation.TAP,
        }:
            continue
        blob = _candidate_blob(candidate)
        if any(m in blob for m in _WITHDRAW_MARKERS):
            continue
        hit = next((m for m in _WALLET_MARKERS if m in blob), None)
        if hit is None:
            continue
        center = center_from_normalized_bounds(
            candidate.metadata.get("normalized_bounds")
            if isinstance(candidate.metadata.get("normalized_bounds"), Mapping)
            else None
        )
        if any(m in blob for m in _RECHARGE_MARKERS):
            target = "recharge"
        elif "余额" in blob or "餘額" in blob:
            target = "balance"
        elif "支付" in blob:
            target = "pay_entry"
        else:
            target = "wallet"
        return ConstrainedLocateResult(
            status="found",
            target=target,
            label=str(candidate.semantic_target or hit).strip() or hit,
            coordinate=center,
            candidate_id=candidate.candidate_id,
            reason=None,
        )
    return ConstrainedLocateResult(
        status="not_found",
        target=None,
        label=None,
        coordinate=None,
        reason=None,
    )


def recognize_alipay(
    work: ExplorationWork,
    *,
    require_selected: bool = False,
) -> ConstrainedLocateResult:
    blob_frame = _frame_text_blob(work)
    available = any(m in blob_frame for m in _ALIPAY_MARKERS)
    channel_row = _has_payment_channel_row(blob_frame)
    for candidate in _current_frame_candidates(work):
        if candidate.action_type not in {
            InteractionOperation.CLICK,
            InteractionOperation.TAP,
        }:
            continue
        blob = _candidate_blob(candidate)
        if not any(m in blob for m in _ALIPAY_MARKERS):
            continue
        center = center_from_normalized_bounds(
            candidate.metadata.get("normalized_bounds")
            if isinstance(candidate.metadata.get("normalized_bounds"), Mapping)
            else None
        )
        selected = (
            candidate.metadata.get("selected") is True
            or "已选" in blob
            or "已選" in blob
            or "selected" in blob.casefold()
        )
        if require_selected and not selected:
            continue
        return ConstrainedLocateResult(
            status="found",
            target="alipay",
            label=str(candidate.semantic_target or "支付宝").strip() or "支付宝",
            coordinate=center,
            candidate_id=candidate.candidate_id,
            reason="selected" if selected else None,
        )
    # 语意图标 + 相对槽：并排渠道行缺坐标时回落；「更多支付」折叠场景留给展开逻辑。
    more_collapsed = any(m in blob_frame for m in _MORE_PAY_MARKERS) and not channel_row
    if not require_selected and not more_collapsed:
        relative = _alipay_relative_coordinate(work, blob_frame=blob_frame)
        if relative is not None:
            return ConstrainedLocateResult(
                status="found",
                target="alipay",
                label="支付宝",
                coordinate=relative,
                reason="semantic_icon_relative",
            )
    if available and require_selected:
        return ConstrainedLocateResult(
            status="uncertain",
            target=None,
            label=None,
            coordinate=None,
            reason="alipay_seen_but_unconfirmed",
        )
    if available:
        # 文案可见但无坐标：uncertain，主流程可展开「更多」
        if any(m in blob_frame for m in _MORE_PAY_MARKERS):
            return ConstrainedLocateResult(
                status="uncertain",
                target=None,
                label=None,
                coordinate=None,
                reason="alipay_may_be_under_more",
            )
        return ConstrainedLocateResult(
            status="uncertain",
            target=None,
            label=None,
            coordinate=None,
            reason="alipay_text_without_coordinate",
        )
    if channel_row and not require_selected:
        # 仅见微信侧文案／图标行时，仍允许右槽相对点支付宝。
        relative = _alipay_relative_coordinate(work, blob_frame=blob_frame)
        if relative is not None:
            return ConstrainedLocateResult(
                status="found",
                target="alipay",
                label="支付宝",
                coordinate=relative,
                reason="semantic_icon_relative",
            )
    return ConstrainedLocateResult(
        status="not_found",
        target=None,
        label=None,
        coordinate=None,
        reason=None,
    )


def is_recharge_not_withdraw(work: ExplorationWork) -> bool:
    blob = _frame_text_blob(work)
    if any(m in blob for m in _WITHDRAW_MARKERS) and not any(
        m in blob for m in _RECHARGE_MARKERS
    ):
        return False
    return any(m in blob for m in _RECHARGE_MARKERS) or any(
        m in blob for m in ("钱包", "錢包")
    )


def parse_external_locate(
    payload: object,
    *,
    allowed_targets: Sequence[str],
) -> ConstrainedLocateResult | None:
    """校验外部 VLM JSON；失败返回 None（主流程不得点击）。"""

    try:
        return validate_constrained_locate(payload, allowed_targets=allowed_targets)
    except ConstrainedLocateError:
        return None


def _tab_target_for_label(label: str) -> str:
    from services.api.app.application.app_flow_navigation import tab_label, tab_role

    mapping = {
        "home": "tab_home",
        "library": "tab_library",
        "vote": "tab_vote",
        "topic": "tab_topic",
        "profile": "tab_mine",
        "darkweb": "tab_darkweb",
        "ai": "tab_ai",
        "community": "tab_community",
    }
    return mapping.get(tab_role(tab_label(label)), "tab_other")


def _label_looks_like_top_tab(label: str) -> bool:
    """语义名是否为上方分类豆（含「上方分类「最新」」包裹）。"""

    text = str(label or "").strip()
    if not text:
        return False
    if text.startswith("上方分类") or text.startswith("上方分類"):
        return True
    bare = text
    for prefix in ("上方分类「", "上方分類「"):
        if bare.startswith(prefix) and bare.endswith("」"):
            bare = bare[len(prefix) : -1].strip()
            break
    return bare in _DEFAULT_TOP_TAB_HINTS


def _looks_like_home_poster_card(
    *,
    label: str,
    center: tuple[float, float],
) -> bool:
    """首页中带海报区：有标题文案、避开顶栏／底栏／分类豆。"""

    x, y = center
    if not (0.02 <= x <= 0.98 and 0.42 <= y <= 0.84):
        return False
    text = str(label or "").strip()
    if not text or len(text) < 2:
        return False
    if _label_looks_like_top_tab(text):
        return False
    if text in {
        "首页",
        "首頁",
        "剧库",
        "劇庫",
        "投票",
        "专题",
        "專題",
        "我的",
        "查看全部",
        "今日新剧",
        "今日新劇",
        "FRESH",
    }:
        return False
    return True


def _home_video_first_card_coordinate(
    work: ExplorationWork,
) -> tuple[float, float] | None:
    """课纲 first_card 相对区中心：语义入口皆空时的强制回落。"""

    from services.api.app.application.app_ui_tree_features import (
        home_content_entry_first_card_bounds,
    )

    bounds = home_content_entry_first_card_bounds(
        package_name=_package_name_from_work(work),
        playbook_id="tangguo",
    )
    return center_from_normalized_bounds(bounds)


def _candidate_blob(candidate: CandidateAction) -> str:
    return " ".join(
        str(item or "")
        for item in (
            candidate.semantic_target,
            candidate.metadata.get("purpose"),
            candidate.metadata.get("text"),
            candidate.metadata.get("content_desc"),
            candidate.metadata.get("label"),
            candidate.metadata.get("expected_state_hint"),
        )
    )


def _frame_text_blob(work: ExplorationWork) -> str:
    observation = work.frontier.observation
    meta = observation.metadata if isinstance(observation.metadata, Mapping) else {}
    parts = [
        str(observation.page_type or ""),
        str(observation.page_state or ""),
        str(meta.get("visual_summary") or ""),
    ]
    for candidate in _current_frame_candidates(work):
        parts.append(_candidate_blob(candidate))
    return " ".join(parts)


def _has_payment_channel_row(blob: str) -> bool:
    """画面是否呈现微信｜支付宝并排渠道行（含仅文案／图标提示）。"""

    text = str(blob or "")
    has_wechat = any(m in text for m in _WECHAT_MARKERS)
    has_alipay = any(m in text for m in _ALIPAY_MARKERS)
    return has_wechat and has_alipay


def _package_name_from_work(work: ExplorationWork) -> str | None:
    meta = work.frontier.observation.metadata
    if not isinstance(meta, Mapping):
        return None
    pkg = meta.get("package_name")
    if isinstance(pkg, str) and pkg.strip():
        return pkg.strip()
    return None


def _alipay_relative_coordinate(
    work: ExplorationWork,
    *,
    blob_frame: str,
) -> tuple[float, float] | None:
    """支付渠道行：用语意图标相对槽取支付宝点击中心。"""

    page_type = str(work.frontier.observation.page_type or "").casefold()
    on_pay_surface = page_type in {
        "payment",
        "wallet",
        "recharge",
        "recharge_page",
        "paywall",
    }
    # 必须有渠道语义线索才用相对槽，避免空白支付页误点。
    allow = (
        _has_payment_channel_row(blob_frame)
        or any(m in blob_frame for m in _ALIPAY_MARKERS)
        or (
            on_pay_surface
            and any(m in blob_frame for m in _WECHAT_MARKERS)
        )
    )
    if not allow:
        return None
    from services.api.app.application.app_ui_tree_features import payment_channel_tap_bounds

    # 本模块专属糖果课纲；无包名时仍按 tangguo playbook 取相对槽。
    bounds = payment_channel_tap_bounds(
        "支付宝",
        package_name=_package_name_from_work(work),
        playbook_id="tangguo",
    )
    return center_from_normalized_bounds(bounds)


def _current_frame_candidates(work: ExplorationWork):
    """只读取当前截图的低风险候选，旧帧候选不能形成选中或定位证据。"""
    return tuple(
        item for item in work.frontier.candidates
        if item.risk_level is InteractionRiskLevel.LOW
        and item.metadata.get("source_capture_id", work.source_capture_id) == work.source_capture_id
    )
