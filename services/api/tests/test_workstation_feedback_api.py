"""工作台反馈工单接口：提交、可见范围、处理权限。"""

from dataclasses import dataclass, field
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from services.api.app.api.v1 import auth as auth_module
from services.api.app.api.v1.workstation import feedback as feedback_module
from services.api.app.main import create_app

NOW = datetime(2026, 9, 8, 8, 0, tzinfo=UTC)
HQ_TENANT = "00000000-0000-0000-0000-000000000001"
BRANCH_TENANT = "00000000-0000-0000-0000-00000000000a"


@dataclass
class FakeFeedbackRepository:
    tickets: list[dict] = field(default_factory=list)
    last_list_call: dict | None = None

    async def create_ticket(self, payload: dict) -> dict:
        item = {
            "id": f"ticket-{len(self.tickets) + 1}",
            "ticket_no": f"FB2026090800{len(self.tickets) + 1:02d}",
            "tenant_name": "北京分公司",
            "status": "pending",
            "reply": None,
            "handled_by": None,
            "handled_at": None,
            "created_at": NOW,
            "updated_at": NOW,
            **payload,
        }
        self.tickets.append(item)
        return dict(item)

    async def list_tickets(self, **kwargs):
        self.last_list_call = kwargs
        items = [dict(item) for item in self.tickets]
        if kwargs.get("tenant_id") is not None:
            items = [item for item in items if item["tenant_id"] == kwargs["tenant_id"]]
        if kwargs.get("submitter_id") is not None:
            items = [item for item in items if item["submitter_id"] == kwargs["submitter_id"]]
        return items, len(items)

    async def find_by_id(self, ticket_id: str):
        return next((dict(item) for item in self.tickets if item["id"] == ticket_id), None)

    async def update_ticket(self, ticket_id: str, *, status: str, reply, handled_by: str):
        for item in self.tickets:
            if item["id"] == ticket_id:
                item.update(status=status, reply=reply, handled_by=handled_by, handled_at=NOW)
                return dict(item)
        return None


def _user(user_id: str, role: str, tenant_id: str, permissions: list[str]):
    return auth_module.AuthUserResponse(
        id=user_id,
        username=user_id,
        full_name=f"用户 {user_id}",
        role=role,
        status="enabled",
        permissions=permissions,
        tenant_id=tenant_id,
        tenant_name="北京分公司",
    )


PLAIN_USER = _user(
    "user-plain",
    "规则配置员",
    BRANCH_TENANT,
    ["dashboard", "qcRules", "taskObjects:read:owner", "taskObjects:write:owner"],
)
COMPANY_ADMIN = _user(
    "user-admin",
    "公司管理员",
    BRANCH_TENANT,
    ["dashboard", "users", "taskObjects:read:tenant", "taskObjects:review:tenant"],
)
HQ_ADMIN = _user(
    "user-hq",
    "平台管理员",
    HQ_TENANT,
    ["dashboard", "taskObjects:read:any", "taskObjects:write:any", "taskObjects:review:any"],
)


def _client(repo: FakeFeedbackRepository, user) -> TestClient:
    app = create_app()
    app.dependency_overrides[feedback_module._get_feedback_repository] = lambda: repo
    app.dependency_overrides[auth_module.require_auth_user] = lambda: user
    return TestClient(app)


def _seed(repo: FakeFeedbackRepository) -> None:
    for index, (submitter, tenant) in enumerate(
        [("user-plain", BRANCH_TENANT), ("user-other", BRANCH_TENANT), ("user-hq", HQ_TENANT)],
        start=1,
    ):
        repo.tickets.append(
            {
                "id": f"ticket-{index}",
                "ticket_no": f"FB2026090800{index:02d}",
                "tenant_id": tenant,
                "tenant_name": "北京分公司",
                "submitter_id": submitter,
                "submitter_name": submitter,
                "kind": "false_positive",
                "priority": "medium",
                "title": f"反馈 {index}",
                "description": "描述",
                "task_id": None,
                "rule_code": None,
                "contact": None,
                "status": "pending",
                "reply": None,
                "handled_by": None,
                "handled_at": None,
                "created_at": NOW,
                "updated_at": NOW,
            }
        )


def test_create_feedback_binds_submitter_and_tenant_from_login_state() -> None:
    repo = FakeFeedbackRepository()
    client = _client(repo, PLAIN_USER)

    response = client.post(
        "/api/v1/workstation/feedback",
        json={
            "kind": "missed",
            "priority": "high",
            "title": "首页弹窗漏判",
            "description": "首页有倒计时广告但报告里没体现",
            "task_id": "  task-1  ",
            "rule_code": "alioth_3",
            "tenant_id": "attacker-tenant",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["ticket_no"].startswith("FB")
    assert body["submitter_id"] == "user-plain"
    assert body["submitter_name"] == "用户 user-plain"
    assert body["tenant_id"] == BRANCH_TENANT
    assert body["task_id"] == "task-1"
    assert body["status"] == "pending"


def test_list_scope_follows_role() -> None:
    repo = FakeFeedbackRepository()
    _seed(repo)

    own = _client(repo, PLAIN_USER).get("/api/v1/workstation/feedback").json()
    assert own["visibility"] == "own"
    assert own["can_handle"] is False
    assert [item["id"] for item in own["items"]] == ["ticket-1"]
    assert repo.last_list_call["submitter_id"] == "user-plain"

    tenant = _client(repo, COMPANY_ADMIN).get("/api/v1/workstation/feedback").json()
    assert tenant["visibility"] == "tenant"
    assert tenant["can_handle"] is True
    assert [item["id"] for item in tenant["items"]] == ["ticket-1", "ticket-2"]
    assert repo.last_list_call["tenant_id"] == BRANCH_TENANT
    assert repo.last_list_call["submitter_id"] is None

    everything = _client(repo, HQ_ADMIN).get("/api/v1/workstation/feedback").json()
    assert everything["visibility"] == "all"
    assert everything["total"] == 3
    assert repo.last_list_call["tenant_id"] is None


def test_plain_user_cannot_see_or_handle_others_ticket() -> None:
    repo = FakeFeedbackRepository()
    _seed(repo)
    client = _client(repo, PLAIN_USER)

    assert client.get("/api/v1/workstation/feedback/ticket-2").status_code == 404
    assert client.get("/api/v1/workstation/feedback/ticket-1").status_code == 200
    denied = client.patch(
        "/api/v1/workstation/feedback/ticket-1",
        json={"status": "resolved", "reply": "自己关掉"},
    )
    assert denied.status_code == 403
    assert repo.tickets[0]["status"] == "pending"


def test_company_admin_handles_own_tenant_ticket_only() -> None:
    repo = FakeFeedbackRepository()
    _seed(repo)
    client = _client(repo, COMPANY_ADMIN)

    handled = client.patch(
        "/api/v1/workstation/feedback/ticket-2",
        json={"status": "processing", "reply": "已转规则负责人"},
    )
    assert handled.status_code == 200
    body = handled.json()
    assert body["status"] == "processing"
    assert body["reply"] == "已转规则负责人"
    assert body["handled_by"] == "用户 user-admin"

    other_tenant = client.patch(
        "/api/v1/workstation/feedback/ticket-3",
        json={"status": "closed"},
    )
    assert other_tenant.status_code == 404
