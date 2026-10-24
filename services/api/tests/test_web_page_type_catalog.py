from __future__ import annotations

import importlib
import importlib.util
import json
from copy import deepcopy
from pathlib import Path

import pytest

MODULE_NAME = "services.api.app.domain.page.web_page_catalog"
EXPECTED_WEB_PAGE_TYPES = (
    "home",
    "content_list",
    "content_detail",
    "video_list",
    "video_detail",
    "activity",
    "search_page",
    "collection_page",
    "age_gate",
    "ad_page",
    "recharge_page",
    "topup_page",
    "guide_page",
    "unknown",
)


def _load_web_catalog_module():
    spec = importlib.util.find_spec(MODULE_NAME)
    assert spec is not None, "Web 独立页面目录加载器尚未实现"
    return importlib.import_module(MODULE_NAME)


def test_web_catalog_retains_home_navigation_and_site_observations() -> None:
    module = _load_web_catalog_module()

    catalog = module.load_web_page_type_catalog()

    assert catalog.codes == EXPECTED_WEB_PAGE_TYPES
    assert len(set(catalog.codes)) == 14
    assert catalog.platform == "web"
    assert catalog.require_code("home").required_positive_signals == (
        "页面顶部或侧边存在完整主导航（含多个分类入口、搜索、登录/注册等）",
        "主体区域有多个内容板块，或选中首页导航下呈现可切换的连续视频信息流",
        "存在多个并列内容卡片或可信的重复视频候选，不以单个当前播放项断言详情",
    )
    assert catalog.navigation_signals.common_positive_signals
    assert catalog.common_site_observations.age_gate
    assert all(not code.startswith("web_") for code in catalog.codes)
    for definition in catalog.page_types:
        assert definition.purpose
        assert definition.required_positive_signals
        assert definition.supporting_signals
        assert definition.strong_negative_signals
        assert definition.confusable_with
        assert definition.missing_evidence_actions


def test_current_v18_catalog_keeps_v17_v16_and_v15_frozen_payload_readable() -> None:
    module = _load_web_catalog_module()
    current = module.load_web_page_type_catalog()
    current_payload = json.loads(
        Path(module.__file__).with_name("web_page_type_catalog.json").read_text(encoding="utf-8")
    )
    current_rank = next(
        item for item in current_payload["page_types"] if item["code"] == "video_list"
    )
    assert {item["fact"] for item in current_rank["recognition_rules"]["required_any"]} >= {
        "ranked_video_row_count"
    }
    payload = json.loads(
        (Path(__file__).with_name("fixtures") / "web_page_catalog_20260930_v17.json")
        .read_text(encoding="utf-8")
    )
    frozen_v17 = module.parse_web_page_type_catalog(payload, legacy_frozen=True)
    rank = next(item for item in payload["page_types"] if item["code"] == "video_list")
    assert not any(
        item["fact"] == "ranked_video_row_count"
        for item in rank["recognition_rules"]["required_any"]
    )
    home = next(item for item in payload["page_types"] if item["code"] == "home")
    feed_guard = next(
        item for item in home["recognition_rules"]["strong_negative_any"]
        if item["reason_code"] == "active_home_player_without_feed"
    )
    assert {item["fact"] for item in feed_guard["all_of"]} == {
        "active_home_navigation", "player_root_count",
        "repeated_primary_card_count", "continuous_video_feed_count",
    }
    frozen_v16_payload = deepcopy(payload)
    frozen_v16_payload["version"] = "2026-09-30.web.v16"
    frozen_v16_home = next(
        item for item in frozen_v16_payload["page_types"] if item["code"] == "home"
    )
    frozen_v16_feed_guard = next(
        item for item in frozen_v16_home["recognition_rules"]["strong_negative_any"]
        if item["reason_code"] == "active_home_player_without_feed"
    )
    frozen_v16_feed_guard["all_of"] = [
        item for item in frozen_v16_feed_guard["all_of"]
        if item["fact"] != "continuous_video_feed_count"
    ]
    frozen_v16 = module.parse_web_page_type_catalog(frozen_v16_payload, legacy_frozen=True)

    payload = deepcopy(frozen_v16_payload)
    payload["version"] = "2026-09-29.web.v15"
    home = next(item for item in payload["page_types"] if item["code"] == "home")
    single_player = next(
        item for item in home["recognition_rules"]["strong_negative_any"]
        if item["reason_code"] == "single_player_conflicts_with_home"
    )
    single_player["all_of"] = [
        item for item in single_player["all_of"]
        if item["fact"] != "active_home_navigation"
    ]
    home["recognition_rules"]["strong_negative_any"] = [
        item for item in home["recognition_rules"]["strong_negative_any"]
        if not item["reason_code"].startswith("active_home_player_without_")
    ]

    frozen = module.parse_web_page_type_catalog(payload, legacy_frozen=True)

    assert current.version == "2026-09-30.web.v18"
    assert frozen_v17.version == "2026-09-30.web.v17"
    assert frozen_v16.version == "2026-09-30.web.v16"
    assert frozen.version == "2026-09-29.web.v15"
    assert frozen_v17.codes == current.codes
    assert frozen_v16.codes == current.codes
    assert frozen.codes == current.codes


def test_web_catalog_preserves_navigation_and_detail_link_knowledge() -> None:
    catalog = _load_web_catalog_module().load_web_page_type_catalog()

    collapsed_rules = catalog.navigation_signals.collapsed_and_visible_nav.rules
    assert collapsed_rules[0].startswith("主导航可视结构判定")
    assert any("隐藏或收起的真实安全 href 仍应采集并直接导航" in rule for rule in collapsed_rules)
    assert any("visibility 只保留作审计" in note for note in catalog.navigation_signals.usage_notes)
    assert "breadcrumb" in catalog.navigation_signals.distinguish_from_breadcrumb
    assert catalog.detail_link_signals.purpose.startswith("在列表或菜单页中")
    assert "to_video_detail" in catalog.detail_link_signals.content_vs_video_detail_link


def test_web_catalog_publishes_fourteen_types_and_exact_groups() -> None:
    catalog = _load_web_catalog_module().load_web_page_type_catalog()

    assert catalog.codes == EXPECTED_WEB_PAGE_TYPES
    assert catalog.page_type_groups.business == EXPECTED_WEB_PAGE_TYPES[:8]
    assert catalog.page_type_groups.lifecycle == ("age_gate",)
    assert catalog.page_type_groups.evidence_only == (
        "ad_page", "recharge_page", "topup_page", "guide_page"
    )
    assert catalog.page_type_groups.neither == ("unknown",)
    assert catalog.require_code("ad_page").category == "evidence_only"
    assert catalog.require_code("unknown").category == "neither"
    assert "profile" not in catalog.require_code("activity").confusable_with


def test_web_detail_media_rules_are_loaded_not_ignored() -> None:
    catalog = _load_web_catalog_module().load_web_page_type_catalog()

    assert catalog.media_rules["content_detail"]["image_gallery_or_viewer"] == (
        "支持判定为 content_detail"
    )
    assert catalog.media_rules["video_detail"]["player_shell_without_resource"] == (
        "不支持 video_detail"
    )
    assert "图片查看器、图集、漫画翻页 → content_detail" in (
        catalog.detail_classification_principles
    )


@pytest.mark.parametrize(
    "mutation",
    (
        lambda payload: payload["media_rules"]["content_detail"].pop("embedded_media_in_body"),
        lambda payload: payload["media_rules"]["video_detail"].pop(
            "background_ad_or_preview_video"
        ),
        lambda payload: payload.__setitem__(
            "detail_classification_principles",
            payload["detail_classification_principles"][:-1],
        ),
        lambda payload: payload["detail_classification_principles"].__setitem__(
            0,
            "播放器出现即为视频详情",
        ),
    ),
)
def test_web_catalog_rejects_incomplete_or_changed_v8_detail_semantics(mutation) -> None:
    module = _load_web_catalog_module()
    default_catalog = Path(module.__file__).with_name("web_page_type_catalog.json")
    payload = json.loads(default_catalog.read_text(encoding="utf-8"))
    mutation(payload)

    with pytest.raises(module.WebPageTypeCatalogError):
        module.parse_web_page_type_catalog(payload)


@pytest.fixture(
    params=(("2026-09-11.web.v5", False), ("2026-09-12.web.v6", True), ("2026-09-13.web.v7", True))
)
def frozen_v5_to_v7_catalog_payload(request):
    version, includes_detail_link_signals = request.param
    page_types = []
    for code in ("home", "content_list", "content_detail", "video_list", "video_detail"):
        page_types.append(
            {
                "code": code,
                "name": f"冻结 {code}",
                "status": "supported",
                "category": "business",
                "coverage_required": True,
                "signals": {
                    "ocr_keywords": [f"{code} OCR"],
                    "dom_hints": [f"{code}-dom"],
                    "layout_hints": [f"{code} 布局"],
                },
                "recognition_rules": {
                    "required_all": [
                        {"fact": "top_level_obstruction_count", "operator": "eq", "value": 0}
                    ],
                    "required_any": [
                        {"fact": "dom_tokens", "operator": "contains_exact", "value": f"{code}-dom"}
                    ],
                    "supporting": [],
                    "strong_negative_any": [],
                },
            }
        )
    payload = {
        "schema_version": "page-type-catalog.v2",
        "version": version,
        "platform": "web",
        "source": {
            "workbook_name": "冻结五类目录",
            "sheets": ["Web 五类页面"],
            "extraction_method": "frozen-web-five-types",
        },
        "page_types": page_types,
        "navigation_signals": {
            "common_positive_signals": ["冻结主导航"],
            "supporting_signals": ["冻结导航辅助"],
            "collapsed_and_visible_nav": {"description": "冻结导航", "rules": ["冻结规则"]},
            "strong_negative_signals": ["冻结导航否定"],
            "distinguish_from_breadcrumb": {
                "menu_nav": ["冻结菜单"],
                "breadcrumb": ["冻结面包屑"],
            },
            "usage_notes": ["冻结导航说明"],
        },
        "common_site_observations": {
            "age_gate": "冻结年龄门",
            "ad_overlay": "冻结广告",
            "mixed_content": "冻结混合内容",
            "navigation": "冻结导航观察",
            "player": "冻结播放器",
        },
    }
    if includes_detail_link_signals:
        payload["detail_link_signals"] = {
            "purpose": "冻结详情入口",
            "required_positive_signals": ["冻结详情正向"],
            "supporting_signals": ["冻结详情辅助"],
            "strong_negative_signals": ["冻结详情否定"],
            "distinguish_from_nav_and_ad": {
                "detail_link": ["冻结详情链接"],
                "menu_nav": ["冻结菜单导航"],
                "ad_or_promo": ["冻结广告链接"],
            },
            "content_vs_video_detail_link": {
                "to_content_detail": ["冻结内容详情"],
                "to_video_detail": ["冻结视频详情"],
            },
            "usage_notes": ["冻结详情说明"],
        }
    return payload, includes_detail_link_signals


def test_v5_to_v7_frozen_catalogs_restore_five_types_without_v8_knowledge(
    frozen_v5_to_v7_catalog_payload,
) -> None:
    module = _load_web_catalog_module()
    payload, includes_detail_link_signals = frozen_v5_to_v7_catalog_payload

    catalog = module.parse_web_page_type_catalog(payload, legacy_frozen=True)

    assert catalog.codes == ("home", "content_list", "content_detail", "video_list", "video_detail")
    assert (catalog.detail_link_signals is not None) is includes_detail_link_signals
    assert catalog.page_type_groups is None
    assert catalog.media_rules == {}
    assert catalog.detail_classification_principles == ()
    assert catalog.notes == ()


def test_v8_frozen_catalog_restores_nine_types_without_ad_page() -> None:
    module = _load_web_catalog_module()
    default_catalog = Path(module.__file__).with_name("web_page_type_catalog.json")
    payload = json.loads(default_catalog.read_text(encoding="utf-8"))
    payload["version"] = "2026-09-13.web.v8"
    payload["page_types"] = [
        item
        for item in payload["page_types"]
        if item["code"] not in {
            "ad_page", "recharge_page", "topup_page", "collection_page", "guide_page"
        }
    ]
    payload["page_type_groups"].pop("evidence_only")
    payload["page_type_groups"]["business"].remove("collection_page")

    catalog = module.parse_web_page_type_catalog(payload, legacy_frozen=True)

    assert catalog.codes == tuple(
        code
        for code in EXPECTED_WEB_PAGE_TYPES
        if code not in {"ad_page", "recharge_page", "topup_page", "collection_page", "guide_page"}
    )
    assert catalog.page_type_groups is not None
    assert catalog.page_type_groups.evidence_only == ()


@pytest.mark.asyncio
async def test_frozen_catalog_rejection_never_introduces_v8_unknown(
    frozen_v5_to_v7_catalog_payload,
):
    from services.api.app.application.web_page_classification import (
        WebPageAssessment,
        WebPageClassifier,
    )
    from services.api.app.domain.page.web_page_catalog import parse_web_page_type_catalog
    from services.api.tests.test_web_page_classification import Assessor

    payload, _ = frozen_v5_to_v7_catalog_payload
    with pytest.raises(ValueError, match="WEB_LEGACY_CATALOG_EXECUTION_FORBIDDEN"):
        WebPageClassifier(
            assessor=Assessor(
                WebPageAssessment(page_type="video_detail", confidence=0.99, evidence=("视觉建议",))
            ),
            catalog=parse_web_page_type_catalog(payload, legacy_frozen=True),
        )


def test_legacy_catalog_with_four_field_navigation_restores_without_v6_knowledge() -> None:
    module = _load_web_catalog_module()
    fixture = Path(__file__).with_name("fixtures") / "web_page_catalog_20260904_v3.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    payload["platform"] = "web"
    payload["navigation_signals"] = {
        "common_positive_signals": ["旧版主导航"],
        "supporting_signals": ["旧版导航辅助信号"],
        "strong_negative_signals": ["旧版导航否定信号"],
        "usage_notes": ["旧版导航说明"],
    }
    payload["common_site_observations"] = {
        "age_gate": "旧版年龄门说明",
        "ad_overlay": "旧版广告说明",
        "mixed_content": "旧版混合内容说明",
        "navigation": "旧版导航说明",
        "player": "旧版播放器说明",
    }

    catalog = module.parse_web_page_type_catalog(payload, legacy_frozen=True)

    assert catalog.navigation_signals is None
    assert catalog.detail_link_signals is None
    assert catalog.common_site_observations.navigation == "旧版导航说明"


def test_web_catalog_requires_exact_published_page_type_order() -> None:
    module = _load_web_catalog_module()
    default_catalog = Path(module.__file__).with_name("web_page_type_catalog.json")
    payload = json.loads(default_catalog.read_text(encoding="utf-8"))

    assert tuple(item["code"] for item in payload["page_types"]) == EXPECTED_WEB_PAGE_TYPES

    payload["page_types"] = list(reversed(payload["page_types"]))
    with pytest.raises(module.WebPageTypeCatalogError, match="固定顺序"):
        module.parse_web_page_type_catalog(payload)


@pytest.mark.parametrize("page_type", EXPECTED_WEB_PAGE_TYPES)
def test_web_catalog_rejects_invalid_status_for_every_page_type(page_type: str) -> None:
    module = _load_web_catalog_module()
    default_catalog = Path(module.__file__).with_name("web_page_type_catalog.json")
    payload = json.loads(default_catalog.read_text(encoding="utf-8"))
    target = next(item for item in payload["page_types"] if item["code"] == page_type)
    target["status"] = {"invalid": 1}

    with pytest.raises(module.WebPageTypeCatalogError, match="status"):
        module.parse_web_page_type_catalog(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("ocr_keywords", None),
        ("dom_hints", {"invalid": 1}),
        ("layout_hints", [None]),
        ("dom_hints", [""]),
    ),
)
def test_web_catalog_rejects_invalid_home_signal_arrays(field: str, value: object) -> None:
    module = _load_web_catalog_module()
    default_catalog = Path(module.__file__).with_name("web_page_type_catalog.json")
    payload = json.loads(default_catalog.read_text(encoding="utf-8"))
    home = next(item for item in payload["page_types"] if item["code"] == "home")
    home["signals"][field] = value

    with pytest.raises(module.WebPageTypeCatalogError, match=field):
        module.parse_web_page_type_catalog(payload)


@pytest.mark.parametrize(
    ("mutation", "expected_message"),
    (
        (lambda payload: payload.pop("platform"), "platform"),
        (lambda payload: payload.__setitem__("platform", "h5"), "platform"),
        (
            lambda payload: payload.__setitem__(
                "page_types",
                [item for item in payload["page_types"] if item["code"] != "home"],
            ),
            "14 类页面",
        ),
        (lambda payload: payload.pop("navigation_signals"), "navigation_signals"),
        (
            lambda payload: payload.__setitem__("common_site_observations", {}),
            "common_site_observations",
        ),
        (lambda payload: payload.__setitem__("unexpected_root", True), "顶层字段"),
    ),
)
def test_web_catalog_rejects_invalid_web_root_contract(mutation, expected_message) -> None:
    module = _load_web_catalog_module()
    default_catalog = Path(module.__file__).with_name("web_page_type_catalog.json")
    payload = json.loads(default_catalog.read_text(encoding="utf-8"))
    mutation(payload)

    with pytest.raises(module.WebPageTypeCatalogError, match=expected_message):
        module.parse_web_page_type_catalog(payload)


def test_web_catalog_failure_does_not_load_default_h5_catalog(tmp_path: Path) -> None:
    module = _load_web_catalog_module()

    with pytest.raises(module.WebPageTypeCatalogError, match="Web 页面目录"):
        module.load_web_page_type_catalog(tmp_path / "missing.json")


def test_web_catalog_rejects_extra_page_type(tmp_path: Path) -> None:
    module = _load_web_catalog_module()
    default_catalog = Path(module.__file__).with_name("web_page_type_catalog.json")
    payload = json.loads(default_catalog.read_text(encoding="utf-8"))
    extra_page_type = deepcopy(payload["page_types"][0])
    extra_page_type["code"] = "login"
    extra_page_type["name"] = "登录页"
    payload["page_types"].append(extra_page_type)
    invalid_path = tmp_path / "web_page_type_catalog.json"
    invalid_path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(module.WebPageTypeCatalogError, match="Web 页面目录"):
        module.load_web_page_type_catalog(invalid_path)


@pytest.mark.parametrize(
    "field_name",
    (
        "purpose",
        "required_positive_signals",
        "supporting_signals",
        "strong_negative_signals",
        "confusable_with",
        "missing_evidence_actions",
    ),
)
def test_web_catalog_rejects_missing_recognition_guide_field(
    tmp_path: Path,
    field_name: str,
) -> None:
    module = _load_web_catalog_module()
    default_catalog = Path(module.__file__).with_name("web_page_type_catalog.json")
    payload = json.loads(default_catalog.read_text(encoding="utf-8"))
    payload["page_types"][0].pop(field_name)
    invalid_path = tmp_path / "web_page_type_catalog.json"
    invalid_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(module.WebPageTypeCatalogError, match=field_name):
        module.load_web_page_type_catalog(invalid_path)


@pytest.mark.parametrize(
    "mutation",
    (
        lambda payload: payload.pop("page_type_groups"),
        lambda payload: payload.pop("detail_classification_principles"),
        lambda payload: payload.pop("notes"),
        lambda payload: payload["media_rules"].pop("content_detail"),
        lambda payload: payload["page_type_groups"]["neither"].__setitem__(0, "unkonw"),
        lambda payload: next(item for item in payload["page_types"] if item["code"] == "activity")[
            "confusable_with"
        ].append("profile"),
    ),
)
def test_web_catalog_rejects_missing_or_invalid_v8_knowledge(mutation) -> None:
    module = _load_web_catalog_module()
    default_catalog = Path(module.__file__).with_name("web_page_type_catalog.json")
    payload = json.loads(default_catalog.read_text(encoding="utf-8"))
    mutation(payload)

    with pytest.raises(module.WebPageTypeCatalogError):
        module.parse_web_page_type_catalog(payload)


@pytest.mark.parametrize(
    ("code", "field", "value"),
    (
        ("unknown", "category", "business"),
        ("activity", "coverage_required", True),
        ("search_page", "coverage_required", True),
        ("age_gate", "coverage_required", True),
    ),
)
def test_web_catalog_rejects_category_or_coverage_outside_its_group(
    code: str,
    field: str,
    value: object,
) -> None:
    module = _load_web_catalog_module()
    default_catalog = Path(module.__file__).with_name("web_page_type_catalog.json")
    payload = json.loads(default_catalog.read_text(encoding="utf-8"))
    target = next(item for item in payload["page_types"] if item["code"] == code)
    target[field] = value

    with pytest.raises(module.WebPageTypeCatalogError):
        module.parse_web_page_type_catalog(payload)


def test_recharge_page_is_evidence_only_and_not_business_coverage() -> None:
    module = _load_web_catalog_module()
    catalog = module.load_web_page_type_catalog()
    recharge = catalog.require_code("recharge_page")
    assert recharge.category == "evidence_only"
    assert recharge.coverage_required is False
    assert recharge.rule_matchable is False
    assert "recharge_page" in catalog.page_type_groups.evidence_only
    assert "recharge_page" not in catalog.page_type_groups.business
    topup = catalog.require_code("topup_page")
    assert topup.category == "evidence_only"
    assert topup.coverage_required is False
    assert topup.rule_matchable is False
    assert "topup_page" not in catalog.page_type_groups.business


def test_guide_page_is_evidence_only_and_v13_frozen_catalog_restores() -> None:
    module = _load_web_catalog_module()
    catalog = module.load_web_page_type_catalog()
    guide = catalog.require_code("guide_page")
    assert guide.category == "evidence_only"
    assert guide.coverage_required is False
    assert guide.rule_matchable is False
    payload = json.loads(
        Path(module.__file__).with_name("web_page_type_catalog.json").read_text(encoding="utf-8")
    )
    payload["version"] = "2026-09-28.web.v13"
    payload["page_types"] = [
        item for item in payload["page_types"] if item["code"] != "guide_page"
    ]
    payload["page_type_groups"]["evidence_only"].remove("guide_page")
    restored = module.parse_web_page_type_catalog(payload, legacy_frozen=True)
    assert "guide_page" not in restored.codes


def test_v12_frozen_catalog_restores_without_topup_page() -> None:
    module = _load_web_catalog_module()
    catalog_path = Path(module.__file__).with_name("web_page_type_catalog.json")
    payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    payload["version"] = "2026-09-26.web.v12"
    payload["page_types"] = [
        item for item in payload["page_types"] if item["code"] not in {"topup_page", "guide_page"}
    ]
    payload["page_type_groups"]["evidence_only"].remove("topup_page")
    payload["page_type_groups"]["evidence_only"].remove("guide_page")
    wallet = next(
        item for item in payload["page_types"] if item["code"] == "recharge_page"
    )
    wallet["confusable_with"].remove("topup_page")
    wallet["next_page_candidates"] = []
    restored = module.parse_web_page_type_catalog(payload, legacy_frozen=True)
    assert "topup_page" not in restored.codes
    assert restored.page_type_groups.evidence_only == ("ad_page", "recharge_page")


def test_v11_frozen_catalog_restores_without_recharge_page() -> None:
    module = _load_web_catalog_module()
    catalog_path = Path(module.__file__).with_name("web_page_type_catalog.json")
    payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    payload["version"] = "2026-09-19.web.v11"
    payload["page_types"] = [
        item for item in payload["page_types"]
        if item["code"] not in {"recharge_page", "topup_page", "guide_page"}
    ]
    if "recharge_page" in payload["page_type_groups"]["evidence_only"]:
        payload["page_type_groups"]["evidence_only"].remove("recharge_page")
    if "topup_page" in payload["page_type_groups"]["evidence_only"]:
        payload["page_type_groups"]["evidence_only"].remove("topup_page")
    payload["page_type_groups"]["evidence_only"].remove("guide_page")
    restored = module.parse_web_page_type_catalog(payload, legacy_frozen=True)
    assert "recharge_page" not in restored.codes
    assert restored.page_type_groups.evidence_only == ("ad_page",)
