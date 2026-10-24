"""将当前运行时Capability Manifest同步到数据库注册表。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from dotenv import load_dotenv

from services.api.app.agents.bootstrap import build_default_capability_manifest
from services.api.app.application.capability_registry import CapabilityRegistrySyncService
from services.api.app.infrastructure.db.capability_registry_repository import (
    PostgresCapabilityRegistryRepository,
)

ROOT_DIR = Path(__file__).resolve().parents[1]


async def main() -> None:
    load_dotenv(dotenv_path=ROOT_DIR / ".env")
    manifest = build_default_capability_manifest()
    service = CapabilityRegistrySyncService(repository=PostgresCapabilityRegistryRepository())
    result = await service.sync(manifest)
    print(
        "capability registry synced "
        f"synced={result.synced_count} disabled={result.disabled_count}"
    )


if __name__ == "__main__":
    asyncio.run(main())
