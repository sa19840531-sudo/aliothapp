#!/usr/bin/env python3
"""只读检查 App 页面探索部署级验收的外部前置条件。

本脚本不连接数据库、Redis、对象存储、视觉 Provider 或设备，也不注入故障。
``ready_to_run`` 只表示可以开始真实验收，绝不表示 Batch 6 已通过。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.api.app.infrastructure.db.test_database_guard import (  # noqa: E402
    require_isolated_test_database_url,
)

PROTOCOL_VERSION = "app-exploration-acceptance-prerequisites.v1"

_LIMITATIONS = (
    "本报告只核对配置，不执行 PostgreSQL、Redis、MinIO、视觉 Provider、设备或 Worker 故障注入",
    (
        "Official App 合同测试只覆盖单 APK 的 start/install/open/capture/close，"
        "不等于双 APK 多页面探索验收"
    ),
    "Fake、in-memory 与 Mock 测试只能作为仓库证据，不能把部署验收状态升级为通过",
    (
        "ready_to_run 后必须对已完成任务执行 "
        "scripts/verify_app_morelogin_acceptance.py；"
        "platform_tech_passed 不等于 APP 质量通过"
    ),
)


@dataclass(frozen=True, kw_only=True)
class AcceptancePrerequisiteCheck:
    """单项外部前置条件；只暴露变量名，不回显任何配置值。"""

    code: str
    ready: bool
    missing_or_invalid_variables: tuple[str, ...]
    message: str

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "status": "ready" if self.ready else "blocked",
            "missing_or_invalid_variables": list(self.missing_or_invalid_variables),
            "message": self.message,
        }


@dataclass(frozen=True, kw_only=True)
class AcceptancePrerequisiteReport:
    """App 页面探索真实验收的只读配置快照。"""

    checks: tuple[AcceptancePrerequisiteCheck, ...]

    @property
    def ready_to_run(self) -> bool:
        return all(check.ready for check in self.checks)

    def to_dict(self) -> dict[str, object]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "status": "ready_to_run" if self.ready_to_run else "blocked",
            "checks": [check.to_dict() for check in self.checks],
            "limitations": list(_LIMITATIONS),
        }


def build_acceptance_prerequisite_report(
    environment: Mapping[str, str],
) -> AcceptancePrerequisiteReport:
    """从显式环境快照构建报告，不读取或修改任何外部系统。"""

    return AcceptancePrerequisiteReport(
        checks=(
            _runtime_check(environment),
            _postgres_check(environment),
            _redis_check(environment),
            _object_store_check(environment),
            _visual_provider_check(environment),
            _official_app_check(environment),
        )
    )


def _runtime_check(environment: Mapping[str, str]) -> AcceptancePrerequisiteCheck:
    invalid: list[str] = []
    expected = {
        "ALIOTH_WORKFLOW_VERSION": "v2",
        "ALIOTH_APP_QC_ENABLED": "true",
        "ALIOTH_APP_RUNTIME_PROVIDER": "official",
        "ALIOTH_AGENT_RUN_STATE_STORE_BACKEND": "postgres",
        "ALIOTH_APP_DEVICE_LEASE_BACKEND": "postgres",
    }
    for name, expected_value in expected.items():
        if _value(environment, name).casefold() != expected_value:
            invalid.append(name)
    roles = {
        item.strip().casefold()
        for item in _value(environment, "ALIOTH_WORKER_ROLES", "explorer,judge").split(",")
        if item.strip()
    }
    if "explorer" not in roles:
        invalid.append("ALIOTH_WORKER_ROLES")
    if not _valid_device_slots(_value(environment, "ALIOTH_DEVICE_POOL_SLOTS")):
        invalid.append("ALIOTH_DEVICE_POOL_SLOTS")
    return _check(
        code="app_runtime",
        invalid=invalid,
        ready_message=(
            "App Runtime 已显式启用 Official Provider、PostgreSQL 状态源与设备租约"
        ),
        blocked_message="App Runtime 尚未按真实 App 探索模式启用",
    )


def _postgres_check(environment: Mapping[str, str]) -> AcceptancePrerequisiteCheck:
    test_url = _configured_value(environment, "ALIOTH_TEST_DATABASE_URL")
    if test_url is None:
        return _check(
            code="isolated_postgres",
            invalid=["ALIOTH_TEST_DATABASE_URL"],
            ready_message="",
            blocked_message="未配置显式隔离的 PostgreSQL 验收库",
        )
    primary_url = _configured_value(environment, "ALIOTH_DATABASE_URL")
    if primary_url is None:
        return _check(
            code="isolated_postgres",
            invalid=["ALIOTH_DATABASE_URL"],
            ready_message="",
            blocked_message="缺少主库地址，无法证明 PostgreSQL 验收库已隔离",
        )
    try:
        require_isolated_test_database_url(test_url, primary_url)
    except ValueError:
        return _check(
            code="isolated_postgres",
            invalid=["ALIOTH_TEST_DATABASE_URL"],
            ready_message="",
            blocked_message="PostgreSQL 验收库地址无效或与主库相同",
        )
    return _check(
        code="isolated_postgres",
        invalid=[],
        ready_message="已配置与主库隔离的 PostgreSQL 验收地址",
        blocked_message="",
    )


def _redis_check(environment: Mapping[str, str]) -> AcceptancePrerequisiteCheck:
    invalid = _missing(environment, ("ALIOTH_REDIS_URL",))
    return _check(
        code="redis_worker_recovery",
        invalid=invalid,
        ready_message="已配置真实 Redis 地址，可安排丢唤醒与 Worker 重启验收",
        blocked_message="未配置真实 Redis 地址",
    )


def _object_store_check(environment: Mapping[str, str]) -> AcceptancePrerequisiteCheck:
    invalid = _missing(
        environment,
        (
            "ALIOTH_MINIO_ENDPOINT",
            "ALIOTH_MINIO_ACCESS_KEY",
            "ALIOTH_MINIO_SECRET_KEY",
            "ALIOTH_MINIO_BUCKET",
        ),
    )
    if _value(environment, "ALIOTH_EVIDENCE_STORAGE_BACKEND").casefold() != "minio":
        invalid.append("ALIOTH_EVIDENCE_STORAGE_BACKEND")
    return _check(
        code="minio_evidence_store",
        invalid=invalid,
        ready_message="已配置 MinIO，可安排 missing/checksum 故障验收",
        blocked_message="对象存储未按真实 MinIO 验收环境配置",
    )


def _visual_provider_check(environment: Mapping[str, str]) -> AcceptancePrerequisiteCheck:
    invalid = _missing(environment, ("ALIOTH_LLM_API_KEY",))
    if _value(environment, "ALIOTH_APP_VISION_PROVIDER").casefold() != "llm":
        invalid.append("ALIOTH_APP_VISION_PROVIDER")
    provider = _value(environment, "ALIOTH_LLM_PROVIDER", "openai").casefold()
    if provider in {"custom", "xai"} and _configured_value(
        environment,
        "ALIOTH_LLM_BASE_URL",
    ) is None:
        invalid.append("ALIOTH_LLM_BASE_URL")
    if provider == "azure_openai":
        invalid.extend(
            _missing(
                environment,
                ("ALIOTH_LLM_AZURE_ENDPOINT", "ALIOTH_LLM_AZURE_API_VERSION"),
            )
        )
    return _check(
        code="visual_provider",
        invalid=invalid,
        ready_message="已配置真实 App 视觉 Provider，可安排 503 与坏输出验收",
        blocked_message="App 视觉仍未切换到显式 LLM Provider 或缺少认证配置",
    )


def _official_app_check(environment: Mapping[str, str]) -> AcceptancePrerequisiteCheck:
    invalid = _missing(
        environment,
        (
            "ALIOTH_OFFICIAL_APP_API_BASE_URL",
            "ALIOTH_OFFICIAL_APP_AUTH_TOKEN",
            "ALIOTH_OFFICIAL_APP_CONTRACT_PLATFORM_BUILD_REF",
            "ALIOTH_OFFICIAL_APP_CONTRACT_PACKAGE_NAME",
            "ALIOTH_OFFICIAL_APP_CONTRACT_APK_SHA256",
        ),
    )
    if _value(environment, "ALIOTH_OFFICIAL_APP_CONTRACT_TEST_ENABLED").casefold() != "true":
        invalid.append("ALIOTH_OFFICIAL_APP_CONTRACT_TEST_ENABLED")
    if _value(environment, "ALIOTH_XINGYU_OFFICIAL_DRY_RUN").casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        invalid.append("ALIOTH_XINGYU_OFFICIAL_DRY_RUN")
    apk_sha256 = _configured_value(
        environment,
        "ALIOTH_OFFICIAL_APP_CONTRACT_APK_SHA256",
    )
    if apk_sha256 is not None and not _is_sha256_hex(apk_sha256):
        invalid.append("ALIOTH_OFFICIAL_APP_CONTRACT_APK_SHA256")
    return _check(
        code="official_app_device",
        invalid=invalid,
        ready_message="Official App 合同测试已显式启用并具备专用测试 APK 标识",
        blocked_message="Official App 真实合同或专用测试 APK 配置不完整",
    )


def _missing(environment: Mapping[str, str], names: Sequence[str]) -> list[str]:
    return [name for name in names if _configured_value(environment, name) is None]


def _configured_value(environment: Mapping[str, str], name: str) -> str | None:
    value = _value(environment, name)
    lowered = value.casefold()
    if not value or "replace-with" in lowered or (value.startswith("<") and value.endswith(">")):
        return None
    return value


def _is_sha256_hex(value: str) -> bool:
    if len(value) != 64:
        return False
    try:
        return len(bytes.fromhex(value)) == hashlib.sha256().digest_size
    except ValueError:
        return False


def _valid_device_slots(value: str) -> bool:
    slots = tuple(item.strip() for item in value.split(",") if item.strip())
    if not slots:
        return False
    for slot in slots:
        backend, separator, profile = slot.partition(":")
        if not separator or not backend.strip() or not profile.strip():
            return False
    return len(set(slots)) == len(slots)


def _value(environment: Mapping[str, str], name: str, default: str = "") -> str:
    value = environment.get(name, default)
    return value.strip() if isinstance(value, str) else ""


def _check(
    *,
    code: str,
    invalid: Sequence[str],
    ready_message: str,
    blocked_message: str,
) -> AcceptancePrerequisiteCheck:
    unique = tuple(dict.fromkeys(invalid))
    return AcceptancePrerequisiteCheck(
        code=code,
        ready=not unique,
        missing_or_invalid_variables=unique,
        message=ready_message if not unique else blocked_message,
    )


def _print_human(report: AcceptancePrerequisiteReport) -> None:
    status = "READY_TO_RUN" if report.ready_to_run else "BLOCKED"
    print(f"App exploration deployment acceptance prerequisites: {status}")
    for check in report.checks:
        marker = "READY" if check.ready else "BLOCKED"
        variables = ", ".join(check.missing_or_invalid_variables)
        suffix = f" ({variables})" if variables else ""
        print(f"[{marker}] {check.code}: {check.message}{suffix}")
    print("Limitations:")
    for limitation in _LIMITATIONS:
        print(f"- {limitation}")


def main(
    argv: list[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="输出不含配置值的 JSON 报告")
    args = parser.parse_args(argv)
    report = build_acceptance_prerequisite_report(
        os.environ if environment is None else environment
    )
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True))
    else:
        _print_human(report)
    return 0 if report.ready_to_run else 2


if __name__ == "__main__":
    raise SystemExit(main())
