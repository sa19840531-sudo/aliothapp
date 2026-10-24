"""V2 统一任务入口的公开请求与响应合同。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

from services.api.app.application.h5_shortflows import (
    H5ShortflowSelection,
    validate_h5_shortflow_selections,
)
from services.api.app.application.v2_task_entries import (
    V2_TIMEOUT_MAX_MINUTES,
    AppArtifactSource,
    AppArtifactSourceKind,
    ProductSnapshot,
    V2TaskSubmissionCommand,
)
from services.api.app.application.web_browser_profile import (
    DEFAULT_WEB_BROWSER_PROFILE_ID,
    WebBrowserProfileRegistry,
)
from services.api.app.application.web_page_applicability import (
    WebPageApplicabilitySnapshot,
    resolve_web_page_applicability,
)
from services.api.app.domain.page.registry import get_default_page_type_registry
from services.api.app.domain.page.web_page_catalog import WEB_BUSINESS_PAGE_TYPE_CODES
from services.api.app.domain.task import (
    ADULT_SELF_ATTESTATION_STATEMENT_VERSION,
    TaskTargetPlatform,
)


class ProductSnapshotRequest(BaseModel):
    """创建任务时锁定的产品快照。"""

    department_name: str = Field(default="-", max_length=128)
    product_code: str = Field(default="-", max_length=128)
    product_name: str = Field(default="-", max_length=128)


class DeviceRequest(BaseModel):
    """App 运行设备选择。"""

    backend: str = Field(default="cloud_phone", min_length=1, max_length=64)
    profile_id: str = Field(default="default", min_length=1, max_length=128)

    @field_validator("backend", "profile_id")
    @classmethod
    def validate_non_blank_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("设备配置不能为空")
        return normalized


class AppExplorationTargetRequest(BaseModel):
    """App 产品覆盖目标；提交后锁定为带哈希快照。"""

    required_page_types: list[str] = Field(default_factory=list, max_length=100)
    optional_page_types: list[str] = Field(default_factory=list, max_length=100)
    lifecycle_page_types: list[str] = Field(default_factory=list, max_length=100)
    applicability_mode: (
        Literal[
            "dynamic_discovery",
            "explicit_targets",
            "profile_declared",
        ]
        | None
    ) = None
    milestones: list[str] = Field(default_factory=list, max_length=100)
    minimum_business_pages: int | None = Field(default=None, ge=1, le=100)
    max_actions: int | None = Field(default=None, ge=1, le=600)
    device_capability_template: Literal[
        "android_visual_readonly_v1",
        "android_visual_evidence_v1",
    ] = "android_visual_readonly_v1"

    @field_validator(
        "required_page_types",
        "optional_page_types",
        "lifecycle_page_types",
        "milestones",
    )
    @classmethod
    def validate_target_items(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for item in values:
            value = item.strip()
            if not value or len(value) > 256:
                raise ValueError("App 探索目标单项必须为 1 到 256 字符")
            if value not in normalized:
                normalized.append(value)
        return normalized

    @field_validator("required_page_types")
    @classmethod
    def validate_catalog_page_types(cls, values: list[str]) -> list[str]:
        allowed = set(get_default_page_type_registry().coverage_required_codes)
        normalized = list(dict.fromkeys(item.casefold() for item in values))
        invalid = sorted(set(normalized) - allowed)
        if invalid:
            raise ValueError(
                "App required_page_types 不属于业务页面类型目录：" + ", ".join(invalid)
            )
        return normalized

    @field_validator("optional_page_types")
    @classmethod
    def validate_optional_catalog_page_types(cls, values: list[str]) -> list[str]:
        allowed = set(get_default_page_type_registry().coverage_required_codes)
        normalized = list(dict.fromkeys(item.casefold() for item in values))
        invalid = sorted(set(normalized) - allowed)
        if invalid:
            raise ValueError(
                "App optional_page_types 不属于业务页面类型目录：" + ", ".join(invalid)
            )
        return normalized

    @field_validator("lifecycle_page_types")
    @classmethod
    def validate_lifecycle_page_types(cls, values: list[str]) -> list[str]:
        allowed = set(get_default_page_type_registry().lifecycle_codes)
        normalized = list(dict.fromkeys(item.casefold() for item in values))
        invalid = sorted(set(normalized) - allowed)
        if invalid:
            raise ValueError(
                "App lifecycle_page_types 不属于生命周期页面类型目录：" + ", ".join(invalid)
            )
        return normalized

    @model_validator(mode="after")
    def validate_page_type_partition(self) -> AppExplorationTargetRequest:
        overlap = sorted(set(self.required_page_types) & set(self.optional_page_types))
        if overlap:
            raise ValueError(
                "App required_page_types 与 optional_page_types 不能交叉：" + ", ".join(overlap)
            )
        return self


class H5ExplorationTargetRequest(BaseModel):
    """H5 产品覆盖目标；缺省时由正式发现事实形成动态覆盖分母。"""

    required_page_types: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("required_page_types")
    @classmethod
    def validate_catalog_page_types(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for item in values:
            value = item.strip().casefold()
            if not value or len(value) > 256:
                raise ValueError("H5 required_page_types 单项必须为 1 到 256 字符")
            if value not in normalized:
                normalized.append(value)
        allowed = set(get_default_page_type_registry().coverage_required_codes)
        invalid = sorted(set(normalized) - allowed)
        if invalid:
            raise ValueError("H5 required_page_types 不属于业务页面类型目录：" + ", ".join(invalid))
        return normalized


class WebExplorationTargetRequest(BaseModel):
    """Web 产品覆盖目标；默认由本次运行的入口与证据动态确定业务分区。"""

    required_page_types: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="before")
    @classmethod
    def reject_legacy_execution_overrides(cls, value: object) -> object:
        if isinstance(value, dict) and set(value) & {
            "applicability_mode",
            "discovery_policy",
            "direct_ad_page_enabled",
            "menu_registry_version",
            "registry_version",
            "site_menu_registry_version",
        }:
            raise PydanticCustomError(
                "WEB_LEGACY_EXECUTION_OVERRIDE_FORBIDDEN",
                "WEB_LEGACY_EXECUTION_OVERRIDE_FORBIDDEN",
            )
        return value

    @field_validator("required_page_types")
    @classmethod
    def validate_catalog_page_types(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for item in values:
            value = item.strip().casefold()
            if not value or len(value) > 256:
                raise ValueError("Web required_page_types 单项必须为 1 到 256 字符")
            if value not in normalized:
                normalized.append(value)
        allowed = set(WEB_BUSINESS_PAGE_TYPE_CODES)
        invalid = sorted(set(normalized) - allowed)
        if invalid:
            raise ValueError(
                "Web required_page_types 不属于业务页面类型目录：" + ", ".join(invalid)
            )
        return normalized


class BuildReferenceSourceRequest(BaseModel):
    """已上传并验证的 App 制品引用。"""

    type: Literal["build_ref"]
    platform_build_ref: str = Field(min_length=1, max_length=128)

    @field_validator("platform_build_ref")
    @classmethod
    def validate_platform_build_ref(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized.startswith("app-build:"):
            raise ValueError("platform_build_ref 必须使用 app-build:<uuid> 格式")
        try:
            UUID(normalized.removeprefix("app-build:"))
        except ValueError as exc:
            raise ValueError("platform_build_ref 必须使用 app-build:<uuid> 格式") from exc
        return normalized


class DownloadUrlSourceRequest(BaseModel):
    """等待制品接入方导入的 App 下载链接。"""

    type: Literal["download_url"]
    download_url: str = Field(min_length=1, max_length=4096)

    @field_validator("download_url")
    @classmethod
    def validate_download_url(cls, value: str) -> str:
        normalized = value.strip()
        parsed = urlsplit(normalized)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise ValueError("download_url 必须是 HTTP(S) URL")
        return normalized


AppSourceRequest = Annotated[
    BuildReferenceSourceRequest | DownloadUrlSourceRequest,
    Field(discriminator="type"),
]


class H5TargetRequest(BaseModel):
    """H5 移动页面目标。"""

    platform: Literal["h5"]
    url: str = Field(min_length=1, max_length=4096)
    exploration_target: H5ExplorationTargetRequest = Field(
        default_factory=H5ExplorationTargetRequest
    )

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return _validated_h5_url(value)


class AdultSelfAttestationRequest(BaseModel):
    """客户端只能主动确认声明内容，身份与时间由服务端冻结。"""

    model_config = ConfigDict(extra="forbid")

    declared: Literal[True]
    statement_version: Literal["adult-v1"] = ADULT_SELF_ATTESTATION_STATEMENT_VERSION


class WebTargetRequest(BaseModel):
    """桌面 Web 页面目标。"""

    platform: Literal["web"]
    url: str = Field(min_length=1, max_length=4096)
    browser_profile_id: str = Field(
        default=DEFAULT_WEB_BROWSER_PROFILE_ID,
        min_length=1,
        max_length=128,
    )
    exploration_target: WebExplorationTargetRequest = Field(
        default_factory=WebExplorationTargetRequest
    )
    adult_self_attestation: AdultSelfAttestationRequest | None = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return _validated_http_url(value, field_name="url")

    @field_validator("browser_profile_id")
    @classmethod
    def validate_browser_profile_id(cls, value: str) -> str:
        return WebBrowserProfileRegistry().resolve(value).profile_id


class AppTargetRequest(BaseModel):
    """Android App 目标。"""

    platform: Literal["app"]
    source: AppSourceRequest
    package_name: str | None = Field(default=None, max_length=255)
    device: DeviceRequest = Field(default_factory=DeviceRequest)
    exploration_target: AppExplorationTargetRequest = Field(
        default_factory=AppExplorationTargetRequest
    )


TaskTargetRequest = Annotated[
    H5TargetRequest | WebTargetRequest | AppTargetRequest,
    Field(discriminator="platform"),
]


class V2TaskSubmissionRequest(BaseModel):
    """完整质检和页面探索测试共用的提交参数。"""

    task_name: str = Field(min_length=1, max_length=256)
    target: TaskTargetRequest
    # 不填 = 跑全部开启的规则集；ruleset_codes 选多个；ruleset_code 只锁单个规则集（老调用方）
    ruleset_code: str | None = Field(default=None, min_length=1, max_length=128)
    ruleset_version: int | None = Field(default=None, ge=1)
    ruleset_codes: list[str] | None = Field(default=None, max_length=20)
    channel: Literal["workbench", "telegram", "api"] = "workbench"
    priority: int = Field(default=100, ge=0)
    product: ProductSnapshotRequest = Field(default_factory=ProductSnapshotRequest)
    scan_mode: Literal["full", "quick", "custom"] = "full"
    timeout_minutes: int | None = Field(default=None, ge=1, le=V2_TIMEOUT_MAX_MINUTES)
    # App 任務忽略 timeout_minutes，牆鐘不限時。其他平台為真時也不使用 150／180 分鐘牆鐘。
    unlimited_duration: bool = False
    journey_steps: list[str] = Field(default_factory=list, max_length=100)
    h5_shortflows: list[H5ShortflowSelection] = Field(default_factory=list, max_length=10)
    remark: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_shortflows(self):
        if self.h5_shortflows:
            if self.target.platform != "h5":
                raise ValueError("h5_shortflows 只允许 H5 任务")
            validate_h5_shortflow_selections([item.model_dump() for item in self.h5_shortflows])
        return self

    @field_validator("task_name")
    @classmethod
    def validate_non_blank_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("字段不能为空")
        return normalized

    @field_validator("ruleset_code")
    @classmethod
    def validate_optional_ruleset_code(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("字段不能为空")
        return normalized

    @field_validator("ruleset_codes")
    @classmethod
    def validate_ruleset_codes(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        normalized: list[str] = []
        for item in values:
            value = item.strip()
            if not value or len(value) > 128:
                raise ValueError("ruleset_codes 单项必须为 1 到 128 字符")
            if value not in normalized:
                normalized.append(value)
        return normalized

    def to_command(self) -> V2TaskSubmissionCommand:
        target_url: str | None = None
        app_source: AppArtifactSource | None = None
        package_name: str | None = None
        device_backend = "cloud_phone"
        device_profile_id = "default"
        required_page_types: tuple[str, ...] = ()
        optional_page_types: tuple[str, ...] = ()
        lifecycle_page_types: tuple[str, ...] = ()
        applicability_mode: str | None = None
        web_applicability_snapshot: WebPageApplicabilitySnapshot | None = None
        required_milestones: tuple[str, ...] = ()
        minimum_business_pages: int | None = None
        device_capability_template = "android_visual_readonly_v1"
        app_max_actions: int | None = None
        browser_profile_id: str | None = None
        adult_self_attestation_declared: bool | None = None
        adult_self_attestation_statement_version: str | None = None
        if isinstance(self.target, AppTargetRequest):
            package_name = _optional_text(self.target.package_name)
            device_backend = self.target.device.backend.strip()
            device_profile_id = self.target.device.profile_id.strip()
            required_page_types = tuple(self.target.exploration_target.required_page_types)
            optional_page_types = tuple(self.target.exploration_target.optional_page_types)
            lifecycle_page_types = tuple(self.target.exploration_target.lifecycle_page_types)
            applicability_mode = self.target.exploration_target.applicability_mode
            required_milestones = tuple(self.target.exploration_target.milestones)
            minimum_business_pages = self.target.exploration_target.minimum_business_pages
            app_max_actions = self.target.exploration_target.max_actions
            device_capability_template = self.target.exploration_target.device_capability_template
            if isinstance(self.target.source, BuildReferenceSourceRequest):
                app_source = AppArtifactSource(
                    kind=AppArtifactSourceKind.BUILD_REF,
                    platform_build_ref=self.target.source.platform_build_ref,
                )
            else:
                app_source = AppArtifactSource(
                    kind=AppArtifactSourceKind.DOWNLOAD_URL,
                    download_url=self.target.source.download_url,
                )
        else:
            target_url = self.target.url.strip()
            if isinstance(self.target, H5TargetRequest):
                required_page_types = tuple(self.target.exploration_target.required_page_types)
            else:
                browser_profile_id = self.target.browser_profile_id
                web_applicability_snapshot = resolve_web_page_applicability(
                    target_url=target_url,
                    requested_mode=None,
                    requested_required_page_types=tuple(
                        self.target.exploration_target.required_page_types
                    ),
                )
                required_page_types = web_applicability_snapshot.required_page_types
                optional_page_types = web_applicability_snapshot.optional_page_types
                applicability_mode = web_applicability_snapshot.applicability_mode
                if self.target.adult_self_attestation is not None:
                    adult_self_attestation_declared = self.target.adult_self_attestation.declared
                    adult_self_attestation_statement_version = (
                        self.target.adult_self_attestation.statement_version
                    )

        return V2TaskSubmissionCommand(
            task_name=self.task_name.strip(),
            platform=TaskTargetPlatform(self.target.platform),
            target_url=target_url,
            app_source=app_source,
            package_name=package_name,
            device_backend=device_backend,
            device_profile_id=device_profile_id,
            ruleset_code=self.ruleset_code,
            ruleset_version=self.ruleset_version,
            ruleset_codes=tuple(self.ruleset_codes or ()),
            channel=self.channel,
            priority=self.priority,
            product=ProductSnapshot(
                department_name=self.product.department_name.strip(),
                product_code=self.product.product_code.strip(),
                product_name=self.product.product_name.strip(),
            ),
            scan_mode=self.scan_mode,
            timeout_minutes=self.timeout_minutes,
            unlimited_duration=self.unlimited_duration,
            journey_steps=tuple(step.strip() for step in self.journey_steps if step.strip()),
            h5_shortflows=tuple(item.model_dump() for item in self.h5_shortflows),
            required_page_types=required_page_types,
            optional_page_types=optional_page_types,
            lifecycle_page_types=lifecycle_page_types,
            applicability_mode=applicability_mode,
            web_applicability_snapshot=web_applicability_snapshot,
            web_discovery_policy=None,
            required_milestones=required_milestones,
            minimum_business_pages=minimum_business_pages,
            device_capability_template=device_capability_template,
            remark=_optional_text(self.remark),
            app_max_actions=app_max_actions,
            browser_profile_id=browser_profile_id,
            adult_self_attestation_declared=adult_self_attestation_declared,
            adult_self_attestation_statement_version=(adult_self_attestation_statement_version),
            # 质检范围取用户原样选的页面类型；Web 的覆盖分母会另外补首页，不能拿来当范围
            inspection_scope_page_types=tuple(self.target.exploration_target.required_page_types),
        )


class V2TaskSubmissionResponse(BaseModel):
    """完整 V2 任务提交响应。"""

    task_id: str
    run_id: str
    ruleset_id: str
    status: str


class ExplorationTaskSubmissionResponse(BaseModel):
    """页面探索测试任务提交响应。"""

    task_id: str
    run_id: str
    status: str


class RuleEvaluationSubmissionRequest(BaseModel):
    """独立规则质检运行提交请求；不传或传空表示测全部命中规则。"""

    rule_codes: list[str] | None = Field(default=None, max_length=200)


class RuleEvaluationSubmissionResponse(BaseModel):
    """独立规则质检运行提交响应。"""

    task_id: str
    rule_evaluation_run_id: str
    status: str


class RuleEvaluationRunSummary(BaseModel):
    """独立规则质检运行摘要（不含逐规则结果正文）。"""

    run_id: str
    status: str
    page_count: int
    rule_count: int
    requested_rule_codes: list[str]
    error_message: str | None
    created_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None


class RuleEvaluationRunListResponse(BaseModel):
    """任务的独立规则质检运行列表（新→旧）。"""

    task_id: str
    runs: list[RuleEvaluationRunSummary]


class RuleEvaluationRunDetailResponse(RuleEvaluationRunSummary):
    """单次独立规则质检运行详情，含逐规则结果。"""

    task_id: str
    results: list[dict[str, Any]]


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _validated_http_url(value: str, *, field_name: str) -> str:
    normalized = value.strip()
    parsed = urlsplit(normalized)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{field_name} 必须是 HTTP(S) URL")
    return normalized


_H5_URL_VALIDATION_MESSAGE = "url 必须是有效且无凭据的 HTTP(S) URL"


def _validated_h5_url(value: str) -> str:
    try:
        normalized = _validated_http_url(value, field_name="url")
        parsed = urlsplit(normalized)
        _ = parsed.port
    except ValueError:
        raise ValueError(_H5_URL_VALIDATION_MESSAGE) from None
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(_H5_URL_VALIDATION_MESSAGE)
    return normalized
