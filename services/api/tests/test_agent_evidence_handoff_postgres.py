"""真实 PostgreSQL 验证采集角色交接、冻结指纹和跨 Run 拒绝。"""

import hashlib
import json
import os
from contextlib import asynccontextmanager
from dataclasses import replace
from io import BytesIO
from uuid import uuid4

import asyncpg
import pytest
from PIL import Image

from services.api.app.application.observation_packet import (
    BuildObservationPacketRequest,
    ObservationPacketService,
    ObservationPacketValidationError,
)
from services.api.app.infrastructure.db.observation_packet_repository import (
    PostgresObservationPacketRepository,
)
from services.api.app.infrastructure.db.test_database_guard import (
    require_isolated_test_database_url,
)


class ConnectionPool:
    def __init__(self, connection):
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


class MemoryStore:
    def __init__(self):
        self.payloads = {}

    async def read(self, path):
        return self.payloads[path]


@pytest.fixture
async def evidence_database():
    url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not url:
        pytest.skip("需要显式隔离 ALIOTH_TEST_DATABASE_URL；只写连接级临时表")
    database = await asyncpg.connect(
        require_isolated_test_database_url(
            url,
            os.getenv("ALIOTH_DATABASE_URL") or None,
        )
    )
    try:
        await database.execute("""
            CREATE TEMP TABLE qc_task (id uuid PRIMARY KEY, target_platform text);
            CREATE TEMP TABLE qc_capture (
                id uuid PRIMARY KEY, task_id uuid, agent_run_id uuid, surface_kind text,
                capture_role text, parent_capture_id uuid, created_at timestamptz DEFAULT now(),
                capture_status text DEFAULT 'captured', session_command_sequence integer,
                metadata jsonb DEFAULT '{}'::jsonb
            );
            CREATE TEMP TABLE qc_page_capture (
                id uuid, page_seq integer, url text, page_type text,
                page_state text, action_log jsonb
            );
            CREATE TEMP TABLE qc_app_capture (
                capture_id uuid, package_name text, activity text, width integer, height integer,
                device_snapshot jsonb
            );
            CREATE TEMP TABLE qc_app_visual_observation (
                capture_id uuid, visual_summary text, key_elements jsonb, candidate_targets jsonb,
                result_hash text, page_state text, overlay_signature text, surface_facts jsonb,
                created_at timestamptz DEFAULT now()
            );
            CREATE TEMP TABLE qc_evidence_artifact (
                id uuid, task_id uuid, page_capture_id uuid, source text, artifact_type text,
                storage_path text, content_type text, checksum text, size_bytes integer,
                created_at timestamptz DEFAULT now()
            );
            CREATE TEMP TABLE qc_observation_packet (
                id uuid PRIMARY KEY DEFAULT gen_random_uuid(), task_id uuid, page_capture_id uuid,
                rule_id uuid, evidence_artifact_ids jsonb, packet_payload jsonb,
                quality_gate_status text, quality_gate_reason text, created_by_agent_run_id uuid,
                input_fingerprint text, updated_at timestamptz DEFAULT now()
            );
            CREATE UNIQUE INDEX packet_test_binding ON qc_observation_packet (
                created_by_agent_run_id, page_capture_id, rule_id, input_fingerprint
            ) WHERE created_by_agent_run_id IS NOT NULL AND page_capture_id IS NOT NULL
              AND rule_id IS NOT NULL AND input_fingerprint IS NOT NULL;
        """)
        ids = {
            key: str(uuid4())
            for key in (
                "task",
                "run",
                "consumer",
                "rule",
                "primary",
                "before",
                "source",
                "shot-primary",
                "shot-before",
                "shot-source",
            )
        }
        store = MemoryStore()
        await database.execute("INSERT INTO qc_task VALUES ($1::uuid, 'h5')", ids["task"])
        for seq, role in enumerate(("source", "before", "primary"), 1):
            await database.execute(
                """
                INSERT INTO qc_capture (id, task_id, agent_run_id, surface_kind, capture_role)
                VALUES ($1::uuid, $2::uuid, $3::uuid, 'web', $4)
            """,
                ids[role],
                ids["task"],
                ids["run"],
                role,
            )
            await database.execute(
                """
                INSERT INTO qc_page_capture VALUES ($1::uuid, $2, $3, 'home', 'ready', '[]')
            """,
                ids[role],
                seq,
                f"https://test.invalid/{role}",
            )
            image = Image.new("RGB", (8, 8), "white")
            image.putpixel((seq, seq), (20 * seq, 50, 180))
            output = BytesIO()
            image.save(output, format="PNG")
            payload = output.getvalue()
            path = f"{ids['task']}/{ids[role]}/{ids['shot-' + role]}.png"
            store.payloads[path] = payload
            await database.execute(
                """
                INSERT INTO qc_evidence_artifact (
                    id, task_id, page_capture_id, source, artifact_type, storage_path,
                    content_type, checksum, size_bytes
                ) VALUES ($1::uuid, $2::uuid, $3::uuid, 'baseline', 'screenshot',
                          $4, 'image/png', $5, $6)
            """,
                ids["shot-" + role],
                ids["task"],
                ids[role],
                path,
                "sha256:" + hashlib.sha256(payload).hexdigest(),
                len(payload),
            )
        repository = PostgresObservationPacketRepository(
            pool=ConnectionPool(database),
            artifact_store=store,
        )
        request = BuildObservationPacketRequest(
            task_id=ids["task"],
            page_capture_id=ids["primary"],
            rule_id=ids["rule"],
            rule_code="alioth_evidence_test",
            rule_name="真实角色材料测试",
            category="visual",
            executor_type="llm",
            check_point="实际页面材料",
            input_contract={},
            judge_config={},
            manual_review_policy={},
            requested_evidence_artifact_ids=(),
            agent_input={},
            agent_run_id=ids["consumer"],
            source_agent_run_id=ids["run"],
            evidence_capture_roles={role: ids[role] for role in ("primary", "before", "source")},
        )
        yield database, repository, request, ids
    finally:
        await database.close()


async def test_real_sql_role_images_and_binding_fingerprint(evidence_database):
    database, repository, request, ids = evidence_database
    service = ObservationPacketService(repository=repository)
    first = await service.build(request)
    assert {image[0] for image in first.image_payloads} == {
        ids["shot-primary"],
        ids["shot-before"],
        ids["shot-source"],
    }
    assert (await service.build(request)).observation_packet_id == first.observation_packet_id
    swapped = await service.build(
        replace(
            request,
            evidence_capture_roles={
                "primary": ids["primary"],
                "before": ids["source"],
                "source": ids["before"],
            },
        )
    )
    assert set(swapped.evidence_artifact_ids) == set(first.evidence_artifact_ids)
    assert swapped.observation_packet_id != first.observation_packet_id
    stored = await database.fetch(
        "SELECT input_fingerprint, evidence_artifact_ids FROM qc_observation_packet"
    )
    assert len(stored) == 2
    assert stored[0]["input_fingerprint"] != stored[1]["input_fingerprint"]
    assert set(json.loads(stored[0]["evidence_artifact_ids"])) == set(first.evidence_artifact_ids)


async def test_real_sql_cross_run_capture_rejected_before_packet_write(evidence_database):
    database, repository, request, ids = evidence_database
    await database.execute(
        "UPDATE qc_capture SET agent_run_id=$1::uuid WHERE id=$2::uuid", str(uuid4()), ids["before"]
    )
    with pytest.raises(ObservationPacketValidationError, match="生产 Run"):
        await ObservationPacketService(repository=repository).build(request)
    assert await database.fetchval("SELECT count(*) FROM qc_observation_packet") == 0


async def test_real_sql_projects_frozen_web_body_readiness(evidence_database):
    database, repository, request, ids = evidence_database
    await database.execute(
        "UPDATE qc_capture SET metadata=$1::jsonb WHERE id=$2::uuid",
        json.dumps(
            {
                "web_inspection_readiness": {
                    "role": "inspection",
                    "status": "human_verification_blocked",
                    "blocking_reasons": ["HUMAN_VERIFICATION"],
                },
                "web_page_type_assessment": {
                    "final_page_type": "content_detail",
                    "classification_status": "needs_review",
                    "needs_review": True,
                    "subject_region_facts": {
                        "primary_subject_count": 0,
                        "primary_subject_kind": "unknown",
                    },
                },
            }
        ),
        ids["primary"],
    )
    packet = await ObservationPacketService(repository=repository).build(request)
    document = packet.packet_payload["facts"]["web_document"]
    assert document["readiness"]["status"] == "human_verification_blocked"
    assert document["primary_subject"]["primary_subject_count"] == 0
    assert document["classification"]["needs_review"] is True
