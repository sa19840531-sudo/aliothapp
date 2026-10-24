"""登录与鉴权接口。

登录令牌为标准 JWT（HS256 签名）：payload 携带用户身份与权限声明，`exp` 到期
后 pyjwt 校验直接拒绝。签名密钥取自环境变量 ``ALIOTH_AUTH_SECRET``；开发环境
未配置时使用开发兜底值并打告警日志，生产环境则必须显式配置强密钥（各实例需
一致，否则互相签发的令牌无法通过校验）。
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Annotated

import jwt
from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field

from services.api.app.core.errors import AliothError
from services.api.app.domain.iam.access_scope import AccessScope, access_scope_from_user
from services.api.app.infrastructure.db.user_repository import PostgresUserRepository

logger = logging.getLogger(__name__)

router = APIRouter()

_JWT_ALGORITHM = "HS256"
_DEV_FALLBACK_SECRET = "alioth-dev-secret-change-me"
_PRODUCTION_PLACEHOLDER_SECRET = "replace-with-at-least-32-random-characters"
_MIN_PRODUCTION_SECRET_LENGTH = 32


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)


class AuthUserResponse(BaseModel):
    id: str
    username: str
    full_name: str
    role: str
    status: str
    permissions: list[str]
    # 测试替身可以暂时为空；真实登录态始终由 qc_user.tenant_id 填充。
    tenant_id: str = ""
    tenant_name: str = ""
    # 账号所属部门（部门平台页脚「公司 · 部门」、任务默认部门）。
    department_name: str = ""


class LoginResponse(BaseModel):
    token: str
    user: AuthUserResponse
    expires_at: datetime


@lru_cache
def _get_user_repository() -> PostgresUserRepository:
    return PostgresUserRepository()


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), 120000
    ).hex()
    return f"pbkdf2_sha256${salt}${digest}"


def verify_password(password: str, stored_hash: str) -> bool:
    if not stored_hash:
        return False
    if not stored_hash.startswith("pbkdf2_sha256$"):
        return secrets.compare_digest(password, stored_hash)

    parts = stored_hash.split("$", 2)
    if len(parts) != 3:
        return False
    _, salt, expected = parts
    actual = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), 120000
    ).hex()
    return secrets.compare_digest(actual, expected)


def _permissions_by_role(role: str) -> list[str]:
    if role == "平台管理员":
        # 总部（体验中心）视角：跨公司看任务、管所有公司的账号、维护公司列表。
        return [
            "dashboard",
            "users",
            "users:any",
            "tenants",
            "departments",
            "roles",
            "auditLogs",
            "qcRules",
            # 平台规则与规则集的增删改、启停、发布和规则测试只归体验中心管理员
            "qcRules:write",
            "configCenter",
            "costReports",
            "analytics",
            "system",
            "agentRuns",
            "systemOperations",
            "taskObjects:read:any",
            "taskObjects:write:any",
            "taskObjects:review:any",
            "evidenceExports:original",
            "evidenceExports:redacted",
            "evidenceRedactions:manage",
        ]
    if role == "公司管理员":
        # 分公司管理员：只管本公司账号，看本公司全部任务，自己也能发起任务。
        return [
            "dashboard",
            "users",
            "departments",
            "roles",
            "qcRules",
            "configCenter",
            "taskObjects:read:tenant",
            "taskObjects:write:owner",
            "taskObjects:review:tenant",
            "evidenceExports:original",
            "evidenceExports:redacted",
            "evidenceRedactions:manage",
        ]
    if role == "审计员":
        return [
            "dashboard",
            "auditLogs",
            "qcRules",
            "configCenter",
            "taskObjects:read:tenant",
            "taskObjects:review:tenant",
            "evidenceExports:original",
            "evidenceExports:redacted",
        ]
    # 部门账号：看本部门的任务/报告并可复核，只能改自己发起的任务；没填部门时退回只看自己的。
    return [
        "dashboard",
        "qcRules",
        "configCenter",
        "taskObjects:read:owner",
        "taskObjects:write:owner",
        "taskObjects:read:department",
        "taskObjects:review:department",
        "evidenceExports:original",
        "evidenceExports:redacted",
    ]


def _build_user_response(row: dict[str, object]) -> AuthUserResponse:
    role = str(row.get("role") or "规则配置员")
    return AuthUserResponse(
        id=str(row["id"]),
        username=str(row["username"]),
        full_name=str(row["full_name"]),
        role=role,
        status=str(row["status"]),
        permissions=_permissions_by_role(role),
        tenant_id=str(row.get("tenant_id") or ""),
        tenant_name=str(row.get("tenant_name") or ""),
        department_name=str(row.get("department_name") or ""),
    )


def _tenant_disabled(row: dict[str, object]) -> bool:
    """所属公司被总部停用后，公司里的账号一律不能再登录或续用登录态。"""
    return str(row.get("tenant_status") or "") == "disabled"


def _is_production_environment() -> bool:
    """判断当前进程是否处于需要强制密钥门禁的生产环境。"""
    return (os.getenv("ALIOTH_ENVIRONMENT") or "").strip().casefold() in {
        "prod",
        "production",
    }


def _resolve_auth_secret() -> str:
    """读取 JWT 密钥，并在生产环境拒绝缺失、弱密钥和开发兜底值。"""
    secret = (os.getenv("ALIOTH_AUTH_SECRET") or "").strip()
    if _is_production_environment():
        if not secret:
            raise ValueError("生产环境必须配置 ALIOTH_AUTH_SECRET")
        if len(secret.encode("utf-8")) < _MIN_PRODUCTION_SECRET_LENGTH:
            raise ValueError(
                "生产环境 ALIOTH_AUTH_SECRET 长度不足，UTF-8 编码后至少需要 "
                f"{_MIN_PRODUCTION_SECRET_LENGTH} 字节"
            )
        if any(
            secrets.compare_digest(secret, insecure_secret)
            for insecure_secret in (_DEV_FALLBACK_SECRET, _PRODUCTION_PLACEHOLDER_SECRET)
        ):
            raise ValueError("生产环境禁止使用开发兜底或模板占位 ALIOTH_AUTH_SECRET")
        return secret
    if secret:
        return secret
    logger.warning(
        "未配置 ALIOTH_AUTH_SECRET，JWT 使用开发兜底密钥签名；生产环境必须显式配置。"
    )
    return _DEV_FALLBACK_SECRET


@lru_cache
def _auth_secret() -> str:
    """返回进程内复用的 JWT 密钥；生产门禁在首次读取时即生效。"""
    return _resolve_auth_secret()


def validate_auth_secret_at_startup() -> None:
    """在应用装配阶段触发 JWT 密钥校验，避免生产服务带弱密钥启动。"""
    # 启动门禁必须读取当前环境，不能复用测试或预加载阶段缓存的开发密钥。
    _resolve_auth_secret()


def _encode_token(user: AuthUserResponse, *, issued_at: datetime, expires_at: datetime) -> str:
    payload = {
        "sub": user.id,
        "username": user.username,
        "full_name": user.full_name,
        "role": user.role,
        "status": user.status,
        "tenant_id": user.tenant_id,
        "permissions": user.permissions,
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    return jwt.encode(payload, _auth_secret(), algorithm=_JWT_ALGORITHM)


def download_ticket_signing_secret() -> str:
    """派生资源下载票据专用密钥，避免与登录 JWT 共享令牌用途。"""

    return hashlib.sha256(
        b"alioth-resource-download-ticket-v1\x00" + _auth_secret().encode("utf-8")
    ).hexdigest()


def _extract_token(authorization: str | None) -> str:
    if authorization:
        prefix = "Bearer "
        if not authorization.startswith(prefix):
            raise AliothError(message="登录令牌格式错误", status_code=401)
        return authorization.removeprefix(prefix).strip()
    raise AliothError(message="缺少登录令牌", status_code=401)


async def require_auth_user(
    repository: Annotated[PostgresUserRepository, Depends(_get_user_repository)],
    authorization: Annotated[str | None, Header()] = None,
) -> AuthUserResponse:
    token = _extract_token(authorization)
    try:
        payload = jwt.decode(token, _auth_secret(), algorithms=[_JWT_ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise AliothError(message="登录态已过期，请重新登录", status_code=401) from exc
    except jwt.InvalidTokenError as exc:
        raise AliothError(message="登录态无效，请重新登录", status_code=401) from exc

    username = str(payload.get("username", "")).strip()
    if not username:
        raise AliothError(message="登录态无效，请重新登录", status_code=401)

    user_row = await repository.find_by_username(username)
    if user_row is None or user_row["status"] != "enabled":
        raise AliothError(message="登录态无效，请重新登录", status_code=401)
    if _tenant_disabled(user_row):
        raise AliothError(message="所属公司已停用，请联系总部管理员", status_code=401)
    return _build_user_response(user_row)


async def require_agent_permission(
    current_user: Annotated[AuthUserResponse, Depends(require_auth_user)],
) -> AuthUserResponse:
    """限制 Agent 调试/运行接口，仅允许具备 Agent 执行权限的用户调用。"""
    if "agentRuns" not in current_user.permissions:
        raise AliothError(message="无权限执行 Agent 运行", status_code=403)
    return current_user


async def require_cost_reports_permission(
    current_user: Annotated[AuthUserResponse, Depends(require_auth_user)],
) -> AuthUserResponse:
    """成本报表是全平台的模型开销，不按公司拆分，只放行总部账号。"""
    if "costReports" not in current_user.permissions:
        raise AliothError(message="无权限查看成本报表", status_code=403)
    return current_user


async def require_access_scope(
    current_user: Annotated[AuthUserResponse, Depends(require_auth_user)],
) -> AccessScope:
    """把数据库加载的当前用户投影为不可由客户端伪造的对象访问范围。"""

    try:
        return access_scope_from_user(current_user)
    except ValueError as exc:
        raise AliothError(message="登录态缺少有效租户主体", status_code=401) from exc


@router.post("/auth/login", response_model=LoginResponse, summary="登录管理后台")
async def login(
    payload: LoginRequest,
    repository: Annotated[PostgresUserRepository, Depends(_get_user_repository)],
) -> LoginResponse:
    username = payload.username.strip()
    if not username or not payload.password.strip():
        raise AliothError(message="账号和密码不能为空")

    user_row = await repository.find_by_username(username)
    if user_row is None:
        raise AliothError(message="账号或密码错误", status_code=401)
    if user_row["status"] != "enabled":
        raise AliothError(message="账号已禁用，请联系管理员", status_code=401)
    if not verify_password(payload.password, str(user_row["password_hash"])):
        raise AliothError(message="账号或密码错误", status_code=401)
    if _tenant_disabled(user_row):
        raise AliothError(message="所属公司已停用，请联系总部管理员", status_code=401)

    await repository.touch_last_login(str(user_row["id"]))
    user = _build_user_response(user_row)
    issued_at = datetime.now(tz=UTC)
    expires_at = issued_at + timedelta(hours=12)
    token = _encode_token(user, issued_at=issued_at, expires_at=expires_at)
    return LoginResponse(token=token, user=user, expires_at=expires_at)


@router.get("/auth/me", response_model=AuthUserResponse, summary="获取当前登录用户")
async def get_current_user(
    current_user: Annotated[AuthUserResponse, Depends(require_auth_user)],
) -> AuthUserResponse:
    return current_user


__all__ = [
    "AccessScope",
    "AuthUserResponse",
    "LoginRequest",
    "LoginResponse",
    "access_scope_from_user",
    "download_ticket_signing_secret",
    "hash_password",
    "require_access_scope",
    "require_agent_permission",
    "require_auth_user",
    "verify_password",
]
