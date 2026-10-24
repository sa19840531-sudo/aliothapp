"""从 Catalog 与运行时账本生成探索进度，成功、失败和预算跳过分别展示。"""

from collections import Counter
from collections.abc import Mapping

from services.api.app.domain.exploration import WebMenuTerminalStatus


def web_menu_progress(summary: Mapping[str, object]) -> dict[str, int]:
    rows = [
        row
        for key in ("menu_ledger", "runtime_discovered_ledger")
        for row in (summary.get(key) or ())
        if isinstance(row, Mapping)
    ]
    counts = Counter(str(row.get("status") or "") for row in rows)
    terminal = {item.value for item in WebMenuTerminalStatus}
    failures = {
        "popup_blocked", "human_verification_blocked", "unsafe_navigation",
        "page_type_unclassified", "detail_selection_untrusted", "detail_page_type_mismatch",
        "navigation_failed", "allowlist_drifted",
    }
    return {
        "terminal": sum(count for status, count in counts.items() if status in terminal)
        if rows else max(0, int(summary.get("menu_total") or 0)
                         - int(summary.get("frontier_remaining") or 0)),
        "details": counts["detail_captured"],
        "skipped": counts["menu_limit_skipped"],
        "failed": sum(counts[status] for status in failures),
    }
