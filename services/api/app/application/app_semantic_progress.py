"""App 动态探索的跨截图同义动作进度。"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence

from services.api.app.domain.exploration import CandidateAction
from services.api.app.domain.interaction import InteractionOperation
from services.api.app.domain.protocol import stable_protocol_hash

APP_SEMANTIC_ACTION_COUNTS = "app_semantic_action_counts"
APP_COVERAGE_SEEK_FAILURE_SCORES = "app_coverage_seek_failure_scores"
APP_COVERAGE_SEEK_TARGET_ATTEMPT_COUNTS = "app_coverage_seek_target_attempt_counts"
APP_BOTTOM_NAV_LABEL_ATTEMPT_COUNTS = "app_bottom_nav_label_attempt_counts"
APP_REDUNDANT_ACTION_THRESHOLD = 1
APP_COVERAGE_SEEK_FAILURE_THRESHOLD = 2
# 同一底栏未切到选中态的空点上限：允许再试一次，第三次起 suppress／改点其它未访栏。
APP_BOTTOM_NAV_SEEK_ATTEMPT_THRESHOLD = 2
APP_DYNAMIC_PAGE_SAMPLING_VERSION = "app-dynamic-page-sampling.v1"
APP_DYNAMIC_STARTUP_OBSERVATION_VERSION = "app-dynamic-startup-observation.v1"
APP_DYNAMIC_STARTUP_OBSERVATION_LIMIT = 5
APP_DYNAMIC_UNSTABLE_PAGE_REOBSERVE_VERSION = "app-dynamic-unstable-page-reobserve.v1"
APP_DYNAMIC_UNSTABLE_PAGE_REOBSERVE_LIMIT = 3
APP_DYNAMIC_UNSTABLE_PAGE_BACK_VERSION = "app-dynamic-unstable-page-back.v1"
APP_DYNAMIC_UNSTABLE_PAGE_BACK_LIMIT = 2
VISITED_BOTTOM_NAVIGATION_LABELS_KEY = "visited_bottom_navigation_labels"
# 我的页账号凭证：先点保存凭证，见保存成功后再点我已保存。
PROFILE_CREDENTIAL_SAVE_DONE_KEY = "profile_credential_save_done"
# 已点击「保存凭证」次数；toast 常漏检时，>=1 即允许点「我已保存」。
PROFILE_CREDENTIAL_SAVE_ATTEMPT_KEY = "profile_credential_save_attempt_count"
# 短剧进入播放器后：见解锁／付费层文案即点左上角返回 → 再切下一栏（禁止用播放墙钟判断）。
SHORT_DRAMA_EXIT_PENDING_KEY = "short_drama_exit_pending"
SHORT_DRAMA_PLAYBACK_OBSERVE_COUNT_KEY = "short_drama_playback_observe_count"
SHORT_DRAMA_PLAYBACK_STARTED_AT_KEY = "short_drama_playback_started_at"
SHORT_DRAMA_TOP_LEFT_BACK_ATTEMPT_KEY = "short_drama_top_left_back_attempt_count"
# 粘性：已见解锁／贴尾／播完 CTA；离开短剧底栏或成功切下一栏前不得清。
# 防止「解锁→左上角返回→又见底栏」后 pending 被清、空等再点进播放死循环。
SHORT_DRAMA_END_SEEN_KEY = "short_drama_end_seen"
# 禁止在短剧待退出／已播完时再点进播放的文案（查看完整／金币解锁／重播等）。
SHORT_DRAMA_REENTRY_BLOCK_MARKERS = (
    "查看完整短剧",
    "查看完整短劇",
    "观看完整短剧",
    "觀看完整短劇",
    "观看完整剧集",
    "觀看完整劇集",
    "30金币解锁",
    "金币解锁",
    "开通短剧卡",
    "开通短剧卡免费看",
    "更多精彩解锁",
    "重播",
    "再看一遍",
    "重新播放",
    "中间播放",
    "中央播放",
    "播放按钮",
    "播放按鈕",
)
# 未见解锁层时短间隔重采，只为等 OCR／视觉检出付费文案；不得当作「播满 N 分钟」。
SHORT_DRAMA_UNLOCK_POLL_MS = 12_000
# 兼容旧常量名：外部／历史测试若仍引用，语义改为轮询间隔。
SHORT_DRAMA_PLAYBACK_WAIT_CHUNK_MS = SHORT_DRAMA_UNLOCK_POLL_MS
SHORT_DRAMA_PLAYBACK_WAIT_MS = SHORT_DRAMA_UNLOCK_POLL_MS
SHORT_DRAMA_PLAYBACK_OBSERVE_BEFORE_BACK = 10**9
SHORT_DRAMA_PLAYER_PAGE_TYPES = frozenset({"video_detail", "content_detail", "payment"})
SHORT_DRAMA_TAB_LABELS = frozenset({"短剧", "短视频", "视频"})
SHORT_DRAMA_PLAYBACK_END_MARKERS = (
    "更多精彩解锁即享",
    "更多精彩解锁",
    "30金币解锁",
    "金币解锁",
    "开通短剧卡免费看",
    "开通短剧卡免費看",
    "开通短剧卡",
    "剩余金币数",
    "剩余金币",
    "观看完整剧集",
    # 注意：勿加「立即解锁」——暗网会员落地页也有此文案，会误触发短剧退出支。
    "解锁观看",
    # Tab 内嵌播完三形态之一：片尾「未完待续」卡（无解锁层、进度贴尾）。
    "未完待续",
    "未完待續",
    "重播",
    "再看一遍",
    "重新播放",
    "播放完成",
    "已播完",
    "已结束",
    "完结",
    "下一集",
    # Tab 内嵌播完卡常见 CTA（hjll：查看完整短剧·全 N 集）。
    "查看完整短剧",
    "查看完整短劇",
    "观看完整短剧",
    "觀看完整短劇",
)
# 进度条贴尾：cur/dur 剩余秒数 ≤ 此值视为播完（Tab 内嵌无解锁层时切下一栏）。
SHORT_DRAMA_PROGRESS_NEAR_END_REMAINING_SECONDS = 3
_SHORT_DRAMA_PROGRESS_RE = re.compile(
    r"(?P<cm>\d{1,2})[:.：](?P<cs>\d{2})\s*/\s*(?P<dm>\d{1,2})[:.：](?P<ds>\d{2})"
)
# 中间大播放钮／重播控件（形态②：贴尾后中央 ▶，非左上角返回）。
SHORT_DRAMA_CENTER_REPLAY_MARKERS = (
    "中间播放",
    "中央播放",
    "中間播放",
    "中央播放钮",
    "中间播放钮",
    "大播放钮",
    "播放按钮",
    "播放按鈕",
    "center play",
    "play button",
    "replay",
)
# Tab 内嵌形态：轮询未见解锁层时，观察次数达到后仍允许切下一栏（防永久空等）。
# TimedObservation 每次重采须累加 observe_count；否则永远达不到此阈值。
SHORT_DRAMA_IN_TAB_WAIT_POLLS_BEFORE_ADVANCE = 2
# 沉浸态左上角空点仍停播放器时：内嵌底栏已见则第 1 次后即可切栏（勿等满 3 次撞 repeated_page）。
SHORT_DRAMA_IN_TAB_TOP_LEFT_BEFORE_TAB_ADVANCE = 1
# 暗网底栏会员／解锁落地页：只留证，禁止点「立即解锁」，并切课纲下一栏。
ANWANG_MEMBERSHIP_LANDING_MARKERS = (
    "揭秘世界最阴暗面",
    "至尊金卡",
    "至尊全网通",
    "立即解锁",
    "永久解锁 开启暗黑世界",
    "开启暗黑世界大门",
    "重口稀缺资源",
    "全球禁片大赏",
    "i视频+暗网",
)# 播放器左上角返回键（录屏／截图：白色 < chevron）；多槽轮换防点偏。
SHORT_DRAMA_TOP_LEFT_BACK_BOUNDS = {
    "left": 0.02,
    "top": 0.045,
    "right": 0.12,
    "bottom": 0.11,
}
SHORT_DRAMA_TOP_LEFT_BACK_BOUND_SLOTS: tuple[dict[str, float], ...] = (
    {"left": 0.02, "top": 0.045, "right": 0.12, "bottom": 0.11},
    {"left": 0.01, "top": 0.02, "right": 0.16, "bottom": 0.13},
    {"left": 0.00, "top": 0.03, "right": 0.20, "bottom": 0.15},
    {"left": 0.02, "top": 0.08, "right": 0.14, "bottom": 0.16},
)
# 左上角已多次 completed 仍停播放器时，允许切课纲下一栏，避免空转。
SHORT_DRAMA_TOP_LEFT_BACK_BEFORE_TAB_ADVANCE = 3
# 短剧两种形态：① 沉浸播放器（左上角有 < 返回）② Tab 内嵌（无返回箭头、底栏可见）。
# 仅①可点／合成左上角返回；②见解锁后直接切课纲下一栏，禁止点左上角 Logo。
SHORT_DRAMA_IN_TAB_TOP_MARKERS = (
    "直播",
    "抖音",
    "ai短剧",
    "热门短剧",
    "赤果短剧",
    "黄豆魔改短剧",
    "黄豆短剧",
)
SHORT_DRAMA_BOTTOM_NAV_PRESENCE_LABELS = (
    "主页",
    "首页",
    "短剧",
    "暗网",
    "社区",
    "我的",
)

# 底栏已访别名：录屏文案与 playbook／Vision 标签常不一致（主页↔首页、短剧↔视频）。
# 注意：暗网／漫画／福利等「中槽换皮」只用于已访互认，不得用于 playbook 提升放行，
# 否则 Vision 误标「福利」会被当成「暗网」入口，空点后仍停在主页并烧光 Frontier。
BOTTOM_TAB_LABEL_ALIASES: dict[str, frozenset[str]] = {
    "首页": frozenset({"首页", "主页", "顶部", "頂部"}),
    "主页": frozenset({"首页", "主页", "顶部", "頂部"}),
    "顶部": frozenset({"首页", "主页", "顶部", "頂部"}),
    "頂部": frozenset({"首页", "主页", "顶部", "頂部"}),
    "视频": frozenset({"视频", "短剧", "短视频"}),
    "短剧": frozenset({"视频", "短剧", "短视频"}),
    "短视频": frozenset({"视频", "短剧", "短视频"}),
    "暗网": frozenset({"暗网", "漫画", "福利", "陪玩", "圈子", "短片"}),
    "漫画": frozenset({"暗网", "漫画", "福利", "陪玩", "圈子", "短片"}),
    "福利": frozenset({"暗网", "漫画", "福利", "陪玩", "圈子", "短片"}),
    "陪玩": frozenset({"暗网", "漫画", "福利", "陪玩", "圈子", "短片"}),
    "圈子": frozenset({"暗网", "漫画", "福利", "陪玩", "圈子", "短片"}),
    "短片": frozenset({"暗网", "漫画", "福利", "陪玩", "圈子", "短片"}),
}

# 提升／选点放行别名：仅稳定同义（主页／短剧／AI），不含中槽换皮袋。
BOTTOM_TAB_PROMOTION_ALIASES: dict[str, frozenset[str]] = {
    "首页": frozenset({"首页", "主页", "顶部", "頂部"}),
    "主页": frozenset({"首页", "主页", "顶部", "頂部"}),
    "顶部": frozenset({"首页", "主页", "顶部", "頂部"}),
    "頂部": frozenset({"首页", "主页", "顶部", "頂部"}),
    "视频": frozenset({"视频", "短剧", "短视频"}),
    "短剧": frozenset({"视频", "短剧", "短视频"}),
    "短视频": frozenset({"视频", "短剧", "短视频"}),
    "ai": frozenset({"ai", "ai科技"}),
    "ai科技": frozenset({"ai", "ai科技"}),
}


def expand_bottom_tab_promotion_aliases(normalized_label: str) -> frozenset[str]:
    """提升放行别名；无条目时仅含自身（不做中槽换皮互认）。"""

    key = str(normalized_label or "").strip().casefold()
    if not key:
        return frozenset()
    return BOTTOM_TAB_PROMOTION_ALIASES.get(key, frozenset({key}))


def _normalize_bottom_tab_label_for_visit(raw: object) -> str:
    """与覆盖寻页共用的底栏标签归一，避免 suppress 与 visited 口径分叉。"""

    text = str(raw or "").strip()
    # 前缀匹配忽略大小写（Vision 常写「底部TAB」；上方分类同剥表示层）。
    for prefix in (
        "上方分类「",
        "上方分類「",
        "上方分类",
        "上方分類",
        "底栏「",
        "底部导航「",
        "底部Tab",
        "底部tab",
        "底部TAB",
        "底部",
        "底栏",
    ):
        if text.casefold().startswith(prefix.casefold()):
            text = text[len(prefix) :].strip()
            break
    return text.strip("「」[]()（） ").casefold()


def expand_bottom_tab_label_aliases(normalized_label: str) -> frozenset[str]:
    """展开底栏标签别名袋；先剥表示层前缀，无别名时仅含归一自身。"""

    key = _normalize_bottom_tab_label_for_visit(normalized_label)
    if not key:
        return frozenset()
    return BOTTOM_TAB_LABEL_ALIASES.get(key, frozenset({key}))


def bottom_tab_label_already_visited(
    label: object,
    visited_labels: set[str] | frozenset[str],
) -> bool:
    """已访集合与候选标签按别名互认（主页已访则首页不再点）。"""

    probe = _normalize_bottom_tab_label_for_visit(label)
    if not probe or not visited_labels:
        return False
    visited_expanded = set()
    for item in visited_labels:
        visited_expanded.update(expand_bottom_tab_label_aliases(item))
    return bool(expand_bottom_tab_label_aliases(probe) & visited_expanded)


def bottom_tab_playbook_progress_already_visited(
    label: object,
    visited_labels: set[str] | frozenset[str],
) -> bool:
    """课纲下一栏推进用的已访判定：只用稳定同义，禁止漫画／福利冒充暗网已访。

    中槽换皮袋仍可用于换皮 App 的「点过暗网＝福利已访」互认；但 hjll 等固定顺序
    课纲不得因上方分类「漫画」或误检选中态跳过「暗网」直接点 AI（257b4aaa）。
    """

    probe = _normalize_bottom_tab_label_for_visit(label)
    if not probe or not visited_labels:
        return False
    visited_expanded: set[str] = set()
    for item in visited_labels:
        key = _normalize_bottom_tab_label_for_visit(item)
        if not key:
            continue
        visited_expanded.add(key)
        visited_expanded.update(expand_bottom_tab_promotion_aliases(key))
    if probe in visited_expanded:
        return True
    return bool(expand_bottom_tab_promotion_aliases(probe) & visited_expanded)


def is_short_drama_bottom_tab_label(label: object) -> bool:
    """是否属于短剧／短视频／视频底栏族。"""

    probe = _normalize_bottom_tab_label_for_visit(label)
    if not probe:
        return False
    return bool(expand_bottom_tab_label_aliases(probe) & SHORT_DRAMA_TAB_LABELS)


def is_anwang_bottom_tab_label(label: object) -> bool:
    """是否属于暗网／漫画／福利等中槽底栏族。"""

    probe = _normalize_bottom_tab_label_for_visit(label)
    if not probe:
        return False
    return bool(expand_bottom_tab_label_aliases(probe) & frozenset({"暗网"}))


def membership_center_surface_detected(text_blob: object) -> bool:
    """是否为「会员中心」全屏套餐页（左上角有「<」，禁止当暗网解锁层切栏）。"""

    return "会员中心" in str(text_blob or "")


def anwang_membership_landing_detected(text_blob: object) -> bool:
    """是否为暗网底栏会员／「立即解锁」落地页（只留证，禁止点解锁 CTA）。"""

    blob = str(text_blob or "")
    if not blob.strip():
        return False
    # 会员中心也有「至尊全网通」套餐卡；那是全屏支付页，必须点左上角「<」。
    if membership_center_surface_detected(blob):
        return False
    # 「立即解锁」 alone 可能出现在短剧层；须叠加强特征。
    strong = (
        "揭秘世界最阴暗面",
        "至尊金卡",
        "至尊全网通",
        "开启暗黑世界大门",
        "重口稀缺资源",
        "全球禁片大赏",
        "i视频+暗网",
    )
    if any(marker in blob for marker in strong):
        return True
    return "立即解锁" in blob and (
        "暗网" in blob or "永久解锁" in blob or "暗黑世界" in blob
    )


def anwang_unlock_cta_text(text: object) -> bool:
    """是否为暗网落地页解锁／开会员 CTA（禁止点击）。"""

    blob = str(text or "").casefold()
    if not blob.strip():
        return False
    return any(
        marker in blob
        for marker in (
            "立即解锁",
            "至尊金卡",
            "至尊全网通",
            "开通专属会员",
            "永久解锁",
        )
    )


def short_drama_exit_pending(coverage_metadata: Mapping[str, object] | None) -> bool:
    """覆盖元数据是否标记「短剧播放器待 BACK 退出」。"""

    if not isinstance(coverage_metadata, Mapping):
        return False
    return coverage_metadata.get(SHORT_DRAMA_EXIT_PENDING_KEY) is True


def short_drama_end_seen(coverage_metadata: Mapping[str, object] | None) -> bool:
    """是否已粘性记录见解锁／贴尾／播完（须切栏，禁止再进播放）。"""

    if not isinstance(coverage_metadata, Mapping):
        return False
    return coverage_metadata.get(SHORT_DRAMA_END_SEEN_KEY) is True


def short_drama_reentry_candidate_blocked(
    candidate: CandidateAction,
    *,
    coverage_metadata: Mapping[str, object] | None = None,
) -> bool:
    """短剧待退出或已播完时，禁止点「查看完整／解锁／重播」等再进播放候选。"""

    if not (
        short_drama_exit_pending(coverage_metadata)
        or short_drama_end_seen(coverage_metadata)
    ):
        return False
    # 底栏切栏／返回／关层始终放行。
    if candidate.metadata.get("bottom_navigation_tab_seek") is True:
        return False
    if candidate.metadata.get("short_drama_in_tab_form_exit") is True:
        return False
    if candidate.metadata.get("short_drama_top_left_back") is True:
        return False
    if candidate.action_type is InteractionOperation.BACK:
        return False
    terms = " ".join(
        (
            str(candidate.semantic_target or ""),
            str(candidate.metadata.get("purpose") or ""),
            str(candidate.metadata.get("expected_state_hint") or ""),
            str(candidate.metadata.get("label") or ""),
        )
    )
    terms_cf = terms.casefold()
    return any(marker.casefold() in terms_cf for marker in SHORT_DRAMA_REENTRY_BLOCK_MARKERS)

def short_drama_playback_observe_count(
    coverage_metadata: Mapping[str, object] | None,
) -> int:
    if not isinstance(coverage_metadata, Mapping):
        return 0
    raw = coverage_metadata.get(SHORT_DRAMA_PLAYBACK_OBSERVE_COUNT_KEY)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return 0
    return max(0, raw)


def short_drama_top_left_back_attempt_count(
    coverage_metadata: Mapping[str, object] | None,
) -> int:
    """短剧左上角返回已完成落点次数（仍停播放器时累加，用于换槽／切栏）。"""

    if not isinstance(coverage_metadata, Mapping):
        return 0
    raw = coverage_metadata.get(SHORT_DRAMA_TOP_LEFT_BACK_ATTEMPT_KEY)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return 0
    return max(0, raw)


def short_drama_top_left_back_bounds_for_attempt(attempt_count: int) -> dict[str, float]:
    """按已完成次数轮换左上角点击条带。"""

    slots = SHORT_DRAMA_TOP_LEFT_BACK_BOUND_SLOTS
    if not slots:
        return dict(SHORT_DRAMA_TOP_LEFT_BACK_BOUNDS)
    index = max(0, int(attempt_count)) % len(slots)
    return dict(slots[index])


def _short_drama_frame_text_blob(
    *,
    coverage_metadata: Mapping[str, object] | None,
    key_elements: Sequence[object] | None = None,
) -> str:
    parts: list[str] = []
    if isinstance(coverage_metadata, Mapping):
        for key in (
            "visual_summary",
            "page_summary",
            "ocr_text",
            "overlay_signature",
        ):
            raw = coverage_metadata.get(key)
            if isinstance(raw, str) and raw.strip():
                parts.append(raw)
        raw_keys = coverage_metadata.get("key_elements")
        if isinstance(raw_keys, (list, tuple)):
            parts.extend(str(item) for item in raw_keys)
        identity = coverage_metadata.get("visual_page_identity")
        if not isinstance(identity, Mapping):
            identity = coverage_metadata.get("app_visual_page_identity")
        if isinstance(identity, Mapping):
            for key in ("return_control", "root_control", "title"):
                anchor = identity.get(key)
                if isinstance(anchor, Mapping):
                    parts.append(str(anchor.get("label") or ""))
                    parts.append(str(anchor.get("text") or ""))
                elif isinstance(anchor, str):
                    parts.append(anchor)
    if key_elements is not None:
        parts.extend(str(item) for item in key_elements)
    return " ".join(parts)


def _candidate_normalized_bounds(
    candidate: CandidateAction,
) -> tuple[float, float, float, float] | None:
    raw = candidate.metadata.get("normalized_bounds")
    if not isinstance(raw, Mapping):
        return None
    try:
        return (
            float(raw.get("left")),
            float(raw.get("top")),
            float(raw.get("right")),
            float(raw.get("bottom")),
        )
    except (TypeError, ValueError):
        return None


def short_drama_top_left_back_arrow_visible(
    *,
    coverage_metadata: Mapping[str, object] | None = None,
    candidates: Sequence[CandidateAction] | None = None,
    key_elements: Sequence[object] | None = None,
) -> bool:
    """同帧是否可见左上角返回箭头（沉浸播放器形态①）。"""

    for candidate in candidates or ():
        terms = " ".join(
            (
                str(candidate.semantic_target or ""),
                str(candidate.metadata.get("purpose") or ""),
                str(candidate.metadata.get("expected_state_hint") or ""),
            )
        ).casefold()
        looks_back = (
            any(
                marker in terms
                for marker in ("返回", "back", "上一页", "上一级", "<", "左上角")
            )
            or candidate.metadata.get("short_drama_top_left_back") is True
            or candidate.metadata.get("coverage_parent_restore") is True
        )
        if not looks_back:
            continue
        bounds = _candidate_normalized_bounds(candidate)
        if bounds is None:
            # 语义已是返回且无坐标：仍视为有返回控件（Vision 常漏 bounds）。
            return True
        left, top, right, bottom = bounds
        if left <= 0.22 and top <= 0.18 and right <= 0.32 and bottom <= 0.22:
            return True

    if isinstance(coverage_metadata, Mapping):
        identity = coverage_metadata.get("visual_page_identity")
        if not isinstance(identity, Mapping):
            identity = coverage_metadata.get("app_visual_page_identity")
        if isinstance(identity, Mapping):
            control = identity.get("return_control")
            if isinstance(control, Mapping) and control:
                label = str(
                    control.get("label") or control.get("text") or ""
                ).casefold()
                if any(
                    marker in label
                    for marker in ("返回", "back", "箭头", "chevron", "<")
                ) or str(control.get("kind") or "").casefold() in {
                    "icon",
                    "back",
                    "return",
                }:
                    loc = control.get("location") or control.get("normalized_bounds")
                    if isinstance(loc, Mapping):
                        try:
                            left = float(loc.get("left", loc.get("x", 1)))
                            top = float(loc.get("top", loc.get("y", 1)))
                        except (TypeError, ValueError):
                            left = top = 1.0
                        if left <= 0.28 and top <= 0.20:
                            return True
                    else:
                        return True

    blob = _short_drama_frame_text_blob(
        coverage_metadata=coverage_metadata,
        key_elements=key_elements,
    ).casefold()
    if any(
        marker in blob
        for marker in ("返回箭头", "左上角返回", "top-left back", "return_control")
    ):
        return True
    return False


def short_drama_bottom_nav_visible_on_frame(
    *,
    coverage_metadata: Mapping[str, object] | None = None,
    candidates: Sequence[CandidateAction] | None = None,
    key_elements: Sequence[object] | None = None,
) -> bool:
    """同帧是否露出底栏／短剧顶部分类（内嵌形态②特征）。"""

    tab_hits = 0
    for candidate in candidates or ():
        if candidate.metadata.get("bottom_navigation_tab_seek") is True:
            tab_hits += 1
            continue
        bounds = _candidate_normalized_bounds(candidate)
        label = _normalize_bottom_tab_label_for_visit(candidate.semantic_target)
        if (
            bounds is not None
            and bounds[1] >= 0.86
            and label
            and any(
                bottom_tab_label_already_visited(label, {marker})
                for marker in SHORT_DRAMA_BOTTOM_NAV_PRESENCE_LABELS
            )
        ):
            tab_hits += 1
    if tab_hits >= 1:
        return True

    blob = _short_drama_frame_text_blob(
        coverage_metadata=coverage_metadata,
        key_elements=key_elements,
    )
    blob_cf = blob.casefold()
    label_hits = sum(
        1 for marker in SHORT_DRAMA_BOTTOM_NAV_PRESENCE_LABELS if marker in blob
    )
    if label_hits >= 3:
        return True
    if any(marker in blob_cf for marker in SHORT_DRAMA_IN_TAB_TOP_MARKERS):
        return True
    return False


def short_drama_immersive_player_chrome(text_blob: object) -> bool:
    """同帧是否露出沉浸播放器铬条（进度／观看完整剧集），用于压过残留底栏候选。"""

    blob = str(text_blob or "")
    if not blob.strip():
        return False
    chrome_markers = (
        "观看完整剧集",
        "觀看完整劇集",
        "查看完整短剧",
        "查看完整短劇",
        "观看完整短剧",
        "觀看完整短劇",
        "观看完整剧集",
    )
    if any(marker in blob for marker in chrome_markers):
        return True
    return _SHORT_DRAMA_PROGRESS_RE.search(blob) is not None


def short_drama_bottom_nav_label_hits(text_blob: object) -> int:
    """帧文案里出现了几个课纲底栏标签。"""

    blob = str(text_blob or "")
    return sum(
        1 for marker in SHORT_DRAMA_BOTTOM_NAV_PRESENCE_LABELS if marker in blob
    )


def short_drama_should_tap_top_left_back(
    *,
    coverage_metadata: Mapping[str, object] | None = None,
    candidates: Sequence[CandidateAction] | None = None,
    key_elements: Sequence[object] | None = None,
) -> bool:
    """短剧退出是否应点左上角。

    **底栏可见＝Tab 内嵌形态**：一律不点左上角（Vision 常把 Logo／顶栏误标成返回箭头，
    空点后停在 AI短剧播完页直至 ``repeated_page_limit``）。仅沉浸播放器（无底栏）才点／合成左上角。
    """

    if short_drama_bottom_nav_visible_on_frame(
        coverage_metadata=coverage_metadata,
        candidates=candidates,
        key_elements=key_elements,
    ):
        return False
    if short_drama_top_left_back_arrow_visible(
        coverage_metadata=coverage_metadata,
        candidates=candidates,
        key_elements=key_elements,
    ):
        return True
    # 沉浸播放器常漏 Vision 返回候选：默认仍合成左上角 < 。
    return True


def short_drama_center_replay_control_detected(text_blob: object) -> bool:
    """画面是否含中央播放／重播控件文案（形态②贴尾后常见）。"""

    blob = str(text_blob or "").casefold()
    if not blob.strip():
        return False
    return any(marker.casefold() in blob for marker in SHORT_DRAMA_CENTER_REPLAY_MARKERS)


def short_drama_in_tab_finished_for_advance(
    text_blob: object,
    *,
    observe_count: int = 0,
    page_type: object = None,
    coverage_metadata: Mapping[str, object] | None = None,
) -> bool:
    """Tab 内嵌是否已达可切下一栏的播完信号（贴尾／CTA／中间播放／解锁／轮询／粘性）。"""

    if short_drama_end_seen(coverage_metadata):
        return True
    blob = str(text_blob or "")
    page = str(page_type or "").strip().casefold()
    if page == "payment":
        return True
    if short_drama_playback_progress_near_end(blob):
        return True
    if short_drama_playback_end_detected(blob):
        return True
    if short_drama_center_replay_control_detected(blob):
        return True
    if observe_count >= SHORT_DRAMA_IN_TAB_WAIT_POLLS_BEFORE_ADVANCE:
        return True
    return False

def short_drama_playback_started_at(
    coverage_metadata: Mapping[str, object] | None,
) -> str | None:
    if not isinstance(coverage_metadata, Mapping):
        return None
    raw = coverage_metadata.get(SHORT_DRAMA_PLAYBACK_STARTED_AT_KEY)
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def short_drama_playback_elapsed_ms(
    coverage_metadata: Mapping[str, object] | None,
    *,
    now: object | None = None,
) -> int | None:
    """自进入短剧播放器以来的墙钟毫秒；无起点则 None。"""

    from datetime import UTC, datetime

    started = short_drama_playback_started_at(coverage_metadata)
    if not started:
        return None
    try:
        start_dt = datetime.fromisoformat(started)
    except ValueError:
        return None
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=UTC)
    if now is None:
        now_dt = datetime.now(UTC)
    elif isinstance(now, datetime):
        now_dt = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    else:
        return None
    return max(0, int((now_dt - start_dt).total_seconds() * 1000))


def short_drama_playback_end_detected(text_blob: object) -> bool:
    """画面文案是否含短剧解锁付费层／播完结束态（主闸：解锁即返回）。"""

    blob = str(text_blob or "")
    if not blob.strip():
        return False
    return any(marker in blob for marker in SHORT_DRAMA_PLAYBACK_END_MARKERS)


def short_drama_playback_progress_near_end(
    text_blob: object,
    *,
    remaining_seconds_max: int = SHORT_DRAMA_PROGRESS_NEAR_END_REMAINING_SECONDS,
) -> bool:
    """进度条 cur/dur 已贴尾（如 02:11/02:12）时视为本集播完。

    用于 Tab 内嵌短剧无解锁层文案时仍可切课纲下一栏；不代替解锁层主闸，
    也不用墙钟「播满 N 分钟」。
    """

    blob = str(text_blob or "")
    if not blob.strip():
        return False
    near_limit = max(0, int(remaining_seconds_max))
    # 同帧可能有多段时间码；任一段进度贴尾即视为本集播完。
    for match in _SHORT_DRAMA_PROGRESS_RE.finditer(blob):
        try:
            current = int(match.group("cm")) * 60 + int(match.group("cs"))
            duration = int(match.group("dm")) * 60 + int(match.group("ds"))
        except (TypeError, ValueError):
            continue
        if duration <= 0 or current < 0:
            continue
        if duration - current <= near_limit:
            return True
    return False


def parse_bottom_nav_label_attempt_counts(value: object) -> dict[str, int]:
    """底栏标签空点计数；键为归一化标签。"""

    result: dict[str, int] = {}
    for raw_key, count in parse_app_semantic_action_counts(value).items():
        key = _normalize_bottom_tab_label_for_visit(raw_key)
        if not key:
            continue
        result[key] = result.get(key, 0) + count
    return result


def record_bottom_nav_label_seek_attempt(
    value: object,
    *,
    label: object,
    evidenced: bool,
) -> dict[str, int]:
    """底栏点击未切到选中态时累计；留证成功仅清零当前实际标签。"""

    result = parse_bottom_nav_label_attempt_counts(value)
    key = _normalize_bottom_tab_label_for_visit(label)
    if not key:
        return result
    if evidenced:
        result.pop(key, None)
        return result
    result[key] = result.get(key, 0) + 1
    return result


def bottom_nav_label_attempts_exhausted(
    label: object,
    coverage_metadata: Mapping[str, object] | None,
    *,
    threshold: int = APP_BOTTOM_NAV_SEEK_ATTEMPT_THRESHOLD,
) -> bool:
    """同一底栏空点达到上限后禁止再点，改走其它未访栏。"""

    probe = _normalize_bottom_tab_label_for_visit(label)
    if not probe or coverage_metadata is None:
        return False
    counts = parse_bottom_nav_label_attempt_counts(
        coverage_metadata.get(APP_BOTTOM_NAV_LABEL_ATTEMPT_COUNTS)
    )
    return counts.get(probe, 0) >= threshold


def _visited_bottom_labels_from_metadata(
    coverage_metadata: Mapping[str, object] | None,
) -> set[str]:
    if not isinstance(coverage_metadata, Mapping):
        return set()
    raw = coverage_metadata.get(VISITED_BOTTOM_NAVIGATION_LABELS_KEY)
    if not isinstance(raw, (list, tuple)):
        return set()
    return {
        _normalize_bottom_tab_label_for_visit(item)
        for item in raw
        if isinstance(item, str) and _normalize_bottom_tab_label_for_visit(item)
    }


def _canonical_app_page_identity(value: str | None) -> str:
    """去掉恢复路径会变化的深度与作用域，只保留稳定业务页身份。"""

    parts = tuple(part.strip().casefold() for part in str(value or "").split("|") if part.strip())
    return "|".join(part for part in parts if not part.startswith(("depth:", "scope:")))


def _canonical_app_page_class(value: str | None) -> str:
    """忽略页面名称、页型与视觉对象实例，只保留包与 Activity 作为寻页来源页类别。

    覆盖核心身份已解耦 page_type；寻页去重同样不得因页型改判或 vid 细节绕开。
    """

    parts: list[str] = []
    for part in str(value or "").split("|"):
        text = part.strip().casefold()
        if not text or text.startswith(("name:", "depth:", "scope:", "vid:")):
            continue
        parts.append(text)
        if len(parts) >= 2:
            break
    return "|".join(parts)


def _canonical_coverage_seek_target(value: str) -> str:
    """合并模型对同一页签入口的 ``分类``、``Tab`` 等标签别名。"""

    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    compact = re.sub(r"[\s_\-:：/|]+", "", normalized)
    prefixes = (
        "顶部分类选项卡",
        "底部分类选项卡",
        "二级分类选项卡",
        "子分类选项卡",
        "顶部分类tab",
        "底部分类tab",
        "二级分类tab",
        "子分类tab",
        "分类选项卡",
        "分类标签",
        "分类tab",
        "顶部tab",
        "底部tab",
        "二级tab",
        "子tab",
        "分类",
        "tab",
    )
    suffixes = (
        "分类选项卡",
        "分类标签页",
        "分类标签",
        "分类tab",
        "选项卡",
        "标签页",
        "标签",
        "tab",
    )
    changed = True
    while compact and changed:
        changed = False
        for prefix in prefixes:
            if compact.startswith(prefix) and len(compact) > len(prefix):
                compact = compact[len(prefix) :]
                changed = True
                break
        for suffix in suffixes:
            if compact.endswith(suffix) and len(compact) > len(suffix):
                compact = compact[: -len(suffix)]
                changed = True
                break
    # 同一列表首卡常被改写为“首卡／第一个卡片／卡片1”；只合并明确
    # 的第一序号，不吞掉第二张及后续真实替代入口。
    compact = re.sub(r"(?:首卡|第一(?:个)?卡片|卡片一)$", "卡片1", compact)
    return compact


def build_app_semantic_action_key(
    candidate: CandidateAction,
    *,
    page_identity: str | None = None,
) -> str:
    """生成绑定业务页面、但不依赖截图和坐标的动作语义键。"""

    purpose = str(candidate.metadata.get("purpose") or "").strip().casefold()
    semantic_target = candidate.semantic_target.strip().casefold()
    return "app-semantic-action:" + stable_protocol_hash(
        {
            # 同一导航文案可能在多个业务页承担不同作用；必须按页面隔离，
            # 同页滚动或轻微重绘又必须复用同一个计数。
            # depth/scope 描述的是本次恢复路径，不是业务页本身。同一首页
            # sibling 从 depth:0 恢复到 depth:17 后仍必须命中同一个语义计数，
            # 否则同义底栏会被当成新入口反复执行直至耗尽预算。
            "page_identity": _canonical_app_page_identity(page_identity),
            "action_type": candidate.action_type.value,
            # Provider 可能把同一按钮输出为“登录按钮”或 ``login_button``；
            # 有明确目的时以目的为主，避免瞬时文案绕过循环保护。
            "intent": purpose or semantic_target,
            "expected_state_hint": str(candidate.metadata.get("expected_state_hint") or "")
            .strip()
            .casefold(),
            "expected_next_page_type": str(candidate.metadata.get("expected_next_page_type") or "")
            .strip()
            .casefold(),
        }
    )


def build_app_coverage_seek_key(
    candidate: CandidateAction,
    *,
    page_identity: str | None = None,
) -> str:
    """生成来源业务页到缺口页型的稳定寻页键，忽略截图、坐标和瞬时入口文案。"""

    expected = (
        str(
            candidate.metadata.get("expected_next_page_type")
            or candidate.metadata.get("expected_state_hint")
            or ""
        )
        .strip()
        .casefold()
    )
    return "app-coverage-seek:" + stable_protocol_hash(
        {
            "page_identity": _canonical_app_page_identity(page_identity),
            "action_type": candidate.action_type.value,
            "expected_page_type": expected,
        }
    )


def build_app_coverage_seek_target_key(
    candidate: CandidateAction,
    *,
    page_identity: str | None = None,
) -> str:
    """生成跨页面名称实例稳定、但区分真实入口的覆盖寻页目标键。"""

    expected = (
        str(
            candidate.metadata.get("expected_next_page_type")
            or candidate.metadata.get("expected_state_hint")
            or ""
        )
        .strip()
        .casefold()
    )
    return "app-coverage-seek-target:" + stable_protocol_hash(
        {
            "source_page_class": _canonical_app_page_class(page_identity),
            "action_type": candidate.action_type.value,
            "expected_page_type": expected,
            "semantic_target": _canonical_coverage_seek_target(candidate.semantic_target),
        }
    )


def parse_app_semantic_action_counts(value: object) -> dict[str, int]:
    """只接受持久化的非负整数计数，忽略损坏或未知字段。"""

    if not isinstance(value, Mapping):
        return {}
    result: dict[str, int] = {}
    for key, count in value.items():
        if (
            isinstance(key, str)
            and key.strip()
            and isinstance(count, int)
            and not isinstance(count, bool)
            and count >= 0
        ):
            result[key] = count
    return result


def record_app_semantic_action(
    value: object,
    *,
    candidate: CandidateAction,
    page_identity: str | None = None,
) -> dict[str, int]:
    """在动作成功后单调累计跨截图语义次数。"""

    result = parse_app_semantic_action_counts(value)
    key = build_app_semantic_action_key(candidate, page_identity=page_identity)
    result[key] = result.get(key, 0) + 1
    return result


def record_app_coverage_seek_outcome(
    value: object,
    *,
    candidate: CandidateAction,
    page_identity: str | None,
    action_succeeded: bool,
    arrival_proven: bool,
    tab_seek_evidenced: bool = False,
) -> dict[str, int]:
    """累计合成寻页失败分值：执行失败立即封顶，到达未证实允许再试一次。"""

    result = parse_app_semantic_action_counts(value)
    if candidate.metadata.get("coverage_page_seek") is not True:
        return result
    # 底栏巡检以截图留证为准，不计入普通覆盖寻页失败分。
    if candidate.metadata.get("bottom_navigation_tab_seek") is True:
        if tab_seek_evidenced or arrival_proven:
            key = build_app_coverage_seek_key(candidate, page_identity=page_identity)
            result.pop(key, None)
        return result
    key = build_app_coverage_seek_key(candidate, page_identity=page_identity)
    if arrival_proven:
        result.pop(key, None)
        return result
    increment = APP_COVERAGE_SEEK_FAILURE_THRESHOLD if not action_succeeded else 1
    result[key] = result.get(key, 0) + increment
    return result


def record_app_coverage_seek_target_attempt(
    value: object,
    *,
    candidate: CandidateAction,
    page_identity: str | None,
    arrival_proven: bool,
    tab_seek_evidenced: bool = False,
) -> dict[str, int]:
    """无到达时记住已尝试的语义入口，阻止换标签或页面名后重复点击。"""

    result = parse_app_semantic_action_counts(value)
    if candidate.metadata.get("coverage_page_seek") is not True:
        return result
    # 底栏巡检改走 visited_bottom_navigation_labels，禁止一次 UNPROVEN 就 suppress。
    if candidate.metadata.get("bottom_navigation_tab_seek") is True:
        return result
    key = build_app_coverage_seek_target_key(
        candidate,
        page_identity=page_identity,
    )
    if arrival_proven or tab_seek_evidenced:
        result.pop(key, None)
        return result
    result[key] = result.get(key, 0) + 1
    return result


def should_suppress_redundant_app_candidate(
    *,
    candidate: CandidateAction,
    coverage_metadata: Mapping[str, object],
    threshold: int = APP_REDUNDANT_ACTION_THRESHOLD,
) -> bool:
    """同一业务页上的已成功点击或滚动不再重复采样证据。"""

    if candidate.metadata.get("coverage_page_seek") is True:
        # 底栏巡检：已访或同栏空点达上限才 suppress；一次 UNPROVEN 仍可再试。
        if candidate.metadata.get("bottom_navigation_tab_seek") is True:
            visited = _visited_bottom_labels_from_metadata(coverage_metadata)
            if bottom_tab_label_already_visited(
                candidate.semantic_target,
                visited,
            ):
                return True
            return bottom_nav_label_attempts_exhausted(
                candidate.semantic_target,
                coverage_metadata,
            )
        page_identity = coverage_metadata.get("business_page_identity")
        if not isinstance(page_identity, str) or not page_identity.strip():
            page_identity = None
        target_attempts = parse_app_semantic_action_counts(
            coverage_metadata.get(APP_COVERAGE_SEEK_TARGET_ATTEMPT_COUNTS)
        )
        target_key = build_app_coverage_seek_target_key(
            candidate,
            page_identity=page_identity,
        )
        if target_attempts.get(target_key, 0) >= 1:
            return True
        failures = parse_app_semantic_action_counts(
            coverage_metadata.get(APP_COVERAGE_SEEK_FAILURE_SCORES)
        )
        key = build_app_coverage_seek_key(candidate, page_identity=page_identity)
        return failures.get(key, 0) >= APP_COVERAGE_SEEK_FAILURE_THRESHOLD
    if str(candidate.metadata.get("expected_state_hint") or "").strip().casefold() in {
        "close_popup_x",
        "tap_skip",
    }:
        # 多层广告可能在同一业务页、同一坐标连续出现，甚至沿用完全相同的
        # 关闭文案。每张新截图上的当前关闭控件都是新的清障动作，不能套用
        # 业务点击的一次性语义去重，否则下一层广告会只剩 BACK 兜底。
        return False
    recovery_text = " ".join(
        (
            candidate.semantic_target,
            str(candidate.metadata.get("purpose") or ""),
        )
    ).casefold()
    if (
        candidate.metadata.get("frontier_scope") == "local"
        and candidate.action_type is InteractionOperation.CLICK
        and (
            (
                "关闭" in recovery_text
                and any(marker in recovery_text for marker in ("弹窗", "弹层"))
            )
            or any(
                marker in recovery_text
                for marker in (
                    "close popup",
                    "dismiss popup",
                    "close overlay",
                    "dismiss overlay",
                )
            )
        )
    ):
        # Provider 可能把已对准 X 的关闭候选标成 home 而非 close_popup_x。
        # 当前截图上的局部弹层关闭仍是清障动作，不能因历史层文案相同而丢弃。
        return False
    if candidate.action_type not in {
        InteractionOperation.CLICK,
        InteractionOperation.SCROLL,
    }:
        return False
    page_identity = coverage_metadata.get("business_page_identity")
    if not isinstance(page_identity, str) or not page_identity.strip():
        page_identity = None
    expected = candidate.metadata.get("expected_next_page_type")
    covered_raw = coverage_metadata.get("covered_page_types")
    covered = (
        {item.strip().casefold() for item in covered_raw if isinstance(item, str) and item.strip()}
        if isinstance(covered_raw, (list, tuple))
        else set()
    )
    expected_page_type = expected.strip().casefold() if isinstance(expected, str) else ""
    # 明确指向尚未覆盖页型的候选仍有业务增益，不能因同页其他点击而被抑制。
    if expected_page_type and expected_page_type not in covered:
        return False
    counts = parse_app_semantic_action_counts(coverage_metadata.get(APP_SEMANTIC_ACTION_COUNTS))
    scoped_key = build_app_semantic_action_key(
        candidate,
        page_identity=page_identity,
    )
    # v1 在升级前没有页面身份；只把它作为兼容回读，不再继续写入。
    legacy_key = build_app_semantic_action_key(candidate)
    effective_threshold = (
        max(threshold, 2) if candidate.metadata.get("dynamic_page_sampling") is True else threshold
    )
    return max(counts.get(scoped_key, 0), counts.get(legacy_key, 0)) >= effective_threshold
