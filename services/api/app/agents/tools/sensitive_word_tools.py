"""敏感词检测 Tool：基于禁止内容库执行可审计穷举扫描。"""

from typing import Any

from services.api.app.agents.tools.registry import PermissionLevel, tool
from services.api.app.domain.rule.forbidden_content import scan_sensitive_words


def detect_sensitive_words(text: str) -> dict[str, Any]:
    """对待检文本执行禁止内容库多分类扫描。"""
    return scan_sensitive_words(text)


@tool(
    name="sensitive_word_detection_tool",
    description=(
        "对采集到的文本执行禁止内容库多分类穷举扫描，返回命中词、分类、位置与白名单状态。"
    ),
    category="text",
    permission_level=PermissionLevel.SAFE,
)
def sensitive_word_detection_tool(text: str) -> dict[str, Any]:
    """Agent 可调用的敏感词检测入口。"""
    return detect_sensitive_words(text)
