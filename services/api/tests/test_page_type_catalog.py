import json
import re
import unittest
from pathlib import Path

from services.api.app.domain.page.catalog import (
    InvalidPageTypeCodeError,
    load_default_page_type_catalog,
)

PHASE_SKILL_PATHS = (
    Path("services/api/app/agents/skills/phase/page-detection/SKILL.md"),
    Path("services/api/app/agents/skills/phase/page-exploration/SKILL.md"),
)
PAGE_TYPE_KEYS = {"page_type", "expected_page_type", "expected_next_page_type"}
DEPRECATED_PHASE_EXAMPLE_CODES = {
    "ad_popup",
    "detail",
    "global",
    "player",
    "unknown",
}


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _json_blocks(text: str) -> list[object]:
    return [
        json.loads(match.group(1))
        for match in re.finditer(r"```json\s*(.*?)\s*```", text, flags=re.DOTALL)
    ]


def _page_type_values(value: object) -> list[str | None]:
    if isinstance(value, dict):
        found: list[str | None] = []
        for key, item in value.items():
            if key in PAGE_TYPE_KEYS:
                if item is None or isinstance(item, str):
                    found.append(item)
                continue
            found.extend(_page_type_values(item))
        return found
    if isinstance(value, list):
        found = []
        for item in value:
            found.extend(_page_type_values(item))
        return found
    return []


class PageTypeCatalogTest(unittest.TestCase):
    def test_loads_explicit_supported_page_types(self) -> None:
        catalog = load_default_page_type_catalog()
        codes = catalog.codes

        self.assertTrue(codes)
        self.assertEqual(len(codes), len(set(codes)))
        self.assertNotIn("ranking", codes)
        self.assertEqual(catalog.candidate_page_types, {})
        for page_type in codes:
            self.assertEqual(catalog.require(page_type).code, page_type)

    def test_transient_line_loading_is_not_rule_matchable(self) -> None:
        catalog = load_default_page_type_catalog()

        line_loading = catalog.require_code("line_loading")
        startup_ad = catalog.require_code("startup_ad")
        guide_page = catalog.require_code("guide_page")

        self.assertFalse(line_loading.rule_matchable)
        self.assertTrue(startup_ad.rule_matchable)
        self.assertIn("倒计时", "".join(startup_ad.signals.ocr_keywords))
        self.assertFalse(guide_page.rule_matchable)
        self.assertFalse(guide_page.coverage_required)
        self.assertIn("安卓下载", guide_page.signals.ocr_keywords)

    def test_page_names_and_legacy_aliases_resolve_to_explicit_page_types(self) -> None:
        catalog = load_default_page_type_catalog()

        self.assertEqual(catalog.require("首页").code, "home")
        self.assertEqual(catalog.require("播放器").code, "video_detail")
        self.assertEqual(catalog.require("引导页").code, "guide_page")

    def test_catalog_resolves_requested_h5_navigation_aliases(self) -> None:
        catalog = load_default_page_type_catalog()

        self.assertEqual(catalog.require("二次元").code, "home")
        self.assertEqual(catalog.require("朋友圈").code, "content_list")
        self.assertEqual(catalog.require("星球").code, "post_list")
        self.assertEqual(catalog.require("资源").code, "post_list")

        video_list = catalog.require_code("video_list")
        self.assertIn("home-video", video_list.route_prefixes)
        self.assertIn("twodimensional", video_list.route_prefixes)

    def test_post_list_also_exposes_moments_detection_signals(self) -> None:
        """朋友圈可辅助识别帖子列表，但唯一导航解析仍保持内容列表。"""
        catalog = load_default_page_type_catalog()
        post_list = catalog.require_code("post_list")

        self.assertIn("朋友圈", post_list.aliases)
        self.assertIn("朋友圈", post_list.signals.ocr_keywords)
        self.assertIn("朋友圈", post_list.entry_hints[0].target_keywords)
        self.assertEqual(catalog.require("朋友圈").code, "content_list")

    def test_home_catalog_treats_navigation_station_as_home_signal(self) -> None:
        catalog = load_default_page_type_catalog()
        home = catalog.require_code("home")

        self.assertEqual(catalog.require("导航站").code, "home")
        self.assertIn("导航站", home.signals.ocr_keywords)
        self.assertIn("导航站", home.entry_hints[1].target_keywords)

    def test_catalog_maps_product_branded_navigation_labels(self) -> None:
        catalog = load_default_page_type_catalog()
        home = catalog.require_code("home")
        video_list = catalog.require_code("video_list")

        self.assertEqual(catalog.require("91娱乐").code, "home")
        self.assertIn("91娱乐", home.signals.ocr_keywords)
        self.assertIn("91娱乐", home.entry_hints[1].target_keywords)
        self.assertEqual(catalog.require("91看片").code, "video_list")
        self.assertIn("91看片", video_list.signals.ocr_keywords)
        self.assertIn("91看片", video_list.entry_hints[0].target_keywords)
        self.assertIn("91看片", home.next_page_candidates[0].target_keywords)

    def test_payment_catalog_exposes_profile_vip_entry_contract(self) -> None:
        payment = load_default_page_type_catalog().require_code("payment")

        self.assertEqual(
            payment.entry_hints[0].target_keywords,
            ("立即开通", "会员中心"),
        )
        self.assertIn("开通VIP", payment.signals.ocr_keywords)
        self.assertIn("开通VIP", payment.signals.dom_hints)
        self.assertNotIn("vip", payment.route_prefixes)
        self.assertIn("确认开通", payment.risk_actions)
        self.assertIn("立即支付", payment.risk_actions)
        self.assertIn("提交订单", payment.risk_actions)

    def test_runtime_code_validation_rejects_alias_and_legacy_type(self) -> None:
        catalog = load_default_page_type_catalog()

        self.assertEqual(catalog.require_code("video_detail").code, "video_detail")
        with self.assertRaises(InvalidPageTypeCodeError):
            catalog.require_code("播放器")
        with self.assertRaises(InvalidPageTypeCodeError):
            catalog.require_code("ad_popup")

    def test_video_list_exposes_detection_material_for_model_prompt(self) -> None:
        catalog = load_default_page_type_catalog()
        page_type = catalog.require("video_list")

        self.assertIn("推荐", page_type.signals.ocr_keywords)
        self.assertIn("多个视频封面、标题或内容卡片组成的列表页面", page_type.signals.layout_hints)

    def test_common_product_lifecycle_exposes_entry_and_exit_hints(self) -> None:
        catalog = load_default_page_type_catalog()

        self.assertEqual(
            catalog.require("line_loading").entry_hints[0].action_intent,
            "打开产品入口",
        )
        self.assertEqual(
            catalog.require("startup_ad").entry_hints[0].from_page_type,
            "line_loading",
        )
        self.assertIn("切换可见底部导航", catalog.require("home").exit_hints)
        self.assertEqual(
            catalog.require("video_list").next_page_candidates[0].page_type,
            "video_detail",
        )
        self.assertEqual(
            catalog.require("post_list").next_page_candidates[0].page_type,
            "content_detail",
        )

    def test_source_metadata_avoids_local_absolute_file_paths(self) -> None:
        catalog = load_default_page_type_catalog()

        self.assertEqual(catalog.source.workbook_name, "质检平台_综合方案表 (3).html")
        self.assertIn("用户调整：player 改为 video_detail 并新增 video_list", catalog.source.sheets)
        self.assertNotIn(":\\", catalog.source.workbook_name)

    def test_phase_skill_structured_examples_use_catalog_page_types(self) -> None:
        allowed_page_types = set(load_default_page_type_catalog().codes)

        for path in PHASE_SKILL_PATHS:
            with self.subTest(path=str(path)):
                text = _read_text(path)
                for block in _json_blocks(text):
                    for page_type in _page_type_values(block):
                        if page_type is not None:
                            self.assertIn(page_type, allowed_page_types)

    def test_page_detection_skill_uses_runtime_catalog_context(self) -> None:
        text = _read_text(PHASE_SKILL_PATHS[0])
        catalog_codes = set(load_default_page_type_catalog().codes)
        listed_codes = set(re.findall(r"^- `([^`]+)`$", text, flags=re.MULTILINE))

        self.assertFalse(catalog_codes.issubset(listed_codes))
        self.assertIn("Catalog Context", text)
        self.assertIn("all_page_types", text)

    def test_phase_skill_runtime_examples_do_not_expose_legacy_page_type_codes(self) -> None:
        for path in PHASE_SKILL_PATHS:
            text = _read_text(path)
            for code in DEPRECATED_PHASE_EXAMPLE_CODES:
                with self.subTest(path=str(path), code=code):
                    self.assertNotIn(f"`{code}`", text)
