"""Interaction Runtime 的受限命令策略。

该模块只依赖领域合同，不执行任何设备、浏览器或数据库操作。它的职责是
在动作进入应用层协调器之前，校验动作类型、风险等级、目标范围以及参数
是否试图携带底层命令或运行时句柄。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from urllib.parse import urlsplit

from services.api.app.domain.interaction.action_intent import ActionIntent
from services.api.app.domain.interaction.enums import (
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)
from services.api.app.domain.interaction.validation import (
    ACTION_PARAMETER_FORBIDDEN_KEYS,
    InteractionValidationError,
)

COMMAND_POLICY_PROTOCOL_VERSION = "command-policy.v1"


class CommandPolicyError(InteractionValidationError):
    """ActionIntent 不符合平台命令策略。"""

    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        if not isinstance(error_code, str) or not error_code.strip():
            raise ValueError("error_code 必须为非空字符串")
        self.error_code = error_code.strip()
        self.retryable = bool(retryable)


# 这是动作层的默认范围，不代替具体 Surface Driver 的能力清单。Driver
# 仍必须在自己的适配器边界内再次拒绝不支持的动作。
DEFAULT_ALLOWED_OPERATIONS: Mapping[SurfaceKind, tuple[InteractionOperation, ...]] = {
    SurfaceKind.WEB: (
        InteractionOperation.NAVIGATE,
        InteractionOperation.CLICK,
        InteractionOperation.TAP,
        InteractionOperation.INPUT,
        InteractionOperation.HOVER,
        InteractionOperation.SCROLL,
        InteractionOperation.BACK,
        InteractionOperation.REFRESH,
        InteractionOperation.OBSERVE,
        InteractionOperation.SEEK_MEDIA,
        InteractionOperation.NETWORK_FAULT,
    ),
    SurfaceKind.ANDROID: (
        InteractionOperation.CLICK,
        InteractionOperation.TAP,
        InteractionOperation.INPUT,
        InteractionOperation.SCROLL,
        InteractionOperation.BACK,
        InteractionOperation.REFRESH,
        InteractionOperation.OBSERVE,
        InteractionOperation.DEEP_LINK,
        InteractionOperation.WARM_RESTORE,
        InteractionOperation.RESET_APP,
    ),
    SurfaceKind.IOS: (
        InteractionOperation.CLICK,
        InteractionOperation.TAP,
        InteractionOperation.INPUT,
        InteractionOperation.SCROLL,
        InteractionOperation.BACK,
        InteractionOperation.REFRESH,
        InteractionOperation.OBSERVE,
        InteractionOperation.DEEP_LINK,
        InteractionOperation.WARM_RESTORE,
        InteractionOperation.RESET_APP,
    ),
}

# ActionIntent 已经禁止一部分句柄和平台定位器。下面这组是更高一层的
# “命令注入”字段：Agent 可以描述语义动作，但不能把 shell、脚本、CLI
# 参数或连接地址塞进 parameters 让 Provider 直接执行。
COMMAND_PARAMETER_FORBIDDEN_KEYS = frozenset(
    {
        "adb",
        "adb_args",
        "browser_endpoint",
        "browser_ws_endpoint",
        "cli",
        "command",
        "command_args",
        "connection_url",
        "device_command",
        "endpoint",
        "executable",
        "javascript",
        "js",
        "process",
        "program",
        "script",
        "shell",
        "shell_args",
        "shell_command",
        "webdriver_endpoint",
    }
)

_FORBIDDEN_PARAMETER_KEYS = frozenset(
    key.casefold() for key in ACTION_PARAMETER_FORBIDDEN_KEYS | COMMAND_PARAMETER_FORBIDDEN_KEYS
)

_CAPABILITY_PARAMETER_BY_OPERATION = {
    # network_fault 不是任意网络命令：它只接受有界 fault_url_pattern，并且
    # 现有规则探针已经在上游按 supplemental policy 做能力判定。这里不把
    # Agent 自报的 capability 字段当授权来源，避免破坏既有正式探针入口。
    InteractionOperation.OPEN_EXTERNAL: "external_navigation_capability",
    InteractionOperation.SET_CLIPBOARD: "clipboard_capability",
}

# 这是正式任务范围使用的最低风险等级，不是 Agent 可以自行修改的提示。
# 默认静态策略不启用该表，以兼容离线 Demo；生产组合根通过任务事实源显式
# 注入，避免把低风险自报当成高副作用动作的授权。
SERVER_MINIMUM_RISK_BY_OPERATION: Mapping[InteractionOperation, InteractionRiskLevel] = {
    InteractionOperation.DEEP_LINK: InteractionRiskLevel.MEDIUM,
    InteractionOperation.WARM_RESTORE: InteractionRiskLevel.MEDIUM,
    InteractionOperation.RESET_APP: InteractionRiskLevel.MEDIUM,
    InteractionOperation.NETWORK_FAULT: InteractionRiskLevel.HIGH,
    InteractionOperation.OPEN_EXTERNAL: InteractionRiskLevel.HIGH,
    InteractionOperation.SET_CLIPBOARD: InteractionRiskLevel.HIGH,
}

# 目标风险不能只看 Agent 在 ActionIntent 上填写的 risk_level。下面的词表
# 只用于平台已经从 Capture/视觉候选/结构化 Snapshot 解析出目标后再次复核；
# 它不是开放给 Agent 自己声明的“风险标签”。
TRUSTED_TARGET_SOURCES = frozenset(
    {
        "android_ui_hierarchy",
        "app_visual_candidate",
        "dom_snapshot",
        "flutter_semantics",
        "logical_snapshot",
        "platform_capability",
        "trusted",
        "visual_coordinate",
    }
)
_HIGH_IMPACT_TARGET_MARKERS = (
    # 资金或订单提交动作；“进入支付页面”这类纯导航语义由导航上下文排除。
    "立即支付",
    "确认支付",
    "去支付",
    "支付",
    "付款",
    "购买",
    "下单",
    "订购",
    "充值",
    "转账",
    "提现",
    "支付密码",
    "purchase",
    "payment",
    "pay",
    "checkout",
    "place order",
    "top up",
    "withdraw",
    "transfer",
    "pay now",
    # 破坏性操作。
    "删除",
    "删除账号",
    "注销",
    "清空数据",
    "清除数据",
    "卸载",
    "erase",
    "delete",
    "remove",
    "uninstall",
    "clear data",
    # 授权和系统权限确认。
    "确认授权",
    "授予权限",
    "允许权限",
    "权限",
    "同意授权",
    "授权",
    "允许",
    "permission",
    "authorize",
    "authorization",
    "consent",
    "grant",
    "allow",
)
_MEDIUM_IMPACT_TARGET_MARKERS = (
    "保存",
    "提交",
    "发布",
    "发送",
    "save",
    "submit",
    "publish",
    "send",
)
_TARGET_NAVIGATION_MARKERS = (
    "进入",
    "前往",
    "打开",
    "查看",
    "浏览",
    "页面",
    "中心",
    "入口",
    "导航",
    "详情",
    "page",
    "open",
    "view",
    "navigate",
    "screen",
)
_BACK_CONTROL_LABELS = frozenset(
    {
        "左上角返回",
        "左上角返回按钮",
        "返回",
        "返回按钮",
        "返回键",
        "back",
        "backbutton",
        "navigateback",
        "navigateup",
        "up",
        "<",
    }
)
_TARGET_NEGATED_AUTH_MARKERS = (
    "不授权",
    "拒绝授权",
    "暂不授权",
    "下次再",
    "稍后再",
    "deny",
    "decline",
    "not now",
    "later",
)
_RESET_POLICIES = frozenset({"warm_preferred", "force_stop", "clear_data", "none"})


@dataclass(frozen=True, kw_only=True)
class CommandPolicy:
    """单次 Run 使用的命令白名单和能力开关。

    ``allowed_operations``、风险等级和允许主机/包名应由任务运行时装配；
    默认值覆盖现有探索主流程的低风险动作。外部跳转和剪贴板必须通过
    显式能力开关，不接受 Agent 自报字段作为授权来源；故障探针仅接受
    有界 URL 匹配模式，能力门禁由其已有 supplemental policy 负责。
    """

    allowed_operations: Mapping[SurfaceKind, Sequence[InteractionOperation]] = field(
        default_factory=lambda: dict(DEFAULT_ALLOWED_OPERATIONS)
    )
    allowed_risk_levels: tuple[InteractionRiskLevel, ...] = (
        InteractionRiskLevel.LOW,
        InteractionRiskLevel.MEDIUM,
    )
    approved_high_risk_intent_ids: tuple[str, ...] = ()
    allowed_hosts: tuple[str, ...] = ()
    allowed_packages: tuple[str, ...] = ()
    capabilities: frozenset[str] = frozenset()
    minimum_risk_by_operation: Mapping[InteractionOperation, InteractionRiskLevel] = field(
        default_factory=dict
    )
    protocol_version: str = COMMAND_POLICY_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.protocol_version != COMMAND_POLICY_PROTOCOL_VERSION:
            raise CommandPolicyError(
                "命令策略版本不受支持",
                error_code="COMMAND_POLICY_VERSION_UNSUPPORTED",
            )

        normalized_operations: dict[SurfaceKind, tuple[InteractionOperation, ...]] = {}
        for surface_kind, operations in self.allowed_operations.items():
            if not isinstance(surface_kind, SurfaceKind):
                raise CommandPolicyError(
                    "命令策略包含未知 Surface",
                    error_code="COMMAND_POLICY_INVALID",
                )
            if isinstance(operations, (str, bytes, bytearray)):
                raise CommandPolicyError(
                    "allowed_operations 必须为动作集合",
                    error_code="COMMAND_POLICY_INVALID",
                )
            values = tuple(operations)
            if any(not isinstance(item, InteractionOperation) for item in values):
                raise CommandPolicyError(
                    "allowed_operations 包含未知动作",
                    error_code="COMMAND_POLICY_INVALID",
                )
            if len(values) != len(set(values)):
                raise CommandPolicyError(
                    "allowed_operations 不能包含重复动作",
                    error_code="COMMAND_POLICY_INVALID",
                )
            normalized_operations[surface_kind] = values
        object.__setattr__(
            self,
            "allowed_operations",
            MappingProxyType(normalized_operations),
        )

        if not self.allowed_risk_levels or any(
            not isinstance(item, InteractionRiskLevel) for item in self.allowed_risk_levels
        ):
            raise CommandPolicyError(
                "allowed_risk_levels 必须为非空风险等级集合",
                error_code="COMMAND_POLICY_INVALID",
            )
        if len(self.allowed_risk_levels) != len(set(self.allowed_risk_levels)):
            raise CommandPolicyError(
                "allowed_risk_levels 不能包含重复等级",
                error_code="COMMAND_POLICY_INVALID",
            )

        approved = _normalize_text_tuple(
            self.approved_high_risk_intent_ids,
            field_name="approved_high_risk_intent_ids",
        )
        hosts = _normalize_text_tuple(self.allowed_hosts, field_name="allowed_hosts")
        packages = _normalize_text_tuple(
            self.allowed_packages,
            field_name="allowed_packages",
        )
        capabilities = frozenset(
            _normalize_text_tuple(self.capabilities, field_name="capabilities")
        )
        if not isinstance(self.minimum_risk_by_operation, Mapping):
            raise CommandPolicyError(
                "minimum_risk_by_operation 必须为动作风险映射",
                error_code="COMMAND_POLICY_INVALID",
            )
        minimum_risk = dict(self.minimum_risk_by_operation)
        for operation, risk_level in minimum_risk.items():
            if not isinstance(operation, InteractionOperation) or not isinstance(
                risk_level, InteractionRiskLevel
            ):
                raise CommandPolicyError(
                    "minimum_risk_by_operation 包含未知动作或风险等级",
                    error_code="COMMAND_POLICY_INVALID",
                )
        object.__setattr__(self, "approved_high_risk_intent_ids", approved)
        object.__setattr__(self, "allowed_hosts", hosts)
        object.__setattr__(self, "allowed_packages", packages)
        object.__setattr__(self, "capabilities", capabilities)
        object.__setattr__(
            self,
            "minimum_risk_by_operation",
            MappingProxyType(minimum_risk),
        )

    @classmethod
    def default(cls) -> CommandPolicy:
        """返回当前正式动作类型的安全默认策略。"""

        return cls()

    def validate(self, intent: ActionIntent) -> None:
        """在任何 Capture、Gate 或 Driver 调用前验证一条动作意图。"""

        if not isinstance(intent, ActionIntent):
            raise CommandPolicyError(
                "命令必须是 ActionIntent",
                error_code="COMMAND_INTENT_INVALID",
            )
        allowed = self.allowed_operations.get(intent.surface_kind, ())
        if intent.action_type not in allowed:
            raise CommandPolicyError(
                f"{intent.surface_kind.value} 不允许执行 {intent.action_type.value}",
                error_code="COMMAND_OPERATION_NOT_ALLOWED",
            )
        if intent.risk_level not in self.allowed_risk_levels:
            raise CommandPolicyError(
                "动作风险等级不在当前任务允许范围内",
                error_code="COMMAND_RISK_NOT_ALLOWED",
            )
        reset_policy = _reset_policy_for_intent(intent)
        minimum_risk = self.minimum_risk_by_operation.get(intent.action_type)
        if (
            intent.action_type is InteractionOperation.RESET_APP
            and reset_policy == "clear_data"
            and (
                minimum_risk is None
                or _risk_rank(minimum_risk) < _risk_rank(InteractionRiskLevel.HIGH)
            )
        ):
            # 清数据会轮换 App 数据容器并丢失登录/本地状态，不得沿用
            # force_stop/warm_preferred 的 medium 最低等级。
            minimum_risk = InteractionRiskLevel.HIGH
        if minimum_risk is not None and _risk_rank(intent.risk_level) < _risk_rank(minimum_risk):
            raise CommandPolicyError(
                "动作自报风险低于服务端为该动作锁定的最低等级",
                error_code="COMMAND_RISK_UNDERRATED",
            )
        if intent.risk_level is InteractionRiskLevel.HIGH and (
            intent.intent_id not in self.approved_high_risk_intent_ids
            and "*" not in self.approved_high_risk_intent_ids
        ):
            raise CommandPolicyError(
                "高风险动作缺少服务端审批",
                error_code="COMMAND_HIGH_RISK_APPROVAL_REQUIRED",
            )

        self._validate_parameters(intent)

    def validate_resolved_target(
        self,
        intent: ActionIntent,
        *,
        trusted_target_texts: Sequence[str | None] = (),
        target_trust: str | None = None,
        target_role: str | None = None,
    ) -> InteractionRiskLevel | None:
        """按平台解析后的控件语义复核风险，防止 Agent 把危险动作报成 low。

        ``trusted_target_texts`` 必须来自平台保存的视觉候选或 UI Snapshot，
        不能把 Agent 原始参数当成可信目标。没有可信目标文本时，仅对明显的
        Agent 危险语义做保守拦截；无法证明危险不等于证明安全，正式 Web
        入口应补齐 Snapshot 目标来源后再调用本方法。
        """

        if not isinstance(intent, ActionIntent):
            raise CommandPolicyError(
                "命令必须是 ActionIntent",
                error_code="COMMAND_INTENT_INVALID",
            )
        if target_trust is not None and target_trust not in TRUSTED_TARGET_SOURCES:
            raise CommandPolicyError(
                "目标来源不是受支持的可信来源",
                error_code="COMMAND_TARGET_TRUST_INVALID",
            )
        if intent.action_type in {InteractionOperation.BACK, InteractionOperation.OBSERVE}:
            # 返回和观察都没有待触发的目标控件，说明中提及允许／支付不代表执行授权或支付。
            # 操作白名单、风险等级、参数及作用域仍由既有命令策略分别校验。
            return None
        normalized_trusted = _normalize_target_texts(trusted_target_texts)
        normalized_role = _normalize_target_text(target_role)
        inferred = infer_resolved_target_risk(
            trusted_target_texts=normalized_trusted,
            fallback_target_texts=(
                intent.semantic_target,
                intent.purpose,
                _optional_text(intent.parameters.get("submit_semantic_target")),
            )
            if not normalized_trusted
            else (),
            target_role=normalized_role,
        )
        if _hjll_recorded_payment_tap(intent):
            # 海角錄製腳本的支付點。任務命令範圍只有 low/medium，
            # 升成 HIGH 會被服務端直接拒絕，點永遠到不了手機。
            # 只放行腳本自己簽發的 tag 和原文案，模型另報的支付點仍然拒絕。
            if _risk_rank(intent.risk_level) < _risk_rank(InteractionRiskLevel.MEDIUM):
                raise CommandPolicyError(
                    "海角录制支付点至少要报 medium",
                    error_code="COMMAND_TARGET_RISK_UNDERRATED",
                )
            return InteractionRiskLevel.MEDIUM
        if inferred is None:
            return None
        if not normalized_trusted and target_trust is None:
            # 旧 Web/ADB 若尚未把解析后的控件语义接入，遇到明显危险文案
            # 只能拒绝，不能把 Agent 自报的 low 当作授权继续执行。
            raise CommandPolicyError(
                "危险目标缺少平台解析语义，禁止按 Agent 自报风险执行",
                error_code="COMMAND_TARGET_SEMANTICS_UNTRUSTED",
            )
        if _risk_rank(intent.risk_level) < _risk_rank(inferred):
            raise CommandPolicyError(
                "动作自报风险低于平台解析目标的实际副作用",
                error_code="COMMAND_TARGET_RISK_UNDERRATED",
            )
        if intent.risk_level is InteractionRiskLevel.HIGH and (
            intent.intent_id not in self.approved_high_risk_intent_ids
            and "*" not in self.approved_high_risk_intent_ids
        ):
            raise CommandPolicyError(
                "高风险目标动作缺少服务端审批",
                error_code="COMMAND_HIGH_RISK_APPROVAL_REQUIRED",
            )
        return inferred

    def validate_runtime_location(
        self,
        *,
        surface_kind: SurfaceKind,
        url: str | None = None,
        package_name: str | None = None,
    ) -> None:
        """校验 Driver 观察到的当前来源，不能只校验请求目标。

        页面实际重定向后由 Runtime 调用该方法；调用方不能以 Agent 提供的
        ``parameters`` 替代真实 URL/包名。跨来源地址没有任务快照授权时默认
        拒绝，防止初始 URL 重定向后继续在未知页面操作。
        """

        if not isinstance(surface_kind, SurfaceKind):
            raise CommandPolicyError(
                "运行时来源缺少有效 Surface",
                error_code="COMMAND_RUNTIME_LOCATION_INVALID",
            )
        if surface_kind is SurfaceKind.WEB:
            if not isinstance(url, str) or not url.strip():
                raise CommandPolicyError(
                    "Web 运行时来源缺少 URL",
                    error_code="COMMAND_RUNTIME_LOCATION_INVALID",
                )
            _validate_destination(url.strip(), allowed_hosts=self.allowed_hosts)
            return
        if self.allowed_packages:
            if not isinstance(package_name, str) or not package_name.strip():
                raise CommandPolicyError(
                    "App 运行时来源缺少包名",
                    error_code="COMMAND_RUNTIME_LOCATION_INVALID",
                )
            if package_name.strip().casefold() not in {
                item.casefold() for item in self.allowed_packages
            }:
                raise CommandPolicyError(
                    "App 运行时包名超出当前任务边界",
                    error_code="COMMAND_PACKAGE_BOUNDARY_VIOLATION",
                )

    def _validate_parameters(self, intent: ActionIntent) -> None:
        parameters = intent.parameters
        forbidden = _find_forbidden_parameter_key(parameters)
        if forbidden is not None:
            raise CommandPolicyError(
                f"命令参数包含禁止字段：{forbidden}",
                error_code="COMMAND_PARAMETER_FORBIDDEN",
            )

        capability = _CAPABILITY_PARAMETER_BY_OPERATION.get(intent.action_type)
        if capability is not None and capability not in self.capabilities:
            raise CommandPolicyError(
                f"动作 {intent.action_type.value} 缺少服务端能力授权",
                error_code="COMMAND_CAPABILITY_NOT_ALLOWED",
            )

        if intent.action_type is InteractionOperation.OPEN_EXTERNAL and (
            parameters.get("authorized") is not True
        ):
            raise CommandPolicyError(
                "外部跳转必须携带服务端批准标记",
                error_code="COMMAND_EXTERNAL_APPROVAL_REQUIRED",
            )

        if intent.action_type is InteractionOperation.NAVIGATE:
            destination = parameters.get("destination_url")
            if not isinstance(destination, str) or not destination.strip():
                raise CommandPolicyError(
                    "导航动作缺少 destination_url",
                    error_code="COMMAND_DESTINATION_REQUIRED",
                )
            _validate_destination(
                destination.strip(),
                allowed_hosts=self.allowed_hosts,
            )
        elif self.allowed_hosts and "destination_url" in parameters:
            destination = parameters.get("destination_url")
            if not isinstance(destination, str) or not destination.strip():
                raise CommandPolicyError(
                    "destination_url 必须为非空字符串",
                    error_code="COMMAND_DESTINATION_INVALID",
                )
            _validate_destination(
                destination.strip(),
                allowed_hosts=self.allowed_hosts,
            )

        if self.allowed_packages and "package_name" in parameters:
            package_name = parameters.get("package_name")
            if not isinstance(package_name, str) or not package_name.strip():
                raise CommandPolicyError(
                    "package_name 必须为非空字符串",
                    error_code="COMMAND_PACKAGE_INVALID",
                )
            if package_name.strip().casefold() not in {
                item.casefold() for item in self.allowed_packages
            }:
                raise CommandPolicyError(
                    "package_name 超出当前任务包边界",
                    error_code="COMMAND_PACKAGE_BOUNDARY_VIOLATION",
                )

        if intent.action_type is InteractionOperation.NETWORK_FAULT:
            pattern = parameters.get("fault_url_pattern")
            if not isinstance(pattern, str) or not pattern.strip() or len(pattern) > 512:
                raise CommandPolicyError(
                    "network_fault 必须携带有界 fault_url_pattern",
                    error_code="COMMAND_FAULT_PATTERN_INVALID",
                )


_HJLL_RECORDED_PAYMENT_LABELS = {
    "pay-now": "立即支付",
    "pay-now-2": "立即支付",
    "pay-success": "支付成功",
    "pay-back": "返回暗网",
}


def _hjll_recorded_payment_tap(intent: ActionIntent) -> bool:
    """只認海角編排器簽發的支付鏈落點，不認其它自報支付。"""

    parameters = intent.parameters
    if parameters.get("hjll_script") is not True:
        return False
    tag = parameters.get("hjll_script_tag")
    if not isinstance(tag, str):
        return False
    expected = _HJLL_RECORDED_PAYMENT_LABELS.get(tag)
    return expected is not None and intent.semantic_target == expected


def infer_resolved_target_risk(
    *,
    trusted_target_texts: Sequence[str | None] = (),
    fallback_target_texts: Sequence[str | None] = (),
    target_role: str | None = None,
) -> InteractionRiskLevel | None:
    """从受信控件语义推断最低动作风险。

    ``fallback_target_texts`` 只应传入未解析到 Snapshot 时的原始语义，且
    结果只能用于保守拒绝，不能用于证明目标安全。该函数不访问外部状态，
    便于 Web DOM、Flutter Semantics 和 App 视觉候选共用同一套判定。
    """

    trusted = _normalize_target_texts(trusted_target_texts)
    fallback = _normalize_target_texts(fallback_target_texts)
    role = _normalize_target_text(target_role)
    if role in {
        "dismiss",
        "skip",
        "profile_credential_confirm",
        "back",
        "navigate_back",
    }:
        # 平台已经确认这是清障／返回控件时，不把弹窗或套餐页背景中的
        # “支付/授权”文案误算成当前点击的副作用。
        return InteractionRiskLevel.LOW
    if any(
        _normalize_target_text(part) in _BACK_CONTROL_LABELS
        for part in (trusted or ())
    ):
        # 左上角「<」／返回按钮本身无资金副作用；purpose 里写「禁止点支付」
        # 不得把这次点击抬成 MEDIUM/HIGH。
        return InteractionRiskLevel.LOW
    source = trusted or fallback
    if not source:
        return None
    # 只消除当前文本中的明确拒绝短语，不能因为一个控件写着“不授权”，
    # 就连另一个提交控件的“确认授权”一起忽略。复合文本中的支付/删除也必须保留。
    remaining = []
    for part in source:
        for marker in sorted(_TARGET_NEGATED_AUTH_MARKERS, key=len, reverse=True):
            part = part.replace(_normalize_target_text(marker), "")
        remaining.append(part)
    combined = " ".join(remaining)
    if _contains_high_impact_marker(combined):
        return InteractionRiskLevel.HIGH
    if _contains_navigation_sensitive_marker(combined):
        # 进入支付/授权页面本身不是资金或权限提交，但也不能被模型降成
        # 普通 low click；它至少需要 medium 级别的任务意图。
        return InteractionRiskLevel.MEDIUM
    if _contains_medium_impact_marker(combined):
        return InteractionRiskLevel.MEDIUM
    return None


def _reset_policy_for_intent(intent: ActionIntent) -> str | None:
    if intent.action_type is not InteractionOperation.RESET_APP:
        return None
    value = intent.parameters.get("reset_policy", "force_stop")
    if not isinstance(value, str) or not value.strip():
        raise CommandPolicyError(
            "reset_policy 必须为非空字符串",
            error_code="COMMAND_RESET_POLICY_INVALID",
        )
    normalized = value.strip().casefold()
    if normalized not in _RESET_POLICIES:
        raise CommandPolicyError(
            "reset_policy 仅支持 warm_preferred/force_stop/clear_data/none",
            error_code="COMMAND_RESET_POLICY_INVALID",
        )
    return normalized


def _contains_high_impact_marker(value: str) -> bool:
    if not value:
        return False
    navigation_only = _contains_any_marker(value, _TARGET_NAVIGATION_MARKERS)
    for marker in _HIGH_IMPACT_TARGET_MARKERS:
        if not _contains_marker(value, marker):
            continue
        # “进入支付页面/查看授权页面”只是在导航，不是提交资金或授予权限；
        # 但购买、删除、充值等明确副作用词不受导航上下文豁免。
        if marker in {
            "支付",
            "payment",
            "pay",
            "授权",
            "权限",
            "允许",
            "authorization",
            "authorize",
            "allow",
        }:
            if navigation_only and not _contains_action_verb(value, marker):
                continue
        return True
    return False


def _contains_medium_impact_marker(value: str) -> bool:
    return any(_contains_marker(value, marker) for marker in _MEDIUM_IMPACT_TARGET_MARKERS)


def _contains_navigation_sensitive_marker(value: str) -> bool:
    if not _contains_any_marker(value, _TARGET_NAVIGATION_MARKERS):
        return False
    return any(
        _contains_marker(value, marker)
        for marker in ("支付", "payment", "授权", "authorization", "authorize")
    )


def _contains_action_verb(value: str, marker: str) -> bool:
    del marker
    return _contains_any_marker(
        value,
        (
            "立即",
            "确认",
            "去",
            "提交",
            "下单",
            "购买",
            "充值",
            "付款",
            "pay now",
            "checkout",
            "purchase",
            "submit",
        ),
    )


def _contains_any_marker(value: str, markers: Sequence[str]) -> bool:
    return any(_contains_marker(value, marker) for marker in markers)


def _contains_marker(value: str, marker: str) -> bool:
    if any("\u4e00" <= character <= "\u9fff" for character in marker):
        return marker in value
    compact_marker = re.sub(r"[^a-z0-9]+", "", marker.casefold())
    if not compact_marker:
        return False
    compact_value = re.sub(r"[^a-z0-9]+", "", value.casefold())
    return compact_marker in compact_value


def _normalize_target_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(normalized.split())


def _normalize_target_texts(values: Sequence[str | None]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes, bytearray)):
        raise CommandPolicyError(
            "目标语义必须为字符串集合",
            error_code="COMMAND_TARGET_SEMANTICS_INVALID",
        )
    normalized: list[str] = []
    for value in values:
        if value is None:
            continue
        if not isinstance(value, str):
            raise CommandPolicyError(
                "目标语义必须为字符串或空值",
                error_code="COMMAND_TARGET_SEMANTICS_INVALID",
            )
        text = _normalize_target_text(value)
        if text and text not in normalized:
            normalized.append(text)
    return tuple(normalized)


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _find_forbidden_parameter_key(value: object, *, _path: str = "parameters") -> str | None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if isinstance(key, str) and key.casefold() in _FORBIDDEN_PARAMETER_KEYS:
                return f"{_path}.{key}"
            nested = _find_forbidden_parameter_key(child, _path=f"{_path}.{key}")
            if nested is not None:
                return nested
    elif isinstance(value, (tuple, list)):
        for index, child in enumerate(value):
            nested = _find_forbidden_parameter_key(child, _path=f"{_path}[{index}]")
            if nested is not None:
                return nested
    return None


def _risk_rank(risk_level: InteractionRiskLevel) -> int:
    return {
        InteractionRiskLevel.LOW: 1,
        InteractionRiskLevel.MEDIUM: 2,
        InteractionRiskLevel.HIGH: 3,
    }[risk_level]


def _validate_destination(destination: str, *, allowed_hosts: Sequence[str]) -> None:
    try:
        parsed = urlsplit(destination)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise CommandPolicyError(
            "导航目标 URL 无效",
            error_code="COMMAND_DESTINATION_INVALID",
        ) from exc
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or any(char.isspace() for char in destination)
    ):
        raise CommandPolicyError(
            "导航目标必须是无凭据的 HTTP(S) URL",
            error_code="COMMAND_DESTINATION_INVALID",
        )
    del port  # 访问端口的限制由 allowed_hosts 的上层任务边界决定。
    if allowed_hosts and hostname.casefold() not in {item.casefold() for item in allowed_hosts}:
        raise CommandPolicyError(
            "导航目标主机超出当前任务边界",
            error_code="COMMAND_HOST_BOUNDARY_VIOLATION",
        )


def _normalize_text_tuple(
    values: Sequence[str] | frozenset[str], *, field_name: str
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes, bytearray)):
        raise CommandPolicyError(
            f"{field_name} 必须为字符串集合",
            error_code="COMMAND_POLICY_INVALID",
        )
    result = tuple(values)
    if any(
        not isinstance(item, str) or not item.strip() or item != item.strip() for item in result
    ):
        raise CommandPolicyError(
            f"{field_name} 必须为无首尾空白的非空字符串集合",
            error_code="COMMAND_POLICY_INVALID",
        )
    if len(result) != len(set(result)):
        raise CommandPolicyError(
            f"{field_name} 不能包含重复值",
            error_code="COMMAND_POLICY_INVALID",
        )
    return result


__all__ = [
    "COMMAND_POLICY_PROTOCOL_VERSION",
    "COMMAND_PARAMETER_FORBIDDEN_KEYS",
    "DEFAULT_ALLOWED_OPERATIONS",
    "SERVER_MINIMUM_RISK_BY_OPERATION",
    "TRUSTED_TARGET_SOURCES",
    "CommandPolicy",
    "CommandPolicyError",
    "infer_resolved_target_risk",
]
