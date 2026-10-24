"""仅供本地隔离验收的 H5 补采 Worker 入口。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from services.api.app.application.supplemental_evidence_policy import (
    SupplementalEvidencePolicy,
    build_lab_supplemental_evidence_policy,
)
from services.api.app.core.config import Settings, get_settings

WorkerMain = Callable[..., Awaitable[None]]


async def run_h5_probe_lab_worker(
    *,
    settings: Settings | None = None,
    worker_main: WorkerMain | None = None,
) -> None:
    """在隔离本地环境中为 H5 Runtime 显式注入补采能力。"""
    resolved = settings or get_settings()
    environment = resolved.environment.strip().casefold()
    if environment in {"production", "prod"}:
        raise RuntimeError("H5 补采 Lab Worker 禁止在生产环境运行")
    roles = set(resolved.worker.roles)
    if "explorer" not in roles or not roles <= {"explorer", "judge"}:
        raise RuntimeError("H5 补采 Lab Worker 只能启用 H5 explorer 和 judge 角色")
    if resolved.workflow.version != "v2" or resolved.workflow.h5_v2_mode != "on":
        raise RuntimeError("H5 补采 Lab Worker 要求 H5 V2 正式执行模式")
    if resolved.workflow.web_v2_mode != "off":
        raise RuntimeError("H5 补采 Lab Worker 不得同时启用桌面 Web")
    if resolved.workflow.app_qc_enabled:
        raise RuntimeError("H5 补采 Lab Worker 不得同时启用 App 质检")

    if worker_main is None:
        from services.api.app.worker import main as worker_main

    policy: SupplementalEvidencePolicy = build_lab_supplemental_evidence_policy()
    await worker_main(h5_supplemental_evidence_policy=policy)


def main() -> None:
    asyncio.run(run_h5_probe_lab_worker())


if __name__ == "__main__":
    main()
