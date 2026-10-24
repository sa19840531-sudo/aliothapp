"""H5 补采本地 Lab Worker 的安全边界测试。"""

from unittest.mock import AsyncMock

import pytest

from services.api.app.core.config import Settings
from services.api.app.h5_probe_lab_worker import run_h5_probe_lab_worker


@pytest.mark.asyncio
async def test_lab_worker_injects_ready_policy_for_isolated_h5_explorer() -> None:
    worker_main = AsyncMock()
    settings = Settings.model_validate(
        {
            "environment": "local",
            "worker": {"roles": ["explorer"]},
            "workflow": {
                "version": "v2",
                "h5_v2_mode": "on",
                "web_v2_mode": "off",
                "app_qc_enabled": False,
            },
        }
    )

    await run_h5_probe_lab_worker(settings=settings, worker_main=worker_main)

    policy = worker_main.await_args.kwargs["h5_supplemental_evidence_policy"]
    assert policy.enabled is True
    assert policy.runtime_ready is True


@pytest.mark.asyncio
async def test_lab_worker_injects_same_ready_policy_for_h5_explorer_and_judge() -> None:
    worker_main = AsyncMock()
    settings = Settings.model_validate(
        {
            "environment": "local",
            "worker": {"roles": ["explorer", "judge"]},
            "workflow": {
                "version": "v2",
                "h5_v2_mode": "on",
                "web_v2_mode": "off",
                "app_qc_enabled": False,
            },
        }
    )

    await run_h5_probe_lab_worker(settings=settings, worker_main=worker_main)

    policy = worker_main.await_args.kwargs["h5_supplemental_evidence_policy"]
    assert policy.enabled is True
    assert policy.runtime_ready is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("settings_values", "message"),
    [
        ({"environment": "production"}, "生产环境"),
        ({"worker": {"roles": ["judge"]}}, "只能启用 H5 explorer 和 judge"),
        ({"worker": {"roles": ["explorer", "web_explorer"]}}, "只能启用 H5 explorer 和 judge"),
        ({"workflow": {"h5_v2_mode": "off"}}, "H5 V2"),
        ({"workflow": {"web_v2_mode": "on"}}, "桌面 Web"),
        ({"workflow": {"app_qc_enabled": True}}, "App"),
    ],
)
async def test_lab_worker_rejects_non_h5_or_production_scope(
    settings_values: dict[str, object],
    message: str,
) -> None:
    base = {
        "environment": "local",
        "worker": {"roles": ["explorer"]},
        "workflow": {
            "version": "v2",
            "h5_v2_mode": "on",
            "web_v2_mode": "off",
            "app_qc_enabled": False,
        },
    }
    for key, value in settings_values.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = {**base[key], **value}
        else:
            base[key] = value
    worker_main = AsyncMock()

    with pytest.raises(RuntimeError, match=message):
        await run_h5_probe_lab_worker(
            settings=Settings.model_validate(base),
            worker_main=worker_main,
        )

    worker_main.assert_not_awaited()
