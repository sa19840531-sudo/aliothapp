"""E26：清单元数据与判定审计载荷的敏感字段与体积防线。

冻结依据：
- E26 验收：登录凭证、Cookie、令牌、原始截图 base64、完整 DOM
  不进入清单元数据与判定审计载荷

三类处置：

| 类别 | 例子 | 处置 |
|---|---|---|
| 凭证类键 | password / token / cookie / authorization / api_key | 值替换为 `[redacted]` |
| 大体积原文键 | dom / html / page_source / screenshot_base64 | 整键丢弃 |
| 超长字符串值 | 完整 DOM、base64 截图落在任意键下 | 截断并留长度与摘要 |

第三条是兜底：凭证与原文可能藏在调用方自定义的键名下，
仅靠键名黑名单挡不住，因此对**任意**超长字符串一律截断。
`data:` 内联资源同样压成摘要，避免整张图片进审计。

本模块只做投影，不改写任何原始证据（E02 条款 1.3）。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

REDACTED = "[redacted]"

# 审计载荷中单个字符串值的上限；超出只留摘要与长度
MAX_AUDIT_STRING_CHARACTERS = 512

# 审计载荷整体字节上限
MAX_AUDIT_PAYLOAD_BYTES = 16_384

# 递归深度上限，避免异常结构撑爆栈
_MAX_DEPTH = 12

# 凭证类键：整词或以此结尾/开头，避免误伤 author / tokenizer 之类
_SENSITIVE_KEY_PATTERN = re.compile(
    r"(^|_)("
    r"password|passwd|pwd|secret|token|authorization|auth_header|cookie|cookies|"
    r"api_key|apikey|access_key|secret_key|private_key|credential|credentials|"
    r"bearer|jwt|session_token|refresh_token|otp|signature"
    r")($|_)",
    re.IGNORECASE,
)

# 大体积原文键：审计不需要原文，整键丢弃
_BULK_CONTENT_KEYS = frozenset(
    {
        "base64",
        "dom",
        "dom_snapshot",
        "full_dom",
        "html",
        "image_base64",
        "outer_html",
        "page_source",
        "raw_html",
        "screenshot",
        "screenshot_base64",
    }
)


def is_sensitive_key(key: str) -> bool:
    """键名是否属于凭证类。"""
    return _SENSITIVE_KEY_PATTERN.search(str(key)) is not None


def is_bulk_content_key(key: str) -> bool:
    """键名是否属于大体积原文类。"""
    return str(key).strip().casefold() in _BULK_CONTENT_KEYS


def redact_audit_payload(
    value: Any,
    *,
    max_string_characters: int = MAX_AUDIT_STRING_CHARACTERS,
    max_bytes: int = MAX_AUDIT_PAYLOAD_BYTES,
) -> Any:
    """把任意结构收敛为可安全落审计的投影。"""
    redacted = _redact(value, max_string_characters=max_string_characters, depth=0)
    encoded = json.dumps(redacted, ensure_ascii=False, default=str).encode("utf-8")
    if len(encoded) <= max_bytes:
        return redacted
    return {
        "audit_payload_truncated": True,
        "byte_count": len(encoded),
        "maximum_bytes": max_bytes,
        "keys": (
            sorted(str(key) for key in redacted)
            if isinstance(redacted, dict)
            else None
        ),
    }


def _redact(value: Any, *, max_string_characters: int, depth: int) -> Any:
    if depth > _MAX_DEPTH:
        return "[depth_limit]"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if is_bulk_content_key(key):
                continue
            if is_sensitive_key(key):
                result[str(key)] = REDACTED
                continue
            result[str(key)] = _redact(
                item,
                max_string_characters=max_string_characters,
                depth=depth + 1,
            )
        return result
    if isinstance(value, (list, tuple)):
        return [
            _redact(item, max_string_characters=max_string_characters, depth=depth + 1)
            for item in value
        ]
    if isinstance(value, str):
        return _redact_string(value, max_string_characters=max_string_characters)
    return value


def _redact_string(value: str, *, max_string_characters: int) -> str:
    if value.startswith("data:"):
        return _describe_oversized(value, kind="data_uri")
    if len(value) > max_string_characters:
        return _describe_oversized(value, kind="oversized_text")
    return value


def _describe_oversized(value: str, *, kind: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()
    return f"[{kind}:len={len(value)},sha256={digest[:16]}]"
