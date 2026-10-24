"""H5 通用遮挡事实回归。"""

from services.api.app.application.h5_obstruction_facts import (
    assess_h5_ui_obstruction,
)


def test_complete_modal_census_rejects_background_card_class_names() -> None:
    snapshot = {
        "viewport": {"width": 390, "height": 844},
        "h5_modal_census_selectors": [],
        "h5_modal_census_truncated": False,
        "elements": [{
            "selector": "#card", "class_tokens": ["post-card-mask"],
            "ancestor_class_tokens": ["card-popup"],
            "rect": {"width": 280, "height": 270},
            "style": {"position": "absolute", "z_index": "auto"},
            "effective_z_index": 1,
            "hit_test": {"center_hits_target": True, "pointer_interception": True},
        }],
    }
    assert assess_h5_ui_obstruction(snapshot).blocking is False
    snapshot["elements"][0]["h5_modal_ancestor_selectors"] = ["#panel"]
    snapshot["h5_modal_census_selectors"] = ["#panel"]
    assert assess_h5_ui_obstruction(snapshot).blocking is True
    snapshot["h5_modal_census_truncated"] = True
    assert assess_h5_ui_obstruction(snapshot).blocking is True


def test_small_notification_with_fallthrough_is_not_blocking() -> None:
    assessment = assess_h5_ui_obstruction(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "role": "dialog",
                    "class_tokens": ["toast", "toast--text"],
                    "rect": {"x": 135, "y": 404, "width": 120, "height": 36},
                    "style": {
                        "display": "flex",
                        "visibility": "visible",
                        "opacity": "1",
                    },
                    "hit_test": {"center_hits_target": False},
                }
            ],
        }
    )

    assert assessment.kind == "nonblocking_notification"
    assert assessment.blocking is False


def test_pointer_events_none_notification_is_nonblocking() -> None:
    assessment = assess_h5_ui_obstruction(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "role": "alert",
                    "class_tokens": ["notification"],
                    "rect": {"x": 40, "y": 40, "width": 310, "height": 52},
                    "style": {
                        "display": "block",
                        "visibility": "visible",
                        "opacity": "1",
                        "pointer_events": "none",
                    },
                    "hit_test": {"center_hits_target": True},
                }
            ],
        }
    )

    assert assessment.kind == "nonblocking_notification"
    assert assessment.blocking is False


def test_fullscreen_dialog_remains_blocking() -> None:
    assessment = assess_h5_ui_obstruction(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "role": "dialog",
                    "class_tokens": ["modal", "fullscreen"],
                    "rect": {"x": 0, "y": 0, "width": 390, "height": 844},
                    "style": {
                        "display": "block",
                        "visibility": "visible",
                        "opacity": "1",
                    },
                    "hit_test": {"center_hits_target": True},
                }
            ],
        }
    )

    assert assessment.kind == "blocking_overlay"
    assert assessment.blocking is True


def test_small_modal_that_receives_hit_test_is_still_blocking() -> None:
    assessment = assess_h5_ui_obstruction(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "role": "dialog",
                    "class_tokens": ["modal"],
                    "rect": {"x": 45, "y": 240, "width": 300, "height": 280},
                    "style": {
                        "display": "block",
                        "visibility": "visible",
                        "opacity": "1",
                    },
                    "hit_test": {"center_hits_target": True},
                }
            ],
        }
    )

    assert assessment.kind == "blocking_overlay"


def test_popup_descendant_hit_proves_blocking_ancestor() -> None:
    assessment = assess_h5_ui_obstruction(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "class_tokens": ["title"],
                    "ancestor_class_tokens": ["goldPopup", "movieBox"],
                    "text": "试看结束",
                    "rect": {"width": 67, "height": 23},
                    "style": {
                        "display": "block",
                        "visibility": "visible",
                        "opacity": "1",
                    },
                    "hit_test": {
                        "center_hits_target": True,
                        "pointer_interception": True,
                    },
                }
            ],
        }
    )

    assert assessment.kind == "blocking_overlay"
    assert assessment.blocking is True


def test_small_floating_widget_descendant_does_not_block_h5_page() -> None:
    assessment = assess_h5_ui_obstruction(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "class_tokens": ["icon"],
                    "ancestor_class_tokens": ["floating-widget"],
                    "rect": {"width": 48, "height": 48},
                    "style": {
                        "display": "block",
                        "visibility": "visible",
                        "opacity": "1",
                    },
                    "hit_test": {
                        "center_hits_target": True,
                        "pointer_interception": True,
                    },
                }
            ],
        }
    )

    assert assessment.kind == "none"
    assert assessment.blocking is False


def test_small_float_popup_descendant_does_not_block_h5_page() -> None:
    assessment = assess_h5_ui_obstruction(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "class_tokens": ["icon"],
                    "ancestor_class_tokens": ["floatPopup", "activity-entry"],
                    "rect": {"width": 54, "height": 54},
                    "style": {
                        "display": "block",
                        "visibility": "visible",
                        "opacity": "1",
                    },
                    "hit_test": {
                        "center_hits_target": True,
                        "pointer_interception": True,
                    },
                }
            ],
        }
    )

    assert assessment.kind == "none"
    assert assessment.blocking is False


def test_full_viewport_warning_media_is_blocking_gate() -> None:
    assessment = assess_h5_ui_obstruction(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "tag": "img",
                    "class_tokens": [],
                    "ancestor_class_tokens": ["warning-box", "content-shell"],
                    "rect": {"x": 0, "y": 0, "width": 390, "height": 788},
                    "effective_z_index": 1000,
                    "style": {
                        "display": "block",
                        "visibility": "visible",
                        "opacity": "1",
                    },
                    "hit_test": {
                        "center_hits_target": True,
                        "pointer_interception": True,
                        "topmost": True,
                    },
                }
            ],
        }
    )

    assert assessment.kind == "blocking_overlay"
    assert assessment.blocking is True
    assert assessment.full_viewport_gate_count == 1


def test_full_viewport_business_media_without_gate_semantics_is_not_blocking() -> None:
    assessment = assess_h5_ui_obstruction(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "tag": "video",
                    "class_tokens": ["video-player"],
                    "ancestor_class_tokens": ["video-detail"],
                    "rect": {"x": 0, "y": 0, "width": 390, "height": 788},
                    "effective_z_index": 1000,
                    "style": {
                        "display": "block",
                        "visibility": "visible",
                        "opacity": "1",
                    },
                    "hit_test": {
                        "center_hits_target": True,
                        "pointer_interception": True,
                        "topmost": True,
                    },
                }
            ],
        }
    )

    assert assessment.kind == "none"
    assert assessment.blocking is False
    assert assessment.full_viewport_gate_count == 0


def test_business_player_overlay_that_does_not_intercept_pointer_is_not_blocking() -> None:
    assessment = assess_h5_ui_obstruction(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "tag": "div",
                    "class_tokens": ["overlay"],
                    "ancestor_class_tokens": ["drama-player-card", "swiper-slide-active"],
                    "rect": {"x": 0, "y": 0, "width": 390, "height": 787},
                    "effective_z_index": 1000,
                    "style": {
                        "display": "block",
                        "visibility": "visible",
                        "opacity": "1",
                        "pointer_events": "none",
                    },
                    "hit_test": {
                        "center_hits_target": False,
                        "pointer_interception": False,
                        "topmost": False,
                    },
                },
                {
                    "tag": "p",
                    "class_tokens": ["title"],
                    "ancestor_class_tokens": ["overlay", "drama-player-card"],
                    "rect": {"x": 16, "y": 630, "width": 220, "height": 24},
                    "style": {
                        "display": "block",
                        "visibility": "visible",
                        "opacity": "1",
                    },
                    "hit_test": {
                        "center_hits_target": True,
                        "pointer_interception": True,
                        "topmost": True,
                    },
                },
            ],
        }
    )

    assert assessment.kind == "none"
    assert assessment.blocking is False
