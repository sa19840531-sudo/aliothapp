"""质检规则管理接口。"""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

import asyncpg
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from services.api.app.api.v1.auth import AuthUserResponse, require_auth_user
from services.api.app.application.aggregate_rule_config import (
    AggregateRuleConfigurationError,
    validate_enabled_aggregate_rule_configuration,
)
from services.api.app.application.rule_admin import (
    build_quick_rule_payload,
    merge_rule_business_fields,
    next_quick_rule_code,
    rule_enable_problem,
    rule_payload_from_row,
    ruleset_payload_from_row,
)
from services.api.app.core.errors import AliothError
from services.api.app.domain.page.catalog import InvalidPageTypeCodeError
from services.api.app.domain.page.registry import get_default_page_type_registry
from services.api.app.infrastructure.db.rule_repository import PostgresRuleRepository

router = APIRouter()

RuleCategory = Literal[
    'text',
    'visual',
    'interaction',
    'ad',
    'payment',
    'input_risk',
    'player',
    'compliance',
]

RuleCategory = Literal[
    'text',
    'visual',
    'interaction',
    'ad',
    'payment',
    'input_risk',
    'player',
    'compliance',
]


class RulesetPayload(BaseModel):
    # 留空时由系统生成编码（业务新增规则集只填名称、状态、说明）
    ruleset_code: str = Field(default='', max_length=128)
    name: str = Field(..., min_length=1, max_length=255)
    version: int = Field(default=1, ge=1)
    status: Literal['draft', 'enabled', 'disabled', 'archived'] = 'draft'
    description: str = ''
    source_type: Literal['manual', 'spreadsheet', 'import', 'api'] = 'manual'
    source_ref: str = ''
    page_sampling_policy: dict[str, Any] = Field(default_factory=dict)
    loop_guard: dict[str, Any] = Field(default_factory=dict)


class RulesetResponse(BaseModel):
    id: str
    ruleset_code: str
    name: str
    version: int
    status: str
    description: str | None
    source_type: str
    source_ref: str | None
    snapshot_hash: str | None
    rule_count: int
    page_sampling_policy: dict[str, Any]
    loop_guard: dict[str, Any]
    created_by: str | None
    published_at: datetime | None
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime
    # 实时统计（列表接口才带）：rule_count 是入库时的静态值，不随增删规则更新
    total_rule_count: int | None = None
    enabled_rule_count: int | None = None


class RulePayload(BaseModel):
    ruleset_id: str = Field(..., min_length=1)
    rule_code: str = Field(..., min_length=1, max_length=128)
    name: str = Field(..., min_length=1, max_length=255)
    description: str = ''
    category: RuleCategory
    executor_type: Literal['llm', 'rule_engine', 'hybrid']
    severity: Literal['low', 'medium', 'high', 'critical'] = 'medium'
    status: Literal['draft', 'enabled', 'disabled', 'archived'] = 'draft'
    scope: str = ''
    # None = 不限渠道（种子规则的默认口径）；编辑时必须能原样保留
    channel: Literal['workbench', 'telegram', 'api'] | None = 'workbench'
    page_type: str = ''
    applicable_page_types: list[str] = Field(default_factory=list)
    url_pattern: str = ''
    steps: list[dict[str, Any]] = Field(default_factory=list)
    input_contract: dict[str, Any] = Field(default_factory=dict)
    check_point: str = Field(..., min_length=1)
    judge_config: dict[str, Any] = Field(default_factory=dict)
    manual_review_policy: dict[str, Any] = Field(default_factory=dict)
    sort_order: int = 100

    @model_validator(mode="after")
    def validate_page_types(self) -> RulePayload:
        from services.api.app.application.web_rule_evidence_contract import (
            validate_web_sequence_execution,
        )

        validate_web_sequence_execution(
            self.input_contract, executor_type=self.executor_type, judge_config=self.judge_config,
        )
        capability = self.judge_config.get("capability_config")
        if isinstance(capability, dict) and capability.get("evidence_input_scope") == "web_sequence":
            validate_enabled_aggregate_rule_configuration(self.judge_config)
        catalog = get_default_page_type_registry().catalog
        for page_type in self.applicable_page_types:
            catalog.require_code(page_type)
        if self.status == "enabled" and not self.applicable_page_types:
            raise ValueError("enabled规则必须配置applicable_page_types")
        if self.page_type:
            catalog.require_code(self.page_type)
            if self.page_type not in self.applicable_page_types:
                raise ValueError("page_type必须包含在applicable_page_types中")
        for step in self.steps:
            expected_page_type = step.get("expected_page_type")
            if expected_page_type:
                try:
                    catalog.require_code(str(expected_page_type))
                except InvalidPageTypeCodeError as exc:
                    raise ValueError(str(exc)) from exc
        if self.status == "enabled":
            validate_enabled_aggregate_rule_configuration(self.judge_config)
        return self


class RuleResponse(BaseModel):
    id: str
    ruleset_id: str
    rule_code: str
    name: str
    description: str | None
    category: str
    executor_type: str
    severity: str
    status: str
    scope: str | None
    channel: str | None
    page_type: str | None
    applicable_page_types: list[str]
    url_pattern: str | None
    steps: list[dict[str, Any]]
    input_contract: dict[str, Any]
    check_point: str
    judge_config: dict[str, Any]
    manual_review_policy: dict[str, Any]
    sort_order: int
    created_by: str | None
    created_at: datetime
    updated_at: datetime
    # 跨规则集列表时带上所属规则集，便于表格展示
    ruleset_code: str | None = None
    ruleset_name: str | None = None
    ruleset_version: int | None = None
    source_type: str | None = None


class RuleBusinessPayload(BaseModel):
    """基础业务字段及显式证据范围；其余系统配置不接收客户端覆盖。"""

    model_config = ConfigDict(extra='forbid')

    name: str = Field(..., min_length=1, max_length=255)
    check_point: str = Field(..., min_length=1)
    applicable_page_types: list[str] = Field(..., min_length=1)
    severity: Literal['low', 'medium', 'high', 'critical'] = 'medium'
    evidence_input_scope: Literal['page', 'journey'] | None = None
    journey_rule_contract: dict[str, Any] | None = None

    @field_validator('journey_rule_contract')
    @classmethod
    def validate_journey_contract(cls, value):
        from services.api.app.application.h5_journey_rule_contract import parse_rule_contract

        return parse_rule_contract(value) if value is not None else None


class RuleStatusPayload(BaseModel):
    status: Literal['enabled', 'disabled']


class RulesetBusinessPayload(BaseModel):
    """规则集业务字段：名称、状态、说明；编码、版本、抽样与防死循环策略原样保留。"""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    status: Literal['draft', 'enabled', 'disabled', 'archived'] | None = None


class PagedRulesetResponse(BaseModel):
    items: list[RulesetResponse]
    total: int
    page: int
    page_size: int


class PagedRuleResponse(BaseModel):
    items: list[RuleResponse]
    total: int
    page: int
    page_size: int


@lru_cache
def _get_rule_repository() -> PostgresRuleRepository:
    return PostgresRuleRepository()


async def _require_rules_permission(
    current_user: Annotated[AuthUserResponse, Depends(require_auth_user)],
) -> AuthUserResponse:
    if 'qcRules' not in current_user.permissions:
        raise AliothError(message='无权限访问该资源', status_code=403)
    return current_user


async def _require_rules_write_permission(
    current_user: Annotated[AuthUserResponse, Depends(require_auth_user)],
) -> AuthUserResponse:
    """平台规则与规则集的增删改、启停、发布只归体验中心管理员（2.1 原型配置中心权限口径）。"""
    if 'qcRules:write' not in current_user.permissions:
        raise AliothError(message='只有体验中心管理员可以修改平台规则和规则集', status_code=403)
    return current_user


def _require_uuid(value: str, *, label: str) -> None:
    try:
        UUID(value)
    except (TypeError, ValueError) as exc:
        raise AliothError(message=f'未找到 {label}={value}', status_code=404) from exc


async def _validate_journey_rule(data, repository):
    from services.api.app.application.h5_journey_rule_contract import (
        parse_rule_contract,
        validate_rule_definition,
    )

    config = data.get('judge_config', {}).get('capability_config', {})
    value = config.get('journey_rule_contract')
    if value is None:
        return
    try:
        value = parse_rule_contract(value)
        if config.get('evidence_input_scope') != 'journey' or data['input_contract'].get('target_clients') != ['H5']:
            raise ValueError('流程配置不能覆盖其他平台或汇总合同')
        definition = await repository.get_journey_definition(
            definition_id=value['definition_id'], version=value['version'],
        )
        validate_rule_definition(value, definition)
    except (ValueError, KeyError) as exc:
        raise AliothError(message=str(exc), status_code=422) from exc


def _validated_rule_payload(payload: dict[str, Any]) -> RulePayload:
    """服务端拼出的规则载荷也走同一套校验，校验失败按 422 返回大白话原因。"""
    try:
        return RulePayload(**payload)
    except ValidationError as exc:
        messages = [
            str(error.get('msg') or '').removeprefix('Value error, ')
            for error in exc.errors()
        ]
        raise AliothError(
            message='；'.join(item for item in messages if item) or '规则配置不合法',
            status_code=422,
        ) from exc


async def _ensure_ruleset_publishable(repository: PostgresRuleRepository, ruleset_id: str) -> None:
    """开启 / 发布规则集前，确认里面已开启的规则都能被正式任务执行。"""
    rules, _ = await repository.list_rules(
        ruleset_id=ruleset_id,
        keyword=None,
        category=None,
        status="enabled",
        page=1,
        page_size=10000,
    )
    try:
        for rule in rules:
            validate_enabled_aggregate_rule_configuration(rule.get("judge_config") or {})
    except AggregateRuleConfigurationError as exc:
        raise AliothError(
            message=f"规则集包含不可执行的聚合规则配置：{exc}",
            status_code=422,
        ) from exc


async def _default_ruleset_policies(repository: PostgresRuleRepository) -> dict[str, Any]:
    """业务新增规则集沿用当前启用规则集的抽样与防死循环策略，避免落成演示小额度。"""
    items, _ = await repository.list_rulesets(keyword=None, status='enabled', page=1, page_size=1)
    if not items:
        return {}
    return {
        'page_sampling_policy': dict(items[0].get('page_sampling_policy') or {}),
        'loop_guard': dict(items[0].get('loop_guard') or {}),
    }


@router.get('/rulesets', response_model=PagedRulesetResponse, summary='查询规则集列表')
async def list_rulesets(
    _: Annotated[AuthUserResponse, Depends(_require_rules_permission)],
    repository: Annotated[PostgresRuleRepository, Depends(_get_rule_repository)],
    keyword: str | None = None,
    status: Literal['draft', 'enabled', 'disabled', 'archived'] | None = None,
    page: int = 1,
    page_size: int = 10,
) -> PagedRulesetResponse:
    safe_page = max(page, 1)
    safe_page_size = min(max(page_size, 1), 100)
    items, total = await repository.list_rulesets(
        keyword=keyword,
        status=status,
        page=safe_page,
        page_size=safe_page_size,
    )
    return PagedRulesetResponse(
        items=[RulesetResponse.model_validate(item) for item in items],
        total=total,
        page=safe_page,
        page_size=safe_page_size,
    )


@router.post('/rulesets', response_model=RulesetResponse, status_code=201, summary='创建规则集')
async def create_ruleset(
    payload: RulesetPayload,
    _: Annotated[AuthUserResponse, Depends(_require_rules_write_permission)],
    repository: Annotated[PostgresRuleRepository, Depends(_get_rule_repository)],
) -> RulesetResponse:
    data = payload.model_dump()
    if not data['ruleset_code'].strip():
        data['ruleset_code'] = f'ruleset_{uuid4().hex[:8]}'
    if not data['page_sampling_policy'] and not data['loop_guard']:
        data.update(await _default_ruleset_policies(repository))
    try:
        row = await repository.create_ruleset(data)
    except asyncpg.UniqueViolationError as exc:
        raise AliothError(message='同编码同版本的规则集已存在', status_code=409) from exc
    return RulesetResponse.model_validate(row)


@router.get('/rulesets/{ruleset_id}', response_model=RulesetResponse, summary='获取规则集详情')
async def get_ruleset(
    ruleset_id: str,
    _: Annotated[AuthUserResponse, Depends(_require_rules_permission)],
    repository: Annotated[PostgresRuleRepository, Depends(_get_rule_repository)],
) -> RulesetResponse:
    row = await repository.get_ruleset(ruleset_id)
    if row is not None:
        return RulesetResponse.model_validate(row)
    raise AliothError(message=f'未找到 ruleset_id={ruleset_id} 对应的规则集', status_code=404)


@router.put(
    '/rulesets/{ruleset_id}', response_model=RulesetResponse, summary='更新规则集（全字段）'
)
async def update_ruleset(
    ruleset_id: str,
    payload: RulesetPayload,
    _: Annotated[AuthUserResponse, Depends(_require_rules_write_permission)],
    repository: Annotated[PostgresRuleRepository, Depends(_get_rule_repository)],
) -> RulesetResponse:
    data = payload.model_dump()
    if data['status'] == 'enabled':
        await _ensure_ruleset_publishable(repository, ruleset_id)
    row = await repository.update_ruleset(ruleset_id, data)
    if row is None:
        raise AliothError(message=f'未找到 ruleset_id={ruleset_id} 对应的规则集', status_code=404)
    return RulesetResponse.model_validate(row)


@router.patch(
    '/rulesets/{ruleset_id}',
    response_model=RulesetResponse,
    summary='修改规则集名称、状态、说明（编码、版本、抽样与防死循环策略原样保留）',
)
async def patch_ruleset(
    ruleset_id: str,
    payload: RulesetBusinessPayload,
    _: Annotated[AuthUserResponse, Depends(_require_rules_write_permission)],
    repository: Annotated[PostgresRuleRepository, Depends(_get_rule_repository)],
) -> RulesetResponse:
    _require_uuid(ruleset_id, label='ruleset_id')
    row = await repository.get_ruleset(ruleset_id)
    if row is None:
        raise AliothError(message=f'未找到 ruleset_id={ruleset_id} 对应的规则集', status_code=404)
    data = ruleset_payload_from_row(row)
    if payload.name is not None:
        data['name'] = payload.name.strip()
    if payload.description is not None:
        data['description'] = payload.description.strip()
    if payload.status is not None:
        data['status'] = payload.status
    if data['status'] == 'enabled' and row.get('status') != 'enabled':
        await _ensure_ruleset_publishable(repository, ruleset_id)
    updated = await repository.update_ruleset(ruleset_id, data)
    if updated is None:
        raise AliothError(message=f'未找到 ruleset_id={ruleset_id} 对应的规则集', status_code=404)
    return RulesetResponse.model_validate(updated)


@router.delete(
    '/rulesets/{ruleset_id}',
    status_code=204,
    response_model=None,
    summary='删除规则集',
)
async def delete_ruleset(
    ruleset_id: str,
    _: Annotated[AuthUserResponse, Depends(_require_rules_write_permission)],
    repository: Annotated[PostgresRuleRepository, Depends(_get_rule_repository)],
) -> None:
    deleted = await repository.delete_ruleset(ruleset_id)
    if not deleted:
        raise AliothError(message=f'未找到 ruleset_id={ruleset_id} 对应的规则集', status_code=404)


@router.post('/rulesets/{ruleset_id}/publish', response_model=RulesetResponse, summary='发布规则集')
async def publish_ruleset(
    ruleset_id: str,
    _: Annotated[AuthUserResponse, Depends(_require_rules_write_permission)],
    repository: Annotated[PostgresRuleRepository, Depends(_get_rule_repository)],
) -> RulesetResponse:
    await _ensure_ruleset_publishable(repository, ruleset_id)
    row = await repository.publish_ruleset(ruleset_id)
    if row is None:
        raise AliothError(message=f'未找到 ruleset_id={ruleset_id} 对应的规则集', status_code=404)
    return RulesetResponse.model_validate(row)


@router.get(
    '/rules', response_model=PagedRuleResponse, summary='查询规则列表（不传规则集 = 全部规则集）'
)
async def list_rules(
    _: Annotated[AuthUserResponse, Depends(_require_rules_permission)],
    repository: Annotated[PostgresRuleRepository, Depends(_get_rule_repository)],
    ruleset_id: str | None = None,
    keyword: str | None = None,
    category: RuleCategory | None = None,
    status: Literal['draft', 'enabled', 'disabled', 'archived'] | None = None,
    severity: Literal['low', 'medium', 'high', 'critical'] | None = None,
    source_type: Literal['manual', 'spreadsheet', 'import', 'api'] | None = None,
    page_type: str | None = None,
    page: int = 1,
    page_size: int = 10,
) -> PagedRuleResponse:
    safe_page = max(page, 1)
    safe_page_size = min(max(page_size, 1), 100)
    items, total = await repository.list_rules(
        ruleset_id=ruleset_id or None,
        keyword=keyword,
        category=category,
        status=status,
        page=safe_page,
        page_size=safe_page_size,
        severity=severity,
        source_type=source_type,
        page_type=(page_type or '').strip() or None,
    )
    return PagedRuleResponse(
        items=[RuleResponse.model_validate(item) for item in items],
        total=total,
        page=safe_page,
        page_size=safe_page_size,
    )


@router.post('/rules', response_model=RuleResponse, status_code=201, summary='创建规则（全字段）')
async def create_rule(
    payload: RulePayload,
    _: Annotated[AuthUserResponse, Depends(_require_rules_write_permission)],
    repository: Annotated[PostgresRuleRepository, Depends(_get_rule_repository)],
) -> RuleResponse:
    data = payload.model_dump()
    await _validate_journey_rule(data, repository)
    if data['status'] == 'enabled' and (problem := rule_enable_problem(data)):
        raise AliothError(message=problem, status_code=422)
    row = await repository.create_rule(data)
    return RuleResponse.model_validate(row)


@router.post(
    '/rulesets/{ruleset_id}/rules',
    response_model=RuleResponse,
    status_code=201,
    summary='业务新增规则：只填名称、详情、适用页面、优先级，系统字段自动补齐，默认关闭',
)
async def create_business_rule(
    ruleset_id: str,
    payload: RuleBusinessPayload,
    _: Annotated[AuthUserResponse, Depends(_require_rules_write_permission)],
    repository: Annotated[PostgresRuleRepository, Depends(_get_rule_repository)],
) -> RuleResponse:
    _require_uuid(ruleset_id, label='ruleset_id')
    if await repository.get_ruleset(ruleset_id) is None:
        raise AliothError(message=f'未找到 ruleset_id={ruleset_id} 对应的规则集', status_code=404)
    existing, _ = await repository.list_rules(
        ruleset_id=ruleset_id,
        keyword=None,
        category=None,
        status=None,
        page=1,
        page_size=10000,
    )
    codes = [str(item.get('rule_code') or '') for item in existing]
    sort_order = max([int(item.get('sort_order') or 0) for item in existing] or [0]) + 10
    for _attempt in range(3):
        rule_code = next_quick_rule_code(codes)
        data = build_quick_rule_payload(
            ruleset_id=ruleset_id,
            rule_code=rule_code,
            name=payload.name,
            check_point=payload.check_point,
            applicable_page_types=payload.applicable_page_types,
            severity=payload.severity,
            sort_order=sort_order,
        )
        if payload.evidence_input_scope == 'journey':
            data['input_contract']['target_clients'] = ['H5']
        try:
            validated = _validated_rule_payload(
                merge_rule_business_fields(data, evidence_input_scope=payload.evidence_input_scope,
                                           journey_rule_contract=payload.journey_rule_contract)
            )
        except ValueError as exc:
            raise AliothError(message=str(exc), status_code=422) from exc
        await _validate_journey_rule(validated.model_dump(), repository)
        try:
            row = await repository.create_rule(validated.model_dump())
        except asyncpg.UniqueViolationError:
            codes.append(rule_code)
            continue
        return RuleResponse.model_validate(row)
    raise AliothError(message='规则编号冲突，请稍后重试', status_code=409)


@router.get('/rules/{rule_id}', response_model=RuleResponse, summary='获取规则详情')
async def get_rule(
    rule_id: str,
    _: Annotated[AuthUserResponse, Depends(_require_rules_permission)],
    repository: Annotated[PostgresRuleRepository, Depends(_get_rule_repository)],
) -> RuleResponse:
    row = await repository.get_rule(rule_id)
    if row is not None:
        return RuleResponse.model_validate(row)
    raise AliothError(message=f'未找到 rule_id={rule_id} 对应的规则', status_code=404)


@router.put('/rules/{rule_id}', response_model=RuleResponse, summary='更新规则（全字段）')
async def update_rule(
    rule_id: str,
    payload: RulePayload,
    _: Annotated[AuthUserResponse, Depends(_require_rules_write_permission)],
    repository: Annotated[PostgresRuleRepository, Depends(_get_rule_repository)],
) -> RuleResponse:
    data = payload.model_dump()
    await _validate_journey_rule(data, repository)
    if data['status'] == 'enabled' and (problem := rule_enable_problem(data)):
        raise AliothError(message=problem, status_code=422)
    row = await repository.update_rule(rule_id, data)
    if row is None:
        raise AliothError(message=f'未找到 rule_id={rule_id} 对应的规则', status_code=404)
    return RuleResponse.model_validate(row)


@router.patch(
    '/rules/{rule_id}',
    response_model=RuleResponse,
    summary='修改规则名称、详情、适用页面、优先级（其余系统字段原样保留）',
)
async def patch_rule(
    rule_id: str,
    payload: RuleBusinessPayload,
    _: Annotated[AuthUserResponse, Depends(_require_rules_write_permission)],
    repository: Annotated[PostgresRuleRepository, Depends(_get_rule_repository)],
) -> RuleResponse:
    _require_uuid(rule_id, label='rule_id')
    row = await repository.get_rule(rule_id)
    if row is None:
        raise AliothError(message=f'未找到 rule_id={rule_id} 对应的规则', status_code=404)
    try:
        data = merge_rule_business_fields(
            row,
            name=payload.name,
            check_point=payload.check_point,
            applicable_page_types=payload.applicable_page_types,
            severity=payload.severity,
            evidence_input_scope=payload.evidence_input_scope,
            journey_rule_contract=payload.journey_rule_contract,
        )
    except ValueError as exc:
        raise AliothError(message=str(exc), status_code=422) from exc
    validated = _validated_rule_payload(data)
    await _validate_journey_rule(data, repository)
    if (payload.evidence_input_scope is not None or payload.journey_rule_contract is not None) and data['status'] == 'enabled':
        if problem := rule_enable_problem(data):
            raise AliothError(message=problem, status_code=422)
    updated = await repository.update_rule(rule_id, validated.model_dump())
    if updated is None:
        raise AliothError(message=f'未找到 rule_id={rule_id} 对应的规则', status_code=404)
    return RuleResponse.model_validate(updated)


@router.patch(
    '/rules/{rule_id}/status',
    response_model=RuleResponse,
    summary='开启 / 关闭规则（开启前校验正式任务能执行）',
)
async def patch_rule_status(
    rule_id: str,
    payload: RuleStatusPayload,
    _: Annotated[AuthUserResponse, Depends(_require_rules_write_permission)],
    repository: Annotated[PostgresRuleRepository, Depends(_get_rule_repository)],
) -> RuleResponse:
    _require_uuid(rule_id, label='rule_id')
    row = await repository.get_rule(rule_id)
    if row is None:
        raise AliothError(message=f'未找到 rule_id={rule_id} 对应的规则', status_code=404)
    data = rule_payload_from_row(row)
    data['status'] = payload.status
    if payload.status == 'enabled':
        await _validate_journey_rule(data, repository)
    if payload.status == 'enabled' and (problem := rule_enable_problem(data)):
        raise AliothError(message=problem, status_code=422)
    validated = _validated_rule_payload(data)
    updated = await repository.update_rule(rule_id, validated.model_dump())
    if updated is None:
        raise AliothError(message=f'未找到 rule_id={rule_id} 对应的规则', status_code=404)
    return RuleResponse.model_validate(updated)


@router.delete(
    '/rules/{rule_id}',
    status_code=204,
    response_model=None,
    summary='删除规则',
)
async def delete_rule(
    rule_id: str,
    _: Annotated[AuthUserResponse, Depends(_require_rules_write_permission)],
    repository: Annotated[PostgresRuleRepository, Depends(_get_rule_repository)],
) -> None:
    deleted = await repository.delete_rule(rule_id)
    if not deleted:
        raise AliothError(message=f'未找到 rule_id={rule_id} 对应的规则', status_code=404)
