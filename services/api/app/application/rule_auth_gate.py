"""规则登录依赖闸门：auth_requirement 声明与会话登录态事实的对接点。

规则种子在 ``input_contract.auth_requirement`` 上声明登录依赖：
``authenticated``=仅登录态下判定有意义；``guest``=仅未登录态可测；缺省=无关。
本模块负责回答「当前应拦截哪个声明值」，SQL 侧据此过滤候选并在收尾补记
出「未执行（登录态不满足）」，不能用缺少登录证据证明规则不适用。
"""

from __future__ import annotations

AUTH_REQUIREMENT_AUTHENTICATED = "authenticated"
AUTH_REQUIREMENT_GUEST = "guest"

# 收尾补记与读侧共用的 judge_output.reason 值。
AUTH_REQUIREMENT_NOT_MET_REASON = "auth_requirement_not_met"

# 仓储方法 auth_gate 参数的自动解析哨兵：按当前环境事实解析拦截值。
AUTH_GATE_AUTO = "auto"

_VALID_GATE_VALUES = (
    AUTH_GATE_AUTO,
    AUTH_REQUIREMENT_AUTHENTICATED,
    AUTH_REQUIREMENT_GUEST,
    None,
)


def resolve_blocked_auth_requirement() -> str | None:
    """返回浏览器会话当前应拦截的 auth_requirement 值；无确定事实时返回 None。

    一期事实源：H5 主动登录未启用（ALIOTH_H5_AUTH_ENABLED=false）时，浏览器
    会话必然是未登录（游客）态，需要登录态的规则应拦下不判。
    App 平台不得再整平台豁免：调用方应传显式 auth_gate，或使用
    ``resolve_blocked_auth_requirement_from_session_auth``。
    二期（登录机启用后）：应改读 qc_surface_phase_run 里 ``h5_auth_bootstrap``
    的会话结果（登录成功拦 guest、未成拦 authenticated）；接线前启用状态下
    返回 None——状态未知时闸门不动作，规则照常执行。
    """
    from services.api.app.core.config import get_settings

    if not get_settings().h5_auth.enabled:
        return AUTH_REQUIREMENT_AUTHENTICATED
    return None


def resolve_blocked_auth_requirement_from_session_auth(
    session_auth: str | None,
    *,
    no_test_account: bool = False,
) -> str | None:
    """按 App Capture／任务配置的登录态事实解析应拦截的 auth_requirement。

    - authenticated → 拦 guest 规则
    - guest → 拦 authenticated（规则尚未执行，不能记为不适用）
    - 无测试账号且登录态未知 → 拦 authenticated；不覆盖已证实登录事实
    - unknown → 无确定事实，不拦（避免误杀）
    """

    text = str(session_auth or "").strip().casefold()
    if text in {"authenticated", "logged_in", "login", "authed", "user"}:
        return AUTH_REQUIREMENT_GUEST
    if no_test_account or text in {"guest", "anonymous", "visitor", "游客", "logged_out"}:
        return AUTH_REQUIREMENT_AUTHENTICATED
    return None


def normalize_auth_gate(auth_gate: str | None) -> str | None:
    """把仓储层 auth_gate 参数归一为应拦截的声明值（None=不拦）。"""

    if auth_gate not in _VALID_GATE_VALUES:
        raise ValueError(f"非法 auth_gate 取值：{auth_gate!r}")
    if auth_gate == AUTH_GATE_AUTO:
        return resolve_blocked_auth_requirement()
    return auth_gate
