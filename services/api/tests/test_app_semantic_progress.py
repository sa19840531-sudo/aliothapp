"""App 底栏标签访问与尝试计数的精确身份合同。"""

from __future__ import annotations

from services.api.app.application.app_semantic_progress import (
    APP_BOTTOM_NAV_LABEL_ATTEMPT_COUNTS,
    anwang_membership_landing_detected,
    bottom_nav_label_attempts_exhausted,
    bottom_tab_label_already_visited,
    bottom_tab_playbook_progress_already_visited,
    expand_bottom_tab_label_aliases,
    expand_bottom_tab_promotion_aliases,
    membership_center_surface_detected,
    parse_bottom_nav_label_attempt_counts,
    record_bottom_nav_label_seek_attempt,
    short_drama_bottom_nav_label_hits,
    short_drama_immersive_player_chrome,
)


def test_immersive_player_chrome_overrides_missing_bottom_labels() -> None:
    chrome = "醒了 观看完整剧集-全5集 02:35/03:27"
    assert short_drama_immersive_player_chrome(chrome) is True
    assert short_drama_bottom_nav_label_hits(chrome) == 0
    assert short_drama_immersive_player_chrome("主页 短剧 暗网 社区") is False


def test_membership_center_is_not_anwang_landing() -> None:
    blob = "会员中心会员套餐与支付页 至尊全网通 国庆尊享卡 ¥300/立即支付"
    assert membership_center_surface_detected(blob) is True
    assert anwang_membership_landing_detected(blob) is False
    assert anwang_membership_landing_detected(
        "揭秘世界最阴暗面 至尊全网通 立即解锁"
    ) is True


def test_stable_synonym_aliases_mutual_visit() -> None:
    """主页↔首页、短剧↔视频：已访互认（录屏／playbook 文案常不一致）。"""

    assert "主页" in expand_bottom_tab_label_aliases("首页")
    assert "首页" in expand_bottom_tab_label_aliases("主页")
    assert bottom_tab_label_already_visited("首页", {"主页"})
    assert bottom_tab_label_already_visited("短剧", {"视频"})
    assert "短剧" in expand_bottom_tab_promotion_aliases("视频")


def test_mid_slot_skin_aliases_visit_but_not_playbook_progress() -> None:
    """中槽换皮袋可已访互认，但课纲推进不得用漫画冒充暗网已访。"""

    assert "漫画" in expand_bottom_tab_label_aliases("暗网")
    assert bottom_tab_label_already_visited("暗网", {"漫画"})
    assert not bottom_tab_playbook_progress_already_visited("暗网", {"漫画"})
    assert "暗网" not in expand_bottom_tab_promotion_aliases("漫画")


def test_bottom_label_representation_normalization_feeds_alias_bag() -> None:
    """表示层前缀归一后进入产品别名袋，禁止把「底栏「短剧」」当成独立产品身份。"""

    assert expand_bottom_tab_label_aliases(" 底栏「短剧」 ") == frozenset(
        {"视频", "短剧", "短视频"}
    )
    assert bottom_tab_label_already_visited(" 底栏「短剧」 ", {"底部TAB短剧"})
    assert bottom_tab_label_already_visited("短剧", {"视频"})


def test_bottom_attempt_counts_normalize_keys_without_merging_labels() -> None:
    parsed = parse_bottom_nav_label_attempt_counts(
        {" 底栏「暗网」 ": 1, "暗网": 2, "漫画": 2}
    )
    assert parsed == {"暗网": 3, "漫画": 2}

    counts = record_bottom_nav_label_seek_attempt(
        {}, label="底栏「暗网」", evidenced=False
    )
    counts = record_bottom_nav_label_seek_attempt(
        counts, label="漫画", evidenced=False
    )
    assert counts == {"暗网": 1, "漫画": 1}

    metadata = {APP_BOTTOM_NAV_LABEL_ATTEMPT_COUNTS: {"暗网": 2}}
    assert bottom_nav_label_attempts_exhausted("暗网", metadata)
    assert not bottom_nav_label_attempts_exhausted("漫画", metadata)


def test_bottom_attempt_evidence_clears_only_current_label() -> None:
    counts = {"暗网": 2, "漫画": 2, "视频": 1}

    result = record_bottom_nav_label_seek_attempt(
        counts,
        label="底部Tab暗网",
        evidenced=True,
    )

    assert result == {"漫画": 2, "视频": 1}
