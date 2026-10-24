"""页面采集浏览器处理层：封装 Playwright，并向 Tool 层暴露稳定契约。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import math
import re
import secrets
import sys
import time
import zlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import parse_qs, unquote_to_bytes, urlsplit, urlunsplit

from services.api.app.application.h5_interaction_capabilities import (
    COUNTDOWN_JUMP_DISMISSAL_SELECTOR,
    GLOBAL_LAUNCH_DISMISSAL_SELECTOR,
    LAUNCH_PAGE_ENTRY_DISMISSAL_SELECTOR,
    POPUP_COUNT_DOWN_DISMISSAL_SELECTOR,
    POPUP_SKIP_WRAP_DISMISSAL_SELECTOR,
    STARTUP_AD_DISMISSAL_SELECTOR,
    h5_startup_dismissal_selector_from_snapshot_element,
)
from services.api.app.application.h5_list_detail_navigation import (
    H5_COMMUNITY_HOME_POST_TITLE_SELECTOR,
    H5_CONTENT_LAYOUT3_TITLE_SELECTOR,
    H5_HOME_VIDEO_GRID_TITLE_SELECTOR,
    H5_HOME_VIDEO_TITLE_SELECTOR,
    H5_LONG_VIDEO_CARD_TITLE_SELECTOR,
    H5_MANGA_VIDEO_TITLE_SELECTOR,
    H5_MASONRY_VIDEO_TITLE_SELECTOR,
)
from services.api.app.application.h5_logical_surface_evidence import (
    build_h5_logical_surface,
)
from services.api.app.application.h5_popup_close_contract import (
    OVERLAY_AD_CLOSE_SELECTOR as POPUP_OVERLAY_AD_CLOSE_SELECTOR,
)
from services.api.app.application.h5_profile_activity_navigation import (
    h5_profile_daily_signin_src_matches,
    h5_profile_signin_src_matches,
)
from services.api.app.application.h5_profile_credential_popup import (
    H5_PROFILE_CREDENTIAL_SAVE_CONFIRM_SELECTOR,
    H5_PROFILE_CREDENTIAL_SCREENSHOT_CONFIRM_SELECTOR,
)
from services.api.app.application.h5_profile_login_navigation import (
    H5_FLUTTER_PROFILE_LOGIN_RUNTIME_SELECTOR,
)
from services.api.app.application.h5_profile_payment_navigation import (
    h5_profile_mine_vip_center_src_matches,
    h5_profile_vip_center_src_matches,
)
from services.api.app.application.h5_profile_visual_navigation import (
    h5_profile_visual_entry_contract,
)
from services.api.app.application.h5_recapture_lease import (
    H5PhysicalOperationNotDrained,
    run_h5_physical_operation,
)
from services.api.app.application.h5_session_journey import (
    bind_h5_physical_resource,
    quarantine_h5_journeys,
)
from services.api.app.application.h5_structural_navigation import (
    H5_BOTTOM_NAVIGATION_FAMILY_ADAPTIVE,
    H5_BOTTOM_NAVIGATION_FAMILY_PAGES_FOOT,
    H5_GENERIC_NAVIGATION_CONTAINER_CLASSES,
    H5_GENERIC_NAVIGATION_ITEM_CLASSES,
    h5_flutter_bottom_navigation_label_hash,
    h5_match_bottom_navigation_target,
    h5_match_flutter_bottom_navigation_runtime_selector,
)
from services.api.app.application.interaction_runtime import (
    LogicalTargetRuntimeBinding,
    VisualNavigationRuntimeTarget,
    logical_target_snapshot_identity_signature,
    logical_target_snapshot_signature,
)
from services.api.app.application.web_article_body_structure import (
    WEB_ARTICLE_BODY_SELECTOR,
    is_web_article_body_node,
)
from services.api.app.application.web_full_page_stabilization import (
    WebFullPageStabilizationError,
    WebFullPageStabilizationPolicy,
    WebFullPageStabilizationResult,
    probe_direct_full_page_candidate,
)
from services.api.app.application.web_readiness_contracts import (
    DEFAULT_WEB_READINESS_PROFILE,
    WebReadinessProfile,
    web_capture_consistency_proven,
    web_profile_network_scope,
)
from services.api.app.core.config import get_settings
from services.api.app.core.h5_auth_config import normalize_h5_auth_hostname
from services.api.app.domain.exploration.age_confirm import AGE_CONFIRM_ENTER_SELECTOR
from services.api.app.domain.exploration.h5_logical_surface import (
    compare_h5_logical_surfaces,
)
from services.api.app.infrastructure.browser.dom_selectors import UNIQUE_SELECTOR_FOR_SCRIPT
from services.api.app.infrastructure.browser.h5_frame_origin import (
    annotate_h5_frame_origins,
    capture_h5_frame,
    install_h5_canvas_observer,
    read_h5_frame_origin,
)
from services.api.app.infrastructure.browser.h5_gallery_binding import (
    H5_GALLERY_BINDING_CENSUS_SCRIPT,
)
from services.api.app.infrastructure.browser.h5_physical_stop import H5BrowserStopGuard
from services.api.app.infrastructure.browser.h5_visual_navigation_runtime import (
    VisualCoordinateResolution,
    assess_visual_navigation_transition,
    extract_visual_layout_evidence,
    resolve_visual_navigation_coordinate,
)
from services.api.app.infrastructure.browser.navigation_document import navigation_document_facts
from services.api.app.infrastructure.browser.playwright_cli_session import (
    CliBrowserSession,
    observation_digest,
)
from services.api.app.infrastructure.browser.visible_text import (
    read_visible_text as _read_visible_text,
)
from services.api.app.infrastructure.browser.web_user_agent import web_chrome_user_agent
from services.api.app.infrastructure.browser.web_volatile_motion import WEB_VOLATILE_MOTION_JS

logger = logging.getLogger(__name__)


class PageCaptureMode(StrEnum):
    """page_capture_tool 支持的采集模式。"""

    GOTO = "goto"
    OBSERVE = "observe"
    TIMED_CAPTURE = "timed_capture"
    SINGLE_TRIGGER = "single_trigger"


PAGE_SETTLE_MAX_WAIT_MS = 15000
STARTUP_AD_DISMISSAL_VERIFICATION_TIMEOUT_MS = 5_000
PAGE_SETTLE_POLL_INTERVAL_MS = 1000
PAGE_SETTLE_STABLE_SAMPLES = 2
# 单次页面交互后的动画通常在数百毫秒内结束。短暂稳定等待可避免把
# van-dialog 的 leave 过渡帧误存为下一层弹窗；跨页导航仍使用完整等待。
PAGE_POST_TRIGGER_SETTLE_MAX_WAIT_MS = 3000
PAGE_POST_TRIGGER_SETTLE_POLL_INTERVAL_MS = 200
PAGE_DOM_QUIET_WINDOW_MS = 600
PAGE_SCROLL_OVERLAP_RATIO = 0.15
PAGE_SETTLE_CRITICAL_RESOURCE_TYPES = frozenset({"document", "xhr", "fetch", "script"})
MAX_OBSTRUCTION_CHAIN_DISMISSALS = 12
MAX_OBSTRUCTION_CHAIN_DURATION_MS = 45_000
OBSTRUCTION_CHAIN_CLICK_TIMEOUT_MS = 5_000
OBSTRUCTION_CHAIN_LAYER_SETTLE_MS = 600
OBSTRUCTION_CHAIN_NEXT_LAYER_TIMEOUT_MS = 5_000
OBSTRUCTION_CHAIN_NEXT_LAYER_POLL_MS = 250
MAX_UI_SNAPSHOT_ELEMENTS = 500
MAX_UI_SNAPSHOT_BYTES = 512 * 1024
MAX_DOM_ARTIFACT_BYTES = 2 * 1024 * 1024
MAX_UI_SELECTOR_LENGTH = 512
MAX_UI_RESOURCE_URL_LENGTH = 2048
MAX_UI_CLASS_TOKEN_LENGTH = 100
MAX_TEMPORAL_TEXT_LENGTH = 20_000
MIN_TEMPORAL_SAMPLES = 3
MAX_TIMED_CAPTURE_FRAMES = 100
MAX_TIMED_CAPTURE_INTERVAL_MS = 60_000
MAX_PROBE_TEXT_LENGTH = 80
MAX_INPUT_TEXT_LENGTH = 2_000
MAX_FAULT_URL_PATTERN_LENGTH = 500
MAX_FAULT_MATCH_DETAILS = 20
MAX_TRANSIENT_MESSAGE_EVENTS = 50
TRANSIENT_MESSAGE_OBSERVATION_WINDOW_MS = 1_000
MAX_VISUAL_NAVIGATION_PNG_CHUNKS = 4096
MAX_VISUAL_NAVIGATION_PNG_DIMENSION = 32768
MAX_VISUAL_NAVIGATION_PNG_PIXELS = 100_000_000
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_DOM_NON_SEMANTIC_BLOCK_PATTERN = re.compile(
    r"<(?P<tag>script|style|noscript|template)\b[^>]*>.*?</(?P=tag)\s*>",
    flags=re.IGNORECASE | re.DOTALL,
)
_DOM_INLINE_RESOURCE_ATTRIBUTE_PATTERN = re.compile(
    r"(?P<prefix>\b(?:src|srcset|href|poster|style)\s*=\s*)"
    r"(?P<quote>['\"])(?P<value>.*?)(?P=quote)",
    flags=re.IGNORECASE | re.DOTALL,
)
SAFE_PROBE_TEXT_PATTERN = re.compile(r"^ALIOTH_(?:RATE|COMMENT)_PROBE_[A-Za-z0-9_-]{1,48}$")
FLUTTER_SEMANTIC_NODE_SELECTOR_PATTERN = re.compile(r"^#flt-semantic-node-[0-9]+$")
LINE_LOADING_TEXT_MARKERS = (
    "正在获取线路",
    "正在获取最佳线路",
    "正在读取最快线路",
    "正在读取最快路线",
    "线路读取中",
    "线路读取完成",
    "获取线路",
    "读取线路",
    "线路加载",
)
STARTUP_AD_TEXT_MARKERS = (
    "跳过广告",
    "开屏广告",
    "启动广告",
    "秒后跳过",
)
GLOBAL_COUNT_DOWN_DISMISSAL_SELECTOR = '.count-down:text-is("跳过"):visible'
POPUP_SKIP_WRAP_STARTUP_AD_LAYER_SELECTOR = ".van-popup:visible:has(.skip-wrap:visible)"
POPUP_COUNT_DOWN_STARTUP_AD_LAYER_SELECTOR = (
    '.van-popup:visible:has(.count-down:text-is("跳过"):visible)'
)
FLUTTER_FULLSCREEN_STARTUP_AD_DISMISSAL_SELECTOR = "internal:flutter_fullscreen_startup_ad_skip"
# 开屏层右上角清障控件的文案：杏吧（Flutter）写「进入」而不是「跳过」，
# 两者是同一个角标控件，几何、唯一性与命中约束不变。
FLUTTER_STARTUP_DISMISS_TEXTS = ("跳过", "进入")
STARTUP_AD_LAYER_SELECTOR_BY_DISMISSAL = {
    STARTUP_AD_DISMISSAL_SELECTOR: ".loading-page:visible",
    GLOBAL_LAUNCH_DISMISSAL_SELECTOR: ".globalLaunch:visible",
    COUNTDOWN_JUMP_DISMISSAL_SELECTOR: ".wrapper:has(.countdown):visible",
    POPUP_SKIP_WRAP_DISMISSAL_SELECTOR: POPUP_SKIP_WRAP_STARTUP_AD_LAYER_SELECTOR,
    POPUP_COUNT_DOWN_DISMISSAL_SELECTOR: POPUP_COUNT_DOWN_STARTUP_AD_LAYER_SELECTOR,
    GLOBAL_COUNT_DOWN_DISMISSAL_SELECTOR: GLOBAL_COUNT_DOWN_DISMISSAL_SELECTOR,
    LAUNCH_PAGE_ENTRY_DISMISSAL_SELECTOR: ".launch-page:visible",
}
STARTUP_AD_COORDINATE_TEXT_CONTRACT_BY_DISMISSAL = {
    STARTUP_AD_DISMISSAL_SELECTOR: {
        "expected_text": None,
        "use_direct_child": False,
    },
    GLOBAL_LAUNCH_DISMISSAL_SELECTOR: {
        "expected_text": "跳过",
        "use_direct_child": False,
    },
    COUNTDOWN_JUMP_DISMISSAL_SELECTOR: {
        "expected_text": "跳过",
        "use_direct_child": False,
    },
    POPUP_SKIP_WRAP_DISMISSAL_SELECTOR: {
        "expected_text": "0s(跳过)",
        "use_direct_child": True,
    },
    POPUP_COUNT_DOWN_DISMISSAL_SELECTOR: {
        "expected_text": "跳过",
        "use_direct_child": False,
    },
    GLOBAL_COUNT_DOWN_DISMISSAL_SELECTOR: {
        "expected_text": "跳过",
        "use_direct_child": False,
    },
    LAUNCH_PAGE_ENTRY_DISMISSAL_SELECTOR: {
        "expected_text": "进入",
        "use_direct_child": True,
    },
}
POPUP_DIALOG_CONTENT_CLOSE_SELECTOR = (
    ':is([role="dialog"], .van-dialog):visible > .van-dialog__content '
    "> .btn:visible > img:visible, "
    ':is([role="dialog"], .van-dialog):visible > .van-dialog__content '
    "> :is(.btn-box.end, .btn-box.close):visible"
)
POPUP_DIALOG_ACTIVE_AD_CLOSE_SELECTOR = (
    ':is([role="dialog"], .van-dialog):visible '
    ".ad-item:visible .btn-box:visible "
    ".btn-close:visible:has(.b-img.active:visible), "
    ':is([role="dialog"], .van-dialog):visible '
    ".ad-item:visible .btn-box:visible .b-img.active:visible"
)
POPUP_DIALOG_TEXT_AD_CLOSE_SELECTOR = (
    ':is([role="dialog"], .van-dialog):visible > .van-dialog__content > .btn-box.end:visible'
)
POPUP_DIALOG_ACCESSIBLE_CLOSE_SELECTOR = (
    ':is([role="dialog"], [aria-modal="true"], .van-dialog):visible '
    ':is(button, [role="button"], a, input[type="button"], input[type="submit"])'
    ':is([aria-label*="关闭"], [aria-label*="close" i], '
    '[title*="关闭"], [title*="close" i], '
    '[data-testid*="close" i], [id*="close" i], [class*="close" i]):visible'
)
POPUP_DIALOG_ACKNOWLEDGEMENT_SELECTOR = ':is([role="dialog"], .van-dialog):visible .app-btn:visible'
POPUP_DIALOG_GENERIC_CLOSE_SELECTOR = (
    ':is([role="dialog"], .van-dialog) .close_btn:visible:has(.van-icon-close:visible), '
    ':is([role="dialog"], .van-dialog) .btn-close:visible, '
    ':is([role="dialog"], .van-dialog) .close-btn:visible, '
    ':is([role="dialog"], .van-dialog) .van-popup__close-icon:visible, '
    ':is([role="dialog"], .van-dialog) .van-icon-close:visible'
)
POPUP_HOME_SUSPEND_CLOSE_SELECTOR = ".home-suspend:visible .btn-close:visible"
POPUP_HOMEPAGE_PAYMENT_CLOSE_SELECTOR = ".homepagePaymentPopup .imgWrapper > .close:visible"
POPUP_FLUTTER_APP_OPEN_CLOSE_SELECTOR = "flt-semantics-host flt-semantics[flt-tappable]:visible"
POPUP_FLUTTER_CENTERED_AD_CLOSE_SELECTOR = "internal:flutter_centered_ad_popup_close"
POPUP_FLUTTER_CENTERED_AD_CLOSE_ABOVE_SELECTOR = "internal:flutter_centered_ad_popup_close_above"
POPUP_FLUTTER_CENTERED_SCROLL_CLOSE_BELOW_SELECTOR = (
    "internal:flutter_centered_scroll_popup_close_below"
)
POPUP_FLUTTER_IMAGE_TOP_RIGHT_CLOSE_SELECTOR = "internal:flutter_image_popup_top_right_close"
POPUP_FLUTTER_ACCOUNT_NOTICE_TOP_RIGHT_CLOSE_SELECTOR = (
    "internal:flutter_account_notice_top_right_close"
)
POPUP_FLUTTER_MARKETING_ANNOUNCEMENT_CONFIRM_SELECTOR = (
    "internal:flutter_marketing_announcement_confirm"
)
_TRUSTED_OPAQUE_POPUP_DISMISSAL_SELECTORS = frozenset(
    {
        POPUP_FLUTTER_APP_OPEN_CLOSE_SELECTOR.casefold(),
        POPUP_FLUTTER_CENTERED_AD_CLOSE_SELECTOR.casefold(),
        POPUP_FLUTTER_CENTERED_AD_CLOSE_ABOVE_SELECTOR.casefold(),
        POPUP_FLUTTER_CENTERED_SCROLL_CLOSE_BELOW_SELECTOR.casefold(),
        POPUP_FLUTTER_IMAGE_TOP_RIGHT_CLOSE_SELECTOR.casefold(),
        POPUP_FLUTTER_ACCOUNT_NOTICE_TOP_RIGHT_CLOSE_SELECTOR.casefold(),
        POPUP_FLUTTER_MARKETING_ANNOUNCEMENT_CONFIRM_SELECTOR.casefold(),
        H5_PROFILE_CREDENTIAL_SAVE_CONFIRM_SELECTOR.casefold(),
        H5_PROFILE_CREDENTIAL_SCREENSHOT_CONFIRM_SELECTOR.casefold(),
    }
)
_TRUSTED_AGE_CONFIRM_SELECTORS = frozenset({AGE_CONFIRM_ENTER_SELECTOR.casefold()})
_POPUP_STRUCTURE_REACQUIRE_TIMEOUT_MS = 2_000
_POPUP_STRUCTURE_REACQUIRE_POLL_MS = 250
TRANSIENT_GOTO_ERROR_MARKERS = (
    "net::ERR_CONNECTION_CLOSED",
    "net::ERR_CONNECTION_RESET",
    "net::ERR_NETWORK_CHANGED",
    "net::ERR_EMPTY_RESPONSE",
    'interrupted by another navigation to "chrome-error://chromewebdata/"',
)
# 导航失败后重试前，等 Chromium 错误文档提交的上限与轮询间隔（毫秒）
_NAVIGATION_ERROR_DOCUMENT_WAIT_MS = 2_000
_NAVIGATION_RETRY_POLL_MS = 50
BOTTOM_NAVIGATION_NATIVE_CLICK_TIMEOUT_MS = 5000
BOTTOM_NAVIGATION_EFFECT_TIMEOUT_MS = 1000
# 深层 SPA 可能把同 URL 状态也压入 history；一次 back 只会退出弹窗/同页状态，
# 不一定回到带 Tabbar 的业务页。恢复仍须严格有界并逐次验证唯一目标。
BOTTOM_NAVIGATION_HISTORY_RECOVERY_LIMIT = 3
PAGES_FOOT_SCOPED_CLICK_TIMEOUT_MS = 3000
PAGES_FOOT_DOM_EFFECT_TIMEOUT_MS = 2000
PAGES_FOOT_COMMUNITY_LABEL = "社区"
PAGES_FOOT_COMMUNITY_SELECTOR = (
    "div.pages-foot-box:visible ul.pages-foot-fix:visible > "
    'li:has(> .pages-foot-label:text-is("社区")):visible'
)
# 列表页详情入口常用集合 selector，滚动补采后 Playwright 默认点击第一个匹配项
# 可能落在视口外。只扫描有限数量，避免异常页面把一次点击拖成全 DOM 遍历。
MULTI_MATCH_VIEWPORT_SCAN_LIMIT = 50
MULTI_MATCH_VIEWPORT_CLICK_ATTEMPT_LIMIT = 5
MULTI_MATCH_VIEWPORT_CLICK_TIMEOUT_MS = 5000
MULTI_MATCH_DYNAMIC_PROBE_TIMEOUT_MS = 3000
MULTI_MATCH_CAROUSEL_TAB_SETTLE_MS = 300
# 点击前判断目标是否位于外站锚点：探测必须廉价，超时即放弃自动证据窗口。
EXTERNAL_ANCHOR_PROBE_TIMEOUT_MS = 1500
DISMISSAL_SELECTOR_MARKERS = (
    "关闭",
    "close",
    "dismiss",
    "skip",
    "跳过",
    "稍后",
    "我知道了",
    "overlay",
    "backdrop",
    "mask",
    "遮罩",
)


class PageTriggerAction(StrEnum):
    """单次最小触发允许的动作。"""

    CLICK = "click"
    BACK = "back"
    SCROLL = "scroll"
    HOVER = "hover"
    SEEK_MEDIA = "seek_media"
    FILL = "fill"
    REFRESH = "refresh"
    NETWORK_FAULT = "network_fault"
    SUBMIT_PROBE = "submit_probe"
    DIRECT_AD_OUTBOUND = "direct_ad_outbound"


class BlockedPageCaptureActionError(ValueError):
    """请求包含 page_capture_tool 明确拒绝的高风险动作。"""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "PAGE_CAPTURE_ACTION_BLOCKED",
    ) -> None:
        super().__init__(message)
        self.error_code = error_code


class BrowserSessionCrashedError(RuntimeError):
    """绑定 Run 的 Chromium 页面已经崩溃，必须重建页面会话。"""

    error_code = "PAGE_CAPTURE_SESSION_CRASHED"


class PageInteractionFailedError(RuntimeError):
    """页面交互目标无法安全执行，向 Tool 层暴露稳定诊断码。"""

    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable


VISUAL_ACTION_COMMITTED_CLEANUP_INTERRUPTED_CODE = "VISUAL_ACTION_COMMITTED_CLEANUP_INTERRUPTED"
VISUAL_ACTION_COMMITTED_CLEANUP_INTERRUPTED_MESSAGE = "视觉点击已提交，清理未完成，禁止自动重试"


class VisualActionCommittedCleanupInterruptedError(RuntimeError):
    """视觉点击已提交、但 wait_for 只观察到清理超时。"""


@dataclass(frozen=True, slots=True)
class _VisualNavigationClickCommit:
    """只在当前动作进程内保留的脱敏点击提交事实。"""

    click_committed: bool
    resolved_by: str
    visual_target_relocated: bool
    visual_match_score: float
    visual_audit_codes: tuple[str, ...]


class _VisualActionCommitHolder:
    """跨 asyncio 子任务边界传递的单请求私有提交槽。"""

    __slots__ = ("_commit",)

    def __init__(self) -> None:
        self._commit: _VisualNavigationClickCommit | None = None

    @property
    def commit_record(self) -> _VisualNavigationClickCommit | None:
        return self._commit

    def commit(self, record: _VisualNavigationClickCommit) -> None:
        if self._commit is not None:
            raise RuntimeError("视觉动作提交状态不得重复写入")
        # 单次引用赋值发生在 click 返回后、首次 cleanup await 前。
        self._commit = record

    def __repr__(self) -> str:
        return "_VisualActionCommitHolder()"

    def __deepcopy__(self, memo: dict[int, Any]) -> _VisualActionCommitHolder:
        del memo
        return type(self)()

    def __reduce__(self) -> tuple[type[_VisualActionCommitHolder], tuple[()]]:
        return (type(self), ())


@dataclass(frozen=True, slots=True)
class _VerifiedVisualNavigationBeforeFrame:
    """只在当前进程传递的已验签动作前帧。"""

    viewport_png: bytes = field(repr=False, compare=False)
    ui_snapshot: dict[str, Any] = field(repr=False, compare=False)
    viewport: dict[str, float]
    source_image_sha256: str


class _VisualNavigationFrameContext:
    """跨请求克隆共享实时前帧；复制和序列化时主动清空。"""

    __slots__ = ("_verified_before_frame",)

    def __init__(self) -> None:
        self._verified_before_frame: _VerifiedVisualNavigationBeforeFrame | None = None

    @property
    def verified_before_frame(self) -> _VerifiedVisualNavigationBeforeFrame | None:
        return self._verified_before_frame

    def bind(self, frame: _VerifiedVisualNavigationBeforeFrame) -> None:
        self._verified_before_frame = frame

    def clear(self) -> None:
        self._verified_before_frame = None

    def __repr__(self) -> str:
        return "_VisualNavigationFrameContext()"

    def __deepcopy__(self, memo: dict[int, Any]) -> _VisualNavigationFrameContext:
        del memo
        return type(self)()

    def __reduce__(self) -> tuple[type[_VisualNavigationFrameContext], tuple[()]]:
        return (type(self), ())


@dataclass(frozen=True)
class PageCaptureArtifact:
    """一次页面采集产生的结构化产物。"""

    artifact_type: str
    artifact_id: str | None
    content: Any

    def to_dict(self) -> dict[str, Any]:
        """转换为 Agent Tool 可直接返回的 JSON 兼容字典。"""
        return {
            "artifact_type": self.artifact_type,
            "artifact_id": self.artifact_id,
            "content": self.content,
        }


@dataclass(frozen=True)
class PageCaptureRequest:
    """页面采集请求。"""

    mode: PageCaptureMode
    url: str | None = None
    selector: str | None = None
    trigger_action: str | PageTriggerAction | None = None
    probe_text: str | None = None
    input_text: str | None = None
    submit_selector: str | None = None
    fault_url_pattern: str | None = None
    seek_fraction: float | None = None
    seek_time_seconds: float | None = None
    click_x_ratio: float | None = None
    click_y_ratio: float | None = None
    left_ratio: float | None = None
    top_ratio: float | None = None
    right_ratio: float | None = None
    bottom_ratio: float | None = None
    visual_confidence: float | None = None
    visual_source_url: str | None = None
    visual_entry_kind: str | None = None
    visual_expected_text: str | None = None
    screenshot_index: int | None = None
    capture_dom: bool = True
    capture_visible_text: bool = True
    capture_screenshot: bool = True
    capture_media_state: bool = False
    capture_checkpoint_observation: bool = False
    capture_transient_messages: bool = False
    capture_ad_landing_evidence: bool = False
    # 仅供 Web 广告证据调度器注入；浏览器直接在新页打开，不点击源页广告体。
    ad_landing_url: str | None = None
    # 仅 Web 广告适配器注入；不依赖业务页采样阶段，默认不影响 App/H5。
    web_ad_javascript: bool = False
    full_page: bool = True
    # 仅供 H5 支付页会员套餐容器使用；普通纵向滚动不设置。
    horizontal_scroll_max_segments: int | None = None
    frame_count: int = 3
    interval_ms: int = 1000
    timeout_ms: int = 30000
    # H5 详情页由受信 Runtime 固定为 2；None 保持其他采集路径的全局覆盖预算。
    scroll_max_segments: int | None = None
    split_scroll_screenshots: bool = False
    navigation_depth: int | None = None
    navigation_family: str | None = None
    navigation_index: int | None = None
    navigation_label: str | None = None
    interaction_pattern_signature: str | None = None
    clear_obstructions: bool = False
    # h5 保持线路加载/启动广告语义；Web V2 使用 direct_home 直接识别落地首页。
    startup_policy: str = "h5"
    # 仅 Web V2 显式标注；None 时保持现有 H5/App 行为。
    web_capture_stage: str | None = None
    web_full_page_stabilization_policy: WebFullPageStabilizationPolicy | None = None
    # 可信 Runtime 注入；T4 将绑定 Run 冻结快照，公开工具不暴露此参数。
    web_readiness_profile: WebReadinessProfile = DEFAULT_WEB_READINESS_PROFILE
    allow_high_z_obstruction_hide: bool = False
    h5_popup_dismissal_strategy: str | None = None
    # 普通 H5 探索允许在关闭一层后保留下一层后态，由 Frontier 继续清障；
    # 补采仍必须一次清空弹窗链，避免把遮挡截图误记为有效证据。
    allow_h5_popup_layer_continuation: bool = False
    verified_card_activation: str | None = None
    # 仅可信补证 Runtime 注入；公开工具不提供这个动作授权入口。
    _h5_probe_display_contract: dict[str, Any] | None = field(
        default=None, repr=False, compare=False,
    )
    # 多匹配 selector 的几何消歧：解析端选中元素的快照矩形。
    expected_rect: dict[str, float] | None = None
    session_id: str | None = None
    viewport_width: int | None = None
    viewport_height: int | None = None
    screen_width: int | None = None
    screen_height: int | None = None
    device_scale_factor: float | None = None
    is_mobile: bool | None = None
    has_touch: bool | None = None
    color_scheme: str | None = None
    # 入口动作的 before 采集：此刻尚未导航，会话里没有任何页面，
    # 但门禁要求 before/after 成对留证。置真时允许先开一个空白页再观察。
    bootstrap_blank_surface: bool = False
    # 仅由 Runtime 构造器注入，Agent Tool 不能自行开启逻辑页面决策。
    logical_surface_enabled: bool = False
    logical_surface_shadow: bool = False
    # 仅由当前浏览器会话注入，不能通过公开工具参数选择命令进程。
    _cli_session: CliBrowserSession | None = field(default=None, repr=False, compare=False)
    # replace 克隆共享本次操作槽；外层超时取消后仍可识别底层 CLI 的未知副作用。
    _cli_action_state: dict[str, str] = field(default_factory=dict, repr=False, compare=False)
    # 平台 Runtime 注入，在会话锁内、任何动作前复核实际页面来源；不接受 Agent 参数。
    _pre_action_validation: Any = field(default=None, repr=False, compare=False)
    # 仅 H5 入口或受控恢复 Runtime 注入；冻结边界拒绝落点才允许有界身份兼容重试。
    _h5_entry_landing_validation: Any = field(default=None, repr=False, compare=False)
    # 已确认是广告落地页时，动作发送前交给既有外跳专项，保留源页与回位链路。
    _force_legacy_ad_landing: bool = field(default=False, repr=False, compare=False)
    # 仅由同进程 Runtime 注入；不得进入公开 intent、日志或持久化 metadata。
    _visual_navigation_target: VisualNavigationRuntimeTarget | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    # 仅由 Runtime 在语义解析验签后注入，Agent 参数不能构造或覆盖。
    _logical_target_binding: LogicalTargetRuntimeBinding | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    # 每个 Runtime 请求独占；Agent 参数无法注入，也不参与 repr/比较/复制序列化。
    _visual_action_commit_holder: _VisualActionCommitHolder = field(
        default_factory=_VisualActionCommitHolder,
        init=False,
        repr=False,
        compare=False,
    )
    _visual_navigation_frame_context: _VisualNavigationFrameContext = field(
        default_factory=_VisualNavigationFrameContext,
        init=False,
        repr=False,
        compare=False,
    )


def _clone_page_capture_request(
    request: PageCaptureRequest,
    **changes: Any,
) -> PageCaptureRequest:
    """克隆请求时保留同一私有提交槽与实时前帧上下文。"""
    cloned = replace(request, **changes)
    object.__setattr__(
        cloned,
        "_visual_action_commit_holder",
        request._visual_action_commit_holder,
    )
    object.__setattr__(
        cloned,
        "_visual_navigation_frame_context",
        request._visual_navigation_frame_context,
    )
    return cloned


def _clone_quarantined_readonly_request(request: PageCaptureRequest) -> PageCaptureRequest:
    """为未知动作后的诊断采集建立严格只读请求。

    进程进入隔离态后仍要在原浏览器上下文里保存后态证据，但不能把原请求
    中潜在的辅助动作选项带进实现层。尤其是 ``full_page`` 截图可能改变
    页面滚动位置，弹窗清理、广告落地和视觉导航则可能继续驱动页面。
    这里只保留观察、DOM、文本、媒体和视口截图能力；动作结果仍保持
    ``unknown``，不会被诊断采集改写成成功。
    """
    return _clone_page_capture_request(
        request,
        capture_transient_messages=False,
        capture_ad_landing_evidence=False,
        full_page=False,
        clear_obstructions=False,
        allow_high_z_obstruction_hide=False,
        verified_card_activation=None,
        _force_legacy_ad_landing=False,
        _visual_navigation_target=None,
    )


@dataclass(frozen=True)
class PageCaptureResult:
    """页面采集结果。"""

    execution_status: str
    mode: str
    reachable: bool | None
    final_url: str | None
    title: str | None
    status_code: int | None
    artifacts: list[PageCaptureArtifact] = field(default_factory=list)
    console_logs: list[dict[str, Any]] = field(default_factory=list)
    network_requests: list[dict[str, Any]] = field(default_factory=list)
    action_trace: list[dict[str, Any]] = field(default_factory=list)
    post_action_snapshot: dict[str, Any] | None = None
    error_message: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转换为 Agent Tool 可直接返回的 JSON 兼容字典。"""
        return {
            "execution_status": self.execution_status,
            "mode": self.mode,
            "reachable": self.reachable,
            "final_url": self.final_url,
            "title": self.title,
            "status_code": self.status_code,
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "console_logs": self.console_logs,
            "network_requests": self.network_requests,
            "action_trace": self.action_trace,
            "post_action_snapshot": self.post_action_snapshot,
            "error_message": self.error_message,
            "metrics": self.metrics,
        }


class PageCaptureBrowser(Protocol):
    """浏览器采集处理器协议，Tool 层只依赖这个稳定接口。"""

    async def capture(self, request: PageCaptureRequest) -> PageCaptureResult:
        """执行一次页面采集。"""


async def capture_page(
    *,
    request: PageCaptureRequest,
    browser: PageCaptureBrowser,
) -> PageCaptureResult:
    """校验 page_capture_tool 边界后，委托浏览器处理层执行采集。"""
    _validate_request(request)
    return await browser.capture(request)


async def capture_page_with_timeout(
    *,
    request: PageCaptureRequest,
    browser: PageCaptureBrowser,
    timeout_seconds: float,
) -> PageCaptureResult:
    """校验采集边界，并在取得浏览器并发槽后执行页面级超时。"""
    _validate_request(request)
    try:
        capture_with_timeout = getattr(browser, "capture_with_timeout", None)
        if callable(capture_with_timeout):
            return await capture_with_timeout(request, timeout_seconds=timeout_seconds)
        if str(request.session_id or "").startswith("h5:"):
            return await run_h5_physical_operation(
                browser.capture(request), timeout_seconds=timeout_seconds,
            )
        return await asyncio.wait_for(browser.capture(request), timeout=timeout_seconds)
    except TimeoutError:
        if _visual_action_was_committed(request):
            raise VisualActionCommittedCleanupInterruptedError(
                VISUAL_ACTION_COMMITTED_CLEANUP_INTERRUPTED_MESSAGE
            ) from None
        raise


@dataclass
class _PlaywrightSession:
    """一个 Agent Run 独占的 Playwright 页面会话。"""

    playwright: Any
    browser: Any
    context: Any
    page: Any
    console_logs: list[dict[str, Any]] = field(default_factory=list)
    network_requests: list[dict[str, Any]] = field(default_factory=list)
    network_inflight: dict[int, dict[str, str]] = field(default_factory=dict)
    suppressed_request_ids: set[int] = field(default_factory=set)
    observers_muted: bool = False
    cli_session: CliBrowserSession | None = None
    stop_guard: H5BrowserStopGuard | None = None
    frame_observer: Any = None


@dataclass(frozen=True, slots=True, kw_only=True)
class H5BrowserSecretFormResult:
    """浏览器秘密窗口的安全结果，不包含 selector、秘密或衍生摘要。"""

    submitted: bool
    fields_scrubbed: bool
    hostname: str
    reason_code: str


class _SecretFormSubmitError(RuntimeError):
    """只携带固定安全原因码的秘密提交失败。"""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


async def _submit_secret_form_on_page(
    *,
    page: Any,
    target_hostname: str,
    phone_selector: str,
    password_selector: str,
    submit_selector: str,
    submit_semantic_target: str,
    allowed_request_hostnames: tuple[str, ...],
    phone: str,
    password: str,
    timeout_ms: int,
) -> H5BrowserSecretFormResult:
    """秘密填写原子窗口；任何失败只返回固定原因码。"""
    current_hostname = _page_hostname(page)
    if current_hostname != target_hostname:
        return H5BrowserSecretFormResult(
            submitted=False,
            fields_scrubbed=True,
            hostname=current_hostname or target_hostname,
            reason_code="secret_form_hostname_mismatch",
        )
    route_page = getattr(page, "route", None)
    unroute_page = getattr(page, "unroute", None)
    if not callable(route_page) or not callable(unroute_page):
        return H5BrowserSecretFormResult(
            submitted=False,
            fields_scrubbed=True,
            hostname=target_hostname,
            reason_code="secret_route_guard_unavailable",
        )

    phone_locator = page.locator(phone_selector)
    password_locator = page.locator(password_selector)
    submit_locator = page.locator(submit_selector)
    try:
        counts = (
            await phone_locator.count(),
            await password_locator.count(),
            await submit_locator.count(),
        )
    except Exception:  # noqa: BLE001 - selector 异常不得携带页面值
        counts = (0, 0, 0)
    if counts != (1, 1, 1):
        return H5BrowserSecretFormResult(
            submitted=False,
            fields_scrubbed=True,
            hostname=target_hostname,
            reason_code="secret_form_target_not_unique",
        )

    allowed_request_hostname_set = frozenset((target_hostname, *allowed_request_hostnames))
    guard_state = {
        "blocked_cross_hostname": False,
        "blocked_cross_hostname_post": False,
        "post_request_observed": False,
        "credential_request_observed": False,
    }

    async def same_hostname_guard(route: Any) -> None:
        request = getattr(route, "request", None)
        request_url = str(getattr(request, "url", "") or "")
        request_method = str(getattr(request, "method", "") or "").upper()
        is_post = request_method == "POST"
        is_credential_request = is_post and (
            _is_secret_credential_submit_request(
                request_url=request_url,
                semantic_target=submit_semantic_target,
            )
            or _request_contains_secret_credentials(
                request=request,
                phone=phone,
                password=password,
            )
        )
        request_hostname = _url_hostname(request_url)
        if request_hostname not in allowed_request_hostname_set:
            guard_state["blocked_cross_hostname"] = True
            if is_post:
                guard_state["blocked_cross_hostname_post"] = True
            logger.info(
                "H5 秘密窗口已中止未授权跨主机请求："
                "method=%s hostname=%s credential=%s path_kind=%s",
                request_method or "UNKNOWN",
                request_hostname or "invalid",
                is_credential_request,
                _secret_request_path_kind(request_url),
            )
            await route.abort()
            return
        if is_post:
            guard_state["post_request_observed"] = True
        if is_credential_request:
            guard_state["credential_request_observed"] = True
            logger.info(
                "H5 秘密窗口已观察到允许的认证请求：hostname=%s",
                request_hostname,
            )
        await route.continue_()

    try:
        await route_page("**/*", same_hostname_guard)
    except Exception:  # noqa: BLE001
        return H5BrowserSecretFormResult(
            submitted=False,
            fields_scrubbed=True,
            hostname=target_hostname,
            reason_code="secret_route_guard_unavailable",
        )

    phone_dirty = False
    password_dirty = False
    submitted = False
    reason_code = "secret_form_submit_failed"
    try:
        phone_dirty = True
        password_dirty = True
        if FLUTTER_SEMANTIC_NODE_SELECTOR_PATTERN.fullmatch(submit_selector):
            await _type_flutter_secret_locator(
                phone_locator,
                value=phone,
                timeout_ms=timeout_ms,
            )
            await _type_flutter_secret_locator(
                password_locator,
                value=password,
                timeout_ms=timeout_ms,
            )
            await _require_secret_locator_value(
                phone_locator,
                expected=phone,
                timeout_ms=timeout_ms,
            )
            await _require_secret_locator_value(
                password_locator,
                expected=password,
                timeout_ms=timeout_ms,
            )
        else:
            await phone_locator.fill(phone, timeout=timeout_ms)
            await password_locator.fill(password, timeout=timeout_ms)
        await _click_secret_submit_target(
            page=page,
            locator=submit_locator,
            selector=submit_selector,
            semantic_target=submit_semantic_target,
            timeout_ms=timeout_ms,
        )
        submitted = True
        wait_for_timeout = getattr(page, "wait_for_timeout", None)
        if callable(wait_for_timeout):
            try:
                await wait_for_timeout(min(timeout_ms, 750))
            except Exception:  # noqa: BLE001
                pass
        wait_for_load_state = getattr(page, "wait_for_load_state", None)
        if callable(wait_for_load_state):
            try:
                await wait_for_load_state(
                    "domcontentloaded",
                    timeout=min(timeout_ms, 3_000),
                )
            except Exception:  # noqa: BLE001
                pass
        current_hostname = _page_hostname(page) or target_hostname
        if (
            guard_state["blocked_cross_hostname_post"]
            and not guard_state["credential_request_observed"]
        ):
            submitted = False
            reason_code = "cross_hostname_request_blocked"
        elif current_hostname != target_hostname:
            submitted = False
            reason_code = "cross_hostname_after_submit"
        else:
            reason_code = "secret_form_submitted"
    except _SecretFormSubmitError as exc:
        submitted = False
        reason_code = exc.reason_code
        if reason_code == "secret_form_submit_no_effect":
            if (
                guard_state["blocked_cross_hostname_post"]
                and not guard_state["credential_request_observed"]
            ):
                reason_code = "cross_hostname_request_blocked"
            elif guard_state["credential_request_observed"]:
                submitted = True
                reason_code = "secret_form_submit_response_observe_required"
            elif guard_state["post_request_observed"]:
                reason_code = "secret_form_submit_noncredential_post_only"
            elif guard_state["blocked_cross_hostname"]:
                reason_code = "cross_hostname_request_blocked"
            else:
                reason_code = "secret_form_submit_no_request"
    except Exception:  # noqa: BLE001 - 浏览器异常可能包含秘密，禁止传播
        submitted = False
        reason_code = (
            "cross_hostname_request_blocked"
            if (
                guard_state["blocked_cross_hostname_post"]
                and not guard_state["credential_request_observed"]
            )
            else "secret_form_submit_failed"
        )
    finally:
        phone_scrubbed = not phone_dirty or await _scrub_secret_locator(
            phone_locator,
            timeout_ms=timeout_ms,
        )
        password_scrubbed = not password_dirty or await _scrub_secret_locator(
            password_locator,
            timeout_ms=timeout_ms,
        )
        fields_scrubbed = phone_scrubbed and password_scrubbed
        try:
            await unroute_page("**/*", same_hostname_guard)
        except Exception:  # noqa: BLE001
            if submitted:
                submitted = False
                reason_code = "secret_route_guard_cleanup_failed"
    if not fields_scrubbed:
        submitted = False
        reason_code = "secret_fields_scrub_failed"
    return H5BrowserSecretFormResult(
        submitted=submitted,
        fields_scrubbed=fields_scrubbed,
        hostname=current_hostname or target_hostname,
        reason_code=reason_code,
    )


async def _click_secret_submit_target(
    *,
    page: Any,
    locator: Any,
    selector: str,
    semantic_target: str,
    timeout_ms: int,
) -> None:
    """Flutter 语义按钮只对实时复验后的同一节点做一次受限分派。"""
    if FLUTTER_SEMANTIC_NODE_SELECTOR_PATTERN.fullmatch(selector) is None:
        await locator.click(timeout=timeout_ms)
        return
    state = await locator.evaluate(
        """element => ({
            tag: element.tagName.toLowerCase(),
            tappable: element.hasAttribute('flt-tappable'),
            text: (element.getAttribute('aria-label') || element.textContent || '').trim(),
            center_hit: (() => {
                const rect = element.getBoundingClientRect();
                const target = document.elementFromPoint(
                    rect.left + rect.width / 2,
                    rect.top + rect.height / 2,
                );
                return target === element || element.contains(target);
            })(),
        })"""
    )
    if (
        not isinstance(state, dict)
        or state.get("tag") != "flt-semantics"
        or state.get("tappable") is not True
        or state.get("center_hit") is not True
        or _normalize_secret_submit_label(state.get("text"))
        != _normalize_secret_submit_label(semantic_target)
    ):
        raise RuntimeError("Flutter 秘密提交目标实时复验失败")
    bounding_box = await locator.bounding_box()
    if not _valid_flutter_secret_submit_box(bounding_box):
        raise RuntimeError("Flutter 秘密提交目标几何复验失败")
    click = getattr(locator, "click", None)
    if not callable(click):
        raise RuntimeError("Flutter 秘密提交目标缺少指针输入能力")
    before_url = str(getattr(page, "url", "") or "")
    await click(timeout=timeout_ms)
    wait_for_timeout = getattr(page, "wait_for_timeout", None)
    stable_success_samples = 0
    for _ in range(20):
        if callable(wait_for_timeout):
            await wait_for_timeout(250)
        else:
            await asyncio.sleep(0.25)
        effect = await _flutter_secret_submit_effect_state(
            page=page,
            selector=selector,
        )
        feedback_code = str(effect.get("feedback_code") or "").strip()
        if feedback_code:
            raise _SecretFormSubmitError(feedback_code)
        has_success_effect = (
            str(effect.get("url") or "") != before_url
            or effect.get("submit_visible") is False
            or effect.get("dialog_visible") is True
            or effect.get("success_marker_visible") is True
        )
        stable_success_samples = stable_success_samples + 1 if has_success_effect else 0
        if stable_success_samples >= 2:
            return
    raise _SecretFormSubmitError("secret_form_submit_no_effect")


async def _type_flutter_secret_locator(
    locator: Any,
    *,
    value: str,
    timeout_ms: int,
) -> None:
    """先以真实指针激活 Flutter 编辑态，再逐键同步文本控制器。"""
    click = getattr(locator, "click", None)
    press = getattr(locator, "press", None)
    press_sequentially = getattr(locator, "press_sequentially", None)
    if not all(callable(operation) for operation in (click, press, press_sequentially)):
        raise _SecretFormSubmitError("secret_form_keyboard_input_unavailable")
    await click(timeout=timeout_ms)
    await press("ControlOrMeta+A", timeout=timeout_ms)
    await press("Backspace", timeout=timeout_ms)
    await press_sequentially(
        value,
        delay=15,
        timeout=timeout_ms,
    )
    await _require_secret_locator_value(
        locator,
        expected=value,
        timeout_ms=timeout_ms,
    )


async def _require_secret_locator_value(
    locator: Any,
    *,
    expected: str,
    timeout_ms: int,
) -> None:
    """只在秘密窗口内验证输入控制器值，不投影原文或摘要。"""
    input_value = getattr(locator, "input_value", None)
    if not callable(input_value):
        raise _SecretFormSubmitError("secret_form_value_sync_failed")
    if await input_value(timeout=min(timeout_ms, 3_000)) != expected:
        raise _SecretFormSubmitError("secret_form_value_sync_failed")


def _normalize_secret_submit_label(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _is_secret_credential_submit_request(
    *,
    request_url: str,
    semantic_target: str,
) -> bool:
    path_kind = _secret_request_path_kind(request_url)
    target = _normalize_secret_submit_label(semantic_target)
    if "注册" in target or "register" in target or "sign up" in target:
        return path_kind == "register"
    if "登录" in target or "login" in target or "sign in" in target:
        return path_kind == "login"
    return False


def _secret_request_path_kind(request_url: str) -> str:
    path = (
        unquote_to_bytes(urlsplit(request_url).path)
        .decode(
            "utf-8",
            errors="ignore",
        )
        .casefold()
    )
    if any(token in path for token in ("register", "signup", "sign-up")):
        return "register"
    if any(token in path for token in ("login", "signin", "sign-in")):
        return "login"
    return "unknown"


def _request_contains_secret_credentials(
    *,
    request: Any,
    phone: str,
    password: str,
) -> bool:
    """只在秘密窗口内做精确值比较，禁止投影请求体或衍生摘要。"""
    try:
        payload = getattr(request, "post_data_json", None)
        if callable(payload):
            payload = payload()
    except Exception:  # noqa: BLE001 - 请求体解析异常不得携带秘密
        payload = None
    if isinstance(payload, Mapping) and (
        payload.get("username") == phone and payload.get("password") == password
    ):
        return True
    try:
        raw_payload = getattr(request, "post_data", None)
        if callable(raw_payload):
            raw_payload = raw_payload()
    except Exception:  # noqa: BLE001 - 请求体解析异常不得携带秘密
        return False
    if not isinstance(raw_payload, str):
        return False
    try:
        form_payload = parse_qs(
            raw_payload,
            keep_blank_values=True,
            strict_parsing=True,
        )
    except ValueError:
        return False
    return form_payload.get("username") == [phone] and form_payload.get("password") == [password]


def _valid_flutter_secret_submit_box(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    raw = tuple(value.get(name) for name in ("x", "y", "width", "height"))
    if any(isinstance(item, bool) or not isinstance(item, int | float) for item in raw):
        return False
    x, y, width, height = (float(item) for item in raw)
    if not all(math.isfinite(item) for item in (x, y, width, height)):
        return False
    return (
        0 <= x <= 390
        and 0 <= y <= 844
        and 80 <= width <= 360
        and 28 <= height <= 90
        and x + width <= 390
        and y + height <= 844
    )


async def _flutter_secret_submit_effect_state(
    *,
    page: Any,
    selector: str,
) -> dict[str, Any]:
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return {}
    result = await evaluate(
        r"""
        selector => {
          // secretSubmitEffectProbe：只返回固定布尔与原因码，不返回页面原文。
          const visible = (element) => {
            if (!(element instanceof Element)) return false;
            const style = getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.display !== "none"
              && style.visibility !== "hidden"
              && Number(style.opacity || 1) > 0
              && rect.width > 0 && rect.height > 0;
          };
          const semanticText = [...document.querySelectorAll("flt-semantics")]
            .filter(visible)
            .map(element => String(
              element.getAttribute("aria-label") || element.textContent || "",
            ).trim())
            .filter(Boolean)
            .join(" ");
          const text = `${String(
            document.querySelector("flt-semantics-host")?.innerText || "",
          )} ${semanticText}`.replace(/\s+/g, " ").trim().slice(0, 50000);
          const lowered = text.toLowerCase();
          const feedbackSemanticText = [...document.querySelectorAll("flt-semantics")]
            .filter(element => visible(element) && !element.querySelector("input, textarea"))
            .map(element => String(
              element.getAttribute("aria-label") || element.textContent || "",
            ).trim())
            .filter(Boolean)
            .join(" ");
          let feedbackCode = null;
          if (/验证码|滑块|captcha|verification code/i.test(text)) {
            feedbackCode = "secret_form_challenge_visible";
          } else if (/请输入账号|请输入手机号/.test(feedbackSemanticText)) {
            feedbackCode = "secret_form_controller_username_empty";
          } else if (/请输入(?:登录)?密码/.test(feedbackSemanticText)) {
            feedbackCode = "secret_form_controller_password_empty";
          } else if (
            /操作频繁|注册频繁|过于频繁|请求频繁|稍后再试|稍后重试|人数已满|too many|rate limit/i
              .test(text)
          ) {
            feedbackCode = "registration_rate_limited";
          } else if (/已存在|已注册|already exists|already registered/i.test(text)) {
            feedbackCode = "registration_account_exists";
          } else if (/暂停注册|注册已关闭|暂未开放|registration unavailable/i.test(text)) {
            feedbackCode = "registration_unavailable";
          } else if (
            /注册失败|注册错误|注册异常|创建失败|无法注册|账号不可用|账号不合法|格式错误|请重试|网络异常|系统繁忙|invalid|failed|error/i
              .test(lowered)
          ) {
            feedbackCode = "registration_rejected";
          }
          const successMarkerVisible = (
            /注册成功|registration successful|registration success|signed up successfully/i
              .test(text)
          );
          const dialogs = [...document.querySelectorAll(
            '[role="dialog"], [role="alertdialog"], [aria-modal="true"]',
          )].filter(visible);
          const submit = document.querySelector(selector);
          return {
            url: location.href,
            submit_visible: visible(submit),
            dialog_visible: dialogs.length > 0,
            success_marker_visible: successMarkerVisible,
            feedback_code: feedbackCode,
          };
        }
        """,
        selector,
    )
    return result if isinstance(result, dict) else {}


async def _scrub_secret_locator(locator: Any, *, timeout_ms: int) -> bool:
    try:
        count = await locator.count()
        if count == 0:
            return True
        if count != 1:
            return False
        await locator.fill("", timeout=min(timeout_ms, 3_000))
        input_value = getattr(locator, "input_value", None)
        if not callable(input_value):
            return False
        return await input_value(timeout=min(timeout_ms, 3_000)) == ""
    except Exception:  # noqa: BLE001 - 清空失败只返回布尔安全状态
        return False


def _page_hostname(page: Any) -> str | None:
    return _url_hostname(str(getattr(page, "url", "") or ""))


def _url_hostname(url: str) -> str | None:
    raw = urlsplit(url).hostname
    if not raw:
        return None
    try:
        return normalize_h5_auth_hostname(raw)
    except ValueError:
        return None


@dataclass
class _CoverageState:
    """浏览器会话内同一路由的滚动覆盖累计状态。"""

    route_key: str
    content_fingerprints: set[str] = field(default_factory=set)
    segments: list[dict[str, Any]] = field(default_factory=list)
    consecutive_no_new: int = 0
    stable_height_samples: int = 0
    previous_scroll_height: float | None = None
    previous_visible_fingerprints: list[str] = field(default_factory=list)
    continuity_segment_index: int = 0
    continuity_gap_count: int = 0


class PlaywrightPageCaptureBrowser:
    """基于 Playwright 的浏览器采集处理器。"""

    def __init__(
        self,
        *,
        default_timeout_ms: int = 30000,
        max_concurrent_captures: int = 2,
        logical_surface_enabled: bool = False,
        logical_surface_shadow: bool = False,
        h5_visual_coordinate_mode: Literal["off", "shadow", "enforced"] = "off",
        proxy_server: str | None = None,
        proxy_bypass: str | None = None,
        cli_mode: Literal["off", "observe", "actions"] = "off",
        cli_script: str | None = None,
        cli_workspace_root: str | None = None,
    ) -> None:
        if max_concurrent_captures < 1:
            raise ValueError("页面采集并发数必须至少为 1")
        if h5_visual_coordinate_mode not in {"off", "shadow", "enforced"}:
            raise ValueError("h5_visual_coordinate_mode 必须为 off、shadow 或 enforced")
        self.default_timeout_ms = default_timeout_ms
        self.max_concurrent_captures = max_concurrent_captures
        self._logical_surface_enabled = bool(logical_surface_enabled)
        self._logical_surface_shadow = bool(logical_surface_enabled and logical_surface_shadow)
        self._h5_visual_coordinate_mode = h5_visual_coordinate_mode
        self._proxy_server = proxy_server
        self._proxy_bypass = proxy_bypass if proxy_server else None
        if cli_mode not in {"off", "observe", "actions"}:
            raise ValueError("CLI 模式必须为 off、observe 或 actions")
        if cli_mode != "off" and (not cli_script or not cli_workspace_root):
            raise ValueError("启用 CLI 必须配置脚本路径和会话根目录")
        self._cli_mode = cli_mode
        self._cli_script = cli_script
        self._cli_workspace_root = cli_workspace_root
        self._capture_semaphore = asyncio.Semaphore(max_concurrent_captures)
        self._sessions: dict[str, _PlaywrightSession] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._coverage_states: dict[str, _CoverageState] = {}
        self._invalidated_sessions: set[str] = set()
        self._quarantined_sessions: set[str] = set()

    async def _launch_browser(self, playwright: Any) -> Any:
        """统一 Chromium 启动参数，未显式配置代理时保持既有直连行为。"""
        launch_options: dict[str, Any] = {"headless": True}
        if self._proxy_server is not None:
            proxy: dict[str, str] = {"server": self._proxy_server}
            if self._proxy_bypass is not None:
                proxy["bypass"] = self._proxy_bypass
            launch_options["proxy"] = proxy
        return await playwright.chromium.launch(**launch_options)

    async def capture(self, request: PageCaptureRequest) -> PageCaptureResult:
        """执行一次采集；绑定 Run 时复用该 Run 的页面会话。"""
        async with self._capture_semaphore:
            return await self._capture_with_slot(request)

    async def capture_with_timeout(
        self,
        request: PageCaptureRequest,
        *,
        timeout_seconds: float,
    ) -> PageCaptureResult:
        """等待并发槽后，仅对真实浏览器采集阶段计时。"""
        if str(request.session_id or "").startswith("h5:"):
            if timeout_seconds <= 0:
                raise TimeoutError("H5 物理执行窗口已耗尽，禁止取得并发槽")
            await self._capture_semaphore.acquire()

            physical = asyncio.create_task(self._capture_with_slot(request))
            # 包含启动前取消；槽只随实际子操作结束释放，不能随上层超时释放。
            physical.add_done_callback(lambda _: self._capture_semaphore.release())
            try:
                return await run_h5_physical_operation(physical, timeout_seconds=timeout_seconds)
            except H5PhysicalOperationNotDrained:
                self._quarantined_sessions.add(request.session_id)
                raise
            except (asyncio.CancelledError, TimeoutError):
                quarantine_h5_journeys("H5_CAPTURE_STOP_UNCONFIRMED")
                raise
        async with self._capture_semaphore:
            return await asyncio.wait_for(
                self._capture_with_slot(request),
                timeout=timeout_seconds,
            )

    async def submit_secret_form(
        self,
        *,
        session_id: str,
        target_hostname: str,
        phone_selector: str,
        password_selector: str,
        submit_selector: str,
        submit_semantic_target: str,
        allowed_request_hostnames: tuple[str, ...] = (),
        phone: str,
        password: str,
        timeout_ms: int,
        pre_action_validation: Any = None,
    ) -> H5BrowserSecretFormResult:
        """在同一 Session 锁内填写、提交并清空登录表单，不执行任何采集。"""
        for field_name, value in (
            ("session_id", session_id),
            ("phone_selector", phone_selector),
            ("password_selector", password_selector),
            ("submit_selector", submit_selector),
            ("submit_semantic_target", submit_semantic_target),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{field_name} 必须为非空字符串")
        if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or timeout_ms <= 0:
            raise ValueError("timeout_ms 必须为正整数")
        hostname = normalize_h5_auth_hostname(target_hostname)
        if not isinstance(allowed_request_hostnames, tuple):
            raise ValueError("allowed_request_hostnames 必须为 tuple")
        allowed_hostnames = tuple(
            normalize_h5_auth_hostname(item) for item in allowed_request_hostnames
        )
        if len(allowed_hostnames) != len(set(allowed_hostnames)):
            raise ValueError("allowed_request_hostnames 不得重复")
        async with self._capture_semaphore:
            lock = self._session_locks.setdefault(session_id, asyncio.Lock())
            async with lock:
                session = self._sessions.get(session_id)
                if session is None:
                    return H5BrowserSecretFormResult(
                        submitted=False,
                        fields_scrubbed=True,
                        hostname=hostname,
                        reason_code="secret_form_session_missing",
                    )
                if session.cli_session is not None:
                    session.cli_session.process.require_action_session()
                if pre_action_validation is not None:
                    await pre_action_validation(str(session.page.url))
                _mute_session_observers(session)
                try:
                    return await _submit_secret_form_on_page(
                        page=session.page,
                        target_hostname=hostname,
                        phone_selector=phone_selector,
                        password_selector=password_selector,
                        submit_selector=submit_selector,
                        submit_semantic_target=submit_semantic_target,
                        allowed_request_hostnames=allowed_hostnames,
                        phone=phone,
                        password=password,
                        timeout_ms=timeout_ms,
                    )
                finally:
                    _clear_session_observer_buffers(session)

    async def _capture_with_slot(self, request: PageCaptureRequest) -> PageCaptureResult:
        """在浏览器采集并发槽内执行一次真实采集。"""
        _validate_ad_capture_request(request)
        if request.session_id in self._quarantined_sessions:
            raise H5PhysicalOperationNotDrained("原浏览器会话物理排空未确认，禁止恢复或后续采集")
        if self._cli_mode != "off" and not request.session_id:
            raise ValueError("CLI 采集必须绑定平台会话，禁止静默使用无归属浏览器")
        request = _clone_page_capture_request(
            request,
            logical_surface_enabled=self._logical_surface_enabled,
            logical_surface_shadow=self._logical_surface_shadow,
        )
        if (
            request._visual_navigation_target is not None
            and self._h5_visual_coordinate_mode != "enforced"
        ):
            raise ValueError("当前视觉导航执行模式不是 enforced，禁止视觉导航执行")
        try:
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError("缺少 Playwright 依赖，请先安装 playwright 并安装 chromium") from exc

        if request.session_id is None:
            async with async_playwright() as playwright:
                browser = await self._launch_browser(playwright)
                try:
                    context_options = _browser_context_options(request)
                    if request.startup_policy == "direct_home":
                        context_options["user_agent"] = await web_chrome_user_agent(browser)
                    context = await browser.new_context(
                        ignore_https_errors=True,
                        **context_options,
                    )
                    if request.logical_surface_enabled:
                        await _install_logical_surface_history_observer(context)
                    page = await context.new_page()
                    console_logs: list[dict[str, Any]] = []
                    network_requests: list[dict[str, Any]] = []
                    network_inflight: dict[int, dict[str, str]] = {}
                    _attach_observers(
                        page,
                        console_logs,
                        network_requests,
                        network_inflight,
                    )
                    return await self._capture_on_page(
                        request=request,
                        context=context,
                        page=page,
                        console_logs=console_logs,
                        network_requests=network_requests,
                        network_inflight=network_inflight,
                        timeout_error_type=PlaywrightTimeoutError,
                    )
                finally:
                    await browser.close()

        lock = self._session_locks.setdefault(request.session_id, asyncio.Lock())
        async with lock:
            session = self._sessions.get(request.session_id)
            if session is None:
                if not request.url and not request.bootstrap_blank_surface:
                    if request.session_id in self._invalidated_sessions:
                        raise BrowserSessionCrashedError(
                            "浏览器页面会话已崩溃，请使用 goto 模式恢复来源页面"
                        )
                    raise ValueError("当前 Agent Run 尚未建立页面会话，请先使用 goto 模式打开 URL")
                if request.bootstrap_blank_surface and request.session_id in (
                    self._invalidated_sessions
                ):
                    # 崩溃恢复不能借道入口通道：会话曾有真实页面，必须 goto 回原页
                    raise BrowserSessionCrashedError(
                        "浏览器页面会话已崩溃，请使用 goto 模式恢复来源页面"
                    )
                session = await self._create_session(async_playwright, request=request)
                self._sessions[request.session_id] = session
                self._invalidated_sessions.discard(request.session_id)
            try:
                if request.session_id.startswith("h5:") and session.stop_guard is not None:
                    bind_h5_physical_resource(request.session_id, session.stop_guard)
                result = await self._capture_on_page(
                    request=_clone_page_capture_request(request, _cli_session=session.cli_session),
                    context=session.context,
                    page=session.page,
                    console_logs=session.console_logs,
                    network_requests=session.network_requests,
                    network_inflight=session.network_inflight,
                    timeout_error_type=PlaywrightTimeoutError,
                )
                return await self._retry_h5_entry_profile(
                    request=request,
                    session=session,
                    result=result,
                    timeout_error_type=PlaywrightTimeoutError,
                )
            except Exception as exc:
                if not _is_terminal_browser_session_error(exc):
                    raise
                if request.session_id.startswith("h5:"):
                    try:
                        await _dispose_playwright_session(session, require_stop_proof=True)
                    except BaseException:
                        self._quarantined_sessions.add(request.session_id)
                        raise
                self._sessions.pop(request.session_id, None)
                self._coverage_states.pop(request.session_id, None)
                self._invalidated_sessions.add(request.session_id)
                if not request.session_id.startswith("h5:"):
                    await _dispose_playwright_session(session)
                raise BrowserSessionCrashedError(str(exc) or "Chromium 页面会话已崩溃") from exc

    async def _retry_h5_entry_profile(
        self,
        *,
        request: PageCaptureRequest,
        session: _PlaywrightSession,
        result: PageCaptureResult,
        timeout_error_type: type[Exception],
    ) -> PageCaptureResult:
        """H5 入口不可用时试一次移动 Context，不更改有效入口及后续动作身份。"""
        validate = request._h5_entry_landing_validation
        if (
            not callable(validate)
            or request.mode is not PageCaptureMode.GOTO
            or request.startup_policy != "h5"
            or session.cli_session is not None
            or result.execution_status != "success"
        ):
            return result

        native_scope_verified = await validate(result.final_url)
        document = result.metrics.get("navigation_document")
        native_document_unavailable = (
            isinstance(document, Mapping) and document.get("inspectable") is False
        ) or (
            result.status_code is not None
            and (result.status_code >= 400 or result.status_code in {204, 205})
        )
        if native_scope_verified and not native_document_unavailable:
            return result

        attempts = [{
            "profile": "native",
            "final_url": result.final_url,
            "status_code": result.status_code,
            "scope_verified": native_scope_verified,
            "document_available": not native_document_unavailable,
            "navigation": result.action_trace,
        }]
        context_options = _browser_context_options(request)
        context_options["user_agent"] = session.playwright.devices["Pixel 7"]["user_agent"]
        context = None
        selected = False
        validating = False
        native_coverage = self._coverage_states.pop(request.session_id, None)
        try:
            context = await session.browser.new_context(ignore_https_errors=True, **context_options)
            from services.api.app.infrastructure.browser.h5_readonly_display import (
                install_h5_readonly_display_guard,
            )

            await install_h5_readonly_display_guard(context)
            if request.logical_surface_enabled:
                await _install_logical_surface_history_observer(context)
            page = await context.new_page()
            frame_observer = await install_h5_canvas_observer(page)
            replacement = _PlaywrightSession(
                playwright=session.playwright,
                browser=session.browser,
                context=context,
                page=page,
                stop_guard=session.stop_guard,
                frame_observer=frame_observer,
            )
            _attach_observers(
                page, replacement.console_logs, replacement.network_requests,
                replacement.network_inflight,
                is_muted=lambda: replacement.observers_muted,
                suppressed_request_ids=replacement.suppressed_request_ids,
            )
            candidate = await self._capture_on_page(
                request=_clone_page_capture_request(request, _h5_entry_landing_validation=None),
                context=context,
                page=page,
                console_logs=replacement.console_logs,
                network_requests=replacement.network_requests,
                network_inflight=replacement.network_inflight,
                timeout_error_type=timeout_error_type,
            )
            validating = True
            candidate_scope_verified = await validate(candidate.final_url)
            candidate_document = candidate.metrics.get("navigation_document")
            candidate_document_available = (
                isinstance(candidate_document, Mapping)
                and candidate_document.get("inspectable") is True
                and (candidate.status_code is None or (
                    candidate.status_code < 400 and candidate.status_code not in {204, 205}
                ))
            )
            accepted = (
                candidate.execution_status == "success"
                and candidate_scope_verified and candidate_document_available
            )
            validating = False
            attempts.append({
                "profile": "playwright_mobile",
                "final_url": candidate.final_url,
                "status_code": candidate.status_code,
                "scope_verified": candidate_scope_verified,
                "document_available": candidate_document_available,
                "selected": accepted,
                "navigation": candidate.action_trace,
            })
            if accepted:
                self._sessions[request.session_id] = replacement
                selected = True
                result = candidate
                try:
                    await session.context.close()
                except Exception:  # noqa: BLE001 - 已验证的新 Context 不因旧 Context 清理失败回退
                    logger.warning("H5 首跳身份兼容后旧 Context 清理失败", exc_info=True)
        except Exception as exc:  # noqa: BLE001 - 兼容失败保留原越界事实，不伪造已到达
            if validating:
                raise
            attempts.append({
                "profile": "playwright_mobile",
                "scope_verified": False,
                "error_type": type(exc).__name__,
            })
        finally:
            if not selected:
                if context is not None:
                    try:
                        await context.close()
                    except Exception:  # noqa: BLE001 - 清理失败不得遮蔽原执行或权限异常
                        logger.warning("H5 首跳身份兼容 Context 清理失败", exc_info=True)
                self._coverage_states.pop(request.session_id, None)
                if native_coverage is not None:
                    self._coverage_states[request.session_id] = native_coverage
        audit = {"action": "h5_entry_profile_compatibility", "attempts": attempts}
        trace = [audit, *result.action_trace]
        artifacts = [
            replace(item, content=trace) if item.artifact_type == "action_trace" else item
            for item in result.artifacts
        ]
        return replace(
            result,
            action_trace=trace,
            artifacts=artifacts,
            metrics={**result.metrics, "h5_entry_profile_compatibility": attempts},
        )

    async def close_session(self, session_id: str) -> dict[str, Any] | None:
        """关闭一个 Agent Run 的浏览器会话并释放 Chromium 资源。"""
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        proof = None
        async with lock:
            session = self._sessions.get(session_id)
            if session is not None:
                try:
                    proof = await _dispose_playwright_session(
                        session, require_stop_proof=session_id.startswith("h5:"),
                    )
                except BaseException:
                    if session_id.startswith("h5:"):
                        self._quarantined_sessions.add(session_id)
                    raise
                self._sessions.pop(session_id, None)
                self._quarantined_sessions.discard(session_id)
            elif session_id in self._quarantined_sessions:
                raise H5PhysicalOperationNotDrained("H5 原会话句柄缺失，停止证明未确认")
            self._coverage_states.pop(session_id, None)
            self._invalidated_sessions.discard(session_id)
        self._session_locks.pop(session_id, None)
        return {**proof, 'session_id': session_id} if proof is not None else None

    async def _create_session(
        self, async_playwright: Any, *, request: PageCaptureRequest
    ) -> _PlaywrightSession:
        playwright = await async_playwright().start()
        cli_session = None
        browser = None
        frame_observer = None
        try:
            context_options = _browser_context_options(request)
            if self._cli_mode != "off":
                if request.startup_policy == "direct_home":
                    probe_browser = await self._launch_browser(playwright)
                    try:
                        context_options["user_agent"] = await web_chrome_user_agent(
                            probe_browser
                        )
                    finally:
                        await probe_browser.close()
                cli_session = CliBrowserSession(
                    session_id=request.session_id,
                    root=Path(self._cli_workspace_root),
                    script=Path(self._cli_script),
                    mode=self._cli_mode,
                )
                proxy = None
                if self._proxy_server:
                    proxy = {"server": self._proxy_server}
                    if self._proxy_bypass:
                        proxy["bypass"] = self._proxy_bypass
                context = await cli_session.start(
                    playwright,
                    context_options=context_options,
                    proxy=proxy,
                )
                browser = context.browser
                page = context.pages[0]
            else:
                browser = await self._launch_browser(playwright)
                if request.startup_policy == "direct_home":
                    context_options["user_agent"] = await web_chrome_user_agent(browser)
                context = await browser.new_context(
                    ignore_https_errors=True,
                    **context_options,
                )
                page = await context.new_page()
            if str(request.session_id or "").startswith("h5:"):
                from services.api.app.infrastructure.browser.h5_readonly_display import (
                    install_h5_readonly_display_guard,
                )

                await install_h5_readonly_display_guard(context)
                frame_observer = await install_h5_canvas_observer(page)
            if request.logical_surface_enabled:
                await _install_logical_surface_history_observer(context)
            session = _PlaywrightSession(
                playwright=playwright,
                browser=browser,
                context=context,
                page=page,
                cli_session=cli_session,
                frame_observer=frame_observer,
                stop_guard=(await H5BrowserStopGuard.bind(
                    browser, owned_local=True, playwright=playwright, cli_session=cli_session,
                ) if str(request.session_id or "").startswith("h5:") else None),
            )
            _attach_observers(
                page,
                session.console_logs,
                session.network_requests,
                session.network_inflight,
                is_muted=lambda: session.observers_muted,
                suppressed_request_ids=session.suppressed_request_ids,
            )
            return session
        except BaseException:
            if cli_session is not None:
                try:
                    await cli_session.close()
                except Exception:
                    pass
            if browser is not None:
                try:
                    await browser.close()
                except Exception:
                    pass
            try:
                await playwright.stop()
            finally:
                if cli_session is not None:
                    cli_session.cleanup()
            raise

    async def _capture_on_page(
        self,
        *,
        request: PageCaptureRequest,
        context: Any,
        page: Any,
        console_logs: list[dict[str, Any]],
        network_requests: list[dict[str, Any]],
        timeout_error_type: type[Exception],
        network_inflight: dict[int, dict[str, str]] | None = None,
    ) -> PageCaptureResult:
        """执行单页采集，并在完整退出边界释放视觉动作私有前帧。"""
        diagnostic_readonly = False
        cli_active_on_entry = (
            request._cli_session is not None and not request._cli_session.process.is_quarantined
        )
        try:
            if request._pre_action_validation is not None:
                await request._pre_action_validation(str(page.url))
            if request._cli_session is not None:
                process = request._cli_session.process
                # 动作结果不确定时保留同一浏览器上下文做只读诊断，避免
                # 后态证据丢失；goto、single_trigger 等任何可能改变页面的
                # 请求仍必须经过动作会话门禁。
                diagnostic_readonly = process.is_quarantined and request.mode in {
                    PageCaptureMode.OBSERVE,
                    PageCaptureMode.TIMED_CAPTURE,
                }
                if not diagnostic_readonly:
                    process.require_action_session()
                if diagnostic_readonly:
                    # 只读后态必须使用视口截图，避免 full_page 在采集时滚动页面；
                    # 同时清掉所有会触发清障、外跳或视觉点击的私有/辅助选项。
                    request = _clone_quarantined_readonly_request(request)
            result = await self._capture_on_page_impl(
                request=request,
                context=context,
                page=page,
                console_logs=console_logs,
                network_requests=network_requests,
                timeout_error_type=timeout_error_type,
                network_inflight=network_inflight,
            )
            if str(request.session_id or "").startswith("h5:") and request.capture_screenshot:
                result.metrics["h5_frame_origin"] = await read_h5_frame_origin(page)
                result.metrics["h5_frame_origin"]["observation_role"] = "post_capture_diagnostic"
                result = annotate_h5_frame_origins(result)
            if request._cli_session is not None:
                try:
                    observation = await request._cli_session.observe(page)
                    # 对比观察是独立时序的只读执行日志，不改写正式动作列表或最后动作。
                    entry = {
                        "action": "observe",
                        "provider_observation": observation,
                        "comparison_only": True,
                        "diagnostic_capture": request._cli_session.process.is_quarantined,
                        **(
                            {"diagnostic_role": "post_action_readonly_unknown"}
                            if diagnostic_readonly
                            else {}
                        ),
                        **(
                            {
                                "provider_outcome": "unknown",
                                "quarantine_reason": (
                                    request._cli_session.process.quarantine_reason
                                    or "action_outcome_uncertain"
                                ),
                            }
                            if request._cli_session.process.is_quarantined
                            else {}
                        ),
                    }
                    for index, artifact in enumerate(result.artifacts):
                        if artifact.artifact_type == "action_trace":
                            result.artifacts[index] = replace(
                                artifact,
                                content=[entry, *artifact.content],
                            )
                            break
                    else:
                        result.artifacts.append(
                            PageCaptureArtifact(
                                artifact_type="action_trace",
                                artifact_id=None,
                                content=[entry],
                            )
                        )
                    result.metrics["playwright_cli"] = {
                        "status": (
                            "quarantined_readonly"
                            if request._cli_session.process.is_quarantined
                            else "observed"
                        ),
                        "version": observation["version"],
                        "mode": request._cli_session.mode,
                        "same_url": result.final_url == observation["url"],
                        "same_title": result.title == observation["title"],
                        "observation_sha256": observation_digest(observation),
                        "comparison_only": True,
                        "diagnostic_capture": request._cli_session.process.is_quarantined,
                        **(
                            {"diagnostic_role": "post_action_readonly_unknown"}
                            if diagnostic_readonly
                            else {}
                        ),
                        **(
                            {
                                "provider_outcome": "unknown",
                                "quarantine_reason": (
                                    request._cli_session.process.quarantine_reason
                                    or "action_outcome_uncertain"
                                ),
                            }
                            if request._cli_session.process.is_quarantined
                            else {}
                        ),
                    }
                except Exception as exc:
                    # 只读对比失败独立记录，不能反向修改现有采集或页面到达结论。
                    result.metrics["playwright_cli"] = {
                        "status": "unavailable",
                        "mode": request._cli_session.mode,
                        "reason": getattr(exc, "error_code", type(exc).__name__),
                    }
            return result
        except BaseException:
            if (
                cli_active_on_entry
                and request._cli_session is not None
                and request._cli_session.process.quarantine_reason == "action_outcome_uncertain"
            ):
                request._cli_action_state["outcome"] = "unknown"
            raise
        finally:
            if request.web_capture_stage is not None:
                try:
                    await _restore_web_capture_motion(page)
                except Exception:  # noqa: BLE001 - 清理失败不能覆盖采集原始异常
                    pass
            request._visual_navigation_frame_context.clear()

    async def _capture_on_page_impl(
        self,
        *,
        request: PageCaptureRequest,
        context: Any,
        page: Any,
        console_logs: list[dict[str, Any]],
        network_requests: list[dict[str, Any]],
        timeout_error_type: type[Exception],
        network_inflight: dict[int, dict[str, str]] | None = None,
    ) -> PageCaptureResult:
        _validate_ad_capture_request(request)
        request = _clone_page_capture_request(
            request,
            logical_surface_enabled=self._logical_surface_enabled,
            logical_surface_shadow=self._logical_surface_shadow,
        )
        capture_started_at = time.perf_counter()
        timeout_ms = request.timeout_ms or self.default_timeout_ms
        page.set_default_timeout(timeout_ms)
        console_start = len(console_logs)
        network_start = len(network_requests)
        action_trace: list[dict[str, Any]] = []
        timings_ms: dict[str, float] = {}
        dom_collection_audit: dict[str, Any] = {}
        response_status: int | None = None
        scroll_artifacts: list[PageCaptureArtifact] = []
        page_settle_complete = True
        page_inspection_ready = True
        page_settle_diagnostics: dict[str, Any] = {
            "outcome": "settled",
            "blocking_reasons": [],
            "dynamic_reasons": [],
        }
        obstruction_chain_metrics: dict[str, Any] | None = None
        obstruction_frames: list[dict[str, Any]] = []
        transient_message_frames: list[dict[str, Any]] = []
        prepared_visual_snapshot: dict[str, Any] | None = None
        prepared_visual_viewport_png: bytes | None = None
        web_artifact_identity_before: str | None = None
        web_artifact_identity_after: str | None = None
        web_readiness_snapshot: dict[str, Any] | None = None
        web_structure_before: dict[str, Any] = {}
        web_structure_after: dict[str, Any] = {}
        web_capture_started_ms: float | None = None
        web_capture_ended_ms: float | None = None
        web_capture_stabilization: dict[str, Any] | None = None
        settle_waiter = (
            _wait_for_web_page_settle
            if request.web_capture_stage is not None
            else _wait_for_page_settle
        )
        web_wait_options = (
            {"profile": request.web_readiness_profile}
            if request.web_capture_stage is not None
            else {"allow_dynamic_business": str(request.session_id or "").startswith("h5:")}
        )
        navigation_retry: dict[str, Any] | None = None
        web_redirect_chain: tuple[str, ...] = ()
        observe_before_snapshot: dict[str, Any] | None = None
        observe_before_url = str(getattr(page, "url", "") or "")
        observe_before_page_urls: list[str] = []

        ad_landing_frames: list[dict[str, Any]] = []
        ad_landing_observation: dict[str, Any] | None = None
        ad_landing_dom: dict[str, Any] | None = None
        ad_landing_screenshot: dict[str, Any] | None = None
        ad_landing_ui_snapshot: dict[str, Any] | None = None
        ad_landing_har: dict[str, Any] | None = None
        ad_landing_window = request.capture_ad_landing_evidence

        _validate_url_mode_contract(page=page, request=request)
        if request.mode == PageCaptureMode.GOTO and request.url and ad_landing_window:
            stage_started_at = time.perf_counter()
            pre_trigger_url = str(getattr(page, "url", "") or "")
            pages_before = _context_pages(context)
            observed_new_pages: list[Any] = []
            page_event_handler = _subscribe_new_pages(
                context=context,
                observed_pages=observed_new_pages,
            )
            navigation_outcome_recorded = False
            source_frame = await _capture_ad_navigation_frame(
                page=page,
                index=0,
                role="source",
            )
            if source_frame is not None:
                ad_landing_frames.append(source_frame)
            try:
                ad_landing_observation = await _run_isolated_ad_goto(
                    page=page,
                    context=context,
                    request=request,
                    action_trace=action_trace,
                    timeout_ms=timeout_ms,
                )
                ad_landing_observation = await _record_navigation_outcome(
                    page=page,
                    context=context,
                    before_url=pre_trigger_url,
                    pages_before=pages_before,
                    observed_new_pages=observed_new_pages,
                    action_trace=action_trace,
                    timeout_ms=timeout_ms,
                    capture_ad_landing_evidence=True,
                    isolated_landing=ad_landing_observation,
                )
                navigation_outcome_recorded = True
            finally:
                if not navigation_outcome_recorded:
                    await _close_unclaimed_new_pages(
                        context=context,
                        pages_before=pages_before,
                        observed_new_pages=observed_new_pages,
                    )
                _unsubscribe_new_pages(
                    context=context,
                    page_event_handler=page_event_handler,
                )
            landing_frame = ad_landing_observation.pop("frame", None)
            ad_landing_dom = ad_landing_observation.pop("dom", None)
            ad_landing_screenshot = ad_landing_observation.pop("screenshot", None)
            ad_landing_ui_snapshot = ad_landing_observation.pop("ui_snapshot", None)
            ad_landing_har = ad_landing_observation.pop("har", None)
            if isinstance(landing_frame, dict):
                ad_landing_frames.append(landing_frame)
            _attach_ad_navigation_evidence(
                action_trace=action_trace,
                before_url=pre_trigger_url,
                after_url=str(getattr(page, "url", "") or ""),
                source_target=None,
                landing=ad_landing_observation,
                frame_count=len(ad_landing_frames),
            )
            timings_ms["isolated_navigation_and_capture"] = _elapsed_ms(stage_started_at)
        elif request.mode == PageCaptureMode.GOTO and request.url:
            from services.api.app.infrastructure.browser.h5_readonly_display import (
                authorize_h5_readonly_goto,
            )

            authorize_h5_readonly_goto(page, request.url)
            stage_started_at = time.perf_counter()
            goto_before_url = str(getattr(page, "url", "") or "")
            goto_before_snapshot = None
            goto_before_pages = _context_pages(context)
            goto_before_page_urls = _context_page_urls(context)
            if (
                request.logical_surface_enabled
                and str(request.session_id or "").startswith("h5:")
                and goto_before_url not in {"", "about:blank"}
            ):
                # H5 直开与点击共用真实前后状态证明；仅 URL 到达不能证明业务页面切换。
                goto_before_snapshot = await _collect_ui_snapshot(
                    page,
                    logical_surface_enabled=request.logical_surface_enabled,
                    logical_surface_shadow=request.logical_surface_shadow,
                    surface_session_id=request.session_id,
                )
            navigation_settled: bool | None = None
            if request._cli_session is not None:
                request._cli_session.set_initial_url(request.url)
            if request._cli_session is not None and request._cli_session.mode == "actions":
                receipt, response = await request._cli_session.navigate(page, request.url)
                action_trace.append({"action": "navigate", **receipt})
            else:
                navigation_retry = {}

                async def validate_committed_document(current_page: Any) -> str | None:
                    nonlocal navigation_settled
                    navigation_settled = await settle_waiter(
                        page=current_page,
                        network_inflight=network_inflight,
                        startup_policy=request.startup_policy,
                        diagnostics=page_settle_diagnostics,
                        **web_wait_options,
                    )
                    readiness = await _read_page_readiness(current_page)
                    if readiness is not None and readiness.get("blank_document") is True:
                        return "blank_document"
                    return None

                response = await _goto_with_retry(
                    page=page,
                    url=request.url,
                    timeout_ms=timeout_ms,
                    timeout_error_type=timeout_error_type,
                    audit=navigation_retry,
                    post_navigation_validator=(
                        validate_committed_document
                        if request.web_capture_stage is not None
                        else None
                    ),
                )
            response_status = response.status if response is not None else None
            if request.web_capture_stage is not None:
                from services.api.app.infrastructure.browser.web_redirect_identity import (
                    natural_web_redirect_chain,
                )

                web_redirect_chain = natural_web_redirect_chain(response)
            settled = navigation_settled
            if settled is None:
                h5_entry_wait_options = {}
                if (
                    str(request.session_id or "").startswith("h5:")
                    and request.web_capture_stage is None
                    and response_status not in {204, 205}
                    and (response_status is None or response_status < 400)
                ):
                    # 框架启动与慢线路不重载；在同一动作剩余预算内等待首个可采页面。
                    remaining_ms = max(1, timeout_ms - int(_elapsed_ms(capture_started_at)) - 3000)
                    h5_entry_wait_options["max_wait_ms"] = min(60000, remaining_ms)
                settled = await settle_waiter(
                    page=page,
                    network_inflight=network_inflight,
                    startup_policy=request.startup_policy,
                    diagnostics=page_settle_diagnostics,
                    **web_wait_options,
                    **h5_entry_wait_options,
                )
            page_settle_complete = settled is not False
            from services.api.app.infrastructure.browser.web_redirect_identity import (
                natural_web_redirect_chain,
            )

            # 导航审计同时保留请求、主文档响应与实际落地，不把缓存响应或离域落地伪装成目标已到达。
            action_trace.append({
                "action": "navigation_observation",
                "requested_url": request.url,
                "response_url": getattr(response, "url", None),
                "status_code": response_status,
                "redirect_chain": list(natural_web_redirect_chain(response)),
                "final_url": str(getattr(page, "url", "") or ""),
            })
            if goto_before_snapshot is not None:
                goto_after_url = str(getattr(page, "url", "") or "")
                goto_after_page_urls = _context_page_urls(context)
                goto_new_pages = [
                    item for item in _context_pages(context) if item not in goto_before_pages
                ]
                action_trace[-1]["navigation"] = {
                    "before_url": goto_before_url,
                    "after_url": goto_after_url,
                    "current_url_changed": goto_before_url != goto_after_url,
                    "new_page_count": len(goto_new_pages),
                }
                _complete_action_trace(
                    action_trace,
                    before_url=goto_before_url,
                    after_url=goto_after_url,
                    before_page_urls=goto_before_page_urls,
                    after_page_urls=goto_after_page_urls,
                    before_snapshot=goto_before_snapshot,
                    after_snapshot=await _collect_ui_snapshot(
                        page,
                        logical_surface_enabled=request.logical_surface_enabled,
                        logical_surface_shadow=request.logical_surface_shadow,
                        surface_session_id=request.session_id,
                    ),
                )
                action_trace[-1]["new_page_urls"] = [
                    str(item.url) for item in goto_new_pages
                ]
            timings_ms["navigation_and_stable_wait"] = _elapsed_ms(stage_started_at)
            if request.clear_obstructions:
                stage_started_at = time.perf_counter()
                obstruction_chain_metrics = await _clear_page_entry_obstructions(
                    page=page,
                    context=context,
                    request=request,
                    action_trace=action_trace,
                    obstruction_frames=obstruction_frames,
                    timeout_ms=timeout_ms,
                    timeout_error_type=timeout_error_type,
                )
                timings_ms["page_entry_obstruction_clear"] = _elapsed_ms(stage_started_at)
        elif request.mode == PageCaptureMode.OBSERVE:
            stage_started_at = time.perf_counter()
            if request.logical_surface_enabled and str(request.session_id or "").startswith("h5:"):
                observe_before_page_urls = _context_page_urls(context)
                observe_before_snapshot = await _collect_ui_snapshot(
                    page,
                    logical_surface_enabled=request.logical_surface_enabled,
                    logical_surface_shadow=request.logical_surface_shadow,
                    surface_session_id=request.session_id,
                )
            settled = await settle_waiter(
                page=page,
                network_inflight=network_inflight,
                startup_policy=request.startup_policy,
                diagnostics=page_settle_diagnostics,
                **web_wait_options,
            )
            page_settle_complete = settled is not False
            timings_ms["stable_wait"] = _elapsed_ms(stage_started_at)

        pre_trigger_url = page.url
        pre_trigger_page_urls = _context_page_urls(context)
        pre_trigger_ui_snapshot: dict[str, Any] | None = None
        if request.mode == PageCaptureMode.SINGLE_TRIGGER:
            if request._visual_navigation_target is None:
                pre_trigger_ui_snapshot = await _collect_ui_snapshot(
                    page,
                    logical_surface_enabled=request.logical_surface_enabled,
                    logical_surface_shadow=request.logical_surface_shadow,
                    surface_session_id=request.session_id,
                )
                if request._logical_target_binding is not None:
                    _validate_live_logical_target_binding(
                        snapshot=pre_trigger_ui_snapshot,
                        binding=request._logical_target_binding,
                    )
            if (
                not ad_landing_window
                and _normalize_trigger_action(request.trigger_action) == PageTriggerAction.CLICK
            ):
                external_anchor_href = await _click_target_external_anchor_href(
                    page=page,
                    selector=request.selector,
                )
                if external_anchor_href:
                    # 点击目标位于外站锚点内：自动开启落地页证据窗口，
                    # 新页签观察后关闭、同页外跳采证后回位，探索不出站。
                    ad_landing_window = True
                    request = _clone_page_capture_request(
                        request,
                        _force_legacy_ad_landing=True,
                    )
                    _validate_ad_capture_request(request)
                    if action_trace:
                        action_trace[-1]["external_anchor_href"] = external_anchor_href[:2048]
            if ad_landing_window:
                source_frame = await _capture_ad_navigation_frame(
                    page=page,
                    index=0,
                    role="source",
                )
                if source_frame is not None:
                    ad_landing_frames.append(source_frame)
            if request.clear_obstructions and request.capture_screenshot:
                obstruction_frames.append(
                    await _capture_obstruction_frame(
                        page=page,
                        index=0,
                        full_page=False,
                    )
                )
            is_scroll = (
                _normalize_trigger_action(request.trigger_action) == PageTriggerAction.SCROLL
            )
            if is_scroll and request.horizontal_scroll_max_segments is not None:
                stage_started_at = time.perf_counter()
                scroll_artifacts = await self._collect_horizontal_scroll_artifacts(
                    page=page,
                    context=context,
                    request=request,
                    action_trace=action_trace,
                    timeout_ms=timeout_ms,
                    timeout_error_type=timeout_error_type,
                    baseline_ui_snapshot=pre_trigger_ui_snapshot,
                )
                timings_ms["horizontal_scroll_coverage"] = _elapsed_ms(stage_started_at)
                transition_kind = "same_page_interaction"
            elif is_scroll and (
                request.web_capture_stage == "web_primary_content_evidence"
                or (request.web_capture_stage == "web_page_probe" and request.selector)
            ):
                # Web 主体/已绑定详情每个意图只滚动一次，随后采样稳定窗口和原始视口。
                # 普通/H5 滚动序列仍走原 coverage 分支，不升级其预制附件。
                stage_started_at = time.perf_counter()
                before_scroll = await _collect_scroll_state(page)
                await _run_single_trigger(
                    page=page,
                    context=context,
                    request=request,
                    action_trace=action_trace,
                    timeout_ms=timeout_ms,
                    timeout_error_type=timeout_error_type,
                )
                settled = await settle_waiter(
                    page=page,
                    network_inflight=network_inflight,
                    startup_policy=request.startup_policy,
                    diagnostics=page_settle_diagnostics,
                    **web_wait_options,
                )
                page_settle_complete = settled is not False
                _record_scroll_effect(
                    action_trace=action_trace,
                    before=before_scroll,
                    after=await _collect_scroll_state(page),
                )
                timings_ms["post_action_stable_wait"] = _elapsed_ms(stage_started_at)
                transition_kind = "same_page_interaction"
            elif is_scroll:
                stage_started_at = time.perf_counter()
                scroll_artifacts = await self._collect_scroll_coverage_artifacts(
                    page=page,
                    context=context,
                    request=request,
                    action_trace=action_trace,
                    timeout_ms=timeout_ms,
                    timeout_error_type=timeout_error_type,
                    baseline_ui_snapshot=pre_trigger_ui_snapshot,
                )
                timings_ms["scroll_coverage"] = _elapsed_ms(stage_started_at)
                transition_kind = "same_page_interaction"
            else:
                pages_before = _context_pages(context)
                observed_new_pages: list[Any] = []
                page_event_handler = _subscribe_new_pages(
                    context=context,
                    observed_pages=observed_new_pages,
                )
                transient_monitor_started = (
                    await _install_transient_message_monitor(page)
                    if request.capture_transient_messages
                    else False
                )
                transient_monitor_stopped = False
                navigation_outcome_recorded = False
                try:
                    direct_observation = await _run_single_trigger(
                        page=page,
                        context=context,
                        request=request,
                        action_trace=action_trace,
                        timeout_ms=timeout_ms,
                        timeout_error_type=timeout_error_type,
                    )
                    if request.capture_transient_messages:
                        if transient_monitor_started:
                            await _wait_for_transient_message(
                                page=page,
                                timeout_ms=min(
                                    timeout_ms,
                                    TRANSIENT_MESSAGE_OBSERVATION_WINDOW_MS,
                                ),
                            )
                        transient_messages = (
                            await _stop_transient_message_monitor(page)
                            if transient_monitor_started
                            else []
                        )
                        transient_monitor_stopped = transient_monitor_started
                        if action_trace:
                            action_trace[-1]["transient_message_observation"] = {
                                "schema_version": "transient-message-observation.v1",
                                "status": (
                                    "observed" if transient_monitor_started else "unavailable"
                                ),
                                "message_count": len(transient_messages),
                            }
                            if transient_messages:
                                action_trace[-1]["transient_messages"] = transient_messages
                        if transient_messages and request.capture_screenshot:
                            frame = await _capture_transient_message_frame(
                                page=page,
                                messages=transient_messages,
                            )
                            if frame is not None:
                                transient_message_frames.append(frame)
                    if request.clear_obstructions:
                        stage_started_at = time.perf_counter()
                        obstruction_chain_metrics = await _clear_remaining_obstructions(
                            page=page,
                            context=context,
                            request=request,
                            action_trace=action_trace,
                            obstruction_frames=obstruction_frames,
                            timeout_ms=timeout_ms,
                            timeout_error_type=timeout_error_type,
                        )
                        _require_h5_obstruction_chain_cleared(
                            request=request,
                            metrics=obstruction_chain_metrics,
                            action_trace=action_trace,
                        )
                        timings_ms["obstruction_clear"] = _elapsed_ms(stage_started_at)
                    ad_landing_observation = await _record_navigation_outcome(
                        page=page,
                        context=context,
                        before_url=pre_trigger_url,
                        pages_before=pages_before,
                        observed_new_pages=observed_new_pages,
                        action_trace=action_trace,
                        timeout_ms=timeout_ms,
                        capture_ad_landing_evidence=ad_landing_window,
                        isolated_landing=direct_observation,
                    )
                    if ad_landing_observation is not None:
                        ad_landing_window = True
                    navigation_outcome_recorded = True
                finally:
                    if transient_monitor_started and not transient_monitor_stopped:
                        try:
                            await _stop_transient_message_monitor(page)
                        except Exception:  # noqa: BLE001  # 清理失败不能跳过新页签回收
                            pass
                    if not navigation_outcome_recorded:
                        await _close_unclaimed_new_pages(
                            context=context,
                            pages_before=pages_before,
                            observed_new_pages=observed_new_pages,
                        )
                    _unsubscribe_new_pages(
                        context=context,
                        page_event_handler=page_event_handler,
                    )
                stage_started_at = time.perf_counter()
                post_action_wait_options = dict(web_wait_options)
                if (
                    str(request.session_id or "").startswith("h5:")
                    and request.web_capture_stage is None
                    and request.navigation_depth is not None
                    and _normalize_trigger_action(request.trigger_action) is PageTriggerAction.CLICK
                    and not request.clear_obstructions
                    and not ad_landing_window
                ):
                    # 业务点击后必须等真实路由与内容稳定，促销倒计时不是导航完成证明。
                    post_action_wait_options["allow_startup_obstruction_shortcut"] = False
                if request.navigation_depth is not None:
                    settled = await settle_waiter(
                        page=page,
                        network_inflight=network_inflight,
                        startup_policy=request.startup_policy,
                        diagnostics=page_settle_diagnostics,
                        **post_action_wait_options,
                    )
                else:
                    settled = await settle_waiter(
                        page=page,
                        network_inflight=network_inflight,
                        startup_policy=request.startup_policy,
                        max_wait_ms=PAGE_POST_TRIGGER_SETTLE_MAX_WAIT_MS,
                        poll_interval_ms=PAGE_POST_TRIGGER_SETTLE_POLL_INTERVAL_MS,
                        diagnostics=page_settle_diagnostics,
                        **post_action_wait_options,
                    )
                page_settle_complete = settled is not False
                timings_ms["post_action_stable_wait"] = _elapsed_ms(stage_started_at)
                if (
                    ad_landing_window
                    and ad_landing_observation is None
                    and str(getattr(page, "url", "") or "") != pre_trigger_url
                ):
                    ad_landing_observation = await _capture_ad_landing_observation(
                        page=page,
                        index=1,
                    )
                transition_kind = await _block_undeclared_click_navigation(
                    page=page,
                    request=request,
                    before_url=pre_trigger_url,
                    timeout_ms=timeout_ms,
                    ad_landing_window=ad_landing_window,
                    action_trace=action_trace,
                )
            logical_surface_transition: dict[str, object] | None = None
            if request._visual_navigation_target is not None and not is_scroll:
                verified_before = request._visual_navigation_frame_context.verified_before_frame
                if verified_before is None:
                    raise _visual_navigation_stale_error()
                if verified_before.source_image_sha256 != (
                    request._visual_navigation_target.source_image_sha256
                ):
                    request._visual_navigation_frame_context.clear()
                    raise _visual_navigation_stale_error()
                try:
                    pre_trigger_ui_snapshot = verified_before.ui_snapshot
                    pre_trigger_ui_snapshot = _attach_visual_navigation_evidence_to_snapshot(
                        pre_trigger_ui_snapshot,
                        page=page,
                        viewport_png=verified_before.viewport_png,
                        logical_surface_enabled=request.logical_surface_enabled,
                        logical_surface_shadow=request.logical_surface_shadow,
                        surface_session_id=request.session_id,
                    )
                    after_pngs = await _capture_visual_navigation_after_frames(page)
                    prepared_visual_viewport_png = after_pngs[1]
                    prepared_visual_snapshot = await _collect_visual_navigation_snapshot(
                        page,
                        viewport_png=prepared_visual_viewport_png,
                        logical_surface_enabled=request.logical_surface_enabled,
                        logical_surface_shadow=request.logical_surface_shadow,
                        surface_session_id=request.session_id,
                    )
                    post_trigger_ui_snapshot = prepared_visual_snapshot
                    logical_surface_transition = assess_visual_navigation_transition(
                        source_png=request._visual_navigation_target.source_png,
                        before_png=verified_before.viewport_png,
                        after_pngs=after_pngs,
                        capability=request._visual_navigation_target.capability,
                        before_surface=_logical_surface(pre_trigger_ui_snapshot),
                        after_surface=_logical_surface(post_trigger_ui_snapshot),
                    )
                finally:
                    request._visual_navigation_frame_context.clear()
                    verified_before = None
            else:
                post_trigger_ui_snapshot = await _collect_ui_snapshot(
                    page,
                    logical_surface_enabled=request.logical_surface_enabled,
                    logical_surface_shadow=request.logical_surface_shadow,
                    surface_session_id=request.session_id,
                )
            _complete_action_trace(
                action_trace,
                before_url=pre_trigger_url,
                after_url=page.url,
                before_page_urls=pre_trigger_page_urls,
                after_page_urls=_context_page_urls(context),
                before_snapshot=pre_trigger_ui_snapshot,
                after_snapshot=post_trigger_ui_snapshot,
                logical_surface_transition=logical_surface_transition,
                h5_session=str(request.session_id or "").startswith("h5:"),
            )
            if (
                not is_scroll
                and post_action_wait_options.get("allow_startup_obstruction_shortcut") is False
                and action_trace
            ):
                # 导航事件先于稳定等待记录；H5 决策必须与完成后的逻辑快照使用同一落点。
                navigation = action_trace[-1].get("navigation")
                if isinstance(navigation, dict):
                    navigation.update({
                        "after_url": page.url,
                        "current_url_changed": pre_trigger_url != page.url,
                    })
            if ad_landing_window:
                landing_frame = (
                    ad_landing_observation.pop("frame", None)
                    if isinstance(ad_landing_observation, dict)
                    else None
                )
                ad_landing_dom = (
                    ad_landing_observation.pop("dom", None)
                    if isinstance(ad_landing_observation, dict)
                    else None
                )
                ad_landing_screenshot = (
                    ad_landing_observation.pop("screenshot", None)
                    if isinstance(ad_landing_observation, dict)
                    else None
                )
                ad_landing_ui_snapshot = (
                    ad_landing_observation.pop("ui_snapshot", None)
                    if isinstance(ad_landing_observation, dict)
                    else None
                )
                ad_landing_har = (
                    ad_landing_observation.pop("har", None)
                    if isinstance(ad_landing_observation, dict)
                    else None
                )
                if isinstance(landing_frame, dict):
                    ad_landing_frames.append(landing_frame)
                _attach_ad_navigation_evidence(
                    action_trace=action_trace,
                    before_url=pre_trigger_url,
                    after_url=str(getattr(page, "url", "") or ""),
                    source_target=_action_target_fact(
                        snapshot=pre_trigger_ui_snapshot,
                        selector=request.selector,
                    ),
                    landing=ad_landing_observation,
                    frame_count=len(ad_landing_frames),
                )
            if action_trace and transition_kind is not None:
                action_trace[-1]["transition_kind"] = transition_kind

        full_page_audit: dict[str, Any] = {}
        artifact_started_at = time.perf_counter()
        if request.web_capture_stage is not None:
            stabilization_started_at = time.perf_counter()
            web_capture_stabilization = await _pause_web_capture_motion(page)
            stabilization_settle: dict[str, Any] = {}
            stabilized = await _wait_for_web_page_settle(
                page=page,
                network_inflight=network_inflight,
                startup_policy=request.startup_policy,
                max_wait_ms=2000,
                diagnostics=stabilization_settle,
                profile=request.web_readiness_profile,
            )
            page_settle_complete = stabilized is not False
            page_settle_diagnostics = stabilization_settle
            web_capture_stabilization["bounded_settle_complete"] = page_settle_complete
            timings_ms["web_capture_stabilization"] = _elapsed_ms(stabilization_started_at)
            if request.web_capture_stage == "web_full_page_supplemental":
                direct, probe_audit = await probe_direct_full_page_candidate(page)
                full_page_audit.update(probe_audit)
                if not direct:
                    raise WebFullPageStabilizationError(
                        WebFullPageStabilizationResult(
                            document_height=(
                                probe_audit["document_height"]
                                if type(probe_audit.get("document_height")) is int
                                else 0
                            ),
                            limit_exceeded=probe_audit.get("probe_reason") == "limit_exceeded",
                            reason_code=probe_audit["reason_code"],
                        )
                    )
            web_artifact_identity_before = str(getattr(page, "url", "") or "")
            web_capture_started_ms = time.perf_counter() * 1000
            web_structure_before = await _read_web_structure(page)
        baseline_temporal_capture = False
        if request.mode in {PageCaptureMode.GOTO, PageCaptureMode.OBSERVE}:
            if request.startup_policy != "direct_home":
                flutter_countdown = await _resolve_flutter_fullscreen_startup_countdown_target(page)
                baseline_temporal_capture = bool(
                    _contains_active_countdown(await _read_visible_text(page))
                    or flutter_countdown.get("match_count") == 1
                )
        capture_identity_before = await _read_capture_boundary_identity(
            page=page,
            context=context,
        )
        artifacts = await _collect_artifacts(
            page=page,
            request=request,
            include_screenshot=(
                request.mode != PageCaptureMode.TIMED_CAPTURE
                and not scroll_artifacts
                and not baseline_temporal_capture
            ),
            include_ui_snapshot=(
                request.mode != PageCaptureMode.TIMED_CAPTURE
                and not scroll_artifacts
                and not baseline_temporal_capture
            ),
            timings_ms=timings_ms,
            dom_collection_audit=dom_collection_audit,
            logical_surface_enabled=request.logical_surface_enabled,
            logical_surface_shadow=request.logical_surface_shadow,
            prepared_visual_snapshot=prepared_visual_snapshot,
            prepared_visual_viewport_png=prepared_visual_viewport_png,
            h5_visual_coordinate_mode=self._h5_visual_coordinate_mode,
        )
        artifacts.extend(scroll_artifacts)
        if obstruction_frames:
            artifacts.append(
                PageCaptureArtifact(
                    artifact_type="screenshot",
                    artifact_id=None,
                    content={
                        "sequence": True,
                        "purpose": "obstruction_clearance",
                        "frames": obstruction_frames,
                    },
                )
            )
        if transient_message_frames:
            artifacts.append(
                PageCaptureArtifact(
                    artifact_type="screenshot",
                    artifact_id=None,
                    content={
                        "sequence": True,
                        "purpose": "transient_message",
                        "frames": transient_message_frames,
                    },
                )
            )
        if len(ad_landing_frames) == 2:
            artifacts.append(
                PageCaptureArtifact(
                    artifact_type="screenshot",
                    artifact_id=None,
                    content={
                        "sequence": True,
                        "purpose": "ad_landing_consistency",
                        "frames": ad_landing_frames,
                    },
                )
            )
        if isinstance(ad_landing_dom, dict):
            artifacts.append(
                PageCaptureArtifact(
                    artifact_type="dom",
                    artifact_id=None,
                    content=ad_landing_dom,
                )
            )
        if isinstance(ad_landing_screenshot, dict):
            artifacts.append(
                PageCaptureArtifact(
                    artifact_type="screenshot",
                    artifact_id=None,
                    content=ad_landing_screenshot,
                )
            )
        if isinstance(ad_landing_ui_snapshot, dict):
            artifacts.append(
                PageCaptureArtifact(
                    artifact_type="ui_snapshot",
                    artifact_id=None,
                    content=ad_landing_ui_snapshot,
                )
            )
        if isinstance(ad_landing_har, dict):
            artifacts.append(
                PageCaptureArtifact(
                    artifact_type="har",
                    artifact_id=None,
                    content=ad_landing_har,
                )
            )
        if request.mode == PageCaptureMode.TIMED_CAPTURE or baseline_temporal_capture:
            timed_started_at = time.perf_counter()
            timed_request = (
                request
                if request.mode == PageCaptureMode.TIMED_CAPTURE
                else _clone_page_capture_request(
                    request,
                    frame_count=max(request.frame_count, 12),
                )
            )
            artifacts.extend(await _collect_timed_artifacts(page=page, request=timed_request))
            timings_ms["timed_capture"] = _elapsed_ms(timed_started_at)
        timings_ms["artifact_collection"] = _elapsed_ms(artifact_started_at)
        capture_identity_after = await _read_capture_boundary_identity(
            page=page,
            context=context,
        )
        capture_consistency = _assess_capture_boundary_consistency(
            before=capture_identity_before,
            after=capture_identity_after,
            temporal_capture=(
                request.mode == PageCaptureMode.TIMED_CAPTURE or baseline_temporal_capture
            ),
        )

        if request.web_capture_stage is not None:
            web_artifact_identity_after = str(getattr(page, "url", "") or "")
            web_readiness_snapshot = await _read_page_readiness(page)
            human_facts = [
                element.get("web_human_verification_present")
                for artifact in artifacts
                if artifact.artifact_type == "ui_snapshot" and isinstance(artifact.content, dict)
                for element in artifact.content.get("elements", [])
                if isinstance(element, dict)
                and type(element.get("web_human_verification_present")) is bool
            ]
            if human_facts and web_readiness_snapshot is not None:
                web_readiness_snapshot["human_verification_present"] = any(human_facts)
            web_structure_after = await _read_web_structure(page)
            if (
                web_readiness_snapshot is not None
                and type(web_structure_after.get("human_verification_present")) is bool
            ):
                web_readiness_snapshot["human_verification_present"] = web_structure_after[
                    "human_verification_present"
                ] or any(human_facts)
            web_capture_ended_ms = time.perf_counter() * 1000
            restoration = await _restore_web_capture_motion(page)
            web_capture_stabilization.update(restoration)

        if not page_settle_complete:
            (
                page_inspection_ready,
                page_settle_diagnostics,
            ) = await _assess_page_inspection_readiness(
                page=page,
                artifacts=artifacts,
                network_inflight=network_inflight,
                startup_policy=request.startup_policy,
                wait_diagnostics=page_settle_diagnostics,
            )
        if capture_consistency["status"] == "identity_drift":
            page_settle_complete = False
            page_inspection_ready = False
            page_settle_diagnostics = {
                **page_settle_diagnostics,
                "outcome": "capture_identity_drift",
                "blocking_reasons": list(
                    dict.fromkeys(
                        [
                            *page_settle_diagnostics.get("blocking_reasons", []),
                            "capture_identity_drift",
                        ]
                    )
                ),
            }

        video_publication = next(
            ({**artifact.content["publication"], "samples": artifact.content["samples"]}
             for artifact in artifacts
             if artifact.artifact_type == "checkpoint_observation"
             and isinstance(artifact.content, dict) and "publication" in artifact.content),
            None,
        )
        if video_publication is not None:
            from services.api.app.application.h5_video_publication import video_publication_is_ready

            if (video_publication.get("page_url") != str(page.url)
                    or not video_publication_is_ready(video_publication)):
                page_inspection_ready = False
                page_settle_diagnostics = {
                    **page_settle_diagnostics,
                    "blocking_reasons": list(dict.fromkeys([
                        *page_settle_diagnostics.get("blocking_reasons", []),
                        "video_loaded_frame_unverified",
                    ])),
                }

        if request.mode == PageCaptureMode.SINGLE_TRIGGER and _normalize_trigger_action(
            request.trigger_action
        ) in {PageTriggerAction.CLICK, PageTriggerAction.SUBMIT_PROBE}:
            # 某些 SPA 在首个静默窗口之后才提交路由。持久化前再次校验最终 URL，
            # 防止“Tool 已落证据、策略层才显示 denied”的审计矛盾。
            late_transition = await _block_undeclared_click_navigation(
                page=page,
                request=request,
                before_url=pre_trigger_url,
                timeout_ms=timeout_ms,
                ad_landing_window=ad_landing_window,
                action_trace=action_trace,
            )
            if action_trace and late_transition is not None:
                if not (
                    action_trace[-1].get("transition_kind") == "external_landing_observed"
                    and late_transition == "same_page_interaction"
                ):
                    action_trace[-1]["transition_kind"] = late_transition

        if (
            request.mode == PageCaptureMode.SINGLE_TRIGGER
            and request.logical_surface_enabled
            and str(request.session_id or "").startswith("h5:")
            and request._visual_navigation_target is None
            and not request.capture_ad_landing_evidence
            and pre_trigger_ui_snapshot is not None
            and action_trace
        ):
            final_snapshots = [
                artifact.content for artifact in artifacts
                if artifact.artifact_type == "ui_snapshot"
                and isinstance(artifact.content, dict)
                and isinstance(artifact.content.get("logical_surface"), Mapping)
                and not artifact.content.get("sequence")
            ]
            if len(final_snapshots) == 1 and capture_consistency["status"] != "identity_drift":
                # 后验必须使用正式采集的同一帧；中间轨迹保留作审计，不新增动作或采集。
                entry = action_trace[-1]
                old_transition = (entry.get("state_diff") or {}).get("logical_surface_transition")
                if isinstance(old_transition, Mapping):
                    entry["intermediate_logical_surface_transition"] = dict(old_transition)
                _complete_action_trace(
                    action_trace, before_url=pre_trigger_url, after_url=str(page.url),
                    before_page_urls=pre_trigger_page_urls,
                    after_page_urls=_context_page_urls(context),
                    before_snapshot=pre_trigger_ui_snapshot, after_snapshot=final_snapshots[0],
                    h5_session=True,
                )

        if observe_before_snapshot is not None:
            # 普通观察也必须绑定真实前后帧；同页可信不等于目标导航已到达。
            observed_snapshot = None
            for artifact in artifacts:
                if artifact.artifact_type != "ui_snapshot" or not isinstance(
                    artifact.content, dict
                ):
                    continue
                frames = artifact.content.get("frames")
                candidate = frames[-1] if isinstance(frames, list) and frames else artifact.content
                if isinstance(candidate, dict) and isinstance(
                    candidate.get("logical_surface"), Mapping
                ):
                    observed_snapshot = candidate
            if observed_snapshot is not None:
                observed_page_urls = _context_page_urls(context)
                action_trace.append({
                    "action": "observe",
                    "navigation": {
                        "before_url": observe_before_url,
                        "after_url": str(page.url),
                        "current_url_changed": observe_before_url != str(page.url),
                        "new_page_count": sum(
                            url not in observe_before_page_urls for url in observed_page_urls
                        ),
                    },
                })
                _complete_action_trace(
                    action_trace,
                    before_url=observe_before_url,
                    after_url=str(page.url),
                    before_page_urls=observe_before_page_urls,
                    after_page_urls=observed_page_urls,
                    before_snapshot=observe_before_snapshot,
                    after_snapshot=observed_snapshot,
                )

        current_console_logs = console_logs[console_start:]
        current_network_requests = network_requests[network_start:]
        artifacts.extend(
            _collect_runtime_artifacts(
                console_logs=current_console_logs,
                network_requests=current_network_requests,
                action_trace=action_trace,
            )
        )

        title = await page.title()
        post_action_snapshot = None
        if request.mode == PageCaptureMode.SINGLE_TRIGGER:
            post_action_snapshot = {
                "final_url": page.url,
                "title": title,
                "artifacts": [artifact.to_dict() for artifact in artifacts],
            }
        web_readiness_metrics = _web_readiness_metrics(
            web_capture_stage=request.web_capture_stage,
            identity_before=web_artifact_identity_before,
            identity_after=web_artifact_identity_after,
            page_settle_complete=page_settle_complete,
            page_inspection_ready=page_inspection_ready,
            readiness=web_readiness_snapshot,
            network_observable=network_inflight is not None,
            critical_network_pending=(
                _web_critical_network_pending(
                    network_inflight,
                    page_url=str(page.url),
                    profile=request.web_readiness_profile,
                )
                if request.web_capture_stage is not None
                else None
            ),
            profile=request.web_readiness_profile,
            settle_diagnostics=page_settle_diagnostics,
            capture_consistency=capture_consistency,
            capture_started_ms=web_capture_started_ms,
            capture_ended_ms=web_capture_ended_ms,
            structure_before=(
                web_structure_before.get("structure_digest")
                if not scroll_artifacts
                and prepared_visual_snapshot is None
                and prepared_visual_viewport_png is None
                else None
            ),
            structure_after=web_structure_after.get("structure_digest"),
        )
        navigation_document = None
        if request.mode == PageCaptureMode.GOTO and not ad_landing_window:
            # 首跳与后继 goto 都读取真实最终文档；状态码只参与错误响应判断，
            # 有缓存的 304 与超时后已提交的可读页面仍可形成有效目标证据。
            navigation_document = navigation_document_facts(
                final_url=str(page.url),
                status_code=response_status,
                readiness=await _read_page_readiness(page),
                title=title,
                visible_text=await _read_visible_text(
                    page, web_capture=request.web_capture_stage is not None,
                ),
            )
        return PageCaptureResult(
            execution_status="success",
            mode=request.mode.value,
            reachable=True,
            final_url=page.url,
            title=title,
            status_code=response_status,
            artifacts=artifacts,
            console_logs=current_console_logs,
            network_requests=current_network_requests,
            action_trace=action_trace,
            post_action_snapshot=post_action_snapshot,
            metrics={
                **({"web_full_page_stabilization": full_page_audit} if full_page_audit else {}),
                **(
                    {"web_natural_redirect_chain": list(web_redirect_chain)}
                    if web_redirect_chain
                    else {}
                ),
                "page_settle_complete": page_settle_complete,
                "page_inspection_ready": page_inspection_ready,
                "page_settle_diagnostics": page_settle_diagnostics,
                "capture_consistency": capture_consistency,
                **({"h5_video_publication": video_publication}
                   if video_publication is not None else {}),
                **(
                    {"navigation_document": navigation_document}
                    if navigation_document is not None
                    else {}
                ),
                **({"navigation_retry": navigation_retry} if navigation_retry is not None else {}),
                **(
                    {"web_readiness": web_readiness_metrics}
                    if web_readiness_metrics is not None
                    else {}
                ),
                **(
                    {"web_capture_stabilization": web_capture_stabilization}
                    if web_capture_stabilization is not None
                    else {}
                ),
                **(
                    {"obstruction_chain": obstruction_chain_metrics}
                    if obstruction_chain_metrics is not None
                    else {}
                ),
                "timings_ms": {
                    **timings_ms,
                    "total": _elapsed_ms(capture_started_at),
                },
                **({"dom_collection_audit": dom_collection_audit} if dom_collection_audit else {}),
                **_artifact_payload_metrics(artifacts),
            },
        )

    async def _collect_scroll_coverage_artifacts(
        self,
        *,
        page: Any,
        context: Any,
        request: PageCaptureRequest,
        action_trace: list[dict[str, Any]],
        timeout_ms: int,
        timeout_error_type: type[Exception],
        baseline_ui_snapshot: dict[str, Any],
    ) -> list[PageCaptureArtifact]:
        """在一次 Tool 调用内完成有界滚动，避免片段间重复识别与持久化。"""
        coverage_key = request.session_id or f"ephemeral:{id(page)}"
        self._coverage_states.pop(coverage_key, None)
        h5_scroll = str(request.session_id or "").startswith("h5:")
        capture_role = "h5_vertical_scroll_collection" if h5_scroll else "page_coverage"
        baseline_identity_before = await _read_capture_boundary_identity(
            page=page,
            context=context,
        )
        scroll_scope = (
            {"surface_session_id": request.session_id}
            if str(request.session_id or "").startswith("h5:") else {}
        )
        before = await _collect_scroll_state(page, **scroll_scope)
        snapshot_frames = [{"index": 0, **_compact_scroll_ui_snapshot(baseline_ui_snapshot)}]
        screenshot_frames: list[dict[str, Any]] = []
        if request.capture_screenshot:
            screenshot_frames.append(
                {
                    "index": 0,
                    "encoding": "base64",
                    "data": _encode_bytes(await page.screenshot(full_page=False)),
                }
            )
        baseline_identity_after = await _read_capture_boundary_identity(
            page=page,
            context=context,
        )
        before["capture_consistency"] = _assess_capture_boundary_consistency(
            before=baseline_identity_before,
            after=baseline_identity_after,
            temporal_capture=False,
        )

        max_segments = (
            request.scroll_max_segments
            if request.scroll_max_segments is not None
            else get_settings().quality_optimization.scroll_max_segments
        )
        if before.get("at_bottom") is True or max_segments <= 1:
            termination_reason = (
                "bottom_reached" if before.get("at_bottom") is True else "segment_budget_exhausted"
            )
            coverage = _single_segment_coverage(
                state=before,
                termination_reason=termination_reason,
            )
        else:
            coverage: dict[str, Any] = {}
            while True:
                await _run_single_trigger(
                    page=page,
                    context=context,
                    request=request,
                    action_trace=action_trace,
                    timeout_ms=timeout_ms,
                    timeout_error_type=timeout_error_type,
                )
                await _wait_for_page_settle(
                    page=page,
                    startup_policy=request.startup_policy,
                    max_wait_ms=min(PAGE_POST_TRIGGER_SETTLE_MAX_WAIT_MS, 1500),
                    poll_interval_ms=PAGE_POST_TRIGGER_SETTLE_POLL_INTERVAL_MS,
                    stable_samples=1,
                )
                frame_identity_before = await _read_capture_boundary_identity(
                    page=page,
                    context=context,
                )
                after = await _collect_scroll_state(page, **scroll_scope)
                _record_scroll_effect(
                    action_trace=action_trace,
                    before=before,
                    after=after,
                )
                scroll_progressed = _vertical_scroll_progressed(
                    before=before,
                    after=after,
                )
                if not request.split_scroll_screenshots or scroll_progressed:
                    frame_index = len(snapshot_frames)
                    snapshot_frames.append(
                        {
                            "index": frame_index,
                            **_compact_scroll_ui_snapshot(
                                await _collect_ui_snapshot(
                                    page,
                                    logical_surface_enabled=request.logical_surface_enabled,
                                    logical_surface_shadow=request.logical_surface_shadow,
                                    surface_session_id=request.session_id,
                                )
                            ),
                        }
                    )
                    if request.capture_screenshot:
                        screenshot_frames.append(
                            {
                                "index": frame_index,
                                "encoding": "base64",
                                "data": _encode_bytes(
                                    await page.screenshot(full_page=False)
                                ),
                            }
                        )
                frame_identity_after = await _read_capture_boundary_identity(
                    page=page,
                    context=context,
                )
                after["capture_consistency"] = _assess_capture_boundary_consistency(
                    before=frame_identity_before,
                    after=frame_identity_after,
                    temporal_capture=False,
                )
                coverage = self._update_page_coverage(
                    coverage_key=coverage_key,
                    before=before,
                    after=after,
                    max_segments=max_segments,
                )
                if request.split_scroll_screenshots and not scroll_progressed:
                    if coverage.get("termination_reason") not in {
                        "bottom_reached",
                        "capture_identity_drift",
                    }:
                        coverage.update(
                            {
                                "termination_reason": "scroll_no_effect",
                                "collection_complete": True,
                                "coverage_complete": False,
                                "coverage_status": "bounded_incomplete",
                                "coverage_gap_reason": "scroll_no_effect",
                            }
                        )
                    break
                if coverage.get("collection_complete") is True:
                    break
                before = after

        bounded_snapshots = _bound_ui_snapshot_sequence(
            {
                "schema_version": "ui-snapshot-sequence.v1",
                "sequence": True,
                "capture_role": capture_role,
                "frames": snapshot_frames,
            }
        )
        available_ui_frames = {
            int(frame.get("index"))
            for frame in bounded_snapshots.get("frames", [])
            if isinstance(frame, dict) and isinstance(frame.get("index"), int)
        }
        available_screenshot_frames = {
            int(frame["index"])
            for frame in screenshot_frames
            if isinstance(frame.get("index"), int)
        }
        for segment in coverage.get("segments", []):
            if not isinstance(segment, dict):
                continue
            index = int(segment.get("index") or 0)
            segment["ui_snapshot_frame_index"] = index if index in available_ui_frames else None
            segment["screenshot_frame_index"] = (
                index if index in available_screenshot_frames else None
            )
        bounded_snapshot_frames = bounded_snapshots.get("frames")
        if not isinstance(bounded_snapshot_frames, list):
            bounded_snapshot_frames = []
        _mark_terminal_scroll_frames(
            coverage=coverage,
            snapshot_frames=bounded_snapshot_frames,
            screenshot_frames=screenshot_frames,
        )

        artifacts: list[PageCaptureArtifact] = []
        if screenshot_frames:
            if request.split_scroll_screenshots:
                artifacts.extend(
                    PageCaptureArtifact(
                        artifact_type="screenshot",
                        artifact_id=None,
                        content={
                            "encoding": frame["encoding"],
                            "data": frame["data"],
                            **({"capture_role": capture_role, "frame_index": frame["index"]}
                               if h5_scroll else {}),
                        },
                    )
                    for frame in screenshot_frames
                )
            else:
                artifacts.append(
                    PageCaptureArtifact(
                        artifact_type="screenshot",
                        artifact_id=None,
                        content={
                            "sequence": True,
                            "capture_role": capture_role,
                            "frames": screenshot_frames,
                        },
                    )
                )
        artifacts.extend(
            [
                PageCaptureArtifact(
                    artifact_type="ui_snapshot",
                    artifact_id=None,
                    content=bounded_snapshots,
                ),
                PageCaptureArtifact(
                    artifact_type="page_coverage",
                    artifact_id=None,
                    content={**coverage, "capture_role": capture_role} if h5_scroll else coverage,
                ),
            ]
        )
        return artifacts

    async def _collect_horizontal_scroll_artifacts(
        self,
        *,
        page: Any,
        context: Any,
        request: PageCaptureRequest,
        action_trace: list[dict[str, Any]],
        timeout_ms: int,
        timeout_error_type: type[Exception],
        baseline_ui_snapshot: dict[str, Any],
    ) -> list[PageCaptureArtifact]:
        """有界遍历 H5 支付页横向会员套餐，并逐屏保留证据。"""
        assert request.selector is not None
        assert request.horizontal_scroll_max_segments is not None

        before = await _collect_horizontal_scroll_state(
            page,
            selector=request.selector,
        )
        states = [before]
        snapshots = [
            {"index": 0, **_compact_scroll_ui_snapshot(baseline_ui_snapshot)}
        ]
        screenshots: list[PageCaptureArtifact] = []

        async def capture_frame(index: int) -> None:
            if not request.capture_screenshot:
                return
            screenshots.append(
                PageCaptureArtifact(
                    artifact_type="screenshot",
                    artifact_id=None,
                    content={
                        "encoding": "base64",
                        "data": _encode_bytes(await page.screenshot(full_page=False)),
                        "capture_role": "h5_payment_horizontal_collection",
                        "frame_index": index,
                    },
                )
            )

        await capture_frame(0)
        termination_reason = "end_reached" if before.get("at_end") is True else None
        while termination_reason is None and len(states) < request.horizontal_scroll_max_segments:
            await _run_single_trigger(
                page=page,
                context=context,
                request=request,
                action_trace=action_trace,
                timeout_ms=timeout_ms,
                timeout_error_type=timeout_error_type,
            )
            await _wait_for_page_settle(
                page=page,
                startup_policy=request.startup_policy,
                max_wait_ms=min(PAGE_POST_TRIGGER_SETTLE_MAX_WAIT_MS, 1500),
                poll_interval_ms=PAGE_POST_TRIGGER_SETTLE_POLL_INTERVAL_MS,
                stable_samples=1,
            )
            after = await _collect_horizontal_scroll_state(
                page,
                selector=request.selector,
            )
            _record_horizontal_scroll_effect(
                action_trace=action_trace,
                before=before,
                after=after,
            )
            if not _horizontal_scroll_progressed(before=before, after=after):
                termination_reason = "scroll_no_effect"
                break
            index = len(states)
            states.append(after)
            snapshots.append(
                {
                    "index": index,
                    **_compact_scroll_ui_snapshot(
                        await _collect_ui_snapshot(
                            page,
                            logical_surface_enabled=request.logical_surface_enabled,
                            logical_surface_shadow=request.logical_surface_shadow,
                            surface_session_id=request.session_id,
                        )
                    ),
                }
            )
            await capture_frame(index)
            if after.get("at_end") is True:
                termination_reason = "end_reached"
            before = after

        if termination_reason is None:
            termination_reason = "segment_budget_exhausted"

        segments = [
            {
                "index": index,
                "scroll_left_before": (
                    states[index - 1].get("scroll_left") if index else state.get("scroll_left")
                ),
                "scroll_left_after": state.get("scroll_left"),
                "scroll_width": state.get("scroll_width"),
                "viewport_width": state.get("client_width"),
                "scroll_mode": state.get("scroll_mode"),
                "swiper_active_index_before": (
                    states[index - 1].get("swiper_active_index")
                    if index
                    else state.get("swiper_active_index")
                ),
                "swiper_active_index_after": state.get("swiper_active_index"),
                "swiper_slide_count": state.get("swiper_slide_count"),
                "swiper_is_end": state.get("swiper_is_end"),
                "screenshot_frame_index": index,
                "ui_snapshot_frame_index": index,
            }
            for index, state in enumerate(states)
        ]
        coverage_complete = termination_reason == "end_reached"
        bounded_snapshots = _bound_ui_snapshot_sequence(
            {
                "schema_version": "ui-snapshot-sequence.v1",
                "sequence": True,
                "capture_role": "h5_payment_horizontal_collection",
                "frames": snapshots,
            }
        )
        return [
            *screenshots,
            PageCaptureArtifact(
                artifact_type="ui_snapshot",
                artifact_id=None,
                content=bounded_snapshots,
            ),
            PageCaptureArtifact(
                artifact_type="page_coverage",
                artifact_id=None,
                content={
                    "schema_version": "page-coverage.v2",
                    "capture_role": "h5_payment_horizontal_collection",
                    "axis": "horizontal",
                    "url": states[-1].get("url"),
                    "container_selector": states[-1].get("container_selector"),
                    "segments": segments,
                    "termination_reason": termination_reason,
                    "collection_complete": True,
                    "end_reached": states[-1].get("at_end") is True,
                    "coverage_complete": coverage_complete,
                    "coverage_status": (
                        "complete" if coverage_complete else "bounded_incomplete"
                    ),
                    "coverage_gap_reason": (
                        None if coverage_complete else termination_reason
                    ),
                },
            ),
        ]

    def _update_page_coverage(
        self,
        *,
        coverage_key: str,
        before: dict[str, Any],
        after: dict[str, Any],
        max_segments: int | None = None,
    ) -> dict[str, Any]:
        """聚合同一路由滚动片段，并计算有界终止条件。"""
        route_key = f"{after.get('url')}|{after.get('container_selector')}"
        state = self._coverage_states.get(coverage_key)
        if state is None or state.route_key != route_key:
            state = _CoverageState(route_key=route_key)
            self._coverage_states[coverage_key] = state
            baseline_fingerprints = {
                str(item) for item in before.get("content_fingerprints", []) if str(item)
            }
            state.content_fingerprints.update(baseline_fingerprints)
            state.previous_scroll_height = float(before.get("scroll_height") or 0)
            state.previous_visible_fingerprints = [
                str(item) for item in before.get("content_fingerprints", []) if str(item)
            ]
            baseline_consistency = _compact_capture_consistency(before.get("capture_consistency"))
            if baseline_consistency.get("status") == "identity_drift":
                state.continuity_gap_count += 1
            state.segments.append(
                {
                    "index": 0,
                    "scroll_top_before": before.get("scroll_top"),
                    "scroll_top_after": before.get("scroll_top"),
                    "scroll_height": before.get("scroll_height"),
                    "viewport_height": before.get("client_height"),
                    "content_fingerprint": before.get("content_fingerprint"),
                    "new_element_count": len(baseline_fingerprints),
                    "continuity_segment_index": 0,
                    "continuity_status": "baseline",
                    "capture_consistency": baseline_consistency,
                }
            )
        ordered_fingerprints = [
            str(item) for item in after.get("content_fingerprints", []) if str(item)
        ]
        fingerprints = set(ordered_fingerprints)
        new_fingerprints = fingerprints.difference(state.content_fingerprints)
        state.content_fingerprints.update(fingerprints)
        state.consecutive_no_new = state.consecutive_no_new + 1 if not new_fingerprints else 0
        scroll_height = float(after.get("scroll_height") or 0)
        state.stable_height_samples = (
            state.stable_height_samples + 1 if state.previous_scroll_height == scroll_height else 0
        )
        state.previous_scroll_height = scroll_height
        continuity = _assess_scroll_segment_continuity(
            before=before,
            after=after,
            previous_visible_fingerprints=state.previous_visible_fingerprints,
            current_visible_fingerprints=ordered_fingerprints,
        )
        if continuity["starts_new_segment"] is True:
            state.continuity_segment_index += 1
            state.continuity_gap_count += 1
        frame_consistency = _compact_capture_consistency(after.get("capture_consistency"))
        if (
            frame_consistency.get("status") == "identity_drift"
            and continuity["starts_new_segment"] is not True
        ):
            state.continuity_segment_index += 1
            state.continuity_gap_count += 1
        state.previous_visible_fingerprints = ordered_fingerprints
        segment = {
            "index": len(state.segments),
            "scroll_top_before": before.get("scroll_top"),
            "scroll_top_after": after.get("scroll_top"),
            "scroll_height": after.get("scroll_height"),
            "viewport_height": after.get("client_height"),
            "content_fingerprint": after.get("content_fingerprint"),
            "new_element_count": len(new_fingerprints),
            "continuity_segment_index": state.continuity_segment_index,
            "continuity_status": continuity["status"],
            "overlap_item_count": continuity["overlap_item_count"],
            "scroll_progressed": continuity["scroll_progressed"],
            "capture_consistency": frame_consistency,
        }
        state.segments.append(segment)
        max_segments = (
            max_segments
            if max_segments is not None
            else get_settings().quality_optimization.scroll_max_segments
        )
        at_bottom = bool(after.get("at_bottom"))
        termination_reason = None
        if frame_consistency.get("status") == "identity_drift":
            termination_reason = "capture_identity_drift"
        elif at_bottom:
            termination_reason = "bottom_reached"
        elif state.consecutive_no_new >= 2:
            termination_reason = "two_segments_without_new_content"
        elif state.stable_height_samples >= 2:
            termination_reason = "document_height_stable"
        elif len(state.segments) >= max_segments:
            termination_reason = "segment_budget_exhausted"
        collection_complete = termination_reason is not None
        coverage_complete = at_bottom and state.continuity_gap_count == 0
        return {
            "schema_version": "page-coverage.v2",
            "url": after.get("url"),
            "container_selector": after.get("container_selector"),
            "overlap_ratio": PAGE_SCROLL_OVERLAP_RATIO,
            "segments": list(state.segments),
            "covered_content_fingerprint_count": len(state.content_fingerprints),
            "termination_reason": termination_reason,
            "collection_complete": collection_complete,
            "bottom_reached": at_bottom,
            "coverage_complete": coverage_complete,
            "coverage_status": _page_coverage_status(
                collection_complete=collection_complete,
                coverage_complete=coverage_complete,
            ),
            "continuity_segment_count": state.continuity_segment_index + 1,
            "continuity_gap_count": state.continuity_gap_count,
            "coverage_gap_reason": _page_coverage_gap_reason(
                termination_reason=termination_reason,
                at_bottom=at_bottom,
                continuity_gap_count=state.continuity_gap_count,
            ),
        }


async def _dispose_playwright_session(
    session: _PlaywrightSession, *, require_stop_proof: bool = False,
) -> dict[str, Any] | None:
    """尽最大努力释放已完成或已崩溃的 Playwright 会话。"""
    if require_stop_proof:
        if session.stop_guard is None:
            raise H5PhysicalOperationNotDrained("H5 浏览器缺少原实例停止证明")
        proof = await session.stop_guard.stop_and_prove()
        if session.cli_session is not None:
            session.cli_session.cleanup()
        return proof
    if session.cli_session is not None:
        try:
            await session.cli_session.close()
        except Exception:
            logger.warning("CLI 会话分离失败，继续关闭所属浏览器")
    if not require_stop_proof:
        try:
            await session.browser.close()
        except Exception:  # noqa: BLE001 - Web保留已有尽力清理，不代替H5停止证明
            pass
    try:
        await session.playwright.stop()
    except Exception:  # noqa: BLE001 - Playwright 子进程可能已随浏览器退出
        pass
    if session.cli_session is not None:
        session.cli_session.cleanup()


def _is_terminal_browser_session_error(exc: Exception) -> bool:
    """识别页面、上下文或浏览器已经不可继续复用的 Playwright 错误。"""
    message = str(exc).strip().casefold()
    return any(
        marker in message
        for marker in (
            "target crashed",
            "page crashed",
            "target page, context or browser has been closed",
            "browser has been closed",
            "context has been closed",
        )
    )


def _compact_scroll_ui_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """滚动片段只保留可见语义元素和结构摘要，控制八段聚合载荷。"""
    elements = snapshot.get("elements")
    source_element_count = len(elements or [])
    visible_elements = [
        item
        for item in elements or []
        if isinstance(item, dict) and item.get("visible") is not False
    ][:80]
    truncated = len(visible_elements) < source_element_count
    return {
        "schema_version": snapshot.get("schema_version", "ui-snapshot.v2"),
        "url": snapshot.get("url"),
        "viewport": snapshot.get("viewport"),
        "document": snapshot.get("document"),
        "structure_signature": snapshot.get("structure_signature"),
        **({"h5_scroll_regions": snapshot["h5_scroll_regions"][:16]}
           if isinstance(snapshot.get("h5_scroll_regions"), list) else {}),
        "elements": visible_elements,
        "truncated": truncated,
        "truncated_reason": (
            "scroll_segment_semantic_compaction" if truncated else snapshot.get("truncated_reason")
        ),
        "derivation": {
            "kind": "visible_semantic_projection",
            "source_element_count": source_element_count,
            "retained_element_count": len(visible_elements),
            "element_limit": 80,
        },
    }


def _single_segment_coverage(*, state: dict[str, Any], termination_reason: str) -> dict[str, Any]:
    fingerprints = {str(item) for item in state.get("content_fingerprints", []) if str(item)}
    at_bottom = bool(state.get("at_bottom"))
    capture_consistency = _compact_capture_consistency(state.get("capture_consistency"))
    identity_drift = capture_consistency.get("status") == "identity_drift"
    if identity_drift:
        termination_reason = "capture_identity_drift"
    coverage_complete = at_bottom and not identity_drift
    return {
        "schema_version": "page-coverage.v2",
        "url": state.get("url"),
        "container_selector": state.get("container_selector"),
        "overlap_ratio": PAGE_SCROLL_OVERLAP_RATIO,
        "segments": [
            {
                "index": 0,
                "scroll_top_before": state.get("scroll_top"),
                "scroll_top_after": state.get("scroll_top"),
                "scroll_height": state.get("scroll_height"),
                "viewport_height": state.get("client_height"),
                "content_fingerprint": state.get("content_fingerprint"),
                "new_element_count": len(fingerprints),
                "continuity_segment_index": 0,
                "continuity_status": "baseline",
                "capture_consistency": capture_consistency,
            }
        ],
        "covered_content_fingerprint_count": len(fingerprints),
        "termination_reason": termination_reason,
        "collection_complete": True,
        "bottom_reached": at_bottom,
        "coverage_complete": coverage_complete,
        "coverage_status": "complete" if coverage_complete else "bounded_incomplete",
        "continuity_segment_count": 1,
        "continuity_gap_count": int(identity_drift),
        "coverage_gap_reason": None if coverage_complete else termination_reason,
    }


def _mark_terminal_scroll_frames(
    *,
    coverage: dict[str, Any],
    snapshot_frames: list[Any],
    screenshot_frames: list[dict[str, Any]],
) -> None:
    """区分真正触底终帧与有界停止时的最后观察帧。"""
    if coverage.get("collection_complete") is not True:
        return
    segments = coverage.get("segments")
    if not isinstance(segments, list):
        return
    last_segment = next((item for item in reversed(segments) if isinstance(item, dict)), None)
    if last_segment is None:
        return

    terminal_ui_index = last_segment.get("ui_snapshot_frame_index")
    terminal_screenshot_index = last_segment.get("screenshot_frame_index")
    coverage_complete = coverage.get("coverage_complete") is True
    capture_role = "scroll_terminal" if coverage_complete else "scroll_last_observed"
    coverage["terminal_capture_role"] = capture_role
    coverage["terminal_ui_snapshot_frame_index"] = terminal_ui_index
    coverage["terminal_screenshot_frame_index"] = terminal_screenshot_index
    coverage["terminal_scroll_state"] = {
        key: last_segment.get(key)
        for key in ("scroll_top_after", "scroll_height", "viewport_height")
        if key in last_segment
    }

    for frame in snapshot_frames:
        if isinstance(frame, dict) and frame.get("index") == terminal_ui_index:
            frame["capture_role"] = capture_role
            frame["collection_terminal"] = True
            frame["terminal"] = coverage_complete
    for frame in screenshot_frames:
        if frame.get("index") == terminal_screenshot_index:
            frame["capture_role"] = capture_role
            frame["collection_terminal"] = True
            frame["terminal"] = coverage_complete


def _assess_scroll_segment_continuity(
    *,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    previous_visible_fingerprints: list[str],
    current_visible_fingerprints: list[str],
) -> dict[str, Any]:
    """验证相邻滚动帧有序重叠；无法证明连续时切成新的证据段。"""

    before_top = float(before.get("scroll_top") or 0)
    after_top = float(after.get("scroll_top") or 0)
    scroll_progressed = after_top > before_top
    viewport_height = float(after.get("client_height") or before.get("client_height") or 0)
    maximum_continuous_delta = viewport_height * (1 - PAGE_SCROLL_OVERLAP_RATIO)
    current_set = set(current_visible_fingerprints)
    previous_set = set(previous_visible_fingerprints)
    previous_overlap = [item for item in previous_visible_fingerprints if item in current_set]
    current_overlap = [item for item in current_visible_fingerprints if item in previous_set]
    overlap_item_count = len(previous_overlap)

    if not scroll_progressed:
        status = "no_scroll_progress"
    elif viewport_height <= 0:
        status = "viewport_height_unavailable"
    elif after_top - before_top > maximum_continuous_delta + 2:
        status = "scroll_gap_detected"
    elif not previous_visible_fingerprints or not current_visible_fingerprints:
        status = "semantic_content_unavailable"
    elif not previous_overlap:
        status = "overlap_unverified"
    elif previous_overlap != current_overlap:
        status = "content_order_changed"
    else:
        status = "continuous"
    return {
        "status": status,
        "scroll_progressed": scroll_progressed,
        "overlap_item_count": overlap_item_count,
        "starts_new_segment": status
        in {
            "no_scroll_progress",
            "viewport_height_unavailable",
            "scroll_gap_detected",
            "semantic_content_unavailable",
            "overlap_unverified",
            "content_order_changed",
        },
    }


def _page_coverage_status(*, collection_complete: bool, coverage_complete: bool) -> str:
    if coverage_complete:
        return "complete"
    if collection_complete:
        return "bounded_incomplete"
    return "collecting"


def _page_coverage_gap_reason(
    *,
    termination_reason: str | None,
    at_bottom: bool,
    continuity_gap_count: int,
) -> str | None:
    if termination_reason == "capture_identity_drift":
        return termination_reason
    if at_bottom and continuity_gap_count:
        return "segment_continuity_unverified"
    if termination_reason is not None and not at_bottom:
        return termination_reason
    return None


def _compact_capture_consistency(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {
            "schema_version": "capture-consistency.v1",
            "status": "unavailable",
            "changed_signals": [],
            "unavailable_signals": ["capture_boundary"],
        }
    return {
        "schema_version": value.get("schema_version", "capture-consistency.v1"),
        "status": value.get("status"),
        "changed_signals": list(value.get("changed_signals") or []),
        "unavailable_signals": list(value.get("unavailable_signals") or []),
    }


async def _wait_for_page_settle(
    *,
    page: Any,
    network_inflight: dict[int, dict[str, str]] | None = None,
    max_wait_ms: int = PAGE_SETTLE_MAX_WAIT_MS,
    poll_interval_ms: int = PAGE_SETTLE_POLL_INTERVAL_MS,
    stable_samples: int = PAGE_SETTLE_STABLE_SAMPLES,
    startup_policy: str = "h5",
    diagnostics: dict[str, Any] | None = None,
    allow_startup_obstruction_shortcut: bool = True,
    allow_dynamic_business: bool = False,
) -> bool:
    """等待可见文本稳定，同时避免把线路加载页误当成真实页面。

    纯线路加载文案是入口瞬态，重复出现时仍继续等待；明确启动广告文案或
    现场关闭入口可提前采集，促销倒计时本身不取得快速采集资格。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_wait_ms / 1000
    monitor_revision = await _install_page_state_monitor(page)
    previous_sample: tuple[str, str] | None = None
    stable = 0
    sample_count = 0
    while loop.time() < deadline:
        sample_count += 1
        try:
            raw_text = await page.inner_text("body", timeout=max(poll_interval_ms, 1000))
            current = _normalize_visible_text(raw_text)
        except Exception:  # noqa: BLE001  # 跳转销毁执行上下文等瞬态错误，视为未稳定
            current = None

        if (
            allow_startup_obstruction_shortcut
            and startup_policy != "direct_home"
            and current
            and (
                _looks_like_startup_ad_text(current)
                or await _has_visible_startup_ad_dismissal(page)
            )
        ):
            if diagnostics is not None:
                diagnostics.update(
                    {
                        "outcome": "startup_obstruction_ready",
                        "blocking_reasons": [],
                        "dynamic_reasons": ["startup_obstruction_visible"],
                        "sample_count": sample_count,
                        "stable_sample_count": stable,
                        "page_url_sha256": _page_url_sha256(page),
                        "readiness": {},
                    }
                )
            return True

        readiness = await _read_page_readiness(page)
        critical_network_pending = sum(
            1
            for item in (network_inflight or {}).values()
            if item.get("resource_type") in PAGE_SETTLE_CRITICAL_RESOURCE_TYPES
        )
        business_ready = readiness is not None and (
            readiness.get("document_ready_state", "complete") in {"interactive", "complete"}
            and readiness.get("core_present") is True
            and readiness.get("necessary_media_ready") is True
            and readiness.get("visible_loader") is not True
            and readiness.get("transition_active") is not True
            and readiness.get("flutter_bootstrap_pending") is not True
            and float(readiness.get("dom_quiet_ms") or 0) >= PAGE_DOM_QUIET_WINDOW_MS
            and critical_network_pending == 0
        )
        dynamic_business_ready = bool(
            allow_dynamic_business and readiness is not None
            and readiness.get("document_ready_state") in {"interactive", "complete"}
            and readiness.get("core_present") is True
            and readiness.get("necessary_media_ready") is True
            and readiness.get("blocking_loader", True) is False
            and readiness.get("blocking_transition", True) is False
            and readiness.get("loading_business_content_missing") is not True
            and readiness.get("flutter_bootstrap_pending") is not True
            and int(readiness.get("business_text_characters") or 0) >= 40
            and int(readiness.get("business_element_count") or 0) >= 5
        )
        current_url = str(getattr(page, "url", "") or "")
        current_sample = (current_url, current) if current is not None else None
        is_line_loading = bool(
            startup_policy != "direct_home" and current and _looks_like_line_loading_text(current)
        )
        if (
            current_sample is not None
            and current_sample == previous_sample
            and not is_line_loading
            and (business_ready or dynamic_business_ready)
        ):
            stable += 1
            if stable >= stable_samples:
                if diagnostics is not None:
                    diagnostics.update(
                        {
                            "outcome": "settled" if business_ready else "business_content_ready",
                            "blocking_reasons": [],
                            "dynamic_reasons": ([] if business_ready else ["background_activity"]),
                            "sample_count": sample_count,
                            "stable_sample_count": stable,
                            "critical_network_pending": critical_network_pending,
                            "page_url_sha256": _page_url_sha256(page),
                            "readiness": dict(readiness),
                            "satisfied_signals": [
                                "page_url_stable",
                                "core_content_present",
                                "necessary_media_ready",
                                "flutter_bootstrap_not_pending",
                                "stable_sample_quorum",
                                *(["loader_absent", "transition_absent", "dom_quiet",
                                   "critical_network_idle"] if business_ready
                                  else ["blocking_loader_absent", "blocking_transition_absent"]),
                            ],
                        }
                    )
                return True
        else:
            stable = 0
        previous_sample = current_sample
        remaining_ms = max(int((deadline - loop.time()) * 1000), 0)
        monitor_revision = await _wait_for_page_state_change(
            page=page,
            previous_revision=monitor_revision,
            timeout_ms=min(poll_interval_ms, remaining_ms),
        )
    if diagnostics is not None:
        diagnostics.update(
            {
                "outcome": "settle_timeout",
                "sample_count": sample_count,
                "stable_sample_count": stable,
                "page_url_sha256": _page_url_sha256(page),
            }
        )
    return False


def _web_critical_network_pending(
    inflight: Mapping[int, Mapping[str, str]] | None,
    *,
    page_url: str,
    profile: WebReadinessProfile,
) -> int | None:
    """按可信 profile 选择关键请求，缺失观测不能归零。"""

    if inflight is None:
        return None
    origin = urlsplit(page_url)
    count = 0
    for item in inflight.values():
        if item.get("resource_type") not in profile.critical_resource_types:
            continue
        url = str(item.get("url") or "")
        parsed = urlsplit(url)
        if profile.same_origin_only and (parsed.scheme, parsed.netloc) != (
            origin.scheme,
            origin.netloc,
        ):
            continue
        if profile.critical_url_prefixes and not any(
            url.startswith(x) for x in profile.critical_url_prefixes
        ):
            continue
        count += 1
    return count


async def _pause_web_capture_motion(page: Any) -> dict[str, Any]:
    """在正式 Web 制品窗口内暂停轮播与动画，仍保留完整结构一致性校验。"""

    from services.api.app.infrastructure.browser.web_carousel_motion import pause_web_carousels

    carousel_facts = await pause_web_carousels(page)

    facts = await page.evaluate(
        """
        () => {
          const key = Symbol.for("alioth.web.capture.stabilization.v1");
          const existing = window[key];
          if (existing) {
            return {
              status: "already_applied",
              swiper_autoplay_paused: existing.swipers.length,
              css_animations_paused: existing.animations.length,
            };
          }
          const animations = [];
          try {
            for (const animation of document.getAnimations({subtree: true})) {
              if (animation.playState === "running") {
                animation.pause();
                animations.push(animation);
              }
            }
          } catch (_) {}
          const swipers = [];
          const instances = new Set();
          for (const element of document.querySelectorAll(
            ".swiper,.swiper-container,[class*='swiper-']"
          )) {
            if (element && element.swiper) instances.add(element.swiper);
          }
          for (const swiper of instances) {
            const autoplay = swiper && swiper.autoplay;
            if (autoplay && autoplay.running && typeof autoplay.stop === "function") {
              autoplay.stop();
              swipers.push(swiper);
            }
          }
          const style = document.createElement("style");
          style.setAttribute("data-alioth-web-capture-stabilization", "v1");
          style.textContent = "*,*::before,*::after{"
            + "animation-play-state:paused!important;"
            + "transition-duration:0s!important;transition-delay:0s!important;"
            + "scroll-behavior:auto!important;}";
          (document.head || document.documentElement).appendChild(style);
          window[key] = {animations, swipers, style};
          return {
            status: "applied",
            swiper_autoplay_paused: swipers.length,
            css_animations_paused: animations.length,
          };
        }
        """
    )
    result = dict(facts) if isinstance(facts, dict) else {"status": "unavailable"}
    return {
        "schema_version": "web-capture-stabilization.v1",
        **carousel_facts,
        "status": str(result.get("status") or "unavailable"),
        "swiper_autoplay_paused": int(result.get("swiper_autoplay_paused") or 0),
        "css_animations_paused": int(result.get("css_animations_paused") or 0),
    }


async def _restore_web_capture_motion(page: Any) -> dict[str, Any]:
    """幂等恢复正式制品窗口暂停的页面运动。"""

    from services.api.app.infrastructure.browser.web_carousel_motion import restore_web_carousels

    carousel_facts = await restore_web_carousels(page)

    facts = await page.evaluate(
        """
        () => {
          const key = Symbol.for("alioth.web.capture.stabilization.v1");
          const state = window[key];
          if (!state) return {status: "not_applied"};
          let swiperRestored = 0, animationRestored = 0;
          if (state.style && state.style.isConnected) state.style.remove();
          for (const swiper of state.swipers || []) {
            try {
              if (swiper.autoplay && typeof swiper.autoplay.start === "function") {
                swiper.autoplay.start();
                swiperRestored += 1;
              }
            } catch (_) {}
          }
          for (const animation of state.animations || []) {
            try {
              if (animation.playState === "paused") {
                animation.play();
                animationRestored += 1;
              }
            } catch (_) {}
          }
          delete window[key];
          return {
            status: "restored",
            swiper_autoplay_restored: swiperRestored,
            css_animations_restored: animationRestored,
          };
        }
        """
    )
    if not isinstance(facts, dict):
        return {"status": "restore_unavailable", **carousel_facts}
    return {
        **carousel_facts,
        "status": str(facts.get("status") or "restore_unavailable"),
        "swiper_autoplay_restored": int(facts.get("swiper_autoplay_restored") or 0),
        "css_animations_restored": int(facts.get("css_animations_restored") or 0),
    }


async def _read_web_structure(page: Any) -> dict[str, Any]:
    """仅 Web 读取自然可见结构；哈希派生值不会改写截图或 DOM。"""

    try:
        value = await page.evaluate(
            """
            () => {
              __ALIOTH_VOLATILE_MOTION__
              const visible = el => {
                const style = getComputedStyle(el), rect = el.getBoundingClientRect();
                return style.display !== "none" && style.visibility !== "hidden"
                  && rect.width > 0 && rect.height > 0 && rect.bottom > 0
                  && rect.top < innerHeight && rect.right > 0 && rect.left < innerWidth;
              };
              const challenge = [...document.querySelectorAll(
                "[data-sitekey],[id*='captcha' i],[class*='captcha' i],"
                + "[id*='turnstile' i],[class*='turnstile' i],iframe[src*='captcha' i],"
                + "iframe[title*='human verification' i]"
              )].some(visible);
              const rows = [...document.querySelectorAll("body,body *")]
                .filter(el => visible(el) && !isVolatileWebMotion(el)).map(el => {
                const r = el.getBoundingClientRect();
                return [el.tagName, el.id, el.getAttribute("role"),
                  el.getAttribute("aria-selected"),
                  el.getAttribute("aria-expanded"), el.getAttribute("src"), el.getAttribute("href"),
                  Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height),
                  [...el.childNodes].filter(n => n.nodeType === 3)
                    .map(n => n.textContent).join("")];
              });
              return {document_time_origin_ms: Math.round(performance.timeOrigin * 100) / 100,
                structure: JSON.stringify(rows), visibility_state: document.visibilityState,
                human_verification_present: challenge};
            }
            """.replace("__ALIOTH_VOLATILE_MOTION__", WEB_VOLATILE_MOTION_JS)
        )
        if not isinstance(value, dict) or not isinstance(value.get("structure"), str):
            return {}
        return {
            "structure_digest": _capture_identity_digest(value["structure"]),
            "document_time_origin_ms": value.get("document_time_origin_ms"),
            "visibility_state": value.get("visibility_state"),
            "human_verification_present": value.get("human_verification_present"),
            "observed_at_ms": time.perf_counter() * 1000,
        }
    except Exception:  # noqa: BLE001 - 采集能力缺失保留为未知，不制造空结构稳定证据。
        return {}


def _web_necessary_media_ready(readiness: Mapping[str, Any]) -> object:
    """Web v2 只消费完整必要集合；H5 比例和历史 v1 事实不回填重判。"""
    media = readiness.get("viewport_media")
    if isinstance(media, Mapping):
        if media.get("state") != "ready":
            return False
        if media.get("schema_version") == "viewport-media.v2":
            count, ready = media.get("necessary_count"), media.get("necessary_ready_count")
            return (
                media.get("sample_truncated") is False
                and type(count) is int
                and type(ready) is int
                and 0 <= count <= 100
                and ready == count
            )
    return readiness.get("necessary_media_ready")


async def _wait_for_web_page_settle(
    *,
    page: Any,
    network_inflight: dict[int, dict[str, str]] | None = None,
    max_wait_ms: int = PAGE_SETTLE_MAX_WAIT_MS,
    poll_interval_ms: int = PAGE_SETTLE_POLL_INTERVAL_MS,
    startup_policy: str = "direct_home",
    diagnostics: dict[str, Any] | None = None,
    profile: WebReadinessProfile = DEFAULT_WEB_READINESS_PROFILE,
) -> bool:
    """复用既有只读观测器，为 Web 记录可复核的连续结构稳定窗口。"""

    from services.api.app.infrastructure.browser.web_settle_diagnostics import record_window_reset

    # 截图前窗口只有两秒，通用一秒轮询无法取得三样本；缩短间隔而不延长截止或降低样本门槛。
    poll_interval_ms = min(
        poll_interval_ms,
        max(1, int(profile.minimum_window_ms / (profile.minimum_samples - 1))),
    )
    deadline = time.perf_counter() * 1000 + max_wait_ms
    revision = await _install_page_state_monitor(page, web_volatile_motion=True)
    previous = None
    stable_count = sample_count = 0
    window_start = None
    facts = diagnostics if diagnostics is not None else {}
    facts.clear()
    facts.update(
        {
            "outcome": "waiting",
            "settle_complete": False,
            "quorum_met": False,
            "timed_out": False,
            "sample_count": 0,
            "stable_sample_count": 0,
        }
    )
    while time.perf_counter() * 1000 < deadline:
        readiness = await _read_page_readiness(page) or {}
        structure = await _read_web_structure(page)
        now = time.perf_counter() * 1000
        pending = _web_critical_network_pending(
            network_inflight,
            page_url=str(page.url),
            profile=profile,
        )
        sample_count += 1
        reasons = []
        viewport_media = readiness.get("viewport_media")
        if isinstance(viewport_media, Mapping) and viewport_media.get("state") != "ready":
            reasons.append("viewport_media_not_ready")
        for name, expected in (
            ("core_present", True),
            ("necessary_media_ready", True),
            ("visible_loader", False),
            ("transition_active", False),
        ):
            actual = (
                _web_necessary_media_ready(readiness)
                if name == "necessary_media_ready"
                else readiness.get(name)
            )
            if actual is not expected:
                reasons.append(name + "_not_ready")
        if float(readiness.get("dom_quiet_ms") or 0) < profile.minimum_dom_quiet_ms:
            reasons.append("dom_not_quiet")
        if profile.network_required and pending != 0:
            reasons.append(
                "required_network_unobservable" if pending is None else "critical_network_pending"
            )
        if (
            not structure.get("structure_digest")
            or structure.get("document_time_origin_ms") is None
        ):
            reasons.append("structure_unobservable")
        if readiness.get("flutter_bootstrap_pending") is True:
            reasons.append("flutter_bootstrap_pending")
        key = (
            str(page.url),
            structure.get("document_time_origin_ms"),
            structure.get("structure_digest"),
        )
        record_window_reset(
            facts, previous=previous, current=key, reasons=reasons,
            sample_index=sample_count, observed_at_ms=now,
            previous_stable_count=stable_count,
        )
        if reasons:
            stable_count, window_start = 0, None
        elif key != previous or window_start is None:
            stable_count, window_start = 1, now
        else:
            stable_count += 1
        quorum = bool(
            not reasons
            and stable_count >= profile.minimum_samples
            and window_start is not None
            and now - window_start >= profile.minimum_window_ms
        )
        facts.update(
            {
                "sample_count": sample_count,
                "stable_sample_count": stable_count,
                "sample_window_started_ms": window_start,
                "sample_window_ended_ms": now,
                "document_time_origin_ms": structure.get("document_time_origin_ms"),
                "structure_digest": structure.get("structure_digest"),
                "readiness": readiness,
                "critical_network_pending": pending,
                "network_observable": network_inflight is not None,
                "blocking_reasons": reasons,
                "dynamic_reasons": [] if quorum else ["stable_window_pending"],
                "quorum_met": quorum,
            }
        )
        if quorum and now <= deadline:
            facts.update({"outcome": "settled", "settle_complete": True})
            return True
        previous = key
        revision = await _wait_for_page_state_change(
            page=page,
            previous_revision=revision,
            timeout_ms=max(0, min(poll_interval_ms, int(deadline - now))),
        )
    facts.update({"outcome": "settle_timeout", "timed_out": True, "quorum_met": False})
    return False


def _web_readiness_metrics(
    *,
    web_capture_stage: str | None,
    identity_before: str | None,
    identity_after: str | None,
    page_settle_complete: bool,
    page_inspection_ready: bool,
    readiness: dict[str, Any] | None,
    network_observable: bool,
    critical_network_pending: int | None,
    profile: WebReadinessProfile = DEFAULT_WEB_READINESS_PROFILE,
    settle_diagnostics: Mapping[str, Any] | None = None,
    capture_consistency: Mapping[str, Any] | None = None,
    capture_started_ms: float | None = None,
    capture_ended_ms: float | None = None,
    structure_before: str | None = None,
    structure_after: str | None = None,
) -> dict[str, object] | None:
    """仅为 Web V2 输出可持久化就绪事实，不改变 H5/App 采集结果。"""

    role_by_stage = {
        "web_entry_probe": "entry_observation",
        "web_page_probe": "interaction_after",
        "web_page_ready_baseline": "classification",
        "web_primary_content_evidence": "classification",
        "web_full_page_supplemental": "supplemental_classification",
    }
    role = role_by_stage.get(web_capture_stage or "")
    if role is None:
        return None
    current = readiness or {}
    blocking_loader = current.get("visible_loader", current.get("blocking_loader"))
    blocking_transition = current.get(
        "transition_active",
        current.get("blocking_transition"),
    )
    wait = dict(settle_diagnostics or {})
    consistency = dict(capture_consistency or {})
    before = consistency.get("before") or {}
    after = consistency.get("after") or {}
    result = {
        "role": role,
        "identity_before": identity_before,
        "identity_after": identity_after,
        "artifacts_same_epoch": bool(
            before.get("document_observation_available") is True
            and after.get("document_observation_available") is True
            and _optional_nonnegative_float(before.get("document_time_origin_ms")) is not None
            and before.get("document_time_origin_ms") == after.get("document_time_origin_ms")
        ),
        "capture_failed": current.get("blank_document") is True,
        "blank_document": current.get("blank_document") is True,
        "timed_out": wait.get("timed_out") is True,
        "settle_complete": page_settle_complete is True and wait.get("settle_complete") is True,
        "quorum_met": wait.get("quorum_met"),
        "capture_consistent": None,
        "profile_version": profile.version,
        "inspection_ready": page_inspection_ready,
        "settle_diagnostics": wait,
        "capture_consistency": consistency,
        "capture_started_ms": capture_started_ms,
        "capture_ended_ms": capture_ended_ms,
        "structure_before": structure_before,
        "structure_after": structure_after,
        "core_present": current.get("core_present"),
        "visible_loader": blocking_loader,
        "transition_active": blocking_transition,
        "necessary_media_ready": _web_necessary_media_ready(current),
        "viewport_media": current.get("viewport_media"),
        "blocking_loader": current.get("blocking_loader"),
        "human_verification_present": current.get("human_verification_present"),
        "dom_quiet_ms": _optional_nonnegative_float(current.get("dom_quiet_ms")),
        "minimum_dom_quiet_ms": profile.minimum_dom_quiet_ms,
        "network_capability": "observable" if network_observable else "unknown",
        "network_required": profile.network_required,
        "network_scope": web_profile_network_scope(profile),
        "critical_network_pending": (critical_network_pending if network_observable else None),
    }
    result["capture_consistent"] = web_capture_consistency_proven(result, profile=profile)
    return result


def _optional_nonnegative_float(value: object) -> float | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value < 0
    ):
        return None
    return float(value)


async def _install_page_state_monitor(
    page: Any, *, web_volatile_motion: bool = False
) -> int:
    """安装会话内事件监视器，聚合 DOM、路由、图片和性能状态。"""
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return 0
    try:
        script = """
            () => {
              __ALIOTH_VOLATILE_MOTION__
              const filterVolatileMotion = __ALIOTH_FILTER_VOLATILE_MOTION__;
              if (!window.__aliothQcStateMonitor) {
                const state = {
                  revision: 0,
                  last_change_ms: performance.now(),
                  route: window.location.href,
                  filter_volatile_motion: filterVolatileMotion,
                };
                const changed = records => {
                  if (state.filter_volatile_motion && Array.isArray(records)
                      && records.length > 0 && records.every(record => {
                    if (!isVolatileWebMotion(record.target)) return false;
                    if (record.type === "attributes" || record.type === "characterData")
                      return true;
                    if (record.type !== "childList") return false;
                    const nodes = [...record.addedNodes, ...record.removedNodes];
                    if (nodes.every(node => node.nodeType === Node.TEXT_NODE)) return true;
                    return record.target.matches("span.time-current,span.time-duration")
                      && nodes.length > 0 && nodes.every(node =>
                        node.nodeType === Node.TEXT_NODE
                        || (node.nodeType === Node.ELEMENT_NODE
                          && node.matches("span.time-min-width")));
                  })) return;
                  state.revision += 1;
                  state.last_change_ms = performance.now();
                  state.route = window.location.href;
                };
                new MutationObserver(changed).observe(document.documentElement, {
                  subtree: true,
                  childList: true,
                  attributes: true,
                  characterData: true,
                });
                window.addEventListener("hashchange", changed, { passive: true });
                window.addEventListener("popstate", changed, { passive: true });
                window.addEventListener("load", changed, { passive: true });
                window.__aliothQcStateMonitor = state;
              }
              if (filterVolatileMotion)
                window.__aliothQcStateMonitor.filter_volatile_motion = true;
              const images = [...document.images];
              window.__aliothQcStateMonitor.image_complete_ratio = images.length
                ? images.filter((item) => item.complete).length / images.length
                : 1;
              window.__aliothQcStateMonitor.resource_count = performance
                .getEntriesByType("resource").length;
              return window.__aliothQcStateMonitor.revision;
            }
            """
        script = script.replace("__ALIOTH_VOLATILE_MOTION__", WEB_VOLATILE_MOTION_JS)
        script = script.replace(
            "__ALIOTH_FILTER_VOLATILE_MOTION__",
            "true" if web_volatile_motion else "false",
        )
        value = await evaluate(script)
        return int(value) if isinstance(value, int | float) else 0
    except Exception:  # noqa: BLE001
        return 0


async def _wait_for_page_state_change(*, page: Any, previous_revision: int, timeout_ms: int) -> int:
    if timeout_ms <= 0:
        return previous_revision
    wait_for_function = getattr(page, "wait_for_function", None)
    evaluate = getattr(page, "evaluate", None)
    if callable(wait_for_function) and callable(evaluate):
        try:
            await wait_for_function(
                "previous => window.__aliothQcStateMonitor"
                " && window.__aliothQcStateMonitor.revision > previous",
                arg=previous_revision,
                timeout=timeout_ms,
            )
            value = await evaluate(
                "() => window.__aliothQcStateMonitor ? window.__aliothQcStateMonitor.revision : 0"
            )
            return int(value) if isinstance(value, int | float) else previous_revision
        except Exception:  # noqa: BLE001
            return previous_revision
    await asyncio.sleep(timeout_ms / 1000)
    return previous_revision


async def _read_page_readiness(page: Any) -> dict[str, Any] | None:
    """稳定业务页只等待核心容器和当前视口必要媒体，不等待全部懒加载资源。"""
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return None
    try:
        value = await evaluate(
            """
            () => {
              const isVisible = (item) => {
                const style = getComputedStyle(item);
                const rect = item.getBoundingClientRect();
                return style.display !== "none" && style.visibility !== "hidden"
                  && Number(style.opacity || 1) > 0.05
                  && rect.width > 0 && rect.height > 0;
              };
              const core = [...document.querySelectorAll(
                "main,[role='main'],#app,#root,.app,.page,.content,.van-tabbar",
              )].find((item) => item.getClientRects().length) || document.body;
              const media = [...document.querySelectorAll("img,video")].filter((item) => {
                const rect = item.getBoundingClientRect();
                return isVisible(item)
                  && rect.right >= 0 && rect.left <= window.innerWidth
                  && rect.bottom >= 0 && rect.top <= window.innerHeight;
              });
              const readyMedia = media.filter((item) => (
                item instanceof HTMLImageElement
                  ? item.complete && item.naturalWidth > 0
                  : item.readyState >= 2
              ));
              const businessMedia = media.filter(item => core.contains(item)
                && !item.closest('header,nav,footer,aside,[role="navigation"],'
                  + '[role="dialog"],[aria-modal="true"],.ad,[data-ad],'
                  + '.loader,.loading,.van-loading,.spinner,.skeleton'));
              const isBlockingSurface = (item) => {
                if (!isVisible(item)) return false;
                const style = getComputedStyle(item);
                const rect = item.getBoundingClientRect();
                const viewportArea = Math.max(window.innerWidth * window.innerHeight, 1);
                const coverage = Math.min(rect.width * rect.height / viewportArea, 1);
                const overlayPosition = ["fixed", "absolute", "sticky"].includes(
                  style.position,
                );
                return (overlayPosition && coverage >= 0.1)
                  || (item.getAttribute("aria-busy") === "true" && coverage >= 0.25);
              };
              const visibleLoaders = [...document.querySelectorAll(
                ".loader,.loading,.van-loading,.spinner,.skeleton,[aria-busy='true']",
              )].filter(isVisible);
              const activeTransitions = [...document.querySelectorAll(
                "[class*='-enter-active'],[class*='-leave-active']",
              )].filter(isVisible);
              const flutterBootstrapDeclared = Boolean(document.querySelector(
                'script[src*="flutter_bootstrap.js"]',
              ));
              const flutterRuntimeMounted = Boolean(document.querySelector(
                "flt-glass-pane,flt-semantics-host,flutter-view,canvas",
              ));
              const meaningfulSelector = [
                "a[href]", "button", "input", "textarea", "select", "img", "picture",
                "video", "audio", "canvas", "svg", "iframe", "object", "embed", "table",
                "li", "h1", "h2", "h3", "h4", "h5", "h6",
              ].join(",");
              const hasVisibleMeaningfulNode = [...document.body.querySelectorAll(
                meaningfulSelector,
              )].some(isVisible);
              const hasVisibleBackground = getComputedStyle(document.body).backgroundImage
                !== "none" || [...document.body.querySelectorAll("*")].some(
                  (item) => isVisible(item) && getComputedStyle(item).backgroundImage !== "none",
                );
              const blankDocument = !(document.body.innerText || "").trim()
                && !hasVisibleMeaningfulNode
                && !hasVisibleBackground
                && visibleLoaders.length === 0
                && activeTransitions.length === 0;
              const blockingLoaders = visibleLoaders.filter(isBlockingSurface);
              const blockingTransitions = activeTransitions.filter(isBlockingSurface);
              const businessNodes = [...core.querySelectorAll(meaningfulSelector)].filter(
                item => isVisible(item) && !item.closest(
                  '[role="dialog"],[aria-modal="true"],[aria-busy="true"],'
                  + '.loader,.loading,.spinner,.skeleton',
                ),
              );
              const loadedMedia = [...core.querySelectorAll('img,video')].some(
                item => isVisible(item) && !item.closest(
                  'header,nav,footer,[role="navigation"],[role="tablist"],'
                  + '[role="dialog"],[aria-modal="true"],[aria-busy="true"],'
                  + '.loader,.loading,.van-loading,.spinner,.skeleton',
                ) && (item instanceof HTMLImageElement
                  ? item.complete && item.naturalWidth > 0
                  : item instanceof HTMLVideoElement
                    ? item.readyState >= 2
                    : false),
              );
              const contentWalker = document.createTreeWalker(core, NodeFilter.SHOW_TEXT);
              let loadedText = false;
              while (contentWalker.nextNode()) {
                const node = contentWalker.currentNode;
                const parent = node.parentElement;
                const text = (node.textContent || '').trim();
                if (parent && isVisible(parent) && text && !parent.closest(
                  'header,nav,footer,button,h1,h2,h3,h4,h5,h6,script,style,'
                  + '[role="navigation"],[role="tablist"],[role="dialog"],'
                  + '[aria-modal="true"],[aria-busy="true"],'
                  + '.loader,.loading,.van-loading,.spinner,.skeleton',
                ) && !/^(加载中?|正在加载|请稍候|请稍后|loading)[\\s.…]*$/i.test(text)) {
                  loadedText = true;
                  break;
                }
              }
              const loadedContent = loadedMedia || loadedText;
              return {
                document_ready_state: document.readyState,
                blank_document: blankDocument,
                core_present: Boolean(!blankDocument && core && core.getClientRects().length),
                business_text_characters: (core.innerText || '').trim().length,
                business_element_count: businessNodes.length,
                visible_media_count: media.length,
                ready_media_count: readyMedia.length,
                necessary_media_ready: media.length === 0
                  || readyMedia.length / media.length >= 0.9,
                business_media_pending: businessMedia.length > 0
                  && businessMedia.filter(item => readyMedia.includes(item)).length
                    / businessMedia.length < 0.9,
                visible_loader: visibleLoaders.length > 0,
                visible_loader_count: visibleLoaders.length,
                loading_business_content_missing: visibleLoaders.some(
                  item => core.contains(item),
                ) && !loadedContent,
                blocking_loader: blockingLoaders.length > 0,
                blocking_loader_count: blockingLoaders.length,
                transition_active: activeTransitions.length > 0,
                active_transition_count: activeTransitions.length,
                blocking_transition: blockingTransitions.length > 0,
                blocking_transition_count: blockingTransitions.length,
                flutter_bootstrap_pending: flutterBootstrapDeclared
                  && !flutterRuntimeMounted
                  && !(document.body.innerText || "").trim(),
                dom_quiet_ms: window.__aliothQcStateMonitor
                  ? performance.now() - window.__aliothQcStateMonitor.last_change_ms
                  : 0,
              };
            }
            """
        )
        if not isinstance(value, dict):
            return None
        from services.api.app.infrastructure.browser.web_media_readiness import (
            read_viewport_media_readiness,
        )

        result = dict(value)
        result["viewport_media"] = await read_viewport_media_readiness(page)
        return result
    except Exception:  # noqa: BLE001
        return None


async def _assess_page_inspection_readiness(
    *,
    page: Any,
    artifacts: list[PageCaptureArtifact],
    network_inflight: dict[int, dict[str, str]] | None = None,
    startup_policy: str = "h5",
    wait_diagnostics: dict[str, Any] | None = None,
) -> tuple[bool, dict[str, Any]]:
    """稳定等待耗尽后，区分真正加载态与已有可质检证据的动态页面。"""

    readiness = await _read_page_readiness(page)
    evidence = _inspection_evidence_summary(artifacts)
    critical_network_pending = sum(
        1
        for item in (network_inflight or {}).values()
        if item.get("resource_type") in PAGE_SETTLE_CRITICAL_RESOURCE_TYPES
    )
    blocking_reasons: list[str] = []
    dynamic_reasons: list[str] = []

    if readiness is None:
        blocking_reasons.append("readiness_unavailable")
    else:
        if readiness.get("core_present") is not True:
            blocking_reasons.append("core_content_missing")
        sparse_page = evidence["ui_element_count"] < 5 and evidence["visible_text_characters"] < 40
        visible_loader = readiness.get("visible_loader") is True
        blocking_loader = readiness.get("blocking_loader", readiness.get("visible_loader"))
        if blocking_loader is True or (visible_loader and sparse_page):
            blocking_reasons.append("blocking_loader_visible")
        elif visible_loader:
            dynamic_reasons.append("localized_loader_visible")
        if (
            startup_policy != "direct_home"
            and readiness.get("loading_business_content_missing") is True
        ):
            blocking_reasons.append("business_content_loading")

        transition_active = readiness.get("transition_active") is True
        blocking_transition = readiness.get(
            "blocking_transition", readiness.get("transition_active")
        )
        if blocking_transition is True or (transition_active and sparse_page):
            blocking_reasons.append("blocking_transition_active")
        elif transition_active:
            dynamic_reasons.append("localized_transition_active")

        if readiness.get("necessary_media_ready") is not True:
            dynamic_reasons.append("visible_media_still_loading")
        if startup_policy != "direct_home" and readiness.get("business_media_pending") is True:
            blocking_reasons.append("business_media_loading")
        if float(readiness.get("dom_quiet_ms") or 0) < PAGE_DOM_QUIET_WINDOW_MS:
            dynamic_reasons.append("dom_continues_changing")

    visible_text = str(evidence.get("visible_text") or "")
    if (
        startup_policy != "direct_home"
        and visible_text
        and _looks_like_line_loading_text(visible_text)
    ):
        if (
            evidence["startup_ad_dismissal_visible"] is True
            and await _has_visible_startup_ad_dismissal(page)
        ):
            dynamic_reasons.append("startup_ad_ready_after_line_loading")
        else:
            blocking_reasons.append("line_loading_text_visible")
    if evidence["inspectable_evidence"] is not True:
        blocking_reasons.append("inspectable_evidence_missing")
    if critical_network_pending:
        dynamic_reasons.append("background_requests_pending")
    if not blocking_reasons and not dynamic_reasons:
        dynamic_reasons.append("visible_text_or_page_state_keeps_changing")

    inspection_ready = not blocking_reasons
    diagnostics = {
        **dict(wait_diagnostics or {}),
        "outcome": "inspectable_dynamic" if inspection_ready else "blocked",
        "blocking_reasons": list(dict.fromkeys(blocking_reasons)),
        "dynamic_reasons": list(dict.fromkeys(dynamic_reasons)),
        "critical_network_pending": critical_network_pending,
        "readiness": dict(readiness or {}),
        "evidence": {key: value for key, value in evidence.items() if key != "visible_text"},
    }
    return inspection_ready, diagnostics


def _inspection_evidence_summary(
    artifacts: list[PageCaptureArtifact],
) -> dict[str, Any]:
    dom_characters = 0
    visible_text = ""
    ui_element_count = 0
    has_screenshot = False
    has_loading_page = False
    visible_startup_dismissal = False
    for artifact in artifacts:
        if artifact.artifact_type == "dom":
            dom_content = str(artifact.content or "")
            dom_characters += len(dom_content)
            has_loading_page |= bool(
                re.search(
                    r"class\s*=\s*['\"][^'\"]*\bloading-page\b",
                    dom_content,
                    re.IGNORECASE,
                )
            )
        elif artifact.artifact_type == "visible_text":
            visible_text += " " + str(artifact.content or "")
        elif artifact.artifact_type == "ui_snapshot" and isinstance(artifact.content, dict):
            elements = artifact.content.get("elements")
            if isinstance(elements, list):
                ui_element_count += len(elements)
                visible_startup_dismissal |= any(
                    isinstance(element, dict)
                    and isinstance(element.get("class_tokens"), list)
                    and "down-time"
                    in {str(token).casefold() for token in element.get("class_tokens", [])}
                    and str(element.get("text") or "").strip() != ""
                    for element in elements
                )
        elif artifact.artifact_type in {"screenshot", "screenshot_sequence"}:
            has_screenshot = True

    normalized_text = _normalize_visible_text(visible_text)
    inspectable_evidence = bool(
        has_screenshot and dom_characters > 0 and (normalized_text or ui_element_count > 0)
    )
    return {
        "has_screenshot": has_screenshot,
        "dom_characters": dom_characters,
        "visible_text_characters": len(normalized_text),
        "ui_element_count": ui_element_count,
        "inspectable_evidence": inspectable_evidence,
        "startup_ad_dismissal_visible": (has_loading_page and visible_startup_dismissal),
        "visible_text": normalized_text,
    }


def _normalize_visible_text(text: str) -> str:
    return " ".join(text.split()).lower()


def _page_url_sha256(page: Any) -> str | None:
    """记录页面身份摘要，避免诊断字段重复暴露查询参数。"""
    url = str(getattr(page, "url", "") or "")
    if not url:
        return None
    return f"sha256:{hashlib.sha256(url.encode('utf-8')).hexdigest()}"


def _looks_like_line_loading_text(text: str) -> bool:
    return any(marker.lower() in text for marker in LINE_LOADING_TEXT_MARKERS)


def _looks_like_startup_ad_text(text: str) -> bool:
    # 促销计时、免广告权益及普通关闭提示不能跳过业务加载判定。
    text = text.casefold()
    if any(marker.lower() in text for marker in STARTUP_AD_TEXT_MARKERS):
        return True
    return "跳过" in text and ("广告" in text or "秒" in text or "s后" in text)


async def _has_visible_startup_ad_dismissal(page: Any) -> bool:
    """确认线路加载壳已经进入可关闭的开屏广告阶段。"""

    locator_factory = getattr(page, "locator", None)
    if not callable(locator_factory):
        return False
    try:
        locator = locator_factory(STARTUP_AD_DISMISSAL_SELECTOR)
        evaluate_all = getattr(locator, "evaluate_all", None)
        if callable(evaluate_all) and await evaluate_all("""
            nodes => nodes.some(node => {
              const layer = node.closest('.loading-page');
              if (!layer) return false;
              const rect = layer.getBoundingClientRect();
              const button = node.getBoundingClientRect();
              const style = getComputedStyle(layer);
              return ['fixed', 'absolute'].includes(style.position)
                && style.display !== 'none' && style.visibility !== 'hidden'
                && Number(style.opacity || 1) > 0
                && rect.x <= 2 && rect.y <= 2
                && rect.right >= innerWidth - 2 && rect.bottom >= innerHeight - 2
                && button.width > 0 && button.height > 0;
            })
        """):
            return True
        flutter_target = await _resolve_flutter_fullscreen_startup_skip_target(page)
        return flutter_target.get("match_count") == 1
    except Exception:  # noqa: BLE001  # 页面跳转销毁上下文时继续走普通稳定判断
        return False


async def _resolve_flutter_fullscreen_startup_skip_target(page: Any) -> dict[str, Any]:
    """在动作时重新验证 Flutter 全屏开屏层及其唯一右上角跳过控件。"""
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return {"match_count": -1, "selector": None}
    try:
        result = await evaluate(
            """
            () => {
              const visible = (element) => {
                if (!(element instanceof Element)) return false;
                const style = getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.display !== "none"
                  && style.visibility !== "hidden"
                  && Number(style.opacity || 1) > 0
                  && rect.width > 0
                  && rect.height > 0;
              };
              const viewportWidth = window.innerWidth;
              const viewportHeight = window.innerHeight;
              // Flutter 不保证给角标控件加 role="text"（杏吧实测就没有），
              // 所以按可点语义节点取集合，再用 role / 文案 / 几何三重收紧。
              const dismissTexts = new Set(["跳过", "进入"]);
              const matches = [...document.querySelectorAll(
                'flt-semantics[flt-tappable]'
              )].filter((element) => {
                if (!visible(element)) return false;
                const role = element.getAttribute("role") || "";
                if (role && role !== "text") return false;
                const text = (element.innerText || element.textContent || "")
                  .replace(/\\s+/g, "");
                if (!dismissTexts.has(text)) return false;
                const rect = element.getBoundingClientRect();
                const zIndex = Number.parseInt(getComputedStyle(element).zIndex || "0", 10);
                const layer = element.parentElement?.closest(
                  'flt-semantics[flt-tappable]'
                );
                if (!layer || !visible(layer)) return false;
                const layerRect = layer.getBoundingClientRect();
                return Number.isFinite(zIndex)
                  && zIndex >= 1
                  && rect.width >= 30 && rect.width <= 100
                  && rect.height >= 20 && rect.height <= 60
                  && rect.x >= viewportWidth * 0.65
                  && rect.right <= viewportWidth + 2
                  && rect.y >= 0 && rect.y <= viewportHeight * 0.1
                  && Math.abs(layerRect.x) <= 2
                  && Math.abs(layerRect.y) <= 2
                  && layerRect.width >= viewportWidth * 0.95
                  && layerRect.height >= viewportHeight * 0.95;
              }).map((element) => ({
                selector: element.id ? `#${CSS.escape(element.id)}` : null,
              }));
              return {
                match_count: matches.length,
                selector: matches.length === 1 ? matches[0].selector : null,
              };
            }
            """
        )
    except Exception:  # noqa: BLE001
        return {"match_count": -1, "selector": None}
    if not isinstance(result, dict):
        return {"match_count": -1, "selector": None}
    match_count = result.get("match_count")
    selector = result.get("selector")
    return {
        "match_count": match_count if isinstance(match_count, int) else -1,
        "selector": selector if isinstance(selector, str) else None,
    }


async def _resolve_flutter_fullscreen_startup_countdown_target(
    page: Any,
) -> dict[str, Any]:
    """在采集时验证 Flutter 全屏开屏层右上角唯一纯数字倒计时。"""
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return {"match_count": -1, "selector": None}
    try:
        result = await evaluate(
            """
            () => {
              const visible = (element) => {
                if (!(element instanceof Element)) return false;
                const style = getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.display !== "none"
                  && style.visibility !== "hidden"
                  && Number(style.opacity || 1) > 0
                  && rect.width > 0
                  && rect.height > 0;
              };
              const viewportWidth = window.innerWidth;
              const viewportHeight = window.innerHeight;
              const matches = [...document.querySelectorAll(
                'flt-semantics[flt-tappable][role="text"]'
              )].filter((element) => {
                if (!visible(element)) return false;
                const text = (element.innerText || element.textContent || "")
                  .replace(/\\s+/g, "");
                if (!/^\\d{1,2}$/.test(text)) return false;
                const rect = element.getBoundingClientRect();
                const zIndex = Number.parseInt(getComputedStyle(element).zIndex || "0", 10);
                const layer = element.parentElement?.closest(
                  'flt-semantics[flt-tappable]'
                );
                if (!layer || !visible(layer)) return false;
                const layerRect = layer.getBoundingClientRect();
                return Number.isFinite(zIndex)
                  && zIndex >= 1
                  && rect.width >= 30 && rect.width <= 100
                  && rect.height >= 20 && rect.height <= 60
                  && rect.x >= viewportWidth * 0.65
                  && rect.right <= viewportWidth + 2
                  && rect.y >= 0 && rect.y <= viewportHeight * 0.1
                  && Math.abs(layerRect.x) <= 2
                  && Math.abs(layerRect.y) <= 2
                  && layerRect.width >= viewportWidth * 0.95
                  && layerRect.height >= viewportHeight * 0.95;
              }).map((element) => ({
                selector: element.id ? `#${CSS.escape(element.id)}` : null,
              }));
              return {
                match_count: matches.length,
                selector: matches.length === 1 ? matches[0].selector : null,
              };
            }
            """
        )
    except Exception:  # noqa: BLE001
        return {"match_count": -1, "selector": None}
    if not isinstance(result, dict):
        return {"match_count": -1, "selector": None}
    match_count = result.get("match_count")
    selector = result.get("selector")
    return {
        "match_count": match_count if isinstance(match_count, int) else -1,
        "selector": selector if isinstance(selector, str) else None,
    }


async def _wait_for_flutter_fullscreen_startup_skip_to_hide(
    *,
    page: Any,
    timeout_ms: int,
) -> bool:
    """等待已验签的 Flutter 开屏跳过结构消失，歧义或读取失败均不算关闭。"""
    deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
    while True:
        target = await _resolve_flutter_fullscreen_startup_skip_target(page)
        if target.get("match_count") == 0:
            return True
        if asyncio.get_running_loop().time() >= deadline:
            return False
        wait_for_timeout = getattr(page, "wait_for_timeout", None)
        if callable(wait_for_timeout):
            await wait_for_timeout(200)
        else:
            await asyncio.sleep(0.2)


async def _goto_with_retry(
    *,
    page: Any,
    url: str,
    timeout_ms: int,
    timeout_error_type: type[Exception],
    max_attempts: int = 2,
    audit: dict[str, Any] | None = None,
    post_navigation_validator: Callable[[Any], Awaitable[str | None]] | None = None,
) -> Any:
    """在总导航预算内重试超时，避免外层 deadline 抢先取消第二次尝试。"""
    attempts = max(max_attempts, 1)
    attempt_timeout_ms = max(1, timeout_ms // attempts)
    retry_reasons: list[str] = []
    _update_navigation_retry_audit(
        audit,
        max_attempts=attempts,
        attempt_count=0,
        retry_reasons=retry_reasons,
        outcome="pending",
    )
    for attempt in range(1, attempts + 1):
        before_url = str(getattr(page, "url", "") or "")
        try:
            response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=attempt_timeout_ms,
            )
            validation_reason = (
                await post_navigation_validator(page)
                if post_navigation_validator is not None
                else None
            )
            if validation_reason is not None:
                if attempt < attempts:
                    retry_reasons.append(validation_reason)
                    await _wait_for_navigation_retry_ready(page)
                    continue
                _update_navigation_retry_audit(
                    audit,
                    max_attempts=attempts,
                    attempt_count=attempt,
                    retry_reasons=retry_reasons,
                    outcome=f"{validation_reason}_after_retry",
                )
                return response
            _update_navigation_retry_audit(
                audit,
                max_attempts=attempts,
                attempt_count=attempt,
                retry_reasons=retry_reasons,
                outcome="succeeded" if attempt == 1 else "succeeded_after_retry",
            )
            return response
        except timeout_error_type:
            if await _has_inspectable_committed_document(
                page=page,
                requested_url=url,
                before_url=before_url,
            ):
                validation_reason = (
                    await post_navigation_validator(page)
                    if post_navigation_validator is not None
                    else None
                )
                if validation_reason is not None and attempt < attempts:
                    retry_reasons.append(validation_reason)
                    await _wait_for_navigation_retry_ready(page)
                    continue
                _update_navigation_retry_audit(
                    audit,
                    max_attempts=attempts,
                    attempt_count=attempt,
                    retry_reasons=retry_reasons,
                    outcome=(
                        f"{validation_reason}_after_retry"
                        if validation_reason is not None
                        else "committed_document_after_timeout"
                    ),
                )
                return None
            if attempt >= attempts:
                _update_navigation_retry_audit(
                    audit,
                    max_attempts=attempts,
                    attempt_count=attempt,
                    retry_reasons=retry_reasons,
                    outcome="failed",
                )
                raise
            retry_reasons.append("timeout")
            await _wait_for_navigation_retry_ready(page)
        except Exception as exc:  # noqa: BLE001
            if attempt >= attempts or not _is_transient_goto_error(exc):
                _update_navigation_retry_audit(
                    audit,
                    max_attempts=attempts,
                    attempt_count=attempt,
                    retry_reasons=retry_reasons,
                    outcome="failed",
                )
                raise
            retry_reasons.append(_transient_goto_error_code(exc))
            await _wait_for_navigation_retry_ready(page, expect_error_document=True)
    raise RuntimeError("页面导航重试未产生结果")


def _update_navigation_retry_audit(
    audit: dict[str, Any] | None,
    *,
    max_attempts: int,
    attempt_count: int,
    retry_reasons: list[str],
    outcome: str,
) -> None:
    """只记录稳定错误码和次数，不把目标地址或异常正文写入审计。"""
    if audit is None:
        return
    audit.clear()
    audit.update(
        {
            "schema_version": "navigation_retry.v1",
            "max_attempts": max_attempts,
            "attempt_count": attempt_count,
            "retry_count": len(retry_reasons),
            "retry_reasons": list(retry_reasons),
            "outcome": outcome,
        }
    )


def _transient_goto_error_code(exc: Exception) -> str:
    """把浏览器异常正文收敛为稳定、无 URL 的重试原因。"""
    message = str(exc)
    for marker in TRANSIENT_GOTO_ERROR_MARKERS:
        if marker in message:
            if "chrome-error://chromewebdata/" in marker:
                return "browser_error_document"
            return marker.removeprefix("net::").casefold()
    return "transient_navigation_error"


async def _wait_for_navigation_retry_ready(
    page: Any,
    *,
    expect_error_document: bool = False,
) -> None:
    """重试前等上一跳落定，避免 Chromium 迟到的错误文档反向中断下一次导航。

    空响应、连接重置等导航失败后，Chromium 还会再提交一个 chrome-error:// 错误文档。
    机器繁忙时它可能几百毫秒后才提交，若下一次 goto 已经开始就会被它打断（interrupted
    by another navigation to chrome-error://），最后一次尝试随之失败。所以这类失败要等到
    错误文档确实提交（有上限），其余重试原因只做短暂让步。
    """
    wait_for_timeout = getattr(page, "wait_for_timeout", None)
    if not callable(wait_for_timeout):
        return
    try:
        if expect_error_document:
            for _ in range(_NAVIGATION_ERROR_DOCUMENT_WAIT_MS // _NAVIGATION_RETRY_POLL_MS):
                if str(getattr(page, "url", "") or "").startswith("chrome-error://"):
                    break
                await wait_for_timeout(_NAVIGATION_RETRY_POLL_MS)
        await wait_for_timeout(_NAVIGATION_RETRY_POLL_MS)
    except Exception:  # noqa: BLE001 - 等待失败仍由下一次导航给出权威结果
        return


def _direct_ad_navigation_error_code(exc: Exception) -> str:
    """把广告落地导航异常收敛为不含目标地址的稳定原因。"""

    message = str(exc)
    markers = (
        ("ERR_NAME_NOT_RESOLVED", "name_not_resolved"),
        ("ERR_CONNECTION_REFUSED", "connection_refused"),
        ("ERR_CONNECTION_TIMED_OUT", "connection_timed_out"),
        ("ERR_TIMED_OUT", "navigation_timed_out"),
        ("Timeout", "navigation_timed_out"),
    )
    return next((code for marker, code in markers if marker in message), "navigation_failed")


async def _wait_for_direct_ad_error_document(*, page: Any, timeout_ms: int) -> None:
    """等待 Chromium 提交自身错误文档，以便仍可读取 DOM 和截图。"""

    deadline = asyncio.get_running_loop().time() + max(timeout_ms, 0) / 1000
    while str(getattr(page, "url", "") or "") in {"", "about:blank"}:
        if asyncio.get_running_loop().time() >= deadline:
            return
        wait_for_timeout = getattr(page, "wait_for_timeout", None)
        if callable(wait_for_timeout):
            try:
                await wait_for_timeout(250)
            except Exception:  # noqa: BLE001 - 等待失败由后续附件采集判定完整性
                return
        else:
            await asyncio.sleep(0.25)


async def _has_inspectable_committed_document(
    *,
    page: Any,
    requested_url: str,
    before_url: str,
) -> bool:
    """DOMContentLoaded 超时后，只保留已提交且可读取的 HTTP(S) 文档。"""
    current_url = str(getattr(page, "url", "") or "").strip()
    parsed = urlsplit(current_url)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return False
    if current_url != requested_url and current_url == before_url:
        return False
    read_content = getattr(page, "content", None)
    if not callable(read_content):
        return False
    try:
        async with asyncio.timeout(1.0):
            html = await read_content()
    except Exception:  # noqa: BLE001 - 读取失败时维持原导航重试语义
        return False
    return isinstance(html, str) and "<html" in html.casefold()


def _is_transient_goto_error(exc: Exception) -> bool:
    """识别 Chromium 导航阶段的瞬态网络关闭错误。"""
    message = str(exc)
    return any(marker in message for marker in TRANSIENT_GOTO_ERROR_MARKERS)


async def _block_undeclared_click_navigation(
    *,
    page: Any,
    request: PageCaptureRequest,
    before_url: str,
    timeout_ms: int,
    ad_landing_window: bool = False,
    action_trace: list[dict[str, Any]] | None = None,
) -> str | None:
    action = _normalize_trigger_action(request.trigger_action)
    if action not in {PageTriggerAction.CLICK, PageTriggerAction.SUBMIT_PROBE}:
        return None
    external_new_page_observed = _external_new_page_observed(
        action_trace=action_trace,
        before_url=before_url,
    )
    if page.url == before_url:
        return (
            "external_landing_observed" if external_new_page_observed else "same_page_interaction"
        )
    if _is_lifecycle_transition(request=request, before_url=before_url, after_url=page.url):
        return "lifecycle"
    if ad_landing_window and _is_offsite_navigation(
        before_url=before_url,
        after_url=page.url,
    ):
        # 落地页证据窗口内的同页外跳：证据已在上游采集，观察后必须回位，
        # 保证探索会话不停留在被测站点之外。
        restored = await _restore_page_url(
            page=page,
            target_url=before_url,
            timeout_ms=timeout_ms,
        )
        _record_external_navigation_return(
            action_trace=action_trace,
            page=page,
            restored=restored,
        )
        if not restored:
            raise BlockedPageCaptureActionError(
                "外部落地页已观察，但浏览器未能返回原被测页面，已停止后续探索。",
                error_code="EXTERNAL_NAVIGATION_RETURN_FAILED",
            )
        return "external_landing_observed"
    if request.navigation_depth is not None:
        return "business_navigation"
    final_url = page.url
    await _restore_page_url(page=page, target_url=before_url, timeout_ms=timeout_ms)
    raise BlockedPageCaptureActionError(
        f"single_trigger {action.value} 已进入新的 URL，"
        "预期跨页动作必须显式传入 navigation_depth；"
        f"已尝试恢复到原页面。from={before_url} final_url={final_url}"
    )


def _external_new_page_observed(
    *,
    action_trace: list[dict[str, Any]] | None,
    before_url: str,
) -> bool:
    if not action_trace:
        return False
    navigation = action_trace[-1].get("navigation")
    if not isinstance(navigation, Mapping):
        return False
    urls = navigation.get("new_page_urls")
    if not isinstance(urls, list):
        return False
    return any(
        isinstance(url, str) and _is_offsite_navigation(before_url=before_url, after_url=url)
        for url in urls
    )


def _is_offsite_navigation(*, before_url: str, after_url: str) -> bool:
    """判断点击后的当前页是否离开了被测站点。"""
    before = urlsplit(str(before_url or ""))
    after = urlsplit(str(after_url or ""))
    if before.scheme not in {"http", "https"} or not before.netloc:
        return False
    if after.scheme not in {"http", "https"} or not after.netloc:
        return False
    return after.netloc.casefold() != before.netloc.casefold()


async def _click_target_external_anchor_href(*, page: Any, selector: str | None) -> str | None:
    """点击目标位于外站锚点内时返回其 href；探测失败一律回退普通点击。"""
    if not selector:
        return None
    source = urlsplit(str(getattr(page, "url", "") or ""))
    if source.scheme not in {"http", "https"} or not source.netloc:
        return None
    locator_method = getattr(page, "locator", None)
    if not callable(locator_method):
        return None
    try:
        first = locator_method(selector).first
        evaluate = getattr(first, "evaluate", None)
        if not callable(evaluate):
            return None
        href = await evaluate(
            "el => (el.closest && el.closest('a[href]')) ? el.closest('a[href]').href : ''",
            timeout=EXTERNAL_ANCHOR_PROBE_TIMEOUT_MS,
        )
    except Exception:  # noqa: BLE001  # 探测不可用时不阻断正常点击链路
        return None
    if not isinstance(href, str) or not href.strip():
        return None
    target = urlsplit(href)
    if target.scheme not in {"http", "https"} or not target.netloc:
        return None
    if target.netloc.casefold() == source.netloc.casefold():
        return None
    return href


def _is_lifecycle_transition(
    *, request: PageCaptureRequest, before_url: str, after_url: str
) -> bool:
    """仅放行同站点的关闭、跳过类生命周期跳转。"""
    selector = str(request.selector or "").casefold()
    if not selector or not _request_has_trusted_dismissal_intent(request):
        return False
    before = urlsplit(before_url)
    after = urlsplit(after_url)
    return bool(before.netloc and before.scheme == after.scheme and before.netloc == after.netloc)


def _selector_has_dismissal_intent(selector: str) -> bool:
    normalized = selector.casefold()
    if any(marker.casefold() in normalized for marker in DISMISSAL_SELECTOR_MARKERS):
        return True
    return any(
        marker in normalized
        for marker in (
            ".van-overlay",
            ".van-dialog",
            "down-time",
            "btn-box",
            "app-btn",
            "close-icon",
        )
    )


def _context_pages(context: Any) -> list[Any]:
    pages = getattr(context, "pages", None)
    return list(pages) if isinstance(pages, list) else []


def _subscribe_new_pages(*, context: Any, observed_pages: list[Any]) -> Any | None:
    on = getattr(context, "on", None)
    if not callable(on):
        return None

    def observe(page: Any) -> None:
        observed_pages.append(page)

    on("page", observe)
    return observe


def _unsubscribe_new_pages(*, context: Any, page_event_handler: Any | None) -> None:
    if page_event_handler is None:
        return
    remove_listener = getattr(context, "remove_listener", None)
    if callable(remove_listener):
        remove_listener("page", page_event_handler)


async def _close_unclaimed_new_pages(
    *,
    context: Any,
    pages_before: list[Any],
    observed_new_pages: list[Any],
) -> None:
    """动作异常时关闭本次新开的页面，避免污染后续会话。"""
    before_ids = {id(item) for item in pages_before}
    seen_ids: set[int] = set()
    for candidate in [*observed_new_pages, *_context_pages(context)]:
        candidate_id = id(candidate)
        if candidate_id in before_ids or candidate_id in seen_ids:
            continue
        seen_ids.add(candidate_id)
        close = getattr(candidate, "close", None)
        if not callable(close):
            continue
        try:
            await close()
        except Exception:  # noqa: BLE001  # 清理失败不能覆盖原始动作异常
            pass


async def _record_navigation_outcome(
    *,
    page: Any,
    context: Any,
    before_url: str,
    pages_before: list[Any],
    observed_new_pages: list[Any],
    action_trace: list[dict[str, Any]],
    timeout_ms: int,
    capture_ad_landing_evidence: bool = False,
    isolated_landing: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """记录当前窗口和新窗口结果，并关闭不可继续编排的额外页面。"""
    action_entry = next(
        (
            item
            for item in reversed(action_trace)
            if item.get("action") in {"click", "submit_probe", "direct_ad_outbound"}
            or (
                item.get("action") == "navigate"
                and item.get("navigation_mode") == "isolated_context_goto"
            )
        ),
        None,
    )
    before_ids = {id(item) for item in pages_before}
    new_pages: list[Any] = []
    seen_ids: set[int] = set()
    for candidate in [*observed_new_pages, *_context_pages(context)]:
        candidate_id = id(candidate)
        if candidate_id in before_ids or candidate_id in seen_ids:
            continue
        seen_ids.add(candidate_id)
        new_pages.append(candidate)

    new_page_urls: list[str] = []
    closed_count = 0
    close_failed_count = 0
    followed_visual_vip_new_page = False
    # 隔离广告已经观察完毕；来源 context 新页只审计/关闭，禁止再次广告取证。
    landing_observation = isolated_landing
    for index, new_page in enumerate(new_pages):
        wait_for_load_state = getattr(new_page, "wait_for_load_state", None)
        if isolated_landing is None and callable(wait_for_load_state):
            try:
                await wait_for_load_state(
                    "domcontentloaded",
                    timeout=min(timeout_ms, 1000),
                )
            except Exception:  # noqa: BLE001  # 新窗口 URL 仍需按当前状态留证
                pass
        if index < 10:
            new_page_urls.append(str(getattr(new_page, "url", "") or ""))
        new_page_url = str(getattr(new_page, "url", "") or "")
        if (
            action_entry is not None
            and landing_observation is None
            and (
                capture_ad_landing_evidence
                or _is_offsite_navigation(
                    before_url=before_url,
                    after_url=new_page_url,
                )
            )
        ):
            landing_observation = await _capture_ad_landing_observation(
                page=new_page,
                index=1,
            )
            landing_observation = _normalize_direct_ad_landing_observation(
                action_entry=action_entry,
                observation=landing_observation,
            )
        if index == 0 and _is_followable_visual_vip_new_page(
            action_entry=action_entry,
            before_url=before_url,
            new_pages=new_pages,
        ):
            goto = getattr(page, "goto", None)
            if callable(goto):
                try:
                    await goto(new_page_urls[0], wait_until="domcontentloaded", timeout=timeout_ms)
                    followed_visual_vip_new_page = True
                except Exception:  # noqa: BLE001  # 跟随失败仍必须回收新页并保留事实
                    pass
        close = getattr(new_page, "close", None)
        if callable(close):
            try:
                await close()
                closed_count += 1
            except Exception:  # noqa: BLE001  # 关闭失败不覆盖已采集的跳转证据
                close_failed_count += 1
        else:
            close_failed_count += 1

    after_url = str(getattr(page, "url", "") or "")
    if (
        action_entry is not None
        and landing_observation is None
        and _is_offsite_navigation(before_url=before_url, after_url=after_url)
    ):
        landing_observation = await _capture_ad_landing_observation(
            page=page,
            index=1,
        )
    if action_entry is not None:
        direct_ad_outbound = action_entry.get("action") == "direct_ad_outbound" or (
            action_entry.get("action") == "navigate"
            and action_entry.get("navigation_mode") == "isolated_context_goto"
        )
        if (
            direct_ad_outbound
            and action_entry.get("result") == "opened_new_page_unreachable"
            and action_entry.get("destination_url")
            and new_page_urls
        ):
            new_page_urls[0] = str(action_entry["destination_url"])
        navigation = {
            "before_url": (
                _url_without_query_or_fragment(before_url)
                if direct_ad_outbound
                else str(before_url or "")
            ),
            "after_url": (
                _url_without_query_or_fragment(after_url) if direct_ad_outbound else after_url
            ),
            "current_url_changed": after_url != str(before_url or ""),
            "new_page_count": len(new_pages),
            "new_page_urls": (
                [_url_without_query_or_fragment(item) for item in new_page_urls]
                if direct_ad_outbound
                else new_page_urls
            ),
            "new_pages_truncated": len(new_pages) > 10,
            "closed_after_observation_count": closed_count,
            **(
                {"landing_reachable": False}
                if direct_ad_outbound
                and action_entry.get("result") == "opened_new_page_unreachable"
                else {}
            ),
        }
        if isolated_landing is not None and direct_ad_outbound:
            isolated_navigation = action_entry.get("navigation", {})
            isolated_count = isolated_navigation.get("new_page_count", 0)
            isolated_closed = isolated_navigation.get("closed_after_observation_count", 0)
            navigation.update(
                {
                    "isolated_new_page_count": isolated_count,
                    "isolated_closed_after_observation_count": isolated_closed,
                    "source_context_new_page_count": len(new_pages),
                    "source_context_closed_page_count": closed_count,
                    "source_context_contaminated": bool(new_pages),
                    "new_page_count": isolated_count + len(new_pages),
                    "closed_after_observation_count": isolated_closed + closed_count,
                    "new_page_urls": [
                        *isolated_navigation.get("new_page_urls", []),
                        *navigation["new_page_urls"],
                    ],
                }
            )
            if new_pages:
                navigation["source_context_reason_code"] = "WEB_AD_SOURCE_CONTEXT_CONTAMINATED"
        if action_entry.get("resolved_by") == "validated_visual_coordinate":
            navigation["followed_visual_vip_new_page"] = followed_visual_vip_new_page
        if close_failed_count:
            navigation["new_page_close_failed_count"] = close_failed_count
        action_entry["navigation"] = navigation
    if close_failed_count:
        raise BlockedPageCaptureActionError(
            "动作打开了无法关闭的新页面，已停止后续探索以避免外部页面污染会话。",
            error_code="EXTERNAL_NAVIGATION_ISOLATION_FAILED",
        )
    return landing_observation


def _normalize_direct_ad_landing_observation(
    *,
    action_entry: Mapping[str, Any],
    observation: dict[str, Any],
) -> dict[str, Any]:
    """不可达直开页使用已锁定请求 URL，同时明确标记浏览器错误文档。"""

    if (
        action_entry.get("action") != "direct_ad_outbound"
        or action_entry.get("result") != "opened_new_page_unreachable"
    ):
        return observation
    destination = _safe_direct_ad_landing_url(action_entry.get("destination_url"))
    if destination is None:
        return observation
    reported_url = _url_without_query_or_fragment(destination)[:2048]
    normalized = {
        **observation,
        "url": reported_url,
        "reachable": False,
        "reason_code": "WEB_AD_LANDING_UNREACHABLE",
    }
    dom = observation.get("dom")
    if isinstance(dom, Mapping):
        normalized["dom"] = {**dict(dom), "url": reported_url}
    return normalized


def _is_followable_visual_vip_new_page(
    *,
    action_entry: dict[str, Any] | None,
    before_url: str,
    new_pages: list[Any],
) -> bool:
    if (
        action_entry is None
        or action_entry.get("resolved_by") != "validated_visual_coordinate"
        or len(new_pages) != 1
    ):
        return False
    source = urlsplit(before_url)
    target = urlsplit(str(getattr(new_pages[0], "url", "") or ""))
    return (
        source.scheme.casefold() in {"http", "https"}
        and target.scheme.casefold() == source.scheme.casefold()
        and bool(source.netloc)
        and target.netloc.casefold() == source.netloc.casefold()
        and target.path == "/vip"
    )


async def _restore_page_url(*, page: Any, target_url: str, timeout_ms: int) -> bool:
    target = _normalize_url(target_url)
    if target is None:
        return False
    try:
        await page.go_back(wait_until="domcontentloaded", timeout=timeout_ms)
    except Exception:  # noqa: BLE001
        pass
    if _normalize_url(getattr(page, "url", None)) == target:
        return True
    try:
        await page.goto(target_url, wait_until="domcontentloaded", timeout=timeout_ms)
    except Exception:  # noqa: BLE001
        pass
    return _normalize_url(getattr(page, "url", None)) == target


def _record_external_navigation_return(
    *,
    action_trace: list[dict[str, Any]] | None,
    page: Any,
    restored: bool,
) -> None:
    if not action_trace:
        return
    action_entry = next(
        (
            item
            for item in reversed(action_trace)
            if item.get("action") in {"click", "submit_probe"}
        ),
        None,
    )
    if action_entry is None:
        return
    navigation = action_entry.get("navigation")
    if not isinstance(navigation, dict):
        navigation = {}
        action_entry["navigation"] = navigation
    navigation["return"] = {
        "attempted": True,
        "restored": restored,
        "final_url": str(getattr(page, "url", "") or ""),
    }


def _validate_url_mode_contract(*, page: Any, request: PageCaptureRequest) -> None:
    if request.url is None or request.mode == PageCaptureMode.GOTO:
        return
    current_url = _normalize_url(getattr(page, "url", None))
    requested_url = _normalize_url(request.url)
    if requested_url is None or requested_url == current_url:
        return
    raise BlockedPageCaptureActionError(
        "非 goto 模式不得打开新的 URL；请使用 mode=goto 并按任务边界传入 navigation_depth。"
    )


def _validate_browser_history_return(*, before_url: str, after_url: str) -> None:
    """浏览器返回必须落在同源业务页，禁止把空历史当成探索成功。"""
    if not before_url and not after_url:
        # 轻量测试替身可能不提供 URL；真实 Playwright Page 始终提供。
        return
    before = urlsplit(before_url)
    after = urlsplit(after_url)
    if not after_url or after.scheme.casefold() not in {"http", "https"} or not after.netloc:
        raise BlockedPageCaptureActionError(
            "浏览器历史为空或返回后进入空白页，未形成可信业务页面。",
            error_code="BROWSER_HISTORY_EMPTY",
        )
    if (
        before.scheme.casefold() in {"http", "https"}
        and before.netloc
        and (
            before.scheme.casefold(),
            before.netloc.casefold(),
        )
        != (
            after.scheme.casefold(),
            after.netloc.casefold(),
        )
    ):
        raise BlockedPageCaptureActionError(
            "浏览器历史返回离开当前任务站点，已拒绝把跨站页面作为恢复结果。",
            error_code="BROWSER_HISTORY_OUT_OF_SCOPE",
        )


def _normalize_url(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    parsed = urlsplit(text)
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query, ""))


def _safe_direct_ad_landing_url(value: object) -> str | None:
    """候选与直开共用静态拒绝规则，执行时另做 DNS 固定。"""
    from services.api.app.application.web_ad_outbound_policy import safe_ad_url

    return safe_ad_url(value)


def _url_without_query_or_fragment(value: str) -> str:
    """保留故障命中的资源定位信息，但不把查询参数写入审计产物。"""
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _validate_ad_capture_request(request: PageCaptureRequest) -> None:
    """请求和分发层共用广告门禁，必须先于 CLI、定位器及坐标回退。"""
    direct = request.trigger_action == PageTriggerAction.DIRECT_AD_OUTBOUND
    implicit_web_ad = request.web_capture_stage is not None and request._force_legacy_ad_landing
    if not request.capture_ad_landing_evidence and not direct and not implicit_web_ad:
        return
    if (
        request.mode != PageCaptureMode.GOTO
        or request.trigger_action is not None
        or not request.capture_ad_landing_evidence
        or request.url != request.ad_landing_url
        or request.selector is not None
        or request.submit_selector is not None
        or request._visual_navigation_target is not None
        or any(value is not None for value in _visual_coordinate_fields(request).values())
    ):
        raise BlockedPageCaptureActionError(
            "广告只允许安全 href 隔离直开，禁止点击、定位器及坐标动作",
            error_code="WEB_AD_CLICK_FORBIDDEN",
        )
    from services.api.app.application.web_ad_outbound_policy import (
        AdOutboundDenied,
        validate_ad_url,
    )

    try:
        validate_ad_url(request.url)
    except AdOutboundDenied as exc:
        raise BlockedPageCaptureActionError(
            "广告 href 被安全策略拒绝",
            error_code=exc.reason,
        ) from None


def _validate_request(request: PageCaptureRequest) -> None:
    _validate_ad_capture_request(request)
    if request.startup_policy not in {"h5", "direct_home"}:
        raise ValueError("startup_policy 只允许 h5 或 direct_home")
    if request.web_capture_stage not in {
        None,
        "web_entry_probe",
        "web_page_probe",
        "web_page_ready_baseline",
        "web_primary_content_evidence",
        "web_full_page_supplemental",
    }:
        raise ValueError("web_capture_stage 不受支持")
    if not isinstance(request.capture_transient_messages, bool):
        raise ValueError("capture_transient_messages 必须为布尔值")
    if not isinstance(request.capture_ad_landing_evidence, bool):
        raise ValueError("capture_ad_landing_evidence 必须为布尔值")
    if request.scroll_max_segments is not None and (
        isinstance(request.scroll_max_segments, bool)
        or not isinstance(request.scroll_max_segments, int)
        or not 1 <= request.scroll_max_segments <= 8
    ):
        raise ValueError("scroll_max_segments 必须为 1 到 8 的整数")
    if not isinstance(request.split_scroll_screenshots, bool):
        raise ValueError("split_scroll_screenshots 必须为布尔值")
    if request.horizontal_scroll_max_segments is not None:
        if (
            isinstance(request.horizontal_scroll_max_segments, bool)
            or not isinstance(request.horizontal_scroll_max_segments, int)
            or not 2 <= request.horizontal_scroll_max_segments <= 8
        ):
            raise ValueError("horizontal_scroll_max_segments 必须为 2 到 8 的整数")
    if (
        request.scroll_max_segments is not None
        and request.horizontal_scroll_max_segments is not None
    ):
        raise ValueError("纵向与横向分屏采集参数不得同时设置")
    if request.split_scroll_screenshots and (
        request.mode is not PageCaptureMode.SINGLE_TRIGGER
        or _normalize_trigger_action(request.trigger_action) is not PageTriggerAction.SCROLL
        or (
            request.scroll_max_segments is None
            and request.horizontal_scroll_max_segments is None
        )
        or not request.capture_screenshot
        or (
            request.horizontal_scroll_max_segments is not None
            and not request.selector
        )
    ):
        raise ValueError("独立滚动截图只允许绑定有段数上限的 single_trigger scroll")
    if request.horizontal_scroll_max_segments is not None and (
            request.mode != PageCaptureMode.SINGLE_TRIGGER
            or _normalize_trigger_action(request.trigger_action) != PageTriggerAction.SCROLL
            or not request.selector
            or request.capture_screenshot is not True
            or request.split_scroll_screenshots is not True
    ):
        raise BlockedPageCaptureActionError(
            "横向分屏采集只允许绑定有 selector 的 single_trigger scroll，且必须逐屏截图"
        )
    if request.verified_card_activation not in {
        None,
        "post_card",
        "video_item",
        "comic_item",
    }:
        raise ValueError("verified_card_activation 不是受支持的卡片激活模式")
    visual_fields = _visual_coordinate_fields(request)
    has_visual_coordinate = any(value is not None for value in visual_fields.values())
    has_visual_navigation_coordinate = request._visual_navigation_target is not None
    if has_visual_navigation_coordinate and not isinstance(
        request._visual_navigation_target,
        VisualNavigationRuntimeTarget,
    ):
        raise BlockedPageCaptureActionError(
            "视觉导航 Runtime 目标无效",
            error_code="VISUAL_TARGET_COORDINATE_INVALID",
        )
    if request.capture_transient_messages and request.mode != PageCaptureMode.SINGLE_TRIGGER:
        raise BlockedPageCaptureActionError("瞬态提示观察器只允许绑定 single_trigger 动作窗口")
    if request.capture_ad_landing_evidence:
        if request.navigation_depth is None or not request.capture_screenshot:
            raise BlockedPageCaptureActionError(
                "广告落地页证据要求单次直开、显式 navigation_depth 和截图",
                error_code="WEB_AD_PAGE_OPEN_FAILED",
            )
    elif request.ad_landing_url is not None:
        raise BlockedPageCaptureActionError("ad_landing_url 仅允许用于广告落地页证据窗口")
    if request.clear_obstructions:
        action = _normalize_trigger_action(request.trigger_action)
        if (
            request.mode != PageCaptureMode.SINGLE_TRIGGER
            or action != PageTriggerAction.CLICK
            or not request.selector
            or not _request_has_trusted_dismissal_intent(request)
        ):
            raise BlockedPageCaptureActionError(
                "弹窗链清障只允许绑定到可信的 single_trigger click 关闭动作",
                error_code=(
                    "H5_POPUP_DISMISSAL_INTENT_INVALID"
                    if (request.session_id or "").startswith("h5:")
                    else "PAGE_CAPTURE_ACTION_BLOCKED"
                ),
            )
    if request.verified_card_activation is not None:
        action = _normalize_trigger_action(request.trigger_action)
        if (
            request.mode != PageCaptureMode.SINGLE_TRIGGER
            or action != PageTriggerAction.CLICK
            or not request.selector
        ):
            raise BlockedPageCaptureActionError(
                "验签卡片激活只允许绑定到有 selector 的 single_trigger click"
            )
    if request.mode != PageCaptureMode.SINGLE_TRIGGER and (
        request.probe_text is not None
        or request.input_text is not None
        or request.submit_selector is not None
        or request.fault_url_pattern is not None
        or request.seek_fraction is not None
        or request.seek_time_seconds is not None
        or has_visual_coordinate
        or has_visual_navigation_coordinate
    ):
        raise BlockedPageCaptureActionError("输入、探针提交和网络故障参数仅允许用于 single_trigger")
    if request.mode == PageCaptureMode.SINGLE_TRIGGER:
        action = _normalize_trigger_action(request.trigger_action)
        if action not in set(PageTriggerAction):
            raise BlockedPageCaptureActionError(
                "单次触发仅允许 click、back、scroll、hover、fill、refresh、"
                "seek_media、network_fault 或 submit_probe，"
                f"拒绝动作：{request.trigger_action}。动作和目标必须分开传递，"
                "例如 trigger_action='click', selector='text=关闭'"
            )
        if has_visual_coordinate:
            if action != PageTriggerAction.CLICK:
                raise BlockedPageCaptureActionError("视觉坐标只允许用于 single_trigger click")
            if request.selector is not None:
                raise BlockedPageCaptureActionError("视觉坐标与 selector 必须严格二选一")
            if any(value is None for value in visual_fields.values()):
                raise BlockedPageCaptureActionError("视觉坐标字段必须完整提供")
            _validate_visual_coordinate_request(request)
        if has_visual_navigation_coordinate:
            if action != PageTriggerAction.CLICK:
                raise BlockedPageCaptureActionError(
                    "视觉导航坐标只允许用于 single_trigger click",
                    error_code="VISUAL_TARGET_COORDINATE_INVALID",
                )
            if request.selector is not None or has_visual_coordinate:
                raise BlockedPageCaptureActionError(
                    "视觉导航坐标与 selector、旧视觉坐标必须严格隔离",
                    error_code="VISUAL_TARGET_COORDINATE_INVALID",
                )
            _validate_visual_navigation_coordinate_request(request)
        if (
            action in {PageTriggerAction.CLICK, PageTriggerAction.HOVER}
            and not request.selector
            and not has_visual_coordinate
            and not has_visual_navigation_coordinate
        ):
            raise BlockedPageCaptureActionError(
                f"{action.value} 触发必须单独提供 selector，"
                f"例如 trigger_action='{action.value}', selector='text=关闭'"
            )
        if action == PageTriggerAction.SEEK_MEDIA:
            if not request.selector:
                raise BlockedPageCaptureActionError(
                    "seek_media 必须提供由 Runtime 解析的进度条语义目标"
                )
            _validate_seek_target(request)
        elif request.seek_fraction is not None or request.seek_time_seconds is not None:
            raise BlockedPageCaptureActionError(
                "seek_fraction 与 seek_time_seconds 仅允许用于 seek_media"
            )
        if action == PageTriggerAction.FILL:
            if not request.selector:
                raise BlockedPageCaptureActionError("fill 触发必须提供 selector")
            if not isinstance(request.input_text, str) or not request.input_text:
                raise BlockedPageCaptureActionError("fill 触发必须提供非空 input_text")
            if len(request.input_text) > MAX_INPUT_TEXT_LENGTH:
                raise BlockedPageCaptureActionError(
                    f"input_text 最多允许 {MAX_INPUT_TEXT_LENGTH} 字符"
                )
        elif request.input_text is not None:
            raise BlockedPageCaptureActionError("input_text 仅允许用于 fill")
        if action == PageTriggerAction.SUBMIT_PROBE:
            if request.navigation_depth is not None:
                raise BlockedPageCaptureActionError(
                    "submit_probe 不允许声明跨页导航，提交后必须留在当前页面"
                )
            if not request.selector or not request.submit_selector:
                raise BlockedPageCaptureActionError(
                    "submit_probe 必须分别提供输入框 selector 与 submit_selector"
                )
            if (
                not isinstance(request.probe_text, str)
                or len(request.probe_text) > MAX_PROBE_TEXT_LENGTH
                or SAFE_PROBE_TEXT_PATTERN.fullmatch(request.probe_text) is None
            ):
                raise BlockedPageCaptureActionError(
                    "submit_probe 仅允许 ALIOTH_RATE_PROBE_ 或 "
                    "ALIOTH_COMMENT_PROBE_ 前缀的有界中性文本"
                )
        elif request.probe_text is not None or request.submit_selector is not None:
            raise BlockedPageCaptureActionError(
                "probe_text 与 submit_selector 仅允许用于 submit_probe"
            )
        if action == PageTriggerAction.NETWORK_FAULT:
            pattern = request.fault_url_pattern
            if not isinstance(pattern, str) or not pattern.strip():
                raise BlockedPageCaptureActionError(
                    "network_fault 必须提供只匹配目标资源的 fault_url_pattern"
                )
            if len(pattern) > MAX_FAULT_URL_PATTERN_LENGTH:
                raise BlockedPageCaptureActionError(
                    f"fault_url_pattern 最多允许 {MAX_FAULT_URL_PATTERN_LENGTH} 字符"
                )
            try:
                re.compile(pattern)
            except re.error as exc:
                raise BlockedPageCaptureActionError(
                    f"fault_url_pattern 不是有效正则：{exc}"
                ) from exc
            if _is_unscoped_fault_pattern(pattern):
                raise BlockedPageCaptureActionError(
                    "fault_url_pattern 不得匹配全部请求，必须限定广告资源域名或路径"
                )
        elif request.fault_url_pattern is not None:
            raise BlockedPageCaptureActionError("fault_url_pattern 仅允许用于 network_fault")
    if request.mode == PageCaptureMode.TIMED_CAPTURE:
        if isinstance(request.frame_count, bool) or not isinstance(request.frame_count, int):
            raise ValueError("timed_capture 的 frame_count 必须为整数")
        if not 1 <= request.frame_count <= MAX_TIMED_CAPTURE_FRAMES:
            raise ValueError(
                f"timed_capture 的 frame_count 必须在 1 到 {MAX_TIMED_CAPTURE_FRAMES} 之间"
            )
    if isinstance(request.interval_ms, bool) or not isinstance(request.interval_ms, int):
        raise ValueError("interval_ms 必须为整数")
    if not 0 <= request.interval_ms <= MAX_TIMED_CAPTURE_INTERVAL_MS:
        raise ValueError(f"interval_ms 必须在 0 到 {MAX_TIMED_CAPTURE_INTERVAL_MS} 之间")


def _visual_coordinate_fields(request: PageCaptureRequest) -> dict[str, object | None]:
    return {
        "click_x_ratio": request.click_x_ratio,
        "click_y_ratio": request.click_y_ratio,
        "left_ratio": request.left_ratio,
        "top_ratio": request.top_ratio,
        "right_ratio": request.right_ratio,
        "bottom_ratio": request.bottom_ratio,
        "visual_confidence": request.visual_confidence,
        "visual_source_url": request.visual_source_url,
        "visual_entry_kind": request.visual_entry_kind,
        "visual_expected_text": request.visual_expected_text,
        "screenshot_index": request.screenshot_index,
    }


def _validate_visual_coordinate_request(request: PageCaptureRequest) -> None:
    ratios = (
        request.click_x_ratio,
        request.click_y_ratio,
        request.left_ratio,
        request.top_ratio,
        request.right_ratio,
        request.bottom_ratio,
        request.visual_confidence,
    )
    if any(
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(float(value))
        or not 0 <= float(value) <= 1
        for value in ratios
    ):
        raise BlockedPageCaptureActionError("视觉坐标比例或置信度无效")
    assert request.click_x_ratio is not None
    assert request.click_y_ratio is not None
    assert request.left_ratio is not None
    assert request.top_ratio is not None
    assert request.right_ratio is not None
    assert request.bottom_ratio is not None
    assert request.visual_confidence is not None
    if (
        request.left_ratio >= request.right_ratio
        or request.top_ratio >= request.bottom_ratio
        or request.visual_confidence < 0.80
        or (request.right_ratio - request.left_ratio) * (request.bottom_ratio - request.top_ratio)
        > 0.35
    ):
        raise BlockedPageCaptureActionError("视觉坐标矩形或置信度无效")
    if not math.isclose(
        request.click_x_ratio,
        (request.left_ratio + request.right_ratio) / 2,
    ) or not math.isclose(
        request.click_y_ratio,
        (request.top_ratio + request.bottom_ratio) / 2,
    ):
        raise BlockedPageCaptureActionError("视觉坐标点击点必须位于受信矩形中心")
    contract = h5_profile_visual_entry_contract(
        request.visual_expected_text,
        entry_kind=request.visual_entry_kind,
    )
    if contract is None:
        raise BlockedPageCaptureActionError("视觉坐标目标文字无效")
    if request.visual_confidence < contract.minimum_confidence or (
        (request.right_ratio - request.left_ratio) * (request.bottom_ratio - request.top_ratio)
        > contract.maximum_area_ratio
    ):
        raise BlockedPageCaptureActionError("视觉坐标矩形或置信度无效")
    if (
        not isinstance(request.screenshot_index, int)
        or isinstance(request.screenshot_index, bool)
        or request.screenshot_index < 1
    ):
        raise BlockedPageCaptureActionError("视觉坐标截图编号无效")
    if (
        not isinstance(request.visual_source_url, str)
        or not request.visual_source_url
        or request.visual_source_url != request.visual_source_url.strip()
    ):
        raise BlockedPageCaptureActionError("视觉坐标来源 URL 无效")
    parsed_source_url = urlsplit(request.visual_source_url)
    if (
        parsed_source_url.scheme.casefold() not in {"http", "https"}
        or not parsed_source_url.hostname
        or parsed_source_url.username is not None
        or parsed_source_url.password is not None
    ):
        raise BlockedPageCaptureActionError("视觉坐标来源 URL 无效")
    try:
        _ = parsed_source_url.port
    except ValueError as exc:
        raise BlockedPageCaptureActionError("视觉坐标来源 URL 无效") from exc


def _validate_visual_navigation_coordinate_request(
    request: PageCaptureRequest,
) -> None:
    """验证进程内视觉导航目标，保持与旧会员视觉坐标分支完全隔离。"""
    target = request._visual_navigation_target
    if not isinstance(target, VisualNavigationRuntimeTarget):
        raise BlockedPageCaptureActionError(
            "视觉导航 Runtime 目标缺失",
            error_code="VISUAL_TARGET_COORDINATE_INVALID",
        )
    if request.full_page is not False or request.capture_screenshot is not True:
        raise BlockedPageCaptureActionError(
            "视觉导航点击只允许使用视口截图",
            error_code="VISUAL_TARGET_COORDINATE_INVALID",
        )
    if not isinstance(target.source_png, bytes) or not target.source_png:
        raise BlockedPageCaptureActionError(
            "视觉导航来源帧无效",
            error_code="VISUAL_TARGET_COORDINATE_INVALID",
        )
    expected_sha256 = getattr(target.capability, "source_artifact_sha256", None)
    actual_sha256 = f"sha256:{hashlib.sha256(target.source_png).hexdigest()}"
    if (
        not isinstance(expected_sha256, str)
        or target.source_image_sha256 != expected_sha256
        or actual_sha256 != expected_sha256
    ):
        raise BlockedPageCaptureActionError(
            "视觉导航来源帧已经失效",
            error_code="VISUAL_TARGET_SOURCE_STATE_STALE",
        )


def _validate_seek_target(request: PageCaptureRequest) -> None:
    """校验受控拖动目标，避免任意坐标和媒体末端动作。"""
    if (request.seek_fraction is None) == (request.seek_time_seconds is None):
        raise BlockedPageCaptureActionError(
            "seek_media 必须且只能提供 seek_fraction 或 seek_time_seconds"
        )
    if request.seek_fraction is not None:
        value = request.seek_fraction
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not 0.05 <= float(value) <= 0.95
        ):
            raise BlockedPageCaptureActionError("seek_fraction 必须在 0.05 到 0.95 之间")
    if request.seek_time_seconds is not None:
        value = request.seek_time_seconds
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not 0 < float(value) <= 86_400
        ):
            raise BlockedPageCaptureActionError("seek_time_seconds 必须在 0 到 86400 秒之间")


def _normalize_trigger_action(
    action: str | PageTriggerAction | None,
) -> PageTriggerAction | None:
    if action is None:
        return None
    if isinstance(action, PageTriggerAction):
        return action
    try:
        return PageTriggerAction(str(action))
    except ValueError:
        return None


def _is_unscoped_fault_pattern(pattern: str) -> bool:
    """拒绝明显会覆盖全部网络请求的故障正则。"""
    compact = re.sub(r"\s+", "", pattern)
    if compact in {".*", ".+", "^.*$", "^.+$", "(?s).*", "(?s).+"}:
        return True
    compiled = re.compile(pattern)
    unrelated_urls = (
        "https://example.com/",
        "https://unrelated.invalid/resource",
        "data:text/plain,alioth",
    )
    return sum(compiled.search(url) is not None for url in unrelated_urls) >= 2


def _attach_observers(
    page: Any,
    console_logs: list[dict[str, Any]],
    network_requests: list[dict[str, Any]],
    network_inflight: dict[int, dict[str, str]] | None = None,
    *,
    is_muted: Callable[[], bool] | None = None,
    suppressed_request_ids: set[int] | None = None,
) -> None:
    def muted() -> bool:
        return bool(is_muted is not None and is_muted())

    def on_console(message: Any) -> None:
        if muted():
            return
        console_logs.append(
            {
                "type": message.type,
                "text": message.text,
            }
        )

    page.on(
        "console",
        on_console,
    )
    inflight = network_inflight if network_inflight is not None else {}
    suppressed = suppressed_request_ids if suppressed_request_ids is not None else set()

    def on_request(request: Any) -> None:
        if muted():
            suppressed.add(id(request))
            return
        inflight[id(request)] = {
            "url": str(request.url),
            "resource_type": str(request.resource_type),
        }
        network_requests.append(
            {
                "event": "request",
                "method": request.method,
                "url": request.url,
                "resource_type": request.resource_type,
            }
        )

    def on_response(response: Any) -> None:
        request = getattr(response, "request", None)
        if muted() or (request is not None and id(request) in suppressed):
            return
        network_requests.append(
            {
                "event": "response",
                "url": response.url,
                "status": response.status,
            }
        )

    def on_request_finished(request: Any) -> None:
        inflight.pop(id(request), None)
        suppressed.discard(id(request))

    def on_request_failed(request: Any) -> None:
        inflight.pop(id(request), None)
        was_suppressed = id(request) in suppressed
        suppressed.discard(id(request))
        if muted() or was_suppressed:
            return
        network_requests.append(
            {
                "event": "request_failed",
                "url": request.url,
                "resource_type": request.resource_type,
            }
        )

    page.on("request", on_request)
    page.on("response", on_response)
    page.on("requestfinished", on_request_finished)
    page.on("requestfailed", on_request_failed)


def _clear_session_observer_buffers(session: _PlaywrightSession) -> None:
    """清除秘密窗口前后可能携带表单值的非持久化观察缓冲。"""
    session.console_logs.clear()
    session.network_requests.clear()
    session.network_inflight.clear()


def _mute_session_observers(session: _PlaywrightSession) -> None:
    """秘密填写开始前静默浏览器观察器，并丢弃既有内存缓冲。"""
    session.observers_muted = True
    _clear_session_observer_buffers(session)


def _browser_viewport_options(request: PageCaptureRequest) -> dict[str, Any]:
    """仅在任务明确提供合法视口时覆盖 Playwright 默认值。"""
    width = request.viewport_width
    height = request.viewport_height
    if not isinstance(width, int) or not isinstance(height, int):
        return {}
    if width < 240 or height < 320 or width > 3840 or height > 3840:
        return {}
    return {"viewport": {"width": width, "height": height}}


def _browser_context_options(request: PageCaptureRequest) -> dict[str, Any]:
    """构造 Browser Context 参数；未提供设备语义时保持既有触控默认值。"""

    options: dict[str, Any] = {
        "has_touch": request.has_touch if isinstance(request.has_touch, bool) else True,
        "device_scale_factor": 1.0,
        "is_mobile": request.is_mobile if isinstance(request.is_mobile, bool) else True,
        **_browser_viewport_options(request),
    }
    screen_width = request.screen_width
    screen_height = request.screen_height
    if (
        isinstance(screen_width, int)
        and not isinstance(screen_width, bool)
        and isinstance(screen_height, int)
        and not isinstance(screen_height, bool)
        and 240 <= screen_width <= 3840
        and 320 <= screen_height <= 3840
    ):
        options["screen"] = {"width": screen_width, "height": screen_height}
    scale = request.device_scale_factor
    if (
        isinstance(scale, int | float)
        and not isinstance(scale, bool)
        and 0.1 <= float(scale) <= 4.0
    ):
        options["device_scale_factor"] = float(scale)
    if request.color_scheme in {"light", "dark", "no-preference"}:
        options["color_scheme"] = request.color_scheme
    return options


async def _run_isolated_ad_goto(
    *,
    page: Any,
    context: Any,
    request: PageCaptureRequest,
    action_trace: list[dict[str, Any]],
    timeout_ms: int,
) -> dict[str, Any]:
    """用全新隔离 Page 执行广告 URL 导航，源业务页只读且保持原位。"""
    from services.api.app.application.web_ad_outbound_policy import AdOutboundDenied
    from services.api.app.infrastructure.browser.ad_isolated_page import isolated_ad_page

    destination = str(request.url or "")
    before_url = str(getattr(page, "url", "") or "")
    web_script_render = request.web_ad_javascript is True or request.web_capture_stage is not None
    render_mode = "javascript_render" if web_script_render else "static_render"

    async def web_ad_snapshot(ad_page):
        return await _collect_ui_snapshot(ad_page, web_detail_candidate_priority=True)

    action_trace.append(
        {
            "action": "navigate",
            "navigation_mode": "isolated_context_goto",
            "transport_mode": "isolated_safe_transport",
            "render_mode": render_mode,
            "destination_url": _url_without_query_or_fragment(destination),
        }
    )
    web_ad_diagnostics = [] if web_script_render else None
    if web_ad_diagnostics is not None:
        action_trace[-1]["outbound_hops"] = web_ad_diagnostics
    try:
        ad_user_agent = (
            await web_chrome_user_agent(context.browser)
            if request.startup_policy == "direct_home"
            else None
        )
        async with isolated_ad_page(
            browser=context.browser,
            url=destination,
            source_url=before_url,
            timeout_ms=timeout_ms,
            snapshot_collector=web_ad_snapshot if web_script_render else _collect_ui_snapshot,
            **({"enable_javascript": True} if web_script_render else {}),
            **({"diagnostic_events": web_ad_diagnostics} if web_ad_diagnostics is not None else {}),
            **({"user_agent": ad_user_agent} if ad_user_agent is not None else {}),
        ) as (new_page, transport):
            observation = await _capture_ad_landing_observation(
                page=new_page,
                index=1,
                static_render=True,
            )
            observation.update(
                transport_mode="isolated_safe_transport",
                render_mode=render_mode,
                outbound_warning_codes=sorted(transport.denied_reasons),
            )
            landing_url = str(new_page.url)
    except AdOutboundDenied as exc:
        if web_ad_diagnostics is not None:
            action_trace[-1]["source_page_unchanged"] = str(page.url) == before_url
        raise BlockedPageCaptureActionError(
            "广告安全出站策略拒绝本次观察",
            error_code=exc.reason,
        ) from None
    action_trace[-1].update(
        {
            "result": "opened_new_page",
            "navigation": {
                "before_url": _url_without_query_or_fragment(before_url),
                "after_url": _url_without_query_or_fragment(str(page.url)),
                "current_url_changed": str(page.url) != before_url,
                "new_page_count": 1,
                "new_page_urls": [_url_without_query_or_fragment(landing_url)],
                "closed_after_observation_count": 1,
                "isolated_new_page_count": 1,
                "isolated_closed_after_observation_count": 1,
            },
        }
    )
    return observation


async def _run_single_trigger(
    *,
    page: Any,
    context: Any,
    request: PageCaptureRequest,
    action_trace: list[dict[str, Any]],
    timeout_ms: int,
    timeout_error_type: type[Exception] = TimeoutError,
) -> dict[str, Any] | None:
    _validate_ad_capture_request(request)
    action = _normalize_trigger_action(request.trigger_action)
    if action is None:
        return

    if (
        request._cli_session is not None
        and action != PageTriggerAction.DIRECT_AD_OUTBOUND
        and request.horizontal_scroll_max_segments is None
        and request._h5_probe_display_contract is None
    ):
        before_url = str(getattr(page, "url", "") or "")
        cli_result = await request._cli_session.trigger(page=page, request=request)
        if cli_result is not None:
            action_trace.append(cli_result)
            if action == PageTriggerAction.BACK:
                _validate_browser_history_return(
                    before_url=before_url,
                    after_url=str(getattr(page, "url", "") or ""),
                )
            return

    action_trace.append(
        {
            "action": action.value,
            "selector": request.selector,
            **(
                {
                    "navigation_family": request.navigation_family,
                    "navigation_index": request.navigation_index,
                    "navigation_label": request.navigation_label,
                }
                if request.navigation_family is not None
                else {}
            ),
            **(
                {"interaction_pattern_signature": request.interaction_pattern_signature}
                if request.interaction_pattern_signature is not None
                else {}
            ),
        }
    )
    if request._h5_probe_display_contract is not None:
        from services.api.app.infrastructure.browser.h5_probe_display_execution import (
            execute_display_probe,
        )

        await execute_display_probe(page=page, request=request, action_trace=action_trace,
                                    timeout_ms=timeout_ms)
        return
    if action == PageTriggerAction.DIRECT_AD_OUTBOUND:
        from services.api.app.application.web_ad_outbound_policy import AdOutboundDenied
        from services.api.app.infrastructure.browser.ad_isolated_page import isolated_ad_page

        destination = str(request.ad_landing_url or "")
        before_url = str(getattr(page, "url", "") or "")
        try:
            ad_user_agent = (
                await web_chrome_user_agent(context.browser)
                if request.startup_policy == "direct_home"
                else None
            )
            async with isolated_ad_page(
                browser=context.browser,
                url=destination,
                source_url=before_url,
                timeout_ms=timeout_ms,
                snapshot_collector=_collect_ui_snapshot,
                **({"user_agent": ad_user_agent} if ad_user_agent is not None else {}),
            ) as (new_page, transport):
                observation = await _capture_ad_landing_observation(
                    page=new_page,
                    index=1,
                    static_render=True,
                )
                observation.update(
                    transport_mode="isolated_safe_transport",
                    render_mode="static_render",
                    outbound_warning_codes=sorted(transport.denied_reasons),
                )
                landing_url = str(new_page.url)
        except AdOutboundDenied as exc:
            raise BlockedPageCaptureActionError(
                "广告安全出站策略拒绝本次观察",
                error_code=exc.reason,
            ) from None
        action_trace[-1].update(
            {
                "destination_url": _url_without_query_or_fragment(destination),
                "result": "opened_new_page",
                "transport_mode": "isolated_safe_transport",
                "render_mode": "static_render",
                "navigation": {
                    "before_url": _url_without_query_or_fragment(before_url),
                    "after_url": _url_without_query_or_fragment(str(page.url)),
                    "current_url_changed": str(page.url) != before_url,
                    "new_page_count": 1,
                    "new_page_urls": [_url_without_query_or_fragment(landing_url)],
                    "closed_after_observation_count": 1,
                },
            }
        )
        return observation
    if action == PageTriggerAction.CLICK:
        if request._visual_navigation_target is not None:
            await _run_validated_visual_navigation_coordinate_click(
                page=page,
                context=context,
                request=request,
                action_trace=action_trace,
                timeout_ms=timeout_ms,
            )
            return
        if any(value is not None for value in _visual_coordinate_fields(request).values()):
            await _run_validated_visual_coordinate_click(
                page=page,
                request=request,
                action_trace=action_trace,
            )
            return
        if not request.selector:
            raise ValueError("click 触发必须提供 selector 或受信视觉坐标")
        flutter_startup_ad = request.selector == FLUTTER_FULLSCREEN_STARTUP_AD_DISMISSAL_SELECTOR
        flutter_bottom_navigation = h5_match_flutter_bottom_navigation_runtime_selector(
            request.selector
        )
        css_bottom_navigation = h5_match_bottom_navigation_target(request.selector)
        flutter_profile_login_entry = request.selector == H5_FLUTTER_PROFILE_LOGIN_RUNTIME_SELECTOR
        popup_target = None
        prefer_safe_popup_outside = (
            request.h5_popup_dismissal_strategy == "safe_outside_then_close"
        )
        if (
            str(request.session_id or "").startswith("h5:")
            and request.selector.startswith("internal:rendered_modal_close:")
        ):
            # 已签发唯一安全关闭键时优先原生点击，避免遮罩先关闭导致跨层复用旧证明。
            prefer_safe_popup_outside = False
        if (
            prefer_safe_popup_outside
            and str(request.session_id or "").startswith("h5:")
            and len(action_trace) > 1
            and action_trace[-2].get("requested_selector") == request.selector
            and action_trace[-2].get("dismissal_kind") == "overlay"
            and action_trace[-2].get("popup_dismissal_outcome") == "next_popup_layer_visible"
        ):
            # 外侧点击后同一关闭合同又出现，下一层改用现场关闭键，不循环点蒙版。
            prefer_safe_popup_outside = False
            action_trace[-1]["repeated_outside_strategy_skipped"] = True
        if request.selector == ".van-overlay:visible" and request.allow_high_z_obstruction_hide:
            # 高层 Dialog 候选已在来源 Capture 中验签。动作时仍能证明当前
            # 顶层 Dialog 存在，就必须先关闭它，不能等待其下方开屏壳退出。
            popup_target = await _resolve_popup_dismissal_target(
                page=page,
                selector=request.selector,
                **({"prefer_safe_outside": True} if prefer_safe_popup_outside else {}),
            )
        if request.selector == ".van-overlay:visible" and not (
            popup_target and popup_target.get("dialog_present")
        ):
            loading_layer_still_visible = await _wait_for_loading_layer_before_overlay_click(
                page=page,
                timeout_ms=timeout_ms,
                action_trace=action_trace,
                timeout_error_type=timeout_error_type,
            )
            if loading_layer_still_visible:
                action_trace[-1]["result"] = "loading_layer_still_visible"
                raise BlockedPageCaptureActionError(
                    "顶层加载或开屏广告仍然可见，禁止点击被遮挡的底层弹窗。",
                    error_code="TOPMOST_OBSTRUCTION_STILL_VISIBLE",
                )
            popup_target = None
        if (
            popup_target is None
            and request.selector not in STARTUP_AD_LAYER_SELECTOR_BY_DISMISSAL
            and not flutter_startup_ad
            and flutter_bottom_navigation is None
            and css_bottom_navigation is None
            and not flutter_profile_login_entry
        ):
            popup_target = await _resolve_popup_dismissal_target(
                page=page,
                selector=request.selector,
                **({"prefer_safe_outside": True} if prefer_safe_popup_outside else {}),
            )
        signed_popup_structure_missing = bool(
            request.allow_high_z_obstruction_hide
            and request.selector not in STARTUP_AD_LAYER_SELECTOR_BY_DISMISSAL
            and not flutter_startup_ad
            and _selector_has_dismissal_intent(request.selector)
            and not _popup_dismissal_target_is_executable(popup_target)
        )
        if signed_popup_structure_missing:
            popup_target, reacquire_attempts = await _reacquire_signed_popup_dismissal_target(
                page=page,
                selector=request.selector,
                timeout_ms=timeout_ms,
                prefer_safe_outside=prefer_safe_popup_outside,
            )
            if reacquire_attempts:
                action_trace[-1]["popup_structure_reacquire_attempts"] = reacquire_attempts
            if _popup_dismissal_target_is_executable(popup_target):
                action_trace[-1]["popup_structure_reacquired"] = True
        if (
            request.allow_high_z_obstruction_hide
            and request.selector not in STARTUP_AD_LAYER_SELECTOR_BY_DISMISSAL
            and not flutter_startup_ad
            and _selector_has_dismissal_intent(request.selector)
            and not _popup_dismissal_target_is_executable(popup_target)
        ):
            # Explorer 来源快照已对弹窗候选验签，但动作时的实时 DOM 未再次
            # 证明顶层 Dialog，必须重观察；Main Agent 的显式遮罩点击保留原契约。
            action_trace[-1]["result"] = "popup_dismissal_target_not_proven"
            raise BlockedPageCaptureActionError(
                "动作时未再次证明可关闭的顶层弹窗，请重新观察当前页面。",
                error_code="POPUP_DISMISSAL_NOT_FOUND",
            )
        interactive_target = (
            None
            if request.selector in STARTUP_AD_LAYER_SELECTOR_BY_DISMISSAL
            or flutter_startup_ad
            or flutter_bottom_navigation is not None
            or css_bottom_navigation is not None
            or flutter_profile_login_entry
            or (popup_target and popup_target.get("dialog_present"))
            else await _resolve_exact_interactive_text_target(
                page=page,
                selector=request.selector,
            )
        )
        if popup_target and popup_target.get("dialog_present"):
            resolved_selector = str(popup_target.get("selector") or "").strip()
            dismissal_kind = str(popup_target.get("kind") or "")
            if not resolved_selector or dismissal_kind not in {
                "close_control",
                "overlay",
            }:
                action_trace[-1]["result"] = "popup_dismissal_target_not_proven"
                raise BlockedPageCaptureActionError(
                    "实时顶层弹窗未证明可执行的关闭控件，请重新观察当前页面。",
                    error_code="POPUP_DISMISSAL_NOT_FOUND",
                )
            action_trace[-1].update(
                {
                    "requested_selector": request.selector,
                    "selector": resolved_selector,
                    "resolved_by": "visible_popup_structure",
                    "dismissal_kind": dismissal_kind,
                }
            )
            if (
                str(request.session_id or "").startswith("h5:")
                and popup_target.get("verification_required") is True
                and not popup_target.get("dialog_layer_shape")
            ):
                from services.api.app.infrastructure.browser.h5_modal_dismissal_target import (
                    bind_modal_layer,
                )

                # H5 的旧结构兜底也须验证真实弹层，不能凭动态文字变化确认关闭。
                bound_target = await bind_modal_layer(page, popup_target)
                if bound_target is None:
                    raise BlockedPageCaptureActionError(
                        "无法完整绑定当前 H5 弹层结构，禁止执行未证明的关闭动作。",
                        error_code="POPUP_DISMISSAL_NOT_FOUND",
                    )
                popup_target = bound_target
            locator = page.locator(resolved_selector)
            if (str(request.session_id or "").startswith("h5:")
                and dismissal_kind == "close_control"
                and popup_target.get("pointer_click_required") is True):
                from services.api.app.infrastructure.browser.h5_modal_dismissal_target import (
                    sample_modal_close_geometry,
                )
                action_trace[-1]["popup_hit_geometry"] = await sample_modal_close_geometry(
                    page, popup_target,
                )
            if dismissal_kind == "overlay":
                position = popup_target.get("position")
                if not isinstance(position, dict):
                    action_trace[-1]["result"] = "popup_overlay_outside_point_not_proven"
                    return
                action_trace[-1]["position"] = position
                await locator.click(position=position, timeout=timeout_ms)
            else:
                if popup_target.get("pointer_click_required") is True:
                    # 广告弹层只接受真实指针可达的原生点击；禁止 element.click()
                    # 绕过更高层登录、年龄门或其他安全遮挡。
                    await locator.click(timeout=timeout_ms)
                    used_direct_dispatch = False
                else:
                    used_direct_dispatch = await _click_popup_close_control_with_offscreen_fallback(
                        page=page,
                        locator=locator,
                        timeout_ms=timeout_ms,
                        timeout_error_type=timeout_error_type,
                    )
                if used_direct_dispatch:
                    action_trace[-1]["resolved_by"] = (
                        "popup_close_direct_dispatch_after_actionability_timeout"
                    )
            if popup_target.get("verification_required") is True:
                dismissal_verified = await _wait_for_popup_dismissal(
                    page=page,
                    popup_target=popup_target,
                    timeout_ms=timeout_ms,
                    timeout_error_type=timeout_error_type,
                )
                action_trace[-1]["dismissal_verified"] = dismissal_verified
                if prefer_safe_popup_outside and dismissal_kind == "overlay":
                    safe_outside_attempts = [
                        {
                            "position": position,
                            "dismissal_verified": dismissal_verified,
                        }
                    ]
                    raw_positions = popup_target.get("positions")
                    remaining_positions = (
                        [item for item in raw_positions if isinstance(item, dict)]
                        if isinstance(raw_positions, list)
                        else []
                    )
                    for safe_position in remaining_positions[:4]:
                        if dismissal_verified:
                            break
                        if safe_position == position:
                            continue
                        await locator.click(position=safe_position, timeout=timeout_ms)
                        dismissal_verified = await _wait_for_popup_dismissal(
                            page=page,
                            popup_target=popup_target,
                            timeout_ms=timeout_ms,
                            timeout_error_type=timeout_error_type,
                        )
                        safe_outside_attempts.append(
                            {
                                "position": safe_position,
                                "dismissal_verified": dismissal_verified,
                            }
                        )
                    action_trace[-1]["safe_outside_attempts"] = safe_outside_attempts
                    action_trace[-1]["dismissal_verified"] = dismissal_verified
                fallback_close_target = popup_target.get("fallback_close_target")
                if (
                    not dismissal_verified
                    and prefer_safe_popup_outside
                    and dismissal_kind == "overlay"
                    and isinstance(fallback_close_target, Mapping)
                ):
                    fallback_selector = str(
                        fallback_close_target.get("selector") or ""
                    ).strip()
                    if fallback_selector:
                        if fallback_close_target.get("pointer_click_required") is True:
                            await page.locator(fallback_selector).click(timeout=timeout_ms)
                            used_direct_dispatch = False
                        else:
                            used_direct_dispatch = (
                                await _click_popup_close_control_with_offscreen_fallback(
                                    page=page,
                                    locator=page.locator(fallback_selector),
                                    timeout_ms=timeout_ms,
                                    timeout_error_type=timeout_error_type,
                                )
                            )
                        action_trace[-1].update(
                            {
                                "fallback": "visible_close_control",
                                "fallback_selector": fallback_selector,
                                "fallback_resolved_by": (
                                    "direct_dispatch_after_actionability_timeout"
                                    if used_direct_dispatch
                                    else "visible_popup_structure"
                                ),
                            }
                        )
                        dismissal_verified = await _wait_for_popup_dismissal(
                            page=page,
                            popup_target=popup_target,
                            timeout_ms=timeout_ms,
                            timeout_error_type=timeout_error_type,
                        )
                        action_trace[-1]["dismissal_verified"] = dismissal_verified
                if (
                    not dismissal_verified
                    and request.allow_high_z_obstruction_hide
                    and not prefer_safe_popup_outside
                    and popup_target.get("pointer_click_required") is not True
                ):
                    fallback = await _hide_verified_high_z_popup_obstruction(
                        page=page,
                        popup_target=popup_target,
                    )
                    if fallback.get("hidden") is True:
                        action_trace[-1].update(
                            {
                                "fallback": "verified_high_z_obstruction_hide",
                                "fallback_min_z_index": 500,
                                "fallback_hidden_count": fallback.get("hidden_count", 0),
                                "fallback_max_z_index": fallback.get("max_z_index"),
                            }
                        )
                        dismissal_verified = await _wait_for_popup_dismissal(
                            page=page,
                            popup_target=popup_target,
                            timeout_ms=timeout_ms,
                            timeout_error_type=timeout_error_type,
                        )
                        action_trace[-1]["dismissal_verified"] = dismissal_verified
                action_trace[-1][
                    "popup_dismissal_outcome"
                ] = await _observe_popup_dismissal_outcome(
                    page=page,
                    popup_target=popup_target,
                )
                if not dismissal_verified:
                    action_trace[-1]["result"] = "popup_dismissal_not_observed"
                    error = BlockedPageCaptureActionError(
                        "关闭动作已点击，但原弹窗仍然可见，禁止标记为已关闭。",
                        error_code="POPUP_DISMISSAL_NOT_OBSERVED",
                    )
                    if str(request.session_id or "").startswith("h5:"):
                        # 仅执行器私有传递已点击事实；不授权再次点击或改变动作终态。
                        error._h5_popup_action_trace = [{
                            key: value for key, value in action_trace[-1].items()
                            if key in {"action", "selector", "requested_selector", "result",
                                       "resolved_by", "dismissal_kind", "dismissal_verified",
                                       "popup_dismissal_outcome", "popup_hit_geometry"}
                        }]
                    raise error
        elif interactive_target and interactive_target.get("matched"):
            resolved_selector = str(interactive_target.get("selector") or "").strip()
            if not resolved_selector:
                action_trace[-1]["result"] = "interactive_text_target_not_unique"
                return
            action_trace[-1].update(
                {
                    "requested_selector": request.selector,
                    "selector": resolved_selector,
                    "resolved_by": "exact_visible_interactive_text",
                }
            )
            if not interactive_target.get("actionable"):
                action_trace[-1]["result"] = "interactive_target_obstructed"
                obstruction = interactive_target.get("obstruction")
                if isinstance(obstruction, dict):
                    action_trace[-1]["obstruction"] = obstruction
                raise PageInteractionFailedError(
                    _click_target_failure_message(
                        selector=request.selector,
                        error_code="CLICK_TARGET_OBSTRUCTED",
                        diagnostics=interactive_target,
                    ),
                    error_code="CLICK_TARGET_OBSTRUCTED",
                    retryable=True,
                )
            await page.locator(resolved_selector).click(timeout=timeout_ms)
        elif request.selector == ".van-overlay:visible":
            if not await _selector_has_visible_match(page, request.selector):
                action_trace[-1]["result"] = "overlay_absent_after_loading_transition"
                return
            # 遮罩中心常被对话框覆盖；点击左上角保留真实的遮罩点击语义，
            # 不使用 force，也不绕过页面自身的关闭策略。
            action_trace[-1]["position"] = {"x": 1, "y": 1}
            used_direct_dispatch = await _click_overlay_with_pointer_interception_fallback(
                locator=page.locator(request.selector),
                position={"x": 1, "y": 1},
                timeout_ms=timeout_ms,
                timeout_error_type=timeout_error_type,
            )
            if used_direct_dispatch:
                action_trace[-1]["resolved_by"] = (
                    "overlay_direct_dispatch_after_pointer_interception"
                )
                dismissal_verified = await _wait_for_visible_selector_to_hide(
                    page=page,
                    selector=request.selector,
                    timeout_ms=timeout_ms,
                    timeout_error_type=timeout_error_type,
                )
                action_trace[-1]["dismissal_verified"] = dismissal_verified
                if not dismissal_verified:
                    raise BlockedPageCaptureActionError(
                        "遮挡层子元素截获指针后已派发容器关闭事件，但遮挡层仍然可见。",
                        error_code="POPUP_DISMISSAL_NOT_OBSERVED",
                    )
        elif flutter_startup_ad:
            flutter_target = await _resolve_flutter_fullscreen_startup_skip_target(page)
            resolved_selector = str(flutter_target.get("selector") or "").strip()
            if flutter_target.get("match_count") != 1 or not resolved_selector:
                action_trace[-1]["result"] = "flutter_startup_ad_skip_not_proven"
                raise BlockedPageCaptureActionError(
                    "当前页面未唯一证明 Flutter 全屏开屏层右上角跳过控件。",
                    error_code="STARTUP_AD_DISMISSAL_NOT_FOUND",
                )
            action_trace[-1].update(
                {
                    "requested_selector": request.selector,
                    "selector": resolved_selector,
                    "resolved_by": "flutter_fullscreen_startup_structure",
                }
            )
            await page.locator(resolved_selector).click(timeout=timeout_ms)
        elif request.selector in STARTUP_AD_LAYER_SELECTOR_BY_DISMISSAL:
            await _click_startup_ad_with_pointer_interception_fallback(
                page=page,
                selector=request.selector,
                timeout_ms=timeout_ms,
                action_trace=action_trace,
                timeout_error_type=timeout_error_type,
            )
        elif flutter_bottom_navigation is not None:
            await _click_flutter_bottom_navigation(
                page=page,
                navigation_index=flutter_bottom_navigation[0],
                label_hash=flutter_bottom_navigation[1],
                timeout_ms=timeout_ms,
                timeout_error_type=timeout_error_type,
                action_trace=action_trace,
            )
        elif flutter_profile_login_entry:
            await _click_flutter_profile_login_entry(
                page=page,
                timeout_ms=timeout_ms,
                timeout_error_type=timeout_error_type,
                action_trace=action_trace,
            )
        else:
            await _click_with_bottom_navigation_fallback(
                page=page,
                selector=request.selector,
                timeout_ms=timeout_ms,
                action_trace=action_trace,
                timeout_error_type=timeout_error_type,
                navigation_family=request.navigation_family,
                navigation_label=request.navigation_label,
                verified_card_activation=request.verified_card_activation,
                expected_rect=request.expected_rect,
            )
        if request.selector in STARTUP_AD_LAYER_SELECTOR_BY_DISMISSAL or flutter_startup_ad:
            startup_ad_dismissed = (
                await _wait_for_flutter_fullscreen_startup_skip_to_hide(
                    page=page,
                    timeout_ms=timeout_ms,
                )
                if flutter_startup_ad
                else await _wait_for_visible_selector_to_hide(
                    page=page,
                    selector=STARTUP_AD_LAYER_SELECTOR_BY_DISMISSAL[request.selector],
                    timeout_ms=timeout_ms,
                    timeout_error_type=timeout_error_type,
                    verification_timeout_ms=(STARTUP_AD_DISMISSAL_VERIFICATION_TIMEOUT_MS),
                    stable_absence_ms=(
                        800 if request.selector == POPUP_SKIP_WRAP_DISMISSAL_SELECTOR else 0
                    ),
                )
            )
            if (
                not startup_ad_dismissed
                and not flutter_startup_ad
                and str(request.session_id or "").startswith("h5:")
                and await _retry_h5_startup_ad_direct_dispatch(
                    page=page,
                    selector=request.selector,
                    timeout_ms=timeout_ms,
                    timeout_error_type=timeout_error_type,
                    action_trace=action_trace,
                )
            ):
                startup_ad_dismissed = True
            action_trace[-1]["dismissal_verified"] = startup_ad_dismissed
            if not startup_ad_dismissed:
                action_trace[-1]["result"] = "startup_ad_dismissal_not_observed"
                raise BlockedPageCaptureActionError(
                    "开屏关闭动作已点击，但顶层开屏广告仍然可见，禁止标记为已关闭。",
                    error_code="STARTUP_AD_DISMISSAL_NOT_OBSERVED",
                )
    elif action == PageTriggerAction.BACK:
        if request.navigation_depth is None or request.navigation_depth <= 0:
            raise BlockedPageCaptureActionError(
                "浏览器历史返回只允许用于已声明来源深度的详情页恢复；同页弹窗关闭不得使用 back。"
            )
        before_url = str(getattr(page, "url", "") or "")
        await page.go_back(wait_until="domcontentloaded", timeout=timeout_ms)
        _validate_browser_history_return(
            before_url=before_url,
            after_url=str(getattr(page, "url", "") or ""),
        )
    elif action == PageTriggerAction.HOVER:
        if not request.selector:
            raise ValueError("hover 触发必须提供 selector")
        await page.hover(request.selector, timeout=timeout_ms)
    elif action == PageTriggerAction.SEEK_MEDIA:
        await _seek_media_progress(
            page=page,
            request=request,
            trace=action_trace[-1],
            timeout_ms=timeout_ms,
        )
    elif action == PageTriggerAction.FILL:
        if not request.selector or request.input_text is None:
            raise ValueError("fill 触发缺少输入框或输入内容")
        trace = action_trace[-1]
        trace.update(
            {
                "input_text_length": len(request.input_text),
                "input_text_sha256": hashlib.sha256(request.input_text.encode("utf-8")).hexdigest(),
            }
        )
        await page.locator(request.selector).fill(request.input_text, timeout=timeout_ms)
        trace["result"] = "input_filled"
    elif action == PageTriggerAction.REFRESH:
        await page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
    elif action == PageTriggerAction.SCROLL:
        if request.horizontal_scroll_max_segments is not None:
            if not request.selector:
                raise ValueError("横向分屏采集必须提供 selector")
            dispatch_mode = await page.locator(request.selector).evaluate(
                """
                element => {
                  const swiper = element.swiper;
                  const trustedSwiper = element.classList.contains("swiper-horizontal")
                    && swiper
                    && !swiper.destroyed
                    && typeof swiper.slideNext === "function";
                  if (trustedSwiper) {
                    swiper.slideNext(0);
                    return "swiper_slide_next";
                  }
                  const distance = Math.max(element.clientWidth || 1, 1) * 0.85;
                  element.scrollBy({left: distance, behavior: "auto"});
                  return "native_scroll";
                }
                """
            )
            action_trace[-1]["result"] = "horizontal_collection_scroll_dispatched"
            action_trace[-1]["horizontal_dispatch_mode"] = (
                dispatch_mode if isinstance(dispatch_mode, str) else "unknown"
            )
        elif request.selector:
            locator = page.locator(request.selector)
            scroll_into_view = getattr(locator, "scroll_into_view_if_needed", None)
            if callable(scroll_into_view):
                await scroll_into_view(timeout=timeout_ms)
            else:
                await locator.evaluate(
                    "element => element.scrollIntoView({block: 'center', inline: 'nearest'})"
                )
            action_trace[-1]["result"] = "primary_content_scrolled_into_view"
        else:
            await page.evaluate(
                """
                () => {
              const candidates = [...document.querySelectorAll("*")].filter((element) => {
                const style = getComputedStyle(element);
                return /(auto|scroll)/.test(style.overflowY)
                  && element.scrollHeight > element.clientHeight + 1
                  && __H5_SCROLL_CONTAINER_VISIBLE__;
              });
              const prioritized = __H5_SCROLL_CONTAINER_PRIORITY__;
              const container = prioritized.sort(
                (left, right) => (right.clientWidth * right.clientHeight)
                  - (left.clientWidth * left.clientHeight),
              )[0] || document.scrollingElement || document.documentElement;
              const distance = Math.max(container.clientHeight || window.innerHeight || 600, 1)
                * 0.85;
              container.scrollBy({ top: distance, behavior: "auto" });
                }
                """.replace(
                    "__H5_SCROLL_CONTAINER_VISIBLE__",
                    _H5_SCROLL_CONTAINER_VISIBLE_JS
                    if str(request.session_id or "").startswith("h5:") else "true",
                ).replace(
                    "__H5_SCROLL_CONTAINER_PRIORITY__",
                    _H5_SCROLL_CONTAINER_PRIORITY_JS
                    if str(request.session_id or "").startswith("h5:") else "candidates",
                )
            )
    elif action == PageTriggerAction.SUBMIT_PROBE:
        if not request.selector or not request.submit_selector or not request.probe_text:
            raise ValueError("submit_probe 缺少输入框、提交按钮或中性探针文本")
        trace = action_trace[-1]
        trace.update(
            {
                "submit_selector": request.submit_selector,
                "probe_text_length": len(request.probe_text),
                "probe_text_sha256": hashlib.sha256(request.probe_text.encode("utf-8")).hexdigest(),
            }
        )
        await page.locator(request.selector).fill(request.probe_text, timeout=timeout_ms)
        await page.locator(request.submit_selector).click(timeout=timeout_ms)
        trace["result"] = "probe_submitted"
    elif action == PageTriggerAction.NETWORK_FAULT:
        if not request.fault_url_pattern:
            raise ValueError("network_fault 缺少 fault_url_pattern")
        pattern = re.compile(request.fault_url_pattern)
        matched_urls: list[str] = []
        matched_count = 0

        async def abort_matching_route(route: Any) -> None:
            nonlocal matched_count
            request_url = str(getattr(getattr(route, "request", None), "url", "") or "")
            if pattern.search(request_url) is None:
                await route.continue_()
                return
            matched_count += 1
            if len(matched_urls) < MAX_FAULT_MATCH_DETAILS:
                matched_urls.append(_url_without_query_or_fragment(request_url)[:1000])
            await route.abort()

        trace = action_trace[-1]
        trace.update(
            {
                "fault_url_pattern_length": len(request.fault_url_pattern),
                "fault_url_pattern_sha256": hashlib.sha256(
                    request.fault_url_pattern.encode("utf-8")
                ).hexdigest(),
            }
        )
        await context.route("**/*", abort_matching_route)
        try:
            await page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception as exc:  # noqa: BLE001
            trace["reload_error"] = str(exc)[:500]
        finally:
            await context.unroute("**/*", abort_matching_route)
        trace.update(
            {
                "result": "network_fault_injected",
                "matched_request_count": matched_count,
                "matched_request_urls": matched_urls,
                "matched_requests_truncated": matched_count > len(matched_urls),
                "route_restored": True,
            }
        )


async def _run_validated_visual_navigation_coordinate_click(
    *,
    page: Any,
    context: Any,
    request: PageCaptureRequest,
    action_trace: list[dict[str, Any]],
    timeout_ms: int,
) -> None:
    """以 CDP 隔离世代和紧邻双帧复验能力，成功后只点击一次。"""
    _validate_visual_navigation_coordinate_request(request)
    target = request._visual_navigation_target
    assert isinstance(target, VisualNavigationRuntimeTarget)
    tracker = await _start_visual_navigation_cdp_tracker(
        context=context,
        page=page,
        observer_ttl_ms=_visual_navigation_observer_ttl_ms(timeout_ms),
    )
    second_resolution: VisualCoordinateResolution | None = None
    click_commit: _VisualNavigationClickCommit | None = None
    try:
        baseline_state = await _sample_visual_navigation_preclick_state(tracker)
        protected_snapshot = await _collect_ui_snapshot(
            page,
            logical_surface_enabled=request.logical_surface_enabled,
            logical_surface_shadow=request.logical_surface_shadow,
            surface_session_id=request.session_id,
            defer_logical_surface=True,
        )
        post_snapshot_state = await _sample_visual_navigation_preclick_state(tracker)
        _assert_visual_navigation_preclick_state_stable(
            baseline=baseline_state,
            current=post_snapshot_state,
        )
        first_png = await page.screenshot(full_page=False, type="png")
        action_trace[-1]["preclick_frame_origin"] = {
            **await read_h5_frame_origin(page, tracker=tracker),
            "screenshot_sha256": hashlib.sha256(first_png).hexdigest(),
        }
        first_state = await _sample_visual_navigation_preclick_state(tracker)
        _assert_visual_navigation_preclick_state_stable(
            baseline=baseline_state,
            current=first_state,
        )
        first_resolution = resolve_visual_navigation_coordinate(
            source_png=target.source_png,
            current_png=first_png,
            capability=target.capability,
            current_viewport=first_state["viewport"],
        )
        _validate_visual_navigation_resolution(first_resolution)

        # 第二帧仍是点击前验签材料，不是动作后的页面到达证明。
        second_png = await page.screenshot(full_page=False, type="png")
        second_state = await _sample_visual_navigation_preclick_state(tracker)
        _assert_visual_navigation_preclick_state_stable(
            baseline=baseline_state,
            current=second_state,
        )
        second_resolution = resolve_visual_navigation_coordinate(
            source_png=target.source_png,
            current_png=second_png,
            capability=target.capability,
            current_viewport=second_state["viewport"],
        )
        _validate_visual_navigation_resolution(second_resolution)
        frame_stability = resolve_visual_navigation_coordinate(
            source_png=first_png,
            current_png=second_png,
            capability=target.capability,
            current_viewport=second_state["viewport"],
        )
        _validate_visual_navigation_resolution(frame_stability)

        # 最终隔离世代读取与坐标断言之后不再发任何 CDP 调用，直接 click。
        final_state = await _sample_visual_navigation_preclick_state(tracker)
        _assert_visual_navigation_preclick_state_stable(
            baseline=baseline_state,
            current=final_state,
        )
        if (
            first_resolution.css_x != second_resolution.css_x
            or first_resolution.css_y != second_resolution.css_y
        ):
            raise BlockedPageCaptureActionError(
                "视觉导航双帧坐标不稳定",
                error_code="VISUAL_TARGET_SOURCE_STATE_STALE",
            )
        request._visual_navigation_frame_context.bind(
            _VerifiedVisualNavigationBeforeFrame(
                viewport_png=second_png,
                ui_snapshot=protected_snapshot,
                viewport=_safe_visual_navigation_viewport(second_state["viewport"]),
                source_image_sha256=target.source_image_sha256,
            )
        )
        assert second_resolution.css_x is not None
        assert second_resolution.css_y is not None
        await page.mouse.click(second_resolution.css_x, second_resolution.css_y)
        click_commit = _commit_visual_navigation_click(
            request,
            resolved_by="validated_visual_navigation_coordinate",
            visual_target_relocated=second_resolution.relocated,
            visual_match_score=second_resolution.match_score,
            visual_audit_codes=tuple(second_resolution.audit_codes),
        )
    finally:
        pending_control_flow = sys.exc_info()[1]
        try:
            await _close_visual_navigation_cdp_tracker(tracker)
        except (asyncio.CancelledError, SystemExit, KeyboardInterrupt):
            request._visual_navigation_frame_context.clear()
            if click_commit is not None:
                _log_visual_navigation_postclick_cleanup(
                    click_commit,
                    message="视觉点击已完成，隔离 tracker 清理被控制流中断",
                )
            raise
        except Exception:  # noqa: BLE001 - 点击后普通清理错误不得诱发重试
            if click_commit is not None:
                _log_visual_navigation_postclick_cleanup(
                    click_commit,
                    message="视觉点击已完成，隔离 tracker 清理未完整结束",
                )
            elif pending_control_flow is None:
                raise
        finally:
            if click_commit is None:
                request._visual_navigation_frame_context.clear()

    assert click_commit is not None
    action_trace[-1].update(
        {
            "resolved_by": click_commit.resolved_by,
            "visual_target_verified": True,
            "visual_target_relocated": click_commit.visual_target_relocated,
            "visual_match_score": click_commit.visual_match_score,
            "visual_audit_codes": list(click_commit.visual_audit_codes),
        }
    )


def _safe_visual_navigation_viewport(value: Mapping[str, Any]) -> dict[str, float]:
    """只保留转换复验所需的有限数值 viewport 事实。"""
    fields = (
        "width",
        "height",
        "offset_left",
        "offset_top",
        "page_left",
        "page_top",
        "scale",
        "device_pixel_ratio",
    )
    return {field: float(value[field]) for field in fields}


def _commit_visual_navigation_click(
    request: PageCaptureRequest,
    *,
    resolved_by: str,
    visual_target_relocated: bool,
    visual_match_score: float,
    visual_audit_codes: tuple[str, ...],
) -> _VisualNavigationClickCommit:
    """在首次 cleanup await 前，把脱敏提交事实原子写入请求私有槽。"""
    commit = _VisualNavigationClickCommit(
        click_committed=True,
        resolved_by=resolved_by,
        visual_target_relocated=visual_target_relocated,
        visual_match_score=visual_match_score,
        visual_audit_codes=visual_audit_codes,
    )
    request._visual_action_commit_holder.commit(commit)
    return commit


def _visual_action_was_committed(request: PageCaptureRequest) -> bool:
    """只返回动作是否已提交，不把提交材料投影到公开结果。"""
    commit = request._visual_action_commit_holder.commit_record
    return commit is not None and commit.click_committed is True


def _log_visual_navigation_postclick_cleanup(
    commit: _VisualNavigationClickCommit,
    *,
    message: str,
) -> None:
    logger.warning(
        message,
        extra={
            "click_committed": commit.click_committed,
            "resolved_by": commit.resolved_by,
            "visual_match_score": commit.visual_match_score,
            "visual_audit_codes": commit.visual_audit_codes,
        },
    )


_VISUAL_NAVIGATION_OBSERVER_TTL_SAFETY_MARGIN_MS = 5_000
_VISUAL_NAVIGATION_OBSERVER_TTL_MAX_MS = 305_000
_VISUAL_NAVIGATION_CLEANUP_STEP_TIMEOUT_SECONDS = 1.0


def _visual_navigation_observer_ttl_ms(action_timeout_ms: int) -> int:
    """按有效动作墙钟预算派生单次 sample 间隔 TTL，并限制异常超大请求。"""
    if (
        not isinstance(action_timeout_ms, int)
        or isinstance(action_timeout_ms, bool)
        or action_timeout_ms <= 0
    ):
        raise _visual_navigation_stale_error()
    return min(
        action_timeout_ms + _VISUAL_NAVIGATION_OBSERVER_TTL_SAFETY_MARGIN_MS,
        _VISUAL_NAVIGATION_OBSERVER_TTL_MAX_MS,
    )


_VISUAL_NAVIGATION_ISOLATED_TRACKER_EXPRESSION = r"""
(() => {
  const state = { dom_mutation_generation: 0 };
  let disposed = false;
  let expired = false;
  let expiryTimer = null;
  let expiresAtMs = null;
  const observer = new MutationObserver(() => {
    state.dom_mutation_generation += 1;
  });
  const dispose = (dueToExpiry = false) => {
    if (disposed) return;
    disposed = true;
    expired = dueToExpiry;
    observer.disconnect();
    if (expiryTimer !== null) clearTimeout(expiryTimer);
  };
  const armExpiry = () => {
    if (disposed) return;
    if (expiryTimer !== null) clearTimeout(expiryTimer);
    expiresAtMs = performance.now() + __ALIOTH_OBSERVER_TTL_MS__;
    expiryTimer = setTimeout(() => dispose(true), __ALIOTH_OBSERVER_TTL_MS__);
  };
  observer.observe(document, {
    subtree: true,
    childList: true,
    attributes: true,
    characterData: true,
  });
  armExpiry();
  const keyedDigest = (material) => {
    const hashes = [
      0x811c9dc5, 0x9e3779b9, 0x85ebca6b, 0xc2b2ae35,
      0x27d4eb2f, 0x165667b1, 0xd3a2646c, 0xfd7046c5,
    ];
    const keyed = "__ALIOTH_TRACKER_NONCE__:" + material;
    for (let index = 0; index < keyed.length; index += 1) {
      const code = keyed.charCodeAt(index);
      for (let slot = 0; slot < hashes.length; slot += 1) {
        hashes[slot] = Math.imul(
          (hashes[slot] ^ code ^ (slot * 131)) >>> 0,
          (0x01000193 + slot * 2) >>> 0,
        ) >>> 0;
      }
    }
    return `v1:${hashes
      .map((value) => value.toString(16).padStart(8, "0"))
      .join("")}`;
  };
  const boundedStateMaterial = (root) => {
    const limits = {
      maxDepth: 16,
      maxNodes: 4096,
      maxEntries: 4096,
      maxScalars: 8192,
      maxContainerEntries: 1024,
      maxStringLength: 4096,
      maxByteLength: 4096,
      maxOutputBytes: 131072,
    };
    const seen = new Map();
    const work = { nodes: 0, entries: 0, scalars: 0, outputBytes: 0 };
    let complete = true;
    const bounded = (kind, size = null) => ["bounded", kind, size];
    const consume = (field, amount, limit) => {
      if (!Number.isSafeInteger(amount) || amount < 0 || work[field] + amount > limit) {
        complete = false;
        return false;
      }
      work[field] += amount;
      return true;
    };
    const consumeOutput = (amount) => consume(
      "outputBytes",
      amount,
      limits.maxOutputBytes,
    );
    const consumeScalar = (outputBytes = 24) => (
      consume("scalars", 1, limits.maxScalars) && consumeOutput(outputBytes)
    );
    const includeString = (value) => {
      if (value.length > limits.maxStringLength) {
        complete = false;
        return false;
      }
      // JSON 控制字符最坏会展开为六个 ASCII 字节，先按最坏值占预算。
      return consumeScalar(24 + value.length * 6);
    };
    const startObject = (value, depth, tag) => {
      if (depth >= limits.maxDepth) {
        complete = false;
        return null;
      }
      if (
        !includeString(tag)
        || !consume("nodes", 1, limits.maxNodes)
        || !consumeOutput(32)
      ) {
        return null;
      }
      const objectId = work.nodes - 1;
      seen.set(value, objectId);
      return objectId;
    };
    const includeEntry = () => consume("entries", 1, limits.maxEntries);
    const encode = (value, depth) => {
      if (value === null) {
        return consumeScalar(8) ? ["null"] : bounded("scalar");
      }
      const type = typeof value;
      if (type === "string") {
        return includeString(value)
          ? ["string", value]
          : bounded("string", value.length);
      }
      if (type === "boolean" || type === "undefined") {
        return consumeScalar(16) ? [type, value] : bounded("scalar");
      }
      if (type === "number") {
        if (!consumeScalar(32)) return bounded("number");
        if (Number.isNaN(value)) return ["number", "NaN"];
        if (!Number.isFinite(value)) return ["number", String(value)];
        if (Object.is(value, -0)) return ["number", "-0"];
        return ["number", value];
      }
      if (type === "bigint") {
        complete = false;
        return bounded("bigint");
      }
      if (type !== "object") {
        complete = false;
        return bounded(type);
      }
      if (seen.has(value)) {
        return consumeScalar(24)
          ? ["ref", seen.get(value)]
          : bounded("ref");
      }
      const tag = Object.prototype.toString.call(value);
      const objectId = startObject(value, depth, tag);
      if (objectId === null) return bounded("object");
      if (Array.isArray(value)) {
        const length = value.length;
        if (
          !Number.isSafeInteger(length)
          || length < 0
          || length > limits.maxContainerEntries
          || work.entries + length > limits.maxEntries
        ) {
          complete = false;
          return bounded("array", length);
        }
        const items = [];
        for (let index = 0; index < length; index += 1) {
          if (!includeEntry()) break;
          items.push(encode(value[index], depth + 1));
          if (!complete) break;
        }
        return ["array", objectId, length, items];
      }
      if (tag === "[object Date]") {
        return consumeScalar(32)
          ? ["date", objectId, value.getTime()]
          : bounded("date");
      }
      if (tag === "[object Map]") {
        const size = value.size;
        if (
          !Number.isSafeInteger(size)
          || size < 0
          || size > limits.maxContainerEntries
          || work.entries + size > limits.maxEntries
        ) {
          complete = false;
          return bounded("map", size);
        }
        const entries = [];
        const iterator = value.entries();
        let count = 0;
        while (count <= size) {
          const step = iterator.next();
          if (step.done) break;
          if (count >= size || !includeEntry()) {
            complete = false;
            break;
          }
          const pair = step.value;
          if (!Array.isArray(pair) || pair.length < 2) {
            complete = false;
            break;
          }
          entries.push([
            encode(pair[0], depth + 1),
            encode(pair[1], depth + 1),
          ]);
          count += 1;
          if (!complete) break;
        }
        if (count !== size) complete = false;
        return ["map", objectId, size, entries];
      }
      if (tag === "[object Set]") {
        const size = value.size;
        if (
          !Number.isSafeInteger(size)
          || size < 0
          || size > limits.maxContainerEntries
          || work.entries + size > limits.maxEntries
        ) {
          complete = false;
          return bounded("set", size);
        }
        const items = [];
        const iterator = value.values();
        let count = 0;
        while (count <= size) {
          const step = iterator.next();
          if (step.done) break;
          if (count >= size || !includeEntry()) {
            complete = false;
            break;
          }
          items.push(encode(step.value, depth + 1));
          count += 1;
          if (!complete) break;
        }
        if (count !== size) complete = false;
        return ["set", objectId, size, items];
      }
      if (tag === "[object ArrayBuffer]" || ArrayBuffer.isView(value)) {
        const byteLength = value.byteLength;
        if (
          !Number.isSafeInteger(byteLength)
          || byteLength < 0
          || byteLength > limits.maxByteLength
          || work.scalars + byteLength > limits.maxScalars
        ) {
          complete = false;
          return bounded("bytes", byteLength);
        }
        const bytes = tag === "[object ArrayBuffer]"
          ? new Uint8Array(value)
          : new Uint8Array(value.buffer, value.byteOffset, byteLength);
        const items = [];
        for (let index = 0; index < byteLength; index += 1) {
          if (!consumeScalar(8)) break;
          items.push(bytes[index]);
        }
        return ["bytes", objectId, byteLength, items];
      }
      const entries = [];
      let entryCount = 0;
      for (const key in value) {
        if (
          entryCount >= limits.maxContainerEntries
          || !includeEntry()
        ) {
          complete = false;
          break;
        }
        entryCount += 1;
        if (!Object.prototype.hasOwnProperty.call(value, key)) continue;
        if (!includeString(key)) break;
        entries.push([key, encode(value[key], depth + 1)]);
        if (!complete) break;
      }
      return ["object", objectId, tag, entries];
    };
    const encoded = encode(root, 0);
    let material = JSON.stringify(encoded);
    if (material.length * 3 > limits.maxOutputBytes) {
      complete = false;
      material = JSON.stringify(bounded("output", material.length));
    }
    return { material, complete };
  };
  const historyStateDigest = () => {
    let material;
    let complete = true;
    try {
      const summary = boundedStateMaterial(__ALIOTH_HISTORY_STATE__);
      material = summary.material;
      complete = summary.complete;
    } catch {
      material = "unserializable";
      complete = false;
    }
    return { digest: keyedDigest(material), complete };
  };
  return Object.freeze({
    sample: async () => {
      if (
        !disposed
        && Number.isFinite(expiresAtMs)
        && performance.now() >= expiresAtMs
      ) {
        dispose(true);
      }
      const visualViewport = window.visualViewport;
      const round = (value) => Number.isFinite(value)
        ? Math.round(value * 100) / 100
        : null;
      const historySummary = historyStateDigest();
      const observerActive = !disposed;
      if (observerActive) armExpiry();
      return {
        url: window.location.href,
        time_origin_ms: round(performance.timeOrigin),
        history_length: history.length,
        history_state_digest: historySummary.digest,
        history_state_complete: historySummary.complete,
        dom_mutation_generation: state.dom_mutation_generation,
        observer_active: observerActive,
        expired,
        viewport: {
          layout_width: round(window.innerWidth),
          layout_height: round(window.innerHeight),
          width: round(visualViewport ? visualViewport.width : window.innerWidth),
          height: round(visualViewport ? visualViewport.height : window.innerHeight),
          offset_left: round(visualViewport ? visualViewport.offsetLeft : 0),
          offset_top: round(visualViewport ? visualViewport.offsetTop : 0),
          page_left: round(visualViewport ? visualViewport.pageLeft : window.scrollX),
          page_top: round(visualViewport ? visualViewport.pageTop : window.scrollY),
          scale: round(visualViewport ? visualViewport.scale : 1),
          device_pixel_ratio: round(window.devicePixelRatio),
          full_page: false,
        },
      };
    },
    dispose,
  });
})()
"""


@dataclass(slots=True)
class _VisualNavigationCdpTracker:
    """单次视觉点击私有的 CDP 隔离 world tracker。"""

    session: Any
    frame_id: str
    loader_id: str
    execution_context_id: int
    object_id: str


async def _start_visual_navigation_cdp_tracker(
    *,
    context: Any,
    page: Any,
    observer_ttl_ms: int,
) -> _VisualNavigationCdpTracker:
    """创建页面脚本不可见的隔离 world 与对象句柄。"""
    new_cdp_session = getattr(context, "new_cdp_session", None)
    if not callable(new_cdp_session):
        raise _visual_navigation_stale_error()
    session = None
    try:
        session = await new_cdp_session(page)
        frame_id, loader_id, _ = await _read_visual_navigation_main_frame(session)
        world = await session.send(
            "Page.createIsolatedWorld",
            {
                "frameId": frame_id,
                "worldName": "alioth_visual_navigation_preclick",
                "grantUniveralAccess": False,
            },
        )
        execution_context_id = world.get("executionContextId")
        if isinstance(execution_context_id, bool) or not isinstance(execution_context_id, int):
            raise ValueError("CDP 隔离执行上下文无效")
        created = await session.send(
            "Runtime.evaluate",
            {
                "expression": (
                    _VISUAL_NAVIGATION_ISOLATED_TRACKER_EXPRESSION.replace(
                        "__ALIOTH_TRACKER_NONCE__",
                        secrets.token_hex(32),
                    )
                    .replace("__ALIOTH_HISTORY_STATE__", "history.state")
                    .replace(
                        "__ALIOTH_OBSERVER_TTL_MS__",
                        str(observer_ttl_ms),
                    )
                ),
                "contextId": execution_context_id,
                "returnByValue": False,
                "awaitPromise": True,
                "silent": True,
            },
        )
        result = created.get("result")
        if created.get("exceptionDetails") or not isinstance(result, Mapping):
            raise ValueError("CDP 隔离 tracker 创建失败")
        object_id = result.get("objectId")
        if not isinstance(object_id, str) or not object_id:
            raise ValueError("CDP 隔离 tracker 句柄无效")
        return _VisualNavigationCdpTracker(
            session=session,
            frame_id=frame_id,
            loader_id=loader_id,
            execution_context_id=execution_context_id,
            object_id=object_id,
        )
    except asyncio.CancelledError:
        if session is not None:
            await _detach_visual_navigation_cdp_session_best_effort(session)
        raise
    except Exception:  # noqa: BLE001 - CDP 私有错误统一映射稳定码
        if session is not None:
            await _detach_visual_navigation_cdp_session_best_effort(session)
        raise _visual_navigation_stale_error() from None


async def _sample_visual_navigation_preclick_state(
    tracker: _VisualNavigationCdpTracker,
) -> Mapping[str, Any]:
    """以可信 frame/loader 和隔离对象句柄采集一次动作前状态。"""
    try:
        frame_id, loader_id, frame_url = await _read_visual_navigation_main_frame(tracker.session)
        if frame_id != tracker.frame_id or loader_id != tracker.loader_id:
            raise ValueError("主 frame 或 loader 已换代")
        response = await tracker.session.send(
            "Runtime.callFunctionOn",
            {
                "functionDeclaration": "async function () { return await this.sample(); }",
                "objectId": tracker.object_id,
                "returnByValue": True,
                "awaitPromise": True,
                "silent": True,
            },
        )
        result = response.get("result")
        if response.get("exceptionDetails") or not isinstance(result, Mapping):
            raise ValueError("隔离 tracker 采样失败")
        value = result.get("value")
        _validate_visual_navigation_preclick_state(value)
        assert isinstance(value, Mapping)
        if value["url"] != frame_url:
            raise ValueError("隔离 URL 与 CDP frame URL 不一致")
        return value
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - context destroyed 等统一稳定失败
        raise _visual_navigation_stale_error() from None


async def _read_visual_navigation_main_frame(
    session: Any,
) -> tuple[str, str, str]:
    response = await session.send("Page.getFrameTree")
    frame_tree = response.get("frameTree")
    if not isinstance(frame_tree, Mapping):
        raise ValueError("CDP frame tree 无效")
    frame = frame_tree.get("frame")
    if not isinstance(frame, Mapping):
        raise ValueError("CDP main frame 无效")
    frame_id = frame.get("id")
    loader_id = frame.get("loaderId")
    url = frame.get("url")
    if not all(isinstance(item, str) and item for item in (frame_id, loader_id, url)):
        raise ValueError("CDP main frame 身份不完整")
    return frame_id, loader_id, url


async def _close_visual_navigation_cdp_tracker(
    tracker: _VisualNavigationCdpTracker,
) -> None:
    """按 dispose、release、detach 顺序限时清理，并统一脱敏失败。"""
    failure = False
    cancellation: asyncio.CancelledError | None = None
    try:
        disposed = await asyncio.wait_for(
            tracker.session.send(
                "Runtime.callFunctionOn",
                {
                    "functionDeclaration": "function () { this.dispose(); }",
                    "objectId": tracker.object_id,
                    "returnByValue": True,
                    "silent": True,
                },
            ),
            timeout=_VISUAL_NAVIGATION_CLEANUP_STEP_TIMEOUT_SECONDS,
        )
        if disposed.get("exceptionDetails"):
            failure = True
    except asyncio.CancelledError as exc:
        cancellation = exc
    except Exception:  # noqa: BLE001 - observer 释放错误不得泄漏 CDP 内容
        failure = True
    try:
        await asyncio.wait_for(
            tracker.session.send(
                "Runtime.releaseObject",
                {"objectId": tracker.object_id},
            ),
            timeout=_VISUAL_NAVIGATION_CLEANUP_STEP_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError as exc:
        cancellation = cancellation or exc
    except Exception:  # noqa: BLE001 - 释放错误不得泄漏 CDP 内容
        failure = True
    try:
        await asyncio.wait_for(
            tracker.session.detach(),
            timeout=_VISUAL_NAVIGATION_CLEANUP_STEP_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError as exc:
        cancellation = cancellation or exc
    except Exception:  # noqa: BLE001 - detach 错误不得泄漏 CDP 内容
        failure = True
    if cancellation is not None:
        raise cancellation
    if failure:
        raise _visual_navigation_stale_error()


async def _detach_visual_navigation_cdp_session_best_effort(session: Any) -> None:
    try:
        await session.detach()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - 创建失败后的释放只做尽力清理
        return


def _validate_visual_navigation_preclick_state(value: object) -> None:
    if not isinstance(value, Mapping) or not isinstance(value.get("viewport"), Mapping):
        raise ValueError("隔离视觉状态无效")
    required_fields = {
        "url",
        "time_origin_ms",
        "history_length",
        "history_state_digest",
        "history_state_complete",
        "dom_mutation_generation",
        "observer_active",
        "expired",
        "viewport",
    }
    if not required_fields.issubset(value):
        raise ValueError("隔离视觉状态字段不完整")
    viewport = value["viewport"]
    assert isinstance(viewport, Mapping)
    integer_state_fields = ("history_length", "dom_mutation_generation")
    viewport_numeric_fields = (
        "layout_width",
        "layout_height",
        "width",
        "height",
        "offset_left",
        "offset_top",
        "page_left",
        "page_top",
        "scale",
        "device_pixel_ratio",
    )
    state_is_valid = (
        isinstance(value["url"], str)
        and _is_finite_visual_fact(value["time_origin_ms"])
        and isinstance(value["history_state_digest"], str)
        and re.fullmatch(r"v1:[0-9a-f]{64}", value["history_state_digest"]) is not None
        and value["history_state_complete"] is True
        and value["observer_active"] is True
        and value["expired"] is False
        and all(
            isinstance(value[field], int)
            and not isinstance(value[field], bool)
            and value[field] >= 0
            for field in integer_state_fields
        )
        and all(field in viewport for field in viewport_numeric_fields)
        and all(_is_finite_visual_fact(viewport[field]) for field in viewport_numeric_fields)
        and viewport.get("full_page") is False
    )
    if not state_is_valid:
        raise ValueError("隔离视觉状态字段无效")


def _visual_navigation_stale_error() -> BlockedPageCaptureActionError:
    return BlockedPageCaptureActionError(
        "浏览器隔离视觉状态无法稳定复验",
        error_code="VISUAL_TARGET_SOURCE_STATE_STALE",
    )


def _is_finite_visual_fact(value: object) -> bool:
    return bool(
        not isinstance(value, bool)
        and isinstance(value, int | float)
        and math.isfinite(float(value))
    )


def _assert_visual_navigation_preclick_state_stable(
    *,
    baseline: Mapping[str, Any],
    current: Mapping[str, Any],
) -> None:
    """页面世代、History 或完整 viewport 任一变化即作废点击授权。"""
    scalar_fields = (
        "url",
        "time_origin_ms",
        "history_length",
        "history_state_digest",
        "history_state_complete",
        "dom_mutation_generation",
        "observer_active",
        "expired",
    )
    baseline_viewport = baseline.get("viewport")
    current_viewport = current.get("viewport")
    if (
        any(baseline.get(field) != current.get(field) for field in scalar_fields)
        or not isinstance(baseline_viewport, Mapping)
        or not isinstance(current_viewport, Mapping)
        or dict(baseline_viewport) != dict(current_viewport)
    ):
        raise BlockedPageCaptureActionError(
            "视觉导航点击前页面世代或视口已变化",
            error_code="VISUAL_TARGET_SOURCE_STATE_STALE",
        )


def _validate_visual_navigation_resolution(
    resolution: VisualCoordinateResolution,
) -> None:
    """统一消费 Runtime 的脱敏失败码，禁止失败路径触发 click。"""
    blocking_code = next(
        (code for code in resolution.audit_codes if code.startswith("VISUAL_")),
        None,
    )
    if blocking_code is not None:
        raise BlockedPageCaptureActionError(
            "视觉导航目标未通过最新帧安全复验",
            error_code=blocking_code,
        )
    if resolution.css_x is None or resolution.css_y is None or resolution.relocated:
        raise BlockedPageCaptureActionError(
            "视觉导航坐标结果无效",
            error_code="VISUAL_TARGET_COORDINATE_INVALID",
        )


async def _run_validated_visual_coordinate_click(
    *,
    page: Any,
    request: PageCaptureRequest,
    action_trace: list[dict[str, Any]],
) -> None:
    _validate_visual_coordinate_request(request)
    current_url = _normalize_url(page.url)
    source_url = _normalize_url(request.visual_source_url)
    if current_url is None or source_url is None or current_url != source_url:
        raise BlockedPageCaptureActionError(
            "当前页面已离开视觉坐标来源页，拒绝点击过期位置。",
            error_code="VISUAL_SOURCE_PAGE_STALE",
        )
    assert request.click_x_ratio is not None
    assert request.click_y_ratio is not None
    mapping = await page.evaluate(
        r"""
        ({
          xRatio,
          yRatio,
          leftRatio,
          topRatio,
          rightRatio,
          bottomRatio,
          expectedText,
          entryKind,
        }) => {
          const root = document.documentElement;
          const body = document.body;
          const documentWidth = Math.max(
            root?.scrollWidth || 0,
            root?.clientWidth || 0,
            body?.scrollWidth || 0,
            body?.clientWidth || 0,
            window.innerWidth || 0
          );
          const documentHeight = Math.max(
            root?.scrollHeight || 0,
            root?.clientHeight || 0,
            body?.scrollHeight || 0,
            body?.clientHeight || 0,
            window.innerHeight || 0
          );
          const documentX = documentWidth * xRatio;
          const documentY = documentHeight * yRatio;
          const targetScrollX = Math.max(0, documentX - window.innerWidth / 2);
          const targetScrollY = Math.max(0, documentY - window.innerHeight / 2);
          window.scrollTo(targetScrollX, targetScrollY);
          const viewportX = documentX - window.scrollX;
          const viewportY = documentY - window.scrollY;
          const normalize = (value) => String(value || "")
            .normalize("NFKC")
            .replace(/\s+/g, " ")
            .trim()
            .toLocaleLowerCase();
          const normalizedExpected = normalize(expectedText);
          const exactMatch = (element) => {
            if (!element) return false;
            const ariaLabel = normalize(element.getAttribute?.("aria-label"));
            const alt = normalize(element.getAttribute?.("alt"));
            const ownText = normalize(element.innerText || element.textContent);
            return ariaLabel === normalizedExpected
              || alt === normalizedExpected
              || ownText === normalizedExpected;
          };
          const visible = (element) => {
            const style = getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.display !== "none"
              && style.visibility !== "hidden"
              && Number(style.opacity || 1) > 0
              && element.getAttribute?.("aria-disabled") !== "true"
              && !element.disabled
              && rect.width > 0
              && rect.height > 0;
          };
          const classTokens = (element) => new Set(
            String(element?.className || "").split(/\s+/).filter(Boolean)
          );
          const ancestorClassTokens = (element, maximumDepth = 8) => {
            const tokens = new Set();
            let current = element?.parentElement;
            for (let depth = 0; current && depth < maximumDepth; depth += 1) {
              for (const token of classTokens(current)) tokens.add(token);
              current = current.parentElement;
            }
            return tokens;
          };
          const hasAll = (tokens, required) => required.every((token) => tokens.has(token));
          const profileActivityContractFor = (hitElement) => {
            if (entryKind !== "profile_activity_ocr" || !hitElement) return null;
            let current = hitElement;
            for (let depth = 0; current && depth <= 4; depth += 1) {
              if (!visible(current)) {
                current = current.parentElement;
                continue;
              }
              const classes = classTokens(current);
              if (classes.has("checkin-entry")) {
                const label = current.querySelector?.(".checkin-entry-text");
                if (label && exactMatch(label)) {
                  return { element: current, depth, kind: "profile_activity_contract" };
                }
              }
              if (
                classes.has("item")
                && current.parentElement?.classList?.contains("bottom-link")
              ) {
                const label = current.querySelector?.(".name");
                const image = current.querySelector?.("img");
                if ((label && exactMatch(label)) || (image && exactMatch(image))) {
                  return { element: current, depth, kind: "profile_activity_contract" };
                }
              }
              current = current.parentElement;
            }
            return null;
          };
          const profileSettingsContractFor = (hitElement) => {
            if (entryKind !== "profile_settings_image" || !hitElement) return null;
            let current = hitElement;
            for (let depth = 0; current && depth <= 4; depth += 1) {
              const tag = current.tagName?.toLocaleLowerCase() || "";
              if (tag !== "img" || !visible(current)) {
                current = current.parentElement;
                continue;
              }
              const classes = classTokens(current);
              const ancestors = ancestorClassTokens(current);
              const registered = (
                classes.has("setting")
                  && hasAll(ancestors, ["left", "top", "header"])
              ) || (
                classes.has("wh-24")
                  && !classes.has("mr-15")
                  && hasAll(ancestors, ["header-left", "index-header"])
              ) || (
                hasAll(classes, ["icon", "icon-setting"])
                  && hasAll(ancestors, ["top-right-menu", "top-wrap", "base-info"])
              ) || hasAll(ancestors, ["options", "right"]);
              if (registered) {
                return { element: current, depth, kind: "profile_settings_contract" };
              }
              current = current.parentElement;
            }
            return null;
          };
          const profilePaymentContracts = () => {
            const contracts = [];
            for (const root of document.querySelectorAll(".meTabPaymentBanner")) {
              const target = root.querySelector?.(".bannerImg");
              if (visible(root) && target && visible(target)) {
                contracts.push({ root, target, xRatio: 0.82 });
              }
            }
            for (const root of document.querySelectorAll(".vip-img")) {
              const target = root.querySelector?.("img.vip-right-btn");
              if (visible(root) && target && visible(target)) {
                contracts.push({ root, target, xRatio: 0.5 });
              }
            }
            for (const root of document.querySelectorAll(
              ".payment_me_tab.vip_box, .payment_me_tab .vip_box"
            )) {
              const target = root.querySelector?.(".vipBoxsBgImg img, img.vipBoxsBgImg");
              if (visible(root) && target && visible(target)) {
                contracts.push({ root, target, xRatio: 0.82 });
              }
            }
            return contracts.filter((candidate, index, items) => (
              items.findIndex((item) => item.root === candidate.root) === index
            ));
          };
          const profilePaymentContractFor = (hitElement) => {
            if (!hitElement) return null;
            return profilePaymentContracts().find((candidate) => (
              candidate.root === hitElement || candidate.root.contains(hitElement)
            )) || null;
          };
          const matchedLineage = (hitElement) => {
            if (entryKind === "profile_activity_ocr") {
              return profileActivityContractFor(hitElement);
            }
            if (entryKind === "profile_settings_image") {
              return profileSettingsContractFor(hitElement);
            }
            let current = hitElement;
            for (let depth = 0; current && depth <= 4; depth += 1) {
              if (visible(current) && exactMatch(current)) {
                return { element: current, depth, kind: "exact_text" };
              }
              current = current.parentElement;
            }
            if (entryKind !== "profile_vip_payment" || !hitElement) return null;
            const paymentContract = profilePaymentContractFor(hitElement);
            if (!paymentContract) return null;
            let structuralCurrent = hitElement;
            for (let depth = 0; structuralCurrent && depth <= 8; depth += 1) {
              if (structuralCurrent === paymentContract.root) {
                return {
                  element: paymentContract.root,
                  depth,
                  kind: "profile_vip_banner",
                };
              }
              structuralCurrent = structuralCurrent.parentElement;
            }
            return null;
          };
          let hit = document.elementFromPoint(viewportX, viewportY);
          let match = matchedLineage(hit);
          let matchedDepth = null;
          let matchKind = null;
          let hitTag = hit?.tagName?.toLocaleLowerCase() || null;
          let resolvedDocumentX = documentX;
          let resolvedDocumentY = documentY;
          let resolvedViewportX = viewportX;
          let resolvedViewportY = viewportY;
          let targetAdjusted = false;
          if (match) {
            matchedDepth = match.depth;
            matchKind = match.kind;
          } else {
            const expectedBounds = {
              left: documentWidth * leftRatio,
              top: documentHeight * topRatio,
              right: documentWidth * rightRatio,
              bottom: documentHeight * bottomRatio,
            };
            const targetHeight = expectedBounds.bottom - expectedBounds.top;
            const verticalTolerance = Math.max(64, targetHeight * 1.5);
            const nearbyMatches = [];
            const elements = [...document.querySelectorAll("body *")].slice(0, 5000);
            for (const element of elements) {
              if (!visible(element) || !exactMatch(element)) continue;
              const rect = element.getBoundingClientRect();
              const documentRect = {
                left: rect.left + window.scrollX,
                top: rect.top + window.scrollY,
                right: rect.right + window.scrollX,
                bottom: rect.bottom + window.scrollY,
              };
              const horizontalOverlap = Math.min(documentRect.right, expectedBounds.right)
                - Math.max(documentRect.left, expectedBounds.left);
              const verticalGap = Math.max(
                expectedBounds.top - documentRect.bottom,
                documentRect.top - expectedBounds.bottom,
                0,
              );
              if (horizontalOverlap <= 0 || verticalGap > verticalTolerance) continue;
              const centerX = rect.left + rect.width / 2;
              const centerY = rect.top + rect.height / 2;
              if (
                centerX < 0
                || centerY < 0
                || centerX >= window.innerWidth
                || centerY >= window.innerHeight
              ) continue;
              const centerHit = document.elementFromPoint(centerX, centerY);
              if (
                !centerHit
                || !(
                  element === centerHit
                  || element.contains(centerHit)
                  || centerHit.contains(element)
                )
              ) continue;
              const centerMatch = matchedLineage(centerHit);
              if (!centerMatch) continue;
              nearbyMatches.push({
                element,
                centerX,
                centerY,
                area: rect.width * rect.height,
                centerHit,
                centerMatch,
              });
            }
            nearbyMatches.sort((left, right) => left.area - right.area);
            const distinctMatches = [];
            for (const candidate of nearbyMatches) {
              if (
                distinctMatches.some(
                  (existing) => existing.element.contains(candidate.element)
                    || candidate.element.contains(existing.element),
                )
              ) continue;
              distinctMatches.push(candidate);
            }
            if (distinctMatches.length === 1) {
              const corrected = distinctMatches[0];
              resolvedViewportX = corrected.centerX;
              resolvedViewportY = corrected.centerY;
              resolvedDocumentX = corrected.centerX + window.scrollX;
              resolvedDocumentY = corrected.centerY + window.scrollY;
              hit = corrected.centerHit;
              hitTag = hit?.tagName?.toLocaleLowerCase() || null;
              matchedDepth = corrected.centerMatch.depth;
              matchKind = corrected.centerMatch.kind;
              targetAdjusted = true;
            } else if (entryKind === "profile_vip_payment") {
              // 支付横幅的按钮文字绘制在图片内，旧截图坐标可能因倒计时或弹窗
              // 清理产生纵向漂移；仅允许当前页面唯一的严格横幅结构重新定位。
              const paymentBannerMatches = profilePaymentContracts();
              if (paymentBannerMatches.length === 1) {
                const paymentBanner = paymentBannerMatches[0];
                const bannerImage = paymentBanner.target;
                bannerImage.scrollIntoView({ block: "center", inline: "nearest" });
                const rect = bannerImage.getBoundingClientRect();
                const correctedX = rect.left + rect.width * paymentBanner.xRatio;
                const correctedY = rect.top + rect.height / 2;
                const correctedHit = document.elementFromPoint(correctedX, correctedY);
                const correctedMatch = matchedLineage(correctedHit);
                if (correctedMatch?.kind === "profile_vip_banner") {
                  resolvedViewportX = correctedX;
                  resolvedViewportY = correctedY;
                  resolvedDocumentX = correctedX + window.scrollX;
                  resolvedDocumentY = correctedY + window.scrollY;
                  hit = correctedHit;
                  hitTag = hit?.tagName?.toLocaleLowerCase() || null;
                  matchedDepth = correctedMatch.depth;
                  matchKind = correctedMatch.kind;
                  targetAdjusted = true;
                }
              }
            }
          }
          return {
            x: resolvedViewportX,
            y: resolvedViewportY,
            document_x: resolvedDocumentX,
            document_y: resolvedDocumentY,
            scroll_x: window.scrollX,
            scroll_y: window.scrollY,
            viewport_width: window.innerWidth,
            viewport_height: window.innerHeight,
            target_matched: matchedDepth !== null,
            target_adjusted: targetAdjusted,
            matched_depth: matchedDepth,
            match_kind: matchKind,
            hit_tag: hitTag,
            expected_bounds: {
              left: documentWidth * leftRatio,
              top: documentHeight * topRatio,
              right: documentWidth * rightRatio,
              bottom: documentHeight * bottomRatio,
            },
          };
        }
        """,
        {
            "xRatio": request.click_x_ratio,
            "yRatio": request.click_y_ratio,
            "leftRatio": request.left_ratio,
            "topRatio": request.top_ratio,
            "rightRatio": request.right_ratio,
            "bottomRatio": request.bottom_ratio,
            "expectedText": request.visual_expected_text,
            "entryKind": request.visual_entry_kind,
        },
    )
    if not isinstance(mapping, dict):
        raise BlockedPageCaptureActionError(
            "浏览器未返回有效视觉坐标映射",
            error_code="VISUAL_COORDINATE_INVALID",
        )
    numeric_values = {
        key: mapping.get(key)
        for key in (
            "x",
            "y",
            "document_x",
            "document_y",
            "scroll_x",
            "scroll_y",
            "viewport_width",
            "viewport_height",
        )
    }
    if any(
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(float(value))
        for value in numeric_values.values()
    ):
        raise BlockedPageCaptureActionError(
            "浏览器返回的视觉坐标映射无效",
            error_code="VISUAL_COORDINATE_INVALID",
        )
    x = float(numeric_values["x"])
    y = float(numeric_values["y"])
    viewport_width = float(numeric_values["viewport_width"])
    viewport_height = float(numeric_values["viewport_height"])
    if not 0 <= x < viewport_width or not 0 <= y < viewport_height:
        raise BlockedPageCaptureActionError(
            "视觉坐标映射超出当前视口",
            error_code="VISUAL_COORDINATE_OUT_OF_VIEWPORT",
        )
    if mapping.get("target_matched") is not True:
        raise BlockedPageCaptureActionError(
            "视觉坐标中心未命中预期入口，拒绝点击",
            error_code="VISUAL_TARGET_NOT_AT_COORDINATE",
        )
    matched_depth = mapping.get("matched_depth")
    match_kind = mapping.get("match_kind")
    hit_tag = mapping.get("hit_tag")
    if (
        isinstance(matched_depth, bool)
        or not isinstance(matched_depth, int)
        or not isinstance(match_kind, str)
        or match_kind
        not in {
            "exact_text",
            "profile_activity_contract",
            "profile_settings_contract",
            "profile_vip_banner",
        }
        or not (0 <= matched_depth <= (8 if match_kind == "profile_vip_banner" else 4))
        or not isinstance(hit_tag, str)
        or not hit_tag
    ):
        raise BlockedPageCaptureActionError(
            "浏览器返回的视觉目标命中证据无效",
            error_code="VISUAL_COORDINATE_INVALID",
        )
    assert request.visual_expected_text is not None
    action_trace[-1].update(
        {
            "resolved_by": "validated_visual_coordinate",
            "visual_entry_kind": request.visual_entry_kind,
            "screenshot_index": request.screenshot_index,
            "visual_bounds": {
                "left_ratio": request.left_ratio,
                "top_ratio": request.top_ratio,
                "right_ratio": request.right_ratio,
                "bottom_ratio": request.bottom_ratio,
            },
            "visual_confidence": request.visual_confidence,
            "visual_target_verified": True,
            "visual_expected_text_sha256": hashlib.sha256(
                request.visual_expected_text.encode("utf-8")
            ).hexdigest(),
            "visual_hit_tag": hit_tag[:64],
            "visual_match_kind": match_kind,
            "visual_match_depth": matched_depth,
            "document_point": {
                "x": numeric_values["document_x"],
                "y": numeric_values["document_y"],
            },
            "viewport_point": {"x": numeric_values["x"], "y": numeric_values["y"]},
            "scroll_offset": {
                "x": numeric_values["scroll_x"],
                "y": numeric_values["scroll_y"],
            },
        }
    )
    if mapping.get("target_adjusted") is True:
        action_trace[-1]["visual_coordinate_adjusted"] = True
    await page.mouse.click(x, y)


async def _seek_media_progress(
    *,
    page: Any,
    request: PageCaptureRequest,
    trace: dict[str, Any],
    timeout_ms: int,
) -> None:
    """把语义进度目标编译为一次真实指针拖动，并验证媒体状态变化。"""
    if not request.selector:
        raise BlockedPageCaptureActionError(
            "seek_media 缺少由 Runtime 解析的进度条目标",
            error_code="SEEK_TARGET_MISSING",
        )
    evaluate = getattr(page, "evaluate", None)
    mouse = getattr(page, "mouse", None)
    if not callable(evaluate) or mouse is None:
        raise PageInteractionFailedError(
            "浏览器驱动不支持进度条拖动",
            error_code="SEEK_DRIVER_UNAVAILABLE",
        )
    plan = await evaluate(
        _SEEK_MEDIA_PLAN_SCRIPT,
        {
            "selector": request.selector,
            "target_fraction": request.seek_fraction,
            "target_time_seconds": request.seek_time_seconds,
        },
    )
    if not isinstance(plan, dict) or plan.get("status") != "ready":
        reason = str((plan or {}).get("reason") or "seek_target_unavailable")
        error_code = {
            "target_not_found": "SEEK_TARGET_NOT_FOUND",
            "target_not_progress_control": "SEEK_TARGET_NOT_PROGRESS_CONTROL",
            "media_not_unique": "SEEK_MEDIA_NOT_UNIQUE",
            "media_duration_unavailable": "SEEK_MEDIA_STATE_UNAVAILABLE",
            "media_not_seekable": "SEEK_MEDIA_UNSEEKABLE",
            "native_controls_unavailable": "SEEK_NATIVE_CONTROLS_UNAVAILABLE",
            "target_out_of_range": "SEEK_TARGET_OUT_OF_RANGE",
            "target_not_visible": "SEEK_TARGET_NOT_VISIBLE",
        }.get(reason, "SEEK_TARGET_UNAVAILABLE")
        raise BlockedPageCaptureActionError(
            "无法把语义目标解析为唯一、可拖动的媒体进度条",
            error_code=error_code,
        )
    start = plan.get("start") if isinstance(plan.get("start"), dict) else {}
    end = plan.get("end") if isinstance(plan.get("end"), dict) else {}
    start_x = float(start.get("x") or 0)
    start_y = float(start.get("y") or 0)
    end_x = float(end.get("x") or 0)
    end_y = float(end.get("y") or 0)
    pointer_down = False
    try:
        await mouse.move(start_x, start_y)
        await mouse.down()
        pointer_down = True
        await mouse.move(end_x, end_y, steps=8)
        await mouse.up()
        pointer_down = False
    finally:
        if pointer_down:
            await mouse.up()

    wait_for_function = getattr(page, "wait_for_function", None)
    if callable(wait_for_function):
        try:
            await wait_for_function(
                _SEEK_MEDIA_SETTLED_SCRIPT,
                arg={
                    "selector": request.selector,
                    "target_time_seconds": plan.get("target_time_seconds"),
                },
                timeout=min(timeout_ms, 5_000),
            )
        except Exception as exc:  # noqa: BLE001 - 统一转为结构化动作失败
            raise PageInteractionFailedError(
                "拖动后未观测到媒体跳转完成",
                error_code="SEEK_NOT_OBSERVED",
                retryable=True,
            ) from exc
    after = await evaluate(
        _SEEK_MEDIA_STATE_SCRIPT,
        {"selector": request.selector},
    )
    before = plan.get("before") if isinstance(plan.get("before"), dict) else {}
    after = after if isinstance(after, dict) else {}
    before_time = _finite_number(before.get("current_time"))
    after_time = _finite_number(after.get("current_time"))
    target_time = _finite_number(plan.get("target_time_seconds"))
    if (
        before_time is None
        or after_time is None
        or target_time is None
        or abs(after_time - before_time) < 0.25
        or abs(after_time - target_time) > 2.0
    ):
        raise PageInteractionFailedError(
            "拖动动作没有产生可验证的媒体时间变化",
            error_code="SEEK_EFFECT_NOT_CONFIRMED",
        )
    trace.update(
        {
            "result": "media_seek_confirmed",
            "target_fraction": plan.get("target_fraction"),
            "target_time_seconds": target_time,
            "media_before": before,
            "media_after": after,
        }
    )


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if number == number and abs(number) != float("inf") else None


_SEEK_MEDIA_PLAN_SCRIPT = """
({ selector, target_fraction: requestedFraction, target_time_seconds: requestedTime }) => {
  const target = document.querySelector(selector);
  if (!target) return { status: "blocked", reason: "target_not_found" };
  const visibleMedia = [...document.querySelectorAll("video,audio")].filter((item) => {
    const rect = item.getBoundingClientRect();
    const style = getComputedStyle(item);
    return rect.width > 0 && rect.height > 0
      && style.display !== "none" && style.visibility !== "hidden";
  });
  let media = target instanceof HTMLMediaElement ? target : null;
  let ancestor = target.parentElement;
  while (!media && ancestor) {
    const nearby = [...ancestor.querySelectorAll("video,audio")].filter(
      (item) => visibleMedia.includes(item),
    );
    if (nearby.length === 1) media = nearby[0];
    if (nearby.length > 1) break;
    ancestor = ancestor.parentElement;
  }
  if (!media && visibleMedia.length === 1) media = visibleMedia[0];
  if (!media) return { status: "blocked", reason: "media_not_unique" };
  if (!(target instanceof HTMLMediaElement)) {
    const targetIdentity = [
      target.getAttribute("role"),
      target.getAttribute("aria-label"),
      target.getAttribute("aria-valuetext"),
      target.getAttribute("name"),
      target.getAttribute("data-testid"),
      target.className,
    ].map((item) => String(item || "").toLowerCase()).join(" ");
    if (
      /volume|brightness|rating|音量|亮度|评分/.test(targetIdentity)
      || !/progress|seek|timeline|current.?time|进度|播放时间/.test(targetIdentity)
    ) {
      return { status: "blocked", reason: "target_not_progress_control" };
    }
  }
  const duration = Number(media.duration);
  const currentTime = Number(media.currentTime);
  if (!Number.isFinite(duration) || duration <= 0 || !Number.isFinite(currentTime)) {
    return { status: "blocked", reason: "media_duration_unavailable" };
  }
  const fraction = requestedFraction == null
    ? Number(requestedTime) / duration
    : Number(requestedFraction);
  if (!Number.isFinite(fraction) || fraction < 0.05 || fraction > 0.95) {
    return { status: "blocked", reason: "target_out_of_range" };
  }
  const targetTime = duration * fraction;
  const seekableRanges = [];
  for (let index = 0; index < media.seekable.length; index += 1) {
    seekableRanges.push({
      start: Number(media.seekable.start(index)),
      end: Number(media.seekable.end(index)),
    });
  }
  if (!seekableRanges.some((range) => (
    Number.isFinite(range.start)
    && Number.isFinite(range.end)
    && range.start - 0.5 <= targetTime
    && targetTime <= range.end + 0.5
  ))) {
    return { status: "blocked", reason: "media_not_seekable" };
  }
  if (target instanceof HTMLMediaElement && !target.controls) {
    return { status: "blocked", reason: "native_controls_unavailable" };
  }
  const rect = target.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) {
    return { status: "blocked", reason: "target_not_visible" };
  }
  const pad = Math.min(Math.max(rect.width * 0.02, 2), 12);
  const usableWidth = Math.max(rect.width - pad * 2, 1);
  const currentFraction = Math.min(Math.max(currentTime / duration, 0.02), 0.98);
  const y = target instanceof HTMLMediaElement
    ? rect.bottom - Math.min(Math.max(rect.height * 0.06, 8), 24)
    : rect.top + rect.height / 2;
  const round = (value) => Math.round(value * 1000) / 1000;
  return {
    status: "ready",
    start: { x: round(rect.left + pad + usableWidth * currentFraction), y: round(y) },
    end: { x: round(rect.left + pad + usableWidth * fraction), y: round(y) },
    target_fraction: round(fraction),
    target_time_seconds: round(targetTime),
    before: {
      current_time: round(currentTime),
      duration: round(duration),
      paused: media.paused,
      ended: media.ended,
      seeking: media.seeking,
      ready_state: media.readyState,
      playback_rate: round(media.playbackRate),
    },
  };
}
"""


_SEEK_MEDIA_SETTLED_SCRIPT = """
({ selector, target_time_seconds: targetTime }) => {
  const target = document.querySelector(selector);
  if (!target) return false;
  const media = target instanceof HTMLMediaElement
    ? target
    : (() => {
        let ancestor = target.parentElement;
        while (ancestor) {
          const items = ancestor.querySelectorAll("video,audio");
          if (items.length === 1) return items[0];
          if (items.length > 1) return null;
          ancestor = ancestor.parentElement;
        }
        const all = document.querySelectorAll("video,audio");
        return all.length === 1 ? all[0] : null;
      })();
  return Boolean(
    media
    && !media.seeking
    && Number.isFinite(media.currentTime)
    && Math.abs(media.currentTime - Number(targetTime)) <= 2,
  );
}
"""


_SEEK_MEDIA_STATE_SCRIPT = """
({ selector }) => {
  const target = document.querySelector(selector);
  if (!target) return {};
  const media = target instanceof HTMLMediaElement
    ? target
    : (() => {
        let ancestor = target.parentElement;
        while (ancestor) {
          const items = ancestor.querySelectorAll("video,audio");
          if (items.length === 1) return items[0];
          if (items.length > 1) return null;
          ancestor = ancestor.parentElement;
        }
        const all = document.querySelectorAll("video,audio");
        return all.length === 1 ? all[0] : null;
      })();
  if (!media) return {};
  const round = (value) => Number.isFinite(value)
    ? Math.round(value * 1000) / 1000
    : null;
  return {
    current_time: round(media.currentTime),
    duration: round(media.duration),
    paused: media.paused,
    ended: media.ended,
    seeking: media.seeking,
    ready_state: media.readyState,
    playback_rate: round(media.playbackRate),
  };
}
"""


async def _clear_page_entry_obstructions(
    *,
    page: Any,
    context: Any,
    request: PageCaptureRequest,
    action_trace: list[dict[str, Any]],
    obstruction_frames: list[dict[str, Any]],
    timeout_ms: int,
    timeout_error_type: type[Exception],
) -> dict[str, Any]:
    """Web 页面落地后先检测并安全关闭首层阻断物，再进入既有有界清障链。"""

    popup_target = await _resolve_first_popup_dismissal_target(
        page=page,
        selectors=_obstruction_chain_dismissal_selectors("text=关闭"),
    )
    if not popup_target or popup_target.get("dialog_present") is not True:
        return {
            "enabled": True,
            "detected": False,
            "dismissal_count": 0,
            "outcome": "stable",
            "remaining_obstruction": False,
            "llm_fallback_required": False,
        }
    if str(popup_target.get("kind") or "") not in {"close_control", "overlay"}:
        return {
            "enabled": True,
            "detected": True,
            "dismissal_count": 0,
            "outcome": "llm_fallback_required",
            "remaining_obstruction": True,
            "llm_fallback_required": True,
        }
    if request.capture_screenshot:
        obstruction_frames.append(
            await _capture_obstruction_frame(
                page=page,
                index=0,
                full_page=False,
            )
        )
    dismissal_request = replace(
        request,
        mode=PageCaptureMode.SINGLE_TRIGGER,
        trigger_action=PageTriggerAction.CLICK,
        selector="text=关闭",
        clear_obstructions=False,
        timeout_ms=min(timeout_ms, OBSTRUCTION_CHAIN_CLICK_TIMEOUT_MS),
    )
    try:
        await _run_single_trigger(
            page=page,
            context=context,
            request=dismissal_request,
            action_trace=action_trace,
            timeout_ms=dismissal_request.timeout_ms,
            timeout_error_type=timeout_error_type,
        )
    except BlockedPageCaptureActionError:
        return {
            "enabled": True,
            "detected": True,
            "dismissal_count": 0,
            "outcome": "llm_fallback_required",
            "remaining_obstruction": True,
            "llm_fallback_required": True,
        }
    metrics = await _clear_remaining_obstructions(
        page=page,
        context=context,
        request=dismissal_request,
        action_trace=action_trace,
        obstruction_frames=obstruction_frames,
        timeout_ms=timeout_ms,
        timeout_error_type=timeout_error_type,
    )
    metrics["detected"] = True
    return metrics


async def _clear_remaining_obstructions(
    *,
    page: Any,
    context: Any,
    request: PageCaptureRequest,
    action_trace: list[dict[str, Any]],
    obstruction_frames: list[dict[str, Any]],
    timeout_ms: int,
    timeout_error_type: type[Exception],
) -> dict[str, Any]:
    """在一次可信关闭动作后有界清理后续弹窗，并为每层保留轻量截图与动作轨迹。"""
    started_at = time.perf_counter()
    dismissal_count = 1
    if action_trace:
        action_trace[-1]["obstruction_chain_index"] = dismissal_count
    outcome = "stable"
    remaining_obstruction = False

    while dismissal_count < MAX_OBSTRUCTION_CHAIN_DISMISSALS:
        elapsed_ms = _elapsed_ms(started_at)
        if elapsed_ms >= MAX_OBSTRUCTION_CHAIN_DURATION_MS:
            remaining_obstruction = await _visible_popup_remains(
                page, h5_session=str(request.session_id or "").startswith("h5:"),
            )
            outcome = "duration_limit_reached" if remaining_obstruction else "stable"
            break

        popup_target = await _wait_for_next_obstruction_target(
            page=page,
            selectors=_obstruction_chain_dismissal_selectors(request.selector),
            prefer_safe_outside=(
                request.h5_popup_dismissal_strategy == "safe_outside_then_close"
            ),
            h5_session=str(request.session_id or "").startswith("h5:"),
            timeout_ms=min(
                OBSTRUCTION_CHAIN_NEXT_LAYER_TIMEOUT_MS,
                max(MAX_OBSTRUCTION_CHAIN_DURATION_MS - int(elapsed_ms), 0),
            ),
        )
        if not popup_target or popup_target.get("dialog_present") is not True:
            outcome = "stable"
            break
        if str(popup_target.get("kind") or "") not in {"close_control", "overlay"}:
            outcome = "llm_fallback_required"
            remaining_obstruction = True
            break

        if request.capture_screenshot:
            obstruction_frames.append(
                await _capture_obstruction_frame(
                    page=page,
                    index=dismissal_count,
                    full_page=False,
                )
            )
        nested_request = _clone_page_capture_request(
            request,
            # 下一层的绝对 DOM selector 只用于证明当前弹窗存在；它可能不含
            # close/dismiss 类名，直接回灌会丢失关闭语义并落入普通点击。
            # 年龄确认是一次性门禁，后续必须使用已证明的弹窗关闭目标；
            # 其他清障动作继续保留原受信关闭 selector。
            selector=_nested_obstruction_click_selector(
                request_selector=request.selector,
                popup_target=popup_target,
            ),
            timeout_ms=min(timeout_ms, OBSTRUCTION_CHAIN_CLICK_TIMEOUT_MS),
            clear_obstructions=False,
        )
        try:
            await _run_single_trigger(
                page=page,
                context=context,
                request=nested_request,
                action_trace=action_trace,
                timeout_ms=nested_request.timeout_ms,
                timeout_error_type=timeout_error_type,
            )
        except BlockedPageCaptureActionError:
            outcome = "llm_fallback_required"
            remaining_obstruction = True
            break
        finally:
            # 预检查只传递签名，正式动作重新绑定；两者的监听必须分别释放。
            if popup_target.get("dialog_node_token"):
                try:
                    await page.evaluate("""token => {
                        window.__aliothModalObservers?.get(token)?.disconnect();
                        window.__aliothModalObservers?.delete(token);
                    }""", popup_target["dialog_node_token"])
                except Exception:  # noqa: BLE001  # 页面销毁不能掩盖原动作结果
                    pass
        dismissal_count += 1
        if action_trace:
            action_trace[-1]["obstruction_chain_index"] = dismissal_count

    else:
        remaining_obstruction = await _visible_popup_remains(
            page, h5_session=str(request.session_id or "").startswith("h5:"),
        )
        outcome = "dismissal_limit_reached" if remaining_obstruction else "stable"

    return {
        "enabled": True,
        "dismissal_count": dismissal_count,
        "max_dismissals": MAX_OBSTRUCTION_CHAIN_DISMISSALS,
        "duration_ms": round(_elapsed_ms(started_at), 3),
        "outcome": outcome,
        "remaining_obstruction": remaining_obstruction,
        "llm_fallback_required": outcome == "llm_fallback_required",
    }


def _require_h5_obstruction_chain_cleared(
    *,
    request: PageCaptureRequest,
    metrics: Mapping[str, Any],
    action_trace: list[dict[str, Any]],
) -> None:
    """H5 补采必须清空可证明的弹窗链，禁止用仍被遮挡的截图满足请求。"""
    if request.h5_popup_dismissal_strategy != "safe_outside_then_close":
        return
    if request.allow_h5_popup_layer_continuation:
        return
    if metrics.get("remaining_obstruction") is not True:
        return
    if action_trace:
        action_trace[-1]["result"] = "h5_popup_chain_remains_blocked"
        action_trace[-1]["obstruction_chain"] = dict(metrics)
    raise BlockedPageCaptureActionError(
        "当前 H5 仍有不可安全关闭的后续弹层，禁止将遮挡截图记为补采成功。",
        error_code="H5_POPUP_CHAIN_REMAINS_BLOCKED",
    )


def _nested_obstruction_click_selector(
    *,
    request_selector: str | None,
    popup_target: dict[str, Any],
) -> str:
    signed_selector = popup_target.get("signed_selector")
    if isinstance(signed_selector, str) and signed_selector:
        return signed_selector
    normalized = str(request_selector or "").strip()
    if normalized.casefold() in _TRUSTED_AGE_CONFIRM_SELECTORS:
        if str(popup_target.get("kind") or "") == "overlay":
            # 遮罩关闭依赖实时解析得到的弹窗外安全坐标，不能退化为普通 selector 点击。
            return "text=关闭"
        resolved = str(popup_target.get("selector") or "").strip()
        return resolved or "text=关闭"
    return normalized or "text=关闭"


async def _wait_for_obstruction_chain_layer_settle(page: Any) -> None:
    """等待页面关闭动画或延迟弹窗入场，避免过早把弹窗链误判为稳定。"""
    wait_for_timeout = getattr(page, "wait_for_timeout", None)
    if not callable(wait_for_timeout):
        return
    await wait_for_timeout(OBSTRUCTION_CHAIN_LAYER_SETTLE_MS)


async def _wait_for_next_obstruction_target(
    *,
    page: Any,
    selectors: tuple[str, ...],
    timeout_ms: int,
    prefer_safe_outside: bool = False,
    h5_session: bool = False,
) -> dict[str, Any] | None:
    """等待下一层弹窗入场；窗口内无弹窗才认为链路稳定。"""
    deadline = time.perf_counter() + max(timeout_ms, 0) / 1000
    # 未采集到事实时保持保守阻断，不能把耗尽的窗口解释为弹窗消失。
    popup_target: dict[str, Any] | None = {"dialog_present": True, "kind": "unproven"}
    if timeout_ms <= 0:
        return popup_target
    wait_for_timeout = getattr(page, "wait_for_timeout", None)
    if not callable(wait_for_timeout):
        if time.perf_counter() >= deadline:
            return popup_target
        return await _resolve_first_popup_dismissal_target(
            page=page,
            selectors=selectors,
            prefer_safe_outside=prefer_safe_outside,
            h5_session=h5_session,
        )

    await wait_for_timeout(min(
        OBSTRUCTION_CHAIN_LAYER_SETTLE_MS,
        max((deadline - time.perf_counter()) * 1000, 0),
    ))
    # 采集耗时也计入同一期限；已开始的采集仍保留真实阻断，不能超时伪造稳定。
    while time.perf_counter() < deadline:
        popup_target = await _resolve_first_popup_dismissal_target(
            page=page,
            selectors=selectors,
            prefer_safe_outside=prefer_safe_outside,
            h5_session=h5_session,
        )
        if popup_target and popup_target.get("dialog_present") is True:
            return popup_target
        remaining_ms = (deadline - time.perf_counter()) * 1000
        if remaining_ms <= 0:
            break
        await wait_for_timeout(min(
            OBSTRUCTION_CHAIN_NEXT_LAYER_POLL_MS,
            remaining_ms,
        ))
    return popup_target if isinstance(popup_target, dict) else None


async def _resolve_first_popup_dismissal_target(
    *,
    page: Any,
    selectors: tuple[str, ...],
    prefer_safe_outside: bool = False,
    h5_session: bool = False,
) -> dict[str, Any] | None:
    h5_obstruction_confirmed = False
    if h5_session:
        from services.api.app.application.h5_modal_acknowledgement import (
            H5_MODAL_LOCAL_SAFE_ACKNOWLEDGEMENT_SELECTOR,
            h5_modal_local_safe_acknowledgement,
        )
        from services.api.app.application.h5_modal_close import (
            H5_MODAL_CLOSE_SELECTOR,
            h5_modal_close,
        )
        from services.api.app.application.h5_obstruction_facts import assess_h5_ui_obstruction

        snapshot = await _collect_ui_snapshot(page, surface_session_id="h5:next-modal")
        h5_obstruction_confirmed = assess_h5_ui_obstruction(snapshot).blocking
        close = h5_modal_close(snapshot)
        proof = close or h5_modal_local_safe_acknowledgement(snapshot)
        if proof is not None:
            from services.api.app.infrastructure.browser.h5_modal_dismissal_target import (
                signed_modal_target,
            )

            prefix = (
                H5_MODAL_CLOSE_SELECTOR if close else H5_MODAL_LOCAL_SAFE_ACKNOWLEDGEMENT_SELECTOR
            )
            signed_selector = f"{prefix}:{proof.obstruction_signature}"
            # 选下一层时复用本次实时证明；真正点击仍在 _run_single_trigger 再次验签。
            target = await signed_modal_target(page, proof, prefer_safe_outside=prefer_safe_outside)
            if target is not None:
                target["signed_selector"] = signed_selector
                return target
        # 完整现场普查已确认无阻断时，不再用旧 CSS 名称把页面组件误认作弹窗。
        if (
            snapshot.get("h5_modal_census_truncated") is False
            and isinstance(snapshot.get("h5_modal_census_selectors"), list)
            and isinstance(snapshot.get("elements"), list)
            and snapshot["elements"]
            and not any(snapshot.get(key) for key in (
                "collection_error", "capture_degraded", "budget_truncated",
            ))
            and not h5_obstruction_confirmed
        ):
            return {"dialog_present": False}
    last_result: dict[str, Any] | None = None
    for selector in selectors:
        popup_target = await _resolve_popup_dismissal_target(
            page=page,
            selector=selector,
            **({"prefer_safe_outside": True} if prefer_safe_outside else {}),
        )
        if isinstance(popup_target, dict):
            last_result = popup_target
        if popup_target and popup_target.get("dialog_present") is True:
            return popup_target
    if h5_obstruction_confirmed:
        # 找不到安全动作不等于弹窗消失；交回探索器重新观察，不能伪造清障成功。
        return {"dialog_present": True, "kind": "unproven"}
    return last_result


def _obstruction_chain_dismissal_selectors(selector: str | None) -> tuple[str, ...]:
    normalized = str(selector or "").strip()
    selectors = []
    if (
        normalized
        and normalized.casefold() not in _TRUSTED_AGE_CONFIRM_SELECTORS
        and (
            _selector_has_dismissal_intent(normalized)
            or normalized.casefold() in _TRUSTED_OPAQUE_POPUP_DISMISSAL_SELECTORS
        )
    ):
        selectors.append(normalized)
    selectors.append("text=关闭")
    return tuple(dict.fromkeys(selectors))


async def _visible_popup_remains(page: Any, *, h5_session: bool = False) -> bool:
    """只判断是否仍有结构化可见弹窗，不执行任何点击。"""
    if h5_session:
        popup_target = await _resolve_first_popup_dismissal_target(
            page=page, selectors=("text=关闭",), h5_session=True,
        )
    else:
        popup_target = await _resolve_popup_dismissal_target(
            page=page, selector="text=关闭",
        )
    return bool(popup_target and popup_target.get("dialog_present") is True)


async def _capture_obstruction_frame(
    *,
    page: Any,
    index: int,
    full_page: bool,
) -> dict[str, Any]:
    """采集弹窗清障帧；仅截取视口，避免重复保存整页大图。"""
    return {
        "index": index,
        "encoding": "base64",
        "data": _encode_bytes(await page.screenshot(full_page=full_page)),
    }


async def _resolve_popup_dismissal_target(
    *,
    page: Any,
    selector: str,
    prefer_safe_outside: bool = False,
) -> dict[str, Any] | None:
    """把含糊的关闭文本解析为最上层弹窗的真实控件或弹窗外遮罩点。

    ``text=关闭`` 可能命中“可安全关闭”正文。只有页面确有可见弹窗且
    DOM 能证明真实关闭控件，才改点该控件；若控件不存在，则仅允许点击
    遮罩中位于弹窗外、且命中元素就是遮罩本身的坐标。
    """
    normalized_selector = selector.strip().casefold()
    from services.api.app.application.h5_modal_acknowledgement import (
        H5_MODAL_LOCAL_SAFE_ACKNOWLEDGEMENT_SELECTOR,
        h5_modal_local_safe_acknowledgement,
    )
    from services.api.app.application.h5_modal_close import (
        H5_MODAL_CLOSE_SELECTOR,
        h5_modal_close,
    )

    if normalized_selector.startswith(H5_MODAL_LOCAL_SAFE_ACKNOWLEDGEMENT_SELECTOR + ":"):
        signature = normalized_selector[len(H5_MODAL_LOCAL_SAFE_ACKNOWLEDGEMENT_SELECTOR) + 1:]
        proof = h5_modal_local_safe_acknowledgement(
            await _collect_ui_snapshot(page, surface_session_id="h5:modal-acknowledgement")
        )
        if proof is None or proof.obstruction_signature != signature:
            return None
        from services.api.app.infrastructure.browser.h5_modal_dismissal_target import (
            signed_modal_target,
        )

        return await signed_modal_target(page, proof, prefer_safe_outside=prefer_safe_outside)
    if normalized_selector.startswith(H5_MODAL_CLOSE_SELECTOR + ":"):
        signature = normalized_selector[len(H5_MODAL_CLOSE_SELECTOR) + 1:]
        proof = h5_modal_close(
            await _collect_ui_snapshot(page, surface_session_id="h5:modal-close")
        )
        if proof is None or proof.obstruction_signature != signature:
            return None
        from services.api.app.infrastructure.browser.h5_modal_dismissal_target import (
            signed_modal_target,
        )

        return await signed_modal_target(page, proof, prefer_safe_outside=prefer_safe_outside)
    if normalized_selector == POPUP_FLUTTER_APP_OPEN_CLOSE_SELECTOR.casefold():
        return await _resolve_flutter_app_open_popup_close_target(
            page=page,
            require_app_open_label=True,
        )
    if normalized_selector == POPUP_FLUTTER_CENTERED_AD_CLOSE_SELECTOR.casefold():
        return await _resolve_flutter_app_open_popup_close_target(
            page=page,
            require_app_open_label=False,
        )
    if normalized_selector == POPUP_FLUTTER_CENTERED_AD_CLOSE_ABOVE_SELECTOR.casefold():
        return await _resolve_flutter_app_open_popup_close_target(
            page=page,
            require_app_open_label=False,
            close_above=True,
        )
    if normalized_selector == POPUP_FLUTTER_CENTERED_SCROLL_CLOSE_BELOW_SELECTOR.casefold():
        return await _resolve_flutter_app_open_popup_close_target(
            page=page,
            require_app_open_label=False,
            scroll_body=True,
        )
    if normalized_selector == POPUP_FLUTTER_IMAGE_TOP_RIGHT_CLOSE_SELECTOR.casefold():
        return await _resolve_flutter_app_open_popup_close_target(
            page=page,
            require_app_open_label=False,
            image_body_top_right=True,
        )
    if normalized_selector == POPUP_FLUTTER_ACCOUNT_NOTICE_TOP_RIGHT_CLOSE_SELECTOR.casefold():
        return await _resolve_flutter_app_open_popup_close_target(
            page=page,
            require_app_open_label=False,
            account_notice_child=True,
        )
    if normalized_selector == POPUP_FLUTTER_MARKETING_ANNOUNCEMENT_CONFIRM_SELECTOR.casefold():
        return await _resolve_flutter_marketing_announcement_confirm_target(page=page)
    if normalized_selector == POPUP_HOMEPAGE_PAYMENT_CLOSE_SELECTOR.casefold():
        return await _resolve_homepage_payment_popup_close_target(page=page)
    if not _selector_has_dismissal_intent(normalized_selector) and normalized_selector not in {
        H5_PROFILE_CREDENTIAL_SAVE_CONFIRM_SELECTOR.casefold(),
        H5_PROFILE_CREDENTIAL_SCREENSHOT_CONFIRM_SELECTOR.casefold(),
    }:
        return None
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return None
    try:
        result = await evaluate(
            """
            (input) => {
              const requestedSelector = typeof input === "string"
                ? input : (input.requestedSelector || input.selector);
              const strictNavigationSubtree = Boolean(input?.strictNavigationSubtree);
              const preferSafeOutside = Boolean(input?.prefer_safe_outside);
              const isVisible = (element) => {
                if (!(element instanceof Element)) return false;
                const style = getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.display !== "none"
                  && style.visibility !== "hidden"
                  && Number(style.opacity || 1) > 0
                  && rect.width > 0
                  && rect.height > 0;
              };
              const selectorFor = (element) => {
                if (!(element instanceof Element)) return null;
                if (element.id) return `#${CSS.escape(element.id)}`;
                const tagName = element.tagName.toLowerCase();
                for (const attribute of ["data-testid", "aria-label", "title"]) {
                  const value = element.getAttribute(attribute);
                  if (!value) continue;
                  const candidate = (
                    `${tagName}`
                    + `[${attribute}="${CSS.escape(value)}"]`
                  );
                  if (document.querySelectorAll(candidate).length === 1) return candidate;
                }
                for (const className of element.classList) {
                  if (!/(?:close|dismiss|skip|down-time)/i.test(className)) continue;
                  const candidate = `${tagName}.${CSS.escape(className)}`;
                  if (document.querySelectorAll(candidate).length === 1) return candidate;
                }
                const parts = [];
                let current = element;
                while (current instanceof Element && current !== document.body) {
                  const tag = current.tagName.toLowerCase();
                  const parent = current.parentElement;
                  if (!parent) break;
                  const siblings = [...parent.children].filter(
                    (item) => item.tagName === current.tagName
                  );
                  const index = Math.max(siblings.indexOf(current), 0) + 1;
                  parts.unshift(`${tag}:nth-of-type(${index})`);
                  current = parent;
                }
                return parts.length ? `body > ${parts.join(" > ")}` : null;
              };
              const browserSelectorFor = (selector) => selector
                .replaceAll(':visible', '')
                .replace(/:has\\([^)]*\\)/g, '');
              const centerHitSafe = (element) => {
                if (!(element instanceof Element)) return false;
                const rect = element.getBoundingClientRect();
                if (
                  rect.width <= 0 || rect.height <= 0
                  || rect.right <= 0 || rect.bottom <= 0
                  || rect.left >= window.innerWidth || rect.top >= window.innerHeight
                ) return false;
                const x = Math.min(Math.max(rect.left + rect.width / 2, 0), window.innerWidth - 1);
                const y = Math.min(Math.max(rect.top + rect.height / 2, 0), window.innerHeight - 1);
                const hit = document.elementFromPoint(x, y);
                return Boolean(hit && (hit === element || element.contains(hit)));
              };
              const navigationPayloadSelector = [
                "a[href]", "[role='link']", "[href]", "[data-href]",
                "[data-url]", "[data-uri]", "[data-route]", "[to]",
              ].join(",");
              const hasNavigationPayload = (element) => Boolean(
                element.matches(navigationPayloadSelector)
                || element.querySelector(navigationPayloadSelector)
              );
              const dialogSignature = (element) => JSON.stringify([
                element.tagName.toLowerCase(),
                element.getAttribute("role") || "",
                element.className || "",
                (element.innerText || element.textContent || "")
                  .replace(/\\s+/g, " ").trim().slice(0, 500),
                [...element.querySelectorAll("img[src]")]
                  .slice(0, 5)
                  .map((item) => item.getAttribute("src") || ""),
                [...element.querySelectorAll('.ad-item, .btn-close, .b-img, [class*="active"]')]
                  .slice(0, 30)
                  .map((item) => {
                    const style = getComputedStyle(item);
                    const rect = item.getBoundingClientRect();
                    return [
                      item.tagName.toLowerCase(),
                      item.className || "",
                      item.getAttribute("style") || "",
                      style.display,
                      style.visibility,
                      Number(style.opacity || 1) > 0,
                      rect.width > 0 && rect.height > 0,
                      item.getAttribute("data-track-ad-click") || "",
                      item.getAttribute("src") || "",
                    ];
                  }),
              ]);
              const isNonblockingFloatingWidget = (element) => {
                const className = String(
                  element.className?.baseVal || element.className || ""
                );
                if (!/(suspend|float)/i.test(className)) return false;
                const rect = element.getBoundingClientRect();
                const viewportArea = Math.max(window.innerWidth * window.innerHeight, 1);
                return Math.max(rect.width * rect.height, 0) / viewportArea < 0.20;
              };
              const knownDialogs = [
                ...document.querySelectorAll(
                  '[role="dialog"], [aria-modal="true"], .van-dialog, '
                  + '.modal, .dialog, .persalePopup > .van-overlay > .wrapper, '
                  + '.nineGridPopup > .van-overlay > .wrapper, '
                  + '.announcementPopup > .van-overlay > .imgWrapper, '
                  + '.homepagePaymentPopup > .van-overlay > .imgWrapper, '
                  + '[class*="popup"]:not(.popupContent), '
                  + '.loading-page, .start-up, [class*="startup"], '
                  + '.ad-item, .home-suspend, .recommend-layer, '
                  + '[class*="suspend"], [class*="float"]'
                ),
              ].filter((element) => (
                isVisible(element) && !isNonblockingFloatingWidget(element)
              ));
              const structuralDialogs = [...document.querySelectorAll("*")]
                .filter((element) => {
                  if (!isVisible(element)) return false;
                  if (isNonblockingFloatingWidget(element)) return false;
                  const className = String(
                    element.className?.baseVal || element.className || ""
                  );
                  if (!/(popup|modal|dialog)/i.test(className)) return false;
                  const rect = element.getBoundingClientRect();
                  const viewportArea = Math.max(window.innerWidth * window.innerHeight, 1);
                  const coverage = Math.max(rect.width * rect.height, 0) / viewportArea;
                  if (coverage < 0.02) return false;
                  const hit = document.elementFromPoint(
                    rect.left + rect.width / 2,
                    rect.top + rect.height / 2,
                  );
                  return Boolean(hit && (element === hit || element.contains(hit)));
                });
              const explicitDialogRootSelector = [
                "[role='dialog']", "[aria-modal='true']", ".van-dialog", ".van-popup", ".modal",
                ".dialog", ".adspop", "#popup-window", ".nineGridPopup",
                ".announcementPopup", ".homepagePaymentPopup", ".ad-dialog",
                ".recommend-layer", ".home-suspend", ".loading-page", ".start-up",
              ].join(",");
              // 已知弹层的内部广告项统一折叠到真实根；通用常驻 popup host 不会
              // 吞掉其内独立的高层弹窗，二者继续按完整 stacking context 仲裁。
              const dialogs = [...new Set(
                [...knownDialogs, ...structuralDialogs].map((candidate) => {
                  const root = candidate.closest(explicitDialogRootSelector);
                  // 固定定位的内层可见弹窗不能被零高度宿主吞掉。
                  return root && isVisible(root) ? root : candidate;
                }),
              )].filter(isVisible);
              if (!dialogs.length) return { dialog_present: false };
              const stackingRank = (element) => {
                const rank = [];
                let current = element;
                while (current instanceof Element) {
                  const style = getComputedStyle(current);
                  const position = String(style.position || "static").toLowerCase();
                  const zIndex = Number.parseInt(style.zIndex, 10);
                  if (position !== "static" && Number.isFinite(zIndex)) rank.unshift(zIndex);
                  current = current.parentElement;
                }
                return rank;
              };
              const compareRank = (left, right) => {
                const leftRank = stackingRank(left);
                const rightRank = stackingRank(right);
                const length = Math.max(leftRank.length, rightRank.length);
                for (let index = 0; index < length; index += 1) {
                  const delta = (leftRank[index] || 0) - (rightRank[index] || 0);
                  if (delta !== 0) return delta;
                }
                const relation = left.compareDocumentPosition(right);
                return relation & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1;
              };
              dialogs.sort(compareRank);
              const dialog = dialogs.at(-1);
              const dialogText = (dialog.innerText || "").trim().toLowerCase();
              const dialogRequestsRetry = new RegExp(
                '(?:配置数据失败|请刷新|网络|失败|network|error|refresh|retry)',
                'i',
              ).test(dialogText);
              const isAccountCredentialDialog = (
                /账号凭证|永久官方地址|请勿删除此凭证|官方客服找回账号/.test(dialogText)
              );
              let requestedInteractive = [];
              try {
                requestedInteractive = [
                  ...document.querySelectorAll(browserSelectorFor(requestedSelector)),
                ].filter((element) => (
                  isVisible(element)
                  && dialog.contains(element)
                  && !element.classList.contains("van-overlay")
                ));
              } catch (_error) {
                requestedInteractive = [];
              }
              const exactDismissals = [
                ...document.querySelectorAll(
                  '.homepagePaymentPopup .imgWrapper > .close, '
                  + '.announcementPopup .van-overlay .imgWrapper > .close, '
                  + '.nineGridPopup .van-overlay .wrapper > .close'
                ),
              ].filter((element) => isVisible(element) && dialog.contains(element));
              const accountCredentialConfirmControls = isAccountCredentialDialog
                ? [...dialog.querySelectorAll("*")].filter((element) => {
                    if (!isVisible(element)) return false;
                    const text = (element.innerText || element.textContent || "")
                      .replace(/\\s+/g, " ").trim();
                    if (text !== "我已保存") return false;
                    const rect = element.getBoundingClientRect();
                    if (rect.width < 40 || rect.height < 24) return false;
                    const hit = document.elementFromPoint(
                      rect.left + rect.width / 2,
                      rect.top + rect.height / 2
                    );
                    return Boolean(hit && (element === hit || element.contains(hit)));
                  })
                : [];
              const nativeInteractive = [
                ...requestedInteractive,
                ...exactDismissals,
                ...accountCredentialConfirmControls,
                ...dialog.querySelectorAll(
                  'button, [role="button"], a, input[type="button"], '
                  + 'input[type="submit"], input[type="reset"], '
                  + '.btn-box, .close_btn, .btn-close, .close-btn, '
                  + '.van-popup__close-icon, .van-icon-close, .close-img, '
                  + '.close-image, .on-dialog-close, img[alt="关闭"], '
                  + 'img[alt="close" i], .van-dialog__confirm, '
                  + '.van-dialog__cancel, .app-btn, .down-time'
                ),
              ].filter(isVisible);
              const semanticLeafControls = [...dialog.querySelectorAll("*")]
                .filter((element) => {
                  if (!isVisible(element) || element.children.length > 0) return false;
                  const text = (element.innerText || element.textContent || "")
                    .replace(/\\s+/g, " ").trim();
                  const markers = [
                    element.getAttribute("aria-label"),
                    element.getAttribute("title"),
                    element.getAttribute("data-testid"),
                    element.id,
                    element.className,
                  ].filter(Boolean).join(" ");
                  return /^(关闭|跳过|稍后|close|dismiss|skip)$/i.test(text)
                    && /(btn|button|close|dismiss|skip|end)/i.test(markers);
                });
              // 纯图片关闭键：无文字、无 aria-label，类名只是通用 btn，
              // 关键词打分恒为 0，且不在上面的关闭类名清单里，等于既进不了
              // 候选也拿不到分（实测性巴克九宫格广告弹窗）。只能按结构取证：
              // 弹窗内唯一的、无文字的单图控件，且图标本身是 20~64px 的小方形。
              // 广告位是四列宫格、单边普遍大于 64px，落不进该几何；要求弹窗内
              // 有且仅有一个才认，多个时无法区分关闭与广告，宁可不认。
              const imageOnlyCloseControls = [...dialog.querySelectorAll("*")]
                .filter((element) => {
                  if (!isVisible(element)) return false;
                  const text = (element.innerText || element.textContent || "").trim();
                  if (text) return false;
                  if (
                    element.getAttribute("aria-label")
                    || element.getAttribute("title")
                  ) return false;
                  const images = element.querySelectorAll("img");
                  if (images.length !== 1) return false;
                  const iconRect = images[0].getBoundingClientRect();
                  if (
                    iconRect.width < 20
                    || iconRect.width > 64
                    || iconRect.height < 20
                    || iconRect.height > 64
                    || Math.abs(iconRect.width - iconRect.height) > 8
                  ) return false;
                  const rect = element.getBoundingClientRect();
                  const hit = document.elementFromPoint(
                    rect.left + rect.width / 2,
                    rect.top + rect.height / 2,
                  );
                  return Boolean(hit && (element === hit || element.contains(hit)));
                });
              const uniqueImageOnlyClose = imageOnlyCloseControls.length === 1
                ? imageOnlyCloseControls
                : [];
              const interactive = [...new Set([
                ...nativeInteractive,
                ...semanticLeafControls,
                ...uniqueImageOnlyClose,
              ])].filter((element) => Boolean(
                isVisible(element)
                && centerHitSafe(element)
                && (!strictNavigationSubtree || !hasNavigationPayload(element))
              ));
              const score = (element) => {
                const attributes = [
                  element.getAttribute("aria-label"),
                  element.getAttribute("title"),
                  element.getAttribute("data-testid"),
                  element.id,
                  element.className,
                ].filter(Boolean).join(" ").toLowerCase();
                const text = (element.innerText || element.value || "").trim().toLowerCase();
                let value = 0;
                const markerText = `${attributes} ${text}`;
                if (requestedInteractive.includes(element)) value += 200;
                if (
                  dialogRequestsRetry
                  && new RegExp(
                    '(?:van-dialog__confirm|confirm|ok|确认|确定)',
                    'i',
                  ).test(markerText)
                ) value += 140;
                if (
                  dialogRequestsRetry
                  && /(?:van-dialog__cancel|cancel|\u53d6\u6d88)/i.test(markerText)
                ) value += 80;
                if (isAccountCredentialDialog && text === "我已保存") value += 180;
                if (uniqueImageOnlyClose.includes(element)) value += 80;
                if (/(关闭|close|dismiss)/i.test(attributes)) value += 120;
                if (/(跳过|skip|稍后)/i.test(attributes)) value += 90;
                if (/^(×|x|✕|✖|关闭|close|跳过|skip|稍后|我知道了)$/i.test(text)) value += 100;
                if (
                  /(down-time|countdown)/i.test(attributes)
                  && /(关闭|close|跳过|skip)/i.test(text)
                ) value += 100;
                if (
                  text.length <= 12
                  && /(关闭|close|跳过|skip|稍后|我知道了)/i.test(text)
                ) value += 60;
                const rect = element.getBoundingClientRect();
                const hit = document.elementFromPoint(
                  rect.left + rect.width / 2,
                  rect.top + rect.height / 2
                );
                if (hit && (element === hit || element.contains(hit))) value += 20;
                return value;
              };
              const closeControl = interactive
                .map((element) => ({ element, score: score(element) }))
                .filter((item) => item.score >= 80)
                .sort((left, right) => right.score - left.score)[0]?.element;
              const dialogSelector = selectorFor(dialog);
              const originalDialogSignature = dialogSignature(dialog);
              const closeTarget = closeControl
                ? {
                  dialog_present: true,
                  kind: "close_control",
                  selector: selectorFor(closeControl),
                  dialog_selector: dialogSelector,
                  dialog_signature: originalDialogSignature,
                  verification_required: Boolean(dialogSelector),
                }
                : null;

              if (closeTarget && !preferSafeOutside) return closeTarget;

              const dialogRect = dialog.getBoundingClientRect();
              const overlayCandidates = [
                ...document.querySelectorAll(
                  '.van-overlay, .overlay, .backdrop, .mask, '
                  + '[class*="-overlay"], [class*="overlay-"], '
                  + '[class*="-backdrop"], [class*="-mask"]'
                ),
              ].filter((element) => {
                if (!isVisible(element)) return false;
                const rect = element.getBoundingClientRect();
                return rect.left <= dialogRect.left
                  && rect.top <= dialogRect.top
                  && rect.right >= dialogRect.right
                  && rect.bottom >= dialogRect.bottom;
              });
              const overlay = overlayCandidates
                .sort((left, right) => {
                  const leftZ = Number.parseInt(getComputedStyle(left).zIndex || "0", 10) || 0;
                  const rightZ = Number.parseInt(getComputedStyle(right).zIndex || "0", 10) || 0;
                  return rightZ - leftZ;
                })[0];
              if (!overlay) {
                return closeTarget || {
                  dialog_present: true,
                  kind: "blocked",
                  selector: null,
                  dialog_selector: dialogSelector,
                  dialog_signature: originalDialogSignature,
                  verification_required: Boolean(dialogSelector),
                };
              }
              const overlayRect = overlay.getBoundingClientRect();
              const cornerPoints = [
                [overlayRect.left + 2, overlayRect.top + 2],
                [overlayRect.right - 2, overlayRect.top + 2],
                [overlayRect.left + 2, overlayRect.bottom - 2],
                [overlayRect.right - 2, overlayRect.bottom - 2],
              ];
              const visibleLeft = Math.max(overlayRect.left, 0);
              const visibleTop = Math.max(overlayRect.top, 0);
              const visibleRight = Math.min(overlayRect.right, window.innerWidth);
              const visibleBottom = Math.min(overlayRect.bottom, window.innerHeight);
              const fractions = [0.05, 0.25, 0.5, 0.75, 0.95];
              // 移动端四角可能被安全区或壳层占用；补充有界网格，但仍只接受
              // Dialog 外且 elementFromPoint 严格等于遮罩本身的坐标。
              const gridPoints = (
                visibleRight - visibleLeft >= 4 && visibleBottom - visibleTop >= 4
              ) ? fractions.flatMap((yRatio) => fractions.map((xRatio) => [
                  visibleLeft + (visibleRight - visibleLeft) * xRatio,
                  visibleTop + (visibleBottom - visibleTop) * yRatio,
                ])) : [];
              const points = [...cornerPoints, ...gridPoints];
              const safePoints = points.filter(([x, y]) => {
                const outsideDialog = (
                  x < dialogRect.left || x > dialogRect.right
                  || y < dialogRect.top || y > dialogRect.bottom
                );
                return outsideDialog && document.elementFromPoint(x, y) === overlay;
              }).slice(0, 4);
              if (!safePoints.length) {
                return closeTarget || {
                  dialog_present: true,
                  kind: "blocked",
                  selector: null,
                  dialog_selector: dialogSelector,
                  dialog_signature: originalDialogSignature,
                  verification_required: Boolean(dialogSelector),
                };
              }
              const positions = safePoints.map(([x, y]) => ({
                x: Math.max(x - overlayRect.left, 1),
                y: Math.max(y - overlayRect.top, 1),
              }));
              return {
                dialog_present: true,
                kind: "overlay",
                selector: selectorFor(overlay),
                dialog_selector: dialogSelector,
                dialog_signature: originalDialogSignature,
                verification_required: Boolean(dialogSelector),
                fallback_close_target: closeTarget,
                position: positions[0],
                positions,
              };
            }
            """,
            {
                "requestedSelector": selector,
                "selector": selector,
                "prefer_safe_outside": prefer_safe_outside,
                "strictNavigationSubtree": (
                    normalized_selector == POPUP_OVERLAY_AD_CLOSE_SELECTOR.casefold()
                ),
            },
        )
    except Exception:  # noqa: BLE001  # 测试替身或旧浏览器不支持结构解析时保留原行为
        return None
    if not isinstance(result, dict):
        return None
    if (
        normalized_selector == POPUP_OVERLAY_AD_CLOSE_SELECTOR.casefold()
        and result.get("kind") == "close_control"
    ):
        # 广告关闭键禁止通过 element.click() 绕过指针遮挡；执行前后的真实
        # actionability 与弹层消失都必须由 Playwright 重新证明。
        result["pointer_click_required"] = True
    return result


async def _resolve_homepage_payment_popup_close_target(
    *,
    page: Any,
) -> dict[str, Any] | None:
    """实时复核 H5 首页付费弹层的唯一关闭键，避免被并列常驻浮层抢占。"""
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return None
    try:
        result = await evaluate(
            """
            () => {
              const visible = (element) => {
                if (!(element instanceof Element)) return false;
                const style = getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.display !== "none"
                  && style.visibility !== "hidden"
                  && Number(style.opacity || 1) > 0
                  && rect.width > 0
                  && rect.height > 0;
              };
              const candidates = [
                ...document.querySelectorAll(
                  ".homepagePaymentPopup .imgWrapper > .close"
                ),
              ].filter((element) => {
                const popup = element.closest(".homepagePaymentPopup");
                const overlay = element.closest(".van-overlay");
                if (
                  !popup || !overlay || !visible(overlay) || !visible(element)
                ) {
                  return false;
                }
                const rect = element.getBoundingClientRect();
                const x = Math.min(
                  Math.max(rect.left + rect.width / 2, 0),
                  window.innerWidth - 1,
                );
                const y = Math.min(
                  Math.max(rect.top + rect.height / 2, 0),
                  window.innerHeight - 1,
                );
                const hit = document.elementFromPoint(x, y);
                return Boolean(hit && (hit === element || element.contains(hit)));
              });
              if (candidates.length !== 1) return null;
              const close = candidates[0];
              const wrapper = close.closest(".imgWrapper");
              if (!(wrapper instanceof Element)) return null;
              const signature = JSON.stringify([
                wrapper.tagName.toLowerCase(),
                wrapper.getAttribute("role") || "",
                wrapper.className || "",
                (wrapper.innerText || wrapper.textContent || "")
                  .replace(/\\s+/g, " ").trim().slice(0, 500),
                [...wrapper.querySelectorAll("img[src]")]
                  .slice(0, 5)
                  .map((item) => item.getAttribute("src") || ""),
                [...wrapper.querySelectorAll('.ad-item, .btn-close, .b-img, [class*="active"]')]
                  .slice(0, 30)
                  .map((item) => {
                    const style = getComputedStyle(item);
                    const rect = item.getBoundingClientRect();
                    return [
                      item.tagName.toLowerCase(),
                      item.className || "",
                      item.getAttribute("style") || "",
                      style.display,
                      style.visibility,
                      Number(style.opacity || 1) > 0,
                      rect.width > 0 && rect.height > 0,
                      item.getAttribute("data-track-ad-click") || "",
                      item.getAttribute("src") || "",
                    ];
                  }),
              ]);
              return {
                dialog_present: true,
                kind: "close_control",
                selector: ".homepagePaymentPopup .imgWrapper > .close:visible",
                dialog_selector: ".homepagePaymentPopup .imgWrapper",
                dialog_signature: signature,
                verification_required: true,
                pointer_click_required: true,
              };
            }
            """
        )
    except Exception:  # noqa: BLE001
        return None
    return result if isinstance(result, dict) else None


async def _reacquire_signed_popup_dismissal_target(
    *,
    page: Any,
    selector: str,
    timeout_ms: int,
    prefer_safe_outside: bool = False,
) -> tuple[dict[str, Any] | None, int]:
    """短时重获来源 Capture 已验签、但执行瞬间缺席的弹窗结构。"""
    wait_for_timeout = getattr(page, "wait_for_timeout", None)
    if not callable(wait_for_timeout):
        return None, 0
    budget_ms = max(0, min(timeout_ms, _POPUP_STRUCTURE_REACQUIRE_TIMEOUT_MS))
    elapsed_ms = 0
    attempts = 0
    last_target: dict[str, Any] | None = None
    while elapsed_ms < budget_ms:
        poll_ms = min(
            _POPUP_STRUCTURE_REACQUIRE_POLL_MS,
            budget_ms - elapsed_ms,
        )
        await wait_for_timeout(poll_ms)
        elapsed_ms += poll_ms
        attempts += 1
        last_target = await _resolve_popup_dismissal_target(
            page=page,
            selector=selector,
            **({"prefer_safe_outside": True} if prefer_safe_outside else {}),
        )
        if _popup_dismissal_target_is_executable(last_target):
            return last_target, attempts
    return last_target, attempts


def _popup_dismissal_target_is_executable(
    target: Mapping[str, Any] | None,
) -> bool:
    """确认实时弹窗解析结果已经具备可执行且可复验的安全出口。"""
    if not target or target.get("dialog_present") is not True:
        return False
    if not str(target.get("selector") or "").strip():
        return False
    kind = str(target.get("kind") or "")
    if kind == "close_control":
        return True
    if kind != "overlay":
        return False
    position = target.get("position")
    if not isinstance(position, Mapping):
        return False
    return all(
        isinstance(position.get(axis), int | float)
        and not isinstance(position.get(axis), bool)
        and math.isfinite(float(position[axis]))
        and float(position[axis]) >= 0
        for axis in ("x", "y")
    )


async def _resolve_flutter_marketing_announcement_confirm_target(
    *,
    page: Any,
) -> dict[str, Any] | None:
    """实时复核含多项营销文案的 Flutter 公告及其唯一“确认”按钮。"""
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return None
    try:
        result = await evaluate(
            r"""
            () => {
              const visible = (element) => {
                if (!(element instanceof Element)) return false;
                const style = getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.display !== "none"
                  && style.visibility !== "hidden"
                  && Number(style.opacity || 1) > 0
                  && rect.width > 0
                  && rect.height > 0;
              };
              const textOf = (element) => String(
                element.innerText || element.textContent || ""
              ).replace(/\s+/g, " ").trim();
              const selectorFor = (element) => {
                if (!(element instanceof Element) || !element.id) return null;
                const selector = `#${CSS.escape(element.id)}`;
                return document.querySelectorAll(selector).length === 1 ? selector : null;
              };
              const signatureFor = (element) => JSON.stringify([
                element.tagName.toLowerCase(),
                element.getAttribute("role") || "",
                element.className || "",
                textOf(element).slice(0, 500),
                [...element.querySelectorAll("img[src]")]
                  .slice(0, 5)
                  .map((item) => item.getAttribute("src") || ""),
                [],
              ]);
              const nodes = [
                ...document.querySelectorAll("flt-semantics-host flt-semantics"),
              ].filter(visible);
              const confirms = nodes.filter((element) => (
                element.hasAttribute("flt-tappable") && textOf(element) === "确认"
              ));
              if (confirms.length !== 1) return { dialog_present: false };
              const confirmRect = confirms[0].getBoundingClientRect();
              const markers = [
                "TIKTOK重磅来袭",
                "官方推荐",
                "Telegram 交流群",
                "品牌推广",
                "收藏关注",
              ];
              // Flutter 的公告正文与确认按钮是同一语义容器下的兄弟节点，
              // 不能假设正文位于按钮祖先链。只在受限尺寸、相邻几何关系且
              // 命中多项公告标识的节点中，选择与唯一确认按钮最近的正文。
              const announcementBodies = nodes
                .filter((element) => element !== confirms[0])
                .map((element) => {
                  const rect = element.getBoundingClientRect();
                  const text = textOf(element);
                  const markerCount = markers.filter((marker) => text.includes(marker)).length;
                  const verticalGap = confirmRect.top - rect.bottom;
                  const confirmCenterX = confirmRect.left + confirmRect.width / 2;
                  const horizontallyBound = confirmCenterX >= rect.left - 20
                    && confirmCenterX <= rect.right + 20;
                  return { element, rect, markerCount, verticalGap, horizontallyBound };
                })
                .filter((candidate) => (
                  candidate.markerCount >= 2
                  && candidate.rect.width >= 240 && candidate.rect.width <= 360
                  && candidate.rect.height >= 180 && candidate.rect.height <= 500
                  && candidate.horizontallyBound
                  && candidate.verticalGap >= -40 && candidate.verticalGap <= 120
                ))
                .sort((left, right) => (
                  right.markerCount - left.markerCount
                  || Math.abs(left.verticalGap) - Math.abs(right.verticalGap)
                  || right.rect.width * right.rect.height - left.rect.width * left.rect.height
                ));
              const body = announcementBodies[0]?.element || null;
              if (!(body instanceof Element)) return { dialog_present: false };
              const confirmSelector = selectorFor(confirms[0]);
              const dialogSelector = selectorFor(body);
              if (!confirmSelector || !dialogSelector) return { dialog_present: false };
              const centerTarget = document.elementFromPoint(
                confirmRect.left + confirmRect.width / 2,
                confirmRect.top + confirmRect.height / 2,
              );
              const semanticCenterTarget = centerTarget?.closest("flt-semantics") || null;
              if (
                !centerTarget
                || (
                  centerTarget !== confirms[0]
                  && semanticCenterTarget !== confirms[0]
                  && !confirms[0].contains(centerTarget)
                )
              ) return { dialog_present: false };
              return {
                dialog_present: true,
                selector: confirmSelector,
                kind: "close_control",
                dialog_selector: dialogSelector,
                dialog_signature: signatureFor(body),
                verification_required: true,
              };
            }
            """
        )
    except Exception:  # noqa: BLE001 - Flutter 公告复核失败必须保持失败关闭
        return None
    return result if isinstance(result, dict) else None


async def _resolve_flutter_app_open_popup_close_target(
    *,
    page: Any,
    require_app_open_label: bool,
    close_above: bool = False,
    scroll_body: bool = False,
    image_body_top_right: bool = False,
    account_notice_child: bool = False,
) -> dict[str, Any] | None:
    """在实时 Flutter 语义树中绑定推广卡片相邻的唯一小方形关闭节点。"""
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return None
    try:
        result = await evaluate(
            """
            (options) => {
              const requireAppOpenLabel = options.requireAppOpenLabel;
              const closeAbove = options.closeAbove;
              const scrollBody = options.scrollBody;
              const imageBodyTopRight = options.imageBodyTopRight;
              const accountNoticeChild = options.accountNoticeChild;
              const visible = (element) => {
                if (!(element instanceof Element)) return false;
                const style = getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.display !== "none"
                  && style.visibility !== "hidden"
                  && Number(style.opacity || 1) > 0
                  && rect.width > 0
                  && rect.height > 0;
              };
              const selectorFor = (element) => {
                if (!(element instanceof Element) || !element.id) return null;
                const selector = `#${CSS.escape(element.id)}`;
                return document.querySelectorAll(selector).length === 1 ? selector : null;
              };
              const signatureFor = (element) => JSON.stringify([
                element.tagName.toLowerCase(),
                element.getAttribute("role") || "",
                element.className || "",
                (element.innerText || element.textContent || "")
                  .replace(/\\s+/g, " ").trim().slice(0, 500),
                [...element.querySelectorAll("img[src]")]
                  .slice(0, 5)
                  .map((item) => item.getAttribute("src") || ""),
                [...element.querySelectorAll('.ad-item, .btn-close, .b-img, [class*="active"]')]
                  .slice(0, 30)
                  .map((item) => {
                    const style = getComputedStyle(item);
                    const rect = item.getBoundingClientRect();
                    return [
                      item.tagName.toLowerCase(),
                      item.className || "",
                      item.getAttribute("style") || "",
                      style.display,
                      style.visibility,
                      Number(style.opacity || 1) > 0,
                      rect.width > 0 && rect.height > 0,
                      item.getAttribute("data-track-ad-click") || "",
                      item.getAttribute("src") || "",
                    ];
                  }),
              ]);
              const allNodes = [
                ...document.querySelectorAll(
                  "flt-semantics-host flt-semantics",
                ),
              ].filter(visible);
              const nodes = allNodes.filter((element) => (
                element.hasAttribute("flt-tappable")
              ));
              const labelPresent = nodes.some((element) => (
                String(element.getAttribute("role") || "").toLowerCase() === "text"
                && /APP内打开$/i.test(
                  String(element.innerText || element.textContent || "")
                    .replace(/\\s+/g, " ").trim(),
                )
              ));
              if (requireAppOpenLabel && !labelPresent) {
                return { dialog_present: false };
              }
              const empty = nodes.filter((element) => (
                !element.getAttribute("role")
                && !String(element.innerText || element.textContent || "").trim()
              ));
              const bodyCandidates = accountNoticeChild
                ? allNodes.filter((element) => {
                    const label = String(element.getAttribute("aria-label") || "")
                      .replace(/\\s+/g, "");
                    return !element.hasAttribute("flt-tappable")
                      && String(element.getAttribute("role") || "").toLowerCase() === "group"
                      && ["重要提示", "首次安装APP请先保存此信息", "账号找回", "邀请码"]
                        .every((marker) => label.includes(marker));
                  })
                : imageBodyTopRight
                  ? allNodes.filter((element) => (
                    !element.hasAttribute("flt-tappable")
                    && String(element.getAttribute("role") || "").toLowerCase() === "img"
                    ))
                  : scrollBody
                    ? allNodes.filter((element) => {
                      const style = getComputedStyle(element);
                      return !element.hasAttribute("flt-tappable")
                        && !element.getAttribute("role")
                        && ["auto", "scroll"].includes(style.overflowY)
                        && element.scrollHeight > element.clientHeight + 1;
                      })
                    : empty;
              const bodies = bodyCandidates.filter((element) => {
                const rect = element.getBoundingClientRect();
                return rect.width >= 240 && rect.width <= 360
                  && rect.height >= 240 && rect.height <= 500;
              });
              const closes = empty.filter((element) => {
                const rect = element.getBoundingClientRect();
                return rect.width >= 20 && rect.width <= 64
                  && rect.height >= 20 && rect.height <= 64
                  && Math.abs(rect.width - rect.height) <= 8;
              });
              const matches = [];
              for (const body of bodies) {
                const bodyRect = body.getBoundingClientRect();
                for (const close of closes) {
                  if (accountNoticeChild) {
                    const semanticParent = close.parentElement?.closest("flt-semantics");
                    if (semanticParent !== body) continue;
                  } else {
                    const wrapper = body.parentElement?.closest("flt-semantics");
                    const wrapperRect = wrapper?.getBoundingClientRect();
                    const nestedScrollRelation = Boolean(
                      scrollBody
                      && wrapper
                      && wrapper !== body
                      && !wrapper.hasAttribute("flt-tappable")
                      && !wrapper.getAttribute("role")
                      && wrapper.parentElement === close.parentElement
                      && wrapper.nextElementSibling === close
                      && wrapperRect
                      && Math.abs(wrapperRect.left - bodyRect.left) <= 2
                      && Math.abs(wrapperRect.top - bodyRect.top) <= 2
                      && Math.abs(wrapperRect.width - bodyRect.width) <= 2
                      && Math.abs(wrapperRect.height - bodyRect.height) <= 2
                    );
                    if (!nestedScrollRelation) {
                      if (body.parentElement !== close.parentElement) continue;
                      if (
                        closeAbove
                          ? close.nextElementSibling !== body
                          : body.nextElementSibling !== close
                      ) continue;
                    }
                  }
                  const closeRect = close.getBoundingClientRect();
                  const centered = Math.abs(
                    (bodyRect.left + bodyRect.width / 2)
                    - (closeRect.left + closeRect.width / 2),
                  ) <= 4;
                  const gap = closeAbove
                    ? bodyRect.top - closeRect.bottom
                    : closeRect.top - bodyRect.bottom;
                  const topInset = closeRect.top - bodyRect.top;
                  const rightInset = bodyRect.right - closeRect.right;
                  const geometryMatched = imageBodyTopRight || accountNoticeChild
                    ? topInset >= 0
                      && topInset <= 48
                      && rightInset >= 0
                      && rightInset <= 48
                    : centered && gap >= 0 && gap <= 80;
                  const bodyZ = Number.parseInt(getComputedStyle(body).zIndex || "0", 10) || 0;
                  const closeZ = Number.parseInt(getComputedStyle(close).zIndex || "0", 10) || 0;
                  const centerTarget = document.elementFromPoint(
                    closeRect.left + closeRect.width / 2,
                    closeRect.top + closeRect.height / 2,
                  );
                  if (
                    geometryMatched
                    && (closeAbove || closeZ > bodyZ)
                    && centerTarget
                    && (centerTarget === close || close.contains(centerTarget))
                  ) {
                    matches.push({ body, close });
                  }
                }
              }
              if (matches.length !== 1) return { dialog_present: false };
              const dialogSelector = selectorFor(matches[0].body);
              const closeSelector = selectorFor(matches[0].close);
              if (!dialogSelector || !closeSelector) return { dialog_present: false };
              return {
                dialog_present: true,
                selector: closeSelector,
                kind: "close_control",
                dialog_selector: dialogSelector,
                dialog_signature: signatureFor(matches[0].body),
                verification_required: true,
              };
            }
            """,
            {
                "requireAppOpenLabel": require_app_open_label,
                "closeAbove": close_above,
                "scrollBody": scroll_body,
                "imageBodyTopRight": image_body_top_right,
                "accountNoticeChild": account_notice_child,
            },
        )
    except Exception:  # noqa: BLE001 - Flutter 增强失败必须保持失败关闭
        return None
    return result if isinstance(result, dict) else None


async def _wait_for_popup_dismissal(
    *,
    page: Any,
    popup_target: dict[str, Any],
    timeout_ms: int,
    timeout_error_type: type[Exception],
) -> bool:
    """只有原 Dialog 持续消失或被不同弹窗稳定替换后，才确认关闭成功。"""
    dialog_selector = str(popup_target.get("dialog_selector") or "").strip()
    dialog_signature = str(popup_target.get("dialog_signature") or "")
    wait_for_function = getattr(page, "wait_for_function", None)
    if not dialog_selector or not dialog_signature or not callable(wait_for_function):
        return False
    from services.api.app.infrastructure.browser.h5_modal_dismissal_target import (
        MODAL_LAYER_SHAPE_JS,
    )

    try:
        await wait_for_function(
            "({ selector, signature, stableMs, requireHidden, nodeToken,"
            " layerShape, layerMedia }) => {"
            + MODAL_LAYER_SHAPE_JS + """
              const dismissedBySignature = (
                window.__aliothPopupDismissalSinceBySignature ||= Object.create(null)
              );
              const stateKey = `${selector}|${signature}|${nodeToken || ''}`;
              let element = nodeToken
                ? window.__aliothSignedModalNodes?.get(nodeToken)
                : document.querySelector(selector);
              if (nodeToken && !element) return false;
              const replacement = document.querySelector(selector);
              // 每个样本核对异步下一层，而不是只在等待开始前检查一次。
              const pending = window.__aliothModalAdvanceChecks ||= new Set();
              if (nodeToken && !pending.has(nodeToken)) {
                pending.add(nodeToken);
                modalLayerAdvanced(element, replacement, nodeToken, layerMedia).then(result => {
                  if (result) (window.__aliothAdvancedModalNodes ||= new Map())
                    .set(nodeToken, replacement);
                }).finally(() => pending.delete(nodeToken));
              }
              const advanced = nodeToken && replacement
                && window.__aliothAdvancedModalNodes?.get(nodeToken) === replacement;
              if (layerShape && element && !element.isConnected) {
                if (replacement) {
                  const shape = modalLayerShape(replacement);
                  if (!shape || (shape === layerShape
                    && window.__aliothAdvancedModalNodes?.get(nodeToken) !== replacement)) {
                    element = replacement;
                  }
                }
              }
              let originalVisible = false;
              if (element instanceof Element && element.isConnected) {
                const style = getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                const visible = style.display !== "none"
                  && style.visibility !== "hidden"
                  && Number(style.opacity || 1) > 0
                  && rect.width > 0
                  && rect.height > 0;
                const currentSignature = JSON.stringify([
                element.tagName.toLowerCase(),
                element.getAttribute("role") || "",
                element.className || "",
                (element.innerText || element.textContent || "")
                  .replace(/\\s+/g, " ").trim().slice(0, 500),
                [...element.querySelectorAll("img[src]")]
                  .slice(0, 5)
                  .map((item) => item.getAttribute("src") || ""),
                [...element.querySelectorAll('.ad-item, .btn-close, .b-img, [class*="active"]')]
                  .slice(0, 30)
                  .map((item) => {
                    const style = getComputedStyle(item);
                    const rect = item.getBoundingClientRect();
                    return [
                      item.tagName.toLowerCase(),
                      item.className || "",
                      item.getAttribute("style") || "",
                      style.display,
                      style.visibility,
                      Number(style.opacity || 1) > 0,
                      rect.width > 0 && rect.height > 0,
                      item.getAttribute("data-track-ad-click") || "",
                      item.getAttribute("src") || "",
                    ];
                  }),
                ]);
                originalVisible = !advanced && visible
                  && (requireHidden || currentSignature === signature);
              }
              if (originalVisible) {
                delete dismissedBySignature[stateKey];
                return false;
              }
              const dismissedAt = dismissedBySignature[stateKey] ??= performance.now();
              return performance.now() - dismissedAt >= stableMs;
            }
            """,
            arg={
                "selector": dialog_selector,
                "signature": dialog_signature,
                "stableMs": 800,
                **({"layerMedia": popup_target["dialog_layer_media"]}
                   if popup_target.get("dialog_layer_media") else {}),
                **({"layerShape": popup_target["dialog_layer_shape"]}
                   if popup_target.get("dialog_layer_shape") else {}),
                **({"nodeToken": popup_target["dialog_node_token"]}
                   if popup_target.get("dialog_node_token") else {}),
                **({"requireHidden": True} if popup_target.get("require_hidden") is True else {}),
            },
            timeout=min(max(timeout_ms, 1), 2_300),
            polling=100,
        )
    except timeout_error_type:
        return False
    except Exception:  # noqa: BLE001  # 无法完成关闭后验证时必须保守失败
        return False
    finally:
        if popup_target.get("dialog_node_token"):
            try:
                await page.evaluate("""token => {
                    window.__aliothModalObservers?.get(token)?.disconnect();
                    window.__aliothModalObservers?.delete(token);
                }""", popup_target["dialog_node_token"])
            except Exception:  # noqa: BLE001  # 页面销毁不能掩盖原动作结果
                pass
    return True


async def _observe_popup_dismissal_outcome(
    *,
    page: Any,
    popup_target: dict[str, Any],
) -> str:
    """记录关闭后的原弹层状态，供 Action Trace 区分清空与下一层出现。"""
    dialog_selector = str(popup_target.get("dialog_selector") or "").strip()
    dialog_signature = str(popup_target.get("dialog_signature") or "")
    evaluate = getattr(page, "evaluate", None)
    if not dialog_selector or not dialog_signature or not callable(evaluate):
        return "popup_dismissal_observation_unavailable"
    from services.api.app.infrastructure.browser.h5_modal_dismissal_target import (
        MODAL_LAYER_SHAPE_JS,
    )

    try:
        outcome = await evaluate(
            "async ({ selector, signature, requireHidden, nodeToken, layerShape, layerMedia }) => {"
            + MODAL_LAYER_SHAPE_JS + """
              const element = nodeToken
                ? window.__aliothSignedModalNodes?.get(nodeToken)
                : document.querySelector(selector);
              if (nodeToken && !element) return "popup_dismissal_observation_unavailable";
              const next = document.querySelector(selector);
              if (nodeToken && await modalLayerAdvanced(element, next, nodeToken, layerMedia)) {
                return next.getClientRects().length
                  ? "next_popup_layer_visible" : "original_layer_hidden";
              }
              if (element && !element.isConnected) {
                const replacement = document.querySelector(selector);
                if (layerShape && replacement && replacement.getClientRects().length) {
                  const replacementStyle = getComputedStyle(replacement);
                  if (replacementStyle.display === "none"
                    || replacementStyle.visibility === "hidden"
                    || Number(replacementStyle.opacity || 1) <= 0) {
                    return "original_layer_absent";
                  }
                  const shape = modalLayerShape(replacement);
                  if (!shape) return "popup_dismissal_observation_unavailable";
                  if (shape === layerShape
                    && !await replacedModalMedia(replacement, nodeToken, layerMedia)) {
                    return "original_layer_still_visible";
                  }
                }
                return replacement && replacement.getClientRects().length
                  ? "next_popup_layer_visible" : "original_layer_absent";
              }
              if (!(element instanceof Element)) return "original_layer_absent";
              const style = getComputedStyle(element);
              const rect = element.getBoundingClientRect();
              const visible = style.display !== "none"
                && style.visibility !== "hidden"
                && Number(style.opacity || 1) > 0
                && rect.width > 0
                && rect.height > 0;
              const currentSignature = JSON.stringify([
                element.tagName.toLowerCase(),
                element.getAttribute("role") || "",
                element.className || "",
                (element.innerText || element.textContent || "")
                  .replace(/\\s+/g, " ").trim().slice(0, 500),
                [...element.querySelectorAll("img[src]")]
                  .slice(0, 5)
                  .map((item) => item.getAttribute("src") || ""),
                [...element.querySelectorAll('.ad-item, .btn-close, .b-img, [class*="active"]')]
                  .slice(0, 30)
                  .map((item) => {
                    const itemStyle = getComputedStyle(item);
                    const itemRect = item.getBoundingClientRect();
                    return [
                      item.tagName.toLowerCase(),
                      item.className || "",
                      item.getAttribute("style") || "",
                      itemStyle.display,
                      itemStyle.visibility,
                      Number(itemStyle.opacity || 1) > 0,
                      itemRect.width > 0 && itemRect.height > 0,
                      item.getAttribute("data-track-ad-click") || "",
                      item.getAttribute("src") || "",
                    ];
                  }),
              ]);
              if (!visible) return "original_layer_hidden";
              if (requireHidden) return "original_layer_still_visible";
              if (currentSignature === signature) return "original_layer_still_visible";
              return "next_popup_layer_visible";
            }
            """,
            {
                "selector": dialog_selector,
                "signature": dialog_signature,
                "layerMedia": popup_target.get("dialog_layer_media"),
                **({"layerShape": popup_target["dialog_layer_shape"]}
                   if popup_target.get("dialog_layer_shape") else {}),
                **({"nodeToken": popup_target["dialog_node_token"]}
                   if popup_target.get("dialog_node_token") else {}),
                **({"requireHidden": True} if popup_target.get("require_hidden") is True else {}),
            },
        )
    except Exception:  # noqa: BLE001 - 审计增强失败不能覆盖原关闭结果
        return "popup_dismissal_observation_unavailable"
    allowed = {
        "original_layer_absent",
        "original_layer_hidden",
        "next_popup_layer_visible",
        "original_layer_still_visible",
    }
    return outcome if outcome in allowed else "popup_dismissal_observation_unavailable"


async def _resolve_exact_interactive_text_target(
    *,
    page: Any,
    selector: str,
) -> dict[str, Any] | None:
    """把文本点击收敛到唯一、可见且中心命中真实元素的交互控件。"""
    normalized = selector.strip()
    if not normalized.casefold().startswith("text="):
        return None
    text = normalized[5:].strip().strip("'\"")
    if not text:
        return None
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return None
    try:
        result = await evaluate(
            """
            (expectedText) => {
              const isVisible = (element) => {
                const style = getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.display !== "none"
                  && style.visibility !== "hidden"
                  && Number(style.opacity || 1) > 0
                  && rect.width > 0
                  && rect.height > 0;
              };
              const selectorFor = (element) => {
                if (element.id) return `#${CSS.escape(element.id)}`;
                for (const attribute of ["data-testid", "aria-label", "title"]) {
                  const value = element.getAttribute(attribute);
                  if (!value) continue;
                  const candidate = (
                    `${element.tagName.toLowerCase()}`
                    + `[${attribute}="${CSS.escape(value)}"]`
                  );
                  if (document.querySelectorAll(candidate).length === 1) return candidate;
                }
                const parts = [];
                let current = element;
                while (current instanceof Element && current !== document.body) {
                  const tag = current.tagName.toLowerCase();
                  const parent = current.parentElement;
                  if (!parent) break;
                  const siblings = [...parent.children].filter(
                    (item) => item.tagName === current.tagName
                  );
                  const index = Math.max(siblings.indexOf(current), 0) + 1;
                  parts.unshift(`${tag}:nth-of-type(${index})`);
                  current = parent;
                }
                return parts.length ? `body > ${parts.join(" > ")}` : null;
              };
              const controls = [
                ...document.querySelectorAll(
                  'button, [role="button"], a, input[type="button"], '
                  + 'input[type="submit"], input[type="reset"]'
                ),
              ].filter((element) => {
                if (!isVisible(element)) return false;
                const label = (
                  element.innerText
                  || element.value
                  || element.getAttribute("aria-label")
                  || ""
                ).trim();
                return label === expectedText;
              });
              if (controls.length !== 1) {
                return { matched: controls.length > 0, selector: null, actionable: false };
              }
              const control = controls[0];
              const rect = control.getBoundingClientRect();
              const hit = document.elementFromPoint(
                rect.left + rect.width / 2,
                rect.top + rect.height / 2
              );
              const actionable = Boolean(
                hit && (control === hit || control.contains(hit))
              );
              const obstruction = (
                !actionable && hit instanceof Element
                  ? {
                      tag: hit.tagName.toLowerCase(),
                      role: hit.getAttribute("role"),
                      class_tokens: [...hit.classList]
                        .slice(0, 8)
                        .map((item) => item.slice(0, 100)),
                    }
                  : null
              );
              return {
                matched: true,
                selector: selectorFor(control),
                actionable,
                obstruction,
              };
            }
            """,
            text,
        )
    except Exception:  # noqa: BLE001
        return None
    return result if isinstance(result, dict) else None


async def _wait_for_loading_layer_before_overlay_click(
    *,
    page: Any,
    timeout_ms: int,
    action_trace: list[dict[str, Any]],
    timeout_error_type: type[Exception],
) -> bool:
    """等待顶层线路加载壳退出，避免把底层广告遮罩误当作当前可点目标。"""
    locator = page.locator(".loading-page:visible")
    count_method = getattr(locator, "count", None)
    if not callable(count_method) or await count_method() < 1:
        return False
    wait_for = getattr(locator, "wait_for", None)
    if not callable(wait_for):
        return False
    action_trace[-1]["waited_for"] = ".loading-page:visible"
    try:
        await wait_for(state="hidden", timeout=timeout_ms)
    except timeout_error_type:
        # 加载壳仍在时不能误点底层广告遮罩；保留当前采集让页面识别
        # 继续驱动同会话 observe，而不是把它写成广告关闭失败。
        return True
    return False


async def _wait_for_visible_selector_to_hide(
    *,
    page: Any,
    selector: str,
    timeout_ms: int,
    timeout_error_type: type[Exception],
    verification_timeout_ms: int = 1_500,
    stable_absence_ms: int = 0,
) -> bool:
    """点击关闭控件后等待对应顶层容器真正消失，并可要求持续缺席。"""
    locator = page.locator(selector)
    count_method = getattr(locator, "count", None)
    has_visible_match = not callable(count_method) or await count_method() > 0
    if has_visible_match:
        wait_for = getattr(locator, "wait_for", None)
        if not callable(wait_for):
            return False
        try:
            await wait_for(
                state="hidden",
                timeout=min(max(timeout_ms, 1), max(verification_timeout_ms, 1)),
            )
        except timeout_error_type:
            return False
        except Exception:  # noqa: BLE001  # 关闭后验证失败必须保守处理
            return False
    if stable_absence_ms <= 0:
        return True
    wait_for_timeout = getattr(page, "wait_for_timeout", None)
    if not callable(wait_for_timeout) or not callable(count_method):
        return False
    elapsed_ms = 0
    while elapsed_ms < stable_absence_ms:
        interval_ms = min(100, stable_absence_ms - elapsed_ms)
        await wait_for_timeout(interval_ms)
        elapsed_ms += interval_ms
        if await count_method() > 0:
            return False
    return True


async def _selector_has_visible_match(page: Any, selector: str) -> bool:
    """兼容测试替身地判断触发目标在加载转场后是否仍可见。"""
    locator = page.locator(selector)
    count_method = getattr(locator, "count", None)
    if not callable(count_method):
        return True
    return await count_method() > 0


async def _click_overlay_with_pointer_interception_fallback(
    *,
    locator: Any,
    position: dict[str, int],
    timeout_ms: int,
    timeout_error_type: type[Exception],
) -> bool:
    """仅在覆盖层被子元素截获指针时，直接向已证明的覆盖层派发关闭事件。"""
    try:
        await locator.click(position=position, timeout=timeout_ms)
    except timeout_error_type as exc:
        if "intercepts pointer events" not in str(exc).casefold():
            raise
        evaluate = getattr(locator, "evaluate", None)
        if not callable(evaluate):
            raise
        await evaluate("(element) => element.click()")
        return True
    return False


async def _click_popup_close_control_with_offscreen_fallback(
    *,
    page: Any,
    locator: Any,
    timeout_ms: int,
    timeout_error_type: type[Exception],
) -> bool:
    """对已证明唯一、但原生点击不可动作的弹窗关闭控件受限派发点击。"""
    bounding_box = getattr(locator, "bounding_box", None)
    if not callable(bounding_box):
        return await _click_verified_popup_close_or_dispatch_after_timeout(
            locator=locator,
            timeout_ms=timeout_ms,
            timeout_error_type=timeout_error_type,
        )
    viewport = await _resolve_page_viewport_size(page)
    box = await bounding_box()
    if viewport is None or _box_center_in_viewport(box, viewport):
        return await _click_verified_popup_close_or_dispatch_after_timeout(
            locator=locator,
            timeout_ms=timeout_ms,
            timeout_error_type=timeout_error_type,
        )

    scroll_into_view = getattr(locator, "scroll_into_view_if_needed", None)
    if callable(scroll_into_view):
        try:
            await scroll_into_view(
                timeout=min(timeout_ms, OBSTRUCTION_CHAIN_CLICK_TIMEOUT_MS),
            )
        except timeout_error_type:
            pass
        refreshed_box = await bounding_box()
        if _box_center_in_viewport(refreshed_box, viewport):
            return await _click_verified_popup_close_or_dispatch_after_timeout(
                locator=locator,
                timeout_ms=timeout_ms,
                timeout_error_type=timeout_error_type,
            )

    evaluate = getattr(locator, "evaluate", None)
    if not callable(evaluate):
        await locator.click(timeout=timeout_ms)
        return False
    await evaluate("(element) => element.click()")
    return True


async def _click_verified_popup_close_or_dispatch_after_timeout(
    *,
    locator: Any,
    timeout_ms: int,
    timeout_error_type: type[Exception],
) -> bool:
    """原生点击超时时，仅向已经过顶层弹窗结构验签的关闭控件派发点击。"""
    try:
        await locator.click(timeout=timeout_ms)
    except timeout_error_type:
        evaluate = getattr(locator, "evaluate", None)
        if not callable(evaluate):
            raise
        await evaluate("(element) => element.click()")
        return True
    return False


async def _hide_verified_high_z_popup_obstruction(
    *,
    page: Any,
    popup_target: dict[str, Any],
) -> dict[str, Any]:
    """仅隐藏已证明原 Dialog 及其 z-index 不低于 500 的配套蒙版。"""
    dialog_selector = str(popup_target.get("dialog_selector") or "").strip()
    dialog_signature = str(popup_target.get("dialog_signature") or "")
    evaluate = getattr(page, "evaluate", None)
    if not dialog_selector or not dialog_signature or not callable(evaluate):
        return {"hidden": False}
    try:
        result = await evaluate(
            """
            ({ selector, signature, minZIndex }) => {
              const isVisible = (element) => {
                const style = getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.display !== "none"
                  && style.visibility !== "hidden"
                  && Number(style.opacity || 1) > 0
                  && rect.width > 0
                  && rect.height > 0;
              };
              const signatureFor = (element) => JSON.stringify([
                element.tagName.toLowerCase(),
                element.getAttribute("role") || "",
                element.className || "",
                (element.innerText || element.textContent || "")
                  .replace(/\\s+/g, " ").trim().slice(0, 500),
                [...element.querySelectorAll("img[src]")]
                  .slice(0, 5)
                  .map((item) => item.getAttribute("src") || ""),
                [...element.querySelectorAll('.ad-item, .btn-close, .b-img, [class*="active"]')]
                  .slice(0, 30)
                  .map((item) => {
                    const style = getComputedStyle(item);
                    const rect = item.getBoundingClientRect();
                    return [
                      item.tagName.toLowerCase(),
                      item.className || "",
                      item.getAttribute("style") || "",
                      style.display,
                      style.visibility,
                      Number(style.opacity || 1) > 0,
                      rect.width > 0 && rect.height > 0,
                      item.getAttribute("data-track-ad-click") || "",
                      item.getAttribute("src") || "",
                    ];
                  }),
              ]);
              const dialog = document.querySelector(selector);
              if (!(dialog instanceof Element) || !isVisible(dialog)) {
                return { hidden: false, reason: "dialog_missing" };
              }
              if (signatureFor(dialog) !== signature) {
                return { hidden: false, reason: "dialog_signature_changed" };
              }
              const associated = [
                dialog,
                ...document.querySelectorAll('.van-overlay'),
              ].filter((element, index, values) => (
                element instanceof Element
                && values.indexOf(element) === index
                && isVisible(element)
              ));
              const zIndexes = associated.map((element) => {
                const parsed = Number.parseFloat(getComputedStyle(element).zIndex || "");
                return Number.isFinite(parsed) ? parsed : 0;
              });
              const maxZIndex = Math.max(...zIndexes, 0);
              if (maxZIndex < minZIndex) {
                return { hidden: false, reason: "z_index_below_threshold", max_z_index: maxZIndex };
              }
              for (const element of associated) {
                element.style.setProperty("display", "none", "important");
                element.style.setProperty("visibility", "hidden", "important");
                element.style.setProperty("pointer-events", "none", "important");
                element.setAttribute("aria-hidden", "true");
              }
              return {
                hidden: true,
                hidden_count: associated.length,
                max_z_index: maxZIndex,
              };
            }
            """,
            {
                "selector": dialog_selector,
                "signature": dialog_signature,
                "minZIndex": 500,
            },
        )
    except Exception:  # noqa: BLE001  # DOM 兜底失败时仍保守返回原关闭失败
        return {"hidden": False}
    return result if isinstance(result, dict) else {"hidden": False}


async def _click_startup_ad_with_pointer_interception_fallback(
    *,
    page: Any,
    selector: str,
    timeout_ms: int,
    action_trace: list[dict[str, Any]],
    timeout_error_type: type[Exception],
) -> None:
    """开屏关闭控件优先走 Locator，失败后仅使用已验签结构的中心坐标。"""
    before_coordinate_target = await _verified_startup_ad_coordinate_target(
        page=page,
        selector=selector,
    )
    try:
        await page.click(selector, timeout=timeout_ms)
    except timeout_error_type as exc:
        coordinate_target = await _verified_startup_ad_coordinate_target(
            page=page,
            selector=selector,
        )
        if _same_startup_ad_coordinate_target(
            before=before_coordinate_target,
            after=coordinate_target,
        ):
            assert coordinate_target is not None
            mouse = getattr(page, "mouse", None)
            mouse_click = getattr(mouse, "click", None)
            if callable(mouse_click):
                await mouse_click(
                    coordinate_target["center_x"],
                    coordinate_target["center_y"],
                )
                action_trace[-1].update(
                    {
                        "resolved_by": (
                            "startup_ad_verified_text_coordinate_after_locator_failure"
                        ),
                        "click_coordinates": {
                            "x": coordinate_target["center_x"],
                            "y": coordinate_target["center_y"],
                        },
                        "target_bounds": {
                            "x": coordinate_target["x"],
                            "y": coordinate_target["y"],
                            "width": coordinate_target["width"],
                            "height": coordinate_target["height"],
                        },
                    }
                )
                return
        if "intercepts pointer events" not in str(exc).casefold():
            raise
        evaluate = getattr(page.locator(selector), "evaluate", None)
        if not callable(evaluate):
            raise
        await evaluate("(element) => element.click()")
        action_trace[-1]["resolved_by"] = "startup_ad_direct_dispatch_after_pointer_interception"


async def _retry_h5_startup_ad_direct_dispatch(
    *,
    page: Any,
    selector: str,
    timeout_ms: int,
    timeout_error_type: type[Exception],
    action_trace: list[dict[str, Any]],
) -> bool:
    """H5 开屏控件真实点击无响应时，在重新验签后补一次原生 click。"""
    coordinate_target = await _verified_startup_ad_coordinate_target(
        page=page,
        selector=selector,
    )
    if coordinate_target is None:
        return False
    locator = page.locator(selector)
    evaluate = getattr(locator, "evaluate", None)
    if not callable(evaluate):
        return False
    await evaluate("(element) => element.click()")
    dismissed = await _wait_for_visible_selector_to_hide(
        page=page,
        selector=STARTUP_AD_LAYER_SELECTOR_BY_DISMISSAL[selector],
        timeout_ms=timeout_ms,
        timeout_error_type=timeout_error_type,
        verification_timeout_ms=STARTUP_AD_DISMISSAL_VERIFICATION_TIMEOUT_MS,
        stable_absence_ms=(800 if selector == POPUP_SKIP_WRAP_DISMISSAL_SELECTOR else 0),
    )
    action_trace[-1].update(
        {
            "startup_ad_direct_dispatch_retried": True,
            "startup_ad_direct_dispatch_verified": dismissed,
        }
    )
    return dismissed


async def _verified_startup_ad_coordinate_target(
    *,
    page: Any,
    selector: str,
) -> dict[str, float | str] | None:
    """只为当前唯一、可见且中心命中的已登记开屏结构返回视口坐标。"""
    text_contract = STARTUP_AD_COORDINATE_TEXT_CONTRACT_BY_DISMISSAL.get(selector)
    if text_contract is None:
        return None
    try:
        locator = page.locator(selector)
        count = getattr(locator, "count", None)
        evaluate = getattr(locator, "evaluate", None)
        if not callable(count) or not callable(evaluate) or await count() != 1:
            return None
        proof = await evaluate(
            """
            (element, contract) => {
              const normalize = (value) => String(value || "").replace(/\\s+/g, "").toLowerCase();
              const expectedText = contract.expected_text;
              let target = element;
              if (contract.use_direct_child) {
                const matches = Array.from(element.children).filter((child) => (
                  child.tagName === "DIV"
                    && normalize(child.textContent) === normalize(expectedText)
                ));
                if (matches.length !== 1) return { verified: false };
                target = matches[0];
              } else if (
                expectedText !== null
                  && normalize(element.textContent) !== normalize(expectedText)
              ) {
                return { verified: false };
              }
              const rect = target.getBoundingClientRect();
              const style = getComputedStyle(target);
              const centerX = rect.left + rect.width / 2;
              const centerY = rect.top + rect.height / 2;
              const hit = document.elementFromPoint(centerX, centerY);
              const disabled = target.disabled === true
                || target.getAttribute("aria-disabled") === "true"
                || element.disabled === true
                || element.getAttribute("aria-disabled") === "true";
              const visible = style.display !== "none"
                && style.visibility !== "hidden"
                && Number(style.opacity || "1") > 0
                && rect.width > 0
                && rect.height > 0
                && rect.right > 0
                && rect.bottom > 0
                && rect.left < window.innerWidth
                && rect.top < window.innerHeight;
              let tokens = globalThis.__aliothStartupAdCoordinateTokens;
              if (!(tokens instanceof WeakMap)) {
                tokens = new WeakMap();
                Object.defineProperty(globalThis, "__aliothStartupAdCoordinateTokens", {
                  value: tokens,
                  configurable: true,
                });
              }
              let token = tokens.get(element);
              if (!token) {
                const next = Number(globalThis.__aliothStartupAdCoordinateTokenSeq || 0) + 1;
                globalThis.__aliothStartupAdCoordinateTokenSeq = next;
                token = `startup-element-${next}`;
                tokens.set(element, token);
              }
              const layer = element.closest(
                ".van-popup,.loading-page,.globalLaunch,.wrapper,.launch-page,.count-down"
              ) || element;
              const layerPayload = [
                layer.tagName,
                layer.id,
                layer.className,
                normalize(layer.textContent),
                ...Array.from(layer.querySelectorAll("img,video")).slice(0, 3).map((item) => (
                  item.currentSrc || item.getAttribute("src") || ""
                )),
                getComputedStyle(layer).backgroundImage || "",
              ].join("|");
              let layerHash = 2166136261;
              for (let index = 0; index < layerPayload.length; index += 1) {
                layerHash ^= layerPayload.charCodeAt(index);
                layerHash = Math.imul(layerHash, 16777619);
              }
              return {
                visible,
                enabled: !disabled,
                center_hits_target: hit === target || target.contains(hit),
                element_token: token,
                layer_signature: `fnv1a:${(layerHash >>> 0).toString(16).padStart(8, "0")}`,
                x: rect.left,
                y: rect.top,
                width: rect.width,
                height: rect.height,
              };
            }
            """,
            dict(text_contract),
        )
    except Exception:  # noqa: BLE001  # 结构或页面在复验中漂移时失败关闭
        return None
    if not isinstance(proof, Mapping):
        return None
    if not (
        proof.get("visible") is True
        and proof.get("enabled") is True
        and proof.get("center_hits_target") is True
    ):
        return None
    element_token = str(proof.get("element_token") or "").strip()
    layer_signature = str(proof.get("layer_signature") or "").strip()
    if not element_token or not layer_signature:
        return None
    numeric: dict[str, float] = {}
    for key in ("x", "y", "width", "height"):
        value = proof.get(key)
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        normalized = float(value)
        if not math.isfinite(normalized):
            return None
        numeric[key] = normalized
    if numeric["width"] <= 0 or numeric["height"] <= 0:
        return None
    center_x = numeric["x"] + numeric["width"] / 2
    center_y = numeric["y"] + numeric["height"] / 2
    if center_x < 0 or center_y < 0:
        return None
    return {
        **numeric,
        "element_token": element_token,
        "layer_signature": layer_signature,
        "center_x": center_x,
        "center_y": center_y,
    }


def _same_startup_ad_coordinate_target(
    *,
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
) -> bool:
    """确认 Locator 超时前后仍是同一个广告层，避免一次动作连点两层。"""
    if before is None or after is None:
        return False
    return bool(
        before.get("element_token") == after.get("element_token")
        and before.get("layer_signature") == after.get("layer_signature")
    )


async def _click_flutter_bottom_navigation(
    *,
    page: Any,
    navigation_index: int,
    label_hash: str,
    timeout_ms: int,
    timeout_error_type: type[Exception],
    action_trace: list[dict[str, Any]],
) -> None:
    """实时复核 Flutter 底栏同一行，并验证受限点击确实改变页面语义状态。"""
    target = await _resolve_flutter_bottom_navigation_target(
        page=page,
        navigation_index=navigation_index,
        label_hash=label_hash,
    )
    selector = str(target.get("selector") or "").strip()
    if target.get("row_count") != 1 or not selector:
        raise BlockedPageCaptureActionError(
            "当前页面未唯一证明已验签的 Flutter 底部导航目标。",
            error_code="FLUTTER_BOTTOM_NAVIGATION_NOT_FOUND",
        )
    before_signature = await _flutter_semantics_state_signature(page)
    locator = page.locator(selector)
    used_dispatch = False
    try:
        await locator.click(timeout=min(timeout_ms, 1_500))
    except timeout_error_type:
        dispatch_event = getattr(locator, "dispatch_event", None)
        if callable(dispatch_event):
            await dispatch_event("click")
        else:
            evaluate = getattr(locator, "evaluate", None)
            if not callable(evaluate):
                raise
            await evaluate("(element) => element.click()")
        used_dispatch = True
    wait_for_timeout = getattr(page, "wait_for_timeout", None)
    effect_observed = False
    for _ in range(20):
        after_signature = await _flutter_semantics_state_signature(page)
        if before_signature and after_signature and after_signature != before_signature:
            effect_observed = True
            break
        if not callable(wait_for_timeout):
            break
        await wait_for_timeout(250)
    action_trace[-1].update(
        {
            "requested_selector": action_trace[-1].get("selector"),
            "selector": selector,
            "resolved_by": (
                "flutter_bottom_navigation_verified_dispatch"
                if used_dispatch
                else "flutter_bottom_navigation_verified_native_click"
            ),
            "navigation_effect_verified": effect_observed,
        }
    )
    if not effect_observed:
        raise BlockedPageCaptureActionError(
            "Flutter 底部导航点击后未观察到页面语义状态变化。",
            error_code="FLUTTER_BOTTOM_NAVIGATION_NO_EFFECT",
        )


async def _click_flutter_profile_login_entry(
    *,
    page: Any,
    timeout_ms: int,
    timeout_error_type: type[Exception],
    action_trace: list[dict[str, Any]],
) -> None:
    """实时复核 Flutter 游客登录组并验证点击确实切换认证页面。"""
    target = await _resolve_flutter_profile_login_entry_target(page)
    selector = str(target.get("selector") or "").strip()
    if target.get("match_count") != 1 or not selector:
        action_trace[-1]["result"] = "flutter_profile_login_entry_not_proven"
        raise BlockedPageCaptureActionError(
            "当前页面未唯一证明 Flutter 个人中心登录入口。",
            error_code="FLUTTER_PROFILE_LOGIN_ENTRY_NOT_FOUND",
        )
    before_signature = await _flutter_semantics_state_signature(page)
    locator = page.locator(selector)
    used_dispatch = False
    try:
        await locator.click(timeout=min(timeout_ms, 1_500))
    except timeout_error_type:
        await locator.dispatch_event("click")
        used_dispatch = True
    effect_observed = False
    for _ in range(10):
        await asyncio.sleep(0.2)
        after_signature = await _flutter_semantics_state_signature(page)
        if after_signature and after_signature != before_signature:
            effect_observed = True
            break
    action_trace[-1].update(
        {
            "requested_selector": H5_FLUTTER_PROFILE_LOGIN_RUNTIME_SELECTOR,
            "selector": selector,
            "resolved_by": (
                "flutter_profile_login_verified_dispatch"
                if used_dispatch
                else "flutter_profile_login_verified_native_click"
            ),
            "navigation_effect_verified": effect_observed,
        }
    )
    if not effect_observed:
        action_trace[-1]["result"] = "flutter_profile_login_entry_no_effect"
        raise BlockedPageCaptureActionError(
            "Flutter 个人中心登录入口点击后未观察到认证页面变化。",
            error_code="FLUTTER_PROFILE_LOGIN_ENTRY_NO_EFFECT",
        )


async def _resolve_flutter_profile_login_entry_target(page: Any) -> dict[str, Any]:
    """只按顶部完整游客身份组实时绑定 Flutter 登录入口。"""
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return {"match_count": 0, "selector": None}
    try:
        result = await evaluate(
            r"""
            () => {
              const visible = (element) => {
                const style = getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.display !== "none"
                  && style.visibility !== "hidden"
                  && Number(style.opacity || 1) > 0
                  && rect.width > 0 && rect.height > 0;
              };
              const selectorFor = (element) => {
                if (!(element instanceof Element) || !element.id) return null;
                const selector = `#${CSS.escape(element.id)}`;
                return document.querySelectorAll(selector).length === 1 ? selector : null;
              };
              const matches = [...document.querySelectorAll(
                'flt-semantics-host flt-semantics[flt-tappable][role="group"]',
              )].filter((element) => {
                if (!visible(element)) return false;
                const label = String(
                  element.getAttribute("aria-label")
                    || element.innerText
                    || element.textContent
                    || "",
                ).replace(/\s+/g, " ").trim();
                const rect = element.getBoundingClientRect();
                const parent = element.parentElement?.closest("flt-semantics");
                return (label.match(/立即登录/g) || []).length === 1
                  && /\bID\s*:\s*[A-Za-z0-9]+\b/i.test(label)
                  && parent
                  && !parent.hasAttribute("flt-tappable")
                  && rect.left >= 0 && rect.left <= 32
                  && rect.top >= 0 && rect.top <= 120
                  && rect.width >= 320 && rect.width <= 390
                  && rect.height >= 45 && rect.height <= 90;
              });
              const selected = matches.length === 1 ? matches[0] : null;
              return {
                match_count: matches.length,
                selector: selected ? selectorFor(selected) : null,
              };
            }
            """
        )
    except Exception:  # noqa: BLE001 - 实时结构复核失败必须保持失败关闭
        return {"match_count": 0, "selector": None}
    return result if isinstance(result, dict) else {"match_count": 0, "selector": None}


async def _resolve_flutter_bottom_navigation_target(
    *,
    page: Any,
    navigation_index: int,
    label_hash: str,
) -> dict[str, Any]:
    """按底部几何行与横向顺序实时复核 Flutter 导航节点。"""
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return {"row_count": 0, "selector": None}
    try:
        result = await evaluate(
            r"""
            ({ navigationIndex }) => {
              const visible = (element) => {
                const style = getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.display !== "none"
                  && style.visibility !== "hidden"
                  && Number(style.opacity || 1) > 0
                  && rect.width > 0 && rect.height > 0;
              };
              const selectorFor = (element) => {
                if (!(element instanceof Element) || !element.id) return null;
                const selector = `#${CSS.escape(element.id)}`;
                return document.querySelectorAll(selector).length === 1 ? selector : null;
              };
              const candidates = [
                ...document.querySelectorAll(
                  "flt-semantics-host flt-semantics[flt-tappable]",
                ),
              ].filter((element) => {
                if (!visible(element)) return false;
                const rect = element.getBoundingClientRect();
                return rect.width >= 20 && rect.width <= 100
                  && rect.height >= 36 && rect.height <= 80
                  && rect.left >= -4 && rect.right <= window.innerWidth + 4
                  && rect.top >= window.innerHeight * 0.82
                  && rect.bottom <= window.innerHeight + 8
                  && String(element.innerText || element.textContent || "").trim();
              }).map((element) => ({ element, rect: element.getBoundingClientRect() }));
              const rows = [];
              for (const candidate of candidates.sort((left, right) => (
                left.rect.top - right.rect.top || left.rect.left - right.rect.left
              ))) {
                let row = rows.find((item) => Math.abs(item[0].rect.top - candidate.rect.top) <= 4);
                if (!row) {
                  row = [];
                  rows.push(row);
                }
                row.push(candidate);
              }
              const validRows = rows
                .map((row) => row.sort((left, right) => left.rect.left - right.rect.left))
                .filter((row) => row.length >= 5 && row.length <= 10
                  && row[row.length - 1].rect.left - row[0].rect.left >= 220);
              if (validRows.length !== 1 || navigationIndex > validRows[0].length) {
                return { row_count: validRows.length, selector: null };
              }
              const selected = validRows[0][navigationIndex - 1].element;
              return {
                row_count: 1,
                selector: selectorFor(selected),
                label: String(selected.innerText || selected.textContent || "")
                  .replace(/\s+/g, " ").trim(),
              };
            }
            """,
            {"navigationIndex": navigation_index},
        )
    except Exception:  # noqa: BLE001 - 实时结构复核失败必须保持失败关闭
        return {"row_count": 0, "selector": None}
    if not isinstance(result, dict):
        return {"row_count": 0, "selector": None}
    if h5_flutter_bottom_navigation_label_hash(str(result.get("label") or "")) != label_hash:
        return {**result, "selector": None, "label_mismatch": True}
    return result


async def _flutter_semantics_state_signature(page: Any) -> str:
    """生成不含正文的有界 Flutter 语义状态摘要，仅用于动作效果验证。"""
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return ""
    try:
        result = await evaluate(
            r"""
            () => {
              const text = String(
                document.querySelector("flt-semantics-host")?.innerText || ""
              ).replace(/\s+/g, " ").trim().slice(0, 50000);
              let hash = 2166136261;
              for (let index = 0; index < text.length; index += 1) {
                hash ^= text.charCodeAt(index);
                hash = Math.imul(hash, 16777619);
              }
              const nodeCount = document.querySelectorAll(
                "flt-semantics-host flt-semantics",
              ).length;
              return `${location.href}|${nodeCount}|${(hash >>> 0).toString(16)}`;
            }
            """
        )
    except Exception:  # noqa: BLE001 - 无法验效时不得假定动作成功
        return ""
    return str(result or "")


async def _click_with_bottom_navigation_fallback(
    *,
    page: Any,
    selector: str,
    timeout_ms: int,
    action_trace: list[dict[str, Any]],
    timeout_error_type: type[Exception],
    navigation_family: str | None = None,
    navigation_label: str | None = None,
    verified_card_activation: str | None = None,
    expected_rect: dict[str, float] | None = None,
) -> None:
    """仅对确定性底部导航 selector 处理 Playwright 可见性误判。

    动态页面在长时间规则执行后可能短暂让 ``:visible`` 过滤返回空集合，
    但对应 Tab 仍唯一存在于当前 DOM。只有平台生成的精确 ``nth-of-type``
    selector 且去除可见性过滤后恰好命中一个元素时，才允许受限强制点击；
    其他点击保持 Playwright 原始动作性门禁。
    """
    if navigation_family == H5_BOTTOM_NAVIGATION_FAMILY_ADAPTIVE:
        target_locator = page.locator(selector)
        if await target_locator.count() != 1:
            raise PageInteractionFailedError(
                "H5 自适应底部导航执行前不再唯一，已拒绝点击",
                error_code="SEMANTIC_TARGET_AMBIGUOUS",
            )
        before_url = str(getattr(page, "url", "") or "")
        before_activation = await _bottom_navigation_activation_state(target_locator)
        try:
            if await _click_bottom_navigation_with_url_change_watch(
                page=page,
                target_locator=target_locator,
                before_url=before_url,
                timeout_ms=min(timeout_ms, BOTTOM_NAVIGATION_NATIVE_CLICK_TIMEOUT_MS),
                action_trace=action_trace,
            ):
                return
        except timeout_error_type as exc:
            raise PageInteractionFailedError(
                "H5 自适应底部导航目标不可点击",
                error_code="CLICK_TARGET_NOT_ACTIONABLE",
            ) from exc
        if await _bottom_navigation_effect_observed(
            page=page,
            target_locator=target_locator,
            before_url=before_url,
            timeout_ms=min(timeout_ms, BOTTOM_NAVIGATION_EFFECT_TIMEOUT_MS),
            before_activation_state=before_activation,
            require_observable_activation=True,
        ):
            after_activation = await _bottom_navigation_activation_state(target_locator)
            before_activation_hash = _bottom_navigation_activation_digest(before_activation)
            after_activation_hash = _bottom_navigation_activation_digest(after_activation)
            if (
                before_activation_hash is None
                or after_activation_hash is None
                or before_activation_hash == after_activation_hash
            ):
                raise BlockedPageCaptureActionError(
                    "H5 自适应底部导航缺少可审计的激活状态变化",
                    error_code="BOTTOM_NAVIGATION_TRANSITION_NOT_OBSERVED",
                )
            action_trace[-1].update(
                {
                    "navigation_effect_verified": True,
                    "navigation_effect_reason": "activation_state_change",
                    "activation_before_sha256": before_activation_hash,
                    "activation_after_sha256": after_activation_hash,
                }
            )
            return
        raise BlockedPageCaptureActionError(
            "H5 自适应底部导航点击未产生路由或激活状态变化",
            error_code="BOTTOM_NAVIGATION_TRANSITION_NOT_OBSERVED",
        )

    matched = h5_match_bottom_navigation_target(selector)
    scoped_pages_foot_result = await _click_main_tab_video_pages_foot_community(
        page=page,
        matched=matched,
        navigation_family=navigation_family,
        navigation_label=navigation_label,
        timeout_ms=timeout_ms,
        action_trace=action_trace,
        timeout_error_type=timeout_error_type,
    )
    if scoped_pages_foot_result is True:
        return
    if matched is None or ":visible" not in selector:
        try:
            if await _click_first_viewport_intersecting_match(
                page=page,
                selector=selector,
                timeout_ms=timeout_ms,
                action_trace=action_trace,
                verified_card_activation=verified_card_activation,
                expected_rect=expected_rect,
            ):
                return
            await page.click(selector, timeout=timeout_ms)
        except timeout_error_type as exc:
            diagnostics = await _diagnose_click_target(
                page=page,
                selector=selector,
            )
            error_code = (
                "CLICK_TARGET_NOT_FOUND"
                if diagnostics.get("match_count") == 0
                else "CLICK_TARGET_NOT_ACTIONABLE"
            )
            action_trace[-1].update(
                {
                    "result": error_code.lower(),
                    "click_diagnostics": diagnostics,
                }
            )
            raise PageInteractionFailedError(
                _click_target_failure_message(
                    selector=selector,
                    error_code=error_code,
                    diagnostics=diagnostics,
                ),
                error_code=error_code,
            ) from exc
        return

    visible_locator = page.locator(selector)
    native_click_error: Exception | None = None
    fallback_reason = "visible_filter_miss"
    if await visible_locator.count() == 1:
        before_native_url = str(getattr(page, "url", "") or "")
        try:
            if await _click_bottom_navigation_with_url_change_watch(
                page=page,
                target_locator=visible_locator,
                before_url=before_native_url,
                timeout_ms=min(timeout_ms, BOTTOM_NAVIGATION_NATIVE_CLICK_TIMEOUT_MS),
                action_trace=action_trace,
            ):
                return
            if await _bottom_navigation_effect_observed(
                page=page,
                target_locator=visible_locator,
                before_url=before_native_url,
                timeout_ms=min(timeout_ms, BOTTOM_NAVIGATION_EFFECT_TIMEOUT_MS),
            ):
                _record_bottom_navigation_url_change_effect(
                    action_trace=action_trace,
                    before_url=before_native_url,
                    after_url=str(getattr(page, "url", "") or ""),
                )
                return
            fallback_reason = "visible_click_no_effect"
        except timeout_error_type as exc:
            # Playwright 可能找到 :visible 节点，但因覆盖关系或瞬时布局持续
            # 等待动作性直到超时。该分支仍必须通过无 visible selector 的
            # DOM 唯一性校验，不能把通用点击错误直接降级为 force。
            native_click_error = exc
            fallback_reason = "visible_click_timeout"

    fallback_selectors = tuple(
        dict.fromkeys(
            (
                selector.removesuffix(":visible"),
                selector.replace(":visible", ""),
            )
        )
    )

    async def resolve_unique_fallback() -> tuple[str | None, Any | None, list[int]]:
        counts: list[int] = []
        for candidate_selector in fallback_selectors:
            candidate_locator = page.locator(candidate_selector)
            candidate_count = await candidate_locator.count()
            counts.append(candidate_count)
            if candidate_count == 1:
                return candidate_selector, candidate_locator, counts
        return None, None, counts

    (
        fallback_selector,
        fallback_locator,
        fallback_counts,
    ) = await resolve_unique_fallback()
    history_recovery: dict[str, Any] | None = None
    if fallback_selector is None and fallback_counts == [0, 0]:
        # 深度内容点击可能在结果采集后才完成 SPA 路由，导致当前浏览器已离开
        # 带底部导航的列表页。同 URL 的弹层/上传状态也可能占一个 history
        # 记录，因此最多后退三次；每次都必须重新通过唯一性校验，命中即停，
        # 不能直接在未知页面 force 或无界回退到入口页。
        initial_url = str(getattr(page, "url", "") or "")
        recovery_steps: list[dict[str, Any]] = []
        recovered_counts: list[int] = []
        for attempt in range(1, BOTTOM_NAVIGATION_HISTORY_RECOVERY_LIMIT + 1):
            previous_url = str(getattr(page, "url", "") or "")
            try:
                await page.go_back(
                    wait_until="domcontentloaded",
                    timeout=min(timeout_ms, 5000),
                )
                wait_for_timeout = getattr(page, "wait_for_timeout", None)
                if callable(wait_for_timeout):
                    await wait_for_timeout(500)
            except timeout_error_type:
                pass
            (
                fallback_selector,
                fallback_locator,
                recovered_counts,
            ) = await resolve_unique_fallback()
            recovery_steps.append(
                {
                    "attempt": attempt,
                    "from_url": previous_url,
                    "to_url": str(getattr(page, "url", "") or ""),
                    "candidate_counts": recovered_counts,
                }
            )
            fallback_counts.extend(recovered_counts)
            if fallback_selector is not None:
                break
        history_recovery = {
            "from_url": initial_url,
            "to_url": str(getattr(page, "url", "") or ""),
            "candidate_counts": recovered_counts,
            "attempt_count": len(recovery_steps),
            "steps": recovery_steps,
        }
    if fallback_selector is None or fallback_locator is None:
        if native_click_error is not None:
            raise native_click_error
        raise ValueError(f"底部导航可见性回退未找到唯一目标：candidate_counts={fallback_counts}")

    action_trace[-1].update(
        {
            "fallback": "unique_bottom_navigation_text_dispatch",
            "fallback_reason": (
                "browser_history_recovery" if history_recovery is not None else fallback_reason
            ),
            "fallback_selector": fallback_selector,
            **({"history_recovery": history_recovery} if history_recovery is not None else {}),
        }
    )
    before_dispatch_url = str(getattr(page, "url", "") or "")
    if not await _dispatch_bottom_navigation_until_effect_observed(
        page=page,
        fallback_locator=fallback_locator,
        before_url=before_dispatch_url,
        timeout_ms=min(timeout_ms, 10000),
        action_trace=action_trace,
    ):
        raise BlockedPageCaptureActionError(
            "底部导航事件分派未产生路由变化或目标激活状态，已拒绝假成功",
            error_code="BOTTOM_NAVIGATION_TRANSITION_NOT_OBSERVED",
        )


async def _click_main_tab_video_pages_foot_community(
    *,
    page: Any,
    matched: tuple[str, int] | None,
    navigation_family: str | None,
    navigation_label: str | None,
    timeout_ms: int,
    action_trace: list[dict[str, Any]],
    timeout_error_type: type[Exception],
) -> bool | None:
    """在视频首页底栏容器内唯一定位“社区”，避免命中正文同名文本。"""
    current_url = str(getattr(page, "url", "") or "")
    if (
        matched is None
        or matched[0] != H5_BOTTOM_NAVIGATION_FAMILY_PAGES_FOOT
        or navigation_family != H5_BOTTOM_NAVIGATION_FAMILY_PAGES_FOOT
        or navigation_label != PAGES_FOOT_COMMUNITY_LABEL
        or not _is_main_tab_video_url(current_url)
    ):
        return None

    target_locator = page.locator(PAGES_FOOT_COMMUNITY_SELECTOR)
    try:
        match_count = await asyncio.wait_for(target_locator.count(), timeout=1)
    except Exception as exc:  # noqa: BLE001 - 精确目标探测必须受独立时限约束
        raise ValueError("视频首页社区底栏目标探测超时") from exc
    if match_count != 1:
        raise ValueError(f"视频首页社区底栏目标不唯一：match_count={match_count}")

    native_click_error: str | None = None
    try:
        await target_locator.click(timeout=min(timeout_ms, PAGES_FOOT_SCOPED_CLICK_TIMEOUT_MS))
        if await _pages_foot_community_effect_observed(
            page=page,
            before_url=current_url,
            timeout_ms=min(timeout_ms, PAGES_FOOT_DOM_EFFECT_TIMEOUT_MS),
        ):
            action_trace[-1].update(
                {
                    "resolved_by": "scoped_pages_foot_native_click",
                    "navigation_label": PAGES_FOOT_COMMUNITY_LABEL,
                    "scoped_selector": PAGES_FOOT_COMMUNITY_SELECTOR,
                }
            )
            _record_bottom_navigation_url_change_effect(
                action_trace=action_trace,
                before_url=current_url,
                after_url=str(getattr(page, "url", "") or ""),
            )
            return True
    except timeout_error_type as exc:
        native_click_error = str(exc)[:200]

    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        raise ValueError("浏览器不支持视频首页社区底栏 DOM 点击")
    try:
        dispatch_result = await asyncio.wait_for(
            evaluate(
                r"""
                (label) => {
                  const visible = (element) => {
                    const rect = element.getBoundingClientRect();
                    const style = getComputedStyle(element);
                    return rect.width > 0 && rect.height > 0
                      && style.display !== "none"
                      && style.visibility !== "hidden";
                  };
                  const roots = Array.from(
                    document.querySelectorAll(".pages-foot-box"),
                  ).filter(visible);
                  const items = roots.flatMap((root) =>
                    Array.from(root.querySelectorAll("li")).filter((item) => {
                      const labels = Array.from(
                        item.querySelectorAll(".pages-foot-label"),
                      ).map((node) => String(node.textContent || "").trim());
                      return visible(item) && labels.length === 1 && labels[0] === label;
                    }),
                  );
                  if (items.length !== 1) {
                    return {match_count: items.length, clicked: false};
                  }
                  const item = items[0];
                  item.click();
                  return {
                    match_count: 1,
                    clicked: true,
                    class_name: String(item.className || ""),
                  };
                }
                """,
                PAGES_FOOT_COMMUNITY_LABEL,
            ),
            timeout=max(
                0.001,
                min(timeout_ms, PAGES_FOOT_SCOPED_CLICK_TIMEOUT_MS) / 1000,
            ),
        )
    except Exception as exc:  # noqa: BLE001 - DOM 点击也必须受独立时限约束
        raise ValueError("视频首页社区底栏 DOM 点击超时") from exc
    if not isinstance(dispatch_result, dict) or dispatch_result.get("match_count") != 1:
        raise ValueError(f"视频首页社区底栏 DOM 目标不唯一：result={dispatch_result!r}")

    action_trace[-1].update(
        {
            "fallback": "scoped_pages_foot_dom_click",
            "navigation_label": PAGES_FOOT_COMMUNITY_LABEL,
            "scoped_selector": PAGES_FOOT_COMMUNITY_SELECTOR,
            **({"native_click_error": native_click_error} if native_click_error else {}),
        }
    )
    if await _pages_foot_community_effect_observed(
        page=page,
        before_url=current_url,
        timeout_ms=min(timeout_ms, PAGES_FOOT_DOM_EFFECT_TIMEOUT_MS),
    ):
        _record_bottom_navigation_url_change_effect(
            action_trace=action_trace,
            before_url=current_url,
            after_url=str(getattr(page, "url", "") or ""),
        )
        return True
    raise BlockedPageCaptureActionError(
        "视频首页社区底栏点击未产生路由变化或 active 状态",
        error_code="BOTTOM_NAVIGATION_TRANSITION_NOT_OBSERVED",
    )


def _is_main_tab_video_url(page_url: str) -> bool:
    """只匹配查询参数明确声明 mainTab=video 的页面。"""
    values = parse_qs(urlsplit(page_url).query).get("mainTab", [])
    return any(str(value).strip().lower() == "video" for value in values)


async def _pages_foot_community_effect_observed(
    *,
    page: Any,
    before_url: str,
    timeout_ms: int,
) -> bool:
    """通过重新查询底栏 DOM 验证社区页效果，避免等待已被 SPA 替换的旧节点。"""
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return False
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(timeout_ms, 0) / 1000
    while True:
        if before_url and str(getattr(page, "url", "") or "") != before_url:
            return True
        try:
            state = await asyncio.wait_for(
                evaluate(
                    r"""
                    (label) => {
                      const roots = Array.from(
                        document.querySelectorAll(".pages-foot-box"),
                      );
                      const items = roots.flatMap((root) =>
                        Array.from(root.querySelectorAll("li")).filter((item) => {
                          const labels = Array.from(
                            item.querySelectorAll(".pages-foot-label"),
                          ).map((node) => String(node.textContent || "").trim());
                          return labels.length === 1 && labels[0] === label;
                        }),
                      );
                      return {
                        match_count: items.length,
                        class_name: items.length === 1
                          ? String(items[0].className || "")
                          : "",
                      };
                    }
                    """,
                    PAGES_FOOT_COMMUNITY_LABEL,
                ),
                timeout=0.5,
            )
        except Exception:  # noqa: BLE001 - 单次 DOM 状态读取失败时保留短轮询
            state = None
        if isinstance(state, dict) and state.get("match_count") == 1:
            if "active" in str(state.get("class_name") or "").split():
                return True
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(min(0.1, max(deadline - loop.time(), 0)))


async def _dispatch_bottom_navigation_until_effect_observed(
    *,
    page: Any,
    fallback_locator: Any,
    before_url: str,
    timeout_ms: int,
    action_trace: list[dict[str, Any]],
) -> bool:
    """对唯一底部导航项尝试多个安全事件目标，并逐次验证真实效果。"""
    candidate_specs: tuple[tuple[str, str | None], ...] = (
        ("text", ".van-tabbar-item__text"),
        ("text_child", ".van-tabbar-item__text > *"),
        ("icon", ".van-tabbar-item__icon"),
        ("icon_child", ".van-tabbar-item__icon > *"),
        ("item", None),
    )
    attempts: list[dict[str, Any]] = []
    for name, child_selector in candidate_specs:
        candidate = (
            fallback_locator if child_selector is None else fallback_locator.locator(child_selector)
        )
        count: int | None = None
        count_method = getattr(candidate, "count", None)
        if callable(count_method):
            try:
                count = await count_method()
            except Exception as exc:  # noqa: BLE001  # 单个子目标失效时继续尝试同一 Tab 内其他安全目标
                attempts.append(
                    {
                        "target": name,
                        "status": "count_error",
                        "error": str(exc)[:200],
                    }
                )
                continue
            if count != 1:
                attempts.append({"target": name, "status": "skipped", "count": count})
                continue
        dispatch_event = getattr(candidate, "dispatch_event", None)
        if not callable(dispatch_event):
            attempts.append({"target": name, "status": "unsupported", "count": count})
            continue
        try:
            await dispatch_event("click")
        except Exception as exc:  # noqa: BLE001  # 事件目标不可分派时继续尝试同一 Tab 内其他安全目标
            attempts.append(
                {
                    "target": name,
                    "status": "dispatch_error",
                    "count": count,
                    "error": str(exc)[:200],
                }
            )
            continue
        if await _bottom_navigation_effect_observed(
            page=page,
            target_locator=fallback_locator,
            before_url=before_url,
            timeout_ms=timeout_ms,
        ):
            if attempts:
                attempts.append({"target": name, "status": "success", "count": count})
                action_trace[-1]["fallback_dispatch_attempts"] = attempts
            _record_bottom_navigation_url_change_effect(
                action_trace=action_trace,
                before_url=before_url,
                after_url=str(getattr(page, "url", "") or ""),
            )
            return True
        attempts.append({"target": name, "status": "no_effect", "count": count})
    if attempts:
        action_trace[-1]["fallback_dispatch_attempts"] = attempts
    return False


async def _click_first_viewport_intersecting_match(
    *,
    page: Any,
    selector: str,
    timeout_ms: int,
    action_trace: list[dict[str, Any]],
    verified_card_activation: str | None = None,
    expected_rect: dict[str, float] | None = None,
) -> bool:
    """多匹配 selector 优先点击当前视口内元素，避免滚动后命中离屏首项。"""
    locator_method = getattr(page, "locator", None)
    if not callable(locator_method):
        return False
    locator = locator_method(selector)
    count_method = getattr(locator, "count", None)
    nth_method = getattr(locator, "nth", None)
    if not callable(count_method) or not callable(nth_method):
        return False
    try:
        match_count = await count_method()
    except Exception:  # noqa: BLE001  # 测试替身或浏览器瞬时状态不支持时回退原生点击
        return False
    if match_count == 0:
        first_candidate = getattr(locator, "first", None)
        wait_for = getattr(first_candidate, "wait_for", None)
        if callable(wait_for):
            try:
                await wait_for(
                    state="visible",
                    timeout=min(timeout_ms, MULTI_MATCH_DYNAMIC_PROBE_TIMEOUT_MS),
                )
                match_count = await count_method()
            except Exception:  # noqa: BLE001  # 动态候选未在探测窗口出现时保留原生点击等待
                return False
    if match_count <= 1 and verified_card_activation is None:
        return False
    viewport = await _resolve_page_viewport_size(page)
    if viewport is None:
        return False
    click_errors: list[Exception] = []
    offscreen_candidates: list[tuple[float, int, Any]] = []
    click_attempt_count = 0
    viewport_candidates: list[tuple[float, int, Any]] = []
    for index in range(min(match_count, MULTI_MATCH_VIEWPORT_SCAN_LIMIT)):
        candidate = locator.nth(index)
        bounding_box = getattr(candidate, "bounding_box", None)
        if not callable(bounding_box):
            return False
        try:
            box = await bounding_box()
        except Exception:  # noqa: BLE001  # 单个候选布局失效时继续检查后续候选
            continue
        if not _box_center_in_viewport(box, viewport):
            distance = _box_center_distance_to_viewport(box, viewport)
            if distance is not None:
                offscreen_candidates.append((distance, index, candidate))
            continue
        # 位置型 selector 可能同时命中容器与叶节点；带解析矩形时按几何最
        # 接近排序，否则保持原有的索引顺序语义。
        rank = float(index)
        if expected_rect is not None:
            geometry_distance = _box_center_distance_to_rect(box, expected_rect)
            if geometry_distance is not None:
                rank = geometry_distance
        viewport_candidates.append((rank, index, candidate))
    for _rank, index, candidate in sorted(viewport_candidates, key=lambda item: (item[0], item[1])):
        click_attempt_count += 1
        action_trace[-1].update(
            {
                "resolved_by": (
                    f"first_viewport_intersecting_verified_{verified_card_activation}_click"
                    if verified_card_activation is not None
                    else (
                        "expected_rect_geometry_match"
                        if expected_rect is not None
                        else "first_viewport_intersecting_match"
                    )
                ),
                "match_index": index,
                "match_count": match_count,
            }
        )
        try:
            if verified_card_activation is not None:
                await _activate_verified_card(
                    candidate,
                    activation_mode=verified_card_activation,
                )
            else:
                await candidate.click(
                    timeout=min(timeout_ms, MULTI_MATCH_VIEWPORT_CLICK_TIMEOUT_MS)
                )
            return True
        except Exception as exc:  # noqa: BLE001  # 当前候选动作性失败时尝试后续视口候选
            click_errors.append(exc)
            action_trace[-1]["viewport_click_failures"] = len(click_errors)
            if click_attempt_count >= MULTI_MATCH_VIEWPORT_CLICK_ATTEMPT_LIMIT:
                break
            continue
    if not click_errors:
        for _distance, index, candidate in sorted(offscreen_candidates):
            scroll_into_view = getattr(candidate, "scroll_into_view_if_needed", None)
            bounding_box = getattr(candidate, "bounding_box", None)
            if not callable(scroll_into_view) or not callable(bounding_box):
                continue
            click_attempt_count += 1
            try:
                await scroll_into_view(
                    timeout=min(
                        timeout_ms,
                        MULTI_MATCH_VIEWPORT_CLICK_TIMEOUT_MS,
                    )
                )
                box = await bounding_box()
                carousel_activation = None
                if not _box_center_in_viewport(box, viewport):
                    carousel_activation = await _activate_candidate_carousel_tab(
                        page=page,
                        candidate=candidate,
                    )
                    if carousel_activation is not None:
                        box = await bounding_box()
                if not _box_center_in_viewport(box, viewport):
                    continue
                action_trace[-1].update(
                    {
                        "resolved_by": (
                            f"verified_{verified_card_activation}_click_scrolled_into_view"
                            if verified_card_activation is not None
                            else (
                                "carousel_tab_activation_then_viewport_click"
                                if carousel_activation is not None
                                else "nearest_match_scrolled_into_view"
                            )
                        ),
                        "match_index": index,
                        "match_count": match_count,
                        **(
                            {"carousel_tab_index": carousel_activation["slide_index"]}
                            if carousel_activation is not None
                            else {}
                        ),
                    }
                )
                if verified_card_activation is not None:
                    await _activate_verified_card(
                        candidate,
                        activation_mode=verified_card_activation,
                    )
                else:
                    await candidate.click(
                        timeout=min(
                            timeout_ms,
                            MULTI_MATCH_VIEWPORT_CLICK_TIMEOUT_MS,
                        )
                    )
                return True
            except Exception as exc:  # noqa: BLE001  # 当前候选滚动或点击失败时尝试后续近邻
                click_errors.append(exc)
                action_trace[-1]["viewport_click_failures"] = len(click_errors)
            if click_attempt_count >= MULTI_MATCH_VIEWPORT_CLICK_ATTEMPT_LIMIT:
                break
    if click_errors:
        raise click_errors[0]
    return False


async def _activate_verified_card(
    candidate: Any,
    *,
    activation_mode: str,
) -> None:
    """从验签叶节点激活固定业务卡片，并在活页重新校验容器结构。"""
    evaluate = getattr(candidate, "evaluate", None)
    if not callable(evaluate):
        raise PageInteractionFailedError(
            "当前叶节点不支持验签卡片激活",
            error_code="VERIFIED_CARD_TARGET_STALE",
            retryable=True,
        )
    result = await evaluate(
        """
        (element, mode) => {
          if (mode === "video_item" || mode === "comic_item") {
            const card = mode === "video_item"
              ? element.closest('.video-item')
              : element.closest('.comic-item');
            const leafValid = mode === "video_item" || element.classList.contains('name');
            const inspectCenter = () => {
              const bounds = element.getBoundingClientRect();
              const centerX = Math.min(
                Math.max(bounds.left + bounds.width / 2, 0),
                Math.max(window.innerWidth - 1, 0),
              );
              const centerY = Math.min(
                Math.max(bounds.top + bounds.height / 2, 0),
                Math.max(window.innerHeight - 1, 0),
              );
              const centerTarget = document.elementFromPoint(centerX, centerY);
              const shell = centerTarget?.closest('.launch-page');
              const shellInactive = Boolean(
                shell
                && !(shell.innerText || "").trim()
                && shell.children.length > 0
                && [...shell.children].every((child) => {
                  const style = getComputedStyle(child);
                  const rect = child.getBoundingClientRect();
                  return style.display === "none"
                    || style.visibility === "hidden"
                    || Number(style.opacity || 1) <= 0
                    || rect.width <= 0
                    || rect.height <= 0;
                })
              );
              const centerTargetWithinCard = Boolean(
                centerTarget
                && card
                && (centerTarget === card || card.contains(centerTarget))
              );
              return {
                allowed: Boolean(shellInactive || centerTargetWithinCard),
                shellInactive,
                centerTargetWithinCard,
                centerTargetTag: centerTarget?.tagName || null,
                centerTargetClasses: centerTarget
                  ? [...centerTarget.classList].slice(0, 12)
                  : [],
                bounds: {
                  x: bounds.x,
                  y: bounds.y,
                  width: bounds.width,
                  height: bounds.height,
                },
              };
            };
            let center = inspectCenter();
            let recentered = false;
            if (leafValid && card?.contains(element) && !center.allowed) {
              element.scrollIntoView({
                behavior: "instant",
                block: "center",
                inline: "nearest",
              });
              recentered = true;
              center = inspectCenter();
            }
            if (
              !leafValid
              || !card
              || !card.contains(element)
              || !center.allowed
            ) {
              return {
                activated: false,
                leafValid,
                cardFound: Boolean(card),
                cardContainsElement: Boolean(card?.contains(element)),
                recentered,
                ...center,
              };
            }
            card.click();
            return { activated: true };
          }
          if (mode !== "post_card") {
            return { activated: false };
          }
          const content = element.closest('.dynamicData-content');
          const card = element.closest('.dynamicData-box');
          if (
            !element.classList.contains('title') ||
            !content ||
            !card ||
            !card.contains(content)
          ) {
            return { activated: false };
          }
          card.click();
          return { activated: true };
        }
        """,
        activation_mode,
    )
    if not isinstance(result, dict) or result.get("activated") is not True:
        diagnostics = json.dumps(result, ensure_ascii=False)[:1_500]
        raise PageInteractionFailedError(
            f"当前叶节点已不属于验签卡片或安全遮挡结构已变化：{diagnostics}",
            error_code="VERIFIED_CARD_TARGET_STALE",
            retryable=True,
        )


async def _activate_candidate_carousel_tab(
    *,
    page: Any,
    candidate: Any,
) -> dict[str, int] | None:
    """激活候选所在的 Vant 轮播页签，并由调用方复核候选是否进入视口。"""
    evaluate = getattr(candidate, "evaluate", None)
    if not callable(evaluate):
        return None
    try:
        result = await evaluate(
            """
            (element) => {
              const slide = element.closest(".van-swipe-item");
              const track = slide?.parentElement;
              if (!slide || !track?.classList.contains("van-swipe__track")) {
                return { dispatched: false, reason: "not_vant_swipe_slide" };
              }
              const slides = Array.from(track.children).filter(
                (item) => item.classList.contains("van-swipe-item"),
              );
              const slideIndex = slides.indexOf(slide);
              const swipeContainer = track.closest(".swipe-container, .van-swipe");
              const component = swipeContainer?.parentElement;
              const navItems = component
                ? Array.from(
                    component.querySelectorAll(
                      ":scope > .custom-nav .nav-items > .nav-item",
                    ),
                  )
                : [];
              const navItem = navItems[slideIndex];
              if (slideIndex < 0 || !navItem || typeof navItem.click !== "function") {
                return {
                  dispatched: false,
                  reason: "matching_carousel_tab_not_found",
                  slide_index: slideIndex,
                };
              }
              navItem.click();
              return { dispatched: true, slide_index: slideIndex };
            }
            """
        )
    except Exception:  # noqa: BLE001  # 非 Vant 轮播或瞬时 DOM 变化时保持保守回退
        return None
    if not isinstance(result, dict) or result.get("dispatched") is not True:
        return None
    slide_index = result.get("slide_index")
    if isinstance(slide_index, bool) or not isinstance(slide_index, int):
        return None
    if slide_index < 0:
        return None
    wait_for_timeout = getattr(page, "wait_for_timeout", None)
    if callable(wait_for_timeout):
        await wait_for_timeout(MULTI_MATCH_CAROUSEL_TAB_SETTLE_MS)
    else:
        await asyncio.sleep(MULTI_MATCH_CAROUSEL_TAB_SETTLE_MS / 1000)
    return {"slide_index": slide_index}


async def _diagnose_click_target(
    *,
    page: Any,
    selector: str,
) -> dict[str, Any]:
    """点击超时后只记录有界目标数量，避免把 Playwright 原始异常当成能力结论。"""

    locator_method = getattr(page, "locator", None)
    if not callable(locator_method):
        return {"match_count": None, "visible_count": None}
    try:
        locator = locator_method(selector)
        count_method = getattr(locator, "count", None)
        if not callable(count_method):
            return {"match_count": None, "visible_count": None}
        match_count = max(int(await count_method()), 0)
        visible_count = 0
        nth_method = getattr(locator, "nth", None)
        if callable(nth_method):
            for index in range(min(match_count, 20)):
                candidate = nth_method(index)
                is_visible = getattr(candidate, "is_visible", None)
                if not callable(is_visible):
                    visible_count = None
                    break
                try:
                    visible_count += int(bool(await is_visible()))
                except Exception:  # noqa: BLE001  # 诊断失败不得覆盖原始点击失败
                    visible_count = None
                    break
        else:
            visible_count = None
        return {
            "match_count": match_count,
            "visible_count": visible_count,
        }
    except Exception:  # noqa: BLE001  # 诊断本身不应改变页面失败边界
        return {"match_count": None, "visible_count": None}


def _click_target_failure_message(
    *,
    selector: str,
    error_code: str,
    diagnostics: dict[str, Any],
) -> str:
    """生成不包含页面正文的稳定点击失败摘要。"""

    safe_selector = selector[:MAX_UI_SELECTOR_LENGTH]
    if error_code == "CLICK_TARGET_NOT_FOUND":
        reason = "当前页面未找到点击目标"
    elif error_code == "CLICK_TARGET_OBSTRUCTED":
        obstruction = diagnostics.get("obstruction")
        obstruction_summary = (
            {
                "tag": obstruction.get("tag"),
                "role": obstruction.get("role"),
                "class_tokens": obstruction.get("class_tokens"),
            }
            if isinstance(obstruction, dict)
            else None
        )
        reason = f"点击目标被其他可见元素遮挡，obstruction={obstruction_summary!r}"
    else:
        reason = (
            "点击目标存在但未通过浏览器动作性校验，"
            f"match_count={diagnostics.get('match_count')!r}, "
            f"visible_count={diagnostics.get('visible_count')!r}"
        )
    return f"{reason}；selector={safe_selector!r}"


async def _resolve_page_viewport_size(page: Any) -> dict[str, float] | None:
    """读取当前页面视口尺寸，兼容真实 Playwright 与测试替身。"""
    viewport = getattr(page, "viewport_size", None)
    if isinstance(viewport, dict):
        width = _positive_number(viewport.get("width"))
        height = _positive_number(viewport.get("height"))
        if width is not None and height is not None:
            return {"width": width, "height": height}
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return None
    try:
        result = await evaluate(
            """
            () => ({
              width: window.innerWidth || document.documentElement.clientWidth || 0,
              height: window.innerHeight || document.documentElement.clientHeight || 0,
            })
            """
        )
    except Exception:  # noqa: BLE001  # 视口探测失败时保持原始点击行为
        return None
    if not isinstance(result, dict):
        return None
    width = _positive_number(result.get("width"))
    height = _positive_number(result.get("height"))
    if width is None or height is None:
        return None
    return {"width": width, "height": height}


def _box_center_in_viewport(box: Any, viewport: dict[str, float]) -> bool:
    """判断元素点击中心点是否位于当前视口内。"""
    if not isinstance(box, dict):
        return False
    x = _finite_number(box.get("x"))
    y = _finite_number(box.get("y"))
    width = _positive_number(box.get("width"))
    height = _positive_number(box.get("height"))
    if x is None or y is None or width is None or height is None:
        return False
    viewport_width = viewport["width"]
    viewport_height = viewport["height"]
    center_x = x + width / 2
    center_y = y + height / 2
    return 0 <= center_x <= viewport_width and 0 <= center_y <= viewport_height


def _box_center_distance_to_rect(
    box: Any,
    expected_rect: dict[str, float],
) -> float | None:
    """计算候选中心到解析期望矩形中心的距离，用于多命中几何消歧。"""
    if not isinstance(box, dict) or not isinstance(expected_rect, dict):
        return None
    x = _finite_number(box.get("x"))
    y = _finite_number(box.get("y"))
    width = _positive_number(box.get("width"))
    height = _positive_number(box.get("height"))
    ex = _finite_number(expected_rect.get("x"))
    ey = _finite_number(expected_rect.get("y"))
    ew = _finite_number(expected_rect.get("width"))
    eh = _finite_number(expected_rect.get("height"))
    if None in (x, y, width, height, ex, ey):
        return None
    center_x = x + width / 2
    center_y = y + height / 2
    expected_center_x = ex + (ew or 0.0) / 2
    expected_center_y = ey + (eh or 0.0) / 2
    return ((center_x - expected_center_x) ** 2 + (center_y - expected_center_y) ** 2) ** 0.5


def _box_center_distance_to_viewport(
    box: Any,
    viewport: dict[str, float],
) -> float | None:
    """计算元素中心点到视口矩形的最短距离，用于选择最近的离屏候选。"""
    if not isinstance(box, dict):
        return None
    x = _finite_number(box.get("x"))
    y = _finite_number(box.get("y"))
    width = _positive_number(box.get("width"))
    height = _positive_number(box.get("height"))
    if x is None or y is None or width is None or height is None:
        return None
    center_x = x + width / 2
    center_y = y + height / 2
    horizontal_distance = max(0.0, -center_x, center_x - viewport["width"])
    vertical_distance = max(0.0, -center_y, center_y - viewport["height"])
    return horizontal_distance**2 + vertical_distance**2


def _positive_number(value: Any) -> float | None:
    number = _finite_number(value)
    if number is None or number <= 0:
        return None
    return number


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if not isinstance(value, int | float):
        return None
    number = float(value)
    if number != number or number in {float("inf"), float("-inf")}:
        return None
    return number


async def _bottom_navigation_effect_observed(
    *,
    page: Any,
    target_locator: Any,
    before_url: str,
    timeout_ms: int,
    before_activation_state: tuple[str | None, ...] | None = None,
    require_observable_activation: bool = False,
) -> bool:
    """确认底部导航点击确实改变路由或激活了目标 Tab。"""
    get_attribute = getattr(target_locator, "get_attribute", None)
    # 兼容只实现 click/count 的测试替身；真实 Playwright Locator 一定支持
    # get_attribute，因此生产链路不会退化成“调用成功即成功”。
    if not callable(get_attribute):
        wait_for_url = getattr(page, "wait_for_url", None)
        if callable(wait_for_url) and before_url:
            try:
                await wait_for_url(
                    lambda current_url: str(current_url) != before_url,
                    timeout=timeout_ms,
                )
                return True
            except Exception:  # noqa: BLE001  # 测试替身或路由等待超时
                return False
        return not require_observable_activation

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(timeout_ms, 0) / 1000
    while True:
        current_url = str(getattr(page, "url", "") or "")
        if before_url and current_url != before_url:
            return True
        activation_state = await _bottom_navigation_activation_state(target_locator)
        if (
            _bottom_navigation_state_is_active(activation_state)
            and (
                before_activation_state is None
                or activation_state != before_activation_state
            )
        ):
            return True
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(min(0.1, max(deadline - loop.time(), 0)))


async def _bottom_navigation_activation_state(
    target_locator: Any,
) -> tuple[str | None, ...] | None:
    """读取当前目标的有限激活事实；读取失败时不得假定已激活。"""
    get_attribute = getattr(target_locator, "get_attribute", None)
    if not callable(get_attribute):
        return None
    try:
        values: list[str | None] = []
        for name in ("class", "aria-selected", "aria-current", "checked"):
            values.append(await get_attribute(name))
        return tuple(values)
    except Exception:  # noqa: BLE001 - 单次 DOM 读取失败时由调用方按未验效处理
        return None


def _bottom_navigation_state_is_active(
    state: tuple[str | None, ...] | None,
) -> bool:
    if state is None:
        return False
    class_name, aria_selected, aria_current, checked = state
    class_tokens = set(str(class_name or "").split())
    return bool(
        any(
            token in {"van-tabbar-item--active", "active", "is-active", "selected"}
            or re.search(
                r"(?:^|[-_])(active|checked|current|selected|on)(?:$|[-_])",
                token.casefold(),
            )
            for token in class_tokens
        )
        or str(aria_selected or "").casefold() == "true"
        or str(aria_current or "").casefold() in {"page", "true"}
        or str(checked or "").casefold() in {"checked", "true"}
    )


def _bottom_navigation_activation_digest(
    state: tuple[str | None, ...] | None,
) -> str | None:
    if state is None:
        return None
    return _capture_identity_digest(
        json.dumps(list(state), ensure_ascii=True, separators=(",", ":"))
    )


async def _click_bottom_navigation_with_url_change_watch(
    *,
    page: Any,
    target_locator: Any,
    before_url: str,
    timeout_ms: int,
    action_trace: list[dict[str, Any]],
) -> bool:
    """点击期间监听 URL；路由先变化时不再等待 Playwright 导航收尾。"""
    click_task = asyncio.create_task(target_locator.click(timeout=timeout_ms))
    url_change_task = asyncio.create_task(
        _wait_for_page_url_change(
            page=page,
            before_url=before_url,
            timeout_ms=timeout_ms,
        )
    )
    try:
        done, _ = await asyncio.wait(
            {click_task, url_change_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if url_change_task in done and url_change_task.result():
            action_trace[-1]["resolved_by"] = "bottom_navigation_url_change_during_native_click"
            _record_bottom_navigation_url_change_effect(
                action_trace=action_trace,
                before_url=before_url,
                after_url=str(getattr(page, "url", "") or ""),
            )
            return True
        try:
            await click_task
        except Exception:
            if before_url and str(getattr(page, "url", "") or "") != before_url:
                action_trace[-1]["resolved_by"] = "bottom_navigation_url_change_during_native_click"
                _record_bottom_navigation_url_change_effect(
                    action_trace=action_trace,
                    before_url=before_url,
                    after_url=str(getattr(page, "url", "") or ""),
                )
                return True
            raise
        if before_url and str(getattr(page, "url", "") or "") != before_url:
            action_trace[-1]["resolved_by"] = "bottom_navigation_url_change_during_native_click"
            _record_bottom_navigation_url_change_effect(
                action_trace=action_trace,
                before_url=before_url,
                after_url=str(getattr(page, "url", "") or ""),
            )
            return True
        return False
    finally:
        for task in (click_task, url_change_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(click_task, url_change_task, return_exceptions=True)


async def _wait_for_page_url_change(
    *,
    page: Any,
    before_url: str,
    timeout_ms: int,
) -> bool:
    """轮询当前页面 URL，避免用固定休眠猜测 SPA 导航完成时机。"""
    if not before_url:
        return False
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(timeout_ms, 0) / 1000
    while True:
        if str(getattr(page, "url", "") or "") != before_url:
            return True
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(min(0.05, max(deadline - loop.time(), 0)))


def _record_bottom_navigation_url_change_effect(
    *,
    action_trace: list[dict[str, Any]],
    before_url: str,
    after_url: str,
) -> None:
    """仅把实际 URL 变化标为 DOM 底栏验效，后续仍需结构与同源验签。"""
    if not action_trace or not before_url or not after_url or before_url == after_url:
        return
    action_trace[-1].update(
        {
            "navigation_effect_verified": True,
            "navigation_effect_reason": "same_origin_url_change",
        }
    )


async def _collect_artifacts(
    page: Any,
    request: PageCaptureRequest,
    *,
    include_screenshot: bool = True,
    include_ui_snapshot: bool = True,
    timings_ms: dict[str, float] | None = None,
    dom_collection_audit: dict[str, Any] | None = None,
    logical_surface_enabled: bool = False,
    logical_surface_shadow: bool = False,
    prepared_visual_snapshot: dict[str, Any] | None = None,
    prepared_visual_viewport_png: bytes | None = None,
    h5_visual_coordinate_mode: Literal["off", "shadow", "enforced"] = "off",
) -> list[PageCaptureArtifact]:
    artifacts: list[PageCaptureArtifact] = []
    if h5_visual_coordinate_mode not in {"off", "shadow", "enforced"}:
        raise ValueError("h5_visual_coordinate_mode 必须为 off、shadow 或 enforced")
    if (prepared_visual_snapshot is None) is not (prepared_visual_viewport_png is None):
        raise ValueError("预备视觉 Snapshot 与最终视口帧必须成对提供")
    visual_snapshot = prepared_visual_snapshot
    if request.capture_dom:
        stage_started_at = time.perf_counter()
        if str(request.session_id or "").startswith("h5:"):
            from services.api.app.infrastructure.browser.h5_dom_artifact import read_h5_dom_artifact

            result = await read_h5_dom_artifact(
                page, max_bytes=MAX_DOM_ARTIFACT_BYTES,
                timeout_ms=request.timeout_ms or 30000,
            )
            raw_dom = result["dom"]
            if dom_collection_audit is not None:
                dom_collection_audit.update(result["metrics"])
        else:
            raw_dom = await page.content()
        if timings_ms is not None:
            timings_ms["dom_browser_read"] = _elapsed_ms(stage_started_at)
        compaction_started_at = time.perf_counter()
        artifacts.append(
            PageCaptureArtifact(
                artifact_type="dom",
                artifact_id=None,
                content=(raw_dom if str(request.session_id or "").startswith("h5:")
                         else _compact_dom_artifact(raw_dom)),
            )
        )
        if timings_ms is not None:
            timings_ms["dom_python_compaction"] = _elapsed_ms(compaction_started_at)
            timings_ms["dom"] = _elapsed_ms(stage_started_at)
    if request.capture_visible_text:
        stage_started_at = time.perf_counter()
        artifacts.append(
            PageCaptureArtifact(
                artifact_type="visible_text",
                artifact_id=None,
                content=await _read_visible_text(
                    page, web_capture=request.web_capture_stage is not None,
                ),
            )
        )
        if timings_ms is not None:
            timings_ms["visible_text"] = _elapsed_ms(stage_started_at)
    if (
        visual_snapshot is None
        and h5_visual_coordinate_mode != "off"
        and request.capture_screenshot
        and include_screenshot
        and include_ui_snapshot
    ):
        stage_started_at = time.perf_counter()
        visual_snapshot = await _collect_ui_snapshot(
            page,
            logical_surface_enabled=logical_surface_enabled,
            logical_surface_shadow=logical_surface_shadow,
            surface_session_id=request.session_id,
            defer_logical_surface=True,
        )
        if timings_ms is not None:
            timings_ms["ui_snapshot"] = _elapsed_ms(stage_started_at)
    if request.capture_screenshot and include_screenshot:
        stage_started_at = time.perf_counter()
        direct_full_page = request.web_capture_stage == "web_full_page_supplemental"
        frame_origin = None
        if str(request.session_id or "").startswith("h5:") and not direct_full_page:
            screenshot_bytes, frame_origin = await capture_h5_frame(
                page, full_page=request.full_page,
            )
        else:
            screenshot_bytes = (
                await asyncio.wait_for(page.screenshot(full_page=True, type="png"), timeout=10)
                if direct_full_page
                else await page.screenshot(full_page=request.full_page)
            )
        artifacts.append(
            PageCaptureArtifact(
                artifact_type="screenshot",
                artifact_id=None,
                content={
                    "encoding": "base64",
                    "data": _encode_bytes(screenshot_bytes),
                    **({"frame_origin": frame_origin} if frame_origin is not None else {}),
                    **(
                        {"purpose": "web_direct_full_page", "full_page": True}
                        if direct_full_page
                        else {}
                    ),
                },
            )
        )
        if timings_ms is not None:
            timings_ms["screenshot"] = _elapsed_ms(stage_started_at)
    should_collect_visual_viewport = bool(
        visual_snapshot is not None
        and prepared_visual_viewport_png is None
        and _raw_snapshot_requires_visual_navigation_viewport(
            visual_snapshot,
            page=page,
            surface_session_id=request.session_id,
        )
    )
    if prepared_visual_viewport_png is not None or should_collect_visual_viewport:
        stage_started_at = time.perf_counter()
        viewport_png = (
            prepared_visual_viewport_png
            if prepared_visual_viewport_png is not None
            else await page.screenshot(full_page=False, type="png")
        )
        assert viewport_png is not None
        image_width, image_height = _png_pixel_dimensions(viewport_png)
        screenshot_index = sum(artifact.artifact_type == "screenshot" for artifact in artifacts) + 1
        artifacts.append(
            PageCaptureArtifact(
                artifact_type="screenshot",
                artifact_id=None,
                content={
                    "purpose": "visual_navigation_viewport",
                    "encoding": "base64",
                    "data": _encode_bytes(viewport_png),
                },
            )
        )
        if visual_snapshot is not None:
            visual_snapshot["visual_navigation_capture"] = {
                "protocol_version": "visual_navigation_capture.v1",
                "purpose": "visual_navigation_viewport",
                "screenshot_index": screenshot_index,
                "screenshot_sha256": f"sha256:{hashlib.sha256(viewport_png).hexdigest()}",
                "image_width": image_width,
                "image_height": image_height,
                "coordinate_space": "visual_viewport_css_pixels",
            }
            if prepared_visual_viewport_png is None:
                try:
                    visual_snapshot["visual_layout_evidence"] = extract_visual_layout_evidence(
                        viewport_png=viewport_png
                    )
                except Exception:  # noqa: BLE001 - 视觉布局失败只降级，不覆盖原 Capture
                    visual_snapshot["visual_layout_degraded"] = "visual_layout_unavailable"
        if timings_ms is not None:
            timings_ms["visual_navigation_screenshot"] = _elapsed_ms(stage_started_at)
    if visual_snapshot is not None and prepared_visual_snapshot is None:
        visual_snapshot = _attach_logical_surface(
            visual_snapshot,
            page=page,
            logical_surface_enabled=logical_surface_enabled,
            logical_surface_shadow=logical_surface_shadow,
            surface_session_id=request.session_id,
        )
    if include_ui_snapshot:
        stage_started_at = time.perf_counter()
        artifacts.append(
            PageCaptureArtifact(
                artifact_type="ui_snapshot",
                artifact_id=None,
                content=(
                    visual_snapshot
                    if visual_snapshot is not None
                    else await _collect_ui_snapshot(
                        page,
                        logical_surface_enabled=logical_surface_enabled,
                        logical_surface_shadow=logical_surface_shadow,
                        surface_session_id=request.session_id,
                        web_detail_candidate_priority=bool(request.web_capture_stage),
                    )
                ),
            )
        )
        if timings_ms is not None and visual_snapshot is None:
            timings_ms["ui_snapshot"] = _elapsed_ms(stage_started_at)
    if request.capture_media_state:
        stage_started_at = time.perf_counter()
        artifacts.append(
            PageCaptureArtifact(
                artifact_type="media_state",
                artifact_id=None,
                content=await _collect_media_state(page),
            )
        )
        if timings_ms is not None:
            timings_ms["media_state"] = _elapsed_ms(stage_started_at)
    ui_artifact = next((item for item in artifacts if item.artifact_type == "ui_snapshot"), None)
    from services.api.app.application.evidence_presentation import derive_h5_presentation
    from services.api.app.infrastructure.browser.h5_player_frames import business_videos_in_view

    ordinary_video = bool(
        str(request.session_id or "").startswith("h5:")
        and request.capture_screenshot and include_screenshot and include_ui_snapshot
        and ui_artifact is not None and isinstance(ui_artifact.content, dict)
        and business_videos_in_view(ui_artifact.content)
        and derive_h5_presentation(ui_snapshot=ui_artifact.content).to_dict()["topmost_layer_kind"]
            == "page"
    )
    if request.capture_checkpoint_observation or ordinary_video:
        from services.api.app.infrastructure.browser.h5_checkpoint_sampling import (
            collect_checkpoint_samples,
        )

        measured = await collect_checkpoint_samples(
            page,
            read_snapshot=lambda surface: _collect_ui_snapshot(
                surface,
                surface_session_id=request.session_id,
                logical_surface_enabled=logical_surface_enabled,
                logical_surface_shadow=logical_surface_shadow,
            ),
            capture_frame=capture_h5_frame,
            retain_snapshots=ordinary_video,
            business_video_only=ordinary_video,
        )
        pngs = measured.pop("pngs")
        if ordinary_video:
            # 后续观测不追认旧主帧；主截图、最终 DOM 与 UI 同取最后观测窗口。
            final_snapshot = measured.pop("snapshots")[-1]
            measured["publication"] = {
                "schema_version": "h5-video-publication.v1",
                "primary_screenshot_sha256": measured["samples"][-1]["screenshot_sha256"],
                "page_url": final_snapshot.get("url"),
            }
            if any(item.artifact_type == "screenshot"
                   and item.content.get("purpose") == "visual_navigation_viewport"
                   for item in artifacts):
                final_snapshot = _attach_visual_navigation_evidence_to_snapshot(
                    final_snapshot, page=page, viewport_png=pngs[-1],
                    logical_surface_enabled=logical_surface_enabled,
                    logical_surface_shadow=logical_surface_shadow,
                    surface_session_id=request.session_id,
                )
            primary_seen = False
            for index, artifact in enumerate(artifacts):
                content = None
                if artifact.artifact_type == "ui_snapshot":
                    content = final_snapshot
                elif artifact.artifact_type == "dom":
                    content = _compact_dom_artifact(await page.content())
                elif artifact.artifact_type == "visible_text":
                    content = await _read_visible_text(page)
                elif artifact.artifact_type == "screenshot":
                    if (not primary_seen
                            or artifact.content.get("purpose") == "visual_navigation_viewport"):
                        content = {
                            **artifact.content, "data": _encode_bytes(pngs[-1]), "full_page": False,
                            "frame_origin": measured["samples"][-1]["frame_origin"],
                            "purpose": ("h5_video_primary" if not primary_seen
                                        else "visual_navigation_viewport"),
                        }
                    primary_seen = True
                if content is not None:
                    artifacts[index] = PageCaptureArtifact(
                        artifact_type=artifact.artifact_type, artifact_id=None, content=content,
                    )
        frames = zip(pngs, measured["samples"], strict=True)
        for index, (png, sample) in enumerate(frames):
            if ordinary_video and not request.capture_checkpoint_observation and index == 1:
                continue
            artifacts.append(
                PageCaptureArtifact(
                    artifact_type="screenshot",
                    artifact_id=None,
                    content={
                        "encoding": "base64",
                        "data": _encode_bytes(png),
                        "purpose": ("checkpoint_post_action_sample"
                                    if request.capture_checkpoint_observation
                                    else "video_publication_sample"),
                        "frame_origin": sample["frame_origin"],
                    },
                )
            )
        if request.capture_checkpoint_observation or ordinary_video:
            artifacts.append(
                PageCaptureArtifact(
                    artifact_type="checkpoint_observation",
                    artifact_id=None,
                    content=measured,
                )
            )
    return artifacts


async def _capture_visual_navigation_after_frames(page: Any) -> tuple[bytes, bytes]:
    """在动作 settle 后采集两张短间隔帧，中间帧仅供内存验证。"""
    after_1 = await page.screenshot(full_page=False, type="png")
    await asyncio.sleep(PAGE_POST_TRIGGER_SETTLE_POLL_INTERVAL_MS / 1000)
    after_2 = await page.screenshot(full_page=False, type="png")
    return after_1, after_2


async def _collect_visual_navigation_snapshot(
    page: Any,
    *,
    viewport_png: bytes,
    logical_surface_enabled: bool,
    logical_surface_shadow: bool,
    surface_session_id: str | None,
) -> dict[str, Any]:
    """先在内存中补齐专用 PNG 布局证据，再唯一附加最终逻辑身份。"""
    snapshot = await _collect_ui_snapshot(
        page,
        logical_surface_enabled=logical_surface_enabled,
        logical_surface_shadow=logical_surface_shadow,
        surface_session_id=surface_session_id,
        defer_logical_surface=True,
    )
    snapshot = _attach_visual_navigation_evidence_to_snapshot(
        snapshot,
        page=page,
        viewport_png=viewport_png,
        logical_surface_enabled=logical_surface_enabled,
        logical_surface_shadow=logical_surface_shadow,
        surface_session_id=surface_session_id,
    )
    snapshot["visual_navigation_capture"]["frame_origin"] = await read_h5_frame_origin(page)
    return snapshot


def _attach_visual_navigation_evidence_to_snapshot(
    snapshot: dict[str, Any],
    *,
    page: Any,
    viewport_png: bytes,
    logical_surface_enabled: bool,
    logical_surface_shadow: bool,
    surface_session_id: str | None,
) -> dict[str, Any]:
    """用指定内存帧补视觉事实，再唯一生成最终逻辑身份。"""
    image_width, image_height = _png_pixel_dimensions(viewport_png)
    snapshot["visual_navigation_capture"] = {
        "protocol_version": "visual_navigation_capture.v1",
        "purpose": "visual_navigation_viewport",
        "screenshot_index": 0,
        "screenshot_sha256": f"sha256:{hashlib.sha256(viewport_png).hexdigest()}",
        "image_width": image_width,
        "image_height": image_height,
        "coordinate_space": "visual_viewport_css_pixels",
    }
    try:
        snapshot["visual_layout_evidence"] = extract_visual_layout_evidence(
            viewport_png=viewport_png,
        )
    except Exception:  # noqa: BLE001 - 不可用视觉帧必须失闭为证据不足
        snapshot["visual_layout_degraded"] = "visual_layout_unavailable"
    return _attach_logical_surface(
        snapshot,
        page=page,
        logical_surface_enabled=logical_surface_enabled,
        logical_surface_shadow=logical_surface_shadow,
        surface_session_id=surface_session_id,
    )


def _logical_surface(snapshot: Mapping[str, Any]) -> Mapping[str, Any] | None:
    surface = snapshot.get("logical_surface")
    return surface if isinstance(surface, Mapping) else None


def _validate_live_logical_target_binding(
    *,
    snapshot: Mapping[str, Any],
    binding: LogicalTargetRuntimeBinding,
) -> None:
    """在真实输入前复核当前页面，禁止旧 Capture 的目标落到新状态。"""

    surface = _logical_surface(snapshot)
    identity_matches = bool(
        isinstance(surface, Mapping)
        and surface.get("protocol_version") == "logical_surface.v1"
        and surface.get("shadow") is False
        and surface.get("identity_fingerprint")
        == binding.source_identity_fingerprint
    )
    if identity_matches and (
        surface.get("state_fingerprint") == binding.source_state_fingerprint
        or _dynamic_logical_target_still_bound(snapshot=snapshot, binding=binding)
    ):
        return
    raise BlockedPageCaptureActionError(
        "动作前实时页面状态已偏离来源 Capture，请重新观察后再执行。",
        error_code="LOGICAL_SOURCE_STATE_STALE",
    )


def _dynamic_logical_target_still_bound(
    *,
    snapshot: Mapping[str, Any],
    binding: LogicalTargetRuntimeBinding,
) -> bool:
    """动态页面只在唯一目标结构签名仍一致时接受最终动作。"""

    if (
        not binding.allow_dynamic_state_revalidation
        or binding.target_source is None
    ):
        return False
    raw_elements = snapshot.get("elements")
    if not isinstance(raw_elements, list):
        return False
    exact_matches: list[Mapping[str, Any]] = []
    identity_matches: list[Mapping[str, Any]] = []
    for raw in raw_elements:
        if not isinstance(raw, Mapping) or raw.get("visible") is not True:
            continue
        hit_test = raw.get("hit_test")
        if (
            not isinstance(hit_test, Mapping)
            or raw.get("disabled") is True
            or str(raw.get("aria_disabled") or "").casefold() == "true"
        ):
            continue
        center_hits_target = hit_test.get("center_hits_target") is True
        if not center_hits_target and not (
            binding.allow_horizontal_offscreen_revalidation
            and _center_horizontally_outside_viewport(raw=raw, snapshot=snapshot)
        ):
            continue
        signature = logical_target_snapshot_signature(
            raw,
            target_source=binding.target_source,
        )
        if signature is not None and secrets.compare_digest(
            signature,
            binding.target_signature,
        ):
            exact_matches.append(raw)
        if not (
            binding.allow_dynamic_geometry_revalidation
            and binding.target_identity_signature is not None
        ):
            continue
        identity_signature = logical_target_snapshot_identity_signature(
            raw,
            target_source=binding.target_source,
        )
        if identity_signature is not None and secrets.compare_digest(
            identity_signature,
            binding.target_identity_signature,
        ):
            identity_matches.append(raw)
    return len(exact_matches) == 1 or len(identity_matches) == 1


def _center_horizontally_outside_viewport(
    *,
    raw: Mapping[str, Any],
    snapshot: Mapping[str, Any],
) -> bool:
    """只接受横向滚动栏中纵向仍可见、仅横向离屏的目标。"""

    rect = raw.get("rect")
    viewport = snapshot.get("viewport")
    if not isinstance(rect, Mapping) or not isinstance(viewport, Mapping):
        return False
    x = _finite_number(rect.get("x"))
    y = _finite_number(rect.get("y"))
    width = _positive_number(rect.get("width"))
    height = _positive_number(rect.get("height"))
    viewport_width = _positive_number(viewport.get("width"))
    viewport_height = _positive_number(viewport.get("height"))
    if None in {x, y, width, height, viewport_width, viewport_height}:
        return False
    assert x is not None
    assert y is not None
    assert width is not None
    assert height is not None
    assert viewport_width is not None
    assert viewport_height is not None
    center_x = x + width / 2
    center_y = y + height / 2
    return bool(
        0 <= center_y <= viewport_height
        and (center_x < 0 or center_x > viewport_width)
    )


def _record_scroll_effect(
    *,
    action_trace: list[dict[str, Any]],
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> None:
    """以实际容器位移记录滑动效果，不把脚本执行成功当成交互成功。"""

    if not action_trace:
        return
    before_top = _finite_number(before.get("scroll_top"))
    after_top = _finite_number(after.get("scroll_top"))
    before_selector = str(before.get("container_selector") or "")
    after_selector = str(after.get("container_selector") or "")
    segment_observed = bool(
        before_top is not None
        and after_top is not None
        and after_top > before_top
        and before_selector
        and before_selector == after_selector
    )
    existing_state_diff = action_trace[-1].get("state_diff")
    existing_effect = (
        existing_state_diff.get("scroll_effect")
        if isinstance(existing_state_diff, Mapping)
        else None
    )
    previous_effects = [
        entry.get("state_diff", {}).get("scroll_effect")
        for entry in action_trace[:-1]
        if isinstance(entry, Mapping) and isinstance(entry.get("state_diff"), Mapping)
    ]
    known_effects = [*previous_effects, existing_effect]
    observed = bool(
        segment_observed
        or any(
            isinstance(effect, Mapping) and effect.get("observed") is True
            for effect in known_effects
        )
    )
    first_before_top = next(
        (
            parsed
            for effect in known_effects
            if isinstance(effect, Mapping)
            and (parsed := _finite_number(effect.get("scroll_top_before"))) is not None
        ),
        before_top,
    )
    action_trace[-1].update(
        {
            "result": "scroll_effect_observed" if observed else "scroll_no_effect",
            "state_diff": {
                "scroll_effect": {
                    "protocol_version": "scroll-effect.v1",
                    "observed": observed,
                    "segment_observed": segment_observed,
                    "container_selector": after_selector or before_selector or None,
                    "scroll_top_before": first_before_top,
                    "scroll_top_after": after_top,
                }
            },
        }
    )


def _record_horizontal_scroll_effect(
    *,
    action_trace: list[dict[str, Any]],
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> None:
    """记录会员套餐容器的真实横向位移，供 Runtime 后置条件验收。"""
    if not action_trace:
        return
    before_left = _finite_number(before.get("scroll_left"))
    after_left = _finite_number(after.get("scroll_left"))
    before_active_index = _finite_number(before.get("swiper_active_index"))
    after_active_index = _finite_number(after.get("swiper_active_index"))
    before_selector = str(before.get("container_selector") or "")
    after_selector = str(after.get("container_selector") or "")
    observed = _horizontal_scroll_progressed(before=before, after=after)
    action_trace[-1].update(
        {
            "result": "scroll_effect_observed" if observed else "scroll_no_effect",
            "state_diff": {
                "scroll_effect": {
                    "protocol_version": "scroll-effect.v1",
                    "axis": "horizontal",
                    "observed": observed,
                    "segment_observed": observed,
                    "container_selector": after_selector or before_selector or None,
                    "scroll_left_before": before_left,
                    "scroll_left_after": after_left,
                    "scroll_mode": after.get("scroll_mode") or before.get("scroll_mode"),
                    "swiper_active_index_before": before_active_index,
                    "swiper_active_index_after": after_active_index,
                    "swiper_slide_count": after.get("swiper_slide_count"),
                    "swiper_is_end": after.get("swiper_is_end"),
                }
            },
        }
    )


def _horizontal_scroll_progressed(
    *,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> bool:
    before_left = _finite_number(before.get("scroll_left"))
    after_left = _finite_number(after.get("scroll_left"))
    before_active_index = _finite_number(before.get("swiper_active_index"))
    after_active_index = _finite_number(after.get("swiper_active_index"))
    before_selector = str(before.get("container_selector") or "")
    after_selector = str(after.get("container_selector") or "")
    same_container = bool(before_selector and before_selector == after_selector)
    native_progressed = bool(
        before_left is not None
        and after_left is not None
        and after_left > before_left
    )
    swiper_progressed = bool(
        before.get("scroll_mode") == "swiper"
        and after.get("scroll_mode") == "swiper"
        and before_active_index is not None
        and after_active_index is not None
        and after_active_index > before_active_index
    )
    return same_container and (native_progressed or swiper_progressed)


def _vertical_scroll_progressed(
    *,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> bool:
    """确认分屏采集发生了同一容器内的真实纵向位移。"""
    before_top = _finite_number(before.get("scroll_top"))
    after_top = _finite_number(after.get("scroll_top"))
    before_selector = str(before.get("container_selector") or "")
    after_selector = str(after.get("container_selector") or "")
    return bool(
        before_top is not None
        and after_top is not None
        and after_top > before_top
        and before_selector
        and before_selector == after_selector
    )


def _requires_visual_navigation_viewport(snapshot: Mapping[str, Any]) -> bool:
    """只为已确认且语义稀疏的 Flutter/Hybrid 页面密封专用帧。"""
    logical_surface = snapshot.get("logical_surface")
    if not isinstance(logical_surface, Mapping):
        return False
    render_profile = logical_surface.get("render_profile")
    semantics_coverage = logical_surface.get("semantics_coverage")
    return bool(
        isinstance(render_profile, Mapping)
        and render_profile.get("kind") in {"flutter_web", "hybrid"}
        and render_profile.get("status") == "confirmed"
        and isinstance(semantics_coverage, Mapping)
        and semantics_coverage.get("level") == "semantics_sparse"
    )


def _raw_snapshot_requires_visual_navigation_viewport(
    snapshot: Mapping[str, Any],
    *,
    page: Any,
    surface_session_id: str | None,
) -> bool:
    """不向 Snapshot 写入临时身份，只计算专用帧门禁所需的逻辑事实。"""
    document = snapshot.get("document")
    document_facts = document if isinstance(document, Mapping) else {}
    time_origin = document_facts.get("time_origin_ms")
    try:
        logical_surface = build_h5_logical_surface(
            ui_snapshot=snapshot,
            page_url=str(snapshot.get("url") or getattr(page, "url", "") or ""),
            surface_session_id=surface_session_id,
            document_token=f"{id(page)}:{time_origin if time_origin is not None else ''}",
        )
    except Exception:  # noqa: BLE001 - 门禁补证失败只关闭视觉专用帧
        return False
    return _requires_visual_navigation_viewport({"logical_surface": logical_surface})


def _png_pixel_dimensions(payload: object) -> tuple[int, int]:
    """有界验证专用 PNG 结构，并从 IHDR 读取真实像素尺寸。"""
    if not isinstance(payload, bytes) or len(payload) < 33:
        raise ValueError("专用视觉视口截图不是有效 PNG")
    if not payload.startswith(_PNG_SIGNATURE):
        raise ValueError("专用视觉视口截图不是有效 PNG")
    offset = len(_PNG_SIGNATURE)
    dimensions: tuple[int, int] | None = None
    idat_seen = False
    iend_seen = False
    for chunk_index in range(MAX_VISUAL_NAVIGATION_PNG_CHUNKS):
        if len(payload) - offset < 12:
            raise ValueError("专用视觉视口 PNG chunk 不完整")
        chunk_length = int.from_bytes(payload[offset : offset + 4], "big")
        chunk_type = payload[offset + 4 : offset + 8]
        chunk_data_start = offset + 8
        chunk_data_end = chunk_data_start + chunk_length
        chunk_end = chunk_data_end + 4
        if chunk_end > len(payload):
            raise ValueError("专用视觉视口 PNG chunk 越界")
        expected_crc = int.from_bytes(payload[chunk_data_end:chunk_end], "big")
        actual_crc = zlib.crc32(chunk_type)
        actual_crc = zlib.crc32(payload[chunk_data_start:chunk_data_end], actual_crc)
        if actual_crc != expected_crc:
            raise ValueError("专用视觉视口 PNG chunk 校验失败")
        if chunk_index == 0 and chunk_type != b"IHDR":
            raise ValueError("专用视觉视口 PNG 缺少首个 IHDR")
        if chunk_type == b"IHDR":
            if chunk_index != 0 or chunk_length != 13 or dimensions is not None:
                raise ValueError("专用视觉视口 PNG IHDR 无效")
            width = int.from_bytes(payload[chunk_data_start : chunk_data_start + 4], "big")
            height = int.from_bytes(
                payload[chunk_data_start + 4 : chunk_data_start + 8],
                "big",
            )
            if (
                width < 1
                or height < 1
                or width > MAX_VISUAL_NAVIGATION_PNG_DIMENSION
                or height > MAX_VISUAL_NAVIGATION_PNG_DIMENSION
                or width * height > MAX_VISUAL_NAVIGATION_PNG_PIXELS
            ):
                raise ValueError("专用视觉视口 PNG 像素尺寸超出限制")
            dimensions = (width, height)
        elif chunk_type == b"IDAT":
            idat_seen = True
        elif chunk_type == b"IEND":
            if chunk_length != 0 or chunk_end != len(payload):
                raise ValueError("专用视觉视口 PNG IEND 无效")
            iend_seen = True
            break
        offset = chunk_end
    else:
        raise ValueError("专用视觉视口 PNG chunk 数量超出限制")
    if dimensions is None or not idat_seen or not iend_seen:
        raise ValueError("专用视觉视口截图不是完整 PNG")
    return dimensions


def _compact_dom_artifact(dom: str) -> str:
    """保留可审计语义 DOM，并移除内联资源后限制单份落盘体积。"""
    if not isinstance(dom, str):
        raise TypeError("DOM 证据必须为字符串")
    original_payload = dom.encode("utf-8", errors="replace")
    original_bytes = len(original_payload)
    original_sha256 = hashlib.sha256(original_payload).hexdigest()
    del original_payload

    compacted = _DOM_NON_SEMANTIC_BLOCK_PATTERN.sub(
        _redact_dom_nonsemantic_block,
        dom,
    )
    compacted = _DOM_INLINE_RESOURCE_ATTRIBUTE_PATTERN.sub(
        _redact_dom_inline_resource_attribute,
        compacted,
    )
    payload = compacted.encode("utf-8", errors="replace")
    if len(payload) <= MAX_DOM_ARTIFACT_BYTES:
        return compacted

    marker = (
        f"<!-- alioth:dom-truncated original_bytes={original_bytes} sha256={original_sha256} -->"
    ).encode()
    retained_budget = MAX_DOM_ARTIFACT_BYTES - len(marker)
    head_budget = retained_budget // 2
    tail_budget = retained_budget - head_budget
    head = payload[:head_budget].decode("utf-8", errors="ignore")
    tail = payload[-tail_budget:].decode("utf-8", errors="ignore")
    return head + marker.decode("ascii") + tail


def _redact_dom_nonsemantic_block(match: re.Match[str]) -> str:
    """脚本与样式不是页面语义结构，只保留类型、大小与摘要。"""
    payload = match.group(0).encode("utf-8", errors="replace")
    tag = match.group("tag").casefold()
    return (
        f'<{tag} data-alioth-redacted="true" '
        f'data-alioth-original-bytes="{len(payload)}" '
        f'data-alioth-sha256="{hashlib.sha256(payload).hexdigest()}"></{tag}>'
    )


def _redact_dom_inline_resource_attribute(match: re.Match[str]) -> str:
    """属性内 data URI 只保留长度与摘要，普通 selector 属性保持原样。"""
    value = match.group("value")
    if "data:" not in value.casefold():
        return match.group(0)
    payload = value.encode("utf-8", errors="replace")
    replacement = (
        "alioth:inline-resource-redacted;"
        f"bytes={len(payload)};sha256={hashlib.sha256(payload).hexdigest()}"
    )
    quote = match.group("quote")
    return f"{match.group('prefix')}{quote}{replacement}{quote}"


async def _collect_timed_artifacts(
    *,
    page: Any,
    request: PageCaptureRequest,
) -> list[PageCaptureArtifact]:
    loop = asyncio.get_running_loop()
    started_at = loop.time()
    frames: list[dict[str, Any]] = []
    snapshot_frames: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []
    temporal_event_count = await _install_countdown_monitor(page)
    countdown_events: list[dict[str, Any]] = []
    for index in range(request.frame_count):
        if index > 0 and request.interval_ms > 0:
            temporal_event_count = await _wait_for_countdown_change(
                page=page,
                previous_event_count=temporal_event_count,
                timeout_ms=request.interval_ms,
            )
        visible_text = await _read_visible_text(
            page, web_capture=request.web_capture_stage is not None,
        )
        offset_ms = (loop.time() - started_at) * 1000
        frames.append(
            {
                "index": index,
                "encoding": "base64",
                "data": _encode_bytes(await page.screenshot(full_page=request.full_page)),
            }
        )
        snapshot = await _collect_ui_snapshot(
            page,
            logical_surface_enabled=request.logical_surface_enabled,
            logical_surface_shadow=request.logical_surface_shadow,
            surface_session_id=request.session_id,
        )
        snapshot_frames.append({"index": index, **snapshot})
        samples.append(
            {
                "index": index,
                "offset_ms": offset_ms,
                "visible_text": visible_text[:MAX_TEMPORAL_TEXT_LENGTH],
                "visible_text_truncated": len(visible_text) > MAX_TEMPORAL_TEXT_LENGTH,
            }
        )
        countdown_events = await _read_countdown_events(page)
        if len(samples) >= MIN_TEMPORAL_SAMPLES and any(
            item.get("kind") == "end" for item in countdown_events
        ):
            break
    if not countdown_events:
        countdown_events = await _read_countdown_events(page)
    return [
        PageCaptureArtifact(
            artifact_type="screenshot",
            artifact_id=None,
            content={
                "sequence": True,
                "interval_ms": request.interval_ms,
                "frames": frames,
            },
        ),
        PageCaptureArtifact(
            artifact_type="ui_snapshot",
            artifact_id=None,
            content=_bound_ui_snapshot_sequence(
                {
                    "schema_version": "ui-snapshot-sequence.v1",
                    "sequence": True,
                    "interval_ms": request.interval_ms,
                    "frames": snapshot_frames,
                }
            ),
        ),
        PageCaptureArtifact(
            artifact_type="temporal_trace",
            artifact_id=None,
            content={
                "schema_version": "1.0",
                "clock": "monotonic",
                "requested_interval_ms": request.interval_ms,
                "samples": samples,
                "countdown_events": countdown_events,
            },
        ),
    ]


async def _install_transient_message_monitor(page: Any) -> bool:
    """在动作前安装有界观察器，只记录动作窗口内出现的瞬态提示。"""
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return False
    try:
        result = await evaluate(
            r"""
            () => {
              const previous = window.__aliothTransientMessageMonitorV1;
              if (previous?.observer) previous.observer.disconnect();
              const LIMIT = 50;
              const SELECTOR = [
                "[role='alert']",
                "[role='status']",
                "[aria-live='assertive']",
                "[aria-live='polite']",
                "[class*='toast' i]",
                "[class*='snackbar' i]",
                "[class*='notification' i]",
                "[class*='notice' i]",
                "[class*='message' i]",
              ].join(",");
              const startedAt = performance.now();
              const records = new Map();
              const elementIds = new WeakMap();
              let nextId = 1;
              const round = (value) => Number.isFinite(value)
                ? Math.round(value * 1000) / 1000
                : null;
              const alpha = (color) => {
                const normalized = String(color || "").trim().toLowerCase();
                if (!normalized || normalized === "transparent") return 0;
                const match = normalized.match(/^rgba\([^,]+,[^,]+,[^,]+,\s*([\d.]+)\)$/);
                return match ? round(Number(match[1])) : 1;
              };
              const selectorFor = (element) => {
                if (element.id) return `#${CSS.escape(element.id)}`.slice(0, 512);
                const testId = element.getAttribute("data-testid");
                if (testId) {
                  return `[data-testid="${CSS.escape(testId)}"]`.slice(0, 512);
                }
                const classes = [...element.classList]
                  .filter(Boolean)
                  .slice(0, 3)
                  .map((item) => `.${CSS.escape(item)}`)
                  .join("");
                return `${element.tagName.toLowerCase()}${classes}`.slice(0, 512);
              };
              const positionZone = (rect) => {
                const x = (rect.left + rect.width / 2) / Math.max(window.innerWidth, 1);
                const y = (rect.top + rect.height / 2) / Math.max(window.innerHeight, 1);
                const vertical = y < 1 / 3 ? "top" : (y > 2 / 3 ? "bottom" : "center");
                const horizontal = x < 1 / 3 ? "left" : (x > 2 / 3 ? "right" : "center");
                return `${vertical}_${horizontal}`;
              };
              const surfaceFor = (element) => {
                let current = element;
                for (let depth = 0; current && depth < 5; depth += 1) {
                  const style = getComputedStyle(current);
                  if (
                    ["fixed", "absolute", "sticky"].includes(style.position)
                    || alpha(style.backgroundColor) > 0
                  ) return current;
                  current = current.parentElement;
                }
                return element;
              };
              const identityFor = (element) => [
                element.getAttribute("role"),
                element.getAttribute("aria-live"),
                element.className,
              ].map((item) => String(item || "").toLowerCase()).join(" ");
              const kindFor = (identity) => {
                if (/snackbar/.test(identity)) return "snackbar";
                if (/toast/.test(identity)) return "toast";
                if (/notification/.test(identity)) return "notification";
                if (/alert/.test(identity)) return "alert";
                if (/status/.test(identity)) return "status";
                if (/assertive|polite/.test(identity)) return "status";
                if (/notice/.test(identity)) return "notice";
                return "message";
              };
              const confidenceFor = (identity, surfaceStyle) => {
                if (/toast|snackbar|notification/.test(identity)) return 0.95;
                if (/alert|status|assertive|polite/.test(identity)) return 0.9;
                if (["fixed", "absolute", "sticky"].includes(surfaceStyle.position)) return 0.7;
                return 0.4;
              };
              const insideContent = (element) => Boolean(element.closest(
                "article,[contenteditable='true'],[class*='comment' i],"
                + "[class*='chat' i],[class*='feed' i],[data-post-id]",
              ));
              const markDisappeared = (element) => {
                const id = elementIds.get(element);
                const record = id ? records.get(id) : null;
                if (record && record.disappeared_at_ms == null) {
                  record.disappeared_at_ms = round(performance.now() - startedAt);
                }
              };
              const observe = (element) => {
                if (!(element instanceof Element)) return;
                // 同一提示的父子包装只记一次，不把样式继承差异当多条 Toast。
                for (let parent = element.parentElement; parent && parent.matches(SELECTOR);
                  parent = parent.parentElement) {
                  if (String(parent.textContent || "").trim()
                    !== String(element.textContent || "").trim()) break;
                  element = parent;
                }
                const existingId = elementIds.get(element);
                if (!existingId && records.size >= LIMIT) return;
                const identity = identityFor(element);
                const explicitLiveRegion = /alert|status|assertive|polite/.test(identity);
                if (insideContent(element) && !explicitLiveRegion) return;
                const surface = surfaceFor(element);
                const textElement = [...element.querySelectorAll("*")].reverse().find((child) => {
                  const childStyle = getComputedStyle(child);
                  const childRect = child.getBoundingClientRect();
                  return String(child.textContent || "").trim()
                    === String(element.textContent || "").trim()
                    && childRect.width > 0 && childRect.height > 0
                    && childStyle.display !== "none" && childStyle.visibility !== "hidden"
                    && Number(childStyle.opacity || 1) > 0;
                }) || element;
                const style = getComputedStyle(textElement);
                const surfaceStyle = getComputedStyle(surface);
                const rect = surface.getBoundingClientRect();
                let ancestorsVisible = true;
                for (let parent = element; parent; parent = parent.parentElement) {
                  const parentStyle = getComputedStyle(parent);
                  if (parentStyle.display === "none" || parentStyle.visibility === "hidden"
                    || Number(parentStyle.opacity || 1) <= 0) ancestorsVisible = false;
                }
                const visible = element.isConnected
                  && rect.width > 0 && rect.height > 0
                  && rect.right > 0 && rect.bottom > 0
                  && rect.left < window.innerWidth && rect.top < window.innerHeight
                  && ancestorsVisible && parseFloat(style.fontSize) > 0
                  && style.display !== "none" && style.visibility !== "hidden"
                  && Number(style.opacity || 1) > 0;
                if (!visible) {
                  markDisappeared(element);
                  return;
                }
                const confidence = confidenceFor(identity, surfaceStyle);
                if (confidence < 0.7) return;
                const text = String(
                  element.getAttribute("aria-label") || element.textContent || "",
                ).replace(/\s+/g, " ").trim().slice(0, 500);
                if (!text) return;
                // 公告与忙碌状态不是动作反馈；错误提示“加载失败”仍可作为真实提示。
                const kind = kindFor(identity);
                if (kind === "notice" || kind === "message"
                  || element.closest("[aria-busy='true'],.loading,.spinner,.skeleton")
                  || /^(加载中|正在加载|loading|请稍候|请稍等)[.。…\s]*$/i.test(text)
                  || (!/toast|snackbar|notification/.test(identity)
                    && !["fixed", "absolute", "sticky"].includes(surfaceStyle.position))) return;
                let id = existingId;
                if (!id) {
                  id = nextId;
                  nextId += 1;
                  elementIds.set(element, id);
                }
                const offset = round(performance.now() - startedAt);
                const existing = records.get(id);
                const payload = existing || {
                  event_id: `transient-${id}`,
                  observation_id: `${performance.timeOrigin}:${startedAt}`,
                  first_seen_ms: offset,
                  element,
                };
                Object.assign(payload, {
                  kind,
                  platform_ui_confidence: confidence,
                  selector: selectorFor(element),
                  text,
                  role: element.getAttribute("role"),
                  aria_live: element.getAttribute("aria-live"),
                  class_tokens: [...element.classList].slice(0, 10),
                  rect: {
                    x: round(rect.x),
                    y: round(rect.y),
                    width: round(rect.width),
                    height: round(rect.height),
                  },
                  position_zone: positionZone(rect),
                  viewport: { width: window.innerWidth, height: window.innerHeight },
                  style: {
                    font_size: style.fontSize,
                    font_weight: style.fontWeight,
                    text_color: style.color,
                    background_color: surfaceStyle.backgroundColor,
                    background_opacity: alpha(surfaceStyle.backgroundColor),
                    opacity: round(Number(surfaceStyle.opacity || 1)),
                    border_radius: surfaceStyle.borderRadius,
                  },
                  last_seen_ms: offset,
                  disappeared_at_ms: null,
                });
                records.set(id, payload);
              };
              const inspectNode = (node) => {
                if (!(node instanceof Element)) return;
                if (node.matches(SELECTOR)) observe(node);
                node.querySelectorAll(SELECTOR).forEach(observe);
                const closest = node.closest(SELECTOR);
                if (closest) observe(closest);
              };
              const observer = new MutationObserver((mutations) => {
                mutations.forEach((mutation) => {
                  if (mutation.type === "childList") {
                    mutation.addedNodes.forEach(inspectNode);
                    mutation.removedNodes.forEach((node) => {
                      if (!(node instanceof Element)) return;
                      records.forEach((record, id) => {
                        if (record.element === node || node.contains(record.element)) {
                          record.disappeared_at_ms = round(performance.now() - startedAt);
                          records.set(id, record);
                        }
                      });
                    });
                  } else {
                    inspectNode(mutation.target.parentElement || mutation.target);
                  }
                });
              });
              observer.observe(document.documentElement, {
                subtree: true,
                childList: true,
                attributes: true,
                characterData: true,
                attributeFilter: ["class", "style", "role", "aria-live", "aria-label"],
              });
              window.__aliothTransientMessageMonitorV1 = {
                observer,
                records,
                elementIds,
                started_at_ms: startedAt,
              };
              return true;
            }
            """
        )
        return result is True
    except Exception:  # noqa: BLE001 - 瞬态观察失败只降低证据质量，不影响安全动作
        return False


async def _stop_transient_message_monitor(page: Any) -> list[dict[str, Any]]:
    """停止动作窗口观察器并返回有界、无 DOM 句柄的样式事实。"""
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return []
    try:
        value = await evaluate(
            r"""
            () => {
              const monitor = window.__aliothTransientMessageMonitorV1;
              if (!monitor) return [];
              monitor.observer.disconnect();
              const stoppedAt = performance.now();
              const output = [...monitor.records.values()].slice(0, 50).map((record) => {
                const item = { ...record };
                delete item.element;
                if (item.disappeared_at_ms == null && record.element) {
                  const style = record.element.isConnected
                    ? getComputedStyle(record.element)
                    : null;
                  if (
                    !record.element.isConnected
                    || style?.display === "none"
                    || style?.visibility === "hidden"
                  ) {
                    item.disappeared_at_ms = Math.round(
                      (stoppedAt - monitor.started_at_ms) * 1000,
                    ) / 1000;
                  }
                }
                item.observation_window_ms = Math.round(
                  (stoppedAt - monitor.started_at_ms) * 1000,
                ) / 1000;
                return item;
              });
              delete window.__aliothTransientMessageMonitorV1;
              return output;
            }
            """
        )
    except Exception:  # noqa: BLE001 - 页面导航销毁上下文时返回空事实
        return []
    if not isinstance(value, list):
        return []
    return [
        _sanitize_transient_message(item)
        for item in value[:MAX_TRANSIENT_MESSAGE_EVENTS]
        if isinstance(item, dict)
    ]


async def _wait_for_transient_message(*, page: Any, timeout_ms: int) -> bool:
    """在有界动作窗口内等待异步接口回调触发首个 Toast。"""
    wait_for_function = getattr(page, "wait_for_function", None)
    if not callable(wait_for_function):
        await asyncio.sleep(max(timeout_ms, 0) / 1000)
        return False
    try:
        await wait_for_function(
            "() => Boolean(window.__aliothTransientMessageMonitorV1?.records?.size)",
            timeout=max(timeout_ms, 1),
        )
        return True
    except Exception:  # noqa: BLE001 - 无 Toast 是可信负事实，不属于动作失败
        return False


def _sanitize_transient_message(value: dict[str, Any]) -> dict[str, Any]:
    """二次裁剪浏览器返回的 Toast 样式事实。"""
    compact = {
        key: value.get(key)
        for key in (
            "event_id",
            "observation_id",
            "kind",
            "platform_ui_confidence",
            "selector",
            "text",
            "role",
            "aria_live",
            "position_zone",
            "first_seen_ms",
            "last_seen_ms",
            "disappeared_at_ms",
            "observation_window_ms",
        )
        if value.get(key) is not None
    }
    compact["selector"] = str(compact.get("selector") or "")[:MAX_UI_SELECTOR_LENGTH]
    compact["text"] = str(compact.get("text") or "")[:500]
    compact["class_tokens"] = [
        str(item)[:MAX_UI_CLASS_TOKEN_LENGTH] for item in list(value.get("class_tokens") or [])[:10]
    ]
    compact["rect"] = _bounded_scalar_mapping(value.get("rect"), limit=8)
    compact["viewport"] = _bounded_scalar_mapping(value.get("viewport"), limit=4)
    compact["style"] = _bounded_scalar_mapping(value.get("style"), limit=12)
    return compact


def _bounded_scalar_mapping(value: Any, *, limit: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key)[:100]: item[:200] if isinstance(item, str) else item
        for key, item in list(value.items())[:limit]
        if isinstance(item, str | bool | int | float) or item is None
    }


async def _capture_transient_message_frame(
    *,
    page: Any,
    messages: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """在动作返回后立即截取当前视口，给结构化瞬态事实补视觉锚点。"""
    screenshot = getattr(page, "screenshot", None)
    if not callable(screenshot):
        return None
    try:
        content = await screenshot(full_page=False)
    except Exception:  # noqa: BLE001 - 截图失败不覆盖已采集的结构化事实
        return None
    return {
        "index": 0,
        "encoding": "base64",
        "data": _encode_bytes(content),
        "message_event_ids": [
            str(item.get("event_id") or "")[:100]
            for item in messages[:MAX_TRANSIENT_MESSAGE_EVENTS]
        ],
    }


async def _capture_ad_navigation_frame(
    *,
    page: Any,
    index: int,
    role: str,
) -> dict[str, Any] | None:
    """截取广告源素材或落地页视口，并给成对帧写入稳定角色。"""
    screenshot = getattr(page, "screenshot", None)
    if not callable(screenshot):
        return None
    try:
        content = await screenshot(full_page=False)
    except Exception:  # noqa: BLE001 - 单帧失败必须降级证据，不覆盖动作事实
        return None
    return {
        "index": index,
        "role": role,
        "encoding": "base64",
        "data": _encode_bytes(content),
    }


async def _capture_ad_landing_observation(
    *,
    page: Any,
    index: int,
    static_render: bool = False,
) -> dict[str, Any]:
    """读取落地页有界语义摘要；原始 DOM、正文和定位器不进入动作事实。"""
    title = ""
    read_title = getattr(page, "title", None)
    if callable(read_title):
        try:
            title = str(await read_title())[:500]
        except Exception:  # noqa: BLE001 - 标题缺失不覆盖截图与 URL
            title = ""
    read_safe_snapshot = getattr(page, "ui_snapshot", None)
    if callable(read_safe_snapshot):
        snapshot = await read_safe_snapshot()
    else:
        snapshot = {} if static_render else await _collect_ui_snapshot(page)
    read_safe_har = getattr(page, "har_entries", None)
    har_entries = await read_safe_har() if callable(read_safe_har) else []
    raw_elements = snapshot.get("elements")
    semantic_anchors: list[str] = []
    if isinstance(raw_elements, list):
        for item in raw_elements:
            if not isinstance(item, dict):
                continue
            for key in ("text", "aria_label", "alt"):
                value = str(item.get(key) or "").strip()
                if value and value not in semantic_anchors:
                    semantic_anchors.append(value[:160])
                if len(semantic_anchors) >= 20:
                    break
            if len(semantic_anchors) >= 20:
                break
    landing_url = _url_without_query_or_fragment(str(getattr(page, "url", "") or ""))[:2048]
    html = ""
    dom_status = "unreadable"
    read_content = getattr(page, "content", None)
    if callable(read_content):
        try:
            html = _compact_dom_artifact(await read_content())
            dom_status = "readable" if isinstance(html, str) and html.strip() else "empty"
        except Exception:  # noqa: BLE001 - DOM 缺失不覆盖落地 URL 与截图
            html = ""
    landing_frame = await _capture_ad_navigation_frame(
        page=page,
        index=index,
        role="landing",
    )
    landing_screenshot = None
    if isinstance(landing_frame, dict) and isinstance(landing_frame.get("data"), str):
        landing_screenshot = {
            "purpose": "ad_landing",
            "encoding": "base64",
            "data": landing_frame["data"],
        }
    return {
        "url": landing_url,
        "title": title,
        "semantic_anchors": semantic_anchors,
        "dom_ref": "ad_landing_dom",
        "dom_status": dom_status,
        "dom": {
            "purpose": "ad_landing",
            "url": landing_url,
            "html": html,
        },
        "ui_snapshot": {
            **snapshot,
            "schema_version": str(snapshot.get("schema_version") or "ui-snapshot.v1"),
            "purpose": "ad_landing",
            "capture_mode": "isolated_safe_render",
            "url": landing_url,
            "elements": list(snapshot.get("elements") or []),
        },
        "har": {
            "schema_version": "ad-safe-transport-har.v1",
            "purpose": "ad_landing",
            "format": "safe_transport",
            "entries": list(har_entries),
        },
        "screenshot": landing_screenshot,
        "frame": landing_frame,
    }


def _attach_ad_navigation_evidence(
    *,
    action_trace: list[dict[str, Any]],
    before_url: str,
    after_url: str,
    source_target: dict[str, Any] | None,
    landing: dict[str, Any] | None,
    frame_count: int,
) -> None:
    """把广告点击与成对截图绑定；缺任一侧时明确保留不足状态。"""
    if not action_trace:
        return
    navigation = action_trace[-1].get("navigation")
    navigation = navigation if isinstance(navigation, dict) else {}
    # 成对证据只绑定被允许的隔离广告页；额外来源页另由回位合同否决。
    raw_new_page_count = navigation.get("isolated_new_page_count", navigation.get("new_page_count"))
    new_page_count = (
        raw_new_page_count
        if isinstance(raw_new_page_count, int) and not isinstance(raw_new_page_count, bool)
        else 0
    )
    landing_url = str((landing or {}).get("url") or "").strip()
    parsed_landing_url = urlsplit(landing_url)
    landing_url_valid = bool(
        parsed_landing_url.scheme.casefold() in {"http", "https"}
        and parsed_landing_url.netloc
        and landing_url != before_url
    )
    # 同页外跳会在落地页取证后回位；此时 after_url 与 before_url 相同，
    # 但 landing 已经是本次动作窗口内的真实页面，不应被回位动作抹掉。
    navigation_mode = (
        "ambiguous_new_pages"
        if new_page_count > 1
        else "new_page"
        if new_page_count == 1
        else "same_page"
        if after_url != before_url or landing_url_valid
        else "none"
    )
    pair_complete = bool(
        frame_count == 2
        and isinstance(landing, dict)
        and navigation_mode in {"new_page", "same_page"}
        and landing_url_valid
    )
    direct_ad = bool(
        action_trace[-1].get("action") == "direct_ad_outbound"
        or (
            action_trace[-1].get("action") == "navigate"
            and action_trace[-1].get("navigation_mode") == "isolated_context_goto"
        )
    )
    dom_complete = isinstance(landing, dict) and (
        direct_ad or landing.get("dom_status") == "readable"
    )
    status = (
        "observed_unreachable"
        if pair_complete and dom_complete and landing.get("reachable") is False
        else "observed"
        if pair_complete and dom_complete
        else "open_failed"
        if direct_ad
        else "evidence_insufficient"
    )
    action_trace[-1]["ad_navigation_evidence"] = {
        "schema_version": "ad-navigation-evidence.v1",
        "status": status,
        "navigation_mode": navigation_mode,
        "pair_complete": pair_complete,
        "source": {
            "url": _url_without_query_or_fragment(before_url)[:2048],
            "screenshot_ref": "ad_landing_consistency#frame-0",
            **({"target": source_target} if source_target else {}),
        },
        "landing": {
            key: value
            for key, value in {
                **(landing or {}),
                "screenshot_ref": "ad_landing_consistency#frame-1",
            }.items()
            if key != "frame" and value not in (None, "", [], {})
        },
        **(
            {}
            if pair_complete and dom_complete
            else {
                "reason_code": (
                    "WEB_AD_PAGE_OPEN_FAILED"
                    if direct_ad
                    else "WEB_AD_LANDING_EVIDENCE_INSUFFICIENT"
                ),
                "reason": "广告成对截图或可读非空落地 DOM 不足",
            }
        ),
    }


def _contains_active_countdown(visible_text: str) -> bool:
    """入口采集发现活动倒计时时立即留存时序证据，避免被页面识别耗时吃掉。"""
    return (
        re.search(
            r"\d+(?:\.\d+)?\s*(?:秒|s)\s*(?:后)?\s*(?:关闭|跳过)",
            visible_text,
            flags=re.IGNORECASE,
        )
        is not None
    )


def _bound_ui_snapshot_sequence(value: dict[str, Any]) -> dict[str, Any]:
    """限制整个 UI 时序包，优先保留首帧、尾帧和少量中间关键帧。"""
    frames = value.get("frames")
    if not isinstance(frames, list):
        return value
    original_count = len(frames)
    while len(frames) > 2 and _json_size_bytes(value) > MAX_UI_SNAPSHOT_BYTES:
        frames.pop(len(frames) // 2)
    while frames and _json_size_bytes(value) > MAX_UI_SNAPSHOT_BYTES:
        largest = max(
            (frame for frame in frames if isinstance(frame, dict)),
            key=lambda frame: (
                len(frame.get("elements", [])) if isinstance(frame.get("elements"), list) else 0
            ),
            default=None,
        )
        if largest is None or not isinstance(largest.get("elements"), list):
            break
        if not largest["elements"]:
            break
        largest["elements"].pop()
        largest["truncated_reason"] = "sequence_serialized_size_limit"
    if len(frames) != original_count or _json_size_bytes(value) > MAX_UI_SNAPSHOT_BYTES:
        value["truncated"] = True
        value["truncated_reason"] = "sequence_serialized_size_limit"
        value["original_frame_count"] = original_count
    return value


async def _install_countdown_monitor(page: Any) -> int:
    """用页面单调时钟监听倒计时目标，保存开始、异常与结束事件。"""
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return 0
    try:
        value = await evaluate(
            """
            () => {
              const visible = (element) => {
                if (!(element instanceof Element)) return false;
                const style = getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.display !== "none"
                  && style.visibility !== "hidden"
                  && Number(style.opacity || 1) > 0
                  && rect.width > 0
                  && rect.height > 0;
              };
              const flutterCountdownTargets = [...document.querySelectorAll(
                'flt-semantics[flt-tappable][role="text"]'
              )].filter((element) => {
                if (!visible(element)) return false;
                const text = String(element.innerText || element.textContent || "")
                  .replace(/\\s+/g, "");
                if (!/^\\d{1,2}$/.test(text)) return false;
                const rect = element.getBoundingClientRect();
                const layer = element.parentElement?.closest(
                  'flt-semantics[flt-tappable]'
                );
                if (!layer || !visible(layer)) return false;
                const layerRect = layer.getBoundingClientRect();
                return rect.width >= 30 && rect.width <= 100
                  && rect.height >= 20 && rect.height <= 60
                  && rect.x >= window.innerWidth * 0.65
                  && rect.right <= window.innerWidth + 2
                  && rect.y >= 0 && rect.y <= window.innerHeight * 0.1
                  && Math.abs(layerRect.x) <= 2
                  && Math.abs(layerRect.y) <= 2
                  && layerRect.width >= window.innerWidth * 0.95
                  && layerRect.height >= window.innerHeight * 0.95;
              });
              const candidates = [...document.querySelectorAll(
                "[class*='countdown'],[class*='timer'],[class*='down-time'],[data-countdown]",
              )];
              const target = candidates.find((item) => item.getClientRects().length > 0)
                || [...document.querySelectorAll("button,span,div")].find((item) => (
                  /(?:跳过|关闭|skip).{0,12}(?:秒|s)/i.test(item.innerText || "")
                ))
                || (flutterCountdownTargets.length === 1
                  ? flutterCountdownTargets[0]
                  : null);
              const monitor = {
                started_at_ms: performance.now(),
                target,
                previous_number: null,
                events: [],
              };
              const readText = () => String(target?.innerText || target?.textContent || "")
                .replace(/\\s+/g, " ").trim().slice(0, 300);
              const record = (kind) => {
                const text = readText();
                const match = text.match(/\\d+(?:\\.\\d+)?/);
                const number = match ? Number(match[0]) : null;
                let eventKind = kind;
                if (number !== null && monitor.previous_number !== null
                  && number > monitor.previous_number) eventKind = "anomaly";
                else if ((number !== null && number <= 0) || !text
                  || (number === null && /(?:关闭|跳过|close|skip)/i.test(text))) {
                  eventKind = "end";
                }
                monitor.events.push({
                  kind: eventKind,
                  offset_ms: performance.now() - monitor.started_at_ms,
                  text,
                  value: number,
                });
                monitor.previous_number = number;
                if (monitor.events.length > 100) monitor.events.shift();
              };
              if (target) {
                record("start");
                monitor.observer = new MutationObserver(() => record("change"));
                monitor.observer.observe(
                  target.parentElement || target,
                  { subtree: true, childList: true, characterData: true },
                );
              }
              window.__aliothCountdownMonitor = monitor;
              return monitor.events.length;
            }
            """
        )
        return int(value) if isinstance(value, int | float) else 0
    except Exception:  # noqa: BLE001
        return 0


async def _wait_for_countdown_change(
    *, page: Any, previous_event_count: int, timeout_ms: int
) -> int:
    wait_for_function = getattr(page, "wait_for_function", None)
    evaluate = getattr(page, "evaluate", None)
    if callable(wait_for_function) and callable(evaluate):
        try:
            await wait_for_function(
                "previous => window.__aliothCountdownMonitor"
                " && window.__aliothCountdownMonitor.events.length > previous",
                previous_event_count,
                timeout=timeout_ms,
            )
            value = await evaluate("() => window.__aliothCountdownMonitor.events.length")
            return int(value) if isinstance(value, int | float) else previous_event_count
        except Exception:  # noqa: BLE001
            return previous_event_count
    await asyncio.sleep(timeout_ms / 1000)
    return previous_event_count


async def _read_countdown_events(page: Any) -> list[dict[str, Any]]:
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return []
    try:
        value = await evaluate(
            """
            () => {
              const monitor = window.__aliothCountdownMonitor;
              if (!monitor) return [];
              if (monitor.target && !monitor.target.isConnected) {
                monitor.events.push({
                  kind: "end",
                  offset_ms: performance.now() - monitor.started_at_ms,
                  text: "",
                  value: null,
                });
              }
              return monitor.events.slice(0, 100).map((item) => ({ ...item }));
            }
            """
        )
        return (
            [dict(item) for item in value if isinstance(item, dict)]
            if isinstance(value, list)
            else []
        )
    except Exception:  # noqa: BLE001
        return []


def _collect_runtime_artifacts(
    *,
    console_logs: list[dict[str, Any]],
    network_requests: list[dict[str, Any]],
    action_trace: list[dict[str, Any]],
) -> list[PageCaptureArtifact]:
    artifacts: list[PageCaptureArtifact] = []
    if console_logs:
        artifacts.append(
            PageCaptureArtifact(
                artifact_type="console_log",
                artifact_id=None,
                content=console_logs,
            )
        )
    artifacts.append(
        PageCaptureArtifact(
            artifact_type="har",
            artifact_id=None,
            content={
                "format": "network_events",
                "entries": network_requests,
            },
        )
    )
    artifacts.append(
        PageCaptureArtifact(
            artifact_type="action_trace",
            artifact_id=None,
            content=action_trace,
        )
    )
    return artifacts


_H5_SCROLL_CONTAINER_VISIBLE_JS = """
(() => {
  const rect = element.getBoundingClientRect();
  if (style.display === "none" || style.visibility === "hidden"
    || style.visibility === "collapse" || Number(style.opacity) === 0
    || rect.width <= 0 || rect.height <= 0
    || rect.right <= 0 || rect.left >= window.innerWidth
    || rect.bottom <= 0 || rect.top >= window.innerHeight) return false;
  const hit = document.elementFromPoint(
    (Math.max(0, rect.left) + Math.min(window.innerWidth, rect.right)) / 2,
    (Math.max(0, rect.top) + Math.min(window.innerHeight, rect.bottom)) / 2,
  );
  return Boolean(hit && (hit === element || element.contains(hit)));
})()
"""


# 已识别的主体列表优先于包裹它的微小溢出外壳，读位置与执行使用同一选择规则。
_H5_SCROLL_CONTAINER_PRIORITY_JS = """
candidates.filter(element => !candidates.some(nested =>
  nested !== element && element.contains(nested)
  && nested.clientWidth >= window.innerWidth * 0.6
  && nested.clientHeight >= window.innerHeight * 0.35
  && nested.scrollHeight > nested.clientHeight * 1.25
))
"""


async def _collect_scroll_state(
    page: Any, *, surface_session_id: str | None = None,
) -> dict[str, Any]:
    """读取真实滚动容器、位置和当前视口内容指纹素材。"""
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return {"url": str(getattr(page, "url", "") or ""), "content_items": []}
    try:
        raw = await evaluate(
            """
            () => {
              const candidates = [...document.querySelectorAll("*")].filter((element) => {
                const style = getComputedStyle(element);
                return /(auto|scroll)/.test(style.overflowY)
                  && element.scrollHeight > element.clientHeight + 1
                  && __H5_SCROLL_CONTAINER_VISIBLE__;
              });
              const prioritized = __H5_SCROLL_CONTAINER_PRIORITY__;
              const container = prioritized.sort(
                (left, right) => (right.clientWidth * right.clientHeight)
                  - (left.clientWidth * left.clientHeight),
              )[0] || document.scrollingElement || document.documentElement;
              const containerRect = container.getBoundingClientRect();
              const selector = container === document.scrollingElement
                || container === document.documentElement
                ? "document"
                : (container.id
                  ? `#${CSS.escape(container.id)}`
                  : `${container.tagName.toLowerCase()}.${[...container.classList]
                    .slice(0, 2).map((item) => CSS.escape(item)).join(".")}`);
              const contentItems = [...container.querySelectorAll(
                "li,article,[role='listitem'],[data-id],[data-key],a,h1,h2,h3,p,img",
              )].filter((element) => {
                const rect = element.getBoundingClientRect();
                return rect.bottom >= Math.max(containerRect.top, 0)
                  && rect.top <= Math.min(containerRect.bottom, window.innerHeight);
              }).slice(0, 500).map((element) => ({
                tag: element.tagName.toLowerCase(),
                id: element.getAttribute("data-id")
                  || element.getAttribute("data-key")
                  || element.id,
                text: String(element.innerText || element.getAttribute("alt") || "")
                  .replace(/\\s+/g, " ").trim().slice(0, 160),
              }));
              const scrollTop = container === document.scrollingElement
                || container === document.documentElement
                ? window.scrollY
                : container.scrollTop;
              return {
                url: window.location.href,
                container_selector: selector.slice(0, 512),
                scroll_top: Math.round(scrollTop * 100) / 100,
                scroll_height: container.scrollHeight,
                client_height: container.clientHeight || window.innerHeight,
                at_bottom: scrollTop + (container.clientHeight || window.innerHeight)
                  >= container.scrollHeight - 2,
                content_items: contentItems,
              };
            }
            """.replace(
                "__H5_SCROLL_CONTAINER_VISIBLE__",
                _H5_SCROLL_CONTAINER_VISIBLE_JS
                if str(surface_session_id or "").startswith("h5:") else "true",
            ).replace(
                "__H5_SCROLL_CONTAINER_PRIORITY__",
                _H5_SCROLL_CONTAINER_PRIORITY_JS
                if str(surface_session_id or "").startswith("h5:") else "candidates",
            )
        )
    except Exception:  # noqa: BLE001
        return {"url": str(getattr(page, "url", "") or ""), "content_items": []}
    state = dict(raw) if isinstance(raw, dict) else {}
    raw_items = state.pop("content_items", [])
    items = (
        [item for item in raw_items if isinstance(item, dict)]
        if isinstance(raw_items, list)
        else []
    )
    fingerprints = [
        hashlib.sha256(
            json.dumps(item, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        for item in items
    ]
    state["content_fingerprints"] = fingerprints
    state["content_fingerprint"] = hashlib.sha256(
        "\n".join(fingerprints).encode("ascii")
    ).hexdigest()
    return state


async def _collect_horizontal_scroll_state(
    page: Any,
    *,
    selector: str,
) -> dict[str, Any]:
    """读取已验签会员容器的横向位置；运行时不重新猜测其他容器。"""
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return {
            "url": str(getattr(page, "url", "") or ""),
            "container_selector": selector,
        }
    try:
        raw = await evaluate(
            """
            selector => {
              const matches = [...document.querySelectorAll(selector)];
              if (matches.length !== 1) return null;
              const container = matches[0];
              const scrollLeft = Number(container.scrollLeft || 0);
              const scrollWidth = Number(container.scrollWidth || 0);
              const clientWidth = Number(container.clientWidth || 0);
              const swiper = container.classList.contains("swiper-horizontal")
                ? container.swiper
                : null;
              const swiperActiveIndex = Number(swiper?.activeIndex);
              const swiperSlideCount = Number(swiper?.slides?.length || 0);
              const trustedSwiper = Boolean(
                swiper
                && !swiper.destroyed
                && Number.isFinite(swiperActiveIndex)
                && swiperSlideCount > 0
                && typeof swiper.slideNext === "function"
              );
              const swiperIsEnd = trustedSwiper && Boolean(
                swiper.isEnd || swiperActiveIndex >= swiperSlideCount - 1
              );
              return {
                url: window.location.href,
                container_selector: selector,
                scroll_left: Math.round(scrollLeft * 100) / 100,
                scroll_width: scrollWidth,
                client_width: clientWidth,
                scroll_mode: trustedSwiper ? "swiper" : "native",
                swiper_active_index: trustedSwiper ? swiperActiveIndex : null,
                swiper_slide_count: trustedSwiper ? swiperSlideCount : null,
                swiper_is_end: trustedSwiper ? swiperIsEnd : null,
                at_end: trustedSwiper
                  ? swiperIsEnd
                  : scrollLeft + clientWidth >= scrollWidth - 2,
              };
            }
            """,
            selector,
        )
    except Exception:  # noqa: BLE001
        raw = None
    if not isinstance(raw, dict):
        raise BlockedPageCaptureActionError(
            "会员套餐横向容器在执行时已失效，请重新观察当前页面。",
            error_code="LOGICAL_TARGET_INVALID",
        )
    return dict(raw)


async def _enable_flutter_semantics_if_available(page: Any) -> str:
    """仅激活 Flutter 官方可访问性占位控件，使语义节点进入可信 DOM。"""
    locator_factory = getattr(page, "locator", None)
    if not callable(locator_factory):
        return "unavailable"
    try:
        semantics = locator_factory("flt-semantics-host flt-semantics")
        if await semantics.count() > 0:
            return "already_enabled"
        placeholder = locator_factory(
            'flt-semantics-placeholder[role="button"][aria-label="Enable accessibility"]'
        )
        if await placeholder.count() == 0:
            return "not_applicable"
        if await placeholder.count() != 1:
            return "ambiguous"
        # Flutter 将入口放在视口外 1px 位置，Playwright 普通点击不可用；
        # 只对完整标签、角色和 aria-label 匹配的官方占位节点派发原生事件。
        await placeholder.evaluate("(element) => element.click()")
        await semantics.first.wait_for(state="attached", timeout=2300)
        return "enabled"
    except Exception:  # noqa: BLE001 - 可访问性增强失败不得破坏原始证据采集
        return "failed"


async def _install_logical_surface_history_observer(context: Any) -> None:
    """在逻辑采集开启时记录 History API 事实，不暴露业务参数。"""
    add_init_script = getattr(context, "add_init_script", None)
    if not callable(add_init_script):
        return
    try:
        await add_init_script(
            script="""
            (() => {
              const key = Symbol.for("alioth.logicalSurface.history");
              if (globalThis[key]) return;
              const state = {
                push_state_count: 0,
                replace_state_count: 0,
                pop_state_count: 0,
                hash_change_count: 0,
              };
              Object.defineProperty(globalThis, key, {
                value: state,
                configurable: false,
                enumerable: false,
                writable: false,
              });
              for (const [name, field] of [
                ["pushState", "push_state_count"],
                ["replaceState", "replace_state_count"],
              ]) {
                const original = history[name];
                if (typeof original !== "function") continue;
                history[name] = function (...args) {
                  state[field] += 1;
                  return Reflect.apply(original, this, args);
                };
              }
              addEventListener("popstate", () => { state.pop_state_count += 1; });
              addEventListener("hashchange", () => { state.hash_change_count += 1; });
            })();
            """
        )
    except Exception:  # noqa: BLE001 - History 补证失败不能破坏页面采集
        return


async def _collect_ui_snapshot(
    page: Any,
    *,
    logical_surface_enabled: bool = False,
    logical_surface_shadow: bool = False,
    surface_session_id: str | None = None,
    web_detail_candidate_priority: bool = False,
    defer_logical_surface: bool = False,
) -> dict[str, Any]:
    """采集有界、可复现的渲染事实，避免 Judge 从模型摘要反推页面状态。"""
    flutter_semantics_activation = await _enable_flutter_semantics_if_available(page)
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return _finish_ui_snapshot_collection(
            {
                "schema_version": "ui-snapshot.v2",
                "url": str(getattr(page, "url", "") or ""),
                "elements": [],
                "element_count": 0,
                "capture_degraded": "page_evaluate_unavailable",
                "flutter_semantics_activation": flutter_semantics_activation,
            },
            page=page,
            logical_surface_enabled=logical_surface_enabled,
            logical_surface_shadow=logical_surface_shadow,
            surface_session_id=surface_session_id,
            defer_logical_surface=defer_logical_surface,
        )
    generic_navigation_selectors = [
        f"[class~='{class_name}']"
        for class_name in sorted(
            H5_GENERIC_NAVIGATION_CONTAINER_CLASSES | H5_GENERIC_NAVIGATION_ITEM_CLASSES
        )
    ]
    article_body_selector = WEB_ARTICLE_BODY_SELECTOR
    if web_detail_candidate_priority:
        from services.api.app.application.web_article_body_structure import (
            WEB_RENDERED_ARTICLE_SELECTOR,
        )

        article_body_selector += "," + WEB_RENDERED_ARTICLE_SELECTOR
    snapshot_script = (
        """
        async () => {
          __H5_LOCAL_MEDIA_DIGEST__
          const ELEMENT_LIMIT = 300;
          const TEXT_LIMIT = 300;
          const SELECTOR_LIMIT = 512;
          const URL_LIMIT = 2048;
          const CLASS_TOKEN_LIMIT = 100;
          const round = (value) => Number.isFinite(value)
            ? Math.round(value * 100) / 100
            : null;
          const trimText = (value) => String(value || "")
            .replace(/\\s+/g, " ")
            .trim()
            .slice(0, TEXT_LIMIT);
          const visible = (element, style, rect) => {
            if (rect.width <= 0 || rect.height <= 0) return false;
            let current = element;
            while (current instanceof Element) {
              const currentStyle = current === element ? style : getComputedStyle(current);
              if (
                current.hidden
                || current.getAttribute("aria-hidden") === "true"
                || currentStyle.display === "none"
                || ["hidden", "collapse"].includes(currentStyle.visibility)
                || currentStyle.contentVisibility === "hidden"
                || Number(currentStyle.opacity || 1) <= 0
              ) return false;
              current = current.parentElement;
            }
            return true;
          };
          const boundsFor = (rect) => ({
            x: round(rect.x),
            y: round(rect.y),
            width: round(rect.width),
            height: round(rect.height),
          });
          const unionBounds = (bounds, fallback) => {
            if (!bounds.length) return fallback;
            const left = Math.min(...bounds.map((item) => item.x));
            const top = Math.min(...bounds.map((item) => item.y));
            const right = Math.max(...bounds.map((item) => item.x + item.width));
            const bottom = Math.max(...bounds.map((item) => item.y + item.height));
            return {
              x: round(left),
              y: round(top),
              width: round(right - left),
              height: round(bottom - top),
            };
          };
          const viewportCoverage = (bounds) => {
            const left = Math.max(bounds.x, 0);
            const top = Math.max(bounds.y, 0);
            const right = Math.min(bounds.x + bounds.width, window.innerWidth);
            const bottom = Math.min(bounds.y + bounds.height, window.innerHeight);
            const visibleArea = Math.max(right - left, 0) * Math.max(bottom - top, 0);
            return Math.min(visibleArea / Math.max(window.innerWidth * window.innerHeight, 1), 1);
          };
          __UNIQUE_SELECTOR_FOR__
          const resourceCache = new Map();
          const summarizeResource = async (value, allowModalBlob = false) => {
            if (!value) return { value: null, summary: null };
            const raw = String(value);
            const modalBlob = __H5_MODAL_CENSUS__ && allowModalBlob && raw.startsWith("blob:");
            if (!raw.startsWith("data:") && !modalBlob) {
              return { value: raw.slice(0, URL_LIMIT), summary: null };
            }
            if (resourceCache.has(raw)) return resourceCache.get(raw);
            const comma = raw.indexOf(",");
            const metadata = comma >= 0 ? raw.slice(5, comma) : "";
            const payload = comma >= 0 ? raw.slice(comma + 1) : "";
            const parts = metadata.split(";");
            let mime = parts[0] || "text/plain";
            const isBase64 = parts.some((item) => item.toLowerCase() === "base64");
            let bytes;
            try {
              if (modalBlob) {
                // 只读取已加载弹层的本地 Blob，不发起远端图片下载，也不使用随机 URL 当身份。
                const controller = new AbortController();
                const timer = setTimeout(() => controller.abort(), 500);
                try {
                  const response = await fetch(raw, {signal:controller.signal});
                  if (!response.ok) throw new Error("modal_blob_unavailable");
                  const blob = await response.blob();
                  if (blob.size > 5 * 1024 * 1024) throw new Error("modal_blob_size_limit");
                  mime = blob.type || "application/octet-stream";
                  bytes = new Uint8Array(await blob.arrayBuffer());
                } finally {
                  clearTimeout(timer);
                }
              } else if (isBase64) {
                const decoded = atob(payload);
                bytes = Uint8Array.from(decoded, (char) => char.charCodeAt(0));
              } else {
                bytes = new TextEncoder().encode(decodeURIComponent(payload));
              }
            } catch {
              if (modalBlob) return {value:null,summary:null};
              bytes = new TextEncoder().encode(payload);
            }
            let sha256 = null;
            let digestUnavailable = null;
            if (__H5_MODAL_CENSUS__ && (modalBlob || raw.startsWith("data:"))) {
              try {
                sha256 = await h5LocalMediaDigest(bytes);
              } catch {
                digestUnavailable = "h5_local_media_digest_unavailable";
              }
            } else if (globalThis.crypto?.subtle?.digest) {
              try {
                const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes);
                sha256 = [...new Uint8Array(digest)]
                  .map((item) => item.toString(16).padStart(2, "0"))
                  .join("");
              } catch {
                digestUnavailable = "crypto_digest_failed";
              }
            } else {
              digestUnavailable = "crypto_subtle_unavailable";
            }
            const summary = {
              scheme: modalBlob ? "blob" : "data",
              mime,
              byte_length: bytes.byteLength,
              sha256,
              ...(digestUnavailable ? { digest_unavailable: digestUnavailable } : {}),
            };
            const result = {
              value: `${modalBlob ? "blob" : "data"}:${mime};bytes=${bytes.byteLength}`
                + `;sha256=${sha256 || "unavailable"}`
                + (digestUnavailable ? `;digest=${digestUnavailable}` : ""),
              summary,
            };
            resourceCache.set(raw, result);
            return result;
          };
          const origin = window.location.origin;
          const elements = [];
          const webArticleBodySelector = __WEB_ARTICLE_BODY_SELECTOR__;
          // 仅输出正文数字长度，次要区域和控件文本不进入主体事实。
          const webSubjectTextLength = (root) => {
            const excluded = [
              "nav", "aside", "footer", "script", "style", "noscript",
              "h1", "h2", "h3", "h4", "h5", "h6", "figcaption",
              "button", "input", "textarea", "select", "option",
              "[role='navigation']", "[role='complementary']", "[role='button']",
              "[hidden]", "[aria-hidden='true']",
              ".recommendations", ".related", ".related-content", ".related-posts", ".recommended",
              ".advertisement", ".advertising", ".ad-slot", ".sponsored",
            ].join(",");
            const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
            let length = 0;
            let visited = 0;
            for (let text = walker.nextNode(); text && visited < 20000;
              text = walker.nextNode()) {
              visited += 1;
              let parent = text.parentElement;
              let blocked = false;
              while (parent && root.contains(parent)) {
                const style = getComputedStyle(parent);
                const recommendation = [...parent.classList].some((token) =>
                  /(?:^|[-_])(?:recommend|recommendations|recommended|related)(?:[-_]|$)/i
                    .test(token));
                if (parent.matches(excluded) || recommendation
                  || (__WEB_DETAIL_CANDIDATE_PRIORITY__
                    && hasWebAdvertisementSemantics(parent)) || style.display === "none"
                  || style.visibility === "hidden" || Number(style.opacity) === 0) {
                  blocked = true;
                  break;
                }
                if (parent === root) break;
                parent = parent.parentElement;
              }
              if (!blocked) length += text.data.trim().length;
              if (length >= 1000000) return 1000000;
            }
            return length;
          };
          const regularCandidates = document.querySelectorAll(
            ".dynamicData-box .dynamicData-content .title,"
            + ".video-section .img-box,"
            + "[class~='horizontalVideoBox'][class~='videoBox'],"
            + __HOME_VIDEO_TITLE_SELECTOR__ + ","
            + ".ai-page-card .ai-page-card-title,"
            + ".home-suspend,"
            + "a,button,input,textarea,select,img,video,source,audio,canvas,svg,"
            + "main,article,"
            + "h1,h2,h3,h4,h5,h6,p,span,label,li,td,th,figcaption,"
            + "[class~='title'],[class~='name'],"
            + "[role],[aria-label],[onclick],[data-route],[data-href],[data-url],"
            + "router-link,[class~='btn'],"
            + "[class*='dialog'],[class*='modal'],"
            + "[class*='overlay'],[class*='popup'],[class*='toast'],[class*='timer'],"
            + "[class*='countdown'],[class*='count-down'],[class*='module'],[class*='content'],"
            + "[class*='comment'],[class*='post'],[class*='rank'],[class*='price'],"
            + "[class*='activity'],[class*='advert'],[class*=' ad-'],[data-testid],"
            + "[class*='fullscreen'],[class*='full-screen'],"
            + "flt-semantics-host flt-semantics[flt-tappable]",
          );
          const startupAdDismissalSelector = [
            ".globalLaunch .header-title > span:last-child",
            ".countdown > .jump_over",
            ".van-popup .skip-wrap",
            ".van-popup .count-down",
            ".count-down",
            ".launch-page .screen-ad > .button",
            ".homepagePaymentPopup .imgWrapper > .close",
          ].join(",");
          const obstructionSelector = [
            "[role='dialog']",
            "[aria-modal='true']",
            "[class*='dialog']",
            "[class*='modal']",
            "[class*='overlay']",
            "[class*='popup']",
            ".loading-page",
            ".globalLaunch",
            ".wrapper:has(.countdown)",
            ".van-popup",
            ".launch-page",
            ".home-suspend",
            ".recommend-layer",
          ].join(",");
          const webObstructionLayerSelector = [
            "[role='dialog']",
            "[aria-modal='true']",
            "[class~='van-dialog']",
            "[class~='van-overlay']",
            "[class~='van-popup']",
            "[class~='modal']",
            "[class~='dialog']",
            "[class~='popup']",
            "[class~='overlay']",
            "[class~='recommend-layer']",
            "[id*='age-gate' i]",
            "[class*='age-gate' i]",
            "[id*='adult-gate' i]",
            "[class*='adult-gate' i]",
            "[id*='login-modal' i]",
            "[class*='login-modal' i]",
            "[id*='register-modal' i]",
            "[class*='register-modal' i]",
          ].join(",");
          const obstructionDismissalSelector = [
            ".down-time",
            ".globalLaunch .header-title > span:last-child",
            ".countdown > .jump_over",
            ".van-popup .skip-wrap",
            ".van-popup .count-down",
            ".count-down",
            ".launch-page .screen-ad > .button",
            "[class~='btn']",
            "[class*='close']",
            "[class*='cancel']",
            "[class*='confirm']",
            "[aria-label*='close' i]",
            "[aria-label*='关闭']",
          ].join(",");
          const interactiveSelector = [
            "a",
            "button",
            "input",
            "select",
            "textarea",
            "summary",
            "[role='button']",
            "[role='link']",
            "[onclick]",
            "[data-route]",
            "[data-href]",
            "[data-url]",
            "router-link",
            "[tabindex]",
          ].join(",");
          const adaptiveNavigationGroupFor = (element) => {
            if (__WEB_DETAIL_CANDIDATE_PRIORITY__ || !element) return null;
            let current = element.parentElement;
            for (let depth = 0; current && depth < 10; depth += 1) {
              const style = getComputedStyle(current);
              const rect = current.getBoundingClientRect();
              const tag = current.tagName.toLowerCase();
              const role = String(current.getAttribute("role") || "").toLowerCase();
              if (tag === "nav" || role === "navigation") return current;
              if (
                ["fixed", "sticky"].includes(String(style.position || "").toLowerCase())
                && rect.width >= window.innerWidth * 0.45
                && rect.height > 0
                && rect.height <= window.innerHeight * 0.3
                && rect.top >= window.innerHeight * 0.62
                && rect.bottom <= window.innerHeight + 8
              ) return current;
              current = current.parentElement;
            }
            return null;
          };
          const adaptiveNavigationHitCandidates = () => {
            if (__WEB_DETAIL_CANDIDATE_PRIORITY__) return [];
            const candidates = new Set();
            const columnCount = Math.max(
              10,
              Math.min(20, Math.ceil(window.innerWidth / 28)),
            );
            const yRatios = [0.69, 0.75, 0.81, 0.87, 0.93, 0.975];
            for (const yRatio of yRatios) {
              const y = Math.min(window.innerHeight - 1, window.innerHeight * yRatio);
              for (let column = 0; column < columnCount; column += 1) {
                const x = window.innerWidth * (column + 0.5) / columnCount;
                const stack = document.elementsFromPoint(x, y).slice(0, 8);
                for (const hit of stack) {
                  let current = hit;
                  for (let depth = 0; current && depth < 8; depth += 1) {
                    if (current === document.body || current === document.documentElement) break;
                    const style = getComputedStyle(current);
                    const role = String(current.getAttribute("role") || "").toLowerCase();
                    const label = trimText(
                      current.getAttribute("aria-label")
                      || current.innerText
                      || current.getAttribute("alt")
                      || current.getAttribute("title"),
                    );
                    const interactive = Boolean(
                      current.matches(interactiveSelector)
                      || ["menuitem", "tab"].includes(role)
                      || style.cursor === "pointer"
                    );
                    if (
                      interactive
                      && label
                      && adaptiveNavigationGroupFor(current)
                    ) {
                      candidates.add(current);
                      break;
                    }
                    current = current.parentElement;
                  }
                }
              }
            }
            return [...candidates].slice(0, 80);
          };
          const bottomNavigationSelector = [
            "[class*='tabbar']",
            "[class*='tab-bar']",
            "[class*='bottom-nav']",
            "[class*='bottom_nav']",
            "[class~='pages-foot-fix']",
            "[class~='footer-wrap']",
            ...__GENERIC_NAVIGATION_SELECTORS__,
          ].join(",");
          const pageTabNavigationSelector = [
            "[role='tablist']",
            "[role='tab']",
            "[class~='van-tabs__nav']",
            "[class~='van-tab']",
            "[class*='tab-nav']",
          ].join(",");
          const generalNavigationSelector = "nav,[role='navigation']";
          const floatingObstructionSelector = [
            ".home-suspend",
            ".home-suspend .btn-close",
            ".home-suspend .van-icon-close",
            "[class*='suspend']",
            "[class*='suspend'] [class*='close']",
            "[class*='float'] [class*='close']",
            "[class*='floating'] [class*='close']",
          ].join(",");
          // 模态弹窗常挂在 body 尾部；内容重的页面按文档序收集会在 300 上限前
          // 收满，弹窗被截掉导致执行端"未证明可关闭的唯一弹窗"死循环，必须最优先
          const modalObstructionSelector = [
            "[role='dialog']",
            "[aria-modal='true']",
            "[class~='van-dialog']",
            "[class~='van-overlay']",
            "[class~='van-popup']",
            "[class*='dialog'] [class*='close']",
            "[class*='popup'] [class*='close']",
            "[class*='modal'] [class*='close']",
            "[class*='overlay'] [class*='close']",
            "[class*='dialog'] [class*='btn']",
            "[class*='popup'] [class*='btn']",
            ".recommend-layer",
            ".recommend-layer > .recommend-wrapper > .recommend-card > .close-btn",
          ].join(",");
          // 内容密集的全屏广告会在关闭控件之前生成上百个 .btn；若把根节点、
          // 广告按钮和关闭控件放在同一次文档序查询中再截断，尾部关闭控件会丢失。
          // 因此先单独收集模态层关闭控件，再收集其余模态结构。
          const modalDismissalSelector = [
            "[role='dialog'] [class*='close']",
            "[aria-modal='true'] [class*='close']",
            "[class~='van-dialog'] [class*='close']",
            "[class~='van-popup'] [class*='close']",
            "[class*='dialog'] [class*='close']",
            "[class*='modal'] [class*='close']",
            "[class*='overlay'] [class*='close']",
            "[class*='popup'] [class*='close']",
            ".recommend-layer > .recommend-wrapper > .recommend-card > .close-btn",
          ].join(",");
          const flutterAppOpenPopupCandidates = [
            ...document.querySelectorAll(
              "flt-semantics-host flt-semantics[flt-tappable]",
            ),
          ].filter((element) => {
            const rect = element.getBoundingClientRect();
            const text = trimText(element.innerText || element.textContent);
            const role = String(element.getAttribute("role") || "").toLowerCase();
            const isAppOpenLabel = role === "text" && /APP内打开$/i.test(text);
            const isEmptySemanticControl = !text && !role;
            const isSmallSquare = (
              rect.width >= 20
              && rect.width <= 64
              && rect.height >= 20
              && rect.height <= 64
              && Math.abs(rect.width - rect.height) <= 8
            );
            const isPopupBody = (
              rect.width >= 240
              && rect.width <= 360
              && rect.height >= 240
              && rect.height <= 500
            );
            return isAppOpenLabel || (isEmptySemanticControl && (isSmallSquare || isPopupBody));
          });
          const flutterScrollablePopupCandidates = [
            ...document.querySelectorAll(
              "flt-semantics-host flt-semantics:not([flt-tappable])",
            ),
          ].filter((element) => {
            const style = getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return !element.getAttribute("role")
              && ["auto", "scroll"].includes(style.overflowY)
              && element.scrollHeight > element.clientHeight + 1
              && rect.width >= 240
              && rect.width <= 360
              && rect.height >= 240
              && rect.height <= 500;
          });
          const flutterBottomNavigationCandidates = [
            ...document.querySelectorAll(
              "flt-semantics-host flt-semantics[flt-tappable]",
            ),
          ].filter((element) => {
            const rect = element.getBoundingClientRect();
            return rect.width >= 20
              && rect.width <= 100
              && rect.height >= 36
              && rect.height <= 80
              && rect.left >= -4
              && rect.right <= window.innerWidth + 4
              && rect.top >= window.innerHeight * 0.82
              && rect.bottom <= window.innerHeight + 8;
          }).slice(0, 20);
          const candidatesWithin = (selector) => [...regularCandidates].filter(
            (element) => Boolean(element.closest(selector)),
          );
          const priorityAdaptiveNavigationCandidates = __WEB_DETAIL_CANDIDATE_PRIORITY__
            ? []
            : [...new Set([
              ...adaptiveNavigationHitCandidates(),
              ...regularCandidates,
            ])].filter((element) => {
              const rect = element.getBoundingClientRect();
              const style = getComputedStyle(element);
              const role = String(element.getAttribute("role") || "").toLowerCase();
              const navigationGroup = adaptiveNavigationGroupFor(element);
              const semanticNavigationGroup = Boolean(
                navigationGroup
                && (
                  navigationGroup.tagName.toLowerCase() === "nav"
                  || String(navigationGroup.getAttribute("role") || "").toLowerCase()
                    === "navigation"
                )
              );
              const interactive = Boolean(
                element.matches(interactiveSelector)
                || ["menuitem", "tab"].includes(role)
                || style.cursor === "pointer"
              );
              return interactive
                && navigationGroup
                && visible(element, style, rect)
                && rect.width >= 20
                && rect.height >= 20
                && rect.width <= window.innerWidth * 0.5
                && rect.height <= Math.min(160, window.innerHeight * 0.22)
                && (
                  semanticNavigationGroup
                  || rect.top + rect.height / 2 >= window.innerHeight * 0.68
                )
                && rect.top < window.innerHeight
                && rect.bottom <= window.innerHeight + 4;
            }).slice(0, 80);
          const priorityModalDismissalCandidates = [
            ...document.querySelectorAll(modalDismissalSelector),
          ].slice(0, 40);
          const priorityModalCandidates = [
            ...document.querySelectorAll(modalObstructionSelector),
          ].slice(0, 60);
          // H5 保留现场顶层容器及其控件；是否能关闭由同帧证明判断，不按站点类名。
          const h5ModalNodes = __H5_MODAL_CENSUS__
            ? [...document.querySelectorAll(
              "div,section,aside,dialog,[role='dialog'],[aria-modal='true']",
            )]
            : [];
          const h5ModalLayers = h5ModalNodes.slice(0, 4000).filter((element) => {
            const style = getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return visible(element, style, rect) && (
              element.matches("dialog,[role='dialog'],[aria-modal='true']")
              || (style.position === "fixed"
                && rect.width * rect.height >= window.innerWidth * window.innerHeight * 0.02
                && element.querySelector("[data-dismiss='modal'],[data-bs-dismiss='modal']"))
              || (["fixed", "absolute"].includes(style.position)
                && Number.parseInt(style.zIndex, 10) >= 100
                && rect.width * rect.height >= window.innerWidth * window.innerHeight * 0.8)
            );
          });
          const h5ModalLayerSet = new Set(h5ModalLayers.slice(0, 40));
          // 内部滚动区未必是可点击节点，独立采样，不能受候选选择器限制。
          const h5ScrollNodes = __H5_MODAL_CENSUS__
            ? [...document.querySelectorAll("*")].slice(0, 4000)
            : [];
          const h5ScrollRegions = h5ScrollNodes.filter((element) => {
            const style = getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            if (!visible(element, style, rect)
              || !["auto", "scroll"].includes(style.overflowY)
              || element.scrollHeight <= element.clientHeight * 1.25
              || rect.width < window.innerWidth * 0.6
              || rect.height < window.innerHeight * 0.35
              || rect.right <= 0 || rect.left >= window.innerWidth
              || rect.bottom <= 0 || rect.top >= window.innerHeight) return false;
            const hit = document.elementFromPoint(
              Math.max(0, Math.min(window.innerWidth - 1, rect.left + rect.width / 2)),
              Math.max(0, Math.min(window.innerHeight - 1, rect.top + rect.height / 2)),
            );
            return Boolean(hit && (hit === element || element.contains(hit)));
          }).slice(0, 16).map((element) => ({
            selector: selectorFor(element),
            scroll_top: round(element.scrollTop),
            scroll_height: element.scrollHeight,
            client_height: element.clientHeight,
          }));
          const h5ModalLocalSemantics = (element) => {
            const bounds = element.getBoundingClientRect();
            if (bounds.width > window.innerWidth || bounds.height > 100) return null;
            const label = (element.innerText || element.getAttribute("aria-label")
              || element.getAttribute("title") || element.getAttribute("alt") || "")
              .trim().toLowerCase();
            const iconSized = bounds.width >= 20 && bounds.width <= 64
              && bounds.height >= 20 && bounds.height <= 64
              && Math.abs(bounds.width - bounds.height) <= 12;
            const ownCloseMarker = [element.id, ...element.classList].some((token) =>
              /(?:^|[^a-z0-9])(close|dismiss)(?:$|[^a-z0-9])|^closebtn$|^btnclose$/i.test(token)
              || (iconSized && /(?:^|[^a-z0-9])(cancel|times|x)(?:$|[^a-z0-9])/i.test(token)));
            if (/^(关闭|关闭弹窗|关闭广告|跳过|忽略|稍后|close|dismiss|skip|x|×|✕|✖)$/.test(label)
              || ownCloseMarker
              || element.matches("[data-dismiss='modal'],[data-bs-dismiss='modal']")) {
              return "close";
            }
            if (/^(确认|确定|知道了|我知道了|我知道|好的|ok)$/.test(label)) return "acknowledge";
            return null;
          };
          // 框外关闭键可与 Dialog 同级；仅绑定同一局部根内唯一面板和遮罩。
          const h5ModalSiblingPanels = new Map();
          for (const panel of h5ModalLayerSet) {
            if (!panel.matches("dialog,[role='dialog'],[aria-modal='true']")) continue;
            const scope = panel.parentElement;
            if (!scope || scope === document.body || scope === document.documentElement) continue;
            const peers = [...h5ModalLayerSet].filter(layer => layer.parentElement === scope);
            if (peers.filter(layer => layer.matches(
              "dialog,[role='dialog'],[aria-modal='true']")).length !== 1
              || !peers.some(layer => layer !== panel
                && layer.getBoundingClientRect().width * layer.getBoundingClientRect().height
                  >= window.innerWidth * window.innerHeight * 0.8)) continue;
            const nodes = [...scope.querySelectorAll("*")];
            if (nodes.length > 4000) continue;
            const bounds = panel.getBoundingClientRect();
            for (const control of nodes) {
              if (peers.some(layer => layer === control || layer.contains(control))
                || h5ModalLocalSemantics(control) !== "close") continue;
              const rect = control.getBoundingClientRect();
              const x = rect.left + rect.width / 2, y = rect.top + rect.height / 2;
              if (x < 0 || x > innerWidth || y < 0 || y > innerHeight
                || x < bounds.left - 96 || x > bounds.right + 96
                || y < bounds.top - 96 || y > bounds.bottom + 96) continue;
              h5ModalSiblingPanels.set(control, panel);
            }
          }
          const h5ModalDescendants = [...new Set([
            ...[...h5ModalLayerSet].flatMap(layer => [...layer.querySelectorAll("*")]),
            ...h5ModalSiblingPanels.keys(),
          ])];
          const h5ModalControls = h5ModalDescendants.slice(0, 4000).filter((element) => {
            const style = getComputedStyle(element);
            if (!element.matches(interactiveSelector)
              && !element.matches("flt-semantics[flt-tappable]")
              && style.cursor !== "pointer"
              && !h5ModalLocalSemantics(element)
              && !element.matches("input,select,textarea,form,p,h1,h2,h3")) {
              return false;
            }
            if (!visible(element, style, element.getBoundingClientRect())) {
              return false;
            }
            if (h5ModalSiblingPanels.has(element)) return true;
            for (let parent = element.parentElement, depth = 0; parent && depth < 32;
              parent = parent.parentElement, depth += 1) {
              if (h5ModalLayerSet.has(parent)) return true;
            }
            return false;
          });
          const obstructionCandidates = [
            ...new Set([
              ...document.querySelectorAll(floatingObstructionSelector),
              ...document.querySelectorAll(obstructionSelector),
              ...candidatesWithin(obstructionSelector),
            ]),
          ];
          const priorityObstructionCandidates = [...new Set([
            ...flutterAppOpenPopupCandidates,
            ...flutterScrollablePopupCandidates,
            ...document.querySelectorAll(startupAdDismissalSelector),
            ...obstructionCandidates.filter((element) => Boolean(
              element.matches(obstructionDismissalSelector)
              || element.closest(obstructionDismissalSelector),
            )).slice(0, 80),
            ...obstructionCandidates.filter(
              (element) => element.matches(interactiveSelector),
            ).slice(0, 80),
            ...obstructionCandidates.slice(0, 160),
          ])];
          const priorityNavigationCandidates = [
            ...flutterBottomNavigationCandidates,
            ...priorityAdaptiveNavigationCandidates,
            ...document.querySelectorAll(bottomNavigationSelector),
            ...candidatesWithin(bottomNavigationSelector).slice(0, 60),
            ...candidatesWithin(pageTabNavigationSelector).slice(0, 120),
            ...candidatesWithin(generalNavigationSelector).slice(0, 60),
          ];
          const profileResourceBasename = (element) => {
            const source = element.getAttribute("src");
            if (!source) return "";
            try {
              const parts = new URL(source, document.baseURI).pathname.split("/");
              return String(parts.at(-1) || "").toLowerCase();
            } catch {
              const path = String(source).split(/[?#]/, 1)[0];
              return String(path.split("/").at(-1) || "").toLowerCase();
            }
          };
          const profileCandidateVisible = (element) => {
            if (!element) return false;
            const rect = element.getBoundingClientRect();
            return visible(element, getComputedStyle(element), rect);
          };
          // 站3同容器可能出现钱包图标、装饰图或错误文案；先按真实资源与
          // 精确文字过滤，再进入 20 个优先槽位，避免诱饵把真实入口挤出。
          const siteThreeSigninCandidates = [
            ...document.querySelectorAll(".sign-in > img.d-img"),
          ].filter((element) => Boolean(
            profileCandidateVisible(element)
            && profileResourceBasename(element).startsWith("singi-")
          ));
          const siteThreeVipButtonCandidates = [
            ...document.querySelectorAll(
              ".vip-center.mine-vip-banner > .mine-vip-banner__btn",
            ),
          ].filter(
            (element) => Boolean(
              profileCandidateVisible(element)
              && String(element.innerText || "").replace(/\\s+/g, "") === "立即开通"
            ),
          );
          const siteThreeVipBannerCandidates = siteThreeVipButtonCandidates
            .map((element) => element.parentElement)
            .filter((element) => profileCandidateVisible(element));
          const siteThreeMemberCenterCandidates = [
            ...document.querySelectorAll(".my-balance-wrap img"),
          ].filter(
            (element) => Boolean(
              profileCandidateVisible(element)
              && profileResourceBasename(element).startsWith("mine-vip-icon-")
            ),
          );
          // 普通 DIV 支付按钮没有 onclick/role 也要留证；是否可点击仍独立验签。
          const h5VipTextCandidates = __H5_MODAL_CENSUS__
            ? [...document.querySelectorAll(".header .vip-section > .button")].filter(
              (element) => element.tagName === "DIV" && profileCandidateVisible(element)
                && String(element.innerText || "").replace(/\\s+/g, "") === "立即开通",
            ) : [];
          const priorityProfileNavigationCandidates = [...new Set([
            ...h5VipTextCandidates,
            ...siteThreeSigninCandidates,
            ...siteThreeVipBannerCandidates,
            ...siteThreeVipButtonCandidates,
            ...siteThreeMemberCenterCandidates,
            ...document.querySelectorAll([
              ".top-right-menu img.icon.icon-setting",
              ".img-menus-wrap > .left > img.img[src*='my-vip-center']",
              ".img-menus-wrap .sigin-box > img.img[src*='m-signin-bg']",
              "img.sign-in-entry[alt='签到']",
              ".vip-promo img.vip-promo__button[alt='立即开通']",
              ".quick-card-row img.quick-card[alt='会员中心']",
              ".mine-header__actions button.icon-button img.icon-button__img[alt='设置']",
            ].join(",")),
          ])].slice(0, 20);
          // 长列表、轮播复用节点可能先耗尽 300 元素上限；只提前已登记的
          // 详情目标结构，并优先当前视口内节点，避免离屏克隆挤掉真实卡片。
          const listDetailViewportRank = (element) => {
            const rect = element.getBoundingClientRect();
            const intersectsHorizontally = rect.right > 0 && rect.left < window.innerWidth;
            const intersectsViewport = (
              intersectsHorizontally
              && rect.bottom > 0
              && rect.top < window.innerHeight
            );
            return [
              intersectsViewport ? 0 : (intersectsHorizontally ? 1 : 2),
              Math.max(rect.top, 0),
              Math.abs(rect.left),
            ];
          };
          const compareListDetailViewportRank = (left, right) => {
            const leftRank = listDetailViewportRank(left);
            const rightRank = listDetailViewportRank(right);
            for (let index = 0; index < leftRank.length; index += 1) {
              if (leftRank[index] !== rightRank[index]) {
                return leftRank[index] - rightRank[index];
              }
            }
            return 0;
          };
          const webStructuralAttributeText = (element) => {
            if (!element) return "";
            return [
              element.id,
              element.getAttribute("role"),
              ...element.classList,
            ].filter(Boolean).join(" ").toLowerCase();
          };
          const webAdvertisementToken = (value) => {
            const normalized = String(value || "").trim().toLowerCase();
            if (!normalized) return false;
            const negative = new RegExp(
              "(^|[-_])(?:no|not|non|without)[-_]"
              + "(?:ad|ads|advert(?:isement|ising)?|banner|sponsor(?:ed)?)(?=$|[-_])",
            );
            if (negative.test(normalized)) return false;
            return new RegExp(
              "(^|[-_])(?:ad|ads|aditem|adslot|adlink|adfloat|advert|"
              + "advertisement|advertising|banner|sponsor|sponsored|promotion|promoted)"
              + "(?=$|[-_])",
            ).test(
              normalized
            );
          };
          const hasWebAdvertisementSemantics = (element, ignorePlayerAdConfig = false) => {
            if (!element) return false;
            if ([element.id, ...element.classList].some(webAdvertisementToken)) return true;
            if (String(element.getAttribute("rel") || "").toLowerCase().split(/\\s+/)
              .includes("sponsored")) return true;
            return [...element.attributes].some((attribute) => {
              const name = attribute.name.toLowerCase();
              if (ignorePlayerAdConfig && name === "data-ads_url") return false;
              if (/^data-(?:ad|ads|advert|sponsor)(?:$|[-_])/.test(name)) return true;
              return name.startsWith("data-") && webAdvertisementToken(attribute.value);
            });
          };
          const withinScopedWebNavigation = (element) => {
            let current = element;
            for (let depth = 0; current && depth < 12; depth += 1) {
              const tag = current.tagName.toLowerCase();
              const role = String(current.getAttribute("role") || "").toLowerCase();
              if (tag === "nav" || role === "navigation") return true;
              const attributes = webStructuralAttributeText(current);
              const hasLocalListHierarchy = (
                ["ul", "ol", "li"].includes(tag)
                || Boolean(current.querySelector(":scope > ul > li, :scope > ol > li"))
              );
              if (
                current !== document.body
                && current !== document.documentElement
                && attributes.includes("nav")
                && hasLocalListHierarchy
              ) return true;
              current = current.parentElement;
            }
            return false;
          };
          const withinWebAdvertisement = (element) => {
            let current = element;
            for (let depth = 0; current && depth < 12; depth += 1) {
              if (hasWebAdvertisementSemantics(current)) return true;
              current = current.parentElement;
            }
            return false;
          };
          const priorityWebDetailCandidates = __WEB_DETAIL_CANDIDATE_PRIORITY__
            ? [...document.querySelectorAll("a[href]")].filter((element) => {
              let resolved;
              try { resolved = new URL(element.href, document.baseURI); }
              catch { return false; }
              let evidenceUrl;
              try {
                const currentUrl = new URL(window.location.href);
                evidenceUrl = ["http:", "https:"].includes(currentUrl.protocol)
                  ? currentUrl
                  : new URL(document.baseURI);
              } catch { return false; }
              const sameOrigin = resolved.origin === evidenceUrl.origin;
              const sameHostHttpsUpgrade = (
                evidenceUrl.protocol === "http:"
                && ["", "80"].includes(evidenceUrl.port)
                && resolved.protocol === "https:"
                && ["", "443"].includes(resolved.port)
                && resolved.hostname.toLowerCase() === evidenceUrl.hostname.toLowerCase()
              );
              if (!sameOrigin && !sameHostHttpsUpgrade) return false;
              if (withinScopedWebNavigation(element)) return false;
              const structural = webStructuralAttributeText(element.closest(
                "[class*='pagination'],[class*='pager'],[class*='breadcrumb'],"
                + "[class*='advert'],[class*='banner'],[class*='modal'],[class*='dialog']",
              ));
              return !/pagination|pager|breadcrumb|advert|banner|modal|dialog/.test(
                structural,
              );
            }).sort(compareListDetailViewportRank).slice(0, 80)
            : [];
          // 广告容器本身通常没有 href；真正的落地链接位于其后代锚点。
          // 密集页面若只按普通 DOM 顺序采集，会在有界快照中只留下容器，
          // 导致后续独立广告页调度无法取得目标 URL。
          const priorityWebAdCandidates = __WEB_DETAIL_CANDIDATE_PRIORITY__
            ? [...document.querySelectorAll("a[href]")].filter(
              (element) => withinWebAdvertisement(element),
            ).sort(compareListDetailViewportRank).slice(0, 40)
            : [];
          // 仅 Web 提前采集组合主体根；默认与 H5 候选顺序保持不变。
          const priorityWebSubjectCandidates = __WEB_DETAIL_CANDIDATE_PRIORITY__
            ? [...document.querySelectorAll([
              "main", "iframe",
              webArticleBodySelector,
              "article p", ".article-body p", ".post-content p", ".entry-content p",
              "form",
              ".image-gallery", ".photo-gallery", ".image-viewer", ".photo-viewer",
              ".gallery", ".lightbox",
              ".activity", ".campaign", ".activity-landing", ".campaign-landing",
              ".search-results", ".query-results",
            ].join(","))].sort(compareListDetailViewportRank).slice(0, 60)
            : [];
          // 推荐边界必须和正文一起保留，否则推荐卡片会变成详情页的主列表冲突。
          const priorityWebRecommendationComponents = __WEB_DETAIL_CANDIDATE_PRIORITY__
            ? [...document.querySelectorAll("section,aside,div,ul,ol")].filter((element) =>
              [...element.classList].some((token) =>
                /(?:^|[-_])(?:recommend|recommendations|recommended|related)(?:[-_]|$)/i
                  .test(token)))
              .filter((element) => !element.matches(webArticleBodySelector))
              .slice(0, 8)
            : [];
          const priorityWebRecommendationHeadings = __WEB_DETAIL_CANDIDATE_PRIORITY__
            ? [...document.querySelectorAll("h2,h3")].filter((element) => [
              "相关推荐", "猜你喜欢", "related content", "recommended",
              "you may also like",
            ].includes((element.innerText || "").trim().toLowerCase()))
              .filter((element) => element.parentElement?.matches("section,aside,div")
                && !element.parentElement.matches(webArticleBodySelector))
              .slice(0, 8).flatMap((element) => [element.parentElement, element]).filter(Boolean)
            : [];
          const priorityWebRecommendationBoundaries = [
            ...priorityWebRecommendationComponents, ...priorityWebRecommendationHeadings,
          ];
          const priorityListDetailCandidates = [
            ...document.querySelectorAll([
              ".video-section .layout3 .img-box",
              __CONTENT_LAYOUT3_TITLE_SELECTOR__,
              ".masonry-grid .data-item",
              ".masonry-grid .movie-item",
              __MASONRY_VIDEO_TITLE_SELECTOR__,
              ".video-section .layout5 .img-box",
              ".comic-item .name",
              __MANGA_VIDEO_TITLE_SELECTOR__,
              __LONG_VIDEO_CARD_TITLE_SELECTOR__,
              __HOME_VIDEO_GRID_TITLE_SELECTOR__,
              __HOME_VIDEO_TITLE_SELECTOR__,
              ".ai-page-card .ai-page-card-title",
              ".dynamicData-box .title",
              ".modVideoBox .modCover_name",
              ".post-box.post .post-text .text-overflow",
              ".post-main-item .content-images-wrap .title",
              "li.big-list .decial",
              ".van-list .item_container .poi_title",
              __COMMUNITY_HOME_POST_TITLE_SELECTOR__,
            ].join(",")),
          ].sort(compareListDetailViewportRank).slice(0, 40);
          const probePreviewRoots = __H5_MODAL_CENSUS__
            ? [...document.querySelectorAll(
              '.van-image-preview,.el-image-viewer__wrapper,.viewer-container')] : [];
          const previewLedger=__H5_MODAL_CENSUS__
            ? (window.__aliothProbePreviewIdsV1 ||= {ids:new WeakMap(),next:1})
            : {ids:new WeakMap(),next:1};
          const previewId=node=>{
            if(!previewLedger.ids.has(node))previewLedger.ids.set(node,
              `${performance.timeOrigin}:preview:${previewLedger.next++}`);
            return previewLedger.ids.get(node);
          };
          const previewStates = () => probePreviewRoots.slice(0,16).map(root=>({
            root:previewId(root),
            visible:[...root.querySelectorAll('img')].some(image=>
              visible(image,getComputedStyle(image),image.getBoundingClientRect())),
          }));
          __H5_GALLERY_BINDING_CENSUS__
          const imageResources = async image => {
            const current=image.currentSrc || image.src;
            const result={values:[],complete:true};
            let family=image.getAttribute('srcset') || '';
            // picture 的不同 media 分支可能是不同内容，只采实际选中资源所在的声明。
            if(image.parentElement?.tagName==='PICTURE') {
              const sources=[...image.parentElement.querySelectorAll('source')];
              if(sources.length>8) return {values:[],complete:false};
              for(const source of sources) {
                if(source.media && !matchMedia(source.media).matches) continue;
                const raw=source.getAttribute('srcset') || '';
                if(raw.length>2048) return {values:[],complete:false};
                if(raw.split(',').some(part=>{
                  try{return new URL(part.trim().split(/\\s+/)[0],document.baseURI).href===current;}
                  catch{return false;}
                })) {family=raw;break;}
              }
            }
            if(family.length>2048) return {values:[],complete:false};
            const parts=family ? family.split(',') : [];
            if(parts.length>8) return {values:[],complete:false};
            const urls=[];
            let descriptorKind=null;
            const descriptors=new Set();
            for(const part of parts) {
              const match=part.trim().match(/^(\\S+)\\s+(\\d+(?:\\.\\d+)?)(w|x)$/);
              if(!match || Number(match[2])<=0
                || (match[3]==='w' && !Number.isInteger(Number(match[2])))
                || (descriptorKind && descriptorKind!==match[3])
                || descriptors.has(Number(match[2]))) return {values:[],complete:false};
              descriptorKind=match[3];descriptors.add(Number(match[2]));
              try {
                const url=new URL(match[1],document.baseURI);
                if(!['http:','https:'].includes(url.protocol) || url.href.length>URL_LIMIT)
                  return {values:[],complete:false};
                urls.push(url.href);
              } catch {return {values:[],complete:false};}
            }
            if(parts.length && !urls.includes(current)) return {values:[],complete:false};
            if(current?.length>URL_LIMIT) return {values:[],complete:false};
            for(const url of [...new Set([current,...urls].filter(Boolean))])
              result.values.push((await summarizeResource(url)).value);
            result.values.sort();
            return result;
          };
          const candidates = [
            ...new Set([
              ...priorityModalDismissalCandidates,
              ...priorityModalCandidates,
              ...h5ModalLayers.slice(0, 40),
              ...h5ModalControls,
              ...probePreviewRoots.slice(0,16).flatMap(root=>[...root.querySelectorAll('img')]),
              ...priorityObstructionCandidates,
              ...priorityWebAdCandidates,
              // Web 详情主体必须先于可能含数百链接的导航容器进入有界快照；
              // 弹窗仍保留最高优先级，避免主体优先级削弱清障能力。
              ...priorityWebSubjectCandidates,
              ...priorityWebRecommendationBoundaries,
              ...priorityNavigationCandidates,
              ...priorityWebDetailCandidates,
              ...priorityProfileNavigationCandidates,
              ...priorityListDetailCandidates,
              ...(__H5_MODAL_CENSUS__ ? document.querySelectorAll(
                ".member .card-box .swiper-wrapper > .swiper-slide,"
                + ".van-collapse-item__title,[aria-label='返回顶部'],.back-to-top,.back-top",
              ) : []),
              ...regularCandidates,
            ]),
          ];
          // 先读取有界源图声明，预览图片即使排在源图前也能核对真实实例与索引。
          for(const candidate of candidates.slice(0,ELEMENT_LIMIT))sourceGalleryBinding(candidate);
          const boundedVisualCoverCandidates = [...document.querySelectorAll([
            "[role='dialog']",
            "[aria-modal='true']",
            "[class*='overlay']",
            "[class*='modal']",
            "[class*='popup']",
            ".loading-page",
            ".recommend-layer",
          ].join(","))].slice(0, 80);
          const isInactiveLaunchShell = (element) => Boolean(
            element
            && element.matches(".launch-page")
            && !trimText(element.innerText)
            && element.children.length > 0
            && [...element.children].every((child) => {
              const childStyle = getComputedStyle(child);
              const childRect = child.getBoundingClientRect();
              return !visible(child, childStyle, childRect);
            })
          );
          const stackingFactsFor = (element) => {
            let effectiveZIndex = 0;
            let fixedOrStickyAncestor = false;
            const positionedAncestorZIndices = [];
            let current = element;
            for (let depth = 0; current && depth < 32; depth += 1) {
              const currentStyle = getComputedStyle(current);
              const position = String(currentStyle.position || "static").toLowerCase();
              if (["fixed", "sticky"].includes(position)) {
                fixedOrStickyAncestor = true;
              }
              const parsedZIndex = Number.parseInt(currentStyle.zIndex, 10);
              if (position !== "static" && Number.isFinite(parsedZIndex)) {
                effectiveZIndex = Math.max(effectiveZIndex, parsedZIndex);
                positionedAncestorZIndices.push({
                  depth,
                  position,
                  z_index: parsedZIndex,
                });
              }
              current = current.parentElement;
            }
            return {
              effective_z_index: effectiveZIndex,
              fixed_or_sticky_ancestor: fixedOrStickyAncestor,
              positioned_ancestor_z_indices: positionedAncestorZIndices.slice(0, 12),
            };
          };
          const structuralAttributeText = (element) => {
            if (!element) return "";
            return [...element.attributes]
              .filter((attribute) => (
                ["class", "id", "role"].includes(attribute.name.toLowerCase())
                || attribute.name.toLowerCase().startsWith("aria-")
                || attribute.name.toLowerCase().startsWith("data-")
              ))
              .map((attribute) => `${attribute.name} ${attribute.value}`.toLowerCase())
              .join(" ");
          };
          const structuralContextFactsFor = (element) => {
            let withinNavigation = false;
            let withinWebNavigation = false;
            let withinPagination = false;
            let withinBreadcrumb = false;
            let withinFooter = false;
            let isAdvertisement = false;
            let current = element;
            for (let depth = 0; current && depth < 12; depth += 1) {
              const tag = current.tagName.toLowerCase();
              const role = String(current.getAttribute("role") || "").toLowerCase();
              const attributes = structuralAttributeText(current);
              const isSemanticNavigation = tag === "nav" || role === "navigation";
              const hasNavLikeAttribute = attributes.includes("nav");
              const hasLocalListHierarchy = (
                ["ul", "ol", "li"].includes(tag)
                || Boolean(current.querySelector(":scope > ul > li, :scope > ol > li"))
              );
              withinNavigation = withinNavigation || (
                isSemanticNavigation
                || hasNavLikeAttribute
              );
              // Web 菜单允许 class/id 模糊命中 nav，但必须由局部 ul/li 层级佐证；
              // body.use-navbar 这类全局样式类不能污染正文卡片。
              withinWebNavigation = withinWebNavigation || (
                isSemanticNavigation
                || (
                  current !== document.body
                  && current !== document.documentElement
                  && hasNavLikeAttribute
                  && hasLocalListHierarchy
                )
              );
              withinPagination = withinPagination || /pagination|pager|page[-_ ]?nav/.test(
                attributes
              );
              withinBreadcrumb = withinBreadcrumb || /breadcrumb|bread[-_ ]?crumb/.test(
                attributes
              );
              withinFooter = withinFooter || (
                tag === "footer"
                || role === "contentinfo"
                || /(^|[^a-z0-9])footer([^a-z0-9]|$)/.test(attributes)
              );
              isAdvertisement = isAdvertisement || hasWebAdvertisementSemantics(current);
              current = current.parentElement;
            }
            return {
              within_navigation: withinNavigation,
              within_web_navigation: withinWebNavigation,
              within_pagination: withinPagination,
              within_breadcrumb: withinBreadcrumb,
              within_footer: withinFooter,
              is_advertisement: isAdvertisement,
            };
          };
          const closeTokenPattern = /(^|[-_])(?:close|dismiss|cancel)(?:$|[-_])/;
          const compoundCloseTokens = new Set([
            "closebtn",
            "closebutton",
            "closeicon",
            "closeimg",
            "closeimage",
            "btnclose",
          ]);
          const hasOwnCloseToken = (element) => [element.id, ...element.classList]
            .some((token) => {
              const normalized = String(token || "").toLowerCase();
              return closeTokenPattern.test(normalized)
                || compoundCloseTokens.has(normalized);
            });
          const closeHitTargetFor = (element, knownCenterTarget = null) => {
            const rect = element.getBoundingClientRect();
            const centerTarget = knownCenterTarget || (
              rect.width > 0 && rect.height > 0
                ? document.elementFromPoint(
                  Math.min(
                    Math.max(rect.left + rect.width / 2, 0),
                    Math.max(window.innerWidth - 1, 0),
                  ),
                  Math.min(
                    Math.max(rect.top + rect.height / 2, 0),
                    Math.max(window.innerHeight - 1, 0),
                  ),
                )
                : null
            );
            if (
              !centerTarget
              || (centerTarget !== element && !element.contains(centerTarget))
            ) return null;
            const nestedLink = centerTarget.closest("a[href],[role='link']");
            if (nestedLink && nestedLink !== element) return null;
            const nestedControl = centerTarget.closest(
              "button,input,select,textarea,summary,[role='button'],[onclick],[tabindex]",
            );
            return !nestedControl || nestedControl === element ? centerTarget : null;
          };
          const closeHitSafeFor = (element, knownCenterTarget = null) => Boolean(
            closeHitTargetFor(element, knownCenterTarget)
          );
          const navigationPayloadSelector = [
            "a[href]",
            "[role='link']",
            "[href]",
            "[data-href]",
            "[data-url]",
            "[data-uri]",
            "[data-route]",
            "[to]",
          ].join(",");
          const navigationPayloadInSubtree = (element) => {
            const candidates = [element, ...element.querySelectorAll(navigationPayloadSelector)];
            return candidates.some((candidate) => {
              // SVG use/image 的 href 是图像资源；SVG a 仍是真实导航，不能排除。
              if (__H5_MODAL_CENSUS__ && candidate.namespaceURI === "http://www.w3.org/2000/svg"
                && ["use", "image"].includes(candidate.localName)) return false;
              return candidate.matches(navigationPayloadSelector);
            });
          };
          const webObstructionLayerFactCache = new WeakMap();
          const webObstructionLayerFactsFor = (layer) => {
            if (!layer) {
              return {
                human_verification_present: false,
                close_control_unique: false,
              };
            }
            const cached = webObstructionLayerFactCache.get(layer);
            if (cached) return cached;
            const humanVerificationText = trimText(
              layer.innerText || "",
            ).toLowerCase();
            const humanVerificationSelector = [
              "iframe[src*='captcha' i]",
              "iframe[src*='recaptcha' i]",
              "iframe[src*='hcaptcha' i]",
              "iframe[src*='turnstile' i]",
              "iframe[title*='captcha' i]",
              "iframe[title*='human verification' i]",
              "[data-sitekey]",
              "[class*='captcha' i]",
              "[id*='captcha' i]",
              "[class*='turnstile' i]",
              "[id*='turnstile' i]",
              "[class*='human-verification' i]",
              "[id*='human-verification' i]",
              "[class*='slider-verify' i]",
              "[class*='slide-verify' i]",
            ].join(",");
            const humanVerificationTextPattern = new RegExp(
              "验证码|人机验证|滑块验证|captcha|hcaptcha|recaptcha|turnstile|"
              + "human verification|not a robot|notarobot",
            );
            const humanVerificationPresent = Boolean(
              humanVerificationTextPattern.test(humanVerificationText)
              || [...layer.querySelectorAll(humanVerificationSelector)].some((item) => {
                const itemStyle = getComputedStyle(item);
                const itemRect = item.getBoundingClientRect();
                return visible(item, itemStyle, itemRect);
              })
            );
            const closeControlSelector = [
              ".on-popup-close",
              ".on-dialog-close",
              "[class*='close' i]",
              "[class*='dismiss' i]",
              "[aria-label*='close' i]",
              "[aria-label*='关闭']",
              "img[alt*='关闭']",
              "img[alt*='close' i]",
            ].join(",");
            const closeControlTargets = new Set(
              [...layer.querySelectorAll(closeControlSelector)]
                .map((item) => {
                  const itemStyle = getComputedStyle(item);
                  const itemRect = item.getBoundingClientRect();
                  const itemTag = item.tagName.toLowerCase();
                  const itemRole = String(item.getAttribute("role") || "").toLowerCase();
                  const delegatedCloseControl = item.matches(
                    ".on-popup-close,.on-dialog-close,[class~='on-popup-close'],"
                    + "[class~='on-dialog-close']",
                  );
                  const actionable = ["button", "input", "summary"].includes(itemTag)
                    || itemRole === "button"
                    || item.hasAttribute("onclick")
                    || item.tabIndex >= 0
                    || itemStyle.cursor === "pointer"
                    || delegatedCloseControl
                    // 部分站点把动态监听器绑定到 popup-close 包装层，DOM
                    // 不暴露 onclick/cursor；精确自有关闭 token 仍需纳入同层
                    // 唯一性计算，最终动作继续受结构、命中与非链接复验约束。
                    || hasOwnCloseToken(item);
                  if (!(hasOwnCloseToken(item)
                    && visible(item, itemStyle, itemRect)
                    && actionable
                    && itemTag !== "a"
                    && itemRole !== "link"
                    && !item.hasAttribute("href")
                  )) return null;
                  // 父容器与子图片若命中同一真实节点，只代表一个关闭控件。
                  return closeHitTargetFor(item);
                })
                .filter(Boolean),
            );
            const facts = {
              human_verification_present: humanVerificationPresent,
              close_control_unique: closeControlTargets.size === 1,
            };
            webObstructionLayerFactCache.set(layer, facts);
            return facts;
          };
          const isInlinePrimaryVideoMount = (layer, layerRect) => {
            const subject = layer.closest("main,article,[role='main']");
            if (!subject
              || layer.matches(webObstructionLayerSelector)
              || withinWebAdvertisement(layer)
              || layer.querySelector("[role='dialog'],[aria-modal='true'],"
                + "[class*='close' i],[aria-label*='close' i],"
                + "[aria-label*='关闭']")) return false;
            const videos = layer.querySelectorAll("video");
            if (videos.length !== 1) return false;
            const video = videos[0];
            if (!(video.currentSrc || video.src || video.querySelector("source[src]"))) {
              return false;
            }
            const mediaRect = video.getBoundingClientRect();
            const overlapWidth = Math.max(0, Math.min(layerRect.right, mediaRect.right)
              - Math.max(layerRect.left, mediaRect.left));
            const overlapHeight = Math.max(0, Math.min(layerRect.bottom, mediaRect.bottom)
              - Math.max(layerRect.top, mediaRect.top));
            if (overlapWidth * overlapHeight
              < layerRect.width * layerRect.height * 0.75) return false;
            // 真正的内嵌播放器须由主体内独立布局槽位承载，不能只是 main 上的浮层。
            let slot = layer.parentElement;
            while (slot && slot !== subject
              && ["absolute", "fixed"].includes(getComputedStyle(slot).position)) {
              slot = slot.parentElement;
            }
            if (!slot || slot === subject) return false;
            const slotRect = slot.getBoundingClientRect();
            const slotOverlapWidth = Math.max(0,
              Math.min(layerRect.right, slotRect.right)
              - Math.max(layerRect.left, slotRect.left));
            const slotOverlapHeight = Math.max(0,
              Math.min(layerRect.bottom, slotRect.bottom)
              - Math.max(layerRect.top, slotRect.top));
            const slotOverlap = slotOverlapWidth * slotOverlapHeight;
            if (slotRect.width <= 0 || slotRect.height <= 0
              || slotOverlap < layerRect.width * layerRect.height * 0.9
              || slotOverlap < slotRect.width * slotRect.height * 0.75) return false;
            for (const other of subject.querySelectorAll("video")) {
              if (other === video || layer.contains(other)) continue;
              const style = getComputedStyle(other);
              if (style.display === "none" || style.visibility === "hidden"
                || Number(style.opacity) === 0) continue;
              const rect = other.getBoundingClientRect();
              const coveredWidth = Math.max(0,
                Math.min(layerRect.right, rect.right)
                - Math.max(layerRect.left, rect.left));
              const coveredHeight = Math.max(0,
                Math.min(layerRect.bottom, rect.bottom)
                - Math.max(layerRect.top, rect.top));
              if (rect.width > 0 && rect.height > 0
                && coveredWidth * coveredHeight
                  >= Math.min(layerRect.width * layerRect.height,
                    rect.width * rect.height) * 0.5) return false;
            }
            for (const prose of subject.querySelectorAll(`p,${webArticleBodySelector}`)) {
              if (prose === layer || prose.contains(layer) || layer.contains(prose)) continue;
              const rect = prose.getBoundingClientRect();
              const left = Math.max(0, layerRect.left, rect.left);
              const top = Math.max(0, layerRect.top, rect.top);
              const right = Math.min(innerWidth, layerRect.right, rect.right);
              const bottom = Math.min(innerHeight, layerRect.bottom, rect.bottom);
              if (right <= left || bottom <= top
                || (right - left) * (bottom - top) < rect.width * rect.height * 0.2
                || (prose.innerText || "").trim().length < 120) continue;
              const hit = document.elementFromPoint((left + right) / 2, (top + bottom) / 2);
              // 视频层若命中独立正文的位置，就不能把它当作正文自身的布局。
              if (hit === layer || layer.contains(hit)) return false;
            }
            return true;
          };
          const webObstructionFactsFor = (element, centerTarget) => {
            if (!__WEB_DETAIL_CANDIDATE_PRIORITY__) return null;
            const semanticLayer = element.closest(webObstructionLayerSelector);
            const geometricLayer = (() => {
              let current = element.parentElement;
              const viewportArea = Math.max(window.innerWidth * window.innerHeight, 1);
              for (let depth = 0; current && depth < 16; depth += 1) {
                // 文档根节点承载整页布局，即使站点把 body 设为 absolute，
                // 也不能把全部正文误标成几何阻断层。
                if (current === document.body || current === document.documentElement) {
                  current = current.parentElement;
                  continue;
                }
                const style = getComputedStyle(current);
                const position = String(style.position || "static").toLowerCase();
                const rect = current.getBoundingClientRect();
                const left = Math.max(rect.left, 0);
                const top = Math.max(rect.top, 0);
                const right = Math.min(rect.right, window.innerWidth);
                const bottom = Math.min(rect.bottom, window.innerHeight);
                const coverage = (
                  Math.max(right - left, 0) * Math.max(bottom - top, 0)
                ) / viewportArea;
                const coversViewportCenter = (
                  rect.left <= window.innerWidth / 2
                  && rect.right >= window.innerWidth / 2
                  && rect.top <= window.innerHeight / 2
                  && rect.bottom >= window.innerHeight / 2
                );
                const scrollLocked = [document.body, document.documentElement].some(
                  (root) => ["hidden", "clip"].includes(getComputedStyle(root).overflow),
                );
                if (
                  ["fixed", "absolute"].includes(position)
                  && coverage >= 0.08
                  && (coversViewportCenter || scrollLocked)
                ) {
                  // 主体内绝对定位播放器是视频自身的布局，不是盖在页面之上的浮层。
                  if (position !== "absolute"
                    || !isInlinePrimaryVideoMount(current, rect)) return current;
                }
                current = current.parentElement;
              }
              return null;
            })();
            const structuralLayer = semanticLayer || geometricLayer;
            let layer = structuralLayer;
            const layerFacts = layer ? [
              layer.tagName,
              layer.id,
              layer.getAttribute("role"),
              layer.getAttribute("aria-modal"),
              ...layer.classList,
            ].join(" ").toLowerCase() : "";
            const isAgeGateLayer = Boolean(
              layer
              && /(?:age|adult)[-_ ]?(?:gate|verify|modal|lock(?:ed)?)/.test(
                layerFacts,
              )
            );
            let layerKind = null;
            if (layer) {
              if (isAgeGateLayer) layerKind = "age_gate";
              else if (
                layer.matches("[role='dialog'],[aria-modal='true']")
                || /(^|[^a-z])dialog([^a-z]|$)/.test(layerFacts)
              ) layerKind = "dialog";
              else if (/popup/.test(layerFacts)) layerKind = "popup";
              else if (/modal/.test(layerFacts)) layerKind = "modal";
              else if (/overlay/.test(layerFacts)) layerKind = "overlay";
              else if (geometricLayer) layerKind = "geometric_overlay";
              else layerKind = "positioned_layer";
            }
            const explicitCloseFacts = [
              element.innerText,
              element.getAttribute("aria-label"),
              element.getAttribute("title"),
              element.getAttribute("alt"),
            ].join(" ").toLowerCase();
            const delegatedClose = element.closest(
              ".on-popup-close,.on-dialog-close,[class~='on-popup-close'],[class~='on-dialog-close']",
            );
            const explicitCloseText = /关闭|取消|稍后|跳过|我知道了|close|dismiss|cancel|×/.test(
              explicitCloseFacts,
            );
            const ageGateConfirmation = Boolean(
              isAgeGateLayer
              && /18|成年|adult/.test(explicitCloseFacts)
              && /进入|继续|确认|enter|continue|confirm|accept/.test(
                explicitCloseFacts,
              )
            );
            const delegatedWrapperSelf = delegatedClose === element;
            const ownCloseToken = hasOwnCloseToken(element);
            const closeBasis = explicitCloseText
              ? "explicit_close_text"
              : (ageGateConfirmation
                ? "age_gate_confirmation"
                : (delegatedWrapperSelf
                  ? "delegated_wrapper_self"
                  : (ownCloseToken
                    ? "own_close_token"
                    : (delegatedClose ? "delegated_wrapper_descendant" : null))));
            const layerWideFacts = webObstructionLayerFactsFor(layer);
            const closeSemantics = Boolean(
              closeBasis && closeBasis !== "delegated_wrapper_descendant"
            );
            return {
              layer_selector: layer ? selectorFor(layer) : null,
              layer_rect: layer ? boundsFor(layer.getBoundingClientRect()) : null,
              layer_kind: layerKind,
              layer_structural: Boolean(structuralLayer),
              close_semantics: closeSemantics,
              close_basis: closeBasis,
              close_control_unique: layerWideFacts.close_control_unique,
              close_hit_safe: closeHitSafeFor(element, centerTarget),
              human_verification_present: layerWideFacts.human_verification_present,
            };
          };
          const webOverlayGeometryFor = () => {
            // 只读局部浮层普查独立于可执行关闭候选；超限不得声称完整。
            const result = {
              schema_version: "web-overlay-geometry.v1", complete: true, layers: [], subjects: [],
            };
            const nodes = document.querySelectorAll("*");
            if (nodes.length > 4000) result.complete = false;
            for (const element of [...nodes].slice(0, 4000)) {
              if (element === document.body || element === document.documentElement) continue;
              const style = getComputedStyle(element);
              const rect = element.getBoundingClientRect();
              if (!visible(element, style, rect) || rect.right <= 0 || rect.bottom <= 0
                || rect.left >= window.innerWidth || rect.top >= window.innerHeight) continue;
              const attributes = structuralAttributeText(element);
              const special = element.matches("[role='dialog'],[aria-modal='true']")
                || element.matches("iframe[title*='captcha' i],iframe[src*='captcha' i],"
                  + "iframe[title*='human verification' i],[data-sitekey]")
                || /(?:age|adult)[-_ ]?gate|captcha|turnstile|human[-_ ]?verification/.test(
                  attributes,
                );
              const context = structuralContextFactsFor(element);
              const subject = element.matches("main,article,[role='main']")
                && !context.within_web_navigation && !context.within_footer
                && !context.is_advertisement && !element.closest(webObstructionLayerSelector);
              if (subject) {
                let positionedAncestor = false;
                for (let current = element; current && current !== document.body;
                  current = current.parentElement) {
                  positionedAncestor = positionedAncestor
                    || ["fixed", "sticky"].includes(getComputedStyle(current).position);
                }
                if (!positionedAncestor) {
                  if (result.subjects.length < 32) result.subjects.push(boundsFor(rect));
                  else result.complete = false;
                }
              }
              const semantic = element.matches(webObstructionLayerSelector);
              if (!special && !semantic && (context.within_web_navigation || context.within_footer
                || element.closest("header,nav,[role='navigation']"))) continue;
              const positioned = ["fixed", "sticky"].includes(style.position)
                || (style.position === "absolute" && element.querySelector(
                  "[class*='close' i],[aria-label*='close' i],[aria-label*='关闭']",
                ));
              if (!special && !semantic && !positioned) continue;
              if (result.layers.length >= 32) {
                result.complete = false;
                break;
              }
              result.layers.push({
                rect: boundsFor(rect),
                ordinary: !special && (!semantic || element.matches("[class~='overlay']"))
                  && !webObstructionLayerFactsFor(element).human_verification_present,
              });
            }
            return result;
          };
          const repeatedSiblingSignature = (element) => {
            const classes = [...element.classList].slice(0, 8).sort().join(".");
            return [
              element.tagName.toLowerCase(),
              String(element.getAttribute("role") || "").toLowerCase(),
              classes,
            ].join("|");
          };
          const repeatGroupSizeFor = (element) => {
            let current = element;
            for (let depth = 0; current && current.parentElement && depth < 8; depth += 1) {
              const parent = current.parentElement;
              const signature = repeatedSiblingSignature(current);
              const siblings = [...parent.children].filter(
                (sibling) => repeatedSiblingSignature(sibling) === signature,
              );
              if (siblings.length >= 2) return siblings.length;
              current = parent;
            }
            return 0;
          };
          for (const element of candidates) {
            if (elements.length >= ELEMENT_LIMIT) break;
            // `<source>` 自身没有渲染盒，但它是视频/音频分类所需的结构事实。
            // 仅当父媒体真实可见时，借用父媒体矩形收集为只读节点。
            const sourceMediaParent = (
              element instanceof HTMLSourceElement
              && (
                element.parentElement instanceof HTMLVideoElement
                || element.parentElement instanceof HTMLAudioElement
              )
            ) ? element.parentElement : null;
            const renderedElement = sourceMediaParent || element;
            const style = getComputedStyle(renderedElement);
            const rect = renderedElement.getBoundingClientRect();
            if (!visible(renderedElement, style, rect)) continue;
            const href = element instanceof HTMLAnchorElement ? element.href : null;
            const src = (
              element instanceof HTMLImageElement
              || element instanceof HTMLVideoElement
              || element instanceof HTMLAudioElement
            ) ? element.currentSrc || element.src || null
              : (
                element instanceof HTMLSourceElement
                || (__WEB_DETAIL_CANDIDATE_PRIORITY__ && element instanceof HTMLIFrameElement)
              ) ? element.src || null : null;
            const hrefResource = await summarizeResource(href);
            const srcResource = await summarizeResource(src);
            const centerX = rect.left + rect.width / 2;
            const centerY = rect.top + rect.height / 2;
            const rectIntersectsViewport = Boolean(
              rect.width > 0
              && rect.height > 0
              && rect.right > 0
              && rect.bottom > 0
              && rect.left < window.innerWidth
              && rect.top < window.innerHeight
            );
            const centerInsideViewport = Boolean(
              rectIntersectsViewport
              && centerX >= 0
              && centerX < window.innerWidth
              && centerY >= 0
              && centerY < window.innerHeight
            );
            const centerTarget = (
              centerInsideViewport
            ) ? document.elementFromPoint(centerX, centerY) : null;
            const isFlutterSemantics = element.tagName.toLowerCase() === "flt-semantics";
            const sameFlutterSemanticsTreeFor = (candidate) => {
              if (!candidate) return false;
              const elementSemantics = element.closest("flt-semantics");
              const targetSemantics = candidate.closest("flt-semantics");
              return Boolean(
                elementSemantics
                && targetSemantics
                && (
                  elementSemantics === targetSemantics
                  || elementSemantics.contains(targetSemantics)
                  || targetSemantics.contains(elementSemantics)
                )
              );
            };
            const centerStack = (
              centerInsideViewport
              && typeof document.elementsFromPoint === "function"
                ? document.elementsFromPoint(centerX, centerY).slice(0, 12)
                : (centerTarget ? [centerTarget] : [])
            );
            const stackHitsTarget = centerStack.some((candidate) => (
              candidate === element
              || element.contains(candidate)
              || sameFlutterSemanticsTreeFor(candidate)
            ));
            const flutterRootFor = (candidate) => {
              if (!(candidate instanceof Element)) return null;
              return candidate.closest("flutter-view")
                || candidate.closest("flt-glass-pane");
            };
            const flutterSurfaceFor = (candidate) => {
              if (!(candidate instanceof Element)) return null;
              if (candidate.matches("canvas,flt-glass-pane,flutter-view")) {
                return candidate;
              }
              if (candidate.matches([
                "flt-scene-host",
                "flt-scene",
                "flt-canvas-container",
                "flt-picture",
              ].join(","))) {
                return candidate.closest("flt-glass-pane,flutter-view");
              }
              return null;
            };
            const semanticsHost = element.closest("flt-semantics-host");
            const semanticsFlutterRoot = flutterRootFor(semanticsHost);
            // paint stack 首项必须自身是受信渲染表面或白名单内部节点；
            // 禁止跳过普通 painted div 再借用其下方同根 canvas。
            const firstFlutterSurface = centerStack.length > 0
              ? flutterSurfaceFor(centerStack[0])
              : null;
            const flutterSurfaceHit = Boolean(
              isFlutterSemantics
              && centerInsideViewport
              && semanticsHost
              && semanticsFlutterRoot
              && firstFlutterSurface
              && flutterRootFor(firstFlutterSurface) === semanticsFlutterRoot
            );
            const centerTargetFlutterSurface = flutterSurfaceFor(centerTarget);
            const centerTargetHitsSameFlutterRoot = Boolean(
              centerTargetFlutterSurface
              && semanticsFlutterRoot
              && flutterRootFor(centerTargetFlutterSurface) === semanticsFlutterRoot
            );
            const visualOcclusionDetected = Boolean(
              isFlutterSemantics
              && (
                (
                  centerTarget
                  && centerTarget !== element
                  && !element.contains(centerTarget)
                  && !sameFlutterSemanticsTreeFor(centerTarget)
                  && !centerTargetHitsSameFlutterRoot
                )
                || boundedVisualCoverCandidates.some((candidate) => {
                if (
                  candidate === element
                  || element.contains(candidate)
                  || candidate.contains(element)
                  || sameFlutterSemanticsTreeFor(candidate)
                ) return false;
                const candidateStyle = getComputedStyle(candidate);
                const candidateRect = candidate.getBoundingClientRect();
                if (!visible(candidate, candidateStyle, candidateRect)) return false;
                if (
                  centerX < candidateRect.left
                  || centerX > candidateRect.right
                  || centerY < candidateRect.top
                  || centerY > candidateRect.bottom
                ) return false;
                const candidateZ = Number.parseInt(candidateStyle.zIndex, 10);
                const elementZ = Number.parseInt(style.zIndex, 10);
                if (
                  Number.isFinite(candidateZ)
                  && Number.isFinite(elementZ)
                  && candidateZ < elementZ
                ) return false;
                const background = candidateStyle.backgroundColor;
                const backgroundAlpha = background.startsWith("rgba")
                  ? Number(background.split(",").at(-1).replace(")", "").trim())
                  : 1;
                const backgroundPainted = background !== "transparent"
                  && background !== "rgba(0, 0, 0, 0)"
                  && backgroundAlpha > 0;
                return Boolean(
                  backgroundPainted
                  || candidateStyle.backgroundImage !== "none"
                  || candidateStyle.boxShadow !== "none"
                  || Number.parseFloat(candidateStyle.borderTopWidth) > 0
                  || Number.parseFloat(candidateStyle.borderRightWidth) > 0
                  || Number.parseFloat(candidateStyle.borderBottomWidth) > 0
                  || Number.parseFloat(candidateStyle.borderLeftWidth) > 0
                );
                })
              )
            );
            const directCenterHit = Boolean(
              centerTarget
              && (
                centerTarget === element
                || element.contains(centerTarget)
                || sameFlutterSemanticsTreeFor(centerTarget)
              )
            );
            const centerHitsTarget = Boolean(
              isFlutterSemantics
                ? !visualOcclusionDetected && (stackHitsTarget || flutterSurfaceHit)
                : directCenterHit
            );
            const bounds = boundsFor(rect);
            const childBounds = [...element.children]
              .map((child) => {
                const childRect = child.getBoundingClientRect();
                const childStyle = getComputedStyle(child);
                return visible(child, childStyle, childRect) ? boundsFor(childRect) : null;
              })
              .filter(Boolean);
            const inactiveLaunchShell = isInactiveLaunchShell(element);
            const parentSemantics = element.parentElement
              ? element.parentElement.closest("flt-semantics")
              : null;
            const stackingFacts = stackingFactsFor(renderedElement);
            const structuralContextFacts = structuralContextFactsFor(element);
            const repeatGroupSize = repeatGroupSizeFor(element);
            const webObstructionFacts = webObstructionFactsFor(element, centerTarget);
            const adaptiveNavigationGroup = adaptiveNavigationGroupFor(element);
            const adConfiguredPlayerRoot = (() => {
              if (!__WEB_DETAIL_CANDIDATE_PRIORITY__
                || !(element instanceof HTMLVideoElement)) return null;
              let parent = element.parentElement;
              for (let depth = 0; parent && depth < 5; depth += 1) {
                if (parent === document.body || parent === document.documentElement) break;
                const identity = `${parent.localName} ${parent.id} `
                  + [...parent.classList].join(" ");
                if (/player|video|media/i.test(identity)
                  && parent.querySelectorAll("video").length === 1
                  && parent.getAttribute("data-video")
                  && parent.hasAttribute("data-ads_url")
                  && (parent.hasAttribute("data-video_id")
                    || parent.hasAttribute("data-video_title"))) return parent;
                parent = parent.parentElement;
              }
              return null;
            })();
            const videoAdvertisementFacts = (() => {
              if (!__WEB_DETAIL_CANDIDATE_PRIORITY__
                || !(element instanceof HTMLVideoElement)) return null;
              let current = element;
              for (let depth = 0; current && depth < 32; depth += 1) {
                if (hasWebAdvertisementSemantics(
                  current, current === adConfiguredPlayerRoot
                )) return { is_advertisement: true, complete: true };
                if (current === document.body || current === document.documentElement) {
                  return { is_advertisement: false, complete: true };
                }
                current = current.parentElement;
              }
              return { is_advertisement: null, complete: false };
            })();
            const videoCenterFacts = (() => {
              if (!__WEB_DETAIL_CANDIDATE_PRIORITY__
                || !(element instanceof HTMLVideoElement)) return null;
              const centerObstructionLayer = Boolean(
                centerTarget && centerTarget.closest(webObstructionLayerSelector)
              );
              const centerPointerInterception = Boolean(
                centerTarget
                && getComputedStyle(centerTarget).pointerEvents !== "none"
              );
              const centerAdClickOverlay = (() => {
                if (!adConfiguredPlayerRoot || !centerTarget
                  || centerObstructionLayer || !centerPointerInterception) return false;
                const link = centerTarget.closest("a[href]");
                if (!link || !adConfiguredPlayerRoot.contains(link)
                  || !hasWebAdvertisementSemantics(link)
                  || link.children.length || (link.textContent || "").trim()) return false;
                const hasVisiblePaint = (paint) => {
                  const color = paint.backgroundColor.replaceAll(" ", "");
                  const content = String(paint.content || "")
                    .replaceAll('"', "").replaceAll("'", "").trim();
                  const borders = ["Top", "Right", "Bottom", "Left"].some(
                    (side) => Number.parseFloat(paint[`border${side}Width`]) !== 0
                  );
                  const effects = [
                    paint.boxShadow, paint.textShadow, paint.borderImageSource,
                    paint.filter, paint.backdropFilter, paint.webkitBackdropFilter,
                    paint.maskImage, paint.webkitMaskImage,
                  ];
                  return !["transparent", "rgba(0,0,0,0)"].includes(color)
                    || paint.backgroundImage !== "none"
                    || borders || effects.some((value) => value && value !== "none")
                    || paint.outlineStyle !== "none"
                    || paint.mixBlendMode !== "normal"
                    || !["", "none", "normal"].includes(content);
                };
                if (hasVisiblePaint(getComputedStyle(link))
                  || ["::before", "::after"].some(
                    (part) => hasVisiblePaint(getComputedStyle(link, part))
                  )) return false;
                const overlayRect = link.getBoundingClientRect();
                const videoRect = element.getBoundingClientRect();
                const overlapWidth = Math.max(0, Math.min(overlayRect.right, videoRect.right)
                  - Math.max(overlayRect.left, videoRect.left));
                const overlapHeight = Math.max(0, Math.min(overlayRect.bottom, videoRect.bottom)
                  - Math.max(overlayRect.top, videoRect.top));
                const videoArea = videoRect.width * videoRect.height;
                return videoArea > 0
                  && overlapWidth * overlapHeight >= videoArea * 0.9
                  && overlayRect.width * overlayRect.height <= videoArea * 1.1;
              })();
              if (!centerTarget || centerObstructionLayer) {
                return {
                  center_obstruction_layer: centerObstructionLayer,
                  center_within_player_surface: false,
                  center_ad_click_overlay: false,
                  center_pointer_interception: centerPointerInterception,
                };
              }
              if (centerAdClickOverlay) {
                return {
                  center_obstruction_layer: false,
                  center_within_player_surface: false,
                  center_ad_click_overlay: true,
                  center_pointer_interception: true,
                };
              }
              if (centerTarget === element || element.contains(centerTarget)) {
                return {
                  center_obstruction_layer: false,
                  center_within_player_surface: true,
                  center_ad_click_overlay: false,
                  center_pointer_interception: centerPointerInterception,
                };
              }
              const centerContext = structuralContextFactsFor(centerTarget);
              if (centerContext.is_advertisement || centerContext.within_web_navigation) {
                return {
                  center_obstruction_layer: false,
                  center_within_player_surface: false,
                  center_ad_click_overlay: false,
                  center_pointer_interception: centerPointerInterception,
                };
              }
              let parent = element.parentElement;
              for (let depth = 0; parent && depth < 5; depth += 1) {
                if (parent === document.body || parent === document.documentElement) break;
                const identity = `${parent.localName} ${parent.id} `
                  + [...parent.classList].join(" ");
                if (parent.contains(centerTarget)
                  && /player|video|media/i.test(identity)
                  && parent.querySelectorAll("video").length === 1) {
                  return {
                    center_obstruction_layer: false,
                    center_within_player_surface: true,
                    center_ad_click_overlay: false,
                    center_pointer_interception: centerPointerInterception,
                  };
                }
                parent = parent.parentElement;
              }
              return {
                center_obstruction_layer: false,
                center_within_player_surface: false,
                center_ad_click_overlay: false,
                center_pointer_interception: centerPointerInterception,
              };
            })();
            const item = {
              selector: selectorFor(element),
              tag: element.tagName.toLowerCase(),
              media_parent_tag: sourceMediaParent
                ? sourceMediaParent.tagName.toLowerCase()
                : null,
              role: element.getAttribute("role"),
              ...(__H5_MODAL_CENSUS__ ? {
                aria_modal: element.getAttribute("aria-modal") === "true",
                dismiss_semantics: element.getAttribute("data-dismiss")
                  || element.getAttribute("data-bs-dismiss") || null,
                h5_modal_sibling_panel_selector: h5ModalSiblingPanels.has(element)
                  ? selectorFor(h5ModalSiblingPanels.get(element)) : null,
                h5_modal_action_semantics: h5ModalControls.includes(element)
                  ? h5ModalLocalSemantics(element) : null,
                h5_modal_safe_outside: (() => {
                  if (!h5ModalLayerSet.has(element)
                    || !["fixed", "absolute"].includes(style.position)
                    || rect.width * rect.height < window.innerWidth * window.innerHeight * .8
                    || element.matches("a,button,input,[href],[data-href],"
                      + "[data-url],[data-route]")) {
                    return null;
                  }
                  const panels = [...h5ModalLayerSet].filter((layer) => layer !== element
                    && layer.matches("dialog,[role='dialog'],[aria-modal='true']")
                    && (element.contains(layer) || layer.parentElement === element.parentElement));
                  if (panels.length !== 1) return null;
                  const panel = panels[0].getBoundingClientRect();
                  const positions = [];
                  for (const yr of [.05,.5,.95]) for (const xr of [.05,.5,.95]) {
                    const x = Math.max(0, rect.left)
                      + (Math.min(innerWidth, rect.right) - Math.max(0, rect.left)) * xr;
                    const y = Math.max(0, rect.top)
                      + (Math.min(innerHeight, rect.bottom) - Math.max(0, rect.top)) * yr;
                    if (x >= panel.left && x <= panel.right
                      && y >= panel.top && y <= panel.bottom) continue;
                    if (document.elementFromPoint(x, y) === element) {
                      positions.push({x:x-rect.left,y:y-rect.top});
                    }
                  }
                  return positions.length ? {
                    panel_selector: selectorFor(panels[0]), positions: positions.slice(0,2),
                  } : null;
                })(),
                form_associated: element instanceof HTMLButtonElement
                  ? Boolean(element.form) : null,
                stateful_control_in_subtree: Boolean(element.querySelector(
                  "form,input,select,textarea,[role='textbox'],[role='checkbox']",
                )),
                h5_modal_ancestor_selectors: (() => {
                  const parents = [];
                  for (let parent = element.parentElement, depth = 0; parent && depth < 32;
                    parent = parent.parentElement, depth += 1) {
                    if (h5ModalLayerSet.has(parent)) parents.push(selectorFor(parent));
                  }
                  return parents;
                })(),
              } : {}),
              text: trimText(
                element instanceof HTMLInputElement
                  ? element.getAttribute("aria-label") || element.placeholder
                  : element.innerText || element.getAttribute("aria-label"),
              ),
              aria_label: element.getAttribute("aria-label"),
              title: element.getAttribute("title"),
              aria_selected: element.getAttribute("aria-selected"),
              aria_checked: element.getAttribute("aria-checked"),
              aria_disabled: element.getAttribute("aria-disabled"),
              aria_expanded: element.getAttribute("aria-expanded"),
              aria_controls: element.getAttribute("aria-controls"),
              ...(__H5_MODAL_CENSUS__ && element instanceof HTMLImageElement
                && element.closest('.van-image-preview,.el-image-viewer__wrapper,.viewer-container')
                ? {h5_probe_preview_root:previewId(element.closest(
                  '.van-image-preview,.el-image-viewer__wrapper,.viewer-container'))} : {}),
              ...(__H5_MODAL_CENSUS__ && element instanceof HTMLImageElement
                ? {h5_probe_preview_controlled_root:(()=>{
                  const id=element.getAttribute('aria-controls');
                  if(!id || /\\s/.test(id))return null;
                  const matches=document.querySelectorAll(`#${CSS.escape(id)}`);
                  return matches.length===1 && probePreviewRoots.includes(matches[0])
                    ? previewId(matches[0]) : null;
                })()} : {}),
              ...(__H5_MODAL_CENSUS__ && element.classList.contains("van-collapse-item__title")
                ? {h5_probe_controlled_region: (() => {
                  const id=element.getAttribute('aria-controls');
                  if(!id) return null;
                  const matches=document.querySelectorAll(`#${CSS.escape(id)}`);
                  if(matches.length!==1) return {unique:false};
                  const target=matches[0],rect=target.getBoundingClientRect();
                  const style=getComputedStyle(target);
                  return {unique:true,id,width:rect.width,height:rect.height,
                    visible:style.display!=='none' && style.visibility!=='hidden'
                      && Number(style.opacity)>0 && rect.width>0 && rect.height>0};
                })()} : {}),
              aria_current: element.getAttribute("aria-current"),
              h5_probe_local_display: !element.closest(
                "a[href],form,[data-href],[data-url],[data-route],"
                + "[data-action='submit'],[type='submit'],[aria-disabled='true'],[inert]",
              ) && !element.querySelector("form,[type='submit'],[data-action='submit']"),
              ...(__H5_MODAL_CENSUS__ && element.matches(
                ".member .card-box .swiper-wrapper > .swiper-slide,"
                + ".vip-plans .plan-option,.membership-plans .plan-option,"
                + ".plan-list .plan-option,.package-list .package-item",
              ) ? {h5_probe_plan_label: (() => {
                const copy=element.cloneNode(true);
                for(const timer of copy.querySelectorAll("[role='timer'],.countdown,.count-down"))
                  timer.remove();
                return String(copy.textContent||'').replace(/\\s+/g,' ').trim().slice(0,500);
              })()} : {}),
              parent_semantics_selector: parentSemantics
                ? selectorFor(parentSemantics)
                : null,
              parent_semantics_aria_label: parentSemantics
                ? parentSemantics.getAttribute("aria-label")
                : null,
              parent_semantics_tappable: Boolean(
                parentSemantics && parentSemantics.hasAttribute("flt-tappable")
              ),
              class_tokens: [...element.classList]
                .slice(0, 12)
                .map((token) => token.slice(0, CLASS_TOKEN_LIMIT)),
              parent_class_tokens: element.parentElement
                ? [...element.parentElement.classList]
                  .slice(0, 12)
                  .map((token) => token.slice(0, CLASS_TOKEN_LIMIT))
                : [],
              parent_selector: element.parentElement
                ? selectorFor(element.parentElement)
                : null,
              dom_sibling_index: element.parentElement
                ? [...element.parentElement.children].filter(
                  (sibling) => sibling.tagName === element.tagName,
                ).indexOf(element) + 1
                : 1,
              h5_adaptive_navigation_group_selector: adaptiveNavigationGroup
                ? selectorFor(adaptiveNavigationGroup)
                : null,
              ancestor_class_tokens: (() => {
                // 结构记号常在包装容器上（.img-box > img），裸元素自身无类名，
                // 补充有限层祖先记号供语义解析端做结构证明。
                const seen = new Set();
                let ancestor = element.parentElement;
                for (let depth = 0; ancestor && depth < 8 && seen.size < 16; depth += 1) {
                  for (const token of ancestor.classList) {
                    if (seen.size >= 16) break;
                    seen.add(token.slice(0, CLASS_TOKEN_LIMIT));
                  }
                  ancestor = ancestor.parentElement;
                }
                return [...seen];
              })(),
              structural_navigation_kind: flutterBottomNavigationCandidates.includes(element)
                ? "flutter_bottom_navigation"
                : null,
              ...(__WEB_DETAIL_CANDIDATE_PRIORITY__ ? {
                web_navigation_dom_path: (() => {
                  // 与同一 Capture 的序列化 DOM 按元素位置对齐，重复链接不能复用身份。
                  const path = [];
                  let current = element;
                  while (current) {
                    let index = 1;
                    let sibling = current.previousElementSibling;
                    while (sibling) { index += 1; sibling = sibling.previousElementSibling; }
                    path.unshift(index);
                    current = current.parentElement;
                  }
                  return path.join("/");
                })(),
                web_navigation_disabled: element.matches(":disabled")
                  || Boolean(element.closest('[aria-disabled="true"], [inert]')),
                web_ancestor_structure: (() => {
                  // 已采节点携带必要父链，普通 div 不进入候选集合；全局字节上限继续生效。
                  const ancestors = [];
                  let parent = element.parentElement;
                  while (parent && ancestors.length < 32) {
                    const tag = parent.tagName.toLowerCase();
                    ancestors.push({
                      selector: selectorFor(parent),
                      tag,
                      repeat_group_size: repeatGroupSizeFor(parent),
                    });
                    parent = parent.parentElement;
                    if (tag === "main" || tag === "article" || tag === "body") break;
                  }
                  return ancestors;
                })(),
                web_ancestor_structure_complete: (() => {
                  let parent = element.parentElement;
                  for (let depth = 0; parent && depth < 32; depth += 1) {
                    const tag = parent.tagName.toLowerCase();
                    if (tag === "main" || tag === "article" || tag === "body") return true;
                    parent = parent.parentElement;
                  }
                  return !parent;
                })(),
                within_web_navigation: structuralContextFacts.within_web_navigation,
                web_detail_priority_candidate: priorityWebDetailCandidates.includes(element),
                web_subject_boundary: priorityWebRecommendationBoundaries.includes(element),
                web_obstruction_layer: webObstructionFacts.layer_selector,
                web_obstruction_layer_rect: webObstructionFacts.layer_rect,
                web_obstruction_layer_kind: webObstructionFacts.layer_kind,
                web_obstruction_layer_structural: webObstructionFacts.layer_structural,
                web_obstruction_close_semantics: webObstructionFacts.close_semantics,
                web_obstruction_close_basis: webObstructionFacts.close_basis,
                web_obstruction_close_control_unique:
                  webObstructionFacts.close_control_unique,
                web_obstruction_close_hit_safe: webObstructionFacts.close_hit_safe,
                web_human_verification_present:
                  webObstructionFacts.human_verification_present,
              } : {}),
              web_recommend_ad_close_direct_chain: element.matches(
                ".recommend-layer > .recommend-wrapper > .recommend-card > .close-btn"
              ),
              navigation_payload_in_subtree: navigationPayloadInSubtree(element),
              rect: bounds,
              hit_bounds: bounds,
              visual_bounds: unionBounds(childBounds, bounds),
              viewport_coverage: viewportCoverage(bounds),
              visible: !isFlutterSemantics || !visualOcclusionDetected,
              effective_z_index: stackingFacts.effective_z_index,
              fixed_or_sticky_ancestor: stackingFacts.fixed_or_sticky_ancestor,
              positioned_ancestor_z_indices: stackingFacts.positioned_ancestor_z_indices,
              within_navigation: structuralContextFacts.within_navigation,
              within_pagination: structuralContextFacts.within_pagination,
              within_breadcrumb: structuralContextFacts.within_breadcrumb,
              within_footer: structuralContextFacts.within_footer,
              is_advertisement: structuralContextFacts.is_advertisement,
              ...(videoAdvertisementFacts ? {
                web_media_is_advertisement: videoAdvertisementFacts.is_advertisement,
                web_media_ad_context_complete: videoAdvertisementFacts.complete,
              } : {}),
              repeat_group_size: repeatGroupSize,
              within_repeated_structure: repeatGroupSize >= 2,
              style: {
                display: style.display,
                visibility: style.visibility,
                opacity: style.opacity,
                position: style.position,
                z_index: style.zIndex,
                pointer_events: style.pointerEvents,
                overflow: style.overflow,
                overflow_x: style.overflowX,
                overflow_y: style.overflowY,
                font_family: style.fontFamily.slice(0, 160),
                font_size: style.fontSize,
                font_weight: style.fontWeight,
                font_style: style.fontStyle,
                text_transform: style.textTransform,
                text_overflow: style.textOverflow,
                white_space: style.whiteSpace,
                webkit_line_clamp: style.webkitLineClamp,
                line_clamp: style.lineClamp,
                object_fit: style.objectFit,
              },
              text_metrics: {
                scroll_width: element.scrollWidth,
                client_width: element.clientWidth,
                offset_width: element.offsetWidth,
                scroll_height: element.scrollHeight,
                client_height: element.clientHeight,
                offset_height: element.offsetHeight,
                horizontal_overflow: element.scrollWidth > element.clientWidth + 1,
                vertical_overflow: element.scrollHeight > element.clientHeight + 1,
                text_overflow_ellipsis: style.textOverflow === "ellipsis",
                line_clamp: style.webkitLineClamp || style.lineClamp || null,
              },
              href: hrefResource.value,
              href_resource_summary: hrefResource.summary,
              external: href ? (() => {
                try { return new URL(href, document.baseURI).origin !== origin; }
                catch { return null; }
              })() : null,
              target: element.getAttribute("target"),
              rel: element.getAttribute("rel"),
              onclick: trimText(element.getAttribute("onclick")),
              data_route: trimText(element.getAttribute("data-route")),
              data_href: trimText(element.getAttribute("data-href")),
              data_url: trimText(element.getAttribute("data-url")),
              data_uri: trimText(element.getAttribute("data-uri")),
              router_to: trimText(element.getAttribute("to")),
              src: srcResource.value,
              src_resource_summary: srcResource.summary,
              alt: element.getAttribute("alt"),
              type: __WEB_DETAIL_CANDIDATE_PRIORITY__ && (
                element instanceof HTMLInputElement || element instanceof HTMLButtonElement
              ) ? element.type : element.getAttribute("type"),
              name: element.getAttribute("name"),
              placeholder: element.getAttribute("placeholder"),
              accept: element.getAttribute("accept"),
              interactive: !sourceMediaParent && Boolean(
                ["a", "button", "input", "select", "textarea", "summary"]
                  .includes(element.tagName.toLowerCase())
                || [
                  "button", "checkbox", "link", "menuitem", "option", "radio", "switch", "tab",
                ].includes(String(element.getAttribute("role") || "").toLowerCase())
                || element.hasAttribute("onclick")
                || element.tabIndex >= 0
                || style.cursor === "pointer"
                || (__H5_MODAL_CENSUS__ && h5ModalControls.includes(element)
                  && Boolean(h5ModalLocalSemantics(element)))
                || (
                  element.classList.contains("horizontalVideoBox")
                  && element.classList.contains("videoBox")
                )
                || (
                  element.tagName.toLowerCase() === "flt-semantics"
                  && element.hasAttribute("flt-tappable")
                )
              ),
              inactive_launch_shell: inactiveLaunchShell,
              content_editable: element.isContentEditable,
              read_only: "readOnly" in element ? Boolean(element.readOnly) : null,
              disabled: "disabled" in element ? Boolean(element.disabled) : null,
              checked: "checked" in element ? Boolean(element.checked) : null,
              hit_test: {
                center_target: centerTarget ? selectorFor(centerTarget) : null,
                center_hit_element: centerTarget ? selectorFor(centerTarget) : null,
                center_target_class_tokens: centerTarget
                  ? [...centerTarget.classList]
                    .slice(0, 12)
                    .map((token) => token.slice(0, CLASS_TOKEN_LIMIT))
                  : [],
                center_target_ancestor_class_tokens: (() => {
                  const seen = new Set();
                  let ancestor = centerTarget?.parentElement || null;
                  for (let depth = 0; ancestor && depth < 8 && seen.size < 16; depth += 1) {
                    for (const token of ancestor.classList) {
                      if (seen.size >= 16) break;
                      seen.add(token.slice(0, CLASS_TOKEN_LIMIT));
                    }
                    ancestor = ancestor.parentElement;
                  }
                  return [...seen];
                })(),
                center_target_inactive_launch_shell: isInactiveLaunchShell(centerTarget),
                center_hits_target: centerHitsTarget,
                visual_occlusion_detected: visualOcclusionDetected,
                topmost: centerHitsTarget,
                pointer_interception: Boolean(
                  style.pointerEvents !== "none"
                  && centerHitsTarget,
                ),
                ...(videoCenterFacts || {}),
              },
            };
            if (__H5_MODAL_CENSUS__ && h5ModalLayerSet.has(element)) {
              const identities = [];
              for (const image of [...element.querySelectorAll("img")].slice(0,4)) {
                const resource = await summarizeResource(image.currentSrc || image.src, true);
                if (resource.value && (!resource.summary || resource.summary.sha256)) {
                  identities.push(resource.value);
                }
              }
              item.h5_modal_media_identity = [...new Set(identities)];
            }
            if (element instanceof HTMLImageElement) {
              if(__H5_MODAL_CENSUS__){
                item.h5_probe_image_resources=await imageResources(element);
                item.h5_probe_gallery_binding=sourceGalleryBinding(element);
                item.h5_probe_gallery_item=galleryPreviewItems.get(element) || null;
              }
              item.image = {
                natural_width: element.naturalWidth,
                natural_height: element.naturalHeight,
                complete: element.complete,
              };
            }
            if (__WEB_DETAIL_CANDIDATE_PRIORITY__
              && element.matches("main,p," + webArticleBodySelector)) {
              item.text_length = Math.min((element.innerText || "").trim().length, 1000000);
              if (element.matches("main," + webArticleBodySelector)) {
                item.web_subject_text_length = webSubjectTextLength(element);
              }
            }
            if (element instanceof HTMLMediaElement) {
              if (__H5_MODAL_CENSUS__ && element instanceof HTMLVideoElement) {
                const ledger=window.__aliothProbeMediaIdsV1 ||= {ids:new WeakMap(), next:1};
                if(!ledger.ids.has(element)) ledger.ids.set(element,
                  `${performance.timeOrigin}:video:${ledger.next++}`);
                item.h5_media_instance_id=ledger.ids.get(element);
              }
              if (__WEB_DETAIL_CANDIDATE_PRIORITY__ && element instanceof HTMLVideoElement) {
                item.poster = (await summarizeResource(element.poster || null)).value;
              }
              item.media = {
                paused: element.paused,
                ended: element.ended,
                muted: element.muted,
                controls: element.controls,
                current_time: round(element.currentTime),
                duration: round(element.duration),
                ready_state: element.readyState,
                seeking: element.seeking,
                playback_rate: round(element.playbackRate),
                ...(__H5_MODAL_CENSUS__ && element instanceof HTMLVideoElement ? {
                  video_width: element.videoWidth, video_height: element.videoHeight,
                  fullscreen_enabled: document.fullscreenEnabled === true,
                } : {}),
              };
            }
            elements.push(item);
          }
          const media = elements
            .filter((item) => (
              ["video", "img", "canvas"].includes(item.tag)
              || item.class_tokens.some((token) => /full-?screen/i.test(token))
            ))
            .slice(0, 100)
            .map((item, mediaIndex) => ({
              media_index: mediaIndex,
              selector: item.selector,
              tag: item.tag,
              class_tokens: item.class_tokens,
              bounds: item.rect,
              viewport_coverage: viewportCoverage(item.rect),
              state: item.media || null,
              ...(__WEB_DETAIL_CANDIDATE_PRIORITY__ && item.tag === "video" ? {
                is_advertisement: item.web_media_is_advertisement,
                media_ad_context_complete: item.web_media_ad_context_complete,
                within_navigation: item.within_navigation,
                within_web_navigation: item.within_web_navigation,
                within_repeated_structure: item.within_repeated_structure,
                within_footer: item.within_footer,
                hit_test: {
                  center_hits_target: item.hit_test.center_hits_target,
                  center_within_player_surface:
                    item.hit_test.center_within_player_surface,
                  center_obstruction_layer: item.hit_test.center_obstruction_layer,
                  center_ad_click_overlay: item.hit_test.center_ad_click_overlay,
                  center_pointer_interception:
                    item.hit_test.center_pointer_interception,
                },
              } : {}),
            }));
          const navigation = performance.getEntriesByType("navigation")[0];
          const paints = performance.getEntriesByType("paint").map((entry) => ({
            name: entry.name,
            start_time_ms: round(entry.startTime),
          }));
          const historyState = globalThis[
            Symbol.for("alioth.logicalSurface.history")
          ] || {};
          const historyApiObserved = [
            historyState.push_state_count,
            historyState.replace_state_count,
            historyState.pop_state_count,
            historyState.hash_change_count,
          ].some((value) => Number(value || 0) > 0);
          const spaRootPresent = Boolean(document.querySelector(
            "#app,[data-v-app],#root,[data-reactroot],[class*='app-root']",
          ));
          const visualViewport = window.visualViewport;
          const flutterHost = document.querySelector("flt-semantics-host");
          const flutterHostTransform = flutterHost
            ? new DOMMatrixReadOnly(getComputedStyle(flutterHost).transform)
            : null;
          return {
            schema_version: "ui-snapshot.v2",
            ...(__H5_MODAL_CENSUS__ ? {
              h5_scroll_regions: h5ScrollRegions,
              h5_probe_preview_roots:probePreviewRoots.slice(0,16).map(previewId),
              h5_probe_preview_roots_truncated:probePreviewRoots.length>16,
              h5_probe_preview_root_states:previewStates(),
              h5_main_structure: __H5_NATIVE_MAIN_SHELL__,
              h5_modal_census_selectors: [...new Set([
                ...h5ModalLayers, ...h5ModalControls,
              ])].map(selectorFor),
              h5_modal_census_truncated: h5ModalNodes.length > 4000 || h5ModalLayers.length > 40
                || h5ModalDescendants.length > 4000
                || [...h5ModalLayers, ...h5ModalControls].some((element) =>
                  !elements.some((item) => item.selector === selectorFor(element))),
            } : {}),
            captured_at: new Date().toISOString(),
            performance_now_ms: round(performance.now()),
            url: window.location.href,
            title: document.title,
            ready_state: document.readyState,
            visibility_state: document.visibilityState,
            viewport: {
              width: window.innerWidth,
              height: window.innerHeight,
              device_pixel_ratio: window.devicePixelRatio,
              scroll_x: round(window.scrollX),
              scroll_y: round(window.scrollY),
              visual_viewport_width: round(
                visualViewport ? visualViewport.width : window.innerWidth,
              ),
              visual_viewport_height: round(
                visualViewport ? visualViewport.height : window.innerHeight,
              ),
              visual_viewport_offset_left: round(
                visualViewport ? visualViewport.offsetLeft : 0,
              ),
              visual_viewport_offset_top: round(
                visualViewport ? visualViewport.offsetTop : 0,
              ),
              visual_viewport_page_left: round(
                visualViewport ? visualViewport.pageLeft : window.scrollX,
              ),
              visual_viewport_page_top: round(
                visualViewport ? visualViewport.pageTop : window.scrollY,
              ),
              visual_viewport_scale: round(visualViewport ? visualViewport.scale : 1),
              flutter_host_transform: flutterHostTransform
                ? [
                    round(flutterHostTransform.a),
                    round(flutterHostTransform.b),
                    round(flutterHostTransform.c),
                    round(flutterHostTransform.d),
                    round(flutterHostTransform.e),
                    round(flutterHostTransform.f),
                  ]
                : [1, 0, 0, 1, 0, 0],
            },
            document: {
              time_origin_ms: round(performance.timeOrigin),
              flutter_root_present: Boolean(document.querySelector(
                "flt-glass-pane,flt-semantics-host,flutter-view",
              )),
              history_length: history.length,
              history_api_observed: historyApiObserved,
              spa_root_present: spaRootPresent,
              navigation_type: navigation ? navigation.type : null,
              scroll_width: document.documentElement.scrollWidth,
              scroll_height: document.documentElement.scrollHeight,
              fullscreen: Boolean(document.fullscreenElement),
              active_element: document.activeElement
                ? selectorFor(document.activeElement)
                : null,
            },
            document_state: {
              fullscreen_active: Boolean(document.fullscreenElement),
              fullscreen_tag: document.fullscreenElement
                ? document.fullscreenElement.tagName.toLowerCase()
                : null,
            },
            performance: navigation ? {
              dom_content_loaded_ms: round(navigation.domContentLoadedEventEnd),
              load_event_ms: round(navigation.loadEventEnd),
              response_end_ms: round(navigation.responseEnd),
              transfer_size: navigation.transferSize,
              paints,
            } : { paints },
            element_count: elements.length,
            element_limit_reached: elements.length >= ELEMENT_LIMIT,
            ...(__WEB_DETAIL_CANDIDATE_PRIORITY__ ? {
              web_detail_candidate_priority_enabled: true,
              web_native_structure: __WEB_NATIVE_STRUCTURE_CENSUS__,
              web_overlay_geometry: webOverlayGeometryFor(),
            } : {}),
            elements,
            media_truncated: media.length >= 100,
            media,
          };
        }
        """.replace("__UNIQUE_SELECTOR_FOR__", UNIQUE_SELECTOR_FOR_SCRIPT).replace(
            "__GENERIC_NAVIGATION_SELECTORS__",
            json.dumps(generic_navigation_selectors, ensure_ascii=True),
        )
        .replace(
            "__COMMUNITY_HOME_POST_TITLE_SELECTOR__",
            json.dumps(
                H5_COMMUNITY_HOME_POST_TITLE_SELECTOR.removesuffix(":visible"),
                ensure_ascii=True,
            ),
        )
        .replace(
            "__MANGA_VIDEO_TITLE_SELECTOR__",
            json.dumps(
                H5_MANGA_VIDEO_TITLE_SELECTOR.removesuffix(":visible"),
                ensure_ascii=True,
            ),
        )
        .replace(
            "__LONG_VIDEO_CARD_TITLE_SELECTOR__",
            json.dumps(
                H5_LONG_VIDEO_CARD_TITLE_SELECTOR.removesuffix(":visible"),
                ensure_ascii=True,
            ),
        )
        .replace(
            "__CONTENT_LAYOUT3_TITLE_SELECTOR__",
            json.dumps(
                H5_CONTENT_LAYOUT3_TITLE_SELECTOR.removesuffix(":visible"),
                ensure_ascii=True,
            ),
        )
        .replace(
            "__HOME_VIDEO_GRID_TITLE_SELECTOR__",
            json.dumps(
                H5_HOME_VIDEO_GRID_TITLE_SELECTOR.removesuffix(":visible"),
                ensure_ascii=True,
            ),
        )
        .replace(
            "__HOME_VIDEO_TITLE_SELECTOR__",
            json.dumps(
                H5_HOME_VIDEO_TITLE_SELECTOR.removesuffix(":visible"),
                ensure_ascii=True,
            ),
        )
        .replace(
            "__MASONRY_VIDEO_TITLE_SELECTOR__",
            json.dumps(
                H5_MASONRY_VIDEO_TITLE_SELECTOR.removesuffix(":visible"),
                ensure_ascii=True,
            ),
        )
        .replace(
            "__WEB_ARTICLE_BODY_SELECTOR__", json.dumps(article_body_selector),
        )
        .replace(
            "__WEB_DETAIL_CANDIDATE_PRIORITY__",
            "true" if web_detail_candidate_priority else "false",
        )
        .replace(
            "__H5_GALLERY_BINDING_CENSUS__",
            H5_GALLERY_BINDING_CENSUS_SCRIPT,
        )
        .replace(
            "__H5_MODAL_CENSUS__",
            "true" if str(surface_session_id or "").startswith("h5:") else "false",
        )
    )
    from services.api.app.infrastructure.browser.web_native_structure import (
        WEB_NATIVE_STRUCTURE_CENSUS_SCRIPT,
    )

    snapshot_script = snapshot_script.replace(
        "__WEB_NATIVE_STRUCTURE_CENSUS__",
        WEB_NATIVE_STRUCTURE_CENSUS_SCRIPT if web_detail_candidate_priority else "null",
    )
    from services.api.app.infrastructure.browser.h5_native_main_shell import (
        H5_NATIVE_MAIN_SHELL_SCRIPT,
    )

    snapshot_script = snapshot_script.replace(
        "__H5_NATIVE_MAIN_SHELL__",
        H5_NATIVE_MAIN_SHELL_SCRIPT
        if str(surface_session_id or "").startswith("h5:") else "null",
    )
    from services.api.app.infrastructure.browser.h5_local_media_digest import (
        H5_LOCAL_MEDIA_DIGEST_JS,
        ensure_h5_local_media_digest,
    )

    h5_surface = str(surface_session_id or "").startswith("h5:")
    snapshot_script = snapshot_script.replace(
        "__H5_LOCAL_MEDIA_DIGEST__", H5_LOCAL_MEDIA_DIGEST_JS if h5_surface else "",
    )
    if web_detail_candidate_priority:
        from services.api.app.infrastructure.browser.web_snapshot_compat import (
            prepare_web_snapshot_script,
        )

        snapshot_script = prepare_web_snapshot_script(snapshot_script)
    try:
        if h5_surface:
            await ensure_h5_local_media_digest(page)
        raw_snapshot = await evaluate(snapshot_script)
        if isinstance(raw_snapshot, dict) and str(surface_session_id or "").startswith("h5:"):
            # 保存当前真实滚动位置，不把一次下滚或列表末项可见等同于到达底部。
            raw_snapshot["h5_scroll_state"] = await _collect_scroll_state(
                page, surface_session_id=surface_session_id,
            )
        snapshot = _sanitize_and_bound_ui_snapshot(raw_snapshot)
        if h5_surface:
            from services.api.app.infrastructure.browser.h5_player_frames import (
                attach_player_frames,
            )

            snapshot = await attach_player_frames(page, snapshot)
        snapshot["flutter_semantics_activation"] = flutter_semantics_activation
        return _finish_ui_snapshot_collection(
            snapshot,
            page=page,
            logical_surface_enabled=logical_surface_enabled,
            logical_surface_shadow=logical_surface_shadow,
            surface_session_id=surface_session_id,
            defer_logical_surface=defer_logical_surface,
        )
    except Exception as exc:  # noqa: BLE001
        return _finish_ui_snapshot_collection(
            {
                "schema_version": "ui-snapshot.v2",
                "url": str(getattr(page, "url", "") or ""),
                "elements": [],
                "element_count": 0,
                "collection_error": str(exc)[:500],
                "flutter_semantics_activation": flutter_semantics_activation,
            },
            page=page,
            logical_surface_enabled=logical_surface_enabled,
            logical_surface_shadow=logical_surface_shadow,
            surface_session_id=surface_session_id,
            defer_logical_surface=defer_logical_surface,
        )


def _finish_ui_snapshot_collection(
    snapshot: dict[str, Any],
    *,
    page: Any,
    logical_surface_enabled: bool,
    logical_surface_shadow: bool,
    surface_session_id: str | None,
    defer_logical_surface: bool,
) -> dict[str, Any]:
    """允许专用 PNG 先补视觉事实，再由调用方执行唯一一次最终身份附加。"""
    if defer_logical_surface:
        return snapshot
    return _attach_logical_surface(
        snapshot,
        page=page,
        logical_surface_enabled=logical_surface_enabled,
        logical_surface_shadow=logical_surface_shadow,
        surface_session_id=surface_session_id,
    )


def _attach_logical_surface(
    snapshot: dict[str, Any],
    *,
    page: Any,
    logical_surface_enabled: bool,
    logical_surface_shadow: bool,
    surface_session_id: str | None,
) -> dict[str, Any]:
    """为 Snapshot 追加可选逻辑证据，并继续遵守 512 KiB 上限。"""
    if not logical_surface_enabled:
        document = snapshot.get("document")
        if isinstance(document, dict):
            document.pop("flutter_root_present", None)
        return snapshot
    document = snapshot.get("document")
    document_facts = document if isinstance(document, Mapping) else {}
    time_origin = document_facts.get("time_origin_ms")
    try:
        logical_surface = build_h5_logical_surface(
            ui_snapshot=snapshot,
            page_url=str(snapshot.get("url") or getattr(page, "url", "") or ""),
            surface_session_id=surface_session_id,
            document_token=f"{id(page)}:{time_origin if time_origin is not None else ''}",
        )
    except Exception as exc:  # noqa: BLE001 - 逻辑补证失败不得覆盖原 Capture
        snapshot["logical_surface_degraded"] = str(exc)[:200]
        return snapshot
    logical_surface["shadow"] = bool(logical_surface_shadow)
    snapshot["logical_surface"] = logical_surface
    _bound_snapshot_after_logical_surface(snapshot)
    return snapshot


def _bound_snapshot_after_logical_surface(snapshot: dict[str, Any]) -> None:
    """优先保留逻辑审计，超限时淘汰低价值 UI 元素。"""
    elements = snapshot.get("elements")
    sanitized_elements = elements if isinstance(elements, list) else []
    while sanitized_elements and _json_size_bytes(snapshot) > MAX_UI_SNAPSHOT_BYTES:
        remove_at = min(
            range(len(sanitized_elements)),
            key=lambda index: _snapshot_element_retention_score(
                snapshot, sanitized_elements[index]
            ),
        )
        sanitized_elements.pop(remove_at)
        snapshot["truncated"] = True
        snapshot["truncated_reason"] = "logical_surface_size_reservation"
        snapshot["truncated_element_count"] = (
            int(snapshot.get("truncated_element_count") or 0) + 1
        )
        snapshot["element_count"] = len(sanitized_elements)
    _validate_h5_modal_census_retention(snapshot)
    if _json_size_bytes(snapshot) <= MAX_UI_SNAPSHOT_BYTES:
        return
    surface = snapshot.get("logical_surface")
    if isinstance(surface, dict):
        surface["identity_signals"] = {}
        surface["state_signals"] = {}
        surface["signals_truncated"] = True


def _snapshot_element_retention_score(
    snapshot: dict[str, Any], element: dict[str, Any], *, web_primary_media: bool = False,
) -> tuple[int, ...]:
    """保留 H5 普查和可验签业务入口；压缩不改变动作合同或字节上限。"""
    census = snapshot.get("h5_modal_census_selectors")
    protected = int(isinstance(census, list) and element.get("selector") in census)
    navigation = 0
    if isinstance(census, list):
        from services.api.app.application.h5_profile_payment_navigation import (
            H5_PROFILE_PAYMENT_IMAGE_CAPABILITIES,
            h5_profile_payment_snapshot_structure_matches,
        )

        navigation = int(any(
            h5_profile_payment_snapshot_structure_matches(element, capability=capability)
            for capability in H5_PROFILE_PAYMENT_IMAGE_CAPABILITIES
        ))
    return (
        protected, navigation,
        *_ui_element_retention_score(element, web_primary_media=web_primary_media),
    )


def _validate_h5_modal_census_retention(snapshot: dict[str, Any]) -> None:
    """压缩后缺失任何普查成员都必须降级，不能沿用浏览器压缩前的完整标记。"""
    if "h5_modal_census_truncated" not in snapshot:
        return
    census = snapshot.get("h5_modal_census_selectors")
    selectors = {
        element.get("selector") for element in snapshot.get("elements", [])
        if isinstance(element, dict)
    }
    if not isinstance(census, list) or any(selector not in selectors for selector in census):
        snapshot["h5_modal_census_truncated"] = True


async def _collect_media_state(page: Any) -> dict[str, Any]:
    """采集同一浏览器时钟下的媒体真值与页面时间展示候选。"""
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return {
            "schema_version": "media-state.v1",
            "url": str(getattr(page, "url", "") or ""),
            "items": [],
            "display_candidates": [],
            "capture_degraded": "page_evaluate_unavailable",
        }
    try:
        raw = await evaluate(
            """
            () => {
              const ITEM_LIMIT = 20;
              const DISPLAY_LIMIT = 30;
              const SELECTOR_LIMIT = 512;
              const round = (value) => Number.isFinite(value)
                ? Math.round(value * 1000) / 1000
                : null;
              __UNIQUE_SELECTOR_FOR__
              const boundsFor = (element) => {
                const rect = element.getBoundingClientRect();
                return {
                  x: round(rect.x),
                  y: round(rect.y),
                  width: round(rect.width),
                  height: round(rect.height),
                };
              };
              const ranges = (value) => {
                const items = [];
                for (let index = 0; index < value.length && index < 10; index += 1) {
                  items.push({ start: round(value.start(index)), end: round(value.end(index)) });
                }
                return items;
              };
              const items = [...document.querySelectorAll("video,audio")]
                .slice(0, ITEM_LIMIT)
                .map((media, index) => ({
                  media_index: index,
                  selector: selectorFor(media),
                  tag: media.tagName.toLowerCase(),
                  bounds: boundsFor(media),
                  current_time: round(media.currentTime),
                  duration: round(media.duration),
                  paused: media.paused,
                  ended: media.ended,
                  muted: media.muted,
                  controls: media.controls,
                  seeking: media.seeking,
                  ready_state: media.readyState,
                  playback_rate: round(media.playbackRate),
                  seekable_ranges: ranges(media.seekable),
                }));
              const timePattern = /(?:^|\\s)(?:\\d{1,2}:)?\\d{1,2}:\\d{2}(?:\\s|$)/;
              const displayCandidates = [...document.querySelectorAll(
                "time,output,[role='timer'],[class*='time'],[class*='duration'],"
                + "[class*='progress'],[aria-valuetext]",
              )].filter((element) => {
                const rect = element.getBoundingClientRect();
                const style = getComputedStyle(element);
                const text = String(
                  element.getAttribute("aria-valuetext") || element.textContent || "",
                ).replace(/\\s+/g, " ").trim();
                return rect.width > 0 && rect.height > 0
                  && style.display !== "none" && style.visibility !== "hidden"
                  && text && timePattern.test(text);
              }).slice(0, DISPLAY_LIMIT).map((element) => ({
                selector: selectorFor(element),
                text: String(
                  element.getAttribute("aria-valuetext") || element.textContent || "",
                ).replace(/\\s+/g, " ").trim().slice(0, 160),
                bounds: boundsFor(element),
              }));
              return {
                schema_version: "media-state.v1",
                captured_at: new Date().toISOString(),
                performance_now_ms: round(performance.now()),
                url: window.location.href,
                item_count: items.length,
                items_truncated: document.querySelectorAll("video,audio").length > ITEM_LIMIT,
                items,
                display_candidates: displayCandidates,
              };
            }
            """.replace("__UNIQUE_SELECTOR_FOR__", UNIQUE_SELECTOR_FOR_SCRIPT)
        )
    except Exception as exc:  # noqa: BLE001 - 证据采集失败要固化为降级事实
        return {
            "schema_version": "media-state.v1",
            "url": str(getattr(page, "url", "") or ""),
            "items": [],
            "display_candidates": [],
            "collection_error": str(exc)[:500],
        }
    state = dict(raw) if isinstance(raw, dict) else {}
    state["schema_version"] = "media-state.v1"
    state["url"] = _sanitize_resource_value(state.get("url"))[0]
    state["items"] = [
        dict(item) for item in list(state.get("items") or [])[:20] if isinstance(item, dict)
    ]
    state["display_candidates"] = [
        {
            "selector": str(item.get("selector") or "")[:MAX_UI_SELECTOR_LENGTH],
            "text": str(item.get("text") or "")[:160],
            "bounds": item.get("bounds") if isinstance(item.get("bounds"), dict) else {},
        }
        for item in list(state.get("display_candidates") or [])[:30]
        if isinstance(item, dict)
    ]
    return state


def _sanitize_and_bound_ui_snapshot(raw_snapshot: Any) -> dict[str, Any]:
    """二次清理浏览器返回值，并把序列化结果硬限制在 512 KiB 内。"""
    snapshot = dict(raw_snapshot) if isinstance(raw_snapshot, dict) else {}
    snapshot["schema_version"] = "ui-snapshot.v2"
    snapshot["url"] = _sanitize_resource_value(snapshot.get("url"))[0]
    raw_elements = snapshot.get("elements")
    elements = raw_elements if isinstance(raw_elements, list) else []
    sanitized_elements: list[dict[str, Any]] = []
    resource_summaries: list[dict[str, Any]] = []
    for raw_element in elements[:MAX_UI_SNAPSHOT_ELEMENTS]:
        if not isinstance(raw_element, dict):
            continue
        element = _sanitize_ui_element(raw_element, resource_summaries=resource_summaries)
        sanitized_elements.append(element)
    snapshot["elements"] = sanitized_elements
    snapshot["element_count"] = len(sanitized_elements)
    snapshot["resource_summaries"] = resource_summaries[:MAX_UI_SNAPSHOT_ELEMENTS]
    snapshot["structure_signature"] = _ui_structure_signature(sanitized_elements)

    if (snapshot.get("web_detail_candidate_priority_enabled") is True
            and "web_overlay_geometry" in snapshot):
        raw_geometry = snapshot["web_overlay_geometry"]
        raw_layers = raw_geometry.get("layers") if isinstance(raw_geometry, dict) else None
        raw_subjects = raw_geometry.get("subjects") if isinstance(raw_geometry, dict) else None
        layers = []
        for layer in (raw_layers if isinstance(raw_layers, list) else [])[:32]:
            if not isinstance(layer, dict):
                continue
            rect = layer.get("rect")
            valid_rect = isinstance(rect, dict) and all(
                isinstance(rect.get(key), int | float) and not isinstance(rect.get(key), bool)
                and math.isfinite(rect[key]) for key in ("x", "y", "width", "height")
            ) and rect["width"] > 0 and rect["height"] > 0
            layers.append({
                "rect": {key: rect[key] for key in ("x", "y", "width", "height")}
                if valid_rect else None,
                "ordinary": layer.get("ordinary") is True,
            })
        snapshot["web_overlay_geometry"] = {
            "schema_version": "web-overlay-geometry.v1",
            "complete": isinstance(raw_geometry, dict)
            and raw_geometry.get("schema_version") == "web-overlay-geometry.v1"
            and raw_geometry.get("complete") is True
            and isinstance(raw_layers, list) and len(raw_layers) == len(layers)
            and all(layer["rect"] is not None for layer in layers)
            and isinstance(raw_subjects, list) and len(raw_subjects) <= 32
            and all(isinstance(rect, dict) and all(
                isinstance(rect.get(key), int | float) and not isinstance(rect.get(key), bool)
                and math.isfinite(rect[key]) for key in ("x", "y", "width", "height")
            ) and rect["width"] > 0 and rect["height"] > 0 for rect in raw_subjects),
            "layers": layers,
            "subjects": [
                {key: rect.get(key) for key in ("x", "y", "width", "height")}
                for rect in (raw_subjects if isinstance(raw_subjects, list) else [])[:32]
                if isinstance(rect, dict)
            ],
        }

    removed = 0
    while sanitized_elements and _json_size_bytes(snapshot) > MAX_UI_SNAPSHOT_BYTES:
        remove_at = min(
            range(len(sanitized_elements)),
            key=lambda index: _snapshot_element_retention_score(
                snapshot, sanitized_elements[index],
                web_primary_media=snapshot.get("web_detail_candidate_priority_enabled") is True,
            ),
        )
        sanitized_elements.pop(remove_at)
        removed += 1
    if _json_size_bytes(snapshot) > MAX_UI_SNAPSHOT_BYTES:
        snapshot["media"] = []
        snapshot["resource_summaries"] = []
    if _json_size_bytes(snapshot) > MAX_UI_SNAPSHOT_BYTES:
        h5_census = (
            {"h5_modal_census_truncated": True}
            if "h5_modal_census_truncated" in snapshot else {}
        )
        snapshot = {
            "schema_version": "ui-snapshot.v2",
            "url": snapshot.get("url"),
            "title": str(snapshot.get("title") or "")[:300],
            "viewport": snapshot.get("viewport"),
            "document": snapshot.get("document"),
            "elements": [],
            "element_count": 0,
            "structure_signature": snapshot.get("structure_signature"),
            "capture_degraded": "serialized_size_limit",
            **h5_census,
        }
        removed = len(sanitized_elements) + removed
    if removed:
        snapshot["truncated"] = True
        snapshot["truncated_reason"] = "serialized_size_limit"
        snapshot["truncated_element_count"] = removed
        snapshot["element_count"] = len(snapshot.get("elements", []))
    _validate_h5_modal_census_retention(snapshot)
    return snapshot


def _sanitize_ui_element(
    raw_element: dict[str, Any], *, resource_summaries: list[dict[str, Any]]
) -> dict[str, Any]:
    element = dict(raw_element)
    element["selector"] = str(element.get("selector") or "")[:MAX_UI_SELECTOR_LENGTH]
    parent_selector = str(element.get("parent_selector") or "")[:MAX_UI_SELECTOR_LENGTH]
    adaptive_group_selector = str(
        element.get("h5_adaptive_navigation_group_selector") or ""
    )[:MAX_UI_SELECTOR_LENGTH]
    if "web_ancestor_structure" in element:
        from services.api.app.infrastructure.browser.web_body_snapshot import (
            sanitize_web_ancestor_structure,
        )

        raw_ancestors = element["web_ancestor_structure"]
        element["web_ancestor_structure"] = sanitize_web_ancestor_structure(
            raw_ancestors, selector_limit=MAX_UI_SELECTOR_LENGTH
        )
        element["web_ancestor_structure_complete"] = (
            element.get("web_ancestor_structure_complete") is True
            and isinstance(raw_ancestors, list)
            and len(raw_ancestors) == len(element["web_ancestor_structure"])
        )
    if parent_selector:
        element["parent_selector"] = parent_selector
    else:
        element.pop("parent_selector", None)
    if adaptive_group_selector:
        element["h5_adaptive_navigation_group_selector"] = adaptive_group_selector
    else:
        element.pop("h5_adaptive_navigation_group_selector", None)
    class_tokens = element.get("class_tokens")
    element["class_tokens"] = (
        [str(token)[:MAX_UI_CLASS_TOKEN_LENGTH] for token in class_tokens[:12] if str(token)]
        if isinstance(class_tokens, list)
        else []
    )
    ancestor_class_tokens = element.get("ancestor_class_tokens")
    element["ancestor_class_tokens"] = (
        [
            str(token)[:MAX_UI_CLASS_TOKEN_LENGTH]
            for token in ancestor_class_tokens[:16]
            if str(token)
        ]
        if isinstance(ancestor_class_tokens, list)
        else []
    )
    parent_class_tokens = element.get("parent_class_tokens")
    if isinstance(parent_class_tokens, list):
        element["parent_class_tokens"] = [
            str(token)[:MAX_UI_CLASS_TOKEN_LENGTH]
            for token in parent_class_tokens[:12]
            if str(token)
        ]
    else:
        element.pop("parent_class_tokens", None)
    for key in ("href", "src"):
        value, summary = _sanitize_resource_value(element.get(key))
        element[key] = value
        if summary is not None:
            element[f"{key}_resource_summary"] = summary
            resource_summaries.append(
                {
                    "selector": element["selector"],
                    "attribute": key,
                    **summary,
                }
            )
    hit_test = element.get("hit_test")
    if isinstance(hit_test, dict):
        center_target_classes = hit_test.get("center_target_class_tokens")
        center_target_ancestors = hit_test.get("center_target_ancestor_class_tokens")
        element["hit_test"] = {
            **hit_test,
            "center_target": str(hit_test.get("center_target") or "")[:MAX_UI_SELECTOR_LENGTH]
            or None,
            "center_target_class_tokens": (
                [
                    str(token)[:MAX_UI_CLASS_TOKEN_LENGTH]
                    for token in center_target_classes[:12]
                    if str(token)
                ]
                if isinstance(center_target_classes, list)
                else []
            ),
            "center_target_ancestor_class_tokens": (
                [
                    str(token)[:MAX_UI_CLASS_TOKEN_LENGTH]
                    for token in center_target_ancestors[:16]
                    if str(token)
                ]
                if isinstance(center_target_ancestors, list)
                else []
            ),
        }
    return _scrub_nested_data_resources(element)


def _sanitize_resource_value(value: Any) -> tuple[str | None, dict[str, Any] | None]:
    if value is None:
        return None, None
    raw = str(value)
    if not raw.startswith("data:"):
        return raw[:MAX_UI_RESOURCE_URL_LENGTH], None
    summarized = re.fullmatch(
        r"data:(?P<mime>[^;]{1,200});bytes=(?P<bytes>\d+);"
        r"sha256=(?P<sha>[0-9a-f]{64}|unavailable)"
        r"(?:;digest=(?P<digest>[a-z_]{1,80}))?",
        raw,
    )
    if summarized is not None:
        sha256 = summarized.group("sha")
        digest_unavailable = summarized.group("digest")
        summary: dict[str, Any] = {
            "scheme": "data",
            "mime": summarized.group("mime"),
            "byte_length": int(summarized.group("bytes")),
            "sha256": None if sha256 == "unavailable" else sha256,
        }
        if digest_unavailable:
            summary["digest_unavailable"] = digest_unavailable
        return raw, summary
    header, separator, payload = raw.partition(",")
    metadata = header[5:].split(";") if header.startswith("data:") else []
    mime = metadata[0] or "text/plain"
    try:
        content = (
            base64.b64decode(payload, validate=False)
            if any(item.casefold() == "base64" for item in metadata[1:])
            else unquote_to_bytes(payload)
        )
    except (ValueError, TypeError):
        content = payload.encode("utf-8", errors="replace") if separator else raw.encode()
    digest = hashlib.sha256(content).hexdigest()
    summary = {
        "scheme": "data",
        "mime": mime[:200],
        "byte_length": len(content),
        "sha256": digest,
    }
    return f"data:{summary['mime']};bytes={len(content)};sha256={digest}", summary


def _scrub_nested_data_resources(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _scrub_nested_data_resources(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub_nested_data_resources(item) for item in value]
    if isinstance(value, str) and value.startswith("data:"):
        return _sanitize_resource_value(value)[0]
    return value


def _ui_structure_signature(elements: list[dict[str, Any]]) -> str:
    structure = [
        {
            "selector": item.get("selector"),
            "tag": item.get("tag"),
            "role": item.get("role"),
            "class_tokens": item.get("class_tokens"),
            "text": str(item.get("text") or "")[:80],
            "rect": item.get("rect"),
            "hit_bounds": item.get("hit_bounds"),
            "visual_bounds": item.get("visual_bounds"),
            "visibility": (
                {
                    key: item["style"].get(key)
                    for key in ("display", "visibility", "opacity", "position")
                }
                if isinstance(item.get("style"), dict)
                else None
            ),
        }
        for item in elements
    ]
    return hashlib.sha256(
        json.dumps(structure, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _ui_element_retention_score(
    element: dict[str, Any],
    *,
    web_primary_media: bool = False,
) -> tuple[int, int, int, int, int, int, int]:
    from services.api.app.application.h5_probe_targets import payment_plan_structure

    # 模态弹窗证明是弹窗关闭执行的唯一依据，体积压缩驱逐时必须最后才轮到它
    class_tokens = {
        str(token).casefold() for token in (element.get("class_tokens") or []) if str(token)
    }
    parent_class_tokens = {
        str(token).casefold() for token in (element.get("parent_class_tokens") or []) if str(token)
    }
    ancestor_class_tokens = {
        str(token).casefold()
        for token in (element.get("ancestor_class_tokens") or [])
        if str(token)
    }
    source = element.get("src")
    nested_modal_dismissal = bool(
        any("close" in token for token in class_tokens)
        and {"van-dialog", "van-overlay", "van-popup"}.intersection(ancestor_class_tokens)
    )
    trusted_startup_dismissal = bool(h5_startup_dismissal_selector_from_snapshot_element(element))
    obstruction = int(
        str(element.get("role") or "").casefold() == "dialog"
        or element.get("aria_modal") is True
        or bool({"van-dialog", "van-overlay", "van-popup"} & class_tokens)
        # 关闭包装层本身没有 dialog role；只看自身会让它在密集广告按钮之后
        # 被 512 KiB 二次裁剪先驱逐，最终 Runtime 只能看到弹窗而看不到出口。
        or nested_modal_dismissal
        # 开屏退出结构由共享能力合同登记；此处只确保其不被体积裁剪驱逐，
        # 实际点击仍由执行端做唯一性、可见性和 hit-test 验签。
        or trusted_startup_dismissal
        # Web 普通阻断关闭控件已经由同帧结构、唯一性和 hit-test 形成
        # 密封事实；这里只保证该事实在 512 KiB 裁剪后仍可供运行时验签。
        or element.get("web_obstruction_close_semantics") is True
    )
    tag = str(element.get("tag") or "").casefold()
    trusted_subject_structure = int(
        tag in {"main", "article"}
        or (web_primary_media and (
            is_web_article_body_node(element) or element.get("web_subject_boundary") is True
        ))
        # 仅 Web 保留真实媒体节点，屏外播放器仍需后续密封候选验签；不提升 media 摘要。
        or (
            web_primary_media
            and element.get("visible") is True
            and tag == "video"
            and isinstance(source, str) and bool(source.strip())
            and element.get("is_advertisement") is not True
            and element.get("within_web_navigation") is not True
        )
        or bool(
            {
                "article-body",
                "post-content",
                "entry-content",
                "content-detail",
                "detail-content",
                "post-title",
                "breadcrumb",
                "nav-breadcrumb-wrap",
            }
            & class_tokens
        )
        or bool(
            {"article-body", "post-content", "entry-content", "detail-content"}
            & ancestor_class_tokens
        )
    )
    # 这些站点把设置、签到、会员入口和自定义底栏绘制成无文字图片或普通
    # li/div；若按普通内容节点裁剪，后续确定性导航会失去唯一证据。只保护
    # 已登记的结构族，不能把任意页脚或图片提升为点击授权。
    trusted_navigation_structure = int(
        bool(
            {
                "setting",
                "task_sign",
                "sign-in-entry",
                "checkin-entry",
                "vip-right-btn",
                "vip-promo__button",
                "quick-card",
                "icon-button__img",
                "vipboxsbgimg",
                "icon-setting",
            }
            & class_tokens
        )
        or bool(
            "wh-24" in class_tokens
            and {"header-left", "index-header"}.issubset(ancestor_class_tokens)
        )
        or bool({"vip-img", "payment_me_tab", "vip_box", "vipboxsbgimg"} & ancestor_class_tokens)
        or bool(
            {"icon", "icon-setting"}.issubset(class_tokens)
            and {"top-right-menu", "top-wrap", "base-info"}.issubset(ancestor_class_tokens)
        )
        or bool(
            {"left", "img-menus-wrap"}.issubset(ancestor_class_tokens)
            and h5_profile_vip_center_src_matches(source)
        )
        or bool(
            {"sigin-box", "right", "img-menus-wrap"}.issubset(ancestor_class_tokens)
            and h5_profile_daily_signin_src_matches(source)
        )
        or bool("sign-in-entry" in class_tokens and str(element.get("alt") or "").strip() == "签到")
        or bool(
            "d-img" in class_tokens
            and "sign-in" in parent_class_tokens
            and h5_profile_signin_src_matches(source)
        )
        or bool(
            "vip-promo__button" in class_tokens
            and "vip-promo" in ancestor_class_tokens
            and str(element.get("alt") or "").strip() == "立即开通"
        )
        or bool(
            "quick-card" in class_tokens
            and "quick-card-row" in ancestor_class_tokens
            and str(element.get("alt") or "").strip() == "会员中心"
        )
        or bool(
            "mine-vip-banner__btn" in class_tokens
            and {"vip-center", "mine-vip-banner"}.issubset(parent_class_tokens)
            and "".join(str(element.get("text") or "").split()) == "立即开通"
        )
        or bool({"vip-center", "mine-vip-banner"}.issubset(class_tokens))
        or bool(
            "my-balance-wrap" in ancestor_class_tokens
            and h5_profile_mine_vip_center_src_matches(source)
        )
        or bool(
            "icon-button__img" in class_tokens
            and {"icon-button", "mine-header__actions"}.issubset(ancestor_class_tokens)
            and str(element.get("alt") or "").strip() == "设置"
        )
        or "pages-foot-fix" in class_tokens
        or "pages-foot-fix" in ancestor_class_tokens
        or "footer-wrap" in class_tokens
        or "footer-wrap" in ancestor_class_tokens
        or ("footer-item" in class_tokens and "footer-wrap" in ancestor_class_tokens)
        or "van-tabbar" in class_tokens
        or "van-tabbar-item" in class_tokens
        or bool(H5_GENERIC_NAVIGATION_CONTAINER_CLASSES & class_tokens)
        or bool(H5_GENERIC_NAVIGATION_CONTAINER_CLASSES & ancestor_class_tokens)
        or bool(H5_GENERIC_NAVIGATION_ITEM_CLASSES & class_tokens)
        or bool(
            "poi_title" in class_tokens
            and "van-list" in ancestor_class_tokens
            and bool(
                {
                    "item_container",
                    "community_home",
                    "community_home_tab",
                }
                & ancestor_class_tokens
            )
        )
        or "videotitle" in class_tokens
        or bool("duohang2" in class_tokens and "item-box-comics" in ancestor_class_tokens)
        or bool(
            "name" in class_tokens and {"layout3", "video-section"}.issubset(ancestor_class_tokens)
        )
        or bool(
            "img-box" in class_tokens
            and {"layout3", "video-section"}.issubset(ancestor_class_tokens)
        )
        or bool(
            "desc" in class_tokens
            and {"item", "twogridlayout", "home-video-panel__scroll"}.issubset(
                ancestor_class_tokens
            )
        )
        or bool(
            "video-title" in class_tokens
            and {"movie-item", "masonry-grid"}.issubset(ancestor_class_tokens)
        )
        or bool({"longvideocard", "line-clamp-2"}.issubset(ancestor_class_tokens))
        or bool("modcover_name" in class_tokens and "modvideobox" in ancestor_class_tokens)
        or bool(
            "text-overflow" in class_tokens
            and {"post-box", "post", "post-text"}.issubset(ancestor_class_tokens)
        )
        or bool(
            "title" in class_tokens
            and {"post-main-item", "content-images-wrap"}.issubset(ancestor_class_tokens)
        )
        or bool(
            "decial" in class_tokens and {"big-list", "decial-wrap"}.issubset(ancestor_class_tokens)
        )
        or element.get("structural_navigation_kind") == "flutter_bottom_navigation"
        or element.get("web_detail_priority_candidate") is True
        # 选档只保留登记结构；可见性、命中、对象身份和禁止提交仍由执行端复验。
        or payment_plan_structure(element)
    )
    # 广告落地调度读取的是带 href 的锚点，而不是无 href 的广告容器；
    # 体积裁剪时必须保留这个最小可执行证据。
    trusted_ad_href = int(
        str(element.get("tag") or "").casefold() == "a"
        and bool(str(element.get("href") or "").strip())
        and element.get("is_advertisement") is True
    )
    rect = element.get("rect")
    viewport_visible = 0
    if isinstance(rect, dict):
        y = float(rect.get("y") or 0)
        height = float(rect.get("height") or 0)
        viewport_visible = int(y + height >= 0 and y <= 2000)
    interactive = int(
        str(element.get("tag") or "") in {"a", "button", "input", "select", "textarea"}
        or bool(element.get("role"))
        or element.get("interactive") is True
        or element.get("inactive_launch_shell") is True
    )
    has_text = int(bool(str(element.get("text") or "").strip()))
    return (
        obstruction,
        trusted_subject_structure,
        trusted_navigation_structure,
        trusted_ad_href,
        viewport_visible,
        interactive,
        has_text,
    )


def _request_has_trusted_dismissal_intent(request: PageCaptureRequest) -> bool:
    selector = str(request.selector or "").casefold()
    # 内部能力不接受任意 CSS；当前页面仍须在指针执行前重新验签。
    if str(request.session_id or "").startswith("h5:") and re.fullmatch(
        r"internal:modal_local_safe_acknowledgement:h5-obstruction:[0-9a-f]{64}",
        selector,
    ):
        return True
    return bool(
        selector
        and (
            _selector_has_dismissal_intent(selector)
            or selector in _TRUSTED_AGE_CONFIRM_SELECTORS
            or (
                request.allow_high_z_obstruction_hide
                and selector in _TRUSTED_OPAQUE_POPUP_DISMISSAL_SELECTORS
            )
        )
    )


def _json_size_bytes(value: Any) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    )


def _elapsed_ms(started_at: float) -> float:
    return round((time.perf_counter() - started_at) * 1000, 3)


def _artifact_payload_metrics(artifacts: list[PageCaptureArtifact]) -> dict[str, Any]:
    evidence_bytes: dict[str, int] = {}
    for artifact in artifacts:
        size = _json_size_bytes(artifact.content)
        evidence_bytes[artifact.artifact_type] = (
            evidence_bytes.get(artifact.artifact_type, 0) + size
        )
    total_bytes = sum(evidence_bytes.values())
    text_bytes = sum(
        size for artifact_type, size in evidence_bytes.items() if artifact_type != "screenshot"
    )
    return {
        "evidence_bytes": evidence_bytes,
        "evidence_bytes_total": total_bytes,
        "binary_evidence_bytes_total": total_bytes - text_bytes,
        "estimated_text_tokens": (text_bytes + 2) // 3,
    }


def _context_page_urls(context: Any) -> list[str]:
    """读取浏览器上下文页签 URL；测试替身缺失时安全降级。"""
    pages = getattr(context, "pages", None)
    if not isinstance(pages, list):
        return []
    return [
        str(getattr(item, "url", "") or "") for item in pages if str(getattr(item, "url", "") or "")
    ]


async def _read_capture_boundary_identity(*, page: Any, context: Any) -> dict[str, Any]:
    """采集不含正文和凭证的页面边界事实，用于前后同一性复核。"""

    page_url = str(getattr(page, "url", "") or "")
    context_pages = _context_pages(context)
    context_urls = [str(getattr(item, "url", "") or "") for item in context_pages]
    active_page_index = next(
        (index for index, item in enumerate(context_pages) if item is page),
        None,
    )
    document_facts: dict[str, Any] | None = None
    evaluate = getattr(page, "evaluate", None)
    if callable(evaluate):
        try:
            value = await evaluate(
                """
                () => {
                  const visible = (element) => {
                    const style = getComputedStyle(element);
                    const rect = element.getBoundingClientRect();
                    return style.display !== "none"
                      && style.visibility !== "hidden"
                      && Number(style.opacity || 1) > 0.05
                      && rect.width > 0
                      && rect.height > 0;
                  };
                  const selectors = [
                    "dialog[open]",
                    "[role='alertdialog']",
                    "[role='dialog'][aria-modal='true']",
                    "[aria-modal='true']",
                    ".van-dialog",
                    ".van-popup",
                    ".van-overlay",
                    ".modal.show",
                  ];
                  const layers = [...new Set(
                    selectors.flatMap((selector) => [...document.querySelectorAll(selector)]),
                  )].filter(visible).map((element, order) => {
                    const style = getComputedStyle(element);
                    const rect = element.getBoundingClientRect();
                    const parsedZIndex = Number.parseInt(style.zIndex || "0", 10);
                    return {
                      order,
                      z_index: Number.isFinite(parsedZIndex) ? parsedZIndex : 0,
                      tag: element.tagName.toLowerCase(),
                      id: element.id || null,
                      role: element.getAttribute("role"),
                      aria_modal: element.getAttribute("aria-modal"),
                      class_tokens: [...element.classList].sort().slice(0, 20),
                      rect: {
                        x: Math.round(rect.x * 100) / 100,
                        y: Math.round(rect.y * 100) / 100,
                        width: Math.round(rect.width * 100) / 100,
                        height: Math.round(rect.height * 100) / 100,
                      },
                    };
                  }).sort((left, right) => (
                    left.z_index - right.z_index || left.order - right.order
                  ));
                  const topLayer = layers.length ? layers[layers.length - 1] : null;
                  return {
                    time_origin_ms: Math.round(performance.timeOrigin * 100) / 100,
                    visibility_state: document.visibilityState,
                    top_layer: topLayer,
                  };
                }
                """
            )
            if isinstance(value, dict):
                document_facts = value
        except Exception:  # noqa: BLE001 - 能力缺失由一致性诊断显式记录
            document_facts = None

    top_layer = document_facts.get("top_layer") if document_facts else None
    return {
        "schema_version": "capture-boundary-identity.v1",
        "page_url_sha256": _capture_identity_digest(page_url) if page_url else None,
        "context_page_urls_sha256": [_capture_identity_digest(url) for url in context_urls if url],
        "active_page_index": active_page_index,
        "document_time_origin_ms": (
            document_facts.get("time_origin_ms") if document_facts else None
        ),
        "visibility_state": (document_facts.get("visibility_state") if document_facts else None),
        "top_layer_sha256": (
            _capture_identity_digest(
                json.dumps(top_layer, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            )
            if top_layer is not None
            else None
        ),
        "document_observation_available": document_facts is not None,
        # 通用 H5 无法可靠读取业务账号身份；不以 Cookie 或 Token 值伪造账号证明。
        "account_identity_available": False,
    }


def _assess_capture_boundary_consistency(
    *,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    temporal_capture: bool,
) -> dict[str, Any]:
    """比较采集边界；动态时间序列保留漂移事实但不要求同帧稳定。"""

    compared_fields = (
        "page_url_sha256",
        "context_page_urls_sha256",
        "active_page_index",
        "document_time_origin_ms",
        "visibility_state",
        "top_layer_sha256",
    )
    changed_signals = [field for field in compared_fields if before.get(field) != after.get(field)]
    unavailable_signals: list[str] = []
    if not before.get("page_url_sha256") or not after.get("page_url_sha256"):
        unavailable_signals.append("page_url")
    if not before.get("context_page_urls_sha256") or not after.get("context_page_urls_sha256"):
        unavailable_signals.append("window_set")
    if not before.get("document_observation_available") or not after.get(
        "document_observation_available"
    ):
        unavailable_signals.extend(("document_generation", "top_layer"))
    if not before.get("account_identity_available") or not after.get("account_identity_available"):
        unavailable_signals.append("account_identity")

    if temporal_capture:
        status = "time_series"
    elif changed_signals:
        status = "identity_drift"
    elif unavailable_signals:
        status = "consistent_with_capability_gaps"
    else:
        status = "consistent"
    return {
        "schema_version": "capture-consistency.v1",
        "status": status,
        "changed_signals": changed_signals,
        "unavailable_signals": list(dict.fromkeys(unavailable_signals)),
        "before": dict(before),
        "after": dict(after),
    }


def _capture_identity_digest(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _complete_action_trace(
    action_trace: list[dict[str, Any]],
    *,
    before_url: str,
    after_url: str,
    before_page_urls: list[str],
    after_page_urls: list[str],
    before_snapshot: dict[str, Any],
    after_snapshot: dict[str, Any],
    logical_surface_transition: Mapping[str, Any] | None = None,
    h5_session: bool = False,
) -> None:
    """补齐动作前后导航与有限状态差异，供规则 Handler 确定性使用。"""
    if not action_trace:
        return
    new_page_urls = [url for url in after_page_urls if url not in before_page_urls]
    existing_state_diff = action_trace[-1].get("state_diff")
    state_diff = {
        **(dict(existing_state_diff) if isinstance(existing_state_diff, Mapping) else {}),
        **_ui_state_diff(
            before_snapshot=before_snapshot,
            after_snapshot=after_snapshot,
            prioritize_content_changes=(
                isinstance(before_snapshot.get("h5_main_structure"), Mapping)
                and before_snapshot["h5_main_structure"].get("schema_version")
                == "h5-native-main-shell.v1"
                and isinstance(after_snapshot.get("h5_main_structure"), Mapping)
                and after_snapshot["h5_main_structure"].get("schema_version")
                == "h5-native-main-shell.v1"
            ),
        ),
    }
    before_surface = before_snapshot.get("logical_surface")
    after_surface = after_snapshot.get("logical_surface")
    target_before = _action_target_fact(
        snapshot=before_snapshot,
        selector=action_trace[-1].get("selector"),
    )
    target_after = _action_target_fact(
        snapshot=after_snapshot,
        selector=action_trace[-1].get("selector"),
    )
    if isinstance(before_surface, Mapping) and isinstance(after_surface, Mapping):
        page_tab_proof = _page_tab_transition_proof(
            action_entry=action_trace[-1],
            before_url=before_url,
            after_url=after_url,
            before_target=target_before,
            after_target=target_after,
            before_surface=before_surface,
            after_surface=after_surface,
            ui_state_diff=state_diff,
        )
        bottom_navigation_proof = _bottom_navigation_transition_proof(
            action_entry=action_trace[-1],
            before_url=before_url,
            after_url=after_url,
            before_surface=before_surface,
            after_surface=after_surface,
        )
        if page_tab_proof is not None or bottom_navigation_proof is not None:
            state_diff["logical_surface_transition"] = compare_h5_logical_surfaces(
                before=before_surface,
                after=after_surface,
                page_tab_proof=page_tab_proof,
                bottom_navigation_proof=bottom_navigation_proof,
            )
        elif logical_surface_transition is not None:
            state_diff["logical_surface_transition"] = dict(
                logical_surface_transition
            )
        else:
            state_diff["logical_surface_transition"] = compare_h5_logical_surfaces(
                before=before_surface,
                after=after_surface,
            )
    elif logical_surface_transition is not None:
        state_diff["logical_surface_transition"] = dict(logical_surface_transition)
    if (
        isinstance(before_surface, Mapping)
        and isinstance(after_surface, Mapping)
        and h5_session
    ):
        before_document = before_snapshot.get("document")
        after_document = after_snapshot.get("document")
        before_origin = (
            before_document.get("time_origin_ms") if isinstance(before_document, Mapping) else None
        )
        after_origin = (
            after_document.get("time_origin_ms") if isinstance(after_document, Mapping) else None
        )
        state_diff["h5_obstruction_document_scope"] = {
            "schema_version": "h5-obstruction-document-scope.v1",
            "same_document": bool(
                isinstance(before_origin, (int, float))
                and not isinstance(before_origin, bool)
                and isinstance(after_origin, (int, float))
                and not isinstance(after_origin, bool)
                and math.isfinite(before_origin)
                and math.isfinite(after_origin)
                and before_origin == after_origin
                and before_url == after_url
                and before_page_urls == after_page_urls
            ),
        }
    action_trace[-1].update(
        {
            "before_url": before_url,
            "after_url": after_url,
            "new_page_urls": new_page_urls,
            "target_before": target_before,
            "target_after": target_after,
            "state_diff": state_diff,
        }
    )


def _page_tab_transition_proof(
    *,
    action_entry: Mapping[str, Any],
    before_url: str,
    after_url: str,
    before_target: Mapping[str, Any] | None,
    after_target: Mapping[str, Any] | None,
    before_surface: Mapping[str, Any],
    after_surface: Mapping[str, Any],
    ui_state_diff: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """由同一语义 Tab 的未选中到选中变化构造一次性内部证明。"""
    if (
        action_entry.get("action") != PageTriggerAction.CLICK.value
        or not before_url
        or not isinstance(before_target, Mapping)
        or not isinstance(after_target, Mapping)
    ):
        return None
    selector = str(action_entry.get("selector") or "").strip()
    before_selector = str(before_target.get("selector") or "").strip()
    after_selector = str(after_target.get("selector") or "").strip()
    before_label = re.sub(
        r"\s+",
        " ",
        str(before_target.get("aria_label") or before_target.get("text") or ""),
    ).strip()
    after_label = re.sub(
        r"\s+",
        " ",
        str(after_target.get("aria_label") or after_target.get("text") or ""),
    ).strip()
    if (
        not selector
        or selector != before_selector
        or selector != after_selector
        or not before_label
        or (after_label and before_label != after_label)
        or str(before_target.get("role") or "").casefold() != "tab"
        or str(after_target.get("role") or "").casefold() != "tab"
        or _page_tab_fact_is_active(before_target)
        or not _page_tab_fact_is_active(after_target)
    ):
        return None
    before_identity = str(before_surface.get("identity_fingerprint") or "")
    after_identity = str(after_surface.get("identity_fingerprint") or "")
    before_state = str(before_surface.get("state_fingerprint") or "")
    after_state = str(after_surface.get("state_fingerprint") or "")
    if (
        not before_identity
        or not after_identity
        or not before_state
        or not after_state
    ):
        return None
    content_change_proof: dict[str, Any] = {}
    if (
        before_url == after_url
        and before_identity == after_identity
        and before_state == after_state
    ):
        content_change_proof = _page_tab_content_change_proof(
            ui_state_diff=ui_state_diff,
            target_selector=selector,
        )
        if not content_change_proof:
            return None
    before_activation = _page_tab_activation_digest(before_target)
    after_activation = _page_tab_activation_digest(after_target)
    if before_activation == after_activation:
        return None
    return {
        "protocol_version": "page_tab_transition_proof.v1",
        "status": "trusted",
        "effect": (
            "activation_state_change"
            if before_url == after_url
            else "route_activation_change"
        ),
        "before_location": before_url,
        "after_location": after_url,
        "target_selector_sha256": _capture_identity_digest(selector),
        "target_label_sha256": _capture_identity_digest(before_label.casefold()),
        "activation_before_sha256": before_activation,
        "activation_after_sha256": after_activation,
        "before_identity_fingerprint": before_identity,
        "after_identity_fingerprint": after_identity,
        **content_change_proof,
    }


def _page_tab_content_change_proof(
    *,
    ui_state_diff: Mapping[str, Any] | None,
    target_selector: str,
) -> dict[str, Any]:
    """为同页 Tab 内容面板替换生成摘要，排除跑马灯位移等局部动态。"""
    if not isinstance(ui_state_diff, Mapping):
        return {}
    changes = ui_state_diff.get("changes")
    if not isinstance(changes, list):
        return {}
    before_material: list[str] = []
    after_material: list[str] = []
    material_change_count = 0
    for change in changes:
        if not isinstance(change, Mapping):
            continue
        selector = str(change.get("selector") or "").strip()
        fields = {
            str(item).strip()
            for item in change.get("changed_fields") or ()
            if str(item).strip()
        }
        if not selector or selector == target_selector or fields == {"rect"}:
            continue
        before = _page_tab_content_state_material(change.get("before"))
        after = _page_tab_content_state_material(change.get("after"))
        if before is None and after is None:
            continue
        if before is not None:
            before_material.append(f"{selector}:{before}")
        if after is not None:
            after_material.append(f"{selector}:{after}")
        material_change_count += 1
    if not before_material or not after_material or material_change_count < 2:
        return {}
    before_digest = _capture_identity_digest(
        json.dumps(sorted(before_material), ensure_ascii=False, separators=(",", ":"))
    )
    after_digest = _capture_identity_digest(
        json.dumps(sorted(after_material), ensure_ascii=False, separators=(",", ":"))
    )
    if before_digest == after_digest:
        return {}
    return {
        "content_before_sha256": before_digest,
        "content_after_sha256": after_digest,
        "content_change_count": material_change_count,
    }


def _page_tab_content_state_material(value: Any) -> str | None:
    """只摘要具有业务内容体量的区域，忽略小控件与纯坐标变化。"""
    if not isinstance(value, Mapping):
        return None
    text = re.sub(r"\s+", " ", str(value.get("text") or "")).strip()
    rect = value.get("rect")
    if len(text) < 24 or not isinstance(rect, Mapping):
        return None
    try:
        area = float(rect.get("width") or 0) * float(rect.get("height") or 0)
    except (TypeError, ValueError):
        return None
    if area < 10_000:
        return None
    return _capture_identity_digest(text)


def _page_tab_fact_is_active(value: Mapping[str, Any]) -> bool:
    classes = {
        str(token).strip().casefold()
        for token in value.get("class_tokens") or []
        if str(token).strip()
    }
    return bool(
        classes.intersection({"active", "checked", "current", "on", "selected"})
        or classes.intersection({"is-active", "is-current", "is-selected"})
        or any(token.endswith(("--active", "-active", "_active")) for token in classes)
        or str(value.get("aria_selected") or "").casefold() == "true"
        or str(value.get("aria_current") or "").casefold() in {"page", "true"}
        or value.get("checked") is True
    )


def _page_tab_activation_digest(value: Mapping[str, Any]) -> str:
    material = {
        "class_tokens": sorted(
            str(token).strip().casefold()
            for token in value.get("class_tokens") or []
            if str(token).strip()
        ),
        "aria_selected": str(value.get("aria_selected") or "").casefold(),
        "aria_current": str(value.get("aria_current") or "").casefold(),
        "checked": value.get("checked") is True,
    }
    return _capture_identity_digest(
        json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def _bottom_navigation_transition_proof(
    *,
    action_entry: Mapping[str, Any],
    before_url: str,
    after_url: str,
    before_surface: Mapping[str, Any],
    after_surface: Mapping[str, Any],
) -> dict[str, Any] | None:
    """从精确平台 selector 与已观测导航效果构造一次性内部证明。"""
    effect = str(action_entry.get("navigation_effect_reason") or "")
    url_changed = effect == "same_origin_url_change" and before_url != after_url
    activation_changed = effect == "activation_state_change" and before_url == after_url
    if (
        action_entry.get("action") != PageTriggerAction.CLICK.value
        or action_entry.get("navigation_effect_verified") is not True
        or not before_url
        or not after_url
        or not (url_changed or activation_changed)
    ):
        return None
    matched = h5_match_bottom_navigation_target(str(action_entry.get("selector") or ""))
    if matched is None:
        family = str(action_entry.get("navigation_family") or "").strip()
        index = action_entry.get("navigation_index")
        matched = (
            (family, index)
            if family == "adaptive_snapshot"
            and isinstance(index, int)
            and not isinstance(index, bool)
            and 1 <= index <= 50
            else None
        )
    if matched is None or matched[0] not in {
        "vant",
        "pages_foot",
        "footer_wrap",
        "adaptive_snapshot",
    }:
        return None
    before_identity = str(before_surface.get("identity_fingerprint") or "")
    after_identity = str(after_surface.get("identity_fingerprint") or "")
    if not before_identity or not after_identity:
        return None
    activation_before_hash = str(action_entry.get("activation_before_sha256") or "")
    activation_after_hash = str(action_entry.get("activation_after_sha256") or "")
    if activation_changed and (
        re.fullmatch(r"sha256:[0-9a-f]{64}", activation_before_hash) is None
        or re.fullmatch(r"sha256:[0-9a-f]{64}", activation_after_hash) is None
        or activation_before_hash == activation_after_hash
    ):
        return None
    return {
        "protocol_version": "bottom_navigation_transition_proof.v1",
        "status": "trusted",
        "effect": effect,
        "before_location": before_url,
        "after_location": after_url,
        "navigation_family": matched[0],
        "navigation_index": matched[1],
        **(
            {"interaction_pattern_signature": action_entry["interaction_pattern_signature"]}
            if matched[0] == "adaptive_snapshot"
            and isinstance(action_entry.get("interaction_pattern_signature"), str)
            else {}
        ),
        **(
            {
                "activation_before_sha256": activation_before_hash,
                "activation_after_sha256": activation_after_hash,
            }
            if activation_changed
            else {}
        ),
        "before_identity_fingerprint": before_identity,
        "after_identity_fingerprint": after_identity,
    }


def _action_target_fact(
    *,
    snapshot: dict[str, Any],
    selector: Any,
) -> dict[str, Any] | None:
    """保留动作目标的非敏感渲染事实，供 Judge 证明实际点击对象。"""
    normalized_selector = str(selector or "").strip()
    if not normalized_selector:
        return None
    elements = snapshot.get("elements")
    if not isinstance(elements, list):
        return None
    for item in elements:
        if not isinstance(item, dict) or item.get("selector") != normalized_selector:
            continue
        return {
            key: item.get(key)
            for key in (
                "selector",
                "tag",
                "role",
                "text",
                "aria_label",
                "aria_selected",
                "aria_current",
                "class_tokens",
                "rect",
                "href",
                "src",
                "alt",
                "external",
                "target",
                "rel",
                "type",
                "disabled",
                "checked",
            )
            if item.get(key) not in (None, "", [])
        }
    return None


def _ui_state_diff(
    *,
    before_snapshot: dict[str, Any],
    after_snapshot: dict[str, Any],
    prioritize_content_changes: bool = False,
) -> dict[str, Any]:
    """按稳定 selector 比较可见状态，不记录输入值或敏感字段。"""
    before = _snapshot_element_states(before_snapshot)
    after = _snapshot_element_states(after_snapshot)
    changes: list[dict[str, Any]] = []
    changed_fields: set[str] = set()
    selectors = sorted(set(before) | set(after))
    if prioritize_content_changes:
        # H5 先保留实际内容面板替换，避免轮播位移耗尽 50 项摘要。
        content_changes = {
            selector for selector in selectors
            if _page_tab_content_state_material(before.get(selector))
            != _page_tab_content_state_material(after.get(selector))
        }
        selectors.sort(key=lambda selector: selector not in content_changes)
    for selector in selectors:
        left = before.get(selector)
        right = after.get(selector)
        if left == right:
            continue
        fields = sorted(
            key
            for key in set(left or {}) | set(right or {})
            if (left or {}).get(key) != (right or {}).get(key)
        )
        changed_fields.update(fields)
        if len(changes) < 50:
            changes.append(
                {
                    "selector": selector,
                    "changed_fields": fields,
                    "before": left,
                    "after": right,
                }
            )
    return {
        "changed_fields": sorted(changed_fields),
        "change_count": sum(
            1
            for selector in set(before) | set(after)
            if before.get(selector) != after.get(selector)
        ),
        "changes": changes,
        "changes_truncated": len(changes) >= 50,
    }


def _snapshot_element_states(
    snapshot: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    elements = snapshot.get("elements")
    if not isinstance(elements, list):
        return {}
    states: dict[str, dict[str, Any]] = {}
    for item in elements:
        if not isinstance(item, dict):
            continue
        selector = str(item.get("selector") or "").strip()
        if not selector:
            continue
        media = item.get("media")
        media = media if isinstance(media, dict) else {}
        states[selector] = {
            "text": item.get("text"),
            "checked": item.get("checked"),
            "disabled": item.get("disabled"),
            "rect": item.get("rect"),
            "media_paused": media.get("paused"),
            "media_current_time": media.get("current_time"),
            "media_duration": media.get("duration"),
            "media_seeking": media.get("seeking"),
            "media_playback_rate": media.get("playback_rate"),
        }
    return states


def _encode_bytes(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")
