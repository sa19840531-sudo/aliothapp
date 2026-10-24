import logging
import math
import os
from datetime import UTC, datetime
from functools import lru_cache
from ipaddress import ip_address
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from dotenv import dotenv_values, find_dotenv
from pydantic import BaseModel, Field

from services.api.app.core.h5_auth_config import H5AuthSettings

logger = logging.getLogger(__name__)

DEFAULT_WEB_INTERACTION_TIMEOUT_SECONDS = 180.0


def load_web_menu_collection_limit() -> int:
    """读取并严格校验新 Web Run 的菜单采集上限。"""
    raw = os.getenv("ALIOTH_WEB_MENU_COLLECTION_LIMIT", "10").strip()
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError("ALIOTH_WEB_MENU_COLLECTION_LIMIT 必须为整数") from error
    if not 1 <= value <= 1000:
        raise ValueError("ALIOTH_WEB_MENU_COLLECTION_LIMIT 必须在 1..1000 范围内")
    return value

# Agent Framework 的各家客户端不会自动读取 .env 文件，统一在配置模块加载一次，
# 保证 ALIOTH_* 与后续可能新增的第三方 SDK 环境变量都能在本地开发时从环境文件生效。
_ENV_FILE_NAMES = (".env", ".env.local")


def _load_dotenv_files(file_names: tuple[str, ...] = _ENV_FILE_NAMES) -> None:
    """按 .env -> .env.local 加载本地配置，同时保留进程环境变量最高优先级。"""
    protected_keys = set(os.environ)
    for file_name in file_names:
        env_path = find_dotenv(file_name, usecwd=True)
        if not env_path:
            continue
        for key, value in dotenv_values(env_path).items():
            if value is None or key in protected_keys:
                continue
            os.environ[key] = value


_load_dotenv_files()


class LLMSettings(BaseModel):
    """大模型 API 调用相关配置，统一通过 OpenAI 兼容协议接入（OpenAI/Azure OpenAI/x.ai 等）。"""

    provider: str = Field(default="openai")
    """供应商标识：openai | azure_openai | xai | custom。"""

    api_style: str = Field(default="responses")
    """接口风格：responses（Responses API） | chat_completion（Chat Completions API）。"""

    model: str = Field(default="gpt-4o-mini")
    api_key: str | None = Field(default=None)
    base_url: str | None = Field(default=None)
    reasoning_effort: Literal["none", "low", "medium", "high"] | None = Field(default=None)
    """模型推理强度；留空时沿用供应商默认值。"""

    azure_endpoint: str | None = Field(default=None)
    azure_api_version: str | None = Field(default=None)
    timeout_seconds: float = Field(default=60.0)
    max_retries: int = Field(default=2)
    cost_currency: str = Field(default="CNY")
    input_price_per_million_tokens: float = Field(default=0.0, ge=0.0)
    output_price_per_million_tokens: float = Field(default=0.0, ge=0.0)
    cache_hit_price_per_million_tokens: float = Field(default=0.0, ge=0.0)
    long_context_threshold_tokens: int | None = Field(default=None)
    long_context_input_price_per_million_tokens: float = Field(default=0.0, ge=0.0)
    long_context_output_price_per_million_tokens: float = Field(default=0.0, ge=0.0)
    long_context_cache_hit_price_per_million_tokens: float = Field(default=0.0, ge=0.0)


class AgentRuntimeSettings(BaseModel):
    """Agent 核心循环的默认护栏配置，具体任务可在发起 Run 时覆盖。"""

    max_steps: int = Field(default=200)
    """单次 Run 的基础工具步数预算；动态页面模式可扩展，但不得超过 max_steps_hard_cap。"""

    max_iterations: int = Field(default=200)
    """写入 Agent Framework function_invocation_configuration 的最大大模型
    往返轮次（框架尽力而为防护，非硬上限）。"""

    max_duration_seconds: float = Field(default=3600.0)
    """单次 Run 允许的最长墙钟时间（秒），由编排层 asyncio.wait_for 强制中断。"""

    stuck_window: int = Field(default=3)
    """判定"卡死/无进展"所需的连续相同调用（工具名+参数完全一致）次数。"""

    retry_max_attempts: int = Field(default=3)
    """单次工具调用尝试允许的最大总次数（含首次尝试），由 RetryMiddleware 强制执行。"""

    retry_backoff_seconds: float = Field(default=0.5)
    """RetryMiddleware 相邻两次重试之间的固定退避等待时间（秒）。"""

    max_continuation_attempts: int = Field(default=3, ge=0)
    """模型提前返回但页面工作未收敛时，编排层允许追加的续跑次数。"""

    max_consecutive_infrastructure_failures: int = Field(default=3, ge=1)
    """连续页面/规则基础设施故障达到该阈值后，停止无效探索并按全局故障收口。"""

    max_steps_hard_cap: int = Field(default=600, ge=1)
    """任务级动态步数预算的系统硬上限。"""

    page_budget_base_steps: int = Field(default=12, ge=1)
    """按页面策略或运行时已发现工作推导工具步数时的固定基础预算。"""

    page_budget_steps_per_page: int = Field(default=16, ge=1)
    """每个页面上限或已建立质检上下文的逻辑页面追加的预算。"""

    adaptive_page_budget: bool = Field(default=True)
    """页面总量未知时，是否按运行中已形成首采的逻辑页面动态扩展步数预算。"""


class QueueSettings(BaseModel):
    """Redis 队列相关配置：Agent Run 任务的入队/出队/状态存储载体（不落库，带 TTL 自然过期）。"""

    redis_url: str = Field(default="redis://localhost:6379/0")
    agent_run_queue_key: str = Field(default="alioth:agent-runs:queue", min_length=1)
    web_agent_run_queue_key: str = Field(default="alioth:agent-runs:web-v2", min_length=1)
    run_record_ttl_seconds: int = Field(default=86400)
    """Run 记录在 Redis 中的存活时间（秒），默认 24 小时；仅用于"提交后一段时间内可查询
    结果"，不是长期审计存储（长期审计留痕见后续接入数据库后的 qc_agent_run 等表）。"""


class WorkerSettings(BaseModel):
    """后台 Worker 进程（services/api/app/worker.py）配置。"""

    concurrency: int = Field(default=3, ge=1, le=32)
    """单个 Worker 进程内并发消费任务的协程数量。"""

    poll_timeout_seconds: float = Field(default=5.0)
    """每次向 Redis 队列阻塞式取任务的超时时间（秒）；超时后重新尝试，
    使 Worker 有机会响应停止信号，而不是永久阻塞在一次取任务操作上。"""

    web_concurrency: int = Field(default=1, ge=1, le=32)
    """Web V2 专属等待队列的进程内并发数，首期固定默认为 1。"""

    max_active_runs: int = Field(default=4, ge=1, le=32)
    """同一进程内 App/H5/Web 合计的运行许可数，不能先取消息再等待许可。"""

    finalization_stall_timeout_seconds: float = Field(default=600.0, ge=30, le=86400)
    """Main 已终态且全部工作已停止后，等待异常收口的最长宽限。"""

    roles: frozenset[Literal["explorer", "web_explorer", "judge"]] = Field(
        default_factory=lambda: frozenset({"explorer", "judge"})
    )
    """进程内角色：explorer 消费原有探索/App Runtime；web_explorer 只消费 Web V2；
    judge 消费 Inspection Work Item。通过 ALIOTH_WORKER_ROLES 配置独立扩缩容。"""

    app_run_lease_seconds: int = Field(default=180, ge=30, le=3600)
    """App Agent Run 的 PostgreSQL 执行租约；覆盖 Bootstrap、探索与 Closure 全段。"""

    app_run_recovery_interval_seconds: float = Field(default=5.0, ge=0.5, le=300.0)
    """Redis 唤醒丢失时扫描 PostgreSQL 可恢复 App Run 的低频间隔。"""

    device_lease_recovery_interval_seconds: float = Field(default=5.0, ge=0.5, le=300.0)
    """扫描过期 App 设备租约并回收终态占用的间隔。"""

    device_lease_recovery_grace_seconds: int = Field(default=60, ge=1, le=3600)
    """recovering 且 Session 仍非终态时，超过该宽限期才隔离，避免误杀仍在续租的短抖动。"""


class EvidenceStorageSettings(BaseModel):
    """页面采集证据存储配置。"""

    backend: Literal["local", "minio"] = Field(default="local")
    local_root: str = Field(default=".alioth/evidence")
    minio_endpoint: str | None = Field(default=None)
    minio_access_key: str | None = Field(default=None)
    minio_secret_key: str | None = Field(default=None)
    minio_bucket: str = Field(default="alioth")
    minio_secure: bool = Field(default=False)


class EvidenceManifestReadSettings(BaseModel):
    """E24：判定侧按清单读取证据的灰度开关。

    `mode` 三档：off＝旧读取；shadow＝旧读取权威并双读对账；on＝清单读取为主路径。
    请求 on 但缺少零差异对账凭证时由决议器降级为 shadow（见
    `application/manifest_read_cutover.py`）；回滚即改回 shadow／off。
    `cutover_at` 是按采集时间分流的切换时点：早于该时点的历史采集直接走旧读取。
    """

    mode: Literal["off", "shadow", "on"] = Field(default="off")
    cutover_at: datetime | None = Field(default=None)
    minimum_compared_captures: int = Field(default=1, ge=1)


class WebEvidenceExitGateSettings(BaseModel):
    """E16：Web 生产路径离场门禁的灰度开关。

    - `off`：沿用空门禁（改造前行为）；
    - `shadow`：真实评估并持久化门禁事实，但阻断结论不生效，只留审计；
    - `on`：证据不全时真实拒绝离场动作。

    切到 `on` 前必须确认需求集发布与清单裁决已在采集流程中产出事实
    （见 `application/capture_evidence_readiness.py`），否则门禁会因
    「无 pass 清单／无需求集」拒绝全部离场动作。
    """

    mode: Literal["off", "shadow", "on"] = Field(default="off")
    reservation_seconds: int = Field(default=120, ge=15, le=3600)
    expire_sweep_interval_seconds: float = Field(default=60.0, ge=1.0, le=3600.0)
    expire_sweep_limit: int = Field(default=100, ge=1, le=1000)


class EvidenceLifecycleSettings(BaseModel):
    """证据对象保留／归档／GC 策略；默认关闭物理删除。"""

    policy_version: str = Field(default="evidence-lifecycle.v1")
    retention_days: int = Field(default=90, ge=1, le=3650)
    gc_enabled: bool = Field(default=False)
    batch_limit: int = Field(default=100, ge=1, le=10_000)
    sweep_interval_seconds: float = Field(default=300.0, ge=1.0, le=86_400.0)


class QualityOptimizationSettings(BaseModel):
    """质检优化流水线的灰度、载荷与并发配置。"""

    pipeline_mode: Literal["off", "shadow", "on"] = Field(default="off")
    verdict_mode: Literal["strict", "balanced"] = Field(default="strict")
    shadow_sample_rate: float = Field(default=0.10, ge=0.0, le=1.0)
    prompt_input_token_budget: int = Field(default=80_000, ge=1_000)
    semantic_batch_size: int = Field(default=4, ge=1, le=4)
    semantic_concurrency: int = Field(default=2, ge=1, le=2)
    semantic_batch_timeout_seconds: float = Field(default=90.0, ge=5.0, le=300.0)
    page_detection_timeout_seconds: float = Field(default=90.0, ge=5.0, le=300.0)
    page_capture_timeout_seconds: float = Field(default=300.0, ge=30.0, le=600.0)
    web_interaction_timeout_seconds: float = Field(
        default=DEFAULT_WEB_INTERACTION_TIMEOUT_SECONDS,
        ge=5.0,
        le=300.0,
    )
    page_capture_concurrency: int = Field(default=1, ge=1, le=4)
    semantic_request_timeout_seconds: float = Field(default=60.0, gt=0.0, le=180.0)
    """单次语义规则判定的总墙钟预算，包含结构化结果修正与批次降级。"""

    semantic_max_retries: int = Field(default=0, ge=0, le=2)
    """语义 Judge 底层 SDK 重试次数；默认快速失败，由任务级熔断避免重复长等待。"""

    semantic_circuit_seconds: float = Field(default=120.0, gt=0.0, le=3600.0)
    """同一任务确认语义线路故障后的短时熔断窗口。"""

    scroll_max_segments: int = Field(default=8, ge=1, le=8)


class InspectionWorkItemSettings(BaseModel):
    """页面质检持久化工作项控制面配置；工作项门禁与唤醒消费始终启用。"""

    lease_seconds: int = Field(default=180, ge=30, le=3600)
    max_attempts: int = Field(default=3, ge=1, le=10)
    retry_base_seconds: float = Field(default=2.0, ge=0.1, le=300.0)
    judge_concurrency: int = Field(default=2, ge=1, le=32)
    """独立 Judge worker 并发数；默认保守限制模型与数据库负载。"""

    dispatch_batch_limit: int = Field(default=1, ge=1, le=100)
    """单个 task 每轮最多领取的工作项数，避免高积压任务垄断 worker。"""

    publication_batch_limit: int = Field(default=1, ge=1, le=100)
    """单个 task 每轮最多尝试发布的 Capture 数，与 Judge claim 批次独立。"""

    publication_timeout_seconds: float = Field(default=30.0, ge=0.1, le=300.0)
    """单轮 Capture Publication 墙钟预算，不复用 Judge attempt timeout。"""

    dispatch_queue_capacity: int = Field(default=128, ge=1, le=10_000)
    """进程内 task/run 唤醒身份队列上限；PostgreSQL 仍是工作项事实源。"""


class SupplementalEvidenceSettings(BaseModel):
    """服务端补采/补证能力的控制面配置，默认关闭。"""

    enabled: bool = Field(default=False)


class RuleResultReuseSettings(BaseModel):
    """规则结论复用（规则声明 reuse_policy）开关；关闭即恢复每页各自判定。"""

    enabled: bool = Field(default=True)


class WorkflowSettings(BaseModel):
    """Workflow 版本和 App Runtime 注册点配置。"""

    version: Literal["v1", "v2"] = Field(default="v1")
    h5_v2_mode: Literal["off", "shadow", "on"] = Field(default="off")
    web_v2_mode: Literal["off", "exploration_only", "on"] = Field(default="off")
    h5_inspection_notification_mode: Literal["off", "shadow", "enforced"] = Field(default="off")
    h5_visual_coordinate_mode: Literal["off", "shadow", "enforced"] = Field(default="off")
    h5_logical_surface_enabled: bool = Field(default=False)
    h5_logical_surface_shadow: bool = Field(default=False)
    h5_logical_targets_enabled: bool = Field(default=False)
    h5_browser_proxy_server: str | None = Field(default=None)
    h5_browser_proxy_bypass: str | None = Field(default=None)
    web_browser_proxy_server: str | None = Field(default=None)
    web_browser_proxy_bypass: str | None = Field(default=None)
    h5_cli_mode: Literal["off", "observe", "actions"] = Field(default="off")
    h5_cli_script: str = Field(
        default="infra/playwright-cli/node_modules/@playwright/cli/playwright-cli.js"
    )
    h5_cli_workspace_root: str = Field(default="/tmp/alioth-playwright-cli")
    app_qc_enabled: bool = Field(default=False)
    app_runtime_provider: Literal["fake", "official"] = Field(default="fake")
    app_vision_provider: Literal["auto", "fake", "llm"] = Field(default="auto")
    app_device_lease_backend: Literal["in_process", "postgres"] = Field(default="in_process")
    mobile_demo_enabled: bool = Field(default=False)
    mobile_demo_smoke_enabled: bool = Field(default=False)
    official_app_base_url: str | None = Field(default=None)
    official_app_timeout_seconds: float = Field(default=60.0, gt=0)
    # 云手机大 APK 远端 push／pm install 常超过通用 60～120s；Install 单独放宽。
    official_app_install_timeout_seconds: float = Field(default=600.0, gt=0)
    # 云手机 remote pull + 帧内刷新重试 + 前台身份最多 3 次采集；Capture 必须覆盖整段墙钟。
    official_app_capture_timeout_seconds: float = Field(default=780.0, gt=0)
    official_app_max_screenshot_bytes: int = Field(default=20 * 1024 * 1024, gt=0)
    official_app_auth_header_name: str = Field(default="Authorization")
    official_app_auth_token: str | None = Field(default=None)
    official_app_start_session_path: str = Field(default="/v1/sessions")
    official_app_reattach_path: str = Field(default="/v1/sessions/reattach")
    official_app_install_path: str = Field(default="/v1/apps/install")
    official_app_open_path: str = Field(default="/v1/apps/open")
    official_app_capture_path: str = Field(default="/v1/apps/capture")
    official_app_capture_ui_tree_path: str = Field(default="/v1/apps/capture-ui-tree")
    official_app_capture_aux_path: str = Field(default="/v1/apps/capture-aux")
    official_app_execute_path: str = Field(default="/v1/apps/execute")
    official_app_close_path: str = Field(default="/v1/sessions/close")
    # App 会话网络代理（真 proxy HAR）；未启用时 network_trace 回退 dumpsys
    app_network_proxy_enabled: bool = Field(default=False)
    app_network_proxy_listen_host: str = Field(default="0.0.0.0")
    app_network_proxy_listen_port: int = Field(default=18888, gt=0, le=65535)
    app_network_proxy_advertise_host: str = Field(default="10.0.2.2")
    app_network_proxy_device_host: str | None = Field(default=None)
    app_network_proxy_mitm_enabled: bool = Field(default=False)
    app_network_proxy_mitm_ca_dir: str | None = Field(default=None)

    @property
    def h5_logical_surface_effective(self) -> bool:
        """逻辑页面证据可影响 Frontier、覆盖和规则的判定模式。"""
        return self.h5_logical_surface_enabled and not self.h5_logical_surface_shadow

    @property
    def h5_logical_targets_effective(self) -> bool:
        """统一逻辑目标只允许在非 Shadow 的判定模式启用。"""
        return self.h5_logical_surface_effective and self.h5_logical_targets_enabled

    @property
    def h5_visual_coordinate_runtime_mode(self) -> Literal["off", "shadow", "enforced"]:
        """视觉坐标能力必须跟随逻辑页面和逻辑目标开关共同降级。"""
        if self.h5_visual_coordinate_mode == "off" or not self.h5_logical_surface_enabled:
            return "off"
        if (
            self.h5_visual_coordinate_mode == "shadow"
            or self.h5_logical_surface_shadow
            or not self.h5_logical_targets_enabled
        ):
            return "shadow"
        return "enforced"

    def validate_runtime_environment(self, environment: str) -> None:
        """启用 App 时禁止生产环境意外使用默认 Fake Provider。"""
        if not isinstance(environment, str) or not environment.strip():
            raise ValueError("environment 不能为空")
        if (
            self.app_qc_enabled
            and self.app_runtime_provider == "fake"
            and environment.strip().casefold() in {"prod", "production"}
        ):
            raise ValueError("production 环境禁止启用 Fake App Runtime Provider")
        if (
            self.app_qc_enabled
            and self.app_device_lease_backend != "postgres"
            and environment.strip().casefold() in {"prod", "production"}
        ):
            raise ValueError("production 环境必须使用 PostgreSQL App 设备租约")
        if self.mobile_demo_enabled and environment.strip().casefold() in {"prod", "production"}:
            raise ValueError("production 环境禁止匿名启用 mobile-demo")

    def validate_official_app_configuration(self) -> None:
        """校验 Official Adapter 的部署配置，避免静默降级到其他 Runtime。"""
        if not isinstance(self.official_app_base_url, str) or not self.official_app_base_url:
            raise ValueError("Official App 缺少 base URL 配置")
        if (
            not isinstance(self.official_app_auth_header_name, str)
            or not self.official_app_auth_header_name.strip()
        ):
            raise ValueError("Official App 缺少认证请求头配置")
        if not isinstance(self.official_app_auth_token, str) or not self.official_app_auth_token:
            raise ValueError("Official App 缺少认证配置")
        for field_name in (
            "official_app_start_session_path",
            "official_app_reattach_path",
            "official_app_install_path",
            "official_app_open_path",
            "official_app_capture_path",
            "official_app_capture_ui_tree_path",
            "official_app_capture_aux_path",
            "official_app_execute_path",
            "official_app_close_path",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.startswith("/"):
                raise ValueError(f"{field_name} 必须是以 / 开头的路径")


class PlatformBuildSettings(BaseModel):
    """APK 构建制品存储和上传限制。"""

    local_root: str = Field(default=".alioth/platform-builds")
    max_size_bytes: int = Field(default=512 * 1024 * 1024, gt=0)


class WebAdultSelfAttestationSettings(BaseModel):
    """跨账号、跨会话和跨站点复用的 Web 成年自我声明配置。"""

    enabled: bool = False
    statement_version: Literal["adult-v1"] = "adult-v1"
    declared_by: str | None = None
    declared_at: datetime | None = None


class Settings(BaseModel):
    app_name: str = Field(default="Alioth API")
    environment: str = Field(default="local")
    api_v1_prefix: str = Field(default="/api/v1")
    api_v2_prefix: str = Field(default="/api/v2")
    debug: bool = Field(default=False)
    cors_allowed_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"]
    )
    llm: LLMSettings = Field(default_factory=LLMSettings)
    agent_runtime: AgentRuntimeSettings = Field(default_factory=AgentRuntimeSettings)
    queue: QueueSettings = Field(default_factory=QueueSettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    evidence_storage: EvidenceStorageSettings = Field(default_factory=EvidenceStorageSettings)
    evidence_manifest_read: EvidenceManifestReadSettings = Field(
        default_factory=EvidenceManifestReadSettings
    )
    web_evidence_exit_gate: WebEvidenceExitGateSettings = Field(
        default_factory=WebEvidenceExitGateSettings
    )
    evidence_lifecycle: EvidenceLifecycleSettings = Field(default_factory=EvidenceLifecycleSettings)
    quality_optimization: QualityOptimizationSettings = Field(
        default_factory=QualityOptimizationSettings
    )
    inspection_work_items: InspectionWorkItemSettings = Field(
        default_factory=InspectionWorkItemSettings
    )
    supplemental_evidence: SupplementalEvidenceSettings = Field(
        default_factory=SupplementalEvidenceSettings
    )
    rule_result_reuse: RuleResultReuseSettings = Field(default_factory=RuleResultReuseSettings)
    workflow: WorkflowSettings = Field(default_factory=WorkflowSettings)
    h5_auth: H5AuthSettings = Field(default_factory=H5AuthSettings)
    web_adult_self_attestation: WebAdultSelfAttestationSettings = Field(
        default_factory=WebAdultSelfAttestationSettings
    )
    platform_builds: PlatformBuildSettings = Field(default_factory=PlatformBuildSettings)


def _read_bool(name: str, default: bool, *, warn_on_invalid: bool = False) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    if warn_on_invalid:
        logger.warning(
            "环境变量 %s=%r 不是合法布尔值，补采能力按关闭处理；"
            "允许值为 1/0、true/false、yes/no、on/off。",
            name,
            raw_value,
        )
    return False


def _read_csv(name: str, default: list[str]) -> list[str]:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    values = [value.strip() for value in raw_value.split(",") if value.strip()]
    return values or default


def _read_int(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None or raw_value.strip() == "":
        return default
    try:
        return int(raw_value)
    except ValueError:
        return default


def _read_float(name: str, default: float) -> float:
    raw_value = os.getenv(name)
    if raw_value is None or raw_value.strip() == "":
        return default
    try:
        return float(raw_value)
    except ValueError:
        return default


def _read_positive_float(name: str, default: float) -> float:
    raw_value = os.getenv(name)
    if raw_value is None or raw_value.strip() == "":
        return default
    try:
        value = float(raw_value)
    except ValueError as error:
        raise ValueError(f"{name} 必须为正数") from error
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} 必须大于 0")
    return value


def _read_positive_int(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None or raw_value.strip() == "":
        return default
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ValueError(f"{name} 必须为正整数") from error
    if value <= 0:
        raise ValueError(f"{name} 必须大于 0")
    return value


def _read_optional_positive_int(name: str) -> int | None:
    raw_value = os.getenv(name)
    if raw_value is None or raw_value.strip() == "":
        return None
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ValueError(f"{name} 必须为正整数") from error
    if value <= 0:
        raise ValueError(f"{name} 必须大于 0")
    return value


def _read_h5_browser_proxy_server() -> str | None:
    """读取显式 H5 浏览器代理；默认直连且拒绝把凭据写入代理 URL。"""
    return _read_browser_proxy_server("ALIOTH_H5_BROWSER_PROXY_SERVER")


def _read_web_browser_proxy_server() -> str | None:
    """读取显式 Web 浏览器代理；与 H5 配置保持物理隔离。"""
    return _read_browser_proxy_server(
        "ALIOTH_WEB_BROWSER_PROXY_SERVER",
        strict_authority=True,
    )


def _read_browser_proxy_server(
    environment_name: str,
    *,
    strict_authority: bool = False,
) -> str | None:
    """读取无凭据、无路径的浏览器代理地址。"""
    raw_value = os.getenv(environment_name)
    if raw_value is None or not raw_value.strip():
        return None
    value = raw_value.strip()
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"{environment_name} 不是合法代理地址") from error
    if (
        parsed.scheme.casefold() not in {"http", "https", "socks5"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or port is not None
        and not 1 <= port <= 65535
    ):
        raise ValueError(
            f"{environment_name} 必须是无凭据、无路径的 http/https/socks5 地址"
        )
    if strict_authority and not _is_safe_browser_proxy_authority(value, parsed.hostname):
        raise ValueError(f"{environment_name} 包含不安全的主机或端口")
    return value[:-1] if value.endswith("/") else value


def _is_safe_browser_proxy_authority(value: str, hostname: str) -> bool:
    """拒绝 urllib 与浏览器可能产生不同解释的代理 authority。"""
    if "\\" in value or any(character.isspace() or ord(character) < 32 for character in value):
        return False
    netloc = urlsplit(value).netloc
    if not netloc or netloc.endswith(":"):
        return False
    bracketed_authority = netloc.startswith("[")
    try:
        parsed_address = ip_address(hostname)
        if "%" in hostname:
            return False
        return parsed_address.version == 6 if bracketed_authority else True
    except ValueError:
        if bracketed_authority:
            return False
        try:
            ascii_hostname = hostname.encode("idna").decode("ascii")
        except UnicodeError:
            return False
        if len(ascii_hostname) > 253:
            return False
        labels = ascii_hostname.split(".")
        lowercase_last_label = labels[-1].casefold()
        if lowercase_last_label.isdigit() or (
            lowercase_last_label.startswith("0x")
            and len(lowercase_last_label) > 2
            and all(
                character in "0123456789abcdef"
                for character in lowercase_last_label[2:]
            )
        ):
            return False
        return all(
            label
            and len(label) <= 63
            and label[0] != "-"
            and label[-1] != "-"
            and all(character.isalnum() or character == "-" for character in label)
            for label in labels
        )


def _read_h5_browser_proxy_bypass() -> str | None:
    """按 Playwright 逗号分隔合同规范化 H5 代理旁路列表。"""
    return _read_browser_proxy_bypass("ALIOTH_H5_BROWSER_PROXY_BYPASS")


def _read_web_browser_proxy_bypass() -> str | None:
    """按 Playwright 逗号分隔合同规范化 Web 代理旁路列表。"""
    return _read_browser_proxy_bypass("ALIOTH_WEB_BROWSER_PROXY_BYPASS")


def _read_browser_proxy_bypass(environment_name: str) -> str | None:
    """规范化浏览器代理旁路列表并保持首次出现顺序。"""
    raw_value = os.getenv(environment_name)
    if raw_value is None:
        return None
    values = [item.strip() for item in raw_value.split(",") if item.strip()]
    return ",".join(dict.fromkeys(values)) or None


def _build_llm_settings() -> LLMSettings:
    return LLMSettings(
        provider=os.getenv("ALIOTH_LLM_PROVIDER", "openai"),
        api_style=os.getenv("ALIOTH_LLM_API_STYLE", "responses"),
        model=os.getenv("ALIOTH_LLM_MODEL", "gpt-4o-mini"),
        api_key=os.getenv("ALIOTH_LLM_API_KEY") or None,
        base_url=os.getenv("ALIOTH_LLM_BASE_URL") or None,
        reasoning_effort=(os.getenv("ALIOTH_LLM_REASONING_EFFORT", "").strip().lower() or None),
        azure_endpoint=os.getenv("ALIOTH_LLM_AZURE_ENDPOINT") or None,
        azure_api_version=os.getenv("ALIOTH_LLM_AZURE_API_VERSION") or None,
        timeout_seconds=_read_float("ALIOTH_LLM_TIMEOUT_SECONDS", 60.0),
        max_retries=_read_int("ALIOTH_LLM_MAX_RETRIES", 2),
        cost_currency=os.getenv("ALIOTH_LLM_COST_CURRENCY", "CNY"),
        input_price_per_million_tokens=_read_float(
            "ALIOTH_LLM_INPUT_PRICE_PER_MILLION_TOKENS",
            0.0,
        ),
        output_price_per_million_tokens=_read_float(
            "ALIOTH_LLM_OUTPUT_PRICE_PER_MILLION_TOKENS",
            0.0,
        ),
        cache_hit_price_per_million_tokens=_read_float(
            "ALIOTH_LLM_CACHE_HIT_PRICE_PER_MILLION_TOKENS",
            0.0,
        ),
        long_context_threshold_tokens=_read_optional_positive_int(
            "ALIOTH_LLM_LONG_CONTEXT_THRESHOLD_TOKENS",
        ),
        long_context_input_price_per_million_tokens=_read_float(
            "ALIOTH_LLM_LONG_CONTEXT_INPUT_PRICE_PER_MILLION_TOKENS",
            0.0,
        ),
        long_context_output_price_per_million_tokens=_read_float(
            "ALIOTH_LLM_LONG_CONTEXT_OUTPUT_PRICE_PER_MILLION_TOKENS",
            0.0,
        ),
        long_context_cache_hit_price_per_million_tokens=_read_float(
            "ALIOTH_LLM_LONG_CONTEXT_CACHE_HIT_PRICE_PER_MILLION_TOKENS",
            0.0,
        ),
    )


def _build_agent_runtime_settings() -> AgentRuntimeSettings:
    return AgentRuntimeSettings(
        max_steps=_read_int("ALIOTH_AGENT_MAX_STEPS", 200),
        max_iterations=_read_int("ALIOTH_AGENT_MAX_ITERATIONS", 200),
        max_duration_seconds=_read_float("ALIOTH_AGENT_MAX_DURATION_SECONDS", 3600.0),
        stuck_window=_read_int("ALIOTH_AGENT_STUCK_WINDOW", 3),
        retry_max_attempts=_read_int("ALIOTH_AGENT_RETRY_MAX_ATTEMPTS", 3),
        retry_backoff_seconds=_read_float("ALIOTH_AGENT_RETRY_BACKOFF_SECONDS", 0.5),
        max_continuation_attempts=_read_int("ALIOTH_AGENT_MAX_CONTINUATION_ATTEMPTS", 3),
        max_consecutive_infrastructure_failures=_read_int(
            "ALIOTH_AGENT_MAX_CONSECUTIVE_INFRA_FAILURES", 3
        ),
        max_steps_hard_cap=_read_int("ALIOTH_AGENT_MAX_STEPS_HARD_CAP", 600),
        page_budget_base_steps=_read_int("ALIOTH_AGENT_PAGE_BUDGET_BASE_STEPS", 12),
        page_budget_steps_per_page=_read_int("ALIOTH_AGENT_PAGE_BUDGET_STEPS_PER_PAGE", 16),
        adaptive_page_budget=_read_bool("ALIOTH_AGENT_ADAPTIVE_PAGE_BUDGET", True),
    )


def _build_queue_settings() -> QueueSettings:
    return QueueSettings(
        redis_url=os.getenv("ALIOTH_REDIS_URL", "redis://localhost:6379/0"),
        agent_run_queue_key=(
            os.getenv("ALIOTH_AGENT_RUN_QUEUE_KEY", "alioth:agent-runs:queue").strip()
            or "alioth:agent-runs:queue"
        ),
        web_agent_run_queue_key=(
            os.getenv("ALIOTH_WEB_AGENT_RUN_QUEUE_KEY", "alioth:agent-runs:web-v2").strip()
            or "alioth:agent-runs:web-v2"
        ),
        run_record_ttl_seconds=_read_int("ALIOTH_QUEUE_RUN_TTL_SECONDS", 86400),
    )


def _build_worker_settings() -> WorkerSettings:
    raw_roles = {
        item.strip().lower()
        for item in _read_csv("ALIOTH_WORKER_ROLES", ["explorer", "judge"])
        if item.strip()
    }
    roles = frozenset(
        role for role in raw_roles if role in {"explorer", "web_explorer", "judge"}
    ) or frozenset({"explorer", "judge"})
    return WorkerSettings(
        concurrency=_read_int("ALIOTH_WORKER_CONCURRENCY", 3),
        web_concurrency=_read_int("ALIOTH_WEB_WORKER_CONCURRENCY", 1),
        max_active_runs=_read_int("ALIOTH_WORKER_MAX_ACTIVE_RUNS", 4),
        finalization_stall_timeout_seconds=_read_float(
            "ALIOTH_FINALIZATION_STALL_TIMEOUT_SECONDS", 600.0
        ),
        poll_timeout_seconds=_read_float("ALIOTH_WORKER_POLL_TIMEOUT_SECONDS", 5.0),
        roles=roles,  # type: ignore[arg-type]
        app_run_lease_seconds=_read_int("ALIOTH_APP_RUN_LEASE_SECONDS", 180),
        app_run_recovery_interval_seconds=_read_float(
            "ALIOTH_APP_RUN_RECOVERY_INTERVAL_SECONDS",
            5.0,
        ),
        device_lease_recovery_interval_seconds=_read_float(
            "ALIOTH_DEVICE_LEASE_RECOVERY_INTERVAL_SECONDS",
            5.0,
        ),
        device_lease_recovery_grace_seconds=_read_int(
            "ALIOTH_DEVICE_LEASE_RECOVERY_GRACE_SECONDS",
            60,
        ),
    )


def _build_evidence_storage_settings() -> EvidenceStorageSettings:
    return EvidenceStorageSettings(
        backend=os.getenv("ALIOTH_EVIDENCE_STORAGE_BACKEND", "local"),
        local_root=os.getenv("ALIOTH_EVIDENCE_STORAGE_ROOT", ".alioth/evidence"),
        minio_endpoint=os.getenv("ALIOTH_MINIO_ENDPOINT") or None,
        minio_access_key=os.getenv("ALIOTH_MINIO_ACCESS_KEY") or None,
        minio_secret_key=os.getenv("ALIOTH_MINIO_SECRET_KEY") or None,
        minio_bucket=os.getenv("ALIOTH_MINIO_BUCKET", "alioth"),
        minio_secure=_read_bool("ALIOTH_MINIO_SECURE", False),
    )


def _build_evidence_manifest_read_settings() -> EvidenceManifestReadSettings:
    """读取清单读取灰度配置；非法模式与非法时间一律安全回退。"""
    raw_mode = os.getenv("ALIOTH_EVIDENCE_MANIFEST_READ_MODE", "off").strip().lower()
    mode: Literal["off", "shadow", "on"] = (
        raw_mode if raw_mode in {"off", "shadow", "on"} else "off"
    )
    raw_cutover = (os.getenv("ALIOTH_EVIDENCE_MANIFEST_CUTOVER_AT") or "").strip()
    cutover_at: datetime | None = None
    if raw_cutover:
        try:
            parsed = datetime.fromisoformat(raw_cutover)
        except ValueError:
            logger.warning(
                "ALIOTH_EVIDENCE_MANIFEST_CUTOVER_AT 不是合法 ISO 时间，已忽略：%s",
                raw_cutover,
            )
        else:
            # 无时区的配置按 UTC 解释，避免宿主时区影响分流边界
            cutover_at = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return EvidenceManifestReadSettings(
        mode=mode,
        cutover_at=cutover_at,
        minimum_compared_captures=max(
            _read_int("ALIOTH_EVIDENCE_MANIFEST_MIN_COMPARED_CAPTURES", 1), 1
        ),
    )


def _build_web_evidence_exit_gate_settings() -> WebEvidenceExitGateSettings:
    """读取 Web 离场门禁灰度配置；非法值一律安全回退到关闭。"""
    raw_mode = os.getenv("ALIOTH_WEB_EVIDENCE_EXIT_GATE_MODE", "off").strip().lower()
    mode: Literal["off", "shadow", "on"] = (
        raw_mode if raw_mode in {"off", "shadow", "on"} else "off"
    )
    return WebEvidenceExitGateSettings(
        mode=mode,
        reservation_seconds=max(_read_int("ALIOTH_WEB_EVIDENCE_EXIT_RESERVATION_SECONDS", 120), 15),
        expire_sweep_interval_seconds=max(
            _read_float("ALIOTH_WEB_EVIDENCE_EXIT_SWEEP_INTERVAL_SECONDS", 60.0), 1.0
        ),
        expire_sweep_limit=max(_read_int("ALIOTH_WEB_EVIDENCE_EXIT_SWEEP_LIMIT", 100), 1),
    )


def _build_evidence_lifecycle_settings() -> EvidenceLifecycleSettings:
    """读取证据保留／GC 配置；缺省关闭物理删除。"""
    return EvidenceLifecycleSettings(
        policy_version=(
            os.getenv("ALIOTH_EVIDENCE_LIFECYCLE_POLICY_VERSION", "evidence-lifecycle.v1").strip()
            or "evidence-lifecycle.v1"
        ),
        retention_days=max(_read_int("ALIOTH_EVIDENCE_RETENTION_DAYS", 90), 1),
        gc_enabled=_read_bool("ALIOTH_EVIDENCE_GC_ENABLED", False, warn_on_invalid=True),
        batch_limit=max(_read_int("ALIOTH_EVIDENCE_GC_BATCH_LIMIT", 100), 1),
        sweep_interval_seconds=max(
            _read_float("ALIOTH_EVIDENCE_LIFECYCLE_SWEEP_INTERVAL_SECONDS", 300.0),
            1.0,
        ),
    )


def _build_quality_optimization_settings() -> QualityOptimizationSettings:
    """读取质检优化配置；非法模式回退为关闭，避免未授权热切换。"""
    raw_mode = os.getenv("ALIOTH_QC_OPTIMIZED_PIPELINE_MODE", "off").strip().lower()
    pipeline_mode: Literal["off", "shadow", "on"] = (
        raw_mode if raw_mode in {"off", "shadow", "on"} else "off"
    )
    raw_verdict_mode = os.getenv("ALIOTH_QC_VERDICT_MODE", "strict").strip().lower()
    verdict_mode: Literal["strict", "balanced"] = (
        raw_verdict_mode if raw_verdict_mode in {"strict", "balanced"} else "strict"
    )
    return QualityOptimizationSettings(
        pipeline_mode=pipeline_mode,
        verdict_mode=verdict_mode,
        shadow_sample_rate=_read_float("ALIOTH_QC_SHADOW_SAMPLE_RATE", 0.10),
        prompt_input_token_budget=_read_int("ALIOTH_QC_PROMPT_INPUT_TOKEN_BUDGET", 80_000),
        semantic_batch_size=_read_int("ALIOTH_QC_SEMANTIC_BATCH_SIZE", 4),
        semantic_concurrency=_read_int("ALIOTH_QC_SEMANTIC_CONCURRENCY", 2),
        semantic_batch_timeout_seconds=_read_float(
            "ALIOTH_QC_SEMANTIC_BATCH_TIMEOUT_SECONDS", 90.0
        ),
        page_detection_timeout_seconds=_read_float(
            "ALIOTH_QC_PAGE_DETECTION_TIMEOUT_SECONDS", 90.0
        ),
        page_capture_timeout_seconds=_read_float("ALIOTH_QC_PAGE_CAPTURE_TIMEOUT_SECONDS", 300.0),
        web_interaction_timeout_seconds=_read_float(
            "ALIOTH_QC_WEB_INTERACTION_TIMEOUT_SECONDS",
            DEFAULT_WEB_INTERACTION_TIMEOUT_SECONDS,
        ),
        page_capture_concurrency=_read_int("ALIOTH_QC_PAGE_CAPTURE_CONCURRENCY", 1),
        semantic_request_timeout_seconds=_read_float(
            "ALIOTH_QC_SEMANTIC_REQUEST_TIMEOUT_SECONDS", 60.0
        ),
        semantic_max_retries=_read_int("ALIOTH_QC_SEMANTIC_MAX_RETRIES", 0),
        semantic_circuit_seconds=_read_float("ALIOTH_QC_SEMANTIC_CIRCUIT_SECONDS", 120.0),
        scroll_max_segments=_read_int("ALIOTH_QC_SCROLL_MAX_SEGMENTS", 8),
    )


def _build_inspection_work_item_settings() -> InspectionWorkItemSettings:
    """读取持久化工作项租约与重试配置。"""
    return InspectionWorkItemSettings(
        lease_seconds=_read_int("ALIOTH_QC_INSPECTION_LEASE_SECONDS", 180),
        max_attempts=_read_int("ALIOTH_QC_INSPECTION_MAX_ATTEMPTS", 3),
        retry_base_seconds=_read_float(
            "ALIOTH_QC_INSPECTION_RETRY_BASE_SECONDS",
            2.0,
        ),
        judge_concurrency=max(
            _read_int("ALIOTH_QC_INSPECTION_JUDGE_CONCURRENCY", 2),
            1,
        ),
        dispatch_batch_limit=max(
            _read_int("ALIOTH_QC_INSPECTION_DISPATCH_BATCH_LIMIT", 1),
            1,
        ),
        publication_batch_limit=max(
            _read_int("ALIOTH_QC_INSPECTION_PUBLICATION_BATCH_LIMIT", 1),
            1,
        ),
        publication_timeout_seconds=max(
            _read_float("ALIOTH_QC_INSPECTION_PUBLICATION_TIMEOUT_SECONDS", 30.0),
            0.1,
        ),
        dispatch_queue_capacity=max(
            _read_int("ALIOTH_QC_INSPECTION_DISPATCH_QUEUE_CAPACITY", 128),
            1,
        ),
    )


def _build_supplemental_evidence_settings() -> SupplementalEvidenceSettings:
    """读取补采开关；缺省或非法值均安全关闭。"""
    return SupplementalEvidenceSettings(
        enabled=_read_bool(
            "ALIOTH_SUPPLEMENTAL_EVIDENCE_ENABLED",
            False,
            warn_on_invalid=True,
        )
    )


def _build_rule_result_reuse_settings() -> RuleResultReuseSettings:
    """读取规则结论复用开关；默认开启，非法值按开启处理并告警。"""
    return RuleResultReuseSettings(
        enabled=_read_bool(
            "ALIOTH_RULE_RESULT_REUSE_ENABLED",
            True,
            warn_on_invalid=True,
        )
    )


def _build_workflow_settings() -> WorkflowSettings:
    """读取工作流开关；未知值安全回退到关闭状态。"""
    raw_version = os.getenv("ALIOTH_WORKFLOW_VERSION", "v1").strip().lower()
    version: Literal["v1", "v2"] = raw_version if raw_version in {"v1", "v2"} else "v1"
    raw_h5_v2_mode = os.getenv("ALIOTH_H5_V2_MODE", "off").strip().lower()
    h5_v2_mode: Literal["off", "shadow", "on"] = (
        raw_h5_v2_mode if raw_h5_v2_mode in {"off", "shadow", "on"} else "off"
    )
    raw_web_v2_mode = os.getenv("ALIOTH_WEB_V2_MODE", "off").strip().lower()
    web_v2_mode: Literal["off", "exploration_only", "on"] = (
        raw_web_v2_mode
        if raw_web_v2_mode in {"off", "exploration_only", "on"}
        else "off"
    )
    raw_h5_notification_mode = (
        os.getenv("ALIOTH_H5_INSPECTION_NOTIFICATION_MODE", "off").strip().lower()
    )
    h5_inspection_notification_mode: Literal["off", "shadow", "enforced"] = (
        raw_h5_notification_mode
        if raw_h5_notification_mode in {"off", "shadow", "enforced"}
        else "off"
    )
    raw_h5_visual_coordinate_mode = (
        os.getenv(
            "ALIOTH_H5_VISUAL_COORDINATE_MODE",
            "off",
        )
        .strip()
        .casefold()
    )
    if raw_h5_visual_coordinate_mode not in {"off", "shadow", "enforced"}:
        raise ValueError("ALIOTH_H5_VISUAL_COORDINATE_MODE 必须为 off、shadow 或 enforced")
    h5_visual_coordinate_mode: Literal["off", "shadow", "enforced"] = raw_h5_visual_coordinate_mode
    raw_provider = os.getenv("ALIOTH_APP_RUNTIME_PROVIDER", "fake").strip().lower()
    provider: Literal["fake", "official"] = (
        raw_provider if raw_provider in {"fake", "official"} else "fake"
    )
    raw_vision = os.getenv("ALIOTH_APP_VISION_PROVIDER", "auto").strip().lower()
    vision_provider: Literal["auto", "fake", "llm"] = (
        raw_vision if raw_vision in {"auto", "fake", "llm"} else "auto"
    )
    raw_device_lease_backend = (
        os.getenv(
            "ALIOTH_APP_DEVICE_LEASE_BACKEND",
            os.getenv("ALIOTH_DEVICE_POOL_PROVIDER", "in_process"),
        )
        .strip()
        .lower()
    )
    device_lease_backend: Literal["in_process", "postgres"] = (
        "postgres"
        if raw_device_lease_backend in {"postgres", "postgresql", "database"}
        else "in_process"
    )
    official_app_base_url = os.getenv("ALIOTH_OFFICIAL_APP_API_BASE_URL") or os.getenv(
        "ALIOTH_OFFICIAL_APP_BASE_URL"
    )
    if official_app_base_url is not None:
        official_app_base_url = official_app_base_url.strip() or None
    return WorkflowSettings(
        version=version,
        h5_v2_mode=h5_v2_mode,
        web_v2_mode=web_v2_mode,
        h5_inspection_notification_mode=h5_inspection_notification_mode,
        h5_visual_coordinate_mode=h5_visual_coordinate_mode,
        h5_logical_surface_enabled=_read_bool(
            "ALIOTH_H5_LOGICAL_SURFACE_ENABLED",
            False,
        ),
        h5_logical_surface_shadow=_read_bool(
            "ALIOTH_H5_LOGICAL_SURFACE_SHADOW",
            False,
        ),
        h5_logical_targets_enabled=_read_bool(
            "ALIOTH_H5_LOGICAL_TARGETS_ENABLED",
            False,
        ),
        h5_browser_proxy_server=_read_h5_browser_proxy_server(),
        h5_browser_proxy_bypass=_read_h5_browser_proxy_bypass(),
        web_browser_proxy_server=_read_web_browser_proxy_server(),
        web_browser_proxy_bypass=_read_web_browser_proxy_bypass(),
        h5_cli_mode=os.getenv("ALIOTH_H5_CLI_MODE", "off"),
        h5_cli_script=os.getenv(
            "ALIOTH_PLAYWRIGHT_CLI_SCRIPT",
            "infra/playwright-cli/node_modules/@playwright/cli/playwright-cli.js",
        ),
        h5_cli_workspace_root=os.getenv(
            "ALIOTH_PLAYWRIGHT_CLI_WORKSPACE_ROOT", "/tmp/alioth-playwright-cli",
        ),
        app_qc_enabled=_read_bool("ALIOTH_APP_QC_ENABLED", False),
        app_runtime_provider=provider,
        app_vision_provider=vision_provider,
        app_device_lease_backend=device_lease_backend,
        mobile_demo_enabled=_read_bool("ALIOTH_MOBILE_DEMO_ENABLED", False),
        mobile_demo_smoke_enabled=_read_bool(
            "ALIOTH_MOBILE_DEMO_SMOKE_ENABLED",
            False,
        ),
        official_app_base_url=official_app_base_url,
        official_app_timeout_seconds=_read_positive_float(
            "ALIOTH_OFFICIAL_APP_TIMEOUT_SECONDS",
            60.0,
        ),
        official_app_install_timeout_seconds=_read_positive_float(
            "ALIOTH_OFFICIAL_APP_INSTALL_TIMEOUT_SECONDS",
            600.0,
        ),
        official_app_capture_timeout_seconds=_read_positive_float(
            "ALIOTH_OFFICIAL_APP_CAPTURE_TIMEOUT_SECONDS",
            780.0,
        ),
        official_app_max_screenshot_bytes=_read_positive_int(
            "ALIOTH_OFFICIAL_APP_MAX_SCREENSHOT_BYTES",
            20 * 1024 * 1024,
        ),
        official_app_auth_header_name=os.getenv(
            "ALIOTH_OFFICIAL_APP_AUTH_HEADER_NAME",
            "Authorization",
        ).strip(),
        official_app_auth_token=os.getenv("ALIOTH_OFFICIAL_APP_AUTH_TOKEN") or None,
        official_app_start_session_path=os.getenv(
            "ALIOTH_OFFICIAL_APP_START_SESSION_PATH",
            "/v1/sessions",
        ).strip(),
        official_app_reattach_path=os.getenv(
            "ALIOTH_OFFICIAL_APP_REATTACH_PATH",
            "/v1/sessions/reattach",
        ).strip(),
        official_app_install_path=os.getenv(
            "ALIOTH_OFFICIAL_APP_INSTALL_PATH",
            "/v1/apps/install",
        ).strip(),
        official_app_open_path=os.getenv(
            "ALIOTH_OFFICIAL_APP_OPEN_PATH",
            "/v1/apps/open",
        ).strip(),
        official_app_capture_path=os.getenv(
            "ALIOTH_OFFICIAL_APP_CAPTURE_PATH",
            "/v1/apps/capture",
        ).strip(),
        official_app_capture_ui_tree_path=os.getenv(
            "ALIOTH_OFFICIAL_APP_CAPTURE_UI_TREE_PATH",
            "/v1/apps/capture-ui-tree",
        ).strip(),
        official_app_capture_aux_path=os.getenv(
            "ALIOTH_OFFICIAL_APP_CAPTURE_AUX_PATH",
            "/v1/apps/capture-aux",
        ).strip(),
        official_app_execute_path=os.getenv(
            "ALIOTH_OFFICIAL_APP_EXECUTE_PATH",
            "/v1/apps/execute",
        ).strip(),
        official_app_close_path=os.getenv(
            "ALIOTH_OFFICIAL_APP_CLOSE_PATH",
            "/v1/sessions/close",
        ).strip(),
        app_network_proxy_enabled=_read_bool("ALIOTH_APP_NETWORK_PROXY_ENABLED", False),
        app_network_proxy_listen_host=os.getenv(
            "ALIOTH_APP_NETWORK_PROXY_LISTEN_HOST",
            "0.0.0.0",
        ).strip()
        or "0.0.0.0",
        app_network_proxy_listen_port=_read_positive_int(
            "ALIOTH_APP_NETWORK_PROXY_LISTEN_PORT",
            18888,
        ),
        app_network_proxy_advertise_host=os.getenv(
            "ALIOTH_APP_NETWORK_PROXY_ADVERTISE_HOST",
            "10.0.2.2",
        ).strip()
        or "10.0.2.2",
        app_network_proxy_device_host=(os.getenv("ALIOTH_APP_NETWORK_PROXY_DEVICE_HOST") or None),
        app_network_proxy_mitm_enabled=_read_bool(
            "ALIOTH_APP_NETWORK_PROXY_MITM_ENABLED",
            False,
        ),
        app_network_proxy_mitm_ca_dir=(os.getenv("ALIOTH_APP_NETWORK_PROXY_MITM_CA_DIR") or None),
    )


def _build_platform_build_settings() -> PlatformBuildSettings:
    return PlatformBuildSettings(
        local_root=os.getenv(
            "ALIOTH_PLATFORM_BUILD_STORAGE_ROOT",
            ".alioth/platform-builds",
        ),
        max_size_bytes=_read_int(
            "ALIOTH_PLATFORM_BUILD_MAX_SIZE_BYTES",
            512 * 1024 * 1024,
        ),
    )


def _build_h5_auth_settings() -> H5AuthSettings:
    """读取 H5 主动登录开关；开关关闭时不访问配置文件。"""
    raw_path = os.getenv("ALIOTH_H5_AUTH_CONFIG_PATH")
    return H5AuthSettings(
        enabled=_read_bool("ALIOTH_H5_AUTH_ENABLED", False),
        config_path=Path(raw_path) if raw_path and raw_path.strip() else None,
    )


def _build_web_adult_self_attestation_settings() -> WebAdultSelfAttestationSettings:
    """读取配置级成年声明；启用时必须具备固定来源与带时区声明时间。"""

    enabled = _read_bool("ALIOTH_WEB_ADULT_SELF_ATTESTATION_ENABLED", False)
    if not enabled:
        return WebAdultSelfAttestationSettings(enabled=False)
    statement_version = (
        os.getenv("ALIOTH_WEB_ADULT_SELF_ATTESTATION_VERSION", "adult-v1").strip()
    )
    if statement_version != "adult-v1":
        raise ValueError("ALIOTH_WEB_ADULT_SELF_ATTESTATION_VERSION 必须为 adult-v1")
    declared_by = (
        os.getenv("ALIOTH_WEB_ADULT_SELF_ATTESTATION_DECLARED_BY", "").strip()
    )
    if not declared_by:
        raise ValueError("ALIOTH_WEB_ADULT_SELF_ATTESTATION_DECLARED_BY 不能为空")
    raw_declared_at = (
        os.getenv("ALIOTH_WEB_ADULT_SELF_ATTESTATION_DECLARED_AT", "").strip()
    )
    try:
        declared_at = datetime.fromisoformat(raw_declared_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(
            "ALIOTH_WEB_ADULT_SELF_ATTESTATION_DECLARED_AT 必须为 ISO 时间"
        ) from error
    if declared_at.tzinfo is None:
        raise ValueError(
            "ALIOTH_WEB_ADULT_SELF_ATTESTATION_DECLARED_AT 必须包含时区"
        )
    return WebAdultSelfAttestationSettings(
        enabled=True,
        statement_version="adult-v1",
        declared_by=declared_by,
        declared_at=declared_at,
    )


@lru_cache
def get_settings() -> Settings:
    settings = Settings(
        app_name=os.getenv("ALIOTH_APP_NAME", "Alioth API"),
        environment=os.getenv("ALIOTH_ENVIRONMENT", "local"),
        api_v1_prefix=os.getenv("ALIOTH_API_V1_PREFIX", "/api/v1"),
        api_v2_prefix=os.getenv("ALIOTH_API_V2_PREFIX", "/api/v2"),
        debug=_read_bool("ALIOTH_DEBUG", False),
        cors_allowed_origins=_read_csv(
            "ALIOTH_CORS_ALLOWED_ORIGINS",
            ["http://localhost:5173", "http://127.0.0.1:5173"],
        ),
        llm=_build_llm_settings(),
        agent_runtime=_build_agent_runtime_settings(),
        queue=_build_queue_settings(),
        worker=_build_worker_settings(),
        evidence_storage=_build_evidence_storage_settings(),
        evidence_manifest_read=_build_evidence_manifest_read_settings(),
        web_evidence_exit_gate=_build_web_evidence_exit_gate_settings(),
        evidence_lifecycle=_build_evidence_lifecycle_settings(),
        quality_optimization=_build_quality_optimization_settings(),
        inspection_work_items=_build_inspection_work_item_settings(),
        supplemental_evidence=_build_supplemental_evidence_settings(),
        rule_result_reuse=_build_rule_result_reuse_settings(),
        workflow=_build_workflow_settings(),
        h5_auth=_build_h5_auth_settings(),
        web_adult_self_attestation=_build_web_adult_self_attestation_settings(),
        platform_builds=_build_platform_build_settings(),
    )
    settings.workflow.validate_runtime_environment(settings.environment)
    return settings
