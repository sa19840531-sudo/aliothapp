"""规则测试接口：人工上传证据、选一条规则，同步返回判定结果（不落任何正式结果）。

每次测试的证据和结论另存一条测试记录（qc_rule_test_record），供体验中心管理员回看、重测、批量删除。
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field

from services.api.app.agents.runtime.usage_meter import bind_llm_usage_scope
from services.api.app.api.v1.auth import AuthUserResponse, require_auth_user
from services.api.app.application.rule_sandbox import (
    RuleSandboxEvidenceError,
    RuleSandboxOption,
    RuleSandboxRuleNotFoundError,
    RuleSandboxRunResult,
    RuleSandboxService,
)
from services.api.app.application.rule_sandbox_evidence import (
    MAX_SANDBOX_SCREENSHOTS,
    RuleSandboxEvidence,
    SandboxCheckMode,
    SandboxPlatform,
    SandboxScreenshot,
)
from services.api.app.application.rule_test_records import (
    RuleTestRecordDeleteError,
    RuleTestRecordNotFoundError,
    RuleTestRecordService,
)
from services.api.app.core.config import get_settings
from services.api.app.core.errors import AliothError
from services.api.app.domain.page.registry import get_default_page_type_registry
from services.api.app.infrastructure.db.rule_repository import PostgresRuleRepository
from services.api.app.infrastructure.db.rule_test_record_repository import (
    PostgresRuleTestRecordRepository,
)
from services.api.app.infrastructure.llm_gateway.screenshot_text_reader import (
    read_screenshot_text,
)
from services.api.app.infrastructure.llm_gateway.semantic_rule_judge import (
    LLMSemanticRuleJudge,
)
from services.api.app.infrastructure.rule_sandbox_evidence_reader import (
    SandboxRuleJudgeEvidenceReader,
)
from services.api.app.infrastructure.rule_sandbox_page_capture import (
    RuleTestPageCaptureError,
    capture_rule_test_page,
)
from services.api.app.infrastructure.rule_sandbox_screenshots import (
    ScreenshotDecodeError,
    normalize_screenshot_png,
)
from services.api.app.infrastructure.storage.factory import build_evidence_artifact_store

logger = logging.getLogger(__name__)

MAX_SCREENSHOT_BYTES = 8 * 1024 * 1024
MAX_VISIBLE_TEXT_CHARACTERS = 200_000
# 正式采集的源码上限是 2 MiB（再加截断标记），按网址抓来的源码要能原样提交
MAX_DOM_CHARACTERS = 2_200_000
# 正式界面快照上限 512 KiB，页面上按缩进展示会变长
MAX_JSON_CHARACTERS = 2_000_000
# 框架默认每个表单字段只收 1MB，抓来的源码、界面快照经常超过；按 UTF-8 每字符最多 4 字节放宽，
# 真正的上限仍是接口里按字符数做的检查
MAX_FORM_FIELD_BYTES = 4 * MAX_DOM_CHARACTERS


class _LargeFormFieldRequest(Request):
    """规则测试的表单字段不受框架默认 1MB 限制，按上面的字符数上限收。"""

    def form(
        self,
        *,
        max_files: int | float = 1000,
        max_fields: int | float = 1000,
        max_part_size: int = MAX_FORM_FIELD_BYTES,
    ) -> Any:
        return super().form(
            max_files=max_files, max_fields=max_fields, max_part_size=max_part_size
        )


class _LargeFormFieldRoute(APIRoute):
    def get_route_handler(self) -> Any:
        handler = super().get_route_handler()

        async def route_handler(request: Request) -> Response:
            return await handler(_LargeFormFieldRequest(request.scope, request.receive))

        return route_handler


router = APIRouter(route_class=_LargeFormFieldRoute)


class RuleTestEvidenceNeedResponse(BaseModel):
    artifact_type: str
    label: str
    form_supported: bool


class RuleTestExtraInputResponse(BaseModel):
    # 采集角色：before 操作前页面 / location_1 第二个价格位置
    role: str
    # screenshot 一张截图 / text 一段页面文字
    kind: str
    label: str
    hint: str


class RuleTestOptionResponse(BaseModel):
    rule_id: str
    rule_code: str
    name: str
    category: str
    severity: str
    executor_type: str
    status: str
    check_point: str
    applicable_page_types: list[str]
    ruleset_id: str
    ruleset_code: str
    ruleset_name: str
    ruleset_version: int
    ruleset_status: str
    testable: bool
    untestable_reason: str | None
    config_problem: str | None
    relaxable: bool
    evidence_needs: list[RuleTestEvidenceNeedResponse]
    min_screenshot_count: int
    required_page_states: list[str]
    target_clients: list[str]
    notes: list[str]
    judge_input_fields: list[str] = []
    # 正式任务里另一组采集才有的证据，测试页单独放输入框
    extra_inputs: list[RuleTestExtraInputResponse] = []


class RuleTestPageTypeResponse(BaseModel):
    code: str
    name: str


class RuleTestOptionsResponse(BaseModel):
    items: list[RuleTestOptionResponse]
    # 页面类型下拉以正式页型目录为准（规则适用页型也按它校验）
    page_types: list[RuleTestPageTypeResponse]


class RuleTestFindingResponse(BaseModel):
    summary: str
    evidence: list[str]


class RuleTestPageCaptureRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    platform: SandboxPlatform = SandboxPlatform.H5
    # 要测的页面类型；是开屏广告（startup_ad）时抓到开屏层就是目标，不去跳过它
    page_type: str | None = Field(default=None, max_length=64)


class RuleTestPageCaptureResponse(BaseModel):
    final_url: str
    title: str
    visible_text: str
    dom_html: str
    ui_snapshot: dict[str, Any] | None
    screenshot_png_base64: str | None
    notices: list[str]


class RuleTestRunResponse(BaseModel):
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
    findings: list[RuleTestFindingResponse]
    missing_evidence: list[str]
    used_evidence: list[str]
    notices: list[str]
    duration_ms: int
    judge_input: dict[str, Any]
    judge_output: dict[str, Any]
    # 存进测试记录后的记录 ID；没存上时为空（结果里会多一条提示）
    record_id: str | None = None
    # 只传了截图时模型从截图里读出的页面文字（判定用的就是它）
    screenshot_text: str | None = None


class RuleTestRecordSummaryResponse(BaseModel):
    id: str
    rule_id: str | None
    rule_code: str
    rule_name: str
    tester_name: str
    platform: str
    page_url: str
    page_title: str
    outcome: str
    result_summary: str
    screenshot_count: int
    has_visible_text: bool
    has_dom: bool
    has_ui_snapshot: bool
    has_judge_input: bool
    created_at: datetime


class RuleTestRecordPageResponse(BaseModel):
    items: list[RuleTestRecordSummaryResponse]
    total: int
    page: int
    page_size: int


class RuleTestRecordScreenshotResponse(BaseModel):
    # 从 1 开始，取图时用它
    index: int
    filename: str
    width: int
    height: int
    # primary 页面截图 / before 操作前的截图
    capture_role: str = "primary"


class RuleTestRecordDetailResponse(RuleTestRecordSummaryResponse):
    check_mode: str
    page_type: str
    page_state: str
    visible_text: str
    dom_html: str
    ui_snapshot: dict[str, Any] | None
    judge_input: dict[str, Any]
    second_location_text: str = ""
    screenshots: list[RuleTestRecordScreenshotResponse]
    result: RuleTestRunResponse


class RuleTestRecordDeleteRequest(BaseModel):
    # 二选一：勾选的记录，或者时间段（开始含、结束不含，可只给一头）
    record_ids: list[str] = Field(default_factory=list)
    created_from: datetime | None = None
    created_to: datetime | None = None


class RuleTestRecordDeleteResponse(BaseModel):
    deleted: int


@lru_cache
def _get_rule_sandbox_service() -> RuleSandboxService:
    # 判定装配与 agents/rule_judge/bootstrap.py、独立规则质检同源：同一语义 Judge 参数与判定口径；
    # 证据来源与观察包换成内存实现，不读写任何任务证据表。
    optimization = get_settings().quality_optimization
    return RuleSandboxService(
        rules=PostgresRuleRepository(),
        evidence_reader_factory=SandboxRuleJudgeEvidenceReader,
        semantic_judge_factory=lambda: LLMSemanticRuleJudge(
            request_timeout_seconds=optimization.semantic_request_timeout_seconds,
            circuit_seconds=optimization.semantic_circuit_seconds,
            # 证据是人挑的：按看得见的判，别把缺操作记录/页面结构当证据不足
            manual_test=True,
        ),
        verdict_mode=optimization.verdict_mode,
        # 只给截图时让模型把字抄出来，规则引擎类规则才有页面文字可查
        screenshot_text_reader=read_screenshot_text,
    )


@lru_cache
def _get_rule_test_record_service() -> RuleTestRecordService:
    return RuleTestRecordService(
        repository=PostgresRuleTestRecordRepository(),
        artifact_store_factory=lambda: build_evidence_artifact_store(
            get_settings().evidence_storage
        ),
    )


async def _require_rules_permission(
    current_user: Annotated[AuthUserResponse, Depends(require_auth_user)],
) -> AuthUserResponse:
    # 规则测试会调用模型、属于规则配置能力，只开放给体验中心管理员（与规则写权限同口径）
    if "qcRules:write" not in current_user.permissions:
        raise AliothError(message="只有体验中心管理员可以测试规则", status_code=403)
    return current_user


@router.get(
    "/rule-tests/options",
    response_model=RuleTestOptionsResponse,
    summary="规则测试：可选规则及每条规则要的证据",
)
async def list_rule_test_options(
    _: Annotated[AuthUserResponse, Depends(_require_rules_permission)],
    service: Annotated[RuleSandboxService, Depends(_get_rule_sandbox_service)],
) -> RuleTestOptionsResponse:
    options = await service.list_options()
    catalog = get_default_page_type_registry().catalog
    return RuleTestOptionsResponse(
        items=[_option_response(item) for item in options],
        page_types=[
            RuleTestPageTypeResponse(code=item.code, name=item.name)
            for item in catalog.page_types
            if item.status == "supported"
        ],
    )


@router.post(
    "/rule-tests",
    response_model=RuleTestRunResponse,
    summary="规则测试：用人工证据跑一条规则（与正式任务同一套判定，只存测试记录、不落正式结果）",
)
async def run_rule_test(
    current_user: Annotated[AuthUserResponse, Depends(_require_rules_permission)],
    service: Annotated[RuleSandboxService, Depends(_get_rule_sandbox_service)],
    records: Annotated[RuleTestRecordService, Depends(_get_rule_test_record_service)],
    rule_id: Annotated[str, Form()],
    platform: Annotated[SandboxPlatform, Form()] = SandboxPlatform.H5,
    check_mode: Annotated[SandboxCheckMode, Form()] = SandboxCheckMode.RELAXED,
    page_type: Annotated[str, Form(max_length=64)] = "",
    page_state: Annotated[str, Form(max_length=64)] = "",
    page_url: Annotated[str, Form(max_length=2048)] = "",
    page_title: Annotated[str, Form(max_length=512)] = "",
    app_package_name: Annotated[str, Form(max_length=255)] = "",
    visible_text: Annotated[str, Form()] = "",
    dom_html: Annotated[str, Form()] = "",
    ui_snapshot: Annotated[str, Form()] = "",
    judge_input: Annotated[str, Form()] = "",
    screenshots: Annotated[list[UploadFile] | None, File()] = None,
    # 规则比「操作前 / 操作后」两张截图时的操作前那张
    before_screenshot: Annotated[UploadFile | None, File()] = None,
    # 规则比同一商品两个位置的价格时，第二个位置的页面文字
    second_location_text: Annotated[str, Form()] = "",
) -> RuleTestRunResponse:
    if not _is_uuid(rule_id):
        raise AliothError(message=f"规则 {rule_id} 不存在", status_code=404)
    if len(visible_text) > MAX_VISIBLE_TEXT_CHARACTERS:
        raise AliothError(message="页面文字太长，请控制在 20 万字以内", status_code=422)
    if len(second_location_text) > MAX_VISIBLE_TEXT_CHARACTERS:
        raise AliothError(message="第二个位置的页面文字太长，请控制在 20 万字以内", status_code=422)
    if len(dom_html) > MAX_DOM_CHARACTERS:
        raise AliothError(message="页面源码太长，请控制在 200 万字符以内", status_code=422)
    evidence = RuleSandboxEvidence(
        platform=platform,
        page_url=page_url,
        page_title=page_title,
        page_type=page_type,
        page_state=page_state,
        visible_text=visible_text,
        dom_html=dom_html,
        ui_snapshot=_json_object_field(ui_snapshot, label="界面快照") or None,
        app_package_name=app_package_name,
        judge_input=_json_object_field(judge_input, label="补充判定输入"),
        screenshots=await _read_screenshots(screenshots or []),
        before_screenshot=(
            await _read_screenshot(before_screenshot, label="操作前的截图")
            if before_screenshot is not None and before_screenshot.filename
            else None
        ),
        second_location_text=second_location_text,
    )
    try:
        # 测试用的模型调用记为调试用量，不挂任何任务
        with bind_llm_usage_scope(scope_type="debug", source="rule_sandbox_test"):
            result = await service.run(rule_id=rule_id, evidence=evidence, check_mode=check_mode)
    except RuleSandboxRuleNotFoundError as exc:
        raise AliothError(message=str(exc), status_code=404) from exc
    except RuleSandboxEvidenceError as exc:
        raise AliothError(message=str(exc), status_code=422) from exc
    response = _run_response(result)
    try:
        response.record_id = await records.save(
            tester_id=current_user.id,
            tester_name=current_user.full_name or current_user.username,
            # 记录里的页面文字要和判定用的一致：从截图读出来的也存进去
            evidence=(
                replace(evidence, visible_text=result.screenshot_text)
                if result.screenshot_text and not evidence.visible_text.strip()
                else evidence
            ),
            result=result,
        )
    except Exception:  # noqa: BLE001 - 存记录失败不影响这次测试结果
        logger.exception("规则测试记录保存失败：rule_id=%s", rule_id)
        response.notices.append("这次结果没能存进测试记录（测试结果不受影响），可以联系技术人员查日志")
    return response


@router.get(
    "/rule-tests/records",
    response_model=RuleTestRecordPageResponse,
    summary="规则测试记录：按规则、结论、时间查，新的在前",
)
async def list_rule_test_records(
    _: Annotated[AuthUserResponse, Depends(_require_rules_permission)],
    records: Annotated[RuleTestRecordService, Depends(_get_rule_test_record_service)],
    keyword: str | None = None,
    outcome: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    page: int = 1,
    page_size: int = 20,
) -> RuleTestRecordPageResponse:
    safe_page = max(page, 1)
    safe_page_size = min(max(page_size, 1), 100)
    items, total = await records.list_records(
        keyword=(keyword or "").strip() or None,
        outcome=(outcome or "").strip() or None,
        created_from=_aware(created_from),
        created_to=_aware(created_to),
        page=safe_page,
        page_size=safe_page_size,
    )
    return RuleTestRecordPageResponse(
        items=[RuleTestRecordSummaryResponse.model_validate(item) for item in items],
        total=total,
        page=safe_page,
        page_size=safe_page_size,
    )


@router.post(
    "/rule-tests/records/delete",
    response_model=RuleTestRecordDeleteResponse,
    summary="规则测试记录：按勾选或按时间段批量删除（连同截图，删了找不回来）",
)
async def delete_rule_test_records(
    payload: RuleTestRecordDeleteRequest,
    current_user: Annotated[AuthUserResponse, Depends(_require_rules_permission)],
    records: Annotated[RuleTestRecordService, Depends(_get_rule_test_record_service)],
) -> RuleTestRecordDeleteResponse:
    try:
        deleted = await records.delete(
            record_ids=payload.record_ids,
            created_from=_aware(payload.created_from),
            created_to=_aware(payload.created_to),
        )
    except RuleTestRecordDeleteError as exc:
        raise AliothError(message=str(exc), status_code=422) from exc
    logger.info("规则测试记录已删除 %s 条：操作人=%s", deleted, current_user.username)
    return RuleTestRecordDeleteResponse(deleted=deleted)


@router.get(
    "/rule-tests/records/{record_id}",
    response_model=RuleTestRecordDetailResponse,
    summary="规则测试记录详情：当时的证据和结论",
)
async def get_rule_test_record(
    record_id: str,
    _: Annotated[AuthUserResponse, Depends(_require_rules_permission)],
    records: Annotated[RuleTestRecordService, Depends(_get_rule_test_record_service)],
) -> RuleTestRecordDetailResponse:
    try:
        record = await records.get_record(record_id)
    except RuleTestRecordNotFoundError as exc:
        raise AliothError(message=str(exc), status_code=404) from exc
    return RuleTestRecordDetailResponse.model_validate(
        {
            **record,
            "screenshots": [
                {
                    "index": index,
                    "filename": str(item.get("filename") or f"截图 {index}"),
                    "width": int(item.get("width") or 0),
                    "height": int(item.get("height") or 0),
                    "capture_role": str(item.get("capture_role") or "primary"),
                }
                for index, item in enumerate(record["screenshots"], start=1)
                if isinstance(item, dict)
            ],
            "result": record["result_payload"],
        }
    )


@router.get(
    "/rule-tests/records/{record_id}/screenshots/{index}",
    response_class=Response,
    summary="规则测试记录里的一张截图（PNG，从 1 开始数）",
)
async def get_rule_test_record_screenshot(
    record_id: str,
    index: int,
    _: Annotated[AuthUserResponse, Depends(_require_rules_permission)],
    records: Annotated[RuleTestRecordService, Depends(_get_rule_test_record_service)],
) -> Response:
    try:
        content = await records.read_screenshot(record_id, index)
    except RuleTestRecordNotFoundError as exc:
        raise AliothError(message=str(exc), status_code=404) from exc
    return Response(
        content=content,
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.post(
    "/rule-tests/page-capture",
    response_model=RuleTestPageCaptureResponse,
    summary="规则测试：按网址用正式采集程序抓一次页面（源码、文字、界面快照、截图），不落库",
)
async def capture_rule_test_page_endpoint(
    payload: RuleTestPageCaptureRequest,
    _: Annotated[AuthUserResponse, Depends(_require_rules_permission)],
) -> RuleTestPageCaptureResponse:
    if payload.platform.is_app:
        raise AliothError(message="App 页面没有网址，请直接上传截图", status_code=422)
    try:
        page = await capture_rule_test_page(
            url=payload.url,
            platform="web" if payload.platform is SandboxPlatform.WEB else "h5",
            keep_startup_layer=(payload.page_type or "").strip().casefold() == "startup_ad",
        )
    except RuleTestPageCaptureError as exc:
        raise AliothError(message=str(exc), status_code=422) from exc
    return RuleTestPageCaptureResponse(
        final_url=page.final_url,
        title=page.title,
        visible_text=page.visible_text,
        dom_html=page.dom_html,
        ui_snapshot=page.ui_snapshot,
        screenshot_png_base64=(
            base64.b64encode(page.screenshot_png).decode("ascii") if page.screenshot_png else None
        ),
        notices=list(page.notices),
    )


async def _read_screenshots(files: list[UploadFile]) -> tuple[SandboxScreenshot, ...]:
    uploads = [item for item in files if item.filename]
    if len(uploads) > MAX_SANDBOX_SCREENSHOTS:
        raise AliothError(
            message=f"截图最多 {MAX_SANDBOX_SCREENSHOTS} 张（判定一次最多看这么多张）",
            status_code=422,
        )
    return tuple(
        [
            await _read_screenshot(upload, label=f"第 {index} 张截图")
            for index, upload in enumerate(uploads, start=1)
        ]
    )


async def _read_screenshot(upload: UploadFile, *, label: str) -> SandboxScreenshot:
    raw = await upload.read(MAX_SCREENSHOT_BYTES + 1)
    if len(raw) > MAX_SCREENSHOT_BYTES:
        raise AliothError(message=f"{label}超过 8MB", status_code=422)
    try:
        payload, width, height = normalize_screenshot_png(raw)
    except ScreenshotDecodeError as exc:
        raise AliothError(message=f"{label}（{upload.filename}）{exc}", status_code=422) from exc
    return SandboxScreenshot(
        filename=str(upload.filename)[:120],
        payload=payload,
        width=width,
        height=height,
    )


def _json_object_field(value: str, *, label: str) -> dict[str, Any]:
    text = value.strip()
    if not text:
        return {}
    if len(text) > MAX_JSON_CHARACTERS:
        raise AliothError(message=f"{label}内容太长", status_code=422)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AliothError(
            message=f"{label}不是有效的 JSON（第 {exc.lineno} 行附近有错）", status_code=422
        ) from exc
    if not isinstance(parsed, dict):
        raise AliothError(message=f"{label}必须是 JSON 对象（用大括号包起来）", status_code=422)
    return parsed


def _is_uuid(value: str) -> bool:
    try:
        UUID(value)
    except (TypeError, ValueError):
        return False
    return True


def _aware(value: datetime | None) -> datetime | None:
    # 页面按 ISO 带时区传；不带时区的按 UTC 理解
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _option_response(option: RuleSandboxOption) -> RuleTestOptionResponse:
    return RuleTestOptionResponse(
        rule_id=option.rule_id,
        rule_code=option.rule_code,
        name=option.name,
        category=option.category,
        severity=option.severity,
        executor_type=option.executor_type,
        status=option.status,
        check_point=option.check_point,
        applicable_page_types=list(option.applicable_page_types),
        ruleset_id=option.ruleset_id,
        ruleset_code=option.ruleset_code,
        ruleset_name=option.ruleset_name,
        ruleset_version=option.ruleset_version,
        ruleset_status=option.ruleset_status,
        testable=option.testable,
        untestable_reason=option.untestable_reason,
        config_problem=option.config_problem,
        relaxable=option.relaxable,
        evidence_needs=[
            RuleTestEvidenceNeedResponse(
                artifact_type=need.artifact_type,
                label=need.label,
                form_supported=need.form_supported,
            )
            for need in option.evidence_needs
        ],
        min_screenshot_count=option.min_screenshot_count,
        required_page_states=list(option.required_page_states),
        target_clients=list(option.target_clients),
        notes=list(option.notes),
        judge_input_fields=list(option.judge_input_fields),
        extra_inputs=[
            RuleTestExtraInputResponse(
                role=item.role, kind=item.kind, label=item.label, hint=item.hint
            )
            for item in option.extra_inputs
        ],
    )


def _run_response(result: RuleSandboxRunResult) -> RuleTestRunResponse:
    return RuleTestRunResponse(
        rule_id=result.rule_id,
        rule_code=result.rule_code,
        rule_name=result.rule_name,
        executor_type=result.executor_type,
        rule_status=result.rule_status,
        check_mode=result.check_mode,
        outcome=result.outcome,
        result_summary=result.result_summary,
        problem_desc=result.problem_desc,
        confidence=result.confidence,
        confidence_threshold=result.confidence_threshold,
        findings=[
            RuleTestFindingResponse(summary=item.summary, evidence=list(item.evidence))
            for item in result.findings
        ],
        missing_evidence=list(result.missing_evidence),
        used_evidence=list(result.used_evidence),
        notices=list(result.notices),
        duration_ms=result.duration_ms,
        judge_input=result.judge_input,
        judge_output=result.judge_output,
        screenshot_text=result.screenshot_text,
    )
