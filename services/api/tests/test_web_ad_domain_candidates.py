"""Web 广告 href 的域名候选筛选，不改共享出站与其他平台。"""

import pytest

from services.api.app.application.web_ad_landing_evidence import select_web_ad_candidate
from services.api.app.application.web_ad_outbound_policy import validate_ad_url


def _select(*urls):
    return select_web_ad_candidate(
        registered_domain="site.example",
        capture_snapshots=[
            (
                "capture-ready",
                {
                    "elements": [
                        {"href": url, "class_name": "popup-ad", "visible": True}
                        for url in urls
                    ]
                },
            )
        ],
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://23.225.52.86:4466/vip501.html",
        "https://23.225.52.68/ad",
        "https://23.225.52.68./ad",
        "https://[2606:4700:4700::1111]/ad",
        "https://[::ffff:23.225.52.86]/ad",
        "https://127.0.0.1/ad",
        "https://2130706433/ad",
        "https://127.1/ad",
        "https://0177.0.0.1/ad",
        "https://0x7f000001/ad",
        "https://0x7f.0.0.1/ad",
        "https://２３.２２５.５２.８６/ad",
    ],
)
def test_ip_ad_href_is_skipped_and_next_domain_candidate_is_selected(url):
    assert _select(url) is None
    selected = _select(url, "https://ads18.other.example:51212/ad?campaign=2#banner")
    assert selected is not None
    assert selected.url == "https://ads18.other.example:51212/ad?campaign=2"
    assert selected.source_capture_id == "capture-ready"


@pytest.mark.parametrize(
    "url",
    [
        "https://ads18.other.example/ad",
        "http://ads.other.example:4466/ad?campaign=2",
        "https://123.other.example/ad",
        "https://广告.示例.com/ad",
        "https://ADS.other.example./ad",
    ],
)
def test_normal_domain_ad_href_remains_eligible(url):
    selected = _select(url)
    assert selected is not None
    assert selected.url == url


def test_candidate_filter_does_not_change_shared_outbound_ip_policy():
    url = "https://23.225.52.86/ad"
    assert validate_ad_url(url) == url
    assert _select(url) is None
