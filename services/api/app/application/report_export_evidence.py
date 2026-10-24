"""报告导出已有证据的内嵌与省略说明；不补造或替换判定材料。"""

from __future__ import annotations

import base64
import hashlib
from typing import Any


def referenced_artifact_ids(items: list[dict[str, Any]]) -> list[str]:
    """按规则引用顺序去重，保证多张证据的展示顺序可追溯。"""
    return list(dict.fromkeys(
        str(artifact_id)
        for item in items
        for artifact_id in item.get("evidence_artifact_ids") or []
    ))


async def collect_export_evidence(
    artifact_ids: list[str],
    metadata: dict[str, dict[str, Any]],
    store: Any,
    *,
    omissions: dict[str, str],
    max_bytes: int,
    max_count: int,
    require_checksum: bool = False,
) -> dict[str, str]:
    """保留所有可用截图；容量、读取和冻结校验失败均记录真实原因。"""
    images: dict[str, str] = {}
    for artifact_id in artifact_ids:
        entry = metadata.get(artifact_id)
        if entry is None:
            omissions[artifact_id] = "冻结报告未提供该材料" if require_checksum else "未找到该材料"
            continue
        content_type = str(entry.get("content_type") or "")
        if not content_type.startswith("image/"):
            omissions[artifact_id] = "非图片材料，未内嵌"
            continue
        storage_path = str(entry.get("storage_path") or "")
        checksum = str(entry.get("checksum") or "")
        if not storage_path or (require_checksum and not checksum):
            omissions[artifact_id] = "材料存储信息不完整"
            continue
        if len(images) >= max_count:
            omissions[artifact_id] = f"超过全报告 {max_count} 张内嵌上限"
            continue
        try:
            content = await store.read(storage_path)
        except Exception:  # noqa: BLE001 - 明确保留失败信息，单张证据不阻断其他材料
            omissions[artifact_id] = "材料读取失败"
            continue
        if not content:
            omissions[artifact_id] = "材料内容为空"
            continue
        if len(content) > max_bytes:
            omissions[artifact_id] = f"超过单张 {max_bytes // 1000} KB 内嵌上限"
            continue
        if (
            require_checksum
            and hashlib.sha256(content).hexdigest() != checksum.removeprefix("sha256:").casefold()
        ):
            omissions[artifact_id] = "冻结材料校验失败"
            continue
        encoded = base64.b64encode(content).decode("ascii")
        images[artifact_id] = f"data:{content_type};base64,{encoded}"
    return images


def export_evidence_note(
    artifact_ids: list[str], images: dict[str, str], omissions: dict[str, str],
) -> str:
    """即使材料缺失，也保留原引用标识，便于回到平台核对。"""
    ids = list(dict.fromkeys(artifact_ids))
    if not ids:
        return "判定材料：本条结果未关联截图证据。"
    included = sum(artifact_id in images for artifact_id in ids)
    missing = [
        f"{artifact_id}（{omissions.get(artifact_id, '未内嵌')}）"
        for artifact_id in ids if artifact_id not in images
    ]
    note = f"判定材料：已内嵌 {included} 张截图；原材料标识：{'、'.join(ids)}。"
    if missing:
        note += f"未内嵌材料：{'；'.join(missing)}。"
    return note
