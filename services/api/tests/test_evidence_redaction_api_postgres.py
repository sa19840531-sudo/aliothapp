"""真实 PG、文件存储与公开 API 验证可供业务复核的局部脱敏交付。"""

import base64
import hashlib
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import asyncpg
import httpx
from PIL import Image, ImageDraw

from services.api.app.api.v1 import auth, evidence, object_access
from services.api.app.api.v2 import evidence_redactions
from services.api.app.application.task_insight import TaskInsightService
from services.api.app.domain.evidence import (
    ArtifactReference,
    EvidenceQualityStatus,
    build_evidence_manifest_reference,
)
from services.api.app.domain.protocol import stable_protocol_hash
from services.api.app.infrastructure.db.evidence_download_audit_repository import (
    PostgresEvidenceDownloadAuditRepository,
)
from services.api.app.infrastructure.db.evidence_redaction_repository import (
    PostgresEvidenceRedactionRepository,
)
from services.api.app.infrastructure.db.object_access_repository import (
    PostgresObjectAccessRepository,
)
from services.api.app.infrastructure.db.task_insight_repository import PostgresTaskInsightRepository
from services.api.app.infrastructure.db.user_repository import PostgresUserRepository
from services.api.app.infrastructure.storage.local_evidence_store import LocalEvidenceArtifactStore
from services.api.app.main import create_app
from services.api.tests.test_exploration_scheduler_facts_postgres_integration import (
    _seed_context,
    _test_database_url,
    _upgrade_isolated_postgres,  # noqa: F401 - 只迁移显式隔离库
)
from services.api.tests.test_exploration_scheduler_facts_postgres_integration import (
    pytestmark as pytestmark,
)


def _sha(content):
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


@asynccontextmanager
async def retained_case():
    """专用测试库中保留受控证据原件及归属，关闭连接而不清除不可变验收链。"""
    database_url = _test_database_url()
    connection = await asyncpg.connect(database_url)
    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=3)
    ids = {
        name: str(uuid4())
        for name in (
            "tenant",
            "user",
            "ruleset",
            "rule",
            "task",
            "run",
            "capture",
            "old_capture",
            "bootstrap_capture",
            "before_capture",
            "inspection_work_item",
            "old_inspection_work_item",
        )
    }
    ids.update(
        {
            name: f"redaction-api-{name}-{uuid4()}"
            for name in (
                "work",
                "session",
                "index",
                "action",
                "bootstrap_manifest",
                "request",
                "old_request",
            )
        }
    )
    root = Path.cwd() / ".alioth/reports/2026-10-23-evidence-local-redaction/api-pg" / ids["task"]
    root.mkdir(parents=True)
    try:
        await _seed_context(connection, ids, surface_kind="web", target_platform="h5")
        yield connection, pool, ids, root
    finally:
        await pool.close()
        await connection.close()


def materials():
    image = Image.new("RGB", (320, 160), (242, 246, 250))
    draw = ImageDraw.Draw(image)
    draw.text((12, 12), "Order review", fill="black")
    draw.rectangle((12, 80, 180, 140), fill=(50, 170, 90))
    draw.text((20, 95), "PAID  USD 128.00", fill="white")
    draw.text((205, 26), "13800138000", fill="black")
    output = BytesIO()
    image.save(output, format="PNG")
    text = "订单编号：ORDER-001\n状态：已支付\n金额：128.00元\n手机号：13800138000\n"
    start = text.index("13800138000")
    return [
        (
            "screenshot",
            output.getvalue(),
            {
                "method": "image_regions",
                "image_width": 320,
                "image_height": 160,
                "regions": [{"x": 200, "y": 20, "width": 110, "height": 30}],
            },
            ".png",
        ),
        (
            "visible_text",
            text,
            {
                "method": "text_ranges",
                "ranges": [{"start": start, "end": start + 11}],
            },
            ".txt",
        ),
        (
            "media_state",
            {"order": {"status": "paid", "amount": 128, "phone": "13800138000"}},
            {
                "method": "json_pointers",
                "pointers": ["/order/phone"],
            },
            ".json",
        ),
    ]


async def seed_materials(connection, ids, store):
    members, cases = [], []
    for kind, content, spec, extension in materials():
        artifact = await store.store(
            task_id=ids["task"],
            page_capture_id=ids["capture"],
            artifact_id=str(uuid4()),
            artifact_type=kind,
            content=content,
        )
        await connection.execute(
            "INSERT INTO qc_evidence_artifact (id,task_id,page_capture_id,artifact_type,"
            "source,storage_path,content_type,checksum,size_bytes,metadata) "
            "VALUES ($1::uuid,$2::uuid,$3::uuid,$4,'baseline',$5,$6,$7,$8,$9::jsonb)",
            artifact.artifact_id,
            ids["task"],
            ids["capture"],
            kind,
            artifact.storage_path,
            artifact.content_type,
            artifact.checksum,
            artifact.size_bytes,
            json.dumps(artifact.metadata),
        )
        members.append(
            ArtifactReference(
                artifact_id=artifact.artifact_id,
                artifact_type=kind,
                role="primary",
                storage_path=artifact.storage_path,
                checksum=artifact.checksum,
                size_bytes=artifact.size_bytes,
                content_type=artifact.content_type,
            )
        )
        cases.append((artifact, spec, extension))
    manifest = build_evidence_manifest_reference(
        task_id=ids["task"],
        session_id=ids["session"],
        capture_id=ids["capture"],
        manifest_version=1,
        artifacts=members,
        quality_status=EvidenceQualityStatus.PASS,
        created_at=datetime.now(UTC),
    )
    await connection.execute(
        "INSERT INTO qc_evidence_manifest (manifest_id,manifest_version,task_id,session_id,"
        "capture_id,protocol_version,manifest_hash,payload_hash,quality_status,manifest_payload,"
        "created_at) VALUES ($1,1,$2::uuid,$3,$4::uuid,'evidence-manifest.v1',$5,$6,'pass',"
        "$7::jsonb,$8)",
        manifest.manifest_id,
        ids["task"],
        ids["session"],
        ids["capture"],
        manifest.manifest_hash,
        stable_protocol_hash(manifest.to_dict()),
        json.dumps(manifest.to_dict()),
        manifest.created_at,
    )
    return cases, manifest


def build_app(pool, store, user):
    app = create_app()
    repository = PostgresEvidenceRedactionRepository(pool=pool)
    reader = TaskInsightService(
        repository=PostgresTaskInsightRepository(pool=pool),
        evidence_store_factory=lambda: store,
    )
    audit = PostgresEvidenceDownloadAuditRepository(pool=pool)
    access = PostgresObjectAccessRepository(pool=pool)
    users = PostgresUserRepository(pool=pool)
    app.dependency_overrides.update(
        {
            auth.require_auth_user: lambda: auth._build_user_response(user),
            evidence_redactions._get_redaction_repository: lambda: repository,
            evidence._get_task_insight_service: lambda: reader,
            evidence._get_download_audit_repository: lambda: audit,
            object_access._get_object_access_repository: lambda: access,
            evidence._get_ticket_subject_repository: lambda: users,
        }
    )
    return app


def assert_reviewable(kind, original, derived):
    if kind == "screenshot":
        with Image.open(BytesIO(original)) as before, Image.open(BytesIO(derived)) as after:
            assert after.size == before.size == (320, 160)
            assert after.getpixel((205, 25)) == (32, 32, 32)
            assert (
                after.crop((12, 80, 180, 140)).tobytes()
                == before.crop((12, 80, 180, 140)).tobytes()
            )
    elif kind == "visible_text":
        assert "已支付" in derived.decode() and "128.00元" in derived.decode()
        assert b"13800138000" not in derived and "[已脱敏]" in derived.decode()
    else:
        parsed = json.loads(derived)
        assert parsed["order"] == {"status": "paid", "amount": 128, "phone": "[已脱敏]"}


async def test_public_redaction_api_keeps_business_facts_originals_and_pg_audit_chain():
    async with retained_case() as (connection, pool, ids, root):
        store = LocalEvidenceArtifactStore(root=root / "originals")
        cases, manifest = await seed_materials(connection, ids, store)
        user = await PostgresUserRepository(pool=pool).find_by_id(ids["user"])
        app = build_app(pool, store, user)
        receipt = {"ids": ids, "manifest": manifest.to_dict(), "materials": [], "controlled": True}
        _write_json(root / "manifest.json", manifest.to_dict())
        before = await connection.fetch(
            "SELECT * FROM qc_evidence_artifact WHERE task_id=$1::uuid ORDER BY id",
            ids["task"],
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for artifact, spec, extension in cases:
                artifact_id = artifact.artifact_id
                body = {
                    "source_checksum": artifact.checksum,
                    "manifest_id": manifest.manifest_id,
                    "manifest_version": 1,
                    "spec": spec,
                    "review_confirmed": True,
                }
                prefix = f"/api/v2/evidence/{artifact_id}/redactions"
                preview = await client.post(prefix + "/preview", json=body)
                assert preview.status_code == 200, preview.text
                preview_bytes = base64.b64decode(preview.json()["content_base64"])
                created = await client.post(prefix, json=body)
                assert created.status_code == 200, created.text
                summary = created.json()
                redaction_id = summary["redaction_id"]
                delivered = await client.get(f"/api/v2/evidence-redactions/{redaction_id}/file")
                assert delivered.status_code == 200, delivered.text
                assert delivered.content == preview_bytes
                assert delivered.headers["cache-control"] == "no-store"
                assert _sha(delivered.content) == summary["derived_checksum"]
                original = await store.read(artifact.storage_path)
                assert _sha(original) == artifact.checksum
                assert_reviewable(artifact.artifact_type, original, delivered.content)
                listed = await client.get(prefix)
                assert listed.status_code == 200 and listed.json()["items"] == [summary]
                plan = await PostgresEvidenceRedactionRepository(pool=pool).get(redaction_id)
                assert plan["source"]["manifest_hash"] == manifest.manifest_hash
                assert plan["derived_checksum"] == _sha(delivered.content) != artifact.checksum
                (root / f"derived-{artifact_id}{extension}").write_bytes(delivered.content)
                _write_json(root / f"plan-{redaction_id}.json", plan)
                receipt["materials"].append(
                    {
                        "artifact_id": artifact_id,
                        "redaction_id": redaction_id,
                        "plan_hash": plan["plan_hash"],
                        "original_checksum": artifact.checksum,
                        "derived_checksum": summary["derived_checksum"],
                        "spec": spec,
                        "requests": [
                            response.headers["x-evidence-redaction-request-id"]
                            for response in (preview, created, delivered, listed)
                        ],
                    }
                )
            # 同一公开读取入口在当前主体停用后拒绝交付，仍由真实审计仓储记录。
            await connection.execute(
                "UPDATE qc_user SET status='disabled' WHERE id=$1::uuid", ids["user"]
            )
            revoked = await client.get(f"/api/v2/evidence-redactions/{redaction_id}/file")
            assert revoked.status_code == 404
            assert b"13800138000" not in revoked.content
            await connection.execute(
                "UPDATE qc_user SET status='enabled' WHERE id=$1::uuid", ids["user"]
            )
        after = await connection.fetch(
            "SELECT * FROM qc_evidence_artifact WHERE task_id=$1::uuid ORDER BY id",
            ids["task"],
        )
        assert after == before and len(after) == 3
        audit_rows = await connection.fetch(
            "SELECT * FROM qc_evidence_download_audit WHERE task_id=$1 ORDER BY created_at,id",
            ids["task"],
        )
        assert len(audit_rows) == 13
        assert sum(row["result"] == "allowed" for row in audit_rows) == 12
        assert sum(row["result"] == "denied" for row in audit_rows) == 1
        receipt["audit"] = [dict(row) for row in audit_rows]
        receipt["database"] = "alioth_local_redaction_test"
        _write_json(root / "receipt.json", receipt)
        print(f"局部脱敏真实 API/PG 验收回执：{root / 'receipt.json'}")
