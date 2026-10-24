"""规则测试沙箱：人工证据走正式任务同一套规则判定，结果只回给页面。

对应管理后台「规则配置 → 规则测试」：配置人员上传截图、粘贴页面文字等证据，
下拉选一条规则，立即看到这条规则能不能判、判出什么、为什么。

隔离红线：
- 不写任何正式表：不写 qc_capture / qc_evidence_artifact / qc_observation_packet /
  qc_rule_execution，不产生补采请求；报告、审核、收口门禁都看不到测试结果；
- 规则定义直接读 qc_rule 当前内容（草稿、停用规则也能测），不经任务锁定规则集；
- 判定计算完全复用 RuleJudgeService：只把「证据来源」「观察包落库」换成内存实现，
  确定性取证端口与语义 Judge 由调用方按生产同源方式注入。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol
from uuid import uuid4

from services.api.app.application.observation_packet import (
    BuildObservationPacketRequest,
    ObservationPacket,
    ObservationPacketService,
    ObservationPacketValidationError,
    ObservationSource,
    project_rule_input_contract,
)
from services.api.app.application.rule_judge import (
    RuleJudgeDefinition,
    RuleJudgeEvidenceInsufficientError,
    RuleJudgeEvidenceReader,
    RuleJudgeNotConfiguredError,
    RuleJudgeRequest,
    RuleJudgeResult,
    RuleJudgeService,
    RuleJudgeValidationError,
    VerdictMode,
    resolve_rule_execution_route,
)
from services.api.app.application.rule_sandbox_evidence import (
    BEFORE_ROLE,
    EXTRA_INPUTS,
    FORM_SUPPORTED_ARTIFACT_TYPES,
    RELAXABLE_EXECUTOR_TYPES,
    RELAXABLE_VISUAL_ARTIFACT_TYPES,
    RuleEvidenceProfile,
    RuleSandboxEvidence,
    SandboxCheckMode,
    SandboxExtraInput,
    SandboxScreenshot,
    build_rule_evidence_profile,
    build_sandbox_observation_source,
    build_sandbox_role_sources,
    capture_roles_sentence,
    evidence_type_label,
    humanize_gate_sources,
    relax_input_contract,
    sandbox_evidence_labels,
)
from services.api.app.application.semantic_rule_judge import SemanticRuleJudge
from services.api.app.application.task_progress import humanize_qc_text
from services.api.app.domain.rule.url_scope import candidate_rule_matches_page_url

logger = logging.getLogger(__name__)

# 规则配置不可执行、或本页测试不支持时的沙箱专属结论（正式判定词汇之外）
OUTCOME_NOT_EXECUTABLE = "not_executable"
# observation_packet 质量门禁不通过时的原话前缀
_QUALITY_GATE_REASON_PREFIX = "缺少规则输入契约要求的可信证据："

_RULE_STATUS_LABELS = {
    "draft": "草稿",
    "enabled": "启用",
    "disabled": "停用",
    "archived": "归档",
}
_PLATFORM_LABELS = {"H5": "H5", "WEB": "Web", "APP": "App"}
_AGGREGATE_SCOPE_REASONS = {
    "task": "这条规则是整任务汇总判定（要把多个页面放在一起看），暂不支持单页测试",
    "journey": "这条规则按用户操作流程汇总判定，暂不支持单页测试",
    "environment_matrix": "这条规则要对比多个环境的结果，暂不支持单页测试",
    "web_sequence": "这条规则要用真实采集的 Web 连续画面判定，暂不支持手动测试",
}


class RuleSandboxRuleNotFoundError(LookupError):
    """要测试的规则不存在。"""


class RuleSandboxEvidenceError(ValueError):
    """测试证据不满足最低要求。"""


class RuleSandboxRuleSource(Protocol):
    """规则当前定义读取端口（PostgresRuleRepository 结构性满足）。"""

    async def get_rule(self, rule_id: str) -> dict[str, Any] | None:
        """按规则 ID 读 qc_rule 当前内容，不区分状态。"""

    async def list_rules_for_sandbox(self) -> list[dict[str, Any]]:
        """列全部规则及所属规则集，供测试页下拉选择。"""


# 入参：本页证据、规则要的另一组采集（按采集角色）
EvidenceReaderFactory = Callable[
    [ObservationSource, Mapping[str, ObservationSource]], RuleJudgeEvidenceReader
]
SemanticJudgeFactory = Callable[[], SemanticRuleJudge]


@dataclass(frozen=True, kw_only=True)
class SandboxRule:
    """测试用的规则当前定义。"""

    rule_id: str
    rule_code: str
    name: str
    category: str
    severity: str
    executor_type: str
    status: str
    check_point: str
    applicable_page_types: tuple[str, ...]
    url_pattern: str | None
    steps: list[Any]
    input_contract: dict[str, Any]
    judge_config: dict[str, Any]
    manual_review_policy: dict[str, Any]

    @property
    def evidence_scope(self) -> str:
        capability = self.judge_config.get("capability_config")
        capability = capability if isinstance(capability, dict) else {}
        return str(capability.get("evidence_input_scope") or "page").strip() or "page"

    def definition(self, *, input_contract: dict[str, Any]) -> RuleJudgeDefinition:
        return RuleJudgeDefinition(
            rule_id=self.rule_id,
            rule_code=self.rule_code,
            executor_type=self.executor_type,
            judge_config=self.judge_config,
            rule_name=self.name or self.rule_code,
            category=self.category,
            severity=self.severity,
            check_point=self.check_point,
            input_contract=input_contract,
            manual_review_policy=self.manual_review_policy,
            steps=self.steps,
        )


@dataclass(frozen=True, kw_only=True)
class RuleSandboxEvidenceNeed:
    """规则在正式任务里要的一类证据。"""

    artifact_type: str
    label: str
    form_supported: bool


@dataclass(frozen=True, kw_only=True)
class RuleSandboxOption:
    """测试页下拉里的一条规则及其证据要求。"""

    rule_id: str
    rule_code: str
    name: str
    category: str
    severity: str
    executor_type: str
    status: str
    check_point: str
    applicable_page_types: tuple[str, ...]
    ruleset_id: str
    ruleset_code: str
    ruleset_name: str
    ruleset_version: int
    ruleset_status: str
    testable: bool
    untestable_reason: str | None
    config_problem: str | None
    relaxable: bool
    evidence_needs: tuple[RuleSandboxEvidenceNeed, ...]
    min_screenshot_count: int
    required_page_states: tuple[str, ...]
    target_clients: tuple[str, ...]
    notes: tuple[str, ...]
    # 正式任务里由采集 Agent 结构化填好的判定输入字段（如优惠价、原价）；测试页要人工补
    judge_input_fields: tuple[str, ...] = ()
    # 正式任务里另一组采集才有的证据（操作前的截图、第二个位置的页面文字），测试页单独放输入框
    extra_inputs: tuple[SandboxExtraInput, ...] = ()


@dataclass(frozen=True, kw_only=True)
class RuleSandboxFinding:
    """判定给出的一条具体发现。"""

    summary: str
    evidence: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class RuleSandboxRunResult:
    """一次规则测试的结论（只回给页面，不落库）。"""

    rule_id: str
    rule_code: str
    rule_name: str
    executor_type: str
    rule_status: str
    check_mode: str
    outcome: str
    result_summary: str
    problem_desc: str | None
    confidence: float | None
    confidence_threshold: float | None
    findings: tuple[RuleSandboxFinding, ...]
    missing_evidence: tuple[str, ...]
    used_evidence: tuple[str, ...]
    notices: tuple[str, ...]
    duration_ms: int
    judge_input: dict[str, Any]
    judge_output: dict[str, Any]
    # 只给截图时模型读出来的页面文字；判定用的就是它，记录也存它
    screenshot_text: str | None = None


ScreenshotTextReader = Callable[[Sequence[SandboxScreenshot]], Awaitable[str]]

SCREENSHOT_TEXT_NOTICE = (
    "页面文字是模型从你传的截图里读出来的，可能有错字漏字；要更准就填网址自动抓，或把页面文字粘进来"
)
SCREENSHOT_TEXT_EMPTY_NOTICE = "没能从截图里读出文字，这条规则仍按缺页面文字处理"

def _rule_accepts_visible_text(rule: SandboxRule, profile: RuleEvidenceProfile) -> bool:
    """规则合同把页面文字列为必需或可选时，读图文字才对程序判有用。"""
    if "visible_text" in profile.required_artifact_types:
        return True
    optional = rule.input_contract.get("optional_artifacts")
    return isinstance(optional, list) and "visible_text" in {str(item) for item in optional}


class RuleSandboxService:
    """规则测试的受理与同步执行。"""

    def __init__(
        self,
        *,
        rules: RuleSandboxRuleSource,
        evidence_reader_factory: EvidenceReaderFactory,
        semantic_judge_factory: SemanticJudgeFactory,
        verdict_mode: VerdictMode = "strict",
        clock: Callable[[], float] = time.monotonic,
        screenshot_text_reader: ScreenshotTextReader | None = None,
        qr_validator: Callable[..., Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        self._rules = rules
        # 二维码规则校验网址的口子留给测试注入；判定程序自己会从截图解码
        self._qr_validator = qr_validator
        self._evidence_reader_factory = evidence_reader_factory
        self._semantic_judge_factory = semantic_judge_factory
        # 只给了截图时，把截图里的字抄出来当页面文字（规则引擎类规则读不了图）
        self._screenshot_text_reader = screenshot_text_reader
        self._verdict_mode = verdict_mode
        self._clock = clock

    async def list_options(self) -> list[RuleSandboxOption]:
        rows = await self._rules.list_rules_for_sandbox()
        return [_option_from_row(row) for row in rows]

    async def run(
        self,
        *,
        rule_id: str,
        evidence: RuleSandboxEvidence,
        check_mode: SandboxCheckMode = SandboxCheckMode.RELAXED,
    ) -> RuleSandboxRunResult:
        row = await self._rules.get_rule(rule_id)
        if row is None:
            raise RuleSandboxRuleNotFoundError(f"规则 {rule_id} 不存在")
        if not evidence.has_any_evidence():
            raise RuleSandboxEvidenceError(
                "请至少提供一份证据：截图、页面文字、页面源码或界面快照"
            )
        rule = sandbox_rule_from_row(row)
        started = self._clock()
        profile = build_rule_evidence_profile(
            executor_type=rule.executor_type,
            input_contract=rule.input_contract,
            judge_config=rule.judge_config,
        )
        evidence, text_notice = await self._read_text_from_screenshots(rule, profile, evidence)
        notices = _applicability_notices(rule, profile, evidence)
        if text_notice:
            notices.append(text_notice)
        result = await self._judge_evidence(
            rule=rule,
            profile=profile,
            evidence=evidence,
            notices=notices,
            check_mode=check_mode,
            started=started,
        )
        if text_notice is SCREENSHOT_TEXT_NOTICE:
            result = replace(result, screenshot_text=evidence.visible_text)
        return result

    async def _judge_evidence(
        self,
        *,
        rule: SandboxRule,
        profile: RuleEvidenceProfile,
        evidence: RuleSandboxEvidence,
        notices: list[str],
        check_mode: SandboxCheckMode,
        started: float,
    ) -> RuleSandboxRunResult:
        base = _ResultBase(rule=rule, check_mode=check_mode, started=started, clock=self._clock)

        scope_reason = _unsupported_scope_reason(rule.evidence_scope)
        if scope_reason is not None:
            return base.not_executable(scope_reason, notices=notices)

        missing_vs_formal = profile.missing_for(evidence)
        relaxed = (
            check_mode is SandboxCheckMode.RELAXED
            and rule.executor_type in RELAXABLE_EXECUTOR_TYPES
        )
        unavailable = [
            evidence_type_label(kind)
            for kind in profile.required_artifact_types
            if kind not in RELAXABLE_VISUAL_ARTIFACT_TYPES
        ]
        if relaxed and unavailable:
            # 人工表单没有真实网络、动作或时序采集端口，多张上传图片不能代替这些义务。
            return replace(
                base.evidence_insufficient(
                    "人工证据缺少规则合同要求的真实采集事实：" + "、".join(unavailable),
                    notices=notices,
                ),
                missing_evidence=tuple(unavailable),
            )
        if relaxed:
            if missing_vs_formal:
                notices.append(
                    "人工材料检查允许继续送审，原规则输入要求仍完整交给判定。正式任务还要求："
                    + "、".join(missing_vs_formal)
                    + "，缺了会判「证据不足」"
                )
            if profile.extra_capture_roles:
                notices.append(
                    "正式任务里这条规则"
                    + capture_roles_sentence(profile.extra_capture_roles)
                    + "；这里只提供人工材料，不能据此证明真实动作或正式流程已完成"
                )
        elif check_mode is SandboxCheckMode.RELAXED and missing_vs_formal:
            notices.append("规则引擎类规则始终按正式任务的证据要求检查，缺证据不会放宽")

        input_contract = (
            relax_input_contract(rule.input_contract) if relaxed else rule.input_contract
        )
        if (
            rule.executor_type != "rule_engine"
            and evidence.before_screenshot is not None
            and BEFORE_ROLE in _string_list(rule.input_contract.get("required_capture_roles"))
        ):
            # 模型看图的规则不比「操作前 / 操作后」两组采集：操作前那张放到第 1 张，和页面截图一起按先后顺序交给模型
            evidence = replace(
                evidence,
                screenshots=(evidence.before_screenshot, *evidence.screenshots),
                before_screenshot=None,
            )
            notices.append("「操作前的截图」已放在第 1 张，和页面截图一起按先后顺序交给模型看")
        # 另一组采集只交给要它、测试页也放了输入框的规则；放宽口径已去掉多组要求
        extra_roles = {item.role for item in profile.extra_inputs} & set(
            _string_list(input_contract.get("required_capture_roles"))
        )
        for role in sorted(evidence.provided_extra_roles() - extra_roles):
            notices.append(f"这条规则用不到「{EXTRA_INPUTS[role].label}」，没有拿去判")
        task_id = f"rule-sandbox-{uuid4()}"
        page_capture_id = str(uuid4())
        role_sources = build_sandbox_role_sources(
            evidence=evidence,
            task_id=task_id,
            roles=extra_roles,
        )
        if BEFORE_ROLE in role_sources and len(evidence.screenshots) != 1:
            # 判定按采集角色各取一张截图比，操作后多放一张就分不清拿哪张
            return base.evidence_insufficient(
                "这条规则拿「操作前」「操作后」各一张截图比，页面截图里只能放 1 张（操作后那张）",
                notices=notices,
            )
        source = build_sandbox_observation_source(
            evidence=evidence,
            task_id=task_id,
            page_capture_id=page_capture_id,
        )
        judge = RuleJudgeService(
            repository=_FixedDefinitionRepository(
                rule.definition(input_contract=input_contract)
            ),
            evidence_reader=self._evidence_reader_factory(source, role_sources),
            observation_packet_service=(
                _SandboxObservationPacketService(
                    repository=_SandboxObservationRepository(source),
                    original_input_contract=rule.input_contract,
                )
                if relaxed else ObservationPacketService(repository=_SandboxObservationRepository(source))
            ),
            semantic_judge=self._semantic_judge_factory(),
            qr_validator=self._qr_validator,
            verdict_mode=self._verdict_mode,
        )
        request = RuleJudgeRequest(
            task_id=task_id,
            rule_code=rule.rule_code,
            page_capture_id=page_capture_id,
            evidence_artifact_ids=(),
            judge_input=dict(evidence.judge_input),
            evidence_capture_roles=(
                {
                    "primary": page_capture_id,
                    **{role: item.page_capture_id for role, item in role_sources.items()},
                }
                if role_sources
                else None
            ),
        )
        labels = sandbox_evidence_labels(evidence)
        try:
            result = await judge.judge(request)
        except RuleJudgeNotConfiguredError as exc:
            return base.not_executable(
                f"这条规则的判定配置跑不起来：{exc}", notices=notices
            )
        except RuleJudgeEvidenceInsufficientError as exc:
            return base.evidence_insufficient(str(exc), notices=notices)
        except (RuleJudgeValidationError, ObservationPacketValidationError) as exc:
            return base.evidence_insufficient(
                f"判定需要的输入不可用：{exc}", notices=notices
            )
        except Exception as exc:  # noqa: BLE001 - 测试工具要把异常原因直接给配置人员看
            logger.exception("规则测试执行异常：rule_code=%s", rule.rule_code)
            return base.crashed(exc, notices=notices)
        return base.from_judge_result(result, labels=labels, notices=notices)

    async def _read_text_from_screenshots(
        self,
        rule: SandboxRule,
        profile: RuleEvidenceProfile,
        evidence: RuleSandboxEvidence,
    ) -> tuple[RuleSandboxEvidence, str | None]:
        """规则引擎类规则要页面文字（必需或可选）、测试人员只给了截图：让模型把字抄出来。

        混合 / 语义规则本来就看图，不多花这一次调用；抄出来的字标明来源，可能有错字漏字。
        页面文字只是可选的规则（如播放前广告存在与否）同样读：程序从文字里找广告特征，没有文字就永远判不足。
        """
        if (
            self._screenshot_text_reader is None
            or rule.executor_type in RELAXABLE_EXECUTOR_TYPES
            or not _rule_accepts_visible_text(rule, profile)
            or evidence.visible_text.strip()
            or not evidence.screenshots
        ):
            return evidence, None
        text = await self._screenshot_text_reader(evidence.screenshots)
        if not text.strip():
            return evidence, SCREENSHOT_TEXT_EMPTY_NOTICE
        return replace(evidence, visible_text=text), SCREENSHOT_TEXT_NOTICE

def sandbox_rule_from_row(row: dict[str, Any]) -> SandboxRule:
    return SandboxRule(
        rule_id=str(row["id"]),
        rule_code=str(row["rule_code"]),
        name=str(row.get("name") or row["rule_code"]),
        category=str(row.get("category") or ""),
        severity=str(row.get("severity") or "low"),
        executor_type=str(row.get("executor_type") or ""),
        status=str(row.get("status") or ""),
        check_point=str(row.get("check_point") or ""),
        applicable_page_types=tuple(
            str(item) for item in (row.get("applicable_page_types") or []) if str(item).strip()
        ),
        url_pattern=(str(row["url_pattern"]) if row.get("url_pattern") else None),
        steps=list(row.get("steps") or []),
        input_contract=dict(row.get("input_contract") or {}),
        judge_config=dict(row.get("judge_config") or {}),
        manual_review_policy=dict(row.get("manual_review_policy") or {}),
    )


class _FixedDefinitionRepository:
    """把测试规则的当前定义交给判定服务（替代任务锁定规则查询）。"""

    def __init__(self, definition: RuleJudgeDefinition) -> None:
        self._definition = definition

    async def get_rule_judge_definition(
        self, *, task_id: str, rule_code: str
    ) -> RuleJudgeDefinition | None:
        return self._definition if rule_code == self._definition.rule_code else None


class _SandboxObservationRepository:
    """观察包的内存证据来源：只认本次测试组装的那一页，观察包不落库。"""

    def __init__(self, source: ObservationSource) -> None:
        self._source = source

    async def load_source(
        self, *, task_id: str, page_capture_id: str
    ) -> ObservationSource | None:
        if task_id != self._source.task_id or page_capture_id != self._source.page_capture_id:
            return None
        return self._source

    async def save_packet(self, **_kwargs: Any) -> str:
        return f"sandbox-packet-{uuid4().hex[:12]}"


class _SandboxObservationPacketService(ObservationPacketService):
    """材料组装可用人工便利口径，模型判定义务始终保留原合同且先于缓存键生成。"""

    def __init__(self, *, repository, original_input_contract):
        super().__init__(repository=repository)
        self._original_input_contract = original_input_contract

    async def build(self, request: BuildObservationPacketRequest) -> ObservationPacket:
        packet = await super().build(request)
        payload = packet.packet_payload
        return replace(packet, packet_payload={
            **payload,
            "rule": {
                **payload["rule"],
                "input_contract": project_rule_input_contract(self._original_input_contract),
            },
            "quality_gate": {
                **payload["quality_gate"],
                "sandbox_provided_materials_only": True,
                "notice": "只检查本组人工材料能否阅读，不证明正式规则输入或真实操作已满足",
            },
        })


class _ResultBase:
    """把判定结果 / 异常收敛成统一的测试结论。"""

    def __init__(
        self,
        *,
        rule: SandboxRule,
        check_mode: SandboxCheckMode,
        started: float,
        clock: Callable[[], float],
    ) -> None:
        self._rule = rule
        self._check_mode = check_mode
        self._started = started
        self._clock = clock

    def not_executable(self, reason: str, *, notices: list[str]) -> RuleSandboxRunResult:
        return self._build(
            outcome=OUTCOME_NOT_EXECUTABLE,
            result_summary="这条规则没法按单页证据测试",
            problem_desc=reason,
            notices=notices,
        )

    def evidence_insufficient(self, reason: str, *, notices: list[str]) -> RuleSandboxRunResult:
        return self._build(
            outcome="evidence_insufficient",
            result_summary="证据不足，没有给出结论",
            problem_desc=humanize_qc_text(reason) or reason,
            notices=notices,
        )

    def crashed(self, exc: Exception, *, notices: list[str]) -> RuleSandboxRunResult:
        detail = str(exc).strip()[:500] or exc.__class__.__name__
        return self._build(
            outcome="error",
            result_summary="测试执行出错",
            problem_desc=f"判定过程中出现异常：{detail}",
            notices=notices,
            judge_output={"exception": exc.__class__.__name__, "message": detail},
        )

    def from_judge_result(
        self,
        result: RuleJudgeResult,
        *,
        labels: dict[str, str],
        notices: list[str],
    ) -> RuleSandboxRunResult:
        output = result.judge_output if isinstance(result.judge_output, dict) else {}
        model_output = output.get("model_output")
        model_output = model_output if isinstance(model_output, dict) else {}
        findings = tuple(
            RuleSandboxFinding(
                summary=humanize_qc_text(str(item.get("summary") or "")) or "",
                evidence=_evidence_names(item.get("evidence_refs"), labels),
            )
            for item in (model_output.get("findings") or [])
            if isinstance(item, dict) and str(item.get("summary") or "").strip()
        )
        gate_labels = humanize_gate_sources(_string_list(output.get("missing_evidence")))
        missing = [
            *gate_labels,
            *(
                humanize_qc_text(item) or item
                for item in _string_list(model_output.get("missing_evidence"))
            ),
        ]
        problem_desc = result.problem_desc
        if gate_labels and str(problem_desc or "").startswith(_QUALITY_GATE_REASON_PREFIX):
            # 质量门禁原话带工程术语，换成缺哪几样证据的大白话
            problem_desc = "缺少这条规则要求的证据：" + "、".join(gate_labels)
        threshold = output.get("confidence_threshold")
        # 只有模型真给出了通过/不通过/风险结论，把握度才有意义（线路故障等兜底输出恒为 0）
        judged_by_model = model_output.get("conclusion") in {"pass", "fail", "warning"}
        return self._build(
            outcome=result.execution_status,
            result_summary=humanize_qc_text(result.result_summary) or result.result_summary,
            problem_desc=humanize_qc_text(problem_desc),
            notices=notices,
            confidence=float(result.confidence) if judged_by_model else None,
            confidence_threshold=float(threshold) if isinstance(threshold, int | float) else None,
            findings=findings,
            missing_evidence=tuple(dict.fromkeys(item for item in missing if item)),
            used_evidence=_evidence_names(list(result.evidence_artifact_ids), labels),
            judge_input=dict(result.judge_input or {}),
            judge_output=dict(output),
        )

    def _build(
        self,
        *,
        outcome: str,
        result_summary: str,
        problem_desc: str | None,
        notices: list[str],
        confidence: float | None = None,
        confidence_threshold: float | None = None,
        findings: tuple[RuleSandboxFinding, ...] = (),
        missing_evidence: tuple[str, ...] = (),
        used_evidence: tuple[str, ...] = (),
        judge_input: dict[str, Any] | None = None,
        judge_output: dict[str, Any] | None = None,
    ) -> RuleSandboxRunResult:
        return RuleSandboxRunResult(
            rule_id=self._rule.rule_id,
            rule_code=self._rule.rule_code,
            rule_name=self._rule.name,
            executor_type=self._rule.executor_type,
            rule_status=self._rule.status,
            check_mode=self._check_mode.value,
            outcome=outcome,
            result_summary=result_summary,
            problem_desc=problem_desc,
            confidence=confidence,
            confidence_threshold=confidence_threshold,
            findings=findings,
            missing_evidence=missing_evidence,
            used_evidence=used_evidence,
            notices=tuple(notices),
            duration_ms=max(0, round((self._clock() - self._started) * 1000)),
            judge_input=judge_input or {},
            judge_output=judge_output or {},
        )


def _applicability_notices(
    rule: SandboxRule,
    profile: RuleEvidenceProfile,
    evidence: RuleSandboxEvidence,
) -> list[str]:
    """对照正式任务挑规则的条件（状态 / 端 / 页面类型 / 地址），提示哪里对不上；不拦测试。"""
    notices: list[str] = []
    if rule.status != "enabled":
        notices.append(
            f"这条规则现在是「{_RULE_STATUS_LABELS.get(rule.status, rule.status)}」状态，"
            "正式任务不会跑它；这里照样可以测试"
        )
    if profile.target_clients and evidence.platform.target_client not in profile.target_clients:
        notices.append(
            "正式任务里这条规则只在 "
            + "、".join(_PLATFORM_LABELS.get(item, item) for item in profile.target_clients)
            + f" 端跑，你选的是 {_PLATFORM_LABELS[evidence.platform.target_client]}"
        )
    page_type = evidence.page_type.strip()
    if rule.applicable_page_types and page_type and page_type not in rule.applicable_page_types:
        notices.append(
            humanize_qc_text(
                "正式任务里这条规则只在"
                + "".join(f"「{item}」" for item in rule.applicable_page_types)
                + f"上跑，你选的是「{page_type}」"
            )
            or ""
        )
    if rule.url_pattern:
        matches, _ = candidate_rule_matches_page_url(
            page_url=evidence.page_url.strip() or None,
            url_pattern=rule.url_pattern,
        )
        if not matches:
            notices.append(
                f"正式任务里这条规则只在地址符合「{rule.url_pattern}」的页面上跑，"
                "你填的地址不符合（或没填）"
            )
    if profile.auth_requirement == "authenticated":
        notices.append("这条规则要求页面处于已登录状态，请确认你给的是登录后的页面")
    return notices


def _unsupported_scope_reason(scope: str) -> str | None:
    if scope == "page":
        return None
    return _AGGREGATE_SCOPE_REASONS.get(scope, "这条规则不是按单个页面判定的，暂不支持单页测试")


def _option_from_row(row: dict[str, Any]) -> RuleSandboxOption:
    rule = sandbox_rule_from_row(row)
    profile = build_rule_evidence_profile(
        executor_type=rule.executor_type,
        input_contract=rule.input_contract,
        judge_config=rule.judge_config,
    )
    route = resolve_rule_execution_route(
        executor_type=rule.executor_type,
        judge_config=rule.judge_config,
    )
    config_problem = None
    if not route.ready:
        reason = route.blocked_reason or {}
        config_problem = str(reason.get("message") or "判定配置不可执行")
    untestable_reason = _unsupported_scope_reason(rule.evidence_scope)
    notes: list[str] = []
    if profile.extra_capture_roles:
        notes.append("正式任务里这条规则" + capture_roles_sentence(profile.extra_capture_roles))
    if profile.auth_requirement == "authenticated":
        notes.append("要求页面处于已登录状态")
    return RuleSandboxOption(
        rule_id=rule.rule_id,
        rule_code=rule.rule_code,
        name=rule.name,
        category=rule.category,
        severity=rule.severity,
        executor_type=rule.executor_type,
        status=rule.status,
        check_point=rule.check_point,
        applicable_page_types=rule.applicable_page_types,
        ruleset_id=str(row.get("ruleset_id") or ""),
        ruleset_code=str(row.get("ruleset_code") or ""),
        ruleset_name=str(row.get("ruleset_name") or ""),
        ruleset_version=int(row.get("ruleset_version") or 0),
        ruleset_status=str(row.get("ruleset_status") or ""),
        testable=untestable_reason is None,
        untestable_reason=untestable_reason,
        config_problem=config_problem,
        relaxable=rule.executor_type in RELAXABLE_EXECUTOR_TYPES,
        evidence_needs=tuple(
            RuleSandboxEvidenceNeed(
                artifact_type=artifact_type,
                label=evidence_type_label(artifact_type),
                form_supported=artifact_type in FORM_SUPPORTED_ARTIFACT_TYPES,
            )
            for artifact_type in profile.required_artifact_types
        ),
        min_screenshot_count=profile.min_screenshot_count,
        required_page_states=profile.required_page_states,
        target_clients=profile.target_clients,
        notes=tuple(notes),
        judge_input_fields=(
            route.required_input_fields if "agent_structured" in route.judge_input_source else ()
        ),
        extra_inputs=profile.extra_inputs,
    )


def _evidence_names(refs: Any, labels: dict[str, str]) -> tuple[str, ...]:
    names: list[str] = []
    for ref in refs if isinstance(refs, list) else []:
        text = str(ref).strip()
        if not text:
            continue
        # 截图序列帧引用形如 screenshot-1#frame-0，按所属截图显示
        names.append(labels.get(text) or labels.get(text.split("#", 1)[0]) or text)
    return tuple(dict.fromkeys(names))


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
