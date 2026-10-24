"""H5 线路加载壳的受信恢复能力常量。"""

H5_LINE_LOADING_LOGIN_RETRY_TARGET = "重试线路页登录"
H5_LINE_LOADING_LOGIN_RETRY_TARGET_MODE = "line_loading_login_retry"
H5_LINE_LOADING_LOGIN_RETRY_CAPABILITY = "line_loading_login_retry"
H5_LINE_LOADING_LOGIN_RETRY_PURPOSE = "重试线路页匿名登录并在当前浏览器会话重新采集"

H5_LINE_LOADING_LOGIN_RETRY_KEYWORDS = frozenset(
    {
        "line_loading_login_retry",
        "authentication_retry",
        "evidence:structure",
    }
)
