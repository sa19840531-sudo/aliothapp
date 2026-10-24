"""H5 页面状态通用语义分类回归。"""

from __future__ import annotations

import pytest

from services.api.app.domain.exploration.h5_page_state import (
    classify_h5_page_state,
    h5_page_state_has_overlay,
    h5_page_state_supports_popup_dismissal,
)


@pytest.mark.parametrize(
    "page_state",
    [
        "popup",
        "popup_obstructed",
        "popup-overlay",
        "modal.loading",
        "DIALOG ACTIVE",
        "overlay",
    ],
)
def test_compound_overlay_state_supports_popup_dismissal(page_state: str) -> None:
    semantics = classify_h5_page_state(page_state)

    assert semantics.has_overlay is True
    assert semantics.supports_popup_dismissal is True
    assert h5_page_state_has_overlay(page_state) is True
    assert h5_page_state_supports_popup_dismissal(page_state) is True


@pytest.mark.parametrize(
    "page_state",
    [
        "popup_absent",
        "dialog-dismissed",
        "overlay_closed",
        "modal_removed",
        "not_popup",
    ],
)
def test_absent_overlay_state_never_supports_popup_dismissal(page_state: str) -> None:
    semantics = classify_h5_page_state(page_state)

    assert semantics.absent is True
    assert semantics.has_overlay is False
    assert semantics.supports_popup_dismissal is False


@pytest.mark.parametrize(
    "page_state",
    [
        "verification_popup",
        "captcha-overlay",
        "mfa_dialog",
        "otp_modal",
        "2fa_obstructed",
    ],
)
def test_challenge_overlay_is_visible_but_not_automatically_dismissible(
    page_state: str,
) -> None:
    semantics = classify_h5_page_state(page_state)

    assert semantics.challenge is True
    assert semantics.has_overlay is True
    assert semantics.supports_popup_dismissal is False


def test_non_overlay_state_remains_non_overlay() -> None:
    semantics = classify_h5_page_state("stable_loaded")

    assert semantics.has_overlay_marker is False
    assert semantics.has_overlay is False
    assert semantics.supports_popup_dismissal is False
