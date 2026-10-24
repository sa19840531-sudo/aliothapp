"""上方分类：skip 槽（乱伦）不得冒充课纲已访。"""

from __future__ import annotations

import pytest

from services.api.app.application.app_coverage_page_seek import (
    home_top_category_seek_selection_matches,
    record_visited_home_top_category_tab,
    resolve_selected_home_top_category_label,
)
from services.api.app.application.app_ui_tree_features import (
    home_top_category_label_is_skip,
    home_top_category_skip_labels,
    ordered_home_top_category_tab_labels,
)
from services.api.tests.support.app_home_top_contract import (
    legacy_home_top_features as legacy_home_top_features,
)

pytestmark = pytest.mark.usefixtures("legacy_home_top_features")

_HJLL_PACKAGE = "dark.wwcskrpojs.ebjhyebjbl"


def test_hjll_curriculum_skips_default_recommend() -> None:
    assert ordered_home_top_category_tab_labels(package_name=_HJLL_PACKAGE) == (
        "最新",
        "国产",
        "动漫",
        "漫画",
    )


def test_skip_labels_include_luanlun() -> None:
    skips = home_top_category_skip_labels(playbook_id="hjll")
    assert "乱伦" in skips or "亂倫" in skips
    assert "推荐" in skips or "推薦" in skips
    assert home_top_category_label_is_skip("乱伦", playbook_id="hjll")
    assert home_top_category_label_is_skip("乱伦", package_name=_HJLL_PACKAGE)
    assert home_top_category_label_is_skip("推荐", package_name=_HJLL_PACKAGE)
    assert home_top_category_label_is_skip("制服", package_name=_HJLL_PACKAGE)
    assert home_top_category_label_is_skip("OnlyFans", playbook_id="hjll")
    assert home_top_category_label_is_skip("日本AV", package_name=_HJLL_PACKAGE)
    assert home_top_category_label_is_skip("精选短剧", package_name=_HJLL_PACKAGE)
    assert home_top_category_label_is_skip("精選短劇", playbook_id="hjll")
    assert home_top_category_label_is_skip("乱伦综", package_name=_HJLL_PACKAGE)
    assert home_top_category_label_is_skip("亂倫綜", playbook_id="hjll")
    assert home_top_category_label_is_skip("Only Fans", package_name=_HJLL_PACKAGE)
    assert home_top_category_label_is_skip("三级片", playbook_id="hjll")


def test_resolve_selected_japan_av_is_skip() -> None:
    selected = resolve_selected_home_top_category_label(
        {
            "surface_facts": {"home_top_selected_label": "日本AV"},
        },
        package_name=_HJLL_PACKAGE,
    )
    assert selected == "__skip__"
    assert not home_top_category_seek_selection_matches(
        expected_label="漫画",
        selected_label=selected,
        package_name=_HJLL_PACKAGE,
    )


def test_record_visited_rejects_skip_label() -> None:
    visited = record_visited_home_top_category_tab(
        {"visited_home_top_category_tabs": ["最新", "推荐", "国产"]},
        label="乱伦",
    )
    assert "乱伦" not in visited
    assert visited == ["最新", "推荐", "国产"]


def test_selection_mismatch_on_skip_sentinel() -> None:
    assert not home_top_category_seek_selection_matches(
        expected_label="动漫",
        selected_label="__skip__",
        package_name=_HJLL_PACKAGE,
    )
    assert not home_top_category_seek_selection_matches(
        expected_label="动漫",
        selected_label="乱伦",
        package_name=_HJLL_PACKAGE,
    )
    assert home_top_category_seek_selection_matches(
        expected_label="动漫",
        selected_label="動漫",
        package_name=_HJLL_PACKAGE,
    )


def test_resolve_selected_reads_skip_flag() -> None:
    class _Analysis:
        surface_facts = {
            "home_top_selected_is_skip": True,
            "pixel_home_top_category_calibration": {
                "selected_is_skip": True,
                "selected_slot_index": 3,
                "tabs": [],
            },
        }

    assert resolve_selected_home_top_category_label(_Analysis()) == "__skip__"
