"""绿色八条第4版目录；仅构造新版本，不发布、迁移或覆盖历史规则。"""

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from services.api.app.application.green_rule_skills import (
    build_green_skill_program,
    validate_frozen_green_skill_program,
)
from services.api.app.domain.page.registry import get_default_page_type_registry

GREEN_RULESET_CODE = "alioth_green_ruleset"
GREEN_RULESET_VERSION = 4
GREEN_CONTRACT_VERSION = "green-rule-contract.v2"
GREEN_INTERACTION_SCOPE_KEY = "green_interaction_stability.v1"
GREEN_TEMPORAL_SCOPE_KEY = "green_temporal_rules.v1"
GREEN_RULE_SOURCE_URL = (
    "https://docs.google.com/spreadsheets/d/1BzFpZYqmELVvo58VYFng2Qw8XwF29iYVH3Eri9XK12M/"
    "edit?gid=1730171659"
)


class GreenRuleConfigurationError(ValueError):
    """显式绿色合同与其目录身份、范围或冻结程序不一致。"""


@dataclass(frozen=True)
class GreenRuleDefinition:
    key: str
    source_row: int
    source_spec: str
    name: str
    category: str
    input_kind: str
    subchecks: tuple[str, ...]
    check_point: str
    evidence_boundary: str

    @property
    def rule_code(self) -> str:
        return "alioth_green_" + self.key

    @property
    def target_clients(self) -> list[str]:
        return ["APP"] if self.key == "41" else ["WEB", "H5", "APP"]


GREEN_RULES = (
    GreenRuleDefinition(
        "102",
        13,
        "102",
        "内容安全三类风险与实际覆盖检查",
        "compliance",
        "page",
        ("minor_safety", "political_safety", "terrorism_safety", "content_scope"),
        "首页及一级目录中的等价首页、首页所有内容列表；其他内容列表前两屏按原文19:9范围"
        "扣除固定功能和导航。排除广告，图文联合，涉未、涉政、涉恐独立输出结果、置信度、"
        "触发证据和复核建议。",
        "保留原图、可识别文字、内容范围及实际覆盖、广告排除来源。不得猜精确年龄；年龄或类别"
        "不确定进入复核。普通战争、枪械元素不能单独证明涉恐；具体红线政策引用受控版本。",
    ),
    GreenRuleDefinition(
        "s72",
        14,
        "S72",
        "空数据与加载失败呈现检查",
        "visual",
        GREEN_TEMPORAL_SCOPE_KEY,
        ("empty_state_meaning", "load_failure_meaning"),
        "同一模块、同次请求周期，真实空数据有暂无数据类说明；加载失败应引导刷新或重试，"
        "不能把失败显示为空。",
        "唯一关联真实请求结果与模块呈现。空态可以有插图或引导；普通刷新功能存在本身不证明"
        "状态错误。HTTP200、空白截图不能单独证明空数据；未知保留未验证。",
    ),
    GreenRuleDefinition(
        "41",
        15,
        "41",
        "App外链由系统或第三方浏览器承接检查",
        "interaction",
        "page",
        ("external_browser_handoff",),
        "App点击已知外部链接后，应由系统或第三方浏览器承接，不在App内置WebView承接。",
        "同一正式外链动作的前后截图、目标及前后台应用或组件身份。截图变化、同包、剪贴板URL"
        "不能证明承接类型。仅App适用；缺真实设备验收不得声明能力已验证。不是旧alioth_41。",
    ),
    GreenRuleDefinition(
        "62",
        16,
        "62",
        "广告正常结束及关闭后正片衔接检查",
        "ad",
        GREEN_TEMPORAL_SCOPE_KEY,
        ("natural_end_handoff", "manual_close_handoff", "content_preload", "presented_first_frame"),
        "有广告时，正常结束或手动关闭后与正片顺畅衔接，包含正片预加载；自然结束、手动关闭"
        "分别覆盖。",
        "同一广告与正片身份、真实结束或关闭、预加载、实际呈现首帧和中间过程。呈现事件不能"
        "冒充首帧PNG。广告加载失败兜底是独立旧场景；缺完整过程不能据末帧通过。",
    ),
    GreenRuleDefinition(
        "s35",
        17,
        "S35",
        "交互响应布局与可比前态稳定性检查",
        "interaction",
        GREEN_INTERACTION_SCOPE_KEY,
        ("correct_response", "usable_layout", "comparable_state_stability"),
        "分项检查点击响应正确、列表或弹窗无影响使用的错位重叠遮挡，以及同一操作在可核验的"
        "相同前置条件下表现稳定。",
        "正式独立Action及前后和过程、真实反馈因果、命中及布局事实、可比前态。不同前态不能"
        "作不稳定反例；合法蒙层叠放不自动违规。危险或不可安全重复的操作保留一致性未验证。",
    ),
    GreenRuleDefinition(
        "s49",
        18,
        "S49",
        "真实异常原因恢复路径与支付保护检查",
        "interaction",
        GREEN_TEMPORAL_SCOPE_KEY,
        (
            "failure_reason",
            "retry_entry",
            "business_recovery",
            "video_line_recovery",
            "account_payment_support",
            "payment_not_interrupted",
        ),
        "实际断网、加载失败、超时时说明原因并提供重试或刷新路径；视频异常有切线恢复路径，"
        "账号支付异常有客服路径，支付关键过程不被客服打断。",
        "当前模块真实异常、原因与入口、可信业务类型及支付阶段。页面自报普通内容不能免除"
        "视频、账号、支付义务；分类或阶段未知保留缺证。入口存在不等于实际打断，入口可用与"
        "重试后业务恢复分别记录；不提交真实支付、不联系客户。",
    ),
    GreenRuleDefinition(
        "s73",
        19,
        "S73",
        "加载空态与真实业务成功反馈检查",
        "interaction",
        GREEN_TEMPORAL_SCOPE_KEY,
        ("loading_feedback", "empty_state_guidance", "business_success_feedback"),
        "加载过程有进度反馈；真实空态有插图或有效引导；真实业务成功后有本次明确提示或动效。"
        "分项按实际条件报告。",
        "完整加载过程、真实空态、可信业务结果及本次新反馈。Driver执行成功、HTTP200、常驻"
        "旧Toast不是业务成功证明；受控响应合同、业务状态或明确回执无法验证时保留未知。",
    ),
    GreenRuleDefinition(
        "32",
        20,
        "32",
        "同级分类导航分布文字与初始露出检查",
        "visual",
        "page",
        (
            "equal_width_navigation",
            "fit_navigation_spacing",
            "overflow_scroll",
            "initial_next_label",
        ),
        "同级2—4类在导航可用区域等宽分布；5类以上完整容纳时文字完整且间距均衡，否则横向"
        "滚动；初始右侧还有分类时露出下一项部分文字。",
        "完整分类及容器、真实文字边界、视口、缩放、滚动位置和初始状态。不能以中间滚动帧"
        "代替初始状态，不能用不完整枚举或空数组证明产品没有导航。",
    ),
)
_DEFINITIONS = {rule.key: rule for rule in GREEN_RULES}


def build_green_rule_payload(
    rule_key: str,
    *,
    skills_root: Path | None = None,
    shared_bindings: Mapping | None = None,
    content_policies: Mapping | None = None,
    content_scope_contract: Mapping | None = None,
) -> dict:
    """构造未绑定数据库规则集身份的新规则；所有条目默认关闭。"""
    definition = _definition(rule_key)
    scope = "page" if definition.input_kind == "page" else "task"
    pages = (
        ["home", "content_list", "video_list", "post_list"]
        if rule_key == "102"
        else list(get_default_page_type_registry().business_codes)
    )
    payload = {
        "rule_code": definition.rule_code,
        "name": definition.name,
        "description": f"来源：9月新增规则优先级物理第{definition.source_row}行，"
        f"规范{definition.source_spec}。{definition.evidence_boundary}",
        "category": definition.category,
        "executor_type": "hybrid",
        "severity": "medium",
        "status": "disabled",
        "scope": "all_product_clients",
        "channel": None,
        "page_type": "",
        "applicable_page_types": pages,
        "url_pattern": "",
        "steps": [],
        "check_point": definition.check_point,
        "input_contract": {
            "schema_version": "1.1",
            "target_clients": definition.target_clients,
            "required_artifacts": ["screenshot"],
            "optional_artifacts": [
                "ui_snapshot",
                "visible_text",
                "action_trace",
                "har",
                "media_state",
                "temporal_trace",
            ],
            "required_fields": [],
            "quality_gate": {"allow_empty_visible_text": True},
        },
        "judge_config": {
            "schema_version": "1.1",
            "thresholds": {},
            "llm_checks": [definition.check_point],
            "tool_refs": ["semantic_rule_judge_tool"],
            "capability_config": {
                "schema_version": "1.0",
                "evidence_input_scope": scope,
                "evidence_scope_key": definition.input_kind,
                "green_rule_key": definition.key,
                "requires_journey": False,
                "required_artifact_types": ["screenshot"],
                "deterministic_tools": ["green_rule_evidence_tool"],
                "semantic_judge": "semantic_rule_judge_tool",
                "evidence_source": "observation_packet",
            },
            "green_rule_contract": _contract(definition),
            "green_skill_program": build_green_skill_program(rule_key, skills_root=skills_root),
        },
        "manual_review_policy": {
            "schema_version": "1.0",
            "min_confidence": 0.75,
            "reviewer_group": "qc_rule_owner",
            "triggers": ["evidence_insufficient", "model_uncertainty"],
        },
        "sort_order": (definition.source_row - 12) * 10,
    }
    if rule_key != "102" and (content_policies is not None or content_scope_contract is not None):
        raise GreenRuleConfigurationError("受控内容政策和范围仅适用于102")
    for key, value in (
        ("green_shared_bindings", shared_bindings),
        ("green_content_policies", content_policies),
        ("green_content_scope_contract", content_scope_contract),
    ):
        if value is not None:
            payload["judge_config"][key] = deepcopy(value)
    validate_green_rule_configuration(payload)
    return payload


def build_green_ruleset_manifest(
    *, version: int = GREEN_RULESET_VERSION, skills_root: Path | None = None
) -> dict:
    """只构造第4版发布候选，不借此安装或覆盖旧green第1—3版。"""
    if type(version) is not int or version != GREEN_RULESET_VERSION:
        raise GreenRuleConfigurationError("此构造器仅支持绿色第4版，禁止改写历史版本")
    return {
        "ruleset": {
            "ruleset_code": GREEN_RULESET_CODE,
            "name": "绿色八条规则",
            "version": version,
            "status": "draft",
            "description": "绿色规则三端实施规范第4版；新规则默认关闭。",
            "source_type": "spreadsheet",
            "source_ref": GREEN_RULE_SOURCE_URL,
        },
        "rules": [
            build_green_rule_payload(rule.key, skills_root=skills_root) for rule in GREEN_RULES
        ],
    }


def validate_green_rule_configuration(rule: object) -> dict | None:
    """仅显式新合同启用；只持有judge_config的路由也可核验冻结程序与范围。"""
    config = rule.get("judge_config") if isinstance(rule, Mapping) else None
    if not isinstance(config, Mapping) or "green_rule_contract" not in config:
        return None
    contract = config["green_rule_contract"]
    if (
        isinstance(contract, Mapping)
        and contract.get("schema_version") == "green-rule-contract.v1"
        and type(contract.get("ruleset_version")) is int
        and contract["ruleset_version"] in (1, 2, 3)
    ):
        # 已知旧版本交回旧执行链，不注入第4版程序，不读当前磁盘升级历史定义。
        return None
    if (
        not isinstance(contract, Mapping)
        or contract.get("schema_version") != GREEN_CONTRACT_VERSION
    ):
        raise GreenRuleConfigurationError("绿色规则合同版本不受支持")
    definition = _definition(contract.get("rule_key"))
    if dict(contract) != _contract(definition):
        raise GreenRuleConfigurationError("绿色合同的规则版本、来源或分项与第4版目录不一致")
    for field, expected in {
        "rule_code": definition.rule_code,
        "category": definition.category,
        "executor_type": "hybrid",
    }.items():
        if field in rule and rule[field] != expected:
            raise GreenRuleConfigurationError("绿色规则身份或执行类别与显式合同不一致")
    inputs = rule.get("input_contract")
    if inputs is not None and (
        not isinstance(inputs, Mapping) or inputs.get("target_clients") != definition.target_clients
    ):
        raise GreenRuleConfigurationError("绿色规则适用端与合同不一致")
    capability = config.get("capability_config")
    if not isinstance(capability, Mapping) or any(
        capability.get(field) != expected
        for field, expected in {
            "evidence_input_scope": "page" if definition.input_kind == "page" else "task",
            "evidence_scope_key": definition.input_kind,
            "green_rule_key": definition.key,
            "requires_journey": False,
        }.items()
    ):
        raise GreenRuleConfigurationError("绿色规则输入范围与目录不一致")
    validate_frozen_green_skill_program(config.get("green_skill_program"), rule_key=definition.key)
    if "green_shared_bindings" in config:
        from services.api.app.application.green_fact_bindings import validate_green_shared_bindings

        validate_green_shared_bindings(config["green_shared_bindings"])
    if "green_content_policies" in config or "green_content_scope_contract" in config:
        from services.api.app.application.green_content_judge import (
            validate_content_policies,
            validate_content_scope_contract,
        )

        validate_content_policies(config.get("green_content_policies"))
        validate_content_scope_contract(config.get("green_content_scope_contract"))
    return deepcopy(dict(contract))


def _contract(definition: GreenRuleDefinition) -> dict:
    return {
        "schema_version": GREEN_CONTRACT_VERSION,
        "ruleset_version": GREEN_RULESET_VERSION,
        "rule_key": definition.key,
        "source_row": definition.source_row,
        "source_spec": definition.source_spec,
        "input_kind": definition.input_kind,
        "subchecks": list(definition.subchecks),
        "task_scope_is_coverage": False,
    }


def _definition(rule_key) -> GreenRuleDefinition:
    if not isinstance(rule_key, str) or rule_key not in _DEFINITIONS:
        raise GreenRuleConfigurationError("未声明的绿色规则标识")
    return _DEFINITIONS[rule_key]
