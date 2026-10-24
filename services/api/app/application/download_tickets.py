"""资源下载短时票据。

票据只携带资源类型、资源身份和审计主体，默认 60 秒过期；对象权限必须在签发前由
PostgreSQL owner/tenant 事实裁决。票据不能用于普通业务 API，也不能换取登录态。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt


class DownloadTicketError(ValueError):
    """下载票据无效、过期或资源身份不匹配。"""


@dataclass(frozen=True)
class DownloadTicketClaims:
    resource_kind: str
    resource_id: str
    actor_user_id: str
    tenant_id: str
    expires_at: datetime
    ticket_id: str


class SignedDownloadTicketService:
    """签发和验签资源绑定的短时 JWT，不接受登录 JWT。"""

    _AUDIENCE = "alioth-resource-download"
    _TYPE = "resource_download_ticket"

    def __init__(self, *, secret: str, ttl_seconds: int = 60) -> None:
        if len(secret.encode("utf-8")) < 32:
            raise ValueError("下载票据签名密钥至少需要 32 字节")
        if not 1 <= ttl_seconds <= 300:
            raise ValueError("下载票据有效期必须在 1 到 300 秒之间")
        self._secret = secret
        self._ttl_seconds = ttl_seconds

    def issue(
        self,
        *,
        resource_kind: str,
        resource_id: str,
        actor_user_id: str,
        tenant_id: str,
        now: datetime | None = None,
    ) -> tuple[str, datetime]:
        issued_at = (now or datetime.now(UTC)).astimezone(UTC)
        expires_at = issued_at + timedelta(seconds=self._ttl_seconds)
        token = jwt.encode(
            {
                "typ": self._TYPE,
                "aud": self._AUDIENCE,
                "jti": str(uuid4()),
                "sub": actor_user_id,
                "tenant_id": tenant_id,
                "resource_kind": resource_kind,
                "resource_id": resource_id,
                "iat": int(issued_at.timestamp()),
                "exp": int(expires_at.timestamp()),
            },
            self._secret,
            algorithm="HS256",
        )
        return token, expires_at

    def verify(
        self,
        token: str,
        *,
        resource_kind: str,
        resource_id: str,
    ) -> DownloadTicketClaims:
        try:
            payload = jwt.decode(
                token,
                self._secret,
                algorithms=["HS256"],
                audience=self._AUDIENCE,
                options={"require": ["exp", "iat", "aud", "sub", "jti"]},
            )
        except jwt.InvalidTokenError as exc:
            raise DownloadTicketError("下载票据无效或已过期") from exc
        if payload.get("typ") != self._TYPE:
            raise DownloadTicketError("下载票据类型无效")
        if payload.get("resource_kind") != resource_kind:
            raise DownloadTicketError("下载票据资源类型不匹配")
        if payload.get("resource_id") != resource_id:
            raise DownloadTicketError("下载票据资源身份不匹配")
        try:
            expires_at = datetime.fromtimestamp(int(payload["exp"]), tz=UTC)
        except (KeyError, TypeError, ValueError, OSError) as exc:
            raise DownloadTicketError("下载票据到期时间无效") from exc
        actor_user_id = str(payload.get("sub") or "").strip()
        tenant_id = str(payload.get("tenant_id") or "").strip()
        ticket_id = str(payload.get("jti") or "").strip()
        if not actor_user_id or not tenant_id or not ticket_id:
            raise DownloadTicketError("下载票据缺少审计主体")
        return DownloadTicketClaims(
            resource_kind=resource_kind,
            resource_id=resource_id,
            actor_user_id=actor_user_id,
            tenant_id=tenant_id,
            expires_at=expires_at,
            ticket_id=ticket_id,
        )
