"""Web 阻断候选的密封身份和可点击过滤合同。"""

import json

import pytest

from services.api.app.domain.exploration.web_exploration import (
    build_web_obstruction_candidates,
    web_obstruction_state_fingerprint,
)
from services.api.app.infrastructure.browser.page_capture import (
    _collect_ui_snapshot,
)
from services.api.tests.web_browser_test_support import (
    WebBrowserUnavailable,
    launch_web_test_browser,
)


def _snapshot(*, z_index: int, hit_test: bool = True) -> dict[str, object]:
    return {
        "schema_version": "ui-snapshot.v2",
        "elements": [
            {
                "selector": "#adult-confirm",
                "tag": "button",
                "role": "button",
                "text": "我已年满18岁并进入",
                "class_tokens": ["confirm"],
                "ancestor_class_tokens": ["age-gate"],
                "web_obstruction_layer": "#entry-age-gate",
                "web_obstruction_layer_kind": "age_gate",
                "web_obstruction_layer_structural": True,
                "web_obstruction_close_semantics": True,
                "web_obstruction_close_basis": "age_gate_confirmation",
                "web_obstruction_close_control_unique": True,
                "web_obstruction_close_hit_safe": True,
                "web_human_verification_present": False,
                "within_web_navigation": False,
                "rect": {"x": 760, "y": 600, "width": 400, "height": 60},
                "interactive": True,
                "visible": True,
                "effective_z_index": z_index,
                "hit_test": {"center_hits_target": hit_test},
            }
        ],
    }


def test_high_effective_z_candidate_gets_structure_bound_sealed_id() -> None:
    candidates = build_web_obstruction_candidates(
        capture_id="capture-entry-1",
        ui_snapshot=_snapshot(z_index=900),
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.effective_z_index == 900
    assert candidate.candidate_id.startswith("web-candidate:")
    assert candidate.capability == "web_age_gate_confirm"
    assert candidate.selector == "#adult-confirm"
    assert "selector" not in candidate.to_public_dict()


def test_candidate_id_survives_fresh_capture_but_changes_with_geometry() -> None:
    first = build_web_obstruction_candidates(
        capture_id="capture-entry-1",
        ui_snapshot=_snapshot(z_index=900),
    )[0]
    second = build_web_obstruction_candidates(
        capture_id="capture-entry-2",
        ui_snapshot=_snapshot(z_index=900),
    )[0]
    moved_snapshot = _snapshot(z_index=900)
    moved_snapshot["elements"][0]["rect"]["x"] = 761
    moved = build_web_obstruction_candidates(
        capture_id="capture-entry-1",
        ui_snapshot=moved_snapshot,
    )[0]

    assert first.candidate_id == second.candidate_id
    assert moved.candidate_id != first.candidate_id


def test_obstruction_state_fingerprint_tracks_layer_content_not_capture_time() -> None:
    first = _snapshot(z_index=900)
    first["captured_at"] = "2026-09-03T10:00:00Z"
    first["elements"][0]["src"] = "https://ads.example/creative-a.png"
    same = json.loads(json.dumps(first))
    same["captured_at"] = "2026-09-03T10:00:10Z"
    rotated = json.loads(json.dumps(same))
    rotated["elements"][0]["src"] = "https://ads.example/creative-b.png"

    first_fingerprint = web_obstruction_state_fingerprint(first)

    assert first_fingerprint is not None
    assert web_obstruction_state_fingerprint(same) == first_fingerprint
    assert web_obstruction_state_fingerprint(rotated) != first_fingerprint


def test_obstruction_state_fingerprint_tracks_hashed_creative_inside_layer() -> None:
    """同一弹层的创意轮换由脱敏资源摘要区分，顺序和重复项不影响身份。"""

    first = _snapshot(z_index=900)
    first["elements"][0]["web_obstruction_layer"] = "#popup"
    resource = {
        "selector": "#popup > div > img",
        "attribute": "src",
        "scheme": "data",
        "mime": "image/gif",
        "byte_length": 100,
        "sha256": "a" * 64,
    }
    first["resource_summaries"] = [resource, dict(resource)]
    same = json.loads(json.dumps(first))
    same["resource_summaries"].reverse()
    rotated = json.loads(json.dumps(first))
    rotated["resource_summaries"][0]["sha256"] = "b" * 64
    rotated["resource_summaries"][1]["sha256"] = "b" * 64
    outside = json.loads(json.dumps(first))
    outside["resource_summaries"].append(
        {
            **resource,
            "selector": "#unrelated-banner > img",
            "sha256": "c" * 64,
        }
    )

    first_fingerprint = web_obstruction_state_fingerprint(first)

    assert first_fingerprint is not None
    assert web_obstruction_state_fingerprint(same) == first_fingerprint
    assert web_obstruction_state_fingerprint(outside) == first_fingerprint
    assert web_obstruction_state_fingerprint(rotated) != first_fingerprint


def test_obstruction_resource_fingerprint_is_bounded_and_ignores_raw_url_fields() -> None:
    """阻断资源事实最多取稳定排序后的 32 项，且原始 URL 不进入身份。"""

    snapshot = _snapshot(z_index=900)
    snapshot["elements"][0]["web_obstruction_layer"] = "#popup"
    summaries = [
        {
            "selector": f"#popup > img[data-index='{index:02d}']",
            "attribute": "src",
            "sha256": f"{index:064x}",
            "raw_url": f"https://sensitive.invalid/{index}",
        }
        for index in range(40)
    ]
    snapshot["resource_summaries"] = summaries
    capped = json.loads(json.dumps(snapshot))
    capped["resource_summaries"] = summaries[:32]
    changed_raw_url = json.loads(json.dumps(snapshot))
    changed_raw_url["resource_summaries"][0]["raw_url"] = (
        "https://another-sensitive.invalid/value"
    )

    fingerprint = web_obstruction_state_fingerprint(snapshot)

    assert fingerprint == web_obstruction_state_fingerprint(capped)
    assert fingerprint == web_obstruction_state_fingerprint(changed_raw_url)


def test_structural_age_gate_does_not_depend_on_z_index_but_requires_hit_test() -> None:
    assert len(build_web_obstruction_candidates(
        capture_id="capture-entry-1",
        ui_snapshot=_snapshot(z_index=100),
    )) == 1
    assert build_web_obstruction_candidates(
        capture_id="capture-entry-1",
        ui_snapshot=_snapshot(z_index=900, hit_test=False),
    ) == ()


def test_high_z_standalone_close_without_structural_layer_is_rejected() -> None:
    snapshot = _snapshot(z_index=5000)
    snapshot["elements"][0].update(
        {
            "selector": "#floating-toolbar-close",
            "text": "关闭工具条",
            "class_tokens": ["close"],
            "ancestor_class_tokens": ["floating-toolbar"],
            "web_obstruction_layer": None,
            "web_obstruction_layer_kind": None,
            "web_obstruction_layer_structural": False,
            "web_obstruction_close_semantics": True,
        }
    )

    assert build_web_obstruction_candidates(
        capture_id="capture-entry-1",
        ui_snapshot=snapshot,
    ) == ()


def test_delegated_popup_close_wrapper_is_deduplicated_and_safe() -> None:
    snapshot = {
        "schema_version": "ui-snapshot.v2",
        "elements": [
            {
                "selector": "#advertise-1 > div > div.close-img",
                "tag": "div",
                "role": None,
                "text": "",
                "class_tokens": ["close-img", "on-popup-close"],
                "ancestor_class_tokens": ["popup-window", "van-overlay"],
                "web_obstruction_layer": "#popup-window",
                "web_obstruction_layer_kind": "popup",
                "web_obstruction_layer_structural": True,
                "web_obstruction_close_semantics": True,
                "web_obstruction_close_basis": "delegated_wrapper_self",
                "web_obstruction_close_control_unique": True,
                "web_obstruction_close_hit_safe": True,
                "web_human_verification_present": False,
                "rect": {"x": 945, "y": 150, "width": 30, "height": 30},
                "interactive": True,
                "visible": True,
                "effective_z_index": 2000,
                "hit_test": {"center_hits_target": True},
            },
            {
                "selector": "#advertise-1 img.close-image",
                "tag": "img",
                "role": None,
                "text": "",
                "class_tokens": ["close-image"],
                "ancestor_class_tokens": ["close-img", "on-popup-close"],
                "web_obstruction_layer": "#popup-window",
                "web_obstruction_layer_kind": "popup",
                "web_obstruction_layer_structural": True,
                "web_obstruction_close_semantics": True,
                "web_obstruction_close_basis": "own_close_token",
                "web_obstruction_close_control_unique": True,
                "web_obstruction_close_hit_safe": True,
                "web_human_verification_present": False,
                "rect": {"x": 945, "y": 150, "width": 30, "height": 30},
                "interactive": True,
                "visible": True,
                "effective_z_index": 2000,
                "hit_test": {"center_hits_target": True},
            },
        ],
    }

    candidates = build_web_obstruction_candidates(
        capture_id="capture-entry-1",
        ui_snapshot=snapshot,
    )

    assert len(candidates) == 1
    assert candidates[0].selector.endswith("div.close-img")
    assert candidates[0].capability == "web_obstruction_dismiss"


@pytest.mark.parametrize(
    ("control_unique", "center_hits_target"),
    ((False, True), (True, False)),
)
def test_popup_window_close_requires_unique_and_hit_safe_control(
    control_unique: bool,
    center_hits_target: bool,
) -> None:
    snapshot = {
        "elements": [
            {
                "selector": "#popup-window .close-img",
                "tag": "div",
                "role": None,
                "text": "",
                "class_tokens": ["close-img", "on-popup-close"],
                "ancestor_class_tokens": ["popup-window"],
                "web_obstruction_layer": "#popup-window",
                "web_obstruction_layer_kind": "popup",
                "web_obstruction_layer_structural": True,
                "web_obstruction_close_semantics": True,
                "web_obstruction_close_basis": "delegated_wrapper_self",
                "web_obstruction_close_control_unique": control_unique,
                "web_obstruction_close_hit_safe": center_hits_target,
                "web_human_verification_present": False,
                "within_web_navigation": False,
                "rect": {"x": 945, "y": 150, "width": 30, "height": 30},
                "interactive": True,
                "visible": True,
                "effective_z_index": 6,
                "hit_test": {"center_hits_target": center_hits_target},
            }
        ]
    }

    assert build_web_obstruction_candidates(
        capture_id="capture-popup-window",
        ui_snapshot=snapshot,
    ) == ()

def test_low_z_dialog_close_wins_over_high_z_navigation() -> None:
    snapshot = {
        "schema_version": "ui-snapshot.v2",
        "elements": [
            {
                "selector": ".van-popup[role='dialog'] .close-btn",
                "tag": "div",
                "role": "button",
                "text": "",
                "class_tokens": ["close-btn"],
                "ancestor_class_tokens": ["van-popup", "van-popup--center"],
                "web_obstruction_layer": ".van-popup[role='dialog']",
                "web_obstruction_layer_kind": "dialog",
                "web_obstruction_layer_structural": True,
                "web_obstruction_close_semantics": True,
                "web_obstruction_close_basis": "own_close_token",
                "web_obstruction_close_control_unique": True,
                "web_obstruction_close_hit_safe": True,
                "web_human_verification_present": False,
                "rect": {"x": 1430, "y": 145, "width": 42, "height": 42},
                "interactive": False,
                "visible": True,
                "effective_z_index": 100,
                "hit_test": {"center_hits_target": True},
            },
            {
                "selector": "#site-header a.login",
                "tag": "a",
                "role": "link",
                "text": "登录",
                "class_tokens": ["login"],
                "ancestor_class_tokens": ["site-header", "navbar-nav"],
                "web_obstruction_layer": "#site-header",
                "web_obstruction_layer_kind": "navigation",
                "web_obstruction_layer_structural": False,
                "web_obstruction_close_semantics": False,
                "within_web_navigation": True,
                "rect": {"x": 1730, "y": 20, "width": 80, "height": 40},
                "interactive": True,
                "visible": True,
                "effective_z_index": 1900,
                "hit_test": {"center_hits_target": True},
            },
        ],
    }

    candidates = build_web_obstruction_candidates(
        capture_id="capture-entry-1",
        ui_snapshot=snapshot,
    )

    assert len(candidates) == 1
    assert candidates[0].selector == ".van-popup[role='dialog'] .close-btn"
    assert candidates[0].effective_z_index == 100
    assert candidates[0].capability == "web_obstruction_dismiss"


def test_registration_adult_consent_is_not_an_entry_age_gate_candidate() -> None:
    snapshot = _snapshot(z_index=900)
    element = snapshot["elements"][0]
    element.update(
        {
            "selector": "#register-form label.adult-consent",
            "tag": "label",
            "text": "我确认已经在18岁以上并同意用户条款",
            "class_tokens": ["adult-consent"],
            "ancestor_class_tokens": ["login-modal", "register-form"],
            "web_obstruction_layer": "#register-form",
            "web_obstruction_layer_kind": "age_gate",
            "web_obstruction_close_semantics": True,
        }
    )

    candidates = build_web_obstruction_candidates(
        capture_id="capture-entry-1",
        ui_snapshot=snapshot,
    )

    assert candidates == ()


def test_generic_adult_continue_with_password_sibling_is_not_entry_gate() -> None:
    snapshot = _snapshot(z_index=900)
    snapshot["elements"][0].update(
        {
            "selector": "#account-panel label.consent",
            "text": "我已满18岁并继续",
            "class_tokens": ["consent"],
            "ancestor_class_tokens": ["account-panel"],
            "web_obstruction_layer": "#account-panel",
            "web_obstruction_layer_kind": "age_gate",
            "web_obstruction_close_semantics": True,
        }
    )
    snapshot["elements"].append(
        {
            "selector": "#account-panel input[name='password']",
            "tag": "input",
            "role": None,
            "text": "",
            "placeholder": "请输入密码",
            "type": "password",
            "class_tokens": ["field"],
            "ancestor_class_tokens": ["account-panel"],
            "web_obstruction_layer": "#account-panel",
            "rect": {"x": 760, "y": 500, "width": 400, "height": 48},
            "interactive": True,
            "visible": True,
            "effective_z_index": 900,
            "hit_test": {"center_hits_target": True},
        }
    )

    candidates = build_web_obstruction_candidates(
        capture_id="capture-entry-1",
        ui_snapshot=snapshot,
    )

    assert all(item.text != "我已满18岁并继续" for item in candidates)


def test_low_z_registration_sibling_still_suppresses_age_entry_candidate() -> None:
    snapshot = _snapshot(z_index=100)
    snapshot["elements"][0].update(
        {
            "selector": "#account-panel button.consent",
            "text": "我已满18岁并继续",
            "ancestor_class_tokens": ["account-panel"],
            "web_obstruction_layer": "#account-panel",
        }
    )
    snapshot["elements"].append(
        {
            "selector": "#account-panel input[name='password']",
            "tag": "input",
            "text": "",
            "type": "password",
            "ancestor_class_tokens": ["account-panel"],
            "web_obstruction_layer": "#account-panel",
            "rect": {"x": 760, "y": 500, "width": 400, "height": 48},
            "interactive": True,
            "visible": True,
            "effective_z_index": 100,
            "hit_test": {"center_hits_target": True},
        }
    )

    assert build_web_obstruction_candidates(
        capture_id="capture-entry-1",
        ui_snapshot=snapshot,
    ) == ()


def test_real_age_gate_survives_unrelated_high_z_login_navigation() -> None:
    snapshot = _snapshot(z_index=900)
    snapshot["elements"][0]["web_obstruction_layer"] = "#entry-age-gate"
    snapshot["elements"].append(
        {
            "selector": "#fixed-login-nav button",
            "tag": "button",
            "role": "button",
            "text": "登录",
            "class_tokens": ["login-button"],
            "ancestor_class_tokens": ["fixed-login-nav"],
            "web_obstruction_layer": "#fixed-login-nav",
            "rect": {"x": 1700, "y": 20, "width": 120, "height": 48},
            "interactive": True,
            "visible": True,
            "effective_z_index": 1000,
            "hit_test": {"center_hits_target": True},
        }
    )

    candidates = build_web_obstruction_candidates(
        capture_id="capture-entry-1",
        ui_snapshot=snapshot,
    )

    age_gate = next(item for item in candidates if item.text == "我已年满18岁并进入")
    assert age_gate.capability == "web_age_gate_confirm"


@pytest.mark.asyncio
async def test_production_snapshot_suppresses_iframe_captcha_layer() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>
      #captcha-dialog { position:fixed; inset:120px 420px; }
      #real-close { position:absolute; top:8px; right:8px; width:36px; height:36px;
                    cursor:pointer; }
      iframe { display:block; width:420px; height:180px; margin:100px auto; }
    </style>
    <section id="captcha-dialog" class="popup" role="dialog">
      <div id="real-close" class="on-dialog-close"></div>
      <iframe title="reCAPTCHA challenge" src="about:blank?recaptcha"></iframe>
    </section>
    """
    try:
        async with async_playwright() as playwright:
            browser = await launch_web_test_browser(playwright.chromium, headless=True)
            page = await browser.new_page(viewport={"width": 1920, "height": 1080})
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(
                page,
                web_detail_candidate_priority=True,
            )
            await browser.close()
    except WebBrowserUnavailable as exc:
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    close_element = next(
        item for item in snapshot["elements"] if item.get("selector") == "#real-close"
    )
    assert close_element["web_human_verification_present"] is True
    assert build_web_obstruction_candidates(
        capture_id="capture-captcha-dialog",
        ui_snapshot=snapshot,
    ) == ()


@pytest.mark.asyncio
async def test_production_snapshot_rejects_delegated_empty_link_descendant() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>
      #popup { position:fixed; inset:120px 420px; }
      #unsafe-shell { position:absolute; left:40px; top:40px; width:120px; height:50px;
                      cursor:pointer; }
      #unsafe-link { display:block; width:120px; height:50px; }
      #real-close { position:absolute; top:8px; right:8px; width:36px; height:36px;
                    cursor:pointer; }
    </style>
    <section id="popup" class="popup" role="dialog">
      <div id="unsafe-shell" class="on-popup-close">
        <a id="unsafe-link" href="/advertisement"></a>
      </div>
      <div id="real-close" class="on-dialog-close"></div>
    </section>
    """
    try:
        async with async_playwright() as playwright:
            browser = await launch_web_test_browser(playwright.chromium, headless=True)
            page = await browser.new_page(viewport={"width": 1920, "height": 1080})
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(
                page,
                web_detail_candidate_priority=True,
            )
            await browser.close()
    except WebBrowserUnavailable as exc:
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    by_selector = {item.get("selector"): item for item in snapshot["elements"]}
    assert by_selector["#unsafe-link"]["web_obstruction_close_basis"] == (
        "delegated_wrapper_descendant"
    )
    assert by_selector["#unsafe-link"]["web_obstruction_close_semantics"] is False
    assert by_selector["#unsafe-shell"]["web_obstruction_close_hit_safe"] is False
    assert by_selector["#real-close"]["web_obstruction_close_basis"] == (
        "delegated_wrapper_self"
    )
    assert by_selector["#real-close"]["web_obstruction_close_control_unique"] is True
    assert by_selector["#real-close"]["web_obstruction_close_hit_safe"] is True

    candidates = build_web_obstruction_candidates(
        capture_id="capture-delegated-link",
        ui_snapshot=snapshot,
    )
    assert tuple(item.selector for item in candidates) == ("#real-close",)


@pytest.mark.asyncio
async def test_production_snapshot_rejects_non_close_token_substrings() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    snapshots = []
    try:
        async with async_playwright() as playwright:
            browser = await launch_web_test_browser(playwright.chromium, headless=True)
            for class_name in ("disclose-payment", "enclosed-offer"):
                page = await browser.new_page(viewport={"width": 1920, "height": 1080})
                await page.set_content(
                    f"""
                    <style>
                      #dialog {{ position:fixed; inset:120px 420px; }}
                      #business-action {{ width:180px; height:60px; cursor:pointer; }}
                    </style>
                    <section id="dialog" class="popup" role="dialog">
                      <button id="business-action" class="{class_name}"></button>
                    </section>
                    """
                )
                snapshot = await _collect_ui_snapshot(
                    page,
                    web_detail_candidate_priority=True,
                )
                await page.close()
                snapshots.append((class_name, snapshot))
            await browser.close()
    except WebBrowserUnavailable as exc:
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    for class_name, snapshot in snapshots:
        action = next(
            item
            for item in snapshot["elements"]
            if item.get("selector") == "#business-action"
        )
        assert action["web_obstruction_close_basis"] is None
        assert build_web_obstruction_candidates(
            capture_id=f"capture-{class_name}",
            ui_snapshot=snapshot,
        ) == ()


@pytest.mark.asyncio
async def test_production_snapshot_accepts_bounded_compound_close_tokens() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    snapshots = []
    try:
        async with async_playwright() as playwright:
            browser = await launch_web_test_browser(playwright.chromium, headless=True)
            for class_name in ("closeBtn", "closebtn"):
                page = await browser.new_page(viewport={"width": 1920, "height": 1080})
                await page.set_content(
                    f"""
                    <style>
                      #dialog {{ position:fixed; inset:120px 420px; }}
                      #control {{ position:absolute; top:8px; right:8px;
                                  width:36px; height:36px; cursor:pointer; }}
                    </style>
                    <section id="dialog" class="popup" role="dialog">
                      <div id="control" class="{class_name}"></div>
                    </section>
                    """
                )
                snapshot = await _collect_ui_snapshot(
                    page,
                    web_detail_candidate_priority=True,
                )
                await page.close()
                snapshots.append((class_name, snapshot))
            await browser.close()
    except WebBrowserUnavailable as exc:
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    for class_name, snapshot in snapshots:
        close_control = next(
            item
            for item in snapshot["elements"]
            if item.get("selector") == "#control"
        )
        assert close_control["web_obstruction_close_basis"] == "own_close_token"
        assert close_control["web_obstruction_close_control_unique"] is True
        assert close_control["web_obstruction_close_hit_safe"] is True
        candidates = build_web_obstruction_candidates(
            capture_id=f"capture-{class_name}",
            ui_snapshot=snapshot,
        )
        assert tuple(item.selector for item in candidates) == ("#control",)


@pytest.mark.asyncio
async def test_production_snapshot_coalesces_wide_close_parent_and_delegated_child() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>
      #dialog { position:fixed; inset:120px 16px; }
      #close-shell { height:44px; cursor:pointer; }
      #close-image { display:block; width:44px; height:44px; margin:auto; }
    </style>
    <section id="dialog" class="van-popup van-popup--center" role="dialog">
      <div class="popup-form">
        <div id="close-shell" class="close-btn">
          <img id="close-image" class="close-image on-dialog-close" alt="关闭"
               src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==">
        </div>
      </div>
    </section>
    """
    try:
        async with async_playwright() as playwright:
            browser = await launch_web_test_browser(playwright.chromium, headless=True)
            page = await browser.new_page(viewport={"width": 1920, "height": 1080})
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(
                page,
                web_detail_candidate_priority=True,
            )
            await browser.close()
    except WebBrowserUnavailable as exc:
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    by_selector = {item.get("selector"): item for item in snapshot["elements"]}
    assert by_selector["#close-shell"]["web_obstruction_close_control_unique"] is True
    assert by_selector["#close-image"]["web_obstruction_close_control_unique"] is True
    assert by_selector["#close-image"]["web_obstruction_close_semantics"] is True
    assert by_selector["#close-image"]["web_obstruction_close_basis"] == (
        "explicit_close_text"
    )
    assert by_selector["#close-image"]["web_obstruction_close_hit_safe"] is True
    assert by_selector["#close-image"]["hit_test"]["center_hits_target"] is True
    candidates = build_web_obstruction_candidates(
        capture_id="capture-wide-close-shell",
        ui_snapshot=snapshot,
    )
    assert tuple(item.selector for item in candidates) == ("#close-image",)


@pytest.mark.asyncio
async def test_production_snapshot_accepts_unique_close_token_wrapper_over_image() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>
      #popup { position:fixed; inset:0; z-index:9999; }
      #popup-card { width:1536px; height:772px; margin:154px auto; }
      #popup-close { width:42px; height:42px; margin:auto; }
      #popup-close img { display:block; width:42px; height:42px; }
    </style>
    <section id="popup" class="popup-container adspop">
      <div id="popup-card" class="popup-content">
        <div id="popup-close" class="popup-close">
          <img id="popup-close-image" alt=""
               src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==">
        </div>
      </div>
    </section>
    """
    try:
        async with async_playwright() as playwright:
            browser = await launch_web_test_browser(playwright.chromium, headless=True)
            page = await browser.new_page(viewport={"width": 1920, "height": 1080})
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(
                page,
                web_detail_candidate_priority=True,
            )
            await browser.close()
    except WebBrowserUnavailable as exc:
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    by_selector = {item.get("selector"): item for item in snapshot["elements"]}
    close = by_selector["#popup-close"]
    assert close["web_obstruction_close_basis"] == "own_close_token"
    assert close["web_obstruction_close_control_unique"] is True
    assert close["web_obstruction_close_hit_safe"] is True
    candidates = build_web_obstruction_candidates(
        capture_id="capture-popup-close-wrapper",
        ui_snapshot=snapshot,
    )
    assert tuple(item.selector for item in candidates) == ("#popup-close",)
