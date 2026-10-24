"""规则测试按网址抓取：只抓公网 http(s)，结果按正式采集产物填回测试页。"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any

import pytest

from services.api.app.infrastructure import rule_sandbox_page_capture as capture_module
from services.api.app.infrastructure.rule_sandbox_page_capture import (
    RuleTestPageCaptureError,
    _address_is_public,
    _page_capture_from_result,
    _PublicAddressGuard,
    validate_capture_url,
)


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("file:///etc/passwd", "http:// 或 https://"),
        ("ftp://example.test/a", "http:// 或 https://"),
        ("https:///no-host", "域名或端口"),
        ("https://user:secret@example.test/", "账号密码"),
        ("https://example.test:99999/", "网址格式不对"),
    ],
)
def test_capture_url_must_be_plain_http_or_https(url: str, message: str) -> None:
    with pytest.raises(RuleTestPageCaptureError, match=message):
        validate_capture_url(url)
    assert validate_capture_url(" https://example.test/a?b=1 ") == "https://example.test/a?b=1"


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("93.184.216.34", True),
        # 开了代理软件的机器，域名都解析到 fake-ip 段
        ("198.18.0.190", True),
        ("10.0.0.8", False),
        ("172.17.0.2", False),
        ("192.168.1.10", False),
        ("127.0.0.1", False),
        ("169.254.169.254", False),
        ("100.64.0.1", False),
        ("0.0.0.0", False),
        ("::1", False),
        ("fc00::1", False),
        ("::ffff:10.0.0.1", False),
    ],
)
def test_only_public_addresses_are_reachable(address: str, public: bool) -> None:
    assert _address_is_public(address) is public


async def test_guard_blocks_internal_and_local_scheme_requests() -> None:
    guard = _PublicAddressGuard()

    assert await guard.allows("data:image/png;base64,AAAA") is True
    assert await guard.allows("file:///workspace/.env") is False
    assert await guard.allows("http://127.0.0.1:8000/health") is False
    assert await guard.allows("https://93.184.216.34/logo.png") is True
    assert guard.blocked_hosts == {"127.0.0.1"}


async def test_guard_aborts_blocked_routes_and_continues_public_ones() -> None:
    class FakeRoute:
        def __init__(self, url: str) -> None:
            self.request = type("Request", (), {"url": url})()
            self.outcome: str | None = None

        async def continue_(self) -> None:
            self.outcome = "continue"

        async def abort(self, error_code: str | None = None) -> None:
            self.outcome = f"abort:{error_code}"

    guard = _PublicAddressGuard()
    internal = FakeRoute("http://169.254.169.254/latest/meta-data/")
    public = FakeRoute("https://93.184.216.34/")

    await guard.handle(internal)
    await guard.handle(public)

    assert internal.outcome == "abort:blockedbyclient"
    assert public.outcome == "continue"


async def test_capture_refuses_internal_host_before_opening_a_browser(monkeypatch) -> None:
    async def resolves_to_private(hostname: str) -> bool:
        return False

    monkeypatch.setattr(capture_module, "_is_public_host", resolves_to_private)

    with pytest.raises(RuleTestPageCaptureError, match="内网"):
        await capture_module.capture_rule_test_page(
            url="http://alioth-postgres:5432/", platform="h5"
        )


@dataclass
class _Artifact:
    artifact_type: str
    content: Any
    artifact_id: str | None = None


@dataclass
class _Result:
    artifacts: list[_Artifact]
    final_url: str | None = "https://example.test/final"
    title: str | None = "标题"
    status_code: int | None = 200
    execution_status: str = "success"
    error_message: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)


def _png(tag: bytes) -> dict[str, str]:
    return {"encoding": "base64", "data": base64.b64encode(tag).decode("ascii")}


def test_result_uses_formal_artifacts_and_last_frame_of_a_changing_page() -> None:
    guard = _PublicAddressGuard()
    guard.blocked_hosts.add("10.0.0.8")
    result = _Result(
        artifacts=[
            _Artifact("dom", "<html>源码</html>"),
            _Artifact("visible_text", "5秒后关闭"),
            _Artifact(
                "screenshot",
                {
                    "sequence": True,
                    "frames": [{"index": 0, **_png(b"f0")}, {"index": 1, **_png(b"f1")}],
                },
            ),
            _Artifact(
                "ui_snapshot",
                {
                    "schema_version": "ui-snapshot-sequence.v1",
                    "sequence": True,
                    "frames": [
                        {"index": 0, "schema_version": "ui-snapshot.v2", "elements": [1]},
                        {"index": 1, "schema_version": "ui-snapshot.v2", "elements": [1, 2]},
                    ],
                },
            ),
        ],
        status_code=404,
        metrics={"page_settle_complete": False},
    )

    page = _page_capture_from_result(result, requested_url="https://example.test/a", guard=guard)

    assert page.final_url == "https://example.test/final"
    assert page.dom_html == "<html>源码</html>"
    assert page.visible_text == "5秒后关闭"
    assert page.screenshot_png == b"f1"
    assert page.ui_snapshot == {"schema_version": "ui-snapshot.v2", "elements": [1, 2]}
    assert any("取最后一帧" in notice for notice in page.notices)
    assert any("404" in notice for notice in page.notices)
    assert any("没加载稳" in notice for notice in page.notices)
    assert any("10.0.0.8" in notice for notice in page.notices)


def test_result_without_optional_artifacts_stays_empty() -> None:
    page = _page_capture_from_result(
        _Result(artifacts=[_Artifact("screenshot", _png(b"shot"))], final_url=None, title=None),
        requested_url="https://example.test/a",
        guard=_PublicAddressGuard(),
    )

    assert page.final_url == "https://example.test/a"
    assert page.title == ""
    assert page.visible_text == ""
    assert page.dom_html == ""
    assert page.ui_snapshot is None
    assert page.screenshot_png == b"shot"
    assert page.notices == ()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("5s(跳过)" + chr(10) + "线路读取完成，请稍后", True),
        ("跳过 3", True),
        ("跳过广告", True),
        ("正在获取最佳线路...", True),
        ("", False),
        # 正文页哪怕带「跳过」二字，文字量也远超开屏页
        ("首页 热门 影片 " + "内容" * 200 + " 跳过", False),
        ("首页 热门 影片 短视频 社区 我的 立即充值 月卡 ¥30 季卡 ¥80", False),
    ],
)
def test_startup_layer_detection_uses_countdown_and_loading_markers(
    text: str, expected: bool
) -> None:
    assert capture_module.looks_like_startup_layer(text) is expected


def test_result_warns_when_the_frame_is_still_a_startup_layer() -> None:
    """抓到的还是开屏 / 线路加载层：要大声说出来，不能让 15 条规则默默按开屏页判。"""
    result = _Result(
        artifacts=[
            _Artifact("visible_text", "5s(跳过)" + chr(10) + "线路读取完成，请稍后"),
            _Artifact("screenshot", _png(b"splash")),
        ]
    )

    page = _page_capture_from_result(
        result, requested_url="https://example.test/movie", guard=_PublicAddressGuard()
    )

    assert page.screenshot_png == b"splash"
    assert capture_module.STARTUP_LAYER_NOTICE in page.notices
    assert any("不是目标页面" in notice for notice in page.notices)


class _FakeBrowser:
    def __init__(self) -> None:
        self.closed_sessions: list[str] = []

    async def close_session(self, session_id: str) -> None:
        self.closed_sessions.append(session_id)


def _splash(text: str = "5s(跳过)" + chr(10) + "线路读取完成，请稍后") -> _Result:
    return _Result(
        artifacts=[
            _Artifact("visible_text", text),
            _Artifact("dom", "<div class=loading-page><span class=down-time>5s(跳过)</span></div>"),
            _Artifact("screenshot", _png(b"splash")),
        ]
    )


def _home() -> _Result:
    return _Result(
        final_url="https://example.test/movie",
        artifacts=[
            _Artifact("visible_text", "首页 热门 影片 短视频 社区 我的 最新 热播 福利姬 原创 " * 3),
            _Artifact("dom", "<html>home</html>"),
            _Artifact("screenshot", _png(b"home")),
        ],
    )


def _wire_capture(monkeypatch, results: list[_Result], *, selector: str | None):
    """把采集换成按顺序吐结果的假实现，记录每次请求的模式 / 会话 / 选择器。"""
    from services.api.app.infrastructure.browser import page_capture as page_capture_module

    browser = _FakeBrowser()
    requests: list[Any] = []
    queue = list(results)

    async def fake_capture(*, request, browser, timeout_seconds):  # noqa: ARG001
        requests.append(request)
        return queue.pop(0)

    async def public(_hostname: str) -> bool:
        return True

    monkeypatch.setattr(page_capture_module, "capture_page_with_timeout", fake_capture)
    monkeypatch.setattr(capture_module, "_h5_capture_browser", lambda guard: browser)  # noqa: ARG005
    monkeypatch.setattr(capture_module, "_is_public_host", public)
    monkeypatch.setattr(capture_module, "STARTUP_LAYER_AUTO_DISMISS_WAIT_SECONDS", 0)
    monkeypatch.setattr(capture_module, "_resolve_startup_dismissal_selector", lambda dom: selector)
    return browser, requests


async def test_startup_layer_is_skipped_inside_one_session(monkeypatch) -> None:
    """第一帧是开屏层：等一等再看一眼就进去了——同一个会话，抓到的是进入后的页面。"""
    browser, requests = _wire_capture(monkeypatch, [_splash(), _home()], selector=None)

    page = await capture_module.capture_rule_test_page(
        url="https://example.test/movie", platform="h5"
    )

    assert [str(r.mode) for r in requests] == ["goto", "observe"]
    assert len({r.session_id for r in requests}) == 1 and requests[0].session_id
    assert page.screenshot_png == b"home"
    assert capture_module.STARTUP_LAYER_DISMISSED_NOTICE in page.notices
    assert capture_module.STARTUP_LAYER_NOTICE not in page.notices
    assert browser.closed_sessions == [requests[0].session_id]


async def test_startup_layer_is_clicked_away_with_the_formal_dismissal_control(monkeypatch) -> None:
    """等完还在开屏层：用正式采集认出的「跳过」控件点一下，返回点完后的页面。"""
    browser, requests = _wire_capture(
        monkeypatch, [_splash(), _splash(), _home()], selector=".loading-page .down-time:visible"
    )

    page = await capture_module.capture_rule_test_page(
        url="https://example.test/movie", platform="h5"
    )

    assert [str(r.mode) for r in requests] == ["goto", "observe", "single_trigger"]
    assert requests[2].selector == ".loading-page .down-time:visible"
    assert str(requests[2].trigger_action) == "click"
    assert requests[2].url is None  # 同一会话里点，不重新导航
    assert page.screenshot_png == b"home"
    assert capture_module.STARTUP_LAYER_DISMISSED_NOTICE in page.notices
    assert browser.closed_sessions == [requests[0].session_id]


async def test_startup_layer_that_will_not_go_away_is_returned_with_a_clear_notice(
    monkeypatch,
) -> None:
    browser, requests = _wire_capture(
        monkeypatch, [_splash(), _splash(), _splash()], selector=".loading-page .down-time:visible"
    )

    page = await capture_module.capture_rule_test_page(
        url="https://example.test/movie", platform="h5"
    )

    assert len(requests) == 3
    assert page.screenshot_png == b"splash"
    assert capture_module.STARTUP_LAYER_DISMISS_FAILED_NOTICE in page.notices
    assert capture_module.STARTUP_LAYER_NOTICE not in page.notices
    assert browser.closed_sessions == [requests[0].session_id]


async def test_startup_ad_rules_keep_the_startup_frame_without_warning(monkeypatch) -> None:
    """查开屏广告本身的规则：抓到开屏层就是要的那一帧，不跳过、也不提示「不是目标页面」。"""
    browser, requests = _wire_capture(
        monkeypatch, [_splash()], selector=".loading-page .down-time:visible"
    )

    page = await capture_module.capture_rule_test_page(
        url="https://example.test/", platform="h5", keep_startup_layer=True
    )

    assert [str(r.mode) for r in requests] == ["goto"]
    assert page.screenshot_png == b"splash"
    assert page.notices == ()
    assert browser.closed_sessions == [requests[0].session_id]


async def test_normal_page_is_captured_once_and_session_closed(monkeypatch) -> None:
    browser, requests = _wire_capture(monkeypatch, [_home()], selector=None)

    page = await capture_module.capture_rule_test_page(
        url="https://example.test/movie", platform="h5"
    )

    assert [str(r.mode) for r in requests] == ["goto"]
    assert page.notices == ()
    assert browser.closed_sessions == [requests[0].session_id]


def _popup_wall() -> _Result:
    return _Result(
        final_url="https://example.test/movie",
        artifacts=[
            _Artifact("visible_text", "关闭"),
            _Artifact("dom", "<div class=ad><span>关闭</span></div>"),
            _Artifact("screenshot", _png(b"popup")),
        ],
    )


async def test_entry_popups_are_closed_in_the_same_session(monkeypatch) -> None:
    """开屏进去之后被弹窗广告盖着：接着点「关闭」，抓到的是关掉后的首页。"""
    browser, requests = _wire_capture(
        monkeypatch, [_splash(), _popup_wall(), _popup_wall(), _home()], selector=None
    )

    page = await capture_module.capture_rule_test_page(
        url="https://example.test/movie", platform="h5"
    )

    assert [str(r.mode) for r in requests] == [
        "goto",
        "observe",
        "single_trigger",
        "single_trigger",
    ]
    assert {r.selector for r in requests[2:]} == {capture_module.ENTRY_POPUP_DISMISSAL_SELECTOR}
    assert page.screenshot_png == b"home"
    assert capture_module.STARTUP_LAYER_DISMISSED_NOTICE in page.notices
    assert capture_module.ENTRY_POPUP_DISMISSED_NOTICE.format(count=2) in page.notices
    assert browser.closed_sessions == [requests[0].session_id]


async def test_entry_popup_without_startup_layer_is_closed_too(monkeypatch) -> None:
    browser, requests = _wire_capture(monkeypatch, [_popup_wall(), _home()], selector=None)

    page = await capture_module.capture_rule_test_page(
        url="https://example.test/movie", platform="h5"
    )

    assert [str(r.mode) for r in requests] == ["goto", "single_trigger"]
    assert page.screenshot_png == b"home"
    assert page.notices == (capture_module.ENTRY_POPUP_DISMISSED_NOTICE.format(count=1),)


async def test_entry_popups_that_keep_coming_stop_after_the_limit(monkeypatch) -> None:
    browser, requests = _wire_capture(monkeypatch, [_popup_wall()] * 5, selector=None)

    page = await capture_module.capture_rule_test_page(
        url="https://example.test/movie", platform="h5"
    )

    assert len(requests) == 1 + capture_module.ENTRY_POPUP_MAX_DISMISSALS
    assert page.screenshot_png == b"popup"
    assert capture_module.ENTRY_POPUP_REMAINS_NOTICE in page.notices


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("关闭", True),
        ("我知道了", True),
        ("×", True),
        ("首页 热门 影片 短视频 社区 我的 关闭 " + "内容" * 40, False),
        ("", False),
    ],
)
def test_popup_wall_detection(text: str, expected: bool) -> None:
    assert capture_module.looks_like_popup_wall(text) is expected


async def test_base_page_still_covered_after_clicks_is_reported(monkeypatch) -> None:
    """进入后被只有图标叉号的弹窗盖着：文字看不出来，但快照的展示层判定看得出来——关不掉就明说。"""
    grid = _Result(
        final_url="https://example.test/movie",
        artifacts=[
            _Artifact(
                "visible_text",
                "同城上门 春药商场 新葡京 开元棋牌 PG电子 PG游戏 PG官方 PG娱乐 赏金女王",
            ),
            _Artifact("ui_snapshot", {"schema_version": "ui-snapshot.v2", "elements": [1]}),
            _Artifact("screenshot", _png(b"grid")),
        ],
    )
    failed_click = _Result(
        artifacts=[], execution_status="failed", error_message="没有找到「关闭」"
    )
    browser, requests = _wire_capture(monkeypatch, [grid, failed_click], selector=None)
    monkeypatch.setattr(
        capture_module, "_base_page_occluded", lambda snapshot: snapshot is not None
    )

    page = await capture_module.capture_rule_test_page(
        url="https://example.test/movie", platform="h5"
    )

    assert [str(r.mode) for r in requests] == ["goto", "single_trigger"]
    assert page.screenshot_png == b"grid"
    assert capture_module.ENTRY_POPUP_REMAINS_NOTICE in page.notices
    assert browser.closed_sessions == [requests[0].session_id]
