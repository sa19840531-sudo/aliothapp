"""2.0 原型对齐补的接口：任务列表新列与筛选、任务草稿、产品 CSV 导入、报告批量导出。"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import BytesIO
from zipfile import ZipFile

from fastapi.testclient import TestClient

from services.api.app.api.v1 import products as products_module
from services.api.app.api.v1.workstation import reports as workstation_reports_module
from services.api.app.api.v1.workstation import task_drafts as task_drafts_module
from services.api.app.api.v1.workstation import tasks as workstation_tasks_module
from services.api.app.application.report_insight import ReportExportBlockedError
from services.api.app.application.workstation_tasks import (
    ProductSnapshot,
    WorkstationTaskListItem,
    WorkstationTaskListResult,
)
from services.api.app.domain.task import TaskExecutionMode, TaskTargetPlatform
from services.api.app.main import create_app

NOW = datetime(2026, 9, 8, 8, 0, tzinfo=UTC)


# ---------- 任务列表：所属公司 / 发起人 / 执行时长 + 部门 / 平台筛选 ----------


class FakeTaskService:
    def __init__(self) -> None:
        self.list_filter = None

    async def list_tasks(self, filters, *, access_scope):
        self.list_filter = filters
        item = WorkstationTaskListItem(
            task_id="task-1",
            task_name="手机银行首页巡检",
            product=ProductSnapshot(department_name="一部", product_code="MB-001", product_name="手机银行 Android"),
            target_url=None,
            target_platform=TaskTargetPlatform.APP,
            execution_mode=TaskExecutionMode.FULL_INSPECTION,
            journey_steps=[],
            status="done",
            progress=100,
            report_status="ready",
            report_availability="final",
            failure_reason=None,
            available_actions=["detail", "report"],
            created_at=NOW,
            tenant_name="北京分公司",
            submitter_name="张敏",
            execution_started_at=datetime(2026, 9, 8, 1, 28, tzinfo=UTC),
            execution_finished_at=datetime(2026, 9, 8, 1, 30, 15, tzinfo=UTC),
        )
        return WorkstationTaskListResult(items=[item], total=1, page=filters.page, page_size=filters.page_size)


def test_task_list_exposes_prototype_columns_and_filters() -> None:
    service = FakeTaskService()
    app = create_app()
    app.dependency_overrides[workstation_tasks_module._get_workstation_task_service] = lambda: service
    client = TestClient(app)

    response = client.get(
        "/api/v1/workstation/tasks",
        params={"department_name": "一部", "target_platform": "app", "page_size": 10},
    )

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["tenant_name"] == "北京分公司"
    assert item["submitter_name"] == "张敏"
    assert item["duration_ms"] == 135_000
    assert service.list_filter.department_name == "一部"
    assert service.list_filter.target_platform == "app"


# ---------- 任务草稿 ----------


@dataclass
class FakeTaskDraftRepository:
    drafts: list[dict] = field(default_factory=list)

    async def list_drafts(self, *, owner_id: str, limit: int = 50):
        return [dict(item) for item in self.drafts if item["owner_id"] == owner_id][:limit]

    async def create_draft(self, payload: dict):
        item = {
            "id": f"draft-{len(self.drafts) + 1}",
            "created_at": NOW,
            "updated_at": NOW,
            **payload,
        }
        self.drafts.append(item)
        return dict(item)

    async def update_draft(self, draft_id: str, *, owner_id: str, payload: dict):
        for item in self.drafts:
            if item["id"] == draft_id and item["owner_id"] == owner_id:
                item.update(payload)
                return dict(item)
        return None

    async def delete_draft(self, draft_id: str, *, owner_id: str) -> bool:
        before = len(self.drafts)
        self.drafts = [
            item for item in self.drafts if not (item["id"] == draft_id and item["owner_id"] == owner_id)
        ]
        return len(self.drafts) < before


def test_task_drafts_are_bound_to_the_login_user() -> None:
    repo = FakeTaskDraftRepository()
    app = create_app()
    app.dependency_overrides[task_drafts_module._get_task_draft_repository] = lambda: repo
    client = TestClient(app)

    created = client.post(
        "/api/v1/workstation/task-drafts",
        json={"name": "手机银行回归", "target_platform": "app", "payload": {"form": {"task_name": "手机银行回归"}}},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["payload"]["form"]["task_name"] == "手机银行回归"
    assert repo.drafts[0]["owner_id"] == "test-user"
    assert repo.drafts[0]["tenant_id"] == "tenant-1"

    listed = client.get("/api/v1/workstation/task-drafts").json()
    assert [item["id"] for item in listed["items"]] == [body["id"]]

    # 别人的草稿看不到、删不掉
    repo.drafts.append({**repo.drafts[0], "id": "draft-other", "owner_id": "someone-else"})
    assert [item["id"] for item in client.get("/api/v1/workstation/task-drafts").json()["items"]] == [body["id"]]
    assert client.delete("/api/v1/workstation/task-drafts/draft-other").status_code == 404
    assert client.delete(f"/api/v1/workstation/task-drafts/{body['id']}").status_code == 204


# ---------- 产品 CSV 导入 ----------


@dataclass
class FakeProductRepository:
    products: dict = field(default_factory=dict)
    records: list = field(default_factory=list)

    async def upsert_product(self, payload: dict):
        inserted = payload["project_code"] not in self.products
        if payload["project_code"] == "BAD-DB":
            raise RuntimeError("数据库拒绝")
        self.products[payload["project_code"]] = dict(payload)
        return dict(payload), inserted

    async def find_by_code(self, project_code: str):
        item = self.products.get(project_code)
        return dict(item) if item is not None else None

    async def record_import(self, payload: dict):
        item = {"id": "record-1", "batch_no": "IMP-20260908-001", **payload}
        self.records.append(item)
        return item

    async def find_import_record(self, record_id: str):
        return next(
            ({**item, "tenant_name": "北京分公司", "created_at": NOW} for item in self.records if item["id"] == record_id),
            None,
        )

    async def list_import_records(self, *, tenant_id, limit=50):
        return [
            {
                **item,
                "tenant_name": "北京分公司",
                "created_at": NOW,
            }
            for item in self.records
        ]


def test_product_csv_import_upserts_and_records_errors() -> None:
    repo = FakeProductRepository(products={"H5-001": {"project_code": "H5-001", "tenant_id": "tenant-1"}})
    app = create_app()
    app.dependency_overrides[products_module._get_product_repository] = lambda: repo
    client = TestClient(app)

    csv_text = "\n".join(
        [
            "产品编码,产品名称,所属部门,负责人,支持平台,H5地址,状态",
            "MB-001,手机银行 Android,一部,小明,Android,,启用",
            "H5-001,H5 活动页,一部,小红,H5;Web,https://example.com/h5,停用",
            ",没有编码的行,一部,,,,",
            "BAD-URL,坏地址,一部,,H5,example.com,启用",
            "BAD-DB,库拒绝,一部,,,,",
        ]
    )
    response = client.post(
        "/api/v1/products/import",
        files={"file": ("products.csv", csv_text.encode("utf-8-sig"), "text/csv")},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created_count"] == 1
    assert body["updated_count"] == 1
    assert body["failed_count"] == 3
    assert [item["row"] for item in body["errors"]] == [4, 5, 6]
    assert repo.products["MB-001"]["platforms"] == ["app"]
    assert repo.products["MB-001"]["department_name"] == "一部"
    assert repo.products["H5-001"]["platforms"] == ["h5", "web"]
    assert repo.products["H5-001"]["status"] == "disabled"
    assert repo.records[0]["operator_name"] == "测试用户"
    assert repo.records[0]["failed_count"] == 3

    records = client.get("/api/v1/products/import-records").json()["items"]
    assert records[0]["filename"] == "products.csv"
    assert records[0]["tenant_name"] == "北京分公司"
    assert records[0]["batch_no"] == "IMP-20260908-001"

    result = client.get("/api/v1/products/import-records/record-1/result")
    assert result.status_code == 200, result.text
    text = result.content.decode("utf-8-sig")
    assert "IMP-20260908-001" in text
    assert "产品编码或产品名称为空" in text


def test_product_csv_import_rejects_non_csv_and_missing_columns() -> None:
    repo = FakeProductRepository()
    app = create_app()
    app.dependency_overrides[products_module._get_product_repository] = lambda: repo
    client = TestClient(app)

    xlsx = client.post("/api/v1/products/import", files={"file": ("p.xlsx", b"PK", "application/octet-stream")})
    assert xlsx.status_code == 400

    missing = client.post(
        "/api/v1/products/import",
        files={"file": ("p.csv", "产品名称\n仅有名称\n".encode(), "text/csv")},
    )
    assert missing.status_code == 400
    assert "产品编码" in missing.json()["error"]["message"]


# ---------- 报告批量导出 ----------


class FakeReportInsightService:
    async def export_html(self, task_id: str, *, access_scope):
        if task_id == "blocked":
            raise ReportExportBlockedError("仍有待确认")
        return f"report_{task_id}.html", f"<html>{task_id}</html>"


def test_batch_export_zips_exportable_reports_and_lists_skipped() -> None:
    app = create_app()
    app.dependency_overrides[workstation_reports_module._get_report_insight_service] = (
        lambda: FakeReportInsightService()
    )
    client = TestClient(app)

    response = client.get(
        "/api/v1/workstation/reports/export-batch",
        params=[("task_ids", "task-a"), ("task_ids", "blocked"), ("task_ids", "task-a")],
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/zip")
    with ZipFile(BytesIO(response.content)) as archive:
        names = archive.namelist()
        assert "report_task-a.html" in names
        assert "未导出说明.txt" in names
        assert "blocked" in archive.read("未导出说明.txt").decode("utf-8")
        assert names.count("report_task-a.html") == 1

    nothing = client.get("/api/v1/workstation/reports/export-batch", params={"task_ids": "blocked"})
    assert nothing.status_code == 409
