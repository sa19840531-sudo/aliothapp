"""检测和执行共用的已登记弹层控件族，不包含产品或域名映射。"""

VANT_CENTER_CLOSE_WRAPPERS = frozenset(
    {
        "textpopcontent",
        "imgwrapper",
        "img-wrapper",
        "image-wrapper",
        "popup-wrapper",
        "wrapper",
        "ad-dialog",
    }
)

OVERLAY_AD_CLOSE_SELECTOR = (
    ".adspop:visible > .popup-container:visible > .popup-content:visible "
    "> .popup-close:visible, "
    ".nineGridPopup:visible > .van-overlay:visible > .wrapper:visible > .close:visible, "
    ".announcementPopup:visible > .van-overlay:visible > .imgWrapper:visible > .close:visible, "
    ".van-overlay:visible :is(.on-popup-close, .close-img):visible, "
    "#popup-window:visible :is(.on-popup-close, .close-img):visible, "
    ".ad-dialog:visible :is(.close, .btn-close):visible, "
    ".recommend-layer:visible > .recommend-wrapper:visible "
    "> .recommend-card:visible > .close-btn:visible, "
    ".van-popup.van-popup--center:visible "
    "> :is(.closeBtn, .closebtn, .close, .btn-close, .close-btn, "
    ".van-icon-close, .van-popup__close-icon):visible, "
    ".van-popup.van-popup--center:visible "
    ":is(.textPopContent, .textpopcontent, .imgWrapper, .img-wrapper, "
    ".image-wrapper, .popup-wrapper, .wrapper, .ad-dialog):visible "
    "> :is(.closeBtn, .closebtn, .close, .btn-close, .close-btn, "
    ".van-icon-close, .van-popup__close-icon):visible, "
    ".van-popup.van-popup--center:visible > .textPopContent:visible > .closeBtn:visible"
)
