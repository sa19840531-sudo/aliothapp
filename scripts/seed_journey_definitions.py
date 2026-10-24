"""将 docs/data/journey_definitions/*.json 发布到 qc_journey_definition。

用法（容器内，推荐）：
  docker exec -w /workspace alioth-api python scripts/seed_journey_definitions.py

主机侧需能连到 Postgres（把主机名换成 localhost 与映射端口）。
幂等：同 definition_id+version+content_hash 已存在则跳过。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from services.api.app.application.journey_definitions import (  # noqa: E402
    JourneyDefinitionService,
)
from services.api.app.domain.journey import parse_journey_definition  # noqa: E402
from services.api.app.infrastructure.db.journey_definition_repository import (  # noqa: E402
    PostgresJourneyDefinitionRepository,
)

DEFAULT_DIR = _ROOT / "docs" / "data" / "journey_definitions"


def _database_url() -> str:
    load_dotenv(dotenv_path=_ROOT / ".env")
    load_dotenv(dotenv_path=_ROOT / "infra" / "env" / ".env.local", override=False)
    raw = os.getenv("ALIOTH_DATABASE_URL")
    if not raw:
        raise RuntimeError("缺少 ALIOTH_DATABASE_URL")
    return raw.replace("postgresql+asyncpg://", "postgresql://", 1)


def _load_payloads(directory: Path) -> list[dict]:
    payloads: list[dict] = []
    for path in sorted(directory.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            payloads.extend(item for item in raw if isinstance(item, dict))
        elif isinstance(raw, dict):
            payloads.append(raw)
        else:
            raise RuntimeError(f"{path} 必须是对象或对象数组")
    if not payloads:
        raise RuntimeError(f"{directory} 未找到任何 Journey Definition JSON")
    return payloads


async def _publish_all(*, directory: Path, database_url: str) -> int:
    repo = PostgresJourneyDefinitionRepository(database_url=database_url)
    service = JourneyDefinitionService(repository=repo)
    created = 0
    reused = 0
    for payload in _load_payloads(directory):
        definition = parse_journey_definition(payload)
        result = await service.publish(definition)
        label = f"{definition.definition_id}@v{definition.version}"
        if result.created:
            created += 1
            print(f"created {label} hash={definition.content_hash[:16]}…", flush=True)
        else:
            reused += 1
            print(f"exists  {label} hash={definition.content_hash[:16]}…", flush=True)
    print(f"done created={created} exists={reused} total={created + reused}", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed qc_journey_definition from JSON")
    parser.add_argument(
        "--dir",
        type=Path,
        default=DEFAULT_DIR,
        help="Journey JSON 目录（默认 docs/data/journey_definitions）",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="覆盖 ALIOTH_DATABASE_URL",
    )
    args = parser.parse_args()
    url = args.database_url or _database_url()
    return asyncio.run(_publish_all(directory=args.dir, database_url=url))


if __name__ == "__main__":
    raise SystemExit(main())
