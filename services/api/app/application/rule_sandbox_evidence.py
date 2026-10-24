"""规则测试沙箱的人工证据：组装成与正式采集同形的证据来源，并说明规则要什么证据。

人工证据只活在单次测试请求的内存里。组装出的 ObservationSource 与
qc_capture + qc_evidence_artifact 读回的形状一致，交给 ObservationPacketService
走与正式任务同一套观察包组装、质量门禁与展示层评估。
"""

from __future__ import annotations

import json
from collections.abc import Collection
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import uuid4

from services.api.app.application.observation_packet import (
    ObservationArtifact,
    ObservationSource,
)
from services.api.app.application.rule_judge import (
    RULE_ENGINE_EXECUTOR,
    resolve_rule_execution_route,
)
from services.api.app.application.task_progress import humanize_qc_text

# 与判定送模型的截图上限一致（observation_packet 只把前 6 张交给模型）
MAX_SANDBOX_SCREENSHOTS = 6

SCREENSHOT_ARTIFACT_PREFIX = "screenshot-"
VISIBLE_TEXT_ARTIFACT_ID = "visible-text"
DOM_ARTIFACT_ID = "dom"
UI_SNAPSHOT_ARTIFACT_ID = "ui-snapshot"
BEFORE_SCREENSHOT_ARTIFACT_ID = "before-screenshot"
SECOND_LOCATION_TEXT_ARTIFACT_ID = "location-1-text"

# 正式任务里另一组采集的角色名：操作前页面、第二个价格位置
BEFORE_ROLE = "before"
SECOND_LOCATION_ROLE = "location_1"

# 测试页能直接提供的证据类型；其余类型只能来自真实采集
FORM_SUPPORTED_ARTIFACT_TYPES = frozenset({"screenshot", "visible_text", "dom", "ui_snapshot"})

# 纯视觉审阅可缺少辅助结构或 OCR，但原规则义务仍须交给模型判断是否为必要事实。
RELAXABLE_VISUAL_ARTIFACT_TYPES = FORM_SUPPORTED_ARTIFACT_TYPES | {"ui_tree", "ocr_text"}


@dataclass(frozen=True, kw_only=True)
class SandboxExtraInput:
    """正式任务里另一组采集才有的证据，测试页单独放一个输入框。"""

    role: str
    # screenshot：一张截图；text：一段页面文字
    kind: str
    label: str
    hint: str


EXTRA_INPUTS = {
    BEFORE_ROLE: SandboxExtraInput(
        role=BEFORE_ROLE,
        kind="screenshot",
        label="操作前的截图",
        hint="这条规则拿「操作前」「操作后」各一张截图比有没有变化：这里放操作前那张，"
        "页面截图里放操作后那张（只放 1 张）",
    ),
    SECOND_LOCATION_ROLE: SandboxExtraInput(
        role=SECOND_LOCATION_ROLE,
        kind="text",
        label="第二个位置的页面文字",
        hint="这条规则拿同一商品在两个位置的价格比：页面文字填第一个位置的，这里贴第二个位置的；"
        "商品名和价格要在同一行（如「月卡 ¥30」），两边商品名一样才对得上",
    ),
}

# 正式证据类型 → 大白话名称（结果、选项、缺口说明共用）
EVIDENCE_TYPE_LABELS = {
    "screenshot": "页面截图",
    "visible_text": "页面文字",
    "dom": "页面源码（HTML）",
    "ui_snapshot": "界面快照（页面元素结构）",
    "ui_tree": "界面快照（App 元素树）",
    "har": "网络请求记录",
    "network_trace": "网络请求记录",
    "console_log": "浏览器控制台日志",
    "action_trace": "操作记录",
    "media_state": "播放器状态",
    "temporal_trace": "连续观察记录",
    "page_coverage": "页面覆盖记录",
    "device_log": "设备日志",
    "fault_artifact": "闪退 / 卡死记录",
    "ocr_text": "图片识别文字",
}

# 与 observation_packet._ARTIFACT_ALIASES 保持一致的历史别名
_ARTIFACT_ALIASES = {
    "dom_snapshot": "dom",
    "network_log": "har",
    "network_events": "har",
    "network_proxy": "network_trace",
}

# 质量门禁 missing_sources 里的非类型条目
_GATE_SOURCE_LABELS = {
    "identity.package": "App 包名",
    "identity.activity": "App 页面标识（Activity）",
    "safe_area": "屏幕尺寸（上传截图后自动得到）",
    "visible_text(non_empty)": "页面文字（不能为空）",
}

# 只有大模型 / 混合规则允许按已给证据放宽齐全性：确定性检查缺输入时会直接判「未通过」，
# 放宽只会制造误导性结论，所以规则引擎类规则始终按正式要求检查。
RELAXABLE_EXECUTOR_TYPES = frozenset({"llm", "hybrid"})


class SandboxPlatform(StrEnum):
    """测试证据来自哪一端；决定展示层评估与 App 身份门禁口径。"""

    H5 = "h5"
    WEB = "web"
    ANDROID = "android"
    IOS = "ios"

    @property
    def surface_kind(self) -> str:
        """与 qc_capture.surface_kind 同口径：H5 与 Web 都是浏览器界面。"""
        return "web" if self in (SandboxPlatform.H5, SandboxPlatform.WEB) else self.value

    @property
    def target_client(self) -> str:
        """与规则 input_contract.target_clients 同口径。"""
        return {"h5": "H5", "web": "WEB"}.get(self.value, "APP")

    @property
    def is_app(self) -> bool:
        return self in (SandboxPlatform.ANDROID, SandboxPlatform.IOS)


class SandboxCheckMode(StrEnum):
    """证据齐全性口径。"""

    # 按测试人员实际给的证据直接判（只对大模型 / 混合规则放宽）
    RELAXED = "relaxed"
    # 严格按正式任务的证据要求，缺什么就判证据不足
    STRICT = "strict"


@dataclass(frozen=True, kw_only=True)
class SandboxScreenshot:
    """已规范化为 PNG 的一张测试截图。"""

    filename: str
    payload: bytes
    width: int
    height: int


@dataclass(frozen=True, kw_only=True)
class RuleSandboxEvidence:
    """测试人员提供的一页证据。"""

    platform: SandboxPlatform
    page_url: str = ""
    page_title: str = ""
    page_type: str = ""
    page_state: str = ""
    visible_text: str = ""
    dom_html: str = ""
    ui_snapshot: dict[str, Any] | None = None
    app_package_name: str = ""
    judge_input: dict[str, Any] = field(default_factory=dict)
    screenshots: tuple[SandboxScreenshot, ...] = ()
    # 规则比「操作前 / 操作后」两张截图时，操作前那张（页面截图算操作后）
    before_screenshot: SandboxScreenshot | None = None
    # 规则比同一商品两个位置的价格时，第二个位置的页面文字（页面文字算第一个位置）
    second_location_text: str = ""

    def provided_extra_roles(self) -> set[str]:
        """测试人员补了哪几组另一组采集的证据。"""
        roles: set[str] = set()
        if self.before_screenshot is not None:
            roles.add(BEFORE_ROLE)
        if self.second_location_text.strip():
            roles.add(SECOND_LOCATION_ROLE)
        return roles

    def provided_artifact_types(self) -> set[str]:
        types: set[str] = set()
        if self.screenshots:
            types.add("screenshot")
        if self.visible_text.strip():
            types.add("visible_text")
        if self.dom_html.strip():
            types.add("dom")
        if self.ui_snapshot:
            types.add("ui_snapshot")
        return types

    def has_any_evidence(self) -> bool:
        return bool(self.provided_artifact_types())


def build_sandbox_artifacts(evidence: RuleSandboxEvidence) -> tuple[ObservationArtifact, ...]:
    """按正式制品类型与内容类型组装证据；ID 用可读名，方便结果里指回是哪份证据。"""
    artifacts = [
        ObservationArtifact(
            artifact_id=f"{SCREENSHOT_ARTIFACT_PREFIX}{index}",
            artifact_type="screenshot",
            content_type="image/png",
            payload=screenshot.payload,
        )
        for index, screenshot in enumerate(evidence.screenshots, start=1)
    ]
    if evidence.visible_text.strip():
        artifacts.append(
            ObservationArtifact(
                artifact_id=VISIBLE_TEXT_ARTIFACT_ID,
                artifact_type="visible_text",
                content_type="text/plain; charset=utf-8",
                payload=evidence.visible_text.encode("utf-8"),
            )
        )
    if evidence.dom_html.strip():
        artifacts.append(
            ObservationArtifact(
                artifact_id=DOM_ARTIFACT_ID,
                artifact_type="dom",
                content_type="text/html; charset=utf-8",
                payload=evidence.dom_html.encode("utf-8"),
            )
        )
    if evidence.ui_snapshot:
        artifacts.append(
            ObservationArtifact(
                artifact_id=UI_SNAPSHOT_ARTIFACT_ID,
                artifact_type="ui_snapshot",
                content_type="application/json",
                payload=json.dumps(evidence.ui_snapshot, ensure_ascii=False).encode("utf-8"),
            )
        )
    return tuple(artifacts)


def sandbox_evidence_labels(evidence: RuleSandboxEvidence) -> dict[str, str]:
    """证据 ID → 页面上显示的名字（判定结果里的证据引用靠它翻译）。"""
    labels = {
        f"{SCREENSHOT_ARTIFACT_PREFIX}{index}": f"截图 {index}（{screenshot.filename}）"
        for index, screenshot in enumerate(evidence.screenshots, start=1)
    }
    if evidence.visible_text.strip():
        labels[VISIBLE_TEXT_ARTIFACT_ID] = "页面文字"
    if evidence.dom_html.strip():
        labels[DOM_ARTIFACT_ID] = "页面源码"
    if evidence.ui_snapshot:
        labels[UI_SNAPSHOT_ARTIFACT_ID] = "界面快照"
    if evidence.before_screenshot is not None:
        labels[BEFORE_SCREENSHOT_ARTIFACT_ID] = (
            f"{EXTRA_INPUTS[BEFORE_ROLE].label}（{evidence.before_screenshot.filename}）"
        )
    if evidence.second_location_text.strip():
        labels[SECOND_LOCATION_TEXT_ARTIFACT_ID] = EXTRA_INPUTS[SECOND_LOCATION_ROLE].label
    return labels


def build_sandbox_observation_source(
    *,
    evidence: RuleSandboxEvidence,
    task_id: str,
    page_capture_id: str,
) -> ObservationSource:
    """组装与正式采集读回同形的证据来源；App 身份只取测试人员填的包名与真实截图尺寸。"""
    app_capture_facts: dict[str, Any] | None = None
    if evidence.platform.is_app:
        app_capture_facts = {}
        if evidence.app_package_name.strip():
            app_capture_facts["package_name"] = evidence.app_package_name.strip()
        if evidence.screenshots:
            # 与 qc_app_capture.width/height 同义：取第一张截图的真实 PNG 尺寸
            app_capture_facts["width"] = evidence.screenshots[0].width
            app_capture_facts["height"] = evidence.screenshots[0].height
    return ObservationSource(
        task_id=task_id,
        page_capture_id=page_capture_id,
        page_url=evidence.page_url.strip(),
        page_title=evidence.page_title.strip() or None,
        page_type=evidence.page_type.strip() or None,
        page_state=evidence.page_state.strip() or None,
        action_log=(),
        artifacts=build_sandbox_artifacts(evidence),
        surface_kind=evidence.platform.surface_kind,
        app_capture_facts=app_capture_facts or None,
    )


def build_sandbox_role_sources(
    *,
    evidence: RuleSandboxEvidence,
    task_id: str,
    roles: Collection[str],
) -> dict[str, ObservationSource]:
    """规则要的另一组采集（操作前页面、第二个价格位置）各自组成一份独立采集，与正式任务的多组采集同形。"""
    artifacts: dict[str, ObservationArtifact] = {}
    if BEFORE_ROLE in roles and evidence.before_screenshot is not None:
        artifacts[BEFORE_ROLE] = ObservationArtifact(
            artifact_id=BEFORE_SCREENSHOT_ARTIFACT_ID,
            artifact_type="screenshot",
            content_type="image/png",
            payload=evidence.before_screenshot.payload,
        )
    if SECOND_LOCATION_ROLE in roles and evidence.second_location_text.strip():
        artifacts[SECOND_LOCATION_ROLE] = ObservationArtifact(
            artifact_id=SECOND_LOCATION_TEXT_ARTIFACT_ID,
            artifact_type="visible_text",
            content_type="text/plain; charset=utf-8",
            payload=evidence.second_location_text.encode("utf-8"),
        )
    return {
        role: ObservationSource(
            task_id=task_id,
            page_capture_id=str(uuid4()),
            page_url=evidence.page_url.strip(),
            page_title=None,
            page_type=evidence.page_type.strip() or None,
            page_state=None,
            action_log=(),
            artifacts=(artifact,),
            surface_kind=evidence.platform.surface_kind,
        )
        for role, artifact in artifacts.items()
    }


def relax_input_contract(input_contract: dict[str, Any]) -> dict[str, Any]:
    """仅供人工材料组装和可读性检查；模型可见合同必须由沙箱适配层恢复原要求。"""
    relaxed = dict(input_contract)
    relaxed["required_artifacts"] = []
    relaxed.pop("required_capture_roles", None)
    quality_gate = input_contract.get("quality_gate")
    quality_gate = dict(quality_gate) if isinstance(quality_gate, dict) else {}
    quality_gate["min_screenshot_count"] = 0
    quality_gate["require_app_identity"] = False
    relaxed["quality_gate"] = quality_gate
    return relaxed


@dataclass(frozen=True, kw_only=True)
class RuleEvidenceProfile:
    """一条规则在正式任务里对单页证据的要求。"""

    required_artifact_types: tuple[str, ...]
    min_screenshot_count: int
    extra_capture_roles: tuple[str, ...]
    required_page_states: tuple[str, ...]
    target_clients: tuple[str, ...]
    auth_requirement: str | None
    require_app_identity: bool = True

    @property
    def extra_inputs(self) -> tuple[SandboxExtraInput, ...]:
        """测试页能补的另一组证据：补上之后这条规则要的证据测试页都给得了，才放出输入框。"""
        roles = self.extra_capture_roles
        if not roles or any(role not in EXTRA_INPUTS for role in roles):
            return ()
        if any(
            artifact_type not in FORM_SUPPORTED_ARTIFACT_TYPES
            for artifact_type in self.required_artifact_types
        ):
            return ()
        return tuple(EXTRA_INPUTS[role] for role in roles)

    def missing_for(self, evidence: RuleSandboxEvidence) -> list[str]:
        """这份测试证据比正式要求少了哪些（大白话）。"""
        provided = evidence.provided_artifact_types()
        missing = [
            evidence_type_label(artifact_type)
            for artifact_type in self.required_artifact_types
            if artifact_type not in provided
            and not (evidence.platform.is_app and artifact_type == "dom")
        ]
        if self.min_screenshot_count > len(evidence.screenshots):
            missing.append(f"至少 {self.min_screenshot_count} 张截图")
        if evidence.platform.is_app and self.require_app_identity:
            # 与质量门禁的 App 身份要求同口径：包名 + 屏幕尺寸
            if not evidence.app_package_name.strip():
                missing.append(_GATE_SOURCE_LABELS["identity.package"])
            if not evidence.screenshots:
                missing.append(_GATE_SOURCE_LABELS["safe_area"])
        return list(dict.fromkeys(missing))


def build_rule_evidence_profile(
    *,
    executor_type: str,
    input_contract: dict[str, Any],
    judge_config: dict[str, Any],
) -> RuleEvidenceProfile:
    """汇总质量门禁（input_contract）与确定性 Handler（执行路由）两处的证据要求。"""
    required: list[str] = [
        _ARTIFACT_ALIASES.get(item, item)
        for item in _string_list(input_contract.get("required_artifacts"))
    ]
    if executor_type == RULE_ENGINE_EXECUTOR:
        route = resolve_rule_execution_route(
            executor_type=executor_type,
            judge_config=judge_config,
        )
        required.extend(route.required_artifact_types)
    quality_gate = input_contract.get("quality_gate")
    quality_gate = quality_gate if isinstance(quality_gate, dict) else {}
    engine_config = judge_config.get("engine_config")
    engine_config = engine_config if isinstance(engine_config, dict) else {}
    auth_requirement = str(input_contract.get("auth_requirement") or "").strip()
    target_clients = [item.upper() for item in _string_list(input_contract.get("target_clients"))]
    return RuleEvidenceProfile(
        required_artifact_types=tuple(dict.fromkeys(required)),
        min_screenshot_count=_non_negative_int(quality_gate.get("min_screenshot_count")),
        extra_capture_roles=tuple(
            role
            for role in dict.fromkeys(_string_list(input_contract.get("required_capture_roles")))
            if role != "primary"
        ),
        required_page_states=tuple(
            dict.fromkeys(item.casefold() for item in _string_list(
                engine_config.get("required_page_states")
            ))
        ),
        target_clients=tuple(dict.fromkeys(target_clients)),
        auth_requirement=auth_requirement or None,
        require_app_identity=bool(quality_gate.get("require_app_identity", True)),
    )


def evidence_type_label(artifact_type: str) -> str:
    return EVIDENCE_TYPE_LABELS.get(artifact_type, artifact_type)


def capture_roles_sentence(roles: tuple[str, ...]) -> str:
    """多组证据要求的大白话说明（角色名走平台统一翻译）。"""
    return humanize_qc_text("需要多组页面证据，采集角色：" + "、".join(roles)) or ""


def humanize_gate_sources(sources: list[str]) -> list[str]:
    """把质量门禁缺口（visible_text(non_empty)、screenshot(min=2) 等）翻成大白话。"""
    labels: list[str] = []
    for source in sources:
        text = str(source).strip()
        if not text:
            continue
        if text in _GATE_SOURCE_LABELS:
            labels.append(_GATE_SOURCE_LABELS[text])
        elif text.startswith("screenshot(min="):
            labels.append(f"至少 {text.removeprefix('screenshot(min=').rstrip(')')} 张截图")
        elif text.startswith("page_state("):
            labels.append(f"页面状态为「{text.removeprefix('page_state(').rstrip(')')}」时不能判")
        elif text.startswith("fault_artifact("):
            labels.append("页面出现闪退或卡死")
        elif text == "packet_input_budget":
            labels.append("证据内容太多，压缩后仍超出判定上限")
        else:
            labels.append(evidence_type_label(_ARTIFACT_ALIASES.get(text, text)))
    return list(dict.fromkeys(labels))


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _non_negative_int(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, number)
