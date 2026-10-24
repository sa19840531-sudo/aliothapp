"""引导页候选只从可信 Web 首屏快照签发。"""

from services.api.app.application.web_guide_candidate import select_web_guide_candidates


def test_visible_app_entry_is_candidate_without_reclassifying_source() -> None:
    candidates = select_web_guide_candidates(
        source_capture_id="source-1",
        source_url="https://site.example/",
        snapshot={"elements": [
            {"tag": "a", "visible": True, "text": "官方 APP", "href": "/app.html"},
            {"tag": "a", "visible": True, "text": "首页", "href": "/"},
        ]},
    )
    assert len(candidates) == 1
    assert candidates[0].destination_url == "https://site.example/app.html"
    assert candidates[0].kind == "page_candidate"
    assert candidates[0].source_capture_id == "source-1"


def test_direct_apk_is_resource_and_generic_footer_is_not_candidate() -> None:
    candidates = select_web_guide_candidates(
        source_capture_id="source-1",
        source_url="https://site.example/",
        snapshot={"elements": [
            {"tag": "a", "visible": True, "text": "下载", "href": "/download"},
            {"tag": "a", "visible": True, "text": "APP 下载", "href": "https://cdn.example/app.apk"},
            {"tag": "a", "visible": False, "text": "官方 APP", "href": "/hidden"},
        ]},
    )
    assert len(candidates) == 1
    assert candidates[0].kind == "download_resource"


def test_unsafe_urls_are_rejected_and_duplicates_are_collapsed() -> None:
    candidates = select_web_guide_candidates(
        source_capture_id="source-1",
        source_url="https://site.example/",
        snapshot={"elements": [
            {"tag": "a", "visible": True, "text": "官方 APP", "href": "http://127.0.0.1/app"},
            {"tag": "a", "visible": True, "text": "官方 APP", "href": "javascript:alert(1)"},
            {"tag": "a", "visible": True, "text": "官方 APP", "href": "/app"},
            {"tag": "a", "visible": True, "text": "APP 下载", "href": "/app#download"},
        ]},
    )
    assert len(candidates) == 1
    assert candidates[0].destination_url == "https://site.example/app"


def test_real_snapshot_class_tokens_discover_app_icon_but_exclude_ad_banner() -> None:
    candidates = select_web_guide_candidates(
        source_capture_id="source-1", source_url="https://site.example/",
        snapshot={"elements": [
            {"tag": "a", "visible": True, "text": "", "class_tokens": ["app-download"],
             "href": "/app.html"},
            {"tag": "a", "visible": True, "text": "APP 下载", "class_tokens": ["ad-banner"],
             "href": "/campaign"},
        ]},
    )
    assert [item.destination_url for item in candidates] == ["https://site.example/app.html"]


def test_direct_packages_do_not_displace_guide_landing_candidate() -> None:
    candidates = select_web_guide_candidates(
        source_capture_id="source-1", source_url="https://site.example/",
        snapshot={"elements": [
            {"visible": True, "text": "APP 下载", "href": "/client-a.apk"},
            {"visible": True, "text": "APP 下载", "href": "/client-b.apk"},
            {"visible": True, "text": "APP 下载说明", "href": "/app.html"},
        ]},
    )
    assert candidates[0].destination_url == "https://site.example/app.html"
